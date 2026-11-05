"""
bot/job_state.py — worker 显式状态机（借鉴 LCA app_core/control_plane/job_state.py）

把散落在 worker 各处的裸字符串状态("starting"/"running"/...)收敛成枚举,
并配一张"合法转移表"。任何非法跳转(如 paused→completed)在入口就被拒。

本步学到的知识点:
- 枚举(Enum) + 转移表 = 把"状态怎么变"写死成数据,而不是散在各处 if 里
- 单一出口 _set_state:所有状态 emit 只走这一处,先 can_transition 校验再放行,
  状态机永远不会进入一个"没人定义过怎么可能到"的怪状态
"""

from enum import Enum


class WorkerState(Enum):
    """worker 生命周期状态。.value 与历史字符串保持一致(UI 零改动)。"""

    STOPPED = "stopped"        # 线程已退出(初始态/终态)
    STARTING = "starting"      # 正在校准/初始化
    RUNNING = "running"        # 正常挂机中
    PAUSED = "paused"          # 已暂停
    ERROR = "error"            # 异常退出


# 合法转移表:每个状态只能跳到这些状态(Python 3.7+ set 字面量)
# 覆盖真实路径:启动、启动即失败、暂停/恢复、异常、正常收尾
# (MONITORING 巡逻态已随弹窗拦截子系统一起下线，字符串 "monitoring" 不再产生；
#  老配置/UI 若残留该值，from_value 返回 None、各处 .get 兜底显示原样，不会崩)
_TRANSITIONS = {
    WorkerState.STOPPED: {WorkerState.STARTING, WorkerState.ERROR},
    WorkerState.STARTING: {WorkerState.RUNNING, WorkerState.ERROR, WorkerState.STOPPED},
    WorkerState.RUNNING: {WorkerState.PAUSED, WorkerState.ERROR, WorkerState.STOPPED},
    WorkerState.PAUSED: {WorkerState.RUNNING, WorkerState.ERROR, WorkerState.STOPPED},
    WorkerState.ERROR: {WorkerState.STOPPED},
}


def can_transition(current: WorkerState, new: WorkerState) -> bool:
    """判断 current → new 是否合法。同状态视为合法(幂等)。"""
    if current == new:
        return True
    return new in _TRANSITIONS.get(current, set())


def from_value(value) -> WorkerState:
    """按字符串值反查枚举(方便对接旧字符串),找不到返回 None。"""
    for state in WorkerState:
        if state.value == value:
            return state
    return None