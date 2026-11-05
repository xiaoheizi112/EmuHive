# 流程相机互相隔离自检（正式冒烟，tests/ 下常驻）
# 守住的 bug：整个面板只有一条共用 view，平移(滚动条 value)+缩放(矩阵)都挂在
# view 上，切流程 _rebuild_scene 只清场景不碰镜头 → 在 A 里平移/滚轮缩放一下，
# 切到 B 时 B "继承" A 的相机：B 的节点集体偏向一边；摆得离原点远的流程直接
# "消失不见"（视野根本没框到它）。
# 修法：_reload_flow 一条道走到底——离场前按流程名存相机
# (transform, 视口中心场景点)，进场后灌回。
# 2026-10-06 首开语义修正（用户："每次切换都自动跳转"）：本次会话没存过的流程
# 不再无脑复位+fit —— 内容就在当前视口里就镜头原地不动；整个在视口外才框住
# （"消失不见"病根仍守住：看不见必框，看得见绝不跳）。相机只活在会话内存，不落盘。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_flow_camera.py
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

from PySide6.QtCore import QPoint, Qt, QEventLoop, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication.instance() or QApplication(sys.argv)
# offscreen 模态框会永久阻塞（CLAUDE.md 记录的坑），凡确认框一律打桩放行
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

from ui.flow_editor import FlowNodeItem, FlowPanel


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def node(nid, x, y):
    return {"id": nid, "type": "sleep", "x": x, "y": y, "label": "", "seconds": 1}


# 三条流程坐标刻意拉远：借用别人的镜头时，远的必然飞出视野（复现"消失不见"）
cfg.set("custom_flows", {
    "flowNear": {"version": 2, "nodes": [node("a", 100, 100), node("b", 400, 100)],
                 "edges": [{"from": "a", "port": "out", "to": "b"}]},
    "flowFar":  {"version": 2, "nodes": [node("a", 8000, 6000)], "edges": []},
    "flowGone": {"version": 2, "nodes": [node("a", -9000, -9000)], "edges": []},
    # 超长流程（黑市整理布局后的形状）：包围盒宽近万 px，fitInView 一口能压到
    # MIN_SCALE 以下 → zoom() 区间守卫两头拒 = 滚轮彻底死锁（实测 bug）
    "flowHuge": {"version": 2, "nodes": [node("a", 0, 0), node("b", 9000, 6000)],
                 "edges": [{"from": "a", "port": "out", "to": "b"}]},
})

panel = FlowPanel()
panel.chk_autosave.setChecked(False)
panel.resize(1400, 900)
panel.show()
wait(300)


def screenpos(name=None):
    """当前画布全部节点中心的屏幕坐标（未选中任何流程时为空 dict）。"""
    v = panel.view
    return {it.node["id"]:
            (lambda p: (round(p.x()), round(p.y())))(
                v.mapFromScene(it.mapToScene(it.boundingRect().center())))
            for it in panel.scene.items() if isinstance(it, FlowNodeItem)}


def visible_count():
    """节点中心落进视口内的个数——"消失不见"的直接判据。"""
    v = panel.view
    r = v.viewport().rect()
    return sum(1 for p in screenpos().values()
               if 0 <= p[0] <= r.width() and 0 <= p[1] <= r.height())


ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


def close_pos(a, b, tol=2):
    return set(a) == set(b) and all(abs(a[k][0] - b[k][0]) <= tol
                                    and abs(a[k][1] - b[k][1]) <= tol
                                    for k in a)


# ① 首次打开的流流程必须框得住内容（不继承上一条的镜头 → 不"消失不见"）
panel._select_flow("flowFar")
wait(600)
far1 = screenpos()
check(f"首开 flowFar 节点在视野内（可见 {visible_count()}/1）", visible_count() >= 1)

