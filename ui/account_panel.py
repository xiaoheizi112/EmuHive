"""
ui/account_panel.py — 实例表格（超级列表框风格）

参考多开工具的"超级列表框"重做外观与信息密度：
- 信息列 13 列 = 账号文件的列（一一对应，拖进来即所见即所得）：
  档案在前 序号/备注/账号/区服/等级，运行时在后 运行任务/当前动作/完成度/
  当前模拟器/状态/挂机状态/已运行/资产（末列「资产」= 流程「资产入库」节点
  落的账，缩写显示，右键「查看资产」看明细，读自 accounts.extra_json.assets）。
  ⚠ 列名列序与 core/account_io.py 的 FILE_COLUMNS 一字不差，改一边同步另一边
  档案列数据源 = accounts 表按 ld_index 绑定（TXT 拖进主窗口即导入）；
  运行时列 = worker 实时状态，文件里写什么都不生效（防显示假状态）
- 整行选中（蓝底白字，和参考图一致）、斑马纹、细网格线、表头底色
- 8pt 字号 + 22px 行高的密排；双向滚动条常驻（列固定宽 + 表头可拖动调宽/调换，
  总宽超面板即横向滚，行多即纵向滚；AlwaysOn 不随内容跳动、不挤列宽）
- 操作走右键菜单（启动/关闭/挂机/暂停恢复），不占表格列宽；
  菜单项可用性由该行当前状态决定，与旧按钮逻辑一一对应
- 双击原地改档案（2026-09 需求）：选中行的 备注/账号/区服/等级 四格左键双击
  即变输入框，回车/点别处提交写库；「序号」= 模拟器绑定关系不给改，运行时列
  （运行任务→资产）是 worker 现算的，改了也会被 3 秒刷新顶掉——都不给编辑位；
  没绑账号的空行同样不给（先拖 TXT 导入才有号可改）

本步学到的知识点：
- 右键菜单：setContextMenuPolicy(CustomContextMenu) + customContextMenuRequested
  信号，pos 参数是 viewport 坐标，rowAt(pos) 定位右键的是哪一行；
  菜单用 popup() 在该位置弹出（不用 exec()——exec 阻塞且样式走不动）
- 按钮状态由数据驱动：refresh() 时把实例列表和 worker 状态存下来，
  右键菜单构建时直接读——界面永远只反映数据，不自己"记住"状态
- 菜单项的 toggle 语义（挂机/停止挂机、暂停/恢复）按 worker 状态换文字，
  与原按钮的切换逻辑相同
- 菜单三段（2026-09-21 扩充）：模拟器操作（启动/关闭/重启/置前）→
  挂机控制 → 账号档案（重置该账号/账号全部重置，后者弹确认框）。
  全部走信号回主窗口执行，面板自己不做任何数据库/系统调用
- 进度列（当前任务/当前动作/已运行）的数据来自 worker 的 task_changed 信号，
  主窗口攒成 progress 字典传进来；表格只投影，不自己计时
- 双击原地编辑：Qt 的"能不能编辑"写在每个 item 的 flags 上（ItemIsEditable），
  setEditTriggers(DoubleClicked) 只是打开"双击进编辑"这扇门——但注意
  QTableWidgetItem 默认就带可编辑位（旧版靠 NoEditTriggers 全局锁没暴露），
  所以 _centered 建格时先摘位，refresh 再按 EDITABLE_COLS 点名挂回去。
  提交靠 cellChanged 信号，两道闸防误报：程序自己刷格子时置 _loading 标志
  忽略；编辑框还开着时 refresh 直接跳过（否则 3 秒一轮重建把输入框连人带字顶掉）
"""

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMenu,
    QMessageBox,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
)

from core import database, ldplayer
from ui import theme


# 可双击原地编辑的档案列 → 数据库字段（口径与 core/account_io.FILE_COLUMNS 同源）。
# 刻意排除：「序号」（改它=改模拟器绑定关系，误点代价大）、运行时 8 列
# （worker 每 3 秒现算，改了也会被顶掉——那是显示假状态的根源）。
EDITABLE_COLS = {1: "name", 2: "account", 3: "server", 4: "level"}

