# 冒烟：点模板「点全部」链路 —— 属性面板字段往返 + 编译层分流。
# 断言四件事：老节点（无 all_hits）面板出勾选框但默认关、附属两行置灰；
# 勾选/数值落节点 dict 且重载回填；编译层 all_hits=True 走 TapAllStep、
# 老节点恒不沾（防默认值漂移把存量流程悄悄换成点全部）。
# 不真存盘（红线：临时配置单例 + autosave 关），不碰真模板账本（_tpl_missing 打桩）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_tap_all_panel.py
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
from PySide6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox,
                               QSpinBox)

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowPanel
from bot.tasks import flow as F

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


p = FlowPanel()
p.chk_autosave.setChecked(False)


def fill(node):
    """走正式重建入口（同 smoke_server_pick_panel 的纪律：清了再找，
    offscreen 下须手动跑 DeferredDelete，不然抓到上张面板的残留控件）。"""
    p._rebuild_props(node)
    app.processEvents()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def _chk(panel):
    return next((c for c in panel.findChildren(QCheckBox)
                 if "点全部" in c.text() and c.isVisibleTo(panel)), None)


def _sp_max(panel):
    # 本表单里范围 1..50 的整数框独此一家（偏移±539 / 抖动 0..20 都不撞）
    return next((s for s in panel.findChildren(QSpinBox)
                 if s.minimum() == 1 and s.maximum() == 50
                 and s.isVisibleTo(panel)), None)


def _sp_gap(panel):
    return next((s for s in panel.findChildren(QDoubleSpinBox)
                 if s.suffix() == " s" and s.isVisibleTo(panel)), None)


# ① 老节点（没存过 all_hits）：勾选框在、默认关、两行置灰
n1 = {"id": "c1", "type": "click_template", "category": "", "name": "x",
      "label": ""}
fill(n1)
chk, smax, sgap = _chk(p.props_panel), _sp_max(p.props_panel), _sp_gap(p.props_panel)
check("老节点面板出「点全部」勾选框且默认关",
      chk is not None and not chk.isChecked())
check("未勾选时 最多点数/点击间隔 置灰",
      smax is not None and sgap is not None
      and not smax.isEnabled() and not sgap.isEnabled())
check("构表阶段不写 all_hits（老节点 dict 零污染）", "all_hits" not in n1)

# ② 勾开 → 落节点 + 两行解禁 + 数值往返
chk.setChecked(True)
check("勾选 → all_hits=True 落节点", n1.get("all_hits") is True)
check("勾选后两行解禁", smax.isEnabled() and sgap.isEnabled())
smax.setValue(7)
sgap.setValue(0.5)
check("最多点数/间隔 落节点",
      n1.get("all_hits_max") == 7 and abs(n1.get("all_hits_gap") - 0.5) < 1e-9)
chk.setChecked(False)
check("取消勾选 → all_hits=False 落节点", n1.get("all_hits") is False)

# ③ 已开点全的节点重载：回填不丢
n2 = {"id": "c2", "type": "click_template", "category": "", "name": "x",
      "label": "", "all_hits": True, "all_hits_max": 12, "all_hits_gap": 1.5}
fill(n2)
chk2, smax2, sgap2 = _chk(p.props_panel), _sp_max(p.props_panel), _sp_gap(p.props_panel)
check("重载回填：勾选/12 处/1.5s",
      chk2.isChecked() and smax2.value() == 12
      and abs(sgap2.value() - 1.5) < 1e-9 and smax2.isEnabled())

# ④ 编译层分流（_tpl_missing 打桩 False：临时账本空，不桩会一律降级缺图步）
_real_missing = F._tpl_missing
F._tpl_missing = lambda c, n: False
try:
    base = {"type": "click_template", "category": "c", "name": "x",
            "label": "", "delay_after_click": False}
    s_all = F._action_step({**base, "all_hits": True, "all_hits_max": 7,
                            "all_hits_gap": 0.5, "wait_timeout": 8})
    check("all_hits=True 编译成 TapAllStep", isinstance(s_all, F.TapAllStep))
    check("参数落步骤：max 7 / 间隔 0.5 / 门 8s",
          s_all.max_hits == 7 and abs(s_all.gap - 0.5) < 1e-9
          and s_all.gate_timeout == 8)
    s_old = F._action_step(dict(base))
    check("老节点（无 all_hits）编译结果不沾 TapAllStep",
          not isinstance(s_old, F.TapAllStep))
    s_off = F._action_step({**base, "all_hits": False})
    check("all_hits=False 显式存 False 也不走点全",
          not isinstance(s_off, F.TapAllStep))
    s_nw = F._action_step({**base, "all_hits": True, "wait_before": False})
    check("关前置门 → gate_timeout=0（零命中当场收口档）",
          isinstance(s_nw, F.TapAllStep) and s_nw.gate_timeout == 0)
finally:
    F._tpl_missing = _real_missing

print("TAP-ALL PANEL SMOKE PASS" if ok else "TAP-ALL PANEL SMOKE FAIL")
sys.exit(0 if ok else 1)
