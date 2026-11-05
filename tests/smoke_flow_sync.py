# -*- coding: utf-8 -*-
"""
tests/smoke_flow_sync.py — 流程事件唯一出口 + 主窗口对账 冒烟（离屏，临时配置）

验四件事：
1. FlowPanel 各事件路都发 flow_saved(新名, 旧名)：保存=(名,"")、新建=(名,"")、
   改名=(新,旧)、删除=("",旧)；导入路用源码守卫（真走要弹文件框，成本高收益低）
2. MainWindow._sync_flows（借未绑方法挂假 self）：改名就地换任务栏/清单的旧名，
   删除按集合对账剔死项 —— 顺序必须是换名在先、对账在后
3. 任务清单双击发 view_flow_requested(流程名)（新交互：双击=查看，不再是移除）
4. 编辑期校验防炸：畸形图（缺 id / 边不是 dict）不闪退（外壳吞扫描崩溃、
   不亮条）；缺图照常报「缺图」不误报编译；编译探针挂线走源码守卫

红线：临时配置单例（cm._config 换临时文件），绝不写真 custom_flows；
模态框全打桩（offscreen 下 exec/question 会永久阻塞）。
"""
import inspect
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

app = QApplication([])

# ── 临时配置单例：一切写盘都进临时文件 ──
import core.config_manager as cm

_tmp = tempfile.mkdtemp(prefix="smoke_flow_sync_")
cm._config = cm.ConfigManager(os.path.join(_tmp, "config.json"))
from core.config_manager import get_config

cfg = get_config()

FAILS = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def empty_graph():
    return {"version": 2, "nodes": [], "edges": []}


# ── 模态打桩：确认框恒 Yes；输入框返回可改的固定文本 ──
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
_INPUT = ["桩名"]
QInputDialog.getText = staticmethod(lambda *a, **k: (_INPUT[0], True))

# ══════════ 1. FlowPanel：flow_saved 唯一出口 ══════════
cfg.set("custom_flows", {"甲": empty_graph()})
from ui.flow_editor import FlowPanel

panel = FlowPanel()
events = []
panel.flow_saved.connect(lambda n, o: events.append((n, o)))

panel._select_flow("甲")
panel._on_save()                                  # 空画布无体检问题 → 不弹报告
check(events[-1] == ("甲", ""), f"保存发 (名,\"\")：{events[-1]}")

_INPUT[0] = "甲新"
panel._on_rename_flow()
check(events[-1] == ("甲新", "甲"), f"改名发 (新,旧)：{events[-1]}")
check("甲新" in (cfg.get("custom_flows") or {}) and
      "甲" not in (cfg.get("custom_flows") or {}), "改名后库键换新名")

panel._on_del_flow()
check(events[-1] == ("", "甲新"), f"删除发 (\"\",旧)：{events[-1]}")
check((cfg.get("custom_flows") or {}) == {}, "删除后流程库为空")

_INPUT[0] = "新建"
panel._on_new_flow()
check(events[-1] == ("新建", ""), f"新建发 (名,\"\")：{events[-1]}")
check("新建" in (cfg.get("custom_flows") or {}), "新建落进流程库")

# 导入路：真走要弹文件框+造 zip，源码守卫够用（事件全发这一发的口径）
_src = inspect.getsource(FlowPanel._on_import_flows)
check("_emit_flow_saved" in _src, "导入路也发 flow_saved（源码守卫）")
check(inspect.getsource(FlowPanel._emit_flow_saved).count("RuntimeError") == 2,
      "出口 helper 吞信号源无 C++ 对象的 RuntimeError（__new__ 借骨架冒烟路）")

# ══════════ 2. MainWindow._sync_flows：换名在先、对账在后 ══════════
from ui.category_sidebar import TaskSidebar
from ui.main_window import MainWindow
from ui.queue_panel import TaskQueuePanel

cfg.set("custom_flows", {"甲": empty_graph(), "乙": empty_graph()})
cfg.set("task_bar", ["甲", "乙"])
cfg.set("task_order", ["甲", "乙"])
fake = types.SimpleNamespace()
fake.cat_sidebar = TaskSidebar()
fake.queue_panel = TaskQueuePanel()
check(fake.queue_panel.current_queue() == ["甲", "乙"], "清单初始 甲乙")

