# -*- coding: gbk -*-
"""
QMT自动止损策略 - 追单版
功能：
1. 查询账户持仓股票
2. 监控每只股票的当日涨跌幅
3. 当涨跌幅 <= -5% 时，自动市价卖出
4. 追单功能：只要没卖光，就一直尝试卖
5. 检查未成交挂单，防止重复下单

注意事项：
- 市价委托使用"五档即成剩撤"提高成交概率
- 会检查是否有未成交挂单，避免重复下单
- 建议在交易时间运行
"""

from datetime import datetime
import threading

# ========== 配置参数 ==========
STOP_LOSS_PERCENT = -5.0  # 止损触发线，-5%
CHECK_INTERVAL = 1  # 检查间隔（秒）
DEBUG_MODE = False  # 调试模式，True时只打印不下单，False时实盘下单
CHECK_TRADING_TIME = True  # 是否检查交易时间

# ========== 测试下单配置（仅DEBUG_MODE=False时生效） ==========
TEST_ORDER_ENABLED = False  # 是否启用测试下单
TEST_ORDER_STOCK = "000001.SZ"  # 测试股票代码
TEST_ORDER_VOLUME = 100  # 测试下单数量
TEST_ORDER_DIRECTION = "buy"  # 测试方向: "buy"=买入, "sell"=卖出
TEST_ORDER_PRICE_OFFSET = 0.1  # 测试价格偏移百分比，如+0.1%, +5.0%, -5.0%

# 账户配置（根据实际情况修改）
ACCOUNT_ID = "62254788"  # 资金账号
ACCOUNT_TYPE = "STOCK"  # 账户类型：STOCK=股票

# 全局变量
g_context = None
g_initialized = False
g_triggered_stocks = {}  # 已触发止损的股票 {stock: trigger_time}
g_check_count = 0
g_lock = threading.Lock()


def log(msg, level="INFO"):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {msg}")


def init(ContextInfo):
    global g_context, g_initialized
    
    g_context = ContextInfo
    
    log("=" * 60)
    log("========== 自动止损策略初始化（追单版） ==========")
    log("=" * 60)
    log(f"止损触发线: {STOP_LOSS_PERCENT}%")
    log(f"检查间隔: {CHECK_INTERVAL}秒")
    log(f"调试模式: {'开启（只打印不下单）' if DEBUG_MODE else '关闭（实盘下单）'}")
    log(f"交易时间检查: {'开启' if CHECK_TRADING_TIME else '关闭'}")
    log("")
    log("追单功能: 已启用")
    log("  - 触发止损后，只要还有持仓就持续尝试卖出")
    log("  - 自动检查未成交挂单，防止重复下单")
    
    # 设置定时器
    ContextInfo.run_time("check_stop_loss", f"{CHECK_INTERVAL}nSecond", "2024-01-01 09:30:00", "SH")
    log(f"定时器设置完成，每{CHECK_INTERVAL}秒检查一次")
    
    g_initialized = True
    log("=" * 60)
    log("========== 初始化完成 ==========")
    log("=" * 60)
    log("")
    
    # 测试下单（TEST_ORDER_ENABLED=True时自动执行一次）
    if TEST_ORDER_ENABLED:
        log("")
        log("★★★ 测试下单模式已启用 ★★★")
        run_test_order(ContextInfo)
    
    # 首次检查
    check_stop_loss(ContextInfo)


def handlebar(ContextInfo):
    pass


def is_trading_time(dt):
    """判断是否为交易时间"""
    t = dt.time()
    morning_start = datetime.strptime("09:30", "%H:%M").time()
    morning_end = datetime.strptime("11:30", "%H:%M").time()
    afternoon_start = datetime.strptime("13:00", "%H:%M").time()
    afternoon_end = datetime.strptime("15:00", "%H:%M").time()
    
    return (morning_start <= t <= morning_end) or (afternoon_start <= t <= afternoon_end)


def get_account_id(ContextInfo):
    """获取资金账号"""
    if ACCOUNT_ID:
        return ACCOUNT_ID
    
    # 使用全局函数 get_trade_detail_data 获取账户列表
    try:
        # 尝试获取账户信息
        acct_list = get_trade_detail_data("", "STOCK", "account")
        if acct_list and len(acct_list) > 0:
            acct_id = getattr(acct_list[0], 'm_strAccountID', '')
            if acct_id:
                return acct_id
    except:
        pass
    
    return ""


