# -*- coding: utf-8 -*-
"""
查看阴线都是十字星策略的Redis数据
"""
import redis
import json
import base64
from datetime import datetime, timedelta


# Redis连接配置
REDIS_HOST = "175.24.178.113"
REDIS_PORT = 5268
REDIS_PASSWORD = "lyy654321@REDIS"
REDIS_DB = 0


def connect_redis():
    """连接Redis"""
    try:
        r = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            db=REDIS_DB,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5
        )
        r.ping()
        print("✅ Redis连接成功！\n")
        return r
    except Exception as e:
        print(f"❌ Redis连接失败: {e}")
        return None


def view_today_data(r):
    """查看今天的数据"""
    today = datetime.now().strftime('%Y-%m-%d')
    
    print("=" * 80)
    print(f"查看阴线都是十字星策略数据 - {today}")
    print("=" * 80)
    
    key = f"stocks_yxdszx:{today}"
    timestamp_key = f"{key}:timestamp"
    
    # 检查键是否存在
    if not r.exists(key):
        print(f"\n⚠️  今天还没有数据: {key}")
        print("\n提示：程序需要在交易时间运行并发现符合条件的股票后才会有数据")
        return
    
    # 获取时间戳
    timestamp = r.get(timestamp_key)
    if timestamp:
        ts_time = datetime.fromtimestamp(int(timestamp) / 1000)
        print(f"\n⏰ 最后更新时间: {ts_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 获取所有股票数据
    stocks_data = r.hgetall(key)
    
    if not stocks_data:
        print(f"\n⚠️  键存在但没有数据: {key}")
        return
    
    print(f"\n📊 共找到 {len(stocks_data)} 只股票\n")
    
    # 按分组统计
    group1_stocks = []  # 分组1
    group2_stocks = []  # 分组2
    
    for code, value in stocks_data.items():
        try:
            # Base64解码
            decoded_json = base64.b64decode(value.encode('utf-8')).decode('utf-8')
            stock_data = json.loads(decoded_json)
            
            group = stock_data.get('group', '未知')
            if group == "分组1":
                group1_stocks.append((code, stock_data))
            elif group == "分组2":
                group2_stocks.append((code, stock_data))
            else:
                print(f"⚠️  未知分组: {code} - {stock_data}")
        except Exception as e:
            print(f"❌ 解码失败 [{code}]: {e}")
    
    # 显示分组1
    if group1_stocks:
        print("=" * 80)
        print(f"📌 分组1: 十字并不碰/全阳 ({len(group1_stocks)} 只)")
        print("=" * 80)
        
        # 按涨幅排序
        group1_stocks.sort(key=lambda x: x[1].get('today_change', 0), reverse=True)
        
        for idx, (code, data) in enumerate(group1_stocks, 1):
            strategy = data.get('strategies', ['未知'])[0]
            name = data.get('name', code)
            change = data.get('today_change', 0)
            ls_num = data.get('ls_num', 0)
            no_xs_num = data.get('no_xs_num', 0)
            market_cap = data.get('market_cap', 0)
            warning_time = data.get('warning_time', 'N/A')
            
            print(f"\n[{idx}] {code} {name}")
            print(f"    策略: {strategy}")
            print(f"    涨跌幅: {change:.2f}%")
            print(f"    拉升次数: {ls_num} | 不下杀次数: {no_xs_num}")
            print(f"    市值: {market_cap}亿")
            print(f"    预警时间: {warning_time}")
    
    # 显示分组2
    if group2_stocks:
        print("\n" + "=" * 80)
        print(f"📌 分组2: 阴线都是十字星 ({len(group2_stocks)} 只)")
        print("=" * 80)
        
        # 按涨幅排序
        group2_stocks.sort(key=lambda x: x[1].get('today_change', 0), reverse=True)
        
        for idx, (code, data) in enumerate(group2_stocks, 1):
            strategy = data.get('strategies', ['未知'])[0]
            name = data.get('name', code)
            change = data.get('today_change', 0)
            ls_num = data.get('ls_num', 0)
            no_xs_num = data.get('no_xs_num', 0)
            market_cap = data.get('market_cap', 0)
            warning_time = data.get('warning_time', 'N/A')
            
            print(f"\n[{idx}] {code} {name}")
            print(f"    策略: {strategy}")
            print(f"    涨跌幅: {change:.2f}%")
            print(f"    拉升次数: {ls_num} | 不下杀次数: {no_xs_num}")
            print(f"    市值: {market_cap}亿")
            print(f"    预警时间: {warning_time}")
    
    # 统计汇总
    print("\n" + "=" * 80)
    print("📈 统计汇总")
    print("=" * 80)
    print(f"分组1（十字并不碰/全阳）: {len(group1_stocks)} 只")
    print(f"分组2（阴线都是十字星）: {len(group2_stocks)} 只")
    print(f"总计: {len(stocks_data)} 只")
    print("=" * 80)


def view_all_keys(r):
    """查看所有阴线都是十字星相关的键"""
    print("\n" + "=" * 80)
    print("查看所有阴线都是十字星策略的键")
    print("=" * 80)
    
    pattern = "stocks_yxdszx:*"
    keys = r.keys(pattern)
    
    if not keys:
        print(f"\n⚠️  没有找到任何键（模式: {pattern}）")
        return
    
    print(f"\n找到 {len(keys)} 个键:\n")
    
    data_keys = [k for k in keys if not k.endswith(':timestamp')]
    timestamp_keys = [k for k in keys if k.endswith(':timestamp')]
    
    # 显示数据键
    if data_keys:
        print("📊 数据键:")
        for key in sorted(data_keys):
            stock_count = r.hlen(key)
            ttl = r.ttl(key)
            ttl_days = ttl // (24 * 60 * 60) if ttl > 0 else 0
            print(f"  - {key}: {stock_count} 只股票, 过期时间: {ttl_days}天")
    
    # 显示时间戳键
    if timestamp_keys:
        print("\n⏰ 时间戳键:")
        for key in sorted(timestamp_keys):
            timestamp = r.get(key)
            if timestamp:
                ts_time = datetime.fromtimestamp(int(timestamp) / 1000)
                print(f"  - {key}: {ts_time.strftime('%Y-%m-%d %H:%M:%S')}")
            else:
                print(f"  - {key}: (空)")
    
    print("")


def view_specific_date(r, date_str):
    """查看指定日期的数据"""
    print("=" * 80)
    print(f"查看指定日期的数据 - {date_str}")
    print("=" * 80)
    
    key = f"stocks_yxdszx:{date_str}"
    
    if not r.exists(key):
        print(f"\n⚠️  该日期没有数据: {key}")
        return
    
    stocks_data = r.hgetall(key)
    print(f"\n📊 共 {len(stocks_data)} 只股票\n")
    
    for idx, (code, value) in enumerate(stocks_data.items(), 1):
        try:
            decoded_json = base64.b64decode(value.encode('utf-8')).decode('utf-8')
            stock_data = json.loads(decoded_json)
            
            name = stock_data.get('name', code)
            strategy = stock_data.get('strategies', ['未知'])[0]
            group = stock_data.get('group', '未知')
            change = stock_data.get('today_change', 0)
            
            print(f"[{idx}] {code} {name} - {group} - {strategy} - {change:.2f}%")
        except Exception as e:
            print(f"[{idx}] {code} - 解码失败: {e}")


def main():
    """主函数"""
    print("\n" + "=" * 80)
    print("阴线都是十字星策略 - Redis数据查看工具")
    print("=" * 80)
    
    r = connect_redis()
    if not r:
        return
    
    while True:
        print("\n请选择操作:")
        print("1. 查看今天的数据")
        print("2. 查看所有键")
        print("3. 查看指定日期的数据")
        print("4. 退出")
        
        choice = input("\n请输入选项 (1-4): ").strip()
        
        if choice == "1":
            view_today_data(r)
        elif choice == "2":
            view_all_keys(r)
        elif choice == "3":
            date_str = input("请输入日期 (格式: YYYY-MM-DD): ").strip()
            view_specific_date(r, date_str)
        elif choice == "4":
            print("\n再见！")
            break
        else:
            print("\n❌ 无效选项，请重新选择")


if __name__ == "__main__":
    main()
