# 冒烟：工作台流程库侧栏的分类本体管理（2026-10-04 新增）
# 守的链路：①空分类不再被藏（重建后标题行出现、文本带组名、UserRole+1==组名）；
# ②新建分类 → flow_category_order 登记 + 落盘（重读临时 config 文件验证）；
# ③重命名 → 所有指向旧名的流程值换新名、order 原位换名、custom_flows 逐字节不变
#   （红线：流程库是用户资产，分类操作只动账本标签）；
# ④删除非空分类（打桩 Yes）→ 组内流程键 pop 回落未分类、order 移除、本体不变；
# ⑤「未分类」伪分类改名/删除函数级硬挡；⑥撞名新建被拒（order 不涨）。
# 全程 FlowPanel.__new__ 借方法（smoke_multi_scan 同款），配置单例换成临时目录，
# 绝不打真实 configs/config.json（冒烟写盘陷阱铁律）；offscreen 下模态框先打桩。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_flow_categories.py
import json
import os
import sys
import tempfile
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (QApplication, QComboBox, QLabel, QListWidget,
                               QListWidgetItem, QInputDialog, QMessageBox,
                               QVBoxLayout, QWidget)

app = QApplication.instance() or QApplication(sys.argv)

# ── offscreen 模态框打桩（QInputDialog/QMessageBox.question 会永久阻塞）──
ANS = {"text": "", "ok": True}          # getText 的回答（各段用例现填）
QInputDialog.getText = staticmethod(lambda *a, **k: (ANS["text"], ANS["ok"]))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

import core.config_manager as cm

# ── 临时配置单例：三个账本函数全走 cfg.save()，绝不碰真实 config ──
_tmpdir = tempfile.mkdtemp(prefix="fc_smoke_")
_cfgpath = os.path.join(_tmpdir, "config.json")
cm._config = cm.ConfigManager(_cfgpath)
_flows = {name: {"version": 2, "nodes": [], "edges": []}
          for name in ("甲", "乙", "丙")}
cm._config.data["custom_flows"] = _flows
cm._config.set("flow_categories", {"甲": "日常", "乙": "日常", "丙": "挂野"})
cm._config.set("flow_category_order", ["日常", "挂野", "空组"])   # "空组"=登记了没塞人
cm._config.save()

import ui.flow_editor as fe
from bot.template_deps import UNCLASSIFIED
from ui.flow_editor import FlowPanel


class FakeScene:
    def update(self):
        pass


ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


def headers():
    """当前列表里的组标题行 [(文本, 分类名)]"""
    out = []
    for i in range(f.flow_list.count()):
        it = f.flow_list.item(i)
        c = it.data(Qt.UserRole + 1)
        if c:
            out.append((it.text(), c))
    return out


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


f = FlowPanel.__new__(FlowPanel)
f.cfg = cm._config
f.graph = {"nodes": []}
f.scene = FakeScene()
f.flow_name = "甲"
f._mark_dirty = lambda: None
msgs = []
f._set_status = lambda t: msgs.append(t)
f._locked_list = lambda: []
f._notes = lambda: {}
f._lock_deleg = type("_D", (), {"set_locked": lambda s, x: None})()
f.flow_list = QListWidget()
f.lbl_flow_title = QLabel()
f.cmb_cat = QComboBox()
f.cmb_cat.setEditable(True)
holder = QWidget()
f.props_layout = QVBoxLayout(holder)

# ── ① 空分类显示 + 组标题挂数据键 ──
f._refresh_flow_list()
hdrs = dict((c, t) for t, c in headers())
check("空组「空组」不再被藏，标题文本带组名与（0）",
      "空组" in hdrs and "空组" in hdrs.get("空组", "")
      and "（0）" in hdrs.get("空组", ""))
check("每个组标题都挂了 UserRole+1=分类名（含未分类兜底组）",
      {c for _, c in headers()} == {"日常", "挂野", "空组", UNCLASSIFIED})
check("未分类组（0 条）也显示", "（0）" in hdrs.get(UNCLASSIFIED, "")
      or f"{UNCLASSIFIED}（0）" in hdrs.get(UNCLASSIFIED, ""))

