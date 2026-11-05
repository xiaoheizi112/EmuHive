"""
ui/icons.py — 图标按钮的唯一登记处（系统字形 + 内联 SVG 两种来源）

优先 Windows 自带图标字体（Segoe MDL2 Assets）：零资源、随系统主题走。
新增图标先在这里登记常量，不散落硬编码 \\ueXXX。
纪律（宁缺毋滥）：只有在 Windows 10 实际核对过显示的字形才登记——
错一个字形，按钮上就是乱码方块。

MDL2 没有的（全 PUA 区段逐码位扫描确认）：垂直对齐、等距分布类图标。
这类走内联 SVG（layout_button）：不贴图片文件、矢量字符串随代码走，
QtSvg（PySide6 自带模块）渲染。
"""

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QPushButton

from ui import theme

# ── 已核对字形（对照 Win10 Segoe MDL2 Assets 实际显示）──
ADD = ""        # Add：＋
UP = ""         # Up：↑
DOWN = ""       # Down：↓
CANCEL = ""     # Cancel：✕（移除选中项）
CLEAR = ""      # Clear：清空
SAVE = ""       # Save：磁盘（存配置）
DELETE = ""     # Delete：垃圾桶（删配置）
LOCK = ""       # Lock：闭锁（流程上锁 = 只读保护）
UNLOCK = ""     # Unlock：开锁（流程解锁 = 恢复可编辑）
CAMERA = ""     # Camera：相机（采图 / 去采集）
EDIT = ""       # Edit：铅笔（就地编辑引用的子流程）


ICON_FONT = QFont("Segoe MDL2 Assets", 11)
_ICON_FONT = ICON_FONT   # 旧名保留：文件内既有引用不动


def icon_button(glyph: str, object_name: str = "", size: int = 24,
                tip: str = "") -> QPushButton:
    """图标按钮：字形 + 系统图标字体。悬停提示必配——光图标说不清功能。"""
    b = QPushButton(glyph)
    b.setFont(_ICON_FONT)
    b.setFixedSize(size, size)
    if object_name:
        b.setObjectName(object_name)
    if tip:
        b.setToolTip(tip)
    return b


def glyph_icon(glyph: str, px: int = 16) -> QIcon:
    """字形 → QIcon（给「图标 + 中文文字」的按钮用）。
    不能直接给按钮设图标字体：Segoe MDL2 没有中文字形，整串中文会跟着
    变方块。画成位图当 icon，图标与文字各用各的字体，互不干扰。
    渲染尺寸取 2×（高分屏不发虚），显示尺寸由按钮的 setIconSize 定。"""
    pm = QPixmap(px * 2, px * 2)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    f = QFont(ICON_FONT)
    f.setPixelSize(px * 2)
    painter.setFont(f)
    painter.setPen(QColor(theme.TEXT))
    painter.drawText(pm.rect(), Qt.AlignCenter, glyph)
    painter.end()
    return QIcon(pm)


def window_icon(char: str, color: str, px: int = 32) -> QIcon:
    """任务栏辨认图标（需求：多开最小化后缩略图一眼分得出谁是谁）：
    现画纯色圆底 + 白色中文单字，QPainter 零图片资源。
    不走 Segoe MDL2 字形：图标字体字形须肉眼核对（本文件纪律），
    而「三窗互不相同 + 与默认 python 图标明显不同」几何画法即达标。
    2× 渲染同 glyph_icon，高分屏不发虚。char 用界面中文（项目显示文本约定）。"""
    size = px * 2
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(0, 0, size, size)
    painter.setPen(QColor("white"))
    f = QFont()
    f.setPixelSize(int(size * 0.55))
    f.setBold(True)
    painter.setFont(f)
    painter.drawText(pm.rect(), Qt.AlignCenter, char)
    painter.end()
    return QIcon(pm)


# ── 画布对齐/分布工具图标（市面通用样式，见下每键注释）──
# MDL2 没有垂直对齐与等距分布的字形（全 PUA 扫描确认），这类走内联 SVG：
# 零图片文件，QtSvg 渲染。条形长短/位置编码语义：左/中/右 = 三条横线
# 长短不一、公共边在左/中/右（Word 段落对齐同款）；顶/竖中/底 = 旋转 90°；
# 分布 = 两端竖线 + 居中块（PPT 横向分布同款）。
def _svg(rects: str) -> str:
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            f'<g fill="{theme.TEXT}">{rects}</g></svg>')

_LAYOUT_SVGS = {
    "left":   _svg('<rect x="2" y="2" width="12" height="2"/><rect x="2" y="7" width="8" height="2"/><rect x="2" y="12" width="12" height="2"/>'),
    "hmid":   _svg('<rect x="2" y="2" width="12" height="2"/><rect x="4" y="7" width="8" height="2"/><rect x="2" y="12" width="12" height="2"/>'),
    "right":  _svg('<rect x="2" y="2" width="12" height="2"/><rect x="6" y="7" width="8" height="2"/><rect x="2" y="12" width="12" height="2"/>'),
    "top":    _svg('<rect x="2" y="2" width="2" height="12"/><rect x="7" y="2" width="2" height="8"/><rect x="12" y="2" width="2" height="12"/>'),
    "vmid":   _svg('<rect x="2" y="2" width="2" height="12"/><rect x="7" y="4" width="2" height="8"/><rect x="12" y="2" width="2" height="12"/>'),
    "bottom": _svg('<rect x="2" y="2" width="2" height="12"/><rect x="7" y="6" width="2" height="8"/><rect x="12" y="2" width="2" height="12"/>'),
    "hspread": _svg('<rect x="1" y="2" width="2" height="12"/><rect x="7" y="5" width="2" height="6"/><rect x="13" y="2" width="2" height="12"/>'),
    "vspread": _svg('<rect x="2" y="1" width="12" height="2"/><rect x="5" y="7" width="6" height="2"/><rect x="2" y="13" width="12" height="2"/>'),
}


def layout_button(name: str) -> QPushButton:
    """对齐/分布工具按钮：16px SVG 图标（32px 渲染，高分屏不发虚）。
    颜色取 theme.TEXT，与文字按钮同色，无需第二套图标资源。"""
    r = QSvgRenderer(QByteArray(_LAYOUT_SVGS[name].encode("utf-8")))
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)          # PySide6 的 QSvgRenderer 只吃 QPainter
    r.render(painter)
    painter.end()
    b = QPushButton()
    b.setObjectName("iconBtn")   # 免全局 padding 挤压，见 theme.py#iconBtn
    b.setIcon(QIcon(pm))
    b.setIconSize(QSize(16, 16))
    b.setFixedSize(26, 24)
    return b