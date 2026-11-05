"""
core/database.py — SQLite 数据库（第 4 步）

保存"流水数据"：账号档案、任务执行历史、战力快照、邮件台账、运行日志。

本步学到的知识点：
1. SQLite：零配置的文件型数据库，整个库就是一个 data/app.db 文件。
   对比 JSON：JSON 适合"整个读整个写"的小配置；
   数据库适合"频繁追加 + 按条件查询"的记录（任务日志、邮件台账）
2. CREATE TABLE IF NOT EXISTS：每次启动都执行一遍也安全（表存在就跳过），
   这就是"数据库结构自动创建/升级"的最简单形态
3. WAL 模式：写操作不阻塞读操作；busy_timeout：别人在写时等 5 秒而不是立刻报错
   ——两者配合，多线程（以后每账号一个 worker 线程）同时写库才安全
4. 参数化 SQL：用 ? 占位符传值（execute("...=?", (value,))），
   杜绝 SQL 注入、也免去字符串引号转义的麻烦
5. 单数据源原则：账号/密码的"真源"在数据库这张表，配置文件和界面都从这里读
"""

import json
import sqlite3
import time
from pathlib import Path

from core.config_manager import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "app.db"

# 五张表的结构定义（需求 3 的数据全部有家可归）
SCHEMA = """
-- 账号主表：一个游戏账号一行（需求 3：账号/密码/区服/战力/等级/钥匙/箱子）
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ld_index INTEGER NOT NULL DEFAULT 0,   -- 绑定的模拟器实例编号
    name TEXT DEFAULT '',                  -- 备注名
    account TEXT DEFAULT '',               -- 游戏账号
    password TEXT DEFAULT '',              -- 游戏密码
    server TEXT DEFAULT '',                -- 登录区服
    player_id TEXT DEFAULT '',             -- 游戏内玩家ID
    enabled INTEGER NOT NULL DEFAULT 1,    -- 1=启用 0=停用
    status TEXT DEFAULT 'idle',            -- 当前状态：idle/running/paused/error
    current_task TEXT DEFAULT '',          -- 正在执行的任务名（需求 3）
    combat_power INTEGER DEFAULT 0,        -- 战力
    level INTEGER DEFAULT 0,               -- 等级
    keys INTEGER DEFAULT 0,                -- 钥匙
    boxes INTEGER DEFAULT 0,               -- 箱子
    extra_json TEXT DEFAULT '{}',          -- 个人信息等扩展字段（避免频繁改表结构）
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 任务执行历史：需求 3 的"任务完成度"从这里聚合统计
CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    task_name TEXT NOT NULL,
    start_at TEXT,
    end_at TEXT,
    status TEXT NOT NULL,                  -- success/fail/skip
    fail_reason TEXT DEFAULT '',
    screenshot_path TEXT DEFAULT '',       -- 失败截图留证
    detail_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 角色快照表 character_snapshot / 邮件台账表 mail_log 已删（2026-10-09）：
-- 功能下线（发-mail 需求 5 未做、快照无读方），全项目零调用。
-- 老库里残留的这两张空表无害不动（红线：不删用户数据文件）。
"""


