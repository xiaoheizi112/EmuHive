"""
bot/template_deps.py — 模板↔任务 关系派生（任务绑定改版）

账本(index.json)里只存模板的"物理住址"(category = templates/ 下的一级目录名)。
"这个模板被哪些任务用"不写进账本 —— 写死两份必然漂移，不如每次现算：

  归属 1（声明）：任务类 required_templates 直接点名 [(目录, 名字), ...]
  归属 2（约定）：任务 category == 模板目录 → 整目录归该任务
                  （活动领奖下线后这条只剩兜底作用：目录名与任务 category
                   一致时，里面新采的图自动归该任务，采集一个零代码生效）
  归属 3（配置）：config 的 custom_flows 节点树里显式写了 (category, name)
                  引用（含 branch 条件、debug_print）的，flow 认领

三类合并去重 = 完整使用者清单。
"""

# 非任务但值得展示中文名的虚拟归属者（暂无成员，保留结构）
_EXTRA_DISPLAY = {}


def task_dir_display() -> dict[str, str]:
    """
    目录/标识 → 界面显示名：{任务标识: display_name, 任务目录名: display_name,
    自定义库: 登记名}。
    库点挂机目录改版后任务目录名 = 中文显示名，同名映射也要显式登记 ——
    UI 侧统一 names.get(x, x) 的兜底虽然恰好也对，但别依赖巧合。
    自定义库（用户经「新增库」建的，登记在 config.template_categories）排在
    最后并入 —— 任务目录优先，理论上 create_category 已挡重名，这里再兜一层。
    """
    from bot.tasks import TASKS
    from core import template_manager
    out = {name: cls.display_name for name, cls in TASKS.items()}
    out.update({cls.category: cls.display_name for cls in TASKS.values()})
    out.update(_EXTRA_DISPLAY)
    out.update(template_manager.custom_categories())
    return out


def task_dir_names() -> set[str]:
    """
    一切「算任务目录」的名字集合 = 各任务的中文显示名（现行目录命名）
    ∪ 任务标识（历史兼容：旧迁移脚本/老账本里可能出现的 login/story 等）。
    core/template_manager.create_category 防撞、ui/template_tool 删库确认
    判断"这是任务目录"都以此为准 —— 别再各自写一份 in TASKS，目录改中文后
    那种检查会静默失效（中文名不在 TASKS 键里）。
    """
    from bot.tasks import TASKS
    out = set(TASKS)
    out |= {cls.display_name for cls in TASKS.values()}
    return out


def flow_ref_summary() -> dict[str, set[str]]:
    """
    流程名 → 这条流程引用到的模板库集合（侧栏悬停提示用）。
    与 collect_refs 同一份口径现场派生、不落账本（两份必漂移）。
    空串的 category（节点 category="" 的跨目录逃生口）不进结果，没展示价值。

    历史：上一版这里曾是 library_bindings()/category_task_map()
    （库→任务的入队闸门换算），侧栏/清单改成直接列流程后失去全部消费者，
    已随那版改版一并删除。
    """
    out: dict[str, set[str]] = {}
    for (cat, _name), owners in collect_refs().items():
        if not cat:
            continue
        for u in owners:
            if u.startswith("flow:"):
                out.setdefault(u[5:], set()).add(cat)
    return out


def _iter_flow_nodes(nodes: list):
    """递归产出 custom_flows 步骤树里的所有节点（branch 的 true/false 子链也进）。"""
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        if n.get("type") == "branch":
            yield from _iter_flow_nodes(n.get("true", []) or [])
            yield from _iter_flow_nodes(n.get("false", []) or [])


def iter_all_flow_nodes(flow: dict):
    """
    一份流程 dict → 所有引用模板的节点，v1/v2 通吃：
      v1：{"steps": [...]}，branch 子链嵌套 → _iter_flow_nodes 递归展开
      v2：{"nodes": [...], "edges": [...]}（流程编辑器现格式），全部节点平铺在
          nodes 里，branch 子节点经 true/false 边挂接、无嵌套 → 直接迭代
    collect_refs（读侧）与 rewrite_template_name（写侧）共用这一份展开逻辑，
    防止"读侧认识 v2、写侧只改 v1"这种两侧各写一套必然漂移的坑。
    """
    flow = flow or {}
    yield from _iter_flow_nodes(flow.get("steps") or [])
    for n in flow.get("nodes") or []:
        if isinstance(n, dict):
            yield n


