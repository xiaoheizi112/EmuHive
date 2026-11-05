# 冒烟：监视行有效性 A+B（2026-10-05 新增）
# A =「▶测这行」：编辑器发 request_row_test(目录,图名)，结果经 apply_row_result
#     回投信息行（换行/重建仍显示）；B = 每行命中统计：WatchStep 命中一笔进
#     bot/watch_stats 内存账本，列表行尾 ×N、编辑区「命中 ×N · 最近X前」。
# 附带：账本 click_offset 的「跟随图」提示、3s 定时器、_clear_props 摘钩、
#       同行去抖后 _lbl_ref 不被摘（改行再刷统计仍能投到活控件）、workbench 接线守卫。
# 红线：临时配置单例 + 临时模板库（重定向 TEMPLATES_DIR/INDEX_PATH），不碰真实资产。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_watch_validity.py
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np

import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

from core import template_manager as tm

base = Path(tempfile.mkdtemp())
tm.TEMPLATES_DIR = base / "templates"
tm.INDEX_PATH = tm.TEMPLATES_DIR / "index.json"
img = np.zeros((10, 20, 3), np.uint8)
tm.add_template("imgA", "offlib", img, 0.9, click_offset=[5, -2])
tm.add_template("imgB", "offlib", img, 0.9)

from PySide6.QtCore import QEvent, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QListWidget, QPushButton

app = QApplication.instance() or QApplication(sys.argv)

from bot import watch_stats
from ui.flow_editor import FlowPanel

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    # offscreen 嵌套循环不跑 DeferredDelete：强制回收旧编辑区控件，
    # 否则 findChildren 捞到上张面板的隐藏残留（server_pick 同款实测坑）
    app.sendPostedEvents(None, QEvent.DeferredDelete)


# ── ① watch_stats 纯单元（刻意用别的键：别污染 ③ 界面链路读的 ("SmokeFlow","w1")）──
KEY = ("UnitFlow", "u1")
watch_stats._hits.clear()
watch_stats.record(None, 0)                      # 没登记键 = 静默不记
check("record(None) 不落账", not watch_stats._hits)
watch_stats.record(KEY, 1)
watch_stats.record(KEY, 1)
snap = watch_stats.snapshot(KEY)
check("两次命中同一行 → (2, ts)",
      1 in snap and snap[1][0] == 2 and snap[1][1] > 0)
snap[1] = (999, 0.0)
check("snapshot 是拷贝（外改不脏账本）", watch_stats.snapshot(KEY)[1][0] == 2)

# ── ② WatchStep 命中入账（引擎侧，打桩 template_present/tap_template）──
import bot.tasks.flow as fl


class FakeCtx:
    log = None
    frame = None


_orig_tp, _orig_tap = fl.template_present, fl.tap_template
fl.template_present = lambda ctx, cat, nm, roi=None: nm == "imgA"
fl.tap_template = lambda *a, **k: True
try:
    step = fl.WatchStep("w", rows=[{"category": "offlib", "name": "imgB", "kind": "tap"},
                                   {"category": "offlib", "name": "imgA", "kind": "tap"}],
                        bodies=[object(), object()],
                        stats_key=("EngineFlow", "n9"))
    step.tick(FakeCtx())
    s2 = watch_stats.snapshot(("EngineFlow", "n9"))
    check("哨兵 tick：命中行 ②（下标1）入账一笔", s2.get(1, (0,))[0] == 1)
    check("没命中的行 ①不入账", 0 not in s2)
finally:
    fl.template_present, fl.tap_template = _orig_tp, _orig_tap

# ── ③ 界面链路 ──
cm.get_config().set("custom_flows", {"SmokeFlow": {"version": 2, "nodes": [
    {"id": "w1", "type": "watch", "x": 0, "y": 0, "label": "",
     "rows": [{"category": "offlib", "name": "imgA", "kind": "tap"},
              {"category": "offlib", "name": "imgB", "kind": "subflow",
               "flow_name": ""},
              {"category": "", "name": "", "kind": "tap"}],
     "cooldown_secs": 5, "fail_limit": 3},
    {"id": "t2", "type": "timer", "x": 600, "y": 0, "label": "",
     "rows": [{"interval_min": 5, "flow_name": ""}],
     "cooldown_secs": 5, "fail_limit": 3},
], "edges": []}})

panel = FlowPanel()
panel.chk_autosave.setChecked(False)
panel.resize(1400, 900)
panel.show()
wait(300)
panel._select_flow("SmokeFlow")
wait(300)

