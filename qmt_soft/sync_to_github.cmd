@echo off
setlocal
cd /d "%~dp0.."
set "PYTHON=C:\Program Files\Python311\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
git diff --cached --quiet
if errorlevel 1 (
  echo Existing staged changes found. Commit or unstage them before syncing.
  goto failed
)
echo Export desktop sources, remove credentials, and run offline tests.
"%PYTHON%" -X utf8 qmt_soft\tools\sync_desktop.py --test
if errorlevel 1 goto failed
git diff --stat -- qmt_soft
git status --short -- qmt_soft
choice /c YN /n /m "Commit and upload qmt_soft to public GitHub? [Y/N] "
if errorlevel 2 exit /b 0
git add -- qmt_soft
if errorlevel 1 goto failed
git diff --cached --quiet
if not errorlevel 1 goto upload
git commit -m "Sync desktop QMT shared-memory strategies"
if errorlevel 1 goto failed
:upload
"%PYTHON%" -X utf8 qmt_soft\tools\publish_github.py --push
if errorlevel 1 goto failed
echo Upload complete. Desktop running files were not changed.
pause
exit /b 0
:failed
echo Stopped because a step failed. No force push was performed.
pause
exit /b 1
