"""
ui/template_tool.py — 模板采集面板 + 预览控件

TemplatePanel 是「采模板」工作区（嵌在三栏工作台 ui/workbench.py 右栏，
采集表单在上、模板库在下，常驻可见）：
- 采集表单（模板库、名称、阈值、保存并测试、找图区域、点击坐标、放大镜）
- 模板库（按任务目录分组，缩略图 + 使用者，点击自动测试）
预览 ImageView、实例选择、截图定时器由工作台外壳共用；本面板通过
attach_view() 接共用预览的信号、on_frame() 收画面。对流程面板的联动走
两个信号，各管一职：template_saved（采完一张图、带名字）专供「去采集」的
自动回填（有回程票时外壳转 apply_capture_result）；templates_changed（模板库
有任何变化：保存/删除/改名/移动/导入）让流程面板就地重建模板下拉——三栏同屏后面板
不再被藏，旧的"脏标志延迟重建"机制已整个作废。改名/移库还会经
template_deps 的写侧传播把 config 里的引用一并改写。

ImageView 由工作台外壳共用（框选模板 / 点选坐标 / 取色）。
默认 1:1 显示；工作台外壳可调 set_display_scale 让它等比缩放，
缩放只发生在"显示"和"鼠标读回"两个边界，
一切对外坐标仍是安卓基准（540×960）。

任务↔模板绑定改版：分类下拉 = 已注册任务 + 磁盘孤儿目录，
不再让用户手填分类名（孤儿分类的来源）。每个模板显示"被哪些任务用"，
数据由 bot/template_deps.collect_refs() 现算，不存账本（两份必漂移）。

一键流：框选 → 填名 → 保存并测试 → 自动清空框选 → 继续采下一个
"""

import os

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bot import template_deps
from core import capture, template_manager, vision
from core.config_manager import get_config
from ui.confirm import confirm
from ui.help_badge import make_help_badge
# numpy→Qt 图像换算统一在 ui/image_util（原本文件的两份定义迁入那里，这里别名引回，调用点不动）
from ui.image_util import numpy_to_qimage as _numpy_to_qimage, numpy_to_qpixmap as _numpy_to_qpixmap

# 树形模板库的节点类型标记（QTreeWidget 混装分组节+条目节，按 type 区分）
_TYPE_GROUP = QTreeWidgetItem.UserType + 1
_TYPE_TPL = QTreeWidgetItem.UserType + 2

# 模板名前缀预设：命名规范「前缀_描述」，模板库列表一眼看出这张图是干什么的。
# 按现有模板的习惯整理（弹窗_在线奖励 / 七日签到_签到按钮 / 游戏图标）。
# 锚点 = 认界面的"身份证"（永远不变的固定局部，如标题/角落 logo），
#      认路判定用它，和"按钮/图标"这种要点的区分开。
# 前缀已搬进 config（"_config_prefixes"），可在采集面板「管理前缀」增删，
# 这里只留默认兜底（写入 config 用，初次运行后以 config 为准）。
NAME_PREFIXES = ("弹窗", "窗口", "关闭窗口", "锚点", "标签", "红点",
                 "按钮", "图标", "入口", "文字", "状态", "奖励", "背景")
_NO_PREFIX = "（不加）"


def _config_prefixes() -> list:
    """从 config 读前缀列表（用户可增删），缺省回退内置默认。"""
    raw = get_config().get("template_prefixes")
    ok = [p for p in (raw or []) if isinstance(p, str) and p.strip()]
    return ok if ok else list(NAME_PREFIXES)


# 模板库树缩略图缓存：键 (账本file, 文件mtime, 尺寸档) → (QIcon|None, 是否抠图)。
# 整树重建（删除/改名/保存后刷列表都要重画）过去逐项「读图→转 QPixmap→扫 alpha」，
# 几百项一遍就是肉眼发黏。mtime 变（重采覆盖）自动算新键；删掉的项在剪枝步清出。
# ponytail: 不持久化——QIcon 不可序列化，且重建缓存只付一次解码，值当。
_icon_cache: dict[tuple, tuple] = {}


def _tree_icon(t: dict) -> tuple:
    """取（或现做）一张模板在库树里的 (QIcon|None, 有抠图透明)。"""
    f = t.get("file")
    key = None
    if f:
        try:
            key = (f, os.stat(os.path.join(str(template_manager.TEMPLATES_DIR),
                                           f)).st_mtime_ns, 80)
        except OSError:
            key = None          # 文件没了/读不了：不缓存，下面照样返回 None
    if key in _icon_cache:
        return _icon_cache[key]
    icon, matte = None, False
    tpl_img = template_manager.load_template_image(t)
    if tpl_img is not None:
        icon = QIcon(_numpy_to_qpixmap(tpl_img, max_size=80))
        # 抠图标记现场从 PNG alpha 派生，不写账本（透明度=唯一事实来源）
        matte = vision.matte_of(tpl_img) is not None
    if key is not None:
        _icon_cache[key] = (icon, matte)
    return icon, matte


