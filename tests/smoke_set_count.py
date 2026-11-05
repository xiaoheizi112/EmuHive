"""
tests/smoke_set_count.py — 「模拟器多开：台数双向设置」UI 链路回归冒烟

覆盖（改 feature_panel / main_window.on_clone_apply / ldplayer.set_instance_count 后必跑）：
1. 按钮两态：目标≠当前可点（文案「设置」），相等时禁用显示「无需调整」
2. 删台红线：目标 < 当前必弹确认框、点名 victim 与其上账号；答"否"零改动
3. 批次作业 _set_count_job：被删机上绑的账号自动解绑（ld_index=-1，档案保留），
   报告字符串含「删除 N 台 [..]」「解绑账号 N 个」「当前 X 台」
4. 补台方向不弹框（只增不减无风险）
5. set_instance_count 收敛：假台数表从 5 台设到 3 台，只删尾部 5、4，母体 0 幸存

⚠ 隔离：临时库 + 临时配置单例 + 打桩 ldplayer 枚举函数（不真跑 ldconsole）。
⚠ QMessageBox.question 必须打桩（offscreen 模态框永久阻塞的实测坑）。

跑法：python tests/smoke_set_count.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication.instance() or QApplication([])

_tmpdir = tempfile.mkdtemp()

# ── 隔离：临时库 + 临时配置单例（冒烟写盘陷阱同款纪律）──
from core import database, ldplayer
database.DB_PATH = Path(_tmpdir) / "sc.db"
database.init_db()
import core.config_manager as cm
cm._config = cm.ConfigManager(str(Path(_tmpdir) / "config.json"))


class _Inst:
    """最小 InstanceInfo 替身：pid/窗口句柄/安卓标志都可按台调
    （并行关闭与就绪盯梢要判定这三样，台数与标题不够用）"""
    def __init__(self, index, pid=0, hwnd=0, android=0):
        self.index = index
        self.title = f"雷电模拟器{index}"
        self.top_hwnd = self.bind_hwnd = hwnd
        self.android_started = android
        self.pid = pid
        self.extra = 0

    @property
    def running(self):
        return self.pid > 0

    @property
    def android_ready(self):
        return self.android_started == 1


_STATE: list[int] = [1, 2, 3, 4, 5]      # 假雷电现有台（不含母体 0，卡点已排除）
_LIVE: dict[int, dict] = {}              # index -> {"pid":..,"hwnd":..,"android":..}
                                         # 缺省 = 这台停着；测试按段改写


def _fake_list():
    return [_Inst(i, **_LIVE.get(i, {})) for i in _STATE]


ldplayer.list_instances = _fake_list      # 界面侧「当前台数」
ldplayer.refresh_instances = _fake_list   # set_instance_count 逐台复核
ldplayer._blocked_indexes = lambda: set()
ldplayer.clone_instance = lambda from_index=0: _STATE.append(
    (max(_STATE) + 1) if _STATE else 1)   # 假克隆：编号往后排一位
ldplayer.remove_instance = lambda index: _STATE.remove(index)
ldplayer.quit_now = lambda index, **k: True

ok = True


def check(name, cond):
    global ok
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok = ok and bool(cond)


# ── 1. 按钮两态（feature_panel，不经主窗口）──
from ui.feature_panel import FeaturePanel
fp = FeaturePanel()
fp.spn_count.setValue(6)
fp.refresh_count(5)
check("目标 6 ≠ 当前 5 → 可点、文案「设置」",
      fp.btn_clone.isEnabled() and fp.btn_clone.text() == "设置")
fp.refresh_count(6)
check("目标 = 当前 → 禁用、文案「无需调整」",
      not fp.btn_clone.isEnabled() and fp.btn_clone.text() == "无需调整")
fp.refresh_count(8)
check("目标 6 < 当前 8（删台方向）→ 也可点（双向设置）",
      fp.btn_clone.isEnabled() and fp.btn_clone.text() == "设置")

# 1b. 半截输入不被定时器顶回（用户报"改了数字又跳回去"的回归）。
#     keyboardTracking=False：敲的字只在编辑框里，valueChanged 要等失焦才发，
#     半截输入期间 touched 还是 False —— 旧代码的 refresh_count 会把数字顶回
#     默认值（当前+1）。新守卫：聚焦期间不碰输入框。
fp._clone_touched = False
fp.refresh_count(4)
check("没手改、没聚焦 → 自动顶到当前+1（默认值逻辑没被改坏）",
      fp.spn_count.value() == 5)
fp.show()
fp.spn_count.setFocus(Qt.FocusReason.OtherFocusReason)
app.processEvents()
check("offscreen 下聚焦能生效", fp.spn_count.hasFocus())
fp.spn_count.lineEdit().setText("9")      # 模拟敲到一半：没回车/没失焦
fp.refresh_count(7)
check("聚焦输入中 → 值不被顶回默认 8", fp.spn_count.value() == 5)
check("聚焦输入中 → 敲到一半的 9 原样留在编辑框",
      fp.spn_count.lineEdit().text() == "9")
check("聚焦输入中 → 「当前 X 台」照样回填", "当前 7 台" in fp.lbl_cur.text())
fp.spn_count.clearFocus()
app.processEvents()
check("失焦提交 9 且 touched 锁定", fp.spn_count.value() == 9 and fp._clone_touched)
fp.refresh_count(4)
check("提交后再刷新也不覆盖", fp.spn_count.value() == 9)

# ── 2. 删台确认框：点名 victim + 账号，答否零改动 ──
from ui.main_window import MainWindow
mw = MainWindow()
_captured = []
mw._spawn_batch = lambda jobs, *a, **k: _captured.append(jobs)
database.add_account(ld_index=5, name="尾机号", account="vm5", server="一区")
database.add_account(ld_index=3, name="保留机", account="keep3", server="二区")

_asked = {}


def _stub_no(_parent, title, text, *a, **k):   # 第 1 位是父窗口，打桩必须接住
    _asked.update(title=title, text=text)
    return QMessageBox.No


QMessageBox.question = staticmethod(_stub_no)
mw.on_clone_apply(3)                        # 当前 5 → 目标 3：删 5、4
check("删台方向弹了确认框", "确认删除" in _asked.get("title", ""))
check("框内点名实例 5、4 与 vm5 解绑预告",
      "实例 5" in _asked["text"] and "实例 4" in _asked["text"]
      and "vm5" in _asked["text"])
check("保留机 3 不进删除名单", "实例 3" not in _asked["text"])
check("答否 → 没提交批次", not _captured)
check("答否 → vm5 绑定原样",
      database.find_account_by_login("vm5")["ld_index"] == 5)

# ── 3. 答是 → 提交批次；作业真调 set_instance_count ──
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
mw.on_clone_apply(3)
check("答是 → 提交了批次", bool(_captured))
desc, job, *jargs = _captured[-1][0]
check(f"批次条目名「模拟器多开」（实际：{desc}）", desc == "模拟器多开")
report = job(*jargs)
check(f"假雷电收台到 3 台（剩 {_STATE}）", _STATE == [1, 2, 3])
check(f"作业报「删除 2 台 [5, 4]」（实际：{report}）", "删除 2 台 [5, 4]" in report)
check(f"报告含终态「当前 3 台」（实际：{report}）", "当前 3 台" in report)
check(f"报告含「解绑账号 1 个」（实际：{report}）", "解绑账号 1 个" in report)
a5 = database.find_account_by_login("vm5")
check(f"被删机账号解绑 ld_index={a5['ld_index']} 且档案保留",
      a5["ld_index"] == -1 and a5["server"] == "一区")
check("未挨删的机绑定不动",
      database.find_account_by_login("keep3")["ld_index"] == 3)

# ── 4. 补台方向不弹框、直接跑 ──
_asked.clear()
_captured.clear()
mw.on_clone_apply(5)                        # 当前 3 → 目标 5：只增
check("补台不弹确认框", not _asked and bool(_captured))
_report2 = _captured[-1][0][1](*_captured[-1][0][2:])
check(f"补台报「新建 2 台」（实际：{_report2}）", "新建 2 台" in _report2)
check("母体 0 全程不进台数", 0 not in _STATE)

# ── 5. set_instance_count 收敛与错误口径（不经 UI，直调 core）──
_PROGRESS = []
made, removed, err = ldplayer.set_instance_count(4, on_progress=_PROGRESS.append)
check(f"5→4 只删最大号：made={made} removed={removed} err={err}",
      made == 0 and removed == [5] and err is None)
made, removed, err = ldplayer.set_instance_count(4)
check(f"已相等 → 零动作（made={made} removed={removed} err={err}）",
      made == 0 and removed == [] and err is None)
check("on_progress 逐台报了进度", any("正在删除实例 5" in s for s in _PROGRESS))


ldplayer.remove_instance = lambda index: None      # 假删：台数纹丝不动
made, removed, err = ldplayer.set_instance_count(2)
check(f"remove 不生效时报错停手（err={err}）",
      err is not None and "remove 未删除" in err and removed == [])
ldplayer.remove_instance = lambda index: _STATE.remove(index)

# ── 6. 源码级护栏：确认框默认按钮 = No（offscreen 桩吞掉了真值，只能查码）
#    2026-10-09 起 Yes|No 模板收进 ui/confirm.confirm 单一出口：默认 No 的写法
#    在 confirm.py，主窗口三处破坏性确认（重置/封号/删模拟器）都须走它、
#    且不许传 default_no=False 把默认钮翻成 Yes。──
_root = Path(__file__).resolve().parents[1]
_txt = (_root / "ui" / "main_window.py").read_text(encoding="utf-8")
_conf = (_root / "ui" / "confirm.py").read_text(encoding="utf-8")
check("确认框默认按钮是 No（红线纪律，出口 ui/confirm.py）",
      "QMessageBox.Yes | QMessageBox.No," in _conf
      and "QMessageBox.No if default_no else QMessageBox.Yes" in _conf
      and "default_no=False" not in _txt and "default_no = False" not in _txt)

# ── 7. 「全部启动」口径（2026-09-24 用户规则）：列表框里有数据的账号行
#       都是启动对象；开不了的必须点名报原因，不能只留一行易被无视的日志。
#    此刻台=[1,2,3,4]，账号 keep3→3（可开）、vm5→-1（未绑）。再加一台幽灵。
#    2026-09-25 改秒回版：批次只装「并发统一分辨率 + 下达 launch」，
#    等就绪移交给界面定时器 _watch_ready（不再占批次锁按钮）。
_captured.clear()
mw._ready_pending.clear()
_logs = []
mw.log_panel.log = lambda msg, lvl="INFO": _logs.append(str(msg))
database.add_account(ld_index=99, name="幽灵台", account="ghost99")
mw.batch_start_all()
_descs = [d for d, *_ in _captured[-1]]
check("keep3 绑的台 3 存在 → 分辨率打包一条+下达启动",
      any(d.startswith("统一") for d in _descs)
      and any("启动实例 3" in d for d in _descs))
check("等就绪已移出批次（descs 里不该再有「等待实例」）",
      not any("等待实例" in d for d in _descs))
check("台 3 已登记进就绪盯梢", 3 in mw._ready_pending)
check("未绑账号 vm5 点名（序号未绑）",
      any("vm5(序号未绑)" in m for m in _logs))
check("绑了不存在台号的 ghost99 点名（序号99）",
      any("ghost99(序号99)" in m for m in _logs))
check("正常下达也有状态栏回声（不止日志）",
      "已下达" in mw.statusBar().currentMessage())
# 全无可启动：所有账号都摘掉有效台号 → 空批次 + 状态栏大字报
for a in database.list_accounts():
    database.update_account(a["id"], ld_index=-1)
_captured.clear()
mw._ready_pending.clear()
mw.batch_start_all()
check("一台都开不了 → 不再偷偷返回，状态栏明说",
      not _captured and "没有可启动的账号行" in mw.statusBar().currentMessage())
check("空口径时也不登记就绪盯梢", not mw._ready_pending)

# ── 8. 秒回三件套（2026-09-25）：并行关闭 / 两级就绪 / 关闭永不上锁 ──
_arranged = []
ldplayer.restore_native_window = lambda i: _arranged.append(("native", i))
ldplayer.cascade_window = lambda i: _arranged.append(("cascade", i))

# 8a. 两级就绪：窗口出现先摆位（不等安卓标志），安卓就绪再报可挂机
mw._ready_pending.clear()
mw._watch_ready([3])
_LIVE[3] = {"pid": 303, "hwnd": 55, "android": 0}
mw._tick_ready_watch()
check("窗口出现即摆位+播报（安卓标志没翻也摆）",
      ("native", 3) in _arranged and ("cascade", 3) in _arranged
      and any("窗口已出现" in m for m in _logs or []) and 3 in mw._ready_pending)
_logs.clear()
_LIVE[3] = {"pid": 303, "hwnd": 55, "android": 1}
mw._tick_ready_watch()
check("安卓标志翻了 → 报可挂机并收账",
      any("安卓就绪" in m for m in _logs) and 3 not in mw._ready_pending)
# 窗口消失 = 死亡检测：不再干等 150 秒
_logs.clear()
mw._watch_ready([2])
_LIVE[2] = {"pid": 202, "hwnd": 66}
mw._tick_ready_watch()
_LIVE.pop(2)
mw._tick_ready_watch()
check("曾出现又消失 → 立刻收账不等满 150 秒",
      2 not in mw._ready_pending and any("窗口消失" in m for m in _logs))
mw._ready_pending.clear()
mw._ready_timer.stop()

# 8b. 并行关闭：quit 命令一次全发；graceful 内退掉 = 零 taskkill；
#     赖着不走的 = taskkill 补刀后确认
import subprocess as _sp
_quits, _kills = [], []


class _FakePopen:
    def __init__(self, args, **k):
        _quits.append(args)
        self.pid = 70000 + len(_quits)

    def poll(self):
        return 0


# 快照可控：stubborn 翻 False 前，台 4 一直报 pid=404（赖着不走）
_STUBBORN = {"4": False}


def _snap_stub():
    out = []
    for i in _STATE:
        if i == 4 and _STUBBORN["4"]:
            out.append(_Inst(4, pid=404))
        else:
            out.append(_Inst(i, **_LIVE.get(i, {})))
    return out


def _fake_run(args, **k):
    if args[0] == "taskkill" and "404" in map(str, args):
        _STUBBORN["4"] = False      # 补刀生效：下一轮快照它就没了
    if args[0] == "taskkill":
        _kills.append(args)

    class _R:
        returncode = 0
    return _R()


_real_popen, _real_run = _sp.Popen, _sp.run
_rl, _rf = ldplayer.list_instances, ldplayer.refresh_instances
try:
    _sp.Popen = _FakePopen
    _sp.run = _fake_run
    ldplayer.list_instances = ldplayer.refresh_instances = _snap_stub
    # 场景一：两台都在 graceful 期自然退（快照里都没有活 pid）
    _LIVE.clear()
    report = ldplayer.quit_indexes_now([3, 4], graceful_wait=0.6, kill_wait=0.6)
    check(f"quit 命令两条一次性全发（实收 {len(_quits)} 条）",
          len(_quits) == 2 and all(a[1] == "quit" for a in _quits))
    check(f"graceful 期全退净 = 零 taskkill：{report}",
          "已关闭 2 台" in report and not _kills)
    # 场景二：台 4 赖着不走 → taskkill 补刀 → 确认退
    _quits.clear()
    _STUBBORN["4"] = True
    report = ldplayer.quit_indexes_now([3, 4], graceful_wait=0.5, kill_wait=0.6)
    check(f"赖着不走的台被 taskkill 补刀：{report}",
          _kills and "404" in map(str, _kills[-1]) and "硬杀 1 台" in report
          and "已关闭 2 台" in report)
finally:
    _sp.Popen, _sp.run = _real_popen, _real_run
    ldplayer.list_instances, ldplayer.refresh_instances = _rl, _rf

# 8c. batch_quit_all：只挑运行中的台、一条并行作业
#     （抢占入口也要截胡——不然真起线程对真机发 quit 命令）
mw._spawn_batch = lambda jobs, *a, **k: _captured.append(jobs)
_captured.clear()
_logs.clear()
_LIVE.clear()
_LIVE.update({2: {"pid": 202}, 4: {"pid": 404, "hwnd": 9}})
mw.batch_quit_all()
_qdescs = [d for d, *_ in _captured[-1]]
check(f"关闭批次一条打包作业（实收 {_qdescs}）",
      len(_qdescs) == 1 and "并行关闭 2 台" in _qdescs[0])
_fn = _captured[-1][0][1]   # partial(quit_indexes_now, idxs)
check("打包作业点名 2、4 两台",
      getattr(_fn, "args", None) and list(_fn.args[0]) == [2, 4])
_LIVE.clear()
_captured.clear()
mw.batch_quit_all()
check("没有运行中的台 → 直说并状态栏回声",
      not _captured and "没有运行中的实例" in mw.statusBar().currentMessage())

# 8d. 上锁口径：批次忙时「全部关闭」永不上锁（抢占通道），启动/刷新才锁
mw._set_busy(True)
check("批次在跑：全部关闭仍可点", mw.btn_quit_all.isEnabled())
check("批次在跑：全部启动/刷新锁住",
      not mw.btn_start_all.isEnabled() and not mw.btn_refresh.isEnabled())
mw._set_busy(False)
check("空闲：全部启动/刷新回魂",
      mw.btn_start_all.isEnabled() and mw.btn_refresh.isEnabled())

print("ALL OK" if ok else "存在 FAIL")
sys.exit(0 if ok else 1)