def get_connection() -> sqlite3.Connection:
    """开一个新连接（每 worker 线程独立连接；短事务用完即关）"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")      # 写不阻塞读
    conn.execute("PRAGMA busy_timeout=5000")     # 写冲突时等 5 秒
    conn.row_factory = sqlite3.Row               # 查出来按列名访问 row["name"]
    return conn


def init_db():
    """建表（幂等：每次启动调用一次，表已存在则跳过）"""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ── DAO 函数（Data Access Object：数据读写的最小操作集）────────────────

def _row_to_dict(row) -> dict:
    return dict(row)


def add_account(ld_index: int, name: str = "", account: str = "",
                password: str = "", server: str = "", player_id: str = "",
                enabled: int = 1, combat_power: int = 0, level: int = 0,
                keys: int = 0, boxes: int = 0) -> int:
    """
    新增账号，返回新账号 id。
    ld_index 传 -1 = 暂不绑定实例（导入时实例不够用会这样记，
    表格里看不到，但导出还能拿到，不会丢数据）。
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO accounts (ld_index, name, account, password, server,"
            " player_id, enabled, combat_power, level, keys, boxes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ld_index, name, account, password, server,
             player_id, enabled, combat_power, level, keys, boxes),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_accounts() -> list[dict]:
    """全部账号（GUI 账号表的数据源）"""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def update_account(account_id: int, **fields) -> bool:
    """
    按字段更新账号。只更新传入的字段（白名单外的键会被忽略）。
    例：update_account(1, status="running", combat_power=12345)
    """
    allowed = {"ld_index", "name", "account", "password", "server", "player_id",
               "enabled", "status", "current_task", "combat_power", "level",
               "keys", "boxes", "extra_json"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return False
    sets = ", ".join(f"{k}=?" for k in fields)
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE accounts SET {sets}, updated_at=datetime('now','localtime')"
            f" WHERE id=?",
            (*fields.values(), account_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


# 「重置账号」要清回建表默认值的档案字段（2026-09-21，右键菜单用）。
# 只清"会过期的档案"：区服、玩家ID、等级（等级后续由游戏实时读取，重置=清掉旧值）。
# 刻意不含：account/password（登录凭据，重置不该把人家的号弄丢）、
# ld_index（绑定关系）、name（备注是用户自己起的标识，清了就不知道哪台是谁了）、
# status/current_task（运行时状态，worker 自己管）、enabled（封禁是用户意志）、
# combat_power/keys/boxes（数量类字段——用户明确不要"钥匙数量重置"，任何重置都不碰）。
_RESET_FIELDS = {"server": "", "player_id": "", "level": 0}


def reset_account(account_id: int) -> bool:
    """单账号档案重置：区服/玩家ID/等级回默认值（凭据、绑定、数量字段不动）。"""
    return update_account(account_id, **_RESET_FIELDS)


def reset_all_accounts() -> int:
    """全表档案重置（右键「账号全部重置」），返回受影响行数。"""
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE accounts SET server='', player_id='', level=0,"
            " updated_at=datetime('now','localtime')")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_account(account_id: int) -> bool:
    """
    删除账号档（只删 accounts 这一行）。
    task_history 等流水表按 account_id 留存，
    不级联删除——历史记录是审计用的，账号档没了它还有归因价值。
    """
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def find_account_by_login(login: str) -> dict | None:
    """
    按「游戏账号」字段查账号档，没有返回 None。
    账号导入的 upsert 靠它判断"这个账号是新的还是已存在的"——
    已存在就更新原行（保住 id，task_history 的历史不断链），
    不存在才新增。id 变了 = 历史记录全成孤儿，这是导入功能最大的坑。
    """
    if not login:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM accounts WHERE account=? ORDER BY id LIMIT 1", (login,)
        ).fetchone()
        # 查不到返回 None（与文档口径一致）。以前走 _row_to_dict 会返回空字典 {}，
        # "真值判断"的调用方碰巧没事，但 "is not None" 的写法会永远为真——
        # 冒烟里就有一条因此一直是空断言（恒过），这次一并掰正
        return _row_to_dict(row) if row is not None else None
    finally:
        conn.close()


def find_account_by_login_and_index(login: str, ld_index: int) -> dict | None:
    """
    按「账号名 + 实例序号」联合键查账号档（多开同账号导入的 upsert 键）。

    为什么不用 find_account_by_login：一个游戏账号合法地绑多台实例（一号多开）时，
    库里就是多条 account 同名、ld_index 不同的记录。只按账号名查会把它们折叠成一条、
    互相覆盖绑定。联合键让"abc02 在实例 2"和"abc02 在实例 3"各认各的 id，
    重导时同名同序号才更新原行（历史不断链），不同序号则各立各行。
    """
    if not login:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM accounts WHERE account=? AND ld_index=? ORDER BY id LIMIT 1",
            (login, ld_index),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None
    finally:
        conn.close()


def is_banned(acct: dict | None) -> str | None:
    """账号字典（list_accounts/accounts_by_instance 的元素）→ 封号原因，
    没封号返回 None。与 set_banned 同一份 extra_json["ban"] 格式，读写同源。"""
    if not acct or acct.get("enabled", 1):
        return None
    try:
        ban = json.loads(acct.get("extra_json") or "{}").get("ban")
    except (ValueError, TypeError, AttributeError):
        ban = None
    return (ban or {}).get("reason", "") if isinstance(ban, dict) else ""


