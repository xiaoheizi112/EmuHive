"""
tests/smoke_asset_tab.py — 资产管理页签 + 资产总览表 常驻自检（离屏，临时库/配置隔离）

覆盖：
- AssetPanel 动态列（资产名并集排序）、缩写、未绑号行、封号红字
- 列集合没变不重建（用户拖好的列宽不被 3 秒刷新顶掉）
- 列宽按列名跨"新增资产列"保住；restore 早于首刷的攥账路径
- MainWindow 页签：默认账号管理、翻页、关窗落 ui.asset_layout
跑法：QT_QPA_PLATFORM 由脚本自设。python tests/smoke_asset_tab.py
⚠ 冒烟写盘陷阱同款纪律：database.DB_PATH 与 cm._config 先换临时再碰任何 save。
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

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication.instance() or QApplication([])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)  # offscreen 模态坑

_tmpdir = tempfile.mkdtemp()
from core import database, ldplayer
database.DB_PATH = Path(_tmpdir) / "at.db"
database.init_db()
import core.config_manager as cm
cm._config = cm.ConfigManager(str(Path(_tmpdir) / "config.json"))


class _Inst:
    def __init__(self, index, running=False):
        self.index = index
        self.title = f"雷电模拟器{index}"
        self.top_hwnd = self.bind_hwnd = 0
        self.android_started = 0
        self.running = running
        self.extra = 0

    @property
    def android_ready(self):
        return False


ldplayer.list_instances = lambda: [_Inst(1, True)]
ldplayer.refresh_instances = ldplayer.list_instances
ldplayer._blocked_indexes = lambda: set()

ok = True


def check(name, cond):
    global ok
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok = ok and bool(cond)


# ── 备账：一号有账、孤岛号绑 5（不在册）、废号封号没账、未绑号 -1 ──
a1 = database.add_account(1, account="一号", name="打米机")
a2 = database.add_account(5, account="孤岛号")           # 没绑到在册实例
a3 = database.add_account(2, account="废号")
database.add_account(-1, account="未绑号")               # 暂不绑定实例
database.set_banned(a3, True, reason="测试")
database.merge_account_assets(a1, {"gold": 1234567, "强化石": 88}, {"gold": "金币"})
database.merge_account_assets(a2, {"强化石": 9000})

from ui.asset_panel import AssetPanel

ap = AssetPanel()
ap.refresh(database.list_accounts())

heads = [ap.horizontalHeaderItem(c).text() for c in range(ap.columnCount())]
check("列 = 固定头 + 资产名排序并集 + 记账尾列",
      heads == ["序号", "账号", "备注", "区服", "强化石", "金币", "最后记账"])
check("行数 = 账号全表（4 号，含未绑/封号）", ap.rowCount() == 4)


def cell(r, c):
    it = ap.item(r, c)
    return it.text() if it else None


# list_accounts 按 id 升序：一号/孤岛号/废号/没账(不存在) → 实际 3 行 + 第 4 号
row_of = {cell(r, 1): r for r in range(ap.rowCount())}
check("缩写显示（金币 1234567 → 123.5万）", cell(row_of["一号"], heads.index("金币")) == "123.5万")
it = ap.item(row_of["一号"], heads.index("金币"))
check("悬停看千分位全量", "1,234,567" in (it.toolTip() or ""))
check("绑了 5 号（不在册）序号直通显 5", cell(row_of["孤岛号"], 0) == "5")
check("未绑实例（ld_index=-1）行序号列显 —", cell(row_of["未绑号"], 0) == "—")
check("有账行最后记账有时间", cell(row_of["一号"], heads.index("最后记账")) != "—")
check("没账行资产格全 —、记账 —",
      cell(row_of["废号"], heads.index("金币")) == "—"
      and cell(row_of["废号"], heads.index("最后记账")) == "—")
ban_it = ap.item(row_of["废号"], 1)
check("封号行红字加粗", ban_it.foreground() == QColor("#d64545") and ban_it.font().bold())

# ── 列集合不变 → 不重建：拖宽金币列后刷新，宽度不动 ──
gc = heads.index("金币")
ap.setColumnWidth(gc, 150)
ap.refresh(database.list_accounts())
check("列集没变 → 刷新不顶掉手拖列宽", ap.columnWidth(gc) == 150)

# ── 布局攥账：restore 早于建列（新列出现后旧存档仍生效）──
ap2 = AssetPanel()
ap2.restore_layout({"w": {"金币": 177, "序号": 33, "坏键": "x", "强化石": 5}})  # 5<10 整条拒
ap2.refresh(database.list_accounts())
h2 = [ap2.horizontalHeaderItem(c).text() for c in range(ap2.columnCount())]
check("先 restore 后建列：金币宽照样套上 177", ap2.columnWidth(h2.index("金币")) == 177)
check("非法宽度(5)拒收 → 用默认 90", ap2.columnWidth(h2.index("强化石")) == 90)

# ── 新增资产列：旧列宽按名保住，新列默认宽 ──
database.merge_account_assets(a1, {"钻石": 30})
ap.refresh(database.list_accounts())
h3 = [ap.horizontalHeaderItem(c).text() for c in range(ap.columnCount())]
check("新资产名自动多列", h3 == ["序号", "账号", "备注", "区服", "强化石", "金币", "钻石", "最后记账"])
check("加列后金币旧宽 150 不串位", ap.columnWidth(h3.index("金币")) == 150)
check("新列钻石用默认宽 90", ap.columnWidth(h3.index("钻石")) == 90)
check("save_layout 按列名存", ap.save_layout()["w"].get("金币") == 150)

# ── 主窗口页签 ──
from ui.main_window import MainWindow
mw = MainWindow()
mw._spawn_batch = lambda jobs, *a, **k: None
check("默认停在「账号管理」页", mw._mid_stack.currentIndex() == 0)
check("第一页就是原超级列表框本体", mw._mid_stack.widget(0) is mw.panel)
check("第二页是资产总览表", mw._mid_stack.widget(1) is mw.asset_panel)
mw.select_mid_tab("assets")
check("点「资产管理」翻到第二页", mw._mid_stack.currentIndex() == 1
      and mw._mid_tab_actions["assets"].isChecked()
      and not mw._mid_tab_actions["accounts"].isChecked())
mw.select_mid_tab("accounts")
check("翻回「账号管理」高亮跟着走", mw._mid_stack.currentIndex() == 0
      and mw._mid_tab_actions["accounts"].isChecked())
mw.refresh()   # 3 秒刷新链路整跑一遍不抛
check("主窗口刷新喂到资产页", mw.asset_panel.rowCount() >= 3)

from PySide6.QtGui import QCloseEvent
mw.closeEvent(QCloseEvent())
saved = cm._config.data.get("ui", {}).get("asset_layout", {})
check("关窗落盘 ui.asset_layout", isinstance(saved, dict) and bool(saved.get("w")))

print("ALL OK" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
