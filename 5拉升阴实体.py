# -*- coding: gbk -*-
"""
QMT极品策略 - 10天阴线≤3 + 10天5次拉升 + 今日最高涨幅>4.5% + 阴线前后必有大涨
策略逻辑：
1. 参考日期：今天是交易日则为今天，否则为最近一个交易日
2. 最近10个交易日的阴线次数 ≤ 3（必须满足）
3. 10天内 最高>昨收*1.065 的次数>=5
4. 今日最高涨幅 > 4.5%
5. 所有阴线的前一天或后一天必须满足最高涨幅≥6.5%
6. 计算并存储10天内阴线实体百分比总和（跌停和一字跌停按10%计算）
"""

import numpy as np
from datetime import datetime
import threading

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
REDIS_KEY_PREFIX = "5lsshiti"  # Redis key前缀

# ========== 参考日期设置 ==========
# 设置参考日期，格式：20260105，为空则使用今天
REFERENCE_DATE = ""  # 留空使用今天，或设置如 "20260105"

# 输出文件路径
OUTPUT_DIR = r"C:\Users\lyy\Desktop\股票\预警"
STRATEGY_NAME = "10天阴线≤3+5次拉升+今日涨幅>4.5%+阴线前后必有大涨"

# ========== 邮件和微信推送已禁用 ==========
EMAIL_ENABLED = False
WXPUSHER_ENABLED = False


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
            # Redis Key: 5lsshiti:20260121  Field: 600001  Value: JSON数据
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


