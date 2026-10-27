@echo off
chcp 65001 >nul
title QMT External Panzhong + THS
echo Keep only the qmt_shm_bridge strategy running inside QMT.
echo Close the OLD external scanner window before starting this combined version.
echo Do not start old PANZHONG or THS strategies inside QMT.
echo This runs BOTH original scripts with their existing Redis, logs and alert settings.
echo.
choice /C YN /N /M "Start both Panzhong and THS? [Y/N]: "
if not "%errorlevel%"=="1" exit /b 0
"C:\Program Files\Python311\python.exe" -X utf8 -u "%~dp0external_runner.py" --run --strategy both
set "QMT_RUN_EXIT=%errorlevel%"
echo.
echo Both strategy workers stopped. Read the result above.
pause
exit /b %QMT_RUN_EXIT%
