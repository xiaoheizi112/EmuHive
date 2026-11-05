"""
core/capture.py — 取帧层（挂机主路 = adb 单次快照；窗口截图为手动场景保留）

把"屏幕画面"变成 numpy 像素数组，让程序能"看见"屏幕。两条路：
- **挂机/测试/预览主路 = adb 单次快照**（`adb_frame`/`grab_with_fallback`）：
  截图发生在安卓内部，与 Windows 版本无关（1607 可用），无常驻进程、CPU 近零。
  详见下方「adb 取帧」段头。
- 窗口截图（`grab_frame`/`capture`，BitBlt/PrintWindow）只服务**手动场景**：
  框选采集、校准量偏移、预览跟随——这些要的是"带窗口边框的整窗图"。挂机主循环
  不吃窗口（叠放/遮挡/最小化都不影响）。
⚠ 2026-10-11：scrcpy 取帧后端已**整体移除**（曾作 1607 冻帧根治主路，后降级为
可选后端，最终删除）。挂机取帧现只有 adb 单次快照这一条路。

本文件保留的窗口截图知识点（手动场景仍用它）：
1. ctypes —— Python 和 C 语言库之间的桥。Windows 提供的 API（user32/gdi32）
   本来就是给 C 程序用的，ctypes 让 Python 也能直接调用它们
2. 两种截窗口的方式：
   - BitBlt：直接从窗口画布抄像素（快，但模拟器的显卡画面可能抄到黑屏）
   - PrintWindow + PW_RENDERFULLCONTENT：请系统桌面合成器"重放"窗口画面
     （慢一点，但对模拟器通常有效）
3. 图片 = 像素数组：一张 BGR 图就是一个三维 numpy 数组 (高, 宽, 3)，
   每个元素是 0-255 的一个数。一切"图像处理"都是对这个数组做数学运算
4. 黑屏判定：全黑图片的灰度均值≈0 且方差≈0（所有像素都一样才叫"黑"），
   用 numpy 的 mean()/std() 就能统计
"""

import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes

import cv2
import numpy as np

from core.config_manager import PROJECT_ROOT
from core.win_utils import user32   # 全项目唯一已声明 user32 出口（签名在 win_utils 里声明）

# ── 一、DPI 感知（重要！）─────────────────────────────────────────────
# Windows 显示缩放（如 125%）会让窗口尺寸 API 返回"逻辑像素"而非真实像素，
# 截图会错位。这里把【当前线程】设为"每显示器 V2 感知"（真实像素）。
# 为什么用线程级而不是进程级 SetProcessDPIAware：
# 进程级感知一旦设置就不可更改；Qt6 启动时也要给进程设自己的感知，
# 谁抢在前面谁赢，另一个报"拒绝访问"警告（第 6 步实测踩坑）。
# 线程级设置不与 Qt 冲突、随时可调，且对 GetClientRect/BitBlt 同样生效。
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
ctypes.windll.user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)

gdi32 = ctypes.windll.gdi32   # gdi32 只有截图用，签名声明留在本模块

# Windows API 的"魔法数字"（常量），含义固定，直接照抄
SRCCOPY = 0x00CC0020              # BitBlt 的"原样复制"模式
PW_RENDERFULLCONTENT = 0x00000002  # PrintWindow：请求 DWM 合成后的完整画面
DIB_RGB_COLORS = 0                 # GetDIBits：直接给 RGB 像素值
BI_RGB = 0                         # 位图压缩方式：不压缩

# 基准分辨率唯一源：模板采集/截图缩放/点击坐标全按这两个常量（三方一致铁律）。
# 它不是配置项（原 emulator.base_width/base_height 死旋钮已删），改基准=改这里。
BASE_WIDTH, BASE_HEIGHT = 540, 960


# ── 二、ctypes 类型准备：告诉 Python 每个 API 的参数和返回值类型 ─────────
# 不声明 argtypes 的话，句柄这类 64 位整数传参时可能被截断成 32 位 → 崩溃。
# 这一步是 ctypes 编程最容易踩的坑。
# user32 的签名已全部收口到 core/win_utils.py（本模块 import 它即生效）；
# 下面只留 gdi32 —— 画图函数只有截图这条路用，不必进公共出口。

