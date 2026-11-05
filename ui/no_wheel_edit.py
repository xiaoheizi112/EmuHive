"""
ui/no_wheel_edit.py — 数值/选择/滑块控件彻底不吃滚轮（应用级事件过滤器）

Qt 的 QAbstractSpinBox（QSpinBox/QDoubleSpinBox）/ QComboBox / QSlider 默认
"滚轮悬停即改值/改选项"，浏览长页面时鼠标扫过就误改（本项目 80+ 个这类
控件全中招）。这里装一个应用级事件过滤器：滚轮命中这几类控件一律吞掉 ——
滚轮永远碰不到数值，改值只走打字 / ±按钮 / 点开下拉 / 拖滑块。
下拉展开后在列表里滚选项目样能用：那是 popup 里的 QListView，不在拦截范围。

代价（用户拍板接受的）：光标压在控件上时滚轮是死区，页面不滚，挪开再滚。
刻意保留的滚轮功能（画布缩放、放大镜倍率）不在这三类里，不受影响。
main.py install 一次覆盖所有窗口，同 raise_on_restore 套路。
"""

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox,
                               QSlider, QWidget)

_BLOCKED = (QAbstractSpinBox, QComboBox, QSlider)


def _blocked_ancestor(w):
    """从滚轮命中的控件沿 parent 链找"该不该拦"：真窗口里 spinbox 收到的
    滚轮，事件对象往往是它内部的 QLineEdit（铺满显示区），不是 spinbox
    本身 —— 只判 isinstance(obj) 会漏（实测踩坑）。遇到窗口边界即停：
    combo 展开的 popup 列表 parent 也挂着 combo，但它是独立窗口，里面的
    滚轮是"下拉里滚选项"的正常功能，不许拦。"""
    # 非 QWidget（如滚轮直接落在底层 QWindow 上，实测「全部启动」弹模拟器窗口时触发）
    # 一律不拦：QWindow 没有 isWindow()，裸爬 parent 链会 AttributeError
    while isinstance(w, QWidget):
        if isinstance(w, _BLOCKED):
            return w
        if w.isWindow():
            return None
        w = w.parent()
    return None


class _NoWheelEdit(QObject):
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel and _blocked_ancestor(obj) is not None:
            return True   # 吞掉：不转发（滚轮在控件上是死区，用户拍板）
        return False


def install(app: QApplication):
    """装"控件不吃滚轮"过滤器。filter 必须以 app 为 parent 挂住引用——
    Python 侧一旦被 GC，C++ 那边就是个野指针（同 raise_on_restore）。"""
    app.installEventFilter(_NoWheelEdit(app))
