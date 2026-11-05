"""
ui/asset_panel.py — 资产总览表（中间列页签第二页「资产管理」）

一行一个账号、一列一种资产的全库对账表。数据源与超级列表框末列「资产」
完全同源（accounts.extra_json["assets"]，「资产入库」节点落的账），这里只
读不写——写入侧零改动，两本账不可能漂移。

设计口径（2026-09-21 与用户确认）：
- 行 = accounts 全表（含未绑模拟器的号，序号列显"—"），不是实例列表：
  对账看的是号，不是窗口
- 资产列 = 全库出现过的资产名取并集、按名排序自动生成——流程里加个新
  读数变量，这页自动多一列，不改代码；排序固定是为了列序不随查库顺序跳
- 数值走 fmt_asset 缩写（1.2万/3.5亿），悬停看千分位全量，跟第一页同一套
- 封号行（enabled=0）账号列红字加粗，与超级列表框同一措辞
- 布局记忆按「列名」存宽度（不按列号）：资产列会增删，按名存才不会新列
  出现后旧宽度串门；列序/滚动不记（动态列记了也没意义）
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem

from core import database
from ui import theme
from ui.account_panel import _centered, fmt_asset

# 固定列（资产列插在它们和「最后记账」之间）
_HEAD = ("序号", "账号", "备注", "区服")
_TAIL = "最后记账"
_COL_W = {"序号": 40, "账号": 140, "备注": 90, "区服": 100, _TAIL: 140}
_ASSET_W = 90


class AssetPanel(QTableWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("accountTable")   # 复用密排样式（8pt 在下面自设）
        f = self.font()
        f.setPointSize(8)
        self.setFont(f)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setHighlightSections(False)
        self.verticalHeader().setDefaultSectionSize(22)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        # restore_layout 可能早于第一次 refresh（那时资产列还没生成），
        # 存的宽度先攥在这，_rebuild_headers 建列时一并套用
        self._saved_w: dict[str, int] = {}
        self._rebuild_headers([])

    # ── 列结构（随资产名并集动态生成）──
    def _rebuild_headers(self, asset_names: list[str]):
        """重建表头/列数。列集合没变就直接返回——每 3 秒刷一次，
        无脑重建会把用户拖好的列宽和横向滚动位置顶掉。"""
        labels = list(_HEAD) + asset_names + [_TAIL]
        if [self.horizontalHeaderItem(c).text() if self.horizontalHeaderItem(c)
            else None for c in range(self.columnCount())] == labels:
            return labels
        # 列要变了：先按列名保住现有宽度（新列没有历史记录用默认宽）
        keep = {}
        for c in range(self.columnCount()):
            it = self.horizontalHeaderItem(c)
            if it:
                keep[it.text()] = self.columnWidth(c)
        self.setColumnCount(len(labels))
        for c, name in enumerate(labels):
            it = QTableWidgetItem(name)
            it.setTextAlignment(Qt.AlignCenter)
            self.setHorizontalHeaderItem(c, it)
            # 宽度优先级：本次现宽（keep）> 关窗存档（_saved_w，冷启动时
            # 建列早于第一次 refresh 的兜底）> 默认宽
            w = keep.get(name) or self._saved_w.get(name) or _COL_W.get(name) or _ASSET_W
            self.setColumnWidth(c, w)
        # 末列吃剩余宽度（与超级列表框同款观感）
        self.horizontalHeader().setStretchLastSection(True)
        return labels

    # ── 布局记忆（按列名存宽，见模块头设计口径）──
    def save_layout(self) -> dict:
        return {"w": {self.horizontalHeaderItem(c).text(): self.columnWidth(c)
                      for c in range(self.columnCount())
                      if self.horizontalHeaderItem(c)}}

    def restore_layout(self, data):
        """按列名套关窗存的宽度。此刻还不存在的列（资产列要等第一次
        refresh 才生成）攥进 _saved_w，建列时套用后即消费。"""
        if not isinstance(data, dict):
            return
        widths = data.get("w")
        if not isinstance(widths, dict):
            return
        good = {k: v for k, v in widths.items()
                if isinstance(k, str) and isinstance(v, int) and 10 <= v <= 2000}
        self._saved_w.update(good)
        for c in range(self.columnCount()):
            it = self.horizontalHeaderItem(c)
            if it and it.text() in good:
                self.setColumnWidth(c, good[it.text()])
                self._saved_w.pop(it.text(), None)

    # ── 刷新 ──
    def refresh(self, accounts: list[dict], assets_by_id: dict | None = None):
        """accounts = database.list_accounts() 全表。纯读展示，
        坏数据（extra_json 不是 JSON）在 get_account_assets 侧已兜成空。
        assets_by_id = 主窗口一轮算一次的 {账号id: 资产}（与表格「资产」列同源
        同份，省一遍 JSON 解析）；不传则逐号自解，老调用点行为不变。"""
        def _assets(acc):
            if assets_by_id is not None:
                return assets_by_id.get(acc.get("id"), {})
            return database.get_account_assets(acc)

        names: list[str] = []
        for acc in accounts:
            for k in _assets(acc):
                if k not in names:
                    names.append(k)
        names.sort()   # 列序固定：不随查库顺序 / 插入顺序跳
        labels = self._rebuild_headers(names)

        self.setRowCount(len(accounts))
        for row, acc in enumerate(accounts):
            li = acc.get("ld_index", -1)
            self.setItem(row, 0, _centered(str(li) if li is not None and li >= 0 else "—"))
            acct_item = _centered(str(acc.get("account") or "—"))
            if not acc.get("enabled", 1):
                acct_item.setForeground(QColor(theme.DANGER))
                f = acct_item.font()
                f.setBold(True)
                acct_item.setFont(f)
            self.setItem(row, 1, acct_item)
            self.setItem(row, 2, _centered(str(acc.get("name") or "—")))
            self.setItem(row, 3, _centered(str(acc.get("server") or "—")))

            assets = _assets(acc)
            for off, name in enumerate(names):
                if name in assets:
                    it = _centered(fmt_asset(assets[name]))
                    try:
                        it.setToolTip(f"{name}：{int(assets[name]):,}")
                    except (TypeError, ValueError):
                        pass   # 坏值（非数字）不设悬停全量，显示原样
                    self.setItem(row, len(_HEAD) + off, it)
                else:
                    self.setItem(row, len(_HEAD) + off, _centered("—"))
            self.setItem(row, len(labels) - 1,
                         _centered(database.get_assets_updated_at(acc) or "—"))
