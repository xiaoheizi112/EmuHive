# 冒烟：工作台预览后台抓帧（2026-10-04 修「全部启动→点工作台 未响应」；
# 2026-10-11 预览取帧走 adb 单次快照主路，校准/裁图整套随窗口截图退出）。
# 守的链路：_tick 不再 GUI 线程截图——只做闸门判断+投递任务给后台线程；
# 一帧在飞(_grab_busy)绝不投第二帧；定格/框选中整拍早退；_run_job 走
# grab_with_fallback（窗口 capture()/calibrate 被调到直接炸=防复活）；
# 回帧 _on_frame 才落帧、喂预览与采集面板；飞行中换实例整帧作废；
# _run_job 异常护栏把炸帧变文案。
# 全程不拉真线程（_PreviewGrabber 整体桩掉，_run_job 直调）、capture.* 全桩，
# 绝不打真实雷电；配置单例换临时目录（冒烟写盘陷阱铁律）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_workbench_grab.py
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

import core.capture as capture
import core.ldplayer as ldplayer
import ui.workbench as wb
from ui.workbench import WorkbenchDialog, _run_job

ldplayer.list_instances = lambda: []
ldplayer.get_instance = lambda *a, **k: None
capture.find_render_window = lambda h: None   # 漂移复核默认桩死：冒烟绝不枚举真窗口子句柄

failures = []


def check(label, cond):
    if cond:
        print(f"  ✓ {label}")
    else:
        failures.append(label)
        print(f"  ✗ {label}")


CAL = {"offset_x": 0, "offset_y": 0, "width": 582, "height": 960, "score": 1.0}
ADB_IMG = np.zeros((960, 540, 3), np.uint8) + 4   # adb 取帧帧（纯安卓基准尺寸）

# ── ① _run_job 纯逻辑（直调，不拉线程）──
# 防复活护栏：预览与挂机同路，谁再调窗口截图/校准就地炸（2026-10-09 决策）
def _no_window(h, **kw):
    raise AssertionError("预览不该调窗口截图 capture()")
def _no_calibrate(*a, **k):
    raise AssertionError("预览帧无边框可裁，不该再校准")
capture.capture = _no_window
capture.calibrate = _no_calibrate

# 2026-10-11：取帧唯一路 = adb 单次快照（scrcpy 后端已移除），grab_with_fallback 恒走 adb
capture.adb_frame = lambda idx, **kw: ADB_IMG
idxA, imgA, errA = _run_job((0, 12345))
check("① adb 取帧帧直回（回投三元组无 calib 位）",
      imgA is ADB_IMG and errA == "")

capture.adb_frame = lambda idx, **kw: None
idxB, imgB, errB = _run_job((0, 12345))
check("① 取不到帧：(None, '')，GUI 侧播「取帧失败」",
      imgB is None and errB == "")

# 强制 adb 逃生口（emulator.capture_adb_only=true）：仍走 adb（行为同默认）
cm._config.set("emulator.capture_adb_only", True)
capture.adb_frame = lambda idx, **kw: ADB_IMG
idxF, imgF, errF = _run_job((0, 12345))
check("① capture_adb_only=true：直取 adb 帧（窗口不碰）",
      imgF is ADB_IMG and errF == "")
cm._config.set("emulator.capture_adb_only", False)

# 校准漂移每帧复核：雷电左侧工具栏展开/收起、拖窗口 → 安卓区搬家，旧 calib
# 裁歪会把窗口边框裁进预览（用户实拍）；几何可得就自动换新，找不到不碰原值
_GEO = {"offset_x": 64, "offset_y": 34, "width": 540, "height": 960, "score": 1.0}
capture.find_render_window = lambda h: dict(_GEO)
d1 = capture.calib_drift_check(CAL, 1)
check("漂移>2px：换新 calib（offset_x 0→64）", d1 is not CAL and d1["offset_x"] == 64)
check("无漂移（逐键相等）：原对象原样返回（不白换）",
      capture.calib_drift_check(_GEO, 1) is _GEO)
