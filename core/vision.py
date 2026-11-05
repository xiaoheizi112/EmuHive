"""
core/vision.py — 图像识别（第 3 步最小版）

本步学到的知识点：
1. cv2.matchTemplate 原理：把小模板图放在大图上"滑动"，每个位置算一次
   相似度，结果是一张"分数矩阵"；minMaxLoc 找出最高分的位置和分值
2. 阈值：多像才算"找到"。0.85 是经验起点——按钮图标类可用 0.9，
   带文字/半透明的元素要放宽到 0.7-0.8（详见计划"阈值三档"）
3. 坐标：命中位置是模板左上角；点击中心 = 左上角 + 模板尺寸的一半

命令行用法：
    python -m core.vision cut <index> <x> <y> <w> <h> <模板.png>
        —— 从模拟器当前画面框选一块区域存为模板（采集模板）
    python -m core.vision match <index> <模板.png> [--threshold 0.85] [--click]
        —— 在当前画面找模板，打印分数与位置；--click 则直接点过去
    python -m core.vision health <index> [--frame 画面.png] [--lib 库名]
                              [--threshold 0.85] [--json out.json]
        —— 模板体检：整屏逐张找库内全部模板，列出"分数够不到自己阈值"的死图。
        游戏改版当天先跑它，几分钟知道哪些流程会瘸；--frame 可用之前 shot
        存的画面离线复检（不占模拟器）；--json 出机器可读报告（AI 流水线圈图用）。
"""

import sys
import threading
import time

import cv2
import numpy as np

from core import capture, json_store, ldplayer


def _cal_rgb_confidence(crop: np.ndarray, tpl: np.ndarray,
                        mask: np.ndarray | None = None) -> float:
    """
    Airtest 风格 RGB 颜色置信度：逐像素三通道归一化相关系数取均值。

    灰度匹配只看"明暗形状"，会丢失颜色信息——两个灰度分布相同但颜色
    不同的图（如红/蓝按钮）灰度相关系数同样很高。这里把命中位置的
    裁剪块与模板逐通道算皮尔逊相关系数，颜色不符会显著拉低分数。

    平坦通道兜底（实测踩坑）：纯色模板的某通道方差≈0，皮尔逊相关系数在
    数学上无定义（分母为 0）。此时该通道无"形状"可辨，退化成"只比均值颜色"：
    均值接近 = 颜色一致贡献 1.0，否则 0.0。否则平坦通道的 0 分会拖垮
    整张均值，把颜色一致的模板也误拒。
    尺寸不一致（极少见，模板已保证小于原图）返回 0。

    mask 非空（抠图模板）时只在保留像素上比色：被抠掉的背景像素不参与
    （它们的 RGB 往往仍是旧背景色，算进来会把分数拖低、产生假阴性）。
    tpl 可能是 4 通道 BGRA，逐通道只取前 3 通道，与 crop（3 通道）对齐。
    """
    if crop.shape[:2] != tpl.shape[:2]:
        return 0.0
    sel = None
    if mask is not None:
        # 遮罩归一：_match_core 传的是 0/1 float，外部也可能传 0/255，>0.5 两者通吃
        sel = mask > 0.5
        if not sel.any():
            return 0.0
    confs = []
    for c in range(3):  # BGR 三通道
        a = crop[:, :, c].astype(np.float32)
        b = tpl[:, :, c].astype(np.float32)
        if sel is None:
            a = a.ravel(); b = b.ravel()
        else:
            a = a[sel]; b = b[sel]
        mean_a, mean_b = float(a.mean()), float(b.mean())
        a = a - mean_a
        b = b - mean_b
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denom < 1e-6:
            # 平坦通道：无形状信息，只比平均颜色（±10 容忍）
            confs.append(1.0 if abs(mean_a - mean_b) < 10.0 else 0.0)
        else:
            confs.append(float(np.dot(a, b) / denom))
    return float(np.mean(confs))


# 遮罩匹配的「平坦窗口」标准差门槛（灰度空间）：掩码内每像素方差 < 它的平方
# 就判该窗口无纹理、相关系数无定义，结果置 -1。取 1.0 与普通分支
# _match_core 里「退化窗口掩码 var_aligned < 1.0」的同口径（都是灰度方差）。
_MASKED_FLAT_STD = 1.0


def _masked_ncc(search: np.ndarray, tpl: np.ndarray,
                mask: np.ndarray) -> np.ndarray | None:
    """
    带遮罩的归一化互相关（masked normalized cross-correlation）。

    抠图模板：只在 mask（0/1）标出的保留像素上比对，被抠掉的背景完全不算分。
    分数口径与 TM_CCOEFF_NORMED 一致（同是"减均值的归一化相关系数"，恒 -1~1、
    抗整体亮度偏移），所以用户在普通模板上调好的阈值迁到抠图模板仍然可比，
    不会出现"同一张图抠完分变了、阈值要重调"的割裂。

    OpenCV 自带 matchTemplate 的 mask 参数只支持 SQDIFF/CCORR_NORMED（不减均值、
    不抗亮度），与项目现有语义不符，故按官方教程公式手写。展开后每项都能化成
    一次 matchTemplate(TM_CCORR) 的滑窗量（见 OpenCV "Template Matching with a
    Custom Mask"）：
        num = Σ M·T·I − (Σ M·T)(Σ M·I)/ΣM
        den = √( [Σ M·T² − (Σ M·T)²/ΣM] · [Σ M·I² − (Σ M·I)²/ΣM] )
    退化防护：图像窗口在遮罩内是纯色（var_I≈0，相关无定义）的位置置 -1，
    等价普通分支的"退化窗口掩码"，直接算到窗口级、更准。
    search/tpl/mask 均为 float32；mask 值 0/1。无解（遮罩内模板纯色）返回 None。

    ⚠ 平坦判据必须按「每像素方差」比，不能拿 denom 定绝对下限（实测修复
    2026-10-11）：var_t/var_i 均为「掩码内方差 × 像素数」，量纲是 n·像素方差，
    不是像素方差本身。纯色/低纹理窗口下 float32 抵消残差会让 var_i 掉到 1e-13
    级却仍 >0，而 denom=√(var_t·var_i) 在 var_t 很大时照样越过 1e-6 这道绝对
    门槛 → 除出来是 >1 的荒唐分（归一化相关理论上限 1.0）。实测真机 540×960
    帧上「图标_箭头朝下」算出 1.93 且直接误点、合成帧算出 49.07。判据改成
    var_i ≥ n·flat_std²（每像素方差 < flat_std² 判平坦），与普通分支
    "窗口方差 < 1.0" 的口径对齐。
    """
    n = float(mask.sum())
    if n < 1:
        return None
    m_t = mask * tpl                       # M·T
    sum_mt = float(m_t.sum())
    var_t = float((mask * tpl * tpl).sum()) - sum_mt * sum_mt / n
    # 遮罩内模板纯色：无特征可定位。同样按每像素方差判（纯色模板的 var_t 在
    # float32 下可能是小正数甚至负数，绝对门槛 1e-6 拦不住）。
    if var_t < n * (_MASKED_FLAT_STD ** 2):
        return None
    c_it = cv2.matchTemplate(search, m_t, cv2.TM_CCORR)      # Σ M·T·I
    c_i = cv2.matchTemplate(search, mask, cv2.TM_CCORR)       # Σ M·I
    c_ii = cv2.matchTemplate(search * search, mask, cv2.TM_CCORR)  # Σ M·I²
    numerator = c_it - c_i * (sum_mt / n)
    var_i = np.maximum(c_ii - c_i * c_i / n, 0.0)
    denom = np.sqrt(var_t * var_i)
    res = np.full(denom.shape, -1.0, dtype=np.float32)
    good = var_i >= n * (_MASKED_FLAT_STD ** 2)   # 每像素方差 < flat_std² = 平坦窗
    np.divide(numerator, denom, out=res, where=good)
    return res


