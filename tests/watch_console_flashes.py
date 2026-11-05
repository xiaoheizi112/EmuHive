# 取证哨兵：逮「命令行子进程闪现黑窗」。打包版是无控制台 GUI，每起一个
# 命令行子进程（ldconsole/adb/taskkill）系统就被迫新分配一个控制台窗——
# 实测症状「窗口一闪一闪、打字被打断」即此。
# 判据：可见窗 + 类名是控制台系 + 宿主进程是那几个命令行工具。
# 跑法：python tests/watch_console_flashes.py [秒数=20]
# 用法：dist 里有 EmuHive.exe 时本脚本自动拉起它（跑完杀掉）；
#       也可以先手动开好被测程序再跑本脚本。
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
EnumWindows = user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
IsWindowVisible = user32.IsWindowVisible
GetClassName = user32.GetClassNameW
GetWindowThreadProcessId = user32.GetWindowThreadProcessId

CONSOLE_CLS = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS",
               "PseudoConsoleWindow"}
# 本程序会起的子进程名（ffmpeg 随 scrcpy 后端一并移除，不再列）
WATCH_PROC = ("ldconsole", "dnconsole", "adb", "taskkill")

PQ = 0x1000


def proc_name(pid):
    h = kernel32.OpenProcess(PQ, False, pid)
    if not h:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return "?"
    finally:
        kernel32.CloseHandle(h)


def scan_once():
    hits = []

    def cb(hwnd, _):
        if not IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        GetClassName(hwnd, buf, 256)
        if buf.value not in CONSOLE_CLS:
            return True
        pid = wt.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = proc_name(pid.value)
        if any(w in name.lower() for w in WATCH_PROC):
            hits.append((hwnd, buf.value, pid.value, name))
        return True

    EnumWindows(EnumWindowsProc(cb), 0)
    return hits


secs = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
exe = sys.argv[2] if len(sys.argv) > 2 else next(
    (p for p in (os.path.join(root, "dist", "EmuHive", "EmuHive.exe"),
                 os.path.join(root, "dist", "EmuHive", "EmuHive",
                              "EmuHive.exe"))
     if os.path.isfile(p)), "")
proc = None
if os.path.isfile(exe):
    proc = subprocess.Popen([exe])
    print(f"已拉起被测: {exe} (pid={proc.pid})")
print(f"哨兵盯 {secs:.0f}s，统计闪现黑窗（ldconsole/adb/taskkill 的控制台窗）…")
seen, deadline = set(), time.time() + secs
flashes = 0
while time.time() < deadline:
    for hwnd, cls, pid, name in scan_once():
        if hwnd not in seen:
            seen.add(hwnd)
            flashes += 1
            print(f"  [{time.strftime('%H:%M:%S')}] 闪现: {name} (pid={pid}) 类={cls}")
    time.sleep(0.02)
if proc is not None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(f"结论: {secs:.0f} 秒内闪现黑窗 {flashes} 个 —— "
      + ("仍有闪窗，没修好" if flashes else "零闪窗，修好了"))
