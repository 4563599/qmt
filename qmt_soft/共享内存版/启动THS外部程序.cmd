@echo off
chcp 65001 >nul
title QMT External THS Scanner
echo Keep only the qmt_shm_bridge strategy running inside QMT.
echo Close any other external scanner for this shared-memory channel first.
echo This runs the original THS source with its existing Redis and log settings.
echo.
choice /C YN /N /M "Start the THS external scanner? [Y/N]: "
if not "%errorlevel%"=="1" exit /b 0
"C:\Program Files\Python311\python.exe" -X utf8 -u "%~dp0external_runner.py" --run --strategy ths
set "QMT_RUN_EXIT=%errorlevel%"
echo.
echo THS worker stopped. Read the result above.
pause
exit /b %QMT_RUN_EXIT%
