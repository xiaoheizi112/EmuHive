"""
tests/smoke_launch_gap.py — 「间隔多开」（启动节拍）回归冒烟

覆盖（改 main_window.batch_start_all 阶段2 / _launch_gapped / feature_panel 启动间隔行后必跑）：
1. 间隔 >0：batch_start_all 产出的 launch 作业逐个下达时真的错开约 gap 秒
   （首台立即，其余先睡再发）
2. 间隔 =0：全部连发（与改造前行为一致，回归护栏）
3. 等待期间可被取消：_launch_gapped 睡到一半收到取消 → 立刻返回、不发 launch

⚠ 隔离：临时配置单例 + 打桩 ldplayer（不真跑 ldconsole）。
跑法：python tests/smoke_launch_gap.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

_tmpdir = tempfile.mkdtemp()

from core import database, ldplayer
database.DB_PATH = Path(_tmpdir) / "lg.db"
database.init_db()
import core.config_manager as cm
cm._config = cm.ConfigManager(str(Path(_tmpdir) / "config.json"))


class _Inst:
    def __init__(self, index):
        self.index = index
        self.title = f"雷电模拟器{index}"
        self.top_hwnd = self.bind_hwnd = 0
        self.running = False
        self.pid = 0
        self.android_ready = False   # _tick_ready_watch 读它：缺了 processEvents 一转到就 AttributeError


ldplayer.list_instances = lambda: [_Inst(i) for i in (1, 2, 3)]
ldplayer.refresh_instances = ldplayer.list_instances
ldplayer._blocked_indexes = lambda: set()
ldplayer.ensure_resolution = lambda *a, **k: "跳过（已一致）"
ldplayer.wait_until_ready = lambda *a, **k: True

_LAUNCHED: list[tuple[int, float]] = []
ldplayer.launch = lambda index: (_LAUNCHED.append((index, time.time())), True)[1]

ok = True


def check(name, cond):
    global ok
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok = ok and bool(cond)


from ui.feature_panel import FeaturePanel
from ui.main_window import MainWindow, _launch_gapped

# 功能设置页：控件初值读 config、改动即存回；下限 1 秒（用户定，界面不给填 0）
cm._config.set("emulator.launch_interval_sec", 1.5)
fp = FeaturePanel()
check("控件初值跟 config 走", abs(fp.spn_launch_gap.value() - 1.5) < 1e-6)
fp.spn_launch_gap.setValue(2.0)
check("改动即写回 emulator.launch_interval_sec",
      abs(float(cm._config.get("emulator.launch_interval_sec")) - 2.0) < 1e-6)
# 下限 0.3 秒（2026-09-25 拍板：SSD 机器 1 秒/台纯排队太亏；0 后门照旧只在 config）
check("界面最低 0.3 秒：想填 0 被夹到下限", fp.spn_launch_gap.minimum() == 0.3)
cm._config.set("emulator.launch_interval_sec", 0)
# 必须落变量：临时 FeaturePanel() 取完属性就被解引用销毁，子控件成悬垂 C++ 指针
fp2 = FeaturePanel()
check("config 残留 0 时重开页不炸（显示夹到 0.3，读取侧仍认 0=连发）",
      fp2.spn_launch_gap.value() == 0.3)
cm._config.set("emulator.launch_interval_sec", 1.0)

# 挂机循环节拍行（general.frame_interval）：同款「即改即存」链路
cm._config.set("general.frame_interval", 0.35)
fp3 = FeaturePanel()
check("循环节拍控件初值跟 config 走", abs(fp3.spn_frame.value() - 0.35) < 1e-6)
fp3.spn_frame.setValue(0.8)
check("循环节拍改动即写回 general.frame_interval",
      abs(float(cm._config.get("general.frame_interval")) - 0.8) < 1e-6)
check("循环节拍上下限与 worker 钳位同口径（0.05~2.0）",
      fp3.spn_frame.minimum() == 0.05 and fp3.spn_frame.maximum() == 2.0)

mw = MainWindow()
Captured: list = []
mw._spawn_batch = lambda jobs, *a, **k: Captured.append(jobs)
for i in (1, 2, 3):
    database.add_account(ld_index=i, account=f"g{i}")

# ── 1. 间隔 0.4：launch 作业逐个跑，下达时刻真的错开 ──
cm._config.set("emulator.launch_interval_sec", 0.4)
mw.batch_start_all()
launch_jobs = [j for j in Captured[-1] if j[0].startswith("启动实例")]
check("3 台实例产出 3 条 launch 作业", len(launch_jobs) == 3)
_LAUNCHED.clear()
for _desc, fn, *args in launch_jobs:
    fn(*args)
gaps = [_LAUNCHED[k + 1][1] - _LAUNCHED[k][1] for k in range(len(_LAUNCHED) - 1)]
check(f"下达时刻按 ~0.4s 错峰（实测 {[round(g, 2) for g in gaps]}）",
      all(0.3 <= g <= 0.8 for g in gaps))
check("顺序仍按实例号", [i for i, _ in _LAUNCHED] == [1, 2, 3])

# ── 2. 间隔 0：全连发（原行为）──
cm._config.set("emulator.launch_interval_sec", 0)
mw.batch_start_all()
launch_jobs = [j for j in Captured[-1] if j[0].startswith("启动实例")]
_LAUNCHED.clear()
for _desc, fn, *args in launch_jobs:
    fn(*args)
span = _LAUNCHED[-1][1] - _LAUNCHED[0][1]
check(f"间隔 0 → 三台几乎同时下达（总跨度 {span:.2f}s）", span < 0.15)

# ── 3. 取消快停：睡到一半被取消 → 不 launch ──
_LAUNCHED.clear()
t0 = time.time()
_r = _launch_gapped(5.0, 9, lambda: time.time() - t0 > 0.1)
spent = time.time() - t0
check(f"取消后提前收手（{spent:.2f}s < 5s）且未下达",
      spent < 1.0 and _LAUNCHED == [] and "取消" in str(_r))

# ── 4. 批次收工尸体回归（2026-09-26「全部关闭」没反应病根）：
#    worker finished → deleteLater 销毁 C++ 对象后，界面 _worker 必须同步摘掉，
#    否则下一次开批 prev.isRunning() 炸 RuntimeError，崩在槽函数第一行、零日志 ──
insts = [_Inst(i) for i in (1, 2, 3)]
insts[0].running = True                      # 让 batch_quit_all 有的可关
ldplayer.list_instances = lambda: insts
ldplayer.quit_indexes_now = lambda idxs: "已关"
mw.batch_quit_all()
t0 = time.time()
while time.time() - t0 < 5 and mw._worker is not None:
    app.processEvents()
    time.sleep(0.02)
check("批次收工后界面指针已摘（_forget_worker 生效）", mw._worker is None)
for _ in range(30):                          # 再转一会儿，让 deleteLater 真销毁
    app.processEvents()
    time.sleep(0.02)
try:
    mw.batch_quit_all()                      # 旧代码：这里 prev.isRunning() 必炸
    second = True
except RuntimeError:
    second = False
check("连点第二次「全部关闭」不踩尸体", second)
t0 = time.time()
while time.time() - t0 < 5 and mw._worker is not None:
    app.processEvents()
    time.sleep(0.02)

print("ALL OK" if ok else "存在 FAIL")
sys.exit(0 if ok else 1)