from ui.flow_editor import FlowNodeItem
panel.scene.clearSelection()
wit = next(it for it in panel.scene.items() if isinstance(it, FlowNodeItem)
           and it.node.get("id") == "w1")
wit.setSelected(True)
wait(200)
node = wit.node
lst = panel.props_panel.findChild(QListWidget)
check("监视 3 行摘要列表", lst is not None and lst.count() == 3)


# ⚠ 找控件必须过滤可见性：嵌套 QEventLoop 不处理 DeferredDelete，
#   旧编辑区控件 hide+deleteLater 后仍是 findChildren 活幽灵（server_pick 同款坑）。
def labels_with(sub):
    return [l for l in panel.props_panel.findChildren(QLabel)
            if sub in l.text() and l.isVisibleTo(panel)]


def click_test_btn():
    b = next(x for x in panel.props_panel.findChildren(QPushButton)
             if x.text() == "▶测这行" and x.isVisibleTo(panel))
    b.click()
    wait(80)


# B：命中统计 → 列表 ×N + 信息行（键 = 编辑器 stats_key：(流程名, 节点id)）
UIKEY = ("SmokeFlow", "w1")
watch_stats.record(UIKEY, 0)
watch_stats.record(UIKEY, 0)
watch_stats.record(UIKEY, 0)
panel._watch_stats_hook()
check("行① 列表尾挂 ×3", lst.item(0).text().endswith("×3"))
check("行② 无统计不挂尾", "×" not in lst.item(1).text())
check("信息行显示 命中 ×3 · 最近", labels_with("命中 ×3 · 最近") != [])
check("信息行带「跟随图(5,-2)」提示", labels_with("跟随图(5,-2)") != [])
node["rows"][0]["offset_x"] = 3
lst.setCurrentRow(1)
wait(80)
lst.setCurrentRow(0)
wait(80)
check("行上自己采了偏移 → 跟随图提示消失", labels_with("跟随图") == [])
node["rows"][0].pop("offset_x")
lst.setCurrentRow(1)
wait(80)
lst.setCurrentRow(0)
wait(80)

# 同行去抖：统计连击后信息行仍投得到活控件（_lbl_ref 不许被去抖路径摘掉）
watch_stats.record(UIKEY, 0)
panel._watch_stats_hook()
check("去抖后再刷统计：信息行跟到 ×4", labels_with("命中 ×4") != [])

# A：▶测这行 发射 (目录,图名)
emitted = []
panel.request_row_test.connect(lambda c, n: emitted.append((c, n)))
click_test_btn()
check("▶测这行 发出 request_row_test(offlib,imgA)", emitted == [("offlib", "imgA")])
# 结果回投 + 换行仍显示
panel.apply_row_result("offlib", "imgA", "✓ 命中 0.912 @(100,200)")
check("试测结果进信息行", labels_with("试测：✓ 命中 0.912") != [])
lst.setCurrentRow(1)
wait(80)
lst.setCurrentRow(0)
wait(80)
check("换行回来结果不丢", labels_with("试测：✓ 命中 0.912") != [])
# 没选图的行（空名行会被「保真兜底」回写成真实图名——建好编辑区后手动清空才测到本分支）
lst.setCurrentRow(2)
wait(80)
node["rows"][2]["name"] = ""
click_test_btn()
check("没选图的行：提示「这行还没选图」", labels_with("这行还没选图") != [])

# 定时器 / _clear_props 摘钩
check("3s 统计定时器已建并启动",
      panel._watch_stat_timer is not None
      and panel._watch_stat_timer.interval() == 3000
      and panel._watch_stat_timer.isActive())
tit = next(it for it in panel.scene.items()
           if isinstance(it, FlowNodeItem) and it.node.get("id") == "t2")
panel.scene.clearSelection()
tit.setSelected(True)
wait(150)
check("切到别的节点（面板拆建）：统计钩已摘", panel._watch_stats_hook is None)

# _rel_age 档位
check("_rel_age 档位",
      FlowPanel._rel_age(time.time() - 10) == "刚刚"
      and FlowPanel._rel_age(time.time() - 601) == "10分钟前"
      and FlowPanel._rel_age(time.time() - 7300) == "2小时前"
      and FlowPanel._rel_age(time.time() - 86400 * 3 - 60) == "3天前")

# ④ workbench 接线守卫（源码级：断线=发射没人接，静默失灵最难查）
wb = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "ui", "workbench.py"), encoding="utf-8").read()
check("workbench 已接 request_row_test",
      "request_row_test.connect(self._on_request_row_test)" in wb
      and "def _on_request_row_test" in wb)

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke watch-validity ALL OK")
