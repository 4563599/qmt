# -*- coding: gbk -*-
"""
QMT极品必买策略 - 对应通达信公式
条件：
1. ZF45: 今日最高 > 昨收*1.045 (涨幅4.5%)
2. NIB1: 10天内 最高>昨收*1.065 的次数>=5 (涨幅6.5%)
3. NIB2: 排除8和4开头
4. 10天内阴线数量<=3
5. 10天内不存在"阴线且最低价<=MA5"的情况
"""

import numpy as np
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# 全局变量
g_stock_pool = []
g_qualified_stocks = {}  # 当前符合条件的股票 {stock: info}
g_all_found_stocks = {}  # 累计发现的所有股票 {stock: first_found_time}
g_history_cache = {}
g_prev_close_cache = {}
g_scan_count = 0
g_initialized = False
g_context = None
g_lock = threading.Lock()

# 配置参数
DEBUG_MODE = True
CHECK_TRADING_TIME = False
BATCH_SIZE = 800
MAX_WORKERS = 12
PRINT_PROGRESS = True


def log(msg, level="INFO"):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {msg}")


def init(ContextInfo):
    global g_stock_pool, g_initialized, g_context, g_history_cache, g_prev_close_cache
    
    g_context = ContextInfo
    log("=" * 60)
    log("========== 策略初始化开始 ==========")
    log("=" * 60)
    
    g_stock_pool = get_stock_pool(ContextInfo)
    log(f"股票池加载完成，共 {len(g_stock_pool)} 只股票")
    
    log("")
    log(">>> 正在预加载历史数据，请稍候...")
    preload_all_data(ContextInfo)
    log(f">>> 历史数据预加载完成")
    log(f">>> 缓存股票数: {len(g_history_cache)}")
    log(f">>> 昨收价缓存: {len(g_prev_close_cache)}")
    
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
    log(f"合并去重后: {len(all_stocks)} 只")
    
    excluded_st = 0
    excluded_bse = 0
    included_main = 0
    included_kcb = 0
    
    for stock in all_stocks:
        code = stock.split('.')[0]
        
        if 'ST' in stock.upper():
            excluded_st += 1
            continue
        # NIB2: 排除8和4开头（北交所）
        if code.startswith('8') or code.startswith('4'):
            excluded_bse += 1
            continue
        
        stock_list.append(stock)
        
        if code.startswith('688'):
            included_kcb += 1
        else:
            included_main += 1
    
    log(f"筛选结果:")
    log(f"  - 主板/创业板: {included_main} 只")
    log(f"  - 科创板(688): {included_kcb} 只")
    log(f"  - 排除ST: {excluded_st} 只")
    log(f"  - 排除北交所(8/4开头): {excluded_bse} 只")
    log(f"  - 最终股票池: {len(stock_list)} 只")
    
    return stock_list


def preload_all_data(ContextInfo):
    global g_history_cache, g_prev_close_cache
    
    batch_size = 300
    total = len(g_stock_pool)
    success_count = 0
    fail_count = 0
    
    log(f"开始批量加载，共 {total} 只股票，批次大小 {batch_size}")
    
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
                        
                        if stock_data is not None and len(stock_data) >= 2:
                            g_history_cache[stock] = stock_data
                            g_prev_close_cache[stock] = float(stock_data['close'].iloc[-2])
                            success_count += 1
                        else:
                            fail_count += 1
                    except:
                        fail_count += 1
            else:
                log(f"  批次{batch_num}批量获取失败，尝试逐个获取...", "WARN")
                for stock in batch:
                    try:
                        d = ContextInfo.get_market_data(['open','high','low','close'], [stock], period='1d', count=15)
                        if d is not None and len(d) >= 2:
                            g_history_cache[stock] = d
                            g_prev_close_cache[stock] = float(d['close'].iloc[-2])
                            success_count += 1
                        else:
                            fail_count += 1
                    except:
                        fail_count += 1
                        
        except Exception as e:
            log(f"  批次{batch_num}异常: {str(e)}", "ERROR")
            for stock in batch:
                try:
                    d = ContextInfo.get_market_data(['open','high','low','close'], [stock], period='1d', count=15)
                    if d is not None and len(d) >= 2:
                        g_history_cache[stock] = d
                        g_prev_close_cache[stock] = float(d['close'].iloc[-2])
                        success_count += 1
                    else:
                        fail_count += 1
                except:
                    fail_count += 1
        
        progress = min(i + batch_size, total)
        pct = progress * 100 // total
        log(f"  [预加载] 批次 {batch_num}/{total_batches} | 进度: {progress}/{total} ({pct}%) | 成功: {success_count} | 失败: {fail_count}")
    
    log(f"预加载完成: 成功 {success_count} 只, 失败 {fail_count} 只")


