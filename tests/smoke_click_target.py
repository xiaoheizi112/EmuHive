# -*- coding: utf-8 -*-
"""
tests/smoke_click_target.py — 共用预览一次性回调送达链路 冒烟（离屏，临时配置+临时模板库）

验 ImageView 新登记的 set_point_pick_callback / set_roi_pick_callback 唯一送达路：
1. 有回调：完成即消费作废、信号**不发**（杜绝双份投递）
2. 回调消费后再点：回落到老信号路（外壳 owner 兜底不误伤）
3. 右键取消：两种一次性回调一起作废，不留孤儿回调吃下一次无关动作
4. 没登记回调（旧调用方）：roi/点选完成照发信号，行为零变化
5. 采集表单接线：on_set_click_target / on_add_region 发起即登记，完成回填
   _pending_click_target / _pending_roi（不经信号）
6. 流程取点：_begin_pick(field="click") 真视图直连，完成写回节点 tap_x/tap_y
   且 _end_pick 撤销登记

红线：临时配置单例 + template_manager 指向空临时库，绝不碰真 templates/。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])

import core.config_manager as cm

_tmp = tempfile.mkdtemp(prefix="smoke_click_")
cm._config = cm.ConfigManager(os.path.join(_tmp, "config.json"))

# 临时空模板库（读侧隔离：TemplatePanel 建树/图标全走这里，不碰真库）
import core.template_manager as tm
_lib = os.path.join(_tmp, "templates")
os.makedirs(_lib, exist_ok=True)
tm.TEMPLATES_DIR = __import__("pathlib").Path(_lib)
tm.INDEX_PATH = tm.TEMPLATES_DIR / "index.json"
tm.INDEX_PATH.write_text('{"templates": []}', encoding="utf-8")
tm._cache = {"index": None, "mtime": 0.0, "images": {}}
tm._stat_ts = 0.0

FAILS = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def click(v, x, y, button=Qt.LeftButton):
    """合成一次按下（ImageView 只看 mousePressEvent，offscreen 无需真实光标）"""
    v.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPoint(x, y),
                                  button, button, Qt.NoModifier))


from ui.template_tool import ImageView


def new_view():
    v = ImageView()
    v.set_frame(np.zeros((960, 540, 3), dtype=np.uint8))   # 基准帧：1:1 恒等换算
    return v


# ══════════ 1/2. 点选回调：一次消费、不发信号；消费后回落信号 ══════════
v = new_view()
pts, sig = [], []
v.set_point_pick_callback(lambda x, y: pts.append((x, y)))
v.click_target_set.connect(lambda x, y: sig.append((x, y)))
v.draw_mode = "click_target"
click(v, 100, 200)
check(pts == [(100, 200)], f"回调收到坐标：{pts}")
check(sig == [], f"有回调时 click_target_set 不发（唯一送达）：{sig}")
check(v._point_pick_cb is None, "一次性：取走即作废")
check(v.draw_mode == v.default_mode, "完成后回到稳态模式")

v.draw_mode = "click_target"          # 再发起一次：回调已没了 → 老信号路
click(v, 30, 40)
check(pts == [(100, 200)], "作废后不再收第二发")
check(sig == [(30, 40)], f"未登记回调回落信号（旧调用方零变化）：{sig}")

# ══════════ 3. 右键取消：两种一次性回调一起作废 ══════════
v = new_view()
got = []
v.set_point_pick_callback(lambda *a: got.append(("pt",) + a))
v.set_roi_pick_callback(lambda *a: got.append(("roi",) + a))
v.draw_mode = "click_target"
click(v, 50, 50, Qt.RightButton)
check(v._point_pick_cb is None and v._roi_pick_cb is None, "右键取消清掉两种回调")
check(v.draw_mode == v.default_mode, "右键取消回稳态")
v.draw_mode = "click_target"
click(v, 60, 70)
check(got == [], "取消后没有孤儿回调吃这一发")

# ══════════ 4. 区域框选：有回调直送、无回调发信号 ══════════
v = new_view()
rois, rsig = [], []
v.set_roi_pick_callback(lambda x, y, w, h: rois.append((x, y, w, h)))
v.roi_drawn.connect(lambda x, y, w, h: rsig.append((x, y, w, h)))
v.draw_mode = "roi"
click(v, 10, 10)
click(v, 100, 90)
check(rois == [(10, 10, 90, 80)], f"区域回调直送 (x,y,w,h)：{rois}")
check(rsig == [], f"有回调时 roi_drawn 不发：{rsig}")
v.draw_mode = "roi"
click(v, 0, 0)
click(v, 20, 20)
check(rsig == [(0, 0, 20, 20)], f"未登记回落信号：{rsig}")

# ══════════ 5. 采集表单接线：发起即登记，完成回填表单 ══════════
from ui.template_tool import TemplatePanel

v = new_view()
panel = TemplatePanel()
panel.attach_view(v)
ptsig = []
v.click_target_set.connect(lambda x, y: ptsig.append((x, y)))
panel.on_set_click_target()
check(v._point_pick_cb is not None, "点「记录点击坐标」即登记回调")
click(v, 250, 350)
check(panel._pending_click_target == [250, 350],
      f"采集表单回填待保存点击坐标：{panel._pending_click_target}")
check("250" in panel.lbl_click.text(), "界面「当前」标签同步")
check(panel.lbl_click.text().startswith("✓"), "记录点击成功即时出 ✓ 反馈")
check(ptsig == [], "表单回填不经信号")

panel.on_add_region()
click(v, 8, 8)
click(v, 68, 58)
check(panel._pending_roi == [8, 8, 68, 58],
      f"区域回填 _pending_roi [x0,y0,x1,y1]：{panel._pending_roi}")
check(panel.lbl_roi.text().startswith("✓"), "框区域成功即时出 ✓ 反馈")
panel.on_clear_roi()
check("已清除" in panel.lbl_roi.text(), "清除区域有反馈话术")
panel.on_clear_click_target()
check("已清除" in panel.lbl_click.text(), "清除坐标有反馈话术")

# 两块区都上了「操作顺序」小字引导（用户拍板 2026-10-06：明确顺序+及时反馈）
from PySide6.QtWidgets import QLabel
check(sum(1 for l in panel.findChildren(QLabel) if "顺序" in l.text()) >= 2,
      "识别区域/固定点击坐标各有一条「顺序」引导")

# ══════════ 6. 流程取点：真视图直连写回节点 ══════════
from ui.flow_editor import FlowPanel

v2 = new_view()
fp = FlowPanel()
fp.attach_view(v2)
node = {"id": "n1", "type": "click_template", "category": "", "name": "",
        "label": "取点"}
fp._begin_pick(node, "click")
check(v2._point_pick_cb is not None, "流程发起取点即登记回调")
check(v2.click_owner == "pick", "click_owner 仍标记（外壳兜底路依据）")
click(v2, 123, 456)
check((node.get("tap_x"), node.get("tap_y")) == (123, 456),
      f"坐标写回节点：({node.get('tap_x')},{node.get('tap_y')})")
check(fp._pick_ctx is None, "完成后取点会话收口")
check(v2._point_pick_cb is None, "回调消费 + _end_pick 撤销登记")

print("FAILED:", FAILS or "无")
sys.exit(1 if FAILS else 0)
