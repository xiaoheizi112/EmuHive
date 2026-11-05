# 冒烟：最小化窗口两处修复（2026-09 末真机反馈：从属窗最小化缩成迷你标题栏
# 飘屏；主窗点「工作台/日志/状态」按钮唤不回已最小化的窗）
# 守的链路：①recall_window 统一召回——必须先清 WindowMinimized 位再 show
#   （offscreen 实测 show() 不自愈最小化，真机上带主人的从属窗同样不稳）；
#   FloatBall.recall 委托同一入口，不再自带一套 show+raise；
# ②core.win_utils.ensure_taskbar_button 坏句柄防御：0/负数/瞎数一律 False 不抛
#   （真机 Win32 调用冒烟里不验效果，只验不炸——效果靠 WS_EX_APPWINDOW 语义）；
# ②b core.win_utils.set_window_app_id/window_app_id 坏句柄防御（2026-10-15 新增，
#   点任务栏图标不最小化的根因）：0/负数/瞎数/空 app_id 一律 False/None 不抛。
#   效果（拆任务栏分组 = 从"点按钮弹预览"恢复"点一下最小化"）只能真机看，
#   离屏窗口是假 HWND（winId=1）验不了；真机判据 = 任务栏上本程序窗口各占一个
#   独立按钮（UIA: 'Python - 1 个运行窗口' ×N，而非合并的 'Python - N 个运行窗口'）。
# ③（2026-10-09 冒烟减负已摘：三窗 showEvent 补按钮/召回点走 recall_window 的
#   接线文本守卫——断链表现为"任务栏没按钮/按钮点了唤不回"，真机一眼可见；
#   2026-10-15 的 set_window_app_id 接线同类，同样不进源码守卫）
# 全程临时 config 单例（FloatBall 构造读 ui.float_ball），绝不碰真实 config。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_taskbar_recall.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtWidgets import QApplication, QWidget

app = QApplication.instance() or QApplication(sys.argv)

import core.config_manager as cm

cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(prefix="tb_smoke_"),
                                           "config.json"))

from core.win_utils import (APPID_LOG, APPID_MAIN, APPID_STATUS, APPID_WORKBENCH,
                            ensure_taskbar_button, set_window_app_id, window_app_id)
from ui.float_ball import FloatBall, recall_window

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


# ── ② 坏句柄防御（不碰真窗口，纯防炸）──
check("ensure_taskbar_button 坏句柄防御：0/负数/瞎数都 False 且不抛",
      ensure_taskbar_button(0) is False
      and ensure_taskbar_button(-3) is False
      and ensure_taskbar_button(999999999) is False)

# ── ②b set_window_app_id / window_app_id 坏句柄防御（点不动最小化根因，只防炸）──
check("四个窗口 AppID 常量互不相同且都以根 ID 开头",
      len({APPID_MAIN, APPID_WORKBENCH, APPID_LOG, APPID_STATUS}) == 4
      and all(a.startswith("EmuHive.IdleTool") for a in
              (APPID_MAIN, APPID_WORKBENCH, APPID_LOG, APPID_STATUS)))
check("set_window_app_id 坏句柄/空 id 一律 False 且不抛",
      set_window_app_id(0, APPID_LOG) is False
      and set_window_app_id(-3, APPID_LOG) is False
      and set_window_app_id(999999999, APPID_LOG) is False
      and set_window_app_id(1, "") is False)   # offscreen 假 HWND + 空 app_id
check("window_app_id 坏句柄一律 None 且不抛",
      window_app_id(0) is None
      and window_app_id(-3) is None
      and window_app_id(999999999) is None)

# ── ① recall_window 清最小化位 ──
w = QWidget()
w.setWindowTitle("召回头号")
w.resize(200, 120)
w.show()
app.processEvents()
w.showMinimized()
app.processEvents()
check("前置：offscreen 下 showMinimized 真置最小化位", w.isMinimized())
recall_window(w)
app.processEvents()
check("recall_window 清最小化位、保持可见", not w.isMinimized() and w.isVisible())

b = FloatBall()
w2 = QWidget()
w2.setWindowTitle("召回二号")
w2.show()
app.processEvents()
w2.showMinimized()
app.processEvents()
b.attach(w2)
b.recall(w2)
app.processEvents()
check("FloatBall.recall 委托同一套清位逻辑", not w2.isMinimized() and w2.isVisible())

print("SMOKE " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
