# 冒烟：F1 变量算术 / 计数 —— 安全求值器 bot.expr + 「计算」节点
# 覆盖两条链：
# ① eval_expr 白名单求值：整数结果/变量直写/{x} 花括号/abs/max；
#    除零、__import__、语法错、str 参与减法 一律抛 ExprError。
# ② compute 节点：编译一条极简图跑通，流程变量被写入正确值；
#    表达式非法时变量不被写、节点不失败（整图 ok）；变量名空 → 跳过不写。
# 红线：全程临时 config 单例，绝不碰真 configs/config.json（不 import Qt）。
# 跑法：python tests/smoke_expr_compute.py
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

from bot.expr import ExprError, eval_expr
from bot.tasks.flow import compile_graph, flow_var_get

fails = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        fails.append(name)


# ── ① eval_expr：白名单求值 ──────────────────────────────────────────
check("10-5 == 5（整数结果保 int）", eval_expr("10-5", None) == 5)
check("10/2 归一为 int 5", eval_expr("10/2", None) == 5)
check("10/4 保 float 2.5", eval_expr("10/4", None) == 2.5)
check("{x}*2（lookup 提供 x=21）", eval_expr("{x}*2", lambda n: {"x": 21}.get(n)) == 42)
check("x*2 变量名直写", eval_expr("x*2", lambda n: {"x": 21}.get(n)) == 42)
check("abs(-3) == 3", eval_expr("abs(-3)", None) == 3)
check("max(1,5,2) == 5", eval_expr("max(1,5,2)", None) == 5)
check("min(1,5,2) == 1", eval_expr("min(1,5,2)", None) == 1)
check("体力 - 50（中文变量名直写）",
      eval_expr("体力 - 50", lambda n: {"体力": 120}.get(n)) == 70)


def raises_expr(text, lookup=None):
    try:
        eval_expr(text, lookup)
        return False
    except ExprError:
        return True
    except Exception:
        return False


check("除零 1/0 抛 ExprError", raises_expr("1/0"))
check("__import__('os') 抛 ExprError", raises_expr("__import__('os')"))
check("1+ 语法错抛 ExprError", raises_expr("1+"))
check("str 参与减法 'abc'-1 抛 ExprError", raises_expr("'abc' - 1"))
check("变量是 str 参与减法 抛 ExprError",
      raises_expr("s - 1", lambda n: {"s": "abc"}.get(n)))
check("属性访问 (1).__class__ 抛 ExprError", raises_expr("(1).__class__"))


# ── ② compute 节点：编译执行 ─────────────────────────────────────────
class Ctx:
    """引擎 stub：GraphStep 动作步只用 ctx.log / ctx.extra / ctx.frame。"""
    ld_index = 0
    frame = None

    def __init__(self):
        self.extra = {}
        self.lines = []

    def log(self, msg, lv="INFO"):
        self.lines.append((lv, msg))


def run_graph(node, beats=40):
    """编译单节点图并 tick 到收口，返回 (结论, ctx)。"""
    g = {"version": 2, "nodes": [node], "edges": []}
    steps = compile_graph(g, {}, "t")
    gs = steps[0]
    ctx = Ctx()
    r = "running"
    for _ in range(beats):
        r = gs.tick(ctx)
        if r != "running":
            break
    return r, ctx


# 正常：10-5 → r=5
r, ctx = run_graph({"id": "c1", "type": "compute", "expr": "10-5", "var_name": "r"})
check("compute 编译执行收口 ok", r == "ok")
check("compute 写入 r == 5", flow_var_get(ctx, "r") == 5)

# 变量名直写 + 花括号占位符：先读体力，再 体力-50
r, ctx = run_graph({"id": "c2", "type": "compute",
                    "expr": "{体力} - 50", "var_name": "体力"})
# 体力未读到时占位符留 {体力} → 算式非法 → 跳过（不写）
check("未读到变量时占位符不展开 → 跳过不写",
      r == "ok" and flow_var_get(ctx, "体力") is None)

# 预置变量后计算：体力=120 → 体力-50=70
g = {"version": 2, "nodes": [
        {"id": "p", "type": "compute", "expr": "0+120", "var_name": "体力"},
        {"id": "q", "type": "compute", "expr": "体力 - 50", "var_name": "体力"},
     ], "edges": [{"from": "p", "port": "out", "to": "q"}]}
gs = compile_graph(g, {}, "t")[0]
ctx = Ctx()
r = "running"
for _ in range(40):
    r = gs.tick(ctx)
    if r != "running":
        break
check("两节点链：先写 120 再 -50", r == "ok" and flow_var_get(ctx, "体力") == 70)

# max 函数：max(0, 体力-999) → 0
r, ctx = run_graph({"id": "c3", "type": "compute",
                    "expr": "max(0, 5-999)", "var_name": "m"})
check("compute 支持 max()", r == "ok" and flow_var_get(ctx, "m") == 0)

# 非法表达式：变量不被写、节点不失败（整图 ok）
r, ctx = run_graph({"id": "c4", "type": "compute", "expr": "1+", "var_name": "bad"})
check("非法表达式：节点仍 ok（不拦路）", r == "ok")
check("非法表达式：变量不被写", flow_var_get(ctx, "bad") is None)
check("非法表达式：打了一行 WARN 点名",
      any(lv == "WARN" and "bad" in m for lv, m in ctx.lines))

# 类型不匹配：str - 1 → 跳过
r, ctx = run_graph({"id": "c5", "type": "compute",
                    "expr": "'abc' - 1", "var_name": "s"})
check("类型不匹配：不写变量且节点 ok",
      r == "ok" and flow_var_get(ctx, "s") is None)

# var_name 为空 → WARN 跳过，不写空名变量
r, ctx = run_graph({"id": "c6", "type": "compute", "expr": "1+1", "var_name": ""})
check("变量名空：节点 ok 且不写空名变量",
      r == "ok" and flow_var_get(ctx, "") is None)
check("变量名空：打了一行 WARN",
      any(lv == "WARN" and "变量名" in m for lv, m in ctx.lines))

print("-" * 46)
if fails:
    print("SMOKE FAILED:", fails)
    sys.exit(1)
print("smoke expr-compute ALL OK")