def get_positions(ContextInfo):
    """
    获取账户持仓
    返回: {stock: {volume, available_volume, cost_price}, ...}
    """
    positions = {}
    
    try:
        # 使用配置的账户ID
        account_id = ACCOUNT_ID if ACCOUNT_ID else ""
        
        # 尝试不同的账户类型
        account_types = [ACCOUNT_TYPE, "STOCK", "stock", "CREDIT", ""]
        
        for acct_type in account_types:
            try:
                pos_data = get_trade_detail_data(account_id, acct_type, "position")
                log(f"[DEBUG] 账户 '{account_id}' 类型 '{acct_type}': 返回 {len(pos_data) if pos_data else 0} 条")
                
                if pos_data and len(pos_data) > 0:
                    for item in pos_data:
                        stock_code = getattr(item, 'm_strInstrumentID', '') or getattr(item, 'stock_code', '')
                        volume = getattr(item, 'm_nVolume', 0) or getattr(item, 'volume', 0)
                        available = getattr(item, 'm_nCanUseVolume', 0) or getattr(item, 'available_volume', volume)
                        cost_price = getattr(item, 'm_dOpenPrice', 0) or getattr(item, 'cost_price', 0)
                        
                        # 补全股票代码后缀（注意判断顺序：更具体的条件放前面）
                        if stock_code and '.' not in stock_code:
                            if stock_code.startswith('68'):  # 科创板 688xxx
                                stock_code = stock_code + '.SH'
                            elif stock_code.startswith('6'):  # 上海主板 60xxxx
                                stock_code = stock_code + '.SH'
                            elif stock_code.startswith('8') or stock_code.startswith('4'):  # 北交所 8xxxxx/4xxxxx
                                stock_code = stock_code + '.BJ'
                            elif stock_code.startswith('0') or stock_code.startswith('3'):  # 深圳主板/创业板
                                stock_code = stock_code + '.SZ'
                            elif stock_code.startswith('9'):  # 上海B股
                                stock_code = stock_code + '.SH'
                            elif stock_code.startswith('2'):  # 深圳B股
                                stock_code = stock_code + '.SZ'
                        
                        log(f"[DEBUG] 解析: stock={stock_code}, vol={volume}, avail={available}")
                        
                        if stock_code and volume > 0:
                            positions[stock_code] = {
                                'volume': volume,
                                'available_volume': available,
                                'cost_price': cost_price
                            }
                    
                    if positions:
                        log(f"[DEBUG] 成功获取 {len(positions)} 只持仓")
                        return positions
            except Exception as e:
                log(f"[DEBUG] 账户 '{account_id}' 类型 '{acct_type}' 失败: {e}")
                continue
                
    except Exception as e:
        log(f"获取持仓异常: {e}", "ERROR")
    
    return positions


def get_pending_orders(ContextInfo):
    """
    获取未成交挂单
    返回: {stock: pending_sell_volume, ...}
    """
    pending = {}
    
    try:
        account_id = get_account_id(ContextInfo)
        
        # 方法1: 使用全局函数 get_trade_detail_data 获取委托
        try:
            orders = get_trade_detail_data(account_id, ACCOUNT_TYPE, "order")
            if orders:
                for order in orders:
                    # 获取委托状态
                    status = getattr(order, 'm_nOrderStatus', -1) or getattr(order, 'order_status', -1)
                    # 未成交状态：0=未报, 1=待报, 2=已报, 3=已报待撤, 5=部成, 6=部撤
                    # 已完成状态：4=已撤, 7=已成, 8=废单
                    if status in [0, 1, 2, 3, 5, 6]:
                        stock = getattr(order, 'm_strInstrumentID', '') or getattr(order, 'stock_code', '')
                        direction = getattr(order, 'm_nOffsetFlag', 0) or getattr(order, 'direction', 0)
                        # 卖出方向：24=卖出
                        if direction == 24 or direction == 2 or str(direction).lower() == 'sell':
                            volume = getattr(order, 'm_nVolume', 0) or getattr(order, 'volume', 0)
                            traded = getattr(order, 'm_nTradedVolume', 0) or getattr(order, 'traded_volume', 0)
                            pending_vol = volume - traded
                            if stock and pending_vol > 0:
                                pending[stock] = pending.get(stock, 0) + pending_vol
                return pending
        except Exception as e:
            log(f"get_trade_detail_data 获取委托失败: {e}", "WARN")
            
    except Exception as e:
        log(f"获取未成交委托异常: {e}", "ERROR")
    
    return pending


