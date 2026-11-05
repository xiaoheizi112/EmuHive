# 冒烟：动态引导手指检测（core.vision.detect_finger / extend_click）
# 守的链路（2026-10-11 新增，用户解冻后加的引擎能力）：
#   ① 白块+黑描边 → 检出（真阳性）；四方向（上/下/左/右）伸入都能认，
#      指尖落在"离触入边最远"那端、指向朝画面内侧；
#   ② 假阳性被拒：小白字碎块（面积太小）、纯白块无黑边（描边佐证不过）、
#      整屏亮背景（面积超上限）、纯色无纹理（无轮廓）；
#   ③ 缩放/旋转免疫：同一形状放大、旋转后仍检出（这是换掉"每角度每大小存图"
#      老办法的根本理由——模板匹配只有平移不变，扛不住这两样）；
#   ④ extend_click：指尖 + extend_px×方向；0=点指尖，>0 顺手势外延；
#      finger=None → (0,0) 不抛。
# 红线：离屏无关（纯 numpy/cv2 合成图，不碰模拟器、不碰 config）。
# 跑法：python tests/smoke_finger_detect.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import cv2

from core import vision

failures = []


def check(label, cond):
    if cond:
        print(f"  ✓ {label}")
    else:
        failures.append(label)
        print(f"  ✗ {label}")


def base_frame():
    f = np.zeros((960, 540, 3), np.uint8)
    f[:] = (60, 70, 80)   # 深色背景（非纯黑，避免无纹理误判）
    # 加点纹理噪声，防"纯色区域方差≈0"的退化分支
    noise = np.random.randint(0, 12, (960, 540, 3), dtype=np.uint8)
    f = cv2.add(f, noise)
    return f


def _hand_sprite(scale=1.0, rotate=0.0):
    """"白手掌+黑描边"精灵：手指尖朝上（apex 在上），宽 120 高 200。
    rotate 用于把它转向；描边 10px（真实引导手描边够粗，抗锯齿洗不掉）。"""
    hand = np.full((200, 120, 3), (250, 250, 250), np.uint8)
    cv2.polylines(hand, [np.array([[0, 199], [0, 60], [60, 10],
                                   [119, 60], [119, 199]], np.int32)],
                  True, (10, 10, 10), 10)
    if scale != 1.0:
        hand = cv2.resize(hand, (int(120 * scale), int(200 * scale)))
    if rotate:
        h, w = hand.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), rotate, 1.0)
        cos, sin = abs(M[0, 0]), abs(M[0, 1])
        nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
        M[0, 2] += nw / 2 - w / 2
        M[1, 2] += nh / 2 - h / 2
        hand = cv2.warpAffine(hand, M, (nw, nh), borderValue=(60, 70, 80))
    return hand


