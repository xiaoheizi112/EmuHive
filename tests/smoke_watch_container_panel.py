# 冒烟：并行监视容器属性面板的「编号页签 + ＋/－钮」（分段③）
# 断言四件事：选中容器出页签（数量=组数、徽章编号）；页签内编辑命中组 dict
# 而非容器顶层；－删组同步剪这组的命中信号线；－/拆出都不摊平独苗组，删到 0 组空壳才退回普通哨兵。
# 不真存盘（红线：临时配置单例 + autosave 关）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_watch_container_panel.py
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

cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))
cfg = cm.get_config()

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import (QApplication, QDoubleSpinBox, QListWidget,
                               QTabWidget)

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowPanel


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def n(nid, t, x, y, **kw):
    return {"id": nid, "type": t, "x": x, "y": y, "label": "", **kw}


rowA = {"category": "", "name": "", "kind": "tap"}
# 容器 c1 带两组，gA 挂着一条旧「命中→信号」线（一刀切暂停后已下线，
# 载入重建时须被剪除 —— 见下方 "gA 命中线载入即剪" 断言）
cfg.set("custom_flows", {"ct": {"version": 2, "nodes": [
    n("c1", "watch", 0, 0, groups=[
        {"id": "gA", "rows": [dict(rowA)], "cooldown_secs": 7, "fail_limit": 4},
        {"id": "gB", "rows": [dict(rowA), dict(rowA)], "cooldown_secs": 2, "fail_limit": 1},
    ]),
    n("lp", "loop", 400, 0, steps=[], cond={"kind": "template", "category": "", "name": ""},
      max_times=0, on_interrupt="continue"),
], "edges": [{"from": "gA", "port": "hit", "to": "lp", "to_port": "sig"}]}})

panel = FlowPanel()
panel.chk_autosave.setChecked(False)
panel.resize(1400, 900)
panel.show()
wait(300)
panel._select_flow("ct")
wait(300)

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


def select(nid):
    from ui.flow_editor import FlowNodeItem
    panel.scene.clearSelection()
    item = next(it for it in panel.scene.items()
                if isinstance(it, FlowNodeItem) and it.node.get("id") == nid)
    item.setSelected(True)
    wait(150)
    return item.node


# ── 页签出现且编号正确 ──
node = select("c1")
tabs = next((t for t in panel.findChildren(QTabWidget)), None)
check("选中容器 → 属性面板出 QTabWidget", tabs is not None)
check("页签数 = 组数（2）", tabs is not None and tabs.count() == 2)
check("页签标题 = ①②", tabs is not None and
      [tabs.tabText(i) for i in range(tabs.count())] == ["①", "②"])

# ── 编辑命中组 dict：切到第②页改冷却，写进 gB 不碰 gA/容器顶层 ──
tabs.setCurrentIndex(1)
wait(100)
spin = next(s for s in tabs.widget(1).findChildren(QDoubleSpinBox))
check("页内编辑器读的是本组参数（gB cooldown=2）", spin.value() == 2.0)
spin.setValue(9.5)
wait(100)
gs = node["groups"]
check("改值落进 gB", gs[1]["cooldown_secs"] == 9.5)
check("gA 不被误伤", gs[0]["cooldown_secs"] == 7)
check("容器顶层无 cooldown_secs 脏键", "cooldown_secs" not in node)

# ── ＋新建组：页签变 3，空组各就各位 ──
from PySide6.QtWidgets import QPushButton
corner_btns = [b for b in tabs.cornerWidget().findChildren(QPushButton)]
b_add = next(b for b in corner_btns if b.toolTip() == "新建监视组")
b_rm = next(b for b in corner_btns if b.toolTip().startswith("删除当前监视组"))
b_add.click()
wait(200)
node = select("c1")
gs = node["groups"]
tabs = next((t for t in panel.findChildren(QTabWidget)), None)
check("＋后组数=3、页签数=3", len(gs) == 3 and tabs and tabs.count() == 3)
check("新组是当前页（记忆落在末页编号③）", tabs.currentIndex() == 2)

# ── －删当前页（新空组）：不碰线 ──
corner_btns = [b for b in tabs.cornerWidget().findChildren(QPushButton)]
b_rm = next(b for b in corner_btns if b.toolTip().startswith("删除当前监视组"))
b_rm.click()
wait(250)
node = select("c1")
gs = node["groups"]
check("－后回到 2 组", gs is not None and len(gs) == 2)
edges = panel.graph["edges"]
check("gA 老命中线载入重建即剪（信号线通道已下线）",
      not any(e["from"] == "gA" for e in edges))

