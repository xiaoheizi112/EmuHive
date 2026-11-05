# -*- coding: utf-8 -*-
"""
tests/smoke_core_fixes.py — core 修复队回归冒烟（2026-10-04）

覆盖（全离屏、临时配置单例，不打真模拟器、不碰真 configs/templates）：
① core/json_store：atomic_dump/load_or_default round-trip、坏文件回默认、
   默认值深拷贝不共享
② template_manager.load_template_image 负缓存：None（PNG 丢失）也命中缓存，
   不每拍重复磁盘探测
③ ldplayer.android_size：adb_command 非 0 抛的 RuntimeError 接住返回 None
   （不再打穿"取不到返回 None"承诺）；TimeoutExpired/FileNotFoundError 同验
④ quit_now / quit_indexes_now / quit_all 三路共用 _quit_batch（打桩 Popen /
   实例快照，含硬杀补刀与 ldconsole 缺失分支）
⑤ config：新三键登记、base_width/base_height 死旋钮删除、get_safe 类型防御、
   blocked_indexes 转正
⑥ database：run_log 建表与 add_run_log 已删
⑦ run_log：常驻句柄按日轮换（临时目录），一次 import 不逐行开文件
"""

import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["EMUHIVE_NO_FILELOG"] = "1"   # 冒烟不往仓库 logs/ 写

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── 冒烟写盘红线：先换临时配置单例，绝不打真 configs/config.json ──
import core.config_manager as cm

_tmp = Path(tempfile.mkdtemp(prefix="smoke_core_fixes_"))
cm._config = cm.ConfigManager(_tmp / "config.json")

from core import database, json_store, ldplayer, run_log, template_manager

# ────────────────────────────────────────────────────────────────────
# ① json_store
d = _tmp / "js"
p = d / "a.json"                      # 父目录不存在：atomic_dump 应自建
data = {"x": [1, 2], "y": {"z": "中文"}}
json_store.atomic_dump(p, data)
assert json_store.load_or_default(p, {}) == data, "round-trip 不一致"
assert not (d / "a.json.tmp").exists(), "atomic_dump 不许残留 .tmp"

default = {"templates": []}
got = json_store.load_or_default(d / "missing.json", default)
assert got == default and got is not default, "缺文件应回默认"
got["templates"].append(1)
assert default["templates"] == [], "默认值必须深拷贝，不许被调用方就地改污染"

bad = d / "bad.json"
bad.write_text("{oops", encoding="utf-8")
assert json_store.load_or_default(bad, {"k": 1}) == {"k": 1}, "坏 JSON 应回默认"

# ────────────────────────────────────────────────────────────────────
# ② 模板图像负缓存：读失败(None)也照缓存，第二条不许再碰磁盘
calls: list[str] = []
_orig_load = template_manager.load_image_cn
try:
    template_manager.load_image_cn = lambda path, keep_alpha=False: (
        calls.append(path) or None)
    entry = {"file": "_smoke_missing_tpl.png"}
    assert template_manager.load_template_image(entry) is None
    assert template_manager.load_template_image(entry) is None
    assert len(calls) == 1, f"负缓存失效：丢了图每拍重复磁盘探测 {len(calls)} 次"
finally:
    template_manager.load_image_cn = _orig_load

# ────────────────────────────────────────────────────────────────────
# ③ android_size 读类承诺：adb_shell 三种炸法一律返回 None
_orig_shell = ldplayer.adb_shell
try:
    for _f, _tag in (
        (lambda *a, **k: (_ for _ in ()).throw(RuntimeError("adb rc=1")), "RuntimeError"),
        (lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("无 ldconsole")), "FileNotFoundError"),
        (lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(["x"], 1)), "TimeoutExpired"),
    ):
        ldplayer.adb_shell = _f
        assert ldplayer.android_size(999, force=True) is None, f"{_tag} 应返回 None 不许上抛"
finally:
    ldplayer.adb_shell = _orig_shell

