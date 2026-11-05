"""
core/ldplayer.py — 雷电模拟器控制层（第 1 步）

本模块封装对雷电模拟器自带命令行工具 ldconsole.exe 的调用，
让程序的其他部分用"Python 函数"的方式开关模拟器，而不是到处拼命令字符串。

本步学到的知识点：
1. subprocess.run —— 在 Python 里启动另一个程序（子进程），拿它的输出
2. encoding="gbk" —— 中文 Windows 程序的输出通常用 GBK 编码，
   不指定的话中文会变成乱码（实测过，list2 输出就是 GBK）
3. 防御式解析 —— 外部程序的输出格式可能随版本变化，
   解析失败时宁可跳过这一行并报警，也不要让整个程序崩溃
4. dataclass —— 省事的"数据类"写法，自动生成 __init__ 等方法

实测备忘（2026-08-30，D:\\LDPlayer 绿化版）：
list2 每行 7 列，逗号分隔：
   0 index          实例编号（0 开始）
   1 title          实例名称（窗口标题）
   2 top_hwnd       顶层窗口句柄，0 = 未启动
   3 bind_hwnd      绑定窗口句柄，0 = 未启动
   4 android_started  1 = 安卓系统已启动完成（窗口出现后还要再等几秒才变 1）
   5 pid            模拟器进程 PID，-1 = 未运行
   6 extra          含义未明（运行中是 2288），保留字段，不猜
"""

import os
import re
import subprocess
# 打包版是无控制台 GUI 进程（emuhive.spec console=False）：从它里面起命令行
# 子进程（ldconsole/adb/taskkill），系统会被迫给每个子进程新分配一个控制台
# 黑窗——每条命令闪一下还抢焦点，实测即「窗口一闪一闪、打字被打断」。
# CREATE_NO_WINDOW = 子进程有控制台但不显示；源码态（python main.py 自带
# 控制台、子进程继承不弹窗）加不加无区别，所以统一带上，零行为变化。
# 非 Windows 平台该常量不存在，getattr 兜 0 = 无 flag，无害。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

from core import json_store
from core.win_utils import user32   # 唯一已声明 user32 出口（本模块窗口操作用它，不再裸调）

