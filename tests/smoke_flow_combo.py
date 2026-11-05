# 冒烟：流程下拉的分类分组显示（2026-10-06 用户实拍「分类跟流程名分不出」→
# ▍记号+加粗灰底标题行+流程缩进；循环体子流程动作/监视·定时行补齐同口径）。
# 守的不变量：标题行不可选中且带▍；流程行 currentData 恒=纯流程名（缩进只是
# 显示皮，存储零动）；已删流程残留值垫末尾且被选中（下拉不偷梁换柱）；
# 单分类/没分类不插标题不缩进（老版长相）；三处入口（节点面板/循环动作/行内
# 小下拉）全走同一份灌法。临时配置单例（冒烟写盘铁律），绝不碰真实 config。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_flow_combo.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtWidgets import QApplication, QComboBox, QFormLayout, QWidget

app = QApplication.instance() or QApplication(sys.argv)

import core.config_manager as cm

_tmp = tempfile.mkdtemp(prefix="fc_smoke_")
cm._config = cm.ConfigManager(os.path.join(_tmp, "config.json"))
cfg = cm.get_config()
g = {"version": 2, "nodes": [], "edges": []}
cfg.set("custom_flows", {"甲流程": dict(g), "乙流程": dict(g), "丙流程": dict(g)})
cfg.set("flow_categories", {"甲流程": "日常", "乙流程": "主线"})
cfg.set("flow_category_order", ["主线", "日常"])

from ui.flow_editor import FlowPanel, _fill_flow_grouped

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


class FakeScene:
    def update(self):
        pass


f = FlowPanel.__new__(FlowPanel)
f.graph = {"nodes": []}
f.scene = FakeScene()
f.flow_name = "主流程"
f._flows = lambda: dict(cfg.get("custom_flows"))
f._mark_dirty = lambda: None
f._set_status = lambda t: None
f._repaint_node = lambda n: None
f._rebuild_props = lambda n: None


def texts(cmb):
    return [cmb.itemText(i) for i in range(cmb.count())]


def datas(cmb):
    return [cmb.itemData(i) for i in range(cmb.count())]


def header_rows(cmb):
    return [i for i in range(cmb.count())
            if cmb.itemText(i).startswith("▍")]


# ① 节点面板大下拉：两分类 → 标题行不可选、流程缩进、data 纯名
holder = QWidget()
form = QFormLayout(holder)
cmb = f._flow_combo_row(form, {"type": "subflow", "flow_name": "乙流程"})
hs = header_rows(cmb)
check("两分类+未分类兜底：3 条▍标题行且全不可选中",
      len(hs) == 3 and all(not cmb.model().item(i).isEnabled() for i in hs))
check("分类顺序照 flow_category_order（主线在前·未分类恒最后）",
      [cmb.itemText(i) for i in hs] == ["▍主线", "▍日常", "▍未分类"])
check("流程行缩进两格但 currentData=纯流程名（存储零动）",
      ("　　乙流程", "乙流程") in [(texts(cmb)[i], datas(cmb)[i])
                                   for i in range(cmb.count())])
check("选中回位：存值「乙流程」高亮在自己标题下", cmb.currentData() == "乙流程")
check("无分类的「丙流程」兜底排尾不丢", "丙流程" in datas(cmb))

# ② 残留值：目标流程被删 → 垫末尾且照样选中，不"偷梁换柱"
cmb2 = f._flow_combo_row(form, {"type": "subflow", "flow_name": "幽灵流程"})
check("残留值保住：可见可选中被选中（下拉不偷改进存值）",
      "幽灵流程" in datas(cmb2) and cmb2.currentData() == "幽灵流程")

# ③ 单分类/没分类：不插标题不缩进（零杂音，跟老版长相一致）
cfg.set("flow_categories", {"甲流程": "日常", "乙流程": "日常", "丙流程": "日常"})
cmb3 = f._flow_combo_row(form, {"type": "subflow", "flow_name": ""})
check("全挤一个分类：不插标题不缩进",
      not header_rows(cmb3) and all(not t.startswith("　") for t in texts(cmb3)))
cfg.set("flow_categories", {})
cmb4 = f._flow_combo_row(form, {"type": "subflow", "flow_name": ""})
check("没分类：纯平铺老版长相", not header_rows(cmb4))

# ④⑤ 另两处入口：先把分类灌回去（③ 掏空过）
cfg.set("flow_categories", {"甲流程": "日常", "乙流程": "主线"})
ln = {"id": "L", "type": "loop", "steps": []}
s = {"id": "s", "type": "subflow", "flow_name": "甲流程"}
w = f._build_loop_step_editor(ln, s)
cmb_l = next(c for c in w.findChildren(QComboBox) if "甲流程" in datas(c))
check("循环体子流程动作：分组标题在+选中回位（曾是平铺裸列表）",
      len(header_rows(cmb_l)) == 3 and cmb_l.currentData() == "甲流程")

# ⑤ 监视/定时行内小下拉：同口径 + 占位符文案不吃亏
cmb_r, btn = f._flow_combo({"flow_name": "乙流程"})
check("行内小下拉：分组在、占位符仍是（选子流程）",
      len(header_rows(cmb_r)) == 3
      and cmb_r.itemText(0) == "（选子流程）" and cmb_r.currentData() == "乙流程")

print("SMOKE " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
