@echo off
echo 启动股票预警网站...
cd /d %~dp0
call venv\Scripts\activate
if not defined PORT set PORT=5000
python -m flask --app app run --host 127.0.0.1 --port %PORT%
pause
