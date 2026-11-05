"""
core/template_manager.py — 模板库管理（第 6 步；任务绑定改版）

模板 = 游戏 UI 元素的小截图 + 它的元数据（归属目录、匹配阈值、采集时间）。
PNG 图片存在 templates/<目录>/ 下，元数据统一记在 templates/index.json——
这个"账本"让程序知道库里有哪些模板、各自用什么阈值，游戏更新后也能按采集时间排查。

目录即任务（库点挂机目录改版后）：
  templates/ 一级目录 = 任务的中文显示名（登录游戏/挂机设置/自定义流程）。
  任务类用 required_templates 声明依赖哪些 (目录, 名字)，worker 跑前查缺；
  「哪个库对应哪个任务」= 任务类 category == 目录名，现场反查（template_deps）。
  用户「＋新增库」建一个与某任务显示名同名的中文目录，即成为该任务目录，
  直接可进任务清单挂机 —— 采集侧和任务侧用同一套中文名字，不再中英对齐。

本步学到的知识点：
1. 结构化数据存 JSON：模板元数据是"小、偶尔改、要整体读"的数据，
   用 index.json 比数据库更直观（还能用记事本打开看）
2. 文件与元数据的一致性：加模板 = 写 PNG + 更新账本两步；
   删模板 = 删 PNG + 删账本记录两步，必须成对出现
3. 时间戳记录：模板带采集日期，游戏版本更新后一眼看出哪些模板过期了
"""

import json
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from core import json_store
from core.capture import imwrite_cn, load_image_cn
from core.config_manager import PROJECT_ROOT

TEMPLATES_DIR = PROJECT_ROOT / "templates"
INDEX_PATH = TEMPLATES_DIR / "index.json"

# 默认分类（仅用于「全新安装零模板」时给下拉兜底；实际分类以 list_categories() 动态扫描为准）。
# 流程即任务改版后内置脚本任务已删，唯一任务目录 = flow 的「自定义流程」；
# 其余素材目录由用户自己「＋新增库」建、被流程引用后自动可入队，无需预置。
# 本表是 core 层不 import bot 的静态底牌，新增/改名任务时记得同步这里
#（忘了也无致命后果：只影响「零模板全新安装」的下拉初始项，采一张图目录就自己长出来了）。
CATEGORIES = ["自定义流程"]


def list_categories() -> list[str]:
    """
    动态分类：扫描 templates/ 下「含 PNG 的一级子目录」+ index.json 里出现过的
    category + 用户声明的空库（config.template_categories）的并集，
    去重排序；仅「全新安装（账本还不存在）」时才回退默认分类。
    只收有 PNG 的目录，避免历史上遗留的空目录（如 common/digit 等）被当作分类
    —— 但「用户主动新建、暂时还没采图」的空库是合法的，凭 config 里的登记出现，
    不开"任意空目录都算"的口子。分类由目录/账本/登记驱动，可任意新增。
    """
    cats: set[str] = set()
    # 1) 磁盘一级子目录（忽略隐藏目录 + 空目录）
    if TEMPLATES_DIR.exists():
        for p in TEMPLATES_DIR.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                if any(f.suffix.lower() == ".png" for f in p.iterdir()):
                    cats.add(p.name)
    # 2) 账本里出现过的 category（目录可能被手动删过，账本仍有记录）
    for t in load_index().get("templates", []):
        c = t.get("category")
        if c:
            cats.add(c)
    if not cats and not INDEX_PATH.exists():
        # 真正的「全新安装、从没用过」：连账本文件都没有 → 保底默认分类，
        # 让新用户下拉里直接有任务目录可选、能采图。
        # ⚠ 条件必须带「账本不存在」：只看扫描结果为空就兜底的话，用户把库全
        # 删空后（账本还在、只是空列表）兜底会把刚删掉的任务目录集体复活 ——
        # 与「删除空库即从下拉移除」的约定直接冲突（实测踩坑：删了还在下拉）。
        # 再减墓碑：用户明确删过的任务目录不许被兜底复活（全新安装后立刻全删的极端场景）。
        cats = set(CATEGORIES) - set(deleted_categories())
    # 3) 用户声明的自定义库（哪怕目录还是空的也要显示）
    cats.update(custom_categories())
    return sorted(cats)