class ImageView(QWidget):
    """
    图片显示 + 两次点击取角框选控件。
    - set_frame(img)：放入一帧 numpy 图片
    - selection：完成的框选 (x, y, w, h)（图片坐标 = 安卓坐标）
    - hit_rects：测试匹配的命中框列表（绿色，测试全部时叠加多个）
    - roi_rect：找图区域框（黄色虚线）
    - 残留痕迹清空：稳态右键画面 或 Esc → clear_overlays()（蓝/黄/绿框、
      目标点、第一角十字一清而光；只清视觉不动数据，框选进行中右键仍是取消当前）
    - mouse_moved 信号：实时报告鼠标所在图片坐标
    - roi_drawn 信号：区域框选完成
    - multi_point_clicked 信号：draw_mode="multi_point" 期间每次单击发一点，
      不退模式（右键结束并清掉洋红记点标记）
    """

    mouse_moved = Signal(int, int)
    roi_drawn = Signal(int, int, int, int)  # 两次点击完成区域框选 (x, y, w, h)
    selection_done = Signal(int, int, int, int)  # 模板框选（蓝框）完成，供「自动贴边」挂钩
    click_target_set = Signal(int, int)  # 单次点击设置固定点击坐标 (x, y)
    multi_point_clicked = Signal(int, int)  # 多点取色点选态：每单击发一次 (x, y)，不退模式

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img = None
        self._corner1 = None      # 第一角（图片坐标），None = 未开始
        self._hover = None        # 当前鼠标位置（画预览虚线用）
        self.selection = None     # 已完成的框选 (x, y, w, h)
        self.hit_rects = []       # 匹配命中框列表 [(x, y, w, h), ...]（测试全部叠加多个）
        self.roi_rect = None      # 识别范围框（找图区域显示用）(x, y, w, h)
        self.click_target_pos = None  # 固定点击坐标标记 (x, y)
        self.multi_marks = []         # 多点取色已记点标记 [(x, y), ...]（仅点选态期间显示）
        # Esc 清痕要收键盘事件：点击即聚焦（不掺 Tab 焦点序，输入面板照常走）
        self.setFocusPolicy(Qt.ClickFocus)
        self.draw_mode = "template"  # "template" / "roi" / "click_target" / "multi_point" / "none"（只读）
        # 一次性模式（click_target/roi）完成后回到的稳态。三栏常驻后不再有
        # "采模板/编流程"全局模式，稳态恒为 template（随时可框选采集）。
        self.default_mode = "template"
        # ── 等比缩放显示 ──
        # 内部所有几何量（selection/roi_rect/click_target_pos/hit_rects）一律存
        # 图片=安卓坐标（540×960 基准空间）；原生倍数只发生在"显示"和"鼠标读回"
        # 两个边界：
        #   工作台定"目标显示倍率"（1:1 就是 1.0）；ImageView 按帧内容定"原生
        #   倍数 _magn"（RenderWindow 物理尺寸/基准尺寸）。
        #   paintEvent 先按 _target_scale 画图（原生帧乘 target 恰好填满控件
        #     → 每显示像素 = 模拟器 1 物理像素，1:1 零插值），叠加框再补乘
        #     _scale(=target×magn) 落位；
        #   _to_img() 把鼠标控件坐标除回基准空间；放大镜/取样按 _magn 乘法
        #     采样原生帧。
        # 控件固定尺寸 = 基准 × _target_scale × _magn；默认全 1 时零改变。
        self._target_scale = 1.0      # 外壳设置的用户目标显示倍率
        self._magn = 1.0              # 当前帧内容：物理尺寸/基准尺寸（原生帧才有提升）
        self._disp_w = capture.BASE_WIDTH
        self._disp_h = capture.BASE_HEIGHT
        self._scale = 1.0             # 真实显示因子 = _target_scale × _magn
        # click_target 事件归属方（"capture"=采集记点击坐标 / "pick"=流程取坐标）。
        # 三栏同屏后没有全局模式，谁最后进入 click_target 态谁拥有这一次点选，
        # 外壳（workbench）按此字段把 click_target_set 分发给对应面板。
        self.click_owner = None
        # roi 框选归属方（"flow"=流程节点限域框选，其余空=采集页框选）。
        # 与 click_owner 同理：一次性框选，发起方声明归属，成框/取消即作废。
        self.roi_owner = None
        # 一次性取点/框选回调（发起方在 begin 时直连登记，完成即送达作废）。
        # 有回调时**不再**发对应信号——送达只走一条路，绝不双份；没登记回调的
        # 老调用方（外壳按 owner 路由的兜底路）照常收 roi_drawn/click_target_set。
        self._point_pick_cb = None
        self._roi_pick_cb = None
        self.setMouseTracking(True)
        self.setFixedSize(capture.BASE_WIDTH, capture.BASE_HEIGHT)
        # 放大镜（跟手悬浮）：跟随鼠标浮在指针右下方，中心像素 = 指针所指，
        # 镜上直标坐标+RGB（取色不用瞟状态栏）。滚轮切倍率 2x/4x/8x。
        # 常驻置顶小窗、鼠标穿透；指针移出画面自动隐藏；画面边缘自动翻转防出界。
        self.magnifier = QLabel("放大镜", None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.magnifier.setObjectName("magnifier")   # 样式走全局主题
        self.magnifier.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.magnifier.setScaledContents(False)
        self._mag_size = 160          # 放大镜边长
        self._mag_zoom = 2            # 当前倍率 2/3/4（滚轮切，默认 2x——X4 太大）
        self._mag_zoom_idx = 0
        self._mag_zooms = [2, 3, 4]   # 倍率档
        # 内容=裁图 QPixmap，状态文字（坐标/RGB/倍率）放放大镜控件上的标签
        self._mag_lbl = QLabel(self.magnifier)
        self._mag_lbl.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        self._mag_lbl.setStyleSheet("color:#fff; background:rgba(0,0,0,140);"
                                    "padding:2px 4px; border-radius:2px;")
        self._mag_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.magnifier.hide()
        self.mouse_moved.connect(self._update_magnifier)
        self._mag_center = None       # 当前镜心（图片坐标），用于状态显示
        self._mag_off = (24, 16)      # 镜离指针的偏移（稍向右下挪，让开手指）
        self._mag_screen_margin = 8

    def set_display_scale(self, scale: float):
        """外壳按显示目标调用：等比缩放画面到 scale 显示。
        上限不再封 1.0——镜像模拟器窗口被拖大时（RenderWindow 物理尺寸超过
        基准 540×960），预览按同比例放大，所见（截图）与模拟器画面一致。

        注意：scale 是【目标倍率】（基准空间），与帧内容无关。真实显示因子
        = scale × _magn（原生帧时 1:1 显示）；set_frame 与 set_display_scale
        相互独立——set_frame 不碰目标倍率，set_display_scale 不感知帧形状，
        控件大小总是 scale × _magn。"""
        scale = max(0.1, float(scale))
        self._target_scale = scale
        d = scale * self._magn
        self._scale = d
        # round 而不是 int：原生帧倍率均值与真实比例差 0.03%，int 截断会掉 1px
        # （工作台旧代码同坑：int(1955.78)=1955，round=1956 精确对上帧尺寸）
        self._disp_w = int(round(capture.BASE_WIDTH * d))
        self._disp_h = int(round(capture.BASE_HEIGHT * d))
        self.setFixedSize(self._disp_w, self._disp_h)
        self.update()

    def _to_img(self, cx: int, cy: int) -> tuple[int, int]:
        """控件坐标 → 图片坐标（除以真实显示因子）。缩放/放大显示下鼠标命中处
        要换算回 540×960 空间，才能保证 selection/click 等一切落库/绘制量都是
        安卓坐标。"""

        d = self._scale
        if d == 1.0:
            return cx, cy
        return int(cx / d), int(cy / d)

    @property
    def selecting(self) -> bool:
        """是否正在框选过程中（第一角已点、第二角未点）"""
        return self._corner1 is not None

    def set_frame(self, img):
        """放入一帧 numpy 图片。图片坐标永远 = 540×960 基准空间（识别/采集
        的几何量都用它）；预览 1:1 需要的是"显示"层的原生物理像素，所以：
        - 帧是原生帧（Shape 与基准不成比例）→ 记 _magn = 物理尺寸/基准尺寸，
          控件尺寸成到显示因子 _scale×_magn；
        - 帧是基准帧（540×960）→ _magn = 1.0，行为完全等同旧版。
        set_frame 与 set_display_scale 相互独立：set_frame 不碰用户设置的目标
        倍率，set_display_scale 不感知帧形状——控件大小总是两者乘积。"""
        if img is None or img.shape[1] == capture.BASE_WIDTH and img.shape[0] == capture.BASE_HEIGHT:
            self._magn = 1.0
        else:
            # 与基准同长宽比的原生帧（RenderWindow 物理像素，540×960 的 n 倍）
            # → 记倍率，坐标/几何仍按 540 空间算。长宽比不必精确相等
            # （实测拖大后 1100×1956：2.0370/2.0375，差 0.03%），
            # 容差内取均值即可，1 像素级误差肉眼不可见。
            if img.shape[0] > 0 and img.shape[1] > 0:
                scale_w = img.shape[1] / capture.BASE_WIDTH
                scale_h = img.shape[0] / capture.BASE_HEIGHT
                if abs(scale_w - scale_h) < 0.02:
                    self._magn = (scale_w + scale_h) / 2
                else:
                    self._magn = 1.0
            else:
                self._magn = 1.0
        self._img = img
        # 重算控件尺寸：新帧的原生倍数变化时，显示尺寸跟着变（1:1 保持）
        # 但_scale 不动——目标倍率是用户的设置，不受帧内容影响
        self.set_display_scale(self._target_scale)
        self.update()

    def _clamp(self, x, y):
        """坐标钳制到 540 基准空间（图片坐标恒在基准框内，与 _img 是否原生
        帧无关——原生帧的物理像素由 _magn 乘法换算时肉眼安全，钳在基准上
        才不会让坐标链混进原生尺寸）。"""
        if self._img is None:
            return 0, 0
        return (min(max(x, 0), capture.BASE_WIDTH - 1),
                min(max(y, 0), capture.BASE_HEIGHT - 1))

    def _update_magnifier(self, x, y):
        """跟手放大镜：指针在画面内，放大镜浮在指针右下方，中心像素=指针所指，
        镜内直标坐标+RGB+倍率；越界/无图隐藏。出屏自动翻转（边距 _mag_screen_margin）。"""
        if self._img is None or x < 0 or y < 0 or y >= self._img.shape[0] or x >= self._img.shape[1]:
            self.magnifier.hide()
            self._mag_center = None
            return
        img = self._img
        # 原生帧时（_magn>1）x/y 是 540 基准坐标，放大镜按像素级精度采样：
        # 先换算到原生像素再裁，取色看的就是真实像素（在 540 空间拉倍率
        # 采的是插值像素——原生帧要逐像素看，不能插值）。
        xm, ym = int(x * self._magn), int(y * self._magn)
        if not (0 <= xm < img.shape[1] and 0 <= ym < img.shape[0]):
            self.magnifier.hide()
            self._mag_center = None
            return
        h, w = img.shape[:2]
        # 裁图跟随倍率：160px 显示 ÷ 倍率 = 每边取多少原像素
        half = self._mag_size // (2 * self._mag_zoom)
        x0, x1 = max(0, xm - half), min(w, xm + half)
        y0, y1 = max(0, ym - half), min(h, ym + half)
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            self.magnifier.hide()
            self._mag_center = None
            return
        # INTER_NEAREST：最近邻放大，像素保真不模糊（取色要看清单像素）
        zoom = cv2.resize(crop, (crop.shape[1] * self._mag_zoom,
                                 crop.shape[0] * self._mag_zoom),
                          interpolation=cv2.INTER_NEAREST)
        # 中心线：指针所在像素在放大图里的位置
        cx = (xm - x0) * self._mag_zoom
        cy = (ym - y0) * self._mag_zoom
        cv2.line(zoom, (cx - 8, cy), (cx + 8, cy), (0, 0, 255), 1)
        cv2.line(zoom, (cx, cy - 8), (cx, cy + 8), (0, 0, 255), 1)
        self.magnifier.setPixmap(QPixmap.fromImage(_numpy_to_qimage(zoom)))
        self.magnifier.setFixedSize(self._mag_size, self._mag_size)
        # 状态文字（底部黑条）：坐标显示 540 基准坐标（对外一致）
        b, g, r = (int(v) for v in img[ym, xm, :3])
        self._mag_lbl.setText(f"({x},{y}) #{r:02X}{g:02X}{b:02X}  x{self._mag_zoom}")
        self._mag_lbl.setFixedSize(self._mag_size - 4, 18)
        self._mag_lbl.move(2, self._mag_size - 20)
        self._mag_lbl.raise_()
        self._mag_center = (x, y)
        # 摆放：跟手指针右下方，出屏翻转
        # ⚠ x,y 是 540 基准坐标，mapToGlobal 要控件坐标：乘显示因子，否则原生帧放大时镜面飘离指针
        gp = self.mapToGlobal(QPoint(int(x * self._scale), int(y * self._scale)))
        ox = gp.x() + self._mag_off[0]
        oy = gp.y() + self._mag_off[1]
        scr = self.screen().availableGeometry() if self.screen() else QRect(0, 0, 1920, 1080)
        mm = self._mag_screen_margin
        if ox + self._mag_size > scr.width() - mm:
            ox = gp.x() - self._mag_off[0] - self._mag_size
        if oy + self._mag_size > scr.height() - mm:
            oy = gp.y() - self._mag_off[1] - self._mag_size
        self.magnifier.move(ox, oy)
        self.magnifier.show()

    def leaveEvent(self, event):
        """鼠标离开画面 → 放大镜立刻隐藏（移出后没有 mouseMove 事件进来，
        只能靠 leaveEvent 收尾，否则镜像悬浮会残留屏幕中央）。"""
        self.magnifier.hide()
        self._mag_center = None
        super().leaveEvent(event)

    def wheelEvent(self, event):
        """滚轮在画面空白处切放大镜倍率（2x/4x/8x 循环）。只读不阻碍框选。
        缺省：滚轮从来都向上滚才可换挡（向上=放大）。"""
        if self._img is not None and self._mag_center is not None:
            delta = event.angleDelta().y()
            if delta > 0:
                self._mag_zoom_idx = min(len(self._mag_zooms) - 1, self._mag_zoom_idx + 1)
            elif delta < 0:
                self._mag_zoom_idx = max(0, self._mag_zoom_idx - 1)
            self._mag_zoom = self._mag_zooms[self._mag_zoom_idx]
            cx, cy = self._mag_center
            self._update_magnifier(cx, cy)
            return
        super().wheelEvent(event)

    def set_point_pick_callback(self, cb):
        """click_target 完成的一次性回调 cb(x, y)：下一次成点即消费作废。
        流程取坐标/采集表单/属性窗都走这个唯一入口，不再绕外壳按 owner 嗅探；
        右键取消自动作废，传 None = 撤销。未登记时完成仍发 click_target_set 信号。"""
        self._point_pick_cb = cb

    def set_roi_pick_callback(self, cb):
        """roi 区域框选完成的一次性回调 cb(x, y, w, h)，语义同 set_point_pick_callback。"""
        self._roi_pick_cb = cb

    def clear_selection(self):
        """清空框选（一键流：保存后自动清空）"""
        self.selection = None
        self._corner1 = None
        self._hover = None
        self.update()

    def clear_overlays(self):
        """清空画面上全部残留痕迹：框选蓝框/识别区黄框/命中绿框/失配红框/
        目标点标记/第一角十字。只清视觉，不动画面帧、不动已写进节点或模板的
        任何数据（痕迹只是参考线）。稳态下右键画面或按 Esc 触发。"""
        self.selection = None
        self.roi_rect = None
        self.hit_rects = []
        self.click_target_pos = None
        self._corner1 = None
        self._hover = None
        self.update()

    def keyPressEvent(self, event):
        # Esc = 清空全部残留痕迹（与稳态右键同义，手不离键盘的快捷路）
        if event.key() == Qt.Key_Escape and self._img is not None:
            self.clear_overlays()
            event.accept()
            return
        super().keyPressEvent(event)

    # ── 鼠标：两次点击取角 / 单次点击设置坐标 ──
    def mousePressEvent(self, event):
        if self._img is None:
            return
        if event.button() == Qt.RightButton:
            cancel_active = (self._corner1 is not None
                             or self.draw_mode in ("click_target", "roi", "multi_point"))
            self._corner1 = None  # 右键取消第一角
            self._hover = None
            if self.draw_mode == "multi_point":
                # 右键结束多点取色点选态（发起面板的 route 收不到这一次，
                # 面板下次点按钮会重新发起，无残留状态）
                self.draw_mode = self.default_mode
                self.multi_marks = []
            elif self.draw_mode in ("click_target", "roi"):
                self.draw_mode = self.default_mode  # 右键取消一次性取点，回稳态
                if self.click_owner == "pick":
                    # 流程侧取消取点：收掉面板画的预匹配绿框/残留目标点，
                    # 不留孤儿叠加（采集侧的测试框不受此影响）
                    self.click_target_pos = None
                    self.hit_rects = []
                self.click_owner = None             # 取消点选：归属方作废
                self.roi_owner = None               # 取消框选：归属方作废
                self._point_pick_cb = None          # 一次性回调随会话作废：
                self._roi_pick_cb = None            # 留着会吃掉下一次无关动作
            elif not cancel_active:
                # 稳态右键（没有进行中的框选/点选可取消）→ 视作"清空全部残留痕迹"
                self.clear_overlays()
            self.update()
            return
        if event.button() != Qt.LeftButton:
            return
        if self.draw_mode == "none":
            return  # 只读态：不响应框选，防画出无用的蓝框
        # 控件坐标 → 图片坐标（缩放显示时除回比例；1:1 恒等）
        x, y = self._clamp(*self._to_img(event.pos().x(), event.pos().y()))

        # 多点取色点选态：每单击发一次坐标，不退模式（连着点连着记），右键结束
        if self.draw_mode == "multi_point":
            self.multi_point_clicked.emit(x, y)
            return

        # 单次点击模式：设置固定点击坐标
        if self.draw_mode == "click_target":
            self.click_target_pos = (x, y)
            self.draw_mode = self.default_mode
            cb, self._point_pick_cb = self._point_pick_cb, None   # 一次性：取走即作废
            if cb is not None:
                cb(x, y)                      # 回调送达就不发信号，杜绝双份投递
            else:
                self.click_target_set.emit(x, y)
            self.update()
            return

        if self._corner1 is None:
            # 第一下：定第一角
            self._corner1 = (x, y)
            self._hover = (x, y)
            self.selection = None  # 重新框选时清掉旧选择
            self.update()
            return
        # 第二下：两点成框
        end = (x, y)
        x0, y0 = min(self._corner1[0], end[0]), min(self._corner1[1], end[1])
        w, h = abs(end[0] - self._corner1[0]), abs(end[1] - self._corner1[1])
        self._corner1 = None
        self._hover = None
        if w >= 5 and h >= 5:  # 太小的框视为误触
            if self.draw_mode == "roi":
                # 区域模式：显示黄框，回到当前模式的稳态；送达走唯一一条路
                self.roi_rect = (x0, y0, w, h)
                self.draw_mode = self.default_mode
                cb, self._roi_pick_cb = self._roi_pick_cb, None
                if cb is not None:
                    cb(x0, y0, w, h)
                else:
                    self.roi_drawn.emit(x0, y0, w, h)
            else:
                self.selection = (x0, y0, w, h)
                self.selection_done.emit(x0, y0, w, h)
        self.update()

    def mouseMoveEvent(self, event):
        # 同样先换算回图片坐标：悬停读数取 RGB、放大镜裁图都按图片空间索引
        x, y = self._clamp(*self._to_img(event.pos().x(), event.pos().y()))
        self.mouse_moved.emit(x, y)
        if self._corner1 is not None:
            self._hover = (x, y)
            self.update()

    # ── 绘制 ──
    def paintEvent(self, event):
        painter = QPainter(self)
        if self._img is None:
            painter.fillRect(self.rect(), Qt.black)
            painter.setPen(Qt.gray)
            painter.drawText(self.rect(), Qt.AlignCenter, "等待画面加载…")
            return
        # 两级缩放绘制（顺序关键！）：
        #   画面用 _target_scale（用户目标显示倍率）画：原生帧（物理宽=540×m）
        #     乘 target 恰好填满控件（控件宽=540×target×m）→ 每显示像素=模拟器
        #     1 物理像素，1:1 零插值；基准帧（m=1）时等价旧行为。
        #   叠加框（selection/roi/命中/取点，基准 540 坐标）再乘 _scale
        #     （=target×m）落在物理位置——所以画完图再补乘一次。
        if self._target_scale != 1.0:
            painter.scale(self._target_scale, self._target_scale)
        painter.drawImage(0, 0, _numpy_to_qimage(self._img))
        if self._scale != 1.0:
            painter.scale(self._scale, self._scale)
        # 第一角：白色十字 + 虚线预览
        if self._corner1 is not None:
            cx, cy = self._corner1
            _pen = QPen(Qt.white, 2); _pen.setCosmetic(True)
            painter.setPen(_pen)
            painter.drawLine(cx - 6, cy, cx + 6, cy)
            painter.drawLine(cx, cy - 6, cx, cy + 6)
            if self._hover is not None:
                self._draw_rect(painter, (self._corner1, self._hover), Qt.white, dashed=True)
        # 框选：蓝色实线
        if self.selection is not None:
            x, y, w, h = self.selection
            self._draw_rect(painter, ((x, y), (x + w, y + h)), Qt.blue, dashed=False)
        # 命中：绿色实线（支持多目标叠加）
        for hr in self.hit_rects:
            x, y, w, h = hr
            self._draw_rect(painter, ((x, y), (x + w, y + h)), Qt.green, dashed=False)
        # 找图区域：黄色虚线
        if self.roi_rect is not None:
            x, y, w, h = self.roi_rect
            self._draw_rect(painter, ((x, y), (x + w, y + h)), Qt.yellow, dashed=True)
        # 多点取色已记点：洋红小十字（点选态期间实时显示，右键结束即清）
        if self.multi_marks:
            _pen = QPen(Qt.magenta, 2, Qt.SolidLine); _pen.setCosmetic(True)
            painter.setPen(_pen)
            for mx, my in self.multi_marks:
                painter.drawLine(mx - 5, my, mx + 5, my)
                painter.drawLine(mx, my - 5, mx, my + 5)
        # 固定点击坐标：红色十字 + 圆圈
        if self.click_target_pos is not None:
            tx, ty = self.click_target_pos
            _pen = QPen(Qt.red, 3, Qt.SolidLine); _pen.setCosmetic(True)
            painter.setPen(_pen)
            size = 15
            painter.drawLine(tx - size, ty, tx + size, ty)
            painter.drawLine(tx, ty - size, tx, ty + size)
            painter.drawEllipse(tx - size, ty - size, size * 2, size * 2)

    def _draw_rect(self, painter, pts, color, dashed):
        p1, p2 = pts
        pen = QPen(color, 2)
        pen.setStyle(Qt.DashLine if dashed else Qt.SolidLine)
        pen.setCosmetic(True)   # 恒 2px：原生帧放大时不被 scale 拉粗糊住小字
        painter.setPen(pen)
        x, y = min(p1[0], p2[0]), min(p1[1], p2[1])
        w, h = abs(p2[0] - p1[0]), abs(p2[1] - p1[1])
        painter.drawRect(int(x), int(y), int(w), int(h))


class TemplatePropsDialog(QDialog):
    """模板属性编辑弹窗（模板库右键「修改属性」）。

    只改影响识别的账本字段：匹配阈值 / 找图区域 roi / 固定点击坐标 / 相对点击偏移。
    刻意不含「名字/分类」——改名是断引用级操作，引用传播逻辑收口在右键
    「重命名」一条路径；也刻意不含图片本体/宽高——那要走重采同名覆盖。
    roi / 点击坐标各带一个启用勾选：勾掉 = 清除该项（存 None）。
    """

    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        cat, name = entry.get("category", ""), entry.get("name", "")
        self.setWindowTitle(f"属性 · {cat}/{name}")
        self.setMinimumWidth(340)
        form = QFormLayout(self)

        self.spn_thr = QDoubleSpinBox()
        self.spn_thr.setRange(0.60, 0.99)
        self.spn_thr.setSingleStep(0.01)
        self.spn_thr.setValue(float(entry.get("threshold", 0.85)))
        self.spn_thr.setToolTip("匹配相似度门槛：越高越严格越不易误认，越低越易找到。\n"
                                "测试报告里『实际最高分』接近阈值时最该微调这个。\n"
                                "可用范围 0.60~0.99：低于 0.60 必满屏误认（故意封底），\n"
                                "高于 0.99 几乎认不出（封顶层）；越界输入自动弹回边界。")
        form.addRow("匹配阈值", self.spn_thr)

        # ── 找图区域 roi ──
        roi = entry.get("roi")
        self.chk_roi = QCheckBox("限定区域")
        self.chk_roi.setToolTip("只在这个矩形里找图：更快、更不容易在别处误认。\n"
                                "取消勾选 = 全屏搜索。数值是画面像素坐标（基准 540×960）。")
        self.chk_roi.setChecked(bool(roi))
        self.spin_roi = []
        for i in range(4):
            s = QSpinBox()
            s.setRange(0, 99999)
            s.setMaximumWidth(64)
            self.spin_roi.append(s)
        if roi:
            for s, v in zip(self.spin_roi, roi):
                s.setValue(int(v))
        roi_row = QHBoxLayout()
        roi_row.addWidget(QLabel("x0"))
        roi_row.addWidget(self.spin_roi[0])
        roi_row.addWidget(QLabel("y0"))
        roi_row.addWidget(self.spin_roi[1])
        roi_row.addWidget(QLabel("x1"))
        roi_row.addWidget(self.spin_roi[2])
        roi_row.addWidget(QLabel("y1"))
        roi_row.addWidget(self.spin_roi[3])
        roi_row.addStretch(1)
        # 借用共用预览从画面上取点（弹窗非模态：点完回画面框两下，坐标自动填回）
        self.btn_roi_pick = QPushButton("画面框选")
        self.btn_roi_pick.setToolTip("弹窗不挡路：直接在工作台左侧画面上点两下框区域，\n"
                                     "坐标自动填进这里。右键画面 = 取消框选。")
        roi_row.addWidget(self.btn_roi_pick)
        form.addRow(self.chk_roi, roi_row)
        self.chk_roi.toggled.connect(lambda on: [s.setEnabled(on) for s in self.spin_roi])
        for s in self.spin_roi:
            s.setEnabled(bool(roi))

        # ── 固定点击坐标 click_target ──
        ct = entry.get("click_target")
        self.chk_ct = QCheckBox("固定点击")
        self.chk_ct.setToolTip("找到模板后固定点这个坐标，而不是模板中心。\n"
                               "适合「图标在 A 处但要点 B 才触发」的情况。取消勾选 = 点模板中心。")
        self.chk_ct.setChecked(bool(ct))
        self.spin_ct = []
        for i in range(2):
            s = QSpinBox()
            s.setRange(0, 99999)
            s.setMaximumWidth(64)
            self.spin_ct.append(s)
        if ct:
            for s, v in zip(self.spin_ct, ct):
                s.setValue(int(v))
        ct_row = QHBoxLayout()
        ct_row.addWidget(QLabel("x"))
        ct_row.addWidget(self.spin_ct[0])
        ct_row.addWidget(QLabel("y"))
        ct_row.addWidget(self.spin_ct[1])
        ct_row.addStretch(1)
        self.btn_ct_pick = QPushButton("画面点选")
        self.btn_ct_pick.setToolTip("弹窗不挡路：直接在工作台左侧画面上单击一点，\n"
                                    "坐标自动填进这里。右键画面 = 取消点选。")
        ct_row.addWidget(self.btn_ct_pick)
        form.addRow(self.chk_ct, ct_row)
        self.chk_ct.toggled.connect(lambda on: [s.setEnabled(on) for s in self.spin_ct])
        for s in self.spin_ct:
            s.setEnabled(bool(ct))

        # ── 相对点击偏移 click_offset（存进图：全体没自配偏移的使用者自动跟随）──
        co = entry.get("click_offset")
        self.chk_off = QCheckBox("跟随图偏移")
        self.chk_off.setToolTip("目标点离图标固定距离（手指指引图标点它指的地方）：\n"
                                "dx/dy 相对命中框中心，弹窗挪位置照样点得准。\n"
                                "存进这张图后，所有没自己采偏移的使用者（监视行/点模板节点）\n"
                                "自动继承；行/节点采了非 0 偏移的以它为准。")
        self.chk_off.setChecked(co is not None)
        self.spin_off = []
        for i in range(2):
            s = QSpinBox()
            s.setRange(-539, 539)
            s.setMaximumWidth(70)
            self.spin_off.append(s)
        if co is not None:
            for s, v in zip(self.spin_off, co):
                s.setValue(int(v))
        off_row = QHBoxLayout()
        off_row.addWidget(QLabel("dx"))
        off_row.addWidget(self.spin_off[0])
        off_row.addWidget(QLabel("dy"))
        off_row.addWidget(self.spin_off[1])
        off_row.addStretch(1)
        # 存量图补偏移的取量入口：与点模板节点「点选目标位算偏移」同套路
        # （预匹配定锚点绿框 → 点目标位 → 差值回填），点确定才存进图账本
        self.btn_off_pick = QPushButton("画面点选算偏移")
        self.btn_off_pick.setToolTip("先按当前画面认出这张图定绿框锚点，再单击要点的目标位：\n"
                                     "dx/dy = 目标点 − 框中心，自动填好，点「确定」存进图。\n"
                                     "画面里没这张图时改手动填。右键画面 = 取消点选。")
        self.btn_off_pick.clicked.connect(
            lambda: self.pick_fn and self.pick_fn("offset"))
        off_row.addWidget(self.btn_off_pick)
        form.addRow(self.chk_off, off_row)
        self.chk_off.toggled.connect(lambda on: [s.setEnabled(on) for s in self.spin_off])
        for s in self.spin_off:
            s.setEnabled(co is not None)

        # ── RGB 颜色校验 rgb_threshold（灰度命中后按颜色复核，拒绝"形状像颜色不同"）──
        # 灰度匹配只看明暗形状：红/蓝按钮、同款不同色的图标灰度分布可能几乎一样，
        # 灰度分会很高。勾上后对命中块逐通道比颜色，置信度低于门槛即作废。
        rt = entry.get("rgb_threshold")
        self.chk_rgb = QCheckBox("颜色校验")
        self.chk_rgb.setToolTip("灰度找到后，再核对命中处的颜色是否和模板一致：\n"
                                "颜色不符（红按钮误认成蓝按钮这类）的假命中被作废。\n"
                                "门槛越高越严格（0.9 左右可挡住明显异色，0.7 较宽松）；\n"
                                "取消勾选 = 只看形状不比颜色（老行为）。")
        self.chk_rgb.setChecked(rt is not None)
        self.spn_rgb = QDoubleSpinBox()
        self.spn_rgb.setRange(0.50, 0.99)
        self.spn_rgb.setSingleStep(0.01)
        self.spn_rgb.setValue(float(rt) if rt is not None else 0.80)
        self.spn_rgb.setEnabled(rt is not None)
        self.chk_rgb.toggled.connect(self.spn_rgb.setEnabled)
        form.addRow(self.chk_rgb, self.spn_rgb)

        # ── 只读信息（宽高/采集时间，改不了，仅告知）──
        info = f"{entry.get('width', '?')}×{entry.get('height', '?')} px"
        if entry.get("captured_at"):
            info += f"   采集于 {entry['captured_at']}"
        lbl_info = QLabel(info)
        lbl_info.setToolTip("图片尺寸改不了：要换图就在采集表单里重采同名模板，会自动覆盖。")
        form.addRow("图片", lbl_info)

        # accepted 由面板接管：先落盘保存再关窗（关窗前保存失败还能改数值重试）
        self.btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.btns.rejected.connect(self.reject)
        form.addRow(self.btns)

        # 「画面框选/点选」转发回调，由面板注入（弹窗自己不懂共用预览）
        self.pick_fn = None
        self.btn_roi_pick.clicked.connect(lambda: self.pick_fn and self.pick_fn("roi"))
        self.btn_ct_pick.clicked.connect(lambda: self.pick_fn and self.pick_fn("click"))

    def apply_picked_roi(self, x: int, y: int, w: int, h: int):
        """共用预览上框完区域 → 回填并自动勾选『限定区域』"""
        self.chk_roi.setChecked(True)
        for s, v in zip(self.spin_roi, (x, y, x + w, y + h)):
            s.setValue(int(v))

    def apply_picked_click(self, x: int, y: int):
        """共用预览上点选坐标 → 回填并自动勾选『固定点击』"""
        self.chk_ct.setChecked(True)
        self.spin_ct[0].setValue(int(x))
        self.spin_ct[1].setValue(int(y))

    def apply_picked_offset(self, dx: int, dy: int):
        """共用预览上点选算出的偏移（目标位 − 图中心）→ 勾上『跟随图偏移』回填"""
        self.chk_off.setChecked(True)
        self.spin_off[0].setValue(int(dx))
        self.spin_off[1].setValue(int(dy))

    def values(self) -> tuple[float, list | None, list | None, list | None, float | None]:
        """回读 (threshold, roi, click_target, click_offset, rgb_threshold)；
        未勾选的项返回 None（rgb_threshold 同理）。
        roi 手填可能 x0>x1，取 min/max 归一（与画面上框选的语义一致）。"""
        if self.chk_roi.isChecked():
            x0, x1 = sorted((self.spin_roi[0].value(), self.spin_roi[2].value()))
            y0, y1 = sorted((self.spin_roi[1].value(), self.spin_roi[3].value()))
            roi = [x0, y0, x1, y1]
        else:
            roi = None
        ct = ([s.value() for s in self.spin_ct] if self.chk_ct.isChecked() else None)
        co = ([s.value() for s in self.spin_off] if self.chk_off.isChecked() else None)
        rgb = (self.spn_rgb.value() if self.chk_rgb.isChecked() else None)
        return self.spn_thr.value(), roi, ct, co, rgb


class _MatteView(QWidget):
    """抠图预览：棋盘格底 + 抠剩的像素浮在上面，透明一眼看穿。
    左键=魔棒点背景（同色全抠）；右键=撤销最近一次点。整数倍放大看像素。"""

    seed_clicked = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img = None          # 原图 BGR
        self._keep = None         # bool 掩码 True=保留
        self._scale = 1
        self.setMouseTracking(True)

    def set_content(self, img: np.ndarray, keep: np.ndarray):
        self._img, self._keep = img, keep
        h, w = img.shape[:2]
        # 整数倍放大：预览控制在 ~260px 内，小图标至少 2 倍看得清像素
        self._scale = max(1, min(8, 260 // max(w, h, 1)))
        self.setFixedSize(w * self._scale, h * self._scale)
        self.update()

    def mousePressEvent(self, event):
        if self._img is None or event.button() not in (Qt.LeftButton, Qt.RightButton):
            return
        x, y = event.position().x() // self._scale, event.position().y() // self._scale
        if event.button() == Qt.LeftButton:
            self.seed_clicked.emit(int(x), int(y))
        else:
            self.seed_clicked.emit(-1, -1)   # 约定：(-1,-1) = 撤销最近一刀

    def paintEvent(self, event):
        p = QPainter(self)
        if self._img is None:
            return
        # 棋盘格（Photoshop 惯例：灰白格 = 透明），奇偶格交替
        cell = 8
        for gy in range(0, self.height(), cell):
            for gx in range(0, self.width(), cell):
                p.fillRect(gx, gy, cell, cell,
                           Qt.lightGray if (gx // cell + gy // cell) % 2 else Qt.white)
        rgba = np.dstack([cv2.cvtColor(self._img, cv2.COLOR_BGR2RGB),
                          np.where(self._keep, 255, 0).astype(np.uint8)])
        qimg = QImage(rgba.data, rgba.shape[1], rgba.shape[0],
                      rgba.shape[1] * 4, QImage.Format_RGBA8888).copy()
        p.drawImage(self.rect(), qimg)   # Qt 自己按整数倍拉伸，keepAspectRatio 无碍


class MatteDialog(QDialog):
    """抠图弹窗（采集前预抠 / 存量模板重抠共用）。

    交互：点一下背景色 → 全图容差内相似色全变透明（字缝里的同色背景一起抠净）；
    背景有多块不同色 → 再点一处，逐刀叠加；容差滑杆实时重算全部点。
    「一键去底」= 采样矩形边框主色自动抠，适合纯色底一步到位。
    抠完保存 → 带 alpha 的 BGRA 写进 PNG，识别自动切带遮罩匹配（见 core.vision）。

    ⚠ 抠图语义与识别精度直接挂钩：抠多了（伤目标）或抠少了（剩背景）都会影响
    分数，所以必须过预览确认再保存，不做"保存时顺手自动抠"的静默路径。
    """

    def __init__(self, img: np.ndarray, keep0: np.ndarray | None = None,
                 title: str = "抠图", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._img = img.copy()                 # BGR 原图（弹窗生命周期内不改）
        self._seeds: list[tuple[int, int]] = []
        self._keep0 = None if keep0 is None else keep0.copy()   # 重抠初始态（老 alpha）
        self.matte: np.ndarray | None = None  # accept 后的 BGRA；None=没抠/取消

        lay = QVBoxLayout(self)
        self.view = _MatteView()
        self.view.seed_clicked.connect(self._on_seed)
        row1 = QHBoxLayout()
        row1.addWidget(self.view)

        side = QVBoxLayout()
        side.addWidget(QLabel("在右侧画面上点一下背景色\n（右键 = 撤销最近一刀）"))
        tol_row = QHBoxLayout()
        tol_row.addWidget(QLabel("容差"))
        self.sld_tol = QSlider(Qt.Horizontal)
        self.sld_tol.setRange(5, 150)
        self.sld_tol.setValue(30)
        self.sld_tol.setToolTip("颜色差多少以内算「同一种背景」。抠不动就调大，\n"
                                "抠到目标了就调小。每次改动都会按已有点击点重算。")
        tol_row.addWidget(self.sld_tol, 1)
        self.lbl_tol = QLabel("30")
        tol_row.addWidget(self.lbl_tol)
        side.addLayout(tol_row)
        self.btn_auto = QPushButton("一键去底")
        self.btn_auto.setToolTip("采样模板矩形四周一圈的主色当背景色，自动抠掉。\n"
                                 "图标浮在纯色底上时一步到位；抠得不准再点色补刀。")
        self.btn_auto.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        side.addWidget(self.btn_auto)
        self.btn_reset = QPushButton("恢复（不抠）")
        self.btn_reset.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        side.addWidget(self.btn_reset)
        self.lbl_info = QLabel("")
        self.lbl_info.setWordWrap(True)
        side.addWidget(self.lbl_info)
        side.addStretch(1)
        row1.addLayout(side)
        lay.addLayout(row1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("保存抠图")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self.sld_tol.valueChanged.connect(self._recompute)
        self.btn_auto.clicked.connect(self._on_auto)
        self.btn_reset.clicked.connect(self._on_reset)
        self._recompute()

    # ── 掩码计算：初始态 ∩ 每个种子点的魔棒结果 ──
    def _compute_keep(self) -> np.ndarray:
        base = np.ones(self._img.shape[:2], bool) if self._keep0 is None else self._keep0.copy()
        if not self._seeds:
            return base
        tol = self.sld_tol.value()
        for sx, sy in self._seeds:
            base &= vision.magic_matte(self._img, (sx, sy), tol)
        return base

    def _recompute(self):
        self._keep = self._compute_keep()
        self.lbl_tol.setText(str(self.sld_tol.value()))
        total = self._keep.size
        kept = int(self._keep.sum())
        self.lbl_info.setText(f"保留 {kept} / {total} 像素（抠掉 {total - kept}）\n"
                              "棋盘格 = 透明，识别时这些位置完全不参与比对。")
        self.view.set_content(self._img, self._keep)

    def _on_seed(self, x: int, y: int):
        if (x, y) == (-1, -1):
            if self._seeds:
                self._seeds.pop()
        else:
            self._seeds.append((x, y))
        self._recompute()

    def _on_auto(self):
        self._keep0 = vision.auto_matte(self._img, tolerance=self.sld_tol.value())
        self._seeds.clear()
        self._recompute()

    def _on_reset(self):
        self._keep0 = None
        self._seeds.clear()
        self._recompute()

    def _on_accept(self):
        if bool(self._keep.all()):
            self._keep0 = None
            self._seeds.clear()
            self._recompute()
            self.lbl_info.setText("还没抠出任何透明区——点一下背景或按「一键去底」再保存。")
            return
        self.matte = vision.to_bgra(self._img, self._keep)
        self.accept()


class TemplatePanel(QWidget):
    """采模板工作区：采集表单 + 模板库。嵌在三栏工作台（ui/workbench.py）
    右栏常驻可见，预览/实例/截图定时器由外壳提供。三栏同屏后不再有
    "采模板/编流程"模式概念，本面板始终就绪，随时可框选可保存。"""

    template_saved = Signal(str, str)  # (category, name) 保存成功 → 服务「去采集」回填
    templates_changed = Signal()       # 模板库变了 → 服务流程面板刷新模板下拉
    status_message = Signal(str)       # 请求外壳共用状态栏显示文本

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame = None             # 外壳推送的最新画面（采集/测试数据源）
        self._pending_verify = None    # 保存后攒的「新帧复检」(name, category)，on_frame 兑现
        self._view = None              # 共用预览（attach_view 注入）
        self._pending_roi = None       # 待保存的区域（与模板本体一起入库）
        self._pending_click_target = None  # 待保存的固定点击坐标
        self._pending_click_offset = None  # 待保存的相对点击偏移 [dx,dy]（跟随图模式）
        self._props_dialog = None      # 打开着的模板属性编辑窗（右键「修改属性」，非模态）
        self._replace_name = None      # 「重新采集·覆盖同名」锁名模式：非空=保存将覆盖这张图
        # 识别区域模式：fullscreen=全屏识图 / manual=指定区域识图 / bind=区域联动识图。
        # 默认全屏；切换模式即清场，不留上一模式攒的区域（见 _on_roi_mode）。
        self._roi_mode = "fullscreen"

        # 采集区：模板库、名称、阈值 + 保存按钮（网格行内并排，不再一行一个字段）
        grp_capture = QGroupBox("模板采集")
        grid_cap = QGridLayout(grp_capture)
        # 模板库一行：标签 + 下拉 + 新增库按钮（下拉宽度吃掉多余空间）
        row_cat = QHBoxLayout()
        row_cat.addWidget(QLabel("模板库："))
        row_cat.addWidget(make_help_badge(
            "模板库 = templates/ 下的一个目录，只是装图的容器（平级，不分贵贱）。\n"
            "流程引用的模板都按「库/模板名」定位；被流程用着的库删不掉（非空拒绝）。\n"
            "改名只改显示名、移动只搬目录，引用会自动跟着改写，不断链。"))
        self.cmb_category = QComboBox()
        self._refresh_category_combo()
        row_cat.addWidget(self.cmb_category, 1)
        self.btn_new_cat = QPushButton("＋新增库")
        self.btn_new_cat.setToolTip("新建一个模板库（templates/ 下新目录，建议用中文命名，"
                                    "如「周常活动」），建好即可下拉选中往里采图。任务目录无需新建。")
        self.btn_new_cat.clicked.connect(self.on_new_category)
        row_cat.addWidget(self.btn_new_cat)
        self.btn_ren_cat = QPushButton("改名")
        self.btn_ren_cat.setToolTip("重命名下拉当前选中的库（只改界面显示名，\n"
                                    "存储目录和流程引用不动，零断链）。\n"
                                    "任务目录（如「自定义流程」）的名字由任务注册表管理，不可改。")
        self.btn_ren_cat.clicked.connect(self.on_rename_category)
        row_cat.addWidget(self.btn_ren_cat)
        self.btn_del_cat = QPushButton("－删除库")
        self.btn_del_cat.setToolTip("删除下拉当前选中的空库（任务目录 / 自定义库都行，"
                                    "非空会拒绝并提示剩余模板数）。\n"
                                    "误删任务目录后，用「＋新增库」填同名即可恢复。")
        self.btn_del_cat.clicked.connect(self.on_delete_category)
        row_cat.addWidget(self.btn_del_cat)
        grid_cap.addLayout(row_cat, 0, 0, 1, 2)
        # 名/阈值并排一行：前缀下拉 + 阈值 + 主按钮吃剩余宽度（原来各占一行）
        row_name = QHBoxLayout()
        row_name.addWidget(QLabel("模板名："))
        row_name.addWidget(make_help_badge(
            "采集一条的完整动作：先在左侧画面点两下框选模板 → 这里填中文名 →\n"
            "点「保存并测试」入库并当场验证匹配。\n"
            "「前缀下拉」给名字挂类型头（弹窗_/按钮_/锚点_…），模板库列表一眼分拣；\n"
            "「匹配阈值」是这张图的识别门槛；识别区域/固定点击坐标都是可选附加项。"))
        self.edt_name = QLineEdit()
        self.edt_name.setPlaceholderText("中文，如 在线奖励")
        row_name.addWidget(self.edt_name, 1)
        self.cmb_prefix = QComboBox()
        self.cmb_prefix.setToolTip(
            "给模板名挂个类型前缀（弹窗/按钮/图标…），再自己补名字，"
            "存成「锚点_登录界面」这种。同一张图重采时前缀也会自动接上。\n"
            "点右侧「管理」增删自定义前缀（写进 config，重启保留）。")
        self._reload_prefix_combo(self.cmb_prefix)
        self.cmb_prefix.setFixedWidth(92)   # 容纳最长前缀「关闭窗口」
        self.cmb_prefix.currentTextChanged.connect(self._apply_name_prefix)
        row_name.addWidget(self.cmb_prefix)
        self.btn_manage_prefix = QPushButton("管理")
        # 别设固定宽：44px 装不下两个中文字+按钮内边距，字会被挤成乱码样
        self.btn_manage_prefix.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.btn_manage_prefix.setToolTip("增删自定义前缀（写进 config，重启保留）")
        self.btn_manage_prefix.clicked.connect(self.on_manage_prefix)
        row_name.addWidget(self.btn_manage_prefix)
        self.btn_recut_asnew = QPushButton("另存为新图")
        self.btn_recut_asnew.setToolTip("解锁模板名、另存成新名字：这次采的存成一张新图，原图不动。\n"
                                        "（「重新采集」跳过来时名字是锁着的，存盘=覆盖原图）")
        self.btn_recut_asnew.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.btn_recut_asnew.clicked.connect(self._clear_replace_lock)
        self.btn_recut_asnew.hide()
        row_name.addWidget(self.btn_recut_asnew)
        self.btn_recut_cancel = QPushButton("取消")
        self.btn_recut_cancel.setToolTip("不想重采了：解除锁名、清空名字，回到普通采集"
                                         "（原图一根毛不动）。")
        self.btn_recut_cancel.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.btn_recut_cancel.clicked.connect(self._cancel_replace)
        self.btn_recut_cancel.hide()
        row_name.addWidget(self.btn_recut_cancel)
        grid_cap.addLayout(row_name, 1, 0, 1, 2)
        # 覆盖模式黄条：只在「重新采集·锁名」时现身（存盘前还会再弹一次确认——红线）
        self.lbl_replace = QLabel("")
        self.lbl_replace.setWordWrap(True)
        self.lbl_replace.setStyleSheet(
            "QLabel{background:#5a4413;color:#ffd54f;border:1px solid #8a6d1a;"
            "border-radius:4px;padding:4px 8px;}")
        self.lbl_replace.hide()
        grid_cap.addWidget(self.lbl_replace, 2, 0, 1, 2)
        row_thr = QHBoxLayout()
        row_thr.addWidget(QLabel("匹配阈值："))
        self.spn_threshold = QDoubleSpinBox()
        self.spn_threshold.setRange(0.60, 0.99)
        self.spn_threshold.setSingleStep(0.01)
        self.spn_threshold.setValue(0.85)
        self.spn_threshold.setToolTip("新采模板的匹配门槛，可用 0.60~0.99（越低越易找到但越易误认）。\n"
                                      "只作用于这次保存的图；改已有模板请右键模板库条目→「修改属性」。")
        row_thr.addWidget(self.spn_threshold)
        self.btn_save_test = QPushButton("保存并测试")
        self.btn_save_test.setObjectName("btnSuccess")   # 样式走全局主题
        # 自然宽 + 行尾吸收：不拉满整行（原来 stretch=1 把按钮抻成横幅，
        # 名/阈值/按钮三个分量失衡）
        self.btn_save_test.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        row_thr.addWidget(self.btn_save_test)
        row_thr.addStretch(1)
        grid_cap.addLayout(row_thr, 3, 0, 1, 2)
        # 抠图开关：按下后「保存并测试」先弹抠图预览（弹窗见 MatteDialog）。
        # 状态全在按下态里（checkable 按钮自带，项目"状态画进控件"约定），不再挂文字标签
        self.btn_matte = QPushButton("抠图（去背景）")
        self.btn_matte.setCheckable(True)
        self.btn_matte.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.btn_matte.setToolTip(
            "点一下背景色就把它抠成透明，识别时背景完全不参与比对——\n"
            "同一按钮背后的画面再怎么变都不掉分（配合小图标/文字类模板效果最明显）。\n"
            "开关：开启后「保存并测试」会弹抠图预览确认，保存的是带透明通道的模板；\n"
            "不开 = 整块矩形照旧入库。先框选好模板区域再点。")
        grid_cap.addWidget(self.btn_matte, 4, 0, 1, 1)
        # 自动贴边：框选完成即把蓝框吸附到框内主图标轮廓（core.vision.snap_bbox
        # 两簇分前景/背景）。只对「模板框选/联动圈」生效，识别区域黄框不吸
        # （区域本来就该画得比模板大）。
        self.chk_snap = QCheckBox("框选后自动贴边")
        self.chk_snap.setToolTip(
            "点两下框完，蓝框自动吸附到框内图标的真实边缘——手抖多框的空白、\n"
            "少框掉的边角都替你修好（同一套算法也喂给 tools/frame_cut.py --snap 命令行）。\n"
            "分不出前景时保持原框不动，并在状态栏提示。\n"
            "⚠ 图标浮在复杂画面（有角色/特效背景）上时吸附可能框歪，关掉即可。")
        self.chk_snap.setChecked(bool(get_config().get("template_auto_snap", False)))
        self.chk_snap.toggled.connect(
            lambda on: get_config().set("template_auto_snap", bool(on)))
        grid_cap.addWidget(self.chk_snap, 5, 0, 1, 2)
        grid_cap.setColumnStretch(0, 1)
        if grid_cap.columnCount() > 1:
            grid_cap.setColumnStretch(1, 3)

        # 识别区域区：三选一模式（全屏 / 指定区域 / 圈哪认哪）
        grp_roi = QGroupBox("识别区域")
        lay_roi = QVBoxLayout(grp_roi)
        # 上墙的「操作顺序」小字：新手最常卡在先点哪个/点了有没有效，
        # 成功反馈靠下面「当前」行出 ✓（见 on_roi_drawn/on_clear_roi）
        hint_roi = QLabel("顺序：① 选模式（默认「全屏识图」＝不设范围，什么都不用点）"
                          "→ ② 选「指定区域/联动」自动进框选态，回画面点两下画框"
                          "→ ③ 下面「当前」行出 ✓ ＝ 框好了"
                          "→ ④ 填模板名点「保存并测试」，区域随图入库")
        hint_roi.setWordWrap(True)
        hint_roi.setStyleSheet("color:#888;")
        lay_roi.addWidget(hint_roi)
        self.radio_roi_full = QRadioButton("全屏识图")
        self.radio_roi_full.setToolTip("默认。找图不设范围，全屏搜索（老行为）。")
        self.radio_roi_manual = QRadioButton("指定区域识图")
        self.radio_roi_manual.setToolTip("手动框一个区域，只在这个区域里找图。\n"
                                         "选中后自动进入框选态，在画面上点两下框选区域。")
        self.radio_roi_bind = QRadioButton("区域联动识图")
        self.radio_roi_bind.setToolTip("在画面上框一个圈：这个圈既是模板也是识别区域，\n"
                                       "选中即进入框选态，一次框选一步入库。")
        self.radio_roi_full.setChecked(True)
        self._roi_group = QButtonGroup(self)
        self._roi_group.addButton(self.radio_roi_full)
        self._roi_group.addButton(self.radio_roi_manual)
        self._roi_group.addButton(self.radio_roi_bind)
        self._roi_group.buttonClicked.connect(self._on_roi_mode_clicked)
        lay_roi.addWidget(self.radio_roi_full)
        lay_roi.addWidget(self.radio_roi_manual)
        lay_roi.addWidget(self.radio_roi_bind)
        row_roi_btn = QHBoxLayout()
        self.btn_add_region = QPushButton("添加区域")
        self.btn_add_region.setToolTip("手动指定找图区域（比模板本体更大/更小）。\n"
                                       "「区域联动识图」模式下框选模板即区域，一般不用手动设。")
        self.btn_add_region.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        row_roi_btn.addWidget(self.btn_add_region)
        self.btn_clear_roi = QPushButton("清除区域")
        self.btn_clear_roi.setToolTip("清空已框的找图区域，回到全屏识图。")
        self.btn_clear_roi.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        row_roi_btn.addWidget(self.btn_clear_roi)
        row_roi_btn.addStretch(1)   # 成对按钮不平均撑满：空白由行尾吸收
        lay_roi.addLayout(row_roi_btn)
        self.lbl_roi = QLabel("当前: 无")
        self.lbl_roi.setWordWrap(True)
        lay_roi.addWidget(self.lbl_roi)

        # 点击坐标区（说明文字并入按钮 tooltip，不再占一行）
        grp_click = QGroupBox("固定点击坐标（可选）")
        lay_click = QVBoxLayout(grp_click)
        hint_click = QLabel("顺序：不设＝识别到模板后点它的中心（多数情况够用）。要改点击位："
                            "①（推荐）勾「存为相对偏移（跟随图）」→ ② 先在画面框好模板"
                            "→ ③ 点「记录点击坐标」→ ④ 画面点一下目标位，「当前」行出 ✓"
                            "＝记好了 → ⑤ 保存随图入库（用这张图的流程全体继承）")
        hint_click.setWordWrap(True)
        hint_click.setStyleSheet("color:#888;")
        lay_click.addWidget(hint_click)
        row_click_btn = QHBoxLayout()
        self.btn_set_click = QPushButton("记录点击坐标")
        self.btn_set_click.setToolTip("点击后，在左侧画面上单击一点，"
                                      "识别到模板后就固定点这个坐标；不设则默认点模板中心。\n"
                                      "勾下方「存为相对偏移（跟随图）」改存跟图偏移——"
                                      "图标在哪都点得准，且全体使用者自动继承（推荐）。")
        self.btn_set_click.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        row_click_btn.addWidget(self.btn_set_click)
        self.btn_clear_click = QPushButton("清除坐标")
        self.btn_clear_click.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        row_click_btn.addWidget(self.btn_clear_click)
        row_click_btn.addStretch(1)
        lay_click.addLayout(row_click_btn)
        self.chk_click_follow = QCheckBox("存为相对偏移（跟随图）")
        self.chk_click_follow.setToolTip(
            "勾上后点出来的目标位存成「相对这张图的偏移」进图账本：弹窗在屏幕上\n"
            "挪位置也点得准，而且一次校正全体受益——所有没自己配偏移的使用者\n"
            "（监视行/点模板节点）自动继承，行/节点采了非 0 偏移才盖过它。\n"
            "不勾 = 老行为：存绝对坐标，弹窗位置固定时才用。需先框选好模板。")
        lay_click.addWidget(self.chk_click_follow)
        self.lbl_click = QLabel("当前: 无（默认点模板中心）")
        self.lbl_click.setWordWrap(True)
        lay_click.addWidget(self.lbl_click)

        # ── 左列：采集表单（模板采集、识别区域、点击坐标竖排；底部匹配测试吃剩余）──
        left_panel = QWidget()
        left_col = QVBoxLayout(left_panel)
        left_col.setSpacing(10)
        left_col.addWidget(grp_capture)
        left_col.addWidget(grp_roi)
        left_col.addWidget(grp_click)
        left_panel.setMinimumWidth(400)

        # 匹配测试区：固定高度的滚动区，多行报告滚动查看、不撑高布局
        grp_result = QGroupBox("匹配测试")
        lay_res = QVBoxLayout(grp_result)
        self.lbl_result = QLabel("尚未测试")
        self.lbl_result.setWordWrap(True)
        self.lbl_result.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        # 内容高度 = 文本行数撑起，固定高度由滚动区承担：label 垂直方向用
        # Preferred（Expanding 会去填满滚动区视口，把多行报告压成一行不许滚）
        self.lbl_result.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        scroll_result = QScrollArea()
        scroll_result.setWidgetResizable(True)
        scroll_result.setWidget(self.lbl_result)
        scroll_result.setFrameShape(QFrame.Shape.NoFrame)
        lay_res.addWidget(scroll_result)
        self._result_scroll = scroll_result  # 写结果后滚到顶
        left_col.addWidget(grp_result, 1)   # 匹配测试吃左列剩余高度

        # ── 右列：模板库（按任务分组，吃掉剩余高度）──
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(10, 0, 0, 0)
        right_layout.setSpacing(6)
        right_layout.addWidget(QLabel("模板库（按任务分组）："))
        self.edt_search = QLineEdit()
        self.edt_search.setPlaceholderText("🔍 搜索模板名...")
        # 搜索防抖：每敲一个字符就整树清空重建（几百模板 × 每项读图取图标），
        # 连打造成肉眼可见的闪烁发黏。停手 200ms 才刷一次，体感仍是"即输即滤"。
        # 定时器挂本面板下（parent=self），随面板析构回收。
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(200)
        self._search_debounce.timeout.connect(self._apply_search_filter)
        right_layout.addWidget(self.edt_search)
        self.tree_templates = QTreeWidget()
        self.tree_templates.setIconSize(QSize(80, 80))
        self.tree_templates.setHeaderHidden(True)
        self.tree_templates.setIndentation(16)
        self.tree_templates.setExpandsOnDoubleClick(True)
        # 显式多选（Ctrl/Shift 点选多个模板）：删除按钮支持批量删选中全部
        self.tree_templates.setSelectionMode(QAbstractItemView.ExtendedSelection)
        right_layout.addWidget(self.tree_templates, stretch=1)
        # 底部工具按钮一行并排（原两行：删除/测试全部 + 批量阈值/导出/导入）
        btn_row = QHBoxLayout()
        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.setToolTip("删除树里选中的模板。Ctrl/Shift 多选后点此批量删除；"
                                   "选中被任务使用的模板会弹二次确认，列出使用者")
        self.btn_test_all = QPushButton("测试全部")
        self.btn_batch_thresh = QPushButton("批量阈值")
        self.btn_export = QPushButton("导出")
        self.btn_import = QPushButton("导入")
        for b in (self.btn_delete, self.btn_test_all, self.btn_batch_thresh,
                  self.btn_export, self.btn_import):
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)  # 不平均撑满，行尾吸收空白
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        right_layout.addLayout(btn_row)
        right_panel.setMinimumWidth(320)

        # 模板库右键菜单：修改属性 / 重命名 / 删除（与底部按钮同一删除入口）
        self.tree_templates.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree_templates.customContextMenuRequested.connect(self.on_template_menu)

        # ── 组装左右两栏：表单在左、模板库在右（整栏宽度由外壳右列决定）──
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(left_panel, 2)
        lay.addWidget(right_panel, 3)
        lay.setSpacing(10)

        # ── 信号 ──
        self.btn_save_test.clicked.connect(self.on_save_and_test)
        self.btn_add_region.clicked.connect(self.on_add_region)
        self.btn_clear_roi.clicked.connect(self.on_clear_roi)
        self.btn_set_click.clicked.connect(self.on_set_click_target)
        self.btn_clear_click.clicked.connect(self.on_clear_click_target)
        self.btn_delete.clicked.connect(self.on_delete)
        self.btn_test_all.clicked.connect(self.on_test_all)
        self.btn_batch_thresh.clicked.connect(self.on_batch_threshold)
        self.btn_export.clicked.connect(self.on_export)
        self.btn_import.clicked.connect(self.on_import)
        self.tree_templates.currentItemChanged.connect(self.on_template_selected)
        self.edt_search.textChanged.connect(lambda _t: self._search_debounce.start())

        self.refresh_template_list()

    # ── 外壳接线 ──
    def attach_view(self, view):
        """工作台建好共用预览后调用。取点/框选的送达走发起时登记的一次性回调
        （见 on_add_region / on_set_click_target，谁发起谁收，两面板共用一个
        预览也不串扰）；外壳的 owner 信号路由只在没登记回调时兜底。
        放大镜已挪到外壳预览图上方，本面板不再处理悬停。"""
        self._view = view
        view.selection_done.connect(self._on_selection_done)

    def _on_selection_done(self, x, y, w, h):
        """模板框选完成（蓝框）：开了「自动贴边」就把框吸到框内主图标轮廓。
        吸不动（分不出前景）保持原框，只播状态，绝不动手改坏用户的框。"""
        if not self.chk_snap.isChecked() or self._frame is None or self._view is None:
            return
        bbox = vision.snap_bbox(self._frame, (x, y, x + w, y + h))
        if bbox is None:
            self._set_status("贴边：框内分不出前景（背景太花/目标太小），保持原框")
            return
        if tuple(bbox) != (x, y, w, h):
            self._view.selection = bbox
            self._view.update()
            self._set_status(f"✓ 已贴边 ({x},{y}) {w}×{h} → ({bbox[0]},{bbox[1]}) "
                             f"{bbox[2]}×{bbox[3]}")

    def on_frame(self, img):
        """外壳截图定时器推送新画面（采集/测试数据源）。
        兼作「保存后新帧验证」的发令枪：on_save_and_test 攒的复检在这里兑现
        ——裁它的那帧自测恒 1.000 没有意义，下一帧才是真考卷。"""
        self._frame = img
        if self._pending_verify is not None:
            name, cat = self._pending_verify
            self._pending_verify = None
            self.on_test_template(name, cat, note="新帧复检 ")

    def begin_capture(self):
        """流程「缺图？去采集」联动：三栏同屏无需切模式，这里把采集表单拉到
        眼前——预览回框选稳态并清掉半截操作，焦点给模板名输入框。"""
        if self._view is not None:
            self._view.draw_mode = "template"
            self._view.click_owner = None
            self._view.clear_selection()
        self.edt_name.setFocus()
        if self._replace_name:
            self._set_status(f"重新采集「{self._replace_name}」：框选新图 → 保存 = 覆盖原图")
        else:
            self._set_status("模板库已预填，在左侧画面点两下框选模板区域 → 保存并测试")

    def prefill_category(self, category: str):
        """流程面板「去采集」联动：预填模板库。"""
        idx = self.cmb_category.findData(category)
        if idx >= 0:
            self.cmb_category.setCurrentIndex(idx)

    # ── 「重新采集·同名覆盖」锁名模式 ──
    def prefill_replace(self, name: str):
        """流程槽带旧图名跳来 = 锁名覆盖模式：名字不给改，保存前另弹确认；
        空名 = 普通新建采集（顺手清掉上一次残留的锁，防串态）。"""
        if not name:
            self._clear_replace_lock()
            return
        self._replace_name = name
        self.edt_name.setText(name)
        self.edt_name.setReadOnly(True)
        self.cmb_prefix.setEnabled(False)
        self.btn_recut_asnew.show()
        self.btn_recut_cancel.show()
        self.lbl_replace.setText(
            f"重新采集模式：名字已锁定 —— 框选好新图点「保存并测试」= 覆盖「{name}」，"
            "图换芯不换名，用这张图的流程立刻改用新图。想存成新图点右侧「另存为新图」，"
            "不想重采了点「取消」。")
        self.lbl_replace.show()

    def _clear_replace_lock(self):
        self._replace_name = None
        self.edt_name.setReadOnly(False)
        self.cmb_prefix.setEnabled(True)
        self.btn_recut_asnew.hide()
        self.btn_recut_cancel.hide()
        self.lbl_replace.hide()

    def _cancel_replace(self):
        """取消重新采集：解锁 + 清名（前缀骨架照挂），回普通新建，原图不动。"""
        self._clear_replace_lock()
        prefix = self.cmb_prefix.currentText()
        self.edt_name.setText("" if prefix == _NO_PREFIX else f"{prefix}_")
        self._set_status("已取消重新采集：原图未动，回到普通采集")

    def _confirm_overwrite(self, name: str) -> bool:
        """红线：覆盖用户资产前先问。列出现役引用者（现场派生，不落账本）。"""
        cat = self._selected_category()
        # 判据看库里实际有没有同名项，不看锁状态——解锁后保留原名硬存也一样拦
        if not template_manager.has_template(cat, name):
            return True                      # 库里没这张图 = 普通新建，不拦
        users = template_deps.users_of(cat, name)
        who = "、".join(template_deps.display_user(u) for u in users[:6]) + \
              ("…" if len(users) > 6 else "")
        return confirm(self, "重新采集：覆盖原图",
                       f"将覆盖「{cat}/{name}」：图片本体换新、名字不变。\n"
                       f"{('用这张图的流程：' + who) if who else '没查到有流程在用。'}\n\n"
                       "确认覆盖保存？")

    def _set_status(self, text: str):
        self.status_message.emit(text)

    # ── 模板库下拉：注册任务 + 自定义库 + 磁盘孤儿目录 ──
    def _refresh_category_combo(self):
        """重建分类下拉（切换/刷新时保留当前选择）。孤儿目录排在最后。"""
        cur = self.cmb_category.currentText()
        names = template_deps.task_dir_display()
        cats = template_manager.list_categories()
        known = [c for c in cats if c in names]
        orphans = [c for c in cats if c not in names]
        self.cmb_category.blockSignals(True)
        self.cmb_category.clear()
        for c in known + orphans:
            # 只显示库名，不再拖个「（目录）」尾巴：自定义库目录名 = 库名，重复
            # 一遍没意义；任务库想查目录悬停 tooltip 可见
            self.cmb_category.addItem(names.get(c, c), c)
            self.cmb_category.setItemData(self.cmb_category.count() - 1,
                                          f"存储目录：{c}", Qt.ToolTipRole)
        idx = self.cmb_category.findData(cur)
        if idx >= 0:
            self.cmb_category.setCurrentIndex(idx)
        self.cmb_category.blockSignals(False)

    def _selected_category(self) -> str:
        return self.cmb_category.currentData() or self.cmb_category.currentText()

    def _apply_name_prefix(self, prefix: str):
        """前缀下拉一变：先摘掉名字里挂着的旧前缀，再挂新的（选"不加"只摘不挂）。
        摘认得的（config 里的，含用户自定义），用户手打的不认识的段不动。"""
        stem = self.edt_name.text()
        head, sep, tail = stem.partition("_")
        if sep and head in _config_prefixes():
            stem = tail
        self.edt_name.setText(stem if prefix == _NO_PREFIX else f"{prefix}_{stem}")

    def _reload_prefix_combo(self, cmb):
        """按 config 前缀刷新下拉（不含「不加」占位项）。"""
        cmb.blockSignals(True)
        cmb.clear()
        cmb.addItem(_NO_PREFIX)
        cmb.addItems(_config_prefixes())
        cmb.blockSignals(False)

    def on_manage_prefix(self):
        """自定义前缀管理：列出当前前缀，可逐个删除，可加新前缀（防重复）。
        写回 config（template_prefixes），刷新下拉。删的只是下拉预设，
        存量模板名带旧前缀的不受影响（名字照用，只是下拉里不再可选）。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("管理模板前缀")
        lay = QVBoxLayout(dlg)

        lst = QListWidget()
        # ⚠ 无父填项前强制样式生效（同取色点列表坑）：全局主题给
        # QListWidget::item 加了 padding，不 polish 则排位 17px/绘制 29px，
        # 行文字互压、热区偏半行 → 点错行「删除选中」会删掉隔壁前缀。
        lst.ensurePolished()
        lst.addItems(_config_prefixes())
        lay.addWidget(lst)
        h = QHBoxLayout()
        btn_add = QPushButton("添加前缀")
        btn_rm = QPushButton("删除选中")
        btn_close = QPushButton("完成")
        h.addWidget(btn_add)
        h.addWidget(btn_rm)
        h.addStretch(1)
        h.addWidget(btn_close)
        lay.addLayout(h)

        def do_add():
            text, ok = QInputDialog.getText(
                dlg, "添加前缀", "新前缀（如 数字 / 进度）：")
            if not ok or not text.strip():
                return
            t = text.strip()
            cur = _config_prefixes()
            if t in cur:
                return
            cur.append(t)
            get_config().set("template_prefixes", cur)
            lst.addItem(t)
            _reload_prefix_combo(self.cmb_prefix)

        def do_rm():
            it = lst.currentItem()
            if it is None:
                return
            cur = _config_prefixes()
            cur = [c for c in cur if c != it.text()]
            get_config().set("template_prefixes", cur)
            lst.takeItem(lst.row(it))
            _reload_prefix_combo(self.cmb_prefix)

        btn_add.clicked.connect(do_add)
        btn_rm.clicked.connect(do_rm)
        btn_close.clicked.connect(dlg.accept)
        dlg.exec()

    def on_new_category(self):
        """
        新增模板库：只问一次库名（显示名 = 库名，目录同名建在 templates/ 下）。
        正常情况只建自定义库；唯一例外是"重建被删空的任务目录"——
        任务目录被「删除库」清空后下拉里就选不到了，这里填同名可把它恢复回来
        （显示名以任务注册表为准，core.create_category 内部处理）。
        """
        dir_name, ok = QInputDialog.getText(
            self, "新增模板库",
            "库名（直接填中文，如「周常活动」；目录会按此名建在 templates/ 下）：")
        if not ok:
            return
        dir_name = dir_name.strip()
        if not dir_name:
            self._set_status("库名为空，已取消")
            return
        success, msg = template_manager.create_category(dir_name)
        if not success:
            QMessageBox.warning(self, "无法新建", msg)
            self._set_status(f"新建库失败：{msg}")
            return
        # 刷新下拉并立刻选中新库（用户建库就是为了往里采图）
        self._refresh_category_combo()
        idx = self.cmb_category.findData(dir_name)
        if idx >= 0:
            self.cmb_category.setCurrentIndex(idx)
        self.refresh_template_list()
        self.templates_changed.emit()   # 新库进下拉：刷新已打开的流程属性面板归属选择
        self._set_status(f"✓ {msg}，已选中，可直接框选采图")

    def on_rename_category(self):
        """
        重命名下拉当前选中的库：只改显示名（config.template_categories），
        存储目录名不动 → 账本、流程节点引用、清单队列全都按目录名存，
        零断链，改完即全界面生效（各处显示名都现取 task_dir_display）。
        任务目录拒改（硬规则在 core.set_category_display），下拉里照常选得到。
        """
        dir_name = self._selected_category()
        if not dir_name:
            self._set_status("没有选中模板库")
            return
        disp = template_deps.task_dir_display().get(dir_name, dir_name)
        new_disp, ok = QInputDialog.getText(
            self, "重命名模板库",
            f"给「{disp}」起个新名字（只改显示名，存储目录「{dir_name}」不变）：",
            text=disp)
        if not ok:
            return
        new_disp = new_disp.strip()
        if not new_disp or new_disp == disp:
            return  # 空名/同名：无事发生
        success, msg = template_manager.set_category_display(dir_name, new_disp)
        if not success:
            QMessageBox.warning(self, "无法改名", msg)
            self._set_status(f"库改名失败：{msg}")
            return
        self._refresh_category_combo()
        self.refresh_template_list()
        self.templates_changed.emit()   # 流程面板归属下拉 + 主界面侧栏同步新名
        self._set_status(f"✓ {msg}")

    def on_delete_category(self):
        """
        删除下拉当前选中的空库（任务目录/自定义库都行）。删目录是红线操作，
        这里管两道：
        1. 界面二次确认（列清楚要删的是哪个库 + 误删任务目录的恢复方法）
        2. 真删与否的硬规则全在 core.delete_category —— 只删空库，
           非空库拒绝并提示剩余模板数（账本+磁盘双查 + rmdir 兜底）
        """
        dir_name = self._selected_category()
        if not dir_name:
            self._set_status("没有选中模板库")
            return
        disp = template_deps.task_dir_display().get(dir_name, dir_name)
        # 「是不是任务目录」用 task_dir_names（显示名+历史标识一份集合）判断；
        # 目录改中文后 dir_name in TASKS 会静默判不出（TASKS 键还是英文标识）
        extra = ("\n注意：这是任务目录，删后下拉里会消失，"
                 "误删了用「＋新增库」填同名可恢复。"
                 ) if dir_name in template_deps.task_dir_names() else ""
        if not confirm(self, "删除模板库",
                       f"要删除的是「{disp}（{dir_name}）」。\n"
                       "只有【空库】能删（里面还有模板会拒绝并提示数量）。"
                       f"{extra}\n确定删除吗？"):
            return
        success, msg = template_manager.delete_category(dir_name)
        if not success:
            QMessageBox.warning(self, "无法删除", msg)
            self._set_status(f"删除库失败：{msg}")
            return
        self._refresh_category_combo()
        self.refresh_template_list()
        self.templates_changed.emit()   # 库没了：已打开的流程属性面板归属下拉同步刷新
        self._set_status(f"✓ {msg}")

    # ── 模板库列表（按任务目录分组的树 + 搜索 + 使用者标注）──
    def refresh_template_list(self, search_text=""):
        """整树重建（图标派生走 _icon_cache，不再逐项读图缩放）。
        搜索词只作可见性过滤（_apply_search_filter），不参与建树；
        search_text 参数兼容旧调用：传了就先写回搜索框。"""
        if search_text:
            self.edt_search.setText(search_text)
        self.tree_templates.blockSignals(True)
        self.tree_templates.clear()
        names = template_deps.task_dir_display()
        refs = template_deps.collect_refs()   # 「谁在用」全树只算一遍（曾逐项全库扫，树越大越灾难）
        groups: dict[str, list] = {}
        for t in template_manager.list_templates():
            groups.setdefault(t["category"], []).append(t)
        # 组顺序：注册任务在前，孤儿目录在后
        ordered = [c for c in template_manager.list_categories() if c in groups]
        # 记住用户当前的折叠状态，重建后原样恢复（树为空 = 默认全展开）
        expanded_state = {
            self.tree_templates.topLevelItem(i).data(0, Qt.UserRole + 1)
            for i in range(self.tree_templates.topLevelItemCount())
            if self.tree_templates.topLevelItem(i).type() == _TYPE_GROUP
            and self.tree_templates.topLevelItem(i).isExpanded()
        } if self.tree_templates.topLevelItemCount() else None
        for cat in ordered:
            # 树节只显示库名+张数，不带目录尾巴（存储目录悬停可见）
            header = QTreeWidgetItem([f"{names.get(cat, cat)}  {len(groups[cat])} 张"],
                                      _TYPE_GROUP)
            header.setToolTip(0, f"存储目录：{cat}/")
            header.setData(0, Qt.UserRole + 1, cat)  # 记住目录，供折叠状态恢复
            header.setFlags(header.flags() & ~Qt.ItemIsSelectable)
            self.tree_templates.addTopLevelItem(header)
            for t in sorted(groups[cat], key=lambda x: x["name"]):
                users = sorted(refs.get((cat, t["name"]), set()))
                user_txt = "、".join(template_deps.display_user(u) for u in users) if users else "未绑定"
                text = f"{t['name']}   [{t['width']}x{t['height']}]  阈{t['threshold']}"
                if t.get("roi"):
                    roi = t["roi"]
                    text += f"  区域[{roi[0]},{roi[1]},{roi[2]},{roi[3]}]"
                if t.get("click_target"):
                    ct = t["click_target"]
                    text += f"  →点击({ct[0]},{ct[1]})"
                if t.get("rgb_threshold") is not None:
                    text += f"  颜色校验{t['rgb_threshold']:.2f}"
                item = QTreeWidgetItem([text, f"用：{user_txt}"], _TYPE_TPL)
                icon, matte = _tree_icon(t)
                if icon is not None:
                    item.setIcon(0, icon)
                    if matte:
                        item.setText(0, text + "  抠图")
                item.setData(0, Qt.UserRole, (t["name"], cat))
                header.addChild(item)
            if expanded_state is None or cat in expanded_state:
                header.setExpanded(True)
        # 图标缓存剪枝：账本里没这个文件了（删除/改名/移库）就清出，防只增不减
        alive = {t["file"] for lst in groups.values() for t in lst}
        for stale in [k for k in _icon_cache if k[0] not in alive]:
            del _icon_cache[stale]
        self.tree_templates.blockSignals(False)
        self._apply_search_filter()

    def _apply_search_filter(self):
        """搜索框 = 纯可见性过滤（隐藏不命中的条目，不重建整树）。
        组标题下的条目全被隐藏时标题跟着藏；清空搜索恢复全展。"""
        q = self.edt_search.text().strip().lower()
        tree = self.tree_templates
        tree.blockSignals(True)
        for i in range(tree.topLevelItemCount()):
            header = tree.topLevelItem(i)
            hit_cat = q in str(header.data(0, Qt.UserRole + 1) or "").lower()
            any_vis = False
            for j in range(header.childCount()):
                it = header.child(j)
                name = str((it.data(0, Qt.UserRole) or ("", ""))[0]).lower()
                vis = not q or q in name or hit_cat
                it.setHidden(not vis)
                any_vis |= vis
            header.setHidden(bool(q) and not any_vis)
            if not q and any_vis:
                header.setExpanded(True)
        tree.blockSignals(False)

    # ── 一键流：保存并测试 ──
    def on_save_and_test(self):
        if self._frame is None:
            self._set_status("还没有画面，先选实例")
            return
        if self._view is None or self._view.selection is None:
            self._set_status("请先在画面上点两下框选模板区域（右键取消第一角）")
            return
        name = self.edt_name.text().strip()
        if not name or name.endswith("_"):
            self._set_status("请填写模板名（选了前缀的话，前缀后面还要补名字）")
            return

        # 识别区域模式定 roi（在保存前算好，拦在写盘前不留半成品）：
        #  - 全屏识图：不写 roi（全屏搜索）
        #  - 区域联动识图：框选模板的框就是识别区域
        #  - 指定区域识图：手动框的区域（还没框则拦截，不保存）
        if self._roi_mode == "manual" and self._pending_roi is None:
            self._set_status("「指定区域识图」模式：请先框选一个区域（点「添加区域」或切换模式已自动进入框选态）")
            return
        x, y, w, h = self._view.selection
        if self._roi_mode == "bind":
            self._pending_roi = [x, y, x + w, y + h]   # 框选即区域
        save_roi = self._pending_roi

        # 红线：同名覆盖用户资产，落盘前先问一次（列明引用者）
        if not self._confirm_overwrite(name):
            self._set_status("已取消：原图未动（想另存新图点「另存为新图」解锁改名）")
            return
        was_replace = (self._replace_name == name)

        # 1. 保存（框选 → 模板本体 + 三属性随图入账）
        tpl = self._frame[y:y + h, x:x + w].copy()
        # 抠图开关开启：先弹预览确认，抠成 BGRA 再入库（取消 = 不保存，半成品不落盘）
        if self.btn_matte.isChecked():
            dlg = MatteDialog(tpl, title=f"抠图 · {name}（点背景抠成透明）", parent=self)
            if dlg.exec() != QDialog.Accepted or dlg.matte is None:
                self._set_status("已取消抠图，模板未保存（关掉「抠图」开关可按原矩形入库）")
                return
            tpl = dlg.matte
        # 重新采集 = 图换芯不换配置：没重采的 区域/固定点击/跟随偏移 从旧条目继承
        # （普通新建路 old=None，三属性照本来的 pending 走，行为一字不变）
        old = (template_manager.find_template(self._selected_category(), name)
               if was_replace else None) or {}
        entry = template_manager.add_template(
            name, self._selected_category(), tpl, self.spn_threshold.value(),
            roi=save_roi if save_roi is not None else old.get("roi"),
            click_target=(self._pending_click_target
                          if self._pending_click_target is not None
                          else old.get("click_target")),
            click_offset=(self._pending_click_offset
                          if self._pending_click_offset is not None
                          else old.get("click_offset")),
            rgb_threshold=old.get("rgb_threshold"))
        if entry is None:
            self._set_status("保存失败：分类不合法")
            return

        # 2. 一键流清场
        self._pending_roi = None
        self.lbl_roi.setText("当前: 无" if save_roi is None else f"当前: {save_roi}")
        if self._view is not None:
            self._view.roi_rect = None
            self._view.update()
        self._pending_click_target = None
        self._pending_click_offset = None
        self.lbl_click.setText("当前: 无（默认点模板中心）")
        if self._view is not None:
            self._view.click_target_pos = None

        # 2. 刷新分类下拉（可能新建了目录）+ 列表（重算使用者标注）
        self._refresh_category_combo()
        self.refresh_template_list()

        # 3. 自动测试：不再当场测（拿裁它的这帧自测=自己找自己，恒 1.000 假分），
        #    挂起等截图定时器推来下一帧新画面再测——鼠标残留/动画帧当场现形。
        #    画面定格或实例停拍时不会来新帧，状态栏会说明在等。
        self._pending_verify = (entry["name"], entry["category"])
        self._show_result("✓ 已保存，等待新帧复检匹配分…（约 1 秒；画面定格中则先解定格）")

        # 4. 自动清空框选 + 模板名整栏清空（上次采的名字不留）。前缀下拉保留
        #    原选，这里清完直接把 "前缀_" 骨架挂回去（选"不加"则留空）——
        #    否则下拉看着选了前缀、实际存出来的名字没前缀，每次都得重选一遍。
        self._view.clear_selection()
        self.edt_name.clear()
        if was_replace:
            self._clear_replace_lock()   # 覆盖已兑现，下次采集回普通新建
        prefix = self.cmb_prefix.currentText()
        if prefix != _NO_PREFIX:
            self.edt_name.setText(f"{prefix}_")
        self.edt_name.setFocus()

        # 两信号都发：templates_changed 兜底置脏（覆盖"同名覆盖、无待回填"等情形），
        # template_saved 走回填通道（workbench 优先回填、回填不成也只是再置脏一次）
        self.templates_changed.emit()
        self.template_saved.emit(entry["category"], entry["name"])
        self._set_status(f"✓ {'已覆盖更新' if was_replace else '已保存'} "
                         f"{entry['category']}/{entry['name']}，继续框选下一个")

    # ── 测试结果显示（写进固定高滚动区 + 滚到顶）──
    def _show_result(self, txt: str):
        self.lbl_result.setText(txt)
        # adjustSize：让 label 立刻按文本行数撑高。QScrollArea 的 widgetResizable
        # 依赖一次布局重算才把子 widget 扩到 sizeHint，离屏/未布局下不自动发生，
        # 主动撑一次让滚动条当场可靠出现（真窗口也一样稳）。
        self.lbl_result.adjustSize()
        sc = getattr(self, "_result_scroll", None)
        if sc is not None:
            sc.verticalScrollBar().setValue(0)

    # ── 当前画面公开读口（外壳 workbench 取现帧测行用，不再读私有 _frame）──
    def current_frame(self):
        return self._frame

    def test_on_frame(self, frame, category: str, name: str, note: str = "", entry=None):
        """对给定画面测这张图：查库 → 读图 → 匹配（采集/测行的公共核心链）。
        返回 (hit|None, result)：
          hit   —— match_template 结果 dict（附 w/h/roi/click_offset/click_target，
                   供上层拼文案；guard 失败为 None）
          result—— 采集页滚动区文案（note 前缀照旧：当场测/新帧复检）
        entry 可由上层传入（选中一次只查一遍）；None 则这里现场 find_template。
        本方法不碰 self._view、不调 _show_result——预览覆盖层是 on_test_template 的事。"""
        if frame is None:
            return None, "没有画面"
        if entry is None:
            entry = template_manager.find_template(category, name)
        if entry is None:
            return None, "模板不存在"
        tpl = template_manager.load_template_image(entry)
        if tpl is None:
            return None, "模板图片读不出来"

        # 测试匹配（debug=True：未命中也回传实际最高分，供调参）
        hit = vision.match_template(frame, tpl, entry["threshold"],
                                    roi=entry.get("roi"),
                                    rgb_threshold=entry.get("rgb_threshold"),
                                    debug=True)
        scope = "限定区域" if entry.get("roi") else "全屏"
        # 无论命中与否都给 hit 补上上层要用的账本信息（roi 决定预览黄框、
        # w/h 决定命中绿框、click_* 决定点击落点），命中/未命中的文案各自拼。
        if hit is not None:
            ct = entry.get("click_target")
            hit = dict(hit)
            hit.update(w=tpl.shape[1], h=tpl.shape[0], roi=entry.get("roi"),
                       click_offset=entry.get("click_offset"), click_target=ct)
        if hit is None or not hit.get("matched"):
            actual = hit["score"] if hit else 0.0
            ms = hit.get("ms", 0.0) if hit else 0.0
            return (hit,
                    f"{note}✗ 未找到\n{scope}，阈值 {entry['threshold']}\n"
                    f"实际最高 {actual:.4f}（{ms:.1f}ms），建议：实际分接近阈值则微调阈值，差距大则换模板")
        ct = entry.get("click_target")
        click_info = f"\n固定点击: ({ct[0]},{ct[1]})" if ct else ""
        return (hit,
                f"{note}✓ 匹配度 {hit['score']:.4f}（{hit.get('ms', 0.0):.1f}ms）\n"
                f"位置 ({hit['x']},{hit['y']})\n[{scope}]{click_info}")

    # ── 测试单个模板（内部调用）──
    def on_test_template(self, name: str, category: str, note: str = "", entry=None):
        """note：报告前缀标注测试来源（如「新帧复检 」），区分当场测/隔帧考。
        entry 可选：on_template_selected 已查过账本就直接传进来，选中一次只查一遍。"""
        hit, result = self.test_on_frame(self._frame, category, name, note, entry=entry)
        self._show_result(result)
        if hit is None or self._view is None:
            return
        roi = hit.get("roi")
        self._view.roi_rect = (roi[0], roi[1], roi[2] - roi[0], roi[3] - roi[1]) if roi else None
        self._view.hit_rects = ([(hit["x"], hit["y"], hit["w"], hit["h"])]
                                if hit.get("matched") else [])
        self._view.update()

    # ── 模板库选中事件 ──
    def on_template_selected(self, current, previous):
        """选中模板 → 自动测试 + 显示 click_target（点击或键盘导航都触发）。
        仅【单选】时自动测试：Ctrl 多选时每次变化都测同一个会重复跑
        （且多选场景下自动测试对"查找"目标模糊），跳过以提高体验。"""
        if current is None:
            return
        # 多选（>1）时不自动测试，但仍显示最后点的那个模板的点击坐标
        if len(self.tree_templates.selectedItems()) > 1:
            return
        data = current.data(0, Qt.UserRole)  # 分组节没有 data → None，跳过
        if not data:
            return
        name, category = data
        # 选中一次只查一遍：现场查账本后连同 entry 交给测模板链路复用
        entry = template_manager.find_template(category, name)
        self.on_test_template(name, category, entry=entry)
        # 显示该模板的固定点击坐标（如果有）
        if entry:
            # 阈值输入框跟随选中模板：它只服务"采新图"，不回填的话用户会以为
            # 改它能改选中模板（实际改已有模板走右键「修改属性」）
            self.spn_threshold.setValue(entry.get("threshold", 0.85))
        if self._view is not None:
            if entry and entry.get("click_target"):
                ct = entry["click_target"]
                self.lbl_click.setText(f"当前: ({ct[0]},{ct[1]})")
                self._view.click_target_pos = (ct[0], ct[1])
            else:
                self.lbl_click.setText("当前: 无（默认点模板中心）")
                self._view.click_target_pos = None
            self._view.update()

    # ── 识别区域模式切换 ──
    def _on_roi_mode_clicked(self, btn):
        """切换到某模式：更新标志 + 必要时清掉上一模式攒的区域/黄框。
        「全屏」和「指定区域」都清场（区域联动也随切换清掉，不留残留打架）"""
        if btn is self.radio_roi_full:
            self._roi_mode = "fullscreen"
        elif btn is self.radio_roi_manual:
            self._roi_mode = "manual"
        else:
            self._roi_mode = "bind"
        # 清场：清待保存区域 + 预览黄框 + 标签；draw_mode 复位回模板稳态
        # （框选到一半就切模式会留下半截 roi 态，不复位的话下次框选模板
        # 会误判成区域框选，模板永远框不出来）
        self._pending_roi = None
        if self._view is not None:
            self._view.roi_rect = None
            if self._view.draw_mode in ("roi", "click_target"):
                self._view.draw_mode = "template"
            self._view.update()
        self.lbl_roi.setText("当前: 无")
        if self._roi_mode == "manual":
            # 指定区域：自动进入框选态，省去手动点「添加区域」那一步
            self.on_add_region()
        elif self._roi_mode == "bind":
            # 区域联动：同样直接进框选态——画一个圈就够（on_roi_drawn 里
            # 圈同时记为模板，保存一步完成，不再要求补画第二个框）
            self.on_add_region()
            self._set_status("联动识图：在画面上点两下画一个圈（圈=模板=识别区域）")
        else:
            self._set_status("识别区域：全屏识图（不设范围）")

    # ── 找图区域 ──
    def on_add_region(self):
        """进入区域框选模式（共用预览切态，两下取角后直接回调 on_roi_drawn）"""
        if self._view is None:
            return
        self._view.set_roi_pick_callback(self.on_roi_drawn)   # 一次性直连：不经外壳 owner 嗅探
        self._view.draw_mode = "roi"
        self._set_status("区域模式：在画面上点两下框选区域（右键取消）")

    def on_roi_drawn(self, x, y, w, h):
        """区域框选完成 → 记录待保存。
        「区域联动识图」下这一框就是全部：同步当成模板框（圈=模板=识别区域），
        不必再画第二个蓝框，直接填名保存一步入库。"""
        # 属性编辑窗开着时，画面框选回填到弹窗，不占用采集表单的待保存槽
        if self._props_dialog is not None and self._props_dialog.isVisible():
            self._props_dialog.apply_picked_roi(x, y, w, h)
            self._set_status("区域已回填到属性窗，回到窗口点『确定』保存")
            return
        self._pending_roi = [x, y, x + w, y + h]
        self.lbl_roi.setText(f"✓ 已框区域 [{x},{y},{x+w},{y+h}]（保存随图入库）")
        if self._roi_mode == "bind" and self._view is not None:
            self._view.selection = (x, y, w, h)
            self._view.update()
            # 联动圈也是模板：照样吃「自动贴边」（保存时 roi 按贴边后的框重算，
            # 见 on_save_and_test 的 bind 分支，模板和区域不会分家）
            self._on_selection_done(x, y, w, h)
            self._set_status("联动识图：圈已记为模板兼识别区域，填模板名 → 保存并测试（一步完成）")
            return
        self._set_status("区域已记录，点\"保存并测试\"时一并入库")

    def on_clear_roi(self):
        """清除待保存区域 + 画面显示"""
        self._pending_roi = None
        if self._view is not None:
            self._view.roi_rect = None
            self._view.update()
        self.lbl_roi.setText("✓ 已清除区域（当前: 无）")
        self._set_status("已清除找图区域")

    # ── 固定点击坐标 ──
    def on_set_click_target(self):
        """进入点击坐标记录模式：在画面上点一下。
        发起时直接在共用预览登记一次性回调（谁发起谁收，天然不串扰；
        click_owner 仍标 "capture" 供外壳兜底路与状态判断用）。"""
        if self._view is None:
            return
        self._view.set_point_pick_callback(self.on_click_target_set)  # 一次性直连
        self._view.draw_mode = "click_target"
        self._view.click_owner = "capture"
        self._set_status("点击坐标模式：在画面上点一下目标位置")

    def on_click_target_set(self, x, y):
        """点击坐标已记录（属性窗开着时先回填属性窗，见 on_roi_drawn 同款分流）"""
        if self._props_dialog is not None and self._props_dialog.isVisible():
            self._props_dialog.apply_picked_click(x, y)
            self._set_status("点击坐标已回填到属性窗，回到窗口点『确定』保存")
            return
        if self.chk_click_follow.isChecked():
            if self._view is None or self._view.selection is None:
                self._set_status("「跟随图」模式：先在画面上框选模板，再点目标位")
                return
            # 跟随图模式：目标位 − 框选中心 = 相对偏移，存进图账本全体使用者继承
            sx, sy, sw, sh = self._view.selection
            dx, dy = int(x - (sx + sw / 2)), int(y - (sy + sh / 2))
            self._pending_click_offset = [dx, dy]
            self._pending_click_target = None
            self.lbl_click.setText(f"✓ 已记录 跟随图偏移 ({dx},{dy})（保存随图入库）")
            self._set_status(f"相对偏移 ({dx},{dy}) 已记录，点\"保存并测试\"随图入库"
                             "（没自配偏移的行/节点自动继承）")
            return
        self._pending_click_target = [x, y]
        self._pending_click_offset = None
        self.lbl_click.setText(f"✓ 已记录 固定点击 ({x},{y})（保存随图入库）")
        self._set_status(f"点击坐标已记录 ({x},{y})，点\"保存并测试\"时一并入库")

    def on_clear_click_target(self):
        """清除待保存的点击坐标（含跟随图偏移）"""
        self._pending_click_target = None
        self._pending_click_offset = None
        if self._view is not None:
            self._view.click_target_pos = None
            self._view.update()
        self.lbl_click.setText("✓ 已清除（当前: 默认点模板中心）")
        self._set_status("已清除点击坐标")

    # ── 删除模板（支持 Ctrl 多选批量）──
    def on_delete(self):
        """底部「删除选中」按钮：删树里当前选中的模板（过滤分组节：无 data→None）。"""
        # 统一以 selectedItems 为准（去重保序）：上一条导航的 currentItem 可能残留
        # "非选中的最后点击项"，漏删/误删都靠这一步挡掉
        items = list(dict.fromkeys(
            it.data(0, Qt.UserRole) for it in self.tree_templates.selectedItems()
            if it.data(0, Qt.UserRole)))  # [(name, category), ...]
        if not items:
            self._set_status("请先选中要删除的模板（可 Ctrl 多选）")
            return
        self._delete_entries(items)

    def _delete_entries(self, items):
        """删一批模板（items=[(name,category),...]，调用方保证去重）。
        按钮「删除选中」与右键菜单「删除」共用此实体，只删法一致。"""
        # 任务↔模板绑定的价值所在：删前查使用者，被任务依赖的图弹确认。
        # 一批只扫一遍引用（collect_refs 现算较贵，users_of 收 refs 复用）
        refs = template_deps.collect_refs()
        used = [(cat, nm) for nm, cat in items
                if template_deps.users_of(cat, nm, refs=refs)]
        if used:
            names = "、".join(f"{c}/{n}" for c, n in used)
            if not confirm(self, "这些模板正被任务使用",
                           f"选中 {len(items)} 个，其中 {len(used)} 个正被使用：{names}\n"
                           f"删除后这些引用会悬空（流程/每日配方跑到对应步骤找不到图）。"
                           f"\n确定删除吗？"):
                return

        # 逐个删，汇总结果
        deleted, failed = [], []
        for nm, cat in items:
            # 防重复：同文件被多次选中只删一次（去重保序已保证不重）
            if template_manager.remove_template(nm, cat):
                deleted.append(f"{cat}/{nm}")
            else:
                failed.append(f"{cat}/{nm}")

        if deleted:
            self.templates_changed.emit()  # 下拉里留着已删模板名 = 陈旧
            self.refresh_template_list()
            if self._view is not None:
                self._view.hit_rects = []
                self._view.roi_rect = None
                self._view.update()
            self.lbl_result.setText("尚未测试")
        msg = (f"✓ 已删除 {len(deleted)} 个：{'、'.join(deleted)}"
               + (f"（失败 {len(failed)} 个：{'、'.join(failed)}）" if failed else ""))
        self._set_status(msg)

    # ── 模板管理：批量阈值 / 导出 / 导入 / 重命名 ──
    # 「＋新增库」入口在采集区模板库下拉旁（on_new_category）：只建自定义库，
    # 任务目录仍由注册表驱动不手填 —— 与旧版"任意手填分类"的区别是
    # core.create_category 做了名字校验 + 防撞任务标识 + 显示名登记 config。
    def on_batch_threshold(self):
        val, ok = QInputDialog.getDouble(self, "批量阈值", "新阈值（0.60–0.99）：",
                                         0.85, 0.60, 0.99, 2)
        if not ok:
            return
        # 范围 = 当前选中：树里选中某库（或其下任意模板）→ 只改该库；
        # 没选中 → 全库。旧版注释写着"有选中分类则只改该分类"，代码却恒传
        # None（无条件重写全库阈值、悄悄落盘 index.json）——注释说谎 +
        # 踩「模板资产改动须先确认」红线，两头一起补：先算范围，再弹确认。
        cat = None
        item = self.tree_templates.currentItem()
        if item is not None:
            if item.type() == _TYPE_GROUP:
                cat = item.data(0, Qt.UserRole + 1)
            elif item.parent() is not None:
                cat = item.parent().data(0, Qt.UserRole + 1)
        scope = f"库「{cat}」下的全部模板" if cat else "全部库的全部模板"
        if not confirm(self, "批量阈值确认",
                       f"确定把{scope}的匹配阈值统一改为 {val:.2f} 吗？\n"
                       f"（改完立即写入模板账本，逐模板的旧阈值不保留）"):
            self._set_status("批量阈值已取消")
            return
        n = template_manager.batch_set_threshold(cat, val)
        self._set_status(f"已改 {n} 个模板阈值 → {val}"
                         + ("" if cat else "（全库）"))
        self.refresh_template_list()

    def on_export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出模板账本",
                                              "templates_index_export.json", "JSON (*.json)")
        if not path:
            return
        if template_manager.export_index(path):
            self._set_status(f"已导出账本到 {path}")
        else:
            self._set_status("导出失败")

    def on_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入模板账本",
                                              "", "JSON (*.json)")
        if not path:
            return
        n = template_manager.import_index(path)
        if n:
            self.templates_changed.emit()  # 导入可能新增/覆盖名字，流程面板下拉变陈旧
        self._refresh_category_combo()
        self.refresh_template_list()
        self._set_status(f"已导入 {n} 个模板" if n else "导入失败或无有效条目")

    def on_template_menu(self, pos):
        """模板库右键菜单：修改属性 / 重新抠图 / 重新采集 / 重命名 / 移动到库 / 删除。"""
        item = self.tree_templates.itemAt(pos)
        if item is None or not item.data(0, Qt.UserRole):
            return  # 分组节/空白：不出菜单（删库走按钮）
        # 资源管理器惯例：右键【没选中】的条目 → 清掉多选、只锁定这一条；
        # 右键【已选中】的条目 → 保留整组多选（批量删除正好删这一组）。
        if not item.isSelected():
            self.tree_templates.clearSelection()
            item.setSelected(True)
        sel = self.tree_templates.selectedItems()
        menu = QMenu(self)
        # 属性/重命名/移动是单模板语义，多选说不清改哪个 → 只在锁定单条时出现
        if len(sel) == 1:
            name, category = item.data(0, Qt.UserRole)
            menu.addAction("修改属性…", lambda: self._edit_props(category, name))
            menu.addAction("重新抠图…", lambda: self._recrop_template(category, name))
            # 重新找图入口不用绕流程了：同一套锁名覆盖（预填库→锁名→拉表单到眼前），
            # 框选新图保存 = 覆盖原图（存前还有 _confirm_overwrite 红线确认）
            menu.addAction("重新采集·覆盖…",
                           lambda: (self.prefill_category(category),
                                    self.prefill_replace(name),
                                    self.begin_capture()))
            menu.addAction("重命名…", lambda: self._rename_template(category, name))
            menu.addAction("移动到库…", lambda: self._move_template(category, name))
        # 删除复用按钮同一入口（on_delete 按当前选中集收集，口径完全一致）
        menu.addAction("删除" if len(sel) == 1 else f"删除 {len(sel)} 个",
                       self.on_delete)
        menu.exec(self.tree_templates.viewport().mapToGlobal(pos))

    # ── 右键「修改属性」：阈值/找图区域/点击坐标 ──
    def _edit_props(self, category: str, name: str):
        entry = next((t for t in template_manager.list_templates()
                      if t["name"] == name and t["category"] == category), None)
        if entry is None:
            self._set_status("模板不存在（可能已被删除，右键菜单是旧的）")
            return
        dlg = TemplatePropsDialog(entry, self)
        dlg.setModal(False)   # 非模态：才能边开着窗边在左侧画面上框选取点
        dlg.pick_fn = lambda kind, e=entry: self._props_pick(dlg, kind, e)
        dlg.btns.accepted.connect(lambda: self._props_saved(dlg, category, name))
        self._props_dialog = dlg
        # 只清"仍指向本窗"的引用：连开第二个属性窗后，先开那只的 finished
        # 若无条件置 None，会把第二只的指针一起抹掉 → 框选回填分流失效
        # （画完框没人接），排查起来还以为框选坏了。
        def _clear_props_ref(who=dlg):
            if self._props_dialog is who:
                self._props_dialog = None
        dlg.finished.connect(_clear_props_ref)
        # 确定按下但保存失败时不关窗：_props_saved 里处理（accepted 先于 done 跑）
        dlg.show()

    def _props_pick(self, dlg, kind: str, entry=None):
        """弹窗「画面框选/点选/算偏移」→ 借共用预览进对应取点模式。
        roi/click 完成仍回 on_roi_drawn / on_click_target_set（里面检测弹窗开着
        再转回填）；offset 自带一次性回调（要拿预匹配锚点算差值，不走那两条）。"""
        if self._view is None or self._frame is None:
            self._set_status("没有画面，无法在画面上取点（可手动填数值）")
            return
        if self._view.draw_mode not in ("template", "roi", "click_target"):
            self._set_status("当前预览正被其他操作占用（如流程取坐标），稍后再试")
            return
        if kind == "offset":
            self._props_pick_offset(dlg, entry)
            return
        if kind == "roi":
            self._view.set_roi_pick_callback(self.on_roi_drawn)
            self._view.draw_mode = "roi"
            self._set_status("在画面上点两下框选识别区域（右键取消），完成后回属性窗看结果")
        else:
            self._view.set_point_pick_callback(self.on_click_target_set)
            self._view.draw_mode = "click_target"
            self._view.click_owner = "capture"
            self._set_status("在画面上单击目标点（右键取消），完成后回属性窗看结果")

    def _props_pick_offset(self, dlg, entry):
        """属性窗「画面点选算偏移」：先按当前帧认出这张图定绿框锚点（与运行时
        tap_template 同口径：同阈值/roi，这里算的中心=挂机时的中心），再点目标位
        → dx/dy = 目标 − 框中心回填。存量图补偏移不用重新采集。"""
        img = template_manager.load_template_image(entry)
        if img is None:
            self._set_status("模板图片读不出来，改手动填 dx/dy")
            return
        hit = vision.match_template(self._frame, img, entry.get("threshold", 0.85),
                                    roi=entry.get("roi"),
                                    rgb_threshold=entry.get("rgb_threshold"))
        if hit is None:
            self._set_status("当前画面里没认出这张图：切到图面出现的画面再点，"
                             "或在属性窗手动填 dx/dy")
            return
        cx = hit["x"] + img.shape[1] // 2
        cy = hit["y"] + img.shape[0] // 2

        def _done(x, y, c=(cx, cy)):
            if self._view is not None:
                self._view.hit_rects = []
                self._view.update()
            dlg.apply_picked_offset(int(x - c[0]), int(y - c[1]))
            self._set_status(f"偏移已取：目标({x},{y}) − 图中心({c[0]},{c[1]}) = "
                             f"({x - c[0]},{y - c[1]})，回属性窗点「确定」存进图")
        self._view.set_point_pick_callback(_done)
        self._view.click_target_pos = None
        self._view.hit_rects = [(hit["x"], hit["y"], img.shape[1], img.shape[0])]
        self._view.draw_mode = "click_target"
        self._view.click_owner = "capture"
        self._view.update()
        self._set_status("绿框=认出的图（框中心即偏移原点），请单击要点的目标位置（右键取消）")

    def _props_saved(self, dlg, category: str, name: str):
        """属性窗点「确定」：落盘成功才关窗，失败留窗可改。"""
        thr, roi, ct, co, rgb = dlg.values()
        if template_manager.set_template_props(name, category, thr, roi, ct, co, rgb):
            self.refresh_template_list()
            self._set_status(f"✓ 已更新属性 {category}/{name}：阈值 {thr:.2f}"
                             + (f"，区域{roi}" if roi else "，全屏找图")
                             + (f"，固定点击{ct}" if ct else
                                (f"，跟随图偏移{co}" if co is not None else "，点模板中心"))
                             + (f"，颜色校验 {rgb:.2f}" if rgb is not None else ""))
            dlg.accept()
        else:
            self._set_status("保存失败：模板已不存在（可能被别处删除）")
            dlg.reject()

    # ── 右键「重命名」 ──
    # ── 右键「重新抠图」：在已入库的 PNG 上抠，保存即覆盖（阈值/区域/点击全不动）──
    def _recrop_template(self, category: str, name: str):
        entry = template_manager.find_template(category, name)
        if entry is None:
            self._set_status("模板不存在（可能已被删除，右键菜单是旧的）")
            return
        img = template_manager.load_template_image(entry)
        if img is None:
            self._set_status("模板图片读不出来")
            return
        # 已抠过的再开：以现有 alpha 为初始保留态（只补刀不清零）；否则全保留起步
        keep0 = vision.matte_of(img)
        dlg = MatteDialog(cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                          if img.shape[2] == 4 else img,
                          keep0=keep0, title=f"重新抠图 · {category}/{name}", parent=self)
        if dlg.exec() != QDialog.Accepted or dlg.matte is None:
            return
        # 覆盖 PNG 走 update_template_image：只换图 + 同步宽高，阈值/roi/点击坐标
        # 原样保留；抠图不改尺寸，宽高不变时它也会弹掉图片缓存（详见该函数注释）
        if not template_manager.update_template_image(name, category, dlg.matte):
            self._set_status("保存失败：模板已不存在或图片写入失败")
            return
        self.refresh_template_list()
        self.templates_changed.emit()
        # 有画面就顺手重测一张，让分数当场说话
        if self._frame is not None:
            self.on_test_template(name, category)
        self._set_status(f"✓ 已重抠 {category}/{name}（阈值/区域/点击坐标保持不变）")

    def _rename_template(self, category: str, name: str):
        """改名是"断引用"级操作 —— config 里流程节点/每日配方全都按
        名字找图，所以先列使用者弹确认，rename 成功后紧跟
        template_deps.rewrite_template_name 把这些引用一并改写（读侧 collect_refs
        与写侧 rewrite 是同一份引用点知识，见该模块）。"""
        new_name, ok = QInputDialog.getText(self, "重命名模板", "新模板名：",
                                            text=name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == name:
            return  # 空名/同名：无事发生（原来会误报"重命名失败"）
        users = [template_deps.display_user(u)
                 for u in template_deps.users_of(category, name)]
        if users:
            if not confirm(self, "该模板正被任务使用",
                           f"「{category}/{name}」正被使用：{'、'.join(users)}\n"
                           f"改名后这些地方的引用会自动同步更新。\n确定改名吗？",
                           default_no=False):
                return
        if template_manager.rename_template(name, category, new_name):
            # 先改账本/PNG，成功才传播引用（改名失败时引用必须保持指向旧名 = 仍有效）
            n = template_deps.rewrite_template_name(category, name, new_name)
            self.templates_changed.emit()
            self.refresh_template_list()
            self._set_status(f"✓ 已重命名 {category}/{name} → {new_name}"
                             + (f"，{n} 处引用已同步更新" if n else "（无引用需要更新）"))
        else:
            self._set_status("重命名失败（新名已存在或模板不存在）")

    # ── 右键「移动到库」 ──
    def _move_template(self, category: str, name: str):
        """把模板整体挪进另一个库（目录）：账本+PNG 成对搬，config 里按
        (库,名) 引用的流程节点跟着改写 —— 和改名同一条传播路（读侧 collect_refs
        与写侧 rewrite_template_refs 一份知识，见 bot/template_deps）。"""
        names = template_deps.task_dir_display()
        targets = [c for c in template_manager.list_categories() if c != category]
        if not targets:
            self._set_status("没有其它库可移入（可先「＋新增库」）")
            return
        # 显示中文库名、数据存目录名（换存储值会断引用，绝不动）
        items = [f"{names.get(c, c)}（{c}）" for c in targets]
        disp, ok = QInputDialog.getItem(self, "移动到库",
                                        f"把「{name}」从「{names.get(category, category)}」移到：",
                                        items, 0, False)
        if not ok:
            return
        to_cat = targets[items.index(disp)]
        # 目标库同名 = move 会拒绝（防覆盖丢图），先把话说在弹窗里
        if any(t["name"] == name and t["category"] == to_cat
               for t in template_manager.list_templates()):
            self._set_status(f"移动失败：「{names.get(to_cat, to_cat)}」库里已有同名模板「{name}」"
                             "（请先给对方改名再移）")
            return
        users = [template_deps.display_user(u)
                 for u in template_deps.users_of(category, name)]
        if users:
            if not confirm(self, "该模板正被任务使用",
                           f"「{category}/{name}」正被使用：{'、'.join(users)}\n"
                           f"移库后这些地方的引用会自动同步更新。\n确定移动吗？",
                           default_no=False):
                return
        if template_manager.move_template(name, category, to_cat):
            # 先搬文件，成功才传播（失败时引用保持指向原库 = 仍有效）
            n = template_deps.rewrite_template_refs(category, name, new_cat=to_cat)
            self.templates_changed.emit()
            self.refresh_template_list()
            self._set_status(f"✓ 已移动 {category}/{name} → {to_cat}/"
                             + (f"，{n} 处引用已同步更新" if n else "（无按库引用需要更新）"))
        else:
            self._set_status("移动失败（目标库同名、模板不存在或文件占用）")

    # ── 测试全部 ──
    def on_test_all(self):
        """测试全部模板，输出匹配率报告"""
        if self._frame is None:
            self._set_status("没有画面")
            return
        results = []
        for t in template_manager.list_templates():
            tpl = template_manager.load_template_image(t)
            if tpl is None:
                continue
            hit = vision.match_template(self._frame, tpl, t["threshold"],
                                        roi=t.get("roi"),
                                        rgb_threshold=t.get("rgb_threshold"),
                                        debug=True)
            ms = hit.get("ms", 0.0) if hit else 0.0
            score = hit["score"] if hit else 0.0
            found = bool(hit and hit.get("matched"))
            results.append((t["category"], t["name"], t["threshold"], score, found, ms, hit))

        # 按匹配度排序（未命中的用实际最高分参与排序）
        results.sort(key=lambda r: r[3], reverse=True)

        # 叠加所有命中的绿色框；未命中只出分数不画框
        if self._view is not None:
            self._view.hit_rects = []
            for cat, name, _thr, _score, found, _ms, hit in results:
                if not found:
                    continue
                t = next((t for t in template_manager.list_templates()
                          if t["name"] == name and t["category"] == cat), None)
                tpl = template_manager.load_template_image(t) if t else None
                if tpl is not None:
                    self._view.hit_rects.append((hit["x"], hit["y"], tpl.shape[1], tpl.shape[0]))
            self._view.update()

        # 生成报告（全量）
        lines = [f"全部模板匹配报告（{len(results)} 个，按匹配度降序）：\n"]
        for cat, name, thresh, score, found, ms, _hit in results:
            status = "✓" if found else "✗"
            lines.append(f"{status} {cat}/{name}: {score:.3f} (阈值{thresh}) {ms:.1f}ms")
        self._show_result("\n".join(lines))
        self._set_status(f"测试完成：{len(results)}个模板，命中 {sum(1 for r in results if r[4])} 个")
