# 冒烟：F4 —— ① 循环内「跳过本轮」(stop_flow scope=continue) ② 变量条件的区间比较
# (between / not_between)。纯引擎为主 + 一小段编辑器面板链路（offscreen）。
# 不碰真实模板库/配置（临时 config 单例 + 临时目录；红线见 CLAUDE.md）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_loop_continue_range.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 临时配置单例：绝不写真 configs/config.json（冒烟写盘陷阱）
import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication.instance() or QApplication(sys.argv)

import bot.fsm as fsm                                   # noqa: E402
from bot.fsm import (LoopStep, StopFlowStep, Step,      # noqa: E402
                     GraphStep, LOOP_CONTINUE, FLOW_END, ABORT)
from bot.tasks.flow import (_make_condition, SubFlowStep,  # noqa: E402
                            _make_build_step, flow_var_set)

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


class Ctx:
    """最小 ctx：引擎只用 log / ld_index / extra / frame 四处。"""
    ld_index = 0
    frame = None
    log = None

    def __init__(self):
        self.extra = {}


class CountStep(Step):
    """计数不落手的步骤（记录被 tick 了几次）。"""

    def __init__(self, name, box):
        super().__init__(name, timeout=0)
        self._box = box

    def tick(self, ctx):
        self._box[0] += 1
        return "ok"


# ══ ① continue（跳过本轮）════════════════════════════════════════════════

# 1a) 循环体：continue 之后的剩余步骤本轮不执行；轮数照常推进；到上限正常收尾
ctx = Ctx()
box = [0]
lp = LoopStep("循", [StopFlowStep("跳过", scope="continue"), CountStep("尾", box)],
              times=2)
check("continue·常量存在且区别于 FLOW_END",
      LOOP_CONTINUE != FLOW_END and LOOP_CONTINUE != ABORT)
check("continue·第1轮：返回 running、剩余步 0 次、轮数=1",
      lp.tick(ctx) == "running" and box[0] == 0 and lp._count == 1)
check("continue·第2轮：剩余步仍 0 次、轮数=2（continue 计入正常轮数）",
      lp.tick(ctx) == "running" and box[0] == 0 and lp._count == 2)
check("continue·第3拍：到轮数上限正常收尾（ok）不死循环",
      lp.tick(ctx) == "ok" and box[0] == 0)

# 1b) continue 之前的前置步骤照常执行（只在命中点截断"本轮剩余"）
ctx2 = Ctx()
front, back = [0], [0]
lp2 = LoopStep("循", [CountStep("前", front), StopFlowStep("跳过", scope="continue"),
                     CountStep("尾", back)], times=2)
r = [lp2.tick(ctx2) for _ in range(3)]
check("continue·命中前的步骤执行、命中后不执行（前=2 尾=0，轮数推进）",
      front[0] == 2 and back[0] == 0)

# 1c) continue + until 退出条件：条件成立出循环走 ok，不无限转
ctx3 = Ctx()
lp3 = LoopStep("循", [StopFlowStep("跳过", scope="continue")],
               until=lambda c: True)      # 第一拍就满足退出 → ok
check("continue·配退出条件：until 成立 → 正常 ok 出口",
      lp3.tick(ctx3) == "ok")

# 1d) 非循环环境边界：continue 一路冒泡（GraphStep 顶层原样上抛）
ctx4 = Ctx()
gs_top = GraphStep("图", {"s": {"id": "s", "type": "stop_flow"}}, [],
                   build_step=lambda nd: StopFlowStep("跳过", scope="continue"),
                   is_action=lambda nd: False)
check("continue·顶层 GraphStep：原样上抛 LOOP_CONTINUE",
      gs_top.tick(ctx4) == LOOP_CONTINUE)

# 1e) 非循环环境边界：子流程体内 continue 冒泡（子流程不是循环，不吸收）
ctx5 = Ctx()
check("continue·子流程体内：原样上抛（不吸收、不算跑完走Y）",
      SubFlowStep("子", [StopFlowStep("跳过", scope="continue")]).tick(ctx5)
      == LOOP_CONTINUE)

# 1f) 编译层：stop_scope=continue 传下去；非法值回落 local；缺键 local（老行为）
bld = _make_build_step({}, [])
s_cont = bld({"id": "s", "type": "stop_flow", "label": "x",
              "stop_scope": "continue"})
s_bad = bld({"id": "s", "type": "stop_flow", "label": "x", "stop_scope": "xyz"})
s_old = bld({"id": "s", "type": "stop_flow", "label": "x"})
check("continue·编译层 stop_scope=continue 照传",
      isinstance(s_cont, StopFlowStep) and s_cont._scope == "continue")
check("continue·编译层非法值回落 local", s_bad._scope == "local")
check("continue·存量 local 逐字不变（缺键默认 local）", s_old._scope == "local")