# ── 删 gA 组：命中线被剪；剩 1 组不摊平（用户拍板 2026-10-05）──
tabs = next((t for t in panel.findChildren(QTabWidget)), None)
tabs.setCurrentIndex(0)
wait(100)
corner_btns = [b for b in tabs.cornerWidget().findChildren(QPushButton)]
b_rm = next(b for b in corner_btns if b.toolTip().startswith("删除当前监视组"))
b_rm.click()
wait(250)
node = next(it.node for it in panel.scene.items()
            if type(it).__name__ == "FlowNodeItem" and it.node.get("type") == "watch")
check("删到 1 组 → 仍是容器（groups 留着、独苗 gB）",
      isinstance(node.get("groups"), list) and len(node["groups"]) == 1
      and node["groups"][0]["id"] == "gB")
check("不摊平则节点 id 不动", node["id"] == "c1")
check("gA 命中线已剪", not any(e["from"] == "gA" for e in panel.graph["edges"]))
tabs_fl = next((t for t in panel.findChildren(QTabWidget) if t.count()), None)
check("－后面板仍是容器页签（只剩 1 页）", tabs_fl is not None and tabs_fl.count() == 1)

# ── 组换序：松在容器内 → 换序不拆出；拖回原位无操作；松在容器外放行给拆出 ──
from PySide6.QtCore import QPointF
from ui.flow_editor import container_groups, group_body_h, NODE_H, NODE_W

# 上一段删剩独苗（仍是容器），这里给同一节点重新摆三组 + 混入一条假命中的
# gA hit 边（模拟老存档漏网）：重建应把它剪干净。节点换个独立 id，
# 免得拆出的组 id=gB 与后文查找撞车
node["id"] = "cX"
node["groups"] = [
    {"id": "gA", "rows": [dict(rowA)], "cooldown_secs": 7, "fail_limit": 4},
    {"id": "gB", "rows": [dict(rowA)], "cooldown_secs": 2, "fail_limit": 1},
    {"id": "gC", "rows": [dict(rowA)], "cooldown_secs": 3, "fail_limit": 2},
]
panel.graph["edges"].append({"from": "gA", "port": "hit", "to": "lp", "to_port": "sig"})
panel._rebuild_scene()
item = panel._node_items[node["id"]]
h = sum(group_body_h(g) for g in node["groups"])
# 把第①组(gA)拖到最底组带内 = 换到末尾
check("容器内落点 → 换序成功",
      panel._try_reorder_group(item, "gA",
                               QPointF(item.pos().x() + 50,
                                       item.pos().y() + NODE_H + h - 5)))
check("顺序变为 B,C,A",
      [g["id"] for g in container_groups(node)] == ["gB", "gC", "gA"])
check("混入的 hit 边被重建剪除（换序只动组序不造线）",
      not [e for e in panel.graph["edges"] if e.get("port") == "hit"])
# gB 现居第①位，落在它自己带内 = 拖回原位，无操作
item = panel._node_items[node["id"]]
check("拖回原位不算改动（返回True但顺序不变）",
      panel._try_reorder_group(item, "gB",
                               QPointF(item.pos().x() + 50, item.pos().y() + NODE_H + 5))
      and container_groups(node)[0]["id"] == "gB")
# 松在容器方框外（下方很远）→ reorder 放行返回 False，交给拆出
check("容器外落点 reorder 放行（交给拆出）",
      not panel._try_reorder_group(item, "gB",
                                   QPointF(item.pos().x() + 50,
                                           item.pos().y() + NODE_H + h + 500)))

# ── 事件层回归守卫（961cdf7c 事故：PySide6 6.11 场景事件只有旧名 pos()/
#    scenePos()，新名一按鼠标就 AttributeError 抓组+拖节点全废；offscreen 喂
#    不进合成鼠标，改用假事件直调处理器，崩在哪行都能暴露）──
class _FakeSceneEv:
    def __init__(self, pos, scenePos, buttons=Qt.LeftButton):
        self._p, self._sp, self._b = pos, scenePos, buttons
    def button(self): return Qt.LeftButton
    def buttons(self): return self._b
    def pos(self): return self._p
    def scenePos(self): return self._sp
    def accept(self): pass

item = panel._node_items[node["id"]]
g0 = container_groups(node)[0]              # 现居①的 gB
hp = QPointF(60, NODE_H + 5)                # ①组标题带内
item.mousePressEvent(_FakeSceneEv(hp, item.mapToScene(hp)))
check("假按下命中①组把手 → 进入抓取态", item._group_pull is not None
      and item._group_pull[0] == g0["id"])
