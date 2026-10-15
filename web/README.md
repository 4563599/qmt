# QMT 股票网站

从 `C:\stock\python\python_web` 整理的独立网站副本。保留现有页面、API、
K 线展示、重点关注、推送日志、性能页面及可编辑复盘表格，不改变选股条件或 Redis 字段。

## 运行

建议使用 Python 3.11。进入 `web` 目录后：

```powershell
python -m venv venv
venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env.production
```

在本机 `.env.production` 中填写 Redis 地址和密码，然后启动：

```powershell
venv\Scripts\python -m flask --app app run --host 127.0.0.1 --port 5000
```

也可依次运行 `install.bat`、`start.bat`。访问 `http://127.0.0.1:5000/重点关注`。
使用 Flask CLI 或 Gunicorn 启动，以确保所有页面路由先完成注册；不要直接运行 `python app.py`。

Linux 部署：

```sh
python3.11 -m venv venv
venv/bin/python -m pip install -r requirements.txt
cp .env.example .env.production
venv/bin/gunicorn -c gunicorn_config.py app:app
```

生产环境应通过反向代理和访问控制提供服务，勿使用 Flask 开发服务器对外部署。
此副本未新增登录鉴权，部分接口支持写入网页编辑内容，需要由部署环境限制访问。

## 配置

`app.py` 自动读取同目录 `.env.production`，已设置的系统环境变量优先。
本仓库只提供不含凭据的 `.env.example`，真实密码、令牌及加密密钥不得提交。

| 配置 | 用途 |
| --- | --- |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD` | Redis 连接 |
| `TUSHARE_TOKEN` | 需要 Tushare 的行情和市值接口，其他 Redis 页面不要求此项 |
| `KLINE_API_AES_KEY_B64` | 加密 K 线 API 的 Base64 编码 32 字节密钥，须由部署者自行设置 |
| `FOCUS_PAGE_URL` | 表格内重点关注跳转地址，默认使用站内相对路径 |
| `GUNICORN_BIND` | Linux 监听地址，默认 `0.0.0.0:5000` |
| `GUNICORN_ACCESS_LOG` / `GUNICORN_ERROR_LOG` | Linux 日志位置，默认输出到控制台 |

## 目录和数据

- `app.py`：Flask 页面和接口。
- `templates/`、`static/`：网站模板、样式、脚本和所需第三方资源。
- `redis_review.py`、`review_layout.json`：复盘表格布局、样式和固定文字。
- `feishu_daily_review.py`：从现有日表生成器提取的本地表格函数及只读 Redis 回退逻辑。
  不包含飞书凭据、上传任务或归档任务，复盘页不依赖另一个目录中的脚本。
- `redis_config.py`、`config.py`：无真实凭据的配置。
- `stock_redis.py`：保留的 Redis 数据存储辅助模块。
- `tests/`：网站回归测试，Redis 和外部接口使用模拟对象。

主要数据由原有采集程序写入 Redis，本项目不替代 QMT 盘中扫描器。
重点关注读取 `pz`、`pz1`、`qy1`、`qy2` 等日期数据；同花顺读取 `ths`、
`thscleaning` 等数据；财联社涨停分析读取 `cls:limit_up:日期`。
网页复盘修改保存到独立 `focus:web:edits:*`，不修改源选股快照。

## 测试

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

安装 Node.js 后可运行测试中的前端逻辑断言；未安装时相应测试会跳过。

## 清理范围

未上传缓存、日志、备份、截图、空文件、个人提示词、过期操作说明及一次性 Redis
诊断/写入脚本。依赖本机桌面 QMT 源文件的 `test_qmt_*` 属于采集程序测试，
不放在独立网站目录。可能仍有用途的页面和接口保留，不做未经确认的功能删除。
本地原目录及线上部署没有被本次整理修改。
