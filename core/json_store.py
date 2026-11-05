"""
core/json_store.py — JSON 落盘文件的三个标准动作（容错读 / 原子写 / 带历史快照的原子写）

config_manager、template_manager、ldplayer 各写过一份
「try: open→json.load」和「写 .tmp→os.replace」的样板，四处四个样。
本模块收口成一对函数；2026-10-09 用户资产保险丝再收第三个：

- load_or_default：文件不存在 / 读不了（IO 错）/ JSON 语法坏 → 默认值的深拷贝。
  只吞这三类"读不出来"，解析成功后调用方对值的转换错（int() 等）照旧自己管——
  与原各处语义一致（各处原来也只在 open/load 段设防）。
- atomic_dump：先写 <path>.tmp 再 os.replace 整体替换，断电不产生半个坏文件。
  写侧异常（磁盘满/无权限）照常上抛——写失败静默会丢用户资产，不许吞。
- dump_with_history：atomic_dump 之前，若旧版存在且"文本内容确有变化"，先把
  旧版存档到备份目录（保留最近 keep 份，超出剪最旧）。用户资产账本专用保险丝：
  改坏了从备份目录挑一份拷回覆盖即可，不需要"动库前人肉签字"那套流程。
  快照是保险丝不是主链路：存档 IO 失败只放弃这次存档，绝不拦正常落盘。
"""

import copy
import json
import os
from datetime import datetime
from pathlib import Path


def load_or_default(path, default):
    """读 JSON 文件；不存在 / IO 错 / JSON 语法错 → 返回 default 的深拷贝。
    深拷贝防调用方就地改缓存的默认值（同一可变对象被多轮读共享是隐蔽 bug 源）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        # OSError 已含 FileNotFoundError/PermissionError 等"文件名错/读不了"
        return copy.deepcopy(default)


def atomic_dump(path, data):
    """原子写 JSON（ensure_ascii=False, indent=2，与原各处写法一致）。
    父目录不存在会先建；写侧异常上抛。"""
    p = os.fspath(path)
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def dump_with_history(path, data, backup_dir, prefix, keep=30) -> bool:
    """用户资产专用落盘：原子写 + 历史快照。

    覆写前把旧版存进 <backup_dir>/<prefix>_<时间戳>.json，超出 keep 份剪最旧
    （只剪本函数自己产出的快照，绝不碰目录里别人的东西）。规则：
    - 旧版不存在（首次落盘=初版）或 内容与新版相同 → 不打快照
      （无变化重复保存不刷屏——快照的意义是"曾经丢过的东西能找回"）；
    - 比对按文本不按字节：atomic_dump 走文本模式写盘，Windows 会 \\n→\\r\\n，
      读回时换行已归一，按原始字节比会"永远不相等"（config_manager 首版实测踩过）；
    - 快照段任何 OSError 只放弃这次存档、不拦主写（保险丝不能反过来烧主路）；
      主写段异常照 atomic_dump 上抛——写失败静默会丢用户资产，不许吞。
    返回本次是否打了快照（冒烟据此断言）。"""
    p = os.fspath(path)
    new_text = json.dumps(data, ensure_ascii=False, indent=2)
    old_text = None
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                old_text = f.read()
    except OSError:
        old_text = None      # 读不出旧版就不备份，绝不因此拦新写
    snapped = False
    if old_text is not None and old_text != new_text:
        try:
            bd = os.fspath(backup_dir)
            os.makedirs(bd, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")   # 微秒防快循环撞名
            with open(os.path.join(bd, f"{prefix}_{stamp}.json"), "w",
                      encoding="utf-8", newline="") as f:
                f.write(old_text)
            files = sorted(
                (f for f in os.listdir(bd)
                 if f.startswith(f"{prefix}_") and f.endswith(".json")))
            for f in files[:-keep]:
                os.remove(os.path.join(bd, f))
            snapped = True
        except OSError:
            pass             # 快照失败=保险丝熔断，主写照常
    atomic_dump(path, data)
    return snapped
