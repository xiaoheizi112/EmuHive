# 冒烟：「遍历全部」节点的面板↔数据往返 + 引用派生（P1，2026-09-29 新增；
# 同日上 P2 读数/P3 文字源+去重：⑦~⑩ 段守读数框/检测源置灰/去重解禁/文字源不认领主图；
# 2026-10-06 上数字过滤：⑪ 段守勾选落 filter_op/val、读数置灰、取消清空、重建回读）
# 守的链路：模式下拉切「进一个」→ 子流程/标志图整块显隐（_vis 纪律）、mode 落 dict；
# 子流程下拉写 flow_name（共享 _flow_combo_row 与 subflow 一份）；列表标志图写
# 嵌套 confirm_tpl 而非平铺键（断配防线：换平铺键读侧反查会漏）；各处数值控件
# 写回各自的键；关等门 → 超时控件置灰。引用侧：node_tpl_refs 门开着才算、
# node_flow_refs 只在 enter 引流程、rewrite_template_refs 主图+标志图成对传播。
# 全程 FlowPanel.__new__ 借方法（smoke_branch_multicond 同款），模板库/流程表
# 全部打桩，绝不打真实 configs/config.json（冒烟写盘陷阱铁律）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_multi_scan.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QLineEdit, QSpinBox,
                               QVBoxLayout, QWidget)

app = QApplication.instance() or QApplication(sys.argv)

import core.config_manager as cm
import ui.flow_editor as fe
from bot import template_deps
from ui.flow_editor import FlowPanel, new_node

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


# ── 临时配置单例：flow 下拉/引用扫都吃它，绝不碰真实 config ──
_tmpdir = tempfile.mkdtemp(prefix="ms_smoke_")
cm._config = cm.ConfigManager(os.path.join(_tmpdir, "config.json"))
_flows = {"子甲": {"version": 2, "nodes": [], "edges": []},
          "乙流程": {"version": 2, "nodes": [], "edges": []}}
cm._config.data["custom_flows"] = _flows

# ── 模板库打桩：单一假库，避免真 index.json 灌名字进来搅断言 ──
fe.template_manager.list_templates = lambda *a, **k: [
    {"category": "库A", "name": "头像"}, {"category": "库A", "name": "标志"}]
fe._fill_category_combo = lambda cmb, cur="": (cmb.clear(),
                                               cmb.addItem("库A", "库A"))


class FakeScene:
    def update(self):
        pass


f = FlowPanel.__new__(FlowPanel)
f.graph = {"nodes": []}
f.scene = FakeScene()
f.flow_name = "主流程"
f._mark_dirty = lambda: None
f._set_status = lambda t: None
f._repaint_node = lambda n: None
f._delete_selected = lambda: None
f._flows = lambda: dict(_flows)

holder = QWidget()
f.props_layout = QVBoxLayout(holder)

node = new_node("multi_scan")
check("建节点默认：mode=tap + 嵌套 confirm_tpl 骨架",
      node["mode"] == "tap" and isinstance(node.get("confirm_tpl"), dict)
      and set(node["confirm_tpl"]) >= {"category", "name"})

f._fill_props(node)

# ── ① 模式切换：enter 展开子块、tap 收起（_vis 纪律：构建期只藏） ──
mode_cmb = next(c for c in holder.findChildren(QComboBox)
                if [c.itemData(i) for i in range(c.count())] == ["tap", "enter", "read"])
flow_cmb = next(c for c in holder.findChildren(QComboBox)
                if c.itemText(0).startswith("（选一条流程）"))
box = flow_cmb.parentWidget()
check("默认 tap：进一个专属整块收起", box.isHidden())
mode_cmb.setCurrentIndex(1)
check("切「进一个」：mode 落 dict + 整块亮出",
      node["mode"] == "enter" and not box.isHidden())
mode_cmb.setCurrentIndex(0)
check("切回「逐个点掉」：整块收起（残值不清盘，引用侧按模式滤）",
      node["mode"] == "tap" and box.isHidden())

# ── ② 子流程下拉（与 subflow 共用一份 _flow_combo_row）写 flow_name ──
mode_cmb.setCurrentIndex(1)
i_zi = flow_cmb.findData("子甲")
check("流程下拉不吃自己名字？（主流程不该在里面，自引用=环）",
      flow_cmb.findData("主流程") < 0 and i_zi >= 0)
flow_cmb.setCurrentIndex(i_zi)
check("选「子甲」→ flow_name 落 dict", node["flow_name"] == "子甲")

# ── ③ 列表标志图：写进嵌套 confirm_tpl，绝不污染平铺键 ──
cmbos_in_box = [c for c in box.findChildren(QComboBox) if c is not flow_cmb]
name_cmb = next(c for c in cmbos_in_box
                if c.count() and c.itemText(0) == "头像")
check("标志图行灌进假库（嵌套 target 生效）",
      node["confirm_tpl"]["category"] == "库A")
