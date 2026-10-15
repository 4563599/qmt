"""
Gunicorn配置文件
"""
import multiprocessing
import os

# 绑定地址和端口
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:5000")

# 工作进程数
workers = multiprocessing.cpu_count() * 2 + 1

# 工作模式
worker_class = "gevent"

# 超时时间
timeout = 120

# 日志
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")
errorlog = os.environ.get("GUNICORN_ERROR_LOG", "-")
loglevel = "info"

# 进程名
proc_name = "stock_web"

# 守护进程
daemon = False

# 最大请求数
max_requests = 1000
max_requests_jitter = 50
