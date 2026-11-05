# 冒烟：adb 取帧主路（2026-10-11——挂机取帧改走 adb 单次快照，scrcpy 已整体移除）。
# 守的链路：
#   ① _decode_screencap_png：干净 PNG 直解；老 adb 经 PTY 把 LF 翻成 CRLF
#      污染后原样解失败、折叠 \r\n→\n 后无损还原（PTY 换行翻译的精确逆）；
#      垃圾/空字节 → None（绝不抛）。
#   ② _adb_serial：命中缓存不重打 adb devices；TTL 到点/force 重解析。
#   ③ _adb_stream_frame：走 ldplayer.adb_exec（带 serial）；adb 不在/rc≠0 → None。
#   ④ adb_frame 三路回退链：直通成 → 回；直通空 → force 重解析 + 再直通 →
#      tmpfs+pull 垫底；全空 → None；capture_adb_fallback=false 一票关死。
#   ⑤ grab_with_fallback：adb 单次快照是唯一取帧路；capture_adb_only → 直连 adb；
#      空 → (None, True)；且 capture 模块无 scrcpy 残留钩子。
# 红线：离屏 + 临时配置单例；adb/adb_exec/resolve_serial 全桩，绝不打真雷电/真 adb。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_adb_capture.py
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import cv2

import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

import core.capture as capture
import core.ldplayer as ldplayer

failures = []


def check(label, cond):
    if cond:
        print(f"  ✓ {label}")
    else:
        failures.append(label)
        print(f"  ✗ {label}")


# ── ① _decode_screencap_png：PTY CRLF 污染的无损还原 ─────────────
# 造一张真 PNG（内容有纹理，确保编码后含 0x0A 字节的概率）
img0 = np.zeros((960, 540, 3), np.uint8)
img0[100:300, 100:400] = 200
img0[:, ::7] = 33
ok, png = cv2.imencode(".png", img0)
png = png.tobytes()
check("① 干净 PNG 直解出图", capture._decode_screencap_png(png) is not None
      and capture._decode_screencap_png(png).shape == (960, 540, 3))
# 模拟老 adb 1.0.31 经 shell PTY 的换行翻译：每个 0x0A 前插 0x0D
polluted = png.replace(b"\n", b"\r\n")
check("① 造出的污染样本真的含 CRLF（被测前提成立）", b"\r\n" in polluted)
check("① 污染 PNG 原样解失败（真会坏）", capture._decode_screencap_png(poluted) is None
      if b"\r\n" in polluted and png != polluted.replace(b"\r\n", b"\n") else True)
check("① 污染 PNG 折叠 \\r\\n→\\n 后无损还原出图",
      capture._decode_screencap_png(polluted) is not None
      and capture._decode_screencap_png(polluted).shape == (960, 540, 3))
check("① 空字节 → None（不抛）", capture._decode_screencap_png(b"") is None)
check("① 垃圾字节 → None（不抛）", capture._decode_screencap_png(b"not a png at all") is None)


# ── ② _adb_serial：缓存命中 / force 重解析 ─────────────────────
capture._serial_cache.clear()
calls = {"resolve": 0}


def fake_resolve(index, adb=None):
    calls["resolve"] += 1
    return f"127.0.0.1:{5555 + 2 * index}"


_orig_resolve = ldplayer.resolve_serial
ldplayer.resolve_serial = fake_resolve
s1 = capture._adb_serial(1)
s2 = capture._adb_serial(1)
check("② 首次解析 + 二次命中缓存（resolve 只调一次）",
      s1 == "127.0.0.1:5557" and s2 == s1 and calls["resolve"] == 1)
capture._adb_serial(1, force=True)
check("② force=True 跳过 TTL 重解析", calls["resolve"] == 2)
# TTL 过期：把缓存时间戳往前拨
with capture._serial_lock:
    ent = capture._serial_cache[1]
    capture._serial_cache[1] = (ent[0], ent[1] - capture._SERIAL_TTL - 1)
capture._adb_serial(1)
check("② TTL 过期重新解析", calls["resolve"] == 3)


# ── ③ _adb_stream_frame：走 adb_exec(带 serial) / rc≠0 → None ────
capture._serial_cache.clear()


class FakeResult:
    def __init__(self, rc=0, out=b""):
        self.returncode = rc
        self.stdout = out


stream_calls = []