# 档案字段 → 单元格显示文本（"—" = 空值占位；refresh 与编辑去重共用这一份口径）
def acct_display(acct: dict, field: str) -> str:
    if field == "level":
        try:
            lv = int(acct.get("level") or 0)
        except (TypeError, ValueError):
            lv = 0
        return str(lv) if lv else "—"
    return str(acct.get(field) or "") or "—"


def fmt_asset(n) -> str:
    """资产数值缩写：≥1亿→x.x亿，≥1万→x.x万（多开扫表看趋势够用，
    精确值右键「查看资产」看明细）。非数字原样返回（账本容坏数据）。"""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return str(n)
    if v >= 100000000:
        return f"{v / 100000000:.1f}亿"
    if v >= 10000:
        return f"{v / 10000:.1f}万"
    return str(v)

# 表格样式统一在全局主题 ui/theme.py（表格/表头/选中态/斑马纹/网格线），
# 本面板不再自带样式表。inst_status 返回的状态色在这里上色。


# worker 状态 → 显示文字（status_window 共用同一份，别各写各的）
WS_TEXT = {"idle": "空闲", "running": "运行中", "paused": "已暂停",
           "starting": "启动中…", "error": "异常"}


def fmt_elapsed(seconds: float) -> str:
    """秒数 → 03:25 / 1:03:25 这种时长文本"""
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def inst_status(inst) -> tuple[str, str]:
    """实例状态列的 (文字, 颜色)：主表格与状态窗共用同一套措辞，不分家"""
    if not inst.running:
        return "已停止", theme.TEXT_MUTED
    return ("安卓就绪", theme.SUCCESS) if inst.android_ready else ("启动中…", theme.WARN)


def _centered(text: str) -> QTableWidgetItem:
    """居中对齐的单元格（超级列表框风格：数字/短状态全部走中间）。
    tooltip 带全文：列宽装不下被省略号截断时，悬停即见完整内容
    （信息齐全的最后兜底，零布局代价）；占位符 "—" 不设，悬停弹个破折号是噪音。

    ⚠ QTableWidgetItem 默认自带 ItemIsEditable（以前 NoEditTriggers 全局锁着
    看不出来），这里先摘干净——谁该可编辑由 refresh 按 EDITABLE_COLS 点名挂位。"""
    it = QTableWidgetItem(text)
    it.setTextAlignment(Qt.AlignCenter)
    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if text and text != "—":
        it.setToolTip(text)
    return it


class _CommitDelegate(QStyledItemDelegate):
    """界面默认委托（渲染/编辑框都照旧），只加一件事：
    在「写回模型」那一刻把编辑框里的字截下来存到面板的 _commit_capture。

    为什么要截：实测提交路径上 cellChanged 触发时 item 文本还是**旧值**
    （模型写入发生在 setModelData 里、信号在其后才同步到 item），
    编辑框文本才是用户最终敲的字——在 setModelData 里拿最可靠。"""

    def setModelData(self, editor, model, index):
        host = self.parent()
        text = editor.text() if hasattr(editor, "text") else ""
        host._commit_capture = (index.row(), index.column(), text)
        super().setModelData(editor, model, index)


