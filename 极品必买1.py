# -*- coding: gbk -*-
"""
QMT极品必买策略 - 优化版
优化点：
1. 全量行情获取：subscribe_whole_quote + get_full_tick(全部股票) 一次性获取
2. 增量更新：历史数据只加载一次，扫描时只用实时数据
3. 极速过滤：按淘汰率排序条件判断，90%股票在前两步被淘汰

条件：
1. NIB2: 排除8和4开头（最快，字符串判断，初始化时过滤）
2. ZF45: 今日最高 > 昨收*1.045（极快，只需tick数据）
3. NIB1: 10天内 最高>昨收*1.065 的次数>=5
4. 10天内阴线数量<=3
5. 10天内不存在"阴线且最低价<=MA5"的情况
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
g_stock_pool = []  # 预过滤后的股票池（已排除8/4开头）
g_qualified_stocks = {}
g_all_found_stocks = {}
g_scan_count = 0
g_initialized = False
g_context = None
g_lock = threading.Lock()

# ========== 优化2: 历史数据缓存（只加载一次） ==========
g_history_data = {}  # {stock: {'closes': array, 'opens': array, 'highs': array, 'lows': array}}
g_prev_close_cache = {}  # {stock: prev_close}
g_stock_names = {}  # {stock: name} 股票名称缓存

# 配置参数
DEBUG_MODE = True
CHECK_TRADING_TIME = True  # 开启交易时间检查，收盘后停止执行
PRINT_PROGRESS = True
LOG_ENABLED = False  # 日志开关，False=关闭日志打印

# 输出文件路径
OUTPUT_FILE = r"C:\Users\lyy\Desktop\股票\预警\5拉升阴少_qmt.txt"
OUTPUT_FILE_2 = r"C:\Users\lyy\Desktop\股票\预警\5拉升阴少.txt"  # 第二个输出文件
STRATEGY_NAME = "极品必买1"  # 策略名称，写入文件时使用

# ========== QQ邮箱配置 ==========
EMAIL_ENABLED = True  # 是否启用邮件发送，True=发送，False=不发送
EMAIL_RECEIVERS = ["4563599@qq.com"]  # 接收邮件的邮箱列表，可添加多个
EMAIL_SENDER = "4563599@qq.com"  # 发送邮件的QQ邮箱
EMAIL_AUTH_CODE = "iruvzesxztercaic"  # QQ邮箱授权码
EMAIL_SMTP_SERVER = "smtp.qq.com"
EMAIL_SMTP_PORT = 465

# ========== WxPusher微信推送配置 ==========
WXPUSHER_ENABLED = True  # 是否启用微信推送
WXP_TOKEN = "AT_mUKiCaJyUxhf0V1K9X3MuhqDaLmguFY4"
WXP_UID = "UID_d5iiHewR5VxqIWV3t2vkumOkykre"
WXP_SUMMARY = "【极品必买1，阴线都不碰】"

# 已发送邮件的股票记录（程序运行期间）
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
    log("========== 策略初始化开始（优化版） ==========")
    log("=" * 60)
    
    # ========== 优化1: 订阅全市场行情 ==========
    log(">>> 订阅全市场行情...")
    try:
        ContextInfo.subscribe_whole_quote(["SH", "SZ"])
        log(">>> 全市场行情订阅成功 (SH, SZ)")
    except Exception as e:
        log(f">>> 全市场行情订阅失败: {e}", "WARN")
    
    # 获取股票池（预过滤8/4开头）
    g_stock_pool = get_stock_pool(ContextInfo)
    log(f"股票池加载完成，共 {len(g_stock_pool)} 只股票（已排除8/4开头）")
    
    # 预加载股票名称
    log("")
    log(">>> 正在预加载股票名称...")
    preload_stock_names(ContextInfo)
    log(f">>> 股票名称加载完成: {len(g_stock_names)} 只")
    
    # ========== 优化2: 只加载一次历史数据 ==========
    log("")
    log(">>> 正在预加载历史数据（只加载一次）...")
    preload_history_data(ContextInfo)
    log(f">>> 历史数据预加载完成")
    log(f">>> 缓存股票数: {len(g_history_data)}")
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
    """获取股票池，预过滤8/4开头（NIB2条件前置）"""
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
        # NIB2: 排除8和4开头（北交所）- 在初始化时就过滤掉
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


def preload_history_data(ContextInfo):
    """
    优化2: 只加载一次历史数据到内存
    历史数据（前14天）在交易日内不变，无需重复获取
    """
    global g_history_data, g_prev_close_cache
    
    batch_size = 300
    total = len(g_stock_pool)
    success_count = 0
    fail_count = 0
    
    log(f"开始批量加载历史数据，共 {total} 只股票")
    
    for i in range(0, total, batch_size):
        batch = g_stock_pool[i:i+batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size
        
        try:
            # 获取15天数据（含今天，用于计算MA5等）
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
                            # 存储为numpy数组，加速后续计算
                            g_history_data[stock] = {
                                'closes': stock_data['close'].values.astype(float),
                                'opens': stock_data['open'].values.astype(float),
                                'highs': stock_data['high'].values.astype(float),
                                'lows': stock_data['low'].values.astype(float)
                            }
                            # 昨收价 = 倒数第二天的收盘价
                            g_prev_close_cache[stock] = float(stock_data['close'].iloc[-2])
                            success_count += 1
                        else:
                            fail_count += 1
                    except:
                        fail_count += 1
            else:
                fail_count += len(batch)
                        
        except Exception as e:
            log(f"  批次{batch_num}异常: {str(e)}", "ERROR")
            fail_count += len(batch)
        
        if PRINT_PROGRESS:
            progress = min(i + batch_size, total)
            pct = progress * 100 // total
            log(f"  [预加载] 批次 {batch_num}/{total_batches} | 进度: {progress}/{total} ({pct}%) | 成功: {success_count}")
    
    log(f"预加载完成: 成功 {success_count} 只, 失败 {fail_count} 只")


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


def preload_stock_names(ContextInfo):
    """预加载股票名称"""
    global g_stock_names
    
    for stock in g_stock_pool:
        try:
            name = ContextInfo.get_stock_name(stock)
            if name:
                g_stock_names[stock] = name
            else:
                g_stock_names[stock] = stock.split('.')[0]
        except:
            g_stock_names[stock] = stock.split('.')[0]


def get_date_with_weekday():
    """获取星期，格式：星期日"""
    weekdays = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
    now = datetime.now()
    return weekdays[now.weekday()]


def push_to_wechat(stocks):
    """通过 WxPusher 推送微信消息"""
    log(f">>> 准备微信推送，共 {len(stocks)} 只股票")
    log(f">>> WXPUSHER_ENABLED: {WXPUSHER_ENABLED}")
    log(f">>> requests模块: {'已加载' if requests else '未加载'}")
    
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
        
        # 格式：【极品必买1，阴线都不碰】\n股票：万集科技 (300552)\n涨幅：2.38%\n时间：10:30:00 星期日 12月28日
        msg_content = f"{WXP_SUMMARY}\n股票：{name} ({code})\n涨幅：{gain}\n时间：{time_str} {date_weekday}"
        
        log(f">>> 推送内容: {msg_content}")
        log(f">>> WXP_TOKEN: {WXP_TOKEN[:20]}...")
        log(f">>> WXP_UID: {WXP_UID}")
        
        url = "https://wxpusher.zjiecode.com/api/send/message"
        payload = {
            "appToken": WXP_TOKEN,
            "content": msg_content,
            "summary": f"阴线都不碰 {name} {time_str} {date_weekday}",
            "contentType": 1,
            "uids": [WXP_UID]
        }
        
        try:
            log(f">>> 正在发送请求到: {url}")
            response = requests.post(url, json=payload, timeout=10)
            log(f">>> HTTP状态码: {response.status_code}")
            log(f">>> 响应内容: {response.text}")
            
            res = response.json()
            if res.get('code') == 1000:
                log(f">>> 微信推送成功: {name} {code}")
            else:
                log(f">>> 微信推送失败: code={res.get('code')}, msg={res.get('msg')}", "WARN")
        except Exception as e:
            log(f">>> 微信推送异常: {type(e).__name__}: {e}", "ERROR")


def send_sms_tencent(stocks):
    """
    通过QQ邮箱发送邮件通知
    stocks: 新增股票列表
    每只股票单独发送一封邮件
    """
    global g_email_sent_stocks
    
    if not EMAIL_ENABLED:
        return
    
    # 过滤出未发送过邮件的股票
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
    
    # 每只股票单独发送一封邮件
    for item in new_stocks_to_send:
        code = item['stock'].split('.')[0]
        name = g_stock_names.get(item['stock'], code)
        gain = f"{item['current_gain']:.2f}%"
        time_str = item['time']
        
        # 邮件标题：阴线都不碰 万集科技 10:30:00 星期日
        subject = f"阴线都不碰 {name} {time_str} {date_weekday}"
        # 邮件内容
        body = f"发现时间：{current_time}\n\n【{STRATEGY_NAME}，阴线都不碰】\n股票：{name} ({code})\n涨幅：{gain}\n时间：{time_str} {date_weekday}"
        
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
    """
    将新增股票写入文件
    格式：代码\t名称\t日期 时间\t价格\t涨幅%\t成交量\t策略名称
    示例：001331	胜通能源	2025-12-26 09:29	42.10	10.01%	77994	极品必买1
    """
    # 写入两个文件
    for output_file in [OUTPUT_FILE, OUTPUT_FILE_2]:
        try:
            with open(output_file, 'a', encoding='gbk') as f:
                for item in stocks:
                    code = item['stock'].split('.')[0]
                    # 从缓存获取股票名称
                    stock_name = g_stock_names.get(item['stock'], code)
                    price = item['current_price']
                    gain = item['current_gain']  # 使用当前价格涨幅
                    volume = item.get('volume', 0)
                    
                    # 单行格式：代码\t名称\t日期 时间\t价格\t涨幅%\t成交量\t策略名称
                    line = f"{code}\t{stock_name}\t{date_str} {time_str}\t{price:.2f}\t{gain:.2f}%\t{volume}\t{STRATEGY_NAME}\n"
                    f.write(line)
            
            log(f">>> 已写入 {len(stocks)} 只新增股票到文件: {output_file}")
        except Exception as e:
            log(f">>> 写入文件失败 [{output_file}]: {e}", "ERROR")


def handlebar(ContextInfo):
    pass


def scan_stocks(ContextInfo):
    """
    优化后的扫描逻辑：
    1. 一次性获取全部股票行情（传入完整股票列表）
    2. 极速过滤：按淘汰率排序条件
    3. 只用内存中的历史数据 + 实时tick计算
    """
    global g_qualified_stocks, g_scan_count
    
    current_time = datetime.now()
    if CHECK_TRADING_TIME and not is_trading_time(current_time):
        log(f"当前非交易时间，跳过扫描", "WARN")
        return
    
    g_scan_count += 1
    log("")
    log("#" * 70)
    log(f"############## 第 {g_scan_count} 轮扫描开始（优化版） ##############")
    log("#" * 70)
    
    start_time = datetime.now()
    
    # ========== 优化1: 一次性获取全部股票行情 ==========
    log("")
    log(">>> 步骤1: 获取实时行情（一次性传入全部股票）")
    tick_start = datetime.now()
    
    try:
        # 一次性传入全部股票列表获取行情
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
    
    # ========== 优化3: 极速过滤 ==========
    log("")
    log(">>> 步骤2: 极速过滤（按淘汰率排序）")
    filter_start = datetime.now()
    
    qualified = []
    stats = {
        'total': len(g_stock_pool),
        'no_tick': 0,
        'no_prev': 0,
        'no_hist': 0,
        'fail_zf45': 0,  # 第1道过滤（最快）
        'fail_nib1': 0,  # 第2道过滤
        'fail_yinxian': 0,  # 第3道过滤
        'fail_ma5': 0,  # 第4道过滤（最慢）
        'pass': 0
    }
    
    # 遍历预过滤后的股票池（已排除8/4开头）
    for stock in g_stock_pool:
        result = check_stock_fast(stock, all_ticks, stats)
        if result:
            qualified.append(result)
            log(f"  ★ 命中: {stock} | 涨幅:{result['today_max_gain']}% | 6.5%次数:{result['gain_65_count']} | 阴线:{result['yinxian_count']}天", "SUCCESS")
    
    filter_elapsed = (datetime.now() - filter_start).total_seconds()
    total_elapsed = (datetime.now() - start_time).total_seconds()
    
    # 打印统计
    log("")
    log("=" * 60)
    log("扫描统计汇总（极速过滤版）")
    log("=" * 60)
    log(f"总股票数: {stats['total']} (已预排除8/4开头)")
    log(f"行情获取耗时: {tick_elapsed:.3f} 秒")
    log(f"过滤计算耗时: {filter_elapsed:.3f} 秒")
    log(f"总扫描耗时: {total_elapsed:.2f} 秒")
    log(f"扫描速度: {stats['total']/total_elapsed:.0f} 只/秒")
    log("-" * 40)
    log(f"无行情数据: {stats['no_tick']}")
    log(f"无昨收价: {stats['no_prev']}")
    log(f"无历史数据: {stats['no_hist']}")
    log(f"第1道淘汰(ZF45: 今日涨幅<4.5%): {stats['fail_zf45']}")
    log(f"第2道淘汰(NIB1: 6.5%次数<5): {stats['fail_nib1']}")
    log(f"第3道淘汰(阴线数量>3): {stats['fail_yinxian']}")
    log(f"第4道淘汰(阴线跌破MA5): {stats['fail_ma5']}")
    log(f"全部通过: {stats['pass']}")
    log("=" * 60)
    
    # 处理结果
    process_results(qualified, current_time)
    
    log("")
    log("#" * 70)
    log(f"############## 第 {g_scan_count} 轮扫描结束 ##############")
    log("#" * 70)
    log("")


def check_stock_fast(stock, all_ticks, stats):
    """
    优化3: 极速过滤 - 按淘汰率排序条件判断
    顺序：ZF45(最快) → NIB1 → 阴线数量 → MA5(最慢)
    """
    # 获取tick数据
    tick = all_ticks.get(stock)
    if not tick:
        stats['no_tick'] += 1
        return None
    
    # 获取昨收价
    prev_close = g_prev_close_cache.get(stock)
    if not prev_close or prev_close <= 0:
        stats['no_prev'] += 1
        return None
    
    today_high = tick.get('high', 0)
    if not today_high or today_high <= 0:
        stats['no_tick'] += 1
        return None
    
    # ========== 第1道过滤: ZF45（最快，只需tick数据） ==========
    # 今日最高 > 昨收*1.045 (涨幅4.5%)
    if today_high <= prev_close * 1.045:
        stats['fail_zf45'] += 1
        return None
    
    # 获取历史数据（从内存缓存）
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
    
    # ========== 第2道过滤: NIB1 ==========
    # COUNT(H > REF(C,1)*1.065, 10) >= 5
    count_65 = 0
    for i in range(len(closes) - 10, len(closes)):
        if i >= 1 and closes[i-1] > 0 and highs[i] > closes[i-1] * 1.065:
            count_65 += 1
    
    if count_65 < 5:
        stats['fail_nib1'] += 1
        return None
    
    # ========== 第3道过滤: 阴线数量 ==========
    # COUNT(C<O, 10) <= 3
    yinxian_count = 0
    for i in range(len(closes) - 10, len(closes)):
        if closes[i] < opens[i]:
            yinxian_count += 1
    
    if yinxian_count > 3:
        stats['fail_yinxian'] += 1
        return None
    
    # ========== 第4道过滤: MA5条件（最慢） ==========
    # COUNT(C<O AND L<=MA5, 10) = 0
    for i in range(len(closes) - 10, len(closes)):
        if i >= 4:
            ma5 = np.mean(closes[i-4:i+1])
            if closes[i] < opens[i] and lows[i] <= ma5:
                stats['fail_ma5'] += 1
                return None
    
    # ========== 通过所有条件 ==========
    stats['pass'] += 1
    
    current_price = tick.get('lastPrice', 0)
    if not current_price:
        current_price = tick.get('price', tick.get('last', 0))
    
    # 获取成交量
    volume = tick.get('volume', 0)
    if not volume:
        volume = tick.get('vol', 0)
    
    # 最高价涨幅（用于条件判断）
    max_gain = (today_high - prev_close) / prev_close * 100
    # 当前价格涨幅（用于显示和写入文件）
    current_gain = (current_price - prev_close) / prev_close * 100 if current_price > 0 else max_gain
    
    return {
        'stock': stock,
        'today_max_gain': round(max_gain, 2),
        'current_gain': round(current_gain, 2),
        'gain_65_count': count_65,
        'yinxian_count': yinxian_count,
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
    
    # 检查新增股票
    new_stocks = []
    for item in qualified:
        stock = item['stock']
        if stock not in g_all_found_stocks:
            g_all_found_stocks[stock] = current_time_str
            new_stocks.append(item)
    
    # 新增股票发送微信、邮件并写入文件
    if new_stocks:
        push_to_wechat(new_stocks)  # 先推送微信
        send_sms_tencent(new_stocks)  # 再发邮件
        write_stocks_to_file(new_stocks, date_str, time_str)  # 最后写文件
    
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
        sorted_stocks = sorted(g_all_found_stocks.items(), key=lambda x: x[1])
        for stock, first_time in sorted_stocks:
            code = stock.split('.')[0]
            board = "科创" if code.startswith('688') else "主板"
            log(f"  {stock} ({board}) - 首次发现: {first_time}")
    log("=" * 70)


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
    log("缓存已清理，扫描计数已重置，累计股票已清空，邮件记录已重置")


def manual_scan(ContextInfo):
    log(">>> 手动触发扫描")
    scan_stocks(ContextInfo)
