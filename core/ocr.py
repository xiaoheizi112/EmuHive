"""
core/ocr.py — 文字识别（RapidOCR，选区/选服等按文字找目标的能力）

重型可选依赖：没装 rapidocr/onnxruntime 时
available() 返回 False、find_text() 返回 None，绝不抛异常。引擎惰性单例，
首次调用才初始化（模型加载 ~0.4s，在调用方线程里跑）。注意：首帧会有一次
秒级初始化（依赖导入 + 模型加载冷启动叠加），建议启动时在后台线程预热
一次 available()+_get_engine()，让 worker 首 tick 不吃这笔加载时间。

⚠ 同步推理，单次全屏 ~0.5s（实测 540×960）。目前只在「选区点击」节点
用 —— 登录流程一次性操作，worker 慢一拍无害。若将来要在高频循环里用，升级
路径 = 独立识别线程 + 提交/轮询（见计划文件「核心机制」一节）。

命令行验收：
    python -m core.ocr <图片路径> [目标文字...]
    不带目标文字 = 打印全部识别结果；带 = 打印每个目标的匹配结果与坐标。
"""

import sys
import threading
import time

import numpy as np

_engine = None
# 引擎初始化锁：多个 worker 线程可能同时首次调 OCR，无锁会让 RapidOCR
# 被并发加载多份（模型几百 MB、内存翻倍且 onnxruntime 会话初始化非线程安全）。
_engine_lock = threading.Lock()

_available_cache: bool | None = None   # available() 结果缓存（进程内装没装不会变）

# 引擎异常"首次必报 + 降频"闸：find_text/list_text 对上层承诺绝不抛，
# 但全静默会让"OCR 引擎崩了"伪装成"文字就是认不出"，把人引向调阈值改截图
# 的歧途（排查黑洞，审查实锤）。折中：异常照旧吞成 None/[]，但第一次和
# 此后每 60 秒各喊一嗓子 stderr（带异常类型），既不刷屏又留了案底。
_err_state = {"last": 0.0, "count": 0}


def _note_engine_error(where: str, exc: BaseException) -> None:
    import time as _t
    _err_state["count"] += 1
    now = _t.monotonic()
    if _err_state["count"] == 1 or now - _err_state["last"] >= 60.0:
        _err_state["last"] = now
        print(f"[OCR警告] {where} 引擎异常（第 {_err_state['count']} 次，此后每 60 秒至多报一条）："
              f"{type(exc).__name__}: {exc}", file=sys.stderr)


def available() -> bool:
    """OCR 依赖是否装好（仅探测 import，不加载模型）。
    find_spec 要遍历 sys.path 探磁盘，而"装没装"进程内不会变——text 条件
    每 tick 都进来问一次，不缓存就是每拍白烧两遍路径扫描。结果缓存一次。"""
    global _available_cache
    if _available_cache is None:
        import importlib.util
        _available_cache = (importlib.util.find_spec("rapidocr") is not None
                            and importlib.util.find_spec("onnxruntime") is not None)
    return _available_cache


def _get_engine():
    """惰性单例（双重检查锁）：高频路径先无锁快读，只有确认未建才进锁；
    进锁后二次确认，避免多线程同时首调各加载一份模型。失败不缓存——
    RapidOCR() 抛异常时全局保持 None，下次调用还会重试（异常照旧向上抛，
    由调用方 try/except 兜住，本模块对外绝不抛的纪律不变）。"""
    global _engine
    if _engine is not None:        # 快路径：引擎已就绪，不加锁直接返回
        return _engine
    with _engine_lock:
        if _engine is not None:    # 二次确认：等锁期间可能已被别人建好
            return _engine
        from rapidocr import RapidOCR
        engine = RapidOCR()        # 先在局部建好再一次性发布，杜绝半成品可见
        _engine = engine
    return _engine


