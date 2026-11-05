# 冒烟：单拍识别开销三件套（性能审计 bot 层）
#   ①fsm._tpl_hit：同帧同模板同区域只匹配一次（门+动作/多行撞同图不再双匹配）；
#   ②neighbor_read：同帧同邻框只 OCR 一次（去重身份/读数共用）；
#   ③MultiScanStep 去重识边单拍预算：超预算的点顺延下一拍，验的总数一个不少；
#     预算宽松时行为与旧版逐拍一致（一帧扫完）。
#   ④判断节点多条件单拍预算：超预算的条本拍按未成立跳过（只推迟成立点，不误判死）。
# 红线：离屏不需要（纯 bot 层）；临时配置单例；不打模拟器、不碰真 config。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_watch_budget.py
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import core.config_manager as cm
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


class Ctx:
    def __init__(self):
        self.frame = None
        self.log = None
        self.ld_index = 0
        self.extra = {}
        self.last_hit_rect = None


# ── ① _tpl_hit 同拍缓存（经 template_present/tap_template 公开面测）────
import bot.fsm as _fsm
import core.template_manager as _tm

_tm_real = (_tm.find_template, _tm.load_template_image)
_tm_calls = [0]
_tm.find_template = lambda cat, name: (_tm_calls.__setitem__(0, _tm_calls[0] + 1) or
                                       {"threshold": 0.85, "roi": None})
_tm.load_template_image = lambda entry: np.zeros((10, 10, 3), np.uint8)
_vreal = _fsm.vision


class _V:
    n = 0

    @staticmethod
    def match_template(img, tpl, th, roi=None, rgb_threshold=None):
        _V.n += 1
        return {"x": 10, "y": 20, "score": 0.9, "ms": 1}


_fsm_real = (_fsm.vision, _fsm.ldplayer)
_fsm.vision = _V
_adb = []
_fsm.ldplayer = type("M", (), {"adb_shell": staticmethod(lambda i, cmd: _adb.append(cmd))})
try:
    c = Ctx()
    c.frame = np.zeros((960, 540, 3), np.uint8)
    a = _fsm.template_present(c, "库", "图")
    b = _fsm.template_present(c, "库", "图")
    t = _fsm.tap_template(c, "库", "图")
    check("① 同帧：门+门+动作三问只匹配一次（缓存命中）",
          a and b and t and _V.n == 1 and len(_adb) == 1)
    _fsm.template_present(c, "库", "图", roi=(0, 0, 100, 100))
    check("① roi 不同=不同问题，照常另匹配（键含区域）", _V.n == 2)
    c.frame = np.zeros((960, 540, 3), np.uint8)   # 下一拍新帧对象：缓存自动作废
    _fsm.template_present(c, "库", "图")
    check("① 帧对象一换缓存即废（按帧判鲜不脏读）", _V.n == 3)
finally:
    _fsm.vision, _fsm.ldplayer = _fsm_real
    _tm.find_template, _tm.load_template_image = _tm_real

# ── ② neighbor_read 同拍缓存 ──────────────────────────────────────
from core import ocr as _ocr
from bot.tasks import flow as _flow

_nr_calls = [0]
_ocr.available = lambda: True
_ocr.list_text = lambda img, roi=None, **k: (
    _nr_calls.__setitem__(0, _nr_calls[0] + 1) or
    [{"text": "玩家A", "score": 0.9, "x0": 1, "y0": 1, "x1": 9, "y1": 9}])
c2 = Ctx()
c2.frame = np.zeros((960, 540, 3), np.uint8)
v1 = _flow.neighbor_read(c2, 100, 100, 0, 0, 40, 20)
v2 = _flow.neighbor_read(c2, 100, 100, 0, 0, 40, 20)
check("② 同帧同邻框：两次读数一次 OCR（值相同）",
      _nr_calls[0] == 1 and v1 == v2 and v1[1] == "玩家A")
c2.frame = np.zeros((960, 540, 3), np.uint8)
_flow.neighbor_read(c2, 100, 100, 0, 0, 40, 20)
check("② 换新帧（下一拍）照新再 OCR", _nr_calls[0] == 2)
_flow.neighbor_read(c2, 500, 500, 0, 0, 40, 20)
check("② 换框=换问题，照常另 OCR", _nr_calls[0] == 3)
_ocr.available = lambda: False
check("② OCR 没装：不碰缓存直接 (None,None)",
      _flow.neighbor_read(c2, 100, 100, 0, 0, 40, 20) == (None, None))
_ocr.available = lambda: True

# ── ③ MultiScanStep 去重识边单拍预算 ─────────────────────────────
cm._config.set("vision.ocr_tick_budget_sec", 0.001)   # 预算压到 1ms：每拍只验 1 点

