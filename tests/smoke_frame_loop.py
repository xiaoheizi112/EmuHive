# 冒烟：bot/frame_loop.py 单拍共享执行核（7×24 审计 P0：终止异常不许被拍级兜底吞掉）
# 守五条：
#   ①FlowTimeoutExit / AccountBannedExit 从 tick_fn 抛出必须原样穿透核（旧版
#     worker 通用 except 吞了它们 → 「超时退出程序」「封号标记」节点双双失效）；
#   ②杂散拍级异常 → 计数重试自动恢复；连犯达阈值 → failed 且 on_exc_failed 收到原文；
#   ③截图失败：实例已关 → stopped，还在 → 等恢复继续；
#   ④暂停钩子 on_pause/on_resume 按序触发；clamp_frame_interval 校验钳位；
#   ⑤flow_test 迁移后三大漂移点补齐（源码守卫 + 行为：坏间隔钳位、重校准 5 秒冷却）。
# 红线：离屏 + 临时配置单例，不打真模拟器、不碰真 configs/config.json、不写库。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_frame_loop.py
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from bot.fsm import AccountBannedExit, FlowTimeoutExit
from bot.frame_loop import clamp_frame_interval, run_frame_loop

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


NO_SLEEP = lambda s: False   # 免真睡（退避 0.5s×N）


# ── ① 终止异常通道：穿透，不许吞 ────────────────────────────────
for exc_cls, label in ((FlowTimeoutExit, "FlowTimeoutExit（超时退出程序）"),
                       (AccountBannedExit, "AccountBannedExit（封号标记）")):
    seen = []
    try:
        run_frame_loop(lambda: "IMG",
                       (lambda e=exc_cls: (_ for _ in ()).throw(e("x"))),
                       lambda: False, wait_fn=NO_SLEEP, log=lambda l, m: seen.append((l, m)))
        check(f"① {label} 原样穿透核（不吞、不降级重试）", False)
    except exc_cls:
        # 穿透且核内没打过"拍级异常"WARN（吞的证据 = 出现过兜底日志）
        check(f"① {label} 原样穿透核（不吞、不降级重试）",
              not any(l == "WARN" and "拍级异常" in m for l, m in seen))


# ── ② 杂散异常：偶发自愈 / 连犯封顶 ────────────────────────────
class _Flaky:
    def __init__(self, n_raise):
        self.n = n_raise
        self.beats = 0

    def tick(self):
        self.beats += 1
        if self.beats <= self.n:
            raise RuntimeError("模拟 adb 抽风")
        return "done"


logs, f1 = [], _Flaky(2)
r = run_frame_loop(lambda: "IMG", f1.tick, lambda: False,
                   wait_fn=NO_SLEEP, log=lambda l, m: logs.append((l, m)))
check("② 偶发 2 次杂散异常 → 自愈跑完 done（WARN 计数在案）",
      r == "done" and f1.beats == 3
      and any(l == "WARN" and "拍级异常" in m for l, m in logs))

exc_cb, f2 = [], _Flaky(999)
import bot.frame_loop as fl
_old_max = fl.EXC_FAILS_MAX
fl.EXC_FAILS_MAX = 4          # 冒烟缩阈值快进封顶路径（生产=30，语义同一处代码）
try:
    r = run_frame_loop(lambda: "IMG", f2.tick, lambda: False, wait_fn=NO_SLEEP,
                       on_exc_failed=lambda e, n: exc_cb.append((str(e), n)))
finally:
    fl.EXC_FAILS_MAX = _old_max
check("② 连犯达阈值 → failed 且 on_exc_failed 带错误原文+次数",
      r == "failed" and exc_cb and "模拟 adb 抽风" in exc_cb[0][0] and exc_cb[0][1] == 4)


# ── ③ 截图失败两分：关了的 vs 挡住的 ───────────────────────────
seen = []
r = run_frame_loop(lambda: None, lambda: "done", lambda: False,
                   instance_alive=lambda: False, wait_fn=NO_SLEEP,
                   log=lambda l, m: seen.append(m))
check("③ 截不到帧+实例已关 → stopped（话术可换）",
      r == "stopped" and seen == ["检测到模拟器已关闭，挂机自动停止"])

beats = [0]
_grabs = [0]


def _grab_flaky_once():
    _grabs[0] += 1
    return None if _grabs[0] == 1 else "IMG"


def _tick_once():
    beats[0] += 1
    return "done"


r = run_frame_loop(_grab_flaky_once, _tick_once, lambda: False,
                   instance_alive=lambda: True, wait_fn=NO_SLEEP, log=lambda l, m: None)
check("③ 截不到帧但实例还在 → 等恢复，下一帧照常推进到 done",
      r == "done" and beats[0] == 1 and _grabs[0] == 2)


# ── ④ 暂停钩子 + 间隔钳位 ──────────────────────────────────────
order = []
_pa = [0]


def _paused_once():
    _pa[0] += 1
    return _pa[0] == 1     # 第一次查=暂停中；循环内再查已解除（不真等事件）


r = run_frame_loop(lambda: "IMG", lambda: "done", lambda: False,
                   is_paused=_paused_once,
                   on_pause=lambda: order.append("pause"),
                   on_resume=lambda: order.append("resume"),
                   wait_fn=NO_SLEEP)
