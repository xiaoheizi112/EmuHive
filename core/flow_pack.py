"""
core/flow_pack.py — 流程包导出/导入（换电脑 / 整库传给装同款软件的朋友，导入即用）

包结构（.zip；包里的 flows.json 也可单独成一个文件 = 纯流程无图）：
    flows.json                     包标记 + config 三键全量（custom_flows /
                                   flow_categories / flow_category_order）
    templates/index.json           仅「被本包流程引用到」的账本子集，格式同真账本
    templates/<库目录>/<模板>.png  图片实体

包内模板子目录刻意照抄 core.template_manager.import_index 认识的样子
（「index.json + 同目录下的 分类/名.png」），导入直接借力它现成的合并逻辑，
不再自写一套 PNG 搬运 —— 两套搬运必漂移。

引用关系绝不持久化（铁律 4）：导出瞬间用 bot.template_deps.collect_refs()
现场扫「这些流程用哪些图」（子流程传递闭包已含）；对方机器导入后同样现场
派生。这里存进去的只有图本体，「谁在用」永远由扫描说话。

本模块零 Qt：同名流程覆盖还是跳过由调用方定策略（overwrite 参数），
文件对话框/弹窗全归 UI 层，这里只「文件进、结果字典出」。
"""

import json
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from core.config_manager import PROJECT_ROOT

PACK_KIND = "emuhive-flow-pack"


def export_dir(kind: str) -> Path:
    """导出/导入共用的默认落点：项目下「导出/<中文类名>」，没有就建。
    用户拍板（2026-10-04）：导出的东西一律进本项目对应文件夹、中文命名好认，
    下次导入也从同一文件夹挑，不用满硬盘找。"""
    d = PROJECT_ROOT / "导出" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_path(kind: str, ext: str) -> str:
    """默认完整文件名：<项目>/导出/<类名>/<类名>_日期.扩展名。"""
    return str(export_dir(kind) / f"{kind}_{time.strftime('%Y%m%d')}.{ext}")
PACK_VERSION = 1
FLOWS_ENTRY = "flows.json"
TPL_DIR = "templates"   # 包内模板子目录名（import_index 认「账本同级目录里的 PNG」）


def pack_flow_refs(flow_names=None) -> dict[str, set[tuple[str, str]]]:
    """
    流程名 → 它引用到的模板 {(目录, 名字)}。口径与删图确认完全同源
    （collect_refs 的 flow: 归属，含子流程传递闭包），不另起炉灶。
    flow_names=None = 全部流程。
    """
    from bot.template_deps import collect_refs   # 延迟导入：core 不硬依赖 bot
    out: dict[str, set[tuple[str, str]]] = {}
    for (cat, name), owners in collect_refs().items():
        for u in owners:
            if u.startswith("flow:") and (flow_names is None or u[5:] in flow_names):
                out.setdefault(u[5:], set()).add((cat, name))
    return out


def _validate_payload(data) -> dict:
    """flows.json 内容体检：不是包格式就报人话错（不静默吞）。"""
    if not isinstance(data, dict) or data.get("kind") != PACK_KIND:
        raise ValueError("不是本程序导出的流程包（缺包标记）")
    if not isinstance(data.get("custom_flows"), dict):
        raise ValueError("流程包里的 custom_flows 不是字典")
    return data


