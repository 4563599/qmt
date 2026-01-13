# -*- coding: utf-8 -*-
"""
自动查看阴线都是十字星策略的Redis数据（今天）
"""
import redis
import json
import base64
from datetime import datetime


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


def main():
    """主函数"""
    today = datetime.now().strftime('%Y-%m-%d')
    
    print("=" * 80)
    print(f"查看阴线都是十字星策略数据 - {today}")
    print("=" * 80)
    
    r = connect_redis()
    if not r:
        return
    
    key = f"stocks_yxdszx:{today}"
    timestamp_key = f"{key}:timestamp"
    
    # 检查键是否存在
    if not r.exists(key):
        print(f"\n⚠️  今天还没有数据: {key}")
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
    group1_stocks = []
    group2_stocks = []
    unknown_stocks = []
    
    for code, value in stocks_data.items():
        try:
            decoded_json = base64.b64decode(value.encode('utf-8')).decode('utf-8')
            stock_data = json.loads(decoded_json)
            
            group = stock_data.get('group', '未知')
            if group == "分组1":
                group1_stocks.append((code, stock_data))
            elif group == "分组2":
                group2_stocks.append((code, stock_data))
            else:
                unknown_stocks.append((code, stock_data))
        except Exception as e:
            print(f"❌ 解码失败 [{code}]: {e}")
    
    # 显示分组1
    if group1_stocks:
        print("=" * 80)
        print(f"📌 分组1: 十字并不碰/全阳 ({len(group1_stocks)} 只)")
        print("=" * 80)
        
        group1_stocks.sort(key=lambda x: x[1].get('today_change', 0), reverse=True)
        
        for idx, (code, data) in enumerate(group1_stocks, 1):
            strategy = data.get('strategies', ['未知'])[0]
            name = data.get('name', code)
            change = data.get('today_change', 0)
            ls_num = data.get('ls_num', 0)
            no_xs_num = data.get('no_xs_num', 0)
            
            print(f"[{idx}] {code} {name} - {strategy} - {change:.2f}% | 拉升:{ls_num} 不下杀:{no_xs_num}")
    
    # 显示分组2
    if group2_stocks:
        print("\n" + "=" * 80)
        print(f"📌 分组2: 阴线都是十字星 ({len(group2_stocks)} 只)")
        print("=" * 80)
        
        group2_stocks.sort(key=lambda x: x[1].get('today_change', 0), reverse=True)
        
        for idx, (code, data) in enumerate(group2_stocks, 1):
            strategy = data.get('strategies', ['未知'])[0]
            name = data.get('name', code)
            change = data.get('today_change', 0)
            ls_num = data.get('ls_num', 0)
            no_xs_num = data.get('no_xs_num', 0)
            
            print(f"[{idx}] {code} {name} - {strategy} - {change:.2f}% | 拉升:{ls_num} 不下杀:{no_xs_num}")
    
    # 显示未知分组
    if unknown_stocks:
        print("\n" + "=" * 80)
        print(f"⚠️  未知分组 ({len(unknown_stocks)} 只)")
        print("=" * 80)
        
        for idx, (code, data) in enumerate(unknown_stocks, 1):
            strategy = data.get('strategies', ['未知'])[0]
            name = data.get('name', code)
            group = data.get('group', '未知')
            print(f"[{idx}] {code} {name} - group:{group} - strategy:{strategy}")
    
    # 统计汇总
    print("\n" + "=" * 80)
    print("📈 统计汇总")
    print("=" * 80)
    print(f"分组1（十字并不碰/全阳）: {len(group1_stocks)} 只")
    print(f"分组2（阴线都是十字星）: {len(group2_stocks)} 只")
    if unknown_stocks:
        print(f"未知分组: {len(unknown_stocks)} 只")
    print(f"总计: {len(stocks_data)} 只")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
