"""
ui/multi_test_dialog.py — 工作台「多开测试」：同一条流程在选中的多台实例上各跑一遍

分层与既有单实例试跑（ui/workbench.py 的 _start_test + bot/flow_test.py）一致，
只是把「一台」扩成「一组」：

- 每台一条现成的 bot.flow_test.FlowTestWorker（零改动复用，图快照/执行链/信号
  形状全部照旧）；
- MultiTestController 负责：逐台**错峰起跑**、汇总每台结果、停止全部；
- MultiTestDialog 负责：勾选实例 + 结果小表；工作台（外壳）只负责准入检查
  与开窗，运行期把单实例「▶ 测试流程」按钮禁用。

错峰（为什么不一次全起）：N 台同时起跑会让 CPU/GUI 瞬时高峰（同
core.ldplayer 的 launch_interval_sec 思路）——各台 worker 起跑间隔
general.multi_test_gap_ms（默认 700ms），用 QTimer.singleShot 逐个 start：
**绝不在 GUI 线程 sleep**，也绝不一次全拥上。

单主人：控制器持有 worker 列表，一台实例同时只允许一个测试 worker；跑多开
测试期间单实例按钮禁用，避免两个 worker 撞同一台。broker/准入检查归外壳。

结果表四列：实例号 / 结果（✓完成/✗失败/■已停/运行中/待启动）/ 卡在哪一步 /
失败截图路径（引擎自动存 logs/fail_capture/inst{实例号}/）。
"""

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from bot.flow_test import FlowTestWorker
from ui import theme

# 结果状态 → 表内文字（与 _on_test_done 的单实例口径一致，多一个 pending/aborted）
_STATE_TEXT = {
    "pending": "待启动",
    "running": "运行中",
    "done": "✓ 完成",
    "failed": "✗ 失败",
    "stopped": "■ 已停",
    "aborted": "■ 已中止",
}
# 状态 → 结果列颜色（只动文字色，不整行铺底）
_STATE_COLOR = {
    "done": theme.SUCCESS,
    "failed": theme.DANGER,
    "stopped": theme.TEXT_MUTED,
    "aborted": theme.WARN,
    "running": theme.ACCENT,
    "pending": theme.TEXT_MUTED,
}
_TERMINAL = ("done", "failed", "stopped", "aborted")

# 各台起跑默认间隔（毫秒）：约 0.7 秒一台，CPU 不至于被 N 台 worker 同时起跑顶出瞬时高峰
# （同 core.ldplayer 的 launch_interval_sec 思路，量级按"0.5~1 秒"定）。不新开配置键——
# 需要时由调用方传 gap_ms 覆盖（冒烟用它把间隔压到几十毫秒）。
DEFAULT_GAP_MS = 700