def _load_pack(path) -> tuple[dict, bool]:
    """读包 → (payload, 是否带模板图)。.zip / 纯 .json 都吃，内容非法抛 ValueError。"""
    p = Path(path)
    try:
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
            if FLOWS_ENTRY not in names:
                raise ValueError(f"流程包里缺 {FLOWS_ENTRY}")
            payload = _validate_payload(json.loads(zf.read(FLOWS_ENTRY).decode("utf-8")))
            has_tpl = any(n.startswith(TPL_DIR + "/") and n.lower().endswith(".png")
                          for n in names)
            return payload, has_tpl
    except zipfile.BadZipFile:
        # 不是 zip 就按纯 json 读（flows.json 单文件 = 只有流程无图）
        try:
            payload = _validate_payload(json.loads(p.read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"文件读不出来（既不是 zip 也不是合法 json）：{e}")
        return payload, False


def export_pack(path, with_templates=True) -> dict:
    """
    把当前 config 的流程三键打包写进 path（.zip）。with_templates=True 时
    连同被引用模板（账本子集 + PNG 实体）一起装进包。
    返回 {"flows": 流程数, "templates": 打进包的模板数, "missing_png": [(目录,名)...]}
    —— missing_png = 账本有记录但磁盘 PNG 已丢的引用（打包时没法带上）。
    """
    from core import template_manager
    from core.config_manager import get_config
    cfg = get_config()
    flows = cfg.get("custom_flows", {}) or {}
    payload = {
        "kind": PACK_KIND,
        "version": PACK_VERSION,
        "custom_flows": flows,
        "flow_categories": cfg.get("flow_categories", {}) or {},
        "flow_category_order": cfg.get("flow_category_order", []) or [],
    }
    result = {"flows": len(flows), "templates": 0, "missing_png": []}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(FLOWS_ENTRY, json.dumps(payload, ensure_ascii=False, indent=2))
        if with_templates and flows:
            refs: set[tuple[str, str]] = set()
            for s in pack_flow_refs().values():
                refs |= s
            name_only = {n for c, n in refs if not c}   # 跨目录逃生口引用：只按名命中
            entries = [t for t in template_manager.list_templates()
                       if t.get("name") and t.get("category") and t.get("file")
                       and ((t["category"], t["name"]) in refs or t["name"] in name_only)]
            zf.writestr(f"{TPL_DIR}/index.json",
                        json.dumps({"templates": entries}, ensure_ascii=False, indent=2))
            base = template_manager.TEMPLATES_DIR
            for t in entries:
                src = base / t["file"]
                if src.exists():
                    zf.write(str(src), f"{TPL_DIR}/{t['file']}")
                else:
                    result["missing_png"].append((t["category"], t["name"]))
            result["templates"] = len(entries) - len(result["missing_png"])
    return result


def peek_pack(path) -> dict:
    """
    不写任何东西地预读一个包：{"flows": [流程名...], "has_templates": bool}。
    UI 用它先算同名冲突、再决定问不问「覆盖/跳过」。
    """
    payload, has_tpl = _load_pack(path)
    return {"flows": list(payload["custom_flows"]), "has_templates": has_tpl}


def import_pack(path, overwrite=True) -> dict:
    """
    合并导入一个包（.zip / 纯 .json）到当前 config + 模板库。
    同名流程一次性处置（调用方已问过用户，这里只执行策略）：
      overwrite=True  包里版本覆盖本机同名流程
      overwrite=False 本机同名原样保留，包里的跳过
    新增流程照常进；分类只带上车流程的（包里没给分类的落未分类）；
    order 并集追加。带图的包把模板交给 template_manager.import_index 以
    skip_existing=True 合并 —— 同名模板不覆盖本机资产、只补缺。
    返回 {"added","overwritten","skipped","templates_merged",
          "missing_refs":[(目录,名)...]} —— missing_refs 是上车流程仍缺的图，
    导入后跑不起来属正常降级，UI 播报用。
    """
    from core import template_manager
    from core.config_manager import get_config
    payload, has_tpl = _load_pack(path)
    pack_flows = payload["custom_flows"]
    pack_cats = payload.get("flow_categories") or {}
    pack_order = [c for c in (payload.get("flow_category_order") or []) if c]

    cfg = get_config()
    flows = dict(cfg.get("custom_flows", {}) or {})
    cats = dict(cfg.get("flow_categories", {}) or {})
    order = [c for c in (cfg.get("flow_category_order", []) or []) if c]

    from bot.template_deps import UNCLASSIFIED   # 延迟导入（兜底组名不进分类账本）
    added = overwritten = skipped = 0
    landed: list[str] = []
    for name, graph in pack_flows.items():
        if name in flows:
            if overwrite:
                flows[name] = graph
                overwritten += 1
                landed.append(name)
            else:
                skipped += 1
        else:
            flows[name] = graph
            added += 1
            landed.append(name)
    # 分类账本只随上车的流程动；跳过的本机流程保持原分类
    for name in landed:
        if name in pack_cats:
            cats[name] = pack_cats[name]
        else:
            cats.pop(name, None)
    for c in dict.fromkeys(pack_order + list(pack_cats.values())):
        if c and c != UNCLASSIFIED and c not in order:
            order.append(c)

    templates_merged = _merge_pack_templates(Path(path)) if has_tpl else 0

    cfg.set("custom_flows", flows)
    cfg.set("flow_categories", cats)
    cfg.set("flow_category_order", order)
    cfg.save()

    # 缺图提醒：落账后对上车流程现场重扫（子流程闭包用的是本机现状）
    missing_refs: list[tuple[str, str]] = []
    if landed:
        flat = {r for s in pack_flow_refs(set(landed)).values() for r in s}
        missing_refs = template_manager.check_missing(sorted(flat))
    return {"added": added, "overwritten": overwritten, "skipped": skipped,
            "templates_merged": templates_merged, "missing_refs": missing_refs}


def _merge_pack_templates(zip_path) -> int:
    """把 zip 里的 templates/ 解到临时目录，整手交给 import_index
    （skip_existing=True：同名不覆盖本机资产、只补缺）。返回并入条目数。"""
    from core import template_manager
    tmp = Path(tempfile.mkdtemp(prefix="flow_pack_"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [n for n in zf.namelist() if n.startswith(TPL_DIR + "/")]
            if not members:
                return 0
            zf.extractall(str(tmp), members=members)
        idx = tmp / TPL_DIR / "index.json"
        if not idx.exists():
            return 0
        return template_manager.import_index(str(idx), skip_existing=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
