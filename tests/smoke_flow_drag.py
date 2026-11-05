# 冒烟：工作台流程库侧栏「拖流程→落目标分类=换分类」（2026-10-04 新增；
# 同日真机反馈修复：落点从 20px 组标题行扩到整组区——拖到组内流程行也算，
# 高亮始终挂组标题；拖起带影子标签）
# 守的链路：①判定纯函数——落点（标题行→分类名、流程行带组标→所在组、
#   无组标/None→None，不解析「▼ xxx（N）」文本）、拖起资格（只有流程行）、
#   mime 组装往返；
# ②合成真 drop：打到标题行/组内流程行 → flowDropped(流程名, 分类名) 且事件被接；
#   打到空白/外部 mime → 零回调且不接；
# ③高亮标记 UserRole+2：悬停组标题或组内流程行都挂到组标题行，移开/无效位摘掉；
# ④落账联动（FlowPanel.__new__ 借方法骨架，smoke_flow_categories 同款）：
#   回调接 _move_flow_to → flow_categories 改值 + order 追加 + 落盘，
#   custom_flows 逐字节不变；落「未分类」标题 = 键 pop（取消分类）；空组也能拖进。
# 全程临时 config 单例，绝不打真实 configs/config.json（冒烟写盘陷阱铁律）；
# 本任务无模态框路径（改名/删除都不走），无需打桩。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_flow_drag.py
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtCore import QMimeData, QPoint, Qt
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent
from PySide6.QtWidgets import QAbstractItemView, QApplication, QLabel, QListWidgetItem

app = QApplication.instance() or QApplication(sys.argv)

import core.config_manager as cm

# ── 临时配置单例：落账段走 cfg.save()，绝不碰真实 config ──
_tmpdir = tempfile.mkdtemp(prefix="fd_smoke_")
_cfgpath = os.path.join(_tmpdir, "config.json")
cm._config = cm.ConfigManager(_cfgpath)
_flows = {name: {"version": 2, "nodes": [], "edges": []}
          for name in ("甲", "乙")}
cm._config.data["custom_flows"] = _flows
cm._config.set("flow_categories", {"甲": "日常", "乙": "日常"})
cm._config.set("flow_category_order", ["日常", "挂野"])   # "挂野"=登记了没塞人的空组
cm._config.save()

import ui.flow_editor as fe
from bot.template_deps import UNCLASSIFIED
from ui.flow_editor import FlowPanel

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


def flows_dump():
    """custom_flows 快照（逐字节比对的基准）"""
    return json.dumps(cm._config.data["custom_flows"],
                      ensure_ascii=False, sort_keys=True)


def read_saved():
    """流程资产分流后合页读回：设置落 config.json、资产落同目录 flows.json"""
    with open(_cfgpath, encoding="utf-8") as fh:
        data = json.load(fh)
    flows_p = os.path.join(os.path.dirname(_cfgpath), "flows.json")
    if os.path.exists(flows_p):
        with open(flows_p, encoding="utf-8") as fh:
            data.update(json.load(fh))
    return data


# ── ① 判定纯函数（裸 QListWidgetItem，不挂列表）──
hdr = QListWidgetItem("▼ 日常（1）")
hdr.setFlags(Qt.NoItemFlags)
hdr.setData(Qt.UserRole + 1, "日常")
frow = QListWidgetItem("甲")
frow.setData(Qt.UserRole, "甲")
frow_g = QListWidgetItem("甲")
frow_g.setData(Qt.UserRole, "甲")
frow_g.setData(Qt.UserRole + 3, "日常")
check("落点判定：标题行→分类名 / 流程行带组标→所在组 / 无组标→None / None→None",
      fe._drop_cat_name(hdr) == "日常"
      and fe._drop_cat_name(frow_g) == "日常"
      and fe._drop_cat_name(frow) is None
      and fe._drop_cat_name(None) is None)
check("拖起资格：流程行→名 / 标题行→空串 / None→空串",
      fe._drag_flow_name(frow) == "甲"
      and fe._drag_flow_name(hdr) == ""
      and fe._drag_flow_name(None) == "")
md = fe._flow_mime_of("甲")
check("mime 组装往返：自家格式带流程名；空白 mime 取不到",
      md.hasFormat(fe.FLOW_DRAG_MIME) and fe._flow_name_of_mime(md) == "甲"
      and fe._flow_name_of_mime(QMimeData()) == "")

# ── ②③ 合成真 drop / 悬停高亮（独立 _FlowLibList）──
lst = fe._FlowLibList()
h1 = QListWidgetItem("▼ 日常（1）")
h1.setFlags(Qt.NoItemFlags)
h1.setData(Qt.UserRole + 1, "日常")
i1 = QListWidgetItem("甲")
i1.setData(Qt.UserRole, "甲")
i1.setData(Qt.UserRole + 3, "日常")     # 组标：_refresh_flow_list 真重建时同款手打
h2 = QListWidgetItem(f"▼ {UNCLASSIFIED}（0）")
h2.setFlags(Qt.NoItemFlags)
h2.setData(Qt.UserRole + 1, UNCLASSIFIED)
for it in (h1, i1, h2):
    lst.addItem(it)
lst.resize(168, 300)
lst.show()
app.processEvents()
check("离屏出几何：流程行有可视矩形（drop 位置测试不空转）",
      lst.visualItemRect(i1).height() > 0)


def mk_ev(pos, mime):
    ev = QDropEvent(QPoint(pos), Qt.MoveAction, mime,
                    Qt.LeftButton, Qt.NoModifier)
    ev._mime_keep = mime          # 防 Shiboken 回收（事件只存指针）
    return ev


