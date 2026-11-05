# 冒烟：流程便签（flow_notes 旁账本 + 编辑器右上常驻框，2026-09-25）
# 守五条：①打字防抖后切流程自动落账、编辑器随流程换页；②列表 📝 角标随有无翻动；
#         ③改名迁移键（且 _reload_flow 不得把旧名孤儿键写回——修过的连环坑）；
#         ④删流程剪便签账；⑤空文除键不占账本。
# 红线：临时配置单例，绝不碰真 configs/config.json；模态框全打桩。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_flow_note.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core.config_manager as cm
_tmp = os.path.join(tempfile.mkdtemp(), "config.json")
cm._config = cm.ConfigManager(_tmp)
_cfg = cm.get_config()
_cfg.set("custom_flows", {
    "甲": {"version": 2, "nodes": [], "edges": []},
    "乙": {"version": 2, "nodes": [], "edges": []},
})
_cfg.save()

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox
app = QApplication.instance() or QApplication(sys.argv)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
QInputDialog.getText = staticmethod(lambda *a, **k: ("甲改", True))

from ui.flow_editor import FlowPanel

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


def find_row(p, name):
    for i in range(p.flow_list.count()):
        it = p.flow_list.item(i)
        if it.data(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.UserRole) == name:
            return i
    return -1


def goto(p, name):
    """切流程：选列表行 → _reload_flow（真实点击链路的核心段）。"""
    p.flow_list.setCurrentRow(find_row(p, name))
    p._reload_flow()


p = FlowPanel()
p.show()

# ① 记 → 切走 → 落账；切回 → 见字
goto(p, "甲")
p.txt_note.setPlainText("甲的欠账：等模板超时没配 N 口")
check("打字即起防抖", p._note_timer.isActive())
goto(p, "乙")
check("切流程自动落账到甲名下",
      (_cfg.get("flow_notes") or {}).get("甲") == "甲的欠账：等模板超时没配 N 口")
check("切到乙编辑器换页（不吃甲的字）", p.txt_note.toPlainText() == "")
p.txt_note.setPlainText("乙记一笔")
p._note_timer.stop(); p._save_note()
goto(p, "甲")
check("切回甲见旧账", p.txt_note.toPlainText().startswith("甲的欠账"))

# ② 列表角标随有无翻动
def row_text(p, name):
    it = p.flow_list.item(find_row(p, name))
    return it.text()
check("有便签的行带 📝", row_text(p, "甲").endswith("📝") and row_text(p, "乙").endswith("📝"))
p.txt_note.clear()
p._note_timer.stop(); p._save_note()
check("清空后角标摘掉", "📝" not in row_text(p, "甲"))
check("空文除键不占账本", "甲" not in (_cfg.get("flow_notes") or {}))

# ③ 改名迁移键 + 孤儿键不复活
p.txt_note.setPlainText("恢复记录")          # 甲先留一笔（防抖挂着，测改名路径自己冲账）
p._note_timer.stop(); p._save_note()         # 手动落账等价（改名内部也这套顺序）
goto(p, "甲")
p.flow_name = "甲"
p._on_rename_flow()                           # 弹窗已桩：甲 → 甲改
notes = _cfg.get("flow_notes") or {}
check("改名后便签随键迁移",
      notes.get("甲改") == "恢复记录" and "甲" not in notes)
check("改名+_reload_flow 后旧名孤儿键没被写回",
      "甲" not in (_cfg.get("flow_notes") or {}))
check("改名后角标跟到新名行", "📝" in row_text(p, "甲改"))

# ④ 删流程剪便签账
goto(p, "乙")
p._delete_flow("乙")
check("删流程连便签一起除账",
      "乙" not in (_cfg.get("flow_notes") or {}) and "乙" not in (_cfg.get("custom_flows") or {}))

p.cfg.save()
with open(_tmp, encoding="utf-8") as f:
    import json
    on_disk = json.load(f)
# 流程资产分流后：flow_notes 落在同目录 flows.json，合页读回
_flows_p = os.path.join(os.path.dirname(_tmp), "flows.json")
if os.path.exists(_flows_p):
    with open(_flows_p, encoding="utf-8") as f:
        on_disk.update(json.load(f))
check("全部落在临时配置（真 configs 未触碰由临时单例保证）",
      on_disk.get("flow_notes", {}).get("甲改") == "恢复记录")

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke flow-note ALL OK")
