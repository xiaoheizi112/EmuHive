"""
ui/log_panel.py — 日志面板

一个只读文本框，全程序的运行信息都往这里写。
setMaximumBlockCount(2000)：最多保留 2000 行，超了自动丢最旧的——
防止挂机几天日志无限增长吃光内存。
外观（白底卡片、细线边框）统一在全局主题 ui/theme.py，认 objectName: logPanel。
"""

import html
from datetime import datetime

from PySide6.QtWidgets import QPlainTextEdit

from ui import theme

# 级别显示样式：内部级别照旧传英文 INFO/WARN/ERROR（调用方、落盘、存储都不动），
# 这里只换「显示文本 + 整行颜色」——中文标签、颜色取 theme 现成常量（绿/橙/红）。
_LEVEL_STYLE = {
    "INFO": ("信息", theme.SUCCESS),
    "WARN": ("警告", theme.WARN),
    "ERROR": ("错误", theme.DANGER),
}

# 任务边界/结果收口行（2026-10-04 用户拍板）：命中这些成词短语的行，级别标签
# 整个换成「开始/完成/失败」并走专属色（金/蓝/红），信息海洋里一眼切出任务块。
# ⚠ 只认成词短语防误伤：「截图失败（等待恢复）」「写库失败」这类过程性文案
#   不算执行失败，照原级别显示。「开始」查词在最前：分隔线「── 开始测试… ──」
#   同款句式先归边界行。
_RESULT_STYLE = [
    (("开始执行任务", "开始测试"), "开始", theme.GOLD),
    (("执行完成", "任务完成", "测试完成"), "完成", theme.ACCENT),
    (("任务失败", "执行失败", "测试失败", "判失败"), "失败", theme.DANGER),
]


class LogPanel(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("logPanel")
        self.setReadOnly(True)                 # 只读
        self.setMaximumBlockCount(2000)        # 上限 2000 行
        self.setPlaceholderText("日志输出区……")

    def log(self, message: str, level: str = "INFO"):
        """追加一行日志：[时间] [级别] 内容（级别显示中文，整行按级别着色）

        执行完成/失败类收口行优先顶掉级别标签（见 _RESULT_STYLE）。未知级别
        （非三档）且非结果行原样显示、用面板默认色。消息走 html.escape：
        内容里带 <>&（用户输入、异常文本都可能）不许当标签解析，防吞字炸排版。
        """
        ts = datetime.now().strftime("%H:%M:%S")
        label, color = _LEVEL_STYLE.get(level, (level, ""))
        for kws, tag, c in _RESULT_STYLE:
            if any(k in message for k in kws):
                label, color = tag, c
                break
        if not color:
            self.appendPlainText(f"[{ts}] [{label}] {message}")
            return
        line = html.escape(f"[{ts}] [{label}] {message}")
        self.appendHtml(f'<span style="color:{color};">{line}</span>')