class BITMAPINFOHEADER(ctypes.Structure):
    """位图信息头：描述"像素怎么排列"的说明书"""
    _fields_ = [
        ("biSize", wintypes.DWORD),      # 本结构大小 = 40
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),     # 负数 = 行从上往下排
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),   # 32 = 每像素 4 字节 BGRA
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


# ── 三、核心函数 ─────────────────────────────────────────────────────

def grab_frame(hwnd: int, method: str = "bitblt") -> np.ndarray | None:
    """
    用指定方式截取窗口客户区（不含标题栏），返回 BGR 像素数组。

    参数:
        hwnd:   窗口句柄（从 ldplayer.list_instances 的 top_hwnd 拿）
        method: "bitblt" 或 "printwindow"

    返回:
        numpy 数组 (高, 宽, 3)，失败（最小化/句柄无效等）返回 None

    流程：申请一块内存画布 → 把窗口画面画上去 → 把画布读成像素数组 → 释放资源
    （内存画布用完必须释放，否则反复截图上万次后内存会耗尽——这是 C 编程的规矩）
    """
    if user32.IsIconic(hwnd):
        return None  # 窗口最小化了，截不到

    # 1. 问窗口要尺寸
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    # 2. 建内存画布（和窗口画布"兼容"的画布 + 位图）
    #    句柄为 0 的失败路径逐层短路：旧版 GetDC/CreateCompatibleDC/CreateCompatibleBitmap
    #    失败时不检查，继续拿 0 句柄跑 SelectObject/BitBlt/GetDIBits（靠后面
    #    finally 释放，不泄漏，但拿 0 句柄刷 API 属纯踩雷行为，未定义结果）。
    #    ⚠ 资源释放的全局保证一直是 finally（早退 return 也走 finally），别改坏。
    hdc_win = user32.GetDC(hwnd)
    if not hdc_win:
        return None
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    if not hdc_mem:
        user32.ReleaseDC(hwnd, hdc_win)
        return None
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    if not bmp:
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)
        return None
    gdi32.SelectObject(hdc_mem, bmp)

    try:
        # 3. 把窗口画面画到内存画布（两种方式二选一）
        if method == "bitblt":
            ok = gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_win, 0, 0, SRCCOPY)
        else:
            ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
        if not ok:
            return None

        # 4. 把画布读成原始字节
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h  # 负数 = 从上往下排，省去手动翻转
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = BI_RGB

        buf = ctypes.create_string_buffer(w * h * 4)  # 每像素 4 字节
        got = gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)
        if got != h:
            return None

        # 5. 字节 → numpy 数组 (h, w, 4) → 去掉 Alpha 通道变 (h, w, 3) BGR
        #    （cvtColor 返回新建的 numpy 数组，不吊着 buf 这块 ctypes 原生内存）
        img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    finally:
        # 无论成功失败都要释放（finally：即使上面 return/异常也会执行）
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)


def is_black(img: np.ndarray, mean_thresh: float = 10.0,
             std_thresh: float = 8.0) -> bool:
    """
    判断一张图是不是"黑屏"：灰度均值很低 且 方差很低（所有像素几乎一样）。
    mean = 亮度高低，std = 内容多不多。两者都低 = 没内容 = 黑屏。

    快路径：正常画面先过一道 BGR 通道均值粗筛（cv2.mean 一遍 SIMD 扫过，
    比 cvtColor+mean+std 三遍全帧便宜得多）。数学上无损：灰度均值 =
    0.114R+0.587G+0.299B 的通道加权，三通道均值全都 ≥ mean_thresh 时
    加权平均必然 ≥ mean_thresh，原式第一个条件就不成立 → 直接 False。
    只有"至少一个通道偏暗"的帧（真黑屏/转场/暗场景）才付精确判的全代价。
    """
    m = cv2.mean(img)   # (B, G, R, A)：float64 累加成对求和，无溢出
    if min(m[0], m[1], m[2]) >= mean_thresh:
        return False
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) < mean_thresh and float(gray.std()) < std_thresh


def capture(hwnd: int, method: str = "auto") -> np.ndarray | None:
    """
    截图主入口。method="auto" 时自动回退：BitBlt 失败或截到黑屏 → 换
    PrintWindow 再试 → 还不行返回 None（更底层兜底走 adb_frame/grab_with_fallback）。
    """
    if method in ("bitblt", "printwindow"):
        return grab_frame(hwnd, method)
    img = grab_frame(hwnd, "bitblt")
    if img is None or is_black(img):
        img = grab_frame(hwnd, "printwindow")
    return img