def get_prev_close(ContextInfo, stock):
    """获取昨收价"""
    try:
        tick = ContextInfo.get_full_tick([stock])
        if tick and stock in tick:
            prev_close = tick[stock].get('lastClose', 0) or tick[stock].get('preClose', 0)
            if prev_close and prev_close > 0:
                return prev_close
        
        data = ContextInfo.get_market_data(['close'], [stock], period='1d', count=2)
        if data is not None:
            try:
                if hasattr(data, 'minor_xs'):
                    stock_data = data.minor_xs(stock)
                elif hasattr(data, 'loc'):
                    stock_data = data.loc[:, :, stock]
                else:
                    stock_data = data[stock]
                
                if stock_data is not None and len(stock_data) >= 2:
                    return float(stock_data['close'].iloc[-2])
            except:
                pass
    except Exception as e:
        log(f"获取昨收价失败 [{stock}]: {e}", "WARN")
    
    return 0


def get_current_price(ContextInfo, stock):
    """获取当前价格"""
    try:
        tick = ContextInfo.get_full_tick([stock])
        if tick and stock in tick:
            price = tick[stock].get('lastPrice', 0) or tick[stock].get('price', 0)
            if price and price > 0:
                return price
    except Exception as e:
        log(f"获取当前价格失败 [{stock}]: {e}", "WARN")
    
    return 0


def sell_stock_market_order(ContextInfo, stock, volume):
    """
    市价卖出股票
    使用"五档即成剩撤"提高成交概率
    """
    if DEBUG_MODE:
        log(f"[调试模式] 模拟卖出: {stock} 数量: {volume}", "DEBUG")
        return True
    
    try:
        account_id = get_account_id(ContextInfo)
        
        # 确保股票代码格式正确 (002788.SZ 而不是 SZ002788)
        if '.' not in stock:
            log(f"股票代码格式错误: {stock}，需要 xxx.SZ 格式", "ERROR")
            return False
        
        log(f"[DEBUG] 准备卖出: stock={stock}, volume={volume}, account={account_id}")
        
        # 方法1: passorder 市价委托 (prType=11, 价格填-1)
        try:
            result = passorder(
                24,           # 卖出
                1101,         # 股票
                account_id,
                stock,
                11,           # 五档即成剩撤（市价委托）
                -1,           # 市价委托价格填-1
                volume,
                "止损策略",
                2,            # quickTrade: 2=普通
                "",
                ContextInfo
            )
            log(f">>> 市价卖出委托已提交: {stock} 数量: {volume} 结果: {result}")
            return True
        except Exception as e:
            log(f"passorder市价(prType=11)失败: {e}", "WARN")
        
        # 方法2: passorder 卖五价 (prType=5)
        try:
            result = passorder(
                24,           # 卖出
                1101,         # 股票
                account_id,
                stock,
                5,            # 卖五价
                -1,           # 价格填-1
                volume,
                "止损策略",
                2,
                "",
                ContextInfo
            )
            log(f">>> 卖五价委托已提交: {stock} 数量: {volume} 结果: {result}")
            return True
        except Exception as e:
            log(f"passorder卖五价(prType=5)失败: {e}", "WARN")
        
        # 方法3: 获取当前价格，用限价委托
        try:
            current_price = get_current_price(ContextInfo, stock)
            if current_price > 0:
                # 用跌停价卖出，确保成交
                limit_price = round(current_price * 0.9, 2)
                result = passorder(
                    24,           # 卖出
                    1101,         # 股票
                    account_id,
                    stock,
                    4,            # 限价委托
                    limit_price,  # 跌停价
                    volume,
                    "止损策略",
                    2,
                    "",
                    ContextInfo
                )
                log(f">>> 限价卖出委托已提交: {stock} 数量: {volume} 价格: {limit_price} 结果: {result}")
                return True
        except Exception as e:
            log(f"passorder限价失败: {e}", "WARN")
            
    except Exception as e:
        log(f"卖出股票异常 [{stock}]: {e}", "ERROR")
    
    return False


