"""
bot/frame_loop.py — 单拍共享执行核（正式挂机与流程测试合用一条拍循环）

把 worker._run_single_task 与 flow_test._run 各抄一份的拍循环收成一个核：
停检查 → 暂停等待 → 截帧 → tick → 拍级异常兜底 → 结果处置 → 拍间隔等待。
差异全部走回调注入（capture_fn/tick_fn/on_stop/log/on_result/...），核里
不含任何"挂机/测试"专属语义——专属语义（状态上报、DB 写字段、话术）留调用方。

收核的动机（审计）：两条循环此前已漂移——flow_test 没有拍级异常兜底（一条
杂散异常=整条测试线程崩）、拍间隔配置没校验钳位（坏值=拍失控）、重校准没有
冷却。兜底进核后这些坑一次填平，以后两边同步长。

⚠ 异常通道铁律（本次最高优先级修复）：FlowTimeoutExit / AccountBannedExit 是
用户预期终止通道（「超时退出程序」「封号标记」节点靠它们直达 run() 的专属处理），
在核里必须原样 re-raise，绝不当拍级瞬时错误吞掉——旧版通用 except 吞了它们，
两类节点在正式挂机里双双失效。
"""

import time
import traceback

from bot.fsm import AccountBannedExit, FlowTimeoutExit


def _exc_where(e):
    """炸点定位尾注：最深一层 traceback 的 文件:行号 函数名。
    1607 移植机排障实测：只有 str(e) 的「'NoneType' has no attribute 'shape'」
    全项目几十处候选，远程机器无法复现，日志必须自带案发现场。"""
    tb = getattr(e, "__traceback__", None)
    if tb is None:
        return ""
    fr = traceback.extract_tb(tb)[-1]
    return f"[{fr.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{fr.lineno} {fr.name}] "

EXC_BACKOFF = 0.5        # 拍级异常退避（秒，可中断）
EXC_FAILS_MAX = 30       # 连续拍级异常判失败阈值（≈15s 起步，adb 真死才走到这）
EXC_LOG_EVERY = 5        # 重试提醒节流：每 5 次连犯喊一嗓子
IMG_FAIL_LOG_EVERY = 10  # 截图失败提醒约 5 秒一嗓子，不刷屏


def clamp_frame_interval(value, default=0.5):
    """拍间隔配置校验钳位：非数字或越界（<0.05 空转烧 CPU / >2.0 形同卡死）回落默认。"""
    try:
        iv = float(value)
    except (TypeError, ValueError):
        return default
    return iv if 0.05 <= iv <= 2.0 else default


def interruptible_wait(secs, should_stop):
    """有界小步 sleep（≤0.2s/步）：睡满返回 False；等待中收到停止立刻返回 True。
    worker 循环绝不阻塞不变量的执行体——所有等待都从这里过。"""
    deadline = time.monotonic() + max(0.0, secs)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if should_stop():
            return True
        time.sleep(min(0.2, remaining))


def run_frame_loop(capture_fn, tick_fn, on_stop, *,
                   frame_interval=0.5,
                   log=None,
                   is_paused=None, on_pause=None, on_resume=None,
                   instance_alive=None,
                   on_result=None,
                   on_exc_failed=None,
                   wait_fn=None,
                   msg_emu_gone="检测到模拟器已关闭，挂机自动停止",
                   msg_grab_fail="截图失败（窗口最小化/被遮挡？adb 模式下=模拟器暂时无响应？），等待恢复"):
    """跑拍循环直到出终态，返回 "done"/"failed"/"stopped"。

    回调约定（全部可选，缺省 = 该层无此行为）：
      capture_fn()   → 本帧图像（None = 截图失败，走容错等待）
      tick_fn()      → FSM.tick 结果字符串；异常即拍级异常（FlowTimeoutExit/
                       AccountBannedExit 例外：原样穿透给调用方 run()）
      on_stop()      → 停止请求（软停+硬停合并口径）
      log(level,msg) → 界面日志出口
      is_paused()    → 暂停标志；真则调 on_pause() 一次、有界睡到解除再 on_resume()
      instance_alive() → 截图失败时核实模拟器是否还在，False 判 "stopped"；
                         不传则失败一律按"等恢复"处理
      on_result(res) → 拍结果处置，返回终态字符串则该拍结束后循环退出，返回 None 继续
      on_exc_failed(e, n) → 连犯达阈值判失败前回调（调用方记失败原因进 DB）
      wait_fn(secs) → 有界等待注入点（默认 = interruptible_wait(secs, on_stop)；
                      调用方自带停止口径时传入，冒烟免真睡也走这里）
    """
    def _log(level, msg):
        if log is not None:
            log(level, msg)

    _wait = wait_fn if wait_fn is not None else (lambda s: interruptible_wait(s, on_stop))

    fail_ticks = 0
    exc_fails = 0
    while not on_stop():
        if is_paused is not None and is_paused():
            if on_pause is not None:
                on_pause()
            while is_paused() and not on_stop():
                time.sleep(0.2)
            if on_stop():
                return "stopped"
            if on_resume is not None:
                on_resume()
        try:
            img = capture_fn()
            res = None if img is None else tick_fn()
        except (FlowTimeoutExit, AccountBannedExit):
            raise  # 用户预期终止通道：穿透给 run() 的专属 handler，不许当瞬时错误吞
        except Exception as e:
            exc_fails += 1
            if exc_fails % EXC_LOG_EVERY == 1:
                _log("WARN", f"拍级异常（连续 {exc_fails} 次）：{_exc_where(e)}{e}，自动重试中")
            if exc_fails >= EXC_FAILS_MAX:
                if on_exc_failed is not None:
                    on_exc_failed(e, exc_fails)
                _log("ERROR", f"拍级连续异常 {exc_fails} 次，挂机停止（最后错误：{_exc_where(e)}{e}）")
                return "failed"
            if _wait(EXC_BACKOFF):
                return "stopped"
            continue
        exc_fails = 0
        if img is None:
            if instance_alive is not None and not instance_alive():
                _log("WARN", msg_emu_gone)
                return "stopped"
            fail_ticks += 1
            if fail_ticks % IMG_FAIL_LOG_EVERY == 1:
                _log("WARN", msg_grab_fail)
            time.sleep(0.2)
            continue
        fail_ticks = 0
        if on_result is not None:
            term = on_result(res)
            if term:
                return term
        elif res == "failed":
            return "failed"
        elif res == "done":
            return "done"
        elif res in ("abort", "flow_end", "loop_continue"):
            return "done"
        if interruptible_wait(frame_interval, on_stop):
            return "stopped"
    return "stopped"
