# 冒烟：工具栏流式布局 FlowLayout —— 摆不下就换行、按钮永远拿自然宽。
# 病根回归（2026-10-05 用户实拍）：工作台右栏可拖窄，横排工具栏把
# 「保存流程/测试流程…」压到 36px，文字整截挤出按钮。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_flow_layout.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

app = QApplication.instance() or QApplication(sys.argv)


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


from ui.flow_layout import FlowLayout

ok = True


def check(name, cond):
    global ok
    ok = ok and cond
    print(("FAIL " if not cond else "OK   ") + name)


# ① 窄容器：8 个按钮必须折成多行，且每个宽 >= 自身最小需求（不裁字）
w = QWidget()
lay = FlowLayout(w)
btns = [QPushButton(f"按钮{i}") for i in range(8)]
for b in btns:
    lay.addWidget(b)
w.setLayout(lay)
w.resize(150, 400)
w.show()
wait(100)
ys = {b.y() for b in btns}
check("窄栏折成多行", len(ys) > 1)
check("按钮不裁宽", all(b.width() >= b.minimumSizeHint().width() for b in btns))

# ② addStretch 末行摊余宽：状态标签吃掉剩余宽度
lay.addSpacing(4)
from PySide6.QtWidgets import QLabel
tail = QLabel("状态")
lay.addWidget(tail, stretch=1)
wait(100)
check("stretch 末行拉满", tail.width() > tail.sizeHint().width())

# ③ 空布局/零宽不得崩（heightForWidth 探测路径）
w2 = QWidget()
lay2 = FlowLayout(w2)
w2.setLayout(lay2)
w2.resize(0, 0)
w2.show()
wait(50)
check("空布局零宽不崩", True)

print("SMOKE PASS" if ok else "SMOKE FAIL")
sys.exit(0 if ok else 1)
