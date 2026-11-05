# 冒烟：优先级/顺序行清单的每行「↑上移 / ↓下移」按钮（2026-09 新增）
# 覆盖 _row_move_btns 的行编辑器：循环体步骤行 / 循环体内嵌清弹窗模块行 / 定时执行行。
# ⚠ 监视行 2026-10-05 改版：摘要列表拖拽换序 + 选中行一对 ↑↓（轻重建不换面板），
#   钮对不在行里了，本文件改按新钮 tooltip + 列表换算通道 _watch_sync 直测。
# 断言三件事：钮按行数出现且边界置灰（首行禁↑末行禁↓）、点击后行数据整体换位、
# 换位后表单重建（旧钮销毁）。不真存盘（红线：临时配置单例 + autosave 关）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_row_move.py
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
cfg = cm.get_config()

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QPushButton

app = QApplication.instance() or QApplication(sys.argv)

from ui.flow_editor import FlowPanel


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


UP_TIP = "上移（提高优先级/提前执行）"
DOWN_TIP = "下移（降低优先级/延后执行）"


def n(nid, t, x, y, **kw):
    return {"id": nid, "type": t, "x": x, "y": y, "label": "", **kw}


# 行名字刻意留空：编辑器重建表单时"保真兜底"会把库里没有的假名字回写成
# 真实模板名（既有行为），A/B/C 这种假名进了面板就同化得看不出演习。
# 换位判据全走自加的 tag 键（编辑器不认识的键绝口不碰）。
cfg.set("custom_flows", {"moveTest": {"version": 2, "nodes": [
    n("w1", "watch", 0, 0, rows=[{"category": "", "name": "", "kind": "tap", "tag": 1},
                                 {"category": "", "name": "", "kind": "tap", "tag": 2},
                                 {"category": "", "name": "", "kind": "tap", "tag": 3}],
      cooldown_secs=5, fail_limit=3),
    n("t1", "timer", 600, 0, rows=[{"interval_min": 5, "flow_name": ""},
                                   {"interval_min": 10, "flow_name": ""}],
      cooldown_secs=5, fail_limit=3),
    n("l1", "loop", 0, 300, steps=[
        {"id": "s1", "type": "sleep", "seconds": 1},
        {"id": "s2", "type": "sleep", "seconds": 2},
        {"id": "s3", "type": "popup_zone", "modules": [
            {"category": "", "name": "", "tag": 21},
            {"category": "", "name": "", "tag": 22},
            {"category": "", "name": "", "tag": 23}], "silent_secs": 5, "timeout": 180},
    ], cond={"kind": "template", "category": "", "name": ""},
        max_times=0, on_interrupt="continue"),
], "edges": []}})

panel = FlowPanel()
panel.chk_autosave.setChecked(False)
panel.resize(1400, 900)
panel.show()
wait(300)
panel._select_flow("moveTest")
wait(300)

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


def select(nid):
    """在画布里选中指定节点 → 触发属性面板重建。"""
    from ui.flow_editor import FlowNodeItem
    panel.scene.clearSelection()
    item = next(it for it in panel.scene.items()
                if isinstance(it, FlowNodeItem) and it.node.get("id") == nid)
    item.setSelected(True)
    wait(150)
    return item.node


def move_btns():
    return [b for b in panel.findChildren(QPushButton)
            if b.toolTip() in (UP_TIP, DOWN_TIP)]


def click(k, up):
    """点第 k 行的上/下钮（行编辑器重建后钮对象即销毁，每次先重查）。"""
    btns = move_btns()
    btns[2 * k + (0 if up else 1)].click()
    wait(150)


# ── 监视行（2026-10-09 收口进通用 RowListEditor：↑↓ 与换算通道都吃组件同款；
#    断言意图不变：选中行一对移动钮/换位/选中跟随/末行置灰/拖放换算）──
node = select("w1")
from PySide6.QtWidgets import QListWidget
from ui.flow_editor import RowListEditor
W_UP = "上移选中行；挪得远直接在列表里拖"
W_DOWN = "下移选中行"
lst = panel.props_panel.findChild(QListWidget)
check("监视 3 行 → 摘要列表 3 条", lst is not None and lst.count() == 3)


def wbtn(tip):
    return [b for b in panel.props_panel.findChildren(QPushButton)
            if b.toolTip() == tip][0]


lst.setCurrentRow(1)
wait(120)
check("选中行 → 一对移动钮且↑可用", len(
    [b for b in panel.props_panel.findChildren(QPushButton)
     if b.toolTip() in (W_UP, W_DOWN)]) == 2 and wbtn(W_UP).isEnabled())
wbtn(W_UP).click()
wait(120)   # 第2行(tag2)上移 → [2,1,3]
check("监视行↑：换位生效", [r["tag"] for r in node["rows"]] == [2, 1, 3])
check("↑ 后选中跟随挪走的行（不弹回）", lst.currentRow() == 0)
lst.setCurrentRow(1)
wait(80)
wbtn(W_DOWN).click()
wait(120)   # tag1 下移 → [2,3,1]
check("监视行↓：换位生效", [r["tag"] for r in node["rows"]] == [2, 3, 1])
lst.setCurrentRow(2)
wait(80)
check("末行：↑可用、↓置灰", wbtn(W_UP).isEnabled() and not wbtn(W_DOWN).isEnabled())
it = lst.takeItem(2)            # 模拟拖放落定（拖走 tag1 插到最前）
lst.insertItem(0, it)
next(r for r in panel.props_panel.findChildren(RowListEditor)
     if r.lst is lst)._sync()   # 组件换算通道直测（拖放真路由 rowsMoved 自动调）
