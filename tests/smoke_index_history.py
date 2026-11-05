# 冒烟：用户资产历史快照 json_store.dump_with_history + 模板账本接入
# （2026-10-09 阶段0续：templates/index.json 同款保险丝；flows.json 行为由
#  smoke_flows_split 守——它现在走同一个实现，重构即全绿＝两边都验到）
# 覆盖：首写不存/有变才存/同文不存/返回值/封顶剪最旧/只剪自家前缀不碰邻居/
#       tm._save_index 经 add_template 落快照/快照拷回=找回旧账本。
# 红线：全程临时目录，绝不碰真 templates/ 与 configs/。不 import Qt，直接跑。
import json
import os
import shutil
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
cm._config = cm.ConfigManager(os.path.join(tempfile.mkdtemp(), "config.json"))

from core import json_store
from core import template_manager as tm

fails = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        fails.append(name)


def snaps_of(bdir, prefix):
    return sorted(f for f in os.listdir(bdir)
                  if f.startswith(prefix + "_") and f.endswith(".json")) \
        if os.path.isdir(bdir) else []


# ── ① 快照原语语义 ──
d = tempfile.mkdtemp()
target = os.path.join(d, "t.json")
bdir = os.path.join(d, "t_backup")
check("首次落盘=初版不存快照",
      json_store.dump_with_history(target, {"v": 1}, bdir, "t") is False
      and not os.path.isdir(bdir))
check("内容有变 → 存快照且返回 True",
      json_store.dump_with_history(target, {"v": 2}, bdir, "t") is True
      and len(snaps_of(bdir, "t")) == 1)
check("同文重复写不刷快照",
      json_store.dump_with_history(target, {"v": 2}, bdir, "t") is False
      and len(snaps_of(bdir, "t")) == 1)
with open(os.path.join(bdir, "neighbor.json"), "w") as fh:
    fh.write("x")                        # 目录里邻居文件：剪枝绝不误伤
for i in range(5):
    json_store.dump_with_history(target, {"v": 10 + i}, bdir, "t", keep=3)
check("封顶 keep=3 剪最旧", len(snaps_of(bdir, "t")) == 3)
check("剪枝只认自家前缀", os.path.exists(os.path.join(bdir, "neighbor.json")))
oldest_kept = snaps_of(bdir, "t")[0]
with open(os.path.join(bdir, oldest_kept), encoding="utf-8") as fh:
    # 循环 v10..v14 各存"覆写前旧版"（v2,v10,v11,v12,v13）+ 之前的 v1 共 6 份，
    # 剪到 3 份 → 留最后三次快照内容 v11/v12/v13，最早留的应是 v11
    check("封顶后留下的都是最近的（最早留 v=11）", json.load(fh) == {"v": 11})

# ── ② 模板账本链路：add_template → index_backup 落快照；改账本再存即累积 ──
base = Path(tempfile.mkdtemp())
tm.TEMPLATES_DIR = base / "templates"
tm.INDEX_PATH = tm.TEMPLATES_DIR / "index.json"
img = np.zeros((8, 8, 3), np.uint8)
tm.add_template("图一", "甲库", img, 0.85)          # 首次建账本：无快照
check("账本首建不存快照", not os.path.isdir(os.path.join(tm.TEMPLATES_DIR, "index_backup")))
tm.add_template("图二", "甲库", img, 0.85)          # 账本有变：覆写前存旧版
bk = os.path.join(tm.TEMPLATES_DIR, "index_backup")
check("账本改动 → index_backup 落 1 份", len(snaps_of(bk, "index")) == 1)
tm._save_index(tm.load_index())                     # 同文重存不刷
check("同内容重存不刷快照", len(snaps_of(bk, "index")) == 1)

# ── ③ 找回语义：拿快照拷回覆盖 index.json → 读回旧账本（1 条模板） ──
snap = os.path.join(bk, snaps_of(bk, "index")[0])
shutil.copy2(snap, tm.INDEX_PATH)
tm._refresh_cache_if_needed()
names = [t["name"] for t in tm.list_templates()]
check("快照拷回=找回旧账本", names == ["图一"])

print("-" * 46)
if fails:
    print("SMOKE FAILED:", fails)
    sys.exit(1)
print("smoke index-history ALL OK")
