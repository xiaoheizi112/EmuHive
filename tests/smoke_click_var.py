# 冒烟：F3「点坐标」的坐标来自流程变量/表达式——回归 2026-10-11 新增
# 覆盖编译链与运行期求值：
# ① coord 表达式 = 元组变量 (x,y)（「识字取物·读坐标」存的那种）→ 落点正确；
# ② x_expr/y_expr 分轴（算术 / 变量名）→ 落点正确，且优先于 coord；
# ③ coord 变量 = 字符串 "123, 456" → 按逗号拆出两个数；
# ④ 求值失败（变量没读到 / 只填一个轴）→ 不 tap、不崩、走 WARN 跳过；
# ⑤ 存量节点（只有 tap_x/tap_y，无新键）→ 老行为逐字不变；
# ⑥ 越界钳位（负 / 超屏）→ 钳回边界再点 + WARN。
# 打桩不碰模拟器：fsm.ldplayer.adb_shell 收集落点命令串（同 smoke_click_offset 做法）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_click_var.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

from core import ldplayer
from bot.tasks.flow import (CompiledClickStep, _action_step, _resolve_click_xy,
                            flow_var_set)

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


class FakeCtx:
    ld_index = 0
    frame = None

    def __init__(self):
        self.extra = {}
        self.logs = []

    def log(self, msg, level="INFO"):
        self.logs.append((level, str(msg)))


taps = []
_orig_shell = ldplayer.adb_shell
ldplayer.adb_shell = lambda idx, cmd: taps.append(cmd)


def run_click(node, ctx):
    """编译一个 click 节点并 tick 到落手，返回 (结论, 收集到的 tap 命令)。"""
    taps.clear()
    step = _action_step(node)
    r = step.tick(ctx)
    return r, (list(taps), step)


