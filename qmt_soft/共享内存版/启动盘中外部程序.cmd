@echo off
chcp 65001 >nul
title QMT External Panzhong Scanner
echo Keep the QMT qmt_shm_bridge strategy running.
echo Stop any old Panzhong scanner before starting this version.
echo This starts the ORIGINAL scanner with Redis and configured DingTalk alerts.
echo.
choice /C YN /N /M "Start the external scanner? [Y/N]: "
if not "%errorlevel%"=="1" exit /b 0
"C:\Program Files\Python311\python.exe" -X utf8 -u "%~dp0external_runner.py" --run
set "QMT_RUN_EXIT=%errorlevel%"
echo.
echo Scanner finished. Read the result above.
pause
exit /b %QMT_RUN_EXIT%
