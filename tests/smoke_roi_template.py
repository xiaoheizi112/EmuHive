# 冒烟：区域跟引用走（判断模板条件 / 等图 / 点模板 的「限定搜索区域」）
# 守四条（2026-09-28 新增，同一张图多区域判定）：
# ① 存量零污染：老节点打开面板不写 roi 键；勾选才落 [0,0,540,960]，取消 = None；
# ② 框选路由：按钮 → begin_roi_pick(target) → route_roi 回填 [x,y,x+w,y+h]；
#    判断条件回填进 cond 自己（不脏节点），且重建宿主节点表单；
# ③ 识字取物共用 _roi_block 后原链路不坏（勾选/摘要仍在）；
# ④ 校验条：框比模板图小 → 点名「永远认不中」；没框/够大 → 不报。
# 引擎侧透传由 `python -m bot.tasks.flow` 自检 10h 段守，这里只管面板↔数据。
# 不真存盘（红线：临时配置单例 + autosave 关；账本只读不写）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_roi_template.py
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
from PySide6.QtWidgets import (QApplication, QCheckBox, QLabel, QPushButton)

app = QApplication.instance() or QApplication(sys.argv)

import ui.flow_editor as fe
from ui.flow_editor import FlowPanel

p = FlowPanel()
p.chk_autosave.setChecked(False)
# 离屏无预览外壳：begin_roi_pick 遇 _view=None 拒发（"预览未就绪"）。
# 垫一个只挨 draw_mode/roi_owner 两个属性赋值的假 view，路由链路照测。
p._view = type("_V", (), {})()

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


def fill(node):
    p._rebuild_props(node)
    app.processEvents()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def chk_roi_in(widget):
    return next((c for c in widget.findChildren(QCheckBox)
                 if c.text().startswith("限定搜索区域")
                 and c.isVisibleTo(p.props_panel)), None)


# ① 等图老节点：打开零污染
node = {"id": "w", "type": "wait_template", "category": "x", "name": "t"}
fill(node)
check("① 老等图节点：不偷写 roi 键", "roi" not in node)
chk = chk_roi_in(p.props_panel)
check("① 等图面板有「限定搜索区域」勾选（未勾）", chk is not None and not chk.isChecked())
sum_lbl = next(l for l in p.props_panel.findChildren(QLabel)
               if "跟随模板默认区域" in l.text() and l.isVisibleTo(p.props_panel))
check("① 摘要直说未框=跟随模板默认（不装已框）", "未框" in sum_lbl.text())
chk.toggle()
check("① 勾选 → roi 落默认全屏坐标", node.get("roi") == [0, 0, 540, 960])
chk.toggle()
check("① 取消 → roi=None（编译侧回落账本）", node.get("roi") is None)

# ② 框选路由：点按钮发起 → route_roi 回填 [x,y,x+w,y+h]
chk.toggle()   # 先勾上（等价用户路径）
btn = next(b for b in p.props_panel.findChildren(QPushButton)
           if "在预览上框选（搜索区域）" in b.text()
           and b.isVisibleTo(p.props_panel))
btn.click()
check("② 框选钮挂到本节点", p._roi_pick_node is node)
p.route_roi(10, 20, 30, 40)
check("② 成框回填左上右下（10,20→40,60）", node.get("roi") == [10, 20, 40, 60])

# ②b 判断模板条件：区域进 cond 不进节点，重建宿主表单
bnode = {"id": "b", "type": "branch",
         "cond": {"kind": "template", "category": "x", "name": "锁"}}
fill(bnode)
bchk = chk_roi_in(p.props_panel)
check("②b 判断模板条件有同款勾选", bchk is not None)
bchk.toggle()
check("②b 勾选落进 cond、节点本体不脏",
      bnode["cond"].get("roi") == [0, 0, 540, 960] and "roi" not in bnode)
bbtn = next(x for x in p.props_panel.findChildren(QPushButton)
            if "在预览上框选（搜索区域）" in x.text()
            and x.isVisibleTo(p.props_panel))
bbtn.click()
check("②b 条件框选 target=cond（owner=宿主节点）",
      p._roi_pick_node is bnode["cond"])
p.route_roi(100, 200, 50, 60)
check("②b 成框回填 cond['roi']", bnode["cond"].get("roi") == [100, 200, 150, 260])

# ③ 识字取物共用 _roi_block 后原链路不坏
snode = {"id": "s", "type": "server_pick", "mode": "click", "roi": None}
fill(snode)
s_chk = next((c for c in p.props_panel.findChildren(QCheckBox)
              if c.text().startswith("限定识别区域")
              and c.isVisibleTo(p.props_panel)), None)
check("③ server_pick 勾选壳仍在（升格没弄丢）", s_chk is not None)
s_btn = next((b for b in p.props_panel.findChildren(QPushButton)
              if "在预览上框选（识别区域）" in b.text()
              and b.isVisibleTo(p.props_panel)), None)
check("③ server_pick 框选钮仍挂节点本体", s_btn is not None)

# ④ 校验条：框比模板图小 → 点名
_real_find = fe.template_manager.find_template
fe.template_manager.find_template = \
    lambda c, n: {"width": 100, "height": 100, "roi": None}
try:
    p.graph = {"nodes": [
        {"id": "w1", "type": "wait_template", "category": "x", "name": "t",
         "roi": [0, 0, 50, 50]},                      # 装不下 → 报
        {"id": "w2", "type": "wait_template", "category": "x", "name": "t",
         "roi": [0, 0, 120, 120]},                    # 够大 → 不报
        {"id": "b1", "type": "branch", "cond": {"kind": "template",
         "category": "x", "name": "t", "roi": [0, 0, 30, 30]}},   # 条件装不下 → 报
        {"id": "w3", "type": "wait_template", "category": "x", "name": "t"},
    ], "edges": []}
    p._refresh_validation()
    txt = "；".join(p._val_problems)
    seg = txt[txt.find("框选区域比模板图小"):] if "框选区域比模板图小" in txt else ""
    check("④ 节点框太小被点名（带尺寸账）",
          "框 50×50 < 模板 100×100" in seg)
    check("④ 判断条件框太小也被点名（30×30 + 模板条件字样）",
          "模板条件" in seg and "框 30×30 < 模板 100×100" in seg)
    check("④ 够大/没框的不误报（一条问题串收全部）",
          txt.count("框选区域比模板图小") == 1 and "120×120" not in seg)
finally:
    fe.template_manager.find_template = _real_find
    p.graph = {"nodes": [], "edges": []}

print("-" * 46)
if not ok:
    print("SMOKE FAIL")
    sys.exit(1)
print("smoke roi-template ALL OK")
