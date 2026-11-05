"""
bot/tasks/flow.py — 自定义流程任务（可视化编排的运行时翻译）

用户在图色脚本编辑器里编排的流程（config.json 的 custom_flows）存成节点树，
本任务把它翻译成流程编排图（GraphStep）执行：branch 节点编译成投票式分支
（引擎内 vote 窗口 + 可选滑动翻页），其 true/false 子链挂在分支节点上。

节点类型（见编辑器文档/plan）：
- click_template / click / swipe / back / sleep / wait_template —— 动作类
  （click_template 带前置门：等目标模板出现再点击，超时跳过/失败可配，见 _action_step）
- multi_scan —— 遍历全部：本帧命中同模板的所有达标处一次收清（快照），
  模式 tap 逐个点掉 / 模式 enter 进一个（跑子流程）→ 返回键 → 等列表标志图
  回家 → 下一处。收口把命中数写进流程变量。编译成 MultiScanStep，
  子流程体按 subflow 同款递归编译（成环/缺图编译期拆穿）。
- debug_print —— 打印检测：文本里 {变量名} 占位符自动替换 + 可选模板匹配/
  坐标取色/变量值，纯观察不改走向
- save_assets —— 资产入库：把「识字取物」读数用法存的流程变量按账号写进
  资产账本（accounts.extra_json["assets"]，金币/钻石这类数值，表格「资产」列
  与右键详情窗的数据源）。永远成功、绝不拦路（见 _save_assets）
- compute —— 计算赋值：把算术表达式（支持 + - * / // % ** 与 abs/min/max/
  round，变量名直写或 {变量名}/{账号.列} 占位符）的值写进流程变量，供后续
  判断/打印/资产入库用。表达式非法只 WARN 跳过、绝不拦路（见 _compute）
- branch —— 判断：cond 五种（template / color / multi_color / text / var），
  true/false 各自子链；var = 流程变量比较（配 server_pick 读数用法，先读后判）
- subflow —— 子流程（加强版点模板）：把另一条已存流程当成一个节点嵌进当前流程。
  带进入前置门（等目标模板出现/纯固定延时，复用 ActionStep 的 gate 语义）；
  跑完分「成功/失败」两个出口，失败不炸整图、交用户连线决定走哪条。
  运行时编译成子 GraphStep（GraphStep 本身就是一个 Step，契约相同），由
  SubFlowStep 包裹转三门结论。递归带 flow_stack 防自引用死循环。
  见 SubFlowStep / _make_build_step / _compile_graph_impl。
- popup_zone —— 清弹窗行（只作为循环体行存在，独立「弹窗区」节点 2026-09-25
  已删）：「识别模板→动作」模块行每拍按序扫描，命中即执行并重置静默计时，
  连续 silent_secs 秒无命中才算这行走完，total_timeout 全程兜底防弹窗死循环。
  编译成 PopupZoneStep（见 bot/fsm.py）。老流程里的独立弹窗区节点载入时经
  _drop_popup_zones 自动摘除重接线。
- watch —— 并行监视哨兵：不连线、开局即在岗，rows 每行「弹窗模板→行型」，
  每拍主线收敛后扫一遍（扫描不落手），命中即跑对应子流程 ——
  子链期间主线整条暂停（一刀切，不用连线；2026-09-25 拍板），办完自动从暂停处续跑。与主线并行而非串在链上 ——
  这是"扩宽多种类别流程"的并行线路（用户拍板设计）。
  行型三种：kind="tap" 命中即点（认出图标直接点掉，纯关弹窗，不跑子流程）；
  kind="ban" 封号停用（认出封号弹窗 = 抛 AccountBannedExit 收工：留证截图、
  写库标记账号封号、停本实例线程，右键可解禁）；
  缺省"subflow" 命中跑子流程（可勾 tap_on_hit=起跑前先点入口图标，子流程体
  不用再配「点模板」入口节点，哨兵调子流程与主线（点模板→子流程）对称）。
  跑完冷却、连败 N 次停用该行。编译成 WatchStep，推进通道见 GraphStep 拍尾。
- timer —— 定时执行哨兵：watch 的"看钟版"，同样不连线、开局即在岗，
  rows 每行「间隔分钟→子流程」，到点自动跑一次，跑完歇满下一个间隔再跑。
  失败不烧周期（歇短冷却重试）、连败 N 次停用该行。编译成 TimerStep
  （WatchStep 子类，只换触发判据），推进通道同走 GraphStep 拍尾。
- signal —— 发信号：往 ctx.extra["fs:pbox"] 信箱投一个信号名就直通（永不失败）。
  外层「循环 / 遍历全部」节点在属性里登记同名「收到即结束的信号」名单才会掐：
  命中即销账（一次销账，先到先得），被掐容器 ok 收口走 out 口续跑。
  跨子流程天然可达（父子共用同一个 BotContext）。

引擎复用 bot/fsm.py 的 ActionStep / WaitStep / LogStep 与 GraphStep、
滑动找状态机 SwipeFinder、超时收口 resolve_step_timeout、复位重建 restart_chain，
以及动作原语 tap_template / swipe / template_present；
ActionStep 为此新增可选 gate 前置门（带默认值，其余任务调用零影响）。
"""

import re
import time
import uuid

from bot.fsm import (
    ABORT,
    FLOW_END,
    LOOP_CONTINUE,
    AccountBannedExit,
    ActionStep,
    FlowTimeoutExit,
    GraphStep,
    LogStep,
    LoopStep,
    PopupZoneStep,
    SignalStep,
    Step,
    StopFlowStep,
    SwipeFinder,
    WaitStep,
    resolve_step_timeout,
    restart_chain,
    signal_take,
    jitter_offset,
    swipe,
    tap_all_collect,
    tap_template,
    template_present,
)
from bot import watch_stats
from bot.expr import eval_expr
from bot.frame_cache import cached
from core import ldplayer, template_manager, vision
from core.config_manager import get_config

# ── 动作节点 → FSM 步骤 ────────────────────────────────────────────────

def _ocr_tick_budget() -> float:
    """识边/读数单拍 OCR 时间预算（配置 vision.ocr_tick_budget_sec，默认 0.2 秒）：
    rapidocr 一次推理可达数百毫秒，逐点串行不限量的话一拍能吃穿整个节拍循环。
    超预算的点顺延下一拍（拍间不丢，只是摊平）。坏值/缺省一律回落默认——
    识别节点不许因配置崩。"""
    try:
        v = float(get_config().get("vision.ocr_tick_budget_sec", 0.2))
    except (TypeError, ValueError):
        return 0.2
    return v if v > 0 else 0.2


def _scan_tick_budget() -> float:
    """匹配类判定单拍时间预算（配置 vision.click_timeout_scan_budget_sec，默认
    0.15 秒）：多条件判断/兜底扫描每拍允许烧的时间上限，口径同上。"""
    try:
        v = float(get_config().get("vision.click_timeout_scan_budget_sec", 0.15))
    except (TypeError, ValueError):
        return 0.15
    return v if v > 0 else 0.15


def _tap(c, x, y):
    ldplayer.adb_shell(c.ld_index, f"input tap {x} {y}")


def _tap_jitter(c, x, y, jitter):
    """固定坐标点击 + 抖动（与 tap_template 共用 jitter_offset：int=[0,r] 或
    (min,max) 环带，每次落手现掷），并钳在画面内（贴屏幕角也不会抖出界）。"""
    jdx, jdy = jitter_offset(jitter)
    _tap(c, max(0, min(539, x + jdx)), max(0, min(959, y + jdy)))


def flow_var_set(ctx, name, value):
    """流程变量写入：挂在 ctx.extra 的 "fv:名字" 键（BotContext 一次挂机运行
    内共享，跨节点、跨子流程都取得到）。P1 先落存储，P2 接读取侧：判断节点加
    var 条件、打印节点认 {变量名} 占位符。"""
    ctx.extra["fv:" + name] = value


def flow_var_get(ctx, name, default=None):
    return ctx.extra.get("fv:" + name, default)


def _jit_range(node: dict) -> tuple[int, int]:
    """落点抖动半径区间 [min,max]（px）：新键 jitter_px_min/max；
    max 没配回落老单键 jitter_px（老节点 = [0,r] 圆内，语义不变），再没 = 不抖。"""
    jmn = max(0, int(node.get("jitter_px_min", 0) or 0))
    jmx = int(node.get("jitter_px_max", 0) or 0) or int(node.get("jitter_px", 0) or 0)
    return jmn, max(jmn, jmx)


