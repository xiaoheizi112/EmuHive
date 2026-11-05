# 工作台"打开即摆正"自检（正式冒烟，tests/ 下常驻）
# 守的两层 bug：
#  ① restore/默认的窗口几何越界，Windows 对程序放的几何不做任务栏避让
#    （只在手动拖标题栏时管）→ 底边进任务栏按钮被压住；顶边越界则标题栏
#    整条顶出屏外，最小化/关闭够不着（实测 config 存过 y=-31）。
#  ② 上一版钳位自己的坑：顶层窗 x()/setGeometry 是客户区系，
#    frameGeometry() 是含标题栏的窗框系，读框写客=钳完标题栏照样在屏外。
#    本测试全程按【窗框系】断言（与实现同一量具）。offscreen 无标题栏、
#    两系重合，量具一致仍防逻辑回归；真机验证见注释末尾。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_workbench_clamp.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


import core.config_manager as cm

cfg = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))
cm._config = cfg   # 冒烟写盘红线：不碰真 config

from PySide6.QtWidgets import QApplication, QWidget

app = QApplication.instance() or QApplication(sys.argv)

from ui.workbench import WorkbenchDialog


class Bare(QWidget):
    """只借钳位方法：真 QWidget 才有 screen/frameGeometry，不建整套工作台。"""
    _clamp_into_screen = WorkbenchDialog._clamp_into_screen


ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


w = Bare()
w.resize(1700, 1000)
scr = w.screen().availableGeometry()


def in_scr():
    g = w.frameGeometry()
    return (g.left() >= scr.left() and g.top() >= scr.top()
            and g.right() <= scr.right() and g.bottom() <= scr.bottom())


# ① 底边越进任务栏 + 右边越界：钳回后窗框四边都在可用区内
w.move(scr.left() + 100, scr.bottom() - 300)
w._clamp_into_screen()
check("越界窗体钳回可用区（下/右边界）", in_scr())

# ② 左上越界（负坐标，标题栏顶出屏外的真实事故形）也要钳回来
w.move(scr.left() - 400, scr.top() - 31)
w._clamp_into_screen()
check("负坐标越界钳回（上/左边界，标题栏可见）", in_scr())

# ③ 窗框比可用区还大：先缩到放得下再钳位
w.resize(scr.width() + 500, scr.height() + 500)
w.move(scr.left() - 200, scr.top() - 200)
w._clamp_into_screen()
check("超屏窗体缩到可用区内",
      w.frameGeometry().width() <= scr.width()
      and w.frameGeometry().height() <= scr.height() and in_scr())

# ④ 已在可用区内 → 一毫米都不动（不跟用户摆好的窗口过不去）。
# 量具用 pos()（与 move 同一客户区系）：顶层窗 setGeometry 写框系读客户区
# 天生不等，别拿它们自比。尺寸小到肯定塞得进（offscreen 屏仅 800×800）。
w.resize(400, 300)
w.move(scr.left() + 20, scr.top() + 30)
before = w.pos()
w._clamp_into_screen()
check("合界窗口原样不动", w.pos() == before)

# ⑤ 幂等：连钳两次结果一致（钳位不该自己把窗口越钳越歪）
w.move(scr.left() - 400, scr.top() - 31)
w._clamp_into_screen()
p1 = w.pos()
w._clamp_into_screen()
check("重复钳位幂等", w.pos() == p1 and in_scr())

print("ALL OK" if ok else "HAS FAIL")
sys.exit(0 if ok else 1)
# 真机（有标题栏）验证口径：config 里 workbench_geometry 存 y=-31 时重开
# 工作台，标题栏（含最小化/关闭）应完整可见 —— 由用户实测确认。
