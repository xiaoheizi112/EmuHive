# 模板"移动到库"自检（正式冒烟，tests/ 下常驻）
# 守的链路：右键「移动到库」→ core.move_template（账本 category/file + PNG 成对搬）
# → template_deps.rewrite_template_refs(new_cat=)（config 里按 (库,名) 的引用跟着改）。
# 三件要命的事各一条断言：①搬完引用不断链（读侧 collect_refs 挂到新键）
# ②目标库同名一律拒绝、零副作用（绝不覆盖丢图）③按名跨目录引用（category=""）
# 移动时不用改也照样命中，改名时反倒必须跟着改 —— 两种传播的差项语义各测一遍。
# ⚠ 全程临时目录：重定向 template_manager 的账本/图目录 + 临时 config 单例，
#   绝不碰真 templates/ 与 configs/config.json（用户资产红线）。
# 跑法：QT_QPA_PLATFORM=offscreen python tests/smoke_template_move.py
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


import numpy as np

# 冒烟写盘红线：先换临时 config 单例，再动 template_manager 路径常量
import core.config_manager as cm

_tmp = tempfile.mkdtemp()
cfg = cm.ConfigManager(os.path.join(_tmp, "config.json"))
cm._config = cfg

import core.template_manager as tm

tm.TEMPLATES_DIR = Path(_tmp) / "templates"
tm.INDEX_PATH = tm.TEMPLATES_DIR / "index.json"

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from bot.template_deps import collect_refs, rewrite_template_refs

ok = True


def check(label, cond):
    global ok
    print(("OK  " if cond else "FAIL") + f" {label}")
    ok = ok and cond


def img():
    return np.zeros((10, 20, 3), dtype=np.uint8)


tm.add_template("攻击按钮", "挂机打怪", img(), 0.85)
tm.add_template("血条", "挂机打怪", img(), 0.85)
tm.add_template("金币图标", "游戏登录", img(), 0.85)
tm.add_template("血条", "仓库", img(), 0.85)        # 故意让 仓库 也有同名，测拒绝

# ── 拒绝路径：一律 False，账本/文件纹丝不动 ──
before = json.dumps(tm.load_index()["templates"], ensure_ascii=False)
check("同库互移拒绝", not tm.move_template("血条", "挂机打怪", "挂机打怪"))
check("目标库为空拒绝", not tm.move_template("血条", "挂机打怪", ""))
check("模板不存在拒绝", not tm.move_template("没这张", "挂机打怪", "仓库"))
check("目标库已有同名拒绝（防覆盖丢图）",
      not tm.move_template("血条", "挂机打怪", "仓库"))
check("PNG 没被偷偷搬（仓库/血条.png 存在、挂机打怪/血条.png 还在）",
      (tm.TEMPLATES_DIR / "仓库" / "血条.png").exists()
      and (tm.TEMPLATES_DIR / "挂机打怪" / "血条.png").exists())
check("拒绝路径零副作用：账本原样",
      json.dumps(tm.load_index()["templates"], ensure_ascii=False) == before)

# ── 正路：成对搬家 ──
check("合法移动成功", tm.move_template("金币图标", "游戏登录", "仓库"))
entry = next(t for t in tm.list_templates() if t["name"] == "金币图标")
check("账本成对更新（file 恒为 目录/名.png）",
      entry["category"] == "仓库" and entry["file"] == "仓库/金币图标.png")
check("PNG 实体搬到位（旧位置清空）",
      (tm.TEMPLATES_DIR / "仓库" / "金币图标.png").exists()
      and not (tm.TEMPLATES_DIR / "游戏登录" / "金币图标.png").exists())
check("搬完还能读出图片本体", tm.load_template_image(entry) is not None)

# ── 写侧传播 + 读侧口径 ──
# 三种引用点：按库引用（移动要改）、跨目录按名引用（移动不改、改名才改）、
# motion 帧组（整组一个 category）
cfg.set("custom_flows", {
    "日常": {"version": 2, "nodes": [
        {"id": "n1", "type": "click_template", "x": 0, "y": 0,
         "category": "游戏登录", "name": "金币图标"},
        {"id": "n2", "type": "wait_template", "x": 0, "y": 100,
         "category": "", "name": "金币图标"},
        {"id": "n3", "type": "branch", "x": 0, "y": 200,
         "cond": {"kind": "motion", "category": "游戏登录",
                  "frames": ["金币图标", "其它"]}},
    ], "edges": []},
})
cfg.save()

n = rewrite_template_refs("游戏登录", "金币图标", new_cat="仓库")
nodes = cfg.get("custom_flows")["日常"]["nodes"]
check(f"移动传播=2 处（按库节点 + motion 组；跨目录''不动）（实测 {n}）", n == 2)
check("n1 换库", nodes[0]["category"] == "仓库" and nodes[0]["name"] == "金币图标")
check("n2（''）原样不动", nodes[1]["category"] == "" and nodes[1]["name"] == "金币图标")
check("n3 motion 整组换库、帧名不动",
      nodes[2]["cond"]["category"] == "仓库"
      and nodes[2]["cond"]["frames"] == ["金币图标", "其它"])

refs = collect_refs()
check("读侧同步：新键挂上使用者、旧键消失、''键仍在",
      refs.get(("仓库", "金币图标")) == {"flow:日常"}
      and ("游戏登录", "金币图标") not in refs
      and refs.get(("", "金币图标")) == {"flow:日常"})

# ── 改名回归：老签名薄壳 + ''/帧名同样要跟 ──
n2 = rewrite_template_refs("仓库", "金币图标", new_name="金币")
nodes = cfg.get("custom_flows")["日常"]["nodes"]
check(f"改名传播=3 处（含''按名与 motion 帧名）（实测 {n2}）", n2 == 3)
check("三处名字全跟上",
      nodes[0]["name"] == "金币" and nodes[1]["name"] == "金币"
      and nodes[2]["cond"]["frames"] == ["金币", "其它"])

print("ALL OK" if ok else "HAS FAIL")
sys.exit(0 if ok else 1)
