"""
tests/smoke_cell_edit.py — 超级列表框双击原地改档案 的回归冒烟

覆盖（改动 account_panel 编辑链 / main_window.on_account_field_edited 任何一处后必跑）：
1. 可编辑范围：只有"绑了账号的行"的 备注/账号/区服/等级 四列挂了 ItemIsEditable；
   序号列、运行时列、没号的行一律双击无门
2. 提交语义（实测口径）：回车提交走委托 setModelData 截获编辑框文本 →
   cellChanged → account_field_edited(实例index, 字段, 新值)；ESC 撤销什么都不发；
   同值提交/本来无值再"清空" 不发信号（去重）
3. 编辑闸门：编辑框开着时 refresh 只更新快照不重建表格（不顶掉输入框），
   关掉后下一帧正常刷新
4. 主窗口写库链：备注/账号/区服直写；等级必须非负整数，填字母 → WARN +
   格子弹回原值、库里不进脏数；等级清空=0、区服清空=""，显示回落"—"

⚠ 隔离：全程临时库（database.DB_PATH 换掉）+ 假 list_instances（实例1有号、
   实例2没号），绝不碰真实 data/app.db 和真雷电。

跑法：python tests/smoke_cell_edit.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QLineEdit,
                               QMessageBox)

app = QApplication.instance() or QApplication([])
# offscreen 模态框必桩（本链理论不弹窗，防御性照惯例来一发）
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

from core import database, ldplayer
from ui.account_panel import AccountPanel
from ui.main_window import MainWindow

# ── 隔离 1：临时库 ──
_tmpdir = tempfile.mkdtemp()
database.DB_PATH = Path(_tmpdir) / "cell.db"
database.init_db()

# ── 隔离 2：假实例（1 号有账号、2 号空行）──
class _Inst:
    def __init__(self, index):
        self.index = index
        self.title = f"雷电模拟器{index}"
    @property
    def running(self): return True
    @property
    def android_ready(self): return True

ldplayer.list_instances = lambda: [_Inst(1), _Inst(2)]
ldplayer._blocked_indexes = lambda: set()

database.add_account(ld_index=1, name="大号", account="abc01",
                     server="含沙射影6服", level=5)

ok = True
def check(name, cond):
    global ok
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok = ok and bool(cond)

# ── 主窗口替身：真 handler（MainWindow.on_account_field_edited 原函数）挂在
#    假 self 上跑——不构造整套 MainWindow，但写库/校验/驳回逻辑一行不改地过。
#    refresh 仿真实：计数 + 让面板按最新库值重画──
class _Log:
    def __init__(self): self.lines = []
    def log(self, msg, level="INFO"): self.lines.append((level, msg))

class _MW:
    _FIELD_LABELS = MainWindow._FIELD_LABELS
    def __init__(self, panel):
        self.panel = panel
        self.log_panel = _Log()
        self.refresh_calls = 0
    def refresh(self):
        self.refresh_calls += 1
        self.panel.refresh({}, {}, database.accounts_by_instance())

panel = AccountPanel()
panel.show()          # editItem 对隐藏控件一律返回 False（实测），offscreen 也得先亮出来
app.processEvents()
mw = _MW(panel)
emitted: list[tuple[int, str, str]] = []
panel.account_field_edited.connect(
    lambda i, f, t: (emitted.append((i, f, t)),
                     MainWindow.on_account_field_edited(mw, i, f, t)))

def cell(r, c):
    it = panel.item(r, c)
    return it.text() if it else None

def editable(r, c):
    it = panel.item(r, c)
    return bool(it and it.flags() & Qt.ItemFlag.ItemIsEditable)

def _editors():
    # 实测坑：closeEditor 后旧编辑框只是 hide（deleteLater 在 offscreen 不即时跑），
    # findChildren 照样翻得出来——必须按可见性滤，不然第二次编辑抓到旧框
    return [w for w in panel.findChildren(QLineEdit)
            if w.parentWidget() is panel.viewport() and w.isVisible()]

def do_edit(r, c, new_text, commit=True):
    """开编辑框（= 双击进门）→ 填字 → 回车提交 / ESC 撤销。
    实测坑：PySide6 6.11 的 editItem 返回 None（void），开没开要看编辑框"""
    panel.editItem(panel.item(r, c))
    eds = _editors()
    assert eds, f"({r},{c}) 编辑门没开"
    eds[0].setText(new_text)
    QTest.keyClick(eds[0], Qt.Key_Escape if not commit else Qt.Key_Return)
    # 转两圈：第一圈走提交链，第二圈才轮到 handler 里 singleShot(0) 的延迟刷新
    app.processEvents()
    app.processEvents()

# ── 1. 可编辑范围 ──
panel.refresh({}, {}, database.accounts_by_instance())
check("有号行 备注/账号/区服/等级(1-4) 可编辑", all(editable(0, c) for c in (1, 2, 3, 4)))
check("序号列(0)不可编辑（改绑定关系代价大，门不开）", not editable(0, 0))
check("运行时列(5-12)不可编辑（3秒就被顶掉，改了白改）",
      not any(editable(0, c) for c in range(5, 13)))
check("没号的行整行不可编辑", not any(editable(1, c) for c in range(13)))
panel.editItem(panel.item(1, 1))
check("无号行硬叫 editItem 也开不出编辑框（flags 挡门）", not _editors())

# ── 2. 提交语义 ──
do_edit(0, 2, "abc01")                  # 同值回车
check("同值提交不发信号（去重）", not emitted)
do_edit(0, 2, "abc01", commit=False)    # ESC 撤销
check("ESC 撤销什么都不发、格字不变", not emitted and cell(0, 2) == "abc01")
do_edit(0, 1, "改备注")
check(f"回车提交发 (1, name, 改备注)（实际：{emitted}）",
      emitted == [(1, "name", "改备注")])
acc1 = lambda: database.accounts_by_instance()[1]
check(f"主窗口已写库 name=改备注（实际：{acc1()['name']}）", acc1()["name"] == "改备注")
check(f"成功提交计一次 refresh（实际 {mw.refresh_calls}）", mw.refresh_calls == 1)

# ── 3. 等级校验：坏值驳回 + 弹回 ──
do_edit(0, 4, "abc")
check(f"坏等级也发了信号（面板只管报，拦在写库前）（实际：{emitted[-1]}）",
      emitted[-1] == (1, "level", "abc"))
warns = [m for lv, m in mw.log_panel.lines if lv == "WARN"]
check(f"等级非数字 → WARN 日志（实际：{warns}）",
      any("等级只能填数字" in m for m in warns))
check(f"驳回后格子弹回原值 5（模型里一度是 abc，实际：{cell(0, 4)}）", cell(0, 4) == "5")
check(f"库里等级没被脏写（实际：{acc1()['level']}）", acc1()["level"] == 5)
do_edit(0, 4, "-3")
check("负等级同样驳回（WARN）",
      any("负数" in m for lv, m in mw.log_panel.lines if lv == "WARN")
      and acc1()["level"] == 5)
do_edit(0, 4, "42")
check(f"合法等级入库（实际：{acc1()['level']}）", acc1()["level"] == 42)
do_edit(0, 4, "")
check("等级清空=0（实际：{}）".format(acc1()["level"]), acc1()["level"] == 0)
check("0 等级显示回 —", cell(0, 4) == "—")

# ── 3b. "—" 占位与清空：无值格再"清空"提交 = 本来就没值，去重不发 ──
n_emit = len(emitted)
do_edit(0, 3, "")                  # 区服有值 → 清空：该发
check(f"区服清空发 ('',)（实际：{emitted[-1]}）", emitted[-1] == (1, "server", ""))
check("库中区服为空", acc1()["server"] in ("", None))
check("清空后显示 —", cell(0, 3) == "—")
do_edit(0, 3, "")                  # 本来就是空 → 再"清空"一次：不该发
check("无值格重复清空不发信号", len(emitted) == n_emit + 1)

# ── 4. 编辑中 refresh 闸门（模拟主窗口 3 秒定时器撞进来）──
panel.editItem(panel.item(0, 1))
eds = _editors()
assert len(eds) == 1, f"第4段该恰好一个编辑框，实际 {len(eds)}"
eds[0].setText("字敲一半")
check("确实在编辑态", panel.state() == QAbstractItemView.State.EditingState)
_snapshot_before = panel._accounts[1]["name"]
panel.refresh({}, {}, {1: {**acc1(), "name": "外来户"}})   # 新数据但别想重建
eds2 = _editors()
check(f"refresh 被跳过：编辑框还活着（实际 {len(eds2)} 个）",
      len(eds2) == 1 and eds2[0].text() == "字敲一半")
check("refresh 被跳过：格字没被顶掉", cell(0, 1) == "改备注")
check("但快照已更新（去重按最新值判）",
      panel._accounts[1]["name"] == "外来户" and _snapshot_before == "改备注")
QTest.keyClick(eds2[0], Qt.Key_Return)
app.processEvents()
check(f"编辑照常提交（敲的字 vs 快照「外来户」算变化）（实际：{emitted[-1]}）",
      emitted[-1] == (1, "name", "字敲一半"))
check("关掉编辑后 refresh 恢复重建", cell(0, 1) == "字敲一半")

# ── 5. 未绑定行的提交兜底（门都开不了，防御性直接呼 handler）──
emitted.clear(); mw.log_panel.lines.clear()
MainWindow.on_account_field_edited(mw, 2, "name", "幽灵改")
check(f"没绑账号的行改档 → WARN（实际：{mw.log_panel.lines}）",
      any(lv == "WARN" for lv, _ in mw.log_panel.lines)
      and 2 not in database.accounts_by_instance())

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
