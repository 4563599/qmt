@echo off
echo 安装股票预警网站依赖...
cd /d %~dp0

echo 创建虚拟环境...
python -m venv venv

echo 激活虚拟环境...
call venv\Scripts\activate

echo 安装依赖...
pip install -r requirements.txt

echo 安装完成！
echo 运行 start.bat 启动网站
pause
