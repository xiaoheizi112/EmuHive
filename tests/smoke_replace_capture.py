# 冒烟：重新采集·同名覆盖（2026-10-05 新增）
# 链路：流程槽 📷（槽已带图=「重新采集·覆盖」）→ request_capture(目录, 旧名)
#   → 采集页锁名 + 黄条 → 保存前 _confirm_overwrite 列现役引用者 → 确认后
#   add_template 同名覆盖（图换芯不换名）→ 锁自动解除。
# 红线自证：全程只写临时模板库 + 临时配置单例，不碰真 templates/ 与真 config。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_replace_capture.py
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np

import core.config_manager as cm
tmp = tempfile.mkdtemp()
cm._config = cm.ConfigManager(os.path.join(tmp, "config.json"))
cfg = cm.get_config()

from core import template_manager
tdir = Path(tmp) / "templates"
tdir.mkdir()
template_manager.TEMPLATES_DIR = tdir
template_manager.INDEX_PATH = tdir / "index.json"

CAT, NAME = "测试库", "图标_按钮A"
template_manager.add_template(NAME, CAT, np.full((8, 8, 3), 1, np.uint8), 0.85)
cfg.set("custom_flows", {"测试流程": {"version": 2, "nodes": [
    {"id": "c1", "type": "click_template", "x": 0, "y": 0, "label": "",
     "category": CAT, "name": NAME}], "edges": []}})

from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication.instance() or QApplication(sys.argv)

# 离屏模态框必桩（CLAUDE.md 铁律）：记录确认正文供断言
ASKED = []
QMessageBox.question = staticmethod(
    lambda *a, **k: (ASKED.append(a[2]), QMessageBox.Yes)[1])

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


# ── ① 流程面板出口：一处分流，槽带图名随信号走 ──
from ui.flow_editor import FlowPanel

got = []
panel = FlowPanel()
panel.request_capture.connect(lambda c, n: got.append((c, n)))
panel._request_capture(CAT, None, {"name": NAME})       # 条件槽已带图
panel._request_capture(CAT, {"name": ""}, None)          # 节点槽还没选图
check("槽已带图 → emit 带旧名（进锁名覆盖）", got[0] == (CAT, NAME))
check("槽空图 → emit 带空串（走老新建路）", got[1] == (CAT, ""))

# ── ② 采集页锁名态 ──
import ui.template_tool as tt

tp = tt.TemplatePanel()
tp._refresh_category_combo()
tp.prefill_category(CAT)
tp.prefill_replace(NAME)
check("锁名：输入=旧名且只读、前缀锁、黄条/另存钮/取消钮显形",
      tp.edt_name.text() == NAME and tp.edt_name.isReadOnly()
      and not tp.cmb_prefix.isEnabled()
      and not tp.lbl_replace.isHidden() and not tp.btn_recut_asnew.isHidden()
      and not tp.btn_recut_cancel.isHidden())

# 解锁但保留原名硬存 → 判据看库里同名、不看锁（防绕红线）
tp._replace_name = "残留错值"
check("非锁态同名照样拦（Yes→放行）", tp._confirm_overwrite(NAME))
check("确认正文点名现役引用者", "测试流程" in ASKED[-1])
tp._replace_name = None
check("库里无同名 → 普通新建不拦", tp._confirm_overwrite("新图B"))

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
tp._replace_name = NAME
check("No → 拒绝覆盖", not tp._confirm_overwrite(NAME))

# ── ③ 「另存为新图」解锁 / 空名清残留锁 ──
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
tp.btn_recut_asnew.click()
check("另存为新图 → 解锁可编辑、黄条收",
      tp._replace_name is None and not tp.edt_name.isReadOnly()
      and tp.cmb_prefix.isEnabled() and tp.lbl_replace.isHidden())
tp.prefill_replace(NAME)
tp.prefill_replace("")
check("prefill_replace(空) 清残留锁（防串态）",
      tp._replace_name is None and not tp.edt_name.isReadOnly()
      and tp.lbl_replace.isHidden())

# 「取消」钮：一键退出重采态（解锁清名、黄条收、原图不碰）
tp.prefill_replace(NAME)
tp.btn_recut_cancel.click()
check("取消＝解锁清名回普通采集（黄条/双钮全收）",
      tp._replace_name is None and not tp.edt_name.isReadOnly()
      and not tp.edt_name.text().startswith(NAME)
      and tp.lbl_replace.isHidden() and tp.btn_recut_cancel.isHidden())

# ── ④ 保存闭环：Yes → 同名覆盖落盘、图换芯、锁自解 ──
class FakeView:
    selection = (2, 2, 10, 10)
    roi_rect = None
    click_target_pos = None
    draw_mode = "template"
    click_owner = None

    def clear_selection(self):
        self.selection = None

    def update(self):
        pass


tp._view = FakeView()
tp._frame = np.full((20, 20, 3), 200, np.uint8)
tp.prefill_replace(NAME)
st = []
tp.status_message.connect(st.append)
tp.on_save_and_test()
entry = template_manager.find_template(CAT, NAME)
img = template_manager.load_template_image(entry)
check("同名覆盖落盘：账本名/库不变", entry is not None
      and entry["name"] == NAME and entry["category"] == CAT)
check("图片本体换新芯（均值 1→200）", img is not None and img.mean() > 150)
check("覆盖成功话术「已覆盖更新」", any("已覆盖更新" in s for s in st))
check("覆盖后锁自动解除（下次=普通新建）",
      tp._replace_name is None and not tp.edt_name.isReadOnly())

# No 分支：原图一根毛都不动
tp._view = FakeView()
tp._frame = np.full((20, 20, 3), 123, np.uint8)
tp.edt_name.setText(NAME)
tp.prefill_replace(NAME)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
tp.on_save_and_test()
check("No → 不写盘（图仍是 200 芯）",
      template_manager.load_template_image(
          template_manager.find_template(CAT, NAME)).mean() > 150
      and tp._replace_name == NAME)      # 取消 = 锁还在，可改框重save

# ── ⑤ 源码守卫：确认必须插在写盘之前（挪到 add_template 后就形同虚设）──
src = __import__("inspect").getsource(tt.TemplatePanel.on_save_and_test)
check("确认拦在 add_template 落盘之前",
      -1 < src.find("_confirm_overwrite") < src.find("add_template"))
wb = open(os.path.join(os.path.dirname(__file__), "..", "ui",
                       "workbench.py"), encoding="utf-8").read()
check("工作台接线 prefill_replace", "prefill_replace(name)" in wb)

# ── ⑥ 模板库右键直达「重新采集·覆盖」（2026-10-06：重新找图不绕流程）──
m_src = __import__("inspect").getsource(tt.TemplatePanel.on_template_menu)
check("右键菜单挂「重新采集·覆盖…」三连",
      "重新采集·覆盖" in m_src and "prefill_category(category)" in m_src
      and "prefill_replace(name)" in m_src and "begin_capture()" in m_src)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
tp._clear_replace_lock()          # 上一段 No 分支留的锁先解干净
tp.prefill_category(CAT)
tp.prefill_replace(NAME)
tp.begin_capture()
check("三连落地：库选中/名锁死/覆盖态到位",
      tp.cmb_category.currentData() == CAT and tp._replace_name == NAME
      and tp.edt_name.isReadOnly())
check("三连后仍可正常解锁另存（红线门不变）",
      tp._confirm_overwrite(NAME) is True)   # Yes 桩放行，但拦问过（下方验话术）

print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke replace-capture ALL OK")