check("按下把手 → 光标攥成握紧手",
      item.cursor().shape() == Qt.CursorShape.ClosedHandCursor)
mp = QPointF(60, NODE_H + 90)
item.mouseMoveEvent(_FakeSceneEv(mp, item.mapToScene(mp)))
check("假拖动越阈值 → moved 置真", item._group_pull and item._group_pull[2])
rp = QPointF(4000, 4000)                    # 容器外松手 = 拆出
item.mouseReleaseEvent(_FakeSceneEv(rp, rp))
check("松在容器外 → 真拆出（watch 节点数 +1）",
      sum(1 for n in panel.graph["nodes"] if n["type"] == "watch") == 2)
gb_node = panel.graph["nodes"][-1]          # 拆出的追加在末尾
check("拆出的也是一台容器（不摊平，用户拍板 2026-10-05）",
      gb_node["id"] == "gB" and container_groups(gb_node) is not None
      and len(gb_node["groups"]) == 1 and gb_node["groups"][0]["id"] == "gB")
check("源容器剩 2 组仍是容器（拆出不摊平源）",
      len(container_groups(panel._node_items["cX"].node)) == 2)

# ── 拆出/合并残影动画（方案A：数据先就位、画布先重建，残影只是视觉回放）──
check("拆出落定 → 组带残影在飞", panel._ghost is not None)
# 飞行途中再重建（模拟用户立刻又拖一下）：动画必须被掐掉且不碰已销毁 item
panel._rebuild_scene()
check("飞行中重建 → 残影被掐、无悬空引用", panel._ghost is None)
# 合并路径：把拆出的 gB 摆回容器身上投进去
gb_item = panel._node_items["gB"]
cont_item = panel._node_items["cX"]   # 现在两台都是容器，按 id 点名别 next 撞车
gb_item.setPos(cont_item.pos().x() + 10, cont_item.pos().y() + 10)
check("投进容器 → 合并成功且残影在飞（吸收式）",
      panel._try_merge_watch(gb_item) and panel._ghost is not None)
wait(400)   # 动画 200ms，等它自然收尾（顺带越过 80ms 选中防抖）
check("动画结束 → 残影自毁，场景清干净",
      panel._ghost is None and not any(
          i.zValue() == 1000 for i in panel.scene.items()))
# 合并面板回归：重建清了选中，不补选容器 → 80ms 后防抖以"没选节点"刷
# 空白表单（实测踩坑，_try_merge_watch 已补 _reselect_node）。
# 上面的 wait(400) 已越过防抖窗口，此刻页签必须还在、容器必须仍选中。
tabs2 = next((t for t in panel.findChildren(QTabWidget) if t.count()), None)
check("合并越过选中防抖 → 容器页签不被刷空白",
      tabs2 is not None and tabs2.count() == 3)   # gC+gA+投回的 gB
sel = [i for i in panel.scene.selectedItems() if type(i).__name__ == "FlowNodeItem"]
check("合并后容器保持选中",
      len(sel) == 1 and container_groups(sel[0].node) is not None)

class _FakeHoverEv(_FakeSceneEv):
    pass
item = next(it for it in panel.scene.items()
            if type(it).__name__ == "FlowNodeItem" and container_groups(it.node))
try:
    item.hoverMoveEvent(_FakeHoverEv(hp, item.mapToScene(hp)))
except TypeError:
    pass   # 末尾 super() 要真类型；光标设置发生在它之前
check("悬停把手带 → 光标变张开手（可抓的视觉预告，同 Figma/n8n）",
      item.cursor().shape() == Qt.CursorShape.OpenHandCursor)
try:
    item.hoverMoveEvent(_FakeHoverEv(QPointF(60, NODE_H + 999),
                                     item.mapToScene(QPointF(60, NODE_H + 999))))
except TypeError:
    pass
check("悬停把手外 → 光标复位", item.cursor().shape() != Qt.CursorShape.OpenHandCursor)

# 源码级守卫：FlowNodeItem 段内绝不许再出现新名（视图层 QMouseEvent 有新名、
# 合法，所以只圈这个类）
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ui", "flow_editor.py"), encoding="utf-8").read()
seg = src.split("class FlowNodeItem", 1)[1].split("\nclass ", 1)[0] \
        .split("\ndef _bezier_path", 1)[0]
check("FlowNodeItem 源码无 position()/scenePosition() 残留",
      ".position()" not in seg and ".scenePosition()" not in seg)