# ────────────────────────────────────────────────────────────────────
# ④ quit 三路共用 _quit_batch（纯打桩，不碰真雷电）
class _Inst:
    __slots__ = ("index", "pid", "top_hwnd")

    def __init__(self, index, pid):
        self.index, self.pid, self.top_hwnd = index, pid, 1

    @property
    def running(self):
        return self.pid > 0


_state = {1: 101, 2: 102}                       # index→pid
_snapshot = lambda: [_Inst(i, p) for i, p in _state.items()]
quits: list[tuple] = []
kills: list[tuple] = []
_stubborn: set[int] = set()                     # 这批 id 在 Popen 后不自动退，等 taskkill


class _FakePopen:
    def __init__(self, args, **kw):
        quits.append(tuple(args))
        idx = int(args[-1])                     # [..., "--index", "N"]
        if idx not in _stubborn:
            _state[idx] = -1                    # 模拟 graceful 期自然退出

    def poll(self):
        return 0                                # quit 命令早退：finally 收摊 no-op


def _fake_run(args, **kw):
    kills.append(tuple(args))
    pid = int(args[-1])                         # taskkill ... /PID <pid>
    for i, p in list(_state.items()):
        if p == pid:
            _state[i] = -1                      # 补刀生效：快照里它没了


_orig = (ldplayer.subprocess, ldplayer.list_instances, ldplayer.refresh_instances,
         ldplayer._console_path, ldplayer._quit_batch)
_real_batch = ldplayer._quit_batch
_via_batch: list[list] = []


def _spy_batch(*a, **k):                        # 证明三个公开入口都真走了批量实现
    r = _real_batch(*a, **k)
    _via_batch.append(list(a[0]))
    return r


try:
    ldplayer.subprocess = types.SimpleNamespace(
        Popen=_FakePopen, run=_fake_run, DEVNULL=subprocess.DEVNULL)
    ldplayer.list_instances = ldplayer.refresh_instances = _snapshot
    ldplayer._console_path = lambda: "fake/ldconsole.exe"
    ldplayer._quit_batch = _spy_batch

    # 4a 单机：quit_now → _quit_batch([1])，graceful 期自然退 = True、零补刀
    _state.update({1: 101, 2: 102})
    quits.clear(); kills.clear(); _via_batch.clear()
    assert ldplayer.quit_now(1, graceful_wait=0.6, kill_wait=0.6) is True
    assert _via_batch == [[1]], f"quit_now 没走批量实现：{_via_batch}"
    assert len(quits) == 1 and not kills, f"单机 graceful 自然退不该有补刀：{quits},{kills}"

    # 4b 批量：两台全 graceful 退 = 汇总"已关闭 2 台"、零 taskkill
    _state.update({1: 101, 2: 102})
    quits.clear(); kills.clear(); _via_batch.clear()
    rep = ldplayer.quit_indexes_now([1, 2], graceful_wait=0.6, kill_wait=0.6)
    assert _via_batch == [[1, 2]], f"quit_indexes_now 没走批量实现：{_via_batch}"
    assert len(quits) == 2 and "已关闭 2 台" in rep and not kills, rep

    # 4c 批量赖台补刀：台 2 硬扛 → taskkill /T → "硬杀 1 台"
    _state.update({1: 101, 2: 102})
    _stubborn.add(2)
    quits.clear(); kills.clear()
    rep = ldplayer.quit_indexes_now([1, 2], graceful_wait=0.6, kill_wait=1.2)
    _stubborn.clear()
    assert "硬杀 1 台 [2]" in rep and "已关闭 2 台" in rep, rep
    assert kills and kills[-1][:3] == ("taskkill", "/F", "/T"), kills

    # 4d 全部关闭：quit_all 同样经批量实现，运行中的都关净 = True
    _state.update({1: 101, 2: 102})
    quits.clear(); _via_batch.clear()
    assert ldplayer.quit_all(graceful_wait=0.6, kill_wait=0.6) is True
    assert _via_batch and sorted(_via_batch[0]) == [1, 2], f"quit_all 没走批量实现：{_via_batch}"

    # 4e ldconsole 缺失分支：quit_now 抛 FileNotFoundError（旧语义）、
    #    quit_indexes_now 返回错误字符串、quit_all 返回 False
    ldplayer.subprocess = types.SimpleNamespace(
        Popen=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no console")),
        run=_fake_run, DEVNULL=subprocess.DEVNULL)
    try:
        ldplayer.quit_now(1)
        assert False, "缺 ldconsole 必须抛 FileNotFoundError"
    except FileNotFoundError:
        pass
    assert "找不到 ldconsole" in ldplayer.quit_indexes_now([1, 2])
    _state.update({1: 101, 2: 102})               # 有可关的台才进流水线撞错误分支
    assert ldplayer.quit_all() is False
