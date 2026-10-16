"""
股票预警展示网站 - Flask应用
"""
import os
import re
import io
import json
import base64
import time
import uuid
import unicodedata
import tempfile
import threading
import traceback
import importlib.util
from types import SimpleNamespace
import redis
import requests
import pandas as pd
from flask import Flask, render_template, jsonify, request, make_response, send_file, redirect, url_for
from flask_cors import CORS
from datetime import datetime, timedelta
import tushare as ts
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 加载环境变量配置文件
from pathlib import Path
env_file = Path(__file__).parent / '.env.production'
if env_file.exists():
    with open(env_file, encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())

app = Flask(__name__)
CORS(app)

MOBILE_SINGLE_LIST_ASSET_VERSION = '20260614_4'
MOBILE_SINGLE_LIST_CSS_TAG = (
    f'<link rel="stylesheet" href="/static/mobile-single-list.css?v={MOBILE_SINGLE_LIST_ASSET_VERSION}">'
)
MOBILE_SINGLE_LIST_JS_TAG = (
    f'<script src="/static/mobile-single-list.js?v={MOBILE_SINGLE_LIST_ASSET_VERSION}"></script>'
)
MONOCHROME_THEME_ASSET_VERSION = '20260621_1'
MONOCHROME_THEME_CSS_TAG = (
    f'<link rel="stylesheet" href="/static/monochrome-theme.css?v={MONOCHROME_THEME_ASSET_VERSION}">'
)
MONOCHROME_THEME_JS_TAG = (
    f'<script src="/static/monochrome-theme.js?v={MONOCHROME_THEME_ASSET_VERSION}"></script>'
)
MONOCHROME_THEME_BOOTSTRAP_TAG = (
    '<script id="monochrome-theme-bootstrap">'
    '(function(){try{'
    'var s=null,k="stock-web-simple-theme",c="stock_web_simple_theme";'
    'try{s=localStorage.getItem(k)}catch(e){}'
    'if(s!=="1"&&s!=="0"){var p=String(document.cookie||"").split(";");'
    'for(var i=0;i<p.length;i++){var v=p[i].trim();'
    'if(v.indexOf(c+"=")===0){s=decodeURIComponent(v.slice(c.length+1));break}}}'
    'var on=s!=="0";'
    'document.documentElement.classList.toggle("monochrome-theme",on);'
    'document.documentElement.setAttribute("data-monochrome-theme",on?"on":"off");'
    '}catch(e){}})();'
    '</script>'
)


@app.after_request
def inject_mobile_single_list_assets(response):
    if response.direct_passthrough or response.mimetype != 'text/html':
        return response

    try:
        html = response.get_data(as_text=True)
    except Exception:
        return response

    if not html:
        return response

    if 'data-skip-global-assets="1"' in html:
        return response

    changed = False
    if 'monochrome-theme-bootstrap' not in html:
        head_start = html.find('<head')
        if head_start != -1:
            head_open_end = html.find('>', head_start)
            if head_open_end != -1:
                html = (
                    html[:head_open_end + 1]
                    + '\n    '
                    + MONOCHROME_THEME_BOOTSTRAP_TAG
                    + '\n'
                    + html[head_open_end + 1:]
                )
                changed = True

    head_tags = []
    if 'mobile-single-list.css' not in html:
        head_tags.append(MOBILE_SINGLE_LIST_CSS_TAG)
    if 'monochrome-theme.css' not in html:
        head_tags.append(MONOCHROME_THEME_CSS_TAG)
    if head_tags and '</head>' in html:
        html = html.replace('</head>', '    ' + '\n    '.join(head_tags) + '\n</head>', 1)
        changed = True

    body_tags = []
    if 'mobile-single-list.js' not in html:
        body_tags.append(MOBILE_SINGLE_LIST_JS_TAG)
    if 'monochrome-theme.js' not in html:
        body_tags.append(MONOCHROME_THEME_JS_TAG)
    if body_tags and '</body>' in html:
        body_close_idx = html.rfind('</body>')
        html = (
            html[:body_close_idx]
            + '\n'.join(body_tags)
            + '\n'
            + html[body_close_idx:]
        )
        changed = True

    if changed:
        response.set_data(html)

    return response

# Redis配置
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)
REDIS_DB = int(os.environ.get('REDIS_DB', 0))

# Tushare配置
TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')
if TUSHARE_TOKEN:
    ts.set_token(TUSHARE_TOKEN)

# 腾讯财经批量行情缓存
_tencent_cache = {
    'data': {},
    'update_time': None
}

# 今日K线特征缓存（30秒更新一次）
_k_features_cache = {
    'data': {},
    'update_time': None
}

_ztorgj_weak20_excel_jobs = {}
_ztorgj_weak20_excel_jobs_lock = threading.Lock()
_ZTORGJ_WEAK20_EXCEL_JOB_TTL = 1800
_ZTORGJ_WEAK20_EXCEL_EXPORT_DIR = os.environ.get(
    'STOCK_WEB_EXPORT_DIR',
    os.path.join(tempfile.gettempdir(), 'stock_web_exports')
)


def _ztorgj_excel_diag(stage, **kwargs):
    try:
        payload = ' '.join([f'{key}={value}' for key, value in kwargs.items()])
        print(f'[ztorgj][weak20-kline-excel][{stage}] {payload}', flush=True)
    except Exception:
        pass

def get_stock_board_type(code):
    """判断股票所属板块类型"""
    if code.startswith('300'):
        return '创业板'
    elif code.startswith('688'):
        return '科创板'
    elif code.startswith('8') and not code.startswith('688'):
        return '北交所'
    else:
        return '主板'

def get_limit_up_threshold(code):
    """根据股票代码获取涨停阈值"""
    board_type = get_stock_board_type(code)
    if board_type in ['创业板', '科创板']:
        return 19.9
    elif board_type == '北交所':
        return 29.9
    else:
        return 9.9

TENCENT_QUOTE_CHUNK_SIZE = 200

def _normalize_stock_code_text(code):
    if isinstance(code, bytes):
        code = code.decode('utf-8', errors='ignore')
    code_str = str(code or '').strip()
    if '.' in code_str:
        code_str = code_str.split('.')[0]
    return code_str

def _to_tencent_quote_code(code):
    if isinstance(code, bytes):
        code = code.decode('utf-8', errors='ignore')
    code_str = str(code or '').strip()
    if not code_str:
        return ''
    upper_code = code_str.upper()
    if upper_code.endswith('.SH'):
        return f"sh{upper_code.replace('.SH', '')}"
    if upper_code.endswith('.SZ'):
        return f"sz{upper_code.replace('.SZ', '')}"
    if upper_code.endswith('.BJ'):
        return f"bj{upper_code.replace('.BJ', '')}"
    plain_code = _normalize_stock_code_text(code_str)
    if plain_code.startswith('6'):
        return f'sh{plain_code}'
    if plain_code.startswith('8') or plain_code.startswith('4'):
        return f'bj{plain_code}'
    return f'sz{plain_code}'

def _chunked(items, chunk_size=TENCENT_QUOTE_CHUNK_SIZE):
    for index in range(0, len(items), chunk_size):
        yield items[index:index + chunk_size]

def get_tencent_full_quotes(codes):
    """
    腾讯财经批量行情接口 - 获取完整K线数据（开高低收）
    返回: {code: {price, pre_close, change_pct, open, high, low, name}}
    """
    if not codes:
        return {}
    
    tencent_codes = [tc for tc in (_to_tencent_quote_code(code) for code in codes) if tc]
    
    result = {}
    try:
        for chunk in _chunked(tencent_codes):
            url = f"http://qt.gtimg.cn/q={','.join(chunk)}"
            response = requests.get(url, timeout=3)
            response.encoding = 'gbk'
            
            lines = response.text.strip().split(';')
            for line in lines:
                if not line.strip() or '=' not in line:
                    continue
                
                try:
                    parts = line.split('=')
                    if len(parts) < 2:
                        continue
                    
                    data_str = parts[1].strip().strip('"')
                    if not data_str:
                        continue
                    
                    fields = data_str.split('~')
                    if len(fields) < 35:
                        continue
                    
                    # 腾讯接口字段说明:
                    # fields[1]: 股票名称
                    # fields[2]: 股票代码
                    # fields[3]: 当前价格
                    # fields[4]: 昨收价
                    # fields[5]: 今开
                    # fields[33]: 最高价
                    # fields[34]: 最低价
                    # fields[32]: 涨跌幅(%)
                    code = fields[2]
                    current_price = float(fields[3]) if fields[3] else 0
                    pre_close = float(fields[4]) if fields[4] else 0
                    today_open = float(fields[5]) if fields[5] else 0
                    today_high = float(fields[33]) if fields[33] else 0
                    today_low = float(fields[34]) if fields[34] else 0
                    change_pct = float(fields[32]) if fields[32] else 0
                    
                    result[code] = {
                        'price': current_price,
                        'pre_close': pre_close,
                        'change_pct': change_pct,
                        'open': today_open,
                        'high': today_high,
                        'low': today_low,
                        'name': fields[1]
                    }
                except (ValueError, IndexError):
                    continue
                
    except Exception as e:
        print(f"腾讯完整行情获取失败: {e}")
    
    return result

def is_k_features_update_time():
    """判断是否为K线特征更新时间（9点到15点）"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周末不更新
        return False
    from datetime import time
    current_time = now.time()
    start_time = time(9, 0)
    end_time = time(15, 0)  # 收盘后不再实时计算，使用Redis中的收盘数据
    return start_time <= current_time <= end_time

def is_after_market_close():
    """判断是否为收盘后时间（15:00之后）"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周末
        return True
    from datetime import time
    current_time = now.time()
    market_close = time(15, 0)
    return current_time > market_close

def is_market_trading_time():
    """判断是否为交易时间（9:30-15:00）"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周末不交易
        return False
    from datetime import time
    current_time = now.time()
    
    # 早盘：9:30-11:30
    morning_start = time(9, 30)
    morning_end = time(11, 30)
    
    # 午盘：13:00-15:00
    afternoon_start = time(13, 0)
    afternoon_end = time(15, 0)
    
    return (morning_start <= current_time <= morning_end) or \
           (afternoon_start <= current_time <= afternoon_end)

def get_redis_refresh_interval():
    """根据时间返回Redis刷新间隔（秒）"""
    if is_market_trading_time():
        return 1  # 开盘时间每1秒
    else:
        return 1  # 非开盘时间每1秒（测试用）

def calculate_k_features(codes):
    """
    使用tushare计算今日K线特征（9-15点每30秒更新，收盘后使用Redis中的数据）
    返回: {code: {is_limit_up, is_high_gain, is_barefoot_positive, today_highest_gain}}
    """
    global _k_features_cache
    
    now = datetime.now()
    
    # 收盘后不再实时计算，返回空结果让前端使用Redis中的收盘数据
    if is_after_market_close():
        return {}
    
    # 检查缓存是否有效
    if _k_features_cache['update_time'] and _k_features_cache['data']:
        elapsed = (now - _k_features_cache['update_time']).total_seconds()
        # 9-15点之间：30秒更新一次
        # 其他时间：使用缓存不更新
        if not is_k_features_update_time():
            return _k_features_cache['data']
        if elapsed < 30:
            return _k_features_cache['data']
    
    # 非更新时间且无缓存，返回空结果
    if not is_k_features_update_time() and not _k_features_cache['data']:
        return {}
    
    result = {}
    
    try:
        # 使用tushare获取实时行情
        # 构建tushare格式的股票代码列表
        ts_codes = []
        code_map = {}  # ts_code -> code 映射
        for code in codes:
            if code.startswith('6'):
                ts_code = f"{code}.SH"
            elif code.startswith('0') or code.startswith('3'):
                ts_code = f"{code}.SZ"
            elif code.startswith('8') or code.startswith('4'):
                ts_code = f"{code}.BJ"
            else:
                ts_code = f"{code}.SZ"
            ts_codes.append(ts_code)
            code_map[ts_code] = code
        
        # 使用tushare基础API获取实时行情
        for code in codes:
            try:
                # 添加市场前缀
                if code.startswith('6'):
                    full_code = f'sh{code}'
                elif code.startswith('0') or code.startswith('3'):
                    full_code = f'sz{code}'
                elif code.startswith('8') or code.startswith('4'):
                    full_code = f'bj{code}'
                else:
                    full_code = f'sz{code}'
                
                # 获取实时行情
                df = ts.get_realtime_quotes(full_code)
                
                if df is not None and not df.empty:
                    row = df.iloc[0]
                    
                    pre_close = float(row['pre_close']) if row['pre_close'] else 0
                    today_open = float(row['open']) if row['open'] else 0
                    today_high = float(row['high']) if row['high'] else 0
                    today_low = float(row['low']) if row['low'] else 0
                    today_close = float(row['price']) if row['price'] else 0
                    
                    if pre_close <= 0 or today_open <= 0:
                        result[code] = {
                            'is_limit_up': False,
                            'is_high_gain': False,
                            'is_barefoot_positive': False,
                            'today_highest_gain': None
                        }
                        continue
                    
                    # 计算今日涨幅
                    change_pct = (today_close - pre_close) / pre_close * 100
                    
                    # 计算今日最高涨幅
                    today_highest_gain = (today_high - pre_close) / pre_close * 100
                    
                    # 判断涨停
                    threshold = get_limit_up_threshold(code)
                    is_limit_up = change_pct >= threshold
                    
                    # 最高涨幅是否>=6.85%
                    is_high_gain = today_highest_gain >= 6.85
                    
                    # 是否光脚阳线：阳线(收盘>开盘) 且 下引线极小
                    is_positive = today_close > today_open
                    if is_positive:
                        lower_shadow = today_open - today_low
                        body = today_close - today_open
                        # 光脚阳线：下引线小于实体的5%或下引线小于0.1%的价格
                        is_barefoot_positive = body > 0 and (lower_shadow < body * 0.05 or lower_shadow < today_open * 0.001)
                    else:
                        is_barefoot_positive = False
                    
                    result[code] = {
                        'is_limit_up': is_limit_up,
                        'is_high_gain': is_high_gain,
                        'is_barefoot_positive': is_barefoot_positive,
                        'today_highest_gain': round(today_highest_gain, 2)
                    }
                else:
                    result[code] = {
                        'is_limit_up': False,
                        'is_high_gain': False,
                        'is_barefoot_positive': False,
                        'today_highest_gain': None
                    }
            except Exception as e:
                print(f"tushare获取{code}行情失败: {e}")
                result[code] = {
                    'is_limit_up': False,
                    'is_high_gain': False,
                    'is_barefoot_positive': False,
                    'today_highest_gain': None
                }
    except Exception as e:
        print(f"calculate_k_features失败: {e}")
    
    # 更新缓存
    _k_features_cache['data'] = result
    _k_features_cache['update_time'] = now
    
    return result

# Redis连接池（复用连接，避免每次请求都创建新连接）
_redis_pool = redis.ConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    db=REDIS_DB,
    decode_responses=True,
    max_connections=50,  # 最大连接数
    socket_connect_timeout=2,  # 连接超时2秒
    socket_timeout=2  # 读写超时2秒
)

def get_redis_client():
    """获取Redis连接（使用连接池）"""
    return redis.Redis(connection_pool=_redis_pool)

def no_cache_response(data):
    """创建禁用缓存的JSON响应"""
    response = jsonify(data)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

def is_trading_time():
    """判断是否为交易时间"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    current_time = now.time()
    from datetime import time
    morning_start = time(9, 0)
    morning_end = time(11, 30)
    afternoon_start = time(13, 0)
    afternoon_end = time(15, 0)
    return (morning_start <= current_time <= morning_end) or \
           (afternoon_start <= current_time <= afternoon_end)

def should_fetch_quotes():
    """判断是否应该获取实时行情（包括中午休市时间）"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    current_time = now.time()
    from datetime import time
    # 9:00-15:00 都应该获取行情（包括中午休市）
    market_start = time(9, 0)
    market_end = time(15, 0)
    return market_start <= current_time <= market_end

def get_tencent_batch_quotes(codes):
    """
    腾讯财经批量行情接口 - 极速获取多只股票涨跌幅
    一次HTTP请求获取所有股票数据，大幅降低延迟
    """
    if not codes:
        return {}
    
    tencent_codes = [tc for tc in (_to_tencent_quote_code(code) for code in codes) if tc]
    
    result = {}
    try:
        for chunk in _chunked(tencent_codes):
            url = f"http://qt.gtimg.cn/q={','.join(chunk)}"
            response = requests.get(url, timeout=3)
            response.encoding = 'gbk'
            
            # 解析返回数据
            # 格式: v_sh600519="1~贵州茅台~600519~1850.00~1830.00~1835.00~12345~...~1.09~..."
            lines = response.text.strip().split(';')
            for line in lines:
                if not line.strip() or '=' not in line:
                    continue
                
                try:
                    # 提取股票代码和数据
                    parts = line.split('=')
                    if len(parts) < 2:
                        continue
                    
                    data_str = parts[1].strip().strip('"')
                    
                    if not data_str:
                        continue
                    
                    fields = data_str.split('~')
                    if len(fields) < 33:
                        continue
                    
                    # 提取关键字段
                    # fields[1]: 股票名称
                    # fields[2]: 股票代码
                    # fields[3]: 当前价格
                    # fields[4]: 昨收价
                    # fields[32]: 涨跌幅(%)
                    code = fields[2]
                    current_price = float(fields[3]) if fields[3] else 0
                    pre_close = float(fields[4]) if fields[4] else 0
                    
                    # 腾讯接口直接返回涨跌幅，在第32个字段
                    change_pct = float(fields[32]) if fields[32] else 0
                    
                    result[code] = {
                        'price': current_price,
                        'pre_close': pre_close,
                        'change_pct': change_pct,
                        'name': fields[1]
                    }
                except (ValueError, IndexError) as e:
                    continue
                
    except Exception as e:
        print(f"腾讯批量行情获取失败: {e}")
    
    return result

def get_realtime_price(code):
    """获取实时价格和涨幅（保留兼容，单只股票）"""
    quotes = get_tencent_batch_quotes([code])
    if code in quotes:
        return quotes[code]['price'], quotes[code]['change_pct']
    return None, None

@app.route('/')
def index():
    """首页 - 默认跳转到5拉升"""
    from flask import redirect
    return redirect('/5拉升')

@app.route('/api/stocks')
def get_stocks():
    """获取今日股票列表"""
    try:
        r = get_redis_client()
        today = datetime.now().strftime('%Y-%m-%d')
        key = f"stocks:{today}"
        
        stocks_data = r.hgetall(key)
        stocks = []
        codes = []
        
        for code, data_str in stocks_data.items():
            stock = json.loads(data_str)
            stocks.append(stock)
            codes.append(code)
        
        # 收盘后直接使用Redis中的数据，不再获取实时行情
        if not is_after_market_close():
            # 交易时间批量获取实时涨幅（腾讯接口，一次请求）
            if should_fetch_quotes() and codes:
                quotes = get_tencent_batch_quotes(codes)
                for stock in stocks:
                    code = stock.get('code')
                    if code in quotes:
                        stock['today_change'] = round(quotes[code]['change_pct'], 2)
                        stock['current_price'] = quotes[code]['price']
            
            # 计算今日K线特征（30秒缓存）
            if codes:
                k_features = calculate_k_features(codes)
                for stock in stocks:
                    code = stock.get('code')
                    if code in k_features:
                        features = k_features[code]
                        stock['is_limit_up'] = features.get('is_limit_up', False)
                        stock['is_high_gain'] = features.get('is_high_gain', False)
                        stock['is_barefoot_positive'] = features.get('is_barefoot_positive', False)
                        stock['today_highest_gain'] = features.get('today_highest_gain')
        
        # 按今日涨幅排序
        stocks.sort(key=lambda x: x.get('today_change', 0), reverse=True)
        
        return no_cache_response({
            'success': True,
            'data': stocks,
            'date': today,
            'is_trading': is_trading_time(),
            'update_time': datetime.now().strftime('%H:%M:%S')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/realtime')
def get_realtime_quotes():
    """
    专用实时涨跌幅接口 - 腾讯财经批量获取
    前端3秒轮询此接口，极速刷新涨跌幅
    收盘后返回Redis中存储的收盘数据
    """
    try:
        r = get_redis_client()
        today = datetime.now().strftime('%Y-%m-%d')
        key = f"stocks:{today}"
        
        stocks_data = r.hgetall(key)
        codes = list(stocks_data.keys())
        
        if not codes:
            return no_cache_response({
                'success': True,
                'data': {},
                'update_time': datetime.now().strftime('%H:%M:%S')
            })
        
        # 收盘后直接返回Redis中的数据
        if is_after_market_close():
            result = {}
            for code, data_str in stocks_data.items():
                stock = json.loads(data_str)
                result[code] = {
                    'change_pct': stock.get('today_change', 0),
                    'price': stock.get('current_price', 0)
                }
            return no_cache_response({
                'success': True,
                'data': result,
                'is_trading': False,
                'update_time': datetime.now().strftime('%H:%M:%S')
            })
        
        # 腾讯批量接口一次获取所有涨跌幅
        quotes = get_tencent_batch_quotes(codes)
        
        # 返回简化数据：{code: {change_pct, price}}
        result = {}
        for code, quote in quotes.items():
            result[code] = {
                'change_pct': round(quote['change_pct'], 2),
                'price': quote['price']
            }
        
        return no_cache_response({
            'success': True,
            'data': result,
            'is_trading': is_trading_time(),
            'update_time': datetime.now().strftime('%H:%M:%S')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/stocks/history/<date>')
def get_stocks_by_date(date):
    """获取指定日期的股票列表"""
    try:
        r = get_redis_client()
        key = f"stocks:{date}"
        
        stocks_data = r.hgetall(key)
        stocks = []
        codes = []
        
        for code, data_str in stocks_data.items():
            stock = json.loads(data_str)
            stocks.append(stock)
            codes.append(code)
        
        # 如果是今天的数据且在交易时间，获取实时行情
        today = datetime.now().strftime('%Y-%m-%d')
        if date == today and not is_after_market_close():
            if should_fetch_quotes() and codes:
                quotes = get_tencent_batch_quotes(codes)
                for stock in stocks:
                    code = stock.get('code')
                    if code in quotes:
                        stock['today_change'] = round(quotes[code]['change_pct'], 2)
                        stock['current_price'] = quotes[code]['price']
        
        stocks.sort(key=lambda x: x.get('today_change', 0), reverse=True)
        
        return jsonify({'success': True, 'data': stocks, 'date': date})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/dates')
def get_dates():
    """获取所有有数据的日期"""
    try:
        r = get_redis_client()
        keys = r.keys('stocks:*')
        dates = [k.replace('stocks:', '') for k in keys]
        dates.sort(reverse=True)
        return jsonify({'success': True, 'data': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ========== 收藏功能API ==========
from flask import request

@app.route('/api/favorites/add', methods=['POST'])
def add_favorite():
    """添加收藏
    请求体: {code, name, group, date(可选)}
    Redis key: favorites:{group}:{date}
    """
    try:
        data = request.get_json()
        code = data.get('code')
        name = data.get('name')
        group = data.get('group')  # 1 或 2
        date = data.get('date') or datetime.now().strftime('%Y-%m-%d')
        
        if not code or not group:
            return jsonify({'success': False, 'error': '缺少必要参数'})
        
        r = get_redis_client()
        key = f"favorites:{group}:{date}"
        
        stock_data = json.dumps({
            'code': code,
            'name': name,
            'add_time': datetime.now().strftime('%H:%M:%S')
        })
        
        r.hset(key, code, stock_data)
        
        return jsonify({'success': True, 'message': '收藏成功'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/favorites/remove', methods=['POST'])
def remove_favorite():
    """取消收藏"""
    try:
        data = request.get_json()
        code = data.get('code')
        group = data.get('group')
        date = data.get('date') or datetime.now().strftime('%Y-%m-%d')
        
        if not code or not group:
            return jsonify({'success': False, 'error': '缺少必要参数'})
        
        r = get_redis_client()
        key = f"favorites:{group}:{date}"
        r.hdel(key, code)
        
        return jsonify({'success': True, 'message': '取消收藏成功'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/favorites/list')
def get_favorites():
    """获取收藏列表，按日期和分组返回"""
    try:
        r = get_redis_client()
        
        # 获取所有收藏key
        keys1 = r.keys('favorites:1:*')
        keys2 = r.keys('favorites:2:*')
        
        result = {
            'group1': {},  # 收藏1: 阴线都向上
            'group2': {}   # 收藏2: 一字横盘
        }
        
        # 处理收藏1
        for key in keys1:
            date = key.replace('favorites:1:', '')
            stocks_data = r.hgetall(key)
            stocks = []
            for code, data_str in stocks_data.items():
                stock = json.loads(data_str)
                stock['code'] = code
                stocks.append(stock)
            if stocks:
                result['group1'][date] = stocks
        
        # 处理收藏2
        for key in keys2:
            date = key.replace('favorites:2:', '')
            stocks_data = r.hgetall(key)
            stocks = []
            for code, data_str in stocks_data.items():
                stock = json.loads(data_str)
                stock['code'] = code
                stocks.append(stock)
            if stocks:
                result['group2'][date] = stocks
        
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/favorites/check')
def check_favorite():
    """检查股票是否已收藏"""
    try:
        code = request.args.get('code')
        date = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
        
        if not code:
            return jsonify({'success': False, 'error': '缺少code参数'})
        
        r = get_redis_client()
        
        in_group1 = r.hexists(f"favorites:1:{date}", code)
        in_group2 = r.hexists(f"favorites:2:{date}", code)
        
        return jsonify({
            'success': True,
            'data': {
                'group1': in_group1,
                'group2': in_group2
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/debug/performance')
def get_performance_debug():
    """获取性能调试信息 - 显示每只股票的处理耗时"""
    try:
        r = get_redis_client()
        today = datetime.now().strftime('%Y-%m-%d')
        
        result = {
            'bimai1': {},
            'bimai2': {},
            'bimai3': {},
            'bimai4': {}
        }
        
        # 获取各个必买页面的性能数据
        for category in ['bimai1', 'bimai2', 'bimai3', 'bimai4']:
            key_map = {
                'bimai1': 'stocks_jpbm1',
                'bimai2': 'stocks_jpbm2',
                'bimai3': 'stocks_jpbm3',
                'bimai4': 'stocks_jpbm4'
            }
            
            key = f"{key_map[category]}:{today}"
            stocks_data = r.hgetall(key)
            
            if stocks_data:
                # 统计信息
                stock_count = len(stocks_data)
                stocks_detail = []
                total_process_time = 0
                
                # 获取每只股票的详细信息
                for code, data_str in stocks_data.items():
                    try:
                        stock = json.loads(data_str)
                        
                        stocks_detail.append({
                            'code': code,
                            'name': stock.get('name', ''),
                            # 时间节点
                            't0_source': stock.get('perf_t0_source', ''),
                            't1_txt_read': stock.get('perf_t1_txt_read', ''),
                            't2_start_process': stock.get('perf_t2_start_process', ''),
                            't3_start_indicators': stock.get('perf_t3_start_indicators', ''),
                            't4_end_indicators': stock.get('perf_t4_end_indicators', ''),
                            't5_before_redis': stock.get('perf_t5_before_redis', ''),
                            't6_after_redis': stock.get('perf_t6_after_redis', ''),
                            # 各阶段耗时
                            'duration_t1_t2': stock.get('perf_duration_t1_t2', 0),
                            'duration_t2_t3': stock.get('perf_duration_t2_t3', 0),
                            'duration_t3_t4': stock.get('perf_duration_t3_t4', 0),
                            'duration_t4_t5': stock.get('perf_duration_t4_t5', 0),
                            'duration_t5_t6': stock.get('perf_duration_t5_t6', 0),
                            'total_duration': stock.get('perf_total_duration', 0)
                        })
                        total_process_time += stock.get('perf_total_duration', 0)
                    except Exception as e:
                        print(f"解析股票数据失败 {code}: {e}")
                        pass
                
                # 按处理时间排序
                stocks_detail.sort(key=lambda x: x.get('t1_txt_read', ''))
                
                # 获取第一个和最后一个股票的时间
                first_time = stocks_detail[0]['t1_txt_read'] if stocks_detail else None
                last_time = stocks_detail[-1]['t6_after_redis'] if stocks_detail else None
                
                result[category] = {
                    'stock_count': stock_count,
                    'first_process_time': first_time,
                    'last_process_time': last_time,
                    'total_process_duration': round(total_process_time, 3),
                    'avg_process_duration': round(total_process_time / stock_count, 3) if stock_count > 0 else 0,
                    'redis_key': key,
                    'data_exists': True,
                    'stocks': stocks_detail
                }
                
                # 计算总耗时
                if first_time and last_time:
                    try:
                        from datetime import datetime as dt
                        first_dt = dt.strptime(first_time, '%H:%M:%S.%f')
                        last_dt = dt.strptime(last_time, '%H:%M:%S.%f')
                        duration = (last_dt - first_dt).total_seconds()
                        result[category]['total_duration'] = round(duration, 3)
                    except:
                        pass
            else:
                result[category] = {
                    'stock_count': 0,
                    'data_exists': False,
                    'redis_key': key,
                    'stocks': []
                }
        
        return jsonify({
            'success': True,
            'date': today,
            'data': result,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)


# ========== 阴线都向上页面相关API ==========
@app.route('/阴线都向上')
def yxdxs():
    """阴线都向上页面"""
    return render_template('yxdxs.html')

@app.route('/极品关注')
def jpgz():
    """极品关注页面"""
    return render_template('jpgz.html')

@app.route('/同花顺板块')
def ths_bankuai_page():
    """同花顺板块页面"""
    return render_template('ths_bankuai.html')

def _ths_normalize_date(date_text=None):
    raw = str(date_text or '').strip()
    if not raw:
        return datetime.now().strftime('%Y%m%d')
    digits = re.sub(r'\D', '', raw)
    if len(digits) >= 8:
        return digits[:8]
    return datetime.now().strftime('%Y%m%d')

def _ths_format_date(compact_date):
    text = str(compact_date or '')
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text

def _ths_decode_payload(raw):
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='ignore')
    return json.loads(raw)

@app.route('/api/ths/dates')
def get_ths_dates():
    """获取同花顺板块 Redis 中已有日期"""
    try:
        r = get_redis_client()
        dates = set()
        for key in r.keys('ths:*'):
            if isinstance(key, bytes):
                key = key.decode('utf-8', errors='ignore')
            if key in ('ths:latest',) or key.endswith(':timestamp'):
                continue
            date_part = key.split(':', 1)[1] if ':' in key else ''
            if len(date_part) == 8 and date_part.isdigit():
                try:
                    parsed_date = datetime.strptime(date_part, '%Y%m%d')
                except ValueError:
                    continue
                dates.add(parsed_date.strftime('%Y-%m-%d'))
        latest_key = r.get('ths:latest')
        latest_date = ''
        if latest_key:
            if isinstance(latest_key, bytes):
                latest_key = latest_key.decode('utf-8', errors='ignore')
            latest_part = latest_key.split(':', 1)[1] if ':' in latest_key else ''
            if len(latest_part) == 8 and latest_part.isdigit():
                try:
                    latest_date = datetime.strptime(latest_part, '%Y%m%d').strftime('%Y-%m-%d')
                except ValueError:
                    latest_date = ''
                if latest_date:
                    dates.add(latest_date)
        return jsonify({'success': True, 'dates': sorted(dates, reverse=True), 'latest_date': latest_date})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})

@app.route('/api/ths/data')
@app.route('/api/ths/data/<date>')
def get_ths_data(date=None):
    """读取同花顺板块 Redis 最新 JSON 数据"""
    try:
        r = get_redis_client()
        requested = date or request.args.get('date')
        if requested:
            compact_date = _ths_normalize_date(requested)
            key = f"ths:{compact_date}"
        else:
            key = r.get('ths:latest') or f"ths:{datetime.now().strftime('%Y%m%d')}"
            if isinstance(key, bytes):
                key = key.decode('utf-8', errors='ignore')
            compact_date = key.split(':', 1)[1] if ':' in key else _ths_normalize_date()

        payload = _ths_decode_payload(r.get(key))
        if not payload:
            return jsonify({
                'success': True,
                'date': _ths_format_date(compact_date),
                'redis_key': key,
                'data': None,
                'boards': [],
                'stocks': [],
                'boards_summary': [],
                'message': f'没有找到Redis数据: {key}'
            })

        boards_summary = payload.get('boards_summary') or []
        stocks = payload.get('stocks') or []
        boards = payload.get('boards') or []
        return jsonify({
            'success': True,
            'date': _ths_format_date(payload.get('date') or compact_date),
            'redis_key': key,
            'data': payload,
            'boards': boards,
            'stocks': stocks,
            'boards_summary': boards_summary,
            'counts': payload.get('counts', {}),
            'snapshot': payload.get('snapshot', {}),
            'timings': payload.get('timings', {}),
            'updated_at': payload.get('updated_at', '')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'boards': [], 'stocks': [], 'boards_summary': []})

@app.route('/test_yxdxs')
def test_yxdxs():
    """阴线都向上测试页面"""
    return render_template('test_yxdxs.html')

@app.route('/test_doji_star')
def test_doji_star():
    """阴线都是十字星测试页面"""
    return render_template('test_doji_star.html')

@app.route('/5拉升阴少3不下杀')
def lsys3bxs():
    """5拉升阴少3不下杀页面"""
    return render_template('5lsys3bxs.html')

@app.route('/api/5lsys/stocks')
def get_5lsys_stocks():
    """获取5拉升阴少3不下杀股票列表"""
    try:
        import base64
        import time as time_module
        
        start_time = time_module.time()
        r = get_redis_client()
        date_param = request.args.get('date')
        if date_param:
            today = date_param
        else:
            today = datetime.now().strftime('%Y-%m-%d')
        key = f"stocks_5lsys:{today}"
        
        ping_start = time_module.time()
        r.ping()
        network_latency = (time_module.time() - ping_start) * 1000
        
        scan_start = time_module.time()
        cursor = 0
        all_keys = []
        
        while True:
            cursor, keys = r.hscan(key, cursor, count=100)
            all_keys.extend(keys.items())
            if cursor == 0:
                break
        
        scan_time = (time_module.time() - scan_start) * 1000
        
        if not all_keys:
            total_time = (time_module.time() - start_time) * 1000
            return jsonify({
                'success': True, 
                'data': [],
                'performance': {
                    'total_time': f"{total_time:.2f}ms",
                    'network_latency': f"{network_latency:.2f}ms",
                    'count': 0
                }
            })
        
        parse_start = time_module.time()
        stocks = []
        
        for code, data_str in all_keys:
            try:
                try:
                    decoded_data = base64.b64decode(data_str).decode('utf-8')
                    stock_info = json.loads(decoded_data)
                except:
                    stock_info = json.loads(data_str)
                
                stock_item = {
                    'code': stock_info.get('code', code.decode('utf-8') if isinstance(code, bytes) else code),
                    'name': stock_info.get('name', ''),
                    'strategies': stock_info.get('strategies', []),
                    'group': stock_info.get('group', '未分组'),
                    'today_change': stock_info.get('today_change', 0),
                    'ls_num': stock_info.get('ls_num', 0),
                    'no_xs_num': stock_info.get('no_xs_num', 0),
                    'market_cap': stock_info.get('market_cap', 0),
                    'warning_time': stock_info.get('warning_time', 0)
                }
                if 'is_bare_yang' in stock_info:
                    stock_item['is_bare_yang'] = stock_info['is_bare_yang']
                if 'is_nearly_bare_yang' in stock_info:
                    stock_item['is_nearly_bare_yang'] = stock_info['is_nearly_bare_yang']
                stocks.append(stock_item)
            except Exception as e:
                print(f"解析股票数据失败 {code}: {e}")
                continue
        
        parse_time = (time_module.time() - parse_start) * 1000
        total_time = (time_module.time() - start_time) * 1000
        
        return jsonify({
            'success': True, 
            'data': stocks,
            'performance': {
                'total_time': f"{total_time:.2f}ms",
                'network_latency': f"{network_latency:.2f}ms",
                'scan_time': f"{scan_time:.2f}ms",
                'parse_time': f"{parse_time:.2f}ms",
                'count': len(stocks)
            },
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        print(f"获取5拉升阴少3不下杀数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/5lsys/realtime', methods=['POST'])
def get_5lsys_realtime():
    """5拉升阴少3不下杀实时涨跌幅接口"""
    try:
        data = request.get_json()
        codes = data.get('codes', [])
        
        if not codes:
            return jsonify({'success': True, 'data': {}})
        
        realtime_data = {}
        for code in codes:
            try:
                if '.' in code:
                    if code.endswith('.SH'):
                        tencent_code = 'sh' + code.split('.')[0]
                    elif code.endswith('.SZ'):
                        tencent_code = 'sz' + code.split('.')[0]
                    else:
                        continue
                else:
                    code_num = code
                    if code_num.startswith('6'):
                        tencent_code = 'sh' + code_num
                    elif code_num.startswith(('0', '3', '2')):
                        tencent_code = 'sz' + code_num
                    elif code_num.startswith('688'):
                        tencent_code = 'sh' + code_num
                    else:
                        continue
                
                url = f'http://qt.gtimg.cn/q={tencent_code}'
                response = requests.get(url, timeout=3)
                
                if response.status_code == 200:
                    content = response.text
                    if '~' in content:
                        parts = content.split('~')
                        if len(parts) > 32:
                            try:
                                current_price = float(parts[3])
                                prev_close = float(parts[4])
                                if prev_close > 0:
                                    change = ((current_price - prev_close) / prev_close) * 100
                                    realtime_data[code] = {'change': round(change, 2)}
                            except (ValueError, IndexError) as e:
                                continue
            except Exception as e:
                continue
        
        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/5lsys/dates')
def get_5lsys_dates():
    """获取5拉升阴少3不下杀所有有数据的日期"""
    try:
        r = get_redis_client()
        keys = r.keys('stocks_5lsys:*')
        dates = []
        
        for key in keys:
            if ':' in key and not key.endswith(':timestamp'):
                date_part = key.split(':', 1)[1]
                if date_part and '-' in date_part:
                    dates.append(date_part)
        
        dates.sort(reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/5lsys/market_cap/<code>')
def get_5lsys_market_cap(code):
    """获取单个股票的市值"""
    try:
        import tushare as ts
        
        if code.startswith('6'):
            ts_code = f"{code}.SH"
        elif code.startswith('0') or code.startswith('3'):
            ts_code = f"{code}.SZ"
        elif code.startswith('8') or code.startswith('4'):
            ts_code = f"{code}.BJ"
        else:
            ts_code = f"{code}.SZ"
        
        pro = ts.pro_api()
        df = pro.daily_basic(ts_code=ts_code, fields='ts_code,total_mv')
        
        if df is not None and not df.empty:
            market_cap = float(df.iloc[0]['total_mv']) / 10000
            return jsonify({
                'success': True,
                'code': code,
                'market_cap': round(market_cap, 2)
            })
        else:
            return jsonify({'success': False, 'error': '未获取到市值数据', 'market_cap': 0})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'market_cap': 0})

@app.route('/api/5lsys/check_update')
def check_5lsys_update():
    """检查5拉升阴少3不下杀Redis数据是否更新"""
    try:
        import time as time_module
        start_time = time_module.time()
        
        r = get_redis_client()
        today = datetime.now().strftime('%Y-%m-%d')
        key = f"stocks_5lsys:{today}"
        timestamp_key = f"stocks_5lsys:{today}:timestamp"
        
        pipe = r.pipeline()
        pipe.get(timestamp_key)
        pipe.exists(key)
        results = pipe.execute()
        
        timestamp = results[0]
        key_exists = results[1]
        
        total_time = (time_module.time() - start_time) * 1000
        
        if key_exists:
            data_count = r.hlen(key)
            if data_count > 0:
                if not timestamp:
                    import time
                    timestamp = str(int(time.time() * 1000))
                    r.set(timestamp_key, timestamp)
                
                return jsonify({
                    'success': True, 
                    'updated': True,
                    'timestamp': timestamp,
                    'count': data_count,
                    'refresh_interval': get_redis_refresh_interval()
                })
        
        return jsonify({
            'success': True,
            'updated': False,
            'timestamp': None,
            'count': 0,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/yxdxs/stocks')
def get_yxdxs_stocks():
    """获取阴线都向上今日股票列表（优化版：使用SCAN和Pipeline）"""
    try:
        import base64
        import time as time_module
        
        # 记录开始时间
        start_time = time_module.time()
        
        r = get_redis_client()
        date_param = request.args.get('date')
        if date_param:
            today = date_param
        else:
            today = datetime.now().strftime('%Y-%m-%d')
        key = f"stocks_yxdxs:{today}"
        
        # 测量网络延迟
        ping_start = time_module.time()
        r.ping()
        network_latency = (time_module.time() - ping_start) * 1000
        print(f"Redis网络延迟: {network_latency:.2f}ms")
        
        # 使用SCAN代替HGETALL（优化大数据量）
        scan_start = time_module.time()
        cursor = 0
        all_keys = []
        
        # HSCAN逐批次获取，避免阻塞
        while True:
            cursor, keys = r.hscan(key, cursor, count=100)
            all_keys.extend(keys.items())
            if cursor == 0:
                break
        
        scan_time = (time_module.time() - scan_start) * 1000
        print(f"HSCAN耗时: {scan_time:.2f}ms, 获取{len(all_keys)}条数据")
        
        if not all_keys:
            total_time = (time_module.time() - start_time) * 1000
            return jsonify({
                'success': True, 
                'data': [],
                'performance': {
                    'total_time': f"{total_time:.2f}ms",
                    'network_latency': f"{network_latency:.2f}ms",
                    'count': 0
                }
            })
        
        # 使用Pipeline批量解析（减少网络往返）
        parse_start = time_module.time()
        stocks = []
        
        for code, data_str in all_keys:
            try:
                # 尝试Base64解码
                try:
                    decoded_data = base64.b64decode(data_str).decode('utf-8')
                    stock_info = json.loads(decoded_data)
                except:
                    # 如果不是Base64，直接解析JSON
                    stock_info = json.loads(data_str)
                
                stock_item = {
                    'code': stock_info.get('code', code.decode('utf-8') if isinstance(code, bytes) else code),
                    'name': stock_info.get('name', ''),
                    'strategies': stock_info.get('strategies', []),
                    'group': stock_info.get('group', '未分组'),
                    'today_change': stock_info.get('today_change', 0),
                    'ls_num': stock_info.get('ls_num', 0),
                    'no_xs_num': stock_info.get('no_xs_num', 0),
                    'market_cap': stock_info.get('market_cap', 0),
                    'warning_time': stock_info.get('warning_time', 0)
                }
                if 'is_bare_yang' in stock_info:
                    stock_item['is_bare_yang'] = stock_info['is_bare_yang']
                if 'is_nearly_bare_yang' in stock_info:
                    stock_item['is_nearly_bare_yang'] = stock_info['is_nearly_bare_yang']
                stocks.append(stock_item)
            except Exception as e:
                print(f"解析股票数据失败 {code}: {e}")
                continue
        
        parse_time = (time_module.time() - parse_start) * 1000
        total_time = (time_module.time() - start_time) * 1000
        
        print(f"数据解析耗时: {parse_time:.2f}ms")
        print(f"总耗时: {total_time:.2f}ms")
        
        # 返回性能指标
        return jsonify({
            'success': True, 
            'data': stocks,
            'performance': {
                'total_time': f"{total_time:.2f}ms",
                'network_latency': f"{network_latency:.2f}ms",
                'scan_time': f"{scan_time:.2f}ms",
                'parse_time': f"{parse_time:.2f}ms",
                'count': len(stocks)
            },
            'refresh_interval': get_redis_refresh_interval()  # 返回刷新间隔
        })
    except Exception as e:
        print(f"获取阴线都向上数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/yxdxs/realtime', methods=['POST'])
def get_yxdxs_realtime():
    """阴线都向上实时涨跌幅接口"""
    try:
        from flask import request
        data = request.get_json()
        codes = data.get('codes', [])
        
        if not codes:
            return jsonify({'success': True, 'data': {}})
        
        # 获取实时行情
        realtime_data = {}
        for code in codes:
            try:
                # 转换股票代码格式用于腾讯接口
                # 处理可能没有后缀的情况
                if '.' in code:
                    # 有后缀的情况
                    if code.endswith('.SH'):
                        tencent_code = 'sh' + code.split('.')[0]
                    elif code.endswith('.SZ'):
                        tencent_code = 'sz' + code.split('.')[0]
                    else:
                        continue
                else:
                    # 没有后缀，根据代码判断
                    code_num = code
                    if code_num.startswith('6'):
                        tencent_code = 'sh' + code_num
                    elif code_num.startswith(('0', '3', '2')):
                        tencent_code = 'sz' + code_num
                    elif code_num.startswith('688'):
                        tencent_code = 'sh' + code_num
                    else:
                        continue
                
                # 调用腾讯接口
                url = f'http://qt.gtimg.cn/q={tencent_code}'
                response = requests.get(url, timeout=3)
                
                if response.status_code == 200:
                    content = response.text
                    if '~' in content:
                        parts = content.split('~')
                        if len(parts) > 32:
                            try:
                                current_price = float(parts[3])
                                prev_close = float(parts[4])
                                if prev_close > 0:
                                    change = ((current_price - prev_close) / prev_close) * 100
                                    realtime_data[code] = {'change': round(change, 2)}
                            except (ValueError, IndexError) as e:
                                print(f"解析{code}价格失败: {e}")
                                continue
            except Exception as e:
                print(f"获取{code}实时行情失败: {e}")
                continue
        
        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        print(f"实时行情API异常: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/yxdxs/dates')
def get_yxdxs_dates():
    """获取阴线都向上所有有数据的日期"""
    try:
        r = get_redis_client()
        
        # 查找所有相关的key
        keys = r.keys('stocks_yxdxs:*')
        
        dates = []
        
        # 处理格式的key: stocks_yxdxs:2026-01-21
        for key in keys:
            if ':' in key and not key.endswith(':timestamp'):
                date_part = key.split(':', 1)[1]
                if date_part and '-' in date_part:
                    dates.append(date_part)
        
        # 排序（最新日期在前）
        dates.sort(reverse=True)
        
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        print(f"获取阴线都向上日期列表失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/yxdxs/market_cap/<code>')
def get_yxdxs_market_cap(code):
    """获取单个股票的市值（使用Tushare）"""
    try:
        import tushare as ts
        
        # 转换股票代码格式
        if code.startswith('6'):
            ts_code = f"{code}.SH"
        elif code.startswith('0') or code.startswith('3'):
            ts_code = f"{code}.SZ"
        elif code.startswith('8') or code.startswith('4'):
            ts_code = f"{code}.BJ"
        else:
            ts_code = f"{code}.SZ"
        
        # 使用Tushare获取市值
        pro = ts.pro_api()
        df = pro.daily_basic(ts_code=ts_code, fields='ts_code,total_mv')
        
        if df is not None and not df.empty:
            # total_mv单位是万元，转换为亿元
            market_cap = float(df.iloc[0]['total_mv']) / 10000
            return jsonify({
                'success': True,
                'code': code,
                'market_cap': round(market_cap, 2)
            })
        else:
            return jsonify({
                'success': False,
                'error': '未获取到市值数据',
                'market_cap': 0
            })
    except Exception as e:
        print(f"获取市值失败 {code}: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'market_cap': 0
        })

@app.route('/api/yxdxs/check_update')
def check_yxdxs_update():
    """检查阴线都向上Redis数据是否更新（优化版：使用Pipeline）"""
    try:
        import time as time_module
        start_time = time_module.time()
        
        r = get_redis_client()
        today = datetime.now().strftime('%Y-%m-%d')
        key = f"stocks_yxdxs:{today}"
        timestamp_key = f"stocks_yxdxs:{today}:timestamp"
        
        # 使用Pipeline批量获取（减少网络往返）
        pipe = r.pipeline()
        pipe.get(timestamp_key)
        pipe.exists(key)
        results = pipe.execute()
        
        timestamp = results[0]
        key_exists = results[1]
        
        total_time = (time_module.time() - start_time) * 1000
        print(f"check_update耗时: {total_time:.2f}ms")
        
        # 如果键存在且有数据（使用Pipeline已获取的结果）
        if key_exists:
            # 使用HLEN获取数据量（比HGETALL快很多）
            data_count = r.hlen(key)
            if data_count > 0:
                # 如果没有时间戳，创建一个（兼容旧数据）
                if not timestamp:
                    import time
                    timestamp = str(int(time.time() * 1000))  # 毫秒级时间戳
                    r.set(timestamp_key, timestamp)
                
                return jsonify({
                    'success': True, 
                    'updated': True,
                    'timestamp': timestamp,
                    'count': data_count,
                    'refresh_interval': get_redis_refresh_interval()  # 动态刷新间隔
                })
        
        return jsonify({
            'success': True, 
            'updated': False, 
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/阴线都是十字星')
def doji_star_page():
    """阴线都是十字星页面"""
    return render_template('doji_star.html')

@app.route('/阴线都是十字星调试')
def doji_star_debug_page():
    """阴线都是十字星调试页面"""
    return render_template('doji_star_debug.html')

@app.route('/api/doji_star/stocks')
def get_doji_star_stocks():
    """获取阴线都是十字星股票列表（优化版：使用SCAN和Pipeline）"""
    try:
        import base64
        import time as time_module
        
        # 记录开始时间
        start_time = time_module.time()
        
        r = get_redis_client()
        date_param = request.args.get('date')
        if date_param:
            today = date_param
        else:
            today = datetime.now().strftime('%Y-%m-%d')
        key = f"stocks_yxdszx:{today}"
        
        # 测量网络延迟
        ping_start = time_module.time()
        r.ping()
        network_latency = (time_module.time() - ping_start) * 1000
        print(f"Redis网络延迟: {network_latency:.2f}ms")
        
        # 使用SCAN代替HGETALL（优化大数据量）
        scan_start = time_module.time()
        cursor = 0
        all_keys = []
        
        # HSCAN逐批次获取，避免阻塞
        while True:
            cursor, keys = r.hscan(key, cursor, count=100)
            all_keys.extend(keys.items())
            if cursor == 0:
                break
        
        scan_time = (time_module.time() - scan_start) * 1000
        print(f"HSCAN耗时: {scan_time:.2f}ms, 获取{len(all_keys)}条数据")
        
        if not all_keys:
            total_time = (time_module.time() - start_time) * 1000
            return jsonify({
                'success': True, 
                'data': [],
                'performance': {
                    'total_time': f"{total_time:.2f}ms",
                    'network_latency': f"{network_latency:.2f}ms",
                    'count': 0
                }
            })
        
        # 使用Pipeline批量解析（减少网络往返）
        parse_start = time_module.time()
        stocks = []
        
        for code, data_str in all_keys:
            try:
                # 尝试Base64解码
                try:
                    decoded_data = base64.b64decode(data_str).decode('utf-8')
                    stock_info = json.loads(decoded_data)
                except:
                    # 如果不是Base64，直接解析JSON
                    stock_info = json.loads(data_str)
                
                stock_item = {
                    'code': stock_info.get('code', code.decode('utf-8') if isinstance(code, bytes) else code),
                    'name': stock_info.get('name', ''),
                    'strategies': stock_info.get('strategies', []),
                    'group': stock_info.get('group', '未分组'),
                    'today_change': stock_info.get('today_change', 0),
                    'ls_num': stock_info.get('ls_num', 0),
                    'no_xs_num': stock_info.get('no_xs_num', 0),
                    'market_cap': stock_info.get('market_cap', 0),
                    'warning_time': stock_info.get('warning_time', '')
                }
                if 'is_bare_yang' in stock_info:
                    stock_item['is_bare_yang'] = stock_info['is_bare_yang']
                if 'is_nearly_bare_yang' in stock_info:
                    stock_item['is_nearly_bare_yang'] = stock_info['is_nearly_bare_yang']
                stocks.append(stock_item)
            except Exception as e:
                print(f"解析股票数据失败 {code}: {e}")
                continue
        
        parse_time = (time_module.time() - parse_start) * 1000
        total_time = (time_module.time() - start_time) * 1000
        
        print(f"数据解析耗时: {parse_time:.2f}ms")
        print(f"总耗时: {total_time:.2f}ms")
        
        # 返回性能指标
        return jsonify({
            'success': True, 
            'data': stocks,
            'performance': {
                'total_time': f"{total_time:.2f}ms",
                'network_latency': f"{network_latency:.2f}ms",
                'scan_time': f"{scan_time:.2f}ms",
                'parse_time': f"{parse_time:.2f}ms",
                'count': len(stocks)
            },
            'refresh_interval': get_redis_refresh_interval()  # 返回刷新间隔
        })
    except Exception as e:
        print(f"获取阴线都是十字星数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/doji_star/realtime', methods=['POST'])
def get_doji_star_realtime():
    """阴线都是十字星实时涨跌幅接口"""
    try:
        from flask import request
        data = request.get_json()
        codes = data.get('codes', [])
        
        if not codes:
            return jsonify({'success': True, 'data': {}})
        
        # 获取实时行情
        realtime_data = {}
        for code in codes:
            try:
                # 转换股票代码格式用于腾讯接口
                if '.' in code:
                    # 有后缀的情况
                    if code.endswith('.SH'):
                        tencent_code = f"sh{code.replace('.SH', '')}"
                    elif code.endswith('.SZ'):
                        tencent_code = f"sz{code.replace('.SZ', '')}"
                    else:
                        tencent_code = f"sz{code.split('.')[0]}"
                else:
                    # 没有后缀，根据首位判断
                    if code.startswith('6'):
                        tencent_code = f"sh{code}"
                    else:
                        tencent_code = f"sz{code}"
                
                url = f"http://qt.gtimg.cn/q={tencent_code}"
                response = requests.get(url, timeout=2)
                response.encoding = 'gbk'
                
                if response.text:
                    # 解析腾讯接口返回的数据
                    match = response.text.strip().split('~')
                    if len(match) > 3:
                        current_price = float(match[3]) if match[3] else 0
                        pre_close = float(match[4]) if match[4] else 0
                        if pre_close > 0:
                            change = ((current_price - pre_close) / pre_close) * 100
                            realtime_data[code] = {'change': round(change, 2)}
            except Exception as e:
                print(f"获取股票{code}实时行情失败: {e}")
                continue
        
        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/doji_star/dates')
def get_doji_star_dates():
    """获取阴线都是十字星所有有数据的日期"""
    try:
        r = get_redis_client()
        
        # 查找所有相关的key
        keys = r.keys('stocks_yxdszx:*')
        
        dates = []
        
        # 处理格式的key: stocks_yxdszx:2026-01-21
        for key in keys:
            if ':' in key and not key.endswith(':timestamp'):
                date_part = key.split(':', 1)[1]
                if date_part and '-' in date_part:
                    dates.append(date_part)
        
        # 排序（最新日期在前）
        dates.sort(reverse=True)
        
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        print(f"获取阴线都是十字星日期列表失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/doji_star/market_cap/<code>')
def get_doji_star_market_cap(code):
    """获取单个股票的市值（使用Tushare）"""
    try:
        import tushare as ts
        
        # 转换股票代码格式
        if code.startswith('6'):
            ts_code = f"{code}.SH"
        elif code.startswith('0') or code.startswith('3'):
            ts_code = f"{code}.SZ"
        elif code.startswith('8') or code.startswith('4'):
            ts_code = f"{code}.BJ"
        else:
            ts_code = f"{code}.SZ"
        
        # 使用Tushare获取市值
        pro = ts.pro_api()
        df = pro.daily_basic(ts_code=ts_code, fields='ts_code,total_mv')
        
        if df is not None and not df.empty:
            # total_mv单位是万元，转换为亿元
            market_cap = float(df.iloc[0]['total_mv']) / 10000
            return jsonify({
                'success': True,
                'code': code,
                'market_cap': round(market_cap, 2)
            })
        else:
            return jsonify({
                'success': False,
                'error': '未获取到市值数据',
                'market_cap': 0
            })
    except Exception as e:
        print(f"获取市值失败 {code}: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'market_cap': 0
        })

@app.route('/api/doji_star/check_update')
def check_doji_star_update():
    """检查阴线都是十字星Redis数据是否更新（优化版：使用Pipeline）"""
    try:
        import time as time_module
        start_time = time_module.time()
        
        r = get_redis_client()
        today = datetime.now().strftime('%Y-%m-%d')
        key = f"stocks_yxdszx:{today}"
        timestamp_key = f"stocks_yxdszx:{today}:timestamp"
        
        # 使用Pipeline批量获取（减少网络往返）
        pipe = r.pipeline()
        pipe.get(timestamp_key)
        pipe.exists(key)
        results = pipe.execute()
        
        timestamp = results[0]
        key_exists = results[1]
        
        total_time = (time_module.time() - start_time) * 1000
        print(f"check_update耗时: {total_time:.2f}ms")
        
        # 如果键存在且有数据（使用Pipeline已获取的结果）
        if key_exists:
            # 使用HLEN获取数据量（比HGETALL快很多）
            data_count = r.hlen(key)
            if data_count > 0:
                # 如果没有时间戳，创建一个（兼容旧数据）
                if not timestamp:
                    import time
                    timestamp = str(int(time.time() * 1000))  # 毫秒级时间戳
                    r.set(timestamp_key, timestamp)
                
                return jsonify({
                    'success': True, 
                    'updated': True,
                    'timestamp': timestamp,
                    'count': data_count,
                    'refresh_interval': get_redis_refresh_interval()  # 动态刷新间隔
                })
        
        return jsonify({
            'success': True, 
            'updated': False, 
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ========== 5拉升20阴少页面相关API ==========
@app.route('/5拉升20阴少')
def ls20ys_page():
    """5拉升20阴少页面"""
    return render_template('5ls20ys.html')

@app.route('/api/5ls20ys/stocks')
@app.route('/api/5ls20ys/stocks/<date>')
def get_5ls20ys_stocks(date=None):
    """获取5拉升20阴少股票列表（兼容多种数据格式和key格式）"""
    try:
        import base64
        import time as time_module
        from flask import request
        
        # 记录开始时间
        start_time = time_module.time()
        
        r = get_redis_client()
        
        # 如果没有指定日期，使用今天
        if not date:
            date = request.args.get('date')
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
        
        # 转换日期格式
        today = date
        today_compact = date.replace('-', '')
        
        # 尝试两种key格式
        key_standard = f"stocks_5ls20:{today}"
        key_compact = f"5ls20:{today_compact}"
        
        # 测量网络延迟
        ping_start = time_module.time()
        r.ping()
        network_latency = (time_module.time() - ping_start) * 1000
        print(f"Redis网络延迟: {network_latency:.2f}ms")
        
        # 检查哪个key存在并获取数据类型
        key = None
        key_type = None
        
        if r.exists(key_compact):
            key = key_compact
            key_type = r.type(key)
            print(f"使用紧凑格式key: {key}, 类型: {key_type}")
        elif r.exists(key_standard):
            key = key_standard
            key_type = r.type(key)
            print(f"使用标准格式key: {key}, 类型: {key_type}")
        
        if not key:
            total_time = (time_module.time() - start_time) * 1000
            return jsonify({
                'success': True,
                'data': [],
                'performance': {
                    'total_time': f"{total_time:.2f}ms",
                    'network_latency': f"{network_latency:.2f}ms",
                    'count': 0
                },
                'message': f'没有找到数据key: {key_standard} 或 {key_compact}'
            })
        
        # 使用SCAN或直接GET获取数据
        scan_start = time_module.time()
        all_keys = []
        
        if key_type == 'hash':
            # Hash类型：使用HSCAN逐批次获取
            cursor = 0
            while True:
                cursor, keys = r.hscan(key, cursor, count=100)
                all_keys.extend(keys.items())
                if cursor == 0:
                    break
        elif key_type == 'string':
            # String类型：直接GET获取JSON数组
            data_str = r.get(key)
            if data_str:
                try:
                    data_list = json.loads(data_str)
                    # 转换为(code, json_str)格式
                    for item in data_list:
                        code = item.get('code', '')
                        all_keys.append((code, json.dumps(item, ensure_ascii=False)))
                except:
                    pass
        
        scan_time = (time_module.time() - scan_start) * 1000
        print(f"数据获取耗时: {scan_time:.2f}ms, 获取{len(all_keys)}条数据")
        
        if not all_keys:
            total_time = (time_module.time() - start_time) * 1000
            return jsonify({
                'success': True, 
                'data': [],
                'performance': {
                    'total_time': f"{total_time:.2f}ms",
                    'network_latency': f"{network_latency:.2f}ms",
                    'count': 0
                }
            })
        
        # 使用Pipeline批量解析（减少网络往返）
        parse_start = time_module.time()
        stocks = []
        
        for code, data_str in all_keys:
            try:
                # 尝试Base64解码
                try:
                    decoded_data = base64.b64decode(data_str).decode('utf-8')
                    stock_info = json.loads(decoded_data)
                except:
                    # 如果不是Base64，直接解析JSON
                    stock_info = json.loads(data_str)
                
                # 转换分组名称（兼容"第一组"/"第二组"格式）
                group = stock_info.get('group', '未分组')
                if "第一组" in str(group):
                    group = "分组1"
                elif "第二组" in str(group):
                    group = "分组2"
                
                # 兼容不同字段名
                today_change = stock_info.get('today_change', stock_info.get('current_gain', 0))
                ls_num = stock_info.get('ls_num', stock_info.get('pull_count_10', 0))
                warning_time = stock_info.get('warning_time', stock_info.get('time', ''))
                bare_foot_yang_count_10 = stock_info.get('bare_foot_yang_count_10', 0)
                
                stocks.append({
                    'code': stock_info.get('code', code.decode('utf-8') if isinstance(code, bytes) else code),
                    'name': stock_info.get('name', ''),
                    'strategies': stock_info.get('strategies', ['5拉升20']),
                    'group': group,
                    'today_change': today_change,
                    'ls_num': ls_num,
                    'yin_count_10': stock_info.get('yin_count_10', 0),
                    'yin_count_20': stock_info.get('yin_count_20', 0),
                    'pull_count_10': stock_info.get('pull_count_10', 0),
                    'bare_foot_yang_count_10': bare_foot_yang_count_10,
                    'no_xs_num_10': stock_info.get('no_xs_num_10', 0),
                    'market_cap': stock_info.get('market_cap', 0),
                    'warning_time': warning_time
                })
            except Exception as e:
                print(f"解析股票数据失败 {code}: {e}")
                continue
        
        parse_time = (time_module.time() - parse_start) * 1000
        total_time = (time_module.time() - start_time) * 1000
        
        print(f"数据解析耗时: {parse_time:.2f}ms")
        print(f"总耗时: {total_time:.2f}ms")
        
        # 返回性能指标
        return jsonify({
            'success': True, 
            'data': stocks,
            'performance': {
                'total_time': f"{total_time:.2f}ms",
                'network_latency': f"{network_latency:.2f}ms",
                'scan_time': f"{scan_time:.2f}ms",
                'parse_time': f"{parse_time:.2f}ms",
                'count': len(stocks)
            },
            'refresh_interval': get_redis_refresh_interval()  # 返回刷新间隔
        })
    except Exception as e:
        print(f"获取5拉升20阴少数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/5ls20ys/realtime', methods=['POST'])
def get_5ls20ys_realtime():
    """5拉升20阴少实时涨跌幅接口"""
    try:
        from flask import request
        data = request.get_json()
        codes = data.get('codes', [])
        
        if not codes:
            return jsonify({'success': True, 'data': {}})
        
        # 获取实时行情
        realtime_data = {}
        for code in codes:
            try:
                # 转换股票代码格式用于腾讯接口
                if '.' in code:
                    # 有后缀的情况
                    if code.endswith('.SH'):
                        tencent_code = f"sh{code.replace('.SH', '')}"
                    elif code.endswith('.SZ'):
                        tencent_code = f"sz{code.replace('.SZ', '')}"
                    else:
                        tencent_code = f"sz{code.split('.')[0]}"
                else:
                    # 没有后缀，根据首位判断
                    if code.startswith('6'):
                        tencent_code = f"sh{code}"
                    else:
                        tencent_code = f"sz{code}"
                
                url = f"http://qt.gtimg.cn/q={tencent_code}"
                response = requests.get(url, timeout=2)
                response.encoding = 'gbk'
                
                if response.text:
                    # 解析腾讯接口返回的数据
                    match = response.text.strip().split('~')
                    if len(match) > 3:
                        current_price = float(match[3]) if match[3] else 0
                        pre_close = float(match[4]) if match[4] else 0
                        if pre_close > 0:
                            change = ((current_price - pre_close) / pre_close) * 100
                            realtime_data[code] = {'change': round(change, 2)}
            except Exception as e:
                print(f"获取股票{code}实时行情失败: {e}")
                continue
        
        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/5ls20ys/market_cap/<code>')
def get_5ls20ys_market_cap(code):
    """获取单个股票的市值（使用Tushare）"""
    try:
        import tushare as ts
        
        # 转换股票代码格式
        if code.startswith('6'):
            ts_code = f"{code}.SH"
        elif code.startswith('0') or code.startswith('3'):
            ts_code = f"{code}.SZ"
        elif code.startswith('8') or code.startswith('4'):
            ts_code = f"{code}.BJ"
        else:
            ts_code = f"{code}.SZ"
        
        # 使用Tushare获取市值
        pro = ts.pro_api()
        df = pro.daily_basic(ts_code=ts_code, fields='ts_code,total_mv')
        
        if df is not None and not df.empty:
            # total_mv单位是万元，转换为亿元
            market_cap = float(df.iloc[0]['total_mv']) / 10000
            return jsonify({
                'success': True,
                'code': code,
                'market_cap': round(market_cap, 2)
            })
        else:
            return jsonify({'success': False, 'error': '无法获取市值数据'})
            
    except Exception as e:
        print(f"获取市值失败 {code}: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/5ls20ys/dates')
def get_5ls20ys_dates():
    """获取5拉升20阴少所有有数据的日期"""
    try:
        r = get_redis_client()
        
        # 查找所有相关的key
        keys_standard = r.keys('stocks_5ls20:*')
        keys_compact = r.keys('5ls20:*')
        
        dates = []
        
        # 处理标准格式的key: stocks_5ls20:2026-01-21
        for key in keys_standard:
            if ':' in key and not key.endswith(':timestamp'):
                date_part = key.split(':', 1)[1]
                if date_part and '-' in date_part:
                    dates.append(date_part)
        
        # 处理紧凑格式的key: 5ls20:20260121
        for key in keys_compact:
            if ':' in key and not key.endswith(':timestamp'):
                date_part = key.split(':', 1)[1]
                # 转换20260121 -> 2026-01-21
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        
        # 去重并排序
        dates = sorted(list(set(dates)), reverse=True)
        
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/5ls20ys/check_update')
def check_5ls20ys_update():
    """检查5拉升20阴少Redis数据是否更新（优化版：使用Pipeline）"""
    try:
        import time as time_module
        start_time = time_module.time()
        
        r = get_redis_client()
        today = datetime.now().strftime('%Y-%m-%d')
        today_compact = datetime.now().strftime('%Y%m%d')  # 20260122格式
        
        # 尝试两种key格式
        key1 = f"stocks_5ls20:{today}"
        key2 = f"5ls20:{today_compact}"
        timestamp_key = f"stocks_5ls20:{today}:timestamp"
        
        # 使用Pipeline批量检查两种key格式
        pipe = r.pipeline()
        pipe.get(timestamp_key)
        pipe.exists(key1)
        pipe.exists(key2)
        results = pipe.execute()
        
        timestamp = results[0]
        key1_exists = results[1]
        key2_exists = results[2]
        
        # 选择存在的key
        key = key1 if key1_exists else key2
        key_exists = key1_exists or key2_exists
        
        total_time = (time_module.time() - start_time) * 1000
        print(f"check_update耗时: {total_time:.2f}ms, key1_exists={key1_exists}, key2_exists={key2_exists}")
        
        # 如果键存在且有数据
        if key_exists:
            # 获取数据量
            if key == key2:
                # key2是string类型，需要解析JSON来获取数量
                data_str = r.get(key)
                if data_str:
                    try:
                        data = json.loads(data_str)
                        data_count = len(data) if isinstance(data, list) else 0
                    except:
                        data_count = 0
                else:
                    data_count = 0
            else:
                # key1是hash类型
                data_count = r.hlen(key)
            
            if data_count > 0:
                # 如果没有时间戳，创建一个（兼容旧数据）
                if not timestamp:
                    import time
                    timestamp = str(int(time.time() * 1000))  # 毫秒级时间戳
                    r.set(timestamp_key, timestamp)
                
                return jsonify({
                    'success': True, 
                    'updated': True,
                    'timestamp': timestamp,
                    'count': data_count,
                    'refresh_interval': get_redis_refresh_interval()  # 动态刷新间隔
                })
        
        return jsonify({
            'success': True, 
            'updated': False, 
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ========== 5拉升小于2阴页面相关API ==========
@app.route('/5拉升小于2阴')
def lsy2_page():
    """5拉升小于2阴页面"""
    return render_template('5lsy2.html')

@app.route('/api/5lsy2/stocks')
@app.route('/api/5lsy2/stocks/<date>')
def get_5lsy2_stocks(date=None):
    """获取5拉升小于2阴股票列表（兼容hash/string两种格式）"""
    try:
        import base64
        import time as time_module
        start_time = time_module.time()

        r = get_redis_client()

        if not date:
            date = request.args.get('date')
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')

        today_compact = date.replace('-', '')
        key = f"5lsy2:{today_compact}"

        ping_start = time_module.time()
        r.ping()
        network_latency = (time_module.time() - ping_start) * 1000

        if not r.exists(key):
            total_time = (time_module.time() - start_time) * 1000
            return jsonify({
                'success': True,
                'data': [],
                'performance': {
                    'total_time': f"{total_time:.2f}ms",
                    'network_latency': f"{network_latency:.2f}ms",
                    'count': 0
                },
                'message': f'没有找到数据key: {key}'
            })

        key_type = r.type(key)
        print(f"5lsy2 key={key}, type={key_type}")

        all_items = []
        if key_type == 'hash':
            # Hash类型：使用HSCAN逐批次获取（与5ls20ys相同格式）
            cursor = 0
            while True:
                cursor, batch = r.hscan(key, cursor, count=100)
                all_items.extend(batch.items())
                if cursor == 0:
                    break
        elif key_type == 'string':
            data_str = r.get(key)
            if data_str:
                try:
                    data_list = json.loads(data_str)
                    for item in data_list:
                        code = item.get('code', '')
                        all_items.append((code, json.dumps(item, ensure_ascii=False)))
                except Exception as e:
                    print(f"解析5lsy2 string数据失败: {e}")

        stocks = []
        for code, data_str in all_items:
            try:
                # 尝试Base64解码，失败则直接解析JSON
                try:
                    decoded = base64.b64decode(data_str).decode('utf-8')
                    stock_info = json.loads(decoded)
                except Exception:
                    stock_info = json.loads(data_str)

                code_str = code.decode('utf-8') if isinstance(code, bytes) else code
                stocks.append({
                    'code': stock_info.get('code', code_str),
                    'name': stock_info.get('name', ''),
                    'today_change': stock_info.get('today_change', stock_info.get('current_gain', 0)),
                    'pull_count_10': stock_info.get('pull_count_10', stock_info.get('ls_num', 0)),
                    'yin_count_10': stock_info.get('yin_count_10', 0),
                    'market_cap': stock_info.get('market_cap', 0),
                    'warning_time': stock_info.get('warning_time', stock_info.get('time', ''))
                })
            except Exception as e:
                print(f"解析5lsy2股票数据失败 {code}: {e}")
                continue

        total_time = (time_module.time() - start_time) * 1000
        print(f"5lsy2 获取到 {len(stocks)} 条记录，耗时 {total_time:.2f}ms")
        return jsonify({
            'success': True,
            'data': stocks,
            'performance': {
                'total_time': f"{total_time:.2f}ms",
                'network_latency': f"{network_latency:.2f}ms",
                'count': len(stocks)
            },
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        print(f"获取5拉升小于2阴数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/5lsy2/realtime', methods=['POST'])
def get_5lsy2_realtime():
    """5拉升小于2阴实时涨跌幅接口"""
    try:
        data = request.get_json()
        codes = data.get('codes', [])

        if not codes:
            return jsonify({'success': True, 'data': {}})

        realtime_data = {}
        for code in codes:
            try:
                if code.startswith('6'):
                    tencent_code = f"sh{code}"
                else:
                    tencent_code = f"sz{code}"

                url = f"http://qt.gtimg.cn/q={tencent_code}"
                response = requests.get(url, timeout=2)
                response.encoding = 'gbk'

                if response.text:
                    match = response.text.strip().split('~')
                    if len(match) > 3:
                        current_price = float(match[3]) if match[3] else 0
                        pre_close = float(match[4]) if match[4] else 0
                        if pre_close > 0:
                            change = ((current_price - pre_close) / pre_close) * 100
                            realtime_data[code] = {'change': round(change, 2)}
            except Exception as e:
                print(f"获取股票{code}实时行情失败: {e}")
                continue

        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/5lsy2/market_cap/<code>')
def get_5lsy2_market_cap(code):
    """获取单个股票的市值"""
    try:
        import tushare as ts

        if code.startswith('6'):
            ts_code = f"{code}.SH"
        elif code.startswith('0') or code.startswith('3'):
            ts_code = f"{code}.SZ"
        elif code.startswith('8') or code.startswith('4'):
            ts_code = f"{code}.BJ"
        else:
            ts_code = f"{code}.SZ"

        pro = ts.pro_api()
        df = pro.daily_basic(ts_code=ts_code, fields='ts_code,total_mv')

        if df is not None and not df.empty:
            market_cap = float(df.iloc[0]['total_mv']) / 10000
            return jsonify({'success': True, 'code': code, 'market_cap': round(market_cap, 2)})
        else:
            return jsonify({'success': False, 'error': '无法获取市值数据'})
    except Exception as e:
        print(f"获取市值失败 {code}: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/5lsy2/dates')
def get_5lsy2_dates():
    """获取5拉升小于2阴所有有数据的日期"""
    try:
        r = get_redis_client()
        keys = r.keys('5lsy2:*')
        dates = []
        for key in keys:
            key_str = key if isinstance(key, str) else key.decode('utf-8')
            if ':' in key_str:
                date_part = key_str.split(':', 1)[1]
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        dates = sorted(list(set(dates)), reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/5lsy2/check_update')
def check_5lsy2_update():
    """检查5拉升小于2阴Redis数据是否更新"""
    try:
        import time as time_module
        r = get_redis_client()
        today_compact = datetime.now().strftime('%Y%m%d')
        key = f"5lsy2:{today_compact}"
        timestamp_key = f"5lsy2:{today_compact}:timestamp"

        pipe = r.pipeline()
        pipe.get(timestamp_key)
        pipe.exists(key)
        results = pipe.execute()
        timestamp = results[0]
        key_exists = results[1]

        if key_exists:
            key_type = r.type(key)
            if key_type == 'hash':
                data_count = r.hlen(key)
            elif key_type == 'string':
                data_str = r.get(key)
                data_count = 0
                if data_str:
                    try:
                        data = json.loads(data_str)
                        data_count = len(data) if isinstance(data, list) else 0
                    except:
                        pass
            else:
                data_count = 0

            if data_count > 0:
                if not timestamp:
                    import time
                    timestamp = str(int(time.time() * 1000))
                    r.set(timestamp_key, timestamp)

                return jsonify({
                    'success': True,
                    'updated': True,
                    'timestamp': timestamp,
                    'count': data_count,
                    'refresh_interval': get_redis_refresh_interval()
                })

        return jsonify({
            'success': True,
            'updated': False,
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ========== 连续涨停页面相关API ==========
@app.route('/连续涨停')
def lszty_page():
    """连续涨停页面"""
    return render_template('lszty.html')


@app.route('/api/lszty/dates')
def get_lszty_dates():
    """获取连续涨停所有有数据的日期"""
    try:
        r = get_redis_client()
        keys = r.keys('lszty:*')
        dates = []
        for key in keys:
            if ':' in key and not key.endswith(':timestamp'):
                date_part = key.split(':', 1)[1]
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        dates = sorted(set(dates), reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/lszty/stocks')
@app.route('/api/lszty/stocks/<date>')
def get_lszty_stocks(date=None):
    """获取连续涨停股票列表"""
    try:
        import base64
        import time as time_module
        from flask import request

        start_time = time_module.time()
        r = get_redis_client()

        if not date:
            date = request.args.get('date')
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')

        today_compact = date.replace('-', '')
        key = f"lszty:{today_compact}"

        key_type = r.type(key) if r.exists(key) else None
        if not key_type:
            return jsonify({
                'success': True, 'data': [],
                'message': f'没有找到数据key: {key}'
            })

        all_keys = []
        if key_type == 'hash':
            cursor = 0
            while True:
                cursor, batch = r.hscan(key, cursor, count=100)
                all_keys.extend(batch.items())
                if cursor == 0:
                    break
        elif key_type == 'string':
            data_str = r.get(key)
            if data_str:
                try:
                    data_list = json.loads(data_str)
                    for item in data_list:
                        code = item.get('code', '')
                        all_keys.append((code, json.dumps(item, ensure_ascii=False)))
                except:
                    pass

        stocks = []
        scan_info = None
        for code, data_str in all_keys:
            try:
                if isinstance(code, bytes):
                    code = code.decode('utf-8', errors='ignore')
                if code in ('_scan_info', '_timestamp'):
                    if code == '_scan_info':
                        scan_info = _yhy_normalize_scan_info(_yhy_decode_json_payload(data_str))
                    continue
                try:
                    decoded = base64.b64decode(data_str).decode('utf-8')
                    info = json.loads(decoded)
                except:
                    info = json.loads(data_str)

                stocks.append({
                    'code': info.get('code', code),
                    'name': info.get('name', ''),
                    'stock_code_with_market': info.get('stock_code_with_market', ''),
                    'group': info.get('group', ''),
                    'board_type': info.get('board_type', ''),
                    'limit_ratio': info.get('limit_ratio', 10.0),
                    'ref_gain': info.get('ref_gain', 0),
                    'consecutive_count': info.get('consecutive_count', 0),
                    'prev_limit_date': info.get('prev_limit_date', ''),
                    'n_days': info.get('n_days', 0),
                    'yin_count_between': info.get('yin_count_between', 0),
                    'jipink_count': info.get('jipink_count', 0),
                    'jipink_ratio': info.get('jipink_ratio', 0),
                    'all_yang': info.get('all_yang', False),
                    'all_yin_up': info.get('all_yin_up', False),
                    'all_yin_strong': info.get('all_yin_strong', False),
                    'high_gain_count': int(info.get('high_gain_count', 0) or 0),
                    'high_gain_days_gte5': bool(info.get('high_gain_days_gte5', False)),
                    'all_yin_pull_prev_or_next': bool(info.get('all_yin_pull_prev_or_next', False)),
                })
            except Exception as e:
                print(f"解析lszty股票数据失败 {code}: {e}")
                continue

        total_time = (time_module.time() - start_time) * 1000
        return jsonify({
            'success': True,
            'data': stocks,
            'scan_info': scan_info,
            'performance': {
                'total_time': f"{total_time:.2f}ms",
                'count': len(stocks)
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/lszty/check_update')
def check_lszty_update():
    """检查连续涨停数据是否更新"""
    try:
        r = get_redis_client()
        today_compact = datetime.now().strftime('%Y%m%d')
        key = f"lszty:{today_compact}"
        if r.exists(key):
            count = r.hlen(key) if r.type(key) == 'hash' else 0
            ts_key = f"lszty:{today_compact}:timestamp"
            timestamp = r.get(ts_key) if r.exists(ts_key) else str(count)
            return jsonify({
                'success': True, 'updated': True,
                'timestamp': timestamp, 'count': count,
                'refresh_interval': get_redis_refresh_interval()
            })
        return jsonify({
            'success': True, 'updated': False,
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ========== 涨停前连续强页面相关API ==========
@app.route('/涨停前连续强')
def ztqq_page():
    """涨停前连续强页面"""
    return render_template('ztqq.html')


@app.route('/api/ztqq/dates')
def get_ztqq_dates():
    """获取涨停前连续强所有有数据的日期"""
    try:
        r = get_redis_client()
        keys = r.keys('ztqq:*')
        dates = []
        for key in keys:
            if ':' in key and not key.endswith(':timestamp'):
                date_part = key.split(':', 1)[1]
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        dates = sorted(set(dates), reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/ztqq/stocks')
@app.route('/api/ztqq/stocks/<date>')
def get_ztqq_stocks(date=None):
    """获取涨停前连续强股票列表"""
    try:
        import base64
        import time as time_module
        from flask import request

        start_time = time_module.time()
        r = get_redis_client()

        if not date:
            date = request.args.get('date')
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')

        today_compact = date.replace('-', '')
        key = f"ztqq:{today_compact}"

        key_type = r.type(key) if r.exists(key) else None
        if not key_type:
            return jsonify({
                'success': True, 'data': [],
                'message': f'没有找到数据key: {key}'
            })

        all_keys = []
        if key_type == 'hash':
            cursor = 0
            while True:
                cursor, batch = r.hscan(key, cursor, count=100)
                all_keys.extend(batch.items())
                if cursor == 0:
                    break

        stocks = []
        for code, data_str in all_keys:
            try:
                try:
                    decoded = base64.b64decode(data_str).decode('utf-8')
                    info = json.loads(decoded)
                except Exception:
                    info = json.loads(data_str)

                stocks.append({
                    'code': info.get('code', code if isinstance(code, str) else code.decode('utf-8')),
                    'name': info.get('name', ''),
                    'stock_code_with_market': info.get('stock_code_with_market', ''),
                    'board_type': info.get('board_type', ''),
                    'limit_ratio': info.get('limit_ratio', 10.0),
                    'ref_gain': info.get('ref_gain', 0),
                    'n_strong_days': info.get('n_strong_days', 0),
                    'strong_details': info.get('strong_details', []),
                })
            except Exception as e:
                print(f"解析ztqq股票数据失败 {code}: {e}")
                continue

        total_time = (time_module.time() - start_time) * 1000
        return jsonify({
            'success': True, 'data': stocks,
            'count': len(stocks), 'date': date,
            'performance': {'total_ms': round(total_time, 2)}
        })
    except Exception as e:
        print(f"获取ztqq数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/ztqq/check_update')
def check_ztqq_update():
    """检查涨停前连续强数据是否更新"""
    try:
        r = get_redis_client()
        today_compact = datetime.now().strftime('%Y%m%d')
        key = f"ztqq:{today_compact}"
        if r.exists(key):
            count = r.hlen(key) if r.type(key) == 'hash' else 0
            ts_key = f"ztqq:{today_compact}:timestamp"
            timestamp = r.get(ts_key) if r.exists(ts_key) else str(count)
            return jsonify({
                'success': True, 'updated': True,
                'timestamp': timestamp, 'count': count,
                'refresh_interval': get_redis_refresh_interval()
            })
        return jsonify({
            'success': True, 'updated': False,
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ========== 首板涨停页面相关API ==========
@app.route('/首板涨停')
def sbzt_page():
    """首板涨停页面"""
    return render_template('sbzt.html')


@app.route('/api/sbzt/dates')
def get_sbzt_dates():
    """获取首板涨停所有有数据的日期"""
    try:
        r = get_redis_client()
        keys = r.keys('sbzt:*')
        dates = []
        for key in keys:
            if ':' in key and not key.endswith(':timestamp'):
                date_part = key.split(':', 1)[1]
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        dates = sorted(set(dates), reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/sbzt/stocks')
@app.route('/api/sbzt/stocks/<date>')
def get_sbzt_stocks(date=None):
    """获取首板涨停股票列表"""
    try:
        import base64
        import time as time_module
        from flask import request

        start_time = time_module.time()
        r = get_redis_client()

        if not date:
            date = request.args.get('date')
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')

        today_compact = date.replace('-', '')
        key = f"sbzt:{today_compact}"

        key_type = r.type(key) if r.exists(key) else None
        if not key_type:
            return jsonify({
                'success': True, 'data': [],
                'message': f'没有找到数据key: {key}'
            })

        all_items = []
        if key_type == 'hash':
            cursor = 0
            while True:
                cursor, batch = r.hscan(key, cursor, count=100)
                all_items.extend(batch.items())
                if cursor == 0:
                    break

        stocks = []
        scan_info = None
        for code, data_str in all_items:
            try:
                if isinstance(code, bytes):
                    code = code.decode('utf-8', errors='ignore')
                if code in ('_scan_info', '_timestamp'):
                    if code == '_scan_info':
                        scan_info = _yhy_normalize_scan_info(_yhy_decode_json_payload(data_str))
                    continue
                try:
                    decoded = base64.b64decode(data_str).decode('utf-8')
                    info = json.loads(decoded)
                except Exception:
                    info = json.loads(data_str)

                stocks.append({
                    'code': info.get('code', code),
                    'name': info.get('name', ''),
                    'stock_code_with_market': info.get('stock_code_with_market', ''),
                    'board_type': info.get('board_type', ''),
                    'limit_ratio': info.get('limit_ratio', 10.0),
                    'ref_gain': info.get('ref_gain', 0),
                    'max_drawdown': info.get('max_drawdown', 0),
                    'yin_count': info.get('yin_count', 0),
                    'max_drop': info.get('max_drop', 0),
                    'max_yin_body': info.get('max_yin_body', 0),
                    'sum_yin_body': info.get('sum_yin_body', 0),
                    'max_above_ma5_days': info.get('max_above_ma5_days', 0),
                    'nearly_bare_foot_count': info.get('nearly_bare_foot_count', 0),
                    'pull_up_count': info.get('pull_up_count', 0),
                })
            except Exception as e:
                print(f"解析sbzt股票数据失败 {code}: {e}")
                continue

        total_time = (time_module.time() - start_time) * 1000
        return jsonify({
            'success': True, 'data': stocks,
            'count': len(stocks), 'date': date,
            'scan_info': scan_info,
            'performance': {'total_ms': round(total_time, 2)}
        })
    except Exception as e:
        print(f"获取sbzt数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/sbzt/check_update')
def check_sbzt_update():
    """检查首板涨停数据是否更新"""
    try:
        r = get_redis_client()
        today_compact = datetime.now().strftime('%Y%m%d')
        key = f"sbzt:{today_compact}"
        if r.exists(key):
            count = r.hlen(key) if r.type(key) == 'hash' else 0
            ts_key = f"sbzt:{today_compact}:timestamp"
            timestamp = r.get(ts_key) if r.exists(ts_key) else str(count)
            return jsonify({
                'success': True, 'updated': True,
                'timestamp': timestamp, 'count': count,
                'refresh_interval': get_redis_refresh_interval()
            })
        return jsonify({
            'success': True, 'updated': False,
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})



# ========== 连续5不碰页面相关API ==========
@app.route('/连续5不碰')
def lx5bp_page():
    """连续5不碰页面"""
    return render_template('lx5bp.html')


@app.route('/api/lx5bp/dates')
def get_lx5bp_dates():
    """获取连续5不碰所有有数据的日期"""
    try:
        r = get_redis_client()
        keys = r.keys('lx5bp:*')
        dates = []
        for key in keys:
            if isinstance(key, bytes):
                key = key.decode()
            if ':' in key and not key.endswith(':timestamp'):
                date_part = key.split(':', 1)[1]
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        dates = sorted(set(dates), reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/lx5bp/stocks')
@app.route('/api/lx5bp/stocks/<date>')
def get_lx5bp_stocks(date=None):
    """获取连续5不碰股票列表"""
    try:
        import base64
        import time as time_module
        from flask import request

        start_time = time_module.time()
        r = get_redis_client()

        if not date:
            date = request.args.get('date')
        if date:
            date_compact = date.replace('-', '')
        else:
            keys = r.keys('lx5bp:*')
            date_keys = []
            for key_item in keys:
                if isinstance(key_item, bytes):
                    key_item = key_item.decode()
                if ':' in key_item and not key_item.endswith(':timestamp'):
                    date_part = key_item.split(':', 1)[1]
                    if date_part and len(date_part) == 8 and date_part.isdigit():
                        date_keys.append(date_part)
            if not date_keys:
                return jsonify({'success': True, 'data': [], 'message': '没有找到数据 key: lx5bp:*'})
            date_compact = sorted(set(date_keys), reverse=True)[0]
            date = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}"

        key = f"lx5bp:{date_compact}"
        key_type = r.type(key) if r.exists(key) else None
        if not key_type:
            return jsonify({'success': True, 'data': [], 'message': f'没有找到数据key: {key}'})

        all_items = []
        if key_type in ('hash', b'hash'):
            cursor = 0
            while True:
                cursor, batch = r.hscan(key, cursor, count=100)
                all_items.extend(batch.items())
                if cursor == 0:
                    break
        elif key_type in ('string', b'string'):
            raw = r.get(key)
            if raw:
                all_items.append((date_compact, raw))

        stocks = []
        scan_info = None
        for code, data_str in all_items:
            try:
                if isinstance(code, bytes):
                    code = code.decode('utf-8', errors='ignore')
                if code in ('_scan_info', '_timestamp'):
                    if code == '_scan_info':
                        scan_info = _yhy_normalize_scan_info(_yhy_decode_json_payload(data_str))
                    continue

                try:
                    decoded = base64.b64decode(data_str).decode('utf-8')
                    info = json.loads(decoded)
                except Exception:
                    info = json.loads(data_str)

                if isinstance(info, list):
                    iter_items = info
                else:
                    iter_items = [info]

                for item in iter_items:
                    if not isinstance(item, dict):
                        continue
                    stocks.append({
                        'code': str(item.get('code', code)),
                        'name': str(item.get('name', '')),
                        'stock_code_with_market': str(item.get('stock_code_with_market', '')),
                        'board_type': str(item.get('board_type', '')),
                        'limit_ratio': float(item.get('limit_ratio', 0) or 0),
                        'ref_gain': float(item.get('ref_gain', 0) or 0),
                        'ref_high_gain': float(item.get('ref_high_gain', 0) or 0),
                        'ref_is_limit_up': int(item.get('ref_is_limit_up', 0) or 0),
                        'ref_high_gain_gte_6_5': int(item.get('ref_high_gain_gte_6_5', 0) or 0),
                        'ref_high_gain_gte_6_5_and_yang': int(item.get('ref_high_gain_gte_6_5_and_yang', 0) or 0),
                        'current_streak': int(item.get('current_streak', 0) or 0),
                        'max_streak_10d': int(item.get('max_streak_10d', 0) or 0),
                        'lower_shadow_sum': round(float(item.get('lower_shadow_sum', 0) or 0), 4),
                        'three_day_lower_shadow_sum': round(float(item.get('three_day_lower_shadow_sum', item.get('lower_shadow_sum', 0)) or 0), 4),
                        'three_day_high_gain_count': int(item.get('three_day_high_gain_count', 0) or 0),
                        'three_day_strong_k_count': int(item.get('three_day_strong_k_count', 0) or 0),
                        'three_day_near_barefoot_yang_count': int(item.get('three_day_near_barefoot_yang_count', 0) or 0),
                        'yang_body_sum': round(float(item.get('yang_body_sum', 0) or 0), 2),
                        'yang_high_gain_count': int(item.get('yang_high_gain_count', 0) or 0),
                        'n_param': int(item.get('n_param', 0) or 0),
                    })
            except Exception as e:
                print(f"解析lx5bp股票数据失败 {code}: {e}")
                continue

        total_time = (time_module.time() - start_time) * 1000
        return jsonify({
            'success': True,
            'data': stocks,
            'scan_info': scan_info,
            'count': len(stocks),
            'date': date,
            'performance': {'total_ms': round(total_time, 2)}
        })
    except Exception as e:
        print(f"获取lx5bp数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/lx5bp/check_update')
def check_lx5bp_update():
    """检查连续5不碰数据是否更新"""
    try:
        r = get_redis_client()
        today_compact = datetime.now().strftime('%Y%m%d')
        key = f"lx5bp:{today_compact}"
        if r.exists(key):
            count = r.hlen(key) if r.type(key) in ('hash', b'hash') else 0
            ts_key = f"lx5bp:{today_compact}:timestamp"
            timestamp = r.get(ts_key) if r.exists(ts_key) else str(count)
            return jsonify({
                'success': True,
                'updated': True,
                'timestamp': timestamp,
                'count': count,
                'refresh_interval': get_redis_refresh_interval()
            })
        return jsonify({
            'success': True,
            'updated': False,
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
# ========== 涨停或光脚页面相关API ==========
@app.route('/涨停或光脚')
def ztorgj_page():
    """涨停或光脚页面"""
    response = make_response(render_template('ztorgj.html'))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.route('/涨停或光脚/弱阴线K线')
def ztorgj_weak20_kline_page():
    """20日弱阴线首列股票 K 线浏览页"""
    date_compact = _ztorgj_parse_export_date(request.args.get('date'))
    new_high_only = '1' if str(request.args.get('new_high_only') or '0').strip() == '1' else '0'
    max_weak_yin_20d = str(request.args.get('max_weak_yin_20d') or '').strip()
    if max_weak_yin_20d and not re.match(r'^\d+$', max_weak_yin_20d):
        max_weak_yin_20d = ''
    order_token = re.sub(r'[^0-9A-Za-z_:-]', '', str(request.args.get('order_token') or '').strip())[:120]
    response = make_response(render_template(
        'ztorgj_weak20_kline.html',
        date=_ztorgj_format_date_text(date_compact),
        new_high_only=new_high_only,
        max_weak_yin_20d=max_weak_yin_20d,
        order_token=order_token,
    ))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response



def _ztorgj_kline_screening_state_key(date=None):
    date_text = str(date or datetime.now().strftime('%Y-%m-%d')).strip().replace('-', '')
    if not re.match(r'^\d{8}$', date_text):
        date_text = datetime.now().strftime('%Y%m%d')
    return f'myStock:klineScreening:{date_text}'


@app.route('/api/ztorgj/kline-screening/state', methods=['GET', 'POST', 'DELETE'])
def manage_ztorgj_kline_screening_state():
    """保存/读取 K线筛选工作台进度，便于刷新页面后续筛。"""
    try:
        payload = request.get_json(silent=True) or {}
        date = payload.get('date') if request.method == 'POST' else request.args.get('date')
        target_key = _ztorgj_kline_screening_state_key(date)
        r = get_redis_client()

        if request.method == 'GET':
            raw_value = r.get(target_key)
            state = _yhy_decode_json_payload(raw_value) if raw_value else None
            return jsonify({
                'success': True,
                'exists': isinstance(state, dict),
                'data': state if isinstance(state, dict) else {},
                'key': target_key,
            })

        if request.method == 'DELETE':
            r.delete(target_key)
            return jsonify({'success': True, 'key': target_key, 'message': '已重新开始'})

        state = payload.get('state') or {}
        if not isinstance(state, dict):
            return jsonify({'success': False, 'error': 'state 必须是对象'})
        state['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        r.set(target_key, _yhy_encode_json_payload(state))
        try:
            r.expire(target_key, 30 * 24 * 3600)
        except Exception:
            pass
        return jsonify({'success': True, 'data': state, 'key': target_key})
    except Exception as e:
        print(f'[ztorgj][kline-screening] 状态处理失败: {e}')
        return jsonify({'success': False, 'error': str(e)})


def _ztorgj_normalize_ts_code(code):
    code = str(code or '').strip().upper()
    if not code:
        return ''
    if '.' in code:
        return code
    if code.startswith('6'):
        return f'{code}.SH'
    if code.startswith(('0', '3')):
        return f'{code}.SZ'
    return f'{code}.BJ'


def _ztorgj_decode_stock_payload(data_str):
    import base64
    if isinstance(data_str, bytes):
        data_str = data_str.decode('utf-8', errors='ignore')
    try:
        decoded = base64.b64decode(data_str).decode('utf-8')
        return json.loads(decoded)
    except Exception:
        return json.loads(data_str)


def _ztorgj_load_hash_payloads(r, key_name):
    key_type = r.type(key_name) if r.exists(key_name) else None
    if isinstance(key_type, bytes):
        key_type = key_type.decode('utf-8', errors='ignore')
    if key_type != 'hash':
        return []
    rows = []
    cursor = 0
    while True:
        cursor, batch = r.hscan(key_name, cursor, count=100)
        for code, data_str in batch.items():
            if isinstance(code, bytes):
                code = code.decode('utf-8', errors='ignore')
            if code in ('_scan_info', '_timestamp', '_snapshot_info'):
                continue
            rows.append((code, _ztorgj_decode_stock_payload(data_str)))
        if cursor == 0:
            break
    return rows


def _special5_merge_source_rows(special5_rows, fourabove5_rows):
    """按股票代码合并特别关注5和连续4日不碰MA5的数据。"""
    merged = {}

    def merge_rows(rows, membership_field):
        for redis_code, raw_info in rows or []:
            info = dict(raw_info or {})
            code = _ztorgj_stock_simple_code(info.get('code') or redis_code)
            if not code:
                continue
            record = merged.setdefault(code, {
                'code': code,
                'redis_code': redis_code,
                'info': {},
                'special5_member': 0,
                'fourabove5_member': 0,
            })
            record['info'].update(info)
            record[membership_field] = 1

    merge_rows(special5_rows, 'special5_member')
    merge_rows(fourabove5_rows, 'fourabove5_member')
    return list(merged.values())


def _ztorgj_truthy_flag(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'y')


def _ztorgj_render_candlestick_png(rows):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    if not rows:
        raise ValueError('空K线数据')

    fig, ax = plt.subplots(figsize=(5.8, 2.6), dpi=120)
    ax.set_facecolor('white')
    fig.patch.set_facecolor('white')

    xs = list(range(len(rows)))
    width = 0.62
    for idx, row in enumerate(rows):
        open_price = float(row.get('open', 0) or 0)
        close_price = float(row.get('close', 0) or 0)
        high_price = float(row.get('high', 0) or 0)
        low_price = float(row.get('low', 0) or 0)
        up = close_price >= open_price
        color = '#d91e18' if up else '#149b55'
        body_bottom = min(open_price, close_price)
        body_height = abs(close_price - open_price)
        if body_height == 0:
            body_height = 0.01
        ax.vlines(idx, low_price, high_price, color=color, linewidth=0.9)
        rect = Rectangle((idx - width / 2, body_bottom), width, body_height, facecolor='white' if up else color, edgecolor=color, linewidth=0.9)
        ax.add_patch(rect)

    ax.set_xlim(-0.8, len(rows) - 0.2)
    price_values = [float(row.get(k, 0) or 0) for row in rows for k in ('high', 'low')]
    min_price = min(price_values)
    max_price = max(price_values)
    pad = max((max_price - min_price) * 0.08, 0.05)
    ax.set_ylim(min_price - pad, max_price + pad)
    ax.grid(True, color='#e5e7eb', linewidth=0.5)
    ax.tick_params(axis='both', labelsize=7, colors='#374151', length=2)
    label_indexes = sorted(set([0, len(rows) // 2, len(rows) - 1]))
    ax.set_xticks(label_indexes)
    ax.set_xticklabels([str(rows[i].get('trade_date', ''))[4:] for i in label_indexes])
    for spine in ax.spines.values():
        spine.set_color('#d1d5db')
        spine.set_linewidth(0.6)

    output = io.BytesIO()
    fig.tight_layout(pad=0.35)
    fig.savefig(output, format='png')
    plt.close(fig)
    output.seek(0)
    return output


def _ztorgj_parse_export_date(date_value):
    date_compact = str(date_value or datetime.now().strftime('%Y-%m-%d')).strip().replace('-', '')
    if len(date_compact) != 8 or not date_compact.isdigit():
        return datetime.now().strftime('%Y%m%d')
    return date_compact


def _ztorgj_format_date_text(date_compact):
    date_text = str(date_compact or '').strip().replace('-', '')
    if len(date_text) == 8 and date_text.isdigit():
        return f'{date_text[:4]}-{date_text[4:6]}-{date_text[6:8]}'
    return str(date_compact or '')


def _ztorgj_stock_simple_code(value):
    text = str(value or '').strip()
    if '.' in text:
        text = text.split('.', 1)[0]
    return text


def _ztorgj_qmt_kline_key(date_compact):
    return f'qmt:kline:{date_compact}'


def _ztorgj_api_history_kline_key(date_compact):
    return f'qmt:kline:api:history:{date_compact}'


def _ztorgj_api_realtime_kline_key(date_compact):
    return f'qmt:kline:api:realtime:{date_compact}'


def _pz_history_kline_key(date_compact):
    return f'pz:kline:history:{date_compact}'


def _pz_realtime_kline_key(date_compact):
    return f'pz:kline:realtime:{date_compact}'


def _qy1_history_kline_key(date_compact):
    return f'qy1:kline:history:{date_compact}'


def _qy1_realtime_kline_key(date_compact):
    return f'qy1:kline:realtime:{date_compact}'


def _pz_available_dates(redis_client):
    dates = set()
    for raw_key in redis_client.keys('pz:*'):
        key_text = raw_key.decode('utf-8', errors='ignore') if isinstance(raw_key, bytes) else str(raw_key)
        matched = re.match(r'^pz:(\d{8})$', key_text)
        if not matched:
            continue
        try:
            parsed = datetime.strptime(matched.group(1), '%Y%m%d')
        except ValueError:
            continue
        dates.add(parsed.strftime('%Y-%m-%d'))
    return sorted(dates, reverse=True)


def _pz1_available_dates(redis_client):
    dates = set()
    for raw_key in redis_client.keys('pz1:*'):
        key_text = raw_key.decode('utf-8', errors='ignore') if isinstance(raw_key, bytes) else str(raw_key)
        matched = re.match(r'^pz1:(\d{8})$', key_text)
        if not matched:
            continue
        try:
            parsed = datetime.strptime(matched.group(1), '%Y%m%d')
        except ValueError:
            continue
        dates.add(parsed.strftime('%Y-%m-%d'))
    return sorted(dates, reverse=True)


def _pz_page2_available_dates(redis_client):
    return sorted(set(_pz_available_dates(redis_client))
                  | set(_pz1_available_dates(redis_client)), reverse=True)


def _qy1_available_dates(redis_client):
    dates = set()
    for raw_key in redis_client.keys('qy1:*'):
        key_text = raw_key.decode('utf-8', errors='ignore') if isinstance(raw_key, bytes) else str(raw_key)
        matched = re.match(r'^qy1:(\d{8})$', key_text)
        if not matched:
            continue
        try:
            parsed = datetime.strptime(matched.group(1), '%Y%m%d')
        except ValueError:
            continue
        dates.add(parsed.strftime('%Y-%m-%d'))
    return sorted(dates, reverse=True)


def _qy2_available_dates(redis_client):
    dates = set()
    for raw_key in redis_client.keys('qy2:*'):
        key_text = raw_key.decode('utf-8', errors='ignore') if isinstance(raw_key, bytes) else str(raw_key)
        matched = re.match(r'^qy2:(\d{8})$', key_text)
        if not matched:
            continue
        try:
            parsed = datetime.strptime(matched.group(1), '%Y%m%d')
        except ValueError:
            continue
        dates.add(parsed.strftime('%Y-%m-%d'))
    return sorted(dates, reverse=True)


def _pz_push_log_available_dates(redis_client):
    dates = set()
    for raw_key in redis_client.keys('dingtalk:pz:records:*'):
        key_text = raw_key.decode('utf-8', errors='ignore') if isinstance(raw_key, bytes) else str(raw_key)
        matched = re.match(r'^dingtalk:pz:records:(\d{8})$', key_text)
        if not matched:
            continue
        try:
            parsed = datetime.strptime(matched.group(1), '%Y%m%d')
        except ValueError:
            continue
        dates.add(parsed.strftime('%Y-%m-%d'))
    return sorted(dates, reverse=True)


def _pz_performance_available_dates(redis_client):
    dates = set()
    for raw_key in redis_client.keys('pz:performance:*'):
        key_text = raw_key.decode('utf-8', errors='ignore') if isinstance(raw_key, bytes) else str(raw_key)
        matched = re.match(r'^pz:performance:(\d{8})$', key_text)
        if not matched:
            continue
        try:
            parsed = datetime.strptime(matched.group(1), '%Y%m%d')
        except ValueError:
            continue
        dates.add(parsed.strftime('%Y-%m-%d'))
    return sorted(dates, reverse=True)


def _pz_push_reason_labels(record):
    reasons = []
    if _ztorgj_truthy_flag(record.get('five_pull_all_yin_up')):
        reasons.append('5拉阴线都向上')
    if _ztorgj_truthy_flag(record.get('five_pull_all_yin_eaten_next')):
        reasons.append('5拉阴被吃')
    yin_pull_value = record.get('five_pull_yin_pull_next')
    if yin_pull_value is None:
        yin_pull_value = record.get('five_pull_yin_pull_next_or_eaten')
    if _ztorgj_truthy_flag(yin_pull_value):
        reasons.append('5拉阴后拉或吃')
    if _ztorgj_truthy_flag(record.get('five_pull_all_yin_cross_star')):
        reasons.append('5拉阴线都是十字星')
    if _ztorgj_truthy_flag(record.get('is_quanyang_10d')):
        reasons.append('全阳')
    yin_count_20 = _ztorgj_parse_int(record.get('yin_count_20'), None)
    if yin_count_20 is not None and yin_count_20 <= 4:
        reasons.append(f'20天阴线等于{yin_count_20}')
    return reasons


def _ztorgj_format_kline_rows(rows, code, days, end_date):
    formatted = []
    for raw in rows or []:
        trade_date = str(raw.get('trade_date') or raw.get('date') or '').strip().replace('-', '')
        if not trade_date or trade_date > end_date:
            continue
        pre_close = _ztorgj_parse_float(raw.get('pre_close'), 0.0)
        close = _ztorgj_parse_float(raw.get('close'), 0.0)
        pct_chg = raw.get('pct_chg')
        if pct_chg is None or pct_chg == '':
            pct_chg = round((close - pre_close) / pre_close * 100, 4) if pre_close > 0 else 0.0
        formatted.append({
            'ts_code': str(raw.get('ts_code') or raw.get('stock_code_with_market') or code),
            'trade_date': trade_date,
            'date_label': raw.get('date_label') or (f'{trade_date[4:6]}/{trade_date[6:8]}' if len(trade_date) == 8 else trade_date),
            'open': _ztorgj_parse_float(raw.get('open'), 0.0),
            'high': _ztorgj_parse_float(raw.get('high'), 0.0),
            'low': _ztorgj_parse_float(raw.get('low'), 0.0),
            'close': close,
            'pre_close': pre_close,
            'pct_chg': _ztorgj_parse_float(pct_chg, 0.0),
            'vol': _ztorgj_parse_float(raw.get('vol'), 0.0),
            'amount': _ztorgj_parse_float(raw.get('amount'), 0.0),
        })
    formatted.sort(key=lambda item: item.get('trade_date') or '')
    return formatted[-days:]


def _ztorgj_public_ohlc_rows(rows):
    """Return strictly ordered OHLC rows for external K-line consumers."""
    by_date = {}
    for raw in rows or []:
        trade_date = str(raw.get('trade_date') or '').strip()
        if not re.match(r'^\d{8}$', trade_date):
            continue
        try:
            datetime.strptime(trade_date, '%Y%m%d')
            open_price = float(raw.get('open'))
            high_price = float(raw.get('high'))
            low_price = float(raw.get('low'))
            close_price = float(raw.get('close'))
        except (TypeError, ValueError):
            continue
        if min(open_price, high_price, low_price, close_price) <= 0:
            continue
        if low_price > min(open_price, close_price) or max(open_price, close_price) > high_price:
            continue
        by_date[trade_date] = {
            'trade_date': trade_date,
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
        }
    return [by_date[trade_date] for trade_date in sorted(by_date)]


_KLINE_API_AES_AAD = b'10above5-klines:v1'
_YXCS_KLINE_API_AES_AAD = b'yxcs-klines:v1'
_SPECIAL5_KLINE_API_AES_AAD = b'special5-klines:v1'
_5LSABOVE10_KLINE_API_AES_AAD = b'5lsabove10-klines:v1'
_THS_KLINE_API_AES_AAD = b'ths-klines:v1'
_20XIASHA_KLINE_API_AES_AAD = b'20xiasha-klines:v1'


def _encrypt_kline_api_payload(payload, aad):
    raw_key = str(os.environ.get('KLINE_API_AES_KEY_B64') or '').strip()
    try:
        key = base64.b64decode(raw_key, validate=True)
    except Exception as exc:
        raise RuntimeError('KLINE_API_AES_KEY_B64配置无效') from exc
    if len(key) != 32:
        raise RuntimeError('KLINE_API_AES_KEY_B64必须是32字节密钥的Base64编码')

    nonce = os.urandom(12)
    plaintext = json.dumps(
        payload, ensure_ascii=False, separators=(',', ':')
    ).encode('utf-8')
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return {
        'success': True,
        'encrypted': True,
        'algorithm': 'AES-256-GCM',
        'version': 1,
        'nonce': base64.b64encode(nonce).decode('ascii'),
        'ciphertext': base64.b64encode(ciphertext).decode('ascii'),
    }


def _encrypt_10above5_kline_payload(payload):
    return _encrypt_kline_api_payload(payload, _KLINE_API_AES_AAD)


def _kline_api_reference_flags(stock_fields):
    """Normalize reference-day high-gain and limit-up flags for encrypted APIs."""
    stock_fields = stock_fields or {}
    high_gain_value = stock_fields.get('ref_high_gain_ok')
    if high_gain_value is None:
        high_gain_value = stock_fields.get('is_high_gain')
    if high_gain_value is None:
        high_gain_value = _ztorgj_parse_float(stock_fields.get('max_gain'), 0.0) >= 6.50
    limit_up_value = stock_fields.get('ref_is_limit_up')
    if limit_up_value is None:
        limit_up_value = stock_fields.get('is_limit_up', 0)
    return {
        'ref_high_gain_ok': _ztorgj_truthy_flag(high_gain_value),
        'ref_is_limit_up': _ztorgj_truthy_flag(limit_up_value),
    }


def _ztorgj_load_qmt_kline_payload(code, date_compact, days=30):
    simple_code = _ztorgj_stock_simple_code(code)
    ts_code = _ztorgj_normalize_ts_code(code)
    redis_key = _ztorgj_qmt_kline_key(date_compact)
    r = get_redis_client()
    raw_value = r.hget(redis_key, simple_code)
    if not raw_value:
        return None, redis_key, simple_code
    payload = _ztorgj_decode_stock_payload(raw_value)
    rows = _ztorgj_format_kline_rows(payload.get('rows') or payload.get('data') or [], ts_code, days, date_compact)
    if not rows:
        return None, redis_key, simple_code
    stock_name = str(payload.get('name') or payload.get('stock_name') or simple_code)
    stock_code = str(payload.get('ts_code') or payload.get('stock_code_with_market') or ts_code)
    return {
        'success': True,
        'stock': {'code': stock_code, 'name': stock_name},
        'days': days,
        'start_date': rows[0]['trade_date'],
        'end_date': rows[-1]['trade_date'],
        'data': rows,
        'source': 'redis:qmt:kline',
        'redis_key': redis_key,
        'redis_field': simple_code,
        'updated_at': payload.get('updated_at') or '',
    }, redis_key, simple_code


def _ztorgj_load_api_kline_payload(code, date_compact, days=30):
    simple_code = _ztorgj_stock_simple_code(code)
    ts_code = _ztorgj_normalize_ts_code(code)
    history_key = _ztorgj_api_history_kline_key(date_compact)
    realtime_key = _ztorgj_api_realtime_kline_key(date_compact)
    redis_client = get_redis_client()
    history_raw = redis_client.hget(history_key, simple_code)
    realtime_raw = redis_client.hget(realtime_key, simple_code)

    history_payload = (
        _ztorgj_decode_stock_payload(history_raw) if history_raw else {}
    )
    realtime_payload = (
        _ztorgj_decode_stock_payload(realtime_raw) if realtime_raw else {}
    )
    history_rows = (
        history_payload.get('rows')
        or history_payload.get('data')
        or []
    )
    realtime_row = realtime_payload.get('row')
    if not isinstance(realtime_row, dict):
        realtime_rows = (
            realtime_payload.get('rows')
            or realtime_payload.get('data')
            or []
        )
        if isinstance(realtime_rows, list) and realtime_rows:
            realtime_row = realtime_rows[-1]

    by_date = {}
    for row in history_rows:
        if not isinstance(row, dict):
            continue
        trade_date = str(row.get('trade_date') or '').strip().replace('-', '')
        if trade_date:
            by_date[trade_date] = row
    if isinstance(realtime_row, dict):
        trade_date = str(
            realtime_row.get('trade_date') or ''
        ).strip().replace('-', '')
        if trade_date:
            by_date[trade_date] = realtime_row

    rows = _ztorgj_format_kline_rows(
        [by_date[key] for key in sorted(by_date)],
        ts_code,
        days,
        date_compact,
    )
    if rows:
        stock_name = str(
            realtime_payload.get('name')
            or history_payload.get('name')
            or simple_code
        )
        stock_code = str(
            realtime_payload.get('stock_code_with_market')
            or history_payload.get('stock_code_with_market')
            or history_payload.get('ts_code')
            or ts_code
        )
        published_at = str(
            realtime_payload.get('published_at')
            or realtime_payload.get('updated_at')
            or history_payload.get('updated_at')
            or ''
        )
        redis_key = realtime_key if realtime_raw else history_key
        return {
            'success': True,
            'stock': {'code': stock_code, 'name': stock_name},
            'days': days,
            'start_date': rows[0]['trade_date'],
            'end_date': rows[-1]['trade_date'],
            'data': rows,
            'source': 'redis:qmt:kline:api',
            'redis_key': redis_key,
            'redis_field': simple_code,
            'snapshot_at': str(realtime_payload.get('snapshot_at') or ''),
            'published_at': published_at,
            'updated_at': published_at,
        }, redis_key, simple_code

    return _ztorgj_load_qmt_kline_payload(
        simple_code, date_compact, days=days
    )


def _pz_load_kline_payload(redis_client, code, date_compact, days=60):
    simple_code = _ztorgj_stock_simple_code(code)
    history_key = _pz_history_kline_key(date_compact)
    realtime_key = _pz_realtime_kline_key(date_compact)
    history_raw = redis_client.hget(history_key, simple_code)
    realtime_raw = redis_client.hget(realtime_key, simple_code)
    history_payload = _ztorgj_decode_stock_payload(history_raw) if history_raw else {}
    realtime_payload = _ztorgj_decode_stock_payload(realtime_raw) if realtime_raw else {}

    history_rows = history_payload.get('rows') or history_payload.get('data') or []
    realtime_row = realtime_payload.get('row')
    if not isinstance(realtime_row, dict):
        realtime_rows = realtime_payload.get('rows') or realtime_payload.get('data') or []
        realtime_row = realtime_rows[-1] if isinstance(realtime_rows, list) and realtime_rows else None

    by_date = {}
    for row in history_rows:
        if not isinstance(row, dict):
            continue
        trade_date = str(row.get('trade_date') or '').strip().replace('-', '')
        if trade_date:
            by_date[trade_date] = row
    if isinstance(realtime_row, dict):
        trade_date = str(realtime_row.get('trade_date') or '').strip().replace('-', '')
        if trade_date:
            by_date[trade_date] = realtime_row

    formatted = _ztorgj_format_kline_rows(
        [by_date[value] for value in sorted(by_date)],
        _ztorgj_normalize_ts_code(simple_code),
        days,
        date_compact,
    )
    rows = _ztorgj_public_ohlc_rows(formatted)
    if not rows:
        return None
    updated_at = str(
        realtime_payload.get('published_at')
        or realtime_payload.get('updated_at')
        or history_payload.get('updated_at')
        or ''
    ).strip()
    return {
        'rows': rows[-days:],
        'updated_at': updated_at,
        'snapshot_at': str(realtime_payload.get('snapshot_at') or '').strip(),
        'history_key': history_key,
        'realtime_key': realtime_key,
    }


def _qy1_load_kline_payload(redis_client, code, date_compact, days=60):
    simple_code = _ztorgj_stock_simple_code(code)
    history_key = _qy1_history_kline_key(date_compact)
    realtime_key = _qy1_realtime_kline_key(date_compact)
    history_raw = redis_client.hget(history_key, simple_code)
    realtime_raw = redis_client.hget(realtime_key, simple_code)
    history_payload = _ztorgj_decode_stock_payload(history_raw) if history_raw else {}
    realtime_payload = _ztorgj_decode_stock_payload(realtime_raw) if realtime_raw else {}

    history_rows = history_payload.get('rows') or history_payload.get('data') or []
    realtime_row = realtime_payload.get('row')
    if not isinstance(realtime_row, dict):
        realtime_rows = realtime_payload.get('rows') or realtime_payload.get('data') or []
        realtime_row = realtime_rows[-1] if isinstance(realtime_rows, list) and realtime_rows else None

    by_date = {}
    for row in history_rows:
        if not isinstance(row, dict):
            continue
        trade_date = str(row.get('trade_date') or '').strip().replace('-', '')
        if trade_date:
            by_date[trade_date] = row
    if isinstance(realtime_row, dict):
        trade_date = str(realtime_row.get('trade_date') or '').strip().replace('-', '')
        if trade_date:
            by_date[trade_date] = realtime_row

    formatted = _ztorgj_format_kline_rows(
        [by_date[value] for value in sorted(by_date)],
        _ztorgj_normalize_ts_code(simple_code),
        days,
        date_compact,
    )
    rows = _ztorgj_public_ohlc_rows(formatted)
    if not rows:
        return None
    updated_at = str(
        realtime_payload.get('published_at')
        or realtime_payload.get('updated_at')
        or history_payload.get('updated_at')
        or ''
    ).strip()
    return {
        'rows': rows[-days:],
        'updated_at': updated_at,
        'snapshot_at': str(realtime_payload.get('snapshot_at') or '').strip(),
        'history_key': history_key,
        'realtime_key': realtime_key,
    }


def _kline_api_stale_count(snapshot_times, now=None, threshold_seconds=15):
    now = now or datetime.now()
    stale_count = 0
    for value in snapshot_times:
        try:
            snapshot_time = datetime.strptime(
                str(value or ''), '%Y-%m-%d %H:%M:%S'
            )
        except (TypeError, ValueError):
            continue
        if (now - snapshot_time).total_seconds() > threshold_seconds:
            stale_count += 1
    return stale_count


def _ztorgj_load_weak20_kline_stocks(date_compact, new_high_only=False, max_weak_yin_20d=None):
    r = get_redis_client()
    rows = _ztorgj_load_hash_payloads(r, f'ruoyinxian:{date_compact}')
    stocks = []
    for code, info in rows:
        if new_high_only and not _ztorgj_truthy_flag(info.get('is_20d_new_high', 0)):
            continue
        weak_yin_20d = _ztorgj_parse_int(info.get('weak_yin_20d'), 0)
        if max_weak_yin_20d is not None and weak_yin_20d > max_weak_yin_20d:
            continue
        raw_code = str(info.get('code') or info.get('stock_code') or code or '').strip()
        stock_code_with_market = str(info.get('stock_code_with_market') or info.get('ts_code') or '').strip()
        if not stock_code_with_market:
            stock_code_with_market = _ztorgj_normalize_ts_code(raw_code) or raw_code
        simple_code = _ztorgj_stock_simple_code(raw_code or stock_code_with_market)
        display_gain_source = info.get('today_change') if info.get('today_change') is not None else info.get('ref_gain', info.get('change_percent', 0))
        display_gain = _ztorgj_parse_float(display_gain_source, 0.0)
        stocks.append({
            'code': simple_code,
            'name': str(info.get('name', '') or info.get('stock_name', '') or simple_code).strip(),
            'stock_code_with_market': stock_code_with_market,
            'ts_code': stock_code_with_market,
            'weak_yin_20d': weak_yin_20d,
            'yin_count_20': _ztorgj_parse_int(info.get('yin_count_20'), 0),
            'is_20d_new_high': 1 if _ztorgj_truthy_flag(info.get('is_20d_new_high', 0)) else 0,
            'today_change': display_gain,
            'ref_gain': _ztorgj_parse_float(info.get('ref_gain'), 0.0),
            '_display_gain': display_gain,
            'max_gain': _ztorgj_parse_float(info.get('max_gain'), 0.0),
            'high_gain_10d_count': _ztorgj_parse_int(info.get('high_gain_10d_count'), 0),
        })
    stocks.sort(key=lambda item: (999 if item['weak_yin_20d'] > 5 else item['weak_yin_20d'], -item['_display_gain'], item['code']))
    return stocks


def _ztorgj_build_weak20_kline_excel(date_compact, new_high_only=False, progress_callback=None):
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font, PatternFill

    stocks = _ztorgj_load_weak20_kline_stocks(date_compact, new_high_only)
    total = len(stocks)
    _ztorgj_excel_diag('build-list', date=date_compact, new_high_only=int(bool(new_high_only)), total=total)
    if progress_callback:
        progress_callback(0, total, '准备生成Excel')

    wb = Workbook()
    ws = wb.active
    ws.title = '20日弱阴线'
    ws.append(['股票名称', 'K线图'])
    ws.column_dimensions['A'].width = 20
    ws.column_dimensions['B'].width = 62
    ws.row_dimensions[1].height = 24
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='1D4ED8')
        cell.alignment = Alignment(horizontal='center', vertical='center')

    image_buffers = []
    for index, stock in enumerate(stocks, start=1):
        row_idx = index + 1
        if progress_callback:
            progress_callback(index - 1, total, f"正在生成 {stock['name']}")
        ws.cell(row=row_idx, column=1, value=stock['name'])
        ws.cell(row=row_idx, column=1).alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[row_idx].height = 128
        try:
            kline_payload, _redis_key, _redis_field = _ztorgj_load_qmt_kline_payload(stock['code'], date_compact, days=30)
            if not kline_payload:
                raise ValueError(f"Redis K线不存在: {stock['code']}")
            kline_rows = kline_payload.get('data') or []
            png_buffer = _ztorgj_render_candlestick_png(kline_rows)
            image_buffers.append(png_buffer)
            img = XLImage(png_buffer)
            img.width = 430
            img.height = 150
            ws.add_image(img, f'B{row_idx}')
        except Exception as item_error:
            _ztorgj_excel_diag('item-kline-fail', index=index, total=total, code=stock['code'], name=stock['name'], error=item_error)
            cell = ws.cell(row=row_idx, column=2, value='K线获取失败')
            cell.alignment = Alignment(horizontal='center', vertical='center')
        if progress_callback:
            progress_callback(index, total, f"已完成 {stock['name']}")

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    suffix = '_新高' if new_high_only else ''
    filename = f'20日弱阴线次数排序_{date_compact}{suffix}.xlsx'
    _ztorgj_excel_diag('build-complete', date=date_compact, new_high_only=int(bool(new_high_only)), total=total, filename=filename, bytes=len(output.getvalue()))
    return output.getvalue(), filename, total


def _ztorgj_weak20_excel_file_path(job_id):
    safe_job_id = re.sub(r'[^0-9a-fA-F]', '', str(job_id or '')) or uuid.uuid4().hex
    return os.path.join(_ZTORGJ_WEAK20_EXCEL_EXPORT_DIR, f'{safe_job_id}.xlsx')


def _ztorgj_weak20_excel_job_path(job_id):
    safe_job_id = re.sub(r'[^0-9a-fA-F]', '', str(job_id or '')) or uuid.uuid4().hex
    return os.path.join(_ZTORGJ_WEAK20_EXCEL_EXPORT_DIR, f'{safe_job_id}.json')


def _ztorgj_save_weak20_excel_file(job_id, file_bytes):
    os.makedirs(_ZTORGJ_WEAK20_EXCEL_EXPORT_DIR, exist_ok=True)
    file_path = _ztorgj_weak20_excel_file_path(job_id)
    with open(file_path, 'wb') as f:
        f.write(file_bytes)
    _ztorgj_excel_diag('file-saved', job_id=job_id, path=file_path, bytes=len(file_bytes or b''))
    return file_path


def _ztorgj_load_weak20_excel_file_from_path(file_path):
    if not file_path:
        return None
    try:
        abs_export_dir = os.path.abspath(_ZTORGJ_WEAK20_EXCEL_EXPORT_DIR)
        abs_file_path = os.path.abspath(file_path)
        if not abs_file_path.startswith(abs_export_dir + os.sep):
            return None
        if not os.path.exists(abs_file_path):
            return None
        with open(abs_file_path, 'rb') as f:
            return f.read()
    except Exception as e:
        _ztorgj_excel_diag('file-read-fail', path=file_path, error=e)
        return None


def _ztorgj_parse_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def _ztorgj_parse_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _ztorgj_persist_weak20_excel_job(job_id, job):
    """同步导出进度到本机文件，避免多 worker 时轮询不到进度。"""
    try:
        os.makedirs(_ZTORGJ_WEAK20_EXCEL_EXPORT_DIR, exist_ok=True)
        payload = {}
        for key in [
            'job_id', 'status', 'current', 'total', 'percent', 'message',
            'filename', 'error', 'file_path', 'created_at', 'updated_at',
        ]:
            payload[key] = job.get(key, '')
        job_path = _ztorgj_weak20_excel_job_path(job_id)
        tmp_path = f'{job_path}.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, job_path)
    except Exception as e:
        _ztorgj_excel_diag('job-write-fail', job_id=job_id, error=e)


def _ztorgj_load_weak20_excel_job_from_file(job_id, include_file=False):
    try:
        job_path = _ztorgj_weak20_excel_job_path(job_id)
        if not os.path.exists(job_path):
            return None
        with open(job_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        snapshot = {
            'job_id': data.get('job_id') or job_id,
            'status': data.get('status') or 'pending',
            'current': _ztorgj_parse_int(data.get('current'), 0),
            'total': _ztorgj_parse_int(data.get('total'), 0),
            'percent': _ztorgj_parse_int(data.get('percent'), 0),
            'message': data.get('message') or '',
            'filename': data.get('filename') or '',
            'error': data.get('error') or '',
            'file_path': data.get('file_path') or '',
            'created_at': _ztorgj_parse_float(data.get('created_at'), 0.0),
            'updated_at': _ztorgj_parse_float(data.get('updated_at'), 0.0),
        }
        if include_file:
            snapshot['file_bytes'] = _ztorgj_load_weak20_excel_file_from_path(snapshot.get('file_path'))
        return snapshot
    except Exception as e:
        _ztorgj_excel_diag('job-read-fail', job_id=job_id, error=e)
        return None


def _ztorgj_update_weak20_excel_job(job_id, **fields):
    persist_job = None
    with _ztorgj_weak20_excel_jobs_lock:
        job = _ztorgj_weak20_excel_jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        job['updated_at'] = time.time()
        persist_job = dict(job)
    _ztorgj_persist_weak20_excel_job(job_id, persist_job)


def _ztorgj_snapshot_weak20_excel_job(job_id, include_file=False):
    with _ztorgj_weak20_excel_jobs_lock:
        job = _ztorgj_weak20_excel_jobs.get(job_id)
        snapshot = dict(job) if job else None
    if not snapshot:
        return _ztorgj_load_weak20_excel_job_from_file(job_id, include_file=include_file)
    if include_file and not snapshot.get('file_bytes'):
        snapshot['file_bytes'] = _ztorgj_load_weak20_excel_file_from_path(snapshot.get('file_path'))
    if not include_file:
        snapshot.pop('file_bytes', None)
    return snapshot


def _ztorgj_cleanup_weak20_excel_jobs():
    now = time.time()
    with _ztorgj_weak20_excel_jobs_lock:
        stale_ids = [
            job_id for job_id, job in _ztorgj_weak20_excel_jobs.items()
            if now - float(job.get('updated_at', job.get('created_at', now))) > 1800
        ]
        for job_id in stale_ids:
            stale_job = _ztorgj_weak20_excel_jobs.get(job_id) or {}
            _ztorgj_weak20_excel_jobs.pop(job_id, None)
            for path in [stale_job.get('file_path'), _ztorgj_weak20_excel_job_path(job_id)]:
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass


def _ztorgj_run_weak20_excel_job(job_id, date_compact, new_high_only):
    try:
        _ztorgj_excel_diag('worker-start', job_id=job_id, date=date_compact, new_high_only=int(bool(new_high_only)), export_dir=_ZTORGJ_WEAK20_EXCEL_EXPORT_DIR)
        def progress_callback(current, total, message):
            percent = 100 if total <= 0 else int(round((float(current) / float(total)) * 100))
            percent = max(0, min(100, percent))
            _ztorgj_update_weak20_excel_job(
                job_id,
                status='running',
                current=int(current),
                total=int(total),
                percent=percent,
                message=message,
            )
            if current == 0 or current == total or current % 25 == 0 or percent in (10, 20, 30, 40, 50, 60, 70, 80, 90):
                _ztorgj_excel_diag('worker-progress', job_id=job_id, current=current, total=total, percent=percent, message=message)

        file_bytes, filename, total = _ztorgj_build_weak20_kline_excel(date_compact, new_high_only, progress_callback)
        file_path = _ztorgj_save_weak20_excel_file(job_id, file_bytes)
        _ztorgj_update_weak20_excel_job(
            job_id,
            status='completed',
            current=int(total),
            total=int(total),
            percent=100,
            message='导出完成',
            filename=filename,
            file_path=file_path,
            file_bytes=file_bytes,
        )
        _ztorgj_excel_diag('worker-complete', job_id=job_id, total=total, filename=filename, file_path=file_path, bytes=len(file_bytes or b''))
    except Exception as e:
        _ztorgj_excel_diag('worker-fail', job_id=job_id, error=e)
        print(traceback.format_exc(), flush=True)
        _ztorgj_update_weak20_excel_job(job_id, status='failed', message='导出失败', error=str(e), percent=0)


@app.route('/api/ztorgj/weak20/kline-excel')
def export_ztorgj_weak20_kline_excel():
    """导出20日弱阴线首列股票及纯K线图。"""
    try:
        date_compact = _ztorgj_parse_export_date(request.args.get('date'))
        new_high_only = str(request.args.get('new_high_only') or '0').strip() == '1'
        _ztorgj_excel_diag('direct-export-start', date=date_compact, new_high_only=int(bool(new_high_only)))
        file_bytes, filename, _total = _ztorgj_build_weak20_kline_excel(date_compact, new_high_only)
        _ztorgj_excel_diag('direct-export-complete', date=date_compact, filename=filename, bytes=len(file_bytes or b''))
        return send_file(
            io.BytesIO(file_bytes),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        _ztorgj_excel_diag('direct-export-fail', error=e)
        print(traceback.format_exc(), flush=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ztorgj/weak20/kline-excel/start', methods=['POST'])
def start_ztorgj_weak20_kline_excel():
    """启动20日弱阴线K线Excel后台导出任务。"""
    try:
        _ztorgj_cleanup_weak20_excel_jobs()
        payload = request.get_json(silent=True) or {}
        date_compact = _ztorgj_parse_export_date(request.args.get('date') or payload.get('date'))
        new_high_only = str(request.args.get('new_high_only', payload.get('new_high_only', '0'))).strip() == '1'
        job_id = uuid.uuid4().hex
        now = time.time()
        _ztorgj_excel_diag('start-request', job_id=job_id, date=date_compact, new_high_only=int(bool(new_high_only)), remote=request.remote_addr, export_dir=_ZTORGJ_WEAK20_EXCEL_EXPORT_DIR)
        initial_job = {
            'job_id': job_id,
            'status': 'pending',
            'current': 0,
            'total': 0,
            'percent': 0,
            'message': '等待开始',
            'filename': '',
            'error': '',
            'file_path': '',
            'file_bytes': None,
            'created_at': now,
            'updated_at': now,
        }
        with _ztorgj_weak20_excel_jobs_lock:
            _ztorgj_weak20_excel_jobs[job_id] = dict(initial_job)
        _ztorgj_persist_weak20_excel_job(job_id, initial_job)
        worker = threading.Thread(target=_ztorgj_run_weak20_excel_job, args=(job_id, date_compact, new_high_only), daemon=True)
        worker.start()
        _ztorgj_excel_diag('start-response', job_id=job_id, thread_name=worker.name, alive=worker.is_alive())
        return jsonify({'success': True, 'job_id': job_id})
    except Exception as e:
        _ztorgj_excel_diag('start-fail', error=e)
        print(traceback.format_exc(), flush=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ztorgj/weak20/kline-excel/progress/<job_id>')
def get_ztorgj_weak20_kline_excel_progress(job_id):
    job = _ztorgj_snapshot_weak20_excel_job(job_id)
    if not job:
        _ztorgj_excel_diag('progress-miss', job_id=job_id)
        return jsonify({'success': False, 'error': '导出任务不存在'}), 404
    current = _ztorgj_parse_int(job.get('current'), 0)
    total = _ztorgj_parse_int(job.get('total'), 0)
    percent = _ztorgj_parse_int(job.get('percent'), 0)
    if job.get('status') in ('failed', 'completed') or current == 0 or current == total or current % 50 == 0 or percent in (10, 20, 30, 40, 50, 60, 70, 80, 90):
        _ztorgj_excel_diag('progress-response', job_id=job_id, status=job.get('status'), current=current, total=total, percent=percent, message=job.get('message'), error=job.get('error'))
    job['success'] = True
    return jsonify(job)


@app.route('/api/ztorgj/weak20/kline-excel/download/<job_id>')
def download_ztorgj_weak20_kline_excel_job(job_id):
    job = _ztorgj_snapshot_weak20_excel_job(job_id, include_file=True)
    if not job:
        _ztorgj_excel_diag('download-miss', job_id=job_id)
        return jsonify({'success': False, 'error': '导出任务不存在'}), 404
    if job.get('status') == 'failed':
        _ztorgj_excel_diag('download-failed-job', job_id=job_id, error=job.get('error'))
        return jsonify({'success': False, 'error': job.get('error') or '导出失败'}), 500
    if job.get('status') != 'completed' or not job.get('file_bytes'):
        _ztorgj_excel_diag('download-not-ready', job_id=job_id, status=job.get('status'), message=job.get('message'), has_file=bool(job.get('file_bytes')))
        return jsonify({'success': False, 'status': job.get('status'), 'message': job.get('message') or '导出未完成'}), 202
    _ztorgj_excel_diag('download-send', job_id=job_id, filename=job.get('filename'), bytes=len(job.get('file_bytes') or b''), file_path=job.get('file_path'))
    return send_file(
        io.BytesIO(job['file_bytes']),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=job.get('filename') or '20日弱阴线次数排序.xlsx',
    )


@app.route('/api/ztorgj/weak20/kline-stocks')
def get_ztorgj_weak20_kline_stocks():
    """返回20日弱阴线首列 K 线浏览页股票，排序与首列一致。"""
    try:
        date_compact = _ztorgj_parse_export_date(request.args.get('date'))
        new_high_only = str(request.args.get('new_high_only') or '0').strip() == '1'
        max_weak_yin_arg = str(request.args.get('max_weak_yin_20d') or '').strip()
        max_weak_yin_20d = int(max_weak_yin_arg) if max_weak_yin_arg.isdigit() else None
        stocks = _ztorgj_load_weak20_kline_stocks(date_compact, new_high_only, max_weak_yin_20d)
        return jsonify({
            'success': True,
            'date': _ztorgj_format_date_text(date_compact),
            'new_high_only': bool(new_high_only),
            'max_weak_yin_20d': max_weak_yin_20d,
            'count': len(stocks),
            'data': stocks,
        })
    except Exception as e:
        print(f'[ztorgj][weak20-kline-stocks] 获取失败: {e}', flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': []}), 500



@app.route('/api/ztorgj/kline')
def get_ztorgj_kline():
    """从 QMT 写入 Redis 的 K 线缓存读取数据，前端负责绘图。"""
    raw_code = request.args.get('code') or '688628.SH'
    try:
        code = _ztorgj_normalize_ts_code(raw_code)
        if not code:
            code = '688628.SH'

        try:
            days = int(request.args.get('days') or 30)
        except Exception:
            days = 30
        days = max(1, min(days, 120))

        end_arg = str(request.args.get('end_date') or '').strip().replace('-', '')
        if end_arg and (len(end_arg) != 8 or not end_arg.isdigit()):
            end_arg = ''
        end_date = end_arg or datetime.now().strftime('%Y%m%d')
        print(
            f'[ztorgj][kline][redis-request] raw_code={raw_code} normalized_code={code} '
            f'days={days} end_date={end_date}',
            flush=True
        )

        result, redis_key, redis_field = _ztorgj_load_qmt_kline_payload(code, end_date, days=days)
        if not result:
            print(
                f'[ztorgj][kline][redis-miss] code={code} key={redis_key} field={redis_field}',
                flush=True
            )
            return jsonify({
                'success': False,
                'error': f'Redis K线数据不存在: {redis_key} {redis_field}',
                'data': [],
                'key': redis_key,
                'field': redis_field,
            }), 404
        print(
            f'[ztorgj][kline][redis-hit] code={code} key={redis_key} field={redis_field} '
            f'rows={len(result.get("data") or [])}',
            flush=True
        )
        print(
            f'[ztorgj][kline][success] code={code} rows={len(result.get("data") or [])} '
            f'first={result["start_date"]} last={result["end_date"]}',
            flush=True
        )
        return jsonify(result)
    except Exception as e:
        print(f'[ztorgj][kline][fail] raw_code={raw_code} error={e}', flush=True)
        print(traceback.format_exc(), flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': []})


@app.route('/api/ztorgj/dates')
def get_ztorgj_dates():
    """获取涨停或光脚所有有数据的日期"""
    try:
        r = get_redis_client()
        dates = set()
        for prefix in ('ztorgj', 'yxcs', 'ruoyinxian', 'ztgjcs', '20tupo', 'lsbp', 'yangxian', 'yangxian1', '5bupeng', 'lsyin', 'lsyin:quanyang', 'huiche6', 'lstz'):
            keys = r.keys(f'{prefix}:*')
            for key in keys:
                if ':' in key and not key.endswith(':timestamp'):
                    date_part = key.split(':')[-1]
                    if date_part and len(date_part) == 8 and date_part.isdigit():
                        formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                        dates.add(formatted_date)
        dates = sorted(dates, reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/ztorgj/stocks')
@app.route('/api/ztorgj/stocks/<date>')
def get_ztorgj_stocks(date=None):
    """获取涨停或光脚股票列表"""
    try:
        import base64
        import time as time_module
        from flask import request

        def _json_no_store(payload):
            response = jsonify(payload)
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
            return response

        start_time = time_module.time()
        r = get_redis_client()

        if not date:
            date = request.args.get('date')
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')

        today_compact = date.replace('-', '')
        def _load_hash_rows(key_name):
            key_type = r.type(key_name) if r.exists(key_name) else None
            if key_type != 'hash':
                return []
            all_rows = []
            cursor = 0
            while True:
                cursor, batch = r.hscan(key_name, cursor, count=100)
                all_rows.extend(batch.items())
                if cursor == 0:
                    break
            return all_rows

        def _decode_payload(data_str):
            try:
                decoded = base64.b64decode(data_str).decode('utf-8')
                return json.loads(decoded)
            except Exception:
                return json.loads(data_str)

        def _normalize_stock(code, info):
            is_bare_foot = info.get('is_bare_foot')
            if is_bare_foot is None:
                is_bare_foot = info.get('is_bare_foot_yang', 0)
            is_bare_foot_yang = info.get('is_bare_foot_yang', 0)
            is_high_gain = info.get('is_high_gain')
            if is_high_gain is None:
                is_high_gain = info.get('ref_high_gain_gte_685', 0)
            ref_high_gain_ok = info.get('ref_high_gain_ok', 0)
            ref_low_above_ma5 = info.get('ref_low_above_ma5', 0)
            ref_high_gain_no_touch = bool(
                _ztorgj_truthy_flag(ref_high_gain_ok) and _ztorgj_truthy_flag(ref_low_above_ma5)
            )
            return {
                'code': info.get('code', code),
                'name': info.get('name', ''),
                'stock_code_with_market': info.get('stock_code_with_market', ''),
                'board_type': info.get('board_type', ''),
                'limit_ratio': info.get('limit_ratio', 10.0),
                'ref_gain': info.get('ref_gain', 0),
                'today_change': info.get('today_change', info.get('ref_gain', 0)),
                'current_price': info.get('current_price', 0),
                'max_gain': info.get('max_gain', 0),
                'is_limit_up': info.get('is_limit_up', 0),
                'is_first_limit_up': info.get('is_first_limit_up', 0),
                'is_bare_foot': is_bare_foot,
                'is_bare_foot_yang': is_bare_foot_yang,
                'is_nearly_bare_foot_yang': info.get('is_nearly_bare_foot_yang', is_bare_foot_yang),
                'is_high_gain': is_high_gain,
                'is_ref_yang': info.get('is_ref_yang', info.get('ref_is_yang', 0)),
                'is_20d_new_high': info.get('is_20d_new_high', 0),
                'ref_high_gain_shape_ok': info.get('ref_high_gain_shape_ok', 0),
                'ref_high_gain_gte_685': info.get('ref_high_gain_gte_685', is_high_gain),
                'yin_count_20': info.get('yin_count_20', 0),
                'yin_count_10': info.get('yin_count_10', 0),
                'weak_yin_20d': info.get('weak_yin_20d', 0),
                'weak_yin_10d': info.get('weak_yin_10d', 0),
                'low_above_ma5_20d_count': info.get('low_above_ma5_20d_count', 0),
                'cum_gain_20d_pct': info.get('cum_gain_20d_pct', 0),
                'high_gain_10d_count': info.get('high_gain_10d_count', 0),
                'high_gain_10d_gte_5': info.get('high_gain_10d_gte_5', 0),
                'yin_pull_prev_or_next': info.get('yin_pull_prev_or_next', 0),
                'high_gain5_and_yin_pull': info.get('high_gain5_and_yin_pull', 0),
                'lsyin_sum': info.get('lsyin_sum', 0),
                'lsyin_avg': info.get('lsyin_avg', (info.get('lsyin_sum', 0) / info.get('yin_count_10d', 0)) if info.get('yin_count_10d', 0) else 0),
                'yin_count_10d': info.get('yin_count_10d', 0),
                'is_quanyang_10d': info.get('is_quanyang_10d', 0),
                'lsyin_group': info.get('lsyin_group', ''),
                'max_drawdown_10d': info.get('max_drawdown_10d', 0),
                'low_above_ma5_count': info.get('low_above_ma5_count', 0),
                'cum_high_to_ma5_pct': info.get('cum_high_to_ma5_pct', 0),
                'gain_10d_pct': info.get('gain_10d_pct', 0),
                'is_all_yin_doji': info.get('is_all_yin_doji', 0),
                'is_all_yang': info.get('is_all_yang', 0),
                'all_yin_cross_star': info.get('all_yin_cross_star', 0),
                'all_yin_no_touch': info.get('all_yin_no_touch', 0),
                'all_yin_up': info.get('all_yin_up', 0),
                'adjust_no_break': info.get('adjust_no_break', 0),
                'layang': info.get('layang', 0),
                'layang5days': info.get('layang5days', 0),
                'strong_adjust_k_pct': info.get('strong_adjust_k_pct', 0),
                'is_breakout': info.get('is_breakout', 0),
                'breakout_gain_pct': info.get('breakout_gain_pct', 0),
                'lstz_new_high': info.get('lstz_new_high', 0),
                'lstz_new_high_pct': info.get('lstz_new_high_pct', 0),
                'max_rise_top_day': info.get('max_rise_top_day', ''),
                'top_day_close': info.get('top_day_close', 0),
                'top_day_ref_max_drawdown_pct': info.get('top_day_ref_max_drawdown_pct', 0),
                'max_rise_pct': info.get('max_rise_pct', 0),
                'max_rise_days': info.get('max_rise_days', 0),
                'yin_below_ma5_count': info.get('yin_below_ma5_count', 0),
                'yin_below_ma5_dates': info.get('yin_below_ma5_dates', []),
                'ref_shape_ok': info.get('ref_shape_ok', 0),
                'yin_count_10': info.get('yin_count_10', 0),
                'limit_up_count_20': info.get('limit_up_count_20', 0),
                'bare_foot_count_20': info.get('bare_foot_count_20', 0),
                'ref_is_limit_up': info.get('ref_is_limit_up', info.get('is_limit_up', 0)),
                'ref_high_gain_ok': ref_high_gain_ok,
                'ref_high_gain_gte_6_5_and_yang': info.get('ref_high_gain_gte_6_5_and_yang', 0),
                'ref_low_above_ma5': ref_low_above_ma5,
                'ref_high_gain_gte_6_5_yang_low_above_ma5': ref_high_gain_no_touch,
                'ref_low_ma5_distance_pct': info.get('ref_low_ma5_distance_pct', 0),
                'ref_lower_shadow_pct': info.get('ref_lower_shadow_pct', 0),
                'yin_close_below_ma5_count_10d': info.get('yin_close_below_ma5_count_10d', 0),
                'yin_close_below_ma5_lte_2_10d': info.get('yin_close_below_ma5_lte_2_10d', 0),
                'yin_body_pct_sum_10d': info.get('yin_body_pct_sum_10d', 0),
                'all_yin_pull_prev_or_next': info.get('all_yin_pull_prev_or_next', 0),
                'days_since_top_day': info.get('days_since_top_day', 0),
                'top_day_close_below_count': info.get('top_day_close_below_count', 0),
                'ref_close': info.get('ref_close', 0),
                'max_close_20': info.get('max_close_20', 0),
                'gain_20d': info.get('gain_20d', 0),
                'consecutive_days': info.get('consecutive_days', 0),
                'current_streak': info.get('current_streak', 0),
                'strong_no_touch_10d_count': info.get('strong_no_touch_10d_count', info.get('low_above_ma5_10d_count', 0)),
                'low_above_ma5_10d_count': info.get('low_above_ma5_10d_count', 0),
                'dingtalk_pushed': info.get('dingtalk_pushed', 0),
                'dingtalk_push_time': info.get('dingtalk_push_time', ''),
                'dingtalk_push_title': info.get('dingtalk_push_title', ''),
            }

        def _load_dataset(prefix):
            rows = _load_hash_rows(f"{prefix}:{today_compact}")
            items = []
            scan = None
            snapshot = None
            for code, data_str in rows:
                try:
                    if isinstance(code, bytes):
                        code = code.decode('utf-8', errors='ignore')
                    if code in ('_scan_info', '_timestamp', '_snapshot_info'):
                        if code == '_scan_info':
                            scan = _yhy_normalize_scan_info(_yhy_decode_json_payload(data_str))
                        elif code == '_snapshot_info':
                            snapshot = _yhy_normalize_snapshot_info(_yhy_decode_json_payload(data_str))
                        continue
                    info = _decode_payload(data_str)
                    items.append(_normalize_stock(code, info))
                except Exception as e:
                    print(f"解析{prefix}股票数据失败 {code}: {e}")
                    continue
            return items, scan, snapshot

        prefixes = ('ztorgj', 'yxcs', 'ruoyinxian', 'ztgjcs', '20tupo', 'lsbp', 'yangxian', 'yangxian1', '5bupeng', 'lsyin', 'lsyin:quanyang', 'huiche6', 'lstz')
        dataset_aliases = {'lsyin:quanyang': 'lsyin_quanyang'}
        snapshot_required_prefixes = tuple(prefix for prefix in prefixes if prefix not in ('lstz', 'lsbp'))
        datasets = {}
        scan_candidates = []
        snapshot_candidates = []
        snapshot_infos_by_prefix = {}
        for prefix in prefixes:
            items, scan, snapshot = _load_dataset(prefix)
            datasets[dataset_aliases.get(prefix, prefix)] = items
            if scan:
                scan_candidates.append(scan)
            if snapshot:
                snapshot_candidates.append(snapshot)
                snapshot_infos_by_prefix[prefix] = snapshot

        scan_info = None
        if scan_candidates:
            scan_info = max(scan_candidates, key=lambda item: str(item.get('last_scan_end_time') or ''))
        snapshot_info = _yhy_pick_latest_snapshot_info(snapshot_candidates)
        snapshot_gate = _yhy_build_snapshot_gate(snapshot_infos_by_prefix, snapshot_required_prefixes)

        if not any(datasets.values()):
            return _json_no_store({
                'success': True,
                'data': [],
                'datasets': datasets,
                'message': f'没有找到数据key: ztorgj:{today_compact} / yxcs:{today_compact} / ruoyinxian:{today_compact} / ztgjcs:{today_compact} / 20tupo:{today_compact} / lsbp:{today_compact} / yangxian:{today_compact} / yangxian1:{today_compact} / 5bupeng:{today_compact} / lsyin:{today_compact} / lsyin:quanyang:{today_compact} / huiche6:{today_compact} / lstz:{today_compact}',
                'scan_info': scan_info,
                'snapshot_info': snapshot_info,
                'snapshot_version': snapshot_gate['snapshot_version'],
                'is_snapshot_stable': snapshot_gate['is_snapshot_stable'],
                'snapshot_reason': snapshot_gate['snapshot_reason'],
            })

        total_time = (time_module.time() - start_time) * 1000
        return _json_no_store({
            'success': True,
            'data': datasets.get('ztorgj', []),
            'datasets': datasets,
            'count': len(datasets.get('ztorgj', [])),
            'date': date,
            'scan_info': scan_info,
            'snapshot_info': snapshot_info,
            'snapshot_version': snapshot_gate['snapshot_version'],
            'is_snapshot_stable': snapshot_gate['is_snapshot_stable'],
            'snapshot_reason': snapshot_gate['snapshot_reason'],
            'performance': {'total_ms': round(total_time, 2)}
        })
    except Exception as e:
        print(f"获取ztorgj数据失败: {e}")
        response = jsonify({'success': False, 'error': str(e)})
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        return response


@app.route('/api/ztorgj/check_update')
def check_ztorgj_update():
    """检查涨停或光脚数据是否更新"""
    try:
        r = get_redis_client()
        today_compact = datetime.now().strftime('%Y%m%d')
        key = f"ztorgj:{today_compact}"
        if r.exists(key):
            count = r.hlen(key) if r.type(key) == 'hash' else 0
            ts_key = f"ztorgj:{today_compact}:timestamp"
            timestamp = r.get(ts_key) if r.exists(ts_key) else str(count)
            return jsonify({
                'success': True, 'updated': True,
                'timestamp': timestamp, 'count': count,
                'refresh_interval': get_redis_refresh_interval()
            })
        return jsonify({
            'success': True, 'updated': False,
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/ztorgj/realtime', methods=['POST'])
def get_ztorgj_realtime():
    """涨停或光脚实时涨跌幅接口"""
    try:
        import time as time_module

        data = request.get_json()
        codes = data.get('codes', []) if isinstance(data, dict) else []

        if not codes:
            return jsonify({'success': True, 'data': {}})

        code_map = {}
        for code in codes:
            code_str = str(code or '').strip()
            if not code_str:
                continue
            if '.' in code_str:
                if code_str.endswith('.SH'):
                    tc = f"sh{code_str.replace('.SH', '')}"
                elif code_str.endswith('.SZ'):
                    tc = f"sz{code_str.replace('.SZ', '')}"
                else:
                    tc = f"sz{code_str.split('.')[0]}"
            else:
                tc = f"sh{code_str}" if code_str.startswith('6') else f"sz{code_str}"
            code_map[tc] = code_str

        if not code_map:
            return jsonify({'success': True, 'data': {}})

        realtime_data = {}
        parsed_tencent_codes = set()
        parsed_sample = []
        invalid_count = 0
        zero_preclose_count = 0
        exception_count = 0
        batch_debug = []
        code_items = list(code_map.items())
        chunk_size = 200
        request_start = time_module.time()
        for batch_index in range(0, len(code_items), chunk_size):
            chunk_items = code_items[batch_index:batch_index + chunk_size]
            chunk_map = dict(chunk_items)
            batch = ','.join(chunk_map.keys())
            url = f"http://qt.gtimg.cn/q={batch}"
            chunk_start = time_module.time()
            response = requests.get(url, timeout=10)
            response.encoding = 'gbk'
            chunk_elapsed_ms = (time_module.time() - chunk_start) * 1000

            raw_text = response.text.strip()
            raw_lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
            semicolon_entries = [line.strip() for line in raw_text.split(';') if line.strip()]
            entries = semicolon_entries if semicolon_entries else raw_lines
            status_code = getattr(response, 'status_code', 0)
            try:
                status_code = int(status_code)
            except Exception:
                status_code = str(status_code)
            batch_debug.append({
                'batch_index': len(batch_debug) + 1,
                'batch_count': len(chunk_items),
                'status': status_code,
                'url_len': len(url),
                'raw_len': len(raw_text),
                'newline_entries': len(raw_lines),
                'semicolon_entries': len(semicolon_entries),
                'elapsed_ms': round(chunk_elapsed_ms, 2),
                'first': raw_text[:120],
            })

            for line in entries:
                try:
                    tc = line.split('=')[0].replace('v_', '').strip()
                    fields = line.split('~')
                    if len(fields) <= 4 or tc not in code_map:
                        invalid_count += 1
                        continue
                    current_price = float(fields[3]) if fields[3] else 0
                    pre_close = float(fields[4]) if fields[4] else 0
                    if pre_close <= 0:
                        zero_preclose_count += 1
                        continue
                    change = round((current_price - pre_close) / pre_close * 100, 2)
                    code = code_map[tc]
                    realtime_data[code] = {'change': change}
                    parsed_tencent_codes.add(tc)
                    if len(parsed_sample) < 8:
                        parsed_sample.append({
                            'tc': tc,
                            'code': code,
                            'price': current_price,
                            'pre_close': pre_close,
                            'change': change,
                        })
                except Exception:
                    exception_count += 1
                    continue
        elapsed_ms = (time_module.time() - request_start) * 1000

        print(
            f"[ztorgj][realtime] codes={len(codes)} batch={len(code_map)} "
            f"chunks={len(batch_debug)} elapsed_ms={elapsed_ms:.2f} sample={codes[:5]}",
            flush=True
        )
        if batch_debug:
            print(
                f"[ztorgj][realtime][raw] first_batch={batch_debug[0]} "
                f"last_batch={batch_debug[-1]}",
                flush=True
            )

        missing_tencent_codes = [tc for tc in code_map.keys() if tc not in parsed_tencent_codes]
        missing_codes = [code_map[tc] for tc in missing_tencent_codes[:10]]
        debug_info = {
            'requested_count': len(codes),
            'batch_count': len(code_map),
            'chunk_count': len(batch_debug),
            'chunk_size': chunk_size,
            'parsed_count': len(realtime_data),
            'missing_count': len(missing_tencent_codes),
            'invalid_count': invalid_count,
            'zero_preclose_count': zero_preclose_count,
            'exception_count': exception_count,
            'newline_entries': sum(item['newline_entries'] for item in batch_debug),
            'semicolon_entries': sum(item['semicolon_entries'] for item in batch_debug),
            'batch_debug': batch_debug[:3] + (batch_debug[-1:] if len(batch_debug) > 3 else []),
            'sample': parsed_sample,
            'missing_sample': missing_codes,
        }
        print(f"[ztorgj][realtime][parsed] {debug_info}", flush=True)

        return jsonify({'success': True, 'data': realtime_data, 'debug': debug_info})
    except Exception as e:
        print(f"[ztorgj][realtime] 获取失败: {e}")
        return jsonify({'success': False, 'error': str(e), 'data': {}})


# ========== 20阴少跌少横盘页面相关API ==========
@app.route('/20阴少跌少横盘')
def yx205_page():
    """20阴少跌少横盘页面"""
    return render_template('yx205.html')

@app.route('/api/yx205/stocks')
@app.route('/api/yx205/stocks/<date>')
def get_yx205_stocks(date=None):
    """获取20阴少跌少横盘股票列表"""
    try:
        import base64
        import time as time_module
        from flask import request
        
        # 记录开始时间
        start_time = time_module.time()
        
        r = get_redis_client()
        
        # 如果没有指定日期，使用今天
        if not date:
            date = request.args.get('date')
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
        
        # 转换日期格式
        today = date
        today_compact = date.replace('-', '')
        
        # Redis key格式: yx205:20260127
        key = f"yx205:{today_compact}"
        key2 = f"yx2051:{today_compact}"
        key3 = f"yx2052:{today_compact}"
        scan_info = None
        scan_info2 = None
        scan_info3 = None
        
        # 测量网络延迟
        ping_start = time_module.time()
        r.ping()
        network_latency = (time_module.time() - ping_start) * 1000
        print(f"Redis网络延迟: {network_latency:.2f}ms")
        
        # 检查key是否存在
        key_type = r.type(key) if r.exists(key) else None
        
        if not key_type:
            # yx205 key不存在，但还需要检查yx2051 / yx2052 是否有数据
            key2_type = r.type(key2) if r.exists(key2) else None
            key3_type = r.type(key3) if r.exists(key3) else None
            if not key2_type and not key3_type:
                total_time = (time_module.time() - start_time) * 1000
                return jsonify({
                    'success': True,
                    'data': [],
                    'data2': [],
                    'data3': [],
                    'scan_info': None,
                    'performance': {
                        'total_time': f"{total_time:.2f}ms",
                        'network_latency': f"{network_latency:.2f}ms",
                        'count': 0,
                        'count2': 0,
                        'count3': 0
                    },
                    'message': f'没有找到数据key: {key}'
                })
            # yx205没有数据但yx2051/yx2052有数据，跳过yx205读取直接走后面的逻辑
            print(f"yx205 key不存在，继续读取备用key: {key2}, {key3}")
            all_keys = []
            stocks = []
            scan_time = 0
            parse_time = 0
            total_time = (time_module.time() - start_time) * 1000
            # 直接跳到yx2051数据读取（复用下方代码块）
            is_today = (date == datetime.now().strftime('%Y-%m-%d'))
            stocks2 = []
            import base64
            all_keys2 = []
            if key2_type == 'hash':
                cursor = 0
                while True:
                    cursor, keys2_batch = r.hscan(key2, cursor, count=100)
                    all_keys2.extend(keys2_batch.items())
                    if cursor == 0:
                        break
            elif key2_type == 'string':
                data_str2 = r.get(key2)
                if data_str2:
                    try:
                        data_list2 = json.loads(data_str2)
                        for item2 in data_list2:
                            code2 = item2.get('code', '')
                            all_keys2.append((code2, json.dumps(item2, ensure_ascii=False)))
                    except:
                        pass
            for code2, data_str2 in all_keys2:
                try:
                    if isinstance(code2, bytes):
                        code2 = code2.decode('utf-8', errors='ignore')
                    if code2 in ('_scan_info', '_timestamp'):
                        if code2 == '_scan_info':
                            scan_info2 = _yhy_normalize_scan_info(_yhy_decode_json_payload(data_str2))
                        continue
                    try:
                        decoded2 = base64.b64decode(data_str2).decode('utf-8')
                        stock_info2 = json.loads(decoded2)
                    except:
                        stock_info2 = json.loads(data_str2)
                    stocks2.append({
                        'code': stock_info2.get('code', code2.decode('utf-8') if isinstance(code2, bytes) else code2),
                        'name': stock_info2.get('name', ''),
                        'yin_count': stock_info2.get('yin_count', stock_info2.get('yin_line_count', 0)),
                        'today_change': stock_info2.get('today_change', stock_info2.get('current_gain', 0)),
                        'is_limit_up': stock_info2.get('is_limit_up', stock_info2.get('limit_up', False)),
                        'market_cap': stock_info2.get('market_cap', 0),
                        'weak_yin_count': stock_info2.get('weak_yin_count', 0),
                        'weak_yin_group': stock_info2.get('weak_yin_group', ''),
                        'no_up_down_kill_ratio': stock_info2.get('no_up_down_kill_ratio', 0),
                        'max_drawdown_20': stock_info2.get('max_drawdown_20', None)
                    })
                except Exception as e:
                    print(f"解析yx2051股票数据失败 {code2}: {e}")
                    continue
            if not is_today and stocks2:
                pro = ts.pro_api()
                for stock2 in stocks2:
                    try:
                        code2 = stock2['code']
                        if code2.startswith('6'):
                            ts_code2 = f"{code2}.SH"
                        elif code2.startswith('0') or code2.startswith('3'):
                            ts_code2 = f"{code2}.SZ"
                        elif code2.startswith('8') or code2.startswith('4'):
                            ts_code2 = f"{code2}.BJ"
                        else:
                            ts_code2 = f"{code2}.SZ"
                        df2 = pro.daily(ts_code=ts_code2, start_date=today_compact, end_date=today_compact)
                        if not df2.empty:
                            stock2['today_change'] = float(df2.iloc[0]['pct_chg'])
                    except Exception as e:
                        print(f"  获取{code2}历史涨跌幅失败: {e}")
                        pass
            stocks3 = []
            all_keys3 = []
            if key3_type == 'hash':
                cursor = 0
                while True:
                    cursor, keys3_batch = r.hscan(key3, cursor, count=100)
                    all_keys3.extend(keys3_batch.items())
                    if cursor == 0:
                        break
            elif key3_type == 'string':
                data_str3 = r.get(key3)
                if data_str3:
                    try:
                        data_list3 = json.loads(data_str3)
                        for item3 in data_list3:
                            code3 = item3.get('code', '')
                            all_keys3.append((code3, json.dumps(item3, ensure_ascii=False)))
                    except:
                        pass
            for code3, data_str3 in all_keys3:
                try:
                    if isinstance(code3, bytes):
                        code3 = code3.decode('utf-8', errors='ignore')
                    if code3 in ('_scan_info', '_timestamp'):
                        if code3 == '_scan_info':
                            scan_info3 = _yhy_normalize_scan_info(_yhy_decode_json_payload(data_str3))
                        continue
                    try:
                        decoded3 = base64.b64decode(data_str3).decode('utf-8')
                        stock_info3 = json.loads(decoded3)
                    except:
                        stock_info3 = json.loads(data_str3)
                    stocks3.append({
                        'code': stock_info3.get('code', code3),
                        'name': stock_info3.get('name', ''),
                        'yin_count': stock_info3.get('yin_count', stock_info3.get('yin_line_count', 0)),
                        'today_change': stock_info3.get('today_change', stock_info3.get('current_gain', 0)),
                        'is_limit_up': stock_info3.get('is_limit_up', stock_info3.get('limit_up', False)),
                        'market_cap': stock_info3.get('market_cap', 0),
                        'weak_yin_count': stock_info3.get('weak_yin_count', 0),
                        'weak_yin_group': stock_info3.get('weak_yin_group', ''),
                        'no_up_down_kill_ratio': stock_info3.get('no_up_down_kill_ratio', 0),
                        'max_drawdown_20': stock_info3.get('max_drawdown_20', None)
                    })
                except Exception as e:
                    print(f"解析yx2052股票数据失败 {code3}: {e}")
                    continue
            if not is_today and stocks3:
                pro = ts.pro_api()
                for stock3 in stocks3:
                    try:
                        code3 = stock3['code']
                        if code3.startswith('6'):
                            ts_code3 = f"{code3}.SH"
                        elif code3.startswith('0') or code3.startswith('3'):
                            ts_code3 = f"{code3}.SZ"
                        elif code3.startswith('8') or code3.startswith('4'):
                            ts_code3 = f"{code3}.BJ"
                        else:
                            ts_code3 = f"{code3}.SZ"
                        df3 = pro.daily(ts_code=ts_code3, start_date=today_compact, end_date=today_compact)
                        if not df3.empty:
                            stock3['today_change'] = float(df3.iloc[0]['pct_chg'])
                    except Exception as e:
                        print(f"  获取{code3}历史涨跌幅失败: {e}")
                        pass
            total_time = (time_module.time() - start_time) * 1000
            return jsonify({
                'success': True,
                'data': [],
                'data2': stocks2,
                'data3': stocks3,
                'scan_info': _yx205_pick_latest_scan_info(scan_info, scan_info2, scan_info3),
                'performance': {
                    'total_time': f"{total_time:.2f}ms",
                    'network_latency': f"{network_latency:.2f}ms",
                    'scan_time': '0.00ms',
                    'parse_time': '0.00ms',
                    'count': 0,
                    'count2': len(stocks2),
                    'count3': len(stocks3)
                }
            })
        
        print(f"使用key: {key}, 类型: {key_type}")
        
        # 使用SCAN获取数据
        scan_start = time_module.time()
        all_keys = []
        
        if key_type == 'hash':
            # Hash类型：使用HSCAN逐批次获取
            cursor = 0
            while True:
                cursor, keys = r.hscan(key, cursor, count=100)
                all_keys.extend(keys.items())
                if cursor == 0:
                    break
        elif key_type == 'string':
            # String类型：直接GET获取JSON数组
            data_str = r.get(key)
            if data_str:
                try:
                    data_list = json.loads(data_str)
                    # 转换为(code, json_str)格式
                    for item in data_list:
                        code = item.get('code', '')
                        all_keys.append((code, json.dumps(item, ensure_ascii=False)))
                except:
                    pass
        
        scan_time = (time_module.time() - scan_start) * 1000
        print(f"数据获取耗时: {scan_time:.2f}ms, 获取{len(all_keys)}条数据")
        
        if not all_keys:
            total_time = (time_module.time() - start_time) * 1000
            return jsonify({
                'success': True, 
                'data': [],
                'performance': {
                    'total_time': f"{total_time:.2f}ms",
                    'network_latency': f"{network_latency:.2f}ms",
                    'count': 0
                }
            })
        
        # 解析数据
        parse_start = time_module.time()
        stocks = []
        
        for code, data_str in all_keys:
            try:
                if isinstance(code, bytes):
                    code = code.decode('utf-8', errors='ignore')
                if code in ('_scan_info', '_timestamp'):
                    if code == '_scan_info':
                        scan_info = _yhy_normalize_scan_info(_yhy_decode_json_payload(data_str))
                    continue
                # 尝试Base64解码
                try:
                    decoded_data = base64.b64decode(data_str).decode('utf-8')
                    stock_info = json.loads(decoded_data)
                except:
                    # 如果不是Base64，直接解析JSON
                    stock_info = json.loads(data_str)
                
                # 提取字段
                today_change = stock_info.get('today_change', stock_info.get('current_gain', 0))
                yin_count = stock_info.get('yin_count', stock_info.get('yin_line_count', 0))
                is_limit_up = stock_info.get('is_limit_up', stock_info.get('limit_up', False))
                market_cap = stock_info.get('market_cap', 0)
                weak_yin_count = stock_info.get('weak_yin_count', 0)
                weak_yin_group = stock_info.get('weak_yin_group', '')
                no_up_down_kill_ratio = stock_info.get('no_up_down_kill_ratio', 0)
                
                stocks.append({
                    'code': stock_info.get('code', code.decode('utf-8') if isinstance(code, bytes) else code),
                    'name': stock_info.get('name', ''),
                    'yin_count': yin_count,
                    'today_change': today_change,
                    'is_limit_up': is_limit_up,
                    'market_cap': market_cap,
                    'weak_yin_count': weak_yin_count,
                    'weak_yin_group': weak_yin_group,
                    'no_up_down_kill_ratio': no_up_down_kill_ratio,
                    'max_drawdown_20': stock_info.get('max_drawdown_20', None)
                })
            except Exception as e:
                print(f"解析股票数据失败 {code}: {e}")
                continue
        
        parse_time = (time_module.time() - parse_start) * 1000
        
        print(f"数据解析耗时: {parse_time:.2f}ms")
        
        # 如果查看历史日期，从Tushare获取历史涨跌幅
        is_today = (date == datetime.now().strftime('%Y-%m-%d'))
        if not is_today and stocks:
            print(f"📅 查看历史日期 {date}，从Tushare获取历史涨跌幅")
            tushare_start = time_module.time()
            
            # 批量获取历史涨跌幅
            pro = ts.pro_api()
            for stock in stocks:
                try:
                    code = stock['code']
                    # 转换股票代码格式
                    if code.startswith('6'):
                        ts_code = f"{code}.SH"
                    elif code.startswith('0') or code.startswith('3'):
                        ts_code = f"{code}.SZ"
                    elif code.startswith('8') or code.startswith('4'):
                        ts_code = f"{code}.BJ"
                    else:
                        ts_code = f"{code}.SZ"
                    
                    # 获取历史日线数据
                    df = pro.daily(ts_code=ts_code, start_date=today_compact, end_date=today_compact)
                    if not df.empty:
                        stock['today_change'] = float(df.iloc[0]['pct_chg'])
                        print(f"  {stock['name']}({code}): {stock['today_change']:.2f}%")
                except Exception as e:
                    print(f"  获取{code}历史涨跌幅失败: {e}")
                    # 保持原有的today_change值
                    pass
            
            tushare_time = (time_module.time() - tushare_start) * 1000
            print(f"Tushare获取历史涨跌幅耗时: {tushare_time:.2f}ms")
        
        total_time = (time_module.time() - start_time) * 1000
        print(f"总耗时: {total_time:.2f}ms")
        
        # 获取 yx2051 数据
        stocks2 = []
        
        key2_type = r.type(key2) if r.exists(key2) else None
        if key2_type:
            print(f"使用key2: {key2}, 类型: {key2_type}")
            all_keys2 = []
            
            if key2_type == 'hash':
                cursor = 0
                while True:
                    cursor, keys = r.hscan(key2, cursor, count=100)
                    all_keys2.extend(keys.items())
                    if cursor == 0:
                        break
            elif key2_type == 'string':
                data_str = r.get(key2)
                if data_str:
                    try:
                        data_list = json.loads(data_str)
                        for item in data_list:
                            code = item.get('code', '')
                            all_keys2.append((code, json.dumps(item, ensure_ascii=False)))
                    except:
                        pass
            
            # 解析 yx2051 数据
            for code, data_str in all_keys2:
                try:
                    if isinstance(code, bytes):
                        code = code.decode('utf-8', errors='ignore')
                    if code in ('_scan_info', '_timestamp'):
                        if code == '_scan_info':
                            scan_info2 = _yhy_normalize_scan_info(_yhy_decode_json_payload(data_str))
                        continue
                    try:
                        decoded_data = base64.b64decode(data_str).decode('utf-8')
                        stock_info = json.loads(decoded_data)
                    except:
                        stock_info = json.loads(data_str)
                    
                    today_change = stock_info.get('today_change', stock_info.get('current_gain', 0))
                    yin_count = stock_info.get('yin_count', stock_info.get('yin_line_count', 0))
                    is_limit_up = stock_info.get('is_limit_up', stock_info.get('limit_up', False))
                    market_cap = stock_info.get('market_cap', 0)
                    weak_yin_count = stock_info.get('weak_yin_count', 0)
                    weak_yin_group = stock_info.get('weak_yin_group', '')
                    no_up_down_kill_ratio = stock_info.get('no_up_down_kill_ratio', 0)
                    
                    stocks2.append({
                        'code': stock_info.get('code', code.decode('utf-8') if isinstance(code, bytes) else code),
                        'name': stock_info.get('name', ''),
                        'yin_count': yin_count,
                        'today_change': today_change,
                        'is_limit_up': is_limit_up,
                        'market_cap': market_cap,
                        'weak_yin_count': weak_yin_count,
                        'weak_yin_group': weak_yin_group,
                        'no_up_down_kill_ratio': no_up_down_kill_ratio,
                        'max_drawdown_20': stock_info.get('max_drawdown_20', None)
                    })
                except Exception as e:
                    print(f"解析yx2051股票数据失败 {code}: {e}")
                    continue
            
            # 如果查看历史日期，从Tushare获取历史涨跌幅
            if not is_today and stocks2:
                print(f"📅 查看历史日期 {date}，为yx2051数据从Tushare获取历史涨跌幅")
                
                for stock in stocks2:
                    try:
                        code = stock['code']
                        # 转换股票代码格式
                        if code.startswith('6'):
                            ts_code = f"{code}.SH"
                        elif code.startswith('0') or code.startswith('3'):
                            ts_code = f"{code}.SZ"
                        elif code.startswith('8') or code.startswith('4'):
                            ts_code = f"{code}.BJ"
                        else:
                            ts_code = f"{code}.SZ"
                        
                        # 获取历史日线数据
                        df = pro.daily(ts_code=ts_code, start_date=today_compact, end_date=today_compact)
                        if not df.empty:
                            stock['today_change'] = float(df.iloc[0]['pct_chg'])
                            print(f"  {stock['name']}({code}): {stock['today_change']:.2f}%")
                    except Exception as e:
                        print(f"  获取{code}历史涨跌幅失败: {e}")
                        pass
        
        # 获取 yx2052 数据（特别关注）
        stocks3 = []

        key3_type = r.type(key3) if r.exists(key3) else None
        if key3_type:
            print(f"使用key3: {key3}, 类型: {key3_type}")
            all_keys3 = []

            if key3_type == 'hash':
                cursor = 0
                while True:
                    cursor, keys = r.hscan(key3, cursor, count=100)
                    all_keys3.extend(keys.items())
                    if cursor == 0:
                        break
            elif key3_type == 'string':
                data_str = r.get(key3)
                if data_str:
                    try:
                        data_list = json.loads(data_str)
                        for item in data_list:
                            code = item.get('code', '')
                            all_keys3.append((code, json.dumps(item, ensure_ascii=False)))
                    except:
                        pass

            for code, data_str in all_keys3:
                try:
                    if isinstance(code, bytes):
                        code = code.decode('utf-8', errors='ignore')
                    if code in ('_scan_info', '_timestamp'):
                        if code == '_scan_info':
                            scan_info3 = _yhy_normalize_scan_info(_yhy_decode_json_payload(data_str))
                        continue
                    try:
                        decoded_data = base64.b64decode(data_str).decode('utf-8')
                        stock_info = json.loads(decoded_data)
                    except:
                        stock_info = json.loads(data_str)

                    stocks3.append({
                        'code': stock_info.get('code', code.decode('utf-8') if isinstance(code, bytes) else code),
                        'name': stock_info.get('name', ''),
                        'yin_count': stock_info.get('yin_count', stock_info.get('yin_line_count', 0)),
                        'today_change': stock_info.get('today_change', stock_info.get('current_gain', 0)),
                        'is_limit_up': stock_info.get('is_limit_up', stock_info.get('limit_up', False)),
                        'market_cap': stock_info.get('market_cap', 0),
                        'weak_yin_count': stock_info.get('weak_yin_count', 0),
                        'weak_yin_group': stock_info.get('weak_yin_group', ''),
                        'no_up_down_kill_ratio': stock_info.get('no_up_down_kill_ratio', 0),
                        'max_drawdown_20': stock_info.get('max_drawdown_20', None)
                    })
                except Exception as e:
                    print(f"解析yx2052股票数据失败 {code}: {e}")
                    continue

            if not is_today and stocks3:
                print(f"📅 查看历史日期 {date}，为yx2052数据从Tushare获取历史涨跌幅")
                for stock in stocks3:
                    try:
                        code = stock['code']
                        if code.startswith('6'):
                            ts_code = f"{code}.SH"
                        elif code.startswith('0') or code.startswith('3'):
                            ts_code = f"{code}.SZ"
                        elif code.startswith('8') or code.startswith('4'):
                            ts_code = f"{code}.BJ"
                        else:
                            ts_code = f"{code}.SZ"
                        df = pro.daily(ts_code=ts_code, start_date=today_compact, end_date=today_compact)
                        if not df.empty:
                            stock['today_change'] = float(df.iloc[0]['pct_chg'])
                    except Exception as e:
                        print(f"  获取{code}历史涨跌幅失败: {e}")
                        pass

        # 返回性能指标
        return jsonify({
            'success': True,
            'data': stocks,
            'data2': stocks2,
            'data3': stocks3,
            'scan_info': _yx205_pick_latest_scan_info(scan_info, scan_info2, scan_info3),
            'performance': {
                'total_time': f"{total_time:.2f}ms",
                'network_latency': f"{network_latency:.2f}ms",
                'scan_time': f"{scan_time:.2f}ms",
                'parse_time': f"{parse_time:.2f}ms",
                'count': len(stocks),
                'count2': len(stocks2),
                'count3': len(stocks3)
            }
        })
    except Exception as e:
        print(f"获取20阴少跌少横盘数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/jpgz/yx2051/stocks')
@app.route('/api/jpgz/yx2051/stocks/<date>')
def get_jpgz_yx2051_stocks(date=None):
    """获取极品关注-20阴少5组股票列表（从yx2051获取）"""
    try:
        import base64
        import time as time_module
        from flask import request
        
        start_time = time_module.time()
        r = get_redis_client()
        
        # 如果没有指定日期，使用今天
        if not date:
            date = request.args.get('date')
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
        
        today = date
        today_compact = date.replace('-', '')
        
        # Redis key格式: yx2051:20260127
        key = f"yx2051:{today_compact}"
        
        network_start = time_module.time()
        r.ping()
        network_latency = (time_module.time() - network_start) * 1000
        
        key_type = r.type(key) if r.exists(key) else None
        
        if not key_type:
            return jsonify({
                'success': True,
                'data': [],
                'performance': {
                    'total_time': f"{(time_module.time() - start_time) * 1000:.2f}ms",
                    'count': 0
                }
            })
        
        # 获取数据
        scan_start = time_module.time()
        all_keys = []
        
        if key_type == 'hash':
            cursor = 0
            while True:
                cursor, keys = r.hscan(key, cursor, count=100)
                all_keys.extend(keys.items())
                if cursor == 0:
                    break
        elif key_type == 'string':
            data_str = r.get(key)
            if data_str:
                try:
                    data_list = json.loads(data_str)
                    for item in data_list:
                        code = item.get('code', '')
                        all_keys.append((code, json.dumps(item, ensure_ascii=False)))
                except:
                    pass
        
        scan_time = (time_module.time() - scan_start) * 1000
        
        if not all_keys:
            return jsonify({
                'success': True,
                'data': [],
                'performance': {
                    'total_time': f"{(time_module.time() - start_time) * 1000:.2f}ms",
                    'count': 0
                }
            })
        
        # 解析数据
        parse_start = time_module.time()
        stocks = []
        
        for code, data_str in all_keys:
            try:
                try:
                    decoded_data = base64.b64decode(data_str).decode('utf-8')
                    stock_info = json.loads(decoded_data)
                except:
                    stock_info = json.loads(data_str)
                
                today_change = stock_info.get('today_change', stock_info.get('current_gain', 0))
                yin_count = stock_info.get('yin_count', stock_info.get('yin_line_count', 0))
                weak_yin_count = stock_info.get('weak_yin_count', 0)
                weak_yin_group = stock_info.get('weak_yin_group', '')
                no_up_down_kill_ratio = stock_info.get('no_up_down_kill_ratio', 0)
                market_cap = stock_info.get('market_cap', 0)
                is_limit_up = stock_info.get('is_limit_up', stock_info.get('limit_up', False))
                total_gain_20 = stock_info.get('total_gain_20', 0)
                
                stocks.append({
                    'code': stock_info.get('code', code.decode('utf-8') if isinstance(code, bytes) else code),
                    'name': stock_info.get('name', ''),
                    'yin_count': yin_count,
                    'weak_yin_count': weak_yin_count,
                    'weak_yin_group': weak_yin_group,
                    'no_up_down_kill_ratio': no_up_down_kill_ratio,
                    'today_change': today_change,
                    'is_limit_up': is_limit_up,
                    'market_cap': market_cap,
                    'total_gain_20': total_gain_20
                })
            except Exception as e:
                print(f"解析yx2051股票数据失败 {code}: {e}")
                continue
        
        parse_time = (time_module.time() - parse_start) * 1000
        
        # 如果查看历史日期，从Tushare获取历史涨跌幅
        is_today = (date == datetime.now().strftime('%Y-%m-%d'))
        if not is_today and stocks:
            print(f"📅 极品关注-查看历史日期 {date}，从Tushare获取历史涨跌幅")
            
            pro = ts.pro_api()
            for stock in stocks:
                try:
                    code = stock['code']
                    if code.startswith('6'):
                        ts_code = f"{code}.SH"
                    elif code.startswith('0') or code.startswith('3'):
                        ts_code = f"{code}.SZ"
                    elif code.startswith('8') or code.startswith('4'):
                        ts_code = f"{code}.BJ"
                    else:
                        ts_code = f"{code}.SZ"
                    
                    df = pro.daily(ts_code=ts_code, start_date=today_compact, end_date=today_compact)
                    if not df.empty:
                        stock['today_change'] = float(df.iloc[0]['pct_chg'])
                except Exception as e:
                    print(f"  获取{code}历史涨跌幅失败: {e}")
                    pass
        
        total_time = (time_module.time() - start_time) * 1000
        
        return jsonify({
            'success': True,
            'data': stocks,
            'performance': {
                'total_time': f"{total_time:.2f}ms",
                'network_latency': f"{network_latency:.2f}ms",
                'scan_time': f"{scan_time:.2f}ms",
                'parse_time': f"{parse_time:.2f}ms",
                'count': len(stocks)
            }
        })
    except Exception as e:
        print(f"获取极品关注yx2051数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/jpgz/yx2051/realtime', methods=['POST'])
def get_jpgz_yx2051_realtime():
    """极品关注-20阴少5组实时涨跌幅接口"""
    try:
        from flask import request
        data = request.get_json()
        codes = data.get('codes', [])
        
        if not codes:
            return jsonify({'success': True, 'data': {}})
        
        # 获取实时行情
        realtime_data = {}
        for code in codes:
            try:
                # 转换股票代码格式用于腾讯接口
                if code.startswith('6'):
                    tencent_code = f"sh{code}"
                else:
                    tencent_code = f"sz{code}"
                
                url = f"http://qt.gtimg.cn/q={tencent_code}"
                response = requests.get(url, timeout=2)
                response.encoding = 'gbk'
                
                if response.text:
                    match = response.text.strip().split('~')
                    if len(match) > 3:
                        current_price = float(match[3]) if match[3] else 0
                        pre_close = float(match[4]) if match[4] else 0
                        if pre_close > 0:
                            change = ((current_price - pre_close) / pre_close) * 100
                            realtime_data[code] = {'change': round(change, 2)}
            except Exception as e:
                print(f"获取股票{code}实时行情失败: {e}")
                continue
        
        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        print(f"获取实时行情失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

# ========== 极品关注手动管理API ==========
JPGZ_REDIS_KEY = 'jpgz:manual'

@app.route('/api/jpgz/manual/stocks')
def get_jpgz_manual_stocks():
    """获取手动添加的极品关注股票列表"""
    try:
        r = get_redis_client()
        data = r.hgetall(JPGZ_REDIS_KEY)
        stocks = []
        for code, info_str in data.items():
            code = code.decode('utf-8') if isinstance(code, bytes) else code
            try:
                info = json.loads(info_str.decode('utf-8') if isinstance(info_str, bytes) else info_str)
            except Exception:
                info = {}
            info['code'] = code
            stocks.append(info)
        return jsonify({'success': True, 'data': stocks})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/jpgz/manual/add', methods=['POST'])
def add_jpgz_manual_stock():
    """手动添加股票到极品关注"""
    try:
        from flask import request as req
        data = req.get_json()
        code = data.get('code', '').strip()
        if not code:
            return jsonify({'success': False, 'error': '股票代码不能为空'})
        info = {
            'code': code,
            'name': data.get('name', ''),
            'add_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'source': data.get('source', ''),
        }
        r = get_redis_client()
        r.hset(JPGZ_REDIS_KEY, code, json.dumps(info, ensure_ascii=False))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/jpgz/manual/delete', methods=['POST'])
def delete_jpgz_manual_stock():
    """从极品关注删除股票"""
    try:
        from flask import request as req
        data = req.get_json()
        code = data.get('code', '').strip()
        if not code:
            return jsonify({'success': False, 'error': '股票代码不能为空'})
        r = get_redis_client()
        r.hdel(JPGZ_REDIS_KEY, code)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/yx205/check_update')
def check_yx205_update():
    """检查20阴少跌少横盘Redis数据是否更新"""
    try:
        import time as time_module
        start_time = time_module.time()
        
        r = get_redis_client()
        today_compact = datetime.now().strftime('%Y%m%d')  # 20260127格式
        
        # Redis key格式: yx205:20260127
        key = f"yx205:{today_compact}"
        timestamp_key = f"yx205:{today_compact}:timestamp"
        
        # 使用Pipeline批量检查
        pipe = r.pipeline()
        pipe.get(timestamp_key)
        pipe.exists(key)
        results = pipe.execute()
        
        timestamp = results[0]
        key_exists = results[1]
        
        if key_exists:
            # 获取数据数量
            key_type = r.type(key)
            if key_type == 'string':
                try:
                    data_str = r.get(key)
                    data_list = json.loads(data_str)
                    data_count = len(data_list)
                except:
                    data_count = 0
            else:
                # hash类型
                data_count = r.hlen(key)
            
            if data_count > 0:
                # 如果没有时间戳，创建一个
                if not timestamp:
                    import time
                    timestamp = str(int(time.time() * 1000))
                    r.set(timestamp_key, timestamp)
                
                return jsonify({
                    'success': True, 
                    'updated': True,
                    'timestamp': timestamp,
                    'count': data_count,
                    'refresh_interval': get_redis_refresh_interval()
                })
        
        return jsonify({
            'success': True, 
            'updated': False, 
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/yx205/dates')
def get_yx205_dates():
    """获取20阴少跌少横盘所有有数据的日期"""
    try:
        r = get_redis_client()
        
        # 查找所有相关的key: yx205:YYYYMMDD
        keys = r.keys('yx205:*')
        
        dates = []
        
        # 处理key格式: yx205:20260127
        for key in keys:
            if ':' in key and not key.endswith(':timestamp'):
                date_part = key.split(':', 1)[1]
                # 转换20260127 -> 2026-01-27
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        
        # 去重并排序
        dates = sorted(list(set(dates)), reverse=True)
        
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/market_cap/<code>')
def get_market_cap(code):
    """获取单个股票的市值（通用接口，使用Tushare）"""
    try:
        import tushare as ts
        
        # 转换股票代码格式
        if code.startswith('6'):
            ts_code = f"{code}.SH"
        elif code.startswith('0') or code.startswith('3'):
            ts_code = f"{code}.SZ"
        elif code.startswith('8') or code.startswith('4'):
            ts_code = f"{code}.BJ"
        else:
            ts_code = f"{code}.SZ"
        
        # 使用Tushare获取市值
        pro = ts.pro_api()
        df = pro.daily_basic(ts_code=ts_code, fields='ts_code,total_mv')
        
        if df is not None and not df.empty:
            # total_mv单位是万元，转换为亿元
            market_cap = float(df.iloc[0]['total_mv']) / 10000
            return jsonify({
                'success': True,
                'code': code,
                'market_cap': round(market_cap, 2)
            })
        else:
            return jsonify({
                'success': False,
                'error': '未获取到市值数据',
                'market_cap': 0
            })
    except Exception as e:
        print(f"获取市值失败 {code}: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'market_cap': 0
        })

@app.route('/api/yx205/realtime', methods=['POST'])
def get_yx205_realtime():
    """实时涨跌幅接口 - 批量请求腾讯行情"""
    try:
        import time as time_module
        from flask import request
        data = request.get_json()
        codes = data.get('codes', [])

        if not codes:
            return jsonify({'success': True, 'data': {}})

        code_map = {}  # tencent_code -> original_code
        for code in codes:
            tc = _to_tencent_quote_code(code)
            if not tc:
                continue
            code_map[tc] = code

        start_time = time_module.time()
        request_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        realtime_data = {}
        raw_line_count = 0
        semicolon_entry_count = 0
        chunk_debug = []
        code_items = list(code_map.items())
        print(f"[yx205][realtime] start at={request_at} codes={len(codes)} sample={codes[:5]} chunks={(len(code_items) + TENCENT_QUOTE_CHUNK_SIZE - 1) // TENCENT_QUOTE_CHUNK_SIZE}")
        for chunk_index, chunk_items in enumerate(_chunked(code_items), start=1):
            chunk_map = dict(chunk_items)
            batch = ','.join(chunk_map.keys())
            url = f"http://qt.gtimg.cn/q={batch}"
            chunk_start = time_module.time()
            response = requests.get(url, timeout=10)
            response.encoding = 'gbk'
            chunk_elapsed_ms = (time_module.time() - chunk_start) * 1000
            raw_text = response.text.strip()
            raw_lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
            semicolon_entries = [line.strip() for line in raw_text.split(';') if line.strip()]
            entries = semicolon_entries if semicolon_entries else raw_lines
            raw_line_count += len(raw_lines)
            semicolon_entry_count += len(semicolon_entries)
            chunk_debug.append({
                'chunk': chunk_index,
                'count': len(chunk_items),
                'url_len': len(url),
                'raw_lines': len(raw_lines),
                'semicolon_entries': len(semicolon_entries),
                'elapsed_ms': round(chunk_elapsed_ms, 2),
            })
            print(
                f"[yx205][realtime] chunk={chunk_index} count={len(chunk_items)} "
                f"url_len={len(url)} raw_lines={len(raw_lines)} "
                f"semicolon_entries={len(semicolon_entries)} elapsed_ms={chunk_elapsed_ms:.2f} "
                f"sample={entries[:2]}"
            )

            for line in entries:
                try:
                    # 提取 tencent_code：v_sh600000="..." 中的 sh600000
                    tc = line.split('=')[0].replace('v_', '').strip()
                    fields = line.split('~')
                    if len(fields) > 4:
                        current_price = float(fields[3]) if fields[3] else 0
                        pre_close = float(fields[4]) if fields[4] else 0
                        if pre_close > 0 and tc in code_map:
                            change = round((current_price - pre_close) / pre_close * 100, 2)
                            orig_code = code_map[tc]
                            print(
                                f"[yx205][realtime] parsed code={orig_code} tencent_code={tc} "
                                f"current={current_price} pre_close={pre_close} change={change}"
                            )
                            realtime_data[orig_code] = {'change': change}
                except Exception as parse_error:
                    print(f"[yx205][realtime] parse_failed line={line} error={parse_error}")
                    continue

        elapsed_ms = (time_module.time() - start_time) * 1000
        print(
            f"[yx205][realtime] done count={len(realtime_data)} raw_lines={raw_line_count} "
            f"semicolon_entries={semicolon_entry_count} elapsed_ms={elapsed_ms:.2f} "
            f"chunks={len(chunk_debug)} sample={dict(list(realtime_data.items())[:3])}"
        )

        return jsonify({'success': True, 'data': realtime_data, 'debug': {
            'requested_count': len(codes),
            'batch_count': len(code_map),
            'chunk_count': len(chunk_debug),
            'chunk_size': TENCENT_QUOTE_CHUNK_SIZE,
            'parsed_count': len(realtime_data),
            'raw_lines': raw_line_count,
            'semicolon_entries': semicolon_entry_count,
            'chunks': chunk_debug[:3] + (chunk_debug[-1:] if len(chunk_debug) > 3 else []),
        }})
    except Exception as e:
        print(f"[yx205][realtime] failed error={e}")
        return jsonify({'success': False, 'error': str(e)})


# ========== 阴线跌破5日次数页面相关API ==========
def _yinbreak5_parse_float(value, default=0.0):
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _yinbreak5_parse_int(value, default=0):
    try:
        if value is None or value == '':
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _yinbreak5_decode_payload(data_str):
    import base64

    if isinstance(data_str, bytes):
        data_str = data_str.decode('utf-8', errors='ignore')
    try:
        decoded = base64.b64decode(data_str).decode('utf-8')
        return json.loads(decoded)
    except Exception:
        return json.loads(data_str)


def _yinbreak5_normalize_date(date_value=None):
    raw = str(date_value or '').strip()
    if not raw:
        raw = datetime.now().strftime('%Y-%m-%d')
    compact = raw.replace('-', '')
    if len(compact) != 8 or not compact.isdigit():
        compact = datetime.now().strftime('%Y%m%d')
    display = f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
    return compact, display


def _yinbreak5_normalize_stock(code, info):
    raw_code = info.get('code') or code
    simple_code = _normalize_stock_code_text(raw_code)
    today_change = _yinbreak5_parse_float(info.get('today_change', info.get('ref_gain', 0)), 0.0)
    ref_high_gain_ok = 1 if info.get('ref_high_gain_ok') in (True, 1, '1', 'true', 'True') else 0
    ref_low_above_ma5 = _yinbreak5_parse_int(info.get('ref_low_above_ma5'), 0)
    ref_high_gain_no_touch = 1 if (ref_high_gain_ok and ref_low_above_ma5) else 0
    return {
        'code': simple_code or raw_code,
        'name': info.get('name', ''),
        'stock_code_with_market': info.get('stock_code_with_market', ''),
        'board_type': info.get('board_type', ''),
        'limit_ratio': _yinbreak5_parse_float(info.get('limit_ratio'), 0.0),
        'ref_gain': _yinbreak5_parse_float(info.get('ref_gain'), 0.0),
        'today_change': round(today_change, 2),
        'max_gain': _yinbreak5_parse_float(info.get('max_gain'), 0.0),
        'is_limit_up': _yinbreak5_parse_int(info.get('is_limit_up'), 0),
        'ref_is_limit_up': 1 if info.get('ref_is_limit_up') in (True, 1, '1', 'true', 'True') else 0,
        'is_ref_yang': _yinbreak5_parse_int(info.get('is_ref_yang'), 0),
        'ref_high_gain_ok': ref_high_gain_ok,
        'ref_high_gain_gte_6_5_and_yang': 1 if info.get('ref_high_gain_gte_6_5_and_yang') in (True, 1, '1', 'true', 'True') else 0,
        'ref_low_above_ma5': ref_low_above_ma5,
        'ref_high_gain_gte_6_5_yang_low_above_ma5': ref_high_gain_no_touch,
        'is_bare_foot': _yinbreak5_parse_int(info.get('is_bare_foot'), 0),
        'is_bare_foot_yang': _yinbreak5_parse_int(info.get('is_bare_foot_yang'), 0),
        'is_bare_head': _yinbreak5_parse_int(info.get('is_bare_head', info.get('is_nearly_bare_head')), 0),
        'is_nearly_bare_head': _yinbreak5_parse_int(info.get('is_nearly_bare_head', info.get('is_bare_head')), 0),
        'is_short_head_foot_doji': _yinbreak5_parse_int(info.get('is_short_head_foot_doji'), 0),
        'ref_open': _yinbreak5_parse_float(info.get('ref_open'), 0.0),
        'ref_close': _yinbreak5_parse_float(info.get('ref_close'), 0.0),
        'ref_high': _yinbreak5_parse_float(info.get('ref_high'), 0.0),
        'ref_low': _yinbreak5_parse_float(info.get('ref_low'), 0.0),
        'ref_prev_close': _yinbreak5_parse_float(info.get('ref_prev_close'), 0.0),
        'max_close_20': _yinbreak5_parse_float(info.get('max_close_20'), 0.0),
        'min_close_20': _yinbreak5_parse_float(info.get('min_close_20'), 0.0),
        'gain_20d': _yinbreak5_parse_float(info.get('gain_20d'), 0.0),
        'limit_up_count_20': _yinbreak5_parse_int(info.get('limit_up_count_20'), 0),
        'yin_count_20': _yinbreak5_parse_int(info.get('yin_count_20'), 0),
        'yin_count_10d': _yinbreak5_parse_int(info.get('yin_count_10d'), 0),
        'weak_yin_20d': _yinbreak5_parse_int(info.get('weak_yin_20d'), 0),
        'weak_yin_count': _yinbreak5_parse_int(info.get('weak_yin_20d'), 0),
        'yin_close_below_ma5_count_10d': _yinbreak5_parse_int(info.get('yin_close_below_ma5_count_10d'), 0),
        'yin_close_below_ma5_lte_2_10d': 1 if info.get('yin_close_below_ma5_lte_2_10d') in (True, 1, '1', 'true', 'True') else 0,
        'yin_body_pct_sum_10d': _yinbreak5_parse_float(info.get('yin_body_pct_sum_10d'), 0.0),
        'high_gain_10d_count': _yinbreak5_parse_int(info.get('high_gain_10d_count'), 0),
        'high_gain_10d_gte_5': _yinbreak5_parse_int(info.get('high_gain_10d_gte_5'), 0),
        'close_above_ma5_count_10d': _yinbreak5_parse_int(info.get('close_above_ma5_count_10d', info.get('close_above_ma5_10d_count')), 0),
        'close_above_ma5_10d_count': _yinbreak5_parse_int(info.get('close_above_ma5_10d_count', info.get('close_above_ma5_count_10d')), 0),
        'low_above_ma5_count': _yinbreak5_parse_int(info.get('low_above_ma5_count'), 0),
        'almost_bare_foot_yang_count': _yinbreak5_parse_int(info.get('almost_bare_foot_yang_count', info.get('bare_foot_yang_count_10d')), 0),
        'bare_foot_yang_count_10d': _yinbreak5_parse_int(info.get('bare_foot_yang_count_10d', info.get('almost_bare_foot_yang_count')), 0),
        'yin_count_10': _yinbreak5_parse_int(info.get('yin_count_10', info.get('yin_count_10d')), 0),
    }


@app.route('/阴线跌破5日次数')
def yinbreak5_page():
    """阴线跌破5日次数页面"""
    return render_template('yinbreak5.html')


@app.route('/yd')
def yd_page():
    """移动端聚合页面"""
    return render_template('yd.html')


@app.route('/10天大5日线')
def ten_above5_page():
    """10天大5日线页面"""
    return render_template('ten_above5.html')


def _render_special_focus_page(
    renderer,
    page_title,
    dates_url='/api/yxcs/dates',
    klines_url='/api/yxcs/klines',
    aad='yxcs-klines:v1',
    group_mode='yin',
    nav_mode='renderer',
    loading_label='20日阴线次数数据',
    encrypted_payload=True,
    yin_group_field='yin_count_20',
    yin_group_label='20日',
):
    """Render a special-focus page with encrypted or plaintext data."""
    response = make_response(render_template(
        'special_focus.html',
        special_focus_renderer=renderer,
        special_focus_title=page_title,
        special_focus_key_b64=(
            str(os.environ.get('KLINE_API_AES_KEY_B64') or '').strip()
            if encrypted_payload else ''
        ),
        special_focus_dates_url=dates_url,
        special_focus_klines_url=klines_url,
        special_focus_aad=aad,
        special_focus_group_mode=group_mode,
        special_focus_nav_mode=nav_mode,
        special_focus_loading_label=loading_label,
        special_focus_encrypted_payload=bool(encrypted_payload),
        special_focus_yin_group_field=yin_group_field,
        special_focus_yin_group_label=yin_group_label,
    ))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.route('/特别关注')
@app.route('/特别关注1')
def special_focus_canvas_page():
    return _render_special_focus_page(
        'canvas',
        '特别关注1',
        dates_url='/api/pz/dates',
        klines_url='/api/pz/klines',
        encrypted_payload=False,
    )


@app.route('/特别关注2')
def special_focus_echarts_page():
    return _render_special_focus_page('echarts', '特别关注2')


@app.route('/特别关注3')
def special_focus_svg_page():
    return _render_special_focus_page('svg', '特别关注3')


@app.route('/特别板块')
def special_focus_board_page():
    return _render_special_focus_page(
        'canvas',
        '特别板块',
        dates_url='/api/ths/dates',
        klines_url='/api/ths/klines/plain',
        aad='ths-klines:v1',
        group_mode='board',
        nav_mode='board',
        loading_label='同花顺清洗板块数据',
        encrypted_payload=False,
    )


def _render_pz_five_pull_page(page_title, condition_no):
    return _render_special_focus_page(
        'canvas',
        page_title,
        dates_url='/api/pz/dates',
        klines_url=f'/api/pz/condition{condition_no}/klines',
        group_mode='yin',
        nav_mode=f'pz-condition-{condition_no}',
        loading_label='PZ盘中筛选数据',
        encrypted_payload=False,
        yin_group_field='yin_count_10d',
        yin_group_label='10日',
    )


@app.route('/5拉收盘大于10日')
def special_focus_pz_condition3_page():
    return _render_pz_five_pull_page('5拉收盘大于10日', 3)


@app.route('/5拉阴线都向上')
def special_focus_pz_condition5_page():
    return _render_pz_five_pull_page('5拉阴线都向上', 5)


@app.route('/5拉阴被吃')
def special_focus_pz_condition8_page():
    return _render_pz_five_pull_page('5拉阴被吃', 8)


@app.route('/5拉阴线后拉')
@app.route('/5拉阴后拉或吃')
def special_focus_pz_condition7_page():
    return _render_pz_five_pull_page('5拉阴后拉或吃', 7)


@app.route('/5拉阴线都是十字星')
def special_focus_pz_condition6_page():
    return _render_pz_five_pull_page('5拉阴线都是十字星', 6)


@app.route('/5拉阴都不碰')
def special_focus_pz_condition9_page():
    return _render_pz_five_pull_page('5拉阴都不碰', 9)


@app.route('/特别关注板块')
def legacy_special_focus_board_page():
    return redirect(url_for('special_focus_board_page'))


@app.route('/api/yinbreak5/dates')
def get_yinbreak5_dates():
    """获取阴线跌破5日次数所有有数据的日期"""
    try:
        r = get_redis_client()
        dates = set()
        for key in r.keys('20dpnum:*'):
            key_str = key.decode('utf-8', errors='ignore') if isinstance(key, bytes) else str(key)
            parts = key_str.split(':')
            if len(parts) != 2:
                continue
            date_part = parts[1]
            if len(date_part) == 8 and date_part.isdigit():
                dates.add(f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}")
        return jsonify({'success': True, 'dates': sorted(dates, reverse=True)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/yinbreak5/stocks')
@app.route('/api/yinbreak5/stocks/<date>')
def get_yinbreak5_stocks(date=None):
    """读取20dpnum:{date}，按weak_yin_20d升序、涨跌幅降序返回"""
    try:
        if not date:
            date = request.args.get('date')
        date_compact, display_date = _yinbreak5_normalize_date(date)
        key = f"20dpnum:{date_compact}"
        r = get_redis_client()

        if not r.exists(key) or r.type(key) != 'hash':
            return no_cache_response({
                'success': True,
                'data': [],
                'date': display_date,
                'count': 0,
                'scan_info': None,
                'message': f'没有找到数据key: {key}'
            })

        rows = []
        scan_info = None
        cursor = 0
        while True:
            cursor, batch = r.hscan(key, cursor, count=200)
            for code, data_str in batch.items():
                try:
                    if isinstance(code, bytes):
                        code = code.decode('utf-8', errors='ignore')
                    if code in ('_scan_info', '_timestamp', '_snapshot_info'):
                        if code == '_scan_info':
                            scan_info = _yinbreak5_decode_payload(data_str)
                        continue
                    info = _yinbreak5_decode_payload(data_str)
                    if isinstance(info, dict):
                        rows.append(_yinbreak5_normalize_stock(code, info))
                except Exception as item_error:
                    print(f"[yinbreak5] 解析股票数据失败 code={code} error={item_error}", flush=True)
            if cursor == 0:
                break

        rows.sort(key=lambda item: (item.get('weak_yin_20d', 0), -item.get('today_change', 0)))
        return no_cache_response({
            'success': True,
            'data': rows,
            'date': display_date,
            'count': len(rows),
            'redis_key': key,
            'scan_info': scan_info,
            'update_time': datetime.now().strftime('%H:%M:%S')
        })
    except Exception as e:
        print(f"[yinbreak5] 获取数据失败: {e}", flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': []})


@app.route('/api/yinbreak5/realtime', methods=['POST'])
def get_yinbreak5_realtime():
    """阴线跌破5日次数实时涨跌幅接口"""
    try:
        data = request.get_json() or {}
        codes = data.get('codes', [])
        quotes = get_tencent_batch_quotes(codes)
        realtime_data = {}
        for code in codes:
            simple_code = _normalize_stock_code_text(code)
            quote = quotes.get(simple_code)
            if quote:
                realtime_data[code] = {
                    'change': round(_yinbreak5_parse_float(quote.get('change_pct'), 0.0), 2),
                    'price': quote.get('price', 0)
                }
        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        print(f"[yinbreak5][realtime] 获取失败: {e}", flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': {}})


@app.route('/api/10above5/dates')
def get_10above5_dates():
    """获取10天大5日线所有有数据的日期"""
    try:
        r = get_redis_client()
        dates = set()
        for key in r.keys('10above5:*'):
            key_str = key.decode('utf-8', errors='ignore') if isinstance(key, bytes) else str(key)
            parts = key_str.split(':')
            if len(parts) != 2:
                continue
            date_part = parts[1]
            if len(date_part) == 8 and date_part.isdigit():
                dates.add(f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}")
        return jsonify({'success': True, 'dates': sorted(dates, reverse=True)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/yxcs/dates')
def get_yxcs_dates():
    """获取20日阴线次数所有有数据的日期。"""
    try:
        r = get_redis_client()
        dates = set()
        for key in r.keys('yxcs:*'):
            key_str = key.decode('utf-8', errors='ignore') if isinstance(key, bytes) else str(key)
            parts = key_str.split(':')
            if len(parts) != 2:
                continue
            date_part = parts[1]
            if len(date_part) == 8 and date_part.isdigit():
                dates.add(f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}")
        return jsonify({'success': True, 'dates': sorted(dates, reverse=True)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/special5/dates')
def get_special5_dates():
    """获取特别关注5全市场结果的可用日期。"""
    try:
        r = get_redis_client()
        dates = set()
        for key_pattern in ('special5:*', '4above5:*'):
            for key in r.keys(key_pattern):
                key_str = key.decode('utf-8', errors='ignore') if isinstance(key, bytes) else str(key)
                parts = key_str.split(':')
                if len(parts) != 2:
                    continue
                date_part = parts[1]
                if len(date_part) == 8 and date_part.isdigit():
                    dates.add(f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}")
        return jsonify({'success': True, 'dates': sorted(dates, reverse=True)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/20xiasha/dates')
def get_20xiasha_dates():
    """明文返回20日下穿MA5累计数据的可用日期。"""
    try:
        r = get_redis_client()
        dates = set()
        for key in r.keys('20xiasha:*'):
            key_str = key.decode('utf-8', errors='ignore') if isinstance(key, bytes) else str(key)
            parts = key_str.split(':')
            if len(parts) != 2:
                continue
            date_part = parts[1]
            if len(date_part) != 8 or not date_part.isdigit():
                continue
            try:
                parsed_date = datetime.strptime(date_part, '%Y%m%d')
            except ValueError:
                continue
            dates.add(parsed_date.strftime('%Y-%m-%d'))
        return jsonify({'success': True, 'dates': sorted(dates, reverse=True)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/5lsabove10/dates')
def get_5lsabove10_dates():
    """明文返回5lsabove10可用日期，按最新日期在前排列。"""
    try:
        r = get_redis_client()
        dates = set()
        for key in r.keys('5lsabove10:*'):
            key_str = key.decode('utf-8', errors='ignore') if isinstance(key, bytes) else str(key)
            parts = key_str.split(':')
            if len(parts) != 2:
                continue
            date_part = parts[1]
            if len(date_part) != 8 or not date_part.isdigit():
                continue
            try:
                parsed_date = datetime.strptime(date_part, '%Y%m%d')
            except ValueError:
                continue
            dates.add(parsed_date.strftime('%Y-%m-%d'))
        return jsonify({'success': True, 'dates': sorted(dates, reverse=True)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/推送日志')
def pz_push_log_page():
    """Render the successful DingTalk push history page."""
    return render_template('pz_push_log.html', pz_page_mode='push_log')


@app.route('/重点关注')
def pz1_focus_page():
    """Render the six-page PZ/PZ1/QY1/QY2/THS focus view."""
    return render_template('pz_push_log.html', pz_page_mode='pz1_focus')


# 顶部“复盘”和“财联社涨停分析”两个独立 Redis 表格页。
# 这里不读取飞书，也不调用飞书 API；复盘页直接组合 Redis 快照，
# 财联社页由收盘分析程序把本地结果缓存到 Redis 后读取。
_REVIEW_BUILDER_MODULE = None
_REVIEW_BUILDER_MODULE_LOCK = threading.Lock()
_FOCUS_EDIT_TTL = 30 * 24 * 60 * 60
_FOCUS_CLS_REDIS_KEY = 'cls:limit_up:{date}'


def _load_review_builder_module():
    global _REVIEW_BUILDER_MODULE
    if _REVIEW_BUILDER_MODULE is not None:
        return _REVIEW_BUILDER_MODULE
    with _REVIEW_BUILDER_MODULE_LOCK:
        if _REVIEW_BUILDER_MODULE is None:
            candidates = []
            configured = os.environ.get('FEISHU_DAILY_REVIEW_SCRIPT')
            if configured:
                candidates.append(Path(configured))
            candidates.extend([
                Path(__file__).resolve().parent / 'feishu_daily_review.py',
                Path(__file__).resolve().parents[1] / 'feishu_daily_review.py',
                Path.cwd() / 'feishu_daily_review.py',
            ])
            module_path = next((path for path in candidates if path.exists()), None)
            if module_path is not None:
                spec = importlib.util.spec_from_file_location(
                    'redis_daily_review_web_builder', module_path
                )
                if spec is not None and spec.loader is not None:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    _REVIEW_BUILDER_MODULE = module
            if _REVIEW_BUILDER_MODULE is None:
                # 发布环境可能只部署 python_web 目录。网页仍需能够直接用 Redis
                # 生成复盘表格，因此这里提供一个不依赖飞书脚本的最小构建器。
                _REVIEW_BUILDER_MODULE = _make_redis_review_fallback_builder()
    return _REVIEW_BUILDER_MODULE


def _make_redis_review_fallback_builder():
    """构造发布环境可用的 Redis 复盘表格生成器。"""
    def text_value(value):
        if isinstance(value, list):
            return ''.join(
                str(item.get('text') or '') if isinstance(item, dict) else str(item or '')
                for item in value
            )
        return '' if value is None else str(value)

    def normalize_code(value):
        digits = ''.join(char for char in str(value or '') if char.isdigit())
        return digits[-6:] if len(digits) >= 6 else ''

    def number(stock, *names, default=0.0):
        for name in names:
            try:
                value = stock.get(name)
                if value not in (None, ''):
                    return float(value)
            except (TypeError, ValueError):
                pass
        return default

    def integer(stock, *names, default=0):
        return int(number(stock, *names, default=default))

    def truthy(value):
        return str(value).strip().lower() in {'1', 'true', 'yes', 'y'}

    def stock_cell(stock):
        name = str(stock.get('name') or stock.get('code') or '').strip()
        code = normalize_code(stock.get('code') or stock.get('stock_code_with_market'))
        gain = number(stock, 'today_change', 'ref_gain')
        color = '#D93025' if gain > 0 else '#188038' if gain < 0 else '#222222'
        return [
            {'type': 'text', 'text': f'{name}  {code}  ',
             'segmentStyle': {'bold': True, 'foreColor': '#222222'}},
            {'type': 'text', 'text': f'{gain:+.2f}%',
             'segmentStyle': {'bold': False, 'foreColor': color}},
        ]

    def normalize_rows(values):
        return [list(row) if isinstance(row, list) else [row] for row in values or []]

    def build_sheet_content(ths_payload, stocks, manual_blocks=None,
                            qy2_stocks=None, related_pz_stocks=None):
        width = 8
        rows = []
        layout = {
            'major_titles': [], 'sub_titles': [], 'plain_headers': [],
            'red_headers': [], 'board_hot_rows': [], 'up_cells': [],
            'down_cells': [], 'regions': [],
        }

        def add(values, style=None):
            row = list(values)[:width] + [''] * max(0, width - len(values))
            rows.append(row)
            if style:
                layout.setdefault(style, []).append(len(rows))

        def blank(count=2):
            rows.extend([[''] * width for _ in range(count)])

        boards = []
        for raw in (ths_payload or {}).get('boards') or []:
            if not isinstance(raw, dict):
                continue
            boards.append(raw)
        boards.sort(key=lambda item: (
            number(item, 'rank', default=999999),
            -number(item, 'change_percent'),
            str(item.get('board_name') or ''),
        ))
        add(['同花顺板块'], 'major_titles')
        add(['板块名称', '板块涨跌幅', '板块涨停家数'], 'plain_headers')
        for board in boards:
            gain = number(board, 'change_percent')
            limit_count = integer(board, 'limit_up_count')
            add([str(board.get('board_name') or ''), f'{gain:+.2f}%', limit_count])
            if limit_count >= 5:
                layout['board_hot_rows'].append(len(rows))

        blank()
        add(['同花顺板块相关个股'], 'major_titles')
        related = [item for item in (ths_payload or {}).get('stocks') or [] if isinstance(item, dict)]
        related.sort(key=lambda item: (
            number(item, 'board_rank', default=999999),
            -number(item, 'change_percent'),
            normalize_code(item.get('code')),
        ))
        for stock in related:
            gain = number(stock, 'change_percent', 'today_change', 'ref_gain')
            add([f"{stock.get('name') or stock.get('code') or ''}  "
                 f"{normalize_code(stock.get('code'))}  {gain:+.2f}%"])

        def add_group(title, selected, count_field='yin_count_20'):
            blank()
            add([title], 'sub_titles')
            buckets = {}
            for stock in selected:
                buckets.setdefault(integer(stock, count_field, default=99), []).append(stock)
            for count in sorted(buckets):
                group = sorted(buckets[count], key=lambda item: (
                    -number(item, 'today_change', 'ref_gain'),
                    normalize_code(item.get('code')),
                ))
                add([f'{title}={count}（{len(group)}只）'])
                for stock in group:
                    add([stock_cell(stock)])

        pz1 = list(stocks or [])
        qy2 = list(qy2_stocks or [])
        add_group('1全阳', [s for s in pz1 if integer(s, 'yin_count_10d', default=99) == 0])
        add_group('阴小4（必做任务）', [s for s in pz1 if integer(s, 'yin_count_20', default=99) <= 4])
        add_group('条件1 ZDZF8(拉伸6.5)', [s for s in pz1 if truthy(s.get('ref_high_gain_ok')) or number(s, 'max_gain') >= 6.5])
        add_group('条件2 4不碰', [s for s in pz1 if truthy(s.get('continuous_4_no_touch'))])
        add_group('3.5拉升', [s for s in pz1 if any(truthy(s.get(field)) for field in (
            'five_pull_all_yin_up', 'five_pull_all_yin_cross_star',
            'five_pull_all_yin_no_touch', 'five_pull_yin_pull_next',
            'five_pull_yin_pull_next_or_eaten', 'five_pull_all_yin_eaten_next',
        ))])
        add_group('3.6涨停前稳定', [s for s in pz1 if truthy(s.get('high_gain_prev3_short_shadow'))], 'prev3_yang_count')
        add_group('4. 全阳阴线后面强势K', qy2, 'close_below_ma5_count_10d')
        blank(2)
        add(['5. 涨停复盘'], 'major_titles')
        blank(2)
        add(['Final'], 'major_titles')
        return rows, layout

    return SimpleNamespace(
        hyperlink_display_text=text_value,
        normalize_stock_code=normalize_code,
        last_nonempty_row=lambda values: next(
            (index + 1 for index in range(len(values) - 1, -1, -1)
             if any(value not in (None, '') for value in values[index])), 0
        ),
        load_daily_qy2=lambda run_date: (
            f"qy2:{run_date.strftime('%Y%m%d')}", [], 'redis'
        ),
        build_sheet_content=build_sheet_content,
        default_manual_blocks=lambda: {},
    )


def _focus_parse_date(date_text):
    try:
        return datetime.strptime(str(date_text).replace('-', ''), '%Y%m%d').date()
    except ValueError as exc:
        raise ValueError('date必须是YYYY-MM-DD格式') from exc


def _focus_decode_redis_value(raw):
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='ignore')
    text = str(raw).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        try:
            return json.loads(base64.b64decode(text).decode('utf-8'))
        except Exception:
            return None


def _focus_load_hash_stocks(redis_client, key):
    stocks = []
    for field, raw in (redis_client.hgetall(key) or {}).items():
        field_text = field.decode('utf-8', errors='ignore') if isinstance(field, bytes) else str(field)
        if field_text.startswith('_'):
            continue
        stock = _focus_decode_redis_value(raw)
        if isinstance(stock, dict):
            stocks.append(stock)
    return stocks


def _focus_text_value(value, module=None):
    if isinstance(value, list):
        if module is not None:
            return module.hyperlink_display_text(value)
        return ''.join(str(item.get('text') or '') for item in value if isinstance(item, dict))
    if isinstance(value, dict):
        return str(value.get('text') or value.get('value') or '')
    return '' if value is None else value


def _focus_rich_segments(value):
    items = value if isinstance(value, list) else [value]
    segments = []
    for item in items:
        if not isinstance(item, dict):
            continue
        nested = item.get('texts') if isinstance(item.get('texts'), list) else [item]
        for part in nested:
            if not isinstance(part, dict):
                continue
            text = str(part.get('text') or '')
            if not text:
                continue
            style = dict(item.get('segmentStyle') or {})
            style.update(part.get('segmentStyle') or {})
            segment = {'text': text}
            if style.get('foreColor'):
                segment['color'] = str(style['foreColor'])
            if style.get('bold') is not None:
                segment['bold'] = bool(style['bold'])
            if style.get('italic') is not None:
                segment['italic'] = bool(style['italic'])
            if style.get('underline') is not None:
                segment['underline'] = bool(style['underline'])
            if style.get('fontSize') is not None:
                try:
                    # Feishu reports font sizes in points.  Convert to CSS
                    # pixels so the browser has the same visual scale.
                    segment['fontSize'] = float(style['fontSize']) * 4 / 3
                except (TypeError, ValueError):
                    pass
            segments.append(segment)
    return segments


def _focus_empty_style():
    return {
        'color': '#111827', 'background': '#FFFFFF', 'bold': False,
        'italic': False, 'underline': False, 'fontSize': 11, 'align': 'left',
        'fontFamily': 'Microsoft YaHei',
    }


def _focus_style_grid(rows, layout=None):
    columns = max((len(row) for row in rows), default=1)
    styles = [[_focus_empty_style().copy() for _ in range(columns)] for _ in rows]
    layout = layout or {}

    def style_rows(row_numbers, **updates):
        for row_number in row_numbers or []:
            index = int(row_number) - 1
            if not (0 <= index < len(styles)):
                continue
            for cell in styles[index]:
                cell.update(updates)

    style_rows(layout.get('major_titles'), color='#FFFFFF', background='#8B3BC8', bold=True, fontSize=12)
    style_rows(layout.get('sub_titles'), color='#5B21B6', background='#F3E8FF', bold=True, fontSize=11)
    style_rows(layout.get('plain_headers'), color='#374151', background='#F1F2F4', bold=True, fontSize=11)
    style_rows(layout.get('red_headers'), color='#FFFFFF', background='#EF4444', bold=True, fontSize=11)
    for row_number in layout.get('board_hot_rows') or []:
        index = int(row_number) - 1
        if 0 <= index < len(styles):
            for cell in styles[index]:
                cell.update(color='#FFFFFF', background='#9B3DD5', bold=True)
    for cell_name in layout.get('up_cells') or []:
        match = re.fullmatch(r'([A-Z]+)(\d+)', str(cell_name))
        if match:
            row = int(match.group(2)) - 1
            column = 0
            for char in match.group(1):
                column = column * 26 + ord(char) - ord('A') + 1
            column -= 1
            if 0 <= row < len(styles) and 0 <= column < columns:
                styles[row][column]['color'] = '#D93025'
                styles[row][column]['bold'] = True
    for cell_name in layout.get('down_cells') or []:
        match = re.fullmatch(r'([A-Z]+)(\d+)', str(cell_name))
        if match:
            row = int(match.group(2)) - 1
            column = 0
            for char in match.group(1):
                column = column * 26 + ord(char) - ord('A') + 1
            column -= 1
            if 0 <= row < len(styles) and 0 <= column < columns:
                styles[row][column]['color'] = '#188038'
                styles[row][column]['bold'] = True
    return styles


def _focus_style_cells_from_groups(style_groups, rows, columns):
    styles = [[_focus_empty_style().copy() for _ in range(columns)] for _ in range(rows)]
    for group in style_groups or []:
        style = group.get('style') or {}
        font = style.get('font') or {}
        updates = {}
        if style.get('foreColor') is not None:
            updates['color'] = style['foreColor']
        if style.get('backColor') is not None:
            updates['background'] = style['backColor']
        font_size = str(font.get('fontSize') or '').split('pt', 1)[0]
        try:
            font_size = float(font_size) * 4 / 3
        except (TypeError, ValueError):
            font_size = None
        if font.get('bold') is not None:
            updates['bold'] = bool(font['bold'])
        if font_size is not None:
            updates['fontSize'] = font_size
        if style.get('hAlign') is not None:
            updates['align'] = {'0': 'left', '1': 'center', '2': 'right'}.get(
                str(style['hAlign']), 'left'
            )
        if style.get('vAlign') is not None:
            updates['verticalAlign'] = {'0': 'top', '1': 'middle', '2': 'bottom'}.get(
                str(style['vAlign']), 'middle'
            )
        if font.get('fontFamily'):
            updates['fontFamily'] = str(font['fontFamily'])
        for raw_range in group.get('ranges') or []:
            # Review styles are produced by style_payload() with an optional
            # sheet prefix ("!A1:H20"). CLS styles use plain A1 notation.
            range_text = str(raw_range).upper().rsplit('!', 1)[-1]
            match = re.fullmatch(r'([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?', range_text)
            if not match:
                continue
            def col_number(text):
                value = 0
                for char in text:
                    value = value * 26 + ord(char) - ord('A') + 1
                return value - 1
            start_col = col_number(match.group(1))
            start_row = int(match.group(2)) - 1
            end_col = col_number(match.group(3) or match.group(1))
            end_row = int(match.group(4) or match.group(2)) - 1
            for row in range(max(0, start_row), min(rows, end_row + 1)):
                for column in range(max(0, start_col), min(columns, end_col + 1)):
                    styles[row][column].update(updates)
    return styles


def _focus_template_review_style_groups(values, module):
    """Return the visual groups used by the current daily review template.

    The workbook is template-driven.  The old web page used the legacy
    red/purple ``style_payload`` and consequently looked different from the
    workbook even when its values were correct.  Keep this mapping based on
    the rendered template's semantic rows instead of guessing styles in JS.
    """
    rows = len(values)
    groups = []

    def add(ranges, *, color=None, background=None, bold=None, size=None,
            align='left', family='Microsoft YaHei'):
        if not ranges:
            return
        font = {'fontFamily': family, 'clean': False}
        if bold is not None:
            font['bold'] = bold
        if size is not None:
            font['fontSize'] = f'{size}pt/1.5'
        style = {'font': font, 'hAlign': {'left': 0, 'center': 1, 'right': 2}.get(align, 0),
                 'vAlign': 1, 'clean': False}
        if color is not None:
            style['foreColor'] = color
        if background is not None:
            style['backColor'] = background
        groups.append({'ranges': list(ranges), 'style': style})

    def row_range(row, start='A', end='H'):
        return f'{start}{row}:{end}{row}'

    # The template uses a quiet white canvas, dark violet section titles and
    # pale violet group/header rows.
    add([f'A1:H{rows}'], color='#222222', background='#FFFFFF', bold=False, size=11)

    for index, row in enumerate(values, 1):
        text = module.hyperlink_display_text(row[0]).strip() if row else ''
        texts = [module.hyperlink_display_text(value).strip() for value in row]
        nonempty = [value for value in texts if value]
        first = texts[0] if texts else ''

        if text in {'同花顺板块', '同花顺板块相关个股', '1全阳', '3.5拉升',
                    '5. 涨停复盘', 'Final'}:
            add([row_range(index)], color='#51336C', bold=True, size=12)
        elif text in {'阴小4（必做任务）', '阴线跌破5日线（必做任务）'}:
            add([f'A{index}:A{index}'], color='#E33D3D', bold=True, size=12)
        elif text.startswith('4. 全阳阴线后面强势K'):
            add([f'A{index}:A{index}'], color='#2563EB', bold=True, size=11)
            add([f'B{index}:D{index}'], color='#51336C', bold=True, size=11)
        elif text in {'板块名称', '全阳'} or text.startswith('20天阴线='):
            add([row_range(index)], color='#51336C', background='#E4D5EF', bold=True, size=11)
        elif text.startswith('条件1 ZDZF8') or text.startswith('条件2 4不碰'):
            add([row_range(index)], color='#51336C', bold=True, size=11)
        elif text == '筛选条件':
            add([row_range(index)], color='#51336C', background='#F1EFF4', bold=True, size=11)
        elif text.startswith('跌破5日线=') or text.startswith('20日阴线='):
            add([row_range(index)], color='#51336C', background='#F5F0F8', bold=True, size=11)
        elif text.startswith('筛选一字'):
            add([f'A{index}:A{index}'], color='#9333EA', bold=True, size=11)
        elif first.startswith('优先选出阴后强'):
            add([f'A{index}:D{index}'], color='#9333EA', bold=True, size=12)
        elif first in {'光脚阳线或者光头阳线', '十字星（上下阴线很少，中体产业吃亏）',
                       '拉升6.5阴被吃'}:
            add([f'A{index}:A{index}'], color='#9333EA', bold=True, size=11)
        elif first.startswith('阴小4（必做任务）和阴线跌破5日线'):
            add([f'A{index}:A{index}'], color='#E33D3D', bold=True, size=12)
        elif first.startswith('筛选出K线最漂亮'):
            add([f'A{index}:A{index}'], color='#E33D3D', bold=True, size=12)
        elif first.startswith(('优先级1.', '优先级2.', '优先级3.', '低位优先级')):
            add([f'A{index}:A{index}'], color='#9333EA', background='#FFF152', bold=True, size=11)
            if len(nonempty) > 1:
                add([f'B{index}:H{index}'], color='#9333EA', bold=True, size=11)
        elif first == '无':
            add([f'A{index}:H{index}'], color='#9333EA', bold=True, size=11)

        # Board rows are the only rows whose limit-up count lives in column C.
        if len(texts) >= 3 and re.fullmatch(r'[+-]?\d+(?:\.\d+)?%', texts[1] or ''):
            count = safe_float(texts[2], -1)
            if count >= 5:
                add([f'A{index}:C{index}'], color='#51336C', background='#DCC8EA', bold=True, size=11)
            change = safe_float(texts[1].rstrip('%'), 0)
            add([f'B{index}:B{index}'], color='#D93025' if change > 0 else '#188038' if change < 0 else '#222222', bold=True, size=11)

        # Every qy2 stock row has a rich stock cell and optional annotations.
        if '满足连续4天不碰5日线' in texts:
            add([f'B{index}:B{index}'], color='#D93025', bold=True, size=11)
        if any(value.startswith('4天不碰5日线累计下引线和') for value in texts):
            add([f'C{index}:C{index}'], color='#7C3AED', bold=True, size=11)

    return groups


def _focus_review_style_groups(module, layout, rows):
    # Kept as a small compatibility wrapper for callers that still pass only
    # the legacy generated rows.  The full review path calls the template
    # version below after composing the current workbook layout.
    if not callable(getattr(module, 'style_payload', None)):
        return None
    return [
        {'ranges': [str(value).rsplit('!', 1)[-1] for value in group.get('ranges') or []],
         'style': group.get('style') or {}}
        for group in module.style_payload('', rows, layout)
    ]


def _focus_calculate_column_widths(module, values, columns):
    calculator = getattr(module, 'calculate_column_widths', None)
    if callable(calculator):
        try:
            return calculator(values, columns)
        except Exception:
            pass
    widths = []
    for column in range(columns):
        maximum = 0
        for row in values:
            value = row[column] if column < len(row) else ''
            if isinstance(value, list):
                value = ''.join(
                    str(item.get('text') or '') if isinstance(item, dict) else str(item or '')
                    for item in value
                )
            maximum = max(maximum, sum(
                2 if unicodedata.east_asian_width(char) in {'W', 'F', 'A'} else 1
                for char in str(value or '')
            ))
        widths.append(max(184, min(1000, maximum * 8 + 48)))
    return widths


def _focus_grid_payload(values, label, date_value, *, layout=None, style_groups=None,
                        source_keys=None, column_widths_px=None, row_heights_px=None):
    module = _load_review_builder_module()
    normalized = [list(row) if isinstance(row, list) else [row] for row in values or []]
    last_row = module.last_nonempty_row(normalized)
    normalized = normalized[:last_row]
    columns = max((len(row) for row in normalized), default=1)
    normalized = [row + [''] * (columns - len(row)) for row in normalized]
    display_values = [[_focus_text_value(value, module) for value in row] for row in normalized]
    rich_cells = [[_focus_rich_segments(value) for value in row] for row in normalized]
    if style_groups is not None:
        styles = _focus_style_cells_from_groups(style_groups, len(normalized), columns)
    else:
        review_groups = _focus_review_style_groups(module, layout or {}, len(normalized))
        styles = (_focus_style_cells_from_groups(review_groups, len(normalized), columns)
                  if review_groups is not None
                  else _focus_style_grid(normalized, layout))
    if column_widths_px is None:
        try:
            column_widths_px = _focus_calculate_column_widths(module, normalized, columns)
        except Exception:
            column_widths_px = [184] * columns
    if row_heights_px is None:
        row_heights_px = [32] * len(normalized)
    stock_codes = [['' for _ in range(columns)] for _ in range(len(display_values))]
    if isinstance(layout, dict):
        for cell_name, code in (layout.get('stock_codes') or {}).items():
            match = re.fullmatch(r'([A-Z]+)(\d+)', str(cell_name).upper())
            if not match:
                continue
            column = 0
            for char in match.group(1):
                column = column * 26 + ord(char) - ord('A') + 1
            row = int(match.group(2)) - 1
            column -= 1
            if 0 <= row < len(stock_codes) and 0 <= column < columns:
                stock_codes[row][column] = str(code or '')
    if str(label) == '财联社涨停分析':
        for row_index, row in enumerate(normalized):
            if len(row) > 1:
                code = str(row[1] or '').strip()
                if re.fullmatch(r'\d{6}(?:\.[A-Z]{2})?', code):
                    stock_codes[row_index][0] = code.split('.', 1)[0]
    return {
        'success': True, 'date': date_value.isoformat(), 'sheet_label': label,
        'values': display_values, 'rich_cells': rich_cells, 'style_cells': styles,
        'rows': len(display_values), 'columns': columns,
        'column_widths_px': [int(value) for value in (column_widths_px or [])[:columns]],
        'row_heights_px': [int(value) for value in (row_heights_px or [])[:len(display_values)]],
        'stock_codes': stock_codes,
        'source': 'redis', 'source_keys': source_keys or [],
        'available_sheets': [
            {'sheet': 'review', 'label': '复盘'},
            {'sheet': 'cls', 'label': '财联社涨停分析'},
        ],
    }


def _focus_review_payload(redis_client, date_value):
    module = _load_review_builder_module()
    compact = date_value.strftime('%Y%m%d')
    ths_payload = _focus_decode_redis_value(redis_client.get(f'ths:{compact}')) or {'boards': []}
    pz1_stocks = _focus_load_hash_stocks(redis_client, f'pz1:{compact}')
    pz_stocks = _focus_load_hash_stocks(redis_client, f'pz:{compact}')
    qy2_stocks = _focus_load_hash_stocks(redis_client, f'qy2:{compact}')
    if not qy2_stocks:
        try:
            _key, qy2_stocks, _source = module.load_daily_qy2(date_value)
        except Exception:
            qy2_stocks = []
    related_by_code = {}
    for stock in pz_stocks + pz1_stocks:
        code = module.normalize_stock_code(stock.get('code') or stock.get('stock_code_with_market'))
        if code:
            related_by_code[code] = stock
    # The web page must match the current daily workbook layout.  The old
    # path called build_sheet_content() directly, which is only the compact
    # pre-template representation and omits the qy2 detail block and Final
    # notes.  Keep the source live in Redis, while using a local, versioned
    # layout snapshot for fixed labels, row spacing and template sections.
    try:
        from redis_review import build_review
    except ModuleNotFoundError:
        from python_web.redis_review import build_review
    composed = build_review(
        module, date_value, ths_payload, pz1_stocks, qy2_stocks,
        list(related_by_code.values()),
    )
    rows = composed['values']
    layout = composed['layout']
    style_groups = composed['style_groups']
    payload = _focus_grid_payload(
        rows, '复盘选股数据', date_value, layout=layout,
        style_groups=style_groups,
        column_widths_px=composed['column_widths_px'],
        row_heights_px=composed['row_heights_px'],
        source_keys=[f'ths:{compact}', f'pz:{compact}', f'pz1:{compact}', f'qy2:{compact}'],
    )
    payload['counts'] = {
        'ths_boards': len(ths_payload.get('boards') or []),
        'pz': len(pz_stocks), 'pz1': len(pz1_stocks), 'qy2': len(qy2_stocks),
    }
    return payload


def _focus_cls_payload(redis_client, date_value):
    compact = date_value.strftime('%Y%m%d')
    key = _FOCUS_CLS_REDIS_KEY.format(date=compact)
    raw = redis_client.get(key)
    decoded = _focus_decode_redis_value(raw)
    if not isinstance(decoded, dict):
        raise LookupError(f'Redis没有当天财联社涨停分析: {key}')
    values = decoded.get('values') or []
    payload = _focus_grid_payload(
        values, '财联社涨停分析', date_value,
        style_groups=decoded.get('style_groups') or [],
        column_widths_px=decoded.get('column_widths_px'),
        row_heights_px=decoded.get('row_heights_px'),
        source_keys=[key],
    )
    payload['workbook_rows'] = decoded.get('rows', payload['rows'])
    payload['workbook_columns'] = decoded.get('columns', payload['columns'])
    return payload


def _focus_edit_key(sheet, date_value):
    # Web edits are an overlay.  Never write back to the source snapshot
    # keys (ths/pz/pz1/qy1/qy2/cls); the original Redis data stays read-only.
    return f'focus:web:edits:{sheet}:{date_value.strftime("%Y%m%d")}'


def _focus_apply_edits(redis_client, payload, sheet, date_value):
    edit_key = _focus_edit_key(sheet, date_value)
    edits = redis_client.hgetall(edit_key)
    payload['web_edit_key'] = edit_key
    payload['raw_data_read_only'] = True
    for raw_key, raw_value in edits.items():
        key = raw_key.decode('utf-8', errors='ignore') if isinstance(raw_key, bytes) else str(raw_key)
        try:
            row_text, column_text = key.split(':', 1)
            row, column = int(row_text), int(column_text)
        except (TypeError, ValueError):
            continue
        if 1 <= row <= payload['rows'] and 1 <= column <= payload['columns']:
            payload['values'][row - 1][column - 1] = raw_value
            payload['rich_cells'][row - 1][column - 1] = []
            payload['style_cells'][row - 1][column - 1]['background'] = '#FFFDF2'
    return payload


def _focus_load_push_log(redis_client, date_compact, updated_times):
    """Load the daily DingTalk push log for pages that display it."""
    push_log_key = f'dingtalk:pz:records:{date_compact}'
    push_log_records = []
    push_log_type = (
        redis_client.type(push_log_key)
        if redis_client.exists(push_log_key) else None
    )
    if isinstance(push_log_type, bytes):
        push_log_type = push_log_type.decode('utf-8', errors='ignore')
    if push_log_type != 'hash':
        return push_log_records, push_log_key
    for redis_code, raw_info in _ztorgj_load_hash_payloads(
        redis_client, push_log_key
    ):
        item = dict(raw_info or {})
        code = _ztorgj_stock_simple_code(item.get('code') or redis_code)
        sent_at = str(item.get('dingtalk_sent_at') or '').strip()
        if not re.match(r'^\d{6}$', code) or not sent_at:
            continue
        selected_at = str(item.get('selected_at') or '').strip()
        gain = _ztorgj_parse_float(
            item.get('today_change')
            if item.get('today_change') is not None
            else item.get('ref_gain'),
            0.0,
        )
        item.update({
            'code': code,
            'name': str(item.get('name') or code),
            'ref_gain': gain,
            'today_change': gain,
            'selected_at': selected_at,
            'dingtalk_sent_at': sent_at,
            'push_reasons': _pz_push_reason_labels(item),
        })
        push_log_records.append(item)
        updated_times.append(sent_at)
    push_log_records.sort(key=lambda item: item['code'])
    push_log_records.sort(
        key=lambda item: (item['dingtalk_sent_at'], item['selected_at']),
        reverse=True,
    )
    return push_log_records, push_log_key


@app.route('/api/pz1/focus/redis', methods=['GET'])
def get_focus_redis_grid():
    try:
        date_text = str(request.args.get('date') or '').strip()
        if not date_text:
            return no_cache_response({'success': False, 'error': '缺少date'}), 400
        date_value = _focus_parse_date(date_text)
        requested = str(request.args.get('sheet') or 'review').strip().lower()
        if requested in ('复盘', '复盘选股数据', 'review', '工作表1', ''):
            sheet = 'review'
            payload = _focus_review_payload(get_redis_client(), date_value)
        elif requested in ('财联社涨停分析', 'cls', 'close'):
            sheet = 'cls'
            payload = _focus_cls_payload(get_redis_client(), date_value)
        else:
            return no_cache_response({'success': False, 'error': 'sheet只支持复盘或财联社涨停分析'}), 400
        return no_cache_response(_focus_apply_edits(get_redis_client(), payload, sheet, date_value))
    except LookupError as exc:
        return no_cache_response({'success': False, 'error': str(exc)}), 404
    except Exception as exc:
        print(f'[pz1][focus][redis] 获取失败: {exc}', flush=True)
        return no_cache_response({'success': False, 'error': str(exc)}), 500


@app.route('/api/pz1/focus/redis', methods=['PUT'])
def save_focus_redis_grid():
    try:
        payload = request.get_json(silent=True) or {}
        date_value = _focus_parse_date(payload.get('date'))
        requested = str(payload.get('sheet') or 'review').strip().lower()
        sheet = 'review' if requested in ('复盘', '复盘选股数据', 'review', '工作表1') else 'cls' if requested in ('财联社涨停分析', 'cls', 'close') else ''
        changes = payload.get('changes')
        if not sheet or not isinstance(changes, list) or len(changes) > 1000:
            return no_cache_response({'success': False, 'error': 'sheet或changes格式错误'}), 400
        redis_client = get_redis_client()
        current = (_focus_review_payload(redis_client, date_value)
                   if sheet == 'review' else _focus_cls_payload(redis_client, date_value))
        edit_key = _focus_edit_key(sheet, date_value)
        saved = 0
        for change in changes:
            if not isinstance(change, dict):
                return no_cache_response({'success': False, 'error': 'changes包含无效项'}), 400
            try:
                row, column = int(change.get('row')), int(change.get('column'))
            except (TypeError, ValueError):
                return no_cache_response({'success': False, 'error': '单元格坐标无效'}), 400
            if not (1 <= row <= current['rows'] and 1 <= column <= current['columns']):
                return no_cache_response({'success': False, 'error': '单元格超出当前表格范围'}), 400
            value = change.get('value', '')
            if value is None:
                value = ''
            if not isinstance(value, (str, int, float, bool)):
                return no_cache_response({'success': False, 'error': '单元格内容必须是文本或数字'}), 400
            redis_client.hset(edit_key, f'{row}:{column}', str(value))
            saved += 1
        redis_client.expire(edit_key, _FOCUS_EDIT_TTL)
        return no_cache_response({
            'success': True,
            'saved_cells': saved,
            'source': 'redis_overlay',
            'redis_key': edit_key,
            'raw_data_unchanged': True,
            'message': '网页修改已单独保存，原始 Redis 数据未修改',
        })
    except LookupError as exc:
        return no_cache_response({'success': False, 'error': str(exc)}), 404
    except Exception as exc:
        print(f'[pz1][focus][redis] 保存失败: {exc}', flush=True)
        return no_cache_response({'success': False, 'error': str(exc)}), 500


@app.route('/性能')
def pz_performance_page():
    """Render the per-cycle PZ performance table."""
    return render_template('pz_performance.html')


@app.route('/api/pz1/focus/dates')
def get_pz1_focus_dates():
    """Return focus snapshot dates, newest first."""
    try:
        page_value = str(request.args.get('page') or '1').strip()
        if page_value not in ('1', '2', '3', '4', '5'):
            return no_cache_response({
                'success': False, 'error': 'page只支持1到5', 'dates': [],
            }), 400
        redis_client = get_redis_client()
        dates_by_page = {
            '1': _pz1_available_dates,
            '2': _pz_page2_available_dates,
            '3': _pz1_available_dates,
            '4': _qy1_available_dates,
            '5': _qy2_available_dates,
        }
        dates = dates_by_page[page_value](redis_client)
        return no_cache_response({'success': True, 'dates': dates})
    except Exception as e:
        print(f"[pz1][focus][dates] 获取失败: {e}", flush=True)
        return no_cache_response({'success': False, 'error': str(e), 'dates': []}), 500


@app.route('/api/pz1/focus/redis/dates')
def get_pz1_focus_redis_dates():
    """Return dates available for the two top-level Redis workbook pages."""
    try:
        redis_client = get_redis_client()
        dates = set()
        # A review date remains available when THS collection is missing. Push-log
        # dates are included as well because the review page can be generated from
        # the DingTalk Redis record even when another source is absent.
        for pattern in (
            'ths:*', 'pz:*', 'pz1:*', 'qy2:*', 'cls:limit_up:*',
            'dingtalk:pz:records:*',
        ):
            for raw_key in redis_client.scan_iter(match=pattern, count=200):
                key = raw_key.decode('utf-8', errors='ignore') if isinstance(raw_key, bytes) else str(raw_key)
                matched = re.fullmatch(
                    r'(?:ths|pz|pz1|qy2|cls:limit_up|dingtalk:pz:records):(\d{8})',
                    key,
                )
                if not matched:
                    continue
                try:
                    dates.add(datetime.strptime(matched.group(1), '%Y%m%d').strftime('%Y-%m-%d'))
                except ValueError:
                    continue
        return no_cache_response({'success': True, 'dates': sorted(dates, reverse=True)})
    except Exception as exc:
        print(f'[pz1][focus][redis-dates] 获取失败: {exc}', flush=True)
        return no_cache_response({'success': False, 'error': str(exc), 'dates': []}), 500


@app.route('/api/pz1/focus')
def get_pz1_focus():
    """Return PZ1, PZ, QY1, and QY2 focus groups for five pages."""
    try:
        page_value = str(request.args.get('page') or '1').strip()
        if page_value not in ('1', '2', '3', '4', '5'):
            return no_cache_response({
                'success': False, 'error': 'page只支持1到5', 'groups': {},
            }), 400
        page_no = int(page_value)
        source_prefix = {1: 'pz1', 2: 'pz', 3: 'pz1', 4: 'qy1', 5: 'qy2'}[page_no]
        redis_client = get_redis_client()
        requested_date = str(request.args.get('date') or '').strip()
        if requested_date:
            date_compact = requested_date.replace('-', '')
            try:
                parsed_date = datetime.strptime(date_compact, '%Y%m%d')
            except ValueError:
                return no_cache_response({
                    'success': False,
                    'error': 'date必须是YYYY-MM-DD格式',
                    'groups': {},
                }), 400
            display_date = parsed_date.strftime('%Y-%m-%d')
        else:
            if page_no in (1, 3):
                dates = _pz1_available_dates(redis_client)
            elif page_no == 2:
                dates = _pz_page2_available_dates(redis_client)
            elif page_no == 4:
                dates = _qy1_available_dates(redis_client)
            else:
                dates = _qy2_available_dates(redis_client)
            if not dates:
                return no_cache_response({
                    'success': False,
                    'error': f'没有可用的{source_prefix.upper()}日期',
                    'groups': {},
                }), 404
            display_date = dates[0]
            date_compact = display_date.replace('-', '')

        redis_key = f'{source_prefix}:{date_compact}'
        key_type = redis_client.type(redis_key) if redis_client.exists(redis_key) else None
        if isinstance(key_type, bytes):
            key_type = key_type.decode('utf-8', errors='ignore')
        extra_redis_key = f'pz1:{date_compact}' if page_no == 2 else None
        extra_key_type = (
            redis_client.type(extra_redis_key)
            if extra_redis_key and redis_client.exists(extra_redis_key) else None
        )
        if isinstance(extra_key_type, bytes):
            extra_key_type = extra_key_type.decode('utf-8', errors='ignore')
        if key_type != 'hash' and extra_key_type != 'hash':
            return no_cache_response({
                'success': False,
                'error': f'没有找到{source_prefix.upper()}数据: {redis_key}',
                'date': display_date,
                'groups': {},
                'redis_key': redis_key,
            }), 404

        records = []
        updated_times = []
        for redis_code, raw_info in _ztorgj_load_hash_payloads(redis_client, redis_key):
            record = dict(raw_info or {})
            code = _ztorgj_stock_simple_code(record.get('code') or redis_code)
            if not re.match(r'^\d{6}$', code):
                continue
            raw_yin_count_20 = _ztorgj_parse_int(record.get('yin_count_20'), None)
            if page_no == 1 and (
                raw_yin_count_20 is None
                or not 0 <= raw_yin_count_20 <= 7
                or not _ztorgj_truthy_flag(record.get('is_ref_yang'))
            ):
                continue
            yin_count_20 = raw_yin_count_20 if raw_yin_count_20 is not None else 0
            yin_count_10d = _ztorgj_parse_int(record.get('yin_count_10d'), 0)
            is_quanyang_10d = _ztorgj_parse_int(
                record.get('is_quanyang_10d'),
                1 if yin_count_10d == 0 else 0,
            )
            yin_pull_value = record.get('five_pull_yin_pull_next_or_eaten')
            if yin_pull_value is None:
                yin_pull_value = record.get('five_pull_yin_pull_next', 0)
            yin_pull_or_eaten = _ztorgj_parse_int(yin_pull_value, 0)
            limit_up_value = record.get('ref_is_limit_up')
            if limit_up_value is None:
                limit_up_value = record.get('is_limit_up', 0)
            display_gain = _ztorgj_parse_float(
                record.get('today_change')
                if record.get('today_change') is not None
                else record.get('ref_gain'),
                0.0,
            )
            updated_at = str(
                record.get('updated_at') or record.get('snapshot_at') or ''
            ).strip()
            if updated_at:
                updated_times.append(updated_at)
            record.update({
                'code': code,
                'name': str(record.get('name') or code),
                'ref_gain': display_gain,
                'today_change': display_gain,
                'yin_count_20': yin_count_20,
                'yin_count_10d': yin_count_10d,
                'is_quanyang_10d': is_quanyang_10d,
                'all_yin_up': int(_ztorgj_truthy_flag(record.get('all_yin_up'))),
                'all_yin_no_touch': int(_ztorgj_truthy_flag(record.get('all_yin_no_touch'))),
                'all_yin_cross_star': int(_ztorgj_truthy_flag(record.get('all_yin_cross_star'))),
                'strong_k_count_10d': _ztorgj_parse_int(
                    record.get('strong_k_count_10d'), None
                ),
                'five_pull_yin_pull_next_or_eaten': yin_pull_or_eaten,
                'condition_2_met': int(_ztorgj_truthy_flag(record.get('condition_2_met'))),
                'ref_is_limit_up': _ztorgj_truthy_flag(limit_up_value),
                'pz1_five_pull_met': int(_ztorgj_truthy_flag(record.get('pz1_five_pull_met'))),
                'bare_foot_count_10d': _ztorgj_parse_int(record.get('bare_foot_count_10d'), None),
                'nearly_bare_foot_count_10d': _ztorgj_parse_int(
                    record.get('nearly_bare_foot_count_10d'), None
                ),
                'pz1_high_gain_prev3_short_shadow_met': int(
                    _ztorgj_truthy_flag(
                        record.get('pz1_high_gain_prev3_short_shadow_met')
                    )
                ),
                'prev3_yang_count': _ztorgj_parse_int(
                    record.get('prev3_yang_count'), None
                ),
                'updated_at': updated_at,
            })
            records.append(record)

        if page_no == 5:
            for item in records:
                item['low_above_ma5_count_10d'] = _ztorgj_parse_int(
                    item.get('low_above_ma5_count_10d'), None
                )
                item['high_gain_10d_count'] = _ztorgj_parse_int(
                    item.get('high_gain_10d_count'), None
                )
                item['close_below_ma5_count_10d'] = _ztorgj_parse_int(
                    item.get('close_below_ma5_count_10d'), None
                )
                item['close_below_ma5_abs_pct_sum_10d'] = _ztorgj_parse_float(
                    item.get('close_below_ma5_abs_pct_sum_10d'), None
                )
                item['high_gain_10d_gte_5'] = int(
                    _ztorgj_truthy_flag(item.get('high_gain_10d_gte_5'))
                )

            def sort_by_close_below_ma5(items):
                return sorted(
                    items,
                    key=lambda item: (
                        item['close_below_ma5_count_10d'] is None,
                        item['close_below_ma5_count_10d']
                        if item['close_below_ma5_count_10d'] is not None else 0,
                        item['close_below_ma5_abs_pct_sum_10d'] is None,
                        item['close_below_ma5_abs_pct_sum_10d']
                        if item['close_below_ma5_abs_pct_sum_10d'] is not None else 0.0,
                        -item['ref_gain'],
                        item['code'],
                    ),
                )

            records.sort(key=lambda item: (
                item['low_above_ma5_count_10d'] is None,
                -(item['low_above_ma5_count_10d'] or 0),
                -item['ref_gain'],
                item['code'],
            ))
            all_qy2_records = sort_by_close_below_ma5(records)
            low_above_5_records = sort_by_close_below_ma5(
                item for item in records
                if item['low_above_ma5_count_10d'] is not None
                and item['low_above_ma5_count_10d'] >= 5
            )
            high_gain_records = sort_by_close_below_ma5(
                item for item in records if item['high_gain_10d_gte_5'] == 1
            )
            return no_cache_response({
                'success': True, 'page': 5, 'date': display_date,
                'count': len(records),
                'groups': {
                    'low_above_ma5_10d': all_qy2_records,
                    'low_above_ma5_10d_gte_5': low_above_5_records,
                    'high_gain_10d_gte_5': high_gain_records,
                },
                'redis_key': redis_key,
                'updated_at': max(updated_times) if updated_times else '',
            })

        if page_no == 4:
            fields = ('all_yin_up', 'all_yin_no_touch', 'all_yin_cross_star')

            def sort_qy1_shape(items):
                return sorted(
                    items,
                    key=lambda item: (
                        item['strong_k_count_10d'] is None,
                        -(item['strong_k_count_10d'] or 0),
                        -item['ref_gain'],
                        item['code'],
                    ),
                )

            groups = {
                field: sort_qy1_shape(
                    item for item in records if item[field] == 1
                )
                for field in fields
            }
            return no_cache_response({
                'success': True, 'page': 4, 'date': display_date,
                'count': len({item['code'] for items in groups.values() for item in items}),
                'groups': groups, 'redis_key': redis_key,
                'updated_at': max(updated_times) if updated_times else '',
            })

        if page_no == 2:
            fields = (
                'condition_3_met', 'five_pull_all_yin_up',
                'five_pull_all_yin_eaten_next', 'five_pull_yin_pull_next_or_eaten',
                'five_pull_all_yin_cross_star', 'five_pull_all_yin_no_touch',
            )
            groups = {
                field: sorted(
                    (item for item in records if _ztorgj_parse_int(item.get(field), 0) == 1),
                    key=lambda item: (item['yin_count_10d'], -item['ref_gain'], item['code']),
                )
                for field in fields
            }
            extra_records = []
            extra_fields = {
                'yi_yin_yi_yang': 'yi_yin_yi_yang',
                'qiangchi': 'qiangchi',
                'pz1_five_pull_yin_pull_next_or_eaten': 'five_pull_yin_pull_next_or_eaten',
            }
            for redis_code, raw_info in _ztorgj_load_hash_payloads(
                redis_client, extra_redis_key
            ):
                item = dict(raw_info or {})
                code = _ztorgj_stock_simple_code(item.get('code') or redis_code)
                if not re.match(r'^\d{6}$', code):
                    continue
                flags = {
                    field: int(_ztorgj_truthy_flag(item.get(field)))
                    for field in ('yi_yin_yi_yang', 'qiangchi')
                }
                next_pull = item.get('five_pull_yin_pull_next_or_eaten')
                if next_pull is None:
                    next_pull = item.get('five_pull_yin_pull_next', 0)
                flags['five_pull_yin_pull_next_or_eaten'] = int(_ztorgj_truthy_flag(next_pull))
                if not any(flags.values()):
                    continue
                gain = _ztorgj_parse_float(
                    item.get('today_change')
                    if item.get('today_change') is not None else item.get('ref_gain'),
                    0.0,
                )
                item.update({
                    'code': code, 'name': str(item.get('name') or code),
                    'ref_gain': gain, 'today_change': gain,
                    'yin_count_10d': _ztorgj_parse_int(item.get('yin_count_10d'), 0),
                    'ref_is_limit_up': _ztorgj_truthy_flag(
                        item.get('ref_is_limit_up')
                        if item.get('ref_is_limit_up') is not None
                        else item.get('is_limit_up', 0)
                    ),
                    **flags,
                })
                extra_records.append(item)
                updated_at = str(item.get('updated_at') or item.get('snapshot_at') or '').strip()
                if updated_at:
                    updated_times.append(updated_at)
            for group_key, field in extra_fields.items():
                groups[group_key] = sorted(
                    (item for item in extra_records if item[field] == 1),
                    key=lambda item: (item['yin_count_10d'], -item['ref_gain'], item['code']),
                )
            push_log_records, push_log_key = _focus_load_push_log(
                redis_client, date_compact, updated_times,
            )
            groups['push_log'] = push_log_records
            return no_cache_response({
                'success': True, 'page': 2, 'date': display_date,
                'count': len({item['code'] for items in groups.values() for item in items}),
                'groups': groups, 'redis_key': redis_key,
                'extra_redis_key': extra_redis_key,
                'push_log_redis_key': push_log_key,
                'updated_at': max(updated_times) if updated_times else '',
            })

        if page_no == 3:
            fields = (
                'five_pull_yin_pull_next_or_eaten', 'condition_3_met',
            )

            def sort_by_nearly_bare_foot(items):
                return sorted(
                    items,
                    key=lambda item: (
                        item['nearly_bare_foot_count_10d'] is None,
                        -(item['nearly_bare_foot_count_10d'] or 0),
                        -item['ref_gain'],
                        item['code'],
                    ),
                )

            groups = {
                field: sort_by_nearly_bare_foot(
                    item for item in records
                    if _ztorgj_parse_int(item.get(field), 0) == 1
                )
                for field in fields
            }
            for field in ('yi_yin_yi_yang_10d', 'yi_yin_yi_yang_20d'):
                groups[field] = sorted(
                    (item for item in records if _ztorgj_truthy_flag(item.get(field))),
                    key=lambda item: (-item['ref_gain'], item['code']),
                )
            groups['high_gain_prev3_short_shadow'] = sorted(
                (
                    item for item in records
                    if item['pz1_high_gain_prev3_short_shadow_met'] == 1
                ),
                key=lambda item: (
                    item['prev3_yang_count'] is None,
                    -(item['prev3_yang_count'] or 0),
                    -item['ref_gain'],
                    item['code'],
                ),
            )
            qy1_key = f'qy1:{date_compact}'
            qy1_records = []
            qy1_type = redis_client.type(qy1_key) if redis_client.exists(qy1_key) else None
            if isinstance(qy1_type, bytes):
                qy1_type = qy1_type.decode('utf-8', errors='ignore')
            if qy1_type == 'hash':
                for redis_code, raw_info in _ztorgj_load_hash_payloads(redis_client, qy1_key):
                    item = dict(raw_info or {})
                    code = _ztorgj_stock_simple_code(item.get('code') or redis_code)
                    if not re.match(r'^\d{6}$', code):
                        continue
                    gain = _ztorgj_parse_float(
                        item.get('today_change')
                        if item.get('today_change') is not None
                        else item.get('ref_gain'),
                        0.0,
                    )
                    updated_at = str(
                        item.get('updated_at') or item.get('snapshot_at') or ''
                    ).strip()
                    item.update({
                        'code': code,
                        'name': str(item.get('name') or code),
                        'ref_gain': gain,
                        'today_change': gain,
                        'is_quanyang_10d': int(_ztorgj_truthy_flag(
                            item.get('is_quanyang_10d')
                        )),
                        'strong_k_count_10d': _ztorgj_parse_int(
                            item.get('strong_k_count_10d'), None
                        ),
                        'low_above_ma5_count_10d': _ztorgj_parse_int(
                            item.get('low_above_ma5_count_10d'), None
                        ),
                        'updated_at': updated_at,
                    })
                    qy1_records.append(item)
                    if updated_at:
                        updated_times.append(updated_at)
            qy1_records.sort(key=lambda item: (
                -item['is_quanyang_10d'],
                item['low_above_ma5_count_10d'] is None,
                -(item['low_above_ma5_count_10d'] or 0),
                -item['ref_gain'],
                item['code'],
            ))
            groups['qy1_all'] = qy1_records
            push_log_records, push_log_key = _focus_load_push_log(
                redis_client, date_compact, updated_times,
            )
            groups['push_log'] = push_log_records
            return no_cache_response({
                'success': True, 'page': 3, 'date': display_date,
                'count': len({item['code'] for items in groups.values() for item in items}),
                'groups': groups, 'redis_key': redis_key,
                'qy1_redis_key': qy1_key,
                'push_log_redis_key': push_log_key,
                'updated_at': max(updated_times) if updated_times else '',
            })

        if page_no == 1:
            records.sort(key=lambda item: (
                item['yin_count_20'], -item['ref_gain'], item['code']
            ))
            return no_cache_response({
                'success': True, 'page': 1, 'date': display_date,
                'count': len(records),
                'groups': {'pz_all': records},
                'redis_key': redis_key,
                'updated_at': max(updated_times) if updated_times else '',
            })

    except Exception as e:
        print(f"[pz1][focus] 获取失败: {e}", flush=True)
        return no_cache_response({
            'success': False,
            'error': str(e),
            'groups': {},
        }), 500


@app.route('/api/pz/push-log/dates')
def get_pz_push_log_dates():
    """Return successful DingTalk push record dates, newest first."""
    try:
        dates = _pz_push_log_available_dates(get_redis_client())
        return no_cache_response({'success': True, 'dates': dates})
    except Exception as e:
        print(f"[pz][push-log][dates] 获取失败: {e}", flush=True)
        return no_cache_response({'success': False, 'error': str(e), 'dates': []}), 500


@app.route('/api/pz/performance/dates')
def get_pz_performance_dates():
    """Return PZ performance record dates, newest first."""
    try:
        dates = _pz_performance_available_dates(get_redis_client())
        return no_cache_response({'success': True, 'dates': dates})
    except Exception as e:
        print(f"[pz][performance][dates] 获取失败: {e}", flush=True)
        return no_cache_response({'success': False, 'error': str(e), 'dates': []}), 500


@app.route('/api/pz/performance')
def get_pz_performance():
    """Return per-cycle performance records stored by the QMT PZ scanner."""
    try:
        redis_client = get_redis_client()
        requested_date = str(request.args.get('date') or '').strip()
        if requested_date:
            date_compact = requested_date.replace('-', '')
            try:
                parsed_date = datetime.strptime(date_compact, '%Y%m%d')
            except ValueError:
                return no_cache_response({
                    'success': False,
                    'error': 'date必须是YYYY-MM-DD格式',
                }), 400
            display_date = parsed_date.strftime('%Y-%m-%d')
        else:
            dates = _pz_performance_available_dates(redis_client)
            if not dates:
                return no_cache_response({
                    'success': False,
                    'error': '没有可用的性能记录日期',
                }), 404
            display_date = dates[0]
            date_compact = display_date.replace('-', '')

        limit_value = str(request.args.get('limit') or '100').strip().lower()
        if limit_value == 'all':
            limit = 'all'
        elif limit_value in ('100', '500'):
            limit = int(limit_value)
        else:
            return no_cache_response({
                'success': False,
                'error': 'limit只支持100、500或all',
            }), 400

        try:
            offset = int(str(request.args.get('offset') or '0').strip())
        except ValueError:
            offset = -1
        if offset < 0:
            return no_cache_response({
                'success': False,
                'error': 'offset必须是非负整数',
            }), 400

        redis_key = f'pz:performance:{date_compact}'
        key_type = redis_client.type(redis_key) if redis_client.exists(redis_key) else None
        if isinstance(key_type, bytes):
            key_type = key_type.decode('utf-8', errors='ignore')
        if key_type != 'list':
            return no_cache_response({
                'success': False,
                'error': f'没有找到性能记录: {redis_key}',
                'date': display_date,
                'redis_key': redis_key,
            }), 404

        total_count = int(redis_client.llen(redis_key) or 0)
        end = -1 if limit == 'all' else offset + limit - 1
        raw_rows = redis_client.lrange(redis_key, offset, end)
        rows = []
        for raw_value in raw_rows:
            try:
                row = _ztorgj_decode_stock_payload(raw_value)
            except Exception as decode_error:
                print(
                    f"[pz][performance] 跳过无法解析的记录: {decode_error}",
                    flush=True,
                )
                continue
            if isinstance(row, dict):
                rows.append(row)

        updated_at = ''
        if rows:
            updated_at = str(
                rows[0].get('recorded_at')
                or rows[0].get('finished_at')
                or rows[0].get('failed_at')
                or ''
            )
        return no_cache_response({
            'success': True,
            'date': display_date,
            'redis_key': redis_key,
            'total_count': total_count,
            'offset': offset,
            'limit': limit,
            'count': len(rows),
            'updated_at': updated_at,
            'data': rows,
        })
    except Exception as e:
        print(f"[pz][performance] 获取失败: {e}", flush=True)
        return no_cache_response({'success': False, 'error': str(e)}), 500


@app.route('/api/pz/push-log')
def get_pz_push_log():
    """Return successful DingTalk pushes grouped for the eight-column log."""
    try:
        redis_client = get_redis_client()
        requested_date = str(request.args.get('date') or '').strip()
        if requested_date:
            date_compact = requested_date.replace('-', '')
            try:
                parsed_date = datetime.strptime(date_compact, '%Y%m%d')
            except ValueError:
                return no_cache_response({
                    'success': False,
                    'error': 'date必须是YYYY-MM-DD格式',
                    'groups': {'all': [], 'condition_2': [], 'condition_3': []},
                }), 400
            display_date = parsed_date.strftime('%Y-%m-%d')
        else:
            dates = _pz_push_log_available_dates(redis_client)
            if not dates:
                return no_cache_response({
                    'success': False,
                    'error': '没有可用的推送日志日期',
                    'groups': {'all': [], 'condition_2': [], 'condition_3': []},
                }), 404
            display_date = dates[0]
            date_compact = display_date.replace('-', '')

        redis_key = f'dingtalk:pz:records:{date_compact}'
        key_type = redis_client.type(redis_key) if redis_client.exists(redis_key) else None
        if isinstance(key_type, bytes):
            key_type = key_type.decode('utf-8', errors='ignore')
        if key_type != 'hash':
            return no_cache_response({
                'success': False,
                'error': f'没有找到推送日志: {redis_key}',
                'date': display_date,
                'groups': {'all': [], 'condition_2': [], 'condition_3': []},
                'redis_key': redis_key,
            }), 404

        records = []
        pz_rows_by_code = {}
        pz_key = f'pz:{date_compact}'
        if redis_client.exists(pz_key):
            pz_type = redis_client.type(pz_key)
            if isinstance(pz_type, bytes):
                pz_type = pz_type.decode('utf-8', errors='ignore')
            if pz_type == 'hash':
                for pz_code, pz_info in _ztorgj_load_hash_payloads(redis_client, pz_key):
                    normalized_code = _ztorgj_stock_simple_code(
                        (pz_info or {}).get('code') or pz_code
                    )
                    if normalized_code:
                        pz_rows_by_code[normalized_code] = dict(pz_info or {})

        for redis_code, raw_info in _ztorgj_load_hash_payloads(redis_client, redis_key):
            record = dict(raw_info or {})
            code = _ztorgj_stock_simple_code(record.get('code') or redis_code)
            sent_at = str(record.get('dingtalk_sent_at') or '').strip()
            if not re.match(r'^\d{6}$', code) or not sent_at:
                continue
            pz_info = pz_rows_by_code.get(code, {})
            yin_count_20 = _ztorgj_parse_int(
                record.get('yin_count_20', pz_info.get('yin_count_20')), 0
            )
            yin_count_10d = _ztorgj_parse_int(
                record.get('yin_count_10d', pz_info.get('yin_count_10d')), 0
            )
            condition_3_met = 1 if _ztorgj_truthy_flag(record.get('condition_3_met')) else 0
            five_pull_fields = (
                'five_pull_all_yin_up',
                'five_pull_all_yin_eaten_next',
                'five_pull_yin_pull_next',
                'five_pull_all_yin_cross_star',
                'five_pull_all_yin_no_touch',
            )
            record.update({
                'code': code,
                'name': str(record.get('name') or code),
                'ref_gain': _ztorgj_parse_float(record.get('ref_gain'), 0.0),
                'yin_count_20': yin_count_20,
                'yin_count_10d': yin_count_10d,
                'condition_2_met': 1 if yin_count_20 <= 7 else 0,
                'condition_3_met': condition_3_met,
                'selected_at': str(record.get('selected_at') or '').strip(),
                'dingtalk_sent_at': sent_at,
            })
            for field in five_pull_fields:
                raw_flag = record.get(field)
                if raw_flag is None:
                    raw_flag = pz_info.get(field, 0)
                record[field] = 1 if _ztorgj_truthy_flag(raw_flag) else 0
            records.append(record)

        records.sort(key=lambda item: (
            str(item.get('dingtalk_sent_at') or ''),
            str(item.get('selected_at') or ''),
            str(item.get('code') or ''),
        ), reverse=True)
        for index, record in enumerate(records):
            record['global_rank'] = index + 1 if index < 3 else 0

        condition_2_records = sorted(
            (item for item in records if item['condition_2_met'] == 1),
            key=lambda item: item['yin_count_20'],
        )

        def sort_by_yin10(items):
            return sorted(items, key=lambda item: item['yin_count_10d'])

        groups = {
            'all': records,
            'condition_2': condition_2_records,
            'condition_3': sort_by_yin10(
                item for item in records if item['condition_3_met'] == 1
            ),
            'five_pull_all_yin_up': sort_by_yin10(
                item for item in records if item['five_pull_all_yin_up'] == 1
            ),
            'five_pull_all_yin_eaten_next': sort_by_yin10(
                item for item in records if item['five_pull_all_yin_eaten_next'] == 1
            ),
            'five_pull_yin_pull_next': sort_by_yin10(
                item for item in records if item['five_pull_yin_pull_next'] == 1
            ),
            'five_pull_yin_pull_next_or_eaten': sort_by_yin10(
                item for item in records if item['five_pull_yin_pull_next'] == 1
            ),
            'five_pull_all_yin_cross_star': sort_by_yin10(
                item for item in records if item['five_pull_all_yin_cross_star'] == 1
            ),
            'five_pull_all_yin_no_touch': sort_by_yin10(
                item for item in records if item['five_pull_all_yin_no_touch'] == 1
            ),
        }
        return no_cache_response({
            'success': True,
            'date': display_date,
            'count': len(records),
            'groups': groups,
            'redis_key': redis_key,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
    except Exception as e:
        print(f"[pz][push-log] 获取失败: {e}", flush=True)
        return no_cache_response({
            'success': False,
            'error': str(e),
            'groups': {'all': [], 'condition_2': [], 'condition_3': []},
        }), 500


@app.route('/api/pz/dates')
def get_pz_dates():
    """Return PZ snapshot dates as plain JSON, newest first."""
    try:
        dates = _pz_available_dates(get_redis_client())
        return no_cache_response({'success': True, 'dates': dates})
    except Exception as e:
        print(f"[pz][dates] 获取失败: {e}", flush=True)
        return no_cache_response({'success': False, 'error': str(e), 'dates': []}), 500


@app.route('/api/qy1/stocks')
@app.route('/api/qy1/stocks/<date>')
def get_qy1_stocks(date=None):
    """Return QY1 stock snapshots as plain JSON."""
    try:
        redis_client = get_redis_client()
        requested_date = str(date or request.args.get('date') or '').strip()
        if requested_date:
            if not re.fullmatch(r'(?:\d{8}|\d{4}-\d{2}-\d{2})', requested_date):
                return no_cache_response({
                    'success': False,
                    'error': 'date必须是YYYY-MM-DD格式',
                    'data': [],
                }), 400
            date_compact = requested_date.replace('-', '')
            try:
                parsed_date = datetime.strptime(date_compact, '%Y%m%d')
            except ValueError:
                return no_cache_response({
                    'success': False,
                    'error': 'date必须是有效的YYYY-MM-DD日期',
                    'data': [],
                }), 400
            display_date = parsed_date.strftime('%Y-%m-%d')
        else:
            dates = _qy1_available_dates(redis_client)
            if not dates:
                return no_cache_response({
                    'success': False,
                    'error': '没有可用的QY1日期',
                    'data': [],
                }), 404
            display_date = dates[0]
            date_compact = display_date.replace('-', '')

        stock_key = f'qy1:{date_compact}'
        key_type = redis_client.type(stock_key) if redis_client.exists(stock_key) else None
        if isinstance(key_type, bytes):
            key_type = key_type.decode('utf-8', errors='ignore')
        if key_type != 'hash':
            return no_cache_response({
                'success': False,
                'error': f'没有找到QY1数据: {stock_key}',
                'date': display_date,
                'count': 0,
                'data': [],
                'redis_key': stock_key,
            }), 404

        scan_info = {}
        raw_scan_info = redis_client.hget(stock_key, '_scan_info')
        if raw_scan_info:
            try:
                scan_info = _ztorgj_decode_stock_payload(raw_scan_info)
            except Exception:
                scan_info = {}

        stocks = []
        updated_times = []
        for redis_code, raw_info in _ztorgj_load_hash_payloads(redis_client, stock_key):
            stock = dict(raw_info or {})
            code = _ztorgj_stock_simple_code(stock.get('code') or redis_code)
            if not re.match(r'^\d{6}$', code):
                continue
            stock['code'] = code
            stock.setdefault('name', code)
            stock['close_below_ma5_count_10d'] = _ztorgj_parse_int(
                stock.get('close_below_ma5_count_10d'), 0
            )
            stock['close_below_ma5_abs_pct_sum_10d'] = _ztorgj_parse_float(
                stock.get('close_below_ma5_abs_pct_sum_10d'), 0.0
            )
            display_gain = stock.get('today_change')
            if display_gain is None:
                display_gain = stock.get('ref_gain', 0)
            stock['_display_gain'] = _ztorgj_parse_float(display_gain, 0.0)
            updated_at = str(stock.get('updated_at') or stock.get('snapshot_at') or '')
            if updated_at:
                updated_times.append(updated_at)
            stocks.append(stock)

        stocks.sort(key=lambda item: (-item['_display_gain'], item.get('code', '')))
        for stock in stocks:
            stock.pop('_display_gain', None)

        for field in ('published_at', 'finished_at', 'updated_at', 'snapshot_at'):
            value = str(scan_info.get(field) or '') if isinstance(scan_info, dict) else ''
            if value:
                updated_times.append(value)

        return no_cache_response({
            'success': True,
            'date': display_date,
            'count': len(stocks),
            'redis_key': stock_key,
            'scan_info': scan_info,
            'updated_at': max(updated_times) if updated_times else '',
            'data': stocks,
        })
    except Exception as e:
        print(f"[qy1][stocks] 获取失败: {e}", flush=True)
        return no_cache_response({
            'success': False,
            'error': str(e),
            'data': [],
        }), 500


@app.route('/api/qy1/klines')
@app.route('/api/qy1/klines/<date>')
def get_qy1_klines(date=None):
    """Return QY1 stocks and their dedicated QMT K-lines as plain JSON."""
    try:
        redis_client = get_redis_client()
        requested_date = str(date or request.args.get('date') or '').strip()
        if requested_date:
            if not re.fullmatch(r'(?:\d{8}|\d{4}-\d{2}-\d{2})', requested_date):
                return no_cache_response({
                    'success': False,
                    'error': 'date必须是YYYY-MM-DD格式',
                    'data': [],
                }), 400
            date_compact = requested_date.replace('-', '')
            try:
                parsed_date = datetime.strptime(date_compact, '%Y%m%d')
            except ValueError:
                return no_cache_response({
                    'success': False,
                    'error': 'date必须是有效的YYYY-MM-DD日期',
                    'data': [],
                }), 400
            display_date = parsed_date.strftime('%Y-%m-%d')
        else:
            dates = _qy1_available_dates(redis_client)
            if not dates:
                return no_cache_response({
                    'success': False,
                    'error': '没有可用的QY1日期',
                    'data': [],
                }), 404
            display_date = dates[0]
            date_compact = display_date.replace('-', '')

        try:
            days = int(request.args.get('days') or 60)
        except (TypeError, ValueError):
            days = 60
        days = max(1, min(days, 120))

        stock_key = f'qy1:{date_compact}'
        key_type = redis_client.type(stock_key) if redis_client.exists(stock_key) else None
        if isinstance(key_type, bytes):
            key_type = key_type.decode('utf-8', errors='ignore')
        if key_type != 'hash':
            return no_cache_response({
                'success': False,
                'error': f'没有找到QY1数据: {stock_key}',
                'date': display_date,
                'days': days,
                'data': [],
                'redis_key': stock_key,
            }), 404

        scan_info = {}
        raw_scan_info = redis_client.hget(stock_key, '_scan_info')
        if raw_scan_info:
            try:
                scan_info = _ztorgj_decode_stock_payload(raw_scan_info)
            except Exception:
                scan_info = {}

        stocks = []
        for redis_code, raw_info in _ztorgj_load_hash_payloads(redis_client, stock_key):
            stock = dict(raw_info or {})
            code = _ztorgj_stock_simple_code(stock.get('code') or redis_code)
            if not re.match(r'^\d{6}$', code):
                continue
            stock['code'] = code
            stock.setdefault('name', code)
            stock['close_below_ma5_count_10d'] = _ztorgj_parse_int(
                stock.get('close_below_ma5_count_10d'), 0
            )
            stock['close_below_ma5_abs_pct_sum_10d'] = _ztorgj_parse_float(
                stock.get('close_below_ma5_abs_pct_sum_10d'), 0.0
            )
            display_gain = stock.get('today_change')
            if display_gain is None:
                display_gain = stock.get('ref_gain', 0)
            stock['_display_gain'] = _ztorgj_parse_float(display_gain, 0.0)
            stocks.append(stock)
        stocks.sort(key=lambda item: (-item['_display_gain'], item.get('code', '')))

        result = []
        missing_codes = []
        updated_times = []
        for stock in stocks:
            code = stock['code']
            kline_payload = _qy1_load_kline_payload(
                redis_client, code, date_compact, days=days
            )
            rows = kline_payload.get('rows', []) if kline_payload else []
            updated_at = kline_payload.get('updated_at', '') if kline_payload else ''
            if not rows:
                missing_codes.append(code)
            if updated_at:
                updated_times.append(updated_at)
            stock.pop('_display_gain', None)
            result.append({
                'code': code,
                'name': str(stock.get('name') or code),
                'ref_gain': _ztorgj_parse_float(stock.get('ref_gain'), 0.0),
                'today_change': _ztorgj_parse_float(
                    stock.get('today_change') if stock.get('today_change') is not None else stock.get('ref_gain'),
                    0.0,
                ),
                'strong_k_count_10d': _ztorgj_parse_int(
                    stock.get('strong_k_count_10d'), 0
                ),
                'is_quanyang_10d': _ztorgj_parse_int(
                    stock.get('is_quanyang_10d'), 0
                ),
                'close_below_ma5_count_10d': _ztorgj_parse_int(
                    stock.get('close_below_ma5_count_10d'), 0
                ),
                'close_below_ma5_abs_pct_sum_10d': _ztorgj_parse_float(
                    stock.get('close_below_ma5_abs_pct_sum_10d'), 0.0
                ),
                'stock': stock,
                'kline_updated_at': updated_at,
                'kline_data': rows,
            })

        for field in ('published_at', 'finished_at', 'updated_at', 'snapshot_at'):
            value = str(scan_info.get(field) or '') if isinstance(scan_info, dict) else ''
            if value:
                updated_times.append(value)

        return no_cache_response({
            'success': True,
            'encrypted': False,
            'date': display_date,
            'days': days,
            'count': len(result),
            'kline_count': len(result) - len(missing_codes),
            'redis_key': stock_key,
            'kline_history_key': _qy1_history_kline_key(date_compact),
            'kline_realtime_key': _qy1_realtime_kline_key(date_compact),
            'scan_info': scan_info,
            'kline_updated_at': max(updated_times) if updated_times else '',
            'missing_codes': missing_codes,
            'data': result,
        })
    except Exception as e:
        print(f"[qy1][klines] 获取失败: {e}", flush=True)
        return no_cache_response({
            'success': False,
            'error': str(e),
            'data': [],
        }), 500


@app.route('/api/pz/quanyang/klines', defaults={'quanyang': True})
@app.route('/api/pz/klines')
@app.route('/api/pz/condition<int:condition_no>/klines')
def get_pz_klines(condition_no=None, quanyang=False):
    """Return current PZ stocks and their dedicated QMT K-lines as plain JSON."""
    try:
        condition_fields = {
            2: ('condition_2_met', '20天阴线次数小于等于7'),
            3: ('condition_3_met', '10日高涨大于等于5次且10日收盘都大于MA10'),
            4: ('condition_4_met', '最近10日最低价大于MA5次数大于等于5'),
            5: ('five_pull_all_yin_up', '5拉阴线都向上'),
            6: ('five_pull_all_yin_cross_star', '5拉阴线都是十字星'),
            7: ('five_pull_yin_pull_next_or_eaten', '5拉阴后拉或吃'),
            8: ('five_pull_all_yin_eaten_next', '5拉阴被吃'),
            9: ('five_pull_all_yin_no_touch', '5拉阴都不碰'),
        }
        condition_field = ''
        condition_name = ''
        quanyang_filter = bool(quanyang)
        if quanyang_filter:
            condition_field = 'is_quanyang_10d'
            condition_name = '最近10个交易日没有阴线'
        elif condition_no is not None:
            condition_config = condition_fields.get(condition_no)
            if condition_config is None:
                return no_cache_response({
                    'success': False,
                    'error': 'condition只支持2、3、4、5、6、7、8、9',
                    'data': [],
                }), 400
            condition_field, condition_name = condition_config

        redis_client = get_redis_client()
        requested_date = str(request.args.get('date') or '').strip()
        if requested_date:
            date_compact = requested_date.replace('-', '')
            try:
                parsed_date = datetime.strptime(date_compact, '%Y%m%d')
            except ValueError:
                return no_cache_response({
                    'success': False,
                    'error': 'date必须是YYYY-MM-DD格式',
                    'data': [],
                }), 400
            display_date = parsed_date.strftime('%Y-%m-%d')
        else:
            dates = _pz_available_dates(redis_client)
            if not dates:
                return no_cache_response({
                    'success': False,
                    'error': '没有可用的PZ日期',
                    'data': [],
                }), 404
            display_date = dates[0]
            date_compact = display_date.replace('-', '')

        try:
            days = int(request.args.get('days') or 60)
        except (TypeError, ValueError):
            days = 60
        days = max(1, min(days, 120))

        stock_key = f'pz:{date_compact}'
        key_type = redis_client.type(stock_key) if redis_client.exists(stock_key) else None
        if isinstance(key_type, bytes):
            key_type = key_type.decode('utf-8', errors='ignore')
        if key_type != 'hash':
            return no_cache_response({
                'success': False,
                'error': f'没有找到PZ数据: {stock_key}',
                'date': display_date,
                'days': days,
                'data': [],
                'redis_key': stock_key,
            }), 404

        scan_info = {}
        raw_scan_info = redis_client.hget(stock_key, '_scan_info')
        if raw_scan_info:
            try:
                scan_info = _ztorgj_decode_stock_payload(raw_scan_info)
            except Exception:
                scan_info = {}

        stocks = []
        for redis_code, raw_info in _ztorgj_load_hash_payloads(redis_client, stock_key):
            stock = dict(raw_info or {})
            code = _ztorgj_stock_simple_code(stock.get('code') or redis_code)
            if not re.match(r'^\d{6}$', code):
                continue
            if condition_field == 'five_pull_yin_pull_next_or_eaten':
                condition_value = stock.get(condition_field)
                if condition_value is None:
                    condition_value = stock.get('five_pull_yin_pull_next', 0)
                stock[condition_field] = _ztorgj_parse_int(condition_value, 0)
            if quanyang_filter:
                if 'is_quanyang_10d' in stock:
                    is_quanyang_10d = _ztorgj_parse_int(
                        stock.get('is_quanyang_10d'), 0
                    )
                elif 'yin_count_10d' in stock:
                    is_quanyang_10d = 1 if _ztorgj_parse_int(
                        stock.get('yin_count_10d'), -1
                    ) == 0 else 0
                else:
                    is_quanyang_10d = 0
                if is_quanyang_10d != 1:
                    continue
                stock['is_quanyang_10d'] = 1
            elif condition_field and _ztorgj_parse_int(stock.get(condition_field), 0) != 1:
                continue
            stock['code'] = code
            stock.setdefault('name', code)
            display_gain = stock.get('today_change')
            if display_gain is None:
                display_gain = stock.get('ref_gain', 0)
            stock['_display_gain'] = _ztorgj_parse_float(display_gain, 0.0)
            stocks.append(stock)
        stocks.sort(key=lambda item: (-item['_display_gain'], item.get('code', '')))

        result = []
        missing_codes = []
        updated_times = []
        for stock in stocks:
            code = stock['code']
            kline_payload = _pz_load_kline_payload(
                redis_client, code, date_compact, days=days
            )
            rows = kline_payload.get('rows', []) if kline_payload else []
            updated_at = kline_payload.get('updated_at', '') if kline_payload else ''
            if not rows:
                missing_codes.append(code)
            if updated_at:
                updated_times.append(updated_at)
            stock.pop('_display_gain', None)
            five_pull_yin_pull_next_or_eaten_value = stock.get(
                'five_pull_yin_pull_next_or_eaten'
            )
            if five_pull_yin_pull_next_or_eaten_value is None:
                five_pull_yin_pull_next_or_eaten_value = stock.get(
                    'five_pull_yin_pull_next', 0
                )
            five_pull_yin_pull_next_or_eaten = _ztorgj_parse_int(
                five_pull_yin_pull_next_or_eaten_value, 0
            )
            stock['five_pull_yin_pull_next_or_eaten'] = (
                five_pull_yin_pull_next_or_eaten
            )
            result.append({
                'code': code,
                'name': str(stock.get('name') or code),
                'ref_gain': _ztorgj_parse_float(stock.get('ref_gain'), 0.0),
                'today_change': _ztorgj_parse_float(
                    stock.get('today_change') if stock.get('today_change') is not None else stock.get('ref_gain'),
                    0.0,
                ),
                'yin_count_20': _ztorgj_parse_int(stock.get('yin_count_20'), 0),
                'yin_count_10d': _ztorgj_parse_int(stock.get('yin_count_10d'), 0),
                'is_quanyang_10d': _ztorgj_parse_int(
                    stock.get('is_quanyang_10d'), 0
                ),
                'condition_2_met': _ztorgj_parse_int(stock.get('condition_2_met'), 0),
                'condition_3_met': _ztorgj_parse_int(stock.get('condition_3_met'), 0),
                'condition_4_met': _ztorgj_parse_int(stock.get('condition_4_met'), 0),
                'five_pull_all_yin_up': _ztorgj_parse_int(
                    stock.get('five_pull_all_yin_up'), 0
                ),
                'five_pull_all_yin_no_touch': _ztorgj_parse_int(
                    stock.get('five_pull_all_yin_no_touch'), 0
                ),
                'five_pull_all_yin_cross_star': _ztorgj_parse_int(
                    stock.get('five_pull_all_yin_cross_star'), 0
                ),
                'five_pull_yin_pull_next': _ztorgj_parse_int(
                    stock.get('five_pull_yin_pull_next'), 0
                ),
                'five_pull_yin_pull_next_or_eaten': (
                    five_pull_yin_pull_next_or_eaten
                ),
                'five_pull_all_yin_eaten_next': _ztorgj_parse_int(
                    stock.get('five_pull_all_yin_eaten_next'), 0
                ),
                'five_pull_groups': list(stock.get('five_pull_groups') or []),
                'stock': stock,
                'kline_updated_at': updated_at,
                'kline_data': rows,
            })

        payload = {
            'success': True,
            'date': display_date,
            'days': days,
            'count': len(result),
            'kline_count': len(result) - len(missing_codes),
            'scan_info': scan_info,
            'kline_updated_at': max(updated_times) if updated_times else '',
            'missing_codes': missing_codes,
            'data': result,
            'redis_key': stock_key,
            'kline_history_redis_key': _pz_history_kline_key(date_compact),
            'kline_realtime_redis_key': _pz_realtime_kline_key(date_compact),
        }
        if condition_field:
            payload.update({
                'condition': 'quanyang' if quanyang_filter else condition_no,
                'condition_field': condition_field,
                'condition_name': condition_name,
            })
        return no_cache_response(payload)
    except Exception as e:
        print(f"[pz][klines] 获取失败: {e}", flush=True)
        return no_cache_response({
            'success': False,
            'error': str(e),
            'data': [],
        }), 500


@app.route('/api/10above5/stocks')
@app.route('/api/10above5/stocks/<date>')
def get_10above5_stocks(date=None):
    """读取10above5:{date}，按收盘价大于MA5次数降序、涨跌幅降序返回"""
    try:
        if not date:
            date = request.args.get('date')
        date_compact, display_date = _yinbreak5_normalize_date(date)
        key = f"10above5:{date_compact}"
        r = get_redis_client()

        if not r.exists(key) or r.type(key) != 'hash':
            return no_cache_response({
                'success': True,
                'data': [],
                'date': display_date,
                'count': 0,
                'scan_info': None,
                'message': f'没有找到数据key: {key}'
            })

        rows = []
        scan_info = None
        cursor = 0
        while True:
            cursor, batch = r.hscan(key, cursor, count=200)
            for code, data_str in batch.items():
                try:
                    if isinstance(code, bytes):
                        code = code.decode('utf-8', errors='ignore')
                    if code in ('_scan_info', '_timestamp', '_snapshot_info'):
                        if code == '_scan_info':
                            scan_info = _yinbreak5_decode_payload(data_str)
                        continue
                    info = _yinbreak5_decode_payload(data_str)
                    if isinstance(info, dict):
                        rows.append(_yinbreak5_normalize_stock(code, info))
                except Exception as item_error:
                    print(f"[10above5] 解析股票数据失败 code={code} error={item_error}", flush=True)
            if cursor == 0:
                break

        rows.sort(key=lambda item: (-item.get('close_above_ma5_count_10d', 0), -item.get('today_change', 0)))
        return no_cache_response({
            'success': True,
            'data': rows,
            'date': display_date,
            'count': len(rows),
            'redis_key': key,
            'scan_info': scan_info,
            'update_time': datetime.now().strftime('%H:%M:%S')
        })
    except Exception as e:
        print(f"[10above5] 获取数据失败: {e}", flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': []})


@app.route('/api/10above5/klines')
def get_10above5_klines():
    """返回10天大5日线两列股票名称及其QMT日K OHLC数据。"""
    try:
        date_compact, display_date = _yinbreak5_normalize_date(request.args.get('date'))
        try:
            days = int(request.args.get('days') or 48)
        except (TypeError, ValueError):
            days = 48
        days = max(1, min(days, 120))

        stock_key = f'10above5:{date_compact}'
        kline_key = _ztorgj_api_realtime_kline_key(date_compact)
        r = get_redis_client()
        source_rows = _ztorgj_load_hash_payloads(r, stock_key)
        stocks = []
        for redis_code, info in source_rows:
            stock = _yinbreak5_normalize_stock(redis_code, info)
            count_10d = stock.get('close_above_ma5_count_10d', 0)
            code = _ztorgj_stock_simple_code(stock.get('code'))
            if count_10d not in (9, 10) or not re.match(r'^\d{6}$', code):
                continue
            stocks.append(stock)
        stocks.sort(key=lambda item: (
            -item.get('close_above_ma5_count_10d', 0),
            -item.get('today_change', 0),
            item.get('code', ''),
        ))

        result = []
        missing_codes = []
        kline_updated_times = []
        kline_snapshot_times = []
        for stock in stocks:
            code = _ztorgj_stock_simple_code(stock.get('code'))
            reference_flags = _kline_api_reference_flags(stock)
            payload, _redis_key, _redis_field = _ztorgj_load_api_kline_payload(
                code, date_compact, days=days
            )
            ohlc_rows = _ztorgj_public_ohlc_rows(payload.get('data')) if payload else []
            if not ohlc_rows:
                missing_codes.append(code)
                continue
            updated_at = str(payload.get('updated_at') or '').strip()
            snapshot_at = str(payload.get('snapshot_at') or '').strip()
            published_at = str(
                payload.get('published_at') or updated_at
            ).strip()
            if updated_at:
                kline_updated_times.append(updated_at)
            if snapshot_at:
                kline_snapshot_times.append(snapshot_at)
            result.append({
                'code': code,
                'name': str(stock.get('name') or payload.get('stock', {}).get('name') or code),
                'close_above_ma5_count_10d': stock.get('close_above_ma5_count_10d', 0),
                'low_above_ma5_count': _ztorgj_parse_int(stock.get('low_above_ma5_count'), 0),
                'ref_high_gain_ok': reference_flags['ref_high_gain_ok'],
                'ref_is_limit_up': reference_flags['ref_is_limit_up'],
                'updated_at': updated_at,
                'kline_snapshot_at': snapshot_at,
                'kline_published_at': published_at,
                'data': ohlc_rows,
            })

        if not result:
            response = no_cache_response({
                'success': False,
                'error': '没有找到有效的10天大5日线K线数据',
                'date': display_date,
                'days': days,
                'count': 0,
                'data': [],
                'missing_codes': missing_codes,
                'redis_key': stock_key,
                'kline_redis_key': kline_key,
            })
            response.status_code = 404
            return response

        plaintext_payload = {
            'success': True,
            'date': display_date,
            'days': days,
            'count': len(result),
            'kline_updated_at': max(kline_updated_times) if kline_updated_times else '',
            'kline_oldest_snapshot_at': min(kline_snapshot_times) if kline_snapshot_times else '',
            'kline_newest_snapshot_at': max(kline_snapshot_times) if kline_snapshot_times else '',
            'kline_stale_count': _kline_api_stale_count(kline_snapshot_times),
            'data': result,
            'missing_codes': missing_codes,
            'redis_key': stock_key,
            'kline_redis_key': kline_key,
        }
        try:
            encrypted_payload = _encrypt_10above5_kline_payload(plaintext_payload)
        except RuntimeError as encryption_error:
            print(f"[10above5][klines] 加密配置失败: {encryption_error}", flush=True)
            response = no_cache_response({
                'success': False,
                'encrypted': False,
                'error': 'K线接口加密配置不可用',
            })
            response.status_code = 503
            return response
        return no_cache_response(encrypted_payload)
    except Exception as e:
        print(f"[10above5][klines] 获取数据失败: {e}", flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': []}), 500


@app.route('/api/10above5/realtime', methods=['POST'])
def get_10above5_realtime():
    """10天大5日线实时涨跌幅接口"""
    try:
        data = request.get_json() or {}
        codes = data.get('codes', [])
        quotes = get_tencent_batch_quotes(codes)
        realtime_data = {}
        for code in codes:
            simple_code = _normalize_stock_code_text(code)
            quote = quotes.get(simple_code)
            if quote:
                realtime_data[code] = {
                    'change': round(_yinbreak5_parse_float(quote.get('change_pct'), 0.0), 2),
                    'price': quote.get('price', 0)
                }
        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        print(f"[10above5][realtime] 获取失败: {e}", flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': {}})


@app.route('/api/yxcs/klines')
@app.route('/api/yxcs/klines/plain')
def get_yxcs_klines():
    """返回20日阴线次数股票字段及QMT日K，支持加密和明文路由。"""
    try:
        date_compact, display_date = _yinbreak5_normalize_date(request.args.get('date'))
        try:
            days = int(request.args.get('days') or 48)
        except (TypeError, ValueError):
            days = 48
        days = max(1, min(days, 120))

        stock_key = f'yxcs:{date_compact}'
        kline_key = _ztorgj_api_realtime_kline_key(date_compact)
        r = get_redis_client()
        source_rows = _ztorgj_load_hash_payloads(r, stock_key)
        source_rows.sort(key=lambda pair: (
            _ztorgj_parse_int((pair[1] or {}).get('yin_count_20'), 0),
            -_ztorgj_parse_float(
                (pair[1] or {}).get('today_change', (pair[1] or {}).get('ref_gain', 0)),
                0.0,
            ),
            _ztorgj_stock_simple_code((pair[1] or {}).get('code') or pair[0]),
        ))

        if not source_rows:
            response = no_cache_response({
                'success': False,
                'error': f'没有找到20日阴线次数数据: {stock_key}',
                'data': [],
            })
            response.status_code = 404
            return response

        result = []
        missing_codes = []
        kline_updated_times = []
        kline_snapshot_times = []
        kline_count = 0
        for redis_code, info in source_rows:
            stock_fields = dict(info or {})
            reference_flags = _kline_api_reference_flags(stock_fields)
            code = _ztorgj_stock_simple_code(stock_fields.get('code') or redis_code)
            name = str(stock_fields.get('name') or stock_fields.get('stock_name') or code)
            payload = None
            if re.match(r'^\d{6}$', code):
                payload, _redis_key, _redis_field = _ztorgj_load_api_kline_payload(
                    code, date_compact, days=days
                )
            ohlc_rows = _ztorgj_public_ohlc_rows(payload.get('data')) if payload else []
            updated_at = str(payload.get('updated_at') or '').strip() if payload else ''
            snapshot_at = str(payload.get('snapshot_at') or '').strip() if payload else ''
            published_at = str(
                payload.get('published_at') or updated_at
            ).strip() if payload else ''
            if ohlc_rows:
                kline_count += 1
                if updated_at:
                    kline_updated_times.append(updated_at)
                if snapshot_at:
                    kline_snapshot_times.append(snapshot_at)
            else:
                missing_codes.append(code)
            result.append({
                'code': code,
                'name': name,
                'stock': stock_fields,
                'ref_high_gain_ok': reference_flags['ref_high_gain_ok'],
                'ref_is_limit_up': reference_flags['ref_is_limit_up'],
                'kline_updated_at': updated_at,
                'kline_snapshot_at': snapshot_at,
                'kline_published_at': published_at,
                'kline_data': ohlc_rows,
            })

        plaintext_payload = {
            'success': True,
            'date': display_date,
            'days': days,
            'count': len(result),
            'kline_count': kline_count,
            'kline_updated_at': max(kline_updated_times) if kline_updated_times else '',
            'kline_oldest_snapshot_at': min(kline_snapshot_times) if kline_snapshot_times else '',
            'kline_newest_snapshot_at': max(kline_snapshot_times) if kline_snapshot_times else '',
            'kline_stale_count': _kline_api_stale_count(kline_snapshot_times),
            'data': result,
            'missing_codes': missing_codes,
            'redis_key': stock_key,
            'kline_redis_key': kline_key,
        }
        if request.path.endswith('/plain'):
            return no_cache_response(plaintext_payload)
        try:
            encrypted_payload = _encrypt_kline_api_payload(
                plaintext_payload, _YXCS_KLINE_API_AES_AAD
            )
        except RuntimeError as encryption_error:
            print(f"[yxcs][klines] 加密配置失败: {encryption_error}", flush=True)
            response = no_cache_response({
                'success': False,
                'encrypted': False,
                'error': 'K线接口加密配置不可用',
            })
            response.status_code = 503
            return response
        return no_cache_response(encrypted_payload)
    except Exception as e:
        print(f"[yxcs][klines] 获取数据失败: {e}", flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': []}), 500


@app.route('/api/special5/klines')
def get_special5_klines():
    """加密返回特别关注5全市场股票字段及对应QMT日K。"""
    try:
        date_compact, display_date = _yinbreak5_normalize_date(request.args.get('date'))
        try:
            days = int(request.args.get('days') or 60)
        except (TypeError, ValueError):
            days = 60
        days = max(1, min(days, 120))

        stock_key = f'special5:{date_compact}'
        fourabove5_key = f'4above5:{date_compact}'
        kline_key = _ztorgj_api_realtime_kline_key(date_compact)
        r = get_redis_client()
        source_rows = _special5_merge_source_rows(
            _ztorgj_load_hash_payloads(r, stock_key),
            _ztorgj_load_hash_payloads(r, fourabove5_key),
        )
        source_rows.sort(key=lambda row: (
            -_ztorgj_parse_float(
                (row['info'] or {}).get(
                    'today_change', (row['info'] or {}).get('ref_gain', 0)
                ),
                0.0,
            ),
            row['code'],
        ))

        if not source_rows:
            response = no_cache_response({
                'success': False,
                'error': f'没有找到特别关注5数据: {stock_key} / {fourabove5_key}',
                'data': [],
            })
            response.status_code = 404
            return response

        result = []
        missing_codes = []
        kline_updated_times = []
        kline_snapshot_times = []
        kline_count = 0
        for source_row in source_rows:
            stock_fields = dict(source_row['info'] or {})
            reference_flags = _kline_api_reference_flags(stock_fields)
            code = source_row['code']
            name = str(stock_fields.get('name') or stock_fields.get('stock_name') or code)
            payload = None
            if re.match(r'^\d{6}$', code):
                payload, _redis_key, _redis_field = _ztorgj_load_api_kline_payload(
                    code, date_compact, days=days
                )
            ohlc_rows = _ztorgj_public_ohlc_rows(payload.get('data')) if payload else []
            updated_at = str(payload.get('updated_at') or '').strip() if payload else ''
            snapshot_at = str(payload.get('snapshot_at') or '').strip() if payload else ''
            published_at = str(
                payload.get('published_at') or updated_at
            ).strip() if payload else ''
            if ohlc_rows:
                kline_count += 1
                if updated_at:
                    kline_updated_times.append(updated_at)
                if snapshot_at:
                    kline_snapshot_times.append(snapshot_at)
            else:
                missing_codes.append(code)
            result.append({
                'code': code,
                'name': name,
                'stock': stock_fields,
                'special5_member': source_row['special5_member'],
                'fourabove5_member': source_row['fourabove5_member'],
                'ref_high_gain_ok': reference_flags['ref_high_gain_ok'],
                'ref_is_limit_up': reference_flags['ref_is_limit_up'],
                'kline_updated_at': updated_at,
                'kline_snapshot_at': snapshot_at,
                'kline_published_at': published_at,
                'kline_data': ohlc_rows,
            })

        plaintext_payload = {
            'success': True,
            'date': display_date,
            'days': days,
            'count': len(result),
            'kline_count': kline_count,
            'kline_updated_at': max(kline_updated_times) if kline_updated_times else '',
            'kline_oldest_snapshot_at': min(kline_snapshot_times) if kline_snapshot_times else '',
            'kline_newest_snapshot_at': max(kline_snapshot_times) if kline_snapshot_times else '',
            'kline_stale_count': _kline_api_stale_count(kline_snapshot_times),
            'data': result,
            'missing_codes': missing_codes,
            'redis_key': stock_key,
            'fourabove5_redis_key': fourabove5_key,
            'kline_redis_key': kline_key,
        }
        try:
            encrypted_payload = _encrypt_kline_api_payload(
                plaintext_payload, _SPECIAL5_KLINE_API_AES_AAD
            )
        except RuntimeError as encryption_error:
            print(f"[special5][klines] 加密配置失败: {encryption_error}", flush=True)
            response = no_cache_response({
                'success': False,
                'encrypted': False,
                'error': 'K线接口加密配置不可用',
            })
            response.status_code = 503
            return response
        return no_cache_response(encrypted_payload)
    except Exception as e:
        print(f"[special5][klines] 获取数据失败: {e}", flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': []}), 500


@app.route('/api/20xiasha/klines')
def get_20xiasha_klines():
    """加密返回20日下穿MA5累计股票的全部Redis字段及对应QMT日K。"""
    try:
        date_compact, display_date = _yinbreak5_normalize_date(request.args.get('date'))
        try:
            days = int(request.args.get('days') or 60)
        except (TypeError, ValueError):
            days = 60
        days = max(1, min(days, 120))

        stock_key = f'20xiasha:{date_compact}'
        kline_key = _ztorgj_api_realtime_kline_key(date_compact)
        r = get_redis_client()
        source_rows = []
        for raw_code, raw_value in (r.hgetall(stock_key) or {}).items():
            redis_code = (
                raw_code.decode('utf-8', errors='ignore')
                if isinstance(raw_code, bytes) else str(raw_code)
            )
            if redis_code.startswith('_'):
                continue
            try:
                info = _ztorgj_decode_stock_payload(raw_value)
            except Exception as decode_error:
                print(
                    f"[20xiasha][klines] 股票数据解析失败 code={redis_code}: {decode_error}",
                    flush=True,
                )
                continue
            if isinstance(info, dict):
                source_rows.append((redis_code, info))
        source_rows.sort(key=lambda pair: (
            0 if _ztorgj_truthy_flag((pair[1] or {}).get('is_high_gain')) else 1,
            _ztorgj_parse_float(
                (pair[1] or {}).get('close_below_ma5_abs_pct_sum_20d'), 0.0
            ),
            -_ztorgj_parse_float(
                (pair[1] or {}).get('today_change', (pair[1] or {}).get('ref_gain', 0)),
                0.0,
            ),
            _ztorgj_stock_simple_code((pair[1] or {}).get('code') or pair[0]),
        ))

        if not source_rows:
            response = no_cache_response({
                'success': False,
                'error': f'没有找到20日下穿MA5累计数据: {stock_key}',
                'data': [],
            })
            response.status_code = 404
            return response

        result = []
        missing_codes = []
        kline_updated_times = []
        kline_snapshot_times = []
        kline_count = 0
        for redis_code, info in source_rows:
            raw_stock_fields = dict(info or {})
            code = _ztorgj_stock_simple_code(raw_stock_fields.get('code') or redis_code)
            name = str(raw_stock_fields.get('name') or raw_stock_fields.get('stock_name') or code)
            is_high_gain = _ztorgj_truthy_flag(raw_stock_fields.get('is_high_gain'))
            close_below_sum = _ztorgj_parse_float(
                raw_stock_fields.get('close_below_ma5_abs_pct_sum_20d'), 0.0
            )
            ref_gain = _ztorgj_parse_float(raw_stock_fields.get('ref_gain'), 0.0)
            today_change = _ztorgj_parse_float(
                raw_stock_fields.get('today_change', ref_gain), ref_gain
            )
            raw_purple_reasons = raw_stock_fields.get('purple_reasons')
            if isinstance(raw_purple_reasons, (list, tuple)):
                purple_reasons = [str(reason) for reason in raw_purple_reasons]
            elif raw_purple_reasons:
                purple_reasons = [str(raw_purple_reasons)]
            else:
                purple_reasons = []
            stock_fields = {
                'code': code,
                'name': name,
                'stock_code_with_market': str(
                    raw_stock_fields.get('stock_code_with_market') or ''
                ),
                'ref_gain': ref_gain,
                'today_change': today_change,
                'is_high_gain': bool(is_high_gain),
                'close_below_ma5_abs_pct_sum_20d': close_below_sum,
                'low_above_ma5_count_20d': _ztorgj_parse_int(
                    raw_stock_fields.get('low_above_ma5_count_20d'), 0
                ),
                'low_above_ma5_count_10d': _ztorgj_parse_int(
                    raw_stock_fields.get('low_above_ma5_count_10d'), 0
                ),
                'purple_match': 1 if _ztorgj_truthy_flag(
                    raw_stock_fields.get('purple_match')
                ) else 0,
                'group_color': str(raw_stock_fields.get('group_color') or ''),
                'group_name': str(raw_stock_fields.get('group_name') or ''),
                'purple_reasons': purple_reasons,
            }
            kline_payload = None
            if re.match(r'^\d{6}$', code):
                kline_payload, _redis_key, _redis_field = (
                    _ztorgj_load_api_kline_payload(
                        code, date_compact, days=days
                    )
                )
            kline_rows = (
                _ztorgj_public_ohlc_rows(kline_payload.get('data'))
                if kline_payload else []
            )
            snapshot_at = (
                str(kline_payload.get('snapshot_at') or '').strip()
                if kline_payload else ''
            )
            published_at = (
                str(
                    kline_payload.get('published_at')
                    or kline_payload.get('updated_at')
                    or ''
                ).strip()
                if kline_payload else ''
            )
            if kline_rows:
                kline_count += 1
                if snapshot_at:
                    kline_snapshot_times.append(snapshot_at)
                if published_at:
                    kline_updated_times.append(published_at)
            else:
                missing_codes.append(code)
            stock_fields.update({
                'kline_data': kline_rows,
                'kline_snapshot_at': snapshot_at,
                'kline_published_at': published_at,
            })
            result.append(stock_fields)

        plaintext_payload = {
            'success': True,
            'date': display_date,
            'days': days,
            'count': len(result),
            'kline_count': kline_count,
            'kline_updated_at': max(kline_updated_times) if kline_updated_times else '',
            'kline_oldest_snapshot_at': min(kline_snapshot_times) if kline_snapshot_times else '',
            'kline_newest_snapshot_at': max(kline_snapshot_times) if kline_snapshot_times else '',
            'kline_stale_count': _kline_api_stale_count(kline_snapshot_times),
            'data': result,
            'missing_codes': missing_codes,
            'redis_key': stock_key,
            'kline_redis_key': kline_key,
        }
        try:
            encrypted_payload = _encrypt_kline_api_payload(
                plaintext_payload, _20XIASHA_KLINE_API_AES_AAD
            )
        except RuntimeError as encryption_error:
            print(f"[20xiasha][klines] 加密配置失败: {encryption_error}", flush=True)
            response = no_cache_response({
                'success': False,
                'encrypted': False,
                'error': 'K线接口加密配置不可用',
            })
            response.status_code = 503
            return response
        return no_cache_response(encrypted_payload)
    except Exception as e:
        print(f"[20xiasha][klines] 获取数据失败: {e}", flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': []}), 500


@app.route('/api/5lsabove10/klines')
def get_5lsabove10_klines():
    """加密返回5lsabove10股票的全部Redis字段及对应QMT日K。"""
    try:
        date_compact, display_date = _yinbreak5_normalize_date(request.args.get('date'))
        try:
            days = int(request.args.get('days') or 48)
        except (TypeError, ValueError):
            days = 48
        days = max(1, min(days, 120))

        stock_key = f'5lsabove10:{date_compact}'
        kline_key = _ztorgj_api_realtime_kline_key(date_compact)
        r = get_redis_client()
        source_rows = _ztorgj_load_hash_payloads(r, stock_key)
        source_rows.sort(key=lambda pair: (
            _ztorgj_parse_int((pair[1] or {}).get('yin_count_10d'), 0),
            -_ztorgj_parse_int((pair[1] or {}).get('high_gain_10d_count'), 0),
            -_ztorgj_parse_float((pair[1] or {}).get('ref_gain'), 0.0),
            _ztorgj_stock_simple_code((pair[1] or {}).get('code') or pair[0]),
        ))

        if not source_rows:
            response = no_cache_response({
                'success': False,
                'error': f'没有找到5lsabove10数据: {stock_key}',
                'data': [],
            })
            response.status_code = 404
            return response

        result = []
        missing_codes = []
        kline_updated_times = []
        kline_snapshot_times = []
        kline_count = 0
        for redis_code, info in source_rows:
            stock_fields = dict(info or {})
            reference_flags = _kline_api_reference_flags(stock_fields)
            code = _ztorgj_stock_simple_code(stock_fields.get('code') or redis_code)
            name = str(stock_fields.get('name') or stock_fields.get('stock_name') or code)
            payload = None
            if re.match(r'^\d{6}$', code):
                payload, _redis_key, _redis_field = _ztorgj_load_api_kline_payload(
                    code, date_compact, days=days
                )
            ohlc_rows = _ztorgj_public_ohlc_rows(payload.get('data')) if payload else []
            updated_at = str(payload.get('updated_at') or '').strip() if payload else ''
            snapshot_at = str(payload.get('snapshot_at') or '').strip() if payload else ''
            published_at = str(
                payload.get('published_at') or updated_at
            ).strip() if payload else ''
            if ohlc_rows:
                kline_count += 1
                if updated_at:
                    kline_updated_times.append(updated_at)
                if snapshot_at:
                    kline_snapshot_times.append(snapshot_at)
            else:
                missing_codes.append(code)
            result.append({
                'code': code,
                'name': name,
                'stock': stock_fields,
                'ref_high_gain_ok': reference_flags['ref_high_gain_ok'],
                'ref_is_limit_up': reference_flags['ref_is_limit_up'],
                'kline_updated_at': updated_at,
                'kline_snapshot_at': snapshot_at,
                'kline_published_at': published_at,
                'kline_data': ohlc_rows,
            })

        plaintext_payload = {
            'success': True,
            'date': display_date,
            'days': days,
            'count': len(result),
            'kline_count': kline_count,
            'kline_updated_at': max(kline_updated_times) if kline_updated_times else '',
            'kline_oldest_snapshot_at': min(kline_snapshot_times) if kline_snapshot_times else '',
            'kline_newest_snapshot_at': max(kline_snapshot_times) if kline_snapshot_times else '',
            'kline_stale_count': _kline_api_stale_count(kline_snapshot_times),
            'data': result,
            'missing_codes': missing_codes,
            'redis_key': stock_key,
            'kline_redis_key': kline_key,
        }
        try:
            encrypted_payload = _encrypt_kline_api_payload(
                plaintext_payload, _5LSABOVE10_KLINE_API_AES_AAD
            )
        except RuntimeError as encryption_error:
            print(f"[5lsabove10][klines] 加密配置失败: {encryption_error}", flush=True)
            response = no_cache_response({
                'success': False,
                'encrypted': False,
                'error': 'K线接口加密配置不可用',
            })
            response.status_code = 503
            return response
        return no_cache_response(encrypted_payload)
    except Exception as e:
        print(f"[5lsabove10][klines] 获取数据失败: {e}", flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': []}), 500


def _ths_optional_float(value):
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ths_optional_int(value):
    if value in (None, ''):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _ths_stock_display_gain(stock):
    value = stock.get('today_change')
    if value in (None, ''):
        value = stock.get('ref_gain')
    return _ztorgj_parse_float(value, 0.0)


@app.route('/api/ths/klines')
@app.route('/api/ths/klines/plain')
def get_ths_klines():
    """返回同花顺板块、对应个股及QMT日K，支持加密和明文路由。"""
    try:
        date_compact, display_date = _yinbreak5_normalize_date(request.args.get('date'))
        try:
            days = int(request.args.get('days') or 30)
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 120))

        redis_key = f'thscleaning:{date_compact}'
        r = get_redis_client()
        raw_cleaning = r.hgetall(redis_key) or {}
        if not raw_cleaning:
            response = no_cache_response({
                'success': False,
                'error': f'没有找到同花顺清洗数据: {redis_key}',
                'data': [],
            })
            response.status_code = 404
            return response

        kline_key = _ztorgj_qmt_kline_key(date_compact)
        kline_ref_date = date_compact

        scan_info = {}
        raw_stocks = []
        for raw_field, raw_value in raw_cleaning.items():
            field = (
                raw_field.decode('utf-8', errors='ignore')
                if isinstance(raw_field, bytes) else str(raw_field)
            )
            if field == '_scan_info':
                decoded_scan_info = _ztorgj_decode_stock_payload(raw_value)
                if isinstance(decoded_scan_info, dict):
                    scan_info = decoded_scan_info
                continue
            if field.startswith('_'):
                continue
            stock_fields = _ztorgj_decode_stock_payload(raw_value)
            if not isinstance(stock_fields, dict):
                continue
            stock_fields = dict(stock_fields)
            stock_fields['code'] = _ztorgj_stock_simple_code(
                stock_fields.get('code') or field
            )
            raw_stocks.append(stock_fields)

        meta = scan_info if isinstance(scan_info, dict) else {}
        board_change_by_name = {}
        board_limit_up_count_by_name = {}
        source_key = str(meta.get('source_key') or '').strip()
        if source_key:
            try:
                source_payload = _ths_decode_payload(r.get(source_key)) or {}
                for board in source_payload.get('boards') or []:
                    board_name = str((board or {}).get('board_name') or '').strip()
                    if board_name:
                        board_change_by_name[board_name] = _ths_optional_float(
                            (board or {}).get('change_percent')
                        )
                        limit_up_count = _ths_optional_int(
                            (board or {}).get('limit_up_count')
                        )
                        if limit_up_count is not None:
                            board_limit_up_count_by_name[board_name] = limit_up_count
            except Exception as source_error:
                print(
                    f"[ths][klines] 板块涨幅解析失败 key={source_key} "
                    f"error={source_error}",
                    flush=True,
                )

        board_order = []
        boards_by_name = {}
        for stock_fields in raw_stocks:
            board_names = stock_fields.get('board_names') or []
            if isinstance(board_names, str):
                board_names = [board_names]
            board_names = [
                str(name).strip() for name in board_names if str(name).strip()
            ]
            if not board_names:
                fallback = str(stock_fields.get('board_name') or '').strip()
                board_names = [fallback or '未分类']
            seen_board_names = set()
            for board_name in board_names:
                if board_name in seen_board_names:
                    continue
                seen_board_names.add(board_name)
                if board_name not in boards_by_name:
                    boards_by_name[board_name] = {
                        'board_name': board_name,
                        'stocks': [],
                    }
                    board_order.append(board_name)
                boards_by_name[board_name]['stocks'].append(dict(stock_fields))

        kline_cache = {}
        missing_codes = set()
        unique_codes = set()
        kline_updated_times = []

        def load_kline(code):
            if code in kline_cache:
                return kline_cache[code]
            raw_kline = r.hget(kline_key, code)
            if not raw_kline:
                result = ('', [])
                kline_cache[code] = result
                missing_codes.add(code)
                return result
            try:
                kline_payload = _ztorgj_decode_stock_payload(raw_kline)
                ts_code = str(
                    kline_payload.get('ts_code')
                    or kline_payload.get('stock_code_with_market')
                    or _ztorgj_normalize_ts_code(code)
                )
                rows = _ztorgj_format_kline_rows(
                    kline_payload.get('rows') or kline_payload.get('data') or [],
                    ts_code,
                    days,
                    kline_ref_date,
                )
                ohlc_rows = _ztorgj_public_ohlc_rows(rows)
                updated_at = str(kline_payload.get('updated_at') or '').strip()
            except Exception as item_error:
                print(f"[ths][klines] K线解析失败 code={code} error={item_error}", flush=True)
                updated_at, ohlc_rows = '', []
            if not ohlc_rows:
                missing_codes.add(code)
            elif updated_at:
                kline_updated_times.append(updated_at)
            result = (updated_at, ohlc_rows)
            kline_cache[code] = result
            return result

        result_boards = []
        association_count = 0
        for board_name in board_order:
            board_result = dict(boards_by_name[board_name])
            enriched_stocks = []
            for stock_fields in board_result.pop('stocks', []):
                stock_result = dict(stock_fields)
                code = _ztorgj_stock_simple_code(stock_result.get('code'))
                stock_result['code'] = code
                association_count += 1
                if re.match(r'^\d{6}$', code):
                    unique_codes.add(code)
                    updated_at, ohlc_rows = load_kline(code)
                else:
                    updated_at, ohlc_rows = '', []
                stock_result['kline_updated_at'] = updated_at
                stock_result['kline_data'] = ohlc_rows
                enriched_stocks.append(stock_result)
            enriched_stocks.sort(key=lambda stock: (
                -_ths_stock_display_gain(stock),
                str(stock.get('code') or ''),
            ))
            board_result['board_change_percent'] = board_change_by_name.get(board_name)
            limit_up_count = board_limit_up_count_by_name.get(board_name)
            if limit_up_count is None:
                fallback_counts = []
                for stock in enriched_stocks:
                    per_board_counts = stock.get('board_limit_up_counts')
                    if isinstance(per_board_counts, dict):
                        fallback_count = _ths_optional_int(per_board_counts.get(board_name))
                    else:
                        fallback_count = None
                    if fallback_count is None:
                        stock_board_names = stock.get('board_names') or []
                        if isinstance(stock_board_names, str):
                            stock_board_names = [stock_board_names]
                        if stock_board_names == [board_name]:
                            fallback_count = _ths_optional_int(stock.get('limit_up_count'))
                    if fallback_count is not None:
                        fallback_counts.append(fallback_count)
                if fallback_counts:
                    limit_up_count = max(fallback_counts)
            board_result['limit_up_count'] = limit_up_count
            board_result['stock_count'] = len(enriched_stocks)
            board_result['stocks'] = enriched_stocks
            result_boards.append(board_result)

        result_boards.sort(key=lambda board: (
            board.get('board_change_percent') is None,
            -(board.get('board_change_percent') or 0.0),
            str(board.get('board_name') or ''),
        ))

        plaintext_payload = {
            'success': True,
            'date': display_date,
            'days': days,
            'redis_key': redis_key,
            'kline_redis_key': kline_key,
            'kline_ref_date': _ths_format_date(kline_ref_date),
            'board_count': len(result_boards),
            'stock_count': association_count,
            'unique_stock_count': len(unique_codes),
            'kline_count': sum(1 for _updated_at, rows in kline_cache.values() if rows),
            'kline_updated_at': max(kline_updated_times) if kline_updated_times else '',
            'missing_codes': sorted(missing_codes),
            'meta': meta,
            'boards': result_boards,
        }
        if request.path.endswith('/plain'):
            return no_cache_response(plaintext_payload)
        try:
            encrypted_payload = _encrypt_kline_api_payload(
                plaintext_payload, _THS_KLINE_API_AES_AAD
            )
        except RuntimeError as encryption_error:
            print(f"[ths][klines] 加密配置失败: {encryption_error}", flush=True)
            response = no_cache_response({
                'success': False,
                'encrypted': False,
                'error': 'K线接口加密配置不可用',
            })
            response.status_code = 503
            return response
        return no_cache_response(encrypted_payload)
    except Exception as e:
        print(f"[ths][klines] 获取数据失败: {e}", flush=True)
        return jsonify({'success': False, 'error': str(e), 'data': []}), 500

# ========== 5拉升阴前后强页面 ==========
# 暂时隐藏
# @app.route('/5拉升阴前后强')
# def ls_yqhq_page():
#     """5拉升阴前后强页面"""
#     return render_template('5lsyqhq.html')

# 暂时隐藏
"""
@app.route('/api/5lsyqhq/stocks')
def get_5lsyqhq_stocks(date=None):
    \"\"\"获取5拉升阴前后强股票列表\"\"\"
    try:
        import base64
        import time as time_module
        from flask import request
        
        # 记录开始时间
        start_time = time_module.time()
        
        r = get_redis_client()
        
        # 如果没有指定日期，使用今天
        if not date:
            date = request.args.get('date')
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
        
        # 转换日期格式
        today = date
        today_compact = date.replace('-', '')
        
        # Redis key格式: 5lsshiti:20260122
        key = f"5lsshiti:{today_compact}"
        
        # 测量网络延迟
        ping_start = time_module.time()
        r.ping()
        network_latency = (time_module.time() - ping_start) * 1000
        print(f"Redis网络延迟: {network_latency:.2f}ms")
        
        # 检查key是否存在
        if not r.exists(key):
            total_time = (time_module.time() - start_time) * 1000
            return jsonify({
                'success': True,
                'data': [],
                'performance': {
                    'total_time': f"{total_time:.2f}ms",
                    'network_latency': f"{network_latency:.2f}ms",
                    'count': 0
                },
                'message': f'没有找到数据key: {key}'
            })
        
        key_type = r.type(key)
        print(f"使用key: {key}, 类型: {key_type}")
        
        # 使用HSCAN获取数据
        scan_start = time_module.time()
        all_data = r.hgetall(key)
        scan_time = (time_module.time() - scan_start) * 1000
        print(f"数据获取耗时: {scan_time:.2f}ms, 获取{len(all_data)}条数据")
        
        if not all_data:
            total_time = (time_module.time() - start_time) * 1000
            return jsonify({
                'success': True, 
                'data': [],
                'performance': {
                    'total_time': f"{total_time:.2f}ms",
                    'network_latency': f"{network_latency:.2f}ms",
                    'count': 0
                }
            })
        
        # 解析数据
        parse_start = time_module.time()
        stocks = []
        
        for code, data_str in all_data.items():
            try:
                stock_info = json.loads(data_str)
                
                stocks.append({
                    'code': stock_info.get('code', code),
                    'name': stock_info.get('name', ''),
                    'strategies': ['5拉升阴前后强'],
                    'group': stock_info.get('group', '分组1'),  # 从Redis读取分组信息
                    'today_change': stock_info.get('current_gain', 0),
                    'ls_num': stock_info.get('pull_count_10', 0),
                    'yin_entity_sum': stock_info.get('yin_entity_sum', 0),
                    'market_cap': stock_info.get('market_cap', 0),
                    'warning_time': stock_info.get('time', '')
                })
            except Exception as e:
                print(f"解析股票数据失败 {code}: {e}")
                continue
        
        parse_time = (time_module.time() - parse_start) * 1000
        total_time = (time_module.time() - start_time) * 1000
        
        print(f"数据解析耗时: {parse_time:.2f}ms")
        print(f"总耗时: {total_time:.2f}ms")
        
        # 返回性能指标
        return jsonify({
            'success': True, 
            'data': stocks,
            'performance': {
                'total_time': f"{total_time:.2f}ms",
                'network_latency': f"{network_latency:.2f}ms",
                'scan_time': f"{scan_time:.2f}ms",
                'parse_time': f"{parse_time:.2f}ms",
                'count': len(stocks)
            },
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        print(f"获取5拉升阴前后强数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})
"""

"""
@app.route('/api/5lsyqhq/realtime', methods=['POST'])
def get_5lsyqhq_realtime():
    \"\"\"5拉升阴前后强实时涨跌幅接口\"\"\"
    try:
        from flask import request
        data = request.get_json()
        codes = data.get('codes', [])
        
        if not codes:
            return jsonify({'success': True, 'data': {}})
        
        # 获取实时行情
        realtime_data = {}
        for code in codes:
            try:
                # 转换股票代码格式用于腾讯接口
                if '.' in code:
                    if code.endswith('.SH'):
                        tencent_code = f"sh{code.replace('.SH', '')}"
                    elif code.endswith('.SZ'):
                        tencent_code = f"sz{code.replace('.SZ', '')}"
                    else:
                        tencent_code = f"sz{code.split('.')[0]}"
                else:
                    if code.startswith('6'):
                        tencent_code = f"sh{code}"
                    else:
                        tencent_code = f"sz{code}"
                
                url = f"http://qt.gtimg.cn/q={tencent_code}"
                response = requests.get(url, timeout=2)
                response.encoding = 'gbk'
                
                if response.text:
                    parts = response.text.split('~')
                    if len(parts) > 32:
                        current_price = float(parts[3])
                        prev_close = float(parts[4])
                        if prev_close > 0:
                            change_percent = ((current_price - prev_close) / prev_close) * 100
                            realtime_data[code] = {'change': round(change_percent, 2)}
            except Exception as e:
                print(f"获取{code}实时行情失败: {e}")
                continue
        
        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
"""

"""
@app.route('/api/5lsyqhq/market_cap/<code>')
def get_5lsyqhq_market_cap(code):
    \"\"\"获取单个股票的市值（使用Tushare）\"\"\"
    try:
        import tushare as ts
        
        # 转换股票代码格式
        if code.startswith('6'):
            ts_code = f"{code}.SH"
        elif code.startswith('0') or code.startswith('3'):
            ts_code = f"{code}.SZ"
        elif code.startswith('8') or code.startswith('4'):
            ts_code = f"{code}.BJ"
        else:
            ts_code = f"{code}.SZ"
        
        # 使用Tushare获取市值
        pro = ts.pro_api()
        df = pro.daily_basic(ts_code=ts_code, fields='ts_code,total_mv')
        
        if df is not None and not df.empty:
            # total_mv单位是万元，转换为亿元
            market_cap = float(df.iloc[0]['total_mv']) / 10000
            return jsonify({
                'success': True,
                'code': code,
                'market_cap': round(market_cap, 2)
            })
        else:
            return jsonify({'success': False, 'error': '无法获取市值数据'})
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
"""

"""
@app.route('/api/5lsyqhq/dates')
def get_5lsyqhq_dates():
    \"\"\"获取5拉升阴前后强所有有数据的日期\"\"\"
    try:
        r = get_redis_client()
        
        # 查找所有相关的key: 5lsshiti:*
        keys = r.keys('5lsshiti:*')
        
        dates = []
        
        # 处理格式: 5lsshiti:20260122
        for key in keys:
            if ':' in key and not key.endswith(':timestamp'):
                date_part = key.split(':', 1)[1]
                # 转换20260122 -> 2026-01-22
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        
        # 去重并排序
        dates = sorted(list(set(dates)), reverse=True)
        
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
"""

"""
@app.route('/api/5lsyqhq/check_update')
def check_5lsyqhq_update():
    \"\"\"检查5拉升阴前后强Redis数据是否更新\"\"\"
    try:
        import time as time_module
        start_time = time_module.time()
        
        r = get_redis_client()
        today = datetime.now().strftime('%Y%m%d')
        key = f"5lsshiti:{today}"
        timestamp_key = f"5lsshiti:{today}:timestamp"
        
        # 使用Pipeline批量获取
        pipe = r.pipeline()
        pipe.get(timestamp_key)
        pipe.exists(key)
        results = pipe.execute()
        
        timestamp = results[0]
        key_exists = results[1]
        
        total_time = (time_module.time() - start_time) * 1000
        print(f"check_update耗时: {total_time:.2f}ms")
        
        if key_exists:
            data_count = r.hlen(key)
            if data_count > 0:
                if not timestamp:
                    import time
                    timestamp = str(int(time.time() * 1000))
                    r.set(timestamp_key, timestamp)
                
                return jsonify({
                    'success': True, 
                    'updated': True,
                    'timestamp': timestamp,
                    'count': data_count,
                    'refresh_interval': get_redis_refresh_interval()
                })
        
        return jsonify({
            'success': True, 
            'updated': False, 
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
"""


# ========== 通达信5拉升十字星路由 ==========

@app.route('/通达信5拉升十字星')
def tdx5lsszx_page():
    """通达信5拉升十字星页面"""
    return render_template('tdx5lsszx.html')

@app.route('/api/tdx5lsszx/stocks')
def get_tdx5lsszx_stocks():
    """获取通达信5拉升十字星股票列表"""
    try:
        import base64
        
        r = get_redis_client()
        date_param = request.args.get('date')
        if date_param:
            today = date_param
        else:
            today = datetime.now().strftime('%Y-%m-%d')
        key = f"tdx5lsszx:{today}"
        
        cursor = 0
        all_keys = []
        
        while True:
            cursor, keys = r.hscan(key, cursor, count=100)
            all_keys.extend(keys.items())
            if cursor == 0:
                break
        
        if not all_keys:
            return jsonify({'success': True, 'data': []})
        
        stocks = []
        
        for field, data_str in all_keys:
            try:
                # 尝试Base64解码
                try:
                    decoded_data = base64.b64decode(data_str).decode('utf-8')
                    stock_info = json.loads(decoded_data)
                except:
                    # 如果不是Base64，直接解析JSON
                    stock_info = json.loads(data_str)
                
                # field格式: groupX:code
                field_str = field.decode('utf-8') if isinstance(field, bytes) else field
                group_key = field_str.split(':')[0] if ':' in field_str else 'unknown'
                
                stocks.append({
                    'code': stock_info.get('code', ''),
                    'name': stock_info.get('name', ''),
                    'group': group_key,
                    'today_change': stock_info.get('today_change', 0),
                    'no_kill_count': stock_info.get('no_kill_count', 0),
                    'market_cap': stock_info.get('market_cap', 0)
                })
            except Exception as e:
                print(f"解析股票数据失败 {field}: {e}")
                continue
        
        return jsonify({'success': True, 'data': stocks})
    except Exception as e:
        print(f"获取通达信5拉升十字星数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/tdx5lsszx/realtime', methods=['POST'])
def get_tdx5lsszx_realtime():
    """通达信5拉升十字星实时涨跌幅接口"""
    try:
        from flask import request
        data = request.get_json()
        codes = data.get('codes', [])
        
        if not codes:
            return jsonify({'success': True, 'data': {}})
        
        realtime_data = {}
        for code in codes:
            try:
                if code.startswith('6'):
                    tencent_code = f"sh{code}"
                else:
                    tencent_code = f"sz{code}"
                
                url = f"http://qt.gtimg.cn/q={tencent_code}"
                response = requests.get(url, timeout=2)
                response.encoding = 'gbk'
                
                content = response.text.strip()
                if content:
                    data_str = content.split('="')[1].split('"')[0]
                    data_parts = data_str.split('~')
                    if len(data_parts) > 32:
                        change_pct = float(data_parts[32])
                        realtime_data[code] = {
                            'change': change_pct
                        }
            except Exception as e:
                print(f"获取实时行情失败 {code}: {e}")
                continue
        
        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        print(f"获取实时行情失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/tdx5lsszx/dates')
def get_tdx5lsszx_dates():
    """获取通达信5拉升十字星所有有数据的日期"""
    try:
        r = get_redis_client()
        
        keys = r.keys('tdx5lsszx:*')
        
        dates = []
        
        for key in keys:
            key_str = key.decode('utf-8') if isinstance(key, bytes) else key
            if ':' in key_str:
                date = key_str.split(':')[1]
                if date and len(date) == 10:
                    dates.append(date)
        
        dates = sorted(set(dates), reverse=True)
        
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ========== 通达信5拉升阴向上路由 ==========

@app.route('/通达信5拉升阴向上')
def tdx5lssxs_page():
    """通达信5拉升阴向上页面"""
    return render_template('tdx5lssxs.html')

@app.route('/api/tdx5lssxs/stocks')
def get_tdx5lssxs_stocks():
    """获取通达信5拉升阴向上股票列表"""
    try:
        import base64
        
        r = get_redis_client()
        date_param = request.args.get('date')
        if date_param:
            today = date_param
        else:
            today = datetime.now().strftime('%Y-%m-%d')
        key = f"tdx5lssxs:{today}"
        
        cursor = 0
        all_keys = []
        
        while True:
            cursor, keys = r.hscan(key, cursor, count=100)
            all_keys.extend(keys.items())
            if cursor == 0:
                break
        
        if not all_keys:
            return jsonify({'success': True, 'data': []})
        
        stocks = []
        
        for field, data_str in all_keys:
            try:
                # 尝试Base64解码
                try:
                    decoded_data = base64.b64decode(data_str).decode('utf-8')
                    stock_info = json.loads(decoded_data)
                except:
                    # 如果不是Base64，直接解析JSON
                    stock_info = json.loads(data_str)
                
                # field格式: groupX:code
                field_str = field.decode('utf-8') if isinstance(field, bytes) else field
                group_key = field_str.split(':')[0] if ':' in field_str else 'unknown'
                
                stocks.append({
                    'code': stock_info.get('code', ''),
                    'name': stock_info.get('name', ''),
                    'group': group_key,
                    'today_change': stock_info.get('today_change', 0),
                    'no_kill_count': stock_info.get('no_kill_count', 0),
                    'market_cap': stock_info.get('market_cap', 0)
                })
            except Exception as e:
                print(f"解析股票数据失败 {field}: {e}")
                continue
        
        return jsonify({'success': True, 'data': stocks})
    except Exception as e:
        print(f"获取通达信5拉升阴向上数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/tdx5lssxs/realtime', methods=['POST'])
def get_tdx5lssxs_realtime():
    """通达信5拉升阴向上实时涨跌幅接口"""
    try:
        from flask import request
        data = request.get_json()
        codes = data.get('codes', [])
        
        if not codes:
            return jsonify({'success': True, 'data': {}})
        
        realtime_data = {}
        for code in codes:
            try:
                if code.startswith('6'):
                    tencent_code = f"sh{code}"
                else:
                    tencent_code = f"sz{code}"
                
                url = f"http://qt.gtimg.cn/q={tencent_code}"
                response = requests.get(url, timeout=2)
                response.encoding = 'gbk'
                
                content = response.text.strip()
                if content:
                    data_str = content.split('="')[1].split('"')[0]
                    data_parts = data_str.split('~')
                    if len(data_parts) > 32:
                        change_pct = float(data_parts[32])
                        realtime_data[code] = {
                            'change': change_pct
                        }
            except Exception as e:
                print(f"获取实时行情失败 {code}: {e}")
                continue
        
        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        print(f"获取实时行情失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/tdx5lssxs/dates')
def get_tdx5lssxs_dates():
    """获取通达信5拉升阴向上所有有数据的日期"""
    try:
        r = get_redis_client()
        
        keys = r.keys('tdx5lssxs:*')
        
        dates = []
        
        for key in keys:
            key_str = key.decode('utf-8') if isinstance(key, bytes) else key
            if ':' in key_str:
                date = key_str.split(':')[1]
                if date and len(date) == 10:
                    dates.append(date)
        
        dates = sorted(set(dates), reverse=True)
        
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============================================================
# 最近交易日 API（用于前端默认日期）
# ============================================================

# 缓存最近交易日（1小时刷新一次）
_latest_trade_date_cache = {'date': None, 'ts': 0}

@app.route('/api/latest_trade_date')
def get_latest_trade_date():
    """获取最近一个交易日。如果今天是交易日返回今天，否则返回最近的交易日。"""
    try:
        import time as _t
        global _latest_trade_date_cache
        now_ts = _t.time()
        # 1小时内使用缓存
        if _latest_trade_date_cache['date'] and (now_ts - _latest_trade_date_cache['ts']) < 3600:
            return jsonify({'success': True, 'date': _latest_trade_date_cache['date']})

        today_str = datetime.now().strftime('%Y%m%d')
        pro = ts.pro_api()
        cal = pro.trade_cal(exchange='SSE', start_date='20240101', end_date=today_str, is_open=1,
                            fields='cal_date')
        dates = sorted(cal['cal_date'].tolist(), reverse=True)
        if dates:
            latest = dates[0]
            formatted = f"{latest[:4]}-{latest[4:6]}-{latest[6:]}"
            _latest_trade_date_cache['date'] = formatted
            _latest_trade_date_cache['ts'] = now_ts
            return jsonify({'success': True, 'date': formatted})

        # fallback
        fallback = datetime.now().strftime('%Y-%m-%d')
        return jsonify({'success': True, 'date': fallback})
    except Exception as e:
        fallback = datetime.now().strftime('%Y-%m-%d')
        print(f"[latest_trade_date] 获取失败，使用今日: {e}")
        return jsonify({'success': True, 'date': fallback})


# ============================================================
# 5拉升3光脚 (5las3guangjiao) 路由
# Redis key: 5las3guangjiao（固定key，也支持 5las3guangjiao:{date} 格式）
# ============================================================

@app.route('/5拉升3光脚')
def las3guangjiao_page():
    """5拉升3光脚页面"""
    return render_template('5las3guangjiao.html')


@app.route('/api/5las3gj/stocks')
@app.route('/api/5las3gj/stocks/<date>')
def get_5las3gj_stocks(date=None):
    """获取5拉升3光脚股票列表，从 Redis key: 5las3guangjiao 读取"""
    try:
        import base64
        import time as time_module
        from flask import request

        start_time = time_module.time()
        r = get_redis_client()

        if not date:
            date = request.args.get('date')
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')

        today_compact = date.replace('-', '')

        # 尝试多种 key 格式（固定key优先，再按日期查找）
        candidate_keys = [
            '5las3guangjiao',
            f'5las3guangjiao:{date}',
            f'5las3guangjiao:{today_compact}',
        ]

        key = None
        key_type = None
        for ck in candidate_keys:
            if r.exists(ck):
                key = ck
                key_type = r.type(key)
                if isinstance(key_type, bytes):
                    key_type = key_type.decode('utf-8')
                print(f"[5las3gj] 使用key: {key}, 类型: {key_type}")
                break

        if not key:
            return jsonify({
                'success': True,
                'data': [],
                'message': f'没有找到数据，尝试过: {candidate_keys}'
            })

        all_items = []
        if key_type == 'hash':
            cursor = 0
            while True:
                cursor, items = r.hscan(key, cursor, count=100)
                all_items.extend(items.items())
                if cursor == 0:
                    break
        elif key_type == 'string':
            data_str = r.get(key)
            if data_str:
                try:
                    data_list = json.loads(data_str)
                    if isinstance(data_list, list):
                        for item in data_list:
                            code = item.get('code', '')
                            all_items.append((code, json.dumps(item, ensure_ascii=False)))
                    elif isinstance(data_list, dict):
                        for code, item in data_list.items():
                            all_items.append((code, json.dumps(item, ensure_ascii=False)))
                except Exception as e:
                    print(f"[5las3gj] 解析string数据失败: {e}")
        elif key_type == 'list':
            items = r.lrange(key, 0, -1)
            for item in items:
                try:
                    item_str = item.decode('utf-8') if isinstance(item, bytes) else item
                    parsed = json.loads(item_str)
                    code = parsed.get('code', '')
                    all_items.append((code, item_str))
                except Exception as e:
                    print(f"[5las3gj] 解析list item失败: {e}")

        if not all_items:
            return jsonify({'success': True, 'data': [], 'message': f'key {key} 数据为空'})

        stocks = []
        for code, data_str in all_items:
            try:
                if isinstance(data_str, bytes):
                    data_str = data_str.decode('utf-8')
                # 尝试 Base64 解码
                try:
                    decoded = base64.b64decode(data_str).decode('utf-8')
                    stock_info = json.loads(decoded)
                except Exception:
                    stock_info = json.loads(data_str)

                # 兼容不同字段名
                stock_code = stock_info.get('code', code.decode('utf-8') if isinstance(code, bytes) else str(code))
                almost_count = stock_info.get('almost_bare_foot_count_10',
                               stock_info.get('almost_guang_jiao_count',
                               stock_info.get('几乎光脚K线次数', 0)))
                bare_count = stock_info.get('bare_foot_count_10',
                             stock_info.get('guang_jiao_count',
                             stock_info.get('光脚K线次数', 0)))
                pull_count = stock_info.get('pull_count_10',
                             stock_info.get('high_gain_count',
                             stock_info.get('最高涨幅≥6.5次数', 0)))
                yin_days = stock_info.get('yin_count_10',
                           stock_info.get('negative_days',
                           stock_info.get('阴线天数', 0)))

                stocks.append({
                    'code': stock_code,
                    'name': stock_info.get('name', stock_info.get('股票名称', '')),
                    'almost_bare_foot_count_10': almost_count,
                    'bare_foot_count_10': bare_count,
                    'pull_count_10': pull_count,
                    'yin_count_10': yin_days,
                    'today_change': stock_info.get('today_change', stock_info.get('current_gain', 0)),
                    'market_cap': stock_info.get('market_cap', 0),
                    'warning_time': stock_info.get('warning_time', stock_info.get('time', '')),
                })
            except Exception as e:
                print(f"[5las3gj] 解析股票数据失败 {code}: {e}")
                continue

        total_time = (time_module.time() - start_time) * 1000
        print(f"[5las3gj] 共解析 {len(stocks)} 只股票，耗时 {total_time:.2f}ms")

        return jsonify({
            'success': True,
            'data': stocks,
            'performance': {'total_time': f"{total_time:.2f}ms", 'count': len(stocks)},
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        print(f"[5las3gj] 获取数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/5las3gj/realtime', methods=['POST'])
def get_5las3gj_realtime():
    """5拉升3光脚实时涨跌幅"""
    try:
        from flask import request
        data = request.get_json()
        codes = data.get('codes', [])
        if not codes:
            return jsonify({'success': True, 'data': {}})

        realtime_data = {}
        batch_result = get_tencent_batch_quotes(codes)
        for code in codes:
            if code in batch_result:
                realtime_data[code] = {'change': round(batch_result[code].get('change_pct', 0), 2)}

        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/5las3gj/market_cap/<code>')
def get_5las3gj_market_cap(code):
    """获取单个股票市值"""
    try:
        if code.startswith('6'):
            ts_code = f"{code}.SH"
        elif code.startswith('0') or code.startswith('3'):
            ts_code = f"{code}.SZ"
        elif code.startswith('8') or code.startswith('4'):
            ts_code = f"{code}.BJ"
        else:
            ts_code = f"{code}.SZ"

        pro = ts.pro_api()
        df = pro.daily_basic(ts_code=ts_code, fields='circ_mv')
        if not df.empty:
            market_cap = float(df.iloc[0]['circ_mv']) / 10000
            return jsonify({'success': True, 'market_cap': round(market_cap, 2)})
        return jsonify({'success': False, 'error': '无市值数据'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/5las3gj/dates')
def get_5las3gj_dates():
    """获取5las3guangjiao可用日期列表"""
    try:
        r = get_redis_client()
        dates = []

        # 检查固定key是否存在（返回今天）
        if r.exists('5las3guangjiao'):
            today = datetime.now().strftime('%Y-%m-%d')
            dates.append(today)

        # 扫描日期格式的key
        for pattern in ['5las3guangjiao:????-??-??', '5las3guangjiao:????????']:
            keys = r.keys(pattern)
            for key in keys:
                key_str = key.decode('utf-8') if isinstance(key, bytes) else key
                if ':' in key_str:
                    date_part = key_str.split(':', 1)[1]
                    # 转换为 YYYY-MM-DD 格式
                    if len(date_part) == 8 and date_part.isdigit():
                        date_part = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:]}"
                    if len(date_part) == 10:
                        dates.append(date_part)

        dates = sorted(set(dates), reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/5las3gj/check_update')
def check_5las3gj_update():
    """检查5las3guangjiao数据是否有更新"""
    try:
        r = get_redis_client()
        key = '5las3guangjiao'
        today = datetime.now().strftime('%Y-%m-%d')
        date_key = f'5las3guangjiao:{today}'

        active_key = key if r.exists(key) else (date_key if r.exists(date_key) else None)

        if not active_key:
            return jsonify({'success': True, 'updated': False, 'timestamp': None})

        key_type = r.type(active_key)
        if isinstance(key_type, bytes):
            key_type = key_type.decode('utf-8')

        count = 0
        if key_type == 'hash':
            count = r.hlen(active_key)
        elif key_type == 'string':
            raw = r.get(active_key)
            if raw:
                try:
                    data = json.loads(raw)
                    count = len(data) if isinstance(data, list) else 1
                except Exception:
                    count = 1
        elif key_type == 'list':
            count = r.llen(active_key)

        ttl = r.ttl(active_key)
        timestamp = f"{active_key}:{count}:{ttl}"

        return jsonify({
            'success': True,
            'updated': count > 0,
            'timestamp': timestamp,
            'count': count
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ========== 每日Excel数据页面 ==========
# 支持通过环境变量覆盖目录；默认使用Linux服务器目录
DAILYSTOCK_DIR = os.environ.get('DAILYSTOCK_DIR', '/home/stock/web')
_DAILYSTOCK_CACHE = {}   # key: (file_path, mtime) → {sheets, headers, rows_by_sheet}
_DS_DATE_PATTERN = re.compile(r'^(\d{4})年(\d{2})月(\d{2})日$')

def _ds_folder(date_str):
    """'2026-03-25' → 'C:\\dailystock\\2026年03月25日'"""
    return os.path.join(DAILYSTOCK_DIR,
                        datetime.strptime(date_str, '%Y-%m-%d').strftime('%Y年%m月%d日'))

def _ds_list_dates():
    if not os.path.exists(DAILYSTOCK_DIR):
        return []
    dates = []
    for name in os.listdir(DAILYSTOCK_DIR):
        if os.path.isdir(os.path.join(DAILYSTOCK_DIR, name)):
            m = _DS_DATE_PATTERN.match(name)
            if m:
                dates.append(f'{m.group(1)}-{m.group(2)}-{m.group(3)}')
    dates.sort(reverse=True)
    return dates

def _ds_list_files(date_str):
    folder = _ds_folder(date_str)
    if not os.path.exists(folder):
        return []
    return sorted(f[:-5] for f in os.listdir(folder)
                  if f.endswith('.xlsx') and not f.startswith('~$'))

def _ds_read_file(file_path):
    """读取 xlsx，带 mtime 缓存。返回 {sheets, data: {sheet: (headers, rows)}}"""
    mtime = os.path.getmtime(file_path)
    cache_key = (file_path, mtime)
    if cache_key in _DAILYSTOCK_CACHE:
        return _DAILYSTOCK_CACHE[cache_key]

    xl = pd.ExcelFile(file_path, engine='openpyxl')
    sheet_names = xl.sheet_names
    data = {}
    for sname in sheet_names:
        df = xl.parse(sname, dtype=str).fillna('')
        headers = [str(c) for c in df.columns.tolist()]
        rows = df.values.tolist()   # 比 iterrows 快 10x
        data[sname] = (headers, rows)

    result = {'sheets': sheet_names, 'data': data}
    _DAILYSTOCK_CACHE[cache_key] = result
    # 避免缓存无限增长，超 30 条时清理最旧的一半
    if len(_DAILYSTOCK_CACHE) > 30:
        keys = list(_DAILYSTOCK_CACHE.keys())
        for k in keys[:15]:
            del _DAILYSTOCK_CACHE[k]
    return result

@app.route('/dailystock')
def dailystock_page():
    return render_template('dailystock.html')

@app.route('/api/dailystock/init')
def get_dailystock_init():
    """一次性返回：日期列表 + 当前日期文件列表 + 第一个文件数据"""
    try:
        date = request.args.get('date') or ''
        file = request.args.get('file') or ''
        sheet = request.args.get('sheet') or ''

        dates = _ds_list_dates()
        if not dates:
            return jsonify({'success': True, 'dates': [], 'files': [],
                            'sheets': [], 'headers': [], 'rows': [],
                            'current_date': '', 'current_file': '', 'current_sheet': ''})

        current_date = date if date in dates else dates[0]
        files = _ds_list_files(current_date)
        if not files:
            return jsonify({'success': True, 'dates': dates, 'files': [],
                            'sheets': [], 'headers': [], 'rows': [],
                            'current_date': current_date, 'current_file': '', 'current_sheet': ''})

        current_file = file if file in files else files[0]
        file_path = os.path.join(_ds_folder(current_date), current_file + '.xlsx')
        cached = _ds_read_file(file_path)
        sheet_names = cached['sheets']
        current_sheet = sheet if sheet in sheet_names else sheet_names[0]
        headers, rows = cached['data'][current_sheet]

        return jsonify({'success': True,
                        'dates': dates, 'current_date': current_date,
                        'files': files, 'current_file': current_file,
                        'sheets': sheet_names, 'current_sheet': current_sheet,
                        'headers': headers, 'rows': rows, 'total': len(rows)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/dailystock/dates')
def get_dailystock_dates():
    try:
        return jsonify({'success': True, 'dates': _ds_list_dates()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})

@app.route('/api/dailystock/files')
def get_dailystock_files():
    try:
        date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        return jsonify({'success': True, 'files': _ds_list_files(date), 'date': date})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'files': []})

@app.route('/api/dailystock/data')
def get_dailystock_data():
    try:
        date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        file = request.args.get('file', '')
        sheet = request.args.get('sheet', '')
        if not file:
            return jsonify({'success': False, 'error': '未指定文件名'})
        file_path = os.path.join(_ds_folder(date), file + '.xlsx')
        if not os.path.exists(file_path):
            return jsonify({'success': False, 'error': f'文件不存在: {file}.xlsx'})
        cached = _ds_read_file(file_path)
        sheet_names = cached['sheets']
        current_sheet = sheet if sheet in sheet_names else sheet_names[0]
        headers, rows = cached['data'][current_sheet]
        return jsonify({'success': True,
                        'sheets': sheet_names, 'current_sheet': current_sheet,
                        'headers': headers, 'rows': rows, 'total': len(rows)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ─── 极品关注5日多列 ────────────────────────────────────────────────────────────

def _jpgz_extract_dates(yxdxs_keys, yx205_keys, lszty_keys=None, top_n=5):
    """
    从多个 Redis key 列表中提取日期，合并取并集，降序取最近 top_n 个。
    yxdxs_keys: ['stocks_yxdxs:YYYY-MM-DD', ...]
    yx205_keys:  ['yx205:YYYYMMDD', ...]
    lszty_keys:  ['lszty:YYYYMMDD', ...]
    返回: ['2026-03-27', '2026-03-26', ...]
    """
    dates = set()
    for k in yxdxs_keys:
        key = k.decode('utf-8') if isinstance(k, bytes) else k
        date_part = key.replace('stocks_yxdxs:', '')
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_part):
            dates.add(date_part)
    for k in yx205_keys:
        key = k.decode('utf-8') if isinstance(k, bytes) else k
        if not key.startswith('yx205:'):
            continue
        date_part = key.replace('yx205:', '')
        if re.match(r'^\d{8}$', date_part):
            dates.add(f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}")
    for k in (lszty_keys or []):
        key = k.decode('utf-8') if isinstance(k, bytes) else k
        date_part = key.replace('lszty:', '')
        if re.match(r'^\d{8}$', date_part):
            dates.add(f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}")
    return sorted(dates, reverse=True)[:top_n]


def _jpgz_read_stock_minimal(code, data_str):
    """
    解析单条 Redis 股票数据，返回最小字段字典。
    支持 JSON 直接解析和 base64 编码两种格式。
    yx205 数据的 today_change 字段可能用 current_gain 作为别名。
    """
    import base64
    if isinstance(code, bytes):
        code = code.decode('utf-8')
    if isinstance(data_str, bytes):
        data_str = data_str.decode('utf-8')
    try:
        try:
            decoded = base64.b64decode(data_str).decode('utf-8')
            info = json.loads(decoded)
        except Exception:
            info = json.loads(data_str)
        change = info.get('today_change', info.get('current_gain', 0))
        return {
            'code': info.get('code', code),
            'name': info.get('name', ''),
            'today_change': change if change is not None else 0,
            'market_cap': info.get('market_cap', 0) or 0,
        }
    except Exception:
        return None


def _jpgz_read_hash_key(r, key):
    """读取 Redis hash key，返回 list of minimal stock dicts"""
    stocks = []
    try:
        cursor = 0
        while True:
            cursor, items = r.hscan(key, cursor, count=200)
            for code, data_str in items.items():
                result = _jpgz_read_stock_minimal(code, data_str)
                if result:
                    stocks.append(result)
            if cursor == 0:
                break
    except Exception as e:
        print(f"[jpgz] 读取 hash key {key} 失败: {e}")
    return stocks


def _jpgz_read_string_key(r, key):
    """读取 Redis string key（JSON list 格式），返回 list of minimal stock dicts"""
    stocks = []
    try:
        raw = r.get(key)
        if not raw:
            return stocks
        data_list = json.loads(raw)
        for item in data_list:
            code = item.get('code', '')
            result = _jpgz_read_stock_minimal(code, json.dumps(item, ensure_ascii=False))
            if result:
                stocks.append(result)
    except Exception as e:
        print(f"[jpgz] 读取 string key {key} 失败: {e}")
    return stocks


def _jpgz_read_lszty(r, key):
    """
    读取 lszty Redis hash，筛选"最多一阴"组中符合条件的股票：
    极品K% >= 50% 或 全阳 或 阴线向上 或 阴线都强 (任一为True)
    返回 list of minimal stock dicts (与 yx205/yxdxs 结构一致)
    """
    import base64
    stocks = []
    if not r.exists(key):
        return stocks
    cursor = 0
    while True:
        cursor, batch = r.hscan(key, cursor, count=100)
        for code, data_str in batch.items():
            try:
                code_str = code.decode('utf-8') if isinstance(code, bytes) else code
                raw = data_str.decode('utf-8') if isinstance(data_str, bytes) else data_str
                try:
                    info = json.loads(base64.b64decode(raw).decode('utf-8'))
                except Exception:
                    info = json.loads(raw)

                if info.get('group') != '最多一阴':
                    continue

                jipink_ratio = info.get('jipink_ratio', 0)
                all_yang = info.get('all_yang', False)
                all_yin_up = info.get('all_yin_up', False)
                all_yin_strong = info.get('all_yin_strong', False)

                if jipink_ratio >= 0.5 or all_yang or all_yin_up or all_yin_strong:
                    stocks.append({
                        'code': info.get('code', code_str),
                        'name': info.get('name', ''),
                        'today_change': info.get('ref_gain', 0),
                        'source': 'lszty',
                        'all_yang': all_yang,
                        'all_yin_up': all_yin_up,
                        'all_yin_strong': all_yin_strong,
                        'jipink_ratio': jipink_ratio,
                        'n_days': info.get('n_days', 0),
                        'yin_count_between': info.get('yin_count_between', 0),
                        'jipink_count': info.get('jipink_count', 0),
                        'board_type': info.get('board_type', ''),
                    })
            except Exception:
                continue
        if cursor == 0:
            break
    return stocks


def _jpgz_extract_recent_trade_dates(trade_keys, top_n=5):
    """从 yhy:YYYYMMDD 交易日 key 中提取最近 top_n 个交易日"""
    dates = set()
    for k in trade_keys or []:
        key = k.decode('utf-8') if isinstance(k, bytes) else str(k)
        if not key.startswith('yhy:'):
            continue
        date_part = key.replace('yhy:', '')
        if re.match(r'^\d{8}$', date_part):
            dates.add(f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}")
    return sorted(dates, reverse=True)[:top_n]


def _jpgz_read_task_groups(r, date):
    """读取某个交易日的共享任务池，按任务类型分组返回"""
    groups = {'yizi': [], 'changzi': [], 'changchangzi': [], 'hengpan': []}
    task_key = _yhy_task_key(date)
    if not r.exists(task_key) or r.type(task_key) not in ('hash', b'hash'):
        return groups

    cursor = 0
    while True:
        cursor, items = r.hscan(task_key, cursor, count=200)
        for code, raw_value in items.items():
            obj = _yhy_decode_json_payload(raw_value)
            if not isinstance(obj, dict):
                continue
            task_type = str(obj.get('task_type', '')).strip()
            if task_type not in groups:
                continue
            gain_value = obj.get('today_change', obj.get('ref_gain', 0))
            try:
                gain_value = float(gain_value or 0)
            except Exception:
                gain_value = 0.0
            try:
                market_cap = float(obj.get('market_cap', 0) or 0)
            except Exception:
                market_cap = 0.0
            groups[task_type].append({
                'code': str(obj.get('code', code.decode('utf-8') if isinstance(code, bytes) else code)),
                'name': str(obj.get('name', '')),
                'today_change': gain_value,
                'market_cap': market_cap,
                'task_type': task_type,
                'source_page': _yhy_normalize_source_page_name(obj.get('source_page', '')),
                'source_pages': _yhy_normalize_source_pages(obj),
                'added_at': str(obj.get('added_at', '')),
            })
        if cursor == 0:
            break

    for task_type in groups:
        groups[task_type].sort(key=lambda x: x.get('today_change', 0), reverse=True)
    return groups


@app.route('/阴后强')
def yhy_page():
    """阴后强页面"""
    return render_template('yhy.html')


@app.route('/5拉升')
def five_ls_page():
    """5拉升页面"""
    return render_template('5ls.html')


@app.route('/涨停前横盘')
def ztqhp_page():
    """涨停前横盘页面"""
    return render_template('ztqhp.html')


@app.route('/连续4天拉升')
def intraday4up_page():
    """连续4天拉升页面"""
    return render_template('intraday4up.html')


def _yhy_resolve_target_key(r, date=None):
    """解析阴后强数据 Redis key，支持 date=YYYY-MM-DD"""
    if date:
        target_key = f"yhy:{date.replace('-', '')}"
        if not r.exists(target_key):
            return None
        return target_key

    keys = r.keys('yhy:*')
    if not keys:
        return None
    return sorted(keys, reverse=True)[0]


def _yhy_decode_json_payload(raw_value):
    """解析 yhy Redis 载荷，兼容 base64 或明文 json"""
    import base64 as _b64

    if raw_value is None:
        return None
    try:
        decoded = _b64.b64decode(raw_value).decode('utf-8')
        return json.loads(decoded)
    except Exception:
        try:
            if isinstance(raw_value, bytes):
                raw_value = raw_value.decode('utf-8')
            return json.loads(raw_value)
        except Exception:
            return None


def _yhy_normalize_scan_info(scan_info):
    if scan_info is None:
        return None
    if not isinstance(scan_info, dict):
        try:
            return {
                'last_scan_duration_sec': float(scan_info or 0),
                'last_scan_end_time': '',
                'status': '',
                'reason': '',
                'stock_count': 0,
                'snapshot_mode': '',
                'redis_update_status': '',
                'redis_update_time': '',
                'redis_update_duration_sec': 0.0,
                'redis_update_error': '',
            }
        except Exception:
            return None

    def _as_int(value, default=0):
        try:
            return int(value or default)
        except Exception:
            return default

    return {
        'last_scan_duration_sec': float(
            scan_info.get('last_scan_duration_sec',
                          scan_info.get('scan_duration',
                                        scan_info.get('duration_sec',
                                                      scan_info.get('duration', 0)))) or 0
        ),
        'last_scan_end_time': str(
            scan_info.get('last_scan_end_time',
                          scan_info.get('scan_end_time',
                                        scan_info.get('end_time', ''))) or ''
        ),
        'status': str(scan_info.get('status', '') or ''),
        'reason': str(scan_info.get('reason', '') or ''),
        'stock_count': _as_int(scan_info.get('stock_count', scan_info.get('count', 0))),
        'snapshot_mode': str(scan_info.get('snapshot_mode', '') or ''),
        'redis_update_status': str(scan_info.get('redis_update_status', '') or ''),
        'redis_update_time': str(scan_info.get('redis_update_time', '') or ''),
        'redis_update_duration_sec': float(scan_info.get('redis_update_duration_sec', 0) or 0),
        'redis_update_error': str(scan_info.get('redis_update_error', '') or ''),
    }

def _yhy_normalize_snapshot_info(snapshot_info):
    if snapshot_info is None or not isinstance(snapshot_info, dict):
        return None
    return {
        'last_snapshot_time': str(snapshot_info.get('last_snapshot_time', '') or ''),
        'last_snapshot_status': str(snapshot_info.get('last_snapshot_status', '') or ''),
        'last_snapshot_count': int(snapshot_info.get('last_snapshot_count', 0) or 0),
        'last_snapshot_duration_ms': float(snapshot_info.get('last_snapshot_duration_ms', 0) or 0),
        'last_snapshot_error': str(snapshot_info.get('last_snapshot_error', '') or ''),
        'overall_status': str(snapshot_info.get('overall_status', '') or ''),
        'overall_error': str(snapshot_info.get('overall_error', '') or ''),
        'snapshot_interval_sec': int(snapshot_info.get('snapshot_interval_sec', 0) or 0),
    }


def _yhy_pick_latest_snapshot_info(snapshot_infos):
    normalized = [info for info in (_yhy_normalize_snapshot_info(item) for item in snapshot_infos) if info]
    if not normalized:
        return None

    def sort_key(info):
        snapshot_time = str(info.get('last_snapshot_time', '') or '')
        if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', snapshot_time):
            return (1, snapshot_time)
        duration = info.get('last_snapshot_duration_ms', 0)
        try:
            duration = float(duration or 0)
        except Exception:
            duration = 0
        return (0, f"{duration:020.6f}")

    return max(normalized, key=sort_key)


def _yhy_build_snapshot_gate(snapshot_infos_by_prefix, required_prefixes):
    normalized_by_prefix = {}
    for prefix in required_prefixes:
        info = _yhy_normalize_snapshot_info(snapshot_infos_by_prefix.get(prefix))
        if info:
            normalized_by_prefix[prefix] = info

    latest = _yhy_pick_latest_snapshot_info(normalized_by_prefix.values())
    if not normalized_by_prefix:
        return {
            'snapshot_version': '',
            'is_snapshot_stable': False,
            'snapshot_reason': 'missing_snapshot_info',
        }

    missing_prefixes = [prefix for prefix in required_prefixes if prefix not in normalized_by_prefix]
    version_set = {
        str(info.get('last_snapshot_time', '') or '')
        for info in normalized_by_prefix.values()
        if str(info.get('last_snapshot_time', '') or '')
    }
    common_version = next(iter(version_set)) if len(version_set) == 1 else ''
    all_prefixes_present = not missing_prefixes
    all_prefixes_success = all(
        str(info.get('last_snapshot_status', '') or '') == 'success'
        for info in normalized_by_prefix.values()
    )
    overall_success = all(
        str(info.get('overall_status', '') or '') == 'success'
        for info in normalized_by_prefix.values()
    )

    reason = 'ok'
    if not all_prefixes_present:
        reason = 'missing_prefix_snapshot'
    elif not common_version:
        reason = 'snapshot_version_mismatch'
    elif not all_prefixes_success:
        reason = 'prefix_snapshot_failed'
    elif not overall_success:
        reason = 'overall_snapshot_not_success'

    return {
        'snapshot_version': common_version or str((latest or {}).get('last_snapshot_time', '') or ''),
        'is_snapshot_stable': (
            all_prefixes_present and
            bool(common_version) and
            all_prefixes_success and
            overall_success
        ),
        'snapshot_reason': reason,
    }


def _yhy_normalize_source_page_name(page):
    """把共享任务来源标识统一映射为中文可读文本"""
    page_text = str(page or '').strip()
    if not page_text:
        return ''

    direct_map = {
        'yhy': '阴后强',
        'ztqhp': '涨停前横盘',
        '5ls': '5拉升',
        'lszty': '连续涨停',
        'ztorgj': '涨停或光脚',
        'yx205': '20阴少跌少横盘',
        'lx5bp': '连续5不碰',
        'intraday4up': '连续4天拉升',
    }
    tab_map = {
        'yin20': '20日阴线次数',
        'limit20': '20日涨停次数',
        'bare20': '20日光脚次数',
        'weak20': '20日弱阴线',
        'weak10': '10日弱阴线',
        'klineTest': 'K线测试',
    }

    if page_text in direct_map:
        return direct_map[page_text]

    if ':' in page_text:
        prefix, suffix = page_text.split(':', 1)
        prefix_text = direct_map.get(prefix, prefix)
        suffix_text = tab_map.get(suffix, suffix)
        return f'{prefix_text}:{suffix_text}'

    return page_text


def _yhy_normalize_source_pages(obj, current_page=None):
    """兼容旧 source_page 和新 source_pages，统一为去重后的中文列表"""
    pages = []

    if isinstance(obj, dict):
        existing_pages = obj.get('source_pages', [])
        if isinstance(existing_pages, (list, tuple)):
            for page in existing_pages:
                page_text = _yhy_normalize_source_page_name(page)
                if page_text and page_text not in pages:
                    pages.append(page_text)

        source_page = _yhy_normalize_source_page_name(obj.get('source_page', ''))
        if source_page and source_page not in pages:
            pages.append(source_page)

    current_page_text = _yhy_normalize_source_page_name(current_page)
    if current_page_text and current_page_text not in pages:
        pages.append(current_page_text)

    return pages


def _yx205_pick_latest_scan_info(*scan_infos):
    normalized = [info for info in (_yhy_normalize_scan_info(item) for item in scan_infos) if info]
    if not normalized:
        return None

    def sort_key(info):
        end_time = str(info.get('last_scan_end_time', '') or '')
        if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', end_time):
            return (1, end_time)
        duration = info.get('last_scan_duration_sec', 0)
        try:
            duration = float(duration or 0)
        except Exception:
            duration = 0
        return (0, f"{duration:020.6f}")

    return max(normalized, key=sort_key)


def _yhy_task_key(date=None):
    """阴后强人工任务 Redis key，格式 myStock:task:YYYYMMDD"""
    if date:
        date_compact = str(date).replace('-', '')
    else:
        date_compact = datetime.now().strftime('%Y%m%d')
    return f"myStock:task:{date_compact}"


def _tushare_ts_code_from_symbol(code):
    """将6位股票代码转换为 Tushare ts_code"""
    code_text = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if len(code_text) != 6:
        raise ValueError('请输入6位股票代码')
    if code_text.startswith('6'):
        return f"{code_text}.SH"
    if code_text.startswith('8') or code_text.startswith('4'):
        return f"{code_text}.BJ"
    return f"{code_text}.SZ"


def _tushare_quote_code_from_symbol(code):
    """将6位股票代码转换为 tushare get_realtime_quotes 所需代码"""
    code_text = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if len(code_text) != 6:
        raise ValueError('请输入6位股票代码')
    if code_text.startswith('6'):
        return f"sh{code_text}"
    if code_text.startswith('8') or code_text.startswith('4'):
        return f"bj{code_text}"
    return f"sz{code_text}"


def _lookup_manual_task_stock_info(code):
    """根据股票代码从 Tushare 查询手动录入所需的基础信息"""
    clean_code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    ts_code = _tushare_ts_code_from_symbol(clean_code)
    quote_code = _tushare_quote_code_from_symbol(clean_code)

    pro = ts.pro_api()
    name = ''
    market_cap = 0.0
    today_change = 0.0

    try:
        basic_df = pro.stock_basic(ts_code=ts_code, fields='ts_code,symbol,name')
        if basic_df is not None and not basic_df.empty:
            name = str(basic_df.iloc[0].get('name', '') or '').strip()
    except Exception as e:
        print(f"[yhy] 手动录入查询股票名称失败 {clean_code}: {e}")

    try:
        quote_df = ts.get_realtime_quotes(quote_code)
        if quote_df is not None and not quote_df.empty:
            row = quote_df.iloc[0]
            if not name:
                name = str(row.get('name', '') or '').strip()
            pre_close_raw = row.get('pre_close')
            price_raw = row.get('price') or row.get('open') or row.get('pre_close')
            pre_close = float(pre_close_raw) if pre_close_raw not in (None, '') else 0.0
            price = float(price_raw) if price_raw not in (None, '') else 0.0
            if pre_close > 0 and price > 0:
                today_change = round(((price - pre_close) / pre_close) * 100, 4)
    except Exception as e:
        print(f"[yhy] 手动录入查询实时涨跌幅失败 {clean_code}: {e}")

    try:
        mv_df = pro.daily_basic(ts_code=ts_code, fields='ts_code,total_mv')
        if mv_df is not None and not mv_df.empty:
            market_cap = round(float(mv_df.iloc[0].get('total_mv', 0) or 0) / 10000, 2)
    except Exception as e:
        print(f"[yhy] 手动录入查询市值失败 {clean_code}: {e}")

    if not name:
        raise ValueError('未找到该股票代码，请确认后重新输入')

    return {
        'code': clean_code,
        'name': name,
        'stock_code_with_market': ts_code,
        'ref_gain': today_change,
        'today_change': today_change,
        'market_cap': market_cap,
    }


def _yhy_encode_json_payload(payload):
    """将 payload 编码为 base64 JSON 字符串"""
    import base64 as _b64
    return _b64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('utf-8')


def _five_ls_resolve_target_key(r, date=None):
    """解析 5拉升 数据 Redis key，支持 date=YYYY-MM-DD"""
    if date:
        target_key = f"5ls:{date.replace('-', '')}"
        if not r.exists(target_key):
            return None
        return target_key

    keys = r.keys('5ls:*')
    if not keys:
        return None

    valid_keys = []
    for key in keys:
        if isinstance(key, bytes):
            key = key.decode('utf-8', errors='ignore')
        if not isinstance(key, str) or ':' not in key:
            continue
        date_part = key.split(':', 1)[1]
        if len(date_part) == 8 and date_part.isdigit():
            valid_keys.append(key)
    if not valid_keys:
        return None
    return sorted(valid_keys, reverse=True)[0]


def _ztqhp_resolve_target_key(r, date=None):
    """解析 涨停前横盘 数据 Redis key，支持 date=YYYY-MM-DD"""
    if date:
        target_key = f"ztqhp:{date.replace('-', '')}"
        if not r.exists(target_key):
            return None
        return target_key

    keys = r.keys('ztqhp:*')
    if not keys:
        return None

    valid_keys = []
    for key in keys:
        if isinstance(key, bytes):
            key = key.decode('utf-8', errors='ignore')
        if not isinstance(key, str) or ':' not in key:
            continue
        date_part = key.split(':', 1)[1]
        if len(date_part) == 8 and date_part.isdigit():
            valid_keys.append(key)
    if not valid_keys:
        return None
    return sorted(valid_keys, reverse=True)[0]


def _intraday4up_resolve_target_key(r, date=None):
    """解析 连续4天拉升 数据 Redis key，支持 date=YYYY-MM-DD"""
    if date:
        target_key = f"intraday4up:{date.replace('-', '')}"
        if not r.exists(target_key):
            return None
        return target_key

    keys = r.keys('intraday4up:*')
    if not keys:
        return None

    valid_keys = []
    for key in keys:
        if isinstance(key, bytes):
            key = key.decode('utf-8', errors='ignore')
        if not isinstance(key, str) or ':' not in key:
            continue
        date_part = key.split(':', 1)[1]
        if len(date_part) == 8 and date_part.isdigit():
            valid_keys.append(key)
    if not valid_keys:
        return None
    return sorted(valid_keys, reverse=True)[0]


def _intraday4up_parse_float(value, default=0.0):
    try:
        if value in (None, ''):
            return default
        return float(value)
    except Exception:
        return default


def _intraday4up_calc_today_change(stock):
    """计算参考日涨跌幅，优先使用显式字段，其次用最后一天明细计算。"""
    if not isinstance(stock, dict):
        return 0.0

    for key in ('today_change', 'today_change_pct', 'pct_chg', 'change_pct'):
        if stock.get(key) not in (None, ''):
            return round(_intraday4up_parse_float(stock.get(key), 0.0), 4)

    details = stock.get('day_details')
    if isinstance(details, list):
        for item in reversed(details):
            if not isinstance(item, dict):
                continue
            prev_close = _intraday4up_parse_float(item.get('prev_close'), 0.0)
            ref_price = _intraday4up_parse_float(
                item.get('ref_or_close', item.get('close')), 0.0
            )
            if prev_close > 0 and ref_price > 0:
                return round((ref_price - prev_close) / prev_close * 100, 4)

    return round(_intraday4up_parse_float(stock.get('gain_4d_pct'), 0.0), 4)


@app.route('/api/yhy/stocks')
def get_yhy_stocks():
    """读取Redis key=yhy:YYYYMMDD的阴后强数据（hash, base64编码）"""
    try:
        r = get_redis_client()

        date = request.args.get('date')
        latest_key = _yhy_resolve_target_key(r, date)
        if not latest_key:
            target_key = f"yhy:{date.replace('-', '')}" if date else 'yhy:*'
            return jsonify({'success': True, 'data': [], 'message': f'key {target_key} 不存在'})

        key_type = r.type(latest_key)
        stocks = []
        scan_info = None

        if key_type in (b'hash', 'hash'):
            all_fields = r.hgetall(latest_key)
            for field, value in all_fields.items():
                if isinstance(field, bytes):
                    field = field.decode('utf-8', errors='ignore')
                if field == '_scan_info':
                    scan_info = _yhy_normalize_scan_info(_yhy_decode_json_payload(value))
                    continue
                if field == '_timestamp':
                    continue

                obj = _yhy_decode_json_payload(value)
                if isinstance(obj, dict):
                    stocks.append(obj)
        elif key_type in (b'string', 'string'):
            raw = r.get(latest_key)
            parsed = _yhy_decode_json_payload(raw)
            if isinstance(parsed, list):
                stocks = parsed
            elif isinstance(parsed, dict):
                stocks = [parsed]

        result = []
        for s in stocks:
            try:
                result.append({
                    'code': str(s.get('code', '')),
                    'name': str(s.get('name', '')),
                    'stock_code_with_market': str(s.get('stock_code_with_market', '')),
                    'ref_gain': float(s.get('ref_gain', 0)),
                    'today_change': float(s.get('today_change', s.get('ref_gain', 0))),
                    'yin_count': int(s.get('yin_count', 0)),
                    'all_yin_strong_next': bool(s.get('all_yin_strong_next', False)),
                    'high_gain_count': int(s.get('high_gain_count', 0)),
                    'gains_10': float(s.get('gains_10', 0)),
                    'max_drawdown_10d': float(s.get('max_drawdown_10d', 0)),
                    'ref_bare_foot': bool(s.get('ref_bare_foot', False)),
                    'ref_bare_head': bool(s.get('ref_bare_head', False)),
                    'ref_limit_up': bool(s.get('ref_limit_up', False)),
                    'ref_cross_star': bool(s.get('ref_cross_star', False)),
                    'jpk_count': int(s.get('jpk_count', 0)),
                    'all_yin_jpyk_next': bool(s.get('all_yin_jpyk_next', False)),
                    'all_yang': bool(s.get('all_yang', False)),
                    'all_yin_up': bool(s.get('all_yin_up', False)),
                    'all_yin_no_touch': bool(s.get('all_yin_no_touch', False)),
                    'all_yin_strong_next': bool(s.get('all_yin_strong_next', False)),
                    'all_yin_pull_next': bool(s.get('all_yin_pull_next', False)),
                    'all_yin_pull_prev_or_next': bool(s.get('all_yin_pull_prev_or_next', False)),
                    'ref_high_gain_ok': bool(s.get('ref_high_gain_ok', False)),
                    'ref_is_yang': bool(s.get('ref_is_yang', False)),
                    'ref_high_gain_gte_6_5_and_yang': bool(s.get('ref_high_gain_gte_6_5_and_yang', False)),
                    'market_cap': float(s.get('market_cap', 0) or 0),
                })
            except Exception:
                continue

        date_str = latest_key.replace('yhy:', '') if isinstance(latest_key, str) else latest_key.decode().replace('yhy:', '')
        return jsonify({'success': True, 'data': result, 'count': len(result), 'date': date_str, 'scan_info': scan_info})

    except Exception as e:
        print(f"[yhy] 获取数据失败: {e}")
        return jsonify({'success': False, 'error': str(e), 'data': []})


@app.route('/api/yhy/scan-info')
def get_yhy_scan_info():
    """读取 Redis yhy:YYYYMMDD hash 中的 _scan_info 元信息"""
    try:
        r = get_redis_client()
        date = request.args.get('date')
        latest_key = _yhy_resolve_target_key(r, date)
        if not latest_key:
            target_key = f"yhy:{date.replace('-', '')}" if date else 'yhy:*'
            return jsonify({'success': True, 'scan_info': None, 'message': f'key {target_key} 不存在'})

        scan_info = None
        if r.type(latest_key) in (b'hash', 'hash'):
            scan_info = _yhy_normalize_scan_info(_yhy_decode_json_payload(r.hget(latest_key, '_scan_info')))

        date_str = latest_key.replace('yhy:', '') if isinstance(latest_key, str) else latest_key.decode().replace('yhy:', '')
        return jsonify({'success': True, 'scan_info': scan_info, 'date': date_str})
    except Exception as e:
        print(f"[yhy] 获取扫描信息失败: {e}")
        return jsonify({'success': False, 'error': str(e), 'scan_info': None})


@app.route('/api/yhy/dates')
def get_yhy_dates():
    """获取阴后强所有有数据的日期"""
    try:
        r = get_redis_client()
        keys = r.keys('yhy:*')
        dates = []
        for key in keys:
            if isinstance(key, bytes):
                key = key.decode()
            if ':' in key:
                date_part = key.split(':', 1)[1]
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        dates = sorted(list(set(dates)), reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/yhy/tasks')
def get_yhy_tasks():
    """读取阴后强人工任务列表，Redis key=myStock:task:YYYYMMDD"""
    try:
        r = get_redis_client()
        date = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
        target_key = _yhy_task_key(date)
        if not r.exists(target_key):
            return jsonify({'success': True, 'data': [], 'count': 0, 'date': target_key.replace('myStock:task:', '')})

        tasks = []
        if r.type(target_key) in (b'hash', 'hash'):
            all_fields = r.hgetall(target_key)
            for field, value in all_fields.items():
                if isinstance(field, bytes):
                    field = field.decode('utf-8', errors='ignore')
                if field in ('_scan_info', '_timestamp'):
                    continue

                obj = _yhy_decode_json_payload(value)
                if not isinstance(obj, dict):
                    continue

                source_pages = _yhy_normalize_source_pages(obj)

                tasks.append({
                    'task_type': str(obj.get('task_type', '')),
                    'code': str(obj.get('code', field or '')),
                    'name': str(obj.get('name', '')),
                    'stock_code_with_market': str(obj.get('stock_code_with_market', '')),
                    'ref_gain': float(obj.get('ref_gain', 0) or 0),
                    'today_change': float(obj.get('today_change', obj.get('ref_gain', 0)) or 0),
                    'market_cap': float(obj.get('market_cap', 0) or 0),
                    'yin_count': int(obj.get('yin_count', 0) or 0),
                    'high_gain_count': int(obj.get('high_gain_count', 0) or 0),
                    'gains_10': float(obj.get('gains_10', 0) or 0),
                    'max_drawdown_10d': float(obj.get('max_drawdown_10d', 0) or 0),
                    'high_gain_10d_count': int(obj.get('high_gain_10d_count', obj.get('high_gain_count', 0)) or 0),
                    'days_since_top_day': int(obj.get('days_since_top_day', 0) or 0),
                    'strong_adjust_k_pct': float(obj.get('strong_adjust_k_pct', 0) or 0),
                    'cum_high_to_ma5_pct': float(obj.get('cum_high_to_ma5_pct', 0) or 0),
                    'all_yin_up': bool(obj.get('all_yin_up', False)),
                    'all_yin_no_touch': bool(obj.get('all_yin_no_touch', False)),
                    'all_yin_cross_star': bool(obj.get('all_yin_cross_star', False)),
                    'layang': bool(obj.get('layang', False)),
                    'ref_limit_up': bool(obj.get('ref_limit_up', False)),
                    'added_at': str(obj.get('added_at', '')),
                    'gain_confirm_level': str(obj.get('gain_confirm_level', '')),
                    'source_page': _yhy_normalize_source_page_name(
                        obj.get('source_page', source_pages[0] if source_pages else '')
                    ) or (source_pages[0] if source_pages else ''),
                    'source_pages': source_pages,
                    'jipin_focus': bool(obj.get('jipin_focus', False)),
                    'focus_reason': str(obj.get('focus_reason', '')),
                    'focus_reasons': obj.get('focus_reasons', []) if isinstance(obj.get('focus_reasons', []), list) else [],
                    'jipin_focus_at': str(obj.get('jipin_focus_at', '')),
                })

        tasks.sort(key=lambda item: (str(item.get('task_type', '')), -float(item.get('today_change', 0) or 0), str(item.get('code', ''))))
        return jsonify({'success': True, 'data': tasks, 'count': len(tasks), 'date': target_key.replace('myStock:task:', '')})
    except Exception as e:
        print(f"[yhy] 获取任务列表失败: {e}")
        return jsonify({'success': False, 'error': str(e), 'data': []})


@app.route('/api/yhy/tasks/manual-lookup', methods=['POST'])
def lookup_yhy_manual_task_stock():
    """根据股票代码查询手动录入任务所需的股票信息"""
    try:
        payload = request.get_json(silent=True) or {}
        stock_info = _lookup_manual_task_stock_info(payload.get('code', ''))
        return jsonify({'success': True, 'data': stock_info})
    except Exception as e:
        print(f"[yhy] 手动录入查询失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/yhy/tasks/add', methods=['POST'])
def add_yhy_task():
    """新增阴后强人工任务，按日期写入 myStock:task:YYYYMMDD"""
    try:
        payload = request.get_json(silent=True) or {}
        task_type = str(payload.get('task_type', '')).strip()
        code = str(payload.get('code', '')).strip()
        name = str(payload.get('name', '')).strip()
        date = str(payload.get('date', '')).strip() or datetime.now().strftime('%Y-%m-%d')
        source_page = _yhy_normalize_source_page_name(payload.get('source_page', '')) or '阴后强'

        def _payload_bool(name, default=False):
            value = payload.get(name, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            return str(value).strip().lower() in ('1', 'true', 'yes', 'y', '是')

        if task_type not in ('yizi', 'changzi', 'changchangzi', 'hengpan'):
            return jsonify({'success': False, 'error': 'task_type 非法'})
        if not code:
            return jsonify({'success': False, 'error': '股票代码不能为空'})
        if not name:
            return jsonify({'success': False, 'error': '股票名称不能为空'})

        task_item = {
            'task_type': task_type,
            'code': code,
            'name': name,
            'stock_code_with_market': str(payload.get('stock_code_with_market', '') or ''),
            'ref_gain': float(payload.get('ref_gain', 0) or 0),
            'today_change': float(payload.get('today_change', payload.get('ref_gain', 0)) or 0),
            'market_cap': float(payload.get('market_cap', 0) or 0),
            'yin_count': int(payload.get('yin_count', 0) or 0),
            'high_gain_count': int(payload.get('high_gain_count', 0) or 0),
            'gains_10': float(payload.get('gains_10', 0) or 0),
            'max_drawdown_10d': float(payload.get('max_drawdown_10d', 0) or 0),
            'high_gain_10d_count': int(payload.get('high_gain_10d_count', payload.get('high_gain_count', 0)) or 0),
            'days_since_top_day': int(payload.get('days_since_top_day', 0) or 0),
            'strong_adjust_k_pct': float(payload.get('strong_adjust_k_pct', 0) or 0),
            'cum_high_to_ma5_pct': float(payload.get('cum_high_to_ma5_pct', 0) or 0),
            'all_yin_up': _payload_bool('all_yin_up'),
            'all_yin_no_touch': _payload_bool('all_yin_no_touch'),
            'all_yin_cross_star': _payload_bool('all_yin_cross_star'),
            'layang': _payload_bool('layang'),
            'ref_limit_up': _payload_bool('ref_limit_up'),
            'gain_confirm_level': str(payload.get('gain_confirm_level', '') or ''),
            'added_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'source_page': source_page,
            'source_pages': [source_page],
            'jipin_focus': _payload_bool('jipin_focus'),
            'focus_reason': str(payload.get('focus_reason', '') or ''),
            'focus_reasons': [],
            'jipin_focus_at': '',
        }

        r = get_redis_client()
        target_key = _yhy_task_key(date)
        existing_raw = r.hget(target_key, code)
        existing_obj = _yhy_decode_json_payload(existing_raw) if existing_raw else None
        if isinstance(existing_obj, dict):
            task_item['source_pages'] = _yhy_normalize_source_pages(existing_obj, source_page)
            for key, value in existing_obj.items():
                if key not in task_item or task_item.get(key) in ('', 0, False, []):
                    task_item[key] = value
            task_item['task_type'] = str(existing_obj.get('task_type') or task_item.get('task_type') or task_type)
            task_item['source_page'] = source_page

        if _payload_bool('jipin_focus'):
            focus_reason = str(payload.get('focus_reason', '') or '').strip()
            existing_reasons = []
            if isinstance(existing_obj, dict):
                raw_reasons = existing_obj.get('focus_reasons', [])
                if isinstance(raw_reasons, list):
                    existing_reasons.extend(str(item) for item in raw_reasons if str(item).strip())
                old_reason = str(existing_obj.get('focus_reason', '') or '').strip()
                if old_reason and old_reason not in existing_reasons:
                    existing_reasons.append(old_reason)
            if focus_reason and focus_reason not in existing_reasons:
                existing_reasons.append(focus_reason)
            task_item['jipin_focus'] = True
            task_item['focus_reason'] = focus_reason
            task_item['focus_reasons'] = existing_reasons
            task_item['jipin_focus_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        r.hset(target_key, code, _yhy_encode_json_payload(task_item))
        try:
            r.expire(target_key, 30 * 24 * 3600)
        except Exception:
            pass

        return jsonify({'success': True, 'data': task_item, 'message': '添加成功'})
    except Exception as e:
        print(f"[yhy] 添加任务失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/yhy/tasks/remove', methods=['POST'])
def remove_yhy_task():
    """删除阴后强人工任务，按日期从 myStock:task:YYYYMMDD 删除"""
    try:
        payload = request.get_json(silent=True) or {}
        code = str(payload.get('code', '')).strip()
        date = str(payload.get('date', '')).strip() or datetime.now().strftime('%Y-%m-%d')

        if not code:
            return jsonify({'success': False, 'error': '股票代码不能为空'})

        r = get_redis_client()
        target_key = _yhy_task_key(date)
        r.hdel(target_key, code)
        return jsonify({'success': True, 'message': '删除成功'})
    except Exception as e:
        print(f"[yhy] 删除任务失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/5ls/dates')
def get_five_ls_dates():
    """获取 5拉升 所有有数据的日期"""
    try:
        r = get_redis_client()
        keys = r.keys('5ls:*')
        dates = []
        for key in keys:
            if isinstance(key, bytes):
                key = key.decode()
            if ':' in key:
                date_part = key.split(':', 1)[1]
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        dates = sorted(list(set(dates)), reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/ztqhp/dates')
def get_ztqhp_dates():
    """获取 涨停前横盘 所有有数据的日期"""
    try:
        r = get_redis_client()
        keys = r.keys('ztqhp:*')
        dates = []
        for key in keys:
            if isinstance(key, bytes):
                key = key.decode()
            if ':' in key:
                date_part = key.split(':', 1)[1]
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        dates = sorted(list(set(dates)), reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/intraday4up/dates')
def get_intraday4up_dates():
    """获取 连续4天拉升 所有有数据的日期"""
    try:
        r = get_redis_client()
        keys = r.keys('intraday4up:*')
        dates = []
        for key in keys:
            if isinstance(key, bytes):
                key = key.decode()
            if ':' in key:
                date_part = key.split(':', 1)[1]
                if date_part and len(date_part) == 8 and date_part.isdigit():
                    formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    dates.append(formatted_date)
        dates = sorted(list(set(dates)), reverse=True)
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'dates': []})


@app.route('/api/5ls/stocks')
def get_five_ls_stocks():
    """读取 Redis key=5ls:YYYYMMDD 的 5拉升数据"""
    try:
        r = get_redis_client()
        date = request.args.get('date')
        latest_key = _five_ls_resolve_target_key(r, date)
        if not latest_key:
            target_key = f"5ls:{date.replace('-', '')}" if date else '5ls:*'
            return jsonify({'success': True, 'data': [], 'message': f'key {target_key} 不存在'})

        key_type = r.type(latest_key)
        stocks = []
        scan_info = None

        if key_type in (b'hash', 'hash'):
            all_fields = r.hgetall(latest_key)
            for field, value in all_fields.items():
                if isinstance(field, bytes):
                    field = field.decode('utf-8', errors='ignore')
                if field == '_scan_info':
                    scan_info = _yhy_normalize_scan_info(_yhy_decode_json_payload(value))
                    continue
                if field == '_timestamp':
                    continue

                obj = _yhy_decode_json_payload(value)
                if isinstance(obj, dict):
                    stocks.append(obj)
        elif key_type in (b'string', 'string'):
            raw = r.get(latest_key)
            parsed = _yhy_decode_json_payload(raw)
            if isinstance(parsed, list):
                stocks = parsed
            elif isinstance(parsed, dict):
                stocks = [parsed]

        result = []
        for s in stocks:
            try:
                result.append({
                    'code': str(s.get('code', '')),
                    'name': str(s.get('name', '')),
                    'stock_code_with_market': str(s.get('stock_code_with_market', '')),
                    'ref_gain': float(s.get('ref_gain', 0) or 0),
                    'ref_max_gain': float(s.get('ref_max_gain', 0) or 0),
                    'today_change': float(s.get('today_change', s.get('ref_gain', 0)) or 0),
                    'current_price': float(s.get('current_price', 0) or 0),
                    'current_high': float(s.get('current_high', 0) or 0),
                    'prev_close': float(s.get('prev_close', 0) or 0),
                    'realtime_update_time': str(s.get('realtime_update_time', '')),
                    'ref_limit_up': bool(s.get('ref_limit_up', False)),
                    'high_gain_count': int(s.get('high_gain_count', 0) or 0),
                    'close_below_ma5_10d_count': (None if s.get('close_below_ma5_10d_count', None) in (None, '') else int(s.get('close_below_ma5_10d_count', 0))),
                    'yin_close_below_ma5_10d_count': int(s.get('yin_close_below_ma5_10d_count', 0) or 0),
                    'gains_10': float(s.get('gains_10', 0) or 0),
                    'lsyin_sum': float(s.get('lsyin_sum', 0) or 0),
                    'lsyin_avg': float(s.get('lsyin_avg', 0) or 0),
                    'yin_body_pct_sum_10d': float(s.get('yin_body_pct_sum_10d', 0) or 0),
                    'max_drawdown_10d': float(s.get('max_drawdown_10d', 0) or 0),
                    'yin_count': int(s.get('yin_count', 0) or 0),
                    'all_yin_up': bool(s.get('all_yin_up', False)),
                    'all_yin_no_touch': bool(s.get('all_yin_no_touch', False)),
                    'all_yin_cross_star': bool(s.get('all_yin_cross_star', False)),
                    'all_yin_after_strong_10d': bool(s.get('all_yin_after_strong_10d', False)),
                    'almost_bare_foot_yang_count': int(s.get('almost_bare_foot_yang_count', 0) or 0),
                    'bare_foot_yang_count_10d': int(s.get('bare_foot_yang_count_10d', 0) or 0),
                    'almost_bare_foot_yang_gte3': bool(s.get('almost_bare_foot_yang_gte3', False)),
                    'yin_count_lt1': bool(s.get('yin_count_lt1', False)),
                    'all_yin_pull_prev_or_next': bool(s.get('all_yin_pull_prev_or_next', False)),
                    'non_special_limit_up': bool(s.get('non_special_limit_up', False)),
                    'ref_high_gain_ok': bool(s.get('ref_high_gain_ok', False)),
                    'ref_is_yang': bool(s.get('ref_is_yang', False)),
                    'is_20d_new_high': bool(s.get('is_20d_new_high', False)),
                    'ref_high_gain_gte_6_5_and_yang': bool(s.get('ref_high_gain_gte_6_5_and_yang', False)),
                    'ref_bare_foot_yang_high_gain_gte_6_5': int(s.get('ref_bare_foot_yang_high_gain_gte_6_5', 0) or 0),
                    'ref_bare_foot_yang_high_gain_gte_6_5_low_gt_ma5': int(s.get('ref_bare_foot_yang_high_gain_gte_6_5_low_gt_ma5', 0) or 0),
                    'first_found_time': str(s.get('first_found_time', '')),
                })
            except Exception:
                continue

        date_str = latest_key.replace('5ls:', '') if isinstance(latest_key, str) else latest_key.decode().replace('5ls:', '')
        return jsonify({'success': True, 'data': result, 'count': len(result), 'date': date_str, 'scan_info': scan_info})
    except Exception as e:
        print(f"[5ls] 获取数据失败: {e}")
        return jsonify({'success': False, 'error': str(e), 'data': []})


@app.route('/api/5ls/realtime', methods=['POST'])
def get_five_ls_realtime():
    """5拉升实时涨跌幅接口"""
    try:
        import time as time_module
        data = request.get_json()
        codes = data.get('codes', []) if isinstance(data, dict) else []

        if not codes:
            return jsonify({'success': True, 'data': {}})

        request_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[5ls][realtime] start at={request_at} codes={len(codes)} sample={codes[:5]}")
        realtime_data = {}
        for code in codes:
            try:
                if '.' in code:
                    if code.endswith('.SH'):
                        tencent_code = f"sh{code.replace('.SH', '')}"
                    elif code.endswith('.SZ'):
                        tencent_code = f"sz{code.replace('.SZ', '')}"
                    else:
                        tencent_code = f"sz{code.split('.')[0]}"
                else:
                    if str(code).startswith('6'):
                        tencent_code = f"sh{code}"
                    else:
                        tencent_code = f"sz{code}"

                url = f"http://qt.gtimg.cn/q={tencent_code}"
                request_start = time_module.time()
                response = requests.get(url, timeout=2)
                response.encoding = 'gbk'
                elapsed_ms = (time_module.time() - request_start) * 1000

                if response.text:
                    match = response.text.strip().split('~')
                    print(
                        f"[5ls][realtime] raw code={code} tencent_code={tencent_code} "
                        f"elapsed_ms={elapsed_ms:.2f} raw={response.text.strip()[:240]}"
                    )
                    if len(match) > 4:
                        current_price = float(match[3]) if match[3] else 0
                        pre_close = float(match[4]) if match[4] else 0
                        if pre_close > 0:
                            change = ((current_price - pre_close) / pre_close) * 100
                            print(
                                f"[5ls][realtime] parsed code={code} tencent_code={tencent_code} "
                                f"current={current_price} pre_close={pre_close} change={round(change, 2)}"
                            )
                            realtime_data[str(code)] = {'change': round(change, 2)}
            except Exception as e:
                print(f"[5ls][realtime] failed code={code} error={e}")
                continue

        print(
            f"[5ls][realtime] done count={len(realtime_data)} "
            f"sample={dict(list(realtime_data.items())[:3])}"
        )

        return jsonify({'success': True, 'data': realtime_data})
    except Exception as e:
        print(f"[5ls][realtime] request_failed error={e}")
        return jsonify({'success': False, 'error': str(e), 'data': {}})


@app.route('/api/ztqhp/stocks')
def get_ztqhp_stocks():
    """读取 Redis key=ztqhp:YYYYMMDD 的涨停前横盘数据"""
    try:
        r = get_redis_client()
        date = request.args.get('date')
        latest_key = _ztqhp_resolve_target_key(r, date)
        if not latest_key:
            target_key = f"ztqhp:{date.replace('-', '')}" if date else 'ztqhp:*'
            return jsonify({'success': True, 'data': [], 'message': f'key {target_key} 不存在'})

        key_type = r.type(latest_key)
        stocks = []
        scan_info = None

        if key_type in (b'hash', 'hash'):
            all_fields = r.hgetall(latest_key)
            for field, value in all_fields.items():
                if isinstance(field, bytes):
                    field = field.decode('utf-8', errors='ignore')
                if field == '_scan_info':
                    scan_info = _yhy_normalize_scan_info(_yhy_decode_json_payload(value))
                    continue
                if field == '_timestamp':
                    continue
                obj = _yhy_decode_json_payload(value)
                if isinstance(obj, dict):
                    stocks.append(obj)
        elif key_type in (b'string', 'string'):
            raw = r.get(latest_key)
            parsed = _yhy_decode_json_payload(raw)
            if isinstance(parsed, list):
                stocks = parsed
            elif isinstance(parsed, dict):
                stocks = [parsed]

        result = []
        for s in stocks:
            try:
                result.append({
                    'code': str(s.get('code', '')),
                    'name': str(s.get('name', '')),
                    'stock_code_with_market': str(s.get('stock_code_with_market', '')),
                    'max_n': int(s.get('max_n', 0) or 0),
                    'hb_range_pct': float(s.get('hb_range_pct', 0) or 0),
                    'hb_high_close': float(s.get('hb_high_close', 0) or 0),
                    'hb_low_close': float(s.get('hb_low_close', 0) or 0),
                    'ref_gain': float(s.get('ref_gain', 0) or 0),
                    'ref_close': float(s.get('ref_close', 0) or 0),
                })
            except Exception:
                continue

        date_str = latest_key.replace('ztqhp:', '') if isinstance(latest_key, str) else latest_key.decode().replace('ztqhp:', '')
        return jsonify({'success': True, 'data': result, 'count': len(result), 'date': date_str, 'scan_info': scan_info})
    except Exception as e:
        print(f"[ztqhp] 获取数据失败: {e}")
        return jsonify({'success': False, 'error': str(e), 'data': []})


@app.route('/api/intraday4up/stocks')
def get_intraday4up_stocks():
    """读取 Redis key=intraday4up:YYYYMMDD 的连续4天拉升数据"""
    try:
        r = get_redis_client()
        date = request.args.get('date')
        latest_key = _intraday4up_resolve_target_key(r, date)
        if not latest_key:
            target_key = f"intraday4up:{date.replace('-', '')}" if date else 'intraday4up:*'
            return jsonify({'success': True, 'data': [], 'message': f'key {target_key} 不存在'})

        key_type = r.type(latest_key)
        stocks = []
        scan_info = None

        if key_type in (b'hash', 'hash'):
            all_fields = r.hgetall(latest_key)
            for field, value in all_fields.items():
                if isinstance(field, bytes):
                    field = field.decode('utf-8', errors='ignore')
                if field == '_scan_info':
                    scan_info = _yhy_normalize_scan_info(_yhy_decode_json_payload(value))
                    continue
                if field == '_timestamp':
                    continue
                obj = _yhy_decode_json_payload(value)
                if isinstance(obj, dict):
                    stocks.append(obj)
        elif key_type in (b'string', 'string'):
            raw = r.get(latest_key)
            parsed = _yhy_decode_json_payload(raw)
            if isinstance(parsed, list):
                stocks = parsed
            elif isinstance(parsed, dict):
                stocks = [parsed]

        result = []
        for s in stocks:
            try:
                result.append({
                    'code': str(s.get('code', '')),
                    'name': str(s.get('name', '')),
                    'stock_code_with_market': str(s.get('stock_code_with_market', '')),
                    'gain_4d_pct': float(s.get('gain_4d_pct', 0) or 0),
                    'today_change': _intraday4up_calc_today_change(s),
                    'max_rebound_4d': float(s.get('max_rebound_4d', 0) or 0),
                    'min_pass_metric_4d': float(s.get('min_pass_metric_4d', 0) or 0),
                    'pass_days': int(s.get('pass_days', 0) or 0),
                    'day_details': s.get('day_details', []) if isinstance(s.get('day_details', []), list) else [],
                    'update_time': str(s.get('update_time', '') or ''),
                })
            except Exception:
                continue

        result.sort(key=lambda item: (float(item.get('gain_4d_pct', 0) or 0), str(item.get('code', ''))))
        date_str = latest_key.replace('intraday4up:', '') if isinstance(latest_key, str) else latest_key.decode().replace('intraday4up:', '')
        return jsonify({
            'success': True,
            'data': result,
            'count': len(result),
            'date': date_str,
            'redis_key': f'intraday4up:{date_str}',
            'scan_info': scan_info,
        })
    except Exception as e:
        print(f"[intraday4up] 获取数据失败: {e}")
        return jsonify({'success': False, 'error': str(e), 'data': []})


@app.route('/api/intraday4up/check_update')
def check_intraday4up_update():
    """检查连续4天拉升数据是否更新"""
    try:
        r = get_redis_client()
        today_compact = datetime.now().strftime('%Y%m%d')
        key = f"intraday4up:{today_compact}"
        if r.exists(key):
            count = r.hlen(key) if r.type(key) in (b'hash', 'hash') else 0
            ts_key = f"intraday4up:{today_compact}:timestamp"
            timestamp = r.get(ts_key) if r.exists(ts_key) else str(count)
            return jsonify({
                'success': True,
                'updated': True,
                'timestamp': timestamp,
                'count': count,
                'refresh_interval': get_redis_refresh_interval()
            })
        return jsonify({
            'success': True,
            'updated': False,
            'timestamp': None,
            'refresh_interval': get_redis_refresh_interval()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/jpgz/fiveday')
def get_jpgz_fiveday():
    """极品关注最近5个交易日任务列表数据"""
    try:
        r = get_redis_client()
        trade_keys = r.keys('yhy:*')
        sorted_dates = _jpgz_extract_recent_trade_dates(trade_keys, top_n=5)

        result_data = {}
        for date in sorted_dates:
            result_data[date] = _jpgz_read_task_groups(r, date)

        return jsonify({'success': True, 'dates': sorted_dates, 'data': result_data})

    except Exception as e:
        print(f"[jpgz] 获取5日数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)})