name_cmb.setCurrentText("标志")
check("选标志图 → 只写 confirm_tpl.name，主扫描 name 不受污染",
      node["confirm_tpl"]["name"] == "标志" and node["name"] == "头像")

# ── ④ 数值控件写回各自的键（按 tooltip 认领，防多个同款 spin 串线） ──
spn_gap = next(s for s in holder.findChildren(QDoubleSpinBox)
               if "两次落手" in (s.toolTip() or ""))
spn_gap.setValue(1.2)
check("落手间隔 → gap_secs", node["gap_secs"] == 1.2)
spn_max = next(s for s in holder.findChildren(QDoubleSpinBox)
               if "迷路" in (s.toolTip() or ""))
spn_max.setValue(2.5)
check("等回程确认 → confirm_timeout", node["confirm_timeout"] == 2.5)
edt_var = next(e for e in holder.findChildren(QLineEdit)
               if "留空" in (e.placeholderText() or ""))
edt_var.setText("次数")
check("命中数变量名 → var_name", node["var_name"] == "次数")
chk_gate = next(c for c in holder.findChildren(QCheckBox)
                if c.text() == "扫描前先等模板出现")
spn_wait = next(s for s in holder.findChildren(QDoubleSpinBox)
                if not (s.toolTip() or "") and s.suffix() == ""
                and s.specialValueText() == "不限时")
chk_gate.toggle()
check("关等门：wait_before=False + 超时控件置灰（配了没生效的错觉不留）",
      node["wait_before"] is False and not spn_wait.isEnabled())
chk_gate.toggle()

# ── ④b 返回键可选（2026-09-30）：新节点默认不勾；勾上写 press_back ──
#      存量节点缺键的兜底（显示勾上=老行为照按）由编译层 18h 自检守
chk_pb = next(c for c in holder.findChildren(QCheckBox)
              if c.text() == "子流程后按返回键")
check("建节点默认关返回（用户拍板：可选项、默认不选）",
      node.get("press_back") is False and not chk_pb.isChecked())
chk_pb.toggle()
check("勾「子流程后按返回键」→ press_back=True 落 dict", node["press_back"] is True)
chk_pb.toggle()

# ── ⑤ 引用派生：门开着才算（切回 tap 的残留不认领） ──
nd_enter = {"type": "multi_scan", "category": "库A", "name": "头像",
            "mode": "enter", "flow_name": "子甲",
            "confirm_tpl": {"category": "库A", "name": "列表标志"}}
nd_tap = {**nd_enter, "mode": "tap"}
check("enter：主扫描图+标志图两张都认领",
      sorted(map(tuple, template_deps.node_tpl_refs(nd_enter)))
      == [("库A", "列表标志"), ("库A", "头像")])
check("tap：标志图残值不算引用（删图反查不冤枉它）",
      template_deps.node_tpl_refs(nd_tap) == [("库A", "头像")])
check("enter 引子流程 / tap 不引（flow_backrefs 同口径）",
      template_deps.node_flow_refs(nd_enter) == {"子甲"}
      and template_deps.node_flow_refs(nd_tap) == set())

# ── ⑥ 改名传播：主图 + 标志图成对跟上（读写一份认知） ──
_flows["引用者"] = {"version": 2, "nodes": [dict(nd_enter)], "edges": []}
n1 = template_deps.rewrite_template_refs("库A", "头像", new_name="头像新")
n2 = template_deps.rewrite_template_refs("库A", "列表标志", new_name="标志新")
_saved = cm._config.data["custom_flows"]["引用者"]["nodes"][0]
check("改名传播：两处各命中 1 且都落进嵌套/平铺正确位置",
      n1 == 1 and n2 == 1 and _saved["name"] == "头像新"
      and _saved["confirm_tpl"]["name"] == "标志新")

# ── ⑦ 读数模式（P2）：切「逐个读数」→ 读数框亮出、去重勾置灰 ──
mode_cmb.setCurrentIndex(2)
# 认领读宽 spin：range(2,540) 独一份（ROI「区域右」也是 540 封顶，靠下限 2 区分）
spn_rw = next(s for s in holder.findChildren(QSpinBox)
              if s.maximum() == 540 and s.minimum() == 2)
rbox = spn_rw.parentWidget()
check("切「逐个读数」：mode 落 dict + 读数整块亮出",
      node["mode"] == "read" and not rbox.isHidden())
chk_dd = next(c for c in holder.findChildren(QCheckBox) if "去重" in c.text())
spn_rounds = next(s for s in holder.findChildren(QSpinBox) if s.value() == 50)
check("读数模式：去重勾+轮数置灰（一下不点无从认脸；编译层同口径强制关）",
      not chk_dd.isEnabled() and not spn_rounds.isEnabled())
spn_rw.setValue(200)
check("读数框宽 → read_w（认脸框与读数框共用同一组键）", node["read_w"] == 200)
mode_cmb.setCurrentIndex(0)

# ── ⑧ 文字源（P3）：切 text → 模板行整套置灰但残值不清盘 ──
cmb_src = next(c for c in holder.findChildren(QComboBox)
               if [c.itemData(i) for i in range(c.count())] == ["template", "text"])
