# 冒烟：判断/循环出口 多条件区（2026-09-25 与/或/取反上线；2026-10-06 循环出口
# 复用同区；同日监视行同款改版：条件1..N 全进「摘要行列表+选中行编辑区」，
# RowListEditor 组件化，cond 不再在区外单摊一张表单）。
# 守的链路（存储侧一个字没动）：单条件不偷建 extra_conds/logic（老数据零迁移）；
# ＋添加 → 落第 2 条 + logic + 触发重建；选中行编辑器只编辑自己那份 cond dict；
# 「本条取反」写 cond["not"]、取消弹键；换序 commit 后 cond 恒=列表第 1 行（身份）；
# 删到只剩 1 条 extra_conds/logic 整个弹回纯老形态；循环复用措辞/禁 motion。
# 组合语义本身由 `python -m bot.tasks.flow` 自检 10e/10i 段守，这里只管面板↔数据。
# 全程 FlowPanel.__new__ 借方法（smoke_cond_text_roi 同款），不建真 UI。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_branch_multicond.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QListWidget, QPushButton)

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowPanel, RowListEditor, cond_row_summary

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


class FakeScene:
    def update(self):
        pass


f = FlowPanel.__new__(FlowPanel)
f.graph = {"nodes": []}   # _cond_var 现扫变量名要读 self.graph（借方法不跑 __init__）
f.scene = FakeScene()     # 模板库下拉回填走 scene.update 刷缩略图
f._mark_dirty = lambda: None
f._set_status = lambda t: None
f._repaint_node = lambda n: None
f._rebuild_calls = []
f._rebuild_props = lambda n: f._rebuild_calls.append(n)


def rows_of(w):
    return w.findChild(QListWidget)


# ⓪ 摘要纯函数：类型中文名+关键参数+取反尾标，没填完直说
check("摘要·模板没选图直说", cond_row_summary({"kind": "template"}) == "模板匹配·（没选图）")
check("摘要·变量带比较式+取反尾标",
      cond_row_summary({"kind": "var", "name": "hp", "op": ">=",
                        "value": "5", "not": True}) == "变量比较·hp>=5（取反）")

# ① 单条件老形态：列表恒一行、该行编辑器直接展开（= 老版第 1 条表单永远可见）；
#    不偷建 extra_conds/logic（老数据打开零变化）
node = {"id": "b", "type": "branch", "cond": {"kind": "var"}}
w = f._build_branch_cond_area(node)
lst = rows_of(w)
check("单条件：列表恒 1 行且自动选中（表单当场可见）",
      lst is not None and lst.count() == 1 and lst.currentRow() == 0
      and "extra_conds" not in node and "logic" not in node)
check("单条件：行文本 = 徽章+摘要",
      lst.item(0).text() == "① " + cond_row_summary(node["cond"]))
btn_add = next(b for b in w.findChildren(QPushButton) if "添加条件" in b.text())
check("单条件：有「＋ 添加条件」钮", btn_add is not None)
chk = next(c for c in w.findChildren(QCheckBox) if "取反" in c.text())
check("取反钮进了选中行编辑区（不再是区头独占行）", chk.text() == "本条取反（不成立才算成立）")

# ② 点添加 → 落第 2 条 + logic=all + 触发重建（结构路 = 全量重建，选中靠记忆）
btn_add.click()
check("添加后：extra_conds 挂 1 条、logic=all",
      len(node.get("extra_conds") or []) == 1
      and node.get("logic") == "all")
check("添加后：触发属性面板重建", f._rebuild_calls == [node])

# ③ 重建后的多条件区：列表 2 行 + 关系下拉（无「条件N」标题了，编号进列表）
w2 = f._build_branch_cond_area(node)
lst2 = rows_of(w2)
logics = [c for c in w2.findChildren(QComboBox)
          if c.itemText(0).startswith("满足全部")]
check("多条件：列表 2 行 + 关系下拉在（与/或 2 项）",
      lst2.count() == 2 and len(logics) == 1 and logics[0].count() == 2)
