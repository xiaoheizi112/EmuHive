# 冒烟：判断节点「认不出时 → 滑动翻页继续找」面板链路（2026-09-28 新增）
# 守四条：
# ① 默认形态零污染：老 branch 节点打开面板不写 on_timeout/scroll_* 任何键；
# ② 切「滑动翻页继续找」才落 on_timeout="scroll"、切回整个弹出（不留 "fail" 残渣）；
# ③ 滑动参数行与识字取物共用 _scroll_editor：改距离/次数直写 scroll_px/scroll_max；
# ④ 构建期无父不闪孤儿窗（_vis 铁律）+ 面板最小宽 ≤318 视口。
# 引擎侧语义由 `python -m bot.tasks.flow` 自检 10g 段守，这里只管面板↔数据。
# 不真存盘（红线：临时配置单例 + autosave 关）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_branch_scroll_panel.py
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

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QLabel, QSpinBox)

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowPanel

p = FlowPanel()
p.chk_autosave.setChecked(False)

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


def fill(node):
    """走正式重建入口（同 smoke_server_pick_panel：清旧表单 + 脏标记闸门），
    DeferredDelete 强制回收，findChildren 才只捞当前这张面板。"""
    p._rebuild_props(node)
    app.processEvents()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


class _OrphanSpy(QObject):
    def __init__(self):
        super().__init__()
        self.hits = []

    def eventFilter(self, o, e):
        if (e.type() == QEvent.Show and o.isWidgetType()
                and o.parent() is None and o is not p):
            self.hits.append(type(o).__name__)
        return False


spy = _OrphanSpy()
app.installEventFilter(spy)


def miss_combo():
    return next(c for c in p.props_panel.findChildren(QComboBox)
                if c.findData("fail") >= 0 and c.isVisibleTo(p.props_panel))


# ① 老节点（无任何新键）：面板亮出「认不出时」但默认走N，一个键都不许写
node = {"id": "b", "type": "branch", "cond": {"kind": "var"}}
fill(node)
check("① 默认形态：不偷写 on_timeout/scroll_* （存量图打开零变化）",
      "on_timeout" not in node and not any(k.startswith("scroll_") for k in node))
cmb = miss_combo()
check("① 下拉两项：超时走 N 口 / 滑动翻页继续找",
      cmb.count() == 2 and cmb.currentData() == "fail")
hint = next(l for l in p.props_panel.findChildren(QLabel)
            if "滑了设定次数" in l.text())
check("① 默认形态滑动参数行藏好", not hint.isVisibleTo(p.props_panel))

# ② 切到滑动 → 落键 + 行显出；切回 → 键整个弹出
cmb.setCurrentIndex(cmb.findData("scroll"))
app.processEvents()
check("② 切滑动：on_timeout=scroll 落节点、参数行显出",
      node.get("on_timeout") == "scroll" and hint.isVisibleTo(p.props_panel))
check("② 仅切换不动参数：scroll_* 仍不偷写（编译侧默认兜）",
      not any(k.startswith("scroll_") for k in node))
cmb.setCurrentIndex(cmb.findData("fail"))
check("② 切回默认：on_timeout 整个弹出（不留 fail 残渣）",
      "on_timeout" not in node)

# ③ 参数行共用 _scroll_editor：改数字直写 scroll_* 键
node2 = {"id": "b2", "type": "branch", "cond": {"kind": "var"},
         "on_timeout": "scroll"}
fill(node2)
spx = [s for s in p.props_panel.findChildren(QSpinBox)
       if s.suffix() == "px" and s.isVisibleTo(p.props_panel)]
smax = [s for s in p.props_panel.findChildren(QSpinBox)
        if s.suffix() == "次" and s.isVisibleTo(p.props_panel)]
check("③ 滑动行在场（距离/次数框各一，与识字取物同款）",
      len(spx) == 1 and len(smax) == 1)
spx[0].setValue(420)
smax[0].setValue(7)
check("③ 改参数直写节点 scroll_px/scroll_max",
      node2.get("scroll_px") == 420 and node2.get("scroll_max") == 7)
# 已存 scroll_* 的节点重开面板：初值读回（不被默认值顶掉）
fill(node2)
spx2 = next(s for s in p.props_panel.findChildren(QSpinBox)
            if s.suffix() == "px" and s.isVisibleTo(p.props_panel))
check("③ 重开面板初值读回 scroll_px=420", spx2.value() == 420)

# ⑤ 限定滑动区域勾选框：不勾 = scroll_roi 无键（全屏滑，老行为）；
#    勾上落默认全屏框 [0,0,540,960]，取消 = None（编译侧兜全屏）
roi_chk = next(c for c in p.props_panel.findChildren(QCheckBox)
               if "限定滑动区域" in c.text() and c.isVisibleTo(p.props_panel))
check("⑤ 滑动节点：限定滑动区域勾选框在场且默认不勾",
      roi_chk.isChecked() is False and "scroll_roi" not in node2)
roi_chk.setChecked(True)
check("⑤ 勾上落 scroll_roi 全屏框", node2.get("scroll_roi") == [0, 0, 540, 960])
roi_chk.setChecked(False)
check("⑤ 取消勾 scroll_roi 置 None", node2.get("scroll_roi") is None)

# ④ 构建期孤儿窗守卫 + 宽度
check("④ scroll 节点重建全程：不闪无父孤儿窗", not spy.hits)
mw = p.props_panel.minimumSizeHint().width()
check(f"④ branch+滑动 面板最小宽 {mw} ≤ 318 视口", mw <= 318)
# 识字取物侧回归挪门：共用 helper 后 3 项下拉仍在（详细语义由
# smoke_server_pick_panel 守，这里只兜「helper 化没弄丢 server_pick」）
fill({"id": "s", "type": "server_pick", "mode": "click",
      "on_timeout": "scroll", "roi": None})
app.processEvents()
sp_cmb = next((c for c in p.props_panel.findChildren(QComboBox)
               if c.findData("exit") >= 0 and c.isVisibleTo(p.props_panel)), None)
check("server_pick「认不出时」三项下拉没被 helper 化弄丢",
      sp_cmb is not None and sp_cmb.count() == 3)

print("-" * 46)
if not ok:
    print("SMOKE FAIL")
    sys.exit(1)
print("smoke branch-scroll-panel ALL OK")
