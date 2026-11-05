"""ui/flow_utils.py — 流程清单的 UI 侧共用小料

任务栏（ui/category_sidebar）与任务清单（ui/queue_panel）都以
config.custom_flows 的键为"现存流程"唯一真相。以前两处各写各的一份集合
推导，口径要改得改两个文件（必漂移）。收成一处。"""

from core.config_manager import get_config


def live_flow_names() -> set[str]:
    """现存流程名集合 = custom_flows 的键（唯一真相，两处 UI 同口径）"""
    return set((get_config().get("custom_flows", {}) or {}).keys())
