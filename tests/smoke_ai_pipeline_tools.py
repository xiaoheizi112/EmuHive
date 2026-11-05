# 冒烟：AI 流水线四件套 tools/（方案 A，2026-10-04）——离屏跑，不碰模拟器、
# 不碰真实模板库/配置（临时库 + 临时 config 双隔离）。
# ① vid_frames：ffmpeg testsrc 现造 2 秒视频 → 抽帧 manifest 有 seq 帧且带时间戳；
# ② frame_cut：1080×1920 帧缩到 540×960 基准裁模板入库（--rect 直裁 + --snap 贴边两路）
#    + 同名红线拒写且不落成功话术/--force 放行；
# ③ flow_apply：空图编译过关写入临时 config / 同名无 force 拒 / 坏 json 拒且不落账；
# ④ flow_run：只验 import 可用（真跑要开雷电，不在离屏职责内）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_ai_pipeline_tools.py
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import cv2
import numpy as np

import core.config_manager as cm
from core import template_manager

_tmp = Path(tempfile.mkdtemp(prefix="aipipe_smoke_"))
cm._config = cm.ConfigManager(str(_tmp / "config.json"))

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


def _use_lib(d: Path):
    """临时模板库四连（smoke_flow_pack 同款）：指路 + 缓存/stat 戳作废。"""
    template_manager.TEMPLATES_DIR = d
    template_manager.INDEX_PATH = d / "index.json"
    template_manager._cache = {"index": None, "mtime": 0.0, "images": {}}
    template_manager._stat_ts = 0.0


# ── ① vid_frames ──
from tools import frame_cut, vid_frames

_have_ff = subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0
if not _have_ff:
    print("SKIP ffmpeg 未装（①②依赖抽帧，直接跳过视频段）")
else:
    v = _tmp / "t.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=2:size=1080x1920:rate=10", str(v)],
                   capture_output=True, check=True)
    man = vid_frames.extract(v, fps=2, scene=0.3, out=_tmp / "fr")
    seqs = [m for m in man if m["tag"] == "seq"]
    check("抽帧：2 秒视频 @2fps 出 ≥3 张 seq 帧且时间戳递增",
          len(seqs) >= 3 and all(s["t"] > 0 for s in seqs)
          and [s["t"] for s in seqs] == sorted(s["t"] for s in seqs))
    check("manifest 落盘可复读", (_tmp / "fr" / "manifest.json").exists()
          and json.loads((_tmp / "fr" / "manifest.json").read_text(encoding="utf-8")) == man)
    one = _tmp / "at.png"
    vid_frames.extract_at(v, 1.0, str(one))
    check("--at 单帧截取", one.exists())

# ── ② frame_cut ──
_use_lib(_tmp / "libs")
frame = np.zeros((1920, 1080, 3), np.uint8)          # 竖屏 1080×1920 假帧
cv2.rectangle(frame, (100, 200), (300, 260), (0, 255, 0), -1)
fp = _tmp / "f.png"
cv2.imwrite(str(fp), np.ascontiguousarray(frame))
# 原帧绿块 (100,200)-(300,260)；缩到 540×960（×0.5）后基准坐标 = 50,100,100,30
e = frame_cut.cut(str(fp), "按钮_绿", "AI测试库", [50, 100, 100, 30])
check("frame_cut：帧缩基准裁块入库（宽高对账 + 账本认得）",
      e and e["width"] == 100 and e["height"] == 30
      and template_manager.has_template("AI测试库", "按钮_绿"))
import contextlib
import io

_buf = io.StringIO()
try:
    with contextlib.redirect_stdout(_buf):
        frame_cut.cut(str(fp), "按钮_绿", "AI测试库", [50, 100, 100, 30])
    rejected = False
except SystemExit:
    rejected = True
check("frame_cut：同名红线拒写（无 --force）", rejected)
check("frame_cut：拒写路径不落成功话术（返回/打印均不称已进库）",
      rejected and "已进库" not in _buf.getvalue())