# 1g) 存量 stop_flow 行为逐字不变（local=FLOW_END / global=ABORT）
ctx6 = Ctx()
check("存量 local：仍返回 FLOW_END",
      StopFlowStep("结束", scope="local").tick(ctx6) == FLOW_END)
check("存量 global：仍返回 ABORT",
      StopFlowStep("结束", scope="global").tick(ctx6) == ABORT)
check("存量 local 循环体：就地吸收跳出循环（ok）",
      LoopStep("循", [StopFlowStep("结束", scope="local")]).tick(Ctx()) == "ok")
check("存量 global 循环体：ABORT 穿透",
      LoopStep("循", [StopFlowStep("结束", scope="global")]).tick(Ctx()) == ABORT)

# 1h) reset：循环体里每轮重进本步，日志不会因 _done 卡住（一次性步骤复位纪律）
st = StopFlowStep("跳过", scope="continue")
lines = []


class LogCtx(Ctx):
    def log(self, msg, lv="INFO"):
        lines.append((lv, msg))


st.tick(LogCtx())          # 第1次：打一行
n1 = len(lines)
st.reset()
st.tick(LogCtx())          # reset 后：再打一行（不因 _done 卡住）
check("continue·reset 后每轮都能再喊一次（日志不哑）", n1 == 1 and len(lines) == 2)

# 1i) 防死循环：continue 无限循环（times=None 不设条件）会一直转 —— 由外层 FSM
#     的退出/上限兜底。这里验证"轮数与拍数成正比、没有同一拍内自旋"。
ctx7 = Ctx()
lp7 = LoopStep("循", [StopFlowStep("跳过", scope="continue")], times=None)
r7 = [lp7.tick(ctx7) for _ in range(5)]
check("continue·无限循环每拍仅推进一轮（不在一拍内自旋）",
      r7 == ["running"] * 5 and lp7._count == 5)

# ══ ② 区间比较（between / not_between）════════════════════════════════════

def mk(**cd):
    ctx = Ctx()
    return _make_condition(cd), ctx


c, cx = mk(kind="var", name="hp", op="between", value="10", value2="20")
check("区间·变量没值 → 恒 False", c(cx) is False)
flow_var_set(cx, "hp", 15)
check("区间 between·命中区间内 15 → True", c(cx) is True)
flow_var_set(cx, "hp", 10)
check("区间 between·下界端点 10 含 → True", c(cx) is True)
flow_var_set(cx, "hp", 20)
check("区间 between·上界端点 20 含 → True", c(cx) is True)
flow_var_set(cx, "hp", 9)
check("区间 between·下界外 9 → False", c(cx) is False)
flow_var_set(cx, "hp", 21)
check("区间 between·上界外 21 → False", c(cx) is False)

# 反填归一（val > value2）
c2, cx2 = mk(kind="var", name="hp", op="between", value="20", value2="10")
flow_var_set(cx2, "hp", 15)
check("区间 between·边界反填 20/10 归一 → 15 命中", c2(cx2) is True)
flow_var_set(cx2, "hp", 25)
check("区间 between·归一后 25 仍越界 → False", c2(cx2) is False)

# not_between 补集
c3, cx3 = mk(kind="var", name="hp", op="not_between", value="10", value2="20")
flow_var_set(cx3, "hp", 15)
check("区间 not_between·区间内 15 → False（补集）", c3(cx3) is False)
flow_var_set(cx3, "hp", 5)
check("区间 not_between·区间外 5 → True（补集）", c3(cx3) is True)
flow_var_set(cx3, "hp", 20)
check("区间 not_between·端点 20 → False（端点算在区间内）", c3(cx3) is False)

# 非数值恒 False（不做字典序）
c4, cx4 = mk(kind="var", name="pos", op="between", value="10", value2="20")
flow_var_set(cx4, "pos", (250, 400))
check("区间·变量非数值 → 恒 False（不做字典序）", c4(cx4) is False)
c5, cx5 = mk(kind="var", name="hp", op="between", value="x", value2="20")
flow_var_set(cx5, "hp", 15)
check("区间·边界非数值 → 恒 False", c5(cx5) is False)
c6, cx6 = mk(kind="var", name="hp", op="not_between", value="", value2="")
flow_var_set(cx6, "hp", 15)
check("区间·边界空串 → 恒 False（not_between 也不误判）", c6(cx6) is False)

# 存量六运算符逐字不变
ops = {"==": (5, "5", True), "!=": (5, "9", True), ">": (6, "5", True),
       ">=": (5, "5", True), "<": (4, "5", True), "<=": (5, "5", True)}
all_ok = True
for o, (v, b, exp) in ops.items():
    cc, cxx = mk(kind="var", name="hp", op=o, value=b)
    flow_var_set(cxx, "hp", v)
    all_ok = all_ok and (cc(cxx) is exp)
