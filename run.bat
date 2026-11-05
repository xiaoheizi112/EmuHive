@echo off
REM ============================================================
REM  run.bat - launcher for EmuHive idle tool (GUI)
REM  Keep this file ASCII-only on purpose: cmd.exe parses .bat
REM  with the OEM codepage, and UTF-8 Chinese text breaks line
REM  offsets (tested on this machine, 2026-09-20).
REM  Double-click from anywhere: it cds to the project dir,
REM  self-checks deps, installs missing ones via Tsinghua
REM  mirror, then starts the GUI (main.py).
REM ============================================================
cd /d "%~dp0"

REM ---- pick interpreter: prefer project venv, then system python ----
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

REM ---- dependency smoke test (PySide6 / OpenCV / numpy) ----
"%PY%" -c "import PySide6, cv2, numpy" 1>nul 2>nul
if errorlevel 1 (
    echo [!] Missing deps. Installing requirements.txt via Tsinghua mirror...
    "%PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [x] pip install failed. Check network, then rerun the command above.
        pause
        exit /b 1
    )
)

echo [*] Starting GUI: python main.py ...
"%PY%" main.py
if errorlevel 1 (
    echo [x] Exited with errors. See messages above.
    pause
)