def check_stop_loss(ContextInfo):
    """
    检查止损条件 - 追单版
    1. 检查是否触发止损
    2. 检查已触发的股票是否还有持仓
    3. 检查是否有未成交挂单（防止重复下单）
    4. 执行卖出
    """
    global g_check_count, g_triggered_stocks
    
    current_time = datetime.now()
    
    if CHECK_TRADING_TIME and not is_trading_time(current_time):
        return
    
    g_check_count += 1
    
    log("")
    log("-" * 60)
    log(f"第 {g_check_count} 次止损检查（追单版） | {current_time.strftime('%H:%M:%S')}")
    log("-" * 60)
    
    # 获取持仓
    positions = get_positions(ContextInfo)
    
    if not positions:
        log("当前无持仓")
        # 清理已触发列表中已清仓的股票
        with g_lock:
            g_triggered_stocks = {k: v for k, v in g_triggered_stocks.items() if k in positions}
        return
    
    log(f"当前持仓: {len(positions)} 只股票")
    
    # 获取未成交挂单
    pending_orders = get_pending_orders(ContextInfo)
    if pending_orders:
        log(f"未成交卖单: {len(pending_orders)} 只")
        for stock, vol in pending_orders.items():
            log(f"  挂单中: {stock} 待成交: {vol}")
    
    # 需要卖出的股票列表
    stocks_to_sell = []
    
    # 先收集所有股票信息，用于排序显示
    stock_info_list = []
    
    for stock, pos in positions.items():
        volume = pos['volume']
        available = pos['available_volume']
        
        # 获取价格
        prev_close = get_prev_close(ContextInfo, stock)
        current_price = get_current_price(ContextInfo, stock)
        
        if prev_close <= 0 or current_price <= 0:
            log(f"  {stock}: 无法获取价格数据", "WARN")
            continue
        
        # 计算涨跌幅
        change_pct = (current_price - prev_close) / prev_close * 100
        
        # 检查是否已有未成交挂单
        pending_vol = pending_orders.get(stock, 0)
        
        # 实际可卖数量 = 可用数量 - 挂单中数量
        real_available = max(0, available - pending_vol)
        
        # 判断是否已触发止损
        is_triggered = stock in g_triggered_stocks
        
        # 新触发止损
        if not is_triggered and change_pct <= STOP_LOSS_PERCENT:
            with g_lock:
                g_triggered_stocks[stock] = current_time.strftime('%H:%M:%S')
            is_triggered = True
        
        # 收集股票信息
        stock_info_list.append({
            'stock': stock,
            'prev_close': prev_close,
            'current_price': current_price,
            'change_pct': change_pct,
            'volume': volume,
            'available': available,
            'real_available': real_available,
            'pending_vol': pending_vol,
            'is_triggered': is_triggered
        })
        
        # 追单逻辑：已触发止损 + 还有实际可卖数量
        if is_triggered and real_available > 0:
            stocks_to_sell.append({
                'stock': stock,
                'volume': real_available,
                'prev_close': prev_close,
                'current_price': current_price,
                'change_pct': change_pct,
                'pending_vol': pending_vol
            })
    
    # 按涨跌幅从高到低排序显示
    stock_info_list.sort(key=lambda x: x['change_pct'], reverse=True)
    
    for info in stock_info_list:
        status_str = ""
        if info['is_triggered']:
            status_str = "[止损中]"
            log(f"  ★★★ {info['stock']}: 昨收={info['prev_close']:.2f} 现价={info['current_price']:.2f} 涨跌={info['change_pct']:.2f}% 持仓={info['volume']} 可卖={info['available']} {status_str}", "ALERT")
        else:
            if info['pending_vol'] > 0:
                status_str = f"[挂单:{info['pending_vol']}]"
            log(f"  {info['stock']}: 昨收={info['prev_close']:.2f} 现价={info['current_price']:.2f} 涨跌={info['change_pct']:.2f}% 持仓={info['volume']} 可卖={info['available']} {status_str}")
    
    # 清理已清仓的触发记录
    with g_lock:
        g_triggered_stocks = {k: v for k, v in g_triggered_stocks.items() if k in positions}
    
    # 执行卖出
    if stocks_to_sell:
        log("")
        log("=" * 60)
        log(f"执行追单卖出，共 {len(stocks_to_sell)} 只股票")
        log("=" * 60)
        
        for item in stocks_to_sell:
            stock = item['stock']
            volume = item['volume']
            pending = item['pending_vol']
            
            if pending > 0:
                log(f">>> 追单: {stock} 本次卖出: {volume} (已有挂单: {pending}) 涨跌: {item['change_pct']:.2f}%")
            else:
                log(f">>> 卖出: {stock} 数量: {volume} 涨跌: {item['change_pct']:.2f}%")
            
            success = sell_stock_market_order(ContextInfo, stock, volume)
            
            if success:
                log(f">>> {stock} 卖出委托成功")
            else:
                log(f">>> {stock} 卖出委托失败", "ERROR")
        
        log("=" * 60)
    
    # 显示止损追踪状态
    if g_triggered_stocks:
        log("")
        log("当前止损追踪中的股票:")
        for stock, trigger_time in g_triggered_stocks.items():
            pos = positions.get(stock, {})
            vol = pos.get('volume', 0)
            avail = pos.get('available_volume', 0)
            pending = pending_orders.get(stock, 0)
            log(f"  {stock}: 触发时间={trigger_time} 剩余持仓={vol} 可卖={avail} 挂单中={pending}")
    
    log("-" * 60)