class MultiTestController(QObject):
    """多台 FlowTestWorker 的总控：错峰起跑 + 结果汇总 + 停止全部。

    不碰任何 widget（只发信号），符合「ui 读 worker 只走信号」：worker 的
    action_log/step_changed/finished_state 全部经这里转成 changed 信号，
    对话框按实例号刷新对应行。
    """

    changed = Signal(int)               # (实例号) 该台结果行变化 → UI 刷新这一行
    log_message = Signal(int, str, str)  # (实例号, 级别, 消息) → 转发主日志面板
    all_done = Signal()                 # 全部收工（含从未启动/被停）

    def __init__(self, graph: dict, flow_name: str, indexes, parent=None,
                 gap_ms=None, worker_factory=None):
        super().__init__(parent)
        self._graph = graph
        self._flow_name = flow_name
        self._order = list(indexes)
        self._pending = list(indexes)
        self._gap_ms = self._resolve_gap(gap_ms)
        # 工厂缝：默认起真 FlowTestWorker；冒烟替换成假线程（不碰模拟器）
        self._factory = worker_factory or (
            lambda idx, g, fn: FlowTestWorker(idx, g, parent=self, flow_name=fn))
        self._workers: dict = {}
        self._results = {i: {"state": "pending", "step": "", "fail_dir": ""}
                         for i in indexes}
        self._active = False
        self._done_emitted = False

    @staticmethod
    def _resolve_gap(gap_ms):
        """起跑间隔（毫秒）：调用方显式给了就用，否则用模块默认（不新开配置键）。"""
        if gap_ms is not None:
            try:
                return max(0, int(gap_ms))
            except (TypeError, ValueError):
                return DEFAULT_GAP_MS
        return DEFAULT_GAP_MS

    # ── 对外只读接口 ──
    def results(self) -> dict:
        """每台结果快照（拷贝）：UI 画表用，外改不脏内部账。"""
        return {i: dict(v) for i, v in self._results.items()}

    def running(self) -> bool:
        """是否仍有 worker 在跑（停止请求已发但线程未退也算在跑）。"""
        return any(w.isRunning() for w in self._workers.values())

    def worker(self, idx: int):
        return self._workers.get(idx)

    # ── 起跑：逐台错峰 ──
    def start(self):
        self._active = True
        self._launch_next()

    def _launch_next(self):
        if not self._active:
            return   # 已被停止：不再起新的
        if not self._pending:
            self._settle()
            return
        idx = self._pending.pop(0)
        w = self._factory(idx, self._graph, self._flow_name)
        self._workers[idx] = w
        w.action_log.connect(self._on_log)
        w.step_changed.connect(self._on_step)
        w.finished_state.connect(self._on_finished)
        self._set(idx, state="running", step="启动中…")
        w.start()
        if self._pending:
            # 下一台隔 gap_ms 再起：QTimer 不进 GUI 线程 sleep，界面不卡
            QTimer.singleShot(self._gap_ms, self._launch_next)

    # ── 停止：掐掉全部（在跑的请求停止，没起的直接记已停）──
    def request_stop_all(self):
        self._active = False
        for idx in list(self._pending):
            self._set(idx, state="stopped", step="未启动")
        self._pending.clear()
        for w in self._workers.values():
            if w.isRunning():
                w.request_stop()
        self._settle()   # 未起的已在上面收口；在跑的要等它自己 emit 收尾

    def wait_all(self, msec: int = 3000):
        """收摊：等所有 worker 线程退出（每台最多给 msec 毫秒）。窗口关闭时调，
        防线程往已销毁的窗口 emit 在后台炸。"""
        for w in self._workers.values():
            if w.isRunning():
                w.wait(msec)

    # ── worker 信号汇总 ──
    def _set(self, idx, state=None, step=None):
        r = self._results.get(idx)
        if r is None:
            return
        if state is not None:
            r["state"] = state
        if step is not None:
            r["step"] = step
        self.changed.emit(idx)

    def _on_log(self, idx, level, msg):
        self.log_message.emit(idx, level, msg)

    def _on_step(self, idx, step):
        # 只在运行中收步骤：收尾后迟到的 step 不覆写终态展示
        if self._results.get(idx, {}).get("state") == "running":
            self._set(idx, step=step)

    def _on_finished(self, idx, state):
        r = self._results.get(idx)
        if r is None:
            return
        r["state"] = state
        if state == "failed" and not r["fail_dir"]:
            r["fail_dir"] = f"logs/fail_capture/inst{idx}"
        self.changed.emit(idx)
        self._settle()

    def _settle(self):
        """全部收口才发 all_done（只发一次）；还有在跑/待起的不发。"""
        if self._done_emitted or self._pending:
            return
        for idx in self._order:
            if self._results[idx]["state"] not in _TERMINAL:
                return
        self._done_emitted = True
        self.all_done.emit()