MainWindow._sync_flows(fake, "甲新", "甲")     # 改名事件（库键已由面板换过）
cfg.set("custom_flows", {"甲新": empty_graph(), "乙": empty_graph()})
check(list(cfg.get("task_bar")) == ["甲新", "乙"],
      f"改名后任务栏就地换名：{cfg.get('task_bar')}")
check(fake.queue_panel.current_queue() == ["甲新", "乙"],
      f"改名后清单就地换名：{fake.queue_panel.current_queue()}")

# 删除对账：库里删掉乙（不带旧名的集合同步事件）
cfg.set("custom_flows", {"甲新": empty_graph()})
MainWindow._sync_flows(fake, "", "乙")
check(list(cfg.get("task_bar")) == ["甲新"],
      f"删除后任务栏剔死项：{cfg.get('task_bar')}")
check(fake.queue_panel.current_queue() == ["甲新"],
      f"删除后清单剔死项：{fake.queue_panel.current_queue()}")

# ══════════ 3. 清单双击 → view_flow_requested(流程名) ══════════
got = []
fake.queue_panel.view_flow_requested.connect(got.append)
it = fake.queue_panel.list.item(0)
check(it is not None, "清单有行可双击")
fake.queue_panel.list.itemDoubleClicked.emit(it)
check(got == ["甲新"], f"双击发流程名（不是移除）：{got}")

# 主窗口接线：双击→查看、工作台事件→对账，都用源码守卫（真开工作台太重）
mw_src = inspect.getsource(MainWindow.open_workbench)
check("flow_saved.connect(self._sync_flows)" in mw_src
      and "closed.connect(self._sync_flows)" in mw_src,
      "主窗口把 flow_saved+closed 都接进 _sync_flows")
check("view_flow_requested.connect(self._on_view_flow)" in
      inspect.getsource(MainWindow.__init__),
      "主窗口把清单双击接进 _on_view_flow")

# ══════════ 4. 编译预判：缺 id 报编译不过、缺图不误报 ══════════
panel.flow_name = "编译探针"
# 实测口径：compile_graph 对结构齐全的图全宽容（缺图/坏类型/幽灵边都不抛）；
# 会抛的畸形形态（缺 id、edges 非 dict）在判据链里先被 detect_cycle 炸掉。
# 所以编辑期这道防线归两道：a) 编译探针只报编译自己抛的（源码守卫在下方）；
# b) 校验外壳兜住扫描崩溃——400ms 定时器在主线程，畸形图炸了就是闪退。
for bad, label in [
    ({"version": 2, "nodes": [{"type": "click_template", "label": "缺id"}],
      "edges": []}, "缺 id 节点"),
    ({"version": 2, "nodes": [{"id": "n1", "type": "click_template",
                               "category": "", "name": ""}], "edges": [1]},
     "边不是 dict"),
]:
    panel.graph = bad
    try:
        panel._refresh_validation()          # 外壳：不许把主线程炸了
        ok = True
    except Exception as e:
        ok = False
        print("  炸了：", type(e).__name__, e)
    check(ok and not panel.lbl_valid.isVisible(),
          f"校验外壳吞掉 {label} 的扫描崩溃且不亮条（不闪退）")

panel.graph = {"version": 2,
               "nodes": [{"id": "n1", "type": "click_template",
                          "category": "smoke不存在的库", "name": "没这张.png",
                          "label": "缺图节点"}],
               "edges": []}
panel._refresh_validation()
txt = panel.lbl_valid.text()
check("缺图" in txt, f"引用缺图 → 报缺图：{txt!r}")
check("编译不过" not in txt, "缺图不误报编译不过（compile_graph 宽容缺图）")

# 编译探针本体（compile_graph 抛 → 横条「编译不过」）：结构齐全的图实测都
# 宽容、到不了 except 分支，行为断言无从构造——源码守卫它挂在判据链上
impl_src = inspect.getsource(FlowPanel._refresh_validation_impl)
check("compile_graph" in impl_src and "编译不过" in impl_src,
      "编译探针已挂进校验判据（源码守卫）")

panel.graph = empty_graph()
panel._refresh_validation()
check(not panel.lbl_valid.isVisible(), "空画布不亮校验条")

print("FAILED:", FAILS or "无")
sys.exit(1 if FAILS else 0)