check("④ 暂停进钩子、解除出钩子，顺序 pause→resume→跑完 done",
      order == ["pause", "resume"] and r == "done")
check("④ clamp_frame_interval：坏值/越界回落默认，好值照收",
      clamp_frame_interval("乱值") == 0.5 and clamp_frame_interval(None) == 0.5
      and clamp_frame_interval(0) == 0.5 and clamp_frame_interval(99) == 0.5
      and clamp_frame_interval(1.0) == 1.0 and clamp_frame_interval("0.3") == 0.3)


# ── ⑤ worker/flow_test 接线（迁移后行为）───────────────────────
from bot.worker import AccountWorker

w = AccountWorker(99, task_name="flow:冒烟")
w._stop, w._pause, w._hard_stop = threading.Event(), threading.Event(), False
w._interruptible_wait = NO_SLEEP
w._grab_frame = lambda: "IMG"
class _ExitStub:
    steps, idx = [], 0

    def tick(self):
        raise FlowTimeoutExit("到点收工")


try:
    w._run_single_task(object(), _ExitStub())
    check("⑤ worker._run_single_task 把 FlowTimeoutExit 交给 run() 专属 handler（不吞）", False)
except FlowTimeoutExit:
    check("⑤ worker._run_single_task 把 FlowTimeoutExit 交给 run() 专属 handler（不吞）", True)

import bot.flow_test as ft
import bot.frame_source as fsrc
import bot.worker as bw

_here = os.path.dirname(os.path.abspath(__file__))
_src = open(os.path.join(_here, "..", "bot", "flow_test.py"), encoding="utf-8").read()
_wsrc = open(os.path.join(_here, "..", "bot", "worker.py"), encoding="utf-8").read()
_fsrc = open(os.path.join(_here, "..", "bot", "frame_source.py"), encoding="utf-8").read()
_rsrc = open(os.path.join(_here, "..", "tools", "flow_run.py"), encoding="utf-8").read()
check("⑤ flow_test 已接共享核+钳位（源码守卫）",
      "run_frame_loop" in _src and "clamp_frame_interval" in _src)
check("⑤ 取帧+校准三份收口 frame_source（兜底/冷却/漂移内化，源码守卫）",
      "_last_quick_calib" in _fsrc and "is_instance_alive" in _fsrc
      and "calib_drift_check" in _fsrc and "grab_with_fallback" in _fsrc
      and "launch_calibrate_timeout" in _fsrc)
check("⑤ scrcpy 已整体移除：取帧样板不再调用 scrcpy 就绪/预热 API（源码守卫）",
      "capture.scrcpy" not in _fsrc and "prewarm_scrcpy" not in _src
      and "prewarm_scrcpy" not in _wsrc and "_wait_scrcpy_ready" not in _src
      and "_wait_scrcpy_ready" not in _wsrc)
check("⑤ worker/flow_test 不再残留第二份取帧样板（薄壳调 FrameSource）",
      "grab_with_fallback" not in _src and "calib_drift_check" not in _src
      and "grab_with_fallback" not in _wsrc and "calib_drift_check" not in _wsrc
      and "FrameSource(" in _src and "FrameSource(" in _wsrc)
check("⑤ flow_run 接入 FrameSource+钳位（旧手搓漂移：None 崩/全量预算/裸 float）",
      "FrameSource" in _rsrc and "clamp_frame_interval" in _rsrc
      and "grab_with_fallback" not in _rsrc)

cm._config.set("general.frame_interval", "乱值")
tw = ft.FlowTestWorker(0, {"version": 2, "nodes": [], "edges": []})
check("⑤ flow_test 坏间隔经共享核钳回默认（旧版裸 float 直吃）",
      tw.frame_interval == 0.5)


class _FakeInst:
    def __init__(self):
        self.running = True
        self.top_hwnd = 1


class _FakeCap:
    def __init__(self):
        self.calib_calls = 0

    def calibrate(self, hwnd, index, **kw):
        self.calib_calls += 1
        return None

    def capture(self, hwnd, method="auto"):
        return object()

    def grab_with_fallback(self, hwnd, index, method="auto"):
        return self.capture(hwnd, method=method), False   # 冒烟绝不真打 adb

    def calib_drift_check(self, calib, hwnd):
        return calib

    def to_base(self, img):
        return img

    def crop_android(self, img, calib):
        return img


inst = _FakeInst()
cap = _FakeCap()
# 取帧样板已收口 bot/frame_source：打桩要打在 FrameSource 用的模块引用上
_old_li, _old_cap = fsrc.ldplayer, fsrc.capture
fsrc.ldplayer = type("M", (), {
    "get_instance": staticmethod(lambda i: inst),
    "is_instance_alive": staticmethod(lambda i, h: True)})
fsrc.capture = cap
try:
    tw.ctx.hwnd = 1
    tw._grab_frame()                      # 句柄一致：不触发重校准
    inst.top_hwnd = 2
    tw._grab_frame()                      # 换句柄：第 1 次重校准
    inst.top_hwnd = 3
    tw._grab_frame()                      # 5 秒冷却内：不再校准（旧版每拍都来）
    check("⑤ flow_test 重校准 5 秒冷却生效（两次换句柄只校准一次）",
          cap.calib_calls == 1)
finally:
    fsrc.ldplayer, fsrc.capture = _old_li, _old_cap

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke frame-loop ALL OK")