def match_score(text: str, needle: str) -> float:
    """
    识别文本 text vs 目标文字 needle 的匹配度 0~1。

    三条规则（旧版"双向包含即满分"有两个致命洞，真机实测都踩中过）：
    1. 数字锚点：needle 里每个数字 text 必须都有，缺一个直接判 0。形近汉字
       （权/杖、含/舍）OCR 认错可容忍，6服/9服 点错行不可容忍 —— 数字较真。
    2. 单向包含：只认"目标整串落在识别行里"（needle in text）给满分 —— 这覆盖
       单字 needle（"定"⊂"确定按"）。旧版反过来"text in needle 也满分"，于是
       标题"区服选择"被 OCR 拆出的单字"服"冒充整行"含沙射影6服"拿 1.0，四个目标
       全点到标题行（实测 y=276）。碎片必须靠相似度垫底淘汰。
    3. 有序相似度：difflib 兜底，"按定确"这类乱序不误报。
    """
    if not text or not needle:
        return 0.0
    for d in set(needle):
        if d.isdigit() and text.count(d) < needle.count(d):
            return 0.0
    if needle in text:
        return 1.0
    import difflib
    return difflib.SequenceMatcher(None, text, needle).ratio()


# 检测模型（PP-OCR det）吃不下太小的一条：真机实测 141×26 的窄框 → 检出 0 行
# （不是认字错，是第一步"没看见字"）；同内容上下各补 8px 立刻认出。留足余量。
_MIN_DET = 64


def _expand(lo: int, hi: int, need: int, limit: int) -> tuple:
    """区间不足 need 时对称向两侧补到 need，再钳进 [0, limit]（补边后仍尽量满 need）。"""
    if hi - lo >= need:
        return lo, hi
    lo -= (need - (hi - lo)) // 2
    hi = lo + need
    if lo < 0:
        lo, hi = 0, need
    if hi > limit:
        hi, lo = limit, max(0, limit - need)
    return lo, hi


def _crop(img: np.ndarray, roi: tuple | None):
    """按 ROI 裁基准帧，返回 (crop, ox, oy)；roi 缺 = 整帧（ox=oy=0）。
    见方不足 _MIN_DET 的框自动补边到可检测尺寸（钳进画面内）——用户框太小
    不再"静默看不见"。补边不伤精确命中：返回坐标仍换回基准帧绝对值，且
    match_score 的数字锚点照样挡住被扩出来的邻行（6服≠9服）。返回 None = 空框。"""
    H, W = img.shape[:2]
    if not roi:
        return img, 0, 0
    x0, y0, x1, y1 = (int(v) for v in roi)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x1 <= x0 or y1 <= y0:
        return None, 0, 0
    x0, x1 = _expand(x0, x1, min(_MIN_DET, W), W)
    y0, y1 = _expand(y0, y1, min(_MIN_DET, H), H)
    return img[y0:y1, x0:x1], x0, y0