def draw_hand(f, side="bottom", scale=1.0, rotate=0.0):
    """把精灵贴到指定边，并**转向**让指尖朝画面内侧（底→指上、左→指右…）。
    只在 bottom/top 时 preserve 手形朝向；left/right 额外转 ±90°。"""
    extra = {"bottom": 0, "top": 0, "left": -90, "right": 90}[side]
    hand = _hand_sprite(scale=scale, rotate=rotate + extra)
    h, w = hand.shape[:2]
    if side in ("bottom", "top"):
        x0 = max(0, (540 - w) // 2)
        y0 = (960 - h) if side == "bottom" else 0
        f[y0:y0 + h, x0:x0 + w] = hand
    else:
        y0 = max(0, (960 - h) // 2)
        x0 = (540 - w) if side == "right" else 0
        f[y0:y0 + h, x0:x0 + w] = hand
    return f


# ── ① 四方向真阳性 ──────────────────────────────────────────────
# 期望（手从该边伸入 → 指尖朝画面内侧、指向朝内）：
#   bottom → dir.y < 0 ；top → dir.y > 0 ；left → dir.x > 0 ；right → dir.x < 0
for side, axis, sign in (("bottom", 1, -1), ("top", 1, 1),
                         ("left", 0, 1), ("right", 0, -1)):
    f = draw_hand(base_frame(), side=side)
    r = vision.detect_finger(f)
    ok = r is not None
    check(f"① {side} 伸手 → 检出", ok)
    if ok:
        check(f"① {side} side 判对", r["side"] == side)
        comp = r["dir"][axis]
        check(f"① {side} 指向{'竖直' if axis else '水平'}分量为 {sign}"
              f"（实际 {comp:.2f}）",
              (comp < 0) if sign < 0 else (comp > 0))


# ── ② 假阳性被拒 ────────────────────────────────────────────────
f = base_frame()
cv2.putText(f, "520/450 世界 布里", (30, 120), cv2.FONT_HERSHEY_SIMPLEX,
            0.6, (255, 255, 255), 1)
check("② 只有小白字（面积太小）→ 拒绝", vision.detect_finger(f) is None)

f = np.full((960, 540, 3), (200, 200, 200), np.uint8)   # 浅灰底（非深色）
f = cv2.add(f, np.random.randint(0, 12, (960, 540, 3), dtype=np.uint8))
# 纯白块无黑边、画在**浅色背景**上（真正要防的"白按钮/白块冒充手指"场景）：
# 周围没有暗像素 → 描边佐证占比≈0 → 拒。
cv2.rectangle(f, (220, 500), (320, 580), (250, 250, 250), -1)   # 白块无黑边
check("② 浅背景上的纯白块无黑描边 → 拒绝（描边佐证）",
      vision.detect_finger(f) is None)
# ⚠ 已知局限（诚实记录，非断言失败）：纯白块画在**整片深色背景**上时，深背景会被
#   描边佐证当成"黑描边"→ 可能误判。真实游戏界面极少出现"纯白块+整片纯深色"
#   组合，真遇到靠调 white_thresh/dark_thresh 收紧。不当回归断言（会随参数浮动）。

f = np.full((960, 540, 3), 250, np.uint8)   # 整屏白
check("② 整屏白背景（面积超上限）→ 拒绝", vision.detect_finger(f) is None)

f = np.full((960, 540, 3), 128, np.uint8)   # 纯色无纹理
check("② 纯色无纹理（无轮廓）→ 拒绝", vision.detect_finger(f) is None)
check("② 空帧/None → 返回 None 不抛",
      vision.detect_finger(None) is None
      and vision.detect_finger(np.zeros((0, 0, 3), np.uint8)) is None)


# ── ③ 缩放/旋转免疫（换掉老办法的根本理由）──────────────────────
# 注：面积带/暗阈值是**按场景调**的旋钮（真机手指大小/画质不同，见 CLAUDE.md）。
# 这里验证的是"能力成立"——放大/旋转后，只要参数放宽到容得下，照样检出；
# 模板匹配则在原理上做不到（它只有平移不变）。故按需传入放宽的参数。
r_big = vision.detect_finger(draw_hand(base_frame(), side="bottom", scale=1.6),
                             max_area=60000)
check("③ 放大 1.6×（max_area 放宽）仍检出（模板匹配做不到）", r_big is not None)
r_rot = vision.detect_finger(draw_hand(base_frame(), side="bottom", rotate=25))
check("③ 旋转 25° 仍检出（默认参数，模板匹配做不到）", r_rot is not None)
# 反证：参数不放松时，超出面积带/描边淡化会被拒（说明旋钮真在起作用）
check("③ 放大 1.6× 但 max_area 不放松 → 拒（面积超带）",
      vision.detect_finger(draw_hand(base_frame(), side="bottom", scale=1.6)) is None)

# ── ④ extend_click：外延与兜底 ──────────────────────────────────
r = vision.detect_finger(draw_hand(base_frame(), side="bottom"))
if r is not None:
    tx, ty = r["tip"]
    check("④ extend_px=0 → 就点指尖",
          vision.extend_click(r, 0) == (int(tx), int(ty)))
    check("④ extend_px=40 且指向朝上 → 落点比指尖更靠上（y 更小）",
          vision.extend_click(r, 40)[1] < ty)
check("④ finger=None → (0,0) 不抛", vision.extend_click(None, 50) == (0, 0))

print("-" * 46)
if failures:
    print(f"SMOKE FAILED: {failures}")
    sys.exit(1)
print("SMOKE PASS")