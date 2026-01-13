# -*- coding: gbk -*-
"""
QMT阴线都向上策略
策略逻辑：
1. 今日最高涨幅>4.5%
   计算最高涨幅：（今日最高点价格 - 昨日收盘价）÷ 昨日收盘价 × 100%
2. 最近10个交易日内最高涨幅≥6.85%次数大于等于5（拉升次数）
3. 10天内阴线的次数<=3或者没有阴线
4. 10天内如果出现阴线则满足阴线都向上
   - 阴线向上：阴线满足（上引线大于阴线实体并且上引线大于下引线）或者满足（最高涨幅≥6.85%）
   - 阴线都向上：最近10个交易日所有阴线满足阴线向上或者没有阴线
5. 计算不下杀次数：
   - 不下杀条件1：满足十字星（收盘价和开盘价幅度 ≤ 1.68%）
   - 不下杀条件2：下引线 < K线实体或者没有下引线
   - 不下杀定义（满足不下杀条件1或者不下杀条件2）
"""

import numpy as np
from datetime import datetime
import threading
import smtplib
from email.mime.text import MIMEText
from email.header import Header
try:
    import requests
except:
    requests = None

try:
    import redis
except:
    redis = None

import queue
import time
import json
import base64

# 全局变量
g_stock_pool = []
g_qualified_stocks = {}
g_all_found_stocks = {}
g_scan_count = 0
g_initialized = False
g_context = None
g_lock = threading.Lock()

# 历史数据缓存
g_history_data = {}
g_prev_close_cache = {}
g_stock_names = {}

# 配置参数
DEBUG_MODE = True
CHECK_TRADING_TIME = True  # 开启交易时间检查，收盘后停止执行
PRINT_PROGRESS = True
LOG_ENABLED = False  # 日志开关，False=关闭日志打印
REDIS_LOG_ENABLED = True  # Redis日志开关，True=打印Redis相关日志
LOG_FILE = r"C:\Users\lyy\Desktop\股票\预警\阴线都向上.log"  # 日志文件路径

# ========== 写入文件开关 ==========
WRITE_FILE_ENABLED = True  # True=写入文件，False=不写入（测试模式）

# 输出文件路径
OUTPUT_DIR = r"C:\Users\lyy\Desktop\股票\预警"
STRATEGY_NAME = "阴线都向上"

# ========== QQ邮箱配置 ==========
EMAIL_ENABLED = True  # True=发送邮件，False=不发送
EMAIL_RECEIVERS = ["4563599@qq.com"]
EMAIL_SENDER = "4563599@qq.com"
EMAIL_AUTH_CODE = "iruvzesxztercaic"
EMAIL_SMTP_SERVER = "smtp.qq.com"
EMAIL_SMTP_PORT = 465

# ========== WxPusher微信推送配置 ==========
WXPUSHER_ENABLED = True  # 是否启用微信推送
WXP_TOKEN = "AT_mUKiCaJyUxhf0V1K9X3MuhqDaLmguFY4"
WXP_UID = "UID_d5iiHewR5VxqIWV3t2vkumOkykre"
WXP_SUMMARY = "【阴线都向上】"

# ========== Redis配置 ==========
REDIS_ENABLED = True  # 是否启用Redis存储
REDIS_HOST = "175.24.178.113"
REDIS_PORT = 5268
REDIS_PASSWORD = "lyy654321@REDIS"
REDIS_DB = 0

g_email_sent_stocks = set()
g_redis_client = None

# 消息队列
g_message_queue = queue.Queue()


def log(msg, level="INFO"):
    if not LOG_ENABLED:
        return
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {msg}")


def log_redis(msg, level="INFO"):
    """Redis专用日志，同时打印到控制台和文件"""
    if not REDIS_LOG_ENABLED:
        return
    
    import os
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_msg = f"[{timestamp}] [{level}] {msg}"
    
    # 打印到控制台
    print(log_msg)
    
    # 写入日志文件
    try:
        # 确保日志目录存在
        log_dir = os.path.dirname(LOG_FILE)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_msg + '\n')
    except Exception as e:
        print(f"[ERROR] 写入日志文件失败: {e}")


