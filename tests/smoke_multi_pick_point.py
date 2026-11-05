# 冒烟：多点取色「在预览上点选记点」链路（2026-09 改版：悬停+按钮 → 预览单击）
# 守的链路：_cond_multi 面板按钮 → _begin_multi_pick 进 multi_point 态 + 标记同步
# → 外壳直连 route_multi_point 现场采色（含原生帧 _magn 换算）追加进 cond.points
# → 按宿主节点重建表单；条件类型被换掉时会话作废不记脏点。
# 全程 FlowPanel.__new__ 借方法（smoke_cond_text_roi 同款），不建真 UI、不碰配置。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_multi_pick_point.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
from PySide6.QtWidgets import QApplication, QPushButton

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowPanel

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


class FakeView:
    def __init__(self, img, magn=1.0):
        self.draw_mode = "template"
        self.default_mode = "template"
        self.multi_marks = []
        self._img = img
        self._magn = magn

    def update(self):
        pass


img = np.zeros((960, 540, 3), np.uint8)
img[100, 50] = (0, 255, 0)          # BGR 纯绿 → #00FF00
img[200, 60] = (255, 0, 0)          # BGR 纯蓝 → #0000FF

node = {"id": "n1", "type": "watch", "cond": {"kind": "multi_color", "points": []}}
f = FlowPanel.__new__(FlowPanel)
f._mark_dirty = lambda: None
f._set_status = lambda t: None
f._rebuild_calls = []
f._rebuild_props = lambda n: f._rebuild_calls.append(n)
f._view = FakeView(img)
f._multi_pick_cond = None
f._multi_pick_node = None

cond = node["cond"]
w = f._cond_multi(node, cond)
btn = next(b for b in w.findChildren(QPushButton) if b.text() == "在预览上点选记点")
check("面板有「在预览上点选记点」按钮（旧悬停按钮已删）",
      btn is not None and not any(
          b.text() == "记录当前取色点" for b in w.findChildren(QPushButton)))

# ① 发起点选：进态 + 已有标记同步到预览
cond["points"] = [[1, 2, "AABBCC"]]
btn.click()
check("点按钮 → 预览进 multi_point 态", f._view.draw_mode == "multi_point")
check("归属绑本条件+宿主节点", f._multi_pick_cond is cond and f._multi_pick_node is node)
check("已有 1 点同步成预览标记", f._view.multi_marks == [(1, 2)])

# ② 单击记点：现场采色、追加、重建宿主表单、标记刷新
f.route_multi_point(50, 100)
check("记点 (50,100) 采到 #00FF00",
      cond["points"][-1] == [50, 100, "00FF00"])
f.route_multi_point(60, 200)
check("连点第二下不退态、采到 #0000FF",
      cond["points"][-1] == [60, 200, "0000FF"]
      and f._view.draw_mode == "multi_point")
check("每次记点按宿主节点重建表单", f._rebuild_calls == [node, node])
check("预览标记跟着涨", f._view.multi_marks == [(1, 2), (50, 100), (60, 200)])

# ③ 原生帧：基准坐标 ×_magn 换算到物理像素采样
f._view = FakeView(np.zeros((1920, 1080, 3), np.uint8), magn=2.0)
f._view._img[160, 80] = (0, 0, 255)   # 物理 (80,160) = 基准 (40,80) → #FF0000
f.route_multi_point(40, 80)
check("原生帧按 _magn 换算采样（基准(40,80)→物理(80,160)）",
      cond["points"][-1] == [40, 80, "FF0000"])

# ④ 条件类型中途被换：会话作废，点不记进别的条件
cond["kind"] = "template"
n_before = len(cond["points"])
f.route_multi_point(50, 100)
check("kind 变了 → 不再追加", len(cond["points"]) == n_before)
check("kind 变了 → 预览退出点选态", f._view.draw_mode == "template")
check("kind 变了 → 归属作废", f._multi_pick_cond is None)

# ⑤ 归属已空时外壳再送点：静默忽略，不炸
f.route_multi_point(1, 1)
check("无会话时收到点 → 静默忽略", len(cond["points"]) == n_before)

# ⑥ 删除点走 _sync_multi_marks：列表与预览标记一致
f._multi_pick_cond = cond
f._multi_pick_node = node
cond["points"] = [[9, 9, "112233"]]
f._sync_multi_marks()
check("_sync_multi_marks 按条件重建预览标记", f._view.multi_marks == [(9, 9)])
f._end_multi_pick()
check("_end_multi_pick 退态清标记",
      f._multi_pick_cond is None and f._view.multi_marks == []
      and f._view.draw_mode == "template")

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke multi-pick-point ALL OK")
