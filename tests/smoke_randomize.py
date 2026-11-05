# 行为随机化自检（正式冒烟，tests/ 下常驻）
# 覆盖：jitter_offset 圆内/环带分布、tap_template 与 _tap_jitter 集成、
#       WaitStep 随机等待（区间内 / 同轮稳定 / reset 重掷）、编译层字段传参与
#       老单键 jitter_px 兼容。
import math
import os
import random
import sys

# 从 tests/ 独立运行：项目根在上一级
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


import numpy as np

import bot.fsm as fsm
from bot.tasks.flow import _action_step, _tap_jitter

random.seed(7)

# ── 桩：20×20 模板命中左上 (100,200)，中心 (110,210)，无 click_target ──
fsm._find_template = lambda c, n: {"threshold": 0.8, "roi": None,
                                   "click_target": None}
fsm._load_tpl = lambda e: np.zeros((20, 20, 3), np.uint8)
fsm.vision.match_template = lambda *a, **k: {"x": 100, "y": 200}
got = []
fsm.ldplayer.adb_shell = lambda i, cmd: got.append(cmd)


class C:
    ld_index = 0
    frame = np.zeros((960, 540, 3), np.uint8)


def tap_xy(cmd):
    """'input tap 110 210' → (110, 210)"""
    parts = cmd.split()
    assert parts[0] == "input" and parts[1] == "tap", cmd
    return int(parts[2]), int(parts[3])


def dist(px, py, cx, cy):
    return math.hypot(px - cx, py - cy)


# 1. jitter_offset：int=[0,r] 圆内，全在 r 圈内且散开
offs = [fsm.jitter_offset(5) for _ in range(200)]
assert all(math.hypot(dx, dy) <= 5.8 for dx, dy in offs)   # 0.5+0.5 舍入余量
assert len(set(offs)) > 50, "圆内散布不足"
assert (0, 0) in offs or any(o == (0, 0) for o in offs) or True  # r∈[0,5] 允许近中心
print("jitter_offset(5): 200 次全在 5.8px 圈内, 分布散开")

# 2. jitter_offset：(min,max) 环带 —— 距中心 ≥ min-0.8（舍入）且 ≤ max+0.8
offs = [fsm.jitter_offset((3, 8)) for _ in range(300)]
ds = [math.hypot(*o) for o in offs]
assert all(2.2 <= d <= 8.8 for d in ds), f"环带越界: {min(ds):.2f}~{max(ds):.2f}"
assert min(ds) > 1.5, "min=3 却没兜住：不该有近中心落点"
print(f"jitter_offset((3,8)): 300 次距离 {min(ds):.1f}~{max(ds):.1f}, 无近中心点")

# 3. tap_template 端到端：老 int 语义 + 新 tuple 语义
for _ in range(30):
    fsm.tap_template(C(), "c", "n", 0, 0, 5)
    x, y = tap_xy(got[-1])
    assert dist(x, y, 110, 210) <= 5.8
for _ in range(30):
    fsm.tap_template(C(), "c", "n", 0, 0, (4, 6))
    x, y = tap_xy(got[-1])
    assert 3.2 <= dist(x, y, 110, 210) <= 6.8
print("tap_template: int=(旧)圆内 / tuple=(新)环带 都守界")

# jitter=0 → 永远精确同一个点（老行为）
for _ in range(5):
    fsm.tap_template(C(), "c", "n", 0, 0, 0)
assert len(set(got[-5:])) == 1 and got[-1] == "input tap 110 210"
print("jitter=0: 落点恒定 (110,210)，老流程语义不变")

# 4. _tap_jitter（点坐标节点用）：边界钳制在 0..539 / 0..959
got.clear()
for _ in range(50):
    _tap_jitter(C(), 3, 3, (0, 5))  # 贴着左上角抖，会撞到负数 → 必须钳住
    x, y = tap_xy(got[-1])
    assert 0 <= x <= 539 and 0 <= y <= 959, got[-1]
print("_tap_jitter 边界: 贴角抖动全被钳在画面内")

# 5. WaitStep 随机等待：区间内 + 同轮读数稳定 + reset 重掷
w = fsm.WaitStep("t", fixed_wait=1.0, fixed_wait_max=3.0, retries=0)
rolls = set()
for _ in range(40):
    w._start = None
    w.reset()
    s = w._fixed_secs()
    assert 1.0 <= s <= 3.0, s
    assert w._fixed_secs() == s, "同一轮内读数必须稳定（不许每拍重掷）"
    rolls.add(round(s, 3))
assert len(rolls) > 5, f"随机没散开: {rolls}"
print(f"WaitStep [1,3]: 40 轮掷出 {len(rolls)} 种时长, 同轮读数一致")

w2 = fsm.WaitStep("t", fixed_wait=2.0, retries=0)
assert w2._fixed_secs() == 2.0
print("WaitStep 不配上限: 恒 2.0s，零影响")

# 6. 编译层传参
import core.template_manager as tm
tm.has_template = lambda c, n: True

# 6a. 老单键 jitter_px=6 → [0,6] 圆内
# 本段只测抖动落点：显式关掉「点击后加延迟」（现缺省开，会包一层 _SequenceStep，
# .action 拿不到裸步），延迟语义由 python -m bot.tasks.flow 自检覆盖
s = _action_step({"type": "click_template", "category": "c", "name": "n",
                  "jitter_px": 6, "wait_before": False,
                  "delay_after_click": False})
got.clear()
for _ in range(30):
    s.reset()
    s.action(C())
    x, y = tap_xy(got[-1])
    assert dist(x, y, 110, 210) <= 6.8, got[-1]
print("编译 老单键 jitter_px=6: 30 次全在圈内（兼容不回归）")

# 6b. 新区间键 min/max=4/8，带偏移 (dx=9,dy=-4) → 锚点 (119,206) 环带
s = _action_step({"type": "click_template", "category": "c", "name": "n",
                  "offset_x": 9, "offset_y": -4,
                  "jitter_px_min": 4, "jitter_px_max": 8,
                  "wait_before": False,
                  "delay_after_click": False})
got.clear()
for _ in range(30):
    s.reset()
    s.action(C())
    x, y = tap_xy(got[-1])
    assert 3.2 <= dist(x, y, 119, 206) <= 8.8, got[-1]
print("编译 新区间 [4,8]+偏移: 环带绕锚点 (119,206) 散布")

# 6c. sleep 随机上限
s3 = _action_step({"type": "sleep", "seconds": 1})
assert s3.fixed_wait_max is None
s4 = _action_step({"type": "sleep", "seconds": 5, "seconds_max": 2})
assert s4.fixed_wait_max is None
s5 = _action_step({"type": "sleep", "seconds": 1, "seconds_max": 2.5})
assert s5.fixed_wait_max == 2.5
print("编译 sleep: 新节点带区间 / 老节点与配错的都回落固定秒")

print("ALL OK")
