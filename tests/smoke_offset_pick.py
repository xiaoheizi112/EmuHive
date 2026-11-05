# 点模板「点选目标位算偏移」链路自检（正式冒烟，tests/ 下常驻）
# 覆盖：预匹配进点选态 + 绿框标注、点选取偏移、与运行时 tap_template 往返一致、
#       防御分支（没选模板 / 画面匹配不到不进点选态）。
# 识字取物「点文字」同款（6a-6c）：锚点走 OCR 命中框中心（同 roi/min_sim 口径
# 传参核对）、点选写回 offset_x/offset_y、运行时 ServerPickStep 吃偏移落回同点、
# 防御（没填文字 / 占位符 / OCR 没装不进点选态）。
import os
import sys

# 从 tests/ 独立运行：项目根在上一级
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


import numpy as np

import core.template_manager as tm
import core.vision as vs
from ui.flow_editor import FlowPanel


class FakeView:
    _img = np.zeros((960, 540, 3), np.uint8)
    draw_mode = "none"
    click_owner = None
    roi_owner = None
    hit_rects = []
    click_target_pos = None

    def update(self):
        pass


f = FlowPanel.__new__(FlowPanel)  # 不跑 __init__，只借用其方法
f._view = FakeView()
f.status = []
f._pick_ctx = None
f._roi_pick_node = None
f._set_status = lambda t: f.status.append(t)
f._mark_dirty = lambda: None
rebuilt = []
f._rebuild_props = lambda n: rebuilt.append(n)
f._end_pick = lambda: (setattr(f, "_pick_ctx", None),
                       setattr(f._view, "click_owner", None))

# 桩：20×20 模板，命中左上 (100,200) → 中心 (110,210)
tm.find_template = lambda c, n: {"threshold": 0.8, "roi": None}
tm.load_template_image = lambda e: np.zeros((20, 20, 3), np.uint8)
vs.match_template = lambda *a, **k: {"x": 100, "y": 200}

# 1. 没选模板 → 不进点选态、不画框
FlowPanel._begin_pick(f, {"category": "", "name": ""}, "offset")
assert f._pick_ctx is None
assert f._view.draw_mode == "none" and f._view.hit_rects == []
print("guard(no template):", f.status[-1])

# 2. 预匹配成功 → 进点选态 + 绿框 + 锚点入上下文
node = {"category": "c", "name": "n"}
FlowPanel._begin_pick(f, node, "offset")
ctx = f._pick_ctx
assert ctx["field"] == "offset"
assert ctx["anchor"] == (110, 210, 20, 20), ctx["anchor"]
assert f._view.hit_rects == [(100, 200, 20, 20)], f._view.hit_rects
assert f._view.draw_mode == "click_target" and f._view.click_owner == "pick"
print("pre-match: anchor", ctx["anchor"], "green", f._view.hit_rects)

# 3. 点目标位 (140,200) → 写回偏移；叠加（绿框/红十字）写完即清
f._view.click_target_pos = (140, 200)  # ImageView 单击自己会记这个
FlowPanel._on_pick_click(f, 140, 200)
assert node["offset_x"] == 30 and node["offset_y"] == -10, node
assert f._view.hit_rects == [] and f._view.click_target_pos is None, \
    "叠加应随写回清掉"
assert f._pick_ctx is None and f._view.click_owner is None
print("pick->node:", node, "|", f.status[-1])

# 4. 往返一致：运行时 tap_template 吃这偏移必须落回 (140,200)
import bot.fsm as fsm

got = {}
fsm.ldplayer.adb_shell = lambda i, c: got.setdefault("c", c)
fsm._find_template = lambda c, n: {"threshold": 0.8, "roi": None,
                                   "click_target": None}
fsm._load_tpl = lambda e: np.zeros((20, 20, 3), np.uint8)
fsm.vision.match_template = lambda *a, **k: {"x": 100, "y": 200}


class C:
    ld_index = 0
    frame = f._view._img


fsm.tap_template(C(), "c", "n", node["offset_x"], node["offset_y"])
assert got["c"] == "input tap 140 200", got
print("round-trip:", got["c"])

