"""
bot/worker.py — 单账号工作线程（第 7 步 / 第 8 步任务链）

每账号一个 AccountWorker(QThread)，跑"截图→任务tick"的循环。
任务链：task_name 传 "任务A,任务B"（逗号分隔），A 完成后自动接续 B，
全部完成（或某个失败/被停止）后线程退出。

⚠ 弹窗拦截 / 后台巡逻（MONITORING）/ 回主城自愈子系统已整体下线
（用户指令"直接删，一起下线"）：任务链跑完 = 线程退出，不再巡逻；
任务失败 = 记日志停止。单步失败只做「截图留证」即判失败（FSM 层，
返回键已随其一起下线，不再按 BACK 回退重试）。

本步学到的知识点（多线程控制）：
1. QThread：继承它、重写 run()；start() 后 run 在【新线程】里执行
2. 跨线程通信只靠信号：worker 在后台线程 emit，Qt 自动排队投递到界面线程
3. 线程控制用标志位而不是杀线程：暂停/恢复/软停/硬停（见 request_* 方法）
4. 有界等待铁律：线程里绝不允许无限阻塞，所有 sleep 都是小步循环
"""

import random
import threading
import time

from PySide6.QtCore import QThread, Signal

from bot.context import BotContext
from bot.fsm import FSM, AccountBannedExit, FlowTimeoutExit, save_fail_capture
from bot.frame_loop import clamp_frame_interval, interruptible_wait, run_frame_loop
from bot.frame_source import FrameSource
from bot.job_state import WorkerState, can_transition
from bot.tasks import get_task
from core import capture, ldplayer
from core.config_manager import get_config


def _account_task_cfg(ld_index) -> dict:
    """
    读某实例在 account_tasks 里的独立配置。JSON 序列化会把键强制变 str，
    所以 int/str 两种键都查，兼容不同来源的调用。没有返回空 dict。
    """
    acct = get_config().get("account_tasks", {}) or {}
    if not isinstance(acct, dict):
        return {}
    if isinstance(ld_index, str):
        keys = (ld_index, int(ld_index)) if ld_index.isdigit() else (ld_index,)
    else:
        keys = (ld_index, str(ld_index))
    for key in keys:
        val = acct.get(key)
        if isinstance(val, dict):
            return val
    return {}


def resolve_task_name(ld_index: int, fallback: str = "flow") -> str:
    """
    每实例独立任务链（线 2-6）：config.account_tasks[ld_index]["queue"] 存在时
    优先用实例队列，否则回退全局任务选择 fallback（向后兼容）。
    返回逗号分隔的任务名字符串，多任务串成链顺序执行。

    任务名可带 ":参数" 记号（流程即任务改版：清单里的库换算成 "flow:流程名"，
    冒号后是该任务的参数覆盖，见 run() 里的 partition）。
    """
    queue = _account_task_cfg(ld_index).get("queue")
    if isinstance(queue, list) and queue:
        return ",".join(str(t) for t in queue)
    return fallback


def _account_id_for(ld_index: int) -> int | None:
    """按 ld_index 找 accounts 表里的账号 id；没建账号档返回 None（跳过写库）。"""
    from core import database, run_log
    try:
        for a in database.list_accounts():
            if a.get("ld_index") == ld_index:
                return a.get("id")
    except Exception as e:
        # 读库失败也返回 None（= 本实例从此静默不写库），但必须落一条盘：
        # 否则"挂机一天任务记录全空"没有任何线索（审查实锤的排查黑洞）。
        run_log.log(ld_index, "WARN", f"读账号档失败，本实例运行记录不会入库：{e}")
        return None
    return None