# 雷电安装路径（第 4 步起不再写死）：
# 优先读 config.json 的 general.ld_console；留空则自动探测常见安装位置。
# 这样整个文件夹拷到别的电脑，只要雷电装在常规路径就能直接用；
# 装在非常规路径就改一次配置。
# 雷电路径动态解析：LD_CONSOLE 只是"当前生效值"的展示镜像（界面读它显示），
# 真正每次调用走 _console_path()。这样用户在功能设置页改了路径，无需重启
# 程序，下一条 ldconsole 命令就用新路径（旧版启动时快照一次，改完必须重启
# 才生效，界面上还显示着旧值，排查时极易误判）。
def _resolve_console() -> str:
    from core import config_manager  # 延迟导入，避免初始化顺序问题
    configured = config_manager.get_config().get("general.ld_console", "")
    candidates = [configured] + [
        r"D:\LDPlayer\ldconsole.exe",
        r"C:\LDPlayer\ldconsole.exe",
        r"C:\LDPlayer\LDPlayer4\ldconsole.exe",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return configured  # 一个都没找到就返回配置值，调用时会给出明确报错


def _console_path() -> str:
    """当前应该使用的 ldconsole.exe 路径（每次查配置，并回写 LD_CONSOLE）。"""
    global LD_CONSOLE
    path = _resolve_console()
    if path != LD_CONSOLE:
        LD_CONSOLE = path
    return path


LD_CONSOLE = _resolve_console()

# ldconsole 命令的统一超时（秒）。启动模拟器慢，给足；普通查询 15 秒足够
CMD_TIMEOUT = 30


@dataclass
class InstanceInfo:
    """一个模拟器实例的信息。字段含义见模块顶部 docstring。"""
    index: int
    title: str
    top_hwnd: int
    bind_hwnd: int
    android_started: int
    pid: int
    extra: int

    @property
    def running(self) -> bool:
        """实例是否在运行（pid > 0 即运行中）"""
        return self.pid > 0

    @property
    def android_ready(self) -> bool:
        """安卓系统是否已完全启动（决定能不能开始截图/点击）"""
        return self.android_started == 1


def _run(args: list[str], timeout: int = CMD_TIMEOUT) -> subprocess.CompletedProcess:
    """
    所有 ldconsole 调用的统一入口（DRY 原则：调外部程序的细节只写一遍）。

    两个 Windows 特有陷阱的防御（第 5 步实战踩坑后加固）：
    1. 不用管道捕获输出，重定向到临时文件——
       管道会被子进程的子进程继承，即使超时杀掉了主进程，
       只要孙进程还活着，读管道就永远等不到 EOF → 永久卡死。
       文件没有"EOF 等待"问题，怎么都读得到
    2. 超时后用 taskkill /T 杀整个进程树——
       普通 kill 只杀主进程，孙进程会变成孤儿继续占资源

    - encoding="gbk"：按 GBK 解码输出，中文才不乱码
    - errors="replace"：万一有解码不动的字节，用替代符顶上，不让程序崩
    """
    console = _console_path()   # 每次现查配置：改雷电路径不必重启程序
    if not console:
        # 没装/没指认雷电：空程序名直接 Popen 会得到 WinError 87「参数错误」
        # （1607 目标机实测：开机即炸穿界面线程）。换成说人话的 FileNotFoundError
        # 走既有报错通道。
        raise FileNotFoundError(
            "未找到 ldconsole.exe：请在「功能设置 → 雷电路径」指认雷电安装目录")
    out_file = tempfile.NamedTemporaryFile(delete=False, suffix=".out")
    err_file = tempfile.NamedTemporaryFile(delete=False, suffix=".err")
    out_path, err_path = out_file.name, err_file.name
    out_file.close()
    err_file.close()

    proc = None
    try:
        # 自己 open() 的句柄用 with 显式关：交给 GC 关不及时，且异常路径
        # （子进程启动失败）下也要放掉（旧版 open() 裸传，高频命令每次泄
        # 2 个文件句柄）
        with open(out_path, "wb") as out_fh, open(err_path, "wb") as err_fh:
            try:
                proc = subprocess.Popen(
                    [console] + args, stdout=out_fh, stderr=err_fh,
                    creationflags=_NO_WINDOW)
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # 杀掉整个进程树（/T = 连孙进程一起），避免孤儿进程
                if proc is not None and proc.poll() is None:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True, timeout=15,
                                   creationflags=_NO_WINDOW)
                raise
            except FileNotFoundError:
                # 把"找不到文件"翻译成人话：告诉用户去哪修
                raise FileNotFoundError(
                    f"找不到 ldconsole.exe（{console}）。"
                    f"请检查雷电安装路径，或编辑 configs/config.json 的 general.ld_console"
                )
        # 读回输出（临时文件已由上面 with 关闭，另开只读句柄）
        with open(out_path, "rb") as f:
            stdout_bytes = f.read()
        with open(err_path, "rb") as f:
            stderr_bytes = f.read()
    finally:
        # 无论成功 / 超时 / 路径配错，临时文件都要删（旧版靠 finally 保证，别丢）
        for p in (out_path, err_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    result = subprocess.CompletedProcess(
        [console] + args,
        returncode=proc.returncode,
        stdout=stdout_bytes.decode("gbk", errors="replace"),
        stderr=stderr_bytes.decode("gbk", errors="replace"),
    )
    return result


def blocked_indexes() -> set[int]:
    """
    读屏蔽名单（config 的 emulator.blocked_indexes）。原私有名 _blocked_indexes，
    account_io 等模块外也要用，转正公开。
    屏蔽 = 单实例测试模式：名单里的实例对全程序"不存在"。
    读不到配置/配置坏掉时返回空集合 = 不屏蔽任何实例（宁可全放出来，不静默藏实例）。
    """
    try:
        from core import config_manager
        raw = config_manager.get_config().get_safe("emulator.blocked_indexes", [])
        return {int(i) for i in raw}
    except Exception:
        return set()


# 兼容垫片：老调用方/冒烟打桩用的旧私有名。注意打桩要打到调用点真正解析的
# 名字上（本模块调用点与 account_io 现都走公开名 blocked_indexes）。
_blocked_indexes = blocked_indexes


# ── 实例快照缓存（"全部启动卡住"的根治点）───────────────────────────
# 病根：list2 是全程序唯一的"实例列表"来源，以前每个要数实例的地方
# （界面 3 秒周期刷新、表格、状态观察窗、每个挂机线程的 0.25s 循环、
# wait_until_ready 的 1s 轮询、点按钮的点击函数本体）都各自同步打一条
# ldconsole。模拟器开机风暴期 list2 慢到数秒且互相排队，主线程排在队尾
# → 整个窗口"未响应"。
# 药方：全程序同一时刻只允许一条 list2 在飞——后台守护线程每 1 秒刷一份
# 快照，所有调用方只读快照（零耗时），不许自己打 ldconsole。
# 读者侧兜底（没起 poller 的进程：CLI / demo / 冒烟单测）：快照过期时抢
# 非阻塞刷新锁，抢到才现打一次；抢不到就拿旧快照返回——UI 线程永远不
# 阻塞在外部命令上。冷启动（本进程第一份快照还没有）是唯一会等的地方，
# 全程序只发生一次。
# 代价：状态最多滞后 1 秒。所有轮询本来就是秒级，无感。

_SNAP_TTL = 1.0        # 快照保鲜期（秒）= poller 刷新周期：无 poller 的进程
                       # （demo/flow_test/CLI）靠读者现打驱动，滞后同样 ≤1s。
                       # 读者现打走非阻塞抢锁，高频调用者不会叠出并发 list2
_POLL_INTERVAL = 1.0   # 后台刷新周期（秒）

_snap_lock = threading.Lock()      # 只护"取/换快照引用"，绝不罩 subprocess
_refresh_lock = threading.Lock()   # 单飞闸：同一时刻最多一条 list2 在飞
_snap: list["InstanceInfo"] | None = None
_snap_ts = 0.0
_poller: threading.Thread | None = None


def _refresh_snapshot() -> list["InstanceInfo"]:
    """真打一条 list2 并换上新快照（调用方必须已持有 _refresh_lock）"""
    global _snap, _snap_ts
    data = _list_instances_raw()
    with _snap_lock:
        _snap = data
        _snap_ts = time.time()
    return data


def list_instances() -> list[InstanceInfo]:
    """
    全程序"数实例"的唯一入口：返回后台快照（poller 在跑时 1 秒一新，零耗时）。

    屏蔽名单过滤在 _list_instances_raw 里统一做（单一卡点），快照存的
    已经是放行后的列表，这里不再二道过滤。
    """
    with _snap_lock:
        snap, ts = _snap, _snap_ts
    if snap is not None and time.time() - ts < _SNAP_TTL:
        return snap
    if snap is None:
        # 冷启动：等第一份快照填好（全程序只发生一次，值得等）。
        # 打不出去（没装/没指认雷电）不许炸穿调用方线程——1607 目标机实测
        # 开机第一条 list2 就 WinError 87 把界面整个带崩：降级返回空列表，
        # 界面显示 0 实例 + 雷电路径 ⚠，poller 在后台带退避重试，指认好路径自然恢复。
        with _refresh_lock:
            if _snap is None:
                try:
                    return _refresh_snapshot()
                except OSError as e:
                    print(f"[ldplayer] 冷启动快照失败，界面按 0 实例降级："
                          f"{type(e).__name__}: {e}", file=sys.stderr)
                    return []
            return _snap
    # 有过期快照：poller 在跑 → 一律拿旧值走人，读者绝不现打。开机风暴期
    # ldconsole 慢到十几秒，界面线程（0.7s 刷新 + 1s 就绪盯梢）抢进空隙现打
    # = 消息泵停摆 = Windows 标「无响应」（实测症状，慢查询让 poller 去扛，
    # 界面顶多滞后一两拍）。只有无 poller 的进程（demo/CLI）才靠读者现打
    # 驱动，且非阻塞抢锁，抢不到也拿旧值走。
    if _poller is not None and _poller.is_alive():
        return snap
    if _refresh_lock.acquire(blocking=False):
        try:
            return _refresh_snapshot()
        finally:
            _refresh_lock.release()
    return snap


def start_instance_poller():
    """
    拉起后台实例快照线程（守护线程，每 _POLL_INTERVAL 秒一条 list2）。
    MainWindow.__init__ 调一次，全进程受益；重复调用幂等。
    不起 poller 的进程自动退回"读者过期现打"路径：功能不变，只是慢。
    """
    global _poller
    if _poller is not None and _poller.is_alive():
        return

    def _loop():
        # 连续失败计数：报错只报首次 + 此后每分钟至多一条；轮询间隔随失败
        # 退避（1s→2→4→…封顶 16s）。旧版 except: pass 全静默——ldconsole
        # 路径坏掉后每秒空转一次、界面永远显示旧状态，查无可查（审查实锤）。
        fails = 0
        last_report = 0.0
        while True:
            with _refresh_lock:
                try:
                    _refresh_snapshot()
                    fails = 0
                except Exception as e:
                    fails += 1
                    now = time.monotonic()
                    if fails == 1 or now - last_report >= 60.0:
                        last_report = now
                        print(f"[ldplayer] 实例快照轮询失败（第 {fails} 次连发）："
                              f"{type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(_POLL_INTERVAL * min(16, 2 ** (fails - 1) if fails else 1))

    _poller = threading.Thread(target=_loop, name="ld-snapshot", daemon=True)
    _poller.start()


def _list_instances_raw() -> list[InstanceInfo]:
    """
    真打一条 ldconsole list2 枚举所有实例（内部实现，外部一律走
    list_instances 读快照——直接调它等于把外部命令拖进调用线程，
    "全部启动卡死"事故就是这么来的）。它仍是全程序最基础的实例信息来源：
    多开管理、窗口截图（要句柄）、账号绑定（要 index）都靠它。
    屏蔽名单在这里统一过滤（单一卡点）：表格、批量操作、挂机启动、
    状态栏统计全部只看到放行的实例，不会漏改某处导致屏蔽失效。
    """
    result = _run(["list2"])
    instances: list[InstanceInfo] = []

    for line in result.stdout.strip().splitlines():
        parts = line.split(",")
        # 防御：列数不对说明版本变了，跳过并提示，而不是崩溃
        if len(parts) != 7:
            print(f"[警告] list2 行格式异常，跳过：{line!r}")
            continue
        try:
            instances.append(
                InstanceInfo(
                    index=int(parts[0]),
                    title=parts[1],
                    top_hwnd=int(parts[2]),
                    bind_hwnd=int(parts[3]),
                    android_started=int(parts[4]),
                    pid=int(parts[5]),
                    extra=int(parts[6]),
                )
            )
        except ValueError:
            # 防御：某列不是数字（理论上不会，但外部输出不可全信）
            print(f"[警告] list2 行解析失败，跳过：{line!r}")
            continue

    # 屏蔽过滤：名单里的实例直接从结果里消失，上层（表格/批量/挂机）根本看不见
    blocked = blocked_indexes()
    if blocked:
        instances = [i for i in instances if i.index not in blocked]

    # 母体排除：0 号是雷电多开的克隆源（用户规则 2026-09-21：只当载体不当多开）。
    # 和屏蔽名单同一个卡点一次做净——表格/状态窗/工作台下拉/批量操作/补位
    # 全链路从 1 开始，上层根本不知道有 0 号。克隆功能不受影响：
    # clone_instance(0) 直接对 ldconsole 下令，不走这份列表。
    instances = [i for i in instances if i.index != 0]

    return instances


def refresh_instances() -> list[InstanceInfo]:
    """
    强制现打一条 list2 刷新快照并返回（阻塞）。

    只给后台线程用在「判定错了代价很大」的节点（如 quit_now 决定要不要
    taskkill 前拿即刻真值）。主线程禁止调用——那正是"全部启动卡死"的病根。
    """
    with _refresh_lock:
        return _refresh_snapshot()


def get_instance(index: int) -> InstanceInfo | None:
    """按编号取单个实例（读快照缓存），不存在返回 None"""
    for inst in list_instances():
        if inst.index == index:
            return inst
    return None


def is_instance_alive(index: int, known_hwnd: int = 0) -> bool:
    """模拟器是否真还开着——快照 running 优先，抽风时凭活窗口句柄兜底。

    病根（1607 移植机实测）：list2 在开机/adb 高频期偶尔返回空列表或 pid 列瞬读
    成 0，快照的 running(=pid>0) 随之误判"已关闭"，挂机/测试无辜中止——可用户
    屏幕上模拟器窗口明明开着。药方：快照说活着就活；快照说没了/没 running，再
    拿"当前正在截的窗口句柄"和"快照里那台的句柄"问 IsWindow——句柄还在就是
    快照抽风，不算关。真关时下次正常 list2 会把 running 纠正过来，句柄也已销毁，
    两路都 False，仍会正确停止。known_hwnd 传 worker 手上那个 ctx.hwnd（最可信
    的活句柄来源，因为它正是我们每拍在截的窗口）。"""
    inst = get_instance(index)
    if inst is not None and inst.running:
        return True
    from core import win_utils
    if inst is not None and win_utils.is_alive(inst.top_hwnd):
        return True
    return win_utils.is_alive(known_hwnd)


def launch(index: int) -> bool:
    """
    启动实例（已启动时再调也安全，返回 True）。
    屏蔽名单里的实例拒绝启动——界面虽然看不见它，
    这里再拦一道，防止任何代码路径（含命令行验收）把它拉起来。
    """
    if index in blocked_indexes():
        print(f"[提示] 实例 {index} 在屏蔽名单中（config: emulator.blocked_indexes），拒绝启动")
        return False
    clear_size_cache(index)   # 新一轮会话：分辨率可能被改过，缓存作废
    result = _run(["launch", "--index", str(index)])
    return result.returncode == 0


def quit(index: int) -> bool:
    """关闭实例"""
    result = _run(["quit", "--index", str(index)])
    return result.returncode == 0


def clone_instance(from_index: int = 0) -> None:
    """
    克隆一个实例（ldconsole copy --from <idx>），只发命令、不判成败。

    实测（2026-09-21，D:\\LDPlayer 绿化版）两个坑，逼出这个"哑函数"：
    1. copy 不接受 --name：help 里写了 [--name]，实测一加就打印 usage 直接罢工，
       所以让雷电自己按「雷电模拟器-N」命名，不传名字
    2. 成功也返回非 0：真建出实例后退出码是 6，不是 0——returncode 根本不可信
    成败唯一可信的判据是"实例数有没有涨"，交给调用方 set_instance_count 数出来。
    克隆 = 整块虚拟机磁盘复制，慢，超时给到 5 分钟；只许后台线程调。
    """
    _run(["copy", "--from", str(from_index)], timeout=300)


def _quit_batch(idxs, graceful_wait: float = 3.0, kill_wait: float = 10.0):
    """
    并行关闭一批实例的内部实现（quit_now / quit_indexes_now / quit_all 三路共用，
    以前单机/成批各写一遍同流水线，~160 行双份）。

    做法（单机多台只是"一次全发、统一确认"）：
    1. 每台一条 quit 命令"脱离"发出（Popen 不等待）：quit() 走 _run 等 ldconsole
       回执，对"正在启动中"的实例会挂住拖满 30s 超时——它想等多久是它的事，
       正常关闭会保存虚拟机状态（下次开机快），这条路走得通就尽量走通
    2. 轮询 graceful_wait 秒（读快照、零外部命令），确认进程真退了
    3. 还没退：taskkill /F /T 杀进程树（硬关不保存状态，下次冷启动稍慢，
       换"点了就关"）；再确认 kill_wait 秒
    4. finally：detach 出去的 quit 命令还没退 = 已无用，连它自己的进程树一起收，
       防反复开关攒僵尸 ldconsole；devnull 句柄所有出口都关

    返回 (仍未关掉的序号集合, 被硬杀的序号列表, 致命错误消息或 None)。
    杀前拿即刻真值（refresh_instances）：快照最多滞后 1s，pid 刚退出又被系统
    复用时拿旧 pid taskkill 会杀错进程。轮询阻塞，只许后台线程调。
    """
    idxs = sorted({int(i) for i in idxs})
    if not idxs:
        return set(), [], None
    devnull = open(os.devnull, "wb")
    procs: dict[int, subprocess.Popen] = {}

    def _live(sub: set[int]) -> set[int]:
        # 轮询只读快照（≤1s 滞后无妨：等得久一点而已）；真值留给杀前那一下
        snap = {i.index: i for i in list_instances()}
        return {i for i in sub
                if snap.get(i) is not None and snap[i].pid > 0}

    pending: set[int] = set(idxs)
    killed: list[int] = []
    try:
        for idx in idxs:
            try:
                procs[idx] = subprocess.Popen(
                    [_console_path(), "quit", "--index", str(idx)],
                    stdout=devnull, stderr=devnull,
                    creationflags=_NO_WINDOW)
            except FileNotFoundError:
                return set(idxs), [], (
                    f"找不到 ldconsole.exe（{_console_path()}），"
                    "请检查雷电安装路径或 config 的 general.ld_console")

        deadline = time.time() + graceful_wait
        while pending and time.time() < deadline:
            time.sleep(0.5)
            pending = _live(pending)

        if pending:
            # 拿即刻真值再决定杀进程（防 pid 复用杀错，见 docstring）
            snap = {i.index: i for i in refresh_instances()}
            for i in sorted(pending):
                inst = snap.get(i)
                if inst is None or inst.pid <= 0:
                    continue
                try:
                    # DEVNULL 而非管道：与约束1"不用管道捕获输出"的口径同构
                    # （taskkill 输出小到撞不上管道死锁，但别留破口）
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(inst.pid)],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=15,
                                   creationflags=_NO_WINDOW)
                    killed.append(i)
                except Exception:
                    pass   # 杀失败（进程刚好自己退了等）继续轮询确认
            deadline = time.time() + kill_wait
            while pending and time.time() < deadline:
                time.sleep(0.5)
                pending = _live(pending)
    finally:
        # 收摊：quit 命令早退则 no-op；devnull 句柄在所有出口都必须关
        # （旧版"等不到关闭"的超时路径漏关过一次）
        for proc in procs.values():
            try:
                if proc.poll() is None:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=15,
                                   creationflags=_NO_WINDOW)
            except Exception:
                pass
        devnull.close()
    return pending, killed, None