try:
    # ── ① coord = 元组变量 ──
    c = FakeCtx()
    flow_var_set(c, "放技能", (100, 200))
    r, (got, st) = run_click({"type": "click", "coord": "放技能"}, c)
    check("coord 元组变量 (100,200) → tap 落点正确",
          r == "ok" and got == ["input tap 100 200"])
    check("带坐标变量的节点编成 CompiledClickStep", isinstance(st, CompiledClickStep))

    # coord 元组变量可带花括号写法
    c = FakeCtx()
    flow_var_set(c, "放技能", (7, 8))
    r, (got, _st) = run_click({"type": "click", "coord": "{放技能}"}, c)
    check("coord 花括号写法 {放技能} → (7,8)",
          r == "ok" and got == ["input tap 7 8"])

    # ── ② x_expr / y_expr 分轴 ──
    c = FakeCtx()
    flow_var_set(c, "Y坐标", 300)
    r, (got, _st) = run_click(
        {"type": "click", "x_expr": "100+5", "y_expr": "Y坐标"}, c)
    check("x_expr=100+5 / y_expr=变量 → tap 105 300",
          r == "ok" and got == ["input tap 105 300"])

    # x/y 表达式优先于 coord：coord 值是坏值也不该被用
    c = FakeCtx()
    flow_var_set(c, "X坐标", 11)
    flow_var_set(c, "Y坐标", 22)
    flow_var_set(c, "坏坐标", "oops")
    r, (got, _st) = run_click(
        {"type": "click", "coord": "{坏坐标}",
         "x_expr": "X坐标", "y_expr": "Y坐标"}, c)
    check("x_expr/y_expr 存在时优先于 coord（不碰坏 coord）",
          r == "ok" and got == ["input tap 11 22"])

    # ── ③ coord = "x, y" 字符串 ──
    c = FakeCtx()
    flow_var_set(c, "坐标串", "123, 456")
    r, (got, _st) = run_click({"type": "click", "coord": "坐标串"}, c)
    check("coord 字符串 \"123, 456\" → 拆出 (123,456)",
          r == "ok" and got == ["input tap 123 456"])

    # ── ④ 求值失败：不 tap、不崩、WARN ──
    c = FakeCtx()
    r, (got, _st) = run_click({"type": "click", "coord": "没这个变量"}, c)
    check("coord 变量没读到 → 不 tap、返回 ok（跳过）",
          r == "ok" and got == [])
    check("coord 失败打了 WARN 点名",
          any(lv == "WARN" for lv, _ in c.logs))

    c = FakeCtx()
    flow_var_set(c, "Y坐标", 5)
    r, (got, _st) = run_click(
        {"type": "click", "x_expr": "X坐标", "y_expr": "Y坐标"}, c)
    check("x_expr 有值但 y_expr 变量没读到 → 不 tap（成对失败整步跳过）",
          r == "ok" and got == [])

    c = FakeCtx()
    r, (got, _st) = run_click({"type": "click", "x_expr": "100"}, c)
    check("只填 x_expr 没填 y_expr → 不 tap（要成对）",
          r == "ok" and got == [])

    c = FakeCtx()
    flow_var_set(c, "坐标串", "只一个字")
    r, (got, _st) = run_click({"type": "click", "coord": "坐标串"}, c)
    check("coord 字符串拆不出两个数 → 不 tap",
          r == "ok" and got == [])

    # 表达式非法（除零）→ 不 tap
    c = FakeCtx()
    r, (got, _st) = run_click({"type": "click", "coord": "1/0"}, c)
    check("coord 表达式非法（1/0）→ 不 tap",
          r == "ok" and got == [])

    # ── ⑤ 存量节点：只有 tap_x/tap_y，老行为逐字不变 ──
    c = FakeCtx()
    r, (got, st) = run_click({"type": "click", "tap_x": 50, "tap_y": 60}, c)
    check("存量节点（无新键）→ 老 ActionStep、落点 (50,60) 不变",
          r == "ok" and got == ["input tap 50 60"]
          and not isinstance(st, CompiledClickStep))
    check("存量节点无 WARN（静默按老行为）", c.logs == [])

    # new_node("click") 的空新键也走老路（留空 = 用固定坐标）
    from ui.flow_editor import new_node   # 仅取默认字典，不建 Qt 控件
    nd = new_node("click")
    c = FakeCtx()
    r, (got, st) = run_click(nd, c)
    vt = got[0].split()
    vx, vy = int(vt[2]), int(vt[3])
    check("new_node 默认空键 → 仍走老 ActionStep（270,480 附近，抖动≤5）",
          not isinstance(st, CompiledClickStep)
          and (vx - 270) ** 2 + (vy - 480) ** 2 <= 25 + 4)

    # ── ⑥ 越界钳位 ──
    c = FakeCtx()
    flow_var_set(c, "越界", (-50, 2000))
    r, (got, _st) = run_click({"type": "click", "coord": "越界"}, c)
    check("越界坐标 (-50,2000) → 钳到 (0,959)", got == ["input tap 0 959"])
    check("越界钳位打了 WARN", any(lv == "WARN" for lv, _ in c.logs))

    # 抖动仍生效（赋 ±r 环带，落点落在钳位后的邻域内）
    c = FakeCtx()
    flow_var_set(c, "点", (300, 400))
    r, (got, _st) = run_click(
        {"type": "click", "coord": "点", "jitter_px_min": 3, "jitter_px_max": 3}, c)
    vt = got[0].split()
    vx, vy = int(vt[2]), int(vt[3])
    check(f"coord 变量 + 抖动：落点在 (300,400) 半径 3 内（实 {vx},{vy}）",
          (vx - 300) ** 2 + (vy - 400) ** 2 <= 9 + 4)

    # ── _resolve_click_xy 单元级：三键全空回落 tap_x/tap_y ──
    c = FakeCtx()
    x, y, err = _resolve_click_xy({"type": "click", "tap_x": 9, "tap_y": 8}, c)
    check("_resolve_click_xy 无新键 → (9,8) err None",
          (x, y, err) == (9, 8, None))
finally:
    ldplayer.adb_shell = _orig_shell

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke click-var ALL OK")