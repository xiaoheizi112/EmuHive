"""
core/config_manager.py — 配置文件读写（第 4 步）

管理 configs/config.json：启动时加载，任何修改自动保存（需求 6 的地基）。

本步学到的知识点：
1. JSON：字典 ↔ 文本文件 的往返。json.dump 把字典写进文件，json.load 读回来。
   配置文件本质就是"一块能存到硬盘的字典"
2. 默认值合并：程序升级后新增了配置项，但用户硬盘上还是旧配置文件 →
   把旧文件的内容和默认字典"深合并"，保证任何情况下每个配置项都有值
3. 防抖保存（debounce）：用户连续改 10 次配置，只在停手 1 秒后写一次盘，
   而不是写 10 次。原理：每次修改重置一个定时器，只有定时器走到头才真写
4. 原子写入：先写临时文件再整体替换，防止写到一半断电产生半个坏文件
5. 可移植性：所有路径基于"项目目录"推算（Path(__file__) 往上找），
   整个文件夹拷到任何电脑、任何盘符都能工作，不依赖绝对路径
"""

import atexit
import copy
import json
import sys
import threading
from pathlib import Path

from core import json_store

# 项目根目录 = 本文件的上上级目录（core/ 的父目录）。
# 打包版（PyInstaller frozen）例外：源码在 _internal 里，锚到 exe 所在目录，
# configs/templates/data/logs 全在 exe 旁边——用户资产跟可执行文件同层，
# 换目录/升版本只替换程序本体，配置不丢（分发审计 P1-⑪）。
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "configs" / "config.json"

# ── 流程资产分流（2026-10-09）────────────────────────────────────────────
# 下面这几个键是"用户资产"（重做一遍要几天的流程库），不再是"程序设置"，
# 落盘时从 config.json 剥出来、单独存 configs/flows.json。内存里仍是同一棵
# 树——get("custom_flows") 等全部照旧，分流只收口在 _load/save 两个出口，
# 上层 ui/bot/tools 一个调用点都不用改。flows.json 每次覆写前把旧版快照进
# configs/flows_backup/（留最近 N 份），改坏流程资产=从备份挑一份拷回，
# 不再需要"动库前先列清单等确认"那套人肉保险。
FLOW_ASSET_KEYS = ("custom_flows", "flow_categories", "flow_category_order",
                   "flow_locked", "flow_notes")
FLOW_BACKUP_KEEP = 30   # 快照保留份数，超出自动剪最旧（只剪快照目录自己的文件）

