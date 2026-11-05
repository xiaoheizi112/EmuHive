"""
tests/smoke_bench_ui.py — 「测试设置」页「容量压测」界面链路回归冒烟（离屏）

覆盖（改 ui/test_panel.py 的容量压测分组框 / _BenchWorker / 存 json，
      或 tools/bench_capacity.py 的 capacity_steps·demo_steps·run_bench·recommend 后必跑）：
注：容量压测 2026-10-10 从「功能设置」拆到独立「测试设置」页（ui/test_panel.py），
    本冒烟原针对 FeaturePanel，已改指 TestPanel。
1. 输入校验：空范围被挡「请填实例范围」；乱序/重叠走 parse_indexes 报错转人话；
   合法范围切档走 capacity_steps（4/8/12… 递增）
2. 前提校验：实测要求实例 running，未启动的实例点名挡掉（不真压测）
3. demo 模式：不碰模拟器也能跑出结果表（桩掉真压测 _bench_runner，
   只验「后台线程→信号→填表→收结论」这条界面链路）
4. 非阻塞：压测跑在 QThread 里，_on_bench 立即返回（界面不卡）
5. 取消：跑中发 cancel → 回投态 is_cancelled True、收工时按钮放开
6. 结果表填对：N/均值/p95/超拍率/达标列齐、行数=档位数、建议值 recomnend 一致
7. 存 json：_on_bench_save 落盘键齐（打桩 QFileDialog 选临时路径，atomic_dump）

⚠ 隔离：临时配置单例（绝不写真 configs/config.json）；打桩 ldplayer.list_instances
   与 _bench_runner（不碰真模拟器/真模板库）；存盘选临时目录（不点模态框）。
⚠ 冒烟三铁律：离屏模态框先桩（QFileDialog.getSaveFileName）；动盘前换临时配置单例；
   找控件用 isVisibleTo（构造期无父控件 setVisible 会弹孤儿窗）。
跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_bench_ui.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 冒烟输出含 ✅/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog

app = QApplication.instance() or QApplication([])

# ── 隔离 1：临时配置单例（CLAUDE.md 冒烟写盘红线，先于任何 cfg.save）──
_tmpdir = tempfile.mkdtemp(prefix="bench_ui_smoke_")
import core.config_manager as cm
cm._config = cm.ConfigManager(str(Path(_tmpdir) / "config.json"))
cfg = cm._config

from core import ldplayer
from ui.test_panel import TestPanel

ok = True


def check(name, cond):
    global ok
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok = ok and bool(cond)


def pump(cond, timeout=20.0):
    """转到条件满足或超时：给后台线程/信号队列回投的机会。返回是否满足。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    app.processEvents()
    return cond()


# ── 隔离 2：假实例（1~8 台，可切 running）+ 计数式桩压测 ──
class _Inst:
    def __init__(self, index, running=True):
        self.index = index
        self.title = f"雷电模拟器{index}"
        self.top_hwnd = self.bind_hwnd = 0
        self.pid = 456 if running else 0
        self.android_started = 1
        self.extra = 0
        self.running = running


_running_flags = {i: True for i in range(1, 9)}
ldplayer.list_instances = lambda: [_Inst(i, _running_flags[i]) for i in range(1, 9)]


def fake_runner(steps, rounds, budget, is_cancelled, on_result):
    """桩 _bench_runner：不取帧不找图，直接合成「每档都达标」的结果并回调。
    只让冒烟验界面链路（线程→信号→填表→收结论），真压测算法由
    tests/smoke_bench_capacity.py 与 CLI 守。"""
    from tools import bench_capacity as bc
    per_n = {}
    for grp in steps:
        if is_cancelled():
            return {"per_n": per_n, "cancelled": True}
        n = len(grp)
        res = {"N": n, "indexes": list(grp),
               "stats": {"n": n * rounds, "mean": 0.01, "p95": 0.02, "max": 0.03,
                         "over_ratio": 0.0, "wall_mean": 0.02}}
        per_n[n] = res
        on_result(n, res)
    return {"per_n": per_n, "cancelled": False}


