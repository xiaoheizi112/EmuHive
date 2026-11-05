"""tools/frame_cut.py — 从抽好的帧上裁模板入库（AI 做流程流水线 ②）

帧可能是任意录屏分辨率，而模板基准必须 540×960（不变量 2：分辨率三方一致，
否则识别对但点空）。所以先把整帧缩放到基准，再按基准坐标框裁——
框坐标一律是「缩放后的 540×960 空间」，和找图/点击同一坐标系。

两种给框方式（二选一）：
  --rect x,y,w,h        目测框，直接裁
  --snap x0,y0,x1,y1    粗种子区 → core.vision.snap_bbox 贴边（算法把图标真边吸准），
                        裁完拿裁图回原帧匹配自检（分 <0.95 打 WARN）——旧 frame_snap 已并入此路

红线：同名同库模板默认拒写（不落账、不喊成功），--force 才覆盖；只新增，绝不碰别的条目。

用法：
  python tools/frame_cut.py <帧.png> <模板名> --lib <库目录> (--rect x,y,w,h | --snap x0,y0,x1,y1)
                            [--threshold 0.85] [--base 540x960] [--force]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2

from core import capture, template_manager, vision


def cut(frame_png, name, lib, rect=None, threshold=0.85, base=(540, 960), force=False,
        snap_seed=None):
    """帧 → 缩放基准 → 贴边(可选) → 裁框 → add_template。返回账本条目 dict。

    同名且未 force：当场 SystemExit 拒绝，不入库、不打印成功话术。"""
    img = capture.load_image_cn(str(frame_png))
    if img is None:
        raise SystemExit(f"帧读不出来：{frame_png}")
    if base and (img.shape[1], img.shape[0]) != tuple(base):
        img = cv2.resize(img, tuple(base), interpolation=cv2.INTER_AREA)
    if snap_seed is not None:
        bbox = vision.snap_bbox(img, tuple(snap_seed))
        if bbox is None:
            raise SystemExit(f"贴边失败：种子区 {snap_seed} 内分不出前景（换帧/放大种子区再来）")
        rect = list(bbox)
    if rect is None:
        raise SystemExit("rect 与 snap_seed 至少给一个（命令行是 --rect / --snap 二选一）")
    x, y, w, h = rect
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > img.shape[1] or y + h > img.shape[0]:
        raise SystemExit(f"框越界（图 {img.shape[1]}x{img.shape[0]}）：rect={rect}")
    piece = img[y:y + h, x:x + w]
    if not force and template_manager.has_template(lib, name):
        raise SystemExit(f"红线：库「{lib}」已有模板「{name}」，要覆盖请加 --force（需用户确认）")
    entry = template_manager.add_template(name, lib, piece, threshold)
    if entry is None:
        raise SystemExit("入库失败：库名不能为空")
    print(f"✓ 模板「{name}」已进库「{lib}」（{w}x{h}，阈值 {threshold}）")
    if snap_seed is not None:
        # 自检：拿裁好的模板回原帧匹配，应满分且位置吻合
        r = vision.match_template(img, piece) or {}
        if r.get("matched") and r.get("score", 0) >= 0.95:
            print(f"  贴边自检分={r['score']:.3f} @({r['x']},{r['y']})")
        else:
            print(f"⚠ WARN 贴边自检分={r.get('score', 0):.3f}："
                  "种子区可能没罩全目标，或背景与图标颜色太接近")
    return entry


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("frame")
    ap.add_argument("name")
    ap.add_argument("--lib", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--rect", help="x,y,w,h（540×960 基准坐标，目测框直裁）")
    g.add_argument("--snap", help="x0,y0,x1,y1（基准坐标粗种子区，贴边裁+回帧自检）")
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument("--base", default="540x960")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    bw, bh = (int(v) for v in a.base.lower().split("x"))
    cut(a.frame, a.name, a.lib,
        [int(v) for v in a.rect.split(",")] if a.rect else None,
        a.threshold, (bw, bh), a.force,
        tuple(int(v) for v in a.snap.split(",")) if a.snap else None)
