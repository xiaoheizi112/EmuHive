# 冒烟：指引图标偏移存进模板（账本 click_offset）——回归 2026-10-05 新增
# 覆盖两条链：
# ① 账本：add_template 存 / set_template_props 四字段改写（单字段 setter 已删）
# ② 引擎回落优先级：行/节点非0偏移 > 图存 click_offset > 模板中心；
#    click_target（绝对坐标）压过一切。tap_all_collect 同链。
# 不碰真实模板库（红线：临时 TEMPLATES_DIR/INDEX_PATH 重定向）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_click_offset.py
import os
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

base = Path(tempfile.mkdtemp())
tm.TEMPLATES_DIR = base / "templates"
tm.INDEX_PATH = tm.TEMPLATES_DIR / "index.json"

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


# ── ① 账本字段 ──
img = np.zeros((10, 20, 3), np.uint8)   # 高10宽20：中心补偿 w2=10, h2=5
ent = tm.add_template("imgA", "offlib", img, 0.9, click_offset=[7, -3])
check("add_template 收 click_offset", ent is not None)
check("账本读回 click_offset == [7,-3]",
      (tm.find_template("offlib", "imgA") or {}).get("click_offset") == [7, -3])
check("set_template_props 改写 click_offset（其余字段带现值）",
      tm.set_template_props("imgA", "offlib", 0.9, roi=None, click_target=None,
                            click_offset=[1, 2])
      and tm.find_template("offlib", "imgA")["click_offset"] == [1, 2])
tm.set_template_props("imgA", "offlib", 0.9, roi=None, click_target=None,
                      click_offset=None)
co = tm.find_template("offlib", "imgA").get("click_offset")
check("set None = 清除（引擎 co 假值不回落）", not co)
check("set_template_props 四字段齐写",
      tm.set_template_props("imgA", "offlib", 0.7, roi=[1, 2, 3, 4],
                            click_target=[9, 9], click_offset=[5, 5])
      and (lambda e: e["threshold"] == 0.7 and e["roi"] == [1, 2, 3, 4]
           and e["click_target"] == [9, 9] and e["click_offset"] == [5, 5])(
          tm.find_template("offlib", "imgA")))
tm.set_template_props("imgA", "offlib", 0.9, roi=None, click_target=None,
                      click_offset=[7, -3])   # 复原给引擎测

# ── ② 引擎回落链（假命中框 @左上(100,200)，模板中心恒=(110,205)）──
import bot.fsm as fsm


class FakeCtx:
    ld_index = 0
    log = None
    frame = img   # tap_all_collect 有「无帧返回 []」闸：给张dummy（匹配已打桩不真用）


taps = []
_orig_hit, _orig_shell = fsm._tpl_hit, fsm.ldplayer.adb_shell
fsm._tpl_hit = lambda ctx, cat, nm, roi=None: {
    "x": 100, "y": 200, "score": 0.95, "matched": True, "ms": 5}
fsm.ldplayer.adb_shell = lambda idx, cmd: taps.append(cmd)
try:
    check("无偏移调用：click_offset 自动跟随图 (117,202)",
          fsm.tap_template(FakeCtx(), "offlib", "imgA")
          and taps[-1] == "input tap 117 202")
    check("行采 dx=2 压过图偏移 (112,205)",
          fsm.tap_template(FakeCtx(), "offlib", "imgA", dx=2, dy=0)
          and taps[-1] == "input tap 112 205")
    check("dy=5（dx 为 0 也算采了偏移）压过图 (110,210)",
          fsm.tap_template(FakeCtx(), "offlib", "imgA", dx=0, dy=5)
          and taps[-1] == "input tap 110 210")
    tm.add_template("imgC", "offlib", img, 0.9)   # 没存偏移的图
    check("图也没存 → 点模板中心 (110,205)",
          fsm.tap_template(FakeCtx(), "offlib", "imgC")
          and taps[-1] == "input tap 110 205")
    tm.set_template_props("imgA", "offlib", 0.9, roi=None,
                          click_target=[33, 44], click_offset=[7, -3])
    check("click_target 绝对点压过一切 (33,44)",
          fsm.tap_template(FakeCtx(), "offlib", "imgA")
          and taps[-1] == "input tap 33 44")
    # tap_all_collect 同链：match_template_all 打一张假命中 @左上(0,0)
    _orig_all = fsm.vision.match_template_all
    fsm.vision.match_template_all = \
        lambda *a, **k: [{"x": 0, "y": 0, "score": 0.95, "matched": True}]
    try:
        pts = fsm.tap_all_collect(FakeCtx(), "offlib", "imgA", roi=(0, 0, 540, 960))
    finally:
        fsm.vision.match_template_all = _orig_all
    check("点全部同链回落：中心(10,5)+图偏移(7,-3)", pts == [(17, 2)])
finally:
    fsm._tpl_hit, fsm.ldplayer.adb_shell = _orig_hit, _orig_shell

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke click-offset ALL OK")