# ── ⑤a 空分类进工具栏下拉候选（去掉 if ms 过滤）──
f._refresh_category_combo()
combo_data = {f.cmb_cat.itemData(i) for i in range(f.cmb_cat.count())}
check("下拉候选含空组（未分类兜底项照旧）",
      "空组" in combo_data and "" in combo_data)

# ── ⑥ 撞名/坏名新建被拒（order 不涨）──
n0 = len(cm._config.get("flow_category_order"))
for bad in ("日常", UNCLASSIFIED, "  "):
    ANS["text"], ANS["ok"] = bad, True
    f._new_category()
check("撞名/「未分类」/空名新建全被拒，order 不涨",
      len(cm._config.get("flow_category_order")) == n0
      and len(read_saved()["flow_category_order"]) == n0)

# ── ② 新建分类 → order 登记 + 落盘 ──
ANS["text"], ANS["ok"] = "新组", True
f._new_category()
saved = read_saved()
check("新建「新组」→ order 追加 + 已落盘",
      "新组" in cm._config.get("flow_category_order")
      and "新组" in saved["flow_category_order"])
f._refresh_flow_list()
check("新建后侧栏立刻出「新组（0）」",
      any(c == "新组" and "（0）" in t for t, c in headers()))

# ── ③ 重命名：甲乙 日常→日常新、order 原位、本体逐字节不变 ──
before = flows_dump()
ANS["text"], ANS["ok"] = "挂野", True      # 撞名要拒
f._rename_category("日常")
check("重命名撞「挂野」被拒，账本不动",
      cm._config.get("flow_categories") == {"甲": "日常", "乙": "日常", "丙": "挂野"})
ANS["text"], ANS["ok"] = "日常新", True
f._rename_category("日常")
check("重命名→指向旧名的流程全换新名",
      cm._config.get("flow_categories") == {"甲": "日常新", "乙": "日常新", "丙": "挂野"})
order = cm._config.get("flow_category_order")
check("order 原位换名（第 0 位、长度不变）",
      order[0] == "日常新" and len(order) == n0 + 1 and "日常" not in order)
check("重命名不碰流程本体（custom_flows 逐字节不变）", flows_dump() == before)
check("重命名已落盘", read_saved()["flow_categories"]["甲"] == "日常新")

# ── ⑤b 「未分类」伪分类改名/删除硬挡（连弹窗都不该走到——打桩会吞答案，故查账本）──
before2 = flows_dump()
snap_cat = dict(cm._config.get("flow_categories"))
snap_order = list(cm._config.get("flow_category_order"))
ANS["text"], ANS["ok"] = "别的名", True
f._rename_category(UNCLASSIFIED)
f._delete_category(UNCLASSIFIED)
check("「未分类」改名/删除被函数级守卫挡住（账本纹丝不动）",
      cm._config.get("flow_categories") == snap_cat
      and cm._config.get("flow_category_order") == snap_order
      and flows_dump() == before2)

# ── ④ 删除非空分类（Yes 打桩）：丙回落未分类、order 移除、本体不变 ──
f._delete_category("挂野")
check("删「挂野」→ 组内流程键 pop（丙回落未分类）",
      "丙" not in cm._config.get("flow_categories"))
check("删「挂野」→ order 移除",
      "挂野" not in cm._config.get("flow_category_order"))
check("删分类不删流程本体（custom_flows 逐字节不变）", flows_dump() == before2)
f._refresh_flow_list()
hdrs2 = dict((c, t) for t, c in headers())
check("删后侧栏：丙显示在未分类组下",
      UNCLASSIFIED in hdrs2 and "（1）" in hdrs2[UNCLASSIFIED])
it = [f.flow_list.item(i) for i in range(f.flow_list.count())
      if f.flow_list.item(i).data(Qt.UserRole) == "丙"][0]
check("丙仍在流程行（ UserRole 键），没被连带删", it.data(Qt.UserRole) == "丙")

