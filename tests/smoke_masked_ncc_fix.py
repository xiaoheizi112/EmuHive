# 冒烟：抠图匹配分数越界修复 + RGB 颜色校验接线（2026-10-11 新增）
# 根因：_masked_ncc 用「den > 1e-6」判平坦窗口，但 denom 量纲是 √(var_t·var_i)，
#   var 是「掩码内方差 × 像素数」。纯色/低纹理窗口下 float32 抵消残差让 var_i
#   掉到 1e-13 级却仍 >0，而 var_t 很大时 den 照样越过绝对门槛 → 归一化相关
#   算出 >1 的荒唐分（实测真机帧 1.93、合成帧 49.07），脚本据此误点。
#   修法：平坦判据按「每像素方差 < flat_std²」比（var_i < n·std²），口径与
#   普通分支「窗口方差 < 1.0」一致。
# 覆盖：
#   ① 修复后：确定性构造（seed 110 的抠图模板+纯色块底帧）经 match_template
#      全程最高分必 <= 1.0（旧逻辑同构造复算出 6.45，>1）；且分数落到噪声级。
#   ② 修复前后正常命中不受损：真图裁块自匹配仍得 ~1.0。
#   ③ rgb_threshold 账本往返：add_template 存 / set_template_props 五字段改写。
#   ④ rgb_threshold 生效：同形状异色块被颜色校验拒（灰度高、颜色低）。
# 红线：临时 config + 临时账本重定向，绝不写真 configs/ 与 templates/。
# 跑法：python tests/smoke_masked_ncc_fix.py
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import cv2
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


# ── ① 越界修复：确定性构造（seed 110）经 match_template 全程 <= 1.0 ──
rng = np.random.default_rng(110)
th, tw = int(rng.integers(20, 60)), int(rng.integers(20, 60))
keep = rng.integers(0, 256, (th, tw)).astype(np.uint8)
maskf = np.ones((th, tw), np.float32)
maskf[:, :max(1, tw // 2)] = 0.0                 # 左半抠掉（走 masked 分流）
H = int(rng.integers(150, 500)); W = int(rng.integers(150, 500))
gray_img = np.zeros((H, W), np.float32)
for _ in range(int(rng.integers(1, 5))):          # 若干纯色亮块（低纹理背景）
    y = int(rng.integers(0, H)); x = int(rng.integers(0, W))
    bh = int(rng.integers(5, 60)); bw = int(rng.integers(5, 60))
    gray_img[y:y + bh, x:x + bw] = float(rng.integers(150, 256))
alpha = (maskf * 255).astype(np.uint8)
tpl4 = np.dstack([np.dstack([keep, keep, keep]).astype(np.uint8), alpha])
frame = cv2.cvtColor(gray_img.astype(np.uint8), cv2.COLOR_GRAY2BGR)

hit = vision.match_template(frame, tpl4, 0.8, debug=True)
check("抠图匹配不再算出 >1 的荒唐分（修复后最高分 <= 1.0）",
      hit is not None and float(hit["score"]) <= 1.0)
check("该构造的分数落到噪声级（< 0.5，绝非假高分）",
      hit is not None and float(hit["score"]) < 0.5)

# 同构造下旧判据会越界：复算旧逻辑，确认它确曾 >1（守的正是这一条）
gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
tplg = cv2.cvtColor(tpl4[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32)
m = (tpl4[:, :, 3] > 127).astype(np.float32)
n = float(m.sum()); m_t = m * tplg; smt = float(m_t.sum())
var_t = float((m * tplg * tplg).sum()) - smt * smt / n
c_it = cv2.matchTemplate(gray, m_t, cv2.TM_CCORR)
c_i = cv2.matchTemplate(gray, m, cv2.TM_CCORR)
c_ii = cv2.matchTemplate(gray * gray, m, cv2.TM_CCORR)
num = c_it - c_i * (smt / n); var_i = np.maximum(c_ii - c_i * c_i / n, 0.0)
den = np.sqrt(var_t * var_i)
with np.errstate(all="ignore"):
    old_max = float(np.where(den > 1e-6, num / den, -1.0).max())
check(f"旧判据同构造确曾越界（>{old_max:.2f} > 1，证明守的是真坑）", old_max > 1.0)

# ── ② 正常命中不受损：真图裁块自匹配仍 ~1.0 ──
scene = np.random.default_rng(7).integers(0, 256, (300, 300, 3), dtype=np.uint8)
patch = scene[100:140, 120:180].copy()
selfhit = vision.match_template(scene, patch, 0.9, debug=True)
check("普通模板自匹配不受影响（分 >= 0.99）",
      selfhit is not None and float(selfhit["score"]) >= 0.99)

# ── ③ rgb_threshold 账本往返 ──
img = np.zeros((10, 20, 3), np.uint8)
ent = tm.add_template("rgbA", "rgblib", img, 0.9, rgb_threshold=0.8)
check("add_template 收 rgb_threshold",
      ent is not None and ent.get("rgb_threshold") == 0.8)
check("账本读回 rgb_threshold == 0.8",
      (tm.find_template("rgblib", "rgbA") or {}).get("rgb_threshold") == 0.8)
check("set_template_props 五字段齐写",
      tm.set_template_props("rgbA", "rgblib", 0.7, roi=[1, 2, 3, 4],
                            click_target=[9, 9], click_offset=[5, 5],
                            rgb_threshold=0.75)
      and (lambda e: e["threshold"] == 0.7 and e["roi"] == [1, 2, 3, 4]
           and e["click_target"] == [9, 9] and e["click_offset"] == [5, 5]
           and e["rgb_threshold"] == 0.75)(tm.find_template("rgblib", "rgbA")))
tm.set_template_props("rgbA", "rgblib", 0.9, roi=None, click_target=None,
                      click_offset=None, rgb_threshold=None)
check("rgb_threshold 传 None = 清除",
      tm.find_template("rgblib", "rgbA").get("rgb_threshold") is None)

# ── ④ rgb_threshold 生效：同灰度分布异色块被颜色校验拒 ──
# 构造：同一 pattern 分别进模板的 R 通道、画面的 B 通道——灰度与模板成正比
# （灰度分布几乎一致，灰度分会很高），但颜色不符（红 vs 蓝）。开颜色校验后
# 命中必被作废。
rg = np.random.default_rng(5)
pat = rg.integers(0, 256, (40, 40)).astype(np.float32)
tpl_red = np.zeros((40, 40, 3), np.uint8); tpl_red[:, :, 2] = pat   # BGR：红
blk_blue = np.zeros((40, 40, 3), np.float32); blk_blue[:, :, 0] = pat
bg_img = rg.integers(0, 256, (300, 300, 3)).astype(np.float32)
canvas2 = bg_img.copy()
canvas2[130:170, 130:170] = blk_blue
canvas2 = canvas2.astype(np.uint8)

no_rgb = vision.match_template(canvas2, tpl_red, 0.85, debug=True)
with_rgb = vision.match_template(canvas2, tpl_red, 0.85, rgb_threshold=0.6)
check("同灰度异色块：不开颜色校验时灰度分够高（会误命中）",
      no_rgb is not None and no_rgb.get("matched")
      and float(no_rgb["score"]) >= 0.95)
check("同灰度异色块：开了颜色校验 → 命中被作废（None）", with_rgb is None)

print("-" * 46)
if fails:
    print("SMOKE FAILED:", fails)
    sys.exit(1)
print("smoke masked-ncc-fix ALL OK")