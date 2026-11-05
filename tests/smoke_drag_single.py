# 画布拖拽/框选自检（正式冒烟，tests/ 下常驻）
# 守住的三个 bug：
#  1. 按下【没被选中】的节点起拖时，上次框选/Ctrl+A 残留的多选跟着一起动
#     （"拖一个其它跟着变"）→ view 按下未选中节点才清别人的选中；按中的节点
#     本身已在选中集则保留多选、交 Qt 整组拖（框选后平移全部 = 标准手势）。
#  2. 框选拖动时右上角闪一堆小窗 → _clear_props 拆属性表单控件用 hide()，
#     绝不 setParent(None)（一断 parent 就变顶层孤儿窗口，deleteLater 前被弹出）。
#  3. 框选每动一下鼠标都重建整张属性表单 = 卡顿 → _on_selection_changed 只
#     重启防抖 timer，停手才刷一次。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_drag_single.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFormLayout, QVBoxLayout, QWidget

from ui.flow_editor import (FlowCanvasView, FlowNodeItem, FlowPanel, FlowScene,
                            new_node)

app = QApplication.instance() or QApplication(sys.argv)


def mk(x, y, pid):
    n = new_node("click")
    n["id"], n["x"], n["y"] = pid, x, y
    return n


def build():
    scene = FlowScene(snap_grid=False)
    nodes = [mk(100, 100, "A"), mk(400, 100, "B"), mk(700, 400, "C")]
    items = {}
    for n in nodes:
        it = FlowNodeItem(n, None)   # panel=None：不碰存盘/撤销栈
        it.setPos(n["x"], n["y"])
        scene.addItem(it)
        items[n["id"]] = it
    view = FlowCanvasView(scene)
    view._scene_ref = scene          # view 不持有 scene 所有权，防 GC
    view.resize(900, 600)
    view.show()
    app.processEvents()
    return view, items


def drag(view, items, key, dx, dy):
    """在节点 key 中心按下左键，拖 (dx,dy) 后松开（未缩放：视口=场景坐标）。"""
    it = items[key]
    p = view.mapFromScene(it.mapToScene(it.boundingRect().center()))
    c = QPoint(int(p.x()), int(p.y()))
    vp = view.viewport()
    QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, c)
    QTest.mouseMove(vp, c + QPoint(dx, dy))
    QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, c + QPoint(dx, dy))
    app.processEvents()


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


ok = True

# ── 用例 1：单选拖 A，B/C（pos + node dict）不许动 ──
view, items = build()
drag(view, items, "A", 60, 30)
check("拖 A 到位 (100,100)→(160,130)",
      (items["A"].pos().x(), items["A"].pos().y()) == (160, 130))
check("A 坐标落账", (items["A"].node["x"], items["A"].node["y"]) == (160, 130))
for k in ("B", "C"):
    check(f"拖 A 时 {k} 不动",
          (items[k].pos().x(), items[k].pos().y())
          == (items[k].node["x"], items[k].node["y"])
          and (items[k].pos().x(), items[k].pos().y())
          == {"B": (400, 100), "C": (700, 400)}[k])

# ── 用例 2：多选后拖【已选中】的节点 → 整组一起平移（框选后挪全部=标准手势）──
view, items = build()
for it in items.values():
    it.setSelected(True)
drag(view, items, "A", 60, 30)          # A 已在选中集里
check("拖已选中的 A：A 到位 (100,100)→(160,130)",
      (items["A"].pos().x(), items["A"].pos().y()) == (160, 130))
check("拖已选中的 A：B 整组跟随 (400,100)→(460,130)",
      (items["B"].pos().x(), items["B"].pos().y()) == (460, 130))
check("拖已选中的 A：C 整组跟随 (700,400)→(760,430)",
      (items["C"].pos().x(), items["C"].pos().y()) == (760, 430))
check("整组坐标都落账",
      all(items[k].node["x"] == int(items[k].pos().x()) and
          items[k].node["y"] == int(items[k].pos().y()) for k in items))

# ── 用例 2b：多选 A/B 后拖【未选中】的 C → 只动 C，残留多选被清、A/B 原地不动 ──
view, items = build()
items["A"].setSelected(True)
items["B"].setSelected(True)
drag(view, items, "C", 40, 40)          # C 不在选中集里
check("拖未选中的 C：C 到位 (700,400)→(740,440)",
      (items["C"].pos().x(), items["C"].pos().y()) == (740, 440))
check("拖未选中的 C：A 原地不动（残留多选没被带着走）",
      (items["A"].pos().x(), items["A"].pos().y()) == (100, 100)
      and (items["A"].node["x"], items["A"].node["y"]) == (100, 100))
check("拖未选中的 C：B 原地不动",
      (items["B"].pos().x(), items["B"].pos().y()) == (400, 100)
      and (items["B"].node["x"], items["B"].node["y"]) == (400, 100))
check("拖未选中的 C 后仅剩 C 选中",
      [i.node["id"] for i in view.scene().selectedItems()
       if isinstance(i, FlowNodeItem)] == ["C"])

# ── 用例 3：_clear_props 拆控件不许 setParent(None)（否则变顶层孤儿窗 → 闪窗）──
# 裸实例只借方法，不建整套 UI。props_layout 里塞一个真父级（props_panel）的
# 子控件，清完必须：parent 仍在（没被丢成顶层窗口）+ 自身 isHidden()。
panel = FlowPanel.__new__(FlowPanel)
props_panel = QWidget()
panel.props_layout = QVBoxLayout(props_panel)
child = QWidget(props_panel)          # 显式给父级，模拟表单控件挂在 props_panel 下
panel.props_layout.addWidget(child)
sub_row = QWidget(props_panel)
inner = QWidget(sub_row)              # 嵌套一层，验证递归也走 hide 不 reparent
QFormLayout().addRow("x", inner)      # 挂进一个布局再被 _take 递归到
panel._clear_props()
check("拆控件后 child 仍有父级（没变顶层孤儿窗）", child.parent() is not None)
check("拆控件后 child 已隐藏", child.isHidden())

# ── 用例 4：选中变化只重启防抖 timer，不同步重建表单 ──
# 打桩 _sel_timer 记录 start 次数、_apply_selection_props 记录被调次数，
# 连点 5 次选中变化 → start 5 次、重建 0 次（重建交 timer 到点后触发）。
calls = {"start": 0, "apply": 0}


class FakeTimer:
    def start(self, *a):
        calls["start"] += 1


p2 = FlowPanel.__new__(FlowPanel)
p2._sel_timer = FakeTimer()
p2._apply_selection_props = lambda: calls.__setitem__("apply", calls["apply"] + 1)
for _ in range(5):
    p2._on_selection_changed()
check("框选 5 次选中变化只重启 timer（不逐次重建）",
      calls["start"] == 5 and calls["apply"] == 0)

print("ALL OK" if ok else "HAS FAIL")
sys.exit(0 if ok else 1)
