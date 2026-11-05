"""
ui/main_window.py — 主窗口（第 5 步）

结构：
    ┌────────┬──────────────────────────┬─────────┐
    │        │ [账号管理|资产管理] 页签栏 │ 模拟器   │
    │ 模板库 │ ├ 表格（或资产总览表）    │ 挂机     │
    │（最左， ├───┬────────┬─────────────┤ 工具     │
    │ 可滚动） │清单│ 菜单栏  │ 日志        │（操作竖列）│
    │        │   │+内容页  │             │         │
    ├────────┴────────────┴─────────────┴─────────┤
    │ 状态栏：● 状态灯 │ 雷电路径 │ 实例统计 │ 当前任务 │
    └─────────────────────────────────────────────┘

本步学到的知识点：
1. QMainWindow + 布局：QMainWindow 是"主窗口骨架"（有工具栏/状态栏区域），
   内容区用 QVBoxLayout 竖着摞控件，addStretch 让表格吃掉多余空间
2. QTimer：每隔几秒触发一次——"周期刷新"就是靠它
3. QThread 初体验：启动模拟器要几十秒，放主线程会让界面卡死。
   BatchWorker 在后台线程顺序干活，每干一步发一个信号回界面打日志
   （信号跨线程自动排队投递到 GUI 线程，天然线程安全）
4. 需求 6 落地：窗口尺寸位置存进 config.json，下次启动还原
5. 布局重构：任务选择从「工具栏下拉框 + 底部套娃菜单的树」两处合并成
   最左侧一条可滚动列表（任务与流程分开后 = 任务栏列表 TaskSidebar，
   列任务，双击入任务清单；流程与流程分类只在工作台管理）。
   选中状态只存在侧栏一处，无翻页联动
6. 布局重构 2：顶部工具栏取消，批量操作改成右侧竖列大按钮（按"管模拟器/
   管挂机/管工具"分组），全局状态（状态灯/雷电路径/实例统计/当前任务）
   沉到 QMainWindow 自带的 statusBar。顶部空间让给表格
7. 任务清单（ui/queue_panel.py）：清单项 = 流程名（与最左侧列表同一套流程），
   点「＋」弹流程菜单入队，清单从上到下即「全部挂机」的执行顺序；交给
   AccountWorker 前逐条冠上 "flow:流程名" 记号（存键沿用 task_order，
   内容已是流程名）。上一版的「库→任务绑定换算」（library_bindings）随
   侧栏直接列流程一并删除。双击左侧流程 = 加入本清单的快捷手势，
   与「＋」同一条路
"""

import threading
import time
from functools import partial

import shiboken6  # PySide6 自带：查 C++ 对象是否已被回收（防"尸体指针"调用）
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QLabel,
    QMainWindow,
    QMenuBar,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)

from bot import template_deps
from bot.worker import AccountWorker
from core import capture, database, ldplayer, run_log, win_utils
from core.config_manager import get_config
from ui import theme, window_geometry
from ui.confirm import confirm
from ui.account_panel import AccountPanel, acct_display
from ui.asset_panel import AssetPanel
from ui.category_sidebar import TaskSidebar
from ui.feature_panel import FeaturePanel
from ui.test_panel import TestPanel
from ui.float_ball import FloatBall, recall_window
from ui.log_window import LogWindow
from ui.queue_panel import TaskQueuePanel

# 底部中间列的通用菜单栏注册表：每项 = (标识, 按钮文字, 悬停提示)。
# 以后新增功能在这里加一行 + 在 _register_menu_pages 里注册内容页即可。
MENU_ENTRIES = [
    ("feature_settings", "功能设置", "全局功能参数（模拟器分辨率等）"),
    ("test_settings", "测试设置", "测试类功能（容量压测等）"),
]


def _launch_gapped(gap: float, index: int, cancelled=None):
    """间隔多开的下达作业：先睡 gap 秒再 launch（在批次后台线程里睡，界面不卡）。
    分小步睡并查取消标志——「全部关闭」接管时最迟 0.2s 内就能掐掉等待。"""
    t0 = time.time()
    while time.time() - t0 < gap:
        if cancelled is not None and cancelled():
            return "已取消，跳过"
        time.sleep(min(0.2, gap - (time.time() - t0)))
    return ldplayer.launch(index)


class BatchWorker(QThread):
    """
    后台线程：按顺序执行一批操作。jobs = [(描述, 函数, 参数...), ...]

    两个模式：
    - 普通模式：跑完全部 jobs，中间可被 cancel() 打断（当前步骤结束即停）
    - 抢占模式（preempt=True）：完成剩余 jobs 后置取消标志——为"关闭"类操作
      设计的：用户点关闭时若已有批次在跑（如启动批次），先做完自己的，
      做完后把旧批次取消掉，不让它继续误操作模拟器。
    """

    job_done = Signal(str)   # 每个步骤的日志（线程安全地回到界面线程）
    all_done = Signal()      # 全部完成

    # 跑批线程 id → 该线程当前批次：wait_until_ready 这类轮询作业靠它查
    # "我这一批"是否被取消。旧版走 MainWindow._cancel_check 读 self._worker
    # （界面指针）——抢占换指针后旧批次作业看的是新批次的脸色，点取消无感。
    _by_thread = {}

    def __init__(self, jobs, parent=None, preempt: bool = False):
        super().__init__(parent)
        self.jobs = jobs
        self.preempt = preempt
        self._cancel = threading.Event()  # 取消标志（线程安全）
        self._target = None               # 抢占模式下：跑完这本批次要取消的旧批次

    def cancel(self):
        """请求取消：当前步骤结束后停止，不再开始新步骤"""
        self._cancel.set()

    def is_cancelled(self) -> bool:
        """取消标志的公开只读接口（stop_check 回调等外部判断用，不碰私有成员）"""
        return self._cancel.is_set()

    def set_preempt_target(self, older: "BatchWorker"):
        """接管对象：本批次跑完后取消它（它排你前面先启动，是更早的操作）"""
        self._target = older

    def run(self):
        """线程入口：QThread 的 run() 在 start() 后于新线程执行"""
        # 登记"本线程正在跑哪批"：作业内的轮询回调（wait 就绪等）据此查自己批次的取消标志
        BatchWorker._by_thread[threading.get_ident()] = self
        try:
            for desc, fn, *args in self.jobs:
                if self._cancel.is_set():
                    self.job_done.emit("批次已取消，剩余步骤跳过")
                    break
                self.job_done.emit(f"开始：{desc}")
                t0 = time.time()
                try:
                    ok = fn(*args)
                    # 步骤耗时（秒）：新批次加了耗时日志，启动慢一眼就能看出
                    # 卡在哪一步，不用再猜是 ldconsole 还是模拟器开机
                    cost = time.time() - t0
                    # 返回字符串的作业（如 ensure_resolution）自带状态描述，直接显示——
                    # 否则「设定失败」这种非空字符串会被当真值报成「成功」（实测踩坑）
                    if isinstance(ok, str):
                        self.job_done.emit(f"{desc}：{ok}（{cost:.1f}s）")
                    else:
                        self.job_done.emit(f"{desc}：{'成功' if ok else '失败'}（{cost:.1f}s）")
                except Exception as e:  # 后台线程不能崩，异常都记日志
                    self.job_done.emit(f"{desc}：异常 {e}")
            # isValid 防尸体：接管对象可能已跑完被 deleteLater 回收，
            # 对它调 isRunning() 会 RuntimeError 把 run() 炸掉（同 _forget_worker 的病根）
            if (self.preempt and self._target is not None
                    and shiboken6.isValid(self._target) and self._target.isRunning()):
                self.job_done.emit("操作接管：取消更早的批次")
                self._target.cancel()
        finally:
            BatchWorker._by_thread.pop(threading.get_ident(), None)
            # finally 保证无论发生什么，界面一定收到"结束"信号，
            # 按钮不会永久卡在禁用状态（第 5 步踩坑修复）
            self.all_done.emit()


