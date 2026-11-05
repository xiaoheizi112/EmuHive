"""
ui/test_panel.py — 测试设置（底部菜单栏「测试设置」条目的内容页）

从「功能设置」页拆出来的独立一页（2026-10-10 用户反馈：功能设置被挤成一大长条，
多账号管理区被压）。本页收「测试类」功能，当前含容量压测；以后测试相关的新面板
都往这里加，不再往功能设置里塞。

容量压测：把 tools/bench_capacity.py 接进界面——重活（几十轮 × N 台取帧找图）
走后台 QThread，绝不占住界面线程；算法一律复用 tools.bench_capacity
（capacity_steps/demo_round_fn/run_bench/recommend），界面只管输入、线程接线、
结果表——两处各写一套必漂移。
"""

import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import config_manager, json_store, ldplayer
from ui.help_badge import titled_group

_BENCH_HELP = (
    "回答一个一直没测过的问题：<b>这台机器到底能稳定挂几台</b>。<br><br>"
    "原理 = 拿挂机的真实节拍当预算（general.frame_interval，默认 0.5s/拍），"
    "按 4/8/12/16… 递增台数并发跑「取帧→灰度→找图」这条链路，每档跑若干轮，"
    "统计单拍均值 / p95 / 超拍率。谁超了预算谁掉拍（挂机日志上看不见，只是越来越迟钝）。<br><br>"
    "<b>实测</b>要实例已启动、画面正常（launch 是磁盘/CPU 活，混进来算的是开机不是识别）；"
    "另外压测时别同时跑 GUI 挂机、雷电同步器要关，否则互相抢 CPU、结论失真。<br><br>"
    "<b>合成负载 demo</b>：不碰模拟器，用假负载只验工具链路，出不了容量结论。<br><br>"
    "结论口径 = 满足「超拍率≤5% 且 p95≤1.5×预算」的最大台数。建议最大负载以下再留余量，"
    "每加一个功能先问会不会把它打下来。"
)


class _BenchWorker(QThread):
    """容量压测后台线程：跑几十轮 × N 台取帧找图，绝不能占住界面线程。

    照 ui/main_window.py 的 BatchWorker 范式：线程里只 emit 信号、绝不碰 widget；
    每跑完一档 bench_progress 回投填表，结束 bench_done 交结论。
    压测逻辑全复用 tools/bench_capacity.py（本类只管线程与信号接线，不重写算法）。
    """

    bench_progress = Signal(object)   # 一档结果 dict（{N, indexes, stats}）
    bench_done = Signal(object, bool) # (per_n, 是否被取消)

    def __init__(self, runner, steps, rounds, budget, parent=None):
        super().__init__(parent)
        self._runner = runner
        self._steps = steps
        self._rounds = rounds
        self._budget = budget
        self._cancel = threading.Event()

    def cancel(self):
        """请求取消：当前档/当前轮结束后收手（bench_n 每轮开跑前问一次）。"""
        self._cancel.set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self):
        try:
            out = self._runner(self._steps, self._rounds, self._budget,
                               self._cancel.is_set,
                               lambda n, res: self.bench_progress.emit(res))
            self.bench_done.emit(out["per_n"], out["cancelled"])
        except Exception as e:   # 后台线程不能崩：异常当空结果 + 取消态上抛给界面收摊
            self.bench_progress.emit({"error": str(e)})
            self.bench_done.emit({}, True)


