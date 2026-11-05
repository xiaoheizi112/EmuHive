"""tools/bench_capacity.py — 多开容量压测（阶段1 · 2026-10-09）

回答一个一直没测过的问题：**这台机器到底能稳定挂几台**。挂机节拍 =
general.frame_interval（默认 0.5s）：每拍要完成"取帧→灰度→找图"，N 台并发时
谁超了预算谁掉拍（日志上看不见，只是越来越迟钝）。本工具按 N 递增并发跑
这条链路，输出每档的 均值/p95/超拍率，给出建议最大负载。

用法：
  python tools/bench_capacity.py --demo                 # 合成帧源自检链路：不出结论，只验工具本身
  python tools/bench_capacity.py --indexes 0-11         # 实测：对已启动的这批实例压测
  python tools/bench_capacity.py --indexes 0,2,5-9 [--rounds 20] [--budget 0.5] [--match-count 30] [--json out.json]

实测前提（工具不负责，缺了会明确报错而不是测出假结论）：
  · 实例已启动、画面正常 —— launch 是磁盘/CPU 重活，混进压测算的是开机不是识别
  · 雷电同步器已关；本机没在跑 GUI 挂机（会互相抢 CPU，结论失真）
  · budget 不传则读 config general.frame_interval（与挂机真实节拍同口径）

把结论（"本机实测稳定 N 台，p95=x.xxs"）记进 README「容量」一行；
以后每加一个功能，先问会不会把 N 打下来。
"""
import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def parse_indexes(spec: str) -> list[int]:
    """'0,2,5-9' → [0,2,5,6,7,8,9]；纯函数供冒烟直测。"""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    if not out or out != sorted(out) or len(set(out)) != len(out):
        raise SystemExit(f"实例号解析异常（应为升序不重叠）：{spec} → {out}")
    return out


def tick_stats(durations: list[float], budget: float) -> dict:
    """一轮压采的分布统计：mean/p95/max/超拍率。durations 单位秒。"""
    if not durations:
        return {"n": 0, "mean": 0.0, "p95": 0.0, "max": 0.0, "over_ratio": 0.0}
    s = sorted(durations)
    n = len(s)
    # 最近秩法（nearest-rank）：ceil(0.95n) 那个序位。别用 round —— 银行家舍入
    # 在 n=10 时 round(9.5)=10（取偶）会把 p95 抬成最大值
    p95 = s[min(n - 1, math.ceil(0.95 * n) - 1)]
    return {"n": n, "mean": round(sum(s) / n, 4), "p95": round(p95, 4),
            "max": round(s[-1], 4),
            "over_ratio": round(sum(1 for d in s if d > budget) / n, 4)}


def recommend(per_n: dict[int, dict], budget: float) -> int | None:
    """建议最大负载 = 满足「超拍率 ≤5% 且 p95 ≤ 1.5×budget」的最大 N；全不过 → None。"""
    good = [n for n, st in sorted(per_n.items())
            if st["stats"]["over_ratio"] <= 0.05 and st["stats"]["p95"] <= budget * 1.5]
    return max(good) if good else None


def capacity_steps(indexes: list[int]) -> list[list[int]]:
    """实测档位切法：按 4/8/12/16/20/24/32 递增取前缀（不足就取满）。

    这是 CLI 与界面共用的口径——切档逻辑只写这一处，免得两处漂移（界面若
    自己再来一套"每两台上加一档"，同一批实例两处测出不同结论）。"""
    sizes = sorted({min(len(indexes), k) for k in (4, 8, 12, 16, 20, 24, 32)})
    return [indexes[:n] for n in (sizes or [len(indexes)])]


def demo_steps() -> list[list[str]]:
    """demo 档位：合成帧源（4/8/16 档），只为验统计/报告链路，不碰模拟器。"""
    return [[f"demo-{i}" for i in range(n)] for n in (4, 8, 16)]


def demo_round_fn():
    """demo 的 round 适配器：把 'demo-3' 这类键解出实例号交给 make_demo_round。"""
    runner = make_demo_round()
    return lambda key: runner(int(str(key).split("-")[1]))


def bench_n(indexes: list[int], round_fn, rounds: int, budget: float,
            cancelled=None) -> dict:
    """对一档 N 跑 rounds 轮：每轮并发执行 round_fn(index)->耗时秒，收全量耗时。
    任何一轮里某实例抛异常（如取帧失败）直接上抛——压测不许静默吞故障。

    cancelled 为可选回调：每轮开跑前问一次，真则立即收手（界面「取消压测」用；
    QThread 里跑几十轮，没有这个钩子取消会没反应）。取消纯函数语义未变——
    不传 cancelled 时行为与旧版逐字一致。"""
    durations, walls = [], []
    with ThreadPoolExecutor(max_workers=len(indexes)) as pool:
        for _ in range(rounds):
            if cancelled is not None and cancelled():
                break
            t0 = time.perf_counter()
            futs = [pool.submit(round_fn, i) for i in indexes]
            ds = [f.result() for f in futs]
            walls.append(time.perf_counter() - t0)
            durations.extend(ds)
    st = tick_stats(durations, budget)
    st["wall_mean"] = round(sum(walls) / len(walls), 4) if walls else 0.0
    return {"N": len(indexes), "stats": st}


