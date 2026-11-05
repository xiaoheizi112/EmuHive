# 工具栏流式布局：一行摆不下的控件自动折到下一行。
# 为什么需要（2026-10-05 用户实拍）：工作台右侧分栏可拖窄，横排工具栏里的
# 「保存流程/测试流程/新增节点…」被布局压到 36px，文字整截挤出按钮。
# 流式布局下每个控件永远拿自己 sizeHint 的宽（= 文字自适应），摆不下就换行，
# 面板高度随换行数自动长（hasHeightForWidth）。
# 来源：Qt 官方 flowlayout 示例的瘦身版 + 本项目补充：末行剩余宽度按
# stretch 分给声明件（状态栏要拉满余宽，和 QHBoxLayout 习惯一致）。
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QSpacerItem


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=-1, hspacing=-1, vspacing=-1):
        super().__init__(parent)
        self._items = []
        self._stretch = []          # 与 _items 平行：addWidget(w, stretch) 记的份额
        self._next_stretch = 0
        # QLayout 只给 setter 不给 horizontalSpacing()/verticalSpacing() getter，
        # 自己存一份，-1 = 跟随 style 的通用 spacing()
        self._hsp = hspacing
        self._vsp = vspacing
        if margin >= 0:
            self.setContentsMargins(margin, margin, margin, margin)
        if hspacing >= 0:
            self.setHorizontalSpacing(hspacing)
        if vspacing >= 0:
            self.setVerticalSpacing(vspacing)

    def _h_spacing(self):
        if self._hsp >= 0:
            return self._hsp
        s = self.spacing()
        return s if s >= 0 else 6      # 都没设过就兜个常用值

    def _v_spacing(self):
        if self._vsp >= 0:
            return self._vsp
        s = self.spacing()
        return s if s >= 0 else 6

    # ── QLayout 协议 ──────────────────────────────────────────────
    def addWidget(self, widget, stretch=0):
        # QLayout.addWidget 内部回调 addItem：用临时槽把 stretch 带过去
        self._next_stretch = stretch
        QLayout.addWidget(self, widget)

    # addStretch/addSpacing 是盒式布局的便利方法，QLayout 本体没有 —— 补上，
    # 调用方（工具栏）才能原样从 QHBoxLayout 换过来
    def addStretch(self, stretch=0):
        self.addItem(QSpacerItem(0, 0, QSizePolicy.Expanding,
                                 QSizePolicy.Minimum))

    def addSpacing(self, width):
        self.addItem(QSpacerItem(width, 0, QSizePolicy.Fixed,
                                 QSizePolicy.Minimum))

    def addItem(self, item):
        self._items.append(item)
        self._stretch.append(self._next_stretch)
        self._next_stretch = 0

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        if 0 <= i < len(self._items):
            self._stretch.pop(i)
            return self._items.pop(i)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        # 下限 = 最宽单件（不是全行总和）——这正是允许分栏拖窄、换行不裁字的底气
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    # ── 排布 ─────────────────────────────────────────────────────
    def _rows(self, rect):
        """按可用宽把条目分组成行，返回 [(行内下标...), ...]。"""
        m = self.contentsMargins()
        right = rect.right() - m.right()
        hsp = self._h_spacing()
        x = rect.x() + m.left()
        rows, cur = [], []
        for i, item in enumerate(self._items):
            w = item.sizeHint().width()
            if cur and x + w - m.right() > right:
                rows.append(cur)
                cur = []
                x = rect.x() + m.left()
            cur.append(i)
            x += w + hsp
        if cur:
            rows.append(cur)
        return rows

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        hsp, vsp = self._h_spacing(), self._v_spacing()
        y = rect.y() + m.top()
        rows = self._rows(rect)
        for r, idxs in enumerate(rows):
            ws = [self._items[i].sizeHint().width() for i in idxs]
            line_h = max(self._items[i].sizeHint().height() for i in idxs)
            if not test_only and r == len(rows) - 1:
                # 末行余宽按 stretch 份额摊给声明件（无 stretch 件保持自然宽）
                total = sum(ws) + hsp * (len(idxs) - 1)
                extra = rect.right() - m.right() - (rect.x() + m.left()) - total
                shares = [self._stretch[i] for i in idxs]
                if extra > 0 and sum(shares) > 0:
                    give = [extra * s // sum(shares) for s in shares]
                    give[give.index(max(give))] += extra - sum(give)   # 凑整不丢像素
                    ws = [w + g for w, g in zip(ws, give)]
            x = rect.x() + m.left()
            for i, w in zip(idxs, ws):
                if not test_only:
                    h = self._items[i].sizeHint().height()
                    self._items[i].setGeometry(QRect(QPoint(x, y), QSize(w, h)))
                x += w + hsp
            y += line_h + vsp
        return y - vsp - rect.y() + m.bottom()