class AccountWorker(QThread):
    # ── 信号（GUI 只通过这些感知 worker，全部跨线程安全）──
    state_changed = Signal(int, str)      # (实例index, starting/running/paused/stopped/error)
    task_changed = Signal(int, str, str)  # (实例index, 任务名, 当前步骤名)
    action_log = Signal(int, str, str)    # (实例index, INFO/WARN/ERROR, 消息)
    # 任务链结局（表格"完成度"列）：True=整条链跑完，False=中途失败/被停
    chain_finished = Signal(int, bool)
    # data_updated（1Hz 心跳带弹窗快照）已随弹窗监控子系统一起下线

    def __init__(self, ld_index: int, task_name: str = "flow", parent=None):
        super().__init__(parent)
        self.ld_index = ld_index
        # 任务链：逗号分隔，如 "任务A,任务B"（前一个完成后自动接续后一个）。
        # 线 2-6：优先实例级队列 account_tasks[ld_index]（每实例独立任务），
        # 无实例级配置时回退全局 task_name（向后兼容）。
        resolved = resolve_task_name(ld_index, task_name or "flow")
        self.task_names = [t.strip() for t in resolved.split(",") if t.strip()] or ["flow"]
        self._pause = threading.Event()   # 置位 = 暂停中
        self._stop = threading.Event()    # 置位 = 请求停止（软停）
        self._hard_stop = False           # 硬停：立即退出
        # 轮询间隔：走配置单入口 general.frame_interval，校验钳位收在共享核
        # clamp_frame_interval（与 flow_test 同一口径，坏值=拍失控）。
        self.frame_interval = clamp_frame_interval(get_config().get("general.frame_interval", 0.5))
        self.ctx = BotContext(ld_index=ld_index)
        # 线 2-8：绑定账号 id（供写库），没建账号档则 None（写库安全跳过）。
        # ⚠ 查库挪到 run() 开头：__init__ 在界面线程执行，SQLite 同步读
        # 慢一次卡一次 UI；这里只占位，线程侧再填。
        self._account_id = None
        self.ctx.account_id = None
        self._last_fail_path = ""      # 最近一次失败截图路径（写库用）
        self._last_fail_reason = ""    # 最近一次失败原因（写库用）
        self._cur_task_label = ""      # 当前任务显示名（含 flow 的流程名，task_changed 上报用）
        # 取帧+校准统一层（与 flow_test/flow_run 三份收口，见 bot/frame_source.py）：
        # 冷却/兜底/adb 一次性 WARN/漂移复核全内化。quick_calib_log=True——
        # 正式挂机历史就在重校准前后播报，文案零变化。
        self._fs = FrameSource(ld_index, self.ctx,
                               lambda lvl, msg: self.action_log.emit(ld_index, lvl, msg),
                               quick_calib_log=True)
        self._state = WorkerState.STOPPED  # 显式状态机初始态（第 9 步收敛）

    # ── 对外控制接口（GUI 调用，都是置标志，立即返回）──
    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def state_value(self) -> str:
        """当前显式状态（UI 刷新表格用，弹窗监控分离改版：不再只看 paused）。"""
        return self._state.value

    def _set_state(self, new_state: WorkerState):
        """状态单一出口：先做合法转移校验，通过才 emit（防非法跳转）。"""
        if not can_transition(self._state, new_state):
            # 非法跳转：拒绝并提醒（不 emit 假状态），便于暴露逻辑异常
            self.action_log.emit(self.ld_index, "WARN",
                                 f"非法状态跳转：{self._state.value} → {new_state.value}（已拒绝）")
            return
        self._state = new_state
        self.state_changed.emit(self.ld_index, new_state.value)

    def request_pause(self):
        self._pause.set()

    def request_resume(self):
        self._pause.clear()

    def request_stop(self, hard: bool = False):
        if hard:
            self._hard_stop = True
        self._stop.set()
        self._pause.clear()  # 暂停状态下也要能停

    # ── 线程主体 ──
    def run(self):
        # 线 2-8：绑定账号 id（供写库），没建账号档则 None（写库安全跳过）。
        # 从构造函数挪到这里：__init__ 跑在 GUI 线程，批量开 N 个 worker 时
        # 每次都同步扫一遍账号表，界面卡一下；挪进线程主体后 GUI 侧零 DB 读。
        # 必须排在 _run_loop 之前——AccountBannedExit 处理器要用 self._account_id。
        self._account_id = _account_id_for(self.ld_index)
        self.ctx.account_id = self._account_id
        try:
            self._run_loop()
        except FlowTimeoutExit as e:
            # 节点配置「直接退出程序」：硬终止穿透恢复链直达此处。
            # 这是用户安排的预期行为（不是崩溃），日志说明原因即可收摊。
            self.action_log.emit(self.ld_index, "ERROR", f"{e}（按节点配置直接退出程序）")
            self._set_state(WorkerState.STOPPED)
        except AccountBannedExit as e:
            # watch「封号停用」行命中：留证截图 → 写库标记封号（enabled=0 +
            # 原因入 extra_json）→ 停本实例线程。标记后全部挂机/右键挂机跳过，
            # 表格红显，右键「解禁」恢复 —— 封号是账号级判决，线程自己不做主，
            # 门都归主窗口侧统一把。
            shot = ""
            try:
                shot = save_fail_capture(self.ctx, "封号", str(e)[:20])
            except Exception:
                pass    # 留证失败不拦标记（截图环节最易因窗口抖动出错）
            from core import database
            marked = False
            if self._account_id is not None:
                try:
                    marked = database.set_banned(self._account_id, True,
                                                 reason=str(e))
                except Exception as db_err:
                    self.action_log.emit(self.ld_index, "WARN",
                                         f"封号标记写库失败：{db_err}")
            self.action_log.emit(
                self.ld_index, "ERROR",
                f"{e} —— 本实例挂机收工"
                + ("，账号已标记封号（表格红显，右键可解禁）" if marked else "")
                + (f"，留证截图：{shot}" if shot else ""))
            self._set_state(WorkerState.STOPPED)
        except Exception as e:  # 线程里兜底：任何异常都要报出来再退
            self.action_log.emit(self.ld_index, "ERROR", f"挂机线程异常退出：{e}")
            self._set_state(WorkerState.ERROR)
        finally:
            # 异常态保留：旧版这里一律覆写 STOPPED，UI 收不到"异常"信号，
            # 表格里崩溃退出和正常停止长得一模一样，事后根本查不出谁炸过。
            # UI 侧对 stopped/error 的收摊逻辑一致（移出注册表），保留 ERROR
            # 只是多一分诚实，线程死活本来就以 isRunning() 为准。
            if self._state is not WorkerState.ERROR:
                self._set_state(WorkerState.STOPPED)

    def _run_loop(self):
        self._set_state(WorkerState.STARTING)
        inst = ldplayer.get_instance(self.ld_index)
        if inst is None or not inst.running:
            self.action_log.emit(self.ld_index, "ERROR", "实例未运行，无法挂机")
            self._set_state(WorkerState.ERROR)
            return
        self.ctx.hwnd = inst.top_hwnd
        # 注入日志回调：任务步骤通过 ctx.log 写日志，统一走 action_log 信号进界面
        self.ctx.log = lambda msg, lvl="INFO": self.action_log.emit(self.ld_index, lvl, msg)
        # 2026-10-11：取帧主路 = adb 单次快照，无链可预热/待等（scrcpy 后端已移除）。
        self._calibrate()
        # 分辨率取证（稳定性审计 ⑦）：模板基准 = capture 基准 = 安卓分辨率
        # 三方一致是架构红线 2；不一致时识别照样命中、tap 系统性点歪且日志
        # 完全看不出来。启动核对一次，歪了当场点名，比事后查一天"点空"便宜。
        try:
            asz = ldplayer.android_size(self.ld_index)
            if asz and asz != (capture.BASE_WIDTH, capture.BASE_HEIGHT):
                self.action_log.emit(
                    self.ld_index, "WARN",
                    f"安卓分辨率 {asz[0]}x{asz[1]} ≠ 模板基准 "
                    f"{capture.BASE_WIDTH}x{capture.BASE_HEIGHT}："
                    f"识别会命中但点击点歪，请改模拟器分辨率或重采模板")
        except Exception as e:
            self.action_log.emit(self.ld_index, "WARN",
                                 f"安卓分辨率核对失败（不拦启动）：{e}")
        self._set_state(WorkerState.RUNNING)

        # 启动错峰（性能审计 P1-⑩）：多开同秒起跑时，每拍的截帧+匹配高峰全部
        # 同相位，CPU 每个 frame_interval 撞一次尖峰；起跑前随机偏一帧间隔内
        # 的相位，各实例节拍自然错开。停机请求照旧由下面任务链的逐轮检查收口。
        self._interruptible_wait(random.uniform(0, self.frame_interval))

        # ── 任务链：逐个执行；done → 下一个；failed/停止 → 结束 ──
        # 任务名支持 "标识:参数" 记号（流程即任务：flow:登录 = 跑名为 登录 的流程，
        # 参数直接烧在任务链里，不依赖「任务设置」页的 flow_name）
        queue_done = True
        for item in self.task_names:
            if self._stop.is_set() or self._hard_stop:
                queue_done = False
                break
            tname, _, tparam = item.partition(":")
            task_cls = get_task(tname)
            if task_cls is None:
                self.action_log.emit(self.ld_index, "ERROR", f"未知任务：{item}")
                queue_done = False
                break
            task = task_cls()
            # 现阶段只有 flow 任务用 ":参数"（flow_name），别给未知键留活口
            inline_params = {"flow_name": tparam} if (tparam and tname == "flow") else {}
            # 上报用的任务显示名：唯一任务是 flow，直接显示流程名本体
            # （多实例跑不同流程也分得清谁是谁）
            self._cur_task_label = tparam if (tparam and tname == "flow") else task.display_name
            self.action_log.emit(
                self.ld_index, "INFO",
                f"开始执行任务：{task.display_name}" + (f"·{tparam}" if tparam else ""))
            # 任务↔模板绑定：开跑前按 required_templates 查缺，缺图当场列清单
            # （只 WARN 不拦截 —— 用户可能就要跑不依赖缺图的部分，让它自己撞）
            self._warn_missing_templates(task)
            start_at = time.time()
            result = self._run_task_with_recovery(task, inline_params)
            # 线 2-8：任务结束写回数据库（已建账号档才写，静默容错）
            self._record_task(task, result, start_at)
            if result == "done":
                self.action_log.emit(self.ld_index, "INFO", f"任务完成：{task.display_name}")
            else:
                queue_done = False
                break  # failed（已记日志）或停止请求

        # 任务链跑完（或失败/被停）= 线程退出。弹窗拦截/巡逻改版已下线，
        # 不再转 MONITORING 保活。
        # chain_finished 先于 _set_state(STOPPED)（在 run() 的 finally 里）发出：
        # 跨线程信号按发出顺序投递，界面侧先记结局、后处理 stopped，不会丢
        self.chain_finished.emit(self.ld_index, queue_done)
        self.action_log.emit(self.ld_index, "INFO", "挂机线程退出")

    def _run_task_with_recovery(self, task, inline_params: dict | None = None) -> str:
        """
        跑一个任务直到结束。
        单步失败的留证（截图存 logs/fail_capture）在 FSM 内部（层 1）。
        ⚠ 原层 2（整任务失败 → 回主城锚点重跑 N 次）随弹窗/主城锚点子系统一起下线。
        inline_params：任务链记号带进来的参数（"flow:名字" → flow_name），
        优先级最高 —— 清单/队列里点名了跑哪条流程，设置页的全局值让位。
        返回 "done" / "failed" / "stopped"。
        """
        task_params = self._task_params_for(task)
        task_params.update(inline_params or {})

        fsm = FSM(task.build_steps(self.ctx, params=task_params),
                  self.ctx, task_name=task.name, quiet=True)
        return self._run_single_task(task, fsm)

    def _warn_missing_templates(self, task):
        """任务声明的 required_templates 里有账本找不到的 → 逐条 WARN 提醒采集。"""
        from core import template_manager
        refs = getattr(task, "required_templates", None) or []
        missing = template_manager.check_missing(refs)
        for cat, name in missing:
            self.action_log.emit(
                self.ld_index, "WARN",
                f"任务「{task.display_name}」缺模板：{cat}/{name}，"
                f"请在模板工具采集到该目录后再挂机")

    def _task_params_for(self, task) -> dict:
        """
        两段式参数合并（线 2-7，ALAS config.override 思想）：
        先取全局 task_params[task.name]，再用实例级 account_tasks[ld_index]["params"][task.name]
        覆盖（实例级优先）。无实例级配置时行为与改造前完全一致。
        """
        cfg = get_config()
        base = dict(cfg.get("task_params", {}).get(task.name, {}) or {})
        override = _account_task_cfg(self.ld_index).get("params", {}).get(task.name, {})
        if isinstance(override, dict):
            merged = dict(base)
            merged.update(override)
            return merged
        return base

    def _record_task(self, task, result: str, start_at: float):
        """线 2-8：任务结束写回 task_history（已建账号档才写，任何异常静默吞掉）。"""
        if self._account_id is None:
            return
        try:
            from core import database
            now = time.time()
            status = "success" if result == "done" else ("fail" if result == "failed" else "skip")
            fail_reason = ""
            screenshot_path = ""
            if result == "failed":
                fail_reason = getattr(self, "_last_fail_reason", "") or "任务失败"
                screenshot_path = getattr(self, "_last_fail_path", "") or ""
            database.add_task_history(
                self._account_id, task.name, status,
                start_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_at)),
                end_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                fail_reason=fail_reason, screenshot_path=screenshot_path)
            database.update_account(self._account_id, status=status, current_task=task.name)
        except Exception as e:
            self.action_log.emit(self.ld_index, "WARN", f"写数据库失败（忽略）：{e}")

    def _run_single_task(self, task, fsm) -> str:
        """
        跑一个任务直到结束。返回 "done" / "failed" / "stopped"。

        拍循环（截帧→tick→兜底→终态处置→节拍等待）交 bot/frame_loop.run_frame_loop
        ——与 flow_test 共用一个核，防两边漂移（此前 flow_test 就没有拍级兜底）。
        逐拍兜底（7×24 稳定性审计 ①）在核里：杂散异常降级为"本拍作废、退避重试"，
        连败超阈值才判 failed 交人工。⚠ FlowTimeoutExit/AccountBannedExit 由核
        原样 re-raise 穿透到这里 run() 的专属 handler——旧版通用 except 把它们
        也吞了，「超时退出程序」「封号标记」节点在正式挂机里双双失效（本次审计
        最高优先级修复）。挂机专属语义（暂停状态上报/写库字段/话术）留在下面回调里。
        """
        last_step = [""]    # 上次上报的步骤名（变化时才上报，不刷屏）

        def _beat(res):
            """拍结果处置：返回终态字符串则循环退出，None = 继续。"""
            if res in ("abort", "flow_end", "loop_continue"):
                # 「结束流程」节点命中（顶层没人吸收，三种范围都算提前收尾）：视为成功
                self.action_log.emit(self.ld_index, "INFO",
                                     "流程被「结束流程」节点主动结束，任务完成")
                return "done"
            if res == "failed":
                # 失败：不无限重试（避免刷屏循环），停下让用户看失败截图修正后重挂。
                # FSM 留证的截图路径/原因接线到本实例字段（_record_task 写库读它俩）
                self._last_fail_path = getattr(fsm, "last_fail_path", "") or ""
                self._last_fail_reason = getattr(fsm, "last_fail_reason", "") or "任务失败"
                self.action_log.emit(self.ld_index, "ERROR",
                                     "任务失败，挂机停止（查看 logs/fail_capture 截图，修正后重新挂机）")
                return "failed"
            if res == "done":
                return "done"
            # 步骤变化上报（观察任务走到哪一步）
            cur_step = fsm.steps[fsm.idx].name if fsm.idx < len(fsm.steps) else "完成"
            if cur_step != last_step[0]:
                last_step[0] = cur_step
                self.task_changed.emit(self.ld_index, self._cur_task_label, cur_step)
                self.action_log.emit(self.ld_index, "INFO", f"当前步骤：{cur_step}")
            return None

        return run_frame_loop(
            self._grab_frame, fsm.tick,
            lambda: self._stop.is_set() or self._hard_stop,
            frame_interval=self.frame_interval,
            log=lambda lvl, msg: self.action_log.emit(self.ld_index, lvl, msg),
            is_paused=self._pause.is_set,
            on_pause=lambda: self._set_state(WorkerState.PAUSED),
            on_resume=lambda: self._set_state(WorkerState.RUNNING),
            instance_alive=self._instance_alive,
            on_result=_beat,
            on_exc_failed=lambda e, n: setattr(
                self, "_last_fail_reason", f"拍级连续异常 {n} 次：{e}"),
            wait_fn=self._interruptible_wait,
            msg_grab_fail="截图失败（窗口最小化/被遮挡？adb 模式下=模拟器暂时无响应？），等待恢复中",
        )

    def _instance_alive(self) -> bool:
        """本实例模拟器是否还在跑（截图失败时区分 永久关闭/暂时遮挡，核回调）。
        薄壳调 FrameSource（快照抽风凭活句柄兜底的语义见 bot/frame_source.ensure_alive）。"""
        return self._fs.ensure_alive()

    def _interruptible_wait(self, secs: float) -> bool:
        """有界小步睡眠（启动错峰用）：实现收口在 bot.frame_loop.interruptible_wait。"""
        return interruptible_wait(secs, lambda: self._stop.is_set() or self._hard_stop)

    # ── 内部：截图与校准（全套样板收口 bot/frame_source.FrameSource，这里薄壳）──
    def _calibrate(self):
        """启动校准：量"安卓画面在窗口里的偏移"。有界预算解析/1 轮收紧、
        循环内快重校准及其 5 秒冷却全内化在 FrameSource（grab 自动触发，
        本方法只剩启动播报）。语义与警告详见 bot/frame_source.startup_calibrate。"""
        self.action_log.emit(self.ld_index, "INFO", "正在校准窗口偏移……")
        self._fs.startup_calibrate()
        if self.ctx.calib:
            self.action_log.emit(self.ld_index, "INFO",
                                 f"校准完成：偏移 ({self.ctx.calib['offset_x']},{self.ctx.calib['offset_y']})")
        else:
            self.action_log.emit(self.ld_index, "WARN", "校准失败，使用整窗缩放回退模式")

    def _grab_frame(self):
        """本帧截图——快照判活兜底/跟句柄+冷却重校准/adb 跳裁剪+一次性 WARN/
        漂移复核，全套见 bot/frame_source.FrameSource.grab。"""
        return self._fs.grab()
