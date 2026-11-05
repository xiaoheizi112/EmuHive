# 冒烟：判断节点「文字识别」的区域/全屏选项（2026-09 新增）
# 守的链路：_cond_text 面板 = 勾选框(限定区域/全屏) + 手填坐标 + 预览框选回填，
# 与 server_pick 同款；框选走 begin_roi_pick(cond, owner=节点) → 外壳 route_roi
# → cond["roi"] 回填 + 按宿主节点重建表单（cond 自身没有表单，owner 必须传节点）。
# 全程 FlowPanel.__new__ 借方法（smoke_offset_pick 同款），不建真 UI、不碰配置。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_cond_text_roi.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowPanel

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


class FakeView:
    draw_mode = None
    roi_owner = None


node = {"id": "n1", "type": "branch", "cond": {"kind": "text"}}
f = FlowPanel.__new__(FlowPanel)
f._mark_dirty = lambda: None
f._set_status = lambda t: None
f._rebuild_calls = []
f._rebuild_props = lambda n: f._rebuild_calls.append(n)
f._view = FakeView()
f._roi_pick_node = f._roi_pick_owner = None

cond = {"kind": "text", "target": "星轮冰杖9服"}
w = f._cond_text(node, cond)

chk = w.findChild(QCheckBox)
# ROI 已收口进共用 _roi_block（按钮带「（区域名）」尾注），断言意图不变：认准框选钮
btn = next(b for b in w.findChildren(QPushButton) if b.text().startswith("在预览上框选"))
check("面板有「限定识别区域（不勾 = 全屏）」勾选框",
      chk is not None and chk.text() == "限定识别区域（不勾 = 全屏）")
check("无 roi 的老条件 → 不勾 = 全屏", chk is not None and not chk.isChecked())
check("构表阶段不写 roi（不误伤数据）", "roi" not in cond)

# ① 全屏态 = 框选按钮禁用（与 server_pick 同款：先勾"限定区域"才能框）
check("全屏态下框选按钮禁用", not btn.isEnabled())
chk.click()
check("勾选 → cond.roi 落到默认全区", cond.get("roi") == [0, 0, 540, 960])
check("勾选 → 框选按钮解禁", btn.isEnabled())

# ② 发起框选：归属正确
btn.click()
check("点「在预览上框选」→ 预览进 roi 态",
      f._view.draw_mode == "roi" and f._view.roi_owner == "flow")
check("回填目标 = cond 本身（不是节点）", f._roi_pick_node is cond)
check("表单重建宿主 = 节点（cond 没表单）", f._roi_pick_owner is node)

# ② 外壳送回成框 → 回填进 cond + 按节点重建
f.route_roi(10, 20, 30, 40)
check("成框回填 cond.roi = (x0,y0,x1,y1)", cond.get("roi") == [10, 20, 40, 60])
check("重建的是宿主节点表单", f._rebuild_calls == [node])
check("一次性归属作废", f._roi_pick_node is None and f._roi_pick_owner is None)
w2 = f._cond_text(node, cond)
chk2 = w2.findChild(QCheckBox)
check("重建后面板反映区域态（勾选+控件启用）",
      chk2.isChecked() and all(s.isEnabled() for s in w2.findChildren(QPushButton)))

# ③ 取消勾选 → roi 置 None（全屏语义，编译侧 None 兜底）
chk2.click()
check("取消勾选 → cond.roi = None（全屏）", cond.get("roi") is None)
# ④ 再勾上 → 恢复默认全区坐标
chk2.click()
check("再勾选 → roi 恢复 [0,0,540,960]", cond.get("roi") == [0, 0, 540, 960])

# ⑤ server_pick 回归：单参调用（owner 缺省 = target 本身）路由不变
f2 = FlowPanel.__new__(FlowPanel)
f2._mark_dirty = lambda: None
f2._set_status = lambda t: None
f2._rebuild_props = lambda n: None
f2._view = FakeView()
f2._roi_pick_node = f2._roi_pick_owner = None
sp = {"type": "server_pick", "roi": None}
f2.begin_roi_pick(sp)
check("server_pick 旧调用式兼容（owner=自身）",
      f2._roi_pick_node is sp and f2._roi_pick_owner is sp)
f2.route_roi(1, 2, 3, 4)
check("server_pick 回填进节点", sp["roi"] == [1, 2, 4, 6])

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke cond-text-roi ALL OK")