# ── 老存档兼容（2026-09-25 信号线下线）：多组容器挂的两条 hit→sig 边
#    载入重建必须全剪干净——账本里不剩、画布上不画（下次保存落盘即净）。
from ui.flow_editor import FlowEdgeItem
panel.graph = {"version": 2, "nodes": [
    n("c9", "watch", 0, 0, groups=[
        {"id": "c9", "rows": [dict(rowA)], "cooldown_secs": 5, "fail_limit": 3},
        {"id": "g9", "rows": [dict(rowA)], "cooldown_secs": 5, "fail_limit": 3}]),
    n("lp9", "loop", 400, 0, steps=[], cond={"kind": "template", "category": "", "name": ""},
      max_times=0, on_interrupt="continue")],
    "edges": [{"from": "c9", "port": "hit", "to": "lp9", "to_port": "sig"},
              {"from": "g9", "port": "hit", "to": "lp9", "to_port": "sig"}]}
panel._rebuild_scene()
check("老 hit/sig 边账本剪净",
      not [e for e in panel.graph["edges"]
           if e.get("port", "").startswith("hit") or e.get("to_port") == "sig"])
check("画布不画命中线",
      not [i for i in panel.scene.items()
           if isinstance(i, FlowEdgeItem) and i.port.startswith("hit")])

# ── 属性面板宽度回归：哨兵编辑器各行最小需求不许超 318px 视口
#    两条真机踩过的撑宽源，都用长文本喂：
#    ① 偏移行「点选目标位算偏移」钮 126px 塞进 spinbox 行 → 已改钮独占一行；
#    ② 一行三个下拉，长模板名（图标_斜边半手3）把下拉最小宽顶到 126 →
#       已设 minimumContentsLength(4) 封顶，显示省略号、点开看全名。
#    容器页签复用同一编辑器，一测两盖。
box = panel._build_watch_editor(
    {"id": "wchk", "type": "watch", "rows": [
        {"category": "新手引导", "name": "图标_斜边半手3", "kind": "tap",
         "offset_x": -33, "offset_y": -30},
        {"category": "游戏弹窗", "name": "状态_主线奖励", "kind": "subflow",
         "tap_on_hit": True}]})
mw = box.minimumSizeHint().width()
check(f"哨兵编辑器最小宽 {mw} ≤ 318 视口（长名+偏移行）", mw <= 318)

# ── 整台容器并入另一台 + 组名字（用户拍板 2026-10-05：方案A+保名）──
from ui.flow_editor import (make_watch_group, new_watch_node_from_group,
                            collapse_watch_container)

g = lambda gid, lbl="", nr=1: {"id": gid, "label": lbl,
                               "rows": [dict(rowA) for _ in range(nr)],
                               "cooldown_secs": 5, "fail_limit": 3}
panel.graph = {"version": 2, "nodes": [
    n("X", "watch", 0, 0, label="X容器", groups=[g("g1"), g("g2", "有名字组")]),
    n("Y", "watch", 500, 0, label="Y容器", groups=[g("g3")]),
    n("W", "watch", 500, 300, label="W哨兵", rows=[dict(rowA)],
      cooldown_secs=5, fail_limit=3)],
    "edges": []}
panel._rebuild_scene()
ix, iy = panel._node_items["X"], panel._node_items["Y"]
iy.setPos(ix.pos().x() + 10, ix.pos().y() + 10)
check("整台容器并入：成功返回", panel._try_merge_watch(iy))
wait(300)
gx = next(nd for nd in panel.graph["nodes"] if nd["id"] == "X")
ids = [x["id"] for x in container_groups(gx)]
check("Y 的组按原序搬进尾部（g1,g2,g3）", ids == ["g1", "g2", "g3"])
check("无名组冠源容器名「Y容器」",
      container_groups(gx)[2]["label"] == "Y容器")
check("已有名组不被顶名（有名字组原样）",
      container_groups(gx)[1]["label"] == "有名字组")
check("Y 节点消失", not any(nd["id"] == "Y" for nd in panel.graph["nodes"]))
# 单哨兵并入同一容器：make_watch_group 继承节点名
iw = panel._node_items["W"]
ix = panel._node_items["X"]
iw.setPos(ix.pos().x() + 10, ix.pos().y() + 10)
check("单哨兵并入容器仍可用（老路径不回归）", panel._try_merge_watch(iw))
wait(300)
gx = next(nd for nd in panel.graph["nodes"] if nd["id"] == "X")
check("W 变第④组且名「W哨兵」",
      [x["id"] for x in container_groups(gx)] == ["g1", "g2", "g3", "W"]
      and container_groups(gx)[3]["label"] == "W哨兵")