def connect_redis():
    """连接Redis"""
    log_redis("=" * 60)
    log_redis("开始连接Redis")
    log_redis("=" * 60)
    
    if redis is None:
        log_redis("❌ redis模块未安装，无法连接Redis", "ERROR")
        log_redis("请运行: pip install redis", "ERROR")
        return None
    
    log_redis(f"Redis模块已安装，版本: {redis.__version__ if hasattr(redis, '__version__') else '未知'}")
    log_redis(f"连接信息: {REDIS_HOST}:{REDIS_PORT}")
    
    try:
        r = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            db=REDIS_DB,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5
        )
        
        log_redis("正在测试连接...")
        r.ping()
        
        # 获取Redis服务器信息
        info = r.info()
        log_redis("✅ Redis连接成功！")
        log_redis(f"Redis版本: {info.get('redis_version', 'N/A')}")
        log_redis(f"已用内存: {info.get('used_memory_human', 'N/A')}")
        log_redis(f"数据库: DB{REDIS_DB}")
        log_redis("=" * 60)
        
        return r
    except redis.ConnectionError as e:
        log_redis(f"❌ Redis连接失败（网络错误）: {e}", "ERROR")
        return None
    except redis.AuthenticationError as e:
        log_redis(f"❌ Redis认证失败（密码错误）: {e}", "ERROR")
        return None
    except Exception as e:
        log_redis(f"❌ Redis连接异常: {e}", "ERROR")
        return None


def is_cross_star(o, c):
    """判断是否为十字星：收盘价和开盘价幅度 ≤ 1.68%"""
    if o <= 0:
        return False
    return abs(c - o) / o * 100 <= 1.68


def check_no_kill(o, c, l):
    """
    判断是否满足不下杀条件
    不下杀条件1：满足十字星（收盘价和开盘价幅度 ≤ 1.68%）
    不下杀条件2：下引线 < K线实体 或者 没有下引线
    """
    # 条件1：十字星
    if is_cross_star(o, c):
        return True
    
    # 条件2：下引线 < K线实体 或 没有下引线
    lower_shadow = min(o, c) - l
    body = abs(c - o)
    
    if lower_shadow <= 0:  # 没有下引线
        return True
    
    if lower_shadow < body:  # 下引线 < 实体
        return True
    
    return False


def calc_max_gain(h, prev_c):
    """
    计算最高涨幅
    最高涨幅 = (今日最高点价格 - 昨日收盘价) ÷ 昨日收盘价 × 100%
    """
    if prev_c <= 0:
        return 0
    return (h - prev_c) / prev_c * 100


def check_yin_up(o, c, h, l, prev_c):
    """
    判断阴线是否向上
    阴线向上：阴线满足（上引线大于阴线实体并且上引线大于下引线）或者满足（最高涨幅≥6.85%）
    """
    if c >= o:  # 不是阴线
        return False
    
    # 条件1：上引线大于阴线实体并且上引线大于下引线
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    body = o - c  # 阴线实体
    
    cond1 = (upper_shadow > body) and (upper_shadow > lower_shadow)
    
    # 条件2：最高涨幅≥6.85%
    max_gain = calc_max_gain(h, prev_c)
    cond2 = max_gain >= 6.85
    
    return cond1 or cond2


def get_market_cap(tick, stock):
    """从实时行情tick中获取市值（亿元）"""
    try:
        # 方法1：直接从tick中获取流通市值
        market_cap = tick.get('circulatingMarketCap', 0)  # 流通市值（元）
        if market_cap > 0:
            return round(market_cap / 100000000, 2)  # 转换为亿元
        
        # 方法2：从tick中获取总市值
        total_cap = tick.get('totalMarketCap', 0)  # 总市值（元）
        if total_cap > 0:
            return round(total_cap / 100000000, 2)
        
        # 方法3：通过流通股本计算
        circulating_shares = tick.get('circulatingShares', 0)  # 流通股本（股）
        price = tick.get('lastPrice', 0)  # 当前价格
        if circulating_shares > 0 and price > 0:
            return round(circulating_shares * price / 100000000, 2)
        
        # 方法4：尝试其他可能的字段名
        # 有些QMT版本可能使用不同的字段名
        for field in ['marketCap', 'market_cap', 'totalValue', 'total_value']:
            value = tick.get(field, 0)
            if value > 0:
                return round(value / 100000000, 2)
        
        return 0.0
    except Exception as e:
        log_redis(f"获取市值失败 [{stock}]: {e}", "WARN")
        return 0.0



def message_sender_worker():
    """消息发送工作线程，从队列中取出消息并发送"""
    while True:
        try:
            # 从队列中获取消息
            message = g_message_queue.get(timeout=1)
            
            if message is None:  # 退出信号
                break
            
            msg_type = message.get('type')
            data = message.get('data')
            
            if msg_type == 'wechat':
                # 发送微信消息
                send_wechat_message(data)
                time.sleep(0.6)  # 限制QPS
            
            elif msg_type == 'email':
                # 发送邮件
                send_email_message(data)
                time.sleep(0.5)  # 邮件发送间隔
            
            elif msg_type == 'redis':
                # 存储到Redis
                save_to_redis(data)
            
            g_message_queue.task_done()
            
        except queue.Empty:
            continue
        except Exception as e:
            log(f">>> 消息发送线程异常: {e}", "ERROR")


