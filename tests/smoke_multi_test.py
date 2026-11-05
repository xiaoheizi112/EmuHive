# 冒烟：工作台「多开测试」——同一条流程在多台实例上各跑一遍（2026-10-10 新增）
# 守的链路：
# ① MultiTestController 逐台**错峰起跑**（各台 start 时刻按 gap 错开，绝不一次全拥上）；
# ② 每台起一个 FlowTestWorker（冒烟里用假线程替代，不碰模拟器），逐台收尾汇总；
# ③ 准入检查对**每一台**都查：没运行的 / 正在挂机的都挡掉并在弹窗里点名原因
#    （复用 workbench._gate_reason）；
# ④ 结果表四列填对（实例/结果/卡在哪一步/失败截图路径）；
# ⑤ 「停止全部」能掐掉所有台（在跑的请求停止、没起的记已停）；
# ⑥ 多开测试期间单实例「▶ 测试流程」按钮禁用，跑完解锁；
# ⑦ WorkbenchDialog() 能正常构造（不崩——最高优先级底线）。
# 铁律：临时配置单例 + 打桩 ldplayer/capture（不真跑模拟器、不打真 config）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_multi_test.py
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QEventLoop, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication.instance() or QApplication([])

import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

import core.capture as capture
import core.ldplayer as ldplayer
import ui.multi_test_dialog as mt

capture.find_render_window = lambda h: None   # 冒烟绝不枚举真窗口子句柄

failures = []


def check(label, cond):
    if cond:
        print(f"  [OK] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}")


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


# ── 假 FlowTestWorker：形状照抄真货（三信号 + isRunning/request_stop/wait），
#    不拉真线程——start() 立即记下起跑时刻并 emit 一个步骤，收尾靠 smoke 主动驱动 ──
class FakeWorker(QObject):
    action_log = Signal(int, str, str)
    step_changed = Signal(int, str)
    finished_state = Signal(int, str)
    REGISTRY: list = []

    def __init__(self, idx, graph, parent=None, flow_name=""):
        super().__init__(parent)
        self.ld_index = idx
        self._running = False
        self.stops = 0
        self.start_time = None
        FakeWorker.REGISTRY.append(self)

    def start(self):
        self._running = True
        self.start_time = time.time()
        self.step_changed.emit(self.ld_index, "步骤·开局")

    def isRunning(self):
        return self._running

    def request_stop(self):
        self.stops += 1
        self._running = False
        self.finished_state.emit(self.ld_index, "stopped")

    def wait(self, msec=0):
        return True

    def finish(self, state="done"):
        self._running = False
        self.finished_state.emit(self.ld_index, state)

    def fail_at(self, step):
        self.step_changed.emit(self.ld_index, step)
        self._running = False
        self.finished_state.emit(self.ld_index, "failed")


mt.FlowTestWorker = FakeWorker   # 控制器默认工厂按模块全局名取 → 打桩生效

# ── 实例桩 ──
class _Inst:
    def __init__(self, index, running=True):
        self.index = index
        self.title = f"雷电模拟器{index}"
        self.top_hwnd = 1000 + index
        self.bind_hwnd = 0
        self.running = running
        self.pid = 1


INSTANCES = {1: _Inst(1), 2: _Inst(2), 3: _Inst(3), 4: _Inst(4)}
ldplayer.list_instances = lambda: list(INSTANCES.values())
ldplayer.get_instance = lambda i: INSTANCES.get(i)

GRAPH = {"version": 2, "nodes": [{"id": "n1", "type": "click", "x": 0, "y": 0}],
         "edges": []}

# ── ① MultiTestController：错峰起跑 + 逐台收尾 + 汇总 ──
print("① MultiTestController 错峰起跑 / 收尾汇总")
FakeWorker.REGISTRY = []
done_signals = []
ctrl = mt.MultiTestController(GRAPH, "甲", [1, 2, 3], gap_ms=60)
ctrl.all_done.connect(lambda: done_signals.append(1))
ctrl.start()
wait(30)
check("首台立即起跑（不等 gap）", len([w for w in FakeWorker.REGISTRY if w.start_time]) == 1)
wait(200)   # 3 台 * 60ms gap 走完
ws = sorted(FakeWorker.REGISTRY, key=lambda w: w.start_time or 0)
check("三台都起了跑（各一个假 worker）", len(ws) == 3)
times = [w.start_time for w in ws]
gaps = [round(times[k + 1] - times[k], 3) for k in range(len(times) - 1)]
check(f"起跑时刻按 ~0.06s 错峰（实测 {gaps}）", all(0.03 <= g <= 0.25 for g in gaps))
check("起跑顺序 = 入参顺序", [w.ld_index for w in ws] == [1, 2, 3])

