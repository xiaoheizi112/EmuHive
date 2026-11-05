# 冒烟：每拍灰度缓存（2026-09-24 成品审计 P1-⑧）
# 守两条：①缓存路径与逐次灰度化结果完全一致（同帧多模板 全/ROI 匹配等价）；
#         ②缓存按帧对象判新鲜——换帧绝不吃到上一帧的灰度（陈旧=最难查的点歪bug）。
# 跑法：python tests/smoke_gray_cache.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
from core import vision

rng = np.random.default_rng(7)
frame = rng.integers(0, 256, (960, 540, 3), dtype=np.uint8)
tpls = []
for k in range(3):
    tpls.append(frame[100 + k * 90:140 + k * 90, 60 + k * 40:120 + k * 40].copy())
# 第 3 张挪个位置再入另一帧，制造「同模板不同帧」的交叉
frame2 = rng.integers(0, 256, (960, 540, 3), dtype=np.uint8)
frame2[300:340, 200:260] = tpls[2]

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


# ① 等价性：同一帧先后匹配（第 2 遍走缓存）结果与「帧的拷贝」（必走冷路径）一致
for i, t in enumerate(tpls):
    for roi in (None, (40, 80, 300, 500)):
        r_hot = vision.match_template(frame, t, threshold=0.5, roi=roi)
        r_cold = vision.match_template(frame.copy(), t, threshold=0.5, roi=roi)
        check(f"模板{i} roi={roi} 热/冷结果一致",
              (r_hot is None) == (r_cold is None) and
              (r_hot is None or (r_hot["x"] == r_cold["x"] and
                                 r_hot["y"] == r_cold["y"] and
                                 abs(r_hot["score"] - r_cold["score"]) < 1e-6)))

# ② 新鲜度：frame2 里挪了位置的模板2 要在新坐标命中，不能吃 frame 的陈旧灰度
r = vision.match_template(frame2, tpls[2], threshold=0.9)
check("换帧不吃陈旧缓存（命中新位置，返回左上角）",
      r is not None and abs(r["x"] - 200) <= 1 and abs(r["y"] - 300) <= 1)

# ③ 缓存命中确认：同帧二次调用 _gray_cached 返回同一对象
g1 = vision._gray_cached(frame)
g2 = vision._gray_cached(frame)
check("同帧二次调用命中缓存（同一数组对象）", g1 is g2)

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke gray-cache ALL OK")