def send_wechat_message(data):
    """发送微信消息"""
    if not WXPUSHER_ENABLED or requests is None:
        return
    
    try:
        code = data['code']
        name = data['name']
        gain = data['gain']
        time_str = data['time']
        date_weekday = data['date_weekday']
        ls_num = data.get('ls_num', 0)
        no_xs_num = data.get('no_xs_num', 0)
        
        msg_content = f"{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n拉升次数：{ls_num}\n不下杀次数：{no_xs_num}\n时间：{time_str} {date_weekday}"
        summary = f"阴线都向上 {name} {gain}"
        
        url = "https://wxpusher.zjiecode.com/api/send/message"
        payload = {
            "appToken": WXP_TOKEN,
            "content": msg_content,
            "summary": summary,
            "contentType": 1,
            "uids": [WXP_UID]
        }
        
        response = requests.post(url, json=payload, timeout=10)
        res = response.json()
        
        if res.get('code') == 1000:
            log(f">>> 微信推送成功: {name} {code}")
        else:
            log(f">>> 微信推送失败: {res.get('msg')}", "WARN")
    
    except Exception as e:
        log(f">>> 微信推送异常: {e}", "ERROR")


def send_email_message(data):
    """发送邮件消息"""
    if not EMAIL_ENABLED:
        return
    
    try:
        code = data['code']
        name = data['name']
        gain = data['gain']
        time_str = data['time']
        date_weekday = data['date_weekday']
        current_time = data['current_time']
        ls_num = data.get('ls_num', 0)
        no_xs_num = data.get('no_xs_num', 0)
        
        subject = f"阴线都向上 {name} {time_str} {date_weekday}"
        body = f"发现时间：{current_time}\n\n{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n拉升次数：{ls_num}\n不下杀次数：{no_xs_num}\n时间：{time_str} {date_weekday}"
        
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['From'] = Header(EMAIL_SENDER)
        msg['To'] = Header(','.join(EMAIL_RECEIVERS))
        msg['Subject'] = Header(subject, 'utf-8')
        
        server = smtplib.SMTP_SSL(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT)
        server.login(EMAIL_SENDER, EMAIL_AUTH_CODE)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVERS, msg.as_string())
        server.quit()
        
        log(f">>> 邮件发送成功: {name} {code}")
    
    except Exception as e:
        log(f">>> 邮件发送失败: {e}", "ERROR")


def save_to_redis(data):
    """保存数据到Redis（使用Base64编码避免敏感词检测）"""
    if not REDIS_ENABLED:
        log_redis("Redis存储已关闭，跳过", "WARN")
        return
    
    if g_redis_client is None:
        log_redis("Redis客户端未初始化，跳过存储", "ERROR")
        return
    
    try:
        date_str = data['date_str']
        stock_data = data['stock_data']
        
        # 键名格式：stocks_yxdxs:YYYY-MM-DD
        key = f"stocks_yxdxs:{date_str}"
        
        # 字段名：股票代码
        field = stock_data['code']
        
        # 值：JSON字符串
        json_str = json.dumps(stock_data, ensure_ascii=False)
        
        # Base64编码（避免敏感词检测）
        value = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
        
        log_redis(f"准备存储到Redis: {key}")
        log_redis(f"  股票: {stock_data['code']} {stock_data['name']}")
        log_redis(f"  涨跌幅: {stock_data['today_change']:.2f}%")
        log_redis(f"  拉升次数: {stock_data['ls_num']}")
        log_redis(f"  不下杀次数: {stock_data['no_xs_num']}")
        log_redis(f"  市值: {stock_data['market_cap']}亿")
        log_redis(f"  数据已Base64编码")
        
        # 设置hash字段
        result = g_redis_client.hset(key, field, value)
        log_redis(f"  HSET结果: {'新增' if result == 1 else '更新'}")
        
        # 设置过期时间（30天）
        ttl = 30 * 24 * 60 * 60
        g_redis_client.expire(key, ttl)
        log_redis(f"  过期时间: 30天")
        
        # 验证存储
        stored_value = g_redis_client.hget(key, field)
        if stored_value:
            # 解码验证
            try:
                decoded_json = base64.b64decode(stored_value.encode('utf-8')).decode('utf-8')
                decoded_data = json.loads(decoded_json)
                log_redis(f"✅ Redis存储成功: {stock_data['code']} {stock_data['name']}")
                log_redis(f"  验证解码: {decoded_data['name']}")
            except Exception as e:
                log_redis(f"⚠️  Redis存储成功但解码验证失败: {e}", "WARN")
        else:
            log_redis(f"⚠️  Redis存储验证失败: {stock_data['code']} {stock_data['name']}", "WARN")
    
    except redis.RedisError as e:
        log_redis(f"❌ Redis存储失败（Redis错误）: {e}", "ERROR")
        log_redis(f"   股票: {stock_data.get('code', 'N/A')} {stock_data.get('name', 'N/A')}", "ERROR")
    except Exception as e:
        log_redis(f"❌ Redis存储失败（未知错误）: {e}", "ERROR")
        log_redis(f"   股票: {stock_data.get('code', 'N/A')} {stock_data.get('name', 'N/A')}", "ERROR")