# ── adb 取帧（2026-10-11 方案 A/B：挂机取帧主路 = adb 单次快照）─────────────
# 认知（本次改造的根本理由）：挂机是「每 frame_interval 一张当前屏」的**离散
# 轮询**，不是连续视频。adb screencap 即拍即得、画面动不动都有帧、无常驻
# 进程——与离散取帧同构，正是它的舒适区。（曾用 scrcpy 视频流当主路，其
# 编码器→隧道→解码器、静止不出帧、冷启动窗口、一实例一主人是「连续投屏」
# 负载，拿它做离散取帧属模型错配；2026-10-11 已把 scrcpy 整体移除。）
#
# 实测（雷电4 自带 adb 1.0.31，2026-10-11）：
#   · exec-out 在 1.0.31 不存在（2012 版，1.0.32+ 才有）——不能拿它图省事；
#   · `shell screencap -p` 直通 stdout = **172ms/帧（单条往返）**，但老 adb 经
#     shell PTY 会把 PNG 里的 0x0A 翻成 0x0D 0x0A（实测 311 处），imdecode 直接
#     失败；折叠 `\r\n`→`\n` 即**无损还原**（pull 版本正好小 311 字节，与 CRLF
#     对数吻合，证明是纯换行翻译而非数据损坏）——故本模块主选直通 + 折叠；
#   · 落 /data/local/tmp（tmpfs，非 /sdcard 的 FUSE 慢盘）+ pull = 323ms（两条
#     往返；拆开 = screencap 260ms + pull 63ms）——只作直通解不出图时的垫底；
#   · 绕开 ldconsole 中转直连 adb.exe：每条少 fork 一个 ldconsole.exe（实测 +40ms/
#     条），取帧是每拍热路径，多开时这笔省得可观。
# adb 单次快照 172ms、无常驻进程、CPU 近零占用——对「能挂几台」是净利好。

_SERIAL_TTL = 30.0            # 序列号缓存保鲜期（秒）：避免每拍 fork `adb devices`
_serial_cache: dict = {}
_serial_lock = threading.Lock()


def _adb_serial(index: int, force: bool = False) -> str | None:
    """缓存本实例当前在线的 adb 序列号（每拍取帧都要用，`adb devices` 探测极贵
    不能每拍打）。TTL 到点或 force 重解析；解析走 ldplayer.resolve_serial
    （雷电一实例两个名字谁在线用谁，写死任一边会复发 offline）。取帧失败会
    force 重解析一次——模拟器重启换名后自愈。"""
    now = time.monotonic()
    with _serial_lock:
        ent = _serial_cache.get(index)
        if ent is not None and not force and now - ent[1] < _SERIAL_TTL:
            return ent[0]
    from core import ldplayer
    try:
        ser = ldplayer.resolve_serial(index)
    except Exception:
        ser = None
    with _serial_lock:
        _serial_cache[index] = (ser, now)
    return ser


def _decode_screencap_png(raw: bytes) -> np.ndarray | None:
    """screencap -p 的字节流 → BGR 图；解不出返回 None。
    老 adb 经 shell PTY 把 LF 翻成 CRLF（见段头实测）：先按原样解，失败再折叠
    `\\r\\n`→`\\n`（PTY 换行翻译的精确逆运算，无损）后重解；两路都不行给 None，
    由上层落 tmpfs+pull 垫底。"""
    if not raw:
        return None
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is not None:
        return img
    if b"\r\n" in raw:
        fixed = raw.replace(b"\r\n", b"\n")
        return cv2.imdecode(np.frombuffer(fixed, np.uint8), cv2.IMREAD_COLOR)
    return None


def _adb_stream_frame(index: int, timeout: int) -> np.ndarray | None:
    """adb 快路主选：`adb -s <serial> shell screencap -p` 直通 stdout，单条往返
    （实测 172ms）。serial 解析不出 / 命令失败 / 解不出图一律返回 None。"""
    from core import ldplayer
    ser = _adb_serial(index)
    if not ser:
        return None
    try:
        r = ldplayer.adb_exec(["shell", "screencap", "-p"], timeout=timeout, serial=ser)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return _decode_screencap_png(r.stdout or b"")