got = []
lst.flowDropped.connect(lambda n, c: got.append((n, c)))
p_hdr = lst.visualItemRect(h1).center()
p_flow = lst.visualItemRect(i1).center()
p_empty = QPoint(lst.viewport().width() // 2, lst.viewport().height() - 2)

ev = mk_ev(p_hdr, md)
lst.dropEvent(ev)
check("drop 打到标题行 → flowDropped(甲, 日常)、事件被接",
      got == [("甲", "日常")] and ev.isAccepted())

ev = mk_ev(p_flow, md)
lst.dropEvent(ev)
check("drop 打到组内流程行 → 也算落进该组（整组区是落点——真机反馈修复）",
      got == [("甲", "日常"), ("甲", "日常")] and ev.isAccepted())

ev = mk_ev(p_empty, md)
lst.dropEvent(ev)
check("drop 打到空白 → 零回调", len(got) == 2 and not ev.isAccepted())

ev = mk_ev(p_hdr, QMimeData())     # 外部拖放（别的程序的 mime）
lst.dropEvent(ev)
check("drop 外部 mime → 不理会（零回调）", len(got) == 2
      and not ev.isAccepted())

ev = QDragEnterEvent(QPoint(p_flow), Qt.MoveAction, md, Qt.LeftButton, Qt.NoModifier)
ev._mime_keep = md
lst.dragEnterEvent(ev)
lst.dragMoveEvent(mk_ev(p_flow, md))
check("悬停组内流程行：高亮挂到组标题、流程行本身不挂",
      bool(h1.data(fe._FLOW_HL_ROLE)) and i1.data(fe._FLOW_HL_ROLE) is None)
lst.dragMoveEvent(mk_ev(p_empty, md))
check("移开组区（空白）：高亮摘掉", not h1.data(fe._FLOW_HL_ROLE))
ev = mk_ev(p_hdr, md)
lst.dragMoveEvent(ev)
check("悬停合法标题行：UserRole+2 高亮标记挂上、事件被接",
      bool(h1.data(fe._FLOW_HL_ROLE)) and ev.isAccepted())
lst.dragMoveEvent(mk_ev(p_flow, md))
check("组区内挪动：高亮不摘（同一组标题持续亮）", bool(h1.data(fe._FLOW_HL_ROLE)))
lst.dragMoveEvent(mk_ev(p_hdr, md))
lst.dragLeaveEvent(QDragLeaveEvent())
check("拖出列表：高亮标记摘掉", not h1.data(fe._FLOW_HL_ROLE))
check("拖拽模式：dragEnabled 开、未启用内置 InternalMove（顺序派生、自由排行会被重建冲掉）",
      lst.dragEnabled()
      and lst.dragDropMode() != QAbstractItemView.DragDropMode.InternalMove)

# ── ④ 落账联动：flowDropped → FlowPanel._move_flow_to（借方法骨架）──
class FakeScene:
    def update(self):
        pass


f = FlowPanel.__new__(FlowPanel)
f.cfg = cm._config
f.graph = {"nodes": []}
f.scene = FakeScene()
f.flow_name = "甲"
f._mark_dirty = lambda: None
f._set_status = lambda t: None
f._locked_list = lambda: []
f._notes = lambda: {}
f._lock_deleg = type("_D", (), {"set_locked": lambda s, x: None})()
f.flow_list = fe._FlowLibList()
f.flow_list.flowDropped.connect(f._move_flow_to)
f.lbl_flow_title = QLabel()
f._refresh_flow_list()
check("重建给流程行打组标（UserRole+3=所在组，整组区认落点的数据源）",
      all(f.flow_list.item(i).data(Qt.UserRole + 3) == "日常"
          for i in range(f.flow_list.count())
          if f.flow_list.item(i).data(Qt.UserRole)))
f.flow_list.resize(168, 400)
f.flow_list.show()
app.processEvents()
before = flows_dump()


def hdr_of(cat):
    """重建后按 UserRole+1 找回分类标题行（拖哪组都用现找的行，防旧引用）"""
    for i in range(f.flow_list.count()):
        it = f.flow_list.item(i)
        if it.data(Qt.UserRole + 1) == cat:
            return it
    return None


# 拖「甲」进空组「挂野」：空分类正是靠拖才有入口的
h = hdr_of("挂野")
f.flow_list.dropEvent(mk_ev(f.flow_list.visualItemRect(h).center(),
                            fe._flow_mime_of("甲")))
check("落账：甲→挂野（空组也能拖进）、order 不动、已落盘",
      cm._config.get("flow_categories") == {"甲": "挂野", "乙": "日常"}
      and cm._config.get("flow_category_order") == ["日常", "挂野"]
      and read_saved()["flow_categories"]["甲"] == "挂野")
check("落账不碰流程本体（custom_flows 逐字节不变）", flows_dump() == before)
h = hdr_of(UNCLASSIFIED)
f.flow_list.dropEvent(mk_ev(f.flow_list.visualItemRect(h).center(),
                            fe._flow_mime_of("乙")))
check("落「未分类」标题 = 取消分类（账本键 pop）",
      cm._config.get("flow_categories") == {"甲": "挂野"}
      and read_saved()["flow_categories"] == {"甲": "挂野"})
check("取消分类后重建：乙显示在未分类组、本体仍未动",
      flows_dump() == before
      and any(f.flow_list.item(i).data(Qt.UserRole) == "乙"
              for i in range(f.flow_list.count())))

print("SMOKE " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