finally:
    (ldplayer.subprocess, ldplayer.list_instances, ldplayer.refresh_instances,
     ldplayer._console_path, ldplayer._quit_batch) = _orig

# ────────────────────────────────────────────────────────────────────
# ⑤ config：新三键 / 死旋钮删除 / get_safe / blocked_indexes 转正
cfg = cm.get_config()
assert cfg.get("general.launch_calibrate_timeout") == 5.0
assert cfg.get("vision.ocr_tick_budget_sec") == 0.2
assert cfg.get("vision.click_timeout_scan_budget_sec") == 0.15
assert "base_width" not in cm.DEFAULT_CONFIG["emulator"], "base_width 死旋钮应删"
assert "base_height" not in cm.DEFAULT_CONFIG["emulator"], "base_height 死旋钮应删"
assert callable(ldplayer.blocked_indexes) and ldplayer.blocked_indexes() == set()
assert ldplayer._blocked_indexes is ldplayer.blocked_indexes, "旧私有名兼容垫片必须在"
assert cfg.get_safe("no.such.path", 42) == 42, "缺路径回 default"
assert cfg.get_safe("vision.threshold", 0.0) == 0.85, "类型对得上照常取值"
cfg.set("template_categories", "不是字典")          # 坏值（临时 config 内）
assert cfg.get_safe("template_categories", {}) == {}, "类型防御：坏值按没配处理"
assert template_manager.custom_categories() == {}, "custom_categories 走 get_safe 不崩"
cfg.set("template_categories", {"库A": "显示A"})
assert template_manager.custom_categories() == {"库A": "显示A"}
cfg.set("deleted_categories", 5)                    # 坏值：非 list
assert template_manager.deleted_categories() == [], "deleted_categories 走 get_safe 不崩"

# json_store 接线后的 config 写盘链路：save → 重新加载读回
cfg.set("vision.threshold", 0.77)
cfg.save()
assert cm.ConfigManager(cfg.path).get("vision.threshold") == 0.77, "atomic_dump 写盘 round-trip"

# ────────────────────────────────────────────────────────────────────
# ⑥ database：run_log 死表/死函数已删
assert "CREATE TABLE IF NOT EXISTS run_log" not in database.SCHEMA, "建表语句还在"
assert not hasattr(database, "add_run_log"), "add_run_log 还在"

# ────────────────────────────────────────────────────────────────────
# ⑦ run_log：常驻句柄、两行同一文件（临时目录，跨天逻辑=按 %Y%m%d 换名）
rl_dir = _tmp / "logs"
_orig_env, _orig_dir = run_log._disabled, run_log._LOG_DIR
try:
    run_log._disabled = False
    run_log._LOG_DIR = rl_dir
    run_log.log(1, "INFO", "第一行")
    run_log.log(2, "WARN", "第二行")
    files = sorted(rl_dir.glob("run_*.log"))
    assert len(files) == 1, f"应只开一个当天文件：{files}"
    assert files[0].read_text(encoding="utf-8").count("\n") == 2, "两行都应落盘"
    assert run_log._fh is not None, "句柄应常驻（不再逐行 open/close）"
    fh_before = run_log._fh
    run_log.log(3, "INFO", "第三行")
    assert run_log._fh is fh_before, "同一晚连续写不该反复重开句柄"
    run_log._close()
finally:
    run_log._disabled, run_log._LOG_DIR = _orig_env, _orig_dir
    run_log._close()

print("smoke_core_fixes: ALL OK")
