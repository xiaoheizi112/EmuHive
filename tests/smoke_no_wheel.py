"""
tests/smoke_no_wheel.py — 「控件彻底不吃滚轮」全局过滤器常驻自检（离屏）

覆盖 ui/no_wheel_edit 的判据链：
- spin/combo/slider 本体收到滚轮 → 值/索引纹丝不动（一律吞掉，不转发）；
- 滚轮发给 spinbox 内部 lineEdit（真机鼠标压的就是它）→ 同款生效；
- 非目标控件（QLabel）滚轮直通，过滤器不插手；
- combo 展开的 popup（独立窗口、parent 挂着 combo）→ 豁免不拦，
  下拉里滚选项的正常功能保住；
- 吞掉的滚轮不会顺手滚外层页面（用户拍板的取舍：控件上是死区）。

跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_no_wheel.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (QApplication, QComboBox, QLabel, QScrollArea,
                               QSlider, QSpinBox, QVBoxLayout, QWidget)

app = QApplication.instance() or QApplication([])

import ui.no_wheel_edit as nwe
nwe.install(app)


def wheel(w, dy=-120):
    e = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, dy),
                    Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(w, e)
    app.processEvents()


def mk_scroll():
    sa = QScrollArea()
    inner = QWidget()
    v = QVBoxLayout(inner)
    sp = QSpinBox()
    sp.setRange(0, 9999)
    sp.setValue(50)
    v.addWidget(sp)
    for _ in range(80):
        v.addWidget(QLabel("垫高"))
    sa.setWidget(inner)
    sa.resize(200, 120)
    sa.show()
    app.processEvents()
    return sa, sp


# 1) spinbox 本体：值不动（吞掉）
sa, sp = mk_scroll()
wheel(sp)
assert sp.value() == 50, sp.value()
print("spinbox 本体：滚轮吞掉、值不变 OK")

# 2) spinbox 内部 lineEdit（真机投递目标）：同款生效
sa, sp = mk_scroll()
wheel(sp.lineEdit())
assert sp.value() == 50, sp.value()
print("内部 lineEdit：滚轮吞掉、值不变 OK")

# 3) combo / slider：索引、值都不动
cb = QComboBox()
cb.addItems(["甲", "乙", "丙"])
cb.setCurrentIndex(1)
sl = QSlider(Qt.Horizontal)
sl.setRange(0, 100)
sl.setValue(50)
wheel(cb)
wheel(sl)
assert cb.currentIndex() == 1 and sl.value() == 50, (cb.currentIndex(), sl.value())
print("combo/slider：不动值不切项 OK")

# 4) 非目标控件直通：QLabel 收到滚轮时过滤器不插手（返回 False）
lbl = QLabel("x")
assert nwe._blocked_ancestor(lbl) is None
print("非目标控件豁免 OK")

# 5) popup 豁免：独立窗口 + parent 挂着 combo（真 combo popup 同款拓扑）
popup = QWidget(cb)          # 有 parent
popup.setWindowFlags(Qt.Popup)  # 成为独立窗口（isWindow True）
assert nwe._blocked_ancestor(popup) is None, "combo popup 被误拦：下拉里滚选项会死"
print("combo popup 豁免（下拉里滚选项不受影响）OK")

# 6) 死区取舍：控件上滚 = 外层也不滚（吞掉不转发，用户拍板）
sa, sp = mk_scroll()
before = sa.verticalScrollBar().value()
wheel(sp)
assert sa.verticalScrollBar().value() == before
print("滚轮压在控件上：值不动、页面不滚（死区，符合拍板）OK")

# 7) 回归：滚轮事件对象是底层 QWindow（无 isWindow()）时不许炸
#    （实测「全部启动」弹模拟器窗口瞬间触发过 AttributeError）
from PySide6.QtGui import QWindow
assert nwe._blocked_ancestor(QWindow()) is None, "QWindow 应直通不拦"
print("QWindow 事件对象不炸 OK")

print("smoke_no_wheel: ALL OK")
