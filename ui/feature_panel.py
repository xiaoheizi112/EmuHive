"""
ui/feature_panel.py — 功能设置（底部菜单栏「功能设置」条目的内容页）

放全局功能参数：模拟器分辨率、点击常驻延时、模拟器多开（含间隔多开）、雷电路径。
（容量压测 2026-10-10 拆到 ui/test_panel.py 的「测试设置」页——本页太长会挤压
上方账号管理区，测试类功能单开一页。）

设计（从旧分辨率对话框简化而来）：
- 去掉逐实例勾选表格——分辨率统一走「全局应用」：改参数 + 一个按钮，
  应用到全部实例（启动批次本来也会按这个值自动统一，见 main_window）
- 值写回 config（emulator.resolution_w/h/dpi），下次启动程序还记得
- 应用动作发 apply_requested 信号给主窗口，转成后台批次执行
  （ldconsole modify 可能慢，不能卡界面线程）
- 功能介绍不占版面：做成紧贴功能名的「?」徽章（见 ui/help_badge.py 的约定）
- 目标值 ≠ 模板基准分辨率（capture.BASE_*）时给醒目警告：
  模板全是在基准空间采集的，改成别的值识别会偏、点击会歪，
  除非重采全部模板。正常没人需要改，警告是防手滑
"""

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core import capture, config_manager, ldplayer
from ui.help_badge import titled_group

_RESOLUTION_HELP = (
    "脚本按 540×960 的画面找按钮，所有模拟器的分辨率必须统一成这个值，"
    "不然点击会点歪。<br><br>"
    "生效时机：<br>"
    "· 已停止的实例：应用后下次启动生效<br>"
    "· 运行中的实例：重启后生效<br><br>"
    "平时不用手动应用——每次「启动」前程序会自动按这里的值统一所有实例。"
)

_POST_DELAY_HELP = (
    "「点模板」节点默认勾了「点击后加延迟」：点完按钮多等这么久再走下一步——"
    "给游戏加载、开屏动画留余量，防止下一刀砍在还没刷出来的界面上。<br><br>"
    "这是全局统一值：一个地方调，所有勾了延迟的节点一起跟着变（不用逐节点改）。"
    "个别不想等的节点，在流程编辑器里取消勾选即可。<br><br>"
    "只管「点模板」节点：并行监视里的点击（命中即点、跑子流程勾的即点）不受这里控制，"
    "认出弹窗点掉就走，不掺和主线节奏。<br><br>"
    "填 0 = 等于不生效。"
)

_FRAME_HELP = (
    "挂机线程「多久看一眼画面」：每拍做一遍 截图→识别/判断→动作。<br><br>"
    "调小 = 反应快（专抓一闪而过的窗口），但每台机器更忙；"
    "调大 = 多开时省 CPU/磁盘，代价是发现目标晚半拍。<br><br>"
    "单次识别质量和它无关——每拍都是完整截图重新找图，只是看的频率。<br><br>"
    "生效时机：<b>改完后新启动的挂机</b>按新节拍；已在挂的线程不换节拍，"
    "想立刻生效就停掉重挂。<br><br>"
    "范围 0.05~2.0 秒，默认 0.5。"
)

_CLONE_HELP = (
    "把模拟器台数<b>直接设置</b>到你填的目标数：少了从<b>母体</b>整盘克隆补齐"
    "（ldconsole copy，新实例由雷电自动命名），多了删除<b>编号最大</b>的多余台"
    "（ldconsole remove）。0 号母体是克隆源，永远不参与删除。<br><br>"
    "克隆是复制整块虚拟机磁盘，<b>很慢</b>（一台几十秒到几分钟，看硬盘），"
    "所以走后台批次执行，日志逐台报进度，界面不卡。<br><br>"
    "典型用法：账号 TXT 里写了序号 1~10 十个号，但模拟器只有 1 台 →"
    "这里填 10 点设置，建够 10 台再启动挂机；以后号减到 5 个，填 5 即收回多余台。<br><br>"
    "目标数量默认帮你填成「当前台数 + 1」，手动改过就一直用你填的值。<br><br>"
    "⚠ <b>删台不可逆</b>：连虚拟机磁盘一起抹，执行前弹确认框点名列出要删的实例；"
    "被删机上绑的账号会自动解绑（账号档案保留，回到未绑定等下次补位）。"
)


