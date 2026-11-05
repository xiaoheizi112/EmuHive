# 模板库右键菜单「删除」自检（正式冒烟，tests/ 下常驻）
# 守的规矩：右键删除必须与底部按钮同一删除入口(on_delete→_delete_entries)，
# 且选择逻辑照资源管理器惯例——右键【未选中】条目清多选锁单条，右键【已选中】
# 条目保留整组多选(=批量删)。属性/重命名是单条语义，多选时不出现。
# ⚠ 全程打桩 _delete_entries + QMenu.exec，绝不真删模板库（用户资产红线）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_template_menu_delete.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (QApplication, QMenu, QTreeWidget,
                               QTreeWidgetItem, QWidget)

from ui.template_tool import _TYPE_TPL, TemplatePanel

app = QApplication.instance() or QApplication(sys.argv)

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


# 捕获菜单项：把被测模块里的 QMenu 整个换成假类。不能只桩 exec——
# PySide6 的 exec 是 Qt 元对象方法，Python 覆盖拦不住，offscreen 下真
# 弹窗会永久阻塞（QMessageBox 同款坑）。假类不弹窗，action 仍可 trigger。
captured = {}


class FakeMenu(QMenu):        # 继承真 QMenu：addAction/QAction 语义全现成
    def __init__(self, parent=None):
        super().__init__(parent)

    def exec(self, *a, **k):  # 只记录不弹（此覆盖对本类实例生效）
        captured["actions"] = [act.text() for act in self.actions()]
        captured["menu"] = self
        return 0


import ui.template_tool as tt
tt.QMenu = FakeMenu

# 最小宿主 QWidget（QMenu(self) 要真 QObject）只借被测的两个方法，
# 再塞一棵真树 + 记录用的 _delete_entries 桩——不建整套 UI、不碰 config。
class BarePanel(QWidget):
    on_template_menu = TemplatePanel.on_template_menu
    on_delete = TemplatePanel.on_delete


panel = BarePanel()
panel._set_status = lambda text: None
tree = QTreeWidget()
tree.setHeaderHidden(True)
panel.tree_templates = tree
panel.status_message = None
deleted_calls = []
panel._delete_entries = lambda items: deleted_calls.append(list(items))
panel._edit_props = lambda cat, name: None
panel._rename_template = lambda cat, name: None
panel._move_template = lambda cat, name: None


def mk(name, cat):
    it = QTreeWidgetItem([f"{name}  [20x20]  阈0.85"], _TYPE_TPL)
    it.setData(0, Qt.UserRole, (name, cat))
    tree.addTopLevelItem(it)
    return it


a, b, c = mk("按钮A", "挂机打怪"), mk("按钮B", "挂机打怪"), mk("图标C", "游戏登录")
tree.expandAll()
app.processEvents()


def center(item):
    r = tree.visualItemRect(item)
    return QPoint(int(r.center().x()), int(r.center().y()))


# ① 无选中时右键单条：锁该条，菜单含"修改属性/重新抠图/重命名/移动到库/删除"
panel.on_template_menu(center(b))
check("右键未选中条目：只锁这一条", [it.data(0, Qt.UserRole)[0]
                                    for it in tree.selectedItems()] == ["按钮B"])
check("右键单条菜单含修改属性/重新抠图/重新采集/重命名/移动到库/删除",
      captured["actions"] == ["修改属性…", "重新抠图…", "重新采集·覆盖…",
                              "重命名…", "移动到库…", "删除"])

# 触发删除项 → 应把锁定的这一条喂给 _delete_entries（真删被桩挡住）
next(act for act in captured["menu"].actions()
     if act.text() == "删除").trigger()
check("点右键『删除』→ 删除集合=锁定的单条",
      deleted_calls[-1] == [("按钮B", "挂机打怪")])

# ② 先多选 A+B，右键【已选中】的 A：保留整组，菜单只有"删除 2 个"
a.setSelected(True)
b.setSelected(True)
panel.on_template_menu(center(a))
check("右键已选中条目：保留整组多选",
      sorted(it.data(0, Qt.UserRole)[0] for it in tree.selectedItems())
      == ["按钮A", "按钮B"])
check("多选右键菜单只有批量删除项",
      captured["actions"] == ["删除 2 个"])
captured["menu"].actions()[0].trigger()
check("点『删除 2 个』→ 删除集合=整组",
      sorted(deleted_calls[-1]) == [("按钮A", "挂机打怪"), ("按钮B", "挂机打怪")])

# ③ 多选 A+B 后右键【未选中】的 C：清掉旧多选、只锁 C（不给 C 带上 A/B 误删）
a.setSelected(True)
b.setSelected(True)
panel.on_template_menu(center(c))
check("多选时右键未选中条目：清旧多选只锁 C",
      [it.data(0, Qt.UserRole)[0] for it in tree.selectedItems()] == ["图标C"])
check("该情形菜单回到单条六项",
      captured["actions"] == ["修改属性…", "重新抠图…", "重新采集·覆盖…",
                              "重命名…", "移动到库…", "删除"])

print("ALL OK" if ok else "HAS FAIL")
sys.exit(0 if ok else 1)