class MainWindow(QMainWindow):
    # 多开台数逐台进度（补台/删台共用；批次线程 emit → Qt 队列连接自动回投
    # 界面线程打日志，与 BatchWorker.job_done 同范式；后台作业不许直接碰 widget）
    clone_progress = Signal(str)
    # 删台收口（批次线程 emit → 界面线程清理）：被删实例的运行时残影
    # （完成度/进度列）不跟着台数消失，补回来的新机挂同号会顶到旧"已完成"
    instances_removed = Signal(list)
    # 3 秒大刷新收尾广播：状态观察窗订阅这一发跟拍渲染（自带可见性闸门），
    # 不再自立 2 秒定时器对着同一份 runtime_snapshot 重复轮询
    refreshed = Signal()

    def __init__(self):
        super().__init__()
        self.cfg = get_config()
        self.setWindowTitle("EmuHive 多开挂机框架")
        self._worker = None  # 必须持有 worker 引用，否则会被垃圾回收导致信号丢失
        # 批次看门狗全局只此一只（旧版每开一批新建一只，旧的那只仍 armed：
        # 8 分钟后假报"卡死"解锁界面并清 _worker，真批次还在跑 → 用户能再开
        # 一批并发操作模拟器。单只 + 每次 start 重置倒计时 = 永远只盯最新批次）
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_watchdog_timeout)

        # 就绪观察器（2026-09-25）：等就绪从批次线程挪进界面 1s 定时器——
        # 批次只剩"下达"（秒级），按钮不再被每台 150 秒的等待占着；
        # 读的是实例快照缓存（零耗时），主线程跑得起。
        # {实例号: {"deadline": float, "seen_hwnd": bool, "arranged": bool}}
        self._ready_pending: dict[int, dict] = {}
        # 后台摆位进行中的实例号（一实例同时在飞最多一条，见 _arrange_window_bg）
        self._arranging: set[int] = set()
        self._ready_timer = QTimer(self)
        self._ready_timer.setInterval(1000)
        self._ready_timer.timeout.connect(self._tick_ready_watch)

        # ── 操作按钮（竖列在 _build_ops_column 里组装，这里只建控件）──
        self.btn_start_all = QPushButton("全部启动")
        self.btn_quit_all = QPushButton("全部关闭")
        self.btn_refresh = QPushButton("刷新")
        self.btn_cancel = QPushButton("取消批次")
        # 固定占位：一直显示、无批次时禁用变灰。旧版用 setVisible(False) 收起，
        # 批次一起来它就冒出来，把它下方的「工作台」等按钮整体顶下一格，鼠标
        # 按原位置点下去就点空了。禁用态占位不缩，按钮位置纹丝不动（Windows 惯例）。
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setToolTip("批量操作执行时才能点：中断当前批次（当前步骤结束后生效）")
        self.btn_workbench = QPushButton("工作台")
        self.btn_status = QPushButton("状态窗口")
        self.btn_log = QPushButton("运行日志")
        # 导出账号：点击弹菜单选含不含密码（拖 TXT 导入已有，导出是缺的那半）
        self.btn_export_accounts = QPushButton("导出账号")
        _exp_menu = QMenu(self.btn_export_accounts)
        _exp_menu.addAction("导出(含密码)", lambda: self._export_accounts(True))
        _exp_menu.addAction("导出(不含密码)", lambda: self._export_accounts(False))
        self.btn_export_accounts.setMenu(_exp_menu)
        self.btn_export_accounts.setToolTip(
            "把账号表全部导出成 TXT（列名与表格一致，拖回列表框即可导入还原）。\n"
            "含密码 = 明文存档，方便换机整迁；不含密码 = 脱敏，方便分享名单。\n"
            "⚠ 含密码的文件等同账号密码清单，别放网盘同步目录。")
        self.btn_worker_all = QPushButton("全部挂机")
        self.btn_pause_all = QPushButton("全部暂停")
        self.btn_resume_all = QPushButton("全部恢复")
        self.btn_stop_all = QPushButton("全部停止")
        # 主操作按钮（全部启动 / 全部挂机）用主题强调色；全部停止用危险色
        self.btn_start_all.setObjectName("primary")
        self.btn_worker_all.setObjectName("btnAccent")
        self.btn_stop_all.setObjectName("dangerBtn")  # 警示色：停止是最"重手"的操作

        # ── 状态栏（全局状态沉到底部，顶部不再放任何东西）──
        bar = self.statusBar()
        self.status_light = QLabel("●")
        self.status_light.setObjectName("statusLight")
        self.status_light.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        bar.addWidget(self.status_light)
        self.lbl_ld_path = QLabel("")          # 雷电路径，构造末尾填
        bar.addWidget(self.lbl_ld_path, 1)     # stretch=1：路径吃掉多余宽度
        self.lbl_inst_stat = QLabel("实例 —")   # 实例统计：总数/运行中
        bar.addWidget(self.lbl_inst_stat)
        # 清单摘要：「全部挂机」按清单执行，状态栏就显示清单本身，所见即所跑；
        # 双击左侧流程 = 加入这份清单，所以双击后这里会立刻跟着变
        self.lbl_queue = QLabel("任务清单：空")
        self.lbl_queue.setObjectName("queueSummary")
        bar.addPermanentWidget(self.lbl_queue)

        # ── 左侧：任务栏（任务平铺，可滚动；双击任务 = 加入任务清单）──
        self.cat_sidebar = TaskSidebar()

        # ── 中间：上表格 / 下（任务清单 + 功能设置页）──
        # 表格外面套一条页签栏（2026-09-21 用户方案）：账号管理 = 超级列表框，
        # 资产管理 = 资产总览表。页签壳 = 两颗胶囊 QToolButton（autoExclusive
        # 互斥选中）——不用 QMenuBar 借外观：实测它的样式表渲染不认 :checked
        # 也不吃 action 字体，选中态根本亮不起来（QToolButton 两者都行）。
        # 翻页不动 panel 本体：右键菜单/拖放导入/布局记忆全在原位。
        self.panel = AccountPanel()
        self.asset_panel = AssetPanel()
        self._mid_stack = QStackedWidget()
        self._mid_stack.addWidget(self.panel)
        self._mid_stack.addWidget(self.asset_panel)
        self._mid_tabs = QWidget()
        tabs_lay = QHBoxLayout(self._mid_tabs)
        tabs_lay.setContentsMargins(2, 0, 0, 0)
        tabs_lay.setSpacing(4)
        self._mid_tab_actions = {}
        for eid, label in (("accounts", "账号管理"), ("assets", "资产管理")):
            btn = QToolButton(text=label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setProperty("tabPill", True)   # 主题按此属性上胶囊样式
            btn.clicked.connect(
                lambda _=False, e=eid: self.select_mid_tab(e))
            tabs_lay.addWidget(btn)
            self._mid_tab_actions[eid] = btn
        tabs_lay.addStretch(1)
        self._mid_tabs.setToolTip(
            "点字切换中间表格：\n"
            "账号管理 = 看每个账号的挂机状态；\n"
            "资产管理 = 看每个账号有多少金币、钻石")
        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(2)
        middle_layout.addWidget(self._mid_tabs)
        middle_layout.addWidget(self._mid_stack, stretch=1)  # 表格吃掉多余空间

        # 底部用 QSplitter：清单/设置两列（宽度比例可拖且存进配置）。
        # 日志输出栏已搬去独立窗口（LogWindow），不再占底部空间
        self.bottom_splitter = QSplitter()
        self.bottom_splitter.setChildrenCollapsible(False)  # 拖到极窄也不让面板被折没
        self.queue_panel = TaskQueuePanel()   # 任务清单：全部挂机按它换算成任务链顺序跑
        self.bottom_splitter.addWidget(self.queue_panel)
        self.bottom_splitter.addWidget(self._build_menu_column())
        # 统一日志入口：self.log_panel 名字不变（全文件 20+ 调用点都认它），
        # 只是 .log() 的显示位置换成了独立窗口
        self.log_panel = LogWindow(self)
        # 悬浮召回球：任一被跟踪顶层窗最小化时出现（见 ui/float_ball.py）。
        # 主窗/日志窗当场挂；工作台/状态窗是惰建的，在各自 open_* 里挂
        self._float_ball = FloatBall()
        self._float_ball.attach(self)
        self._float_ball.attach(self.log_panel)
        self.bottom_splitter.setStretchFactor(0, 0)  # 清单列窄而固定倾向
        self.bottom_splitter.setStretchFactor(1, 1)
        middle_layout.addWidget(self.bottom_splitter, stretch=1)

        # ── 右侧：操作竖列（只跟表格同高，底部日志/设置横跨全宽）──
        ops = self._build_ops_column()

        # 中层横向：中间区 + 操作列
        self.mid_splitter = QSplitter()
        self.mid_splitter.setChildrenCollapsible(False)
        self.mid_splitter.addWidget(middle)
        self.mid_splitter.addWidget(ops)
        self.mid_splitter.setStretchFactor(0, 1)
        self.mid_splitter.setStretchFactor(1, 0)

        # 外层左右：任务栏 | 中层（任务栏宽度可拖，侧栏自身设了最小/最大宽）
        self.root_splitter = QSplitter()
        self.root_splitter.setChildrenCollapsible(False)
        self.root_splitter.addWidget(self.cat_sidebar)
        self.root_splitter.addWidget(self.mid_splitter)
        self.root_splitter.setStretchFactor(0, 0)
        self.root_splitter.setStretchFactor(1, 1)
        self.setCentralWidget(self.root_splitter)

        # ── 信号连接 ──
        self.btn_start_all.clicked.connect(self.batch_start_all)
        self.btn_quit_all.clicked.connect(self.batch_quit_all)
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_cancel.clicked.connect(self.cancel_batch)
        self.btn_workbench.clicked.connect(self.open_workbench)
        self.btn_status.clicked.connect(self.open_status_window)
        self.btn_log.clicked.connect(self.open_log_window)
        self.btn_worker_all.clicked.connect(self.batch_worker_start)
        self.btn_pause_all.clicked.connect(lambda: self.batch_worker("pause"))
        self.btn_resume_all.clicked.connect(lambda: self.batch_worker("resume"))
        self.btn_stop_all.clicked.connect(lambda: self.batch_worker("stop"))
        self.panel.operation_requested.connect(self.on_row_op)
        self.panel.worker_command.connect(self.on_worker_command)
        self.panel.account_op.connect(self.on_account_op)
        # 双击原地改档案（面板只发意图，写库在这）
        self.panel.account_field_edited.connect(self.on_account_field_edited)
        # 拖入账号文件 → 后台批次导入（几百行 = 几百次写库，主线程做会卡界面）
        self.panel.accounts_file_dropped.connect(self.on_accounts_file_dropped)
        # 侧栏双击任务 → 把任务对应的流程加进本清单（与「＋」同一条路）
        self.cat_sidebar.task_double_clicked.connect(self.on_flow_double_clicked)
        # 任务栏右键「删除任务」（无确认，直接删）→ 删任务（侧栏自己删了并 emit 任务名），这里清清单引用
        self.cat_sidebar.task_deleted.connect(self._on_task_deleted)
        # 清单任何变化（入队/排序/清空/导入配置）→ 状态栏摘要跟着更新
        self.queue_panel.queue_changed.connect(lambda _q: self.refresh_queue_label())
        # 清单双击「查看流程」→ 开工作台并切到流程编排页选中该流程
        self.queue_panel.view_flow_requested.connect(self._on_view_flow)
        self.refresh_queue_label()
        # 多开台数逐台进度（批次线程 emit，Qt 队列连接自动回投界面线程）→ 日志
        self.clone_progress.connect(lambda msg: self.log_panel.log(msg))
        self.instances_removed.connect(self._on_instances_removed)

        # 统一工作台（模板采集 + 流程编排；持引用防 GC，非模态可与主界面并排）
        self._workbench = None
        # 状态信息窗口（同样单例缓存 + 非模态）
        self._status_window = None
        # 挂机线程注册表：{实例index: AccountWorker}
        self._workers: dict[int, AccountWorker] = {}
        # 收摊中的线程保活集合：stopped/error 信号到达主线程时，worker 的
        # run() 可能还差最后几行没返回（信号是在 finally 里发的）。此时若
        # 丢掉最后一份引用，QThread 包装器被 GC 撞上"线程仍在跑"，Qt 直接
        # abort 整个进程。finished（run 真返回后才发）再从这里移除。
        self._finishing: set = set()
        # 表格进度列数据源：{实例index: {"task": 显示名, "step": 步骤名, "since": 时间戳}}
        self._progress: dict[int, dict] = {}
        # 任务链结局（"完成度"列数据源）：{实例index: True=整条链跑完 / False=中途失败或被停}
        # 单独存一份：线程退出时 _progress 会被清空，而完成度要留到下一轮才看得见
        self._chain_done: dict[int, bool] = {}
        # 自动重挂台账：{实例index: [重挂时刻...]}（1 小时滑窗限 2 次，见
        # _maybe_auto_restart）。只对 error 生效——用户手动停止走 stopped，永不重挂。
        self._restart_hist: dict[int, list] = {}
        # 开机清一次过期日志/留证截图（7×24 磁盘保留期）。延后到事件循环
        # 起来再做：此刻 log_panel/状态栏还没建好，清理结果得有地方说话。
        QTimer.singleShot(1500, self._cleanup_logs)

        # ── 周期刷新 + 启动时立即刷一次 ──
        # 先拉起后台实例快照线程：此后界面所有"数实例"都读缓存，主线程
        # 不再同步等 ldconsole——开机风暴期"全部启动"卡死窗口的根治点
        # （core/ldplayer.py 快照缓存注释）。
        ldplayer.start_instance_poller()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(3000)  # 每 3 秒
        # 步骤合并闸：on_worker_step 突发多次只触发一次 refresh（单次触发式，
        # 见 on_worker_step 注释）。parent=self，随主窗口析构自然回收。
        self._step_refresh_timer = QTimer(self)
        self._step_refresh_timer.setSingleShot(True)
        self._step_refresh_timer.timeout.connect(self.refresh)
        self.refresh()
        # 2026-10-11：取帧主路 = adb 单次快照（即拍即得、无常驻），无需开跑前预热
        # 任何取帧链（scrcpy 后端已整体移除）。

        # ── 恢复上次的窗口位置大小（需求 6）──
        # 最小尺寸兜底：布局再多控件也不至于被压到变形；
        # 默认尺寸放大到 1320x860（1080p 屏左侧大区），右侧留出 ~560px
        # 给自动贴右居中的模拟器窗口，两边互不遮挡，方便边操作边观察
        self.setMinimumSize(1024, 700)
        window_geometry.restore(self, self.cfg, "ui.window_geometry", (1320, 860))

        # 超级列表框布局还原（列宽/列序/滚动位置，关窗时存的；无存档静默用默认）
        self.panel.restore_layout(self.cfg.get("ui.table_layout"))
        # 资产总览表：起始页固定「账号管理」+ 列宽按列名还原
        self.select_mid_tab("accounts")
        self.asset_panel.restore_layout(self.cfg.get("ui.asset_layout"))

        # 恢复三块 splitter 的拖动比例（左右 / 中右 / 底部两列）
        # 底部列数从 3 变 2（日志搬去独立窗口）：老配置存的是 3 个值，
        # len 对不上自动跳过，用默认比例——不报错也不误还原
        for attr, key, n in (("root_splitter", "ui.root_splitter", 2),
                             ("mid_splitter", "ui.mid_splitter", 2),
                             ("bottom_splitter", "ui.bottom_splitter", 2)):
            sizes = self.cfg.get(key)
            sp = getattr(self, attr)
            if isinstance(sizes, list) and len(sizes) == n:
                sp.setSizes([int(s) for s in sizes])
            # 死白兜底：第0列（任务栏132/任务清单150）是固定宽控件，但 Qt 首启
            # 初始布局会把全部富余塞给第0列（stretch 系数只管 resize 不管首排）——
            # 表现为「解压第一次打开控件被撑大留白，拖一下边框才缩回」。老配置
            # 可能也存着撑大期的比例，一并钳：第0列槽位超过自身固定宽就压回去，
            # 富余让给第1列（功能设置/表格区本来就是 stretch 侧，多宽都吃得下）。
            c0 = sp.widget(0)
            if c0.minimumWidth() == c0.maximumWidth() > 0:
                sp.setSizes([c0.maximumWidth(), sp.width()])

        self.lbl_ld_path.setText(f"雷电路径：{ldplayer.LD_CONSOLE}")
        self.log_panel.log(f"雷电路径：{ldplayer.LD_CONSOLE}")
        # 日志窗不再启动自弹：以前日志常驻主界面底部、不开等于没有才自动弹，
        # 搬成独立窗口后它是旁路视图。要用点「运行日志」召回（主界面/工作台都有）。
        # 不弹也不丢日志：LogWindow 本体一开播就建好了，日志一直往缓冲里攒，
        # 什么时候开窗什么时候全在。

    # ── 右侧操作竖列 ──
    def _build_ops_column(self) -> QWidget:
        """
        批量操作竖列：按"管模拟器 / 管挂机 / 管工具"分三组竖排大按钮。
        竖排比原来工具栏横排的好处：按钮够大够醒目、分组一目了然、
        顶部空间让给表格（表格是这界面里信息密度最高的东西）。
        """
        col = QWidget()
        col.setObjectName("opsColumn")
        col.setFixedWidth(128)
        lay = QVBoxLayout(col)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(8)

        def group(title, *btns):
            box = QGroupBox(title)
            box.setObjectName("opsGroup")
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(6, 10, 6, 6)
            box_layout.setSpacing(8)
            for b in btns:
                b.setMinimumHeight(34)   # 大按钮：戴手套也点得中
                box_layout.addWidget(b)
            return box

        lay.addWidget(group("模拟器", self.btn_start_all, self.btn_quit_all,
                            self.btn_refresh, self.btn_cancel))
        lay.addWidget(group("挂机", self.btn_worker_all, self.btn_pause_all,
                            self.btn_resume_all, self.btn_stop_all))
        lay.addWidget(group("工具", self.btn_workbench, self.btn_status, self.btn_log,
                            self.btn_export_accounts))
        lay.addStretch(1)   # 按钮靠上排，剩余空间留白
        return col

    # ── 底部菜单列：横式菜单栏 + 内容页 ──
    def _build_menu_column(self) -> QWidget:
        """
        菜单列 = 顶部一条横式菜单栏（QMenuBar 壳）+ 下面的内容页
        （QStackedWidget，每条目一页）。

        菜单栏是通用容器：条目来自 MENU_ENTRIES 注册表，以后新增功能 =
        注册表加一行 + _register_menu_pages 注册内容页，不动布局代码。

        条目是无子菜单的 QAction：点一下直接触发（翻内容页），不弹下拉——
        菜单栏在这里只借外观，行为还是页签切换。
        """
        col = QWidget()
        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        # ── 横式菜单栏（条目排他高亮，点击翻页）──
        menu_bar = QMenuBar()
        menu_bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._menu_actions: dict[str, QAction] = {}
        for entry_id, label, tip in MENU_ENTRIES:
            act = QAction(label, self)
            act.setToolTip(tip)
            act.setCheckable(True)
            act.triggered.connect(lambda _checked=False, eid=entry_id: self.select_menu_entry(eid))
            menu_bar.addAction(act)
            self._menu_actions[entry_id] = act
        lay.addWidget(menu_bar)

        # ── 内容页：每条目一页，注册表驱动 ──
        self._menu_stack = QStackedWidget()
        self._menu_pages: dict[str, QWidget] = {}
        self._register_menu_pages()
        lay.addWidget(self._menu_stack, stretch=1)

        # 默认选中第一个条目（此时信号已连好，直接 select 即可）
        if MENU_ENTRIES:
            self.select_menu_entry(MENU_ENTRIES[0][0])
        return col

    def _register_menu_pages(self):
        """注册各菜单条目的内容页。新增功能在这里加一页。"""
        # 「功能设置」页：全局参数（模拟器分辨率等），应用信号转后台批次
        self.feature_panel = FeaturePanel()
        self.feature_panel.apply_requested.connect(self.on_resolution_apply)
        self.feature_panel.clone_requested.connect(self.on_clone_apply)
        self._menu_pages["feature_settings"] = self.feature_panel
        # 「测试设置」页：测试类功能（容量压测）。2026-10-10 从功能设置拆出——
        # 功能设置太长挤压上方账号管理区，测试类功能单开一页。
        self.test_panel = TestPanel()
        self._menu_pages["test_settings"] = self.test_panel
        for page in self._menu_pages.values():
            self._menu_stack.addWidget(page)

    # ── 菜单栏条目切换 ──
    def select_mid_tab(self, tab_id: str):
        """中间列表框页签翻页（账号管理/资产管理）+ 高亮页签条目。
        每次启动固定从「账号管理」开始（用户约定：关窗抓布局不抓页签）"""
        idx = {"accounts": 0, "assets": 1}.get(tab_id)
        if idx is None:
            return
        self._mid_stack.setCurrentIndex(idx)
        for eid, btn in self._mid_tab_actions.items():
            btn.setChecked(eid == tab_id)

    def select_menu_entry(self, entry_id: str):
        """翻到指定条目的内容页 + 高亮菜单栏条目；未注册条目忽略"""
        page = self._menu_pages.get(entry_id)
        if page is None:
            return
        self._menu_stack.setCurrentWidget(page)
        for eid, act in self._menu_actions.items():
            act.setChecked(eid == entry_id)

    # ── 任务栏联动 ──
    def on_flow_double_clicked(self, fname: str):
        """
        侧栏双击任务 = 把该任务对应的流程加进底部「任务清单」（追加到末尾，
        允许重复）。与清单「＋」按钮同一条路（queue_panel.add_flow 自带
        "流程还在不在"的第二道闸），双击只是更快的入队手势；真正开跑仍是
        点「全部挂机」。
        """
        self._add_flow_to_queue(fname, "双击")

    def _on_flow_add_queue(self, fname: str):
        """工作台流程「加入任务栏」→ 生成一条任务挂进任务栏（可重复）"""
        before = len(self.cat_sidebar)
        self.cat_sidebar.add_task(fname)
        if len(self.cat_sidebar) > before:
            self.log_panel.log(
                f"「{fname}」→ 已加入任务栏（双击任务可再入任务清单）")
        else:
            # add_task 静默拒绝 = 流程刚好被删了（工作台与主界面并排开时会遇到）
            self.log_panel.log(f"「{fname}」加不进任务栏：流程已不存在", "WARN")

    def _add_flow_to_queue(self, fname: str, from_where: str):
        before = len(self.queue_panel.current_queue())
        self.queue_panel.add_flow(fname)
        if len(self.queue_panel.current_queue()) > before:
            self.log_panel.log(
                f"{from_where}「{fname}」→ 已加入任务清单（任务：flow:{fname}），"
                "点「全部挂机」按清单顺序执行")
        else:
            # add_flow 静默拒绝 = 这条流程刚好被删了（清单与工作台并排开时会遇到）
            self.log_panel.log(f"「{fname}」加不进任务清单：流程已不存在", "WARN")

    def _on_task_deleted(self, fname: str):
        """任务栏删掉了一条任务：清单里同名条目一并清掉（侧栏只删任务本身）"""
        n = self.queue_panel.remove_all_flows(fname)
        self.log_panel.log(
            f"任务「{fname}」已从任务栏删除"
            + (f"；任务清单里同名项也一并清掉 ×{n}" if n else ""))

    def refresh_queue_label(self):
        """状态栏清单摘要：清单装的是流程名。前 3 项 + 省略号，太长会挤爆状态栏。"""
        queue = self.queue_panel.current_queue()
        if not queue:
            self.lbl_queue.setText("任务清单：空（点「＋」加流程）")
            return
        shown = queue[:3]
        more = f" 等{len(queue)}条" if len(queue) > 3 else ""
        self.lbl_queue.setText(f"任务清单：{' → '.join(shown)}{more}")

    # ── 批量操作 ──
    def _target_resolution(self) -> tuple[int, int, int]:
        """读配置里的目标安卓分辨率 (宽, 高, dpi)"""
        cfg = self.cfg
        return (int(cfg.get("emulator.resolution_w", 540)),
                int(cfg.get("emulator.resolution_h", 960)),
                int(cfg.get("emulator.resolution_dpi", 240)))

    def _bound_instances(self):
        """
        绑了账号的实例列表（「全部启动/全部挂机」的过滤口径，2026-09-21）。

        多开机上"实例比账号多"是常态（克隆完还没导号、删过账号），无脑全启
        会白占内存显存。单行右键操作不走这里——手动点某行就是明确意图。

        0 号 = 雷电多开的母体（克隆源），对全程序"不存在"：排除已在
        ldplayer._list_instances_raw 单一卡点做净，这里无需再过一道；
        库里万一残留绑 0 的号（历史数据），被点名也轮不到它。

        封号台（watch「封号停用」命中后 enabled=0）同样绕开 —— 开机也白开，
        纯占 20 台机位里的内存显存；右键单台启动不受限（用户规则，2026-09-21）。
        """
        bound = {idx for idx, a in database.accounts_by_instance().items()
                 if database.is_banned(a) is None}
        return [i for i in ldplayer.list_instances() if i.index in bound]

    def _banned_indexes(self) -> list[int]:
        """已绑账号且被标记封号的实例号（批量操作点名跳过用，与
        _bound_instances 的过滤口径同源）。"""
        return sorted(idx for idx, a in database.accounts_by_instance().items()
                      if idx != 0 and database.is_banned(a) is not None)

    def batch_start_all(self):
        """
        全部启动（秒回版，2026-09-25）：
        0. 只启动绑了账号的实例（无账号跳过并点名，见 _bound_instances）
        1. 并发统一分辨率：一把线程池查齐（停止台读配置毫秒级；运行台
           要 adb 实测，串行排在下达前面白挡 launch）
        2. 下达 launch（每个 ~0.1s 即返）：一次性全发，或按「间隔多开」
           （config emulator.launch_interval_sec）逐台错开节拍
        3. 等就绪【不再进批次】：移交 _watch_ready 界面定时器盯快照——
           旧版在这里每台最多占 150 秒，按钮全程被锁，"点了关闭没反应"
           的病根。批次跑完 1、2 就收工，按钮几秒内归还。
        """
        instances = self._bound_instances()
        banned = set(self._banned_indexes())
        bound_idx = {i.index for i in instances}
        skipped = sorted(({i.index for i in ldplayer.list_instances()}
                          - bound_idx) - banned)
        # 列表框里"有数据却开不了"的账号行（口径 2026-09-24：有数据就该被
        # 启动，开不了必须点名说原因）：未绑实例（-1）或绑的台已不存在。
        # accounts_by_instance 会静默丢掉这茬，这里单独从全表捞出来报。
        stranded = []
        for a in database.list_accounts():
            if database.is_banned(a) is not None:
                continue
            try:
                li = int(a.get("ld_index", -1))
            except (TypeError, ValueError):
                li = -1
            if li not in bound_idx:
                stranded.append((a, li))
        if skipped:
            self.log_panel.log(f"实例 {skipped} 未绑定账号，跳过启动"
                               "（拖账号 TXT 绑定后才会被启动）", "WARN")
        if banned:
            self.log_panel.log(f"实例 {sorted(banned)} 已标记封号，跳过启动"
                               "（右键「解禁」后才会被启动；单台右键仍可直接开）", "WARN")
        if stranded:
            def _tag(a, li):
                who = a.get("account") or a.get("name") or f"行#{a.get('id')}"
                return f"{who}(序号{'未绑' if li < 0 else li})"
            tag = "，".join(_tag(a, li) for a, li in stranded[:10])
            self.log_panel.log(
                f"这 {len(stranded)} 个账号没有可启动的实例，已跳过：{tag}"
                + ("…" if len(stranded) > 10 else "")
                + "（先「设置台数」补足实例，或双击行编辑改序号）", "WARN")
        if not instances:
            self.log_panel.log("没有任何实例绑定账号：先把账号 TXT 拖进列表框，或用「启动」逐台开",
                               "WARN")
            # 点了没动静是最招怀疑的体验：日志黄字容易被无视，状态栏再喊一遍
            self.statusBar().showMessage(
                "全部启动：没有可启动的账号行（列表框无数据，或账号都没绑上实例）", 8000)
            return
        # 准备工作全程留痕：挑了谁、按什么分辨率开、下达节拍怎么走
        self.log_panel.log(f"启动准备① 挑机：{len(instances)} 台已绑账号 "
                           f"{[i.index for i in instances]}")
        w, h, dpi = self._target_resolution()
        self.log_panel.log(f"启动准备② 目标分辨率 {w}x{h} @{dpi}dpi"
                           "（launch 前统一核对，防识别正常但点空）")
        jobs = []
        # 阶段 1：并发统一分辨率（一条作业打包全部台，见 core 的池子实现）
        jobs.append((f"统一 {len(instances)} 台分辨率",
                     partial(ldplayer.ensure_resolutions_parallel,
                             [i.index for i in instances], w, h, dpi)))
        # 阶段 2：下达 launch（不等待就绪）。间隔多开：config 里 launch_interval_sec
        # >0 时首台立即发、其余每台先睡间隔再发——睡在批次后台线程里，界面不卡；
        # 错开的只是「下达」节拍，模拟器各自开机本身仍是并行的。0 = 原行为全连发
        # （功能设置页最低 1 秒给不了 0，手改 config 才解锁，属排查/特殊用途后门）。
        gap = float(self.cfg.get("emulator.launch_interval_sec", 4) or 0)
        # （写法避开 f-string 内嵌同种引号——那是 3.12 才有的语法，
        # 打包机 3.11 环境要能 parse，见 emuhive.spec 路线 A 说明）
        beat = (f"逐台间隔 {'%g' % gap} 秒（错峰防开机风暴）"
                if gap > 0 else "无间隔全连发")
        self.log_panel.log(f"启动准备③ 下达节拍：{beat}")
        for k, inst in enumerate(instances):
            if k == 0 or gap <= 0:
                jobs.append((f"启动实例 {inst.index}", ldplayer.launch, inst.index))
            else:
                jobs.append((f"启动实例 {inst.index}",
                             partial(_launch_gapped, gap, inst.index,
                                     self._cancel_check)))
        # 阶段 3：等就绪移交界面定时器（不进批次）。全部实例其实同时开机，
        # 总耗时 = 最慢那台的开机时间；盯梢只负责摆窗 + 报就绪日志。
        self._watch_ready([i.index for i in instances])
        self.statusBar().showMessage(
            f"全部启动：{len(instances)} 台已下达，开机进度看日志（慢是 VM 真在开）", 8000)
        self._spawn_batch(jobs)

    def batch_quit_all(self):
        """
        全部关闭（并行抢占版，2026-09-25）：
        - 只挑运行中的台（旧版把停止台也各排一条作业，纯浪费轮询时间）
        - 并行关闭 quit_indexes_now：quit 命令全量脱手发出后统一确认，
          总耗时 ≈ 最慢那台；旧版逐台 quit_now 排队，每台独等 3 秒起步，
          10 台光关闭就白等半分钟
        - 抢占照旧：启动批次在跑也接管（等就绪已移交界面定时器，
          启动批次只剩秒级下达，正常情况根本挡不住这里）
        """
        idxs = [inst.index for inst in ldplayer.list_instances()
                if inst.running or inst.top_hwnd > 0]
        if not idxs:
            self.log_panel.log("没有运行中的实例，无需关闭")
            self.statusBar().showMessage("全部关闭：当前没有运行中的实例", 5000)
            return
        self._spawn_batch(
            [(f"并行关闭 {len(idxs)} 台实例",
              partial(ldplayer.quit_indexes_now, idxs))],
            preempt=True, preempt_msg="正在关闭，接管更早的批次")

    def on_row_op(self, op: str, index: int):
        """表格行内按钮：单实例操作，同样走后台线程"""
        if op == "start":
            w, h, dpi = self._target_resolution()
            self._spawn_batch([
                (f"统一实例 {index} 分辨率",
                 partial(ldplayer.ensure_resolution, index, w, h, dpi)),
                (f"启动实例 {index}", ldplayer.launch, index),
            ])
            # 等就绪移交界面定时器（口径同「全部启动」：批次秒回）
            self._watch_ready([index])
        elif op == "reboot":
            # 重启 = 整台虚拟机重开，走后台批次，等就绪同样移交定时器盯
            self._spawn_batch([
                (f"重启实例 {index}", partial(ldplayer.reboot, index)),
            ])
            self._watch_ready([index])
        elif op == "front":
            # 置前是微秒级系统调用，主线程直接办；句柄失效只打日志不弹窗
            inst = ldplayer.get_instance(index)
            if inst is None or not win_utils.bring_to_front(inst.top_hwnd):
                self.log_panel.log(f"实例 {index} 置前失败（窗口句柄无效，可能刚关闭）",
                                   "WARN")
        else:
            # 关闭单实例：走抢占模式，不卡等启动中的实例
            self._spawn_batch(
                [(f"关闭实例 {index}", partial(ldplayer.quit_now, index))],
                preempt=True, preempt_msg=f"正在关闭实例 {index}，接管更早的批次")

    def on_account_op(self, op: str, index: int):
        """右键「重置」段 + 封号/解禁：档案回默认值 / 标记账号封号。
        写库是毫秒级，主线程直接办。reset_one 按实例找绑定账号；reset_all 先弹确认框（全表动作，误点代价大）。"""
        if op == "reset_one":
            acct = database.accounts_by_instance().get(index)
            if acct is None:
                self.log_panel.log(f"实例 {index} 未绑定账号，无可重置", "WARN")
                return
            database.reset_account(acct["id"])
            self.log_panel.log(f"已重置实例 {index} 账号「{acct.get('account')}」的档案"
                               f"（区服/玩家ID/等级清零，账号密码备注绑定不动）")
        elif op == "reset_all":
            n = len(database.list_accounts())
            if n == 0:
                self.log_panel.log("账号表本来就是空的，无需重置")
                return
            if not confirm(self, "账号全部重置",
                           f"将把全部 {n} 个账号的区服/玩家ID/等级清零"
                           f"（账号、密码、备注、实例绑定都不动）。\n确定继续？"):
                return
            changed = database.reset_all_accounts()
            self.log_panel.log(f"已重置全部 {changed} 个账号的档案（凭据/绑定/备注未动）")
        elif op == "ban":
            # 手动封号（看到游戏内封号字样的备用通道，与 watch「封号停用」
            # 行同一套标记）：确认框拦误点（默认=否），确认后写标记 + 若在
            # 挂机就地请求停止 —— 封了还挂着等于没封。
            acct = database.accounts_by_instance().get(index)
            if acct is None:
                self.log_panel.log(f"实例 {index} 未绑定账号，无可封号", "WARN")
                return
            if database.is_banned(acct) is not None:
                self.log_panel.log(f"实例 {index} 账号「{acct.get('account')}」"
                                   f"已是封号状态，无需重复标记")
                return
            if not confirm(self, "标记封号",
                           f"将把实例 {index} 账号「{acct.get('account')}」标记为封号：\n"
                           f"表格红显，全部启动/全部挂机/右键挂机都会跳过它，\n"
                           f"正在挂机会立即停止。右键「解禁」可恢复。\n确定继续？"):
                return
            database.set_banned(acct["id"], True, reason="手动封号")
            w = self._workers.get(index)
            running = w is not None and w.isRunning()   # 停之前先记：request_stop 后此刻真值会漂
            if running:
                self.stop_worker(index)
            self.log_panel.log(f"已标记实例 {index} 账号「{acct.get('account')}」封号"
                               f"{'，该实例挂机已请求停止' if running else ''}"
                               f"（右键「解禁」恢复）", "WARN")
        elif op == "unban":
            acct = database.accounts_by_instance().get(index)
            if acct is None:
                self.log_panel.log(f"实例 {index} 未绑定账号，无可解禁", "WARN")
                return
            if database.is_banned(acct) is None:
                self.log_panel.log(f"实例 {index} 账号「{acct.get('account')}」"
                                   f"本就没有封号标记")
                return
            database.set_banned(acct["id"], False)
            self.log_panel.log(f"已解禁实例 {index} 账号「{acct.get('account')}」，"
                               f"可正常挂机")
        self.refresh()

    # 档案列显示名（日志里说人话，跟表头对齐）
    _FIELD_LABELS = {"name": "备注", "account": "账号", "server": "区服", "level": "等级"}

    def on_account_field_edited(self, index: int, field: str, text: str):
        """双击原地改档案（超级列表框式）：面板只发意图，这里写库。
        写库毫秒级，主线程直接办（与右键「重置」段同一套路）。
        text 是清空时为空串（"—" 只是显示占位，入库就是空）；
        等级列必须整数，填了别的驳回并让面板弹回原值。"""
        acct = database.accounts_by_instance().get(index)
        if acct is None:
            self.log_panel.log(f"实例 {index} 未绑定账号，改无可改", "WARN")
            return
        label = self._FIELD_LABELS.get(field, field)
        value = text
        if field == "level":
            # 空串 = 清空等级 = 0（与 acct_display「0 显示 —」闭环）；
            # 其它一律必须是整数，字母标点驳回——库里进脏数，导出/排序全遭殃
            try:
                value = int(text) if text else 0
            except ValueError:
                self.log_panel.log(f"等级只能填数字，「{text}」没保存，"
                                   f"实例 {index} 的等级已弹回原值", "WARN")
                self.panel.revert_cell(index, field)
                return
            if value < 0:
                self.log_panel.log(f"等级不能是负数，「{text}」没保存", "WARN")
                self.panel.revert_cell(index, field)
                return
        old = acct_display(acct, field)
        database.update_account(acct["id"], **{field: value})
        self.log_panel.log(f"实例 {index} 的{label}「{old}」已改成「{text or '—'}」")
        # ⚠ 必须延迟一拍再刷：本 handler 是面板 cellChanged 链上同步调的，
        # 而 cellChanged 在 closeEditor 流程内部发出、Qt 此刻还没退出编辑态——
        # 同步 refresh 必被面板的"编辑中不重建"闸门吞掉，"—"归位/高亮全不落。
        # singleShot(0) 排到事件循环下一轮，编辑器已确认关闭，重建照常。
        QTimer.singleShot(0, self.refresh)

    def on_resolution_apply(self, indexes: list[int], w: int, h: int, dpi: int):
        """
        功能设置页提交的分辨率改动：转成 ensure_resolution 批次，走后台线程执行。
        indexes 为空 = 全部放行实例（功能设置页固定传空 = 全局应用）。
        """
        if not indexes:
            indexes = [inst.index for inst in ldplayer.list_instances()]
        if not indexes:
            self.log_panel.log("没有可设置的实例", "WARN")
            return
        jobs = [(f"设置实例 {i} 分辨率",
                 partial(ldplayer.ensure_resolution, i, w, h, dpi)) for i in indexes]
        # 运行中的实例改分辨率需重启才生效，批次末尾补一句提醒（不自动 reboot，交给用户决定）
        self._spawn_batch(jobs)
        running = [i for i in indexes
                   if (inst := ldplayer.get_instance(i)) is not None and inst.running]
        if running:
            self.log_panel.log(
                f"实例 {running} 正在运行，分辨率改动需重启（关闭后重新启动）才生效", "WARN")

    def on_clone_apply(self, target: int):
        """功能设置页「模拟器多开」：台数直接设置到 target——
        少了从母体克隆补齐（很慢，全程后台批次 + 逐台进度进日志），
        多了删除编号最大的多余台（不可逆，先弹确认框点名；0 号母体在
        ldplayer 单一卡点已被排除，永远轮不到它挨删）。"""
        insts = ldplayer.list_instances()
        cur = len(insts)
        if target == cur:
            self.log_panel.log(f"已有 {cur} 台，无需调整")
            return
        if target < cur:
            # 红线操作：删实例连磁盘一起抹。确认框点名每台 + 会解绑的账号，
            # 默认按钮 = 否（账号全部重置同款纪律）
            victims = sorted(insts, key=lambda i: i.index, reverse=True)[:cur - target]
            acc_map = database.accounts_by_instance()
            lines = "\n".join(
                f"　实例 {i.index}「{i.title}」"
                + (f"（账号「{a['account']}」将解绑）" if (a := acc_map.get(i.index)) else "")
                for i in victims)
            if not confirm(self, "确认删除模拟器",
                           f"目标 {target} 台，当前 {cur} 台，将删除以下 {len(victims)} 台：\n"
                           f"{lines}\n\n"
                           f"删除不可恢复（虚拟机磁盘一起抹）；被删机上账号只解绑、\n"
                           f"档案保留，下次导入自动补位到空机。\n确定继续？"):
                return
            self.log_panel.log(f"开始删台：{cur} → {target} 台", "WARN")
        else:
            self.log_panel.log(
                f"开始克隆：{cur} → {target} 台（每台几十秒起，可最小化等它跑）", "WARN")
        # on_progress 走信号回投：克隆/删除单台都可能是几十秒级，
        # 没有逐台进度用户会以为卡死
        self._spawn_batch([("模拟器多开",
                          partial(self._set_count_job, target, self.clone_progress.emit,
                                  self.instances_removed.emit))])

    @staticmethod
    def _set_count_job(target: int, emit, emit_removed) -> str:
        """批次线程里跑：双向设置台数；被删机上绑的账号当场解绑
        （ld_index=-1：档案保留、表格消失、下次导入自动补位）。
        本线程写库与导入作业同范式（SQLite 允许多线程，GUI 不感知）。"""
        made, removed, err = ldplayer.set_instance_count(target, on_progress=emit)
        if removed:
            emit_removed(list(removed))   # 界面线程清完成度/进度残影（Qt 队列回投）
        unbound = 0
        if removed:
            acc_map = database.accounts_by_instance()
            for idx in removed:
                acct = acc_map.get(idx)
                if acct is not None:
                    database.update_account(acct["id"], ld_index=-1)
                    unbound += 1
        parts = []
        if made:
            parts.append(f"新建 {made} 台")
        if removed:
            parts.append(f"删除 {len(removed)} 台 {removed}"
                         + (f"，解绑账号 {unbound} 个" if unbound else ""))
        tail = ("；".join(parts) if parts else "台数无变化") + \
               f"，当前 {len(ldplayer.list_instances())} 台"
        return f"{tail}；中止：{err}" if err else tail

    def on_accounts_file_dropped(self, path: str):
        """拖入账号 TXT → 后台批次导入（几百行 = 几百次写库，主线程做会卡界面）。
        复用 _spawn_batch 全套：按钮自动禁用、耗时日志、完成自动 refresh——
        表格「账号」列跟着亮。表头认不出/文件坏了由批次线程兜异常进日志，不崩。
        ⚠ 覆盖式导入：文件里没写的账号会被删（删前自动备份到 data/backups/）"""
        self._spawn_batch([("导入账号文件", partial(self._import_accounts_job, path))])

    @staticmethod
    def _import_accounts_job(path: str) -> str:
        """在批次线程里跑：导入并把统计报告拼成一行返回（字符串=批次直接进日志）"""
        from core import account_io   # 延迟导入：只有真拖了文件才碰这模块
        result = account_io.import_accounts(path)
        return "；".join(text for _level, text in account_io.format_import_report(result))

    def _export_accounts(self, with_password: bool):
        """「导出账号」按钮菜单：整表写 TXT（列名与表格一致，拖回列表框即可还原）。
        读库+写文件毫秒级，不值得走后台批次；失败进日志，不弹窗打断。"""
        # 默认落进项目「导出/账号/」（用户拍板：导出一律进项目对应中文文件夹）
        from core import flow_pack
        path, _ = QFileDialog.getSaveFileName(
            self, "导出账号", flow_pack.default_path("账号", "txt"),
            "TXT (*.txt)")
        if not path:
            return
        from core import account_io   # 与导入同款延迟导入
        try:
            r = account_io.export_accounts(path, with_password=with_password)
        except OSError as e:
            self.log_panel.log(f"账号导出失败：{e}", "ERROR")
            return
        note = "⚠ 含密码明文，此文件等同账号密码清单，别外传" if with_password \
               else "（不含密码，可放心分享）"
        self.log_panel.log(f"已导出账号 {r['count']} 条 → {r['path']}；{note}")

    def busy_instances(self) -> set:
        """当前有正式挂机线程在跑的实例号集合。
        工作台流程试运行前查这里：同一实例两边点必撞车。"""
        return {i for i, w in self._workers.items() if w.isRunning()}

    def open_workbench(self):
        """打开统一工作台（模板采集 + 流程编排）"""
        from ui.workbench import WorkbenchDialog  # 延迟导入，避免启动变慢
        if self._workbench is None:
            self._workbench = WorkbenchDialog(self)
            # 工作台关闭后刷新：流程任务下拉框 + 左侧任务栏/任务清单对账
            # （用自定义信号 closed，因为窗口重写了 closeEvent，不会走 finished）
            self._workbench.closed.connect(self._sync_flows)
            # 流程事件唯一出口（保存/新建/改名/删除/导入全发这一发）：
            # 主窗口一个槽函数对账，不再散接改名线/删除线/15s 兜底定时器
            self._workbench.flow_saved.connect(self._sync_flows)
            # 流程编排「加入任务栏」→ 底部任务清单（与侧栏双击同一条入队路）
            self._workbench.add_to_queue.connect(self._on_flow_add_queue)
        self._float_ball.attach(self._workbench)  # 惰建后挂进悬浮球（幂等）
        recall_window(self._workbench, focus=True)  # 点按钮=明确召回：清最小化位再 show+raise

    def _on_view_flow(self, fname: str):
        """任务清单双击一项 = 查看流程：开工作台并载入该流程。
        （双击原是"移除"，手滑一点丢一项还不提示；移除腾给 ✕/Del 键。）"""
        if not fname:
            return
        self.open_workbench()
        self._workbench.view_flow(fname)

    def _sync_flows(self, name: str = "", old: str = ""):
        """流程清单对账唯一入口（工作台 flow_saved 事件 + 关窗兜底都走这里）。
        改名=（新名,旧名）：先按名就地换（改名不动集合，集合对账只会把旧名
        当死项删掉、不会补上新名，必须换名在先）；其余事件=按集合剔死项。
        没有周期兜底定时器了：实时性全押在事件源齐整上（保存/新建/改名/
        删除/导入每条路都发 flow_saved）。外部工具（如 flow_apply）运行中
        改库仍需关工作台或重开才对账——见交接报告。"""
        if name and old and name != old:
            self.cat_sidebar.rename_task(old, name)   # 任务栏里的旧名就地换新
            self.queue_panel.rename_flow(old, name)
        if self.cat_sidebar.refresh_flows():
            # 流程集合变了：清单里指向已删流程的死项跟着剔除（同一次变化一起处理）
            self.queue_panel.refresh_flows()

    def open_log_window(self):
        """打开独立运行日志窗口（self.log_panel 就是它本体，非模态可与主界面并排）"""
        recall_window(self.log_panel, focus=True)

    def open_status_window(self):
        """打开独立状态信息窗口（只读观察窗，非模态可与主界面并排）"""
        from ui.status_window import StatusWindow  # 延迟导入，避免启动变慢
        if self._status_window is None:
            self._status_window = StatusWindow(self)
        self._float_ball.attach(self._status_window)  # 惰建后挂进悬浮球（幂等）
        recall_window(self._status_window, focus=True)

    def runtime_snapshot(self) -> dict:
        """
        聚合当前运行时状态，供状态窗口只读拉取：
          workers  = {实例index: worker状态字符串}（idle/running/paused/…）
          progress = {实例index: {"task","step","since"}}（浅拷贝，防迭代中被改）
          chain_done = {实例index: 任务链是否整条跑完}（"完成度"列）
          queue    = 当前任务清单换算出的任务链（从上到下即执行顺序）
        """
        workers = {i: (w.state_value if w.isRunning() else "idle")
                   for i, w in self._workers.items()}
        return {"workers": workers,
                "progress": dict(self._progress),
                "chain_done": dict(self._chain_done),
                # queue = 清单换算后的任务链（状态窗口按任务显示名渲染，口径不变）
                "queue": self.queue_panel.queue_as_tasks()}

    def _watch_ready(self, indexes):
        """登记一批"待就绪"实例，界面 1s 定时器盯快照（见 _tick_ready_watch）。
        两级就绪（2026-09-25）：窗口出现→立刻摆位排窗（看得见摸得着），
        安卓就绪→再报"可挂机"；每台封顶 150 秒。批次线程从此不干等。"""
        now = time.time()
        for i in indexes:
            self._ready_pending[i] = {"deadline": now + 150,
                                      "seen_hwnd": False, "arranged": False}
        self._ready_timer.start()
        # 急停钮在盯梢期间也要可用（取消批次现在覆盖"停等就绪"）
        self.btn_cancel.setEnabled(True)
        self.log_panel.log(f"等待 {sorted(indexes)} 台开机就绪（窗口一出就摆位）")

    def _tick_ready_watch(self):
        if not self._ready_pending:
            self._ready_timer.stop()
            if not (self._worker is not None and self._worker.isRunning()):
                self.btn_cancel.setEnabled(False)
            return
        snap = {i.index: i for i in ldplayer.list_instances()}
        now = time.time()
        for idx in list(self._ready_pending):
            st = self._ready_pending[idx]
            inst = snap.get(idx)
            if inst is not None and inst.android_ready:
                if not st["arranged"]:
                    self._arrange_window_bg(idx)
                self.log_panel.log(f"实例 {idx} 安卓就绪（这台可以开始挂机）")
                self._ready_pending.pop(idx)
            elif inst is not None and inst.top_hwnd > 0:
                if not st["arranged"]:
                    # 两级就绪第一级：窗口在就摆位，不等安卓标志
                    # （雷电 4 绿化版 android_started 翻得慢，干等最坏 150s）
                    st["arranged"] = True
                    self._arrange_window_bg(idx)
                    self.log_panel.log(f"实例 {idx} 窗口已出现，后台校正尺寸/排位中（安卓继续启动）")
                st["seen_hwnd"] = True
            elif st["seen_hwnd"]:
                # 曾经活过又没了：被手动叉掉/启动失败，别傻等（同 wait_until_ready 的死亡检测）
                self.log_panel.log(f"实例 {idx} 窗口消失，不再等待就绪", "WARN")
                self._ready_pending.pop(idx)
            elif now > st["deadline"]:
                self.log_panel.log(f"等待实例 {idx} 就绪超时（150 秒），"
                                   "可能卡在启动画面（看窗口确认，必要时右键重启）", "WARN")
                self._ready_pending.pop(idx)
        if not self._ready_pending:
            self._ready_timer.stop()
            if not (self._worker is not None and self._worker.isRunning()):
                self.btn_cancel.setEnabled(False)

    def _arrange_window_bg(self, idx: int):
        """窗口摆位（1:1 校正 + 级联排位）挪后台线程跑。
        ⚠ 病根：restore_native_window 内含 adb 取分辨率（开机风暴期可挂数秒）
        + ShowWindow 后 sleep(1) + 最多 6 轮×0.6s 收敛睡眠，旧版直接在界面
        1s 定时器里同步调 = 消息泵停摆 = Windows 标「未响应」（实测症状）。
        win32/子进程跨线程安全；完成消息走 clone_progress 信号回投界面线程。
        同实例在飞最多一条（_arranging 去重），跑完销账。"""
        if idx in self._arranging:
            return
        self._arranging.add(idx)

        def _job():
            try:
                ldplayer.restore_native_window(idx)
                ldplayer.cascade_window(idx)
            finally:
                self._arranging.discard(idx)
                self.clone_progress.emit(f"实例 {idx} 窗口摆位完成")
        threading.Thread(target=_job, name=f"arrange-{idx}", daemon=True).start()

    def cancel_batch(self):
        """用户点"取消批次"：请求后台线程停止（当前步骤结束后生效）"""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.log_panel.log("已请求取消批次（当前步骤结束后停止）", "WARN")
        if self._ready_pending:
            # 就绪盯梢一并停：它不算批次，但用户点急停的意图覆盖"别再盯了"
            self._ready_pending.clear()
            self._ready_timer.stop()
            self.log_panel.log("已停止等待就绪（已开出的模拟器不会因此关闭，"
                               "要关请点「全部关闭」）", "WARN")

    def _cancel_check(self):
        """供 wait_until_ready 轮询：当前批次被取消就提前结束等待。
        读的是"跑这个等待的线程所属批次"的取消标志（BatchWorker._by_thread），
        不是界面指针 self._worker——抢占场景下旧批次的等待作业会看错批次。
        线程不在批次里（主线程直调等）退回落 self._worker，语义同旧版。"""
        w = BatchWorker._by_thread.get(threading.get_ident()) or self._worker
        return w is not None and w.is_cancelled()

    def _spawn_batch(self, jobs, preempt: bool = False, preempt_msg: str = ""):
        """开一个后台批次（普通 / 抢占共用一份接线：建线程→连信号→收摊→看门狗）。

        preempt=False（普通批次）：已有批次在跑 → 提示「稍候」直接返回，不接管。
        preempt=True（抢占批次，用于「关闭」类点了就要马上生效的操作）：
          旧批次在跑也开新批次，新批次完成后自动取消旧批次。

        并发安全：旧/新批次各跑自己的 QThread 与 jobs 队列；
        set_preempt_target 只在新批次线程 run 跑完 jobs 后读一次，此时旧批次
        还没退出（isRunning()=True），cancel() 生效。
        """
        prev = self._worker
        busy = prev is not None and prev.isRunning()
        if busy and not preempt:
            self.log_panel.log("有任务正在执行，稍候", "WARN")
            return
        self._set_busy(True)
        new = BatchWorker(jobs, self, preempt=(preempt and busy))
        if preempt and busy:
            new.set_preempt_target(prev)
            if preempt_msg:
                self.log_panel.log(preempt_msg, "WARN")
        self._worker = new
        new.job_done.connect(lambda msg: self.log_panel.log(msg))
        new.all_done.connect(self._on_batch_finished)
        # 线程跑完即回收：parent 挂主窗口只防"提前 GC"、防不了终身驻留——
        # 每天几十次批次就是几十只死线程堆着；收工即摘界面指针 + deleteLater 销毁
        # C++ 对象。留旧指针 = 下次开批 prev.isRunning() 踩尸体炸 RuntimeError
        # （实测 2026-09-26：崩在槽函数第一行，"全部关闭"没反应）。
        new.finished.connect(partial(self._forget_worker, new))
        new.finished.connect(new.deleteLater)
        new.start()
        # 看门狗：批次正常几分钟内必完成；万一线程真卡死，8 分钟后强制解锁界面，
        # 绝不让按钮永远灰着。定时器在 __init__ 建好，这里只重置倒计时。
        self._watchdog.start(8 * 60 * 1000)

    def _forget_worker(self, w, _finished=False):
        """批次线程收工（finished 信号）：若界面还指着它就把指针摘掉。
        紧跟着的 deleteLater 会销毁 C++ 对象，旧指针留着就是尸体——
        下次开批 isRunning() 当场 RuntimeError（「全部关闭」没反应病根）。"""
        if self._worker is w:
            self._worker = None

    def _on_batch_finished(self):
        # 抢占场景：旧批次被 cancel 后仍会 emit all_done，但此时 self._worker 已指向
        # 新批次——只有当 sender() 是当前 worker 时才解锁按钮，否则忽略旧批次的信号
        sender = self.sender()
        if sender is not self._worker:
            return
        self._stop_watchdog()
        self._set_busy(False)
        self.log_panel.log("批次操作完成")
        self.refresh()

    def _on_watchdog_timeout(self):
        self.log_panel.log("批次操作疑似卡死（超过 8 分钟），已强制解锁界面", "ERROR")
        self._stop_watchdog()
        self._set_busy(False)
        self._worker = None  # 解除引用，允许发起新批次

    def _stop_watchdog(self):
        if getattr(self, "_watchdog", None) is not None:
            self._watchdog.stop()

    def _set_busy(self, busy: bool):
        """执行批量操作时禁用按钮，防止连点撞车；同时启用/禁用'取消批次'
        （固定占位，绝不 setVisible —— 收起会顶动下方按钮位置，见构造处注释）"""
        # 「全部关闭」永不上锁（2026-09-25）：关闭批次天生抢占（跑完自己
        # 接管旧批次），锁它等于把逃生通道焊死——启动批次卡着时点不动关闭，
        # 正是"点了没反应"的直接病根。启动/刷新照旧互斥。
        for btn in (self.btn_start_all, self.btn_refresh):
            btn.setEnabled(not busy)
        self.btn_cancel.setEnabled(
            busy or bool(self._ready_pending))

    # ── 挂机 worker 管理（第 7 步）──
    def on_worker_command(self, op: str, index: int):
        if op == "start_worker":
            self.start_worker(index)
        elif op == "stop_worker":
            self.stop_worker(index)
        elif op == "pause":
            self.pause_worker(index)
        elif op == "resume":
            self.resume_worker(index)

    def start_worker(self, index: int):
        """
        启动实例 index 的挂机线程：走「只认清单」逻辑，
        清单为空不启动；实例级 queue 仍优先（见 bot/worker.py resolve_task_name）。
        """
        if index in self._workers and self._workers[index].isRunning():
            return
        # 封号门禁（右键挂机与全部挂机共用本漏斗，一处拦两边）：
        # watch「封号停用」行命中后账号 enabled=0，不拦的话重启程序又会挂上去。
        acct = database.accounts_by_instance().get(index)
        reason = database.is_banned(acct)
        if reason is not None:
            self.log_panel.log(f"实例 {index} 账号「{acct.get('account')}」已标记封号"
                               f"{'（' + reason + '）' if reason else ''}，跳过挂机"
                               f"（右键「解禁」后可挂）", "WARN")
            return
        inst = ldplayer.get_instance(index)
        if inst is None or not inst.running:
            self.log_panel.log(f"实例 {index} 未运行，请先启动模拟器", "WARN")
            return
        # 只认清单：清单（任务清单）换算成任务链后为空就不启动。
        # （实例级 account_tasks.queue 仍优先，见 bot/worker.py 的 resolve_task_name）
        tasks = self.queue_panel.queue_as_tasks()
        if not tasks:
            self.log_panel.log(
                f"实例 {index} 未启动：任务清单为空，请点清单「＋」加入流程", "WARN")
            return
        # AccountWorker 的任务名收逗号分隔串 = 任务链，按序执行，引擎零改动
        task_name = ",".join(tasks)
        w = AccountWorker(index, task_name=task_name)
        self._workers[index] = w
        w.state_changed.connect(self.on_worker_state)
        w.action_log.connect(self._on_worker_action)
        w.task_changed.connect(self.on_worker_step)
        w.chain_finished.connect(self.on_chain_finished)
        # 线程真收摊（run 返回）才允许丢引用：见 _finishing / on_worker_state
        w.finished.connect(lambda *_, _w=w: self._finishing.discard(_w))
        # 重开就清掉上一轮的链结局，否则"完成度"会一直挂着旧的"已完成"
        self._chain_done.pop(index, None)
        # 表格进度列的初值：链首任务 + 启动时刻（task_changed 只在步骤变化时发）
        # 运行任务列初值：flow:X 记号直接取流程名本体（不带「自定义流程·」前缀）
        t0 = tasks[0] if tasks else ""
        self._progress[index] = {
            "task": t0[5:] if t0.startswith("flow:") else (template_deps.display_user(t0) if t0 else ""),
            "step": "启动中",
            "since": time.time(),
        }
        w.start()
        chain = " → ".join(template_deps.display_user(t) for t in tasks)
        self.log_panel.log(f"实例 {index} 挂机启动（清单 {len(tasks)} 项：{chain}）")
        self.refresh()

    def stop_worker(self, index: int):
        w = self._workers.get(index)
        if w is not None and w.isRunning():
            w.request_stop()
            self.log_panel.log(f"实例 {index} 已请求停止（完成当前动作后退出）")

    def pause_worker(self, index: int):
        w = self._workers.get(index)
        if w is not None and w.isRunning():
            w.request_pause()
            self.log_panel.log(f"实例 {index} 已请求暂停")

    def resume_worker(self, index: int):
        w = self._workers.get(index)
        if w is not None and w.isRunning():
            w.request_resume()
            self.log_panel.log(f"实例 {index} 已请求恢复")

    def _on_worker_action(self, index: int, lvl: str, msg: str):
        """worker 运行日志：进界面面板 + 落盘。线程进出、任务动作都走这条，
        是事后归因的主证据流。"""
        self.log_panel.log(f"[实例{index}] {msg}", lvl)
        run_log.log(index, lvl, msg)

    def on_worker_step(self, index: int, tname: str, step: str):
        """worker 上报步骤变化：记日志 + 更新表格进度列的数据源。
        ⚠ 全表重建不跟着步骤信号走：流程节点密集时每秒可报数次，十路实例
        叠起来就是每秒几十次超级列表框重建（主线程肉眼可见发黏）。日志照常
        逐条落（事后归因靠它），表格用一个 0.7 秒单次触发的合并闸收口：
        突发多次变化只刷一次，人眼看仍近似即时。"""
        self.log_panel.log(f"[实例{index}] 步骤：{step}")
        run_log.log(index, "STEP", f"{tname} → {step}")
        pr = self._progress.get(index)
        if pr is None:
            return   # 线程已收摊（stop 后晚到的残帧信号）：别把清掉的进度复活
        # tname 是 worker 拼好的显示标签（"自定义流程·流程名"），直接用；
        # 旧版这里查 `tname in TASKS` 换算 display_name 是死代码——标签永远带
        # ·后缀，匹配不上英文标识的 TASKS 键，分支恒不命中
        pr["task"] = tname
        pr["step"] = step
        if not self._step_refresh_timer.isActive():
            self._step_refresh_timer.start()

    def _cleanup_logs(self):
        """磁盘保留期（稳定性审计 ④）：logs/run_*.log 删 30 天前的；
        fail_capture 每实例目录只留最新 50 张。只动 logs/ 下本工具自产
        文件（2026-09-24 用户批准的成品修复清单），其余一概不碰。"""
        from pathlib import Path
        n = 0
        try:
            cutoff = time.time() - 30 * 86400
            for f in Path("logs").glob("run_*.log"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        n += 1
                except OSError:
                    pass
            for d in Path("logs/fail_capture").glob("inst*"):
                files = sorted(d.glob("*.png"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                for f in files[50:]:
                    try:
                        f.unlink()
                        n += 1
                    except OSError:
                        pass
        except Exception as e:
            self.log_panel.log(f"日志清理失败（忽略）：{e}", "WARN")
            return
        if n:
            self.log_panel.log(
                f"日志清理：删除 {n} 个过期件（run 日志留 30 天、失败截图每实例留 50 张）")

    def _maybe_auto_restart(self, index: int):
        """限次自动重挂（产品决策 2026-09-24 拍板）：error 后 60 秒自动重挂，
        每实例 1 小时滑窗最多 2 次，用完停手等人工。手动停止走 stopped 分支
        永不进这里；start_worker 自带封号/未运行/空清单门禁，重挂零特判。"""
        hist = self._restart_hist.setdefault(index, [])
        now = time.time()
        hist[:] = [t for t in hist if now - t < 3600]
        if len(hist) >= 2:
            self.log_panel.log(f"[实例{index}] 1 小时内自动重挂额度用完（2 次），"
                               f"已停手，请查看日志与失败截图", "WARN")
            run_log.log(index, "WARN", "自动重挂额度用完，停等人工")
            return
        hist.append(now)
        self.log_panel.log(f"[实例{index}] 异常退出，60 秒后自动重挂"
                           f"（本小时第 {len(hist)}/2 次）", "WARN")
        run_log.log(index, "WARN", f"异常退出 → 60 秒后自动重挂（本小时第 {len(hist)}/2 次）")
        QTimer.singleShot(60_000, lambda: self._auto_restart(index))

    def _auto_restart(self, index: int):
        # 60 秒内用户可能已手动重挂或关了程序：注册表有活线程就不插手
        w = self._workers.get(index)
        if w is not None and w.isRunning():
            return
        self.log_panel.log(f"[实例{index}] 执行自动重挂")
        self.start_worker(index)

    def on_worker_state(self, index: int, state: str):
        self.log_panel.log(f"[实例{index}] 挂机状态：{state}")
        # 状态时间线必须落盘：事后归因"什么时候进的什么状态"全靠这条
        run_log.log(index, "STATE", f"挂机状态 → {state}")
        if state == "error":
            self._maybe_auto_restart(index)
        if state in ("stopped", "error"):
            w = self._workers.pop(index, None)  # 移出注册表（不再接受控制指令）
            if w is not None and w.isRunning():
                # ⚠ 信号在 worker 线程 run() 的 finally 里发出，主线程收到时
                # 线程可能还差最后一行没返回：直接丢引用 = GC 撞"destroyed
                # while running"进程级 abort（十路快启快停必踩）。保活到
                # finished（run 真返回）再放，见 __init__ _finishing 注释。
                self._finishing.add(w)
            self._progress.pop(index, None)  # 进度列跟着清空
        # 表格重建走 0.7 秒合并闸（与步骤信号同口径）：多路实例一起停时
        # 不再连环全表重建
        if not self._step_refresh_timer.isActive():
            self._step_refresh_timer.start()

    def on_chain_finished(self, index: int, done: bool):
        """worker 上报任务链结局（表格"完成度"列）：跑完=True，失败/被停=False。
        单独记一份是因为链结束随即 _progress 就被清了，完成度不能跟着一起没。"""
        self._chain_done[index] = done

    def _on_instances_removed(self, indexes: list):
        """删台收口（_set_count_job 批次线程经信号回投）：被删实例的
        完成度/进度残影清掉——完成度列设计上跨挂机轮次留存（worker 退出
        不清），但"实例没了"是终点：补回同号新机不该顶到旧"已完成"。"""
        for i in indexes:
            self._chain_done.pop(i, None)
            self._progress.pop(i, None)
        if indexes:
            self.refresh()

    def batch_worker_start(self):
        """全部挂机：只对「运行中 且 绑了账号」的实例启动 worker
        （空模拟器不白跑线程，口径同 _bound_instances；单行右键不受此限）"""
        for inst in self._bound_instances():
            if inst.running:
                self.start_worker(inst.index)

    def batch_worker(self, op: str):
        """全部暂停/恢复/停止"""
        for index in list(self._workers.keys()):
            if op == "pause":
                self.pause_worker(index)
            elif op == "resume":
                self.resume_worker(index)
            else:
                self.stop_worker(index)

    # ── 刷新表格与状态灯 ──
    def refresh(self):
        # 直接读 worker 显式状态，不拿 paused 布尔硬推
        # （states 计算收口进 runtime_snapshot，状态窗口与表格同源不漂移）
        snap = self.runtime_snapshot()
        # 账号列数据源：一次查全表按实例归并（accounts_by_instance 内部就一条
        # SQL，3 秒一轮不贵）。拖 TXT 导入后正是这一列跟着亮。
        # 完成度并进 progress：链跑完后 progress 被清空，但链结局要留着显示，
        # 合并成一份传过去，面板只认一个数据源
        prog = dict(snap["progress"])
        for i, done in snap["chain_done"].items():
            prog[i] = {**prog.get(i, {}), "done": done}
        all_accounts = database.list_accounts()
        # 资产读数一次算好、两处共用：get_account_assets 是 JSON 解析，过去
        # 表格「资产」列和总览页各自逐号解一遍（3 秒一轮 × 全部账号 ×2）。
        # 键=账号 id（accounts_by_instance 与 list_accounts 返回同一份行结构）
        assets_by_id = {a.get("id"): database.get_account_assets(a)
                        for a in all_accounts}
        self.panel.refresh(snap["workers"], prog,
                           database.accounts_by_instance(),
                           assets_by_id=assets_by_id)
        # 资产总览页与「资产」列同源（accounts 全表，一页多号/未绑号的也列）
        self.asset_panel.refresh(all_accounts, assets_by_id=assets_by_id)
        instances = ldplayer.list_instances()
        n_run = sum(1 for i in instances if i.running)
        n_work = len(self._workers)
        self.feature_panel.refresh_count(len(instances))   # 模拟器多开：当前台数实时回填
        # 状态灯三态（签名元素）：选色统一走 theme.lamp_color，与状态窗同源
        self.status_light.setStyleSheet(f"color: {theme.lamp_color(n_work, n_run)};")
        # 状态栏实例统计：总数/运行中/挂机中
        self.lbl_inst_stat.setText(
            f"实例 {len(instances)} / 运行 {n_run} / 挂机 {n_work}")
        self.refreshed.emit()   # 收尾广播：状态观察窗跟这一拍渲染（可见才画）

    def showEvent(self, event):
        """每次显示给主窗设【窗口级】任务栏身份（APPID_MAIN）。
        病根：进程级 AppID 一旦显式设定（main.py），本进程所有顶层窗默认同组，
        主界面与工作台/日志/状态被 Windows 合成一个任务栏按钮，点它只弹预览、
        不最小化。各窗各设一个窗口级身份即拆成独立按钮，恢复"点一下最小化"。
        每次显示重设：窗口级属性绑在 HWND 上，HWND 重建（如子窗 setWindowFlag）
        会丢，主窗虽少见但同一纪律（详见 core.win_utils.set_window_app_id）。"""
        super().showEvent(event)
        win_utils.set_window_app_id(int(self.winId()), win_utils.APPID_MAIN)

    # ── 关闭窗口时保存配置 + 停掉所有挂机线程（需求 6）──
    def closeEvent(self, event):
        # 工作台先关：它重写过的 closeEvent 有「未保存流程」确认闸 + 试跑线程
        # 收摊。旧版主窗口关闭从不叫它关，Qt 销毁父窗连带抹掉子顶层窗时不走
        # closeEvent —— 编排到一半没存的流程直接蒸发（实测丢过整库重做的流程，
        # 红线级）。close() 被闸拦下返回 False，主窗口跟着 ignore，用户回去存。
        if self._workbench is not None and not self._workbench.close():
            event.ignore()
            return
        # 先软停所有 worker，全局预算封顶 5 秒让线程干净退出（旧版逐个 wait 3s：
        # N 台最坏 3N 秒卡关闭。改全局 deadline，每人 wait(min(3000, 剩余))；
        # 预算烧完仍未退的记一行日志、交给 Qt 收尾，绝不无限等）
        for w in list(self._workers.values()):
            if w.isRunning():
                w.request_stop()
        deadline = time.monotonic() + 5.0
        for w in list(self._workers.values()):
            remain = int((deadline - time.monotonic()) * 1000)
            w.wait(max(0, min(3000, remain)))
        for w in list(self._workers.values()):
            if w.isRunning():
                run_log.log("gui", "WARN",
                            f"挂机线程实例 {w.ld_index} 5 秒预算内未退出，交给 Qt 收尾")
        window_geometry.save(self, self.cfg, "ui.window_geometry")
        # 超级列表框布局记忆：列宽/列序/滚动位置关窗抓一份（用户点名不做
        # 拖动即存——强杀丢布局可接受，换来运行期零写盘）
        self.cfg.set("ui.table_layout", self.panel.save_layout())
        self.cfg.set("ui.asset_layout", self.asset_panel.save_layout())
        # splitter 拖动比例也存下来，下次启动还原
        self.cfg.set("ui.root_splitter", self.root_splitter.sizes())
        self.cfg.set("ui.mid_splitter", self.mid_splitter.sizes())
        self.cfg.set("ui.bottom_splitter", self.bottom_splitter.sizes())
        # 子顶层窗口（日志/状态）随父销毁不走各自 closeEvent，这里显式补存；
        # 只存"当前可见"的——没打开过的窗口坐标是默认值，存了反而把下次
        # 的位置钉死在 (0,0)
        if self.log_panel.isVisible():
            self.log_panel.save_geometry()
        if self._status_window is not None and self._status_window.isVisible():
            self._status_window.save_geometry()
        self.cfg.save()  # 立即写盘（不等防抖定时器）
        event.accept()
