# -*- coding: gbk -*-
"""
QMT极品策略 - 10天阴线≤3 + 10天5次拉升 + 阴线前后必有大涨 (双组策略)

共同条件：
1. 参考日期：今天是交易日则为今天，否则为最近一个交易日
2. 最近10个交易日（包括今日参考日）的阴线次数 ≤ 3（必须满足）
3. 10天内（包括今日参考日） 最高>昨收*1.065 的次数>=5（盘中动态计算）
4. 计算并存储10天内阴线实体百分比总和（跌停和一字跌停按10%计算）

第一组条件：
1. 今日最高涨幅 > 4.5%（盘中动态判断）
2. 所有阴线的前一天或后一天必须满足最高涨幅≥6.5%（今日阴线只检查前一天）

第二组条件：
1. 不要求今日最高涨幅
2. 所有阴线的前一天或后一天必须满足最高涨幅≥6.5%（今日阴线只检查前一天）
3. 所有阴线满足：十字星(收盘与开盘幅度≤1.8%) 或 最高涨幅≥6.5%

注意：10天计算包括今日，今日数据盘中实时变化，可能导致股票动态进出筛选池
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
LOG_ENABLED = True  # 日志开关，True=开启日志打印
LOG_FILE = r"C:\Users\Administrator\Desktop\日志\5拉升阴线前后强\日志.log"  # 日志文件路径

# ========== 个股判断日志配置 ==========
STOCK_CHECK_LOG_ENABLED = True  # 个股判断日志开关
STOCK_CHECK_LOG_FILE = r"C:\Users\Administrator\Desktop\日志\5拉升阴线前后强\个股判断.log"  # 个股判断日志文件

# ========== Redis专用日志配置 ==========
REDIS_LOG_ENABLED = True  # Redis日志开关
REDIS_LOG_FILE = r"C:\Users\Administrator\Desktop\日志\5拉升阴线前后强\redis日志.log"  # Redis操作日志文件

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
STRATEGY_NAME = "10天阴线≤3+5次拉升+阴线前后大涨(双组)"

# ========== 邮件和微信推送已禁用 ==========
EMAIL_ENABLED = False
WXPUSHER_ENABLED = False


def log(msg, level="INFO"):
    if not LOG_ENABLED:
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


def log_redis(msg, level="INFO"):
    """Redis专用日志，同时打印到控制台和Redis专用日志文件"""
    if not REDIS_LOG_ENABLED:
        return
    
    import os
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_msg = f"[{timestamp}] [{level}] {msg}"
    
    # 打印到控制台
    print(log_msg)
    
    # 写入Redis专用日志文件
    try:
        # 确保日志目录存在
        log_dir = os.path.dirname(REDIS_LOG_FILE)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        with open(REDIS_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_msg + '\n')
    except Exception as e:
        print(f"[ERROR] 写入Redis日志文件失败: {e}")


def log_stock_check(msg, level="INFO"):
    """个股判断专用日志，只写入到个股判断日志文件"""
    if not STOCK_CHECK_LOG_ENABLED:
        return
    
    import os
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]  # 精确到毫秒
    log_msg = f"[{timestamp}] [{level}] {msg}"
    
    # 写入个股判断日志文件
    try:
        # 确保日志目录存在
        log_dir = os.path.dirname(STOCK_CHECK_LOG_FILE)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        with open(STOCK_CHECK_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_msg + '\n')
    except Exception as e:
        print(f"[ERROR] 写入个股判断日志文件失败: {e}")


def init_redis():
    """初始化Redis连接"""
    global g_redis_client
    
    if not REDIS_ENABLED:
        log_redis(">>> Redis模块未安装，跳过Redis初始化", "WARN")
        return False
    
    log_redis("=" * 60)
    log_redis("开始连接Redis")
    log_redis("=" * 60)
    log_redis(f"连接信息: {REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}")
    
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
        log_redis("正在测试连接...")
        g_redis_client.ping()
        
        info = g_redis_client.info()
        log_redis("✅ Redis连接成功！")
        log_redis(f"Redis版本: {info.get('redis_version', 'N/A')}")
        log_redis(f"已用内存: {info.get('used_memory_human', 'N/A')}")
        log_redis(f"连接配置: 超时30s, 自动重试, 保持连接")
        log_redis("=" * 60)
        return True
    except Exception as e:
        log_redis(f"❌ Redis连接失败: {e}", "ERROR")
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
            
            # 验证写入
            verify_data = g_redis_client.hget(redis_key, stock_code)
            if verify_data:
                if retry > 0:
                    log_redis(f"✅ Redis保存成功(重试{retry}次): {stock_name}({stock_code})")
                else:
                    log_redis(f"✅ Redis保存成功: {stock_name}({stock_code})")
                return True
            else:
                log_redis(f"⚠️ Redis验证失败: {stock_name}({stock_code})", "WARN")
                if retry < max_retries - 1:
                    continue
                return False
            
        except Exception as e:
            if retry < max_retries - 1:
                log_redis(f"❌ Redis保存失败(第{retry+1}次尝试): {stock_name}({stock_code}) - {str(e)}", "WARN")
                # 尝试重新连接
                try:
                    g_redis_client.ping()
                except:
                    log_redis(f"🔄 尝试重新连接Redis...", "WARN")
            else:
                log_redis(f"❌ Redis保存失败(已重试{max_retries}次): {stock_name}({stock_code}) - {str(e)}", "ERROR")
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
                    log_redis(f"✅ Redis删除成功(重试{retry}次): {stock_name}({stock_code})")
                else:
                    log_redis(f"✅ Redis删除成功: {stock_name}({stock_code})")
                return True
            else:
                # 字段不存在（可能已经被删除了）
                return True
            
        except Exception as e:
            if retry < max_retries - 1:
                log_redis(f"❌ Redis删除失败(第{retry+1}次尝试): {stock_name}({stock_code}) - {str(e)}", "WARN")
                # 尝试重新连接
                try:
                    g_redis_client.ping()
                except:
                    log_redis(f"🔄 尝试重新连接Redis...", "WARN")
            else:
                log_redis(f"❌ Redis删除失败(已重试{max_retries}次): {stock_name}({stock_code}) - {str(e)}", "ERROR")
                return False
    
    return False


def save_to_redis(stocks, date_str):
    """逐只股票实时保存到Redis
    Redis Key格式: 5lsshiti:YYYYMMDD (Hash结构)
    每个Field为股票代码，Value为该股票的JSON数据
    """
    if not g_redis_client:
        log_redis(">>> Redis未连接，跳过保存", "WARN")
        return
    
    # 生成Redis key
    file_date = date_str.replace('-', '')  # 20260121
    redis_key = f"{REDIS_KEY_PREFIX}:{file_date}"
    
    log_redis("=" * 60)
    log_redis(f"开始保存股票到Redis: {redis_key}")
    log_redis("=" * 60)
    log_redis(f"待保存股票数量: {len(stocks)}")
    
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
            'group': item.get('group', '未分组'),
            'time': item['time'],
            'date': date_str
        }
        
        # 立即保存到Redis（带重试）
        if save_single_stock_to_redis(stock_info, redis_key, max_retries=3):
            success_count += 1
        else:
            failed_stocks.append(f"{stock_name}({code})")
    
    # 汇总结果
    log_redis("=" * 60)
    log_redis(f"Redis保存完成: 成功 {success_count}/{len(stocks)} 只")
    if failed_stocks:
        log_redis(f"失败股票: {', '.join(failed_stocks)}", "ERROR")
    log_redis("=" * 60)
    
    # 尝试补救失败的股票（再试一次）
    if failed_stocks and success_count > 0:
        log_redis(f"正在尝试补救失败的 {len(failed_stocks)} 只股票...")
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
                    'group': item.get('group', '未分组'),
                    'time': item['time'],
                    'date': date_str
                }
                if save_single_stock_to_redis(stock_info, redis_key, max_retries=2):
                    retry_success += 1
        if retry_success > 0:
            log_redis(f"补救成功: {retry_success} 只")
            success_count += retry_success


def get_from_redis(date_str):
    """从Redis读取数据"""
    if not g_redis_client:
        log_redis(">>> Redis未连接，无法读取", "WARN")
        return None
    
    try:
        file_date = date_str.replace('-', '')
        redis_key = f"{REDIS_KEY_PREFIX}:{file_date}"
        
        log_redis(f"开始从Redis读取: {redis_key}")
        json_data = g_redis_client.get(redis_key)
        if json_data:
            stock_data = json.loads(json_data)
            log_redis(f"✅ 从Redis读取成功: {redis_key} ({len(stock_data)}只股票)")
            return stock_data
        else:
            log_redis(f"⚠️ Redis中无数据: {redis_key}", "WARN")
            return None
    except Exception as e:
        log_redis(f"❌ 从Redis读取失败: {e}", "ERROR")
        return None


def clear_today_files():
    """清理今天的所有日志文件和输出文件"""
    import os
    from datetime import datetime
    
    today = datetime.now()
    date_str = today.strftime('%Y-%m-%d')
    file_date = today.strftime('%Y%m%d')
    
    # 1. 清理输出文件
    output_file = os.path.join(OUTPUT_DIR, f"{file_date}{STRATEGY_NAME}.txt")
    if os.path.exists(output_file):
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                pass  # 清空文件
            print(f"[清理] 已清理输出文件: {output_file}")
        except Exception as e:
            print(f"[错误] 清理输出文件失败: {e}")
    
    # 2. 清理主日志文件
    if os.path.exists(LOG_FILE):
        try:
            log_dir = os.path.dirname(LOG_FILE)
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            with open(LOG_FILE, 'w', encoding='utf-8') as f:
                pass  # 清空文件
            print(f"[清理] 已清理主日志: {LOG_FILE}")
        except Exception as e:
            print(f"[错误] 清理主日志失败: {e}")
    
    # 3. 清理Redis日志文件
    if os.path.exists(REDIS_LOG_FILE):
        try:
            log_dir = os.path.dirname(REDIS_LOG_FILE)
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            with open(REDIS_LOG_FILE, 'w', encoding='utf-8') as f:
                pass  # 清空文件
            print(f"[清理] 已清理Redis日志: {REDIS_LOG_FILE}")
        except Exception as e:
            print(f"[错误] 清理Redis日志失败: {e}")
    
    # 4. 清理个股判断日志文件
    if os.path.exists(STOCK_CHECK_LOG_FILE):
        try:
            log_dir = os.path.dirname(STOCK_CHECK_LOG_FILE)
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            with open(STOCK_CHECK_LOG_FILE, 'w', encoding='utf-8') as f:
                pass  # 清空文件
            print(f"[清理] 已清理个股判断日志: {STOCK_CHECK_LOG_FILE}")
        except Exception as e:
            print(f"[错误] 清理个股判断日志失败: {e}")


def init(ContextInfo):
    global g_stock_pool, g_initialized, g_context
    
    # 首先清理今天的所有日志文件
    clear_today_files()
    
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
        
        # 判断是否为十字星
        is_cs = is_cross_star(opens[i], closes[i], prev_close_price)
        
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
            'is_cross_star': is_cs,
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


def is_cross_star(open_price, close_price, prev_close, tolerance=0.018):
    """
    判断是否为十字星
    十字星：收盘价和开盘价的幅度 ≤ 1.8%
    
    计算公式：|收盘价 - 开盘价| / 昨收价 * 100% ≤ 1.8%
    
    Args:
        open_price: 开盘价
        close_price: 收盘价
        prev_close: 昨收价
        tolerance: 容差，默认1.8%
    
    Returns:
        bool: 是否为十字星
    """
    if prev_close <= 0:
        return False
    
    # 计算开盘和收盘的幅度百分比
    amplitude = abs(close_price - open_price) / prev_close
    
    return amplitude <= tolerance


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
    stock_code = stock.split('.')[0]
    stock_name = g_stock_names.get(stock, stock_code)
    
    log_stock_check(f"\n{'='*80}")
    log_stock_check(f"开始检查 {stock_name}({stock_code})")
    log_stock_check(f"{'='*80}")
    
    tick = all_ticks.get(stock)
    if not tick:
        stats['no_tick'] += 1
        log_stock_check(f"❌ 淘汰原因：无行情数据")
        log_stock_check(f"{'='*80}\n")
        return None
    
    log_stock_check(f"✓ 获取到行情数据")
    log_stock_check(f"  lastPrice: {tick.get('lastPrice', 0)}")
    log_stock_check(f"  high: {tick.get('high', 0)}")
    log_stock_check(f"  low: {tick.get('low', 0)}")
    log_stock_check(f"  open: {tick.get('open', 0)}")
    log_stock_check(f"  volume: {tick.get('volume', 0)}")
    
    prev_close = g_prev_close_cache.get(stock)
    if not prev_close or prev_close <= 0:
        stats['no_prev'] += 1
        log_stock_check(f"❌ 淘汰原因：无昨收价或昨收价≤0，昨收价={prev_close}")
        log_stock_check(f"{'='*80}\n")
        return None
    
    log_stock_check(f"✓ 昨收价: {prev_close:.2f}")
    
    hist = g_history_data.get(stock)
    if not hist:
        stats['no_hist'] += 1
        log_stock_check(f"❌ 淘汰原因：无历史数据")
        log_stock_check(f"{'='*80}\n")
        return None
    
    log_stock_check(f"✓ 获取到历史数据，数据长度: {len(hist['closes'])}")
    
    closes = hist['closes']
    opens = hist['opens']
    highs = hist['highs']
    lows = hist['lows']
    volumes = hist['volumes']
    
    if len(closes) < 21:
        stats['no_hist'] += 1
        log_stock_check(f"❌ 淘汰原因：历史数据不足21天，实际: {len(closes)}天")
        log_stock_check(f"{'='*80}\n")
        return None
    
    log_stock_check(f"✓ 历史数据充足: {len(closes)}天")
    
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
        log_stock_check(f"❌ 淘汰原因：当前价格或最高价≤0")
        log_stock_check(f"  current_price: {current_price}, current_high: {current_high}")
        log_stock_check(f"{'='*80}\n")
        return None
    
    # 参考日最高涨幅
    ref_max_gain = calc_max_gain(current_high, prev_close)
    
    log_stock_check(f"\n--- 今日数据 ---")
    log_stock_check(f"  当前价: {current_price:.2f}")
    log_stock_check(f"  最高价: {current_high:.2f}")
    log_stock_check(f"  最低价: {current_low:.2f}")
    log_stock_check(f"  开盘价: {current_open:.2f}")
    log_stock_check(f"  成交量: {current_volume}")
    log_stock_check(f"  今日最高涨幅: {ref_max_gain:.2f}%")
    
    # ========== 获取有效的9个历史交易日（排除停牌） ==========
    # 注意：这里获取9天，因为要加上今天（参考日）一起算10天
    log_stock_check(f"\n--- 获取历史有效交易日（9天） ---")
    valid_days_9 = get_valid_trading_days(closes, opens, highs, lows, volumes, 9, stock_code)
    
    log_stock_check(f"获取到 {len(valid_days_9)} 个历史有效交易日")
    
    if len(valid_days_9) < 9:
        stats['no_enough_data'] += 1
        log_stock_check(f"❌ 淘汰原因：历史有效交易日不足9天，实际: {len(valid_days_9)}天")
        log_stock_check(f"{'='*80}\n")
        return None
    
    log_stock_check(f"✓ 历史有效交易日充足: {len(valid_days_9)}天")
    
    # ========== 构建今日数据（参考日），加入到10天计算中 ==========
    # 判断今日是否为阴线（盘中判断）
    today_is_yin = current_price < current_open
    
    # 计算今日阴线实体百分比（盘中计算）
    today_yin_entity_pct = 0
    if today_is_yin:
        # 如果是阴线，计算实体百分比
        today_yin_entity_pct = calc_yin_entity_percentage(current_open, current_price, prev_close, stock_code)
    
    # 判断今日是否为十字星
    today_is_cross_star = is_cross_star(current_open, current_price, prev_close)
    
    # 获取昨日数据（valid_days_9的第一个，即最近的一天）
    yesterday_max_gain = valid_days_9[0]['max_gain_today'] if valid_days_9 else 0
    
    # 今日数据
    today_data = {
        'idx': -1,  # 特殊标记，表示今天
        'open': current_open,
        'high': current_high,
        'low': current_low,
        'close': current_price,  # 盘中使用当前价作为"收盘价"
        'volume': current_volume,
        'prev_close': prev_close,
        'is_limit_down': False,  # 盘中一般不判断跌停
        'is_bare_foot_yang': False,  # 盘中不判断光脚阳
        'is_cross_star': today_is_cross_star,
        'yin_entity_percentage': today_yin_entity_pct,
        'max_gain_today': ref_max_gain,
        'max_gain_prev': yesterday_max_gain,  # 今天的前一天就是昨天
        'max_gain_next': 0  # 今天没有后一天
    }
    
    # 将今日数据插入到列表最前面，组成完整的10天
    valid_days_10 = [today_data] + valid_days_9
    
    log_stock_check(f"\n--- 构建10天完整数据 ---")
    log_stock_check(f"包含今日参考日后，共 {len(valid_days_10)} 个交易日用于计算")
    log_stock_check(f"今日是阴线: {today_is_yin}")
    log_stock_check(f"今日是十字星: {today_is_cross_star}")
    log_stock_check(f"今日阴线实体百分比: {today_yin_entity_pct:.2f}%")
    
    # ========== 条件1：最近10个交易日（含今日）的阴线次数 ≤ 3 ==========
    yin_count_10 = 0
    yin_entity_sum = 0  # 阴线实体百分比总和
    yin_days_info = []  # 记录阴线日期信息，用于后续检查
    
    log_stock_check(f"\n--- 条件1：10天阴线次数 ≤ 3（含今日） ---")
    
    for idx, day in enumerate(valid_days_10, 1):
        # 阴线：收盘价 < 开盘价，或者跌停（包括一字跌停）
        is_yin = day['close'] < day['open'] or day['is_limit_down']
        
        day_label = "今日" if idx == 1 else f"第{idx}天"
        log_stock_check(f"  {day_label}: 开{day['open']:.2f} 收{day['close']:.2f} 高{day['high']:.2f} "
            f"昨收{day['prev_close']:.2f} 最高涨幅{day['max_gain_today']:.2f}% "
            f"{'阴线' if is_yin else '阳线'} "
            f"{'跌停' if day['is_limit_down'] else ''} "
            f"{'十字星' if day['is_cross_star'] else ''}")
        
        if is_yin:
            yin_count_10 += 1
            # 累加阴线实体百分比
            yin_entity_sum += day['yin_entity_percentage']
            # 记录阴线信息（包含更多判断所需的字段）
            yin_days_info.append({
                'idx': day['idx'],
                'day_num': idx,  # 记录是第几天（1=今日）
                'max_gain_prev': day['max_gain_prev'],
                'max_gain_next': day['max_gain_next'],
                'is_cross_star': day['is_cross_star'],
                'max_gain_today': day['max_gain_today']
            })
            
            log_stock_check(f"    → 阴线实体: {day['yin_entity_percentage']:.2f}%")
    
    log_stock_check(f"条件1结果: 10天阴线次数={yin_count_10}, 阴线实体总和={yin_entity_sum:.2f}%")
    
    if yin_count_10 > 3:
        stats['fail_yin_10'] += 1
        log_stock_check(f"❌ 淘汰原因：10天阴线次数={yin_count_10} > 3")
        log_stock_check(f"{'='*80}\n")
        return None
    
    log_stock_check(f"✓ 通过条件1")
    
    # ========== 条件2：10天内（含今日） 最高>昨收*1.065 的次数>=5 ==========
    log_stock_check(f"\n--- 条件2：10天拉升次数 ≥ 5（含今日，盘中动态） ---")
    
    pull_count_10 = 0
    for idx, day in enumerate(valid_days_10, 1):
        if day['prev_close'] > 0:
            target_price = day['prev_close'] * 1.065
            # 今日使用实时最高价判断，历史日期使用历史最高价
            is_pull = day['high'] > target_price
            if is_pull:
                pull_count_10 += 1
            
            day_label = "今日(实时)" if idx == 1 else f"第{idx}天"
            log_stock_check(f"  {day_label}: 昨收{day['prev_close']:.2f} 最高{day['high']:.2f} "
                f"目标{target_price:.2f} {'✓拉升' if is_pull else '×未拉升'}")
    
    log_stock_check(f"条件2结果: 10天拉升次数={pull_count_10}")
    
    if pull_count_10 < 5:
        stats['fail_pull_10'] += 1
        log_stock_check(f"❌ 淘汰原因：10天拉升次数={pull_count_10} < 5")
        log_stock_check(f"{'='*80}\n")
        return None
    
    log_stock_check(f"✓ 通过条件2")
    
    # ========== 判断属于哪个组 ==========
    log_stock_check(f"\n--- 检查分组条件 ---")
    
    groups = []
    
    # 检查共同条件：所有阴线的前一天或后一天必须满足最高涨幅≥6.5%
    log_stock_check(f"共同条件：所有阴线前后必有大涨(≥6.5%)")
    log_stock_check(f"  共有{len(yin_days_info)}个阴线需要检查")
    
    yin_neighbor_check_pass = True
    for idx, yin_info in enumerate(yin_days_info, 1):
        # 如果是今天（day_num=1），后一天不存在，只检查前一天
        if yin_info['day_num'] == 1:
            has_big_gain = yin_info['max_gain_prev'] >= 6.5
            log_stock_check(f"  阴线{idx}(今日): 前一天涨幅{yin_info['max_gain_prev']:.2f}% "
                f"后一天涨幅[盘中无] "
                f"{'✓通过' if has_big_gain else '×不通过'}")
        else:
            has_big_gain = yin_info['max_gain_prev'] >= 6.5 or yin_info['max_gain_next'] >= 6.5
            log_stock_check(f"  阴线{idx}: 前一天涨幅{yin_info['max_gain_prev']:.2f}% "
                    f"后一天涨幅{yin_info['max_gain_next']:.2f}% "
                    f"{'✓通过' if has_big_gain else '×不通过'}")
        
        if not has_big_gain:
            yin_neighbor_check_pass = False
            break
    
    if not yin_neighbor_check_pass:
        stats['fail_yin_neighbor'] += 1
        log_stock_check(f"❌ 淘汰原因：有阴线前后都没有大涨(≥6.5%)")
        log_stock_check(f"{'='*80}\n")
        return None
    
    log_stock_check(f"✓ 通过共同条件")
    
    # 第一组条件：今日最高涨幅 > 4.5%
    log_stock_check(f"\n--- 第一组条件：今日最高涨幅 > 4.5% ---")
    log_stock_check(f"  今日最高涨幅: {ref_max_gain:.2f}%")
    
    if ref_max_gain > 4.5:
        groups.append("第一组")
        log_stock_check(f"  ✓ 满足第一组条件")
    else:
        log_stock_check(f"  × 不满足第一组条件")
    
    # 第二组条件：所有阴线满足十字星或最高涨幅≥6.5%
    log_stock_check(f"\n--- 第二组条件：所有阴线为十字星或当天涨幅≥6.5% ---")
    
    all_yin_meet_group2 = True
    for idx, yin_info in enumerate(yin_days_info, 1):
        # 阴线必须是十字星 或者 当天最高涨幅≥6.5%
        meet_condition = yin_info['is_cross_star'] or yin_info['max_gain_today'] >= 6.5
        
        log_stock_check(f"  阴线{idx}: 十字星={yin_info['is_cross_star']} "
            f"当天涨幅={yin_info['max_gain_today']:.2f}% "
            f"{'✓满足' if meet_condition else '×不满足'}")
        
        if not meet_condition:
            all_yin_meet_group2 = False
    
    if all_yin_meet_group2:
        groups.append("第二组")
        log_stock_check(f"  ✓ 满足第二组条件")
    else:
        log_stock_check(f"  × 不满足第二组条件")
    
    # 判断是否通过
    if not groups:
        stats['fail_no_group'] += 1
        log_stock_check(f"\n❌ 淘汰原因：两组条件都不满足")
        log_stock_check(f"{'='*80}\n")
        return None
    
    # 生成分组标签
    group = "+".join(groups)
    stats['pass'] += 1
    
    log_stock_check(f"\n✅ 通过所有筛选！")
    log_stock_check(f"分组: {group}")
    log_stock_check(f"统计: 10天{yin_count_10}阴{pull_count_10}拉 | 阴实体{yin_entity_sum:.2f}% | 今日最高涨幅{ref_max_gain:.2f}%")
    log_stock_check(f"{'='*80}\n")
    
    # 计算涨幅
    gain = (current_price - prev_close) / prev_close * 100
    
    # 通过条件的股票，打印详细日志
    stock_name = g_stock_names.get(stock, stock.split('.')[0])
    if PRINT_PROGRESS:
        log(f"  ✓ {stock_name}({stock.split('.')[0]}) | {group} | 10天{yin_count_10}阴{pull_count_10}拉 | 阴实体{yin_entity_sum:.2f}% | 今日最高涨幅{ref_max_gain:.2f}%", "INFO")
    
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
        'group': group,
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
            'fail_yin_10': 0, 'fail_pull_10': 0, 'fail_yin_neighbor': 0, 'fail_no_group': 0,
            'pass': 0
        }
        
        # 获取日期字符串（用于Redis保存）
        if REFERENCE_DATE:
            date_str = f"{REFERENCE_DATE[:4]}-{REFERENCE_DATE[4:6]}-{REFERENCE_DATE[6:8]}"
        else:
            date_str = current_time.strftime('%Y-%m-%d')
        
        file_date = date_str.replace('-', '')
        redis_key = f"{REDIS_KEY_PREFIX}:{file_date}"
        
        # 用于跟踪本轮扫描中删除的股票
        removed_stocks = []
        
        for stock in g_stock_pool:
            try:
                result = check_stock(stock, all_ticks, stats)
                code = stock.split('.')[0]
                name = g_stock_names.get(stock, code)
                
                if result:
                    # 股票满足条件
                    qualified.append(result)
                    ld_10 = result.get('limit_down_count_10', 0)
                    ld_20 = result.get('limit_down_count_20', 0)
                    bfy_10 = result.get('bare_foot_yang_count_10', 0)
                    ld_info_10 = f"(含{ld_10}跌停)" if ld_10 > 0 else ""
                    ld_info_20 = f"(含{ld_20}跌停)" if ld_20 > 0 else ""
                    
                    # 检查是否为新股票
                    is_new_stock = stock not in g_all_found_stocks
                    
                    if is_new_stock:
                        log(f"  ★ 新发现: {stock} {name} | {result.get('group', '未分组')} | 涨幅:{result['current_gain']}% | 最高涨幅:{result['today_max_gain']}% | 10天{result['yin_count_10']}阴{result['pull_count_10']}拉 | 阴实体{result['yin_entity_sum']:.2f}%", "SUCCESS")
                        
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
                            'group': result.get('group', '未分组'),
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
                        log(f"  ● 已存在: {stock} {name} | {result.get('group', '未分组')} | 涨幅:{result['current_gain']}% | 最高涨幅:{result['today_max_gain']}% | 10天{result['yin_count_10']}阴{result['pull_count_10']}拉 | 阴实体{result['yin_entity_sum']:.2f}%")
                else:
                    # 股票不满足条件
                    # 检查是否之前在Redis中（今日曾经满足条件，现在不满足了）
                    if stock in g_all_found_stocks:
                        # 之前满足条件，现在不满足，需要从Redis中删除
                        if delete_stock_from_redis(code, name, redis_key, max_retries=3):
                            log(f"  ▼ 剔除: {stock} {name} | 不再满足筛选条件，已从Redis删除", "WARN")
                            removed_stocks.append(stock)
                        else:
                            log(f"  ▼ 剔除失败: {stock} {name} | Redis删除失败", "ERROR")
                        
            except Exception as e:
                log(f"  检查股票异常 [{stock}]: {e}", "ERROR")
        
        # 更新已删除的股票集合
        for stock in removed_stocks:
            if stock in g_all_found_stocks:
                g_all_found_stocks.remove(stock)
        
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
        log(f"第3道淘汰(阴线前后无大涨): {stats['fail_yin_neighbor']}")
        log(f"第4道淘汰(两组条件都不满足): {stats['fail_no_group']}")
        log(f"全部通过: {stats['pass']}")
        if removed_stocks:
            log("-" * 40)
            log(f"盘中剔除(不再满足条件): {len(removed_stocks)}")
            for removed_stock in removed_stocks:
                removed_code = removed_stock.split('.')[0]
                removed_name = g_stock_names.get(removed_stock, removed_code)
                log(f"  ▼ {removed_name}({removed_code})")
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
            group_info = item.get('group', '未分组')
            log(f"【{idx}】{code} {name} - {group_info}")
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