# ── 自定义归属库（新增库功能）─────────────────────────────────────────
# 归属库 = templates/ 下的一级目录。任务目录由注册表驱动（bot/tasks），
# 用户还能手动建"非任务"的自定义库（如按活动、按版本分组收纳模板）。
# 显示名存 config.template_categories = {目录名: 显示名}，遵守"配置单一入口"；
# 目录本身在创建时就建好，空库也能在下拉框出现（靠这份登记，不靠磁盘扫描）。

def custom_categories() -> dict[str, str]:
    """用户声明的自定义库 {目录名: 显示名}；读不到配置返回空。"""
    from core import config_manager   # 延迟导入，避免初始化顺序问题
    # get_safe 类型防御：非 dict 的坏值直接按"没配"处理，不再手拧 try/isinstance
    raw = config_manager.get_config().get_safe("template_categories", {})
    return {str(k): str(v) for k, v in raw.items() if k}


def deleted_categories() -> list[str]:
    """被用户「删除库」清掉过的任务目录名（墓碑名单）。
    唯一用途：全新安装兜底（list_categories 第 2.5 步）整组塞默认分类时，
    把用户明确删过的排除掉 —— 否则「删空任务库→下拉移除」会在零模板状态下失效。
    采图/重建即除名（add_template / create_category），墓碑不阻挡真实内容。"""
    from core import config_manager
    raw = config_manager.get_config().get_safe("deleted_categories", [])
    return [str(x) for x in raw if x]


def _mark_category_deleted(dir_name: str):
    """删除库成功后登记墓碑（幂等，去重）。"""
    from core import config_manager
    cfg = config_manager.get_config()
    tomb = [c for c in (cfg.get("deleted_categories", []) or []) if c != dir_name]
    tomb.append(dir_name)
    cfg.set("deleted_categories", tomb)


def _clear_category_deleted(dir_name: str):
    """目录重新有内容/被重建 → 除名墓碑（幂等，无变化不写 config）。"""
    from core import config_manager
    cfg = config_manager.get_config()
    tomb = list(cfg.get("deleted_categories", []) or [])
    if dir_name in tomb:
        cfg.set("deleted_categories", [c for c in tomb if c != dir_name])


def _valid_category_name(name: str) -> str:
    """校验目录名，合法返回空串，非法返回错误原因（防路径穿越 + 防孤儿垃圾名）。"""
    if not name:
        return "库名不能为空"
    if len(name) > 40:
        return "库名太长（≤40 字符）"
    if name.startswith(".") or name in (".", ".."):
        return "库名不能以 . 开头"
    bad = '/\\:*?"<>|'
    if any(ch in name for ch in bad):
        return f"库名不能含这些字符：{bad}"
    if any(ord(ch) < 32 for ch in name):
        return "库名含不可见字符"
    return ""


def create_category(dir_name: str, display_name: str = "") -> tuple[bool, str]:
    """
    新增一个归属库（模板库目录）：建磁盘目录 + 登记显示名到 config。
    返回 (是否成功, 说明文本)。幂等保护：重名 / 非法名 一律拒绝；
    撞任务标识时——任务目录正活跃在下拉则拒，已被「删除库」清空（下拉看不到）
    则允许同名重建恢复。空库也会出现在 list_categories()（凭 config 登记）。
    """
    from bot.template_deps import task_dir_names   # 延迟导入：core 不硬依赖 bot
    from bot.tasks import TASKS   # 同上；取 display_name 给重建时的显示名

    dir_name = (dir_name or "").strip()
    display_name = (display_name or "").strip() or dir_name

    err = _valid_category_name(dir_name)
    if err:
        return False, err
    # 「是不是任务目录」按名字集合判（显示名 + 历史标识），目录改中文后
    # 老的 `dir_name in TASKS` 会静默判不出，见 template_deps.task_dir_names
    if dir_name in task_dir_names():
        # 任务目录正活跃（含图/账本有记录 → 在下拉里）：归属由注册表驱动，无需新建
        if dir_name in list_categories():
            return False, f"「{dir_name}」是已注册任务的目录，归属由任务自动决定，无需新建"
        # 已被「删除库」清空（下拉看不到）：允许同名重建，恢复该任务的采图入口。
        # 显示名以注册表为准，忽略用户输入（任务中文名不该被手填值覆盖）
        key = next((n for n, c in TASKS.items()
                    if n == dir_name or c.display_name == dir_name), dir_name)
        display_name = TASKS[key].display_name
    elif dir_name in custom_categories():
        return False, f"自定义库「{dir_name}」已存在"
    elif (TEMPLATES_DIR / dir_name).exists():
        return False, f"目录「{dir_name}」已存在（可能之前采过图）"

    # 第 1 步：建磁盘目录（add_template 也会 mkdir，这里提前建好让空库可见）
    try:
        (TEMPLATES_DIR / dir_name).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"创建目录失败：{e}"

    # 第 2 步：登记显示名到 config（单一入口，_deep_merge 自动给老配置补默认）
    from core import config_manager
    cfg = config_manager.get_config()
    reg = dict(cfg.get("template_categories", {}) or {})
    reg[dir_name] = display_name
    cfg.set("template_categories", reg)
    _clear_category_deleted(dir_name)   # 重建（含恢复被删任务目录）→ 除名墓碑
    return True, f"已新建库「{display_name}」（目录 {dir_name}）"


