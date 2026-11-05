# 哨兵：每 25ms 扫一遍桌面顶层窗口，逮一切标题含 "python"（不区分大小写）的
# 可见窗口，打印 类名/标题/PID/进程名/位置。窗口类名能直接区分元凶身份：
#   Qt...QWindowIcon  → Qt 无父孤儿控件闪现
#   ConsoleWindowClass / CASCADIA / PseudoConsole → 子进程弹出的命令行黑窗
#   其它              → 看打印再定
# 跑法：python tests/watch_python_windows.py          （默认盯 60 秒）
#       python tests/watch_python_windows.py 120     （盯 120 秒）
# 用法：先跑着它，再去流程画布上切换识字取物节点复现闪现，回控制台看打印。
import ctypes
import ctypes.wintypes as wt
import os
import re
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
GetWindowText = user32.GetWindowTextW
GetWindowTextLength = user32.GetWindowTextLengthW
IsWindowVisible = user32.IsWindowVisible
GetClassName = user32.GetClassNameW
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetWindowRect = user32.GetWindowRect

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def proc_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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


def title_of(hwnd):
    n = GetWindowTextLength(hwnd)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    GetWindowText(hwnd, buf, n + 1)
    return buf.value


def class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    GetClassName(hwnd, buf, 256)
    return buf.value


def rect_of(hwnd):
    r = wt.RECT()
    GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


seen = set()
secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
deadline = time.time() + secs
print(f"哨兵在线 {secs:.0f}s：现在去复现闪现（切换识字取物节点）…")
while time.time() < deadline:
    hits = []

    def cb(hwnd, _):
        if not IsWindowVisible(hwnd):
            return True
        t = title_of(hwnd)
        # 词形匹配（连字符也算词内）：控制台标题一般是 "Python" 或
        # "...\python.exe xxx.py"；项目名 EmuHive 不许误报
        if re.search(r"(?<![a-z0-9-])python(?![a-z0-9-])", t.lower()):
            pid = wt.DWORD()
            GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == os.getpid():
                return True
            hits.append((hwnd, t, class_of(hwnd), pid.value))
        return True

    EnumWindows(EnumWindowsProc(cb), 0)
    now = {h[0] for h in hits}
    for hwnd, t, cls, pid in hits:
        if hwnd not in seen:
            print(f"[{time.strftime('%H:%M:%S')}] 逮到: 类名={cls} 标题={t!r} "
                  f"PID={pid}({proc_name(pid)}) 位置={rect_of(hwnd)}")
    seen |= now
    time.sleep(0.025)
print("哨兵下班。没打印任何行 = 这 60 秒里没有 Python 标题窗口出现过。")
