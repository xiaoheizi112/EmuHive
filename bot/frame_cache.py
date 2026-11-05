"""
bot/frame_cache.py — 同拍识别结果缓存（bot 层，仿 vision._gray_cached 的按帧判鲜套路）

一拍之内 ctx.frame 是同一个 ndarray 对象，同一帧上重复问同一个问题
（门 template_present 命中后动作 tap_template 再匹配一遍、遍历去重邻框重读、
弹窗区多行撞同图……）答案必然相同，第二次起就该白嫖第一次。
性能审计：这是"门+动作同帧双匹配"这类翻倍开销的单一收口点。

口径：
- 新鲜度按帧对象身份（is）判，不看内容：worker 每拍截新帧（新对象），
  一帧对象活不过一拍 → 缓存自动作废，永不脏。
- 线程本地（threading.local）：多开各线程各拍各的缓存，互不踢（全局单格
  会被轮着冲掉等于没缓存）。
- 键必须含全部影响结果的入参（cat/name/roi、邻框钳后坐标），少一维就是脏读。
"""

import threading

_local = threading.local()


def cached(ctx, key, fn):
    """当前帧内 key 只算一次 fn()，后续原样复用（fn 返回值按引用共享，调用方
    别就地改它）。ctx.frame 一换对象，整格缓存清空重开。"""
    frame = ctx.frame
    store = getattr(_local, "store", None)
    if store is None or store[0] is not frame:
        store = _local.store = [frame, {}]
    d = store[1]
    if key not in d:
        d[key] = fn()
    return d[key]