class TestPanel(QWidget):
    """测试设置页：容量压测（以后测试类新面板都往这页加）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bench_worker = None   # 容量压测后台线程（跑着时持有引用，防提前 GC）
        self._bench_per_n = {}      # 最近一次压测结果 {N: {indexes, stats}}
        self._bench_pending = []    # 待填表的一档结果（信号回投，界面线程填）

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        lay.addWidget(self._build_bench_box())
        lay.addStretch(1)   # 内容靠上，剩余留白

    # ── 容量压测：把 tools/bench_capacity.py 接进界面 ──
    # 重活（几十轮 × N 台取帧找图）走后台 QThread，绝不占住界面线程；
    # 算法一律复用 tools.bench_capacity（capacity_steps/demo_round_fn/run_bench/
    # recommend），界面只管输入、线程接线、结果表——两处各写一套必漂移。

    def _build_bench_box(self) -> QGroupBox:
        box, bl = titled_group("容量压测", _BENCH_HELP)

        # 第一行：实例范围 / 轮数 / 合成负载 demo / 开始 / 取消
        row = QHBoxLayout()
        row.setSpacing(6)
        self.ed_bench_range = QLineEdit("0-11")
        self.ed_bench_range.setPlaceholderText("如 0-11 或 0,2,5-9（升序不重叠）")
        self.ed_bench_range.setClearButtonEnabled(True)
        self.ed_bench_range.setToolTip(
            "要压测的实例号：支持 '0-11' 区间与 '0,2,5-9' 混写（升序不重叠）。\n"
            "实测需这些实例已启动；勾了「合成负载 demo」则忽略本栏、不碰模拟器。")
        row.addWidget(QLabel("实例范围："))
        row.addWidget(self.ed_bench_range, 1)
        row.addWidget(QLabel("轮数："))
        self.spn_bench_rounds = QSpinBox()
        self.spn_bench_rounds.setRange(1, 200)
        self.spn_bench_rounds.setValue(20)   # 与 CLI --rounds 默认同口径
        self.spn_bench_rounds.setKeyboardTracking(False)
        self.spn_bench_rounds.setToolTip("每档并发打多少轮（CLI 默认 20）。轮数越多结论越稳、越慢。")
        row.addWidget(self.spn_bench_rounds)
        self.chk_bench_demo = QCheckBox("合成负载 demo")
        self.chk_bench_demo.setToolTip(
            "勾上 = 用假负载只验工具链路，不碰模拟器、不出容量结论（等同命令行 --demo）。")
        row.addWidget(self.chk_bench_demo)
        self.btn_bench = QPushButton("开始压测")
        self.btn_bench.setToolTip(
            "按预算（挂机循环节拍）递增台数并发压测，实时填下面的结果表。\n"
            "实例没启动会被挡掉；跑的时候按钮变「压测中…」，可点「取消压测」。")
        self.btn_bench.clicked.connect(self._on_bench)
        row.addWidget(self.btn_bench)
        self.btn_bench_cancel = QPushButton("取消压测")
        self.btn_bench_cancel.setEnabled(False)
        self.btn_bench_cancel.clicked.connect(self._on_bench_cancel)
        row.addWidget(self.btn_bench_cancel)
        row.addStretch(1)
        bl.addLayout(row)

        # 状态/建议行：跑时进度、跑完给「建议最大负载 N 台」
        self.lbl_bench = QLabel("")
        self.lbl_bench.setWordWrap(True)
        bl.addWidget(self.lbl_bench)

        # 结果表：N 台 / 均值 / p95 / 超拍率 / 是否达标
        self.tbl_bench = QTableWidget(0, 5)
        self.tbl_bench.setHorizontalHeaderLabels(["N 台", "均值(s)", "p95(s)", "超拍率", "是否达标"])
        self.tbl_bench.verticalHeader().setVisible(False)
        self.tbl_bench.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_bench.setSelectionMode(QTableWidget.NoSelection)
        self.tbl_bench.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        # 5~6 行的结果表不该抢走整页高度：给个不高不矮的固定窗，留白交给末尾 stretch
        self.tbl_bench.setMaximumHeight(180)
        bl.addWidget(self.tbl_bench)

        # 存 json：落盘复用 core.json_store.atomic_dump（与 CLI --json 同一份结构）
        row2 = QHBoxLayout()
        row2.addStretch(1)
        self.btn_bench_save = QPushButton("保存结果 json…")
        self.btn_bench_save.setEnabled(False)
        self.btn_bench_save.setToolTip("把最近一次压测结果存成 json（结构同命令行 --json）")
        self.btn_bench_save.clicked.connect(self._on_bench_save)
        row2.addWidget(self.btn_bench_save)
        bl.addLayout(row2)
        return box

    def _bench_runner(self, steps, rounds, budget, is_cancelled, on_result):
        """后台线程真正跑的动作：现场拼 round_fn + 调 tools.bench_capacity.run_bench。

        单独成方法（而非塞进线程闭包）：冒烟可打桩这一步，绕开真取帧/找图，
        离屏只验「线程→信号→填表→收结论」这条界面链路，绝不碰模拟器。"""
        from tools import bench_capacity as bc   # 延迟导入：不给启动路径加工具模块依赖
        round_fn = bc.demo_round_fn() if self.chk_bench_demo.isChecked() \
            else bc.make_live_round(30)
        return bc.run_bench(steps, round_fn, rounds, budget,
                            on_result=on_result, cancelled=is_cancelled)

    def _on_bench(self):
        """「开始压测」：校验输入 → 建后台线程 → 禁用按钮、显示进度。"""
        from tools import bench_capacity as bc
        if self._bench_worker is not None:
            return   # 已有压测在跑（按钮本该禁用，防连点）
        demo = self.chk_bench_demo.isChecked()
        rounds = self.spn_bench_rounds.value()
        budget = float(config_manager.get_config().get_safe("general.frame_interval", 0.5))

        if demo:
            steps = bc.demo_steps()
        else:
            spec = self.ed_bench_range.text().strip()
            if not spec:
                self.lbl_bench.setText("⚠ 请填实例范围（如 0-11），或勾「合成负载 demo」。")
                return
            try:
                idx = bc.parse_indexes(spec)
            except SystemExit as e:
                # parse_indexes 的报错话术面向命令行（含 SystemExit 文本），转成人话
                self.lbl_bench.setText(f"⚠ 实例范围填错：{e}；应形如 0-11 或 0,2,5-9（升序不重叠）。")
                return
            # 前提校验：真压测要求实例已启动（跑批是磁盘/CPU 活，混进来算的是开机）
            try:
                running = {i.index for i in ldplayer.list_instances() if i.running}
            except Exception as e:
                self.lbl_bench.setText(f"⚠ 读实例列表失败：{e}")
                return
            not_running = sorted(set(idx) - running)
            if not_running:
                self.lbl_bench.setText(
                    f"⚠ 这些实例没启动：{not_running}。先「全部启动」到画面正常再压测，"
                    f"或勾「合成负载 demo」只验链路。")
                return
            steps = bc.capacity_steps(idx)

        self._bench_per_n = {}
        self._bench_pending = []
        self.tbl_bench.setRowCount(0)
        self.btn_bench_save.setEnabled(False)

        w = _BenchWorker(self._bench_runner, steps, rounds, budget, self)
        w.bench_progress.connect(self._on_bench_progress)
        w.bench_done.connect(self._on_bench_done)
        w.finished.connect(w.deleteLater)
        self._bench_worker = w
        self._set_bench_running(True)
        total = sum(len(s) for s in steps)
        self.lbl_bench.setText(
            f"压测中…（预算 {budget}s/拍，{rounds} 轮，合计 {total} 台次，"
            f"{'合成负载 demo 出不了容量结论' if demo else '实例需保持画面正常'}）")
        w.start()

    def _set_bench_running(self, running: bool):
        self.btn_bench.setEnabled(not running)
        self.btn_bench.setText("压测中…" if running else "开始压测")
        self.btn_bench_cancel.setEnabled(running)
        self.chk_bench_demo.setEnabled(not running)
        self.ed_bench_range.setEnabled(not running)
        self.spn_bench_rounds.setEnabled(not running)

    def _on_bench_cancel(self):
        """请求取消：后台每档/每轮开跑前问一次取消标志，最迟当前轮结束收手。"""
        if self._bench_worker is not None:
            self._bench_worker.cancel()
            self.btn_bench_cancel.setEnabled(False)
            self.lbl_bench.setText("正在取消…（当前一轮跑完即停）")

    def _on_bench_progress(self, res: dict):
        """后台每跑完一档回投：挂到待填表队列，界面线程统一落到结果表。"""
        self._bench_pending.append(res)

    def _flush_bench_rows(self, budget: float):
        """把回投的档位结果写进表（只碰 widget，必须界面线程调）。"""
        for res in self._bench_pending:
            if res.get("error"):
                self.lbl_bench.setText(f"⚠ 压测中止：{res['error']}")
                continue
            st = res["stats"]
            self._bench_per_n[res["N"]] = res
            self._append_bench_row(res["N"], st, budget)
        self._bench_pending.clear()

    def _append_bench_row(self, n: int, st: dict, budget: float):
        ok = st["over_ratio"] <= 0.05 and st["p95"] <= budget * 1.5
        vals = [str(n), f"{st['mean']:.3f}", f"{st['p95']:.3f}",
                f"{st['over_ratio']:.0%}", "✅ 达标" if ok else "⚠ 超拍"]
        r = self.tbl_bench.rowCount()
        self.tbl_bench.insertRow(r)
        for c, v in enumerate(vals):
            item = QTableWidgetItem(v)
            if c > 0:
                item.setTextAlignment(Qt.AlignCenter)
            self.tbl_bench.setItem(r, c, item)

    def _on_bench_done(self, per_n: dict, cancelled: bool):
        """线程收工：填完剩余行、算建议、放开按钮。"""
        # 收摊时 budget 与开跑同口径（跑中用户改了节拍也不影响本次判据）
        budget = float(config_manager.get_config().get_safe("general.frame_interval", 0.5))
        self._flush_bench_rows(budget)
        self._bench_worker = None
        self._set_bench_running(False)
        self.btn_bench_save.setEnabled(bool(self._bench_per_n))

        if not self._bench_per_n:
            if not cancelled:
                self.lbl_bench.setText("⚠ 没有可用的档位结果（请检查输入/实例状态）。")
            return
        from tools import bench_capacity as bc
        rec = bc.recommend(self._bench_per_n, budget)
        tail = "压测被取消，已跑档位结果如下。" if cancelled else ""
        if rec:
            self.lbl_bench.setText(
                f"{tail}建议最大负载 <b>{rec} 台</b>"
                f"（判据：超拍率≤5% 且 p95≤{budget * 1.5:.2f}s）。"
                f"{'（demo 合成负载，仅验链路、不是容量结论）' if self.chk_bench_demo.isChecked() else '建议值以下再留余量。'}")
        else:
            self.lbl_bench.setText(
                f"{tail}⚠ 全部档位都超拍，没测出稳定台数——检查模拟器状态、别同时跑 GUI 挂机。")

    def _on_bench_save(self):
        """存最近一次结果 json（结构同 CLI --json；落盘复用 atomic_dump）。"""
        if not self._bench_per_n:
            return
        budget = float(config_manager.get_config().get_safe("general.frame_interval", 0.5))
        from tools import bench_capacity as bc
        rec = bc.recommend(self._bench_per_n, budget)
        p, _ = QFileDialog.getSaveFileName(
            self, "保存压测结果", "bench_result.json", "JSON (*.json)")
        if not p:
            return
        if not p.lower().endswith(".json"):
            p += ".json"
        data = {"budget": budget, "rounds": self.spn_bench_rounds.value(),
                "demo": self.chk_bench_demo.isChecked(), "recommend": rec,
                "per_n": {str(n): v for n, v in sorted(self._bench_per_n.items())}}
        json_store.atomic_dump(p, data)
        self.lbl_bench.setText(f"已保存：{p}")