def quit_now(index: int, graceful_wait: float = 3.0, kill_wait: float = 10.0) -> bool:
    """
    关闭单个实例（"随时能关"版）：单机也走 _quit_batch 批量流水线，
    与"全部关闭"同一实现（旧版两套同逻辑各写各的，改一处忘一处的温床）。
    最坏 graceful_wait + kill_wait 秒必出结果；ldconsole 找不到抛 FileNotFoundError。
    """
    pending, _killed, error = _quit_batch([index], graceful_wait, kill_wait)
    if error:
        raise FileNotFoundError(error)
    return not pending


def quit_indexes_now(indexes, graceful_wait: float = 3.0,
                     kill_wait: float = 10.0) -> str:
    """
    并行关闭多台实例（"全部关闭"专用）：所有 quit 一次性脱手发出、统一确认，
    graceful 期不退的逐个补刀。总耗时 ≈ 最慢那台（旧版逐台排队各独等 3 秒起步）。
    实现见 _quit_batch。返回汇总字符串供批次日志直显。轮询阻塞，只许后台线程调。
    """
    idxs = sorted({int(i) for i in indexes})
    if not idxs:
        return "没有实例需要关闭"
    pending, killed, error = _quit_batch(idxs, graceful_wait, kill_wait)
    if error:
        return error
    parts = [f"已关闭 {len(idxs) - len(pending)} 台"]
    if killed:
        parts.append(f"硬杀 {len(killed)} 台 {killed}")
    if pending:
        parts.append(f"仍关不掉 {sorted(pending)}")
    return "，".join(parts)


