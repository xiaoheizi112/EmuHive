# smoke_flow_subgraph_test.py — 「▶ 从此节点开始测」下游裁剪回归
#
# 覆盖：
# - bot.tasks.flow.subgraph_from：裁子图纯函数（不碰画布原图、保分支/失败口/成环）
# - 编译层透传：subgraph 丢给 compile_graph 能编译，GraphStep 只含子图节点
# - UI 链路：按钮接线、选中态 gating、test_from_requested 信号
# 离屏；不碰模拟器、不写真 config。

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.tasks.flow import subgraph_from, compile_graph, ensure_graph


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def main():
    print("smoke_flow_subgraph_test")

    # ── 纯函数：子图裁剪 ──
    g = {
        "version": 2,
        "nodes": [
            {"id": "a", "type": "click", "label": "A"},
            {"id": "b", "type": "click", "label": "B"},
            {"id": "c", "type": "click", "label": "C"},
            {"id": "d", "type": "click", "label": "D"},
        ],
        "edges": [
            {"from": "a", "port": "out", "to": "b"},
            {"from": "b", "port": "out", "to": "c"},
            {"from": "c", "port": "out", "to": "d"},
        ],
    }
    sub = subgraph_from(g, "b")
    ids = {n["id"] for n in sub["nodes"]}
    check(ids == {"b", "c", "d"}, "下游可达：a 被裁掉")
    check(len(sub["edges"]) == 2, "边只留子图内部连线")
    check(sub["nodes"] is not g["nodes"], "不碰原图（返回新 dict）")

    # 分支口保留
    g2 = {
        "version": 2,
        "nodes": [
            {"id": "s", "type": "branch", "label": "S"},
            {"id": "t", "type": "click", "label": "T"},
            {"id": "f", "type": "click", "label": "F"},
        ],
        "edges": [
            {"from": "s", "port": "true", "to": "t"},
            {"from": "s", "port": "false", "to": "f"},
        ],
    }
    sub2 = subgraph_from(g2, "s")
    ports = {e["port"] for e in sub2["edges"]}
    check(ports == {"true", "false"}, "分支口 true/false 都留")

    # 失败口保留
    g3 = {
        "version": 2,
        "nodes": [
            {"id": "x", "type": "wait_template", "label": "X"},
            {"id": "y", "type": "click", "label": "Y"},
            {"id": "z", "type": "click", "label": "Z"},
        ],
        "edges": [
            {"from": "x", "port": "out", "to": "y"},
            {"from": "x", "port": "fail", "to": "z"},
        ],
    }
    sub3 = subgraph_from(g3, "x")
    ports3 = {e["port"] for e in sub3["edges"]}
    check(ports3 == {"out", "fail"}, "失败口 fail 保留")

    # 成环保留
    g4 = {
        "version": 2,
        "nodes": [
            {"id": "l1", "type": "click", "label": "L1"},
            {"id": "l2", "type": "click", "label": "L2"},
        ],
        "edges": [
            {"from": "l1", "port": "out", "to": "l2"},
            {"from": "l2", "port": "out", "to": "l1"},
        ],
    }
    sub4 = subgraph_from(g4, "l1")
    check(len(sub4["edges"]) == 2, "成环边保留（循环语义不变）")

    # 起点不在图里：原样返回
    g5 = {"version": 2, "nodes": [{"id": "n", "type": "click"}], "edges": []}
    check(subgraph_from(g5, "ghost") is g5, "起点不存在原样返回")

    # ── 编译层：子图能编译、只含子图节点 ──
    compiled = compile_graph(sub, {}, flow_name="t")
    check(len(compiled) == 1, "compile_graph 出 1 个 GraphStep")
    step_nodes = set(compiled[0]._nodes.keys())
    check(step_nodes == {"b", "c", "d"}, "GraphStep 只装子图节点")

    # ── UI：按钮接线 + 选中态 gating ──
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    from ui.flow_editor import FlowPanel
    panel = FlowPanel.__new__(FlowPanel)
    # 借骨架：不跑 __init__，只塞按钮冒烟要的最小属性
    panel.view = type("V", (), {"isEnabled": lambda s: True})()
    panel._selected_item = lambda: None
    panel._solo_test_node = lambda: None
    panel.btn_test_one = type("B", (), {
        "setEnabled": lambda s, v: setattr(s, "_en", v),
        "_en": False,
    })()
    panel.btn_test_from = type("B", (), {
        "setEnabled": lambda s, v: setattr(s, "_en", v),
        "_en": False,
    })()
    panel._refresh_test_one_btn()
    check(panel.btn_test_from._en is False, "没选中时按钮灰")

    class FakeItem:
        node = {"id": "n1", "type": "click", "label": "N1"}
    panel._selected_item = lambda: FakeItem()
    panel._solo_test_node = lambda: FakeItem().node
    panel._refresh_test_one_btn()
    check(panel.btn_test_from._en is True, "选中节点时按钮亮")

    # 信号发射
    emitted = []
    panel.test_from_requested = type("S", (), {"emit": lambda s, n: emitted.append(n)})()
    panel._on_test_from = FlowPanel._on_test_from.__get__(panel, FlowPanel)
    panel._on_test_from()
    check(emitted and emitted[0]["id"] == "n1", "test_from_requested 信号发射")

    print("PASS")


if __name__ == "__main__":
    main()
