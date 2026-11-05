"""
tests/smoke_drop_import.py — 拖 TXT 进超级列表框 → 导入 → 「账号」列亮起来 的回归冒烟

覆盖三块逻辑（改动 account_panel / main_window 拖入侧 / account_io 任何一处后必跑）：
1. 拖放文件筛选：只认 txt、csv 及其它后缀一律拒（2026-09 收成只吃 txt）、非文件拖放拒
2. 导入作业：MainWindow._import_accounts_job 走真 account_io，报告字符串进日志。
   序号口径 = 雷电模拟器编号直通零换算，但从 1 起：0 号母体在
   ldplayer._list_instances_raw 单一卡点直接排除（表格/补位/批量全看不见它），
   文件写 0 视同没填走补位；不做重号/撞车/超范围检查（用户定调：导入即完成任务），
   联合键 upsert 保多开不堆重复
3. 账号列渲染：panel.refresh 拿到 accounts_by_instance() 后直通显示 + 同名多开琥珀高亮

⚠ 隔离：全程临时库（database.DB_PATH 换掉）+ 假 list_instances（1~8 共 8 台，
   另有一处直接喂 list2 原文验母体排除卡点），绝不碰真实 data/app.db 和真雷电。

跑法：python tests/smoke_drop_import.py
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

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from core import database, ldplayer
from ui.account_panel import AccountPanel
from ui.main_window import MainWindow

# ── 隔离 1：临时库 ──
_tmpdir = tempfile.mkdtemp()
database.DB_PATH = Path(_tmpdir) / "smoke.db"
database.init_db()
# 覆盖式导入的备份目录也沙箱化（_backup_accounts 调时现读 PROJECT_ROOT，
# 冒烟不 patch 就会每次跑往真实 data/backups/ 堆一份备份文件）
import core.config_manager as _cm
_cm.PROJECT_ROOT = Path(_tmpdir)

# ── 隔离 2：假实例（只留本冒烟用到的字段；1~8 共 8 台。0 号母体已在
#    ldplayer._list_instances_raw 卡点排除，fake 直接按排除后的口径给）──
class _Inst:
    top_hwnd = bind_hwnd = 123
    android_started = 1
    pid = 456
    extra = 0
    def __init__(self, index):
        self.index = index
        self.title = f"雷电模拟器{index}"
    @property
    def running(self): return True
    @property
    def android_ready(self): return True

ldplayer.list_instances = lambda: [_Inst(i) for i in range(1, 9)]
ldplayer._blocked_indexes = lambda: set()

ok = True
def check(name, cond):
    global ok
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok = ok and bool(cond)

# 先验卡点本身：list2 原文含 0 号母体，进 _list_instances_raw 后必须消失
import subprocess as _sp
_real_run = ldplayer._run
ldplayer._run = lambda args, timeout=60: _sp.CompletedProcess(
    args, 0, "0,母体,0,0,1,1,0\n1,分身1,0,0,1,2,0\n2,分身2,0,0,1,3,0\n", "")
_raw = [i.index for i in ldplayer._list_instances_raw()]
ldplayer._run = _real_run
check(f"卡点排除母体：list2 有 0 也不加载（实际 {_raw}）", _raw == [1, 2])

# ── 1. 拖放文件筛选 ──
class _Ev:
    def __init__(self, mime): self._m = mime
    def mimeData(self): return self._m

def _mime(paths=None, text=None):
    m = QMimeData()
    if paths:
        m.setUrls([QUrl.fromLocalFile(p) for p in paths])
    if text:
        m.setText(text)
    return m

check("csv 后缀拒（只收 txt）", AccountPanel._drop_file(_Ev(_mime(["D:/x/账号.csv"]))) == "")
check("txt 后缀认（大写也认）", AccountPanel._drop_file(_Ev(_mime(["D:/x/a.TXT"]))) == "D:/x/a.TXT")
check("png 拒", AccountPanel._drop_file(_Ev(_mime(["D:/x/a.png"]))) == "")
check("纯文本拖放拒（不是文件）", AccountPanel._drop_file(_Ev(_mime(text="abc,pw"))) == "")
check("多文件只认第一个 txt",
      AccountPanel._drop_file(_Ev(_mime(["D:/x/a.png", "D:/x/b.txt"]))) == "D:/x/b.txt")

# ── 2. 导入作业（真 account_io，临时库）──
# 序号直通口径：文件写 1 = 雷电 1 号机 = 内部 ld_index 1，全链零换算
csv1 = Path(_tmpdir) / "accounts.txt"   # 变量名沿用 csv（指逗号分隔格式本身），文件只收 .txt
csv1.write_text(
    "实例序号,备注名,账号,密码,区服\n"
    "1,大号,abc01,pw1,含沙射影6服\n"
    ",小号,abc02,pw2,新手1区\n",
    encoding="utf-8-sig")   # utf-8-sig = 本程序导出格式，Excel 回灌同款
report = MainWindow._import_accounts_job(str(csv1))
check(f"报告含「新增 2」（实际：{report}）", "新增 2" in report)
accs = {a["account"]: a for a in database.list_accounts()}
check("文件序号 1 → 内部 ld_index 1（直通零换算）", accs["abc01"]["ld_index"] == 1)
check("abc02 序号留空 → 自动补位且不碰母体 0（实际：{}）".format(accs["abc02"]["ld_index"]),
      accs["abc02"]["ld_index"] == 2)   # 1 已被 abc01 占、0 是母体 → 落 2
# 重导同一份：全更新零新增（upsert 不堆重复行）
report2 = MainWindow._import_accounts_job(str(csv1))
check(f"重导=更新不新增（实际：{report2}）", "新增 0" in report2 and "更新 2" in report2)

# ── 2b. 分隔符判定（记事本中文输入法敲的是全角逗号「，」，必须认）──
csv2 = Path(_tmpdir) / "fullwidth.txt"
csv2.write_text(
    "实例序号，备注名，账号，密码，区服\n"
    "1，全角号，fw01，pw，含沙射影6服\n",
    encoding="utf-8")          # 记事本默认：无 BOM 的 UTF-8
# replace=False：这份"小抄"只验分隔符，不触发覆盖删除（会清掉 2 里的 abc01/abc02）
from core import account_io as _aio
_aio.import_accounts(str(csv2), replace=False)
fw = database.find_account_by_login("fw01")
check("全角逗号可读", fw and fw["name"] == "全角号" and fw["server"] == "含沙射影6服")
# 制表符（从 Excel 复制粘贴存 txt）
csv3 = Path(_tmpdir) / "tab.txt"
csv3.write_text("实例序号\t备注名\t账号\n\t制表号\tts01\n", encoding="utf-8")
_aio.import_accounts(str(csv3), replace=False)
check("制表符分隔可读", database.find_account_by_login("ts01") is not None)

# ── 2c. 覆盖式导入：文件没写的账号被删 + 删前备份可还原 ──
# 先攒一个"脏库"：abc01/abc02 之外再塞一条 test01（模拟历史污染源）。
# ld_index=-1 表示"没绑机"；还原链路（备份→拖回）必须零拒绝才算合格后悔药
database.add_account(ld_index=-1, name="测试账号", account="test01", server="一区")
check("覆盖前库里有 test01", database.find_account_by_login("test01") is not None)
csv5 = Path(_tmpdir) / "only1.txt"
csv5.write_text("序号,备注,账号,区服\n1,大号,abc01,含沙射影6服\n", encoding="utf-8")
res5 = _aio.import_accounts(str(csv5))          # 直接拿字典，备份路径不用解析字符串
rep5 = "；".join(t for _l, t in _aio.format_import_report(res5))
check(f"覆盖式：test01 被删（实际：{rep5}）",
      database.find_account_by_login("test01") is None)
check("文件里有的 abc01 保住", database.find_account_by_login("abc01") is not None)
check("abc02 不在文件里 → 也被删", database.find_account_by_login("abc02") is None)
check("报告点了被删账号", "test01" in rep5 and "删除" in rep5)
_bak = Path(res5["backup"] or "")
check(f"备份文件真实存在（{res5['backup']}）", _bak.exists())
database.DB_PATH = Path(_tmpdir) / "restore.db"; database.init_db()   # 换空库模拟全丢
_aio.import_accounts(_bak)                                          # 备份里含 abc01(1)/abc02(2)/test01(留空)
check("拖回备份即可还原", database.find_account_by_login("test01") is not None
      and database.find_account_by_login("abc02") is not None)
# 整批写库失败（一行都没进）时绝不删库：这是最危险的误删路径
database.DB_PATH = Path(_tmpdir) / "smoke.db"; database.init_db()     # 切回脏前的库
_n_before = len(database.list_accounts())
csv_bad = Path(_tmpdir) / "bad.txt"
csv_bad.write_text("序号,备注,账号,区服\n0,空号,,一区\n", encoding="utf-8")  # 账号列空→跳过=零入库
MainWindow._import_accounts_job(str(csv_bad))
check(f"零入库不删库（{_n_before} → {len(database.list_accounts())}）",
      len(database.list_accounts()) == _n_before)

# ── 2d. 一号多开与"不作检查"的直通语义 ──
database.DB_PATH = Path(_tmpdir) / "dup.db"; database.init_db()
# 同名两行、序号 1/2 → 一号多开：各立一行绑各的机，无警告无拦截（检查已按需求全撤）
dupA = Path(_tmpdir) / "dupA.txt"
dupA.write_text("序号,备注,账号,区服\n1,甲,dp1,一区\n2,甲,dp1,二区\n", encoding="utf-8")
rd = _aio.import_accounts(str(dupA))
rows_dp1 = [a for a in database.list_accounts() if a["account"] == "dp1"]
check(f"一号多开两行都入库（实际 {len(rows_dp1)} 行）", len(rows_dp1) == 2)
check("多开各绑各机（直通序号 1/2）", sorted(a["ld_index"] for a in rows_dp1) == [1, 2])
check("报告不再有 multi_open 键", "multi_open" not in rd)
# 重导同一份多开文件：更新不新增（联合键认人，不堆重复行）
rd_re = _aio.import_accounts(str(dupA))
check(f"重导多开=更新不新增（新增 {rd_re['created']}）", rd_re["created"] == 0 and rd_re["updated"] == 2)
# 表格高亮：dp1 绑了实例 1、2 → 那两行「账号」列琥珀底。母体 0 不在表里，
# 行 0 = 实例 1、行 1 = 实例 2（表格从 1 起步）
panel_dup = AccountPanel()
panel_dup.refresh({}, {}, database.accounts_by_instance())
app.processEvents()
def _bg(r):
    it = panel_dup.item(r, 2)
    return it.background().color().name() if it and it.background().color().isValid() else ""
check(f"高亮：多开两行上色（实际 {_bg(0)}/{_bg(1)}）",
      _bg(0) == "#fbeecb" and _bg(1) == "#fbeecb")
check(f"表格从 1 起步：第一行序号显示 1（实际 {panel_dup.item(0, 0).text()}）",
      panel_dup.item(0, 0).text() == "1")
# 两个不同账号写同一序号 → 不拦，都入库（后写的行在表格里会被同 id 优先者遮蔽，
# 属"照单全收"语义的一部分；数据库不丢数据）
dupB = Path(_tmpdir) / "dupB.txt"
dupB.write_text("序号,备注,账号,区服\n1,甲,dp2,一区\n1,乙,dp3,二区\n", encoding="utf-8")
rd2 = _aio.import_accounts(str(dupB), replace=False)
check("撞车序号照单全收", database.find_account_by_login("dp2") is not None
      and database.find_account_by_login("dp3") is not None)
# 同名同序号出现两次 → 联合键认同一行，原地更新不翻倍
dupE = Path(_tmpdir) / "dupE.txt"
dupE.write_text("序号,备注,账号,区服\n3,甲,dp6,一区\n3,甲改备,dp6,一区\n", encoding="utf-8")
_aio.import_accounts(str(dupE), replace=False)
rows_dp6 = [a for a in database.list_accounts() if a["account"] == "dp6"]
check(f"同名同序号只留一行（实际 {len(rows_dp6)} 行）", len(rows_dp6) == 1)
check(f"后行覆盖前行字段（备注={rows_dp6[0]['name'] if rows_dp6 else None}）",
      rows_dp6 and rows_dp6[0]["name"] == "甲改备")
# 序号超出实际台数（假实例 0~8，写 9）→ 不拦，照存（建好机器后自动显形）
dupC = Path(_tmpdir) / "dupC.txt"
dupC.write_text("序号,备注,账号,区服\n9,飞号,dp4,一区\n", encoding="utf-8")
_aio.import_accounts(str(dupC), replace=False)
dp4 = database.find_account_by_login("dp4")
check(f"超范围序号照收（ld_index={dp4['ld_index'] if dp4 else None}）", dp4 and dp4["ld_index"] == 9)
# 留空自动补位的同名行：按账号名 upsert 保住原行（补位每轮可能落不同序号，
# 联合键会把重导误判成新号留幽灵）——单行同名重导不翻倍
database.DB_PATH = Path(_tmpdir) / "auto.db"; database.init_db()
dupF = Path(_tmpdir) / "dupF.txt"
dupF.write_text("序号,备注,账号,区服\n,甲,dp7,一区\n", encoding="utf-8")
_aio.import_accounts(str(dupF))
_aio.import_accounts(str(dupF))
check("留空重导不翻倍", len([a for a in database.list_accounts()
                             if a["account"] == "dp7"]) == 1)
# ── 2e. 多开收缩为一号一机：同名旧幽灵行必须删净，不许遮掉新导行 ──
# 用户实测报障场景（2026-09-21）：先导 abc02 多开 1~8（备注"小号"），
# 再把文件换成"1~8 一人一号"（备注"123"）重导。新账号只能新建行（id 大），
# abc02@1/3/4…8 七行成了幽灵——accounts_by_instance 同机取最小 id，
# 幽灵 id 小 → 新行被整个遮掉，表格看着"备注和账号名全没改过来"。
# 旧版删档豁免按账号名（abc02 还在文件里→它名下所有行全保），正护住了这批幽灵；
# 现按「本批落过的行 id」豁免，幽灵按机号点名删除。
database.DB_PATH = Path(_tmpdir) / "shrink.db"; database.init_db()
shrinkA = Path(_tmpdir) / "shrinkA.txt"
shrinkA.write_text("序号,备注,账号,区服\n"
                   + "".join(f"{i},小号,abc02,一区\n" for i in range(1, 9)),
                   encoding="utf-8")
_aio.import_accounts(str(shrinkA))                       # abc02 占满 8 台
shrinkB = Path(_tmpdir) / "shrinkB.txt"
shrinkB.write_text("序号,备注,账号,区服\n"
                   + "".join(f"{i},123,abc0{i},二区\n" for i in range(1, 9)),
                   encoding="utf-8")
rsh = _aio.import_accounts(str(shrinkB))                 # 一号一机重导
rows = database.list_accounts()
check(f"收缩后全库只剩 8 行（实际 {len(rows)}）", len(rows) == 8)
by_inst = database.accounts_by_instance()
check("每台显示文件里的新账号（幽灵不再遮行）",
      all((a := by_inst.get(i)) and a["account"] == f"abc0{i}" and a["name"] == "123"
          for i in range(1, 9)))
check("abc02 只保留联合键对上的序号 2 那行（id 与历史没断）",
      [a["ld_index"] for a in rows if a["account"] == "abc02"] == [2])
check(f"删档报告按机号点名幽灵（实际 {rsh['deleted']}）",
      len(rsh["deleted"]) == 7 and all(x.startswith("abc02@") for x in rsh["deleted"]))
database.DB_PATH = Path(_tmpdir) / "smoke.db"; database.init_db()   # 切回 2c 的库，后续段落接着用

# ── 3. 表格渲染（列序 = 文件列序：档案在前 序号/备注/账号/区服/等级，
#      运行时在后 运行任务/当前动作/完成度/当前模拟器/状态/挂机状态/已运行/资产）──
panel = AccountPanel()
hdr = [panel.horizontalHeaderItem(c).text() for c in range(panel.columnCount())]
check(f"13 列表头与文件同序（实际：{hdr}）", hdr == [
    "序号", "备注", "账号", "区服", "等级", "运行任务",
    "当前动作", "完成度", "当前模拟器", "状态", "挂机状态", "已运行", "资产"])
# 面板列名必须和 account_io.FILE_COLUMNS 一字不差（两处同源，防漂移）
from core import account_io
check("面板列名 == 文件列名", hdr == account_io.HEADERS)
# 2c 切回后库里只有 abc01（绑实例 1）。母体 0 不加载 → 表格第一行（行 0）就是实例 1
panel.refresh({}, {}, database.accounts_by_instance())
cell = lambda r, c: (panel.item(r, c).text() if panel.item(r, c) else None)
check(f"表格从 1 起步：第一行序号显示 1、无 0 号行（实际：{cell(0, 0)}）", cell(0, 0) == "1")
check(f"备注(1)/账号(2)/区服(3)（实际：{cell(0, 1)}/{cell(0, 2)}/{cell(0, 3)}）",
      cell(0, 1) == "大号" and cell(0, 2) == "abc01" and cell(0, 3) == "含沙射影6服")
check(f"没挂机时 运行任务(5)=未运行/完成度(7)=未完成（实际：{cell(0, 5)}/{cell(0, 7)}）",
      cell(0, 5) == "未运行" and cell(0, 7) == "未完成")
# 链跑完的实例：完成度=已完成（progress 里 done 键驱动，链结束后 progress 已清）
panel.refresh({}, {1: {"done": True}}, database.accounts_by_instance())
check(f"done=True → 已完成（实际：{cell(0, 7)}）", cell(0, 7) == "已完成")

# ── 4. 12 列文件（表头=表格列）导入：档案入库、运行时列忽略、"待读取"→空 ──
csv4 = Path(_tmpdir) / "table12.txt"
csv4.write_text(
    "序，备注，账号，区服，等级，运行任务，当前动作，完成度，当前模拟器，状态，挂机状态，已运行\n"
    "2，图号，tab12，含沙射影9服，待读取，已完成，正在打怪，已完成，雷电模拟器，运行中，运行中，1:00\n",
    encoding="utf-8")
MainWindow._import_accounts_job(str(csv4))
a = database.find_account_by_login("tab12")
check(f"档案列入库（备注/区服）", a and a["name"] == "图号" and a["server"] == "含沙射影9服")
check(f"等级「待读取」→ 0（实际：{a['level'] if a else None}）", a and a["level"] == 0)
check(f"文件序=2 → 内部直绑实例 2（实际：{a['ld_index'] if a else None}）", a and a["ld_index"] == 2)
# 运行时列的字面值绝不进 current_task/status（否则表格显示假状态）
check(f"运行时列忽略：current_task 未被「已完成」污染（实际：{a['current_task'] if a else None}）",
      a and a["current_task"] in ("", None))

print("ALL OK" if ok else "存在 FAIL")
sys.exit(0 if ok else 1)
