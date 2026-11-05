"""tools/vid_align.py — 窗口录制视频 → 纯安卓画面视频（AI 流水线第 0 步）

OBS 等只能整窗录雷电（带标题栏/边框），且录制缩放会引入宽高比畸变——
模板从这种帧上裁下来，到真机就匹配不上。本工具把安卓内容区从窗口帧里
裁出并拉回 540×960 基准，之后 vid_frames/frame_cut 的坐标全部 = 基准坐标。

用法：
  python tools/vid_align.py <窗口录屏.mp4> --probe          # 抽首帧画网格，人读内容区盒子
  python tools/vid_align.py <窗口录屏.mp4> --box x,y,w,h     # 按视频像素给盒子 → 产出 <名>_aligned.mp4
盒子不落任何配置（本工具不读写 config）：同一台雷电同一窗口大小，照抄上次 --box 值即可复用。
"""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = (540, 960)


def _ff(args):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-y"] + args,
                       capture_output=True, text=True, errors="replace")
    if r.returncode:
        raise SystemExit(f"ffmpeg 失败：{r.stderr[-400:]}")


def probe(video, out=None):
    """抽首帧画 20px 网格（标视频像素坐标），供人/AI 读内容区盒子。"""
    import cv2
    from core import capture
    v = Path(video)
    out = Path(out) if out else Path("_ai_work") / f"{v.stem}_probe.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    _ff(["-i", str(v), "-frames:v", "1", str(out)])
    img = capture.load_image_cn(str(out))   # 中文路径 imread 读不了，走项目自带
    h, w = img.shape[:2]
    for x in range(0, w, 20):
        cv2.line(img, (x, 0), (x, h), (0, 255, 0), 1)
        if x % 100 == 0:
            cv2.putText(img, str(x), (x + 2, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    for y in range(0, h, 20):
        cv2.line(img, (0, y), (w, y), (0, 255, 0), 1)
        if y % 100 == 0:
            cv2.putText(img, str(y), (2, y + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    capture.imwrite_cn(str(out), img)
    print(f"首帧网格 → {out}（视频尺寸 {w}x{h}，按 20px 一格读安卓内容区四边）")


def align(video, box, out=None):
    """box=(x,y,w,h) 视频像素；裁出内容区缩到 540×960 基准。"""
    v = Path(video)
    out = Path(out) if out else v.parent / f"{v.stem}_aligned.mp4"
    x, y, w, h = box
    _ff(["-i", str(v), "-vf",
         f"crop={w}:{h}:{x}:{y},scale={BASE[0]}:{BASE[1]}:flags=lanczos",
         "-c:a", "copy", str(out)])
    print(f"对齐视频 → {out}（盒子 {box} → {BASE[0]}×{BASE[1]}，帧坐标=基准坐标）")
    return str(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--box", help="x,y,w,h 视频像素")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.probe:
        probe(a.video, a.out)
    elif a.box:
        align(a.video, tuple(map(int, a.box.split(","))), a.out)
    else:
        raise SystemExit("给 --probe 或 --box x,y,w,h")
