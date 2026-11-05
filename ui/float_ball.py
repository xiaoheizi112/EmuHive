"""
ui/float_ball.py — 桌面悬浮球：最小化窗口的统一召回入口

痛点：主界面/工作台/运行日志/状态信息四类顶层窗口最小化后，只能去任务栏
一堆 python.exe 缩略图里摸。球只在「至少一个被跟踪窗口处于最小化」时出现，
全部恢复后自动隐藏，不常驻挡桌面。

交互：
- 左键单击 = 召回最近被最小化的那个窗口
- 右键 = 菜单逐窗召回（当前最小化的带「（已最小化）」后缀），末尾「本次运行隐藏悬浮球」
- 左键拖拽 = 换位，松手把位置写回配置 ui.float_ball（x/y=-1 表示首次自动摆屏幕右侧）

挂载：MainWindow 建球后逐个 attach（主窗/日志窗创建即挂，工作台/状态窗在
open_* 里创建后挂）。attach/detach 注册表，不扫 topLevelWidgets。

坑位备忘（实测）：offscreen 平台下 show() 不解除 WindowMinimized 位，
召回必须显式 setWindowState(去掉最小化位)；真 Windows 上这行同样正确。
"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QMenu, QWidget
from shiboken6 import isValid

from core.config_manager import get_config

BALL_SIZE = 44  # 球直径（px）


def build_menu_rows(entries):
    """纯函数：菜单行 = [(窗口标题, 是否最小化)] → [(显示文字, 动作负载)]。
    负载 ("recall", 序号) 或 ("hide_session", None)。
    拆出来是因为 offscreen 下 QMenu.exec 会永久阻塞冒烟——exec 只留在事件处理里。"""
    rows = [(f"{title}（已最小化）" if minimized else title, ("recall", i))
            for i, (title, minimized) in enumerate(entries)]
    rows.append(("本次运行隐藏悬浮球", ("hide_session", None)))
    return rows


def recall_window(window: QWidget, focus: bool = False):
    """召回窗口的统一入口（悬浮球与主窗口各按钮共用）：
    先显式去掉最小化位——Qt 的 show() 不自动解除 WindowMinimized（离屏实测，
    真机上带主人的从属窗同样不稳，不赌），再 show+raise；
    用户明点按钮的场合传 focus=True 顺带 activateWindow 拿键盘焦点。"""
    window.setWindowState(window.windowState() & ~Qt.WindowMinimized)
    window.show()
    window.raise_()
    if focus:
        window.activateWindow()


class FloatBall(QWidget):
    """无边框半透明置顶小球。parent 必须为 None：Qt.Tool 子窗在 Windows 上
    会随主窗最小化一起藏，正与「召回最小化的主窗」相反。构造时不 show，
    避免出现 CLAUDE.md 警告的无父孤儿窗闪一帧（只在该出现时才 show）。"""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setFixedSize(BALL_SIZE, BALL_SIZE)
        self.setWindowTitle("悬浮召回球")  # 仅供 tests/watch_python_windows.py 取证辨认
        self.setToolTip("左键：召回最近最小化的窗口\n右键：选择召回哪个\n拖拽：移动位置")
        self._tracked = []      # 注册表（attach 顺序），菜单行序=此序
        self._min_order = []    # 当前最小化的窗口，最近被最小化的在最前
        self._hidden_session = False  # 「本次运行隐藏」点过 = True
        self._drag_off = None
        self._press_gp = None
        self._moved = False
        self._place()

    # ── 注册表 ──
    def attach(self, window: QWidget):
        """挂一个顶层窗口。幂等：重复挂同一窗口无事发生。"""
        if window in self._tracked:
            return
        window.installEventFilter(self)
        # 窗口销毁时自动摘。注意 destroyed 信号在 ~QObject 期间发出，此刻别再调
        # removeEventFilter 碰半死的 C++ 对象，只从名单里除名（见 _forget）
        window.destroyed.connect(lambda: self._forget(window))
        self._tracked.append(window)

    def detach(self, window: QWidget):
        """活着时主动摘挂（当前无人调用，留作注册表的另一半 API）。"""
        if window in self._tracked:
            window.removeEventFilter(self)
            self._forget(window)

    def _forget(self, window: QWidget):
        # 关程序时销毁顺序不定：球本体的 C++ 对象可能先没了，destroyed 信号
        # 再回调进来就是野引用（实测冒烟退出时炸 RuntimeError）——球都没了，
        # 除名无意义，直接返回
        if not isValid(self):
            return
        if window in self._tracked:
            self._tracked.remove(window)
            if window in self._min_order:
                self._min_order.remove(window)
            self._refresh_visibility()

    # ── 最小化监听：与被还原置顶过滤器（raise_on_restore）同一事件口径 ──
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.WindowStateChange and obj in self._tracked:
            minimized = bool(obj.windowState() & Qt.WindowMinimized)
            if minimized and obj not in self._min_order:
                self._min_order.insert(0, obj)
            elif not minimized and obj in self._min_order:
                self._min_order.remove(obj)
            self._refresh_visibility()
        return False  # 只旁听不拦截：Qt 自己还要处理状态事件

    # ── 出现/隐藏 ──
    def _enabled(self) -> bool:
        return bool(get_config().get("ui.float_ball.enabled", True))

    def _refresh_visibility(self):
        should_show = self._enabled() and not self._hidden_session and bool(self._min_order)
        if should_show and not self.isVisible():
            self.show()
        elif not should_show and self.isVisible():
            self.hide()

    # ── 召回 ──
    def recall(self, window: QWidget):
        """召回一个窗口（点球/菜单）：统一走 recall_window，不抢键盘焦点。"""
        recall_window(window)

    def recall_last(self):
        if self._min_order:
            self.recall(self._min_order[0])

    # ── 右键菜单（构建走纯函数 build_menu_rows，exec 只在事件处理里） ──
    def _show_menu(self):
        entries = [(w.windowTitle() or "（无标题窗口）", w in self._min_order)
                   for w in self._tracked]
        menu = QMenu(self)
        for text, payload in build_menu_rows(entries):
            menu.addAction(text, lambda p=payload: self._on_menu_action(p))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _on_menu_action(self, payload):
        kind, idx = payload
        if kind == "recall":
            self.recall(self._tracked[idx])
        else:  # hide_session
            self._hidden_session = True
            self.hide()

    # ── 鼠标：拖拽换位 / 单击召回 / 右键菜单 ──
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_off = e.globalPosition().toPoint() - self.pos()
            self._press_gp = e.globalPosition().toPoint()
            self._moved = False
        elif e.button() == Qt.RightButton:
            self._show_menu()

    def mouseMoveEvent(self, e):
        if self._drag_off is not None and (e.buttons() & Qt.LeftButton):
            gp = e.globalPosition().toPoint()
            if (gp - self._press_gp).manhattanLength() > 4:
                self._moved = True
            if self._moved:
                self.move(gp - self._drag_off)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            if not self._moved:
                self.recall_last()
            else:
                self._save_pos()
        self._drag_off = None

    # ── 位置配置 ──
    def _place(self):
        cfg = get_config().get("ui.float_ball", {}) or {}
        x, y = cfg.get("x", -1), cfg.get("y", -1)
        if x is None or y is None or x < 0 or y < 0:
            scr = QApplication.primaryScreen().availableGeometry()
            x, y = scr.right() - BALL_SIZE - 8, scr.center().y() - BALL_SIZE // 2
        self.move(int(x), int(y))

    def _save_pos(self):
        cfg = get_config()
        old = cfg.get("ui.float_ball", {}) or {}
        cfg.set("ui.float_ball", {**old, "x": self.x(), "y": self.y()})

    # ── 外观：半透明圆 + 「召回」白字（现画，零图片资源） ──
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 140), 2))
        p.setBrush(QColor(40, 90, 170, 175))
        p.drawEllipse(self.rect().adjusted(2, 2, -2, -2))
        p.setPen(QColor(255, 255, 255))
        f = QFont()
        f.setPixelSize(13)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, "召回")