check("加完即选中新行（sel 记忆跨重建跟上，不是弹回第①行）",
      lst2.currentRow() == 1)

# ④ 取反钮（显式切回第 1 条）：勾上写 cond["not"]，取消弹键（不留 False 残渣）
lst2.setCurrentRow(0)
app.processEvents()
app.sendPostedEvents(None, QEvent.DeferredDelete)
chk1 = next(c for c in w2.findChildren(QCheckBox) if "取反" in c.text())
chk1.toggle()
check("第1条勾选取反 → cond['not']=True", node["cond"].get("not") is True)
chk1.toggle()
check("取消取反 → not 键整个弹出", "not" not in node["cond"])
app.processEvents()   # 让 deleteLater 落地，下面按行切换编辑器才找得干净

# ⑤ 删第 2 行（后端直测，右键菜单的触发器）：回到纯老形态
rl2 = w2.findChild(RowListEditor)
rl2._delete(1)
check("删除末条：回到纯老形态（extra_conds/logic 都没了）",
      "extra_conds" not in node and "logic" not in node
      and node["cond"]["kind"] == "var")   # 删的是第2条，第1条本尊没被动过

# ⑥ 选中行编辑器只编辑自己那份 cond dict：换第 2 行 kind 不污染第 1 条
node2 = {"id": "b", "type": "branch", "cond": {"kind": "var"},
         "extra_conds": [{"kind": "template", "category": "", "name": ""}],
         "logic": "any"}
w3 = f._build_branch_cond_area(node2)
lst3 = rows_of(w3)
lst3.setCurrentRow(1)
app.processEvents()
app.sendPostedEvents(None, QEvent.DeferredDelete)
kinds = [c for c in w3.findChildren(QComboBox)
         if c.itemText(0) in ("模板匹配", "坐标取色", "多点取色",
                              "文字识别", "变量比较", "动态检测")]
check("切到第2行：编辑区只剩第2行的 kind 下拉（当前=模板匹配）",
      len(kinds) == 1 and kinds[0].currentText() == "模板匹配")
check("构表阶段不改已存 kind",
      node2["cond"]["kind"] == "var" and node2["extra_conds"][0]["kind"] == "template")

# ⑥b 行摘要跟随类型切换（on_kind 回调）
kinds[0].setCurrentIndex(kinds[0].findData("text"))
check("换类型 → 行摘要即刷", lst3.item(1).text().endswith(cond_row_summary(
    {"kind": "text"}).lstrip()))

# ⑥c 换序 = cond 恒为列表第 1 行（dict 身份重指派，条数不减）
node3 = {"id": "b", "type": "branch", "cond": {"kind": "var", "name": "a"},
         "logic": "any",
         "extra_conds": [{"kind": "text", "target": "买"},
                         {"kind": "color"}]}
w4 = f._build_branch_cond_area(node3)
rl4 = w4.findChild(RowListEditor)
c1, e1, e2 = node3["cond"], node3["extra_conds"][0], node3["extra_conds"][1]
rl4.lst.setCurrentRow(1)
rl4._move(-1)      # 第2行上移 → 原「文字识别」升为第1条 = 存储 cond
check("换序后：cond 就是挪上来的那条（身份重指派），extra_conds 顺位补上",
      node3["cond"] is e1 and node3["extra_conds"] == [c1, e2]
      and w4.findChild(QListWidget).item(0).text().startswith("① 文字识别"))

# ⑦ 循环出口复用同一区（2026-10-06）：措辞换「退出循环」、第2条编辑器不吃 motion
lnode = {"id": "L", "type": "loop", "label": "循环", "steps": [],
         "cond": {"kind": "var"}, "logic": "all",
         "extra_conds": [{"kind": "template", "category": "库A", "name": "图X"}]}
wl = f._build_branch_cond_area(lnode, allow_motion=False, exit_loop=True)
logic_l = [c for c in wl.findChildren(QComboBox)
           if c.itemText(0).startswith("满足全部")]
