"""
bot/context.py — 每账号的运行上下文（第 7 步）

BotContext 把所有"这个账号在操作什么"的信息捆成一个对象，
传给 worker 和每个任务步骤：绑定哪个实例、窗口句柄、当前帧、
校准参数。任务代码只从 ctx 取东西，不直接摸 core 的全局状态。
"""

from dataclasses import dataclass, field

import numpy as np


@dataclass
class BotContext:
    ld_index: int                    # 绑定的模拟器实例编号
    account_id: int | None = None    # 数据库账号 id（第 8 步接入账号后使用）
    hwnd: int = 0                    # 窗口句柄
    calib: dict | None = None        # 校准参数（截图裁剪用）
    frame: np.ndarray | None = None  # 当前帧（基准分辨率空间），每轮由 worker 更新
    log: callable = None             # 日志回调 (message, level)，worker 注入；步骤用它写日志
    extra: dict = field(default_factory=dict)   # 步骤/条件的跨拍临时状态（如 motion 采样历史）

    def update_hwnd(self, hwnd: int):
        """实例重启后句柄会变，校准也要重做"""
        if hwnd != self.hwnd:
            self.hwnd = hwnd
            self.calib = None