# 逐台收尾：done / failed / stopped
by_idx = {w.ld_index: w for w in ws}
by_idx[1].finish("done")
by_idx[2].fail_at("等弹窗")
res = ctrl.results()
check("done 台结果=done", res[1]["state"] == "done")
check("failed 台结果=failed + 卡在哪一步记下", res[2]["state"] == "failed"
      and res[2]["step"] == "等弹窗")
check("failed 台带失败截图路径 logs/fail_capture/inst2",
      res[2]["fail_dir"] == "logs/fail_capture/inst2")
check("尚有台在跑：all_done 未发", done_signals == [])
by_idx[3].finish("stopped")
check("三台全收口：all_done 只发一发", done_signals == [1])
check("stopped 台结果=stopped", ctrl.results()[3]["state"] == "stopped")
check("收工后 running()=False", ctrl.running() is False)

# ── ② 停止全部：掐在跑的 + 未起的记已停 ──
print("② 「停止全部」掐全部")
FakeWorker.REGISTRY = []
ctrl2 = mt.MultiTestController(GRAPH, "甲", [1, 2, 3], gap_ms=5000)   # 大 gap：只起首台
ctrl2.start()
wait(30)
check("大 gap 下只有首台在跑", len([w for w in FakeWorker.REGISTRY if w.isRunning()]) == 1)
ctrl2.request_stop_all()
res2 = ctrl2.results()
check("在跑的台被 request_stop（收到停止请求）",
      FakeWorker.REGISTRY[0].stops == 1)
check("在跑的台结果=stopped", res2[1]["state"] == "stopped")
check("还没起的两台直接记已停（不再起跑）",
      res2[2]["state"] == "stopped" and res2[3]["state"] == "stopped")
wait(200)
check("停止后不再起新台（假 worker 数没涨）", len(FakeWorker.REGISTRY) == 1)

# ── ③ 弹窗 configure：可测勾选、不可测灰显点名 ──
print("③ MultiTestDialog configure 候选渲染")
dlg = mt.MultiTestDialog()
dlg.configure(GRAPH, "甲", [
    {"idx": 1, "label": "[1] 雷电模拟器1", "selectable": True, "reason": ""},
    {"idx": 3, "label": "[3] 雷电模拟器3", "selectable": False, "reason": "实例 3 未运行，无法测试"},
    {"idx": 4, "label": "[4] 雷电模拟器4", "selectable": False, "reason": "实例 4 正在挂机，请先停止该实例再测试"},
])
from PySide6.QtCore import Qt
it0 = dlg.lst_instances.item(0)
it1 = dlg.lst_instances.item(1)
check("可测行默认可勾且已勾", bool(it0.flags() & Qt.ItemIsUserCheckable)
      and it0.checkState() == Qt.Checked)
check("不可测行不可勾（撤销启用位）", not (it1.flags() & Qt.ItemIsEnabled))
check("不可测行点名原因（未运行）", "未运行" in it1.text())
check("不可测行点名原因（正在挂机）", "正在挂机" in dlg.lst_instances.item(2).text())
check("有可测项 → 开始按钮可用", dlg.btn_start.isEnabled() is True)
check("勾选集合 = 可测项", dlg._checked_indexes() == [1])

# ── ④ 弹窗跑一轮：表填对 ──
print("④ 结果表四列填对")
FakeWorker.REGISTRY = []
dlg2 = mt.MultiTestDialog(gap_ms=40)
started = []
dlg2.multi_started.connect(lambda: started.append(1))
dlg2.configure(GRAPH, "甲", [
    {"idx": 1, "label": "[1] a", "selectable": True, "reason": ""},
    {"idx": 2, "label": "[2] b", "selectable": True, "reason": ""},
])
dlg2._on_start()
check("起跑发 multi_started（外壳据此锁单实例按钮）", started == [1])
check("起跑后表建了两行", dlg2.tbl.rowCount() == 2)
wait(150)
w = {x.ld_index: x for x in FakeWorker.REGISTRY}
w[1].finish("done")
w[2].fail_at("卡在点图标")
# 行号按勾选顺序
row_of = dlg2._rows
check("结果列：done 台显示 ✓ 完成",
      dlg2.tbl.item(row_of[1], 1).text() == "✓ 完成")
