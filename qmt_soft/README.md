# QMT 盘中与同花顺程序

此目录包含两份策略源码，以及运行它们所需的共享内存行情桥、外部程序、桌面控制台、启动入口、测试和说明。

```text
qmt_soft/
  盘中.py
  ths筛选.py
  requirements.txt
  共享内存版/
    qmt_shm_bridge.py
    external_runner.py
    launcher_gui.py
    打开策略控制台.vbs
    docs/
    tests/
```

## 安装与运行

1. Windows 安装 Python 3.11，勾选 Tcl/Tk。外部依赖安装到外部 Python，不要安装进 QMT 的内置 Python。
2. 在此目录执行：`python -m pip install -r requirements.txt`。
3. 配置下面的私有环境变量。共享内存版中的 CMD/VBS 默认使用 `C:\Program Files\Python311`；其他安装位置需修改这些启动入口。
4. 在 QMT 导入并手动运行 `共享内存版/qmt_shm_bridge.py`。QMT 中不运行原盘中或同花顺策略。
5. 双击 `共享内存版/检查共享内存.cmd`，通过后打开 `共享内存版/打开策略控制台.vbs`，选择日期并启动。

完整操作、时间开关、历史模式、自动启动和故障说明见 [共享内存版说明](共享内存版/README.md)。自动启动计划任务需要在本机明确安装，不会因下载代码自动安装。交易日校验沿用本机已配置的 Tushare 授权。

## 私有配置

公开副本只把以下凭证改为读取同名环境变量，其他策略配置和选股逻辑不变：

| 变量 | 用途 |
|---|---|
| `REDIS_PASSWORD` | 两份策略的 Redis 密码 |
| `DINGTALK_WEBHOOK` | 盘中默认钉钉地址 |
| `DINGTALK_CONDITION3_WEBHOOK` | 盘中 condition3 钉钉地址 |

请在 Windows 用户环境变量中自行填写，然后重新打开控制台。临时在 PowerShell 中设置 `$env:REDIS_PASSWORD` 等变量后，从同一窗口运行 `python .\共享内存版\launcher_gui.py` 也可以。不要把真实密码或 webhook 写进受 Git 管理的文件。Redis 地址、端口、数据库沿用源码现有配置，其他环境部署前应先核对；空密码不会自动关闭 Redis 密码校验。

日志、运行缓存、备份、机器验证结果及私有配置不上传。生产运行的桌面原文件不会被同步脚本修改。

## 后续同步

本机专用 Git 目录：`C:\stock\github\qmt`。桌面代码继续在 `C:\Users\lyy\Desktop\qmt` 修改、运行。

双击本目录的 **`sync_to_github.cmd`**：白名单复制桌面代码、移除三项凭证、验证业务代码未变、运行离线测试；确认后只提交 `qmt_soft` 并上传。不会清理桌面文件，不会启动选股，不会读写生产 Redis 或发送钉钉。

也可以仅导出和测试，不上传：

```powershell
python -X utf8 .\qmt_soft\tools\sync_desktop.py --test
```

以上命令从仓库根目录执行。自定义来源使用 `--source 'D:\自己的目录\qmt'`。新的文件类型需要先加入同步脚本白名单；被删除的源文件不会自动删除 GitHub 文件，需人工确认删除。

上传器使用 Git Credential Manager 中已有的 GitHub 登录，通过 HTTPS REST 上传与本地完全相同的 Git 对象；不保存或显示令牌，不强制推送。远程分支变化、未提交改动、非 `qmt_soft` 提交、日志等文件都会阻止上传。其他电脑也可以正常使用 `git push origin master`。

首次导出的旧版回归基准保存在测试 fixtures 内；这是去掉凭证的固定测试材料，不是启动用的第三份策略。当前源码摘要在 `source_manifest.json`。