def flow_backrefs() -> dict[str, set[str]]:
    """
    被引用流程名 → 把它当子流程用的流程名集合（主/子角色派生，侧栏 🔗 徽标用）。
    与 flow_ref_summary 同纪律：现场扫 custom_flows 派生、绝不落账本。
    含自引用（A 的子流程节点指向 A 自己 —— 编译期成环会降级，但"被引用"属实）。
    """
    from core.config_manager import get_config
    out: dict[str, set[str]] = {}
    for fname, fl in (get_config().get("custom_flows", {}) or {}).items():
        for n in iter_all_flow_nodes(fl):
            for tgt in node_flow_refs(n):
                out.setdefault(tgt, set()).add(fname)
    return out


# 手动分类的兜底组名：没有分类记录的流程全归这里。空分类表时侧栏只出这一组，
# 观感与未分组的平铺列表几乎一致 —— 功能可以静默上线，不用先教育用户。
UNCLASSIFIED = "未分类"


def flow_category_groups() -> list[tuple[str, list[str]]]:
    """
    [(分类名, [流程名, ...]), ...] —— 侧栏/编辑器下拉共用的分组口径。

    分类 = 手动标签（config.flow_categories，{流程名: 分类名}），故意与流程值
    dict 分开存：_on_save 每次用 ensure_graph 整体替换图 dict，塞值里的字段
    会被保存冲掉。组顺序 = config.flow_category_order（用户建分类/重排写这里，
    空组也保留），没登记的分类按首次出现兜底排其后，未分类恒最后。
    """
    from core.config_manager import get_config
    cfg = get_config()
    flows = list((cfg.get("custom_flows", {}) or {}).keys())
    cat_of = cfg.get("flow_categories", {}) or {}
    order = [c for c in (cfg.get("flow_category_order", []) or []) if c]
    buckets: dict[str, list[str]] = {}
    for f in flows:
        buckets.setdefault(cat_of.get(f) or UNCLASSIFIED, []).append(f)
    # 登记表里的空分类也要出现（拖空的组不消失，和文件夹习惯一致）
    for c in order:
        buckets.setdefault(c, [])
    groups = [(c, buckets[c]) for c in order if c in buckets]
    groups += [(c, v) for c, v in buckets.items()
               if c not in order and c != UNCLASSIFIED]
    groups.append((UNCLASSIFIED, buckets.get(UNCLASSIFIED, [])))
    return groups


def watch_rows(node: dict) -> list[dict]:
    """
    watch/timer 节点 → 它携带的全部监视行（原始 dict 引用，可就地改写）。

    多监视合并改版：容器节点的行藏进 groups（[{id, rows, ...}, ...]），顶层
    rows 只在老格式/未容器化节点上有。读行若只认顶层，容器里的引用就全瞎 ——
    删图确认说"没人用"、哨兵行静默变死行（popup_zone 同款教训）。
    展开逻辑只此一份，引用扫（读侧）与改名传播（写侧）共用，防两处漂移。
    """
    groups = node.get("groups")
    if isinstance(groups, list):
        out = []
        for g in groups:
            if isinstance(g, dict):
                out.extend(r for r in (g.get("rows") or []) if isinstance(r, dict))
        return out
    return [r for r in (node.get("rows") or []) if isinstance(r, dict)]


def branch_cond_items(node: dict) -> list[dict]:
    """
    判断节点的条件清单：第 1 条恒为 cond（老字段，零迁移），extra_conds
    为第 2..N 条；kind 缺失的占位条目滤掉。引擎侧组合投票
    （flow._make_branch_condition）与引用侧扫描/改名传播共用这一份口径 ——
    多条件存哪不统一，删图反查和改名必漏第 2 条以后。
    """
    items = []
    cd = node.get("cond")
    if isinstance(cd, dict) and cd.get("kind"):
        items.append(cd)
    for c in node.get("extra_conds") or []:
        if isinstance(c, dict) and c.get("kind"):
            items.append(c)
    return items


