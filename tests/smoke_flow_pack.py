# 冒烟：流程包导出/导入（core/flow_pack + UI 接线可达性；2026-10-04 新增）
# 守的链路：①导出 zip 清单正确（flows.json 三键齐全；带图时 PNG 实存、
#   index.json 只含被引用条目——没被引用的图不许混进包）；
# ②round-trip：导出→config 三键清空+本机模板删光→导入→custom_flows 逐字节
#   比对、分类/order 还原、PNG 补回；
# ③同名流程 覆盖/跳过 两分支各一条（一次性策略，不逐条）；
# ④template_manager.import_index 的 skip_existing：True 时本机同名 PNG 内容
#   不被改、账本条目不被替换、包里的新条目照补；False（默认）老行为一字不变
#   —— 两个独立 temp 库对拍，绝不碰真实 templates/；
# ⑤纯 json（无图包）可导入，返回缺图提醒 missing_refs；
# ⑥FlowPanel 右键空白两个处理函数：弹窗走 _ask_* 缝替换（offscreen 模态必
#   阻塞，见 CLAUDE.md 实录），落账后重建链路（_refresh_flow_list/取消分支
#   账本纹丝不动）；⑦MainWindow._export_accounts：打桩 QFileDialog 直落
#   txt，表头 = account_io.HEADERS+密码，脱敏分支不含明文。
# 全程临时 config 单例 + 临时 templates 目录（冒烟写盘陷阱铁律），
# 绝不打真实 configs/config.json 或 templates/。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_flow_pack.py
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QLabel,
                               QListWidget, QPushButton)

app = QApplication.instance() or QApplication([])

import core.config_manager as cm
from core import flow_pack, template_manager

_tmp = Path(tempfile.mkdtemp(prefix="fp_smoke_"))
cm._config = cm.ConfigManager(_tmp / "config.json")

# ── 临时模板库（模块全局换址：load_index/_save_index/import_index 调用时现读）──
_tmpl = _tmp / "templates"
_tmpl.mkdir()


def _use_lib(d: Path):
    template_manager.TEMPLATES_DIR = d
    template_manager.INDEX_PATH = d / "index.json"
    # 图缓存/stat 节流戳一并作废（0.5s 窗口内连改不失效 = 冒烟假绿）
    template_manager._cache = {"index": None, "mtime": 0.0, "images": {}}
    template_manager._stat_ts = 0.0


def _write_lib(entries, pngs):
    """pngs: {相对路径: bytes}；entries: 账本条目列表"""
    for rel, data in pngs.items():
        p = template_manager.TEMPLATES_DIR / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    template_manager.INDEX_PATH.write_text(
        json.dumps({"templates": entries}, ensure_ascii=False), encoding="utf-8")
    template_manager._cache = {"index": None, "mtime": 0.0, "images": {}}
    template_manager._stat_ts = 0.0


_use_lib(_tmpl)
_seed_entries = [
    {"name": "图1", "category": "库A", "file": "库A/图1.png", "threshold": 0.9},
    {"name": "图2", "category": "库B", "file": "库B/图2.png", "threshold": 0.8},
    {"name": "无关", "category": "库A", "file": "库A/无关.png", "threshold": 0.8},
]
_write_lib(_seed_entries, {"库A/图1.png": b"AAA1", "库B/图2.png": b"BBB2",
                           "库A/无关.png": b"XXX"})

_seed_flows = {
    "甲": {"version": 2, "nodes": [
        {"type": "click_template", "category": "库A", "name": "图1", "id": "n1"}],
        "edges": []},
    "乙": {"version": 2, "nodes": [
        {"type": "wait_template", "category": "库B", "name": "图2", "id": "n2"}],
        "edges": []},
    "丙": {"version": 2, "nodes": [], "edges": []},
}
_seed_cats = {"甲": "日常", "乙": "日常"}
_seed_order = ["日常"]


def _seed_cfg():
    cm._config.data["custom_flows"] = json.loads(json.dumps(_seed_flows))
    cm._config.set("flow_categories", dict(_seed_cats))
    cm._config.set("flow_category_order", list(_seed_order))
    cm._config.save()


def _wipe_cfg():
    cm._config.set("custom_flows", {})
    cm._config.set("flow_categories", {})
    cm._config.set("flow_category_order", [])
    cm._config.save()


_seed_cfg()
_before = json.dumps(_seed_flows, ensure_ascii=False, sort_keys=True)

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and bool(cond)


# ── ① 导出 zip 清单 ──
_zp = _tmp / "包.zip"
r = flow_pack.export_pack(str(_zp), with_templates=True)
check(f"导出入口：3 流程 2 模板 0 缺图（实际 {r}）",
      r["flows"] == 3 and r["templates"] == 2 and r["missing_png"] == [])
