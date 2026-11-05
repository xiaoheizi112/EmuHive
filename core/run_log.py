"""
core/run_log.py — 运行日志落盘（排查"线程什么时候退的 / 步骤为什么卡住"）

背景：以前所有运行信息只进界面 LogPanel（ui/log_panel.py），关程序即丢，
出问题事后无从归因。本模块把同一批日志再写一份到 logs/run_YYYYMMDD.log，
按天分文件，UTF-8，随时可开来看。

设计约束：
- core 层，不 import PySide6；任何模块都能直接调 log()。
- 写失败（磁盘满/占用/无权限）静默吞掉，绝不因为"记日志"把挂机带崩。
- 一行一条、写完即 flush 进操作系统页缓存：Python 进程崩溃/被正常终止都
  不丢（OS 接管续写）。真 fsync 强刷到磁盘有节流（默认每 2 秒最多一次）：
  旧版每条日志都 fsync，10+ 实例挂机时主线程每秒被同步磁盘写打十几次，
  机械盘/杀软扫描下界面明显发黏；节流后最坏丢的只是"整机断电前最后
  2 秒"，排查价值远低于换来不卡界面。
- 冒烟/测试可用环境变量 EMUHIVE_NO_FILELOG=1 关掉落盘，避免污染 logs/。

调用方在界面层（ui/main_window.py 把 worker 的 action_log 信号同时接到
LogPanel 和本模块），bot 层保持"只 emit 信号"的分层不变。
"""

import os
import threading
import time
from datetime import datetime

from core.config_manager import PROJECT_ROOT

_LOG_DIR = PROJECT_ROOT / "logs"   # PROJECT_ROOT 锚定：不吃当前工作目录
_lock = threading.Lock()
_disabled = bool(os.environ.get("EMUHIVE_NO_FILELOG"))
# fsync 节流：距上次真刷盘不足这么多秒就只 flush 不 fsync
_FSYNC_INTERVAL = 2.0
_last_fsync = 0.0

# 常驻文件句柄（按日轮换）：旧版每行 open()+close() 一次，10+ 实例挂机时
# 每秒几十次文件创建/杀软扫描纯白烧；mkdir 也从每行前置到 import 一次。
_fh = None
_fh_day = ""


def _init_dir() -> None:
    """建 logs/ 目录（import 时一次；失败不抛——首写时 _open 会再试）。"""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _open(day: str) -> None:
    """开当天文件句柄（追加模式）。调用方持锁。"""
    global _fh, _fh_day
    _close()
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _fh = open(_LOG_DIR / f"run_{day}.log", "a", encoding="utf-8")
    _fh_day = day


def _close() -> None:
    """关现句柄（调用方持锁）。"""
    global _fh, _fh_day
    if _fh is not None:
        try:
            _fh.close()
        except OSError:
            pass
        _fh = None
        _fh_day = ""


_init_dir()


def log(index: int | str, level: str, message: str) -> None:
    """写一行运行日志：[时间] [实例N] [级别] 内容。失败静默。"""
    global _last_fsync
    if _disabled:
        return
    try:
        now_dt = datetime.now()
        line = (f"[{now_dt:%Y-%m-%d %H:%M:%S}] [inst{index}] "
                f"[{level}] {message}\n")
        day = f"{now_dt:%Y%m%d}"
        with _lock:
            if _fh is None or _fh_day != day:
                _open(day)          # 首写/跨天轮换：换文件重开
            try:
                _fh.write(line)
                _fh.flush()         # 进 OS 缓存：进程死也不丢
            except OSError:
                _close()            # 句柄坏了（文件被外部删等）：丢本行，下条重开
                return
            now = time.monotonic()
            if now - _last_fsync >= _FSYNC_INTERVAL:
                try:
                    os.fsync(_fh.fileno())   # 节流强刷：防整机断电
                except OSError:
                    pass
                _last_fsync = now
    except Exception:
        # 日志本身出任何问题都不能冒泡——这是防线的底线
        pass