# 全部配置项的默认值。任何新增配置项都要在这里登记，
# 老配置文件缺的项会在加载时自动补默认值
DEFAULT_CONFIG = {
    "general": {
        "ld_console": "",   # 雷电 ldconsole.exe 路径；留空 = 自动探测常见路径
        # 每账号轮询间隔（秒）：0.25→0.5（2026-09-23 多开调优）。单次识别
        # 质量与间隔无关（每拍都是完整截图找图），放慢只晚半拍发现目标；
        # 换来多开时截图/识别吞吐减半。专抓快闪弹窗的流程可再调回小值。
        "frame_interval": 0.5,
        # 点击常驻延时（秒）：「点模板」节点勾了「点击后加延迟」时，点完
        # 多等这么久才走下一步（给游戏加载/动画留余量）。全局一个值，
        # 功能设置页可调，未勾选的节点不受影响。
        "post_click_delay": 1.0,
        # 启动校准最坏预算（秒）：worker 挂机循环里重校准（capture.calibrate）
        # 的最坏耗时上限，超时降级下拍再试。「worker 循环绝不阻塞」不变量的
        # 显式旋钮——旧版启动期校准给 3 轮 × 30 秒 adb，挂机中偶发重校准能冻住
        # 整条 tick 循环几十秒（点停止没反应）。由 bot/worker 消费。
        "launch_calibrate_timeout": 5.0,
    },
    "emulator": {
        # 基准分辨率唯一源 = core/capture.py 的 BASE_WIDTH/BASE_HEIGHT 常量。
        # 原 emulator.base_width/base_height 两键全项目零读取，已删（死旋钮）。
        # 启动前统一给实例设定的安卓分辨率（ldconsole modify --resolution 的目标值）。
        # 默认与采集基准一致 = 540x960：这样识别坐标空间和点击坐标空间重合，
        # input tap 不会点歪。改这三个值会让全程序按新分辨率运行，
        # 但模板是按采集基准采集的，分辨率不一致时模板需重新采集（见分辨率设置对话框的警告）。
        "resolution_w": 540,
        "resolution_h": 960,
        "resolution_dpi": 240,
        # 启动间隔（秒）：「全部启动」逐台错开下达 launch。默认 1，功能设置页最低 1；
        # 手改填 0 = 一次全发（原行为，代码仍认）。瞬时并发开几十台模拟器会触发
        # 雷电/游戏侧风控，且 CPU/磁盘挤在一起反而都慢。
        "launch_interval_sec": 4.0,
        # adb 截屏开关（默认开）：挂机取帧**主路**——adb 单次快照
        # （`adb shell screencap -p` 直通 stdout，实测 172ms/帧、无常驻进程、
        # CPU 近零）。挂机是「每 frame_interval 一张当前屏」的离散轮询，与
        # screencap 同构；认知与实测见 core/capture.py「adb 取帧」段头。
        # false = 一票关死（各取帧路皆返回 None，落空帧自旋）。
        "capture_adb_fallback": True,
        # 强制 adb 逃生口（默认关）：true=每拍直连 adb（与默认同路）。
        # 排障预留（确认真是不是 adb 本身的锅）。
        "capture_adb_only": False,
        "blocked_indexes": [],  # 实例屏蔽名单：名单里的实例全程序不可见（单实例测试用）
    },
    "vision": {
        "threshold": 0.85,   # 模板匹配默认阈值
        # 识字(OCR)单拍时间预算（秒）：worker 每拍给 ocr 识别的时间上限，
        # 超了本拍放弃下拍再试——rapidocr 单次推理可达数百毫秒，多开时
        # 不限量会把 0.25s 的 tick 循环吃穿。由 bot 识字节点消费。
        "ocr_tick_budget_sec": 0.2,
        # 「点击超时后扫描」单拍预算（秒）：点模板节点超时兜底的整屏扫描
        # 每拍允许占用的时间，同上口径防拖垮 tick 循环。由 bot 流程节点消费。
        "click_timeout_scan_budget_sec": 0.15,
    },
    "task_order": [],        # 任务清单（挂机队列）：内容 = 流程名（可按顺序重复），
                             # 见 ui/queue_panel.py，交 worker 前换算成 "flow:流程名"
    "queue_presets": {},     # 队列预设：{预设名: [流程名...]}，见 ui/queue_panel.py
    "task_bar": [],          # 主窗口任务栏：任务（流程名引用，可重复）平铺列表，
                             # 由工作台流程的「加入任务栏」生成，见 ui/category_sidebar.py
    "task_params": {},       # 任务参数：{任务名: {参数键: 值, "_enabled": true/false}}
    "account_tasks": {},     # 每账号独立任务队列：{账号id: {"queue": [...], "params": {...}}}
    # （活动领奖任务已下线：原 "daily.activities" 配方节一并移除，恢复任务时再登记）
    "custom_flows": {},      # 自定义流程：{流程名: {"nodes": [v2 平铺图], "edges": [...]}}，节点类型见 bot/tasks/flow.py
    # 流程手动分类（活动/任务/…用途标签）：{流程名: 分类名}。故意与流程值
    # dict 分开存 —— _on_save 每次整体替换图 dict，存值里的字段会被冲掉。
    "flow_categories": {},
    # 分类显示顺序 + 空分类保留：[分类名...]。不在表里的分类按首次出现兜底，
    # 「未分类」恒最后。删分类 = 组内流程回未分类 + 从本表除名。
    "flow_category_order": [],
    # 流程上锁名单：[流程名...]。同样故意与流程值 dict 分开存（_on_save 会整体
    # 替换图 dict）。上锁 = 只读保护：不能改画布/保存/改名/删除/撤销，
    # 仍可打开查看、加入任务栏、测试、移分类（分类是标签旁账本不算内容）。
    # 锁的是"这条流程的配置别手滑改坏"。
    "flow_locked": [],
    # 流程便签：{流程名: 文本}。用户手写的备忘（还有什么没完善、进行到哪步了），
    # 编辑器右侧顶部常驻显示、流程库列表带 📝 角标。与流程值 dict 分开存
    #（_on_save 整体替换图 dict，存里面的会被冲掉）；纯备忘，不参与编译运行。
    "flow_notes": {},
    # 自定义归属库（新增库功能）：{templates/ 下目录名: 界面显示名}。
    # 正常只登记用户手动新建的非任务库（任务目录由注册表驱动）；唯一例外是
    # 「＋新增库」重建被删空的任务目录 —— 空目录磁盘扫描看不见（只收含 PNG 的），
    # 靠这份登记重新出现在下拉框，所以重建时也会写一条（显示名取注册表的）。
    # list_categories() 的第三条来源，见 core/template_manager.py。
    "template_categories": {},
    # 被「删除库」清掉过的任务目录名（墓碑）：唯一用途是阻止「全新安装零模板
    # 兜底」（list_categories 第 2.5 步）把用户刚删掉的默认任务目录集体复活。
    # 采图/重建自动除名，不需要也不应该手动维护；见 core/template_manager.py。
    "deleted_categories": [],
    # 模板名前缀管理（ui/template_tool.py 的前缀下拉）：用户可自定义增删，
    # 老默认值给初次使用兜底；改完只影响新采模板的名字，不动存量
    "template_prefixes": ["弹窗", "窗口", "关闭窗口", "锚点", "标签", "红点", "按钮",
                          "图标", "入口", "文字", "状态", "奖励", "背景"],
    # 工作台框选后自动贴边（ui/template_tool.py）：粗框一下，算法把蓝框吸到
    # 图标轮廓边缘。默认关——老手可能嫌它多事，勾一次记住即可。
    "template_auto_snap": False,
    "ui": {                  # 窗口尺寸/布局等界面状态（第 5 步起用）
        # 桌面悬浮召回球（ui/float_ball.py）：enabled=false 球永不出现；
        # x/y 为球左上角屏幕坐标，-1 = 首次自动摆到屏幕右侧中部
        "float_ball": {"enabled": True, "x": -1, "y": -1},
        # 流程库侧栏「分类折叠」状态（2026-10-11）：存被折叠（收拢）的分类名列表；
        # 空/缺省 = 全展开。点组标题行切换；重启保留。
        "flow_collapsed": [],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 合并进 base（嵌套字典递归合并），返回新字典"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigManager:
    """配置管理器：get/set 用点路径访问，如 get("vision.threshold")"""

    def __init__(self, path=CONFIG_PATH):
        self.path = Path(path)
        # 流程资产文件与快照目录：跟 config.json 同目录派生——
        # 冒烟脚本把 ConfigManager 指到临时目录时，flows.json 自动隔离进临时目录
        self.flows_path = self.path.parent / "flows.json"
        self.flows_backup_dir = self.path.parent / "flows_backup"
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self._load()
        self._save_timer: threading.Timer | None = None
        # 可重入锁：set() 持锁后会再进 save()/_schedule_save()（定时器回调
        # 线程里 save 也可能被再次触碰），普通 Lock 自锁即死锁，必须 RLock
        self._lock = threading.RLock()
        # 是否有 set 之后尚未落盘的改动（atexit/flush 判断用；均在 _lock 内读写）
        self._pending = False

    # ── 读 ──
    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
            except (json.JSONDecodeError, OSError):
                print(f"[警告] 配置文件损坏，使用默认值：{self.path}")
                loaded = {}
            self.data = _deep_merge(DEFAULT_CONFIG, loaded)  # 老配置缺项自动补默认值
        # 流程资产覆盖：flows.json 存在以它为准（分流后 config.json 已不含这些键）；
        # 不存在则沿用 config.json 里的旧资产键——老配置不改也能跑，
        # 且第一次 save() 会自动完成分流（先写 flows.json 再剥 config.json，见 save）
        flows = json_store.load_or_default(self.flows_path, {})
        if isinstance(flows, dict):
            for k in FLOW_ASSET_KEYS:
                if k in flows:
                    self.data[k] = flows[k]

    def get(self, dot_path: str, default=None):
        """点路径取值：get("vision.threshold")。
        读也走锁：set() 持锁改嵌套树，get() 裸走多步游走时可能读到
        setdefault 刚建、还没填值的中间层 → 偶发返回默认值（下一拍自愈但
        难排查）。RLock 获取百纳秒级，相比点路径游走本身可忽略。"""
        with self._lock:
            node = self.data
            for part in dot_path.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    def get_safe(self, dot_path: str, default=None):
        """get + 类型防御 + 异常全吞：读不到 / 类型不对 / 任何意外，一律回 default。
        给"读配置绝不能崩"的降级路径用（原先各处 try/except get 的手拧样板收口）。
        类型比对以 default 的类为准；default=None 不做类型检查（拿到啥是啥）。"""
        try:
            v = self.get(dot_path, default)
        except Exception:
            return default
        if v is None:
            return default
        if default is not None and not isinstance(v, type(default)):
            return default
        return v

    # ── 写 ──
    def set(self, dot_path: str, value):
        """点路径设值，之后自动防抖保存（1 秒后写盘）"""
        with self._lock:
            parts = dot_path.split(".")
            node = self.data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            self._pending = True      # 有未落盘改动（供 flush()/atexit 判断）
            self._schedule_save()

    def _schedule_save(self):
        """防抖：每次修改重置 1 秒定时器，停手 1 秒后才真正写盘。
        必须持锁调用（触碰 _save_timer 的读写全部收在这一把锁内）。"""
        # 复用槽位：定时器还活着就 cancel 重排，否则（未建/已触发）直接新建。
        # ⚠ 无法字面"复用同一个 Timer 对象"——threading.Timer 是 Thread 子类，
        #   start() 只能调一次，重排必须换新对象；这里省的是无谓的 cancel 空转。
        if self._save_timer is not None:
            if self._save_timer.is_alive():
                self._save_timer.cancel()
            self._save_timer = None
        self._save_timer = threading.Timer(1.0, self.save)
        self._save_timer.daemon = True
        self._save_timer.start()

    def save(self):
        """立即写盘（原子写入：先写 .tmp 再整体替换）。
        整段写盘收在同一把锁内串行：tmp 文件名固定，移出锁会让并发写抢同
        一个 .json.tmp 反造竞态，故不能"出锁再 replace"（简报建议已否决）。
        2026-10-09 起分两页写：流程资产 → flows.json、程序设置 → config.json。
        写序固定"先资产后设置"：万一双写之间崩溃，最坏是 config.json 里残留
        旧资产键（读取时 flows.json 优先，残键被无视）——资产绝不两头落空。"""
        with self._lock:
            # 顺手回收可能挂着的防抖定时器（save 本身即定时回调时 cancel 是空操作）
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
            self._pending = False
            payload = {k: self.data[k] for k in FLOW_ASSET_KEYS if k in self.data}
            # 快照/比对/剪枝全套收进 json_store.dump_with_history（与模板账本
            # 共用一份保险丝实现；含 Windows 换行归一比对的历史教训，见其 docstring）
            json_store.dump_with_history(
                self.flows_path, payload, self.flows_backup_dir,
                "flows", keep=FLOW_BACKUP_KEEP)
            rest = {k: v for k, v in self.data.items() if k not in FLOW_ASSET_KEYS}
            json_store.atomic_dump(self.path, rest)

    def flush(self) -> bool:
        """同步落盘（供退出/关键节点调用）：只有存在未落盘改动才写，
        无改动绝不空写（红线：不碰没改过的配置盘）。与 save() 的区别 = save
        恒写、flush 按需写；二者都是立即原子写。返回是否真的写了盘。"""
        with self._lock:
            if not self._pending:
                # 无待保存内容：顺手清掉可能残留的定时器，直接返回
                if self._save_timer is not None:
                    self._save_timer.cancel()
                    self._save_timer = None
                return False
        self.save()   # save() 内部取消定时器、置 _pending=False、原子写
        return True


# 全局单例：全程序共用同一份配置（"单例模式"——任何模块 import 拿到的都是同一个对象）
_config: ConfigManager | None = None


def get_config() -> ConfigManager:
    global _config
    if _config is None:
        _config = ConfigManager()
    return _config


def _atexit_flush():
    """进程退出前兜底：防抖定时器是 daemon 线程，解释器退出即被杀，
    "改完 1 秒内就关程序"会把这次改动整笔丢掉。这里在退出时补一次 flush。
    只 flush 全局单例且仅在有待保存内容时写真盘（冒烟已把 _config 换成临时单例，
    不会污染真 configs/config.json）；任何异常都吞掉，绝不阻断退出。"""
    try:
        cfg = _config
        if cfg is not None:
            cfg.flush()
    except Exception:
        pass


# 模块级注册一次（不 import 时不建配置、不写盘；get_config 尚未调用则 _config=None，跳过）
atexit.register(_atexit_flush)


if __name__ == "__main__":
    # 命令行验收：python -m core.config_manager
    # 改一个配置 → 强制保存 → 重新加载 → 验证读回新值。
    # ⚠ 2026-10-09 起改在临时目录跑：老写法直接动真 configs/config.json，
    #   分流改造后那等于"自测顺手把真库完成迁移"——迁移脚本（已归档删除）
    #   带备份+校验流程，自测脚本不许替它意外触发真库迁移。
    import tempfile
    tmp_path = Path(tempfile.mkdtemp()) / "config.json"
    cfg = ConfigManager(tmp_path)
    old = cfg.get("vision.threshold")
    cfg.set("custom_flows", {"冒烟": {"nodes": []}})   # 资产键也过一遍：验证分流落盘
    cfg.set("vision.threshold", 0.90)
    cfg.save()
    flows_disk = json.loads((tmp_path.parent / "flows.json").read_text(encoding="utf-8"))
    main_disk = json.loads(tmp_path.read_text(encoding="utf-8"))
    fresh = ConfigManager(tmp_path)  # 模拟"重启程序后重新加载"
    new = fresh.get("vision.threshold")
    ok = (new == 0.90 and "custom_flows" in flows_disk
          and "custom_flows" not in main_disk
          and fresh.get("custom_flows") == {"冒烟": {"nodes": []}})
    print(f"改前={old} → 改后={new}；config.json 剥资产={('custom_flows' not in main_disk)}；"
          f"flows.json 接资产={('custom_flows' in flows_disk)} → 验证 {'通过' if ok else '失败'}")
    sys.exit(0 if ok else 1)
