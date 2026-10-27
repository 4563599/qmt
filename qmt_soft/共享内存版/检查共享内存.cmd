@echo off
chcp 65001 >nul
title QMT Shared Memory Check
"C:\Program Files\Python311\python.exe" -X utf8 -u "%~dp0external_runner.py" --check
set "QMT_CHECK_EXIT=%errorlevel%"
echo.
echo Check finished. Read the result above.
pause
exit /b %QMT_CHECK_EXIT%