# ── 抠图原语（采集端「抠图」用；纯 cv2，无 Qt）─────────────────────────
# 抠图 = 只保留目标像素、把背景标成透明存进 PNG 的 alpha 通道；
# 识别时走上面的带遮罩匹配，背景怎么变都不拖分数。掩码约定一律 bool：True=保留。

def color_close_mask(img: np.ndarray, color, tolerance: int) -> np.ndarray:
    """与参考色欧氏距离 ≤ tolerance 的像素掩码（bool 阵）。抠图共用原语。"""
    diff = img.astype(np.int32) - np.asarray(color, dtype=np.int32)
    dist = np.sqrt((diff * diff).sum(axis=2))
    return dist <= float(tolerance)


def auto_matte(img: np.ndarray, border_px: int = 2,
               tolerance: int = 30) -> np.ndarray:
    """
    一键自动去背景：采样模板矩形【外圈 border_px 行/列】的主色当背景色，
    容差内全部抠掉。适合"图标浮在纯色底上"的大多数游戏 UI。
    返回 bool 掩码（True=保留）。框选时把背景也框进来了也没关系——
    边框一圈采样到的就是那块背景。
    """
    h, w = img.shape[:2]
    b = max(1, min(int(border_px), max(h // 2, 1), max(w // 2, 1)))
    ring = np.vstack([img[:b, :].reshape(-1, 3), img[-b:, :].reshape(-1, 3),
                      img[b:-b, :b].reshape(-1, 3), img[b:-b, -b:].reshape(-1, 3)])
    # 主色 = 量化到 16 级后出现最多的桶，再取桶内均值（比逐色计数快且够准）
    q = (ring // 16).astype(np.int32)
    keys = q[:, 0] * 4096 + q[:, 1] * 64 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    dom = int(vals[int(counts.argmax())])
    color = ring[keys == dom].mean(axis=0)
    return ~color_close_mask(img, color, tolerance)


def magic_matte(img: np.ndarray, seed_xy: tuple[int, int],
                tolerance: int = 30) -> np.ndarray:
    """
    点色魔棒：取 seed_xy 处像素色为参考，抠掉全图容差内的相似色。
    全图而非连通域：字缝里包着的背景颜色和外面背景一致，正是要顺手一起抠掉
    （"中"字口里那点背景不抠，换画面照样在那里拖分数）。误伤目标内部同色块
    的补救 = 把容差调小再点背景；想连别处的第二三种背景色一起抠，再点一处，
    UI 侧按位与叠刀。
    返回 bool 掩码（True=保留）。
    """
    h, w = img.shape[:2]
    sx = min(max(int(seed_xy[0]), 0), w - 1)
    sy = min(max(int(seed_xy[1]), 0), h - 1)
    return ~color_close_mask(img, img[sy, sx], tolerance)


def to_bgra(img: np.ndarray, keep: np.ndarray) -> np.ndarray:
    """BGR 图 + 保留掩码 → BGRA（保留=不透明 255，抠掉=透明 0）。存盘即抠图模板。"""
    alpha = np.where(keep, 255, 0).astype(np.uint8)
    return np.dstack([img, alpha])


def matte_of(tpl: np.ndarray) -> np.ndarray | None:
    """模板图（3 或 4 通道）→ 保留掩码 bool 阵；普通 3 通道模板返回 None。
    判定与 _match_core 的抠图分流同口径（alpha≤127 才算真抠过）。"""
    if tpl.ndim == 3 and tpl.shape[2] == 4 and (tpl[:, :, 3] <= 127).any():
        return tpl[:, :, 3] > 127
    return None


def snap_bbox(frame: np.ndarray, seed: tuple, pad: int = 2):
    """粗种子区 (x0,y0,x1,y1) → 贴边 bbox (x,y,w,h) 或 None。
    两簇颜色分前景/背景（贴边多的簇=背景）→ 最大前景连通域外接框 → 外扩 pad。
    目测坐标误差 ±20px 时的救星：人只给粗区，边让算法吸。
    （原在 tools/frame_snap.py，工作台框选也要用，搬进 core。）"""
    x0, y0, x1, y1 = seed
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(frame.shape[1], x1), min(frame.shape[0], y1)
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return None
    pix = roi.reshape(-1, 3).astype(np.float32)
    # 两簇 KMeans：贴边框像素占多数的簇判为背景
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
    _, lab, cen = cv2.kmeans(pix, 2, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    lab = lab.reshape(roi.shape[:2])
    border = np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])
    bg = int(np.bincount(border).argmax())
    fg = (lab != bg).astype(np.uint8) * 255
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    area = cv2.contourArea(c)
    if area < 100 or area > fg.size * 0.92:   # 太小=没前景；铺满=分簇失败
        return None
    bx, by, bw, bh = cv2.boundingRect(c)
    sx, sy = max(0, bx - pad), max(0, by - pad)
    ex, ey = min(roi.shape[1], bx + bw + pad), min(roi.shape[0], by + bh + pad)
    return (x0 + sx, y0 + sy, ex - sx, ey - sy)


_gray_local = threading.local()


def _gray_cached(img: np.ndarray) -> np.ndarray:
    """整帧灰度 float32 的线程级一拍缓存（成品审计 P1-⑧）。
    按帧对象身份（is）判新鲜：worker 一拍内 ctx.frame 是同一对象，多模板
    反复匹配只算一次 cvtColor+astype。线程本地存 = 十开各拍各的帧互不踢
    缓存（全局单格会被轮着冲掉，等于没缓存）。只存最近一帧，不膨胀。"""
    c = _gray_local
    if getattr(c, "src", None) is img:
        return c.gray
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    c.src = img
    c.gray = g
    return g


def _match_core(img: np.ndarray, tpl: np.ndarray, roi: tuple | None):
    """
    匹配公共核心：尺寸校验 → ROI 裁剪 → 抠图分流 → 灰度化 → 退化窗口掩码。
    返回 (res, search, ox, oy, mask)：res 为分数矩阵，search 为裁剪后大图，
    (ox, oy) 为 ROI 在大图里的原点，mask 为抠图模板的 0/1 遮罩（普通模板 None）；
    前提不成立时返回 None（模板过大 / 区域空洞 / 无纹理区域 / 遮罩退化）。
    """
    if tpl.shape[0] > img.shape[0] or tpl.shape[1] > img.shape[1]:
        return None  # 模板比图还大，不可能找到

    # 抠图模板分流：4 通道 BGRA 且确有透明像素 → 走带遮罩匹配。
    # alpha 拆出来当遮罩（>127 才算保留，半透明边缘像素弃，防蒙边污染分数）；
    # 模板 BGR 部分单列，供候选阶段 RGB 校验对齐 crop（3 通道）比对。
    mask = None
    tpl_bgr = tpl
    if tpl.ndim == 3 and tpl.shape[2] == 4:
        alpha = tpl[:, :, 3]
        if (alpha <= 127).any():           # 全不透明 = 名义带 alpha 实则普通模板
            mask = (alpha > 127).astype(np.float32)
            tpl_bgr = tpl[:, :, :3]

    search = img
    ox = oy = 0
    if roi is not None:
        x0, y0, x1, y1 = roi
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(img.shape[1], int(x1)), min(img.shape[0], int(y1))
        search = img[y0:y1, x0:x1]
        ox, oy = x0, y0
        if search.shape[0] < tpl.shape[0] or search.shape[1] < tpl.shape[1]:
            return None  # 区域比模板还小：matchTemplate 会直接抛异常，提前判无匹配

    # 灰度匹配（性能优化）：三通道匹配计算量是灰度的 3 倍
    # （实测本机全屏三通道单次约 30ms，灰度约 10ms），分数几乎不变。
    # 成品审计 P1-⑧：整帧灰度按线程缓存一拍 —— worker 一拍内同一帧对象被
    # 多模板反复匹配（哨兵 N 行×2 拍/秒是 10 开最先炸的乘数），逐次
    # cvtColor+astype 是每次匹配最大单项开销。灰度对逐像素变换，ROI 灰度
    # = 整帧灰度切片（数学等价），切片须 contiguous（matchTemplate 不吃跨步视图）。
    gray_search = _gray_cached(img)
    if roi is not None:
        gray_search = np.ascontiguousarray(gray_search[y0:y1, x0:x1])
    gray_tpl = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)

    # 无纹理区域防护：纯色/全黑区域方差≈0，相关系数在数学上无定义，
    # OpenCV 会返回假的高分（实测全黑区域匹配白色模板得 1.0）。
    # 方差低于阈值的区域直接判"无匹配"。
    region_std = float(gray_search.std())
    if search.size == 0 or region_std < 0.5:
        return None

    if mask is not None:
        # 抠图模板：只在保留像素上算相关，退化窗口防护已在 _masked_ncc 内做
        res = _masked_ncc(gray_search, gray_tpl, mask)
        if res is None:
            return None
        return res, search, ox, oy, mask

    # TM_CCOEFF_NORMED：归一化相关系数，对亮度变化最稳，分数恒在 -1~1
    res = cv2.matchTemplate(gray_search, gray_tpl, cv2.TM_CCOEFF_NORMED)

    # 退化窗口掩码（实测修复 + 性能优化）：
    # 纯色窗口方差≈0，相关系数数学上无定义，OpenCV 会给这些位置假高分。
    # 掩码计算成本高（两次 box filter），只有"低纹理区域"才可能出现
    # 纯色窗口——区域方差低时启用掩码，正常画面（纹理丰富）跳过。
    if region_std < 20.0:
        win_size = (tpl.shape[1], tpl.shape[0])
        win_mean = cv2.blur(gray_search, win_size)
        win_var = cv2.blur(gray_search * gray_search, win_size) - win_mean * win_mean
        # blur 的方差图与输入同尺寸，且对应"窗口中心"位置；
        # matchTemplate 结果第 (i,j) 个分数 = 图像上起始于 (i,j) 的窗口，
        # 其中心是 (i+h/2, j+w/2)——按中心偏移裁剪对齐
        hh, hw = tpl.shape[0] // 2, tpl.shape[1] // 2
        var_aligned = win_var[hh:hh + res.shape[0], hw:hw + res.shape[1]]
        res[var_aligned < 1.0] = -1.0

    return res, search, ox, oy, None


def _extract_candidates(res: np.ndarray, search: np.ndarray, tpl: np.ndarray,
                        threshold: float, ox: int, oy: int,
                        rgb_threshold: float | None, k: int | None,
                        mask: np.ndarray | None = None) -> list[dict]:
    """
    从分数矩阵提取前 k 个候选（k=None = 全部 ≥ 阈值的），按分数降序。
    每个候选过 RGB 颜色校验（若开启），不含法的剔除。
    mask 非空（抠图模板）时 RGB 校验也只在保留像素上算（见 _cal_rgb_confidence）。
    返回 [{"x","y","score"}, ...]（坐标为原始大图空间的模板左上角）。

    性能：分数矩阵可达数十万元素（全屏匹配），旧版整体 argsort 是 O(n log n)，
    而绝大多数 tick 一个达标位置都没有（等画面出现的轮询全是空手）——先按
    阈值筛出达标位置（O(n) 向量化），只对存活子集排序，输出与旧版等价。
    """
    ys, xs = np.nonzero(res >= threshold)  # 阈值预筛：达标位置通常只有几个
    if ys.size == 0:
        return []
    scores = res[ys, xs]
    order = np.argsort(scores)[::-1]       # 只排达标子集，分数降序

    candidates = []
    for oi in order:
        iy, ix = int(ys[oi]), int(xs[oi])
        s = float(scores[oi])
        if rgb_threshold is not None:
            crop = search[iy:iy + tpl.shape[0], ix:ix + tpl.shape[1]]
            if crop.shape[:2] != tpl.shape[:2]:
                continue
            if _cal_rgb_confidence(crop, tpl, mask) < rgb_threshold:
                continue
        candidates.append({"x": ix + ox, "y": iy + oy, "score": s})
        if k is not None and len(candidates) >= k:
            break
    return candidates


def match_template(img: np.ndarray, tpl: np.ndarray, threshold: float = 0.85,
                   roi: tuple | None = None,
                   rgb_threshold: float | None = None,
                   debug: bool = False) -> dict | None:
    """
    在 img 里找 tpl。roi 传 (x0, y0, x1, y1) 时只在该区域找（更快更精准）；
    不传 = 全屏搜索。

    参数:
        img:           大图（截图），BGR 数组
        tpl:           模板小图，必须比 img 小
        roi:           限定找图区域 (x0, y0, x1, y1)
        rgb_threshold: 灰度命中后再做 RGB 颜色校验，颜色置信度低于它则作废
                       （拒绝"灰度相似但颜色不同"的假阳性）；None = 不校验
        debug:         True 时未命中也返回 dict（matched=False + 实际最高分），
                       供模板工具调参显示真实分数；运行期请保持默认 False
    返回:
        {"x": 模板左上角x, "y": 左上角y, "score": 相似度, "ms": 耗时, "matched": True}
        或 None（debug=False 未命中时）；debug=True 未命中返回 matched=False 的 dict
    """
    t0 = time.perf_counter()
    core = _match_core(img, tpl, roi)
    if core is None:
        ms = (time.perf_counter() - t0) * 1000.0
        if debug:
            return {"x": -1, "y": -1, "score": 0.0, "matched": False, "ms": ms}
        return None

    res, search, ox, oy, mask = core
    cands = _extract_candidates(res, search, tpl, threshold, ox, oy, rgb_threshold, 1, mask)
    ms = (time.perf_counter() - t0) * 1000.0

    if not cands:
        if debug:
            # 未命中（分数低于阈值，或最高分候选被 RGB 校验否决）：
            # 给出灰度层的实际最高分与位置，供调参判断差距
            best = float(res.max()) if res.size else 0.0
            if res.size:
                iy, ix = np.unravel_index(int(res.argmax()), res.shape)
            else:
                iy, ix = 0, 0
            return {"x": int(ix) + ox, "y": int(iy) + oy,
                    "score": best, "matched": False, "ms": ms}
        return None

    hit = cands[0]
    hit["matched"] = True
    hit["ms"] = ms
    return hit


def match_template_all(img: np.ndarray, tpl: np.ndarray, threshold: float = 0.85,
                       roi: tuple | None = None,
                       rgb_threshold: float | None = None,
                       sort_by_score: bool = True,
                       min_gap: int = -1) -> list[dict]:
    """
    多目标返回（大漠 FindPicEx 思想）：找 img 里所有 score ≥ threshold 的 tpl 位置。

    与 match_template 共享同一匹配核心，差异只在「取全部达标位置」而非唯一最高分。
    参数同 match_template，另有：
        sort_by_score: True = 按分数降序；False = 按画面位置（上→下、左→右）
        min_gap:      相邻重叠抑制，≥1 时与已保留候选的左上角在 x、y 双方向都
                      小于该值的候选被丢弃（防同一目标滑窗报几十个相邻高分）；
                      -1 = 关闭（默认）
    返回 [{"x","y","score","ms"}, ...]；无匹配返回 []。
    """
    t0 = time.perf_counter()
    core = _match_core(img, tpl, roi)
    if core is None:
        return []
    res, search, ox, oy, mask = core
    cands = _extract_candidates(res, search, tpl, threshold, ox, oy, rgb_threshold, None, mask)
    ms = (time.perf_counter() - t0) * 1000.0

    # 相邻重叠抑制（贪婪，候选已按分数降序）
    if min_gap >= 1:
        kept = []
        for c in cands:
            if all(abs(c["x"] - k["x"]) >= min_gap or abs(c["y"] - k["y"]) >= min_gap
                   for k in kept):
                kept.append(c)
        cands = kept

    if not sort_by_score:
        cands.sort(key=lambda c: (c["y"], c["x"]))

    for c in cands:
        c["ms"] = ms
    return cands


def match_multi_color(frame: np.ndarray, points: list,
                      tolerance: int = 8) -> dict:
    """
    多点找色（大漠 FindMultiColor 思想）：给 N 个采样点，逐点比对颜色，
    全部命中才算成功。适合「文字/进度条/固定 UI」这种颜色比形状更稳的定位，
    比 match_color 的单峰亮度判定强一个量级。

    参数:
        frame:    大图（BGR 数组，即 capture 截出来的帧）
        points:   [(x, y, "RRGGBB"), ...]，期望颜色用 RGB 十六进制字符串
                  （与模板工具取色器输出的 #RRGGBB 直接打通：取一点喂一点）
        tolerance: 颜色容差，三通道各自 |实际 - 期望| ≤ tolerance 即判该点命中
    返回:
        {"matched": k, "total": N, "ok": k == N}
        始终返回 dict（UI 可显示 "k/N 命中" 便于调点），不抛异常。
    """
    total = len(points)
    if frame is None:
        return {"matched": 0, "total": total, "ok": False}

    h, w = frame.shape[:2]
    matched = 0
    for pt in points:
        # 每个采样点独立容错：格式不对/越界跳过，不影响其余点
        try:
            x, y, hexstr = int(pt[0]), int(pt[1]), str(pt[2]).lstrip("#")
        except (IndexError, TypeError, ValueError):
            continue
        if x < 0 or y < 0 or x >= w or y >= h or len(hexstr) != 6:
            continue
        try:
            er = int(hexstr[0:2], 16)
            eg = int(hexstr[2:4], 16)
            eb = int(hexstr[4:6], 16)
        except ValueError:
            continue
        # frame 是 BGR：像素 [y,x] 顺序为 (B, G, R)
        b, g, r = int(frame[y, x, 0]), int(frame[y, x, 1]), int(frame[y, x, 2])
        if (abs(r - er) <= tolerance and abs(g - eg) <= tolerance
                and abs(b - eb) <= tolerance):
            matched += 1

    return {"matched": matched, "total": total, "ok": matched == total}


def detect_finger(frame: np.ndarray,
                  white_thresh: int = 200,
                  dark_thresh: int = 128,
                  min_area: int = 300,
                  max_area: int = 20000,
                  edge_margin: int = 3,
                  border_ratio: float = 0.2,
                  min_solidity: float = 0.55,
                  min_ratio: float = 0.25,
                  max_ratio: float = 4.0,
                  max_candidates: int = 1) -> dict | None:
    """
    检测「动态引导手指」→ 返回它的位置、指尖、指向与推算点击点。

    为什么不用模板匹配认手指（历史的坑）：`matchTemplate` 只有**平移不变**，
    而引导手指会**缩放/旋转/只从屏幕侧边或下边露一半**——只能靠"每角度每大小
    各存一张"硬堆模板，且露出的一小块信息太少、极易误命中别处（用户实测）。
    本函数改认一个**视觉客体**：「一块够大的白色区域，外圈被暗像素（黑描边）
    包着」。白块对缩放/旋转天然免疫（面积变、仍是亮斑），只露一半也认（露出的
    就是一块白）；黑描边佐证专治"白按钮/白图标/白字"冒充（它们没黑边圈）。

    判据链（任一不过即丢）：
      1. 亮度阈值 → 白掩膜，形态学去噪 + 连通块（findContours）；
      2. 面积带 min_area~max_area（排除白字小碎块与整屏亮背景）；
      3. **黑描边佐证**：白块外扩 edge_margin 像素的一圈里，暗像素占比要够
         （`edge_dark_ratio`）；这是"这是带黑边的手指"而非"随便一块白"的关键；
      4. 实心度 solidity（轮廓面积/凸包面积）≥ min_solidity（排除镂空环状物）；
      5. 宽高比在 [min_ratio, max_ratio]（排除极细长条与极端扁块）。

    指尖与指向（供"由手指推目标"）：
      - `hand_side`：手从屏幕哪条边伸入（left/right/top/bottom）。判据 = 白块
        包围盒哪条边最贴屏幕边缘（含触边）。不触边则取"质心离哪条边最近"。
      - `tip`：**离触入边最远的那个白像素**（手从边上来，只有指头伸向画面内）
        ——用距离变换取块内最大距离点，对朝向免疫。
      - `dir`：单位指向向量 = (tip - 块质心) 归一化。
      - 点击点不在本函数算：由 `extend_click(finger, extend_px)` 现算
        （tip + extend_px × dir），外延距离按场景调。

    参数:
        frame:          大图（BGR）
        white_thresh:   白掩膜阈值（灰度 > 它算白）
        dark_thresh:    描边暗阈值（灰度 < 它算描边/暗像素，默认 128 覆盖抗锯齿细线）
        min_area/max_area: 白块面积带（px²）；靠边只露半只手时白块变小，下限别太大
        edge_margin:    描边检验的紧环宽度（px，默认 3：形态学环 = 白块外扩它一圈）
        border_ratio:   紧环内"暗像素"占比下限（默认 0.2；真手指实测 0.3~0.37）
        min_solidity:   实心度下限
        min_ratio/max_ratio: 宽高比上下限
        max_candidates: 取最像的前 N 个（默认 1）；返回单个 dict 或 list
    返回（max_candidates=1 时）dict 或 None：
        {"x","y","w","h","cx","cy","area","side","tip","dir","click","score"}
        x/y/w/h = 包围盒左上+尺寸；cx/cy = 质心；side = 触入边；
        tip = (tx,ty) 指尖；dir = (dx,dy) 单位向量；click = (px,py) 推算点击点。
    绝不抛：任何异常/空帧返回 None（调用侧按"没检测到"处理）。
    """
    try:
        if frame is None or frame.size == 0:
            return None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h_img, w_img = gray.shape[:2]
        # 1. 白掩膜（描边佐证改相对判据后不再需要绝对暗掩膜）
        _, white = cv2.threshold(gray, int(white_thresh), 255, cv2.THRESH_BINARY)
        # 形态学：开运算去孤立噪点，闭运算把白块里的小黑点（手套褶皱）填掉
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        white = cv2.morphologyEx(white, cv2.MORPH_OPEN, k)
        white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, k)
        contours, _ = cv2.findContours(white, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        cands = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area or area > max_area:
                continue
            x, y, w, h = cv2.boundingRect(c)
            if w <= 0 or h <= 0:
                continue
            ratio = w / float(h)
            if ratio < min_ratio or ratio > max_ratio:
                continue
            hull = cv2.convexHull(c)
            hull_area = cv2.contourArea(hull)
            solidity = (area / hull_area) if hull_area > 0 else 0.0
            if solidity < min_solidity:
                continue
            # 3. 黑描边佐证：白块**轮廓紧贴外一圈**里"暗像素"占比要够。
            #   判据定案史（都经真图实测，2026-10-11）：
            #   - 环几何必须用**形态学环**（dilate(白掩膜) − 白掩膜），不能用包围盒
            #     外扩环 —— 后者把四角远处背景也算进来，稀释占比。
            #   - 暗阈值用**绝对灰度**（默认 128）：游戏描边是深色线，抗锯齿会把边缘
            #     提到 85~150，故阈值取 128 覆盖它；真手指紧环内 <128 约 30~37%。
            #   - 占比门槛默认 0.2：真手指稳过；"白块@浅背景/纯白块无边"周围无 <128
            #     像素 → 占比≈0 被拒；"白字"面积太小早被面积带刷掉。
            wb_bin = (white > 0).astype(np.uint8)
            mm = max(1, int(edge_margin))
            kk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * mm + 1, 2 * mm + 1))
            ring = (cv2.dilate(wb_bin, kk) > 0) & (wb_bin == 0)
            if not ring.any():
                continue
            dark_ratio = float((gray[ring] < float(dark_thresh)).mean())
            if dark_ratio < float(border_ratio):   # 描边佐证门槛（默认 0.2）
                continue
            # 质心（用矩）
            M = cv2.moments(c)
            if M["m00"] <= 0:
                continue
            cx = M["m10"] / M["m00"]
            cy = M["m01"] / M["m00"]
            # 触入边：包围盒哪条边最贴屏幕边缘（离屏幕边 ≤ edge_margin 即算触边）
            gaps = {"left": x, "right": (w_img - (x + w)),
                    "top": y, "bottom": (h_img - (y + h))}
            side = min(gaps, key=lambda s: gaps[s])
            # 指尖：块内离"触入边"最远的白像素 → 用掩膜 + 距离变换取最远点
            x0, y0 = x, y
            sub_white = white[y0:y0 + h, x0:x0 + w].copy()
            # 离触入边的距离场
            ys, xs = np.nonzero(sub_white)
            if ys.size == 0:
                continue
            if side == "left":
                d = xs.astype(np.float32)
            elif side == "right":
                d = (w - 1 - xs).astype(np.float32)
            elif side == "top":
                d = ys.astype(np.float32)
            else:  # bottom
                d = (h - 1 - ys).astype(np.float32)
            kk = int(np.argmax(d))
            tip_x = x0 + int(xs[kk])
            tip_y = y0 + int(ys[kk])
            # 指向 = (指尖 - 质心) 归一化
            vx, vy = tip_x - cx, tip_y - cy
            norm = (vx * vx + vy * vy) ** 0.5
            if norm < 1e-3:
                dirx, diry = 0.0, 0.0
            else:
                dirx, diry = vx / norm, vy / norm
            # 打分：面积越接近"手掌典型"越大、黑描边占比越高越像、越靠中心越高
            cands.append({
                "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                "cx": float(cx), "cy": float(cy), "area": float(area),
                "solidity": float(solidity), "edge_dark_ratio": float(dark_ratio),
                "side": side, "tip": (int(tip_x), int(tip_y)),
                "dir": (float(dirx), float(diry)),
                "score": float(area) * float(dark_ratio) * float(solidity),
            })
        if not cands:
            return None
        # 唯一性：按 score 降序取前 max_candidates（默认 1 = 只认最像的一个，
        # 不是"任一命中就点"的弱 OR —— 那是历史误报的主因）
        cands.sort(key=lambda d: d["score"], reverse=True)
        for c in cands:
            c["click"] = None      # 点击点由调用方用 extend_px 现算（见 extend_click）
        if max_candidates == 1:
            return cands[0]
        return cands[:max_candidates]
    except Exception:
        return None      # 检测绝不反噬流程


