# -*- coding: utf-8 -*-
"""
测试历史涨停判断逻辑
"""
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN


def get_limit_ratio(stock_code, stock_name):
    """根据股票代码和名称获取涨跌幅限制比例"""
    # 北交所 (30%)
    if stock_code.startswith('8') or stock_code.startswith('4') or stock_code.startswith('9'):
        return 0.30
    
    # 科创板 (20%)
    if stock_code.startswith('688'):
        return 0.20
    
    # 创业板 (20%)
    if stock_code.startswith('30'):
        return 0.20
    
    # 主板 (10% / 5%)
    is_st = 'ST' in stock_name.upper()
    if is_st:
        return 0.05
    else:
        return 0.10


def calculate_limit_price(prev_close, ratio, stock_code):
    """精准计算涨停价（兼容北交所严格限价规则）"""
    from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
    
    p = Decimal(str(prev_close))
    r = Decimal(str(ratio))
    target = p * (1 + r)
    
    is_strict_limit = (stock_code.startswith('8') or 
                      stock_code.startswith('4') or 
                      stock_code.startswith('9') or 
                      stock_code.startswith('688') or 
                      stock_code.startswith('30'))
    
    if is_strict_limit:
        limit_price_round = target.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        actual_ratio = (limit_price_round - p) / p
        
        if actual_ratio > r:
            limit_price = target.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        else:
            limit_price = limit_price_round
    else:
        limit_price = target.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    
    return float(limit_price)


def check_condition3_history(c, h, v, prev_c, stock_code, stock_name):
    """
    条件3：涨停（历史数据）
    使用精确的涨停价计算方法判断
    """
    if prev_c <= 0 or v <= 0:
        return False
    
    # 1. 获取涨停比例
    limit_pct = get_limit_ratio(stock_code, stock_name)
    
    # 2. 计算精确的涨停价
    limit_price = calculate_limit_price(prev_c, limit_pct, stock_code)
    
    # 3. 判断收盘价是否达到涨停价（允许0.2%误差）
    price_ok = c >= limit_price * 0.998
    
    # 4. 判断是否封板（收盘价达到最高价的99.8%）
    sealed = c >= h * 0.998
    
    # 条件3：涨停
    return price_ok and sealed


def test_limit_check(stock_code, stock_name, prev_close, close, high, volume):
    """测试涨停判断"""
    # 计算涨停价
    limit_pct = get_limit_ratio(stock_code, stock_name)
    limit_price = calculate_limit_price(prev_close, limit_pct, stock_code)
    
    # 判断是否涨停
    is_limit = check_condition3_history(close, high, volume, prev_close, stock_code, stock_name)
    
    # 计算实际涨幅
    actual_gain = (close - prev_close) / prev_close * 100
    
    # 计算价格达标率
    price_ratio = close / limit_price * 100
    
    # 计算封板率
    seal_ratio = close / high * 100
    
    return {
        'limit_pct': limit_pct * 100,
        'limit_price': limit_price,
        'actual_gain': actual_gain,
        'price_ratio': price_ratio,
        'seal_ratio': seal_ratio,
        'is_limit': is_limit
    }


# ================= 测试案例 =================
print("=" * 80)
print("历史涨停判断测试")
print("=" * 80)

# 案例1：主板涨停 - 创新医疗
print("\n【案例1】主板涨停 - 创新医疗 (002173)")
result = test_limit_check('002173', '创新医疗', 23.66, 26.03, 26.03, 1000000)
print(f"  昨收: 23.66")
print(f"  今收: 26.03")
print(f"  最高: 26.03")
print(f"  涨停比例: {result['limit_pct']}%")
print(f"  涨停价: {result['limit_price']}")
print(f"  实际涨幅: {result['actual_gain']:.2f}%")
print(f"  价格达标率: {result['price_ratio']:.2f}%")
print(f"  封板率: {result['seal_ratio']:.2f}%")
print(f"  是否涨停: {'✅ 是' if result['is_limit'] else '❌ 否'}")

# 案例2：北交所涨停 - 倍益康
print("\n【案例2】北交所涨停 - 倍益康 (920199)")
result = test_limit_check('920199', '倍益康', 44.96, 58.44, 58.44, 1000000)
print(f"  昨收: 44.96")
print(f"  今收: 58.44")
print(f"  最高: 58.44")
print(f"  涨停比例: {result['limit_pct']}%")
print(f"  涨停价: {result['limit_price']}")
print(f"  实际涨幅: {result['actual_gain']:.2f}%")
print(f"  价格达标率: {result['price_ratio']:.2f}%")
print(f"  封板率: {result['seal_ratio']:.2f}%")
print(f"  是否涨停: {'✅ 是' if result['is_limit'] else '❌ 否'}")

# 案例3：主板未涨停（涨幅9.5%）
print("\n【案例3】主板未涨停 - 某股票 (600000)")
result = test_limit_check('600000', '某股票', 10.00, 10.95, 11.00, 1000000)
print(f"  昨收: 10.00")
print(f"  今收: 10.95")
print(f"  最高: 11.00")
print(f"  涨停比例: {result['limit_pct']}%")
print(f"  涨停价: {result['limit_price']}")
print(f"  实际涨幅: {result['actual_gain']:.2f}%")
print(f"  价格达标率: {result['price_ratio']:.2f}%")
print(f"  封板率: {result['seal_ratio']:.2f}%")
print(f"  是否涨停: {'✅ 是' if result['is_limit'] else '❌ 否'}")

