# 冒烟：所有节点类型的属性面板都必须塞进 318px 视口（不出横向滑块）。
# 病根回归①（2026-10-05 用户实拍）：循环节点把 300px 宽的退出条件编辑器挂进
# 带标签列的表单行 → 360px，整个面板左半边被切，全靠左右拖才看得全。
# 修法=条件编辑器跨整行（判断节点同款摆法）。
# 病根回归②（同日实拍）：并行监视容器页签里「跑+流程下拉+即点+5图标钮」一行
# 最小宽 310px，套上页签框边 = 326px 又出横向滑块。修法=行距 6→2 + 流程下拉
# 收缩下限按 4 字算（AdjustToMinimumContentsLengthWithIcon）。
# 故除逐类型外，还须量带 groups 的容器节点和含 tap_on_hit 偏移行的形态。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_prop_width.py
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
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowPanel, _palette_items, new_node

VIEWPORT_W = 318   # props_scroll.setFixedWidth(320) 减边框余量


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


types = sorted({d for d, *_ in _palette_items()})
nodes = []
for i, t in enumerate(types):
    nd = new_node(t)
    nd["id"] = f"x{i}"
    nd["x"] = (i % 5) * 400
    nd["y"] = (i // 5) * 300
    nodes.append(nd)
# 哨兵三形态（回归②的病发形状）：容器双组 / 普通含即点偏移行 / 定时含行
_wrow = {"category": "", "name": "", "kind": "subflow", "flow_name": "probe"}
_trow = {"category": "", "name": "", "kind": "tap", "tap_on_hit": True}
nodes.append({"id": "wc", "type": "watch", "x": 2000, "y": 0, "label": "",
              "groups": [{"id": "gA", "rows": [dict(_wrow)], "cooldown_secs": 5,
                          "fail_limit": 3, "scan_mode": "restart"},
                         {"id": "gB", "rows": [dict(_trow)], "cooldown_secs": 5,
                          "fail_limit": 3, "scan_mode": "continue"}]})
nodes.append({"id": "wp", "type": "watch", "x": 2000, "y": 300, "label": "",
              "rows": [dict(_wrow), dict(_trow)],
              "cooldown_secs": 5, "fail_limit": 3})
nodes.append({"id": "tp", "type": "timer", "x": 2000, "y": 600, "label": "",
              "rows": [{"interval_min": 5, "flow_name": "probe"}],
              "cooldown_secs": 5, "fail_limit": 3})
cm.get_config().set("custom_flows", {
    "probe": {"version": 2, "nodes": nodes, "edges": []}})

panel = FlowPanel()
panel.chk_autosave.setChecked(False)
panel.resize(1400, 900)
panel.show()
wait(200)
panel._select_flow("probe")
wait(300)

ok = True
labels = types + ["watch容器(双组)", "watch普通(含即点行)", "timer(含行)"]
for i, t in enumerate(labels):
    panel._rebuild_props(nodes[i])
    wait(120)   # 兑现上一轮 deleteLater，残留件不参与测量
    mw = panel.props_panel.minimumSizeHint().width()
    for c in panel.props_panel.findChildren(QWidget):
        if c.isVisibleTo(panel.props_panel):
            mw = max(mw, c.minimumSizeHint().width())
    bad = mw > VIEWPORT_W
    ok = ok and not bad
    print(("FAIL " if bad else "OK   ") + f"{t}: 最宽 {mw}px")

print("SMOKE PASS" if ok else "SMOKE FAIL")
sys.exit(0 if ok else 1)