def extend_click(finger: dict, extend_px: float = 0.0) -> tuple[int, int]:
    """由手指框算"点击点"：指尖 + extend_px × 指向。
    extend_px = 0 → 就点指尖；>0 → 顺着手势线往前推（指尖离目标还有一段时用）。
    手指为 None / 无方向 → 回落到质心（尽力而为，不抛）。"""
    if not finger:
        return (0, 0)
    tx, ty = finger.get("tip") or (int(finger["cx"]), int(finger["cy"]))
    dx, dy = finger.get("dir") or (0.0, 0.0)
    return (int(round(tx + dx * extend_px)), int(round(ty + dy * extend_px)))


def extend_click_auto(frame: np.ndarray, finger: dict,
                      max_scan: int = 400,
                      sat_thresh: int = 60,
                      gap_tolerance: int = 2,
                      min_run: int = 6) -> tuple[int, int]:
    """自动外延：从指尖沿指向扫描，把点击点钉在手指指向的目标「内部」。

    为什么需要：引导手指的指尖离目标距离每次不同（实测 50~200px），固定
    extend_px 必然有时点空。本函数从指尖出发，沿 dir 方向逐像素扫描，
    找到第一个「连续高饱和度区间」（= 手指指向的彩色 UI 元素），取**区间中点**。
    取中点而不是"第一个高饱和点"，因为区间起点往往是目标的高光/边缘（点边缘
    会点空），中点必在区间内部 = 目标内部，能稳定点中。

    判据：像素饱和度（max-min）> sat_thresh 记为"在目标上"。白色手指、深色
    背景饱和度都低（<30），金色/彩色按钮/图标饱和度 >60。区间允许
    gap_tolerance 像素的低饱和断点（防抗锯齿/高光把区间劈成两段）。

    参数:
        frame: 当前帧（BGR）
        finger: detect_finger 返回的 dict（含 tip/dir）
        max_scan: 最大扫描距离（px，指尖到目标的距离上限）
        sat_thresh: 饱和度阈值（彩色 UI 通常 > 60）
        gap_tolerance: 区间内允许多少连续低饱和像素仍算同一区间
        min_run: 区间至少多少个采样点才算有效（滤掉细小噪点）

    返回: (x, y) 点击点坐标。扫不出有效区间 / 手指为 None / 无方向 → 点指尖。
    绝不抛异常：任何内部错误都回落到指尖（与 detect_finger 同一纪律）。
    """
    try:
        if not finger:
            return (0, 0)
        tx, ty = finger.get("tip") or (int(finger["cx"]), int(finger["cy"]))
        dx, dy = finger.get("dir") or (0.0, 0.0)
        tip = (int(tx), int(ty))
        if frame is None or frame.size == 0 or (abs(dx) < 1e-6 and abs(dy) < 1e-6):
            return tip
        h, w = frame.shape[:2]

        # 沿射线采样：在目标上的记为区间段（允许小 gap 续上）
        # pts[d] = 该采样点饱和度，供最长区间取饱和度加权中心
        pts = {}
        segs = []          # [(start_d, end_d), ...] 已闭合的区间
        cur_start = None   # 当前未闭合区间起点
        gap = 0            # 当前连续低饱和长度
        for d in range(1, int(max_scan) + 1):
            ix, iy = int(round(tx + dx * d)), int(round(ty + dy * d))
            if not (0 <= ix < w and 0 <= iy < h):
                break      # 扫出画面
            b = int(frame[iy, ix, 0]); g = int(frame[iy, ix, 1]); r = int(frame[iy, ix, 2])
            sat = max(b, g, r) - min(b, g, r)
            pts[d] = sat
            on_target = sat > sat_thresh
            if on_target:
                if cur_start is None:
                    cur_start = d
                gap = 0
            else:
                if cur_start is not None:
                    gap += 1
                    if gap > gap_tolerance:
                        segs.append((cur_start, d - gap))   # 闭合上一段
                        cur_start = None
                        gap = 0
        if cur_start is not None:
            segs.append((cur_start, max_scan))

        # 取**最长**的有效区间（按钮主体是连续高饱和区间里最长的一段，
        # 边缘高光/噪点都是短促区间，被长度门槛刷掉）
        best_seg = None
        for s, e in segs:
            if (e - s + 1) < min_run:
                continue
            if best_seg is None or (e - s) > (best_seg[1] - best_seg[0]):
                best_seg = (s, e)
        if best_seg is not None:
            s, e = best_seg
            # 区间内取饱和度加权中心：主体/高光饱和度高、边缘低，加权中心
            # 天然偏向按钮主体，比纯几何中点更靠近"点得到"的位置
            num = 0.0; den = 0.0
            for d in range(s, e + 1):
                sv = pts.get(d, 0)
                num += d * sv
                den += sv
            mid = num / den if den > 0 else (s + e) / 2
            ix = int(round(tx + dx * mid)); iy = int(round(ty + dy * mid))
            ix = max(0, min(w - 1, ix)); iy = max(0, min(h - 1, iy))
            return (ix, iy)
        return tip   # 没有有效区间 → 点指尖（尽力而为）
    except Exception:
        return extend_click(finger, 0)   # 任何异常回落指尖，绝不反噬流程


