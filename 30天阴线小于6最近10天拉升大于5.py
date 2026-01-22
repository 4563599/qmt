# -*- coding: gbk -*-
"""
QMT极品策略 - 10天阴线≤3 + 10天5次拉升 + 20天阴线≤5或=6
策略逻辑：
1. 参考日期：今天是交易日则为今天，否则为最近一个交易日
2. 最近10个交易日（包括今日参考日）的阴线次数 ≤ 3（必须满足）
3. 10天内（包括今日参考日） 最高>昨收*1.065 的次数>=5（盘中动态计算）
4. 第一组 最近20个交易日（包括今日参考日）的阴线次数 ≤ 5（必须满足）+ 今日最高涨幅>4.5%
   第二组 最近20个交易日（包括今日参考日）的阴线次数=6，并且参考日期最高涨幅>6.5%
5. 如果最近20个交易日内有停牌日期，则停牌日期不算，要继续获取数据，保证要有20个交易日的数据

注意：10天和20天计算包括今日，今日数据盘中实时变化，可能导致股票动态进出筛选池
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
EMAIL_ENABLED = False  # 已关闭邮件发送
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
        # 增加超时设置，避免网络延迟导致的超时问题
        if REDIS_PASSWORD:
            g_redis_client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                password=REDIS_PASSWORD,
                decode_responses=True,
                socket_timeout=30,  # Socket超时30秒
                socket_connect_timeout=10,  # 连接超时10秒
                socket_keepalive=True,  # 保持连接
                retry_on_timeout=True,  # 超时重试
                health_check_interval=30  # 健康检查间隔
            )
        else:
            g_redis_client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                decode_responses=True,
                socket_timeout=30,
                socket_connect_timeout=10,
                socket_keepalive=True,
                retry_on_timeout=True,
                health_check_interval=30
            )
        
        # 测试连接
        g_redis_client.ping()
        log(f">>> Redis连接成功: {REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}")
        log(f"  连接配置: 超时30s, 自动重试, 保持连接")
        return True
    except Exception as e:
        log(f">>> Redis连接失败: {e}", "ERROR")
        g_redis_client = None
        return False


def save_single_stock_to_redis(stock_data, redis_key, max_retries=3):
    """保存单只股票到Redis（带重试机制）
    Args:
        stock_data: 股票数据字典
        redis_key: Redis键名
        max_retries: 最大重试次数
    Returns:
        bool: 是否成功保存
    """
    if not g_redis_client:
        return False
    
    stock_code = stock_data['code']
    stock_name = stock_data['name']
    
    for retry in range(max_retries):
        try:
            # 使用 Hash 结构存储单只股票，field 为股票代码
            # Redis Key: 5ls20:20260121  Field: 600001  Value: JSON数据
            json_data = json.dumps(stock_data, ensure_ascii=False)
            
            # 使用 HSET 存储单只股票
            g_redis_client.hset(redis_key, stock_code, json_data)
            
            # 设置过期时间（只需要设置一次）
            g_redis_client.expire(redis_key, 30 * 24 * 60 * 60)
            
            # 成功则返回
            if retry > 0:
                log(f"  ✓ Redis保存成功(重试{retry}次): {stock_name}({stock_code})")
            return True
            
        except Exception as e:
            if retry < max_retries - 1:
                log(f"  ✗ Redis保存失败(第{retry+1}次尝试): {stock_name}({stock_code}) - {str(e)}", "WARN")
                # 尝试重新连接
                try:
                    g_redis_client.ping()
                except:
                    log(f"  尝试重新连接Redis...", "WARN")
            else:
                log(f"  ✗ Redis保存失败(已重试{max_retries}次): {stock_name}({stock_code}) - {str(e)}", "ERROR")
                return False
    
    return False


def delete_stock_from_redis(stock_code, stock_name, redis_key, max_retries=3):
    """从Redis中删除单只股票（带重试机制）
    Args:
        stock_code: 股票代码（不含后缀）
        stock_name: 股票名称
        redis_key: Redis键名
        max_retries: 最大重试次数
    Returns:
        bool: 是否成功删除
    """
    if not g_redis_client:
        return False
    
    for retry in range(max_retries):
        try:
            # 使用 HDEL 从 Hash 中删除该股票
            result = g_redis_client.hdel(redis_key, stock_code)
            
            if result > 0:
                # 成功删除
                if retry > 0:
                    log(f"  ✓ Redis删除成功(重试{retry}次): {stock_name}({stock_code})")
                return True
            else:
                # 字段不存在（可能已经被删除了）
                return True
            
        except Exception as e:
            if retry < max_retries - 1:
                log(f"  ✗ Redis删除失败(第{retry+1}次尝试): {stock_name}({stock_code}) - {str(e)}", "WARN")
                # 尝试重新连接
                try:
                    g_redis_client.ping()
                except:
                    log(f"  尝试重新连接Redis...", "WARN")
            else:
                log(f"  ✗ Redis删除失败(已重试{max_retries}次): {stock_name}({stock_code}) - {str(e)}", "ERROR")
                return False
    
    return False


def save_to_redis(stocks, date_str):
    """逐只股票实时保存到Redis
    Redis Key格式: 5ls20:YYYYMMDD (Hash结构)
    每个Field为股票代码，Value为该股票的JSON数据
    """
    if not g_redis_client:
        log(">>> Redis未连接，跳过保存", "WARN")
        return
    
    # 生成Redis key
    file_date = date_str.replace('-', '')  # 20260121
    redis_key = f"{REDIS_KEY_PREFIX}:{file_date}"
    
    log(f">>> 开始逐只保存到Redis: {redis_key}")
    log(f"  待保存股票数量: {len(stocks)}")
    
    success_count = 0
    failed_stocks = []
    
    for idx, item in enumerate(stocks, 1):
        code = item['stock'].split('.')[0]
        stock_name = g_stock_names.get(item['stock'], code)
        
        # 准备单只股票数据
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
            'limit_down_count_10': item.get('limit_down_count_10', 0),
            'limit_down_count_20': item.get('limit_down_count_20', 0),
            'bare_foot_yang_count_10': item.get('bare_foot_yang_count_10', 0),
            'group': item['group'],
            'time': item['time'],
            'date': date_str
        }
        
        # 立即保存到Redis（带重试）
        if save_single_stock_to_redis(stock_info, redis_key, max_retries=3):
            success_count += 1
            log(f"  [{idx}/{len(stocks)}] ✓ {stock_name}({code}) 已保存到Redis")
        else:
            failed_stocks.append(f"{stock_name}({code})")
            log(f"  [{idx}/{len(stocks)}] ✗ {stock_name}({code}) 保存失败", "ERROR")
    
    # 汇总结果
    log(f">>> Redis保存完成: 成功 {success_count}/{len(stocks)} 只")
    if failed_stocks:
        log(f"  失败股票: {', '.join(failed_stocks)}", "ERROR")
    
    # 尝试补救失败的股票（再试一次）
    if failed_stocks and success_count > 0:
        log(f"  正在尝试补救失败的 {len(failed_stocks)} 只股票...")
        retry_success = 0
        for item in stocks:
            code = item['stock'].split('.')[0]
            stock_name = g_stock_names.get(item['stock'], code)
            if f"{stock_name}({code})" in failed_stocks:
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
                    'limit_down_count_10': item.get('limit_down_count_10', 0),
                    'limit_down_count_20': item.get('limit_down_count_20', 0),
                    'bare_foot_yang_count_10': item.get('bare_foot_yang_count_10', 0),
                    'group': item['group'],
                    'time': item['time'],
                    'date': date_str
                }
                if save_single_stock_to_redis(stock_info, redis_key, max_retries=2):
                    retry_success += 1
        if retry_success > 0:
            log(f"  补救成功: {retry_success} 只")
            success_count += retry_success


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
    
    # 预加载历史数据（需要至少20天数据，考虑停牌情况多取30天）
    log("")
    log(">>> 正在预加载历史数据...")
    preload_history_data(ContextInfo)
    log(f">>> 历史数据预加载完成: {len(g_history_data)} 只")
    
    ContextInfo.run_time("scan_stocks", "3nSecond", "2024-01-01 09:25:00", "SH")
    log("定时器设置完成，每3秒扫描一次，9:25开始")
    
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
    """预加载历史数据（需要至少20天数据，考虑停牌多取30天）"""
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
            # 使用 get_market_data_ex 替代 get_market_data（推荐接口）
            # 获取30天数据，以应对停牌情况
            if end_date:
                data = ContextInfo.get_market_data_ex(
                    ['open', 'high', 'low', 'close', 'volume'], 
                    batch, 
                    period='1d', 
                    count=30,
                    end_time=end_date
                )
            else:
                data = ContextInfo.get_market_data_ex(
                    ['open', 'high', 'low', 'close', 'volume'], 
                    batch, 
                    period='1d', 
                    count=30
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


def calc_limit_down_price(prev_close, stock_code):
    """
    计算跌停价
    根据不同板块计算跌停价：
    - 科创板/创业板(注册制)：20%
    - 北交所：30%
    - ST股票：5%
    - 主板：10%
    """
    if prev_close <= 0:
        return 0
    
    # 判断股票类型
    if stock_code.startswith('688') or stock_code.startswith('689'):  # 科创板
        limit_ratio = 0.20
    elif stock_code.startswith('300'):  # 创业板(注册制)
        limit_ratio = 0.20
    elif stock_code.startswith('8') or stock_code.startswith('4'):  # 北交所
        limit_ratio = 0.30
    elif 'ST' in stock_code.upper():  # ST股票
        limit_ratio = 0.05
    else:  # 主板
        limit_ratio = 0.10
    
    limit_down = prev_close * (1 - limit_ratio)
    return round(limit_down, 2)


def is_limit_down(close_price, open_price, prev_close, stock_code, tolerance=0.01):
    """
    判断是否跌停（包括一字跌停）
    tolerance: 容差，默认1%，考虑到价格精度问题
    """
    if prev_close <= 0:
        return False
    
    limit_down = calc_limit_down_price(prev_close, stock_code)
    
    # 判断收盘价是否接近跌停价（考虑价格精度）
    price_diff_ratio = abs(close_price - limit_down) / prev_close
    
    # 如果收盘价在跌停价附近（容差范围内），认为是跌停
    if price_diff_ratio <= tolerance:
        return True
    
    return False


def get_valid_trading_days(closes, opens, highs, lows, volumes, need_days, stock_code="", skip_last=True):
    """
    获取有效交易日数据（排除停牌日）
    返回：有效交易日的数据列表
    skip_last: 是否跳过最后一天(默认True,因为最后一天是今天,会用实时行情替代)
    """
    valid_days = []
    
    # 从历史数据的倒数第2天开始往前查找（因为最后一天是今天，会用实时行情替代）
    # 历史数据: [..., 前天, 昨天, 今天]，索引为 [..., -3, -2, -1]
    # 我们需要从昨天(-2)开始往前取数据
    for i in range(len(closes) - 2, -1, -1):
        # 检查是否停牌
        if is_suspended(volumes[i]):
            continue
        
        prev_close_price = closes[i-1] if i > 0 else 0
        
        # 判断是否跌停
        is_ld = is_limit_down(closes[i], opens[i], prev_close_price, stock_code)
        
        # 判断是否光脚阳线
        is_bfy = is_bare_foot_yang(opens[i], closes[i], lows[i])
        
        valid_days.append({
            'idx': i,
            'open': opens[i],
            'high': highs[i],
            'low': lows[i],
            'close': closes[i],
            'volume': volumes[i],
            'prev_close': prev_close_price,
            'is_limit_down': is_ld,
            'is_bare_foot_yang': is_bfy
        })
        
        if len(valid_days) >= need_days:
            break
    
    return valid_days


def is_bare_foot_yang(open_price, close_price, low_price, tolerance=0.001):
    """
    判断是否为光脚阳线
    光脚阳线：阳线且没有下影线（或下影线极短）
    条件：
    1. 收盘价 > 开盘价（阳线）
    2. 最低价 = 开盘价（或非常接近，考虑价格精度）
    
    tolerance: 容差，默认0.1%，用于判断是否"接近"
    """
    # 必须是阳线
    if close_price <= open_price:
        return False
    
    # 计算下影线长度相对于K线实体的比例
    body = close_price - open_price
    lower_shadow = open_price - low_price
    
    if body <= 0:
        return False
    
    # 下影线长度相对于实体的比例小于容差，认为是光脚
    shadow_ratio = lower_shadow / body
    
    return shadow_ratio <= tolerance


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
    # 调试：重点关注的股票
    stock_code = stock.split('.')[0]
    stock_name = g_stock_names.get(stock, stock_code)
    is_debug_stock = stock_name in ['锋龙股份', '五矿发展'] or stock_code in ['002931', '600058']
    
    if is_debug_stock:
        log(f">>> [调试] 开始检查 {stock_name}({stock_code})", "DEBUG")
    
    tick = all_ticks.get(stock)
    if not tick:
        stats['no_tick'] += 1
        if is_debug_stock:
            log(f">>> [调试] {stock_name}({stock_code}) 无行情数据", "DEBUG")
        return None
    
    prev_close = g_prev_close_cache.get(stock)
    if not prev_close or prev_close <= 0:
        stats['no_prev'] += 1
        if is_debug_stock:
            log(f">>> [调试] {stock_name}({stock_code}) 无昨收价", "DEBUG")
        return None
    
    hist = g_history_data.get(stock)
    if not hist:
        stats['no_hist'] += 1
        if is_debug_stock:
            log(f">>> [调试] {stock_name}({stock_code}) 无历史数据", "DEBUG")
        return None
    
    closes = hist['closes']
    opens = hist['opens']
    highs = hist['highs']
    lows = hist['lows']
    volumes = hist['volumes']
    
    if len(closes) < 21:
        stats['no_hist'] += 1
        if is_debug_stock:
            log(f">>> [调试] {stock_name}({stock_code}) 历史数据不足: {len(closes)}天", "DEBUG")
        return None
    
    # 获取今日数据（参考日）
    current_price = tick.get('lastPrice', 0)
    if not current_price:
        current_price = tick.get('price', tick.get('last', 0))
    
    current_high = tick.get('high', 0)
    current_low = tick.get('low', 0)
    current_open = tick.get('open', 0)
    current_volume = tick.get('volume', tick.get('vol', 0))
    
    if is_debug_stock:
        log(f">>> [调试] {stock_name}({stock_code}) 今日行情: 开{current_open:.2f} 高{current_high:.2f} 低{current_low:.2f} 收{current_price:.2f} 昨收{prev_close:.2f}", "DEBUG")
        log(f">>> [调试] {stock_name}({stock_code}) 历史数据最后一天: 开{opens[-1]:.2f} 收{closes[-1]:.2f}", "DEBUG")
    
    if current_price <= 0 or current_high <= 0:
        stats['no_tick'] += 1
        if is_debug_stock:
            log(f">>> [调试] {stock_name}({stock_code}) 价格数据无效", "DEBUG")
        return None
    
    # 参考日最高涨幅
    ref_max_gain = calc_max_gain(current_high, prev_close)
    
    if is_debug_stock:
        log(f">>> [调试] {stock_name}({stock_code}) 今日最高涨幅: {ref_max_gain:.2f}%", "DEBUG")
    
    # ========== 获取有效的9个历史交易日（排除停牌） ==========
    # 注意：这里获取9天，因为要加上今天（参考日）一起算10天
    valid_days_9 = get_valid_trading_days(closes, opens, highs, lows, volumes, 9, stock_code)
    
    if len(valid_days_9) < 9:
        stats['no_enough_data'] += 1
        if is_debug_stock:
            log(f">>> [调试] {stock_name}({stock_code}) 有效交易日不足9天: {len(valid_days_9)}天", "DEBUG")
        return None
    
    # ========== 构建今日数据（参考日），使用实时行情 ==========
    # 判断今天是否跌停
    is_limit_down_today = is_limit_down(current_price, current_open, prev_close, stock_code)
    
    # 判断今天是否光脚阳线
    is_bare_foot_yang_today = is_bare_foot_yang(current_open, current_price, current_low)
    
    today_data = {
        'open': current_open,
        'high': current_high,  # 盘中实时最高价
        'close': current_price,  # 盘中实时价格
        'low': current_low,
        'volume': current_volume,
        'prev_close': prev_close,
        'is_limit_down': is_limit_down_today,
        'is_bare_foot_yang': is_bare_foot_yang_today
    }
    
    # 将今日数据插入到列表最前面，组成完整的10天（含今日）
    valid_days_10 = [today_data] + valid_days_9
    
    # ========== 条件2：最近10个交易日的阴线次数 ≤ 3 ==========
    yin_count_10 = 0
    limit_down_count_10 = 0  # 记录跌停次数
    bare_foot_yang_count_10 = 0  # 记录光脚阳线次数
    for day in valid_days_10:
        # 阴线：收盘价 < 开盘价，或者跌停（包括一字跌停）
        if day['close'] < day['open'] or day['is_limit_down']:
            yin_count_10 += 1
            if day['is_limit_down']:
                limit_down_count_10 += 1
        
        # 统计光脚阳线
        if day['is_bare_foot_yang']:
            bare_foot_yang_count_10 += 1
    
    if is_debug_stock:
        log(f">>> [调试] {stock_name}({stock_code}) 10天阴线: {yin_count_10}次 (含{limit_down_count_10}跌停)", "DEBUG")
    
    if yin_count_10 > 3:
        stats['fail_yin_10'] += 1
        if is_debug_stock:
            log(f">>> [调试] {stock_name}({stock_code}) ❌ 10天阴线>3,被淘汰", "DEBUG")
        return None
    
    # ========== 条件3：10天内 最高>昨收*1.065 的次数>=5 ==========
    pull_count_10 = 0
    for day in valid_days_10:
        if day['prev_close'] > 0:
            if day['high'] > day['prev_close'] * 1.065:
                pull_count_10 += 1
    
    if is_debug_stock:
        log(f">>> [调试] {stock_name}({stock_code}) 10天拉升: {pull_count_10}次", "DEBUG")
    
    if pull_count_10 < 5:
        stats['fail_pull_10'] += 1
        if is_debug_stock:
            log(f">>> [调试] {stock_name}({stock_code}) ❌ 10天拉升<5次,被淘汰", "DEBUG")
        return None
    
    # ========== 获取有效的19个历史交易日（排除停牌） ==========
    # 注意：这里获取19天，因为要加上今天（参考日）一起算20天
    valid_days_19 = get_valid_trading_days(closes, opens, highs, lows, volumes, 19, stock_code)
    
    if len(valid_days_19) < 19:
        stats['no_enough_data'] += 1
        if is_debug_stock:
            log(f">>> [调试] {stock_name}({stock_code}) 有效交易日不足19天: {len(valid_days_19)}天", "DEBUG")
        return None
    
    # 将今日数据插入到列表最前面，组成完整的20天（含今日）
    valid_days_20 = [today_data] + valid_days_19
    
    # ========== 条件4：20天阴线判断（包括今日，盘中动态） ==========
    yin_count_20 = 0
    limit_down_count_20 = 0  # 记录跌停次数
    
    if is_debug_stock:
        log(f">>> [调试] {stock_name}({stock_code}) 20天K线明细:", "DEBUG")
    
    for idx, day in enumerate(valid_days_20):
        # 阴线：收盘价 < 开盘价，或者跌停（包括一字跌停）
        is_yin = day['close'] < day['open'] or day['is_limit_down']
        if is_yin:
            yin_count_20 += 1
            if day['is_limit_down']:
                limit_down_count_20 += 1
        
        if is_debug_stock:
            day_type = "阴线" if is_yin else "阳线"
            day_label = "今日" if idx == 0 else f"T-{idx}"
            log(f"  {day_label}: 开{day['open']:.2f} 收{day['close']:.2f} 高{day['high']:.2f} 低{day['low']:.2f} -> {day_type}", "DEBUG")
    
    if is_debug_stock:
        log(f">>> [调试] {stock_name}({stock_code}) 20天阴线: {yin_count_20}次 (含{limit_down_count_20}跌停)", "DEBUG")
    
    # 判断属于哪个组
    group = None
    
    # 第一组：最近20个交易日的阴线次数 ≤ 5 + 今日最高涨幅>4.5%（盘中动态）
    if yin_count_20 <= 5 and ref_max_gain > 4.5:
        group = "第一组(20天阴线≤5且今日涨幅>4.5%)"
        if is_debug_stock:
            log(f">>> [调试] {stock_name}({stock_code}) ✅ 通过所有条件! 第一组", "DEBUG")
    # 第二组：最近20个交易日的阴线次数=6，并且参考日期最高涨幅>6.5%（盘中动态）
    elif yin_count_20 == 6 and ref_max_gain > 6.5:
        group = "第二组(20天阴线=6且参考日涨幅>6.5%)"
        if is_debug_stock:
            log(f">>> [调试] {stock_name}({stock_code}) ✅ 通过所有条件! 第二组", "DEBUG")
    else:
        # 调试信息：打印接近通过的股票
        if is_debug_stock or yin_count_20 <= 6:
            if yin_count_20 <= 5:
                log(f"  ⊗ {stock_name}({stock_code}) ❌ 20天{yin_count_20}阴满足,但今日最高涨幅{ref_max_gain:.2f}%不够 (需>4.5%)", "DEBUG")
            elif yin_count_20 == 6:
                log(f"  ⊗ {stock_name}({stock_code}) ❌ 20天{yin_count_20}阴满足,但今日最高涨幅{ref_max_gain:.2f}%不够 (需>6.5%)", "DEBUG")
            else:
                log(f"  ⊗ {stock_name}({stock_code}) ❌ 20天阴线{yin_count_20}次,不满足条件", "DEBUG")
        stats['fail_yin_20'] += 1
        return None
    
    # 通过所有条件
    stats['pass'] += 1
    
    # 计算涨幅
    gain = (current_price - prev_close) / prev_close * 100
    
    # 通过条件的股票，打印详细日志
    stock_name = g_stock_names.get(stock, stock.split('.')[0])
    if PRINT_PROGRESS:
        log(f"  ✓ {stock_name}({stock.split('.')[0]}) | 组{group} | 10天{yin_count_10}阴{pull_count_10}拉 | 20天{yin_count_20}阴", "INFO")
    
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
        'limit_down_count_10': limit_down_count_10,
        'limit_down_count_20': limit_down_count_20,
        'bare_foot_yang_count_10': bare_foot_yang_count_10,
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
        
        ld_10 = item.get('limit_down_count_10', 0)
        ld_20 = item.get('limit_down_count_20', 0)
        bfy_10 = item.get('bare_foot_yang_count_10', 0)
        ld_info_10 = f"(含{ld_10}跌停)" if ld_10 > 0 else ""
        ld_info_20 = f"(含{ld_20}跌停)" if ld_20 > 0 else ""
        
        msg_content = f"{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n最高涨幅：{max_gain}\n10天阴线：{item['yin_count_10']}{ld_info_10}\n10天拉升：{item['pull_count_10']}次\n10天光脚阳线：{bfy_10}次\n20天阴线：{item['yin_count_20']}{ld_info_20}\n分组：{item['group']}\n时间：{time_str} {date_weekday}"
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
        
        ld_10 = item.get('limit_down_count_10', 0)
        ld_20 = item.get('limit_down_count_20', 0)
        bfy_10 = item.get('bare_foot_yang_count_10', 0)
        ld_info_10 = f"(含{ld_10}跌停)" if ld_10 > 0 else ""
        ld_info_20 = f"(含{ld_20}跌停)" if ld_20 > 0 else ""
        
        subject = f"{name} {gain}"
        body = f"发现时间：{current_time}\n\n{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n最高涨幅：{max_gain}\n10天阴线：{item['yin_count_10']}{ld_info_10}\n10天拉升：{item['pull_count_10']}次\n10天光脚阳线：{bfy_10}次\n20天阴线：{item['yin_count_20']}{ld_info_20}\n分组：{item['group']}\n时间：{time_str} {date_weekday}"
        
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
                
                ld_10 = item.get('limit_down_count_10', 0)
                ld_20 = item.get('limit_down_count_20', 0)
                bfy_10 = item.get('bare_foot_yang_count_10', 0)
                ld_info_10 = f"(含{ld_10}跌停)" if ld_10 > 0 else ""
                ld_info_20 = f"(含{ld_20}跌停)" if ld_20 > 0 else ""
                
                # 格式：代码\t名称\t日期 时间\t价格\t涨幅%\t最高涨幅%\t成交量\t10天阴线\t10天拉升\t10天光脚阳\t20天阴线\t分组\t策略名称
                line = f"{code}\t{stock_name}\t{date_str} {item['time']}\t{price:.2f}\t{gain:.2f}%\t{max_gain:.2f}%\t{volume}\t{item['yin_count_10']}阴{ld_info_10}\t{item['pull_count_10']}拉\t{bfy_10}光脚阳\t{item['yin_count_20']}阴{ld_info_20}\t{item['group']}\t{STRATEGY_NAME}\n"
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
            'pass': 0,
            'removed': 0  # 新增：记录从Redis删除的股票数量
        }
        
        # 获取日期字符串（用于Redis保存）
        if REFERENCE_DATE:
            date_str = f"{REFERENCE_DATE[:4]}-{REFERENCE_DATE[4:6]}-{REFERENCE_DATE[6:8]}"
        else:
            date_str = current_time.strftime('%Y-%m-%d')
        
        file_date = date_str.replace('-', '')
        redis_key = f"{REDIS_KEY_PREFIX}:{file_date}"
        
        for stock in g_stock_pool:
            try:
                result = check_stock(stock, all_ticks, stats)
                code = stock.split('.')[0]
                name = g_stock_names.get(stock, code)
                
                if result:
                    # ✅ 满足条件
                    qualified.append(result)
                    ld_10 = result.get('limit_down_count_10', 0)
                    ld_20 = result.get('limit_down_count_20', 0)
                    bfy_10 = result.get('bare_foot_yang_count_10', 0)
                    ld_info_10 = f"(含{ld_10}跌停)" if ld_10 > 0 else ""
                    ld_info_20 = f"(含{ld_20}跌停)" if ld_20 > 0 else ""
                    
                    # 检查是否为新股票
                    is_new_stock = stock not in g_all_found_stocks
                    
                    if is_new_stock:
                        log(f"  ★ 新发现: {stock} {name} | 涨幅:{result['current_gain']}% | 最高涨幅:{result['today_max_gain']}% | 10天{result['yin_count_10']}阴{ld_info_10}{result['pull_count_10']}拉{bfy_10}光脚阳 | 20天{result['yin_count_20']}阴{ld_info_20} | {result['group']}", "SUCCESS")
                        
                        # 只对新股票立即保存到Redis
                        stock_info = {
                            'code': code,
                            'name': name,
                            'stock': result['stock'],
                            'current_gain': round(result['current_gain'], 2),
                            'today_max_gain': round(result['today_max_gain'], 2),
                            'current_price': round(result['current_price'], 2),
                            'prev_close': round(result['prev_close'], 2),
                            'volume': result.get('volume', 0),
                            'yin_count_10': result['yin_count_10'],
                            'pull_count_10': result['pull_count_10'],
                            'yin_count_20': result['yin_count_20'],
                            'limit_down_count_10': result.get('limit_down_count_10', 0),
                            'limit_down_count_20': result.get('limit_down_count_20', 0),
                            'bare_foot_yang_count_10': result.get('bare_foot_yang_count_10', 0),
                            'group': result['group'],
                            'time': result['time'],
                            'date': date_str
                        }
                        # 立即保存到Redis
                        if save_single_stock_to_redis(stock_info, redis_key, max_retries=3):
                            log(f"    → Redis已保存: {name}({code})")
                        else:
                            log(f"    → Redis保存失败: {name}({code})", "ERROR")
                    else:
                        # 已存在的股票，只打印日志
                        log(f"  ● 已存在: {stock} {name} | 涨幅:{result['current_gain']}% | 最高涨幅:{result['today_max_gain']}% | 10天{result['yin_count_10']}阴{ld_info_10}{result['pull_count_10']}拉{bfy_10}光脚阳 | 20天{result['yin_count_20']}阴{ld_info_20} | {result['group']}")
                else:
                    # ❌ 不满足条件
                    # 如果之前存在于 g_all_found_stocks，说明之前满足条件，现在不满足了，需要从Redis删除
                    if stock in g_all_found_stocks:
                        log(f"  ⚠ 不再满足条件，从Redis删除: {stock} {name}", "WARN")
                        if delete_stock_from_redis(code, name, redis_key, max_retries=3):
                            log(f"    → Redis已删除: {name}({code})")
                            # 从内存中移除
                            del g_all_found_stocks[stock]
                            stats['removed'] += 1
                        else:
                            log(f"    → Redis删除失败: {name}({code})", "ERROR")
                        
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
        log(f"动态剔除(不再满足条件): {stats['removed']}")
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
    
    # 新增股票处理（Redis已在发现时实时保存）
    if new_stocks:
        # 1. 写入文件
        write_stocks_to_file(new_stocks, date_str)
        
        # 2. 微信推送
        push_to_wechat(new_stocks)
        
        # 3. 邮件发送（已关闭）
        send_email(new_stocks)
    
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
            ld_10 = item.get('limit_down_count_10', 0)
            ld_20 = item.get('limit_down_count_20', 0)
            bfy_10 = item.get('bare_foot_yang_count_10', 0)
            ld_info_10 = f"(含{ld_10}跌停)" if ld_10 > 0 else ""
            ld_info_20 = f"(含{ld_20}跌停)" if ld_20 > 0 else ""
            log(f"【{idx}】{code} {name}")
            log(f"    涨幅: {item['current_gain']}% | 最高涨幅: {item['today_max_gain']}% | 10天{item['yin_count_10']}阴{ld_info_10}{item['pull_count_10']}拉{bfy_10}光脚阳 | 20天{item['yin_count_20']}阴{ld_info_20} | {item['group']}")
        
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
    return (datetime.strptime("09:25", "%H:%M").time() <= t <= datetime.strptime("11:30", "%H:%M").time()) or \
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