with zipfile.ZipFile(_zp) as zf:
    names = set(zf.namelist())
    payload = json.loads(zf.read("flows.json").decode("utf-8"))
    idx = json.loads(zf.read("templates/index.json").decode("utf-8"))
check("zip 清单：flows.json + 被引用 index 子集 + 两张 PNG 实体",
      names == {"flows.json", "templates/index.json",
                "templates/库A/图1.png", "templates/库B/图2.png"})
check("没被任何流程引用的「无关」不进包（账本与图都不进）",
      not any("无关" in n for n in names))
check("flows.json 三键 + 包标记齐全",
      payload["kind"] == "emuhive-flow-pack" and payload["version"] == 1
      and set(payload) >= {"custom_flows", "flow_categories", "flow_category_order"}
      and json.dumps(payload["custom_flows"], ensure_ascii=False, sort_keys=True) == _before)
check("包内账本只含被引用条目",
      {e["name"] for e in idx["templates"]} == {"图1", "图2"})
# 账本有记录但 PNG 已丢 → missing_png 点名、包照常成
(_tmpl / "库B" / "图2.png").unlink()
r2 = flow_pack.export_pack(str(_tmp / "缺图包.zip"), with_templates=True)
check(f"账本有图无 → missing_png 点名（实际 {r2['missing_png']}）",
      ("库B", "图2") in r2["missing_png"] and r2["missing_png"])
(_tmpl / "库B").mkdir(exist_ok=True)
(_tmpl / "库B" / "图2.png").write_bytes(b"BBB2")

# ── ② round-trip：清空→导入→逐字节还原 ──
_wipe_cfg()
(_tmpl / "库A" / "图1.png").unlink()
(_tmpl / "库B" / "图2.png").unlink()
_write_lib([], {})   # 本机模板全丢（空账本+图已删净，导入必须真从包里补图）
r = flow_pack.import_pack(str(_zp), overwrite=True)
after = json.dumps(cm._config.get("custom_flows"), ensure_ascii=False, sort_keys=True)
check("round-trip：custom_flows 逐字节还原", after == _before)
check("round-trip：分类/order 还原",
      cm._config.get("flow_categories") == _seed_cats
      and cm._config.get("flow_category_order") == _seed_order)
check(f"round-trip：模板补回 2 张（实际并入 {r['templates_merged']}）",
      r["templates_merged"] == 2 and r["added"] == 3
      and (_tmpl / "库A" / "图1.png").read_bytes() == b"AAA1")
check("round-trip：导入后引用现场重扫不再缺图", r["missing_refs"] == [])

# ── ③ 同名流程：跳过全部 vs 覆盖全部 ──
_cur = cm._config.get("custom_flows")
_tampered = json.loads(json.dumps(_cur))
_tampered["甲"]["nodes"][0]["name"] = "本机自采图"
cm._config.set("custom_flows", _tampered)
cm._config.set("flow_categories", {"甲": "本机组"})
cm._config.set("flow_category_order", ["日常", "本机组"])
cm._config.save()
r = flow_pack.import_pack(str(_zp), overwrite=False)
check(f"跳过分支：0 覆盖 3 跳过（实际 {r['overwritten']}/{r['skipped']}）",
      r["overwritten"] == 0 and r["skipped"] == 3 and r["added"] == 0)
check("跳过分支：本机流程本体/分类纹丝不动",
      cm._config.get("custom_flows")["甲"]["nodes"][0]["name"] == "本机自采图"
      and cm._config.get("flow_categories")["甲"] == "本机组")
r = flow_pack.import_pack(str(_zp), overwrite=True)
check(f"覆盖分支：3 覆盖 0 跳过（实际 {r['overwritten']}/{r['skipped']}）",
      r["overwritten"] == 3 and r["skipped"] == 0)
check("覆盖分支：包版本替换本机 + 分类跟包（本机组留着——order 只并集）",
      cm._config.get("custom_flows")["甲"]["nodes"][0]["name"] == "图1"
      and cm._config.get("flow_categories")["甲"] == "日常")

# ── ④ import_index 的 skip_existing：两个独立 temp 库对拍 ──
def _mk_ext_lib():
    d = _tmp / "extlib"
    d.mkdir(exist_ok=True)
    _use_lib(d)   # 先指过去再写，_write_lib 认全局
    _write_lib([
        {"name": "图X", "category": "库A", "file": "库A/图X.png", "threshold": 0.55},
        {"name": "图Y", "category": "库A", "file": "库A/图Y.png", "threshold": 0.6},
    ], {"库A/图X.png": b"THEIRS", "库A/图Y.png": b"NEW2"})
    return d