def clear_today_files():
    """清除今天的输出文件和日志文件"""
    import os
    
    try:
        # 获取今天的日期
        today = datetime.now().strftime('%Y%m%d')  # 20260113
        
        # 文件1：YYYYMMDD阴线都向上.txt
        output_filename = f"{today}阴线都向上.txt"
        output_file = os.path.join(OUTPUT_DIR, output_filename)
        
        # 文件2：阴线都向上.log
        log_file = LOG_FILE
        
        files_to_clear = [output_file, log_file]
        
        for file_path in files_to_clear:
            if os.path.exists(file_path):
                try:
                    # 清空文件内容（保留文件）
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write('')
                    print(f"✓ 已清空文件: {file_path}")
                except Exception as e:
                    print(f"✗ 清空文件失败 [{file_path}]: {e}")
            else:
                print(f"○ 文件不存在，跳过: {file_path}")
        
        print("")
    
    except Exception as e:
        print(f"清除文件时发生错误: {e}")


def init(ContextInfo):
    global g_stock_pool, g_initialized, g_context, g_redis_client
    
    g_context = ContextInfo
    
    # 清除今天的文件
    print("=" * 60)
    print("清除今天的输出文件和日志")
    print("=" * 60)
    clear_today_files()
    
    log("=" * 60)
    log("========== 阴线都向上策略初始化开始 ==========")
    log("=" * 60)
    
    # 连接Redis
    if REDIS_ENABLED:
        log_redis("\n>>> 步骤1: 初始化Redis连接")
        g_redis_client = connect_redis()
        if g_redis_client:
            log_redis(">>> Redis初始化成功\n")
        else:
            log_redis(">>> Redis初始化失败，将跳过Redis存储功能\n", "ERROR")
    
    # 订阅全市场行情
    log(">>> 订阅全市场行情...")
    try:
        ContextInfo.subscribe_whole_quote(["SH", "SZ"])
        log(">>> 全市场行情订阅成功 (SH, SZ)")
    except Exception as e:
        log(f">>> 全市场行情订阅失败: {e}", "WARN")
    
    # 获取股票池（预过滤8/4开头）
    g_stock_pool = get_stock_pool(ContextInfo)
    log(f"股票池加载完成，共 {len(g_stock_pool)} 只股票")
    
    # 预加载股票名称
    log("")
    log(">>> 正在预加载股票名称...")
    preload_stock_names(ContextInfo)
    log(f">>> 股票名称加载完成: {len(g_stock_names)} 只")
    
    # 预加载历史数据
    log("")
    log(">>> 正在预加载历史数据...")
    preload_history_data(ContextInfo)
    log(f">>> 历史数据预加载完成: {len(g_history_data)} 只")
    
    ContextInfo.run_time("scan_stocks", "3nSecond", "2024-01-01 09:30:00", "SH")
    log("定时器设置完成，每3秒扫描一次")
    
    # 启动消息发送线程
    sender_thread = threading.Thread(target=message_sender_worker, daemon=True)
    sender_thread.start()
    log(">>> 消息发送线程已启动")
    
    # 显示开关状态
    log("")
    log(f">>> 写入文件开关: {'开启' if WRITE_FILE_ENABLED else '关闭（测试模式）'}")
    log(f">>> 发送邮件开关: {'开启' if EMAIL_ENABLED else '关闭'}")
    log(f">>> Redis存储开关: {'开启' if REDIS_ENABLED else '关闭'}")
    
    g_initialized = True
    log("=" * 60)
    log("========== 阴线都向上策略初始化完成 ==========")
    log("=" * 60)
    log("")
    
    log(">>> 执行首次扫描...")
    scan_stocks(ContextInfo)


def get_stock_pool(ContextInfo):
    """获取股票池，预过滤8/4开头"""
    stock_list = []
    log("正在获取股票列表...")
    
    sectors = ["沪深A股", "科创板"]
    all_stocks = []
    
    for sector in sectors:
        try:
            stocks = ContextInfo.get_stock_list_in_sector(sector)
            if stocks:
                log(f"  {sector}: 获取到 {len(stocks)} 只")
                all_stocks.extend(stocks)
        except Exception as e:
            log(f"  {sector}: 获取失败 - {str(e)}", "WARN")
    
    all_stocks = list(set(all_stocks))
    
    for stock in all_stocks:
        code = stock.split('.')[0]
        if 'ST' in stock.upper():
            continue
        if code.startswith('8') or code.startswith('4'):
            continue
        stock_list.append(stock)
    
    log(f"最终股票池: {len(stock_list)} 只")
    return stock_list