def reboot(index: int) -> bool:
    """重启实例（卡死恢复时用）"""
    clear_size_cache(index)   # 重启后分辨率可能被改过，缓存作废
    result = _run(["reboot", "--index", str(index)])
    return result.returncode == 0


def quit_all(graceful_wait: float = 3.0, kill_wait: float = 10.0) -> bool:
    """
    一键关闭全部实例：与 quit_now/quit_indexes_now 同走 _quit_batch 三路共用。
    旧版走 ldconsole quitall 等回执——开机风暴期能挂满 30s 超时，且它没有
    "不退补刀"兜底。⚠ 关的是快照里可见的运行中实例：0 号母体与屏蔽实例不在
    快照里，不再被 quitall 顺带关掉（母体只当克隆载体、全程序不可见，与
    launch/表格/批量口径一致）。
    """
    idxs = [i.index for i in list_instances() if i.running]
    if not idxs:
        return True
    pending, _killed, error = _quit_batch(idxs, graceful_wait, kill_wait)
    return not error and not pending


def remove_instance(index: int) -> None:
    """
    删除实例（ldconsole remove --index N），只发命令、不判成败——
    同 clone_instance：退出码不可信，成败由调用方数"台数有没有少"。
    ⚠ 不可逆：连虚拟机磁盘一起抹。运行中的实例先 quit 再删（remove 对
    运行中实例行为不可靠）。只许后台线程调。
    """
    _run(["remove", "--index", str(index)], timeout=120)


def set_instance_count(target: int, from_index: int = 0,
                       on_progress=None) -> tuple[int, list[int], str | None]:
    """
    把实例总数"设置"到 target 台：少了从 from_index（母体）克隆补齐，
    多了删掉编号最大的多余台。返回 (新建台数, 已删实例号列表, 错误消息或 None)。

    删除护栏（按危险度层层设防）：
    1. 台数一律走 refresh_instances() 的"可用台数"口径——0 号母体已在
       _list_instances_raw 单一卡点排除，永远进不了删除名单（它是克隆源，删了整套报废）
    2. 只删编号最大的：保留 1~target，删的是尾部克隆机（与账号序号直通口径对齐）
    3. 运行中的先 quit_now 再删（remove 对运行中实例行为不可靠）
    4. 每删/每建一台现打 list2 复核台数变化，没变就如实报错停下——
       与克隆"只认台数涨没涨"同一条验证纪律，也天然杜绝无限循环
    on_progress(文字) 逐台报进度，在批次线程执行（调用方自己保证跨线程安全）。
    ⚠ 调用前必须让用户确认删除名单——remove 不可恢复（红线操作）。
    """
    made = 0
    removed: list[int] = []
    while True:
        insts = refresh_instances()            # 已排除母体 0 = 可用台数
        n = len(insts)
        if n == target:
            return made, removed, None
        if n < target:
            # ── 补台：克隆 ──
            if on_progress:
                on_progress(f"正在克隆第 {made + 1} 台（当前 {n}/{target}，"
                            f"每台几十秒到几分钟，请稍候）…")
            clone_instance(from_index)
            after = len(refresh_instances())
            if after <= n:
                return made, removed, f"copy 未新建实例（源实例 {from_index}，雷电可能异常）"
            made += 1
            if on_progress:
                on_progress(f"已建到 {after}/{target} 台")
            continue
        # ── 收台：删编号最大的多余台 ──
        victim = max(insts, key=lambda i: i.index)
        if on_progress:
            on_progress(f"正在删除实例 {victim.index}「{victim.title}」"
                        f"（剩余 {n}→{target}，不可恢复）…")
        if victim.running:
            quit_now(victim.index)             # 先关掉再删（有界等待必出结果）
        remove_instance(victim.index)
        after = len(refresh_instances())
        if after >= n:
            return made, removed, (f"remove 未删除实例 {victim.index}"
                                   f"（雷电可能异常，请到多开器确认）")
        removed.append(victim.index)
        if on_progress:
            on_progress(f"已删到 {after}/{target} 台")


def runapp(index: int, packagename: str) -> bool:
    """启动实例内的某个 APP（游戏包名）"""
    result = _run(["runapp", "--index", str(index), "--packagename", packagename])
    return result.returncode == 0


# ── 安卓分辨率设定（多开点歪的根治第一步）──────────────────────────────
# 为什么要有这个：脚本识别画面时把截图统一缩到基准分辨率 540x960，
# 找到的点击坐标也在这个空间里，然后直接 input tap 发给安卓。
# 安卓真实分辨率 == 540x960 时两者重合，点得准；不等于就整体偏移
# （例如安卓是 720x1280，脚本点 (270,480)，实际该点 (360,640)）→ 全点到左上角一片。
# 所以多开前必须把每个实例的分辨率拍平成同一个值。
#
# ldconsole modify 的生效时机（实测行为）：
#   实例已停止 → 写进实例配置，下次 launch 直接生效（不额外花时间）
#   实例运行中 → 需要 reboot 才生效
# 因此「启动前统一」是最省事的时机：那时实例还没起，改完即生效。

def set_resolution(index: int, w: int, h: int, dpi: int = 240) -> bool:
    """
    设定实例的安卓分辨率为 w x h（dpi 密度）。
    返回 ldconsole 是否执行成功；调用方需自己判断是否要 reboot 才生效。
    改完清掉本实例的分辨率缓存（android_size 的缓存作废了）。
    """
    clear_size_cache(index)
    result = _run(["modify", "--index", str(index),
                   "--resolution", f"{int(w)},{int(h)},{int(dpi)}"])
    return result.returncode == 0


# get_resolution 死别名已删（2026-10-09）：全项目零调用，直接用 android_size。

# 实例停止时读「配置里写死的分辨率」（实测 2026-09-10，雷电4 绿化版 D:\LDPlayer，旧注误记"雷电9"）：
#   路径 vms/config/leidian<index>.config，JSON 格式，UTF-8
#   字段 advancedSettings.resolution = {"width": 540, "height": 960}
#        advancedSettings.resolutionDpi = 240
# 实测 ldconsole modify --resolution 就是写这个文件（modify 后该文件 mtime 变化）。
# 所以停止的实例也能查出「下次启动会是多少」，界面不用显示「待确认」。
#
# 防御：命名规律 leidian<index> 是绿化版实测值，安装版/新版本可能不同。
# 任何一步读不到都返回 None，调用方回退到「设定后下次启动生效」的措辞。
def get_config_resolution(index: int) -> tuple[int, int, int] | None:
    """
    读实例配置文件里写死的分辨率 (宽, 高, dpi)；读不到返回 None。
    不依赖实例是否运行——这是它相对 adb shell wm size 的唯一优势。
    """
    # 走 _console_path() 现查：LD_CONSOLE 只是展示镜像，配置页改过路径后
    # 拿旧镜像的 dirname 会读错安装目录（读不到→静默 None，界面显示"待确认"）
    cfg_path = os.path.join(os.path.dirname(_console_path()),
                            "vms", "config", f"leidian{index}.config")
    if not os.path.exists(cfg_path):
        return None
    data = json_store.load_or_default(cfg_path, {})
    try:
        res = data.get("advancedSettings.resolution")
        if not isinstance(res, dict):
            return None
        w, h = int(res["width"]), int(res["height"])
        dpi = int(data.get("advancedSettings.resolutionDpi", 240))
        if w <= 0 or h <= 0:
            return None
        return (w, h, dpi)
    except Exception:
        return None


