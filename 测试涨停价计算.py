# -*- coding: utf-8 -*-
"""
测试涨停价计算逻辑
"""
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN


def get_limit_ratio(stock_code, stock_name):
    """根据股票代码和名称获取涨跌幅限制比例"""
    # 1. 北交所 (30%) - 8开头, 4开头, 9开头
    if stock_code.startswith('8') or stock_code.startswith('4') or stock_code.startswith('9'):
        return 0.30
    
    # 2. 科创板 (20%) - 688开头
    if stock_code.startswith('688'):
        return 0.20
    
    # 3. 创业板 (20%) - 30开头
    if stock_code.startswith('30'):
        return 0.20
    
    # 4. 主板 (通常10%，ST为5%)
    is_st = 'ST' in stock_name.upper()
    if is_st:
        return 0.05
    else:
        return 0.10


def calculate_limit_price(prev_close, ratio, stock_code):
    """
    精准计算涨停价（兼容北交所严格限价规则）
    
    规则差异：
    - 主板：允许四舍五入后微幅超标（如10.016%）
    - 北交所/科创板/创业板：严格限价（Hard Cap），不得超过限制比例
    """
    from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
    
    # 使用 Decimal 保证浮点数运算精度
    p = Decimal(str(prev_close))
    r = Decimal(str(ratio))
    
    # 计算目标价
    target = p * (1 + r)
    
    # 判断是否为北交所/科创板/创业板（需要严格限价）
    is_strict_limit = (stock_code.startswith('8') or 
                      stock_code.startswith('4') or 
                      stock_code.startswith('9') or 
                      stock_code.startswith('688') or 
                      stock_code.startswith('30'))
    
    if is_strict_limit:
        # 严格限价：先四舍五入，然后检查是否超标
        limit_price_round = target.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        
        # 检查是否超过限制比例（不使用容差，严格判断）
        actual_ratio = (limit_price_round - p) / p
        
        if actual_ratio > r:
            # 超标了，向下取整
            limit_price = target.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        else:
            limit_price = limit_price_round
    else:
        # 主板：标准四舍五入（允许微幅超标）
        limit_price = target.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    
    return float(limit_price)


def is_limit_up(stock_code, stock_name, prev_close, current_price):
    """判断是否涨停"""
    # 1. 获取该股票的涨停比例
    ratio = get_limit_ratio(stock_code, stock_name)
    
    # 2. 计算精准的涨停价
    limit_price = calculate_limit_price(prev_close, ratio, stock_code)
    
    # 3. 计算实际涨幅
    actual_gain = (current_price - prev_close) / prev_close * 100
    
    # 4. 比较现价和涨停价
    is_limit = abs(current_price - limit_price) < 0.01
    
    return is_limit, limit_price, ratio, actual_gain


# ================= 测试案例 =================
print("=" * 80)
print("涨停价计算测试")
print("=" * 80)

# 案例1：普通主板，10%
# 假设昨收 10.03，10.03 * 1.1 = 11.033 -> 11.03
# 涨幅实际为 (11.03-10.03)/10.03 = 9.97%
print("\n【案例1】主板 - 浦发银行 (600000)")
is_limit, limit_price, ratio, actual_gain = is_limit_up('600000', '浦发银行', 10.03, 11.03)
print(f"  昨收: 10.03")
print(f"  涨停比例: {ratio*100}%")
print(f"  计算涨停价: {limit_price}")
print(f"  当前价: 11.03")
print(f"  实际涨幅: {actual_gain:.2f}%")
print(f"  是否涨停: {is_limit}")

# 案例2：主板 - 创新医疗 (002173)
# 昨收 23.66，23.66 * 1.1 = 26.026 -> 26.03
# 实际涨幅 (26.03-23.66)/23.66 = 10.016%（主板允许）
print("\n【案例2】主板 - 创新医疗 (002173)")
is_limit, limit_price, ratio, actual_gain = is_limit_up('002173', '创新医疗', 23.66, 26.03)
print(f"  昨收: 23.66")
print(f"  涨停比例: {ratio*100}%")
print(f"  计算涨停价: {limit_price}")
print(f"  当前价: 26.03")
print(f"  实际涨幅: {actual_gain:.2f}%")
print(f"  是否涨停: {is_limit}")

# 案例3：北交所 - 倍益康 (920199)
# 昨收 44.96，44.96 * 1.3 = 58.448
# 四舍五入 -> 58.45，但 (58.45-44.96)/44.96 = 30.004% > 30%
# 所以应该向下取整 -> 58.44，(58.44-44.96)/44.96 = 29.982%
print("\n【案例3】北交所 - 倍益康 (920199)")
is_limit, limit_price, ratio, actual_gain = is_limit_up('920199', '倍益康', 44.96, 58.44)
print(f"  昨收: 44.96")
print(f"  涨停比例: {ratio*100}%")
print(f"  计算涨停价: {limit_price}")
print(f"  当前价: 58.44")
print(f"  实际涨幅: {actual_gain:.2f}%")
print(f"  是否涨停: {is_limit}")
print(f"  验证: 如果取58.45，涨幅={(58.45-44.96)/44.96*100:.3f}% (超过30%)")

# 案例4：ST股票，5%
# 假设昨收 5.02，5.02 * 1.05 = 5.271 -> 5.27
# 涨幅实际为 (5.27-5.02)/5.02 = 4.98%
print("\n【案例4】主板ST - ST某某 (600001)")
is_limit, limit_price, ratio, actual_gain = is_limit_up('600001', 'ST某某', 5.02, 5.27)
print(f"  昨收: 5.02")
print(f"  涨停比例: {ratio*100}%")
print(f"  计算涨停价: {limit_price}")
print(f"  当前价: 5.27")
print(f"  实际涨幅: {actual_gain:.2f}%")
print(f"  是否涨停: {is_limit}")

# 案例5：创业板，20%
print("\n【案例5】创业板 - 特锐德 (300001)")
is_limit, limit_price, ratio, actual_gain = is_limit_up('300001', '特锐德', 20.00, 24.00)
print(f"  昨收: 20.00")
print(f"  涨停比例: {ratio*100}%")
print(f"  计算涨停价: {limit_price}")
print(f"  当前价: 24.00")
print(f"  实际涨幅: {actual_gain:.2f}%")
print(f"  是否涨停: {is_limit}")

# 案例6：低价股的极端情况 (9.96% 场景)
# 假设昨收 2.51，涨停价应该是 2.51 * 1.1 = 2.761 -> 2.76
# 实际涨幅 (2.76 - 2.51) / 2.51 = 0.099601... = 9.96%
print("\n【案例6】主板低价股 - 平安银行 (000001)")
is_limit, limit_price, ratio, actual_gain = is_limit_up('000001', '平安银行', 2.51, 2.76)
print(f"  昨收: 2.51")
print(f"  涨停比例: {ratio*100}%")
print(f"  计算涨停价: {limit_price}")
print(f"  当前价: 2.76")
print(f"  实际涨幅: {actual_gain:.2f}%")
print(f"  是否涨停: {is_limit}")

# 案例7：科创板，20%
print("\n【案例7】科创板 - 某科创股 (688001)")
is_limit, limit_price, ratio, actual_gain = is_limit_up('688001', '某科创股', 50.00, 60.00)
print(f"  昨收: 50.00")
print(f"  涨停比例: {ratio*100}%")
print(f"  计算涨停价: {limit_price}")
print(f"  当前价: 60.00")
print(f"  实际涨幅: {actual_gain:.2f}%")
print(f"  是否涨停: {is_limit}")

print("\n" + "=" * 80)
print("测试完成")
print("=" * 80)