wait(80)
check("拖放换算：rows 跟列表新序走", [r["tag"] for r in node["rows"]] == [1, 2, 3])

# ── 定时执行行 ──
node = select("t1")
check("定时 2 行 → 4 颗移动钮", len(move_btns()) == 4)
click(1, up=True)   # 10 分钟行上移
check("定时行↑：换位生效", [r["interval_min"] for r in node["rows"]] == [10, 5])

# ── 循环体动作行（2026-10-06 行列表化：RowListEditor 同款摘要列表+选中行一对↑↓；
#    内嵌清弹窗模块行仍是逐行 _row_move_btns，但只有选中那条动作才摊得开）──
node = select("l1")
from ui.flow_editor import RowListEditor
rls = panel.props_panel.findChildren(RowListEditor)
rl_step = next((r for r in rls if "添加动作" in r._add_text), None)
rl_cond = next((r for r in rls if r.lst.count() == 1), None)
# 2026-10-06「发信号」后循环面板是三张表：出口条件 · 收到即结束的信号 · 动作
# （认领走 _add_text 稳定身份：行数/min_rows 都会撞车，别拿它们瞎认）
check("循环属性 = 三张行列表（条件1行 · 停止信号0行 · 动作3行），不再叠排整套表单",
      len(rls) == 3 and rl_step is not None and rl_cond is not None
      and rl_step.lst.count() == 3
      and any("添加停止信号" in r._add_text and r.lst.count() == 0 for r in rls))
check("动作行摘要带类型+参数",
      rl_step.lst.item(0).text() == "① 等待·1秒"
      and rl_step.lst.item(2).text() == "③ 清弹窗·3模块")
# 出口条件只有 1 行：组件钮对存在但首末双禁（条件区 min_rows=1，删不掉也挪不动）
check("单行列表：↑↓都置灰", not rl_cond._b_up.isEnabled()
      and not rl_cond._b_dn.isEnabled())
rl_step.lst.setCurrentRow(2)
wait(120)
check("动作末行：↑可用、↓置灰", rl_step._b_up.isEnabled()
      and not rl_step._b_dn.isEnabled())
rl_step.lst.setCurrentRow(1)
wait(120)
rl_step._b_up.click()
wait(150)   # sleep2 上移 → [2,1,popup]
check("动作行↑：换位生效", [s.get("seconds") for s in node["steps"][:2]] == [2, 1])
check("↑ 后选中跟随挪走的行", rl_step.lst.currentRow() == 0
      and rl_step.lst.item(0).text().startswith("① 等待·2秒"))
it = rl_step.lst.takeItem(2)          # 模拟拖放：popup 行挪到最前
rl_step.lst.insertItem(0, it)
rl_step._sync()
wait(150)
check("动作行拖放换算：steps 跟列表新序走",
      [s.get("type") if s.get("type") == "popup_zone" else s.get("seconds")
       for s in node["steps"]] == ["popup_zone", 2, 1])
rl_step.lst.setCurrentRow(0)
wait(150)   # 选中清弹窗行 → 表单摊开，内嵌模块行带老式每行钮对
check("选中清弹窗行才摊模块行：3 模块 = 6 颗老式移动钮", len(move_btns()) == 6)
click(1, up=True)   # 模块第 2 行(tag22) 上移
check("内嵌模块行↑：换位生效",
      [m["tag"] for m in node["steps"][0]["modules"]] == [22, 21, 23])
# 右键后端直测（菜单的触发器）：复制→粘贴独立件→删除；结构路会整场重建，每步重查句柄
rl_step._copy(node["steps"][1])
rl_step._paste(0)
wait(200)
check("右键复制/粘贴：插第2位且是 deepcopy 独立件（改一份不牵动另一份）",
      [s.get("seconds") for s in node["steps"][1:3]] == [2, 2]
      and node["steps"][1] is not node["steps"][2])
rl2 = next(r for r in panel.props_panel.findChildren(RowListEditor)
           if r.lst.count() == 4)
rl2._delete(1)
wait(200)
check("右键删除：行没了、剪贴板残值不清（粘贴可再来一发）",
      [s.get("seconds") for s in node["steps"][1:3]] == [2, 1]
      and rl2._clip_get() is not None)
# 结构路每次 _delete 都整场重建，句柄即抛即换：连删 3 条逐个重查
# （按 min_rows 认动作表——删到剩 1 行时行数会和出口条件表撞车，光看行数挑会误删条件）
for _ in range(3):
    rlx = next(r for r in panel.props_panel.findChildren(RowListEditor)
               if "添加动作" in r._add_text)
    rlx._delete(len(node["steps"]) - 1)
    wait(150)
rl_fin = next(r for r in panel.props_panel.findChildren(RowListEditor)
              if "添加动作" in r._add_text)
rl_fin._delete(0)   # 空表越界删：应默默无影响，不崩
check("动作可删光（min_rows=0 空循环合法）+ 空表删不崩",
      node["steps"] == [] and rl_fin.lst.count() == 0)
# 对照：出口条件那张列表 min_rows=1 —— 直调后端删第 1 条必须删不掉
rl_c = next(r for r in panel.props_panel.findChildren(RowListEditor)
            if r._min == 1)
rl_c._delete(0)
check("出口条件删不回 0 行（min_rows=1 兜底）", node["cond"] is not None
      and rl_c.lst.count() == 1)

ok = ok and True
print("-" * 46)
if not ok:
    print("SMOKE FAILED")
    sys.exit(1)
print("smoke row-move ALL OK")