def delete_category(dir_name: str) -> tuple[bool, str]:
    """
    删除一个归属库目录：只删「空库」（里面没有任何模板）。
    任务目录 / 自定义库 / 孤儿目录都可删（清空之后）。
    返回 (是否成功, 说明文本)。

    安全设计（删目录是红线操作，防御全部拉满）：
    - 非空拒删：账本记录 + 磁盘 PNG 双查（账本可能滞后：手动丢进目录的图也算占用）
    - 用 os.rmdir 而不是 shutil.rmtree：内核保证只能删空目录，
      就算上面两个检查被绕过，也绝不可能连带删掉图片文件
    - 任务目录删了不影响任务本身：任务在侧栏由注册表驱动，与 templates 目录
      存在与否无关；下次往该目录采图，add_template 会自动重建目录（可恢复）
    - 删成功后从 config 自定义登记里除名（任务目录本就不在登记里，pop 是 no-op）
    """
    dir_name = (dir_name or "").strip()
    if not dir_name:
        return False, "库名为空"

    in_index = [t["name"] for t in list_templates() if t.get("category") == dir_name]
    if in_index:
        return False, (f"库「{dir_name}」还有 {len(in_index)} 个模板"
                       f"（{('、'.join(in_index[:5]))}{'…' if len(in_index) > 5 else ''}），"
                       f"请先删除或移走这些模板再删库")
    cat_dir = TEMPLATES_DIR / dir_name
    if cat_dir.exists():
        pngs = list(cat_dir.glob("*.png"))
        if pngs:
            return False, (f"目录「{dir_name}」里还有 {len(pngs)} 张 PNG"
                           f"（账本没记录，可能是手动放进去的），请先清走再删库")

    # 双查都空 → 真删。rmdir 对非空目录必然失败 = 最后一道内核级保险
    try:
        if cat_dir.exists():
            cat_dir.rmdir()
    except OSError as e:
        return False, f"删除目录失败：{e}"

    from core import config_manager
    cfg = config_manager.get_config()
    reg = dict(cfg.get("template_categories", {}) or {})
    reg.pop(dir_name, None)
    cfg.set("template_categories", reg)
    _mark_category_deleted(dir_name)    # 墓碑：零模板兜底不许把它复活（见 list_categories）
    return True, f"已删除库「{dir_name}」"


def set_category_display(dir_name: str, new_display: str) -> tuple[bool, str]:
    """
    修改库的显示名：只改 config.template_categories[dir_name] 这一个值。
    磁盘目录、index.json 账本、流程节点引用（全都按目录名存）一概不动 ——
    改名零断引用；界面统一走 task_dir_display() 取名，登记完即刻生效。
    未登记的孤儿目录（磁盘有图没登记）改名 = 顺手登记显示名，同口径。
    任务目录（注册表驱动，如「自定义流程」）拒改：它显示什么由任务类
    display_name 说了算，这里改会把「目录名=任务显示名」的约定搅乱。
    """
    from bot.template_deps import task_dir_names   # 延迟导入：core 不硬依赖 bot
    dir_name = (dir_name or "").strip()
    new_display = (new_display or "").strip()
    if not dir_name:
        return False, "库名为空"
    err = _valid_category_name(new_display)   # 显示名不落地成路径，但校验同源，
    if err:                                   # 将来想升级成真改目录也不用再写一套
        return False, f"新库名不合法：{err}"
    if dir_name in task_dir_names():
        return False, f"「{dir_name}」是任务目录，名字由任务注册表管理，不能改"
    if new_display in task_dir_names():
        return False, f"「{new_display}」是任务目录名，换一个"
    if dir_name not in custom_categories() and not (TEMPLATES_DIR / dir_name).exists():
        return False, f"库「{dir_name}」不存在"
    from core import config_manager
    cfg = config_manager.get_config()
    reg = dict(cfg.get("template_categories", {}) or {})
    reg[dir_name] = new_display
    cfg.set("template_categories", reg)
    return True, f"已把「{dir_name}」显示为「{new_display}」"


