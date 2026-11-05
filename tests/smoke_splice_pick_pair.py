# 冒烟：旧「选区选服 pick_pair」→ [点文字→等待→点文字] 一次性换算（离屏）
# 守三条：
# ① 载入带 pick_pair 的流程：图里节点被拆开、config 原始 dict 一字不动、
#    状态栏点名（用户知道画布上多了两个节点）；
# ② 拆开的首末两节点用「点文字」面板正常可编辑（不再冒出两步专属控件）；
# ③ 保存后落盘的就是三节点（幂等：再载入不重复拆）。
# 红线：临时配置单例 + autosave 关，绝不碰真 configs/config.json。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_splice_pick_pair.py
import copy
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

from PySide6.QtWidgets import QApplication, QComboBox

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowPanel

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


PAIR = {"id": "pp1", "type": "server_pick", "label": "进服", "mode": "pick_pair",
        "x": 100, "y": 200, "text": "{账号.区服}", "zone_text": "",
        "roi": [72, 247, 193, 715], "roi_server": [240, 259, 339, 710],
        "pair_gap_secs": 1.5, "timeout": 1, "on_timeout": "scroll",
        "min_sim": 99, "scroll_dir": "up", "offset_y": 0, "jitter_px_max": 5}
FLOW = {"登录": {"version": 2,
                 "nodes": [{"id": "a", "type": "sleep", "x": 0, "y": 200}, PAIR,
                           {"id": "b", "type": "sleep", "x": 1200, "y": 200}],
                 "edges": [{"from": "a", "port": "out", "to": "pp1"},
                           {"from": "pp1", "port": "out", "to": "b"},
                           {"from": "pp1", "port": "fail", "to": "a"}]}}

cm._config.set("custom_flows", copy.deepcopy(FLOW))

p = FlowPanel()
p.chk_autosave.setChecked(False)
# 换算只走 ensure_graph（载入/编译共用），面板需要一个假 view 才建得起来
p.attach_view(type("_V", (), {"draw_mode": "", "roi_owner": ""})())
p._refresh_flow_list()
p.flow_name = "登录"
p._reload_flow()
app.processEvents()

nodes = p.graph.get("nodes") or []
picks = [n for n in nodes if n.get("type") == "server_pick"]
mids = [n for n in nodes if n.get("type") == "sleep"
        and n.get("label") == "步间等待"]
check("载入后图里三节点（2 点文字 + 1 等待）", len(picks) == 2 and len(mids) == 1)
check("pick_pair 专属键清干净（roi_server/zone_text/pair_gap_secs 不留）",
      all(not ({"roi_server", "zone_text", "pair_gap_secs"} & set(n))
          for n in picks)
      and all(n.get("mode", "click") == "click" for n in picks))
check("区名撞占位符（运行时才展开）→ 派生列 {账号.选区}（取区服前 4 字）",
      picks[0].get("text") == "{账号.选区}")
check("服名 = 全名、两块 ROI 各归各的",
      picks[1].get("text") == "{账号.区服}"
      and picks[0].get("roi") == [72, 247, 193, 715]
      and picks[1].get("roi") == [240, 259, 339, 710])
check("等待秒数 = 原两步间隔", mids and mids[0].get("seconds") == 1.5)
check("共用参数照抄（超时/滑动/相似度/抖动）",
      picks[0].get("timeout") == 1 and picks[1].get("on_timeout") == "scroll"
      and picks[0].get("min_sim") == 99 and picks[1].get("jitter_px_max") == 5)
eid_a, eid_b = picks[0]["id"], picks[1]["id"]
check("连线重挂：入边接首节、out/fail 挂末节",
      any(e["from"] == "a" and e["to"] == eid_a for e in p.graph["edges"])
      and any(e["from"] == eid_b and e["to"] == "b" and e["port"] == "out"
              for e in p.graph["edges"])
      and any(e["from"] == eid_b and e["port"] == "fail" for e in p.graph["edges"])
      and not any("pp1" in (e.get("from"), e.get("to"))
                  for e in p.graph["edges"]))
check("config 原始流程未被就地改写（保存前不落盘）",
      cm._config.get("custom_flows") == FLOW)

# 拆开的首节点走普通「点文字」面板：无两步专属残留、用法下拉不含选区选服
p._rebuild_props(picks[0])
app.processEvents()
cmb_mode = next(c for c in p.props_panel.findChildren(QComboBox)
                if c.findData("click") >= 0)
check("换算后节点面板：用法下拉无「选区选服」", cmb_mode.findData("pick_pair") < 0)
check("换算后节点选中 = 点文字", cmb_mode.currentData() == "click")

# 保存 → 落盘三节点；再载入不重复拆（幂等）
p._on_save(quiet=True)
saved = cm._config.get("custom_flows")["登录"]
check("保存后落盘的就是换算结果",
      len([n for n in saved["nodes"] if n.get("type") == "server_pick"]) == 2
      and not any(n.get("mode") == "pick_pair" for n in saved["nodes"]))
p._reload_flow()
app.processEvents()
check("二次载入幂等：不重复拆",
      len([n for n in p.graph["nodes"] if n.get("type") == "server_pick"]) == 2)

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke splice-pick-pair ALL OK")
