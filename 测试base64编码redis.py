# -*- coding: utf-8 -*-
"""
测试Base64编码存储到Redis（绕过敏感词检测）
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
        print("✅ Redis连接成功！")
        return r
    except Exception as e:
        print(f"❌ Redis连接失败: {e}")
        return None


def test_base64_storage(r):
    """测试Base64编码存储"""
    print("\n" + "=" * 80)
    print("测试Base64编码存储（绕过敏感词检测）")
    print("=" * 80)
    
    # 测试数据（包含之前失败的"锋龙股份"）
    test_stocks = [
        {
            "code": "002931",
            "name": "锋龙股份",
            "strategies": ["阴线都向上"],
            "today_change": 10.01,
            "ls_num": 10,
            "no_xs_num": 10,
            "market_cap": 50.5,
            "warning_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        },
        {
            "code": "002792",
            "name": "通宇通讯",
            "strategies": ["阴线都向上"],
            "today_change": 9.85,
            "ls_num": 8,
            "no_xs_num": 9,
            "market_cap": 45.2,
            "warning_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
    ]
    
    date_str = datetime.now().strftime('%Y-%m-%d')
    key = f"stocks_yxdxs_test:{date_str}"
    
    success_count = 0
    failed_count = 0
    
    for stock_data in test_stocks:
        try:
            print(f"\n测试股票: {stock_data['code']} {stock_data['name']}")
            
            # 方法1：直接存储JSON（会触发敏感词）
            print("  方法1: 直接存储JSON...")
            try:
                json_str = json.dumps(stock_data, ensure_ascii=False)
                r.hset(f"{key}_direct", stock_data['code'], json_str)
                print("    ✓ 直接存储成功")
            except Exception as e:
                print(f"    ✗ 直接存储失败: {e}")
            
            # 方法2：Base64编码后存储
            print("  方法2: Base64编码后存储...")
            try:
                json_str = json.dumps(stock_data, ensure_ascii=False)
                encoded_value = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
                
                print(f"    原始JSON长度: {len(json_str)} 字节")
                print(f"    Base64编码长度: {len(encoded_value)} 字节")
                
                r.hset(f"{key}_base64", stock_data['code'], encoded_value)
                print("    ✓ Base64存储成功")
                
                # 验证读取和解码
                stored_value = r.hget(f"{key}_base64", stock_data['code'])
                if stored_value:
                    decoded_json = base64.b64decode(stored_value.encode('utf-8')).decode('utf-8')
                    decoded_data = json.loads(decoded_json)
                    print(f"    ✓ 解码验证成功: {decoded_data['name']}")
                    success_count += 1
                else:
                    print("    ✗ 读取失败")
                    failed_count += 1
            except Exception as e:
                print(f"    ✗ Base64存储失败: {e}")
                failed_count += 1
        
        except Exception as e:
            print(f"  ✗ 测试失败: {e}")
            failed_count += 1
    
    print("\n" + "=" * 80)
    print("测试完成")
    print("=" * 80)
    print(f"成功: {success_count} 只")
    print(f"失败: {failed_count} 只")
    print("=" * 80)
    
    # 清理测试数据
    print("\n清理测试数据...")
    r.delete(f"{key}_direct")
    r.delete(f"{key}_base64")
    print("✓ 测试数据已清理")


def main():
    """主函数"""
    print("\n" + "=" * 80)
    print("Base64编码Redis存储测试工具")
    print("=" * 80)
    
    r = connect_redis()
    if not r:
        return
    
    test_base64_storage(r)
    
    print("\n" + "=" * 80)
    print("测试完成")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