def _to_px(v):
    """把一个求值结果（数字 / "123"/"123.0" 字符串）转成整数像素。转不出 = 返回 None。
    bool 不当数（与 bot.expr 同纪律）；float 取整（坐标像素无小数）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def _eval_coord_text(ctx, raw: str):
    """坐标表达式求值（coord / x_expr / y_expr 三处共用）：
    先按原式 eval_expr —— Name 直取流程变量（元组变量原样返回，不先字符串化；
    {名字} 由 eval_expr 自带的花括号归一处理）；这一步失败再经 _fmt_text 展开
    {账号.列名} 这类占位符后求值。两路都失败 → 抛（调用方收敛成 WARN 跳过）。
    ⚠ 顺序不能反：先 _fmt_text 会把元组变量 str() 成 "(100, 200)"，那是元组字面量
    语法、不在 eval_expr 白名单里，反而把能用的写法弄坏。"""
    try:
        return eval_expr(raw, lambda n: flow_var_get(ctx, n))
    except Exception:
        return eval_expr(_fmt_text(ctx, raw), lambda n: flow_var_get(ctx, n))


def _click_coord_from_text(raw: str, ctx):
    """把一段坐标表达式文本解成 (x, y)：经 _eval_coord_text 求值后按形态认：
      - 元组/列表（长度≥2）→ 取 [0]、[1]（「识字取物 读坐标」存的就是这类）；
      - 含逗号的字符串（"123, 456"）→ 按逗号拆两个数（全/半角逗号都认）；
      - 其余（单个数字/普通字符串）→ 拆不出坐标，报错。
    拆不出两个整数坐标 / 求值失败 → 抛（调用方统一收成 WARN 跳过）。"""
    val = _eval_coord_text(ctx, raw)
    if isinstance(val, (tuple, list)):
        if len(val) < 2:
            raise ValueError(f"「{raw}」求值是长度 {len(val)} 的序列，取不出 (x,y)")
        x, y = _to_px(val[0]), _to_px(val[1])
    elif isinstance(val, str):
        parts = [p for p in val.replace("，", ",").split(",")]
        if len(parts) != 2:
            raise ValueError(f"「{raw}」字符串里没按「x,y」拆出两个数：{val!r}")
        x, y = _to_px(parts[0]), _to_px(parts[1])
    else:
        raise ValueError(f"「{raw}」求值结果不是坐标：{val!r}")
    if x is None or y is None:
        raise ValueError(f"「{raw}」没解析出两个整数坐标")
    return x, y


def _resolve_click_xy(node: dict, ctx):
    """解析 click 节点落点 (x, y)：优先「坐标变量/表达式」新键，全无则回落写死的
    tap_x/tap_y。返回 (x, y, err)：err 为 None=成功；非 None=一段说明（WARN 用）。

    优先级（新键存在即用它，存量节点逐字不变走老路）：
      1) x_expr 与 y_expr 都给定：各自求值取单轴（变量名直写或算术都行）；
      2) 否则 coord 给定：求值结果须是 (x,y) 元组/列表、或含逗号的 "x,y" 字符串；
      3) 都没给：回落 int(tap_x)/int(tap_y)（旧行为，不做任何表达式解析）。
    ⚠ 宁可不动手，不可瞎点：任何解析失败一律返回 err（调用方只 WARN 跳过本步），
    绝不回落 (0,0)、绝不拿半截坐标落手——点错地方比不点危险得多。"""
    xe = str(node.get("x_expr", "") or "").strip()
    ye = str(node.get("y_expr", "") or "").strip()
    if xe or ye:
        if not (xe and ye):
            return None, None, "x_expr/y_expr 要成对填（只填了一个，本步不点）"
        try:
            x = _to_px(_eval_coord_text(ctx, xe))
            y = _to_px(_eval_coord_text(ctx, ye))
        except Exception as e:      # ExprError / 任何意外都归一成「算不出」
            return None, None, f"坐标表达式算不出（x_expr={xe} y_expr={ye}）：{e}"
        if x is None or y is None:
            return None, None, f"坐标表达式没解出整数（x_expr={xe} y_expr={ye}）"
        return x, y, None
    coord = str(node.get("coord", "") or "").strip()
    if coord:
        try:
            x, y = _click_coord_from_text(coord, ctx)
            return x, y, None
        except Exception as e:
            return None, None, f"坐标表达式算不出（coord={coord}）：{e}"
    # 老路：写死坐标（存量节点逐字不变）
    x = int(node.get("tap_x", node.get("x", 0)))
    y = int(node.get("tap_y", node.get("y", 0)))
    return x, y, None


def _warn_click_var_log(ctx, step):
    """click 坐标变量解析失败时的 WARN 回调（CompiledClickStep 注入）。
    只留一行点名日志，不改走向：宁可不动手，不可瞎点。"""
    if ctx is None or getattr(ctx, "log", None) is None:
        return
    ctx.log(f"点坐标「{step.name}」：{getattr(step, '_coord_err', '')}，"
            f"本步跳过不落手", "WARN")


class CompiledClickStep(ActionStep):
    """点坐标（click）的「坐标来自流程变量/表达式」运行体（F3）。

    存量 click 节点没有 x_expr/y_expr/coord 新键 → 编译期直接走老 ActionStep
    （逐字不变）；只有带新键的节点才编成本类，把坐标求值推迟到每拍 tick：
      - 每次落手前现算坐标（子流程里改过的变量也能读到最新值，不是编译期一次性）；
      - 求值失败 / 拆不出两个整数 → 只 WARN 跳过（返回 "ok"，落手一次都没发生），
        绝不回落 (0,0)、绝不瞎点（见 _resolve_click_xy 的「宁可不动手」纪律）；
      - 求出的坐标与老「点选」同款做屏内钳位（0..539 / 0..959），越界 WARN 后
        钳回边界再点——与 tap_template/识字取物点选同一套钳位口径，保持一致。
    无前置门、无校验：与老 click 的 ActionStep 落手语义完全一致（verify=None）。"""

    def __init__(self, name, node, retries=1, on_coord_warn=None):
        super().__init__(name, action=lambda ctx: None, verify=None,
                         retries=retries)
        self._node = node
        self._jit = _jit_range(node)
        self._coord_err = ""
        self._on_coord_warn = on_coord_warn

    def tick(self, ctx):
        if not self._acted:
            x, y, err = _resolve_click_xy(self._node, ctx)
            if err is not None:
                # 解析失败：点名 WARN + 本步跳过（不落手），绝不点错地方
                self._coord_err = err
                if self._on_coord_warn is not None:
                    self._on_coord_warn(ctx, self)
                elif ctx.log is not None:
                    ctx.log(f"点坐标「{self.name}」：{err}，本步跳过不落手", "WARN")
                self._acted = True
                return "ok"
            # 屏内钳位（越界点名后钳回边界，绝不把 x+抖动溢出屏外）
            cx, cy = max(0, min(539, x)), max(0, min(959, y))
            if (cx, cy) != (x, y) and ctx.log is not None:
                ctx.log(f"点坐标「{self.name}」：坐标 ({x},{y}) 越出画面，"
                        f"已钳到 ({cx},{cy})", "WARN")
            _tap_jitter(ctx, cx, cy, self._jit)
            self._acted = True
            return "ok"
        return "ok"


class _SequenceStep(Step):
    """把一串步骤裹成一步：按顺序驱动，前一步 ok 才走下一步，
    任何一步 failed 整步即 failed（结论原样传给 GraphStep 走败口/恢复链）。
    现在唯一的用户 = 点模板「点击后加延迟」（[动作, 固定等待]），
    GraphStep 每节点只挂单个 Step，才有这个包装；将来要串小步序也用它。"""

    def __init__(self, name, steps):
        super().__init__(name)
        self._steps = list(steps)
        self._i = 0

    def reset(self):
        super().reset()
        self._i = 0
        for s in self._steps:
            s.reset()

    def tick(self, ctx):
        while self._i < len(self._steps):
            r = self._steps[self._i].tick(ctx)
            if r == "running":
                return "running"
            if r != "ok":
                return r          # failed/ABORT 原样上抛
            self._i += 1
        return "ok"


def _after_click_wait(node: dict):
    """「点击后加延迟」勾选后的延时步：等 config 的 general.post_click_delay
    秒（常驻延时，功能设置页调）。缺省勾上（与 wait_before 同款：老节点没这键
    也默认开，想快点完就显式取消勾选存 False）；全局值设 0 = 不白挂一步。"""
    if not bool(node.get("delay_after_click", True)):
        return None
    secs = float(get_config().get("general.post_click_delay", 1.0) or 0)
    if secs <= 0:
        return None               # 延时被设成 0：等价不勾，不白挂一步
    return WaitStep("点击后延迟", fixed_wait=secs, retries=0)


def _warn_tap_all_log(ctx, step):
    """点全部等图超时、且按配置「跳过继续」时触发的 WARN。
    口径同 _warn_gate_log（只在 continue 时注入；failed/exit 各有留证/ERROR）。"""
    if ctx is None or getattr(ctx, "log", None) is None:
        return
    ctx.log(f"点模板「{step.name}」点全部：等图出现超时（{step.gate_timeout:g}秒），"
            f"按配置跳过本次，继续往下走", "WARN")


class TapAllStep(Step):
    """「点全部」：把当前帧里同一模板的所有达标命中逐个点完才收口（一个节点顶
    N 个）。引擎红线适配：每拍最多点一个——单实例同一时刻只许落一次手，且第一下
    点完界面就可能变，拿着收集拍的旧坐标继续点已是妥协，"点一个冒一个"式边点边
    重扫等真需要再加。
    - 收集拍兼作前置门：零命中每拍重扫；等满 gate_timeout 按三档收场
      （continue→ok 跳过 / exit→fatal_cb 抛异常 / 其余→failed 走败口），
      与 ActionStep 的 gate 同一套口径（编译层喂同样的参数）。
    - gate_timeout=0（节点关了"点击前先等模板出现"）：零命中当场 ok——
      没图 = 没得点，语义同普通点模板 wait_before=False 的落手一次。
    - max_hits 封顶：误匹配一屏时防把全屏当目标刷。
    全程时间戳计时，tick 内零阻塞；点完返回 ok 并打"完成 N/N"日志。"""

    def __init__(self, name, category, tpl_name, offset=(0, 0), jitter=0,
                 max_hits=10, gap_secs=0.3, gate_timeout=8.0,
                 ok_on_timeout=False, on_timeout_log=None, fatal_cb=None,
                 roi=None):
        # timeout 传门秒数仅供 fatal_cb 日志显示（本步超时判定全走 gate_timeout，
        # 不吃基类 _timed_out）
        super().__init__(name, timeout=float(gate_timeout), retries=0)
        self.category, self.tpl_name = category, tpl_name
        self.roi = roi        # 搜索区域覆盖（None = 跟模板账本默认区域走）
        self.offset, self.jitter = offset, jitter
        self.max_hits = max(1, int(max_hits))
        self.gap = float(gap_secs)
        self.gate_timeout = float(gate_timeout)
        self.ok_on_timeout = ok_on_timeout
        self.on_timeout_log = on_timeout_log
        self.fatal_cb = fatal_cb
        self._pts = None              # None=还在收集（等门）；list=剩余待点
        self._gate_start = None
        self._next_tap = 0.0
        self._total = 0

    def reset(self):
        super().reset()
        self._pts = None
        self._gate_start = None
        self._next_tap = 0.0
        self._total = 0

    def tick(self, ctx):
        now = time.monotonic()
        if self._pts is None:
            if self._gate_start is None:
                self._gate_start = now
            pts = tap_all_collect(ctx, self.category, self.tpl_name,
                                  dx=self.offset[0], dy=self.offset[1],
                                  jitter=self.jitter, max_hits=self.max_hits,
                                  roi=self.roi)
            if not pts:
                if self.gate_timeout <= 0:
                    return "ok"       # 不等门：没图 = 没得点，直接过
                if now - self._gate_start > self.gate_timeout:
                    return resolve_step_timeout(ctx, self, self.ok_on_timeout,
                                                self.on_timeout_log, self.fatal_cb)
                return "running"
            self._pts = list(pts)
            self._total = len(pts)
            if ctx.log is not None:
                ctx.log(f"点模板「{self.tpl_name}」点全部：本帧命中 {self._total} 处，"
                        f"逐个点收")
        if now < self._next_tap:
            return "running"
        x, y = self._pts.pop(0)
        _tap(ctx, x, y)
        self._next_tap = now + self.gap
        if ctx.log is not None:
            ctx.log(f"点全部「{self.tpl_name}」"
                    f"{self._total - len(self._pts)}/{self._total} → tap({x},{y})")
        return "ok" if not self._pts else "running"


def _press_back(ctx):
    """返回键（安卓 KEYCODE_BACK=4）。独立「返回键」节点已下线（老 back 节点编译
    跳过），这里只是「遍历全部·进一个回一个」的私用原语，不复活成用户节点。"""
    ldplayer.adb_shell(ctx.ld_index, "input keyevent 4")


def _warn_multi_scan_log(ctx, step):
    """遍历全部零命中、等图超时、按配置「跳过继续」时触发的 WARN。
    口径同 _warn_tap_all_log（只在 continue 时注入；failed/exit 各有留证/ERROR）。"""
    if ctx is None or getattr(ctx, "log", None) is None:
        return
    ctx.log(f"遍历全部「{step.name}」：等图出现超时（{step.gate_timeout:g}秒），"
            f"按配置跳过本次遍历，继续往下走", "WARN")


def ocr_text_collect(ctx, needle: str, roi=None, min_sim: float = 0.6,
                     max_hits: int = 20) -> list:
    """遍历全部·文字源采集（P3）：OCR 整帧（或 roi）里所有与 needle 判得
    「够像」的行圈出来 —— 裁决与识字取物同一份 core.ocr.match_score（数字锚点
    防认错行）。返回 [(x, y, 行文字), ...] 按上→下、左→右排，行文字自带身份键
    （去重直接用它）。绝不抛：依赖没装 / 帧没到 / 针为空 → []（零命中由门三档
    收场，与 tap_all_collect 同款兜底）。"""
    from core import ocr
    if ctx.frame is None or not needle or not ocr.available():
        return []
    out = []
    for row in ocr.list_text(ctx.frame, roi=tuple(roi) if roi else None):
        if ocr.match_score(row["text"], needle) >= min_sim:
            out.append(((row["x0"] + row["x1"]) // 2,
                        (row["y0"] + row["y1"]) // 2,
                        str(row["text"]).strip()))
    out.sort(key=lambda p: (p[1] // 12, p[0]))
    return out[:max(1, int(max_hits))]


def neighbor_read(ctx, x, y, dx, dy, w, h):
    """读「命中邻域」一小块（遍历全部·读数模式与去重身份共用一份）：
    框中心 = 命中点 +(dx,dy)，尺寸 w×h，钳进 540×960 基准帧。
    返回 (数值|None, 文字|None)：
    - 数值：块里带数字的行换算后恰好一个值才给（1.17万→11700 同款单位口径；
      0 个或多个 = 歧义当没认出，绝不猜读哪个 —— 同「读数字」裁决）
    - 文字：块内 OCR 置信最高的一行（去重身份 = 能读到昵称就用昵称，
      和读数一样绝不猜：读不到由调用方兜底）
    绝不抛：依赖没装 / 帧没到 / 框无效 → (None, None)。"""
    from core import ocr
    if ctx.frame is None or not ocr.available():
        return None, None
    x0, y0 = int(x + dx - w / 2), int(y + dy - h / 2)
    x1, y1 = x0 + int(w), y0 + int(h)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(540, x1), min(960, y1)
    if x1 <= x0 or y1 <= y0:
        return None, None
    # 同帧同框只认一次（性能审计：去重身份与读数共用邻框，一行多问 = 多次 OCR
    # 推理；bot/frame_cache 按帧对象身份判鲜，下一拍新帧自动作废）
    return cached(ctx, ("nbr", x0, y0, x1, y1),
                  lambda: _neighbor_ocr(ctx, (x0, y0, x1, y1)))


def _neighbor_ocr(ctx, box):
    """邻框 OCR 一次推理 + 换算（neighbor_read 的被缓存本体）。"""
    from core import ocr
    rows = ocr.list_text(ctx.frame, roi=box)
    if not rows:
        return None, None
    vals = [v for v in (parse_game_number(r["text"]) for r in rows)
            if v is not None]
    val = vals[0] if vals and len(set(vals)) == 1 else None
    best = max(rows, key=lambda r: r["score"])
    return val, (str(best["text"]).strip() or None)


# 遍历全部·数字过滤的比较符（存储值英文、界面显示中文；未知符号编译期拆穿）
_FILT_OPS = {"lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
             "eq": lambda a, b: a == b, "ge": lambda a, b: a >= b,
             "gt": lambda a, b: a > b, "ne": lambda a, b: a != b}


def _stop_on_names(node: dict) -> list:
    """节点上的「收到即结束的信号」名单 → 干净信号名列表（空 = 不建闸）。
    行形态两种都认：编辑器存的 {"name": ...}、AI 手搭流程直写的纯字符串；
    空名剔掉、首尾空白去掉、重名去一份（名单去重，查闸本来就先到先得）。"""
    out = []
    for r in node.get("stop_on") or []:
        nm = (r.get("name", "") if isinstance(r, dict) else str(r or "")).strip()
        if nm and nm not in out:
            out.append(nm)
    return out


class MultiScanStep(Step):
    """「遍历全部」：把画面里同一目标的所有达标命中一次收清（一份快照），再按
    「模式」逐处处置完才收口（一个节点顶 N 次操作）。

    检测源（source）：
    - "template"（默认）：模板多命中采集（tap_all_collect，同「点全部」一条路）。
    - "text"：OCR 圈字（ocr_text_collect）—— 按文字把所有行圈成目标，不依赖模板。
    处置模式（mode）：
    - mode="tap" 逐个点掉：与 TapAllStep 同款队列（每拍点一个、拍间隔 gap），
      差别只在收口时把命中数写进流程变量（var_name）。
    - mode="enter" 进一个回一个：每处 = 点该命中 → 跑子流程体（内层 FSM，
      同 SubFlowStep 每次起跑先 reset）→（可选，默认不选）按返回键 →
      （配了列表标志图就）等它出现确认"回家了" → 下一处。返回键关着的，
      回列表由子流程自己负责（体内有关闭/返回按钮时用），此时强烈建议配
      列表标志图确认 —— 不然体跑完还赖在详情页，下一处坐标就是瞎点。
      单处子流程失败只 WARN 计数、回来接着办（一棵歪脖子不烧整片林）；
      回程等标志图超时 = 画面没了坐标全废，本节点直接 failed 走败口。
    - mode="read" 逐个读数（P2）：一下都不点 —— 每处读「邻域框」（框中心 =
      命中点+读偏移，尺寸 w×h）里的数字，依次存 var_name_1..N 变量；块里
      0 个或多个数字算没认出（绝不写脏值），只 WARN 不拦路。
    - dedupe=True 去重边扫边办（P3，动态列表用）：不吃快照队列 —— 每办完一处
      重新采集，身份键已办过的跳过，没有新面孔才收口。身份键 = 邻域框里置信
      最高的文字（两个源同口径：文字源的行文字就是圈的词本身、处处相同分不
      开人，认脸还得靠邻域昵称），读不到退化成坐标格子（静态列表同位同脸不
      误伤）。扫满 max_rounds 轮还收不了口 = 身份读不稳/列表停不下来，WARN
      判败，绝不空转。
    - filt_op/filt_val 数字过滤（2026-10-06）：每处先就地读「邻域框」里的数字，
      达标（lt/le/eq/ge/gt/ne）才进队列处置；读不出（无数字/歧义）当不达标
      跳过 —— 绝不拿未知数落手。命中全被滤掉 = 就地 ok 收口，不烧门超时。
      与去重共用同一拍邻框读数（一次 OCR 既认脸又过滤）。read 模式无意义，
      编译层强制关（同 dedupe 双保险）。
    - 零命中的收集拍兼作前置门：每拍重扫，等满 gate_timeout 三档收场
      （continue→ok 跳过 / exit→fatal_cb 抛 / 其余→failed），与 TapAllStep
      同一套口径（编译层喂同样参数）。gate_timeout=0 = 没图没得办，当场 ok。
    - stop_on 收到即结束的信号（2026-10-06）：办完一处、开下一处之前查信箱，
      对上名单就销账收工（ok 收口走 out 口）——正在办的这一处办完才收，
      绝不半途把列表坐标撕在半路；剩下的处数不碰了。没配 = 老行为。
    - 快照模型（dedupe=False 时）：坐标收集一次、循环期间不再重扫也不去重 ——
      只适合"回列表页后列表不变"的场景；会自己刷新/翻页的列表开 dedupe。
    全程时间戳计时，tick 内零阻塞；每拍最多落一次手。"""

    def __init__(self, name, category, tpl_name, mode="tap", offset=(0, 0),
                 jitter=0, max_hits=20, gap_secs=0.5, gate_timeout=8.0,
                 ok_on_timeout=False, on_timeout_log=None, fatal_cb=None,
                 roi=None, var_name="", body=None, confirm=None,
                 confirm_timeout=5.0, settle_secs=0.8,
                 source="template", text="", min_sim=0.6,
                 dedupe=False, max_rounds=50, read_box=(0, 0, 160, 48),
                 press_back=True, filt_op="", filt_val=0.0, stop_on=None):
        # timeout 传门秒数仅供 fatal_cb 日志显示（本步超时判定全走 gate_timeout，
        # 不吃基类 _timed_out）——与 TapAllStep 同款
        super().__init__(name, timeout=float(gate_timeout), retries=0)
        self.category, self.tpl_name = category, tpl_name
        self.roi = roi
        self.mode = mode
        self.offset, self.jitter = offset, jitter
        self.max_hits = max(1, int(max_hits))
        self.gap = float(gap_secs)
        self.gate_timeout = float(gate_timeout)
        self.ok_on_timeout = ok_on_timeout
        self.on_timeout_log = on_timeout_log
        self.fatal_cb = fatal_cb
        self.var_name = var_name        # 空串 = 不写变量
        self.body = list(body or [])    # enter 模式的子流程编译产物
        self.confirm = confirm          # (category, name) 或 None=返回后不确认
        self.confirm_timeout = float(confirm_timeout)
        self.settle = float(settle_secs)  # 返回后先让画面稳一会儿再确认/落手
        self.source = source            # "template" / "text"
        self.text = text                # 文字源的目标文字
        self.min_sim = float(min_sim)
        self.dedupe = bool(dedupe)
        self.max_rounds = max(1, int(max_rounds))
        self.read_box = tuple(int(v) for v in read_box)  # (dx,dy,w,h) 邻域框
        self.press_back = bool(press_back)   # enter 回程：关 = 子流程自己负责回列表
        # 数字过滤："" = 关；只认 _FILT_OPS 里的符号（编译层已校验，构造双保险）
        self.filt_op = str(filt_op or "")
        if self.filt_op not in _FILT_OPS:
            self.filt_op = ""
        self.filt_val = float(filt_val or 0)
        # 收到即结束的信号名单（空/None = 不建闸，老行为逐字不变）
        self.stop_on = [str(s).strip() for s in (stop_on or [])
                        if str(s).strip()] or None
        self._label_src = tpl_name or text   # 日志里的目标名（模板名/文字）
        self._pts = None                # None=还在收集（等门/重扫）；list=剩余待办
        self._gate_start = None
        self._next_act = 0.0            # 下一次落手/确认的最早时间
        self._total = 0
        self._fails = 0
        self._phase = "hit"             # enter 子机器：hit/body/back/confirm
        self._confirm_start = None
        self._inner = None              # 子流程内层 FSM（惰性，收口即弃）
        self._seen = set()              # 去重：已办身份键
        self._rounds = 0                # 去重：已扫描轮数
        self._scan_rest = None          # 去重/过滤：本轮还剩几个点没扫完（超预算顺延用）
        self._scan_news = []            # 去重/过滤：已验出的待办点（跨拍攒）
        self._round_raw = 0             # 本轮原始命中数（判「全被滤掉」用）
        self._filt_drop = 0             # 本轮被数字过滤剔掉几处

    def reset(self):
        super().reset()
        self._pts = None
        self._gate_start = None
        self._next_act = 0.0
        self._total = 0
        self._fails = 0
        self._phase = "hit"
        self._confirm_start = None
        self._inner = None
        self._seen = set()
        self._rounds = 0
        self._scan_rest = None
        self._scan_news = []
        self._round_raw = 0
        self._filt_drop = 0

    def _done_ok(self, ctx):
        if self.dedupe and self.var_name:
            # 去重模式的收集拍数字是"本轮新面孔"，收口才写真实办成总数
            flow_var_set(ctx, self.var_name, len(self._seen))
        if ctx.log is not None:
            n = len(self._seen) if self.dedupe else \
                self._total - (len(self._pts) if self._pts else 0)
            extra = f"，其中子流程没办成 {self._fails} 处" if self._fails else ""
            ctx.log(f"遍历「{self._label_src}」共办 {n} 处收口{extra}")
        return "ok"

    def _stop_by_signal(self, ctx) -> bool:
        """「收到即结束」查闸（2026-10-06）：办完一处、正要开下一处时调。
        信箱里躺着本节点登记的信号 → 销账返回 True：刚办的那处是办完的
        （列表页坐标不半途撕），剩下的处数不碰了，ok 收口走 out 口续跑。"""
        if not self.stop_on:
            return False
        hit = signal_take(ctx, self.stop_on)
        if hit is None:
            return False
        if ctx.log is not None:
            left = len(self._pts) if self._pts else 0
            ctx.log(f"遍历「{self._label_src}」收到停止信号「{hit}」：提前收工，"
                    f"还剩 {left} 处不碰了", "INFO")
        return True

    def _ident(self, ctx, x, y):
        """去重身份键：邻域框文字（读得到昵称就用昵称），读不到退化坐标格子。
        退化不是将就 —— 静态列表里同位 = 同脸，照样滤得对；只有动态列表才
        指望昵称，那已由邻域文字覆盖。"""
        _, txt = neighbor_read(ctx, x, y, *self.read_box)
        return txt if txt else (x // 24, y // 24)

    def tick(self, ctx):
        now = time.monotonic()
        if self._pts is None:
            # ── 收集拍（兼零命中前置门；去重模式 = 每轮重扫）──
            if self._gate_start is None:
                self._gate_start = now
            if self.source == "text":
                pts = ocr_text_collect(ctx, self.text, roi=self.roi,
                                       min_sim=self.min_sim,
                                       max_hits=self.max_hits)
            else:
                pts = [(x, y, None) for x, y in tap_all_collect(
                    ctx, self.category, self.tpl_name,
                    dx=self.offset[0], dy=self.offset[1], jitter=self.jitter,
                    max_hits=self.max_hits, roi=self.roi)]
            if self.dedupe or self.filt_op:
                if self._scan_rest is None:
                    if self.dedupe:
                        # 新一轮起跑才计轮数（顺延拍不重复计，max_rounds 语义不变）
                        self._rounds += 1
                        if self._rounds > self.max_rounds:
                            if ctx.log is not None:
                                ctx.log(f"遍历「{self._label_src}」去重扫了 {self._rounds} 轮"
                                        f"还有新面孔：列表停不下来或身份读不稳，本节点判败",
                                        "WARN")
                            return "failed"
                    self._round_raw = len(pts)
                    self._filt_drop = 0
                    self._scan_rest = list(pts)
                    self._scan_news = []
                # 识边单拍预算（性能审计）：每命中邻框 OCR 一次可数百毫秒，
                # N 点串行不限量的话一拍就能吃穿节拍循环。预算内验几个算几个，
                # 剩下的点顺延下一拍接着验——身份仍现读当拍帧（帧对象一换缓存
                # 即废，不脏读），总结果与逐点顺序都不变，只是把拍摊平。
                deadline = time.monotonic() + _ocr_tick_budget()
                rest = self._scan_rest
                i = 0
                while i < len(rest):
                    # 身份/过滤读数一律现读邻框：一次 neighbor_read 既认脸又
                    # 过数（同框同帧走缓存，不重复推理）；文字源的行文字 =
                    # 圈的词本身分不开人，认脸只认邻框
                    x, y, _row = rest[i]
                    if self.filt_op:
                        val, txt = neighbor_read(ctx, x, y, *self.read_box)
                        # 读不出（无数字/歧义）当不达标 —— 绝不拿未知数落手
                        if val is None or not _FILT_OPS[self.filt_op](val, self.filt_val):
                            self._filt_drop += 1
                        else:
                            key = txt if txt else (x // 24, y // 24)
                            if not self.dedupe or key not in self._seen:
                                self._scan_news.append((x, y, key))
                    else:
                        key = self._ident(ctx, x, y)
                        if key not in self._seen:
                            self._scan_news.append((x, y, key))
                    i += 1
                    if i < len(rest) and time.monotonic() >= deadline:
                        break
                self._scan_rest = rest[i:]
                if self._scan_rest:
                    return "running"   # 本拍预算用完还有点没验：下拍接着验
                pts = self._scan_news
                self._scan_rest, self._scan_news = None, []
                if not pts:
                    if self.dedupe and self._seen:
                        return self._done_ok(ctx)   # 没新面孔 = 全办完了
                    if self.filt_op and self._round_raw:
                        # 有命中但全被滤掉 = 「没活可干」当场收口，不进等门
                        # 反复重扫（否则白烧 OCR 预算，on_timeout=fail 还会误判败）
                        if ctx.log is not None:
                            ctx.log(f"遍历「{self._label_src}」命中 {self._round_raw} 处，"
                                    f"数字过滤后 0 处（不达标/读不出），本节点空手收口")
                        return "ok"
                    pts = []
            if not pts:
                if self.gate_timeout <= 0:
                    return "ok"       # 不等门：没图 = 没得办，直接过
                if now - self._gate_start > self.gate_timeout:
                    return resolve_step_timeout(ctx, self, self.ok_on_timeout,
                                                self.on_timeout_log, self.fatal_cb)
                return "running"
            self._pts = list(pts)
            self._total = len(self._pts)
            self._fails = 0
            if self.var_name and not self.dedupe:
                flow_var_set(ctx, self.var_name, self._total)  # 收集到手就写，不等收口
            if ctx.log is not None:
                how = {"tap": "逐个点掉", "enter": "逐个进入",
                       "read": "逐个读数"}.get(self.mode, self.mode)
                extra = (f"（原始 {self._round_raw} 处，滤掉 {self._filt_drop} 处）"
                         if self.filt_op else "")
                ctx.log(f"遍历「{self._label_src}」本帧命中 {self._total} 处{extra}，{how}")
        if self.mode == "read":
            if self._stop_by_signal(ctx):
                return self._done_ok(ctx)
            # 读数不落手（不动画面），每拍读一处；OCR 同步推理，一拍一块小框
            x, y, _key = self._pts.pop(0)
            i = self._total - len(self._pts)
            val, _txt = neighbor_read(ctx, x, y, *self.read_box)
            if val is not None and self.var_name:
                flow_var_set(ctx, f"{self.var_name}_{i}", val)
            if ctx.log is not None:
                if val is not None:
                    ctx.log(f"遍历读数「{self._label_src}」{i}/{self._total} → "
                            f"{self.var_name}_{i}={val}")
                elif self.var_name:
                    ctx.log(f"遍历读数「{self._label_src}」第 {i} 处：邻域里数字"
                            f"没有或不止一个（歧义当没认出），{self.var_name}_{i} 不写",
                            "WARN")
            return "ok" if not self._pts else "running"
        if self.mode == "tap":
            if self._stop_by_signal(ctx):
                return self._done_ok(ctx)
            if now < self._next_act:
                return "running"
            x, y, key = self._pts.pop(0)
            _tap(ctx, x, y)
            if self.dedupe:
                self._seen.add(key)
            self._next_act = now + self.gap
            if ctx.log is not None:
                ctx.log(f"遍历点掉「{self._label_src}」"
                        f"{self._total - len(self._pts)}/{self._total} → tap({x},{y})")
            if self.dedupe:
                self._pts = None      # 点完列表可能挪位：下拍重扫，别吃旧快照
                return "running"
            return "ok" if not self._pts else "running"
        # ── enter 模式：每处子机器（每拍最多落一次手）──
        ph = self._phase
        if ph == "hit":
            if self._stop_by_signal(ctx):
                return self._done_ok(ctx)
            if now < self._next_act:
                return "running"
            x, y, key = self._pts.pop(0)
            _tap(ctx, x, y)
            if self.dedupe:
                self._seen.add(key)       # 进门即记账：这处已在办/办过
            if ctx.log is not None:
                ctx.log(f"遍历进入「{self._label_src}」"
                        f"{self._total - len(self._pts)}/{self._total} → tap({x},{y})")
            if self.body:
                # 编译产物跨轮复用：复位+重建纪律见 restart_chain
                self._inner = restart_chain(self.body, ctx, task_name=self.name)
                self._phase = "body"
            else:
                self._phase = "back"
            return "running"
        if ph == "body":
            res = self._inner.tick()
            if res in (ABORT, FLOW_END, LOOP_CONTINUE):
                return res        # 体内「结束流程」：原样上抛，不再遍历剩下的
                                  # （行内子流程自己吸收 FLOW_END；LOOP_CONTINUE 冒泡到外层循环）
            if res == "running":
                return "running"
            if res == "failed":
                self._fails += 1
                if ctx.log is not None:
                    ctx.log(f"遍历「{self._label_src}」一处子流程没跑完：算这处没办成，"
                            f"照常返回接着办下一处", "WARN")
            self._inner = None
            self._phase = "back"
            return "running"
        if ph == "back":
            if self.press_back:
                _press_back(ctx)
            if self.dedupe:
                self._pts = None          # 本轮旧快照作废：回去重扫新面孔
            if not self._pts and not self.dedupe:
                return self._done_ok(ctx)
            self._next_act = now + self.settle
            self._confirm_start = None
            self._phase = "confirm" if self.confirm else "hit"
            return "running"
        # ph == "confirm"：等列表标志图回来 = 确认真的回到列表页
        if now < self._next_act:
            return "running"
        if template_present(ctx, self.confirm[0], self.confirm[1]):
            if self._pts:
                self._phase = "hit"
                return "running"
            if self.dedupe:
                self._phase = "hit"       # _pts 已是 None：下一拍重扫
                return "running"
            return self._done_ok(ctx)
        if self._confirm_start is None:
            self._confirm_start = now
        elif now - self._confirm_start > self.confirm_timeout:
            if ctx.log is not None:
                ctx.log(f"遍历「{self._label_src}」返回后等了 {self.confirm_timeout:g}秒"
                        f"仍没看到列表标志图：画面迷路，本节点失败", "ERROR")
            return "failed"
        return "running"


def _fmt_text(c, text: str) -> str:
    """文本占位符统一展开（判断/识字取物/打印三处共用，一处实现全节点通）：
    - {变量名}      → 流程变量现值（识字取物读数存的）
    - {账号.列名}    → 当前实例绑定账号的那一列，列名照界面表头中文写
                      （区服/等级/备注/战力/钥匙/箱子…英文数据库字段也认）；
                      「密码」永不供取（占位符会进日志，日志不是放密码的地方）
    取不到（变量没存 / 没绑账号 / 列名打错）→ 原样留着 {…}，一眼看出哪个没取到。
    绝不静默换空串：空串拿去 OCR 匹配 = 每一行都算命中，那是点错服的事故。"""
    def sub(m):
        name = m.group(1).strip()
        if name.startswith("账号."):
            v = _acct_field(c, name[len("账号."):])
        else:
            v = flow_var_get(c, name)
        return str(v) if v is not None else m.group(0)
    return re.sub(r"\{([^{}]+)\}", sub, text or "")


def _acct_field(c, col: str):
    """{账号.X} 取数：X 界面列名（account_io._IMPORT_FIELDS 同款映射，含老别名）
    或数据库英文字段直写；空值按没取到（None）。
    另认两个派生列（进服两步专用，不占账号表列）：
      选区 = 区服列前 4 个字（游戏区名恒 4 字，不足 4 取整串；含沙射影6服→含沙射影），
             给「点区」那步用——区列表只显示 4 字区名，拿全名去认会被数字锚点判 0 分；
      选服 = 区服全文，给「点服」那步用（语义同 {账号.区服}，纯粹凑成一对好记）。"""
    from core import database, account_io   # 局部 import：与 server_pick 读数侧同款
    acc = database.accounts_by_instance().get(c.ld_index)
    if not acc:
        return None
    if col in _ACCT_DERIVED:
        v = acc.get("server")
        if v is None or not str(v).strip():
            return None
        full = str(v).strip()
        if col == "选服":
            return full
        return full[:4]      # 区名恒 4 字（用户口径），不足 4 Python 切片自动取整串
    field = account_io._IMPORT_FIELDS.get(col, col)
    if field == "password":
        return None
    v = acc.get(field)
    return str(v).strip() if v is not None and str(v).strip() else None


_ACCT_DERIVED = ("选区", "选服")   # 派生列名单（界面下拉同款来源，口径不分叉）


def _save_assets(c, node: dict):
    """资产入库（save_assets 节点）：把指定的流程变量数值按账号存进资产账本。

    items = [{"var": 流程变量名, "name": 资产显示名}, ...]，通常接在
    「识字取物」读数用法后面：先 [读金币→var gold]、[读钻石→var diamond]，
    再一个本节点 {gold→金币, diamond→钻石} 一次落库。
    落库走 database.merge_account_assets（extra_json["assets"]，读写同源）。
    账号归属：优先 ctx.account_id（worker 起线程时已绑），没绑（试运行/没导入
    账号）时按 ld_index 现查 accounts_by_instance —— 试运行也能验落库链路。
    整段吞异常（同 _debug_print 纪律）：这是"顺路记个数"，读没读到都不该拦路、
    更不该把挂机线程带崩。没读到值的变量按"没这数"如实跳过，绝不写 0 污染账面。
    """
    from core import database   # 局部 import：与 server_pick 读数侧同款，避开 core 环
    try:
        items = [it for it in (node.get("items") or []) if isinstance(it, dict)]
        label = node.get("label") or "资产入库"
        pairs = {}     # {变量名: 数值}
        names = {}     # {变量名: 资产显示名}
        for it in items:
            var = str(it.get("var") or "").strip()
            if not var:
                continue
            v = flow_var_get(c, var)
            if v is None:
                continue               # 还没读到这个变量：跳过，不写脏 0
            pairs[var] = v
            nm = str(it.get("name") or "").strip()
            if nm:
                names[var] = nm
        if not pairs:
            if c.log is not None:
                c.log(f"{label}：没有已读到的变量可存"
                      f"（前面「识字取物」的读数用法跑了吗？）", "WARN")
            return
        aid = c.account_id
        if aid is None:
            acc = database.accounts_by_instance().get(c.ld_index)
            aid = (acc or {}).get("id")
        if aid is None:
            if c.log is not None:
                c.log(f"{label}：实例 {c.ld_index} "
                      f"未绑定账号，本批读数没处存（导入账号后生效）", "WARN")
            return
        res = database.merge_account_assets(aid, pairs, names)
        written = res.get("written") or {}
        if c.log is not None:
            if written:
                detail = "、".join(f"{names.get(k) or k}={v}" for k, v in written.items())
                c.log(f"{label}：已存 账号id{aid} {detail}", "INFO")
            else:
                c.log(f"{label}：读数都没落成账"
                      f"（值非数字？账号被删？）", "WARN")
    except Exception as e:
        if c.log is not None:
            c.log(f"资产入库异常（已忽略，不拦流程）：{e}", "WARN")


def _debug_print(c, node: dict):
    """打印检测（纯观察，不改流程走向）：日志输出前缀文本 + 可选检测结果。

    target 四种：none=只打文本；template=打模板命中/最高分/位置；
    color=打某坐标当前像素颜色；var=打流程变量现值。
    整段吞异常 —— 调试节点绝不能把流程带崩。"""
    try:
        parts = []
        text = (node.get("text") or "").strip()
        if text:
            parts.append(_fmt_text(c, text))
        target = node.get("target", "none")

        if target == "var":
            var = (node.get("var_name") or "").strip()
            v = flow_var_get(c, var) if var else None
            parts.append(f"变量 {var}: 没这个值（还没读到？）" if v is None
                         else f"变量 {var} = {v}")
        elif target == "template":
            cat, name = node.get("category", ""), node.get("name", "")
            entry = template_manager.find_template(cat, name)
            tpl = template_manager.load_template_image(entry) if entry else None
            if entry is None or tpl is None or c.frame is None:
                parts.append(f"模板 {cat}/{name}: 不存在或无画面")
            else:
                hit = vision.match_template(c.frame, tpl, entry["threshold"],
                                            roi=entry.get("roi"),
                                            rgb_threshold=entry.get("rgb_threshold"),
                                            debug=True)
                if hit and hit.get("matched"):
                    parts.append(f"模板 {cat}/{name}: ✓({hit['x']},{hit['y']}) "
                                 f"分{hit['score']:.3f}")
                else:
                    score = hit["score"] if hit else 0.0
                    parts.append(f"模板 {cat}/{name}: ✗最高分{score:.3f}"
                                 f"（阈值{entry['threshold']}）")
        elif target == "color":
            x, y = int(node.get("tap_x", node.get("x", 0))), \
                int(node.get("tap_y", node.get("y", 0)))
            if c.frame is None or not (0 <= y < c.frame.shape[0]
                                       and 0 <= x < c.frame.shape[1]):
                parts.append(f"坐标({x},{y}): 无画面或越界")
            else:
                b, g, r = (int(v) for v in c.frame[y, x, :3])
                parts.append(f"({x},{y}) #{r:02X}{g:02X}{b:02X} RGB({r},{g},{b})")

        msg = " | ".join(parts) if parts else "（空打印）"
        level = node.get("level", "INFO")
        if c.log is not None:
            c.log(msg, level)
        else:
            print(f"[debug_print] {msg}")
    except Exception as e:
        if c.log is not None:
            c.log(f"打印检测异常（已忽略）：{e}", "WARN")


def _compute(c, node: dict):
    """计算赋值（compute 节点）：把算术表达式的值写进流程变量。

    两条链：
      ① text = _fmt_text(c, node["expr"])  先展开 {变量名} / {账号.列名} 占位符；
      ② val = eval_expr(text, lookup)      再按白名单算术求值（Name 走流程变量）。
    成功 → flow_var_set(c, var_name, val) + 一行日志（值 + 原式）。
    失败（表达式非法/类型不匹配/除零/变量名空）→ 只 WARN 点名，**跳过继续**，
    绝不拦路、绝不进败口 —— 与 _debug_print / _save_assets 同纪律（顺路算个数，
    算不出来只该留日志、不该把挂机带崩）。
    """
    var = str(node.get("var_name", "") or "").strip()
    raw = str(node.get("expr", "") or "")
    if not var:
        if c.log is not None:
            c.log("计算：没填变量名，跳过（不写空名变量）", "WARN")
        return
    try:
        text = _fmt_text(c, raw)                     # {账号.列} 等占位符先展开
        val = eval_expr(text, lambda n: flow_var_get(c, n))
    except Exception as e:
        # ExprError 或任何意外都归此处：表达式与原因都点名，便于排查
        if c.log is not None:
            c.log(f"计算 {var}：表达式「{raw}」算不出（{e}），已跳过", "WARN")
        else:
            print(f"[compute] 计算 {var} 失败：{e}")
        return
    flow_var_set(c, var, val)
    if c.log is not None:
        c.log(f"计算 {var} = {val}（式 {raw}）")


def _subflow_params(node: dict) -> list:
    """子流程节点的「调用绑定」形参表（F2）：节点键 params = 列表，每项
    {"name": 变量名, "expr": 实参表达式}。兼容纯字符串行（"名字=表达式"，
    离线手搭流程/AI 直写 JSON 时省点结构）：无 "=" 时整串当变量名、expr 空。
    非 list / 项非 dict / 非法行一律跳过（坏一行不连累整表，同 stop_on 纪律）；
    无 params 键 = 老节点 = 空表（返回 []，零污染）。"""
    raw = node.get("params")
    if not isinstance(raw, list):
        return []
    out = []
    for r in raw:
        if isinstance(r, dict):
            out.append(r)      # dict 行 = 存储本尊（编辑器要求身份稳定，不复制）
        elif isinstance(r, str):
            name, _, expr = r.partition("=")
            out.append({"name": name.strip(), "expr": expr.strip()})
    return out


def _apply_subflow_params(c, label: str, params: list):
    """子流程调用绑定（F2）：进入子流程前，把调用方登记的形参逐个求值写进流程变量。

    params 每项 {"name": 变量名, "expr": 实参表达式}。形参名即流程变量名，
    子流程内部照常 {变量名} / 变量比较引用（全局扁平，父子共用同一 ctx）。
      ① text = _fmt_text(c, expr)               先展开 {变量名}/{账号.列名} 占位符；
      ② val  = eval_expr(text, lambda n: flow_var_get(c, n))  再按白名单算术求值。
    失败（形参名空 / 表达式非法/类型不匹配/除零）→ 只 WARN 点名并跳过该项，
    绝不拦路（与 compute 节点同纪律）。无 params / 空表 = 无操作，存量节点零污染。

    只做「进入时赋值」，不做「退出时恢复」：赋值 = 一次同步落 ctx.extra，
    等价于用户手摆一个计算节点；restore 需跨拍保存旧值并在子流程终态回调里还原，
    会侵入 SubFlowStep 生命周期，收益不抵复杂度（设计层已说明取舍）。
    """
    for p in params or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name", "") or "").strip()
        raw = str(p.get("expr", "") or "")
        if not name:
            if c.log is not None:
                c.log(f"子流程「{label}」参数没填名字，跳过该项", "WARN")
            continue
        try:
            text = _fmt_text(c, raw)                      # {账号.列} 等占位符先展开
            val = eval_expr(text, lambda n: flow_var_get(c, n))
        except Exception as e:
            # ExprError 或任何意外都归此处：形参名与原因都点名，便于排查
            if c.log is not None:
                c.log(f"子流程「{label}」参数 {name}：实参「{raw}」算不出（{e}），"
                      f"已跳过该项", "WARN")
            continue
        flow_var_set(c, name, val)
        if c.log is not None:
            c.log(f"子流程「{label}」参数 {name} = {val}（式 {raw}）")


def _tpl_missing(cat: str, name: str) -> bool:
    """
    节点引用的模板是否缺图。name 为空 = 用户还没选模板，不算缺（编译成原步骤，
    运行时自然 no-op）。跨目录逃生口 cat="" 由 has_template 内部按名字全库找。

    缺图降级（bug 修复）：以前 flow 缺图有 4 种静默失败——click_template 空点判
    成功、wait_template exists=False 秒过、exists=True 空转到超时再 failed 自愈、
    branch cond 永走 false 分支。全都不报错，日志一个字的没有，是最难排查的。
    这里在编译期逐节点查，缺的直接换成 WARN LogStep 跳过，让"点不到图"可见。
    """
    return bool(name) and not template_manager.has_template(cat, name)


def _missing_step(label: str, cat: str, name: str, scope: str) -> LogStep:
    """缺图统一降级成一条 WARN 日志步骤（scope = 这步原本要干什么）。"""
    return LogStep(label, f"模板「{cat}/{name}」不存在，{scope}（先去采集该图）", "WARN")


def _make_fatal_exit_cb(step_type: str):
    """生成超时退出程序的回调：打 ERROR 日志后抛 FlowTimeoutExit。
    step_type 用于日志前缀（如"等模板"、"点模板"）。"""
    def fatal_cb(ctx, step):
        if ctx is not None and getattr(ctx, "log", None) is not None:
            ctx.log(f"{step_type}「{step.name}」超时（{step.timeout}秒），直接退出程序", "ERROR")
        # 超时退出恰是"没点上"的第一现场，旧版明示不留证（可排查性审计 ②）：
        # 补一张失败截图，与 failed 路径同通道同函数，日志点名截图路径。
        try:
            from bot.fsm import save_fail_capture
            path = save_fail_capture(ctx, "flow", step.name)
            if ctx is not None and getattr(ctx, "log", None) is not None:
                ctx.log(f"留证截图：{path}", "ERROR")
        except Exception:
            pass    # 留证失败不拦退出主语义
        raise FlowTimeoutExit("流程超时，退出程序")
    return fatal_cb


# 会真返回 "failed" 的动作类型（= 编辑器允许拉「败」口出线的类型）。
# 排除理由逐个说清：click/swipe/sleep/debug_print 无门无校验恒 ok；
# stop_flow 恒 ABORT/FLOW_END/LOOP_CONTINUE（不返 failed）；branch 必走 Y/N 之一；subflow 的败口已有自己的 false 口；
# popup_zone 独立节点已下线（载入即摘除，见 _drop_popup_zones），
# 循环体「清弹窗」行是顺序行走 steps 通道，不查此表。
# multi_scan 会真 failed：零命中门超时按配置判败 / 「进一个」返回后等不回列表页。
FAILABLE_TYPES = ("click_template", "wait_template", "server_pick", "multi_scan")


def _action_step(node: dict, retries: int = 1, has_fail: bool = False):
    """动作节点 → 单个 Step。返回 Step 或 None（未知类型）。
    retries：引擎合格重试次数。图语义（GraphStep 场景）传 0——图失败会整体
    failed 交给 FSM 恢复链，不需要单步重试退避（详见 _make_build_step）。
    has_fail：True = 该节点在图上接了「败」口出线（由 _compile_graph_impl 扫边
    算出传入）。接败口 = 用户声明"这一步允许失败、失败另走一条线"，编译层据此
    不再注入「超时直接退出程序」回调——不然异常先穿透出去，引擎里的败口传播
    永远轮不到执行（接了败口却照样硬退出 = 最难查的静默违背）。"""
    t = node.get("type", "")
    label = node.get("label", "")

    if t == "click_template":
        cat, name = node.get("category", ""), node.get("name", "")
        # 命中中心 + 像素偏移：图标在左、真正要点的是旁边文字/区域时用
        odx, ody = int(node.get("offset_x", 0)), int(node.get("offset_y", 0))
        # 节点自带搜索区域 = 覆盖模板账本那一份（没框 = 跟账本走，老节点零影响）
        roi = node.get("roi") or None
        # 落点抖动半径区间 [min,max]（px），每次落手现掷（见 _jit_range）
        jmn, jmx = _jit_range(node)
        if _tpl_missing(cat, name):
            return _missing_step(label or f"点模板 {name}", cat, name, "跳过本步点击")
        # 前置门：等目标图出现再点。缺省开（老流程不重编也自动获得修复）——
        # 以前图没加载出来 tap_template 找不到图会静默空放、本步照样 ok，
        # 流程带着没点的按钮往下走，是"点了但没点上"这类玄学 bug 的源头。
        # wait_before=False 保留旧语义（落手一次就走，不看图在不在）。
        step_name = label or f"点模板 {name}"
        # 「点击后加延迟」：点完等常驻延时（config general.post_click_delay）再收口。
        # GraphStep 每节点只能挂一个 Step，用 _SequenceStep 把 [动作, 等待] 串成一步；
        # 没勾 = None = 单动作步，老节点零影响。
        act = _after_click_wait(node)
        if bool(node.get("all_hits")):
            # 点全部（2026-09）：同屏 N 处同一个图一个节点逐个点完。门/超时三档
            # 复用本节点已有字段（wait_before/wait_timeout/on_timeout），不另造
            # 参数；接败口让位 fatal 的口径与普通点模板逐字一致。
            skip_on_timeout = node.get("on_timeout", "fail") == "continue"
            gt = (float(node.get("wait_timeout", 8))
                  if bool(node.get("wait_before", True)) else 0.0)
            fatal_cb = (_make_fatal_exit_cb("点模板")
                        if gt > 0 and not skip_on_timeout and not has_fail
                        else None)
            step = TapAllStep(
                step_name, cat, name,
                offset=(odx, ody), jitter=(jmn, jmx), roi=roi,
                max_hits=int(node.get("all_hits_max", 10) or 10),
                gap_secs=float(node.get("all_hits_gap", 0.3) or 0.3),
                gate_timeout=gt,
                ok_on_timeout=skip_on_timeout,
                on_timeout_log=_warn_tap_all_log if (gt > 0 and skip_on_timeout)
                else None,
                fatal_cb=fatal_cb)
            return _SequenceStep(step_name, [step, act]) if act is not None else step
        if bool(node.get("wait_before", True)):
            gate_timeout = float(node.get("wait_timeout", 8))
            on_timeout = node.get("on_timeout", "fail")
            skip_on_timeout = on_timeout == "continue"
            # 超时行为只剩两档：continue=跳过本步继续；其余（含存量 fail 值，
            # 语义已与 exit 合并）= 打 ERROR + 抛异常退出程序（不截图留证）。
            # 例外：接了败口（has_fail）→ 不注入 fatal，超时返回 failed 交引擎
            # 走败口连线 —— 抛异常会穿透，败口永远轮不到（见 _action_step docstring）
            fatal_cb = (_make_fatal_exit_cb("点模板")
                        if not skip_on_timeout and not has_fail else None)
            step = ActionStep(
                step_name,
                action=lambda c, a=cat, b=name, dx=odx, dy=ody, jt=(jmn, jmx), r=roi: tap_template(c, a, b, dx, dy, jt, roi=r),
                retries=retries,
                gate=lambda c, a=cat, b=name, r=roi: template_present(c, a, b, roi=r),
                gate_timeout=gate_timeout,
                ok_on_gate_timeout=skip_on_timeout,
                on_gate_timeout_log=_warn_gate_log if skip_on_timeout else None,
                on_gate_timeout_fatal=fatal_cb,
            )
        else:
            step = ActionStep(
                step_name,
                action=lambda c, a=cat, b=name, dx=odx, dy=ody, jt=(jmn, jmx), r=roi: tap_template(c, a, b, dx, dy, jt, roi=r),
                retries=retries,
            )
        return _SequenceStep(step_name, [step, act]) if act is not None else step
    if t == "click":
        # v2 图：点击坐标存 tap_x/tap_y（x/y 是画布坐标）；兼容旧节点回落 x/y
        # F3：坐标可来自流程变量/表达式（新键 coord 或 x_expr/y_expr，二者运行期
        # 现算，见 _resolve_click_xy）。带新键的节点编成 CompiledClickStep——只有
        # 存量节点（无新键）走回老 ActionStep，行为逐字不变。
        if (str(node.get("coord", "") or "").strip()
                or str(node.get("x_expr", "") or "").strip()
                or str(node.get("y_expr", "") or "").strip()):
            return CompiledClickStep(label or "点坐标(变量)", node,
                                     retries=retries,
                                     on_coord_warn=_warn_click_var_log)
        x, y = int(node.get("tap_x", node.get("x", 0))), \
            int(node.get("tap_y", node.get("y", 0)))
        jmn, jmx = _jit_range(node)
        return ActionStep(
            label or f"点坐标 ({x},{y})",
            action=lambda c, a=x, b=y, jt=(jmn, jmx): _tap_jitter(c, a, b, jt),
            retries=retries,
        )
    if t == "swipe":
        pts = node.get("swipe") or [node.get("x1", 0), node.get("y1", 0),
                                    node.get("x2", 0), node.get("y2", 0)]
        ms = int(node.get("ms", 500))
        x1, y1, x2, y2 = (int(v) for v in pts[:4])
        return ActionStep(
            label or f"滑动 ({x1},{y1})→({x2},{y2})",
            action=lambda c, a=x1, b=y1, d=x2, e=y2, m=ms: swipe(c, a, b, d, e, m),
            retries=retries,
        )
    if t == "debug_print":
        # verify=None：打印永远成功，纯观察不拦路
        return ActionStep(
            label or "打印检测",
            action=lambda c, n=node: _debug_print(c, n),
            retries=retries,
        )
    if t == "save_assets":
        # 同打印纪律：读数入库永远成功，没读到/没绑号只 WARN，不拦路不进败口
        return ActionStep(
            label or "资产入库",
            action=lambda c, n=node: _save_assets(c, n),
            retries=retries,
        )
    if t == "compute":
        # 计算赋值（F1）：算不出只 WARN 跳过，无门无校验恒 ok，不进败口
        return ActionStep(
            label or "计算",
            action=lambda c, n=node: _compute(c, n),
            retries=retries,
        )
    if t == "sleep":
        sec = float(node.get("seconds", node.get("sleep", 1.0)))
        # 随机上限：实际等待在 [sec, sec_max] 掷。不配 / 配小了 = 固定 sec 秒，
        # 与老流程零差异；配了才反"每步卡整秒"的秒表味。
        sec_max = node.get("seconds_max")
        sec_max = float(sec_max) if sec_max and float(sec_max) > sec else None
        return WaitStep(label or f"等待 {sec}s", fixed_wait=sec,
                        fixed_wait_max=sec_max, retries=0)
    if t == "wait_template":
        cat, name = node.get("category", ""), node.get("name", "")
        if _tpl_missing(cat, name):
            return _missing_step(label or f"等模板 {name}", cat, name, "跳过本步等待")
        exists = bool(node.get("exists", True))
        timeout = float(node.get("timeout", 8))    # 0 = 不限时（图不出现一直等）
        on_timeout = node.get("on_timeout", "fail")
        # 超时行为只剩两档：continue=跳过继续；其余（含存量 fail 值，语义已与
        # exit 合并）= 直接退出程序（打 ERROR + 抛异常穿透恢复链）；
        # 接了败口（has_fail）则不注入 fatal，超时返回 failed 走败口连线
        skip_on_timeout = on_timeout == "continue"
        fatal_cb = (_make_fatal_exit_cb("等模板")
                    if not skip_on_timeout and not has_fail else None)
        return WaitStep(
            label or f"等模板 {name}",
            template=(cat, name),
            roi=node.get("roi") or None,   # 节点框的区域覆盖账本（没框=跟账本走）
            timeout=timeout,
            exists=exists,
            retries=retries,
            ok_on_timeout=skip_on_timeout,
            on_timeout_log=_warn_timeout_log if skip_on_timeout else None,
            on_timeout_fatal=fatal_cb,
            confirm_secs=float(node.get("confirm_secs", 0.6)),
        )
    if t == "server_pick":
        from core import ocr
        # 识字取物（type id 沿用 server_pick = 存量流程零破坏）：认出目标文字后
        # 按用法分流 —— 点击 / 抠数字进流程变量 / 记坐标进流程变量。
        # 纯 OCR 认字（点阵字库能力已整体删除）。老流程里残留的
        # method / lib 字段不再读取，用户再存一次流程自然清干净。
        if not ocr.available():
            return LogStep(label or "点文字",
                           "OCR 依赖未安装（pip install rapidocr onnxruntime），跳过本步",
                           "WARN")
        mode = node.get("mode", "click")
        legacy_pair = mode == "pick_pair"
        if legacy_pair:
            # 旧「选区选服」两段用法已删（等价摆法：点文字 → sleep → 点文字，
            # 编辑器载入流程时 FlowPanel._splice_pick_pair 会自动换算成三节点）。
            # 走到这里说明是手改 JSON / 别的版本导进来的流程：降级成第一步
            # 「点文字」并打一条 WARN 点名，绝不静默吞掉第二阶段。
            mode = "click"
        dflt = _SP_DEFAULT_LABEL.get(mode, "点文字")
        inner = ServerPickStep(
            label or dflt,
            text=str(node.get("text", "") or ""),
            timeout=float(node.get("timeout", 15)),
            on_timeout=node.get("on_timeout", "exit"),
            min_sim=float(node.get("min_sim", 60)) / 100.0,
            min_interval=float(node.get("min_interval", 0.3)),
            roi=tuple(node["roi"]) if node.get("roi") else None,
            mode=mode,
            var_name=str(node.get("var_name", "") or ""),
            offset=(int(node.get("offset_x", 0) or 0),
                    int(node.get("offset_y", 0) or 0)),
            jitter=_jit_range(node),
            scroll_dir=node.get("scroll_dir", "up"),
            scroll_px=int(node.get("scroll_px", 300) or 300),
            scroll_max=int(node.get("scroll_max", 5) or 0),
            scroll_ms=int(node.get("scroll_ms", 400) or 400),
            scroll_roi=list(node["scroll_roi"]) if node.get("scroll_roi") else None,
            settle_secs=float(node.get("scroll_settle", 0.8) or 0.8),
            stable_secs=float(node.get("stable_secs", 0) or 0),
            # 接败口同样让位：认不出目标超时后返回 failed 走败口线而非硬退出；
            # 「没登记目标服」的 failed 本来就走败口（ServerPickStep 直接返回）
            fatal_cb=None if has_fail else _make_fatal_exit_cb(dflt),
        )
        if legacy_pair:
            return _SequenceStep(label or "选区选服",
                                 [LogStep("选区选服",
                                          "旧「选区选服」两段用法已删除：本节点只认"
                                          "第一步（区名），第二块区域失效。在流程编辑器"
                                          "里打开该流程会自动换算成 [点文字→等待→点文字]",
                                          "WARN"),
                                  inner])
        return inner
    if t == "signal":
        # 「发信号」节点（2026-10-06）：往运行信箱投一条命名信号后直通。
        # 只发不收：掐谁由外层「循环/遍历全部」面板登记的
        # 「收到即结束的信号」名单决定；谁先对上名单谁消费销账，外层不陪葬。
        return SignalStep(label or "发信号", node.get("name", ""))
    if t == "stop_flow":
        # 「结束流程」节点：范围可切换（节点属性「结束范围」）——默认 local =
        # 只结束所在层（子流程/循环就地吸收，主线续跑），global = ABORT 穿透
        # 一切直接停整条任务，continue = 跳过本轮（仅循环体里有意义，等价循环的
        # continue）。老节点没这键按 local（顶层行为与从前一致）。非法值回落 local。
        scope = node.get("stop_scope", "local")
        if scope not in ("local", "global", "continue"):
            scope = "local"
        return StopFlowStep(label or "结束流程", scope=scope)
    if t == "popup_zone":
        # 循环体「清弹窗」行（独立弹窗区节点已下线，图上到不了这里）：
        # modules 行自带缺图处理（PopupZoneStep 第一拍 WARN 点名死行），
        # 不在编译期整节点拆掉 —— 少一行图不该连累其余能用的行
        return PopupZoneStep(
            label or "弹窗区",
            [dict(m) for m in (node.get("modules") or []) if isinstance(m, dict)],
            silent_secs=float(node.get("silent_secs", 5)),
            total_timeout=float(node.get("timeout", 180)),
        )
    return None


class SubFlowStep(Step):
    """
    子流程节点 = 加强版点模板：先等门（可选），再跑整条子流程，按结果分两个出口。

    三段生命周期，tick 返回：
      "running"  仍在等门 / 子流程未走完
      "ok"       成功出口（门过 + 子流程全跑完）
      "skipped"  失败出口 —— 门超时按 skip 判，或子流程内部失败。
                 ⚠ 这里绝不返回 "failed"：failed 会让 GraphStep 直接把整图
                 判死，子流程就失去"失败另走一条线"的意义。失败语义由外层
                 节点转成 false 口传播，交给用户在画布上决定失败走哪。

    gate：进入前置门（同 ActionStep 的 gate，ctx->bool）。gate_timeout 秒等不到
          → on_gate_timeout=="fail" 走失败出口，否则走成功出口（"等不到也照跑"）。
    body：子流程编译出的步骤链，内部套一个 FSM 顺序推进 —— 步骤失败先由
          FSM 截图留证，再判子流程失败走 false 口。
          body 里的动作步（如 ActionStep 点模板）落手一次返回 "ok" 即完成，
          子 FSM 前进到下一步；不会重复落手。
    子流程是「结束流程」的边界：体内范围=当前流程（FLOW_END）在这里吸收，
    子流程算跑完走 Y 口、主线续跑；范围=整条流程（ABORT）才继续上抛穿透。
    范围=跳过本轮（LOOP_CONTINUE）：子流程不是循环，吸收不了 —— 原样上抛，
    外层循环（若有）接住，一路到顶按"任务正常收尾"处理（绝不死循环）。

    on_gate_timeout_log：门超时打一行 WARN 的回调 (ctx, step)（引擎无 ctx.log 通道）。
    """

    def __init__(self, name, body: list, gate=None, gate_timeout: float = 8,
                 on_gate_timeout: str = "fail", on_gate_timeout_log=None,
                 bg_watch: bool = True, params=None):
        super().__init__(name, timeout=0, retries=0)
        # bg_watch=False：本步运行期间 GraphStep 停掉本图 watch 哨兵的扫描
        # （主流程监视不管子流程，一切按子流程自己的逻辑走，用户拍板 2026-10-05）
        self.suppresses_watch = not bg_watch
        self.body = body
        self.gate = gate
        self.gate_timeout = gate_timeout
        self.on_gate_timeout = on_gate_timeout
        self.on_gate_timeout_log = on_gate_timeout_log
        # 调用绑定（F2）：进入前逐个求值写流程变量；空表 = 无操作（老节点零污染）
        self.params = [p for p in (params or []) if isinstance(p, dict)]
        self._phase = "gate" if gate is not None else "run"
        self._gate_start = None
        self._gate_passed = False
        self._params_applied = False   # 本轮形参是否已落值（跨拍幂等，绝不多写）
        self._inner = None   # 惰性建的子 FSM（bot.fsm.FSM），reset 时清空重建

    def reset(self):
        super().reset()
        self._phase = "gate" if self.gate is not None else "run"
        self._gate_start = None
        self._gate_passed = False
        self._params_applied = False   # 按 GraphStep 每轮起跑纪律复位
        self._inner = None

    def tick(self, ctx):
        # ── 阶段 1：前置门（等模板出现 / 等够固定延时）──
        if self._phase == "gate" and not self._gate_passed:
            if self._gate_start is None:
                self._gate_start = time.monotonic()
            if self.gate(ctx):
                self._gate_passed = True
                self._phase = "run"
            else:
                if 0 < self.gate_timeout <= time.monotonic() - self._gate_start:
                    # "continue"=等不到也算过（直通成功口）；"fail"=走失败口(skipped)
                    return resolve_step_timeout(
                        ctx, self, self.on_gate_timeout == "continue",
                        self.on_gate_timeout_log, None, fail_result="skipped")
                return "running"
        # ── 阶段 2：跑子流程链，子 FSM 推进 ──
        # 进门后、开跑子流程前落一次形参（含"不等门/wait 关"的直连子流程）：
        # 幂等闸门保证跨拍只写一次；无 params = 一句话不写（存量节点零污染）。
        if not self._params_applied:
            _apply_subflow_params(ctx, self.name, self.params)
            self._params_applied = True
        if self._inner is None:
            # body 编译产物跨轮复用（如循环体内含子流程行）：复位+重建见 restart_chain
            self._inner = restart_chain(self.body, ctx, task_name=self.name)
        res = self._inner.tick()
        if res == "done":
            return "ok"       # 成功出口
        if res == "failed":
            return "skipped"  # 失败出口（子链自愈也没救回来）
        if res == FLOW_END:
            return "ok"       # 子链「结束流程」(当前流程)：就地吸收 = 子流程算跑完，走 Y 口主线续跑
        if res == ABORT:
            return ABORT      # 子链「结束流程」(整条流程)：全局急停穿透边界，外层收到照停
        if res == LOOP_CONTINUE:
            # 子链「结束流程」(跳过本轮)：子流程不是循环，吸收不了 —— 原样上抛，
            # 让外层循环（若有）接住；一路到顶则按"任务正常收尾"处理（绝不死循环）。
            return LOOP_CONTINUE
        return "running"


class WatchStep(Step):
    """
    并行监视哨兵（watch 节点编译产物）：常驻全局的「识别到哪个弹窗 → 跑哪条子流程」。

    与清弹窗行（PopupZoneStep）的本质区别：清弹窗行是循环体里的一站，清完就放行；
    哨兵永远在岗，主线照跑自己的 —— 所以它绝不返回 ok/failed（返回 ok 会让
    GraphStep 把它收口、不再监视；failed 会判死整图），只在有结论的拍返回
    "running"。每拍归谁：GraphStep 收敛完主线后跑哨兵一拍（扫描不落手，
    每拍必扫）；一旦命中起跑子链，子链期间主线整条暂停（动作与纯评估
    全停，"弹窗来了先办弹窗"，用户拍板 2026-09-25 改一刀切），收口自动解除、主线续跑。

    rows 每行 = {"category","name","kind","flow_name","tap_on_hit"}，扫描顺序 = 优先级。
    scan_mode 两型（用户拍板 2026-10-05，管"命中办完后下一轮从哪扫"）：
    "restart"（缺省，老行为逐字不变）= 每轮都从第①行重扫，高优先行永远压着
    低优先行；"continue" = 从刚命中那行的下一行往后轮转着扫，谁都不被饿死。
    kind 型："tap"=命中直接点这个图标一下（纯关弹窗，不跑子流程）；
    "ban"=封号停用（认出即抛 AccountBannedExit 收工，worker 侧标记封号，不点不跑链）；
    "finger"=动态手指（2026-10-11 新增，用户解冻后加）：**不认图**，每拍用
    vision.detect_finger 现扫"白块+黑描边"的手指客体（对缩放/旋转/只露半边免疫，
    替掉"每角度每大小各存一张手指模板"的老办法），检出即点推算目标（指尖 +
    extend_px×指向）；无 category/name、不跑子流程。其余/缺省="subflow"=命中跑
    flow_name 子流程（tap_on_hit 勾了则起跑前先点图标）。
    行状态（冷却/失败计数/停用标记）与本类一一对应，绝不合进 row：row 是编辑
    数据，会被序列化进 custom_flows。
    - 命中且冷却已过 → 跑该行子流程（内部 FSM，同 SubFlowStep 套路），
      跑完进 cooldown_secs 冷却（老弹窗拦截实测教训：关不掉的图/误命中的
      图若每拍连点 = 劫持整个循环）。
    - 收口拍就地续扫（用户拍板）：子链跑完的当拍不先交还主线，顺手再扫一遍
      监视行——还有弹窗就同拍直接起新链。收益：takeover 全程不断档，主线的冻结闸不经历
      "解除→下拍又冻结" 的抖动，连环弹窗少等一拍；代价为零 —— 扫描不落手，收口拍本来就不归主线动作。刚收口
      的行已进 cooldown_secs 冷却，同拍不会自我重触发（顽固弹窗仍由连败停用
      兜底），扫空才返回、交还落手权。
    - tap_on_hit=True（命中即点）：起跑子链时自动往开头插一步"点这个触发图标"，
      子流程体不用再自己配"点模板"节点进这个页面 —— 认图判据与入口点击合一，
      哨兵调子流程和主线 subflow（点模板→子流程）就此对称。
    - 同一条子流程连续 fail_limit 次没搞定（failed）→ 停用该行 + WARN
      （用户拍板）：防一个关不掉的弹窗把整条线反复拖死饿死主线。
    - 行内子链命中「结束流程」(ABORT/FLOW_END/LOOP_CONTINUE)：只算该行干完一轮
      （进冷却继续监视），绝不上抛 —— 哨兵替主线关弹窗时，不能把主线整个 abort 掉。
    主线全部收口 = 整图完成（GraphStep 把哨兵节点排除在 done 判定外）：
    哨兵与主线同生共死，不拖着任务链永远不退。行全死光时它只是每拍空转，
    看门狗不把它算进展 —— 主线该判卡死还照判。
    """

    def __init__(self, name, rows: list, bodies: list, cooldown_secs: float = 5.0,
                 fail_limit: int = 3, scan_ttl: float = 0.5,
                 scan_mode: str = "restart", stats_key=None):
        super().__init__(name, timeout=0, retries=0)
        # 命中统计登记键 (流程名, 节点id)：None = 不记（timer 行/自检构造）。
        # 行命中一笔进 bot/watch_stats 内存账本，编辑器显示「×N · 最近 X 前」
        self.stats_key = stats_key
        self.rows = rows or []
        self._continue_scan = (scan_mode == "continue")
        self._cursor = 0                  # continue 模式：下轮扫描起点
        self.bodies = list(bodies or [])      # 与 rows 对齐：None=编译期已判死
        self.cooldown = float(cooldown_secs)
        self.fail_limit = max(1, int(fail_limit))
        # 未命中复读间隔（性能审计 P1-⑨）：弹窗不在的拍不必每 0.25s 全屏重扫，
        # miss 后 scan_ttl 秒内跳过该行。代价=弹窗出现最晚晚半拍被看到，
        # 对"关弹窗"场景无感；收益=10 开时哨兵全屏扫开销直接对半砍。
        self.scan_ttl = float(scan_ttl)
        self._scan_next = [0.0] * len(self.rows)  # 各行下次允许真扫的时刻
        self._cool = [0.0] * len(self.rows)   # 冷却截止时刻
        self._fails = [0] * len(self.rows)    # 连续失败计数（成功清零）
        # 停用标记（含编译期死行：body=None = 缺图/流程不存在/递归引用）
        self._off = [i >= len(self.bodies) or self.bodies[i] is None
                     for i in range(len(self.rows))]
        self._pending = None                  # (idx, 子FSM)：哨兵链跨拍续跑
        self._dead_warned = False             # 编译期死行第一拍点名一次
        self.tapped = False   # 本拍是否动了手（GraphStep 哨兵通道读它补预算）

    def reset(self):
        super().reset()
        self._pending = None   # 行状态（冷却/停用）刻意保留：重跑不复活死行
        self._cursor = 0

    def tick(self, ctx):
        now = time.monotonic()
        self.tapped = False
        if not self._dead_warned:
            # 编译期判死的行第一拍一次性点名（缺图拆穿纪律同 PopupZoneStep，
            # 免得用户对着"这行监视怎么不生效"抓瞎）
            self._dead_warned = True
            if ctx.log is not None:
                dead = [(self.rows[i].get("name") or self.rows[i].get("flow_name")
                         or "（没配置）") for i in range(len(self.rows))
                        if self._off[i]]
                if dead:
                    ctx.log(f"并行监视「{self.name}」失效行：{'、'.join(dead)}"
                            f"（缺图/流程不存在/递归引用，去流程编辑器修）", "WARN")
        # ── 收口续扫（类注释拍板）：子链收口当拍不 return，清完账往下走扫描
        # ——下面 for 就是续扫：还有命中同拍起新链（takeover 不断档），扫空
        # 才走到尾部返回、交还落手权。起新链只挂链不落手，与首发拍同构。──
        if self._pending is not None:
            idx, inner = self._pending
            row = self.rows[idx]
            res = inner.tick()
            if res == "running":
                self.tapped = True   # 续跑：子链本拍大概率落手（闸门语义同弹窗区）
                return "running"
            self._pending = None
            if res == "failed":
                self._fails[idx] += 1
                if self._fails[idx] >= self.fail_limit:
                    self._off[idx] = True
                    if ctx.log is not None:
                        ctx.log(f"并行监视「{self.name}」行「{row.get('name')}→"
                                f"{row.get('flow_name')}」连续 {self._fails[idx]} 次"
                                f"没搞定，停用该行（本流程内不再监视，日志见 "
                                f"logs/fail_capture 截图）", "WARN")
            else:
                # done / ABORT / FLOW_END / LOOP_CONTINUE（行内「结束流程」只算
                # 干完一轮，绝不上抛）：成功清零
                self._fails[idx] = 0
            self._cool[idx] = now + self.cooldown
            self.tapped = True       # 收口拍动作归哨兵，续扫/让路判定都由此闸门
        _n = len(self.rows)
        # continue 模式从游标轮转起扫，restart（缺省）恒从第①行 —— 游标只在
        # 命中拍推进（下面两处 cursor 赋值），空扫描拍不动它
        _start = (self._cursor % _n) if (self._continue_scan and _n) else 0
        for _k in range(_n):
            i = (_start + _k) % _n
            row = self.rows[i]
            if self._off[i] or now < self._cool[i] or now < self._scan_next[i]:
                continue
            # ── 动态手指行（2026-10-11 新增，用户解冻后加）：与其余行最大区别
            # 是**不认图**——它没有 category/name，判据是 vision.detect_finger
            # 现扫"白块+黑描边"的手指客体（对缩放/旋转/只露半边免疫），检出即点
            # 推算目标（指尖 + extend_px×指向）。必须在 nm 空判与 template_present
            # 之前拦截：它没图可认，走那两步会被 continue 直接跳过（永远不生效）。
            # 每拍都扫（用户拍板：不加"何时启用"限定）——手指出现即点。
            if row.get("kind") == "finger":
                self._cool[i] = now + self.cooldown
                finger = None
                try:
                    finger = vision.detect_finger(
                        ctx.frame,
                        white_thresh=int(row.get("white_thresh", 200) or 200),
                        dark_thresh=int(row.get("dark_thresh", 70) or 70),
                        min_area=int(row.get("min_area", 800) or 800),
                        max_area=int(row.get("max_area", 20000) or 20000))
                except Exception:
                    finger = None
                if finger is None:
                    self._scan_next[i] = now + self.scan_ttl   # 没检出→短冷却再扫
                    continue
                watch_stats.record(self.stats_key, i)
                # 落点两路：勾了「自动外延」就从指尖沿指向扫到目标内部
                # （extend_click_auto 找连续高饱和区间的饱和度加权中心，指尖离目标
                # 距离每次不同都自适应）；不勾走老路 extend_px 固定外延。
                if row.get("auto_extend"):
                    px, py = vision.extend_click_auto(ctx.frame, finger)
                else:
                    ext = float(row.get("extend_px", 0) or 0)
                    px, py = vision.extend_click(finger, ext)
                hit = False
                try:
                    ldplayer.adb_shell(ctx.ld_index, f"input tap {px} {py}")
                    hit = True
                except Exception:
                    hit = False
                self._fails[i] = 0 if hit else self._fails[i] + 1
                if self._fails[i] >= self.fail_limit:
                    self._off[i] = True
                    if ctx.log is not None:
                        ctx.log(f"并行监视「{self.name}」手指行连点 {self._fails[i]} "
                                f"次没点上，停用该行（本流程内不再监视）", "WARN")
                if ctx.log is not None:
                    ctx.log(f"并行监视「{self.name}」检出动态手指（{finger['side']}入"
                            f"·指尖{finger['tip']}）→ tap({px},{py})"
                            + ("" if hit else "（点击失败）"), "INFO")
                if self._continue_scan:
                    self._cursor = (i + 1) % _n
                self.tapped = True
                return "running"
            cat, nm = row.get("category", ""), row.get("name", "")
            if not nm:
                continue
            if not template_present(ctx, cat, nm):
                # miss 进复读冷却（P1-⑨）：ttl 内该行不再真扫，见 __init__ 注释
                self._scan_next[i] = now + self.scan_ttl
                continue
            watch_stats.record(self.stats_key, i)   # 命中入账（内存账本，编辑器×N）
            if row.get("kind") == "ban":
                # 封号停用行：认出封号弹窗 = 这条线没什么可救的，当场收工。
                # 抛异常穿透（与 FlowTimeoutExit 同通道），不走 failed ——
                # 哨兵的结论不该经失败口传播，且留证+写库归 worker 统一做。
                if ctx.log is not None:
                    ctx.log(f"并行监视「{self.name}」命中封号弹窗「{nm}」，"
                            f"本实例收工并标记账号封号", "ERROR")
                raise AccountBannedExit(f"检测到封号弹窗「{nm}」")
            if row.get("kind") == "tap":
                # 命中即点行（纯关弹窗）：本拍点掉图标收工，不起链不跑子流程。
                # 与"起链跑一步 ActionStep"相比省一整拍、日志干净；落手即占本拍
                # 闸门（tapped），与子链起跑拍语义一致。点击失败（图刚消失的竞态）
                # 进短冷却，由连败停用兜底关不掉的顽固弹窗。
                # offset_x/y：相对命中中心的偏移（关闭按钮不在图正中央时用），
                # 与「点模板」节点同一套语义，编辑器「点选目标位算偏移」采出来。
                self._cool[i] = now + self.cooldown
                try:
                    hit = tap_template(ctx, cat, nm,
                                       int(row.get("offset_x", 0) or 0),
                                       int(row.get("offset_y", 0) or 0))
                except Exception:
                    hit = False
                self._fails[i] = 0 if hit else self._fails[i] + 1
                if self._fails[i] >= self.fail_limit:
                    self._off[i] = True
                    if ctx.log is not None:
                        ctx.log(f"并行监视「{self.name}」行「{nm}」连点 {self._fails[i]} "
                                f"次没点掉，停用该行（本流程内不再监视）", "WARN")
                if ctx.log is not None:
                    ctx.log(f"并行监视「{self.name}」命中「{nm}」，点掉关闭"
                            + ("" if hit else "（空点：图刚消失）"), "INFO")
                if self._continue_scan:
                    self._cursor = (i + 1) % _n   # 办完从下一行接着扫
                self.tapped = True
                return "running"
            chain = self.bodies[i]
            if row.get("tap_on_hit"):
                # 命中即点：子链开头预置一步"点这个触发图标"（tap_template
                # 点命中中心，模板设了固定点击坐标则点那里，全项目点击语义一致；
                # 行上采了偏移则中心+(dx,dy) 再点，与 tap 行同款）。
                # 起跑拍只挂链不落手，点击在下一拍这步推进时才发生 —— 与主线
                # 动作共拍零风险；图标若已消失则空点算过（弹窗自己没了，子流程
                # 首步自然空转）。入口点击不必再在子流程里配"点模板"节点。
                odx, ody = int(row.get("offset_x", 0) or 0), int(row.get("offset_y", 0) or 0)
                chain = [ActionStep(f"进入{nm}",
                                    action=lambda c, a=cat, b=nm, dx=odx, dy=ody:
                                    tap_template(c, a, b, dx, dy)
                                    )] + list(self.bodies[i])
            # body/chain 每轮复用：复位+重建纪律见 restart_chain
            self._pending = (i, restart_chain(chain, ctx, task_name=f"监视:{nm}"))
            if ctx.log is not None:
                ctx.log(f"并行监视「{self.name}」命中「{nm}」"
                        + ("，点入并跑子流程" if row.get("tap_on_hit")
                           else "，跑子流程")
                        + f"「{row.get('flow_name')}」", "INFO")
            if self._continue_scan:
                self._cursor = (i + 1) % _n   # 子链收口后的续扫也从下一行接着走
            self.tapped = True    # 起跑即占落手权：下一拍子链第一步多半要点屏
            return "running"
        return "running"          # 无事的一拍：不占落手，GraphStep 补预算


class TimerStep(WatchStep):
    """
    定时哨兵（timer 节点编译产物）：watch 的"看钟版" —— 行不是「认到图才跑」，
    而是「流程运行期间每隔 N 分钟自动跑一次子流程」（用户拍板：周期执行）。

    与 WatchStep 唯一差异 = 触发判据：时钟到期替代认图命中。跑完等下一个
    间隔周期再跑（不像 watch 跑完立刻重新监视）；子流程失败则下一拍重试
    （连败停用、成功清零、行内「结束流程」只收口该行 —— 这些全部原样继承，零改动）。
    其余（永不收口、拍尾通道、takeover、冷却、编译期死行、row 序列化纪律）
    与 watch 一字不差，所以直接 subclass，别再复制一份引擎半段。

    rows 每行 = {"interval_min","flow_name"}；扫描顺序无优先级含义（各自独立计时）。
    时钟起点在构造时：流程起跑 = 第一个周期从起跑时刻起算。
    """

    def __init__(self, name, rows: list, bodies: list, cooldown_secs: float = 5.0,
                 fail_limit: int = 3):
        super().__init__(name, rows, bodies, cooldown_secs, fail_limit)
        # 行间隔（分钟→秒）。钳 0.1 分下限：0 间隔 = 每拍连点，等于劫持循环。
        # ⚠ 不用 `or 5`：0 是 falsy 会被误换成 5，0 该走钳制不是走默认。
        def _iv(r):
            v = r.get("interval_min")
            return 5.0 if v is None or v == "" else max(0.1, float(v))
        self._interval_min = [_iv(r) for r in self.rows]
        # 首次触发等满一个间隔（流程起跑 = 构造时刻起算），不是开局秒触发
        now = time.monotonic()
        self._cool = [now + iv * 60.0 for iv in self._interval_min]

    def tick(self, ctx):
        now = time.monotonic()
        self.tapped = False
        if not self._dead_warned:
            self._dead_warned = True
            if ctx.log is not None:
                dead = [(self.rows[i].get("flow_name") or "（没配置）")
                        for i in range(len(self.rows)) if self._off[i]]
                if dead:
                    ctx.log(f"定时执行「{self.name}」失效行：{'、'.join(dead)}"
                            f"（流程不存在/递归引用，去流程编辑器修）", "WARN")
        if self._pending is not None:
            idx, inner = self._pending
            row = self.rows[idx]
            res = inner.tick()
            if res == "done":
                self._pending = None
                self._fails[idx] = 0
                # 核心差异：跑完 = 歇一个完整间隔，而不是 watch 的短冷却
                self._cool[idx] = now + self._interval(idx)
            elif res == "failed":
                self._pending = None
                self._fails[idx] += 1
                # 失败不烧掉整个周期：歇 cooldown_secs 就重试（watch 同款），
                # 连败到 fail_limit 停用该行
                self._cool[idx] = now + self.cooldown
                if self._fails[idx] >= self.fail_limit:
                    self._off[idx] = True
                    if ctx.log is not None:
                        ctx.log(f"定时执行「{self.name}」行「{row.get('flow_name')}」"
                                f"连续 {self._fails[idx]} 次没搞定，停用该行", "WARN")
            elif res in (ABORT, FLOW_END, LOOP_CONTINUE):
                self._pending = None
                self._fails[idx] = 0
                self._cool[idx] = now + self._interval(idx)
                # 行内「结束流程」只算跑完一轮，绝不上抛（继承 watch 纪律；
                # 三种范围都一样 —— 哨兵行本身就是边界）
            else:
                self.tapped = True
                return "running"
            self.tapped = True
            return "running"
        for i, row in enumerate(self.rows):
            if self._off[i] or now < self._cool[i]:
                continue
            fname = (row.get("flow_name") or "").strip()
            # 复位+重建纪律见 restart_chain（与 WatchStep 同款）
            self._pending = (i, restart_chain(self.bodies[i], ctx,
                                              task_name=f"定时:{fname}"))
            if ctx.log is not None:
                ctx.log(f"定时执行「{self.name}」到点，跑子流程「{fname}」"
                        f"（每 {self._interval_min[i]:g} 分钟一次）", "INFO")
            self.tapped = True
            return "running"
        return "running"

    def _interval(self, i: int) -> float:
        """第 i 行的间隔秒数（构造时换算好，读表即可）。"""
        return self._interval_min[i] * 60.0


_SP_DEFAULT_LABEL = {"click": "点文字", "number": "识文读数字",
                     "position": "读坐标", "roi_number": "读数字"}
# 旧 mode="pick_pair"（选区选服两段用法，已删）不在表里：ServerPickStep
# 构造时回落成 click，编译侧 _action_step 对残存节点先打 WARN，绝不静默变味。

# 游戏资源条常用单位缩写（w = 万的拼音首字母，游戏圈惯例）
_NUM_UNITS = {"万": 10**4, "亿": 10**8, "k": 10**3, "K": 10**3,
              "w": 10**4, "W": 10**4}


def parse_game_number(text: str) -> int | None:
    """游戏数值串 → 整数：认「1.17万 / 1.2亿 / 3.5k / 12w / 1,234 / 695」这类
    资源条写法（旧口径只会拼数字，"1.17万" 抠成 117 是错的）。
    带单位段优先（资源条上 1.17万 比旁边的裸 5 更像正主）；没单位时保持
    老拼接口径（"金币:12345"→12345，存量流程读数零漂移）。
    一个数字都没有 / 数字串带两个以上小数点（版本号"1.2.3"、OCR 串行
    "12.3.4万" 这类拼谁都说不清）→ None，调用方按「没认出」处置，绝不写脏值。
    ⚠ 拼接口径是已记录的存量契约（见自检"金币:12345"），歧义串要拦在拼接口
    之前，别指望调用方再判。"""
    t = (text or "").replace(",", "")
    if re.search(r"\d+\.\d+\.\d+", t):
        return None   # 多段小数点：版本号/串行杂串，数字归属说不清 = 歧义
    m = re.search(r"(\d+(?:\.\d+)?)\s*([万亿kKwW])", t)
    if m:
        return int(round(float(m.group(1)) * _NUM_UNITS[m.group(2)]))
    digits = "".join(re.findall(r"\d+", t))
    return int(digits) if digits else None


def _same_hit(a: dict, b: dict) -> bool:
    """两帧 OCR 命中是不是「同一结果」（供 ServerPickStep 识别稳定用）：
    认出文字一致 + 中心坐标不跳（≤8px 容一点抖动）就算同一处。文字变了或
    位置大幅跳开 = 画面在动/认到别处，稳定段应当清零重攒。"""
    if a is None or b is None:
        return False
    if str(a.get("text", "")) != str(b.get("text", "")):
        return False
    return (abs(int(a.get("x", 0)) - int(b.get("x", 0))) <= 8
            and abs(int(a.get("y", 0)) - int(b.get("y", 0))) <= 8)


def _roi_contains(roi: tuple | None, item: dict) -> bool:
    """识别行中心是否落在用户原框内。roi=None（全屏）恒真。
    ocr.list_text 会给小框自动补边（见 core/ocr._crop），补出来的边扫到的
    邻行数字中心在原框外 → 判 False 剔除，歧义计数只看用户真正框住的那块。"""
    if not roi:
        return True
    x0, y0, x1, y1 = (int(v) for v in roi)
    cx = (int(item.get("x0", 0)) + int(item.get("x1", 0))) // 2
    cy = (int(item.get("y0", 0)) + int(item.get("y1", 0))) // 2
    return x0 <= cx <= x1 and y0 <= cy <= y1


class ServerPickStep(Step):
    """
    识字取物（server_pick 节点）：OCR 认出目标文字那一行，按「用法」处置。

    mode 四选一（不填 = click，老流程零变化）：
      click      点文字中心（+偏移+抖动）—— 选区/选服老行为；
      number     从认出那一行的文字里抠数字，写进流程变量（金币/等级/几服读数）；
      position   把文字框中心坐标写进流程变量（后面点坐标/滑动配合用）；
      roi_number 读区域数字：不认锚点，在 roi 里全量 OCR，取「唯一带数字的
                 行」换算成整数写流程变量（资源条金币/钻石那类图标+数字、
                 没有文字标签可认的场景；换算认 1.17万→11700）。
                 roi 必填（不框等于全屏乱抓一串数字）；区域里 0 个或 ≥2 个
                 数字行都当没认出，继续等/滑动，绝不写脏值。

    ⚠ 旧「选区选服 pick_pair」两段用法已删（一个节点偷跑两步，面板/引擎都为
    它养一套阶段机，不值）。等价摆法：[点文字 框左列] → [sleep] → [点文字 框右列]，
    见 FlowPanel._splices 的一次性换算。

    变量键 = var_name，没填 = 目标文字本身；存取走 flow_var_set/get（ctx.extra）。

    识别走 RapidOCR（点阵字库能力已整体删除），裁决用 core/ocr.match_score
    （数字锚点+单向包含）：标题碎片"服"冒充"含沙射影6服"这类行判 0 分。
    目标文字来源同老版：手填；留空 = 运行时读账号表「登录区服」。

    认不出怎么办（on_timeout）：exit 硬退出 / continue 跳过 / scroll 滑动找。
    scroll = 识别窗口到期就滑一次手（方向/距离可配），等画面停稳
    （settle_secs 秒 —— OCR 同步半秒一张，动画中识别既白跑又拖节拍）再开下一
    个识别窗口，最多滑 scroll_max 次；滑完还没有 = 按认不出收口（N 口/退出）。
    number 用法认出整行但抠不出数字 = 当没认出（认到标题行上了），继续等/滑。

    稳定确认（stable_secs）：认出目标后要【连续认出同一结果】满 stable_secs 秒
    才处置（点击/写变量）——OCR 半秒一张、过场动画里最容易误认到目标词，
    图闪一下没撑满就当没认出（继续等/滑动）。0 = 认出即处置（老行为逐字不变）。
    中途认出别处（文字不同/位置跳开）或认不出 → 计时清零重来。

    全程「记时间戳 + 下拍再查」，本步绝不阻塞（worker 红线）；
    min_interval 节流防连发识别。
    """

    def __init__(self, name, text: str, timeout: float = 15,
                 on_timeout: str = "exit", min_sim: float = 0.6,
                 min_interval: float = 0.3, roi: tuple | None = None,
                 fatal_cb=None, mode: str = "click", var_name: str = "",
                 offset: tuple = (0, 0), jitter: tuple = (0, 0),
                 scroll_dir: str = "up", scroll_px: int = 300,
                 scroll_max: int = 5, scroll_ms: int = 400,
                 scroll_roi: tuple | None = None, settle_secs: float = 0.8,
                 stable_secs: float = 0.0):
        super().__init__(name, timeout=timeout, retries=0)
        self.fixed_text = text
        self.on_timeout = on_timeout
        self.min_sim = min_sim
        self.min_interval = min_interval
        self.roi = roi
        self.fatal_cb = fatal_cb
        self.mode = mode if mode in _SP_DEFAULT_LABEL else "click"
        self.var_name = var_name
        self.offset = offset
        self.jitter = jitter
        self.scroll_dir = scroll_dir if scroll_dir in ("up", "down", "left", "right") else "up"
        self.scroll_px = max(50, min(int(scroll_px), 900))
        self.scroll_max = max(0, int(scroll_max))
        self.scroll_ms = int(scroll_ms)
        self.scroll_roi = tuple(scroll_roi) if scroll_roi else None
        self.settle_secs = max(0.0, float(settle_secs))
        # 识别稳定时长：认出后连续见到同一结果满这么久才处置（0 = 认出即处置）
        self.stable_secs = max(0.0, float(stable_secs))
        self._stable = None        # (起始时刻, 首次 hit dict)；None = 本段未开始
        self._stable_warned = False  # "结果在变"提示每轮只打一次（防刷屏）
        self._num_warned = False   # roi_number 歧义 WARN 每轮只打一次（防 0.3s 刷屏）
        # 滑动找机器（与判断节点翻页同款）：非 scroll 配置不建（None = 无滑动路）
        self._finder = SwipeFinder(self.scroll_dir, self.scroll_px, self.scroll_ms,
                                   self.settle_secs, roi=self.scroll_roi,
                                   max_swipes=self.scroll_max) \
            if on_timeout == "scroll" else None
        self._reset_run()

    def reset(self):
        super().reset()
        self._reset_run()

    def _reset_run(self):
        """运行态清零（构造/重跑/进步各调一次）：滑动余量按配置灌满。"""
        self._last_try = None
        if self._finder is not None:
            self._finder.rearm()
        self._num_warned = False
        self._stable = None
        self._stable_warned = False

    def _target(self, ctx) -> str:
        # 目标文字支持 {变量名} 占位符：先读后点的两步配方（[读服务器名→var] →
        # [识字取物 目标={服务器名} 点击]）。没读到值时保留原文，日志里一眼看出
        # 是哪步没跑。
        if self.fixed_text:
            return _fmt_text(ctx, self.fixed_text)
        from core import database   # 局部 import：与 template_deps 同款
        acc = database.accounts_by_instance().get(ctx.ld_index)
        return str((acc or {}).get("server") or "").strip()

    def _roi_number_hit(self, ctx) -> dict | None:
        """roi_number 的「认中」判据：框内全量 OCR，只取唯一一条带数字的行。
        0 条 = 还没刷出来；≥2 条 = 框圈大了/混进别的数值，读哪条都是猜 ——
        两种都诚实当没认出（走各自的等/滑动收口），绝不写脏值。
        图标本身偶尔会被 OCR 串成杂字符：判据用 parse_game_number 认得出数，
        而不是行里出现 0-9（「金币」两字不含数字，纯图标乱码也不含，安全）。
        ⚠ ocr.list_text 对见方 <_MIN_DET(64px) 的小框会自动补边到可检测尺寸
        （不补 PP-OCR 检不出、0 行 = 永远读不到），代价是补出来的边里邻行数字
        也被扫到。故候选只认「中心落回用户原框内」的行——补边是喂检测模型的，
        不该顺手扩大判歧的范围（窄框读金币常年判 2 歧义、永远空放，实测坑）。"""
        from core import ocr
        cands = [it for it in ocr.list_text(ctx.frame, roi=self.roi)
                 if parse_game_number(it.get("text", "")) is not None
                 and _roi_contains(self.roi, it)]
        if len(cands) != 1:
            if ctx.log is not None and cands and not self._num_warned:
                self._num_warned = True
                texts = "、".join(str(it.get("text")) for it in cands[:4])
                ctx.log(f"{self.name}：框里认出 {len(cands)} 个数字（{texts}），"
                        f"无法确定读哪个，当没认出（把框缩小到只圈住目标数字）", "WARN")
            return None
        it = cands[0]
        x = (int(it.get("x0", 0)) + int(it.get("x1", 0))) // 2
        y = (int(it.get("y0", 0)) + int(it.get("y1", 0))) // 2
        return {"x": x, "y": y, "text": it.get("text", "")}

    def _use_hit(self, ctx, hit: dict, target: str):
        """认出目标后的处置（按 mode 分流）。返回步骤结论。"""
        var = self.var_name or target or "读数"   # roi_number 没锚点也没填名
        if self.mode in ("number", "roi_number"):
            n = parse_game_number(hit["text"])
            if n is None:
                # 认出整行却没数字 = 认到标题/装饰行上了，诚实当没认出：
                # 留在本窗口继续等/滑动，到期按 on_timeout 收口，绝不写脏值
                if ctx.log is not None:
                    ctx.log(f"{self.name}：认出「{hit['text']}」但里面没数字，当没认出",
                            "WARN")
                return None
            flow_var_set(ctx, var, n)
            if ctx.log is not None:
                ctx.log(f"{self.name}：OCR认出「{hit['text']}」→ 数字 {n} "
                        f"已存进流程变量「{var}」", "INFO")
            return "ok"
        if self.mode == "position":
            flow_var_set(ctx, var, (hit["x"], hit["y"]))
            if ctx.log is not None:
                ctx.log(f"{self.name}：OCR认出「{hit['text']}」→ 坐标 "
                        f"({hit['x']},{hit['y']}) 已存进流程变量「{var}」", "INFO")
            return "ok"
        # click 的点选（偏移+抖动后钳进屏内，贴边文字也不点到桌面）
        jdx, jdy = jitter_offset(self.jitter)
        x = max(0, min(539, hit["x"] + self.offset[0] + jdx))
        y = max(0, min(959, hit["y"] + self.offset[1] + jdy))
        _tap(ctx, x, y)
        if ctx.log is not None:
            ctx.log(f"{self.name}：OCR认出「{hit['text']}」(相似{hit['sim']:.2f}) "
                    f"于 ({hit['x']},{hit['y']})，{hit['ms']:.0f}ms，点击 ({x},{y})",
                    "INFO")
        return "ok"

    def tick(self, ctx):
        now = time.monotonic()
        if self._start is None:
            self._on_step_start()
            self._reset_run()
        if self.roi is None and self.mode == "roi_number":
            # 缺框兜底：界面侧校验已拦，这里防手改配置/老数据混进来后
            # 全屏乱抓数字（roi_number 不框 = 把整屏所有数字挨个读走）
            if ctx.log is not None:
                ctx.log(f"{self.name}：没框识别区域，"
                        f"读区域数字必须框住要读的那一小块，本步不执行", "ERROR")
            return "failed"
        if self._finder is not None:
            st = self._finder.wait_settle(now)
            if st == "waiting":
                return "running"          # 滑动动画中：等画面停稳再认
            if st == "due":               # 停稳：重开识别窗口（超时从这拍重新计）
                self._start = now
                self._last_try = None
                self._stable = None       # 刚滑过一页，画面变了：稳定段作废重攒
        anchorless = self.mode == "roi_number"
        target = "" if anchorless else self._target(ctx)
        if not target and not anchorless:
            if ctx.log is not None:
                ctx.log(self._no_target_msg(ctx), "ERROR")
            return "failed"
        if self._last_try is not None and now - self._last_try < self.min_interval:
            return "running"   # 节流：等上次识别的效果，别连发识别
        self._last_try = now
        from core import ocr
        hit = None
        if ctx.frame is not None:
            hit = (self._roi_number_hit(ctx) if anchorless else
                   ocr.find_text(ctx.frame, target, roi=self.roi,
                                 min_sim=self.min_sim,
                                 log_fn=(lambda m: ctx.log(m, "INFO"))
                                 if getattr(ctx, "log", None) else None))
        # 识别稳定（stable_secs）：认出后要连续见到同一结果满 N 秒才处置。
        # 认出别处（文字/位置变了）或认不出 → 清零重来；0 = 认出即处置（老行为）。
        if hit is None:
            self._stable = None
        elif self.stable_secs > 0:
            t_now = time.monotonic()
            prev = self._stable
            if prev is None or not _same_hit(prev[1], hit):
                if prev is not None and ctx.log is not None \
                        and not self._stable_warned:
                    self._stable_warned = True
                    ctx.log(f"{self.name}：认出结果在变（没稳定下来），"
                            f"必须连续 {self.stable_secs:g} 秒认出同一结果才动作",
                            "INFO")
                self._stable = (t_now, hit)
                hit = None                # 本拍不处置，继续攒稳定
            elif t_now - prev[0] >= self.stable_secs:
                self._stable = None       # 撑满：处置后清段
            else:
                hit = None                # 还没撑满
        r = self._use_hit(ctx, hit, target) if hit is not None else None
        if r is not None:
            return r
        # roi_number 无锚点，日志不写空「」；其余模式沿用老格式一字不差
        what = f"「{target}」" if target else "框里的数字"
        if time.monotonic() - self._start > self.timeout:
            if self._finder is not None:
                if self._finder.try_swipe(time.monotonic(), ctx) == "swiped":
                    if ctx.log is not None:
                        ctx.log(f"{self.name}：{self.timeout:g}秒没认出{what}，"
                                f"滑动找（还可滑 {self._finder.left} 次）", "INFO")
                    return "running"
                if ctx.log is not None:
                    ctx.log(f"{self.name}：滑了 {self.scroll_max} 次仍没认出{what}",
                            "WARN")
            # 落败口/跳过的收口（scroll 用尽也走到这）：continue 出口先播一行
            _log = None
            if self.on_timeout == "continue" and ctx.log is not None:
                def _log(c, s):
                    c.log(f"{self.name}：{self.timeout:g}秒没认出{what}，"
                          f"按配置跳过", "WARN")
            return resolve_step_timeout(ctx, self,
                                        self.on_timeout == "continue",
                                        _log, self.fatal_cb)
        return "running"

    def _no_target_msg(self, ctx) -> str:
        """没目标文字的报错文案（手填没填、账号表也没登记「登录区服」）。"""
        return (f"{self.name}：没填目标文字，账号表里实例 {ctx.ld_index} "
                f"也没登记「登录区服」，本步无法执行")


def _warn_timeout_log(ctx, step):
    """wait_template on_timeout=continue 触发时的 WARN 日志回调（注入给 WaitStep）。"""
    if ctx is not None and getattr(ctx, "log", None) is not None:
        ctx.log(f"等模板「{step.name}」超时（{step.timeout}秒），按配置继续往下走", "WARN")


def _warn_gate_log(ctx, step):
    """click_template 前置图等图超时、且按配置「跳过继续」时触发的 WARN。
    早先超时还能"判失败走恢复链"，fail 语义已并入 exit（直接退出程序），
    本回调现在只给 continue 用。"""
    if ctx is None or getattr(ctx, "log", None) is None:
        return
    ctx.log(f"点模板「{step.name}」等图出现超时（{step.gate_timeout:g}秒），"
            f"按配置跳过本次点击，继续往下走", "WARN")


# ── 判断条件工厂（取色在这里包进 condition，不进引擎）──────────────────

def _make_condition(cond: dict):
    """把 cond 节点翻译成 condition(ctx)->bool 闭包。"""
    kind = cond.get("kind") if cond else ""

    if kind == "template":
        cat, name = cond.get("category", ""), cond.get("name", "")
        # 条件自带区域 = 覆盖模板账本那一份：同一张图（如锁图标）在不同条件里
        # 框不同行各找各的。没框 = None，跟账本默认区域走（存量行为不变）。
        r = cond.get("roi") or None
        return lambda c, a=cat, b=name, r=r: template_present(c, a, b, roi=r)

    if kind == "color":
        roi_list = cond.get("roi")
        roi = tuple(roi_list) if roi_list else None
        peak = float(cond.get("peak", 200.0))
        channel = cond.get("channel", "bgr")
        return lambda c, r=roi, p=peak, ch=channel: vision.match_color(
            c.frame, roi=r, peak_thresh=p, channel=ch)

    if kind == "multi_color":
        pts = cond.get("points", []) or []
        tol = int(cond.get("tolerance", 8))
        return lambda c, ps=pts, t=tol: vision.match_multi_color(
            c.frame, ps, t)["ok"]

    if kind == "motion":
        # 动态检测（多帧组判断"动画在不在动"）：engine 每 0.25s 调一次本条件，
        # 把"当前最像动画组哪一帧"记进 ctx.extra 的历史；攒够阈值票数后，
        # 任两次记录的帧号不同 = 动画在动 → True（然后清历史重新攒，本条件只
        # 在"要等动画动起来"这一小段时间内带状态）。
        # frames = 组内每帧模板名（同一库），name 兼容老单张（一张也算一组）。
        frames = cond.get("frames", []) or []
        cat = cond.get("category", "")
        name = cond.get("name", "")
        if not frames and name:
            frames = [name]
        n_of = max(1, int(cond.get("samples", 4)))
        roi = tuple(cond.get("roi", [])) if cond.get("roi") else None
        tpls = []
        for fn in frames:
            e = template_manager.find_template(cat, fn)
            img = template_manager.load_template_image(e) if e else None
            if img is not None:
                tpls.append(img)

        seqkey = ("motion", id(frames))   # 键含帧列表 id：多条 motion 条件各带各的历史，互不污染

        def check(c, tpls=tpls, no=n_of, rs=roi):
            if not tpls:                       # 组空（缺图）→ 恒 False，走 false 分支
                return False
            seq = c.extra.get(seqkey)
            if seq is None:
                seq = c.extra[seqkey] = []
            r = vision.group_best_match(c.frame, tpls, roi=rs)
            seq.append(r[0] if r else -1)     # 没认出 = -1（缺席也算一种"不同"）
            if len(seq) < no:
                return False
            # -1（缺席）也算一个"画面态"：图标闪没又出现 = 真在动；
            # 连续缺席 = 一直没这图 = 静止（两种都要诚实对待）
            moved = any(seq[i] != seq[i - 1] for i in range(1, len(seq)))
            seq.clear()
            return moved

        return check

    if kind == "var":
        # 变量比较（配「识字取物」的读数用法）：先读后判，如
        # [读体力→变量体力] → [判断 体力>=10] → Y 去刷本 / N 收工。
        # 两边都能化成数就按数比，否则退化成字符串相等性（> 之类判不成立，
        # 不做字典序 —— "9" > "10" 这种反直觉结果比判不了更坏）。
        # 变量从没读到 = 恒 False 走 N（收口诊断点名"没这个值"，不静默）。
        # 区间比较（F4）：op=between/not_between，value=下界、value2=上界。
        #   语义 = 下界 ≤ v ≤ 上界（含端点）；两端先 min/max 归一防反填。
        #   not_between = 补集。v 或任一边界转不成数 → 恒 False（与现有一致，
        #   绝不做字典序区间 —— 那正是现有 > 判不成立要避免的坑）。
        name = cond.get("name", "")
        op = cond.get("op", ">=")
        raw = cond.get("value", "")
        raw2 = cond.get("value2", "")

        def check(c, nm=name, o=op, rv=raw, rv2=raw2):
            v = flow_var_get(c, nm)
            if v is None:
                return False
            if o in ("between", "not_between"):
                try:
                    x, b1, b2 = float(v), float(rv), float(rv2)
                except (TypeError, ValueError):
                    return False        # 非数值区间无意义：恒 False，不做字典序
                lo, hi = (b1, b2) if b1 <= b2 else (b2, b1)   # 反填归一
                inside = lo <= x <= hi
                return inside if o == "between" else not inside
            try:
                a, b = float(v), float(rv)
            except (TypeError, ValueError):
                if o == "==":
                    return str(v) == str(rv)
                if o == "!=":
                    return str(v) != str(rv)
                return False
            return {"==": a == b, "!=": a != b, ">": a > b,
                    ">=": a >= b, "<": a < b, "<=": a <= b}.get(o, False)

        return check

    if kind == "text":
        # 文字识别判断（例：默认选的区服跟设定一致吗）：ROI 限定 OCR +
        # match_score 数字锚点（与选区点击同一套裁决，6服/9服不容认错）。
        # OCR 同步 ~0.3s/张，判断条件每拍都跑会拖 worker 节拍 —— ttl 秒内
        # 用缓存判定（缓存键绑本次编译闭包，重编译不串台）；依赖没装恒
        # False 走假分支（编辑面板有黄字提示，真机不静默）。
        from core import ocr as _ocr
        roi_list = cond.get("roi")
        roi = tuple(roi_list) if roi_list else None
        target = cond.get("target", "")
        min_sim = float(cond.get("min_sim", 0.6))
        invert = bool(cond.get("invert", False))
        ttl = max(0.4, float(cond.get("ttl", 1.0)))
        key = "text_cond_" + uuid.uuid4().hex

        def check(c, rs=roi, t=target, ms=min_sim, iv=invert, tl=ttl, k=key):
            if not t or not _ocr.available():
                return False
            now = time.monotonic()
            ent = c.extra.get(k)
            if ent is None or now - ent[0] > tl:
                # 期望文字也吃占位符（{变量名} / {账号.列名}）：放在缓存到期分支
                # 里展开 —— {账号.X} 每次要查库，ttl 窗口内不重复查
                hit = _ocr.find_text(c.frame, _fmt_text(c, t), roi=rs, min_sim=ms)
                ent = c.extra[k] = (now, hit is not None)
            return ent[1] != iv

        return check

    return lambda c: False


def _make_branch_diag(node: dict):
    """判断节点收口瞬间的日志回调 (ctx, shot)->None（shot = 本次走的出口
    "true"/"false"）：
    - 走 N：自动诊断打一行找原因（备注名 = 日志里的标识）。模板条件收口时现扫
      最高分（与打印检测同口径 debug=True），一眼分清"差一点（降阈值）"还是
      "压根没这图（换图）"；其余条件类型只报结论。
    - 走 Y：同样打一行结论 `判断「lb」✓ 条件成立 →走Y`（单条件逐字"条件成立"，
      多条件报「全部/任一条件满足」；通过了没明细可诊断，别灌日志）。
    整段吞异常 —— 诊断日志绝不能把引擎带崩（_debug_print 同纪律）。
    多条件版：逐条报。单条件且未取反 = 旧日志逐字不变；第 2 条起或带取反
    才冠「条件k」编号。"""
    lb = node.get("label", "") or "判断"
    def _detail(cd, ctx):
        kind = cd.get("kind", "")
        if kind == "template":
            cat, name = cd.get("category", ""), cd.get("name", "")
            entry = template_manager.find_template(cat, name)
            tpl = template_manager.load_template_image(entry) if entry else None
            if entry is None or tpl is None:
                return f"模板 {cat}/{name} 读不到"
            hit = vision.match_template(ctx.frame, tpl, entry["threshold"],
                                        roi=entry.get("roi"),
                                        rgb_threshold=entry.get("rgb_threshold"),
                                        debug=True)
            if hit and hit.get("matched"):
                # 收口瞬间图反倒在了 = 阈值边缘/确认期问题，照样如实打
                return (f"模板 {cat}/{name} 此刻已出现 ({hit['x']},{hit['y']}) "
                        f"分{hit['score']:.3f}")
            score = hit["score"] if hit else 0.0
            return (f"模板 {cat}/{name} 最高分{score:.3f}"
                    f"（阈值{entry['threshold']}）")
        if kind == "var":
            v = flow_var_get(ctx, cd.get("name", ""))
            _op = cd.get("op", ">=")
            if _op in ("between", "not_between"):
                # 区间比较诊断：把两端范围摆出来（下界..上界），一眼看出差多远
                _rel = "应落在区间内" if _op == "between" else "应落在区间外"
                return (f"变量 {cd.get('name', '')}"
                        f"{'= ' + str(v) if v is not None else ' 没值（先放识字取物读到它）'}"
                        f" 区间 [{cd.get('value', '')} .. {cd.get('value2', '')}]"
                        f" 不成立（{_rel}）")
            return (f"变量 {cd.get('name', '')}"
                    f"{'= ' + str(v) if v is not None else ' 没值（先放识字取物读到它）'}"
                    f" {_op} {cd.get('value', '')} 不成立")
        if kind == "text":
            return f"没找到文字「{cd.get('target', '')}」"
        if kind == "motion":
            return "动态检测：画面没在动"
        return "条件不成立"

    def cb(ctx, shot="false"):
        try:
            if ctx is None or getattr(ctx, "log", None) is None:
                return
            from bot import template_deps
            items = template_deps.branch_cond_items(node)
            if shot == "true":
                # 走 Y：同样打一行结论；通过了没明细可诊断，单/多条件只换个说法
                if len(items) <= 1 and not (items or [{}])[0].get("not"):
                    ctx.log(f"判断「{lb}」✓ 条件成立 →走Y", "INFO")
                else:
                    rel = "任一条件" if node.get("logic") == "any" else "全部条件"
                    ctx.log(f"判断「{lb}」✓ {rel}满足 →走Y", "INFO")
                return
            if getattr(ctx, "frame", None) is None and \
                    any(cd.get("kind") == "template" for cd in items):
                # 无画面（截图失败那拍）短路：既不查账本也不匹配，省得白读盘
                ctx.log(f"判断「{lb}」✗ 无画面 →走N", "INFO")
                return
            if len(items) <= 1 and not (items or [{}])[0].get("not"):
                # 老单条件格式逐字保留（无 cond = "条件不成立"，同旧 else 兜底）
                det = _detail(items[0], ctx) if items else "条件不成立"
                ctx.log(f"判断「{lb}」✗ {det} →走N", "INFO")
                return
            parts = []
            for i, cd in enumerate(items, 1):
                neg = "（取反）" if cd.get("not") else ""
                parts.append(f"条件{i}{neg}：{_detail(cd, ctx)}")
            rel = "任一条件" if node.get("logic") == "any" else "全部条件"
            ctx.log(f"判断「{lb}」✗ {rel}不满足（{'；'.join(parts)}"
                    f"）→走N", "INFO")
        except Exception:
            pass        # 诊断绝不反噬流程
    return cb


def _diag_missing_branch(node: dict, why: str):
    """缺图降级判断的诊断回调：这种分支恒走 N，且是真问题 → WARN。"""
    lb = node.get("label", "") or "判断"

    def cb(ctx, shot="false"):
        try:
            if ctx is not None and getattr(ctx, "log", None) is not None:
                ctx.log(f"判断「{lb}」✗ {why} →走N", "WARN")
        except Exception:
            pass
    return cb


# ── v1 树 → v2 图迁移 + 图编译（自由连线版流程）────────────────────────

def _graph_id() -> str:
    """生成稳定节点 id：创建后不变，拖拽/改参不动。"""
    return uuid.uuid4().hex[:8]


def _warn_subflow_gate(ctx, step):
    """子流程进入前置门超时：走失败出口的 WARN（端口语义写进日志，好排查）。"""
    if ctx is not None and getattr(ctx, "log", None) is not None:
        ctx.log(f"子流程「{step.name}」进门前等图超时（{step.gate_timeout:g}秒），走失败出口", "WARN")


def _make_build_step(flow_map: dict, stack: list, fail_srcs=frozenset()):
    """
    GraphStep 的 build_step 闭包工厂。stack = 从外层一路展开来的流程名链
    （编译期防环：子流程目标已在栈上 = 递归成环，降级成 WARN，绝不无限展开）。
    fail_srcs = 本图接了「败」口出线的节点 id 集合（_compile_graph_impl 扫边
    算出），透传给 _action_step 关掉「超时退出程序」注入。
    branch 一律返回 None（投票由 GraphStep 内部做，_branch_conds 决定条件——
    缺图就给 (None, timeout) 锁定恒走 false）。
    """
    cache = {}   # id(节点dict) → (dict, 产物)：GraphStep 点火每次都走
                 # _build_node——查缺图/ocr.available()/子流程整图重编译，循环型
                 # 流程一轮接一轮全在重复编译同一份不变配置（性能审计）

    def build(node: dict):
        # 按节点对象身份缓存（不是按 id 字段）：GraphStep 的 rec["node"] 每轮
        # 传的是同一个 dict 对象 → 复用；换对象（哪怕 id 字段同名）= 配置变了
        # → 重建。条目吊着 dict 引用，id 复用不会张冠李戴；缓存规模 ≤ 图节点数
        # ×重建次数，随闭包（= GraphStep 编译轮次）一起回收。
        # 复用安全靠消费方纪律：GraphStep/SubFlowStep/LoopStep 每次起跑先 reset。
        hit = cache.get(id(node))
        if hit is not None and hit[0] is node:
            return hit[1]
        step = _build_node(node)
        # 只缓存成功产物：降级 LogStep（缺图/缺 OCR/成环）恰恰依赖"世界会变"
        # ——用户挂机途中补采模板，下一拍重建就该自愈；缓存它们等于把 WARN 焊死。
        # LogStep 构造本就便宜，不缓存零代价。
        if not isinstance(step, LogStep):
            cache[id(node)] = (node, step)
        return step

    def _build_node(node: dict):
        t = node.get("type", "")
        if t == "branch":
            # 缺图 branch 不能返回 LogStep：GraphStep 对"返回了 Step 的 branch"会把它
            # 当普通动作节点跑（LogStep 直接 ok → 向不存在的 out 口传播 → true/false
            # 下游收到 0 空转）。必须返回 None，让投票机制接管；缺图与否在
            # _branch_conds 里表达（None = 恒 false）。
            return None
        if t == "subflow":
            fname = (node.get("flow_name") or "").strip()
            label = node.get("label") or (f"子流程 {fname}" if fname else "子流程")
            if not fname:
                return LogStep(label, "子流程节点没选流程，按成功直通"
                                      "（去属性面板选一条，或取消「等图再进」）", "WARN")
            if fname in stack:
                return LogStep(label, f"子流程递归成环：{'→'.join(stack)}→{fname}，"
                                      "本节点跳过（按成功直通）", "WARN")
            sub = flow_map.get(fname)
            if sub is None:
                return LogStep(label, f"子流程「{fname}」不存在（被删了？），本节点跳过",
                               "WARN")
            body = _compile_graph_impl(sub, flow_map, stack + [fname])
            gate = None
            cat, nm = node.get("category", ""), node.get("name", "")
            if bool(node.get("wait_before", True)) and nm:
                if template_manager.has_template(cat, nm):
                    gate = lambda c, a=cat, b=nm: template_present(c, a, b)
                else:
                    # 缺图门恒 False → 默默超时走失败口 = 最难排查的哑 bug，
                    # 与 click_template 前置门同口径：缺图在编译期就拆穿
                    return LogStep(label, f"子流程「{fname}」进门等的模板"
                                          f"「{cat}/{nm}」不存在，本节点跳过", "WARN")
            return SubFlowStep(label, body, gate=gate,
                               gate_timeout=float(node.get("wait_timeout", 8)),
                               on_gate_timeout=node.get("on_timeout", "fail"),
                               on_gate_timeout_log=_warn_subflow_gate,
                               bg_watch=bool(node.get("bg_watch", True)),
                               params=_subflow_params(node))
        if t == "multi_scan":
            # 遍历全部（2026-09）：同屏命中一次收清（快照），按模式逐个处置。
            # 零命中门的三档口径与点模板逐字一致（wait_before/wait_timeout/
            # on_timeout + 接败口让位 fatal）；「进一个」的子流程体按 subflow
            # 同款递归编译（成环/没流程/缺图全在编译期拆穿，不留静默哑 bug）。
            cat, name = node.get("category", ""), node.get("name", "")
            src = node.get("source", "template")
            txt = str(node.get("text", "") or "").strip()
            label = (node.get("label")
                     or (f"遍历全部 {name}" if src != "text" and name
                         else f"遍历文字「{txt}」" if txt else "遍历全部"))
            if src == "text":
                # 文字源（P3）：不引模板，改判 OCR 可用性 —— 与识字取物同纪律：
                # 依赖没装编译期就拆穿，别等运行时静默零命中耗满门超时
                if not txt:
                    return LogStep(label, "遍历全部选了文字源却没填文字，跳过本次遍历",
                                   "WARN")
                from core import ocr
                if not ocr.available():
                    return LogStep(label, "OCR 依赖未安装（pip install rapidocr "
                                   "onnxruntime），跳过本次遍历", "WARN")
            elif _tpl_missing(cat, name):
                return _missing_step(label, cat, name, "跳过本次遍历")
            mode = node.get("mode", "tap")
            body = confirm = None
            if mode == "enter":
                fname = (node.get("flow_name") or "").strip()
                if not fname:
                    return LogStep(label, "遍历全部「进一个」没选子流程，本节点跳过"
                                          "（去属性面板选一条）", "WARN")
                if fname in stack:
                    return LogStep(label, f"子流程递归成环：{'→'.join(stack)}→{fname}，"
                                          "本节点跳过（按成功直通）", "WARN")
                sub = flow_map.get(fname)
                if sub is None:
                    return LogStep(label, f"子流程「{fname}」不存在（被删了？），本节点跳过",
                                   "WARN")
                body = _compile_graph_impl(sub, flow_map, stack + [fname])
                ct = node.get("confirm_tpl") or {}
                cc, cn = ct.get("category", ""), ct.get("name", "")
                if cn:
                    if _tpl_missing(cc, cn):
                        # 缺确认图 = "回没回家"永远判不出，瞎点旧坐标是事故；
                        # 与 subflow 门缺图同纪律：编译期整节点拆穿
                        return _missing_step(label, cc, cn,
                                             "跳过本次遍历（列表标志图缺失，回不了列表页）")
                    confirm = (cc, cn)
            skip_on_timeout = node.get("on_timeout", "fail") == "continue"
            gt = (float(node.get("wait_timeout", 8))
                  if bool(node.get("wait_before", True)) else 0.0)
            fatal_cb = (_make_fatal_exit_cb("遍历全部")
                        if gt > 0 and not skip_on_timeout
                        and node.get("id") not in fail_srcs else None)
            # 读数模式一下都不点，"办过的身份"无从谈起——去重在这条路上是
            # 纯耗时的重复扫描，编译层直接强制关（界面上也置灰，双保险）
            dedupe = bool(node.get("dedupe", False)) and mode != "read"
            # 数字过滤（2026-10-06）：read 模式同口径强制关；勾选就得靠邻框读
            # 数 —— OCR 没装/符号不认识都整节点拆穿（宁可什么都不点，绝不
            # 降级成"不过滤瞎点"，那会把不达标的卡片全点进去）
            filt = str(node.get("filter_op", "") or "")
            if filt and mode == "read":
                filt = ""
            if filt:
                if filt not in _FILT_OPS:
                    return LogStep(label, f"遍历全部数字过滤比较符不认识：{filt}，"
                                          "本节点跳过（去属性面板重选）", "WARN")
                from core import ocr
                if not ocr.available():
                    return LogStep(label, "数字过滤依赖识字读数但 OCR 未安装"
                                          "（pip install rapidocr onnxruntime），"
                                          "本节点跳过", "WARN")
            return MultiScanStep(
                label, cat, name, mode=mode,
                offset=(int(node.get("offset_x", 0) or 0),
                        int(node.get("offset_y", 0) or 0)),
                jitter=_jit_range(node), roi=node.get("roi") or None,
                max_hits=int(node.get("max_hits", 20) or 20),
                gap_secs=float(node.get("gap_secs", 0.5) or 0.5),
                gate_timeout=gt, ok_on_timeout=skip_on_timeout,
                on_timeout_log=_warn_multi_scan_log if (gt > 0 and skip_on_timeout)
                else None,
                fatal_cb=fatal_cb,
                var_name=str(node.get("var_name", "遍历命中数") or ""),
                body=body, confirm=confirm,
                confirm_timeout=float(node.get("confirm_timeout", 5) or 5),
                settle_secs=float(node.get("settle_secs", 0.8) or 0.8),
                source=src, text=txt,
                min_sim=float(node.get("min_sim", 60) or 60) / 100.0,
                dedupe=dedupe,
                max_rounds=int(node.get("max_rounds", 50) or 50),
                read_box=(int(node.get("read_dx", 0) or 0),
                          int(node.get("read_dy", 0) or 0),
                          int(node.get("read_w", 160) or 160),
                          int(node.get("read_h", 48) or 48)),
                # 缺键兜 True = 老「进一个回一个」照按返回键（存量流程行为
                # 零变化）；新节点由界面显式落 False
                press_back=bool(node.get("press_back", True)),
                filt_op=filt, filt_val=float(node.get("filter_val", 0) or 0),
                stop_on=_stop_on_names(node))
        if t == "loop":
            # 「循环」容器节点：体内动作行（steps 子列表）编译成一条子图，
            # 套现成 LoopStep。每轮跑完查一次出口条件（until），条件满足就出
            # 循环往下走。max_times=0 = 不限轮数（只认出口条件，直到满足为止）。
            label = node.get("label") or "循环"
            entries = [e for e in (node.get("steps") or []) if isinstance(e, dict)]
            if not entries:
                return LogStep(label, "循环体是空的，本节点按成功直通"
                                      "（去属性面板添加动作行）", "WARN")
            # 出口条件（2026-10-06 起多条件化）：与判断节点同字段同口径 ——
            # cond=第1条 + extra_conds=第2..N条 + logic(all/any) + 每条可 not，
            # 组合复用判断的 _make_branch_condition（引擎/引用扫描一份语义，
            # 面板也共用同一区）。单条件未取反 = 与旧 _make_condition 逐字等价
            # （老流程零变化）。var 也认 ——「读够数才出循环」是真实需求；
            # text 也认（2026-10-06 补：面板一直给选文字识别，白名单漏了它，
            # 用户配「期望文字20」整节点被换成直通 LogStep——循环体一步没跑。
            # 白名单必须跟面板口径同步：除 motion 外五型全收）。
            # 缺图/未知类型整节点拆穿：出口判错 = 循环永不出或提前出，
            # 比不跑更毒，不静默降级。
            from bot import template_deps
            items = template_deps.branch_cond_items(node)
            for cd in items:
                ck = cd.get("kind", "")
                if ck == "template" and _tpl_missing(
                        cd.get("category", ""), cd.get("name", "")):
                    return LogStep(label, f"循环出口模板「{cd.get('name')}」"
                                          "不存在，本节点跳过（先去采集）", "WARN")
                if ck not in ("template", "color", "multi_color", "text", "var"):
                    return LogStep(label, f"未知循环出口条件：{ck}（按成功直通）",
                                   "WARN")
            until = _make_branch_condition(node) if items else None
            # 循环体：动作行是图节点 → 就地编成一条小图 body（GraphStep 自带
            # 每拍落手闸门，和主图互不干扰 —— 体内每拍最多落一次手）。
            # 行可能没 id（草稿/未保存节点）→ 补一个，保证顺序边能连
            nodes_body = []
            edges_body = []
            prev_id = None
            for e in entries:
                row = dict(e)
                row.setdefault("id", _graph_id())
                nodes_body.append(row)
                if prev_id is not None:
                    edges_body.append({"from": prev_id, "port": "out", "to": row["id"]})
                prev_id = row["id"]
            body = _compile_graph_impl(
                {"version": 2, "nodes": nodes_body, "edges": edges_body},
                flow_map, stack)
            return LoopStep(
                label, body,
                until=until,
                times=int(node.get("max_times", 0) or 0) or None,
                stop_on=_stop_on_names(node))
        if t == "watch":
            # 并行监视哨兵：rows 每行 = 弹窗模板→子流程。行级坏数据（缺图/
            # 流程没了/递归引用）逐行判死留位（body=None + 停用），绝不像 subflow
            # 那样整节点降级 —— 少一行不该连累其余监视行（弹窗区同款纪律）。
            label = node.get("label") or "并行监视"
            rows = [dict(r) for r in (node.get("rows") or []) if isinstance(r, dict)]
            if not rows:
                return LogStep(label, "并行监视没配监视行，本节点跳过"
                                      "（去属性面板添加监视行：命中即点 / 跑子流程 / 封号停用）", "WARN")
            bodies = []
            for r in rows:
                fname = (r.get("flow_name") or "").strip()
                r["flow_name"] = fname
                cat, nm = r.get("category", ""), r.get("name", "")
                # 命中即点/封号停用行不需要子流程：只认图，缺图才算死行
                if r.get("kind") in ("tap", "ban"):
                    bodies.append([] if (nm and template_manager.has_template(cat, nm))
                                  else None)
                    continue
                # 动态手指行（2026-10-11 新增）：不认图、无 category/name，也不跑
                # 子流程——body 恒空列表（不判死：它没有"缺图"这个死法）。行本体
                # 自带阈值/外延参数，引擎 tick 里现扫现点，见 WatchStep.tick。
                if r.get("kind") == "finger":
                    bodies.append([])
                    continue
                if not nm or not fname or flow_map.get(fname) is None \
                        or fname in stack:
                    bodies.append(None)
                    continue
                if not template_manager.has_template(cat, nm):
                    bodies.append(None)
                    continue
                bodies.append(_compile_graph_impl(flow_map[fname], flow_map,
                                                  stack + [fname]))
            return WatchStep(label, rows, bodies,
                             cooldown_secs=float(node.get("cooldown_secs", 5)),
                             fail_limit=int(node.get("fail_limit", 3)),
                             scan_mode=str(node.get("scan_mode") or "restart"),
                             stats_key=(stack[0] if stack else "",
                                        node.get("id", "")))
        if t == "timer":
            # 定时执行哨兵：rows 每行 = 间隔分钟→子流程。行级坏数据（流程没了/
            # 递归引用）逐行判死留位 —— watch 同款纪律；不认图所以没有缺图判死。
            label = node.get("label") or "定时执行"
            rows = [dict(r) for r in (node.get("rows") or []) if isinstance(r, dict)]
            if not rows:
                return LogStep(label, "定时执行没配执行行，本节点跳过"
                                      "（去属性面板添加「间隔→子流程」行）", "WARN")
            bodies = []
            for r in rows:
                fname = (r.get("flow_name") or "").strip()
                r["flow_name"] = fname
                if not fname or flow_map.get(fname) is None or fname in stack:
                    bodies.append(None)
                    continue
                bodies.append(_compile_graph_impl(flow_map[fname], flow_map,
                                                  stack + [fname]))
            return TimerStep(label, rows, bodies,
                             cooldown_secs=float(node.get("cooldown_secs", 5)),
                             fail_limit=int(node.get("fail_limit", 3)))
        return _action_step(node, retries=0,
                            has_fail=node.get("id") in fail_srcs)
    return build


def _is_action_node(node: dict) -> bool:
    """动作闸门判定：click_template/click/swipe/back 需要落手（同一拍只能落手
    一次），等待/判断/打印只是评估不落手。
    subflow 也算动作：它内部会落手点屏，必须吃外层落手闸门，否则同一拍里
    子流程和旁边的点击节点各拿一份落手权 = 一帧点两下。
    popup_zone 同理：命中就点屏；且扫描期间独占落手权正是想要的语义——
    弹窗没清完时旁边的操作多半点歪。
    loop 同理：体内是动作链会落手点屏。
    watch/timer 同理会落手（跑子流程），但闸门生效位置特殊：主循环跳过它，
    落手权在 GraphStep 拍尾哨兵通道里管（每拍必扫；子链一跑即 takeover，
    反过来让主线整条暂停——一刀切）。
    multi_scan 同 subflow：点命中/返回键/子流程体内动作都会落手，必须吃闸门。"""
    return node.get("type", "") in ("click_template", "click", "swipe",
                                    "subflow", "popup_zone", "server_pick",
                                    "loop", "watch", "timer", "multi_scan")


def _cond_missing_note(cd: dict) -> str:
    """条件项的缺图说明（"" = 不缺）。template 缺图 / motion 缺任一帧 =
    这条永远判不成立，编译期就得知道（旧版单条件恒走N降级的同款判据）。"""
    if cd.get("kind") == "template" and _tpl_missing(
            cd.get("category", ""), cd.get("name", "")):
        return (f"模板「{cd.get('category', '')}/"
                f"{cd.get('name', '')}」不存在")
    if cd.get("kind") == "motion":
        cat = cd.get("category", "")
        miss = [fn for fn in (cd.get("frames") or []) if _tpl_missing(cat, fn)]
        if miss:
            return "动态检测帧组缺图：" + "、".join(miss)
    return ""


def _wrap_hold(cond: dict, fn):
    """给条件闭包套一层「持续时长」（hold_secs）：这条必须【连续成立】满 N 秒
    才算成立——图闪一下没撑满 = 本拍不算成立（判断节点继续投/超时走 N，循环
    出口继续转）。中途某一拍不成立 → 计时清零重来。

    hold_secs 缺省/0 → 原样返回 fn（存量流程逐字不变，零迁移：用户没拨这个
    旋钮就不写键）。motion 跳过：动态检测本身即"时间窗内动没动"的语义，
    再叠一层持续没有意义。

    跨拍状态挂 ctx.extra[唯一键]（与 motion 采样/text 缓存同款机制；键在编译期
    uuid 生成，整段运行期稳定）。值 = [本段连续成立的起始时刻, 上拍评估时刻]；
    None = 本段尚未开始。距上拍评估超过 max(30s, 10·hold) 视为断点（暂停/重试
    后回来，观测不连续）→ 重开计时，不吃陈旧时间戳瞬达；正常拍间隔（0.25s）
    与循环轮间隔都远小于此阈值，不会误伤。"""
    h = float(cond.get("hold_secs", 0) or 0) if isinstance(cond, dict) else 0.0
    if h <= 0 or (isinstance(cond, dict) and cond.get("kind") == "motion"):
        return fn
    key = "hold_cond_" + uuid.uuid4().hex
    gap = max(30.0, h * 10.0)

    def hold(c, inner=fn, hh=h, k=key, gp=gap):
        now = time.monotonic()
        st = c.extra.get(k)
        if not bool(inner(c)):
            c.extra[k] = None          # 断了：清计时，下次成立从零起
            return False
        if st is None or (now - st[1]) > gp:   # 断点：重开一段
            c.extra[k] = [now, now]
            return False
        start = st[0]
        c.extra[k] = [start, now]
        return (now - start) >= hh

    return hold


def _make_branch_condition(node: dict):
    """判断节点 → 投票闭包：cond（第1条）+ extra_conds（第2..N条）按
    logic 组合（all=满足全部 / any=满足任一），每条可带 not 单独取反。
    单条件未取反未配持续 = 就是 _make_condition 本身（老流程零变化）。
    每条先套 _wrap_hold「持续时长」（判断与循环出口同吃这份 —— 两者都走本
    函数），取反作用在"撑满持续之后"。
    清单口径走 template_deps.branch_cond_items（与引用扫描一份）。"""
    from bot import template_deps
    fns = []
    for cd in template_deps.branch_cond_items(node):
        f = _wrap_hold(cd, _make_condition(cd))
        if cd.get("not"):
            f = (lambda c, inner=f: not inner(c))
        fns.append(f)
    if not fns:
        return lambda c: False
    if len(fns) == 1:
        return fns[0]
    logic = node.get("logic", "all")

    def cond(c, fs=fns, lg=logic):
        # 多条件单拍时间预算（性能审计）：模板/动态检测条件一条就是整屏匹配
        # （~10-30ms），三五条串行能把一拍吃穿。超预算的条本拍跳过按"未成立"
        # 记——判断是逐拍投票窗口，只会把成立点最多推迟几拍，绝不把成立误判
        # 成永久失败（False 不可能因跳过变 True，all/any 语义方向安全）。
        # 短路方向保留：any 遇真即收，all 遇假即停，与旧逐字组合等价。
        t0 = time.perf_counter()
        budget = _scan_tick_budget()
        vals = []
        for f in fs:
            if vals and (time.perf_counter() - t0) > budget:
                vals.append(False)      # 顺延条：本拍按未成立，下拍整窗重来
                if lg == "all":
                    break               # all 已注定 False，不再白跑剩余条
                continue
            v = bool(f(c))
            vals.append(v)
            if lg == "any" and v:
                return True
            if lg == "all" and not v:
                break
        return all(vals) if lg == "all" else any(vals)
    return cond


def _is_pair_node(n) -> bool:
    """旧「选区选服」节点：server_pick + mode=pick_pair（该用法已删，
    只为 ensure_graph 里的一次性换算服务，见 _splice_pick_pair）。"""
    return (isinstance(n, dict) and n.get("type") == "server_pick"
            and n.get("mode") == "pick_pair")


def _has_pair(nodes) -> bool:
    """图里（含循环体 steps）是否还有 pick_pair 节点。"""
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        if _is_pair_node(n) or _has_pair(n.get("steps")):
            return True
    return False


# pick_pair 换算时从旧节点照抄的共用参数（两阶段本来就共用这一套）
_PAIR_SHARED_KEYS = ("timeout", "on_timeout", "min_sim", "min_interval",
                     "offset_x", "offset_y", "jitter_px_min", "jitter_px_max",
                     "jitter_rand", "scroll_dir", "scroll_px", "scroll_max",
                     "scroll_ms", "scroll_settle", "scroll_roi")


# 区服占位符（含别名/英文字段直写）：区名撞上它 = 运行时才展开，静态取不了值（切不了前缀）
_PH_SERVER = re.compile(r"\{\s*账号[.．]\s*(?:区服|登录区服|server|选区|选服)\s*\}")


def _pair_step_nodes(n: dict) -> list:
    """一个 pick_pair 节点 → [点区, sleep, 点服] 三个节点 dict（不含 x/y/id，
    由调用方补齐）。文字回落规则与运行时派生列同口径：区名优先「选区文字」，
    没填就取全名前 4 字（游戏区名恒 4 字，不足 4 = 整串当区名）。
    区名撞上区服占位符（{账号.区服}）或全名留空（运行时读账号表）时，展开发生
    在运行时、这里静态切不了 —— 换成派生列 {账号.选区}：运行时取区服值再切
    前 4 字，多开各号各区照样一步点中。"""
    full = str(n.get("text", "") or "").strip()
    zone = str(n.get("zone_text", "") or "").strip()
    # 占位符判定必须在前："{账号.区服}"[:4] 会切出残串骗过后头的检出
    if _PH_SERVER.search(zone) or _PH_SERVER.search(full) or not (zone or full):
        # 任一名列撞上区服占位符（含整对空 = 运行时读表）：静态切不了，换派生列
        zone = "{账号.选区}"
    elif not zone:
        zone = full[:4]      # 区名恒 4 字，与 _acct_field 派生列同口径

    def step(roi, label, txt):
        s = {"type": "server_pick", "label": label, "text": txt,
             "mode": "click", "roi": list(roi) if roi else None}
        for k in _PAIR_SHARED_KEYS:
            if k in n:
                s[k] = n[k]
        return s
    gap = float(n.get("pair_gap_secs", 1.5) or 1.5)
    return [step(n.get("roi"), "选区", zone),
            {"type": "sleep", "label": "步间等待", "seconds": gap},
            step(n.get("roi_server"), "选服", full)]


def _splice_pick_pair(flow: dict) -> dict:
    """旧「选区选服」(pick_pair) 一次性换算：1 节点 → [点区 → sleep → 点服] 3 节点。
    编辑器载入/保存与运行时编译都经 ensure_graph 走到这里 —— 一处换算全链生效，
    引擎里那套双阶段状态机因此整个删掉了。
    · 图节点：X 的入边改接待首节；X 的 out 边改挂末节；X 的 fail 边复制给
      首、末两节（任一阶段认不出都该走原来的败口线）。
    · 循环体 steps（顺序行）：原地替换成三行。
    · 没有 pick_pair 节点 = 原对象直接返回（不拷不抖，存量流程零开销）。
    · 换算发生在 deep-copy 上：config 里的原始 dict 到用户点保存前都不被动。"""
    if not _has_pair(flow.get("nodes")):
        return flow
    import copy
    flow = copy.deepcopy(flow)

    def splice(nodes, edges):
        """nodes 就地换算（edges=None = 循环体顺序行，只换不接线）。返回新 id 列表。"""
        made = []
        i = 0
        while i < len(nodes):
            n = nodes[i]
            if not (isinstance(n, dict) and _is_pair_node(n)):
                if isinstance(n, dict) and n.get("steps"):
                    splice(n["steps"], None)
                i += 1
                continue
            trio = _pair_step_nodes(n)
            x, y = n.get("x", 0), n.get("y", 0)
            for k, s in enumerate(trio):
                s["id"] = _graph_id()
                s["x"], s["y"] = x + k * 230, y
            made.append((n.get("id"), trio))
            nodes[i:i + 1] = trio
            i += 2
        return made

    mapping = splice(flow.get("nodes"), flow.get("edges"))
    edges = flow.get("edges") or []
    extra = []
    for old_id, trio in mapping:
        a, c = trio[0]["id"], trio[2]["id"]
        for e in edges:
            if e.get("to") == old_id:
                e["to"] = a
            elif e.get("from") == old_id:
                if e.get("port") == "fail":
                    e["from"] = c          # 末节接原线，首节的补一条
                    extra.append({**e, "from": a})
                else:
                    e["from"] = c
    edges.extend(extra)
    return flow


def _drop_popup_zones(flow: dict) -> dict:
    """独立「弹窗区」节点一次性摘除（2026-09-25 用户拍板：功能删掉，清弹窗场景
    交给并行监视 / 循环体「清弹窗」行）：
    · 图上 X 的入边改接 X 的出边目标（多入多出按笛卡尔积补线，重复边剔除）；
      X 的「N」败口出线直接丢弃 —— 节点没了败口永不产结论，留线 = 下游死等。
    · 循环体 steps 里的 popup_zone 行是「清弹窗」（同名不同物），绝不碰。
    · 没有该节点 = 原对象直接返回（幂等，存量流程零开销）。
    · 换算发生在 deep-copy 上：config 里的原始 dict 到用户点保存前都不被动。"""
    if not any(isinstance(n, dict) and n.get("type") == "popup_zone"
               for n in (flow.get("nodes") or [])):
        return flow
    import copy
    flow = copy.deepcopy(flow)
    nodes = flow["nodes"]
    edges = flow.get("edges") or []
    for x in [n for n in nodes if isinstance(n, dict)
              and n.get("type") == "popup_zone"]:
        xid = x.get("id")
        ins = [e for e in edges if e.get("to") == xid]
        outs = [e for e in edges if e.get("from") == xid
                and e.get("port", "out") == "out"]
        edges[:] = [e for e in edges
                    if e.get("to") != xid and e.get("from") != xid]
        for ie in ins:
            for oe in outs:
                cand = {**ie, "to": oe.get("to")}
                if cand not in edges:
                    edges.append(cand)
        nodes.remove(x)
    flow["edges"] = edges
    return flow


def _load_fixups(flow: dict) -> dict:
    """载入即换算链（ensure_graph 各通道共用）：pick_pair 拆三 → 弹窗区摘除。"""
    return _drop_popup_zones(_splice_pick_pair(flow))


# 结构迁移链槽位（分发审计 P1-⑬）：将来 v2→v3 这类结构性变更，在这里登记
# `_MIGRATIONS[2] = fn`（fn 纯函数：吃旧 dict 吐新 dict 并把 version 升上去），
# 老流程载入时经 ensure_graph 自动升一遍，升级不炸老流程。幂等靠版本号：
# 换算完 version 已升，下次载入不会再进这条。当前为空表 = 行为零变化。
_MIGRATIONS: dict = {}


def _run_migrations(flow: dict) -> dict:
    v = flow.get("version", 2)
    while v in _MIGRATIONS:
        flow = _MIGRATIONS[v](flow)
        nv = flow.get("version", v + 1)
        if nv <= v:   # fn 忘了升版本号 = 死循环；宁可当场停也不空转
            break
        v = nv
    return flow


def ensure_graph(flow: dict) -> dict:
    """
    v1 嵌套树 → v2 自由图（nodes + edges）；已是 v2 原样返回。
    无 Qt 依赖，编辑器与运行时共用。迁移单向：保存后即 v2，旧版读不了。
    v2 里残留的旧 pick_pair 节点顺手换算、独立弹窗区节点顺手摘除（见 _load_fixups）。
    v2 之后再挂 _MIGRATIONS 链（见上），升版本改结构有地方接。
    """
    flow = flow or {}
    _v = flow.get("version")
    if isinstance(_v, (int, float)) and _v >= 2:
        # ≥2 都走图格式通道（将来 _MIGRATIONS 升到 v3 后重入也走这里，不回落 v1）
        return _run_migrations(_load_fixups(flow))
    nodes = flow.get("nodes") or []
    if nodes:
        # 半迁移：有 nodes 无 version 也按 v2 走（防手改文件丢 version）
        flow.setdefault("version", 2)
        return _run_migrations(_load_fixups(flow))
    steps = flow.get("steps") or []
    # v1 树 → v2：递归展开
    out_nodes, out_edges = [], []
    _walk_v1(steps, None, "out", out_nodes, out_edges)
    return _run_migrations(_load_fixups(
        {"version": 2, "nodes": out_nodes, "edges": out_edges}))


def _walk_v1(nodes, parent_id, parent_port, out_nodes, out_edges):
    """递归把 v1 节点树转成 v2 图。parent_id 为 None = 入口（无入边）。
    v1 里 click/debug_print 的 x/y 是内容坐标（点击/取色），v2 里 x/y 变成了
    画布坐标 —— 迁移时把内容坐标改名 tap_x/tap_y（不带则留默认），
    画布坐标重新按索引给出。

    顺序语义：v1 同层兄弟是顺序链（上一步完成才走下一步），迁移后必须连成
    prev→cur 的 out 边——否则多个无入边节点在 GraphStep 里会同时开始（并行），
    顺序流程变成并发流程。branch 子链首节点接父的 true/false 边，子链内部
    兄弟之间同样是顺序 out 边。"""
    prev_id = None   # 同层前一个兄弟（顺序链的前驱）
    for i, n in enumerate(nodes):
        nid = _graph_id()
        n = dict(n)  # copy，不改原 dict
        if n.get("type") in ("click", "debug_print"):
            # 内容坐标改名保护，不为画布坐标腾位而丢
            n.setdefault("tap_x", n.get("x", 0))
            n.setdefault("tap_y", n.get("y", 0))
        n["x"] = 40 + i * 20
        n["y"] = 40 + i * 20
        n["id"] = nid
        out_nodes.append(n)
        if parent_id is not None:
            out_edges.append({"from": parent_id, "port": parent_port, "to": nid})
        if prev_id is not None:
            out_edges.append({"from": prev_id, "port": "out", "to": nid})
        if n.get("type") == "branch":
            # true/false 子链 → true/false 出边（去重防重复 id 引用）
            seen = set()
            for child in (n.get("true") or []) + (n.get("false") or []):
                if id(child) in seen:
                    continue
                seen.add(id(child))
            _walk_v1(n.get("true") or [], nid, "true", out_nodes, out_edges)
            _walk_v1(n.get("false") or [], nid, "false", out_nodes, out_edges)
        prev_id = nid


def expand_watch_containers(graph: dict) -> dict:
    """
    watch 容器节点 → 每组展开成一台独立哨兵节点（编译期现算，不落盘）。

    多监视合并改版：编辑器可把若干并行监视存进一个容器（nodes 里一个 watch
    节点，rows 等行数据挪进 groups=[{id, rows, cooldown_secs, fail_limit},...]）。
    执行语义刻意不变：容器只是画布收纳盒，展开后组与组之间仍走拍尾通道的
    轮转+多链串行闸（和今天拆成多个节点一模一样）；组内行序才是稳定优先级。

    - 无 groups 的老格式原样返回（同一对象，早退零开销）——旧数据行为逐字不变
    - 组保留自己的 id：老数据里 from=组id 的「命中」废线（信号线通道已下线，
      编译层剔除）指得展开后的组，不撞车；容器顶层残留的 rows/冷却字段被丢弃
      （有 groups 时以组为准，两份都跑 = 同屏双点）
    - groups=[] 的空容器展开为 0 台
    - 容器自己的节点 id 不算"已用"：合并时首组就冠容器原 id；
      容器本体消失后那个位置由组来接
    - 组 id 缺失或与任何存留节点/已展开的组撞车 → 现编新 id（宁丢该组悬空线，
      不双节点抢一 id）
    """
    nodes = graph.get("nodes") or []

    def _is_cont(n):
        return (isinstance(n, dict) and n.get("type") == "watch"
                and isinstance(n.get("groups"), list))

    if not any(_is_cont(n) for n in nodes):
        return graph
    used = {n.get("id") for n in nodes if isinstance(n, dict) and not _is_cont(n)}
    out = []
    for n in nodes:
        if not _is_cont(n):
            out.append(n)
            continue
        for g in n["groups"]:
            if not isinstance(g, dict):
                continue
            child = {k: v for k, v in n.items()
                     if k not in ("id", "groups", "rows",
                                  "cooldown_secs", "fail_limit", "scan_mode")}
            child.update({k: v for k, v in g.items() if k != "id"})
            gid = g.get("id")
            if not gid or gid in used:
                gid = _graph_id()
            child["id"] = gid
            used.add(gid)
            out.append(child)
    return {**graph, "nodes": out}


def subgraph_from(graph: dict, node_id: str) -> dict:
    """「从此节点开始测」的裁剪：保留 node_id 及其下游可达子图，其余节点
    和边整段摘掉。零入度的起点在 GraphStep 里开局即点亮，下游分支口/失败口/
    成环全部原样，指向被裁节点的悬空边由编译层剔除（_compile_graph_impl 扫边）。
    纯函数不碰画布原图；起点 id 不在图里时原样返回（防御手改配置）。"""
    nodes = graph.get("nodes") or []
    if not any(n.get("id") == node_id for n in nodes):
        return graph
    edges = graph.get("edges") or []
    # BFS 沿出线找可达集：多入口/孤岛图各起一轮也不漏（与 detect_cycle 同口径）
    outs = {}
    for e in edges:
        outs.setdefault(e.get("from"), []).append(e.get("to"))
    keep = set()
    stack = [node_id]
    while stack:
        nid = stack.pop()
        if nid in keep:
            continue
        keep.add(nid)
        stack.extend(outs.get(nid, []))
    sub_nodes = [n for n in nodes if n.get("id") in keep]
    sub_edges = [e for e in edges
                 if e.get("from") in keep and e.get("to") in keep]
    return {**graph, "nodes": sub_nodes, "edges": sub_edges}


def detect_cycle(graph: dict) -> list:
    """
    拓扑排序检环：返回环上的节点 id 列表；无环返回 []。
    仅供编辑器校验提醒用——引擎（GraphStep）不拒环：成环 = 循环执行
    （回边不挡首拍、收口节点收到送达回炉重跑），出口靠「结束流程」/失败口/
    看门狗。用户拍板："流程可以成环，已经有测试提醒就行"。
    """
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    out = {}
    indeg = {}
    for n in nodes:
        out[n] = []
        indeg[n] = 0
    for e in graph.get("edges", []):
        f, t = e.get("from"), e.get("to")
        if e.get("port") == "hit":
            continue   # 老「命中」废线不参与拓扑（信号线通道已下线，编译层
                       # 也剔除）：显式跳过防存量数据在编辑器造出假环误报
        if f in nodes and t in nodes:
            out[f].append(t)
            indeg[t] += 1
    # Kahn：不断去掉入度 0 的节点
    from collections import deque
    q = deque([n for n, d in indeg.items() if d == 0])
    removed = 0
    while q:
        n = q.popleft()
        removed += 1
        for t in out[n]:
            indeg[t] -= 1
            if indeg[t] == 0:
                q.append(t)
    if removed == len(nodes):
        return []
    # 剩下的是环上节点
    return [n for n, d in indeg.items() if d > 0]


def _compile_graph_impl(graph: dict, flow_map: dict, stack: list) -> list:
    """
    图编译主体（compile_graph 与 subflow 递归共用）。stack = 外层已展开的
    流程名链，传给 _make_build_step 做编译期防环。
    """
    graph = expand_watch_containers(ensure_graph(graph))
    # 成环不再拒编（用户拍板）：GraphStep 原生循环执行，编辑器校验条只提醒。
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    # 信号线通道已下线（一刀切暂停后不用接线）：老流程里存的 "hit" 边在
    # 下面剔除——watch 永不收口，hit 边若混进 token 传播会让被连循环死等，
    # 剔除即安全，存量数据零迁移。
    edges = [(e.get("from"), e.get("port", "out"), e.get("to"))
             for e in graph.get("edges", [])
             if e.get("from") in nodes and e.get("to") in nodes
             and e.get("port") != "hit"]
    # 败口扫边：只有 FAILABLE 类型认「fail」出线（其余类型永不返回 failed，
    # 留线 = 下游死等）；非法 fail 边（手改 config 造出来的）编译期直接剔除
    fail_srcs = {f for f, p, t in edges
                 if p == "fail" and nodes[f].get("type") in FAILABLE_TYPES}
    edges = [e for e in edges if e[1] != "fail" or e[0] in fail_srcs]
    branch_conds = {}
    for n in nodes.values():
        if n.get("type") == "branch":
            # 条件清单（cond=第1条 + extra_conds=第2..N条，branch_cond_items
            # 一份口径）。缺图降级推广到多条件：「满足全部」任一条缺图 → 整体
            # 恒假（缺图是真问题，宁可不跑别跑错支，单条件=旧行为逐字保留）；
            # 「满足任一」缺图条单独记假、其余照判，全缺才恒假。
            from bot import template_deps
            items = template_deps.branch_cond_items(n)
            logic = n.get("logic", "all") if len(items) > 1 else "all"
            missing = [s for s in (_cond_missing_note(cd) for cd in items) if s]
            # 恒假时 cond=None → GraphStep 恒选 false（确定性不静默，timeout 0
            # 秒收口）。正常条件 → 闭包。第三元 = 走 N 收口瞬间的诊断回调
            # （_make_branch_diag，打日志找原因；None = 不打）。
            if missing and (logic == "all" or len(missing) == len(items)):
                branch_conds[n["id"]] = (
                    None, 0.0, _diag_missing_branch(n, "；".join(missing) + "，恒走N"))
            else:
                cond = _make_branch_condition(n)
                ent = (cond, float(n.get("timeout", 5)), _make_branch_diag(n))
                if str(n.get("on_timeout", "")) == "scroll":
                    # 滑动翻页继续找（与识字取物同款）：识别窗口到期滑一页、停稳
                    # 重开窗口再投，滑次用尽才走N。ms 编辑器不出这个口，
                    # 与 server_pick 编译侧同默认 400。缺图恒假条不配滑动——
                    # 缺模板是真问题，滑屏幕救不回来。
                    ent += ({"dir": str(n.get("scroll_dir", "up")),
                             "px": int(n.get("scroll_px", 300) or 300),
                             "ms": int(n.get("scroll_ms", 400) or 400),
                             "settle": float(n.get("scroll_settle", 0.8) or 0.8),
                             "roi": list(n["scroll_roi"]) if n.get("scroll_roi")
                             else None,
                             "max": max(0, int(n.get("scroll_max", 5) or 5))},)
                branch_conds[n["id"]] = ent
    gs = GraphStep(
        "流程图",
        nodes=nodes,
        edges=edges,
        branch_conds=branch_conds,
        build_step=_make_build_step(flow_map, stack, fail_srcs),
        is_action=_is_action_node,
        # 看门狗：图顶层可选字段 watchdog_sec（秒，0/缺省=关）。整图连续
        # 这么多秒没有任何节点收口 → 判 failed 走 FSM 留证重试。
        # ⚠ 必须大于图内最慢单步自身超时，否则误杀慢步骤（编辑器有防呆提醒）
        stuck_secs=float(graph.get("watchdog_sec", 0) or 0),
    )
    return [gs]


def compile_graph(graph: dict, flow_map: dict | None = None,
                  flow_name: str = "") -> list:
    """
    v2 图 → 步骤链：组装一个 GraphStep 包裹整图执行。成环照常编译
    （GraphStep 循环执行，见 bot/fsm.py GraphStep 契约）。返回 [GraphStep]。
    flow_map：subflow 节点解析用的全部流程 {名: 图}；None = 现场读 config
    （试运行传的是画布未保存快照 + config 存量，见 bot/flow_test.py）。
    flow_name：本图自己的流程名 —— 必须垫进防环栈底，否则 A 里的
    「子流程→A」自引用查栈查不到，编译直接无限递归（爆栈）。
    """
    if flow_map is None:
        flow_map = get_config().get("custom_flows", {}) or {}
    stack = [flow_name] if flow_name else []
    return _compile_graph_impl(graph, flow_map, stack)


# ── 任务类 ────────────────────────────────────────────────────────────

class FlowTask:
    name = "flow"
    display_name = "自定义流程"
    # 编排出来的流程引用哪些模板，全看用户在流程编辑器里连了什么节点，
    # 没有静态清单可声明 —— required_templates 留空。缺图不在这拦，而是在
    # 编译期 _action_step/_make_build_step 逐节点 has_template 查，缺的降级成 WARN
    # LogStep 跳过（缺图不炸流程，只留日志）。启动预检对 flow 无能为力，
    # 靠的就是这条编译期降级兜底（见 _tpl_missing）。
    category = "自定义流程"  # 模板库目录 = 任务中文显示名（库点挂机目录改版）
    required_templates: list = []
    param_schema = {
        "flow_name": {
            "label": "选择流程",
            "type": "choice",
            "tooltip": "在流程编辑器里编排并保存后，这里会出现流程名",
            "options_from": "custom_flows",  # 动态：任务设置页从 config 取流程名列表
            "default": "",
        },
    }

    def build_steps(self, ctx, params=None):
        params = params or {}
        name = params.get("flow_name", "")
        flows = get_config().get("custom_flows", {}) or {}
        if not name or name not in flows:
            return [LogStep("无流程",
                            f"未选择流程或流程「{name}」不存在", "WARN")]
        return compile_graph(flows[name], flows, flow_name=name)


# ── 败口自检（纯内存，不碰 config/templates/模拟器）─────────────────────
# 用法：python bot/tasks/flow.py     全绿打印 FAILPORT SMOKE PASS，否则退出码 1
# 为什么值得留：成败口传播是图引擎里最容易"静默挂死"的一段（漏写一条入边
# 结论 = 下游死等、tick 永远 running 且不报错），必须有一眼能跑的回归检查。

if __name__ == "__main__":
    from bot.fsm import GraphStep

    class _Ctx:                      # stub 上下文（引擎只用 ctx.log 一处）
        log = None
        frame = None
        ld_index = 0

    class _Fake(Step):               # 恒定返回预设结果的假步骤
        def __init__(self, result):
            super().__init__("fake")
            self.r = result
        def tick(self, ctx):
            return self.r

    def _n(id_, t="click_template"):
        return {"id": id_, "type": t}

    def _run(nodes, edges, results, fails=(), beats=20):
        gs = GraphStep("t", nodes, edges,
                       build_step=lambda nd: None if nd["id"] in _no_step \
                           else _Fake(results[nd["id"]]),
                       is_action=lambda nd: nd["id"] in _act)
        for _ in range(beats):
            r = gs.tick(_Ctx())
            if r != "running":
                return r, gs
        return "TIMEOUT(挂死)", gs

    ok = True
    def check(name, cond):
        global ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)

    # 1) 失败 + 接了败口：fail 口送达、out 口跳过，整图照常走完
    _no_step, _act = set(), {"a"}
    nds = {n["id"]: n for n in (_n("a"), _n("b"), _n("c"))}
    r, gs = _run(nds, [("a", "out", "b"), ("a", "fail", "c")],
                 {"a": "failed", "b": "ok", "c": "ok"})
    check("失败走败口：整图 ok", r == "ok")
    check("失败走败口：败链节点 done、成链 skipped",
          gs.node_state("c") == "done" and gs.node_state("b") == "skipped")
    # 2) 成功：out 送达、fail 边写跳过（漏写 = 下游死等，实测过的静默挂死）
    r, gs = _run(nds, [("a", "out", "b"), ("a", "fail", "c")],
                 {"a": "ok", "b": "ok", "c": "ok"})
    check("成功：败链被熄灭且整图不挂死",
          r == "ok" and gs.node_state("b") == "done" and gs.node_state("c") == "skipped")
    # 3) 没接败口的失败 = 维持原语义，整图 failed 交 FSM 留证
    r, gs = _run({k: dict(v) for k, v in nds.items() if k != "c"},
                 [("a", "out", "b")], {"a": "failed", "b": "ok"})
    check("未接败口：整图 failed（行为与旧版一致）", r == "failed")
    # 4) 上游整体跳过：跳过信号必须连败口一起传播（漏 = 汇合点死等）
    _no_step, _act = {"x"}, set()
    nds4 = {n["id"]: n for n in (_n("x", "empty"), _n("a"), _n("c"))}
    r, gs = _run(nds4, [("x", "out", "a"), ("a", "fail", "c")],
                 {"a": "ok", "c": "ok"})
    check("全跳过链含败口不死等", r == "ok" and gs.node_state("c") == "skipped")
    # 5) 编译层让位：接败口 → 不再注入「超时退出程序」fatal 回调
    step_nofail = _action_step({"type": "server_pick", "id": "a"})
    step_fail = _action_step({"type": "server_pick", "id": "a"}, has_fail=True)
    check("server_pick 未接败口有 fatal", step_nofail.fatal_cb is not None)
    check("server_pick 接败口 fatal 让位", step_fail.fatal_cb is None)
    # wait_template 要先绕过缺图降级（假模板名会变 LogStep），只验 fatal 注入
    _tm_real = _tpl_missing
    globals()["_tpl_missing"] = lambda c, n: False
    # 「点击后加延迟」的延时值也打桩（2 秒），不读真 config
    _gc_real = get_config
    globals()["get_config"] = lambda: type("_Cfg", (), {
        "get": lambda self, k, d=None: 2.0})()
    try:
        wt_plain = _action_step({"type": "wait_template", "name": "t", "category": "x"})
        wt_fail = _action_step({"type": "wait_template", "name": "t",
                                "category": "x"}, has_fail=True)
        # 反证：接败口但 on_timeout=continue 本就没 fatal，两者都不该抛
        ct = _action_step({"type": "click_template", "name": "t", "category": "x"},
                          has_fail=True)
        # 5b) 点击后加延迟（缺省勾上，与 wait_before 同款）：勾了/没这键 →
        # _SequenceStep[动作, 等待2s]；显式取消勾选/勾了但全局值 0 → 原样单 ActionStep
        d_on = _action_step({"type": "click_template", "name": "t", "category": "x",
                             "delay_after_click": True})
        d_default = _action_step({"type": "click_template", "name": "t", "category": "x"})
        d_off = _action_step({"type": "click_template", "name": "t", "category": "x",
                              "delay_after_click": False})
        globals()["get_config"] = lambda: type("_Cfg", (), {
            "get": lambda self, k, d=None: 0})()
        d_zero = _action_step({"type": "click_template", "name": "t", "category": "x",
                               "delay_after_click": True})
    finally:
        globals()["_tpl_missing"] = _tm_real
        globals()["get_config"] = _gc_real
    check("wait_template 未接败口有 fatal", wt_plain.on_timeout_fatal is not None)
    check("wait_template 接败口 fatal 让位", wt_fail.on_timeout_fatal is None)
    # ct 现在默认被延迟串步包了一层（缺省勾上），fatal 挂在内层动作步上
    _ct_inner = ct._steps[0] if isinstance(ct, _SequenceStep) else ct
    check("click_template 接败口 fatal 让位", _ct_inner.on_gate_timeout_fatal is None)
    check("点击后加延迟：勾了 = 动作+等待两步串成单步",
          isinstance(d_on, _SequenceStep) and len(d_on._steps) == 2
          and isinstance(d_on._steps[1], WaitStep)
          and d_on._steps[1].fixed_wait == 2.0)
    check("点击后加延迟：没这键（老节点）= 默认开，同样串等待",
          isinstance(d_default, _SequenceStep)
          and d_default._steps[1].fixed_wait == 2.0)
    check("点击后加延迟：显式取消勾选 = 原样单动作步",
          not isinstance(d_off, _SequenceStep))
    check("点击后加延迟：全局值设 0 = 等价不勾，不白挂等待步",
          not isinstance(d_zero, _SequenceStep))
    # 5c) _SequenceStep 驱动语义：前一步 running 不许跳下一步；failed 原样上抛
    seq_seen = []
    sq = _SequenceStep("s", [_Fake("ok"), _Fake("failed")])
    check("串步：第一步 ok 才推进，第二步 failed 原样上抛",
          sq.tick(_Ctx()) == "failed" and sq._i == 1)
    class _R1(Step):        # 第一步 running 一次后转 ok
        def __init__(self): super().__init__("r1"); self.n = 0
        def tick(self, ctx):
            self.n += 1
            return "running" if self.n == 1 else "ok"
    sq2 = _SequenceStep("s2", [_R1(), _Fake("ok")])
    check("串步：前步 running 时整步 running（不抢跑）",
          sq2.tick(_Ctx()) == "running" and sq2._i == 0
          and sq2.tick(_Ctx()) == "ok")

    # 6) 看门狗：单步恒 running 的卡死图，限时内必判 failed；不配则不误伤
    import time as _t
    nds6 = {_n("a")["id"]: _n("a")}
    gs6 = GraphStep("t", nds6, [], build_step=lambda nd: _Fake("running"),
                    is_action=lambda nd: False, stuck_secs=0.05)
    r6 = "running"
    _dead = _t.monotonic() + 2.0
    while _t.monotonic() < _dead and r6 == "running":
        r6 = gs6.tick(_Ctx())
    check("看门狗限时内判 failed", r6 == "failed")
    gs6.reset()
    check("reset 后看门狗时钟复位（恢复链不被旧时钟秒杀）",
          gs6.tick(_Ctx()) == "running")
    gs7 = GraphStep("t", nds6, [], build_step=lambda nd: _Fake("running"),
                    is_action=lambda nd: False)   # 不配看门狗
    check("不配看门狗：卡死图仍 running（存量行为不变）",
          gs7.tick(_Ctx()) == "running")
    # 7) 编译层透传：graph 顶层 watchdog_sec → GraphStep._stuck_secs
    _g = {"version": 2, "nodes": [{"id": "a", "type": "debug_print"}],
          "edges": [], "watchdog_sec": 45}
    _st = compile_graph(_g, {}, "t")
    check("编译层透传 watchdog_sec", getattr(_st[0], "_stuck_secs", -1) == 45.0)
    _g.pop("watchdog_sec")
    _st = compile_graph(_g, {}, "t")
    check("缺省不带 watchdog_sec = 关", _st[0]._stuck_secs == 0.0)

    # 8) 并行监视哨兵：永不收口；每拍必扫（扫描不落手）；子链在跑（_pending）=
    #    一刀切暂停（2026-09-25 拍板）：主线动作与纯评估全部冻结，收口自动续跑
    class _WFake(Step):          # 哨兵风格步：带 _pending 属性（takeover 看它）
        def __init__(self):
            super().__init__("wf")
            self.tapped = False
            self.beats = 0
            self._pending = None     # None=纯扫描拍；非 None=子链在跑
        def tick(self, ctx):
            self.beats += 1
            return "running"
    class _AFake(Step):          # 主线动作步：恒 running，数被推进了几拍
        def __init__(self):
            super().__init__("af")
            self.count = 0
        def tick(self, ctx):
            self.count += 1
            return "running"
    _wn = {"w": {"id": "w", "type": "watch"}}
    _an = {"a": {"id": "a", "type": "click_template"}}
    _both = {**_wn, **_an}

    wf1 = _WFake(); af1 = _AFake()
    gs8 = GraphStep("t", _both, [],
                    build_step=lambda nd: af1 if nd["id"] == "a" else wf1,
                    is_action=lambda nd: nd["id"] == "a")
    gs8.tick(_Ctx())
    check("每拍必扫：主线落了手的拍哨兵照样扫描",
          af1.count == 1 and wf1.beats == 1)
    af1.count = 0
    wf1._pending = (0, None)         # 模拟子链在跑
    gs8.tick(_Ctx())
    check("takeover：子链在跑时主线动作让路（整拍不推进）",
          af1.count == 0 and wf1.beats == 2)
    wf1._pending = None              # 子链收口
    gs8.tick(_Ctx())
    check("恢复：子链收口后主线动作自动续跑", af1.count == 1)
    # 一刀切补强：纯评估节点（老语义不受让路闸管）在子链期间同样冻结
    class _EFake(_AFake):        # 纯评估风格步：is_action=False
        pass
    wf1._pending = None; af1.count = 0
    ef1 = _EFake()
    gs8e = GraphStep("t", {**_both, "e": {"id": "e", "type": "debug_print"}}, [],
                     build_step=lambda nd: (af1 if nd["id"] == "a" else
                                            ef1 if nd["id"] == "e" else wf1),
                     is_action=lambda nd: nd["id"] == "a")
    gs8e.tick(_Ctx())
    check("无子链时纯评估照常推进", ef1.count == 1 and af1.count == 1)
    wf1._pending = (0, None)
    gs8e.tick(_Ctx())
    check("一刀切：子链在跑时纯评估节点也整拍冻结",
          ef1.count == 1 and af1.count == 1)
    wf1._pending = None
    gs8e.tick(_Ctx())
    check("纯评估收口后自动续跑", ef1.count == 2)
    wf1._pending = None; af1.count = 0
    _ok = _Fake("ok")
    wf1.beats = 0; af2 = _AFake()
    gs8b = GraphStep("t", _both, [],
                     build_step=lambda nd: _ok if nd["id"] == "a" else wf1,
                     is_action=lambda nd: nd["id"] == "a")
    r8 = gs8b.tick(_Ctx())
    check("哨兵不阻止图完成（主线全 done 即 ok）",
          r8 == "ok" and gs8b.node_state("w") == "running")
    # 收口补闸：done 判定当拍子链若在办，不判 ok（否则线程即退、半截子链陪葬）
    wf1._pending = (0, None)
    check("子链办理中不收口（主线全 done 也等它办完）",
          gs8b.tick(_Ctx()) == "running")
    wf1._pending = None
    check("子链收口后补判 done", gs8b.tick(_Ctx()) == "ok")
    # scan_mode 两型（2026-10-05 用户拍板）：restart=每轮重头扫高优先恒压，
    # continue=命中后游标推到下一行轮转（低优先不被饿死）。tap 行最省事：
    # 不起链，直接看连拍各打了哪行。
    _seen_taps = []
    _orig_tp, _orig_tap = template_present, tap_template
    template_present = lambda ctx, c, n: True
    tap_template = lambda ctx, c, n, *o: (_seen_taps.append(n), True)[1]
    _two = [{"category": "c", "name": "A", "kind": "tap"},
            {"category": "c", "name": "B", "kind": "tap"}]
    _wr = WatchStep("wr", [dict(r) for r in _two], [[] for _ in _two],
                    cooldown_secs=0, fail_limit=9, scan_ttl=0.0)
    _wc = WatchStep("wc", [dict(r) for r in _two], [[] for _ in _two],
                    cooldown_secs=0, fail_limit=9, scan_ttl=0.0,
                    scan_mode="continue")
    for _ in range(3):
        _wr.tick(_Ctx())
    check("scan_mode=restart：三拍全打第①行（老行为不变）",
          _seen_taps == ["A", "A", "A"])
    _seen_taps.clear()
    for _ in range(3):
        _wc.tick(_Ctx())
    check("scan_mode=continue：轮转 A→B→A，低优先不饿死",
          _seen_taps == ["A", "B", "A"])
    template_present, tap_template = _orig_tp, _orig_tap
    # takeover 期间看门狗放行：主线动作恒 running 永不收口，哨兵子链在跑
    # 持续超过看门狗限时 → 仍 running（误杀 = 弹窗子链把整图判死）
    wf1._pending = (0, None)
    gs8c = GraphStep("t", _both, [],
                     build_step=lambda nd: _AFake() if nd["id"] == "a" else wf1,
                     is_action=lambda nd: nd["id"] == "a", stuck_secs=0.05)
    r8c = "running"
    _wd = _t.monotonic() + 0.15
    while _t.monotonic() < _wd and r8c == "running":
        r8c = gs8c.tick(_Ctx())
    check("takeover 期间看门狗放行（弹窗子链不被误杀成卡死）",
          r8c == "running")
    wf1._pending = None
    # 子流程「主流程监视照管」开关（bg_watch=False，用户拍板 2026-10-05）：
    # 停哨子流程在跑时本图 watch 哨兵停扫；已起跑的链照常推进；timer 不殃及
    class _HFake(_AFake):        # 扮演跑中的 bg_watch=False 子流程节点
        suppresses_watch = True
    wf1.beats = 0
    h1 = _HFake()
    _hn = {**_wn, "s": {"id": "s", "type": "subflow"}}
    gs8h = GraphStep("t", _hn, [],
                     build_step=lambda nd: h1 if nd["id"] == "s" else wf1,
                     is_action=lambda nd: nd["id"] == "s")
    gs8h.tick(_Ctx())
    check("停哨子流程在跑：watch 哨兵停扫（不落手也不起链）", wf1.beats == 0)
    wf1._pending = (0, None)     # 已起跑的哨兵子链：不该被停哨令半路掐
    gs8h.tick(_Ctx())
    check("已起跑哨兵链照常推进到办完", wf1.beats == 1)
    wf1._pending = None
    h1.suppresses_watch = False  # 同节点改回照管（默认）→ 哨兵复扫
    gs8h.tick(_Ctx())
    check("bg_watch 默认照管：哨兵恢复每拍必扫", wf1.beats == 2)
    _tn = {"w": {"id": "w", "type": "timer"}, "s": {"id": "s", "type": "subflow"}}
    wf1.beats = 0
    h1.suppresses_watch = True
    gs8t = GraphStep("t", _tn, [],
                     build_step=lambda nd: h1 if nd["id"] == "s" else wf1,
                     is_action=lambda nd: nd["id"] == "s")
    gs8t.tick(_Ctx())
    check("timer 定时节点不殃及（停哨只停盯弹窗的）", wf1.beats == 1)
    h1.suppresses_watch = False
    _sf_map = {"子": {"version": 2, "nodes": [], "edges": []}}
    check("编译层字段直达：bg_watch=False → 步骤带停哨标",
          _make_build_step(_sf_map, ["主"])(
              {"id": "s", "type": "subflow", "flow_name": "子",
               "bg_watch": False}).suppresses_watch)
    check("没填字段=照管（存量流程行为零变化）",
          not _make_build_step(_sf_map, ["主"])(
              {"id": "s", "type": "subflow", "flow_name": "子"}).suppresses_watch)

    # 8b) 循环跨轮复位：body 编译产物每轮复用，上一轮残留状态必须清，
    #     否则第二轮"空跑收口"不再落手（实测：无限循环只点第一下）
    from bot.fsm import LoopStep
    class _RSteps(Step):         # 假动作步：tick 一次记一次，reset 记复位次数
        def __init__(self):
            super().__init__("rs")
            self.ticks = 0
            self.resets = 0
        def tick(self, ctx):
            self.ticks += 1
            return "ok" if self.ticks % 2 else "running"   # 两拍一轮
        def reset(self):
            super().reset()
            self.resets += 1
    rs = _RSteps()
    lp = LoopStep("循", [rs])
    lp.tick(_Ctx())
    for _ in range(3):
        lp.tick(_Ctx())          # 一轮收尾 + 下一轮起步
    check("循环每轮起跑前复位 body 步骤（不再空跑收口）",
          lp._count >= 1 and rs.resets >= lp._count)

    # 8c) 信号线通道下线（一刀切暂停，2026-09-25 拍板）：LoopStep 不再自挂起；
    #     子链在跑时暂停闸把主线循环整条冻结、收口自动续跑；老流程里存的
    #     "hit" 线编译层剔除，数据零迁移不炸图。
    from bot.fsm import LoopStep as _LS
    class _WSig(Step):           # 哨兵风格步：第 fire_at 拍起跑子链、done_at 拍收口
        def __init__(self, fire_at, done_at):
            super().__init__("ws")
            self.tapped = False
            self._pending = None
            self.beats = 0
            self.fire_at, self.done_at = fire_at, done_at
        def tick(self, ctx):
            self.beats += 1
            if self.beats == self.fire_at:
                self._pending = (0, None)      # 命中起跑
            elif self.beats == self.done_at:
                self._pending = None           # 子链收口
            return "running"
    _sn = {"w": {"id": "w", "type": "watch"}, "l": {"id": "l", "type": "loop"}}
    ws3 = _WSig(fire_at=1, done_at=99)
    rs3 = _RSteps()
    lp3 = _LS("循", [rs3])
    check("LoopStep 自挂起机制已下线（pause/_paused 全除）",
          not hasattr(lp3, "pause") and not hasattr(lp3, "_paused"))
    gs8c2 = GraphStep("t", _sn, [],
                      build_step=lambda nd: lp3 if nd["id"] == "l" else ws3,
                      is_action=lambda nd: nd["id"] == "l")
    for _ in range(3):
        gs8c2.tick(_Ctx())       # 拍1 loop 推进+watch 起链；拍2/3 冻结
    check("子链在跑：主线循环被暂停闸整条冻结（不接线默认生效）",
          rs3.ticks == 1)
    ws3.done_at = ws3.beats + 1  # 令子链下一拍收口
    # 收口拍 + LoopStep 跨轮固有的一拍"上轮 FSM 收账"（旧有节拍，非暂停闸）
    for _ in range(3):
        gs8c2.tick(_Ctx())
    check("子链收口后自动恢复推进（内部状态从未重置=从暂停处续跑）",
          rs3.ticks > 1 and ws3._pending is None)
    # 编译层：老 hit 线剔除（watch 永不收口，混进 token 传播 = 被连循环饿死）
    _g8c = {"version": 2, "nodes": [
                {"id": "w", "type": "watch", "rows": [], "label": ""},
                {"id": "l", "type": "loop", "steps": [], "max_times": 0}],
            "edges": [{"from": "w", "port": "hit", "to": "l", "to_port": "sig"}]}
    _st8c = compile_graph(_g8c, {}, "t")
    check("编译层：老 hit 线干净剔除（edges 无 hit、GraphStep 无 _sig 残留）",
          type(_st8c[0]).__name__ == "GraphStep"
          and not any(p == "hit" for _, p, _ in _st8c[0]._edges)
          and not hasattr(_st8c[0], "_sig"))
    check("带老 hit 线的流程照常跑（不挂死不饿等）",
          _st8c[0].tick(_Ctx()) in ("ok", "running"))
    check("detect_cycle 仍豁免老 hit 线（防存量数据假环误报）",
          detect_cycle(_g8c) == [])

    # 8e) 成环 = 循环执行（用户拍板"不要限制我去运行成环流程"）：
    #     回边不挡首拍（纯环没入口也不死等）、收口节点收到送达回炉重跑、
    #     每拍每节点至多回炉一圈（防纯评估环同拍无限收敛）、编译层不再拒环。
    #     改 GraphStep 回边/传播语义后必跑。
    class _TCount(Step):         # 恒 ok 的假步：bag 记执行次序，看谁跑了几轮
        def __init__(self, tag, bag):
            super().__init__(tag); self.bag = bag
        def tick(self, ctx):
            self.bag.append(self.name); return "ok"
    _cn = {k: {"id": k, "type": "click"} for k in "abc"}
    _cyc_edges = [("a", "out", "b"), ("b", "out", "c"), ("c", "out", "a")]
    bagC = []
    gsC = GraphStep("t", _cn, _cyc_edges,
                    build_step=lambda nd: _TCount(nd["id"], bagC),
                    is_action=lambda nd: False)
    rC = [gsC.tick(_Ctx()) for _ in range(4)]
    check("纯环不被拒编（compile_graph 照常出 GraphStep）",
          type(compile_graph({"version": 2,
                              "nodes": [dict(n) for n in _cn.values()],
                              "edges": [{"from": f, "port": "out", "to": t}
                                        for f, _p, t in _cyc_edges]},
                             {}, "t")[0]).__name__ == "GraphStep")
    check("纯环引擎能起跑（回边不挡首拍，不再是 5 拍 0 步死等）",
          len(bagC) >= 3 and rC[0] == "running")
    # 拍1 转一整圈 abc；此后同拍回炉被 _last_beat 闸拦住，每拍只前进一格
    check("环稳转不爆跑（4 拍 = abcabc，每拍每节点至多一次）",
          "".join(bagC) == "abcabc" and all(x == "running" for x in rC))
    check("回炉被计数（看门狗进展账的数据源）", gsC._rearm_total >= 3)
    # 看门狗特供图：stuck_secs 开着跑纯环 —— done 数恒定，只有回炉在涨；
    # 旧账本下这图必被误杀 failed（常驻挂机恰恰最常见成环）
    bagW = []
    gsW = GraphStep("t", {k: {"id": k, "type": "click"} for k in "abc"}, _cyc_edges,
                    build_step=lambda nd: _TCount(nd["id"], bagW),
                    is_action=lambda nd: False, stuck_secs=5)
    for _ in range(4):
        gsW.tick(_Ctx())
    import time as _t
    gsW._last_progress = _t.monotonic() - 6   # 拨旧时钟：越过 stuck_secs
    # done 数与上次同步值保持一致（纯环实况：恒有 1 个节点在回炉 idle），
    # 让"只有回炉在涨"成为唯一可能的进展来源
    gsW._done_count = sum(1 for r in gsW._nd.values()
                          if r["state"] in ("done", "skipped"))
    check("稳定循环不被看门狗误杀（回炉计入进展，旧账本此处必 failed）",
          gsW.tick(_Ctx()) != "failed" and gsW._rearm_seen >= 3
          and _t.monotonic() - gsW._last_progress < 1)
    # 带入口的环：入口走一次，进环后一圈圈转
    bagD = []
    _dn = {k: {"id": k, "type": "click"} for k in "sab"}
    gsD = GraphStep("t", _dn, [("s", "out", "a"), ("a", "out", "b"), ("b", "out", "a")],
                    build_step=lambda nd: _TCount(nd["id"], bagD),
                    is_action=lambda nd: False)
    for _ in range(5):
        gsD.tick(_Ctx())
    check("入口链进环（s 只跑一次，a/b 循环圈）",
          "".join(bagD).startswith("sab") and bagD.count("s") == 1
          and bagD.count("a") >= 3)
    # 分支没走的环：入口边判「跳过」→ 整条链熄灭；回边从不送跳过，没走的路不复活
    bagE = []
    _en = {"s": {"id": "s", "type": "branch"}, "a": {"id": "a", "type": "click"},
           "b": {"id": "b", "type": "click"}}
    gsE = GraphStep("t", _en, [("s", "true", "a"), ("a", "out", "b"), ("b", "out", "a")],
                    branch_conds={"s": (lambda ctx: False, -1.0)},
                    build_step=lambda nd: _TCount(nd["id"], bagE),
                    is_action=lambda nd: False)
    rE = [gsE.tick(_Ctx()) for _ in range(3)]
    check("分支没走的环：整条链熄灭、图收口（跳过不回炉）",
          bagE == [] and rE[-1] == "ok")
    # 环上汇合点：非回边入槽保留上一轮结论（送达继续转圈，不用等回边重送）
    check("回炉保留入边结论（环靠旧账直接转下一圈）",
          gsC._nd["a"]["concl"] == [1])

    # 8f) 多链串行闸：两条都在跑子链的哨兵同拍 → 只推进一条（后到的整拍排队），
    #     否则两链各点一下 = 同拍两次落手（违反引擎红线）。watch×watch /
    #     watch×timer 同理，共用一份判定。
    class _CFake(Step):          # 恒在跑子链的哨兵步：数被推进几拍，拍标记 tapped
        def __init__(self, tag):
            super().__init__(tag)
            self.tapped = False
            self.beats = 0
            self._pending = (0, None)   # 全程有链（模拟子链跨拍续跑）
        def tick(self, ctx):
            self.beats += 1
            self.tapped = True          # 续跑落手（真链每拍大概率点屏）
            return "running"
    c1, c2 = _CFake("c1"), _CFake("c2")
    _cn = {"w1": {"id": "w1", "type": "watch"}, "w2": {"id": "w2", "type": "timer"}}
    gs8d = GraphStep("t", _cn, [],
                     build_step=lambda nd: c1 if nd["id"] == "w1" else c2,
                     is_action=lambda nd: False)
    gs8d.tick(_Ctx())
    check("多链串行闸：同拍只推进一条在跑的链",
          (c1.beats, c2.beats) in [(1, 0), (0, 1)])
    c1.beats = c2.beats = 0
    for _ in range(4):
        gs8d.tick(_Ctx())
    check("串行闸不饿死：两链交替都被推进（各≥2拍）", c1.beats >= 2 and c2.beats >= 2)

    # 9) WatchStep 行逻辑：命中→跑子链；连败 N 次→停用；done→冷却后才可再命中
    #    （template_present 与 bot.fsm.FSM 全部打桩，不碰真账本/真截图）
    _tp_real, _fsm_real = template_present, None
    import bot.fsm as _fsm_mod
    _fsm_real = _fsm_mod.FSM

    class _StubFSM:              # 假子 FSM：按预置序列吐结论（bodies 塞 list 当序列）
        def __init__(self, body, ctx, **kw):
            self.body, self.seq, self.i = body, body, 0
        def tick(self):
            r = self.seq[min(self.i, len(self.seq) - 1)]
            self.i += 1
            return r
    _fsm_mod.FSM = _StubFSM
    globals()["template_present"] = lambda c, cat, nm, *a, **k: True
    try:
        ws = WatchStep("监视", [{"category": "x", "name": "弹窗A", "flow_name": "f"}],
                       [["failed"]], cooldown_secs=0.05, fail_limit=2)
        check("哨兵第一步：命中即起子链（占落手权）",
              ws.tick(_Ctx()) == "running" and ws.tapped)
        ws.tick(_Ctx())
        _t.sleep(0.07)
        ws.tick(_Ctx())                       # 冷却过后二次命中
        ws.tick(_Ctx())                       # 第二次失败 → 停用
        check("连败 N 次停用该行", ws._off[0] and ws._fails[0] == 2)
        ws.tick(_Ctx())
        check("停用后不再命中（不占落手）", not ws.tapped)
        ws2 = WatchStep("监视", [{"category": "x", "name": "弹窗A", "flow_name": "f"}],
                        [["done"]], cooldown_secs=0.05, fail_limit=3)
        ws2.tick(_Ctx()); ws2.tick(_Ctx())    # 命中 → 子链跑完进冷却
        check("成功收口：fails 清零 + 进冷却",
              ws2._fails[0] == 0 and ws2._pending is None and ws2._cool[0] > 0)
        _t.sleep(0.07)
        check("冷却期满可再命中",
              ws2.tick(_Ctx()) == "running" and ws2._pending is not None)
        # 行内「结束流程」ABORT：只算干完一轮（进冷却），绝不上抛
        ws3 = WatchStep("监视", [{"category": "x", "name": "弹窗A", "flow_name": "f"}],
                        [[ABORT]], cooldown_secs=99, fail_limit=3)
        ws3.tick(_Ctx())
        r3 = ws3.tick(_Ctx())
        check("行内 ABORT 不上抛（哨兵继续在岗）",
              r3 == "running" and ws3._pending is None)
        # 编译期死行（body=None）开局即停用，不占落手
        ws4 = WatchStep("监视", [{"category": "x", "name": "没了", "flow_name": "f"}],
                        [None], cooldown_secs=5, fail_limit=3)
        ws4.tick(_Ctx())
        check("编译期死行开局停用不占落手", ws4._off[0] and not ws4.tapped)

        # 收口续扫（用户拍板）：A 链收口当拍就地再扫，B 也命中 → 同拍起 B 链，
        # takeover 不断档（省一轮"交还主线→下拍又挂起"抖动）；刚收口的 A 已进
        # 冷却，同拍不会自我重触发。B body=["running","done"]：一拍在跑一拍收口。
        wsr = WatchStep("监视", [{"category": "x", "name": "弹窗A", "flow_name": "fa"},
                                 {"category": "x", "name": "弹窗B", "flow_name": "fb"}],
                        [["done"], ["running", "done"]],
                        cooldown_secs=99, fail_limit=3)
        wsr.tick(_Ctx())                       # 拍1：扫到 A → 起 A 链
        check("收口续扫：首发命中 A", wsr._pending is not None and wsr._pending[0] == 0)
        wsr.tick(_Ctx())                       # 拍2：A done → 续扫 → B 命中同拍起链
        check("收口续扫：A 收口当拍直接起 B（不交还主线、不等下一拍）",
              wsr._pending is not None and wsr._pending[0] == 1)
        wsr.tick(_Ctx())                       # 拍3：B running 续跑
        check("收口续扫：B 链在跑（接管持续）", wsr._pending is not None)
        wsr.tick(_Ctx())                       # 拍4：B done → 续扫 A/B 都在冷却 → 扫空
        check("收口续扫：扫空才交还落手权，且刚收口行不自我重触发",
              wsr._pending is None and wsr.tapped
              and wsr._fails == [0, 0])         # 两行都算成功收口
        # 收口续扫 × 一刀切暂停（端到端，真 WatchStep 进真 GraphStep）：续扫
        # 拍不交还主线 → 冻结闸全程不断档；连环弹窗全办完、扫空那拍才解冻。
        # 主线用计数动作步（此段 _StubFSM 桩在身，真 LoopStep 的循环体不执行，
        # 冻结/恢复看计数即可；跨轮节拍问题由 8c 真 FSM 段覆盖）。
        af9 = _AFake()
        wsr2 = WatchStep("监视", [{"category": "x", "name": "弹窗A", "flow_name": "fa"},
                                  {"category": "x", "name": "弹窗B", "flow_name": "fb"}],
                         [["done"], ["done"]], cooldown_secs=99, fail_limit=3)
        _snr = {"w": {"id": "w", "type": "watch"}, "l": {"id": "l", "type": "click_template"}}
        gsr = GraphStep("t", _snr, [],
                        build_step=lambda nd: af9 if nd["id"] == "l" else wsr2,
                        is_action=lambda nd: nd["id"] == "l")
        gsr.tick(_Ctx())                       # 拍1：主线推进；A 命中起链（拍尾）
        _tf = af9.count
        gsr.tick(_Ctx())                       # 拍2：冻结；A done → 续扫 → B 同拍起链
        check("续扫不断档：收口续扫拍主线仍冻结（B 在跑）",
              af9.count == _tf and wsr2._pending is not None and wsr2._pending[0] == 1)
        gsr.tick(_Ctx())                       # 拍3：冻结；B 收口 → 扫空 → 解冻
        check("续扫×暂停：办完扫空当拍主线不抢跑（解冻在下拍）", af9.count == _tf)
        gsr.tick(_Ctx())                       # 拍4：主线恢复
        check("续扫×暂停：连环弹窗全办完扫空后主线才续跑",
              af9.count > _tf and wsr2._pending is None)

        # 命中即点：row["tap_on_hit"] 时子链开头自动插一步"点触发图标"，
        # 子流程体不用再配"点模板"入口。不勾 = 零预置（老行行为不变）。
        _tap_log = []
        _tt_real = globals()["tap_template"]
        globals()["tap_template"] = lambda c, cat, nm, *a, **k: _tap_log.append((cat, nm)) or True
        try:
            ws5 = WatchStep("监视", [{"category": "x", "name": "入口图",
                                      "flow_name": "f", "tap_on_hit": True}],
                            [["done"]], cooldown_secs=99, fail_limit=3)
            ws5.tick(_Ctx())                      # 命中起跑（本拍不落手）
            chain5 = ws5._pending[1].body if ws5._pending else None
            check("命中即点：子链开头自动插一步点击",
                  chain5 is not None and len(chain5) == 2
                  and isinstance(chain5[0], ActionStep))
            for s in chain5[:1]:                  # 驱动预置步落手一次
                s.tick(_Ctx())
            check("预置步真的点到触发图标（tap_template 收到 cat/name）",
                  _tap_log == [("x", "入口图")])
            ws6 = WatchStep("监视", [{"category": "x", "name": "入口图",
                                      "flow_name": "f"}],   # 没勾 tap_on_hit
                            [["done"]], cooldown_secs=99, fail_limit=3)
            ws6.tick(_Ctx())
            check("未勾 tap_on_hit：子链无预置点击（老行零影响）",
                  ws6._pending is not None and len(ws6._pending[1].body) == 1)
            # 勾了即点 + 行有偏移：预置点击步也带着 (dx,dy) 点（与即点行同口径）。
            # 换桩不复原：下面即点行段本来就要重装自己的桩
            _tap_log_off = []
            globals()["tap_template"] = lambda c, cat, nm, *a, **k: (
                _tap_log_off.append((cat, nm) + tuple(a[:2])) or True)
            ws5b = WatchStep("监视", [{"category": "x", "name": "入口图",
                                      "flow_name": "f", "tap_on_hit": True,
                                      "offset_x": -30, "offset_y": 8}],
                             [["done"]], cooldown_secs=99, fail_limit=3)
            ws5b.tick(_Ctx())
            for s in (ws5b._pending[1].body[:1] if ws5b._pending else []):
                s.tick(_Ctx())          # 驱动预置点击步真落手
            check("即点预置步：行偏移传进 tap_template",
                  _tap_log_off == [("x", "入口图", -30, 8)])

            # 即点行（kind="tap"）：命中当拍点掉收工，绝不起链跑子流程
            _tap_log2 = []
            globals()["tap_template"] = lambda c, cat, nm, *a, **k: (
                _tap_log2.append((cat, nm) + tuple(a[:2])) or True)
            ws7 = WatchStep("监视", [{"category": "x", "name": "弹窗X",
                                      "kind": "tap", "flow_name": ""}],
                            [[]], cooldown_secs=0.05, fail_limit=2)
            r7 = ws7.tick(_Ctx())
            check("即点行：命中当拍点掉、不起链（body 空不跑子流程）",
                  r7 == "running" and ws7.tapped and ws7._pending is None
                  and _tap_log2 == [("x", "弹窗X", 0, 0)])
            _t.sleep(0.07)
            check("即点行：点掉后进冷却可再监视",
                  ws7.tick(_Ctx()) == "running" and ws7._fails[0] == 0)
            # 行上采了偏移 → tap_template 收到 (dx,dy)（学点模板，关闭按钮
            # 不在图中心也能点准）
            _tap_log2b = []
            globals()["tap_template"] = lambda c, cat, nm, *a, **k: (
                _tap_log2b.append((cat, nm) + tuple(a[:2])) or True)
            ws7b = WatchStep("监视", [{"category": "x", "name": "角落X",
                                       "kind": "tap", "offset_x": 40,
                                       "offset_y": -12, "flow_name": ""}],
                             [[]], cooldown_secs=0.05, fail_limit=2)
            ws7b.tick(_Ctx())
            check("即点行：偏移随点击传进 tap_template",
                  _tap_log2b == [("x", "角落X", 40, -12)])
            # 连点不中（图标在、点空）→ 连败停用该行
            globals()["tap_template"] = lambda c, cat, nm, *a, **k: False
            ws8 = WatchStep("监视", [{"category": "x", "name": "顽固X",
                                      "kind": "tap", "flow_name": ""}],
                            [[]], cooldown_secs=0.05, fail_limit=2)
            ws8.tick(_Ctx())                     # 第 1 次点空 fails=1
            _t.sleep(0.07)
            ws8.tick(_Ctx())                     # 第 2 次点空 → 停用
            check("即点行：连点不中停用该行", ws8._off[0] and ws8._fails[0] == 2)

            # 封号停用行（kind="ban"）：命中即抛 AccountBannedExit 穿透收工，
            # 不点不跑链不落冷却 —— 留证+写库归 worker 统一做
            _ban_hit = False
            try:
                WatchStep("监视", [{"category": "x", "name": "封号图",
                                    "kind": "ban", "flow_name": ""}],
                          [[]]).tick(_Ctx())
            except AccountBannedExit as e:
                _ban_hit = "封号图" in str(e)
            check("封号行：命中抛 AccountBannedExit（消息带模板名）", _ban_hit)
            # 没命中（图不在）→ 不抛，普通 running 拍
            globals()["template_present"] = lambda c, cat, nm, *a, **k: False
            try:
                check("封号行：未命中不抛（正常扫描拍）",
                      WatchStep("监视", [{"category": "x", "name": "封号图",
                                          "kind": "ban", "flow_name": ""}],
                                [[]]).tick(_Ctx()) == "running")
            finally:
                globals()["template_present"] = lambda c, cat, nm, *a, **k: True
            # 编译层豁免：ban 行没配 flow_name 也不算死行（tap 同款，缺图才算死）。
            # watch 步是节点点亮时才现场编译，直接调 _build_step 取成品；
            # template_present 先压 False —— 点亮若伴随扫描会真抛封号异常。
            _ht_real = template_manager.has_template
            template_manager.has_template = lambda cat, nm: nm == "封号图"
            globals()["template_present"] = lambda c, cat, nm, *a, **k: False
            try:
                _wg = compile_graph(
                    {"version": 2, "nodes": [{"id": "w", "type": "watch", "rows": [
                        {"category": "x", "name": "封号图", "kind": "ban"}]}],
                     "edges": []}, {}, "t")[0]
                _wb = _wg._build_step(_wg._nodes["w"])
                check("编译层：ban 行免 flow_name、body 空占位不判死",
                      isinstance(_wb, WatchStep) and _wb.bodies == [[]]
                      and not _wb._off[0])
            finally:
                template_manager.has_template = _ht_real
                globals()["template_present"] = lambda c, cat, nm, *a, **k: True
        finally:
            globals()["tap_template"] = _tt_real

        # 9a2) scan_ttl（性能审计 P1-⑨）：miss 后 ttl 内不再真扫；期满图在仍可命中
        _sc = []
        globals()["template_present"] = lambda c, cat, nm, *a, **k: (_sc.append(1), False)[1]
        try:
            wst = WatchStep("监视", [{"category": "x", "name": "弹窗T", "kind": "tap",
                                      "flow_name": ""}], [[]], scan_ttl=99)
            wst.tick(_Ctx())
            n1 = len(_sc)
            wst.tick(_Ctx())
            check("scan_ttl：miss 后 ttl 内二次扫描零真扫（不重复全屏匹配）",
                  n1 == 1 and len(_sc) == 1)
            wst2 = WatchStep("监视", [{"category": "x", "name": "弹窗T", "kind": "tap",
                                       "flow_name": ""}], [[]], scan_ttl=0.05)
            wst2.tick(_Ctx())                        # miss → 进 0.05s 复读冷却
            _t.sleep(0.07)
            globals()["template_present"] = lambda c, cat, nm, *a, **k: (_sc.append(1), True)[1]
            globals()["tap_template"] = lambda c, cat, nm, *a, **k: True
            wst2.tick(_Ctx())
            check("scan_ttl：期满图在照常命中（复读冷却不吞弹窗）", wst2.tapped)
        finally:
            globals()["template_present"] = lambda c, cat, nm, *a, **k: True
            globals()["tap_template"] = _tt_real

        # 9b) TimerStep：到点触发（不认图）、跑完歇满整周期、失败歇短冷却重试
        tm = TimerStep("定时", [{"interval_min": 10, "flow_name": "f"}], [["done"]])
        check("间隔换算+下限钳制", tm._interval_min == [10.0]
              and TimerStep("t", [{"interval_min": 0, "flow_name": "f"}], [[]])._interval_min == [0.1])
        tm.tick(_Ctx())
        check("首跑前不误触发（起跑=第一周期起点）",
              tm._pending is None and not tm.tapped)
        tm._cool[0] = 0.0                      # 手动拨到"到点"
        tm.tick(_Ctx())
        check("到点触发：起子链占落手（不认图）", tm._pending is not None and tm.tapped)
        tm.tick(_Ctx())                        # 子链 done
        check("跑完歇满整周期（不是 watch 的短冷却）",
              tm._pending is None and tm._cool[0] > _t.monotonic() + 5)
        tm.tick(_Ctx())
        check("周期未到：空转不占落手", not tm.tapped)
        tm2 = TimerStep("定时", [{"interval_min": 10, "flow_name": "f"}],
                        [["failed"]], cooldown_secs=0.05, fail_limit=2)
        tm2._cool[0] = 0.0
        tm2.tick(_Ctx())                       # 起链
        tm2.tick(_Ctx())                       # 第1败 → 歇短冷却
        check("失败不烧周期：歇的是 cooldown 不是 interval",
              tm2._cool[0] < _t.monotonic() + 1)
        _t.sleep(0.07)
        tm2.tick(_Ctx())                       # 重试起链
        tm2.tick(_Ctx())                       # 第2败 → 停用
        check("连败停用（watch 同款纪律）", tm2._off[0] and tm2._fails[0] == 2)
        tm3 = TimerStep("定时", [{"interval_min": 10, "flow_name": "没了"}], [None])
        tm3._cool[0] = 0.0
        tm3.tick(_Ctx())
        check("编译死行开局停用不占落手", tm3._off[0] and not tm3.tapped)
    finally:
        globals()["template_present"] = _tp_real
        _fsm_mod.FSM = _fsm_real

    # 9c) timer 编译接线 + 引擎豁免：开局即在岗（不经入边结论）、主线一收口
    #     整图即 ok（timer 不拖完成判定）。主线用 debug_print：纯日志不落手，
    #     自检绝不碰真 adb。
    _g9c = {"version": 2, "nodes": [
                {"id": "t1", "type": "timer", "label": "",
                 "rows": [{"interval_min": 10, "flow_name": "sub"}]},
                {"id": "d", "type": "debug_print", "target": "none", "text": "x"}],
            "edges": []}
    _gs9c = compile_graph(_g9c, {"sub": {"version": 2, "nodes": [], "edges": []}},
                          "main")[0]
    _r9c = _gs9c.tick(_Ctx())
    _nd9c = _gs9c._nd["t1"]
    check("timer 开局即点亮 + 主线收口即整图 ok（哨兵不拖退）",
          _r9c == "ok" and _nd9c["state"] == "running"
          and isinstance(_nd9c["step"], TimerStep))
    check("timer 空配置降级 WARN 步（watch 同款）",
          isinstance(_make_build_step({}, ["m"], set())(
              {"id": "t", "type": "timer", "label": "", "rows": []}), LogStep))

    # 9d) watch 容器展开（多监视合并 · 第 1 段：数据格式 + 编译兼容）
    _rowA = {"category": "x", "name": "弹窗A", "kind": "tap"}
    _rowB = {"category": "x", "name": "弹窗B", "flow_name": "sub"}
    _plain = {"version": 2, "nodes": [{"id": "w", "type": "watch", "rows": [_rowA]}],
              "edges": []}
    check("老格式（无 groups）→ 原对象早退，零扰动",
          expand_watch_containers(_plain) is _plain)
    _cont = {"version": 2, "nodes": [
        {"id": "c1", "type": "watch", "label": "监视容器", "rows": [_rowA],
         "cooldown_secs": 99, "fail_limit": 7,
         "groups": [{"id": "gA", "rows": [_rowA], "cooldown_secs": 5},
                    {"id": "gB", "rows": [_rowB], "fail_limit": 2}]},
        {"id": "lp", "type": "loop"}],
        "edges": [{"from": "gB", "port": "hit", "to": "lp"}]}
    _ex = expand_watch_containers(_cont)
    _exn = {n["id"]: n for n in _ex["nodes"]}
    check("容器展开：两 id 各成一台哨兵、容器消失",
          "c1" not in _exn and set(_exn) == {"gA", "gB", "lp"})
    check("组参数覆盖容器顶层、rows 各归各",
          _exn["gA"].get("cooldown_secs") == 5 and _exn["gA"]["rows"] == [_rowA]
          and _exn["gB"].get("fail_limit") == 2 and _exn["gB"]["rows"] == [_rowB]
          and "groups" not in _exn["gA"])
    check("非行数据字段（label/type）从容器继承",
          _exn["gA"].get("label") == "监视容器" == _exn["gB"].get("label"))
    _dup = [n["id"] for n in expand_watch_containers({"version": 2, "nodes": [
        {"id": "lp", "type": "loop"},
        {"id": "c", "type": "watch", "groups": [
            {"id": "lp", "rows": [_rowA]}, {"rows": [_rowB]}]}],
        "edges": []})["nodes"] if n["type"] == "watch"]
    check("组 id 缺失/撞车 → 现编新 id",
          len(_dup) == 2 and "lp" not in _dup and "" not in _dup
          and _dup[0] != _dup[1])
    # 合并落盘时首组就冠容器原 id：容器本体不算"已用"，那条线不能被判撞车
    _reuse = expand_watch_containers({"version": 2, "nodes": [
        {"id": "c", "type": "watch", "groups": [{"id": "c", "rows": [_rowA]}]}],
        "edges": []})["nodes"]
    check("组 id = 容器原 id → 合法接管（老信号线零迁移）",
          len(_reuse) == 1 and _reuse[0]["id"] == "c"
          and _reuse[0]["rows"] == [_rowA])
    check("空容器展开为 0 台",
          expand_watch_containers({"version": 2, "nodes": [
              {"id": "c", "type": "watch", "groups": []}], "edges": []})
          ["nodes"] == [])
    # 走完整编译漏斗：两组各点亮一台 WatchStep；gB 的「命中→信号」线不经
    # 改写直达展开后的组。老 hit 线（from=组id）编译期剔除。主线用 debug_print：不落手不碰 adb。
    _gs9d = compile_graph(
        {"version": 2, "nodes": [
            {"id": "c1", "type": "watch", "groups": [
                {"id": "gA", "rows": [_rowA]},
                {"id": "gB", "rows": [_rowB]}]},
            {"id": "lp", "type": "loop", "steps": []},
            {"id": "d", "type": "debug_print", "target": "none", "text": "x"}],
         "edges": [{"from": "gB", "port": "hit", "to": "lp"}]},
        {"sub": {"version": 2, "nodes": [], "edges": []}}, "main")[0]
    _gs9d.tick(_Ctx())                    # 点亮拍：哨兵开局即在岗，不经入边结论
    _steps9d = [_gs9d._nd[i]["step"] for i in ("gA", "gB")]
    # WatchStep.rows 是编译副本（每行补 flow_name 空键），比较按"副本=原行+补键"
    check("编译漏斗：两组各点亮一台 WatchStep",
          all(isinstance(s, WatchStep) for s in _steps9d)
          and _steps9d[0].rows == [dict(_rowA, flow_name="")]
          and _steps9d[1].rows == [_rowB]
          and _rowA not in _steps9d[0].rows)   # 副本纪律：绝不污染原行 dict
    check("组 id 上的老命中线编译期剔除（废线不炸图，两台哨兵照常点亮）",
          not any(p == "hit" for _, p, _ in _gs9d._edges))
    check("timer 节点不认 groups（只有 watch 容器化，字段原样留着）",
          expand_watch_containers({"version": 2, "nodes": [
              {"id": "t", "type": "timer", "groups": [{"id": "z", "rows": []}],
               "rows": [_rowA]}], "edges": []})["nodes"][0].get("id") == "t")
    # 引用扫描同口径：容器化后删图反查/改名传播都须看得见 groups 里的行
    from bot import template_deps as _td
    _wr = _td.watch_rows(_cont["nodes"][0])
    check("watch_rows：容器展平两组行（读侧引用不再瞎）",
          _wr == [_rowA, _rowB]
          and _td.node_tpl_refs(_cont["nodes"][0]) == [("x", "弹窗A"), ("x", "弹窗B")]
          and _td.node_flow_refs(_cont["nodes"][0]) == {"sub"}
          and _td.node_tpl_refs(_plain["nodes"][0]) == [("x", "弹窗A")])
    _n9 = _td.node_tpl_refs({"id": "t", "type": "timer", "groups": [
        {"id": "z", "rows": [_rowA]}], "rows": []})
    check("timer 走 watch_rows 顶层口径（groups 不误吞）", _n9 == [])

    # 10) 判断走N自动诊断日志：cond 假 → 收口选 false 打一行、只打一次；
    #     cond 真 → 不打；重试重经 → 再打一次；缺图降级 → 恒走N + WARN。
    class _LC:                        # 收日志的 stub ctx（frame 给真图数组，
        ld_index = 0                  #   但诊断闭包的模板分支在自检里被绕开）
        extra = {}
        def __init__(self): self.lines = []
        def log(self, msg, lv="INFO"): self.lines.append((lv, msg))
    def _br(node):
        bc = {node["id"]: (None, 0.0, _make_branch_diag(node))
              if node.get("_missing") else
              (_mk(node), float(node.get("timeout", 5)), _make_branch_diag(node))}
        return GraphStep("t", {"b": node}, [], branch_conds=bc,
                         build_step=lambda nd: None, is_action=lambda nd: False)
    def _mk(nd):                      # 自检条件桩：_LC 没有 frame 属性，绕开真判据
        flag = nd.get("_r", True)
        return lambda c: flag
    # 10a 判不过 → 打一次
    nd10 = {"id": "b", "type": "branch", "label": "检查登录", "_r": False,
            "timeout": 0.01}
    ctx10 = _LC()
    g10 = _br(nd10)
    import time as _t10
    _d10 = _t10.monotonic() + 2.0
    while g10.node_state("b") != "done" and _t10.monotonic() < _d10:
        g10.tick(ctx10)
    check("判断走N：收口打一行诊断（含备注名）",
          len(ctx10.lines) == 1 and "检查登录" in ctx10.lines[0][1]
          and "走N" in ctx10.lines[0][1])
    g10.reset(); ctx10.lines.clear()  # 10b 重试重经 → 再打一次
    _d10 = _t10.monotonic() + 2.0
    while g10.node_state("b") != "done" and _t10.monotonic() < _d10:
        g10.tick(ctx10)
    check("判断重试重经：再收口再打一次", len(ctx10.lines) == 1)
    nd11 = dict(nd10, _r=True); ctx11 = _LC()
    g11 = _br(nd11)
    _d11 = _t10.monotonic() + 2.0
    while g11.node_state("b") != "done" and _t10.monotonic() < _d11:
        g11.tick(ctx11)
    check("判断走Y：收口打一行结论（含备注名）",
          len(ctx11.lines) == 1 and "检查登录" in ctx11.lines[0][1]
          and "✓ 条件成立 →走Y" in ctx11.lines[0][1])
    # 10c 编译层：模板缺图分支 → 恒走N + WARN 诊断
    _tm10 = _tpl_missing
    globals()["_tpl_missing"] = lambda c, n: True
    try:
        _g10 = {"version": 2, "nodes": [{"id": "b", "type": "branch",
                                         "label": "缺图判", "cond": {"kind": "template",
                                                                    "category": "x",
                                                                    "name": "没了"}}],
                "edges": []}
        _gs10 = compile_graph(_g10, {}, "t")[0]
    finally:
        globals()["_tpl_missing"] = _tm10
    ctx12 = _LC(); ctx12.frame = None
    _d12 = _t10.monotonic() + 2.0
    while _gs10.tick(ctx12) == "running" and _t10.monotonic() < _d12: pass
    check("缺图判断：恒走N（修复 cond=None 误走Y）+ WARN 诊断一行",
          ctx12.lines and ctx12.lines[0][0] == "WARN" and "恒走N" in ctx12.lines[0][1])
    # 10d 真诊断闭包：无画面拍收口 → "无画面"，不碰账本
    dg = _make_branch_diag({"id": "b", "type": "branch", "label": "无画",
                            "cond": {"kind": "template", "category": "x",
                                     "name": "y"}})
    ctx13 = _LC()
    dg(ctx13)
    check("诊断闭包：frame=None 短路报「无画面」",
          len(ctx13.lines) == 1 and "无画面" in ctx13.lines[0][1])
    # 10d2 走Y 自动结论行（成功同样打印，无任何自定义字段）：
    #      单条件="条件成立"；多条件=「全部/任一条件满足」
    _dg15 = _make_branch_diag({"id": "b", "type": "branch", "label": "够不够",
                               "cond": {"kind": "var", "name": "hp",
                                        "op": ">=", "value": "10"}})
    ctx15 = _LC(); ctx15.extra = {}     # 实例 dict，不污染类属性共享 extra
    _dg15(ctx15, "true")
    check("走Y 打结论行（单条件）",
          len(ctx15.lines) == 1
          and ctx15.lines[0][1] == "判断「够不够」✓ 条件成立 →走Y")
    ctx15.lines.clear()
    _dg16 = _make_branch_diag({"id": "b", "type": "branch", "label": "多条件",
                               "cond": {"kind": "var", "name": "hp",
                                        "op": ">=", "value": "1"},
                               "extra_conds": [{"kind": "var", "name": "mp",
                                                "op": ">=", "value": "2"}],
                               "logic": "all"})
    _dg16(ctx15, "true")
    check("走Y 打结论行（多条件·全部满足）",
          len(ctx15.lines) == 1
          and ctx15.lines[0][1] == "判断「多条件」✓ 全部条件满足 →走Y")
    # 10f) 执行成功同样打印：普通节点收口一行 INFO（带备注名）
    class _Ok17(Step):
        def tick(self, ctx): return "ok"
    ctx17 = _LC()
    _gs17 = GraphStep("t", {"a": {"id": "a", "type": "click_template",
                                  "label": "点按钮"}}, [],
                      build_step=lambda nd: _Ok17("点按钮"),
                      is_action=lambda nd: False)
    _gs17.tick(ctx17)
    check("执行成功打印：普通节点收口一行「✓ 执行完成」",
          any("✓ 执行完成" in m and "点按钮" in m for _lv, m in ctx17.lines))
    check("当前动作跟节点：步骤名刷成节点备注名（无前缀）",
          _gs17.name == "点按钮")

    # 10h) 结束流程两种范围：local=谁的边界谁收口，global=ABORT 穿透全停
    ctx18 = _LC()
    _gs18L = GraphStep("t", {"s": {"id": "s", "type": "stop_flow"}}, [],
                       build_step=lambda nd: StopFlowStep("结束", scope="local"),
                       is_action=lambda nd: False)
    check("顶层 local：上抛 FLOW_END（worker 按提前收尾算）",
          _gs18L.tick(ctx18) == FLOW_END)
    _gs18G = GraphStep("t", {"s": {"id": "s", "type": "stop_flow"}}, [],
                       build_step=lambda nd: StopFlowStep("结束", scope="global"),
                       is_action=lambda nd: False)
    check("顶层 global：上抛 ABORT", _gs18G.tick(ctx18) == ABORT)
    check("循环体内 local：就地吸收 = 跳出循环（ok，主线续跑）",
          LoopStep("循", [StopFlowStep("结束", scope="local")]).tick(ctx18) == "ok")
    check("循环体内 global：ABORT 穿透（整条任务停）",
          LoopStep("循", [StopFlowStep("结束", scope="global")]).tick(ctx18) == ABORT)
    check("子流程内 local：就地吸收 = 子流程算跑完（ok 走Y口）",
          SubFlowStep("子", [StopFlowStep("结束", scope="local")]).tick(ctx18) == "ok")
    check("子流程内 global：ABORT 继续上抛",
          SubFlowStep("子", [StopFlowStep("结束", scope="global")]).tick(ctx18) == ABORT)
    # 编译层：stop_scope 从节点读、缺键默认 local（老流程零改动=顶层语义不变）
    _bld18 = _make_build_step({}, [])
    _st18 = _bld18({"id": "s", "type": "stop_flow", "label": "结束"})
    check("编译层：老节点缺 stop_scope 键 → 默认 local",
          isinstance(_st18, StopFlowStep) and _st18._scope == "local")
    _st18g = _bld18({"id": "s", "type": "stop_flow", "label": "结束",
                     "stop_scope": "global"})
    check("编译层：stop_scope=global 照传", _st18g._scope == "global")

    # 10h2) 结束流程第三种范围「跳过本轮」（F4 continue）：循环体内就地吸收 =
    #        本轮剩余步骤丢弃 + 轮数照常 +1 + 重开下一轮；非循环环境原样冒泡。
    _gs18C = GraphStep("t", {"s": {"id": "s", "type": "stop_flow"}}, [],
                       build_step=lambda nd: StopFlowStep("结束", scope="continue"),
                       is_action=lambda nd: False)
    check("顶层 continue：上抛 LOOP_CONTINUE（worker 按提前收尾算）",
          _gs18C.tick(ctx18) == LOOP_CONTINUE)
    check("子流程体内 continue：子流程不是循环 → 原样上抛",
          SubFlowStep("子", [StopFlowStep("结束", scope="continue")]).tick(ctx18)
          == LOOP_CONTINUE)
    _cnt18 = [0]

    class _Cnt18(Step):
        def tick(self, ctx):
            _cnt18[0] += 1
            return "ok"
    lp18 = LoopStep("循", [StopFlowStep("结束", scope="continue"), _Cnt18("计")],
                    times=2)
    _r18 = lp18.tick(ctx18)
    check("循环体 continue：本轮剩余步骤不执行（计数步 0 次）+ 轮数推进到 1",
          _r18 == "running" and _cnt18[0] == 0 and lp18._count == 1)
    _r18 = lp18.tick(ctx18)
    check("循环体 continue：第二轮照旧被跳过（计数仍 0、轮数 2）",
          _r18 == "running" and _cnt18[0] == 0 and lp18._count == 2)
    check("循环体 continue：到达轮数上限正常收尾（ok）不死循环",
          lp18.tick(ctx18) == "ok" and _cnt18[0] == 0)
    _st18c = _bld18({"id": "s", "type": "stop_flow", "label": "结束",
                     "stop_scope": "continue"})
    check("编译层：stop_scope=continue 照传",
          isinstance(_st18c, StopFlowStep) and _st18c._scope == "continue")
    _st18x = _bld18({"id": "s", "type": "stop_flow", "label": "结束",
                     "stop_scope": "乱填"})
    check("编译层：非法 stop_scope 回落 local",
          isinstance(_st18x, StopFlowStep) and _st18x._scope == "local")

    # 10j2) 区间比较（F4）：between/not_between，含端点、反填归一、非数值恒 False
    _cxr = _LC()
    _cxr.extra = {}
    _cbet = _make_condition({"kind": "var", "name": "hp", "op": "between",
                             "value": "10", "value2": "20"})
    check("区间：变量没值 → 恒 False", _cbet(_cxr) is False)
    flow_var_set(_cxr, "hp", 15)
    check("区间 between：15 在 [10,20] 内 → True", _cbet(_cxr) is True)
    flow_var_set(_cxr, "hp", 10)
    check("区间 between：下界端点 10 → True（含端点）", _cbet(_cxr) is True)
    flow_var_set(_cxr, "hp", 20)
    check("区间 between：上界端点 20 → True（含端点）", _cbet(_cxr) is True)
    flow_var_set(_cxr, "hp", 21)
    check("区间 between：21 越界 → False", _cbet(_cxr) is False)
    flow_var_set(_cxr, "hp", 15)
    _cbetswap = _make_condition({"kind": "var", "name": "hp", "op": "between",
                                 "value": "20", "value2": "10"})
    check("区间 between：边界反填 20/10 自动归一 → 15 命中 True",
          _cbetswap(_cxr) is True)
    _cnbet = _make_condition({"kind": "var", "name": "hp", "op": "not_between",
                              "value": "10", "value2": "20"})
    check("区间 not_between：15 在区间内 → False（补集）", _cnbet(_cxr) is False)
    flow_var_set(_cxr, "hp", 5)
    check("区间 not_between：5 在区间外 → True（补集）", _cnbet(_cxr) is True)
    flow_var_set(_cxr, "pos", (250, 400))
    check("区间：变量非数值 → 恒 False（不做字典序）",
          _make_condition({"kind": "var", "name": "pos", "op": "between",
                           "value": "10", "value2": "20"})(_cxr) is False)
    check("区间：边界非数值 → 恒 False",
          _make_condition({"kind": "var", "name": "hp", "op": "between",
                           "value": "x", "value2": "20"})(_cxr) is False)

    # 10i) 子流程参数（F2 调用绑定）：进入前逐个求值写流程变量；空表零污染；
    #      非法实参跳过不拦路；只落值不 restore（同名外层不还原）。
    class _PC(_LC):                   # extra 要各自独立（_LC 的类是共享 dict）
        def __init__(self):
            self.extra = {}
            self.lines = []
    _ctxp = _PC()
    flow_var_set(_ctxp, "体力", 30)
    SubFlowStep("子", [LogStep("体", "x")],
                params=[{"name": "下限", "expr": "10"},
                        {"name": "扣完", "expr": "体力-10"}]).tick(_ctxp)
    check("子流程参数：字面量实参落值 下限==10",
          flow_var_get(_ctxp, "下限") == 10)
    check("子流程参数：算术实参 扣完==20", flow_var_get(_ctxp, "扣完") == 20)
    _ctxx = _PC()
    SubFlowStep("子", [LogStep("体", "x")],
                params=[{"name": "坏", "expr": "1+"}]).tick(_ctxx)
    check("子流程参数：非法实参跳过不写", flow_var_get(_ctxx, "坏") is None)
    _ctxy = _PC()
    SubFlowStep("子", [LogStep("体", "x")]).tick(_ctxy)   # 老节点：无 params
    check("子流程参数：无 params 零污染", _ctxy.extra == {})
    # 编译层：params 从节点读、缺键空表；纯字符串行换算
    _fmp = {"f": {"version": 2, "nodes": [], "edges": []}}
    _bldp = _make_build_step(_fmp, [])
    _stp = _bldp({"id": "s", "type": "subflow", "flow_name": "f",
                  "wait_before": False,
                  "params": [{"name": "p", "expr": "1"}]})
    check("编译层：params 透传（dict 行）",
          isinstance(_stp, SubFlowStep) and _stp.params == [{"name": "p", "expr": "1"}])
    _stp2 = _bldp({"id": "s", "type": "subflow", "flow_name": "f",
                   "wait_before": False, "params": ["下限=10"]})
    check("编译层：params 纯字符串行换算",
          isinstance(_stp2, SubFlowStep)
          and _stp2.params == [{"name": "下限", "expr": "10"}])
    _stp3 = _bldp({"id": "s", "type": "subflow", "flow_name": "f", "wait_before": False})
    check("编译层：老节点缺 params → 空表",
          isinstance(_stp3, SubFlowStep) and _stp3.params == [])

    # 10e) 多条件判断（与/或/取反 + 缺图降级推广）：全用 var 条件桩，不碰图色
    class _VC(_LC):                   # var 条件要各自独立的 extra（类属性会串台）
        def __init__(self):
            self.extra = {}
            self.lines = []
    _cv = _VC()
    flow_var_set(_cv, "hp", 12)
    _c_t = {"kind": "var", "name": "hp", "op": ">=", "value": "5"}     # 真
    _c_f = {"kind": "var", "name": "hp", "op": ">=", "value": "99"}    # 假
    check("多条件·满足全部：一真一假 → 假",
          _make_branch_condition({"cond": dict(_c_t),
                                  "extra_conds": [dict(_c_f)],
                                  "logic": "all"})(_cv) is False)
    check("多条件·满足任一：一真一假 → 真",
          _make_branch_condition({"cond": dict(_c_t),
                                  "extra_conds": [dict(_c_f)],
                                  "logic": "any"})(_cv) is True)
    check("多条件·单条取反：真条取反 → 假",
          _make_branch_condition({"cond": dict(_c_t, **{"not": True})})(_cv) is False)
    check("单条件未取反 = 旧行为等价",
          _make_branch_condition({"cond": dict(_c_t)})(_cv) is True)
    check("空条件节点：恒假（同旧 kind 缺失兜底）",
          _make_branch_condition({"cond": {}})(_cv) is False)
    # 多条件诊断格式：编号 + （取反）标记；单条件旧格式逐字不变（10a 已保）
    _cxd = _VC()
    _dg3 = _make_branch_diag({"id": "b", "type": "branch", "label": "多条件",
                              "cond": dict(_c_t, **{"not": True}),
                              "extra_conds": [dict(_c_f)]})
    _dg3(_cxd)
    check("多条件诊断：条件1（取反）… 全部条件不满足",
          len(_cxd.lines) == 1 and "条件1（取反）" in _cxd.lines[0][1]
          and "条件2" in _cxd.lines[0][1] and "全部条件不满足" in _cxd.lines[0][1])
    # 编译层缺图降级推广：满足全部任一条缺图 → 恒走N；满足任一 → 其余照判
    _tm10e = _tpl_missing
    globals()["_tpl_missing"] = lambda c, n: True
    try:
        _g10e_all = {"version": 2, "nodes": [{"id": "b", "type": "branch",
                    "cond": {"kind": "template", "category": "x", "name": "没了"},
                    "extra_conds": [{"kind": "var", "name": "hp",
                                     "op": ">=", "value": "5"}],
                    "logic": "all"}], "edges": []}
        _g10e_any = {"version": 2, "nodes": [{"id": "b", "type": "branch",
                    "cond": {"kind": "template", "category": "x", "name": "没了"},
                    "extra_conds": [{"kind": "var", "name": "hp",
                                     "op": ">=", "value": "5"}],
                    "logic": "any"}], "edges": []}
        _bc_all = compile_graph(_g10e_all, {}, "t")[0]._branch_conds["b"]
        _bc_any = compile_graph(_g10e_any, {}, "t")[0]._branch_conds["b"]
    finally:
        globals()["_tpl_missing"] = _tm10e
    check("缺图降级·与：任一条缺图 → cond=None 恒走N", _bc_all[0] is None)
    check("缺图降级·或：缺图条记假、var 条照判（closure 真）",
          callable(_bc_any[0]) and _bc_any[0](_cv) is True)
    check("没配滑动的判断：编译产物仍是三元组（存量条目逐字不变）",
          len(_bc_any) == 3)

    # 10i) 循环出口多条件（2026-10-06）：与判断节点同字段（cond+extra_conds+
    #      logic+not），组合复用同一个 _make_branch_condition；全 var 条件桩不碰图色
    _bld_lp = _make_build_step({}, [])
    _lp_body = [{"type": "sleep", "secs": 0}]

    def _lp(**kw):
        return _bld_lp({"id": "L", "type": "loop", "label": "循环",
                        "steps": list(_lp_body), **kw})
    check("循环编译·或：一真一假 → 出口成立（多条件走判断同款组合）",
          isinstance(_lp(cond=dict(_c_t), extra_conds=[dict(_c_f)],
                         logic="any"), LoopStep)
          and _lp(cond=dict(_c_t), extra_conds=[dict(_c_f)],
                  logic="any").until(_cv) is True)
    check("循环编译·与：一真一假 → 不出",
          _lp(cond=dict(_c_t), extra_conds=[dict(_c_f)],
              logic="all").until(_cv) is False)
    check("循环编译·单条取反：假条取反算真",
          _lp(cond=dict(_c_f, **{"not": True})).until(_cv) is True)
    check("循环编译·老单条件节点：未取反 = 旧行为等价（零变化）",
          _lp(cond=dict(_c_t)).until(_cv) is True)
    _lp_none = _lp()
    check("循环编译·无条件：until=None（只认轮数上限）",
          isinstance(_lp_none, LoopStep) and _lp_none.until is None)
    check("循环编译·text 出口照编成 LoopStep（2026-10-06 白名单漏 text 的回归："
          "面板给选、引擎判未知 → 整循环被直通，循环体一步没跑）",
          isinstance(_lp(cond={"kind": "text", "target": "20"}), LoopStep))
    check("循环编译·任一条未知类型：整节点拆穿（永不出/提前出比不跑更毒）",
          isinstance(_lp(cond=dict(_c_t),
                         extra_conds=[{"kind": "bogus"}]), LogStep))
    _tm_lp = _tpl_missing
    globals()["_tpl_missing"] = lambda c, n: bool(n)
    try:
        _lp_miss = _lp(cond=dict(_c_t), extra_conds=[
            {"kind": "template", "category": "x", "name": "没了"}])
    finally:
        globals()["_tpl_missing"] = _tm_lp
    check("循环编译·第2条出口模板缺图：整节点拆穿", isinstance(_lp_miss, LogStep))

    # 10j) 条件「持续时长」hold_secs（2026-10-11）：条件要连续成立满 N 秒才算数，
    #      中途断重计时。用 var 条件桩 + 假时钟注入（不真睡），判断/循环同吃
    #      _make_branch_condition 一份包装。
    import time as _t10j
    _mono_real10j = _t10j.monotonic
    _CLOCK = [100.0]
    _t10j.monotonic = lambda: _CLOCK[0]
    try:
        _cvj = _VC()
        flow_var_set(_cvj, "hp", 12)
        _hold_true = {"kind": "var", "name": "hp", "op": ">=", "value": "5",
                      "hold_secs": 2.0}
        _h_cond = _make_branch_condition({"cond": dict(_hold_true)})
        check("持续·首拍成立不算（还没撑满，返回 False）", _h_cond(_cvj) is False)
        _CLOCK[0] += 1.0
        check("持续·到 1s 仍 False（未满 2s）", _h_cond(_cvj) is False)
        _CLOCK[0] += 1.1
        check("持续·撑满 2s → True", _h_cond(_cvj) is True)
        # 中途断一拍（变量变假）→ 清零重来
        flow_var_set(_cvj, "hp", 1)          # 条件变假
        check("持续·中途不成立 → False 且清零", _h_cond(_cvj) is False)
        flow_var_set(_cvj, "hp", 12)         # 又成立
        check("持续·断了重来：刚恢复不算（False）", _h_cond(_cvj) is False)
        _CLOCK[0] += 0.5
        check("持续·重来后 0.5s 仍 False（从零重新攒）", _h_cond(_cvj) is False)
        _CLOCK[0] += 1.6
        check("持续·重来后撑满 → True", _h_cond(_cvj) is True)
        # 断点检测：距上拍评估 > max(30, 10·hold) 视为不连续 → 重开计时
        _CLOCK[0] += 100.0
        check("持续·断点（间隔过长）重开计时（False）", _h_cond(_cvj) is False)
        # 缺省/0 = 零变化：直接返回原闭包语义（命中即真）
        _plain = _make_branch_condition({"cond": {"kind": "var", "name": "hp",
                                                  "op": ">=", "value": "5"}})
        check("持续·未配 hold_secs：命中即真（老行为逐字不变）",
              _plain(_cvj) is True)
        # motion 跳过包装：配了 hold_secs 也不叠加（动态检测本身即时间窗）
        _cvm = _VC()                      # 新 ctx：不吃前面 hold 测试遗留的键
        _m = _make_branch_condition({"cond": {"kind": "motion", "category": "c",
                                              "frames": [], "hold_secs": 5}})
        check("持续·motion 条件不吃 hold（帧组空 → 恒 False，零状态）",
              _m(_cvm) is False and not any(k.startswith("hold_cond")
                                            for k in _cvm.extra))
    finally:
        _t10j.monotonic = _mono_real10j

    # 10g) 判断节点「滑动翻页继续找」：窗口到期 → 滑一页 → 停稳重开窗，
    #      滑次用尽才收口走N。swipe 打桩不碰真 adb。
    import bot.fsm as _fsm10g
    _bc_sw = compile_graph({"version": 2, "nodes": [{"id": "b", "type": "branch",
                     "label": "翻页", "_r": False, "timeout": 0.05,
                     "cond": {"kind": "var", "name": "hp", "op": ">=",
                              "value": "99"},
                     "on_timeout": "scroll", "scroll_max": 2,
                     "scroll_roi": [100, 200, 400, 600],
                     "scroll_settle": 0.2}], "edges": []}, {}, "t")[0]
    check("配了滑动：编译成四元组、次数进账",
          len(_bc_sw._branch_conds["b"]) == 4
          and _bc_sw._branch_conds["b"][3]["max"] == 2)
    _sw10g = []
    _swipe_real10g = _fsm10g.swipe
    _fsm10g.swipe = lambda c, *p: _sw10g.append(p)
    try:
        ctx10g = _LC()
        _gs10g = GraphStep("t", {"b": {"id": "b", "type": "branch",
                                       "label": "翻页"}}, [],
                           branch_conds={"b": _bc_sw._branch_conds["b"]},
                           build_step=lambda nd: None,
                           is_action=lambda nd: False)
        _d10g = _t10.monotonic() + 5.0
        while _gs10g.node_state("b") != "done" and _t10.monotonic() < _d10g:
            _gs10g.tick(ctx10g)
    finally:
        _fsm10g.swipe = _swipe_real10g
    check("滑动翻页：滑满 2 次仍没认出才收口（真滑了 2 手）",
          _gs10g.node_state("b") == "done" and len(_sw10g) == 2)
    check("滑动翻页：中途 INFO 一行一滑 + 收口 WARN 一滑",
          sum("滑动翻页找" in m for _l, m in ctx10g.lines) == 2
          and any("滑了 2 次仍没认出" in m for _l, m in ctx10g.lines))
    # 限定滑动区域（scroll_roi=[100,200,400,600]）：x=框中心 250，
    # 端点钳进框内（202..598），不许出框
    check("滑动翻页限定区域：起止点全在框内（x=250，y∈[202,598]）",
          len(_sw10g) == 2 and all(
              p[0] == 250 and p[2] == 250
              and 202 <= p[1] <= 598 and 202 <= p[3] <= 598
              for p in _sw10g))

    # 10h) 区域跟引用走：cond/节点自带 roi 覆盖模板账本区域，没框 = None 兜底
    _tp_real10h = globals()["template_present"]
    _tm_real10h = _tpl_missing
    _roi_seen = []
    globals()["template_present"] = \
        lambda c, cat, nm, *a, **k: (_roi_seen.append(k.get("roi")), True)[1]
    globals()["_tpl_missing"] = lambda c, n: False   # 假模板名别走缺图降级
    try:
        _make_condition({"kind": "template", "category": "x", "name": "锁",
                         "roi": [10, 20, 30, 40]})(_LC())
        check("判断模板条件：框的区域原样传进 template_present",
              _roi_seen[-1] == [10, 20, 30, 40])
        _make_condition({"kind": "template", "category": "x", "name": "锁"})(_LC())
        check("判断模板条件：没框 = roi=None（引擎回落账本，老行为）",
              _roi_seen[-1] is None)
        _wt10h = _action_step({"type": "wait_template", "name": "t",
                               "category": "x", "roi": [1, 2, 3, 4]})
        check("等图节点：roi 进 WaitStep", _wt10h.roi == [1, 2, 3, 4])
        _wt10h2 = _action_step({"type": "wait_template", "name": "t",
                                "category": "x"})
        check("等图老节点：roi=None", _wt10h2.roi is None)
        _ta10h = _action_step({"type": "click_template", "name": "t",
                               "category": "x", "all_hits": True,
                               "delay_after_click": False,
                               "roi": [5, 6, 7, 8]})
        check("点全部节点：roi 进 TapAllStep", _ta10h.roi == [5, 6, 7, 8])
    finally:
        globals()["template_present"] = _tp_real10h
        globals()["_tpl_missing"] = _tm_real10h

    # 11) 识字取物（server_pick 五用法 + 滑动找）：_tap / swipe / ocr.find_text /
    #     ocr.list_text 全打桩，不碰真 adb、不加载 OCR 模型。ctx 用 _LC。
    from core import ocr
    import bot.fsm as _fsm11   # 滑动找已收进 SwipeFinder，落手在 fsm 层，打桩须打 fsm.swipe
    _tap_real, _swipe_real = globals()["_tap"], _fsm11.swipe
    _ocr_real = ocr.find_text
    _ocr_list_real = ocr.list_text
    _taps, _swipes = [], []
    globals()["_tap"] = lambda c, x, y: _taps.append((x, y))
    _fsm11.swipe = lambda c, *p: _swipes.append(p)
    try:
        class _SpCtx(_LC):            # _LC 没 frame 属性，补一个 dummy（find_text 桩不看它）
            def __init__(self):
                super().__init__()
                self.frame = object()
                self.extra = {}       # _LC.extra 是类属性（全局共享）：变量测试必须各拿各的

        def _mk_step(mode, **kw):
            return ServerPickStep(kw.pop("name", "读"), text=kw.pop("text", "金币"),
                                  mode=mode, **kw)

        # 11a) number：抠数字进变量（"金币：12345"→12345）
        ocr.find_text = lambda img, needle, **k: {
            "x": 100, "y": 200, "text": "金币:12345", "sim": 1.0, "ms": 5}
        cx = _SpCtx()
        r = _mk_step("number", var_name="gold").tick(cx)
        check("number：抠出数字存进流程变量、收口 ok",
              r == "ok" and flow_var_get(cx, "gold") == 12345)
        # 认出整行没数字 = 当没认出（不写脏值、继续 running）
        cx2 = _SpCtx()
        ocr.find_text = lambda img, needle, **k: {
            "x": 100, "y": 200, "text": "我的金币", "sim": 1.0, "ms": 5}
        r = _mk_step("number", var_name="gold", timeout=999).tick(cx2)
        check("number：认出无数字行 → 不写变量、running（当没认出）",
              r == "running" and flow_var_get(cx2, "gold") is None)
        # var_name 空 → 回落目标文字本身
        cx3 = _SpCtx()
        ocr.find_text = lambda img, needle, **k: {
            "x": 1, "y": 2, "text": "30级", "sim": 1.0, "ms": 5}
        _mk_step("number", text="等级").tick(cx3)
        check("number：var_name 空回落目标文字作变量名", flow_var_get(cx3, "等级") == 30)

        # 11b) position：坐标进变量
        cx4 = _SpCtx()
        ocr.find_text = lambda img, needle, **k: {
            "x": 250, "y": 400, "text": "金币:9", "sim": 1.0, "ms": 5}
        r = _mk_step("position", var_name="pos").tick(cx4)
        check("position：认出坐标存进流程变量",
              r == "ok" and flow_var_get(cx4, "pos") == (250, 400))

        # 11c) click：偏移 + 落点钳进屏内（贴边大偏移也不点到负坐标）
        cx5 = _SpCtx()
        _taps.clear()
        ocr.find_text = lambda img, needle, **k: {
            "x": 530, "y": 955, "text": "金币:1", "sim": 1.0, "ms": 5}
        r = _mk_step("click", offset=(50, 50), jitter=(0, 0)).tick(cx5)
        check("click：落点钳进屏内（530+50→539 / 955+50→959）",
              r == "ok" and _taps == [(539, 959)])
        # 老流程无 offset/jitter 键 → 编译侧取默认 (0,0)，点正中（零变化）
        st_old = _action_step({"type": "server_pick", "id": "a", "text": "新手1区"})
        check("click 老节点默认零偏移（不配 offset/jitter = 点正中）",
              st_old.offset == (0, 0) and st_old.jitter == (0, 0)
              and st_old.mode == "click")

        # 11a2) roi_number：不认锚点，框内唯一数字行直接换算入库
        #       （截图里的资源条：图标+「1.17万」，没有任何可认的文字标签）
        check("parse_game_number：单位换算 / 千分位 / 裸数字 / 无数字",
              parse_game_number("1.17万") == 11700
              and parse_game_number("695") == 695
              and parse_game_number("金币:12345") == 12345
              and parse_game_number("1,234") == 1234
              and parse_game_number("1.2亿") == 120000000
              and parse_game_number("3.5k") == 3500
              and parse_game_number("12w") == 120000
              and parse_game_number("我的金币") is None)
        # number 用法同款换算（存量"金币:12345"口径不变）
        cxr1 = _SpCtx()
        ocr.find_text = lambda img, needle, **k: {
            "x": 100, "y": 200, "text": "金币:1.17万", "sim": 1.0, "ms": 5}
        r = _mk_step("number", var_name="gold").tick(cxr1)
        check("number：带万单位的行换算入库（1.17万→11700，不再是 117）",
              r == "ok" and flow_var_get(cxr1, "gold") == 11700)
        # roi_number 判据 = 认得出数的行（图标常被 OCR 串成杂字符）
        cxr = _SpCtx()
        ocr.list_text = lambda img, roi=None, **k: [
            {"text": "金市", "x0": 0, "y0": 0, "x1": 30, "y1": 20},
            {"text": "1.17万", "x0": 40, "y0": 0, "x1": 100, "y1": 20}]
        r = _mk_step("roi_number", text="", var_name="金币",
                     roi=(0, 0, 120, 30), min_interval=0).tick(cxr)
        check("roi_number：忽略图标杂字符行、唯一数字行换算入库",
              r == "ok" and flow_var_get(cxr, "金币") == 11700)
        # 多个数字行 = 框大了，当没认出（歧义 WARN 只打一次）
        cxr2 = _SpCtx()
        ocr.list_text = lambda img, roi=None, **k: [
            {"text": "1.17万", "x0": 0, "y0": 0, "x1": 60, "y1": 20},
            {"text": "695", "x0": 70, "y0": 0, "x1": 120, "y1": 20}]
        sr2 = _mk_step("roi_number", var_name="金币", roi=(0, 0, 200, 30),
                       min_interval=0, timeout=999)
        seqr = [sr2.tick(cxr2) for _ in range(3)]
        check("roi_number：框内两个数字 → 歧义当没认出、不写变量、WARN 只一次",
              seqr == ["running"] * 3 and flow_var_get(cxr2, "金币") is None
              and sum(1 for lv, m in cxr2.lines if lv == "WARN") == 1)
        # 同款两行数字、框收窄到只圈住第一条：第二条是补边扫进来的邻行（中心在框外），
        # _roi_contains 剔除后不再误判歧义（实测坑：窄框读金币常年判 2 歧义、永远空放）
        cxr4 = _SpCtx()
        ocr.list_text = lambda img, roi=None, **k: [
            {"text": "1.17万", "x0": 0, "y0": 0, "x1": 60, "y1": 20},
            {"text": "695", "x0": 70, "y0": 0, "x1": 120, "y1": 20}]
        r = _mk_step("roi_number", var_name="金币", roi=(0, 0, 60, 30),
                     min_interval=0).tick(cxr4)
        check("roi_number：小框补边扫到的框外邻行剔除、框内唯一数照常读",
              r == "ok" and flow_var_get(cxr4, "金币") == 11700)
        # 没框 = 直接 failed（不框等于全屏乱抓数字）
        cxr3 = _SpCtx()
        r = _mk_step("roi_number", var_name="金币", roi=None).tick(cxr3)
        check("roi_number：没框识别区域 → failed（防全屏乱抓）",
              r == "failed" and flow_var_get(cxr3, "金币") is None)

        # 11d) scroll：识别窗到期滑一次→停稳→再认；滑满仍无 → failed（接败口）
        #      识别桩恒认不出，触发滑动 → 滑满收口。timeout=-1：起跑即到期
        #      （Windows monotonic 粒度 ~15ms，连跑 8 拍 elapsed 恒 0，填 0 不触发）
        ocr.find_text = lambda img, needle, **k: None
        sp = _mk_step("click", text="目标", on_timeout="scroll", timeout=-1,
                      min_interval=0, scroll_max=2, scroll_px=200,
                      scroll_dir="down", settle_secs=0.0,
                      fatal_cb=None)   # 接败口：无 fatal → 收 failed
        cx6 = _SpCtx()
        seq = [sp.tick(cx6) for _ in range(8)]
        check("scroll：滑满 scroll_max 次仍无 → 收口 failed（走败口不硬退出）",
              sp._finder.left == 0 and len(_swipes) == 2 and seq[-1] == "failed")
        # 方向换算：down = 手指自上而下（起点在上、终点在下，内容下移）
        check("scroll：down 方向起点 y<终点 y（内容往下露上方）",
              _swipes[0][1] < _swipes[0][3])

        # 11d2) 识别稳定 stable_secs（2026-10-11）：认出后要连续认出同一结果
        #       满 N 秒才处置；中途认到别处/认不出 → 清零重来。假时钟不真睡。
        _mono_sp = _t10j.monotonic
        _SPCLOCK = [500.0]
        _t10j.monotonic = lambda: _SPCLOCK[0]
        try:
            ocr.find_text = lambda img, needle, **k: {
                "x": 100, "y": 200, "text": "金币:12345", "sim": 1.0, "ms": 5}
            sp2 = _mk_step("number", var_name="gold", timeout=999,
                           min_interval=0, stable_secs=1.5)
            cxs = _SpCtx()
            _taps.clear()
            r0 = sp2.tick(cxs)
            check("稳定·首拍认出不满 N 秒 → running 不处置（变量未写）",
                  r0 == "running" and flow_var_get(cxs, "gold") is None)
            _SPCLOCK[0] += 0.8
            r1 = sp2.tick(cxs)
            check("稳定·到 0.8s 仍 running（未满 1.5s）",
                  r1 == "running" and flow_var_get(cxs, "gold") is None)
            _SPCLOCK[0] += 0.8          # 累计 1.6s ≥ 1.5s
            r2 = sp2.tick(cxs)
            check("稳定·撑满 → 处置 ok、写变量", r2 == "ok"
                  and flow_var_get(cxs, "gold") == 12345)
            # 认出结果在变（文字不同）→ 计时清零重来，永不处置
            sp3 = _mk_step("number", var_name="gold", timeout=999,
                           min_interval=0, stable_secs=1.0)
            cxs2 = _SpCtx()
            _SPCLOCK[0] = 600.0
            ocr.find_text = lambda img, needle, **k: {
                "x": 100, "y": 200, "text": "金币:1", "sim": 1.0, "ms": 5}
            sp3.tick(cxs2)
            _SPCLOCK[0] += 0.9
            ocr.find_text = lambda img, needle, **k: {
                "x": 300, "y": 400, "text": "金币:2", "sim": 1.0, "ms": 5}
            rv = sp3.tick(cxs2)         # 文字/位置变了 → 清零
            _SPCLOCK[0] += 0.9
            ocr.find_text = lambda img, needle, **k: {
                "x": 300, "y": 400, "text": "金币:2", "sim": 1.0, "ms": 5}
            rv2 = sp3.tick(cxs2)        # 0.9s < 1.0s（清零后重攒）
            check("稳定·认出结果在变：每段都不满 → 恒 running 不处置",
                  rv == "running" and rv2 == "running"
                  and flow_var_get(cxs2, "gold") is None)
            # 0 = 认出即处置（老行为零变化）
            ocr.find_text = lambda img, needle, **k: {
                "x": 100, "y": 200, "text": "金币:12345", "sim": 1.0, "ms": 5}
            sp4 = _mk_step("number", var_name="gold", timeout=999, min_interval=0)
            cxs3 = _SpCtx()
            check("稳定·0：认出即处置（老行为逐字不变）",
                  sp4.tick(cxs3) == "ok"
                  and flow_var_get(cxs3, "gold") == 12345)
        finally:
            _t10j.monotonic = _mono_sp

        # 11f) 旧 pick_pair 用法已删：ensure_graph 载入即换算 [点区→sleep→点服]
        pp = {"id": "pp1", "type": "server_pick", "mode": "pick_pair",
              "x": 100, "y": 200, "text": "含沙射影6服", "zone_text": "影区",
              "roi": [1, 2, 3, 4], "roi_server": [5, 6, 7, 8],
              "pair_gap_secs": 2, "timeout": 9, "on_timeout": "scroll",
              "min_sim": 88, "scroll_max": 3}
        g_pp = {"version": 2,
                "nodes": [{"id": "in1", "type": "sleep"}, pp,
                          {"id": "out1", "type": "sleep"}],
                "edges": [{"from": "in1", "port": "out", "to": "pp1"},
                          {"from": "pp1", "port": "out", "to": "out1"},
                          {"from": "pp1", "port": "fail", "to": "out1"}]}
        g2 = ensure_graph(g_pp)
        trio = [n for n in g2["nodes"] if n.get("type") == "server_pick"]
        sleep_mid = next((n for n in g2["nodes"] if n.get("type") == "sleep"
                          and n.get("label") == "步间等待"), None)
        check("换算：pick_pair 消失、变两 pick 一步 sleep",
              not any(n.get("mode") == "pick_pair" for n in g2["nodes"])
              and len(trio) == 2 and sleep_mid is not None
              and sleep_mid.get("seconds") == 2
              and g2["nodes"] is not g_pp["nodes"])   #  deepcopy，原图不动
        a, c = trio
        check("换算：区名=zone_text 覆盖 / 服名=全名，共用参照抄",
              a.get("text") == "影区" and a.get("roi") == [1, 2, 3, 4]
              and c.get("text") == "含沙射影6服" and c.get("roi") == [5, 6, 7, 8]
              and a.get("timeout") == 9 and a.get("on_timeout") == "scroll"
              and a.get("min_sim") == 88 and a.get("scroll_max") == 3
              and c.get("timeout") == 9 and c.get("scroll_max") == 3
              and "roi_server" not in a and "zone_text" not in a)
        ins = [e for e in g2["edges"] if e.get("to") == a["id"]]
        outs = [e for e in g2["edges"] if e.get("from") == c["id"]]
        check("换算：入边接首节、out/fail 挂末节、fail 补首节一条",
              any(e["from"] == "in1" and e["port"] == "out" for e in ins)
              and any(e["port"] == "fail" for e in outs)
              and sum(e.get("port") == "fail" and e.get("from") == a["id"]
                      for e in g2["edges"]) == 1
              and not any(e.get("to") == "pp1" or e.get("from") == "pp1"
                          for e in g2["edges"]))
        # 自动取前 4 字（zone_text 没填）：全名前 4 字当区名（游戏区名恒 4 字）
        g3 = ensure_graph({"version": 2, "nodes": [
            {**pp, "zone_text": ""}], "edges": []})
        check("换算：zone_text 没填 → 取全名前 4 字当区名",
              next(n for n in g3["nodes"]
                   if n.get("label") == "选区").get("text") == "含沙射影")
        # 区名是全号通用的静态字（拆得出来）不许被误换成派生列
        check("换算：静态区名不受派生列规则影响",
              next(n for n in g2["nodes"]
                   if n.get("label") == "选区").get("text") == "影区")
        # 区服占位符（多开各号各区的正主用法）：运行时才展开、静态取不了值
        # → 区名换成派生列 {账号.选区}，服名照旧占位符
        g_ph = ensure_graph({"version": 2, "nodes": [
            {**pp, "text": "{账号.区服}", "zone_text": "{账号.区服}"}],
            "edges": []})
        pa = next(n for n in g_ph["nodes"] if n.get("label") == "选区")
        pc = next(n for n in g_ph["nodes"] if n.get("label") == "选服")
        check("换算：区服占位符 → 区名用派生列 {账号.选区}、服名保持原样",
              pa.get("text") == "{账号.选区}" and pc.get("text") == "{账号.区服}")
        # 前 4 字口径不看数字（旧"拆不出=原样"特例已随规则一并废掉）：5 字照切
        g4 = ensure_graph({"version": 2, "nodes": [
            {**pp, "zone_text": "", "text": "无数字名长"}], "edges": []})
        check("换算：区名恒取前 4 字（跟有没有数字无关）",
              next(n for n in g4["nodes"]
                   if n.get("label") == "选区").get("text") == "无数字名")
        # 循环体里的 pick_pair 也换算（顺序行，原地替换）
        g5 = ensure_graph({"version": 2, "nodes": [
            {"id": "lp", "type": "loop", "steps": [dict(pp)]}], "edges": []})
        check("换算：循环体内 pick_pair 同样拆成三行",
              len(g5["nodes"][0]["steps"]) == 3
              and [s.get("type") for s in g5["nodes"][0]["steps"]]
              == ["server_pick", "sleep", "server_pick"])
        # 无 pick_pair 的老图：ensure_graph 原对象返回（零拷贝零开销）
        g_clean = {"version": 2, "nodes": [{"id": "x", "type": "sleep"}],
                   "edges": []}
        check("换算：无 pick_pair 的图原对象返回不拷贝",
              ensure_graph(g_clean) is g_clean)
        # 11f2) 独立弹窗区节点摘除（2026-09-25 功能下线）：入边直连出边目标、
        # 败口线丢弃、循环体「清弹窗」行绝不连坐；无该节点的图零拷贝。
        gpz = {"version": 2, "nodes": [
            {"id": "a", "type": "sleep"},
            {"id": "pz", "type": "popup_zone", "modules": []},
            {"id": "b", "type": "sleep"}, {"id": "c", "type": "sleep"}],
            "edges": [{"from": "a", "to": "pz", "port": "out"},
                      {"from": "pz", "to": "b", "port": "out"},
                      {"from": "pz", "to": "c", "port": "fail"}]}
        gd = ensure_graph(gpz)
        check("弹窗区摘除：节点没了、a→b 直连、败口线丢弃",
              all(n["id"] != "pz" for n in gd["nodes"])
              and {"from": "a", "to": "b", "port": "out"} in gd["edges"]
              and not any(e.get("to") == "c" or e.get("port") == "fail"
                          for e in gd["edges"]))
        check("弹窗区摘除：幂等（二次载入原对象返回）", ensure_graph(gd) is gd)
        glz = {"version": 2, "nodes": [
            {"id": "lp", "type": "loop",
             "steps": [{"id": "z", "type": "popup_zone", "modules": []}]}],
            "edges": []}
        check("弹窗区摘除：循环体「清弹窗」行不连坐",
              ensure_graph(glz) is glz
              and glz["nodes"][0]["steps"][0]["type"] == "popup_zone")
        # 残留节点绕过换算直接编译：降级成第一步 + 前置 WARN，绝不静默
        st_legacy = _action_step({"type": "server_pick", "id": "a",
                                  "mode": "pick_pair", "text": "新手1区",
                                  "roi": [1, 2, 3, 4]})
        check("pick_pair 残留编译：降级首步 + WARN 点名",
              isinstance(st_legacy, _SequenceStep)
              and isinstance(st_legacy._steps[0], LogStep)
              and "选区选服" in st_legacy._steps[0].message
              and isinstance(st_legacy._steps[1], ServerPickStep)
              and st_legacy._steps[1].mode == "click")
        # ServerPickStep 不再收 pick_pair 专属参：回落成 click 单发
        spr = ServerPickStep("x", text="t", mode="pick_pair")
        check("ServerPickStep：未知 mode 回落 click", spr.mode == "click")
        # 11g) _MIGRATIONS 槽位（分发审计 P1-⑬）：登记 v2→v3 后载入跑一遍、
        # 版本升上去即幂等；空表时 ensure_graph 行为零变化（上面各 check 已证）。
        _MIGRATIONS[2] = lambda g: {**g, "version": 3, "migrated": True}
        try:
            gm = ensure_graph({"version": 2, "nodes": [], "edges": []})
            check("迁移链：注册后老 v2 载入即升 v3 并跑换算",
                  gm.get("migrated") is True and gm.get("version") == 3)
            gm2 = ensure_graph(gm)     # 已是 v3：链上无 3 → 原样，不回落 v1
            check("迁移链：升版后重入幂等（不重复跑、不落 v1 通道）",
                  gm2.get("version") == 3 and gm2.get("migrated") is True)
        finally:
            _MIGRATIONS.pop(2, None)
    finally:
        globals()["_tap"] = _tap_real
        _fsm11.swipe = _swipe_real
        ocr.find_text = _ocr_real
        ocr.list_text = _ocr_list_real
        # 11e) 流程变量读取侧（P2）：var 条件比较 + {占位符} 替换
        cond_ge = _make_condition({"kind": "var", "name": "hp", "op": ">=", "value": "10"})
        cxv = _LC()
        check("var 条件：变量没值 → 恒 False 走 N", cond_ge(cxv) is False)
        flow_var_set(cxv, "hp", 12)
        check("var 条件：12>=10 成立", cond_ge(cxv) is True)
        flow_var_set(cxv, "hp", 9)
        check("var 条件：9>=10 不成立", cond_ge(cxv) is False)
        flow_var_set(cxv, "hp", 10)
        check("var 条件：10>=10 边界成立", cond_ge(cxv) is True)
        cond_neq = _make_condition({"kind": "var", "name": "hp", "op": "!=", "value": "5"})
        check("var 条件：!= 数比", cond_neq(cxv) is True)
        flow_var_set(cxv, "pos", (250, 400))       # position 存的是坐标串
        cond_peq = _make_condition({"kind": "var", "name": "pos", "op": "==", "value": "10"})
        check("var 条件：化不成数只认相等性，> 之类判不成立",
              cond_peq(cxv) is False)
        check("var 条件：未知 op 恒 False",
              _make_condition({"kind": "var", "name": "hp", "op": "~~", "value": "10"})(cxv) is False)
        # {占位符}：有值换值、没值原样留（一眼看出哪步没读）
        cxp = _LC()
        flow_var_set(cxp, "体力", 88)
        check("占位符：有值换值 + 多变量混排",
              _fmt_text(cxp, "体力={体力}，金币={gold}") == "体力=88，金币={gold}")
        check("占位符：None 值不误空", _fmt_text(cxp, "{没见过的}") == "{没见过的}")
        # {账号.列名} 命名空间占位符：账号表任意列一处分全节点通（判断/识字/打印）
        from core import database as _db
        _abi_real = _db.accounts_by_instance
        _db.accounts_by_instance = lambda: {
            0: {"server": "含沙射影6服", "level": 45, "account": "acc1",
                "password": "secret", "name": ""}}
        try:
            check("账号占位符：中文列名取值（区服/等级）",
                  _fmt_text(cxp, "{账号.区服} {账号.等级}级") == "含沙射影6服 45级")
            check("账号占位符：英文数据库字段直写也认",
                  _fmt_text(cxp, "{账号.server}") == "含沙射影6服")
            check("账号占位符：密码列永不供取（防进日志）",
                  _fmt_text(cxp, "{账号.密码}") == "{账号.密码}")
            check("账号占位符：空值列（备注没填）原样留",
                  _fmt_text(cxp, "{账号.备注}") == "{账号.备注}")
            check("账号占位符：列名打错原样留，不静默空串",
                  _fmt_text(cxp, "{账号.不存在的列}") == "{账号.不存在的列}")
            # 派生列：进服两步专用，都从区服列现推，账号表不加列
            check("派生列 选区：区服列前 4 字（含沙射影6服→含沙射影）",
                  _fmt_text(cxp, "{账号.选区}") == "含沙射影")
            check("派生列 选服：区服全名原样（数字锚点防认错服）",
                  _fmt_text(cxp, "{账号.选服}") == "含沙射影6服")
            _sv_real = _db.accounts_by_instance
            _db.accounts_by_instance = lambda: {0: {"server": "无数字区名"}}
            try:
                check("派生列 选区：超 4 字照切不看数字（无数字区名→无数字区）",
                      _fmt_text(cxp, "{账号.选区}") == "无数字区")
            finally:
                _db.accounts_by_instance = _sv_real
            _db.accounts_by_instance = lambda: {0: {"server": "新手区"}}
            try:
                check("派生列 选区：不足 4 字取整串（新手区→新手区）",
                      _fmt_text(cxp, "{账号.选区}") == "新手区")
            finally:
                _db.accounts_by_instance = _sv_real
            # 判断节点文字条件端到端：期望文字 {账号.区服} 展开后才去 OCR
            from core import ocr as _ocr_mod
            _ft_real = _ocr_mod.find_text
            _av_real = _ocr_mod.available
            _ocr_mod.available = lambda: True   # check 短路判依赖，未装机的机器也照测展开
            seen = []
            _ocr_mod.find_text = lambda img, needle, **k: (seen.append(needle), None)[1]
            try:
                _ck = _make_condition({"kind": "text", "target": "{账号.区服}", "ttl": 0.4})
                cxp.frame = None       # _LC 没这属性；桩 find_text 不看它
                _ck(cxp)
                check("判断文字条件：{账号.区服} 展开后才进 OCR（非占位符原文）",
                      seen == ["含沙射影6服"])
            finally:
                _ocr_mod.find_text = _ft_real
                _ocr_mod.available = _av_real
        finally:
            _db.accounts_by_instance = _abi_real
        # 打印节点 target=var 走通（无 log 走 print 分支不抛即过；整段吞异常纪律）
        _debug_print(_LC(), {"target": "var", "var_name": "x"})
        check("打印 target=var 不崩（吞异常纪律）", True)
        # 诊断闭包：var 条件收口点名"没这个值"
        ctxd = _LC()
        ctxd.extra = {}        # _LC.extra 是类属性：前面测试写过 hp，这里自配一份空的
        _make_branch_diag({"id": "b", "type": "branch", "label": "够不够",
                           "cond": {"kind": "var", "name": "hp", "op": ">=",
                                    "value": "10"}})(ctxd)
        check("var 判不过诊断：点名变量没值",
              ctxd.lines and "hp" in ctxd.lines[0][1] and "没值" in ctxd.lines[0][1])

    # ── 点全部 TapAllStep：收集拍→逐拍点→点完收口；零命中等门三档 ──
    _old_tap, _old_col = globals()["_tap"], globals()["tap_all_collect"]
    try:
        _taps13 = []
        globals()["_tap"] = lambda c, x, y: _taps13.append((x, y))
        globals()["tap_all_collect"] = \
            lambda c, cat, nm, **kw: [(10, 10), (20, 20), (30, 30)]
        ctx13 = _LC()
        st13 = TapAllStep("all", "c", "n", gap_secs=0, gate_timeout=99)
        rs13 = [st13.tick(ctx13) for _ in range(3)]
        check("点全部：首拍收集即首点，每拍一个，3 处点完收口",
              rs13 == ["running", "running", "ok"] and len(_taps13) == 3)
        _taps13.clear(); st13.reset()
        st13.tick(ctx13)
        check("点全部：reset 后重新收集重头点", len(_taps13) == 1)
        globals()["tap_all_collect"] = lambda c, cat, nm, **kw: []
        check("点全部：零命中+不等门 = 当场收口",
              TapAllStep("g0", "c", "n", gate_timeout=0).tick(_LC()) == "ok")
        st14 = TapAllStep("gf", "c", "n", gate_timeout=0.01,
                          on_timeout_log=_warn_tap_all_log,
                          ok_on_timeout=True)
        ctx14 = _LC()
        check("点全部：等门没图 = running", st14.tick(ctx14) == "running")
        _t10.sleep(0.05)
        check("点全部：门超时 continue = ok + WARN 点名",
              st14.tick(ctx14) == "ok"
              and any(lv == "WARN" for lv, _ in ctx14.lines))
        st16 = TapAllStep("gx", "c", "n", gate_timeout=0.01,
                          fatal_cb=lambda c, s: (_ for _ in ()).throw(
                              FlowTimeoutExit("t")))
        st16.tick(_LC()); _t10.sleep(0.05)
        try:
            st16.tick(_LC()); r16 = "没抛"
        except FlowTimeoutExit:
            r16 = "抛了"
        check("点全部：门超时 exit = FlowTimeoutExit 穿透", r16 == "抛了")
        st17 = TapAllStep("gd", "c", "n", gap_secs=99, gate_timeout=99)
        globals()["tap_all_collect"] = \
            lambda c, cat, nm, **kw: [(1, 1), (2, 2)]
        _taps13.clear()
        ctx17 = _LC()
        st17.tick(ctx17)                    # 收集+点第 1 个
        r17 = st17.tick(ctx17)              # 间隔未到：不点
        check("点全部：点击间隔未到不落手",
              r17 == "running" and len(_taps13) == 1)
    finally:
        globals()["_tap"], globals()["tap_all_collect"] = _old_tap, _old_col

    # ── 遍历全部 MultiScanStep：点掉队列 / 进一个回一个拍序 / 迷路判败 ──
    _old_tap = globals()["_tap"]
    _old_col = globals()["tap_all_collect"]
    _old_pb = globals()["_press_back"]
    _old_tp = globals()["template_present"]
    try:
        _taps18, _backs18 = [], []
        globals()["_tap"] = lambda c, x, y: _taps18.append((x, y))
        globals()["_press_back"] = lambda c: _backs18.append("back")
        globals()["tap_all_collect"] = \
            lambda c, cat, nm, **kw: [(10, 10), (20, 20)]
        import bot.fsm as _fsm18   # 体步首败会走留证截图：_LC 没截屏能力，桩掉
        _old_sf18 = _fsm18.save_fail_capture
        _fsm18.save_fail_capture = lambda *a, **k: "x"

        class _Ctx18(_LC):
            def __init__(self):
                super().__init__()
                self.extra = {}        # _LC.extra 是类属性：各拿各的变量表

        # 18a) 点掉模式：每拍一个，2 处点完收口 + 变量记数
        cx18 = _Ctx18()
        st18 = MultiScanStep("ms", "c", "n", mode="tap", gap_secs=0,
                             gate_timeout=99, var_name="hits")
        rs18 = [st18.tick(cx18) for _ in range(2)]
        check("遍历·点掉：每拍一个，2 处点完收口",
              rs18 == ["running", "ok"] and _taps18 == [(10, 10), (20, 20)])
        check("遍历·点掉：命中数第一时间写进变量",
              cx18.extra.get("fv:hits") == 2)

        # 18b) 进一个模式（无子流程体、无确认图）：点→返回→点→返回→收口，
        #      且落手拍数=动作数（每拍最多落一次手）
        _taps18.clear(); _backs18.clear()
        st18b = MultiScanStep("mbe", "c", "n", mode="enter", settle_secs=0,
                              gate_timeout=99, var_name="hits")
        n18b = 0
        r18b = st18b.tick(_Ctx18())
        while r18b == "running" and n18b < 30:
            r18b = st18b.tick(_Ctx18())
            n18b += 1
        act18b = len(_taps18) + len(_backs18)
        check("遍历·进一个：两处各 点+返回，最终收口",
              r18b == "ok" and _taps18 == [(10, 10), (20, 20)]
              and _backs18 == ["back", "back"])
        check("遍历·进一个：落手拍数=动作数（每拍至多一次手）", act18b == n18b + 1)

        # 18c) 带子流程体：体 done 后才返回；体内 failed 计数不拦路
        #      注意 FSM 的失败口径：首败只截图留证（给一次重试机会），连败两次
        #      才真返回 failed —— 体步要测"没办成"必须一直败。
        class _B18(Step):
            def __init__(self, always_fail=False):
                super().__init__("b18")
                self._f = always_fail
            def tick(self, ctx):
                _taps18.append("body")
                if self._f:
                    return "failed"
                return "ok"
        _taps18.clear(); _backs18.clear()
        cx18c = _Ctx18()
        st18c = MultiScanStep("mbd", "c", "n", mode="enter", body=[_B18()],
                              settle_secs=0, gate_timeout=99)
        r18c = st18c.tick(cx18c)
        while r18c == "running":
            r18c = st18c.tick(cx18c)
        check("遍历·体：点完才跑体、体完才返回，两处跑完收口",
              r18c == "ok" and _taps18 == [(10, 10), "body", (20, 20), "body"]
              and len(_backs18) == 2)
        _taps18.clear(); _backs18.clear()
        st18d = MultiScanStep("mbf", "c", "n", mode="enter",
                              body=[_B18(always_fail=True)], settle_secs=0,
                              gate_timeout=99)
        n18d = 0
        r18d = st18d.tick(cx18c)
        while r18d == "running" and n18d < 50:
            r18d = st18d.tick(cx18c)
            n18d += 1
        check("遍历·体失败：一处没跑完不烧整场，照样返回点下一处",
              r18d == "ok"
              and [p for p in _taps18 if p != "body"] == [(10, 10), (20, 20)]
              and len(_backs18) == 2)

        # 18e) 返回后迷路：确认图永远不出现 → 超时判 failed（绝不拿旧坐标瞎点）
        globals()["template_present"] = lambda c, cat, nm, **kw: False
        _taps18.clear(); _backs18.clear()
        st18e = MultiScanStep("mbl", "c", "n", mode="enter",
                              confirm=("c", "home"), confirm_timeout=0.05,
                              settle_secs=0, gate_timeout=99)
        n18e = 0
        r18e = st18e.tick(_Ctx18())
        while r18e == "running" and n18e < 50:
            _t10.sleep(0.03)
            r18e = st18e.tick(_Ctx18())
            n18e += 1
        check("遍历·迷路：返回后等不回列表页 = failed，且只点了一处",
              r18e == "failed" and _taps18 == [(10, 10)])
        # 确认图回来才点下一处
        globals()["template_present"] = lambda c, cat, nm, **kw: True
        _taps18.clear(); _backs18.clear()
        st18f = MultiScanStep("mbk", "c", "n", mode="enter",
                              confirm=("c", "home"), confirm_timeout=5,
                              settle_secs=0, gate_timeout=99)
        r18f = st18f.tick(_Ctx18())
        while r18f == "running":
            r18f = st18f.tick(_Ctx18())
        check("遍历·确认：标志图在场才继续，两处办完收口",
              r18f == "ok" and _taps18 == [(10, 10), (20, 20)])

        # 18g) 零命中门三档（与点全部同口径；命中数变量只在真收到底时写，
        #      零命中不碰变量 —— P2 读取侧上线时再定 0 值语义）
        globals()["tap_all_collect"] = lambda c, cat, nm, **kw: []
        check("遍历·零命中不等门 = 当场收口",
              MultiScanStep("g0", "c", "n", gate_timeout=0).tick(_Ctx18()) == "ok")
        cx18h = _Ctx18()
        st18g = MultiScanStep("gc", "c", "n", gate_timeout=0.01, var_name="h",
                              ok_on_timeout=True, on_timeout_log=_warn_multi_scan_log)
        st18g.tick(cx18h); _t10.sleep(0.05)
        check("遍历·门超时 continue = ok + WARN 点名",
              st18g.tick(cx18h) == "ok"
              and any(lv == "WARN" for lv, _ in cx18h.lines))
        st18g2 = MultiScanStep("gx", "c", "n", gate_timeout=0.01,
                               fatal_cb=lambda c, s: (_ for _ in ()).throw(
                                   FlowTimeoutExit("t")))
        st18g2.tick(_Ctx18()); _t10.sleep(0.05)
        try:
            st18g2.tick(_Ctx18()); r18g = "没抛"
        except FlowTimeoutExit:
            r18g = "抛了"
        check("遍历·门超时 exit = FlowTimeoutExit 穿透", r18g == "抛了")

        # 18i) 返回键可关：press_back=False 一拍不按返回，照样两处收口
        #      （回列表由子流程自己负责 —— 编译层缺键兜 True 保存量行为）
        globals()["tap_all_collect"] = \
            lambda c, cat, nm, **kw: [(10, 10), (20, 20)]   # 18g 置空过，先复位
        _taps18.clear(); _backs18.clear()
        st18i = MultiScanStep("mbn", "c", "n", mode="enter", settle_secs=0,
                              gate_timeout=99, press_back=False)
        r18i = st18i.tick(_Ctx18())
        while r18i == "running":
            r18i = st18i.tick(_Ctx18())
        check("遍历·进一个关返回：一拍返回都不按，两处照常收口",
              r18i == "ok" and _backs18 == []
              and _taps18 == [(10, 10), (20, 20)])

        # 18h) 编译层：注册闸门/败口名单 + 进一个的子流程体按 subflow 同款拆穿
        check("遍历全部：算动作节点（吃落手闸门）", _is_action_node({"type": "multi_scan"}))
        check("遍历全部：在败口名单 FAILABLE_TYPES 里", "multi_scan" in FAILABLE_TYPES)
        _tm_real18 = globals()["_tpl_missing"]
        globals()["_tpl_missing"] = lambda c, n: False   # 假模板名别走缺图降级
        try:
            _sub18 = {"version": 2, "nodes": [{"id": "w", "type": "sleep",
                                               "seconds": 0.1}], "edges": []}
            _b18 = _make_build_step({"子A": _sub18, "环B": {"version": 2, "nodes": [],
                                                            "edges": []}},
                                    ["主"], frozenset())
            ms_node = {"id": "m", "type": "multi_scan", "category": "c",
                       "name": "头像", "mode": "enter", "flow_name": "子A",
                       "roi": [1, 2, 3, 4], "confirm_tpl": {"category": "c",
                                                            "name": "列表"},
                       "max_hits": 9, "gap_secs": 0.7}
            step18 = _b18(ms_node)
            check("编译·进一个：MultiScanStep 吃 roi/confirm/body/max_hits",
                  isinstance(step18, MultiScanStep) and step18.roi == [1, 2, 3, 4]
                  and step18.confirm == ("c", "列表") and len(step18.body) == 1
                  and step18.max_hits == 9)
            check("编译·关返回：缺键兜 True（存量节点照按），显式 False 传进",
                  step18.press_back is True
                  and _b18({**ms_node, "press_back": False}).press_back is False)
            check("编译·没选子流程：整节点降级 WARN",
                  isinstance(_b18({"id": "m2", "type": "multi_scan", "mode": "enter"}),
                             LogStep))
            check("编译·成环引用：拆穿为 WARN 不无限展开",
                  isinstance(_make_build_step({"子A": _sub18}, ["主", "子A"],
                                              frozenset())(ms_node), LogStep))
            # 接败口：gate 超时不再注入 fatal（异常穿透会抢败口的线）
            step18f = _make_build_step({"子A": _sub18}, ["主"],
                                       frozenset({"m"}))(ms_node)
            check("编译·接了败口：fatal_cb 让位（超时返回 failed 走线）",
                  step18f.fatal_cb is None)
            globals()["_tpl_missing"] = lambda c, n: True   # 主扫描图缺图
            check("编译·扫描图缺失：缺图降级点名",
                  isinstance(_b18({"id": "m3", "type": "multi_scan",
                                   "category": "c", "name": "没这张"}), LogStep))
        finally:
            globals()["_tpl_missing"] = _tm_real18
    finally:
        globals()["_tap"], globals()["tap_all_collect"] = _old_tap, _old_col
        globals()["_press_back"], globals()["template_present"] = _old_pb, _old_tp
        _fsm18.save_fail_capture = _old_sf18

    # ── 遍历全部 P2/P3：读数存变量 / 文字源 / 去重边扫边办 ──
    _old_tap19 = globals()["_tap"]
    _old_col19 = globals()["tap_all_collect"]
    _old_pb19 = globals()["_press_back"]
    _old_otc19 = globals()["ocr_text_collect"]
    _old_nr19 = globals()["neighbor_read"]
    try:
        _taps19 = []
        globals()["_tap"] = lambda c, x, y: _taps19.append((x, y))
        globals()["_press_back"] = lambda c: None

        # 19a) 读数模式：唯一数字才写 var_i，歧义不写脏值；总数照常收集即写
        globals()["tap_all_collect"] = lambda c, cat, nm, **kw: [(10, 10), (20, 20)]
        _reads19 = iter([(11700, "1.17万"), (None, None)])
        globals()["neighbor_read"] = lambda c, x, y, *b: next(_reads19)
        cx19a = _Ctx18()
        st19a = MultiScanStep("mr", "c", "n", mode="read", var_name="v")
        r19a = [st19a.tick(cx19a), st19a.tick(cx19a)]
        check("遍历·读数：每拍读一处，两处读完收口", r19a == ["running", "ok"])
        check("遍历·读数：唯一数字写 var_1，总数写 var",
              cx19a.extra.get("fv:v_1") == 11700 and cx19a.extra.get("fv:v") == 2)
        check("遍历·读数：歧义当没认出 —— var_2 不写 + WARN 点名",
              "fv:v_2" not in cx19a.extra
              and any(lv == "WARN" for lv, _ in cx19a.lines))
        check("遍历·读数：一下都不点", _taps19 == [])

        # 19b) 文字源：ocr_text_collect 出的 3 元组直接当点位（身份随行白送）
        globals()["ocr_text_collect"] = lambda c, t, **kw: [
            (5, 6, "私聊"), (7, 8, "私聊")]
        _taps19.clear()
        cx19b = _Ctx18()
        st19b = MultiScanStep("mt", "c", "", mode="tap", source="text",
                              text="私聊", min_sim=0.6, gap_secs=0,
                              gate_timeout=99, var_name="h")
        r19b = [st19b.tick(cx19b), st19b.tick(cx19b)]
        check("遍历·文字源：两处点完收口 + 计数写变量",
              r19b == ["running", "ok"] and _taps19 == [(5, 6), (7, 8)]
              and cx19b.extra.get("fv:h") == 2)

        # 19c) 去重边扫边办（文字源·tap）：每轮重扫，身份办过即跳，三轮收口。
        #      身份现读邻框（行文字=圈的词本身分不开人），按 x 给假昵称认脸
        _taps19.clear()
        globals()["neighbor_read"] = lambda c, x, y, *b: (None, f"脸{x}")
        cx19c = _Ctx18()
        st19c = MultiScanStep("md", "c", "", mode="tap", source="text",
                              text="私聊", dedupe=True, gap_secs=0,
                              gate_timeout=99, var_name="d")
        r19c, n19c = st19c.tick(cx19c), 0
        while r19c == "running" and n19c < 30:
            r19c = st19c.tick(cx19c)
            n19c += 1
        check("遍历·去重：两个身份各点一次后收口",
              r19c == "ok" and _taps19 == [(5, 6), (7, 8)])
        check("遍历·去重：轮数=办完轮+空手收口轮，var 写真实办成数",
              st19c._rounds == 3 and cx19c.extra.get("fv:d") == 2)

        # 19d) 去重·进一个：back 后旧快照作废重扫，绝不因"队列空了"提前收口
        globals()["tap_all_collect"] = lambda c, cat, nm, **kw: [(10, 10), (20, 20)]
        globals()["neighbor_read"] = lambda c, x, y, *b: (None, f"脸{x}")
        _taps19.clear()
        st19d = MultiScanStep("me", "c", "n", mode="enter", dedupe=True,
                              settle_secs=0, gate_timeout=99)
        r19d, n19d = st19d.tick(_Ctx18()), 0
        while r19d == "running" and n19d < 40:
            r19d = st19d.tick(_Ctx18())
            n19d += 1
        check("遍历·去重进一个：模板源靠邻域文字认脸，两处各进一次收口",
              r19d == "ok" and _taps19 == [(10, 10), (20, 20)])

        # 19e) 去重扫满轮数封顶：身份每轮都新鲜 = 判败（绝不空转）
        _round19 = [0]

        def _fresh(c, cat, nm, **kw):
            _round19[0] += 1
            return [(30, 30)]
        globals()["tap_all_collect"] = _fresh
        globals()["neighbor_read"] = \
            lambda c, x, y, *b: (None, f"新脸{_round19[0]}")
        st19e = MultiScanStep("mf", "c", "n", mode="tap", dedupe=True,
                              max_rounds=3, gap_secs=0, gate_timeout=99)
        r19e, n19e = st19e.tick(_Ctx18()), 0
        while r19e == "running" and n19e < 40:
            r19e = st19e.tick(_Ctx18())
            n19e += 1
        check("遍历·去重封顶：max_rounds 用尽还有新面孔 = failed",
              r19e == "failed" and st19e._rounds == 4)

        # 19g) 数字过滤（2026-10-06）：三处命中只有达标的一处进队列被点，
        #      读不出/不达标都跳过；命中数变量记的是滤后数
        globals()["tap_all_collect"] = lambda c, cat, nm, **kw: [
            (10, 10), (20, 20), (30, 30)]
        _reads19g = iter([(5, "a"), (30, "b"), (None, None)])
        globals()["neighbor_read"] = lambda c, x, y, *b: next(_reads19g)
        _taps19.clear()
        cx19g = _Ctx18()
        st19g = MultiScanStep("mg", "c", "n", mode="tap", gap_secs=0,
                              gate_timeout=99, var_name="g",
                              filt_op="lt", filt_val=20)
        r19g = st19g.tick(cx19g)
        check("遍历·数字过滤：只点达标的一处，读不出不点",
              r19g == "ok" and _taps19 == [(10, 10)]
              and cx19g.extra.get("fv:g") == 1)
        check("遍历·数字过滤：日志报原始/滤掉几处",
              any("原始 3 处，滤掉 2 处" in m for _, m in cx19g.lines))

        # 19h) 全被滤光 = 空手收口 ok（绝不进等门重烧、更不判败）
        globals()["tap_all_collect"] = lambda c, cat, nm, **kw: [
            (10, 10), (20, 20)]
        _reads19h = iter([(50, "a"), (60, "b")])
        globals()["neighbor_read"] = lambda c, x, y, *b: next(_reads19h)
        _taps19.clear()
        st19h = MultiScanStep("mh", "c", "n", mode="tap", gap_secs=0,
                              gate_timeout=99, filt_op="lt", filt_val=20)
        check("遍历·过滤全滤光：一拍收口 ok 不烧门",
              st19h.tick(_Ctx18()) == "ok" and _taps19 == [])
        globals()["tap_all_collect"] = lambda c, cat, nm, **kw: []
        st19h2 = MultiScanStep("mh2", "c", "n", mode="tap", gate_timeout=99,
                               filt_op="lt", filt_val=20)
        check("遍历·过滤但零命中：照走等门（running），不是空手收口",
              st19h2.tick(_Ctx18()) == "running")

        # 19f) 编译层：文字源不查模板图、OCR 没装当场拆穿、read+dedupe 强制关
        from core import ocr as _ocr19
        _old_avail19 = _ocr19.available
        ms_txt = {"id": "m", "type": "multi_scan", "source": "text",
                  "text": "私聊", "mode": "read", "dedupe": True,
                  "min_sim": 75, "read_dx": 8, "read_w": 120}
        try:
            _b19 = _make_build_step({}, ["主"], frozenset())
            _ocr19.available = lambda: False
            check("编译·文字源 OCR 没装：LogStep WARN（识字取物同纪律）",
                  isinstance(_b19(ms_txt), LogStep))
            _ocr19.available = lambda: True
            step19 = _b19(ms_txt)
            check("编译·文字源：不吃模板缺失检查 + min_sim/读框/源都传进",
                  isinstance(step19, MultiScanStep) and step19.source == "text"
                  and abs(step19.min_sim - 0.75) < 1e-9
                  and step19.read_box == (8, 0, 120, 48))
            check("编译·读数+去重无意义：强制关（界面置灰之外的双保险）",
                  step19.mode == "read" and step19.dedupe is False)
            step19f = _b19({**ms_txt, "mode": "tap",
                            "filter_op": "lt", "filter_val": 20})
            check("编译·数字过滤：符号/阈值透传",
                  isinstance(step19f, MultiScanStep) and step19f.filt_op == "lt"
                  and step19f.filt_val == 20.0)
            check("编译·读数+过滤无意义：强制关（同去重双保险）",
                  _b19({**ms_txt, "filter_op": "lt"}).filt_op == "")
            check("编译·过滤符号不认识：WARN 拆穿（绝不降级成不过滤瞎点）",
                  isinstance(_b19({**ms_txt, "mode": "tap",
                                   "filter_op": "bad"}), LogStep))
            check("编译·文字源没填文字：WARN 拆穿不空转",
                  isinstance(_b19({"id": "m2", "type": "multi_scan",
                                   "source": "text"}), LogStep))
        finally:
            _ocr19.available = _old_avail19
    finally:
        globals()["_tap"], globals()["tap_all_collect"] = _old_tap19, _old_col19
        globals()["_press_back"] = _old_pb19
        globals()["ocr_text_collect"] = _old_otc19
        globals()["neighbor_read"] = _old_nr19

    # ── 20) 停止信号：信箱 + 发信号节点 + 循环/遍历收闸（2026-10-06）──
    from bot.fsm import signal_post as _sp20, signal_take as _st20

    class _CtxSig(_LC):
        def __init__(self):
            super().__init__()
            self.extra = {}        # _LC.extra 是类属性：各拿各的信箱

    # 20a) 信箱语义：不认识留着、认领一次只销一笔、封顶 32 扔最旧
    cx20 = _CtxSig()
    _sp20(cx20, "封号"); _sp20(cx20, "背包满")
    check("信箱·不认识的信号留着等下家",
          _st20(cx20, ["别的"]) is None and cx20.extra["fs:pbox"] == ["封号", "背包满"])
    check("信箱·命中即销账且只销一笔",
          _st20(cx20, ["封号", "背包满"]) == "封号" and cx20.extra["fs:pbox"] == ["背包满"])
    for i in range(40):
        _sp20(cx20, f"s{i}")
    box20 = cx20.extra["fs:pbox"]
    check("信箱·封顶 32 扔最旧（爆箱不爆内存）",
          len(box20) == 32 and box20[0] == "s8" and box20[-1] == "s39")

    # 20b) 发信号节点：投递一次直通、重复拍不重投、reset 可再投、空名白发 WARN
    cx20b = _CtxSig()
    st20b = SignalStep("发信号", "封号")
    check("发信号·投递后直通 ok",
          st20b.tick(cx20b) == "ok" and cx20b.extra["fs:pbox"] == ["封号"])
    st20b.tick(cx20b)
    check("发信号·同一步重复拍只投一次（外层销账制的地基）",
          cx20b.extra["fs:pbox"] == ["封号"])
    st20b.reset(); cx20b.extra["fs:pbox"].clear()
    check("发信号·reset 后每圈都发得出来（循环体复用）",
          st20b.tick(cx20b) == "ok" and cx20b.extra["fs:pbox"] == ["封号"])
    cx20e = _CtxSig()
    check("发信号·空名：不投不炸、WARN 点名",
          SignalStep("发信号", "").tick(cx20e) == "ok"
          and "fs:pbox" not in cx20e.extra
          and any(lv == "WARN" for lv, _ in cx20e.lines))

    # 20c) 循环收闸：非目标不掐不清箱、命中 ok 出口 + 只销同名那笔、没登记零影响
    class _Nop(Step):
        def tick(self, ctx):
            return "ok"

    lp20 = LoopStep("L", [_Nop("n")], stop_on=["封号"])
    cx20c = _CtxSig()
    check("循环闸·空箱照跑", lp20.tick(cx20c) == "running")
    _sp20(cx20c, "别的")
    check("循环闸·非目标信号不掐（信箱原样留给下家）",
          lp20.tick(cx20c) == "running" and cx20c.extra["fs:pbox"] == ["别的"])
    _sp20(cx20c, "封号")
    check("循环闸·命中 → ok 走正常出口 + 只销同名一笔（别的信号不误吞）",
          lp20.tick(cx20c) == "ok" and cx20c.extra["fs:pbox"] == ["别的"])
    lp20g = LoopStep("L2", [_Nop("n")])
    cx20d = _CtxSig()
    _sp20(cx20d, "封号")
    check("循环闸·没登记名单：箱里有信号也绝不查（老行为逐字不变）",
          lp20g.tick(cx20d) == "running" and cx20d.extra["fs:pbox"] == ["封号"])

    # 20d) 遍历收闸：项边界才掐（当前项已办完）、剩余点位不碰、日志报还剩几处
    _old_tap20, _old_col20 = globals()["_tap"], globals()["tap_all_collect"]
    _taps20 = []
    try:
        globals()["_tap"] = lambda c, x, y: _taps20.append((x, y))
        globals()["tap_all_collect"] = lambda c, cat, nm, **kw: [
            (10, 10), (20, 20), (30, 30)]
        st20m = MultiScanStep("ms", "c", "n", mode="tap", gap_secs=0,
                              gate_timeout=99, stop_on=["封号"])
        cx20m = _CtxSig()
        check("遍历闸·首拍照常收点开工不掐",
              st20m.tick(cx20m) == "running" and _taps20 == [(10, 10)])
        _sp20(cx20m, "封号")
        check("遍历闸·下一处之前掐：跑完当前项再收，剩 2 处不碰",
              st20m.tick(cx20m) == "ok" and _taps20 == [(10, 10)]
              and len(st20m._pts) == 2
              and any("还剩 2 处" in m for _, m in cx20m.lines))
        check("遍历闸·吃过即清箱：外层同名单不连掐",
              cx20m.extra["fs:pbox"] == [])
        _taps20.clear()
        st20p = MultiScanStep("ms2", "c", "n", mode="tap", gap_secs=0, gate_timeout=99)
        cx20p = _CtxSig()
        _sp20(cx20p, "封号")
        check("遍历闸·没登记：信号悬着照办全部（老行为）",
              st20p.tick(cx20p) == "running" and cx20p.extra["fs:pbox"] == ["封号"]
              and _taps20 == [(10, 10)])
    finally:
        globals()["_tap"], globals()["tap_all_collect"] = _old_tap20, _old_col20

    # 20e) 编译层：signal → SignalStep 带名；stop_on 行形态两种都认、清洗去重
    step20s = _bld_lp({"id": "S", "type": "signal", "label": "掐循环", "name": "封号"})
    check("编译·发信号 → SignalStep（label 当步名、name 当信号）",
          isinstance(step20s, SignalStep) and step20s.signal == "封号")
    lp20n = _bld_lp({"id": "L", "type": "loop", "label": "循环",
                     "steps": [{"type": "sleep", "secs": 0}],
                     "stop_on": [{"name": "封号"}, {"name": " 封号 "},
                                 {"name": ""}, "背包满"]})
    check("编译·循环 stop_on：字典行+纯字符串都认，空名剔、重名并一",
          isinstance(lp20n, LoopStep) and lp20n.stop_on == ["封号", "背包满"])
    check("编译·stop_on 全空 = None 不建闸（存量流程零迁移）",
          _bld_lp({"id": "L", "type": "loop", "label": "循环",
                   "steps": [{"type": "sleep", "secs": 0}],
                   "stop_on": [{"name": ""}]}).stop_on is None)
    from core import ocr as _ocr20
    _old_av20 = _ocr20.available
    try:
        _ocr20.available = lambda: True
        ms20 = _bld_lp({"id": "M", "type": "multi_scan", "label": "",
                        "source": "text", "text": "私聊", "mode": "tap",
                        "stop_on": [{"name": "封号"}]})
        check("编译·遍历 stop_on 透传",
              isinstance(ms20, MultiScanStep) and ms20.stop_on == ["封号"])
    finally:
        _ocr20.available = _old_av20

    print("FAILPORT SMOKE PASS" if ok else "FAILPORT SMOKE FAIL")
    raise SystemExit(0 if ok else 1)