def node_tpl_refs(node: dict) -> list[tuple[str, str]]:
    """
    单个节点 → 它携带的模板引用 [(目录, 名字), ...]。
    collect_refs 读侧的原口径，抽出来给流程编辑器的编辑期校验复用
    （谁在用哪张图的判断全项目一份，两处各写必漂移）。name 可能是空串
    （用户还没选图），保留给调用方区分「没配置」和「图不存在」。
    不算引用的：
      - subflow 门关着（wait_before=False）时残留的 name
      - debug_print target 切走（≠template）时残留的 name
      - branch 非模板条件（颜色等）、server_pick（走 OCR 不引用图）
    popup_zone 的引用藏在 modules 行里（节点本身没有 category/name）——
    不收的话删图确认说"没人用"、流程里的扫描行却静默变死行。
    """
    t = node.get("type", "")
    if t in ("click_template", "wait_template"):
        return [(node.get("category", "") or "", node.get("name", ""))]
    if t == "loop":
        # 循环容器：出口条件多条与判断同口径（cond=第1条 + extra_conds，
        # branch_cond_items 一份 —— 漏第2条以后 = 删图反查说没人用）+ 体内
        # 动作行按行内类型各收各的引用（行结构与主画布节点同形，口径一份 ——
        # 行内子流程的进门等图同样只算门开着的时候）。不收的话删图确认说
        # "没人用"、循环却静默降级跳过 = 最难排查的哑 bug。
        out: list[tuple[str, str]] = []
        for cond in branch_cond_items(node):
            if cond.get("kind") == "template":
                out.append((cond.get("category", "") or "", cond.get("name", "")))
        for s in node.get("steps") or []:
            if not isinstance(s, dict):
                continue
            out.extend(_step_tpl_refs(s))
        return out
    if t == "subflow":
        # 进门等的图只在门开着才算引用（关门的残留 name 不算）
        if bool(node.get("wait_before", True)) and node.get("name"):
            return [(node.get("category", "") or "", node.get("name", ""))]
        return []
    if t == "multi_scan":
        # 遍历全部：主扫描图恒引用（文字源 P3 除外 —— 那时 category/name
        # 只是切回模板源前的残值，删图反查不该认领它）；「列表标志图」只在
        # 进一个模式且填了名字才算（切回点掉模式的残留不算 —— subflow 门
        # 同款纪律）
        out: list[tuple[str, str]] = []
        if node.get("source", "template") != "text":
            out.append((node.get("category", "") or "", node.get("name", "")))
        ct = node.get("confirm_tpl") or {}
        if node.get("mode", "tap") == "enter" and ct.get("name"):
            out.append((ct.get("category", "") or "", ct.get("name", "")))
        return out
    if t == "branch":
        # 多条件：cond + extra_conds 逐条收（branch_cond_items 一份口径）
        out: list[tuple[str, str]] = []
        for cond in branch_cond_items(node):
            if cond.get("kind") == "template":
                out.append((cond.get("category", "") or "", cond.get("name", "")))
            elif cond.get("kind") == "motion":
                # 动态检测条件引用一整组帧模板（同一库）：每组帧都算引用
                cat = cond.get("category", "")
                out.extend((cat, fn) for fn in (cond.get("frames", []) or []) if fn)
        return out
    if t == "debug_print" and node.get("target") == "template":
        return [(node.get("category", "") or "", node.get("name", ""))]
    if t == "popup_zone":
        return [(m.get("category", "") or "", m.get("name", ""))
                for m in (node.get("modules") or []) if isinstance(m, dict)]
    if t == "watch":
        # 并行监视：引用藏在每行「弹窗模板」列（category/name）；容器化后行
        # 在 groups 里，统一走 watch_rows（不收 → 删图确认说"没人用"、
        # 监视行却静默变死行，同 popup_zone 教训）。
        # 动态手指行（kind="finger"）不认图、无 category/name → 不算引用（滤掉，
        # 免得往删图反查里塞空引用）。
        return [(r.get("category", "") or "", r.get("name", ""))
                for r in watch_rows(node)
                if r.get("kind") != "finger" and r.get("name")]
    return []


