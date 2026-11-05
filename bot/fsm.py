"""
bot/fsm.py — FSM 状态机引擎（第 7 步）

任务 = 一组步骤按顺序推进的状态机。每个步骤是一个小状态，
引擎每轮"滴答"一次（tick），由步骤自己决定继续/成功/失败。

本步学到的知识点（状态机思想）：
- 每个步骤只回答一个问题："我现在该干嘛，干完了吗"
- 引擎每轮调用当前步骤的 tick()，步骤返回三选一：
    "running" = 还没完，下轮再来
    "ok"      = 完成，引擎前进到下一步骤
    "failed"  = 超时/重试耗尽，交给引擎走恢复流程
- 这样任务逻辑全是"无状态的小函数"组成的链，
  超时、重试、截图留证等通用机制全部集中在引擎里，任务作者不用管

五种步骤类型：
- WaitStep   等待：某模板出现/消失（固定超时），带 0.6 秒确认期防画面闪烁误判
- ActionStep 动作：执行点击等操作，然后校验生效（可声明校验条件）
- LoopStep   循环：把子步骤反复执行，直到条件满足或次数用完
- GraphStep  流程编排图：token 传播模型的有向图执行（并行分叉/汇合/branch 投票）
- PopupZoneStep 清弹窗行（循环体）：一组建模板→动作模块，每拍扫描，命中即执行并重置静默
  计时，连续 silent_secs 无命中才算本区完成（total_timeout 兜底防锁死）
"""

import math
import os
import random
import time

import cv2
from collections import defaultdict
from datetime import datetime

from bot.frame_cache import cached
from core import capture, ldplayer, vision

FAIL_CAPTURE_DIR = "logs/fail_capture"


# ── 工具函数：截图留证 ────────────────────────────────────────────────

