"""
core/win_utils.py — 零散 Windows 窗口小工具 + 全项目唯一的 user32/shell32
签名声明出口

放 core 层（无 Qt）：句柄本来就是系统概念，ui 层拿 ldplayer.InstanceInfo.top_hwnd
直接调即可，不绕 Qt。
2026-09-21 从 capture.py 的 ctypes 用法里拆出来——capture 管截图，
窗口操作是另一件事，别混进去。
2026-10-04 升级为「唯一已声明 user32 出口」：capture/ldplayer 原先各自对
windll.user32 重复声明同名函数签名（且 bring_to_front/ensure_taskbar_button
的 SetWindowPos/GetWindowLongW/SetWindowLongW 是裸调——64 位句柄按默认
32 位 int 传参有截断隐患，句柄大值时窗口操作偶发失灵极难排查）。
ctypes.windll 对同一 DLL 缓存同一实例，签名在本模块 import 时声明一次，
所有 import 本模块拿 user32 的地方全局生效。新用 user32 函数：在这里补声明，
别处不要再写 argtypes 样板。gdi32 只有截图用，声明仍留 capture.py。
2026-10-15 起本模块同时是 shell32 的 AppUserModelID 声明出口（
set_window_app_id，任务栏分组身份的唯一入口）。
"""

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
ole32 = ctypes.windll.ole32

ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
ole32.CLSIDFromString.restype = ctypes.c_long
ole32.PropVariantClear.argtypes = [ctypes.c_void_p]
ole32.PropVariantClear.restype = ctypes.c_long

# ── 全项目用到的 user32 函数签名（一次声明，处处生效）──────────────────
# HWND 参数必须声明：默认 int 传参在 64 位下会截断句柄。
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.BOOL]
user32.MoveWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL

# SetWindowPos 用到的常量（微软官方值）
_HWND_TOP = 0
_HWND_NOTOPMOST = -2
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_SHOWWINDOW = 0x0040


def is_alive(hwnd: int) -> bool:
    """句柄对应的窗口当前是否还在（Win32 IsWindow）。0/坏句柄 → False，不抛。
    挂机/测试用来在 list2 快照抽风（空列表或 pid 列瞬时读成 0）时，凭"我们正在
    截的那个窗口句柄还活着"兜底判模拟器未关——窗口可最小化但不许关是本项目铁律，
    活句柄比 ldconsole 一次读数更可信。
    ponytail: 只认 IsWindow；极小概率句柄被回收给别的 app 窗口会误判活着，
    真关了时下一次 list2 正常读数会纠正，不做类名二次核对（省一次 GetClassName 往返）。"""
    if not hwnd or hwnd <= 0:
        return False
    try:
        return bool(user32.IsWindow(int(hwnd)))
    except Exception:
        return False


def bring_to_front(hwnd: int) -> bool:
    """
    把句柄对应的窗口提到所有窗口最前（不改位置、不改大小）。

    先 NOTOPMOST 再 TOP 是大众做法：只调一次时，若目标窗口曾是"置顶窗口"，
    单独 SetWindowPos(TOP) 可能被系统按置顶层压回去；先摘掉置顶属性再提，
    任何状态的窗口都能真的浮到最上面。
    返回是否调用成功（句柄失效/为 0 时 False，不抛异常——菜单点了没反应
    比弹个 WinError 对话框友好）。
    """
    if not hwnd or hwnd <= 0:
        return False
    try:
        user32.SetWindowPos(hwnd, _HWND_NOTOPMOST, 0, 0, 0, 0,
                            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        ok = user32.SetWindowPos(hwnd, _HWND_TOP, 0, 0, 0, 0,
                                 _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        return bool(ok)
    except Exception:
        return False


# GWL_EXSTYLE / WS_EX_APPWINDOW：Windows 扩展样式常量（Win32 文档官方值）
_GWL_EXSTYLE = -20
_WS_EX_APPWINDOW = 0x00040000
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020


def ensure_taskbar_button(hwnd: int) -> bool:
    """
    强制窗口在任务栏有自己的按钮（补 WS_EX_APPWINDOW 扩展样式）。

    为什么需要：带主人（Qt parent）的从属窗口在 Windows 上默认不进任务栏，
    最小化后按老规矩缩成一条迷你标题栏飘在屏幕随缘位置，用户满屏找窗。
    WS_EX_APPWINDOW 就是微软给的「强制顶层窗上任务栏」开关，补上后
    最小化正常进任务栏。SetWindowPos(FRAMECHANGED) 是配套重算框架，
    文档说扩展样式变更要它才即刻生效。
    调用方在 showEvent 里每次显示都调一遍：Qt 的 setWindowFlag（日志窗
    置顶钉）会重建 HWND，扩展样式跟着没，必须重补。
    返回是否成功（句柄无效/非 Windows 平台 False，不抛异常）。
    """
    if not hwnd or hwnd <= 0:
        return False
    try:
        ex = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if not ex & _WS_EX_APPWINDOW:
            if not user32.SetWindowLongW(hwnd, _GWL_EXSTYLE,
                                         ex | _WS_EX_APPWINDOW):
                return False
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED)
        return True
    except Exception:
        return False


# ── AppUserModelID：决定任务栏按钮如何分组（点按钮是最小化还是弹出预览）──────────
# 全局身份由 main.py 的 SetCurrentProcessExplicitAppUserModelID 设一次；下面这组
# 常量是给「带主人的从属顶层窗」设【窗口级】身份用的（窗口级覆盖进程级）。
#
# ⚠ 病根（2026-10-15 真机反馈「点任务栏图标不会最小化」）：进程级 AppID 一旦
# 显式设定，本进程【所有】顶层窗默认同组——主界面 + 工作台/日志/状态（这三窗又
# 被 ensure_taskbar_button 补了 WS_EX_APPWINDOW，全部挤进任务栏）被 Windows 合成
# 【一个】任务栏按钮。而「合并后的按钮，单击 = 弹出窗口预览缩略图（或仅置前），
# 不再是最小化」正是 Win7 起任务栏的既定行为（MSDN「Taskbar Extensions」：一个
# 按钮代表一个 group，组内每个成员各占一张缩略图；单窗口按钮才走"点一下最小化"
# 的开关语义）。解法 = 给每个窗口设各自不同的窗口级 AppID，Windows 即按 AppID
# 拆成独立按钮，各自恢复"点一下最小化"的单窗口开关行为。
_APPID_ROOT = "EmuHive.IdleTool"
APPID_MAIN = _APPID_ROOT                      # 主界面
APPID_WORKBENCH = _APPID_ROOT + ".Workbench"  # 工作台
APPID_LOG = _APPID_ROOT + ".Log"              # 运行日志
APPID_STATUS = _APPID_ROOT + ".Status"        # 状态信息


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16),
                ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]


