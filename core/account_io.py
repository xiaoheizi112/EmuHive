"""
core/account_io.py — 账号 TXT 导入 / 导出

把 accounts 表和 Excel 能编辑的 TXT 文件互相搬运（2026-09 起文件类型只收 .txt，
不再收 .csv——内部格式没变，还是分隔符表格文本，逗号/全角逗号/分号/制表符都认）：
    导出：数据库 → TXT（换电脑、备份、批量手工编辑）
    导入：TXT → 数据库（按「账号」字段 upsert，实例序号混合分配，
          覆盖式——文件即完整名单，没写的账号删除并自动备份到 data/backups/）

为什么不放 database.py：
database.py 只管"和 SQLite 打交道"（DAO），文件格式是另一件事。
把解析混进去，以后换 JSON/Excel 就得改数据层。分开后：
    database.py  存哪、怎么存
    account_io.py 长什么样、怎么读写文件

本模块的知识点：
1. utf-8-sig —— 带 BOM 的 UTF-8。Excel 打开不带 BOM 的 UTF-8 文本会把中文
   当成乱码（它按系统本地编码猜），写 BOM 就是告诉 Excel"我是 UTF-8"。
   读的时候 utf-8-sig 会自动吃掉 BOM，不留脏字符
2. gbk 回退 —— Excel"另存为"文本默认存成 ANSI（中文系统 = GBK）。
   导入时先试 utf-8-sig，炸了再试 gbk，两种来源都吃得下
3. csv.DictReader —— 按表头名取值，不依赖列的顺序。
   用户手工把「区服」列拖到前面也不会读错
4. upsert（有则更新、无则插入）—— 按「账号」字段判断。
   已存在就更新原行保住 id，因为 task_history 等历史记录
   都靠 account_id 挂靠，id 一变历史记录全成孤儿
5. 防御式解析 —— 坏行（列数不对、数字栏填了文字）跳过并记行号，
   不让一行脏数据毁掉整批导入

命令行自测：
    python -m core.account_io export [输出.txt]
    python -m core.account_io import <输入.txt>
"""

import csv
import time
from pathlib import Path

from core import database

# 文件列 = 超级列表框列：同一套名字、同一个顺序。档案列在前（用户填、导入入库），
# 运行时列在后（程序每 3 秒现算，文件里写什么都忽略，只为让文件长得跟表格一样）。
# ⚠ 这里的顺序必须和 ui/account_panel.py 的表头一字不差，改一边同步另一边。
# (表头, 数据库字段)；字段为 None = 运行时列，导入直接忽略。
# ⚠ 「序号」口径：文件写的数字 = 雷电模拟器编号 = 内部 ld_index = 表格显示，
# 全链路直通零换算，但一律从 1 起：0 号母体（克隆源）不进本工具的世界——
# 表格不显示、补位不落到它、文件写 0 视同没填（自动补位到 ≥1）。
FILE_COLUMNS: list[tuple[str, str | None]] = [
    ("序号", "ld_index"),
    ("备注", "name"),
    ("账号", "account"),
    ("区服", "server"),
    ("等级", "level"),
    ("运行任务", None),
    ("当前动作", None),
    ("完成度", None),
    ("当前模拟器", None),
    ("状态", None),
    ("挂机状态", None),
    ("已运行", None),
    ("资产", None),   # 运行时列：「资产入库」节点现算，文件里写什么都不生效
]
HEADERS = [h for h, _ in FILE_COLUMNS]
RUNTIME_HEADERS = {h for h, f in FILE_COLUMNS if f is None}

# 档案列表头 → 数据库字段（含老文件别名，如「实例序号」「备注名」）。
# 运行时列不在这里 = 导入认不出来自动跳过。密码/玩家ID/启用/战力/钥匙/箱子
# 不在 13 列里但照旧认得：想导就在表尾自己加一列（按列名匹配，位置无所谓）。
_IMPORT_FIELDS = {
    "序号": "ld_index", "序": "ld_index", "实例序号": "ld_index",
    "备注": "name", "备注名": "name",
    "账号": "account", "区服": "server", "等级": "level",
    "密码": "password", "玩家ID": "player_id", "启用": "enabled",
    "战力": "combat_power", "钥匙": "keys", "箱子": "boxes",
}
# 需要转类型的字段（其余按字符串）。int 字段遇到"待读取"这种非数字 → 0（留空）
_FIELD_TYPE = {
    "ld_index": "int", "level": "int", "combat_power": "int",
    "keys": "int", "boxes": "int", "enabled": "bool",
}