check("存量·六运算符 == != > >= < <= 行为逐字不变", all_ok)
cc0, cx0 = mk(kind="var", name="hp", op=">=", value="10")
check("存量·变量没值恒 False", cc0(cx0) is False)
flow_var_set(cx0, "hp", (1, 2))
check("存量·非数值只认 ==/!=，> 判不成立",
      _make_condition({"kind": "var", "name": "hp", "op": ">",
                       "value": "10"})(cx0) is False)


# ══ ③ 编辑器面板链路（offscreen；借 FlowPanel 骨架）════════════════════════
from ui.flow_editor import (FlowPanel, cond_row_summary,  # noqa: E402
                            step_row_summary)
from PySide6.QtWidgets import QComboBox, QLineEdit  # noqa: E402


class FakeScene:
    def update(self):
        pass


f = FlowPanel.__new__(FlowPanel)
f.graph = {"nodes": []}
f.scene = FakeScene()
f._mark_dirty = lambda: None
f._set_status = lambda t: None
f._repaint_node = lambda n: None
f._rebuild_props = lambda n: None

# 3a) stop_flow 属性下拉含「跳过本轮」（存 continue）
node_sf = {"type": "stop_flow", "stop_scope": "local"}
from PySide6.QtWidgets import QFormLayout, QWidget  # noqa: E402
form = QFormLayout()
f._props_stop_flow(form, node_sf)
scope_cmbs = [form.itemAt(i).widget() for i in range(form.rowCount())
              if isinstance(form.itemAt(i).widget(), QComboBox)]
scope_cmbs = [w for w in scope_cmbs if w is not None]
scope_cmb = scope_cmbs[0]
check("面板·stop_flow 范围下拉 3 项（local/global/continue）",
      scope_cmb.count() == 3
      and scope_cmb.findData("continue") >= 0
      and scope_cmb.findData("global") >= 0
      and scope_cmb.findData("local") >= 0)
scope_cmb.setCurrentIndex(scope_cmb.findData("continue"))
check("面板·选「跳过本轮」→ 落 stop_scope=continue",
      node_sf.get("stop_scope") == "continue")

# 3b) 循环体动作行：stop_flow 进类型下拉 + 摘要文案
check("面板·循环体类型表认 stop_flow（step_row_summary 有文案）",
      step_row_summary({"type": "stop_flow", "stop_scope": "continue"})
      == "结束流程·跳过本轮")
check("面板·循环体 stop_flow 摘要（local/global）",
      step_row_summary({"type": "stop_flow", "stop_scope": "local"})
      == "结束流程·当前流程"
      and step_row_summary({"type": "stop_flow", "stop_scope": "global"})
      == "结束流程·整条流程")

# 3c) var 条件区间：op 下拉含 between/not_between；选区间才显示第二个边界框
cond = {"kind": "var", "name": "hp", "op": ">=", "value": "10"}
w_var = f._cond_var(None, cond)
op_cmbs = [c for c in w_var.findChildren(QComboBox)
           if c.findData("between") >= 0]
check("面板·var 运算符下拉含 between/not_between",
      len(op_cmbs) == 1 and op_cmbs[0].findData("between") >= 0
      and op_cmbs[0].findData("not_between") >= 0)
edts = w_var.findChildren(QLineEdit)
# 只认独立值输入框（QComboBox 内部也有个 QLineEdit，别数进去）
val_edts = [e for e in edts if e.parent() is w_var]
check("面板·var 有两个独立值输入框（值 + 上界）", len(val_edts) == 2)
op_cmb = op_cmbs[0]
# 普通运算符：上界框藏（构建期无父，_vis 只藏不显 → isVisibleTo False 或 hidden）
_back = next(e for e in val_edts if e.placeholderText().startswith("上界"))
check("面板·普通运算符（>=）：上界框隐藏",
      not _back.isVisibleTo(w_var) or not _back.isVisible())
op_cmb.setCurrentIndex(op_cmb.findData("between"))
check("面板·选区间：上界框显示", _back.isVisibleTo(w_var) or not _back.isHidden())
_back.setText("20")
check("面板·上界框回写 cond['value2']", cond.get("value2") == "20")
op_cmb.setCurrentIndex(op_cmb.findData(">="))
check("面板·切回普通运算符：上界框再藏",
      not _back.isVisibleTo(w_var) or not _back.isVisible())

# 3d) 摘要：区间条件行显示区间式
check("面板·var 区间摘要（between 显示两端）",
      cond_row_summary({"kind": "var", "name": "hp", "op": "between",
                        "value": "10", "value2": "20"}) == "变量比较·hp10~20")
check("面板·存量 var 摘要逐字不变",
      cond_row_summary({"kind": "var", "name": "hp", "op": ">=",
                        "value": "5"}) == "变量比较·hp>=5")

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke loop-continue-range ALL OK")