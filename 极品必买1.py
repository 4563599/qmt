# -*- coding: gbk -*-
"""
QMT鏋佸搧蹇呬拱绛栫暐 - 浼樺寲鐗?
浼樺寲鐐癸細
1. 鍏ㄩ噺琛屾儏鑾峰彇锛歴ubscribe_whole_quote + get_full_tick(鍏ㄩ儴鑲＄エ) 涓?娆℃?ц幏鍙?
2. 澧為噺鏇存柊锛氬巻鍙叉暟鎹彧鍔犺浇涓?娆★紝鎵弿鏃跺彧鐢ㄥ疄鏃舵暟鎹?
3. 鏋侀?熻繃婊わ細鎸夋窐姹扮巼鎺掑簭鏉′欢鍒ゆ柇锛?90%鑲＄エ鍦ㄥ墠涓ゆ琚窐姹?

鏉′欢锛?
1. NIB2: 鎺掗櫎8鍜?4寮?澶达紙鏈?蹇紝瀛楃涓插垽鏂紝鍒濆鍖栨椂杩囨护锛?
2. ZF45: 浠婃棩鏈?楂? > 鏄ㄦ敹*1.045锛堟瀬蹇紝鍙渶tick鏁版嵁锛?
3. NIB1: 10澶╁唴 鏈?楂?>鏄ㄦ敹*1.065 鐨勬鏁?>=5
4. 10澶╁唴闃寸嚎鏁伴噺<=3
5. 10澶╁唴涓嶅瓨鍦?"闃寸嚎涓旀渶浣庝环<=MA5"鐨勬儏鍐?
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

# 鍏ㄥ眬鍙橀噺
g_stock_pool = []  # 棰勮繃婊ゅ悗鐨勮偂绁ㄦ睜锛堝凡鎺掗櫎8/4寮?澶达級
g_qualified_stocks = {}
g_all_found_stocks = {}
g_scan_count = 0
g_initialized = False
g_context = None
g_lock = threading.Lock()

# ========== 浼樺寲2: 鍘嗗彶鏁版嵁缂撳瓨锛堝彧鍔犺浇涓?娆★級 ==========
g_history_data = {}  # {stock: {'closes': array, 'opens': array, 'highs': array, 'lows': array}}
g_prev_close_cache = {}  # {stock: prev_close}
g_stock_names = {}  # {stock: name} 鑲＄エ鍚嶇О缂撳瓨

# 閰嶇疆鍙傛暟
DEBUG_MODE = True
CHECK_TRADING_TIME = True  # 寮?鍚氦鏄撴椂闂存鏌ワ紝鏀剁洏鍚庡仠姝㈡墽琛?
PRINT_PROGRESS = True
LOG_ENABLED = False  # 鏃ュ織寮?鍏筹紝False=鍏抽棴鏃ュ織鎵撳嵃

# 杈撳嚭鏂囦欢璺緞
OUTPUT_FILE = r"C:\Users\lyy\Desktop\鑲＄エ\棰勮\5鎷夊崌闃村皯_qmt.txt"
OUTPUT_FILE_2 = r"C:\Users\lyy\Desktop\鑲＄エ\棰勮\5鎷夊崌闃村皯.txt"  # 绗簩涓緭鍑烘枃浠?
STRATEGY_NAME = "鏋佸搧蹇呬拱1"  # 绛栫暐鍚嶇О锛屽啓鍏ユ枃浠舵椂浣跨敤

# ========== QQ閭閰嶇疆 ==========
EMAIL_ENABLED = True  # 鏄惁鍚敤閭欢鍙戦?侊紝True=鍙戦?侊紝False=涓嶅彂閫?
EMAIL_RECEIVERS = ["4563599@qq.com"]  # 鎺ユ敹閭欢鐨勯偖绠卞垪琛紝鍙坊鍔犲涓?
EMAIL_SENDER = "4563599@qq.com"  # 鍙戦?侀偖浠剁殑QQ閭
EMAIL_AUTH_CODE = "iruvzesxztercaic"  # QQ閭鎺堟潈鐮?
EMAIL_SMTP_SERVER = "smtp.qq.com"
EMAIL_SMTP_PORT = 465

# ========== WxPusher寰俊鎺ㄩ?侀厤缃? ==========
WXPUSHER_ENABLED = True  # 鏄惁鍚敤寰俊鎺ㄩ??
WXP_TOKEN = "AT_mUKiCaJyUxhf0V1K9X3MuhqDaLmguFY4"
WXP_UID = "UID_d5iiHewR5VxqIWV3t2vkumOkykre"
WXP_SUMMARY = "銆愭瀬鍝佸繀涔?1锛岄槾绾块兘涓嶇銆?"

# 宸插彂閫侀偖浠剁殑鑲＄エ璁板綍锛堢▼搴忚繍琛屾湡闂达級
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
    log("========== 绛栫暐鍒濆鍖栧紑濮嬶紙浼樺寲鐗堬級 ==========")
    log("=" * 60)
    
    # ========== 浼樺寲1: 璁㈤槄鍏ㄥ競鍦鸿鎯? ==========
    log(">>> 璁㈤槄鍏ㄥ競鍦鸿鎯?...")
    try:
        ContextInfo.subscribe_whole_quote(["SH", "SZ"])
        log(">>> 鍏ㄥ競鍦鸿鎯呰闃呮垚鍔? (SH, SZ)")
    except Exception as e:
        log(f">>> 鍏ㄥ競鍦鸿鎯呰闃呭け璐?: {e}", "WARN")
    
    # 鑾峰彇鑲＄エ姹狅紙棰勮繃婊?8/4寮?澶达級
    g_stock_pool = get_stock_pool(ContextInfo)
    log(f"鑲＄エ姹犲姞杞藉畬鎴愶紝鍏? {len(g_stock_pool)} 鍙偂绁紙宸叉帓闄?8/4寮?澶达級")
    
    # 棰勫姞杞借偂绁ㄥ悕绉?
    log("")
    log(">>> 姝ｅ湪棰勫姞杞借偂绁ㄥ悕绉?...")
    preload_stock_names(ContextInfo)
    log(f">>> 鑲＄エ鍚嶇О鍔犺浇瀹屾垚: {len(g_stock_names)} 鍙?")
    
    # ========== 浼樺寲2: 鍙姞杞戒竴娆″巻鍙叉暟鎹? ==========
    log("")
    log(">>> 姝ｅ湪棰勫姞杞藉巻鍙叉暟鎹紙鍙姞杞戒竴娆★級...")
    preload_history_data(ContextInfo)
    log(f">>> 鍘嗗彶鏁版嵁棰勫姞杞藉畬鎴?")
    log(f">>> 缂撳瓨鑲＄エ鏁?: {len(g_history_data)}")
    log(f">>> 鏄ㄦ敹浠风紦瀛?: {len(g_prev_close_cache)}")
    
    ContextInfo.run_time("scan_stocks", "3nSecond", "2024-01-01 09:30:00", "SH")
    log("瀹氭椂鍣ㄨ缃畬鎴愶紝姣?3绉掓壂鎻忎竴娆?")
    
    g_initialized = True
    log("=" * 60)
    log("========== 绛栫暐鍒濆鍖栧畬鎴? ==========")
    log("=" * 60)
    log("")
    
    log(">>> 鎵ц棣栨鎵弿...")
    scan_stocks(ContextInfo)


def get_stock_pool(ContextInfo):
    """鑾峰彇鑲＄エ姹狅紝棰勮繃婊?8/4寮?澶达紙NIB2鏉′欢鍓嶇疆锛?"""
    stock_list = []
    log("姝ｅ湪鑾峰彇鑲＄エ鍒楄〃...")
    
    sectors = ["娌繁A鑲?", "绉戝垱鏉?"]
    all_stocks = []
    
    for sector in sectors:
        try:
            stocks = ContextInfo.get_stock_list_in_sector(sector)
            if stocks:
                log(f"  {sector}: 鑾峰彇鍒? {len(stocks)} 鍙?")
                all_stocks.extend(stocks)
        except Exception as e:
            log(f"  {sector}: 鑾峰彇澶辫触 - {str(e)}", "WARN")
    
    all_stocks = list(set(all_stocks))
    log(f"鍚堝苟鍘婚噸鍚?: {len(all_stocks)} 鍙?")
    
    excluded_st = 0
    excluded_bse = 0
    included_main = 0
    included_kcb = 0
    
    for stock in all_stocks:
        code = stock.split('.')[0]
        
        if 'ST' in stock.upper():
            excluded_st += 1
            continue
        # NIB2: 鎺掗櫎8鍜?4寮?澶达紙鍖椾氦鎵?锛?- 鍦ㄥ垵濮嬪寲鏃跺氨杩囨护鎺?
        if code.startswith('8') or code.startswith('4'):
            excluded_bse += 1
            continue
        
        stock_list.append(stock)
        
        if code.startswith('688'):
            included_kcb += 1
        else:
            included_main += 1
    
    log(f"绛涢?夌粨鏋?:")
    log(f"  - 涓绘澘/鍒涗笟鏉?: {included_main} 鍙?")
    log(f"  - 绉戝垱鏉?(688): {included_kcb} 鍙?")
    log(f"  - 鎺掗櫎ST: {excluded_st} 鍙?")
    log(f"  - 鎺掗櫎鍖椾氦鎵?(8/4寮?澶?): {excluded_bse} 鍙?")
    log(f"  - 鏈?缁堣偂绁ㄦ睜: {len(stock_list)} 鍙?")
    
    return stock_list


def preload_history_data(ContextInfo):
    """
    浼樺寲2: 鍙姞杞戒竴娆″巻鍙叉暟鎹埌鍐呭瓨
    鍘嗗彶鏁版嵁锛堝墠14澶╋級鍦ㄤ氦鏄撴棩鍐呬笉鍙橈紝鏃犻渶閲嶅鑾峰彇
    """
    global g_history_data, g_prev_close_cache
    
    batch_size = 300
    total = len(g_stock_pool)
    success_count = 0
    fail_count = 0
    
    log(f"寮?濮嬫壒閲忓姞杞藉巻鍙叉暟鎹紝鍏? {total} 鍙偂绁?")
    
    for i in range(0, total, batch_size):
        batch = g_stock_pool[i:i+batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size
        
        try:
            # 鑾峰彇15澶╂暟鎹紙鍚粖澶╋紝鐢ㄤ簬璁＄畻MA5绛夛級
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
                            # 瀛樺偍涓簄umpy鏁扮粍锛屽姞閫熷悗缁绠?
                            g_history_data[stock] = {
                                'closes': stock_data['close'].values.astype(float),
                                'opens': stock_data['open'].values.astype(float),
                                'highs': stock_data['high'].values.astype(float),
                                'lows': stock_data['low'].values.astype(float)
                            }
                            # 鏄ㄦ敹浠? = 鍊掓暟绗簩澶╃殑鏀剁洏浠?
                            g_prev_close_cache[stock] = float(stock_data['close'].iloc[-2])
                            success_count += 1
                        else:
                            fail_count += 1
                    except:
                        fail_count += 1
            else:
                fail_count += len(batch)
                        
        except Exception as e:
            log(f"  鎵规{batch_num}寮傚父: {str(e)}", "ERROR")
            fail_count += len(batch)
        
        if PRINT_PROGRESS:
            progress = min(i + batch_size, total)
            pct = progress * 100 // total
            log(f"  [棰勫姞杞絔 鎵规 {batch_num}/{total_batches} | 杩涘害: {progress}/{total} ({pct}%) | 鎴愬姛: {success_count}")
    
    log(f"棰勫姞杞藉畬鎴?: 鎴愬姛 {success_count} 鍙?, 澶辫触 {fail_count} 鍙?")


def extract_stock_data(data, stock):
    """浠庢壒閲忔暟鎹腑鎻愬彇鍗曞彧鑲＄エ鏁版嵁"""
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
    """棰勫姞杞借偂绁ㄥ悕绉?"""
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
    """鑾峰彇鏄熸湡锛屾牸寮忥細鏄熸湡鏃?"""
    weekdays = ['鏄熸湡涓?', '鏄熸湡浜?', '鏄熸湡涓?', '鏄熸湡鍥?', '鏄熸湡浜?', '鏄熸湡鍏?', '鏄熸湡鏃?']
    now = datetime.now()
    return weekdays[now.weekday()]


def push_to_wechat(stocks):
    """閫氳繃 WxPusher 鎺ㄩ?佸井淇℃秷鎭?"""
    log(f">>> 鍑嗗寰俊鎺ㄩ?侊紝鍏? {len(stocks)} 鍙偂绁?")
    log(f">>> WXPUSHER_ENABLED: {WXPUSHER_ENABLED}")
    log(f">>> requests妯″潡: {'宸插姞杞?' if requests else '鏈姞杞?'}")
    
    if not WXPUSHER_ENABLED:
        log(">>> 寰俊鎺ㄩ?佸凡鍏抽棴锛岃烦杩?")
        return
    
    if requests is None:
        log(">>> requests妯″潡鏈畨瑁咃紝鏃犳硶鎺ㄩ?佸井淇?", "ERROR")
        return
    
    date_weekday = get_date_with_weekday()
    
    for item in stocks:
        code = item['stock'].split('.')[0]
        name = g_stock_names.get(item['stock'], code)
        gain = f"{item['current_gain']:.2f}%"
        time_str = item['time']
        
        # 鏍煎紡锛氥?愭瀬鍝佸繀涔?1锛岄槾绾块兘涓嶇銆慭n鑲＄エ锛氫竾闆嗙鎶? (300552)\n娑ㄥ箙锛?2.38%\n鏃堕棿锛?10:30:00 鏄熸湡鏃? 12鏈?28鏃?
        msg_content = f"{WXP_SUMMARY}\n鑲＄エ锛歿name} ({code})\n娑ㄥ箙锛歿gain}\n鏃堕棿锛歿time_str} {date_weekday}"
        
        log(f">>> 鎺ㄩ?佸唴瀹?: {msg_content}")
        log(f">>> WXP_TOKEN: {WXP_TOKEN[:20]}...")
        log(f">>> WXP_UID: {WXP_UID}")
        
        url = "https://wxpusher.zjiecode.com/api/send/message"
        payload = {
            "appToken": WXP_TOKEN,
            "content": msg_content,
            "summary": f"闃寸嚎閮戒笉纰? {name} {time_str} {date_weekday}",
            "contentType": 1,
            "uids": [WXP_UID]
        }
        
        try:
            log(f">>> 姝ｅ湪鍙戦?佽姹傚埌: {url}")
            response = requests.post(url, json=payload, timeout=10)
            log(f">>> HTTP鐘舵?佺爜: {response.status_code}")
            log(f">>> 鍝嶅簲鍐呭: {response.text}")
            
            res = response.json()
            if res.get('code') == 1000:
                log(f">>> 寰俊鎺ㄩ?佹垚鍔?: {name} {code}")
            else:
                log(f">>> 寰俊鎺ㄩ?佸け璐?: code={res.get('code')}, msg={res.get('msg')}", "WARN")
        except Exception as e:
            log(f">>> 寰俊鎺ㄩ?佸紓甯?: {type(e).__name__}: {e}", "ERROR")


def send_sms_tencent(stocks):
    """
    閫氳繃QQ閭鍙戦?侀偖浠堕?氱煡
    stocks: 鏂板鑲＄エ鍒楄〃
    姣忓彧鑲＄エ鍗曠嫭鍙戦?佷竴灏侀偖浠?
    """
    global g_email_sent_stocks
    
    if not EMAIL_ENABLED:
        return
    
    # 杩囨护鍑烘湭鍙戦?佽繃閭欢鐨勮偂绁?
    new_stocks_to_send = []
    for item in stocks:
        stock = item['stock']
        if stock not in g_email_sent_stocks:
            new_stocks_to_send.append(item)
            g_email_sent_stocks.add(stock)
    
    if not new_stocks_to_send:
        log(">>> 鎵?鏈夎偂绁ㄥ凡鍙戦?佽繃閭欢锛岃烦杩?")
        return
    
    date_weekday = get_date_with_weekday()
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 姣忓彧鑲＄エ鍗曠嫭鍙戦?佷竴灏侀偖浠?
    for item in new_stocks_to_send:
        code = item['stock'].split('.')[0]
        name = g_stock_names.get(item['stock'], code)
        gain = f"{item['current_gain']:.2f}%"
        time_str = item['time']
        
        # 閭欢鏍囬锛氶槾绾块兘涓嶇 涓囬泦绉戞妧 10:30:00 鏄熸湡鏃?
        subject = f"闃寸嚎閮戒笉纰? {name} {time_str} {date_weekday}"
        # 閭欢鍐呭
        body = f"鍙戠幇鏃堕棿锛歿current_time}\n\n銆恵STRATEGY_NAME}锛岄槾绾块兘涓嶇銆慭n鑲＄エ锛歿name} ({code})\n娑ㄥ箙锛歿gain}\n鏃堕棿锛歿time_str} {date_weekday}"
        
        try:
            msg = MIMEText(body, 'plain', 'utf-8')
            msg['From'] = Header(EMAIL_SENDER)
            msg['To'] = Header(','.join(EMAIL_RECEIVERS))
            msg['Subject'] = Header(subject, 'utf-8')
            
            server = smtplib.SMTP_SSL(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT)
            server.login(EMAIL_SENDER, EMAIL_AUTH_CODE)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVERS, msg.as_string())
            server.quit()
            
            log(f">>> 閭欢鍙戦?佹垚鍔?: {name} {code}")
        except Exception as e:
            log(f">>> 閭欢鍙戦?佸け璐? [{name} {code}]: {e}", "ERROR")


def write_stocks_to_file(stocks, date_str, time_str):
    """
    灏嗘柊澧炶偂绁ㄥ啓鍏ユ枃浠?
    鏍煎紡锛氫唬鐮乗t鍚嶇О\t鏃ユ湡 鏃堕棿\t浠锋牸\t娑ㄥ箙%\t鎴愪氦閲廫t绛栫暐鍚嶇О
    绀轰緥锛?001331	鑳滈?氳兘婧?	2025-12-26 09:29	42.10	10.01%	77994	鏋佸搧蹇呬拱1
    """
    # 鍐欏叆涓や釜鏂囦欢
    for output_file in [OUTPUT_FILE, OUTPUT_FILE_2]:
        try:
            with open(output_file, 'a', encoding='gbk') as f:
                for item in stocks:
                    code = item['stock'].split('.')[0]
                    # 浠庣紦瀛樿幏鍙栬偂绁ㄥ悕绉?
                    stock_name = g_stock_names.get(item['stock'], code)
                    price = item['current_price']
                    gain = item['current_gain']  # 浣跨敤褰撳墠浠锋牸娑ㄥ箙
                    volume = item.get('volume', 0)
                    
                    # 鍗曡鏍煎紡锛氫唬鐮乗t鍚嶇О\t鏃ユ湡 鏃堕棿\t浠锋牸\t娑ㄥ箙%\t鎴愪氦閲廫t绛栫暐鍚嶇О
                    line = f"{code}\t{stock_name}\t{date_str} {time_str}\t{price:.2f}\t{gain:.2f}%\t{volume}\t{STRATEGY_NAME}\n"
                    f.write(line)
            
            log(f">>> 宸插啓鍏? {len(stocks)} 鍙柊澧炶偂绁ㄥ埌鏂囦欢: {output_file}")
        except Exception as e:
            log(f">>> 鍐欏叆鏂囦欢澶辫触 [{output_file}]: {e}", "ERROR")


def handlebar(ContextInfo):
    pass


def scan_stocks(ContextInfo):
    """
    浼樺寲鍚庣殑鎵弿閫昏緫锛?
    1. 涓?娆℃?ц幏鍙栧叏閮ㄨ偂绁ㄨ鎯咃紙浼犲叆瀹屾暣鑲＄エ鍒楄〃锛?
    2. 鏋侀?熻繃婊わ細鎸夋窐姹扮巼鎺掑簭鏉′欢
    3. 鍙敤鍐呭瓨涓殑鍘嗗彶鏁版嵁 + 瀹炴椂tick璁＄畻
    """
    global g_qualified_stocks, g_scan_count
    
    current_time = datetime.now()
    if CHECK_TRADING_TIME and not is_trading_time(current_time):
        log(f"褰撳墠闈炰氦鏄撴椂闂达紝璺宠繃鎵弿", "WARN")
        return
    
    g_scan_count += 1
    log("")
    log("#" * 70)
    log(f"############## 绗? {g_scan_count} 杞壂鎻忓紑濮嬶紙浼樺寲鐗堬級 ##############")
    log("#" * 70)
    
    start_time = datetime.now()
    
    # ========== 浼樺寲1: 涓?娆℃?ц幏鍙栧叏閮ㄨ偂绁ㄨ鎯? ==========
    log("")
    log(">>> 姝ラ1: 鑾峰彇瀹炴椂琛屾儏锛堜竴娆℃?т紶鍏ュ叏閮ㄨ偂绁級")
    tick_start = datetime.now()
    
    try:
        # 涓?娆℃?т紶鍏ュ叏閮ㄨ偂绁ㄥ垪琛ㄨ幏鍙栬鎯?
        all_ticks = ContextInfo.get_full_tick(g_stock_pool)
        tick_count = len(all_ticks) if all_ticks else 0
        tick_elapsed = (datetime.now() - tick_start).total_seconds()
        log(f">>> 琛屾儏鑾峰彇瀹屾垚: {tick_count} 鍙?, 鑰楁椂 {tick_elapsed:.3f} 绉?")
    except Exception as e:
        log(f">>> 琛屾儏鑾峰彇澶辫触: {e}", "ERROR")
        all_ticks = {}
    
    if not all_ticks:
        log(">>> 鏃犺鎯呮暟鎹紝璺宠繃鏈疆鎵弿", "WARN")
        return
    
    # ========== 浼樺寲3: 鏋侀?熻繃婊? ==========
    log("")
    log(">>> 姝ラ2: 鏋侀?熻繃婊わ紙鎸夋窐姹扮巼鎺掑簭锛?")
    filter_start = datetime.now()
    
    qualified = []
    stats = {
        'total': len(g_stock_pool),
        'no_tick': 0,
        'no_prev': 0,
        'no_hist': 0,
        'fail_zf45': 0,  # 绗?1閬撹繃婊わ紙鏈?蹇級
        'fail_nib1': 0,  # 绗?2閬撹繃婊?
        'fail_yinxian': 0,  # 绗?3閬撹繃婊?
        'fail_ma5': 0,  # 绗?4閬撹繃婊わ紙鏈?鎱級
        'pass': 0
    }
    
    # 閬嶅巻棰勮繃婊ゅ悗鐨勮偂绁ㄦ睜锛堝凡鎺掗櫎8/4寮?澶达級
    for stock in g_stock_pool:
        result = check_stock_fast(stock, all_ticks, stats)
        if result:
            qualified.append(result)
            log(f"  鈽? 鍛戒腑: {stock} | 娑ㄥ箙:{result['today_max_gain']}% | 6.5%娆℃暟:{result['gain_65_count']} | 闃寸嚎:{result['yinxian_count']}澶?", "SUCCESS")
    
    filter_elapsed = (datetime.now() - filter_start).total_seconds()
    total_elapsed = (datetime.now() - start_time).total_seconds()
    
    # 鎵撳嵃缁熻
    log("")
    log("=" * 60)
    log("鎵弿缁熻姹囨?伙紙鏋侀?熻繃婊ょ増锛?")
    log("=" * 60)
    log(f"鎬昏偂绁ㄦ暟: {stats['total']} (宸查鎺掗櫎8/4寮?澶?)")
    log(f"琛屾儏鑾峰彇鑰楁椂: {tick_elapsed:.3f} 绉?")
    log(f"杩囨护璁＄畻鑰楁椂: {filter_elapsed:.3f} 绉?")
    log(f"鎬绘壂鎻忚?楁椂: {total_elapsed:.2f} 绉?")
    log(f"鎵弿閫熷害: {stats['total']/total_elapsed:.0f} 鍙?/绉?")
    log("-" * 40)
    log(f"鏃犺鎯呮暟鎹?: {stats['no_tick']}")
    log(f"鏃犳槰鏀朵环: {stats['no_prev']}")
    log(f"鏃犲巻鍙叉暟鎹?: {stats['no_hist']}")
    log(f"绗?1閬撴窐姹?(ZF45: 浠婃棩娑ㄥ箙<4.5%): {stats['fail_zf45']}")
    log(f"绗?2閬撴窐姹?(NIB1: 6.5%娆℃暟<5): {stats['fail_nib1']}")
    log(f"绗?3閬撴窐姹?(闃寸嚎鏁伴噺>3): {stats['fail_yinxian']}")
    log(f"绗?4閬撴窐姹?(闃寸嚎璺岀牬MA5): {stats['fail_ma5']}")
    log(f"鍏ㄩ儴閫氳繃: {stats['pass']}")
    log("=" * 60)
    
    # 澶勭悊缁撴灉
    process_results(qualified, current_time)
    
    log("")
    log("#" * 70)
    log(f"############## 绗? {g_scan_count} 杞壂鎻忕粨鏉? ##############")
    log("#" * 70)
    log("")


def check_stock_fast(stock, all_ticks, stats):
    """
    浼樺寲3: 鏋侀?熻繃婊? - 鎸夋窐姹扮巼鎺掑簭鏉′欢鍒ゆ柇
    椤哄簭锛歓F45(鏈?蹇?) 鈫? NIB1 鈫? 闃寸嚎鏁伴噺 鈫? MA5(鏈?鎱?)
    """
    # 鑾峰彇tick鏁版嵁
    tick = all_ticks.get(stock)
    if not tick:
        stats['no_tick'] += 1
        return None
    
    # 鑾峰彇鏄ㄦ敹浠?
    prev_close = g_prev_close_cache.get(stock)
    if not prev_close or prev_close <= 0:
        stats['no_prev'] += 1
        return None
    
    today_high = tick.get('high', 0)
    if not today_high or today_high <= 0:
        stats['no_tick'] += 1
        return None
    
    # ========== 绗?1閬撹繃婊?: ZF45锛堟渶蹇紝鍙渶tick鏁版嵁锛? ==========
    # 浠婃棩鏈?楂? > 鏄ㄦ敹*1.045 (娑ㄥ箙4.5%)
    if today_high <= prev_close * 1.045:
        stats['fail_zf45'] += 1
        return None
    
    # 鑾峰彇鍘嗗彶鏁版嵁锛堜粠鍐呭瓨缂撳瓨锛?
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
    
    # ========== 绗?2閬撹繃婊?: NIB1 ==========
    # COUNT(H > REF(C,1)*1.065, 10) >= 5
    count_65 = 0
    for i in range(len(closes) - 10, len(closes)):
        if i >= 1 and closes[i-1] > 0 and highs[i] > closes[i-1] * 1.065:
            count_65 += 1
    
    if count_65 < 5:
        stats['fail_nib1'] += 1
        return None
    
    # ========== 绗?3閬撹繃婊?: 闃寸嚎鏁伴噺 ==========
    # COUNT(C<O, 10) <= 3
    yinxian_count = 0
    for i in range(len(closes) - 10, len(closes)):
        if closes[i] < opens[i]:
            yinxian_count += 1
    
    if yinxian_count > 3:
        stats['fail_yinxian'] += 1
        return None
    
    # ========== 绗?4閬撹繃婊?: MA5鏉′欢锛堟渶鎱級 ==========
    # COUNT(C<O AND L<=MA5, 10) = 0
    for i in range(len(closes) - 10, len(closes)):
        if i >= 4:
            ma5 = np.mean(closes[i-4:i+1])
            if closes[i] < opens[i] and lows[i] <= ma5:
                stats['fail_ma5'] += 1
                return None
    
    # ========== 閫氳繃鎵?鏈夋潯浠? ==========
    stats['pass'] += 1
    
    current_price = tick.get('lastPrice', 0)
    if not current_price:
        current_price = tick.get('price', tick.get('last', 0))
    
    # 鑾峰彇鎴愪氦閲?
    volume = tick.get('volume', 0)
    if not volume:
        volume = tick.get('vol', 0)
    
    # 鏈?楂樹环娑ㄥ箙锛堢敤浜庢潯浠跺垽鏂級
    max_gain = (today_high - prev_close) / prev_close * 100
    # 褰撳墠浠锋牸娑ㄥ箙锛堢敤浜庢樉绀哄拰鍐欏叆鏂囦欢锛?
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
    """澶勭悊鎵弿缁撴灉"""
    global g_qualified_stocks, g_all_found_stocks
    
    current_time_str = current_time.strftime('%H:%M:%S')
    date_str = current_time.strftime('%Y-%m-%d')
    time_str = current_time.strftime('%H:%M')
    
    # 妫?鏌ユ柊澧炶偂绁?
    new_stocks = []
    for item in qualified:
        stock = item['stock']
        if stock not in g_all_found_stocks:
            g_all_found_stocks[stock] = current_time_str
            new_stocks.append(item)
    
    # 鏂板鑲＄エ鍙戦?佸井淇°?侀偖浠跺苟鍐欏叆鏂囦欢
    if new_stocks:
        push_to_wechat(new_stocks)  # 鍏堟帹閫佸井淇?
        send_sms_tencent(new_stocks)  # 鍐嶅彂閭欢
        write_stocks_to_file(new_stocks, date_str, time_str)  # 鏈?鍚庡啓鏂囦欢
    
    # 鎵撳嵃鏈疆缁撴灉
    if qualified:
        log("")
        log("*" * 70)
        log(f"鈽呪槄鈽? 鏈疆鍙戠幇 {len(qualified)} 鍙鍚堟潯浠剁殑鑲＄エ 鈽呪槄鈽?")
        log("*" * 70)
        
        qualified.sort(key=lambda x: x['today_max_gain'], reverse=True)
        
        for idx, item in enumerate(qualified, 1):
            code = item['stock'].split('.')[0]
            board = "绉戝垱" if code.startswith('688') else "涓绘澘"
            log("")
            log(f"銆恵idx}銆憑item['stock']} ({board})")
            log(f"    鏄ㄦ敹浠?: {item['prev_close']} | 浠婃棩鏈?楂?: {item['today_high']}")
            log(f"    浠婃棩鏈?楂樻定骞?: {item['today_max_gain']}%")
            log(f"    10澶╁唴6.5%杈炬爣娆℃暟: {item['gain_65_count']} 娆?")
            log(f"    10澶╁唴闃寸嚎鏁伴噺: {item['yinxian_count']} 澶?")
            log(f"    褰撳墠浠锋牸: {item['current_price']}")
            log(f"    鍙戠幇鏃堕棿: {item['time']}")
        
        log("")
        log("*" * 70)
        g_qualified_stocks = {item['stock']: item for item in qualified}
    else:
        log("")
        log("鏈疆鎵弿鏈彂鐜扮鍚堟潯浠剁殑鑲＄エ")
    
    # 鎵撳嵃鏂板鑲＄エ
    if new_stocks:
        log("")
        log("+" * 70)
        log(f"銆愭柊澧炪?戞湰杞柊鍙戠幇 {len(new_stocks)} 鍙偂绁?:")
        log("+" * 70)
        for item in new_stocks:
            log(f"  鈽? 鏂板: {item['stock']} | 棣栨鍙戠幇鏃堕棿: {current_time_str} | 娑ㄥ箙: {item['today_max_gain']}%", "NEW")
        log("+" * 70)
    
    # 鎵撳嵃绱鑲＄エ闆嗗悎
    log("")
    log("=" * 70)
    log(f"銆愮疮璁°?戜粖鏃ュ叡鍙戠幇 {len(g_all_found_stocks)} 鍙鍚堟潯浠剁殑鑲＄エ:")
    log("=" * 70)
    if g_all_found_stocks:
        sorted_stocks = sorted(g_all_found_stocks.items(), key=lambda x: x[1])
        for stock, first_time in sorted_stocks:
            code = stock.split('.')[0]
            board = "绉戝垱" if code.startswith('688') else "涓绘澘"
            log(f"  {stock} ({board}) - 棣栨鍙戠幇: {first_time}")
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
    log("缂撳瓨宸叉竻鐞嗭紝鎵弿璁℃暟宸查噸缃紝绱鑲＄エ宸叉竻绌猴紝閭欢璁板綍宸查噸缃?")


def manual_scan(ContextInfo):
    log(">>> 鎵嬪姩瑙﹀彂鎵弿")
    scan_stocks(ContextInfo)
