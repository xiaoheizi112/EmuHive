"""
bot/flow_test.py — 流程编排「试运行」线程

流程编辑器里点「▶ 测试流程」：把当前编排好的图（哪怕还没保存）
直接在指定模拟器实例上跑一遍，边跑边播步骤日志，跑完/失败/被停
自动收摊。正式挂机（bot/worker.py 的 AccountWorker）零改动 ——
测试走的是同一条执行链（core.capture 截图 →
bot.tasks.flow.compile_graph 编译 → FSM.tick 推进），只是生命周期
不同：正式链是任务队列，测试是一次性试跑。

为什么不塞进 AccountWorker：
- AccountWorker 构造吃 task_name，要跑自定义流程得先在配置里注册
  临时任务、还得绕开每实例任务队列 —— 为了试跑污染正式配置，不值。
- 测试失败不需要"回主城自愈重跑"，也不该进后台巡逻：一码归一码。
- 本线程跑起来后正式挂机同实例会撞车（两边都在点同一个窗口），
  占用检查归 UI 外壳（WorkbenchDialog 问 MainWindow）。

FSM 失败处理（failed → 截图留证 → 判失败）照单全收，
与正式挂机行为一致。日志走 action_log 信号，UI 侧自行落地。
"""

import threading

from PySide6.QtCore import QThread, Signal

from bot.context import BotContext
from bot.fsm import FSM, FlowTimeoutExit, SENTINEL_KINDS
from bot.frame_loop import clamp_frame_interval, run_frame_loop
from bot.frame_source import FrameSource
from bot.tasks.flow import compile_graph
from core import capture, ldplayer


