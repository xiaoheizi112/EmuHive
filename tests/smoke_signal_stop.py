# 冒烟：「发信号 → 收到即结束」界面链路（2026-10-06）
# 引擎语义（信箱销账/循环每拍闸/遍历项边界闸/编译透传）由
#   python -m bot.tasks.flow 第 20 段守；本场只守界面：
# ① 三注册（TYPE_LABEL/调色板分组/TYPE_COLOR/new_node 默认）
# ② 发信号节点面板：信号名 QLineEdit 往返（name 是信号名不是图名）
# ③ 循环面板 stop_on 名单：添加钮落字典行 → 行改名 → 删光 pop 键（不建闸=零迁移）
# ④ 遍历全部面板同款名单在场
# ⑤ 循环体动作行：下拉有「发信号」+ 换型落 dict + 行摘要文本
# ⑥ 校验条：没填信号名点名；填好闭嘴
# ⑦ 画布尾标/直通出口 源码守卫（paint 挂「·停N」、_ports 走默认 in/out）
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_signal_stop.py
import inspect
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

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPushButton

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import (NODE_GROUPS, TYPE_COLOR, TYPE_LABEL, FlowNodeItem,
                            FlowPanel, RowListEditor, new_node,
                            step_row_summary)

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


p = FlowPanel()
p.chk_autosave.setChecked(False)


def fill(node):
    """走正式重建入口（offscreen 纪律：清了再找，DeferredDelete 手动跑，
    不然抓到上张面板的残留控件）。"""
    p._rebuild_props(node)
    app.processEvents()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def find_ed():
    """面板里「信号名」输入框（占位符认领，备注框不掺和），isVisibleTo 过滤残留。"""
    return next((e for e in p.props_panel.findChildren(QLineEdit)
                 if "信号名" in e.placeholderText() and e.isVisibleTo(p.props_panel)),
                None)


def find_add(text):
    return next((b for b in p.props_panel.findChildren(QPushButton)
                 if text in b.text() and b.isVisibleTo(p.props_panel)), None)


def row_editor_of(btn):
    w = btn.parent()
    while w is not None and not isinstance(w, RowListEditor):
        w = w.parent()
    return w


# ① 三注册
ctl = next(g for n, g in NODE_GROUPS if n == "流程控制")
check("① 注册：中文名/调色板分组/颜色都在",
      TYPE_LABEL.get("signal") == "发信号" and "signal" in ctl
      and "signal" in TYPE_COLOR)
nn = new_node("signal")
check("① 注册：新节点默认带空 name 且无模板字段",
      nn["type"] == "signal" and nn["name"] == "" and "category" not in nn)

# ② 发信号面板：信号名往返（建表阶段不许无中生有写键之外——name 是新节点默认键，
#    手搭老节点没有该键时面板只读不偷建：改过字才落）
n2 = {"id": "s1", "type": "signal", "label": "", "name": ""}
fill(n2)
ed = find_ed()
check("② 发信号面板出「信号名」输入框", ed is not None)
ed.setText("收工")
check("② 改名即时落节点 dict", n2["name"] == "收工")

# ③ 循环 stop_on：加行→改名→删光 pop 键
n3 = {"id": "L", "type": "loop", "label": "循环", "steps": [], "max_times": 0}
fill(n3)
b_add = find_add("添加停止信号")
check("③ 循环面板有「添加停止信号」钮", b_add is not None)
check("③ 只开面板不偷建 stop_on 键", "stop_on" not in n3)
b_add.click()
app.processEvents()
check("③ 加行=字典行 [{name:''}]（RowListEditor 身份要求）",
      n3.get("stop_on") == [{"name": ""}])
fill(n3)   # on_struct 整场重建后回位验证：行还在、选中行编辑器可直接改
ed3 = find_ed()
check("③ 重建后行编辑器在（选中记忆回位）", ed3 is not None)
ed3.setText("封号")
check("③ 行改名写进存储本尊", n3["stop_on"][0]["name"] == "封号")
fill(n3)
ed_del = find_add("添加停止信号")
rle = row_editor_of(ed_del)
rle._delete(0)
app.processEvents()
check("③ 删光自动 pop 键（空名单=不建闸=老形态零迁移）",
      "stop_on" not in n3)

# ③b 老形态纯字符串行（AI 手搭 ["封号"]）：面板不炸、名单照显（读侧两种都认）
n3b = {"id": "L2", "type": "loop", "label": "循环", "steps": [],
       "max_times": 0, "stop_on": ["封号"]}
fill(n3b)
check("③b 纯字符串 stop_on 行载入不炸（面板在场即可）",
      find_add("添加停止信号") is not None)

# ④ 遍历全部面板：同款名单在场
n4 = {"id": "M", "type": "multi_scan", "label": "", "category": "",
      "name": "探.png", "mode": "tap"}
fill(n4)
check("④ 遍历面板出「添加停止信号」", find_add("添加停止信号") is not None)
check("④ 开遍历面板不给循环节点偷挂键", "stop_on" not in n4)

# ⑤ 循环体动作行：下拉有发信号 → 换型落 dict → 行摘要
n5 = {"id": "L3", "type": "loop", "label": "循环", "steps": [], "max_times": 0}
fill(n5)
b_step = find_add("添加动作")
b_step.click()
app.processEvents()
fill(n5)
cmb_step = next((c for c in p.props_panel.findChildren(QComboBox)
                 if c.findData("signal") >= 0 and c.isVisibleTo(p.props_panel)),
                None)
check("⑤ 循环体类型下拉有「发信号」", cmb_step is not None)
cmb_step.setCurrentIndex(cmb_step.findData("signal"))
app.processEvents()
row = n5["steps"][0]
check("⑤ 换型原位换成 signal 行（带空 name）",
      row.get("type") == "signal" and row.get("name") == "")
check("⑤ 画布动作条摘要：发信号·（没填信号名）",
      step_row_summary(row) == "发信号·（没填信号名）")
row["name"] = "收工"
check("⑤ 摘要随行名跟新", step_row_summary(row) == "发信号·收工")

# ⑥ 校验条：没填信号名点名、填好闭嘴
p.graph = {"version": 2,
           "nodes": [{"id": "s", "type": "signal", "label": "掐循环", "name": ""}],
           "edges": []}
p._refresh_validation()
# isHidden()=本控件显隐标志（不看父链，offscreen 下面板从不 show 也准）：
# 问题分支走 setVisible(True)、干净分支走 setVisible(False)（干净不清 text 是全场常态）
check("⑥ 空名发信号 → 校验条亮且点名",
      not p.lbl_valid.isHidden() and "没填信号名" in p.lbl_valid.text())
p.graph["nodes"][0]["name"] = "收工"
p._refresh_validation()
check("⑥ 填好名 → 校验条收起（不再点名）", p.lbl_valid.isHidden())

# ⑦ 语义守卫（2026-10-09 冒烟减负：原同段"paint 尾标·停N""循环体表单接线"两条
#   已摘——断链表现为尾标缺失/表单缺失，真机一眼可见；留这条"直通语义"守的是
#   端口行为漂移，肉眼看不见）：发信号走默认 in/out 端口（直通语义）
ports_src = inspect.getsource(FlowNodeItem._ports)
check("⑦ signal 不在 _ports 特判名单（吃默认 in/out = 发完直通）",
      '"signal"' not in ports_src)

print("ALL OK" if ok else "HAS FAIL")
sys.exit(0 if ok else 1)
