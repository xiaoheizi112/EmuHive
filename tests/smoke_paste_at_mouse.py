# 冒烟：画布节点粘贴跟随鼠标（2026-10-07 用户："贴在我鼠标指着的位置，
# 不是原画板固定坐标"）。
# 守住：①单节点贴到鼠标场景点+30 让位；②多节点保持相对布局整组搬；
# ③同一片剪贴板连贴阶梯错开（_paste_n），Ctrl+C 归零；④鼠标不在画布上
# 用视口中心兜底；⑤剪贴板边不丢、id 全换。
# 不真存盘（临时配置单例 + autosave 关）。offscreen 下真鼠标喂不进 Qt——
# 打桩 QCursor.pos（QMessageBox.question 同款静态方法桩），指向视口内某点。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_paste_at_mouse.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core.config_manager as cm

cfg = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))
cm._config = cfg

from PySide6.QtCore import QEventLoop, QPoint, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def node(nid, x, y):
    return {"id": nid, "type": "sleep", "x": x, "y": y, "label": "", "seconds": 1}


cfg.set("custom_flows", {"pt": {"version": 2, "nodes": [
    node("a", 100, 100), node("b", 500, 300),
    node("solo", 900, 100),
], "edges": [{"from": "a", "port": "out", "to": "b", "to_port": "in"}]}})

from ui.flow_editor import FlowNodeItem, FlowPanel

panel = FlowPanel()
panel.chk_autosave.setChecked(False)
panel.resize(1400, 900)
panel.show()
wait(300)
panel._select_flow("pt")
wait(300)

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


def select(*nids):
    panel.scene.clearSelection()
    for nid in nids:
        panel._node_items[nid].setSelected(True)
    wait(120)


def put_mouse(vp_x, vp_y):
    """把（打桩的）全局鼠标指到视口内该点。"""
    gl = panel.view.viewport().mapToGlobal(QPoint(vp_x, vp_y))
    QCursor.pos = staticmethod(lambda: gl)
    return panel.view.mapToScene(QPoint(vp_x, vp_y))  # 期望的场景锚点


def pasted():
    """当前画布上新粘贴节点（不在原三条里的）。"""
    old = {"a", "b", "solo"}
    return [it.node for it in panel.scene.items()
            if isinstance(it, FlowNodeItem) and it.node["id"] not in old]


# ── ① 单节点：贴到鼠标位（左上角 +30 让位，一角露在指针下当把手）──
select("solo")
panel._copy_selection()
anchor = put_mouse(600, 400)
panel._paste()
p = pasted()
check("① 单节点贴上去了", len(p) == 1)
check(f"① 落点=鼠标场景点+30（node({p[0]['x']:.0f},{p[0]['y']:.0f}) "
      f"锚({anchor.x():.0f},{anchor.y():.0f})）",
      abs(p[0]["x"] - (anchor.x() + 30)) < 1
      and abs(p[0]["y"] - (anchor.y() + 30)) < 1)

# ── ② 多节点+边：整组按包围盒左上角对齐，相对布局保持，边跟着重挂新 id ──
n0 = len(panel.graph["nodes"])
select("a", "b")
panel._copy_selection()
anchor2 = put_mouse(300, 600)
panel._paste()
p2 = [nd for nd in panel.graph["nodes"][n0:]]
check("② 两节点一起贴出", len(p2) == 2)
xy = sorted((nd["x"], nd["y"]) for nd in p2)
check(f"② 包围盒左上角到鼠标位（+30），相对偏移 400x200 保持",
      abs(xy[0][0] - (anchor2.x() + 30)) < 1
      and abs(xy[0][1] - (anchor2.y() + 30)) < 1
      and abs(xy[1][0] - xy[0][0] - 400) < 1
      and abs(xy[1][1] - xy[0][1] - 200) < 1)
ids2 = {nd["id"] for nd in p2}
e = [ed for ed in panel.graph["edges"]
     if ed["from"] in ids2 and ed["to"] in ids2]
check("② 剪贴板里的边跟着贴出（两端都指新 id）", len(e) == 1)

# ── ③ 连贴阶梯 + 复制归零 ──
before = sorted((nd["x"], nd["y"]) for nd in pasted())
panel._paste()
after = sorted((nd["x"], nd["y"]) for nd in pasted())
newest = [q for q in after if q not in before]
check(f"③ 鼠标不动连贴第二刀再错开 30（新刀比锚多 {newest[0][0] - anchor2.x():.0f}px）",
      len(newest) == 2 and abs(newest[0][0] - (anchor2.x() + 60)) < 1)
panel._copy_selection()          # 重新 Ctrl+C → 阶梯归零
check("③ Ctrl+C 后 _paste_n 归零", panel._paste_n == 0)

# ── ④ 鼠标不在画布上：视口中心兜底（不炸、贴得出来）──
gl_far = panel.view.viewport().mapToGlobal(QPoint(-5000, -5000))
QCursor.pos = staticmethod(lambda: gl_far)
n1 = len(panel.graph["nodes"])
panel._paste()
check("④ 鼠标越界仍贴得出（视口中心兜底）",
      len(panel.graph["nodes"]) == n1 + 2)

print("SMOKE " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
