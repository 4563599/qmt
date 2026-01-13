# -*- coding: utf-8 -*-
"""
清理阴线都是十字星策略的Redis数据
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
        print("✅ Redis连接成功！\n")
        return r
    except Exception as e:
        print(f"❌ Redis连接失败: {e}")
        return None


def main():
    """主函数"""
    print("=" * 80)
    print("清理阴线都是十字星策略的Redis数据")
    print("=" * 80)
    print("⚠️  此操作将自动删除所有 stocks_yxdszx:* 键")
    print("⚠️  其他策略的数据不会受到影响")
    print("=" * 80)
    
    r = connect_redis()
    if not r:
        return
    
    # 查找所有阴线都是十字星的键
    pattern = "stocks_yxdszx:*"
    keys = r.keys(pattern)
    
    if not keys:
        print("\n✓ 没有找到阴线都是十字星策略的数据，无需清理\n")
        return
    
    print(f"\n找到 {len(keys)} 个键:\n")
    
    # 显示详细信息
    for key in sorted(keys):
        key_type = r.type(key)
        if key_type == 'hash':
            stock_count = r.hlen(key)
            ttl = r.ttl(key)
            ttl_days = ttl // (24 * 60 * 60) if ttl > 0 else 0
            print(f"  [{len(keys)}] {key}")
            print(f"      股票数: {stock_count} 只")
            print(f"      过期时间: {ttl_days}天")
        elif key_type == 'string':
            print(f"  [{len(keys)}] {key}")
    
    # 统计
    data_keys = [k for k in keys if not k.endswith(':timestamp')]
    total_stocks = sum(r.hlen(k) for k in data_keys)
    
    print(f"\n总计: {len(keys)} 个键, {total_stocks} 只股票\n")
    
    print("=" * 80)
    print("开始删除阴线都是十字星策略的Redis数据")
    print("=" * 80)
    
    success_count = 0
    fail_count = 0
    
    for idx, key in enumerate(sorted(keys), 1):
        try:
            key_type = r.type(key)
            stock_count = r.hlen(key) if key_type == 'hash' else 0
            
            print(f"\n[{idx}/{len(keys)}] 删除键: {key}")
            print(f"  类型: {key_type}")
            print(f"  股票数: {stock_count}")
            
            r.delete(key)
            print(f"  ✓ 删除成功")
            success_count += 1
        except Exception as e:
            print(f"  ✗ 删除失败: {e}")
            fail_count += 1
    
    print("\n" + "=" * 80)
    print("删除完成")
    print("=" * 80)
    print(f"成功删除: {success_count} 个键")
    print(f"删除失败: {fail_count} 个键")
    print("=" * 80)
    
    # 验证删除结果
    print("\n" + "=" * 80)
    print("验证删除结果")
    print("=" * 80)
    
    remaining_keys = r.keys(pattern)
    if remaining_keys:
        print(f"⚠️  还有 {len(remaining_keys)} 个键未删除:")
        for key in remaining_keys:
            print(f"  - {key}")
    else:
        print("✓ 所有阴线都是十字星策略的键已成功删除")
    
    # 验证其他策略的数据
    print("\n" + "=" * 80)
    print("验证其他策略的数据")
    print("=" * 80)
    
    other_patterns = [
        "stocks_jpbm1:*",
        "stocks_jpbm2:*",
        "stocks_jpbm3:*",
        "stocks_jpbm4:*",
        "stocks_5lasheng:*",
        "stocks_yxdxs:*"
    ]
    
    for pattern in other_patterns:
        keys = r.keys(pattern)
        if keys:
            strategy_name = pattern.split(':')[0].replace('stocks_', '')
            data_keys = [k for k in keys if not k.endswith(':timestamp')]
            print(f"\n{strategy_name}: {len(keys)} 个键")
            for key in sorted(data_keys)[:3]:
                stock_count = r.hlen(key)
                print(f"  - {key}: {stock_count}只股票")
            if len(data_keys) > 3:
                print(f"  ... 还有 {len(data_keys) - 3} 个键")
    
    print("\n" + "=" * 80)
    print("✓ 其他策略数据完好")
    print("=" * 80)
    
    print("\n" + "=" * 80)
    print("清理完成")
    print("=" * 80)
    print(f"已删除阴线都是十字星策略: {success_count} 个键")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
