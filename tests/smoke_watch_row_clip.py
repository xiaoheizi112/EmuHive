# 冒烟：监视行 复制/粘贴/移动到其他监视（2026-10-05 新增）
# 后端方法直测（_watch_row_* 一组）——右键菜单只弹模态（offscreen 必卡死），
# 菜单接线走源码守卫。断言全按自加 tag 键（编辑器保真兜底会回写 name，别信它）。
# 红线：临时配置单例，不碰真实 custom_flows。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_watch_row_clip.py
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

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

# offscreen 模态死锁铁律：脏面板切流程会弹「丢弃修改」确认框——先桩成一路放行
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowPanel, FlowNodeItem

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def row(tag, kind="tap"):
    return {"category": "", "name": "", "kind": kind, "tag": tag}


cm.get_config().set("custom_flows", {
    "clipTest": {"version": 2, "nodes": [
        {"id": "w1", "type": "watch", "x": 0, "y": 0, "label": "源哨兵",
         "rows": [row(1), row(2)], "cooldown_secs": 5, "fail_limit": 3},
        {"id": "w2", "type": "watch", "x": 400, "y": 0, "label": "空哨兵",
         "rows": [], "cooldown_secs": 5, "fail_limit": 3},
        {"id": "c1", "type": "watch", "x": 800, "y": 0, "label": "容器甲",
         "groups": [{"id": "g1", "rows": [row(3)], "cooldown_secs": 5,
                     "fail_limit": 3, "scan_mode": "restart"},
                    {"id": "g2", "rows": [], "cooldown_secs": 5,
                     "fail_limit": 3, "scan_mode": "restart"}]},
    ], "edges": []},
    "clipB": {"version": 2, "nodes": [
        {"id": "w9", "type": "watch", "x": 0, "y": 0, "label": "",
         "rows": [], "cooldown_secs": 5, "fail_limit": 3},
    ], "edges": []},
})

panel = FlowPanel()
panel.chk_autosave.setChecked(False)
panel.resize(1400, 900)
panel.show()
wait(300)


def select(nid):
    panel.scene.clearSelection()
    it = next(i for i in panel.scene.items()
              if isinstance(i, FlowNodeItem) and i.node.get("id") == nid)
    it.setSelected(True)
    wait(200)
    return it.node


panel._select_flow("clipTest")
wait(300)
n_w1 = select("w1")
rows = n_w1["rows"]

# ── 复制 → 剪贴板是 deepcopy（改原件不脏板）──
panel._watch_row_copy(rows[0])
clip = panel._watch_row_clip
check("复制：剪贴板有货且非同一对象", clip is not None and clip is not rows[0])

# ── 粘贴：插到指定行之后，粘贴件与板互不相干 ──
panel._watch_row_paste(n_w1, rows, 0)
check("粘贴：插到第①行后（tag 序 [1,1,2]）",
      [r.get("tag") for r in rows] == [1, 1, 2])
check("粘贴件独立于剪贴板", rows[1] is not panel._watch_row_clip)

# ── 移动落点枚举：同流程其它节点 + 容器逐组，排除自身 ──
tg = dict(panel._watch_row_move_targets(n_w1))
check("落点含空哨兵节点", "监视节点「空哨兵」" in tg)
names = list(tg)
check("落点含容器两组、不含源自身",
      sum("容器甲" in x for x in names) == 2 and "源哨兵" not in str(names))
g2 = next(tr for nm_, tr in tg.items() if "容器甲" in nm_ and nm_.endswith("②"))

# ── 移动：搬行 dict 本体（同一对象），源减一、目标加一 ──
moved_obj = rows[0]
panel._watch_row_move(n_w1, rows, 0, g2, "容器甲组②")
check("移动后源少一行", [r.get("tag") for r in rows] == [1, 2])
check("移动是搬本体（非复印）", len(g2) == 1 and g2[0] is moved_obj)

# ── 越界移动 = 无动作（右键竞态防线）──
before = ([r.get("tag") for r in rows], list(g2))
panel._watch_row_move(n_w1, rows, 99, g2, "x")
check("越界 idx 不动任何数据",
      [r.get("tag") for r in rows] == before[0] and list(g2) == before[1])

# ── 跨流程粘贴：切流程剪贴板不清空 ──
panel._select_flow("clipB")
wait(300)
n_w9 = select("w9")
check("切流程后剪贴板仍在", panel._watch_row_clip is not None)
panel._watch_row_paste(n_w9, n_w9["rows"], -1)
check("跨流程粘贴落进另一条流程的监视行",
      [r.get("tag") for r in n_w9["rows"]] == [1])

# ── 右键菜单接线守卫（模态弹不得，只能源码级断线即红）──
# 2026-10-09 监视行骨架收口进通用 RowListEditor：标准复制/粘贴/删除项在组件内
# （_menu），「移动到其他监视」走 menu_ext 钩子注入 —— 断言意图不变：菜单必须挂上。
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ui", "flow_editor.py"), encoding="utf-8").read()
check("监视列表已挂右键菜单",
      "lst.setContextMenuPolicy(Qt.CustomContextMenu)" in src
      and "customContextMenuRequested.connect(_menu)" in src
      and "复制此行" in src and "粘贴为新行" in src and "移动到其他监视" in src
      and "menu_ext=_line_menu" in src)

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke watch-row-clip ALL OK")