# 5. 画面匹配不到 → 不进点选态、无残留
f._view.hit_rects = []
vs.match_template = lambda *a, **k: None
FlowPanel._begin_pick(f, {"category": "c", "name": "n"}, "offset")
assert f._pick_ctx is None
assert f._view.hit_rects == []
print("guard(no match):", f.status[-1])

# 6a. 识字取物「点文字」点选算偏移：锚点走 OCR 命中框（与运行时同口径）
import core.ocr as oc

oc.available = lambda: True
seen_kw = {}


def _fake_find(img, needle, roi=None, min_sim=0.6, **k):
    seen_kw.update(needle=needle, roi=roi, min_sim=min_sim)
    return {"x": 270, "y": 500, "x0": 200, "y0": 480, "x1": 340, "y1": 520,
            "text": "免费香肠", "sim": 1.0, "ms": 1.0}


oc.find_text = _fake_find
sp_node = {"type": "server_pick", "mode": "click", "text": "免费香肠",
           "roi": [100, 300, 450, 900], "min_sim": 60}
FlowPanel._begin_pick(f, sp_node, "offset")
ctx = f._pick_ctx
assert ctx is not None and ctx["anchor"] == (270, 500, 140, 40), ctx
assert f._view.hit_rects == [(200, 480, 140, 40)], f._view.hit_rects
assert "文字命中" in f.status[-1], f.status[-1]
# 同口径核对：roi/min_sim/文字都按节点现值传进 find_text（min_sim 换算是 60→0.6）
assert seen_kw == {"needle": "免费香肠", "roi": (100, 300, 450, 900),
                   "min_sim": 0.6}, seen_kw
f._view.click_target_pos = (300, 610)
FlowPanel._on_pick_click(f, 300, 610)
assert sp_node["offset_x"] == 30 and sp_node["offset_y"] == 110, sp_node
assert f._view.hit_rects == [] and f._pick_ctx is None, "叠加与上下文应清"
print("ocr-pick->node:", sp_node, "|", f.status[-1])

# 6b. 运行时往返：ServerPickStep 吃这偏移必须落回 (300,610)
import bot.tasks.flow as tf

got2 = {}
tf._tap = lambda c, x, y: got2.setdefault("c", (x, y))


class C2:
    ld_index = 0
    log = None


step = tf.ServerPickStep("买", "免费香肠", mode="click", offset=(30, 110))
step._use_hit(C2(), {"x": 270, "y": 500, "text": "免费香肠",
                     "sim": 1.0, "ms": 1.0}, "免费香肠")
assert got2["c"] == (300, 610), got2
print("ocr round-trip:", got2["c"])

# 6c. 防御：没填文字 / 占位符 / OCR 没装 → 都不进点选态
f._view.hit_rects = []
for bad, why, unavail in (({"type": "server_pick", "text": ""}, "没填文字", False),
                          ({"type": "server_pick", "text": "{账号.区服}"},
                           "占位符", False),
                          ({"type": "server_pick", "text": "新手1区"},
                           "OCR没装", True)):
    if unavail:
        oc.available = lambda: False
    FlowPanel._begin_pick(f, bad, "offset")
    assert f._pick_ctx is None, f"{why} 竟进了点选态"
    print(f"  guard({why}):", f.status[-1])
    if unavail:
        oc.available = lambda: True

# 6. 预览框选识别区域（server_pick ROI）：发起设归属，成框回填 node 并重建表单
roi_node = {}
FlowPanel.begin_roi_pick(f, roi_node)
assert f._roi_pick_node is roi_node
assert f._view.draw_mode == "roi" and f._view.roi_owner == "flow"
print("roi-pick begin:", f.status[-1])
FlowPanel.route_roi(f, 40, 50, 120, 30)          # 框 (40,50)-(160,80)
assert roi_node["roi"] == [40, 50, 160, 80], roi_node
assert rebuilt and rebuilt[-1] is roi_node, "成框应重建表单回填"
assert f._roi_pick_node is None, "回填后上下文作废"
print("roi->node:", roi_node, "|", f.status[-1])

print("ALL OK")
