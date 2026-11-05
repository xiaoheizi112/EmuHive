"""
ui/theme.py — 全局界面主题（颜色 / 字号 / 全局 QSS 的单一出口）

设计：浅色简约 + 单一强调色。所有颜色、字号、间距收敛成常量，
面板代码只从这取色，禁止散落硬编码 hex。

应用方式：main.py 里 QApplication 建好后调用 theme.apply(app)，
全局 QSS 一次性覆盖所有窗口。各面板不再自带 setStyleSheet——
需要差异化时用 objectName 在全局 QSS 里写选择器（如 #queueCtl）。
只有内容色（状态灯、表格状态列）这类动态颜色在代码里用常量直赋。

约定：
- 画布（窗口底）浅灰 #f6f7f9，卡片（列表/表格/输入框）纯白
- 1px 细线分隔（border），4px 圆角，不玩阴影渐变
- 强调色只给「主操作」和「选中态」，其余一律中性灰
"""

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

# ── 色板 ──
BG_CANVAS = "#f6f7f9"       # 窗口底色（画布）
BG_CARD = "#ffffff"         # 卡片：列表 / 表格 / 输入框 / 菜单
BG_SUBTLE = "#f1f3f7"       # 表头 / 斑马纹 / 分组标题底色
HOVER = "#f3f6fb"           # 列表悬停
BORDER = "#e3e7ee"          # 常规分隔线
BORDER_STRONG = "#d5dbe4"   # 输入框 / 按钮描边

TEXT = "#1f2329"            # 主文字
TEXT_SECONDARY = "#5c6370"  # 次要文字
TEXT_MUTED = "#98a0ab"      # 弱化文字（未分类 / 占位）

ACCENT = "#2f6fed"          # 强调蓝：主按钮 / 选中 / 链接字
ACCENT_HOVER = "#2457d0"
ACCENT_PRESSED = "#1e4ab8"
ACCENT_SOFT = "#e8f0fe"     # 选中底色 / 悬停浅蓝

DANGER = "#d64545"          # 危险（停止 / 错误 / 删除）
DANGER_HOVER = "#b93a3a"
SUCCESS = "#1a9e56"         # 成功（保存）
SUCCESS_HOVER = "#157e45"

WARN = "#b26a00"            # 警告文字（偏离基准分辨率等）
GOLD = "#e8a33d"            # 魂环金：挂机中状态灯（签名元素）
HIGHLIGHT_DUP = "#fbeecb"   # 一号多开：表格里重复账号名的琥珀底（提醒"这号挂了几台"）

# ── 字号 / 字体 ──
FONT_FAMILY = '"Microsoft YaHei UI","Microsoft YaHei","Segoe UI",sans-serif'
BASE_PT = 9                 # setFont 基准字号（约 12px）