def _adb_pull_frame(index: int, tag: str, timeout: int) -> np.ndarray | None:
    """adb 慢兜底取图：screencap 落 /data/local/tmp（tmpfs，非 /sdcard FUSE 慢盘）
    + pull 读回（实测 323ms，二进制安全，无 PTY 污染）。快路直通解不出图时垫底，
    也是启动校准取「安卓真值图」的通道。读不出来返回 None；adb 异常原样上抛，
    由调用方（校准/adb_frame）按各自口径处置。tag = 临时文件名前缀，按实例分开，
    多路同时操作共用一个文件名会互相覆盖 pull 下来的图。"""
    from core import ldplayer
    ser = _adb_serial(index)
    if not ser:
        return None
    remote = f"/data/local/tmp/{tag}_{index}.png"
    # PROJECT_ROOT 锚定：相对路径在「双击启动/服务态 cwd 不是项目根」时会写歪地方
    local = str(PROJECT_ROOT / "logs" / f"{tag}_{index}.png")
    os.makedirs(PROJECT_ROOT / "logs", exist_ok=True)
    ldplayer.adb_exec(["shell", f"screencap -p {remote}"], timeout=timeout, serial=ser)
    ldplayer.adb_exec(["pull", remote, local], timeout=timeout, serial=ser)
    return load_image_cn(local)


def adb_frame(index: int, timeout: int = 8) -> np.ndarray | None:
    """挂机取帧**主路**：adb 单次快照（认知与实测见本段头）。

    先直通 stdout（172ms/帧），解不出 PNG 再落 tmpfs+pull（323ms）。返回纯安卓
    画面（非基准尺寸时缩到基准），调用方拿去做 frame 前**不要再按 calib 裁剪**
    （裁的是窗口截图，这里根本没有窗口边框）。任何失败返回 None，绝不抛。
    配置 emulator.capture_adb_fallback=false 可一票关死本路。
    """
    from core.config_manager import get_config   # 延迟导入避免初始化环
    if not bool(get_config().get_safe("emulator.capture_adb_fallback", True)):
        return None
    img = None
    try:
        img = _adb_stream_frame(index, timeout)
        if img is None:
            # 直通失败：逼一次序列号重解析（模拟器重启换名后自愈）再试一路，
            # 仍不行落 tmpfs+pull 垫底。三路皆空返回 None（上层空帧自旋）。
            _adb_serial(index, force=True)
            img = _adb_stream_frame(index, timeout)
            if img is None:
                img = _adb_pull_frame(index, "_dld_frame", timeout)
    except Exception:
        return None
    if img is None:
        return None
    return to_base(img)   # 安卓分辨率与基准不一致（用户手改过）→ 拉回基准空间


def grab_with_fallback(hwnd: int, index: int, method: str = "auto"):
    """统一取帧决策口（挂机/测试/预览/无头试跑四条路都走这里）。
    返回 (img|None, from_adb:bool)。

    from_adb 语义已从字面「来自 adb」泛化为「纯安卓帧」：True = 图片是安卓
    系统直接产出的基准图，无窗口边框可裁，调用方必须跳过 calib 裁剪。本口
    不再向窗口要像素，恒返回 True。

    取帧序（2026-10-11 方案 A/B 定：**adb 单次快照是唯一取帧路**——挂机是
    离散轮询不是连续视频，screencap 即拍即得、无常驻、CPU 近零，认知与实测
    见「adb 取帧」段头。scrcpy 已整体移除，见文件头「无 scrcpy」说明）：
      1. emulator.capture_adb_only=true → 直连 adb（排障逃生口，行为同默认）。
      2. adb_frame：主路。直通 stdout 单条往返解不出再 tmpfs+pull 垫底。
      3. 空 → (None, True)：空帧自旋，调用侧下拍再试。宁等活帧不喂死帧。
    hwnd/method 两参数只为兼容旧调用位保留，本口不再向窗口要像素；capture()
    （窗口抄像素）仍服务框选采集、校准量偏移、预览跟随等手动场景。
    """
    return adb_frame(index), True


# ── 安卓画面区域定位（窗口路专用）─────────────────────────────────────


def to_base(img: np.ndarray) -> np.ndarray:
    """缩放到基准分辨率 540x960，保证所有实例截图尺寸一致、模板可通用"""
    if img.shape[1] == BASE_WIDTH and img.shape[0] == BASE_HEIGHT:
        return img
    return cv2.resize(img, (BASE_WIDTH, BASE_HEIGHT), interpolation=cv2.INTER_LINEAR)


