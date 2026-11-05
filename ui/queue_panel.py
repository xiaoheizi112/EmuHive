"""
ui/queue_panel.py — 任务清单（挂机队列）面板

清单项 = 流程名（config.custom_flows 的键；同一流程可入队多遍）。
上一版清单装的是模板库目录，交给 worker 前要按 library_bindings() 现场换算
成任务记号 —— 任务/流程分开后清单项本身就是流程名，换算退化成
"存的时候直接存 flow:名字"，那套绑定扫描随之删除。

「全部挂机」把清单逐条换算成 "flow:流程名" 串成任务链交给 AccountWorker
（worker 拆冒号记号的机制原样保留，引擎零改动）。

本文件的知识点：
- 清单内容永远以 self._queue（流程名列表）这份数据为准，界面只是它的投影：
  任何改动都走 _commit()（改数据 → 重画列表 → 存盘 → 发信号），
  不会出现"界面显示的和实际要跑的不一致"
- 存盘键沿用 task_order（历史原因）：内容已是流程名，老数据里的库目录名/
  英文任务名不再是现存流程 → 构造时自动滤掉，无需迁移
- 下拉框重建选项时 blockSignals：程序填选项不该触发"用户选了某个配置"
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from core.config_manager import get_config
from ui import icons
from ui.confirm import confirm
from ui.flow_utils import live_flow_names
from ui.help_badge import make_help_badge

# 固定宽度：和左侧任务栏一个量级的窄条，只留"1. 登录"这种一行字的宽度，
# 腾出来的横向空间全给底部的菜单设置页（那边内容更宽）
QUEUE_WIDTH = 150

# 清单面板：面板底色、卡片列表、图标按钮样式全在全局主题 ui/theme.py

class _QueueList(QListWidget):
    """任务清单列表：Delete/Backspace 即移除选中项。
    （双击原来也是移除，手滑一点就丢一项还不提示；现腾给「查看流程」，
    删除改走按钮/Del 键两条明路。）"""

    delete_pressed = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


# 原来占两行的底部说明文字，现在挂在标题旁问号图标的悬停提示里
_HELP_TOOLTIP = (
    "点「＋」选流程 → 追加进本清单\n"
    "（流程在工作台 → 流程编排 里编；清单里没有的流程 = 已被删，自动剔除）\n"
    "清单从上到下 = 「全部挂机」的执行顺序\n"
    "\n"
    "↑ / ↓ 调整顺序    ✕ 移除选中项\n"
    "清 = 全部清空    双击清单项 = 查看该流程（进工作台编辑）\n"
    "Delete 键 = 移除选中项\n"
    "\n"
    "下拉框选配置 = 导入该配置\n"
    "存 = 把当前清单存成具名配置\n"
    "删 = 删掉下拉框里选中的配置\n"
    "\n"
    "（左侧任务栏双击一个任务 = 等效于在本清单点「＋」加入它）"
)


class TaskQueuePanel(QWidget):
    """任务清单：顺序即执行顺序；可存/读具名配置"""

    queue_changed = Signal(list)   # 清单内容变化（流程名列表）
    view_flow_requested = Signal(str)  # 双击清单项 = 查看该流程（主窗口开工作台选它）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = get_config()
        self.setObjectName("queuePanel")
        self.setFixedWidth(QUEUE_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # ── 标题行：标题 + 紧贴的「?」（说明文字挂悬停，不占版面）──
        # stretch 必须放在徽章后面：放中间会把徽章推到行尾，离名字太远
        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        self.title = QLabel("任务清单")
        self.title.setObjectName("queueTitle")
        title_row.addWidget(self.title)
        title_row.addWidget(make_help_badge(_HELP_TOOLTIP))
        title_row.addStretch(1)
        layout.addLayout(title_row)

        # ── 配置行：下拉选配置导入 / 存当前清单 / 删配置 ──
        # 存/删按钮用 Segoe MDL2 图标（磁盘/垃圾桶），零图片资源
        preset_row = QHBoxLayout()
        preset_row.setSpacing(2)
        self.cmb_preset = QComboBox()
        self.cmb_preset.setToolTip("选择已保存的清单配置，自动导入")
        preset_row.addWidget(self.cmb_preset, stretch=1)
        self.btn_save_preset = icons.icon_button(icons.SAVE, "queueIcon",
                                           tip="把当前清单存成具名配置")
        preset_row.addWidget(self.btn_save_preset)
        self.btn_del_preset = icons.icon_button(icons.DELETE, "queueIconDanger",
                                          tip="删除下拉框里选中的配置")
        preset_row.addWidget(self.btn_del_preset)
        layout.addLayout(preset_row)

        # ── 清单 + 右侧竖排小按钮（清空并入按钮列，不再独占一行）──
        mid = QHBoxLayout()
        mid.setSpacing(2)
        self.list = _QueueList()
        self.list.setObjectName("queueList")
        self.list.setToolTip("双击一项 = 查看该流程（进工作台编辑）\nDelete 键 = 移除选中项")
        mid.addWidget(self.list, stretch=1)

        # 图标按钮：都来自 Segoe MDL2（系统字形，不手搓不贴图）
        arrows = QVBoxLayout()
        arrows.setSpacing(3)
        self.btn_add = icons.icon_button(icons.ADD, "queueIcon",
                                   tip="追加流程到清单末尾（弹出流程菜单选一项）")
        self.btn_up = icons.icon_button(icons.UP, "queueIcon", tip="上移选中项")
        self.btn_down = icons.icon_button(icons.DOWN, "queueIcon", tip="下移选中项")
        self.btn_remove = icons.icon_button(icons.CANCEL, "queueIcon", tip="移除选中项")
        self.btn_clear = icons.icon_button(icons.CLEAR, "queueIconDanger", tip="清空全部流程")
        for b in (self.btn_add, self.btn_up, self.btn_down, self.btn_remove, self.btn_clear):
            arrows.addWidget(b)
        arrows.addStretch(1)
        mid.addLayout(arrows)
        layout.addLayout(mid, stretch=1)

        # ── 信号 ──
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(1))
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear.clicked.connect(self.clear)
        # 双击 = 查看流程（删除已腾给 ✕ 按钮 / Del 键，见 _QueueList）
        self.list.itemDoubleClicked.connect(
            lambda it: self.view_flow_requested.emit(it.data(Qt.UserRole) or ""))
        self.list.delete_pressed.connect(self.remove_selected)
        self.btn_add.clicked.connect(self._on_add_clicked)
        self.btn_save_preset.clicked.connect(self.save_preset)
        self.btn_del_preset.clicked.connect(self.delete_preset)
        # 用 activated 而不是 currentIndexChanged：
        # - 方向键翻选项时 currentIndexChanged 每按一下都触发，会把清单反复冲掉
        # - 重选"当前已显示"的那一项时 currentIndexChanged 不触发，导入会没反应
        # activated 只在用户真正从弹列表/回车选定那一下触发，正合适
        self.cmb_preset.activated.connect(self._on_preset_selected)

        # ── 还原当前清单：只保留还存在的流程 ──
        # 键名沿用 task_order（历史原因）：现存的每一条都是"流程名"。
        # 上一版内容曾是库目录名、更早是英文任务名，都不是现存流程名
        # → 构造时自动滤掉，老数据无迁移成本。
        live = self._live_flows()
        self._queue: list[str] = [
            f for f in (self.cfg.get("task_order", []) or []) if f in live
        ]
        self._refresh_list()
        self.refresh_presets()

    # ── 现存流程 = config.custom_flows 的键（唯一真相，侧栏同款）──
    @staticmethod
    def _live_flows() -> set[str]:
        return live_flow_names()   # 口径收在 ui/flow_utils，与任务栏同一份

    # ── 清单读写（元素 = 流程名）──
    def current_queue(self) -> list[str]:
        return list(self._queue)

    def queue_as_tasks(self) -> list[str]:
        """清单换算成任务链（交给 AccountWorker 的东西）；已删的流程跳过。
        worker 对 "flow:流程名" 记号的拆解机制原样保留，见 bot/worker.py。"""
        live = self._live_flows()
        return [f"flow:{f}" for f in self._queue if f in live]

    def add_flow(self, fname: str):
        """追加一条流程（「＋」菜单/侧栏双击调用）；允许重复，同流程可跑多遍。
        不存在的流程直接忽略——菜单里只有现存流程，这里是对外部调用的第二道闸。"""
        if fname not in self._live_flows():
            return
        self._queue.append(fname)
        self._commit()

    def set_queue(self, flows):
        """整体替换（导入配置用）；过滤掉已不存在的流程"""
        live = self._live_flows()
        self._queue = [f for f in (flows or []) if f in live]
        self._commit()

    def clear(self):
        if not self._queue:
            return
        self._queue = []
        self._commit()

    def remove_selected(self):
        row = self.list.currentRow()
        if 0 <= row < len(self._queue):
            del self._queue[row]
            self._commit()

    def rename_flow(self, old: str, new: str):
        """流程改名（工作台实时转发）：清单里的旧名就地换成新名。
        配置（queue_presets）值里同样按名字引用，一起换。"""
        if old in self._queue:
            self._queue = [new if f == old else f for f in self._queue]
            self._commit()
        else:
            self._refresh_list()
        presets = self._presets()
        dirty = False
        for k, v in presets.items():
            if isinstance(v, list) and old in v:
                presets[k] = [new if f == old else f for f in v]
                dirty = True
        if dirty:
            self.cfg.set("queue_presets", presets)

    def refresh_flows(self):
        """
        流程集合变了（工作台建/删流程）后由主窗口调用：
        把清单里指向已删流程的死项就地剔除并刷新。
        没有死项就只重画，不打扰用户、不空写盘。
        """
        live = self._live_flows()
        pruned = [f for f in self._queue if f in live]
        if pruned != self._queue:
            self._queue = pruned
            self._commit()
        else:
            self._refresh_list()

    def remove_all_flows(self, fname: str) -> int:
        """把清单里 fname 的所有出现清掉（任务栏「删除任务」连带清清单用）。
        允许重复入队，所以清全部出现。返回清掉了几项。"""
        n = self._queue.count(fname)
        if not n:
            return 0
        self._queue = [f for f in self._queue if f != fname]
        self._commit()
        return n

    def _on_add_clicked(self):
        """「＋」：在按钮下方弹流程菜单，选一条追加到清单末尾。

        菜单列全部现存流程（和左侧列表一套）；一条流程都没编过时给出
        指路提示，而不是弹一个空菜单。
        """
        flows = sorted(self._live_flows())
        menu = QMenu(self)
        if not flows:
            menu.addAction("（还没有流程：先去工作台 → 流程编排 编一条）").setEnabled(False)
        for fname in flows:
            act = menu.addAction(fname)
            act.setData(fname)
        picked = menu.exec(self.btn_add.mapToGlobal(self.btn_add.rect().bottomLeft()))
        if picked is not None and picked.data():
            self.add_flow(picked.data())

    def _move(self, delta: int):
        row = self.list.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= len(self._queue):
            return
        self._queue[row], self._queue[target] = self._queue[target], self._queue[row]
        self._commit()
        self.list.setCurrentRow(target)   # 选中跟着走，连点↑不用重新选

    def _commit(self):
        """改数据 → 重画 → 存盘 → 通知外部（唯一的状态出口）"""
        self._refresh_list()
        self.cfg.set("task_order", self._queue)   # 防抖存盘，重启不丢（键名沿用，内容现为流程名）
        self.queue_changed.emit(list(self._queue))

    def _refresh_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for i, fname in enumerate(self._queue, 1):
            item = QListWidgetItem(f"{i}. {fname}")
            item.setData(Qt.UserRole, fname)
            item.setToolTip(f"第 {i} 项：{fname}")
            self.list.addItem(item)
        self.list.blockSignals(False)
        # 窄面板放不下"任务清单（N）"，计数挪进标题的悬停提示
        self.title.setToolTip(f"共 {len(self._queue)} 项任务，从上到下依次挂机")

    # ── 具名配置 ──
    def _presets(self) -> dict:
        presets = self.cfg.get("queue_presets", {})
        return presets if isinstance(presets, dict) else {}

    def refresh_presets(self, keep_selection: bool = True):
        """重建下拉框选项（存/删配置后调用）"""
        current = self.cmb_preset.currentText() if keep_selection else ""
        self.cmb_preset.blockSignals(True)
        self.cmb_preset.clear()
        self.cmb_preset.addItems(list(self._presets().keys()))
        if current and self.cmb_preset.findText(current) >= 0:
            self.cmb_preset.setCurrentText(current)
        self.cmb_preset.blockSignals(False)

    def save_preset(self):
        name, ok = QInputDialog.getText(self, "保存清单配置", "配置名：")
        name = (name or "").strip()
        if not ok or not name:
            return
        presets = self._presets()
        presets[name] = list(self._queue)
        self.cfg.set("queue_presets", presets)
        self.refresh_presets()
        self.cmb_preset.setCurrentText(name)   # 存完顺手选中，所见即所得

    def delete_preset(self):
        name = self.cmb_preset.currentText()
        if not name or name not in self._presets():
            return
        if not confirm(self, "删除配置", f"删除清单配置「{name}」？"):
            return
        presets = self._presets()
        presets.pop(name, None)
        self.cfg.set("queue_presets", presets)
        self.refresh_presets()

    def _on_preset_selected(self, _index: int):
        """下拉选中某个配置 → 导进清单（替换当前清单）"""
        name = self.cmb_preset.currentText()
        preset = self._presets().get(name)
        if preset is not None:
            self.set_queue(preset)
