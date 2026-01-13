# -*- coding: utf-8 -*-
"""
测试清理脚本（只查看，不删除）
"""
import redis

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
        print("✅ Redis连接成功！")
        return r
    except Exception as e:
        print(f"❌ Redis连接失败: {e}")
        return None


def main():
    """主函数"""
    print("\n" + "=" * 80)
    print("测试清理脚本（只查看，不删除）")
    print("=" * 80)
    
    r = connect_redis()
    if not r:
        return
    
    # 查找阴线都向上的键
    print("\n【阴线都向上策略】")
    print("=" * 80)
    yxdxs_keys = []
    cursor = 0
    while True:
        cursor, batch = r.scan(cursor, match="stocks_yxdxs:*", count=100)
        yxdxs_keys.extend(batch)
        if cursor == 0:
            break
    
    if yxdxs_keys:
        print(f"找到 {len(yxdxs_keys)} 个键:")
        for i, key in enumerate(sorted(yxdxs_keys), 1):
            stock_count = r.hlen(key)
            print(f"  [{i}] {key} - {stock_count}只股票")
    else:
        print("没有找到阴线都向上策略的数据")
    
    # 查找其他策略的键
    print("\n【其他策略】")
    print("=" * 80)
    
    other_patterns = {
        "极品必买1": "stocks_jpbm1:*",
        "极品必买2": "stocks_jpbm2:*",
        "极品必买3": "stocks_jpbm3:*",
        "极品必买4": "stocks_jpbm4:*",
        "5拉升": "stocks_5lasheng:*",
    }
    
    total_other = 0
    for name, pattern in other_patterns.items():
        keys = []
        cursor = 0
        while True:
            cursor, batch = r.scan(cursor, match=pattern, count=100)
            keys.extend(batch)
            if cursor == 0:
                break
        
        if keys:
            print(f"\n{name}: {len(keys)} 个键")
            total_other += len(keys)
            for key in sorted(keys)[:3]:
                stock_count = r.hlen(key)
                print(f"  - {key}: {stock_count}只股票")
            if len(keys) > 3:
                print(f"  ... 还有 {len(keys) - 3} 个键")
    
    if total_other == 0:
        print("没有找到其他策略的数据")
    
    print("\n" + "=" * 80)
    print("测试完成")
    print("=" * 80)
    print(f"阴线都向上: {len(yxdxs_keys)} 个键")
    print(f"其他策略: {total_other} 个键")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