check("画布只剩这一台 watch", sum(1 for nd in panel.graph["nodes"]
                                 if nd["type"] == "watch") == 1)
# 页签标题带组名（无名只显编号；名字全由合并/拆出自动带，无手填行）
select("X")
tabs3 = next((t for t in panel.findChildren(QTabWidget) if t.count()), None)
check("页签标题：① / ② 有名字组 / ③ Y容器 / ④ W哨兵",
      tabs3 is not None and [tabs3.tabText(i) for i in range(4)]
      == ["①", "② 有名字组", "③ Y容器", "④ W哨兵"])
tabs3.setCurrentIndex(0)
wait(150)
mw2 = tabs3.widget(0).minimumSizeHint().width()
check(f"组页最小宽 {mw2} ≤ 318", mw2 <= 318)

# 纯函数：拆出容器形 + 名字自动带（用户拍板：不要手填组名行）
check("组名字手填行已删（组名只由合并/拆出自动带）",
      'f1.addRow("组名字"' not in src)
check("make_watch_group 继承节点名",
      make_watch_group({"id": "q", "label": "甲", "rows": []})["label"] == "甲")
_s = new_watch_node_from_group(g("z", "乙组名"), 0, 0)
check("拆出=容器形·节点名=组名",
      container_groups(_s) == [g("z", "乙组名")] and _s["label"] == "乙组名")
_s2 = new_watch_node_from_group(g("z2"), 0, 0, fallback_label="源容器名")
check("无名组拆出兜底源容器名", _s2["label"] == "源容器名"
      and container_groups(_s2)[0].get("label", "") == "")
_flat = {"id": "host", "type": "watch", "label": "容器名",
         "groups": [g("z", "独苗名")]}
collapse_watch_container(_flat)
check("摊平已废止：剩 1 组原样不动（id/名/groups 全保）",
      len(_flat.get("groups") or []) == 1 and _flat["id"] == "host"
      and _flat["label"] == "容器名")
_flat0 = {"id": "host", "type": "watch", "label": "名", "groups": []}
collapse_watch_container(_flat0)
check("空壳 0 组 → 退回普通空哨兵", "groups" not in _flat0
      and _flat0.get("rows") == [])
check("摊平回写代码已删净（不许复活）", "独苗组的名字回写" not in src)

# ── 2026-10-07 修「换行型后列表弹回顶」：选中记忆按组各记各的 ──
# 病根：_watch_sel_row 是面板级单条，容器多组同场重建时先重建的组把记忆
# 顶成自己的行 → 正在编辑的组回位扑空 → 弹回第0行、滚动条回顶（用户实拍）。
# ⚠ 行数刻意 24：offscreen 行高退化到 ~9px，8 行根本溢不出 150px 视口
#   （滚动判据会假过）；24 行才真逼出滚动条。
panel.graph = {"version": 2, "nodes": [
    n("cc", "watch", 0, 0, groups=[
        {"id": "h1", "rows": [dict(rowA) for _ in range(24)],
         "cooldown_secs": 5, "fail_limit": 3},
        {"id": "h2", "rows": [dict(rowA) for _ in range(24)],
         "cooldown_secs": 5, "fail_limit": 3}]),
], "edges": []}
panel._rebuild_scene()
select("cc")
tabs = next((t for t in panel.findChildren(QTabWidget) if t.count()), None)
tabs.setCurrentIndex(1)
wait(150)
lst2 = tabs.widget(1).findChild(QListWidget)
lst2.setCurrentRow(18)
wait(150)
node_cc = next(it.node for it in panel.scene.items()
               if type(it).__name__ == "FlowNodeItem" and it.node.get("id") == "cc")
panel._rebuild_props(node_cc)   # 模拟换行型/增删行那条整场重建路
wait(150)
tabs = next((t for t in panel.findChildren(QTabWidget) if t.count()), None)
check("重建后仍停在第②页（组页记忆不丢）", tabs.currentIndex() == 1)
lst2 = tabs.widget(1).findChild(QListWidget)
lst1 = tabs.widget(0).findChild(QListWidget)
check("整场重建后第②组选中行仍停在第18行（不弹回第0行）",
      lst2 is not None and lst2.currentRow() == 18)
check("第②组列表把选中行滚进视野（不回顶）",
      lst2 is not None and lst2.verticalScrollBar().value() > 0)
check("第①组不借第②组的记忆（自己没记忆才落第0行）",
      lst1 is not None and lst1.currentRow() == 0)

print("SMOKE PASS" if ok else "SMOKE FAIL")
sys.exit(0 if ok else 1)