def save_fail_capture(ctx, task_name: str, step_name: str) -> str:
    """
    失败截图留证：全屏存到 logs/fail_capture/inst{实例号}/，
    文件名带任务/步骤/时间，复盘时一眼看出哪一步在什么画面失败了。
    按实例分目录：多开并发时失败归因清晰、同名不撞（Airtest 每设备独立 log 目录思想）。
    返回文件路径。
    """
    # 实例号目录隔离：避免多开时多个实例的失败截图混在一起无法区分归属
    inst_dir = f"{FAIL_CAPTURE_DIR}/inst{ctx.ld_index}"
    os.makedirs(inst_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_task = "".join(c for c in task_name if c.isalnum() or c in "_-") or "task"
    safe_step = "".join(c for c in step_name if c.isalnum() or c in "_-") or "step"
    path = f"{inst_dir}/{safe_task}_{safe_step}_{ts}.png"
    if ctx.frame is not None:
        img = ctx.frame
        # 画最近一次命中框+落点（可排查性审计 ③）：绿框=它看到图在哪，
        # 红圈=它点到哪，"图在左我点右"这类偏移一眼看穿。用完即清，
        # 防陈旧框混进下一次不相干的失败截图。
        rect = getattr(ctx, "last_hit_rect", None)
        if rect is not None:
            ctx.last_hit_rect = None
            try:
                x, y, w, h, px, py = rect
                img = img.copy()
                cv2.rectangle(img, (x, y), (x + w, y + h), (0, 200, 0), 2)
                cv2.circle(img, (px, py), 6, (0, 0, 255), 2)
            except Exception:
                img = ctx.frame      # 画框失败不拦留证本体
        capture.imwrite_cn(path, img)
    return path


# ── 步骤基类与五种步骤 ────────────────────────────────────────────────

# 结果码：整图主动中止（「结束流程」节点命中且范围=整条流程）。区别于
# failed/finished：不上失败恢复链、不传播任何结论，GraphStep 收到直接整图终止。
ABORT = "abort"
# 结果码：结束当前流程（「结束流程」节点范围=当前流程）。引擎全程原样上抛，
# 只有边界节点就地吸收：子流程收到=算跑完走 Y 口、循环收到=跳出循环、
# 哨兵行收到=该行干完一轮；一路没人接（顶层主流程）则等同 ABORT 收尾任务。
FLOW_END = "flow_end"
# 结果码：跳过本轮剩余（「结束流程」节点范围=跳过本轮，等价循环的 continue）。
# 语义 = 就地结束"当前这一轮"的剩余步骤、立刻开始下一轮。只有循环（LoopStep）
# 才真正接住它：丢掉内层 FSM 剩余步骤、轮数照常 +1、重开下一轮。
# 边界：非循环环境（顶层主流程 / 子流程体直接放 / 哨兵行内）没人接 → 一路
# 原样上抛，顶层收口视为"任务正常结束"（等同 local 收尾），绝不死循环、绝不崩。
# 理由：continue 只在循环里有意义；放在外面当无操作跳过会掩盖配置错误，
# 冒泡成"整条收尾"是最不踩坑的定义（用户意图本就是"本轮别往下做了"）。
LOOP_CONTINUE = "loop_continue"

# 哨兵类节点（GraphStep 专用）：watch=认图触发，timer=看钟触发。两者运行契约
# 完全一致（开局即在岗、永不收口、推进走拍尾通道、子链在跑=takeover），
# 引擎里所有哨兵判定统一走这个元组，别再散落 "== watch" 字面量。
SENTINEL_KINDS = ("watch", "timer")


class FlowTimeoutExit(Exception):
    """超时行为=「直接退出程序」：等图超时（重试耗尽/门超时）由 flow.py 的致命
    回调抛出，穿透 FSM 直达 worker —— 不判失败，收到即打 ERROR 日志并退出
    程序（worker 或测试线程各自收尾）。抛之前 fatal_cb 已补存失败截图留证
    （2026-09-24 成品审计：超时退出正是"没点上"第一现场，旧版零证据）。"""


class AccountBannedExit(Exception):
    """watch「封号停用」行命中封号弹窗：穿透 FSM 直达 worker —— 与
    FlowTimeoutExit 同一条"异常收工"通道。worker 收到即写库标记该账号封号
    （表格红显、全部挂机/右键挂机跳过，右键可解禁）并停线程。
    异常消息 = 命中的弹窗模板名（日志与标记留痕用）。"""




def resolve_step_timeout(ctx, step, ok_on_timeout, log_cb, fatal_cb=None,
                         fail_result="failed") -> str:
    """六种同构"超时收口"的统一处理（等图/动作门/点全部门/遍历门/子流程门/识字）：
    1. log_cb 给了就先播一行（收口瞬间只此一次，绝不每拍打）——跳过续跑和
       判失败两种出口都可能有声，名单口径由编译层接线时定（只给"跳过"接的
       站点，判败时传的就是 None）；
    2. ok_on_timeout → "ok"（按配置跳过本步往下走）；
    3. fatal_cb → 调用（契约：回调内抛 FlowTimeoutExit，程序退出，不返回）；
    4. 其余 → fail_result（默认 "failed"；子流程门用 "skipped" 走败口）。"""
    if log_cb is not None:
        log_cb(ctx, step)
    if ok_on_timeout:
        return "ok"
    if fatal_cb is not None:
        fatal_cb(ctx, step)
    return fail_result


def restart_chain(steps, ctx, task_name: str = "task", quiet: bool = True) -> "FSM":
    """五处"复位+开内层 FSM"（LoopStep/SubFlow/MultiScan/Watch/Timer）的统一纪律：
    编译产物跨轮复用，上一轮步骤内部状态（ActionStep._acted / WaitStep._confirm
    等）不清 = 第二轮直接"空跑收口"不再落手（实测：无限循环只点第一下）。
    每轮开跑前逐步骤复位，再开全新 FSM。task_name 仅供失败截图归名，
    内层 quiet=True（静默：归因/日志由外层边界统一负责）。"""
    for s in steps:
        if isinstance(s, Step):
            s.reset()
    return FSM(steps, ctx, task_name=task_name, quiet=quiet)


class SwipeFinder:
    """"识别窗口到期 → 滑一页 → 停稳 → 重开窗口再认"的滑动找机器
    （GraphStep 判断节点翻页 与 ServerPickStep 识字取物 共用一份状态机；
    两处日志文案不同，由调用方在动作前后自打）。
    swipe 是落手动：一拍一手闸门由调用方按拍注入 can_act（budget 列表内容
    每拍重建，不许构造期捕获）。"""

    def __init__(self, direction: str, px: int, ms: int, settle: float,
                 roi=None, max_swipes=0):
        self.dir, self.px, self.ms = direction, int(px), int(ms)
        self.settle = float(settle)
        self.roi = roi
        self.max_count = max_swipes          # 日志用原始配置值（文案要报它）
        self.left = max(0, int(max_swipes))  # 本链剩余滑动次数
        self.settle_until = None              # 停稳截止时刻；None = 不在停稳

    def rearm(self):
        """新一轮起跑：滑动余量按配置灌满、停稳标记清空。"""
        self.left = max(0, int(self.max_count))
        self.settle_until = None

    def wait_settle(self, now: float) -> str:
        """拍首停稳闸："waiting"=停稳未到点（调用方本拍直接返回）；
        "due"=刚到点（已清标记，调用方重开识别窗口计时）；"idle"=不在停稳。"""
        if self.settle_until is None:
            return "idle"
        if now < self.settle_until:
            return "waiting"
        self.settle_until = None
        return "due"

    def try_swipe(self, now: float, ctx, can_act=None) -> str:
        """识别窗口到期的滑动动作："swiped"=已滑一页（left 先减后落手、
        停稳钟已起）；"blocked"=闸门被占（本拍别处落了手，下拍再试）；
        "none"=没剩余额度。"""
        if self.left <= 0:
            return "none"
        if can_act is not None and not can_act():
            return "blocked"
        self.left -= 1
        x1, y1, x2, y2, ms = swipe_pts(self.dir, self.px, self.ms, self.roi)
        swipe(ctx, x1, y1, x2, y2, ms)
        self.settle_until = now + self.settle
        return "swiped"


class Step:

    def __init__(self, name: str, timeout: float = 30, retries: int = 2):
        self.name = name
        self.timeout = timeout
        self.retries = retries
        self._start = None          # 本步开始时间
        self._retries_left = retries

    def tick(self, ctx):
        raise NotImplementedError

    def reset(self):
        """恢复流程用：把步骤打回初始状态再来一次"""
        self._start = None
        self._retries_left = self.retries

    def _on_step_start(self):
        self._start = time.monotonic()

    def _timed_out(self) -> bool:
        if self.timeout <= 0:
            return False          # timeout=0 = 不限时（一直等，见 WaitStep 文档）
        return self._start is not None and time.monotonic() - self._start > self.timeout


def _wait_timeout_log(ctx, step):
    """WaitStep 无编译层回调时（flow 之外的老用法）的内置超时 WARN（文案逐字保留）。"""
    if getattr(ctx, "log", None) is not None and step.template is not None:
        ctx.log(f"[{step.name}] 等待超时（{step.timeout}秒），按配置继续往下走", "WARN")


class WaitStep(Step):
    """
    等待某模板出现（exists=True）或消失（exists=False）。
    template=(分类, 名字)；template=None 时是纯固定等待（fixed_wait 秒）。
    出现/消失要"稳住" confirm_secs 秒才算数，防止画面闪烁误判。
    timeout=0 表示不限时：图不出现就一直等（给"必然会到"的锚点用，
    比如登录后的主城画面；卡住的只是这一步）。
    """

    def __init__(self, name, template=None, timeout=30, retries=1, exists=True,
                 fixed_wait: float | None = None, ok_on_timeout: bool = False,
                 on_timeout_log=None, confirm_secs: float = 0.6,
                 on_timeout_fatal=None, fixed_wait_max: float | None = None,
                 roi=None, **kwargs):
        super().__init__(name, timeout, retries, **kwargs)
        self.template = template
        # roi：搜索区域覆盖（None = 跟模板账本默认区域走），多选一时整列表共用
        self.roi = roi
        self.exists = exists
        self.fixed_wait = fixed_wait
        # fixed_wait_max：纯固定等待的随机上限 —— 实际等待时长在
        # [fixed_wait, fixed_wait_max] 均匀随机（每次进入本步掷一次，reset
        # 重掷）。不配 = 永远等固定秒数。反"秒表节奏"用，人手点按钮不会次次
        # 卡同一个整秒。
        self.fixed_wait_max = fixed_wait_max
        self._wait_target = None      # 本轮实际等待时长（掷出后固定）
        # 稳定确认时长：命中后连续看到这么久才算数（0=命中即算）
        self.confirm_secs = confirm_secs
        # ok_on_timeout：超时视为成功（等模板超时兜底）。默认 False 保持原语义，
        # flow 编译出的等待节点零影响。
        self.ok_on_timeout = ok_on_timeout
        # on_timeout_log：可选回调 (ctx, step) —— ok_on_timeout 触发时打 WARN 日志。
        # WaitStep 本身没有 ctx.log 通道，日志时机由调用方（flow 编译层）注入。
        self.on_timeout_log = on_timeout_log
        # on_timeout_fatal：可选回调 (ctx, step) —— 超时且即将判 failed 前先调。
        # flow 的 wait_template on_timeout=exit 用：打 ERROR 日志并抛
        # FlowTimeoutExit 退出程序（不截图留证）。契约 = 必抛异常。
        self.on_timeout_fatal = on_timeout_fatal
        self._confirm = None  # 确认期截止时间；None = 未进入确认期

    def reset(self):
        super().reset()
        self._confirm = None
        self._wait_target = None     # 恢复流程重进本步时重掷随机等待时长
        self._hb_next = None         # 无限等待心跳重新计时

    def _fixed_secs(self) -> float:
        """本轮实际等待秒数：配了上限则在 [fixed_wait, 上限] 掷一次并记牢，
        同一轮内读数不变（否则每拍重掷 = 永远等不满）。"""
        if self.fixed_wait_max is None or self.fixed_wait_max <= self.fixed_wait:
            return self.fixed_wait
        if self._wait_target is None:
            self._wait_target = random.uniform(self.fixed_wait, self.fixed_wait_max)
        return self._wait_target

    def _check(self, ctx) -> bool:
        if self.template is None:
            return True
        # 支持"多选一"等待：template 传 [(分类,名字), ...] 列表 = 任意一个出现即满足
        templates = self.template if isinstance(self.template, list) else [self.template]
        for category, tpl_name in templates:
            entry = _find_template(category, tpl_name)
            if entry is None or ctx.frame is None:
                continue
            tpl = _load_tpl(entry)
            if tpl is None:
                continue   # 缺图按未命中（同 _tpl_hit：None 喂匹配会炸 .shape）
            hit = vision.match_template(ctx.frame, tpl, entry["threshold"],
                                        roi=self.roi if self.roi is not None
                                        else entry.get("roi"),
                                        rgb_threshold=entry.get("rgb_threshold"))
            if (hit is not None) == self.exists:
                return True
        # exists=True 走到这 = 一个都没命中；exists=False 走到这 = 全部消失
        return not self.exists

    def tick(self, ctx):
        if self._start is None:
            self._on_step_start()
        if self.fixed_wait is not None:
            # 纯固定等待：到点即成功（配了上限 = 随机区间，掷一次记牢）
            return "ok" if time.monotonic() - self._start >= self._fixed_secs() else "running"
        if self._check(ctx):
            if self._confirm is None:
                self._confirm = time.monotonic() + self.confirm_secs  # 开始确认期
            if time.monotonic() >= self._confirm:
                return "ok"
            return "running"
        self._confirm = None  # 状态反转，之前的确认作废
        # timeout=0 无限等待的心跳（稳定性审计 ③）：静默空转是 7×24 最难
        # 发现的故障形态——每 60 秒报一条"还在等"，卡住与正常等待从此可分。
        if self.timeout <= 0 and ctx.log is not None and self.template is not None:
            now = time.monotonic()
            if getattr(self, "_hb_next", None) is None:
                self._hb_next = now + 60
            elif now >= self._hb_next:
                ctx.log(f"[{self.name}] 仍在等图，已等 {int(now - self._start)} 秒"
                        f"（超时=0 表示一直等）", "INFO")
                self._hb_next = now + 60
        if self._timed_out():
            # 超时收口统一走 resolve_step_timeout。内置 WARN 文案只在"跳过续跑"
            # 出口打（与逐字老行为一致：判败/退出路径不播）。
            _log = None
            if self.ok_on_timeout:
                _log = self.on_timeout_log
                if _log is None and self.template is not None:
                    _log = _wait_timeout_log
            return resolve_step_timeout(ctx, self, self.ok_on_timeout, _log,
                                        getattr(self, "on_timeout_fatal", None))
        return "running"


class ActionStep(Step):
    """
    动作：（可选）先过前置门，再执行 action(ctx)，然后等 verify(ctx) 变 True 才算成功。
    verify=None = 不校验（点了就算成功）。
    校验超时会重新执行动作（重新点一次），直到重试耗尽。

    gate：前置门函数 (ctx)->bool，门未过时不落手、每拍复查（时间戳计时，
    绝不在 tick 里阻塞等待）。流程编排的 click_template 用它实现
    "等模板图出现再点击"——没有这道门时，tap_template 找不到图会静默空放，
    而 ActionStep 照样返回 ok，流程带着没点的按钮继续往下走。
    gate_timeout：等门超时秒（缺省用 timeout）。
    ok_on_gate_timeout：True = 门超时视为本步完成（跳过动作）；
        False = 返回 "failed" 交给 FSM（截图留证后判失败）。
    on_gate_timeout_log：跳过时的 WARN 日志回调 (ctx, step)，
        与 WaitStep.on_timeout_log 同惯例（引擎没有 ctx.log 通道，由编译层注入）。
    """

    def __init__(self, name, action, verify=None, timeout=8, retries=2,
                 gate=None, gate_timeout=None,
                 ok_on_gate_timeout=False, on_gate_timeout_log=None,
                 on_gate_timeout_fatal=None):
        super().__init__(name, timeout, retries)
        self.action = action          # 函数(ctx) -> None
        self.verify = verify          # 函数(ctx) -> bool 或 None
        self.gate = gate              # 函数(ctx) -> bool 或 None（None=无门，行为同旧）
        self.gate_timeout = timeout if gate_timeout is None else gate_timeout
        self.ok_on_gate_timeout = ok_on_gate_timeout
        self.on_gate_timeout_log = on_gate_timeout_log
        # on_gate_timeout_fatal：门等图超时、且门超时不跳过时（on_timeout=exit）
        # 先调：打 ERROR + 抛 FlowTimeoutExit 退出程序（不截图留证）。
        self.on_gate_timeout_fatal = on_gate_timeout_fatal
        self._acted = False
        self._verify_start = None
        self._gate_start = None       # 本步开始等门的时刻；None=还没开始
        self._gate_passed = False     # 门已过（过了就不再重看，动作重发时不重复等）

    def reset(self):
        super().reset()
        self._acted = False
        self._verify_start = None
        self._gate_start = None
        self._gate_passed = False

    def tick(self, ctx):
        # ── 前置门阶段：门未过不落手。等门用时间戳计时，tick 内零阻塞 ──
        if self.gate is not None and not self._gate_passed:
            if self._gate_start is None:
                self._gate_start = time.monotonic()
            if self.gate(ctx):
                self._gate_passed = True
                # 门过：无校验的动作同拍落手即完成（不往下落进 !acted 分支的话，
                # verify=None 路径永远等不到第二次 tick 判 ok——这里直通返回）。
                # 有校验的动作落到下面正常开始"落手 + 等生效"。
                if self.verify is None:
                    self.action(ctx)
                    self._acted = True
                    return "ok"
            else:
                # gate_timeout=0 同 WaitStep.timeout：不限时，图不出现就一直等
                if self.gate_timeout > 0 and time.monotonic() - self._gate_start > self.gate_timeout:
                    # 无论超时后是跳过还是判失败，都先播一行 WARN（on_gate_timeout_log
                    # 接线即两出口都要有声）：不然 failed 只见恢复链动作、
                    # 看不出是"图没出现"造成的
                    return resolve_step_timeout(ctx, self, self.ok_on_gate_timeout,
                                                self.on_gate_timeout_log,
                                                getattr(self, "on_gate_timeout_fatal", None))
                return "running"
        if not self._acted:
            if self._start is None:
                self._on_step_start()
            self.action(ctx)
            self._acted = True
            if self.verify is None:
                return "ok"  # 无需校验
            self._verify_start = time.monotonic()
            return "running"
        if self.verify(ctx):
            return "ok"
        if time.monotonic() - self._verify_start > self.timeout:
            self._acted = False       # 校验超时：重置动作，重新点
            self._verify_start = None
            self._start = None
            if self._retries_left <= 0:
                return "failed"
            self._retries_left -= 1
        return "running"


class LogStep(Step):
    """写一条日志（任务进度/失败标记用），执行一次即完成"""

    def __init__(self, name, message, level="INFO"):
        super().__init__(name, timeout=1, retries=0)
        self.message = message
        self.level = level
        self._done = False

    def reset(self):
        super().reset()
        self._done = False

    def tick(self, ctx):
        if not self._done:
            self._done = True
            if ctx.log is not None:
                ctx.log(self.message, self.level)
            else:
                print(f"[LogStep] {self.message}")
        return "ok"


class StopFlowStep(Step):
    """「结束流程」节点：一执行即收口，按 scope 分三种范围——
      "local"（默认）→ FLOW_END：只结束所在层（子流程=算跑完走 Y 口、
        循环=跳出、哨兵行=干完一轮）；放主流程顶层时等同结束整条流程。
      "global" → ABORT：穿透所有边界，整条任务立即终止。
      "continue"（跳过本轮，2026-10-11 F4）→ LOOP_CONTINUE：仅循环体里有意义，
        立即结束"本轮剩余步骤"、开始下一轮（循环的 continue）。非循环环境没人
        接 → 冒泡，顶层按"任务正常收尾"处理（绝不死循环、绝不崩，见 LOOP_CONTINUE 注释）。
    三者都不传播结论、不点亮下游、不上失败恢复链，worker 视为任务成功提前收尾。"""

    def __init__(self, name="结束流程", scope="local"):
        super().__init__(name, timeout=1, retries=0)
        self._scope = scope if scope in ("local", "global", "continue") else "local"
        self._done = False

    def reset(self):
        # 同 LogStep/SignalStep：一次性步骤复位后要能再喊一次（循环体里
        # 「跳过本轮」每轮重进本步，_done 不清则第二轮起不再打日志）。
        # 对 local/global 无可观测影响：两者当场结束所在层/整条任务，不会重进。
        super().reset()
        self._done = False

    def tick(self, ctx):
        if not self._done:
            self._done = True
            _tail = {"global": "（整条流程）", "continue": "（跳过本轮）"}.get(
                self._scope, "（当前流程）")
            if getattr(ctx, "log", None) is not None:
                ctx.log(f"「结束流程」节点命中{_tail}，主动结束", "INFO")
            else:
                print(f"[StopFlowStep] 结束流程节点命中{_tail}")
        if self._scope == "global":
            return ABORT
        if self._scope == "continue":
            return LOOP_CONTINUE
        return FLOW_END


# ── 停止信号信箱（2026-10-06「发信号」节点）──────────────────────────────
# 子流程/体内节点触发某判定后发一条信号，外层容器（LoopStep / MultiScanStep）
# 在自己属性面板登记「收到即结束的信号」名单，查闸命中即提前收工（ok 收口，
# 走 out 口正常续跑，不是失败）。运输挂在 ctx.extra（一次挂机运行的跨拍状态，
# BotContext 天然全链共用 —— 子流程与父层同一 ctx，不用新建管道）。
# 消费口径 = 一次销账：谁先登记收到谁掐、这条信号随之销账，外层不连环陪葬
# （想让内外都停 = 两级各登记各的名单；同名单列两级也只掐最先查的那级）。
# 没容器认领的信号留在信箱（无害，随 ctx 一起没）；信箱封顶 32 条防无限循环
# 每圈白发把 extra 撑大。

def signal_post(ctx, name: str):
    box = ctx.extra.setdefault("fs:pbox", [])
    box.append(name)
    while len(box) > 32:
        box.pop(0)


def signal_take(ctx, names) -> str | None:
    """查闸：信箱里第一条在 names 名单里的信号，摘走销账；没有返回 None。"""
    box = ctx.extra.get("fs:pbox")
    if not box or not names:
        return None
    for i, nm in enumerate(box):
        if nm in names:
            return box.pop(i)
    return None


class SignalStep(Step):
    """「发信号」节点：往运行信箱投一条命名信号，自身直通下一节点。
    只发不收 —— 掐谁由外层容器的名单决定；没配名单 = 白发（无害）。"""

    def __init__(self, name, signal=""):
        super().__init__(name, timeout=1, retries=0)
        self.signal = str(signal or "")
        self._done = False

    def reset(self):
        super().reset()
        self._done = False

    def tick(self, ctx):
        if not self._done:
            self._done = True
            if self.signal:
                signal_post(ctx, self.signal)
            _log = getattr(ctx, "log", None)
            if _log is not None:
                if self.signal:
                    _log(f"发信号「{self.signal}」已投递"
                         "（外层循环/遍历登记了同名『收到即结束』才会掐）", "INFO")
                else:
                    _log("发信号节点没填信号名，白发直通", "WARN")
        return "ok"


class LoopStep(Step):
    """
    循环：反复执行子步骤链。
    until：条件函数(ctx)->bool，满足则循环结束（返回 ok）
    times：固定次数（None = 无限循环，挂机任务就是无限循环）
    stop_on：「收到即结束的信号」名单（None/空 = 不设闸，老行为逐字不变）。
      每拍开头查信箱：命中 → 销账 + 就地出循环（ok 收口，主线续跑）。
      掐在拍与拍之间：已经点下去的手不回头，正在等的下一步不再等。
    （旧「命中→信号」线的 pause/resume 已下线：并行监视一刀切暂停后，
    子链在跑时 GraphStep 冻结闸连 tick 都不发，循环天然原地停，无需自挂起。）
    """

    def __init__(self, name, steps, until=None, times=None, stop_on=None):
        super().__init__(name)
        self.steps = steps
        self.until = until
        self.times = times
        self.stop_on = [str(s).strip() for s in (stop_on or [])
                        if str(s).strip()] or None
        self._inner = None
        self._count = 0

    def reset(self):
        super().reset()
        self._inner = None
        self._count = 0

    def tick(self, ctx):
        if self.stop_on:
            hit = signal_take(ctx, self.stop_on)
            if hit is not None:
                _log = getattr(ctx, "log", None)
                if _log is not None:
                    _log(f"循环「{self.name}」收到停止信号「{hit}」："
                         "提前收工（正常出口续跑）", "INFO")
                return "ok"
        if self._inner is None:
            # 每轮开跑前复位+重建（纪律统一见 restart_chain）
            self._inner = restart_chain(self.steps, ctx)
        if self.times is not None and self._count >= self.times:
            return "ok"
        if self.until is not None and self.until(ctx):
            return "ok"
        res = self._inner.tick()
        if res == "done":
            self._count += 1
            self._inner = None  # 重新开始一轮
        elif res == LOOP_CONTINUE:
            # 体内「结束流程」(跳过本轮)：就地吸收 = 本轮剩余步骤丢弃、轮数照常 +1、
            # 立刻重开下一轮（等价循环 continue）。不累加退出判据（count 就是正常圈数）。
            self._count += 1
            self._inner = None
        elif res == FLOW_END:
            return "ok"         # 体内「结束流程」(当前流程)：就地吸收 = 跳出循环，主线续跑
        elif res == ABORT:
            return ABORT
        return "running"


class GraphStep(Step):
    """
    流程编排图（自由连线，token 传播模型）。每 tick 对整图推进一步。

    节点状态：idle → running → done | skipped；每条入边记 未达(0) | 送达(1) | 跳过(2)。
    - 就绪 = 所有入边都有结论（送达或跳过）；零入线节点（入口）开局即就绪。
    - 就绪后：全部入边都是「跳过」→ 本节点直接 skipped，向所有出线传「跳过」
      （未选中分支的整条链就这样静默熄灭）；好 node 编译出 Step 执行：
      ok→done 并向所有出线传「送达」；running→下拍继续；failed→整图 failed。
    - branch 节点：投票窗口语义（窗口内任一拍条件成立选 true，超时选 false），
      选定后一条出线传「送达」、另一条传「跳过」。缺图 branch 由编译层处理，
      引擎按无条件 branch 对待。
    - 多条出线 = 各传一份 token，下游同时点亮（并行）；多条入线 = 汇合点，
      所有入边有结论才就绪，混合（有送达有跳过）照常执行。
    - 成环 = 循环执行（用户拍板：引擎不拒环，编辑器提醒即可）：DFS 标记的
      「回边」（指回 DFS 栈上节点的线，如 A→B→C→A 的 C→A）首拍不挡就绪，
      否则纯环没有入口、全图死等（实测踩过的坑）；收口（done/skipped）节点
      收到「送达」token → 保留其余入边结论、重新点亮再跑一轮 = 回路自动成循环。
      无环图回边集为空、done 节点不会二次收到送达，以上全部判据零成本原样。
      循环出口靠「结束流程」（FLOW_END/ABORT）、失败口、看门狗；哨兵照常永不收口。
    - 动作节点可接「fail」口（编辑器端口 + 编译层 fatal 让位，见 flow.py）：
      步骤 failed 且该节点 fail 口有连线 → 本节点按 done 收口，fail 口传送达、
      其余口传跳过——"失败另走一条线"在画布上编排，不再整图判死；
      没接线则维持原语义（整图 failed 交 FSM 恢复链）。
    - 整图完成 = 除哨兵（SENTINEL_KINDS：watch/timer）外所有节点
      done/skipped → 返回 "ok"。哨兵永不收口（常驻并行监视，主线全完则
      随图一起收工），推进走拍尾哨兵通道：每拍必扫（扫描不落手）；子链
      一跑起来即一刀切暂停（用户拍板 2026-09-25）：主线整条冻结、拍尾
      也不判完成，收口后自动从暂停处续跑 —— 不需要连任何线。

    两条红线：
    - 每拍最多落手一次：一轮 tick 里第一个需要执行动作的节点获得执行权，
      其余动作节点顺延下一拍（单模拟器同一时刻只能落一次手）；纯评估
      （等待/判断/日志）不受限。动作判定由编译层 is_action 提供。
      例外：哨兵子链在跑（_watch_takeover）时主线节点（含纯评估）全让路，
      落手权与推进权都归子链。
    - tick 内绝不阻塞：全程无循环等待，等待类步骤靠自身超时计时。

    失败处理：reset() 把全图状态打回 idle，交给 FSM（截图留证后判失败）。
    """

    def __init__(self, name, nodes, edges, branch_conds=None, build_step=None,
                 is_action=None, stuck_secs: float = 0):
        super().__init__(name)
        if build_step is None or is_action is None:
            raise ValueError("GraphStep 需要 build_step 与 is_action 两个回调")
        # 看门狗（图级，学 LCA 思路自实现）：stuck_secs 内没有任何节点完成
        # （done/skipped 计数不涨）= 整条流程卡死空转 → 判 failed 交 FSM 截图留证。
        # ⚠ 契约：stuck_secs 必须大于图中最慢单个步骤自身的超时，否则慢步骤
        # 会被误杀（如 wait_template timeout=60 时看门狗要 ≥75）。0 = 关闭。
        self._stuck_secs = float(stuck_secs or 0)
        self._last_progress = None   # 最近一次"有节点收口"的时刻（monotonic）
        self._done_count = -1        # done/skipped 节点数（变化 = 有进展）
        self._rearm_total = 0        # 累计回炉次数（成环循环 = 有进展，见 _propagate）
        self._rearm_seen = -1        # 看门狗上次核对过的回炉数
        # 当前动作粒度上报（主界面「当前动作」列）：整图步骤原本恒名「流程图」，
        # 表格/日志看不出走到哪个节点。本拍推进主线节点时把 name 改成
        # 「流程图·节点备注名」，worker 逐拍对比步骤名，变化即上报 —— 上报
        # 链路零改动白嫖节点级进度。哨兵（watch/timer）不刷：它并行在办，
        # 刷过去反而盖掉主线正停在哪一步；命中动静由它自己的日志行负责。
        self._base_name = name       # 整图原名（改名永远在它后面拼 ·节点名）
        self._active_lbl = ""        # 上次刷进去的当前节点名（同拍重复推进不重拼）
        # nodes: {id: 节点 dict}（dict 保插入序）
        # edges: [(from_id, port, to_id), ...]，port ∈ {"out","true","false","fail"}
        # branch_conds: {id: (cond_fn|None, timeout[, 走N诊断回调[, 滑动翻页配置]])}，
        #    cond=None 表示缺图降级的无条件分支（永远选 false —— 编译层契约：
        #    "没这图 = 条件不成立"）；第三元可选 = 收口瞬间调一次的日志回调
        #    cb(ctx, shot)，shot = 本次收口走的出口（走N诊断 / 走Y打一行结论）
        self._nodes = nodes
        self._edges = edges
        self._branch_conds = branch_conds or {}
        self._build_step = build_step    # (node) -> Step | None，点亮时现场编译
        self._is_action = is_action      # (node) -> bool：动作类节点（落手闸门）
        self._beat = 0                   # 每拍递增：保证每节点每拍最多推进一次
        self._watch_takeover = False     # 哨兵子链在跑 = 主线整条冻结（一刀切，拍首重算）
        self._sent_last = None           # 拍尾通道上次推进的链主（轮转起点，防后排饿死）
        self.failed_node = ""          # 判死整图时卡在哪节点（FSM 写库归因用）
        self._build_tables()

    # ── 构造：一次建好节点表 + 邻接表（之后每拍只更新状态）──

    def _build_tables(self):
        """节点运行表。concl 与 ins 对齐：每条入边记 0 未达 / 1 送达 / 2 跳过。
        就绪 = 所有入边都有结论；零入线节点（入口）开局即就绪。"""
        self._nd = {}
        for nid, node in self._nodes.items():
            self._nd[nid] = {
                "node": node,
                "kind": node.get("type", ""),
                "step": None,                  # 点亮时现场编译出的引擎步骤
                "ins": [],                     # [(from_id, port), ...]
                "concl": [],                   # 与 ins 对齐的结论码
                "back": [],                    # 与 ins 对齐：True = 回边（成环时不挡首拍）
                "outs": defaultdict(list),     # port -> [to_id, ...]
                "state": "idle",               # idle/running/done/skipped
                "_vote": None,                 # branch：{"start","cond","timeout"}
                "_last_beat": -1,              # 上次推进所在拍号（每拍每节点最多推进一次）
            }
        for frm, port, to in self._edges:
            if frm not in self._nd or to not in self._nd:
                continue  # 悬空边（引用不存在的节点）：忽略
            self._nd[to]["ins"].append((frm, port))
            self._nd[to]["concl"].append(0)
            self._nd[to]["back"].append(False)
            self._nd[frm]["outs"][port].append(to)
        self._mark_back_edges()

    def _mark_back_edges(self):
        """DFS 标回边（指向"当前这条探索路径上已有节点"的线，如 A→B→C→A 的 C→A）。
        回边是环的唯一入口来源：纯环没有任何入度 0 的入口，若回边也按普通入边
        等结论，全图永远等不到第一个 token（实测：5 拍 0 步、整图 idle 死等）。
        故回边槽位在首拍不计入"是否有入边未定"，环靠「收口节点收到送达 → 重新点亮」
        成循环。无环图回边集为空，语义与原来逐位一致。"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in self._nd}

        def succ(nid):
            return {t for ids in self._nd[nid]["outs"].values() for t in ids}

        # 显栈 DFS（原递归版在深链流程上有吃穿 Python 栈的理论风险；
        # 遍历顺序与集合构造逐字相同 → 回边标记结果等价）
        for root in list(self._nd):
            if color[root] != WHITE:
                continue      # 多入口/孤岛图：每个未访问点都起一次，环不会漏标
            color[root] = GRAY
            stack = [(root, iter(succ(root)))]
            while stack:
                nid, it = stack[-1]
                advanced = False
                for to in it:
                    if color[to] == GRAY:
                        # nid → to 指回路径上的祖先 = 回边（该 to 名下所有 (nid, *) 入边一起标）
                        for i, (f, _p) in enumerate(self._nd[to]["ins"]):
                            if f == nid:
                                self._nd[to]["back"][i] = True
                    elif color[to] == WHITE:
                        color[to] = GRAY
                        stack.append((to, iter(succ(to))))
                        advanced = True
                        break   # 下钻；上面 for 暂停的迭代器留在栈里，回溯后接着走
                if not advanced:
                    stack.pop()
                    color[nid] = BLACK

    # ── 每拍推进 ──

    def _chain_busy(self):
        """任一 watch/timer 子链在跑（_pending 非空）= 一刀切暂停的判据。"""
        return any(r["kind"] in SENTINEL_KINDS and r["state"] == "running"
                   and r["step"] is not None
                   and getattr(r["step"], "_pending", None) is not None
                   for r in self._nd.values())

    def tick(self, ctx):
        """对整图推进一步。有序扫描节点表：就绪的节点点亮（含越过确认期后
        跳到 done 的 WaipStep 等），running 的节点每拍每节点最多推进一次；有状态
        变化就继续扫，图节点数上界内必然收敛。动作闸门在本拍内生效：第一个执行
        的动作节点独占落手权，其余顺延下一拍。"""
        self._beat += 1
        # 一刀切暂停（2026-09-25 用户拍板）：任一 watch/timer 子链在跑 →
        # 主线整条冻结 —— 动作和纯评估节点（等待/判断/日志）全停，不连线也
        # 默认「成立就暂停主线、办完自动从暂停处续跑」（loop/subflow 内部
        # 状态从未重置）。原「仅动作让路」语义被此闸覆盖。
        self._watch_takeover = self._chain_busy()
        budget = [not self._watch_takeover]  # 列表引用：动作闸门在推进中可变
        if self._stuck_secs > 0:
            # 有节点收口 = 有进展，刷新看门狗时钟（放拍首：本拍推进出的 done
            # 下拍再查，最坏多等一拍 0.25s，无害且省一遍全表扫描）
            n_done = sum(1 for r in self._nd.values()
                         if r["state"] in ("done", "skipped"))
            if (n_done != self._done_count or self._watch_takeover
                    or self._rearm_total != self._rearm_seen):
                # 哨兵链在跑也算进展：主线冻结期间本来就不会收口，
                # 不算的话开了看门狗的流程会被弹窗子链误杀成"卡死"
                # 回炉也算进展：成环流程稳定循环时 n_done 恒定不变，
                # 不记这笔的话跑满 stuck_secs 必被误杀（环路恰恰最常见于常驻挂机）
                self._done_count = n_done
                self._rearm_seen = self._rearm_total
                self._last_progress = time.monotonic()
            elif (self._last_progress is not None
                  and time.monotonic() - self._last_progress > self._stuck_secs):
                self.failed_node = "看门狗卡死"   # 写库归因：不是节点失败，是超时没进展
                return "failed"      # 卡死：交 FSM 截图留证，日志看得见是哪个任务
        while True:
            changed = False
            for nid, rec in self._nd.items():
                if self._watch_takeover and rec["kind"] not in SENTINEL_KINDS:
                    continue   # 一刀切暂停：子链在跑，主线节点（含纯评估）整拍不碰
                if rec["state"] == "idle":
                    if rec["_last_beat"] == self._beat:
                        continue  # 本拍刚被回炉的节点：下一拍再点亮（防同拍无限圈）
                    # 入边分两种：非回边决定"这一轮进不进场"，回边只管成环后回炉
                    # （环上纯入口节点没有任何非回边入边，等它 = 全图死等）。
                    nb = [c for c, b in zip(rec["concl"], rec["back"]) if not b]
                    if rec["kind"] not in SENTINEL_KINDS and any(c == 0 for c in nb):
                        continue  # 有的非回边入边还挂在等待的节点上，等下一拍再看
                    if rec["kind"] in SENTINEL_KINDS:
                        # 哨兵：开局即点亮（不看入边结论，上面已放行）；后续
                        # 推进全部走拍尾哨兵通道，这里编译好步骤就走
                        rec["state"] = "running"
                        rec["step"] = self._build_step(rec["node"])
                        if rec["step"] is None:
                            rec["state"] = "skipped"  # 空配置：不再监视
                        else:
                            rec["step"].reset()
                        changed = True
                        continue
                    if nb and all(c == 2 for c in nb):
                        # 全部非回边入边都是「跳过」→ 自身跳过，跳过信号向所有出线传。
                        # 必须走 None（全部出口）：branch 的 true/false 两条都得写上
                        # 跳过，漏一条那条子链的汇合点就永远等不到结论（死等）。
                        # ⚠ 用 nb 而非 concl：成环时未送达的回边槽恒 0，算进来的话
                        # 分支没走的循环体就灭不掉（还会被错误点亮执行一遍）。
                        rec["state"] = "skipped"
                        self._propagate(nid, None, False)
                        changed = True
                        continue
                    # 点亮。branch 是纯评估节点，没有可编译的 Step —— 必须在
                    # build_step 之前分流：它返回 None，若走下面「空步骤」分支会
                    # 向不存在的 out 口传播，true/false 下游永远拿不到 token。
                    rec["state"] = "running"
                    if rec["kind"] == "branch":
                        # (cond, timeout, 走N诊断回调, 滑动翻页配置)；兼容手构的两三元组
                        ent = self._branch_conds.get(nid) or (None, 5)
                        cond, timeout = ent[0], ent[1]
                        vote = {"start": time.monotonic(), "cond": cond,
                                "timeout": timeout,
                                "diag": ent[2] if len(ent) > 2 else None,
                                "swipe": None}
                        sw = ent[3] if len(ent) > 3 else None
                        if sw:
                            # 每次点亮独立一份滑动机（重试重经不继承上一轮的剩余次数）
                            vote["swipe"] = SwipeFinder(
                                sw["dir"], sw["px"], sw["ms"], sw["settle"],
                                roi=sw.get("roi"), max_swipes=sw.get("max", 0))
                        rec["_vote"] = vote
                        changed = True
                        continue
                    rec["step"] = self._build_step(rec["node"])
                    if rec["step"] is None:
                        # 缺图降级在编译层已变成 LogStep；None 只可能是该节点
                        # 真没有可执行体（如空节点）→ 视为「没做」，向下游传跳过
                        rec["state"] = "skipped"
                        # 走 None（全出口）而非只 out：节点若接了败口，光写 out
                        # 会漏掉 fail 边 → 那个下游汇合点永远等不到结论（死等）。
                        self._propagate(nid, None, False)
                        changed = True
                        continue
                    rec["step"].reset()
                    # 点亮本身算状态变化：本轮回头立刻推进它（少等一拍）
                    changed = True
                    continue
                if rec["state"] == "running" and rec["_last_beat"] != self._beat:
                    if rec["kind"] in SENTINEL_KINDS:
                        continue   # 哨兵只在拍尾通道推进（主线优先，空拍才给它）
                    if rec["kind"] == "branch":
                        rec["_last_beat"] = self._beat
                        self._set_active(rec["node"])
                        if self._tick_branch(nid, rec, ctx, budget):
                            changed = True
                        continue
                    # 落手闸门：本拍已有动作节点执行过 → 其余动作节点顺延
                    if self._is_action(rec["node"]) and not budget[0]:
                        continue
                    rec["_last_beat"] = self._beat
                    self._set_active(rec["node"], rec["step"])
                    res = rec["step"].tick(ctx)
                    if self._is_action(rec["node"]):
                        budget[0] = False
                    hard = self._apply_step_result(nid, rec, res, ctx)
                    if hard in (ABORT, FLOW_END, LOOP_CONTINUE):
                        return self._abort_flow(hard)
                    if hard == "failed":
                        return "failed"
                    changed = res != "running"
            if not changed:
                break  # 本拍扫到无变化，收敛
        # ── 哨兵通道（拍尾）：主线本拍收敛后，watch 节点各推进一次 ──
        # 每拍必扫（扫描=纯识别不落手，与 branch 条件同量级开销）：一旦命中就
        # 起跑子链，从下一拍起 takeover 生效、主线整条冻结 —— "弹窗来了先办弹窗"
        # 的语义（用户拍板）。主线刚落了手的那拍照扫，起跑本身不落手不会双点。
        # 不落手的空配置步（编译成 WARN）本来就每拍推进，告警当拍就出。
        # ⚠ 多链串行闸（抢位修复）：watch×watch / watch×timer 可能各自起链，
        # 两条子链同拍推进 = 同拍两次落手，违反引擎红线。故本拍已有一条在跑
        # 的链推进过（tick 前 _pending 非空 = 它可能点屏），后到的链整拍排队
        # 等下一拍；纯扫描（tick 前没链）不受限——扫描不落手，最晚下一拍起链
        # 时照样被这道闸排队。轮转公平：扫描起点排到上一拍赢家之后（哨兵
        # 数量个位数的轮转表，开销可忽略），固定表序先到先得会饿死后排链。
        chain_ticked = False
        # 子流程「主流程监视照管」开关（bg_watch=False，用户拍板 2026-10-05）：
        # 主线正跑着标了停哨的子流程节点 → 本图 watch 哨兵停扫（弹窗来了也不管，
        # 一切按子流程自己的逻辑走；子流程内部哨兵归它自己的内层图管，不受影响）。
        # 已起跑的哨兵子链照常推进到办完——半路掐断 = 弹窗办到一半撂挑子更糟；
        # timer 定时节点不殃及（语义是排班触发，不是盯弹窗）。
        hush = any(getattr(r["step"], "suppresses_watch", False)
                   for r in self._nd.values()
                   if r["state"] == "running" and r["kind"] not in SENTINEL_KINDS)
        sents = [nid for nid, r in self._nd.items()
                 if r["kind"] in SENTINEL_KINDS and r["state"] == "running"
                 and r["step"] is not None and r["_last_beat"] != self._beat
                 and not (hush and r["kind"] == "watch"
                          and getattr(r["step"], "_pending", None) is None)]
        if self._sent_last in sents:
            k = sents.index(self._sent_last)
            sents = sents[k + 1:] + sents[:k + 1]
        for nid in sents:
            rec = self._nd[nid]
            step = rec["step"]
            was_pending = getattr(step, "_pending", None) is not None
            if was_pending and chain_ticked:
                continue   # 排队拍：不设 _last_beat，下一拍照常候选
            rec["_last_beat"] = self._beat
            hard = self._apply_step_result(nid, rec, step.tick(ctx), ctx)
            if hard in (ABORT, FLOW_END, LOOP_CONTINUE):
                return self._abort_flow(hard)
            if hard == "failed":
                return "failed"
            if was_pending:
                chain_ticked = True
                self._sent_last = nid
            if getattr(step, "tapped", False):
                budget[0] = False
        # 完成判定：哨兵节点永远 running（常驻监视），排除在外 —— 主线全部
        # 收口即整图完成，线程正常退。若把 watch 计入，含哨兵的流程永不 done，
        # worker 任务链卡死不退（这正是改版前反复权衡的坑）。
        # 一刀切补闸：子链正办着不收口（主线恰好全 done 的当拍起了链 = 等它
        # 办完再判），否则 done 直接终结线程，半截子链陪葬。
        if self._chain_busy():
            return "running"
        return "ok" if all(r["state"] in ("done", "skipped")
                           for r in self._nd.values()
                           if r["kind"] not in SENTINEL_KINDS) else "running"

    def _apply_step_result(self, nid, rec, res, ctx):
        """把一步的结论写进节点状态并向下游传播。返回 None=已处理；返回
        "failed"/ABORT/FLOW_END/LOOP_CONTINUE=调用方须把这拍结论原样向上传
        （整图判死 / 主动中止 / 结束当前流程 / 跳过本轮）。主线主循环与拍尾哨兵
        通道共用这一份 ok/skipped/failed 语义，防两处漂移。"""
        if res in (ABORT, FLOW_END, LOOP_CONTINUE):
            # 主动中止/结束当前流程/跳过本轮（「结束流程」节点命中）：不传播结论、
            # 不点亮下游，整图直接终止并原样上抛（边界节点就地吸收 LOOP_CONTINUE，
            # FLOW_END 同旧；顶层没人接收尾任务）。
            return res
        if res == "ok":
            rec["state"] = "done"
            # 执行成功同样打印（和判断 ✓/✗ 两头对账对称）：普通节点收口打一行；
            # 成环回炉每轮再收口 = 每轮再打一次，与判断节点同纪律。判断/哨兵
            # （watch/timer）在自己收口处有专属日志，不走这条。
            _log = getattr(ctx, "log", None)
            if _log is not None:
                _lb = rec["node"].get("label") or rec.get("kind") or nid
                _log(f"「{_lb}」✓ 执行完成", "INFO")
            # subflow 成功口走 true（端口语义同 branch：一条送达一条跳过），
            # 普通节点仍是单 out 口全送达
            if rec["kind"] == "subflow":
                self._propagate(nid, "true", True)
                self._propagate(nid, "false", False)
            else:
                self._propagate(nid, "out", True)
                # 接了败口的节点成功后必须给 fail 边写「跳过」，否则下游汇合点
                # 永远等不到这条入边的结论 = 整图空拍挂死
                self._propagate(nid, "fail", False)
            return None
        if res == "skipped":
            # 双出口节点的失败口（SubFlowStep：子流程失败/进门前超时）。绝不并入
            # failed —— failed 直接判死整图，"失败另走一条线"就没有意义。非
            # subflow 步骤不返回 skipped，兜底向所有出口传跳过（宁可熄灭下游）。
            if rec["kind"] == "subflow" and not rec["outs"].get("false"):
                # 败口没连线又真失败了：无声熄灭下游、整图照常 done = "没跑完
                # 当跑完了"（点模板静默空放同款坑）。判死整图，交 FSM 截图留证。
                self.failed_node = rec["node"].get("label") or nid
                return "failed"
            rec["state"] = "done"
            if rec["kind"] == "subflow":
                self._propagate(nid, "false", True)
                self._propagate(nid, "true", False)
            else:
                self._propagate(nid, None, False)
            return None
        if res == "failed":
            if rec["outs"].get("fail"):
                # 该节点接了「fail」出口线：以 done 收口，fail 口传送达、其余口
                # 传跳过（成功链下游整条熄灭，汇合点能推进）。编译层对接了 fail
                # 口的节点不再注入 fatal 回调（见 flow.py has_fail）。
                nm = rec["step"].name if rec["step"] is not None else nid
                if ctx.log is not None:
                    ctx.log(f"步骤「{nm}」失败，走失败出口", "WARN")
                rec["state"] = "done"
                self._propagate(nid, "fail", True)
                for p in list(rec["outs"].keys()):
                    if p != "fail":
                        self._propagate(nid, p, False)
                return None
            self.failed_node = rec["node"].get("label") or nid   # 写库归因：死在哪个节点
            return "failed"
        return None

    def _abort_flow(self, code=ABORT):
        """主动中止/结束：reset 后原样上抛结果码（ABORT/FLOW_END/LOOP_CONTINUE），
        由调用方边界吸收（LoopStep 收 LOOP_CONTINUE/FLOW_END、SubFlowStep 收
        FLOW_END）或交 worker 收尾（顶层）。"""
        self.reset()
        return code

    def _set_active(self, node, step=None):
        """把步骤名刷成当前节点名（不带前缀）：主界面「当前动作」列跟到
        节点粒度（worker 逐拍对比步骤名，变了才 emit，上报链路零改动）。
        step.name 就是编译层给节点定的备注名，优先用它；判断类没步骤、拿
        node.label。哨兵通道（拍尾）不调：并行在办不该盖掉主线停在哪一步，
        它的动静由自己的日志行负责。"""
        lbl = (step.name if step is not None else "") or node.get("label") or ""
        if lbl != self._active_lbl:
            self._active_lbl = lbl
            self.name = lbl or self._base_name

    def _tick_branch(self, nid, rec, ctx, budget=None):
        """branch 投票窗口；配了滑动翻页（vote["swipe"]=SwipeFinder）时识别窗口
        到期不落 false，而是滑一页、停稳后重开窗口继续投票，滑次用尽才收口走
        false（与识字取物的滑动找同款，共用 SwipeFinder 状态机）。滑动是落手动，
        受「一拍一手」闸门（budget，按拍经 can_act 注入）约束。
        cond=None（缺图降级）→ 恒选 false（对齐两处编译层
        注释的契约："没这图 = 条件不成立"；曾有 bug 是 None 落进 or 短路选了
        true，缺图反而执行 Y 分支）；窗口内任一
        拍条件成立 → 选 true；超时 → 选 false。选定后一条出线传送达、另一条
        传跳过，本节点 done。返回是否有状态变化。
        收口瞬间调一次 vote["diag"](ctx, shot)（编译层注入的日志回调：走N 自动
        诊断找原因、走Y 打一行结论；None=不打）。shot ∈ {"true","false"} =
        本次收口走的出口，回调按出口自己决定打什么。
        只在收口瞬间打一次，绝不每拍打（投票每 0.25s
        扫一拍，每拍打 = 刷屏）；整图失败重试后重经本节点会再收口一次 = 再打一次。"""
        vote = rec["_vote"]
        if vote is None:
            return False
        now = time.monotonic()
        sw = vote["swipe"]
        if sw is not None:
            st = sw.wait_settle(now)
            if st == "waiting":
                return False        # 刚滑完在停稳：动画中判图容易误判
            if st == "due":
                vote["start"] = now  # 停稳：重开识别窗口（超时重新计）
        shot = None
        if vote["cond"] is None:
            shot = "false"                      # 缺图降级：恒走 N
        elif vote["cond"](ctx):
            shot = "true"
        elif vote["timeout"] is not None and now - vote["start"] > vote["timeout"]:
            if sw is not None:
                act = sw.try_swipe(now, ctx, can_act=(lambda: budget[0])
                                   if budget is not None else None)
                if act == "blocked":
                    return False   # 滑动也是落手：本拍已有动作就顺延下拍（一拍一手）
                if act == "swiped":
                    if budget is not None:
                        budget[0] = False
                    if getattr(ctx, "log", None) is not None:
                        ctx.log(f"{rec['node'].get('label') or '判断'}: "
                                f"{vote['timeout']:g}秒没认出，滑动翻页找"
                                f"（还可滑 {sw.left} 次）", "INFO")
                    return False
                # 额度用尽：收口走 N（先留一行 WARN，摘掉滑动机）
                if getattr(ctx, "log", None) is not None:
                    ctx.log(f"{rec['node'].get('label') or '判断'}: "
                            f"滑了 {sw.max_count} 次仍没认出", "WARN")
                vote["swipe"] = None
            shot = "false"
        if shot is None:
            return False
        if vote.get("diag") is not None:
            vote["diag"](ctx, shot)
        rec["_vote"] = None
        rec["state"] = "done"
        self._propagate(nid, shot, True)
        self._propagate(nid, "false" if shot == "true" else "true", False)
        changed = True   # 触发 while 循环下一轮扫描：下游收到结论后会点亮
        return changed

    def _propagate(self, nid, port, delivered: bool):
        """从 nid 的指定出口向下游写结论：delivered=True 写「送达」=1，
        False 写「跳过」=2。port=None = 向该节点的【所有】出口写（全跳过传播用：
        一个节点因全部入边都跳过而自身跳过时，true/false 两条出线都得写上，
        漏一条下游汇合点就永远等不到结论）。找不到入边记录就跳过。
        成环回炉：向已收口（done/skipped）节点写「送达」= 回路绕回这一步 →
        打回 idle 重新点亮再跑一轮（结论槽保留：本条已写成送达，扫描当拍
        就会重新点亮）。无环图（DAG）的收口节点不会再收到送达，此分支天然
        不触发；「跳过」token 不回炉（没走的路不复活）。"""
        ports = [port] if port is not None else list(self._nd[nid]["outs"].keys())
        for p in ports:
            for to in self._nd[nid]["outs"].get(p, []):
                try:
                    idx = self._nd[to]["ins"].index((nid, p))
                except ValueError:
                    continue
                self._nd[to]["concl"][idx] = 1 if delivered else 2
                if delivered and self._nd[to]["state"] in ("done", "skipped"):
                    self._nd[to]["state"] = "idle"
                    self._nd[to]["step"] = None
                    # 本拍不再重新点亮：纯评估环没有落手闸，不设这道闸的话
                    # a→b→c→a 一圈收口又点亮 a，while-changed 扫描一拍内无限收敛不掉
                    self._nd[to]["_last_beat"] = self._beat
                    self._rearm_total += 1   # 回炉记为看门狗进展（见 tick 拍首）

    def reset(self):
        """失败恢复：全图打回 idle、入边结论清零、投票与编译步骤清空，可重跑。
        看门狗同步复位 —— 重跑等于重新给时间，否则恢复链刚起步就被上轮旧时钟判卡死。"""
        super().reset()
        for rec in self._nd.values():
            rec["state"] = "idle"
            rec["concl"] = [0] * len(rec["concl"])
            rec["step"] = None
            rec["_vote"] = None
        self.failed_node = ""          # 重跑归零：别把上轮的死点带给本轮写库
        self._last_progress = None
        self._done_count = -1
        self._rearm_total = 0
        self._rearm_seen = -1

    def node_state(self, nid) -> str:
        """调试/冒烟用：查某节点当前状态（idle/running/done/skipped）。"""
        return self._nd[nid]["state"]


class PopupZoneStep(Step):
    """
    清弹窗行（循环体 steps 里 popup_zone 行的编译产物；独立「弹窗区」节点
    2026-09-25 已下线，见 flow._drop_popup_zones）：一组「识别这图→点掉」
    模块自治循环。每拍按模块清单顺序扫描，第一个命中就点它并重置静默计时；
    连续 silent_secs 秒无任何命中 → "ok"（用户语义"弹窗多少秒识别不到才算
    完成"）。total_timeout 全程兜底：弹窗关了又冒（互相拉起的死循环）到点
    判 "failed"，交 FSM 失败处理截图留证收摊，绝不锁图。

    红线合规：扫描用 ctx.frame（worker 每拍推下来的帧），tick 内不阻塞；
    命中落手后本拍立即返回，后续行顺延下一拍。外层 GraphStep 把它算进行动
    闸门（见 flow._is_action_node）：弹窗区在扫期间整图落手权归它，并行分支
    顺延——弹窗没清完时别的操作多半会点歪，这反而是想要的语义。
    """

    def __init__(self, name, modules: list, silent_secs: float = 5.0,
                 total_timeout: float = 180.0):
        super().__init__(name, timeout=0, retries=0)
        # modules: [{"category", "name", "action": "tap"}]，顺序 = 扫描优先级
        self.modules = modules or []
        self.silent_secs = float(silent_secs)
        self.total_timeout = float(total_timeout)
        self._entered = None         # 进入本区的时刻（总超时从这里计时）
        self._last_hit = None        # 最近一次命中时刻（静默从这里计时）
        self._missing_warned = False  # 缺图行只报一次

    def reset(self):
        super().reset()
        self._entered = None
        self._last_hit = None
        self._missing_warned = False

    def tick(self, ctx):
        now = time.monotonic()
        if self._entered is None:
            self._entered = self._last_hit = now
        if not self._missing_warned and ctx.log is not None:
            # 缺图行 template_present 恒 False = 静默死行；第一拍一次性点名，
            # 免得用户对着"这个模块怎么不生效"抓瞎（缺图拆穿纪律同 click_template）
            self._missing_warned = True
            dead = [(m.get("name") or "（没选图）") for m in self.modules
                    if not m.get("name") or not _find_template(
                        m.get("category", ""), m.get("name", ""))]
            if dead:
                ctx.log(f"弹窗区「{self.name}」缺图跳过模块：{'、'.join(dead)}"
                        f"（先去采集这些图）", "WARN")
        # 总超时从进入计时（不是从最近命中）：弹窗一直冒 = 关了又开的死循环，
        # 若按命中重置则永不触发，等于没有兜底 —— 判 failed 截图留证收摊
        if 0 < self.total_timeout <= now - self._entered:
            if ctx.log is not None:
                ctx.log(f"弹窗区「{self.name}」{self.total_timeout:g}秒内弹窗没消停，"
                        f"判失败（截图留证在 logs/fail_capture）", "WARN")
            return "failed"
        for m in self.modules:
            name = m.get("name", "")
            if not name:
                continue
            if not template_present(ctx, m.get("category", ""), name):
                continue
            tap_template(ctx, m.get("category", ""), name)
            self._last_hit = now
            if ctx.log is not None:
                ctx.log(f"弹窗区「{self.name}」命中「{name}」，点掉它", "INFO")
            return "running"   # 每拍最多落手一次，后面的行下一拍再说
        if now - self._last_hit >= self.silent_secs:
            return "ok"        # 静默期满：弹窗确实清完了
        return "running"


class FSM:
    """
    步骤链执行引擎。tick() 返回：
      "running" = 继续跑
      "done"    = 全部步骤完成
      "failed"  = 某步骤失败
    失败处理（返回键已下线）：截图留证后直接判失败交给调用方收摊，
    不再按 BACK 回退重试 —— 画面已变，回退重跑没有意义。
    """

    def __init__(self, steps, ctx, task_name="task", quiet=False):
        self.steps = steps
        self.ctx = ctx
        self.task_name = task_name
        self.quiet = quiet
        self.idx = 0
        self._recovered = False
        self.last_fail_path = ""  # 最近一次失败截图路径（worker 写库用）
        self.last_fail_reason = ""  # 失败原因（步骤名，图引擎附死点节点，worker 写库用）

    def tick(self):
        if self.idx >= len(self.steps):
            return "done"
        step = self.steps[self.idx]
        res = step.tick(self.ctx)
        if res == "ok":
            self.idx += 1
            self._recovered = False
        elif res == "failed":
            if not self._recovered:
                # 失败：截图留证，直接判失败交给调用方（不再按 BACK 重试）
                path = save_fail_capture(self.ctx, self.task_name, step.name)
                self.last_fail_path = path
                # 图引擎把"死在哪个节点"记在 step.failed_node 上（截图文件名只有
                # 步骤名"流程图"，看不出是 20 个节点里的哪个）；写库原因拼上它
                fn = getattr(step, "failed_node", "")
                self.last_fail_reason = f"{step.name}·{fn}" if fn else step.name
                if not self.quiet:
                    print(f"[FSM] 步骤「{step.name}」失败，截图留证 {path}")
                self._recovered = True
                return "running"
            return "failed"
        elif res in (ABORT, FLOW_END, LOOP_CONTINUE):
            # 主动中止/结束（「结束流程」节点命中，三种范围）：不走恢复链、
            # 不消耗重试，原样透传给调用方 —— 边界（LoopStep/SubFlowStep）就地
            # 吸收 FLOW_END；一路没人接传到 worker 时当作"任务提前正常收尾"
            return res
        return "running"


# ── 模板小工具（任务里反复用）──────────────────────────────────────────

def _find_template(category: str, name: str) -> dict | None:
    """按 (目录, 名字) 找模板；category 传 "" 表示只按名字找（任意目录）。
    实现收口在 core.template_manager.find_template（全项目一份匹配逻辑）。"""
    from core import template_manager
    return template_manager.find_template(category, name)


def _load_tpl(entry: dict):
    from core import template_manager
    return template_manager.load_template_image(entry)


def _tpl_hit(ctx, category: str, name: str, roi=None):
    """当前帧上 (cat,name,roi) 的匹配结果（同拍只算一次，性能审计）：
    门 template_present 命中后动作 tap_template 会对同一帧同一模板再匹配一遍，
    WatchStep 扫描/PopupZoneStep 多行也反复问同一问题——同帧同参答案必相同。
    缓存按帧对象身份判鲜（bot/frame_cache），roi 口径与调用方一致：
    None=跟账本区域，传值=覆盖。账本缺失/无帧返回 None。
    ⚠ 先查账本再看帧（与改造前的老函数同序）：缺图降级路径的测试 ctx 可能
    压根没有 frame 属性，老代码靠 entry is None 短路从不碰它，顺序反了会炸。"""
    entry = _find_template(category, name)
    if entry is None or ctx.frame is None:
        return None
    key = ("tpl", category, name, tuple(roi) if roi is not None else None)
    def _scan():
        tpl = _load_tpl(entry)
        if tpl is None:
            # 账本有记录但 PNG 读不到（文件缺失/解压中文名乱码——1607 假zip事故
            # 实测）：按未命中返回，绝不把 None 喂进匹配炸 .shape；点名报一次
            # （同图不刷屏），不然流程干转没人知道为什么永不命中。
            warned = getattr(ctx, "_miss_tpl_warned", None)
            if warned is None:
                warned = ctx._miss_tpl_warned = set()
            if (category, name) not in warned:
                warned.add((category, name))
                if getattr(ctx, "log", None) is not None:
                    ctx.log(f"⚠ 模板文件缺失：{category}/{name}（重新解压安装包或重采该图）")
            return None
        return vision.match_template(ctx.frame, tpl, entry["threshold"],
                                     roi=roi if roi is not None else entry.get("roi"),
                                     rgb_threshold=entry.get("rgb_threshold"))
    return cached(ctx, key, _scan)


def jitter_offset(jitter) -> tuple[int, int]:
    """落点抖动的随机位移 (dx,dy)：极坐标定环带，保证「距中心 ∈ [min,max]」
    真成立（先掷半径再方框内散会把 min 兜空 —— 那 r=min 时落点仍可能是正中心）。
    jitter 收 int（= [0,r]，兼容老单键）或 (min,max)。max<=0 = 不抖 (0,0)。
    每次调用掷一个新的 —— 必须每次落手现掷，编译期掷一次 = 全程同一个点，白随机。"""
    if isinstance(jitter, tuple):
        mn, mx = jitter
    else:
        mn, mx = 0, jitter
    mx = int(mx)
    if mx <= 0:
        return 0, 0
    mn = max(0, min(int(mn), mx))
    r = random.uniform(mn, mx)
    ang = random.uniform(0, 2 * math.pi)
    return round(r * math.cos(ang)), round(r * math.sin(ang))


def tap_template(ctx, category: str, name: str,
                 dx: int = 0, dy: int = 0, jitter=0, roi=None) -> bool:
    """通用动作：在当前帧找模板并点击。支持 click_target 固定坐标优先。返回是否点到
    dx/dy：相对命中中心的像素偏移（图标在左、要点右侧文字这类场景）。
    ⚠ 0/0 = 「没特殊要求」→ 回落账本里这张图存的 click_offset（偏移跟着图走，
    一次校正全体使用者受益）；图也没存才点中心。行/节点采的非 0 偏移盖过图的。
    jitter：落点抖动，int（[0,r] 圆内）或 (min,max)（距中心 ∈ 环带），模拟人手
    每次按点不完全重合；0 = 固定。设了 click_target 时偏移与抖动都不生效。
    roi：搜索区域覆盖（None = 跟模板账本默认区域走）。"""
    hit = _tpl_hit(ctx, category, name, roi)
    if hit is None:
        return False
    # 命中结果已由 _tpl_hit 同拍缓存（门/动作/多行撞同图只匹配一次），这里只补点击量
    entry = _find_template(category, name)
    tpl = _load_tpl(entry)
    # 固定点击坐标优先：模板设了 click_target 就点那里，否则点模板中心（+偏移±抖动）
    ct = entry.get("click_target")
    if ct is not None:
        cx, cy = ct[0], ct[1]
    else:
        if not dx and not dy:
            co = entry.get("click_offset")   # 图存的相对偏移（没有=没存，老行为）
            if co:
                dx, dy = int(co[0]), int(co[1])
        cx = hit["x"] + tpl.shape[1] // 2 + dx
        cy = hit["y"] + tpl.shape[0] // 2 + dy
        jdx, jdy = jitter_offset(jitter)
        cx += jdx
        cy += jdy
    ldplayer.adb_shell(ctx.ld_index, f"input tap {cx} {cy}")
    # 命中取证（可排查性审计 ①）：坐标+分数+实际落点全落日志——"为什么没
    # 点上"从此日志可答；顺手存命中框，失败留证截图据此画框（一图两证）。
    if getattr(ctx, "log", None) is not None:
        ctx.log(f"命中 {category}/{name} 左上({hit['x']},{hit['y']}) "
                f"分{hit['score']:.3f} → tap({cx},{cy})")
    ctx.last_hit_rect = (hit["x"], hit["y"], tpl.shape[1], tpl.shape[0], cx, cy)
    return True


def tap_all_collect(ctx, category: str, name: str, dx: int = 0, dy: int = 0,
                    jitter=0, max_hits: int = 10, roi=None) -> list:
    """「点全部」采集：找当前帧里模板的所有达标命中（上→下、左→右序），换算成
    落点 [(cx,cy),...] = 命中中心+偏移+逐点抖动（每处各掷一次，不是整批一个偏量）。
    与 tap_template 同源：账本阈值/ROI 照用（roi 传值 = 覆盖账本区域）；
    min_gap 重叠抑制 = 模板长边一半（同一目标的相邻滑窗只留一个，不同目标照常分开）。
    账本 click_target（固定点击坐标）对多目标无意义：忽略并 WARN 点名，绝不静默。
    找不到模板/无帧返回 []。"""
    entry = _find_template(category, name)
    if entry is None or ctx.frame is None:
        return []
    tpl = _load_tpl(entry)
    if tpl is None:
        return []          # 缺图按无命中（同 _tpl_hit：None 喂匹配会炸 .shape）
    hits = vision.match_template_all(
        ctx.frame, tpl, entry["threshold"],
        roi=roi if roi is not None else entry.get("roi"),
        rgb_threshold=entry.get("rgb_threshold"),
        sort_by_score=False,
        min_gap=max(2, max(tpl.shape[0], tpl.shape[1]) // 2))
    if hits and entry.get("click_target") is not None:
        if getattr(ctx, "log", None) is not None:
            ctx.log(f"模板「{category}/{name}」设了固定点击坐标，点全部时忽略"
                    f"（N 个目标共用一个点无意义），照常点各命中中心", "WARN")
    if not dx and not dy:
        co = entry.get("click_offset")       # 与 tap_template 同链：没配→跟随图偏移
        if co:
            dx, dy = int(co[0]), int(co[1])
    w2, h2 = tpl.shape[1] // 2, tpl.shape[0] // 2
    pts = []
    for h in hits[:max(1, int(max_hits))]:
        jdx, jdy = jitter_offset(jitter)
        pts.append((h["x"] + w2 + dx + jdx, h["y"] + h2 + dy + jdy))
    return pts


def template_present(ctx, category: str, name: str, roi=None) -> bool:
    """校验函数：模板在当前帧是否存在。roi=None = 跟模板账本默认区域走；
    传区域 = 覆盖账本（同一张图在不同条件里框不同位置各找各的）。
    走 _tpl_hit：与紧随其后的 tap_template 共享同拍匹配结果，不再各匹配一遍。"""
    return _tpl_hit(ctx, category, name, roi) is not None


def swipe(ctx, x1: int, y1: int, x2: int, y2: int, ms: int = 500):
    """滑动（安卓坐标空间）。例：上滑 = swipe(ctx, 270, 700, 270, 300)"""
    ldplayer.adb_shell(ctx.ld_index, f"input swipe {x1} {y1} {x2} {y2} {ms}")


def swipe_pts(direction: str, px: int, ms: int = 500, roi=None):
    """沿方向划 px 距离的起止点。默认以画面中心 (270,480) 为原点、钳进屏内；
    给了 roi=[x0,y0,x1,y1]（基准分辨率坐标）则改以区域中心为原点、
    端点钳进区域（离框边留 2px，免得点贴边划不动）。
    识字取物与判断节点「滑动翻页继续找」共用一份口径（两处算法必须一致）。"""
    half = px // 2
    if roi:
        x0, y0, x1, y1 = (int(v) for v in roi)
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        lx, tp, rx, bt = x0 + 2, y0 + 2, max(x0 + 2, x1 - 2), max(y0 + 2, y1 - 2)
    else:
        cx, cy = 270, 480
        lx, tp, rx, bt = 10, 10, 530, 950
    if direction in ("up", "down"):
        # 手指方向：up = 从下往上划（内容上移，露出下方），x 不变
        y_from = cy + half if direction == "up" else cy - half
        y_to = cy - half if direction == "up" else cy + half
        return (cx, max(tp, min(bt, y_from)),
                cx, max(tp, min(bt, y_to)), ms)
    x_from = cx + half if direction == "left" else cx - half
    x_to = cx - half if direction == "left" else cx + half
    return (max(lx, min(rx, x_from)), cy,
            max(lx, min(rx, x_to)), cy, ms)
