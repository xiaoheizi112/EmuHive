# 冒烟：子流程「主流程监视照管」开关（2026-10-05 新增）
# 引擎语义（停扫/已起跑链照常推进/timer 不殃及/编译透传）由 python -m bot.tasks.flow
# 第 8 段守；本冒烟守界面侧：勾选框默认态、改存往返、重建编辑区不丢、断线源码守卫。
# 红线：临时配置单例，不碰真实 custom_flows。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_subflow_watch_switch.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

from PySide6.QtCore import QEvent, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QCheckBox

app = QApplication.instance() or QApplication(sys.argv)

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.sendPostedEvents(None, QEvent.DeferredDelete)  # 嵌套循环不跑延迟删除（老坑）


BG_TXT = "子流程期间主流程监视照常"
cm.get_config().set("custom_flows", {"swTest": {"version": 2, "nodes": [
    {"id": "s1", "type": "subflow", "x": 0, "y": 0, "label": "", "flow_name": ""},
], "edges": []}})

from ui.flow_editor import FlowPanel, FlowNodeItem

panel = FlowPanel()
panel.chk_autosave.setChecked(False)
panel.resize(1400, 900)
panel.show()
wait(300)
panel._select_flow("swTest")
wait(300)


def select(nid):
    panel.scene.clearSelection()
    it = next(i for i in panel.scene.items()
              if isinstance(i, FlowNodeItem) and i.node.get("id") == nid)
    it.setSelected(True)
    wait(200)
    return it.node


def bg_chk():
    return next(c for c in panel.props_panel.findChildren(QCheckBox)
                if c.text() == BG_TXT and c.isVisibleTo(panel))


node = select("s1")
chk = bg_chk()
check("老节点没填字段 → 勾选框默认照管（开）", chk.isChecked())

chk.click()
wait(80)
check("取消勾选 → 节点落 bg_watch=False", node.get("bg_watch") is False)

# 重建编辑区（摘选再选）：存 False → 勾保持关
panel.scene.clearSelection()
wait(100)
node2 = select("s1")
check("重建编辑区：停哨状态不丢", node2 is node and not bg_chk().isChecked())

bg_chk().click()
wait(80)
check("再勾回 → bg_watch=True 写回节点", node.get("bg_watch") is True)

# 断线守卫：引擎两处接线（编译透传 / GraphStep 停哨闸）——删了就静默失灵
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fl = open(os.path.join(root, "bot", "tasks", "flow.py"), encoding="utf-8").read()
fm = open(os.path.join(root, "bot", "fsm.py"), encoding="utf-8").read()
check("编译透传在位（SubFlowStep 吃 bg_watch）",
      "bg_watch=bool(node.get(\"bg_watch\", True))" in fl
      and "self.suppresses_watch = not bg_watch" in fl)
check("GraphStep 停哨闸在位（watch 停扫、链在跑的不掐）",
      "suppresses_watch" in fm and "_pending" in fm.split("hush = any")[1][:600])

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke subflow-watch-switch ALL OK")