def save_to_redis(stocks, date_str):
    """逐只股票实时保存到Redis
    Redis Key格式: 5lsshiti:YYYYMMDD (Hash结构)
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
        
        # 准备单只股票数据（精简字段）
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
            'yin_entity_sum': item['yin_entity_sum'],
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
                    'yin_entity_sum': item['yin_entity_sum'],
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
    
    # 预加载历史数据（需要至少30天数据，考虑停牌情况多取几天）
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
            # 使用 get_market_data_ex 替代 get_market_data（推荐接口）
            # 获取40天数据，以应对停牌情况
            if end_date:
                data = ContextInfo.get_market_data_ex(
                    ['open', 'high', 'low', 'close', 'volume'], 
                    batch, 
                    period='1d', 
                    count=40,
                    end_time=end_date
                )
            else:
                data = ContextInfo.get_market_data_ex(
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


def calc_yin_entity_percentage(open_price, close_price, prev_close, stock_code):
    """
    计算阴线实体百分比
    阴线实体百分比 = (开盘价 - 收盘价) / 昨收价 * 100%
    
    特殊规则：
    - 如果是跌停（包括一字跌停），无论实际跌幅是多少，统一按10%计算
    - 如果跌幅是9.9%，也按10%计算
    - 一字跌停（开盘=收盘且跌停）也算阴线实体10%
    """
    if prev_close <= 0:
        return 0
    
    # 判断是否跌停（包括一字跌停）
    is_ld = is_limit_down(close_price, open_price, prev_close, stock_code)
    
    # 如果是跌停（包括一字跌停），统一按10%计算
    if is_ld:
        return 10.0
    
    # 判断是否为阴线（收盘价 < 开盘价）
    if close_price >= open_price:
        return 0  # 不是阴线，返回0
    
    # 计算实际跌幅
    actual_drop = (open_price - close_price) / prev_close * 100
    
    # 如果跌幅>=9.9%，统一按10%计算
    if actual_drop >= 9.9:
        return 10.0
    
    return actual_drop


def get_valid_trading_days(closes, opens, highs, lows, volumes, need_days, stock_code=""):
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
        
        prev_close_price = closes[i-1] if i > 0 else 0
        
        # 判断是否跌停
        is_ld = is_limit_down(closes[i], opens[i], prev_close_price, stock_code)
        
        # 判断是否光脚阳线
        is_bfy = is_bare_foot_yang(opens[i], closes[i], lows[i])
        
        # 计算阴线实体百分比
        yin_entity_pct = calc_yin_entity_percentage(opens[i], closes[i], prev_close_price, stock_code)
        
        # 计算当天最高涨幅
        max_gain_today = calc_max_gain(highs[i], prev_close_price) if prev_close_price > 0 else 0
        
        # 计算前一天最高涨幅（如果存在）
        max_gain_prev = 0
        if i > 1:  # 确保有前一天数据
            prev_prev_close = closes[i-2] if i > 1 else 0
            if prev_prev_close > 0:
                max_gain_prev = calc_max_gain(highs[i-1], prev_prev_close)
        
        # 计算后一天最高涨幅（如果存在）
        max_gain_next = 0
        if i < len(closes) - 2:  # 确保有后一天数据
            next_close_price = closes[i]  # 当天收盘就是后一天的昨收
            if next_close_price > 0 and i + 1 < len(highs):
                max_gain_next = calc_max_gain(highs[i+1], next_close_price)
        
        valid_days.append({
            'idx': i,
            'open': opens[i],
            'high': highs[i],
            'low': lows[i],
            'close': closes[i],
            'volume': volumes[i],
            'prev_close': prev_close_price,
            'is_limit_down': is_ld,
            'is_bare_foot_yang': is_bfy,
            'yin_entity_percentage': yin_entity_pct,
            'max_gain_today': max_gain_today,
            'max_gain_prev': max_gain_prev,
            'max_gain_next': max_gain_next
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
    
    # 获取股票代码（用于判断跌停）
    stock_code = stock.split('.')[0]
    
    # ========== 获取有效的10个交易日（排除停牌） ==========
    valid_days_10 = get_valid_trading_days(closes, opens, highs, lows, volumes, 10, stock_code)
    
    if len(valid_days_10) < 10:
        stats['no_enough_data'] += 1
        return None
    
    # ========== 条件1：最近10个交易日的阴线次数 ≤ 3 ==========
    yin_count_10 = 0
    yin_entity_sum = 0  # 阴线实体百分比总和
    yin_days_info = []  # 记录阴线日期信息，用于后续检查
    
    for day in valid_days_10:
        # 阴线：收盘价 < 开盘价，或者跌停（包括一字跌停）
        if day['close'] < day['open'] or day['is_limit_down']:
            yin_count_10 += 1
            # 累加阴线实体百分比
            yin_entity_sum += day['yin_entity_percentage']
            # 记录阴线信息
            yin_days_info.append({
                'idx': day['idx'],
                'max_gain_prev': day['max_gain_prev'],
                'max_gain_next': day['max_gain_next']
            })
    
    if yin_count_10 > 3:
        stats['fail_yin_10'] += 1
        return None
    
    # ========== 条件2：10天内 最高>昨收*1.065 的次数>=5 ==========
    pull_count_10 = 0
    for day in valid_days_10:
        if day['prev_close'] > 0:
            if day['high'] > day['prev_close'] * 1.065:
                pull_count_10 += 1
    
    if pull_count_10 < 5:
        stats['fail_pull_10'] += 1
        return None
    
    # ========== 条件3：今日最高涨幅 > 4.5% ==========
    if ref_max_gain <= 4.5:
        stats['fail_today_gain'] += 1
        return None
    
    # ========== 条件4：所有阴线的前一天或后一天必须满足最高涨幅≥6.5% ==========
    for yin_info in yin_days_info:
        # 检查前一天或后一天是否有最高涨幅≥6.5%
        if yin_info['max_gain_prev'] < 6.5 and yin_info['max_gain_next'] < 6.5:
            stats['fail_yin_neighbor'] += 1
            return None
    
    # 通过所有条件
    stats['pass'] += 1
    
    # 计算涨幅
    gain = (current_price - prev_close) / prev_close * 100
    
    # 通过条件的股票，打印详细日志
    stock_name = g_stock_names.get(stock, stock.split('.')[0])
    if PRINT_PROGRESS:
        log(f"  ✓ {stock_name}({stock.split('.')[0]}) | 10天{yin_count_10}阴{pull_count_10}拉 | 阴实体{yin_entity_sum:.2f}% | 今日最高涨幅{ref_max_gain:.2f}%", "INFO")
    
    return {
        'stock': stock,
        'current_gain': round(gain, 2),
        'today_max_gain': round(ref_max_gain, 2),
        'current_price': current_price,
        'prev_close': round(prev_close, 2),
        'volume': current_volume,
        'yin_count_10': yin_count_10,
        'pull_count_10': pull_count_10,
        'yin_entity_sum': round(yin_entity_sum, 2),
        'time': datetime.now().strftime('%H:%M:%S')
    }


# 邮件和微信推送功能已移除


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
                yin_entity = item['yin_entity_sum']
                
                # 格式：代码\t名称\t日期 时间\t价格\t涨幅%\t最高涨幅%\t成交量\t10天阴线\t10天拉升\t阴线实体%\t策略名称
                line = f"{code}\t{stock_name}\t{date_str} {item['time']}\t{price:.2f}\t{gain:.2f}%\t{max_gain:.2f}%\t{volume}\t{item['yin_count_10']}阴\t{item['pull_count_10']}拉\t阴实体{yin_entity:.2f}%\t{STRATEGY_NAME}\n"
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
            'fail_yin_10': 0, 'fail_pull_10': 0, 'fail_today_gain': 0, 'fail_yin_neighbor': 0,
            'pass': 0
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
                if result:
                    qualified.append(result)
                    code = stock.split('.')[0]
                    name = g_stock_names.get(stock, code)
                    ld_10 = result.get('limit_down_count_10', 0)
                    ld_20 = result.get('limit_down_count_20', 0)
                    bfy_10 = result.get('bare_foot_yang_count_10', 0)
                    ld_info_10 = f"(含{ld_10}跌停)" if ld_10 > 0 else ""
                    ld_info_20 = f"(含{ld_20}跌停)" if ld_20 > 0 else ""
                    
                    # 检查是否为新股票
                    is_new_stock = stock not in g_all_found_stocks
                    
                    if is_new_stock:
                        log(f"  ★ 新发现: {stock} {name} | 涨幅:{result['current_gain']}% | 最高涨幅:{result['today_max_gain']}% | 10天{result['yin_count_10']}阴{result['pull_count_10']}拉 | 阴实体{result['yin_entity_sum']:.2f}%", "SUCCESS")
                        
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
                            'yin_entity_sum': result['yin_entity_sum'],
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
                        log(f"  ● 已存在: {stock} {name} | 涨幅:{result['current_gain']}% | 最高涨幅:{result['today_max_gain']}% | 10天{result['yin_count_10']}阴{result['pull_count_10']}拉 | 阴实体{result['yin_entity_sum']:.2f}%")
                        
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
        log(f"第3道淘汰(今日最高涨幅≤4.5%): {stats['fail_today_gain']}")
        log(f"第4道淘汰(阴线前后无大涨): {stats['fail_yin_neighbor']}")
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
    
    # 新增股票处理（Redis已在发现时实时保存）
    if new_stocks:
        # 写入文件
        write_stocks_to_file(new_stocks, date_str)
    
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
            log(f"    涨幅: {item['current_gain']}% | 最高涨幅: {item['today_max_gain']}% | 10天{item['yin_count_10']}阴{item['pull_count_10']}拉 | 阴实体{item['yin_entity_sum']:.2f}%")
        
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
    global g_history_data, g_prev_close_cache, g_qualified_stocks, g_scan_count, g_all_found_stocks, g_stock_names
    g_history_data = {}
    g_prev_close_cache = {}
    g_qualified_stocks = {}
    g_all_found_stocks = {}
    g_stock_names = {}
    g_scan_count = 0
    log("缓存已清理")


def manual_scan(ContextInfo):
    log(">>> 手动触发扫描")
    scan_stocks(ContextInfo)