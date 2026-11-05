"""
tests/smoke_assets.py — 资产账本链路常驻自检（临时库隔离，绝不碰 data/app.db）

覆盖两层：
- core/database.merge_account_assets / get_account_assets（读写同源、坏数据纪律）
- bot/tasks/flow._save_assets（流程变量 → 账本 → 表格能读到的整条链）

跑法：python tests/smoke_assets.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 冒烟输出含 ✓/中文等字符，GBK 控制台或重定向下 print 会炸：统一切 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


_tmp = Path(tempfile.mkdtemp(prefix="assets_smoke_"))
from core import database
database.DB_PATH = _tmp / "assets.db"
database.init_db()

from bot.tasks.flow import _save_assets, flow_var_set


class Ctx:
    def __init__(self, ld_index, account_id=None):
        self.ld_index = ld_index
        self.account_id = account_id
        self.extra = {}
        self.log = None


aid = database.add_account(1, account="zhangsan")

# 1) 基本写入 + 中文显示名键
res = database.merge_account_assets(aid, {"gold": 12345, "diamond": 300},
                                    {"gold": "金币", "diamond": "钻石"})
assert set(res["written"]) == {"gold", "diamond"}, res
a = database.get_account_assets(database.list_accounts()[0])
assert a == {"金币": 12345, "钻石": 300}, a

# 2) 千分位容错 + 覆盖式更新 + 坏值逐条丢
res = database.merge_account_assets(aid, {"gold": "1,200,000", "diamond": None,
                                          "bad": "abc"}, {"gold": "金币"})
assert res["written"] == {"gold": 1200000}, res
a = database.get_account_assets(database.list_accounts()[0])
assert a["金币"] == 1200000 and a["钻石"] == 300, a   # 钻石没值 = 留旧账不写脏

# 3) 与 ban 键互不踩踏（同驻 extra_json，合并保留）
database.set_banned(aid, True, reason="测试封号")
database.merge_account_assets(aid, {"gold": 7}, {"gold": "金币"})
row = database.list_accounts()[0]
assert row["enabled"] == 0 and database.is_banned(row) == "测试封号"
assert database.get_account_assets(row) == {"金币": 7, "钻石": 300}

# 4) 坏 JSON 当空字典重建（不炸写库）
conn = database.get_connection()
conn.execute("UPDATE accounts SET extra_json='{坏' WHERE id=?", (aid,))
conn.commit()
conn.close()
assert database.get_account_assets(database.list_accounts()[0]) == {}
database.merge_account_assets(aid, {"gold": 1})
assert database.get_account_assets(database.list_accounts()[0]) == {"gold": 1}

# 5) 没这个账号 → written 空
assert database.merge_account_assets(9999, {"gold": 1})["written"] == {}

# ── 流程节点层：ctx 变量 → 落库 → 读回 ──
aid2 = database.add_account(2, account="lisi")
ctx = Ctx(2, account_id=aid2)
flow_var_set(ctx, "gold", 88888)
flow_var_set(ctx, "diamond", 66)
_save_assets(ctx, {"label": "资产入库", "items": [
    {"var": "gold", "name": "金币"}, {"var": "diamond", "name": "钻石"},
    {"var": "没读的", "name": "不存在"}]})
a2 = database.get_account_assets(database.accounts_by_instance()[2])
assert a2 == {"金币": 88888, "钻石": 66}, a2

# 6) account_id 为 None（试运行路径）→ 按 ld_index 现查兜底
ctx3 = Ctx(2, account_id=None)
flow_var_set(ctx3, "gold", 99)
_save_assets(ctx3, {"items": [{"var": "gold", "name": "金币"}]})
assert database.get_account_assets(database.accounts_by_instance()[2])["金币"] == 99

# 7) 没绑实例的账号：只 WARN 不抛
logs = []
ctx4 = Ctx(77)
ctx4.log = lambda m, lv="INFO": logs.append((lv, m))
flow_var_set(ctx4, "gold", 5)
_save_assets(ctx4, {"items": [{"var": "gold", "name": "金币"}]})
assert logs and logs[0][0] == "WARN" and "未绑定账号" in logs[0][1], logs

# 8) 全没读到：WARN 点名，账本零污染
logs.clear()
ctx5 = Ctx(2)
ctx5.log = lambda m, lv="INFO": logs.append((lv, m))
_save_assets(ctx5, {"items": [{"var": "gold", "name": "金币"}]})
assert logs and "没有已读到" in logs[0][1], logs
assert database.get_account_assets(database.accounts_by_instance()[2])["金币"] == 99

print("smoke_assets: ALL OK")