cmb_src.setCurrentIndex(1)
cat_cs = [c for c in holder.findChildren(QComboBox)
          if c.count() and c.itemData(0) == "库A"]     # 主库 + 块内标志图库
check("文字源：source 落 dict + 只有主库下拉置灰（标志图不跟源走，照常能选）",
      node["source"] == "text" and len(cat_cs) >= 2
      and not cat_cs[0].isEnabled() and cat_cs[1].isEnabled())
edt_txt = next(e for e in holder.findChildren(QLineEdit)
               if "要圈的文字" in (e.placeholderText() or ""))
edt_txt.setText("私聊")
check("目标文字 → text", node["text"] == "私聊")
spn_ms = next(s for s in holder.findChildren(QSpinBox)
              if "度门槛" in (s.toolTip() or ""))
spn_ms.setValue(80)
check("文字相似门槛 → min_sim（存 0~100，编译侧除 100）", node["min_sim"] == 80)

# ── ⑨ 去重边扫边办：勾上 → dedupe=True + 轮数可编辑 + 读数框回来（认脸用） ──
chk_dd.toggle()
check("勾去重：dedupe=True + 轮数解禁 + 邻域框重显（认脸读的就是这块）",
      node["dedupe"] is True and spn_rounds.isEnabled() and not rbox.isHidden())
spn_rounds.setValue(7)
check("最多轮数 → max_rounds", node["max_rounds"] == 7)

# ── ⑩ 引用侧：文字源不认领主扫描图（name 只是切回模板源的残值） ──
nd_text = {"type": "multi_scan", "source": "text", "text": "私聊",
           "category": "库A", "name": "头像", "mode": "tap"}
check("文字源节点：node_tpl_refs 空手（删图反查不冤枉它）",
      template_deps.node_tpl_refs(nd_text) == [])
nd_txt_enter = {**nd_text, "mode": "enter",
                "confirm_tpl": {"category": "库A", "name": "标志"}}
check("文字源+进一个：只认领列表标志图（确认图找图不跟源走）",
      template_deps.node_tpl_refs(nd_txt_enter) == [("库A", "标志")])

# ── ⑪ 数字过滤（2026-10-06）：勾选落 filter_op/val、邻域框随过滤现身、
#      读数模式置灰（编译强制关同口径）、取消勾清空、重建回读不丢 ──
from PySide6.QtCore import QEvent

chk_flt = next(c for c in holder.findChildren(QCheckBox) if "数字过滤" in c.text())
op_flt = next(c for c in holder.findChildren(QComboBox)
              if [c.itemData(i) for i in range(c.count())][:2] == ["lt", "le"])
spn_flt = next(s for s in holder.findChildren(QDoubleSpinBox)
               if "阈值" in (s.toolTip() or ""))
fbox = op_flt.parentWidget()
check("默认关：比较符/阈值块收起（配了没生效的错觉不留）",
      not chk_flt.isChecked() and fbox.isHidden()
      and not node.get("filter_op"))
chk_dd.toggle()   # 关去重：邻域框先收起，好证明它是随「过滤」重现的
check("关去重后邻域框收起", rbox.isHidden())
chk_flt.toggle()
check("勾过滤：filter_op=lt 落 dict + 比较符块亮 + 邻域框重显（达标就读这块）",
      node["filter_op"] == "lt" and not fbox.isHidden() and not rbox.isHidden())
op_flt.setCurrentIndex(op_flt.findData("ge"))
spn_flt.setValue(20)
check("比较符/阈值写回（显示中文·存储英文）",
      node["filter_op"] == "ge" and node["filter_val"] == 20.0)
mode_cmb.setCurrentIndex(2)
check("读数模式：过滤勾置灰（一下不点，过滤无意义）", not chk_flt.isEnabled())
mode_cmb.setCurrentIndex(0)
check("切回点掉：解禁且配置留着",
      chk_flt.isEnabled() and node["filter_op"] == "ge")
chk_flt.toggle()
check("取消勾：filter_op 清空=关，阈值残值照留",
      node["filter_op"] == "" and node["filter_val"] == 20.0)
node2 = {**new_node("multi_scan"), "filter_op": "ge", "filter_val": 20}
f._clear_props()                                    # _fill_props 不清旧表单
f._fill_props(node2)
app.sendPostedEvents(None, QEvent.DeferredDelete)   # 旧控件回收干净再找新的
chk2 = next(c for c in holder.findChildren(QCheckBox) if "数字过滤" in c.text())
op2 = next(c for c in holder.findChildren(QComboBox)
           if [c.itemData(i) for i in range(c.count())][:2] == ["lt", "le"])
spn2 = next(s for s in holder.findChildren(QDoubleSpinBox)
            if "阈值" in (s.toolTip() or ""))
check("重建回读：勾选+「大于等于」+阈值 20 全亮回来（存盘再开不丢）",
      chk2.isChecked() and op2.currentData() == "ge" and spn2.value() == 20.0)

print("SMOKE " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