def load_index() -> dict:
    """读模板账本；不存在或损坏时返回空账本（防御式：账本坏了不崩程序）"""
    data = json_store.load_or_default(INDEX_PATH, {"templates": []})
    if isinstance(data, dict) and isinstance(data.get("templates"), list):
        return data
    return {"templates": []}


def _save_index(data: dict):
    """写账本（原子写入 + 历史快照，与 config_manager 流程资产同款保险丝）：
    覆写前旧账本自动存档到 templates/index_backup/（留 30 份），改坏了挑一份
    拷回覆盖 index.json 即恢复——账本缓存按 mtime 失效，程序运行中拷回也认。
    写完顺手把内存缓存换成本次结果：本进程自己刚保存的改动立刻可见，
    不用等 stat 节流窗口过期（UI"存一张图→马上列表刷新"的路径依赖这里）。"""
    global _stat_ts
    json_store.dump_with_history(
        INDEX_PATH, data, INDEX_PATH.parent / "index_backup", "index", keep=30)
    with _cache_lock:
        _cache["images"] = {}
        _cache["index"] = data.get("templates", [])
        _cache["mtime"] = _index_mtime()
        _stat_ts = time.monotonic()
    _stat_ts = 0.0   # 本进程刚写过：下一次读必须 stat 复检（节流戳清零）


# ── 内存缓存（实测性能修复）───────────────────────────────────────────
# worker 每 0.25 秒调一次 list_templates + load_template_image，
# 每次从磁盘读 index.json 和 PNG（被杀软扫描拖累时单次可达 0.45 秒），
# 会吃光 worker 循环预算。改为按 index.json 修改时间失效的内存缓存。
#
# 并发纪律（10 worker + UI 共享这份缓存）：
# - 失效检查与"重读-换索引-清图"整套收进 _cache_lock：账本一变，过去是
#   所有线程各挤进来重读一遍整份 JSON（缓存想省的 IO 反而 ×(N+1)），
#   现在单飞 —— 后进锁的看 mtime 已被改平直接返回。
# - stat() 按 _STAT_TTL 节流：Windows 上 stat 要开句柄查元数据，每 tick
#   每 worker 查 5~15 次纯属白烧。写账本的 _save_index 会把节流戳清零，
#   保证"本进程刚写的"最迟下一拍可见，节流不牺牲自家改动的及时性。
# - images 缓存不放锁：dict 单键 get/set 原子；最坏两个线程同时读同一张
#   PNG 各存一遍，结果相同，无害。
_cache: dict = {"index": None, "mtime": 0.0, "images": {}}
_cache_lock = threading.Lock()
_STAT_TTL = 0.5        # stat 复检节流（秒）
_stat_ts = 0.0         # 上次 stat 时刻（monotonic）


def _index_mtime() -> float:
    try:
        return INDEX_PATH.stat().st_mtime
    except OSError:
        return 0.0


def _refresh_cache_if_needed():
    global _stat_ts
    with _cache_lock:
        now = time.monotonic()
        if _cache["index"] is not None and now - _stat_ts < _STAT_TTL:
            return                     # 节流窗口内：不 stat，直接用缓存
        _stat_ts = now
        mtime = _index_mtime()
        if _cache["index"] is not None and _cache["mtime"] == mtime:
            return
        templates = load_index()["templates"]   # 磁盘 IO 在锁内单飞
        _cache["images"] = {}                   # 先废图缓存再换索引：读者
        _cache["index"] = templates             # 不会拿新索引配旧图
        _cache["mtime"] = mtime


def list_templates() -> list[dict]:
    """全部模板元数据（内存缓存，账本文件变化时自动失效）"""
    _refresh_cache_if_needed()
    return _cache["index"]


# ── 统一查找收口（任务↔模板绑定的公共入口）────────────────────────────
# 全项目按 (目录, 名字) 找模板只走这里一份实现，避免 fsm/worker/UI 各写一套
# 匹配逻辑漂移。目录 = 任务目录名；category 传 "" = 只按名字找（跨目录逃生口）。

