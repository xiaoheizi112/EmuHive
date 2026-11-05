# 冒烟：运行日志显示层 —— 中文级别 + 按级别整行着色 + 转义。
# 断言四件事：三档级别显示成 [信息]/[警告]/[错误]（不再露英文）、
# 整行前景色取 theme 的绿/橙/红且三色互异；未知级别原样显示不炸；
# 消息带 <b>& 时 toPlainText 原样保留（appendHtml 转义正确、不吞字）。
# 不碰配置落盘（纯显示层，无 save 路径）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_log_color.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from ui import theme
from ui.log_panel import LogPanel

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


def line_color(panel, substr):
    """按行文本找块，取整行字符的前景色（QTextCursor 选整行读 charFormat）。"""
    doc = panel.document()
    for i in range(doc.blockCount()):
        blk = doc.findBlockByNumber(i)
        if substr in blk.text():
            cur = QTextCursor(blk)
            cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            return cur.charFormat().foreground().color().name()
    return None


p = LogPanel()
p.log("启动完成", "INFO")
p.log("分辨率偏离基准", "WARN")
p.log("找图失败", "ERROR")
p.log("跟踪信息", "TRACE")
p.log("特殊字符 <b>& 测试", "INFO")

txt = p.toPlainText()
check("INFO 显示为 [信息] 且不露英文 INFO", "[信息]" in txt and "INFO" not in txt)
check("WARN 显示为 [警告]", "[警告]" in txt and "WARN]" not in txt)
check("ERROR 显示为 [错误]", "[错误]" in txt and "ERROR]" not in txt)
check("未知级别 TRACE 原样显示", "[TRACE]" in txt)
check("消息含 <b>& 原样保留（转义不吞字）",
      "特殊字符 <b>& 测试" in txt)

c_info = line_color(p, "[信息] 启动完成")
c_warn = line_color(p, "[警告] 分辨率")
c_err = line_color(p, "[错误] 找图失败")
check(f"信息行整行绿 {c_info}", c_info == QColor(theme.SUCCESS).name())
check(f"警告行整行橙 {c_warn}", c_warn == QColor(theme.WARN).name())
check(f"错误行整行红 {c_err}", c_err == QColor(theme.DANGER).name())
check("三色互不相同", len({c_info, c_warn, c_err}) == 3)

# 同一级别的每一行都该同色（着色不能只染标签）
check("带转义字符的信息行也是绿",
      line_color(p, "<b>&") == QColor(theme.SUCCESS).name())

# 行数上限 2000 不被 appendHtml 破坏
p2 = LogPanel()
for i in range(2100):
    p2.log(f"刷 {i}", "INFO")
check("连刷 2100 行后 blockCount <= 2000", p2.blockCount() <= 2000)

# ── 任务边界/结果行特标（2026-10-04 追加）：开始=金 / 完成=蓝 / 失败=红 ──
p3 = LogPanel()
p3.log("──────── 开始测试整条流程… ────────", "INFO")
p3.log("开始执行任务：日常副本", "INFO")
p3.log("「点按钮」✓ 执行完成", "INFO")
p3.log("任务完成：日常副本", "INFO")
p3.log("✓ 流程测试完成", "INFO")
p3.log("任务失败，挂机停止（查看截图）", "ERROR")
p3.log("弹窗区 30 秒内没消停，判失败", "WARN")
p3.log("截图失败（窗口最小化/被遮挡？），等待恢复中", "WARN")
t3 = p3.toPlainText()
check("起始行标签换成 [开始]（分隔线/开始执行任务都认）",
      t3.count("[开始]") == 2 and "[信息] ─" not in t3)
check("收口行标签换成 [完成]（执行/任务/测试完成三种句式）",
      t3.count("[完成]") == 3 and "[信息] 「点按钮" not in t3)
check("失败行标签换成 [失败]（ERROR/WARN 送来的都顶掉级别）",
      t3.count("[失败]") == 2)
check("误伤闸门：「截图失败（等待恢复）」过程性文案照旧 [警告] 不染红",
      "[警告] 截图失败" in t3)
c_start = line_color(p3, "[开始] 开始执行任务")
c_done = line_color(p3, "[完成] 任务完成")
c_fail = line_color(p3, "[失败] 任务失败")
c_keep = line_color(p3, "[警告] 截图失败")
check(f"开始行整行金 {c_start}", c_start == QColor(theme.GOLD).name())
check(f"完成行整行蓝 {c_done}", c_done == QColor(theme.ACCENT).name())
check(f"失败行整行红 {c_fail}", c_fail == QColor(theme.DANGER).name())
check("金/蓝/红/橙四色互异（边界行和信息海洋一眼分开）",
      len({c_start, c_done, c_fail, c_keep}) == 4)

print("SMOKE PASS" if ok else "SMOKE FAIL")
sys.exit(0 if ok else 1)