# 采集桩：文字源 6 个命中；识边桩：每点忙等 3ms（Windows sleep(毫秒级)粒度不保，
# 必须 busy-wait 才确定性超预算）且给唯一身份脸N
_ident_calls = [0]


def _slow_ident(ctx, x, y, *b):
    _ident_calls[0] += 1
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.003:
        pass
    return None, f"脸{x}"


_flow.ocr_text_collect = lambda ctx, text, **k: [(i * 10, 800, f"私聊{i}") for i in range(6)]
_flow.neighbor_read = _slow_ident
_tapped = []
_flow._tap = lambda c, x, y: _tapped.append((x, y))

step = _flow.MultiScanStep("遍历", "", "", source="text", text="私聊",
                           mode="tap", dedupe=True, gate_timeout=8.0)
ctx3 = Ctx()
ctx3.log = lambda *a, **k: None
res_seq = []
for _ in range(12):                       # 观察到首次落手：6 点顺延 6 拍 + 收拍点 1 下
    ctx3.frame = np.zeros((960, 540, 3), np.uint8)   # 每拍新帧（缓存跨拍作废）
    res_seq.append(step.tick(ctx3))
    if _tapped:
        break
check("③ 超预算点顺延：一拍验不完，running 若干拍摊平（不阻塞单拍）",
      len(res_seq) >= 6 and all(r == "running" for r in res_seq))
check("③ 顺延不丢点：6 个命中身份全验到（一个不少）", _ident_calls[0] == 6)
check("③ 验完即进入处置：收拍点掉第一处（结果语义不变）",
      _tapped == [(0, 800)])

# 预算宽松 = 旧行为：一帧扫完直接办
cm._config.set("vision.ocr_tick_budget_sec", 10.0)
_flow.neighbor_read = lambda ctx, x, y, *b: (None, f"脸{x}")
_flow.ocr_text_collect = lambda ctx, text, **k: [(i * 10, 800, f"私聊{i}") for i in range(6)]
_tapped2 = []
_flow._tap = lambda c, x, y: _tapped2.append((x, y))
step2 = _flow.MultiScanStep("遍历", "", "", source="text", text="私聊",
                            mode="tap", dedupe=True, gate_timeout=8.0)
ctx4 = Ctx()
ctx4.log = lambda *a, **k: None
ctx4.frame = np.zeros((960, 540, 3), np.uint8)
r2 = step2.tick(ctx4)
check("③ 预算充足：一拍内扫完 6 点并点掉第一处（与旧版一致）",
      r2 == "running" and len(_tapped2) == 1)

# ── ④ 判断节点多条件单拍预算 ─────────────────────────────────────
cm._config.set("vision.click_timeout_scan_budget_sec", 0.001)
_called = [0, 0]


def _mk_slow_branch(logic, slow_idx):
    """造两条件判断节点：条件 j 走桩——j==slow_idx 睡 20ms。"""
    conds = [{"kind": "var", "name": "a"}, {"kind": "var", "name": "b"}]
    node = {"type": "branch", "id": "b1", "logic": logic, "cond": conds[0],
            "extra_conds": [conds[1]]}
    real = _flow._make_condition

    def fake(cd):
        i = 0 if cd["name"] == "a" else 1
        def f(c):
            _called[i] += 1
            if i == slow_idx:
                time.sleep(0.02)
            return True
        return f
    _flow._make_condition = fake
    try:
        cond = _flow._make_branch_condition(node)
    finally:
        _flow._make_condition = real
    return cond


# all：第一条慢（20ms>预算），第二条本拍跳过 → False（旧版会照跑第二条）
_called[:] = [0, 0]
cond_all = _mk_slow_branch("all", slow_idx=0)
r = cond_all(Ctx())
check("④ all 超预算：后条本拍跳过按未成立 → False，且没白跑",
      r is False and _called == [1, 0])
# 预算充足 = 下一拍窗口条件跑完照常成立：成立只被推迟，绝不误判死
cm._config.set("vision.click_timeout_scan_budget_sec", 10.0)
_called[:] = [0, 0]
cond_all_fast = _mk_slow_branch("all", slow_idx=9)   # 谁都不慢
r = cond_all_fast(Ctx())
check("④ 预算充足：all 两条照跑 → True（语义与旧组合逐字等价）",
      r is True and _called == [1, 1])
# any 短路方向：桩恒 True → 首条即真即收，第二条根本不问（跳过语义已在 all 分支验过）
cm._config.set("vision.click_timeout_scan_budget_sec", 0.001)
_called[:] = [0, 0]
cond_any = _mk_slow_branch("any", slow_idx=1)
check("④ any 首条真即收（第二条根本不问，预算无从生效）",
      cond_any(Ctx()) is True and _called == [1, 0])

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke watch-budget ALL OK")
