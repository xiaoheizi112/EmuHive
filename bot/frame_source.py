"""
bot/frame_source.py — 取帧+校准统一层（A2 三份收口）

此前 bot/worker.py 与 bot/flow_test.py 各抄一份几乎逐字的 _grab_frame
（实例快照判活+活句柄兜底+句柄翻转重校准+5 秒冷却+adb 一次性 WARN+
漂移复核），启动校准（launch_calibrate_timeout 解析+clamp）也各一份；
tools/flow_run.py 是第三份手搓且已漂移（快照抽风但句柄活时 i2=None 崩、
裸 float 间隔、启动校准吃 calibrate 全量默认预算可堵近 3 分钟、
换句柄后无冷却重校准）。收成一个类三份共用，行为逐字守恒：

  grab()              本帧图像（None = 截图失败/实例真关，调用方自处置）
  ensure_alive()      实例是否还在（list2 抽风凭活句柄兜底，见 core.ldplayer）
  startup_calibrate() 启动校准，写 ctx.calib 并返回（有界预算读配置）

⚠ 不 import Qt（worker/flow_test 线程侧、tools/flow_run 无头侧都可用）；
日志出口 log_cb(lvl, msg) 注入：worker 走 action_log.emit、flow_test 同形状、
flow_run 走 print。quick_calib_log=True 只给正式挂机：它历史上就在循环内
重校准前后播报（"窗口句柄变化，快速重校准……"+结果行），测试/无头侧历来
静默——照此分流，三方日志文案零变化。
"""

import time

from core import capture, ldplayer
from core.config_manager import get_config

QUICK_CALIB_COOLDOWN = 5.0   # 换句柄后重校准冷却（秒）
QUICK_ADB_TIMEOUT = 8        # 循环内快重校准的单条 adb 预算（秒）


