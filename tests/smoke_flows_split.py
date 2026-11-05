# 阶段0 流程资产分流回归：config.json ↔ flows.json 分家 + 快照保险。
# 覆盖：分家落盘/重启读回等价/改动才打快照/无变与纯设置改动不刷快照/
#       份数封顶剪最旧/老配置（资产还在 config.json）读出正常且首次 save 自动分流。
# 红线：全程临时目录，绝不碰真 configs/。纯 core 测试不 import Qt，直接 python 跑。
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config_manager import ConfigManager, FLOW_ASSET_KEYS, FLOW_BACKUP_KEEP

fails = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        fails.append(name)


def read(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


tmp = tempfile.mkdtemp()
cfgp = os.path.join(tmp, "config.json")
flowsp = os.path.join(tmp, "flows.json")
bdir = os.path.join(tmp, "flows_backup")

# ── ① 分家落盘：资产键进 flows.json，设置键留 config.json ──
cfg = ConfigManager(cfgp)
cfg.set("custom_flows", {"甲": {"nodes": [1, 2]}})
cfg.set("flow_notes", {"甲": "备注"})
cfg.set("vision.threshold", 0.7)
cfg.save()
main_disk, flows_disk = read(cfgp), read(flowsp)
check("config.json 资产键清零", not any(k in main_disk for k in FLOW_ASSET_KEYS))
check("flows.json 只装资产键（五键齐）", set(flows_disk) == set(FLOW_ASSET_KEYS))
check("非资产键留在 config.json", main_disk["vision"]["threshold"] == 0.7)

# ── ② 重启读回等价 ──
cfg2 = ConfigManager(cfgp)
check("重启读回流程", cfg2.get("custom_flows") == {"甲": {"nodes": [1, 2]}})
check("重启读回便签", cfg2.get("flow_notes") == {"甲": "备注"})
check("重启读回设置", cfg2.get("vision.threshold") == 0.7)

# ── ③ 快照：内容有变才打，存的是覆写前旧版 ──
cfg2.set("custom_flows", {"甲": {"nodes": [3]}, "乙": {}})
cfg2.save()
snaps = sorted(os.listdir(bdir))
check("改一次落 1 份快照", len(snaps) == 1)
check("快照=覆写前旧版", read(os.path.join(bdir, snaps[0]))["custom_flows"] == {"甲": {"nodes": [1, 2]}})

n_snap = len(os.listdir(bdir))
cfg2.save()  # 无内容改动重复 save
check("无改动再 save 不刷快照", len(os.listdir(bdir)) == n_snap)
cfg2.set("vision.threshold", 0.8)  # 只动设置
cfg2.save()
check("纯设置改动不刷流程快照", len(os.listdir(bdir)) == n_snap)
check("设置照常落 config.json", read(cfgp)["vision"]["threshold"] == 0.8)

# ── ④ 份数封顶：连改连存，快照停在 KEEP 份 ──
for i in range(FLOW_BACKUP_KEEP + 5):
    cfg2.set("custom_flows", {"甲": {"nodes": [i]}})
    cfg2.save()
check(f"快照封顶 {FLOW_BACKUP_KEEP} 份", len(os.listdir(bdir)) == FLOW_BACKUP_KEEP)

# ── ⑤ 老配置兜底：资产还写在 config.json、无 flows.json ──
tmp2 = tempfile.mkdtemp()
cfgp2 = os.path.join(tmp2, "config.json")
legacy = {"custom_flows": {"旧流程": {"nodes": []}}, "vision": {"threshold": 0.5}}
with open(cfgp2, "w", encoding="utf-8") as fh:
    json.dump(legacy, fh, ensure_ascii=False)
cfg3 = ConfigManager(cfgp2)
check("老配置照读旧资产", cfg3.get("custom_flows") == {"旧流程": {"nodes": []}})
cfg3.save()  # 首次 save 自动分流
check("首次 save 把资产剥出 config.json", "custom_flows" not in read(cfgp2))
check("flows.json 接住旧资产",
      read(os.path.join(tmp2, "flows.json"))["custom_flows"] == {"旧流程": {"nodes": []}})
check("首次分流=初版不自备份", not os.path.exists(os.path.join(tmp2, "flows_backup")))

print("-" * 46)
if fails:
    print("SMOKE FAILED:", fails)
    sys.exit(1)
print("smoke flows-split ALL OK")