def preload_stock_names(ContextInfo):
    """预加载股票名称"""
    global g_stock_names
    for stock in g_stock_pool:
        try:
            name = ContextInfo.get_stock_name(stock)
            g_stock_names[stock] = name if name else stock.split('.')[0]
        except:
            g_stock_names[stock] = stock.split('.')[0]


def get_date_with_weekday():
    """获取星期，格式：星期日"""
    weekdays = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
    now = datetime.now()
    return weekdays[now.weekday()]


def preload_history_data(ContextInfo):
    """预加载历史数据"""
    global g_history_data, g_prev_close_cache
    
    batch_size = 300
    total = len(g_stock_pool)
    success_count = 0
    
    for i in range(0, total, batch_size):
        batch = g_stock_pool[i:i+batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size
        
        try:
            data = ContextInfo.get_market_data(
                ['open', 'high', 'low', 'close'], 
                batch, 
                period='1d', 
                count=15
            )
            
            if data is not None:
                for stock in batch:
                    try:
                        stock_data = extract_stock_data(data, stock)
                        if stock_data is not None and len(stock_data) >= 11:
                            g_history_data[stock] = {
                                'closes': stock_data['close'].values.astype(float),
                                'opens': stock_data['open'].values.astype(float),
                                'highs': stock_data['high'].values.astype(float),
                                'lows': stock_data['low'].values.astype(float)
                            }
                            g_prev_close_cache[stock] = float(stock_data['close'].iloc[-2])
                            success_count += 1
                    except:
                        pass
        except Exception as e:
            log(f"  批次{batch_num}异常: {str(e)}", "ERROR")
        
        if PRINT_PROGRESS:
            progress = min(i + batch_size, total)
            log(f"  [预加载] 批次 {batch_num}/{total_batches} | 进度: {progress}/{total} | 成功: {success_count}")
    
    log(f"预加载完成: 成功 {success_count} 只")


def extract_stock_data(data, stock):
    """从批量数据中提取单只股票数据"""
    stock_data = None
    if hasattr(data, 'minor_xs'):
        try:
            stock_data = data.minor_xs(stock)
        except:
            pass
    if stock_data is None and hasattr(data, 'loc'):
        try:
            stock_data = data.loc[:, :, stock]
        except:
            pass
    if stock_data is None:
        try:
            stock_data = data[stock]
        except:
            pass
    return stock_data


def handlebar(ContextInfo):
    pass



def scan_stocks(ContextInfo):
    """扫描股票"""
    global g_qualified_stocks, g_scan_count
    
    current_time = datetime.now()
    if CHECK_TRADING_TIME and not is_trading_time(current_time):
        return
    
    g_scan_count += 1
    log("")
    log("#" * 70)
    log(f"############## 第 {g_scan_count} 轮扫描开始（阴线都向上） ##############")
    log("#" * 70)
    
    start_time = datetime.now()
    
    # 获取实时行情
    log("")
    log(">>> 步骤1: 获取实时行情")
    tick_start = datetime.now()
    
    try:
        all_ticks = ContextInfo.get_full_tick(g_stock_pool)
        tick_count = len(all_ticks) if all_ticks else 0
        tick_elapsed = (datetime.now() - tick_start).total_seconds()
        log(f">>> 行情获取完成: {tick_count} 只, 耗时 {tick_elapsed:.3f} 秒")
    except Exception as e:
        log(f">>> 行情获取失败: {e}", "ERROR")
        all_ticks = {}
    
    if not all_ticks:
        log(">>> 无行情数据，跳过本轮扫描", "WARN")
        return
    
    # 极速过滤
    log("")
    log(">>> 步骤2: 极速过滤（阴线都向上版）")
    
    qualified = []
    stats = {
        'total': len(g_stock_pool),
        'no_tick': 0, 'no_prev': 0, 'no_hist': 0,
        'fail_zf45': 0, 'fail_ls_count': 0, 'fail_yinxian': 0, 'fail_yin_up': 0,
        'pass': 0
    }
    
    for stock in g_stock_pool:
        result = check_stock_yxdxs(stock, all_ticks, stats, ContextInfo)
        if result:
            qualified.append(result)
            log(f"  ★ 命中: {stock} | 涨幅:{result['current_gain']}% | 拉升:{result['ls_num']}次 | 阴线:{result['yinxian_count']}天 | 不下杀:{result['no_xs_num']}次", "SUCCESS")
    
    total_elapsed = (datetime.now() - start_time).total_seconds()
    
    # 打印统计
    log("")
    log("=" * 60)
    log("扫描统计汇总（阴线都向上版）")
    log("=" * 60)
    log(f"总股票数: {stats['total']}")
    log(f"总扫描耗时: {total_elapsed:.2f} 秒")
    log("-" * 40)
    log(f"无行情数据: {stats['no_tick']}")
    log(f"无昨收价: {stats['no_prev']}")
    log(f"无历史数据: {stats['no_hist']}")
    log(f"第1道淘汰(今日最高涨幅<4.5%): {stats['fail_zf45']}")
    log(f"第2道淘汰(拉升次数<5): {stats['fail_ls_count']}")
    log(f"第3道淘汰(阴线数量>3): {stats['fail_yinxian']}")
    log(f"第4道淘汰(阴线都向上不满足): {stats['fail_yin_up']}")
    log(f"全部通过: {stats['pass']}")
    log("=" * 60)
    
    # 处理结果
    process_results(qualified, current_time, ContextInfo)
    
    log("")
    log("#" * 70)
    log(f"############## 第 {g_scan_count} 轮扫描结束 ##############")
    log("#" * 70)


def check_stock_yxdxs(stock, all_ticks, stats, ContextInfo):
    """
    检查单只股票 - 阴线都向上版
    条件：
    1. 今日最高涨幅>4.5%
    2. 最近10个交易日内最高涨幅≥6.85%次数大于等于5（拉升次数）
    3. 10天内阴线的次数<=3或者没有阴线
    4. 10天内如果出现阴线则满足阴线都向上
    """
    tick = all_ticks.get(stock)
    if not tick:
        stats['no_tick'] += 1
        return None
    
    prev_close = g_prev_close_cache.get(stock)
    if not prev_close or prev_close <= 0:
        stats['no_prev'] += 1
        return None
    
    today_high = tick.get('high', 0)
    if not today_high or today_high <= 0:
        stats['no_tick'] += 1
        return None
    
    # 第1道: 今日最高涨幅>4.5%
    today_max_gain = calc_max_gain(today_high, prev_close)
    if today_max_gain <= 4.5:
        stats['fail_zf45'] += 1
        return None
    
    hist = g_history_data.get(stock)
    if not hist:
        stats['no_hist'] += 1
        return None
    
    closes = hist['closes']
    opens = hist['opens']
    highs = hist['highs']
    lows = hist['lows']
    
    if len(closes) < 11:
        stats['no_hist'] += 1
        return None
    
    # 第2道: 最近10个交易日内最高涨幅≥6.85%次数（拉升次数）
    ls_num = 0  # 拉升次数
    for i in range(len(closes) - 10, len(closes)):
        if i >= 1 and closes[i-1] > 0:
            max_gain = calc_max_gain(highs[i], closes[i-1])
            if max_gain >= 6.85:
                ls_num += 1
    
    if ls_num < 5:
        stats['fail_ls_count'] += 1
        return None
    
    # 第3道: 10天内阴线的次数<=3
    yinxian_count = 0
    yin_indices = []  # 记录阴线的索引
    for i in range(len(closes) - 10, len(closes)):
        if closes[i] < opens[i]:
            yinxian_count += 1
            yin_indices.append(i)
    
    if yinxian_count > 3:
        stats['fail_yinxian'] += 1
        return None
    
    # 第4道: 10天内如果出现阴线则满足阴线都向上
    if yinxian_count > 0:
        all_yin_up = True
        for i in yin_indices:
            o = opens[i]
            c = closes[i]
            h = highs[i]
            l = lows[i]
            prev_c = closes[i-1] if i >= 1 else 0
            
            if not check_yin_up(o, c, h, l, prev_c):
                all_yin_up = False
                break
        
        if not all_yin_up:
            stats['fail_yin_up'] += 1
            return None
    
    # 计算不下杀次数（10天内）
    no_xs_num = 0
    for i in range(len(closes) - 10, len(closes)):
        o = opens[i]
        c = closes[i]
        l = lows[i]
        
        if check_no_kill(o, c, l):
            no_xs_num += 1
    
    # 通过所有条件
    stats['pass'] += 1
    
    current_price = tick.get('lastPrice', 0)
    if not current_price:
        current_price = tick.get('price', tick.get('last', 0))
    
    volume = tick.get('volume', tick.get('vol', 0))
    
    # 当前价格涨幅
    current_gain = (current_price - prev_close) / prev_close * 100 if current_price > 0 else today_max_gain
    
    # 从tick实时获取市值
    market_cap = get_market_cap(tick, stock)
    
    return {
        'stock': stock,
        'today_max_gain': round(today_max_gain, 2),  # 最高价涨幅
        'current_gain': round(current_gain, 2),  # 当前价格涨幅
        'ls_num': ls_num,  # 拉升次数
        'yinxian_count': yinxian_count,  # 阴线次数
        'no_xs_num': no_xs_num,  # 不下杀次数
        'market_cap': market_cap,  # 市值（从tick获取）
        'current_price': current_price,
        'prev_close': round(prev_close, 2),
        'today_high': round(today_high, 2),
        'volume': volume,
        'time': datetime.now().strftime('%H:%M:%S')
    }



def push_to_wechat(stocks):
    """通过队列推送微信消息"""
    log(f">>> 准备微信推送，共 {len(stocks)} 只股票")
    
    if not WXPUSHER_ENABLED:
        log(">>> 微信推送已关闭，跳过")
        return
    
    if requests is None:
        log(">>> requests模块未安装，无法推送微信", "ERROR")
        return
    
    date_weekday = get_date_with_weekday()
    
    for item in stocks:
        code = item['stock'].split('.')[0]
        name = g_stock_names.get(item['stock'], code)
        gain = f"{item['current_gain']:.2f}%"
        time_str = item['time']
        ls_num = item.get('ls_num', 0)
        no_xs_num = item.get('no_xs_num', 0)
        
        # 将消息放入队列
        message = {
            'type': 'wechat',
            'data': {
                'code': code,
                'name': name,
                'gain': gain,
                'time': time_str,
                'date_weekday': date_weekday,
                'ls_num': ls_num,
                'no_xs_num': no_xs_num
            }
        }
        g_message_queue.put(message)
        log(f">>> 微信消息已加入队列: {name} {code}")


def send_email(stocks):
    """通过队列发送邮件通知"""
    global g_email_sent_stocks
    
    if not EMAIL_ENABLED:
        return
    
    new_stocks_to_send = []
    for item in stocks:
        stock = item['stock']
        if stock not in g_email_sent_stocks:
            new_stocks_to_send.append(item)
            g_email_sent_stocks.add(stock)
    
    if not new_stocks_to_send:
        log(">>> 所有股票已发送过邮件，跳过")
        return
    
    date_weekday = get_date_with_weekday()
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    for item in new_stocks_to_send:
        code = item['stock'].split('.')[0]
        name = g_stock_names.get(item['stock'], code)
        gain = f"{item['current_gain']:.2f}%"
        time_str = item['time']
        ls_num = item.get('ls_num', 0)
        no_xs_num = item.get('no_xs_num', 0)
        
        # 将消息放入队列
        message = {
            'type': 'email',
            'data': {
                'code': code,
                'name': name,
                'gain': gain,
                'time': time_str,
                'date_weekday': date_weekday,
                'current_time': current_time,
                'ls_num': ls_num,
                'no_xs_num': no_xs_num
            }
        }
        g_message_queue.put(message)
        log(f">>> 邮件消息已加入队列: {name} {code}")


def write_stocks_to_file(stocks, date_str, time_str):
    """写入文件，文件名格式：YYYYMMDD阴线都向上.txt"""
    import os
    
    if not WRITE_FILE_ENABLED:
        log(">>> 写入文件已关闭（测试模式），跳过写入")
        return
    
    # 确保输出目录存在
    if not os.path.exists(OUTPUT_DIR):
        try:
            os.makedirs(OUTPUT_DIR)
            log(f">>> 创建输出目录: {OUTPUT_DIR}")
        except Exception as e:
            log(f">>> 创建输出目录失败: {e}", "ERROR")
            return
    
    # 生成文件名：YYYYMMDD阴线都向上.txt
    file_date = date_str.replace('-', '')  # 20260113
    filename = f"{file_date}阴线都向上.txt"
    output_file = os.path.join(OUTPUT_DIR, filename)
    
    try:
        with open(output_file, 'a', encoding='gbk') as f:
            for item in stocks:
                code = item['stock'].split('.')[0]
                stock_name = g_stock_names.get(item['stock'], code)
                price = item['current_price']
                gain = item['current_gain']
                volume = item.get('volume', 0)
                ls_num = item.get('ls_num', 0)
                no_xs_num = item.get('no_xs_num', 0)
                market_cap = item.get('market_cap', 0)
                
                # 格式：代码\t名称\t日期 时间\t价格\t涨幅%\t成交量\t拉升次数\t不下杀次数\t市值\t策略名称
                line = f"{code}\t{stock_name}\t{date_str} {time_str}\t{price:.2f}\t{gain:.2f}%\t{volume}\t{ls_num}次\t{no_xs_num}次\t{market_cap}亿\t{STRATEGY_NAME}\n"
                f.write(line)
        log(f">>> 已写入文件: {output_file}")
    except Exception as e:
        log(f">>> 写入文件失败 [{output_file}]: {e}", "ERROR")


def save_stocks_to_redis(stocks, date_str):
    """保存股票数据到Redis"""
    if not REDIS_ENABLED:
        log_redis("Redis存储已关闭，跳过", "WARN")
        return
    
    if g_redis_client is None:
        log_redis("Redis客户端未初始化，跳过存储", "ERROR")
        return
    
    log_redis("\n" + "=" * 60)
    log_redis(f"开始保存股票到Redis: {date_str}")
    log_redis(f"共 {len(stocks)} 只股票")
    log_redis("=" * 60)
    
    for idx, item in enumerate(stocks, 1):
        code = item['stock'].split('.')[0]
        name = g_stock_names.get(item['stock'], code)
        
        stock_data = {
            "code": code,
            "name": name,
            "strategies": [STRATEGY_NAME],
            "today_change": item['current_gain'],
            "ls_num": item.get('ls_num', 0),
            "no_xs_num": item.get('no_xs_num', 0),
            "market_cap": item.get('market_cap', 0),
            "warning_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        log_redis(f"\n[{idx}/{len(stocks)}] 准备存储: {name} ({code})")
        
        # 将消息放入队列
        message = {
            'type': 'redis',
            'data': {
                'date_str': date_str,
                'stock_data': stock_data
            }
        }
        g_message_queue.put(message)
        log_redis(f"  ✓ Redis消息已加入队列")
    
    log_redis("\n" + "=" * 60)
    log_redis(f"所有股票已加入Redis存储队列")
    log_redis("=" * 60 + "\n")


def process_results(qualified, current_time, ContextInfo):
    """处理扫描结果"""
    global g_qualified_stocks, g_all_found_stocks
    
    current_time_str = current_time.strftime('%H:%M:%S')
    date_str = current_time.strftime('%Y-%m-%d')
    time_str = current_time.strftime('%H:%M')
    
    new_stocks = []
    for item in qualified:
        stock = item['stock']
        if stock not in g_all_found_stocks:
            g_all_found_stocks[stock] = current_time_str
            new_stocks.append(item)
    
    # 新增股票处理
    if new_stocks:
        push_to_wechat(new_stocks)  # 推送微信（队列）
        send_email(new_stocks)  # 发邮件（队列）
        write_stocks_to_file(new_stocks, date_str, time_str)  # 写文件
        save_stocks_to_redis(new_stocks, date_str)  # 存Redis（队列）
    
    # 打印结果
    if qualified:
        log("")
        log("*" * 70)
        log(f"★★★ 本轮发现 {len(qualified)} 只符合条件的股票 ★★★")
        log("*" * 70)
        
        qualified.sort(key=lambda x: x['current_gain'], reverse=True)
        
        for idx, item in enumerate(qualified, 1):
            code = item['stock'].split('.')[0]
            name = g_stock_names.get(item['stock'], code)
            log(f"【{idx}】{code} {name}")
            log(f"    涨幅: {item['current_gain']}% | 拉升: {item['ls_num']}次 | 阴线: {item['yinxian_count']}天 | 不下杀: {item['no_xs_num']}次 | 市值: {item['market_cap']}亿")
        
        g_qualified_stocks = {item['stock']: item for item in qualified}
    else:
        log("")
        log("本轮扫描未发现符合条件的股票")
    
    if new_stocks:
        log("")
        log("+" * 70)
        log(f"【新增】本轮新发现 {len(new_stocks)} 只股票")
        log("+" * 70)
    
    log("")
    log(f"【累计】今日共发现 {len(g_all_found_stocks)} 只符合条件的股票")


def is_trading_time(dt):
    t = dt.time()
    return (datetime.strptime("09:30", "%H:%M").time() <= t <= datetime.strptime("11:30", "%H:%M").time()) or \
           (datetime.strptime("13:00", "%H:%M").time() <= t <= datetime.strptime("15:00", "%H:%M").time())


def clear_cache(ContextInfo):
    global g_history_data, g_prev_close_cache, g_qualified_stocks, g_scan_count, g_all_found_stocks, g_stock_names, g_email_sent_stocks
    g_history_data = {}
    g_prev_close_cache = {}
    g_qualified_stocks = {}
    g_all_found_stocks = {}
    g_stock_names = {}
    g_email_sent_stocks = set()
    g_scan_count = 0
    log("缓存已清理")


def manual_scan(ContextInfo):
    log(">>> 手动触发扫描")
    scan_stocks(ContextInfo)
