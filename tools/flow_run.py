"""tools/flow_run.py — 无头试跑一条已入库流程（AI 做流程流水线 ④）

bot/flow_test.py 的同步版：同一条执行链（截帧 → compile_graph → FSM.tick），
去掉 QThread/信号，日志直接 print，跑完拿退出码。AI 迭代闭环就靠它：
改流程 → flow_apply 写入 → flow_run 试跑 → 看日志/失败截图 → 再改。
失败截图留证在 logs/fail_capture（引擎自动存），拿来看现场。

用法：
  python tools/flow_run.py <流程名> [--index 0] [--max-seconds 180]
退出码：0=跑完/主动结束  2=失败  3=超时截停/环境不对
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def run(flow_name, index=0, max_seconds=180.0):
    from bot.context import BotContext
    from bot.frame_loop import clamp_frame_interval
    from bot.frame_source import FrameSource
    from bot.fsm import FSM, FlowTimeoutExit, SENTINEL_KINDS  # noqa: F401
    from bot.tasks.flow import compile_graph
    from core import ldplayer
    from core.config_manager import get_config

    cfg = get_config()
    graph = (cfg.get("custom_flows", {}) or {}).get(flow_name)
    if graph is None:
        print(f"✗ 配置里没有流程「{flow_name}」（先 flow_apply 写入）")
        return 3
    inst = ldplayer.get_instance(index)
    if inst is None or not inst.running:
        print(f"✗ 实例 {index} 没开着（先启动雷电）")
        return 3

    ctx = BotContext(ld_index=index)
    ctx.hwnd = inst.top_hwnd
    log = lambda lvl, msg: print(f"[{lvl}] {msg}")
    ctx.log = lambda msg, lvl="INFO": log(lvl, msg)
    # 取帧+校准三份收口（bot/frame_source，同 worker/flow_test）：这里此前手搓
    # 一份且已漂移——快照抽风但句柄活时 i2=None 直接崩、启动校准吃 calibrate
    # 全量默认预算（坏机器最坏堵近 3 分钟）、换句柄只跟不重校准。现在：启动
    # 校准走有界预算（launch_calibrate_timeout），重校准带 5 秒冷却，
    # adb 兜底一次性 WARN 照播。
    fs = FrameSource(index, ctx, log)
    if not fs.startup_calibrate():
        print("[WARN] 校准失败，整窗缩放回退（坐标可能微偏）")
    flow_map = dict(cfg.get("custom_flows", {}) or {})
    steps = compile_graph(graph, flow_map, flow_name=flow_name)
    print(f"开始试跑「{flow_name}」：{len(graph.get('nodes') or [])} 节点 → {len(steps)} 步骤")
    fsm = FSM(steps, ctx, task_name="flow_run", quiet=True)
    interval = clamp_frame_interval(cfg.get("general.frame_interval", 0.5))
    deadline = time.monotonic() + max_seconds
    last = ""
    while time.monotonic() < deadline:
        if fs.grab() is None and not fs.ensure_alive():
            # 快照抽风不误杀：句柄还活着就等下一拍（判活语义见 core.ldplayer）
            print("✗ 模拟器中途关了")
            return 3
        res = fsm.tick()
        cur = fsm.steps[fsm.idx].name if fsm.idx < len(fsm.steps) else "完成"
        if cur != last:
            last = cur
            print(f"→ 步骤：{cur}")
        if res == "done" or res in ("abort", "flow_end", "loop_continue"):
            print("✓ 跑完")
            return 0
        if res == "failed":
            print("✗ 失败（现场截图见 logs/fail_capture）")
            return 2
        time.sleep(interval)
    print(f"✗ 超时截停（{max_seconds}s），停在：{last}")
    return 3


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("flow_name")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--max-seconds", type=float, default=180.0)
    a = ap.parse_args()
    sys.exit(run(a.flow_name, a.index, a.max_seconds))
