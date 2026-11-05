"""
bot/expr.py — 安全算术求值器（流程「计算」节点用）

零依赖：只用标准库（ast / operator / re），不 import 项目其它模块、不 import
PySide6 —— 供 bot 侧运行时调用。

设计（白名单求值，绝不执行任意代码）：
- 认的 AST 节点只有：Expression、BinOp（+ - * / // % **）、UnaryOp（正/负）、
  Constant（数字）、Name、Call（仅白名单函数）。
- Name 走 lookup(name) 现场解析 —— 流程变量名可直接写（如「体力 - 50」里的
  「体力」，Python 标识符支持中文，本身就能 parse 通过）。
- 白名单函数：abs / min / max / round / int / float / len（len 认字符串等序列）。
- 除零、类型不匹配（拿字符串做减法）、任何白名单外的 AST 节点（__import__、
  属性访问 Attribute、列表/字典/集合、比较/下标/推导式…）一律抛 ExprError，
  绝不执行。
- 结果语义：能成整数就返回 int（10-5 → 5、10/2 → 5），含小数回 float（10/4 → 2.5）。
  本版不做字符串运算：字符串参与算术直接抛 ExprError。
- 额外容忍「{变量名}」花括号写法（等价于变量名直写，与 _fmt_text 占位符口径一致）：
  计算节点运行时已先经 _fmt_text 展开，这里只是让 eval_expr 直接调用也认。

自测：python -m bot.expr
"""

from __future__ import annotations

import ast
import operator
import re


class ExprError(Exception):
    """表达式非法 / 求值失败（语法错、白名单外节点、类型不匹配、除零等）。"""


# 白名单二元运算符
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# 白名单一元运算符（只认正/负，不认 not 等）
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# 幂运算防炸：整数指数绝对值封顶（9**9**9 这种直接拒，别把内存吃穿）
_MAX_POW = 512

# 花括号写法：{变量名} → 变量名（等价直写，仅去括号，值由 lookup 现取）
_BRACE_RE = re.compile(r"\{\s*([^{}]+?)\s*\}")