def find_template(category: str, name: str) -> dict | None:
    """按 (目录, 名字) 找一条模板账本；category 传 "" 表示只按名字找。"""
    for t in list_templates():
        if t["name"] == name and (category == "" or t.get("category") == category):
            return t
    return None


def has_template(category: str, name: str) -> bool:
    """某模板是否存在（存在性判断，不加载图片）。"""
    return find_template(category, name) is not None


def check_missing(refs) -> list[tuple[str, str]]:
    """
    批量查缺：给定 [(目录, 名字), ...]（或含 category/name 键的 dict 列表），
    返回账本里找不到的引用。任务启动前预检用 —— 缺图当场列清单，
    而不是等挂机跑到那一步才 WaitStep 超时。
    """
    missing: list[tuple[str, str]] = []
    for ref in refs or []:
        if isinstance(ref, dict):
            cat, name = ref.get("category", ""), ref.get("name", "")
        else:
            cat, name = ref[0], ref[1]
        if name and not has_template(cat, name):
            missing.append((cat, name))
    return missing


def add_template(name: str, category: str, img: np.ndarray, threshold: float,
                 roi: list | None = None, click_target: list | None = None,
                 click_offset: list | None = None,
                 rgb_threshold: float | None = None) -> dict | None:
    """
    保存一个模板：PNG 写盘 + 账本登记（同名同分类 = 覆盖更新）。
    roi: 可选限定找图区域 [x0, y0, x1, y1]（安卓坐标空间，数字直填）——
    存了之后识别只在该区域内找，更快更精准；None = 全屏搜索。
    click_target: 可选固定点击坐标 [x, y]（安卓坐标空间）——
    识别到模板后点击此坐标而非模板中心；None = 点击模板中心。
    click_offset: 可选相对点击偏移 [dx, dy]（相对命中中心，弹窗会挪位置时用）——
    存进图本体 = 所有使用者（行/节点）没配自己的偏移时自动跟随这张图。
    rgb_threshold: 可选灰度命中后的 RGB 颜色校验门槛（0~1）——颜色置信度低于它
    的命中作废，拒绝"灰度形状像但颜色不同"的假阳性；None = 不校验（老行为）。
    返回新账本条目；分类为空返回 None（分类可为任意非空字符串，动态建目录）。
    """
    if not category:
        return None

    entry = {
        "name": name,
        "category": category,
        "file": f"{category}/{name}.png",
        "threshold": threshold,
        "width": int(img.shape[1]),
        "height": int(img.shape[0]),
        "roi": roi,  # 限定找图区域；None = 全屏搜索
        "click_target": click_target,  # 固定点击坐标；None = 点击模板中心
        "click_offset": click_offset,  # 相对点击偏移；None = 没存（点中心）
        "rgb_threshold": rgb_threshold,  # 颜色校验门槛；None = 不校验（老行为）
        "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 第 1 步：存 PNG
    png_path = TEMPLATES_DIR / entry["file"]
    png_path.parent.mkdir(parents=True, exist_ok=True)
    imwrite_cn(str(png_path), img)

    # 第 2 步：更新账本（同名的旧条目替换掉）
    data = load_index()
    data["templates"] = [
        t for t in data["templates"]
        if not (t["name"] == name and t["category"] == category)
    ]
    data["templates"].append(entry)
    _save_index(data)
    _clear_category_deleted(category)   # 目录重新有内容 → 除名墓碑（下拉恢复显示该库）
    return entry


def update_template_image(name: str, category: str, img: np.ndarray) -> bool:
    """替换某模板的图片本体（抠图/重抠用），账本只同步宽高，其余字段
    （阈值/roi/点击坐标/采集时间）一概不动 —— 这是与 add_template 的本质区别：
    add 是"重建整条账本"，重抠只该换图，不能让老配置悄悄清零。
    宽高必须跟着换：点击中心坐标 = 左上角 + 宽高一半，账本宽高与实际 PNG
    脱节会点偏。返回是否找到并更新（PNG 写盘失败也 False，账本不半改）。"""
    data = load_index()
    target = None
    for t in data["templates"]:
        if t["name"] == name and t["category"] == category:
            target = t
            break
    if target is None:
        return False
    png_path = TEMPLATES_DIR / target["file"]
    png_path.parent.mkdir(parents=True, exist_ok=True)
    if not imwrite_cn(str(png_path), img):
        return False
    # PNG 被就地覆盖但账本 mtime 可能不变（宽高相同则 _save_index 内容也没差），
    # 图片缓存不会自动失效 —— 必须显式弹掉这一条，否则 worker 继续拿老图匹配，
    # 重抠"看起来没生效"（同一 _cache 是模块级、UI 与 worker 共享，弹了就都读到新图）
    _cache["images"].pop(target["file"], None)
    target["width"] = int(img.shape[1])
    target["height"] = int(img.shape[0])
    _save_index(data)
    return True


def set_template_props(name: str, category: str, threshold: float,
                       roi: list | None, click_target: list | None,
                       click_offset: list | None = None,
                       rgb_threshold: float | None = None) -> bool:
    """一次改单个模板的识别属性（匹配阈值/找图区域/固定点击坐标/相对点击偏移/
    RGB 颜色校验门槛），供模板库右键「修改属性」对话框用；roi/click_target/
    click_offset/rgb_threshold 传 None = 清除该项。
    宽高/图片本体不在这里改（重采同名模板自动覆盖更新）。
    返回是否找到并更新（模板已被别处删除时 False）。"""
    data = load_index()
    found = False
    for t in data["templates"]:
        if t["name"] == name and t["category"] == category:
            t["threshold"] = float(threshold)
            t["roi"] = roi
            t["click_target"] = click_target
            t["click_offset"] = click_offset
            t["rgb_threshold"] = rgb_threshold
            found = True
    if found:
        _save_index(data)
    return found


def remove_template(name: str, category: str) -> bool:
    """删模板：账本记录和 PNG 文件成对删除"""
    data = load_index()
    kept = []
    removed = None
    for t in data["templates"]:
        if t["name"] == name and t["category"] == category:
            removed = t
        else:
            kept.append(t)
    if removed is None:
        return False
    data["templates"] = kept
    _save_index(data)
    png_path = TEMPLATES_DIR / removed["file"]
    if png_path.exists():
        png_path.unlink()
    return True


def load_template_image(entry: dict) -> np.ndarray | None:
    """按账本条目读出模板图片（内存缓存，账本变化时自动失效）。
    keep_alpha=True：抠过图的模板存的是带透明通道的 PNG，透明区 = 遮罩，
    读回来必须带着它，匹配引擎才认得出这是"只比一部分"的模板；
    没抠过的老模板本来就没有 alpha，读回来还是 3 通道，一切照旧。"""
    _refresh_cache_if_needed()
    key = entry["file"]
    # 负缓存：读不出来（PNG 丢失）的 None 也照缓存记一笔，用 `in` 判命中而不是
    # 拿值判 None——否则丢一个模板的引用每拍都重复磁盘探测（10  worker 每 0.25s
    # 一次，丢失图 = 无限开文件失败，纯白烧 IO）。账本变化时 images 整表清空自愈。
    if key in _cache["images"]:
        return _cache["images"][key]
    img = load_image_cn(str(TEMPLATES_DIR / key), keep_alpha=True)
    _cache["images"][key] = img
    return img


def rename_template(old_name: str, category: str, new_name: str) -> bool:
    """
    重命名模板：更新账本 name/file + 移动磁盘 PNG（成对，与 add/remove 同套路）。
    同分类下已有同名模板时拒绝（避免覆盖）。返回是否成功。
    """
    new_name = new_name.strip()
    if not new_name:
        return False
    data = load_index()
    for t in data["templates"]:
        # 目标名已存在 → 拒绝（同名同分类唯一）
        if t["name"] == new_name and t["category"] == category:
            return False

    target = None
    for t in data["templates"]:
        if t["name"] == old_name and t["category"] == category:
            target = t
            break
    if target is None:
        return False

    src = TEMPLATES_DIR / target["file"]
    # 先移 PNG（失败就不改账本，保证两者一致）
    if src.exists():
        dst = TEMPLATES_DIR / category / f"{new_name}.png"
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            src.replace(dst)  # replace 跨平台原子，源存在即可
        except OSError:
            return False

    target["name"] = new_name
    target["file"] = f"{category}/{new_name}.png"
    _save_index(data)
    return True


def move_template(name: str, from_category: str, to_category: str) -> bool:
    """
    移动模板到另一个库：账本 category/file + 磁盘 PNG 成对移动（与 rename 同套路，
    file 恒为 "<目录>/<名>.png" 约定）。目标库已有同名、同库互移、模板不存在
    一律拒绝。返回是否成功。
    """
    if not to_category or from_category == to_category:
        return False
    data = load_index()
    target = next((t for t in data["templates"]
                   if t["name"] == name and t["category"] == from_category), None)
    if target is None:
        return False
    if any(t["name"] == name and t["category"] == to_category
           for t in data["templates"]):
        return False

    src = TEMPLATES_DIR / target["file"]
    # 先移 PNG（失败就不改账本，保证两者一致）
    if src.exists():
        dst = TEMPLATES_DIR / to_category / f"{name}.png"
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            src.replace(dst)  # replace 跨平台原子，源存在即可
        except OSError:
            return False

    target["category"] = to_category
    target["file"] = f"{to_category}/{name}.png"
    _save_index(data)
    _clear_category_deleted(to_category)   # 目标库有了内容 → 除名墓碑
    return True


def batch_set_threshold(category: str | None = None, threshold: float = 0.85) -> int:
    """
    批量改阈值：category 传值只改该分类，None 改全部。返回改动条数。
    """
    data = load_index()
    n = 0
    for t in data["templates"]:
        if category is not None and t["category"] != category:
            continue
        t["threshold"] = float(threshold)
        n += 1
    if n:
        _save_index(data)
    return n


def export_index(path: str) -> bool:
    """
    导出账本到指定路径（连同 PNG 描述）。只导 index.json 的副本，便于备份/分享。
    目标路径已存在则覆盖。返回是否成功。
    """
    try:
        dst = Path(path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(INDEX_PATH, dst)
        return True
    except OSError:
        return False


def import_index(path: str, skip_existing: bool = False) -> int:
    """
    导入外部账本：读取 path 的 index.json，按「同名同分类覆盖」合并进当前账本，
    并复制其引用的 PNG 到 templates/<分类>/。返回导入的模板条目数。
    外部账本格式非法返回 0（防御式，不崩）。
    skip_existing=True：本机已有同名同分类的模板一概不碰（账本不替换、
    PNG 不覆盖），只补缺的条目 —— 流程包导入用（外来包不得无声覆盖本机
    手采资产，见 core/flow_pack）。本机账本有记录但 PNG 丢了时顺手把图补回。
    默认 False = 老行为一字不变（模板工具页的「导入账本」走默认）。
    """
    src = Path(path)
    try:
        with open(src, encoding="utf-8") as f:
            ext = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(ext, dict) or not isinstance(ext.get("templates"), list):
        return 0

    src_dir = src.parent  # 外部 PNG 相对该目录
    current = load_index()

    imported = 0
    for e in ext["templates"]:
        name, category = e.get("name"), e.get("category")
        file = e.get("file")
        if not name or not category or not file:
            continue
        dup = any(t["name"] == name and t["category"] == category
                  for t in current["templates"])
        if skip_existing and dup:
            png_src = src_dir / file if not Path(file).is_absolute() else Path(file)
            png_dst = TEMPLATES_DIR / file
            if not png_dst.exists() and png_src.exists():
                png_dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copyfile(png_src, png_dst)
                except OSError:
                    pass
            continue
        # 覆盖规则：先移除同名的旧条目
        current["templates"] = [
            t for t in current["templates"]
            if not (t["name"] == name and t["category"] == category)
        ]
        # 复制 PNG（源存在才复制，否则跳过但仍登记元数据——图由用户后补）
        png_src = src_dir / file if not Path(file).is_absolute() else Path(file)
        png_dst = TEMPLATES_DIR / file
        if png_src.exists():
            png_dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(png_src, png_dst)
            except OSError:
                pass
        current["templates"].append(e)
        imported += 1

    if imported:
        _save_index(current)
    return imported


if __name__ == "__main__":
    # 命令行自测：加一条 → 查 → 删 → 验证账本干净
    import numpy as np
    img = np.zeros((30, 60, 3), dtype=np.uint8)  # 一张纯黑测试图
    add_template("_test_tpl", "common", img, 0.85)
    names = [(t["name"], t["category"]) for t in list_templates()]
    print("添加后账本:", names)
    remove_template("_test_tpl", "common")
    names = [(t["name"], t["category"]) for t in list_templates()]
    print("删除后账本:", names)
    print("验证:", "通过" if ("_test_tpl", "common") not in names else "失败")