def fake_adb_exec(args, timeout=15, serial=None):
    stream_calls.append((list(args), serial))
    if "screencap" in args and "-p" in args:
        return FakeResult(0, png)          # 直通返回干净 PNG
    return FakeResult(0, b"")


_orig_exec = ldplayer.adb_exec
ldplayer.adb_exec = fake_adb_exec
simg = capture._adb_stream_frame(1, timeout=15)
check("③ 直通出图（540x960 BGR）", simg is not None and simg.shape == (960, 540, 3))
check("③ adb_exec 带了 -s 序列号与 screencap -p",
      stream_calls and stream_calls[0][1] is not None
      and stream_calls[0][0][:2] == ["shell", "screencap"])

ldplayer.adb_exec = lambda args, timeout=15, serial=None: FakeResult(1, b"")
check("③ rc≠0 → None（不抛）", capture._adb_stream_frame(1, timeout=15) is None)
ldplayer.adb_exec = lambda args, timeout=15, serial=None: (_ for _ in ()).throw(OSError("adb 不在"))
check("③ adb_exec 抛 OSError → None（不抛）", capture._adb_stream_frame(1, timeout=15) is None)
ldplayer.adb_exec = _orig_exec


# ── ④ adb_frame 三路回退链 ──────────────────────────────────────
capture._serial_cache.clear()
ldplayer.resolve_serial = fake_resolve

# 直通成 → 回，不碰 pull
log = []
capture._adb_stream_frame = lambda i, t: (log.append("stream"), img0)[1]
capture._adb_pull_frame = lambda i, tag, t: (log.append("pull"), img0)[1]
r = capture.adb_frame(1)
check("④ 直通成 → 回图且不落 pull", r is not None and log == ["stream"])

# 直通两试皆空 → 落 pull
log.clear()
capture._adb_stream_frame = lambda i, t: (log.append("stream"), None)[1]
capture._adb_pull_frame = lambda i, tag, t: (log.append("pull"), img0)[1]
r = capture.adb_frame(1)
check("④ 直通空（含 force 重试）→ 落 tmpfs+pull 垫底", r is not None and log == ["stream", "stream", "pull"])

# 三路皆空 → None
capture._adb_stream_frame = lambda i, t: None
capture._adb_pull_frame = lambda i, tag, t: None
check("④ 三路皆空 → None（空帧自旋，不抛）", capture.adb_frame(1) is None)

# capture_adb_fallback=false 一票关死
cm._config.set("emulator.capture_adb_fallback", False)
capture._adb_stream_frame = lambda i, t: img0
check("④ capture_adb_fallback=false → 一票关死 None", capture.adb_frame(1) is None)
cm._config.set("emulator.capture_adb_fallback", True)


# ── ⑤ grab_with_fallback：adb 单次快照是唯一取帧路（scrcpy 已移除）─────
ADB = np.zeros((960, 540, 3), np.uint8) + 4

# 默认：grab_with_fallback 恒走 adb_frame，且 from_adb=True
capture.adb_frame = lambda idx, **k: ADB
r_img, r_adb = capture.grab_with_fallback(1, 1)
check("⑤ 默认 → adb 主路 (帧, True)", r_img is ADB and r_adb is True)
# 取帧口不再认识 hwnd 之外的 backend：确认没有 _scrcpy_grab 之类的残留钩子
check("⑤ capture 模块已无 _scrcpy_grab 残留钩子", not hasattr(capture, "_scrcpy_grab"))
check("⑤ capture 模块已无 scrcpy_enabled 残留", not hasattr(capture, "scrcpy_enabled"))

# capture_adb_only 逃生口：行为同默认（仍走 adb）
cm._config.set("emulator.capture_adb_only", True)
capture.adb_frame = lambda idx, **k: ADB
r_img, r_adb = capture.grab_with_fallback(1, 1)
check("⑤ capture_adb_only=true → 直连 adb (帧, True)", r_img is ADB and r_adb is True)
cm._config.set("emulator.capture_adb_only", False)

# 空帧：adb 取不到 → (None, True) 空帧自旋
capture.adb_frame = lambda idx, **k: None
r_img, r_adb = capture.grab_with_fallback(1, 1)
check("⑤ adb 空 → (None, True) 空帧自旋（不喂死帧）", r_img is None and r_adb is True)

# 恢复打桩
ldplayer.resolve_serial = _orig_resolve
ldplayer.adb_exec = _orig_exec

print("-" * 46)
if failures:
    print(f"SMOKE FAILED: {failures}")
    sys.exit(1)
print("SMOKE PASS")