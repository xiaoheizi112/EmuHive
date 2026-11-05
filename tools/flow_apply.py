"""tools/flow_apply.py — 校验并写入流程 JSON（AI 做流程流水线 ③）

AI 按 docs/flow-authoring-guide.md 的节点规范产出一个流程 json 文件，
本工具先过编译关（bot.tasks.flow.compile_graph，图不合法当场拒、不落盘），
再写进 config 的 custom_flows——同名流程默认拒写（--force 才覆盖，红线）。
节点结构、端口语义都在指南和 `python -m bot.tasks.flow` 自检里，这里不重复。

用法：
  python tools/flow_apply.py <流程.json> --name <流程名> [--force]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def apply_flow(json_path, name, force=False):
    """编译校验 + 写入 config。返回 (节点数, 步骤数)。"""
    from core.config_manager import get_config
    graph = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise SystemExit("流程 json 必须是 {\"version\":2,\"nodes\":[...],\"edges\":[...]}")
    cfg = get_config()
    flows = dict(cfg.get("custom_flows", {}) or {})
    if name in flows and not force:
        raise SystemExit(f"红线：已存在流程「{name}」，覆盖需 --force（且经用户确认）")
    # 编译前置校验：把这条流程放进 flow_map（子流程按存量找），编译炸 = 图不合法
    from bot.tasks.flow import compile_graph
    flow_map = dict(flows)
    flow_map[name] = graph
    try:
        steps = compile_graph(graph, flow_map, flow_name=name)
    except Exception as e:
        raise SystemExit(f"编译不过，拒写：{e}")
    flows[name] = graph
    cfg.set("custom_flows", flows)
    cfg.save()
    print(f"✓ 流程「{name}」已写入：{len(graph['nodes'])} 节点 → {len(steps)} 步骤")
    try:   # 引用播报（这条流程用了哪些库），扫不动不拦路
        from bot.template_deps import flow_ref_summary
        libs = sorted(flow_ref_summary().get(name, set()))
        print(f"  引用库：{'、'.join(libs) if libs else '（无模板引用）'}")
    except Exception:
        pass
    return len(graph["nodes"]), len(steps)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--name", required=True)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    apply_flow(a.json_path, a.name, a.force)
