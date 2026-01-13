# -*- coding: utf-8 -*-
"""
测试今日涨停判断中的MA5条件
"""
import numpy as np

# 模拟历史收盘价数据（最近5天）
closes_case1 = np.array([10.0, 10.2, 10.5, 10.3, 10.4])  # MA5 = 10.28
closes_case2 = np.array([12.0, 11.8, 11.5, 11.3, 11.0])  # MA5 = 11.52
closes_case3 = np.array([8.0, 8.5, 9.0, 9.5, 10.0])      # MA5 = 9.0

print("=" * 80)
print("今日涨停判断 - MA5条件测试")
print("=" * 80)

# 案例1：今日最低价高于MA5
print("\n【案例1】今日最低价高于MA5")
ma5_1 = np.mean(closes_case1)
today_low_1 = 10.50
print(f"  历史收盘价（最近5天）: {closes_case1}")
print(f"  昨日MA5: {ma5_1:.2f}")
print(f"  今日最低价: {today_low_1}")
print(f"  今日最低价 > MA5: {today_low_1 > ma5_1}")
print(f"  结论: {'✅ 满足条件' if today_low_1 > ma5_1 else '❌ 不满足条件'}")

# 案例2：今日最低价低于MA5
print("\n【案例2】今日最低价低于MA5")
ma5_2 = np.mean(closes_case2)
today_low_2 = 11.20
print(f"  历史收盘价（最近5天）: {closes_case2}")
print(f"  昨日MA5: {ma5_2:.2f}")
print(f"  今日最低价: {today_low_2}")
print(f"  今日最低价 > MA5: {today_low_2 > ma5_2}")
print(f"  结论: {'✅ 满足条件' if today_low_2 > ma5_2 else '❌ 不满足条件'}")

# 案例3：今日最低价刚好等于MA5
print("\n【案例3】今日最低价刚好等于MA5")
ma5_3 = np.mean(closes_case3)
today_low_3 = 9.00
print(f"  历史收盘价（最近5天）: {closes_case3}")
print(f"  昨日MA5: {ma5_3:.2f}")
print(f"  今日最低价: {today_low_3}")
print(f"  今日最低价 > MA5: {today_low_3 > ma5_3}")
print(f"  结论: {'✅ 满足条件' if today_low_3 > ma5_3 else '❌ 不满足条件（需要严格大于）'}")

# 案例4：今日最低价略高于MA5
print("\n【案例4】今日最低价略高于MA5")
ma5_4 = np.mean(closes_case3)
today_low_4 = 9.01
print(f"  历史收盘价（最近5天）: {closes_case3}")
print(f"  昨日MA5: {ma5_4:.2f}")
print(f"  今日最低价: {today_low_4}")
print(f"  今日最低价 > MA5: {today_low_4 > ma5_4}")
print(f"  结论: {'✅ 满足条件' if today_low_4 > ma5_4 else '❌ 不满足条件'}")

print("\n" + "=" * 80)
print("测试完成")
print("=" * 80)
print("\n说明：")
print("1. 今日涨停判断需要满足：今日最低价 > 昨日MA5")
print("2. MA5使用最近5天的收盘价计算（不包括今天）")
print("3. 必须严格大于，等于不满足条件")
print("4. 这个条件与通达信公式中的 BASE_COND:=L>MA(C,5) 对应")