_LDCONSOLE_HELP = (
    "程序靠雷电自带的命令工具 <b>ldconsole.exe</b> 来开关模拟器、改分辨率、多开克隆。<br><br>"
    "默认<b>自动探测</b>三个常见安装位置（D:\\LDPlayer、C:\\LDPlayer、"
    "C:\\LDPlayer\\LDPlayer4），雷电装在常规位置就不用管这里。<br><br>"
    "装在了别的地方：点「浏览」选中雷电安装目录里的 ldconsole.exe，或直接把完整路径"
    "粘进输入框（粘目录也行，会自动补齐成该目录的 ldconsole.exe）。存一次永久记住，"
    "<b>不用重启程序</b>——下一条模拟器命令立刻走新路径。<br><br>"
    "输入框<b>留空 = 回到自动探测</b>。"
)


class FeaturePanel(QWidget):
    """功能设置页。apply_requested(实例列表, 宽, 高, dpi)，列表空 = 全部实例；
    clone_requested(目标台数)"""

    apply_requested = Signal(list, int, int, int)
    clone_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        cfg = config_manager.get_config()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)

        # 分组框标题不能嵌控件，所以标题留空、第一行自己拼：
        # 功能名 + 紧贴的「?」徽章（悬停看功能介绍，不占版面）
        box, box_lay = titled_group("模拟器分辨率", _RESOLUTION_HELP, spacing=3)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.spn_w = self._spin(240, 3840, int(cfg.get("emulator.resolution_w", 540)))
        self.spn_h = self._spin(240, 3840, int(cfg.get("emulator.resolution_h", 960)))
        self.spn_dpi = self._spin(80, 640, int(cfg.get("emulator.resolution_dpi", 240)))
        self.spn_dpi.setSingleStep(40)
        row.addWidget(QLabel("宽"))
        row.addWidget(self.spn_w)
        row.addWidget(QLabel("高"))
        row.addWidget(self.spn_h)
        row.addWidget(QLabel("DPI"))
        row.addWidget(self.spn_dpi)
        self.btn_apply = QPushButton("应用到全部实例")
        self.btn_apply.setToolTip(
            "把上面的分辨率写进每一个模拟器实例（ldconsole modify）。\n"
            "已符合的实例自动跳过；运行中的实例需重启才生效（批次日志会提醒）")
        row.addWidget(self.btn_apply)
        row.addStretch(1)
        box_lay.addLayout(row)

        # 偏离基准分辨率的警告（值一变实时刷新）
        self.lbl_warn = QLabel("")
        self.lbl_warn.setObjectName("warnLabel")
        self.lbl_warn.setWordWrap(True)
        self.lbl_warn.setVisible(False)
        box_lay.addWidget(self.lbl_warn)

        lay.addWidget(box)

        # 点击常驻延时：所有勾了「点击后加延迟」的「点模板」节点共用的一个全局值。
        # 存 config（general.post_click_delay），运行时现读，改完立即生效。
        box2, box2_lay = titled_group("点击常驻延时", _POST_DELAY_HELP, spacing=3)
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.spn_delay = QDoubleSpinBox()
        self.spn_delay.setRange(0, 60)
        self.spn_delay.setDecimals(1)
        self.spn_delay.setSingleStep(0.5)
        self.spn_delay.setSuffix(" 秒")
        self.spn_delay.setValue(float(cfg.get("general.post_click_delay", 1.0)))
        self.spn_delay.valueChanged.connect(self._on_delay_changed)
        row2.addWidget(QLabel("点完等一下再走："))
        row2.addWidget(self.spn_delay)
        row2.addStretch(1)
        box2_lay.addLayout(row2)

        # 挂机循环节拍：worker 每拍间隔（存 general.frame_interval，
        # 起跑新挂机时现读；已在挂的线程不换节拍，见 _FRAME_HELP）
        box2b, box2b_lay = titled_group("挂机循环节拍", _FRAME_HELP, spacing=3)
        row2b = QHBoxLayout()
        row2b.setSpacing(6)
        self.spn_frame = QDoubleSpinBox()
        # 上下限与 bot/worker.py 的钳位 (0.05~2.0) 同口径：界面先挡住，
        # 免得填了个越界值被 worker 悄悄回落 0.5，"改了没反应"
        self.spn_frame.setRange(0.05, 2.0)
        self.spn_frame.setDecimals(2)
        self.spn_frame.setSingleStep(0.05)
        self.spn_frame.setSuffix(" 秒")
        self.spn_frame.setValue(float(cfg.get("general.frame_interval", 0.5)))
        self.spn_frame.setToolTip(
            "每台挂机线程「多久看一眼画面」：每拍做一遍 截图→识别→动作。\n"
            "调小反应快但机器忙；调大省资源但发现目标晚半拍。\n"
            "改完对新启动的挂机生效，已在挂的线程不换节拍（停掉重挂才吃新值）。")
        self.spn_frame.valueChanged.connect(self._on_frame_changed)
        row2b.addWidget(QLabel("每拍间隔："))
        row2b.addWidget(self.spn_frame)
        row2b.addStretch(1)
        box2b_lay.addLayout(row2b)
        # 点击常驻延时 / 挂机循环节拍 都是单行卡片，并排占一行（省竖向高度，
        # 给上方账号表格让位）；stretch 放在节拍卡之后，让两张卡靠左等宽。
        row2c = QHBoxLayout()
        row2c.setSpacing(2)
        row2c.addWidget(box2)
        row2c.addWidget(box2b)
        lay.addLayout(row2c)

        # 模拟器多开：台数双向设置——少了克隆补、多了删尾台（后台批次，见主窗口 on_clone_apply）
        box3, box3_lay = titled_group("模拟器多开", _CLONE_HELP, spacing=3)
        row3 = QHBoxLayout()
        row3.setSpacing(6)
        self.spn_count = QSpinBox()
        self.spn_count.setRange(1, 99)
        self.spn_count.setValue(1)
        # 单位"台"放输入框后面独立标签，不做 setSuffix：后缀挤占框内右侧
        # 空间，原生上下按钮会被压没（用户点名 2026-09-21：框里只放数字）
        self.spn_count.setKeyboardTracking(False)  # 敲完才提交，不边敲边跳数
        self._cur_count = None       # 当前台数（主窗口 refresh_count 回填）
        self._clone_touched = False  # 用户手改过目标后不再自动顶默认值
        self.spn_count.valueChanged.connect(self._on_clone_target_edited)
        row3.addWidget(QLabel("目标数量："))
        row3.addWidget(self.spn_count)
        row3.addWidget(QLabel("台"))
        self.lbl_cur = QLabel("")
        row3.addWidget(self.lbl_cur)
        self.btn_clone = QPushButton("设置")
        self.btn_clone.setToolTip(
            "把模拟器台数调整到上面的目标数量：少了克隆补齐，多了删除编号最大的多余台。\n"
            "删除不可逆，会先弹确认框；台数已相等时按钮显示「无需调整」。")
        row3.addWidget(self.btn_clone)
        row3.addStretch(1)
        box3_lay.addLayout(row3)

        # 间隔多开：「全部启动」逐台错开下达 launch（存 emulator.launch_interval_sec，
        # 启动时现读，改完下次「全部启动」即生效，不用应用按钮）
        row3b = QHBoxLayout()
        row3b.setSpacing(6)
        self.spn_launch_gap = QDoubleSpinBox()
        # 下限 0.3 秒（2026-09-25 用户拍板）：多开机是 SSD，1 秒/台纯排队
        # 太亏；原下限 1 秒是 2026-09-21 为机械盘开机风暴定的。0 = 全连发
        # 仍是手改 config 的后门（界面照旧不给填 0）。
        self.spn_launch_gap.setRange(0.3, 60)
        self.spn_launch_gap.setDecimals(1)
        self.spn_launch_gap.setSingleStep(0.1)
        self.spn_launch_gap.setSuffix(" 秒")
        self.spn_launch_gap.setValue(float(cfg.get("emulator.launch_interval_sec", 4)))
        self.spn_launch_gap.setToolTip(
            "「全部启动」时每台错开这么久再下达下一台（最低 0.3 秒）。\n"
            "机器扛得住就调小；几十台机械盘机器一起开机 CPU/磁盘挤爆反而都慢，\n"
            "且瞬时并发易触发雷电/游戏侧风控。\n"
            "注意：错开的只是「下达启动」的节拍，各机开机本身仍是并行的；\n"
            "填 2 秒开 20 台 ≈ 整批多花 40 秒下达完。")
        self.spn_launch_gap.valueChanged.connect(self._on_launch_gap_changed)
        row3b.addWidget(QLabel("间隔多开：每台错开"))
        row3b.addWidget(self.spn_launch_gap)
        row3b.addWidget(QLabel("下达启动"))
        row3b.addStretch(1)
        box3_lay.addLayout(row3b)

        lay.addWidget(box3)
        self.btn_clone.clicked.connect(self._on_clone)

        lay.addWidget(self._build_ldconsole_box())

        lay.addStretch(1)   # 内容靠上，剩余留白

        for spn in (self.spn_w, self.spn_h, self.spn_dpi):
            spn.valueChanged.connect(self._refresh_warn)
        self.btn_apply.clicked.connect(self._on_apply)
        self._refresh_warn()

    @staticmethod
    def _spin(lo: int, hi: int, val: int) -> QSpinBox:
        spn = QSpinBox()
        spn.setRange(lo, hi)
        spn.setValue(val)
        return spn

    def _refresh_warn(self):
        """目标值不等于模板基准分辨率时亮警告（模板要重采才能用别的值）"""
        w, h = self.spn_w.value(), self.spn_h.value()
        if (w, h) != (capture.BASE_WIDTH, capture.BASE_HEIGHT):
            self.lbl_warn.setText(
                f"⚠ 与模板基准分辨率 {capture.BASE_WIDTH}×{capture.BASE_HEIGHT} 不一致："
                f"现有模板全是在基准画面里采集的，改成别的值会识别不到、点击点歪，"
                f"除非重采全部模板。确定要改再应用。")
            self.lbl_warn.setVisible(True)
        else:
            self.lbl_warn.setVisible(False)

    def _on_delay_changed(self, v: float):
        """常驻延时即改即存（config 防抖落盘）；运行时现读，挂机中改也立即生效。"""
        config_manager.get_config().set("general.post_click_delay", float(v))

    def _on_launch_gap_changed(self, v: float):
        """启动间隔即改即存；「全部启动」每次现读，改完下次批量启动即生效。"""
        config_manager.get_config().set("emulator.launch_interval_sec", float(v))

    def _on_frame_changed(self, v: float):
        """循环节拍即改即存；worker 起跑时现读，改完对下次新启动的挂机生效
        （已在挂的线程不热改，避免半路换节拍打乱等待计时）。"""
        config_manager.get_config().set("general.frame_interval", float(v))

    def _on_apply(self):
        """写回配置（记住这次的值）+ 发信号让主窗口跑应用批次"""
        w, h, dpi = self.spn_w.value(), self.spn_h.value(), self.spn_dpi.value()
        cfg = config_manager.get_config()
        cfg.set("emulator.resolution_w", w)
        cfg.set("emulator.resolution_h", h)
        cfg.set("emulator.resolution_dpi", dpi)
        cfg.save()   # 立即写盘：这是"应用"动作，不该等 1 秒防抖
        self.apply_requested.emit([], w, h, dpi)   # 空列表 = 全部实例（主窗口约定）

    def refresh_count(self, current: int):
        """主窗口每轮刷新回填当前台数。用户没手改过目标时默认给「当前 +1」——
        开页停在一个 ≤ 当前台数的值上，点了必然"没反应"，这就是无效感的来源"""
        self._cur_count = current
        self.lbl_cur.setText(f"当前 {current} 台")
        # hasFocus 判据不能省（用户报"改了数字又跳回去"的根因）：
        # keyboardTracking=False 下敲键盘只改显示文本，valueChanged 要等
        # 回车/移出焦点才发——半截输入期间 touched 还是 False，
        # 刷新定时器一 setValue 就把敲到一半的数字顶回默认值。
        # 聚焦 = 正在输入或准备输入，这期间只回填「当前 X 台」不动输入框；
        # 失焦后 valueChanged 自然置 touched，以后也不再覆盖。
        if not self._clone_touched and not self.spn_count.hasFocus():
            # blockSignals：自动填默认值不算"用户手改"，否则下一轮就永远不更新了
            self.spn_count.blockSignals(True)
            self.spn_count.setValue(min(self.spn_count.maximum(), current + 1))
            self.spn_count.blockSignals(False)
        self._refresh_clone_btn()

    def _on_clone_target_edited(self, _v: int):
        """只有用户真改了数字才走到这（自动填时信号被 block）：记住，别再覆盖"""
        self._clone_touched = True
        self._refresh_clone_btn()

    def _refresh_clone_btn(self):
        """目标 = 当前台数时按钮禁用并写明「无需调整」：双向设置下，
        多了是删台（也有活干），只有相等才是真没动静"""
        if self._cur_count is None:
            return
        need = self.spn_count.value() != self._cur_count
        self.btn_clone.setEnabled(need)
        self.btn_clone.setText("设置" if need else "无需调整")

    def _on_clone(self):
        self.clone_requested.emit(self.spn_count.value())

    # ── 雷电路径：手动指认 ldconsole.exe（浏览/粘贴），留空=自动探测 ──
    # 生效链已备好：core/ldplayer._console_path() 每次调用现查配置，改完不重启。

    def _build_ldconsole_box(self) -> QGroupBox:
        box, bl = titled_group("雷电路径", _LDCONSOLE_HELP, spacing=3)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.ed_ldc = QLineEdit(str(config_manager.get_config().get(
            "general.ld_console", "")))
        self.ed_ldc.setPlaceholderText("留空 = 自动探测常见安装位置（D:\\LDPlayer 等）")
        self.ed_ldc.setClearButtonEnabled(True)
        self.ed_ldc.setToolTip("ldconsole.exe 完整路径；粘贴安装目录也会自动补齐")
        self.ed_ldc.editingFinished.connect(self._on_ldc_edited)
        row.addWidget(self.ed_ldc, 1)
        b_pick = QPushButton("浏览…")
        b_pick.setToolTip("选中雷电安装目录里的 ldconsole.exe")
        b_pick.clicked.connect(self._on_ldc_browse)
        row.addWidget(b_pick)
        b_auto = QPushButton("自动探测")
        b_auto.setToolTip("清空手动路径，回到自动探测三个常见安装位置")
        b_auto.clicked.connect(self._on_ldc_auto)
        row.addWidget(b_auto)
        bl.addLayout(row)
        self.lbl_ldc = QLabel("")
        self.lbl_ldc.setWordWrap(True)
        bl.addWidget(self.lbl_ldc)
        self._refresh_ldc()
        return box

    @staticmethod
    def _ldc_norm(text: str) -> str:
        """去引号去空白；粘的是目录就补成该目录的 ldconsole.exe。"""
        p = text.strip().strip('"')
        if p and os.path.isdir(p):
            p = os.path.join(p, "ldconsole.exe")
        return p

    def _ldc_save(self, path: str):
        cfg = config_manager.get_config()
        cfg.set("general.ld_console", path)
        cfg.save()   # 指认路径是一次"应用"动作，不等 1 秒防抖
        self._refresh_ldc()

    def _on_ldc_edited(self):
        norm = self._ldc_norm(self.ed_ldc.text())
        if norm != self.ed_ldc.text():
            self.ed_ldc.setText(norm)   # 目录补齐/去引号回填可见
        self._ldc_save(norm)

    def _on_ldc_browse(self):
        start = os.path.dirname(self._ldc_norm(self.ed_ldc.text())) or \
            os.path.dirname(ldplayer._console_path())
        p, _ = QFileDialog.getOpenFileName(
            self, "选择雷电安装目录里的 ldconsole.exe", start,
            "ldconsole.exe (ldconsole.exe)")
        if p:
            self.ed_ldc.setText(p)
            self._ldc_save(p)

    def _on_ldc_auto(self):
        self.ed_ldc.clear()
        self._ldc_save("")

    def _refresh_ldc(self):
        """展示当前真正生效的路径（配置优先，留空走探测）+ 找到没找到。
        走 _console_path() 现查：LD_CONSOLE 只是镜像，改完配置不重算就显示旧值。"""
        eff = ldplayer._console_path()
        manual = self._ldc_norm(self.ed_ldc.text())
        if os.path.isfile(eff or ""):
            # 手动值失效时 core 会回落探测（_resolve_console 的既有语义）：
            # 只有生效值确实等于手填值才标"手动指定"，别把探到的路径冒认。
            src = "手动指定" if manual and eff == manual else "自动探测"
            self.lbl_ldc.setText(f"✅ 正在使用（{src}）：{eff}")
        else:
            self.lbl_ldc.setText(
                "⚠ 没找到 ldconsole.exe——用「浏览」指认雷电安装目录里的它，"
                "或确认雷电确实装在常见位置。")
