# 悬浮召回球自检（正式冒烟，tests/ 下常驻）
# 守的四层：
#  ① 注册表出现/隐藏口径：attach 两窗，任一最小化→球显示；全部恢复→球隐藏；
#    enabled=false→球永不出现（配置开关一票否决）。
#  ② 单击召回语义：recall_last 召回的是「最近被最小化」的那个（后进先出），
#    且 offscreen 实测 show() 不解除 WindowMinimized 位——球内部必须走
#    setWindowState 显式清位，本测试断言的就是清位后的状态，回归即抓得住。
#  ③ 右键菜单构建纯函数 build_menu_rows：行名=窗口中文标题、最小化的带
#    「（已最小化）」后缀、末行恒为「本次运行隐藏悬浮球」；
#    QMenu.exec 留在事件处理里，冒烟只调纯函数（offscreen 弹菜单必阻塞）。
#  ④ 球的落点知识边界：float_ball 模块不许 import 流程面板（FlowPanel）等
#    编排层——球只认「挂上来的窗口」，不认业务。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_float_ball.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core.config_manager as cm

cfg = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))
cm._config = cfg   # 冒烟写盘红线：不碰真 config

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication.instance() or QApplication(sys.argv)

import ui.float_ball as fb_mod
from ui.float_ball import FloatBall, build_menu_rows

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


def mkwin(title):
    w = QWidget()
    w.setWindowFlags(w.windowFlags() | Qt.Window)
    w.setWindowTitle(title)
    w.resize(200, 160)
    w.show()
    return w


def restored(w):
    """恢复判定：窗口可见且不再带最小化位（offscreen 下 show() 不清位，必须看 state）。"""
    return w.isVisible() and not (w.windowState() & Qt.WindowMinimized)


# ── ④ 落点判定不写进球：球模块不 import 编排层 ──
src = open(fb_mod.__file__, encoding="utf-8").read()
check("float_ball 不 import FlowPanel/flow_editor（落点知识边界）",
      "FlowPanel" not in src and "flow_editor" not in src)

# ── ① 注册表：出现/隐藏口径 ──
a = mkwin("窗口甲")
b = mkwin("窗口乙")
ball = FloatBall()
ball.attach(a)
ball.attach(b)
app.processEvents()
check("无最小化窗口时球隐藏", not ball.isVisible())
ball.attach(a)  # 重复挂同一窗应无事发生
check("attach 幂等：重复挂同一窗不重复进注册表", ball._tracked.count(a) == 1)

a.showMinimized()
app.processEvents()
check("窗口甲最小化 → 球出现", ball.isVisible())

b.showMinimized()
app.processEvents()
check("两窗都最小化 → 球仍在", ball.isVisible())

# ── ② 单击召回：后进先出（乙是最近被最小化的） ──
ball.recall_last()
app.processEvents()
check("recall_last 召回最近最小化的乙（且最小化位已清）", restored(b))
check("甲仍最小化时球不撤", ball.isVisible() and (a.windowState() & Qt.WindowMinimized))

ball.recall_last()
app.processEvents()
check("再召回甲", restored(a))
check("全部恢复 → 球自动隐藏", not ball.isVisible())

# ── ① 配置开关：enabled=false 一票否决 ──
cfg.set("ui.float_ball", {"enabled": False, "x": -1, "y": -1})
a.showMinimized()
app.processEvents()
check("enabled=false → 最小化了球也不出现", not ball.isVisible())
cfg.set("ui.float_ball", {"enabled": True, "x": -1, "y": -1})
a.show()
a.setWindowState(a.windowState() & ~Qt.WindowMinimized)  # 复位甲
app.processEvents()
a.showMinimized()
app.processEvents()
check("enabled 改回 true 且甲最小化中 → 球重新出现", ball.isVisible())

# ── detach：摘掉的窗口再最小化不算数 ──
ball.detach(a)
a.show()
a.setWindowState(Qt.WindowNoState)
b.showMinimized()   # 只剩乙在跟踪里
app.processEvents()
ball.detach(b)
app.processEvents()
check("全部摘挂后球隐藏（注册表清空不再跟踪）", not ball.isVisible())

# ── ③ 右键菜单构建纯函数 ──
rows = build_menu_rows([("运行日志", True), ("工作台", False)])
check("菜单行数 = 窗口数 + 1（末行隐藏项）", len(rows) == 3)
check("最小化的行名带（已最小化）标记", rows[0][0].endswith("（已最小化）"))
check("未最小化的行名就是中文标题", rows[1][0] == "工作台")
check("末行恒为「本次运行隐藏悬浮球」", rows[2][0] == "本次运行隐藏悬浮球")
check("recall 行负载带窗口序号 / 隐藏行负载 None",
      rows[0][1] == ("recall", 0) and rows[1][1] == ("recall", 1)
      and rows[2][1] == ("hide_session", None))
check("空注册表也有末行", build_menu_rows([]) == [("本次运行隐藏悬浮球", ("hide_session", None))])

# ── 「本次运行隐藏」语义：此后最小化不再出现 ──
a2 = mkwin("窗口丙")
ball2 = FloatBall()
ball2.attach(a2)
a2.showMinimized()
app.processEvents()
check("新球随最小化出现", ball2.isVisible())
ball2._on_menu_action(("hide_session", None))
check("点了本次隐藏 → 球收起", not ball2.isVisible())
a2.setWindowState(a2.windowState() & ~Qt.WindowMinimized)
a2.showMinimized()
app.processEvents()
check("本次运行隐藏后，再次最小化球不再出现", not ball2.isVisible())

print("ALL OK" if ok else "HAS FAIL")
sys.exit(0 if ok else 1)
