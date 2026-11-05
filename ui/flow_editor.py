"""
ui/flow_editor.py — 自定义流程可视化编排面板 + 画布

FlowPanel 是「编流程」工作区（嵌在三栏工作台 ui/workbench.py 中栏常驻可见）：
用户不写代码、编排一条「挂机流程」，节点用线连成有向图。数据模型与运行时
（bot/tasks/flow.py）完全一致——config.json 里 custom_flows.<流程名> 的
v2 图模型 {version: 2, nodes: [...], edges: [...]}：
- 节点带画布坐标 x/y 和类型 type（17 种，见 TYPE_LABEL），参数就挂在节点 dict 上；
- 线是 {from, port, to, to_port}：port 为出口端口名（主干 "out"、判断
  "true"/"false"），to_port 为入口（主干 "in"、循环收口 "sig" 等）。
老的嵌套 steps/back 树模型已整体退役（存量 back 节点编译为 None 按跳过处理）。

面板布局：
- 顶部工具栏：流程操作（保存 / 分类 / 自动保存 / 测试）+ 「新增节点」调色板下拉 + 「适应」按钮；左侧流程库列表（右键管理，双击 = 加入任务栏，行尾锁图标 = 上锁/解锁）
- 中：FlowCanvasView 画布（节点连线图；滚轮缩放，右键按住拖平移，
  左键拖空白框选，双击空白就地加节点，Ctrl+Z/C/V/D 撤销/复制/粘贴/再制，
  拖动节点自动对齐吸附 + 红色参考线）
- 右：属性面板（选中节点后按类型渲染参数表单）
预览 ImageView、实例选择、截图定时器、悬停取色读数由工作台外壳共用，
本面板通过 attach_view() 接预览信号（点选坐标 / 多点取色记点）。
三栏同屏后没有模式切换：模板库有任何变化（保存/删除/改名/导入）外壳直接
调 on_templates_changed() 重建当前表单的模板下拉——面板不再被藏，
原「隐藏期刷了也看不见、延迟到切回时按脏重建」那套机制整个作废。

节点类型（见 bot/tasks/flow.py）：动作类（点模板/等图/点坐标/滑动/等待/
打印/资产入库/计算/识字取物/遍历全部）、控制类（判断/子流程/循环/并行监视/
定时执行/结束流程/发信号）。

复用：core.config_manager 的点路径存取，core.template_manager 的模板下拉。
"""

import copy
import json
import re
import time
import uuid

import cv2
from PySide6.QtCore import (QEasingCurve, QEvent, QMimeData, QPoint,
                            QPointF, QRect, QRectF, QSize, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (
    QGuiApplication,
    QBrush,
    QColor,
    QCursor,
    QDrag,
    QFont,
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QAbstractItemView,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bot import template_deps, watch_stats
from bot.tasks.flow import _has_pair, compile_graph, detect_cycle, ensure_graph
from bot.template_deps import UNCLASSIFIED
from core import account_io, flow_pack, template_manager, vision
# 分辨率单一来源：core.capture 的基准 540×960（CLAUDE.md 红线「分辨率三方一致」，
# 散落 539/959/540/960 字面量 = 改基准时漏改点，识别对、落手歪，日志查不出）
from core.capture import BASE_WIDTH as BASE_W, BASE_HEIGHT as BASE_H
from core.config_manager import get_config
from ui import icons, theme
from ui.flow_layout import FlowLayout
from ui.help_badge import make_help_badge


def _add_group_header(cmb: QComboBox, text: str):
    """下拉框加一条不可选中的组标题行（QComboBox 没有原生分组，
    标准做法：普通条目 + 关掉它的 enabled flag，灰显且选不中）。
    2026-10-06 视觉强化（用户实拍：分类跟流程名长得一样分不出）：▍记号 +
    加粗 + 灰字 + 浅底条。记号是最后防线 —— 哪天主题吞了颜色字重，符号还在。"""
    cmb.addItem("▍" + text)
    it = cmb.model().item(cmb.count() - 1)
    if it is not None:
        it.setEnabled(False)
        f = QFont(it.font())
        f.setBold(True)
        it.setFont(f)
        it.setForeground(QBrush(QColor("#5b6472")))
        it.setBackground(QBrush(QColor("#eef1f5")))


def _fill_flow_grouped(cmb: QComboBox, flows: list, cur_f: str,
                       placeholder: str = "（选一条流程）"):
    """流程下拉统一分组灌法（子流程节点/遍历全部/循环体子流程动作/监视·定时行
    共用一份口径，2026-10-06）：分类灰底标题行 + 流程缩进两全角空格；
    只有 1 组或没分类时不插标题不缩进（零杂音，跟老版长得一样）。
    ⚠ 存储不变量：currentData 恒为纯流程名，缩进/记号只是显示文本；
    已删流程的残留值垫末尾保住不"偷梁换柱"改存值（下拉老惯例）。"""
    cmb.addItem(placeholder, "")
    fs = set(flows)
    groups = [(cat, [f for f in ms if f in fs])
              for cat, ms in template_deps.flow_category_groups()]
    groups = [g for g in groups if g[1]]
    grouped = {f for _c, ms in groups for f in ms}
    pad = "　　" if len(groups) > 1 else ""
    for cat, ms in groups:
        if pad:
            _add_group_header(cmb, cat)
        for f in ms:
            cmb.addItem(pad + f, f)
    for f in flows:
        if f not in grouped:
            cmb.addItem(pad + f, f)
    idx = cmb.findData(cur_f)
    cmb.setCurrentIndex(idx if idx >= 0 else 0)


# ── 左侧流程库列表：样式统一在全局主题 ui/theme.py（认 objectName: flowList）──
_SIDEBAR_WIDTH = 168   # 放下「未分类（99）」和几个右键字段还留出点宽

# 「?」徽章悬停文案（徽章和控件 tooltip 共用一份，改一处两边同步）
_FLOWLIB_HELP = (
    "这里列着你建过的所有流程。\n"
    "双击流程 = 生成任务进主界面任务栏\n"
    "右键流程 = 新建/改名/删除/移到分类/加入任务栏\n"
    "右键组标题 = 新建/重命名/删除分类\n"
    "列表下方「导出/导入」按钮（或右键空白处同两项）= 全部流程连同图片打包 / 导入\n"
    "拖流程到目标分类的任意一行 = 换分类（拖进「未分类」组 = 取消分类）\n"
    "点行尾锁图标 = 上锁/解锁（锁住的流程进只读模式，防误改）")
_CAT_HELP = ("当前流程的用途分类（侧栏分组用）。\n"
             "选「未分类」= 取消分类；直接敲新名字 = 新建分类并用上。")
_WATCHDOG_HELP = (
    "卡死看门狗：连续这么多秒没有任何一步走完 = 判定整条流程卡死，\n"
    "按普通失败处理（截图留证 → 退避重试）。\n"
    "⚠ 必须大于流程里最慢单步的超时，否则会误杀正常的慢步骤；\n"
    "子流程节点的耗时不估算，套了子流程自己心里有数。\n"
    "0 = 关闭（默认，老流程行为不变）。")
_ADDNODE_HELP = (
    "放下一个节点到画布（弹类型菜单，分组列出，悬停类型名看详细说明）。\n"
    "更快的加法：双击画布空白 / 按 Tab，弹搜索框打字即筛就地放置。\n"
    "节点放好后从右缘小圆拉线到下一节点 = 串执行顺序；判断/子流程有 Y、N 双出口。")
# 各节点类型介绍：直接挂到对应按钮/属性面板标题的 tooltip 上，鼠标悬停即看
NODE_HELP = {
    "click_template":
        "在画面里找到这张图，点它的中心（可加相对偏移 X/Y 再点别处）。<br>"
        "默认先等图出现再落手（可关，超时时长/超时行为可配）；图没采集会跳过并告警。<br>"
        "落点抖动 = 每次在目标位周围随机散布几像素（可配半径范围，反「次次点同一像素」）。<br>"
        "「点击后加延迟」= 点完多等全局常驻延时（功能设置页配）再走下一步，给加载/动画留余量。<br>"
        "点按钮优先用它，比点坐标稳。<br>"
        "「限定搜索区域」= 只在框内找这张图（同一图标屏幕多处出现、含义不同时"
        "锁定那一处）；不框 = 跟随模板账本的默认区域。<br>"
        "红色「N」出口：等图超时判失败时走这条线（接了 N 口，「超时退出程序」不再生效）。",
    "wait_template":
        "等画面上出现（或消失）指定图再继续。<br>"
        "超时填 0 = 不限时；稳定确认要求连续看到才算命中，防闪烁误判。<br>"
        "超时行为可选判失败或跳过。适合等加载、判断有没有弹窗。<br>"
        "「限定搜索区域」= 只在框内找这张图；不框 = 跟随模板账本的默认区域。<br>"
        "红色「N」出口：等图超时走这条线（接了 N 口，「超时退出程序」不再生效；超时填 0 = 永不触发 N 口）。",
    "click":
        "不看图，直接点固定坐标（按 540×960 基准），可加落点抖动。<br>"
        "坐标也可来自流程变量/表达式：「坐标」填一个值为 (x,y) 的变量"
        "（如 {放技能}，来自『识字取物·读坐标』），或分轴填 x/y 表达式。<br>"
        "变量没读到 / 算不出坐标 = 这一拍不点、只告警（绝不瞎点）。<br>"
        "界面改版或分辨率不对就会点偏，能用「点模板」就别用这个。",
    "swipe":
        "从起点按住拖到终点，时长 ms，坐标按 540×960 基准。<br>"
        "适合列表滚动、地图拖动这类方向固定的手势。",
    "sleep":
        "停固定秒数再走下一步；填了「上限」则在 [下限,上限] 随机等，反秒表节奏。<br>"
        "有明确的图等就用「等模板」——图一出现就走，更省时间。",
    "debug_print":
        "纯观察节点：往日志打文本 + 可选检测结果（图匹配得分/位置、坐标像素颜色、"
        "流程变量值）。<br>"
        "文本里的 {变量名} 会自动换成该流程变量现值（识字取物读数存的），"
        "没读到的变量原样留着好排查。<br>"
        "永远算成功，不改变流程走向，排查「这一步画面/数值什么状态」用。",
    "save_assets":
        "资产入库：把「识字取物」读数存的流程变量数值，按当前账号"
        "写进资产账本（金币/钻石这类，表格「资产」列与右键「查看资产」的数据源）。<br>"
        "配方三步：[识字取物 文字=金币 用法=识文读数字 变量=gold] →"
        " [识字取物 文字=钻石 用法=识文读数字 变量=diamond] →"
        " [资产入库 条目 gold→金币、diamond→钻石]。<br>"
        "同一次挂机跑多次 = 账面覆盖成最新值；账本按显示名记，改变量名不乱账。<br>"
        "没绑账号 / 变量还没读到值时只 WARN 不拦路，永远算成功"
        "（顺路记个数，不该把挂机带崩）。",
    "compute":
        "计算赋值：算一段算术表达式，把结果写进一个流程变量，供后面的"
        "判断 / 打印 / 资产入库用。<br>"
        "表达式里的变量名<b>直接写</b>（如 体力 - 50），也可写占位符 "
        "{变量名} 或 {账号.列名}（运行时按本实例绑定账号取值）。<br>"
        "支持的运算符：<b>+ - * / // % **</b> 和括号；支持的函数："
        "<b>abs / min / max / round / int / float / len</b>。<br>"
        "例：体力-50（扣体力）、金币/2（取一半）、max(0, 体力-50)（不扣成负数）。<br>"
        "结果尽量是整数（10/2=5）；算不出来（表达式写错 / 拿文字做减法 /"
        "除零 / 变量名没填）只打一行 WARN 跳过，<b>不改变流程走向</b>，"
        "绝不把挂机带崩。<br>"
        "本版只做数字运算，不做字符串拼接（字符串参与算术会被拒）。<br>"
        "典型：识字取物读到体力 → 计算 体力=体力-50 → 判断 体力>=10。",
    "branch":
        "按图/颜色/文字/变量判断：条件成立走「Y」出口，不成立走「N」出口。<br>"
        "可加多条条件（属性面板「＋ 添加条件」）：选「满足全部（与）」或"
        "「满足任一（或）」，每条还能单独「取反」。多条件在同一帧一起判 —— "
        "串联两个判断节点做不到（后一个要等下一拍），闪瞬即逝的状态靠这里。<br>"
        "每条可设「持续时长」：条件要<b>连续成立满 N 秒</b>才算成立，图只闪"
        "一下没撑满走 N 口（防闪烁误判，如结算画面/特产图标一闪而过）。<br>"
        "文字识别可选「限定识别区域」（预览上框选即可）或全屏扫描。<br>"
        "模板匹配可选「限定搜索区域」：区域跟着条件走不碰模板账本——同一张图"
        "（如列表每行一个的锁图标）在多个条件里各框各的位置，谁框谁的。<br>"
        "变量比较配「识字取物」的读数用法用：先 [读体力→变量 体力]，再"
        "[判断 体力>=10]，Y 去刷本 / N 收工 —— 变量还没读到值恒走 N。<br>"
        "分支 = 从节点右侧端口拉线连到目标节点。<br>"
        "列表太长目标不在第一屏？「认不出时」选<b>滑动翻页继续找</b>（与识字取物同款）："
        "每次窗口到期就滑一页再判，滑满设定次数仍没有才走 N；"
        "可勾「限定滑动区域」在预览上框住列表区——起落点都钳在框内，"
        "免得整屏跟着滑、误触顶栏底栏。<br>"
        "判不过走 N 时自动打一行日志找原因（模板条件带最高分/阈值，一眼分清"
        "差一点还是没这图）；判得过走 Y 也打一行结论；备注名会出现在日志里，"
        "想打印什么写备注里。<br>"
        "适合「如果画面出现 X 就做 Y，否则做 Z」。",
    "subflow":
        "把另一条已保存的流程整个嵌进来跑：进入前可选等图当前置门。<br>"
        "「子流程期间主流程监视照常」关掉后，跑本条子流程期间主流程的并行监视"
        "哨兵停手，弹窗全凭子流程自己处置（子流程内可自放监视节点）。<br>"
        "「子流程参数」表可按参数复用同一条子流程：左边填形参名（子流程里"
        "{变量名} 就引它）、右边填实参表达式 —— 进入子流程前逐个求值写进流程变量，"
        "子流程内照常引用。实参支持字面量（10）、变量（体力）、算术（体力-10）、"
        "{账号.列名}；同名变量子流程内改的是同一个（全局共用，不做作用域隔离）。"
        "如调用时传「体力下限 = 10」，子流程内 {体力下限} 直接用。<br>"
        "子流程跑完走「Y」出口；子流程失败或进门等图超时走「N」出口，"
        "「N」口不连线则整图判失败。<br>"
        "公共片段复用它；不允许成环引用。",
    "server_pick":
        "识字取物（旧名「选区点击」）：OCR 认出目标文字那一行，按「用法」处置。<br>"
        "用法四选一：<br>"
        "① 点文字 —— 选区/选服老行为；目标留空 = 运行时读账号表"
        "「登录区服」，一条流程多号各选各服；可配偏移 + 落点抖动。<br>"
        "要点的是文字旁边的按钮（如商店商品名下面的价签）？"
        "「<b>点选目标位算偏移</b>」与点模板同款：预览上先认出目标字画绿框，"
        "再点一下真正要按的位置，偏移自动回填。<br>"
        "② 识文读数字 → 存流程变量 —— 先认出带目标文字那一行再抠数字"
        "（金币:12345 → 12345，带单位认 1.17万 → 11700、1.2亿 → 120000000），"
        "写进指定变量；整行没数字算没认出，继续等/滑动，绝不写脏值。<br>"
        "③ 读数字（不认文字锚点）—— 专治资源条那种「图标+数字、没有文字"
        "标签可当锚点」的读数：在预览上框住数字那一小块（<b>必须框</b>），直接"
        "认框里的字并按单位换算，同样认 1.17万 → 11700。框里 0 个或多个数字"
        "都算没认出（不猜读哪个），按认不出收口。金币、钻石各摆一个节点。<br>"
        "④ 读出坐标 → 存流程变量 —— 把字的位置记下来，后接点坐标/滑动配合用。<br>"
        "「识别稳定(s)」：认出后要连续认出<b>同一结果</b>满这么多秒才动作 —— "
        "OCR 半秒一张，过场动画里目标词一闪而过容易误认，设上它更稳"
        "（不填 = 认出即处置）。<br>"
        "认不出时三档：退出程序 / 跳过继续 / <b>滑动翻页找</b>（每到期滑一次、"
        "等画面停稳再认，最多滑设定次数；适合列表里找服/找活动入口）。"
        "滑动可「限定滑动区域」：框住列表才滑，起落点钳在框内，不框=全屏滑。<br>"
        "可选识别区域（ROI）缩小范围提速提准：手填坐标或「在预览上框选」"
        "（用法③必框）。<br>"
        "裁决规则：目标里的数字必须原样出现（6 服绝不认成 9 服），"
        "标题拆出的单字碎片冒充整行判 0 分；相似度阈值宁高勿低，认不出绝不瞎点。<br>"
        "旧「选区选服」两段用法已删：进服两步摆 [点文字 框区列表] → [等待] →"
        "[点文字 框服列表]，两步各配各的处置还更灵活；载入旧流程会自动换算。<br>"
        "OCR 依赖（rapidocr + onnxruntime）没装时编译成告警跳过。<br>"
        "红色「N」出口：认不出（含滑完次数还没有）/账号表没登记区服时走这条线"
        "（接了 N 口，「超时退出程序」不再生效）。",
    "multi_scan":
        "遍历全部：把画面里同一目标的所有达标命中一次收清，再按「模式」逐处"
        "处置完才收口，一个节点顶 N 次操作。<br>"
        "检测源两选一：模板图（同款图标整屏找）/ 文字OCR圈字（按识别行圈目标，"
        "头像没固定图也能按昵称旁的文字逐个点名，不依赖模板；需装 OCR 依赖）。<br>"
        "模式三选一：<br>"
        "① 逐个点掉 —— 每拍点一处（同「点模板·点全部」），命中数还会写进"
        "流程变量（默认名「遍历命中数」，判断节点可用它做后续决策）。<br>"
        "② 进一个 —— 每处 = 点它 → 跑选好的子流程（点赞/领取这类进店动作）"
        "→（可选，默认关）按返回键回列表 →（可选）等「列表标志图」出现确认真的"
        "回家了 → 下一处。返回键关着 = 回家由子流程自己负责。"
        "单处子流程没跑成只记一笔照常办下一处；返回后等不回列表页 = 画面上"
        "旧坐标全作废，本节点直接判失败走「N」口，绝不瞎点。<br>"
        "③ 逐个读数 —— 一下都不点：每处读「邻域框」里的数字（1.17万也认，"
        "折成 11700），依次存成 变量_1、变量_2…；框里数字没有或不止一个算"
        "没认出（绝不写脏值），该处变量不写、只记一笔 WARN。<br>"
        "先等图再开扫（可关，超时三档同点模板）；可「限定搜索区域」只在列表"
        "区内找，防把屏幕角落的同款图标也算进目标。<br>"
        "去重边扫边办（勾上）：不吃快照 —— 每办完一处重新采集，办过的（按"
        "邻域框里读到的名字认脸，读不到按坐标格子）跳过，没有新面孔才收口，"
        "会自己刷新/补位的动态列表用它；扫满「最多轮数」还停不下来 = 判失败"
        "走「N」口。不勾 = 快照模型：坐标只收一次不重扫，适合「回列表后列表"
        "不变」的静态页面。<br>"
        "数字过滤（勾上）：每处先就地读「邻域读数框」里的数字，满足"
        "「小于 / ≤ / 等于 / ≥ / 大于 / 不等于 阈值」才点它或进它；读不出"
        "（框里没数字或不止一个）当不达标跳过 —— 绝不拿未知数落手。"
        "「遍历所有卡片、只点爱心<20 的」这类活一个节点配完，不用复制"
        "N 份；命中全被滤光时按「没活可干」正常收口，不判失败。读数模式"
        "一下都不点，此项无意义会置灰。<br>"
        "红色「N」出口：扫描目标没等到（按配置判败）、进一个模式迷路、或"
        "去重扫满轮数时走这条线。<br>"
        "「收到即结束的信号」可登记信号名：正在办的这一处照办完（绝不半途"
        "撕当前项），下一处开碰之前查信箱，同名信号躺着就提前收工——"
        "剩几处不碰了会记在日志里，算正常跑完走出口。",
    "loop":
        "反复执行循环体动作行，直到「退出条件」满足或轮数到上限。<br>"
        "退出条件可加多条（「＋ 添加条件」）：满足全部（与）/任一（或）、"
        "每条可取反 —— 「回到主城 或 体力耗尽才出循环」这类复合出口一个"
        "节点配完，每轮收口时一起判。<br>"
        "每条可设「持续时长」：条件要<b>连续成立满 N 秒</b>才算出口成立 —— "
        "只闪一下没撑满不出循环，防结算画面/特产图标一闪而过误出。<br>"
        "与并行监视/定时执行的关系：子链在跑时主线整条自动暂停，"
        "本循环跟着冻结、办完从暂停处续跑（不用连任何线）。<br>"
        "轮数上限填 0 = 不限轮（挂机常驻，只认退出条件）。<br>"
        "「收到即结束的信号」可登记信号名（发信号节点投的那种）：箱子"
        "里躺着同名信号就提前收工（不等圈底，正在等的下一步不再等），"
        "算正常跑完走出口；信号一次销账，外层同名不受牵连。",
    "stop_flow":
        "主动结束流程：命中后按节点上选的「结束范围」收口，不上失败恢复链。<br>"
        "范围=当前流程（默认）：谁的边界谁收口 —— 放子流程里只结束那条子流程"
        "（算跑完走 Y 口，主线继续），放循环里只跳出循环，放主流程顶层才结束整条任务。<br>"
        "范围=整条流程：不管埋在哪一层，命中即整条任务立即停止（全局急停）。<br>"
        "范围=跳过本轮：只对循环体有意义 —— 命中即丢掉本轮剩余步骤、立即开始"
        "下一轮（等价循环的 continue；放循环外没人接，等于结束整条任务）。<br>"
        "适合「达成条件即收尾」场景（如登录成功、任务完成），避免继续跑后续节点。",
    "signal":
        "发信号：往共享信箱投一个「信号名」就直通往下走，永不失败。<br>"
        "本节点只管发，不管谁收 —— 外面套着的「循环 / 遍历全部」节点在"
        "属性里登记同名「收到即结束的信号」名单，才会被掐：循环不等圈底、"
        "遍历跑完当前这一处就收工，都算正常跑完走出口续跑主线。<br>"
        "信号一次销账：谁先登记谁吃掉，外层同名不会再连掐，不会一层掐穿"
        "层层陪葬。<br>"
        "典型用法：子流程里判出「体力耗尽」发信号「收工」，套在外层一直刷"
        "体力的循环登记「收工」→ 一判出整个循环就地结束，主线接下一步。<br>"
        "子流程里发的信号外层照样收得到（父子共用一个信箱）；没人登记的"
        "信号就静静躺在箱里，不影响任何流程。",
    "watch":
        "并行监视哨兵：放上画布就全程在岗 —— 主线照跑自己的，"
        "它每拍扫一遍监视行，认出哪个弹窗就按该行的行型处理。<br>"
        "每行 = 「弹窗模板 → 行型」，扫描顺序 = 优先级。行型四种：<br>"
        "「命中即点」= 认出图标直接点它一下（纯关弹窗，不跑子流程）；<br>"
        "「封号停用」= 认出封号弹窗时不点不跑链：当场截图留证、本实例收工，"
        "账号被标记封号（表格红显、挂机自动跳过，右键「解禁」恢复）；<br>"
        "「动态手指」= 不认图：每拍现扫「白块+黑描边」的引导手指（对缩放/旋转/"
        "只露半边都免疫），摸到就点它指的位置；无模板名，参数管阈值/面积/外延；<br>"
        "「跑子流程」= 认出图标后跑一条子流程办它，跑时主线整条自动暂停，"
        "办完自动从暂停处续跑（默认行为，不用接线）。<br>"
        "「跑子流程」行可勾「即点」：认出图标先自动点它一下再进子流程，"
        "子流程里不用再配「点模板」入口。<br>"
        "两种点图标都能学「点模板」采偏移：「点选目标位算偏移」在预览上点一"
        "下真正要按的位置，之后命中就点中心+偏移（关闭按钮不在图正中央时用）。<br>"
        "命中一条子流程跑完后进冷却（默认 5 秒）再监视，防关不掉的图每拍连点。<br>"
        "同一条子流程连续 3 次没搞定 = 停用该行并打 WARN（本流程内不再监视），"
        "防一个顽固弹窗反复拖死主线。<br>"
        "主线全部跑完时哨兵随流程一起收工（不让线程永远不退）。<br>"
        "多台监视可并成一台容器管理：拖一台监视（或整台容器）压到另一台身上"
        "松手即并成一组，没名字的组自动冠上原容器名（认得出哪批是并过来的）；"
        "拖组标题行拆出去，拆出来的照样是一台容器，随时能再并回来。<br>"
        "适合挂机全程都可能冒出来的弹窗：升级提示、体力赠送、每日签到提醒。",
    "timer":
        "定时执行哨兵：放上画布就全程在岗 —— 不用认图，每行「间隔 → 子流程」，"
        "时间一到自动跑一次那条子流程，跑完歇满下一个间隔再跑。<br>"
        "适合到点必做的事：每小时领体力、每 30 分钟日常扫一遍、定时商店刷新。"
        "第一次触发在流程起跑后的第一个间隔到点时（起跑即计时）。<br>"
        "与并行监视（watch）同一套机制，只是触发从「认到图」换成「看时钟」："
        "子流程在跑时主线整条自动暂停，办完自动续跑（同 watch，不用接线）。<br>"
        "子流程失败不烧周期：歇「失败重试间隔」（默认 5 秒）就重试，"
        "连续 3 次没搞定 = 停用该行并打 WARN，防一条坏流程反复触发拖死主线。<br>"
        "主线全部跑完时随流程一起收工（不让线程永远不退）。",
}


def _graph_id() -> str:
    """本地同款 id 生成（flow.py 的 _graph_id 是私有，编辑器不 import 私有名）。"""
    return uuid.uuid4().hex[:8]


def _dist2(a: QPointF, b: QPointF) -> float:
    """两点平方距离（只比大小，省一次开方）：判断鼠标离线的哪一端更近。"""
    dx, dy = a.x() - b.x(), a.y() - b.y()
    return dx * dx + dy * dy


def _hint(text: str) -> QLabel:
    """属性面板里的灰色小字说明（全文件共用一种样式，别再各写各的）"""
    h = QLabel(text)
    h.setWordWrap(True)
    h.setObjectName("secondary")
    h.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_MUTED};")
    return h


def _fit_btn(btn: QPushButton) -> QPushButton:
    """把按钮收回自然宽度（属性面板 318px 里，QFormLayout 默认把按钮拉满
    一行，"删除"被抻成 306px 的横幅、和旁边输入框不成比例）。
    Fixed 策略 = 按 sizeHint 定宽 + 左对齐，与输入框列同一视觉重量。"""
    btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return btn


def _vis(w: QWidget, on: bool):
    """构建期安全的显隐。属性面板表单是先攒在无父 QWidget 里、最后才挂进
    props_panel 的：这期间对无父控件 setVisible(True) 等于当场把它弹成
    "python"标题的孤儿顶层小窗闪一帧（实测：切到认不出时=滑动的识字取物
    节点、或勾了随机等待的 sleep 节点，画布旁白框一闪）。
    新控件默认就是可见的，所以构建期只需要"藏"；挂进面板后照常显隐。"""
    if on:
        if w.parentWidget() is not None:
            w.show()
    else:
        w.hide()


def _set_opt_key(node: dict, key: str, val):
    """可选字符串键的懒写入（F3 坐标变量 coord/x_expr/y_expr 用）：
    值非空才落键，空则 pop —— 打开看一眼老节点面板绝不写空键，保持
    「没填 = 走老行为」的零污染纪律（同 stop_on / stable_secs 的懒挂键口径）。"""
    s = str(val or "").strip()
    if s:
        node[key] = s
    else:
        node.pop(key, None)


def _acct_token_cmb(edt: QLineEdit) -> QComboBox:
    """「插入账号列」下拉：选中就用 {账号.列名} **整行替换**输入框原内容。
    原先是"插到光标处"，实测反直觉：光标落在哪自己都不知道，点两下越插越乱，
    想换一个列名还得先手动删干净——本行的语义本来就是"这一行填什么"，
    覆盖比追加直观（要拼串直接手打）。
    列名从 core.account_io._IMPORT_FIELDS 现场派生（账号表加列/改列名这里
    零维护），老别名靠"字段第一次出现"去重；密码/启用/序号不提供——
    与 bot 侧 _fmt_text 的取数规则同款（密码防进日志）。
    「选区/选服」是引擎登记的派生列（bot 侧 _ACCT_DERIVED）：都从区服列现推，
    账号表不用加列——进服两步各挑一个就完事。"""
    from bot.tasks.flow import _ACCT_DERIVED   # 派生列名单单一来源
    cmb = QComboBox()
    cmb.addItem("插入账号列…")
    seen = set()
    for header, field in account_io._IMPORT_FIELDS.items():
        if field in ("password", "enabled", "ld_index") or field in seen:
            continue
        seen.add(field)
        cmb.addItem(header, "{账号.%s}" % header)
    # 派生列插在「区服」后面：进服两步天然连着挑，不用翻到队尾
    at = cmb.findData("{账号.区服}")
    at = at + 1 if at >= 0 else cmb.count()
    tips = {"选区": "区服列前 4 个字（含沙射影6服→含沙射影）：点区那步用",
            "选服": "区服列全名（含沙射影6服）：点服那步用，数字锚点防认错服"}
    for i, dcol in enumerate(_ACCT_DERIVED):
        cmb.insertItem(at + i, dcol, "{账号.%s}" % dcol)
        cmb.setItemData(at + i, tips.get(dcol, ""), Qt.ToolTipRole)
    cmb.setToolTip("挑一列，用 {账号.列名} 替换本行内容（原有文字会被覆盖）：\n"
                   "运行时按本实例绑定的账号取值，多开各号各服写一次全号通用，不用手填。\n"
                   "选区/选服 = 区服列现推的两个派生值，账号表不用加列。")
    cmb.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def _pick(i, e=edt, c=cmb):
        tok = c.itemData(i)
        if tok:
            e.setText(tok)         # 整行覆盖并触发 textChanged，账本自动同步
            c.setCurrentIndex(0)   # 复位，下次还能挑同一列
    cmb.currentIndexChanged.connect(_pick)
    return cmb


def _acct_token_row(edt: QLineEdit) -> QWidget:
    """输入框 + 「插入账号列」下拉拼成一行（判断/识字取物共用）。
    下拉定宽不吃列，输入框照旧长缩；整行作为表单字段列，标签列不受影响。"""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(edt)
    lay.addWidget(_acct_token_cmb(edt))
    return row


_CAP_TIP = ("去采模板：切到采集页并把这张图采进这一行的归属目录，\n"
            "采完自动填回本行")

_RECUT_TIP = ("重新采集这张图：跳到采集页并锁名「同名覆盖」——框选好新图保存，\n"
              "原图换芯不换名，所有用到它的流程立刻改用新图（不用重挂）。\n"
              "存盘前会再弹一次覆盖确认。")


def _icon_btn(glyph: str, tip: str) -> QPushButton:
    """属性面板里的图标小钮（采图 / 删行）。固定 28×24，零 padding ——
    全局 QPushButton 的左右 padding 共 28px，固定宽小钮的内容区会被挤成
    负数、字形直接画不出来（theme.py#iconBtn 记的同一个坑），所以样式走
    objectName 而不是在这里 setStyleSheet。提示文案必配：光图标说不清。"""
    b = QPushButton(glyph)
    b.setFont(icons.ICON_FONT)
    b.setFixedSize(28, 24)
    b.setObjectName("flowIcon")
    b.setToolTip(tip)
    return b


def _row_move_btns(items: list, idx: int, resync):
    """优先级/顺序清单里每一行的「↑ 上移 / ↓ 下移」小钮对（返回 [up, down]）。
    扫描顺序=优先级的模块行（清弹窗行、并行监视）与顺序执行的循环体行共用：
    列表换位后整行数据原样跟着走，不动任何字段。首行禁↑、末行禁↓——
    按大众习惯置灰而不是藏起来（位置固定不跳，也知道到头了）。
    resync = 换位后重建（行增删同款：改节点高度/重画表单）。"""
    btns = []
    for glyph, tip, delta in ((icons.UP, "上移（提高优先级/提前执行）", -1),
                              (icons.DOWN, "下移（降低优先级/延后执行）", 1)):
        b = _icon_btn(glyph, tip)
        j = idx + delta
        b.setEnabled(0 <= j < len(items))
        if b.isEnabled():
            b.clicked.connect(
                lambda _=False, k=idx, jj=j, L=items, rs=resync: _swap_rows(L, k, jj, rs))
        btns.append(b)
    return btns


def _swap_rows(items: list, a: int, b: int, resync):
    """把清单里 a、b 两行换位后重建（上移/下移共用）。"""
    items[a], items[b] = items[b], items[a]
    resync()


def _add_btn(text: str, tip: str = "") -> QPushButton:
    """属性面板的「＋ 添加…」行钮：加号图标 + 文字 + 自然宽度。
    原来每颗都被拉成 306px 满行横幅，"添加"这种动作不需要那么大的靶子，
    也和上方输入框列宽对不齐。"""
    b = QPushButton(text)
    b.setIcon(icons.glyph_icon(icons.ADD, 14))
    b.setIconSize(QSize(13, 13))
    if tip:
        b.setToolTip(tip)
    return _fit_btn(b)


def _clear_layout(l_) -> None:
    """布局拆建铁律（_clear_props 同款）：嵌套布局连子控件一起 hide+deleteLater，
    绝不 setParent(None)（无父=顶层孤儿小窗闪一帧，实测坑）。监视行编辑区与
    _row_list_editor 共用这一份。"""
    def _take(item):
        if item is None:
            return
        if sub := item.layout():
            while sub.count():
                _take(sub.takeAt(0))
            sub.setParent(None)
            sub.deleteLater()
        if w := item.widget():
            w.hide()
            w.deleteLater()
    while l_.count():
        _take(l_.takeAt(0))


def _plain(html: str) -> str:
    """节点说明的富文本 → 纯文本（搜索框/列表 tooltip 用，QListWidget 不渲染 HTML）"""
    return re.sub(r"<[^>]+>", " ", html).replace("\n", " ")


_COND_KIND_CN = {"template": "模板匹配", "color": "坐标取色",
                 "multi_color": "多点取色", "text": "文字识别",
                 "var": "变量比较", "motion": "动态检测"}


def cond_row_summary(cd: dict) -> str:
    """判断/循环出口条件行的摘要文本（行列表专用，纯函数可单测）。
    类型中文名 + 关键参数；取反带尾标；没填完的行直说，不留空白行谜团。"""
    k = str(cd.get("kind") or "template")
    lbl = _COND_KIND_CN.get(k, k or "?")
    if k == "template":
        det = cd.get("name") or "（没选图）"
    elif k == "text":
        det = f"「{cd.get('target') or '没填文字'}」"
    elif k == "var":
        op = cd.get("op", ">=")
        if op in ("between", "not_between"):
            # 区间比较：显示「值 下界~上界」（区间外冠「外」尾标）
            det = (f"{cd.get('name') or '?'}"
                   f"{cd.get('value', '')}~{cd.get('value2', '')}")
            if op == "not_between":
                det += "外"
        else:
            det = f"{cd.get('name') or '?'}{op}{cd.get('value', '')}"
    elif k == "multi_color":
        det = f"{len(cd.get('points') or [])} 点"
    else:                      # color/motion/未知型：参数长或没有，光类型名
        det = ""
    tail = "（取反）" if cd.get("not") else ""
    h = float(cd.get("hold_secs", 0) or 0)
    if h > 0:
        tail += f"·持续{h:g}s"
    return (f"{lbl}·{det}" if det else lbl) + tail


_STEP_KIND_CN = {"click": "点坐标", "click_template": "点图", "wait_template": "等图",
                 "swipe": "滑动", "sleep": "等待", "popup_zone": "清弹窗",
                 "subflow": "子流程", "signal": "发信号", "stop_flow": "结束流程"}
# 循环体动作的类型文案单一来源 = 上面的 _STEP_KIND_CN（加一种类型 = 只改这张表）。
# 画布动作条的缩写列（CN 名太长的才登记，其余直接复用 CN 名）：
_STEP_KIND_ABBR = {"click": "点", "swipe": "滑", "sleep": "停"}
# 类型下拉的展示顺序（点图打头 = 最常用，与表的定义顺序不同）；
# stop_flow 排尾（循环体里"跳过本轮/跳出"是控制语句，不是普通动作）
_STEP_DROP_ORDER = ("click_template", "click", "wait_template", "sleep",
                    "swipe", "popup_zone", "subflow", "signal", "stop_flow")


def step_row_summary(s: dict) -> str:
    """循环体动作行的摘要文本（画布行条同款口径：类型短名 + 关键参数）。"""
    st = str(s.get("type") or "")
    lbl = _STEP_KIND_CN.get(st, st or "?")
    if st in ("click_template", "wait_template"):
        det = s.get("name") or "（没选图）"
    elif st == "click":
        # 有坐标变量/表达式（F3）时摘要显示来源，否则显示写死坐标
        if str(s.get("coord") or "").strip():
            det = f"坐标={s.get('coord')}"
        elif str(s.get("x_expr") or "").strip() or str(s.get("y_expr") or "").strip():
            det = f"({s.get('x_expr', '?')},{s.get('y_expr', '?')})"
        else:
            det = f"({s.get('tap_x', 270)},{s.get('tap_y', 480)})"
    elif st == "sleep":
        det = f"{s.get('seconds', 1)}秒"
    elif st == "swipe":
        det = f"({s.get('x1', 0)},{s.get('y1', 0)})→({s.get('x2', 0)},{s.get('y2', 0)})"
    elif st == "popup_zone":
        det = f"{len(s.get('modules') or [])}模块"
    elif st == "subflow":
        det = s.get("flow_name") or "（没选流程）"
        _pn = len([p for p in (s.get("params") or []) if isinstance(p, dict)])
        if _pn:
            det += f"·传参{_pn}"
    elif st == "signal":
        det = s.get("name") or "（没填信号名）"
    elif st == "stop_flow":
        det = {"global": "整条流程", "continue": "跳过本轮"}.get(
            s.get("stop_scope", "local"), "当前流程")
    else:
        det = ""
    txt = f"{lbl}·{det}" if det else lbl
    lb = str(s.get("label") or "").strip()
    return f"{txt}·{lb}" if lb else txt


class RowListEditor(QWidget):
    """监视行同款「摘要行列表 + 选中行编辑区」通用件（2026-10-06 从并行监视
    10-05 版抽出）：治旧版叠加式编辑器的病 —— 每加一条条件/动作就把整套表单
    当场摊开叠在下面，三条以上面板失控。这里行永远一行高，表单一次只开选中那条。

    宿主全部走回调注入，组件不认得行内容：
      get_rows() -> list[dict]   当前行（dict 身份必须稳定=存储本尊）
      summary(row) -> str        行摘要文本
      build(row, touch) -> QWidget  选中行编辑表单；touch() 重刷该行摘要
      commit(rows)               整表写回存储（换序/增/删/粘贴后都走它）
      on_struct()                结构性变动（增/删/粘贴）后宿主重建路径
                                 （行数常影响节点高/徽章 —— 全量重建，选中靠 sel）
      on_reordered()             换序轻通道（行数没变：标记脏 + 按需重画）
      sel_get/sel_set            选中行记忆（dict 身份）：整场重建后回位不弹回第①行
      clip_get/clip_set          面板级剪贴板（右键复制/粘贴，deepcopy 互不共享）
      suffix(row) -> str         可选：行尾轻刷新后缀（监视 ×N 命中统计同款），
                                 只挂显示不落存储；refresh_texts() 重刷
      menu_ext(menu, row)        可选：右键菜单扩展（监视「移动到其他监视」同款），
                                 标准复制/粘贴/删除之后追加宿主专属项
    ⚠ 拖放换算按 id(row) 建映射回取本体：QVariant 存 dict 回读是等值拷贝，
      身份比对/写回会张冠李戴（监视行同款实测坑）。"""

    def __init__(self, *, get_rows, summary, build, commit, on_struct,
                 sel_get, sel_set, clip_get, clip_set,
                 add_text, new_item, on_reordered=None, min_rows=1,
                 list_tip="拖一行换序；点一行在下面编辑。",
                 empty_hint="（点上面一行开始编辑，或按下面的钮加一条）",
                 suffix=None, menu_ext=None, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._get, self._sum, self._build = get_rows, summary, build
        self._commit, self._on_struct = commit, on_struct
        self._on_reordered = on_reordered or (lambda: None)
        self._sel_get, self._sel_set = sel_get, sel_set
        self._clip_get, self._clip_set = clip_get, clip_set
        self._new_item = new_item
        self._sfx, self._menu_ext = suffix, menu_ext
        self._min = min_rows        # 允许删到几行：条件=1（恒有第1条），循环体动作=0（空循环合法）
        self._add_text = add_text   # 「添加动作/添加停止信号/…」：同一面板多张表时的稳定身份
                                    # （行数会撞车、min 也会撞车，冒烟/调试别拿这俩瞎认）
        self._empty_hint_text = empty_hint

        self.lst = QListWidget()
        self.lst.setObjectName("flowRowList")
        self.lst.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.lst.setDefaultDropAction(Qt.MoveAction)
        self.lst.setSelectionMode(QAbstractItemView.SingleSelection)
        # ⚠ 填项前强制样式生效（监视行同款坑）：全局主题给 item 加了 padding，
        # 不 polish 则按裸字体高排位，相邻两行文字互相压着。
        self.lst.ensurePolished()
        self.lst.setMaximumHeight(150)   # 6 行左右封顶：再多把下面的编辑区顶出视口
        self.lst.setToolTip(list_tip)
        lay.addWidget(self.lst)

        self.editor = QWidget()
        self._edit_lay = QVBoxLayout(self.editor)
        self._edit_lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.editor)

        bar = QHBoxLayout()
        btn_add = _add_btn(add_text)
        btn_add.clicked.connect(self._add)
        bar.addWidget(btn_add)
        bar.addStretch()
        self._b_up = _icon_btn(icons.UP, "上移选中行；挪得远直接在列表里拖")
        self._b_dn = _icon_btn(icons.DOWN, "下移选中行")
        self._b_up.clicked.connect(lambda: self._move(-1))
        self._b_dn.clicked.connect(lambda: self._move(1))
        bar.addWidget(self._b_up)
        bar.addWidget(self._b_dn)
        lay.addLayout(bar)

        self._built = [None]

        def _sync_from_view():
            rows = self._get()
            byid = {id(r): r for r in rows}
            try:
                order = [byid[self.lst.item(i).data(Qt.UserRole)]
                         for i in range(self.lst.count())]
            except KeyError:      # 行在悬停期间被外部重建过：整表回灌，绝不写坏数据
                self._fill()
                return
            if [id(x) for x in order] != [id(x) for x in rows]:
                self._commit(order)
                self._on_reordered()
                self._fill()
        self.lst.model().rowsMoved.connect(
            lambda *_: QTimer.singleShot(0, _sync_from_view))
        self._sync = _sync_from_view   # 冒烟直测拖放换算通道（监视行 _watch_sync 同款）

        self.lst.setContextMenuPolicy(Qt.CustomContextMenu)

        def _menu(pos):
            it = self.lst.itemAt(pos)
            k = self.lst.row(it) if it is not None else -1
            rows = self._get()
            valid = 0 <= k < len(rows)
            menu = QMenu(self.lst)
            if valid:
                menu.addAction("复制此行").triggered.connect(
                    lambda _c=False, r=rows[k]: self._copy(r))
            if self._clip_get() is not None:
                menu.addAction("粘贴为新行").triggered.connect(
                    lambda _c=False, kk=k: self._paste(kk if valid else -1))
            if valid and len(rows) > self._min:
                menu.addAction("删除此行").triggered.connect(
                    lambda _c=False, kk=k: self._delete(kk))
            if valid and self._menu_ext is not None:
                self._menu_ext(menu, rows[k])   # 宿主专属项（如「移动到其他监视」）
            if not menu.isEmpty():
                menu.exec(self.lst.mapToGlobal(pos))
        self.lst.customContextMenuRequested.connect(_menu)
        self.lst.currentRowChanged.connect(lambda _: self._rebuild_editor())
        self._fill()   # 建好即回位：sel 记忆在表里就选它，否则选第①行（表单当场可见）

    # ── 内部工具 ──
    def _index(self, r):
        for k, x in enumerate(self._get()):
            if x is r:
                return k
        return -1

    def _line(self, i, r):
        """一行完整显示文本：编号徽章 + 摘要（+ 可选行尾后缀，如命中统计 ×N）。"""
        t = f"{_group_badge(i)} {self._sum(r)}"
        sfx = self._sfx(r) if self._sfx is not None else ""
        return t + (f"  {sfx}" if sfx else "")

    def refresh_texts(self):
        """轻刷新：按当前行序重刷所有行文本（后缀/摘要变了才写），不拆建 ——
        监视 3s 命中统计这类「显示跟数据、绝不碰编辑区控件」的通道走它。"""
        rows = self._get()
        for i in range(min(self.lst.count(), len(rows))):
            it = self.lst.item(i)
            t = self._line(i, rows[i])
            if it is not None and it.text() != t:
                it.setText(t)

    def editing_row(self):
        """当前编辑区渲染的是哪一行（dict 身份；无行 = None）。宿主信息行控件
        回投前先比对它，防止把文本写进已 deleteLater 的旧编辑区（监视 _lbl_ref 同款）。"""
        return self._built[0]

    def _fill(self):
        rows = self._get()
        sel = self._index(self._sel_get())
        self.lst.blockSignals(True)
        self.lst.clear()
        for i, r in enumerate(rows):
            it = QListWidgetItem(self._line(i, r))
            it.setData(Qt.UserRole, id(r))   # 存 id 整数（等值拷贝坑的解法）
            it.setToolTip("拖放/↑↓ 换序 · 右键 复制/粘贴/删除")
            self.lst.addItem(it)
        self.lst.blockSignals(False)
        if rows:
            self.lst.setCurrentRow(sel if 0 <= sel < len(rows) else 0)
            # ⚠ 建期列表 viewport 还是 0×0，setCurrentRow 的自动滚入视野会落空
            # （选中在、滚动条却回顶，监视行实拍「每次整场重建都要重新往下滑」）：
            # 排到事件尾，面板摆好后补滚一次。
            QTimer.singleShot(
                0, lambda: (self.lst.currentItem() is not None
                            and self.lst.scrollToItem(self.lst.currentItem())))
        self._rebuild_editor()

    def _touch(self, r):
        """只重刷指定行的摘要文本（编辑改类型/参数后行上跟随；别的行没动不碰）。"""
        i = self._index(r)
        if 0 <= i < self.lst.count():
            it = self.lst.item(i)
            t = self._line(i, r)
            if it is not None and it.text() != t:
                it.setText(t)

    def _rebuild_editor(self):
        rows = self._get()
        k = self.lst.currentRow()
        r = rows[k] if 0 <= k < len(rows) else None
        if r is not None:
            self._sel_set(r)
        self._b_up.setEnabled(r is not None and k > 0)
        self._b_dn.setEnabled(r is not None and k < len(rows) - 1)
        if r is self._built[0]:
            return   # 同行不重建：表单控件还活着，拆建纯属闪一下
        self._built[0] = r
        _clear_layout(self._edit_lay)
        if r is None:
            self._edit_lay.addWidget(_hint(self._empty_hint_text))
            return
        self._edit_lay.addWidget(self._build(r, lambda rr=r: self._touch(rr)))

    # ── 结构操作（全部先落 sel 记忆再动存储，宿主整场重建后靠身份回位）──
    def _add(self):
        rows = self._get()
        nr = self._new_item()
        rows.append(nr)
        self._commit(rows)
        self._sel_set(nr)
        self._on_struct()

    def _delete(self, k):
        rows = self._get()
        if not (0 <= k < len(rows)) or len(rows) <= self._min:
            return
        rows.pop(k)
        self._sel_set(rows[min(k, len(rows) - 1)] if rows else None)
        self._commit(rows)
        self._on_struct()

    def _paste(self, after_idx):
        clip = self._clip_get()
        if clip is None:
            return
        rows = self._get()
        nr = copy.deepcopy(clip)
        k = after_idx + 1 if 0 <= after_idx < len(rows) else len(rows)
        rows.insert(k, nr)
        self._commit(rows)
        self._sel_set(nr)
        self._on_struct()

    def _copy(self, r):
        self._clip_set(copy.deepcopy(r))

    def _move(self, delta):
        rows = self._get()
        i = self.lst.currentRow()
        j = i + delta
        if not (0 <= i < len(rows) and 0 <= j < len(rows)):
            return
        moved = rows[i]
        rows[i], rows[j] = rows[j], rows[i]
        self._commit(rows)
        self._sel_set(moved)
        self._on_reordered()
        self._fill()


def _fill_category_combo(cmb: QComboBox, current: str):
    """归属下拉：目录值存 data、显示"中文名（目录）"（任务↔模板绑定改版）。"""
    names = template_deps.task_dir_display()
    cur = (current or "").strip()
    # 节点存的旧值（含孤儿目录）不在账本里也要能显示，垫到最前避免丢失
    extra = [cur] if cur and cur not in names else []
    for cat in template_manager.list_categories() + [c for c in extra
                                                     if c not in template_manager.list_categories()]:
        # 只显示库名；想查存储目录悬停 tooltip（自定义库目录名=库名，重复显示没意义）
        cmb.addItem(names.get(cat, cat), cat)
        idx = cmb.count() - 1
        cmb.setItemData(idx, f"存储目录：{cat}", Qt.ToolTipRole)
    idx = cmb.findData(cur) if cur else -1
    cmb.setCurrentIndex(idx if idx >= 0 else 0)


# ── 节点模板缩略图 ────────────────────────────────────────────────────
# 节点方块上直接画出所选模板的小图标。paint 每帧都跑，绝不能每次都去
# 读盘转码：按 (目录, 名字) 缓存转换好的 QPixmap；查不到的也缓存 None
# （负缓存，防每帧全量扫账本）。模板库任何变化时整表清空重建即可，
# 图可能被删/改名/重采覆盖，做精细失效不值得。
_thumb_cache: dict[tuple[str, str], QPixmap | None] = {}


def _numpy_to_qpixmap(img) -> QPixmap | None:
    """numpy 数组 → QPixmap：BGR→RGB 后 .copy() 脱离 numpy 缓冲区，
    否则原数组被回收时图像悬空。与 ui.template_tool 同款实现，不 import
    那个模块 —— 免得把采集面板的整串依赖拖进流程编辑器。
    BGRA（抠图模板）走 RGBA8888 保留透明，节点缩略图右上角就是透明的。"""
    if img is None or getattr(img, "size", 0) == 0:
        return None
    h, w = img.shape[:2]
    if img.ndim == 3 and img.shape[2] == 4:
        rgba = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
        qimg = QImage(rgba.data, w, h, 4 * w, QImage.Format_RGBA8888).copy()
    else:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(qimg)


def _template_thumb(cat: str, name: str) -> QPixmap | None:
    """按 (目录, 名字) 取模板缩略图（带缓存）。缺账本/读盘失败返回 None。"""
    if not name:
        return None
    key = (cat or "", name)
    if key in _thumb_cache:
        return _thumb_cache[key]
    pix = None
    entry = template_manager.find_template(cat or "", name)
    if entry is not None:
        pix = _numpy_to_qpixmap(template_manager.load_template_image(entry))
    _thumb_cache[key] = pix
    return pix


def clear_thumb_cache():
    _thumb_cache.clear()


# ── 节点外观 ──────────────────────────────────────────────────────────
NODE_W, NODE_H = 200, 64
PORT_PAD = 8   # boundingRect 外扩：罩住端口圆（缘外 4px）与选中框（3px 宽）

THUMB_W = 48   # 节点方块上模板缩略图的固定显示宽（高按原图比例算）

TYPE_LABEL = {
    "click_template": "点模板",
    "wait_template": "等模板",
    "click": "点坐标",
    "swipe": "滑动",
    "sleep": "等待",
    "debug_print": "打印",
    "save_assets": "资产入库",
    "compute": "计算",
    "branch": "判断",
    "subflow": "子流程",
    "server_pick": "识字取物",
    "multi_scan": "遍历全部",
    "stop_flow": "结束流程",
    "signal": "发信号",
    "loop": "循环",
    "watch": "并行监视",
    "timer": "定时执行",
}
# 节点调色板（「＋ 节点 ▾」下拉菜单的内容，按用途分三组）。
# 有意不列 click（点坐标）：随分辨率漂移是 footgun。back（返回键）类型已整体
# 下线（含编译分支）—— 老流程里存过的 back 节点编译为 None、按跳过处理。
NODE_GROUPS = [
    ("动作", ["click_template", "wait_template", "swipe", "sleep", "server_pick",
              "multi_scan"]),
    ("流程控制", ["branch", "subflow", "loop", "watch", "timer", "stop_flow",
                 "signal"]),
    ("变量", ["compute"]),
    ("调试", ["debug_print", "save_assets"]),
]

TYPE_COLOR = {
    "click_template": "#42a5f5",
    "wait_template": "#42a5f5",
    "click": "#26a69a",
    "swipe": "#26a69a",
    "sleep": "#78909c",
    "debug_print": "#8e24aa",   # 紫色：调试节点一眼可辨，白字对比也够
    "save_assets": "#c2185b",   # 品红：和打印的紫、结束的玫红同族但分得开——落库类
    "compute": "#00897b",       # 深青绿：变量运算/赋值，和调试的紫拉开
    "branch": "#ff7043",
    "subflow": "#5c6bc0",       # 靛蓝：复用型模块，和判断的橙区分开
    "loop": "#3949ab",          # 深靛：循环容器（复用型+区域型之间）
    "server_pick": "#9ccc65",   # 草绿：OCR 识别类，和纯图色的蓝/青都区分开
    "multi_scan": "#00acc1",    # 青：批量遍历族（点模板蓝的复数形态）
    "stop_flow": "#e91e63",     # 玫红：终止语义，一眼看出「到这流程就没了」
    "signal": "#ef5350",        # 大红：发信号=报警旗，跟结束流程的玫红、判断的橙都分得开
    "watch": "#f57f17",         # 深琥珀：常驻哨兵，跟子流程的靛拉开（够暗才衬白字）
    "timer": "#6d4c41",         # 咖啡：看钟的哨兵，跟认图的琥珀同为哨兵族但一眼分得开
}


# 行清单在画布上的行高（循环体、监视行共用）；区域本体高度 = 头部 NODE_H + 行高×行数。
POPUP_ROW_H = 22

# 并行监视容器：每组顶部一条「组标题行」（① N条线 · 命中口对齐在这行右侧）。
GROUP_H = 18
# 组编号徽章：画布标题行、命中口标签、属性面板页签共用同一串（所见=所得）。
GROUP_BADGE = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _group_badge(i: int) -> str:
    return GROUP_BADGE[i] if i < len(GROUP_BADGE) else f"[{i + 1}]"


def container_groups(n: dict) -> list | None:
    """watch 容器形态判定：groups 是 list → 返回它；普通哨兵 → None。
    画布/面板/合并拆分全部走这一个入口认"是不是容器"，别各处自己 isinstance。"""
    g = n.get("groups")
    return g if isinstance(g, list) else None


def watch_group_rows(g: dict) -> list:
    return [r for r in (g.get("rows") or []) if isinstance(r, dict)]


def normalize_watch_containers(nodes: list) -> None:
    """容器就地整形（_rebuild_scene 必经点调用）：每组补 id、rows 补 list。
    缺 id 的组在编译展开时会撞车重编号 —— 编辑期先堵死。"""
    for n in nodes:
        g = container_groups(n) if n.get("type") == "watch" else None
        if g is None:
            continue
        for one in g:
            if isinstance(one, dict):
                one.setdefault("rows", [])
                if not one.get("id"):
                    one["id"] = _graph_id()


def make_watch_group(node: dict) -> dict:
    """普通 watch 节点 → 首组数据（组 id = 节点 id，节点备注名跟着进组名）。"""
    return {"id": node.get("id") or _graph_id(),
            "label": node.get("label") or "",
            "rows": copy.deepcopy(node.get("rows") or []),
            "cooldown_secs": node.get("cooldown_secs", 5),
            "fail_limit": node.get("fail_limit", 3),
            "scan_mode": node.get("scan_mode") or "restart"}


def group_body_h(g: dict) -> float:
    """一组占的画布高度：标题行 + 行×行高。"""
    return GROUP_H + POPUP_ROW_H * max(0, len(watch_group_rows(g)))


def new_watch_node_from_group(g: dict, x: int, y: int,
                              fallback_label: str = "") -> dict:
    """组 → 独立并行监视（拆出用；用户拍板 2026-10-05：拆出来照样是一台容器，
    不摊平——两边长得一样，随时能再并回去）。节点 id 沿用组 id：命中统计不掐断。
    节点名 = 组名（合并时冠的），没组名用来源容器名兜底。"""
    return {"id": g.get("id") or _graph_id(), "type": "watch",
            "label": (g.get("label") or "") or (fallback_label or ""),
            "x": int(x), "y": int(y),
            "groups": [g]}


def collapse_watch_container(n: dict) -> None:
    """空壳容器就地转形态（调用方负责随后重建画布）。
    用户拍板 2026-10-05：只剩 1 组也不摊平——容器就该一直是容器，
    只有删/拆到 0 组剩空壳才退回空普通哨兵。"""
    if container_groups(n):
        return
    n.pop("groups", None)
    n.setdefault("rows", [])


def _node_h(n: dict) -> float:
    """节点本体高度（不含端口余量）。循环/并行监视按行数自动长高。"""
    if n.get("type") == "loop":
        return NODE_H + POPUP_ROW_H * max(0, len(n.get("steps") or []))
    if n.get("type") in ("watch", "timer"):
        gs = container_groups(n) if n.get("type") == "watch" else None
        if gs is not None:
            # 容器：头部一行 + 每组（标题行+行）。组全空也各占一条标题行。
            return NODE_H + sum(group_body_h(g) if isinstance(g, dict) else 0
                                for g in gs)
        return NODE_H + POPUP_ROW_H * max(0, len(n.get("rows") or []))
    return NODE_H


# 对齐/分布工具按钮图标统一走 ui/icons.py layout_button（内联 SVG）：
# 全 Segoe MDL2 PUA 区段逐码位扫描确认过，MDL2 只有横向对齐字形，
# 垂直对齐/等距分布没有 —— 同一排按钮不混两种图标来源。


class FlowScene(QGraphicsScene):
    """流程画布场景：自绘点阵网格背景（照 Qt Designer 画布的点格）。

    show_grid/snap_grid 两个开关由面板复选框驱动（状态记 ui.flow_grid /
    ui.flow_snap）；吸附本身在 FlowNodeItem._snap_pos 里做，这里只管画。
    网格是"底"：drawBackground 天然在节点与参考线之下，不用动前景。"""

    GRID = 20   # 格距（场景坐标）；NODE_W=200 正好 10 格，横排必然对齐

    def __init__(self, show_grid: bool = True, snap_grid: bool = True):
        super().__init__()
        self.show_grid = show_grid
        self.snap_grid = snap_grid

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        if not self.show_grid:
            return
        sc = max(0.01, painter.worldTransform().m11())
        g = self.GRID
        while g * sc < 7:      # 缩太小点阵糊成灰片 → 加倍间距（跟 Designer 同手感）
            g *= 2
        pen = QPen(QColor(0, 0, 0, 55))
        pen.setCosmetic(True)  # 恒 1px 圆点，不随缩放变粗
        painter.setPen(pen)
        pts = []
        x = int(rect.left()) // g * g
        y0 = int(rect.top()) // g * g
        while x <= rect.right():
            y = y0
            while y <= rect.bottom():
                pts.append(QPointF(x, y))
                y += g
            x += g
        if pts:
            painter.drawPoints(pts)


def new_node(t: str) -> dict:
    """按类型生成一个带默认参数的新节点。v2 图模型：节点自带 id 与画布坐标。"""
    nid = uuid.uuid4().hex[:8]
    base = {"id": nid, "x": 0, "y": 0}
    if t == "click_template":
        # 新节点默认开随机落点抖动 [0,5]px（老节点没这键 = 固定，互不干扰）
        return {**base, "type": t, "label": "", "category": "", "name": "",
                "wait_before": True, "wait_timeout": 8, "on_timeout": "fail",
                "jitter_px_min": 0, "jitter_px_max": 5}
    if t == "wait_template":
        return {**base, "type": t, "label": "", "category": "", "name": "",
                "timeout": 8, "exists": True, "on_timeout": "fail"}
    if t == "click":
        # F3：坐标可来自流程变量/表达式。coord = 一个求值成 (x,y) 元组或
        # "x,y" 字符串的表达式；x_expr/y_expr = 分轴表达式（优先于 coord）。
        # 三个新键默认空 = 用上面的写死 tap_x/tap_y（存量节点没这些键，零污染）。
        return {**base, "type": t, "label": "", "tap_x": 270, "tap_y": 480,
                "jitter_px_min": 0, "jitter_px_max": 5,
                "coord": "", "x_expr": "", "y_expr": ""}
    if t == "swipe":
        return {**base, "type": t, "label": "", "x1": 270, "y1": 700,
                "x2": 270, "y2": 300, "ms": 500}
    if t == "sleep":
        return {**base, "type": t, "label": "", "seconds": 1}
    if t == "debug_print":
        return {**base, "type": t, "label": "", "text": "", "level": "INFO",
                "target": "none", "category": "", "name": "", "tap_x": 0, "tap_y": 0}
    if t == "save_assets":
        return {**base, "type": t, "label": "",
                "items": [{"var": "", "name": ""}]}
    if t == "compute":
        # 计算赋值（F1）：expr = 算术表达式（变量名直写或 {变量名}/{账号.列}），
        # var_name = 结果写进的流程变量名。两条都默认空，编译侧空名跳过。
        return {**base, "type": t, "label": "", "expr": "", "var_name": ""}
    if t == "branch":
        # 判断超时默认 1 秒（2026-09-28 用户拍板，原为 5）：判断多是「看一眼有/没有」，
        # 5 秒空等太久。只改这里的建节点默认——存量判断节点建时已把 timeout 写进
        # config（自带 5），不受影响；编译侧 .get("timeout", 5) 兜底也保留，
        # 极老无 timeout 键的节点仍按 5 走，符合「之前的判断模块不用改」。
        return {**base, "type": t, "label": "判断",
                "cond": {"kind": "template", "category": "", "name": ""},
                "timeout": 1}
    if t == "subflow":
        # 升级版点模板：默认带进入前置门（等图再进，语义同 click_template），
        # 出口固定两个：true=子流程跑完、false=子流程失败/门超时判失败
        # params（F2 调用绑定）：每项 {"name": 变量名, "expr": 实参表达式}，
        # 进入前逐个求值写流程变量；空表 = 无操作（老节点缺键零污染）
        return {**base, "type": t, "label": "", "flow_name": "",
                "wait_before": True, "category": "", "name": "",
                "wait_timeout": 8, "on_timeout": "fail", "params": []}
    if t == "loop":
        # 循环容器：一进一出。
        # steps 每行 = 一个动作行（点/等/清弹窗），顺序执行成一轮；
        # cond = 退出条件（template/color/multi_color/text），每轮跑完查一次，
        # 满足即出循环往下走。max_times=0 = 不限轮数（只认退出条件，直到
        # 满足为止；配了上限则到轮数必出防卡死）。
        return {**base, "type": t, "label": "循环", "steps": [],
                "cond": {"kind": "template", "category": "", "name": ""},
                "max_times": 0}
    if t == "server_pick":
        # 识字取物：text 空 = 运行时读账号表的「登录区服」。roi=[x0,y0,x1,y1]
        # 或 null（全屏）。min_sim 存 0~100 整数（编译侧除 100），跟界面直觉一致。
        # mode/偏移/抖动/变量名/滑动参数都不进默认：编译侧 .get 有兜底，
        # 缺键 = 老「点击」语义，存量节点与新节点默认行为完全一致。
        return {**base, "type": t, "label": "", "text": "", "roi": None,
                "timeout": 15, "on_timeout": "exit", "min_sim": 60,
                "min_interval": 0.3}
    if t == "multi_scan":
        # 遍历全部：一帧把所有命中收成快照，按模式逐处处置。
        # mode=tap 逐个点掉；mode=enter 进一个回一个（点→跑子流程→返回键→
        # 可选等列表标志图确认）；mode=read 逐个读数存变量。
        # source=text 时改用 OCR 圈字（category/name 变残值）；dedupe=边扫边办
        # 按身份去重（动态列表用）。flow_name/confirm_tpl 只在 enter 有意义，
        # read_* 只在 read 有意义，编译侧 .get 全有兜底。
        return {**base, "type": t, "label": "", "category": "", "name": "",
                "roi": None, "mode": "tap", "offset_x": 0, "offset_y": 0,
                "jitter_px_min": 0, "jitter_px_max": 5,
                "max_hits": 20, "gap_secs": 0.5,
                "wait_before": True, "wait_timeout": 8, "on_timeout": "fail",
                "var_name": "遍历命中数",
                "flow_name": "", "confirm_tpl": {"category": "", "name": ""},
                "confirm_timeout": 5, "settle_secs": 0.8, "press_back": False,
                "source": "template", "text": "", "min_sim": 60,
                "dedupe": False, "max_rounds": 50,
                "read_dx": 0, "read_dy": 0, "read_w": 160, "read_h": 48}
    if t == "stop_flow":
        # 结束流程：只有入口没有出口（出去了就没了）。stop_scope = 结束范围
        # （local=当前流程/所在层，global=整条流程），存英文值防断配
        return {**base, "type": t, "label": "结束流程", "stop_scope": "local"}
    if t == "signal":
        # 发信号：往共享信箱投一个信号名就直通（永不失败）。外层「循环 / 遍历
        # 全部」在属性里登记同名「收到即结束的信号」名单才会掐，一次销账。
        # name 是信号名（非模板名），编译侧只喂 SignalStep，不碰模板账本。
        return {**base, "type": t, "label": "", "name": ""}
    if t == "watch":
        # 并行监视哨兵：零端口 —— 命中自动暂停主线办完续跑（一刀切），不接线。
        # rows 每行 = {"category","name","flow_name"}
        # 弹窗模板→子流程，扫描顺序=优先级。cooldown_secs = 一条子流程跑完
        # 后这行歇多久再监视（防连点劫持循环）；fail_limit = 连续几次没搞定
        # 就停用该行（防顽固弹窗饿死主线）。
        # scan_mode：restart=命中办完后重头扫（缺省老行为）/ continue=从命中
        # 行下一行接着扫。存储值英文，界面显示中文（同 kind 纪律）
        return {**base, "type": t, "label": "并行监视", "rows": [],
                "cooldown_secs": 5, "fail_limit": 3, "scan_mode": "restart"}
    if t == "timer":
        # 定时执行哨兵：参数结构与 watch 同款（rows、零端口），差别
        # 全在行的语义：{"interval_min","flow_name"} = 每隔 N 分钟跑一次子流程。
        # cooldown_secs 语义变 = 子流程失败后歇多久重试（成功歇整周期，见 TimerStep）。
        return {**base, "type": t, "label": "定时执行", "rows": [],
                "cooldown_secs": 5, "fail_limit": 3}
    return {**base, "type": t, "label": ""}


# ── 递归布局：把节点树拍平成 (节点, x, y) + 连线 ──────────────────────

class FlowNodeItem(QGraphicsItem):
    """画布上一个流程节点。持有节点 dict 引用。
    v2 图：可拖拽，拖完把 pos() 写回 node["x"]/["y"]。左右缘画出连接点。"""

    def __init__(self, node: dict, panel):
        super().__init__()
        self.node = node
        self.panel = panel
        self._test_mark = None   # 试运行结果角标:"ok"/"failed"/"pending"，None=无
        self._dragging = False   # 本节点是否处于拖动中（用来只在真拖过时标脏）
        self._hot_port = None    # 拉线磁吸命中的端口 id（view 设置，paint 画亮环）
        self._group_pull = None  # 容器组标题行抓取态 (gid, 按下场景点, moved)
        # 拖拽 + 选中；不设 ItemIsMovable 之下的缩放/旋转
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        # 悬停事件：组标题行是"抓得动"的把手，光标要提前说清楚
        self.setAcceptHoverEvents(True)

    def boundingRect(self):
        # 必须罩住 paint 的全部内容：端口圆画在方块左右缘外 4px、选中框 3px 宽
        # 也压边。Qt 只按 boundingRect 圈"擦除区"——超出部分拖动后旧像素不重绘，
        # 在空白处留下端口点残影；itemAt 命中测试同样按它算，出点恰在旧右缘
        # （不含边界）上永远点不中，连不上线。两边各留 8px 余量。
        # 高度用 _node_h：循环/监视随行数长高，其余类型 = NODE_H（原样）。
        return QRectF(-PORT_PAD, -PORT_PAD,
                      NODE_W + 2 * PORT_PAD, _node_h(self.node) + 2 * PORT_PAD)

    # 端口（连接点）几何：左缘入点，右缘出点；branch 有两个出点（true/false）
    def _ports(self):
        """
        返回 [(port_id, x, y, label), ...]。
        port_id: "in" 或出点 "out" / "true" / "false" / "fail"；
        watch/timer 零端口（一刀切暂停后不接线）。
        """
        t = self.node.get("type", "")
        h = _node_h(self.node)   # 循环/监视按行数长高，端口跟着走中线/上下口
        if t == "branch":
            return [
                ("in", 0, h // 2, ""),
                ("true", NODE_W, h // 4, "Y"),
                ("false", NODE_W, h * 3 // 4, "N"),
            ]
        if t == "subflow":
            # 双出口几何同 branch：上口=成功（子流程跑完），下口=失败
            # （子流程内部失败或进门前等图超时）。端口 id 沿用 true/false，
            # GraphStep 的传播逻辑一份两用（见 bot/fsm.py 的 skipped 分支）。
            return [
                ("in", 0, h // 2, ""),
                ("true", NODE_W, h // 4, "Y"),
                ("false", NODE_W, h * 3 // 4, "N"),
            ]
        if t == "stop_flow":
            # 只有入口没有出口：命中即整图终止，拉出线去的下游永远不会跑，
            # 不提供出端口免得用户搭出"死线"还一脸困惑。
            return [("in", 0, h // 2, "")]
        if t in ("watch", "timer"):
            # 并行监视/定时执行哨兵：零端口（2026-09-25 一刀切拍板）—— 开局
            # 即在岗、跟主线并行，入线没有意义也绝不允许（运行中永远 running，
            # 接它的下游死等整图挂死）；命中/到点自动暂停主线、办完续跑，
            # 旧「命中/到点→信号」出口连同拉线需求一起下线。
            return []
        if t in ("click_template", "wait_template", "server_pick", "multi_scan"):
            # 会失败的节点多一个红色「N」出口（下半位，与成功链的中线 out 错开）：
            # 从它拉线 = "这步失败另走一条线"。类型清单与 flow.FAILABLE_TYPES
            # 同步维护（编辑器惯例不 import 私有名，这里用字面量）。
            return [
                ("in", 0, h // 2, ""),
                ("out", NODE_W, h // 2, ""),
                ("fail", NODE_W, h * 3 // 4, "N"),
            ]
        if t == "loop":
            # 循环容器：一个入口 + 一个「达成」出口（循环跑完/退出条件满足才走
            # 下一条）。旧「信号」入点已随一刀切暂停下线（不用接线）。
            return [
                ("in", 0, h // 2, ""),
                ("out", NODE_W, h // 2, ""),
            ]
        return [
            ("in", 0, h // 2, ""),
            ("out", NODE_W, h // 2, ""),
        ]

    def port_at(self, pos):
        """画布坐标 → 命中的端口 (port_id, global_pos) 或 None。"""
        for pid, x, y, _ in self._ports():
            if abs(pos.x() - x) <= 6 and abs(pos.y() - y) <= 6:
                return pid, self.mapToScene(x, y)
        return None

    def _thumb(self):
        """本节点所选模板的图标。取图入口按类型分流：点/等模板直接读节点；
        打印节点只有 target=template 时 name 才是模板；判断节点读条件清单
        （branch_cond_items，多条件时取第一条模板条件）。"""
        n = self.node
        t = n.get("type", "")
        if t in ("click_template", "wait_template", "multi_scan"):
            src = n
        elif t == "debug_print" and n.get("target") == "template":
            src = n
        elif t == "branch":
            from bot import template_deps
            src = next((cd for cd in template_deps.branch_cond_items(n)
                        if cd.get("kind") == "template"), None)
            if src is None:
                return None
        else:
            return None
        return _template_thumb(src.get("category", ""), src.get("name", ""))

    def _paint_loop(self, painter: QPainter):
        """循环容器专用绘制（paint 已画好自适应高度的底色）：
        头部 = 类型小字 + 备注/出口条件摘要；下方逐行铺循环体动作行。
        行数多区域就长（_node_h）。"""
        n = self.node
        steps = [s for s in (n.get("steps") or []) if isinstance(s, dict)]
        # 出口条件摘要（一眼看懂"循环到什么为止"）
        cond = n.get("cond") or {}
        cond_txt = ""
        if cond.get("kind") == "template":
            cond_txt = f"直到出现「{cond.get('name') or '?'}」"
        elif cond.get("kind") == "color":
            cond_txt = "直到取色"
        elif cond.get("kind") == "multi_color":
            cond_txt = "直到多点取色"
        elif cond.get("kind") == "text":
            cond_txt = f"直到认出「{cond.get('target') or '?'}」"
        elif cond.get("kind") == "var":
            cond_txt = (f"直到 {cond.get('name') or '?'}"
                        f"{cond.get('op', '>=')}{cond.get('value', '')}")
        mt = int(n.get("max_times", 0) or 0)
        if mt:
            cond_txt += f" · ≤{mt}轮"
        label = n.get("label") or (
            "空循环：右侧添加动作行" if not steps else f"循环体·{len(steps)} 行")
        f_small = QFont(); f_small.setPointSize(8)
        painter.setFont(f_small)
        painter.setPen(Qt.white)
        painter.drawText(6, 4, NODE_W - 12, 16, Qt.AlignLeft, TYPE_LABEL["loop"])
        f_big = QFont(); f_big.setPointSize(10); f_big.setBold(True)
        painter.setFont(f_big)
        painter.drawText(6, 20, NODE_W - 12, 18, Qt.AlignCenter,
                         label[:14] + ("…" if len(label) > 14 else ""))
        painter.setFont(f_small)
        painter.setPen(QColor("#ffe082"))
        painter.drawText(6, 38, NODE_W - 12, 14, Qt.AlignLeft, cond_txt or "（未设退出=无限轮）")
        # 循环体动作行（行长条风格）
        painter.setPen(Qt.white)
        f_row = QFont(); f_row.setPointSize(8)
        painter.setFont(f_row)
        for i, s in enumerate(steps):
            y = NODE_H + i * POPUP_ROW_H
            if i % 2 == 0:   # 斑马纹
                painter.fillRect(QRectF(2, y, NODE_W - 4, POPUP_ROW_H),
                                 QColor(255, 255, 255, 40))
            st = s.get("type", "")
            if st == "click":
                # 有坐标变量/表达式（F3）时画布行预览显示来源，否则写死坐标
                if str(s.get("coord") or "").strip():
                    name = f"坐标={s.get('coord')}"
                elif str(s.get("x_expr") or "").strip() \
                        or str(s.get("y_expr") or "").strip():
                    name = f"({s.get('x_expr', '?')},{s.get('y_expr', '?')})"
                else:
                    name = f"({s.get('tap_x', 270)},{s.get('tap_y', 480)})"
            else:
                name = s.get("name")
            # 行动作标记（文案单一来源 _STEP_KIND_CN + 缩写列 _STEP_KIND_ABBR）
            mark = _STEP_KIND_ABBR.get(st, _STEP_KIND_CN.get(st, st or "?"))
            # 行预览缩略图（模板类取第一个引用图）
            src = None
            if st in ("click_template", "wait_template"):
                src = s
            elif st == "popup_zone":
                for m in (s.get("modules") or []):
                    if isinstance(m, dict) and m.get("name"):
                        src = m; break
            tx = 6
            if src is not None and src.get("name"):
                pix = _template_thumb(src.get("category", ""), src.get("name", ""))
                if pix is not None:
                    painter.drawPixmap(QRect(int(tx), y + 3, 16, 16), pix)
                    tx += 20
            painter.drawText(tx, y, NODE_W - tx - 44, POPUP_ROW_H,
                             Qt.AlignVCenter | Qt.AlignLeft,
                             str(s.get("label") or name or "（空动作）"))
            painter.setPen(QColor("#ffe082"))
            painter.drawText(NODE_W - 44, y, 38, POPUP_ROW_H,
                             Qt.AlignVCenter | Qt.AlignRight, mark)
            painter.setPen(Qt.white)

    def _paint_watch_rows(self, painter: QPainter, rows: list, y0: float):
        """铺一组监视行（普通哨兵与容器内各组共用）：每行 = 模板缩略图 +
        模板名→子流程名/点名关/封号停。斑马纹按"本组内行序"走。"""
        f_row = QFont(); f_row.setPointSize(8)
        painter.setFont(f_row)
        for i, r in enumerate(rows):
            y = y0 + i * POPUP_ROW_H
            if i % 2 == 0:   # 斑马纹（同循环体）
                painter.fillRect(QRectF(2, y, NODE_W - 4, POPUP_ROW_H),
                                 QColor(255, 255, 255, 40))
            tx = 6
            pix = _template_thumb(r.get("category", ""), r.get("name", ""))
            if pix is not None:
                painter.drawPixmap(QRect(int(tx), y + 3, 16, 16), pix)
                tx += 20
            painter.setPen(Qt.white)
            tpl_txt = r.get("name") or "（没选图）"
            # 会点图标的行（tap 型 / 勾了即点）采了偏移时，画布上直接带尾标，
            # 不开属性面板也看得出"这点不是图中心"
            dx = int(r.get("offset_x", 0) or 0)
            dy = int(r.get("offset_y", 0) or 0)
            off = f"({dx:+d},{dy:+d})" if (dx or dy) else ""
            # 四种行型一眼分开：tap=「👆点名关」（纯点图标）、ban=「⛔封号停」
            # （命中即收工标记封号）、finger=「🖐动态手指」（现扫手指点它指的位置）、
            # subflow=「图→流程」（前缀 👆 = 勾了即点，起跑前先点入口图标）
            if r.get("kind") == "finger":
                txt = "🖐动态手指 → 点它指的位置"
            elif r.get("kind") == "tap":
                txt = f"👆{tpl_txt}点名关{off}"
            elif r.get("kind") == "ban":
                txt = f"⛔{tpl_txt}封号停"
            else:
                flow_txt = r.get("flow_name") or "（没选流程）"
                txt = f"{'👆' if r.get('tap_on_hit') else ''}{tpl_txt}→{flow_txt}{off}"
            painter.drawText(tx, y, NODE_W - tx - 6, POPUP_ROW_H,
                             Qt.AlignVCenter | Qt.AlignLeft, txt)

    def _paint_watch(self, painter: QPainter):
        """并行监视哨兵专用绘制（paint 已画好自适应高度底色）：
        头部 = 类型小字 + 备注/行数控件；下方逐行铺「弹窗模板→子流程」映射行。
        容器形态（groups）：每组先画一条「① N 条线」标题行，再铺本组行
        —— 编号与面板页签逐字一致。哨兵零端口（一刀切暂停，不用接线）。"""
        n = self.node
        gs = container_groups(n)
        rows = [r for r in (n.get("rows") or []) if isinstance(r, dict)]
        if gs is None:
            label = n.get("label") or (
                "空监视：右侧添加监视行" if not rows
                else f"并行监视·{len(rows)} 条线")
        else:
            total = sum(len(watch_group_rows(g))
                        for g in gs if isinstance(g, dict))
            label = n.get("label") or (
                "空容器：拖入其它监视或点＋新建组" if not total
                else f"并行监视 ×{len(gs)}")
        f_small = QFont(); f_small.setPointSize(8)
        painter.setFont(f_small)
        painter.setPen(Qt.white)
        painter.drawText(6, 4, NODE_W - 12, 16, Qt.AlignLeft, TYPE_LABEL["watch"])
        f_big = QFont(); f_big.setPointSize(10); f_big.setBold(True)
        painter.setFont(f_big)
        painter.drawText(6, 20, NODE_W - 12, NODE_H - 24, Qt.AlignCenter,
                         label[:14] + ("…" if len(label) > 14 else ""))
        if gs is None:
            self._paint_watch_rows(painter, rows, NODE_H)
            return
        y = NODE_H
        for idx, g in enumerate(gs):
            if not isinstance(g, dict):
                continue
            grows = watch_group_rows(g)
            painter.fillRect(QRectF(2, y, NODE_W - 4, GROUP_H),
                             QColor(0, 0, 0, 55))   # 标题行压深一档，分组边界一眼分
            painter.setPen(QColor("#ffe082"))
            # 组名（可空）：合并/拆出后靠它认出处；超长手动 elide（画布无 elide 基建）
            gl = str(g.get("label") or "").strip()
            if len(gl) > 8:
                gl = gl[:8] + "…"
            painter.drawText(8, y, NODE_W - 16, GROUP_H,
                             Qt.AlignVCenter | Qt.AlignLeft,
                             f"{_group_badge(idx)} {gl} · {len(grows)} 条线"
                             if gl else f"{_group_badge(idx)} {len(grows)} 条线")
            self._paint_watch_rows(painter, grows, y + GROUP_H)
            y += group_body_h(g)

    def _paint_timer(self, painter: QPainter):
        """定时执行哨兵绘制：版式同 _paint_watch，行内容换成「⏱ 间隔→子流程」
        （不认图所以没有缩略图列）。"""
        n = self.node
        rows = [r for r in (n.get("rows") or []) if isinstance(r, dict)]
        label = n.get("label") or (
            "未配执行行：添加「间隔→子流程」行" if not rows
            else f"定时执行·{len(rows)} 条线")
        f_small = QFont(); f_small.setPointSize(8)
        painter.setFont(f_small)
        painter.setPen(Qt.white)
        painter.drawText(6, 4, NODE_W - 12, 16, Qt.AlignLeft, TYPE_LABEL["timer"])
        f_big = QFont(); f_big.setPointSize(10); f_big.setBold(True)
        painter.setFont(f_big)
        painter.drawText(6, 20, NODE_W - 12, NODE_H - 24, Qt.AlignCenter,
                         label[:14] + ("…" if len(label) > 14 else ""))
        f_row = QFont(); f_row.setPointSize(8)
        painter.setFont(f_row)
        for i, r in enumerate(rows):
            y = NODE_H + i * POPUP_ROW_H
            if i % 2 == 0:
                painter.fillRect(QRectF(2, y, NODE_W - 4, POPUP_ROW_H),
                                 QColor(255, 255, 255, 40))
            painter.setPen(Qt.white)
            try:
                iv = f"{float(r.get('interval_min', 5)):g}"
            except (TypeError, ValueError):
                iv = "?"
            flow_txt = r.get("flow_name") or "（没选流程）"
            painter.drawText(6, y, NODE_W - 12, POPUP_ROW_H,
                             Qt.AlignVCenter | Qt.AlignLeft,
                             f"每{iv}分→{flow_txt}")

    def paint(self, painter: QPainter, option, widget=None):
        t = self.node.get("type", "")
        color = QColor(TYPE_COLOR.get(t, "#888"))
        # 节点本体高度 = _node_h（循环/监视随行数长高，其余固定 NODE_H）；
        # boundingRect 比它大一圈（端口/选中框余量），用 boundingRect()
        # 画底会把方块跟着撑大
        h = _node_h(self.node)
        rect = QRectF(0, 0, NODE_W, h)

        # 圆角矩形底
        painter.setBrush(QBrush(color))
        painter.setPen(QPen(color.darker(120), 2))
        painter.drawRoundedRect(rect, 8, 8)

        # 选中高亮黄边框
        if self.isSelected():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#ffd600"), 3))
            painter.drawRoundedRect(rect, 8, 8)

        # 试运行结果角标：右上角盖 14px 圆徽，含状态字母。
        # 来自 panel._test_cache（A 联动：测试跑完回填），只读不驱动。
        if self._test_mark:
            color = {"ok": "#2e7d32", "failed": "#c62828", "pending": "#bdbdbd"}.get(
                self._test_mark, "#888")
            painter.setBrush(QBrush(QColor(color)))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QRectF(NODE_W - 16, -2, 14, 14))
            painter.setPen(Qt.white)
            f_mark = QFont()
            f_mark.setPointSize(8)
            f_mark.setBold(True)
            painter.setFont(f_mark)
            mark_char = "✓" if self._test_mark == "ok" else \
                ("✗" if self._test_mark == "failed" else "…")
            painter.drawText(QRectF(NODE_W - 16, -2, 14, 14),
                             Qt.AlignCenter, mark_char)

        if t == "loop":
            self._paint_loop(painter)
        elif t == "watch":
            self._paint_watch(painter)
        elif t == "timer":
            self._paint_timer(painter)
        else:
            # 类型小字（顶部）+ label 大字；有模板图时 label 文字让位给右侧缩略图
            # subflow 没缩略图可看，没填备注时至少显示引了哪条流程
            label = self.node.get("label") or (
                f"子流程·{self.node.get('flow_name') or '?'}"
                if t == "subflow" else
                f"发信号·{self.node.get('name') or '?'}"
                if t == "signal" else TYPE_LABEL.get(t, t))
            painter.setPen(Qt.white)
            f_small = QFont()
            f_small.setPointSize(8)
            painter.setFont(f_small)
            top_txt = TYPE_LABEL.get(t, t)
            if t == "click_template" and self.node.get("all_hits"):
                # 点全开着的节点带尾标：不开属性面板也认得出这不是普通点模板
                top_txt += "·点全"
            if t == "multi_scan":
                # 尾标把三副面孔/两个源/去重在画布上全摊开：不开属性面板也认得出
                if self.node.get("source") == "text":
                    top_txt += "·文字"
                _ms_mode = self.node.get("mode", "tap")
                top_txt += {"enter": "·进一个", "read": "·读数"}.get(_ms_mode, "")
                if bool(self.node.get("dedupe", False)) and _ms_mode != "read":
                    top_txt += "·去重"
                if str(self.node.get("filter_op", "") or "") and _ms_mode != "read":
                    top_txt += "·过滤"
            if t in ("branch", "loop"):
                # 多条件角标：画布上一眼看出这条判断/循环出口不是单条件
                # （属性面板是真相源）
                _ec = self.node.get("extra_conds") or []
                if _ec:
                    top_txt += f"·{1 + len(_ec)}条" \
                               f"{'任一' if self.node.get('logic') == 'any' else '全部'}"
            if t in ("loop", "multi_scan") and self.node.get("stop_on"):
                # 停止信号登记尾标：一眼看出这台容器有「收到即结束」的闸
                # （名单清洗口径走编译侧同款 _stop_on_names，显示=生效）
                from bot.tasks.flow import _stop_on_names as _son
                _n = len(_son(self.node))
                if _n:
                    top_txt += f"·停{_n}"
            if t == "subflow":
                # 传参尾标：一眼看出这个子流程调用带了几个形参（不摊内容，面板是真相源）
                from bot.tasks.flow import _subflow_params as _sfp
                _pn = len(_sfp(self.node))
                if _pn:
                    top_txt += f"·传参{_pn}"
            painter.drawText(6, 4, NODE_W - 12, 18, Qt.AlignLeft, top_txt)
            f_big = QFont()
            f_big.setPointSize(10)
            f_big.setBold(True)
            painter.setFont(f_big)
            label_w = NODE_W - 12
            thumb = self._thumb()
            if thumb is not None:
                tw = THUMB_W
                th = min(NODE_H - 24, int(tw * thumb.height() / thumb.width()))
                tx, ty = NODE_W - 6 - tw, 20 + (NODE_H - 24 - th) // 2
                # 图框描边固定浅灰：上面几处 setPen 留下的颜色不该染到缩略图
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor("#e8e8e8"), 1))
                painter.drawRoundedRect(tx - 2, ty - 2, tw + 4, th + 4, 3, 3)
                painter.drawPixmap(QRectF(tx, ty, tw, th), thumb,
                                   QRectF(0, 0, thumb.width(), thumb.height()))
                label_w = NODE_W - 12 - (tw + 6)
            painter.drawText(6, 20, label_w, NODE_H - 24, Qt.AlignCenter,
                             label[:12] + ("…" if len(label) > 12 else ""))

        # 连接点：入/出点小圆。branch 的 Y/N 带文字。
        # _hot_port：拉线磁吸命中态（view 在 mouseMove 里设）——画一圈亮橙
        # 放大环，松点前就看清"这条线会接到这个口"（n8n 同款反馈）。
        # 亮环画在 boundingRect 内（外扩 2px 仍在 PORT_PAD=8 余量里），不留残影。
        for pid, x, y, p_label in self._ports():
            hot = pid == self._hot_port
            if hot:
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor("#ffb300"), 3))
                painter.drawEllipse(x - 7, y - 7, 14, 14)
            painter.setBrush(QColor("#ffb300") if hot else Qt.white)
            painter.setPen(QPen(QColor("#333"), 1))
            painter.drawEllipse(x - 4, y - 4, 8, 8)
            if p_label:
                painter.setPen(QColor("#333"))
                pf = QFont(); pf.setPointSize(7)
                painter.setFont(pf)
                # 文本框按字数给宽（「命中」两字 7pt ≈19px，16px 框会裁字）
                lw = 28 if len(p_label) > 1 else 16
                # 上口（true，靠顶）标签下移避让，下口（false/fail，靠底）上移
                painter.drawText(4 if x == 0 else NODE_W - 4 - lw,
                                 y + 4 if pid == "true" else y - 2, lw, 10,
                                 Qt.AlignCenter, p_label)

    # 拖拽写回：PySide 的 setPos/场景内移动不回调 Python 的 itemChange
    # （ItemPositionChange 走 C++ 虚拟但不进 Python override），所以在
    # mouseMoveEvent 里实时把 pos 写回节点 dict 并标脏（release 时 Qt 已把
    # pos 更新好，顺带再同步一次拿最终坐标）。
    # 顺手加了主流画布的对齐吸附：左右缘/中线与其它节点对齐时自动吸住，
    # 并画出红色参考线（Figma/draw.io 的 smart guides 手感）。
    SNAP_TOL = 10.0

    def _snap_pos(self):
        """拖动中吸附对齐：返回参考线段列表 [(x1,y1,x2,y2),...]（已顺手写回 pos）。"""
        items = [i for i in self.scene().items()
                 if isinstance(i, FlowNodeItem) and i is not self]
        w, h = NODE_W, _node_h(self.node)
        ox, oy = self.pos().x(), self.pos().y()
        # 吸附判定用屏幕像素手感：缩小时场景坐标跟着放大容差
        tol = self.SNAP_TOL
        try:
            tol /= max(0.25, self.panel.view._scale_factor())
        except Exception:
            pass

        def near(vals, v):
            hits = [c for c in vals if abs(c - v) <= tol]
            return min(hits, key=lambda c: abs(c - v)) if hits else None

        self_xs = [ox, ox + w / 2, ox + w]
        self_ys = [oy, oy + h / 2, oy + h]
        other_xs = sorted({x for i in items for x in
                           (i.pos().x(), i.pos().x() + NODE_W / 2,
                            i.pos().x() + NODE_W)})
        other_ys = sorted({y for i in items for y in
                           (i.pos().y(), i.pos().y() + _node_h(i.node) / 2,
                            i.pos().y() + _node_h(i.node))})
        guides = []
        nx, ny = ox, oy
        kx = near(other_xs, self_xs[0])
        if kx is not None:
            nx = kx
        else:
            kx = near(other_xs, self_xs[1])
            if kx is not None:
                nx = kx - w / 2
            else:
                kx = near(other_xs, self_xs[2])
                if kx is not None:
                    nx = kx - w
        ky = near(other_ys, self_ys[0])
        if ky is not None:
            ny = ky
        else:
            ky = near(other_ys, self_ys[1])
            if ky is not None:
                ny = ky - h / 2
            else:
                ky = near(other_ys, self_ys[2])
                if ky is not None:
                    ny = ky - h
        # 网格吸附兜底：节点互吸没命中的那根轴，往最近格点吸（Designer 同款
        # 优先级：物件对齐线 > 网格）。格距 20，节点宽 200 是 10 格，吸左上角
        # 即四角全在格点上。
        scene = self.scene()
        if getattr(scene, "snap_grid", False):
            g = FlowScene.GRID
            if kx is None:
                nx = round(nx / g) * g
            if ky is None:
                ny = round(ny / g) * g
        if nx != ox or ny != oy:
            self.setPos(nx, ny)
        # 参考线：贯穿本节点与对齐目标（纵向线取公共 y 跨度并外扩）
        if kx is not None:
            ys = [oy, oy + h] + [i.pos().y() for i in items] + \
                 [i.pos().y() + _node_h(i.node) for i in items]
            guides.append((kx, min(ys) - 30, kx, max(ys) + 30))
        if ky is not None:
            xs = [nx, nx + w] + [i.pos().x() for i in items] + \
                 [i.pos().x() + NODE_W for i in items]
            guides.append((min(xs) - 30, ky, max(xs) + 30, ky))
        return guides

    def _group_move(self):
        """组拖同步：Qt 只回调被抓住的那个 item 的鼠标事件，但场景会带着全部
        选中节点一起动 —— 这里把每个选中节点的 pos 写回 dict，并刷新连到它们
        的任何一条边。漏了这步的话：两两相连但都不含"抓的那个"的线永远不重绘
        （线留在原地像断了），且同伴节点的坐标不落账，下次重建全弹回原位。"""
        nodes = [i for i in self.scene().selectedItems()
                 if isinstance(i, FlowNodeItem)]
        for it in nodes:
            it.node["x"] = int(it.pos().x())
            it.node["y"] = int(it.pos().y())
        for edge in self.scene().items():
            if isinstance(edge, FlowEdgeItem) and \
                    any(edge.is_downstream_of(it) for it in nodes):
                edge.update_positions()
        return nodes

    # ── 容器组标题行：按住拖出 = 拆回独立节点（_try_merge_watch 的逆操作）──
    def _group_head_at(self, local) -> str | None:
        """本地点命中哪一组的标题行（y 落在标题行带内即算，x 不限——整行都是把手）。"""
        gs = container_groups(self.node) if self.node.get("type") == "watch" else None
        if not gs:
            return None
        y = NODE_H
        for g in gs:
            if not isinstance(g, dict):
                continue
            if y <= local.y() < y + GROUP_H:
                return g.get("id")
            y += group_body_h(g)
        return None

    def _is_readonly(self):
        """只读闸（上锁流程 / 未挂面板 = 一律禁拖）：节点侧与视图侧共用这一句口径。
        ⚠ 场景事件（QGraphicsSceneMouseEvent）坐标 API 只许用旧名 pos()/scenePos()：
        PySide6 6.11 绑定没暴露新名 position()/scenePosition()，用新名一按鼠标就
        AttributeError，拖拽/抓组全废（实测踩坑，smoke_watch_container_panel 事件层守卫）。"""
        return self.panel is None or self.panel._is_locked()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._is_readonly():
            gid = self._group_head_at(event.pos())
            if gid:
                # 标题行 = 组的把手：不起拖整台容器，改抓"这一组"
                self._group_pull = [gid, event.scenePos(), False]
                self.setCursor(Qt.ClosedHandCursor)   # 攥住了（松手/重建即复位）
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._group_pull is not None:
            if (event.buttons() & Qt.LeftButton) and \
                    (event.scenePos() - self._group_pull[1]).manhattanLength() > 6:
                self._group_pull[2] = True   # 真拖出把手行了（阈值防手抖误拆）
            event.accept()
            return
        if (event.buttons() & Qt.LeftButton) and \
                self.flags() & QGraphicsItem.ItemIsMovable:
            # 拖动中：位置已由场景更新 → 吸附校正 → 写回节点 + 重排相连的边
            if not self._dragging:
                self._dragging = True
                if self.panel is not None:
                    self.panel._begin_drag_snap()   # 按下→首移之间才记旧坐标快照
            # 吸附只服务单节点摆位；组拖时吸附量只加在被抓的节点上会拖散队形，跳过
            lines = self._snap_pos() \
                if len([i for i in self.scene().selectedItems()
                        if isinstance(i, FlowNodeItem)]) <= 1 else []
            if self.panel is not None:
                self.panel._show_guides(lines)
            self._group_move()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._group_pull is not None:
            # 组把手松手：真拖过 → 落在本容器身上 = 组换序，落在别处 = 拆回
            # 独立节点；轻点标题行不选中也不动（无操作）
            gid, _, moved = self._group_pull
            self._group_pull = None
            self.unsetCursor()   # 松开就撒手；真拆/换序会重建销毁本 item，复位无害
            if moved and self.panel is not None:
                if not self.panel._try_reorder_group(self, gid, event.scenePos()):
                    self.panel._try_split_group(self, gid, event.scenePos())
            event.accept()
            return
        super().mouseReleaseEvent(event)
        # 松开拿最终 pos + 只在真拖过时标脏（点一下不标）；参考线随松手消失
        if self.panel is not None:
            self.panel._clear_guides()
        if self._dragging:
            # 拖拽合并：并行监视松在另一台监视/容器身上 → 收进对方成一组，
            # 画布随即重建（本 item 被销毁）。合并成功就别再写坐标/进"移动"撤销步。
            merged = (self.node.get("type") == "watch"
                      and self.panel._try_merge_watch(self))
            if not merged:
                self._group_move()   # 最终坐标落账（含一起被拖的同伴节点）
                if self.panel is not None:
                    self.panel._end_drag_snap(True)   # 真拖过：一步"移动"进撤销栈
                    self.panel._mark_dirty()
            self._dragging = False

    def hoverMoveEvent(self, event):
        # 组标题行 = 把手：光标提前说清"这里抓得动"（主流画布把手惯例：
        # OpenHand 张开的手 = 可抓，按下换 ClosedHand 攥住，同 Figma/n8n）。
        # 不上锁才给，锁了给了也是骗人。
        if self._group_pull is not None:
            # 拖拽途中指针会移出把手带、触发本控件 hover → 攥紧的手不能撒：
            # 只要 _group_pull 还在，光标保持 ClosedHand（松手才复位）。
            self.setCursor(Qt.ClosedHandCursor)
        elif (self._group_head_at(event.pos())
                and not self._is_readonly()):
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.unsetCursor()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        if self._group_pull is None:   # 同上：抓着组拖时指针离手，手不能松
            self.unsetCursor()
        super().hoverLeaveEvent(event)


def _bezier_path(p1: QPointF, p2: QPointF) -> QPainterPath:
    """连线 = 水平出入的三次贝塞尔（n8n/ComfyUI 同款手感）：两端切线恒水平，
    曲线从出点平滑滑出、水平注入入点，比直线少穿节点。控制点张力随水平距离
    伸缩、下限 30px：短距离也不塌成直线；回头线（目标在左）自然画出大回环。"""
    path = QPainterPath(p1)
    t = max(30.0, abs(p2.x() - p1.x()) * 0.5)
    path.cubicTo(QPointF(p1.x() + t, p1.y()),
                 QPointF(p2.x() - t, p2.y()), p2)
    return path


class _CurveRubber(QGraphicsItem):
    """拉线/改接途中的橡皮筋辅助线：与正式边同款曲线同款虚线动画，
    松手前看到的就是连上后的样子。"""

    def __init__(self, p1, p2):
        super().__init__()
        self._pen = QPen(QColor("#666"), 2, Qt.DashLine)
        self.setZValue(-2)
        self.setAcceptedMouseButtons(Qt.NoButton)
        self.set_points(p1, p2)

    def set_points(self, p1, p2):
        self.prepareGeometryChange()   # 曲线盒界变了必须喊，否则旧像素残影
        self._path = _bezier_path(p1, p2)
        self.update()

    def boundingRect(self):
        return self._path.boundingRect().adjusted(-8, -8, 8, 8)

    def paint(self, painter, option, widget=None):
        painter.setPen(self._pen)
        painter.drawPath(self._path)


class FlowEdgeItem(QGraphicsItem):
    """画布上一条连线（贝塞尔曲线 + 终点箭头）。持有 from/to 节点引用 + port。
    虚线流动动画由共享 QTimer（FlowCanvasView 持有）推进 pen.dashOffset。"""

    # 命中判定容差（场景坐标）：细虚线原样命中太苛刻（线选不中 → Delete 删不
    # 掉 → "只能连不能断"），shape 外扩成 12px 宽的带状轮廓，点线附近即选中。
    HIT_PAD = 6.0
    ARROW = 7.0   # 终点箭头半高（场景坐标）；切线恒水平 → 箭头恒水平指入端口

    def __init__(self, from_item, to_item, port, color="#888", to_port="in"):
        super().__init__()
        self.from_item = from_item
        self.to_item = to_item
        self.port = port
        self.to_port = to_port or "in"   # 目标端口（默认普通入口）
        self._color = QColor(color)
        self._pen = QPen(self._color, 2)
        self._pen.setStyle(Qt.DashLine)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setZValue(-1)
        self.update_positions()

    def endpoints(self):
        """两端端点（场景坐标）。view 的"按住线端改接"判定用它。"""
        return self._p1, self._p2

    def shape(self) -> QPainterPath:
        path = QPainterPath(self._p1)
        path.lineTo(self._p2)
        stroker = QPainterPathStroker()
        stroker.setWidth(self.HIT_PAD * 2)
        s = stroker.createStroke(path)
        # 箭头也归进命中区：想删线时点箭头就该选中整条线
        a = self.ARROW
        s.addRect(self._p2.x() - a * 2, self._p2.y() - a, a * 2, a * 2)
        return s

    def boundingRect(self):
        # 比曲线盒外扩命中/高亮/箭头的余量，超出部分会被裁剪、端点附近选不中
        m = self.HIT_PAD + 4
        return self._path.boundingRect().adjusted(-m, -m, m, m)

    def is_downstream_of(self, item):
        return item is self.from_item or item is self.to_item

    def update_positions(self):
        """按两端节点当前位置重算曲线。prepareGeometryChange 不能省：
        Qt 靠它拿旧 boundingRect 擦背景，漏了就是拖完节点满屏线残影。"""
        self.prepareGeometryChange()
        self._p1 = self._port_scene(self.from_item, self.port)
        self._p2 = self._port_scene(self.to_item, self.to_port)
        self._path = _bezier_path(self._p1, self._p2)
        self.update()

    def paint(self, painter: QPainter, option, widget=None):
        # 选中高亮：虚线太细，不点亮一下用户不知道删的是哪条
        if self.isSelected():
            painter.setPen(QPen(QColor("#ffd600"), 5))
            painter.drawPath(self._path)
        painter.setPen(self._pen)
        painter.drawPath(self._path)
        # 方向箭头：执行流是 token 传播（不看节点摆位），方向一眼可见很重要。
        # 贝塞尔切线恒水平 → 箭头指向 = 终点处切线方向：源在左指右，
        # 源摆得比目标靠右就指左（回边/绕行线一眼能看出方向）。
        a = self.ARROW
        p1, p2 = self._p1, self._p2
        d = -1.0 if p1.x() > p2.x() else 1.0
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(self._color))
        painter.drawPolygon(QPolygonF([
            p2,
            QPointF(p2.x() - d * a * 1.8, p2.y() - a),
            QPointF(p2.x() - d * a * 1.8, p2.y() + a)]))

    @staticmethod
    def _port_scene(item, port):
        for pid, x, y, _ in item._ports():
            if pid == port:
                return item.mapToScene(QPointF(x, y))
        return item.mapToScene(QPointF(NODE_W, NODE_H // 2))


class FlowCanvasView(QGraphicsView):
    """中栏画布：滚轮缩放，右键按住拖平移，左键拖空白框选（无边界大画布，
    任意方向拖），节点连线（点出点→点入点），连线流动动画。

    手势：滚轮缩放 / 右键按住拖平移 / 左键拖空白框选 / 左键按节点拖动。
    不设滚动条：浏览全靠右键拖 + 缩放，
    画布边界大到画不完（±10 万），
    节点摆到负坐标、拖出老远都不受任何范围限制。"""

    MIN_SCALE, MAX_SCALE = 0.2, 4.0
    # 端点抓取半径（场景坐标）：按下点离选中边的端点多近算"捏住端点改接"。
    # 线两端被节点图形盖着，itemAt 在端点附近只会返回节点，所以改接判定
    # 用直接距离而不是命中测试。
    ENDPOINT_GRAB = 16.0
    # 拉线磁吸半径（屏幕像素，换算场景坐标时随缩放走）：拖线途中离端口
    # 最近且在半径内 → 橡皮筋头吸上去 + 端口亮环。松手时目标以吸附为准，
    # 不用精确点到端口才算连上（n8n 同款手感）。
    SNAP_SCREEN = 30.0

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        # 视口尺寸变化时锚定左上角，不重新居中。病根：校验条 lbl_valid 挂在画布
        # 下方主布局里，切换流程时它按各流程校验结果显隐/变高 → 视口高度跟着变，
        # 旧的 AnchorViewCenter 会在视口涨缩时把整幅场景重新居中，表现为"所有节点
        # 集体跳一下"（实测：视口高变 24px → 节点整体平移 12px）。NoAnchor 让画面
        # 顶端钉死，抽屉（校验条）怎么抽拉都不挪画面；顺带修 splitter 调宽时横向漂移。
        self.setResizeAnchor(QGraphicsView.NoAnchor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 右键按住拖平移靠"移动滚动条 value"实现，可拖范围 = sceneRect 减去视口。
        # 旧版把 sceneRect 动态设成刚好罩住内容，等于只能往内容方向拖一点点、
        # 负方向（左上）完全拖不动——这就是"有边界"的病根。这里固定一个以原点
        # 为中心、±10 万的巨大 sceneRect：任何方向都有近乎无限的拖拽余量，节点
        # 摆到负坐标或拖出老远都不受限。滚动条关掉后 sceneRect 不产生可见滚动条，
        # 只作拖拽边界用。
        BIG = 100000
        scene.setSceneRect(-BIG, -BIG, 2 * BIG, 2 * BIG)
        # 视口初始对准原点：四个方向各留约 BIG 的拖拽余量。不设这句的话，
        # 视口默认贴着 sceneRect 某条边角，只有单侧能拖，等于还是"有边界"。
        self.centerOn(0, 0)
        self._panning = False
        self._pan_pos = None
        self._hover_px = QPoint()   # 最近鼠标控件坐标：Tab 唤搜索框时定落点
        self.setMouseTracking(True)
        # 左键拖空白 = 橡皮筋框选（QGraphicsView 原生：拖背景才出现，按在节点上
        # 仍是拖节点，互不干扰）；平移是右键的（原生框选只吃左键，互不冲突）
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setRubberBandSelectionMode(Qt.IntersectsItemShape)

        # ── 连线交互状态 ──
        self._drag_from = None   # (item, port_id, 起点场景坐标) 按下出点后建立
        self._edge_reconnect = None  # (edge_item, "from"/"to", 固定端场景坐标)
                                     # 按住已选中的线再拖 = 拖端点改接（断线重连）
        self._rubber = None      # 橡皮筋 _CurveRubber（曲线预览）
        self._snap = None        # 拉线磁吸命中的目标 (to_item, port_id, 场景点)
        self._hot_item = None    # 当前亮环端口的宿主节点（离开/收尾时熄灭）
        self._connect_fn = None  # (from_item, port, to_item) 完成连线回调（panel 注入）
        self._reconnect_fn = None  # (edge, end, target_item, scene_pos) 改接回调
        self._delete_fn = None   # 删除回调
        self._add_at_fn = None   # 双击空白/Tab 加节点回调 (scene_pos, global_pos)
        self._readonly_fn = None  # 只读探测：True = 当前流程上锁，禁一切拖线/拖节点
        self._shortcut_fn = None  # Ctrl 系快捷键回调 (命令字符串)
        self._nudge_fn = None    # 方向键微调回调 (dx格数, dy格数)

        # ── 流动动画：一个共享 QTimer 推进全部边的 dashOffset ──
        self._anim_offset = 0.0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._advance_anim)
        self._anim_timer.start(33)  # ~30fps

    # ── 动画 ──
    def _advance_anim(self):
        self._anim_offset = (self._anim_offset - 1.0) % 20.0
        for item in self.scene().items():
            # 边/橡皮筋都是持笔自绘：改 pen 的 dashOffset 再 update 即可
            if isinstance(item, (FlowEdgeItem, _CurveRubber)):
                item._pen.setDashOffset(self._anim_offset)
                item.update()

    # ── 连线交互注入（panel 连线时回调）──
    def set_connect_callback(self, fn):
        self._connect_fn = fn

    def set_reconnect_callback(self, fn):
        self._reconnect_fn = fn

    def set_delete_callback(self, fn):
        self._delete_fn = fn

    def set_add_at_callback(self, fn):
        self._add_at_fn = fn

    def set_readonly_callback(self, fn):
        self._readonly_fn = fn

    def set_shortcut_callback(self, fn):
        self._shortcut_fn = fn

    def set_nudge_callback(self, fn):
        self._nudge_fn = fn

    def _is_readonly(self):
        """只读 = 有探测回调且当前流程上锁（口径与 FlowNodeItem._is_readonly 一致；
        探测回调由面板 set_readonly_callback 注入 = panel._is_locked）。"""
        return self._readonly_fn is not None and self._readonly_fn()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete and self._delete_fn is not None:
            self._delete_fn()
            event.accept()
            return
        # Tab = 在光标处唤出可搜索加节点框（ComfyUI/Blueprint 同款）。落点用
        # 当前鼠标所在场景坐标：键盘没有位置，取 mouseMoveEvent 记的 hover 点。
        if event.key() == Qt.Key_Tab and self._add_at_fn is not None \
                and not self._is_readonly():
            scene_pos = self.mapToScene(self._hover_px)
            self._add_at_fn(scene_pos, self.mapToGlobal(self._hover_px))
            event.accept()
            return
        if event.key() == Qt.Key_Escape:
            # 取消选中 + 收掉进行中的拉线/改接（不删任何东西）
            self._clear_rubber()
            self.scene().clearSelection()
            event.accept()
            return
        # 方向键微调选中节点（Designer 同款）：一步一格（20），Shift 五格大步。
        # 执行在面板（撤销栈/写回节点/重排边都归那边管）。
        if event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down) \
                and self._nudge_fn is not None:
            dx = {Qt.Key_Left: -1, Qt.Key_Right: 1}.get(event.key(), 0)
            dy = {Qt.Key_Up: -1, Qt.Key_Down: 1}.get(event.key(), 0)
            step = 5 if event.modifiers() & Qt.ShiftModifier else 1
            self._nudge_fn(dx * step, dy * step)
            event.accept()
            return
        # Ctrl 系快捷键只在画布有焦点时进这里（文本框抢走焦点时收不到），
        # 交面板分流；Shift 单独判（Ctrl+Shift+Z = 重做）
        if event.modifiers() & Qt.ControlModifier and self._shortcut_fn is not None:
            k = event.key()
            shift = bool(event.modifiers() & Qt.ShiftModifier)
            cmd = {Qt.Key_Z: "redo" if shift else "undo",
                   Qt.Key_Y: "redo",
                   Qt.Key_C: "copy", Qt.Key_V: "paste", Qt.Key_X: "cut",
                   Qt.Key_D: "dup", Qt.Key_A: "selectall"}.get(k)
            if cmd:
                self._shortcut_fn(cmd)
                event.accept()
                return
        super().keyPressEvent(event)

    # ── 缩放 ──
    def _scale_factor(self):
        return self.transform().m11()

    # ── 相机（视口 = 一条共用相机，切换流程须整体存取，否则 A 的平移缩放漏给 B）──
    def get_camera(self):
        """当前相机 = (缩放矩阵, 视口中心对应的场景点)。平移改的是滚动条
        value，矩阵里看不见，故一并取中心点。"""
        vp = self.viewport().rect()
        return self.transform(), self.mapToScene(vp.center())

    def set_camera(self, transform, center):
        """灌回一个相机：先复位缩放矩阵，再把 center 这个场景点对回视口中央。
        centerOn 走滚动条，能同时还原矩阵看不见的平移。"""
        self.setTransform(transform)
        self.centerOn(center)

    def reset_camera(self):
        """复位到默认视角：identity 缩放 + 视口对准原点（空流程首次打开用）。
        有节点时首次打开改走 fit_scene 框住内容，都绝不继承上一条流程的镜头。"""
        self.resetTransform()
        self.centerOn(0, 0)

    def zoom(self, factor: float):
        new = self._scale_factor() * factor
        if not (self.MIN_SCALE <= new <= self.MAX_SCALE):
            return
        self.scale(factor, factor)

    def fit_scene(self):
        """「适应」= 把所有节点（含端口外扩余量）框进视口。
        不能 fitInView(sceneRect)：画布是无边界大矩形，那等于永远缩到看不见。"""
        rect = self._content_rect()
        if rect.isEmpty():
            return
        rect = rect.adjusted(-40, -40, 40, 40)
        self.fitInView(rect, Qt.KeepAspectRatio)
        # fitInView 不吃缩放上下限：长流程（如黑市整理布局后宽近万 px）一口能
        # 压到 MIN_SCALE 以下，而 zoom() 守卫是「新值越界即拒」——从越界处 ×1.25
        # 仍 < MIN → 放大被永久拒，滚轮彻底死锁（实测 bug）。钳回最近边界并
        # 对准内容中心：看不全还能右键平移滚着看，滚轮不能死。
        s = self._scale_factor()
        if s < self.MIN_SCALE or s > self.MAX_SCALE:
            tgt = min(max(s, self.MIN_SCALE), self.MAX_SCALE)
            self.resetTransform()
            self.scale(tgt, tgt)
            self.centerOn(rect.center())

    def _content_rect(self):
        """当前画布上全部节点的场景坐标包围盒（含端口外扩余量），空画布返回
        空矩形。不传 scene 用自身场景；FlowPanel 在 scene.clear() 之前抓旧图
        包围盒时显式传旧 scene。"""
        sc = self.scene() if self.scene() is not None else None
        rect = QRectF()
        for item in (sc or self.scene()).items():
            if isinstance(item, FlowNodeItem):
                rect = rect.united(item.boundingRect().translated(item.pos()))
        return rect

    def content_visible(self) -> bool:
        """全部节点中心都在视口可见场景内吗（切流程首开要不要框的判据）：
        全见 → 镜头原地不跳；有节点在视口外 → 框住（"消失不见"病根不复发）。
        判"包围盒有交集"不够格（实测坑）：一条流程半截在视口外照样相交，
        切过去那半截就是看不见的节点。空画布算不可见：新流程节点默认摆原点
        附近，镜头不对准则摆完看不见。"""
        items = [it for it in self.scene().items() if isinstance(it, FlowNodeItem)]
        if not items:
            return False
        vp = self.mapToScene(self.viewport().rect()).boundingRect()
        return all(vp.contains(it.mapToScene(it.boundingRect().center()))
                   for it in items)

    def wheelEvent(self, event):
        # 滚轮直接缩放（原 Ctrl+滚轮的 Ctrl 是多余修饰键，删）；
        # 锚点在鼠标下（__init__ 设了 AnchorUnderMouse），滚哪缩哪
        self.zoom(1.25 if event.angleDelta().y() > 0 else 0.8)
        event.accept()

    # ── 点端口拉线 / 右键拖平移 / 空白框选 ──
    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            # 右键按住 = 平移（右键没有别的职责，不会和框选/菜单抢）；
            # 不进场景，按下即接管
            self._panning = True
            self._pan_pos = event.position()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton and event.modifiers() == Qt.NoModifier:
            # 上锁流程：只读查看，不起手拉线/改接（松手也连不上，别给假反馈）
            ro = self._is_readonly()
            # 按下点：命中出点端口 → 进入连线程
            scene_pos = self.mapToScene(event.position().toPoint())
            # 已选中的线上再按端点 = 拖端点改接（断线重连）。判"按在哪"
            # 不能走 itemAt：线的两端永远被节点项盖住（端口画在节点
            # boundingRect 内、节点 z 序更高），itemAt 只会返回节点。
            # 改成"选中边 + 落点在端点抓取半径内"的直接距离判定。
            if not ro:
                for sel in self.scene().selectedItems():
                    if isinstance(sel, FlowEdgeItem) and self._try_begin_reconnect(sel, scene_pos):
                        event.accept()
                        return
            item = self.scene().itemAt(scene_pos, self.transform())
            if not ro and isinstance(item, FlowNodeItem):
                hit = item.port_at(item.mapFromScene(scene_pos))
                # 入点只有普通 in。点出点才起连线。
                if hit and hit[0] != "in":   # 点的是出点
                    pid, start_sp = hit       # 起点：mapToScene 收的是 QPointF 坐标，拿字符串端口号去转会直接 TypeError
                    self._drag_from = (item, pid, start_sp)
                    self._begin_rubber(start_sp, scene_pos)
                    event.accept()
                    return
            # 按中"没被选中"的节点：先清掉旧多选再起拖。否则上次框选/Ctrl+A
            # 残留的选中集会跟着一起动（Qt 默认多选拖=整组拖），体感"拖一个
            # 其它跟着变"。但按中的节点本身已在选中集时【不清】——那是用户
            # 框选后要整组平移的标准手势（Figma/n8n 同款），交 Qt 原生组拖。
            # Ctrl 加选走原生（本分支外层已限 NoModifier，进不来）。
            if isinstance(item, FlowNodeItem) and not item.isSelected():
                for sel in self.scene().selectedItems():
                    if sel is not item:
                        sel.setSelected(False)
            # 空白处：先取消选中（橡皮筋模式下 Qt 不把空白 press 转给场景，
            # 不显式清一下点空白就取消不掉了），再交给基类起框选
            if item is None:
                self.scene().clearSelection()
        super().mousePressEvent(event)

    def _try_begin_reconnect(self, edge, scene_pos) -> bool:
        """按下点离选中边的某端 ≤ ENDPOINT_GRAB → 进改接拖拽，返回 True。
        两端都在范围内取更近的一端。"""
        p1, p2 = edge.endpoints()
        d1, d2 = _dist2(scene_pos, p1), _dist2(scene_pos, p2)
        r2 = self.ENDPOINT_GRAB * self.ENDPOINT_GRAB
        if d1 > r2 and d2 > r2:
            return False
        end = "from" if d1 <= d2 else "to"
        fixed = p2 if end == "from" else p1
        self._edge_reconnect = (edge, end, fixed)
        self._begin_rubber(fixed, scene_pos)
        return True

    def _begin_rubber(self, start_sp, scene_pos):
        """新建橡皮筋（连新线 / 端点改接共用）。曲线预览与正式边同款。"""
        self._rubber = _CurveRubber(start_sp, scene_pos)
        self.scene().addItem(self._rubber)

    # ── 拉线磁吸 ──
    def _clear_hot(self):
        if self._hot_item is not None:
            self._hot_item._hot_port = None
            self._hot_item.update()
            self._hot_item = None

    def _update_snap(self, scene_pos, allowed_fn):
        """拖线途中找最近端口：命中则橡皮筋头吸到端口、该口画亮环，
        返回 True。allowed_fn(item, port_id) 判这个口能不能接（同节点/改接的
        源端自身等要排除）。没命中清掉上一帧高亮，返回 False。"""
        r = self.SNAP_SCREEN / max(0.25, self._scale_factor())
        r2 = r * r
        best = None
        for item in self.scene().items():
            if not isinstance(item, FlowNodeItem):
                continue
            for pid, x, y, _ in item._ports():
                if not allowed_fn(item, pid):
                    continue
                sp = item.mapToScene(QPointF(x, y))
                d = _dist2(scene_pos, sp)
                if d <= r2 and (best is None or d < best[0]):
                    best = (d, item, pid, sp)
        if best is None:
            self._snap = None
            self._clear_hot()
            return False
        _, item, pid, sp = best
        self._snap = (item, pid, sp)
        if self._hot_item is not item:
            self._clear_hot()
            self._hot_item = item
        if item._hot_port != pid:
            item._hot_port = pid
            item.update()
        return True

    def mouseDoubleClickEvent(self, event):
        # 双击线 = 直接删线（不用先单击选中再按 Delete 两步）
        if event.button() == Qt.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            item = self.scene().itemAt(scene_pos, self.transform())
            if isinstance(item, FlowEdgeItem) and self._delete_fn is not None:
                self.scene().clearSelection()
                item.setSelected(True)
                self._delete_fn()
                event.accept()
                return
            if item is None and self._add_at_fn is not None \
                    and not self._is_readonly():
                # 双击空白 = 就地加节点（弹类型菜单，选完落在双击点上）
                self._add_at_fn(scene_pos, event.globalPosition().toPoint())
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        self._hover_px = event.position().toPoint()
        if self._rubber is not None and self._edge_reconnect is not None:
            # 端点改接拖拽中：橡皮筋从固定端跟到鼠标（磁吸命中则跟到端口）
            edge, end, fixed = self._edge_reconnect
            scene_pos = self.mapToScene(event.position().toPoint())
            if end == "to":
                # 目标端只认 in 口
                ok = self._update_snap(
                    scene_pos,
                    lambda it, pid, _f=edge.from_item: it is not _f and pid == "in")
            else:
                # 源端只认与这条边端口号相同的出点：改接回调按 edge.port 找源端口，
                # 吸附到别的口只会撞上"源端口对不上"被拒。
                ok = self._update_snap(
                    scene_pos,
                    lambda it, pid, _t=edge.to_item, _p=edge.port:
                    it is not _t and pid == _p)
            head = self._snap[2] if ok else scene_pos
            self._rubber.set_points(fixed, head)
            event.accept()
            return
        if self._rubber is not None and self._drag_from is not None:
            from_item, from_port, start_sp = self._drag_from
            scene_pos = self.mapToScene(event.position().toPoint())
            # 起线拖拽的目标端只认 in 口
            ok = self._update_snap(
                scene_pos,
                lambda it, pid, _f=from_item: it is not _f and pid == "in")
            head = self._snap[2] if ok else scene_pos
            self._rubber.set_points(start_sp, head)
            event.accept()
            return
        if self._panning and self._pan_pos is not None:
            d = event.position() - self._pan_pos
            self._pan_pos = event.position()
            hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
            hbar.setValue(hbar.value() - int(d.x()))
            vbar.setValue(vbar.value() - int(d.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def _clear_rubber(self):
        if self._rubber is not None:
            self.scene().removeItem(self._rubber)
            self._rubber = None
        self._snap = None
        self._clear_hot()   # 磁吸亮环保留到下一次 mouseMove 会闪残环，随手一起灭
        self._drag_from = None
        self._edge_reconnect = None

    def contextMenuEvent(self, event):
        # 右键已征用为拖拽平移：吞掉默认上下文菜单，松手不弹空菜单
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton and self._panning:
            self._panning = False
            self._pan_pos = None
            self.viewport().unsetCursor()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._edge_reconnect is not None:
            # 端点改接收口：固定端不动，拖的那端认到哪个节点就改接哪端；
            # 磁吸命中优先于松手位置（不用精确点中节点）；松空白 = 取消
            edge, end, _fixed = self._edge_reconnect
            snap = self._snap
            scene_pos = self.mapToScene(event.position().toPoint())
            item = snap[0] if snap else \
                self.scene().itemAt(scene_pos, self.transform())
            self._clear_rubber()   # 同连新线：先收橡皮筋，回调里会整场重建
            if isinstance(item, FlowNodeItem) and self._reconnect_fn is not None:
                self._reconnect_fn(edge, end, item, scene_pos)
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._drag_from is not None:
            snap = self._snap
            scene_pos = self.mapToScene(event.position().toPoint())
            item = snap[0] if snap else \
                self.scene().itemAt(scene_pos, self.transform())
            from_item, from_port, _ = self._drag_from
            # 顺序不能反：连上后回调会 _rebuild_scene() → scene.clear()，
            # 橡皮筋（挂在同一个 scene 上）会被一并销毁，事后再 removeItem
            # 就撞 "already deleted"。先收线再建边。
            self._clear_rubber()
            # 松在目标节点本体任意处即完成连线。原实现要求精确命中入点
            # 小圆 ±6px —— 手点根本够不着，松开总被判"空白取消"，
            # 体感就是"模块之间连不上"。入点位置由 FlowEdgeItem 画线时自算。
            if isinstance(item, FlowNodeItem) and item is not from_item \
                    and self._connect_fn is not None:
                self._connect_fn(from_item, from_port, item, "in")
            event.accept()
            return
        super().mouseReleaseEvent(event)


# ── 流程库侧栏拖拽：拖流程行 → 落到组标题 = 换分类 ────────────────────
# 列表顺序是分类账本派生的（_refresh_flow_list 每次整体重建），绝不用
# QListWidget 内置 InternalMove——自由排行会被下一次重建冲掉，纯误导。
# 这里只借拖的"手势"：mime 自定义、落点只认组标题行（UserRole+1），
# 结果发信号交回 FlowPanel 复用 _move_flow_to 落账。
FLOW_DRAG_MIME = "application/x-flowlib-flow"
_FLOW_HL_ROLE = Qt.UserRole + 2   # 落点高亮标记（_FlowLockDelegate 据此手画底色）
_FLOW_FOLDED_ROLE = Qt.UserRole + 4  # 分类组标题折叠态（True=收起/▶，False/缺省=展开/▼）
#   为什么折叠态走独立 role、箭头由委托手绘：不用 ▼/▶ 字符拼文本 —— 同一字体里
#   这两个码点的字形大小常不一致（用户实拍：▼ 明显大于 ▶，"箭头一大一小"），
#   且在缺字形的机器上会被 fallback 字体换掉。手绘固定尺寸三角 = 两态永远等大、
#   跨机器一致。文本只留「分类名（N）」，箭头位由绘制留白让出。


def _flow_mime_of(name: str) -> QMimeData:
    """拖起数据组装（拆成纯函数给冒烟直测）。"""
    md = QMimeData()
    md.setData(FLOW_DRAG_MIME, name.encode("utf-8"))
    return md


def _flow_name_of_mime(md) -> str:
    """从拖拽数据取流程名；不是自家 mime → 空串。"""
    if md is None or not md.hasFormat(FLOW_DRAG_MIME):
        return ""
    return bytes(md.data(FLOW_DRAG_MIME)).decode("utf-8", "replace")


def _drag_flow_name(item) -> str:
    """拖起资格：只有流程行（UserRole=流程名）能拖；组标题/None → 空串。"""
    if item is None:
        return ""
    return str(item.data(Qt.UserRole) or "")


def _drop_cat_name(item):
    """落点判定（纯函数）：组标题行（UserRole+1=分类名）→ 分类名；
    流程行 → 所在组的分类（UserRole+3，_refresh_flow_list 打组时挂）——
    整组区都是落点：拖到组内流程行上也该算（2026-10-04 真机反馈：只认
    20px 组标题行，用户凭直觉往「那一坨」上拖被静默拒，体感=功能坏了）。
    空白(None)/没打组标的行 → None。不许解析「▼ xxx（N）」文本。"""
    if item is None:
        return None
    cat = item.data(Qt.UserRole + 1)
    if cat:
        return str(cat)
    if item.data(Qt.UserRole):
        cat = item.data(Qt.UserRole + 3)
        return str(cat) if cat else None
    return None


class _FlowLibList(QListWidget):
    """流程库侧栏列表 + 拖拽换分类。子类不认识 FlowPanel：拖起带自定义 mime，
    唯一合法落点 = 组标题行，落中只发 flowDropped(流程名, 分类名)。
    上锁流程允许拖（改的是分类账本，不碰流程本体，与锁语义不冲突）。

    另发 catToggled(分类名)：单击组标题行 = 折叠/展开该分类（2026-10-11）。
    为什么在列表 mousePressEvent 里做、不靠 itemClicked：组标题是 NoItemFlags
    （不可选不可点，防误选），itemClicked 对它的触发行为随 Qt 版本不一
    （项目同吃 6.4.3 与 6.11.2）——显式命中测试两版一致，最稳。"""

    flowDropped = Signal(str, str)
    catToggled = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self._hl_item = None
        # 滚动条策略钉死（2026-10-11 修「点折叠分类栏宽度跳变」）：默认 AsNeeded
        # 下，展开时内容多→纵向滚动条出现（占 ~14px），折叠后内容少→滚动条消失，
        # 可视区宽度 152↔166 一跳，用户看着就像"分类栏一大一小变了宽"。
        # 纵向 = 常显（宽度恒定，不随内容多少显隐）；横向 = 关（长名字本就走
        # 省略号，不给横向滚动条，免得它也参与跳变）。
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def mousePressEvent(self, event):
        # 单击组标题 → 折叠/展开（命中 UserRole+1 且无 UserRole 的行）。
        # 吞掉事件不乱选中（标题本就 NoItemFlags）；其余行交回原生交互。
        if event.button() == Qt.LeftButton:
            it = self.itemAt(event.position().toPoint())
            if it is not None and not it.data(Qt.UserRole) \
                    and it.data(Qt.UserRole + 1):
                self.catToggled.emit(str(it.data(Qt.UserRole + 1)))
                event.accept()
                return
        super().mousePressEvent(event)

    def startDrag(self, supportedActions):
        name = _drag_flow_name(self.currentItem())
        if not name:
            return      # 组标题 NoItemFlags 本进不来，兜一层：不给内置拖起机会
        drag = QDrag(self)
        drag.setMimeData(_flow_mime_of(name))
        # 拖拽影子：写流程名的小标签。没影子时 Windows 只换光标，
        # 用户看不出"拖起没有"（真机反馈补上）。grab 不 show，不触发孤儿窗铁律。
        lab = QLabel(f" {name} ")
        lab.setStyleSheet("background:#ffffff;color:#333333;"
                          "border:1px solid #9aa0a6;border-radius:4px;"
                          "padding:2px 6px;")
        pm = lab.grab()
        drag.setPixmap(pm)
        drag.setHotSpot(pm.rect().center())
        drag.exec(Qt.MoveAction)

    def _hit_header(self, e):
        """拖拽事件 → 命中的条目（自家 mime 才理）；外部拖放一律 None。"""
        if not _flow_name_of_mime(e.mimeData()):
            return None
        return self.itemAt(e.position().toPoint())

    def _group_hdr(self, it):
        """命中行所在组的标题行（从该行往上找第一个挂 UserRole+1 的）。"""
        for row in range(self.row(it), -1, -1):   # item 没有 .row()，走列表侧 QListWidget.row(item)
            cand = self.item(row)
            if cand.data(Qt.UserRole + 1):
                return cand
        return None

    def _track(self, e):
        """enter/move 共用：合法落点（组区任意行）→ 高亮挂到组标题+接落；
        否则清高亮+拒。高亮只标组标题，整组区悬停都点亮同一行。"""
        it = self._hit_header(e)
        hl = (self._group_hdr(it) if _drop_cat_name(it) is not None else None
              if it is not None else None)
        self._set_hl(hl)
        if hl is not None:
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragEnterEvent(self, e):
        self._track(e)

    def dragMoveEvent(self, e):
        self._track(e)

    def dragLeaveEvent(self, e):
        self._set_hl(None)

    def dropEvent(self, e):
        it = self._hit_header(e)
        cat = _drop_cat_name(it)
        name = _flow_name_of_mime(e.mimeData())
        self._set_hl(None)      # 先摘高亮再发信号：下面重建会把行整个换掉
        if name and cat is not None:
            e.acceptProposedAction()
            self.flowDropped.emit(name, cat)
        else:
            e.ignore()          # 无效落点：松手无动作（条目回原位是默认表现）

    def _set_hl(self, it):
        """高亮只挂标记不画色（画法见 _FlowLockDelegate.paint 标题行分支）。"""
        if self._hl_item is it:
            return
        if self._hl_item is not None:
            self._hl_item.setData(_FLOW_HL_ROLE, None)
        self._hl_item = it
        if it is not None:
            it.setData(_FLOW_HL_ROLE, True)


class _FlowLockDelegate(QStyledItemDelegate):
    """流程库列表行尾的上锁/解锁图标（点击即切锁，不进右键菜单）。

    为什么用委托不用 setItemWidget：嵌真按钮会盖掉列表原生的整行绘制，
    悬停底色/选中蓝底/左边框/文字都得自己复刻 —— 委托只多画一个字形，
    整行样式仍走 theme.py 的 #flowList QSS。

    点行尾 _ZONE 区 = 发 toggled 并吞掉事件（不触发切流程）；区外返回
    False 交回列表原生交互（单击选中/双击挂任务/右键菜单）。
    """

    toggled = Signal(str)     # 参数 = 流程名

    _ZONE = 26      # 行尾图标点击区宽（px，屏幕像素）
    _GLYPH = 18     # 字形绘制框边长
    _ARROW_SLOT = 16   # 组标题行箭头占位宽（文本右移这么多，箭头画在左侧槽里）
    _ARROW = 7      # 手绘折叠三角的边长（▼/▶ 两态同一规格——这是修「一大一小」的关键）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._locked: set[str] = set()

    def set_locked(self, names):
        self._locked = set(names)

    def _draw_fold_arrow(self, painter, option, folded: bool):
        """组标题行的折叠三角：▼（展开）/ ▶（收起），两态同一固定尺寸手绘。
        不用 ▼/▶ 字符 —— 同字体两码点字形常不等大（用户实拍 ▼ 明显大于 ▶），
        缺字形还会被 fallback 换掉。手绘三角跨机器、跨字体永远等大可控。
        颜色随组标题文字（TEXT_MUTED 族，QColor 直接给，不依赖 QSS）。"""
        cx = option.rect.left() + self._ARROW_SLOT // 2 + 1
        cy = option.rect.center().y()
        s = self._ARROW
        half = s / 2.0
        if folded:   # ▶ 指向右
            tri = QPolygonF([QPointF(cx - half, cy - half),
                             QPointF(cx - half, cy + half),
                             QPointF(cx + half, cy)])
        else:        # ▼ 指向下
            tri = QPolygonF([QPointF(cx - half, cy - half),
                             QPointF(cx + half, cy - half),
                             QPointF(cx, cy + half)])
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#9aa0a6"))   # 与组标题文字同色（setForeground 那份）
        painter.drawPolygon(tri)
        painter.restore()

    def _box(self, option):
        """整行矩形去掉行尾图标区 —— 交给基类画文字时不压住图标。"""
        r = QRect(option.rect)
        r.setWidth(max(0, r.width() - self._ZONE))
        return r

    def paint(self, painter, option, index):
        if not index.data(Qt.UserRole):        # 分类组标题：原样画底 + 手绘折叠箭头
            opt = QStyleOptionViewItem(option)
            if index.data(_FLOW_HL_ROLE):      # 拖拽落点高亮：先铺浅蓝底再画字
                # 不用 item.setBackground(BackgroundRole)：theme 的 ::item:hover
                # QSS 灰底会盖掉 role 底色；这里手画 + 剥掉悬停态，颜色必达。
                painter.fillRect(option.rect, QColor(theme.ACCENT_SOFT))
                opt.state &= ~QStyle.State_MouseOver
            # 文本前留出箭头位：把文字矩形整体右移一个箭头槽（本委托负责画箭头，
            # 文本由基类画在右移后的矩形里）。缩进量与下方流程行文字对齐感一致。
            opt.rect = QRect(option.rect).adjusted(self._ARROW_SLOT, 0, 0, 0)
            super().paint(painter, opt, index)
            self._draw_fold_arrow(painter, option, bool(index.data(_FLOW_FOLDED_ROLE)))
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # 两遍绘制：第一遍整行矩形但清空文字 → 悬停/选中底色与分隔线铺满整行
        # （含图标区底下，不然右侧留一块没底色的缺口）；第二遍矩形缩到图标区
        # 以外再画文字 → 长名字省略号不会压在图标上。底色重复画同色无副作用。
        text = opt.text
        opt.text = ""
        super().paint(painter, opt, index)
        opt.rect = self._box(option)
        opt.text = text
        super().paint(painter, opt, index)
        name = index.data(Qt.UserRole)
        locked = name in self._locked
        box = option.rect
        r = QRect(box.right() - self._ZONE, box.center().y() - self._GLYPH // 2,
                  self._GLYPH, self._GLYPH)
        if option.state & QStyle.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.ACCENT_SOFT))
            painter.drawRoundedRect(r, 3, 3)
        painter.setPen(QColor(theme.WARN if locked else theme.TEXT_MUTED))
        painter.setFont(QFont("Segoe MDL2 Assets", 9))
        painter.drawText(r, Qt.AlignCenter, icons.LOCK if locked else icons.UNLOCK)

    def editorEvent(self, event, model, option, index):
        # 图标区内三个鼠标事件全吞：点锁不该顺带切换流程（会弹"未保存丢失"
        # 确认），也不该被双击当成"挂任务"。切换只在 Press 做一次 ——
        # 放 Release 上会被连击的第二次 release 再切回去。
        if index.data(Qt.UserRole) and event.type() in (
                QEvent.MouseButtonPress, QEvent.MouseButtonRelease,
                QEvent.MouseButtonDblClick) and event.button() == Qt.LeftButton:
            if event.position().toPoint().x() >= option.rect.right() - self._ZONE:
                if event.type() == QEvent.MouseButtonPress:
                    self.toggled.emit(index.data(Qt.UserRole))
                return True
        return False


class _NodeSearchPopup(QDialog):
    """Tab / 双击空白唤出的可搜索加节点框（ComfyUI·Blueprint 同款手感）。
    打字即筛（匹配类型名、分组、说明文字），↑↓ 选、回车放、Esc 取消；
    底部一行常驻说明，鼠标停在某项上就看得懂它是干什么的。"""

    _HROLE = Qt.UserRole + 1     # 分组标题行标记（↑↓ 导航绕行用）

    def __init__(self, parent, items, pos):
        super().__init__(parent)
        self.setModal(True)                   # Esc = reject（Qt 白送）；点框外不关，靠 Esc/选中
        self.chosen = None
        self._items = items                   # [(type, label, group, help)]
        self.edt = QLineEdit()
        self.edt.setPlaceholderText("搜索节点类型　↑↓ 选择　回车放置")
        self.lst = QListWidget()
        self.lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 行内边距：默认行高贴着文字，十来项挤成一坨看不出条目边界
        self.lst.setStyleSheet("QListWidget::item{padding:3px 6px;}")
        # ⚠ 无父填项前强制样式生效（同 _cond_multi 取色点列表坑）：本弹窗
        #   __init__ 里就 _filter("") 填项，不 polish 则排位按裸字体 17px、
        #   绘制按带内衬 23px，行文字互压、点击热区偏半行选错节点。
        self.lst.ensurePolished()
        self.lbl_help = _hint("")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)
        lay.addWidget(self.edt)
        lay.addWidget(self.lst)
        lay.addWidget(self.lbl_help)
        self.edt.textChanged.connect(self._filter)
        self.edt.installEventFilter(self)
        self.lst.currentRowChanged.connect(self._show_help)
        self.lst.itemDoubleClicked.connect(lambda _: self._accept())
        self.setWindowTitle("添加节点")   # 标题栏本来就有，只是从没设过文字（用户点名"没标题"）
        self.setMinimumWidth(300)
        self._filter("")
        # 定位钳制：光标右下优先，超出光标所在屏幕的可用区就推回来。
        # 旧版直接 move(pos) 不钳制：画布下半屏双击 = 框体太高（列表按条目
        # 数最长到 470px）整截垂出屏幕外，底部条目看不见也点不到（用户点名）。
        # 留 8px 余量：move 定位的是客户区，窗口边框/阴影在客户区外还要占几像素。
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        fw = self.sizeHint().width() + 8
        fh = self.sizeHint().height() + 8
        x = max(avail.x(), min(pos.x(), avail.x() + avail.width() - fw))
        y = max(avail.y(), min(pos.y(), avail.y() + avail.height() - fh))
        self.move(x, y)
        self.edt.setFocus()

    def _filter(self, text: str):
        """重建列表（保留当前打字进度下第一项为选中）。数据源就十来项，
        每次全清重建比维护增量简单得多。分组标题是占位行（NoItemFlags 不可选，
        与流程库列表同款）：平铺 13 项糊成字墙，按「动作/流程控制/调试」
        分段后一眼能扫；条目标题也不再拖「· 组名」尾巴，标题行已经说了。"""
        kw = text.strip().lower()
        cur = self.lst.currentItem()
        keep = cur.data(Qt.UserRole) if cur else None
        self.lst.blockSignals(True)
        self.lst.clear()
        last_group = None
        for t, label, group, help_ in self._items:
            hay = f"{label} {group} {t} {_plain(help_)}".lower()
            if kw and kw not in hay:
                continue
            if group != last_group:
                last_group = group
                hdr = QListWidgetItem(group)
                hdr.setFlags(Qt.NoItemFlags)
                hdr.setForeground(QColor("#9aa0a6"))
                hdr.setData(self._HROLE, True)   # 标题标记：↑↓ 导航绕行用
                self.lst.addItem(hdr)
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, t)
            it.setToolTip(help_)
            self.lst.addItem(it)
            if keep and t == keep:
                self.lst.setCurrentItem(it)
        if self.lst.currentRow() < 0 and self.lst.count():
            self.lst.setCurrentRow(self._next_row(-1, 1))
        # 框高跟着条目数长，但封顶 300（约 11 行）：旧封顶 470 全量 16 行一屏摊开，
        # 在画布下半区双击时框体比鼠标到屏幕底边的距离还高，必然出界（用户点名"太长"）。
        # 装不下的靠滚轮/↑↓，反正带搜索框，打字两下就剩三五行。
        self.lst.setFixedHeight(min(max(self.lst.count() * 27 + 8, 130), 300))
        self.lst.blockSignals(False)
        self._show_help(self.lst.currentRow())

    def _next_row(self, r: int, d: int) -> int:
        """↑↓ 走一行，跳过分组标题行（占位行永远选中不了，停在上面卡死手感）。
        环形推进；数据上标题必夹在可选项之间，循环 n 次必有落点。"""
        n = self.lst.count()
        for _ in range(max(n, 1)):
            r = (r + d) % n
            it = self.lst.item(r)
            if it is not None and not it.data(self._HROLE):
                return r
        return r

    def _show_help(self, row: int):
        cur = self.lst.currentItem()   # 当前行 ≠ 选中行：键盘导航只动 current
        if cur is None:
            return
        t = cur.data(Qt.UserRole)
        self.lbl_help.setText(_plain(next((h for tt, _l, _g, h in self._items
                                           if tt == t), ""))[:120])

    def eventFilter(self, obj, ev):
        if obj is self.edt and ev.type() == ev.Type.KeyPress:
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter,
                            Qt.Key_Down, Qt.Key_Up):
                if ev.key() == Qt.Key_Down or ev.key() == Qt.Key_Up:
                    if self.lst.count():
                        d = 1 if ev.key() == Qt.Key_Down else -1
                        # 绕行导航：分组标题行不停留（见 _next_row）
                        r = self._next_row(self.lst.currentRow(), d)
                        self.lst.setCurrentRow(r)
                        self.lst.scrollToItem(self.lst.currentItem())
                else:
                    self._accept()
                ev.accept()
                return True
        return super().eventFilter(obj, ev)

    def _accept(self):
        it = self.lst.currentItem()
        if it is None:
            self.reject()
            return
        self.chosen = it.data(Qt.UserRole)
        self.accept()


def _palette_items():
    """节点调色板条目（分组顺序 = 菜单/搜索框顺序）：类型、显示名、组名、说明。
    和「新增节点」下拉共用同一份 NODE_GROUPS/TYPE_LABEL/NODE_HELP，不另存清单。"""
    return [(t, TYPE_LABEL[t], g, NODE_HELP.get(t, ""))
            for g, ts in NODE_GROUPS for t in ts]


class _CurPageTabs(QTabWidget):
    """页签控件高度只按「当前页」算：QStackedLayout 的 minimumSizeHint 默认取
    所有页的最大值——多组页签高低不平时，短页（如空组）会被拉到最高页的高度，
    页签框内下方一大块空白（用户实拍：并行监视 8 行组+空组）。
    把 sizeHint/minimumSizeHint 里 stack 的贡献换成当前页的贡献即可。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.currentChanged.connect(lambda _: self._clamp())

    def _swap_hint(self, base, stack_h, page_h):
        stack = self.findChild(QStackedWidget)
        cur = self.currentWidget()
        if stack is None or cur is None:
            return base
        return QSize(base.width(), max(0, base.height() - stack_h(stack) + page_h(cur)))

    def minimumSizeHint(self):
        return self._swap_hint(super().minimumSizeHint(),
                               lambda s: s.minimumSizeHint().height(),
                               lambda w: w.minimumSizeHint().height())

    def sizeHint(self):
        return self._swap_hint(super().sizeHint(),
                               lambda s: s.sizeHint().height(),
                               lambda w: w.sizeHint().height())

    def _clamp(self):
        # 光改 hint 不够：外层 QFormLayout 把视口富余照样摊给页签行（Preferred
        # 件在行内唯一可长也照吃，实测空页仍被撑到最高页高）。直接钳死上限，
        # 富余留给面板尾部 stretch。行增删走整场重建，钳值不会过期。
        self.setMaximumHeight(self.sizeHint().height())


class FlowPanel(QWidget):
    """自定义流程编排工作区。嵌在三栏工作台（ui/workbench.py）中栏常驻可见，
    预览/实例/截图定时器由外壳提供。"""

    status_message = Signal(str)   # 请求外壳共用状态栏显示文本
    request_capture = Signal(str, str)  # "去采集"联动：(预填归属目录, 已选模板名)。
                                       # 名非空 = 重新采集模式（采集页锁名、保存覆盖同名）
    request_row_test = Signal(str, str)  # 「▶测这行」：(目录, 图名) → 外壳抓现帧
                                        # 找图一次、apply_row_result 同步回投
    request_finger_test = Signal(dict)  # 「▶ 试扫手指」：携带动态手指行的参数快照
                                        # → 外壳抓现帧跑 detect_finger、apply_finger_result 回投
    test_requested = Signal()      # 「▶ 测试流程」：外壳（工作台）负责起 FlowTestWorker
    multi_test_requested = Signal()  # 「▶ 多开测试」：外壳弹选实例小窗，多台各跑一遍
    test_one_requested = Signal(dict)  # 「▶ 测此节点」：携带选中节点的 dict 快照源
    test_from_requested = Signal(dict)  # 「▶ 从此节点开始测」：携带选中节点 dict，外壳裁子图
    test_stop_requested = Signal() # 「■ 停止」：外壳请求停止当前测试线程
    # 流程事件唯一出口（新名, 旧名）：保存/新建/改名/删除/导入全发这一发。
    # 改名=（新名,旧名）、删除=（"",旧名）、保存/新建=（名,"")、导入=("","")。
    # 主窗口一个槽函数全接（旧名非空先换名，再按集合对账剔死项）——以前改名、
    # 删除各一根线外加 15s 定时器兜底，三处口径必漂移。
    flow_saved = Signal(str, str)
    add_to_queue = Signal(str)  # 「加入任务栏」：把当前选中的流程发主窗口生成一条任务

    # 多点取色点选态归属（cond + 宿主节点）。类级默认 None：冒烟常用
    # FlowPanel.__new__ 借方法不跑 __init__，_rebuild_props 的会话守卫要能读。
    _multi_pick_cond = None
    _multi_pick_node = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = get_config()
        self.flow_name = ""          # 当前流程名
        self.graph = {"version": 2, "nodes": [], "edges": []}  # v2 自由连线图
        self._dirty = False
        self._view = None            # 共用预览（attach_view 注入）
        self._pick_ctx = None        # 取坐标上下文：dict(field=..., node=..., stage=...)
        self._roi_pick_node = None   # 预览框选识别区域的归属字典（server_pick 节点或 cond）
        self._roi_pick_key = "roi"   # 回填用的字典键（默认 roi；cond 面板同款机制）
        self._roi_pick_owner = None  # 成框后要重建属性表单的所属节点（cond 场景=其宿主节点）
        self._capture_return = None  # 回程票 {"node","cond"}：采完自动回填的来源节点
        # 监视行编辑区正在编辑的那一行，按 rows 表分记：{id(rows): (rows 表, 行)}。
        # ⚠ 2026-10-07 修「换行型后列表弹回顶」：旧版是面板级单条记忆，容器多组
        # 同场重建时先重建的组把记忆顶成自己的行，正在编辑的组回位扑空 → 弹回第0行。
        # 值里连 rows 表一起存：既按身份认领，又钉住列表对象防 id 复用张冠李戴。
        self._watch_sel_rows: dict = {}
        self._row_test_results = {}  # 「▶测这行」结果 {(目录,图名): 文本}（换行/重建后仍显示）
        self._finger_test_results = {}  # 「▶试扫手指」结果 {id(行dict): 文本}（按行身份挂）
        self._finger_lbl_ref = None  # 当前编辑区手指行结果标签 (lbl, 行dict)
        self._watch_row_clip = None  # 监视行剪贴板（面板级：切流程不清空→跨流程粘贴；重启清空）
        self._watch_stats_hook = None  # 开着监视编辑器时由 _build_watch_editor 注入的轻刷新
        self._watch_stat_timer = None  # 3s 命中统计刷新定时器（全面板共用一个）
        self._test_cache = {}        # 试运行结果回填：{node_id: "ok"/"failed"/"pending"}
        self._val_problems = []      # 最近一次校验的短文案（保存前健康报告数据源）
        self._val_details = []       # 配套明细（哪张缺图被哪个节点用等）
        # 撤销/重做：结构操作的图快照栈（json 串）。属性表单逐键编辑不进栈——
        # 否则 Ctrl+Z 要先回退几十次"删掉一个字"才轮到有意义的一步，栈等于废了。
        self._undo: list[str] = []
        self._redo: list[str] = []
        self._clipboard = None       # 复制缓冲区 {"nodes":[deepcopy...], "edges":[...]}
        self._paste_n = 0            # 同一片剪贴板连贴第几刀（落点错开用，复制即归零）
        self._guide_items = []       # 拖动对齐参考线（QGraphicsLineItem），松手即清
        self._ghost = None           # 拆出/合并残影飞行动画 (anim, item)，见 _fly_ghost
        # 每条流程自己的相机 {流程名: (缩放矩阵, 视口中心场景点)}。view 全场只有
        # 一条，不各存各的就会"A 一平移、B 跟着挪"。只活在会话里，不落盘。
        self._cameras: dict[str, tuple] = {}
        self._view_flow = None       # 此刻画布上摆的是哪条流程（相机存取的依据）
        self._first_open = True      # 本会话是否还没"首开"过任何流程（2026-10-11）：
        #   软件刚开、镜头停在原点、节点也多在原点附近 → content_visible() 判"可见"
        #   就不 fit，用户得手动点「适应」。加这个标记：本会话第一次点开任意流程
        #   无条件 fit 一次（把内容框进视野），之后恢复正常逻辑（见过回相机/可见不跳）。

        self._build_ui()
        self._refresh_flow_list()
        self._reload_flow()
        # ⚠ 上面 _reload_flow 是"构造时自动载入第一条"，不算用户点开——它会
        #   把 _first_open 吃掉（改 False），且此刻 viewport 还没尺寸、fit 无效。
        #   复位：真正的"用户第一次点开流程"（_select_flow → _fit_first_time）
        #   才触发强制 fit（用户诉求 2026-10-11：打开软件第一次点开流程老得
        #   手动点「适应」）。触发点是"点击"，不是"打开工作台"——不点不跳。
        self._first_open = True

    # ── UI 搭建 ──
    def _build_ui(self):
        # ── 顶部工具栏：当前流程操作（保存/分类/自动保存/测试）。
        # 新建/改名/删除/加入任务栏 收进左侧流程库右键菜单（C 方案）──
        # 流式布局：工作台分栏拖窄时按钮整颗折行，不再被压扁裁字（2026-10-05 实拍）
        bar_flow = FlowLayout()
        self.btn_save = QPushButton("保存流程")
        self.btn_save.setObjectName("btnSuccess")   # 样式走全局主题
        # 当前流程的用途分类（工作台内：流程库列表分组 + 工具栏下拉）。
        # 主界面任务栏不再分类（任务与流程分开），这是分类唯一入口。
        self.cmb_cat = QComboBox()
        self.cmb_cat.setEditable(True)
        self.cmb_cat.setInsertPolicy(QComboBox.NoInsert)   # 敲的字不自动混进列表
        self.cmb_cat.setMinimumWidth(96)
        self.cmb_cat.setToolTip(_CAT_HELP)
        # 自动保存：改动即写入当前流程（落盘由 cfg.set 自带的 1 秒防抖兜底，
        # 不会每次按键写一次盘）。状态记 ui.flow_autosave，重启保留。
        self.chk_autosave = QCheckBox("自动保存")
        self.chk_autosave.setToolTip("勾选后每次改动直接写进当前流程，不用手点保存；\n"
                                     "不勾则改完必须手动点「保存流程」。")
        self.chk_autosave.setChecked(bool(self.cfg.get("ui.flow_autosave", False)))
        # 试运行：跑当前画布内容（含未保存修改），执行链与挂机完全一致
        self.btn_test = QPushButton("▶ 测试流程")
        self.btn_test.setObjectName("btnAccent")   # 样式走全局主题
        self.btn_test.setToolTip("在工作台左上角选中的模拟器实例上跑一遍当前画布（不用先保存）：\n"
                                 "截图 → 按连线逐步执行，与正式挂机同一条链。\n"
                                 "步骤日志进主界面日志面板，状态栏显示实时步骤。")
        self.btn_test_stop = QPushButton("■ 停止")
        self.btn_test_stop.setEnabled(False)
        self.btn_test_stop.setToolTip("停止当前试跑（完成手里这一步后退出）")
        # 单节点测试：只把当前选中的这一个节点拉上模拟器跑，不牵整条链。
        # 采完一张图先验"它认得准不准、点得中不中"，比整流程重跑快一个量级。
        self.btn_test_one = QPushButton("▶ 测此节点")
        self.btn_test_one.setEnabled(False)   # 没选中可测节点时灰着
        self.btn_test_one.setToolTip(
            "只测试当前选中的节点（不用先保存）：把这一个节点单独跑一遍，\n"
            "验证「这张图认不认得、点哪里、等多久」，不牵动其它节点。\n"
            "子流程/并行监视/定时执行不能单测（它们要跑一条链/天生常驻）。")
        # 「从此节点开始测」：裁掉选中节点上游，只跑它和它的下游可达链。
        # 分支/失败口/成环原样保留，适合"我已经把画面摆好了，从中间接着验"。
        self.btn_test_from = QPushButton("▶ 从此节点开始测")
        self.btn_test_from.setEnabled(False)   # 没选中节点时灰着
        self.btn_test_from.setToolTip(
            "从当前选中的节点开始往下测（不用先保存）：\n"
            "把上游整段裁掉，只跑它和它的下游分支/失败口/循环，\n"
            "模拟器画面要已停在选中节点该处的位置，否则上游变量为空。")
        # 多开测试：同一条流程在多台实例上各跑一遍（错峰起跑），结果一张小表
        # 汇总。与单实例「测试流程」二选一：跑多开期间单实例按钮被锁。
        self.btn_multi_test = QPushButton("▶ 多开测试")
        self.btn_multi_test.setToolTip(
            "把当前画布（不用先保存）同时丢到多台模拟器上各跑一遍：\n"
            "弹窗勾选实例 → 各台错峰起跑（约 0.7 秒一台，避免 CPU 瞬时高峰）→\n"
            "结果实时汇总（完成/失败/已停、卡在哪一步、失败截图路径）。\n"
            "没运行 / 正在挂机的实例会被挡掉并在弹窗里点名。")
        # 看门狗（流程级，存 graph 顶层 watchdog_sec，0=关）：整图连续这么多
        # 秒没有任何节点收口 = 卡死空转 → 判失败走留证重试。必须大于图内
        # 最慢单步自身超时，否则误杀慢步骤（_refresh_validation 有防呆提醒）。
        self.spn_watchdog = QSpinBox()
        self.spn_watchdog.setRange(0, 3600)
        self.spn_watchdog.setSuffix(" 秒")
        self.spn_watchdog.setSpecialValueText("关")
        self.spn_watchdog.setValue(0)
        self.spn_watchdog.valueChanged.connect(self._on_watchdog_changed)
        self.spn_watchdog.setToolTip(_WATCHDOG_HELP)
        bar_flow.addWidget(self.btn_save)
        lbl_cat = QLabel("分类:")
        bar_flow.addWidget(lbl_cat)
        # 「?」徽章紧贴名字后面（全项目约定，见 ui/help_badge.py）；
        # 控件自带 tooltip 保留——戳徽章和悬停控件殊途同归，多一条线索不亏
        bar_flow.addWidget(make_help_badge(_CAT_HELP))
        bar_flow.addWidget(self.cmb_cat)
        bar_flow.addWidget(self.chk_autosave)
        bar_flow.addSpacing(12)
        bar_flow.addWidget(QLabel("看门狗:"))
        bar_flow.addWidget(make_help_badge(_WATCHDOG_HELP))
        bar_flow.addWidget(self.spn_watchdog)
        bar_flow.addSpacing(12)
        bar_flow.addWidget(self.btn_test)
        bar_flow.addWidget(self.btn_multi_test)
        bar_flow.addWidget(self.btn_test_one)
        bar_flow.addWidget(self.btn_test_from)
        bar_flow.addWidget(self.btn_test_stop)
        bar_flow.addStretch()

        # ── 左侧：流程库列表（双击 = 加入任务栏，右键管理，拖到组标题 = 换分类）──
        self.flow_list = _FlowLibList()
        self.flow_list.setObjectName("flowList")
        self.flow_list.setFixedWidth(_SIDEBAR_WIDTH)
        # 行尾锁图标委托：点图标 = 上锁/解锁（不进右键菜单）
        self._lock_deleg = _FlowLockDelegate(self.flow_list)
        self._lock_deleg.toggled.connect(self._toggle_lock)
        self.flow_list.setItemDelegate(self._lock_deleg)
        self.flow_list.itemClicked.connect(self._on_list_clicked)
        self.flow_list.itemDoubleClicked.connect(self._on_list_double_clicked)
        self.flow_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.flow_list.customContextMenuRequested.connect(self._on_list_context_menu)
        # 拖流程落到组标题 → 与右键「移到分类」同一条落账路（绝不另写一套）
        self.flow_list.flowDropped.connect(self._move_flow_to)
        # 单击组标题行 → 折叠/展开该分类（列表 mousePressEvent 发，见 _FlowLibList）
        self.flow_list.catToggled.connect(self._toggle_category)
        sidebar = QWidget()
        side_lay = QVBoxLayout(sidebar)
        side_lay.setContentsMargins(0, 0, 8, 0)
        side_lay.setSpacing(4)
        self.lbl_flow_title = QLabel("流程库")
        self.lbl_flow_title.setObjectName("statStrong")
        self.lbl_flow_title.setToolTip(_FLOWLIB_HELP)
        _title_row = QHBoxLayout()
        _title_row.setSpacing(4)
        _title_row.addWidget(self.lbl_flow_title)
        _title_row.addWidget(make_help_badge(_FLOWLIB_HELP))
        _title_row.addStretch(1)   # stretch 放徽章后：整组靠左，徽章贴着名字
        side_lay.addLayout(_title_row)
        side_lay.addWidget(self.flow_list, stretch=1)
        # 底部常驻导入/导出按钮（2026-10-04 用户反馈：右键空白入口太隐蔽找不到）。
        # 与右键菜单两项共用同一条 _on_export_flows/_on_import_flows，不另写逻辑。
        _io_row = QHBoxLayout()
        _io_row.setSpacing(4)
        btn_export = QPushButton("导出")
        btn_export.setToolTip("全部流程连同被引用图片打包成 .zip（默认存到 项目/导出/流程/），\n"
                              "换电脑或传给装同款软件的朋友，对方导入即用")
        btn_export.clicked.connect(self._on_export_flows)
        btn_import = QPushButton("导入")
        btn_import.setToolTip("吃本程序导出的 .zip（含图）或 flows.json（纯流程），\n"
                              "同名流程会先问覆盖还是跳过")
        btn_import.clicked.connect(self._on_import_flows)
        _io_row.addWidget(btn_export)
        _io_row.addWidget(btn_import)
        side_lay.addLayout(_io_row)

        # ── 第二行：节点调色板下拉 + 画布缩放（同 bar_flow：窄栏折行不裁字）──
        bar_add = FlowLayout()
        self.btn_add_node = QPushButton("新增节点")
        self.node_menu = QMenu(self.btn_add_node)
        self.node_menu.setToolTipsVisible(True)  # QMenu 默认不显示项 tooltip，必须显式开
        self._add_acts = {}
        for group, types in NODE_GROUPS:
            if self._add_acts:
                self.node_menu.addSeparator()
            self.node_menu.addSection(group)
            for t in types:
                act = self.node_menu.addAction(TYPE_LABEL[t])
                act.setToolTip(NODE_HELP.get(t, TYPE_LABEL[t]))  # 悬停菜单项即看说明
                act.triggered.connect(lambda _=False, tt=t: self._add_to_main(tt))
                self._add_acts[t] = act
        self.btn_add_node.setMenu(self.node_menu)
        bar_add.addWidget(self.btn_add_node)
        bar_add.addWidget(make_help_badge(_ADDNODE_HELP))
        # 一键整理布局（分层拓扑，只挪坐标不改连线 → 运行行为零影响）
        self.btn_layout = QPushButton("整理布局")
        self.btn_layout.setToolTip(
            "按连线把所有节点自动摆整齐：执行链从左到右，同层的分支/并行链上下排开。\n"
            "只重排画布坐标，连线一条不动 → 执行顺序本来就只看连线，整理不影响运行。")
        self.btn_layout.clicked.connect(self._auto_layout)
        bar_add.addWidget(self.btn_layout)
        # ── 对齐/分布工具（Designer Form 菜单的画布版）：多选 ≥2 才有意义 ──
        # 图标 = 市面通用样式（内联 SVG，见 ui/icons.py layout_button）：
        # MDL2 没有垂直对齐/等距分布字形，同一排按钮不混两种来源。
        self._align_btns = {}
        for key, tip in (
                ("left",   "左对齐：选中节点的最左边拉齐"),
                ("hmid",   "水平居中：选中节点横向中心线拉齐"),
                ("right",  "右对齐：选中节点的最右边拉齐"),
                ("top",    "顶对齐：选中节点的最顶边拉齐"),
                ("vmid",   "垂直居中：选中节点纵向中心线拉齐"),
                ("bottom", "底对齐：选中节点的最底边拉齐"),
                ("hspread", "水平等距：选中节点横向间距拉匀（两端不动）"),
                ("vspread", "垂直等距：选中节点纵向间距拉匀（两端不动）")):
            b = icons.layout_button(key)
            b.setToolTip(tip)
            b.setEnabled(False)   # 选中数 <2 时禁用；_on_selection_changed 刷
            b.clicked.connect(lambda _=False, k=key: self._align_selection(k))
            bar_add.addWidget(b)
            self._align_btns[key] = b
        bar_add.addStretch()
        # 网格开关（Designer 画布同款点阵 + 吸附）：状态记 ui.*，重启保留
        self.chk_grid = QCheckBox("网格")
        self.chk_grid.setToolTip("画布点阵网格：节点摆位有参照，流程图更整齐")
        self.chk_grid.setChecked(bool(self.cfg.get("ui.flow_grid", True)))
        self.chk_snap = QCheckBox("吸附")
        self.chk_snap.setToolTip("拖动/方向键挪节点时吸到网格点上")
        self.chk_snap.setChecked(bool(self.cfg.get("ui.flow_snap", True)))
        bar_add.addWidget(self.chk_grid)
        bar_add.addWidget(self.chk_snap)
        # 缩放唯一入口是滚轮；这里只留「适应」一键把全部节点框回视野
        self.btn_zoom_fit = QPushButton("适应")
        self.btn_zoom_fit.setFixedWidth(52)
        bar_add.addWidget(self.btn_zoom_fit)

        # ── 画布 ──
        self.scene = FlowScene(show_grid=self.chk_grid.isChecked(),
                               snap_grid=self.chk_snap.isChecked())
        self.view = FlowCanvasView(self.scene)

        # ── 属性面板 ──
        self.props_panel = QWidget()
        self.props_layout = QVBoxLayout(self.props_panel)
        self.props_layout.setContentsMargins(6, 6, 6, 6)
        props_scroll = QScrollArea()
        props_scroll.setWidgetResizable(True)
        props_scroll.setWidget(self.props_panel)
        props_scroll.setFixedWidth(320)

        # ── 右列 = 便签（常驻顶部）+ 属性滚动区 ──
        # 便签不参与编译运行：纯给自己记「哪还没修、进行到哪」。
        # 锁上的流程照样能写便签——记欠账恰恰常发生在"看得到改不了"的时候。
        self.txt_note = QTextEdit()
        self.txt_note.setFixedHeight(150)
        self.txt_note.setPlaceholderText(
            "本流程便签：记「还要完善什么、进行到哪一步」，切流程自动保存。\n"
            "只是给自己看的备忘，不影响流程运行。")
        self.txt_note.textChanged.connect(self._note_edited)
        self._note_loading = False        # 换流程灌文本时防误触发自动保存
        self._note_timer = QTimer(self)   # 打字防抖：停手 600ms 才落账本
        self._note_timer.setSingleShot(True)
        self._note_timer.setInterval(600)
        self._note_timer.timeout.connect(self._save_note)
        lbl_note = QLabel("📝 流程便签")
        lbl_note.setObjectName("statStrong")
        note_row = QHBoxLayout()
        note_row.setSpacing(4)
        note_row.addWidget(lbl_note)
        note_row.addWidget(make_help_badge(
            "每条流程自己的备忘录：写「待办 / 进度 / 坑」，切流程自动保存，\n"
            "有内容的流程在左侧列表带 📝 角标。\n"
            "只是备忘，不写进流程、不影响挂机运行；上锁的流程也能写。"))
        note_row.addStretch(1)
        right_col = QWidget()
        right_col.setFixedWidth(320)
        rc_lay = QVBoxLayout(right_col)
        rc_lay.setContentsMargins(0, 0, 0, 0)
        rc_lay.setSpacing(4)
        rc_lay.addLayout(note_row)
        rc_lay.addWidget(self.txt_note)
        rc_lay.addWidget(props_scroll, 1)

        # ── 组装 ──
        content = QHBoxLayout()
        content.addWidget(sidebar, 0)
        content.addWidget(self.view, 1)
        content.addWidget(right_col, 0)
        content.setSpacing(8)

        # ── 编辑期校验条：把运行时的"缺图/成环/孤立"提前显示出来，测之前就能看见 ──
        self.lbl_valid = QLabel("")
        self.lbl_valid.setObjectName("flowValid")
        self.lbl_valid.setWordWrap(True)
        self.lbl_valid.setVisible(False)
        self.lbl_valid.setStyleSheet(
            f"font-size: 12px; color: {theme.WARN}; padding: 3px 8px;")
        self._val_timer = QTimer(self)      # 防抖：打字途中不逐键重扫
        self._val_timer.setSingleShot(True)
        self._val_timer.setInterval(400)
        self._val_timer.timeout.connect(self._refresh_validation)
        # 属性面板防抖：框选每动一下鼠标都甩 selectionChanged（实测实时发），
        # 逐发重建表单 = 卡顿；合并到"选中停下来"后一次（见 _on_selection_changed）
        self._sel_timer = QTimer(self)
        self._sel_timer.setSingleShot(True)
        self._sel_timer.setInterval(80)
        self._sel_timer.timeout.connect(self._apply_selection_props)

        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.addLayout(bar_flow)
        main.addLayout(bar_add)
        main.addLayout(content)
        main.addWidget(self.lbl_valid)

        # ── 信号 ──
        self.btn_save.clicked.connect(self._on_save)
        self.chk_autosave.toggled.connect(self._on_autosave_toggled)
        # activated = 从列表里选；editingFinished = 敲完回车或失焦（新建分类走这条）
        self.cmb_cat.activated.connect(self._on_category_changed)
        self.cmb_cat.lineEdit().editingFinished.connect(
            lambda: self._on_category_changed(-1))
        self.btn_test.clicked.connect(self.test_requested)
        self.btn_multi_test.clicked.connect(self.multi_test_requested)
        self.btn_test_one.clicked.connect(self._on_test_one)
        self.btn_test_from.clicked.connect(self._on_test_from)
        self.btn_test_stop.clicked.connect(self.test_stop_requested)
        self.scene.selectionChanged.connect(self._on_selection_changed)
        # 连线/删除回调（画布交互 → 面板数据）
        self.view.set_connect_callback(self._on_edge_connect)
        self.view.set_reconnect_callback(self._on_edge_reconnect)
        self.view.set_delete_callback(self._on_delete_from_canvas)
        self.view.set_add_at_callback(self._on_add_at)
        self.view.set_readonly_callback(self._is_locked)
        self.btn_zoom_fit.clicked.connect(self.view.fit_scene)

        # 画布快捷键走 view.keyPressEvent → 回调分流（见 _on_canvas_shortcut）。
        # 不用 QShortcut：那会在整个面板范围内截走 Ctrl+C/V/Z，属性面板里
        # 文本框的原生复制粘贴撤销就全废了；画布只有自己有焦点才收键。
        self.view.set_shortcut_callback(self._on_canvas_shortcut)
        self.view.set_nudge_callback(self._nudge_selection)
        self.chk_grid.toggled.connect(
            lambda on: (setattr(self.scene, "show_grid", on),
                        self.scene.update(),
                        self.cfg.set("ui.flow_grid", on)))
        self.chk_snap.toggled.connect(
            lambda on: (setattr(self.scene, "snap_grid", on),
                        self.cfg.set("ui.flow_snap", on)))

        self._rebuild_props(None)

    # ── 外壳接线 ──
    def attach_view(self, view):
        """工作台建好共用预览后调用。click_target_set（取坐标）由外壳按
        view.click_owner=="pick" 路由进来；multi_point_clicked（多点取色记点）
        只有本面板会发起，外壳直连 route_multi_point。"""
        self._view = view

    def route_click(self, x, y):
        """外壳路由：预览上的一次点选坐标（编流程取坐标专用）。"""
        self._on_pick_click(x, y)

    def begin_roi_pick(self, target, owner=None, key="roi"):
        """发起「在预览上框选识别区域」（server_pick 节点 ROI / 判断节点
        文字识别 ROI 共用）。target = 回填 roi 的字典（动作节点传自身，
        条件面板传 cond）；owner = 成框后要重建属性表单的所属节点
        （cond 场景传宿主节点，缺省 = target 本身）；key = 回填用的字典键
        （留口子给"一个宿主多个区域"的将来用法，现仅 roi）。
        正常送达路 = 在共用预览登记一次性回调（成框直接回 route_roi）；
        view.roi_owner="flow" 保留给不支持回调的旧壳/测试假视图走信号兜底路。"""
        if self._view is None:
            self._set_status("预览未就绪")
            return
        self._roi_pick_node = target
        self._roi_pick_owner = owner if owner is not None else target
        self._roi_pick_key = key
        setter = getattr(self._view, "set_roi_pick_callback", None)
        if setter is not None:
            setter(self.route_roi)   # 有回调时 ImageView 不再发 roi_drawn，绝不双份
        self._view.draw_mode = "roi"
        self._view.roi_owner = "flow"
        self._set_status("识别区域：在左侧画面点两下框选（右键取消）")

    def route_roi(self, x, y, w, h):
        """外壳路由：限域框选完成 → 回填 roi 并重建属性表单。"""
        target = self._roi_pick_node
        owner = self._roi_pick_owner
        key = getattr(self, "_roi_pick_key", "roi")
        self._roi_pick_node = self._roi_pick_owner = None
        self._roi_pick_key = "roi"
        if target is None:
            return
        target[key] = [x, y, x + w, y + h]
        self._mark_dirty()
        self._rebuild_props(owner)  # 表单 spinbox 以数据为真相源，重建即回填可见
        self._set_status(f"识别区域已锁定：({x},{y})-({x + w},{y + h})")

    def _begin_multi_pick(self, node, cond):
        """发起「在预览上点选记色」（多点取色条件）：进 multi_point 点选态，
        归属绑死这一次会话的 cond + 宿主节点；预览上每单击记一点，右键结束。
        画面由外壳 tick 冻结（draw_mode 在冻结名单里），瞄准的像素不会半路变色。"""
        if self._view is None:
            self._set_status("预览未就绪")
            return
        self._multi_pick_cond = cond
        self._multi_pick_node = node
        self._view.draw_mode = "multi_point"
        self._sync_multi_marks()
        self._set_status("多点取色：在左侧画面逐点单击记色（放大镜确认颜色再点），右键结束")

    def _sync_multi_marks(self):
        """预览上的洋红小十字 = 本条件已记的点（点选态期间的实时反馈）。"""
        if self._view is None:
            return
        cond = self._multi_pick_cond or {}
        self._view.multi_marks = [(int(p[0]), int(p[1]))
                                  for p in cond.get("points", [])
                                  if len(p) >= 2]
        self._view.update()

    def _end_multi_pick(self):
        """结束点选记色会话：退预览模式、清归属和预览标记
        （右键结束走预览侧，这里管面板侧）。预览只在会话真活着时才碰——
        冒烟借方法时塞的是没有这些字段的假 view，getattr 全防御。"""
        self._multi_pick_cond = None
        self._multi_pick_node = None
        v = self._view
        if v is None:
            return
        in_mode = getattr(v, "draw_mode", None) == "multi_point"
        marks = getattr(v, "multi_marks", None)
        if not (in_mode or marks):
            return
        if in_mode:
            v.draw_mode = v.default_mode
        if marks:
            v.multi_marks = []
        v.update()

    def route_multi_point(self, x, y):
        """外壳路由：点选态下预览上的一次单击 → 现场采色、追加进归属 cond。"""
        cond = self._multi_pick_cond
        node = self._multi_pick_node
        if cond is None or self._view is None:
            return
        if cond.get("kind") != "multi_color":
            # 表单里条件类型被换掉了：这次会话作废，别把点记进别的条件
            self._end_multi_pick()
            return
        img = self._view._img
        if img is None:
            return
        # _img 可能是原生帧：基准坐标 ×_magn 换算到物理像素再索引
        m = self._view._magn
        xm, ym = int(x * m), int(y * m)
        if not (0 <= xm < img.shape[1] and 0 <= ym < img.shape[0]):
            return
        b, g, r = (int(v) for v in img[ym, xm, :3])
        cond.setdefault("points", []).append([x, y, f"{r:02X}{g:02X}{b:02X}"])
        self._mark_dirty()
        self._sync_multi_marks()
        self._rebuild_props(node)  # 表单以数据为真相源，重建即刷新点列表

    def on_templates_changed(self):
        """外壳转交「模板库变了」（保存/删除/改名/导入）：面板常驻可见，
        直接重建当前选中节点的表单 —— 节点 dict 是真相源，_rebuild_props
        从 list_templates() 现取下拉项，新图立刻选得到。"""
        # 图可能被删/改名/重采覆盖，缩略图整表作废；画布上所有节点跟着刷一遍
        clear_thumb_cache()
        self.scene.update()
        item = self._selected_item()
        self._rebuild_props(item.node if item else None)
        # 图可能被删/改名：结构没变、_rebuild_scene 不会跑，缺图清单要重算
        self._schedule_validation()

    def apply_capture_result(self, category: str, name: str) -> bool:
        """采完一张图 → 自动回填到当初点「去采集」的节点/分支条件。

        返回 False = 没有回程票（用户不是从流程面板过去的，比如自己直接来采）。
        三栏同屏无需"切回编流程"：回填后表单就地重建，下拉立刻选得到新图。
        画布要跟着重绘 —— 节点方块会画出这张新图的缩略图。"""
        ticket = self._capture_return
        if ticket is None:
            return False
        self._capture_return = None
        target = ticket["cond"] if ticket.get("cond") is not None else ticket["node"]
        target["category"] = category
        target["name"] = name
        node = ticket["node"]
        # 采的是一张全新的图：缩略图缓存整表作废、画布立刻重绘。
        # （templates_changed 通道也会做同样的事，但两个信号先后顺序不固定，
        # 这里自带一份，回填后的画面不依赖通道先到。）
        clear_thumb_cache()
        self.scene.update()
        self._mark_dirty()
        # 显式重建当前选中节点的表单（重选不一定触发 selectionChanged 信号）
        self._reselect_node(node)
        self._rebuild_props(node)
        desc = node.get("label") or TYPE_LABEL.get(node.get("type", ""), "?")
        tail = "" if self.chk_autosave.isChecked() else "，记得保存流程"
        self._set_status(f"✓ 已把「{category}/{name}」回填到节点「{desc}」{tail}")
        return True

    def confirm_discard(self) -> bool:
        """外壳关窗/切流程前问一句：有未保存修改时让用户确认。返回 True=可继续。"""
        if not self._dirty:
            return True
        r = QMessageBox.question(self, "未保存", "流程尚未保存，修改将丢失。继续？",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return r == QMessageBox.Yes

    def _set_status(self, text: str):
        self.status_message.emit(text)

    def set_test_running(self, running: bool, keep_marks: bool = False):
        """试运行期间锁编辑：跑的是内存快照，边跑边改画布只会产生
        「改了没生效」的错觉；跑完（done/failed/stopped）由外壳调回 False。"""
        for w in (self.view, self.props_panel):
            w.setEnabled(not running)
        self.flow_list.setEnabled(not running)
        self.btn_add_node.setEnabled(not running)
        self.btn_test.setEnabled(not running)
        self.btn_multi_test.setEnabled(not running)  # 多开测试同样锁：防两个 worker 撞同一台
        self.btn_test_stop.setEnabled(running)
        self.btn_save.setEnabled(not running)
        # 试跑期间「测此节点」「从此节点开始测」必须锁死：不锁的话再点一发 = 两个线程抢同一实例
        if running:
            self.btn_test_one.setEnabled(False)
            self.btn_test_from.setEnabled(False)
        else:
            self._refresh_test_one_btn()   # 按当前选中态恢复
        self.chk_grid.setEnabled(not running)
        self.chk_snap.setEnabled(not running)
        self._update_align_buttons()   # 对齐/分布按钮随画布一起锁/恢复
        if running:
            # 整流程开测抹掉上轮角标；单节点测试不清（keep_marks）——
            # 全链跑完后逐个修节点单测，只刷新被测那个，其余保留全局图。
            if not keep_marks:
                self._test_cache.clear()
            self._apply_test_marks()

    def apply_test_results(self, index: int, states: dict):
        """试运行收尾回填（外壳转交 node_states 信号）：把每个节点这次跑到
        的终态盖成右上角角标（✓ok / ✗failed / …pending）。只改视觉、不标脏、
        不进存盘 —— 角标是"这一次跑"的临时读数，切流程/重开自动作废。
        合并式更新：单测只回一个节点，其余角标原样保留。"""
        self._test_cache.update(states or {})
        self._apply_test_marks()

    def _apply_test_marks(self):
        """按 _test_cache 重刷画布上所有节点的角标（增删改后统一入口）。"""
        items = getattr(self, "_node_items", {})
        for nid, item in items.items():
            item._test_mark = self._test_cache.get(nid)
            item.update()

    # ── 流程列表 / 加载 / 保存 ──
    def _flows(self) -> dict:
        return self.cfg.get("custom_flows", {}) or {}

    def _notes(self) -> dict:
        return self.cfg.get("flow_notes", {}) or {}

    def _note_edited(self):
        if not self._note_loading:
            self._note_timer.start()

    def _save_note(self):
        """把编辑框内容写进 flow_notes[当前流程]：空文除键（不留空壳）。
        落盘走 cfg.set 自带 1 秒防抖，这里只管改账本 + 刷列表角标。"""
        name = self.flow_name
        if not name:
            return
        text = self.txt_note.toPlainText().rstrip()
        notes = dict(self._notes())
        old_had = bool((notes.get(name) or "").strip())
        if text.strip():
            notes[name] = text
        else:
            notes.pop(name, None)
        self.cfg.set("flow_notes", notes)
        # 角标翻动只点改那一行的文本 —— 绝不整表重建：_reload_flow 换页
        # 流程 = 先冲旧账再读 currentItem，整表重建会把选中按"还没换的旧
        # flow_name"恢复回去，后面读回来的还是旧流程（实测冒烟抓到的坑）。
        if old_had != bool(text.strip()):
            for i in range(self.flow_list.count()):
                it = self.flow_list.item(i)
                if it.data(Qt.UserRole) == name:
                    it.setText(name + (" 📝" if text.strip() else ""))
                    break

    def _load_note(self):
        """切流程时灌入该流程便签（_note_loading 闸防灌文本误触发自动保存）。"""
        self._note_loading = True
        try:
            self.txt_note.setPlainText(self._notes().get(self.flow_name or "", ""))
        finally:
            self._note_loading = False

    def _refresh_flow_list(self):
        """重建左侧流程库列表（按分类分组）。不触发切换事件，选中项靠
        flow_name 找回；找不到（流程被删）退回第一项。"""
        self.flow_list.blockSignals(True)
        self.flow_list.clear()
        self._lock_deleg.set_locked(self._locked_list())   # 行尾图标数据源
        # 折叠状态（收拢的分类名集合，记 ui.flow_collapsed，重启保留）
        collapsed = self._collapsed_cats()
        groups = template_deps.flow_category_groups()
        for cat, members in groups:
            # 空组也显示（分类是本体不是流程的附属：拖空/新建没塞人的组都得看得见管得着）
            folded = cat in collapsed
            # 文本不带箭头字符（▼/▶ 字形大小不一、且缺字形机器会被 fallback 换掉）：
            # 箭头由 _FlowLockDelegate 手绘等大三角（见 _FLOW_FOLDED_ROLE 段注释），
            # 折叠态挂 role；文本只留「分类名（N）」。
            hdr = QListWidgetItem(f"{cat}（{len(members)}）")
            hdr.setFlags(Qt.NoItemFlags)   # 组标题不可选不可点（点它走 mousePressEvent 折叠）
            # 分类名数据键：右键组标题弹分类菜单、拖拽落点、单击折叠都靠它认行
            hdr.setData(Qt.UserRole + 1, cat)
            hdr.setData(_FLOW_FOLDED_ROLE, True if folded else False)
            hdr.setForeground(QColor("#9aa0a6"))
            hdr.setToolTip(f"分组「{cat}」：{len(members)} 条流程\n"
                           f"单击 = {'展开' if folded else '收起'}这个分类\n"
                           f"右键组标题 = 新建/重命名/删除分类")
            self.flow_list.addItem(hdr)
            if folded:
                continue   # 收起的分类：成员行不渲染（单击标题即再展开）
            locked = set(self._locked_list())
            notes = self._notes()
            for name in members:
                note = (notes.get(name) or "").strip()
                it = QListWidgetItem(name + (" 📝" if note else ""))
                it.setData(Qt.UserRole, name)
                # 所在组标记：拖拽落点扩到整组区后，流程行也认落点（判定见 _drop_cat_name）
                it.setData(Qt.UserRole + 3, cat)
                it.setToolTip(f"双击 = 生成任务进主界面任务栏\n"
                              f"右键 = 改名/删除/移动分类\n"
                              f"拖到目标分类的任意一行 = 换分类（拖进「未分类」组 = 取消分类）\n"
                              f"行尾锁图标 = 上锁/解锁"
                              + ("\n已上锁：不能修改/改名/删除" if name in locked else "")
                              + (f"\n📝 {note[:60]}" + ("…" if len(note) > 60 else "")
                                 if note else ""))
                self.flow_list.addItem(it)
        # 恢复选中：按 flow_name 找同名条目。找不着 = 刚删了流程，
        # 退到第一个流程条目（组标题跳过）。
        idx = -1
        for i in range(self.flow_list.count()):
            it = self.flow_list.item(i)
            if it.data(Qt.UserRole) == self.flow_name:
                idx = i
                break
        if idx < 0:
            for i in range(self.flow_list.count()):
                if self.flow_list.item(i).data(Qt.UserRole):
                    idx = i
                    break
        if idx >= 0:
            self.flow_list.setCurrentRow(idx)
        self.flow_list.blockSignals(False)
        self.lbl_flow_title.setText(f"流程库（{sum(len(m) for _, m in groups)}）")

    def _reload_flow(self):
        # 上一条便签若有 600ms 防抖没落地，趁 flow_name 还是旧名冲账。
        # ⚠ 只在防抖真挂着时才存：改名/删除路径自己先处理过便签账，
        # 这里无脑再存一次会把已迁移/已除账的内容按旧名写回（孤儿键复活）。
        if self._note_timer.isActive():
            self._note_timer.stop()
            self._save_note()
        # 相机按流程各存各的（病根：整面板只有一条共用 view，平移=改滚动条、
        # 缩放=改矩阵，scene.clear() 都不碰它们 → 在 A 里平移一下，切到 B 时
        # B 借用了 A 的镜头：节点集体偏一边，坐标离原点远的干脆"消失不见"）。
        # _view_flow 记录"此刻画布上摆的是哪条流程的节点"——不能用 flow_name
        # 判：_select_flow 会先把 flow_name 改成新流程才走到这里。
        if self._view_flow:
            self._cameras[self._view_flow] = self.view.get_camera()
        cur = self.flow_list.currentItem()
        self.flow_name = cur.data(Qt.UserRole) if cur is not None else ""
        if not self.flow_name:   # 列表空/选中了组标题：退回第一项流程
            for i in range(self.flow_list.count()):
                it = self.flow_list.item(i)
                if it.data(Qt.UserRole):
                    self.flow_name = it.data(Qt.UserRole)
                    self.flow_list.setCurrentRow(i)
                    break
        self._refresh_category_combo()
        self._load_note()          # 便签跟着流程走（换人换纸）
        # v1 树自动迁移到 v2 图（ensure_graph 无 Qt，编辑器共用）；
        # 旧「选区选服 pick_pair」节点也在这里一次性换算成
        # [点文字→等待→点文字] 三节点 —— 只改内存里的图，config 原样留着，
        # 用户下一次保存才落盘（换算幂等，不存也无害）。
        raw = self._flows().get(self.flow_name, {})
        # 判"有没有拆过"借用引擎同一只游走函数（含循环体 steps），口径不分叉
        had_pair = bool(raw) and _has_pair(raw.get("nodes") or raw.get("steps"))
        self.graph = ensure_graph(raw) if raw else {"version": 2, "nodes": [], "edges": []}
        self._dirty = False
        if had_pair:
            self._set_status("旧「选区选服」节点已换算成 [点区 → 等待 → 点服] "
                             "三个节点：两块框选、两步间隔全在明面上，保存后生效")
        # 看门狗控件跟着图走：blockSignals 防 setValue 触发回调把刚加载的流程标脏
        self.spn_watchdog.blockSignals(True)
        self.spn_watchdog.setValue(int(self.graph.get("watchdog_sec", 0) or 0))
        self.spn_watchdog.blockSignals(False)
        # 撤销栈是"这份图"的历史，换流程必须清空，否则 Ctrl+Z 把别的流程盖进来
        self._undo.clear()
        self._redo.clear()
        # 试运行角标是"那次跑"的产物，换流程必须作废，否则旧结果贴在新画布上
        self._test_cache.clear()
        # 换流程 = 旧流程节点树整个作废，留着回程票会把新图填进已经不在画布上的 dict
        self._capture_return = None
        self._watch_sel_rows.clear()   # 监视行选中记忆同理：旧图的 rows 不再回位
        self._rebuild_scene()   # 内部排校验 + 按锁态刷只读（换流程必经）
        # 节点摆好了，灌回这条流程自己的相机：见过就回到上次的视角；
        # 本次会话第一次打开则框住它自己的内容（fit），绝不带别人的镜头——
        # 节点离原点远的流程复位到原点照样"看起来消失了"，框住才不会
        cam = self._cameras.get(self.flow_name)
        if cam:
            self.view.set_camera(*cam)
        elif self.view.content_visible():
            pass   # 切换时内容就在当前视口里 → 镜头原地不动（2026-10-06 用户
                   # 拍板：每次切换都"自动跳转"烦人；同布局流程切来切去不该跳）
        else:
            # 内容整个在视口外才框住 —— 10-05 的病根（借镜头→节点消失不见）仍守住
            self.view.reset_camera()
            self.view.fit_scene()
        self._view_flow = self.flow_name
        # 本会话首次点开流程：延一拍框住内容（用户诉求 2026-10-11，见 _fit_first_time）。
        # 只对"没有自己相机"的流程生效（有相机 = 用户之前看过，尊重其视角不打扰）。
        if not cam:
            self._fit_first_time()

    # ── 当前流程的用途分类（工具栏「分类」下拉）──
    def _refresh_category_combo(self):
        """重灌分类候选（全部已登记分类 + 未分类兜底），选中项 = 当前流程所属。
        只走 blockSignals：程序刷新不能触发写盘，否则切流程就把分类刷一遍。"""
        cur = self.flow_name
        own = (self.cfg.get("flow_categories", {}) or {}).get(cur) if cur else None
        cats = [c for c, ms in template_deps.flow_category_groups()
                if c != UNCLASSIFIED]   # 空分类也进候选（分类本体可见可指派）
        self.cmb_cat.blockSignals(True)
        self.cmb_cat.clear()
        self.cmb_cat.addItem(UNCLASSIFIED, "")
        for c in cats:
            self.cmb_cat.addItem(c, c)
        idx = self.cmb_cat.findData(own or "")
        self.cmb_cat.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_cat.blockSignals(False)
        self.cmb_cat.setEnabled(bool(cur))

    def _on_category_changed(self, idx: int = -1):
        """idx≥0 = 从列表选的（用信号给的索引，别信控件自己的 currentText ——
        activated 先于 currentIndex 更新发射）；idx=-1 = 编辑框回车/失焦。"""
        name = self.flow_name
        if not name:
            self._refresh_category_combo()
            return
        new = (self.cmb_cat.itemData(idx) if idx >= 0
               else self.cmb_cat.currentText()) or ""
        new = new.strip()
        # 编辑框清空（回车/失焦）= 误触，不动；取消分类请从下拉显式选「未分类」
        if idx < 0 and not new:
            self._refresh_category_combo()
            return
        new = new or UNCLASSIFIED
        old = (self.cfg.get("flow_categories", {}) or {}).get(name) or UNCLASSIFIED
        if not new or new == old:
            self._refresh_category_combo()
            return
        self._write_flow_category(name, new)
        self.cfg.save()
        self._refresh_category_combo()
        self._set_status(f"✓ 流程「{name}」分类 → {new}")

    # ── 左侧流程库列表交互 ──
    def _on_list_clicked(self, item: QListWidgetItem):
        """单击真流程 → 切画布。右键（给菜单用）也会触发本信号，先滤掉；
        切画布的确认逻辑走 _select_flow，避免右键弹菜单前先弹未保存框。
        组标题的折叠走 _FlowLibList.mousePressEvent + catToggled，不经本信号。"""
        if item is None or not item.data(Qt.UserRole):
            return
        if QGuiApplication.mouseButtons() & Qt.RightButton:
            return   # 右键交个 _on_list_context_menu，这里不动作
        self._select_flow(item.data(Qt.UserRole))

    def _collapsed_cats(self) -> set:
        """当前被折叠（收拢）的分类名集合（读 ui.flow_collapsed，缺省=全展开）。"""
        try:
            return set(self.cfg.get("ui.flow_collapsed", []) or [])
        except Exception:
            return set()

    def _toggle_category(self, cat: str):
        """点组标题：折叠/展开该分类 → 落 ui.flow_collapsed → 重建列表。
        重建后不动选中流程（_refresh_flow_list 内部按 flow_name 找回选中）。"""
        collapsed = self._collapsed_cats()
        if cat in collapsed:
            collapsed.discard(cat)
        else:
            collapsed.add(cat)
        # 稳定排序存盘：同一次折叠集合内容一致 → 不因 set 顺序抖动反复写盘
        self.cfg.set("ui.flow_collapsed", sorted(collapsed))
        self.cfg.save()
        self._refresh_flow_list()

    def _fit_first_time(self):
        """本会话第一次"看到流程内容"时框住（用户诉求 2026-10-11：打开软件后
        第一次点开流程老得手动点「适应」）。只对"没有自己相机"的流程生效
        （有相机 = 用户之前看过，尊重其视角不打扰）；标记 _first_open 只消费一次，
        消费后切换走常规逻辑（见过的回相机、可见的不跳）。

        ⚠ 未显示 / 视口没铺开时不框、也不销标记：面板构造期 viewport 虽已有个默认
        尺寸（~638×478）但还没真实布局，此时 fit 用的尺寸随后会被 resize 改掉 →
        框歪/无效，且标记被白白消费（用户实拍：锁定流程打开没自适应，根因即此）。
        故必须等 isVisible 且视口铺开，才框、才销标记。"""
        if not self._first_open:
            return
        if not self.isVisible():
            return   # 面板未显示（构造期/未切到工作台）：不框、不销标记
        vp = self.view.viewport()
        if vp.width() < 50 or vp.height() < 50:
            return   # 视口没铺开：同上，等真正显示后点开再框
        if not any(isinstance(it, FlowNodeItem) for it in self.scene.items()):
            self._first_open = False   # 空画布无需框，也算"看过了"
            return
        self._first_open = False
        # 延一拍：此刻可能刚切完场景/刚显示，布局未落定，等视口尺寸稳了再框
        QTimer.singleShot(0, self.view.fit_scene)

    def showEvent(self, event):
        """首次真正显示（工作台切到流程编排页 / 面板首次可见）：此刻视口才有
        真实尺寸，构造期排的那次 fit 无效。若"首开标记"还在（说明用户还没点过
        任何流程、当前是自动载入的那条），补一次框住——用户诉求 2026-10-11：
        打开软件后第一次看到流程（含被锁住的、只需看的流程）就该是自适应的。"""
        super().showEvent(event)
        if self._first_open:
            QTimer.singleShot(0, self._fit_first_time)

    def _on_list_double_clicked(self, item: QListWidgetItem):
        """双击流程 = 生成一条任务进主界面任务栏。双击的是当前流程且还有未保存
        改动就先存盘再生成 —— 和测试按钮同一条纪律：入任务栏的得是画布当前内容。
        （双击别的流程时它的内容本来就是上一次保存的，直接发。）"""
        if item is not None and item.data(Qt.UserRole):
            name = item.data(Qt.UserRole)
            if name == self.flow_name and self._dirty:
                self._on_save(quiet=True)
            self.add_to_queue.emit(name)

    def _emit_flow_saved(self, name: str, old: str = ""):
        """flow_saved 唯一出口 helper。吞 RuntimeError：冒烟常用
        FlowPanel.__new__ 借骨架（不跑 __init__、信号源没有 C++ 对象），
        真界面里面板必是活控件永不触发。"""
        try:
            self.flow_saved.emit(name, old)
        except RuntimeError:
            pass

    def edit_flow(self, name: str) -> bool:
        """对外入口（主界面任务清单双击「查看流程」）：把指定流程载入画布。
        已是当前流程=直接满足；有未保存修改照走丢弃确认（与列表单击同一道闸）。"""
        if self.flow_name == name:
            return True
        return self._select_flow(name)

    def _select_flow(self, name: str) -> bool:
        """把当前流程切成 name（单击 / 右键「编辑」/ 右键删改前调）。
        有未保存修改先问；用户拒绝 → 还原列表选中并返回 False。"""
        if name == self.flow_name:
            # 用户点开的就是构造时自动载入的那条：同名早退不给 fit 机会，补一次
            # 首次框住（构造时 viewport 还没尺寸、fit 无效；用户诉求 2026-10-11：
            # 打开软件第一次点开流程老得手动点「适应」）。
            self._fit_first_time()
            return True
        if self._dirty:
            r = QMessageBox.question(self, "未保存", "当前流程尚未保存，切换将丢失修改。继续？",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                self._refresh_flow_list()  # 还原列表选中
                return False
        self.flow_name = name
        # ⚠ 2026-10-07 修"点流程库随机位移"（用户实拍：点新手魂灵任务，列表
        # 往上滑、封号突破跑到鼠标位）：普通单击绝不再整表重建——鼠标按下时
        # 点中行已是 currentItem（高亮就位），重建反而把滚动条归零后由
        # setCurrentRow 把选中行滚到视口边缘，鼠标底下跳出别的行；且重建后
        # 滚动条范围延迟刷新，同步恢复原位会被钳掉（实测救不回）。
        # 只有"列表当前选中不是这条"（edit_flow 外部入口/右键编辑等）才重建同步高亮。
        cur = self.flow_list.currentItem()
        if cur is None or cur.data(Qt.UserRole) != name:
            self._refresh_flow_list()
        self._reload_flow()
        return True

    def _on_list_context_menu(self, pos):
        item = self.flow_list.itemAt(pos)
        menu = QMenu(self)
        name = item.data(Qt.UserRole) if item is not None else None
        if name is not None:
            menu.addAction("编辑", lambda _=False: self._select_flow(name))
            menu.addAction("加入任务栏", lambda _=False: self.add_to_queue.emit(name))
            menu.addSeparator()
            # 上锁/解锁不走菜单：每行行尾常驻锁图标，点一下就切（见 _FlowLockDelegate）
            lk = name in self._locked_list()
            a_ren = menu.addAction("改名…", lambda _=False: self._rename_flow(name))
            a_del = menu.addAction("删除", lambda _=False: self._delete_flow(name))
            if lk:   # 锁住 → 改名/删除灰掉（灰而不可点，比点了弹拒绝更省事）
                for a in (a_ren, a_del):
                    a.setEnabled(False)
                    a.setToolTip("流程已上锁，点行尾锁图标解锁")
            # 移到分类子菜单（与主界面侧栏同一个分类账本）
            sub = menu.addMenu("移到分类")
            for c, _ms in template_deps.flow_category_groups():
                sub.addAction(c, lambda _=False, cc=c: self._move_flow_to(name, cc))
        elif item is not None and item.data(Qt.UserRole + 1):
            # 右键打到组标题 = 分类本体管理。「未分类」是伪分类（兜底组，
            # 账本里没有它的记录），改名/删除无从谈起，只给「新建分类…」
            cat = item.data(Qt.UserRole + 1)
            menu.addAction("新建分类…", lambda _=False: self._new_category())
            if cat != UNCLASSIFIED:
                menu.addAction(f"重命名「{cat}」…",
                               lambda _=False, c=cat: self._rename_category(c))
                menu.addAction(f"删除分类「{cat}」",
                               lambda _=False, c=cat: self._delete_category(c))
        menu.addAction("新建流程…", lambda _=False: self._on_new_flow(_drop_cat_name(item)))
        if item is None:
            # 空白处右键 = 整库进出（换电脑/传给朋友，核心逻辑全在 core/flow_pack）
            menu.addSeparator()
            menu.addAction("导出全部流程…", self._on_export_flows)
            menu.addAction("导入流程…", self._on_import_flows)
        menu.exec(self.flow_list.mapToGlobal(pos))

    def _rename_flow(self, name: str):
        """右键改名：先切到该流程（带未保存确认），逻辑复用 _on_rename_flow"""
        if name == self.flow_name or self._select_flow(name):
            self._on_rename_flow()

    def _delete_flow(self, name: str):
        """右键删除：先切到该流程（带未保存确认），逻辑复用 _on_del_flow。
        真删掉了由 _on_del_flow 自己发 flow_saved（右键/底部按钮两条路同一个口，
        以前底部删除不发信号、只有这里发，事件源漏一半）。"""
        if name == self.flow_name or self._select_flow(name):
            if self.flow_name:   # _select_flow 切成功也可能是流程已被别的动作删掉
                self._on_del_flow()

    def _write_flow_category(self, name: str, cat: str):
        """把流程 name 的分类标签写成 cat；空/「未分类」= 销标签（回落未分类）。
        只动分类账本两键（flow_categories/flow_category_order），不碰流程本体、
        不刷列表不落盘（调用方自行收尾）。移到分类/工具栏下拉/新建流程三处共用，
        口径只此一份（此前三处各写一套，必漂移）。"""
        cat = (cat or "").strip()
        cat_map = dict(self.cfg.get("flow_categories", {}) or {})
        order = [c for c in (self.cfg.get("flow_category_order", []) or []) if c]
        if not cat or cat == UNCLASSIFIED:
            cat_map.pop(name, None)
        else:
            cat_map[name] = cat
            if cat not in order:
                order.append(cat)   # 分类首次启用即登记，侧栏分组同步出现
        self.cfg.set("flow_categories", cat_map)
        self.cfg.set("flow_category_order", order)

    def _move_flow_to(self, name: str, cat: str):
        """右键移到分类：改分类账本 + 落盘 + 重建列表（画布内容不动）。"""
        self._write_flow_category(name, cat)
        self.cfg.save()
        self._refresh_flow_list()
        self._set_status(f"✓ 流程「{name}」分类 → {cat}")

    # ── 导出/导入（整库换电脑 / 传朋友；文件活全在 core/flow_pack）──
    def _on_export_flows(self):
        """底部「导出」按钮 / 右键空白同项：config 三键全量 +（可选）被引用模板，
        打成一个 .zip。弹窗只收路径和问一句含不含图，语义不在这层。"""
        if not self._flows():
            self._set_status("✗ 当前没有流程，无需导出")
            return
        # 默认落进项目「导出/流程/」（用户拍板：导出一律进项目对应中文文件夹）
        path, _ = QFileDialog.getSaveFileName(self, "导出全部流程",
                                              flow_pack.default_path("流程", "zip"),
                                              "压缩包 (*.zip)")
        if not path:
            return
        with_tpl = self._ask_with_templates()
        if with_tpl is None:
            return
        try:
            r = flow_pack.export_pack(path, with_templates=with_tpl)
        except OSError as e:
            self._set_status(f"✗ 导出失败：{e}")
            return
        msg = f"✓ 已导出 {r['flows']} 条流程 → {path}"
        if with_tpl:
            msg += f"，打进模板 {r['templates']} 张"
            if r["missing_png"]:
                msg += f"（{len(r['missing_png'])} 张账本有记录但图文件已丢，没法带上）"
        self._set_status(msg)

    def _on_import_flows(self):
        """底部「导入」按钮 / 右键空白同项：吃 .zip / 纯 .json。同名流程一次性问
        （覆盖全部/跳过全部/取消），落账后走既有重建链路。"""
        # 默认开在「导出/流程/」：自己导的包回头也从这儿挑
        path, _ = QFileDialog.getOpenFileName(self, "导入流程",
                                              str(flow_pack.export_dir("流程")),
                                              "流程文件 (*.zip *.json)")
        if not path:
            return
        try:
            info = flow_pack.peek_pack(path)
        except (ValueError, OSError) as e:
            self._set_status(f"✗ 打开失败：{e}")
            return
        if not info["flows"]:
            self._set_status("✗ 包里没有流程")
            return
        overwrite = True
        dups = [n for n in info["flows"] if n in self._flows()]
        if dups:
            choice = self._ask_conflict(dups)
            if choice is None:
                return
            overwrite = choice
        try:
            r = flow_pack.import_pack(path, overwrite=overwrite)
        except (ValueError, OSError) as e:
            self._set_status(f"✗ 导入失败：{e}")
            return
        self._refresh_flow_list()
        self._refresh_category_combo()
        self._emit_flow_saved("", "")   # 集合可能变了：让主窗口按对账剔死项（无旧名可换）
        if r["templates_merged"]:
            # 新图落账本：缩略图/缺图徽标都是旧快照，走既有"模板库变了"链路
            clear_thumb_cache()
            self.scene.update()
            self._schedule_validation()
        if self.flow_name in dups and overwrite:
            # 当前画布这条被包覆盖了：借 _select_flow 走「未保存确认→重载」。
            # 先把名字清空骗它"这是切换"（同名字它直接返回不重载）；
            # 用户拒绝丢弃未保存修改时把名字放回去，画布保持自己的旧版。
            keep = self.flow_name
            self.flow_name = ""
            if not self._select_flow(keep):
                self.flow_name = keep
        parts = [f"新增 {r['added']}", f"覆盖 {r['overwritten']}", f"跳过 {r['skipped']}"]
        parts.append(f"并入模板 {r['templates_merged']} 张（同名不覆盖本机）"
                     if info["has_templates"] else "包内无图")
        msg = "✓ 导入完成：" + "，".join(parts)
        if r["missing_refs"]:
            msg += (f"；⚠ 仍有 {len(r['missing_refs'])} 处引用缺图"
                    f"（跑到那步会降级跳过，补采图或让对方重新含图导出即可）")
        self._set_status(msg)

    def _ask_with_templates(self):
        """导出前问一句含不含模板图。返回 True=含 / False=不含 / None=取消导出。
        （冒烟里整方法替换——offscreen 弹模态必阻塞。）"""
        box = QMessageBox(self)
        box.setWindowTitle("导出全部流程")
        box.setText("是否连同流程引用的模板图片一起打包？\n"
                    "含图 = 对方导入即用；不含 = 只打包流程排布，图靠对方自己库里有。")
        chk = QCheckBox("包含模板图片")
        chk.setChecked(True)   # 默认开：换机整迁是主场景
        box.setCheckBox(chk)
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        if box.exec() != QMessageBox.Ok:
            return None
        return chk.isChecked()

    def _ask_conflict(self, dups: list[str]):
        """同名流程一次性问（不逐条弹窗）：True=覆盖全部 / False=跳过全部 / None=取消。
        （冒烟里整方法替换——offscreen 弹模态必阻塞。）"""
        box = QMessageBox(self)
        box.setWindowTitle("同名流程")
        show = "、".join(dups[:5]) + ("…" if len(dups) > 5 else "")
        box.setText(f"包里有 {len(dups)} 条流程本机已存在：{show}")
        box.setInformativeText("覆盖全部 = 包里版本替换本机同名；"
                               "跳过全部 = 本机原样不动，只进包里新增的流程。")
        b_over = box.addButton("覆盖全部", QMessageBox.AcceptRole)
        b_skip = box.addButton("跳过全部", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_over:
            return True
        if clicked is b_skip:
            return False
        return None

    # ── 分类本体管理（右键组标题：新建/重命名/删除）──
    def _known_cats(self) -> set:
        """现有分类名集合 = order 登记表 ∪ 账本里出现过的值（老配置可能有没登记
        进 order 的分类）。空组也算存在 —— 撞名校验用。"""
        cats = {c for c in (self.cfg.get("flow_category_order", []) or []) if c}
        cats |= {c for c in (self.cfg.get("flow_categories", {}) or {}).values() if c}
        return cats

    def _new_category(self):
        """右键组标题「新建分类…」：名字只登记进 order 账本（不碰任何流程）。
        空分类靠 _refresh_flow_list 显示成「分类（0）」，之后拖/移流程进来。"""
        text, ok = QInputDialog.getText(self, "新建分类", "分类名称:")
        if not ok:
            return
        new = (text or "").strip()
        if not new:
            self._set_status("✗ 分类名不能为空")
            return
        if new == UNCLASSIFIED:
            self._set_status(f"✗ 「{UNCLASSIFIED}」是兜底组名，不能当分类名")
            return
        if new in self._known_cats():
            self._set_status(f"✗ 分类「{new}」已存在")
            return
        order = [c for c in (self.cfg.get("flow_category_order", []) or []) if c]
        order.append(new)
        self.cfg.set("flow_category_order", order)
        self.cfg.save()
        self._refresh_flow_list()
        self._refresh_category_combo()
        self._set_status(f"✓ 已新建分类「{new}」（空组也显示，把流程移进来即入组）")

    def _rename_category(self, old: str):
        """右键组标题「重命名」：账本里所有值==旧名的流程改成新名 + order 原位换名。
        只动分类标签，流程本体（custom_flows）一个字节不碰。
        「未分类」伪分类函数级硬挡（菜单已不给该项，这里是第二道闸）。"""
        if not old or old == UNCLASSIFIED:
            return
        text, ok = QInputDialog.getText(self, "重命名分类", "新名称:", text=old)
        if not ok:
            return
        new = (text or "").strip()
        if not new or new == old:
            return   # 空名/同名：无事发生
        if new == UNCLASSIFIED:
            self._set_status(f"✗ 「{UNCLASSIFIED}」是兜底组名，不能当分类名")
            return
        if new in self._known_cats() - {old}:
            self._set_status(f"✗ 分类「{new}」已存在")
            return
        cat_map = dict(self.cfg.get("flow_categories", {}) or {})
        for k, v in cat_map.items():
            if v == old:
                cat_map[k] = new
        order = [new if c == old else c
                 for c in (self.cfg.get("flow_category_order", []) or []) if c]
        self.cfg.set("flow_categories", cat_map)
        self.cfg.set("flow_category_order", order)
        self.cfg.save()
        self._refresh_flow_list()
        self._refresh_category_combo()
        self._set_status(f"✓ 分类「{old}」→「{new}」（组内流程本体不动，只换标签）")

    def _delete_category(self, old: str):
        """右键组标题「删除分类」：销分类标签，组内流程回落到未分类。
        ⚠ 绝不删 custom_flows 里的流程本体（流程库是用户资产红线）。
        非空组先问（默认按钮=否），空组直接删；「未分类」伪分类函数级硬挡。"""
        if not old or old == UNCLASSIFIED:
            return
        members = [k for k, v in (self.cfg.get("flow_categories", {}) or {}).items()
                   if v == old]
        if members:
            r = QMessageBox.question(
                self, "删除分类",
                f"组里 {len(members)} 条流程将回落到未分类（流程本体不删），"
                f"确认删除分类「{old}」？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        cat_map = dict(self.cfg.get("flow_categories", {}) or {})
        for k in members:
            cat_map.pop(k, None)
        order = [c for c in (self.cfg.get("flow_category_order", []) or [])
                 if c and c != old]
        self.cfg.set("flow_categories", cat_map)
        self.cfg.set("flow_category_order", order)
        self.cfg.save()
        self._refresh_flow_list()
        self._refresh_category_combo()
        self._set_status(f"✓ 已删除分类「{old}」"
                         + (f"（{len(members)} 条流程回落未分类）" if members else ""))

    # ── 上锁（只读保护）──
    def _locked_list(self) -> list:
        """上锁名单（config.flow_locked，故意与流程值 dict 分开存）。"""
        return list(self.cfg.get("flow_locked", []) or [])

    def _is_locked(self, name: str = None) -> bool:
        """name 缺省 = 判当前流程。锁名单是列表，成员判定即可。"""
        name = self.flow_name if name is None else name
        return bool(name) and name in self._locked_list()

    def _toggle_lock(self, name: str):
        """点行尾锁图标上锁/解锁：只动锁名单 + 落盘 + 刷界面，绝不碰流程图内容。"""
        locked = self._locked_list()
        lock_now = name not in locked
        if lock_now:
            locked.append(name)
        else:
            locked.remove(name)
        # 先落账本再刷界面：_reload_flow/_refresh_flow_list 内部都会读锁态，
        # 顺序反了会用旧锁态渲染（上锁时重载仍把节点摆成可拖）
        self.cfg.set("flow_locked", locked)
        self.cfg.save()
        discarded = False
        # 列表先刷（🔒 进显示文本；_reload_flow 不刷列表，重载路径也靠这里补上），
        # 再按锁态刷画布只读 —— _reload_flow 内部也会 _sync_readonly，殊途同归
        self._refresh_flow_list()
        if lock_now and name == self.flow_name and self._dirty:
            # 锁上时攒着的未保存改动作废：重载落盘版，免得画布挂着改到一半
            # 的图让人以为存了（_reload_flow 会清脏 + 按新锁态刷只读）
            self._reload_flow()
            discarded = True
        elif name == self.flow_name:
            self._sync_readonly()
        msg = (f"🔒 已锁定「{name}」：不能改画布/保存/改名/删除，双击仍可挂任务、可测试"
               if lock_now else f"🔓 已解锁「{name}」，可继续编辑")
        if discarded:
            msg += "（有未保存修改，已丢弃重载落盘版）"
        self._set_status(msg)

    def _guard_edit(self, what: str = "修改") -> bool:
        """所有"改流程图/账本"动作的统一闸门：当前流程上了锁就拦下、给一句
        解锁指引，返回 True = 已拦（调用方直接 return）。"""
        if self._is_locked():
            self._set_status(f"流程「{self.flow_name}」已上锁，不能{what}"
                             f"（点它行尾的锁图标解锁）")
            return True
        return False

    def _sync_readonly(self):
        """按当前流程锁状态刷"能不能改"的界面反馈：节点不可拖、属性面板与结构
        工具按钮禁用。数据闸门另有 _guard_edit，这里只管视觉（二者互补：视觉
        让用户知道锁了，闸门保证从键盘/脚本等旁路也改不动）。"""
        movable = not self._is_locked()
        for it in getattr(self, "_node_items", {}).values():
            it.setFlag(QGraphicsItem.ItemIsMovable, movable)
        self.props_panel.setEnabled(movable)
        for b in (self.btn_save, self.btn_add_node, self.btn_layout,
                  *self._align_btns.values()):
            b.setEnabled(movable)
        self.spn_watchdog.setEnabled(movable)   # 看门狗在工具栏，不在 props_panel 里，单独管

    def _on_new_flow(self, cat=None):
        """新建空流程。cat = 发起处所在的分类组（右键组标题/组内流程行），新流程
        直接归入该分类；cat 为空（空白处发起）回落当前流程所属分类（工具栏「分类」
        下拉现值）——「在当前分类下加流程，就归当前分类」。都没有 = 未分类（老行为）。"""
        name, ok = QInputDialog.getText(self, "新建流程", "流程名：")
        name = name.strip()
        if not ok or not name:
            return
        flows = self._flows()
        if name in flows:
            self._set_status(f"流程「{name}」已存在")
            return
        flows[name] = {"version": 2, "nodes": [], "edges": []}
        self.cfg.set("custom_flows", flows)
        # 归类：先取发起处的组上下文，缺则回落当前流程的分类。只写分类标签，
        # 流程本体（custom_flows）不受影响；空串/「未分类」由写入端销标签兜底。
        target = (cat or "").strip()
        if not target:
            target = (self.cfg.get("flow_categories", {}) or {}).get(self.flow_name) or ""
        self._write_flow_category(name, target)
        # 立即落盘（与移到分类/新建分类同口径）：新建是"资产落账"动作，
        # 不能只吃 1 秒防抖——「刚建完就关程序/崩」不该把这条流程整笔丢掉。
        self.cfg.save()
        self.flow_name = name
        self._refresh_flow_list()
        self._reload_flow()
        self._emit_flow_saved(name, "")
        tail = f"，归入「{target}」" if target and target != UNCLASSIFIED else ""
        self._set_status(f"已新建空流程「{name}」{tail}")

    def _health_report(self):
        """保存前健康报告：有校验问题就弹窗逐条列（含"哪张缺图被哪个节点用"
        这类横条塞不下的明细），并列出可点击定位的孤立/成环节点。
        返回 True=继续保存，False=用户取消。无问题直接 True 不打扰。"""
        self._refresh_validation()   # 横条走 400ms 防抖，保存是即时动作，先现算一遍
        problems = list(getattr(self, "_val_problems", []))
        details = list(getattr(self, "_val_details", []))
        if not problems:
            return True
        box = QMessageBox(self)
        box.setWindowTitle(f"流程「{self.flow_name}」体检报告")
        box.setIcon(QMessageBox.Warning)
        lines = [f"⚠ {p}" for p in problems] + [f"　· {d}" for d in details]
        box.setText("\n".join(lines))
        b_save = box.addButton("仍然保存", QMessageBox.AcceptRole)
        box.addButton("先不改", QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() is b_save

    def _on_save(self, quiet=False):
        if not self.flow_name:
            self._set_status("没有可保存的流程，先新建或选择一个")
            return
        if self._guard_edit("保存（改内容）"):
            return
        # 手动保存前过一遍体检（自动保存 quiet 不拦，否则每敲字弹一次）：
        # 校验条只有一行横条，明细在这里摊开给用户看。
        if not quiet and not self._health_report():
            return
        flows = self._flows()
        flows[self.flow_name] = ensure_graph(self.graph)
        self.cfg.set("custom_flows", flows)
        # 手动保存立刻落盘；自动保存走 cfg.set 自带的 1 秒防抖，
        # 不额外 save() —— 否则每敲一个字写一次盘
        if not quiet:
            self.cfg.save()
        self._dirty = False
        self._emit_flow_saved(self.flow_name, "")   # 内容变了也发（主窗口对账很便宜）
        if not quiet:
            n = len(self.graph["nodes"])
            self._set_status(f"✓ 已保存流程「{self.flow_name}」（{n} 个节点）")

    def _on_rename_flow(self):
        old = self.flow_name
        if not old:
            self._set_status("没有当前流程，先新建或选择一个")
            return
        if self._guard_edit("改名"):
            return
        if self._dirty:
            r = QMessageBox.question(self, "未保存", "改名前需先保存当前修改，是否保存？",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if r != QMessageBox.Yes:
                return
            self._on_save(quiet=True)
        new, ok = QInputDialog.getText(self, "流程改名", "新流程名：", text=old)
        if not ok:
            return
        new = new.strip()
        if not new:
            self._set_status("流程名不能为空")
            return
        if new == old:
            return
        flows = self._flows()
        if new in flows:
            self._set_status(f"流程「{new}」已存在")
            return
        # 其它流程的「子流程」节点/「并行监视」行按名字指名引用旧流程，改名前说清楚会牵动谁
        from bot.template_deps import iter_all_flow_nodes, node_flow_refs
        users = sorted(fn for fn, fl in flows.items() if fn != old and any(
            old in node_flow_refs(n) for n in iter_all_flow_nodes(fl)))
        tip = (f"把流程「{old}」改名为「{new}」？\n\n"
               + (f"⚠ 它正被这些流程当子流程引用，改名后自动指向新名：{'、'.join(users)}\n"
                  if users else "没有其它流程引用它。"))
        if QMessageBox.question(self, "流程改名", tip,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        # 键换新名（推导式保序）。值对象原样保留 —— self.graph 仍是新键的活引用，
        # 不存在改到孤儿 dict 的问题；_reload_flow() 是为了 flow_name/画布状态一致
        self.cfg.set("custom_flows", {new if k == old else k: v for k, v in flows.items()})
        # 分类标签按流程名存，键换了名标签就成孤儿 —— 跟着迁移
        cat_map = self.cfg.get("flow_categories", {}) or {}
        if old in cat_map:
            cat_map[new] = cat_map.pop(old)
            self.cfg.set("flow_categories", cat_map)
        # 便签同理迁移（先把防抖里的未落账文字存进旧名下，再换键）
        self._note_timer.stop()
        self._save_note()
        notes = dict(self._notes())
        if old in notes:
            notes[new] = notes.pop(old)
            self.cfg.set("flow_notes", notes)
        # 相机账本同样按名字记账（会话内存）：不换键旧相机会成孤儿。
        # _view_flow（画布上正摆着谁）也要跟着换：_reload_flow 稍后会拿它当
        # 存键，不换就把当前镜头又存回旧名下。旧键不存在（正显示的流程还没
        # 存过相机）也要换 _view_flow，下面这行无条件做。
        if old in self._cameras:
            self._cameras[new] = self._cameras.pop(old)
        if self._view_flow == old:
            self._view_flow = new
        # 锁名单不用迁移：上锁流程进不了这里（_guard_edit 拦改名），
        # 能走到这的名字必不在锁名单，没有旧键可留
        flows = self._flows()
        # 子流程节点目标名一并改：全流程扫（含改名流程自己引用自己的情况）
        for fl in flows.values():
            for n in iter_all_flow_nodes(fl):
                if n.get("type") == "subflow" and (n.get("flow_name") or "").strip() == old:
                    n["flow_name"] = new
                elif n.get("type") in ("watch", "timer"):
                    # 监视/执行行的子流程目标同样按名字引用，不改 = 静默断链。
                    # watch_rows 认容器 groups（两份口径必漂移，统一走它）
                    for r in template_deps.watch_rows(n):
                        if isinstance(r, dict) and (r.get("flow_name") or "").strip() == old:
                            r["flow_name"] = new
        # 按名字引用旧流程的其它落点：flow 任务全局参数、实例级队列记号、
        # 实例级参数覆盖（worker 两段式合并会读，见 bot/worker.py _task_params_for）
        # ——account_tasks 暂无界面写入方，但 worker 真读它，改名不能留死链
        tp_flow = (self.cfg.get("task_params", {}) or {}).get("flow")
        if isinstance(tp_flow, dict) and tp_flow.get("flow_name") == old:
            tp_flow["flow_name"] = new
        for entry in (self.cfg.get("account_tasks", {}) or {}).values():
            if not isinstance(entry, dict):
                continue
            q = entry.get("queue")
            if isinstance(q, list):
                entry["queue"] = [f"flow:{new}" if e == f"flow:{old}" else e for e in q]
            p_flow = (entry.get("params") or {}).get("flow")
            if isinstance(p_flow, dict) and p_flow.get("flow_name") == old:
                p_flow["flow_name"] = new
        self.cfg.save()
        self.flow_name = new
        self._refresh_flow_list()   # 列表重建并选中新名（内部 blockSignals）
        self._reload_flow()         # 重绑 self.graph + 重建画布
        self._emit_flow_saved(new, old)  # 外壳转发主窗口：清单/侧栏/任务页实时同步（改名=新名+旧名）
        self._set_status(f"✓ 已把流程「{old}」改名为「{new}」，相关引用已同步")

    def _on_del_flow(self):
        if not self.flow_name:
            return
        if self._guard_edit("删除"):
            return
        # 子流程引用点名的扫一遍：删了哪几条大流程的「子流程」节点/「并行监视」行会变哑
        # （运行时编译期会 WARN 降级不炸，但确认框里先说清楚更负责任）
        from bot.template_deps import iter_all_flow_nodes, node_flow_refs
        users = sorted(fname for fname, fl in self._flows().items()
                       if fname != self.flow_name and any(
                           self.flow_name in node_flow_refs(n)
                           for n in iter_all_flow_nodes(fl)))
        tip = (f"确定删除流程「{self.flow_name}」？\n\n"
               + (f"⚠ 它正被这些流程当子流程引用：{'、'.join(users)}\n"
                  "删除后那些流程里的子流程节点会跳过并打 WARN 日志。"
                  if users else "没有其它流程引用它。"))
        r = QMessageBox.question(self, "删除流程", tip,
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        flows = self._flows()
        flows.pop(self.flow_name, None)
        self.cfg.set("custom_flows", flows)
        # 分类标签是 {流程名: 分类} 旁账本：流程删了不留孤儿键
        # （留着的话日后建同名流程会"继承"旧分类，莫名其妙）
        cat_map = self.cfg.get("flow_categories", {}) or {}
        if self.flow_name in cat_map:
            cat_map.pop(self.flow_name)
            self.cfg.set("flow_categories", cat_map)
        # 便签同样按流程名记账：流程没了便签成孤儿（日后同名新流程会
        # "继承"旧便签，莫名其妙）—— 跟着删（先冲掉防抖里的编辑，再除账）
        self._note_timer.stop()
        notes = dict(self._notes())
        if self.flow_name in notes:
            notes.pop(self.flow_name)
            self.cfg.set("flow_notes", notes)
        # （编辑框里残留的旧文本不用管：下面 _reload_flow 会按新流程灌掉）
        self._cameras.pop(self.flow_name, None)   # 相机账本不留孤儿键（防同名新流程"继承"旧视角）
        # 正显示的就是被删流程：画布"归属"要作废。不清的话 _reload_flow 开头
        # 存离场相机时会按旧名把刚 pop 掉的孤儿键又写回去。
        if self._view_flow == self.flow_name:
            self._view_flow = None
        # 锁名单不用清：上锁流程进不了这里（_guard_edit 拦删除），
        # 能删的名字必已解锁（解锁即从名单移除），没有死键可留
        deleted = self.flow_name
        self.flow_name = ""
        self._dirty = False
        self._refresh_flow_list()
        self._reload_flow()
        self._emit_flow_saved("", deleted)   # 事件唯一出口：主窗口按集合对账剔死项
        self._set_status("流程已删除")

    # ── 画布重建（v2 图：按 node.x/y 绝对坐标放节点，按 edges 画边）──
    def _rebuild_scene(self):
        self._stop_ghost()       # 残影 item 会被 scene.clear() 销毁，先掐动画
        self._guide_items = []   # scene.clear() 已销毁参考线，只剩悬空 Python 引用
        self.scene.blockSignals(True)
        self.scene.clear()
        self._node_items = {}   # id -> FlowNodeItem

        # 节点：带 id 的按坐标摆；老节点（无 id）补 id
        id_map = {}
        normalized = []
        for n in self.graph["nodes"]:
            nid = n.get("id") or _graph_id()
            n["id"] = nid
            n["x"] = n.get("x", 40 + len(normalized) * 20)
            n["y"] = n.get("y", 40 + len(normalized) * 20)
            id_map[nid] = n
            normalized.append(n)
        self.graph["nodes"] = normalized
        # 容器就地整形：每组补 id/rows（缺 id 的组线会在编译期撞车重编号）
        normalize_watch_containers(normalized)

        # 边：引用 id（from 节点可能缺 id 时自动补）。to_port 透传，普通线缺省 = "in"。
        # 信号线通道已下线（一刀切暂停不用接线）：老流程里存的 hit / sig 边
        # 在这里剪除（与编译层剔除同口径），下次保存落盘即净。
        edges = []
        for e in self.graph["edges"]:
            f, p = e.get("from", ""), e.get("port", "out")
            t = e.get("to", "")
            tp = e.get("to_port", "in")
            if p == "hit" or p.startswith("hit:") or tp == "sig":
                continue
            if f in id_map and t in id_map:
                edges.append((f, p, t, tp))
        self.graph["edges"] = [{"from": f, "port": p, "to": t, "to_port": tp}
                               for f, p, t, tp in edges]

        # 放节点（含试运行结果角标回填）
        for n in normalized:
            item = FlowNodeItem(n, self)
            item.setPos(n["x"], n["y"])
            self.scene.addItem(item)
            item._test_mark = self._test_cache.get(n["id"])
            self._node_items[n["id"]] = item
        # 画边（虚线），先于节点下方。N 口线染红，其余灰。
        for f, p, t, tp in edges:
            if f in self._node_items and t in self._node_items:
                color = "#e05252" if p == "fail" else "#888"
                edge = FlowEdgeItem(self._node_items[f], self._node_items[t], p,
                                    color=color, to_port=tp)
                self.scene.addItem(edge)

        self.scene.blockSignals(False)
        # 图结构一切变化（连线/删/撤销/粘贴/换流程）都以重建为必经点，
        # 校验挂这里 = 一处接线全覆盖，不用每个操作点各喊一遍
        self._schedule_validation()
        # 新建的节点 item 默认带 ItemIsMovable，锁流程要在这里统一摘掉
        self._sync_readonly()

    # ── 撤销 / 重做 / 复制粘贴（结构操作，快照 = graph 的 json）──
    def _snapshot(self) -> str:
        return json.dumps({"nodes": self.graph["nodes"],
                           "edges": self.graph["edges"]}, ensure_ascii=False)

    def _push_undo(self) -> bool:
        """结构改动前调用：当前状态进撤销栈，重做栈作废（改新账旧重做失效）。
        上锁 = 图只读，这里一处拦住所有"改图"动作（增删节点/连线/粘贴/撤销…
        全部以本方法起手），调用方判返回值 False 即早退。比在每个方法各加一行
        guard 少得多，也堵死键盘快捷键这类不点按钮的旁路。"""
        if self._is_locked():
            self._set_status(f"流程「{self.flow_name}」已上锁，不能修改"
                             f"（点它行尾的锁图标解锁）")
            return False
        self._undo.append(self._snapshot())
        del self._undo[:-100]     # 封顶 100 步，防长会话内存涨
        self._redo.clear()
        return True

    def _restore(self, snap: str):
        data = json.loads(snap)
        self.graph["nodes"] = data["nodes"]
        self.graph["edges"] = data["edges"]
        # 回程票/参考线指向的 dict 可能已被快照回退作废，一并清掉
        self._capture_return = None
        self._guide_items = []
        self._rebuild_scene()
        self._rebuild_props(None)
        self._mark_dirty()

    def _undo_step(self):
        if self._editing_text():
            return
        if not self._undo:
            self._set_status("没有可撤销的操作")
            return
        if self._guard_edit("撤销"):   # 撤销也是改图：锁住的图历史栈不动
            return
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())
        self._set_status("已撤销")

    def _redo_step(self):
        if self._editing_text():
            return
        if not self._redo:
            return
        if self._guard_edit("重做"):
            return
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())
        self._set_status("已重做")

    def _on_canvas_shortcut(self, cmd: str):
        """view.keyPressEvent 的 Ctrl 系快捷键分流。画布只有自己有焦点时
        才收得到键（文本框抢焦点时原生 Ctrl+C/V/Z 不受影响），这里只管派发。"""
        {"undo": self._undo_step, "redo": self._redo_step,
         "copy": self._copy_selection, "paste": self._paste,
         "cut": self._cut_selection, "dup": self._duplicate_selection,
         "selectall": self._select_all_nodes}.get(cmd)()

    def _editing_text(self) -> bool:
        """兜底闸：调用点理论上只从画布焦点路径进入，双保险防快捷键
        在输入框编辑中途误吞整图（焦点判定见各入口）。"""
        fw = self.focusWidget()
        return isinstance(fw, (QLineEdit, QComboBox))

    @staticmethod
    def _node_ref_ids(n: dict) -> set:
        """一个节点在 edges 里可能充当 from/to 的 id 集合。复制判边、
        粘贴重映射都以它为准。（哨兵无端口，组 id 不再被边引用。）"""
        return {n.get("id")}

    def _copy_selection(self) -> int:
        if self._editing_text():
            return 0
        items = [i for i in self.scene.selectedItems() if isinstance(i, FlowNodeItem)]
        if not items:
            self._set_status("没有选中节点，先点选（或框选）要复制的节点")
            return 0
        ids = set()
        for i in items:
            ids |= self._node_ref_ids(i.node)
        self._clipboard = {
            "nodes": [copy.deepcopy(i.node) for i in items],
            # 两端都被复制的边才带走：单端跟着走会粘出一头悬空的死线
            "edges": [copy.deepcopy(e) for e in self.graph["edges"]
                      if e.get("from") in ids and e.get("to") in ids],
        }
        self._paste_n = 0
        return len(items)

    def _paste(self):
        if self._editing_text() or not self._clipboard:
            return
        if self._guard_edit("粘贴节点"):
            return          # 上锁旁路②：Ctrl+V 不走按钮，禁用态拦不住它
        if not self._push_undo():
            return
        # ⚠ 2026-10-07 粘贴跟随鼠标（用户："应贴在我鼠标指着的位置，不是原画板
        # 固定坐标"）：整组按包围盒左上角对齐到鼠标场景点（再多让 30px，贴出来
        # 一角露在指针下当把手）。鼠标不在画布上 → 视口中心兜底。同一片剪贴板
        # 连贴多刀按 _paste_n 阶梯错开不叠死（复制即归零；旧版 +30 是对原坐标
        # 平移，鼠标挪到画布另一头也贴回老地方，才是"固定坐标"的病根）。
        self._paste_n += 1
        # 坐标一律走 viewport 自己：view.mapFromGlobal 带回框厚（滚动区 frame 2px），
        # 而 mapToScene 吃的是视口坐标，混用会整体偏 2px（冒烟实测抓到）。
        gp = self.view.viewport().mapFromGlobal(QCursor.pos())
        if not self.view.viewport().rect().contains(gp):
            gp = QPoint(self.view.viewport().width() // 2,
                        self.view.viewport().height() // 2)
        anchor = self.view.mapToScene(gp)
        min_x = min(n["x"] for n in self._clipboard["nodes"])
        min_y = min(n["y"] for n in self._clipboard["nodes"])
        dx = anchor.x() - min_x + 30 * self._paste_n
        dy = anchor.y() - min_y + 30 * self._paste_n
        id_map, new_nodes = {}, []
        for n in self._clipboard["nodes"]:
            m = copy.deepcopy(n)
            m["id"] = _graph_id()
            m["x"], m["y"] = m["x"] + dx, m["y"] + dy
            id_map[n["id"]] = m["id"]
            # 容器组也要换新 id（不换 = 副本和原件抢同一组 id，编译期撞车
            # 重编号、面板页签也会串数据）
            gs = container_groups(m) if m.get("type") == "watch" else None
            if gs:
                for g in gs:
                    if isinstance(g, dict):
                        old_g = g.get("id")
                        g["id"] = _graph_id()
                        if old_g:
                            id_map[old_g] = g["id"]
            new_nodes.append(m)
        self.graph["nodes"].extend(new_nodes)
        for e in self._clipboard["edges"]:
            self.graph["edges"].append({"from": id_map[e["from"]],
                                        "port": e.get("port", "out"),
                                        "to": id_map[e["to"]],
                                        "to_port": e.get("to_port", "in")})
        self._mark_dirty()
        self._rebuild_scene()
        self.scene.clearSelection()
        for m in new_nodes:                       # 贴完即选中，可马上拖走/再操作
            it = self._node_items.get(m["id"])
            if it:
                it.setSelected(True)
        self._set_status(f"已粘贴 {len(new_nodes)} 个节点")

    def _cut_selection(self):
        if self._editing_text():
            return
        if self._copy_selection():
            self._on_delete_from_canvas()         # 删除自带 _push_undo，撤销一步回到剪前

    def _duplicate_selection(self):
        if self._editing_text():
            return
        if self._copy_selection():
            self._paste()

    def _select_all_nodes(self):
        if self._editing_text():
            return
        self.scene.clearSelection()
        for i in self.scene.items():
            if isinstance(i, FlowNodeItem):
                i.setSelected(True)

    # ── 拖动对齐参考线（smart guides）──
    def _show_guides(self, lines):
        self._clear_guides()
        pen = QPen(QColor("#ff4d4f"), 0, Qt.SolidLine)
        pen.setCosmetic(True)   # 参考线恒 1px：缩得再小也不糊成一坨
        for x1, y1, x2, y2 in lines:
            it = self.scene.addLine(x1, y1, x2, y2, pen)
            it.setZValue(20)                       # 画在最上层，一眼可见
            it.setAcceptedMouseButtons(Qt.NoButton)
            self._guide_items.append(it)

    def _clear_guides(self):
        for it in self._guide_items:
            try:
                self.scene.removeItem(it)
            except RuntimeError:
                pass   # scene.clear()/撤销重建已销毁它
        self._guide_items = []

    # 节点拖拽的撤销快照：按下先记旧坐标，真拖动过才入栈（点一下不占撤销步）
    def _begin_drag_snap(self):
        self._drag_snap = self._snapshot()

    def _end_drag_snap(self, moved: bool):
        if moved and getattr(self, "_drag_snap", None):
            self._undo.append(self._drag_snap)
            del self._undo[:-100]
            self._redo.clear()
        self._drag_snap = None

    # ── 画布布局工具（照 Qt Designer：方向键微调 / 对齐 / 等距分布）──
    def _selected_node_items(self):
        return [i for i in self.scene.selectedItems()
                if isinstance(i, FlowNodeItem)]

    def _move_node_items(self, pairs):
        """[(item, nx, ny)] → 挪图元、写回节点 dict、重排受影响的边。
        不整场重建：重建会丢选中态，连点对齐/连按方向键没法连续操作。"""
        moved = {id(it) for it, _, _ in pairs}
        for it, nx, ny in pairs:
            it.setPos(nx, ny)
            it.node["x"], it.node["y"] = int(nx), int(ny)
        for edge in self.scene.items():
            if isinstance(edge, FlowEdgeItem) and \
                    (id(edge.from_item) in moved or id(edge.to_item) in moved):
                edge.update_positions()

    def _nudge_selection(self, dx: int, dy: int):
        """方向键微调：dx/dy 是格数（view 里已折算 Shift 大步）。一步一格。"""
        if self._editing_text():
            return
        items = self._selected_node_items()
        if not items:
            return
        g = FlowScene.GRID
        snap = getattr(self.scene, "snap_grid", False)
        if not self._push_undo():
            return
        pairs = []
        for it in items:
            nx, ny = it.pos().x() + dx * g, it.pos().y() + dy * g
            if snap:
                nx, ny = round(nx / g) * g, round(ny / g) * g
            pairs.append((it, nx, ny))
        self._move_node_items(pairs)
        self._mark_dirty()

    def _align_selection(self, key: str):
        """对齐/分布按钮的公共实现。盒界以选中节点最外沿为基准（Designer 同语义）。"""
        if self._editing_text():
            return
        items = self._selected_node_items()
        if len(items) < 2:
            return
        xs = [i.pos().x() for i in items]
        ys = [i.pos().y() for i in items]
        box_l, box_r = min(xs), max(x + NODE_W for x in xs)
        box_t, box_b = min(ys), max(y + _node_h(i.node) for i, y in zip(items, ys))
        pairs = []
        if key == "left":
            pairs = [(i, box_l, i.pos().y()) for i in items]
        elif key == "right":
            pairs = [(i, box_r - NODE_W, i.pos().y()) for i in items]
        elif key == "hmid":
            c = (box_l + box_r) / 2
            pairs = [(i, c - NODE_W / 2, i.pos().y()) for i in items]
        elif key == "top":
            pairs = [(i, i.pos().x(), box_t) for i in items]
        elif key == "bottom":
            pairs = [(i, i.pos().x(), box_b - _node_h(i.node)) for i in items]
        elif key == "vmid":
            c = (box_t + box_b) / 2
            pairs = [(i, i.pos().x(), c - _node_h(i.node) / 2) for i in items]
        elif key in ("hspread", "vspread"):
            h = key == "hspread"
            s = sorted(items, key=lambda i: i.pos().x() if h else i.pos().y())
            n = len(s)
            if n < 3:
                return   # 两个节点没有"中间"可均分
            c0 = s[0].pos().x() if h else s[0].pos().y()
            c1 = s[-1].pos().x() if h else s[-1].pos().y()
            step = (c1 - c0) / (n - 1)   # 两端钉死，中间按中心等距
            for k, it in enumerate(s[1:-1], 1):
                pairs.append((it, c0 + step * k, it.pos().y()) if h
                             else (it, it.pos().x(), c0 + step * k))
        if not self._push_undo():
            return
        self._move_node_items(pairs)
        self._mark_dirty()

    # ── 一键整理布局（分层拓扑）──────────────────────────────────────
    COL_W = 280     # 列间距（节点宽 200 + 走线余量）
    ROW_H = 140     # 行间距（节点高 64 + 余量）

    def _auto_layout(self):
        """「整理布局」：按连线把节点摆整齐 —— 执行链从左到右推进，同深度
        的兄弟（Y/N 分叉、并行链）纵向排开。端口在左右缘、贝塞尔走水平切线，
        横排流向与连线走向天然契合。对标 Blueprint「Layout」/ n8n「Tidy up」。
        ⚠ 执行顺序完全由连线（GraphStep 的 token 传播）决定，节点 x/y 只是画布
        坐标、运行时从不读；所以本函数对已存流程的运行行为零影响，可放心整理。

        算法：
        1. 列 = 从入口起的最长路径深度（拓扑序松弛，能扛住回头边不成环）。
        2. 同列多节点按「父节点平均行」排序（barycenter），再按序上下排开。
        3. 孤立/多入度失衡等极端情况不追求完美，只求「比手动摆整齐」。"""
        nodes = self.graph["nodes"]
        if not nodes:
            self._set_status("画布是空的，先加节点")
            return
        if not self._push_undo():
            return
        ids = [n["id"] for n in nodes]
        by_id = {n["id"]: n for n in nodes}

        # 邻接：出边（分列松弛用）+ 入边父节点（barycenter 排序用）
        outs = {i: [] for i in ids}
        ins = {i: [] for i in ids}
        for e in self.graph["edges"]:
            f, t = e.get("from"), e.get("to")
            if f in by_id and t in by_id:
                outs[f].append(t)
                ins[t].append(f)

        # 1) 分列：col = 从入口起的最长路径深度。拓扑序里逐节点取「各父 +1」
        #    的最大值，单趟即精确（无环前提）。有环（编译期本就拦，这里只求摆得下）
        #    退化成按 ids 序，未处理节点 col 记 0、边到边自然铺开。
        col = {i: 0 for i in ids}
        order = self._topo_order(ids, outs, ins) or ids
        for i in order:
            if ins[i]:
                col[i] = max((col[p] + 1 for p in ins[i]), default=0)

        # 2) 分列分桶 + barycenter 定行序（父节点已定位行的均值）
        buckets = {}
        for i, c in col.items():
            buckets.setdefault(c, []).append(i)
        row = {}
        for c in sorted(buckets):
            group = buckets[c]
            def bary(i):
                ps = [row[p] for p in ins[i] if p in row]
                return sum(ps) / len(ps) if ps else ids.index(i)
            # 先按父重心、再按插入序稳定排，保证同重心节点不抖
            group.sort(key=lambda i: (round(bary(i), 3), ids.index(i)))
            for k, i in enumerate(group):
                row[i] = k

        # 3) 写回坐标：列→x（居中，负列向右半轴起），行→y（按各行数居中堆叠）
        for i, n in by_id.items():
            n["x"] = col[i] * self.COL_W
            n["y"] = row[i] * self.ROW_H
        self._mark_dirty()
        self._rebuild_scene()
        self.view.fit_scene()
        self._set_status(f"✓ 已整理 {len(nodes)} 个节点（列 {len(buckets)}），"
                         "运行顺序只看连线、与摆位无关")

    @staticmethod
    def _topo_order(ids, outs, ins):
        """Kahn 拓扑序；图有环则返回 None（调用方退化处理）。"""
        indeg = {i: len([p for p in ins[i]]) for i in ids}
        dq = [i for i in ids if indeg[i] == 0]
        order = []
        while dq:
            i = dq.pop(0)
            order.append(i)
            for j in outs[i]:
                indeg[j] -= 1
                if indeg[j] == 0:
                    dq.append(j)
        return order if len(order) == len(ids) else None

    def _update_align_buttons(self):
        # 可用 = 多选 ≥2 且画布可编辑（试运行锁图时 view 整体 disabled，
        # 画布都点不动了，对齐按钮留着只会报错）
        n = len(self._selected_node_items()) if self.view.isEnabled() else 0
        for b in self._align_btns.values():
            b.setEnabled(n >= 2)

    # ── 画布交互：拖拽合并 / 连线 / 改接 / 删除 ──
    def _stop_ghost(self):
        """掐掉在飞的拆/并残影。_rebuild_scene 开头必调：scene.clear() 会把
        残影 item 连带销毁，动画回调再碰它就是野指针（RuntimeError）。"""
        if self._ghost is not None:
            anim, item = self._ghost
            self._ghost = None
            anim.stop()          # stop 不发 finished，清理归这里管
            self.scene.removeItem(item)

    def _capture_rect(self, item, local: QRectF) -> QPixmap:
        """把节点（或节点上的一块局部矩形，如容器里某一条组带）画成图。
        FlowNodeItem.paint 不读 option 参数，直接调安全（缩略图/端口照画）。
        抓拍前临时摘掉选中框：被拖件必然选中，黄框糊在残影上像贴了层胶带。"""
        sel = item.isSelected()
        item.setSelected(False)
        pix = QPixmap(int(local.width()), int(local.height()))
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.translate(-local.x(), -local.y())
        item.paint(p, None, None)
        p.end()
        item.setSelected(sel)
        return pix

    def _fly_ghost(self, pix: QPixmap, src: QPointF, dst: QPointF,
                   absorb: bool = False):
        """拆出/合并的残影飞行动画：数据早已就位、画布已重建，这里只是补一段
        视觉过渡，让眼睛跟得上"东西从哪来到哪去"。残影浮在最高层、不吃鼠标。
        absorb=True → 边飞边缩小（被吸进容器）；两种都末段淡出叠进真节点。"""
        self._stop_ghost()
        item = self.scene.addPixmap(pix)
        item.setZValue(1000)
        item.setAcceptedMouseButtons(Qt.NoButton)
        item.setOffset(-pix.width() / 2.0, -pix.height() / 2.0)  # 中心对位
        item.setPos(src)
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(200)
        anim.setEasingCurve(QEasingCurve.OutCubic)

        def on_t(t):
            item.setPos(QPointF(src.x() + (dst.x() - src.x()) * t,
                                src.y() + (dst.y() - src.y()) * t))
            item.setOpacity(1.0 - t)
            if absorb:
                item.setScale(1.0 - 0.4 * t)

        anim.valueChanged.connect(on_t)
        anim.finished.connect(lambda: (self.scene.removeItem(item),
                                       setattr(self, "_ghost", None)))
        self._ghost = (anim, item)
        anim.start()

    def _group_band_local(self, gs: list, gi: int) -> QRectF:
        """容器节点上第 gi 组标题带+行体的局部矩形（残影抓拍/落点都拿它定位）。"""
        y = NODE_H + sum(group_body_h(g) for g in gs[:gi] if isinstance(g, dict))
        return QRectF(0, y, NODE_W, group_body_h(gs[gi]))

    def _try_merge_watch(self, dragged_item) -> bool:
        """把拖动的并行监视节点"投"进它松手时压住的另一台监视/容器（同型才收：
        只认 watch，timer 不混装）。返回 True = 已合并并重建画布（调用方别再
        写回坐标）。单哨兵被拖 = 就地变一组；整台容器被拖 = 全部组按原序搬进
        （组 id/参数/名字跟着组走，没名的冠源节点名）；目标普通哨兵先就地容器化。
        edges 一个字不动。投进自己 / 没压住监视 / 上锁 → False。"""
        n = dragged_item.node
        if n.get("type") != "watch":
            return False
        if self._guard_edit("合并监视"):
            return False
        rect = QRectF(dragged_item.pos().x(), dragged_item.pos().y(),
                      NODE_W, _node_h(n))
        target = None
        for it in self.scene.items():
            if not isinstance(it, FlowNodeItem) or it is dragged_item:
                continue
            tn = it.node
            if tn.get("type") != "watch":
                continue
            trect = QRectF(it.pos().x(), it.pos().y(), NODE_W, _node_h(tn))
            if not trect.intersects(rect):
                continue
            # 多个候选压一起时取重叠面积最大的（拖到哪算哪）
            inter = trect.intersected(rect).width() * trect.intersected(rect).height()
            if target is None or inter > target[0]:
                target = (inter, tn)
        if target is None:
            return False
        tn = target[1]
        gs = container_groups(tn)
        if gs is None:
            # 目标还是普通哨兵 → 就地容器化：首组冠目标原 id，
            # 顶层 rows 挪进组里（编译器见 groups 即弃顶层 rows，不会双跑）
            gs = [make_watch_group(tn)]
            tn["groups"] = gs
            tn.pop("rows", None)
        # 被并方：整台容器 = 全部组按原序搬进（用户拍板 2026-10-05，组 id
        # 跟着组走 → 命中统计不丢）；单哨兵 = 就地变一组
        src_groups = ([g for g in (container_groups(n) or []) if isinstance(g, dict)]
                      if container_groups(n) is not None else [make_watch_group(n)])
        # 组没名字 → 冠被并容器的节点名（多台并进来靠名字认出处，编号认顺序）
        src_lbl = (n.get("label") or "").strip()
        if src_lbl:
            for g in src_groups:
                if not (g.get("label") or "").strip():
                    g["label"] = src_lbl
        gs.extend(src_groups)
        self.graph["nodes"] = [x for x in self.graph["nodes"]
                               if x is not n]
        self._mark_dirty()
        # 合并动画：重建前把被吞节点抓成图（数据/画布随后即变，图是独立位图）
        pix = self._capture_rect(dragged_item,
                                 QRectF(0, 0, NODE_W, _node_h(n)))
        src = QPointF(dragged_item.pos().x() + NODE_W / 2,
                      dragged_item.pos().y() + _node_h(n) / 2)
        self._rebuild_scene()
        # 落点 = 并入的第一条新组带中心（残影缩小飞进它 = 被吸收）
        host = self._node_items.get(tn["id"])
        if host is not None and src_groups:
            band = self._group_band_local(gs, len(gs) - len(src_groups))
            dst = QPointF(host.pos().x() + band.center().x(),
                          host.pos().y() + band.center().y())
            self._fly_ghost(pix, src, dst, absorb=True)
        # 重建把选中清空了：不重新选中容器，80ms 后选中防抖会以"没选任何
        # 节点"刷一次面板 —— 刚灌好的容器表单凭空变空白（实测踩坑）。
        # 换序/拆出路径都补了 _reselect_node，合并这条漏了。
        self._reselect_node(tn)
        self._rebuild_props(tn)   # 合并后面板直接看容器（含新组页签）
        self._set_status(f"✓ 已并入选中的并行监视（现 ×{len(gs)} 组）；"
                         "拖组标题行可拆出去（拆出也是一台容器）")
        return True

    def _try_reorder_group(self, item, gid: str, drop_pos) -> bool:
        """组把手松在本容器方框内 = 拖到那一组的位置换序。编号=数组序现算，
        组 id 跟着组走。返回 True = 落点已消费
        （哪怕没挪位），只有松在方框外才放行给拆出 —— 手抖小幅拖不再误拆。"""
        n = item.node
        gs = container_groups(n)
        if not gs:
            return False
        x = drop_pos.x() - item.pos().x()
        y = drop_pos.y() - item.pos().y()
        if not (0 <= x <= NODE_W) or y >= NODE_H + sum(
                group_body_h(g) for g in gs if isinstance(g, dict)):
            return False   # 松在容器外 → 拆出路径
        tgt, acc = None, NODE_H
        for i, g in enumerate(gs):
            if not isinstance(g, dict):
                continue
            if y < acc + group_body_h(g):
                tgt = i
                break
            acc += group_body_h(g)
        if tgt is None:
            tgt = len(gs) - 1
        gi = next((i for i, g in enumerate(gs)
                   if isinstance(g, dict) and g.get("id") == gid), -1)
        if gi < 0:
            return False
        if gi == tgt:
            return True    # 拖回原位/顶部名称带 = 无操作
        if self._guard_edit("组换序"):
            return True
        gs.insert(tgt, gs.pop(gi))
        self._mark_dirty()
        self._rebuild_scene()
        self._reselect_node(n)
        self._rebuild_props(n)
        return True

    def _try_split_group(self, item, gid: str, drop_pos) -> bool:
        """把容器里的一组拖到画布空白处 = 拆回独立监视节点（合并的逆操作）。
        组 id 直接当新节点 id。"""
        n = item.node
        if self._guard_edit("拆出监视组"):
            return False
        gs = container_groups(n)
        if not gs:
            return False
        gi = next((i for i, g in enumerate(gs)
                   if isinstance(g, dict) and g.get("id") == gid), -1)
        if gi < 0:
            return False
        # 拆出动画：pop 之前先抓拍这条组带（paint 读的是节点现状，
        # 数据一改残影就拍到"拆完的空壳"了）—— 组带=标题行+行体
        band = self._group_band_local(gs, gi)
        pix = self._capture_rect(item, band)
        src = QPointF(item.pos().x() + band.center().x(),
                      item.pos().y() + band.center().y())
        g = gs.pop(gi)
        newn = new_watch_node_from_group(g, drop_pos.x() - NODE_W // 2,
                                         drop_pos.y() - NODE_H // 2,
                                         fallback_label=n.get("label") or "")
        self.graph["nodes"].append(newn)
        if not gs:                    # 拆到空壳才退回普通哨兵；剩 1 组也是容器（不摊平）
            collapse_watch_container(n)
        self._mark_dirty()
        self._rebuild_scene()
        it = self._node_items.get(newn["id"])
        if it:
            self.scene.clearSelection()
            it.setSelected(True)
            dst = QPointF(it.pos().x() + NODE_W / 2,
                          it.pos().y() + _node_h(newn) / 2)
            self._fly_ghost(pix, src, dst)   # 组带飞落点，到位淡出叠进新节点
        self._rebuild_props(newn)
        self._set_status("已拆出为独立并行监视（编号会重排）")
        return True

    def _on_edge_connect(self, from_item, port, to_item, to_port="in"):
        """画布上点出点→点入点完成连线。拒绝自连与重复线；branch 端口名保留。"""
        f_id = from_item.node.get("id")
        t_id = to_item.node.get("id")
        stored_port = port
        if f_id == t_id:
            return
        if to_item.node.get("type") in ("watch", "timer"):
            # 哨兵没有入点也拦一道：并行监视/定时执行放上画布就全程在岗，
            # 主线暂停由引擎一刀切，不需要也不接受任何连线。
            self._set_status(f"{TYPE_LABEL[to_item.node['type']]}是常驻哨兵："
                             "不用接线（子链在跑时主线自动暂停）")
            return
        # 重复线（同 from/port/to/to_port）拒绝
        for e in self.graph["edges"]:
            if e.get("from") == f_id and e.get("port") == stored_port \
                    and e.get("to") == t_id \
                    and e.get("to_port", "in") == to_port:
                self._set_status("这条线已存在")
                return
        if not self._push_undo():
            return
        self.graph["edges"].append({"from": f_id, "port": stored_port, "to": t_id,
                                    "to_port": to_port})
        self._mark_dirty()
        self._rebuild_scene()
        self._set_status("已连接")

    def _on_edge_reconnect(self, edge, end, target_item, scene_pos):
        """拖选中线的近端改接到别的节点（断线重连，远端不动）。

        end="from"：改这条线的源端；end="to"：改目标端。
        拒绝：自环、branch 的 Y/N 与普通 out 口互接、改完变重复线。"""
        f_id = edge.from_item.node.get("id")
        t_id = edge.to_item.node.get("id")
        old = next((e for e in self.graph["edges"]
                    if e.get("from") == f_id and e.get("port") == edge.port
                    and e.get("to") == t_id
                    and e.get("to_port", "in") == edge.to_port), None)
        if old is None:
            return   # 画布项与数据不同步（理论上不会），静默放弃
        new_id = target_item.node.get("id")
        if end == "to":
            if new_id == f_id:
                self._set_status("不能改接到源节点自己身上（自环）")
                return
            if target_item.node.get("type") in ("watch", "timer"):
                self._set_status(f"{TYPE_LABEL[target_item.node['type']]}是常驻哨兵："
                                 "不用接线（子链在跑时主线自动暂停）")
                return
            cand = {"from": f_id, "port": edge.port, "to": new_id, "to_port": "in"}
        else:
            # 源端 = 出点集（排除 in 入点）
            outs = [p[0] for p in target_item._ports() if p[0] != "in"]
            if edge.port not in outs:
                self._set_status("源端口对不上（普通节点=顺延、判断节点=Y/N），"
                                 "请删掉这条线重新连")
                return
            if new_id == t_id:
                self._set_status("不能改接到目标节点自己身上（自环）")
                return
            cand = {"from": new_id, "port": edge.port, "to": t_id,
                    "to_port": old.get("to_port", "in")}
        for e in self.graph["edges"]:
            if e is not old and e.get("from") == cand["from"] \
                    and e.get("port") == cand["port"] and e.get("to") == cand["to"] \
                    and e.get("to_port", "in") == cand.get("to_port", "in"):
                self._set_status("这条线已存在")
                return
        if not self._push_undo():
            return          # 快照是 json 串，不受下面就地改写影响
        old.clear()
        old.update(cand)
        self._mark_dirty()
        self._rebuild_scene()
        self._set_status("已改接")

    def _on_delete_from_canvas(self):
        """Delete 键：删选中节点（连带其所有边）或选中边。"""
        if self._guard_edit("删除节点/连线"):
            return          # 上锁旁路①：拖拽/按钮都禁了，键盘 Delete 键照删（实测漏网）
        sel = self.scene.selectedItems()
        nodes = [i for i in sel if isinstance(i, FlowNodeItem)]
        if not nodes and not any(isinstance(i, FlowEdgeItem) for i in sel):
            return
        if not self._push_undo():
            return          # 两种删除路都要撤销；空选不占撤销步
        if nodes:
            for it in nodes:
                nid = it.node.get("id")
                dead = {nid}
                self.graph["nodes"] = [n for n in self.graph["nodes"]
                                       if n.get("id") != nid]
                self.graph["edges"] = [e for e in self.graph["edges"]
                                       if e.get("from") not in dead
                                       and e.get("to") not in dead]
            self._mark_dirty()
            self._rebuild_scene()
            self._rebuild_props(None)
            return
        edges = [i for i in sel if isinstance(i, FlowEdgeItem)]
        if edges:
            for edge_obj in edges:
                self.graph["edges"] = [
                    e for e in self.graph["edges"]
                    if not (e.get("from") == edge_obj.from_item.node.get("id")
                            and e.get("port") == edge_obj.port
                            and e.get("to") == edge_obj.to_item.node.get("id"))]
            self._mark_dirty()
            self._rebuild_scene()

    # ── 节点增删改 ──
    def _mark_dirty(self):
        # 属性面板构建期不算脏：建表单时 combo 的 addItems/setCurrentText 会同步
        # 触发 currentIndexChanged/currentTextChanged → 回调里的 _mark_dirty 是
        # "还原现状"不是"用户编辑"，否则光选中节点看一眼就把流程标成未保存。
        if getattr(self, "_building_props", False):
            return
        self._dirty = True
        # 改属性（选了张不存在的图、切分支条件）不触发 _rebuild_scene，这里补排一次
        self._schedule_validation()
        # 没选流程时不触发：_on_save 会每次改动刷一句"没有可保存的流程"，烦
        if self.chk_autosave.isChecked() and self.flow_name:
            self._on_save(quiet=True)

    def _schedule_validation(self):
        """图结构变了（连线/删节点/撤销/换流程）：排一次防抖校验。"""
        self._val_timer.start()

    def _refresh_validation(self):
        """校验外壳：判据链（detect_cycle/compile_graph）对从盘上直读的畸形图
        （edges 不是 dict 等）会连扫描一起炸——400ms 定时器在主线程里，炸了就是
        闪退。这里兜住：扫描崩了就当"没问题"不亮条（保存/试运行仍会撞上同一
        异常并显示错误，不是静默丢校验）。"""
        try:
            self._refresh_validation_impl()
        except Exception:
            self._val_problems, self._val_details = [], []
            self.lbl_valid.setVisible(False)

    def _refresh_validation_impl(self):
        """编辑期校验：扫当前画布（含未保存修改），把运行时才会暴露的问题提前
        列出来。三项判据全复用现成实现，不自造口径：
          成环   → bot.tasks.flow.detect_cycle（仅提醒不拦运行：引擎把环当循环
                   跑；"无入口"= 全图成环的子集，不单列）
          缺图   → template_deps.node_tpl_refs + template_manager.check_missing
          孤立   → 出入线都没有的节点（多入口 = 并行起链，合法不报）
        空画布不提示（还没开始编排不是错）。"""
        nodes = self.graph.get("nodes") or []
        edges = self.graph.get("edges") or []
        problems, details = [], []
        self._val_problems = problems      # 保存前健康报告的数据源（引用同一 list）
        self._val_details = details

        def _desc(n):
            return (n.get("label") or TYPE_LABEL.get(n.get("type"), "?"))

        if not nodes:
            self.lbl_valid.setVisible(False)
            return

        # 纯哨兵防呆：全图只有 watch/timer 节点 = 主线为空，第一拍即"全部收口"，
        # 流程秒完成、哨兵跟着收工 —— 想常驻监视必须至少有一条主线动作链
        # （要无限挂机就配 循环 节点当主线）。
        if all(n.get("type") in ("watch", "timer") for n in nodes):
            problems.append("画布只有哨兵节点（并行监视/定时执行）：主线为空，"
                            "流程一跑完（=立刻）哨兵就收工。给它配条主线"
                            "（无限挂机用「循环」节点）")

        cyc = detect_cycle({"nodes": nodes, "edges": edges})
        if cyc:
            problems.append(f"成环（{len(cyc)} 个节点连成了回路：会循环执行，"
                            f"确认出口有「结束流程」/失败口接住，否则靠看门狗兜底）")
            by_id = {n.get("id"): n for n in nodes}
            details.append("成环节点：" + "、".join(
                _desc(by_id[i]) for i in cyc if i in by_id))

        refs = [r for n in nodes for r in template_deps.node_tpl_refs(n)]
        missing = template_manager.check_missing(refs)
        if missing:
            txt = "、".join(f"{c}/{m}" if c else m for c, m in missing[:4])
            more = f" 等 {len(missing)} 张" if len(missing) > 4 else ""
            problems.append(f"缺图 {txt}{more}")
            # 每张缺图列"谁在用"——横条一行塞不下，明细留给保存前健康报告
            ref_users = {}
            for n in nodes:
                for r in template_deps.node_tpl_refs(n):
                    ref_users.setdefault(r, []).append(n)
            for c, m in missing:
                users = "、".join(_desc(x) for x in ref_users.get((c, m), [])[:6])
                details.append(f"缺图「{c}/{m}」用在：{users or '（未知节点）'}")

        # 框选区域比模板图还小 = 数学上永不命中（滑窗放不下），编辑期点名。
        # 只核「引用点自带区域」的（节点 roi / 判断模板条件 roi），账本区域不重报。
        tight = []

        def _chk_roi(roi, cat, name, who):
            if not roi or not (cat or name):
                return
            e = template_manager.find_template(cat or "", name)
            if not e:
                return                     # 缺图上面已报，不重复
            try:
                rw, rh = int(roi[2]) - int(roi[0]), int(roi[3]) - int(roi[1])
                if rw < int(e.get("width", 0)) or rh < int(e.get("height", 0)):
                    tight.append(f"{who}（框 {rw}×{rh} < 模板 {e.get('width')}"
                                 f"×{e.get('height')}）")
            except (TypeError, ValueError, IndexError):
                pass

        def _walk_roi(ns):
            for n in ns or []:
                if not isinstance(n, dict):
                    continue
                t = n.get("type")
                if t in ("wait_template", "click_template", "multi_scan"):
                    _chk_roi(n.get("roi"), n.get("category", ""),
                             n.get("name", ""), _desc(n))
                elif t == "branch":
                    from bot import template_deps
                    for cd in template_deps.branch_cond_items(n):
                        if cd.get("kind") == "template":
                            _chk_roi(cd.get("roi"), cd.get("category", ""),
                                     cd.get("name", ""),
                                     f"{_desc(n)}·模板条件")
                _walk_roi(n.get("steps"))
        _walk_roi(nodes)
        if tight:
            problems.append("框选区域比模板图小，永远认不中：" +
                            "、".join(tight[:3]) +
                            ("把框画到能整包住模板图" if tight else ""))
            details.extend(f"区域装不下图：{x}" for x in tight)

        # 孤立节点：入线出线都没有。零入度节点本身就是入口（多入口 = 并行起链，
        # 合法不报）；"所有节点都有入线 = 找不到入口"那种图必然是环，detect_cycle
        # 已报，不重复列。所以这里真正值得提醒的是彻底没连线的孤点。
        if len(nodes) > 1:
            deg = {n["id"]: [0, 0] for n in nodes if n.get("id")}
            for e in edges:
                if e.get("from") in deg:
                    deg[e["from"]][1] += 1
                if e.get("to") in deg:
                    deg[e["to"]][0] += 1
            # watch/timer 哨兵天生无连线（放上即在岗），不算孤点
            dead = [n for n in nodes
                    if n.get("id") and deg.get(n["id"]) == [0, 0]
                    and n.get("type") not in ("watch", "timer")]
            if dead:
                names = "、".join(_desc(n) for n in dead[:3])
                problems.append(f"孤立节点没连线（{names}）")
                details.append("孤立节点：" + "、".join(_desc(n) for n in dead))

        # 必须框的用法缺框防呆：读区域数字不框 = 全屏数字挨个读，读到哪个全靠
        # 运气。编辑期直接点名。（旧「选区选服」的双块口径随用法一并删除：
        # ensure_graph 载入即换算成两个「点文字」节点，各自的框照常可勾可选。）
        nofn = []
        for n in nodes:
            if n.get("type") != "server_pick" or n.get("mode") != "roi_number":
                continue
            if not (n.get("roi") or []):
                nofn.append(f"{_desc(n)}（缺识别区域）")
        if nofn:
            problems.append("该用法必须把识别区域框好：" + "、".join(nofn[:3]))
            details.extend(f"先点该节点，属性面板里「在预览上框选」：{e}"
                           for e in nofn)

        # 发信号防呆：信号名没填 = 白发直通，纯摆设。循环体动作行里的发信号
        # 行一并核（steps 递归）。不做「没人登记」的孤儿信号检查：信号若埋在
        # 子流程里，收它的外层循环在本图之外（父流程才看得见），本图校验只会误报。
        def _walk_sig(ns, sink):
            for n in ns or []:
                if not isinstance(n, dict):
                    continue
                if n.get("type") == "signal" and not str(n.get("name") or "").strip():
                    sink.append(_desc(n))
                _walk_sig(n.get("steps"), sink)
        empty_sig = []
        _walk_sig(nodes, empty_sig)
        if empty_sig:
            problems.append("发信号节点没填信号名（白发直通）：" +
                            "、".join(empty_sig[:3]))
            details.extend(f"发信号没名：{x}（点该节点，属性面板「信号名」填上）"
                           for x in empty_sig)

        # N 口白接防呆：接了 N 口，但节点配置本身让失败路径永不发生 = 死分支。
        # （有原因才报；没原因 = N 口没接线，那是「失败整图判死」的正常语义，不报）
        has_fail = {e.get("from") for e in edges if e.get("port") == "fail"}
        dumb = []
        for n in nodes:
            if n.get("id") not in has_fail:
                continue
            t = n.get("type")
            why = ""
            if t == "wait_template" and float(n.get("timeout", 8) or 0) == 0:
                why = "超时填 0（永不超时）"
            elif t == "click_template" and not bool(n.get("wait_before", True)):
                why = "关了「先等图出现」（不会判失败）"
            elif t == "server_pick" and n.get("on_timeout", "exit") == "continue":
                why = "超时行为=跳过（不会走 N 口）"
            elif (t == "multi_scan" and n.get("mode", "tap") != "enter"
                    and not bool(n.get("dedupe", False))
                    and (not bool(n.get("wait_before", True))
                         or n.get("on_timeout", "fail") == "continue")):
                # 点掉/读数模式只有零命中门会判败（去重开着另有轮数封顶判败）：
                # 关了等门或超时=跳过，N 口成死线。进一个模式另有「返回后迷路」
                # 判败，不算白接。
                why = "点掉/读数模式+不等门或超时跳过（不会判失败）"
            if why:
                dumb.append(f"{_desc(n)}（{why}）")
        if dumb:
            problems.append("N 口永远不会触发：" + "、".join(dumb[:3]))
            details.extend(f"N 口白接：{e}" for e in dumb)

        # 看门狗误杀防呆：看门狗秒数必须 > 图内最慢单步自身超时，否则正常的
        # 慢步骤会被判卡死。timeout=0（不限时等待）配看门狗 = 必误杀，单独点名。
        wd = int(self.graph.get("watchdog_sec", 0) or 0)
        if wd > 0:
            slow = []
            for n in nodes:
                t = n.get("type")
                if t == "loop":
                    # 循环一整轮可能远慢于图内单步：逐行核体内各步的等待时长
                    # 求和，配看门狗时得把轮次一并考虑 —— 无限循环挂机流开看门狗
                    # 基本只会在"某轮里真的没人收口"时才该报警，别按单步口径
                    # 误杀长时间运行的循环体。
                    body_secs = 0.0
                    for row in n.get("steps") or []:
                        if not isinstance(row, dict):
                            continue
                        rt = row.get("type")
                        if rt == "wait_template":
                            secs = float(row.get("timeout", 8) or 0)
                            body_secs += 1e9 if secs == 0 else secs
                        elif rt == "click_template":
                            body_secs += float(row.get("wait_timeout", 8) or 0)
                        elif rt == "popup_zone":
                            body_secs += float(row.get("timeout", 180) or 0)
                    if body_secs >= wd:
                        tail = "（体内有不限时等待）" if body_secs >= 1e9 else \
                               f"（约 {body_secs:g} 秒/轮）"
                        slow.append((n.get("label") or TYPE_LABEL["loop"], tail))
                elif t == "wait_template" and float(n.get("timeout", 8) or 0) == 0:
                    slow.append((n.get("label") or TYPE_LABEL.get(t, "?"), "不限时"))
                elif t in ("wait_template", "server_pick", "branch"):
                    # 默认超时值与 bot/tasks/flow.py _action_step / 图编译侧保持一致
                    dfl = {"wait_template": 8, "server_pick": 15, "branch": 5}[t]
                    secs = float(n.get("timeout", dfl) or 0)
                    if (t in ("server_pick", "branch")
                            and n.get("on_timeout") == "scroll"):
                        # 滑动找：整步最坏 = 每个识别窗口 timeout ×(滑次+1)
                        # + 每回停稳时间，远大于单个 timeout，按总账核看门狗
                        secs = secs * (int(n.get("scroll_max", 5) or 0) + 1) \
                            + float(n.get("scroll_settle", 0.8) or 0.8) \
                            * int(n.get("scroll_max", 5) or 0)
                    if secs >= wd:
                        slow.append((n.get("label") or TYPE_LABEL.get(t, "?"),
                                     f"{secs:g} 秒"))
                elif t == "click_template" and bool(n.get("wait_before", True)):
                    secs = float(n.get("wait_timeout", 8) or 0)   # 前置门超时
                    if secs >= wd:
                        slow.append((n.get("label") or TYPE_LABEL.get(t, "?"),
                                     f"{secs:g} 秒"))
                elif t == "multi_scan":
                    # 只按前置门+每处迷路确认核总账（体步时长同「子流程不钻」惯例）
                    secs = (float(n.get("wait_timeout", 8) or 0)
                            if bool(n.get("wait_before", True)) else 0.0)
                    if n.get("mode", "tap") == "enter":
                        secs += float(n.get("confirm_timeout", 5) or 0) \
                            + float(n.get("settle_secs", 0.8) or 0.8)
                    if secs >= wd:
                        slow.append((n.get("label") or TYPE_LABEL.get(t, "?"),
                                     f"{secs:g} 秒"))
                # ponytail: sleep 节点不计入 —— 显式等待时长用户自己控，
                # 真设了 600s 睡觉也活该被看门狗盯上（子流程内步同理不钻）
            if slow:
                problems.append("看门狗会误杀慢步骤：" +
                                "、".join(f"{nm}{s}" for nm, s in slow[:3]) +
                                "（看门狗秒数要大于所有步骤的超时）")
                details.extend(f"{nm}{s}：已达/超过看门狗 {wd} 秒，会被判卡死"
                               for nm, s in slow)

        # 真编译体检（与保存/试运行/tools/flow_apply 同一入口）：上面几项全是
        # "相面"判据，缺 id / 非 dict 节点这类结构错过得了所有关、编译当场炸。
        # deepcopy 当防火墙：校验只读，绝不让引擎的 fixup 顺手改写内存图。
        # ponytail: 每次全库 deepcopy 检查，百级流程毫秒量级；真慢了再只钻本图
        try:
            key = self.flow_name or "_canvas"
            probe = copy.deepcopy(self._flows() or {})
            probe[key] = copy.deepcopy(self.graph)
            compile_graph(probe[key], probe, flow_name=key)
        except Exception as e:
            problems.append(f"编译不过：{type(e).__name__}: {e}")
            details.append("编译异常（与保存/试运行同一入口），先修报错指到的节点再保存")

        if problems:
            self.lbl_valid.setText("⚠ 流程待检查：" + "　｜　".join(problems))
            self.lbl_valid.setToolTip("\n".join(problems + details))
            self.lbl_valid.setVisible(True)
        else:
            self.lbl_valid.setVisible(False)

    def _on_watchdog_changed(self, secs: int):
        """工具栏看门狗框：写进 graph 顶层 watchdog_sec（0=不写键，保持老图干净）。"""
        if secs > 0:
            self.graph["watchdog_sec"] = int(secs)
        else:
            self.graph.pop("watchdog_sec", None)
        self._mark_dirty()   # 内部会补排校验（误杀防呆）+ 自动保存

    def _on_autosave_toggled(self, on):
        self.cfg.set("ui.flow_autosave", on)
        # 勾选时把攒下的未保存改动立刻落进去，别留"以为存了其实没存"的缝
        if on and self._dirty:
            self._on_save(quiet=True)

    def _add_to_main(self, t: str, scene_pos=None):
        """往画布加一个节点。scene_pos 给定（双击空白就地建）就落在那儿，
        否则落视口中心 + 阶梯偏移防叠死（原行为）。"""
        if self._guard_edit("添加节点"):
            return          # 上锁旁路③：「新增节点」按钮禁了，双击空白/Tab 弹框这条路没禁
        if not self._push_undo():
            return
        node = new_node(t)
        if scene_pos is None:
            vp = self.view.viewport().rect()
            # mapToScene 直接收 QPoint；不绕 QPoint-QPointF（Qt 减法运算符求值会抛 SyntaxError，静默中断整个添加流程）
            center = self.view.mapToScene(vp.center())
            off = len(self.graph["nodes"]) * 30
            node["x"] = int(center.x() + off)
            node["y"] = int(center.y() + off)
        else:
            node["x"] = int(scene_pos.x() - NODE_W // 2)   # 让节点中心对准双击点
            node["y"] = int(scene_pos.y() - NODE_H // 2)
        self.graph["nodes"].append(node)
        self._mark_dirty()
        self._rebuild_scene()
        it = self._node_items.get(node["id"])
        if it:
            it.setSelected(True)

    def _on_add_at(self, scene_pos, global_pos):
        """双击空白 / Tab：在光标处弹可搜索加节点框（市面节点编辑器同款），
        选完落在触发点上。点框外/Esc 取消 = 什么都不加。"""
        if self._guard_edit("添加节点"):
            return          # 上锁就别弹框骗人（选完也加不上）
        dlg = _NodeSearchPopup(self, _palette_items(), global_pos)
        dlg.exec()
        if dlg.chosen:
            self._add_to_main(dlg.chosen, scene_pos=scene_pos)

    def _delete_selected(self):
        # 走画布删除（连带边），删属性面板按钮也复用
        self._on_delete_from_canvas()

    def _selected_item(self):
        items = [i for i in self.scene.selectedItems() if isinstance(i, FlowNodeItem)]
        return items[0] if items else None

    # 单节点测试的禁测名单：subflow 单测=把整条子流程又跑一遍（不叫单测）；
    # watch/timer 哨兵没有入边、天生常驻，单独跑永远 running 出不来结果。
    _NO_SOLO_TEST = ("subflow", "watch", "timer")

    def _solo_test_node(self):
        """当前可单测的选中节点 dict；不可测（没选/多选/名单内）返回 None。"""
        items = [i for i in self.scene.selectedItems() if isinstance(i, FlowNodeItem)]
        if len(items) != 1:
            return None
        node = items[0].node
        return None if node.get("type") in self._NO_SOLO_TEST else node

    def _refresh_test_one_btn(self):
        # 试运行锁定期（view 禁用）没得选新节点，按钮跟着灰
        ok = self.view.isEnabled() and self._solo_test_node() is not None
        self.btn_test_one.setEnabled(ok)
        # 「从此节点开始测」不挑类型：watch/timer 只是没有"开始"的概念，
        # 选它们起跑 = 常驻不退出，没有其它坑，所以不做禁测名单。
        self.btn_test_from.setEnabled(
            self.view.isEnabled() and self._selected_item() is not None)

    def _on_test_one(self):
        node = self._solo_test_node()
        if node is None:
            t = (self._selected_item().node.get("type")
                 if self._selected_item() else None)
            why = {"subflow": "子流程节点单测=再跑整条子链，请直接「测试流程」或测子流程本身",
                   "watch": "并行监视天生常驻，单测永远跑不完（放画布上即已在岗）",
                   "timer": "定时执行天生常驻，单测永远跑不完"}.get(t)
            self._set_status(why or "先单选一个可测的节点（子流程/监视/定时不支持单测）")
            return
        self.test_one_requested.emit(node)

    def _on_test_from(self):
        node = self._solo_test_node()
        if node is None:
            self._set_status("先单选一个节点再点「从此节点开始测」")
            return
        self.test_from_requested.emit(node)

    def _reselect_node(self, node):
        item = self._find_item(node)
        if item is not None:
            item.setSelected(True)

    def _find_item(self, node):
        """按节点 dict 身份找回画布上对应的方块。"""
        for i in self.scene.items():
            if isinstance(i, FlowNodeItem) and i.node is node:
                return i
        return None

    def _repaint_node(self, node):
        """只重绘一个方块（改备注时用，避免整张画布重建）。"""
        item = self._find_item(node)
        if item is not None:
            item.update()

    def _on_label_changed(self, node, txt):
        node["label"] = txt
        self._mark_dirty()
        self._repaint_node(node)

    # ── 属性面板 ──
    def _on_selection_changed(self):
        # 框选每动一下鼠标 Qt 都实时刷选中集 → selectionChanged 一拖几十发，
        # 而属性表单重建很贵（清空 + 灌模板下拉 + 保真回填），逐发重建 = 卡顿。
        # 防抖：每次只重启 80ms 计时器，选中集停下来才真正刷一次。
        self._sel_timer.start()

    def _apply_selection_props(self):
        try:
            item = self._selected_item()
        except RuntimeError:
            # 窗口析构期：C++ 侧 scene 已销毁，Qt 拆树时清选区仍会甩一发
            # selectionChanged。此时重建属性面板毫无意义，直接吞掉。
            return
        self._update_align_buttons()
        self._refresh_test_one_btn()
        self._rebuild_props(item.node if item else None)

    def _clear_props(self):
        self._watch_stats_hook = None   # 面板拆建：监视轻刷新通道跟着摘（防投旧控件）
        # 递归清空：属性表单是用 addLayout 挂的嵌套布局（QFormLayout / QHBoxLayout），
        # 只 takeAt 顶层会漏掉子布局里的控件 —— 那些控件 parent 仍是 props_panel，
        # 脱离布局后按上次几何继续显示，选一次节点就叠一层。必须连子布局带控件一起删。
        # ⚠ 控件只 hide + deleteLater，绝不 setParent(None)：parent 一断它立刻变成
        # 顶层窗口，deleteLater 要等事件循环下一轮才回收 —— 框选拖动每动一下鼠标
        # 都重建一次表单，屏幕上就闪过一片孤儿小窗（实测踩坑）。hide 同样能防
        # "脱离布局后按旧几何继续显示"，且永远不脱离父窗口。
        def _take(item):
            if item is None:
                return
            if sub := item.layout():
                while sub.count():
                    _take(sub.takeAt(0))
                sub.setParent(None)
                sub.deleteLater()
            if w := item.widget():
                w.hide()
                w.deleteLater()
        while self.props_layout.count():
            _take(self.props_layout.takeAt(0))

    def _rebuild_props(self, node: dict | None):
        self._clear_props()
        # 选中节点换了（含取消选中）：多点取色点选会话绑的是旧条件，随表单
        # 一起结束，防后续单击把点记进已不可见的旧列表（同节点重建不受影响——
        # route_multi_point 记完色就是拿原节点重建本表单刷新列表）。
        if node is not self._multi_pick_node:
            self._end_multi_pick()
        # 构建期闸门：建表单过程中 combo/子面板回调触发的 _mark_dirty 全部忽略，
        # 只有构建完成后用户的真实编辑才算脏（见 _mark_dirty）。
        self._building_props = True
        try:
            self._fill_props(node)
        finally:
            self._building_props = False

    def _fill_names_fidelity(self, cmb_name: QComboBox, target: dict, cat: str):
        """模板下拉保真重建（点/等模板、打印 target、判断 cond 三处共用）。
        程序性 clear/addItems 会同步甩 currentTextChanged，以前把存好的选择
        覆写成库内第一项（用户选好的图被"偷梁换柱"）。重建期间屏蔽信号：
        存过的名字还在库里就原样保留；确实没有（第一次选库/图已删）才落
        第一项并写回，保证下拉显示与节点数据一致。"""
        orig = target.get("name", "")
        names = [x["name"] for x in template_manager.list_templates()
                 if x["category"] == cat]
        cmb_name.blockSignals(True)
        cmb_name.clear()
        cmb_name.addItems(names)
        if orig in names:
            cmb_name.setCurrentText(orig)
        else:
            target["name"] = names[0] if names else ""
        cmb_name.blockSignals(False)
        target["category"] = cat
        # 名字可能被落为第一项（信号屏蔽中，回调不会跑），画布缩略图跟着刷。
        # 只在重建表单/切库时发生，整 scene 刷一次代价可忽略。
        self.scene.update()
        self._mark_dirty()

    def _add_cat_name_rows(self, f: QFormLayout, target: dict,
                           repaint_for: dict | None = None):
        """归属+模板下拉对的全套四件（点/等模板、子流程门、打印 target、
        分支 cond 四处共用）：建控件 → addRow → 切库重灌 → 改名写回+标脏+刷缩略图。
        repaint_for = 改模板名后要刷画布缩略图的节点（分支条件传宿主节点，
        其余默认 target 本身）。返回 (cmb_cat, cmb_name)，「去采集」按钮要拿前者。"""
        host = repaint_for or target
        cmb_cat = QComboBox()
        _fill_category_combo(cmb_cat, target.get("category", ""))
        cmb_name = QComboBox()
        f.addRow("归属", cmb_cat)
        f.addRow("模板", cmb_name)
        cmb_cat.currentIndexChanged.connect(
            lambda _=False, c=cmb_cat: self._fill_names_fidelity(cmb_name, target, c.currentData()))
        cmb_name.currentTextChanged.connect(
            lambda nm, t=target: (t.__setitem__("name", nm), self._mark_dirty(),
                                  self._repaint_node(host)))
        self._fill_names_fidelity(cmb_name, target, cmb_cat.currentData())
        return cmb_cat, cmb_name

    def _flow_combo_row(self, form: QFormLayout, node: dict):
        """子流程下拉行（子流程面板与「遍历全部·进一个」共用一份）：
        自己的名字垫不进下拉（自引用 = 环）；存量残值（被删的流程）照旧
        垫最前可见，避免下拉"偷梁换柱"改存值（同 _fill_names_fidelity 的坑）。
        选中即写 node["flow_name"] 并刷画布缩略。"""
        cmb_flow = QComboBox()
        cur_f = (node.get("flow_name") or "").strip()
        opts = [f for f in self._flows() if f != self.flow_name]
        if cur_f and cur_f not in opts:
            opts.append(cur_f)   # 已删流程的残留值垫末尾兜住（灌法内部处理）
        _fill_flow_grouped(cmb_flow, opts, cur_f)
        cmb_flow.currentIndexChanged.connect(
            lambda _=False, n=node, c=cmb_flow: (n.__setitem__("flow_name", c.currentData()),
                                           self._mark_dirty(), self._repaint_node(n)))
        form.addRow("子流程", cmb_flow)
        return cmb_flow

    def _jit_rows(self, form, node):
        """落点抖动勾选框 + 半径[下限,上限]行（点模板/点坐标共用）。
        存储：jitter_px_min / jitter_px_max；不勾 = 两键清 0（= 固定落点）。
        老节点单键 jitter_px 仍被编译侧认（[0,r] 圆内），勾开时自动迁移到区间键。"""
        legacy = int(node.get("jitter_px", 0) or 0)
        jmn = int(node.get("jitter_px_min", 0) or 0)
        jmx = int(node.get("jitter_px_max", 0) or 0) or legacy
        chk = QCheckBox("随机落点抖动")
        spn_min = QSpinBox()
        spn_min.setRange(0, 20)
        spn_min.setValue(jmn)
        spn_max = QSpinBox()
        spn_max.setRange(0, 20)
        spn_max.setValue(jmx if jmx > 0 else 5)

        def _min_changed(v, n=node, m=spn_max):
            n["jitter_px_min"] = v
            m.setMinimum(v)          # 下限抬过上限时 Qt 自动钳位并触发 _max_changed
            self._mark_dirty()

        def _max_changed(v, n=node):
            n["jitter_px_max"] = v
            self._mark_dirty()

        spn_min.valueChanged.connect(_min_changed)
        spn_max.setMinimum(spn_min.value())
        spn_max.setValue(max(spn_max.value(), spn_min.value()))
        spn_max.valueChanged.connect(_max_changed)
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("半径(px)"))
        rl.addWidget(spn_min)
        rl.addWidget(QLabel("~"))
        rl.addWidget(spn_max)

        def _sync(on, n=node, r=row):
            # 半径行常显（"固定显示"），不勾只是禁用变灰 —— 参数一直在眼里，
            # 勾选切换落点模式，不用面板忽高忽低
            for w in (spn_min, spn_max):
                w.setEnabled(on)
            if on:
                if n.get("jitter_px"):
                    n.pop("jitter_px")          # 勾选即完成迁移，单键退役
                    self._mark_dirty()
                if not n.get("jitter_px_max"):
                    n["jitter_px_max"] = spn_max.value()   # 刚勾上：默认值落盘
                    self._mark_dirty()
            elif n.get("jitter_px_min") or n.get("jitter_px_max"):
                n["jitter_px_min"] = 0
                n["jitter_px_max"] = 0
                self._mark_dirty()

        chk.toggled.connect(_sync)
        chk.setChecked(jmx > 0)
        form.addRow(chk)
        form.addRow(row)
        _sync(jmx > 0)
        return chk, row    # 遍历全部文字源要把抖动整对置灰（其余调用方不接无妨）

    def _roi_block(self, form, target, key, title, tip, optional,
                   owner=None, chk_text=None, none_text=None, embed_chk=False,
                   chk_tip=None):
        """一组「限定区域」编辑（整块占一个表单行）：摘要行「已框 x0,y0 → x1,y1」
        + 铅笔钮展开 4 坐标 + 预览框选按钮。区域写进 target[key]（节点或 cond
        字典皆可 —— 同一张图在不同引用点各框各的），框选回填后重建 owner 的表单。
        optional = 再垫一层「限定区域」勾选（不勾 = 该键存 null，编译侧兜底）。
        embed_chk = 勾选框塞进返回的 box 里（整块自包含，不占 form 行，
        方便嵌进别的容器随块显隐 —— 见 _scroll_editor 的「限定滑动区域」）。
        由 server_pick 专用闭包升格而来（2026-09 区域跟引用走：判断模板条件/
        等图/点模板共用这一份，别再长第二套）。"""
        chk_text = chk_text or "限定识别区域（不勾 = 全屏）"
        none_text = none_text or "未框（= 全屏识别）"
        chk = None
        if optional and not embed_chk:
            chk = QCheckBox(chk_text)
            chk.setChecked(bool(target.get(key)))
            form.addRow(chk)

        def _vals():
            return target.get(key) or [0, 0, BASE_W, BASE_H]

        def _sum_text():
            # 没框过就直说没框：摘要垫默认全屏坐标装"已框"，用户会以为
            # 限了区域，实际运行时是全屏识别（老面板四根默认值 spinbox
            # 同款错觉，连壳一起改）
            cur = target.get(key)
            if not cur:
                return f"{title}：{none_text}" if optional \
                    else f"{title}：未框（该用法必须框）"
            return f"{title}：已框 {cur[0]},{cur[1]} → {cur[2]},{cur[3]}"

        box = QWidget()
        blay = QVBoxLayout(box)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(2)
        if optional and embed_chk:
            chk = QCheckBox(chk_text)
            chk.setChecked(bool(target.get(key)))
            blay.addWidget(chk)
        # 摘要行：一行看清框在哪（数据是真相源，框完重建即刷新）
        lbl = QLabel(_sum_text())
        lbl.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_MUTED};")
        btn_edit = _icon_btn(icons.EDIT, "手改坐标")
        sum_row = QWidget()
        srow = QHBoxLayout(sum_row)
        srow.setContentsMargins(0, 0, 0, 0)
        srow.addWidget(lbl)
        srow.addWidget(btn_edit)
        srow.addStretch()
        blay.addWidget(sum_row)
        # 折叠详情：4 坐标横排一行（label 走 tooltip —— 2×2 网格塞
        # 四个 3 位数控件 + 列标签，318 视口装不下，实测坑）
        det = QWidget()
        dlay = QHBoxLayout(det)
        dlay.setContentsMargins(0, 0, 0, 0)
        dlay.setSpacing(4)
        spns = []
        for i, (ck, mx) in enumerate((("左", BASE_W), ("上", BASE_H),
                                      ("右", BASE_W), ("下", BASE_H))):
            spn = QSpinBox()
            spn.setRange(0, mx)
            spn.setValue(int(_vals()[i]))
            spn.setToolTip(f"区域{ck}")
            spn.setAccessibleName(f"区域{ck}")
            spns.append(spn)
            dlay.addWidget(spn)
        blay.addWidget(det)
        det.setVisible(False)
        btn_edit.clicked.connect(
            lambda _=False, b=det: b.setVisible(not b.isVisible()))

        def _set(i, val, n=target, k=key):
            c = n.get(k) or [0, 0, BASE_W, BASE_H]
            c[i] = int(val)
            n[k] = c
            lbl.setText(_sum_text())
            self._mark_dirty()

        for i, spn in enumerate(spns):
            spn.valueChanged.connect(lambda val, idx=i: _set(idx, val))

        btn = _fit_btn(QPushButton(f"在预览上框选（{title}）"))
        btn.setToolTip(tip)
        btn.clicked.connect(
            lambda _=False, n=target, o=owner, k=key: self.begin_roi_pick(
                n, owner=o, key=k))
        blay.addWidget(btn)

        if chk is not None:
            def _sync(on, sps=spns, bt=btn, lb=lbl, ebtn=btn_edit,
                      det_=det):
                for s in sps + [bt, lb, ebtn]:
                    s.setEnabled(on)
                if not on:
                    det_.setVisible(False)   # 勾掉了别留半开的坐标行
                lbl.setText(_sum_text())

            chk.toggled.connect(
                lambda on, n=target, k=key: (
                    n.__setitem__(k,
                                 list(n.get(k) or [0, 0, BASE_W, BASE_H])
                                 if on else None),
                    self._mark_dirty(), _sync(on)))
            _sync(bool(target.get(key)))
        if chk is not None and chk_tip:
            chk.setToolTip(chk_tip)
        return box

    def _scroll_editor(self, node, form):
        """滑动翻页参数行（识字取物与判断节点同款口径）：方向/距离/次数/
        停顿 2×2 带小标签，改动直写节点 scroll_* 键；再垫一组「限定滑动区域」
        （复用 _roi_block 同一份框选机制，写 scroll_roi 键，不框 = 全屏滑 =
        老行为，列表不在屏幕正中时免得误触顶栏底栏）。显隐由调用方控制
        （构建期无父，只许走 _vis）。返回 (参数行, 提示行, 区域块控件)。"""
        scr_row = QWidget()
        # 四控件横排一行实测 348px > 318 视口（出横向滑块，违反节点面板
        # 排版规矩）：拆 2×2 带小标签。
        slay = QGridLayout(scr_row)
        slay.setContentsMargins(0, 0, 0, 0)
        slay.setHorizontalSpacing(4)
        cmb_dir = self._cbo(str(node.get("scroll_dir", "up")),
                            [("向上滑", "up"), ("向下滑", "down"),
                             ("向左滑", "left"), ("向右滑", "right")],
                            node, "scroll_dir")
        spn_spx = self._spin(int(node.get("scroll_px", 300) or 300),
                             50, 900, node, "scroll_px", suffix="px")
        spn_smax = self._spin(int(node.get("scroll_max", 5) or 5),
                              1, 30, node, "scroll_max", suffix="次")
        spn_sset = self._dspin(node.get("scroll_settle", 0.8) or 0.8,
                               0.2, 5.0, node, "scroll_settle",
                               decimals=1, step=0.1, suffix="s")
        spn_sset.setToolTip("滑一下后等画面停稳再认（识别是半秒一张的重活，\n"
                            "动画中硬认 = 白跑还拖节拍）。慢电脑/长列表调大点。")
        slay.addWidget(QLabel("方向"), 0, 0)
        slay.addWidget(cmb_dir, 0, 1)
        slay.addWidget(QLabel("距离"), 0, 2)
        slay.addWidget(spn_spx, 0, 3)
        slay.addWidget(QLabel("次数"), 1, 0)
        slay.addWidget(spn_smax, 1, 1)
        slay.addWidget(QLabel("停顿"), 1, 2)
        slay.addWidget(spn_sset, 1, 3)
        slay.setColumnStretch(1, 1)
        slay.setColumnStretch(3, 1)
        # 限定滑动区域：与识别区域同一份 _roi_block（embed_chk 把勾选框收进
        # 块内，整块一个控件，调用方好随「认不出时=滑动」一起显隐）。
        # 不勾/不框 = scroll_roi 无值 = 全屏滑，与老行为逐字一致。
        roi_box = self._roi_block(
            form, node, "scroll_roi", "滑动区域",
            "列表嵌在屏幕中间、上下有固定栏时，在预览上框住列表区再滑：\n"
            "起落点都钳在框内，免得误触顶栏底栏。不框 = 全屏滑（老行为）。",
            optional=True, owner=node, embed_chk=True,
            chk_text="限定滑动区域（不勾 = 全屏滑动）",
            none_text="未框（= 全屏滑动）")
        scr_hint = _hint("滑了设定次数仍没认出 → 按认不出收口（走 N 口/退出）。\n"
                         "距离=每次滑多远(px)，次数=最多滑几回，停顿=滑完等画面稳；"
                         "框了「滑动区域」就只在框内滑。")
        return scr_row, scr_hint, roi_box

    def _fill_props(self, node: dict | None):
        if node is None:
            # 没选中节点 = 空档期，整块面板拿来讲画布手势和操作流程；
            # 节点类型一行从 NODE_GROUPS/TYPE_LABEL 现拼，不另存一份清单
            types = "　".join(
                f"<b>{g}</b>：{'、'.join(TYPE_LABEL[t] for t in ts)}"
                for g, ts in NODE_GROUPS)
            tip = QLabel(
                "<div style='font-size:12px; line-height:150%;'>"
                "<b style='font-size:14px;'>画布操作</b><br>"
                "滚轮 —— 缩放（滚哪缩哪）<br>"
                "右键按住拖 —— 平移画布<br>"
                "左键拖空白 —— 框选多个节点<br>"
                "双击空白 / Tab —— 弹搜索框就地加节点（打字即筛）<br>"
                "拖节点 —— 自动对齐吸附 + 红色参考线<br>"
                "节点右缘小圆 → 拖到目标节点 = 连线（靠近入点会磁吸+亮环）；双击线 = 删线<br>"
                "Ctrl+Z/Y 撤销重做　Ctrl+C/V/X/D 复制/粘贴/剪/再制<br>"
                "Ctrl+A 全选　Esc 取消选中<br>"
                "「整理布局」—— 按连线一键把节点摆整齐<br>"
                "<br>"
                "<b style='font-size:14px;'>基本流程</b><br>"
                "① 顶部新建或选一条流程<br>"
                "② 「新增节点」或双击空白，往画布放节点<br>"
                "③ 从节点右缘出点拉线到下一节点，串成执行顺序<br>"
                "　（判断/子流程有 Y、N 两个出点，按结果分流）<br>"
                "④ 点选节点 → 本面板填参数（悬停节点类型名可看详细说明）<br>"
                "⑤ 「保存流程」，或「▶ 测试流程」在当前模拟器实例直接试跑<br>"
                "<br>"
                "📖 完整制作指南：项目文件夹 <b>docs/flow-authoring-guide.md</b>"
                "（从截图到跑通，零代码）<br>"
                "<br>"
                f"<b style='font-size:14px;'>节点类型</b><br>{types}<br>"
                "<span style='color:#888;'>点选画布上的节点即可在此编辑参数</span>"
                "</div>")
            tip.setWordWrap(True)
            tip.setTextFormat(Qt.RichText)
            tip.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self.props_layout.addWidget(tip)
            self.props_layout.addStretch()
            return

        t = node.get("type", "")
        form = QFormLayout()
        head = QLabel(f"<b>{TYPE_LABEL.get(t, t)} 节点</b>")
        head.setToolTip(NODE_HELP.get(t, ""))   # 调参时悬停标题也能看说明
        form.addRow(head)

        # label（通用）
        edt_label = QLineEdit(node.get("label", ""))
        edt_label.setPlaceholderText("备注名（可选）")
        edt_label.textChanged.connect(
            lambda txt, n=node: self._on_label_changed(n, txt))
        form.addRow("备注", edt_label)

        # 类型分发（2026-10-09 收口：原来 800+ 行 if/elif 链拆成每型一个
        # _props_<type>(form, node) 方法，加类型=加方法，这条链不再动。
        # 未知类型照旧：只有抬头和备注行。
        h = getattr(self, "_props_" + t, None)
        if h is not None:
            h(form, node)
        self.props_layout.addLayout(form)

        # 底部：删除节点（图模型位置自由，没有上移/下移概念）。
        # 图标 + 文字 + 自然宽度：原来被 QHBoxLayout 拉成 306px 的横幅，
        # 和面板里其他控件不成比例；收紧到内容宽，危险语义靠字形和 tooltip。
        row = QHBoxLayout()
        btn_del = QPushButton("删除节点")
        btn_del.setIcon(icons.glyph_icon(icons.DELETE))
        btn_del.setIconSize(QSize(14, 14))
        btn_del.setToolTip("从画布上删掉当前选中的节点（连同它的连线）；\n"
                           "Ctrl+Z 可撤销。")
        _fit_btn(btn_del)
        btn_del.clicked.connect(self._delete_selected)
        row.addWidget(btn_del)
        row.addStretch()          # 不给按钮再被拉长的机会
        self.props_layout.addLayout(row)
        self.props_layout.addStretch()

    def _props_click_template(self, form, node, host=None):
        self._props_template(form, node, host)

    def _props_wait_template(self, form, node, host=None):
        self._props_template(form, node, host)


    def _props_template(self, form, node, host=None):
        # 本型编辑区（画布与循环体行共用；host=画布重绘/取点归属，缺省=node 自己）
        host = host or node
        t = node.get("type")
        # host = 画布重绘/回程票/框选重建的宿主：画布节点=自己；循环体行=所在节点
        slot = None if host is node else node
        cmb_cat, _cmb_name = self._add_cat_name_rows(form, node, repaint_for=host)
        # 「去采集」联动：缺图时一键跳采模板模式并预填归属目录，
        # 并把本节点记成回程目的地（采完自动填回这里）
        form.addRow(self._goto_capture_btn(cmb_cat, host, cond=slot))
        # 区域跟着节点走（不碰模板账本）：框了 = 只在框内找这张图，
        # 不框 = 跟随账本默认区域（存量节点零影响）。点全部同样只在框内收。
        form.addRow(self._roi_block(
            form, node, "roi", "搜索区域",
            "在左侧预览上点两下框住要查的那一片，框完自动回填。\n"
            "同一张图在屏幕多处出现、含义不同时（如列表每行一个的图标），"
            "用这个锁定要查的那一处。",
            optional=True, owner=host,
            chk_text="限定搜索区域（不框 = 跟随模板默认）",
            none_text="未框（= 跟随模板默认区域）"))

        if t == "click_template":
            # 相对偏移：命中中心 +(dx,dy) 像素再点。图标在左、真正要点的
            # 是旁边文字/热区时用。默认 0 = 点中心（老节点零影响）。
            form.addRow("偏移X", self._spin(node.get("offset_x", 0), -(BASE_W - 1), BASE_W - 1, node, "offset_x"))
            form.addRow("偏移Y", self._spin(node.get("offset_y", 0), -(BASE_H - 1), BASE_H - 1, node, "offset_y"))
            form.addRow(self._pick_btn(node, "offset", "点选目标位算偏移", owner=host))
            self._jit_rows(form, node)
            # 点全部：同屏 N 处同一个图一个节点逐个点完（每拍一个，点完收口）。
            # 没找到图时的等待/超时走下面「点击前先等模板出现」同一套设置。
            chk_all = QCheckBox("点全部（同屏多处命中逐个点完）")
            chk_all.setToolTip(
                "勾选后这一步把当前帧里该模板的所有达标命中按上→下、左→右"
                "逐个点完才走下一步（每拍只点一个，不抢引擎落手红线）。\n"
                "「最多点数」防误匹配把全屏当目标刷；「点击间隔」是两次落手的"
                "最短时间。\n"
                "偏移照旧逐处生效；模板账本里设的固定点击坐标会被忽略"
                "（N 个目标共用一个点无意义，运行时日志点名）。")
            chk_all.setChecked(bool(node.get("all_hits", False)))
            spn_all_max = self._spin(int(node.get("all_hits_max", 10) or 10),
                                     1, 50, node, "all_hits_max")
            spn_all_gap = self._dspin(node.get("all_hits_gap", 0.3),
                                      0.0, 5.0, node, "all_hits_gap",
                                      decimals=1, step=0.1, suffix=" s")

            def _sync_all(on, a=spn_all_max, b=spn_all_gap):
                a.setEnabled(on)
                b.setEnabled(on)

            chk_all.toggled.connect(
                lambda on, n=node: (n.__setitem__("all_hits", on),
                                     self._mark_dirty(), _sync_all(on)))
            form.addRow(chk_all)
            form.addRow("最多点数", spn_all_max)
            form.addRow("点击间隔", spn_all_gap)
            _sync_all(bool(node.get("all_hits", False)))
            # 前置门（编译侧 _action_step 的 gate）：图没出现不落手。
            # 缺省开 —— 老节点没这字段也自动获得"等图再点"。
            chk_wait = QCheckBox("点击前先等模板出现")
            chk_wait.setChecked(bool(node.get("wait_before", True)))
            # 0 = 图不出现就一直等
            spn_wait = self._dspin(node.get("wait_timeout", 8), 0, 300,
                                   node, "wait_timeout",
                                   special="不限时", cast=int)
            cmb_ot = self._on_timeout_combo(node)

            def _sync(on, a=spn_wait, b=cmb_ot):
                # 关掉等待后超时设置无意义，置灰防"配了却没生效"的错觉
                a.setEnabled(on)
                b.setEnabled(on)

            chk_wait.toggled.connect(
                lambda on, n=node: (n.__setitem__("wait_before", on),
                                     self._mark_dirty(), _sync(on)))
            form.addRow(chk_wait)
            form.addRow("等图超时(s)", spn_wait)
            form.addRow("超时行为", cmb_ot)
            _sync(bool(node.get("wait_before", True)))
            # 点击后加延迟：点完多等「常驻延时」（全局一个值，功能设置页
            # 调，运行时现读 config——改了立即对所有勾选的节点生效）。
            # 缺省勾上（与「点击前先等模板出现」同款：老节点没这键也默认开，
            # 读侧和界面两侧同口径，显示与实际不漂移）；取消勾选才写 False。
            # 勾选只存布尔，延时值不进流程（防漂移）。
            from core.config_manager import get_config as _gc
            _pd = float(_gc().get("general.post_click_delay", 1.0) or 0)
            chk_dly = self._chk(node.get("delay_after_click", True), node,
                                "delay_after_click", "点击后加延迟")
            chk_dly.setToolTip(
                f"点完这一步后多等 {_pd:g} 秒再走下一步（给游戏加载/动画留余量）。\n"
                "延时时长是全局「点击常驻延时」，在主界面「功能设置」页修改；\n"
                "默认勾上，个别不想等的节点取消勾选即可。")
            form.addRow(chk_dly)

        if t == "wait_template":
            # 0 = 图不出现就一直等
            spn_time = self._dspin(node.get("timeout", 8), 0, 300, node,
                                   "timeout", special="不限时", cast=int)
            form.addRow("超时(s)", spn_time)
            form.addRow(self._chk(node.get("exists", True), node, "exists",
                                  "等待出现（否则等待消失）"))
            # 超时行为以前只有编译侧支持、编辑器没入口，用户永远只能吃默认 fail
            form.addRow("超时行为", self._on_timeout_combo(node))
            # 命中后还要连续看到这么久才算数（防闪烁误判）；0=命中即算
            spn_confirm = self._dspin(node.get("confirm_secs", 0.6), 0, 30,
                                      node, "confirm_secs", decimals=1,
                                      step=0.1, special="命中即算")
            form.addRow("稳定确认(s)", spn_confirm)

    def _props_click(self, form, node, host=None):
        host = host or node
        spn_x = self._spin(node.get("tap_x", 0), 0, BASE_W - 1, node, "tap_x")
        spn_y = self._spin(node.get("tap_y", 0), 0, BASE_H - 1, node, "tap_y")
        form.addRow("x", spn_x)
        form.addRow("y", spn_y)
        form.addRow(self._pick_btn(node, "click", owner=host))
        self._jit_rows(form, node)
        # F3 坐标变量：把写死的 x/y 换成「流程变量的坐标」——
        # 配方 [识字取物 读坐标→变量 放技能] → [点坐标 坐标={放技能}]。
        # 留空 = 用上面的固定坐标（存量节点逐字不变，绝不写空键）。
        edt_coord = QLineEdit(str(node.get("coord", "")))
        edt_coord.setPlaceholderText("如 {放技能}、坐标变量（值为 (x,y) 或 \"x,y\"）")
        edt_coord.setToolTip(
            "一个表达式，求值结果须是坐标：元组 (x,y)（『识字取物·读坐标』"
            "存的那种）\n或含逗号的字符串 \"123,456\"。\n"
            "填了就忽略上面的固定 x/y（下面的 x/y 表达式优先于此）。\n"
            "⚠ 变量没读到 / 算不出坐标 = 这一拍不点、只告警，绝不瞎点。")
        edt_coord.textChanged.connect(
            lambda v, n=node: (self._set_opt_key(n, "coord", v), self._mark_dirty()))
        form.addRow("坐标", _acct_token_row(edt_coord))
        edt_ex = QLineEdit(str(node.get("x_expr", "")))
        edt_ex.setPlaceholderText("x 表达式，如 100+5、{X坐标}")
        edt_ex.setToolTip("单轴 x 表达式（变量名直写或算术）。\n"
                          "与下面的 y 表达式成对填，优先于上面的「坐标」。")
        edt_ex.textChanged.connect(
            lambda v, n=node: (self._set_opt_key(n, "x_expr", v), self._mark_dirty()))
        edt_ey = QLineEdit(str(node.get("y_expr", "")))
        edt_ey.setPlaceholderText("y 表达式，如 {Y坐标}")
        edt_ey.setToolTip("单轴 y 表达式（变量名直写或算术）。\n"
                          "与上面的 x 表达式成对填；只填一个 = 本步跳过并告警。")
        edt_ey.textChanged.connect(
            lambda v, n=node: (self._set_opt_key(n, "y_expr", v), self._mark_dirty()))
        form.addRow("x 表达式", edt_ex)
        form.addRow("y 表达式", edt_ey)
        form.addRow(_hint("留空 = 用上面的固定坐标。\n"
                          "「坐标」= 一个值为 (x,y) 或 \"x,y\" 的变量/表达式"
                          "（如 {放技能}）；\n"
                          "x/y 表达式成对填则优先，可写变量或算术（如 100+5）。\n"
                          "算不出坐标 = 这一拍不落手只告警（宁可不动手，不可瞎点）。"))

    def _props_swipe(self, form, node, host=None):
        host = host or node
        form.addRow("x1", self._spin(node.get("x1", 0), 0, BASE_W - 1, node, "x1"))
        form.addRow("y1", self._spin(node.get("y1", 0), 0, BASE_H - 1, node, "y1"))
        form.addRow("x2", self._spin(node.get("x2", 0), 0, BASE_W - 1, node, "x2"))
        form.addRow("y2", self._spin(node.get("y2", 0), 0, BASE_H - 1, node, "y2"))
        spn_ms = self._spin(node.get("ms", 500), 100, 5000, node, "ms")
        form.addRow("ms", spn_ms)
        form.addRow(self._pick_btn(node, "swipe", owner=host))

    def _props_sleep(self, form, node, host=None):   # host 只为循环体行统一签名
        spn_sec = QDoubleSpinBox()
        spn_sec.setRange(0.1, 600)
        spn_sec.setDecimals(1)
        spn_sec.setValue(float(node.get("seconds", 1)))
        spn_max = QDoubleSpinBox()
        spn_max.setRange(0.1, 600)
        spn_max.setDecimals(1)

        def _sec_changed(v, n=node, m=spn_max):
            # 下限抬高时 Qt 自动把上限钳到位并触发回写，随机区间恒成立
            n.__setitem__("seconds", v)
            m.setMinimum(v)
            self._mark_dirty()

        spn_sec.valueChanged.connect(_sec_changed)
        spn_max.setMinimum(spn_sec.value())
        saved_max = float(node.get("seconds_max", 0) or 0)
        spn_max.setValue(saved_max if saved_max > 0
                         else min(max(spn_sec.value() * 2, 1.0), 600))
        spn_max.valueChanged.connect(
            lambda v, n=node: (n.__setitem__("seconds_max", v), self._mark_dirty()))

        # 随机勾选：勾上才显示上限行（"秒"变"下限"），取消 = 固定秒数。
        # 判定字段就是 seconds_max>0，不新增键；老节点没这字段 = 不勾，零影响。
        chk_rand = QCheckBox("随机等待")
        row_max = QWidget()
        rl = QHBoxLayout(row_max)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("上限(s)"))
        rl.addWidget(spn_max)
        lbl_sec = None   # addRow 后才拿得到行标签，_sync 里懒取

        def _sync_rand(on, n=node, r=row_max):
            _vis(r, on)
            lbl = form.labelForField(spn_sec)
            if lbl is not None:
                lbl.setText("下限(s)" if on else "秒")
            if not on:
                if n.get("seconds_max"):
                    n["seconds_max"] = 0   # 取消勾选 = 立即恢复固定
                    self._mark_dirty()
            elif not n.get("seconds_max"):
                n["seconds_max"] = spn_max.value()   # 刚勾上：把框里的默认值落盘
                self._mark_dirty()

        chk_rand.toggled.connect(_sync_rand)
        chk_rand.setChecked(saved_max > 0)
        form.addRow("秒", spn_sec)
        form.addRow(chk_rand)
        form.addRow(row_max)
        _sync_rand(chk_rand.isChecked())

    def _props_server_pick(self, form, node):
        # 识字取物（type id 沿用 server_pick，存量流程零破坏）：认出目标
        # 文字后按用法分流。纯 OCR 认字（点阵字库整条能力已删）。老流程里存的
        # method="hybrid"/"fontlib"、lib="xxx" 字段留着不动：编译侧忽略即可，
        # 用户再存一次流程自然清干净。旧「选区选服 pick_pair」两段用法已删：
        # ensure_graph 载入即换算成 [点文字→等待→点文字]，面板不再认识它。
        mode = str(node.get("mode", "click") or "click")
        is_roi_num = mode == "roi_number"
        cmb_mode = QComboBox()
        for data, text in (("click", "点文字"),
                           ("number", "识文读数字"),
                           ("roi_number", "读数字"),
                           ("position", "读坐标")):
            cmb_mode.addItem(text, data)
        im = cmb_mode.findData(mode)
        cmb_mode.setCurrentIndex(im if im >= 0 else 0)
        # 认不出的 mode（如漏换算的残留 pick_pair）：显示回落第 0 项，下面
        # 各分支也按回落值画 —— 面板显示的与实际生效的不许两套口径
        mode = cmb_mode.currentData()
        cmb_mode.setToolTip("点文字 = 旧用法（认出就点）；读数三种把识别结果写进\n"
                            "流程变量，供后面的节点判断/取用（如先读金币数、够本才刷）。\n"
                            "读数字 = 资源条那类「图标+数字、没有文字标签」的读数：\n"
                            "框住数字那一小块直接认，认 1.17万→11700。\n"
                            "识文读数字 = 数字旁有文字标签（如 金币:12345）时，\n"
                            "先认出带标签那行再抠数字。\n"
                            "（旧「选区选服」已删：摆两个「点文字」节点各框一块，"
                            "中间垫个等待，想两步各配处置还更灵活。）")
        cmb_mode.currentIndexChanged.connect(
            lambda _=False, n=node, c=cmb_mode: (
                n.__setitem__("mode", c.currentData()),
                self._mark_dirty(), self._rebuild_props(n)))
        form.addRow("用法", cmb_mode)

        if not is_roi_num:   # roi_number 压根没有文字锚点，不画这行（防误解）
            edt_txt = QLineEdit(str(node.get("text", "")))
            edt_txt.setPlaceholderText(
                "目标区服名，如：新手1区；留空 = 运行时读账号表" if mode == "click"
                else "目标行文字（当锚点用），如：金币；留空 = 运行时读账号表")
            # 占位符提示全用法通用：目标文字都走 _fmt_text 展开
            _ph_tip = "支持占位符：{变量名} 取流程变量、{账号.列名} 取账号表\n" \
                      "任意列（如 {账号.区服} {账号.等级}，列名照表格表头写）。"
            edt_txt.setToolTip(_ph_tip)
            edt_txt.textChanged.connect(
                lambda v, n=node: (n.__setitem__("text", v), self._mark_dirty()))
            form.addRow("目标文字", _acct_token_row(edt_txt))
        if mode == "click":
            form.addRow("偏移X", self._spin(node.get("offset_x", 0), -(BASE_W - 1), BASE_W - 1,
                                            node, "offset_x"))
            form.addRow("偏移Y", self._spin(node.get("offset_y", 0), -(BASE_H - 1), BASE_H - 1,
                                            node, "offset_y"))
            # 点选算偏移与点模板同款：预览上 OCR 预匹配目标字画绿框，
            # 再点真正要按的位置（如商品名下方价签按钮），偏移自动回填
            form.addRow(self._pick_btn(node, "offset", "点选目标位算偏移"))
            self._jit_rows(form, node)
        else:
            edt_var = QLineEdit(str(node.get("var_name", "")))
            edt_var.setPlaceholderText(
                "变量名，如：金币；留空 = 存进「读数」" if is_roi_num
                else "变量名，如：gold；留空 = 用目标文字当名字")
            edt_var.textChanged.connect(
                lambda v, n=node: (n.__setitem__("var_name", v), self._mark_dirty()))
            form.addRow("变量名", edt_var)
        # ROI：勾选才存 [x0,y0,x1,y1]，不勾 = null 全屏（编译侧 None 兜底）。
        # 手填 + 「在预览上框选」两条路（540×960 基准）：框选走
        # view.roi_owner="flow" 路由回 begin_roi_pick/route_roi，成框即回填。
        # roi_number 例外：区域必须框（不框 = 全屏所有数字挨个读走，读哪个
        # 全靠运气），所以不画勾选框，直接画「已框」摘要行 + 框选按钮。
        # 排版（方案 A 收行）：四个坐标默认收成一行摘要「已框 72,247→193,715」，
        # 点摘要行尾的「改」才展开 4 个 spinbox —— 99% 的时间坐标是框选
        # 出来的不是手填的，四行常驻纯属挤爆面板。
        if is_roi_num:
            form.addRow(_hint("必须框（不框 = 全屏数字挨个读，读哪个全靠运气）："
                              "点两下只圈住要读的那个数字，如资源条金币「1.17万」"
                              "那几个字。区域里有几个数字时判「没认出」，不猜。"))
            form.addRow(self._roi_block(
                form, node, "roi", "识别区域",
                "在左侧预览上点两下框住要读的数字那一小块，框完自动回填。",
                optional=False))
        else:
            form.addRow(self._roi_block(
                form, node, "roi", "识别区域",
                "不用手填坐标：在左侧预览上点两下框住识别范围"
                "（如服务器列表那一片），框完自动回填。\n"
                "⚠ 框要比目标字大一圈：贴字画太窄的条 OCR 检不出行，"
                "框大块列表区域最稳。", optional=True))

        spn_time = self._dspin(node.get("timeout", 15), 1, 300, node,
                               "timeout", cast=int)
        form.addRow("识别超时(s)", spn_time)
        # 识别稳定：认出后要连续认出同一结果满 N 秒才处置（OCR 半秒一张、过场
        # 动画里最易误认目标词）。0/不填 = 认出即处置（老行为零变化，不写键）。
        spn_stable = self._dspin(node.get("stable_secs", 0), 0, 30, node,
                                 "stable_secs", decimals=1, step=0.5,
                                 suffix=" 秒", special="认出即处置")
        spn_stable.setToolTip("认出目标文字后，要【连续认出同一结果】满这么多秒\n"
                              "才动作（点击/写变量）：动画里目标词一闪而过不误判。\n"
                              "中途认出别处或认不出 = 计时清零重来。\n"
                              "不要求（0）= 认出即处置。")
        spn_stable.valueChanged.connect(
            lambda v, n=node: n.__setitem__("stable_secs", v)
            if v > 0 else n.pop("stable_secs", None))
        form.addRow("识别稳定(s)", spn_stable)
        # 认不出的处置（server_pick 专用三档，绑 on_timeout 键，老值兼容）：
        # exit/continue 与 _on_timeout_combo 同款语义；scroll = 滑动找。
        cmb_miss = QComboBox()
        # 显示文本不带"超时："前缀（行标签已说明，前缀白占 ~40px 撑宽下拉）
        for data, text in (("exit", "退出程序并打错误日志"),
                           ("continue", "跳过这步继续"),
                           ("scroll", "滑动翻页继续找")):
            cmb_miss.addItem(text, data)
        im = cmb_miss.findData(str(node.get("on_timeout", "exit")))
        cmb_miss.setCurrentIndex(im if im >= 0 else 0)
        form.addRow("认不出时", cmb_miss)

        scr_row, scr_hint, scr_roi = self._scroll_editor(node, form)
        form.addRow(scr_row)
        form.addRow(scr_roi)
        form.addRow(scr_hint)

        def _miss_sync(on, w=(scr_row, scr_roi, scr_hint)):
            for x in w:
                _vis(x, on)

        def _miss_changed(_, n=node, c=cmb_miss):
            n["on_timeout"] = c.currentData()
            self._mark_dirty()
            _miss_sync(c.currentData() == "scroll")

        cmb_miss.currentIndexChanged.connect(_miss_changed)
        _miss_changed(None)
        spn_sim = self._spin(int(node.get("min_sim", 60)), 0, 100, node,
                             "min_sim", suffix="%")
        row = QHBoxLayout()
        row.addWidget(QLabel("相似度门槛"))
        row.addWidget(spn_sim)
        form.addRow(row)
        spn_int = self._dspin(node.get("min_interval", 0.3), 0, 10, node,
                              "min_interval", decimals=1, step=0.1)
        form.addRow("重试间隔(s)", spn_int)
        form.addRow(_hint("OCR 依赖未装时本节点会打 WARN 跳过（不炸流程）。\n"
                          "装法：pip install rapidocr onnxruntime"))

    def _props_debug_print(self, form, node):
        edt_text = QLineEdit(node.get("text", ""))
        edt_text.setPlaceholderText("打印的文本，如：体力={体力} 服={账号.区服}")
        edt_text.setToolTip("文本里写 {变量名} 换成流程变量现值；"
                            "{账号.列名} 换成账号表那列（如 {账号.区服}）")
        edt_text.textChanged.connect(
            lambda txt, n=node: (n.__setitem__("text", txt), self._mark_dirty()))
        form.addRow("文本", edt_text)
        cmb_level = QComboBox()
        cmb_level.addItems(["INFO", "WARN", "ERROR"])
        if node.get("level") in ("INFO", "WARN", "ERROR"):
            cmb_level.setCurrentText(node["level"])
        cmb_level.currentTextChanged.connect(
            lambda lv, n=node: (n.__setitem__("level", lv), self._mark_dirty()))
        form.addRow("日志级别", cmb_level)
        form.addRow(self._build_print_target_editor(node))

    def _props_save_assets(self, form, node):
        form.addRow(self._build_assets_editor(node))

    def _props_compute(self, form, node):
        # 计算赋值（F1）：表达式 + 写入的流程变量名。占位符与 _fmt_text 同口径，
        # 变量名直写 / {变量名} / {账号.列名} 都认；结果落流程变量供判断等取用。
        edt_expr = QLineEdit(str(node.get("expr", "")))
        edt_expr.setPlaceholderText("如：体力-50、金币/2、max(0, 体力-50)")
        edt_expr.setToolTip(
            "算术表达式。变量名直接写（体力 - 50），或写 {变量名}、{账号.列名}。\n"
            "支持 + - * / // % ** 和括号；函数 abs/min/max/round/int/float/len。\n"
            "只做数字运算（不做字符串拼接）；算不出只 WARN 跳过，不拦路。")
        edt_expr.textChanged.connect(
            lambda v, n=node: (n.__setitem__("expr", v), self._mark_dirty()))
        form.addRow("表达式", _acct_token_row(edt_expr))
        edt_var = QLineEdit(str(node.get("var_name", "")))
        edt_var.setPlaceholderText("要写入的流程变量名，如：体力")
        edt_var.setToolTip(
            "结果写进这个流程变量，后续节点可用 {名字} / 变量比较取用。\n"
            "留空 = 跳过本步（只打一行 WARN，不写空名变量）。")
        edt_var.textChanged.connect(
            lambda v, n=node: (n.__setitem__("var_name", v), self._mark_dirty()))
        form.addRow("变量名", edt_var)
        form.addRow(_hint("结果尽量取整数（10/2=5）。典型：识字取物读体力 → "
                          "计算 体力=体力-50 → 判断 体力>=10。"))

    def _props_branch(self, form, node):
        spn_time = self._dspin(node.get("timeout", 5), 1, 300, node,
                               "timeout", cast=int)
        form.addRow("判断超时(s)", spn_time)
        # 认不出的处置（与识字取物同款滑动翻页）：默认超时走 N 口 = 不落
        # 任何新键，存量图行为逐字节不变；scroll = 窗口到期滑一页、停稳
        # 重开窗口继续投票，滑次用尽才走 N。
        cmb_miss = QComboBox()
        for data, text in (("fail", "超时走 N 口"),
                           ("scroll", "滑动翻页继续找")):
            cmb_miss.addItem(text, data)
        im = cmb_miss.findData(str(node.get("on_timeout", "fail")))
        cmb_miss.setCurrentIndex(im if im >= 0 else 0)
        form.addRow("认不出时", cmb_miss)
        scr_row, scr_hint, scr_roi = self._scroll_editor(node, form)
        form.addRow(scr_row)
        form.addRow(scr_roi)
        form.addRow(scr_hint)

        def _miss_changed(_, n=node, c=cmb_miss,
                          w=(scr_row, scr_roi, scr_hint)):
            if c.currentData() == "scroll":
                n["on_timeout"] = "scroll"
            else:
                n.pop("on_timeout", None)   # 默认形态不留键：存量图零污染
            self._mark_dirty()
            for x in w:
                _vis(x, c.currentData() == "scroll")

        cmb_miss.currentIndexChanged.connect(_miss_changed)
        _miss_changed(None)
        # 条件区（2026-10-06 监视行同款）：条件1..N 全进行列表，第 1 条不再单摊
        form.addRow(self._build_branch_cond_area(node))
        form.addRow(_hint("在画布上：点「Y」端口拖到目标节点入点 = 条件成立分支；\n"
                          "点「N」端口拖 = 不成立分支。连线即分支。"))

    def _props_subflow(self, form, node):
        # 子流程 = 加强版点模板：选一条已存流程嵌进来，跑完分 Y/N 两出口。
        self._flow_combo_row(form, node)

        # 调用绑定形参表（F2）：按参数复用同一条子流程，不再靠手写全局变量名硬碰
        form.addRow(QLabel("子流程参数（进入前逐个求值写入流程变量）："))
        form.addRow(self._params_box(node))
        form.addRow(_hint("左=形参名（子流程内部 {名字} 直接引用）、右=实参表达式。\n"
                          "实参可为字面量（10）、变量（体力）、算术（体力-10）、"
                          "{账号.列名}；\n"
                          "进入子流程前逐个求值写入流程变量。算不出的那项跳过、"
                          "不拦路。\n"
                          "同名变量父子共用一份（不做作用域隔离）。空表 = 不传参。"))

        # 主流程监视照管开关（用户拍板 2026-10-05）：关 = 跑本条子流程期间
        # 主流程的并行监视哨兵停手，一切按子流程自己的逻辑走
        chk_bg = self._chk(node.get("bg_watch", True), node, "bg_watch",
                           "子流程期间主流程监视照常")
        chk_bg.setToolTip(
            "开（默认）：跑这条子流程时，主流程的「并行监视」照常扫描，\n"
            "弹窗来了照样暂停子流程先办弹窗。\n"
            "关：子流程期间主流程哨兵停手（已开跑的弹窗办完为止），\n"
            "一切按子流程自己的逻辑走 —— 子流程里也想盯弹窗，\n"
            "就在子流程内部自己放「并行监视」节点；定时触发节点不受影响。")
        form.addRow(chk_bg)

        chk_wait = QCheckBox("进入前等模板出现")
        chk_wait.setChecked(bool(node.get("wait_before", True)))
        spn_wait = self._dspin(node.get("wait_timeout", 8), 0, 300, node,
                               "wait_timeout", special="不限时", cast=int)

        def _sync(on, widgets):
            for w in widgets:
                w.setEnabled(on)

        # 门超时行为：subflow 语义是"走哪个出口"，与 _on_timeout_combo 的
        # "判失败走恢复链"不同，单做一份（选项文案直说出口）
        cmb_ot = QComboBox()
        for data, text in (("fail", "走「N」出口（不跑子流程）"),
                           ("continue", "不等了，照常进入子流程")):
            cmb_ot.addItem(text, data)
        oi = cmb_ot.findData(node.get("on_timeout", "fail"))
        cmb_ot.setCurrentIndex(oi if oi >= 0 else 0)
        cmb_ot.currentIndexChanged.connect(
            lambda _=False, n=node, c=cmb_ot: (n.__setitem__("on_timeout", c.currentData()),
                                         self._mark_dirty()))

        form.addRow(chk_wait)
        form.addRow("等图超时(s)", spn_wait)
        cmb_cat, cmb_name = self._add_cat_name_rows(form, node)
        form.addRow("等不到时", cmb_ot)
        ws = (spn_wait, cmb_cat, cmb_name, cmb_ot)
        # 开关关掉 → 等图相关控件全置灰（控件建好后才连，天然晚绑定）
        chk_wait.toggled.connect(
            lambda on, n=node: (n.__setitem__("wait_before", on),
                                self._mark_dirty(), _sync(on, ws)))
        _sync(bool(node.get("wait_before", True)), ws)
        form.addRow(_hint("在画布上：点「Y」口 = 子流程跑完继续走；\n"
                          "点「N」口 = 子流程失败或等图超时走这条。\n"
                          "「N」口不连线 = 失败直接判整图失败。"))

    def _params_box(self, node, repaint=None):
        """子流程「调用绑定」形参小表（F2）：两列表（形参名 / 实参表达式）+ 行尾 ×，
        底部「＋ 添加参数」。非模态内嵌，不吃余下属性表单的布局（QTableWidget 自带
        滚动，行多也不顶穿 318 视口）。
        存储：node["params"] = [{"name":…, "expr":…}]；行 dict 身份必须稳定
        （就地原地写、只 append/pop，绝不整表 new 重建 —— 与 stop_on 行同纪律）。
        ⚠ 惰性挂键：老节点只是「打开看一眼面板」绝不写 params 键（只有真加/编一行
        才建），存量流程字节级零污染（与 stop_on 的「删光 pop 键」同款纪律）。
        repaint = 画布刷尾标的目标节点（画布 subflow 面板传自己；循环体行传宿主
        节点）；不传 = 用 node 自己。"""
        repaint = repaint if repaint is not None else node
        rows = node.get("params")
        if not isinstance(rows, list):
            rows = []           # 内存空表够回填；键等首编辑才落（惰性建）

        def _ensure():
            # 首次真正改行时把键挂上：原来没键 → 换成这份内存列表；原来是脏数据
            # （非 list）→ 就地纠成 list。之后所有行操作原地改同一个 rows 本体。
            if node.get("params") is not rows:
                node["params"] = rows
                self._mark_dirty()
            return rows

        tbl = QTableWidget(0, 3)
        tbl.setObjectName("subflowParamsTable")
        tbl.setHorizontalHeaderLabels(["形参名", "实参表达式", ""])
        hh = tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        tbl.verticalHeader().setVisible(False)
        tbl.setSelectionMode(QAbstractItemView.NoSelection)
        tbl.setEditTriggers(QAbstractItemView.DoubleClicked
                            | QAbstractItemView.SelectedClicked
                            | QAbstractItemView.EditKeyPressed)
        tbl.setMaximumHeight(140)   # 约 5 行封顶：再多自带滚动条
        tbl.setMinimumWidth(0)

        def _fill():
            tbl.blockSignals(True)
            tbl.setRowCount(0)
            for r in rows:
                k = tbl.rowCount()
                tbl.insertRow(k)
                it_n = QTableWidgetItem(str(r.get("name", "") or ""))
                it_n.setToolTip("形参名 = 子流程内部 {名字} 引用的变量名（如：体力下限）")
                it_e = QTableWidgetItem(str(r.get("expr", "") or ""))
                it_e.setToolTip("实参表达式：字面量（10）/ 变量（体力）/ 算术（体力-10）"
                                " / {账号.列名}")
                tbl.setItem(k, 0, it_n)
                tbl.setItem(k, 1, it_e)
                btn = QToolButton()
                btn.setText("×")
                btn.setToolTip("删除这一行参数")
                btn.clicked.connect(lambda _=False, rr=r: _del(rr))
                tbl.setCellWidget(k, 2, btn)
            tbl.blockSignals(False)

        def _set(r, key, val):
            # 就地原地写行 dict（身份稳定），不整表重建
            if r.get(key) != val:
                r[key] = val
                self._mark_dirty()

        def _changed(k, col, rn=None):
            if 0 <= k < len(rows):
                _ensure()
                it = tbl.item(k, col)
                if it is not None:
                    _set(rows[k], "name" if col == 0 else "expr",
                         it.text().strip() if col == 0 else it.text())
                    self._repaint_node(repaint)   # 画布尾标跟参数数刷新

        tbl.itemChanged.connect(
            lambda it, rr=None: _changed(it.row(), it.column()))

        def _del(r):
            if r in rows:
                _ensure()
                rows.remove(r)
                _fill()

        def _add():
            _ensure()
            rows.append({"name": "", "expr": ""})
            _fill()

        _fill()
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(tbl)
        b = _add_btn("添加参数")
        b.clicked.connect(_add)
        lay.addWidget(b)
        return box

    def _props_multi_scan(self, form, node):
        # 遍历全部：收满目标逐处处置。搜索侧（模板/文字/区域/偏移/抖动）
        # 与点模板同口径；处置侧三副面孔：点掉/进一个/读数。
        cmb_src = QComboBox()
        for data, text in (("template", "模板图"),
                           ("text", "文字（OCR 圈字）")):
            cmb_src.addItem(text, data)
        si = cmb_src.findData(node.get("source", "template"))
        cmb_src.setCurrentIndex(si if si >= 0 else 0)
        form.addRow("检测源", cmb_src)
        cmb_cat, _cmb_name = self._add_cat_name_rows(form, node)
        btn_goto = self._goto_capture_btn(cmb_cat, node)
        form.addRow(btn_goto)
        edt_txt = QLineEdit(node.get("text", ""))
        edt_txt.setPlaceholderText("要圈的文字，如：私聊（每个达标识别行收为一处）")
        edt_txt.textChanged.connect(
            lambda txt, n=node: (n.__setitem__("text", txt),
                                 self._mark_dirty()))
        form.addRow("目标文字", edt_txt)
        spn_ms = self._spin(int(node.get("min_sim", 60)), 0, 100, node,
                             "min_sim", suffix="%")
        spn_ms.setToolTip("识别行与目标文字的相似度门槛（低于它不算命中）")
        form.addRow("文字相似门槛", spn_ms)
        form.addRow(self._roi_block(
            form, node, "roi", "搜索区域",
            "在左侧预览上点两下框住列表那一片，框完自动回填。\n"
            "只在框内收命中，防把屏幕角落的同款图标也算进目标。",
            optional=True,
            chk_text="限定搜索区域（不框 = 跟随模板默认）",
            none_text="未框（= 跟随模板默认区域）"))
        spn_ox = self._spin(node.get("offset_x", 0), -(BASE_W - 1), BASE_W - 1, node, "offset_x")
        spn_oy = self._spin(node.get("offset_y", 0), -(BASE_H - 1), BASE_H - 1, node, "offset_y")
        form.addRow("偏移X", spn_ox)
        form.addRow("偏移Y", spn_oy)
        btn_pick = self._pick_btn(node, "offset", "点选目标位算偏移")
        form.addRow(btn_pick)
        jit_chk, jit_row = self._jit_rows(form, node)

        cmb_mode = QComboBox()
        for data, text in (("tap", "逐个点掉"),
                           ("enter", "进一个（点→子流程）"),
                           ("read", "逐个读数（存变量不点）")):
            cmb_mode.addItem(text, data)
        mi = cmb_mode.findData(node.get("mode", "tap"))
        cmb_mode.setCurrentIndex(mi if mi >= 0 else 0)
        form.addRow("处置模式", cmb_mode)
        form.addRow(_hint("⚠ 不勾去重时坐标只在本帧收一次，中途不重扫不"
                          "去重：适合「办完回列表、列表不变」的页面；会"
                          "自己刷新/补位的列表勾下面的「去重边扫边办」。"))

        # 「进一个」专属块：子流程 + 列表标志图 + 停顿/确认秒数。
        # 先攒在无父 QWidget 里，显隐走 _vis（构建期只藏不显）
        box = QWidget()
        boxf = QFormLayout(box)
        boxf.setContentsMargins(0, 0, 0, 0)
        self._flow_combo_row(boxf, node)
        # 缺键兜 True = 存量「进一个」老行为照按返回；新节点显式落 False
        chk_pb = self._chk(node.get("press_back", True), node, "press_back",
                           "子流程后按返回键")
        chk_pb.setToolTip("不勾 = 回列表页由子流程自己负责（体内有关闭/返回"
                          "按钮时用）。关着又没配「列表标志图」= 体没回家"
                          "也会照着旧坐标瞎点下一处，强烈建议搭配标志图。")
        boxf.addRow(chk_pb)
        boxf.addRow(_hint("「列表标志图」（可选）：回列表页的确认凭证。"
                          "填了 = 办完一处后等它出现，确认真回家了才点"
                          "下一处，等超时判失败走 N 口；不填 = 办完后按"
                          "停留时间直接继续（画面没停稳会点空）。"))
        ct = node.setdefault("confirm_tpl", {"category": "", "name": ""})
        self._add_cat_name_rows(boxf, ct, repaint_for=node)
        spn_cft = self._dspin(node.get("confirm_timeout", 5), 0, 60, node,
                              "confirm_timeout", decimals=1, step=0.5,
                              suffix=" s")
        spn_cft.setToolTip("办完一处回程后最多等列表标志图出现的秒数"
                           "（等不到 = 迷路判失败）")
        boxf.addRow("等回程确认(s)", spn_cft)
        spn_stl = self._dspin(node.get("settle_secs", 0.8), 0, 10, node,
                              "settle_secs", decimals=1, step=0.1,
                              suffix=" s")
        spn_stl.setToolTip("办完一处后先让画面停这么久再确认/落下一手"
                           "（给退场动画留余量）")
        boxf.addRow("回程停留(s)", spn_stl)
        form.addRow(box)

        # 去重边扫边办（P3）+ 邻域读数框（P2）：读数模式一下不点，
        # "办过的身份"无从谈起 —— 编译层强制关、界面同步置灰（双保险）
        chk_dd = QCheckBox("去重边扫边办（列表会刷新/补位）")
        chk_dd.setToolTip("每办完一处重新采集：邻域框里读到名字的按名字认脸，"
                          "读不到按坐标格子；办过的跳过，没有新面孔才收口。")
        chk_dd.setChecked(bool(node.get("dedupe", False)))
        spn_rounds = self._spin(int(node.get("max_rounds", 50) or 50),
                                1, 500, node, "max_rounds")
        spn_rounds.setToolTip("扫这么多轮还冒出新面孔 = 身份读不稳或列表停"
                              "不下来，判失败走 N 口")
        form.addRow(chk_dd)
        form.addRow("最多轮数", spn_rounds)

        # 数字过滤（2026-10-06）：每处先就地读邻域框数字，达标才处置，
        # 读不出当不达标 —— 「遍历所有卡片、只点爱心<20 的」一个节点配完
        chk_flt = QCheckBox("数字过滤（邻域框读数达标才处理）")
        chk_flt.setToolTip("每处先就地读下面「邻域读数框」里的数字，满足条件才点/才进；"
                           "读不出（没有数字或不止一个）当不达标跳过 —— 绝不拿未知数落手。"
                           "典型用法：只点爱心小于 20 的卡片。读数模式一下不点，此项置灰。")
        op_flt = QComboBox()
        for _fd, _ft in (("lt", "小于"), ("le", "小于等于"), ("eq", "等于"),
                         ("ge", "大于等于"), ("gt", "大于"), ("ne", "不等于")):
            op_flt.addItem(_ft, _fd)
        _cur_flt = str(node.get("filter_op", "") or "")
        chk_flt.setChecked(bool(_cur_flt))
        _fi = op_flt.findData(_cur_flt or "lt")
        op_flt.setCurrentIndex(_fi if _fi >= 0 else 0)
        spn_flt = QDoubleSpinBox()
        spn_flt.setRange(0.0, 99999999.0)
        spn_flt.setDecimals(2)
        spn_flt.setValue(float(node.get("filter_val", 0) or 0))
        spn_flt.setToolTip("过滤阈值：邻域框读出的数字与它比较")
        frow = QHBoxLayout()
        frow.addWidget(op_flt)
        frow.addWidget(spn_flt)
        frow.addStretch()
        fbox = QWidget()
        fbox.setLayout(frow)
        form.addRow(chk_flt)
        form.addRow(fbox)

        def _flt_sync(n=node, c=chk_flt, o=op_flt, v=spn_flt):
            n["filter_op"] = o.currentData() if c.isChecked() else ""
            n["filter_val"] = v.value()
            self._mark_dirty()

        rbox = QWidget()
        rf = QFormLayout(rbox)
        rf.setContentsMargins(0, 0, 0, 0)
        rf.addRow("读数框偏移X", self._spin(node.get("read_dx", 0), -(BASE_W - 1), BASE_W - 1,
                                            node, "read_dx"))
        rf.addRow("读数框偏移Y", self._spin(node.get("read_dy", 0), -(BASE_H - 1), BASE_H - 1,
                                            node, "read_dy"))
        rf.addRow("读数框宽", self._spin(node.get("read_w", 160), 2, BASE_W,
                                         node, "read_w"))
        rf.addRow("读数框高", self._spin(node.get("read_h", 48), 2, BASE_H,
                                         node, "read_h"))
        rf.addRow(_hint("框中心 = 命中点 + 偏移。读数模式把框里的数字依次存成"
                        " 变量_1、变量_2…（1.17万折成 11700；框里数字没有或"
                        "不止一个 = 没认出，不写脏值）。勾了去重的，认脸用的"
                        "名字也从这块里读；开了数字过滤，达标判定同样读这块。"))
        form.addRow(rbox)

        def _apply_vis(n=node, c=cmb_mode):
            m = c.currentData()
            _vis(box, m == "enter")
            flt_on = chk_flt.isChecked() and m != "read"
            _vis(fbox, flt_on)
            _vis(rbox, m == "read" or (chk_dd.isChecked() and m != "read")
                 or flt_on)
            chk_dd.setEnabled(m != "read")
            spn_rounds.setEnabled(chk_dd.isChecked() and m != "read")
            chk_flt.setEnabled(m != "read")

        def _mode_changed(_, n=node, c=cmb_mode):
            n["mode"] = c.currentData()
            self._mark_dirty()
            self._repaint_node(n)          # 画布尾标（·进一个/·读数…）跟着换
            _apply_vis()

        def _src_changed(_, n=node, c=cmb_src):
            # 文字源不碰模板：图/库/采集/偏移/抖动整排置灰（同"关门残值
            # 不留配了没生效错觉"纪律）；残值不清盘，切回模板源照用
            is_txt = c.currentData() == "text"
            n["source"] = c.currentData()
            self._mark_dirty()
            self._repaint_node(n)          # 画布尾标「·文字」
            for w in (cmb_cat, _cmb_name, btn_goto, spn_ox, spn_oy,
                      btn_pick, jit_chk, jit_row):
                w.setEnabled(not is_txt)
            for w in (edt_txt, spn_ms):
                w.setEnabled(is_txt)

        cmb_src.currentIndexChanged.connect(_src_changed)
        cmb_mode.currentIndexChanged.connect(_mode_changed)
        chk_dd.toggled.connect(lambda on, n=node: (
            n.__setitem__("dedupe", on), self._mark_dirty(), _apply_vis()))
        chk_flt.toggled.connect(lambda on: (_flt_sync(), _apply_vis()))
        # lambda 吞掉信号首参：直连会把 index/value 灌进 _flt_sync 的 n 默认位
        op_flt.currentIndexChanged.connect(lambda *_: _flt_sync())
        spn_flt.valueChanged.connect(lambda *_: _flt_sync())
        _mode_changed(None)
        _src_changed(None)   # 存量文字源节点一打开就得看见灰对了（残值纪律）

        spn_max = self._spin(int(node.get("max_hits", 20) or 20),
                             1, 50, node, "max_hits")
        spn_max.setToolTip("误匹配一屏时防把全屏当目标刷（快照封顶）")
        form.addRow("最多处数", spn_max)
        spn_gap = self._dspin(node.get("gap_secs", 0.5), 0.0, 5.0, node,
                              "gap_secs", decimals=1, step=0.1, suffix=" s")
        spn_gap.setToolTip("两次落手之间的最短间隔（点掉=两指点间隔；"
                           "进一个=办完一处回列表后歇多久再点下一处）")
        form.addRow("落手间隔(s)", spn_gap)
        edt_var = QLineEdit(node.get("var_name", "遍历命中数"))
        edt_var.setPlaceholderText("命中数写进哪个流程变量（留空 = 不写）")
        edt_var.textChanged.connect(
            lambda txt, n=node: (n.__setitem__("var_name", txt),
                                 self._mark_dirty()))
        form.addRow("命中数变量", edt_var)

        chk_wait = QCheckBox("扫描前先等模板出现")
        chk_wait.setChecked(bool(node.get("wait_before", True)))
        spn_wait = self._dspin(node.get("wait_timeout", 8), 0, 300, node,
                               "wait_timeout", special="不限时", cast=int)
        cmb_ot = self._on_timeout_combo(node)

        def _sync_gate(on, a=spn_wait, b=cmb_ot):
            # 关掉等待后超时设置无意义，置灰防"配了却没生效"的错觉
            a.setEnabled(on)
            b.setEnabled(on)

        chk_wait.toggled.connect(
            lambda on, n=node: (n.__setitem__("wait_before", on),
                                self._mark_dirty(), _sync_gate(on)))
        form.addRow(chk_wait)
        form.addRow("等图超时(s)", spn_wait)
        form.addRow("等不到时", cmb_ot)
        _sync_gate(bool(node.get("wait_before", True)))
        # 收到即结束（跑完当前这一处才掐，绝不半途撕项）
        form.addRow(QLabel("收到即结束的信号："))
        form.addRow(self._stop_on_box(node))

    def _props_watch(self, form, node):
        if container_groups(node) is not None:
            form.addRow(self._build_watch_container_editor(node))
        else:
            form.addRow(self._build_watch_editor(node))

    def _props_timer(self, form, node):
        form.addRow(self._build_timer_editor(node))

    def _props_signal(self, form, node):
        # 发信号：只填信号名。name 是信号名不是模板图名（编译侧只喂
        # SignalStep，node_tpl_refs 不看这个类型，绝不进模板账本/引用扫描）。
        edt_sig = QLineEdit(str(node.get("name", "")))
        edt_sig.setPlaceholderText("信号名，如：收工 / 背包满 / 体力耗尽")
        edt_sig.setToolTip(
            "投进共享信箱的信号名。外层「循环 / 遍历全部」在属性里登记\n"
            "同名「收到即结束的信号」才会被掐；一次销账（先登记的吃掉）。")
        edt_sig.textChanged.connect(
            lambda txt, n=node: (n.__setitem__("name", txt), self._mark_dirty(),
                                 self._repaint_node(n)))
        form.addRow("信号名", edt_sig)
        form.addRow(_hint("本节点只管发、不管谁收，发完直通往下走（永不失败）。\n"
                          "想让谁停下来，就去那个「循环 / 遍历全部」节点属性里\n"
                          "登记同名信号。子流程里发的，外层照样收得到。"))

    def _props_stop_flow(self, form, node):
        # 结束流程：出口都没有（出去了就没了），只有一个范围下拉。
        # 显示中文、存英文（local/global/continue），老节点缺键按 local。
        cmb_scope = self._cbo(node.get("stop_scope", "local"),
                              [("结束当前流程（所在层）", "local"),
                               ("结束整条流程（全局急停）", "global"),
                               ("跳过本轮（循环内 continue）", "continue")],
                              node, "stop_scope")
        cmb_scope.setToolTip(
            "当前流程 = 谁的边界谁收口：放子流程里 = 子流程算跑完（走Y口，\n"
            "主线继续）；放循环里 = 跳出循环；放主流程顶层 = 结束整条任务。\n"
            "整条流程 = 不管埋在哪一层，命中即整条任务立即停止。\n"
            "跳过本轮 = 只对循环体有意义：命中即丢掉本轮剩余步骤、立即开始\n"
            "下一轮（等价循环的 continue）。放在循环外没人接，等于结束整条任务。")
        form.addRow("结束范围", cmb_scope)
        form.addRow(_hint("命中本节点后按上面选择的范围结束（算作成功完成）。\n"
                          "不上失败恢复链；被结束范围内的后续节点不会再执行。\n"
                          "「跳过本轮」放循环体里 = 本轮剩下的行不做了，直接下一轮。\n"
                          "适合「达成条件即收尾」的场景，如登录成功、任务完成。"))

    def _props_loop(self, form, node):
        form.addRow(self._build_loop_editor(node))


    def _build_loop_editor(self, node):
        """循环容器属性编辑：
        - 出口条件 = _build_branch_cond_area 条件行列表（2026-10-06 监视行同款：
          cond+extra_conds+logic+not 存储不变，行摘要+选中行编辑；
          循环出口不支持 motion —— 动态检测语义是"等动画动起来"，而逐轮查询
          只关心状态在不在，两者天然冲突 → allow_motion=False）
        - 轮数上限（max_times，0=无限直到条件）
        - 循环体（2026-10-06 同款改版）：动作进摘要行列表，点一行在下面编辑
          （类型下拉 + 该型参数，行结构与图节点同形），不再每条动作把整套
          表单摊开叠排。行增删会改节点高度 → 整场重建画布；换序只重画节点
          （画布动作条按行序画）。"""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        steps = node.setdefault("steps", [])

        # 顶部：退出条件 + 轮数上限（中断接回方式已随信号线下线，一刀切暂停
        # 统一是"办完从暂停处续跑"语义，没有可选项）
        # ⚠ 条件编辑器必须跨整行：它本体就有 300px（搜索区域一行/四spin一行），
        # 再挂进「退出条件」标签列 = 360px > 318 视口 → 整个属性面板出横向
        # 滑块，所有行左边被切（用户实拍）。判断节点同款编辑器就是跨行摆的。
        f_top = QFormLayout()
        f_top.addRow(QLabel("退出条件："))
        f_top.addRow(self._build_branch_cond_area(node, allow_motion=False,
                                                  exit_loop=True))
        spn_times = self._spin(node.get("max_times", 0), 0, 100000,
                               node, "max_times")
        spn_times.setSpecialValueText("不限（直到条件满足）")
        f_top.addRow("到达上限(轮)", spn_times)
        lay.addLayout(f_top)
        # 抬头走 _hint（wordWrap）：裸 QLabel 长句 minimumSizeHint=整行字宽，
        # 408px 顶穿 318 视口（smoke_prop_width 实测口径）
        lay.addWidget(_hint("收到即结束的信号（「发信号」节点投出同名信号 → 不等圈底收工）："))
        lay.addWidget(self._stop_on_box(node))
        lay.addWidget(QLabel("循环体（顺序执行成一轮）："))

        def _resync():
            self._rebuild_scene()
            self._reselect_node(node)
            self._rebuild_props(node)

        lay.addWidget(RowListEditor(
            get_rows=lambda: [x for x in steps if isinstance(x, dict)],
            summary=step_row_summary,
            build=lambda s, touch: self._build_loop_step_editor(node, s),
            commit=lambda rows: steps.__setitem__(slice(None), rows),
            on_struct=_resync,
            on_reordered=lambda: (self._mark_dirty(), self._repaint_node(node)),
            sel_get=lambda: getattr(self, "_step_sel", None),
            sel_set=lambda r: setattr(self, "_step_sel", r),
            clip_get=lambda: getattr(self, "_step_clip", None),
            clip_set=lambda c: setattr(self, "_step_clip", c),
            add_text="添加动作",
            new_item=lambda: new_node("click"),
            min_rows=0,   # 空循环体合法（老版 × 能删光；条件区 min_rows=1 对照）
            list_tip="从上到下 = 执行顺序；拖一行换序，点一行在下面编辑；\n"
                     "右键=复制/粘贴/删除。"))
        lay.addWidget(_hint("循环体 = 一轮里按顺序做的动作；每轮跑完查一次退出条件，\n"
                            "满足就出循环往下走（比如「主线完成」出现）。\n"
                            "行里放「结束流程」行可提前收口：选「跳过本轮」= 本轮剩下\n"
                            "的行不做了直接下一轮（continue），选「跳出循环」= 结束本循环。\n"
                            "不设退出 + 不限轮数 = 死循环（意图明确时可配轮数上限防卡死）。\n"
                            "有并行监视/定时执行的子链在跑时，本循环跟着冻结，\n"
                            "办完从暂停处自动续跑（不用接线）。"))
        return box

    def _stop_on_box(self, node):
        """「收到即结束的信号」名单（循环/遍历同款，2026-10-06 配「发信号」）：
        登记几个信号名 —— 主线上任何角落（含子流程）的「发信号」节点投出同名
        信号，本容器就地收工（算正常跑完走 out 口续跑主线）。信号一次销账：
        谁先登记谁吃掉，外层同名不受牵连。
        行存 {"name": 信号名} 字典（RowListEditor 要吃 dict 身份 = 存储本尊，
        纯字符串重名会张冠李戴）；删光自动 pop 键 = 不建闸，老流程零迁移。
        引擎读侧（flow._stop_on_names）字典行/纯字符串两种形态都认，
        AI 手搭流程直写 ["封号"] 照跑。"""
        def _rows():
            v = node.get("stop_on")
            if not isinstance(v, list):
                return []
            if all(isinstance(r, dict) for r in v):
                return v    # 字典行 = 存储本尊：行编辑器原位写它（RowListEditor 身份要求）
            fixed = [{"name": (str(r.get("name") or "") if isinstance(r, dict)
                               else str(r or ""))} for r in v]
            node["stop_on"] = fixed   # 字符串行就地换算成字典行（编辑侧只认字典）
            return fixed

        def _commit(rows):
            if rows:
                node["stop_on"] = rows
            else:
                node.pop("stop_on", None)
            self._mark_dirty()

        def _build(row, touch):
            w = QWidget()
            f = QFormLayout(w)
            f.setContentsMargins(0, 0, 0, 0)
            ed = QLineEdit(str(row.get("name") or ""))
            ed.setPlaceholderText("信号名，如：收工")
            ed.textChanged.connect(
                lambda t, r=row: (r.__setitem__("name", t), touch(),
                                  self._mark_dirty()))
            f.addRow("信号名", ed)
            return w

        return RowListEditor(
            get_rows=_rows,
            summary=lambda r: str(r.get("name") or "").strip() or "（没填信号名）",
            build=_build, commit=_commit,
            on_struct=lambda: (self._mark_dirty(), self._rebuild_props(node)),
            on_reordered=lambda: self._mark_dirty(),
            sel_get=lambda: getattr(self, "_stopon_sel", None),
            sel_set=lambda r: setattr(self, "_stopon_sel", r),
            clip_get=lambda: getattr(self, "_stopon_clip", None),
            clip_set=lambda c: setattr(self, "_stopon_clip", c),
            add_text="添加停止信号", new_item=lambda: {"name": ""},
            min_rows=0,
            list_tip="登记「发信号」节点投出后要掐本容器的信号名；\n"
                     "点一行在下面改名，右键=复制/粘贴/删除。")

    def _build_loop_step_editor(self, node, s):
        """循环体单条动作的编辑表单（RowListEditor 选中行才实例化 —— 旧版逐条
        摊开叠排的就是这里的整套表单）。类型下拉 + 按型参数，均写入 s；
        切类型 = 换 new_node 默认参数（备注名保留），同 dict 原位换芯，
        行列表选中身份不破。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        st = s.get("type", "")

        def _resync():
            self._rebuild_scene()
            self._reselect_node(node)
            self._rebuild_props(node)

        # 选项文案来自 _STEP_KIND_CN 单一表（加类型只改那张表+顺序列）
        cmb = QComboBox()
        for k in _STEP_DROP_ORDER:
            cmb.addItem(_STEP_KIND_CN[k], k)
        idx = cmb.findData(st)
        cmb.setCurrentIndex(idx if idx >= 0 else 0)
        lay.addWidget(cmb)

        def _on_type(_idx, target=s, c=cmb):
            # 行类型切换：换成 new_node 的默认参数，备注名保留；
            # 节点高度不变，重建表单 + 重画节点（画布动作条的字/标记跟型走）。
            fresh = new_node(c.currentData())
            if c.currentData() == "stop_flow":
                # 循环体里的「结束流程」行：默认「跳过本轮」（F4，循环 continue），
                # 比画布节点的默认 local（跳出循环）更贴合行语境；可切两外两档。
                fresh["stop_scope"] = "continue"
            saved_label = target.get("label", "")
            target.clear()
            target.update(fresh)
            if saved_label:
                target["label"] = saved_label
            self._mark_dirty()
            self._repaint_node(node)
            self._rebuild_props(node)

        cmb.currentIndexChanged.connect(_on_type)

        # 行参数编辑：点图/等图/点坐标/停顿/滑动 = 直接复用主画布节点的同一份
        # _props_* 编辑区（2026-10-09 收口：画布版为权威，循环体行补齐此前缺的
        # 偏移/框选/去采集/点全部/抖动/随机等待字段；行 dict 与节点同形，
        # 写回落行本体，host=node 只管画布重绘/回程票/取点重建的归属）。
        # 清弹窗/子流程/发信号 = 循环体专属形态，保留本函数自建的编辑区。
        if st in ("click_template", "wait_template", "click", "sleep", "swipe"):
            f = QFormLayout()
            getattr(self, "_props_" + st)(f, s, node)
            lay.addLayout(f)
        elif st == "popup_zone":
            # 清弹窗行：模块清单（行结构 {"category","name"}，命中即点掉），
            # 行内嵌子行（归属+模板 / 采+×）。模块本来就是两行式紧凑行，
            # 不再套行列表（叠的是"整套大表单"的病，不是"多行小控件"的病）。
            mods = s.setdefault("modules", [])
            f = QFormLayout()
            spn_sil = self._dspin(s.get("silent_secs", 5), 0.5, 120, s,
                                  "silent_secs", decimals=1)
            spn_to = self._dspin(s.get("timeout", 180), 0, 600, s, "timeout")
            f.addRow("静默判定(s)", spn_sil)
            f.addRow("总超时(s)", spn_to)
            lay.addLayout(f)
            lay.addWidget(QLabel("模块（扫描顺序=优先级，命中即点掉）："))
            for j, m in enumerate(mods):
                row = QHBoxLayout()
                cmb_cat = QComboBox()
                _fill_category_combo(cmb_cat, m.get("category", ""))
                cmb_name = QComboBox()
                row.addWidget(cmb_cat, 1)
                row.addWidget(cmb_name, 2)

                def _sync_name(c=cmb_cat, mm=m, nm=cmb_name):
                    self._fill_names_fidelity(nm, mm, c.currentData())
                cmb_cat.currentIndexChanged.connect(lambda _=False, fn=_sync_name: fn())
                cmb_name.currentTextChanged.connect(
                    lambda nm, mm=m: (mm.__setitem__("name", nm), self._mark_dirty()))
                _sync_name()
                row2 = QHBoxLayout()
                # 已带图 → 悬停预告「重新采集·覆盖」（点击链照旧走 _request_capture 分流）
                btn_cap = _icon_btn(icons.CAMERA,
                                    _RECUT_TIP if m.get("name") else _CAP_TIP)
                btn_cap.clicked.connect(
                    lambda _=False, n=node, mm=m, c=cmb_cat: self._request_capture(
                        c.currentData(), n, mm))
                row2.addWidget(btn_cap)
                for b in _row_move_btns(mods, j, lambda: (self._mark_dirty(), _resync())):
                    row2.addWidget(b)   # ↑↓ 排在 × 前，钮簇统一靠右
                btn_rm = _icon_btn(icons.DELETE, "删除这一行")
                btn_rm.clicked.connect(
                    lambda _=False, k=j: (mods.pop(k), self._mark_dirty(), _resync()))
                row2.addWidget(btn_rm)
                row2.addStretch()   # 图标钮不许当弹性件：宽度不足时由这里吸收
                lay.addLayout(row)
                lay.addLayout(row2)
            btn_add_m = _add_btn("添加模块")
            btn_add_m.clicked.connect(
                lambda: (mods.append({"category": "", "name": ""}),
                         self._mark_dirty(), _resync()))
            lay.addWidget(btn_add_m)
        elif st == "subflow":
            cmb_flow = QComboBox()
            cur_f = (s.get("flow_name") or "").strip()
            opts = [fn for fn in self._flows() if fn != self.flow_name]
            if cur_f and cur_f not in opts:
                opts.append(cur_f)
            _fill_flow_grouped(cmb_flow, opts, cur_f)   # 2026-10-06 同分组口径
            cmb_flow.currentIndexChanged.connect(
                lambda _=False, c=cmb_flow, x=s: (x.__setitem__("flow_name", c.currentData()),
                                            self._mark_dirty()))
            f = QFormLayout()
            f.addRow("子流程", cmb_flow)
            lay.addLayout(f)
            # 调用绑定形参表（F2）：与画布 subflow 面板同一份编辑区
            lay.addWidget(QLabel("子流程参数（进入前逐个求值写入流程变量）："))
            lay.addWidget(self._params_box(s, repaint=node))
        elif st == "signal":
            # 循环体里的发信号行：只填信号名（外层自己套循环收不到自己体内的
            # 信号——名单挂在外面那层容器上，这里发的名要让谁掐就登记进谁）
            ed = QLineEdit(str(s.get("name") or ""))
            ed.setPlaceholderText("信号名，如：收工")
            ed.textChanged.connect(
                lambda t, x=s: (x.__setitem__("name", t), self._mark_dirty()))
            f = QFormLayout()
            f.addRow("信号名", ed)
            lay.addLayout(f)
        elif st == "stop_flow":
            # 循环体里的「结束流程」行（2026-10-11 F4）：主用途 = 跳过本轮
            # （等价循环 continue），也支持跳出循环/全局急停。范围下拉与画布
            # 节点共用一份文案（存英文值防断配）。
            f = QFormLayout()
            cmb_scope = self._cbo(s.get("stop_scope", "continue"),
                                  [("跳过本轮（本轮剩余不做了）", "continue"),
                                   ("跳出循环（结束本循环）", "local"),
                                   ("结束整条流程（全局急停）", "global")],
                                  s, "stop_scope")
            cmb_scope.setToolTip(
                "跳过本轮 = 命中即丢掉本轮剩余行、立即开始下一轮（循环的 continue）；\n"
                "跳出循环 = 结束本循环，主线接下一步；\n"
                "结束整条流程 = 不管埋在哪一层，命中即整条任务立即停止。")
            f.addRow("结束范围", cmb_scope)
            lay.addLayout(f)
        return w


    def _sentinel_head(self, target, cool_label, cool_tip, cool_hi, fail_tip):
        """哨兵抬头两行（监视/定时同款共用一份，2026-10-09 收口）：
        cooldown_secs 单位秒带小数 + fail_limit 连续失败停用次数。
        同一个键在两种哨兵里语义不同（监视=命中后冷却、定时=失败重试），
        文案/上限各走参数，写回目标恒为 target 的本名键。"""
        f1 = QFormLayout()
        spn_cool = self._dspin(target.get("cooldown_secs", 5), 0.5, cool_hi,
                               target, "cooldown_secs", decimals=1)
        spn_cool.setToolTip(cool_tip)
        f1.addRow(cool_label, spn_cool)
        spn_fail = self._spin(int(target.get("fail_limit", 3)), 1, 20,
                              target, "fail_limit")
        spn_fail.setToolTip(fail_tip)
        f1.addRow("失败停用(次)", spn_fail)
        return f1

    def _build_watch_container_editor(self, node):
        """并行监视容器属性编辑：顶部编号页签一组一页，页内完全复用
        _build_watch_editor（组 dict 与节点 dict 的 rows/cooldown_secs/
        fail_limit 同形，零胶水）。页签栏角上 ＋/－钮 = 新建组 / 删除本组
        （－钮/拆出都不摊平单组容器；删到 0 组空壳才退回普通哨兵）。
        当前页按组 id 记忆（self._watch_tab_gid）：增删行会整场重建表单，
        不记就会从正在编辑的组弹回第①组。"""
        tabs = _CurPageTabs()
        # QTabWidget 缺省纵向 Expanding：属性面板里会把视口富余高度整个吞掉，
        # 页又必然铺满 tab 区 → 页内无尾 stretch 时缝隙摊进行与行中间，
        # 表现为「失败停用」和「监视行」之间一大块空白（用户实拍）。
        # Preferred = 高度只按页内容 sizeHint 长，富余留给面板尾部 stretch；
        # _CurPageTabs 再治「按所有页取最高」：空组页不再被长组页撑出页内空白。
        tabs.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        gs = container_groups(node)
        if gs is None:
            gs = []
            node["groups"] = gs
        gid_cur = getattr(self, "_watch_tab_gid", None)
        for i, g in enumerate(gs):
            gl = str(g.get("label") or "").strip()
            if len(gl) > 6:
                gl = gl[:6] + "…"
            idx = tabs.addTab(self._build_watch_editor(node, group=g),
                              f"{_group_badge(i)} {gl}" if gl else _group_badge(i))
            tabs.setTabToolTip(
                idx, "监视组 %s%s：组间轮转扫描，组内行序 = 优先级；"
                     "画布上拖组标题行可拆出去（拆出也是一台容器）"
                     % (_group_badge(i),
                        f"「{g.get('label') or ''}」" if gl else ""))
            if g.get("id") == gid_cur:
                tabs.setCurrentIndex(idx)
        if 0 <= tabs.currentIndex() < len(gs):
            gid_cur = gs[tabs.currentIndex()].get("id")

        def _remember(i):
            g = gs[i] if 0 <= i < len(gs) else None
            self._watch_tab_gid = g.get("id") if g else None
            tabs.updateGeometry()   # 换页 → 重算「当前页高」，长短页互切不留页内空白
        tabs.currentChanged.connect(_remember)

        def _rebuild_after():
            self._mark_dirty()
            self._rebuild_scene()
            self._reselect_node(node)
            self._rebuild_props(node)

        def _add_group():
            if self._guard_edit("新建监视组"):
                return
            g = {"id": _graph_id(), "rows": [],
                 "cooldown_secs": 5, "fail_limit": 3, "scan_mode": "restart"}
            gs.append(g)
            self._watch_tab_gid = g["id"]
            _rebuild_after()

        def _del_group():
            i = tabs.currentIndex()
            if not (0 <= i < len(gs)):
                return
            if self._guard_edit("删除监视组"):
                return
            gs.pop(i)
            collapse_watch_container(node)
            self._watch_tab_gid = None
            _rebuild_after()
            self._set_status("已删除监视组")

        corner = QWidget()
        cl = QHBoxLayout(corner)
        cl.setContentsMargins(0, 0, 4, 0)
        cl.setSpacing(2)
        b_add = _icon_btn(icons.ADD, "新建监视组")
        b_add.clicked.connect(_add_group)
        b_rm = _icon_btn(icons.DELETE, "删除当前监视组")
        b_rm.clicked.connect(_del_group)
        cl.addWidget(b_add)
        cl.addWidget(b_rm)
        tabs.setCornerWidget(corner)
        tabs._clamp()   # 初始钳一次（addTab 时页还没摆好，currentChanged 未必再响）
        return tabs

    def _build_watch_editor(self, node, group=None):
        """并行监视哨兵属性编辑：
        - 两个行级参数：命中一条子流程跑完后的冷却秒数（防连点劫持循环）、
          连续失败几次停用该行（防顽固弹窗饿死主线，用户拍板 ①）
        - rows 每行 = {"category","name","kind","flow_name","tap_on_hit"}：
          kind 四型 = "tap"（命中即点，纯关弹窗）/ "ban"（封号停用：命中即
          收工并标记账号封号）/ "finger"（动态手指 2026-10-11：不认图、无
          category/name，每拍现扫手指点它指的位置，参数白/黑阈值+面积带+外延）/
          "subflow"（命中跑子流程，可勾 tap_on_hit 起跑前先点入口图标）。
          归属+模板两列走 _add_cat_name_rows 同款下拉（采/× 子行复用清弹窗行
          模式），子流程列 QComboBox（选流程，同循环体 subflow 行）。
        行增删改节点高度 → 整场重建画布 + 重选 + 重建表单（清弹窗行同款）。
        group=容器里的某一组：rows/cooldown/fail 全编辑组 dict，但增删行、
        采图、点选偏移仍要拿宿主节点画布重绘（node 传容器本体）。"""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        target = group if isinstance(group, dict) else node
        rows = target.setdefault("rows", [])
        # 命中统计登记键：与引擎编译处同口径（顶层流程名, 节点/组 id）——×N 显示靠它对上号
        stats_key = (self.flow_name, str(target.get("id") or node.get("id") or ""))
        for r in rows:
            if isinstance(r, dict):
                r.setdefault("kind", "subflow")   # 老行补默认型（存盘后不再漂移）

        # 抬头两行与定时哨兵共用 _sentinel_head；第三行「命中后扫描」监视专属
        f1 = self._sentinel_head(
            target, "命中冷却(s)",
            # 上限 10 分钟（用户拍板 2026-10-05：低频弹窗歇久点再盯）
            "一条子流程跑完后，这一行歇这么多秒再重新监视\n"
            "（防关不掉的图每拍连点劫持整个流程；最长 10 分钟）", 600,
            "该行连续几次没搞定（子流程跑失败 / 即点点不掉）就停用\n"
            "并打 WARN 日志 —— 防一个关不掉的弹窗反复拖死主线")
        # 命中后扫描顺序（scan_mode，用户拍板 2026-10-05）：显示中文、存英文
        cmb_scan = self._cbo(target.get("scan_mode") or "restart",
                             [("重扫", "restart"), ("续扫", "continue")],
                             target, "scan_mode")
        cmb_scan.setToolTip("命中一行、办完之后，下一轮扫描从哪开始：\n"
                            "重扫 = 永远从第①行（最优先）重来，上面的行一直压着下面的；\n"
                            "续扫 = 从刚命中那行的下一行轮转着扫，下面的行不被饿死。\n"
                            "（命中行照常进冷却，两种模式都不会连点同一行）")
        f1.addRow("命中后扫描", cmb_scan)
        lay.addLayout(f1)
        _lbl_rows = QLabel("监视行（拖行换序 · 点行编辑）：")
        _lbl_rows.setToolTip("从上到下=优先级：列表里拖动一行就换序，"
                             "点选一行在下方编辑；首行最优先。")
        lay.addWidget(_lbl_rows)   # ⚠ 别把说明全塞标题文字：QLabel 单行最小宽=全文宽，
                                   #    长句会把面板顶出 318 视口（实测 360px 出横向滑块）

        def _resync():
            self._rebuild_scene()
            self._reselect_node(node)
            self._rebuild_props(node)

        def _row_index(rr):
            for k, x in enumerate(rows):
                if x is rr:
                    return k
            return -1

        def _light_resync():
            # 换序专用轻通道：行数/节点高度没变，画布只重画这一个方块，
            # 面板不拆建 —— 老 ↑↓ 每点整场重建=闪一下，行一多没人肯调序（病根本修）
            self._mark_dirty()
            self._repaint_node(node)

        # 列表/编辑区/钮条骨架 = 通用 RowListEditor（2026-10-09 收口：手搓的
        # 拖放换算、同行去抖、右键复制/粘贴、选中记忆全与循环体/条件区同一份）。
        def _sum_row(r):
            k = r.get("kind")
            nm = r.get("name") or "（没选图）"
            if k == "tap":
                return f"👆 {nm} 点名关"
            if k == "ban":
                return f"⛔ {nm} 封号停"
            if k == "finger":
                return "🖐 动态手指 → 点它指的位置"
            return (f"{'👆' if r.get('tap_on_hit') else ''}{nm} → "
                    f"{r.get('flow_name') or '（没选流程）'}")

        def _stat_suffix(r):
            """行尾命中统计 ×N（RowListEditor 后缀钩子：只挂显示不落存储）。"""
            hs = watch_stats.snapshot(stats_key).get(_row_index(r))
            return f"×{hs[0]}" if hs and hs[0] else ""

        def _line_menu(menu, rr):
            # 右键扩展 = 把行 dict 本体搬去别的监视（后端方法与冒烟直测共用一份）
            tgts = self._watch_row_move_targets(target)
            if tgts:
                sub = menu.addMenu("移动到其他监视 ▸")
                for nm_, tr in tgts:
                    sub.addAction(nm_).triggered.connect(
                        lambda _c=False, r2=rr, tr=tr, nm_=nm_:
                        self._watch_row_move(node, rows, _row_index(r2), tr, nm_))

        _lbl_ref = [None]  # 当前编辑区信息行 (lbl, cmb_cat, 行dict)——命中统计/试测结果投这里

        def _build_row(r, touch):
            w = QWidget()
            edit_lay = QVBoxLayout(w)
            edit_lay.setContentsMargins(0, 0, 0, 0)
            # ── 选中行编辑区（一行只渲一份，同行去抖由 RowListEditor 兜着）──
            # 上=类型+归属+模板，下=子流程+采+×（清弹窗行同款拆行，
            # 一行塞 5 控件必超 320px 视口被切）
            row = QHBoxLayout()
            cmb_kind = QComboBox()
            # 显示中文、存英文键（kind 是写进 custom_flows 的存储值，不许换中文）
            cmb_kind.addItem("命中即点", "tap")
            cmb_kind.addItem("跑子流程", "subflow")
            cmb_kind.addItem("封号停用", "ban")
            cmb_kind.addItem("动态手指", "finger")
            _ki = cmb_kind.findData(r.get("kind", "subflow"))
            cmb_kind.setCurrentIndex(_ki if _ki >= 0 else 1)
            cmb_kind.setToolTip("命中即点：认出图标直接点它一下（纯关弹窗，不跑子流程）。\n"
                                "跑子流程：认出图标后跑一条子流程办它。\n"
                                "封号停用：认出封号弹窗 = 本实例收工、账号标记封号\n"
                                "（截图留证、表格红显、挂机跳过，右键可解禁）。\n"
                                "动态手指：不认图——每拍现扫「白块+黑描边」的引导手指，\n"
                                "摸到就点它指的位置（对缩放/旋转/只露半边免疫）。")
            cmb_kind.currentIndexChanged.connect(
                lambda _=False, rr=r, c=cmb_kind: (
                    rr.__setitem__("kind", c.currentData()),
                    self._mark_dirty(), _resync()))
            row.addWidget(cmb_kind, 0)
            cmb_cat = QComboBox()
            _fill_category_combo(cmb_cat, r.get("category", ""))
            cmb_name = QComboBox()
            # 下拉最小宽别跟着当前文本走（长模板名一行三个下拉必撑爆
            # 318px 视口，真机实测出横向滑块）：封顶 4 字宽，显示省略号，
            # 点开列表照样全名。只动监视行这三件，_add_cat_name_rows
            # 那四处一行一个下拉，不受影响。
            for c in (cmb_cat, cmb_name):
                c.setSizeAdjustPolicy(
                    QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                c.setMinimumContentsLength(4)
            if r.get("kind") != "finger":
                # 动态手指行不认图：不给归属/模板两列（它没有 category/name）
                row.addWidget(cmb_cat, 1)
                row.addWidget(cmb_name, 2)

            def _sync_name(c=cmb_cat, rr=r, nm=cmb_name):
                self._fill_names_fidelity(nm, rr, c.currentData())
            cmb_cat.currentIndexChanged.connect(lambda _=False, f=_sync_name: f())
            cmb_name.currentTextChanged.connect(
                lambda nm, rr=r: (rr.__setitem__("name", nm),
                                  self._mark_dirty(), touch()))
            _sync_name()
            edit_lay.addLayout(row)
            # 子流程型：流程下拉独占一行拿全宽（长流程名不再被钮簇挤成两字，
            # 用户实拍「猎魂手册」显示成「猎魂手」）；「跑」字删——上面类型下拉
            # 已写明「跑子流程」，一行两个下拉挤一个标签是当初病根。
            # 钮行（即点/采/×）跟在下面（tap/ban 型不跑子流程：没有流程行，
            # 也永远不给「即点」勾选）。
            row2 = QHBoxLayout()
            # 行距收紧：钮簇按默认 6px 间距塞窄容器必出横向滑块（用户实拍），
            # 2px 缝不影响读。
            row2.setSpacing(2)
            if r.get("kind") not in ("tap", "ban", "finger"):
                cmb_flow, btn_flow_edit = self._flow_combo(r, full_row=True)
                row2.addWidget(cmb_flow, 1)
                row2.addWidget(btn_flow_edit, 0)
                edit_lay.addLayout(row2)
                row2 = QHBoxLayout()
                row2.setSpacing(2)
                chk_tap = QCheckBox("即点")
                chk_tap.setChecked(bool(r.get("tap_on_hit")))
                chk_tap.setToolTip("命中这个图标时先自动点它一下再跑子流程：\n"
                                   "子流程里不用再放「点模板」节点进这个页面。\n"
                                   "（点了图标会跳到别的页面的才需要勾）")
                chk_tap.toggled.connect(
                    lambda on, rr=r: (rr.__setitem__("tap_on_hit", on),
                                      self._mark_dirty(), _resync()))
                row2.addWidget(chk_tap, 0)
            if r.get("kind") == "finger":
                # 动态手指行参数：白块阈值/面积带/外延距离。独占一行，避免
                # 318px 视口横向滑块（smoke_prop_width 守）。
                rowp = QHBoxLayout()
                rowp.setSpacing(4)
                rowp.addWidget(QLabel("白≥"), 0)
                rowp.addWidget(self._spin(int(r.get("white_thresh", 200) or 200),
                                          1, 255, r, "white_thresh"), 1)
                rowp.addWidget(QLabel("黑≤"), 0)
                rowp.addWidget(self._spin(int(r.get("dark_thresh", 70) or 70),
                                          1, 255, r, "dark_thresh"), 1)
                edit_lay.addLayout(rowp)
                rowp2 = QHBoxLayout()
                rowp2.setSpacing(4)
                rowp2.addWidget(QLabel("面积"), 0)
                rowp2.addWidget(self._spin(int(r.get("min_area", 800) or 800),
                                           1, 200000, r, "min_area"), 1)
                rowp2.addWidget(self._spin(int(r.get("max_area", 20000) or 20000),
                                           1, 400000, r, "max_area"), 1)
                edit_lay.addLayout(rowp2)
                # 自动外延开关：勾了从指尖沿指向扫到目标内部（钉在彩色 UI 元素
                # 的饱和度加权中心），指尖离目标距离每次不同都自适应；不勾走
                # 手动外延（下面那行的固定像素）。勾上后手动外延输入禁用。
                chk_auto = QCheckBox("自动外延（钉到手指指向的目标上）")
                chk_auto.setChecked(bool(r.get("auto_extend")))
                chk_auto.setToolTip(
                    "勾上：从指尖顺着手势方向自动扫描，把点击点钉在手指指向的\n"
                    "彩色按钮/图标上（找连续高饱和区间的饱和度加权中心）。\n"
                    "指尖离目标的距离每次不同都能自适应，不用手调外延。\n"
                    "不勾：用下面「外延」的固定像素从指尖往前推。")
                chk_auto.toggled.connect(
                    lambda on, rr=r: (rr.__setitem__("auto_extend", on),
                                      self._mark_dirty()))
                edit_lay.addWidget(chk_auto)
                rowp3 = QHBoxLayout()
                rowp3.setSpacing(4)
                rowp3.addWidget(QLabel("外延"), 0)
                spn_ext = QSpinBox()
                spn_ext.setRange(-300, 600)
                spn_ext.setValue(int(r.get("extend_px", 0) or 0))
                spn_ext.setSuffix(" px")
                spn_ext.setToolTip("推算点击点 = 指尖顺着手势方向再往前推这么多像素\n"
                                   "（指尖离目标还有一段就调大；0 = 直接点指尖）。\n"
                                   "勾了「自动外延」后本项不生效。")
                spn_ext.valueChanged.connect(
                    lambda v, rr=r: (rr.__setitem__("extend_px", int(v)),
                                     self._mark_dirty()))
                rowp3.addWidget(spn_ext, 1)
                edit_lay.addLayout(rowp3)
                # 勾上自动外延后禁用手动外延输入（视觉上一眼看出哪条路在生效）
                spn_ext.setEnabled(not bool(r.get("auto_extend")))
                chk_auto.toggled.connect(lambda on, s=spn_ext: s.setEnabled(not on))
            if r.get("kind") != "finger":
                # 动态手指行不认图：不给采集钮（无图可采）
                btn_cap = _icon_btn(icons.CAMERA,   # 回程票落到这一行（r 当 cond 槽位）
                                    _RECUT_TIP if r.get("name") else _CAP_TIP)
                btn_cap.clicked.connect(
                    lambda _=False, n=node, rr=r, c=cmb_cat: self._request_capture(
                        c.currentData(), n, rr))
                row2.addWidget(btn_cap)
            btn_rm = _icon_btn(icons.DELETE, "删除这一行")
            # 走 RowListEditor 统一删除道（sel 记忆/commit/on_struct 一起兜）
            btn_rm.clicked.connect(lambda _: rle._delete(_row_index(r)))
            row2.addWidget(btn_rm)
            row2.addStretch()   # 图标钮不许当弹性件：宽度不足时由这里吸收
            edit_lay.addLayout(row2)
            # 偏移行：凡这一行会"点图标"（即点型 / 勾了即点的子流程型）就给。
            # 行 dict 与节点字段同名（offset_x/offset_y），_begin_pick 直接吃行。
            # 点选钮（自然宽 126px）不塞进偏移行：标签+双 spinbox+钮最小需求
            # 已超 318px 视口，右侧被切 —— 点模板同款「钮独占一行」布局。
            # （动态手指行不走偏移——它点的是现扫出来的坐标，无固定偏移可言。）
            if r.get("kind") == "tap" or r.get("tap_on_hit"):
                row3 = QHBoxLayout()
                row3.addWidget(QLabel("偏移"), 0)
                spn_ox = self._spin(int(r.get("offset_x", 0) or 0), -(BASE_W - 1), BASE_W - 1,
                                    r, "offset_x")
                spn_oy = self._spin(int(r.get("offset_y", 0) or 0), -(BASE_H - 1), BASE_H - 1,
                                    r, "offset_y")
                row3.addWidget(spn_ox, 1)
                row3.addWidget(spn_oy, 1)
                edit_lay.addLayout(row3)
                row4 = QHBoxLayout()
                row4.addWidget(self._pick_btn(r, "offset", "点选目标位算偏移",
                                              owner=node))
                row4.addStretch()
                edit_lay.addLayout(row4)
            if r.get("kind") == "finger":
                # 动态手指行：给「▶ 试扫手指」单帧验证 + 结果行（2026-10-11 用户
                # 诉求：怎么知道参数调没调对、捕捉到没有）。点击发 request_finger_test
                # （带本行参数快照）→ 外壳抓现帧跑 detect_finger、所见即所得。
                tip = QLabel("每拍现扫白块+黑描边手指，摸到即点它指的位置（行尾 ×N = 已点次数）。")
                tip.setWordWrap(True)
                tip.setStyleSheet("color: #888;")
                edit_lay.addWidget(tip)
                rowf = QHBoxLayout()
                btn_ftest = QPushButton("▶ 试扫手指")
                btn_ftest.setToolTip(
                    "拿当前画面按下面的参数扫一次手指（只检测、不点击）——\n"
                    "回答「这样调能不能捕捉到」；检测到的框/指尖/落点会画在预览上。\n"
                    "需右侧已选实例且模拟器停在手指出现的界面。")
                def _finger_test(rr=r):
                    # 传行 dict 本体（workbench 原样回传同引用，apply_finger_result
                    # 靠 id 认领结果；不要 dict(rr) 拷贝，否则 id 对不上）
                    self._finger_test_results.pop(id(rr), None)
                    self.request_finger_test.emit(rr)
                btn_ftest.clicked.connect(lambda _=False, f=_finger_test: f())
                rowf.addWidget(btn_ftest)
                lbl_f = QLabel("")
                lbl_f.setWordWrap(True)
                self._finger_lbl_ref = (lbl_f, r)
                rowf.addWidget(lbl_f, 1)
                edit_lay.addLayout(rowf)
                self._upd_finger_lbl(lbl_f, r)
                return w
            # 试测行（所有类型都给）：「▶测这行」单图验证 + 命中统计/试测信息。
            # 独占一行不塞 row2——318px 视口红线（smoke_prop_width 守）。
            row5 = QHBoxLayout()
            btn_test = QPushButton("▶测这行")
            btn_test.setToolTip(
                "拿当前画面按这行的图找一次（只定位不落手）——\n"
                "回答「这张图本身找得到吗」；挂机灵不灵看行尾 ×N。\n"
                "需右侧已选实例且模拟器在跑。重采同名覆盖才是修图的路。")
            def _test_row(rr=r, c=cmb_cat):
                cat_ = c.currentData() or ""
                nm_ = rr.get("name") or ""
                if not nm_:
                    self._row_test_results[(cat_, "")] = "这行还没选图"
                else:
                    self._row_test_results.pop((cat_, nm_), None)
                    self.request_row_test.emit(cat_, nm_)
                _refresh_stats()
            btn_test.clicked.connect(lambda _=False, f=_test_row: f())
            row5.addWidget(btn_test)
            lbl_info5 = QLabel("")
            lbl_info5.setWordWrap(True)
            _lbl_ref[0] = (lbl_info5, cmb_cat, r)
            row5.addWidget(lbl_info5, 1)
            edit_lay.addLayout(row5)
            lbl_info5.setText(_row_line_text(cmb_cat.currentData() or "", r))
            return w

        def _row_line_text(cat, rr):
            """编辑区信息行：命中统计 ｜ 图偏移提示 ｜ ▶测这行结果。"""
            hs = watch_stats.snapshot(stats_key).get(_row_index(rr))
            parts = [f"命中 ×{hs[0]} · 最近 {self._rel_age(hs[1])}" if hs and hs[0]
                     else "本轮还没命中过（配好可点▶测这行）"]
            nm = rr.get("name") or ""
            if nm:
                ent = template_manager.find_template(cat, nm) or {}
                co = ent.get("click_offset")
                if co is not None and not (int(rr.get("offset_x", 0) or 0)
                                           or int(rr.get("offset_y", 0) or 0)):
                    parts.append(f"行偏移未配·跟随图({int(co[0])},{int(co[1])})")
            res = self._row_test_results.get((cat, nm))
            if res:
                parts.append(f"试测：{res}")
            return "｜".join(parts)

        def _refresh_stats():
            """把命中统计刷进列表行尾 ×N 和当前编辑区信息行（3s 定时器 + 试测回投都走这）。"""
            rle.refresh_texts()
            # 信息行只投给「还在编辑区里坐着」的那行：换行/拆建后 _lbl_ref 指的
            # 是旧控件（deleteLater 在路上），不比对身份就会投进死控件（同款去抖坑）
            ent = _lbl_ref[0]
            if ent is not None and ent[2] is rle.editing_row():
                lbl, cmb, rr = ent
                lbl.setText(_row_line_text(cmb.currentData() or "", rr))

        def _sel_get():
            e = self._watch_sel_rows.get(id(rows))   # 按组各记各的：id(rows) 做键
            return e[1] if e is not None and e[0] is rows else None

        def _sel_set(rr):
            self._watch_sel_rows[id(rows)] = (rows, rr)

        rle = RowListEditor(
            get_rows=lambda: rows,
            summary=_sum_row,
            suffix=_stat_suffix,
            build=_build_row,
            commit=lambda rr: rows.__setitem__(slice(None), rr),
            on_struct=lambda: (self._mark_dirty(), _resync()),
            on_reordered=_light_resync,
            sel_get=_sel_get, sel_set=_sel_set,
            # 剪贴板走面板级后端（跨组/跨流程粘贴不丢；复制状态提示也在这份里）
            clip_get=lambda: self._watch_row_clip,
            clip_set=self._watch_row_copy,
            menu_ext=_line_menu,
            add_text="添加监视行",
            new_item=lambda: {"category": "", "name": "", "flow_name": "",
                              "kind": "subflow"},
            min_rows=0,
            list_tip="从上到下 = 优先级：拖一行换序，点一行在下面编辑；"
                     "右键 = 复制/粘贴/删除/移动到其他监视。",
            empty_hint="（点上面一行开始编辑，或「添加监视行」放一条）")
        lay.addWidget(rle)

        self._watch_stats_hook = _refresh_stats   # 试测回投 apply_row_result 靠它刷信息行
        if self._watch_stat_timer is None:        # 全面板共用一个 3s 定时器
            self._watch_stat_timer = QTimer(self)
            self._watch_stat_timer.setInterval(3000)
            self._watch_stat_timer.timeout.connect(
                lambda: self._watch_stats_hook and self._watch_stats_hook())
        self._watch_stat_timer.start()
        lay.addWidget(_hint("放上画布就全程在岗：主线照跑它的，它认到哪个弹窗\n"
                            "就跑哪条子流程；子流程在跑时主线整条自动暂停，\n"
                            "跑完自动续跑（不用接任何线）。"))
        # 尾垫 stretch：页被拉高时富余全落底部，内容贴顶（同 tabs 那处修复，双保险）
        lay.addStretch()
        return box

    def _build_assets_editor(self, node):
        """资产入库条目编辑：每行「流程变量 → 资产显示名」。
        变量下拉备选项 = 本流程「识字取物·读数」现扫出来的变量名（现场派生
        不落存，_print_target_var 同款），也允许手打（接子流程存的变量）。
        行增删 → 整场重建（画布节点随行数长高是 watch/timer 的事，本节点不长，
        重建只为让删除后的行号连续）。"""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        items = node.setdefault("items", [])
        var_opts = self._flow_var_names()

        def _resync():
            self._rebuild_props(node)

        for i, it in enumerate(items):
            row = QHBoxLayout()
            cmb = QComboBox()
            cmb.setEditable(True)
            cur = str(it.get("var", ""))
            opts = list(var_opts)
            if cur and cur not in opts:
                opts.insert(0, cur)
            cmb.addItems(opts or [""])
            cmb.setCurrentText(cur)
            cmb.setToolTip("读数步骤（识文读数字/读数字）存进的那个流程变量名")
            cmb.currentTextChanged.connect(
                lambda v, r=it: (r.__setitem__("var", v.strip()), self._mark_dirty()))
            row.addWidget(cmb, 2)
            row.addWidget(QLabel("→"), 0)
            edt = QLineEdit(str(it.get("name", "")))
            edt.setPlaceholderText("资产名，如：金币")
            edt.setToolTip("账本里怎么记（改变量名不乱账；留空 = 用变量名记）")
            edt.textChanged.connect(
                lambda v, r=it: (r.__setitem__("name", v.strip()), self._mark_dirty()))
            row.addWidget(edt, 2)
            btn_rm = _icon_btn(icons.DELETE, "删除这一条")
            btn_rm.clicked.connect(
                lambda _=False, k=i: (items.pop(k), self._mark_dirty(), _resync()))
            row.addWidget(btn_rm, 0)
            lay.addLayout(row)

        btn_add = _add_btn("添加条目")
        btn_add.clicked.connect(
            lambda: (items.append({"var": "", "name": ""}),
                     self._mark_dirty(), _resync()))
        lay.addWidget(btn_add)
        lay.addWidget(_hint("前面先用「识字取物」的读数用法（读数字/识文读数字）把画面数值读进变量，这里点名存哪几个。\n"
                            "没绑账号 / 变量没读到只告警不拦路，账按最新一次读数覆盖。\n"
                            "存好后的数值看表格「资产」列，右键「查看资产」看明细。"))
        return box

    def _flow_combo(self, r: dict, full_row: bool = False):
        """行 → 子流程下拉（选完写回 r["flow_name"]，同下拉惯例存英文键）。
        watch/timer 行共用一份（两份必漂移）。
        full_row=True = 下拉独占一行（watch 改版后）：不设 68px 窄上限，
        最小宽按 10 字封顶防超长名顶爆视口，平时随 stretch 拉满。"""
        cmb = QComboBox()
        cur_f = (r.get("flow_name") or "").strip()
        opts = list(self._flow_names())
        if cur_f and cur_f not in opts:
            opts.append(cur_f)   # 存的目标已被删/改名：垫末尾保住不"偷梁换柱"
        _fill_flow_grouped(cmb, opts, cur_f, "（选子流程）")   # 2026-10-06 同分组口径
        # ⚠ QComboBox 的收缩下限=最长项文字宽（占位符"（选子流程）"就 102px），
        # 挤在钮簇一行时 326px > 318 视口 → 横向滑块（用户实拍）。尺寸策略按
        # 最小内容宽算下限，长名显示省略号；上限只给挤行（timer）设。
        cmb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        if full_row:
            cmb.setMinimumContentsLength(10)   # 整行宽绰：下限按 10 字算
        else:
            cmb.setMaximumWidth(68)            # 挤行老布局：窄上限防顶爆视口（timer 行）
            cmb.setMinimumContentsLength(4)
        cmb.currentIndexChanged.connect(
            lambda _=False, c=cmb, rr=r: (rr.__setitem__("flow_name", c.currentData()),
                                    self._mark_dirty()))
        # 「编辑」铅笔：点一下就地跳到这条子流程的画布（先问未保存，
        # 用户拒绝就还原列表选中、什么都不发生 —— 与双击流程清单同款链路）。
        btn_edit = _icon_btn(icons.EDIT, "打开这条子流程进行编辑")
        btn_edit.clicked.connect(
            lambda _=False, c=cmb: (fn := c.currentData()) and self._select_flow(fn))
        return cmb, btn_edit

    def _build_timer_editor(self, node):
        """定时执行哨兵属性编辑：版式同 _build_watch_editor，行内容换成
        「间隔分钟 → 子流程」（不认图，所以没有归属/模板列和采图按钮）。
        cooldown_secs 在这里的语义 = 子流程失败后的重试间隔（成功歇整周期）。
        行增删 → 整场重建（画布节点随行数长高，同款先例）。"""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        rows = node.setdefault("rows", [])

        # 抬头两行与监视哨兵共用 _sentinel_head（同键不同语义：这里=失败重试间隔）
        lay.addLayout(self._sentinel_head(
            node, "失败重试(s)",
            "子流程跑失败后，歇这么多秒就重试\n（跑成功不歇这个 —— 直接歇满整周期）", 120,
            "同一条子流程连续失败几次就停用该行（本流程内不再执行）\n"
            "并打 WARN 日志 —— 防一条坏流程反复拖死主线"))
        lay.addWidget(QLabel("执行行（各自独立计时，到点即跑子流程）："))

        def _resync():
            self._rebuild_scene()
            self._reselect_node(node)
            self._rebuild_props(node)

        for i, r in enumerate(rows):
            row = QHBoxLayout()
            row.addWidget(QLabel("每"), 0)
            try:
                iv0 = float(r.get("interval_min", 5))
            except (TypeError, ValueError):
                iv0 = 5.0
            spn_iv = self._dspin(iv0, 0.1, 9999, r, "interval_min",
                                 decimals=1, suffix=" 分钟",
                                 cast=lambda v: round(v, 1))
            row.addWidget(spn_iv, 1)
            row.addWidget(QLabel("→"), 0)
            cmb_flow, btn_flow_edit = self._flow_combo(r)
            cell = QHBoxLayout(); cell.setContentsMargins(0, 0, 0, 0)
            cell.addWidget(cmb_flow); cell.addWidget(btn_flow_edit)
            row.addLayout(cell, 2)
            lay.addLayout(row)
            # 钮簇拆下行（监视行同款：原行 6 件已近满栏宽，硬塞 ↑↓× 必出视口）
            row2 = QHBoxLayout()
            for b in _row_move_btns(rows, i, lambda: (self._mark_dirty(), _resync())):
                row2.addWidget(b)   # ↑↓ 排在 × 前：同拍多行到点按行序先来后到
            btn_rm = _icon_btn(icons.DELETE, "删除这一行")
            btn_rm.clicked.connect(
                lambda _=False, k=i: (rows.pop(k), self._mark_dirty(), _resync()))
            row2.addWidget(btn_rm)
            row2.addStretch()
            lay.addLayout(row2)

        btn_add = _add_btn("添加执行行")
        btn_add.clicked.connect(
            lambda: (rows.append({"interval_min": 5, "flow_name": ""}),
                     self._mark_dirty(), _resync()))
        lay.addWidget(btn_add)
        lay.addWidget(_hint("放上画布就全程在岗：每行按自己的间隔到点跑子流程，\n"
                            "跑完歇满下一个周期；子流程在跑时主线整条自动暂停，\n"
                            "跑完自动续跑。第一次触发在流程起跑后满一个间隔时。"))
        return box

    def _flow_names(self) -> list:
        """当前可引用的流程名列表（不含本流程自己），编辑期校验/下拉共用。"""
        return sorted(fn for fn in self._flows() if fn != self.flow_name)

    def _goto_capture_btn(self, cmb_cat: QComboBox, node: dict,
                          cond: dict | None = None):
        """「缺图？去采集」：切到采模板模式并把当前归属目录传给面板预填；
        node/cond 记下回程票 —— 采完的那张图自动填回这个节点（或它的分支条件）。
        相机图标 + 文字：图标走 QIcon（见 icons.glyph_icon），不能给按钮设
        图标字体——Segoe MDL2 没有中文字形，整串中文会变方块。
        这个按钮是跨页跳转的主操作，文字留着更好认，只收回自然宽度。
        槽位已带图时按钮变「重新采集·覆盖」（_request_capture 带名 → 采集页锁名）。"""
        _has = bool(str(((cond if cond is not None else node) or {}).get("name") or ""))
        btn = _fit_btn(QPushButton("重新采集·覆盖" if _has else "缺图？去采集"))
        btn.setIcon(icons.glyph_icon(icons.CAMERA))
        btn.setIconSize(QSize(14, 14))
        btn.setToolTip(_RECUT_TIP if _has else
                       "切到「模板采集」页，把新采的图自动填回这个节点的模板格；\n"
                       "归属目录已按当前选择预填好。")
        btn.clicked.connect(lambda: self._request_capture(cmb_cat.currentData(),
                                                          node, cond))
        return btn

    @staticmethod
    def _rel_age(ts: float) -> str:
        """最近命中时刻 → 人话（显示用，分钟级精度够）。"""
        d = max(0.0, time.time() - ts)
        if d < 45:
            return "刚刚"
        if d < 5400:
            return f"{int(d // 60)}分钟前"
        if d < 172800:
            return f"{int(d // 3600)}小时前"
        return f"{int(d // 86400)}天前"

    def apply_row_result(self, category: str, name: str, text: str):
        """「▶测这行」结果回投（workbench 同步回调）：记账 + 编辑器开着就轻刷。"""
        self._row_test_results[(category or "", name or "")] = text
        if self._watch_stats_hook is not None:
            self._watch_stats_hook()

    def apply_finger_result(self, row: dict, text: str):
        """「▶ 试扫手指」结果回投（workbench 同步回调）：把结果挂到该行 + 刷编辑区。
        row 是发起时传的快照 dict 的**同身份**引用（workbench 原样回传），
        按身份找当前编辑器里对应行刷标签。"""
        self._finger_test_results[id(row)] = text
        ref = getattr(self, "_finger_lbl_ref", None)
        if ref is not None and ref[1] is row:
            self._upd_finger_lbl(ref[0], row)

    def _upd_finger_lbl(self, lbl, row: dict):
        """手指行结果标签文案（按行身份取上次结果；没扫过给引导语）。"""
        res = self._finger_test_results.get(id(row))
        if res is None:
            lbl.setText("还没试扫过（点左边「▶ 试扫手指」）")
            lbl.setStyleSheet("color: #888;")
        else:
            lbl.setText(res)
            lbl.setStyleSheet(f"color: {theme.SUCCESS if res.startswith('✓') else theme.WARN};")

    # ── 监视行 复制/粘贴/移动（右键菜单的后端，方法化=冒烟可直测不弹模态）──

    def _watch_resync(self, node):
        """行增删/跨目标搬运后整场重建（行数变→节点高度变→画布+表单都要跟上）。"""
        self._rebuild_scene()
        self._reselect_node(node)
        self._rebuild_props(node)

    def _watch_row_copy(self, r: dict):
        self._watch_row_clip = copy.deepcopy(r)
        self._set_status("已复制监视行：可在本组右键粘贴，或切别的监视/别的流程再粘贴")

    def _watch_row_paste(self, node, rows: list, after_idx: int):
        """剪贴板行 deepcopy 插到 after_idx 之后（-1/越界 = 追加末尾）。
        粘贴件与剪贴板互不相干：连贴多次不共享同一 dict。"""
        if self._watch_row_clip is None:
            return
        nr = copy.deepcopy(self._watch_row_clip)
        k = after_idx + 1 if 0 <= after_idx < len(rows) else len(rows)
        rows.insert(k, nr)
        self._watch_sel_rows[id(rows)] = (rows, nr)
        self._mark_dirty()
        self._watch_resync(node)

    def _watch_row_move_targets(self, exclude):
        """本流程内可当移动落点的监视目标 [(显示名, rows 表)]：每个 watch 节点
        （容器摊开成逐组），排除当前正在编辑的 target 自身。"""
        out = []
        for itm in self.scene.items():
            if not isinstance(itm, FlowNodeItem):
                continue
            n = itm.node
            if n.get("type") != "watch":
                continue
            lbl = (n.get("label") or "").strip() or "（没备注）"
            gs = container_groups(n)
            if gs is None:
                if n is not exclude:
                    out.append((f"监视节点「{lbl}」", n.setdefault("rows", [])))
            else:
                for gi, g in enumerate(gs):
                    if g is not exclude:
                        out.append((f"容器「{lbl}」组{_group_badge(gi)}",
                                    g.setdefault("rows", [])))
        return out

    def _watch_row_move(self, node, rows: list, idx: int, tgt_rows: list, name: str):
        """把第 idx 行搬去别的目标：搬的是行 dict 本体（同一对象），不是复印。"""
        if not (0 <= idx < len(rows)):
            return
        r = rows.pop(idx)
        tgt_rows.append(r)
        self._mark_dirty()
        self._watch_resync(node)
        self._set_status(f"已移动监视行 → {name}")

    def _request_capture(self, category: str, node: dict | None = None,
                         cond: dict | None = None):
        self._capture_return = {"node": node, "cond": cond} if node is not None else None
        # 槽位已带图 → 把名字一起带给采集页 = 「重新采集·同名覆盖」锁名模式
        # （没图带空串 = 老的新建采集路，一处出口全部分流）
        tgt = cond if cond is not None else node
        cur_name = str((tgt or {}).get("name") or "") if tgt is not None else ""
        self.request_capture.emit(category or "", cur_name)

    def _on_timeout_combo(self, node):
        """「超时行为」下拉（click_template / wait_template 共用）：
        存储值 exit/continue 与编译侧一致，显示文案说人话。
        exit = 直接退出程序（硬终止，穿透失败恢复链）——已取消"判失败走
        恢复链"选项，存量 fail 值在新编译语义下等同于 exit。"""
        return self._cbo(node.get("on_timeout", "exit"),
                         [("直接退出程序并打印错误到日志", "exit"),
                          ("跳过这步继续", "continue")],
                         node, "on_timeout")

    def _spin(self, value, lo, hi, node, key, suffix=None):
        spn = QSpinBox()
        spn.setRange(lo, hi)
        if suffix is not None:
            spn.setSuffix(suffix)
        spn.setValue(int(value))
        spn.valueChanged.connect(
            lambda v, n=node, k=key: (n.__setitem__(k, v), self._mark_dirty()))
        return spn

    def _dspin(self, value, lo, hi, node, key, decimals=None, step=None,
               suffix=None, special=None, cast=None):
        """小数自增框回写五连（建→量程→值→valueChanged→n[key]+脏标），_spin 同款纪律，
        node 可以是任何 dict（节点/循环体行/条件）。cast=int/lambda 等 = 保住老代码
        的存储类型（timeout 等键历史上存 int，别漂成 float 动配置语义）；
        tooltip/specialValueText 之外的长文案在调用方链式补。"""
        spn = QDoubleSpinBox()
        spn.setRange(lo, hi)
        if decimals is not None:
            spn.setDecimals(decimals)
        if step is not None:
            spn.setSingleStep(step)
        if suffix is not None:
            spn.setSuffix(suffix)
        if special is not None:
            spn.setSpecialValueText(special)
        spn.setValue(float(value))
        spn.valueChanged.connect(
            lambda v, n=node, k=key: (n.__setitem__(k, cast(v) if cast else v),
                                      self._mark_dirty()))
        return spn

    def _cbo(self, current, items, node, key):
        """纯回写下拉三件套：建→灌 (显示文本, 存储值)→findData 回落首项→
        currentIndexChanged→n[key]=currentData()+脏标。显示中文、存储英文
        （存储值写进 config，换存储值老流程全断配）。"""
        cmb = QComboBox()
        for text, data in items:
            cmb.addItem(text, data)
        idx = cmb.findData(current)
        cmb.setCurrentIndex(idx if idx >= 0 else 0)
        cmb.currentIndexChanged.connect(
            lambda _=False, n=node, c=cmb: (n.__setitem__(key, c.currentData()),
                                            self._mark_dirty()))
        return cmb

    def _chk(self, checked, node, key, text):
        """纯回写复选框三件套：建→set→toggled→n[key]=bool+脏标。
        有显隐联动/键弹出等副作用的别用它（那类留在调用方）。"""
        chk = QCheckBox(text)
        chk.setChecked(bool(checked))
        chk.toggled.connect(
            lambda on, n=node, k=key: (n.__setitem__(k, on), self._mark_dirty()))
        return chk

    def _pick_btn(self, node, field, text="在画面点选坐标", owner=None):
        """预览取点钮（点坐标/滑动/偏移共用）。自然宽度：这个动作在表单里
        出现两三次，每次都拉成满行横幅太抢眼；提示补足"点了会怎样"。
        node = 值写回目标（行场景传行 dict，字段同名直接复用）；
        owner = 宿主节点 dict，仅"取完重建表单"用（不传 = 用 node 自己）。"""
        btn = QPushButton(text)
        btn.setToolTip("点了直接去左侧预览上单击取点，取完自动回填上面的坐标")
        btn.clicked.connect(lambda _=False, n=node, f=field, o=owner: self._begin_pick(n, f, o))
        return _fit_btn(btn)

    def _build_print_target_editor(self, node):
        """打印节点的 target 编辑（none / template / color 切换 + 各自参数）。
        结构仿 _build_cond_editor：下拉定类型，子面板按需重建。"""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        cmb_kind = QComboBox()
        # 显示文本 ≠ 存储值：addItems 不设 data（currentData 返回 None），必须 addItem 带 data
        for data, text in (("none", "只打文本"),
                           ("template", "打模板匹配"),
                           ("color", "打坐标颜色"),
                           ("var", "打变量值")):
            cmb_kind.addItem(text, data)
        idx = cmb_kind.findData(node.get("target", "none"))
        cmb_kind.setCurrentIndex(idx if idx >= 0 else 0)
        lay.addWidget(cmb_kind)

        stack_kind = {
            "template": lambda: self._print_target_template(node),
            "color": lambda: self._print_target_color(node),
            "var": lambda: self._print_target_var(node),
            "none": lambda: QWidget(),
        }

        def show(cur):
            while lay.count() > 1:
                w = lay.takeAt(1)
                if w.widget():
                    w.widget().deleteLater()
            lay.addWidget(stack_kind[cur]())
            node["target"] = cur
            self._repaint_node(node)  # 缩略图随 target 类型出现/消失
            self._mark_dirty()  # 构建期由 _building_props 闸门忽略，只有用户切换才标脏

        cmb_kind.currentIndexChanged.connect(
            lambda _=False, c=cmb_kind: show(c.currentData()))
        show(cmb_kind.currentData())
        return box

    def _print_target_template(self, node):
        """打印目标=模板：归属 + 模板下拉（与点模板节点同款交互）。"""
        w = QWidget()
        self._add_cat_name_rows(QFormLayout(w), node)
        return w

    def _print_target_color(self, node):
        """打印目标=坐标：x/y + 在预览画面点选（复用滑动/点坐标的取点交互）。"""
        w = QWidget()
        f = QFormLayout(w)
        f.addRow("x", self._spin(node.get("tap_x", 0), 0, BASE_W - 1, node, "tap_x"))
        f.addRow("y", self._spin(node.get("tap_y", 0), 0, BASE_H - 1, node, "tap_y"))
        f.addRow(self._pick_btn(node, "click"))
        return w

    def _print_target_var(self, node):
        """打印目标=变量：变量名下拉（本流程现扫出来的读数变量 + 手打）。
        打文本行的 {变量名} 占位符也生效，这里只负责"点名打一个变量"。"""
        w = QWidget()
        f = QFormLayout(w)
        cmb = QComboBox()
        cmb.setEditable(True)
        cur = str(node.get("var_name", ""))
        opts = self._flow_var_names()
        if cur and cur not in opts:
            opts.insert(0, cur)
        cmb.addItems(opts or [""])
        cmb.setCurrentText(cur)
        cmb.currentTextChanged.connect(
            lambda v, n=node: (n.__setitem__("var_name", v.strip()),
                               self._mark_dirty()))
        f.addRow("变量名", cmb)
        f.addRow(_hint("想一行里带多个变量：选「只打文本」，文本里直接写\n"
                       "{变量名} 占位符，打印时自动换成现值。"))
        return w

    def _build_branch_cond_area(self, node, allow_motion=True, exit_loop=False):
        """判断/循环出口的多条件区（2026-10-06 监视行同款改版）：条件1..N 全部
        进摘要行列表，点谁谁在下方展开编辑（取反勾 + 全套条件表单）—— 旧版每条
        条件把整套表单当场摊开叠排，三条以上面板失控；现在行永远一行高。

        数据模型没动（零迁移）：cond 恒为第 1 条、extra_conds = 第 2..N 条、
        logic = all/any、每条可 not 取反；换序/删除后按位次重新拆写这两处
        （cond 存的就是列表第 1 行的 dict 本尊）。只剩 1 条时 extra_conds/
        logic 键整个弹出，存储回到纯老形态。
        单条件时列表也有一行 + 表单直接展开（和老版"永远看到条件1表单"等效）。
        exit_loop=True 换措辞（走Y→出循环）；allow_motion=False 沿用循环禁动态。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        def _rows():
            c1 = node.setdefault("cond", {"kind": "template"})
            return [c1] + [c for c in (node.get("extra_conds") or [])
                           if isinstance(c, dict)]

        def _commit(rows):
            node["cond"] = rows[0]
            if len(rows) > 1:
                node["extra_conds"] = rows[1:]
                node.setdefault("logic", "all")
            else:
                node.pop("extra_conds", None)
                node.pop("logic", None)   # 回到纯老形态（两份字段不留残值）

        # 关系下拉只在多条时出现（单条件老面板零杂音；显示中文、存储值英文——
        # logic 写进 config，换存储值老流程断配）
        if len(_rows()) > 1:
            cmb_logic = self._cbo(
                str(node.get("logic", "all")),
                [("满足全部条件才退出循环（与）", "all"),
                 ("满足任一条件即退出循环（或）", "any")] if exit_loop else
                [("满足全部条件才走Y（与）", "all"),
                 ("满足任一条件即走Y（或）", "any")],
                node, "logic")
            lay.addWidget(cmb_logic)

        def _mk(cd, touch):
            wb = QWidget()
            vl = QVBoxLayout(wb)
            vl.setContentsMargins(0, 0, 0, 0)
            chk = QCheckBox("本条取反（不成立才算成立）")
            chk.setChecked(bool(cd.get("not")))

            def _set(on, c=cd):
                if on:
                    c["not"] = True
                else:
                    c.pop("not", None)   # 不留 False 残渣，老引擎看不见=零影响
                self._mark_dirty()
                touch()
            chk.toggled.connect(_set)
            vl.addWidget(chk)
            vl.addWidget(self._build_cond_editor(node, cond=cd,
                                                 allow_motion=allow_motion,
                                                 on_kind=touch))
            return wb

        lay.addWidget(RowListEditor(
            get_rows=_rows, summary=cond_row_summary, build=_mk, commit=_commit,
            on_struct=lambda: (self._mark_dirty(), self._repaint_node(node),
                               self._rebuild_props(node)),
            on_reordered=lambda: self._mark_dirty(),
            sel_get=lambda: getattr(self, "_cond_row_sel", None),
            sel_set=lambda r: setattr(self, "_cond_row_sel", r),
            clip_get=lambda: getattr(self, "_cond_row_clip", None),
            clip_set=lambda c: setattr(self, "_cond_row_clip", c),
            add_text="＋ 添加条件（与/或/取反）",
            new_item=lambda: {"kind": "template", "category": "", "name": ""},
            list_tip="行序不改变语义（与/任一/取反跟行进），拖行只是整理；\n"
                     "点一行在下面编辑这条条件。右键=复制/粘贴/删除。"))
        lay.addWidget(_hint(
            "多个出口条件在每轮收口时一起判：与=全满足才出循环，或=任一满足"
            "即出，每条还能单独取反。" if exit_loop else
            "多个条件同一帧一起判：串联两个判断节点做不到这点"
            "（后一个要等下一拍才评），闪瞬即逝的状态用这里组合。"))
        return w

    def _build_cond_editor(self, node, cond: dict,
                           allow_motion: bool = True, on_kind=None):
        """判断节点的 cond 编辑（kind 切换 + 各自参数）。
        cond = 要编辑的那条条件 dict（多条件行列表的选中行编辑器传入）。
        on_kind = 用户换类型后回调（行列表传 touch：摘要行文本跟着类型走；
        构建期首次 show 不触发 —— 那时列表自己刚灌完）。
        allow_motion=False（循环出口）：motion 是"某状态动起来"的动态窗口语义，
        逐轮查询的出口条件只认静止状态在不在，加了 motion 出口会永远 etc. 在不该
        等动画动起来的语境里 —— 循环出口列为空，编译侧按无条件循环/轮数处理。"""
        kinds = ["template", "color", "multi_color", "text", "var"] \
            + (["motion"] if allow_motion else [])
        if cond.get("kind") not in kinds:
            cond["kind"] = kinds[0]
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        # 显示中文、存储值英文（写进 config 的 cond.kind，换存储值老流程全断配）；
        # 中文名单一来源 _COND_KIND_CN（加一种条件 = 只改那张表 + kinds）
        cmb_kind = QComboBox()
        for k in kinds:
            cmb_kind.addItem(_COND_KIND_CN[k], k)
        cmb_kind.setCurrentIndex(max(0, cmb_kind.findData(cond["kind"])))
        lay.addWidget(cmb_kind)
        # 持续时长（全部条件类型通用，2026-10-11）：条件要【连续成立】满 N 秒
        # 才算成立 —— 判据在引擎侧 _wrap_hold，判断与循环出口同吃。0 = 不要求
        # （不写键，老数据零迁移）。放在类型下拉下、子面板上（子面板随类型重建，
        # 本行不重建）。
        w_hold = self._cond_hold_row(cond, on_change=on_kind)
        lay.addWidget(w_hold)
        _sub = [None]   # 当前子面板（show 只换它，不动上面两行）

        # 按需重建子面板（不预创建 widget，避免把已加入的 widget 二次 add 报错）
        stack_kind = {
            "template": lambda: self._cond_template(node, cond),
            "color": lambda: self._cond_color(node, cond),
            "multi_color": lambda: self._cond_multi(node, cond),
            "text": lambda: self._cond_text(node, cond),
            "var": lambda: self._cond_var(node, cond),
        }
        if allow_motion:
            stack_kind["motion"] = lambda: self._cond_motion(node, cond)

        def show(cur):
            if _sub[0] is not None:
                _sub[0].setParent(None)
                _sub[0].deleteLater()
            w = stack_kind[cur]()
            _sub[0] = w
            lay.addWidget(w)
            cond["kind"] = cur
            # motion 本身就是"时间窗内动没动"的语义，叠持续无意义：藏掉本行
            _vis(w_hold, cur != "motion")
            self._repaint_node(node)  # 缩略图随条件类型出现/消失
            self._mark_dirty()

        # 用户换类型才刷行摘要（构建期首次 show 不触发：列表还没灌完）
        cmb_kind.currentIndexChanged.connect(
            lambda _=False, c=cmb_kind: (show(c.currentData()),
                                   on_kind and on_kind()))
        show(cmb_kind.currentData())
        return box

    def _cond_hold_row(self, cond, on_change=None):
        """条件「持续时长」行（判断/循环出口通用，2026-10-11）：条件要连续成立
        满 N 秒才算成立 —— 闪一下没撑满 = 本拍不成立。存 cond["hold_secs"]
        （0 = 不要求，弹键不留残渣 → 老数据零迁移）。on_change = 值变化回调
        （多条件行列表传 touch 刷摘要行）。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        f = QFormLayout()
        f.setContentsMargins(0, 0, 0, 0)

        def _set(v, c=cond):
            fv = float(v)
            if fv > 0:
                c["hold_secs"] = fv
            else:
                c.pop("hold_secs", None)   # 0 = 不要求：不留键（老引擎看不见=零影响）
            self._mark_dirty()
            on_change and on_change()

        spn = QDoubleSpinBox()
        spn.setRange(0.0, 60.0)
        spn.setDecimals(1)
        spn.setSingleStep(0.5)
        spn.setSuffix(" 秒")
        spn.setSpecialValueText("不要求持续")
        spn.setValue(float(cond.get("hold_secs", 0) or 0))
        spn.setToolTip("条件要【连续成立】满这么多秒才算成立：\n"
                       "· 图只闪一下没撑满 = 本拍不成立，判断节点走 N、循环继续转；\n"
                       "· 中途断了（图消失/变量变了）= 计时清零重来。\n"
                       "防画面闪烁误判用；不要求持续（0）= 命中即算成立。\n"
                       "⚠ 判断节点里别设得比「判断超时」还长，否则永远撑不满。")
        spn.valueChanged.connect(_set)
        f.addRow("持续时长", spn)
        lay.addLayout(f)
        lay.addWidget(_hint("连续成立满这么多秒才算数（闪一下不算）；不填 = 命中即算。"))
        return w

    def _cond_template(self, node, cond):
        w = QWidget()
        f = QFormLayout(w)
        cmb_cat, _cmb_name = self._add_cat_name_rows(f, cond, repaint_for=node)
        # cond 也带去程票：采完回填进这个分支的判断条件而不是节点本身
        f.addRow(self._goto_capture_btn(cmb_cat, node, cond))
        # 区域跟着条件走（不碰模板账本）：同一张图（如锁图标）在不同条件里
        # 框不同行各找各的；不勾 = 跟随账本默认区域（存量行为不变）。
        f.addRow(self._roi_block(
            f, cond, "roi", "搜索区域",
            "在左侧预览上点两下框住要查的那一片，框完自动回填。\n"
            "典型用法：同一张锁图标在列表第 2 行、第 3 行各框一个条件，"
            "谁框谁的、互不干扰。",
            optional=True,
            chk_text="限定搜索区域（不框 = 跟随模板默认）",
            none_text="未框（= 跟随模板默认区域）",
            owner=node))
        return w

    def _cond_color(self, node, cond):
        w = QWidget()
        f = QFormLayout(w)
        roi = cond.setdefault("roi", [0, 0, 100, 100])
        for i, key in enumerate(("x0", "y0", "x1", "y1")):
            spn = QSpinBox()
            spn.setRange(0, BASE_H - 1 if key.startswith("y") else BASE_W - 1)
            spn.setValue(int(roi[i]))
            spn.valueChanged.connect(
                lambda v, c=cond, idx=i: (c["roi"].__setitem__(idx, v), self._mark_dirty()))
            f.addRow(("区域左", "区域上", "区域右", "区域下")[i], spn)
        f.addRow("峰值阈值", self._spin(cond.get("peak", 200), 0, 255,
                                        cond, "peak"))
        return w

    def _flow_var_names(self) -> list:
        """本流程里所有「识字取物」读数用法会写入的变量名（含循环体内），
        给 var 条件的变量名下拉当备选项 —— 现扫不持久化（同款"引用现场派生"）。"""
        names = []
        def walk(nodes):
            for n in nodes or []:
                if not isinstance(n, dict):
                    continue
                if n.get("type") == "server_pick" \
                        and n.get("mode") in ("number", "position", "roi_number"):
                    # roi_number 没锚点文字，引擎侧留空回落「读数」，这里同款口径
                    fb = "读数" if n.get("mode") == "roi_number" else n.get("text")
                    nm = str(n.get("var_name") or fb or "").strip()
                    if nm and nm not in names:
                        names.append(nm)
                walk(n.get("steps") or [])
        walk(self.graph.get("nodes") or [])
        return names

    def _cond_var(self, node, cond):
        """变量比较（var）：判断「识字取物」读回来的流程变量满不满足条件。
        典型配方：[读体力→变量 体力] → [判断 体力>=10] → Y 刷本 / N 收工。
        数字能化成数就按数比；比不了（变量存的是坐标串等）只认 ==/!=。
        区间比较（between/not_between，F4）：值 + 第二个边界值 = 区间，
        选了区间类才显示出「上界」输入框（其它运算符藏掉）。"""
        w = QWidget()
        f = QFormLayout(w)
        cmb_name = QComboBox()
        cmb_name.setEditable(True)
        cur = str(cond.get("name", ""))
        opts = self._flow_var_names()
        if cur and cur not in opts:
            opts.insert(0, cur)      # 存过的名字先垫最前，不被下拉悄悄改掉
        cmb_name.addItems(opts or [""])
        cmb_name.setCurrentText(cur)
        cmb_name.setToolTip("变量名 = 识字取物「读数字/识文读数字/读坐标」用法里填的变量名；\n"
                            "也可直接手打（引用别的流程读的值时）。\n"
                            "下拉清单是本流程现扫出来的，不落盘。")
        cmb_name.currentTextChanged.connect(
            lambda v, c=cond: (c.__setitem__("name", v.strip()), self._mark_dirty()))
        f.addRow("变量名", cmb_name)
        cmb_op = self._cbo(
            str(cond.get("op", ">=")),
            [("≥ 大于等于", ">="), ("≤ 小于等于", "<="), ("> 大于", ">"),
             ("< 小于", "<"), ("= 等于", "=="), ("≠ 不等于", "!="),
             ("↔ 区间内（下界≤值≤上界）", "between"),
             ("↔ 区间外（不在上下界之间）", "not_between")],
            cond, "op")
        f.addRow("比较", cmb_op)
        edt_val = QLineEdit(str(cond.get("value", "")))
        edt_val.setPlaceholderText("比什么值，如：10")
        edt_val.textChanged.connect(
            lambda v, c=cond: (c.__setitem__("value", v), self._mark_dirty()))
        lbl_val = QLabel("值")
        f.addRow(lbl_val, edt_val)
        # 第二个边界值（区间类的上界）：非区间运算符整行藏掉（不预创建无谓控件）
        edt_val2 = QLineEdit(str(cond.get("value2", "")))
        edt_val2.setPlaceholderText("上界，如：20")
        edt_val2.textChanged.connect(
            lambda v, c=cond: (c.__setitem__("value2", v), self._mark_dirty()))
        lbl_val2 = QLabel("上界")
        f.addRow(lbl_val2, edt_val2)

        def _apply_range(_idx=None, c=cond, l1=lbl_val, l2=lbl_val2,
                         e2=edt_val2):
            is_range = str(c.get("op", ">=")) in ("between", "not_between")
            # 下界标签在区间类下换成「下界」，普通比较仍是「值」
            l1.setText("下界" if is_range else "值")
            _vis(l2, is_range)     # 构建期安全显隐（无父期间只藏不显）
            _vis(e2, is_range)

        _apply_range()
        cmb_op.currentIndexChanged.connect(
            lambda _=False, fn=_apply_range: (fn(), self._mark_dirty()))
        f.addRow(_hint("变量还没读到值时恒判不成立走 N（判不过的日志会点名这个变量"
                       "「没值，先放识字取物读到它」）。两边都是数就按数比。\n"
                       "区间比较 = 下界≤值≤上界（含端点，两端填反了自动归一）；\n"
                       "值或边界转不成数恒判不成立（不做字典序比较）。"))
        return w

    def _cond_text(self, node, cond):
        """文字识别（text）：OCR 判定"画面里的字跟期望一致吗"（典型用法：
        默认选的区服是不是设定的那个）。匹配裁决与选区点击节点同一套
        （core.ocr.match_score 数字锚点：形近字可容错，6服/9服不容错）。
        识别范围二选一：勾「限定识别区域」= 只认框内（快且准），
        不勾 = 全屏扫（roi 存 null，编译侧 None 兜底走全屏）。
        区域两条改法与 server_pick 同款：手填坐标 / 在预览上框选回填。
        """
        w = QWidget()
        f = QFormLayout(w)
        f.addRow(self._roi_block(
            f, cond, "roi", "识别区域",
            "不用手填坐标：在左侧预览上点两下框住要认字的那一片，框完自动回填。\n"
            "⚠ 框要比目标字大一圈：贴着字画太窄的条 OCR 检不出行，框大块区域最稳。",
            optional=True, owner=node,
            chk_tip="限定：OCR 只念框内那片（区域小 = 又快又准，推荐）。\n"
                    "全屏：整幅画面扫字 —— 目标位置不固定时才用，慢且易被杂色 UI 认花。"))
        edt = QLineEdit(str(cond.get("target", "")))
        edt.setPlaceholderText("期望文字，如：含沙射影6服 / {账号.区服}")
        edt.setToolTip("支持占位符：{变量名} 取流程变量、{账号.列名} 取账号表任意列\n"
                       "（如 {账号.区服} {账号.等级}，列名照表格表头写）。\n"
                       "多开各号各服时期望文字写 {账号.区服}，一条流程通用不用手填；\n"
                       "取不到（没绑账号/列名错）原样留在日志里，好排查。")
        edt.textChanged.connect(
            lambda v, c=cond: (c.__setitem__("target", v), self._mark_dirty()))
        f.addRow("期望文字", _acct_token_row(edt))
        spn_sim = self._dspin(cond.get("min_sim", 0.6), 0.1, 1.0, cond,
                              "min_sim", step=0.05)
        f.addRow("最低匹配度", spn_sim)
        f.addRow(self._chk(cond.get("invert", False), cond, "invert",
                           "反判（识别不到该文字才走真分支）"))
        from core import ocr as _ocr
        if not _ocr.available():
            warn = QLabel("⚠ OCR 依赖未安装（pip install rapidocr onnxruntime），"
                          "真机上此判断恒走假分支")
            f.addRow(warn)
        return w

    def _cond_motion(self, node, cond):
        """动态检测（motion）：动画关键帧组 + 采样帧数。等图标"动起来"再走真
        分支 —— 帧组里各帧轮流被认到 = 在动；一直认同一帧 = 静止。
        frames 存帧模板名列表（同一库），与模板下拉保真改名的读侧/写侧共用。
        """
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        f = QFormLayout()
        cmb_cat, _cmb_name = self._add_cat_name_rows(f, cond, repaint_for=node)
        lay.addLayout(f)
        # 帧列表：列出 frames（当前库下），可增删
        lst = QListWidget()
        lay.addWidget(QLabel("动画帧模板（同一库的几张关键帧）："))
        lay.addWidget(lst)
        # ⚠ 无父填项前强制样式生效（同取色点列表坑）：首次 _refresh() 在挂窗前
        #   跑，不 polish 则排位 17px/绘制 29px 行字互压、选帧热区偏半行删错帧；
        #   且切库二次 _refresh 时已 polish 又恢复正常——同一面板时好时坏最难缠。
        lst.ensurePolished()

        def _refresh():
            lst.clear()
            for fn in cond.get("frames", []):
                lst.addItem(fn)

        _refresh()

        def _add_frame():
            nm = _cmb_name.currentText()
            if not nm:
                self._set_status("先在上方选一张模板作为动画帧")
                return
            frames = cond.setdefault("frames", [])
            if nm not in frames:            # 同帧去重（同一帧两张没意义）
                frames.append(nm)
                self._mark_dirty()
            _refresh()

        def _del_frame():
            r = lst.currentRow()
            if 0 <= r < len(cond.get("frames", [])):
                cond["frames"].pop(r)
                self._mark_dirty()
                _refresh()

        row = QHBoxLayout()
        btn_add = _add_btn("添加当前模板为帧")
        btn_del = QPushButton("删除选中帧")
        btn_del.setIcon(icons.glyph_icon(icons.DELETE, 14))
        btn_del.setIconSize(QSize(13, 13))
        btn_del.setToolTip("把列表里当前选中的那一帧移出动画帧组")
        _fit_btn(btn_del)
        btn_add.clicked.connect(_add_frame)
        btn_del.clicked.connect(_del_frame)
        row.addWidget(btn_add)
        row.addWidget(btn_del)
        row.addStretch()
        lay.addLayout(row)

        # 采样帧数：至少 ~1 秒（worker 0.25s/拍）才够跨过动画周期
        f2 = QFormLayout()
        spn_n = self._spin(cond.get("samples", 4), 2, 16, cond, "samples")
        spn_n.setToolTip("连续采样多少帧判一次：0.25s/帧，4 = 约 1 秒")
        f2.addRow("采样帧数", spn_n)
        lay.addLayout(f2)
        # roi（限识别区域，可选，默认全屏）：与文字识别/识字取物同一份 _roi_block
        lay.addWidget(self._roi_block(
            None, cond, "roi", "识别区域",
            "只在这片区里比对动画帧：避开画面别处的动效（跑马灯、数值跳动）"
            "误判成“在动”，区域小还更快。",
            optional=True, owner=node, embed_chk=True))
        lay.addWidget(_hint("动画动起来时才走“是”：先在某状态静止采一帧、动起来再采一张，"
                            "帧号在这几张之间跳 = 在动。缺一帧就整组降级走“否”。"))
        return w

    def _cond_multi(self, node, cond):
        w = QWidget()
        lay = QVBoxLayout(w)
        lst = QListWidget()
        # ⚠ 必须在 addItem 前强制样式生效：全局主题给 QListWidget::item 加了
        # padding（行绘制高 29），未 polish 时填项则按裸字体高（17）排位——
        # 行距 17 画高 29，相邻两行文字互相压着（实测踩坑，ensurePolished 一行收口）。
        lst.ensurePolished()
        points = cond.setdefault("points", [])
        self._refresh_points_list(lst, points)
        lay.addWidget(QLabel("取色点列表："))
        lay.addWidget(lst)
        tol = self._spin(cond.get("tolerance", 8), 0, 255, cond, "tolerance")
        row = QHBoxLayout()
        row.addWidget(QLabel("容差"))
        row.addWidget(tol)
        lay.addLayout(row)

        def del_point():
            sel = lst.currentRow()
            if 0 <= sel < len(points):
                points.pop(sel)
                self._refresh_points_list(lst, points)
                self._sync_multi_marks()
                self._mark_dirty()

        row2 = QHBoxLayout()
        btn_add = QPushButton("在预览上点选记点")
        btn_del = QPushButton("删除选中点")
        btn_add.clicked.connect(lambda: self._begin_multi_pick(node, cond))
        btn_del.clicked.connect(del_point)
        row2.addWidget(btn_add)
        row2.addWidget(btn_del)
        lay.addLayout(row2)
        lay.addWidget(_hint("点按钮后到左侧预览上逐点单击：放大镜里看清颜色再点，"
                            "每点一下记一点（画面自动冻结不变色），右键结束。"))
        return w

    def _refresh_points_list(self, lst, points):
        lst.clear()
        for p in points:
            if len(p) >= 3:
                lst.addItem(f"({p[0]},{p[1]}) #{p[2]}")
            else:
                lst.addItem(str(p))

    # ── 共用预览交互：点选坐标（经外壳 route_* 进来）──
    def _begin_pick(self, node, field, owner=None):
        """node = 值写回的目标 dict（可以是节点本体，也可以是 watch 这类
        行 dict —— 偏移/坐标在行结构和节点里字段同名，一套取点逻辑两用）。
        owner = 行场景的宿主节点：只用于"写完重建它的表单"，本体缺省 = node。"""
        if self._view is None:
            self._set_status("预览未就绪")
            return
        anchor = None
        if field == "offset":
            # 预匹配：画面里找不到模板就根本不进点选态（点了也是白点）；
            # 找到则绿框标出命中，框中心即偏移原点 —— 先看得见原点再点目标位。
            anchor, err = self._offset_anchor(node)
            if anchor is None:
                self._set_status(err)
                return
        self._pick_ctx = {"node": node, "field": field, "stage": 1,
                          "anchor": anchor, "owner": owner}
        # 正常送达路 = 在共用预览登记一次性回调；click_owner="pick" 保留给
        # 不支持回调的旧壳/测试假视图走外壳信号兜底路（swipe 第二段同样重登记）。
        self._view.draw_mode = "click_target"
        self._view.click_owner = "pick"
        setter = getattr(self._view, "set_point_pick_callback", None)
        if setter is not None:
            setter(self._on_pick_click)
        if anchor is not None:
            # 上一轮点选残留的红十字先清掉，免得跟本轮目标位混淆
            self._view.click_target_pos = None
            ax, ay, w, h = anchor
            self._view.hit_rects = [(ax - w // 2, ay - h // 2, w, h)]
            self._view.update()
            what = "文字命中" if node.get("type") == "server_pick" else "模板命中"
            self._set_status(f"绿框={what}（框中心即偏移原点），"
                             "请点一下要点击的目标位置（右键取消）")
            return
        t = "起点" if field == "swipe" else "目标点"
        self._set_status(f"请在预览画面点一下 {t}（右键取消）")

    def _on_pick_click(self, x, y):
        ctx = self._pick_ctx
        if ctx is None:
            return
        node, field = ctx["node"], ctx["field"]
        if field == "click":
            node["tap_x"], node["tap_y"] = x, y
            self._set_status(f"已设点击坐标 ({x},{y})")
            self._end_pick()
        elif field == "offset":
            self._apply_offset_pick(node, x, y, ctx.get("anchor"))
            return
        elif field == "swipe":
            if ctx["stage"] == 1:
                node["x1"], node["y1"] = x, y
                ctx["stage"] = 2
                self._set_status(f"已设滑动起点 ({x},{y})，再点一下终点")
                # ImageView 单击后已自动回稳态，取终点需重新进 click_target；
                # 一次性回调刚被第一段消费掉，第二段重新登记（仍归本面板）
                self._view.draw_mode = "click_target"
                setter = getattr(self._view, "set_point_pick_callback", None)
                if setter is not None:
                    setter(self._on_pick_click)
                return
            node["x2"], node["y2"] = x, y
            self._set_status(f"已设滑动 ({node['x1']},{node['y1']})→({x},{y})")
            self._end_pick()
        self._mark_dirty()
        self._rebuild_props(node)

    def _offset_anchor(self, node):
        """对预览当前帧匹配节点模板，返回锚点 (中心x, 中心y, 模板w, 模板h)；
        失败返回 (None, 提示语)。与运行时 tap_template 同口径（同阈值/roi/找图
        函数），所以这里算出的中心 = 挂机时的中心，偏移量两遍一致。
        识字取物「点文字」例外：锚点改走 OCR 命中框中心（_offset_anchor_ocr，
        与运行时 ServerPickStep 的 find_text 同口径），偏移原点 = 认出的那行字。"""
        if node.get("type") == "server_pick":
            return self._offset_anchor_ocr(node)
        cat, name = node.get("category", ""), node.get("name", "")
        if not name:
            return None, "先在上方选好模板，再点选目标位"
        img = self._view._img if self._view else None
        if img is None:
            return None, "预览未就绪"
        entry = template_manager.find_template(cat, name)
        tpl = template_manager.load_template_image(entry) if entry else None
        if tpl is None:
            return None, f"模板「{name}」图片读不到"
        hit = vision.match_template(img, tpl, entry["threshold"],
                                    roi=entry.get("roi"),
                                    rgb_threshold=entry.get("rgb_threshold"))
        if hit is None:
            return None, f"画面里没匹配到模板「{name}」，请到对应界面再点"
        cx = hit["x"] + tpl.shape[1] // 2
        cy = hit["y"] + tpl.shape[0] // 2
        return (cx, cy, tpl.shape[1], tpl.shape[0]), ""

    def _offset_anchor_ocr(self, node):
        """识字取物「点文字」的偏移原点 = OCR 认出目标文字那行的框中心。
        与运行时 ServerPickStep 同口径（同 text/roi/min_sim/同一 find_text），
        所以这里算出的中心 = 挂机时的落点原点，偏移量两遍一致。
        依赖没装 / 没填文字 / 目标文字是占位符（运行时才展开）→ 各自点名。"""
        from core import ocr
        if not ocr.available():
            return None, "未装 OCR 依赖（pip install rapidocr onnxruntime）"
        text = str(node.get("text", "") or "").strip()
        if not text:
            return None, "先在上方填目标文字（留空=运行时读账号，无法预匹配）"
        if "{" in text:
            return None, "目标文字是占位符（运行时才展开），无法预匹配，请手填偏移"
        img = self._view._img if self._view else None
        if img is None:
            return None, "预览未就绪"
        roi = node.get("roi")
        hit = ocr.find_text(img, text,
                            roi=tuple(roi) if roi else None,
                            min_sim=float(node.get("min_sim", 60)) / 100.0)
        if hit is None:
            return None, f"画面里没认出「{text}」，请到对应界面再点"
        cx, cy = hit["x"], hit["y"]
        w = hit.get("x1", cx) - hit.get("x0", cx)
        h = hit.get("y1", cy) - hit.get("y0", cy)
        return (cx, cy, max(w, 4), max(h, 4)), ""

    def _apply_offset_pick(self, node, x, y, anchor):
        """点选目标位 + 预匹配锚点中心 → 写回 offset_x/offset_y。
        写完即清预览叠加（绿框/红十字），数值反馈在状态条和属性面板，
        图上不留残留挡后续观察。"""
        cx, cy = anchor[0], anchor[1]
        node["offset_x"], node["offset_y"] = int(x - cx), int(y - cy)
        if self._view is not None:
            self._view.click_target_pos = None
            self._view.hit_rects = []
            self._view.update()
        self._set_status(f"偏移已设 (dx={x - cx}, dy={y - cy})，"
                         f"目标 ({x},{y}) − 中心 ({cx},{cy})")
        owner = (self._pick_ctx or {}).get("owner") or node
        self._end_pick()
        self._mark_dirty()
        # 行场景 owner=宿主节点（node 是行 dict）：重建节点表单让偏移回填进 spinbox
        self._rebuild_props(owner)

    def _end_pick(self):
        """一次取坐标流程结束：清上下文 + 预览点选归属/回调（防后续点选继续误路由）。
        正常完成路回调已被 ImageView 消费置空；这里清的是提前收场（如取消）的残票。"""
        self._pick_ctx = None
        if self._view is not None:
            self._view.click_owner = None
            setter = getattr(self._view, "set_point_pick_callback", None)
            if setter is not None:
                setter(None)
