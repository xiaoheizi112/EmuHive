"""
ui/help_badge.py — 功能名后面的「?」提示徽章（全项目通用组件）

约定（以后新增任何功能都照这个来）：
    功能名字紧跟一个圆形「?」小徽章，鼠标悬停显示这个功能的介绍/用法。
    徽章必须紧贴名字后面，不要靠 addStretch 推到行尾——
    推到行尾后它离名字太远，看不出是解释哪个功能的。

用法：
    row = QHBoxLayout()
    row.addWidget(QLabel("模拟器分辨率"))
    row.addWidget(make_help_badge("这里写悬停显示的功能介绍"))
    row.addStretch(1)          # stretch 放在徽章后面，把整组往左推

样式统一放全局主题（ui/theme.py）：objectName: helpBadge，
各面板无需任何自带 QSS 就能用，外观全项目统一。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QVBoxLayout


def make_help_badge(tip: str) -> QLabel:
    """
    返回一个「?」徽章控件：16x16 圆形，悬停显示 tip。
    tip 支持富文本（<br> 换行），所以传纯文本或多行说明都行。
    """
    badge = QLabel("?")
    badge.setObjectName("helpBadge")
    badge.setFixedSize(16, 16)
    badge.setAlignment(Qt.AlignCenter)
    badge.setToolTip(tip)
    return badge


def titled_header(title: str, tip: str, object_name: str = "statStrong") -> QHBoxLayout:
    """「功能名 + 紧贴的 ? 徽章 + 行尾留白」这一行样板（不建分组框）。

    stretch 放在徽章后面：整组靠左、徽章贴名字（见本文件头约定）。
    调用点拿到这个 QHBoxLayout 后继续 addWidget 往右追加按钮等。
    object_name：面板内小标题用 statStrong（默认），窗口大标题用 winTitle。
    """
    row = QHBoxLayout()
    row.setSpacing(4)
    lbl = QLabel(title)
    lbl.setObjectName(object_name)
    row.addWidget(lbl)
    row.addWidget(make_help_badge(tip))
    row.addStretch(1)
    return row


def titled_group(title: str, tip: str, spacing: int = 6) -> tuple:
    """空标题 QGroupBox + 第一行「功能名 + ? 徽章」的整块样板。

    分组框标题栏嵌不了控件，所以标题留空、把功能名当第一行画进去。
    返回 (box, layout)：调用点往 layout 里继续 addWidget/addLayout 塞正文。
    spacing = 组内行间距（默认 6）；功能设置页传更小值可做紧凑排版，别处不动。
    """
    box = QGroupBox("")
    lay = QVBoxLayout(box)
    lay.setSpacing(spacing)
    lay.addLayout(titled_header(title, tip))
    return box, lay