def group_best_match(img: np.ndarray, imgs: list[np.ndarray],
                     roi: tuple | None = None,
                     min_score: float = 0.6):
    """
    帧组匹配（motion 动态检测用）：一组动画关键帧里，当前画面最像哪一帧。

    每帧跑一次 _match_core 取最大相似度（同一匹配核心，逐帧独立滑窗），
    返回 (帧下标, 相似度, x, y)；画面缺席（连最像的都低于 min_score）返回
    None —— 调用方据此"按没看到处理"，不把缺席误判成"某一帧"。

    返回帧下标本身而不只分数：motion 靠"下标在变"判动画在动，下标只
    在动画真切帧时才跳；分数会被截图亮度噪声带抖。帧间要挑差别明显的
    瞬间采（两帧太像的话噪声就能让下标乱跳）。
    """
    best = None
    for i, tpl in enumerate(imgs):
        core = _match_core(img, tpl, roi)
        if core is None:
            continue
        res = core[0]
        if not res.size:
            continue
        s = float(res.max())
        if best is None or s > best[1]:
            iy, ix = np.unravel_index(int(res.argmax()), res.shape)
            best = (i, s, int(ix) + core[2], int(iy) + core[3])
    if best is None or best[1] < min_score:
        return None
    return best


def match_color(frame: np.ndarray, roi: tuple | None = None,
                peak_thresh: float = 200.0, channel: str = "bgr") -> bool:
    """
    取色检测抽象（ALAS Button 思想）：判断 roi 区域内是否出现某"峰值色"。

    用途：颜色比"形状模板"更稳定时用颜色判状态。例：下拉框选项已解锁 = 白字
    （峰值亮度高）vs 锁定 = 灰字（峰值亮度低），无需为每个选项采模板。

    参数:
        frame:       大图（BGR）
        roi:         限定区域 (x0, y0, x1, y1)；None = 全图
        peak_thresh: 峰值阈值，roi 内最大值 > 它判 True
        channel:     "bgr" = 三通道取 max（与原 win.max() 行为一致）；
                     "gray" = 先转灰度再取 max
    """
    if roi is not None:
        x0, y0, x1, y1 = roi
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(frame.shape[1], int(x1)), min(frame.shape[0], int(y1))
        roi_img = frame[y0:y1, x0:x1]
    else:
        roi_img = frame
    if roi_img.size == 0:
        return False
    if channel == "gray":
        roi_img = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
    return float(roi_img.max()) > peak_thresh