# 分隔符嗅探 / 表头校验用的"认识的名字"全集：13 列 + 所有别名
_KNOWN_HEADERS = set(HEADERS) | set(_IMPORT_FIELDS)

# 不导出的字段：id（自增主键，导了会和 upsert 打架）、
# status / current_task（运行时状态，导出那一刻就过期了）、
# extra_json / created_at / updated_at（内部字段，人看不懂也不用改）

# 「启用」列认这些写法（手工编辑文件时谁也不会只写 1/0）
_TRUE_WORDS = {"1", "是", "启用", "true", "yes", "y", "开"}
_FALSE_WORDS = {"0", "否", "停用", "false", "no", "n", "关", ""}


class ImportError_(Exception):
    """导入失败（文件读不出来、没有表头、一列都对不上）"""


def _to_int(value, default: int = 0) -> int:
    """字符串转整数；转不动（空/文字/小数）就用默认值，不抛异常"""
    s = str(value or "").strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))      # "3.0" 这种也认
        except ValueError:
            return default


def _to_bool(value, default: int = 1) -> int:
    """「启用」列转 1/0；认得 是/否/true/false/启用/停用 等写法"""
    s = str(value or "").strip().lower()
    if s in _TRUE_WORDS:
        return 1
    if s in _FALSE_WORDS:
        return 0
    return default


def _convert(field: str, value):
    """按列定义的类型转换单个值"""
    kind = _FIELD_TYPE.get(field, "str")
    if kind == "int":
        if field == "ld_index":
            # 「序号」= 雷电模拟器编号直通，但从 1 起：0 是母体（克隆源），
            # 本工具不加载/不绑定/不显示它——文件里写 0 视同没填，走自动补位。
            # 留空/非数字/≤0 → None 走自动补位
            s = str(value or "").strip()
            if not s:
                return None
            n = _to_int(s, -1)
            return None if n <= 0 else n
        return _to_int(value, 0)
    if kind == "bool":
        return _to_bool(value, 1)
    return str(value or "").strip()


# ── 导出 ─────────────────────────────────────────────────────────────