def find_text(img: np.ndarray, needle: str, roi: tuple | None = None,
              min_score: float = 0.5, min_sim: float = 0.6,
              log_fn=None) -> dict | None:
    """
    识别画面，找出与 needle 最像的一行文字。

    返回 {"x", "y", "x0", "y0", "x1", "y1", "text", "sim", "ms"}，
    x/y = 该文字框中心在基准帧(540×960)里的坐标 —— 与模板匹配同一坐标空间，
    可直接 input tap；x0y0/x1y1 = 文字框外接矩形（编辑器画绿框用）。
    没有达到 min_sim 的结果 / 依赖没装 / 识别失败 → None，绝不抛异常。

    min_score：滤掉 OCR 自身没把握的行（乱码/半透装饰字）。
    min_sim：与目标文字的最低相似度（游戏字描边常把字认花）。
    roi 太小检不出时会自动补边到可检测尺寸（见 _crop）。
    """
    if img is None or img.size == 0 or not needle or not available():
        return None
    crop, ox, oy = _crop(img, roi)
    if crop is None:
        return None
    try:
        t0 = time.perf_counter()
        res = _get_engine()(crop)
        ms = (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        _note_engine_error("find_text", e)
        return None
    if res is None or getattr(res, "txts", None) is None:
        return None
    best = None
    for text, box, score in zip(res.txts, res.boxes, res.scores):
        if float(score) < min_score:
            continue
        sim = match_score(str(text), needle)
        if best is None or sim > best[0]:
            best = (sim, str(text), box)
    if best is None or best[0] < min_sim:
        # 未命中不再是黑盒（可排查性审计 ④）：把检出的候选行报出去，
        # "它到底看到了什么"从此可答。log_fn 由调用方注入（core 层无 ctx.log）。
        if log_fn is not None:
            try:
                cands = "; ".join(str(t) for t in list(res.txts)[:6]) or "什么都没检出"
                log_fn(f"识字「{needle}」未命中（阈值 {min_sim:.2f}，"
                       f"最像 {best[0]:.2f}），检出候选：{cands}")
            except Exception:
                pass
        return None
    sim, text, box = best
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return {"x": ox + int((min(xs) + max(xs)) / 2),
            "y": oy + int((min(ys) + max(ys)) / 2),
            "x0": ox + int(min(xs)), "y0": oy + int(min(ys)),
            "x1": ox + int(max(xs)), "y1": oy + int(max(ys)),
            "text": text, "sim": round(sim, 3),
            "ms": round(ms, 1)}


def list_text(img: np.ndarray, roi: tuple | None = None,
              min_score: float = 0.5) -> list[dict]:
    """全量识别（调试/命令行验收用）：[{text, x0, y0, x1, y1, score}, ...]。
    ⚠ 同样吃 _crop 的自动补边：读数字用法（roi_number）因此可能把框外 64px 内的
    邻行数字也算进来 —— 偏保守（多一个候选更容易判"歧义当没认出"），
    不会读错值，比"框太小 → 0 行 → 永远读不到"诚实。"""
    if img is None or img.size == 0 or not available():
        return []
    crop, ox, oy = _crop(img, roi)
    if crop is None:
        return []
    try:
        res = _get_engine()(crop)
    except Exception as e:
        _note_engine_error("list_text", e)
        return []
    if res is None or getattr(res, "txts", None) is None:
        return []
    out = []
    for text, box, score in zip(res.txts, res.boxes, res.scores):
        if float(score) < min_score:
            continue
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
        out.append({"text": str(text), "x0": ox + min(xs), "y0": oy + min(ys),
                    "x1": ox + max(xs), "y1": oy + max(ys),
                    "score": round(float(score), 2)})
    return out


if __name__ == "__main__":
    # 几何自检（纯 numpy，不加载 OCR 模型）：小框补边逻辑改坏必在这里炸
    _img = np.zeros((960, 540, 3), np.uint8)
    _c, _ox, _oy = _crop(_img, (201, 701, 342, 727))   # 真机踩坑的 141×26 窄条
    assert _c.shape[1] >= _MIN_DET and _c.shape[0] >= _MIN_DET, "窄条未补到可检测"
    assert _ox <= 201 and _oy <= 701 and _ox + _c.shape[1] >= 342 and _oy + _c.shape[0] >= 727, "补边没包住原框"
    _c2, o2, p2 = _crop(_img, (0, 0, 30, 30))           # 贴角小框：钳进画面
    assert o2 == 0 and p2 == 0 and _c2.shape[:2] == (_MIN_DET, _MIN_DET)
    _c3, _, _ = _crop(_img, (100, 80, 400, 700))        # 够大的框：一字不改
    assert _c3.shape[:2] == (620, 300), "大框被误扩"
    assert _crop(_img, (300, 300, 100, 100))[0] is None  # 倒框/空框仍判无效
    assert _crop(_img, None)[0] is _img                  # 全屏语义不变
    print("core.ocr 补边自检 OK")
    # 命令行验收：python -m core.ocr <图片> [目标文字...]
    import sys
    from core import capture
    if len(sys.argv) < 2:
        print("用法：python -m core.ocr <图片路径> [目标文字...]")
        sys.exit(1)
    img = capture.load_image_cn(sys.argv[1])
    if img is None:
        print(f"读不到图片：{sys.argv[1]}")
        sys.exit(1)
    print(f"依赖就绪：{available()}（图 {img.shape[1]}×{img.shape[0]}）")
    for row in list_text(img):
        print(f" {row['text']!r:24} score={row['score']:.2f} "
              f"框=({row['x0']},{row['y0']})-({row['x1']},{row['y1']})")
    for needle in sys.argv[2:]:
        hit = find_text(img, needle)
        print(f"目标 {needle!r} → {hit}")
