# -*- coding: utf-8 -*-
"""
自动清理阴线都向上策略的Redis数据（无需手动确认）
只删除 stocks_yxdxs:* 键，不影响其他策略的数据
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


def list_yxdxs_keys(r):
    """列出所有阴线都向上的键"""
    try:
        keys = []
        cursor = 0
        while True:
            cursor, batch = r.scan(cursor, match="stocks_yxdxs:*", count=100)
            keys.extend(batch)
            if cursor == 0:
                break
        
        return sorted(keys)
    except Exception as e:
        print(f"❌ 查询键失败: {e}")
        return []


def delete_yxdxs_keys(r, keys):
    """删除阴线都向上的键"""
    if not keys:
        print("\n没有找到需要删除的键")
        return 0, 0
    
    print("\n" + "=" * 80)
    print("开始删除阴线都向上策略的Redis数据")
    print("=" * 80)
    
    deleted_count = 0
    failed_count = 0
    
    for i, key in enumerate(keys, 1):
        try:
            # 获取键的信息
            key_type = r.type(key)
            stock_count = r.hlen(key) if key_type == 'hash' else 0
            
            print(f"\n[{i}/{len(keys)}] 删除键: {key}")
            print(f"  类型: {key_type}")
            print(f"  股票数: {stock_count}")
            
            # 删除键
            result = r.delete(key)
            
            if result > 0:
                print(f"  ✓ 删除成功")
                deleted_count += 1
            else:
                print(f"  ✗ 删除失败（键可能不存在）")
                failed_count += 1
        
        except Exception as e:
            print(f"  ✗ 删除失败: {e}")
            failed_count += 1
    
    return deleted_count, failed_count


def verify_other_strategies(r):
    """验证其他策略的数据是否完好"""
    print("\n" + "=" * 80)
    print("验证其他策略的数据")
    print("=" * 80)
    
    try:
        # 查找其他策略的键
        other_patterns = {
            "极品必买1": "stocks_jpbm1:*",
            "极品必买2": "stocks_jpbm2:*",
            "极品必买3": "stocks_jpbm3:*",
            "极品必买4": "stocks_jpbm4:*",
            "5拉升": "stocks_5lasheng:*",
        }
        
        total_other_keys = 0
        
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
                total_other_keys += len(keys)
                
                # 显示前3个键
                for key in sorted(keys)[:3]:
                    stock_count = r.hlen(key)
                    print(f"  - {key}: {stock_count}只股票")
                
                if len(keys) > 3:
                    print(f"  ... 还有 {len(keys) - 3} 个键")
        
        print("\n" + "=" * 80)
        if total_other_keys > 0:
            print(f"✓ 其他策略数据完好，共 {total_other_keys} 个键")
        else:
            print("○ 没有找到其他策略的数据")
        print("=" * 80)
        
        return total_other_keys
    
    except Exception as e:
        print(f"✗ 验证失败: {e}")
        return 0


def main():
    """主函数"""
    print("\n" + "=" * 80)
    print("自动清理阴线都向上策略的Redis数据")
    print("=" * 80)
    print("⚠️  此操作将自动删除所有 stocks_yxdxs:* 键")
    print("⚠️  其他策略的数据不会受到影响")
    print("=" * 80)
    
    # 连接Redis
    r = connect_redis()
    if not r:
        return
    
    # 列出所有阴线都向上的键
    print("\n" + "=" * 80)
    print("查找阴线都向上策略的键")
    print("=" * 80)
    
    keys = list_yxdxs_keys(r)
    
    if not keys:
        print("\n✓ 没有找到阴线都向上策略的数据，无需清理")
        
        # 验证其他策略的数据
        verify_other_strategies(r)
        return
    
    print(f"\n找到 {len(keys)} 个键:")
    total_stocks = 0
    for i, key in enumerate(keys, 1):
        try:
            stock_count = r.hlen(key)
            total_stocks += stock_count
            ttl = r.ttl(key)
            ttl_str = f"{ttl//86400}天{ttl%86400//3600}小时" if ttl > 0 else ("永久" if ttl == -1 else "已过期")
            print(f"  [{i}] {key}")
            print(f"      股票数: {stock_count} 只")
            print(f"      过期时间: {ttl_str}")
        except:
            print(f"  [{i}] {key}")
    
    print(f"\n总计: {len(keys)} 个键, {total_stocks} 只股票")
    
    # 自动执行删除
    deleted_count, failed_count = delete_yxdxs_keys(r, keys)
    
    print("\n" + "=" * 80)
    print("删除完成")
    print("=" * 80)
    print(f"成功删除: {deleted_count} 个键")
    print(f"删除失败: {failed_count} 个键")
    print("=" * 80)
    
    # 验证删除结果
    print("\n" + "=" * 80)
    print("验证删除结果")
    print("=" * 80)
    
    remaining_keys = list_yxdxs_keys(r)
    if remaining_keys:
        print(f"⚠️  仍有 {len(remaining_keys)} 个键未删除:")
        for key in remaining_keys:
            print(f"  - {key}")
    else:
        print("✓ 所有阴线都向上策略的键已成功删除")
    
    # 验证其他策略的数据
    other_count = verify_other_strategies(r)
    
    print("\n" + "=" * 80)
    print("清理完成")
    print("=" * 80)
    print(f"已删除阴线都向上策略: {deleted_count} 个键")
    print(f"其他策略数据完好: {other_count} 个键")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
