"""
ui/workbench.py — 素材/流程统一工作台（预览常驻 + 右列分页版）

把「模板采集」和「流程编排」集成进同一个窗口：左=实例预览常驻，
右=两块面板叠成一页一档（顶部按钮切换）。曾经三栏同屏，结果
流程画布和采集表单各挤一半、谁也施展不开 —— 分页后每页独占全宽。

┌────────────────────────────────────────────────────────────┐
│ 实例:[▾][刷新]          鼠标读数           状态栏            │
├───────────┬────────────────────────────────────────────────┤
│ 共用预览    │ [流程编排 | 模板采集]  ← 分页切换               │
│ ImageView  ├────────────────────────────────────────────────┤
│ (1:1固定)   │  FlowPanel / TemplatePanel（当前页独占）        │
└───────────┴────────────────────────────────────────────────┘
   列间 QSplitter 可拖

两块面板共用同一个预览画面（截图定时器只此一份），点选/框选的归属由
ImageView.click_owner 字段裁决（见下"意图路由"）：谁最后进入 click_target
态谁拥有这一次点选，采集与取坐标不会串扰。

联动：
- 采模板保存新图 → flow_panel.on_templates_changed() 直接重建属性面板下拉
  （面板切页只是藏/显，控件不死，回填照旧）
- 采模板保存新图 + 有回程票 → flow_panel.apply_capture_result() 回填到发起
  「去采集」的那个节点/分支条件
- 编流程点「缺图？去采集」→ 发 request_capture(目录)：外壳预填归属 +
  自动切到采集页（begin_capture），预览回框选态；采完不自动跳回，
  连采同类时手动点回更顺手

数据源：截图走 core.capture（与采集原实现同一条链），实例列表走 core.ldplayer。
截图/校准跑在后台抓帧线程（_PreviewGrabber），GUI 线程只投递任务和收帧 ——
"全部启动→点工作台"未响应的根治点（开机风暴期 PrintWindow/adb 兜底会堵死消息泵）。
预览固定 1:1 不缩放，左栏随画面同尺寸钉死（分隔条拖不动它）；坐标一律换算回
安卓基准（540×960）后再落库/绘制。
"""

import threading
import time

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import capture, ldplayer, win_utils
from core.config_manager import get_config
from ui.float_ball import recall_window
from ui.flow_layout import FlowLayout
from ui.help_badge import make_help_badge
from ui.flow_editor import FlowPanel
from ui.icons import window_icon
from ui.template_tool import ImageView, TemplatePanel
from ui import window_geometry


def _run_job(job):
    """后台抓帧本体：统一口取帧（与挂机同路，预览=脚本所见）。
    单独成模块函数，冒烟可不打线程直调。异常护栏同 bot/worker 每拍护栏：
    一帧炸不许带走线程，换成错误文案回投。
    2026-10-09 窗口截图退出主循环：预览帧恒为纯安卓基准图、无边框可裁，
    校准/漂移复核这套随窗口路一并撤（框选采集若要量偏移另有独立口）。"""
    idx, hwnd = job
    try:
        img, _ = capture.grab_with_fallback(hwnd, idx)
    except Exception as e:
        return idx, None, f"预览异常：{e}"
    return idx, img, ""


class _PreviewGrabber:
    """预览抓帧后台守护线程 —— "全部启动→点工作台未响应"的根治（2026-10-04）。

    病根：_tick 挂在 GUI 线程，慢取帧会摁死消息泵（Windows 5 秒不转就标
    「未响应」）：开机风暴期 adb 截图两条往返能堵 0.3~0.8 秒/帧。旧版
    「全部启动」犯过同款病，根治思路一致
    （core/ldplayer.py 快照 poller 注释）：慢查询让后台去扛，界面只拿结果。

    同时最多一帧在飞（GUI 侧 _grab_busy 闸门，post 永不积压）；没活小睡
    0.1s；daemon 线程随进程走，不藏"销毁仍在跑的线程"这种雷。
    """

    def __init__(self, emit):
        self._emit = emit     # 回投：跨线程 emit 由 Qt 自动排队，槽在主线程跑
        self._job = None      # (idx, hwnd)
        self._lock = threading.Lock()   # 只护换引用，绝不罩截图
        threading.Thread(target=self._loop, name="wb-preview", daemon=True).start()

    def post(self, job):
        with self._lock:
            self._job = job

    def _loop(self):
        while True:
            with self._lock:
                job, self._job = self._job, None
            if job is None:
                time.sleep(0.1)
                continue
            self._emit(*_run_job(job))