def imwrite_cn(path: str, img: np.ndarray) -> bool:
    """
    保存图片（兼容中文路径版）。
    cv2.imwrite 在 Windows 上遇到中文路径会静默失败，所以绕道：
    先编码成 PNG 字节，再用 numpy 直接写文件。
    """
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    buf.tofile(path)
    return True


def load_image_cn(path: str, keep_alpha: bool = False) -> np.ndarray | None:
    """读图片（兼容中文路径版），与 imwrite_cn 对称；失败返回 None。
    keep_alpha=True 时保留 PNG 的透明通道（抠图模板遮罩用，返回 BGRA 4 通道）；
    默认 False = 一律转 BGR 3 通道，老调用点行为不变。"""
    if not os.path.exists(path):
        return None
    buf = np.fromfile(path, dtype=np.uint8)
    if buf.size == 0:
        # 0 字节文件（adb pull 失败留的空壳等）：imdecode 遇空 buf 是抛断言
        # 异常而不是返回 None，不拦会把调用方整个炸穿（1607 移植机实测）
        return None
    img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 2:                       # 单通道灰度图 → BGR
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:
        return img if keep_alpha else cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img                              # 3 通道 BGR 原样


# ── 安卓画面区域定位 ──────────────────────────────────────────────────
# 雷电把安卓画面渲染在一个 class 名为 RenderWindow 的子窗口里。
# 它的几何（位置+尺寸）就是"安卓画面在窗口客户区里的偏移和实际大小"，
# 由雷电自己随窗口缩放实时维护 —— 比图像匹配可靠，也不受分辨率/缩放影响。
#
# 实测 2026-09-10（雷电4 绿化版 D:\LDPlayer，dnplayer 4.0.83.0，旧注误记"雷电9"；安卓 540x960）：
#   窗口 582x996   → RenderWindow pos=(1,34)   size=540x960
#   窗口拖大 2x    → RenderWindow pos=(12,34)  size=1100x1956
# 对照 calibrate()（adb screencap + matchTemplate）：原始尺寸 score 0.9999，
# 拖大后直接返回 None —— matchTemplate 不是尺度无关的，找不到放大后的画面。

RENDER_WINDOW_CLASS = "RenderWindow"
_enum_callback_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_render_window(hwnd: int) -> dict | None:
    """
    量出安卓画面在【窗口客户区】里的位置和尺寸。
    返回 {"offset_x", "offset_y", "width", "height", "score": 1.0}；没找到返回 None。

    score 恒为 1.0 是为了与 calibrate() 的返回结构对齐（调用方统一看 score）。
    坐标用 ScreenToClient 换算：EnumChildWindows 给的是屏幕绝对坐标，
    而 grab_frame 截的是客户区，两者原点不一定重合（有边框的窗口就差一截），
    换算过才严格对齐。
    """
    found: dict = {}

    @_enum_callback_t
    def callback(child, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, buf, 256)
        if buf.value == RENDER_WINDOW_CLASS:
            rect = wintypes.RECT()
            if user32.GetWindowRect(child, ctypes.byref(rect)):
                w, h = rect.right - rect.left, rect.bottom - rect.top
                if w > 0 and h > 0:
                    # 左上角屏幕坐标 → 客户区坐标
                    pt = wintypes.POINT(rect.left, rect.top)
                    if user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(pt)):
                        found.update({"offset_x": int(pt.x), "offset_y": int(pt.y),
                                      "width": int(w), "height": int(h), "score": 1.0})
            return False   # 找到了，停止枚举
        return True

    user32.EnumChildWindows(wintypes.HWND(hwnd), callback, 0)
    return found or None


def calib_drift_check(calib: dict | None, hwnd: int) -> dict | None:
    """每取帧顺手复核 RenderWindow 几何，漂移超 2px 就换新值（原 calib 不动）。
    专治「校准后用户展开/收起雷电左侧工具栏、拖动窗口大小 → 安卓区搬了家，
    旧 calib 裁歪」：预览带出边框、识别整体偏移还查不出原因（1607 移植机实测）。
    成本 = 一次 EnumChildWindows（微秒级，零 adb 往返），每拍调用安全。
    非雷电（找不到子窗口）走图像校准值不碰；原 calib 缺失而几何可得则补上。"""
    geo = find_render_window(hwnd)
    if geo is None:
        return calib
    if calib is None:
        return geo
    for k in ("offset_x", "offset_y", "width", "height"):
        if abs(geo[k] - calib.get(k, 1 << 30)) > 2:
            return geo
    return calib


