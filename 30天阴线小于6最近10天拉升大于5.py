# -*- coding: gbk -*-
"""
QMT极品策略 - 10天阴线≤3 + 10天5次拉升 + 20天阴线≤5或=6
策略逻辑：
1. 参考日期：今天是交易日则为今天，否则为最近一个交易日
2. 最近10个交易日的阴线次数 ≤ 3（必须满足）
3. 10天内 最高>昨收*1.065 的次数>=5
4. 第一组 最近20个交易日的阴线次数 ≤ 5（必须满足） 
   第二组 最近20个交易日的阴线次数=6，并且参考日期最高涨幅>6.5%
5. 如果最近20个交易日内有停牌日期，则停牌日期不算，要继续获取数据，保证要有20个交易日的数据
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
    import json
    REDIS_ENABLED = True
except:
    redis = None
    REDIS_ENABLED = False

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

# Redis连接
g_redis_client = None

# 配置参数
DEBUG_MODE = True
CHECK_TRADING_TIME = True
PRINT_PROGRESS = True
LOG_ENABLED = True

# ========== Redis配置 ==========
REDIS_ENABLED = True
REDIS_HOST = "175.24.178.113"
REDIS_PORT = 5268
REDIS_PASSWORD = "lyy654321@REDIS"
REDIS_DB = 0
REDIS_KEY_PREFIX = "5ls20"  # Redis key前缀

# ========== 参考日期设置 ==========
# 设置参考日期，格式：20260105，为空则使用今天
REFERENCE_DATE = ""  # 留空使用今天，或设置如 "20260105"

# 输出文件路径
OUTPUT_DIR = r"C:\Users\lyy\Desktop\股票\预警"
STRATEGY_NAME = "10天阴线≤3+5次拉升+20天阴线≤5或=6"

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
WXP_SUMMARY = "【10天阴线≤3+5次拉升+20天阴线≤5或=6】"

g_email_sent_stocks = set()


def log(msg, level="INFO"):
    if not LOG_ENABLED:
        return
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {msg}")


def init_redis():
    """初始化Redis连接"""
    global g_redis_client
    
    if not REDIS_ENABLED:
        log(">>> Redis模块未安装，跳过Redis初始化", "WARN")
        return False
    
    try:
        if REDIS_PASSWORD:
            g_redis_client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                password=REDIS_PASSWORD,
                decode_responses=True
            )
        else:
            g_redis_client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                decode_responses=True
            )
        
        # 测试连接
        g_redis_client.ping()
        log(f">>> Redis连接成功: {REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}")
        return True
    except Exception as e:
        log(f">>> Redis连接失败: {e}", "ERROR")
        g_redis_client = None
        return False


def save_to_redis(stocks, date_str):
    """保存数据到Redis，按日期存储
    Redis Key格式: 5ls20:YYYYMMDD
    """
    if not g_redis_client:
        log(">>> Redis未连接，跳过保存", "WARN")
        return
    
    try:
        # 生成Redis key
        file_date = date_str.replace('-', '')  # 20260121
        redis_key = f"{REDIS_KEY_PREFIX}:{file_date}"
        
        # 准备数据
        stock_data_list = []
        for item in stocks:
            code = item['stock'].split('.')[0]
            stock_name = g_stock_names.get(item['stock'], code)
            
            stock_info = {
                'code': code,
                'name': stock_name,
                'stock': item['stock'],
                'current_gain': round(item['current_gain'], 2),
                'today_max_gain': round(item['today_max_gain'], 2),
                'current_price': round(item['current_price'], 2),
                'prev_close': round(item['prev_close'], 2),
                'volume': item.get('volume', 0),
                'yin_count_10': item['yin_count_10'],
                'pull_count_10': item['pull_count_10'],
                'yin_count_20': item['yin_count_20'],
                'group': item['group'],
                'time': item['time'],
                'date': date_str
            }
            stock_data_list.append(stock_info)
        
        # 将数据转为JSON字符串存入Redis
        json_data = json.dumps(stock_data_list, ensure_ascii=False)
        g_redis_client.set(redis_key, json_data)
        
        # 设置过期时间（30天）
        g_redis_client.expire(redis_key, 30 * 24 * 60 * 60)
        
        log(f">>> 已保存到Redis: {redis_key} ({len(stock_data_list)}只股票)")
        
    except Exception as e:
        log(f">>> 保存到Redis失败: {e}", "ERROR")


def get_from_redis(date_str):
    """从Redis读取数据"""
    if not g_redis_client:
        log(">>> Redis未连接，无法读取", "WARN")
        return None
    
    try:
        file_date = date_str.replace('-', '')
        redis_key = f"{REDIS_KEY_PREFIX}:{file_date}"
        
        json_data = g_redis_client.get(redis_key)
        if json_data:
            stock_data = json.loads(json_data)
            log(f">>> 从Redis读取成功: {redis_key} ({len(stock_data)}只股票)")
            return stock_data
        else:
            log(f">>> Redis中无数据: {redis_key}", "WARN")
            return None
    except Exception as e:
        log(f">>> 从Redis读取失败: {e}", "ERROR")
        return None


def init(ContextInfo):
    global g_stock_pool, g_initialized, g_context
    
    g_context = ContextInfo
    log("=" * 60)
    log("========== 策略初始化开始 ==========")
    log("=" * 60)
    
    # 初始化Redis
    log("")
    log(">>> 初始化Redis连接...")
    init_redis()
    
    # 订阅全市场行情
    log("")
    log(">>> 订阅全市场行情...")
    try:
        ContextInfo.subscribe_whole_quote(["SH", "SZ"])
        log(">>> 全市场行情订阅成功 (SH, SZ)")
    except Exception as e:
        log(f">>> 全市场行情订阅失败: {e}", "WARN")
    
    # 获取股票池
    g_stock_pool = get_stock_pool(ContextInfo)
    log(f"股票池加载完成，共 {len(g_stock_pool)} 只股票")
    
    # 预加载股票名称
    log("")
    log(">>> 正在预加载股票名称...")
    preload_stock_names(ContextInfo)
    log(f">>> 股票名称加载完成: {len(g_stock_names)} 只")
    
    # 预加载历史数据（需要至少30天数据，考虑停牌情况多取几天）
    log("")
    log(">>> 正在预加载历史数据...")
    preload_history_data(ContextInfo)
    log(f">>> 历史数据预加载完成: {len(g_history_data)} 只")
    
    ContextInfo.run_time("scan_stocks", "3nSecond", "2024-01-01 09:30:00", "SH")
    log("定时器设置完成，每3秒扫描一次")
    
    g_initialized = True
    log("=" * 60)
    log("========== 策略初始化完成 ==========")
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
    
    excluded_st = 0
    for stock in all_stocks:
        if 'ST' in stock.upper():
            excluded_st += 1
            continue
        stock_list.append(stock)
    
    log(f"排除ST股票: {excluded_st} 只")
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
    """预加载历史数据（需要至少30天数据，考虑停牌多取40天）"""
    global g_history_data, g_prev_close_cache
    
    batch_size = 300
    total = len(g_stock_pool)
    success_count = 0
    
    # 如果设置了参考日期，使用指定日期；否则使用今天
    if REFERENCE_DATE:
        log(f">>> 使用参考日期: {REFERENCE_DATE}")
        end_date = REFERENCE_DATE
    else:
        log(f">>> 使用今天作为参考日期")
        end_date = ""
    
    for i in range(0, total, batch_size):
        batch = g_stock_pool[i:i+batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size
        
        try:
            # 获取40天数据，以应对停牌情况
            if end_date:
                data = ContextInfo.get_market_data(
                    ['open', 'high', 'low', 'close', 'volume'], 
                    batch, 
                    period='1d', 
                    count=40,
                    end_time=end_date
                )
            else:
                data = ContextInfo.get_market_data(
                    ['open', 'high', 'low', 'close', 'volume'], 
                    batch, 
                    period='1d', 
                    count=40
                )
            
            if data is not None:
                for stock in batch:
                    try:
                        stock_data = extract_stock_data(data, stock)
                        if stock_data is not None and len(stock_data) >= 21:
                            g_history_data[stock] = {
                                'closes': stock_data['close'].values.astype(float),
                                'opens': stock_data['open'].values.astype(float),
                                'highs': stock_data['high'].values.astype(float),
                                'lows': stock_data['low'].values.astype(float),
                                'volumes': stock_data['volume'].values.astype(float)
                            }
                            g_prev_close_cache[stock] = float(stock_data['close'].iloc[-2])
                            success_count += 1
                    except Exception as e:
                        pass
        except Exception as e:
            log(f"  批次{batch_num}获取历史数据异常: {str(e)}", "ERROR")
        
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


def is_suspended(volume):
    """判断是否停牌（成交量为0）"""
    return volume == 0


def get_valid_trading_days(closes, opens, highs, lows, volumes, need_days):
    """
    获取有效交易日数据（排除停牌日）
    返回：有效交易日的数据列表
    """
    valid_days = []
    
    # 从最新的一天开始往前查找（跳过索引-1，因为那是参考日）
    for i in range(len(closes) - 2, -1, -1):
        # 检查是否停牌
        if is_suspended(volumes[i]):
            continue
        
        valid_days.append({
            'idx': i,
            'open': opens[i],
            'high': highs[i],
            'low': lows[i],
            'close': closes[i],
            'volume': volumes[i],
            'prev_close': closes[i-1] if i > 0 else 0
        })
        
        if len(valid_days) >= need_days:
            break
    
    return valid_days


def calc_max_gain(h, prev_c):
    """
    计算最高涨幅
    最高涨幅 = (今日最高点价格 - 昨日收盘价) ÷ 昨日收盘价 × 100%
    """
    if prev_c <= 0:
        return 0
    return (h - prev_c) / prev_c * 100


def check_stock(stock, all_ticks, stats):
    """检查单只股票"""
    tick = all_ticks.get(stock)
    if not tick:
        stats['no_tick'] += 1
        return None
    
    prev_close = g_prev_close_cache.get(stock)
    if not prev_close or prev_close <= 0:
        stats['no_prev'] += 1
        return None
    
    hist = g_history_data.get(stock)
    if not hist:
        stats['no_hist'] += 1
        return None
    
    closes = hist['closes']
    opens = hist['opens']
    highs = hist['highs']
    lows = hist['lows']
    volumes = hist['volumes']
    
    if len(closes) < 21:
        stats['no_hist'] += 1
        return None
    
    # 获取今日数据（参考日）
    current_price = tick.get('lastPrice', 0)
    if not current_price:
        current_price = tick.get('price', tick.get('last', 0))
    
    current_high = tick.get('high', 0)
    current_low = tick.get('low', 0)
    current_open = tick.get('open', 0)
    current_volume = tick.get('volume', tick.get('vol', 0))
    
    if current_price <= 0 or current_high <= 0:
        stats['no_tick'] += 1
        return None
    
    # 参考日最高涨幅
    ref_max_gain = calc_max_gain(current_high, prev_close)
    
    # ========== 获取有效的10个交易日（排除停牌） ==========
    valid_days_10 = get_valid_trading_days(closes, opens, highs, lows, volumes, 10)
    
    if len(valid_days_10) < 10:
        stats['no_enough_data'] += 1
        return None
    
    # ========== 条件2：最近10个交易日的阴线次数 ≤ 3 ==========
    yin_count_10 = 0
    for day in valid_days_10:
        if day['close'] < day['open']:  # 阴线
            yin_count_10 += 1
    
    if yin_count_10 > 3:
        stats['fail_yin_10'] += 1
        return None
    
    # ========== 条件3：10天内 最高>昨收*1.065 的次数>=5 ==========
    pull_count_10 = 0
    for day in valid_days_10:
        if day['prev_close'] > 0:
            if day['high'] > day['prev_close'] * 1.065:
                pull_count_10 += 1
    
    if pull_count_10 < 5:
        stats['fail_pull_10'] += 1
        return None
    
    # ========== 获取有效的20个交易日（排除停牌） ==========
    valid_days_20 = get_valid_trading_days(closes, opens, highs, lows, volumes, 20)
    
    if len(valid_days_20) < 20:
        stats['no_enough_data'] += 1
        return None
    
    # ========== 条件4：20天阴线判断 ==========
    yin_count_20 = 0
    for day in valid_days_20:
        if day['close'] < day['open']:  # 阴线
            yin_count_20 += 1
    
    # 判断属于哪个组
    group = None
    
    # 第一组：最近20个交易日的阴线次数 ≤ 5
    if yin_count_20 <= 5:
        group = "第一组(20天阴线≤5)"
    # 第二组：最近20个交易日的阴线次数=6，并且参考日期最高涨幅>6.5%
    elif yin_count_20 == 6 and ref_max_gain > 6.5:
        group = "第二组(20天阴线=6且参考日涨幅>6.5%)"
    else:
        stats['fail_yin_20'] += 1
        return None
    
    # 通过所有条件
    stats['pass'] += 1
    
    # 计算涨幅
    gain = (current_price - prev_close) / prev_close * 100
    
    return {
        'stock': stock,
        'current_gain': round(gain, 2),
        'today_max_gain': round(ref_max_gain, 2),
        'current_price': current_price,
        'prev_close': round(prev_close, 2),
        'volume': current_volume,
        'yin_count_10': yin_count_10,
        'pull_count_10': pull_count_10,
        'yin_count_20': yin_count_20,
        'group': group,
        'time': datetime.now().strftime('%H:%M:%S')
    }


def push_to_wechat(stocks):
    """通过 WxPusher 推送微信消息"""
    import time
    
    log(f">>> 准备微信推送，共 {len(stocks)} 只股票")
    
    if not WXPUSHER_ENABLED:
        log(">>> 微信推送已关闭，跳过")
        return
    
    if requests is None:
        log(">>> requests模块未安装，无法推送微信", "ERROR")
        return
    
    date_weekday = get_date_with_weekday()
    
    for idx, item in enumerate(stocks, 1):
        code = item['stock'].split('.')[0]
        name = g_stock_names.get(item['stock'], code)
        gain = f"{item['current_gain']:.2f}%"
        max_gain = f"{item['today_max_gain']:.2f}%"
        time_str = item['time']
        
        msg_content = f"{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n最高涨幅：{max_gain}\n10天阴线：{item['yin_count_10']}\n10天拉升：{item['pull_count_10']}次\n20天阴线：{item['yin_count_20']}\n分组：{item['group']}\n时间：{time_str} {date_weekday}"
        summary = f"{name} {gain}"
        
        log(f">>> 推送内容 [{idx}/{len(stocks)}]: {msg_content}")
        
        url = "https://wxpusher.zjiecode.com/api/send/message"
        payload = {
            "appToken": WXP_TOKEN,
            "content": msg_content,
            "summary": summary,
            "contentType": 1,
            "uids": [WXP_UID]
        }
        
        try:
            response = requests.post(url, json=payload, timeout=10)
            res = response.json()
            if res.get('code') == 1000:
                log(f">>> 微信推送成功 [{idx}/{len(stocks)}]: {name} {code}")
            else:
                log(f">>> 微信推送失败 [{idx}/{len(stocks)}]: {res.get('msg')}", "WARN")
        except Exception as e:
            log(f">>> 微信推送异常 [{idx}/{len(stocks)}]: {e}", "ERROR")
        
        # 限制QPS
        if idx < len(stocks):
            time.sleep(0.6)
            log(f">>> 等待0.6秒后继续推送...")


def send_email(stocks):
    """发送邮件通知"""
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
        max_gain = f"{item['today_max_gain']:.2f}%"
        time_str = item['time']
        
        subject = f"{name} {gain}"
        body = f"发现时间：{current_time}\n\n{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n最高涨幅：{max_gain}\n10天阴线：{item['yin_count_10']}\n10天拉升：{item['pull_count_10']}次\n20天阴线：{item['yin_count_20']}\n分组：{item['group']}\n时间：{time_str} {date_weekday}"
        
        try:
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
            log(f">>> 邮件发送失败 [{name} {code}]: {e}", "ERROR")


def write_stocks_to_file(stocks, date_str):
    """写入文件"""
    import os
    
    # 确保输出目录存在
    if not os.path.exists(OUTPUT_DIR):
        try:
            os.makedirs(OUTPUT_DIR)
            log(f">>> 创建输出目录: {OUTPUT_DIR}")
        except Exception as e:
            log(f">>> 创建输出目录失败: {e}", "ERROR")
            return
    
    # 生成文件名
    file_date = date_str.replace('-', '')
    filename = f"{file_date}_{REDIS_KEY_PREFIX}.txt"
    output_file = os.path.join(OUTPUT_DIR, filename)
    
    try:
        with open(output_file, 'a', encoding='gbk') as f:
            for item in stocks:
                code = item['stock'].split('.')[0]
                stock_name = g_stock_names.get(item['stock'], code)
                price = item['current_price']
                gain = item['current_gain']
                max_gain = item['today_max_gain']
                volume = item.get('volume', 0)
                
                # 格式：代码\t名称\t日期 时间\t价格\t涨幅%\t最高涨幅%\t成交量\t10天阴线\t10天拉升\t20天阴线\t分组\t策略名称
                line = f"{code}\t{stock_name}\t{date_str} {item['time']}\t{price:.2f}\t{gain:.2f}%\t{max_gain:.2f}%\t{volume}\t{item['yin_count_10']}阴\t{item['pull_count_10']}拉\t{item['yin_count_20']}阴\t{item['group']}\t{STRATEGY_NAME}\n"
                f.write(line)
        log(f">>> 已写入文件: {output_file}")
    except Exception as e:
        log(f">>> 写入文件失败 [{output_file}]: {e}", "ERROR")


def handlebar(ContextInfo):
    pass


def scan_stocks(ContextInfo):
    """扫描股票"""
    global g_qualified_stocks, g_scan_count
    
    try:
        current_time = datetime.now()
        
        # 如果设置了参考日期，跳过交易时间检查
        if not REFERENCE_DATE and CHECK_TRADING_TIME and not is_trading_time(current_time):
            return
        
        g_scan_count += 1
        log("")
        log("#" * 70)
        if REFERENCE_DATE:
            log(f"############## 历史选股 {REFERENCE_DATE} ##############")
        else:
            log(f"############## 第 {g_scan_count} 轮扫描开始 ##############")
        log("#" * 70)
        
        start_time = datetime.now()
        
        # 获取实时行情
        log("")
        if REFERENCE_DATE:
            log(">>> 步骤1: 使用历史数据作为参考日行情")
            all_ticks = {}
            for stock, hist in g_history_data.items():
                if len(hist['closes']) > 0:
                    all_ticks[stock] = {
                        'lastPrice': hist['closes'][-1],
                        'high': hist['highs'][-1],
                        'low': hist['lows'][-1],
                        'open': hist['opens'][-1],
                        'volume': hist['volumes'][-1]
                    }
            tick_count = len(all_ticks)
            log(f">>> 历史行情提取完成: {tick_count} 只")
        else:
            log(">>> 步骤1: 获取实时行情")
            tick_start = datetime.now()
            try:
                all_ticks = ContextInfo.get_full_tick(g_stock_pool)
                tick_count = len(all_ticks) if all_ticks else 0
                tick_elapsed = (datetime.now() - tick_start).total_seconds()
                log(f">>> 行情获取完成: {tick_count} 只, 耗时 {tick_elapsed:.3f} 秒")
            except Exception as e:
                log(f">>> 获取实时行情异常: {e}", "ERROR")
                all_ticks = {}
        
        if not all_ticks:
            log(">>> 无行情数据，跳过本轮扫描", "WARN")
            return
        
        # 极速过滤
        log("")
        log(">>> 步骤2: 极速过滤")
        
        qualified = []
        stats = {
            'total': len(g_stock_pool),
            'no_tick': 0, 'no_prev': 0, 'no_hist': 0, 'no_enough_data': 0,
            'fail_yin_10': 0, 'fail_pull_10': 0, 'fail_yin_20': 0,
            'pass': 0
        }
        
        for stock in g_stock_pool:
            try:
                result = check_stock(stock, all_ticks, stats)
                if result:
                    qualified.append(result)
                    code = stock.split('.')[0]
                    name = g_stock_names.get(stock, code)
                    log(f"  ★ 命中: {stock} {name} | 涨幅:{result['current_gain']}% | 最高涨幅:{result['today_max_gain']}% | 10天{result['yin_count_10']}阴{result['pull_count_10']}拉 | 20天{result['yin_count_20']}阴 | {result['group']}", "SUCCESS")
            except Exception as e:
                log(f"  检查股票异常 [{stock}]: {e}", "ERROR")
        
        total_elapsed = (datetime.now() - start_time).total_seconds()
        
        # 打印统计
        log("")
        log("=" * 60)
        log("扫描统计汇总")
        log("=" * 60)
        log(f"总股票数: {stats['total']}")
        log(f"总扫描耗时: {total_elapsed:.2f} 秒")
        log("-" * 40)
        log(f"无行情数据: {stats['no_tick']}")
        log(f"无昨收价: {stats['no_prev']}")
        log(f"无历史数据: {stats['no_hist']}")
        log(f"数据不足: {stats['no_enough_data']}")
        log(f"第1道淘汰(10天阴线>3): {stats['fail_yin_10']}")
        log(f"第2道淘汰(10天拉升<5): {stats['fail_pull_10']}")
        log(f"第3道淘汰(20天阴线不满足): {stats['fail_yin_20']}")
        log(f"全部通过: {stats['pass']}")
        log("=" * 60)
        
        # 处理结果
        process_results(qualified, current_time)
        
        log("")
        log("#" * 70)
        log(f"############## 第 {g_scan_count} 轮扫描结束 ##############")
        log("#" * 70)
        
    except Exception as e:
        log(f">>> scan_stocks函数异常: {e}", "ERROR")


def process_results(qualified, current_time):
    """处理扫描结果"""
    global g_qualified_stocks, g_all_found_stocks
    
    current_time_str = current_time.strftime('%H:%M:%S')
    
    # 使用参考日期或当前日期
    if REFERENCE_DATE:
        date_str = f"{REFERENCE_DATE[:4]}-{REFERENCE_DATE[4:6]}-{REFERENCE_DATE[6:8]}"
    else:
        date_str = current_time.strftime('%Y-%m-%d')
    
    new_stocks = []
    for item in qualified:
        stock = item['stock']
        if stock not in g_all_found_stocks:
            g_all_found_stocks[stock] = current_time_str
            new_stocks.append(item)
    
    # 新增股票处理
    if new_stocks:
        push_to_wechat(new_stocks)
        send_email(new_stocks)
        write_stocks_to_file(new_stocks, date_str)
        save_to_redis(new_stocks, date_str)  # 保存到Redis
    
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
            log(f"    涨幅: {item['current_gain']}% | 最高涨幅: {item['today_max_gain']}% | 10天{item['yin_count_10']}阴{item['pull_count_10']}拉 | 20天{item['yin_count_20']}阴 | {item['group']}")
        
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