check("结果列：failed 台显示 ✗ 失败",
      dlg2.tbl.item(row_of[2], 1).text() == "✗ 失败")
check("卡在哪一步列：failed 台记下步骤",
      dlg2.tbl.item(row_of[2], 2).text() == "卡在点图标")
check("失败截图路径列填对",
      dlg2.tbl.item(row_of[2], 3).text() == "logs/fail_capture/inst2")
check("全部收工 → 开始按钮解锁 + 停止按钮变灰",
      dlg2.btn_start.isEnabled() is True and dlg2.btn_stop.isEnabled() is False)

# 停止全部按钮
FakeWorker.REGISTRY = []
dlg3 = mt.MultiTestDialog(gap_ms=5000)
dlg3.configure(GRAPH, "甲", [{"idx": 1, "label": "[1] a", "selectable": True, "reason": ""},
                             {"idx": 2, "label": "[2] b", "selectable": True, "reason": ""}])
dlg3._on_start()
wait(30)
dlg3._on_stop()
check("弹窗停止按钮 → 掐全部",
      all(r["state"] == "stopped" for r in dlg3._controller.results().values()))

# ── ⑤ 工作台准入：每台都查 + 单实例按钮禁用 + 构造不崩 ──
print("⑤ 工作台 _on_multi_test_requested 准入 + 单实例按钮禁用")
from ui.workbench import WorkbenchDialog


class FakeParent(QWidget):
    """假主窗口：提供 busy_instances（2 号在挂机）。"""
    def busy_instances(self):
        return {2}


INSTANCES[3].running = False   # 3 号没运行
p = FakeParent()
wb = WorkbenchDialog(p)        # ← 底线：构造不崩
check("WorkbenchDialog() 正常构造（不崩）", wb is not None)

wb._on_multi_test_requested()
d = wb._multi_test_dlg
check("多开弹窗已建并持有引用", d is not None)
# 弹窗候选：1 可测 / 2 挂机不可测 / 3 未运行不可测 / 4 可测
cand = {d.lst_instances.item(i).data(Qt.UserRole): d.lst_instances.item(i)
        for i in range(d.lst_instances.count())}
check("2 号（挂机）不可勾 + 点名「正在挂机」",
      not (cand[2].flags() & Qt.ItemIsEnabled) and "正在挂机" in cand[2].text())
check("3 号（未运行）不可勾 + 点名「未运行」",
      not (cand[3].flags() & Qt.ItemIsEnabled) and "未运行" in cand[3].text())
check("1/4 号可测可勾",
      (cand[1].flags() & Qt.ItemIsEnabled) and (cand[4].flags() & Qt.ItemIsEnabled))

# 起跑 → 单实例按钮禁用；收工 → 解锁
FakeWorker.REGISTRY = []
d._on_start()
check("多开测试期间单实例「▶ 测试流程」按钮禁用",
      wb.flow_panel.btn_test.isEnabled() is False)
check("多开测试期间「▶ 多开测试」按钮也禁用",
      wb.flow_panel.btn_multi_test.isEnabled() is False)
wait(900)   # 默认 gap 700ms：等各台都起跑
for x in list(FakeWorker.REGISTRY):
    x.finish("done")
wait(50)
check("跑完后单实例按钮解锁",
      wb.flow_panel.btn_test.isEnabled() is True)
check("跑完后多开按钮解锁",
      wb.flow_panel.btn_multi_test.isEnabled() is True)

# 单实例测试进行中拒开多开测试（互斥）
class _RunningWorker:
    def isRunning(self):
        return True
wb._test_worker = _RunningWorker()
n_before = wb._multi_test_dlg
wb._on_multi_test_requested()
check("单实例测试进行中：拒开多开测试（无新动作）",
      wb._multi_test_dlg is n_before and "进行中" in wb.lbl_status.text())
wb._test_worker = None

# ── ⑥ 全无可用实例：状态栏提示 ──
INSTANCES[1].running = False
INSTANCES[4].running = False
wb._on_multi_test_requested()
check("全不可测：状态栏提示且不开跑",
      "没有可测的实例" in wb.lbl_status.text())

wb.close()
d.close()
dlg.close()
dlg2.close()
dlg3.close()

print("-" * 46)
if failures:
    print(f"SMOKE FAILED: {failures}")
    sys.exit(1)
print("SMOKE PASS")