def health_report(frame: np.ndarray, templates, loader,
                  default_threshold: float = 0.85) -> dict:
    """模板体检核心：给定一屏画面，逐张模板找图，输出"死图"清单。

    纯函数、不碰模拟器/网络——画面来源随意（真机帧、shot 存的 png、合成图），
    所以游戏改版后可以先 shot 一帧、离线反复复检圈图。判据 = 该条账本自己的
    threshold（缺账本值才用 default_threshold 兜底），match 走 debug=True
    拿真实最高分。

    参数:
        templates: 账本条目列表（含 name/category/threshold）
        loader:    条目 -> 模板图 ndarray；返回 None = 账本在、PNG 读不出
    返回:
        {"checked": n,
         "dead": [{"category","name","score","threshold"}, ...按分数升序],
         "skipped": [{"category","name","why"}, ...],   # 没 PNG / 模板比画面大
         "by_category": {库名: {"total": n, "dead": m}}}
    """
    dead, skipped = [], []
    by_cat: dict[str, dict] = {}
    for t in templates or []:
        cat = t.get("category") or "?"
        name = t.get("name") or "?"
        thr = float(t.get("threshold") or default_threshold)
        stat = by_cat.setdefault(cat, {"total": 0, "dead": 0})
        stat["total"] += 1
        img = loader(t)
        if img is None:
            skipped.append({"category": cat, "name": name, "why": "账本在但 PNG 读不出"})
            continue
        if img.shape[0] > frame.shape[0] or img.shape[1] > frame.shape[1]:
            skipped.append({"category": cat, "name": name, "why": "模板比画面还大，没法找"})
            continue
        hit = match_template(frame, img, threshold=thr,
                             rgb_threshold=t.get("rgb_threshold"), debug=True)
        if hit is None or not hit.get("matched"):
            stat["dead"] += 1
            dead.append({"category": cat, "name": name,
                         "score": round(float(hit["score"]) if hit else 0.0, 4),
                         "threshold": thr})
    dead.sort(key=lambda d: d["score"])
    return {"checked": sum(v["total"] for v in by_cat.values()),
            "dead": dead, "skipped": skipped, "by_category": by_cat}