check("缺校准但几何可得：补上（预览带边框的自愈路）",
      capture.calib_drift_check(None, 1)["offset_x"] == 64)
capture.find_render_window = lambda h: None       # 恢复顶部默认桩
check("非雷电（找不到子窗口）：图像校准值不碰",
      capture.calib_drift_check(CAL, 1) is CAL)


def _boom(idx, **kw):
    raise RuntimeError("炸")


capture.adb_frame = _boom
idx3, img3, err3 = _run_job((7, 1))
check("① 异常护栏：炸帧变「预览异常」文案（线程不带死）",
      img3 is None and "预览异常" in err3 and idx3 == 7)
capture.adb_frame = lambda idx, **kw: None

# ── ② GUI 侧：投递闸门 + 回帧落数据（后台线程整体桩掉，只记 post）──
posted = []


class FakeGrabber:
    def post(self, job):
        posted.append(job)


wb._PreviewGrabber = lambda emit: FakeGrabber()   # __init__ 拿到的就是假货

capture.find_render_window = lambda hwnd: {"width": 540, "height": 960}
dlg = WorkbenchDialog()


class FakeInst:
    running = True
    top_hwnd = 12345


ldplayer.get_instance = lambda *a, **k: FakeInst()
dlg.cmb_instance.addItem("模拟器", 0)
dlg._tick_status = ""

dlg.btn_freeze.setChecked(True)
dlg._tick()
check("定格中：整拍早退，一帧不投", posted == [])
dlg.btn_freeze.setChecked(False)

dlg._tick()
check("② 首拍：投 (idx, hwnd) 一帧 + busy 闸门落起",
      len(posted) == 1 and posted[0] == (0, 12345) and dlg._grab_busy is True)
dlg._tick()
check("一帧在飞：第二拍跳过（不攒队列，等回帧）", len(posted) == 1)

dlg._on_frame(0, ADB_IMG, "")
check("② 回帧落数据：busy 解除 + 基准帧 540×960 喂进预览与采集面板",
      not dlg._grab_busy
      and dlg._frame.shape[1] == 540 and dlg._frame.shape[0] == 960
      and dlg.tpl_panel._frame is not None
      and "预览中 540x960" in dlg._tick_status)

# 错误帧：busy 必须照样解除（否则预览永久哑火）
dlg._on_frame(0, None, "预览异常：x")
check("② 错误帧：busy 解除、状态播错误",
      not dlg._grab_busy and "预览异常" in dlg._tick_status)

# 飞行中换实例：旧帧整帧作废
posted.clear()
dlg.btn_freeze.setChecked(True)
dlg.cmb_instance.addItem("另一台", 7)
dlg.cmb_instance.setCurrentIndex(1)           # 真切过去才触发 _on_instance_changed
check("② 换实例：定格自动解除", dlg.btn_freeze.isChecked() is False)
dlg._tick()
check("② 换实例后首拍：投递新 (idx, hwnd)",
      len(posted) == 1 and posted[0] == (7, 12345))
dlg._frame = "SENTINEL"
dlg._on_frame(0, ADB_IMG, "")                  # 迟到的旧实例帧（当前已切到 7）
check("② 飞行中换了实例：旧帧作废（SENTINEL 不被覆写、busy 照常解除）",
      dlg._frame == "SENTINEL" and not dlg._grab_busy)

# 实例没跑：不投帧，状态播「实例未运行」
posted.clear()
ldplayer.get_instance = lambda *a, **k: None
dlg._tick()
check("实例未运行：零投递 + 状态提示",
      posted == [] and "实例未运行" in dlg._tick_status)

dlg.close()
print("-" * 46)
if failures:
    print(f"SMOKE FAILED: {failures}")
    sys.exit(1)
print("SMOKE PASS")