class WorkbenchDialog(QWidget):
    """统一工作台主窗口。非模态，可与主界面并排操作。"""

    # closeEvent 被本类重写（未保存拦截），不会走 destroyed/finished，
    # 自己发一个关闭信号给主窗口刷新流程任务的动态下拉框
    closed = Signal()

    # 模板库增删（新建库/删除库/首次采图入库等，凡 templates_changed 触发点）：
    # 转发给主界面刷新最左侧的模板库列表（库点挂机改版）。主窗口只在
    # "库集合真的变了"时才重建列表，所以这里可以直接透传，无需自己判重。
    libraries_changed = Signal()

    # 流程事件唯一出口（新名, 旧名）：保存/新建/改名/删除/导入全发这一发，
    # 主窗口一个槽函数对账（旧名非空=改名要先换名；新名非空=活名）。
    # 单一事件源：以前改名/删除各一发、主窗口各接一线再留 15s 兜底定时器，
    # 三处口径必漂移。用信号而非让主窗口自己盯着：主窗口不碰工作台的 widget。
    flow_saved = Signal(str, str)

    # 流程「加入任务栏」→ 主窗口底部流程清单（选中即入，单条）
    add_to_queue = Signal(str)

    # 后台抓帧线程回帧（实例号, 纯安卓基准帧|None, 错误文案）。
    # 跨线程 emit → Qt 自动排队 → _on_frame 只在 GUI 线程跑（widget 不在线程侧碰）
    frame_ready = Signal(int, object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # 独立窗口：QWidget 带 parent 默认是"嵌进父容器的子控件"——不置顶层标志
        # 会直接浮在主界面上、背景透明、和任务表格叠成一团。Qt.Window 才是真窗口。
        self.setWindowFlags(self.windowFlags() | Qt.Window)
        self.setWindowTitle("工作台 · 模板采集 / 流程编排")
        # 任务栏辨认图标：蓝圆白字「台」（三窗各一色一字，见 ui.icons.window_icon）
        self.setWindowIcon(window_icon("台", "#3367D6"))
        self._cfg = get_config()

        self._frame = None
        self._tick_status = ""      # 上次 _tick 播报的状态（见 _set_tick_status）
        self._grab_busy = False     # 一帧在飞时不投下一帧（见 _tick/_on_frame）
        self._grabber = _PreviewGrabber(self.frame_ready.emit)
        self.frame_ready.connect(self._on_frame)

        # ── 控制条：实例 + 读数 + 状态（放在右列顶部，不占左列预览空间）──
        # 旧版这条横贯窗口顶部，左端压住预览列：预览想按模拟器画面物理尺寸
        # 1:1 放，就得整窗更高才不触发等比微缩。现整条挪进右列第一行，
        # 左列从上到下全是预览盒，物理占比显示不再被读数条挤。
        # 流式布局：右列被分栏拖窄时「刷新/运行日志/定格」整颗折行，
        # 不再被压扁裁字（2026-10-05 用户实拍「按钮文字挤出去」）
        bar = FlowLayout()
        bar.addWidget(QLabel("实例:"))
        bar.addWidget(make_help_badge(
            "控制条：选一台正在跑的模拟器喂左侧预览（约 0.8 秒一帧，后台线程抓图，\n"
            "界面不卡）。「刷新」重列实例；「定格」冻住画面好仔细框选取色；\n"
            "左栏就是模拟器画面 1:1，框选/点选的坐标即安卓坐标。\n"
            "右侧两页：流程编排（连线搭流程）/ 模板采集（框选存图），共用这同一画面。"))
        self.cmb_instance = QComboBox()
        self.cmb_instance.setMinimumWidth(200)
        bar.addWidget(self.cmb_instance)
        self.btn_refresh = QPushButton("刷新")
        bar.addWidget(self.btn_refresh)
        # 日志窗本体在主窗口（全程序共用一份，所有 .log() 都往那儿写），
        # 这里只是召回：编流程看日志不用切回主界面翻窗口
        self.btn_log = QPushButton("运行日志")
        self.btn_log.clicked.connect(self._open_log_window)
        bar.addWidget(self.btn_log)
        # 定格：冻结预览画面好整帧看图/框选取色。勾选态本身就是状态指示，
        # 文字再切成「恢复」防看不出；换实例自动解冻（看的就是新实例了）。
        self.btn_freeze = QPushButton("定格")
        self.btn_freeze.setCheckable(True)
        self.btn_freeze.setToolTip(
            "冻结预览：不再刷新画面，停在当前这一帧，方便细看动画中间态、\n"
            "在定住的画面上框选/取色/点选目标位。\n"
            "再点一次恢复实时预览；切换实例会自动恢复。")
        self.btn_freeze.toggled.connect(
            lambda on: (self.btn_freeze.setText("恢复" if on else "定格"),
                        self._set_tick_status(
                            "已定格：画面冻结（再点「恢复」继续预览）")
                        if on else None))
        bar.addWidget(self.btn_freeze)
        bar.addStretch()
        self.lbl_coord = QLabel("鼠标: -")
        self.lbl_coord.setMinimumWidth(260)
        bar.addWidget(self.lbl_coord)
        self.lbl_status = QLabel("请选择实例")
        self.lbl_status.setMinimumWidth(320)
        bar.addWidget(self.lbl_status, stretch=2)

        # ── 左：共用预览（固定 1:1，左栏与画面同尺寸钉死，不缩不滚）──
        # 历史四轮：2/3 固定缩放嫌小 → 跟窗实时缩放毁框选手感 → 装不下才
        # 等比微缩（压画面，用户拍板不要）→ QScrollArea 滚动条（画面在框里
        # 滚来滚去，用户也不要）。现方案：ImageView 恒按模拟器画面物理像素
        # 1:1 显示（RenderWindow 物理尺寸，见 capture.find_render_window），
        # 左栏 setFixedSize 到同一尺寸——「左边区域跟着预览图一起固定」：
        # 分隔条拖不动它，模拟器画面变大它就跟着变大；窗口装不下时溢出裁掉，
        # 把窗口拉大即可看全（首显 _auto_resize 会自动拉到装得下）。
        # 显示倍率恒 1.0，坐标一切照旧：ImageView 内部把鼠标位置换算回
        # 540×960 基准，框选/点选零感知。
        self.preview = ImageView()
        self.preview.setToolTip(
            "左键两下 = 框选（第一下点错可右键重新来）\n"
            "右键：框选/点选进行中 = 取消当前操作；稳态 = 清空画面全部残留痕迹\n"
            "Esc = 同稳态右键（蓝框/黄框/绿框/红框/目标点一清而光）")
        self.preview_box = QWidget()
        pv = QVBoxLayout(self.preview_box)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(self.preview)

        # ── 右：流程编排 / 模板采集 分页（顶部互斥按钮切页，仿主窗口菜单栏模式）──
        # 三栏同屏时两块面板各挤一半：画布摆不开、采集表单+模板库也施展不开，
        # 合并成"一页一档"，当前页独占右列全宽。
        self.flow_panel = FlowPanel()
        self.tpl_panel = TemplatePanel()
        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self.flow_panel)
        self.page_stack.addWidget(self.tpl_panel)

        page_bar = QHBoxLayout()
        page_bar.setSpacing(4)
        self._page_buttons = []
        for i, (text, _p) in enumerate((("流程编排", self.flow_panel),
                                        ("模板采集", self.tpl_panel))):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setAutoExclusive(True)   # 互斥按钮天然单选，不用自己记选中
            b.clicked.connect(lambda _=False, k=i: self.page_stack.setCurrentIndex(k))
            self._page_buttons.append(b)
            page_bar.addWidget(b)
        page_bar.addStretch(1)
        self._page_buttons[0].setChecked(True)
        self.page_stack.currentChanged.connect(
            lambda i: self._page_buttons[i].setChecked(True))

        right_box = QWidget()
        rb = QVBoxLayout(right_box)
        rb.setContentsMargins(0, 0, 0, 0)
        rb.setSpacing(6)
        rb.addLayout(bar)          # 控制条：右列第一行
        rb.addLayout(page_bar)     # 切页按钮：右列第二行
        rb.addWidget(self.page_stack, stretch=1)

        # ── 两栏 QSplitter：左列整块给预览（物理占比），右列控制条+当前页 ──
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.preview_box)
        self.splitter.addWidget(right_box)
        # 预览自然宽（缩放后）不跟着拉伸；右列吃掉全部多余宽度
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)

        main = QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.addWidget(self.splitter, stretch=1)
        main.setSpacing(10)

        # ── 接线：面板 ↔ 共用预览 ──
        self.tpl_panel.attach_view(self.preview)
        self.flow_panel.attach_view(self.preview)
        self.preview.mouse_moved.connect(self._on_hover)
        # roi_drawn 按当前页分流：流程页「在预览上框选」→ flow_panel，其余采集页
        self.preview.roi_drawn.connect(self._route_roi_drawn)
        # click_target_set 按归属分发：见 _route_click_target
        self.preview.click_target_set.connect(self._route_click_target)
        # 多点取色点选态只有流程面板会发起，直连即可，不需要归属裁决
        self.preview.multi_point_clicked.connect(self.flow_panel.route_multi_point)

        # ── 接线：面板 → 共用状态栏 ──
        self.tpl_panel.status_message.connect(self.lbl_status.setText)
        self.flow_panel.status_message.connect(self.lbl_status.setText)

        # ── 联动：采到新图 → 刷流程下拉 + 若有回程票则回填 ──
        self.tpl_panel.template_saved.connect(self._on_template_saved)
        self.tpl_panel.templates_changed.connect(self._on_templates_changed)
        # ── 联动：流程「去采集」→ 预填归属 + 把采集表单拉到眼前 ──
        self.flow_panel.request_capture.connect(self._on_request_capture)
        # ── 联动：监视行「▶测这行」→ 拿采集页现成帧找图一次（不落手）──
        self.flow_panel.request_row_test.connect(self._on_request_row_test)
        self.flow_panel.request_finger_test.connect(self._on_request_finger_test)
        # ── 联动：流程「▶ 测试」→ 本窗口起 FlowTestWorker（跑当前画布快照）──
        self.flow_panel.test_requested.connect(self._on_test_requested)
        self.flow_panel.multi_test_requested.connect(self._on_multi_test_requested)
        self.flow_panel.test_one_requested.connect(self._on_test_one_requested)
        self.flow_panel.test_from_requested.connect(self._on_test_from_requested)
        self.flow_panel.test_stop_requested.connect(self._on_test_stop)
        # 流程事件（保存/新建/改名/删除/导入）实时转发主窗口对账
        self.flow_panel.flow_saved.connect(self.flow_saved.emit)
        # 「加入任务栏」：流程 → 工作台 → 主窗口 逐层转发
        self.flow_panel.add_to_queue.connect(self.add_to_queue.emit)
        self._test_worker = None   # 当前试跑线程（同时只允许一个）
        self._multi_test_dlg = None   # 多开测试小窗（惰建；同时只允许一个）

        # ── 实例 / 定时器 ──
        self.cmb_instance.currentIndexChanged.connect(self._on_instance_changed)
        self.btn_refresh.clicked.connect(self._refresh_instances)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(800)

        self._refresh_instances()
        self._restore_geometry()
        # 首次放置挪到 showEvent —— 构造时布局未算（容器还是默认小尺寸），
        # 现在调会把预览错误压成最小倍；显示时 resizeEvent/showEvent 再放

        # ── 截图循环中检测目标变化：模拟器窗口被调大/调小 → 自动跟上 ──
        self._last_target = None
        self._fitted_once = False   # 首显是否拉过窗口（只拉一次，之后交用户手势）

    # ── 意图路由：框选/点选归属 ──
    def _route_roi_drawn(self, x, y, w, h):
        """兜底路：发起方都没在 ImageView 登记一次性回调时，roi_drawn 信号才会
        发到这里，按 roi_owner 分给流程页/采集页。正常链路（两面板 begin 时
        各自 set_roi_pick_callback）不经过这里。"""
        if self.preview.roi_owner == "flow":
            self.preview.roi_owner = None
            self.flow_panel.route_roi(x, y, w, h)
        else:
            self.tpl_panel.on_roi_drawn(x, y, w, h)

    def _open_log_window(self):
        """召回运行日志窗。本体只有一个（主窗口的 LogWindow，全程序 .log()
        都写进那一份），这里绝不另起一份——两份日志必然不同步。"""
        mw = self.parent()
        lp = getattr(mw, "log_panel", None) if mw is not None else None
        if lp is None:
            self.lbl_status.setText("找不到主界面的日志窗口")
            return
        recall_window(lp, focus=True)

    def view_flow(self, name: str):
        """主界面任务清单双击「查看流程」：切到流程编排页并载入该流程。
        画布有未保存修改时的丢弃确认由 FlowPanel 自己把关。"""
        self.page_stack.setCurrentIndex(0)
        self.flow_panel.edit_flow(name)

    def _route_click_target(self, x, y):
        """兜底路（同 _route_roi_drawn）：没登记一次性回调的点选才走到这里，
        按 ImageView.click_owner 分发给流程面板/采集面板。"""
        if self.preview.click_owner == "pick":
            self.flow_panel.route_click(x, y)
        else:
            self.tpl_panel.on_click_target_set(x, y)

    # ── 联动处理 ──
    def _on_request_capture(self, category: str, name: str = ""):
        """FlowPanel 点「去采集/重新采集」：预填模板库 + 切到采集页拉框选。
        name 非空 = 槽位已带图 → 采集页锁名进「覆盖同名」模式（保存前弹确认）。"""
        if category:
            self.tpl_panel.prefill_category(category)
        self.tpl_panel.prefill_replace(name)
        self.page_stack.setCurrentWidget(self.tpl_panel)
        self.tpl_panel.begin_capture()

    def _on_request_row_test(self, category: str, name: str):
        """「▶测这行」：拿采集页现成帧对这张图找一次（只报分/位置，不落手不点）。
        测图核心链（查库→读图→匹配）收在 TemplatePanel.test_on_frame，这里只
        拼状态栏一行字的结果文案，不再读私有 _frame、不再自己 find/match。"""
        fp = self.flow_panel
        if not name:
            fp.apply_row_result(category, name, "这行还没选图")
            return
        frame = self.tpl_panel.current_frame()
        if frame is None:
            fp.apply_row_result(category, name, "没有画面：先在右侧选实例，模拟器要在跑")
            return
        hit, result = self.tpl_panel.test_on_frame(frame, category, name)
        if hit is None:
            # test_on_frame 用 result 区分「库里没这张图」/「读不出图」两种死法（人话照旧）
            msg = (f"库里没这张图（{category}/{name} 死引用）" if result == "模板不存在"
                   else "模板图片读不出来（PNG 坏了？）")
            fp.apply_row_result(category, name, msg)
            return
        if hit.get("matched"):
            cx, cy = hit["x"] + hit["w"] // 2, hit["y"] + hit["h"] // 2
            co = hit.get("click_offset")
            tail = (f"＋图偏移({int(co[0])},{int(co[1])})→({cx + int(co[0])},{cy + int(co[1])})"
                    if co else "")
            fp.apply_row_result(category, name,
                                f"✓ 命中 {hit['score']:.3f} @({hit['x']},{hit['y']}) 中心({cx},{cy}){tail}")
        else:
            fp.apply_row_result(category, name,
                                f"✗ 没找到（最高 {hit['score']:.3f}／用时 {hit.get('ms', 0):.0f}ms）"
                                "——弹窗此刻没弹属正常，重采同名覆盖才是修图的路")

    def _on_template_saved(self, category: str, name: str):
        """新图入库：若流程面板有回程票（发起过「去采集」），回填到那个节点。
        回填内部已重建属性下拉；无票时下拉刷新交给 templates_changed 通道。"""
        self.flow_panel.apply_capture_result(category, name)

    def _on_request_finger_test(self, row: dict):
        """「▶ 试扫手指」：拿当前帧按该行参数跑 vision.detect_finger（只检测不点击）。
        检出 → 在预览上画手指框（绿框）+ 推算点击点（十字）+ 结果文案；
        没检出 → 结果文案给「怎么调」。同「▶测这行」一条纪律：现成帧、只读、
        绝不落手。"""
        fp = self.flow_panel
        frame = self._frame
        if frame is None:
            fp.apply_finger_result(row, "没有画面：先在右侧选实例、模拟器停在手指出现的界面")
            return
        try:
            from core import vision
            r = vision.detect_finger(
                frame,
                white_thresh=int(row.get("white_thresh", 200) or 200),
                dark_thresh=int(row.get("dark_thresh", 128) or 128),
                min_area=int(row.get("min_area", 300) or 300),
                max_area=int(row.get("max_area", 20000) or 20000))
        except Exception as e:
            fp.apply_finger_result(row, f"检测异常：{e}")
            return
        preview = self.preview
        preview.hit_rects = []
        preview.click_target_pos = None
        if r is None:
            fp.apply_finger_result(
                row, "✗ 没检出：画面里没捕捉到「白块+黑描边」。"
                     "调低「白≥」、调高「黑≤/面积」再看；或确认此刻手指确实在屏上。")
            preview.update()
            return
        if row.get("auto_extend"):
            px, py = vision.extend_click_auto(frame, r)
        else:
            ext = float(row.get("extend_px", 0) or 0)
            px, py = vision.extend_click(r, ext)
        preview.hit_rects = [(r["x"], r["y"], r["w"], r["h"])]
        preview.click_target_pos = (px, py)
        preview.update()
        fp.apply_finger_result(
            row, f"✓ 检出：{r['side']}伸入 · 块({r['x']},{r['y']},{r['w']}×{r['h']}) "
                 f"· 指尖{r['tip']} · 落点({px},{py})（预览上已画框+十字）")

    def _on_templates_changed(self):
        """模板库任何变化（保存/删除/改名/导入）：流程属性面板就地重建下拉，
        并转发给主界面（新增/删除库要实时反映到最左侧模板库列表）。"""
        self.flow_panel.on_templates_changed()
        self.libraries_changed.emit()

    # ── 流程试运行：起 FlowTestWorker，日志/步骤回显到本窗口状态栏 ──
    def _gate_reason(self, idx):
        """单台准入判定（多开版对每台都查）：返回可用的实例号，
        检查不过则返回 (None, 原因文案)。整流程测 / 单节点测 / 多开测共用一份。"""
        inst = ldplayer.get_instance(idx)
        if inst is None or not inst.running:
            return None, f"实例 {idx} 未运行，无法测试"
        # 正式挂机占用检查：两边点同一个窗口必撞车，必须拒。
        # 主窗口提供 busy_instances()（ui/main_window.py），拿不到就当没占用。
        mw = self.parent()
        busy = getattr(mw, "busy_instances", None) if mw is not None else None
        if callable(busy) and idx in busy():
            return None, f"实例 {idx} 正在挂机，请先停止该实例再测试"
        return idx, ""

    def _test_gate(self):
        """试运行的公共准入检查：返回可用的实例号，检查不过则播状态栏返回 None。
        整流程测 / 单节点测 共用一份（两份必漂移）。"""
        if self._test_worker is not None and self._test_worker.isRunning():
            self.lbl_status.setText("测试已在进行中，先点「■ 停止」")
            return None
        if self._multi_test_dlg is not None and self._multi_test_dlg.running():
            self.lbl_status.setText("多开测试已在进行中，先停掉它再单实例测试")
            return None
        idx = self._current_index()
        if idx is None:
            self.lbl_status.setText("请先在左上角选择模拟器实例")
            return None
        idx, reason = self._gate_reason(idx)
        if idx is None:
            self.lbl_status.setText(reason)
            return None
        return idx

    def _start_test(self, idx: int, graph: dict, tag: str, keep_marks: bool = False):
        """建 FlowTestWorker 并起跑（整流程/单节点共用）：三信号照接、锁编辑。
        keep_marks=True（单节点测）保留画布上其余节点的旧角标，只刷这一个。"""
        from bot.flow_test import FlowTestWorker
        w = FlowTestWorker(idx, graph, parent=self,
                           flow_name=self.flow_panel.flow_name)
        w.action_log.connect(self._on_test_log)
        w.step_changed.connect(self._on_test_step)
        w.node_states.connect(self.flow_panel.apply_test_results)
        w.finished_state.connect(self._on_test_done)
        self._test_worker = w
        self.flow_panel.set_test_running(True, keep_marks=keep_marks)
        self.lbl_status.setText(tag)
        # 每次试跑在主日志面板打一条分割线，区分最新一轮测试的输出
        mw = self.parent()
        lp = getattr(mw, "log_panel", None) if mw is not None else None
        if lp is not None:
            lp.log(f"──────── {tag} ────────", "INFO")
            # 每次试跑自动召回日志窗（用户拍板）：分隔线刚写进去，窗口跟着
            # 亮到前台才看得见过程。不带 focus——键盘焦点留在工作台，
            # 不打断手头编辑；recall_window 顺带解除最小化位（show 不自愈）。
            recall_window(lp)
        w.start()

    def _on_test_requested(self):
        idx = self._test_gate()
        if idx is None:
            return
        # 未保存修改直接跑当前画布内容：快照即所见，无需先保存
        if self.flow_panel._dirty:
            self.lbl_status.setText("⚠ 流程有未保存修改：测试跑的是当前画布内容（含未保存部分）")
        self._start_test(idx, self.flow_panel.graph, "开始测试整条流程…")

    def _on_test_one_requested(self, node: dict):
        """单节点测试：把选中那一个节点摘出来合成一张孤图（无连线 = 零入度
        入口，GraphStep 开局即点亮），跑的就是"这一步"本身。节点 dict 不用
        自己拷贝 —— FlowTestWorker 进线程先 deepcopy 整图。"""
        idx = self._test_gate()
        if idx is None:
            return
        solo = {"version": 2, "nodes": [node], "edges": []}
        desc = node.get("label") or node.get("type", "?")
        self._start_test(idx, solo, f"开始单测节点「{desc}」…")

    def _on_test_from_requested(self, node: dict):
        """「从此节点开始测」：裁掉选中节点的上游，只跑它和它的下游可达链。
        分支口/失败口/成环原样保留；被裁掉的节点在画布角标上显示 …。"""
        idx = self._test_gate()
        if idx is None:
            return
        from bot.tasks.flow import subgraph_from
        sub = subgraph_from(self.flow_panel.graph, node.get("id", ""))
        desc = node.get("label") or node.get("type", "?")
        self._start_test(idx, sub, f"开始从节点「{desc}」往下测…")

    # ── 多开测试：同一条流程在多台实例上各跑一遍 ──
    def _on_multi_test_requested(self):
        """「▶ 多开测试」：弹选实例小窗，勾中的每台各起一个 FlowTestWorker（错峰）。
        准入检查对**每一台**都查（复用 _gate_reason：在跑 / 正在挂机都挡掉并点名）。
        跑多开期间单实例「▶ 测试流程」按钮禁用（防两个 worker 撞同一台）。"""
        if self._test_worker is not None and self._test_worker.isRunning():
            self.lbl_status.setText("单实例测试已在进行中，先点「■ 停止」再开多开测试")
            return
        from ui.multi_test_dialog import MultiTestDialog
        # 候选实例：全列出来，逐台跑准入判定；不过的灰显点名原因（仍列出来让用户看得见）
        candidates = []
        for inst in ldplayer.list_instances():
            idx, reason = self._gate_reason(inst.index)
            candidates.append({
                "idx": inst.index,
                "label": f"[{inst.index}] {inst.title}",
                "selectable": idx is not None,
                "reason": reason,
            })
        if not any(c["selectable"] for c in candidates):
            self.lbl_status.setText("没有可测的实例（都没运行 / 都在挂机）")
            return
        # 惰建复用一个小窗（同一时刻只允许一个），每次开窗重灌内容
        if self._multi_test_dlg is None:
            self._multi_test_dlg = MultiTestDialog(self)
            self._multi_test_dlg.multi_started.connect(
                lambda: self.flow_panel.set_test_running(True))
            self._multi_test_dlg.multi_finished.connect(self._on_multi_test_finished)
            self._multi_test_dlg.log_message.connect(self._on_multi_test_log)
        dlg = self._multi_test_dlg
        if self.flow_panel._dirty:
            self.lbl_status.setText("⚠ 流程有未保存修改：多开测试跑的是当前画布内容（含未保存部分）")
        dlg.configure(self.flow_panel.graph, self.flow_panel.flow_name, candidates)
        recall_window(dlg, focus=True)

    def _on_multi_test_log(self, level: str, msg: str):
        """多开测试的日志透传主界面日志面板（消息已带实例号前缀）。"""
        self.lbl_status.setText(msg)
        mw = self.parent()
        lp = getattr(mw, "log_panel", None) if mw is not None else None
        if lp is not None:
            lp.log(msg, level)

    def _on_multi_test_finished(self):
        """多开测试全部收工：解锁单实例编辑/测试按钮。"""
        self.flow_panel.set_test_running(False)
        self.lbl_status.setText("多开测试结束")

    def _on_test_log(self, index: int, level: str, msg: str):
        """试跑日志：状态栏播一行（带实例号前缀防多实例混淆）；
        同时喂主界面日志面板（有落点才写，不强迫本窗口常驻显示全量日志）。"""
        self.lbl_status.setText(f"[测试{index}] {msg}")
        mw = self.parent()
        lp = getattr(mw, "log_panel", None) if mw is not None else None
        if lp is not None:
            lp.log(f"[流程测试] {msg}", level)

    def _on_test_step(self, index: int, step: str):
        self.flow_panel._set_status(f"测试中 · 步骤：{step}")

    def _on_test_done(self, index: int, state: str):
        self.flow_panel.set_test_running(False)
        self._test_worker = None
        icon = {"done": "✓ 完成", "failed": "✗ 失败", "stopped": "■ 已停止"}.get(state, state)
        tail = "　·　⚠ 画布有未保存修改，记得保存" if self.flow_panel._dirty else ""
        self.lbl_status.setText(f"流程测试结束（实例 {index}）：{icon}{tail}")

    def _on_test_stop(self):
        if self._test_worker is not None and self._test_worker.isRunning():
            self._test_worker.request_stop()
            self.lbl_status.setText("已请求停止，当前步骤跑完即退…")

    # ── 悬停读数（共用状态条）──
    def _on_hover(self, x, y):
        """悬停读数：x/y 是 540 基准坐标；坐标显示 + 取色都对基准空间做。
        _img 可能是原生帧：取色先换算到原生像素再索引，避免按基准坐标
        越界/错位（采集/流程面板只收基准坐标，零感知）。"""
        img = self.preview._img
        # 边界判定用基准空间：鼠标坐标换算后恒在 540×960 内
        if img is None or not (0 <= x < capture.BASE_WIDTH and 0 <= y < capture.BASE_HEIGHT):
            self.lbl_coord.setText(f"鼠标: ({x},{y})")
            return
        m = self.preview._magn
        b, g, r = (int(img[int(y * m), int(x * m), i]) for i in (0, 1, 2))
        self.lbl_coord.setText(f"鼠标: ({x},{y})  RGB({r},{g},{b})  #{r:02X}{g:02X}{b:02X}")

    # ── 实例管理 ──
    def _refresh_instances(self):
        self.cmb_instance.blockSignals(True)
        self.cmb_instance.clear()
        for inst in ldplayer.list_instances():
            # 下拉显示雷电编号直通（与主表格「序号」列同口径）
            self.cmb_instance.addItem(f"[{inst.index}] {inst.title}", inst.index)
        self.cmb_instance.blockSignals(False)

    def _current_index(self):
        if self.cmb_instance.count() == 0:
            return None
        return self.cmb_instance.currentData()

    def _on_instance_changed(self):
        self.btn_freeze.setChecked(False)   # 换实例 = 要看的是新画面，定格自动解除
        idx = self._current_index()
        if idx is None:
            return
        inst = ldplayer.get_instance(idx)
        if inst is not None and not inst.running:
            self.lbl_status.setText("实例未运行，去主界面启动")
        # 换了实例 → 它的 RenderWindow 物理尺寸可能不同，预览缩放重算一次
        self._on_target_changed()

    # ── 截图循环（唯一投递口，喂两块面板；慢活全在 _PreviewGrabber 后台线程）──
    def _tick(self):
        # 手动定格：整拍早退（连截图都不做，省那份开销），画面停在最后一帧
        if self.btn_freeze.isChecked():
            return
        # 框选/取点过程中冻结画面（两块面板共用同一个预览，状态在预览上）
        if self.preview.selecting or self.preview.draw_mode in (
                "roi", "click_target", "multi_point"):
            return
        if self._grab_busy:
            return    # 上一帧还在后台飞（adb 取帧 ~0.15s/帧，开机风暴期更久）：
                      # 这拍跳过等回帧，绝不排队攒旧帧、更绝不 GUI 线程现截
        idx = self._current_index()
        if idx is None:
            return
        inst = ldplayer.get_instance(idx)
        if inst is None or not inst.running:
            self._set_tick_status("实例未运行")
            return
        self._grab_busy = True
        self._grabber.post((idx, inst.top_hwnd))

    def _on_frame(self, idx, img, err):
        """后台回帧（Qt 排队进 GUI 线程）：这里只剩喂面板，全本地计算。
        2026-10-09 起帧恒为纯安卓基准图（无边框），不再裁图。"""
        self._grab_busy = False
        if err:
            self._set_tick_status(err)
            return
        if idx != self._current_index():
            return    # 飞行中换了实例：整帧作废
        if img is None:
            self._set_tick_status("取帧失败（实例未响应？adb 通道暂时不可用？）")
            return
        self._frame = capture.to_base(img)   # 多数帧本就是 540×960，to_base 原地返回
        self.preview.set_frame(self._frame)
        self.tpl_panel.on_frame(self._frame)
        self._set_tick_status(f"预览中 {self._frame.shape[1]}x{self._frame.shape[0]}")
        # 量模拟器画面物理尺寸；变化且窗口装得下 → 自动拉大露出（跟随窗口）
        self._on_target_changed()

    def _set_tick_status(self, msg: str):
        """截图循环的状态播报：内容没变就不写，免得每 800ms 把面板
        （采集/流程 status_message）刚发的消息盖掉。"""
        if msg != self._tick_status:
            self._tick_status = msg
            self.lbl_status.setText(msg)

    # ── 窗口几何还原/保存（统一走 ui.window_geometry）──
    def _restore_geometry(self):
        window_geometry.restore(self, self._cfg, "ui.workbench_geometry", (1700, 1000))
        self.setMinimumSize(900, 820)   # 两栏分页：不再要求三栏同屏，最小宽降下来
        sizes = self._cfg.get("ui.workbench_splitter")
        if isinstance(sizes, list) and len(sizes) == 2:
            self.splitter.setSizes([int(s) for s in sizes])
        else:
            self.splitter.setSizes([560, 1140])
        # 还原上次停在哪页（默认流程编排=0）。字库采集页已删：老配置存 2 会越界
        page = self._cfg.get("ui.workbench_page", 0)
        self.page_stack.setCurrentIndex(page if page in (0, 1) else 0)

    def _emulator_target(self):
        """当前实例的预览目标尺寸 = 安卓画面在屏幕上的【客户区】物理尺寸。
        用户看到的画面是雷电窗口客户区里那块 RenderWindow 子窗口（真实渲染
        层，纯像素不含边框），而不是整窗 GetWindowRect（含边框/非客户区，
        差出几像素到十几像素）。find_render_window 直接量那块子窗口的客户区
        坐标+尺寸，就是"所见画面"的确实真值 —— 预览按它放，1:1 对上模拟器
        画面，边框那点差距自然消失。实例没跑/量不到回落基准 540×960。"""
        idx = self._current_index()
        if idx is not None:
            inst = ldplayer.get_instance(idx)
            if inst is not None and inst.running:
                geo = capture.find_render_window(inst.top_hwnd)
                if geo is not None:
                    return int(geo["width"]), int(geo["height"])
        return capture.BASE_WIDTH, capture.BASE_HEIGHT

    def _place_preview(self):
        """把预览钉在 1:1，并让左栏跟着画面一起钉死：显示倍率恒 1.0，
        ImageView 按帧内容倍率 _magn 把 RenderWindow 物理像素铺满控件
        （每像素与模拟器画面对应），preview_box 再 setFixedSize 到同一尺寸
        —— 左右分隔条因此拖不动（min==max），左栏完全由画面尺寸决定。
        左栏变大后窗口装不下就溢出裁掉，靠 _auto_resize 拉窗或手动放大窗口。
        坐标换算照旧走 ImageView 内部比例，框选/点选零感知。"""
        if not hasattr(self, "preview_box"):
            return
        self.preview.set_display_scale(1.0)
        self.preview_box.setFixedSize(self.preview._disp_w, self.preview._disp_h)

    def _auto_resize(self):
        """自动拉大工作台窗口，让装得下 1:1 预览 + 右列（首次显示、模拟器
        尺寸变化时调用）。只放大不缩小；左栏尺寸由画面钉死不可让，窗口已顶到
        屏幕边缘还装不下就保持现状（画面溢出被裁，用户手动放大窗口即可）。"""
        if not hasattr(self, "preview_box"):
            return
        tw, th = self._emulator_target()
        fixed_w = max(0, self.width() - self.preview_box.width())
        fixed_h = max(0, self.height() - self.preview_box.height())
        M = 24  # 富余：分隔条把手 + 布局边距
        need_w, need_h = fixed_w + tw + M, fixed_h + th + M
        if self.width() >= need_w and self.height() >= need_h:
            return  # 已够大，不动
        # 可取屏幕区域：offscreen/无屏环境兜底大值，只影响拉大上限
        scr = self.screen().availableGeometry() if self.screen() else QRect(0, 0, 1920, 1080)
        room_w, room_h = min(need_w, scr.width()), min(need_h, scr.height())
        if room_w <= self.width() and room_h <= self.height():
            return  # 屏幕都不够，保持现状
        self.resize(room_w, room_h)
        self._place_preview()

    def _clamp_into_screen(self):
        """把窗框（含标题栏）拽回任务栏以上的可用区，每次 showEvent 都摆正。
        病根一：restore 的几何越界时 Windows 不管（它只在手动拖标题栏时避让），
        底边伸进任务栏 → 最底一排按钮被压住；顶边越界 → 标题栏整条顶出屏外，
        最小化/关闭按钮再也够不着（实测 config 存了 y=-31 的越界几何）。
        病根二（上一版钳位自己踩的坑）：顶层窗 x()/y()/setGeometry 是【客户区】
        坐标系，frameGeometry() 才是含标题栏的【窗框】系，两者差一条标题栏。
        拿客户区坐标对可用区钳位，钳完客户区贴屏幕顶、标题栏照样在屏外。
        这里全程量窗框、位移用增量（move 加差值，坐标系无关，不会串位）。
        置顶救不了这病（任务栏浮在一切窗口之上），钳位才是根因修复；
        最大化跳过（那是用户主动铺满，别对着干）。"""
        if self.isMaximized():
            return
        scr = self.screen().availableGeometry() if self.screen() else QRect(0, 0, 1920, 1080)
        fg = self.frameGeometry()
        if fg.width() > scr.width() or fg.height() > scr.height():
            # resize 吃的是客户区尺寸：越界量要从框/客户差里扣
            deco_w = fg.width() - self.width()
            deco_h = fg.height() - self.height()
            self.resize(max(1, min(self.width(), scr.width() - deco_w)),
                        max(1, min(self.height(), scr.height() - deco_h)))
            fg = self.frameGeometry()   # 改尺寸可能挪位置，重读
        dx = dy = 0
        if fg.left() < scr.left():
            dx = scr.left() - fg.left()
        elif fg.right() > scr.right():
            dx = scr.right() - fg.right()
        if fg.top() < scr.top():
            dy = scr.top() - fg.top()
        elif fg.bottom() > scr.bottom():
            dy = scr.bottom() - fg.bottom()
        if dx or dy:
            self.move(self.pos() + QPoint(dx, dy))

    def _on_target_changed(self):
        """截图循环每拍调用：重钉预览（幂等），并在模拟器画面尺寸【变化】时
        拉大窗口跟上。注意 _place_preview 必须每拍跑，不能只挂在"目标变了"
        分支后面——校准完成那一拍目标尺寸没变、帧却从 540 基准跳到原生物理，
        左栏钉的尺寸会慢一拍（实测漏点）。拉窗只在目标真变了才做，不跟用户
        的手势过不去（首显拉窗由 showEvent 负责）。"""
        target = self._emulator_target()
        self._place_preview()
        if self._last_target == target:
            return
        self._last_target = target
        self._auto_resize()

    def resizeEvent(self, event):
        """窗口尺寸变化：只按当前空间放置预览，绝不自动拉大/弹回
        （用户拖小窗口是他的选择）。构造时布局未就绪跳过。"""
        super().resizeEvent(event)
        if hasattr(self, "preview_box"):
            self._place_preview()

    # ── 关闭 ──
    def closeEvent(self, event):
        """未保存流程在这里统一拦一次；确认可关才停表（拦下时窗口还可见，
        表停了预览就死了）。顺带存窗口几何与栏宽比例。"""
        if not self.flow_panel.confirm_discard():
            event.ignore()
            return
        # 试跑线程随窗口收摊：请求停止并等它退出（最多 3s），
        # 否则线程往已销毁的窗口 emit 会在后台炸
        if self._test_worker is not None and self._test_worker.isRunning():
            self._test_worker.request_stop()
            self._test_worker.wait(3000)
        # 多开测试各台 worker 同样收摊（掐全部 + 逐台有界等退，防孤儿线程 emit 到死窗口）
        if self._multi_test_dlg is not None:
            self._multi_test_dlg.shutdown(3000)
        self.timer.stop()
        window_geometry.save(self, self._cfg, "ui.workbench_geometry")
        self._cfg.set("ui.workbench_splitter", self.splitter.sizes())
        self._cfg.set("ui.workbench_page", self.page_stack.currentIndex())
        self._cfg.save()
        self.closed.emit()  # 通知主窗口：流程可能已改，刷新动态下拉框
        event.accept()

    def hideEvent(self, event):
        """隐藏时停表省 CPU（非模态窗口可能被切到后面长期闲置）。"""
        self.timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # 本窗有主人，默认无任务栏按钮、最小化会缩成迷你标题栏飘屏——
        # 每次显示重补 WS_EX_APPWINDOW（详见 core.win_utils.ensure_taskbar_button）；
        # 再补专属【窗口级】任务栏身份，否则与主窗同组被合并成一个按钮、
        # 点按钮只弹预览不最小化（详见 core.win_utils.set_window_app_id）
        hwnd = int(self.winId())
        win_utils.ensure_taskbar_button(hwnd)
        win_utils.set_window_app_id(hwnd, win_utils.APPID_WORKBENCH)
        if not self.timer.isActive():
            self.timer.start(800)
        # 首次显示：布局已就绪，按用户要求"空间不够就加大空间"——把窗口/分栏
        # 拉到刚好露出模拟器客户区 1:1 预览（只放大不缩小，只此一次；
        # 屏幕/空间真不够则交给 _place_preview 微缩兜底）。之后窗口尺寸
        # 由用户接手，绝不自动弹回。
        if not self._fitted_once:
            self._fitted_once = True
            self._auto_resize()
        # 摆正到任务栏之上（含 _auto_resize 刚放大的那一次），再量预览
        self._clamp_into_screen()
        self._place_preview()
