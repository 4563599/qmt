# -*- coding: gbk -*-
"""
QMT极品必买策略4 - 涨停板选股
通达信公式逻辑：
连续3天满足以下条件 AND 今日涨停：
  基础条件：L > MA(C,5)
  满足以下任一条件：
    条件1：十字星（收盘价和开盘价幅度<=1.2%）且下引线<1.3%
    条件2：阳线且下引线<实体
    条件3：涨停板

涨停板判断：
  ST股票：5%
  科创板(688)、创业板注册制(300且上市>=20200824)、北交所(8/43/83开头)：20%
  其他：10%
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

# ========== 写入文件开关 ==========
WRITE_FILE_ENABLED = True  # True=写入文件，False=不写入（测试模式）

# ========== 历史选股日期 ==========
# 设置日期进行历史选股，格式：20260105，为空则使用实时数据
BACKTEST_DATE = ""  # 例如："20260105" 或 ""

# 输出文件路径
OUTPUT_FILE = r"C:\Users\lyy\Desktop\股票\预警\5拉升阴少_qmt.txt"
OUTPUT_FILE_2 = r"C:\Users\lyy\Desktop\股票\预警\5拉升阴少.txt"
STRATEGY_NAME = "极品必买4"

# ========== QQ邮箱配置 ==========
EMAIL_ENABLED = True  # 开启邮件通知
EMAIL_RECEIVERS = ["4563599@qq.com"]
EMAIL_SENDER = "4563599@qq.com"
EMAIL_AUTH_CODE = "iruvzesxztercaic"
EMAIL_SMTP_SERVER = "smtp.qq.com"
EMAIL_SMTP_PORT = 465

# ========== WxPusher微信推送配置 ==========
WXPUSHER_ENABLED = True  # 开启微信推送
WXP_TOKEN = "AT_mUKiCaJyUxhf0V1K9X3MuhqDaLmguFY4"
WXP_UID = "UID_d5iiHewR5VxqIWV3t2vkumOkykre"
WXP_SUMMARY = "【极品必买4，涨停板】"

g_email_sent_stocks = set()


def log(msg, level="INFO"):
    if not LOG_ENABLED:
        return
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {msg}")


def init(ContextInfo):
    global g_stock_pool, g_initialized, g_context
    
    g_context = ContextInfo
    log("=" * 60)
    log("========== 极品必买4策略初始化开始 ==========")
    log("=" * 60)
    
    # 订阅全市场行情
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
    
    # 预加载历史数据（需要8天数据：前3天+今天+MA5计算）
    log("")
    log(">>> 正在预加载历史数据...")
    preload_history_data(ContextInfo)
    log(f">>> 历史数据预加载完成: {len(g_history_data)} 只")
    
    ContextInfo.run_time("scan_stocks", "3nSecond", "2024-01-01 09:30:00", "SH")
    log("定时器设置完成，每3秒扫描一次")
    
    log("")
    log(f">>> 写入文件开关: {'开启' if WRITE_FILE_ENABLED else '关闭（测试模式）'}")
    log(f">>> 发送邮件开关: {'开启' if EMAIL_ENABLED else '关闭'}")
    log(f">>> 微信推送开关: {'开启' if WXPUSHER_ENABLED else '关闭'}")
    
    g_initialized = True
    log("=" * 60)
    log("========== 极品必买4策略初始化完成 ==========")
    log("=" * 60)
    log("")
    
    log(">>> 执行首次扫描...")
    scan_stocks(ContextInfo)


def get_stock_pool(ContextInfo):
    """获取股票池，排除ST股票"""
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
    
    # 排除ST股票
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
    """预加载历史数据（需要8天数据）"""
    global g_history_data, g_prev_close_cache
    
    batch_size = 300
    total = len(g_stock_pool)
    success_count = 0
    
    # 如果设置了回测日期，使用指定日期；否则使用实时数据
    if BACKTEST_DATE:
        log(f">>> 使用历史日期选股: {BACKTEST_DATE}")
        end_date = BACKTEST_DATE
    else:
        log(f">>> 使用实时数据选股")
        end_date = ""
    
    for i in range(0, total, batch_size):
        batch = g_stock_pool[i:i+batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size
        
        try:
            # 如果有回测日期，使用end参数
            if end_date:
                data = ContextInfo.get_market_data(
                    ['open', 'high', 'low', 'close', 'volume'], 
                    batch, 
                    period='1d', 
                    count=8,
                    end_time=end_date
                )
            else:
                data = ContextInfo.get_market_data(
                    ['open', 'high', 'low', 'close', 'volume'], 
                    batch, 
                    period='1d', 
                    count=8
                )
            
            if data is not None:
                for stock in batch:
                    try:
                        stock_data = extract_stock_data(data, stock)
                        if stock_data is not None and len(stock_data) >= 4:
                            g_history_data[stock] = {
                                'closes': stock_data['close'].values.astype(float),
                                'opens': stock_data['open'].values.astype(float),
                                'highs': stock_data['high'].values.astype(float),
                                'lows': stock_data['low'].values.astype(float),
                                'volumes': stock_data['volume'].values.astype(float)
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



def get_limit_pct(stock, stock_name):
    """
    计算涨停幅度
    ST股票：5%
    科创板(688)、创业板注册制(300且上市>=20200824)、北交所(8/43/83开头)：20%
    其他：10%
    """
    code = stock.split('.')[0]
    
    # ST股票
    if 'ST' in stock_name.upper() or '*ST' in stock_name.upper():
        return 5.0
    
    # 科创板
    if code.startswith('688'):
        return 20.0
    
    # 创业板注册制（简化判断：300开头默认20%）
    if code.startswith('300'):
        return 20.0
    
    # 北交所
    if code.startswith('8') or code.startswith('43') or code.startswith('83'):
        return 20.0
    
    # 其他
    return 10.0


def check_limit_board(prev_close, current_close, current_high, volume, limit_pct):
    """
    判断是否涨停
    1. 涨幅 >= 涨停幅度*0.99
    2. 封板：收盘价 >= 最高价*0.998
    3. 有成交量
    4. 非停牌（昨收价>0）
    """
    if prev_close <= 0 or volume <= 0:
        return False
    
    # 涨幅
    pct = (current_close - prev_close) / prev_close * 100
    pct_ok = pct >= (limit_pct * 0.99)
    
    # 封板
    sealed = current_close >= current_high * 0.998
    
    return pct_ok and sealed


def check_condition1(o, c, l):
    """
    条件1：十字星且下引线<1.3%
    十字星：|C-O|/O <= 1.2%
    下引线：MIN(C,O)-L
    """
    if o <= 0:
        return False
    
    # 十字星
    cross_star = abs(c - o) / o * 100 <= 1.2
    
    # 下引线
    lower_shadow = min(c, o) - l
    lower_shadow_pct = lower_shadow / o * 100
    
    return cross_star and lower_shadow_pct < 1.3


def check_condition2(o, c, l):
    """
    条件2：阳线且下引线<实体
    阳线：C > O
    下引线：MIN(C,O)-L
    实体：|C-O|
    """
    if c <= o:
        return False
    
    lower_shadow = min(c, o) - l
    body = abs(c - o)
    
    return lower_shadow < body


def check_stock(stock, all_ticks, stats):
    """
    检查单只股票
    条件：连续3天满足（L>MA5 AND (条件1 OR 条件2 OR 条件3)）AND 今日涨停
    """
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
    
    if len(closes) < 4:
        stats['no_hist'] += 1
        return None
    
    # 获取今日数据
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
    
    # 获取股票名称和涨停幅度
    stock_name = g_stock_names.get(stock, '')
    limit_pct = get_limit_pct(stock, stock_name)
    
    # 判断今日是否涨停（条件3）
    today_is_limit = check_limit_board(prev_close, current_price, current_high, current_volume, limit_pct)
    
    if not today_is_limit:
        stats['fail_not_limit'] += 1
        return None
    
    # 检查连续3天条件
    # 今天（索引-1）、昨天（索引-2）、前天（索引-3）
    days_ok = 0
    
    for day_offset in [1, 2, 3]:
        idx = len(closes) - day_offset
        if idx < 1:
            break
        
        o = opens[idx]
        h = highs[idx]
        l = lows[idx]
        c = closes[idx]
        v = volumes[idx]
        prev_c = closes[idx - 1]
        
        # 基础条件：L > MA5
        if idx >= 4:
            ma5 = np.mean(closes[idx-4:idx+1])
            base_cond = l > ma5
        else:
            base_cond = False
        
        if not base_cond:
            break
        
        # 条件1：十字星且下引线<1.3%
        cond1 = check_condition1(o, c, l)
        
        # 条件2：阳线且下引线<实体
        cond2 = check_condition2(o, c, l)
        
        # 条件3：涨停
        cond3 = check_limit_board(prev_c, c, h, v, limit_pct)
        
        # 满足任一条件
        any_cond = cond1 or cond2 or cond3
        
        if any_cond:
            days_ok += 1
        else:
            break
    
    if days_ok < 3:
        stats['fail_3days'] += 1
        return None
    
    # 通过所有条件
    stats['pass'] += 1
    
    # 涨幅
    gain = (current_price - prev_close) / prev_close * 100
    
    return {
        'stock': stock,
        'current_gain': round(gain, 2),
        'limit_pct': limit_pct,
        'current_price': current_price,
        'prev_close': round(prev_close, 2),
        'volume': current_volume,
        'time': datetime.now().strftime('%H:%M:%S')
    }


def push_to_wechat(stocks):
    """通过 WxPusher 推送微信消息"""
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
        
        msg_content = f"{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n时间：{time_str} {date_weekday}"
        summary = f"涨停板 {name} {gain}"
        
        log(f">>> 推送内容: {msg_content}")
        
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
                log(f">>> 微信推送成功: {name} {code}")
            else:
                log(f">>> 微信推送失败: {res.get('msg')}", "WARN")
        except Exception as e:
            log(f">>> 微信推送异常: {e}", "ERROR")


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
        time_str = item['time']
        
        subject = f"涨停板 {name} {gain}"
        body = f"发现时间：{current_time}\n\n{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n时间：{time_str} {date_weekday}"
        
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


def write_stocks_to_file(stocks, date_str, time_str):
    """写入文件"""
    if not WRITE_FILE_ENABLED:
        log(">>> 写入文件已关闭（测试模式），跳过写入")
        return
    
    for output_file in [OUTPUT_FILE, OUTPUT_FILE_2]:
        try:
            with open(output_file, 'a', encoding='gbk') as f:
                for item in stocks:
                    code = item['stock'].split('.')[0]
                    stock_name = g_stock_names.get(item['stock'], code)
                    price = item['current_price']
                    gain = item['current_gain']
                    volume = item.get('volume', 0)
                    line = f"{code}\t{stock_name}\t{date_str} {time_str}\t{price:.2f}\t{gain:.2f}%\t{volume}\t{STRATEGY_NAME}\n"
                    f.write(line)
            log(f">>> 已写入文件: {output_file}")
        except Exception as e:
            log(f">>> 写入文件失败 [{output_file}]: {e}", "ERROR")


def handlebar(ContextInfo):
    pass


def scan_stocks(ContextInfo):
    """扫描股票"""
    global g_qualified_stocks, g_scan_count
    
    current_time = datetime.now()
    
    # 如果设置了回测日期，跳过交易时间检查
    if not BACKTEST_DATE and CHECK_TRADING_TIME and not is_trading_time(current_time):
        return
    
    g_scan_count += 1
    log("")
    log("#" * 70)
    if BACKTEST_DATE:
        log(f"############## 历史选股 {BACKTEST_DATE} ##############")
    else:
        log(f"############## 第 {g_scan_count} 轮扫描开始（极品必买4） ##############")
    log("#" * 70)
    
    start_time = datetime.now()
    
    # 获取实时行情（如果是回测模式，使用历史数据的最后一天作为"今日"）
    log("")
    if BACKTEST_DATE:
        log(">>> 步骤1: 使用历史数据作为今日行情")
        # 回测模式：从历史数据中提取最后一天作为"今日"
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
            log(f">>> 行情获取失败: {e}", "ERROR")
            all_ticks = {}
    
    if not all_ticks:
        log(">>> 无行情数据，跳过本轮扫描", "WARN")
        return
    
    # 极速过滤
    log("")
    log(">>> 步骤2: 极速过滤（涨停板版）")
    
    qualified = []
    stats = {
        'total': len(g_stock_pool),
        'no_tick': 0, 'no_prev': 0, 'no_hist': 0,
        'fail_not_limit': 0, 'fail_3days': 0,
        'pass': 0
    }
    
    for stock in g_stock_pool:
        result = check_stock(stock, all_ticks, stats)
        if result:
            qualified.append(result)
            code = stock.split('.')[0]
            name = g_stock_names.get(stock, code)
            log(f"  ★ 命中: {stock} {name} | 涨幅:{result['current_gain']}% | 涨停幅度:{result['limit_pct']}%", "SUCCESS")
    
    total_elapsed = (datetime.now() - start_time).total_seconds()
    
    # 打印统计
    log("")
    log("=" * 60)
    log("扫描统计汇总（涨停板版）")
    log("=" * 60)
    log(f"总股票数: {stats['total']}")
    log(f"总扫描耗时: {total_elapsed:.2f} 秒")
    log("-" * 40)
    log(f"无行情数据: {stats['no_tick']}")
    log(f"无昨收价: {stats['no_prev']}")
    log(f"无历史数据: {stats['no_hist']}")
    log(f"第1道淘汰(今日未涨停): {stats['fail_not_limit']}")
    log(f"第2道淘汰(连续3天条件不满足): {stats['fail_3days']}")
    log(f"全部通过: {stats['pass']}")
    log("=" * 60)
    
    # 处理结果
    process_results(qualified, current_time)
    
    log("")
    log("#" * 70)
    log(f"############## 第 {g_scan_count} 轮扫描结束 ##############")
    log("#" * 70)


def process_results(qualified, current_time):
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
        push_to_wechat(new_stocks)
        send_email(new_stocks)
        write_stocks_to_file(new_stocks, date_str, time_str)
    
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
            log(f"    涨幅: {item['current_gain']}% | 涨停幅度: {item['limit_pct']}%")
        
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
