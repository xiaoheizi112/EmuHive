"""并行监视每行命中统计（运行内存账本，绝不落盘）：
引擎哨兵命中一行记一笔，流程编辑器读来显示「×N · 最近 X 前」——
回答「这条监视行挂机时到底生没生效」的唯一硬证据。

键 = stats_key(流程名, 节点id) + 行号；值 = (命中次数, 最近时刻)。
重启归零是刻意的：统计答的是"这一轮跑起来灵不灵"，不是历史账。

ponytail: 不加锁——worker 线程写、UI 线程读，GIL 保证 dict 单项读写
原子；读到的最多旧一拍，参考展示不致命。要精确计数再上 threading.Lock。
"""
import time

_hits: dict[tuple, dict[int, tuple[int, float]]] = {}


def record(stats_key, idx: int):
    """哨兵命中一行时调用（stats_key=None = 调用方没登记，静默不记）。"""
    if stats_key is None:
        return
    rows = _hits.setdefault(stats_key, {})
    n, _ = rows.get(idx, (0, 0.0))
    rows[idx] = (n + 1, time.time())


def snapshot(stats_key) -> dict[int, tuple[int, float]]:
    """读某监视节点当前各行命中 {(行号): (次数, 最近时刻)}（拷贝，随便用）。"""
    return dict(_hits.get(stats_key) or {})