# 案例4：主板涨停但未封板（炸板）
print("\n【案例4】主板涨停但未封板 - 某股票 (600001)")
result = test_limit_check('600001', '某股票', 10.00, 10.95, 11.00, 1000000)
print(f"  昨收: 10.00")
print(f"  今收: 10.95")
print(f"  最高: 11.00")
print(f"  涨停比例: {result['limit_pct']}%")
print(f"  涨停价: {result['limit_price']}")
print(f"  实际涨幅: {result['actual_gain']:.2f}%")
print(f"  价格达标率: {result['price_ratio']:.2f}%")
print(f"  封板率: {result['seal_ratio']:.2f}%")
print(f"  是否涨停: {'✅ 是' if result['is_limit'] else '❌ 否'}")
print(f"  说明: 虽然收盘价接近涨停价，但最高价超过收盘价，说明炸板了")

# 案例5：创业板涨停
print("\n【案例5】创业板涨停 - 特锐德 (300001)")
result = test_limit_check('300001', '特锐德', 20.00, 24.00, 24.00, 1000000)
print(f"  昨收: 20.00")
print(f"  今收: 24.00")
print(f"  最高: 24.00")
print(f"  涨停比例: {result['limit_pct']}%")
print(f"  涨停价: {result['limit_price']}")
print(f"  实际涨幅: {result['actual_gain']:.2f}%")
print(f"  价格达标率: {result['price_ratio']:.2f}%")
print(f"  封板率: {result['seal_ratio']:.2f}%")
print(f"  是否涨停: {'✅ 是' if result['is_limit'] else '❌ 否'}")

# 案例6：科创板涨停
print("\n【案例6】科创板涨停 - 某科创股 (688001)")
result = test_limit_check('688001', '某科创股', 50.00, 60.00, 60.00, 1000000)
print(f"  昨收: 50.00")
print(f"  今收: 60.00")
print(f"  最高: 60.00")
print(f"  涨停比例: {result['limit_pct']}%")
print(f"  涨停价: {result['limit_price']}")
print(f"  实际涨幅: {result['actual_gain']:.2f}%")
print(f"  价格达标率: {result['price_ratio']:.2f}%")
print(f"  封板率: {result['seal_ratio']:.2f}%")
print(f"  是否涨停: {'✅ 是' if result['is_limit'] else '❌ 否'}")

# 案例7：主板低价股涨停（9.96%）
print("\n【案例7】主板低价股涨停 - 平安银行 (000001)")
result = test_limit_check('000001', '平安银行', 2.51, 2.76, 2.76, 1000000)
print(f"  昨收: 2.51")
print(f"  今收: 2.76")
print(f"  最高: 2.76")
print(f"  涨停比例: {result['limit_pct']}%")
print(f"  涨停价: {result['limit_price']}")
print(f"  实际涨幅: {result['actual_gain']:.2f}%")
print(f"  价格达标率: {result['price_ratio']:.2f}%")
print(f"  封板率: {result['seal_ratio']:.2f}%")
print(f"  是否涨停: {'✅ 是' if result['is_limit'] else '❌ 否'}")

# 案例8：主板ST涨停
print("\n【案例8】主板ST涨停 - ST某某 (600002)")
result = test_limit_check('600002', 'ST某某', 5.02, 5.27, 5.27, 1000000)
print(f"  昨收: 5.02")
print(f"  今收: 5.27")
print(f"  最高: 5.27")
print(f"  涨停比例: {result['limit_pct']}%")
print(f"  涨停价: {result['limit_price']}")
print(f"  实际涨幅: {result['actual_gain']:.2f}%")
print(f"  价格达标率: {result['price_ratio']:.2f}%")
print(f"  封板率: {result['seal_ratio']:.2f}%")
print(f"  是否涨停: {'✅ 是' if result['is_limit'] else '❌ 否'}")

# 案例9：边界测试 - 刚好达到99.8%
print("\n【案例9】边界测试 - 刚好达到99.8%")
limit_price = 11.00
close_price = limit_price * 0.998  # 10.978
result = test_limit_check('600003', '某股票', 10.00, close_price, close_price, 1000000)
print(f"  昨收: 10.00")
print(f"  今收: {close_price:.3f}")
print(f"  最高: {close_price:.3f}")
print(f"  涨停比例: {result['limit_pct']}%")
print(f"  涨停价: {result['limit_price']}")
print(f"  实际涨幅: {result['actual_gain']:.2f}%")
print(f"  价格达标率: {result['price_ratio']:.2f}%")
print(f"  封板率: {result['seal_ratio']:.2f}%")
print(f"  是否涨停: {'✅ 是' if result['is_limit'] else '❌ 否'}")

# 案例10：边界测试 - 略低于99.8%
print("\n【案例10】边界测试 - 略低于99.8%")
limit_price = 11.00
close_price = limit_price * 0.997  # 10.967
result = test_limit_check('600004', '某股票', 10.00, close_price, close_price, 1000000)
print(f"  昨收: 10.00")
print(f"  今收: {close_price:.3f}")
print(f"  最高: {close_price:.3f}")
print(f"  涨停比例: {result['limit_pct']}%")
print(f"  涨停价: {result['limit_price']}")
print(f"  实际涨幅: {result['actual_gain']:.2f}%")
print(f"  价格达标率: {result['price_ratio']:.2f}%")
print(f"  封板率: {result['seal_ratio']:.2f}%")
print(f"  是否涨停: {'✅ 是' if result['is_limit'] else '❌ 否'}")

print("\n" + "=" * 80)
print("测试完成")
print("=" * 80)
print("\n说明：")
print("1. 涨停判断基于精确的涨停价计算，而不是简单的涨幅判断")
print("2. 收盘价需达到涨停价的99.8%以上")
print("3. 收盘价需达到最高价的99.8%以上（封板判断）")
print("4. 必须有成交量")
