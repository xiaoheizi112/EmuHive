# 冒烟：功能设置页「雷电路径」手动指认（2026-10-07 用户要求加浏览导入）
# 守住：①粘目录自动补齐成该目录 ldconsole.exe / 去引号去空白；②editingFinished
# 落配置且立即写盘（临时 config 文件重读验证）；③「自动探测」= 清空回探测；
# ④生效路径展示走 _console_path() 现查（假路径 → ⚠ 警告，不留空 label）。
# 不点「浏览」（QFileDialog 模态，离屏会阻塞——CLAUDE.md 记录的坑）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_ldconsole_path.py
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core.config_manager as cm

_tmpdir = tempfile.mkdtemp(prefix="ldc_smoke_")
_cfgpath = os.path.join(_tmpdir, "config.json")
cm._config = cm.ConfigManager(_cfgpath)
cfg = cm._config

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from ui.feature_panel import FeaturePanel

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


def saved_ldc():
    with open(_cfgpath, encoding="utf-8") as fh:
        return json.load(fh).get("general", {}).get("ld_console")


panel = FeaturePanel()

fake_dir = os.path.join(_tmpdir, "LDPlayer X")
os.makedirs(fake_dir, exist_ok=True)

# ── ① _ldc_norm 纯函数 ──
check("① 目录补齐成 ldconsole.exe",
      panel._ldc_norm(fake_dir) == os.path.join(fake_dir, "ldconsole.exe"))
check("① 带引号带空白照收",
      panel._ldc_norm(f'  "{fake_dir}"  ') == os.path.join(fake_dir, "ldconsole.exe"))
f = os.path.join(fake_dir, "ldconsole.exe")
check("① 已是文件路径原样透传", panel._ldc_norm(f) == f)
check("① 空串仍是空（回自动探测语义不被破坏）", panel._ldc_norm("   ") == "")

# ── ② 界面提交链路：粘贴目录 → 补齐回填输入框 → 落盘 ──
panel.ed_ldc.setText(fake_dir)
panel._on_ldc_edited()
check("② 输入框回填补齐后的文件路径", panel.ed_ldc.text() == f)
check("② 配置写入 + 立即落盘", saved_ldc() == f
      and cfg.get("general.ld_console") == f)
# core 的既有语义：手动值不存在会回落探测（本机 D:\LDPlayer 探得到就照用）。
# ⚠ 警告分支没法靠假路径在本机自然触发，打桩 _console_path 强制"哪儿都没有"：
import core.ldplayer as ldp
_real_cp = ldp._console_path
ldp._console_path = lambda: ""
try:
    panel._refresh_ldc()
    check("② 生效路径彻底找不到时给出 ⚠ 警告（不静默）",
          panel.lbl_ldc.text().startswith("⚠"))
    # 顺带验冒认守卫：手填假值、生效值来自别处时不许标"手动指定"
    ldp._console_path = lambda: f.replace("X", "Y")   # 另一台的真实感路径
    os.makedirs(os.path.dirname(ldp._console_path()), exist_ok=True)
    open(f.replace("X", "Y"), "w").close()
    panel._refresh_ldc()
    check("② 手动值失效回落时来源不冒认「手动指定」",
          "自动探测" in panel.lbl_ldc.text())
finally:
    ldp._console_path = _real_cp

# ── ③ 自动探测按钮：清空回探测（配置值归空）──
panel._on_ldc_auto()
check("③ 自动探测 = 配置清空 + 输入框清空",
      saved_ldc() == "" and panel.ed_ldc.text() == "")

# ── ④ 展示走现查：本机若探测得到真 ldconsole 则 ✅，配不到也只验非空 ──
import core.ldplayer as ldp
eff = ldp._console_path()
panel._refresh_ldc()
expect_ok = os.path.isfile(eff or "")
check(f"④ 生效路径展示与探测结果一致（{'✅' if expect_ok else '⚠'}，eff={eff!r}）",
      panel.lbl_ldc.text().startswith("✅" if expect_ok else "⚠"))

print("SMOKE " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