# ── 全局 QSS（唯一入口，各面板不再自带样式表）──
# ⚠ 字体不在这里设 font-family/font-size：
#   QSS 的 font 属性会覆盖控件 setFont() 设置的一切，导致 flow_editor 的
#   Segoe MDL2 图标按钮变方块、小字号标签全变大。统一字体走 app.setFont()，
#   没显式设置字体的控件自动跟随（行为契约：图标按钮/小字号标签本地声明的
#   字体优先）。
GLOBAL_QSS = f"""
QWidget {{
    background: {BG_CANVAS}; color: {TEXT};
}}
QLabel {{ background: transparent; }}

/* ── 标题（各窗口/面板共用 objectName: winTitle / sidebarTitle / queueTitle）── */
QLabel#winTitle, QLabel#sidebarTitle, QLabel#queueTitle {{
    font-size: 13px; font-weight: 600; color: {TEXT};
}}
QLabel#secondary {{ color: {TEXT_SECONDARY}; }}
QLabel#warnLabel {{ color: {DANGER}; font-weight: 600; }}
QLabel#queueSummary {{ color: {ACCENT}; font-weight: 600; }}
QLabel#statStrong {{ font-weight: 600; }}
QLabel#statusLight {{ font-size: 15px; }}

/* ── 分组框：不要边框，只留分组标题（简约）── */
QGroupBox {{
    border: none; margin-top: 10px; padding-top: 0;
    font-weight: 600; color: {TEXT_SECONDARY};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 2px; padding: 0 2px; }}

/* ── 流程清单 / 侧栏 / 日志面板的特殊底色 ── */
#categorySidebar {{ background: {BG_SUBTLE}; }}
#logPanel {{ background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: 4px; }}
QLabel#queueTitle, QLabel#sidebarTitle {{ color: {TEXT}; }}

/* ── 按钮 ── */
QPushButton {{
    background: {BG_CARD}; border: 1px solid {BORDER_STRONG};
    border-radius: 4px; padding: 5px 14px; color: {TEXT};
}}
QPushButton:hover {{ background: {HOVER}; border-color: #bdc7d6; }}
QPushButton:pressed {{ background: {BG_SUBTLE}; }}
/* 开关/互斥按钮的选中态（抠图开关、工作台切页钮等 checkable 文本钮）：
   全局此前没定 :checked，全靠 Windows 默认按下态 —— 实测跟没按几乎无差，
   用户根本看不出开关开没开。accent 描边 + 浅蓝底 = 大众 toggle 按钮画法。
   显式带 :hover 组合：防悬停时 HOVER 灰底同优先级覆盖选中蓝底（一悬停
   就"掉电"，状态提示归零）。*/
QPushButton:checked {{
    background: {ACCENT_SOFT}; border-color: {ACCENT}; color: {ACCENT}; font-weight: 600;
}}
QPushButton:checked:hover {{ background: {ACCENT_SOFT}; border-color: {ACCENT}; }}
QPushButton:checked:pressed {{ background: {BG_SUBTLE}; }}
QPushButton:disabled {{ background: #f2f3f5; color: #b3b9c2; border-color: #e6e9ee; }}

/* 主操作按钮（全部启动 / 全部挂机 / 试运行） */
QPushButton#primary, QPushButton#btnAccent {{
    background: {ACCENT}; border: 1px solid {ACCENT}; color: #ffffff; font-weight: 600;
}}
QPushButton#primary:hover, QPushButton#btnAccent:hover {{
    background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; color: #ffffff;
}}
QPushButton#primary:pressed, QPushButton#btnAccent:pressed {{
    background: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED};
}}
QPushButton#primary:disabled, QPushButton#btnAccent:disabled {{
    background: #b8c6ea; border-color: #b8c6ea; color: #ffffff;
}}

/* 危险按钮（全部停止） */
QPushButton#dangerBtn {{
    background: {DANGER}; border: 1px solid {DANGER}; color: #ffffff; font-weight: 600;
}}
QPushButton#dangerBtn:hover {{ background: {DANGER_HOVER}; border-color: {DANGER_HOVER}; color: #ffffff; }}
QPushButton#dangerBtn:disabled {{ background: #e8b8b8; border-color: #e8b8b8; color: #ffffff; }}

/* 成功按钮（保存） */
QPushButton#btnSuccess {{
    background: {SUCCESS}; border: 1px solid {SUCCESS}; color: #ffffff; font-weight: 600;
}}
QPushButton#btnSuccess:hover {{ background: {SUCCESS_HOVER}; border-color: {SUCCESS_HOVER}; color: #ffffff; }}

/* 小图标钮通用豁免（置顶钉、对齐/分布钮等，objectName=iconBtn）：
   全局 QPushButton 规则一旦设了 border，QSS 就接管绘制（setFlat 失效），
   padding 左右共 28px 会把固定宽的图标钮内容区挤成 0 —— 字形直接画没
   （日志窗置顶钉消失的实测根因，内容区 -2px）。这类钮一律零内衬零边框，
   悬停/选中态给轻底色补可点性提示。 */
QPushButton#iconBtn {{
    padding: 0; border: 1px solid transparent; background: transparent;
}}
QPushButton#iconBtn:hover {{ background: {HOVER}; border-color: {BORDER_STRONG}; }}
QPushButton#iconBtn:checked {{ background: {ACCENT_SOFT}; border-color: {ACCENT}; }}
QPushButton#iconBtn:pressed {{ background: {BG_SUBTLE}; }}

/* 小方图标按钮（流程清单的 ＋↑↓✕ 存/删/清，见 ui/icons.py）+ 属性面板图标小钮
   （采图相机 / 删行垃圾桶，见 flow_editor._icon_btn）：三者同款零 padding 中性描边
   —— 全局规则那 28px 左右内衬会把 28px 宽的固定小钮内容区挤成负数、字形直接画没；
   flowIcon 不做危险色，删除语义靠垃圾桶字形本身表达。 */
QPushButton#queueIcon, QPushButton#queueIconDanger, QPushButton#flowIcon {{
    padding: 0; border: 1px solid {BORDER_STRONG};
    border-radius: 4px; background: {BG_CARD}; color: {TEXT_SECONDARY};
}}
QPushButton#queueIcon:hover, QPushButton#flowIcon:hover {{
    background: {ACCENT_SOFT}; border-color: {ACCENT}; color: {ACCENT};
}}
QPushButton#queueIconDanger {{ color: {DANGER}; }}
QPushButton#queueIconDanger:hover {{
    background: #fdeeee; border-color: {DANGER}; color: {DANGER};
}}

/* ── 输入控件（下拉 / 数字框 / 输入框）── */
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
    background: {BG_CARD}; border: 1px solid {BORDER_STRONG};
    border-radius: 4px; padding: 3px 6px; color: {TEXT};
}}
QComboBox:hover, QSpinBox:hover, QLineEdit:hover {{ border-color: #a9c0f5; }}
QComboBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {BG_CARD}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT}; outline: 0;
}}
/* QSpinBox 的 ::up-button/::down-button 这里刻意不设规则：样式表一旦声明
   子控件就不回退原生渲染，之前只写 width 没写 subcontrol-position 和箭头图，
   上下按钮变成 16px 纯空白（用户报"看不到增减钮"的根因）。不碰它 = Windows
   原生上下按钮 + 箭头，零资源随主题走（QDoubleSpinBox 没规则一直正常即反证） */

/* ── 菜单栏（底部功能页签）── */
QMenuBar {{ background: transparent; }}
QMenuBar::item {{ padding: 5px 14px; border-radius: 4px; color: {TEXT_SECONDARY}; }}
QMenuBar::item:selected, QMenuBar::item:pressed {{
    background: {ACCENT_SOFT}; color: {ACCENT};
}}

/* ── 胶囊页签（中间列表框上方 账号管理/资产管理：QToolButton 带 tabPill
   动态属性）── 选中态走 :checked。不用 QMenuBar 借外观：实测它的样式表
   渲染不认 :checked、也不吃 action 字体，"当前在哪页"根本亮不出来 */
QToolButton[tabPill="true"] {{
    background: transparent; border: 0; padding: 5px 14px;
    border-radius: 4px; color: {TEXT_SECONDARY};
}}
QToolButton[tabPill="true"]:hover {{ background: {HOVER}; }}
QToolButton[tabPill="true"]:checked {{
    background: {ACCENT_SOFT}; color: {ACCENT};
}}

/* ── 弹出菜单 ── */
QMenu {{
    background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px;
}}
QMenu::item {{ padding: 5px 22px 5px 12px; border-radius: 4px; color: {TEXT}; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
QMenu::item:disabled {{ color: {TEXT_MUTED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

/* ── 列表 ── */
/* 白卡片底三份共用：裸 QListWidget / 流程清单 #queueList / 流程库 #flowList */
QListWidget, #queueList, #flowList {{
    background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: 4px; outline: 0;
}}
QListWidget::item {{ padding: 6px 8px; }}
QListWidget::item:hover {{ background: {HOVER}; }}
QListWidget::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
QListWidget::item:disabled {{ color: {TEXT_MUTED}; }}

/* ── 流程清单（底部窄panel）── */
#queuePanel {{ background: {BG_CANVAS}; }}
#queueList::item {{ padding: 5px 6px; border-bottom: 1px solid {BORDER}; }}
#queueList::item:selected {{ background: {ACCENT_SOFT}; }}

/* 左侧/工作台流程库列表（objectName: flowList，两处共用） */
#flowList::item {{
    padding: 8px 8px; border-bottom: 1px solid {BORDER};
}}
#flowList::item:hover {{ background: {HOVER}; }}
#flowList::item:selected {{
    background: {ACCENT_SOFT}; border-left: 3px solid {ACCENT};
    padding-left: 5px; color: {TEXT};
}}
#flowList::item:selected:hover {{ background: {ACCENT_SOFT}; }}
#flowList::item:disabled {{ color: {TEXT_MUTED}; }}

/* 主窗口右侧操作竖列：大按钮更宽松，分组标题带字间距 */
#opsColumn QPushButton {{ padding: 7px 10px; }}
#opsGroup {{ margin-top: 12px; font-size: 11px; letter-spacing: 1px; }}

/* ── 表格 ── */
QTableWidget {{
    background: {BG_CARD}; alternate-background-color: {BG_SUBTLE};
    /* 网格线原为 #eef1f5：和白卡片几乎同色，用户点名"账号/区服没隔开"的根因
       ——表头有线、格子没线。加深到 BORDER_STRONG，列分隔一眼可见 */
    gridline-color: {BORDER_STRONG}; border: 1px solid {BORDER}; border-radius: 4px;
}}
QTableWidget::item {{ padding: 2px 6px; }}
/* 实例表（超级列表框）专属密排：收窄格内边距，跟 8pt 字号 + 22px 行高配套
   （见 ui/account_panel.py）*/
#accountTable::item {{ padding: 1px 4px; }}
QTableWidget::item:selected {{ background: {ACCENT}; color: #ffffff; }}
QHeaderView::section {{
    background: {BG_SUBTLE}; padding: 5px 8px; border: none;
    border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER};
    font-weight: 600; color: {TEXT_SECONDARY};
}}

/* ── 滚动条（细条圆头）── */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: #c8ced8; border-radius: 5px; min-height: 24px; margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background: #a9b2c0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: #c8ced8; border-radius: 5px; min-width: 24px; margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: #a9b2c0; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    width: 0; height: 0; background: transparent;
}}

/* ── 分隔条 / 状态栏 / 工具提示 / 对话框 ── */
QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:hover {{ background: #c3cbd8; }}
QStatusBar {{
    background: transparent; border-top: 1px solid {BORDER};
}}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {TEXT_SECONDARY}; }}
QToolTip {{
    background: {BG_CARD}; color: {TEXT}; border: 1px solid {BORDER_STRONG};
    padding: 4px 6px;
}}
QDialog {{ background: {BG_CARD}; }}

/* ── 帮助徽章（功能名后的 ?）── */
QLabel#helpBadge {{
    color: {TEXT_MUTED}; font-size: 11px; font-weight: 600;
    background: {BG_SUBTLE}; border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
}}
QLabel#helpBadge:hover {{
    background: {ACCENT_SOFT}; border-color: {ACCENT}; color: {ACCENT};
}}

/* 模板采集放大镜：黑底框是画面预览本体，不是样式 */
QLabel#magnifier {{
    border: 1px solid #888; background: #000; color: #ccc;
}}
"""


def lamp_color(n_work: int, n_run: int) -> str:
    """状态灯三态选色（主窗口 / 状态窗共用一份，避免两处 if/elif 漂移）：
    金=有挂机中 / 蓝=运行但没挂机 / 灰=全停机（签名元素）。"""
    if n_work:
        return GOLD
    if n_run:
        return ACCENT
    return TEXT_MUTED


def apply(app: QApplication):
    """QApplication 建好后调用一次：挂全局字体 + 全局样式表"""
    app.setFont(QFont("Microsoft YaHei UI", BASE_PT))
    app.setStyleSheet(GLOBAL_QSS)