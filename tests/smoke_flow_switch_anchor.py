# 切换流程画布不位移自检（正式冒烟，tests/ 下常驻）
# 守住的 bug：切换流程库里的流程时，画布上所有节点集体"跳一下"。
# 病根：校验条 lbl_valid 挂在画布下方主布局，每条流程校验结果不同（有无警告）。
#   切流程的瞬间 _rebuild_scene 先按【上一条流程的校验条状态】摆好新节点，
#   400ms 后防抖校验才到点、按新流程结果显隐校验条 → 视口高度跟着变 →
#   view 旧设 AnchorViewCenter 在视口涨缩时把整幅场景重新居中 → 新节点整体
#   平移一下（切完约半秒才跳，正是"位移一下"的手感）。
# 修法：view.setResizeAnchor(NoAnchor)，视口尺寸变化时锚定左上角不重新居中。
# 测的是"摆好瞬间"vs"校验到点后"同一批节点的屏幕坐标差（净往返差恒为 0，
# 抓不到这个瞬跳，必须比 stale 帧与 settled 帧）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_flow_switch_anchor.py
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# 冒烟写盘红线：临时配置副本，绝不碰真 configs/config.json
import core.config_manager as cm

_tmp = os.path.join(tempfile.mkdtemp(), "config.json")
cfg = cm.ConfigManager(_tmp)
cm._config = cfg

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowNodeItem, FlowPanel


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


# 两条合成流程：flowWarn 有孤立节点（校验条显示），flowClean 全连线（校验条隐藏）。
# 用 sleep 节点：无模板依赖、无坐标依赖，只喂成环/孤立判定。
def node(nid, x, y):
    return {"id": nid, "type": "sleep", "x": x, "y": y, "label": "", "seconds": 1}


cfg.set("custom_flows", {
    "flowWarn": {"version": 2,
                 "nodes": [node("a", 100, 100), node("b", 400, 100), node("c", 700, 400)],
                 "edges": []},                       # 全孤立 → 报"孤立节点" → 校验条显示
    "flowClean": {"version": 2,
                  "nodes": [node("a", 100, 100), node("b", 400, 100)],
                  "edges": [{"from": "a", "port": "out", "to": "b"}]},  # 都连线 → 无警告 → 校验条隐藏
})

panel = FlowPanel()
panel.chk_autosave.setChecked(False)
panel.resize(1400, 900)
panel.show()
wait(300)


def screenpos():
    v = panel.view
    return {it.node["id"]:
            (lambda p: (round(p.x()), round(p.y())))(
                v.mapFromScene(it.mapToScene(it.boundingRect().center())))
            for it in panel.scene.items() if isinstance(it, FlowNodeItem)}


ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


# 前置：确认两条流程校验状态确实相反（否则测不出视口高度变化，测试空转）
panel._select_flow("flowWarn")
wait(600)
warn_visible = panel.lbl_valid.isVisible()
warn_vp_h = panel.view.viewport().height()
panel._select_flow("flowClean")
wait(600)
clean_visible = panel.lbl_valid.isVisible()
clean_vp_h = panel.view.viewport().height()
check("前置：flowWarn 显示校验条、flowClean 不显示（校验条显隐能改变视口高度）",
      warn_visible and not clean_visible and warn_vp_h != clean_vp_h)

# 核心：比"摆好瞬间(stale)"vs"校验到点后(settled)"。净往返差恒为 0 抓不到瞬跳，
# 必须卡在校验防抖(400ms)到点的前后各采一帧。
def stale_vs_settled(from_flow, to_flow):
    panel._select_flow(from_flow)
    wait(600)                       # 稳定在 from：校验条已是 from 的状态
    panel._select_flow(to_flow)     # 切到 to：_rebuild_scene 摆好新节点，但校验条仍是 from
    stale = screenpos()             # 摆好瞬间（不 wait，防抖未到点，视口还是 from 的高度）
    wait(600)                       # _val_timer 到点 → 校验条按 to 显隐 → 视口涨缩
    settled = screenpos()
    return {k: (stale[k], settled[k]) for k in stale if stale[k] != settled.get(k)}


# clean(条隐藏·视口高) → warn(条显示·视口矮)：视口变矮，旧锚点把节点往上拽半截
jump_c2w = stale_vs_settled("flowClean", "flowWarn")
check(f"clean→warn 切完不瞬跳（实测={jump_c2w}）", not jump_c2w)
# warn(条显示·视口矮) → clean(条隐藏·视口高)：视口变高，旧锚点把节点往下推半截
jump_w2c = stale_vs_settled("flowWarn", "flowClean")
check(f"warn→clean 切完不瞬跳（实测={jump_w2c}）", not jump_w2c)

print("ALL OK" if ok else "HAS FAIL")
sys.exit(0 if ok else 1)
