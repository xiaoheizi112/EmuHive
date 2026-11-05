# 冒烟：worker 逐拍兜底（2026-09-24 成品审计 P0-①）
# 守两条：①拍级异常不再穿杀整实例——偶发几次自动恢复继续跑；
#         ②连败超阈值（30 拍）才判 failed，且 _last_fail_reason 带错误原文（写库用）。
# 红线：临时配置单例，绝不碰真 configs/config.json。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_tick_guard.py
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from bot.worker import AccountWorker

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


def make_worker():
    w = AccountWorker(99, task_name="flow:冒烟")
    w._stop = threading.Event()
    w._hard_stop = False
    w._pause = threading.Event()
    w._interruptible_wait = lambda secs: False   # 免真睡（兜底退避 0.5s×30）
    w._grab_frame = lambda: "IMG"                # 截帧恒成功，专测 tick 兜底
    return w


class FsmStub:
    """前 n_raise 拍抛异常，之后返回 done。"""

    def __init__(self, n_raise):
        self.n_raise = n_raise
        self.beats = 0

    def tick(self):
        self.beats += 1
        if self.beats <= self.n_raise:
            raise RuntimeError("模拟 ldconsole 超时")
        return "done"


# ① 偶发异常自动恢复：抛 3 拍后正常收尾 done
w = make_worker()
fsm = FsmStub(3)
logs = []
w.action_log.connect(lambda i, lvl, m: logs.append((lvl, m)))
r = w._run_single_task(object(), fsm)
check("偶发 3 次拍级异常 → 自动恢复跑完（不杀实例）", r == "done" and fsm.beats == 4)
check("恢复前有 WARN 报数（连第 1 次即报）",
      any(l == "WARN" and "拍级异常" in m for l, m in logs))

# ② 连败超阈值 → failed + 原因带错误原文
w2 = make_worker()
fsm2 = FsmStub(999)
r2 = w2._run_single_task(object(), fsm2)
check("持续异常 → 30 拍封顶判 failed", r2 == "failed" and fsm2.beats == 30)
check("失败原因带错误原文（写库/日志可查因）",
      "模拟 ldconsole 超时" in (w2._last_fail_reason or ""))

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke tick-guard ALL OK")