def current_resolution(index: int) -> tuple[int, int, int] | None:
    """
    取实例「当前该是多少分辨率」，返回 (宽, 高, dpi) 或 None。
    运行中优先用 adb 实测值（生效中的真值，dpi 配置文件取），
    停止时用配置文件值（下次启动会生效的值）。
    这是界面表格显示分辨率的统一入口。

    用 force=True 绕过缓存：调用方都是「一次性读现状」——对话框打开时刷表格、
    ensure_resolution 判定要不要改。这两处读到过期值会直接导致误判
    （比如用户在雷电界面里手改过分辨率，缓存还是旧值，ensure 就以为已符合而跳过）。
    缓存是给 worker 每 0.25 秒轮询用的，不是给这里用的。
    代价是每实例 ~50ms，十个实例开对话框慢半秒，换正确性值得。

    ⚠ 停止台先查快照、直接读配置，绝不试 adb：安卓没开机，adb 命令没有
    设备可路由，会干等到 30 秒超时被 taskkill 才落到"返回 None→读配置"
    这条兜底路——实测「全部启动」前统一分辨率，一台停止机白烧 30.1 秒
    （2026-09-26 日志"1 台：一致 1 台 (30.1s)"）。兜底是防 adb 意外，
    不该是停止台的常规路径。
    """
    inst = get_instance(index)
    if inst is not None and not inst.running:
        return get_config_resolution(index)
    live = android_size(index, force=True)
    if live is not None:
        cfg = get_config_resolution(index)
        return (live[0], live[1], cfg[2] if cfg else 240)
    return get_config_resolution(index)


def ensure_resolution(index: int, w: int, h: int, dpi: int = 240) -> str:
    """
    确保实例分辨率等于目标值，返回状态字符串（供批次日志直接显示）：
      "已是 {w}x{h}，跳过"      —— 已符合，没做任何事（最常见，启动批次里几乎都走这条）
      "已设定 {w}x{h}（下次启动生效）" —— 停止状态下改好了
      "已设定 {w}x{h}（运行中，需重启生效）" —— 运行中改的，调用方决定是否 reboot
      "设定失败"                —— ldconsole 返回非 0

    幂等：运行中用 adb 实测值、停止时用配置文件值判当前分辨率，
    已等于目标值就跳过，不重复 modify（启动批次里每个实例都会调一次，
    第二次起全是「跳过」，几乎零开销——实测读配置 <1ms、adb 查询 ~50ms）。
    """
    cur = current_resolution(index)
    if cur is not None and (cur[0], cur[1]) == (int(w), int(h)):
        return f"已是 {w}x{h}，跳过"

    inst = get_instance(index)
    running = inst is not None and inst.running

    if not set_resolution(index, w, h, dpi):
        return "设定失败"
    if running:
        return f"已设定 {w}x{h}（运行中，需重启生效）"
    return f"已设定 {w}x{h}（下次启动生效）"


def ensure_resolutions_parallel(indexes, w: int, h: int,
                                dpi: int = 240) -> str:
    """
    成批并发地跑 ensure_resolution（"全部启动"的分辨率阶段专用）。

    串行的慢点只有一个：运行中的台要用 adb 实测当前分辨率（每台 ~50ms，
    开机风暴期能放大到秒级），N 台排队就把"下达 launch"整体往后拖。
    停止台是读配置文件（<1ms），并行与否差别不大，一起丢池子里省事。
    不同实例改的是各自的配置文件，无共享写冲突，可放心并发。
    """
    from concurrent.futures import ThreadPoolExecutor

    idxs = sorted({int(i) for i in indexes})
    if not idxs:
        return "没有实例"
    with ThreadPoolExecutor(max_workers=min(8, len(idxs))) as ex:
        results = list(ex.map(lambda i: (i, ensure_resolution(i, w, h, dpi)),
                              idxs))
    skip = [i for i, r in results if r.startswith("已是")]
    fail = [i for i, r in results if r.startswith("设定失败")]
    other = len(idxs) - len(skip) - len(fail)
    tag = f"{len(idxs)} 台：一致 {len(skip)} 台"
    if other:
        tag += f"，已设定 {other} 台"
    if fail:
        tag += f"，设定失败 {fail} 台"
    return tag


# ── adb 命令转发（第 3 步新增）────────────────────────────────────────
# 实测发现：ldconsole adb --index N --command "..." 会自动路由到正确实例，
# 完全不用自己管理 adb 端口/连接（直接连 5555 端口反而显示 offline）。

