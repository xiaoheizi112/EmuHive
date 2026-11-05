"""
main.py — 程序入口

运行：python main.py

DPI 感知说明：这里不再手动设置进程 DPI（之前那样会与 Qt6 的
SetProcessDpiAwarenessContext 冲突，报"拒绝访问"警告）。
Qt 会自动把进程设为"每显示器 V2 感知"，截图模块里另有线程级设置兜底。
"""
import sys
from pathlib import Path

# ⚠ onnxruntime 必须抢在 PySide6 之前加载（2026-10-07 降 Qt 6.4.3 打 1607 兼容包实测）：
# PySide6 导入时会把自家目录塞进 DLL 搜索路径（os.add_dll_directory），之后
# onnxruntime 的原生扩展再加载就会撞上不兼容的运行时直接段错误（闪退连报错都没有）。
# 反过来先请 onnxruntime 进门，两者相安无事。OCR 是可选依赖：没装就安静跳过，
# 识字节点照旧降级（core.ocr.available() 返回 False），绝不拦启动。
try:
    import onnxruntime  # noqa: F401
except Exception:
    pass


def _boot_fail_box(err: str) -> None:
    """Qt 都 import 不起来的机器看不到任何界面——用 Win32 原生弹窗说人话：
    带上系统版本和排查清单（打包分发实测：别的电脑报
    "DLL load failed while importing QtCore: 找不到指定的模块"，
    PyInstaller 的英文弹窗既看不出系统环境也指不出路）。"""
    msg = (
        "界面组件（Qt）加载失败，程序无法启动。\n\n"
        f"{err}\n\n"
        "请按顺序排查：\n"
        "1) 本软件需要 Windows 10 或 11 的 64 位系统，Win7/8 装不上界面组件；\n"
        "2) 杀毒软件（360/电脑管家/Windows 安全中心）把程序文件隔离了——"
        "去「隔离区/恢复区」找回并添加信任，再重新解压一份；\n"
        "3) 拷贝不完整：整个 EmuHive 文件夹应约 366 MB，"
        "少了 _internal 里的文件就会报这个错。"
    )
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "EmuHive — 启动失败", 0x10)
    except Exception:
        print(msg)


try:
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
    from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
    from PySide6.QtWidgets import QApplication
except ImportError as e:
    import platform
    _boot_fail_box(f"系统：{platform.system()} {platform.release()}"
                   f"（build {platform.version()}）{platform.machine()}\n"
                   f"错误：{e}")
    sys.exit(3)

from core import database
from ui import theme
from ui.main_window import MainWindow
from ui.no_wheel_edit import install as install_no_wheel_edit
from ui.raise_on_restore import install as install_raise_on_restore