# ── 启动校准（第 3 步新增）：自动测量"安卓画面在窗口里的偏移" ────────────
# 问题：窗口客户区（本机实测 582x996）比安卓画面（540x960）大一圈，
# 且偏移量可能因电脑/雷电版本/窗口缩放而不同（见第 2 步实测：偏移 (1,34)）。
#
# 两条测量路径（2026-09-10 改造）：
#   首选  量 RenderWindow 子窗口几何 —— 雷电自己维护，窗口拖大/改分辨率都准
#   兜底  adb 截安卓真值 + matchTemplate 在窗口截图里找 —— 几何法失效时才走
#         （非雷电模拟器、雷电改了子窗口类名等）。注意它只在窗口原生尺寸下有效

def calibrate(hwnd: int, index: int, attempts: int = 3,
              adb_timeout: int = 30) -> dict | None:
    """
    测量安卓画面在窗口客户区内的位置和尺寸，返回
    {"offset_x", "offset_y", "width", "height", "score"}。全部测不到返回 None。

    width/height 是安卓画面在【窗口截图里】的实际像素尺寸 —— 窗口原生尺寸时
    等于安卓分辨率（540x960），窗口被拖大时会跟着变大（实测 1100x1956）。
    crop_android 按它裁，再缩到基准分辨率，所以拖大窗口也能正常识别。

    attempts / adb_timeout 是"最坏堵多久"的两个旋钮（默认 = 历史行为）：
    图像兜底路径每轮 = screencap + pull 两条 adb（各 ≤adb_timeout 秒）。
    启动期（独立线程）可以给足 3 轮 × 30 秒；挂机循环内重校准只许传小值
    （见 bot/worker.py：adb 偶发挂起时整条 tick 循环冻结数十秒、点停止
    没反应 —— "worker 循环绝不阻塞"不变量的实际破口）。
    """
    # ── 路径 1：量 RenderWindow 子窗口几何（可靠、零 adb 往返）──
    geo = find_render_window(hwnd)
    if geo is not None:
        return geo

    # ── 路径 2：图像匹配兜底 ──
    import time

    for _ in range(attempts):
        # 1. 用 adb 截安卓画面真值（try/except：adb 偶发超时/断连/通道死时优雅返回 None；
        #    adb_command 非 0 抛 RuntimeError、ldconsole 缺失抛 FileNotFoundError，都属"测不到"）
        try:
            truth = _adb_pull_frame(index, "calib_tmp", adb_timeout)
        except subprocess.TimeoutExpired:
            print("[警告] 校准 adb 操作超时")
            return None
        except (RuntimeError, FileNotFoundError) as e:
            print(f"[警告] 校准 adb 通道不可用：{e}")
            return None
        if truth is None:
            return None

        # 2. 窗口截图
        win = capture(hwnd)
        if win is None:
            return None

        # 3. 在窗口截图里找真值图 → 命中位置 = 偏移，真值尺寸 = 画面尺寸
        #    能找到说明窗口是原生尺寸（matchTemplate 非尺度无关），
        #    所以 width/height 直接取 truth 的形状
        #    ⚠ 先过纹理门槛：真值图若是纯色/全黑（黑屏瞬间截到的），归一化
        #    相关系数数学上无定义，OpenCV 会给假高分（实测全黑图配白色模板
        #    也得 1.0），据此定出的偏移是错的、且错得看不出来。与 vision._match_core
        #    的 std<0.5 同一口径。
        if float(cv2.cvtColor(truth, cv2.COLOR_BGR2GRAY).std()) < 0.5:
            time.sleep(1)  # 画面可能在黑屏/转场，稳住一秒再试
            continue
        res = cv2.matchTemplate(win, truth, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if score >= 0.90:
            return {"offset_x": int(loc[0]), "offset_y": int(loc[1]),
                    "width": int(truth.shape[1]), "height": int(truth.shape[0]),
                    "score": float(score)}
        time.sleep(1)  # 画面可能在动画中，稳住一秒再试

    return None


def _crop_calib(img: np.ndarray, calib: dict, to_base_out: bool) -> np.ndarray:
    """按 calib 的 offset/width/height 从窗口截图裁出安卓画面（裁不掉就整图回）。
    to_base_out=True 再缩到 540x960 基准（识别用）；False 原尺寸返回（预览 1:1 用）。

    按 calib 里的 width/height 裁（不再写死 BASE_*）：窗口被拖大时
    RenderWindow 也跟着变大，写死 540x960 只会裁出左上角一小块。
    老 calib（没有 width/height 键）回退 BASE_*，行为与改造前一致。
    """
    dx, dy = calib["offset_x"], calib["offset_y"]
    aw = calib.get("width", BASE_WIDTH)
    ah = calib.get("height", BASE_HEIGHT)
    h, w = img.shape[:2]
    # 负偏移钳到 0：窗口贴屏幕边缘时客户区原点可能在 RenderWindow 右下方
    x1, y1 = max(0, dx), max(0, dy)
    x2, y2 = min(x1 + aw, w), min(y1 + ah, h)
    if x2 <= x1 or y2 <= y1:
        # 裁不出有效区域（窗口最小化中途/尺寸异常）→ 整图兜底；
        # 预览宁可全显也别黑屏（不缩放），识别照旧拉回基准
        return to_base(img) if to_base_out else img
    cut = img[y1:y2, x1:x2]
    return to_base(cut) if to_base_out else cut


def crop_android(img: np.ndarray, calib: dict) -> np.ndarray:
    """把窗口截图裁出安卓画面区域，并缩放到基准分辨率 540x960。
    裁完后识别坐标 == 基准坐标空间，模板可跨实例通用。"""
    return _crop_calib(img, calib, to_base_out=True)


def crop_raw(img: np.ndarray, calib: dict) -> np.ndarray:
    """
    只裁不缩放：返回安卓画面在截图里的原始像素（可能比 540x960 大，取决于
    RenderWindow 当前物理尺寸）。预览 1:1 显示用它 —— 每像素与模拟器画面对应，
    无插值、无模糊，才是"所见即所得"。识别/采集仍走 crop_android（540 基准）。
    """
    return _crop_calib(img, calib, to_base_out=False)


if __name__ == "__main__":
    # 命令行验收：python -m core.capture <index> [--method bitblt|printwindow|auto] [--compare]
    # --compare：两种方式各存一张，方便肉眼对比哪个不黑屏
    import sys
    from core import ldplayer

    if len(sys.argv) < 2:
        print("用法：python -m core.capture <index> [--method bitblt|printwindow|auto] [--compare]")
        sys.exit(1)

    idx = int(sys.argv[1])
    method = "auto"
    compare = False
    for arg in sys.argv[2:]:
        if arg.startswith("--method"):
            method = arg.split(" ", 1)[1] if " " in arg else sys.argv[sys.argv.index(arg) + 1]
        if arg == "--compare":
            compare = True

    inst = ldplayer.get_instance(idx)
    if inst is None:
        print(f"找不到实例 {idx}")
        sys.exit(1)
    if not inst.running:
        print(f"实例 {idx} 未运行，请先启动（python -m core.ldplayer start {idx}）")
        sys.exit(1)

    out_dir = str(PROJECT_ROOT / "logs")   # PROJECT_ROOT 锚定，不吃当前工作目录
    os.makedirs(out_dir, exist_ok=True)

    def report(name, img, path):
        if img is None:
            print(f"[{name}] 截取失败")
            return
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        black = is_black(img)
        saved = imwrite_cn(path, img)
        print(f"[{name}] 尺寸={img.shape[1]}x{img.shape[0]}  灰度均值={gray.mean():.1f} "
              f"方差={gray.std():.1f}  黑屏判定={black}  已保存 {path if saved else '保存失败'}")

    if compare:
        report("bitblt", grab_frame(inst.top_hwnd, "bitblt"), f"{out_dir}/capture_{idx}_bitblt.png")
        report("printwindow", grab_frame(inst.top_hwnd, "printwindow"), f"{out_dir}/capture_{idx}_printwindow.png")
    else:
        img = capture(inst.top_hwnd, method)
        report(f"{method}", img, f"{out_dir}/capture_{idx}.png")
        if img is not None:
            report(f"{method}→基准分辨率", to_base(img), f"{out_dir}/capture_{idx}_base.png")
