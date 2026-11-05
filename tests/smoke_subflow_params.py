# 冒烟：F2 子流程参数 —— subflow 调用绑定（形参 + 实参表达式）
# 覆盖：
# ① 带 params 的 subflow 编译执行后：目标流程变量被赋成求值结果
#    （字面量 10 / 算术 体力-10 / 形参在子流程内可见）。
# ② 实参表达式非法 → 该项跳过、不失败、其它参数照常落值。
# ③ 无 params 的存量节点：执行前后无任何额外变量写入（零污染）。
# ④ 形参名空 → 跳过该项（不写空名变量）。
# ⑤ 设计取舍固定：只做「进入时赋值」不做 restore（同名外层变量子流程内改动不还原，
#    与用户手摆 compute 节点同语义）。
# 红线：全程临时 config 单例，绝不碰真 configs/config.json（不 import Qt）。
# 跑法：python tests/smoke_subflow_params.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

from bot.tasks.flow import (compile_graph, flow_var_get, flow_var_set,
                            _subflow_params)

fails = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        fails.append(name)


class Ctx:
    """引擎 stub：GraphStep 动作步只用 ctx.log / ctx.extra / ctx.frame。"""
    ld_index = 0
    frame = None

    def __init__(self):
        self.extra = {}
        self.lines = []

    def log(self, msg, lv="INFO"):
        self.lines.append((lv, msg))


def _body_compute_readback():
    """子流程体：一个 compute 节点，把 {体力下限} 读回变量「子读回」——
    证明形参在子流程内部确实可见（{名字} 直接引用）。"""
    return {"version": 2, "nodes": [
        {"id": "r", "type": "compute", "expr": "{体力下限}",
         "var_name": "子读回"}], "edges": []}


def run_subflow(node, flow_map, preset=None, beats=80):
    """编译「单 subflow 节点」图并 tick 到收口，返回 (结论, ctx)。"""
    g = {"version": 2, "nodes": [node], "edges": []}
    gs = compile_graph(g, flow_map, "外层")[0]
    ctx = Ctx()
    for k, v in (preset or {}).items():
        flow_var_set(ctx, k, v)
    r = "running"
    for _ in range(beats):
        r = gs.tick(ctx)
        if r != "running":
            break
    return r, ctx


# ── ① 带 params：字面量 + 算术 + 子流程内可见 ─────────────────────────
fm = {"子流程A": _body_compute_readback()}
node = {"id": "s", "type": "subflow", "flow_name": "子流程A",
        "wait_before": False,
        "params": [{"name": "体力下限", "expr": "10"},
                   {"name": "扣完", "expr": "体力 - 10"}]}
r, ctx = run_subflow(node, fm, preset={"体力": 30})
check("带 params 的 subflow 收口 ok", r == "ok")
check("字面量实参 体力下限 == 10", flow_var_get(ctx, "体力下限") == 10)
check("算术实参（体力-10）扣完 == 20", flow_var_get(ctx, "扣完") == 20)
check("形参在子流程内部可见（{体力下限} 读到 10）",
      flow_var_get(ctx, "子读回") == 10)

# 表达式可为裸变量名：expr="体力" → 直接取变量现值
r, ctx = run_subflow(
    {"id": "s2", "type": "subflow", "flow_name": "子流程A", "wait_before": False,
     "params": [{"name": "体力下限", "expr": "体力"}]},
    {"子流程A": {"version": 2, "nodes": [], "edges": []}},
    preset={"体力": 77})
check("裸变量名实参：体力下限 == 77", r == "ok" and flow_var_get(ctx, "体力下限") == 77)


# ── ② 实参非法：该项跳过，其它照常，节点不失败 ───────────────────────
r, ctx = run_subflow(
    {"id": "s3", "type": "subflow", "flow_name": "子流程A", "wait_before": False,
     "params": [{"name": "好", "expr": "5"},
                {"name": "坏", "expr": "1+"}]},
    {"子流程A": {"version": 2, "nodes": [], "edges": []}})
check("非法实参：节点仍 ok（不拦路）", r == "ok")
check("非法实参：合法项照常落值 好 == 5", flow_var_get(ctx, "好") == 5)
check("非法实参：非法项未写 坏 is None", flow_var_get(ctx, "坏") is None)
check("非法实参：打了一行 WARN 点名「坏」",
      any(lv == "WARN" and "坏" in m for lv, m in ctx.lines))