def _install_zh_translator(app: QApplication) -> None:
    """装 Qt 自带的简体中文翻译：QMessageBox 的 Yes/No、QFileDialog 的
    Open/Cancel、QInputDialog 的 OK 等标准按钮默认跟着系统 locale 渲染英文，
    违反本项目「可见名称必须中文」的约定（逐处 setText 要补十几刀，这里一次
    收口）。翻译只作用于 Qt 标准控件文案，不影响我们自己的中文 setWindowTitle/
    setText；加载失败（如发行版裁掉了 .qm）就安静跳过，界面退回英文但不崩。
    ⚠ 译器对象必须保活：installTranslator 不接管所有权，tr 随函数返回被 GC
    的话翻译当场静默失效——挂到 app 上做子对象，寿命跟随 QApplication。"""
    tr = QTranslator(app)
    if tr.load(QLocale("zh_CN"), "qtbase", "_",
               QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(tr)


def _apply_app_icon(app: QApplication) -> None:
    """全程序默认图标：读 assets/app_icon.png（专用图标资产，从模板库复制
    出来后独立维护——增删模板、改模板名都不影响程序图标）。
    setWindowIcon 挂在 app 上 = 主窗口/任务栏/Alt+Tab 及以后新开的窗全跟着变；
    工作台/日志/状态三窗自己的「台/日/态」字标是 setWindowIcon(窗口级) 盖在
    上面的，不受影响。读不到就安静保持 Qt 默认图标，绝不拦启动。"""
    try:
        from core import capture
        from ui.image_util import numpy_to_qimage
        # 打包后 __file__ 在 _internal 里，图标随 datas 落在 sys._MEIPASS/assets；
        # 源码运行落项目根 assets。两个候选都试，取第一个读得出的。
        roots = []
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            roots.append(Path(sys._MEIPASS) / "assets/app_icon.png")
        roots.append(Path(__file__).resolve().parent / "assets/app_icon.png")
        for png in roots:
            img = capture.load_image_cn(str(png))
            if img is not None:
                app.setWindowIcon(QIcon(QPixmap.fromImage(numpy_to_qimage(img))))
                break
    except Exception:
        pass


def _set_taskbar_app_id() -> None:
    """任务栏图标不跟着换的病根：Windows 按 AppUserModelID 给任务栏按钮分组，
    python.exe 起的 GUI 和控制台黑窗同组，整组显示 python.exe 的图标——
    app.setWindowIcon 根本递不到任务栏脸上。给本进程声明一个专属 ID，
    GUI 从此单飞一组、用自己图标（控制台黑窗仍归 python，那个治不了，
    除非换 pythonw 无控制台启动）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "EmuHive.IdleTool")
    except Exception:
        pass


def _display_env() -> str:
    """启动环境记录：电脑屏幕物理分辨率 + Windows 显示缩放比例。
    Qt 报的是逻辑像素（已被缩放除过），物理 = 逻辑 × devicePixelRatio。
    ⚠ 识别/点击坐标与此无关（全走模拟器安卓 540×960 基准空间 + adb tap），
    这行纯粹是换机器/远程排查时的环境案底，别指望它影响判定。"""
    scr = QGuiApplication.primaryScreen()
    if scr is None:
        return "显示环境：取不到屏幕（无头/远程会话？）"
    geo, dpr = scr.geometry(), scr.devicePixelRatio()
    return (f"显示环境：物理 {round(geo.width() * dpr)}×{round(geo.height() * dpr)}"
            f" · 缩放 {round(dpr * 100)}% · 逻辑 {geo.width()}×{geo.height()}")


def main():
    # GUI 崩溃留案底（稳定性审计 P1-⑫）：主线程未捕获异常默认只闪 stderr
    # （双击 run.bat 看不到），这里兜进 logs/run_*.log——"界面卡死过"从此可查。
    # 只加日志不改传播：写完照常走默认处理（弹控制台/退出），行为零变化。
    def _hook(exc_type, exc_value, exc_tb):
        try:
            import traceback
            from core import run_log
            run_log.log("gui", "ERROR", "GUI 未捕获异常：\n" +
                        "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _hook
    # 建表（幂等）：挂机 worker 写库的前提（线 2-8）
    database.init_db()
    # QApplication：整个界面的"大脑"，一个程序只能有一个
    # 任务栏分组身份要先声明（窗口建了再设就晚了，见 _set_taskbar_app_id）
    _set_taskbar_app_id()
    app = QApplication(sys.argv)
    # 全程序默认图标 = assets/app_icon.png（应用图标，独立于模板库）
    _apply_app_icon(app)
    # 标准按钮中文化（须在建窗口之前装好，见 _install_zh_translator）
    _install_zh_translator(app)
    # 全局主题（字体 + 样式表，所有窗口统一）
    theme.apply(app)
    # 所有顶层窗口从任务栏还原时自动置顶（一次安装，覆盖现在和以后的窗口）
    install_raise_on_restore(app)
    # 数值/选择/滑块控件不吃滚轮：滚轮专心滚页面（同上一行，装一次全局生效）
    install_no_wheel_edit(app)
    win = MainWindow()
    win.show()
    # 启动环境案底：分辨率/缩放写进运行日志 + 界面日志面板（见 _display_env）
    env = _display_env()
    from core import run_log
    run_log.log("gui", "INFO", env)
    lp = getattr(win, "log_panel", None)
    if lp is not None:
        lp.log(env, "INFO")
    # app.exec()：进入事件循环。程序从这行开始"活"了——
    # 之后的一切（点按钮、关窗口）都靠事件驱动，直到窗口关闭返回退出码
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