def run_bench(steps, round_fn, rounds: int, budget: float,
              on_result=None, cancelled=None) -> dict:
    """跑表驱动压测：对 steps 里每一档跑 rounds 轮，边跑边回调、边问取消。

    CLI 与界面共用这一支：档位怎么切、逐档怎么统计、何时问取消，只此一处
    （界面里再手写一遍循环 = 两份必漂移）。
    - steps：如 [[0,1,2,3], [0..7], ...]；重复 N 自动跳过。
    - round_fn(index)->耗时秒；某实例取帧失败会 RuntimeError 上抛，由调用方决定中止方式。
    - on_result(n, res)（可选）：每跑完一档回调一次，界面据此实时填表。
    - cancelled（可选）：每档/每轮开跑前问，真则收手，已跑档的结果照样带回。
    返回 {"per_n": {N: {"N", "indexes", "stats"}}, "cancelled": bool}。
    """
    per_n: dict[int, dict] = {}
    was_cancelled = False
    for grp in steps:
        n = len(grp)
        if n in per_n:
            continue
        if cancelled is not None and cancelled():
            was_cancelled = True
            break
        res = bench_n(grp, round_fn, rounds, budget, cancelled=cancelled)
        per_n[n] = {"indexes": list(grp), **res}
        if cancelled is not None and cancelled():
            was_cancelled = True
            if on_result is not None:
                on_result(n, per_n[n])
            break
        if on_result is not None:
            on_result(n, per_n[n])
    return {"per_n": per_n, "cancelled": was_cancelled}


def make_demo_round():
    """合成帧源：按实例号假负载（0.5~1.2 倍 8ms 级），只为验证统计/报告链路。"""
    def run(index: int) -> float:
        t0 = time.perf_counter()
        time.sleep(0.004 * (1 + (index % 7) * 0.12))
        return time.perf_counter() - t0
    return run


def make_live_round(match_count: int, frame_getter=None, tpl_loader=None):
    """实测帧源：与挂机同链路——取帧（vision.get_screen：adb 单次快照）→ 灰度 → 逐张找图。
    只测识别负载，不点击（点击走 adb 往返、不是 CPU 瓶颈项）。
    参数可注入（冒烟打桩用），默认现拉库内前 match_count 张模板。"""
    import cv2
    from core import vision
    if frame_getter is None:
        frame_getter = vision.get_screen
    if tpl_loader is None:
        from core import template_manager as tm
        entries = tm.list_templates()[:match_count]
        tpls = [t for t in (tm.load_template_image(e) for e in entries) if t is not None]
        if not tpls:
            raise SystemExit("模板库为空/读不出——压测的『找图』环节没有意义，先用 --demo 验链路")
    else:
        tpls = tpl_loader()

    def run(index: int) -> float:
        t0 = time.perf_counter()
        got = frame_getter(index)
        if got is None:
            raise RuntimeError(f"实例 {index} 取帧失败：确认它已启动且画面正常")
        frame, _ = got
        cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        for tpl in tpls:
            vision.match_template(frame, tpl, threshold=0.85)
        return time.perf_counter() - t0
    return run


def main():
    ap = argparse.ArgumentParser(description="多开容量压测：找挂机节拍超拍的最大 N")
    ap.add_argument("--demo", action="store_true", help="合成帧源验链路（不碰模拟器，不出容量结论）")
    ap.add_argument("--indexes", default="", help="实测实例号：'0-11' 或 '0,2,5-9'")
    ap.add_argument("--rounds", type=int, default=20, help="每 N 档压几轮（默认 20）")
    ap.add_argument("--budget", type=float, default=None, help="每拍预算秒；缺省读 config general.frame_interval")
    ap.add_argument("--match-count", type=int, default=30, help="每拍逐张找图的模板张数上限（默认 30）")
    ap.add_argument("--json", default="", help="结果写 json（给 README/以后对比用）")
    args = ap.parse_args()

    budget = args.budget
    if budget is None:
        from core.config_manager import get_config
        budget = float(get_config().get_safe("general.frame_interval", 0.5))
    print(f"预算（每拍节拍）：{budget}s · 每档 {args.rounds} 轮")

    if args.demo:
        steps = demo_steps()
        round_fn = demo_round_fn()
    else:
        if not args.indexes:
            raise SystemExit("实测要 --indexes（如 0-11），或加 --demo 验链路")
        steps = capacity_steps(parse_indexes(args.indexes))
        round_fn = make_live_round(args.match_count)

    def _report(n, res):
        st = res["stats"]
        flag = "⚠ 超拍" if st["over_ratio"] > 0.05 or st["p95"] > budget * 1.5 else "ok"
        print(f"N={n:<3} 单拍均值={st['mean']}s p95={st['p95']}s max={st['max']}s "
              f"超拍率={st['over_ratio']:.0%} 轮墙钟均={st['wall_mean']}s  {flag}")

    try:
        out = run_bench(steps, round_fn, args.rounds, budget, on_result=_report)
    except RuntimeError as e:
        print(f"中止：{e}")
        return 2
    per_n = out["per_n"]
    rec = recommend(per_n, budget)
    print(f"建议最大负载：{rec if rec else '全部档位都超拍（检查模拟器状态/别抢跑）'}"
          f"（判据：超拍率≤5% 且 p95≤{budget * 1.5:.2f}s）")
    if args.demo:
        print("（--demo 是合成负载，上行的\"负载\"数字只验链路，不是容量结论）")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"budget": budget, "rounds": args.rounds, "demo": args.demo, "recommend": rec,
             "per_n": {str(n): v for n, v in per_n.items()}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果 → {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