def reset_triggered_stocks(ContextInfo):
    """重置已触发止损记录（每日开盘前调用）"""
    global g_triggered_stocks
    g_triggered_stocks = {}
    log("已触发止损记录已重置")


def manual_check(ContextInfo):
    """手动触发检查"""
    log(">>> 手动触发止损检查")
    check_stop_loss(ContextInfo)


def set_stop_loss_percent(percent):
    """设置止损百分比"""
    global STOP_LOSS_PERCENT
    STOP_LOSS_PERCENT = percent
    log(f"止损触发线已设置为: {STOP_LOSS_PERCENT}%")


def set_debug_mode(enabled):
    """设置调试模式"""
    global DEBUG_MODE
    DEBUG_MODE = enabled
    log(f"调试模式: {'开启' if DEBUG_MODE else '关闭'}")


def get_triggered_stocks():
    """获取当前止损追踪中的股票"""
    return dict(g_triggered_stocks)


def run_test_order(ContextInfo):
    """
    执行测试下单（根据配置参数自动下单一次）
    TEST_ORDER_ENABLED = True 时在初始化时自动调用
    """
    log("=" * 60)
    log("执行测试下单")
    log("=" * 60)
    log(f"测试股票: {TEST_ORDER_STOCK}")
    log(f"测试数量: {TEST_ORDER_VOLUME}")
    log(f"测试方向: {TEST_ORDER_DIRECTION}")
    log(f"价格偏移: {TEST_ORDER_PRICE_OFFSET:+.2f}%")
    log(f"DEBUG_MODE: {DEBUG_MODE}")
    log("")
    
    if TEST_ORDER_DIRECTION == "buy":
        test_buy_order(ContextInfo, TEST_ORDER_STOCK, TEST_ORDER_VOLUME, TEST_ORDER_PRICE_OFFSET)
    elif TEST_ORDER_DIRECTION == "sell":
        test_sell_order(ContextInfo, TEST_ORDER_STOCK, TEST_ORDER_VOLUME, TEST_ORDER_PRICE_OFFSET)
    elif TEST_ORDER_DIRECTION == "market_sell":
        test_market_sell(ContextInfo, TEST_ORDER_STOCK, TEST_ORDER_VOLUME)
    else:
        log(f"未知的测试方向: {TEST_ORDER_DIRECTION}", "ERROR")
    
    log("=" * 60)
    log("测试下单完成")
    log("=" * 60)