def _num(v):
    """算术操作数只许数值。bool 虽是 int 子类但流程变量里不当数算（一并拒）；
    str / None / 元组 / 列表参与算术 → ExprError（本版不做字符串运算）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ExprError(f"「{v!r}」不是数字，不能参与算术")
    return v


def _norm(v):
    """整数结果归一：float 且无小数部分 → int（10/2 → 5 而不是 5.0），
    便于后续变量比较；真带小数则保持 float。"""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _fn_abs(x):
    return abs(_num(x))


def _fn_min(*args):
    if not args:
        raise ExprError("min() 至少要一个参数")
    return min(_num(a) for a in args)


def _fn_max(*args):
    if not args:
        raise ExprError("max() 至少要一个参数")
    return max(_num(a) for a in args)


def _fn_round(x, n=None):
    x = _num(x)
    if n is None:
        return round(x)
    if isinstance(n, bool) or not isinstance(n, int):
        raise ExprError("round 的第二参数须是整数位数")
    return round(x, n)


def _fn_int(x):
    if isinstance(x, bool) or isinstance(x, (int, float)):
        return int(x)
    if isinstance(x, str):
        try:
            return int(x.strip())
        except ValueError as e:
            raise ExprError(f"int({x!r}) 不是整数") from e
    raise ExprError(f"int() 不接受 {type(x).__name__}")


def _fn_float(x):
    if isinstance(x, bool) or isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.strip())
        except ValueError as e:
            raise ExprError(f"float({x!r}) 不是数字") from e
    raise ExprError(f"float() 不接受 {type(x).__name__}")


def _fn_len(x):
    if isinstance(x, (str, list, tuple)):
        return len(x)
    raise ExprError("len() 只认字符串/列表/元组")


# 白名单函数表（键必须是合法且唯一的名字，不认识的函数名直接拒）
_FUNCS = {
    "abs": _fn_abs,
    "min": _fn_min,
    "max": _fn_max,
    "round": _fn_round,
    "int": _fn_int,
    "float": _fn_float,
    "len": _fn_len,
}


def _eval(node, lookup):
    """递归求值（只走白名单分支，其余一律抛 ExprError）。"""
    if isinstance(node, ast.Expression):
        return _eval(node.body, lookup)
    if isinstance(node, ast.Constant):
        v = node.value
        # 数字常量随便用；字符串常量只许当函数参数（如 len('abc')）——
        # 一旦参与算术，_num() 会拒（本版不做字符串运算）。bool 不当数算。
        if isinstance(v, bool):
            raise ExprError("不支持布尔常量")
        if isinstance(v, (int, float, str)):
            return v
        raise ExprError(f"不支持的常量：{v!r}")
    if isinstance(node, ast.Name):
        try:
            return lookup(node.id)
        except Exception as e:
            raise ExprError(f"取变量「{node.id}」失败：{e}") from e
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ExprError(f"不支持的运算符：{type(node.op).__name__}")
        left = _num(_eval(node.left, lookup))
        right = _num(_eval(node.right, lookup))
        if isinstance(node.op, ast.Pow) and isinstance(right, int) \
                and abs(right) > _MAX_POW:
            raise ExprError(f"幂次过大（|指数| > {_MAX_POW}），拒绝执行")
        try:
            return _norm(op(left, right))
        except ZeroDivisionError as e:
            raise ExprError("除数为零") from e
        except (OverflowError, ValueError) as e:
            raise ExprError(f"计算失败：{e}") from e
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ExprError(f"不支持的一元运算符：{type(node.op).__name__}")
        return _norm(op(_num(_eval(node.operand, lookup))))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ExprError("不认识的函数调用（只许 abs/min/max/round/int/float/len）")
        if node.keywords:
            raise ExprError("函数调用不支持关键字参数")
        args = [_eval(a, lookup) for a in node.args]
        try:
            return _FUNCS[node.func.id](*args)
        except ExprError:
            raise
        except Exception as e:
            raise ExprError(f"函数 {node.func.id} 调用失败：{e}") from e
    # 其余一切（Attribute / List / Set / Compare / Subscript / 推导式…）都拒
    raise ExprError(f"表达式含不允许的语法：{type(node).__name__}")


def eval_expr(text, lookup):
    """安全求值一段算术表达式。

    text   —— 表达式文本（str；None 视为空串 → ExprError）
    lookup —— name -> 值 的解析函数（流程变量名映射；None = 一律取不到）
    返回   —— 求值结果（int/float，若整表达式是裸变量名则原样返回其值）
    失败   —— 一切异常统一抛 ExprError，绝不执行白名单外的任何东西。
    """
    if lookup is None:
        lookup = lambda name: None          # noqa: E731（就地兜底，无副作用）
    text = "" if text is None else str(text)
    # 花括号写法先归一到变量名直写（值由 lookup 现取；与 _fmt_text 同口径）
    text = _BRACE_RE.sub(lambda m: m.group(1), text)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"表达式语法错误：{e.msg}") from e
    except Exception as e:                   # 极端输入（超长/编码怪）也归一
        raise ExprError(f"表达式无法解析：{e}") from e
    return _eval(tree, lookup)


if __name__ == "__main__":
    def _lookup(name):
        return {"体力": 120, "x": 21}.get(name)

    _ok = True

    def _chk(label, got, want):
        global _ok
        good = got == want and type(got) is type(want)
        print(f"  [{'OK ' if good else 'FAIL'}] {label}: {got!r}"
              + ("" if good else f" (要 {want!r})"))
        _ok = _ok and good

    def _chk_err(label, text, lookup=None):
        global _ok
        try:
            r = eval_expr(text, lookup)
            print(f"  [FAIL] {label}: 没抛错，返回 {r!r}")
            _ok = False
        except ExprError as e:
            print(f"  [OK ] {label}: ExprError({e})")
        except Exception as e:               # 非 ExprError 一律算失败（泄漏）
            print(f"  [FAIL] {label}: 抛了非 ExprError {type(e).__name__}({e})")
            _ok = False

    print("— 正常用例 —")
    _chk("10-5", eval_expr("10-5", None), 5)             # 整数结果保持 int
    _chk("10/2 归一为 int", eval_expr("10/2", None), 5)
    _chk("10/4 保 float", eval_expr("10/4", None), 2.5)
    _chk("变量直写 体力-50", eval_expr("体力 - 50", _lookup), 70)
    _chk("花括号 {x}*2", eval_expr("{x}*2", _lookup), 42)
    _chk("abs(-3)", eval_expr("abs(-3)", None), 3)
    _chk("max(1,5,2)", eval_expr("max(1,5,2)", None), 5)
    _chk("min(1,5,2)", eval_expr("min(1,5,2)", None), 1)
    _chk("round(2.567,1)", eval_expr("round(2.567,1)", None), 2.6)
    _chk("int(3.9)", eval_expr("int(3.9)", None), 3)
    _chk("float(3)", eval_expr("float(3)", None), 3.0)
    _chk("len('abcd')", eval_expr("len('abcd')", None), 4)
    _chk("7 % 3", eval_expr("7 % 3", None), 1)
    _chk("2 ** 10", eval_expr("2 ** 10", None), 1024)
    _chk("-5 + 8", eval_expr("-5 + 8", None), 3)

    print("— 失败用例（必须抛 ExprError）—")
    _chk_err("除零", "1/0")
    _chk_err("取模零", "1%0")
    _chk_err("__import__", "__import__('os')")
    _chk_err("语法错", "1+")
    _chk_err("字符串常量减法", "'abc' - 1")
    _chk_err("变量是字符串参与减法", "s - 1", lambda n: "abc")
    _chk_err("属性访问", "(1).__class__")
    _chk_err("列表字面量", "[1,2][0]")
    _chk_err("比较运算", "1 < 2")
    _chk_err("不认识的函数", "eval('1')")
    _chk_err("关键字参数", "round(1.5, ndigits=1)")
    _chk_err("幂次过大", "2 ** 9999")
    _chk_err("空表达式", "")

    print("-" * 46)
    print("bot.expr 自测 " + ("ALL OK" if _ok else "FAILED"))
    raise SystemExit(0 if _ok else 1)