class FlowTestWorker(QThread):
    # (实例index, INFO/WARN/ERROR, 消息) —— 与 AccountWorker.action_log 同形状，
    # UI 侧一套处理逻辑两用
    action_log = Signal(int, str, str)
    # (实例index, 步骤名) —— 当前推进到的 FSM 步骤，界面显示"跑到哪了"
    step_changed = Signal(int, str)
    # (实例index, "running" / "done" / "failed" / "stopped" / "aborted")
    finished_state = Signal(int, str)
    # (实例index, {节点id: "ok"/"failed"/"pending"}) —— 跑完时各节点终态，
    # 编辑器拿去在画布节点角上盖 ✓/✗/… 角标。只读展示，不存盘。
    node_states = Signal(int, dict)

    def __init__(self, ld_index: int, graph: dict, parent=None, flow_name: str = ""):
        super().__init__(parent)
        self.ld_index = ld_index
        self._flow_name = flow_name   # 被试流程的存量名（可能为空 = 新流程没保存）
        # 深拷贝快照：编译/执行期间用户改画布、甚至保存/删流程，
        # 都不影响已在跑的这一次（图 dict 是编辑器的活数据）
        import copy
        self._graph = copy.deepcopy(graph or {"version": 2, "nodes": [], "edges": []})
        self._stop = threading.Event()
        self._steps = None        # _run 编译出的步骤链，收尾时从 GraphStep._nd 取终态
        from core.config_manager import get_config
        # 拍间隔走共享核校验钳位（此前裸 float：坏值=拍失控，与 worker 漂移点之一）
        self.frame_interval = clamp_frame_interval(get_config().get("general.frame_interval", 0.5))
        self.ctx = BotContext(ld_index=ld_index)
        # 取帧+校准统一层（与 worker/flow_run 三份收口，见 bot/frame_source.py）：
        # 冷却/兜底/adb 一次性 WARN/漂移复核全内化。quick_calib_log=False——
        # 测试侧重校准历史就静默，播报只在挂机侧（文案零变化）。
        self._fs = FrameSource(ld_index, self.ctx,
                               lambda lvl, msg: self.action_log.emit(ld_index, lvl, msg))

    # 对外接口（GUI 线程调用，置标志立即返回）
    def request_stop(self):
        self._stop.set()

    # ── 线程主体 ──
    def run(self):
        result = "failed"
        try:
            result = self._run()
        except FlowTimeoutExit as e:    # on_timeout=exit：预期内的硬终止，照单播报
            self.action_log.emit(self.ld_index, "ERROR", f"{e}（按节点配置直接退出程序）")
            result = "aborted"
        except Exception as e:    # 线程兜底：任何异常都要播出来再退，不许静默死亡
            self.action_log.emit(self.ld_index, "ERROR", f"测试线程异常退出：{e}")
            result = "failed"
        finally:
            # 先把节点终态发回画布（角标回填），再播报整体收尾 —— 顺序固定，
            # UI 侧收完角标才解锁编辑控件，不会看到"锁还压着、角标后到"的闪跳。
            try:
                states = self._collect_node_states(result)
            except Exception:
                states = {}
            if states:
                self.node_states.emit(self.ld_index, states)
            self.finished_state.emit(self.ld_index, result)

    def _collect_node_states(self, result: str) -> dict:
        """把 GraphStep 的节点运行表换算成画布角标：{节点id: ok/failed/pending}。

        映射依据（bot/fsm.py _apply_step_result 的状态语义）：
        - done → ✓：跑到了终局。注意失败但接了 N 口的节点也收口成 done，
          这一型看不出失败，角标跟着显示 ✓ —— v1 接受此精度（要区得分
          口颜色得让引擎给每节点记"怎么收的口"，改动量不值）。
        - skipped → …：入边判跳过，压根没执行。
        - running → 整体 failed 时是非哨兵的"卡在这一步"，标 ✗；
          哨兵（watch/timer）本就该跑满全场，标 ✓；其余标 …。
        - idle → …：入边从未达成，没点亮过。
        """
        out = {}
        for st in (self._steps or []):
            nd = getattr(st, "_nd", None)
            if not isinstance(nd, dict):
                continue   # 空图/缺配置等降级编译产物是 LogStep，没有节点表
            for nid, rec in nd.items():
                state = rec.get("state")
                if state == "done":
                    out[nid] = "ok"
                elif state == "skipped":
                    out[nid] = "pending"
                elif state == "running":
                    if rec.get("kind") in SENTINEL_KINDS:
                        out[nid] = "ok"
                    elif result == "failed":
                        out[nid] = "failed"
                    else:
                        out[nid] = "pending"
                else:
                    out[nid] = "pending"
        return out

    def _run(self) -> str:
        inst = ldplayer.get_instance(self.ld_index)
        if inst is None or not inst.running:
            self.action_log.emit(self.ld_index, "ERROR", "实例未运行，无法测试")
            return "failed"
        self.ctx.hwnd = inst.top_hwnd
        self.ctx.log = lambda msg, lvl="INFO": self.action_log.emit(self.ld_index, lvl, msg)

        # 2026-10-11：取帧主路 = adb 单次快照，无链可预热/待等（scrcpy 后端已移除）。
        self.action_log.emit(self.ld_index, "INFO", "正在校准窗口偏移……")
        # 试运行校准收紧（同 bot/worker.py 启动路径，性能审计）：有界预算
        # 收口在 FrameSource.startup_calibrate（读配置 launch_calibrate_timeout，
        # 1 轮封顶），几何路径几乎必中，不值得在点下"测试"按钮后最坏干等近 3 分钟。
        self._fs.startup_calibrate()
        if not self.ctx.calib:
            self.action_log.emit(self.ld_index, "WARN", "校准失败，使用整窗缩放回退模式")

        # 试运行的图可能没保存（画布当前内容），子流程节点仍按名字到存量
        # 流程里找；被试流程自己以快照为准（改到一半没存也应跑看到的版本）。
        from core.config_manager import get_config
        flow_map = dict(get_config().get("custom_flows", {}) or {})
        if self._flow_name:
            flow_map[self._flow_name] = self._graph
        steps = compile_graph(self._graph, flow_map, flow_name=self._flow_name)
        self._steps = steps    # 收尾时从这里取 GraphStep._nd 换算节点角标
        n = len(self._graph.get("nodes") or [])
        self.action_log.emit(self.ld_index, "INFO",
                             f"开始测试流程：{n} 个节点 → {len(steps)} 个步骤")
        if n == 0:
            self.action_log.emit(self.ld_index, "WARN", "画布是空的，先添加节点再测试")
            return "stopped"

        fsm = FSM(steps, self.ctx, task_name="flow_test", quiet=True)
        last_step = [""]

        def _beat(res):
            """拍结果处置（保留原版顺序：先报步骤再判终态——测试 UI 要看停在哪个节点）。"""
            cur = fsm.steps[fsm.idx].name if fsm.idx < len(fsm.steps) else "完成"
            if cur != last_step[0]:
                last_step[0] = cur
                self.step_changed.emit(self.ld_index, cur)
                self.action_log.emit(self.ld_index, "INFO", f"当前步骤：{cur}")
            if res == "done":
                self.action_log.emit(self.ld_index, "INFO", "✓ 流程测试完成")
                return "done"
            if res in ("abort", "flow_end"):
                # 「结束流程」节点命中（顶层没人吸收，两种范围都算提前收尾）：测试视为完成
                self.action_log.emit(self.ld_index, "INFO", "流程被「结束流程」节点主动结束")
                return "done"
            if res == "failed":
                self.action_log.emit(
                    self.ld_index, "ERROR",
                    "✗ 流程测试失败（截图留证见 logs/fail_capture，修正后重试）")
                return "failed"
            return None

        # 拍循环交共享核 bot/frame_loop（与正式挂机同一条兜底语义，此前这里是
        # 裸 tick：任何拍级异常直接崩测试线程——flow_test 三大漂移点之一）；
        # FlowTimeoutExit 照旧穿透到 run() 的专属 handler 播 "aborted"。
        result = run_frame_loop(
            self._grab_frame, fsm.tick, self._stop.is_set,
            frame_interval=self.frame_interval,
            log=lambda lvl, msg: self.action_log.emit(self.ld_index, lvl, msg),
            instance_alive=self._instance_alive,
            on_result=_beat,
            msg_emu_gone="模拟器已关闭，测试中止",
            msg_grab_fail="截图失败（窗口最小化/被遮挡？adb 模式下=模拟器暂时无响应？），等待恢复",
        )
        if result == "stopped":
            self.action_log.emit(self.ld_index, "INFO", "测试已手动停止")
        return result

    def _instance_alive(self) -> bool:
        """快照抽风凭活句柄兜底、不误报「模拟器已关闭，测试中止」——薄壳调
        FrameSource（语义见 core.ldplayer.is_instance_alive，1607 移植机实测）。"""
        return self._fs.ensure_alive()

    def _grab_frame(self):
        """本帧截图——快照判活兜底/跟句柄+5 秒冷却重校准/adb 跳裁剪+一次性
        WARN/漂移复核，全套收口 bot/frame_source.FrameSource（与 worker 共用；
        测试侧重校准保持静默的历史行为由 quick_calib_log=False 守住）。"""
        return self._fs.grab()