# ========== 测试下单功能 ==========
def test_buy_order(ContextInfo, stock=None, volume=None, price_offset=None):
    """
    测试买入下单
    stock: 股票代码，如 "000001.SZ"
    volume: 买入数量，如 100
    price_offset: 价格偏移百分比，如 0.1 表示现价+0.1%，-0.1 表示现价-0.1%
    """
    stock = stock or TEST_ORDER_STOCK
    volume = volume or TEST_ORDER_VOLUME
    price_offset = price_offset if price_offset is not None else TEST_ORDER_PRICE_OFFSET
    
    log("=" * 60)
    log(f"测试买入下单")
    log("=" * 60)
    log(f"股票: {stock}")
    log(f"数量: {volume}")
    log(f"价格偏移: {price_offset:+.2f}%")
    
    # 获取当前价格
    current_price = get_current_price(ContextInfo, stock)
    if current_price <= 0:
        log(f"无法获取 {stock} 当前价格", "ERROR")
        return False
    
    # 计算委托价格
    order_price = round(current_price * (1 + price_offset / 100), 2)
    
    log(f"当前价格: {current_price:.2f}")
    log(f"委托价格: {order_price:.2f} ({price_offset:+.2f}%)")
    
    if DEBUG_MODE:
        log(f"[调试模式] 模拟买入: {stock} 数量: {volume} 价格: {order_price}", "DEBUG")
        return True
    
    try:
        account_id = get_account_id(ContextInfo)
        
        # 限价买入 (passorder是全局函数)
        result = passorder(
            23,  # 买入
            1101,  # 股票
            account_id,
            stock,
            4,  # 限价委托
            order_price,
            volume,
            "测试买入",
            1,
            "",
            ContextInfo
        )
        log(f">>> 买入委托已提交: {stock} 数量: {volume} 价格: {order_price} 结果: {result}")
        return True
    except Exception as e:
        log(f"买入下单失败: {e}", "ERROR")
        return False