# 在 flowNear 里真实右键平移 + 滚轮缩放，污染共用镜头（旧版病根）
panel._select_flow("flowNear")
wait(600)
check("首开 flowNear 节点在视野内", visible_count() >= 2)
vp = panel.view.viewport()
p0 = QPoint(vp.width() // 2 + 180, vp.height() - 60)   # 空白处按下（避开节点）
QTest.mousePress(vp, Qt.RightButton, Qt.NoModifier, p0)
QTest.mouseMove(vp, p0 + QPoint(200, 80))
QTest.mouseRelease(vp, Qt.RightButton, Qt.NoModifier, p0 + QPoint(200, 80))
panel.view.zoom(1.25)
panel.view.zoom(1.25)
wait(600)
near_viewed = screenpos()

# ② 切走再切回 flowFar：自己的视角纹丝不动，A 的平移缩放一点没漏过来
panel._select_flow("flowGone")
wait(600)
panel._select_flow("flowFar")
wait(600)
far2 = screenpos()
check(f"flowNear 平移+缩放后切到 flowFar：视角不受染（far1={far1} far2={far2}）",
      close_pos(far1, far2))

# ③ 自己的相机得记住：切回来还停在自己上次看的位置（不是每次复位）
panel._select_flow("flowFar")
wait(300)
panel._select_flow("flowNear")
wait(600)
near_again = screenpos()
check("切回 flowNear 恢复自己的视角（含刚才的平移缩放）",
      close_pos(near_viewed, near_again))

# ④ 首开 flowGone（节点在 -9000,-9000 犄角旮旯）：也必须直接框住看得见
panel._select_flow("flowGone")
wait(600)
check(f"首开远离原点的 flowGone 不'消失'（可见 {visible_count()}/1）",
      visible_count() >= 1)

# ⑤ 账本口径：每条访问过的流程各有一键，_view_flow 跟住当前流程
check("相机账本按流程名各记各的",
      set(panel._cameras) == {"flowNear", "flowFar", "flowGone"}
      and panel._view_flow == "flowGone")

# ⑥ 超长流程 fit 死锁守卫：黑市整理布局后包围盒宽近万 px，fitInView 一口能把
#    缩放压到 MIN_SCALE 以下；zoom() 的守卫是「新值越界即拒」，从越界处 ×1.25
#    仍 < MIN → 放大被永久拒 = 滚轮彻底失灵（用户实测 bug）。
#    修法：fit_scene 后把缩放钳回 [MIN,MAX]，绝不让镜头停在界外。
panel._select_flow("flowHuge")
wait(600)
v = panel.view
sc = v._scale_factor()
check(f"⑥ flowHuge fit 后缩放钳进 [MIN,MAX]（{sc:.3f} ∈ [{v.MIN_SCALE},{v.MAX_SCALE}]）",
      v.MIN_SCALE - 1e-6 <= sc <= v.MAX_SCALE + 1e-6)
before = sc
v.zoom(1.25)
check("⑥ 钳回后滚轮放大不再被永久拒（缩放确实变大）",
      v._scale_factor() > before + 1e-6)

# ⑦ 首开不跳（2026-10-06 用户拍板）：新流程内容就在当前视口里 → 镜头纹丝不动。
#    先切回 flowNear 拿回已知视角（两节点 (100,100)/(400,100) 全在视口内），
#    塞一条内容 (250,100) 的新流程——正夹在两可见节点中间 → 切过去不许跳。
#    （别拿 flowHuge 的 fit 视角当背景：MIN_SCALE 钳位后原区根本不在视口里。）
panel._select_flow("flowNear")
wait(600)
_fl = dict(cfg.get("custom_flows"))
_fl["flowAlsoNear"] = {"version": 2, "nodes": [node("z", 250, 100)], "edges": []}
_fl["flowDeep"] = {"version": 2, "nodes": [node("w", 60000, 60000)], "edges": []}
cfg.set("custom_flows", _fl)
panel._refresh_flow_list()


def cam_now():
    v = panel.view
    c = v.mapToScene(v.viewport().rect().center())
    return (round(v.transform().m11(), 4), round(c.x()), round(c.y()))


cam0 = cam_now()
panel._select_flow("flowAlsoNear")
wait(600)
check(f"⑦ 首开内容可见的新流程：镜头原地不跳（{cam0} == {cam_now()}）",
      cam_now() == cam0 and visible_count() >= 1)

# ⑧ 但内容整个在视口外的首开流程仍必须框住（10-05 病根不复发）
panel._select_flow("flowDeep")
wait(600)
check(f"⑧ 首开内容全在视口外：照旧 fit 框住（可见 {visible_count()}/1）",
      visible_count() >= 1 and cam_now() != cam0)

print("ALL OK" if ok else "HAS FAIL")
sys.exit(0 if ok else 1)