class MultiTestDialog(QDialog):
    """多开测试小窗：左勾选实例、右结果小表、底部起跑/停止/关闭。

    非模态——用户可边跑边看工作台/日志；运行期把本窗勾选框与「开始」禁用，
    只留「停止全部」。关闭时若还在跑则先掐全部（不留后台孤儿 worker）。
    """

    multi_started = Signal()          # 起跑一发 → 外壳锁单实例按钮
    multi_finished = Signal()         # 全部收工 → 外壳解锁
    log_message = Signal(str, str)    # (级别, 消息，已带实例前缀)

    def __init__(self, parent=None, gap_ms=None):
        super().__init__(parent)
        self.setWindowTitle("多开测试 · 同一条流程在多台实例上各跑一遍")
        self.setModal(False)
        self._gap_ms = gap_ms
        self._graph = None
        self._flow_name = ""
        self._controller = None
        self._rows: dict = {}   # 实例号 → 表格行号

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        self.lbl_info = QLabel("勾选要同时测试的实例（没运行 / 正在挂机的不能选）。"
                               "各台错峰起跑，结果实时汇总在右侧表。")
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)

        mid = QHBoxLayout()
        self.lst_instances = QListWidget()
        self.lst_instances.setFixedWidth(240)
        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["实例", "结果", "卡在哪一步", "失败截图路径"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionMode(QAbstractItemView.NoSelection)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        mid.addWidget(self.lst_instances)
        mid.addWidget(self.tbl, 1)
        lay.addLayout(mid, 1)

        btns = QHBoxLayout()
        self.btn_start = QPushButton("▶ 开始多开测试")
        self.btn_start.setObjectName("btnAccent")
        self.btn_stop = QPushButton("■ 停止全部")
        self.btn_stop.setEnabled(False)
        self.btn_close = QPushButton("关闭")
        btns.addWidget(self.btn_start)
        btns.addWidget(self.btn_stop)
        btns.addStretch(1)
        btns.addWidget(self.btn_close)
        lay.addLayout(btns)

        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_close.clicked.connect(self.close)
        self.resize(760, 420)

    # ── 外壳每次开窗调它：灌流程 + 候选实例 + 清空上轮 ──
    def configure(self, graph: dict, flow_name: str, candidates):
        """candidates: [{"idx","label","selectable","reason"} ...]（外壳已做准入）。"""
        self._graph = graph
        self._flow_name = flow_name
        self.lst_instances.clear()
        any_selectable = False
        for c in candidates:
            it = QListWidgetItem(c["label"])
            if c.get("selectable"):
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked)   # 默认全勾：多数场景就是要全跑
                it.setData(Qt.UserRole, c["idx"])
                any_selectable = True
            else:
                # 不可选：灰显 + 撤掉启用/勾选位，行尾点名原因（未运行/正在挂机）
                it.setFlags(Qt.NoItemFlags)
                it.setData(Qt.UserRole, c["idx"])
                if c.get("reason"):
                    it.setText(f"{c['label']}（{c['reason']}）")
            self.lst_instances.addItem(it)
        self.tbl.setRowCount(0)
        self._rows = {}
        self.btn_start.setEnabled(any_selectable)
        self.btn_stop.setEnabled(False)
        self.lst_instances.setEnabled(True)

    # ── 起跑 ──
    def _checked_indexes(self):
        out = []
        for i in range(self.lst_instances.count()):
            it = self.lst_instances.item(i)
            if (it.flags() & Qt.ItemIsUserCheckable) and it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out

    def _on_start(self):
        if self._controller is not None and self._controller.running():
            return
        idxs = self._checked_indexes()
        if not idxs:
            self.lbl_info.setText("先勾选至少一台可测实例。")
            return
        self._controller = MultiTestController(
            self._graph, self._flow_name, idxs, parent=self, gap_ms=self._gap_ms)
        self._controller.changed.connect(self._on_changed)
        self._controller.log_message.connect(self._forward_log)
        self._controller.all_done.connect(self._on_all_done)
        self._build_rows(idxs)
        self.lst_instances.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_info.setText("多开测试进行中…（各台错峰起跑，跑完自动解锁）")
        self._controller.start()
        self.multi_started.emit()

    def _build_rows(self, idxs):
        self.tbl.setRowCount(len(idxs))
        for row, idx in enumerate(idxs):
            self._rows[idx] = row
            self.tbl.setItem(row, 0, QTableWidgetItem(f"{idx}"))
            self.tbl.setItem(row, 1, QTableWidgetItem(_STATE_TEXT["pending"]))
            self.tbl.setItem(row, 2, QTableWidgetItem("—"))
            self.tbl.setItem(row, 3, QTableWidgetItem(""))
            self._paint(row, "pending")

    # ── 停止 / 收工 ──
    def _on_stop(self):
        if self._controller is not None:
            self._controller.request_stop_all()
            self.lbl_info.setText("已请求停止，正在掐掉各台…")

    def _on_all_done(self):
        self.lst_instances.setEnabled(True)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        done = sum(1 for r in (self._controller.results().values() if self._controller else [])
                   if r["state"] == "done")
        self.lbl_info.setText(f"多开测试结束：{done} 台完成。可重新勾选再跑。")
        self.multi_finished.emit()

    # ── 结果刷行 ──
    def _on_changed(self, idx):
        row = self._rows.get(idx)
        if row is None or self._controller is None:
            return
        r = self._controller.results().get(idx)
        if r is None:
            return
        self.tbl.item(row, 1).setText(_STATE_TEXT.get(r["state"], r["state"]))
        self.tbl.item(row, 2).setText(r["step"] or "—")
        self.tbl.item(row, 3).setText(r["fail_dir"] or "")
        self._paint(row, r["state"])

    def _paint(self, row, state):
        color = QColor(_STATE_COLOR.get(state, theme.TEXT))
        for col in (1, 2):
            it = self.tbl.item(row, col)
            if it is not None:
                it.setForeground(color)

    def _forward_log(self, idx, level, msg):
        self.log_message.emit(level, f"[多开测试·{idx}] {msg}")

    # ── 关闭：还在跑就掐全部（不留后台孤儿 worker）──
    def running(self) -> bool:
        return self._controller is not None and self._controller.running()

    def shutdown(self, msec: int = 3000):
        """收摊（外壳 closeEvent 调）：掐全部 + 逐台有界等线程退出。"""
        if self._controller is not None:
            self._controller.request_stop_all()
            self._controller.wait_all(msec)

    def closeEvent(self, event):
        if self._controller is not None and self._controller.running():
            self._controller.request_stop_all()
        super().closeEvent(event)