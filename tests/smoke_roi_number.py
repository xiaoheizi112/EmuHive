"""
tests/smoke_roi_number.py — 「读出区域内数字」编辑面板常驻自检（离屏）

覆盖 server_pick 属性表单的 roi_number 分支：
- 能建表单不抛（分支里任何 NameError/属性打脸都在这里炸出来）；
- 不画「目标文字」锚点行、画「变量名」行、ROI 走必框形态（有「在预览上框选」
  按钮、没有"限定识别区域"勾选框 —— 不框 = 全屏乱抓数字，拦在编辑期）；
- 老 number 用法回归：锚点行照画、ROI 仍是可选勾选（存量流程界面零变化）；
- 保存校验：roi_number 没框 → 点名拦下（pick_pair 同款防呆顺带回归）。

跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_roi_number.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# 红线：离屏冒烟绝不允许碰真实 config/流程库 —— 本脚本不写盘，但 _fill_props
# 构建期可能读配置（常驻延时），提前换临时配置单例兜底。
import tempfile
import core.config_manager as cm
cm._config = cm.ConfigManager(str(Path(tempfile.mkdtemp(prefix="roinum_smoke_")) / "c.json"))

from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QCheckBox, QLineEdit

app = QApplication.instance() or QApplication([])

from ui.flow_editor import FlowPanel


def _build(node):
    """借 FlowPanel 方法建一次属性表单（不跑 __init__），返回宿主 widget。"""
    p = FlowPanel.__new__(FlowPanel)
    p.props_layout = None
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    host = QWidget()
    p.props_layout = QVBoxLayout(host)
    p._mark_dirty = lambda: None
    p._set_status = lambda t: None
    p._view = None
    p.graph = {"nodes": [node], "edges": []}
    p._rebuild_props(node)
    return host


n_roi = {"id": "a", "type": "server_pick", "mode": "roi_number",
         "label": "读金币", "var_name": "金币", "roi": [10, 10, 100, 40]}
h = _build(dict(n_roi))
labels = {l.text() for l in h.findChildren(QLabel)}
btns = [b.text() for b in h.findChildren(QPushButton)]
assert "目标文字" not in labels and "服务器全名" not in labels, labels
assert "变量名" in labels, labels
assert not h.findChildren(QCheckBox, "限定识别区域（不勾 = 全屏）"), "roi_number 不该有可选 ROI 勾选"
assert any("在预览上框选" in t for t in btns), btns
print("roi_number 表单：无锚点行 / 有变量名 / ROI 必框形态 OK")

n_num = {"id": "b", "type": "server_pick", "mode": "number",
         "label": "读金币", "text": "金币", "var_name": "gold"}
h2 = _build(dict(n_num))
labels2 = {l.text() for l in h2.findChildren(QLabel)}
chk2 = [c.text() for c in h2.findChildren(QCheckBox)]
assert "目标文字" in labels2 and "变量名" in labels2, labels2
assert any("限定识别区域" in t for t in chk2), "number 老用法 ROI 仍可选"
print("number 老用法回归：锚点行 + 可选 ROI 零变化 OK")

# 保存校验：真走编辑器的 _refresh_validation（不另抄一份判定口径）——
# roi_number 缺框必点名，框好的那个不许上榜
# （旧 pick_pair 的"缺选服区域"点名随用法删除，见 smoke_splice_pick_pair）
from PySide6.QtWidgets import QLabel
p = FlowPanel.__new__(FlowPanel)
p.graph = {"nodes": [dict(n_roi, roi=None),
                     {"id": "ok", "type": "server_pick", "mode": "roi_number",
                      "label": "框好的", "var_name": "金币", "roi": [1, 2, 3, 4]}],
           "edges": [{"from": "a", "to": "ok"}]}
p._val_problems, p._val_details = [], []
p.lbl_valid = QLabel()
p._refresh_validation()
txt = "\n".join(p._val_problems + p._val_details)
assert "读金币（缺识别区域）" in txt, txt
assert "框好的" not in txt, txt
print("缺框判定：真调 _refresh_validation，缺框点名、框好的不报 OK")

print("smoke_roi_number: ALL OK")