# 类型不匹配：s - 1（变量 s 是字符串）→ 跳过
r, ctx = run_subflow(
    {"id": "s4", "type": "subflow", "flow_name": "子流程A", "wait_before": False,
     "params": [{"name": "t", "expr": "s - 1"}]},
    {"子流程A": {"version": 2, "nodes": [], "edges": []}},
    preset={"s": "abc"})
check("类型不匹配：不写变量且节点 ok",
      r == "ok" and flow_var_get(ctx, "t") is None)


# ── ③ 零污染：无 params 的存量节点 ───────────────────────────────────
r, ctx = run_subflow(
    {"id": "s5", "type": "subflow", "flow_name": "子流程A", "wait_before": False},
    {"子流程A": {"version": 2, "nodes": [], "edges": []}})
check("存量节点（无 params 键）执行 ok", r == "ok")
check("存量节点零污染：ctx.extra 无任何键", ctx.extra == {})

# 显式空表同样零污染
r, ctx = run_subflow(
    {"id": "s6", "type": "subflow", "flow_name": "子流程A", "wait_before": False,
     "params": []},
    {"子流程A": {"version": 2, "nodes": [], "edges": []}})
check("params=[] 零污染：ctx.extra 无任何键", r == "ok" and ctx.extra == {})


# ── ④ 形参名空：跳过该项 ─────────────────────────────────────────────
r, ctx = run_subflow(
    {"id": "s7", "type": "subflow", "flow_name": "子流程A", "wait_before": False,
     "params": [{"name": "", "expr": "9"},
                {"name": "  ", "expr": "9"},
                {"name": "有效", "expr": "1"}]},
    {"子流程A": {"version": 2, "nodes": [], "edges": []}})
check("形参名空：不写空名变量，合法项照旧",
      r == "ok" and flow_var_get(ctx, "") is None
      and flow_var_get(ctx, " ") is None and flow_var_get(ctx, "有效") == 1)
check("形参名空：打了一行 WARN",
      any(lv == "WARN" and "没填名字" in m for lv, m in ctx.lines))


# ── ⑤ 设计取舍：不做 restore（外层同名变量不还原）────────────────────
# 子流程体把 体力下限 改写成 999；外层若预置了同名变量，子流程结束后仍是 999
# （与用户手摆 compute 节点同语义：全局扁平、无作用域隔离）。
fm_w = {"子流程B": {"version": 2, "nodes": [
    {"id": "w", "type": "compute", "expr": "999", "var_name": "体力下限"}],
    "edges": []}}
r, ctx = run_subflow(
    {"id": "s8", "type": "subflow", "flow_name": "子流程B", "wait_before": False,
     "params": [{"name": "体力下限", "expr": "10"}]},
    fm_w)
check("不 restore：子流程内改写外层同名变量后不还原（体力下限==999）",
      r == "ok" and flow_var_get(ctx, "体力下限") == 999)


# ── ⑥ _subflow_params 键解析（纯函数）───────────────────────────────
check("无 params 键 → 空表", _subflow_params({"type": "subflow"}) == [])
check("非 list → 空表", _subflow_params({"params": "坏"}) == [])
check("dict 行原样保留（存储本尊）",
      _subflow_params({"params": [{"name": "a", "expr": "1"}]})
      == [{"name": "a", "expr": "1"}])
check("纯字符串行「名字=表达式」换算",
      _subflow_params({"params": ["体力下限=10"]})
      == [{"name": "体力下限", "expr": "10"}])
check("纯字符串行无 = → 整串当变量名、expr 空",
      _subflow_params({"params": ["裸名"]}) == [{"name": "裸名", "expr": ""}])


# ── ⑦ 编译侧透传：params 落到 SubFlowStep ────────────────────────────
from bot.tasks.flow import SubFlowStep, _make_build_step
_st = _make_build_step({"子流程A": {"version": 2, "nodes": [], "edges": []}}, [])(
    {"id": "s", "type": "subflow", "flow_name": "子流程A", "wait_before": False,
     "params": [{"name": "p", "expr": "1"}]})
check("编译侧：params 透传进 SubFlowStep",
      isinstance(_st, SubFlowStep) and _st.params == [{"name": "p", "expr": "1"}])
_st2 = _make_build_step({"子流程A": {"version": 2, "nodes": [], "edges": []}}, [])(
    {"id": "s", "type": "subflow", "flow_name": "子流程A", "wait_before": False})
check("编译侧：老节点无 params → 空表", isinstance(_st2, SubFlowStep)
      and _st2.params == [])


print("-" * 46)
if fails:
    print("SMOKE FAILED:", fails)
    sys.exit(1)
print("smoke subflow-params ALL OK")