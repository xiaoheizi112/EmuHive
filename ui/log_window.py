"""
ui/log_window.py — 独立日志窗口

把原来嵌在主窗口底部的日志输出栏搬出来，做成可拖动、可缩放的独立窗口：
主窗口底部腾出整行给 清单 + 设置页，日志想看再拉过来放旁边，不挤主界面。

关键约定：本类自带 .log(message, level) 方法，与旧 LogPanel 同名同签名——
MainWindow 上 self.log_panel 的 20+ 处调用点因此一行都不用改，
只是显示位置从嵌入式面板换成了独立窗口。

窗口行为：
- 非模态顶层窗口（Qt.Window 标志，照 workbench 样板）
- 位置大小存 ui.log_window_geometry，下次启动还原
- 上限 2000 行的滚动缓冲在内部 LogPanel 上（沿用旧实现，不重写）
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import win_utils
from core.config_manager import get_config
from ui.help_badge import titled_header
from ui.icons import window_icon
from ui.log_panel import LogPanel
from ui import window_geometry

# Segoe MDL2 Assets 图钉字形：E718=空心斜图钉（未钉），E840=实心直图钉（已钉）
_PIN_OFF = ""
_PIN_ON = ""


class LogWindow(QWidget):
    """独立运行日志窗口。只读输出，提供 .log() 与旧面板完全相同的接口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # QWidget 带 parent 默认嵌进父容器，必须置顶层标志才是真窗口
        self.setWindowFlags(self.windowFlags() | Qt.Window)
        self.setWindowTitle("运行日志")
        # 任务栏辨认图标：绿圆白字「日」（三窗各一色一字，见 ui.icons.window_icon）
        self.setWindowIcon(window_icon("日", "#2E8B57"))
        self._cfg = get_config()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        # 标题行：功能名 + ?徽章（项目约定）+ 清空按钮
        head = titled_header(
            "运行日志",
            "全程序运行信息的输出栏：批次操作、挂机线程、异常提醒都写在这里。<br><br>"
            "格式：[时间] [级别] 内容，级别显示为中文（信息/警告/错误），"
            "整行按级别着色：信息=绿、警告=橙、错误=红。<br>"
            "任务边界行特标：开始=金、完成=蓝、失败=红（一个任务一块，一眼挑出结局）。<br>"
            "独立窗口：可拖到副屏或主窗口旁边，不挤占主界面空间。<br>"
            "最多保留 2000 行；同时每个实例的日志还会落盘到 logs/run_日期.log（关程序不丢）。",
            "winTitle")
        # 置顶钉：学 Windows 自己的图钉钮（记事本标签页/资源管理器那套）——
        # 没钉=空心斜图钉，钉上=实心直图钉，状态全在字形里，不搞底色那套。
        # 字形来自系统自带图标字体 Segoe MDL2 Assets（Win10+ 必装），
        # 零图片资源零绘制代码，颜色随系统主题自动走。
        self.btn_pin = QPushButton(_PIN_OFF)
        # 豁免全局按钮 QSS 的 padding（钉上后 28px 宽被左右内衬吃光、字形画没）；
        # 样式细节见 theme.py 的 QPushButton#iconBtn
        self.btn_pin.setObjectName("iconBtn")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setFlat(True)
        self.btn_pin.setFixedWidth(28)
        _pin_font = QFont("Segoe MDL2 Assets")
        _pin_font.setPointSize(11)
        self.btn_pin.setFont(_pin_font)
        self.btn_pin.setToolTip("钉住：日志窗浮在所有窗口最前面，不被遮挡")
        self.btn_pin.toggled.connect(self._toggle_pin)
        head.addWidget(self.btn_pin)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(lambda: self.panel.clear())
        head.addWidget(btn_clear)
        lay.addLayout(head)

        # 主体：沿用原 LogPanel（滚动缓冲/行数上限/格式化逻辑都在它身上）
        self.panel = LogPanel()
        lay.addWidget(self.panel, stretch=1)

        # 还原上次位置大小；默认放在主窗口右侧偏下，避开主界面
        window_geometry.restore(self, self._cfg, "ui.log_window_geometry", (560, 640))

    def showEvent(self, event):
        """每次显示重补任务栏按钮：本窗有主人默认不进任务栏（最小化会缩成
        迷你标题栏飘屏）；且「置顶钉」setWindowFlag 会重建 HWND，样式跟着没。
        再补一个专属【窗口级】任务栏身份：不设则与主窗同组、被合并成一个按钮，
        点按钮只弹预览不最小化（详见 core.win_utils.set_window_app_id）。"""
        super().showEvent(event)
        hwnd = int(self.winId())
        win_utils.ensure_taskbar_button(hwnd)
        win_utils.set_window_app_id(hwnd, win_utils.APPID_LOG)

    def log(self, message: str, level: str = "INFO"):
        """追加一行日志（与旧 LogPanel.log 同签名，调用点无感知）"""
        self.panel.log(message, level)

    def _toggle_pin(self, on: bool):
        """置顶钉切换：WindowStaysOnTopHint 是系统级"永远浮顶"，
        盖过任何普通窗口（包括从任务栏还原刚 raise 的窗口）。
        setWindowFlag 改这个标志会把窗口 hide 一次再重建，改完必须补 show，
        否则一点置顶窗口当场消失。"""
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        # 状态像 Windows 标题栏图钉那样全在字形里：斜空心=没钉，直实心=钉上；
        # checkable 按钮自带按下态描边，不再手刷底色
        self.btn_pin.setText(_PIN_ON if on else _PIN_OFF)
        self.btn_pin.setToolTip("已置顶：浮在所有窗口最前面，点击取消" if on
                                else "钉住：日志窗浮在所有窗口最前面，不被遮挡")
        if was_visible:
            self.show()

    def save_geometry(self):
        """
        存位置大小。不放在 closeEvent 里：本窗口是主窗口的子顶层窗口，
        关程序时随父窗口销毁、不走 closeEvent，几何就丢了。
        改由 MainWindow.closeEvent 显式调用（确定性保存）。
        """
        window_geometry.save(self, self._cfg, "ui.log_window_geometry")

    def closeEvent(self, event):
        self.save_geometry()   # 单独关掉日志窗时也要记住位置
        super().closeEvent(event)
