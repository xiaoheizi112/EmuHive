@echo off
rem probe_vbox_shot.bat - 1607 vbox-layer frame probe (does LDPlayer4's VirtualBox core let us screenshot the VM display directly?)
rem Usage: copy to 1607, put it in the LDPlayer install folder (next to ldconsole.exe), double-click.
rem Outputs in this folder: probe_vbox_shot.log (text) + probe_shot_*.png (captured frames). Bring them all back.
setlocal enabledelayedexpansion
cd /d "%~dp0"
if exist probe_vbox_shot.log del probe_vbox_shot.log

echo ===== 1. find vboxmanage (LDPlayer4 green edition keeps it in vbox64\vbox86) =====
set VBM=
for %%D in (vbox64 vbox86 vbox .) do (
  if not defined VBM (
    if exist "%%D\dnvboxmanage.exe" set VBM=%%D\dnvboxmanage.exe
    if exist "%%D\vboxmanage.exe"    set VBM=%%D\vboxmanage.exe
  )
)
if "%VBM%"=="" (
  echo [X] vboxmanage not found in vbox64/vbox86/vbox/. List what exe are in vbox64:
  dir /b vbox64\*.exe 2>nul
  echo [X] Screenshot the whole LDPlayer folder tree and send it back.
  goto :end
)
echo [OK] using %VBM%

echo ===== 2. list vms =====
"%VBM%" list vms > vms.txt 2>&1
type vms.txt

echo ===== 3. screenshot the FIRST running vm, then burst 5 for timing =====
rem grab first vm name (text between the quotes on line 1)
for /f "usebackq tokens=1 delims=" %%L in ("vms.txt") do (
  set "L=%%L"
  goto :got
)
:got
set "L=!L:"=!"
for /f "delims= {" %%N in ("!L!") do set "VM=%%N"
if not defined VM ( echo [X] could not parse a vm name & goto :end )
echo [VM] !VM!

"%VBM%" controlvm "!VM!" screenshotpng "probe_shot_1.png" 2>&1
if exist probe_shot_1.png (
  echo [OK] got probe_shot_1.png:
  dir probe_shot_1.png | findstr probe_shot
) else (
  echo [X] no image produced - vbox screenshot path is dead on this build
  goto :end
)
echo --- burst 5 (compare file timestamps = per-shot speed) ---
for /L %%i in (2,1,5) do "%VBM%" controlvm "!VM!" screenshotpng "probe_shot_%%i.png" >nul 2>&1
dir probe_shot_*.png | findstr probe_shot

echo.
echo ===== done: bring back probe_vbox_shot.log + all probe_shot_*.png =====
:end
pause