def health_from_library(frame: np.ndarray, lib: str | None = None,
                        default_threshold: float = 0.85) -> dict:
    """按当前模板库账本做体检（CLI health 与 AI 流水线共用这一个入口）。
    lib 给目录名 = 只扫该库。延迟 import template_manager 保持 vision 顶轻。"""
    from core import template_manager as tm
    entries = tm.list_templates()
    if lib:
        entries = [t for t in entries if t.get("category") == lib]
    return health_report(frame, entries, tm.load_template_image, default_threshold)


def get_screen(index: int) -> tuple[np.ndarray, dict] | None:
    """
    拿"识别用屏幕图"：走 capture.grab_with_fallback 统一取帧口（与挂机/
    测试/预览四条路同口径：adb 单次快照；窗口截图只服务手动采集场景），窗口帧再
    校准裁安卓区 → 基准；校准失败退回"整窗缩放到基准"（有轻微变形，
    但模板匹配仍自洽）。纯安卓帧（from_adb）已是基准尺寸、无框可裁。
    返回 (图片, 校准信息dict) 或 None。
    """
    inst = ldplayer.get_instance(index)
    if inst is None or not inst.running:
        print(f"实例 {index} 未运行")
        return None

    img, from_adb = capture.grab_with_fallback(inst.top_hwnd, index)
    if img is None:
        print("截图失败")
        return None
    if from_adb:
        return img, {}

    calib = capture.calibrate(inst.top_hwnd, index)
    if calib is not None:
        return capture.crop_android(img, calib), calib
    # 校准失败：整窗缩放到基准（有变形警告）
    print("[警告] 校准失败，使用整窗缩放回退模式（坐标可能有轻微偏差）")
    return capture.to_base(img), {}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法：")
        print("  python -m core.vision cut <index> <x> <y> <w> <h> <模板.png>")
        print("  python -m core.vision match <index> <模板.png> [--threshold 0.85] [--click]")
        sys.exit(1)

    cmd = sys.argv[1]
    try:
        idx = int(sys.argv[2])   # health 的 index 可缺省（--frame 离线模式），非数字位参留给各分支自解析
    except ValueError:
        idx = None

    if cmd == "cut":
        # 框选保存模板：坐标是安卓坐标（基准分辨率 540x960 空间）
        x, y, w, h = (int(v) for v in sys.argv[3:7])
        out = sys.argv[7]
        screen, _ = get_screen(idx)
        if screen is None:
            sys.exit(1)
        capture.imwrite_cn(out, screen[y:y + h, x:x + w])
        print(f"已保存模板 {out}（{w}x{h}）")

    elif cmd == "shot":
        # 保存当前画面，方便查看按钮位置（图片坐标 = 安卓坐标，可直接用于 cut）
        # PROJECT_ROOT 锚定：相对 logs/ 在 cwd 不是项目根时会写歪地方
        import os
        from core.config_manager import PROJECT_ROOT
        out = sys.argv[3] if len(sys.argv) > 3 else str(PROJECT_ROOT / "logs" / "screen.png")
        screen, calib = get_screen(idx)
        if screen is None:
            sys.exit(1)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        capture.imwrite_cn(out, screen)
        print(f"已保存画面 {out}（{screen.shape[1]}x{screen.shape[0]}，安卓坐标空间）")

    elif cmd == "match":
        tpl_path = sys.argv[3]
        threshold = 0.85
        click = False
        for arg in sys.argv[4:]:
            if arg.startswith("--threshold"):
                threshold = float(arg.split("=", 1)[1] if "=" in arg else sys.argv[sys.argv.index(arg) + 1])
            if arg == "--click":
                click = True

        tpl = capture.load_image_cn(tpl_path)
        if tpl is None:
            print(f"模板读不出来：{tpl_path}")
            sys.exit(1)

        screen, calib = get_screen(idx)
        if screen is None:
            sys.exit(1)

        hit = match_template(screen, tpl, threshold)
        if hit is None:
            print(f"未找到（阈值 {threshold}）")
            sys.exit(1)

        cx, cy = hit["x"] + tpl.shape[1] // 2, hit["y"] + tpl.shape[0] // 2
        print(f"找到！相似度={hit['score']:.4f}  左上角=({hit['x']},{hit['y']})  中心=({cx},{cy})")
        if calib:
            print(f"（校准：安卓画面偏移=({calib['offset_x']},{calib['offset_y']})）")

        if click:
            ldplayer.adb_shell(idx, f"input tap {cx} {cy}")
            print(f"已点击 ({cx},{cy})")
    elif cmd == "health":
        # 模板体检：见模块 docstring。--frame 有画面文件就不碰模拟器（离线复检）
        idx, frame_path, lib, thr, json_out = None, None, None, 0.85, None
        argv = sys.argv[2:]
        j = 0
        while j < len(argv):
            a = argv[j]
            if a in ("--frame", "--lib", "--threshold", "--json"):
                j += 1
                v = argv[j]
                if a == "--frame":
                    frame_path = v
                elif a == "--lib":
                    lib = v
                elif a == "--threshold":
                    thr = float(v)
                else:
                    json_out = v
            else:
                idx = int(a)
            j += 1
        if frame_path is not None:
            frame = capture.load_image_cn(frame_path)
            if frame is None:
                print(f"画面读不出来：{frame_path}")
                sys.exit(1)
            src = frame_path
        else:
            if idx is None:
                print("用法：python -m core.vision health <index|--frame 画面.png> "
                      "[--lib 库名] [--threshold 0.85] [--json out.json]")
                sys.exit(1)
            got = get_screen(idx)
            if got is None:
                sys.exit(1)
            frame, _ = got
            src = f"实例 {idx} 当前帧"
        rep = health_from_library(frame, lib=lib, default_threshold=thr)
        print(f"体检画面：{src}（{frame.shape[1]}x{frame.shape[0]}）· "
              f"扫描 {rep['checked']} 张 · 死图 {len(rep['dead'])} · 跳过 {len(rep['skipped'])}")
        for cat, st in sorted(rep["by_category"].items()):
            mark = " ⚠" if st["dead"] else ""
            print(f"  {cat}：{st['total'] - st['dead']}/{st['total']} 通过{mark}")
        for d in rep["dead"]:
            print(f"  [死图] {d['category']}/{d['name']}  分数={d['score']} < 阈值={d['threshold']}")
        for s in rep["skipped"]:
            print(f"  [跳过] {s['category']}/{s['name']}：{s['why']}")
        if json_out:
            json_store.atomic_dump(json_out, {"frame": src, **rep})
            print(f"机器可读报告 → {json_out}")
    else:
        print(f"未知命令：{cmd}")
