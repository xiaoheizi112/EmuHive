"""
ui/raise_on_restore.py — 顶层窗口从任务栏还原时自动拉到最前

从任务栏点回一个最小化的窗口，系统只负责"显示"，不负责"置顶"——
刚弹出的别家窗口（比如运行日志）正好压在还原后的窗口头顶，还得再点一次。
这里装一个应用级事件过滤器：任何顶层窗口，状态从"最小化"变回正常，
就补一次 raise_ + activateWindow。

main.py 对 QApplication install 一次即可，覆盖现在和以后所有窗口
（主界面/工作台/运行日志/状态窗口……），不必每个窗口类各写一份 changeEvent。

eventFilter 恒返回 False（只旁听不拦截）：Qt 自己也要处理这些状态事件，
吞掉会留下半成品状态。这里只加动作，不改流程。
"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget


class _RaiseOnRestore(QObject):
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowStateChange and \
                isinstance(obj, QWidget) and obj.isWindow() and \
                event.oldState() & Qt.WindowMinimized and \
                not obj.windowState() & Qt.WindowMinimized:
            # 只认"出最小化"方向：进最小化也发同一个事件，那时 raise 反而添乱
            obj.raise_()
            obj.activateWindow()
        return False


def install(app: QApplication):
    """装"还原置顶"过滤器。filter 必须以 app 为 parent 挂住引用——
    Python 侧一旦被 GC，C++ 那边就是个野指针。"""
    app.installEventFilter(_RaiseOnRestore(app))
