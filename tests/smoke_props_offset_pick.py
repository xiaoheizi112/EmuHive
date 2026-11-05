# 冒烟：属性窗「画面点选算偏移」→ 存进图（2026-10-05 新增）
# 存量图补偏移不用重采：预匹配定锚 → 点目标位 → dx/dy 回填勾选框。
# 本冒烟守回填纯函数行为（勾选/带回/清除）。
# （2026-10-09 冒烟减负：原"三处接线源码守卫"已摘——守的是按钮文案与调用接线，
#  断链在真机上一眼可见；锚点匹配同口径由 smoke_click_offset/引擎自检守。）
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_props_offset_pick.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from ui.template_tool import TemplatePropsDialog

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


dlg = TemplatePropsDialog({"name": "n", "category": "c", "threshold": 0.85,
                           "width": 10, "height": 20})
check("初始没存偏移 → 「跟随图偏移」未勾", not dlg.chk_off.isChecked())
dlg.apply_picked_offset(5, -2)
check("点选回填：自动勾上 + dx/dy 进 spin",
      dlg.chk_off.isChecked() and dlg.spin_off[0].value() == 5
      and dlg.spin_off[1].value() == -2)
check("values() 带回 click_offset=[5,-2]", dlg.values()[3] == [5, -2])
dlg.chk_off.setChecked(False)
check("取消勾选 → click_offset 回 None（清除路径）", dlg.values()[3] is None)

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke props-offset-pick ALL OK")