class FrameSource:
    """一个实例一帧一条命：快照判活 → 跟句柄 → 截帧 → 裁基准，全内化。"""

    def __init__(self, ld_index: int, ctx, log_cb, quick_calib_log: bool = False):
        self.ld_index = ld_index
        self.ctx = ctx
        self.log_cb = log_cb                 # (lvl, msg) → 信号/print，由调用方注入
        self._quick_calib_log = quick_calib_log
        self._last_quick_calib = 0.0   # 上次循环内重校准时刻（冷却用，见 grab）
        self._fetch_started_logged = False   # 「取帧已启动」一次性播报（见 grab）

    def ensure_alive(self) -> bool:
        """本实例模拟器是否还在跑（截图失败时区分 永久关闭/暂时遮挡）。
        走 ldplayer.is_instance_alive：快照说没了先拿活句柄兜底，list2 抽风
        不再误报「模拟器已关闭」把好好的挂机掐掉（1607 移植机实测）。"""
        return ldplayer.is_instance_alive(self.ld_index, self.ctx.hwnd)

    def startup_calibrate(self, timeout_cfg_key="general.launch_calibrate_timeout",
                          default=5.0):
        """启动全量校准，结果写 ctx.calib 并返回（None = 整窗缩放回退）。

        量"安卓画面在窗口里的偏移"：几何路径（EnumChildWindows 量 RenderWindow）
        本来就零成本；图像兜底路径在 adb 挂起的坏情况下每轮可堵 2×30 秒 +
        sleep(1)，历史默认 3 轮最坏能在启动阶段堵近 3 分钟（性能审计：雷电几何
        路径几乎必中，图像兜底只是保险，不值这个最坏价）。收紧成 1 轮 +
        单条 adb 预算=配置 timeout_cfg_key（默认 5 秒）近似封顶——⚠ capture
        的 calibrate 尚未暴露 stop_check/总超时单旋钮（core 队接口需求已列
        报告），停止请求由调用侧在校准返回后的逐轮检查处收口，不堵死退出。
        播报留在调用方（worker/flow_test 文案一致、flow_run 自有话术）。
        """
        t = get_config().get(timeout_cfg_key, default)
        try:
            t = float(t)
        except (TypeError, ValueError):
            t = default
        self.ctx.calib = capture.calibrate(
            self.ctx.hwnd, self.ld_index, attempts=1,
            adb_timeout=max(1, int(t if t > 0 else default)))
        return self.ctx.calib

    def _quick_recalibrate(self):
        """循环内重校准（模拟器重启换句柄）：有界快路径 1 轮 × 8 秒。
        冷却由 grab 调用侧把（见 QUICK_CALIB_COOLDOWN），冷却期沿用
        update_hwnd 清空的 calib → 走整窗缩放回退，不阻塞。
        quick_calib_log 分流见模块 docstring（挂机播报/测试静默，文案零变化）。"""
        if self._quick_calib_log:
            self.log_cb("INFO", "窗口句柄变化，快速重校准……")
        self.ctx.calib = capture.calibrate(self.ctx.hwnd, self.ld_index,
                                           attempts=1, adb_timeout=QUICK_ADB_TIMEOUT)
        if self._quick_calib_log:
            if self.ctx.calib:
                self.log_cb("INFO",
                            f"校准完成：偏移 ({self.ctx.calib['offset_x']},{self.ctx.calib['offset_y']})")
            else:
                self.log_cb("WARN", "校准失败，使用整窗缩放回退模式")

    def grab(self):
        """截图并裁到基准分辨率；实例句柄变了就重新校准（模拟器重启的情况）"""
        inst = ldplayer.get_instance(self.ld_index)
        if inst is None or not inst.running:
            # 快照说没跑：别急着判关——窗口句柄还活着就是 list2 抽风，继续用
            # 现有 ctx.hwnd 走截图/adb 兜底（1607 移植机实测：抽风期误报
            # 「模拟器已关闭」掐死正在跑的链）。两路皆死才本拍无帧。
            if not self.ensure_alive():
                return None
        if inst is not None and inst.top_hwnd != self.ctx.hwnd:
            # 句柄本身必须立刻跟（否则下一句截的是死句柄）；但重校准要有
            # 冷却：模拟器启动早期句柄会反复翻转（渲染设备重建），且那正是
            # 几何/adb 双失败的高发窗口——quick 路径几何失败会落到 adb 图像
            # 兜底（最坏 8s），不设冷却每拍都能再触发一次，连环起来照样
            # 违反"worker 循环绝不阻塞"。
            self.ctx.update_hwnd(inst.top_hwnd)
            now = time.monotonic()
            if now - self._last_quick_calib >= QUICK_CALIB_COOLDOWN:
                self._last_quick_calib = now
                self._quick_recalibrate()
        img, from_adb = capture.grab_with_fallback(self.ctx.hwnd, self.ld_index)
        if from_adb and img is not None:
            # 纯安卓基准帧（adb 主路，2026-10-11 起唯一取帧路，见 core/capture.py
            # 「adb 取帧」段头），跳过 calib 裁剪。scrcpy 后端已整体移除，不再有
            # 就绪/未就绪播报分流——出帧即成功，不出帧由拍循环按空帧兜底。
            self._log_fetch_started()
            self.ctx.frame = img
            return img
        if img is None:
            return None
        # 工具栏展开/收起、拖窗口都会让安卓区搬家——每拍复核几何，裁歪自愈
        self.ctx.calib = capture.calib_drift_check(self.ctx.calib, self.ctx.hwnd)
        self.ctx.frame = capture.crop_android(img, self.ctx.calib) if self.ctx.calib \
            else capture.to_base(img)
        self._log_fetch_started()
        return self.ctx.frame

    def _log_fetch_started(self):
        """取帧主路可用的一次性播报：第一帧真取回来了才报「取帧已启动」。
        取代原常驻取帧指示灯——界面不再显示取帧状态，改在运行日志打一行：
        取帧是每拍热路径，只有真出帧才算启动成功（启动宣称不算），一次性播报
        后同实例内不再重复（重连/换句柄等异常由既有 WARN 通道负责）。"""
        if self._fetch_started_logged:
            return
        self._fetch_started_logged = True
        self.log_cb("INFO", "取帧已启动（adb 单次快照，出帧即成功）")