def handlebar(ContextInfo):
    pass


def scan_stocks(ContextInfo):
    global g_qualified_stocks, g_scan_count
    
    current_time = datetime.now()
    if CHECK_TRADING_TIME and not is_trading_time(current_time):
        log(f"当前非交易时间，跳过扫描", "WARN")
        return
    
    g_scan_count += 1
    log("")
    log("#" * 70)
    log(f"############## 第 {g_scan_count} 轮扫描开始 ##############")
    log("#" * 70)
    
    start_time = datetime.now()
    
    log("")
    log(">>> 步骤1: 获取实时行情")
    all_ticks = {}
    tick_success = 0
    tick_fail = 0
    
    total_batches = (len(g_stock_pool) + BATCH_SIZE - 1) // BATCH_SIZE
    
    for i in range(0, len(g_stock_pool), BATCH_SIZE):
        batch = g_stock_pool[i:i+BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        
        try:
            ticks = ContextInfo.get_full_tick(batch)
            if ticks:
                all_ticks.update(ticks)
                tick_success += len(ticks)
            else:
                tick_fail += len(batch)
        except Exception as e:
            log(f"  批次{batch_num}获取行情异常: {str(e)}", "ERROR")
            tick_fail += len(batch)
        
        log(f"  [行情] 批次 {batch_num}/{total_batches} | 本批: {len(batch)} 只 | 累计获取: {len(all_ticks)} 只")
    
    log(f">>> 行情获取完成: 成功 {tick_success} 只, 失败 {tick_fail} 只")
    
    log("")
    log(">>> 步骤2: 并行筛选股票（通达信公式逻辑）")
    qualified = []
    stats = {'no_tick': 0, 'no_prev': 0, 'no_hist': 0, 'fail_zf45': 0, 'fail_nib1': 0, 'fail_yinxian': 0, 'fail_ma5': 0, 'pass': 0}
    processed = [0]
    
    def check_stock(stock):
        """检查单只股票 - 对应通达信公式"""
        if stock not in all_ticks:
            return None, 'no_tick'
        
        tick = all_ticks[stock]
        prev_close = g_prev_close_cache.get(stock)
        
        if not prev_close or prev_close <= 0:
            return None, 'no_prev'
        
        today_high = tick.get('high', 0)
        if not today_high or today_high <= 0:
            return None, 'no_tick'
        
        # 条件1: ZF45 - 今日最高 > 昨收*1.045 (涨幅4.5%)
        if today_high <= prev_close * 1.045:
            return None, 'fail_zf45'
        
        # 获取历史数据
        hist = g_history_cache.get(stock)
        if hist is None or len(hist) < 11:
            return None, 'no_hist'
        
        # 条件2: COUNT(NIB1,10)>=5 - 10天内涨幅达6.5%的次数>=5
        count_65 = check_nib1_count(hist)
        if count_65 < 5:
            return None, 'fail_nib1'
        
        # 条件3: COUNT(C<O,10)<=3 - 10天内阴线数量<=3
        yinxian_count = check_yinxian_count(hist)
        if yinxian_count > 3:
            return None, 'fail_yinxian'
        
        # 条件4: COUNT(C<O AND L<=MA5, 10)=0 - 10天内不存在"阴线且最低价<=MA5"
        if not check_ma5_condition(hist):
            return None, 'fail_ma5'
        
        # 通过所有条件
        current_price = tick.get('lastPrice', 0)
        if not current_price:
            current_price = tick.get('price', tick.get('last', 0))
        
        max_gain = (today_high - prev_close) / prev_close * 100
        
        return {
            'stock': stock,
            'today_max_gain': round(max_gain, 2),
            'gain_65_count': count_65,
            'yinxian_count': yinxian_count,
            'current_price': current_price,
            'prev_close': round(prev_close, 2),
            'today_high': round(today_high, 2),
            'time': current_time.strftime('%H:%M:%S')
        }, 'pass'
    
    log(f"  启动 {MAX_WORKERS} 个线程并行处理...")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_stock, stock): stock for stock in g_stock_pool}
        
        for future in as_completed(futures):
            try:
                result, stat_key = future.result()
                with g_lock:
                    processed[0] += 1
                    stats[stat_key] = stats.get(stat_key, 0) + 1
                    
                    if result:
                        qualified.append(result)
                        stock = result['stock']
                        log(f"  ★ 命中: {stock} | 涨幅:{result['today_max_gain']}% | 6.5%次数:{result['gain_65_count']} | 阴线:{result['yinxian_count']}天", "SUCCESS")
                    
                    if processed[0] % 1000 == 0:
                        log(f"  [筛选进度] {processed[0]}/{len(g_stock_pool)} | 当前命中: {len(qualified)} 只")
            except Exception as e:
                pass
    
    end_time = datetime.now()
    elapsed = (end_time - start_time).total_seconds()
    
    log("")
    log("=" * 60)
    log("扫描统计汇总（对应通达信公式条件）")
    log("=" * 60)
    log(f"总股票数: {len(g_stock_pool)}")
    log(f"扫描耗时: {elapsed:.2f} 秒")
    log(f"扫描速度: {len(g_stock_pool)/elapsed:.0f} 只/秒")
    log("-" * 40)
    log(f"无行情数据: {stats['no_tick']}")
    log(f"无昨收价: {stats['no_prev']}")
    log(f"无历史数据: {stats['no_hist']}")
    log(f"条件1淘汰(ZF45: 今日涨幅<4.5%): {stats['fail_zf45']}")
    log(f"条件2淘汰(NIB1: 6.5%次数<5): {stats['fail_nib1']}")
    log(f"条件3淘汰(阴线数量>3): {stats['fail_yinxian']}")
    log(f"条件4淘汰(阴线跌破MA5): {stats['fail_ma5']}")
    log(f"全部通过: {stats['pass']}")
    log("=" * 60)
    
    # 检查新增股票
    new_stocks = []
    current_time_str = current_time.strftime('%H:%M:%S')
    
    for item in qualified:
        stock = item['stock']
        if stock not in g_all_found_stocks:
            g_all_found_stocks[stock] = current_time_str
            new_stocks.append(item)
    
    # 打印本轮结果
    if qualified:
        log("")
        log("*" * 70)
        log(f"★★★ 本轮发现 {len(qualified)} 只符合条件的股票 ★★★")
        log("*" * 70)
        
        qualified.sort(key=lambda x: x['today_max_gain'], reverse=True)
        
        for idx, item in enumerate(qualified, 1):
            code = item['stock'].split('.')[0]
            board = "科创" if code.startswith('688') else "主板"
            log("")
            log(f"【{idx}】{item['stock']} ({board})")
            log(f"    昨收价: {item['prev_close']} | 今日最高: {item['today_high']}")
            log(f"    今日最高涨幅: {item['today_max_gain']}%")
            log(f"    10天内6.5%达标次数: {item['gain_65_count']} 次")
            log(f"    10天内阴线数量: {item['yinxian_count']} 天")
            log(f"    当前价格: {item['current_price']}")
            log(f"    发现时间: {item['time']}")
        
        log("")
        log("*" * 70)
        g_qualified_stocks = {item['stock']: item for item in qualified}
    else:
        log("")
        log("本轮扫描未发现符合条件的股票")
    
    # 打印新增股票
    if new_stocks:
        log("")
        log("+" * 70)
        log(f"【新增】本轮新发现 {len(new_stocks)} 只股票:")
        log("+" * 70)
        for item in new_stocks:
            log(f"  ★ 新增: {item['stock']} | 首次发现时间: {current_time_str} | 涨幅: {item['today_max_gain']}%", "NEW")
        log("+" * 70)
    
    # 打印累计股票集合
    log("")
    log("=" * 70)
    log(f"【累计】今日共发现 {len(g_all_found_stocks)} 只符合条件的股票:")
    log("=" * 70)
    if g_all_found_stocks:
        # 按首次发现时间排序
        sorted_stocks = sorted(g_all_found_stocks.items(), key=lambda x: x[1])
        for stock, first_time in sorted_stocks:
            code = stock.split('.')[0]
            board = "科创" if code.startswith('688') else "主板"
            log(f"  {stock} ({board}) - 首次发现: {first_time}")
    log("=" * 70)
    
    log("")
    log("#" * 70)
    log(f"############## 第 {g_scan_count} 轮扫描结束 ##############")
    log("#" * 70)
    log("")