def set_banned(account_id: int, banned: bool, reason: str = "") -> bool:
    """封号标记（watch「封号停用」行命中 / 右键「解禁」共用一处，两边语义同源）：
    标记 = enabled 置 0 + 原因/时间写进 extra_json["ban"]；解禁 = 回 1 + 清该键。
    不动表结构（enabled 列本就为此类"用户意志开关"预留）；extra_json 里其它
    扩展字段合并保留，坏 JSON 当空字典重建（宁可丢标记也不炸写库）。"""
    conn = get_connection()
    try:
        row = conn.execute("SELECT extra_json FROM accounts WHERE id=?",
                           (account_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    try:
        extra = json.loads(row["extra_json"] or "{}")
        if not isinstance(extra, dict):
            extra = {}
    except (ValueError, TypeError):
        extra = {}
    if banned:
        extra["ban"] = {"reason": reason, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    else:
        extra.pop("ban", None)
    return update_account(account_id, enabled=1 if not banned else 0,
                          extra_json=json.dumps(extra, ensure_ascii=False))


def merge_account_assets(account_id: int, values: dict,
                         names: dict | None = None) -> dict:
    """把 {变量名: 数值} 并进账号资产（图色读数落库的唯一写入口，与 set_banned
    同驻 extra_json——读写同源，键格式只在这里定义一次）。

    names = {变量名: 资产显示名}（如 {"gold": "金币"}）：账本按显示名记，
    节点改了变量名账面不乱；没映射的变量原样用变量名当账本键。
    返回 {"written": {变量名: 数值}}——变量名对不上任何账号（没绑号/没导入）
    时 written 为空，调用方据此如实报"没存进去"，不静默吞读数。
    数值容 OCR 偶尔抠出的千分位逗号；坏值逐条丢（一个脏变量不连累整包）。
    ⚠ 读改写非原子：多开同账号（同 account_id）并发写会互相覆盖。
    一号多开是合法用法但资产两笔谁对说不清，先不做锁，撞上了再提。"""
    conn = get_connection()
    try:
        row = conn.execute("SELECT extra_json FROM accounts WHERE id=?",
                           (account_id,)).fetchone()
        if row is None:
            return {"written": {}}
        try:
            extra = json.loads(row["extra_json"] or "{}")
            if not isinstance(extra, dict):
                extra = {}
        except (ValueError, TypeError):
            extra = {}          # 坏 JSON 当空字典重建（set_banned 同款纪律）
        assets = extra.get("assets")
        if not isinstance(assets, dict):
            assets = {}
        written = {}
        for var, v in (values or {}).items():
            try:
                n = int(str(v).replace(",", "").strip())
            except (TypeError, ValueError):
                continue        # 没读到/读成非数字：宁缺勿错，不写脏值
            key = (names or {}).get(var) or var
            assets[key] = n
            written[var] = n
        extra["assets"] = assets
        extra["assets_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE accounts SET extra_json=?, updated_at=datetime('now','localtime')"
            " WHERE id=?",
            (json.dumps(extra, ensure_ascii=False), account_id))
        conn.commit()
        return {"written": written}
    finally:
        conn.close()


def get_account_assets(acct: dict) -> dict:
    """账号字典（accounts_by_instance 的元素）→ {资产名: 数量}，读自
    extra_json["assets"]（merge_account_assets 写的）。坏 JSON / 非字典 →
    空字典，绝不抛——表格每 3 秒刷一次，这里炸一次界面抖一次。"""
    try:
        a = json.loads((acct or {}).get("extra_json") or "{}").get("assets")
    except (ValueError, TypeError, AttributeError):
        return {}
    return a if isinstance(a, dict) else {}


def get_assets_updated_at(acct: dict) -> str:
    """账号字典 → 资产最后记账时间（merge_account_assets 写的
    extra_json["assets_updated_at"]），没有/坏 JSON → 空串。与
    get_account_assets 同纪律：展示层每 3 秒刷一次，这里绝不抛。"""
    try:
        v = json.loads((acct or {}).get("extra_json") or "{}").get("assets_updated_at")
    except (ValueError, TypeError, AttributeError):
        return ""
    return str(v) if v else ""


def accounts_by_instance() -> dict[int, dict]:
    """
    一次查出「每个实例绑的账号」：{实例序号: 账号字典}。

    表格每 3 秒刷一次，绝对不能一行查一次库（N 行 = N 次开连接）。
    一条 SQL 拿全表（list_accounts 已按 id 升序），Python 侧归并：
    同一实例挂多个账号时取 id 最小的那个——与 bot/worker.py 的
    _account_id_for 行为一致（它遍历 list_accounts() 取第一个命中）。
    两处规则必须同源，否则表格显示的账号和 worker 实际写库的账号会对不上。
    """
    result: dict[int, dict] = {}
    for acc in list_accounts():
        try:
            idx = int(acc.get("ld_index", -1))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx in result:   # 未绑定 / 该实例已有更小 id 的账号
            continue
        result[idx] = acc
    return result


def add_task_history(account_id: int, task_name: str, status: str,
                     start_at: str = None, end_at: str = None,
                     fail_reason: str = "", screenshot_path: str = "",
                     detail_json: str = "{}") -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO task_history (account_id, task_name, start_at, end_at,"
            " status, fail_reason, screenshot_path, detail_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (account_id, task_name, start_at, end_at, status,
             fail_reason, screenshot_path, detail_json),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# add_character_snapshot / add_mail_log 已删（2026-10-09）：对应表功能下线、
# 全项目零调用，见上方建表段注释。

# add_run_log 已删（2026-10-04）：run_log 表的写入函数全项目零调用，见上方建表注释。

# 本模块没有 __main__ 自测块：以前那个每次跑都往真实库插一条 test01
# （污染源头，2026-09 删）。DAO 验证走 tests/ 下的冒烟（临时库隔离），
# 账号导入/导出命令行在 core/account_io.py。