_ext_dir = _mk_ext_lib()
_local = _tmp / "local1"
_local.mkdir()
_use_lib(_local)
_write_lib([{"name": "图X", "category": "库A", "file": "库A/图X.png", "threshold": 0.77}],
           {"库A/图X.png": b"LOCAL"})
n = template_manager.import_index(str(_ext_dir / "index.json"), skip_existing=True)
li = template_manager.load_index()["templates"]
check(f"skip_existing=True：只并入新条目图Y（返回 {n}）",
      n == 1 and {t["name"] for t in li} == {"图X", "图Y"})
check("skip_existing=True：同名条目账本不被替换（阈值仍 0.77）",
      next(t for t in li if t["name"] == "图X")["threshold"] == 0.77)
check("skip_existing=True：本机同名 PNG 内容不被改",
      (_local / "库A" / "图X.png").read_bytes() == b"LOCAL")
check("skip_existing=True：缺的图补进（图Y.png 落盘 NEW2）",
      (_local / "库A" / "图Y.png").read_bytes() == b"NEW2")
# 账本有记录但 PNG 丢了 → skip_existing 顺手把图补回（只补不覆盖，不重复计数）
(_local / "库A" / "图Y.png").unlink()
_n = template_manager.import_index(str(_ext_dir / "index.json"), skip_existing=True)
check(f"skip_existing=True：有账缺图 → 图补回、条目不重复并入（返回 {_n}）",
      _n == 0 and (_local / "库A" / "图Y.png").read_bytes() == b"NEW2"
      and (_local / "库A" / "图X.png").read_bytes() == b"LOCAL")
# 默认 False：老行为一字不变（覆盖账本 + 覆盖 PNG）
_local2 = _tmp / "local2"
_local2.mkdir()
_use_lib(_local2)
_write_lib([{"name": "图X", "category": "库A", "file": "库A/图X.png", "threshold": 0.77}],
           {"库A/图X.png": b"LOCAL"})
n = template_manager.import_index(str(_ext_dir / "index.json"))
li = template_manager.load_index()["templates"]
check(f"默认 False 老行为：2 条全并（返回 {n}）", n == 2)
check("默认 False 老行为：同名条目被外来替换（阈值 0.55）且 PNG 被覆盖",
      next(t for t in li if t["name"] == "图X")["threshold"] == 0.55
      and (_local2 / "库A" / "图X.png").read_bytes() == b"THEIRS")
_use_lib(_tmpl)   # 回主临时库，后面 UI 段继续用

# ── ⑤ 纯 json（无图包）导入 + 缺图提醒 ──
_jp = _tmp / "flows_only.json"
with zipfile.ZipFile(_zp) as zf:
    _jp.write_bytes(zf.read("flows.json"))
info = flow_pack.peek_pack(str(_jp))
check("peek 纯 json：认出流程名单、无图",
      info["flows"] == ["甲", "乙", "丙"] and info["has_templates"] is False)
_empty = _tmp / "empty_lib"
_empty.mkdir()
_use_lib(_empty)
_write_lib([], {})
_wipe_cfg()
r = flow_pack.import_pack(str(_jp), overwrite=True)
check(f"纯 json 可导入：3 新增、并入模板 0（实际 {r['templates_merged']}）",
      r["added"] == 3 and r["overwritten"] == 0 and r["templates_merged"] == 0)
check(f"缺图提醒：图1/图2 点名（实际 {r['missing_refs']}）",
      ("库A", "图1") in r["missing_refs"] and ("库B", "图2") in r["missing_refs"])
# 非法文件拒（既不 zip 也不合法 json / 缺包标记）
_bad = _tmp / "bad.json"
_bad.write_text('{"custom_flows": {"乱": {}}}', encoding="utf-8")
try:
    flow_pack.import_pack(str(_bad))
    check("无包标记的 json 被拒", False)
except ValueError:
    check("无包标记的 json 被拒（ValueError）", True)
_use_lib(_tmpl)

# ── ⑥ FlowPanel 右键接线（弹窗全走 _ask_* 缝替换，offscreen 不 exec）──
import ui.flow_editor as fe
from ui.flow_editor import FlowPanel


class FakeScene:
    def update(self):
        pass


f = FlowPanel.__new__(FlowPanel)
f.cfg = cm._config
f.flow_name = "甲"
f._dirty = False
f.flow_list = QListWidget()
f.lbl_flow_title = QLabel()
f.cmb_cat = QComboBox()
f.cmb_cat.setEditable(True)
f._lock_deleg = type("_D", (), {"set_locked": lambda s, x: None})()
f._locked_list = lambda: []
f._notes = lambda: {}
f.scene = FakeScene()
f._schedule_validation = lambda: None
reload_calls = []
f._reload_flow = lambda: reload_calls.append(1)
msgs = []
f._set_status = lambda t: msgs.append(t)
f._ask_with_templates = lambda: True
f._ask_conflict = lambda dups: True