def node_flow_refs(node: dict) -> set[str]:
    """单节点引用的子流程名集合：subflow 节点的 flow_name + watch/timer 每行的
    flow_name。流程改名/删除的引用扫描共用一份（否则改子流程名，哨兵行里的
    目标悄悄断成死行）。"""
    t = node.get("type", "")
    if t == "subflow":
        fn = (node.get("flow_name") or "").strip()
        return {fn} if fn else set()
    if t == "multi_scan":
        # 「进一个」模式才引子流程；切回「逐个点掉」的残留 flow_name 不算引用
        if node.get("mode", "tap") != "enter":
            return set()
        fn = (node.get("flow_name") or "").strip()
        return {fn} if fn else set()
    if t in ("watch", "timer"):
        # 行统一走 watch_rows（容器化后行在 groups 里；timer 无 groups，同口径）
        return {(r.get("flow_name") or "").strip()
                for r in watch_rows(node)
                if (r.get("flow_name") or "").strip()}
    return set()


def _step_tpl_refs(s: dict) -> list[tuple[str, str]]:
    """循环体内一行动作的模板引用（行结构与主画布节点同形，只收会引图的几种）。

    不算引用的：
      - subflow 门关着（wait_before=False）时残留的 name
      - popup_zone 行里的模块（模块行也是 {"category","name"}，同源）
    """
    st = s.get("type", "")
    if st in ("click_template", "wait_template"):
        return [(s.get("category", "") or "", s.get("name", ""))]
    if st == "subflow" and bool(s.get("wait_before", True)) and s.get("name"):
        return [(s.get("category", "") or "", s.get("name", ""))]
    if st == "popup_zone":
        return [(m.get("category", "") or "", m.get("name", ""))
                for m in (s.get("modules") or []) if isinstance(m, dict)]
    return []


_refs_memo: tuple[str, dict] | None = None   # (custom_flows 内容哈希, refs 结果)


