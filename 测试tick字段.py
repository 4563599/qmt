# -*- coding: utf-8 -*-
"""
测试QMT tick数据中的字段
用于查看tick中是否包含市值相关字段
"""

# 模拟一个tick数据结构（根据QMT实际返回的数据）
sample_tick = {
    'lastPrice': 10.50,
    'high': 11.00,
    'low': 10.20,
    'open': 10.30,
    'volume': 1000000,
    'amount': 10500000,
    'preClose': 10.00,
    'change': 0.50,
    'changeRate': 5.0,
    # 可能的市值字段
    'circulatingMarketCap': 5000000000,  # 流通市值（元）
    'totalMarketCap': 8000000000,  # 总市值（元）
    'circulatingShares': 500000000,  # 流通股本（股）
    'totalShares': 800000000,  # 总股本（股）
}


def get_market_cap(tick, stock):
    """从实时行情tick中获取市值（亿元）"""
    print(f"\n{'='*60}")
    print(f"测试股票: {stock}")
    print(f"{'='*60}")
    
    try:
        # 方法1：直接从tick中获取流通市值
        market_cap = tick.get('circulatingMarketCap', 0)
        print(f"方法1 - circulatingMarketCap: {market_cap}")
        if market_cap > 0:
            result = round(market_cap / 100000000, 2)
            print(f"  ✓ 成功获取流通市值: {result}亿元")
            return result
        
        # 方法2：从tick中获取总市值
        total_cap = tick.get('totalMarketCap', 0)
        print(f"方法2 - totalMarketCap: {total_cap}")
        if total_cap > 0:
            result = round(total_cap / 100000000, 2)
            print(f"  ✓ 成功获取总市值: {result}亿元")
            return result
        
        # 方法3：通过流通股本计算
        circulating_shares = tick.get('circulatingShares', 0)
        price = tick.get('lastPrice', 0)
        print(f"方法3 - circulatingShares: {circulating_shares}, lastPrice: {price}")
        if circulating_shares > 0 and price > 0:
            result = round(circulating_shares * price / 100000000, 2)
            print(f"  ✓ 成功通过股本计算市值: {result}亿元")
            return result
        
        # 方法4：尝试其他可能的字段名
        for field in ['marketCap', 'market_cap', 'totalValue', 'total_value']:
            value = tick.get(field, 0)
            if value > 0:
                result = round(value / 100000000, 2)
                print(f"方法4 - {field}: {value}")
                print(f"  ✓ 成功获取市值: {result}亿元")
                return result
        
        print("  ✗ 所有方法都未能获取市值")
        return 0.0
    except Exception as e:
        print(f"  ✗ 获取市值失败: {e}")
        return 0.0


def print_tick_fields(tick):
    """打印tick中的所有字段"""
    print("\n" + "="*60)
    print("Tick数据中的所有字段:")
    print("="*60)
    
    for key, value in tick.items():
        print(f"  {key}: {value} ({type(value).__name__})")
    
    print("="*60)


def main():
    """主函数"""
    print("\n" + "="*80)
    print("QMT Tick字段测试工具")
    print("="*80)
    
    # 打印所有字段
    print_tick_fields(sample_tick)
    
    # 测试获取市值
    market_cap = get_market_cap(sample_tick, "000001.SZ")
    
    print("\n" + "="*80)
    print(f"最终结果: 市值 = {market_cap}亿元")
    print("="*80)
    
    print("\n" + "="*80)
    print("说明:")
    print("="*80)
    print("1. 如果上面显示'成功获取市值'，说明tick中包含市值字段")
    print("2. 如果显示'所有方法都未能获取市值'，说明需要使用其他API")
    print("3. 在实际QMT环境中运行时，请检查实际tick数据的字段名")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
