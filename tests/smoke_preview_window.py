"""冒烟：预览跟随模拟器窗口（放大镜悬浮化 + 固定 1:1 装不下走滚动条）。

- ImageView 默认 1:1（_scale == 1.0，删掉了工作台的 2/3 硬编码）
- 预览 1:1 = 喂原生帧（crop_raw，RenderWindow 物理像素，零插值）：
  ImageView 按帧记 _magn 内容倍率，工作台显示倍率恒 1.0（用户拍板：
  画面永不缩，左栏装不下由 QScrollArea 出滚动条）；真实显示因子 = magn
- 实例没跑/量不到 → 基准帧回落 1:1
- 缩放时鼠标换算 _to_img 照旧除回比例（回归，防止坐标偏移）
- 放大镜：跟手、默认 2x、滚轮 2/3/4、镜上直标坐标RGB、越界隐藏
- WorkbenchDialog 构造/拆解不炸（放大镜不再挂进布局）
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 从 tests/ 独立运行：项目根在上一级
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


import numpy as np
from PySide6.QtCore import QEvent
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

# ── 冒烟写盘陷阱：先换临时配置单例，绝不碰真 configs/config.json ──
import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

# ── 冒烟不触真实雷电：实例列表/实例对象全桩空 ──
import core.capture as capture
import core.ldplayer as ldplayer
ldplayer.list_instances = lambda: []
ldplayer.get_instance = lambda *a, **k: None

from ui.template_tool import ImageView
from ui.workbench import WorkbenchDialog

failures = []


def check(label, cond):
    if cond:
        print(f"  ✓ {label}")
    else:
        failures.append(label)
        print(f"  ✗ {label}")


# ── 1. ImageView 默认 1:1 ──
v = ImageView()
check("ImageView 默认 1:1", v._scale == 1.0 and v._disp_w == 540 and v._disp_h == 960)

# ── 2. 鼠标坐标换算回归：缩放 0.9 时控件坐标除回图片坐标 ──
v.set_display_scale(0.9)
x, y = v._to_img(270, 480)
check("缩放 0.9 坐标换算", (x, y) == (300, 533) and v._scale == 0.9)

# ── 3. 放大镜跟手：画面内显示、越界隐藏、尺寸固定 160、滚轮切倍率 ──
fake = np.zeros((960, 540, 3), np.uint8)
fake[100, 100] = (255, 255, 255)
v._img = fake
v.set_display_scale(1.0)
v._update_magnifier(100, 100)
check("放大镜显示且 160x160",
      not v.magnifier.isHidden() and v.magnifier.width() == 160
      and v.magnifier.height() == 160)
check("默认倍率 x2", v._mag_zoom == 2)
# 状态标签：坐标+RGB 已在镜上（直接查文本）
v._update_magnifier(100, 100)
check("镜上直标坐标+RGB", "(100,100)" in v._mag_lbl.text() and "#FFFFFF" in v._mag_lbl.text())
# 滚轮向上 → x4（档位 [2,3,4] 顶端），向下 → 回 x2
wup = type("Ev", (), {"angleDelta": lambda self: type("P", (), {"y": lambda self: 120})()})()
v._mag_center = (100, 100)
v.wheelEvent(wup)
v.wheelEvent(wup)
check("滚轮上 → x4", v._mag_zoom == 4)
wdown = type("Ev", (), {"angleDelta": lambda self: type("P", (), {"y": lambda self: -120})()})()
v.wheelEvent(wdown)
v.wheelEvent(wdown)
check("滚轮下 → 回 x2", v._mag_zoom == 2)
v._update_magnifier(-1, -1)
check("鼠标越界 → 放大镜隐藏", v.magnifier.isHidden())
v._update_magnifier(500, 950)
check("角落仍在画面内不崩", True)
v._update_magnifier(600, 1000)
check("越界又隐藏", v.magnifier.isHidden())
# 移出画面要靠 leaveEvent 收尾（没有 mouseMove 进来）
v._update_magnifier(200, 200)
check("画面内显示", not v.magnifier.isHidden())
v.leaveEvent(QEvent(QEvent.Leave))
check("leaveEvent → 放大镜隐藏", v.magnifier.isHidden())

# ── 4. 工作台构造 + 左栏跟画面一起钉死（不缩不滚）──
dlg = WorkbenchDialog()
dlg.show()          # offscreen 下不弹真窗，只让布局引擎认账
app.processEvents()
check("工作台初始 1:1（无实例回落）", dlg.preview._scale == 1.0)
check("左栏钉死 = 画面尺寸（回落基准 540x960）",
      (dlg.preview_box.width(), dlg.preview_box.height())
      == (dlg.preview._disp_w, dlg.preview._disp_h) == (540, 960))
# 钉死语义：外部想改左栏尺寸必须改不动（分隔条因此拖不动）
w0, h0 = dlg.preview_box.width(), dlg.preview_box.height()
dlg.preview_box.resize(300, 500)
check("左栏拒绝外部 resize（固定不移动）",
      (dlg.preview_box.width(), dlg.preview_box.height()) == (w0, h0))

dlg._place_preview()
check("重放幂等：仍 1:1、左栏不变",
      dlg.preview._scale == 1.0
      and (dlg.preview_box.width(), dlg.preview_box.height()) == (w0, h0))

# ── 5. 跟随模拟器窗口：RenderWindow = 1100x1956 ──
class FakeInst:
    running = True
    top_hwnd = 12345

ldplayer.get_instance = lambda *a, **k: FakeInst()
capture.find_render_window = lambda hwnd: {"width": 1100, "height": 1956}
dlg.cmb_instance.addItem("模拟器", 0)   # 有实例可选，_current_index() 有值

# 5a. 喂 1100x1956 原生帧 → 画面与该物理尺寸 1:1，左栏跟着一起变大
dlg.preview.set_frame(np.zeros((1956, 1100, 3), np.uint8))
dlg._place_preview()
check("模拟器1:1 → 预览同物理尺寸",
      abs(dlg.preview._scale - 1100 / 540) < 0.02
      and abs(dlg.preview._disp_w - 1100) <= 2
      and abs(dlg.preview._disp_h - 1956) <= 2)
check("画面变大 → 左栏跟着一起钉到新尺寸",
      abs(dlg.preview_box.width() - dlg.preview._disp_w) <= 2
      and abs(dlg.preview_box.height() - dlg.preview._disp_h) <= 2
      and dlg.preview_box.width() > w0)

# 5b. 窗口装不下也不缩：画面维持物理尺寸；窗缩小时左栏纹丝不动
# （被裁的是窗口的可视区，不是左栏。用户拖窗时 Qt 另按布局最小值兜底，
#  程序 resize 只钳显式 setMinimumSize，两回事，都动不了左栏。）
dlg.preview_box.resize(700, 1100)
dlg.resize(600, 600)
dlg._place_preview()
app.processEvents()
check("窗口装不下 → 画面仍 1:1（不缩不滚）",
      abs(dlg.preview._scale - 1100 / 540) < 0.02
      and abs(dlg.preview_box.width() - dlg.preview._disp_w) <= 2)
check("窗缩 → 左栏纹丝不动（min==max 钉死）",
      dlg.preview_box.width() == dlg.preview_box.minimumWidth()
      == dlg.preview_box.maximumWidth())

# ── 6. 自动拉大窗口（用户要求：空间不够就加大空间）──
# offscreen 测试屏固定 800x800，"拉大"分支只在真机大屏触发（需窗口窄、屏够）。
# 这里测物理极限兜底：屏幕 800x800 装不下 1100x1956 → 不崩、不弹窗、预览微缩。
class FakeInst:
    running = True
    top_hwnd = 12345

ldplayer.get_instance = lambda *a, **k: FakeInst()
capture.find_render_window = lambda hwnd: {"width": 1100, "height": 1956}
dlg = WorkbenchDialog()
dlg.cmb_instance.addItem("模拟器", 0)
w0, h0 = dlg.width(), dlg.height()
try:
    dlg._auto_resize()
    ok = True
except Exception:
    ok = False
check("屏幕不够→不崩不弹窗", ok and dlg.width() == w0 and dlg.height() == h0)

# ── 7. 定格按钮：勾选后 _tick 整拍早退（画面停在最后一帧）；切实例自动解冻 ──
check("控制条有可勾选「定格」", hasattr(dlg, "btn_freeze")
      and dlg.btn_freeze.isCheckable())
dlg.btn_freeze.setChecked(True)
check("勾选：文案切「恢复」", dlg.btn_freeze.text() == "恢复")
st = dlg._tick_status          # toggled 已把状态播成「已定格…」
dlg._tick()
check("定格后 _tick 早退（状态/画面不被动过）", dlg._tick_status == st)
dlg.btn_freeze.setChecked(False)
check("取消勾选：文案回「定格」", dlg.btn_freeze.text() == "定格")
n = dlg.cmb_instance.count()
dlg.cmb_instance.addItem("冒烟实例B", 999)     # 第6节已加过一台，再补一台好切换
dlg.btn_freeze.setChecked(True)
dlg.cmb_instance.setCurrentIndex(n)            # 换实例 → 应自动解冻
check("换实例自动解冻", not dlg.btn_freeze.isChecked())

dlg.close()

print("-" * 46)
if failures:
    print(f"SMOKE FAILED: {failures}")
    sys.exit(1)
print("smoke preview-follow-window ALL OK")