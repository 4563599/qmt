# -*- coding: utf-8 -*-
"""
测试Redis时间戳更新功能
"""
import redis
import time

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


def test_timestamp_update(r):
    """测试时间戳更新"""
    print("\n" + "=" * 80)
    print("测试时间戳更新功能")
    print("=" * 80)
    
    date_str = "2026-01-13"
    key = f"stocks_yxdxs:{date_str}"
    timestamp_key = f"{key}:timestamp"
    
    # 1. 获取当前时间戳
    print(f"\n1. 获取当前时间戳:")
    current_timestamp = r.get(timestamp_key)
    if current_timestamp:
        print(f"✓ 当前时间戳: {timestamp_key} = {current_timestamp}")
    else:
        print(f"⚠️  时间戳不存在: {timestamp_key}")
    
    # 2. 更新时间戳
    print(f"\n2. 更新时间戳:")
    new_timestamp = int(time.time() * 1000)  # 毫秒时间戳
    r.set(timestamp_key, new_timestamp)
    r.expire(timestamp_key, 30 * 24 * 60 * 60)  # 30天过期
    print(f"✅ 更新时间戳成功: {timestamp_key} = {new_timestamp}")
    
    # 3. 验证更新
    print(f"\n3. 验证更新:")
    stored_timestamp = r.get(timestamp_key)
    if stored_timestamp:
        print(f"✅ 获取时间戳: {timestamp_key} = {stored_timestamp}")
        if str(new_timestamp) == stored_timestamp:
            print("✅ 时间戳更新成功!")
        else:
            print("⚠️  时间戳不匹配")
    else:
        print("❌ 获取时间戳失败")
    
    print("\n" + "=" * 80)


def simulate_frontend_polling(r):
    """模拟前端轮询检测数据更新"""
    print("\n" + "=" * 80)
    print("模拟前端轮询检测数据更新（每3秒检查一次）")
    print("=" * 80)
    print("按 Ctrl+C 停止\n")
    
    date_str = "2026-01-13"
    key = f"stocks_yxdxs:{date_str}"
    timestamp_key = f"{key}:timestamp"
    
    last_timestamp = None
    check_count = 0
    
    try:
        while True:
            check_count += 1
            current_time = time.strftime('%H:%M:%S')
            
            # 获取当前时间戳
            current_timestamp = r.get(timestamp_key)
            
            if current_timestamp:
                if last_timestamp is None:
                    # 首次获取
                    print(f"[{current_time}] 第{check_count}次检查 - 初始时间戳: {current_timestamp}")
                    last_timestamp = current_timestamp
                elif current_timestamp != last_timestamp:
                    # 检测到变化
                    print(f"[{current_time}] 第{check_count}次检查 - 🔔 检测到数据更新!")
                    print(f"  旧时间戳: {last_timestamp}")
                    print(f"  新时间戳: {current_timestamp}")
                    print(f"  ✅ 触发重新加载数据...")
                    last_timestamp = current_timestamp
                else:
                    # 无变化
                    print(f"[{current_time}] 第{check_count}次检查 - 无变化")
            else:
                print(f"[{current_time}] 第{check_count}次检查 - ⚠️  时间戳不存在")
            
            # 等待3秒
            time.sleep(3)
    
    except KeyboardInterrupt:
        print("\n\n停止轮询")


def main():
    """主函数"""
    print("\n" + "=" * 80)
    print("Redis时间戳更新测试工具")
    print("=" * 80)
    
    r = connect_redis()
    if not r:
        return
    
    # 测试时间戳更新
    test_timestamp_update(r)
    
    # 询问是否模拟前端轮询
    print("\n" + "=" * 80)
    choice = input("是否模拟前端轮询检测？(yes/no): ").strip().lower()
    
    if choice == "yes":
        simulate_frontend_polling(r)
    else:
        print("\n测试完成")
    
    print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    main()
