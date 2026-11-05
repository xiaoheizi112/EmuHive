"""tools/vid_frames.py — 录屏视频 → 抽帧（AI 做流程流水线 ①）

我（AI）看不了视频本身，靠 ffmpeg 把关键画面抽成 PNG 再逐张看图。
两种抽法一次跑全：
  定距抽帧：每 1/fps 秒一张（看整体节奏）
  换场抽帧：画面突变处一张（scene 阈值，抓到"点了按钮之后"的瞬间）
输出统一进 _ai_work/<视频名>/frames/，附 manifest.json（文件→时间戳），
看图时按时间对步骤。

用法：
  python tools/vid_frames.py <视频路径> [--fps 1] [--scene 0.3]
  python tools/vid_frames.py <视频路径> --at 12.5        # 只取某一时刻一帧
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _run_ffmpeg(args):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-y"] + args,
                       capture_output=True, text=True, errors="replace")
    if r.returncode:
        raise RuntimeError(f"ffmpeg 失败：{r.stderr[-400:]}")
    return r.stderr


def extract(video, fps=1.0, scene=0.3, out=None):
    """定距 + 换场两路抽帧，返回 manifest 列表 [{file, t, tag}]。"""
    v = Path(video)
    if not v.exists():
        raise SystemExit(f"视频不存在：{v}")
    out = Path(out) if out else Path("_ai_work") / v.stem / "frames"
    out.mkdir(parents=True, exist_ok=True)
    manifest = []

    _run_ffmpeg(["-i", str(v), "-vf", f"fps={fps}", str(out / "seq_%05d.png")])
    for i, p in enumerate(sorted(out.glob("seq_*.png"))):
        manifest.append({"file": str(p), "t": round((i + 1) / fps, 2), "tag": "seq"})

    # 换场帧：showinfo 往 stderr 打 pts_time，逐行抠出来当时间戳
    err = _run_ffmpeg(["-i", str(v),
                       "-vf", f"select=gt(scene\\,{scene}),showinfo",
                       "-fps_mode", "vfr", str(out / "cut_%05d.png")])
    times = [float(m.group(1)) for m in re.finditer(r"pts_time:(\d+\.?\d*)", err)]
    for i, p in enumerate(sorted(out.glob("cut_*.png"))):
        t = round(times[i], 2) if i < len(times) else -1
        manifest.append({"file": str(p), "t": t, "tag": "scene"})

    mp = out / "manifest.json"
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"共 {len(manifest)} 帧 → {out}（清单 {mp}）")
    return manifest


def extract_at(video, t, out_png):
    """只取第 t 秒一帧（放大看某个瞬间用）。"""
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(["-ss", str(t), "-i", str(video), "-frames:v", "1", str(out_png)])
    print(f"已取 {t}s 帧 → {out_png}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--scene", type=float, default=0.3)
    ap.add_argument("--out", default=None)
    ap.add_argument("--at", type=float, default=None, help="只取该秒数一帧")
    a = ap.parse_args()
    if a.at is not None:
        extract_at(a.video, a.at, a.out or f"_ai_work/{Path(a.video).stem}/at_{a.at}.png")
    else:
        extract(a.video, a.fps, a.scene, a.out)