def test_sell_order(ContextInfo, stock=None, volume=None, price_offset=None):
    """
    测试卖出下单 - 券商版QMT
    """
    stock = stock or TEST_ORDER_STOCK
    volume = volume or TEST_ORDER_VOLUME
    price_offset = price_offset if price_offset is not None else -abs(TEST_ORDER_PRICE_OFFSET)
    
    log("=" * 60)
    log(f"测试卖出下单（券商版）")
    log("=" * 60)
    log(f"股票: {stock}")
    log(f"数量: {volume}")
    log(f"价格偏移: {price_offset:+.2f}%")
    
    # 获取当前价格
    current_price = get_current_price(ContextInfo, stock)
    if current_price <= 0:
        log(f"无法获取 {stock} 当前价格", "ERROR")
        return False
    
    # 计算委托价格
    order_price = round(current_price * (1 + price_offset / 100), 2)
    
    log(f"当前价格: {current_price:.2f}")
    log(f"委托价格: {order_price:.2f} ({price_offset:+.2f}%)")
    log(f"账户ID: {ACCOUNT_ID}")
    
    if DEBUG_MODE:
        log(f"[调试模式] 模拟卖出: {stock} 数量: {volume} 价格: {order_price}", "DEBUG")
        return True
    
    account_id = ACCOUNT_ID
    
    # 打印可用的全局函数，帮助调试
    log(f"[DEBUG] 检查可用的下单函数...")
    available_funcs = []
    for func_name in ['passorder', 'order_stock', 'order_shares', 'deal', 'smart_order']:
        if func_name in dir():
            available_funcs.append(func_name)
    log(f"[DEBUG] 可用函数: {available_funcs}")
    
    # 方式1: passorder 标准格式（券商版）
    # passorder(opType, orderType, accountid, orderCode, prType, price, volume, strategyName, quickTrade, userOrderId, ContextInfo)
    try:
        log(f"[方式1] passorder 标准11参数...")
        result = passorder(
            24,           # opType: 24=卖出
            1101,         # orderType: 1101=股票买卖
            account_id,   # accountid: 资金账号
            stock,        # orderCode: 股票代码
            5,            # prType: 5=卖五价, 4=限价, 11=市价(五档即成剩撤)
            order_price,  # price: 委托价格
            volume,       # volume: 委托数量
            "止损测试",   # strategyName
            2,            # quickTrade: 2=普通
            "",           # userOrderId
            ContextInfo   # ContextInfo
        )
        log(f">>> [方式1] passorder返回: {result}")
    except Exception as e:
        log(f"[方式1] 异常: {e}", "ERROR")
    
    # 方式2: passorder 8参数格式
    try:
        log(f"[方式2] passorder 8参数...")
        result = passorder(
            24,           # opType
            1101,         # orderType
            account_id,   # accountid
            stock,        # orderCode
            5,            # prType
            order_price,  # price
            volume,       # volume
            ContextInfo   # ContextInfo
        )
        log(f">>> [方式2] passorder返回: {result}")
    except Exception as e:
        log(f"[方式2] 异常: {e}", "ERROR")
    
    # 方式3: passorder 使用市价委托
    try:
        log(f"[方式3] passorder 市价委托...")
        result = passorder(
            24,           # opType: 卖出
            1101,         # orderType: 股票
            account_id,   # accountid
            stock,        # orderCode
            11,           # prType: 11=五档即成剩撤(市价)
            0,            # price: 市价填0
            volume,       # volume
            "止损测试",
            2,
            "",
            ContextInfo
        )
        log(f">>> [方式3] passorder市价返回: {result}")
    except Exception as e:
        log(f"[方式3] 异常: {e}", "ERROR")
    
    # 方式4: 检查ContextInfo的方法
    log(f"[DEBUG] ContextInfo可用方法:")
    ci_methods = [m for m in dir(ContextInfo) if not m.startswith('_') and callable(getattr(ContextInfo, m, None))]
    for m in ci_methods[:30]:  # 只打印前30个
        log(f"  - {m}")
    
    # 方式5: 尝试 ContextInfo.order_stock
    try:
        log(f"[方式5] ContextInfo.order_stock...")
        if hasattr(ContextInfo, 'order_stock'):
            result = ContextInfo.order_stock(account_id, stock, volume, order_price, 'sell', 'limit')
            log(f">>> [方式5] 返回: {result}")
    except Exception as e:
        log(f"[方式5] 异常: {e}", "ERROR")
    
    log(f"测试下单完成，请检查QMT委托列表")
    return True


def test_market_sell(ContextInfo, stock=None, volume=None):
    """
    测试市价卖出（五档即成剩撤）
    stock: 股票代码
    volume: 卖出数量
    """
    stock = stock or TEST_ORDER_STOCK
    volume = volume or TEST_ORDER_VOLUME
    
    log("=" * 60)
    log(f"测试市价卖出")
    log("=" * 60)
    log(f"股票: {stock}")
    log(f"数量: {volume}")
    log(f"委托类型: 五档即成剩撤（市价）")
    
    current_price = get_current_price(ContextInfo, stock)
    if current_price > 0:
        log(f"当前价格: {current_price:.2f}")
    
    if DEBUG_MODE:
        log(f"[调试模式] 模拟市价卖出: {stock} 数量: {volume}", "DEBUG")
        return True
    
    return sell_stock_market_order(ContextInfo, stock, volume)


def test_order(ContextInfo, direction="buy", stock=None, volume=None, price_offset=None):
    """
    通用测试下单函数
    direction: "buy"=买入, "sell"=卖出, "market_sell"=市价卖出
    stock: 股票代码
    volume: 数量
    price_offset: 价格偏移百分比
    
    使用示例：
    test_order(ContextInfo, "buy", "000001.SZ", 100, 0.1)   # 买入100股，现价+0.1%
    test_order(ContextInfo, "sell", "000001.SZ", 100, -0.1) # 卖出100股，现价-0.1%
    test_order(ContextInfo, "market_sell", "000001.SZ", 100) # 市价卖出100股
    """
    if direction == "buy":
        return test_buy_order(ContextInfo, stock, volume, price_offset)
    elif direction == "sell":
        return test_sell_order(ContextInfo, stock, volume, price_offset)
    elif direction == "market_sell":
        return test_market_sell(ContextInfo, stock, volume)
    else:
        log(f"未知的下单方向: {direction}", "ERROR")
        return False
