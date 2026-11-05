# 冒烟：模板体检 core/vision.py health_report/health_from_library（阶段4 · 2026-10-09）
# 覆盖：判死=逐张按账本各自阈值（非一刀切）/从画面裁出的图必活/自适应阈值的
#       噪声图必死/skipped 两类（PNG 读不出、模板比画面大）/by_category 计数/
#       临时账本经 add_template 落盘后 health_from_library 读回等价/--lib 过滤/
#       CLI --frame --json 端到端（不碰模拟器）。
# 红线：临时 config + 临时模板库重定向，绝不写真 configs/ 与 templates/；不打模拟器。
# 跑法：python tests/smoke_vision_health.py
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np

import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

from core import template_manager as tm
from core import vision

base = Path(tempfile.mkdtemp())
tm.TEMPLATES_DIR = base / "templates"
tm.INDEX_PATH = tm.TEMPLATES_DIR / "index.json"

fails = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        fails.append(name)


rng = np.random.default_rng(7)
frame = rng.integers(0, 256, (960, 540, 3), dtype=np.uint8)  # 确定性"噪声画面"
good = frame[400:440, 200:240].copy()                        # 从画面裁出 → 必命中
noise = rng.integers(0, 256, (40, 40, 3), dtype=np.uint8)
s_noise = float(vision.match_template(frame, noise, 0.99, debug=True)["score"])

# ── ① 纯函数 health_report：判定 + 两类跳过 + 分类计数（手工条目+自带 loader） ──
items = [
    {"category": "A", "name": "好图", "threshold": 0.9, "_img": good},
    {"category": "A", "name": "坏图", "threshold": min(s_noise + 0.05, 0.999), "_img": noise},
    {"category": "B", "name": "幽灵", "threshold": 0.9, "_img": None},
    {"category": "B", "name": "巨图", "threshold": 0.9,
     "_img": np.zeros((600, 600, 3), np.uint8)},
]
rep = vision.health_report(frame, items, lambda t: t["_img"])
check("checked=4", rep["checked"] == 4)
check("裁自画面的必活", all(d["name"] != "好图" for d in rep["dead"]))
check(f"噪声图在自适应阈值 {rep['dead'][0]['threshold'] if rep['dead'] else '?':.2f} 下判死",
      len(rep["dead"]) == 1 and rep["dead"][0]["name"] == "坏图"
      and abs(rep["dead"][0]["score"] - s_noise) < 0.01)
skips = {s["name"]: s["why"] for s in rep["skipped"]}
check("PNG 读不出 → skipped 不判死", skips.get("幽灵") == "账本在但 PNG 读不出")
check("模板比画面大 → skipped", "巨图" in skips)
check("by_category 计数（A:1死 B:2跳过0死）",
      rep["by_category"]["A"] == {"total": 2, "dead": 1}
      and rep["by_category"]["B"] == {"total": 2, "dead": 0})

# ── ② 账本链路：add_template 落盘 → health_from_library 读回扫描 ──
tm.add_template("好图", "库一", good, 0.9)
tm.add_template("坏图", "库一", noise, min(s_noise + 0.05, 0.999))
tm.add_template("陪跑", "库二", frame[10:60, 10:60].copy(), 0.9)
rep2 = vision.health_from_library(frame)
check("全库扫 3 张（账本驱动）", rep2["checked"] == 3)
check("死图只有坏图", [d["name"] for d in rep2["dead"]] == ["坏图"])
rep3 = vision.health_from_library(frame, lib="库一")
check("--lib 过滤：只扫库一", rep3["checked"] == 2
      and set(rep3["by_category"]) == {"库一"})

# ── ③ CLI 端到端：--frame 离线 + --json 报告（--lib 不存在库 = 零扫描不碰真账本图） ──
frame_path = str(base / "frame.png")
import cv2
cv2.imwrite(frame_path, frame)
out_json = str(base / "report.json")
r = subprocess.run([sys.executable, "-m", "core.vision", "health",
                    "--frame", frame_path, "--lib", "__不存在库__", "--json", out_json],
                   cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   capture_output=True, text=True, encoding="utf-8", timeout=120)
check("CLI exit 0", r.returncode == 0)
data = json.load(open(out_json, encoding="utf-8")) if os.path.exists(out_json) else {}
check("CLI 报告 checked=0（不存在库零扫描）", data.get("checked") == 0)
check("报告含 frame 来源（--frame 路径）", data.get("frame") == frame_path)

print("-" * 46)
if fails:
    print("SMOKE FAILED:", fails)
    sys.exit(1)
print("smoke vision-health ALL OK")