# ── ① 纯函数：capacity_steps / demo_steps 与 CLI 同口径 ──
from tools import bench_capacity as bc
check("① capacity_steps 按 4/8/12… 递增切档（0-15 → [4,8,12,16]）",
      [len(s) for s in bc.capacity_steps(list(range(16)))] == [4, 8, 12, 16])
check("① capacity_steps 不足 4 也至少一档（0-2 → [3]）",
      [len(s) for s in bc.capacity_steps([0, 1, 2])] == [3])
check("① demo_steps 固定 4/8/16 三档", [len(s) for s in bc.demo_steps()] == [4, 8, 16])

# ── ② 控件初值与布局 ──
panel = TestPanel()
check("② 轮数默认 20（与 CLI --rounds 同口径）", panel.spn_bench_rounds.value() == 20)
check("② demo 默认不勾", panel.chk_bench_demo.isChecked() is False)
check("② 结果表 5 列表头齐",
      [panel.tbl_bench.horizontalHeaderItem(c).text() for c in range(5)]
      == ["N 台", "均值(s)", "p95(s)", "超拍率", "是否达标"])
check("② 取消按钮初始禁用、保存按钮初始禁用",
      not panel.btn_bench_cancel.isEnabled() and not panel.btn_bench_save.isEnabled())

# ── ③ 输入校验：空 / 乱序 / 未启动 ──
panel.chk_bench_demo.setChecked(False)
panel.ed_bench_range.setText("")
panel._on_bench()
check("③ 空范围被挡（提示请填范围）",
      "请填实例范围" in panel.lbl_bench.text() and panel._bench_worker is None)

panel.ed_bench_range.setText("5,1")
panel._on_bench()
check("③ 乱序范围走 parse_indexes 报错转人话（不抛崩）",
      "实例范围填错" in panel.lbl_bench.text() and panel._bench_worker is None)

panel.ed_bench_range.setText("0-11")
panel._on_bench()
check("③ 未启动实例点名挡掉（假实例只有 1~8；0/9/10/11 没跑，不许出假结论）",
      "没启动" in panel.lbl_bench.text() and "[0, 9, 10, 11]" in panel.lbl_bench.text()
      and panel._bench_worker is None)

# ── ④ demo 模式 + 非阻塞 + 结果表填对 + 建议值（桩 _bench_runner）──
panel._bench_runner = fake_runner
panel.chk_bench_demo.setChecked(True)
panel.ed_bench_range.setText("0-2")   # demo 忽略此栏
_mark = time.time()
panel._on_bench()
_spent = time.time() - _mark
check(f"④ _on_bench 立即返回不阻塞界面（{_spent:.3f}s＜0.5s）", _spent < 0.5)
check("④ 按钮切「压测中…」且取消可用、开始禁用",
      panel.btn_bench.text() == "压测中…" and panel.btn_bench_cancel.isEnabled()
      and not panel.btn_bench.isEnabled())
check("④ 跑时 demo/范围/轮数控件被锁", not panel.chk_bench_demo.isEnabled()
      and not panel.ed_bench_range.isEnabled())

_done = pump(lambda: panel._bench_worker is None)
check("④ 后台线程收工（信号回投）", _done)
check("④ 收工后按钮恢复、保存可用",
      panel.btn_bench.isEnabled() and panel.btn_bench.text() == "开始压测"
      and panel.btn_bench_save.isEnabled() and not panel.btn_bench_cancel.isEnabled())

check("④ 结果表行数 = demo 档位数 3", panel.tbl_bench.rowCount() == 3)
check("④ 表内 N 列 = 4/8/16",
      [panel.tbl_bench.item(r, 0).text() for r in range(3)] == ["4", "8", "16"])
