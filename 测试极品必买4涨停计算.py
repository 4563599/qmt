# -*- coding: utf-8 -*-
"""
测试极品必买4.py的涨停价计算
"""
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN


def get_limit_ratio(stock_code, stock_name):
    """根据股票代码和名称获取涨跌幅限制比例（返回小数形式）"""
    # 1. 北交所 (30%) - 8开头, 4开头, 9开头
    if stock_code.startswith('8') or stock_code.startswith('4') or stock_code.startswith('9'):
        return 0.30
    
    # 2. 科创板 (20%) - 688开头
    if stock_code.startswith('688'):
        return 0.20
    
    # 3. 创业板注册制 (20%) - 30开头
    if stock_code.startswith('30'):
        return 0.20
    
    # 4. 主板 (通常10%，ST为5%)
    is_st = 'ST' in stock_name.upper()
    if is_st:
        return 0.05
    else:
        return 0.10


def calculate_limit_price(prev_close, ratio, stock_code):
    """精准计算涨停价（兼容北交所严格限价规则）"""
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
        
        # 检查是否超过限制比例
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


print("=" * 80)
print("极品必买4.py 涨停价计算测试")
print("=" * 80)

# 测试案例
test_cases = [
    ("600000", "浦发银行", 10.03, "主板"),
    ("002173", "创新医疗", 23.66, "主板"),
    ("920199", "倍益康", 44.96, "北交所"),
    ("600001", "ST某某", 5.02, "主板ST"),
    ("300001", "特锐德", 20.00, "创业板"),
    ("000001", "平安银行", 2.51, "主板低价股"),
    ("688001", "某科创股", 50.00, "科创板"),
]

for stock_code, stock_name, prev_close, board_type in test_cases:
    ratio = get_limit_ratio(stock_code, stock_name)
    limit_price = calculate_limit_price(prev_close, ratio, stock_code)
    actual_gain = (limit_price - prev_close) / prev_close * 100
    
    print(f"\n【{board_type}】{stock_name} ({stock_code})")
    print(f"  昨收: {prev_close}")
    print(f"  涨停比例: {ratio*100}%")
    print(f"  涨停价: {limit_price}")
    print(f"  实际涨幅: {actual_gain:.2f}%")

print("\n" + "=" * 80)
print("测试完成")
print("=" * 80)