# ── 删空组：直接删（无流程可回落）──
f._delete_category("新组")
check("空组直接删：order 移除",
      "新组" not in cm._config.get("flow_category_order"))

# ── 右键菜单分支：组标题行只出分类管理项（不 exec，离屏弹不了；构造 QMenu 验证）──
# 直接照抄 _on_list_context_menu 的分支判定逻辑对行数据键的依赖：
hdr_item = [f.flow_list.item(i) for i in range(f.flow_list.count())
            if f.flow_list.item(i).data(Qt.UserRole + 1) == UNCLASSIFIED][0]
check("未分类组标题：UserRole 无值（不走流程菜单支）+ UserRole+1 有值（走分类菜单支）",
      not hdr_item.data(Qt.UserRole)
      and hdr_item.data(Qt.UserRole + 1) == UNCLASSIFIED)

# ── ⑦ 2026-10-07 修「点流程库随机位移」：普通单击绝不整表重建 ──
# 病根：_select_flow 无条件 clear+重灌 → 滚动条归零后 setCurrentRow 又把选中行
# 滚到视口边缘 → 鼠标底下冒出别的行（用户实拍：点新手魂灵任务，封号突破滑到
# 鼠标位）。重建后同步恢复滚动位救不回（滚动条范围延迟刷新会被钳，实测 21）。
# 修法：按下时点中行已是 currentItem（高亮就位）→ 点击路跳过重建；
# 只有列表当前选中不是目标流程（edit_flow 等外部入口）才重建同步高亮。
# 30 条流程才逼得出滚动条（offscreen 行高退化 ~7px，150px 视口要 >21 行才溢出）。
cm._config.data["custom_flows"] = {
    **_flows, **{f"f{i}": {"version": 2, "nodes": [], "edges": []}
                 for i in range(30)}}
f.flow_name = "f15"
f._dirty = False
f._reload_flow = lambda: None   # 借骨架不跑画布重载（镜头那条道有专门冒烟）
f.flow_list.resize(200, 150)
f.flow_list.show()
app.processEvents()
f._refresh_flow_list()
sb = f.flow_list.verticalScrollBar()
sb.setValue(60)
app.processEvents()
saved_scroll = sb.value()
check(f"⑦ 前提：30 条流程滚得动且真滚了位（{saved_scroll} > 0）",
      saved_scroll > 0)
mouse_pt = QPoint(100, 75)   # 视口中点 = 用户鼠标按住的位置
hit_row = f.flow_list.indexAt(mouse_pt).row()
row_under = (f.flow_list.item(hit_row).data(Qt.UserRole)
             if hit_row >= 0 else None)
check("⑦ 前提：鼠标位下是流程行且不是当前选中流程（位移才暴露得出来）",
      bool(row_under) and row_under != "f15")
# 模拟真实单击：Qt 按下先把点中行设成 currentItem → _select_flow
f.flow_list.setCurrentRow(hit_row)
f._select_flow(row_under)
app.processEvents()
new_under = f.flow_list.item(f.flow_list.indexAt(mouse_pt).row()).data(
    Qt.UserRole) if f.flow_list.indexAt(mouse_pt).row() >= 0 else None
check(f"⑦ 点「{row_under}」后鼠标位下的行没换人（→ {new_under}）",
      new_under == row_under)
check(f"⑦ 滚动条纹丝不动（{saved_scroll} → {sb.value()}）",
      sb.value() == saved_scroll)
check("⑦ 切换照常生效：flow_name/currentItem 都指到点中行",
      f.flow_name == row_under and row_under is not None
      and f.flow_list.currentItem().data(Qt.UserRole) == row_under)
# 外部入口（edit_flow 场景）：列表当前指着别处 → 必须重建把高亮跟过去
f.flow_list.setCurrentRow(0)
f._select_flow("f15")
check("⑦ 外部入口仍重建同步高亮（currentItem 跟到 f15）",
      f.flow_list.currentItem().data(Qt.UserRole) == "f15")