def check_nib1_count(hist_data):
    """
    COUNT(NIB1,10)>=5
    NIB1 = H > REF(C,1)*1.065
    10天内最高价>昨收*1.065的次数
    """
    closes = hist_data['close'].values
    highs = hist_data['high'].values
    count = 0
    
    # 取最近10天（不含今天）
    for i in range(len(closes) - 10, len(closes)):
        if i >= 1:
            prev_close = closes[i-1]
            if prev_close > 0 and highs[i] > prev_close * 1.065:
                count += 1
    
    return count


def check_yinxian_count(hist_data):
    """
    COUNT(C<O,10)<=3
    10天内阴线数量
    """
    opens = hist_data['open'].values[-10:]
    closes = hist_data['close'].values[-10:]
    count = 0
    
    for i in range(len(opens)):
        if closes[i] < opens[i]:
            count += 1
    
    return count


def check_ma5_condition(hist_data):
    """
    COUNT(C<O AND L<=MA5, 10)=0
    10天内不存在"阴线且最低价<=MA5"的情况
    """
    opens = hist_data['open'].values
    closes = hist_data['close'].values
    lows = hist_data['low'].values
    
    # 计算每天的MA5
    for i in range(len(closes) - 10, len(closes)):
        if i >= 4:
            ma5 = np.mean(closes[i-4:i+1])
            # 检查是否是阴线且最低价<=MA5
            if closes[i] < opens[i] and lows[i] <= ma5:
                return False
    
    return True


def is_trading_time(dt):
    t = dt.time()
    return (datetime.strptime("09:30", "%H:%M").time() <= t <= datetime.strptime("11:30", "%H:%M").time()) or \
           (datetime.strptime("13:00", "%H:%M").time() <= t <= datetime.strptime("15:00", "%H:%M").time())


def clear_cache(ContextInfo):
    global g_history_cache, g_prev_close_cache, g_qualified_stocks, g_scan_count, g_all_found_stocks
    g_history_cache = {}
    g_prev_close_cache = {}
    g_qualified_stocks = {}
    g_all_found_stocks = {}
    g_scan_count = 0
    log("缓存已清理，扫描计数已重置，累计股票已清空")


def manual_scan(ContextInfo):
    log(">>> 手动触发扫描")
    scan_stocks(ContextInfo)
