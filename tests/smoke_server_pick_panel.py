# 冒烟：识字取物属性面板 —— 「插入账号列」下拉 + 各行宽度 ≤318 视口
# 守两条（2026-09 排版规矩，参照 smoke_watch_container_panel 的宽度回归）：
# ① 目标文字/选区文字 行旁有下拉，选中把 {账号.列名} 插进同一行输入框并落节点；
# ② 各用法 × 各认不出处置下面板最小宽不超 318
#    （曾几何时滑动四控件横排 348px，出横向滑块、上面几行被切一半）。
# 不真存盘（红线：临时配置单例 + autosave 关）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_server_pick_panel.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


import core.config_manager as cm

cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QLabel,
                               QLineEdit, QPushButton, QSpinBox)

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowPanel

p = FlowPanel()
p.chk_autosave.setChecked(False)

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


def fill(node):
    """走正式重建入口（_clear_props 递归清旧表单 + 构建期脏标记闸门），
    别自己 takeAt —— form 是 addLayout 进去的，只清 widget 会漏整张旧表单。"""
    p._rebuild_props(node)
    app.processEvents()
    # offscreen 的 processEvents 不跑 DeferredDelete：手动强制回收，
    # 让 findChildren 只剩当前这张面板（否则按文本 next() 会抓到上张的残留）
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def token_combos():
    # isVisibleTo 过滤（2026-09-22 补）：_clear_props 是 hide+deleteLater 异步销毁，
    # offscreen 下 processEvents 不跑 DeferredDelete，findChildren 会把上一张面板的
    # 残留下拉一起捞出（实测假 FAIL：pick_pair 数出 3 个、roi_number 数出 2 个）。
    # 只数当前真显示行里的，才等于「这一张面板有几个账号下拉」。
    return [c for c in p.props_panel.findChildren(QComboBox)
            if c.count() > 0 and c.itemText(0) == "插入账号列…"
            and c.isVisibleTo(p.props_panel)]


# ① 目标文字行：下拉选中 → 整行覆盖 + 落节点 + 复位
node = {"id": "s1", "type": "server_pick", "mode": "click", "roi": None}
fill(node)
cbs = token_combos()
check("click 用法：目标文字行旁有「插入账号列」下拉", len(cbs) == 1)
if ok:
    cmb = cbs[0]
    edt = next(e for e in cmb.parentWidget().findChildren(QLineEdit))
    i = cmb.findText("区服")
    check("下拉项「区服」的数据 = {账号.区服}", i >= 0 and cmb.itemData(i) == "{账号.区服}")
    check("下拉不提供密码（防进日志）", cmb.findText("密码") < 0)
    check("构表阶段不写 text（留空 = 读账号表语义原样保留）", "text" not in node)
    cmb.setCurrentIndex(i)
    check("选中 → {账号.区服} 填进输入框并落节点",
          edt.text() == "{账号.区服}" and node.get("text") == "{账号.区服}")
    check("插完下拉复位（下次还能挑同一列）", cmb.currentIndex() == 0)
    # 行为改版：由"光标处追加"改成"整行覆盖"（追加实测反直觉：
    # 光标位置不可控，换个列名越点越乱）。再挑一列 → 前一次的内容被顶掉。
    edt.setText("新手1区")   # 模拟手填过的旧内容
    node["text"] = "新手1区"
    cmb.setCurrentIndex(cmb.findText("等级"))
    check("再选一列 → 覆盖原有内容，不再追加",
          edt.text() == "{账号.等级}" and node.get("text") == "{账号.等级}")

# ①b 旧 pick_pair 已从用法下拉除名（换算链另见 smoke_splice_pick_pair）
node2 = {"id": "s2", "type": "server_pick", "mode": "click",
         "roi": [72, 247, 193, 715]}
fill(node2)
cmb_mode = next(c for c in p.props_panel.findChildren(QComboBox)
                if c.findData("click") >= 0 and c.itemText(0) == "点文字"
                and c.isVisibleTo(p.props_panel))
check("用法下拉不再提供「选区选服」", cmb_mode.findData("pick_pair") < 0)

# ①c 残留 pick_pair 节点（绕过换算直挂面板）：显示口径与实际生效不许两套
node_rp = {"id": "rp", "type": "server_pick", "mode": "pick_pair",
           "roi": [1, 2, 3, 4], "roi_server": [5, 6, 7, 8],
           "zone_text": "前缀", "pair_gap_secs": 2}
fill(node_rp)
check("用法下拉共 4 项（点文字/识文读数字/读数字/读坐标）",
      next(c for c in p.props_panel.findChildren(QComboBox)
           if c.findData("click") >= 0).count() == 4)
btn_texts = [b.text() for b in p.props_panel.findChildren(QPushButton)
             if b.isVisibleTo(p.props_panel)]
check("残留节点面板无「选服区域」框选按钮",
      not any("选服" in t for t in btn_texts))
check("残留节点面板无「两步间隔」字样",
      not any("两步" in t for t in btn_texts))
check("残留节点用法下拉回落「点文字」",
      next(c for c in p.props_panel.findChildren(QComboBox)
           if c.findData("click") >= 0).currentData() == "click")

# ①d 方案 A 收行：ROI 默认折叠成摘要行，铅笔钮展开 4 坐标
fill(node2)
spns = [s for s in p.props_panel.findChildren(QSpinBox)
        if s.accessibleName().startswith("区域") and s.isVisibleTo(p.props_panel)]
check("默认收起：坐标 spinbox 不露出", not spns)
btn_edit = next(b for b in p.props_panel.findChildren(QPushButton)
                if b.toolTip() == "手改坐标" and b.isVisibleTo(p.props_panel))