def collect_refs() -> dict[tuple[str, str], set[str]]:
    """
    扫描全项目，汇总 {(模板目录, 模板名): {使用者任务标识, ...}}。
    只认引用、不判存在性 —— 缺不缺图由 template_manager.check_missing 管。
    flow 归属含间接引用：A 的子流程节点引 B → B 用的每张图也记到 A 名下
    （删图确认说"flow:A 在用"才不骗人），传递闭包带访问集防子流程成环。

    记忆化（性能审计）：UI 删图确认/表刷新逐模板调 users_of，每次都全树走查
    一遍 custom_flows（几百流程×几十节点），批量操作明显卡。缓存键 = custom_flows
    整份浅层 JSON 哈希——"最笨但不会错"：内容没变必然结果没变，改流程/改名
    （rewrite_template_refs 就地改活引用）哈希即变、自动重算。
    ponytail: 归属 2（整目录约定）依赖模板账本清单，当前唯一任务 flow 不走那条
    分支（内置任务已全删）；将来注册了带 category 的新任务，键需再并入账本版本。
    ⚠ 返回的是共享缓存对象：当只读用，别就地改里面的 set（要改先 copy）。
    """
    global _refs_memo
    import hashlib
    import json
    from core.config_manager import get_config
    flows = get_config().get("custom_flows", {}) or {}
    try:
        digest = hashlib.sha1(json.dumps(
            flows, sort_keys=True, ensure_ascii=False, default=str
        ).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        digest = None          # 理论到不了（config 纯 JSON）：算不了键就每次现扫，宁慢不错
    if digest is not None and _refs_memo is not None and _refs_memo[0] == digest:
        return _refs_memo[1]
    refs = _collect_refs_uncached(flows)
    if digest is not None:
        _refs_memo = (digest, refs)
    return refs


def _collect_refs_uncached(flows: dict) -> dict[tuple[str, str], set[str]]:
    """collect_refs 的实扫描本体（缓存未命中时才走）。"""
    from bot.tasks import TASKS
    from core import template_manager

    refs: dict[tuple[str, str], set[str]] = {}

    def add(task_name: str, cat: str, name: str):
        if not name:
            return
        refs.setdefault((cat or "", name), set()).add(task_name)

    for tname, cls in TASKS.items():
        cat = getattr(cls, "category", "") or ""
        # 归属 1：声明清单
        for ref in getattr(cls, "required_templates", None) or []:
            add(tname, ref[0], ref[1])
        # 归属 2：整目录约定（任务目录 = category，声明点名即可）。
        # flow 例外：流程任务自己的目录只是存储桶，「谁用哪张图」全在节点引用里，
        # 走归属 3 记成「flow:流程名」；这里再整目录认领会造成双重归属、且粒度过粗。
        if cat and tname != "flow":
            for t in template_manager.list_templates():
                if t.get("category") == cat:
                    add(tname, cat, t["name"])
        # 归属 3：配置引用（活动领奖下线后只剩 flow 一条来源）
        if tname == "flow":
            # flows 由 collect_refs 包装层取好传入（缓存键与实际扫描同一份数据）
            # 第一遍：逐流程收直接引用 + 记子流程目标
            own: dict[str, set[tuple[str, str]]] = {}
            sub_targets: dict[str, set[str]] = {}
            for flow_name, flow in flows.items():
                for node in iter_all_flow_nodes(flow):
                    # 引用口径统一走 node_tpl_refs（与流程编辑器校验共用一份）
                    for cat, name in node_tpl_refs(node):
                        own.setdefault(flow_name, set()).add((cat, name))
                    # 子流程目标名统一走 node_flow_refs（subflow + watch 行）
                    for tgt in node_flow_refs(node):
                        sub_targets.setdefault(flow_name, set()).add(tgt)
            # 第二遍：子流程引用沿调用边传递闭包（访问集 = 防 A↔B 互引死循环）
            def closure(flow_name: str) -> set[tuple[str, str]]:
                out = set(own.get(flow_name, ()))
                stack = list(sub_targets.get(flow_name, ()))
                seen = {flow_name}
                while stack:
                    cur = stack.pop()
                    if cur in seen:
                        continue
                    seen.add(cur)
                    out |= own.get(cur, set())
                    stack.extend(sub_targets.get(cur, ()))
                return out
            for flow_name, flow in flows.items():
                # 记成「flow:流程名」而非裸 flow：删图/改名确认时能指名是哪个流程
                # 在用，而不是含糊一句「flow」。多流程共用一图也分得清。
                owner = f"flow:{flow_name}"
                for cat, name in closure(flow_name):
                    add(owner, cat, name)

    return refs


def display_user(u: str) -> str:
    """
    使用者标识 → 界面显示名，全项目一份口径（替掉 UI 里散落的 names.get(u,u)）。
      "flow:X"   → 自定义流程·X（flow 归属按流程名细分，见 collect_refs）
      其余       → 任务 display_name，查不到原样返回
    """
    names = task_dir_display()
    if u.startswith("flow:"):
        return f"{names.get('flow', 'flow')}·{u[5:]}"
    return names.get(u, u)


def rewrite_template_name(old_cat: str, old_name: str, new_name: str) -> int:
    """模板改名的写侧传播（薄壳，实体见 rewrite_template_refs）。"""
    return rewrite_template_refs(old_cat, old_name, new_name=new_name)


def rewrite_template_refs(old_cat: str, old_name: str,
                          new_name: str = "", new_cat: str = "") -> int:
    """
    把 config 里所有指向 (old_cat, old_name) 这张图的引用改写后返回处数：
    传 new_name = 改名（只动 name），传 new_cat = 移动（只动 category），
    改名/移动共用一份走查 —— 引用点知识只此一处，再抄一份必漂移。
    collect_refs（读侧）的写侧对应物：两边引用点必须一份知识，否则"反查说在用、
    传播却漏改"必然漂移。覆盖：
      - custom_flows 节点：click_template / wait_template / branch(cond=template)
        / debug_print(target=template) / subflow(进门等的 name)，v1 嵌套树与 v2
        平铺图一份口径（iter_all_flow_nodes，与 collect_refs 读侧同源）
    匹配语义与 find_template 对齐：节点 category 为 ""（跨目录逃生口，按名字
    全库找）的引用改名后同样断，必须一起改；【移动】时按名全库照样命中新库里的
    同一张图，所以 category="" 的引用不用动。改完即存盘，不留内存/磁盘分叉。
    """
    if not old_name or ((not new_name or old_name == new_name) and not new_cat):
        return 0
    from core.config_manager import get_config   # 局部 import，与 collect_refs 同款
    cfg = get_config()
    n = 0

    def rebind(d: dict, key: str, cat: str, cat_key: str = "category"):
        nonlocal n
        if d.get(key, "") != old_name:
            return
        if new_name and (cat == "" or cat == old_cat):
            d[key] = new_name
            n += 1
        if new_cat and cat == old_cat:
            d[cat_key] = new_cat
            n += 1

    flows = cfg.get("custom_flows", {}) or {}
    for flow in flows.values():
        # v1/v2 一份展开（iter_all_flow_nodes 与 collect_refs 同源）
        for node in iter_all_flow_nodes(flow):
            t = node.get("type", "")
            # subflow 的 category/name 是进门等的模板，与 click/wait 同结构，一起改写
            if t in ("click_template", "wait_template", "subflow"):
                rebind(node, "name", node.get("category", ""))
            elif t == "multi_scan":
                # 遍历全部：主扫描图 + 列表标志图（confirm_tpl 一对）都跟上，
                # 与读侧 node_tpl_refs 同一份引用点认知。文字源节点读侧不
                # 认领、写侧照样顺手改残值 —— 与 subflow 关门残值同处置
                # （改名传播宁可多改不放过，断链比脏值贵）
                rebind(node, "name", node.get("category", ""))
                ct = node.get("confirm_tpl")
                if isinstance(ct, dict) and ct.get("name"):
                    rebind(ct, "name", ct.get("category", ""))
            elif t == "loop":
                # 循环容器：出口条件多条（cond=第1条 + extra_conds，与判断同一份
                # branch_cond_items 口径，漏一条 = 反查说在用、传播却漏改）+ 体内
                # 动作行（行结构与主画布节点同形），与 collect_refs 读侧同一份节点
                # 展开认知。popup_zone 行 = 模块清单逐项改名，与弹窗区节点同款。
                for cond in branch_cond_items(node):
                    if cond.get("kind") == "template":
                        rebind(cond, "name", cond.get("category", ""))
                for s in node.get("steps") or []:
                    if not isinstance(s, dict):
                        continue
                    st = s.get("type", "")
                    if st in ("click_template", "wait_template", "subflow"):
                        rebind(s, "name", s.get("category", ""))
                    elif st == "popup_zone":
                        for m in s.get("modules") or []:
                            if isinstance(m, dict):
                                rebind(m, "name", m.get("category", ""))
            elif t == "branch":
                # 多条件：cond + extra_conds 逐条改写（与读侧 node_tpl_refs
                # 同一份 branch_cond_items 口径，漏一条 = 反查说在用、传播却漏改）
                for cond in branch_cond_items(node):
                    if cond.get("kind") == "template":
                        rebind(cond, "name", cond.get("category", ""))
                    elif cond.get("kind") == "motion":
                        # motion 组的帧模板（frames 列表，整组共用一个 category）：
                        # 改名逐个换帧名（"" 跨目录引用按名全库找，同样要换），
                        # 移动换整组 category（"" 引用按名在新库照样命中，不用动）。
                        frames = cond.get("frames") or []
                        cat = cond.get("category", "")
                        if new_name:
                            for i, fn in enumerate(frames):
                                if fn == old_name and (cat == "" or cat == old_cat):
                                    frames[i] = new_name
                                    n += 1
                        elif new_cat and cat == old_cat and old_name in frames:
                            cond["category"] = new_cat
                            n += 1
            elif t == "debug_print" and node.get("target") == "template":
                rebind(node, "name", node.get("category", ""))
            elif t == "popup_zone":
                # 与 collect_refs 读侧同一份节点展开认知：modules 行逐个改名
                for m in node.get("modules") or []:
                    if isinstance(m, dict):
                        rebind(m, "name", m.get("category", ""))
            elif t == "watch":
                # 监视行同样藏引用（读侧 node_tpl_refs 已收）：改名不跟上，
                # 删图确认说"没人用"、哨兵行静默变死行。容器化后行在 groups 里，
                # 走 watch_rows 一份展开，改的是原始 dict（就地生效）。
                for r in watch_rows(node):
                    rebind(r, "name", r.get("category", ""))

    # （活动领奖下线：daily.activities 配方的 name/click_tpl 改写段一并移除；
    #   该任务恢复时从 git 历史找回本段，与 collect_refs 同步）

    if n:
        # 上面全部就地改在 cfg.data 的活引用上（ConfigManager.get 不拷贝），
        # 所以只需落盘，不需要 set 重组
        cfg.save()
    return n


def users_of(category: str, name: str, refs=None) -> list[str]:
    """某模板的使用者任务标识列表（refs 可传入已 collect 的结果避免重复扫描）。"""
    if refs is None:
        refs = collect_refs()
    return sorted(refs.get((category or "", name), set()))