check("frame_cut：--force 放行覆盖",
      frame_cut.cut(str(fp), "按钮_绿", "AI测试库", [50, 100, 100, 30], force=True) is not None)
try:
    frame_cut.cut(str(fp), "越界", "AI测试库", [500, 900, 100, 100])
    oob = False
except SystemExit:
    oob = True
check("frame_cut：框越出基准界被拒", oob)
# --snap 贴边路（旧 frame_snap 并入）：粗种子罩住绿块 → 算法吸边入库
# 种子 (40,90,160,140) 相对块 (50,100)-(150,130) 四边各富余 10px，贴边应吸回 ~100×30（含 pad2）
e_snap = frame_cut.cut(str(fp), "按钮_绿贴边", "AI测试库", snap_seed=(40, 90, 160, 140))
check("frame_cut --snap：种子区贴边裁块入库（宽高吸回绿块大小）",
      e_snap and 100 <= e_snap["width"] <= 108 and 30 <= e_snap["height"] <= 38
      and template_manager.has_template("AI测试库", "按钮_绿贴边"))
try:
    frame_cut.cut(str(fp), "贴边失败", "AI测试库", snap_seed=(0, 0, 30, 30))  # 纯黑角分不出前景
    snap_fail = False
except SystemExit:
    snap_fail = True
check("frame_cut --snap：种子区内无前景时拒绝入库", snap_fail)

# ── ③ flow_apply ──
from tools import flow_apply

g = _tmp / "flow.json"
g.write_text(json.dumps({"version": 2, "nodes": [], "edges": []}), encoding="utf-8")
n, s = flow_apply.apply_flow(str(g), "冒烟流程")
check("flow_apply：空图编译过关写进临时 config",
      "冒烟流程" in cm._config.get("custom_flows") and n == 0)
try:
    flow_apply.apply_flow(str(g), "冒烟流程")
    dup = False
except SystemExit:
    dup = True
check("flow_apply：同名无 --force 拒写（资产红线）", dup)
bad = _tmp / "bad.json"
bad.write_text('{"nodes": "不是列表"}', encoding="utf-8")
try:
    flow_apply.apply_flow(str(bad), "坏流程")
    refused = False
except SystemExit:
    refused = True
check("flow_apply：坏 json 拒写且不落账",
      refused and "坏流程" not in cm._config.get("custom_flows"))

# 真图不空转：wait→click 两节点引用刚入库的模板，编译过关 + 引用派生认库
# （docs/flow-json-cheatsheet.md 最小示例同款——AI 直写口径的活体回归）
real_g = _tmp / "real.json"
real_g.write_text(json.dumps({"version": 2, "nodes": [
    {"id": "w1", "type": "wait_template", "x": 0, "y": 0, "label": "等绿",
     "category": "AI测试库", "name": "按钮_绿", "timeout": 15,
     "exists": True, "on_timeout": "fail"},
    {"id": "c1", "type": "click_template", "x": 260, "y": 0, "label": "点绿",
     "category": "AI测试库", "name": "按钮_绿", "wait_before": False,
     "wait_timeout": 8, "on_timeout": "fail",
     "jitter_px_min": 0, "jitter_px_max": 5}],
    "edges": [{"from": "w1", "port": "out", "to": "c1", "to_port": "in"}]},
    ensure_ascii=False), encoding="utf-8")
n2, s2 = flow_apply.apply_flow(str(real_g), "冒烟真图")
check("flow_apply：速查表最小示例真图编译过关入库（2 节点）", n2 == 2 and s2 >= 1)
from bot.template_deps import flow_ref_summary
check("引用现场派生：真图认得用了哪个库（铁律 4 不持久化）",
      "AI测试库" in flow_ref_summary().get("冒烟真图", set()))

# ── ④ flow_run（只验 import；真跑要开雷电）──
import tools.flow_run as fr

check("flow_run 可 import 且带 run()", callable(getattr(fr, "run", None)))

print("SMOKE " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
