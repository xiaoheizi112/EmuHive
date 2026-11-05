"""
ui/category_sidebar.py — 主窗口最左侧任务栏（任务/流程分开版）

任务栏装的是「任务」：工作台流程编辑里点「加入任务栏」把一条流程生成成
任务挂进来（同一流程可生成多条任务，允许重复），全程平铺不分类。
流程本体与流程分类只在工作台里管理（工作台流程库侧栏 + 工具栏分类下拉）。

交互：
- 双击任务 = 加入底部「任务清单」（挂机执行顺序），允许重复入队
- 右键任务 = 删除任务（无确认弹窗，点了就删）
- 删除任务 = 只删任务栏上这一条；任务清单里的同名条目由主窗口一并清掉，
  工作台里的流程本体不受影响

数据：config.task_bar = [流程名...]（顺序 = 显示顺序）。任务只是流程名的
挂靠点：流程从工作台删除后，指向它的任务变死任务，对账时自动剔除。

历史：本文件前任是「分类侧栏」（按用途分类显示全部流程 + 拖拽分类），
任务/流程分开后分类入口只剩工作台，本文件整体改写为任务栏。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from core.config_manager import get_config
from ui import theme
from ui.flow_utils import live_flow_names
from ui.help_badge import make_help_badge

# 固定宽度：平铺任务名一行字 + 两侧留白，132 与旧分类侧栏同宽
SIDEBAR_WIDTH = 132

_ROLE_FLOW = Qt.UserRole        # 任务条目的流程名

# 任务栏底色与全局主题一致，列表卡片样式走全局主题（objectName: flowList）
_DEAD_FG = QBrush(QColor(theme.TEXT_MUTED))


_HELP_TOOLTIP = (
    "这里列的是任务（来自工作台流程的「加入任务栏」，平铺不分类）。\n"
    "双击任务 = 加入底部「任务清单」\n"
    "（清单从上到下 = 「全部挂机」的执行顺序）\n"
    "\n"
    "右键任务 = 删除任务（点了就删，不弹确认）\n"
    "删除任务只影响这一条任务和任务清单里的同名引用，\n"
    "工作台里的流程本体不受影响。"
)


class TaskSidebar(QWidget):
    """主界面最左侧任务栏：任务（流程名引用）平铺列表，双击 = 加入任务清单"""

    task_double_clicked = Signal(str)          # 双击任务：加入底部任务清单
    task_deleted = Signal(str)                 # 右键「删除任务」已从任务栏删掉：主窗口清清单同名引用

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = get_config()
        self.setObjectName("categorySidebar")   # 全局 QSS 认这个名（背景色）
        self.setFixedWidth(SIDEBAR_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # ── 标题行：标题 + 紧贴的「?」（操作说明挂悬停，不占版面）──
        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        self.title = QLabel("任务栏")
        self.title.setObjectName("sidebarTitle")
        title_row.addWidget(self.title)
        title_row.addWidget(make_help_badge(_HELP_TOOLTIP))
        title_row.addStretch(1)
        layout.addLayout(title_row)

        self.list = QListWidget()
        self.list.setObjectName("flowList")   # 主题 flowList 卡片样式（原侧栏同名复用）
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setToolTip("任务从工作台流程的「加入任务栏」进来")
        layout.addWidget(self.list, stretch=1)

        self.list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context_menu)

        self._sig = None   # 渲染签名：没变化就不重建（防 15 秒定时器不动也重画）
        self._build_items()

    # ── 数据：任务 = 流程名列表，存 config.task_bar ──
    def _tasks(self) -> list[str]:
        raw = self.cfg.get("task_bar", [])
        return list(raw) if isinstance(raw, list) else []

    def _live_flows(self) -> set[str]:
        """现存流程 = config.custom_flows 的键（口径收在 ui/flow_utils，
        与任务清单同一份，改语义只改一处）"""
        return live_flow_names()

    def _signature(self) -> tuple:
        """渲染签名：任务列表 + 现存流程集合。任何一项变了才重建。"""
        return (tuple(self._tasks()), tuple(sorted(self._live_flows())))

    def __len__(self) -> int:
        return len(self._tasks())

    # ── 构建 ──
    def _build_items(self):
        live = self._live_flows()
        tasks = self._tasks()
        self.list.blockSignals(True)
        self.list.clear()
        for fname in tasks:
            gone = fname not in live
            it = QListWidgetItem(fname)
            it.setData(_ROLE_FLOW, fname)
            if gone:
                it.setForeground(_DEAD_FG)
                it.setToolTip(f"⚠ 流程「{fname}」已从工作台删除：死任务，刷新后自动剔除")
            else:
                it.setToolTip("双击 = 加入底部任务清单\n"
                              "右键 = 删除任务")
            self.list.addItem(it)
        self.list.blockSignals(False)
        self._sig = self._signature()
        self.title.setToolTip(f"共 {len(tasks)} 条任务")

    def refresh_flows(self) -> bool:
        """流程对账（主窗口周期 15s + 工作台关闭都走这里）：
        剔除指向已删流程的死任务；没有死任务只重画、不空写盘。
        返回是否真的重建了（主窗口据此决定要不要顺带清清单死项）。"""
        if self._signature() == self._sig:
            return False
        live = self._live_flows()
        keep = [f for f in self._tasks() if f in live]
        if keep != self._tasks():
            self.cfg.set("task_bar", keep)   # 防抖存盘：死任务下次启动不再出现
        self._build_items()
        return True

    # ── 写入口 ──
    def add_task(self, fname: str):
        """工作台「加入任务栏」：任务栏追加一条任务（同流程可重复生成）。
        不存在的流程直接忽略——工作台与主界面并排开时流程可能刚被删。"""
        if fname not in self._live_flows():
            return
        tasks = self._tasks()
        tasks.append(fname)
        self.cfg.set("task_bar", tasks)
        self._build_items()

    def rename_task(self, old: str, new: str):
        """流程改名（工作台实时转发）：任务栏里的旧名就地换新。"""
        tasks = self._tasks()
        if old not in tasks:
            return
        self.cfg.set("task_bar", [new if f == old else f for f in tasks])
        self._build_items()

    def remove_at(self, row: int) -> str | None:
        """删除指定行的任务；返回删掉的任务名（外部据此清清单引用）。"""
        tasks = self._tasks()
        if not 0 <= row < len(tasks):
            return None
        name = tasks.pop(row)
        self.cfg.set("task_bar", tasks)
        self._build_items()
        self.task_deleted.emit(name)
        return name

    # ── 交互 ──
    def _on_item_double_clicked(self, item: QListWidgetItem):
        if item is not None:
            self.task_double_clicked.emit(item.data(_ROLE_FLOW))

    def _on_context_menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return   # 空白处右键无菜单：任务只能从工作台进来，侧栏不设入口
        row = self.list.row(item)
        menu = QMenu(self)
        menu.addAction("删除任务",
                       lambda _=False, r=row: self.remove_at(r))
        menu.exec(self.list.mapToGlobal(pos))