# ── ⑧ 新建流程自动归档：在当前分类下加流程 = 归当前分类 ──
# 病根：_on_new_flow 从不写分类标签，用户在「挂野」组里右键新建，流程却掉进
# 「未分类」，得再手动拖一次。修法：发起处的组上下文（右键组标题/组内流程行）
# 走 _drop_cat_name 现算，缺则回落当前流程所属分类。
from ui.flow_editor import _drop_cat_name

# 右键行 → 分类 的纯函数口径（组标题认 UserRole+1；流程行认所在组 UserRole+3）
ii = QListWidgetItem("组标题行")
ii.setData(Qt.UserRole + 1, "挂野")
check("⑧ 落点取组标题行 → 分类名", _drop_cat_name(ii) == "挂野")
ii2 = QListWidgetItem("流程行")
ii2.setData(Qt.UserRole, "某流程")
ii2.setData(Qt.UserRole + 3, "日常")
check("⑧ 落点取组内流程行 → 其所在组", _drop_cat_name(ii2) == "日常")
check("⑧ 空白处（无行）→ None（回落当前分类）", _drop_cat_name(None) is None)

# ① 带 cat 新建 → 直接归该分类（无需先有当前流程）
f.flow_name = "甲"     # 甲=日常新（重命名段已改）；此处只作"当前流程"，重点看 cat 优先
ANS["text"], ANS["ok"] = "群A", True
f._on_new_flow("挂野")
check("⑧ 在「挂野」里新建 → 新流程归「挂野」（cat 优先于当前流程分类）",
      cm._config.get("flow_categories", {}).get("群A") == "挂野"
      and "群A" in cm._config.get("custom_flows"))
check("⑧ 新流程本体是空图（不夹私货）",
      cm._config.get("custom_flows")["群A"] == {"version": 2, "nodes": [], "edges": []})
f._refresh_flow_list()
check("⑧ 重建后侧栏「挂野」组里出现「群A」",
      any(f.flow_list.item(i).data(Qt.UserRole) == "群A"
          and f.flow_list.item(i).data(Qt.UserRole + 3) == "挂野"
          for i in range(f.flow_list.count())))

# ② 不带 cat（空白处发起）→ 回落"当前流程所属分类"
f.flow_name = "乙"     # 乙=日常新（重命名段已改）
ANS["text"], ANS["ok"] = "群B", True
f._on_new_flow(None)
check("⑧ 空白处新建 → 回落当前流程「乙」的分类「日常新」",
      cm._config.get("flow_categories", {}).get("群B") == "日常新")
f.flow_name = "群B"

# ③ 当前流程也没分类（未分类）→ 新流程同样未分类（老行为）
f.flow_name = "丙"     # 丙在删分类段已回落未分类（无标签）
ANS["text"], ANS["ok"] = "群C", True
f._on_new_flow(None)
check("⑧ 当前流程未分类 + 空白处新建 → 新流程也留未分类（不偷造标签）",
      "群C" not in (cm._config.get("flow_categories", {}) or {})
      and "群C" in cm._config.get("custom_flows"))

# ④ 新建随即落盘（流程库是资产，崩溃不该丢刚建的流程）
f.flow_name = "甲"
ANS["text"], ANS["ok"] = "群D", True
f._on_new_flow("挂野")
check("⑧ 新建的分类标签已落盘（flows.json 读回即见）",
      read_saved()["flow_categories"].get("群D") == "挂野")

# ⑤ 右键菜单接线：新建项把发起行现算的分类传下去（源码守卫）
_src_new = inspect.getsource(FlowPanel._on_list_context_menu)
check("⑧ 右键「新建流程…」携带发起处分类（_drop_cat_name(item)）",
      "_on_new_flow(_drop_cat_name(item))" in _src_new)

# ⑥ 三处写分类口径合一：移到分类/工具栏下拉/新建 都走 _write_flow_category
check("⑧ 分类写入单入口（_move_flow_to/_on_category_changed 复用同一 helper）",
      "_write_flow_category" in inspect.getsource(FlowPanel._move_flow_to)
      and "_write_flow_category" in inspect.getsource(FlowPanel._on_category_changed)
      and "_write_flow_category" in inspect.getsource(FlowPanel._on_new_flow))

print("SMOKE " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
