r"""
ui/status_window.py — 独立状态信息窗口（只读观察窗）

把散在主窗口两处（状态栏 / 实例表格）的信息集中到一个
可并排摆放的独立窗口：

    ┌──────────────────────────────────────────────┐
    │ 状态信息 ?                        [立即刷新]  │
    │ ● 实例 2 / 运行 1 / 就绪 1 / 挂机 1           │
    │ 雷电路径：D:\LDPlayer\ldconsole.exe            │
    │ 内存：本程序 120MB / 雷电 3.4GB                │
    │ 清单：登录 → 每日 → …                          │
    ├──────────────────────────────────────────────┤
    │ 实例总览表（一行一实例，含挂机进度）            │
    └──────────────────────────────────────────────┘

设计约束：
- 纯只读：不放任何启停/挂机按钮（操作仍走主窗口），坏了也不会误操作模拟器
- 数据全部「自己拉」：订阅主窗口 refreshed 广播后跟拍现查现画（list_instances /
  snapshot / accounts_by_instance / main_window.runtime_snapshot），一轮 ~3 秒；
  窗口不可见时闸门挡下，一拍都不画（旧版自立 2 秒 QTimer 与主窗口错峰重复轮询，
  已废）
- 每个数据源独立 try/except：任何一路挂了只影响自己那格显示「读取失败」，
  绝不让观察窗崩掉或卡住界面线程
- 重查询降频：psutil 全量扫进程每 3 拍才做一次（跟拍 ~9 秒一攒）；
  分辨率只读缓存/配置文件，绝不主动打 adb（那是 ~50ms/实例的慢查询）
"""

import os
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import capture, database, ldplayer, win_utils
from core.config_manager import get_config
from ui import theme, window_geometry
from ui.account_panel import WS_TEXT, fmt_elapsed, inst_status
from ui.help_badge import titled_header
from ui.icons import window_icon

# 雷电相关进程名关键字（绿化版/安装版/9 都覆盖；按小写包含匹配）
_LD_PROC_KEYS = ("ldplayer", "dnplayer", "ld9box", "ldconsole", "ldcsvc")

# 分组框/表格样式统一在全局主题 ui/theme.py，本窗口不再自带样式表。
# 只读观察窗的风格与主窗口完全一致，只有「状态灯」这类动态色用主题常量直赋


def _fmt_mb(nbytes: float) -> str:
    """字节 → 人类可读（GB/MB）"""
    mb = nbytes / 1024 / 1024
    return f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb:.0f}MB"


