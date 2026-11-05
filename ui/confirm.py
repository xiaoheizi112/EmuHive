"""
ui/confirm.py — 二选一确认框的统一封装（全项目「Yes|No 手搓模板」的单一出口）

散在各面板的 `QMessageBox.question(... Yes | No, ...)` + `if != Yes: return`
样板收这里一个函数。关键约束（别动）：内部仍走 QMessageBox.question ——
离屏冒烟靠打桩 QMessageBox.question 挡住模态阻塞（见 CLAUDE.md「offscreen 模态框坑」），
换成自建对话框会让所有桩失效。

默认按钮 = 否（default_no=True）：破坏性操作（删配置/删台/封号/重置）一律
防手滑，回车不触发。
"""

from PySide6.QtWidgets import QMessageBox


def confirm(parent, title: str, text: str, default_no: bool = True) -> bool:
    """弹 Yes|No 确认框，用户点「是」返回 True。offscreen 下冒烟桩照常命中。"""
    r = QMessageBox.question(
        parent, title, text,
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No if default_no else QMessageBox.Yes)
    return r == QMessageBox.Yes
