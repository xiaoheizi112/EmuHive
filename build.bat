@echo off
chcp 65001 >nul
rem 一键打包（分发审计 P1-11）：双击即出 dist\EmuHive\ 成品文件夹。
rem 注意：本文件必须存成 GBK+CRLF（中文 Windows 的 cmd 按 GBK 解析批处理，
rem    UTF-8 中文注释会被拆出假引号/假括号，行被拆碎后直接报「不是内部或外部命令」）。
rem 必须用 _build311 虚拟环境里的 python（Py3.11 + PySide6 6.4.3）：
rem    目标机 Win10 1607 只支持 Qt<=6.4，用系统 python（3.12+Qt6.11）会静默
rem    产出一个 1607 装不上的包（照 requirements-build311.txt 明文钉版）。
cd /d %~dp0

set "BUILD_PY=_build311\Scripts\python.exe"
if not exist "%BUILD_PY%" (
    echo [错误] 找不到打包环境 %BUILD_PY%
    echo        1607 兼容包必须用 Python 3.11 + PySide6 6.4.3 打包，
    echo        开发环境（系统 python 3.12+Qt6.11）打出来的包装不上目标机。
    echo        请按 requirements-build311.txt 建环境（顶部有复现步骤）：
    echo          py -3.11 -m venv _build311
    echo          _build311\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-build311.txt
    pause
    exit /b 1
)

echo [1/3] 用 _build311 环境打包（首次约 2~5 分钟，勿关窗）...
"%BUILD_PY%" -m PyInstaller --clean --noconfirm emuhive.spec
if errorlevel 1 (
    echo [错误] 打包失败，把上面报错截图发出来排查
    pause
    exit /b 1
)

echo [2/3] 附带制作指南到成品目录...
if exist docs xcopy /y /i /s /q docs \"dist\EmuHive\docs\" >nul

echo [3/3] 完成！成品在 dist\EmuHive\
echo.
echo 整个文件夹拷走即可分发，双击其中 EmuHive.exe 运行。
echo 注意：不带 configs/templates，新机器首启为全新状态
echo （要带自己的模板请手动拷 templates\ 过去）。
pause
