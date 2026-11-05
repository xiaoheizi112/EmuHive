"""工作台采集两功能冒烟（离屏，临时配置，不碰模拟器/不写模板库）：
① core.vision.snap_bbox 纯算法：松种子区 → 吸到前景方块真实边缘
② 「框选后自动贴边」勾选 → 蓝框完成即被面板吸附
③ 保存后「新帧复检」：on_save_and_test 挂起的复检由下一帧 on_frame 兑现
python tests/smoke_snap_verify.py"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

from core import vision
from ui.template_tool import ImageView, TemplatePanel

# ① 纯算法：200×200 暗底，(50,60)-(80,90) 亮块；种子区四周各松 ~10px
img = np.full((200, 200, 3), 30, np.uint8)
img[60:90, 50:80] = 230
bbox = vision.snap_bbox(img, (40, 50, 100, 110))
assert bbox is not None, "贴边失败（合成图必然分得出前景）"
x, y, w, h = bbox
assert abs(x - 50) <= 3 and abs(y - 60) <= 3, f"贴边位置跑偏: {bbox}"
assert abs(w - 30) <= 6 and abs(h - 30) <= 6, f"贴边尺寸没收紧: {bbox}"
# 分不出前景（纯色区）→ None，不许瞎吸
assert vision.snap_bbox(img, (5, 5, 30, 30)) is None

# ② 面板接线：勾选自动贴边 → 框选完成信号把蓝框吸紧
p = TemplatePanel()
v = ImageView()
p.attach_view(v)
p._frame = img
v.selection = (40, 50, 60, 60)          # 模拟 ImageView 成框后的状态
p.chk_snap.setChecked(True)             # toggled 写的是临时 config
v.selection_done.emit(40, 50, 60, 60)
assert v.selection is not None and abs(v.selection[0] - 50) <= 3, \
    f"自动贴边没生效: {v.selection}"
# 不勾选 → 框原样不动
v.selection = (40, 50, 60, 60)
p.chk_snap.setChecked(False)
v.selection_done.emit(40, 50, 60, 60)
assert v.selection == (40, 50, 60, 60), "关了开关还吸边 = 越权改用户的框"

# ③ 新帧复检：挂起 → 下一帧兑现一次（且只一次）
got = []
p.on_test_template = lambda name, cat, note="": got.append((name, cat, note))
p._pending_verify = ("按钮_测试", "测试库")
p.on_frame(img)
assert got == [("按钮_测试", "测试库", "新帧复检 ")], f"复检链路异常: {got}"
p.on_frame(img)
assert len(got) == 1, "复检兑现后没销账，每帧都测 = 刷屏"

print("OK smoke_snap_verify")