class StatusWindow(QWidget):
    """独立状态信息窗口。非模态，可与主界面并排；纯只读，跟主窗口刷新节拍同步。"""

    def __init__(self, main_window):
        super().__init__(main_window)
        # 独立窗口标志：QWidget 带 parent 默认嵌进父容器，必须置顶层（照 workbench 样板）
        self.setWindowFlags(self.windowFlags() | Qt.Window)
        self.setWindowTitle("状态信息")
        # 任务栏辨认图标：橙圆白字「态」（三窗各一色一字，见 ui.icons.window_icon）
        self.setWindowIcon(window_icon("态", "#D6772F"))
        self._mw = main_window          # 只读它的聚合状态（runtime_snapshot）
        self._cfg = get_config()
        self._tick = 0                  # 轮询计数（psutil 降频用）
        self._mem_text = "内存：—"      # psutil 结果缓存（降频期间沿用上次值）

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # ── 标题行：功能名 + ?徽章（项目约定：徽章贴名字后面）+ 立即刷新 ──
        head = titled_header(
            "状态信息",
            "只读观察窗：集中显示模拟器、挂机的实时状态。<br><br>"
            "跟着主窗口每 3 秒的刷新同步更新（内存占用约 9 秒查一次），"
            "窗口隐藏时停止刷新。<br>"
            "所有操作仍在主窗口进行，本窗口不提供任何按钮操作。",
            "winTitle")
        btn = QPushButton("立即刷新")
        btn.clicked.connect(self._poll)
        head.addWidget(btn)
        lay.addLayout(head)

        # ── 区块 1：系统概况 ──
        lay.addWidget(self._build_overview_box())

        # ── 区块 2：实例总览 + 挂机状态（一张表）──
        lay.addWidget(self._build_instance_box(), stretch=1)

        # ── 刷新节拍：不再自立周期定时器 ──
        # 旧版自己 2 秒一轮全量重画：和主窗口 3 秒大刷新读同一份 runtime_snapshot
        # 却各拍各的（两窗数字错峰跳），藏着还照跑。现在订阅主窗口 refreshed 广播：
        # "数据刚变新"的信号一到、且本窗可见才画，可见性闸门天然在槽里做。
        mw_refresh = getattr(self._mw, "refreshed", None)
        if mw_refresh is not None:
            mw_refresh.connect(self._on_mw_refreshed)

        # 还原上次位置大小（统一走 ui.window_geometry）
        window_geometry.restore(self, self._cfg, "ui.status_window_geometry", (860, 620))

        self._poll()   # 打开即有数据，不等第一个 tick

    # ═════════════════ 区块构建 ═════════════════
    def _build_overview_box(self) -> QGroupBox:
        box = QGroupBox("系统概况")
        lay = QVBoxLayout(box)
        lay.setSpacing(3)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        self.status_light = QLabel("●")
        self.status_light.setObjectName("statusLight")
        self.status_light.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        row1.addWidget(self.status_light)
        self.lbl_stat = QLabel("实例 —")
        self.lbl_stat.setObjectName("statStrong")
        row1.addWidget(self.lbl_stat)
        row1.addStretch(1)
        lay.addLayout(row1)

        self.lbl_ld_path = QLabel(f"雷电路径：{ldplayer.LD_CONSOLE}")
        self.lbl_ld_path.setObjectName("secondary")
        self.lbl_ld_path.setWordWrap(True)
        lay.addWidget(self.lbl_ld_path)

        self.lbl_mem = QLabel("内存：—")
        self.lbl_mem.setObjectName("secondary")
        lay.addWidget(self.lbl_mem)

        self.lbl_queue = QLabel("清单：—")
        self.lbl_queue.setObjectName("queueSummary")
        self.lbl_queue.setWordWrap(True)
        lay.addWidget(self.lbl_queue)
        return box

    def _build_instance_box(self) -> QGroupBox:
        box = QGroupBox("实例总览 · 挂机状态")
        lay = QVBoxLayout(box)
        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels([
            "序", "实例名", "状态", "PID", "分辨率",
            "账号", "挂机状态", "当前任务", "当前步骤", "已运行",
        ])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(8, QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table)
        return box

    # ═════════════════ 轮询刷新（每个数据源独立容错）═════════════════
    def _poll(self):
        self._tick += 1
        snap = self._safe(self._mw.runtime_snapshot, {})
        instances = self._safe(ldplayer.list_instances, [])
        self._render_overview(instances, snap)
        self._render_table(instances, snap)

    def _on_mw_refreshed(self):
        """主窗口刷完一轮：只在可见时跟拍（藏着的窗不画、不排队攒帧）。"""
        if self.isVisible():
            self._poll()

    @staticmethod
    def _safe(fn, default):
        """任何数据源挂了返回默认值——观察窗永远不崩、不卡"""
        try:
            return fn()
        except Exception:
            return default

    # ── 区块 1：系统概况 ──
    def _render_overview(self, instances, snap):
        n_run = sum(1 for i in instances if i.running)
        n_ready = sum(1 for i in instances if i.android_ready)
        n_work = len(snap.get("workers", {}))
        # 状态灯三态（与主窗口同一份选色，见 theme.lamp_color）
        self.status_light.setStyleSheet(f"color: {theme.lamp_color(n_work, n_run)};")
        self.lbl_stat.setText(
            f"实例 {len(instances)} / 运行 {n_run} / 安卓就绪 {n_ready} / 挂机 {n_work}")

        queue = snap.get("queue") or []
        if queue:
            from bot import template_deps
            # display_user 认 "flow:流程名" 记号 → 「自定义流程·流程名」
            names = [template_deps.display_user(t) for t in queue]
            self.lbl_queue.setText(f"清单：{' → '.join(names)}")
        else:
            self.lbl_queue.setText("清单：空")

        # psutil 降频：每 3 拍（跟主窗口 ~9 秒）扫一次，期间沿用上次结果
        if self._tick % 3 == 1:
            self._mem_text = self._scan_memory()
        self.lbl_mem.setText(self._mem_text)

    def _scan_memory(self) -> str:
        """本程序 RSS + 雷电相关进程内存合计（名字关键字匹配，见 _LD_PROC_KEYS）"""
        try:
            import psutil
            me = psutil.Process(os.getpid()).memory_info().rss
            ld_total = 0
            for p in psutil.process_iter(["name", "memory_info"]):
                try:
                    name = (p.info.get("name") or "").lower()
                    if any(k in name for k in _LD_PROC_KEYS):
                        mi = p.info.get("memory_info")
                        if mi is not None:
                            ld_total += mi.rss
                except Exception:
                    continue   # 单个进程读失败（刚退出等）跳过
            return f"内存：本程序 {_fmt_mb(me)} / 雷电相关 {_fmt_mb(ld_total)}"
        except Exception as e:
            return f"内存：读取失败（{e}）"

    # ── 区块 2：实例表 ──
    def _render_table(self, instances, snap):
        workers = snap.get("workers", {})
        progress = snap.get("progress", {})
        accounts = self._safe(database.accounts_by_instance, {})

        self.table.setRowCount(len(instances))
        for row, inst in enumerate(instances):
            status, color = inst_status(inst)

            acct = accounts.get(inst.index) or {}
            acct_text = "— "
            if acct:
                name = acct.get("name") or acct.get("account") or ""
                server = acct.get("server") or ""
                acct_text = f"{name}·{server}" if server else name

            ws = workers.get(inst.index, "idle")
            pr = progress.get(inst.index) or {}
            elapsed = fmt_elapsed(time.time() - pr["since"]) if pr.get("since") else "—"

            cells = [
                # 「序」与主表格「序号」列同口径：直通雷电编号（0=母体）
                str(inst.index), inst.title, status,
                str(inst.pid) if inst.pid > 0 else "—",
                self._resolution_text(inst), acct_text,
                WS_TEXT.get(ws, ws),
                pr.get("task", "—"), pr.get("step", "—"), elapsed,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 2:
                    item.setForeground(QColor(color))   # 状态列着色：就绪绿/启动中橙/停止灰
                self.table.setItem(row, col, item)

    def _resolution_text(self, inst) -> str:
        """
        分辨率列：运行中读 adb 缓存值（android_size 自带缓存，不会重复打 adb），
        停止读实例配置文件（<1ms）。绝不用 current_resolution——它 force=True
        每实例 ~50ms adb，2 秒轮询下太重。
        """
        if inst.running:
            size = self._safe(lambda: ldplayer.android_size(inst.index), None)
            if size:
                return f"{size[0]}x{size[1]}"
            return "查询中…"
        cfg = self._safe(lambda: ldplayer.get_config_resolution(inst.index), None)
        if cfg:
            return f"{cfg[0]}x{cfg[1]}@{cfg[2]}"
        return "—"

    # ═════════════════ 生命周期 ═════════════════
    def hideEvent(self, event):
        # 不用再"停定时器省开销"：刷新跟主窗口广播走，_on_mw_refreshed 里
        # isVisible() 闸门天然把藏着的窗挡下，一拍都不多画
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # 有主人的从属窗默认不进任务栏，最小化缩成迷你标题栏——重补按钮；
        # 再补专属【窗口级】任务栏身份，否则与主窗同组被合并成一个按钮、
        # 点按钮只弹预览不最小化（详见 core.win_utils.set_window_app_id）
        hwnd = int(self.winId())
        win_utils.ensure_taskbar_button(hwnd)
        win_utils.set_window_app_id(hwnd, win_utils.APPID_STATUS)
        self._poll()                # 重新露脸立刻画一帧，不等主窗口下一拍

    def save_geometry(self):
        """
        存位置大小。不放 closeEvent 里：本窗口是主窗口的子顶层窗口，
        关程序时随父销毁、不走 closeEvent（实测主窗关闭 0.1s 即退出）。
        改由 MainWindow.closeEvent 显式调用（确定性保存）。
        """
        window_geometry.save(self, self._cfg, "ui.status_window_geometry")

    def closeEvent(self, event):
        self.save_geometry()   # 单独关掉状态窗时也要记住位置
        super().closeEvent(event)