class _PKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _PROPVARIANT(ctypes.Structure):
    """完整 24 字节 PROPVARIANT（x64）：头 8 字节 + 16 字节 union。
    ⚠ 必须整块给足——SetValue/GetValue 按 sizeof(PROPVARIANT) 整块拷贝，
    只声明用到的 pwszVal 会短一截、API 读写越界踩栈。union 里 pad 撑满 16 字节，
    VT_LPWSTR(31) 时读/写 pwszVal 这一支。"""
    class _U(ctypes.Union):
        _fields_ = [("pwszVal", ctypes.c_wchar_p), ("_pad", ctypes.c_byte * 16)]
    _anonymous_ = ("u",)
    _fields_ = [("vt", ctypes.c_uint16), ("r1", ctypes.c_uint16),
                ("r2", ctypes.c_uint16), ("r3", ctypes.c_uint16), ("u", _U)]


def _guid(text: str) -> "_GUID":
    g = _GUID()
    ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    return g


_IID_IPropertyStore = _guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
_PKEY_APPID = _PKEY(_guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)  # pid 5


def _prop_store(hwnd: int):
    """SHGetPropertyStoreForWindow(HWND, IID_IPropertyStore) → IPropertyStore
    裸指针（c_void_p 值），失败返回 None。调用方务必 Release。"""
    shell32.SHGetPropertyStoreForWindow.argtypes = [
        wintypes.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
    store = ctypes.c_void_p()
    hr = shell32.SHGetPropertyStoreForWindow(
        wintypes.HWND(int(hwnd)), ctypes.byref(_IID_IPropertyStore),
        ctypes.byref(store))
    if hr != 0 or not store.value:
        return None
    return store


def _vfn(store, idx: int):
    """COM 裸指针 → vtable 第 idx 个函数指针（IUnknown 占 0~2）。"""
    vt = ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0]
    return ctypes.cast(vt, ctypes.POINTER(ctypes.c_void_p))[idx]


def _release(store):
    try:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(_vfn(store, 2))(store)
    except Exception:
        pass


def set_window_app_id(hwnd: int, app_id: str) -> bool:
    """
    给单个窗口设【窗口级】AppUserModelID（覆盖进程级），拆开任务栏分组。

    流程：SHGetPropertyStoreForWindow(HWND, IID_IPropertyStore) 拿窗口属性仓
    （IPropertyStore） → SetValue(PKEY_AppUserModel_ID, VT_LPWSTR) → Commit。
    Commit 之后 Windows 才刷新任务栏按钮归属，每个不同 app_id 一个独立按钮。

    为什么必须窗口级、不能靠进程级：进程级只能全进程一个身份，主窗与工作台/
    日志/状态永远同组 → 合并成一个按钮 → 点它只弹预览不最小化（见上方病根注释）。
    窗口级身份可按窗各给一个，正好拆散。

    调用方在 showEvent 每次显示都调一遍：Qt 的 setWindowFlag 会重建 HWND
    （日志窗置顶钉），窗口级属性跟着新 HWND 一起没了，必须重设（与
    ensure_taskbar_button 同一纪律）。
    返回是否成功（句柄无效/调用失败一律 False，不抛异常）。
    """
    if not hwnd or hwnd <= 0 or not app_id:
        return False
    store = None
    try:
        store = _prop_store(hwnd)
        if store is None:
            return False
        SetValue = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PKEY),
            ctypes.POINTER(_PROPVARIANT))(_vfn(store, 6))
        pv = _PROPVARIANT(vt=31, pwszVal=app_id)   # VT_LPWSTR
        if SetValue(store, ctypes.byref(_PKEY_APPID), ctypes.byref(pv)) != 0:
            return False
        Commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(_vfn(store, 7))
        return Commit(store) == 0
    except Exception:
        return False
    finally:
        if store is not None:
            _release(store)


def window_app_id(hwnd: int) -> str | None:
    """读窗口的窗口级 AppUserModelID（没设过/读失败 → None）。
    供冒烟做 set→get 往返验证，真机上也用来核对分组身份是否落上。"""
    if not hwnd or hwnd <= 0:
        return None
    store = None
    try:
        store = _prop_store(hwnd)
        if store is None:
            return None
        GetValue = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PKEY),
            ctypes.POINTER(_PROPVARIANT))(_vfn(store, 5))
        pv = _PROPVARIANT()
        if GetValue(store, ctypes.byref(_PKEY_APPID), ctypes.byref(pv)) != 0:
            return None
        value = pv.pwszVal if pv.vt == 31 else None
        if pv.vt == 31:
            ole32.PropVariantClear(ctypes.byref(pv))
        return value
    except Exception:
        return None
    finally:
        if store is not None:
            _release(store)
