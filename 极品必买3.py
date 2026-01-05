# -*- coding: gbk -*-
"""
QMT极品必买策略3 - 阴线小于5版
通达信公式逻辑：
COUNT(C < O, 20) <= 5 AND H>REF(C,1)*1.070

条件：
1. NIB2: 排除8和4开头
2. 今日最高 > 昨收*1.070 (涨幅7%)
3. 20天内阴线数量 <= 5

特色：计算并显示10天累计涨幅
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

# 输出文件路径
OUTPUT_FILE = r"C:\Users\lyy\Desktop\股票\预警\5拉升阴少_qmt.txt"
OUTPUT_FILE_2 = r"C:\Users\lyy\Desktop\股票\预警\5拉升阴少.txt"
STRATEGY_NAME = "极品必买3"

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
WXP_SUMMARY = "【极品必买3，阴线小于5】"

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
    log("========== 极品必买3策略初始化开始 ==========")
    log("=" * 60)
    
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
    
    # 预加载历史数据（需要20天数据）
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
    log("========== 极品必买3策略初始化完成 ==========")
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
    """获取星期"""
    weekdays = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
    now = datetime.now()
    return weekdays[now.weekday()]


def preload_history_data(ContextInfo):
    """预加载历史数据（需要25天数据用于计算20天阴线和10天涨幅）"""
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
                count=25  # 25天数据
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
        cum_gain = item['cum_gain_10d']  # 10天累计涨幅
        
        # 内容格式
        msg_content = f"提醒：买横盘突破！10天累计涨幅{cum_gain:.0f}%\n{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n时间：{time_str} {date_weekday}"
        
        # 标题格式：阴线小于5 中国卫星 10天累计涨幅50%
        summary = f"阴线小于5 {name} 10天累计涨幅{cum_gain:.0f}%"
        
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
        cum_gain = item['cum_gain_10d']
        
        # 邮件标题
        subject = f"阴线小于5 {name} 10天累计涨幅{cum_gain:.0f}%"
        # 邮件内容
        body = f"发现时间：{current_time}\n\n提醒：买横盘突破！10天累计涨幅{cum_gain:.0f}%\n{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n时间：{time_str} {date_weekday}"
        
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
    if CHECK_TRADING_TIME and not is_trading_time(current_time):
        return
    
    g_scan_count += 1
    log("")
    log("#" * 70)
    log(f"############## 第 {g_scan_count} 轮扫描开始（极品必买3） ##############")
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
    log(">>> 步骤2: 极速过滤（阴线小于5版）")
    
    qualified = []
    stats = {
        'total': len(g_stock_pool),
        'no_tick': 0, 'no_prev': 0, 'no_hist': 0,
        'fail_zf70': 0, 'fail_yinxian': 0,
        'pass': 0
    }
    
    for stock in g_stock_pool:
        result = check_stock(stock, all_ticks, stats)
        if result:
            qualified.append(result)
            log(f"  ★ 命中: {stock} | 涨幅:{result['today_max_gain']}% | 阴线:{result['yinxian_count']}天 | 10天累计涨幅:{result['cum_gain_10d']:.0f}%", "SUCCESS")
    
    total_elapsed = (datetime.now() - start_time).total_seconds()
    
    # 打印统计
    log("")
    log("=" * 60)
    log("扫描统计汇总（阴线小于5版）")
    log("=" * 60)
    log(f"总股票数: {stats['total']}")
    log(f"总扫描耗时: {total_elapsed:.2f} 秒")
    log("-" * 40)
    log(f"无行情数据: {stats['no_tick']}")
    log(f"无昨收价: {stats['no_prev']}")
    log(f"无历史数据: {stats['no_hist']}")
    log(f"第1道淘汰(涨幅<7%): {stats['fail_zf70']}")
    log(f"第2道淘汰(20天阴线>5): {stats['fail_yinxian']}")
    log(f"全部通过: {stats['pass']}")
    log("=" * 60)
    
    # 处理结果
    process_results(qualified, current_time)
    
    log("")
    log("#" * 70)
    log(f"############## 第 {g_scan_count} 轮扫描结束 ##############")
    log("#" * 70)


def check_stock(stock, all_ticks, stats):
    """
    检查单只股票
    条件：
    1. H > REF(C,1)*1.070 (今日最高涨幅>7%)
    2. COUNT(C<O, 20) <= 5 (20天内阴线<=5)
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
    
    # 第1道: H > REF(C,1)*1.070
    if today_high <= prev_close * 1.070:
        stats['fail_zf70'] += 1
        return None
    
    hist = g_history_data.get(stock)
    if not hist:
        stats['no_hist'] += 1
        return None
    
    closes = hist['closes']
    opens = hist['opens']
    
    if len(closes) < 21:
        stats['no_hist'] += 1
        return None
    
    # 第2道: COUNT(C<O, 20) <= 5
    yinxian_count = 0
    for i in range(len(closes) - 20, len(closes)):
        if closes[i] < opens[i]:
            yinxian_count += 1
    
    if yinxian_count > 5:
        stats['fail_yinxian'] += 1
        return None
    
    # 通过所有条件
    stats['pass'] += 1
    
    current_price = tick.get('lastPrice', 0)
    if not current_price:
        current_price = tick.get('price', tick.get('last', 0))
    
    volume = tick.get('volume', tick.get('vol', 0))
    
    # 最高价涨幅
    max_gain = (today_high - prev_close) / prev_close * 100
    # 当前价格涨幅
    current_gain = (current_price - prev_close) / prev_close * 100 if current_price > 0 else max_gain
    
    # 计算10天累计涨幅: (今天收盘 - 10天前收盘) / 10天前收盘 * 100
    # 使用当前价格作为今天收盘价
    close_10d_ago = closes[-11] if len(closes) >= 11 else closes[0]
    cum_gain_10d = (current_price - close_10d_ago) / close_10d_ago * 100 if close_10d_ago > 0 else 0
    
    return {
        'stock': stock,
        'today_max_gain': round(max_gain, 2),
        'current_gain': round(current_gain, 2),
        'yinxian_count': yinxian_count,
        'cum_gain_10d': round(cum_gain_10d, 2),  # 10天累计涨幅
        'current_price': current_price,
        'prev_close': round(prev_close, 2),
        'today_high': round(today_high, 2),
        'volume': volume,
        'time': datetime.now().strftime('%H:%M:%S')
    }


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
        
        qualified.sort(key=lambda x: x['cum_gain_10d'], reverse=True)
        
        for idx, item in enumerate(qualified, 1):
            code = item['stock'].split('.')[0]
            name = g_stock_names.get(item['stock'], code)
            log(f"【{idx}】{code} {name}")
            log(f"    当前涨幅: {item['current_gain']}% | 最高涨幅: {item['today_max_gain']}% | 20天阴线: {item['yinxian_count']}天 | 10天累计涨幅: {item['cum_gain_10d']:.0f}%")
        
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