# 导出：打桩 getSaveFileName → 文件真实生成
_out2 = str(_tmp / "ui导出.zip")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (_out2, ""))
f._on_export_flows()
check("UI 导出：zip 落盘 + 状态播报",
      Path(_out2).exists() and any("已导出" in m for m in msgs))

# 导入（先清 config 让包全走新增）：重建链路走一遍。
# _tmpl 此刻状态：账本含 图1/图2（②并入），PNG 都在盘上 —— 直接复用。
_wipe_cfg()
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(_zp), ""))
f.flow_name = ""
f._on_import_flows()
check("UI 导入：3 流程进账本 + 播报缺图提醒为空",
      set(cm._config.get("custom_flows")) == {"甲", "乙", "丙"}
      and any("导入完成" in m for m in msgs) and not any("缺图" in m for m in msgs))
listed = {v for i in range(f.flow_list.count())
          if (v := f.flow_list.item(i).data(fe.Qt.UserRole))}
check("UI 导入后侧栏重建（列表里认得出三条流程）",
      listed == {"甲", "乙", "丙"})

# 同名冲突：覆盖分支画布重载、取消分支账本纹丝不动
_tampered = json.loads(json.dumps(cm._config.get("custom_flows")))
_tampered["甲"]["nodes"][0]["name"] = "画布旧版"
cm._config.set("custom_flows", _tampered)
cm._config.save()
f.flow_name = "甲"
f._on_import_flows()   # _ask_conflict→True 覆盖；_dirty False 直接重载
check("UI 导入覆盖：同名流程被包替换且画布走了重载链路",
      cm._config.get("custom_flows")["甲"]["nodes"][0]["name"] == "图1"
      and reload_calls and f.flow_name == "甲")
n_msg = len(msgs)
f._ask_conflict = lambda dups: None
cm._config.get("custom_flows")["甲"]["nodes"][0]["name"] = "别动我"
cm._config.save()
f._on_import_flows()   # 取消：一行账都不动
check("UI 导入取消分支：账本纹丝不动、无新播报",
      cm._config.get("custom_flows")["甲"]["nodes"][0]["name"] == "别动我"
      and len(msgs) == n_msg)

# ── ⑥b 侧栏底部常驻按钮（2026-10-04 用户反馈：右键空白入口太隐蔽）：
# 真建一个 FlowPanel（smoke_flow_switch_anchor 同款离屏构造），按文字找按钮，
# 打桩 _on_* 后 click() 验证信号真接上了 ──
real = FlowPanel()
by_text = {b.text(): b for b in real.findChildren(QPushButton)}
be, bi = by_text.get("导出"), by_text.get("导入")
check("侧栏底部有常驻「导出/导入」按钮", be is not None and bi is not None)
if be and bi:
    hits = []
    real._on_export_flows = lambda: hits.append("e")
    real._on_import_flows = lambda: hits.append("i")
    be.click()
    bi.click()
    check("按钮点击直达 _on_export_flows/_on_import_flows（与右键同一入口）",
          hits == ["e", "i"])
real.deleteLater()

# ── ⑦ MainWindow._export_accounts（打桩 getSaveFileName，临时库）──
from core import account_io, database
from ui.main_window import MainWindow

database.DB_PATH = _tmp / "acc.db"
database.init_db()
database.add_account(1, account="abc", password="pw9", name="甲号")


class _Sink:
    def __init__(self):
        self.lines = []

    def log(self, message, level="INFO"):
        self.lines.append((level, message))


mw = type("FakeMW", (), {"log_panel": _Sink()})()
_apath = str(_tmp / "账号导出.txt")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (_apath, ""))
MainWindow._export_accounts(mw, True)
lines = Path(_apath).read_text(encoding="utf-8-sig").splitlines()
check(f"导出 txt 表头 = 表格列+密码（实际 {lines[0]}）",
      lines[0] == ",".join(account_io.HEADERS + ["密码"]))
check("导出含密码：明文行落文件 + 日志点名",
      any("abc" in ln and "pw9" in ln for ln in lines[1:])
      and any("已导出账号 1 条" in m for _l, m in mw.log_panel.lines))
MainWindow._export_accounts(mw, False)
lines2 = Path(_apath).read_text(encoding="utf-8-sig").splitlines()
check("导出不含密码：脱敏 ******，明文不出现",
      "******" in lines2[1] and "pw9" not in lines2[1])
# 取消对话框 = 不落文件不再加日志（备份分支）
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
_nlog = len(mw.log_panel.lines)
MainWindow._export_accounts(mw, True)
check("取消导出：日志零新增", len(mw.log_panel.lines) == _nlog)

shutil.rmtree(_tmp, ignore_errors=True)
print("SMOKE " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
