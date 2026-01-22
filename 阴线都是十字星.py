# -*- coding: gbk -*-
"""
QMT阴线都是十字星策略
策略逻辑：
基础条件（所有分组都需要满足）：
1. 最近10个交易日（包括今天参考日）内最高涨幅≥6.85%次数大于等于4（拉升次数）
2. 10天内（包括今天）阴线的次数<=3或者没有阴线
3. 10天内如果出现阴线则满足十字星
4. 计算不下杀次数

分组1（优先级高）：
- 满足基础条件
- 如果出现阴线，所有阴线的最低价大于等于5日均价
- 参考日期（今日）最高涨幅≥5%
- 策略名称：十字并不碰（有阴线）或 全阳（无阴线）

分组2（优先级低）：
- 满足基础条件
- 不满足分组1
- 参考日期（今日）最高涨幅≥6.85%
- 策略名称：阴线都是十字星

注意：10天计算包括今日，今日数据盘中实时变化，可能导致股票动态进出筛选池
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
g_ma5_cache = {}  # 5日均价缓存

# 配置参数
DEBUG_MODE = True
CHECK_TRADING_TIME = True
PRINT_PROGRESS = True
LOG_ENABLED = False
REDIS_LOG_ENABLED = True
LOG_FILE = r"C:\Users\lyy\Desktop\股票\预警\阴线都是十字星.log"

# ========== 写入文件开关 ==========
WRITE_FILE_ENABLED = True

# 输出文件路径
OUTPUT_DIR = r"C:\Users\lyy\Desktop\股票\预警"
STRATEGY_NAME = "阴线都是十字星"

# ========== QQ邮箱配置 ==========
EMAIL_ENABLED = True
EMAIL_RECEIVERS = ["4563599@qq.com"]
EMAIL_SENDER = "4563599@qq.com"
EMAIL_AUTH_CODE = "iruvzesxztercaic"
EMAIL_SMTP_SERVER = "smtp.qq.com"
EMAIL_SMTP_PORT = 465

# ========== WxPusher微信推送配置 ==========
WXPUSHER_ENABLED = True
WXP_TOKEN = "AT_mUKiCaJyUxhf0V1K9X3MuhqDaLmguFY4"
WXP_UID = "UID_d5iiHewR5VxqIWV3t2vkumOkykre"
WXP_SUMMARY = "【阴线都是十字星】"

# ========== Redis配置 ==========
REDIS_ENABLED = True
REDIS_HOST = "175.24.178.113"
REDIS_PORT = 5268
REDIS_PASSWORD = "lyy654321@REDIS"
REDIS_DB = 0

g_email_sent_stocks = set()
g_redis_client = None

# 2个独立的消息队列
g_wechat_queue = queue.Queue()
g_email_queue = queue.Queue()


def log(msg, level="INFO"):
    if not LOG_ENABLED:
        return
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {msg}")


def log_redis(msg, level="INFO"):
    """Redis专用日志"""
    if not REDIS_LOG_ENABLED:
        return
    
    import os
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_msg = f"[{timestamp}] [{level}] {msg}"
    
    print(log_msg)
    
    try:
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
        log_redis("❌ redis模块未安装", "ERROR")
        return None
    
    log_redis(f"Redis模块已安装")
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
        
        info = r.info()
        log_redis("✅ Redis连接成功！")
        log_redis(f"Redis版本: {info.get('redis_version', 'N/A')}")
        log_redis(f"已用内存: {info.get('used_memory_human', 'N/A')}")
        log_redis("=" * 60)
        
        return r
    except Exception as e:
        log_redis(f"❌ Redis连接失败: {e}", "ERROR")
        return None


def is_cross_star(o, c):
    """判断是否为十字星：收盘价和开盘价幅度 ≤ 1.68%"""
    if o <= 0:
        return False
    return abs(c - o) / o * 100 <= 1.68


def check_no_kill(o, c, l):
    """判断是否满足不下杀条件"""
    # 条件1：十字星
    if is_cross_star(o, c):
        return True
    
    # 条件2：下引线 < K线实体 或 没有下引线
    lower_shadow = min(o, c) - l
    body = abs(c - o)
    
    if lower_shadow <= 0:
        return True
    
    if lower_shadow < body:
        return True
    
    return False


def calc_max_gain(h, prev_c):
    """计算最高涨幅"""
    if prev_c <= 0:
        return 0
    return (h - prev_c) / prev_c * 100


def calc_ma5(closes, index):
    """计算指定位置的5日均价"""
    if index < 4:
        return 0
    return np.mean(closes[index-4:index+1])


def get_market_cap(tick, stock):
    """从实时行情tick中获取市值（亿元）"""
    try:
        market_cap = tick.get('circulatingMarketCap', 0)
        if market_cap > 0:
            return round(market_cap / 100000000, 2)
        
        total_cap = tick.get('totalMarketCap', 0)
        if total_cap > 0:
            return round(total_cap / 100000000, 2)
        
        circulating_shares = tick.get('circulatingShares', 0)
        price = tick.get('lastPrice', 0)
        if circulating_shares > 0 and price > 0:
            return round(circulating_shares * price / 100000000, 2)
        
        return 0.0
    except Exception as e:
        log_redis(f"获取市值失败 [{stock}]: {e}", "WARN")
        return 0.0


def wechat_worker():
    """微信推送工作线程"""
    while True:
        try:
            message = g_wechat_queue.get(timeout=1)
            if message is None:
                break
            
            send_wechat_message(message)
            time.sleep(0.6)
            
            g_wechat_queue.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            log(f">>> 微信推送线程异常: {e}", "ERROR")


def email_worker():
    """邮件发送工作线程"""
    while True:
        try:
            message = g_email_queue.get(timeout=1)
            if message is None:
                break
            
            send_email_message(message)
            time.sleep(0.5)
            
            g_email_queue.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            log(f">>> 邮件发送线程异常: {e}", "ERROR")


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
        strategy = data.get('strategy', STRATEGY_NAME)
        
        msg_content = f"{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n拉升次数：{ls_num}\n不下杀次数：{no_xs_num}\n策略：{strategy}\n时间：{time_str} {date_weekday}"
        summary = f"{strategy} {name} {gain}"
        
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
        strategy = data.get('strategy', STRATEGY_NAME)
        
        subject = f"{strategy} {name} {time_str} {date_weekday}"
        body = f"发现时间：{current_time}\n\n{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n拉升次数：{ls_num}\n不下杀次数：{no_xs_num}\n策略：{strategy}\n时间：{time_str} {date_weekday}"
        
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



def save_single_stock_to_redis(stock_item, date_str, is_last=False):
    """单只股票存储到Redis（独立线程执行）"""
    if not REDIS_ENABLED or g_redis_client is None:
        return
    
    try:
        code = stock_item['stock'].split('.')[0]
        name = g_stock_names.get(stock_item['stock'], code)
        strategy = stock_item.get('strategy', STRATEGY_NAME)
        
        # 判断分组
        if strategy in ["全阳", "十字并不碰"]:
            group = "分组1"
        elif strategy == "阴线都是十字星":
            group = "分组2"
        else:
            group = "未知"
        
        stock_data = {
            "code": code,
            "name": name,
            "strategies": [strategy],
            "group": group,  # 新增：分组标识
            "today_change": stock_item['current_gain'],
            "ls_num": stock_item.get('ls_num', 0),
            "no_xs_num": stock_item.get('no_xs_num', 0),
            "market_cap": stock_item.get('market_cap', 0),
            "warning_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        key = f"stocks_yxdszx:{date_str}"  # 阴线都是十字星
        timestamp_key = f"{key}:timestamp"
        ttl = 30 * 24 * 60 * 60
        
        json_str = json.dumps(stock_data, ensure_ascii=False)
        value = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
        
        log_redis(f"[并发存储] 开始: {name} ({code}) - {strategy}")
        
        result = g_redis_client.hset(key, code, value)
        g_redis_client.expire(key, ttl)
        
        log_redis(f"[并发存储] HSET结果: {name} ({code}) - {'新增' if result == 1 else '更新'}")
        
        stored_value = g_redis_client.hget(key, code)
        if stored_value:
            try:
                decoded_json = base64.b64decode(stored_value.encode('utf-8')).decode('utf-8')
                decoded_data = json.loads(decoded_json)
                log_redis(f"✅ [并发存储] 成功: {name} ({code}) - {decoded_data['strategies'][0]}")
            except Exception as e:
                log_redis(f"⚠️  [并发存储] 解码失败: {name} ({code}) - {e}", "WARN")
        else:
            log_redis(f"⚠️  [并发存储] 验证失败: {name} ({code})", "WARN")
        
        if is_last:
            import time
            timestamp = int(time.time() * 1000)
            g_redis_client.set(timestamp_key, timestamp)
            g_redis_client.expire(timestamp_key, ttl)
            log_redis(f"✓✓✓ [并发存储] 更新时间戳: {timestamp} ✓✓✓")
        
    except redis.RedisError as e:
        log_redis(f"❌ [并发存储] Redis错误: {name} ({code}) - {e}", "ERROR")
    except Exception as e:
        log_redis(f"❌ [并发存储] 未知错误: {name} ({code}) - {e}", "ERROR")


def save_stocks_to_redis(stocks, date_str):
    """并发保存股票数据到Redis"""
    if not REDIS_ENABLED:
        log_redis("Redis存储已关闭，跳过", "WARN")
        return
    
    if g_redis_client is None:
        log_redis("Redis客户端未初始化，跳过存储", "ERROR")
        return
    
    log_redis("\n" + "=" * 60)
    log_redis(f"开始并发保存股票到Redis: {date_str}")
    log_redis(f"共 {len(stocks)} 只股票（每只独立线程，立即执行）")
    log_redis("=" * 60)
    
    threads = []
    total = len(stocks)
    
    for idx, item in enumerate(stocks, 1):
        is_last = (idx == total)
        
        thread = threading.Thread(
            target=save_single_stock_to_redis,
            args=(item, date_str, is_last),
            daemon=True
        )
        thread.start()
        threads.append(thread)
        
        code = item['stock'].split('.')[0]
        name = g_stock_names.get(item['stock'], code)
        strategy = item.get('strategy', STRATEGY_NAME)
        log_redis(f"[{idx}/{total}] 启动存储线程: {name} ({code}) - {strategy}")
    
    log_redis("\n" + "=" * 60)
    log_redis(f"✅ 所有 {total} 只股票的存储线程已启动（并发执行）")
    log_redis("=" * 60 + "\n")


def delete_stock_from_redis(stock_code, stock_name, date_str, max_retries=3):
    """从Redis中删除单只股票（带重试机制）"""
    if not REDIS_ENABLED or g_redis_client is None:
        return False
    
    file_date = date_str.replace('-', '')
    key = f"yxdszx:{file_date}"
    
    for retry in range(max_retries):
        try:
            result = g_redis_client.hdel(key, stock_code)
            
            if result > 0:
                if retry > 0:
                    log_redis(f"  ✓ Redis删除成功(重试{retry}次): {stock_name}({stock_code})")
                else:
                    log_redis(f"  ✓ Redis删除: {stock_name}({stock_code})")
                return True
            else:
                # 字段不存在（可能已经被删除了）
                return True
            
        except Exception as e:
            if retry < max_retries - 1:
                log_redis(f"  ✗ Redis删除失败(第{retry+1}次尝试): {stock_name}({stock_code}) - {str(e)}", "WARN")
                try:
                    g_redis_client.ping()
                except:
                    log_redis(f"  尝试重新连接Redis...", "WARN")
            else:
                log_redis(f"  ✗ Redis删除失败(已重试{max_retries}次): {stock_name}({stock_code}) - {str(e)}", "ERROR")
                return False
    
    return False


def clear_today_files():
    """清除今天的输出文件和日志文件"""
    import os
    
    try:
        today = datetime.now().strftime('%Y%m%d')
        
        output_filename = f"{today}阴线都是十字星.txt"
        output_file = os.path.join(OUTPUT_DIR, output_filename)
        
        log_file = LOG_FILE
        
        files_to_clear = [output_file, log_file]
        
        for file_path in files_to_clear:
            if os.path.exists(file_path):
                try:
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
    
    clear_today_files()
    
    log("=" * 60)
    log("========== 阴线都是十字星策略初始化开始 ==========")
    log("=" * 60)
    
    if REDIS_ENABLED:
        log_redis("\n>>> 步骤1: 初始化Redis连接")
        g_redis_client = connect_redis()
        if g_redis_client:
            log_redis(">>> Redis初始化成功\n")
        else:
            log_redis(">>> Redis初始化失败\n", "ERROR")
    
    log(">>> 订阅全市场行情...")
    try:
        ContextInfo.subscribe_whole_quote(["SH", "SZ"])
        log(">>> 全市场行情订阅成功")
    except Exception as e:
        log(f">>> 全市场行情订阅失败: {e}", "WARN")
    
    g_stock_pool = get_stock_pool(ContextInfo)
    log(f"股票池加载完成，共 {len(g_stock_pool)} 只股票")
    
    log("")
    log(">>> 正在预加载股票名称...")
    preload_stock_names(ContextInfo)
    log(f">>> 股票名称加载完成: {len(g_stock_names)} 只")
    
    log("")
    log(">>> 正在预加载历史数据...")
    preload_history_data(ContextInfo)
    log(f">>> 历史数据预加载完成: {len(g_history_data)} 只")
    
    ContextInfo.run_time("scan_stocks", "3nSecond", "2024-01-01 09:25:15", "SH")
    log("定时器设置完成，每3秒扫描一次（从9:25:15开始）")
    
    wechat_thread = threading.Thread(target=wechat_worker, daemon=True)
    email_thread = threading.Thread(target=email_worker, daemon=True)
    
    wechat_thread.start()
    email_thread.start()
    
    log(">>> 微信推送线程已启动")
    log(">>> 邮件发送线程已启动")
    
    log("")
    log(f">>> 写入文件开关: {'开启' if WRITE_FILE_ENABLED else '关闭'}")
    log(f">>> 发送邮件开关: {'开启' if EMAIL_ENABLED else '关闭'}")
    log(f">>> Redis存储开关: {'开启' if REDIS_ENABLED else '关闭'}")
    
    g_initialized = True
    log("=" * 60)
    log("========== 阴线都是十字星策略初始化完成 ==========")
    log("=" * 60)
    log("")
    
    log(">>> 执行首次扫描...")
    scan_stocks(ContextInfo)


def get_stock_pool(ContextInfo):
    """获取股票池"""
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
    """获取星期"""
    weekdays = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
    now = datetime.now()
    return weekdays[now.weekday()]


def preload_history_data(ContextInfo):
    """预加载历史数据"""
    global g_history_data, g_prev_close_cache, g_ma5_cache
    
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
                            closes = stock_data['close'].values.astype(float)
                            g_history_data[stock] = {
                                'closes': closes,
                                'opens': stock_data['open'].values.astype(float),
                                'highs': stock_data['high'].values.astype(float),
                                'lows': stock_data['low'].values.astype(float)
                            }
                            g_prev_close_cache[stock] = float(closes[-2])
                            
                            # 预计算5日均价
                            ma5_list = []
                            for idx in range(len(closes)):
                                ma5_list.append(calc_ma5(closes, idx))
                            g_ma5_cache[stock] = ma5_list
                            
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
    log(f"############## 第 {g_scan_count} 轮扫描开始（阴线都是十字星） ##############")
    log("#" * 70)
    
    start_time = datetime.now()
    
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
    
    log("")
    log(">>> 步骤2: 极速过滤（阴线都是十字星版）")
    
    qualified = []
    stats = {
        'total': len(g_stock_pool),
        'no_tick': 0, 'no_prev': 0, 'no_hist': 0,
        'fail_ls_count': 0, 'fail_yinxian': 0, 'fail_cross': 0,
        'pass_group1': 0, 'pass_group2': 0,
        'removed': 0  # 新增：记录从Redis删除的股票数量
    }
    
    # 获取日期字符串（用于Redis删除）
    date_str = current_time.strftime('%Y-%m-%d')
    
    for stock in g_stock_pool:
        result = check_stock_yxdszx(stock, all_ticks, stats, ContextInfo)
        code = stock.split('.')[0]
        name = g_stock_names.get(stock, code)
        
        if result:
            # ✅ 满足条件
            qualified.append(result)
            strategy = result.get('strategy', STRATEGY_NAME)
            
            # 检查是否为新股票
            is_new_stock = stock not in g_all_found_stocks
            
            if is_new_stock:
                log(f"  ★ 新发现: {stock} {name} | 涨幅:{result['current_gain']}% | 拉升:{result['ls_num']}次 | 阴线:{result['yinxian_count']}天 | 策略:{strategy}", "SUCCESS")
            else:
                log(f"  ● 已存在: {stock} {name} | 涨幅:{result['current_gain']}% | 拉升:{result['ls_num']}次 | 阴线:{result['yinxian_count']}天 | 策略:{strategy}")
        else:
            # ❌ 不满足条件
            # 如果之前存在于 g_all_found_stocks，说明之前满足条件，现在不满足了，需要从Redis删除
            if stock in g_all_found_stocks:
                log(f"  ⚠ 不再满足条件，从Redis删除: {stock} {name}", "WARN")
                if delete_stock_from_redis(code, name, date_str, max_retries=3):
                    log(f"    → Redis已删除: {name}({code})")
                    # 从内存中移除
                    del g_all_found_stocks[stock]
                    stats['removed'] += 1
                else:
                    log(f"    → Redis删除失败: {name}({code})", "ERROR")
    
    total_elapsed = (datetime.now() - start_time).total_seconds()
    
    log("")
    log("=" * 60)
    log("扫描统计汇总（阴线都是十字星版）")
    log("=" * 60)
    log(f"总股票数: {stats['total']}")
    log(f"总扫描耗时: {total_elapsed:.2f} 秒")
    log("-" * 40)
    log(f"无行情数据: {stats['no_tick']}")
    log(f"无昨收价: {stats['no_prev']}")
    log(f"无历史数据: {stats['no_hist']}")
    log(f"第1道淘汰(拉升次数<4): {stats['fail_ls_count']}")
    log(f"第2道淘汰(阴线数量>3): {stats['fail_yinxian']}")
    log(f"第3道淘汰(阴线不是十字星): {stats['fail_cross']}")
    log(f"通过分组1(十字并不碰/全阳): {stats['pass_group1']}")
    log(f"通过分组2(阴线都是十字星): {stats['pass_group2']}")
    log(f"动态剔除(不再满足条件): {stats['removed']}")
    log("=" * 60)
    
    process_results(qualified, current_time, ContextInfo)
    
    log("")
    log("#" * 70)
    log(f"############## 第 {g_scan_count} 轮扫描结束 ##############")
    log("#" * 70)


def check_stock_yxdszx(stock, all_ticks, stats, ContextInfo):
    """
    检查单只股票 - 阴线都是十字星版
    基础条件：
    1. 最近10个交易日（包括今天）内最高涨幅≥6.85%次数大于等于4（拉升次数）
    2. 10天内（包括今天）阴线的次数<=3或者没有阴线
    3. 10天内如果出现阴线则满足十字星
    4. 计算不下杀次数
    
    分组1（优先）：
    - 满足基础条件
    - 如果出现阴线，所有阴线的最低价>=5日均价
    - 今日最高涨幅≥5%
    - 策略：十字并不碰（有阴线）或 全阳（无阴线）
    
    分组2：
    - 满足基础条件
    - 不满足分组1
    - 今日最高涨幅≥6.85%
    - 策略：阴线都是十字星
    """
    tick = all_ticks.get(stock)
    if not tick:
        stats['no_tick'] += 1
        return None
    
    prev_close = g_prev_close_cache.get(stock)
    if not prev_close or prev_close <= 0:
        stats['no_prev'] += 1
        return None
    
    # 获取今日实时数据
    today_high = tick.get('high', 0)
    today_low = tick.get('low', 0)
    today_open = tick.get('open', 0)
    current_price = tick.get('lastPrice', 0)
    if not current_price:
        current_price = tick.get('price', tick.get('last', 0))
    
    if not today_high or today_high <= 0:
        stats['no_tick'] += 1
        return None
    
    hist = g_history_data.get(stock)
    if not hist:
        stats['no_hist'] += 1
        return None
    
    closes = hist['closes']
    opens = hist['opens']
    highs = hist['highs']
    lows = hist['lows']
    
    if len(closes) < 10:
        stats['no_hist'] += 1
        return None
    
    # 获取5日均价
    ma5_list = g_ma5_cache.get(stock, [])
    if len(ma5_list) < len(closes):
        stats['no_hist'] += 1
        return None
    
    # 今日最高涨幅
    today_max_gain = calc_max_gain(today_high, prev_close)
    
    # ========== 第1道: 最近10个交易日（包括今天）内最高涨幅≥6.85%次数（拉升次数） ==========
    # 历史9天 + 今天 = 10天
    ls_num = 0
    # 历史9天
    for i in range(len(closes) - 9, len(closes)):
        if i >= 1 and closes[i-1] > 0:
            max_gain = calc_max_gain(highs[i], closes[i-1])
            if max_gain >= 6.85:
                ls_num += 1
    # 今天（盘中动态）
    if today_max_gain >= 6.85:
        ls_num += 1
    
    if ls_num < 4:
        stats['fail_ls_count'] += 1
        return None
    
    # ========== 第2道: 10天内（包括今天）阴线的次数<=3 ==========
    yinxian_count = 0
    yin_indices = []  # 历史阴线索引
    is_today_yin = False  # 今天是否为阴线
    
    # 历史9天
    for i in range(len(closes) - 9, len(closes)):
        if closes[i] < opens[i]:
            yinxian_count += 1
            yin_indices.append(i)
    
    # 今天（盘中动态判断）
    if current_price < today_open:
        yinxian_count += 1
        is_today_yin = True
    
    if yinxian_count > 3:
        stats['fail_yinxian'] += 1
        return None
    
    # ========== 第3道: 10天内如果出现阴线则满足十字星 ==========
    if yinxian_count > 0:
        all_cross = True
        # 检查历史阴线
        for i in yin_indices:
            o = opens[i]
            c = closes[i]
            
            if not is_cross_star(o, c):
                all_cross = False
                break
        
        # 检查今天是否为阴线并且是否十字星
        if all_cross and is_today_yin:
            if not is_cross_star(today_open, current_price):
                all_cross = False
        
        if not all_cross:
            stats['fail_cross'] += 1
            return None
    
    # ========== 计算不下杀次数（10天内，包括今天） ==========
    no_xs_num = 0
    # 历史9天
    for i in range(len(closes) - 9, len(closes)):
        o = opens[i]
        c = closes[i]
        l = lows[i]
        
        if check_no_kill(o, c, l):
            no_xs_num += 1
    
    # 今天
    if check_no_kill(today_open, current_price, today_low):
        no_xs_num += 1
    
    # ========== 判断分组 ==========
    strategy = None
    
    # 计算今天的5日均价（需要用到最近5天的收盘价，包括今天的当前价）
    # 历史4天收盘价 + 今天当前价
    if len(closes) >= 4:
        ma5_today = (closes[-4] + closes[-3] + closes[-2] + closes[-1] + current_price) / 5
    else:
        ma5_today = 0
    
    # 分组1：如果出现阴线，所有阴线的最低价>=5日均价，且今日最高涨幅≥5%
    if yinxian_count == 0:
        # 全阳，需要今日最高涨幅≥5%
        if today_max_gain >= 5.0:
            strategy = "全阳"
            stats['pass_group1'] += 1
    elif yinxian_count > 0:
        # 检查所有阴线的最低价是否>=5日均价
        all_above_ma5 = True
        
        # 检查历史阴线
        for i in yin_indices:
            ma5 = ma5_list[i]
            if ma5 <= 0 or lows[i] < ma5:
                all_above_ma5 = False
                break
        
        # 检查今天如果是阴线
        if all_above_ma5 and is_today_yin:
            if ma5_today <= 0 or today_low < ma5_today:
                all_above_ma5 = False
        
        if all_above_ma5 and today_max_gain >= 5.0:
            # 十字并不碰，且今日最高涨幅≥5%
            strategy = "十字并不碰"
            stats['pass_group1'] += 1
    
    # 如果不满足分组1，检查分组2
    if strategy is None:
        # 分组2：今日最高涨幅≥6.85%
        if today_max_gain >= 6.85:
            strategy = "阴线都是十字星"
            stats['pass_group2'] += 1
        else:
            # 不满足任何分组
            return None
    
    if strategy is None:
        return None
    
    # 通过所有条件
    volume = tick.get('volume', tick.get('vol', 0))
    
    current_gain = (current_price - prev_close) / prev_close * 100 if current_price > 0 else today_max_gain
    
    market_cap = get_market_cap(tick, stock)
    
    return {
        'stock': stock,
        'today_max_gain': round(today_max_gain, 2),
        'current_gain': round(current_gain, 2),
        'ls_num': ls_num,
        'yinxian_count': yinxian_count,
        'no_xs_num': no_xs_num,
        'market_cap': market_cap,
        'current_price': current_price,
        'prev_close': round(prev_close, 2),
        'today_high': round(today_high, 2),
        'volume': volume,
        'time': datetime.now().strftime('%H:%M:%S'),
        'strategy': strategy
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
        strategy = item.get('strategy', STRATEGY_NAME)
        
        message = {
            'code': code,
            'name': name,
            'gain': gain,
            'time': time_str,
            'date_weekday': date_weekday,
            'ls_num': ls_num,
            'no_xs_num': no_xs_num,
            'strategy': strategy
        }
        g_wechat_queue.put(message)
        log(f">>> 微信消息已加入队列: {name} {code} - {strategy}")


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
        strategy = item.get('strategy', STRATEGY_NAME)
        
        message = {
            'code': code,
            'name': name,
            'gain': gain,
            'time': time_str,
            'date_weekday': date_weekday,
            'current_time': current_time,
            'ls_num': ls_num,
            'no_xs_num': no_xs_num,
            'strategy': strategy
        }
        g_email_queue.put(message)
        log(f">>> 邮件消息已加入队列: {name} {code} - {strategy}")


def write_stocks_to_file(stocks, date_str, time_str):
    """写入文件"""
    import os
    
    if not WRITE_FILE_ENABLED:
        log(">>> 写入文件已关闭（测试模式），跳过写入")
        return
    
    if not os.path.exists(OUTPUT_DIR):
        try:
            os.makedirs(OUTPUT_DIR)
            log(f">>> 创建输出目录: {OUTPUT_DIR}")
        except Exception as e:
            log(f">>> 创建输出目录失败: {e}", "ERROR")
            return
    
    file_date = date_str.replace('-', '')
    filename = f"{file_date}阴线都是十字星.txt"
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
                strategy = item.get('strategy', STRATEGY_NAME)
                
                line = f"{code}\t{stock_name}\t{date_str} {time_str}\t{price:.2f}\t{gain:.2f}%\t{volume}\t{ls_num}次\t{no_xs_num}次\t{market_cap}亿\t{strategy}\n"
                f.write(line)
        log(f">>> 已写入文件: {output_file}")
    except Exception as e:
        log(f">>> 写入文件失败 [{output_file}]: {e}", "ERROR")


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
    
    if new_stocks:
        write_stocks_to_file(new_stocks, date_str, time_str)
        save_stocks_to_redis(new_stocks, date_str)
        push_to_wechat(new_stocks)
        send_email(new_stocks)
    
    if qualified:
        log("")
        log("*" * 70)
        log(f"★★★ 本轮发现 {len(qualified)} 只符合条件的股票 ★★★")
        log("*" * 70)
        
        qualified.sort(key=lambda x: x['current_gain'], reverse=True)
        
        for idx, item in enumerate(qualified, 1):
            code = item['stock'].split('.')[0]
            name = g_stock_names.get(item['stock'], code)
            strategy = item.get('strategy', STRATEGY_NAME)
            log(f"【{idx}】{code} {name}")
            log(f"    涨幅: {item['current_gain']}% | 拉升: {item['ls_num']}次 | 阴线: {item['yinxian_count']}天 | 不下杀: {item['no_xs_num']}次 | 策略: {strategy}")
        
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
    """判断是否在交易时间内"""
    t = dt.time()
    morning_start = datetime.strptime("09:25:15", "%H:%M:%S").time()
    morning_end = datetime.strptime("11:30:00", "%H:%M:%S").time()
    afternoon_start = datetime.strptime("13:00:00", "%H:%M:%S").time()
    afternoon_end = datetime.strptime("15:00:00", "%H:%M:%S").time()
    
    return (morning_start <= t <= morning_end) or (afternoon_start <= t <= afternoon_end)


def clear_cache(ContextInfo):
    global g_history_data, g_prev_close_cache, g_qualified_stocks, g_scan_count, g_all_found_stocks, g_stock_names, g_email_sent_stocks, g_ma5_cache
    g_history_data = {}
    g_prev_close_cache = {}
    g_qualified_stocks = {}
    g_all_found_stocks = {}
    g_stock_names = {}
    g_email_sent_stocks = set()
    g_ma5_cache = {}
    g_scan_count = 0
    log("缓存已清理")


def manual_scan(ContextInfo):
    log(">>> 手动触发扫描")
    scan_stocks(ContextInfo)