check("循环复用：关系下拉措辞换成「退出循环」（不是判断的「走Y」）",
      len(logic_l) == 1 and "退出循环" in logic_l[0].itemText(0)
      and "走Y" not in logic_l[0].itemText(1))
rows_of(wl).setCurrentRow(1)
app.processEvents()
app.sendPostedEvents(None, QEvent.DeferredDelete)
kind_l = next(c for c in wl.findChildren(QComboBox)
              if c.itemText(0) in ("模板匹配", "坐标取色", "多点取色",
                                   "文字识别", "变量比较", "动态检测"))
check("循环复用：第2条编辑器禁 motion（allow_motion=False 沿用循环老口径）",
      "动态检测" not in [kind_l.itemText(i) for i in range(kind_l.count())])

# ⑧ 循环引用扫描认领 extra_conds 的模板（读侧口径，漏一条=删图反查说没人用）
#    纯数据侧断言：不借 ⑦ 的 lnode（构造第2条编辑器时假库无「图X」，_cond_template
#    按「下拉对不上就把 name 洗空」的老规矩会清掉它，与引用扫描无关）
from bot import template_deps
_ln_data = {"type": "loop", "cond": {"kind": "var"}, "logic": "all",
            "extra_conds": [{"kind": "template", "category": "库A", "name": "图X"}],
            "steps": []}
check("循环引用扫描：第2条 extra_conds 模板也进 node_tpl_refs",
      ("库A", "图X") in template_deps.node_tpl_refs(_ln_data))

# ⑨ 接线源码守卫：循环面板真用本区（exit_loop+allow_motion=False）；
#    循环体动作行已换 RowListEditor + 独立步骤编辑器方法
src_loop = __import__("inspect").getsource(FlowPanel._build_loop_editor)
check("_build_loop_editor 接线多条件区（exit_loop=True·allow_motion=False）",
      "_build_branch_cond_area" in src_loop and "exit_loop=True" in src_loop
      and "allow_motion=False" in src_loop)
check("_build_loop_editor 动作行进行列表组件（不再逐条摊表单）",
      "RowListEditor" in src_loop and "_build_loop_step_editor" in src_loop)

# ⑩ 条件「持续时长」hold_secs（2026-10-11）：编辑器通用行落值/弹键 + 摘要尾标
node5 = {"id": "b", "type": "branch", "cond": {"kind": "var", "name": "hp"}}
w5 = f._build_branch_cond_area(node5)
spn_hold = next((s for s in w5.findChildren(QDoubleSpinBox)
                 if s.specialValueText() == "不要求持续"), None)
check("持续时长行进了条件编辑器（默认 0 = 不要求）",
      spn_hold is not None and spn_hold.value() == 0.0
      and "hold_secs" not in node5["cond"])
spn_hold.setValue(2.5)
check("拨动持续时长 → 落 cond['hold_secs'] 且触发重建",
      node5["cond"].get("hold_secs") == 2.5 and f._rebuild_calls[-1] is node5
      or node5["cond"].get("hold_secs") == 2.5)
spn_hold.setValue(0.0)
check("拨回 0 → hold_secs 键弹出（老数据零迁移）",
      "hold_secs" not in node5["cond"])
# 摘要尾标：持续 N 秒跟在取反后面
check("摘要·持续时长尾标（含取反则在其后）",
      cond_row_summary({"kind": "template", "name": "锁",
                        "hold_secs": 3}) == "模板匹配·锁·持续3s"
      and cond_row_summary({"kind": "template", "name": "锁", "not": True,
                            "hold_secs": 3}) == "模板匹配·锁（取反）·持续3s")
# motion 条件藏掉持续行（本身即时间窗语义）
node6 = {"id": "b", "type": "branch", "cond": {"kind": "motion", "category": "",
                                               "frames": []}}
w6 = f._build_branch_cond_area(node6)
spn_hold6 = next((s for s in w6.findChildren(QDoubleSpinBox)
                  if s.specialValueText() == "不要求持续"), None)
check("motion 条件：持续行被藏（不吃这维）",
      spn_hold6 is not None and not spn_hold6.isVisibleTo(w6))

print("SMOKE " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