class AccountPanel(QTableWidget):
    # 实例启停（第 5 步）：("start"/"quit", index)
    operation_requested = Signal(str, int)
    # 挂机控制（第 7 步）：("start_worker"/"stop_worker"/"pause"/"resume", index)
    worker_command = Signal(str, int)
    # 拖入账号文件（TXT）：只发本地路径，导入由主窗口做（它持有日志面板）
    accounts_file_dropped = Signal(str)
    # 账号档案操作（右键菜单「重置」段）："reset_one" 带实例 index / "reset_all" 不带
    account_op = Signal(str, int)
    # 双击原地改档案（提交时发）：(实例index, 数据库字段名, 新文本)。
    # 面板不碰数据库——与右键菜单同一套约定，写库回主窗口办
    account_field_edited = Signal(int, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # 密排三件套（2026-09-21 用户点名"信息更全"）：字号 8pt + 行高 22 +
        # 格内边距收窄（#accountTable 规则在 theme.py）。8pt 是微软雅黑可读底线，
        # 再小中文发糊；被列宽截断的内容靠每格 tooltip 兜底（见 _centered）。
        self.setObjectName("accountTable")
        f = self.font()
        f.setPointSize(8)
        self.setFont(f)
        # 列序 = 账号文件的列序（档案在前：序号/备注/账号/区服/等级，
        # 运行时列在后），一一对应，拖进来即所见即所得。
        # ⚠ 与 core/account_io.py 的 FILE_COLUMNS 一字不差，改一边同步另一边。
        self.setColumnCount(13)
        self.setHorizontalHeaderLabels([
            "序号", "备注", "账号", "区服", "等级", "运行任务",
            "当前动作", "完成度", "当前模拟器", "状态", "挂机状态", "已运行",
            "资产",
        ])
        # 列宽：全部固定起步宽 + 表头分隔线可拖动改宽（拖拉线），列序可拖动调换。
        # ⚠ 早期版本给 账号/当前动作 两列配了 Stretch：内容永远填满视口 →
        # 横向滚动条永远不出现，宽列被挤到看不全 —— 用户点名要左右滚动条的根因。
        # 改 Interactive 后各列总宽超出面板宽度时横向滚动条自然顶出来；
        # StretchLastSection 只让末列「已运行」吃剩余宽度，撑满观感不变。
        _COL_W = (40, 90, 150, 110, 50, 90, 160, 70, 110, 80, 80, 80, 120)
        for c, w in enumerate(_COL_W):
            self.setColumnWidth(c, w)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        hh.setSectionsMovable(True)      # 按住表头可拖动换列序
        hh.setHighlightSections(False)   # 点表头不整列高亮（单选行为在行上）
        self.verticalHeader().setDefaultSectionSize(22)   # 密排：配合 8pt 字号，一屏多看约 1/5 行
        self.verticalHeader().setVisible(False)           # 行号没用，去掉省地方
        self.setAlternatingRowColors(True)                # 斑马纹
        self.setSelectionBehavior(QTableWidget.SelectRows)  # 整行选中
        # 双击进编辑（Excel/超级列表框习惯）。注意：这只是"开扇门"，
        # 具体哪个格子能编辑由各 item 的 ItemIsEditable 位决定（见 refresh），
        # 全表默认不可编辑 → 运行时列/序号列点了没反应
        self.setEditTriggers(QAbstractItemView.DoubleClicked)
        # 双向滚动条常驻（用户点名）：纵向=行多必滚；横向=列拖宽后总能滚回来。
        # AlwaysOn 而非 AsNeeded：位置固定不跳动，滚动条出现/消失不再挤列宽。
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)

        # 右键菜单：pos 是 viewport 坐标，_show_row_menu 里换算行号
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_row_menu)

        # 拖放导入：把账号 TXT 从资源管理器拖进本表 = 直接入库（"账号"列跟着亮）。
        # 只认文件、不认粘贴文本；只认 .txt（2026-09 起不再收 .csv）。
        # dropAction 显示复制光标（+号），松手前就知道会发生什么。
        self.setAcceptDrops(True)
        self.setToolTip((self.toolTip() + "\n" if self.toolTip() else "") +
                        "把账号 TXT 拖进来即可批量导入（表头需含「账号」列，\n"
                        "「序号」= 雷电模拟器编号，直通不换算、从 1 起（0 号母体不加载）；\n"
                        "列名与本表一致；运行时列如完成度/状态由程序现算，文件里写什么不生效）。\n"
                        "重复导入按账号认人不堆重复行。\n"
                        "⚠ 文件即完整名单：没写的账号会被删除（删前自动备份到 data/backups/）。\n"
                        "双击「备注/账号/区服/等级」格可直接改，回车保存、ESC 撤销。\n"
                        "右键行 = 单台操作（启停/重置/查看资产等）。")

        # 最近一次 refresh 的数据快照（右键菜单构建时读，保证与界面显示一致）
        self._instances: list[ldplayer.InstanceInfo] = []
        self._worker_states: dict[int, str] = {}
        self._accounts: dict[int, dict] = {}   # {实例index: 账号字典}，菜单判断"这行有没有号"
        self._row_sigs: dict[int, tuple] = {}  # 上次渲染的行内容签名（没变的一行整行跳过 setItem）

        # 双击原地改档案的配套状态：
        # _loading = refresh 正在自己刷格子，此间 cellChanged 一律当"程序行为"忽略
        # _commit_capture = 委托在提交一刻截下的 (row, col, 编辑框文本)，消费后置空
        self._loading = False
        self._commit_capture: tuple[int, int, str] | None = None
        self.setItemDelegate(_CommitDelegate(self))
        self.cellChanged.connect(self._on_cell_committed)

    # ── 布局记忆（列宽/列序/滚动位置，关窗时主窗口抓一份存 config）──
    def save_layout(self) -> dict:
        """快照当前表格布局。order 存「显示位置→逻辑列号」：逻辑列号永远
        对应 FILE_COLUMNS 的数据口径，重排只是显示层，导入导出不受影响。"""
        hh = self.horizontalHeader()
        return {
            "widths": [self.columnWidth(c) for c in range(self.columnCount())],
            "order": [hh.logicalIndex(v) for v in range(self.columnCount())],
            "scroll": [self.horizontalScrollBar().value(),
                       self.verticalScrollBar().value()],
        }

    def restore_layout(self, data):
        """套用关窗前存的布局。列数和现在的代码对不上（版本改过列）整份
        作废用默认值——部分套用只会更怪；坏数据同样整份拒收，绝不 set 半个。"""
        n = self.columnCount()
        if not isinstance(data, dict):
            return
        widths, order = data.get("widths"), data.get("order")
        if (not isinstance(widths, list) or len(widths) != n
                or not all(isinstance(w, int) and 10 <= w <= 2000 for w in widths)):
            return
        if (not isinstance(order, list) or len(order) != n
                or sorted(order) != list(range(n))):
            return
        for c, w in enumerate(widths):
            self.setColumnWidth(c, w)
        hh = self.horizontalHeader()
        for vis, logical in enumerate(order):
            if hh.visualIndex(logical) != vis:
                hh.moveSection(hh.visualIndex(logical), vis)
        scroll = data.get("scroll")
        if isinstance(scroll, list) and len(scroll) == 2:
            # QScrollBar 自己钳制越界值：实例变少后旧的纵向位置自动回落到合法区
            self.horizontalScrollBar().setValue(int(scroll[0]))
            self.verticalScrollBar().setValue(int(scroll[1]))

    def refresh(self, worker_states: dict | None = None,
                progress: dict | None = None,
                accounts: dict | None = None,
                assets_by_id: dict | None = None):
        """
        刷新表格。
        worker_states = {实例index: "idle"|"running"|"paused"|...}
        progress      = {实例index: {"task": 显示名, "step": 步骤名,
                                     "since": 开始时间戳, "done": 链跑完与否}}
        accounts      = {实例index: 账号字典}（database.accounts_by_instance() 的
                        返回值；TXT 拖进主窗口导入的就是这里的数据源）
        assets_by_id  = {账号id: 资产dict}（主窗口一轮算一次、与资产总览页共用；
                        不传则本表逐号自己解 extra_json，老调用点行为不变）
        """
        worker_states = worker_states or {}
        progress = progress or {}
        accounts = accounts or {}
        instances = ldplayer.list_instances()
        self._instances = instances
        self._worker_states = worker_states
        self._accounts = accounts
        # 编辑框开着就别重建：主窗口 3 秒一轮定时器会把整表 setItem 一遍，
        # 连人带输入框顶掉——用户字敲一半凭空消失，比不改还糟。
        # 快照已更新（右键菜单/提交判断读的都是最新的），只是这一帧不重画；
        # 编辑框一关（回车/ESC/点别处），下一帧 3 秒内自动补上。
        if self.state() == QAbstractItemView.EditingState:
            return
        # 程序重建全程两道静音：blockSignals 让 cellChanged 根本不外发，
        # _loading 标志是第二道闸（见 _on_cell_committed），防任何旁路误报
        self._loading = True
        self.blockSignals(True)
        try:
            self.setRowCount(len(instances))

            # 一号多开高亮：同一账号名绑了 ≥2 台 → 「账号」列琥珀底（纯显示提示，不拦导入）。
            # 现场扫 accounts 计数即可，不落任何持久化（同名多行是合法用法，不是脏数据）
            name_count: dict[str, int] = {}
            for acct in accounts.values():
                n = str(acct.get("account") or "").strip()
                if n:
                    name_count[n] = name_count.get(n, 0) + 1
            dup_names = {n for n, c in name_count.items() if c >= 2}

            for row, inst in enumerate(instances):
                status, color = inst_status(inst)
                pr = progress.get(inst.index) or {}
                acct = accounts.get(inst.index) or {}
                ws = worker_states.get(inst.index, "idle")

                # 运行任务/当前动作：有 worker 在跑才有内容
                task_text = pr.get("task") or "未运行"
                step_text = pr.get("step") or "无动作"
                elapsed = (fmt_elapsed(time.time() - pr["since"])
                           if pr.get("since") else "—")
                # 完成度：链跑完=已完成；在跑/没跑=未完成（与参考图口径一致）
                done_text = "已完成" if pr.get("done") else "未完成"

                # 封号（watch「封号停用」行命中后 worker 写库 enabled=0）：
                # 账号列红字，一眼扫出哪台废了。只认 enabled 标志，extra_json 里的
                # 原因文本归主窗口/日志侧解析（面板不碰数据库，菜单只发信号）。
                banned = bool(acct) and not acct.get("enabled", 1)
                # 一号多开：账号名重复 → 琥珀底 + 加粗，一眼看出这号挂了几台
                dup = str(acct.get("account") or "").strip() in dup_names

                # 资产列（第 13 列）：「资产入库」节点落的账，缩写显示，悬停看全量。
                # 主窗口 3 秒一轮已按账号 id 现算好一份表（与资产总览页同源），
                # 这里只查表；单独被调（没传表）才自己解一遍 JSON
                if assets_by_id is not None:
                    assets = assets_by_id.get(acct.get("id"), {})
                else:
                    assets = database.get_account_assets(acct)
                a_txt = "、".join(f"{k}{fmt_asset(v)}" for k, v in assets.items())

                # 一行内容 = (文本, 前景色, 底色, 加粗) 元组清单，先攒数据再落格。
                # 与上一帧签名相同的行整行跳过 setItem：过去 3 秒一轮把整表
                # item 全换一遍（模型反复发布局信号、肉眼可见发黏），而多数行
                # 内容根本没变。列序与表头一致（档案在前：序号/备注/账号/区服/
                # 等级，运行时列在后）；序号直通 = 雷电模拟器编号（ldconsole 口径），
                # 实例列表已在 core 层排掉 0 号母体，表格天然从 1 起。
                # 档案四列统一走 acct_display 取显示文本——与编辑去重同一份口径，
                # 两处永远算出一样的字，不会出现"刷新显示 A、去重以为是 B"的漂移。
                cells = [
                    (str(inst.index), None, None, False),
                    (acct_display(acct, "name"), None, None, False),
                    (acct_display(acct, "account"),
                     theme.DANGER if banned else None,
                     theme.HIGHLIGHT_DUP if dup else None,
                     banned or dup),
                    (acct_display(acct, "server"), None, None, False),
                    (acct_display(acct, "level"), None, None, False),   # 等级：现阶段读账号档（TXT 可带）
                    (task_text, None, None, False),
                    (step_text, None, None, False),
                    (done_text, None, None, False),
                    (inst.title, None, None, False),
                    # 状态列着色（状态先画进控件本身，不用底色贴标签）
                    (status, color, None, False),
                    # 封号行不给"空闲/运行中"这类正常态 —— 直接顶掉，红字写进状态本身
                    ("已封禁", theme.DANGER, None, False) if banned
                    else (WS_TEXT.get(ws, ws), None, None, False),
                    (elapsed, None, None, False),
                    (a_txt or "—", None, None, False),
                ]
                # 签名带上账号 id：解绑/换绑但显示文本恰好没变的行也要重画（可编辑位跟着走）
                sig = (acct.get("id"), tuple(cells))
                if self._row_sigs.get(row) == sig:
                    continue    # 内容一模一样：一行都不动
                self._row_sigs[row] = sig
                for c, (txt, fg, bg, bold) in enumerate(cells):
                    it = _centered(txt)
                    if fg:
                        it.setForeground(QColor(fg))
                    if bg:
                        it.setBackground(QColor(bg))
                    if bold:
                        f = it.font()
                        f.setBold(True)
                        it.setFont(f)
                    self.setItem(row, c, it)

                # 挂可编辑位：只给绑了账号的行的档案四列（列口径见 EDITABLE_COLS）。
                # 没号的行改了也没有记录可落，门都不开（双击无反应=最直白的提示）
                if acct:
                    for c in EDITABLE_COLS:
                        it = self.item(row, c)
                        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
            # 台数变少后按行号存的签名尾巴剪掉（行号复用但内容早变了，签名对不上自然重画，
            # 留着只是白占内存）
            self._row_sigs = {r: s for r, s in self._row_sigs.items()
                              if r < len(instances)}
        finally:
            self.blockSignals(False)
            self._loading = False

    def _on_cell_committed(self, row: int, col: int):
        """cellChanged = 模型被写入的唯一入口（用户提交编辑才会走到这，
        程序重建全程被 blockSignals + _loading 拦在外面）。

        比对"提交文本 vs 上次渲染的显示值"，真变了才发 account_field_edited。
        实测语义（PySide6 6.11，见模块头注）：提交瞬间 item 还是旧值、
        同值提交根本不发 cellChanged——所以新值从委托截获的编辑框文本拿。"""
        if self._loading or col not in EDITABLE_COLS:
            self._commit_capture = None
            return
        cap, self._commit_capture = self._commit_capture, None
        # 没头没尾的 cellChanged（无提交截获、或行列对不上）按防御性忽略
        if cap is None or cap[0] != row or cap[1] != col:
            return
        if row >= len(self._instances):
            return
        index = self._instances[row].index
        acct = self._accounts.get(index)
        if not acct:
            return   # 快照里没有号：门没开过，理论上到不了这，兜底
        field = EDITABLE_COLS[col]
        # 「—」只是空值的显示糖：编辑框清空提交 "" 和提交 "—" 都按"想清空"算，
        # 与上次渲染值归一后相同就不发信号（双击"—"直接回车 = 什么都没发生）
        def norm(s: str) -> str:
            return "" if s == "—" else s
        new_text = norm(cap[2].strip())
        if new_text == norm(acct_display(acct, field)):
            return
        self.account_field_edited.emit(index, field, new_text)

    def revert_cell(self, index: int, field: str):
        """把某格改回账号快照的显示值。主窗口驳回提交（等级填了字母之类）时调：
        模型里此刻已是用户敲的坏字（Qt 先写模型后发信号），不弹回去就是界面在骗人。
        不叫 refresh 走重建——编辑器刚关，可能还卡在编辑闸门里被跳过，
        直接改这一个 item 才是说到做到。快照没这行/没这列静默返回。"""
        col = next((c for c, f in EDITABLE_COLS.items() if f == field), None)
        row = next((r for r, inst in enumerate(self._instances)
                    if inst.index == index), None)
        if col is None or row is None:
            return
        it = self.item(row, col)
        if it is None:
            return
        acct = self._accounts.get(index) or {}
        self.blockSignals(True)   # setText 会发 cellChanged，屏蔽掉别绕回提交链
        try:
            it.setText(acct_display(acct, field))
        finally:
            self.blockSignals(False)

    # ── 拖放导入账号文件 ──
    @staticmethod
    def _drop_file(event) -> str:
        """从拖放事件里挑出第一个 txt 的本地路径；没有则空串。
        多文件拖进来只认第一个：批量导错文件比让用户重拖一次危险得多。"""
        if not event.mimeData().hasUrls():
            return ""
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith(".txt"):
                return p
        return ""

    def dragEnterEvent(self, event):
        path = self._drop_file(event)
        if path:
            event.acceptProposedAction()   # 认这个拖放，光标显示"复制"
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        # 不接 move 的话 Qt 默认在按下点就拒绝、拖出原点后无法完成放置
        if self._drop_file(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = self._drop_file(event)
        if path:
            event.acceptProposedAction()
            self.accounts_file_dropped.emit(path)

    # ── 右键菜单（原操作列按钮的替身，信号接口不变）──
    def _show_row_menu(self, pos):
        """
        右键弹出该行的操作菜单。

        菜单项与旧按钮一一对应，可用性按实例/worker 状态决定：
        - 启动：仅停止状态可点；关闭/重启：仅运行中可点
        - 置前：仅运行中可点（没开机的窗口不存在，提了也没意义）
        - 挂机 / 停止挂机：按 worker 状态二选一显示（空闲→挂机，需实例运行；
          运行/暂停→停止挂机）
        - 暂停 / 恢复：仅 worker 运行/暂停时出现（对应旧按钮的禁用逻辑，
          菜单里直接不显示更干净——点了也没意义的项不占地方）
        - 重置该账号 / 账号全部重置（2026-09-21）：档案字段回默认值，
          凭据/绑定/备注不动；该行没绑账号时「重置该账号」禁用；
          全部重置由主窗口弹确认框（面板只管发信号，不拦用户）
        - 封号行（账号 enabled=0）：「挂机」禁用并给原因提示，档案段前给
          「解禁」（发 account_op("unban")，写库由主窗口办）
        - 没封的行给「封号」（发 account_op("ban")）：手动标记通道，与 watch
          「封号停用」行同一套标记；封号/解禁互斥占同一个开关位
        """
        row = self.rowAt(pos.y())   # rowAt 收 y 坐标（pos 是 viewport 坐标的 QPoint）
        if row < 0 or row >= len(self._instances):
            return   # 点在表头/空白处：不弹菜单
        inst = self._instances[row]
        ws = self._worker_states.get(inst.index, "idle")
        index = inst.index

        menu = QMenu(self)
        act_start = menu.addAction("启动")
        act_start.setEnabled(not inst.running)
        act_quit = menu.addAction("关闭")
        act_quit.setEnabled(inst.running)
        act_reboot = menu.addAction("重启")
        act_reboot.setEnabled(inst.running)
        act_front = menu.addAction("置前")
        act_front.setEnabled(inst.running)
        menu.addSeparator()

        # 封号行（enabled=0，watch「封号停用」命中后 worker 写库）：不给挂机，
        # 给解禁；主窗口 start_worker 还有一道同口径门禁兜底
        _ban = bool(self._accounts.get(index)) and \
            not self._accounts[index].get("enabled", 1)
        if ws == "idle":
            act_worker = menu.addAction("挂机")
            act_worker.setEnabled(inst.running and not _ban)
            if _ban:
                act_worker.setToolTip("该账号已标记封号，右键「解禁」后才能挂机")
        else:
            act_worker = menu.addAction("停止挂机")
            pausable = ws == "running"
            act_pause = menu.addAction("暂停" if pausable else "恢复")
            act_pause.triggered.connect(
                lambda _=False, i=index, p=pausable: self.worker_command.emit(
                    "pause" if p else "resume", i))

        act_start.triggered.connect(
            lambda _=False, i=index: self.operation_requested.emit("start", i))
        act_quit.triggered.connect(
            lambda _=False, i=index: self.operation_requested.emit("quit", i))
        act_reboot.triggered.connect(
            lambda _=False, i=index: self.operation_requested.emit("reboot", i))
        act_front.triggered.connect(
            lambda _=False, i=index: self.operation_requested.emit("front", i))
        act_worker.triggered.connect(
            lambda _=False, i=index, w=ws: self.worker_command.emit(
                "start_worker" if w == "idle" else "stop_worker", i))

        # 账号档案段：没绑账号的行不给「重置该账号」
        menu.addSeparator()
        has_acct = index in self._accounts
        # 封号/解禁互斥占同一个开关位：没封给「封号」、封了给「解禁」，
        # 不堆两个半灰项（与全自动封号命中同一套标记，右键「解禁」即撤销）
        if _ban:
            act_unban = menu.addAction("解禁")
            act_unban.setToolTip("清除封号标记（封号原因见日志），该账号恢复可挂机")
            act_unban.triggered.connect(
                lambda _=False, i=index: self.account_op.emit("unban", i))
        else:
            act_ban = menu.addAction("封号")
            act_ban.setEnabled(has_acct)
            if not has_acct:
                act_ban.setToolTip("该行未绑定账号，无可封号")
            else:
                act_ban.setToolTip("手动标记账号封号（看到游戏内封号字样的备用通道）：\n"
                                   "表格红显、启动/挂机自动跳过、在跑的挂机即停；\n"
                                   "右键「解禁」恢复")
            act_ban.triggered.connect(
                lambda _=False, i=index: self.account_op.emit("ban", i))
        # 查看资产：纯读最近一次 refresh 的账号快照（数据本来就带来表上了），
        # 面板自己弹只读框即可，不绕主窗口信号——操作类才走信号，看不改的不算
        act_assets = menu.addAction("查看资产")
        act_assets.setEnabled(has_acct)
        if not has_acct:
            act_assets.setToolTip("该行未绑定账号，无可查看")
        else:
            act_assets.setToolTip("「资产入库」节点记下的金币/钻石等读数明细")
        act_assets.triggered.connect(
            lambda _=False, i=index: self._show_assets(i))
        act_reset = menu.addAction("重置该账号")
        act_reset.setEnabled(has_acct)
        if not has_acct:
            act_reset.setToolTip("该行未绑定账号（拖账号 TXT 导入后这里才有号）")
        act_reset.triggered.connect(
            lambda _=False, i=index: self.account_op.emit("reset_one", i))
        act_reset_all = menu.addAction("账号全部重置")
        act_reset_all.triggered.connect(
            lambda _=False: self.account_op.emit("reset_all", -1))

        menu.popup(self.viewport().mapToGlobal(pos))

    def _show_assets(self, index: int):
        """右键「查看资产」：现查库弹只读明细框（毫秒级读，主线程直读安全；
        读的是最新账面而不是 3 秒前的快照——刚入库的数当场就能看到）。"""
        acc = database.accounts_by_instance().get(index)
        if acc is None:
            return
        assets = database.get_account_assets(acc)
        box = QMessageBox(self)
        box.setWindowTitle(f"实例 {index} 资产")
        box.setIcon(QMessageBox.NoIcon)
        if assets:
            body = "\n".join(f"{k}：{v:,}" for k, v in assets.items())
            box.setText(f"账号「{acc.get('account') or '—'}」\n\n{body}")
            info = str(acc.get("extra_json") or "")
            try:
                import json
                at = json.loads(info or "{}").get("assets_updated_at")
            except (ValueError, TypeError):
                at = None
            if at:
                box.setInformativeText(f"最近更新：{at}")
        else:
            box.setText(f"账号「{acc.get('account') or '—'}」还没有资产记录。\n"
                        "在流程里加「资产入库」节点，读数才会记账。")
        box.exec()