def adb_command(index: int, command: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """
    通过 ldconsole 把任意 adb 命令发给指定实例。
    例：adb_command(0, "shell input tap 100 200")
    走 _run（临时文件重定向 + 进程树超时击杀），与普通命令同样防御。

    ⚠ 返回码必须查（稳定性审计 ②）：旧版忽略 rc，adb 通道死了（device
    offline 等）tap 静默"成功"，流程空转到下个等待才撞墙。非 0 即 raise，
    由 worker 逐拍兜底降级成"本拍失败重试"；android_size 等读类调用方
    自带 try/except，不受影响。
    """
    result = _run(["adb", "--index", str(index), "--command", command], timeout=timeout)
    if result.returncode != 0:
        err = ""
        for s in (result.stderr, result.stdout):
            if s:
                err += s if isinstance(s, str) else s.decode("utf-8", "replace")
        raise RuntimeError(
            f"adb 命令失败(rc={result.returncode}) [{command}] → {err.strip()[:120]}")
    return result


def adb_shell(index: int, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """在实例的安卓系统里执行 shell 命令（adb_command 加个 shell 前缀的糖）"""
    return adb_command(index, f"shell {cmd}", timeout)


# ── 原生 adb 直连（2026-10-11：挂机取帧主路改走这条，方案 A/B）──────────────
# 为什么直连：ldconsole adb --index N 每调用多 fork 一个 ldconsole.exe（实测
# 每条 +40ms，取帧是每拍热路径，20~30 台时这笔开销叠得可观）。取帧只读屏幕、
# 不需要 ldconsole 的实例路由，直接用雷电自带 adb.exe -s <serial> 更省。
# 序列号必须**运行时解析**：雷电一实例在 `adb devices` 里有 emulator-(5554+2i)
# 与 127.0.0.1:(5555+2i) 两个名字，谁在线随重启时机漂移，写死任一边都会复发
# "device offline"（2026-10-10 实测教训，见 resolve_serial）。这几个函数原先
# 长在已删除的 scrcpy 后端模块里（scrcpy 私有），取帧主路转 adb 后升格为公共
# 原语——现挂机取帧是 adb 单次快照，这几个是它的取帧底座。

def find_adb() -> str | None:
    """定位 adb.exe：优先 ldconsole.exe 同目录（雷电绿色版自带，移植机必有），
    其次 PATH。找不到返回 None（调用方自动回落/空帧，绝不抛）。"""
    import shutil
    try:
        console = _console_path()
        if console:
            cand = os.path.join(os.path.dirname(console), "adb.exe")
            if os.path.exists(cand):
                return cand
    except Exception:
        pass
    return shutil.which("adb")


def _device_states(adb: str) -> dict[str, str]:
    """`adb devices` → {序列号: 状态}。"""
    r = subprocess.run([adb, "devices"], capture_output=True, text=True,
                       timeout=10, creationflags=_NO_WINDOW)
    st = {}
    for line in (r.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            st[parts[0]] = parts[1]
    return st


def resolve_serial(index: int, adb: str | None = None) -> str | None:
    """挑该实例当前真正在线的 adb 序列号，挑不出返回 None。
    2026-10-10 实测教训：雷电4 一个实例在 `adb devices` 里有两个名字
    （自动发现的 emulator-(5554+2i) 与手连的 127.0.0.1:(5555+2i)），
    哪个在线**随重启时机漂移**：开机后曾见 emulator-5556=device 而
    127.0.0.1:5557=offline（僵尸记录，拿它起 server 秒退
    "error: device offline"）；模拟器重启后反过来。写死任何一边都会复发，
    必须运行时解析：两个名字谁 device 用谁 → 都不在线就 disconnect+connect
    修 TCP 名再查 → 仍没有才判失败。探测本身炸（桩 adb/老 adb）回落公式名，
    不拦启动。"""
    adb = adb or find_adb()
    if not adb:
        return None
    emu, tcp = f"emulator-{5554 + 2 * index}", f"127.0.0.1:{5555 + 2 * index}"
    try:
        st = _device_states(adb)
        if st.get(emu) == "device":
            return emu
        if st.get(tcp) == "device":
            return tcp
        for args in (["disconnect", tcp], ["connect", tcp]):
            subprocess.run([adb] + args, capture_output=True, text=True,
                           timeout=10, creationflags=_NO_WINDOW)
        st = _device_states(adb)
        if st.get(tcp) == "device":
            return tcp
        if st.get(emu) == "device":
            return emu
        return None
    except Exception:
        return emu   # 桩环境/adb 太老查不动：回落公式名赌一把


def adb_exec(args: list[str], timeout: int = 15, serial: str | None = None):
    """直连 adb.exe 执行一条命令，原样返回 CompletedProcess（stdout 是 **bytes**，
    二进制安全——取帧要把 PNG 原始字节捞回来，绝不能被 GBK 文本解码糟蹋）。
    serial 非空自动补 -s <serial>。adb 不在/超时抛异常，由调用方按各自口径兜
    （取帧侧一律吞成 None）。"""
    adb = find_adb()
    if not adb:
        raise FileNotFoundError("未找到 adb.exe（雷电目录 / PATH 都没有）")
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          creationflags=_NO_WINDOW)


# ── 安卓真实分辨率（归一化坐标的地基）─────────────────────────────────
# 实测 2026-09-10，雷电4 绿化版 D:\LDPlayer（dnplayer 4.0.83.0，旧注误记"雷电9"），实例分辨率 540x960：
#   adb shell wm size  →  "Physical size: 540x960\r\r\n"
# 用户手动改过分辨率时会多一行 "Override size: WxH"，此时生效的是 Override。
#
# 为什么要它：窗口客户区尺寸 ≠ 安卓坐标空间尺寸。窗口拖大 2 倍后客户区变
# 1164x1992，但安卓坐标系仍是 540x960 —— 截图要缩放对齐，点击坐标要用安卓真实值。
# 录制回放存 [0,1] 归一化比例、回放时乘以本函数结果，窗口怎么改都不会点歪。

_size_cache: dict[int, tuple[int, int]] = {}


def android_size(index: int, force: bool = False) -> tuple[int, int] | None:
    """
    取实例的安卓真实分辨率 (宽, 高)；查不到返回 None。

    结果按实例缓存（分辨率只在用户改设置时才变，每轮 worker 循环查一次 adb
    是纯浪费）。force=True 跳过缓存重查（实例重启/改分辨率后调用）。
    """
    if not force and index in _size_cache:
        return _size_cache[index]

    try:
        result = adb_shell(index, "wm size")
    except (subprocess.TimeoutExpired, FileNotFoundError, RuntimeError):
        # RuntimeError 必须接住：adb_command 对非 0 返回码抛它（稳定性审计②），
        # adb 通道死（device offline 等）时若不接，会打穿本函数
        # "取不到返回 None"的承诺 → current_resolution/ensure_resolution/表格刷新连坐炸
        return None

    # 优先 Override（用户改过的分辨率），否则 Physical（出厂值）
    for key in ("Override", "Physical"):
        m = re.search(rf"{key} size:\s*(\d+)x(\d+)", result.stdout)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            _size_cache[index] = size
            return size
    return None


def clear_size_cache(index: int | None = None):
    """清分辨率缓存；index=None 清全部（实例重启后调用）"""
    if index is None:
        _size_cache.clear()
    else:
        _size_cache.pop(index, None)


def _cascade_pos(index: int, win_w: int, win_h: int,
                 wa_left: int, wa_top: int, wa_right: int, wa_bottom: int):
    """级联布局纯计算：返回窗口左上角 (x, y)。与 API 分开便于自测。"""
    step_x = max(1, win_w // 4)
    step_y = max(1, win_h // 4)
    cols = max(1, (wa_right - wa_left - win_w) // step_x + 1)
    col, row = index % cols, index // cols
    x = wa_left + col * step_x
    y = min(wa_top + row * step_y, wa_bottom - 40)   # 超底也保证标题栏可见
    return x, y


def cascade_window(index: int) -> bool:
    """
    多开排窗：把模拟器窗口级联摆到错位可辨的位置（经典 Windows 层叠排法）。

    布局：从工作区（全屏减任务栏，SPI_GETWORKAREA）左上角起，横竖步长都是
    窗口尺寸 / 4，相邻窗口重叠 3/4、各露出 1/4 竖条 + 标题栏——有辨识度
    又不缩小画面。列数由工作区宽和窗宽现算，位置只取决于
    「序号 + 当前窗口尺寸 + 工作区」，所以多实例并行逐个就绪、各自摆位
    互不干扰，不需要知道一共开了几台。

    台数多超出工作区底边时钳制到只剩标题栏可见（窗口近满屏高，
    第二行必然压底，这是屏幕物理极限，不是 bug）。

    实现：Windows API（ctypes 直调 user32.dll），GetWindowRect 量当前
    窗口尺寸 → 算格 → MoveWindow 挪过去（尺寸不变）。
    句柄截断防线（64 位句柄默认按 32 位传参会截断）由 core/win_utils.py 的
    签名声明统一保证，本模块不再重复声明。

    失败不抛异常只返回 False——挪窗口是锦上添花，不该弄死启动流程。
    """
    import ctypes
    from ctypes import wintypes

    inst = get_instance(index)
    if inst is None or inst.top_hwnd <= 0:
        return False

    hwnd = wintypes.HWND(inst.top_hwnd)
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    win_w = rect.right - rect.left
    win_h = rect.bottom - rect.top
    if win_w <= 0 or win_h <= 0:
        return False

    wa = wintypes.RECT()   # 工作区：全屏去掉任务栏（SPI_GETWORKAREA=0x30，别写成 0x1E）
    if not user32.SystemParametersInfoW(0x30, 0, ctypes.byref(wa), 0):
        return False
    if wa.right - wa.left <= 0 or wa.bottom - wa.top <= 0:
        return False       # 拿到空矩形时宁可不排，也别把窗口全堆原点

    x, y = _cascade_pos(index, win_w, win_h, wa.left, wa.top, wa.right, wa.bottom)
    return bool(user32.MoveWindow(hwnd, x, y, win_w, win_h, True))


def restore_native_window(index: int) -> bool:
    """
    把模拟器窗口尺寸校正回 1:1（安卓画面在窗口里不放大不缩小）。

    为什么必须做：GDI 抓的是 GPU 合成后的窗口画面。窗口被拖大后，安卓画面
    先被雷电放大渲染、再被截下来，缩回基准分辨率时等于多走一轮重采样 ——
    实测 2026-09-10（登录界面，取【静态区域】做模板，排除动画干扰）：
        粗高对比图形（版本号文字/公告图标）  1.5x、2x 下仍 0.96~0.99，看不出损失
        细小内容（法律声明小字）   原生 1.00 → 1.5x 0.31   ← 低于 0.85 阈值
        小图形（16+ 角标）         原生 1.00 → 1.5x 0.51   ← 低于 0.85 阈值
    即损失"按细节分级"：粗图案扛得住，小字/细线条必糊。采集模板时无法预知
    某个模板是不是细内容，所以唯一可靠解法是别让它放大。
    损失发生在雷电放大渲染那一步，截图层换缩小插值算法救不回来。

    注意：窗口被拖大时【粗图案模板仍能匹配】，所以"识别没全瞎"不等于"没损失"，
    别拿粗图案的分数当缩放无害的证据（本条实测踩过这个坑）。

    做法：误差反馈，不用算边框宽度。每轮量出 RenderWindow 当前尺寸，与安卓
    真实分辨率差多少就把窗口调多少（render 比目标宽 20px，窗口就窄 20px），
    直到 render 正好等于安卓分辨率。

    为什么不按「安卓分辨率 + 边框宽度」一次算到位：实测雷电的边框宽度会随
    当前缩放状态变化（原生 582x996 时边框 42px，拖大到 1164x1992 时变 64px），
    拿「拖坏态」的边框量去反算必然算宽，且每轮基准都在变，窗口宽度会一路漂移
    （实测 736→622→692→824→605）。反馈法只认 render 的实际尺寸，与边框无关。

    失败不抛异常只返回 False（窗口尺寸是质量问题，不该弄死启动流程）。
    """
    import ctypes
    from ctypes import wintypes

    from core import capture as _capture   # 延迟导入，避免模块初始化环

    inst = get_instance(index)
    if inst is None or inst.top_hwnd <= 0:
        return False

    size = android_size(index)
    if size is None:
        return False

    # user32 签名声明收口在 core/win_utils.py，本函数不再重复
    hwnd = wintypes.HWND(inst.top_hwnd)
    # 最小化状态下窗口坐标是 (-32000,-32000)，MoveWindow 一律失效（实测踩坑），
    # 先还原再调尺寸，否则后面全部白做
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        time.sleep(1.0)

    for _ in range(6):
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        win_w, win_h = rect.right - rect.left, rect.bottom - rect.top
        if win_w <= 0 or win_h <= 0:
            return False

        geo = _capture.find_render_window(inst.top_hwnd)
        if geo is None:
            return False
        dw = size[0] - geo["width"]      # >0 = 画面还要变大，窗口跟着变大
        dh = size[1] - geo["height"]
        if dw == 0 and dh == 0:
            return True                  # 已 1:1，达标
        if not user32.MoveWindow(hwnd, rect.left, rect.top, win_w + dw, win_h + dh, True):
            return False
        time.sleep(0.6)    # 等雷电按新尺寸重排子窗口，否则下一轮量到的还是旧几何
    return False           # 6 轮没收敛（异常布局），交给上层的截图兜底


def wait_until_ready(index: int, timeout: int = 90, stop_check=None) -> bool:
    """
    启动后轮询等待安卓就绪。

    实测：窗口出现约 5 秒后 android_started 才变 1，
    所以"launch 成功"≠"可以截图点击"，要等这一下。
    轮询间隔 3 秒，总等待不超过 timeout 秒（正常启动 10-20 秒，90 秒已很宽裕）。

    就绪后自动级联排窗（cascade_window，多开错位可辨），
    挪失败不影响返回值——窗口位置是体验问题，不是启动成败问题。

    两个提前结束机制（第 5 步踩坑后加）：
    1. 死亡检测：等待期间发现实例"曾经活过、现在死了"（被手动关掉/启动失败），
       立刻停止等待——不傻等到超时
    2. stop_check：可选的回调，返回 True 就提前停止（界面"取消批次"用）
    """
    deadline = time.time() + timeout
    seen_alive = False
    while time.time() < deadline:
        if stop_check is not None and stop_check():
            return False
        inst = get_instance(index)
        if inst is not None and inst.android_ready:
            # 先校正 1:1 尺寸再排窗：级联布局按当前窗宽算格子，
            # 反过来先排再改尺寸，窗口会跑到屏幕外
            restore_native_window(index)
            cascade_window(index)
            return True
        # "活着"的判据用窗口句柄而不是 pid：被手动叉掉时窗口立刻消失，
        # 但进程可能残留一会儿——对脚本来说窗口没了就等于死了（截图点击都靠窗口）
        alive = inst is not None and inst.top_hwnd > 0
        if alive:
            seen_alive = True
        elif seen_alive:
            # 曾经活过又没了：被关闭或启动失败，别再干等
            return False
        # 轮询间隔 1 秒：窗口从出现到被叉掉可能只有 1-2 秒，
        # 3 秒采样会漏掉这么短的生命周期（实测踩坑）
        time.sleep(1)
    return False


if __name__ == "__main__":
    # 命令行验收入口：python -m core.ldplayer list / start 0 / quit 0 / quitall
    if len(sys.argv) < 2:
        print("用法：python -m core.ldplayer <list|start|quit|reboot|quitall|size|fixsize|res|setres|ensureres|setcount> [index]")
        sys.exit(1)

    cmd = sys.argv[1]
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    if cmd == "list":
        for inst in list_instances():
            status = "运行中" if inst.running else "已停止"
            ready = "安卓就绪" if inst.android_ready else "启动中/未就绪"
            print(
                f"[{inst.index}] {inst.title}  句柄={inst.top_hwnd}  pid={inst.pid}"
                f"  {status}  {ready}"
            )
    elif cmd == "start":
        print("launch:", launch(idx))
        print("等待安卓就绪...", "OK" if wait_until_ready(idx) else "超时！")
    elif cmd == "quit":
        print("quit:", quit(idx))
    elif cmd == "reboot":
        print("reboot:", reboot(idx))
    elif cmd == "quitall":
        print("quitall:", quit_all())
    elif cmd == "size":
        # 安卓真实分辨率 + 窗口/Render 几何，排查缩放问题用
        from core import capture
        print("android_size:", android_size(idx, force=True))
        inst = get_instance(idx)
        if inst is not None and inst.top_hwnd > 0:
            print("render_window:", capture.find_render_window(inst.top_hwnd))
        else:
            print("render_window: 实例未运行")
    elif cmd == "fixsize":
        print("restore_native_window:", restore_native_window(idx))
    elif cmd == "cascade":
        print("cascade_window:", cascade_window(idx))
    elif cmd == "res":
        # 读当前生效的安卓分辨率（实例必须运行中，停止时 adb 连不上）
        print("android_size:", android_size(idx, force=True))
    elif cmd == "setres":
        # 设定分辨率：python -m core.ldplayer setres <index> <宽> <高> [dpi]
        if len(sys.argv) < 5:
            print("用法：python -m core.ldplayer setres <index> <宽> <高> [dpi]")
            sys.exit(1)
        w, h = int(sys.argv[3]), int(sys.argv[4])
        dpi = int(sys.argv[5]) if len(sys.argv) > 5 else 240
        print("ensure_resolution:", ensure_resolution(idx, w, h, dpi))
    elif cmd == "ensureres":
        # 按配置统一所有实例分辨率（启动批次的同款动作，验收用）
        from core import config_manager
        cfg = config_manager.get_config()
        w = int(cfg.get("emulator.resolution_w", 540))
        h = int(cfg.get("emulator.resolution_h", 960))
        dpi = int(cfg.get("emulator.resolution_dpi", 240))
        print(f"目标分辨率：{w}x{h} @{dpi}dpi")
        for inst in list_instances():
            print(f"  [{inst.index}] {ensure_resolution(inst.index, w, h, dpi)}")
    elif cmd == "cascadetest":
        # 级联布局自检（不碰真窗口）：改过 _cascade_pos 后必跑。
        # 先验真 API：工作区常量写错（0x1E 拿全零矩形→全部堆原点，
        # 纯计算测试查不出）这里一断言就炸。
        import ctypes as _ct
        from ctypes import wintypes as _wt
        _real = _wt.RECT()
        assert _ct.windll.user32.SystemParametersInfoW(0x30, 0, _ct.byref(_real), 0) \
            and (_real.right - _real.left) * (_real.bottom - _real.top) > 10000, \
            f"取不到工作区（检查 SPI_GETWORKAREA 常量）：{_real}"
        wa = (0, 0, 1920, 1040)          # 布局用例：1920 屏去掉任务栏的工作区
        pos = [_cascade_pos(i, 582, 996, *wa) for i in range(12)]
        assert len(set(pos)) == 12, f"有窗口完全重叠：{pos}"
        assert all(x >= 0 and y >= 0 for x, y in pos), f"越出左上角：{pos}"
        assert all(x <= wa[2] - 582 for x, _ in pos), f"越出右边缘：{pos}"
        assert all(y <= wa[3] - 40 for _, y in pos), f"标题栏被推出底边：{pos}"
        # 极端：窗口比工作区还宽 → cols 退化成 1，只能纵向排；不越左界、仍互不重叠
        big = [_cascade_pos(i, 2000, 1200, *wa) for i in range(3)]
        assert all(x == 0 for x, _ in big) and len(set(big)) == 3, \
            f"超宽窗口应贴左纵排且不错位失效：{big}"
        # 台数多到排到工作区底下：y 钳到标题栏可见线（第 100 台）
        assert _cascade_pos(100, 582, 996, *wa)[1] == wa[3] - 40, "底边钳制失效"
        print("cascadetest: ALL OK")
    elif cmd == "setcount":
        # 台数双向设置自检（不碰真雷电）：假 list2/copy/remove 维护内存台数表，
        # 验证补台、删尾部台、母体永不挨删、假成功（台数不变）如实报错。
        # 改过 set_instance_count 后必跑。
        import subprocess as _sp
        _state: dict[int, int] = {0: -1, 1: -1, 2: -1, 3: -1}   # index→pid（-1=停止）
        _mode = {"bad_copy": False, "bad_remove": False}

        def _fake_run2(args, timeout=CMD_TIMEOUT):
            if args[0] == "list2":
                out = "".join(f"{i},t{i},0,0,0,p,0\n".replace("p", str(pid))
                              for i, pid in _state.items())
                return _sp.CompletedProcess(args, 0, out, "")
            if args[0] == "copy" and not _mode["bad_copy"]:
                _state[max(_state) + 1] = -1
                return _sp.CompletedProcess(args, 6, "", "")   # 实测成功也返回 6
            if args[0] == "remove" and not _mode["bad_remove"]:
                _state.pop(int(args[args.index("--index") + 1]), None)
                return _sp.CompletedProcess(args, 0, "", "")
            return _sp.CompletedProcess(args, 0, "", "")
        # 本块在模块顶层作用域执行，赋值即改模块全局（snaptest 同款手法）
        _run = _fake_run2
        blocked_indexes = lambda: set()

        prog: list[str] = []
        m, r, e = set_instance_count(5, on_progress=prog.append)
        assert (m, r, e) == (2, [], None) and sorted(_state) == [0, 1, 2, 3, 4, 5], \
            f"补台失败：{m},{r},{e},{_state}"
        assert any("正在克隆" in s for s in prog), "补台必须有逐台进度"
        m, r, e = set_instance_count(2, on_progress=prog.append)   # 1~5 共 5 台 → 删尾 3 台
        assert (m, r, e) == (0, [5, 4, 3], None) and sorted(_state) == [0, 1, 2], \
            f"删台失败：{m},{r},{e},{_state}"
        assert any("正在删除" in s and "不可恢复" in s for s in prog), "删台必须报进度"
        m, r, e = set_instance_count(0)
        assert 0 in _state and r == [2, 1], f"母体被删或删错序：{r},{_state}"
        _state.update({7: -1}); _mode["bad_copy"] = True
        m, r, e = set_instance_count(9)
        assert e and "copy 未新建" in e and m == 0, f"假 copy 没报错：{m},{e}"
        _mode["bad_copy"] = False; _mode["bad_remove"] = True
        m, r, e = set_instance_count(0)
        assert e and "remove 未删除" in e and 7 in _state, f"假 remove 没报错：{r},{e}"
        print("setcount: ALL OK")
    elif cmd == "snaptest":
        # 快照缓存自检（不碰真雷电）：假 list2 验证保鲜期、单飞、
        # 抢不到锁的读者拿旧值不阻塞、poller 后台真的在刷。
        # 改过本段上方的快照缓存代码后必跑。
        import subprocess as _sp
        calls = []

        def _fake_run(args, timeout=CMD_TIMEOUT):
            calls.append(1)
            # 含 0 号假母体：验证卡点排除（上层永远看不见 index 0）
            out = "".join(f"{i},t{i},0,0,1,{100 + i},0\n" for i in range(4))
            return _sp.CompletedProcess(args, 0, out, "")

        # 覆盖模块全局（_list_instances_raw 运行时查全局表）；屏蔽名单也
        # 桩掉，自检结果不能被真实 config 里的 blocked_indexes 左右
        _run = _fake_run
        blocked_indexes = lambda: set()
        # 本块在模块顶层作用域执行，直接赋值即改到模块全局
        assert _snap is None and len(list_instances()) == 3, "冷启动应现打并填满 3 实例（4 台含母体 0，卡点排除后 3）"
        assert len(calls) == 1, f"冷启动只该打 1 条 list2，实际 {len(calls)}"
        list_instances(); list_instances()
        assert len(calls) == 1, f"保鲜期内读快照不该再打，实际打了 {len(calls)} 条"
        _snap_ts = 0.0  # 手动过期
        assert _refresh_lock.acquire(blocking=False)
        holder_done = []
        t = threading.Thread(
            target=lambda: (list_instances(), holder_done.append(1)), daemon=True)
        t.start(); t.join(0.5)
        assert holder_done, "锁被占时读者必须拿旧快照走人，绝不等 ldconsole（卡死病根）"
        _refresh_lock.release()
        list_instances()
        assert len(calls) == 2, f"过期后抢锁者应现打一条，实际 {len(calls)}"
        # poller 活着 + 快照过期：读者一律拿旧值走人，绝不现打——开机风暴期
        # 界面线程现打慢 ldconsole = 「无响应」病根（2026-09-30 修复）
        _poller = type("FakePoller", (), {"is_alive": lambda self: True})()
        _snap_ts = 0.0
        n0 = len(calls)
        assert len(list_instances()) == 3, "poller 在跑时过期快照也应直接返回旧值"
        assert len(calls) == n0, f"poller 在跑时读者偷打了 list2（{len(calls) - n0} 条）"
        _poller = None
        start_instance_poller()
        base = len(calls)
        time.sleep(_POLL_INTERVAL * 2.5)
        assert len(calls) > base, "poller 在跑却没看到它刷新"
        assert _snap_ts > 0 and time.time() - _snap_ts < _SNAP_TTL + 1, "poller 刷的快照应保鲜"
        # 抽风句柄兜底自检（1607 移植机实测病）：list2 返空/坏 pid 行时，
        # is_instance_alive 必须凭活句柄兜住，不许把开着的模拟器误判成"已关闭"
        from core import win_utils as _wu
        _wu_real, _alive = _wu.is_alive, {}
        _wu.is_alive = lambda h: bool(_alive.get(int(h) if h else 0, False))
        _run = lambda args, timeout=CMD_TIMEOUT: _sp.CompletedProcess(args, 0, "", "")
        _snap, _snap_ts = None, 0.0
        assert get_instance(1) is None
        assert is_instance_alive(1, 0) is False, "快照空且句柄死：该判关"
        _alive[777] = True
        assert is_instance_alive(1, 777) is True, "快照空但手里的句柄活着：抽风不误判关"
        _run = lambda args, timeout=CMD_TIMEOUT: _sp.CompletedProcess(
            args, 0, "1,t1,555,0,0,-1,0\n", "")
        _snap, _snap_ts = None, 0.0
        gi = get_instance(1)
        assert gi is not None and not gi.running, "前提：pid=-1 快照视为未跑"
        _alive[555] = True
        assert is_instance_alive(1, 0) is True, "pid 抽风但快照句柄活着：不误判关"
        _alive.clear()
        assert is_instance_alive(1, 0) is False, "句柄也死：快照与实况一致，判关"
        _wu.is_alive = _wu_real
        print("snaptest: ALL OK")
    else:
        print(f"未知命令：{cmd}")