check("④ 达标档都标 ✅（均值/p95 列有值）",
      all(panel.tbl_bench.item(r, 4).text() == "✅ 达标" for r in range(3))
      and panel.tbl_bench.item(0, 1).text() == "0.010")
check("④ 建议最大负载 = 最大档 16（recommend 与表一致）",
      "16 台" in panel.lbl_bench.text())
check("④ demo 给出「不是容量结论」提醒", "不是容量结论" in panel.lbl_bench.text())

# ── ⑤ 实测链路：running 齐 → steps 走 capacity_steps（不真取帧）──
panel.chk_bench_demo.setChecked(False)
panel.ed_bench_range.setText("1-8")   # 假实例 1~8 全 running
panel._on_bench()
check("⑤ 实例齐跑时不再挡（真压测放行）", panel._bench_worker is not None)
pump(lambda: panel._bench_worker is None)
# capacity_steps(1..8) = [4,8] 两档
check("⑤ 实测切档 = 4/8（1-8 的 capacity_steps）", panel.tbl_bench.rowCount() == 2
      and [panel.tbl_bench.item(r, 0).text() for r in range(2)] == ["4", "8"])

# ── ⑥ 取消：长跑中发 cancel → 收工为取消态、按钮放开 ──
def slow_runner(steps, rounds, budget, is_cancelled, on_result):
    while not is_cancelled():
        time.sleep(0.02)
    return {"per_n": {}, "cancelled": True}


panel._bench_runner = slow_runner
panel.chk_bench_demo.setChecked(True)
panel._on_bench()
check("⑥ 长跑已起（worker 在跑）",
      panel._bench_worker is not None and panel.btn_bench_cancel.isEnabled())
panel._on_bench_cancel()
check("⑥ 取消后立刻禁用取消按钮、提示正在取消",
      not panel.btn_bench_cancel.isEnabled() and "取消" in panel.lbl_bench.text())
check("⑥ 后台收到取消标志", pump(lambda: panel._bench_worker is None, timeout=10))

# ── ⑦ 结果表首行数据一致性：mean/p95/超拍率列与回投 stats 对上 ──
panel._bench_runner = fake_runner
panel.chk_bench_demo.setChecked(True)
panel._on_bench()
pump(lambda: panel._bench_worker is None)
check("⑦ 均值/p95/超拍率三列渲染正确（0.010 / 0.020 / 0%）",
      panel.tbl_bench.item(0, 1).text() == "0.010"
      and panel.tbl_bench.item(0, 2).text() == "0.020"
      and panel.tbl_bench.item(0, 3).text() == "0%")

# ── ⑧ 存 json：打桩 QFileDialog 选临时路径 → atomic_dump 键齐 ──
_out = str(Path(_tmpdir) / "bench_result.json")
_old_save = QFileDialog.getSaveFileName
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (_out, ""))
try:
    panel._on_bench_save()
finally:
    QFileDialog.getSaveFileName = _old_save
import json
_saved = json.load(open(_out, encoding="utf-8")) if os.path.exists(_out) else {}
check("⑧ 存 json 键齐（budget/rounds/demo/per_n/recommend）",
      set(_saved) >= {"budget", "rounds", "demo", "per_n", "recommend"}
      and _saved["demo"] is True and set(_saved["per_n"]) == {"4", "8", "16"})
check("⑧ 保存后提示给出落盘路径", _out in panel.lbl_bench.text())

# ── ⑨ 出错档位不崩：error 回投转成人话（后台线程异常兜底）──
panel._bench_pending.append({"error": "实例 3 取帧失败"})
panel._flush_bench_rows(0.5)
check("⑨ 回投 error 档不静默、转成中止提示",
      "压测中止" in panel.lbl_bench.text() and "取帧失败" in panel.lbl_bench.text())

print("-" * 46)
if ok:
    print("smoke bench-ui ALL OK")
    sys.exit(0)
print("SMOKE FAILED")
sys.exit(1)