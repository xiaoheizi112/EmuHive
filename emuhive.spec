# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配方（分发审计 P1-⑪）：双击 build.bat 使用，不必手碰这里。
#
# 决策（都写死在这，换人打包不用重新踩）：
# - onedir 而非 onefile：Qt+onnx 全家桶 onefile 每次启动解压 ~10s，且杀软易误报；
#   onedir 开袋即跑，分发= 拷整个文件夹。
# - 不打 configs/ templates/ data/：用户资产绝不进包（打包机上采的模板不该
#   随软件发出去；用户自己的配置也不该被升级覆盖）。exe 旁没有 configs 时
#   ConfigManager 自动生成默认值——首启即干净状态。
# - 路线A（2026-10-07）：目标机 Win10 1607 只支持 Qt≤6.4，本 spec 须用
#   Python 3.11 + PySide6 6.4.3 的 _build311 虚拟环境执行，依赖钉版见
#   requirements-build311.txt（onnxruntime 必须 ≤1.20.1，否则与 6.4.3 段错误）。
# - rapidocr/onnxruntime 条件收集：装了才收（识字节点可用）；没装收不进去，
#   运行时 available()=False 节点自动降级，与源码运行同一套可选依赖纪律。

from PyInstaller.utils.hooks import (
    collect_all, collect_data_files, collect_dynamic_libs)

datas, binaries, hiddenimports = [], [], []
# 程序图标：main.py _apply_app_icon 读 assets/app_icon.png（sys._MEIPASS 兜底候选）。
# 漏收这一步 → 打包版任务栏/窗口全变 Qt 默认图标（分发实测：源码跑没事、exe 没图标）。
datas.append(("assets/app_icon.png", "assets"))
# 2026-10-11：scrcpy 取帧后端已整体移除（挂机取帧改走 adb 单次快照，与 Windows
# 版本无关、无常驻进程）。故不再随包分发 scrcpy-server.jar 与 ffmpeg.exe——
# 少装 ~80MB。AI 流水线的视频抽帧工具（tools/vid_frames.py / vid_align.py）
# 走系统 PATH 调 ffmpeg，不依赖这两件，不受影响。
try:
    _d, _b, _h = collect_all("rapidocr")
    datas += _d
    binaries += _b
    hiddenimports += _h
    print("[spec] 已收集可选依赖：rapidocr")
except Exception:
    print("[spec] 可选依赖未安装，跳过：rapidocr（打包版将无识字功能）")

# onnxruntime 特例：collect_all 会把 quantization/transformers/tools 等子模块
# 也收成 hiddenimport，PyInstaller 扫描二进制依赖时 import onnxruntime.quantization
# 会段错误（3.11 实测 0xC0000005，3.12 只是静默跳过）——rapidocr 只用到 onnxruntime
# 核心 InferenceSession，那些子模块全用不到。故 onnxruntime 只收 data + 动态库
# （核心子模块由 rapidocr→onnxruntime 的正常 import 链自动带入，标准钩子收 provider DLL）。
try:
    import onnxruntime  # noqa: F401  探测是否安装（未装则下面 except 跳过）
    datas += collect_data_files("onnxruntime")
    binaries += collect_dynamic_libs("onnxruntime")
    print("[spec] 已收集可选依赖：onnxruntime（仅 data + 动态库）")
except Exception:
    print("[spec] 可选依赖未安装，跳过：onnxruntime（打包版将无识字功能）")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 明确不收的庞物：本项目全用不到，收进来白撑大几十 MB
    excludes=["torch", "matplotlib", "scipy", "pandas", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EmuHive",
    debug=False,
    strip=False,
    upx=False,
    console=False,        # GUI 程序：不带黑色控制台窗
    disable_windowed_traceback=False,
    # 崩了去哪看：GUI 未捕获异常已进 logs/run_*.log（main.py excepthook）
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="EmuHive",
)
