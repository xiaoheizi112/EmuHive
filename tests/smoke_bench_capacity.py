# 冒烟：容量压测 tools/bench_capacity.py（阶段1 · 2026-10-09）
# 覆盖：parse_indexes 区间语法与升序防呆 / tick_stats 已知序列 mean·p95·over /
#       recommend 判据取最大合格档与全灭回 None / bench_n demo 端到端结构 /
#       make_live_round 打桩（假帧源+假模板）耗时正常、取帧 None 明确炸 /
#       CLI --demo --json 子进程 exit 0 且报告键齐。
# 红线：不打真模拟器不读真账本（live 全打桩）；子进程 --demo 无真副作用；临时目录 json。
# 跑法：python tests/smoke_bench_capacity.py
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.join(ROOT, "tools"))
import bench_capacity as bc

fails = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        fails.append(name)


# ── ① parse_indexes ──
check("'0,2,5-9' → [0,2,5,6,7,8,9]", bc.parse_indexes("0,2,5-9") == [0, 2, 5, 6, 7, 8, 9])
check("空格容忍且升序放行", bc.parse_indexes(" 1-2 , 3 ") == [1, 2, 3])
try:
    bc.parse_indexes("5,1")
    bad_order = False
except SystemExit:
    bad_order = True
check("乱序/重叠输入被拒（防把轮次浪费在错档位表上）", bad_order)

# ── ② tick_stats：已知序列（20 样本最近秩：p95=第 19 小=0.95，与 max 分离才算测到 p95） ──
d = [0.05 * k for k in range(1, 21)]
st = bc.tick_stats(d, budget=0.5)
check("mean=0.525", abs(st["mean"] - 0.525) < 1e-9)
check("p95=0.95（ceil(0.95×20)=19 号位）", abs(st["p95"] - 0.95) < 1e-9)
check("max=1.0", abs(st["max"] - 1.0) < 1e-9)
check("over_ratio=0.5（>0.5 者 10 个）", abs(st["over_ratio"] - 0.5) < 1e-9)
check("空序列不炸且 n=0", bc.tick_stats([], 0.5)["n"] == 0)

# ── ③ recommend：判据 超拍≤5% 且 p95≤1.5×budget ──
per_n = {
    4: {"stats": {"over_ratio": 0.0, "p95": 0.4}},
    8: {"stats": {"over_ratio": 0.02, "p95": 0.70}},
    16: {"stats": {"over_ratio": 0.3, "p95": 0.9}},
}
check("取最大合格档（8 过 16 灭 → 8）", bc.recommend(per_n, budget=0.5) == 8)
check("边界：p95=0.74≤0.75 算过", bc.recommend(
    {8: {"stats": {"over_ratio": 0.0, "p95": 0.74}}}, 0.5) == 8)
check("全灭回 None", bc.recommend({16: per_n[16]}, 0.5) is None)

# ── ④ bench_n demo 端到端 ──
grp = ["demo-0", "demo-1", "demo-2"]
res = bc.bench_n(grp, lambda k: bc.make_demo_round()(int(k.split("-")[1])), rounds=4, budget=0.5)
check("demo bench 结构齐（N/stats）", res["N"] == 3 and set(res["stats"]) >=
      {"n", "mean", "p95", "max", "over_ratio", "wall_mean"})
check("样本数 = 实例×轮次", res["stats"]["n"] == 12)

# ── ⑤ make_live_round 打桩：假帧源+假模板 = 与挂机同链路（取帧→灰度→找图） ──
frame = np.random.default_rng(3).integers(0, 256, (240, 180, 3), dtype=np.uint8)
tpl = frame[20:44, 30:60].copy()
ok_runner = bc.make_live_round(1, frame_getter=lambda i: (frame, {}),
                               tpl_loader=lambda: [tpl])
dur = ok_runner(0)
check("live 桩跑通且耗时>0", isinstance(dur, float) and dur > 0)
dead_runner = bc.make_live_round(1, frame_getter=lambda i: None,
                                 tpl_loader=lambda: [tpl])
try:
    dead_runner(7)
    raised = False
except RuntimeError as e:
    raised = "7" in str(e)
check("取帧失败上抛点名实例号（不静默吞）", raised)

# ── ⑥ CLI --demo 子进程端到端 ──
out = os.path.join(tempfile.mkdtemp(), "bench.json")
r = subprocess.run([sys.executable, os.path.join("tools", "bench_capacity.py"),
                    "--demo", "--rounds", "3", "--budget", "0.5", "--json", out],
                   cwd=ROOT, capture_output=True, text=True,
                   encoding="utf-8", timeout=120)
check("CLI exit 0", r.returncode == 0)
data = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else {}
check("报告键齐（budget/rounds/demo/per_n/recommend）",
      set(data) >= {"budget", "rounds", "demo", "per_n", "recommend"} and data["demo"] is True)

print("-" * 46)
if fails:
    print("SMOKE FAILED:", fails)
    sys.exit(1)
print("smoke bench-capacity ALL OK")