def export_accounts(path, with_password: bool = True, rows: list[dict] | None = None) -> dict:
    """
    把 accounts 表全部写成 TXT（逗号分隔表格文本）。

    with_password=False 时密码列写 ******（脱敏导出）。
    默认 True = 明文导出（按需求选的），但这是"能直接登录你游戏账号"的文件，
    调用方应该把路径回报给用户，让他知道东西落在哪。

    rows 传入现成账号字典列表时只写这些行（覆盖式导入删旧账前的备份走这条路，
    格式与正常导出完全一致 = 备份文件能直接拖回界面还原）。

    返回 {"count": 导出条数, "path": 实际写入路径}
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if rows is None:
        rows = database.list_accounts()
    # 导出列 = 表格 13 列 + 表尾「密码」（密码不在表格上，但备份账号总得有地方放；
    # 导入认得这一列，改完拖回来照常生效）
    headers = HEADERS + ["密码"]
    # utf-8-sig：写 BOM，Excel 双击打开中文不乱码；newline="" 防 Windows 多空行
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for acc in rows:
            line = []
            for header, field in FILE_COLUMNS:
                if field is None:
                    line.append("—")   # 运行时列：core 层看不到 worker 状态，占位
                    continue
                if field == "ld_index":
                    # 序号直通（和 _convert 入口成对）：-1 未绑 → 留空；
                    # 库里残留的 0（母体，历史数据）也导出留空 → 重导自动挪离 0
                    li = acc.get("ld_index", -1)
                    line.append(str(li) if isinstance(li, int) and li >= 1 else "")
                    continue
                line.append("" if acc.get(field, "") is None else acc.get(field, ""))
            line.append(acc.get("password", "") if with_password else "******")
            writer.writerow(line)

    return {"count": len(rows), "path": str(path)}


# ── 导入 ─────────────────────────────────────────────────────────────

# 认得的列分隔符：半角逗号、全角逗号「，」（记事本开中文输入法敲出来的）、
# 分号、制表符（从 Excel 直接复制粘贴存 txt 就是它）
_DELIMS = [",", "，", ";", "\t"]


def _sniff_delim(first_line: str) -> str:
    """按表头认分隔符：第一行用每个候选拆开，谁拆出的已知列名多就用谁。
    比 csv.Sniffer 靠谱——Sniffer 压根不认全角逗号（实测），而且对
    只有一两列的小文件经常猜错。全都拆不出已知列名时回退半角逗号，
    让上层「表头对不上」的报错去处理。"""
    best, best_hits = ",", -1
    for d in _DELIMS:
        try:
            cells = next(csv.reader([first_line], delimiter=d))
        except csv.Error:
            continue
        hits = sum(1 for c in cells if c.strip() in _KNOWN_HEADERS)
        if hits > best_hits:
            best, best_hits = d, hits
    return best


def _read_table_txt(path) -> list[dict]:
    """
    读文件（逗号/制表符等分隔的表格文本）成 [{中文表头: 值}, ...]。
    编码先试 utf-8-sig（本程序导出的）再试 gbk（Excel 另存为 ANSI 的）。
    """
    path = Path(path)
    if not path.exists():
        raise ImportError_(f"文件不存在：{path}")

    last_err = None
    for encoding in ("utf-8-sig", "gbk"):
        try:
            with open(path, encoding=encoding, newline="") as f:
                head = f.readline()
                f.seek(0)
                return list(csv.DictReader(f, delimiter=_sniff_delim(head)))
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise ImportError_(f"文件编码认不出来（试过 utf-8-sig / gbk）：{path}，{last_err}")


def _backup_accounts(rows: list[dict]) -> str | None:
    """
    覆盖式导入删旧账前，把要删的行原样写进 data/backups/账号备份_时间戳.txt。

    为什么必须备份：删掉的账号档连同它的密码/区服就没了，而"文件没写=不要了"
    这个判断完全依赖用户拖对文件——拖错一份小抄就会清空整库。备份是唯一的后悔药，
    且走的就是 export_accounts 同一条路，备份文件本身即标准导入格式，
    拖回界面即可还原。

    写盘失败（磁盘满/权限）不阻断导入：宁可删了没备份，也不要备份失败就导入不了。
    """
    from core.config_manager import PROJECT_ROOT
    try:
        out_dir = PROJECT_ROOT / "data" / "backups"
        path = out_dir / f"账号备份_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        return export_accounts(path, rows=rows)["path"]
    except Exception:
        return None




def import_accounts(path, allocate: bool = True, replace: bool = True) -> dict:
    """
    读账号 TXT 文件写进 accounts 表。

    四条规则（和需求确认过的一致）：
    1. upsert 认人：手填序号的行按「账号名+序号」联合键（一号多开各占各位、
       重导不堆重复行）；留空补位的行按「账号」字段。命中就更新原行
       （保住 id，历史不断链），没命中才新增
    2. 实例序号混合分配：填了就用填的（直通雷电编号，不做重号/超范围检查——
       用户明确要求导入即完成任务）；留空按顺序补给还没绑账号的实例；
       实例用完了记 -1（未绑定，表格里看不到但导出还在，数据不丢）
    3. 坏行跳过并记行号，不整体失败
    4. 覆盖式（replace=True，默认）：文件就是完整名单——本批一条都没对上的
       库行从库里删除（只删账号档，任务历史留存）。认行按「本批确认过的行 id」
       而不是账号名：同名多开的旧行也是独立一行，文件收回某台后这台上的旧行
       就是幽灵，必须删——按名豁免会让它们活着遮掉新行（表格同机取最小 id，
       用户实测报障"改备注改账号名没生效"即此）。删除前自动把旧名单
       备份到 data/backups/，日志会报备份路径。
       ⚠ 所以拖进来的文件必须是全量名单；只写几个号的"小抄"会把其余清掉

    allocate=False 时完全不碰实例序号（只导账号资料，保留原有绑定关系）。

    返回 {"created": n, "updated": n, "unbound": n, "skipped": [(行号, 原因)],
          "deleted": [账号名], "backup": 备份路径或 None,
          "blocked_hint": [被屏蔽实例的序号], "path": str}
    """
    rows = _read_table_txt(path)
    if not rows:
        raise ImportError_(f"文件里没有数据行（或表头认不出来）：{path}")

    # 表头对不上就直接报错：宁可让用户知道格式不对，也不要静默导出一堆空账号
    # （认识的名字 = 13 列 + 老别名 + 密码等表尾列；运行时列命中也算认识，只是不入库）
    got = {k.strip() for k in rows[0].keys() if k}
    matched = got & _KNOWN_HEADERS
    if not matched:
        raise ImportError_(
            f"表头对不上，需要这些列名（或其中几个）：{HEADERS}；文件里是：{sorted(got)}")

    from core import ldplayer
    blocked = ldplayer.blocked_indexes()

    # 空闲实例序号池：循环前一次快照（库全表扫 + list_instances 各一次）。
    # 旧版每行现开库重扫（N+1，导几百行几百次全表扫）。行消费掉一个序号就
    # 从池里 discard（自动补位取最小、手填序号也会占位），循环内零 IO。
    # list_instances 已在单一卡点排除 0 号母体/屏蔽台，补位天然从 1 起。
    free_pool: set[int] = set()
    if allocate:
        bound = set(database.accounts_by_instance().keys())
        free_pool = {i.index for i in ldplayer.list_instances() if i.index not in bound}

    created = updated = unbound = 0
    skipped: list[tuple[int, str]] = []
    touched_blocked: set[int] = set()
    occupied: set[int] = set()       # 本次已占用的实例序号（自动补位排除用）
    # 本批真正落过的库行 id（覆盖式收尾唯一豁免名单）。⚠ 不能按账号名豁免：
    # 多开收缩时同名旧行也是独立行——文件只剩 abc02@2 一台，库里 abc02 名下
    # 另外 7 行必须当幽灵删掉；按名豁免会让它们活着（id 更小）遮掉新导的行，
    # 表格看起来"改了没生效还是原数据"（用户实测报障即此）。
    touched_ids: set[int] = set()

    # DictReader 的行号：第 1 行是表头，数据从第 2 行开始
    for offset, row in enumerate(rows):
        line_no = offset + 2
        row = {(k or "").strip(): v for k, v in row.items()}

        login = str(row.get("账号", "") or "").strip()
        if not login:
            skipped.append((line_no, "「账号」列为空"))
            continue

        # 按列名映射入库：运行时列（_IMPORT_FIELDS 里没有）自然被跳过，
        # 文件里写"未运行/已完成"之类的字面不影响表格显示的程序实时状态
        fields = {}
        for header, field in _IMPORT_FIELDS.items():
            if header not in row:
                continue                     # 文件里没这一列 → 不覆盖库里原值
            fields[field] = _convert(field, row.get(header))
        fields["account"] = login

        # ── 实例序号：混合分配 ──
        # 不做重号/超范围检查（用户明确要求"导入即完成任务"）：文件写几就绑几，
        # 机器还没建也先存着，建好后自动显形。None = 文件没填 → 自动补位空闲实例
        raw_idx = fields.get("ld_index")      # None = 文件没填序号
        idx = raw_idx if raw_idx is not None else -1
        if not allocate:
            idx = -2                          # 哨兵：完全不动绑定关系
        elif idx < 0:
            cands = free_pool - occupied      # 前几行补走的位不再复读
            idx = min(cands) if cands else -1  # 没空位了 → 未绑定

        if idx >= 0:
            occupied.add(idx)
            free_pool.discard(idx)            # 增量维护：手填/补位消费的位一并出池
            if idx in blocked:
                touched_blocked.add(idx)
        elif idx == -1:
            unbound += 1
        if idx != -2:
            fields["ld_index"] = idx

        # upsert 身份键：手填了序号的行按「账号名+序号」联合键认人 ——
        # 一号多开（abc02 占序号 2~8 七行）各认各的位，重导不堆重复行。
        # 联合键查不到时回退按账号名找"改了序号"的老行：它占的位没被本批前面的行
        # 要走 → 原地更新（挪位保住 id 和历史）；被要走（多开各占各位）→ 另立新行。
        # 留空自动补位的行纯按账号名：补位每轮可能落不同序号，联合键会把
        # "重导同一份"误判成新账号、留下旧行变幽灵。
        if raw_idx is not None and idx >= 0:
            existing = database.find_account_by_login_and_index(login, idx)
            if existing is None:
                cand = database.find_account_by_login(login)
                if cand is not None and cand["ld_index"] not in occupied:
                    existing = cand
        else:
            existing = database.find_account_by_login(login)
        try:
            if existing:
                database.update_account(existing["id"], **fields)
                touched_ids.add(existing["id"])
                updated += 1
            else:
                touched_ids.add(database.add_account(**{k: v for k, v in fields.items()
                                                        if k != "id"}))
                created += 1
        except Exception as e:                # 单行写库失败不毁整批
            skipped.append((line_no, f"写库失败：{e}"))

    # ── 覆盖式收尾：本批没对上的库行 = 用户已经不要的，删（删前备份）──
    # 认行按 touched_ids（本批真正 update/add 过的行 id），不按账号名：
    # 多开收缩（文件从 abc02@1~8 收回一人一台）时，同名旧行是独立的一台，
    # 按名豁免会让它们幽灵活着，且旧行 id 更小 → accounts_by_instance 取最小
    # id → 新导的行被整个遮掉，表格看起来"改了没生效还是原数据"（实测报障）。
    deleted: list[str] = []
    backup: str | None = None
    # 一行都没对上位 = 大概率整批写库炸了（库被锁等），此时名单不可信，
    # 绝不删——否则一次故障导入就把整库清空（备份也救不了这种误报）
    if replace and (created + updated) > 0:
        stale = [a for a in database.list_accounts() if a["id"] not in touched_ids]
        if stale:
            backup = _backup_accounts(stale)
            for a in stale:
                # 空账号名的行（手工库里可能有）也一并清：它不在任何批次里
                database.delete_account(a["id"])
                name = a.get("account") or f"(id={a['id']})"
                # 带上机号：多开收缩时同名幽灵行有好几条，不点名分不清删了谁
                idx = a.get("ld_index", -1)
                deleted.append(f"{name}@{idx}" if isinstance(idx, int) and idx >= 0 else name)

    return {
        "created": created,
        "updated": updated,
        "unbound": unbound,
        "skipped": skipped,
        "deleted": deleted,
        "backup": backup,
        "blocked_hint": sorted(touched_blocked),
        "path": str(path),
    }


def format_import_report(result: dict) -> list[tuple[str, str]]:
    """
    把 import_accounts 的返回值翻成人话日志行：[(级别, 文本), ...]。
    界面直接遍历喂给日志面板，不用自己拼字符串。
    """
    lines = [("INFO", f"导入完成：新增 {result['created']}，更新 {result['updated']}"
                      f"，未绑定实例 {result['unbound']}")]
    deleted = result.get("deleted") or []
    if deleted:
        # 覆盖式导入删了号必须说响：拖错文件时这是唯一的现场报警
        lines.append(("WARN", f"覆盖导入：文件里没有的 {len(deleted)} 个账号已删除："
                              f"{'、'.join(deleted)}"))
        if result.get("backup"):
            lines.append(("WARN", f"旧账号已备份 → {result['backup']}"
                                  f"（需要还原就把这个文件拖回列表框）"))
        else:
            lines.append(("ERROR", "⚠ 删除前备份写入失败，这批被删账号无法找回"))
    for line_no, reason in result["skipped"]:
        lines.append(("WARN", f"第 {line_no} 行跳过：{reason}"))
    if result["blocked_hint"]:
        lines.append(("WARN", f"这些账号绑到了屏蔽中的实例 {result['blocked_hint']}，"
                              f"表格里暂时看不到（解除屏蔽后出现）"))
    return lines


# ── 命令行自测 ──
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法：python -m core.account_io <export [输出.txt] | import 输入.txt>")
        sys.exit(1)

    database.init_db()
    cmd = sys.argv[1]
    if cmd == "export":
        out = sys.argv[2] if len(sys.argv) > 2 else "accounts_export.txt"
        r = export_accounts(out)
        print(f"已导出 {r['count']} 条 → {r['path']}")
        print("⚠ 密码是明文，这个文件等于账号密码清单，别放网盘同步目录")
    elif cmd == "import":
        if len(sys.argv) < 3:
            print("导入要给出文件路径")
            sys.exit(1)
        r = import_accounts(sys.argv[2])
        for level, text in format_import_report(r):
            print(f"[{level}] {text}")
    else:
        print(f"未知命令：{cmd}")
        sys.exit(1)