btn_edit.click()
spns = [s for s in p.props_panel.findChildren(QSpinBox)
        if s.accessibleName().startswith("区域") and s.isVisibleTo(p.props_panel)]
check("点铅笔 → 展开 4 个坐标框", len(spns) == 4)
spns[0].setValue(123)
check("改坐标 → 摘要行跟着刷新 + 落节点",
      "已框 123," in next(l for l in p.props_panel.findChildren(QLabel)
                          if "识别区域" in l.text()
                          and l.isVisibleTo(p.props_panel)).text()
      and node2.get("roi")[0] == 123)

# ①e 没框过时摘要不许拿默认全屏坐标装"已框"（旧四根默认值 spinbox 的错觉）
fill({"id": "s_noframe", "type": "server_pick", "mode": "click", "roi": None})
sum_lbl = next(l for l in p.props_panel.findChildren(QLabel)
               if "识别区域" in l.text() and l.isVisibleTo(p.props_panel))
check("未框 → 摘要直说未框（= 全屏识别）",
      "未框" in sum_lbl.text() and "已框" not in sum_lbl.text())

# ①f roi_number 没有文字锚点行 → 也不该冒出下拉（且必框无勾选壳）
node3 = {"id": "s3", "type": "server_pick", "mode": "roi_number"}
fill(node3)
check("roi_number：无锚点行则无下拉", len(token_combos()) == 0)
check("roi_number：无「限定识别区域」勾选（该用法必框）",
      not any(c.text().startswith("限定识别区域")
              for c in p.props_panel.findChildren(QCheckBox)
              if c.isVisibleTo(p.props_panel)))

# ①g 回归（2026-09-23 实测坑）：重建期控件还没挂进面板（无父），此时
#    setVisible(True) = 当场弹一个 "python" 标题的孤儿顶层小窗闪一帧。
#    踩中路径：认不出时=滑动 的识字取物节点、勾了随机等待 的 sleep 节点。
#    修法：共用 _vis()（构建期只藏不显，新控件默认可见）。
from PySide6.QtCore import QEvent, QObject


class _OrphanSpy(QObject):
    def __init__(self):
        super().__init__()
        self.hits = []

    def eventFilter(self, o, e):
        if (e.type() == QEvent.Show and o.isWidgetType()
                and o.parent() is None and o is not p):
            self.hits.append(type(o).__name__)
        return False


spy = _OrphanSpy()
app.installEventFilter(spy)
fill({"id": "sc", "type": "server_pick", "mode": "click",
      "on_timeout": "scroll", "roi": [0, 0, 540, 960]})
check("scroll 用法重建：不闪现无父孤儿窗", not spy.hits)
fill({"id": "sr", "type": "sleep", "seconds": 1, "seconds_max": 2})
check("随机等待 sleep 重建：不闪现无父孤儿窗", not spy.hits)
# 藏/显语义不许被修法弄丢：exit 节点重建后滑动行必须仍是藏的
fill({"id": "se", "type": "server_pick", "mode": "click",
      "on_timeout": "exit", "roi": [0, 0, 540, 960]})
scr_hint = next(l for l in p.props_panel.findChildren(QLabel)
                if "滑了设定次数" in l.text())
check("exit 节点：滑动参数行照旧藏好", not scr_hint.isVisibleTo(p.props_panel))
cmb_miss = next(c for c in p.props_panel.findChildren(QComboBox)
                if c.findData("scroll") >= 0 and c.isVisibleTo(p.props_panel))
cmb_miss.setCurrentIndex(cmb_miss.findData("scroll"))
app.processEvents()   # show 的可见态要过一轮事件循环才落到 isVisibleTo
check("面板挂好后切到 scroll：滑动行正常显出（_vis 不吞挂父后的 show）",
      scr_hint.isVisibleTo(p.props_panel))

# ② 宽度回归：全用法 × 认不出处置
for mode in ("click", "number", "roi_number", "position"):
    for ot in ("exit", "scroll"):
        fill({"id": "w", "type": "server_pick", "mode": mode,
              "on_timeout": ot, "roi": [0, 0, 540, 960]})
        mw = p.props_panel.minimumSizeHint().width()
        check(f"server_pick {mode}/{ot} 面板最小宽 {mw} ≤ 318 视口", mw <= 318)
# 展开态也核一次（4 坐标横排不许撑爆视口）
fill({"id": "wx", "type": "server_pick", "mode": "click",
      "roi": [0, 0, 540, 960]})
be = next(b for b in p.props_panel.findChildren(QPushButton)
          if b.toolTip() == "手改坐标" and b.isVisibleTo(p.props_panel))
be.click()
mw = p.props_panel.minimumSizeHint().width()
check(f"展开坐标行后面板最小宽 {mw} ≤ 318 视口", mw <= 318)

# ③ 识别稳定(s) 行（2026-10-11）：默认 0 = 认出即处置（不写键）、拨动落值、
#    拨回 0 弹键（老节点零迁移）
from PySide6.QtWidgets import QDoubleSpinBox
sb1 = {"id": "sb", "type": "server_pick", "mode": "click"}
fill(sb1)
spn_stable = next((s for s in p.props_panel.findChildren(QDoubleSpinBox)
                   if s.specialValueText() == "认出即处置"), None)
check("识字取物面板有「识别稳定(s)」行（默认 0 = 认出即处置，不偷建键）",
      spn_stable is not None and spn_stable.value() == 0.0
      and "stable_secs" not in sb1)
spn_stable.setValue(2.0)
check("识别稳定拨到 2.0 → 节点落 stable_secs=2.0", sb1.get("stable_secs") == 2.0)
spn_stable.setValue(0.0)
check("拨回 0 → stable_secs 键弹出（零迁移）", "stable_secs" not in sb1)

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke server-pick-panel ALL OK")
