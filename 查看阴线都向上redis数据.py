# -*- coding: utf-8 -*-
"""
查看Redis中阴线都向上策略的数据（支持Base64解码）
"""
import redis
import json
import base64

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
        print("\n" + "=" * 80)
        print("查找阴线都向上策略的所有键")
        print("=" * 80)
        
        keys = []
        cursor = 0
        while True:
            cursor, batch = r.scan(cursor, match="stocks_yxdxs:*", count=100)
            keys.extend(batch)
            if cursor == 0:
                break
        
        if keys:
            print(f"\n找到 {len(keys)} 个键:")
            for i, key in enumerate(sorted(keys), 1):
                count = r.hlen(key)
                ttl = r.ttl(key)
                ttl_str = f"{ttl//86400}天{ttl%86400//3600}小时" if ttl > 0 else ("永久" if ttl == -1 else "已过期")
                print(f"  [{i}] {key}")
                print(f"      股票数: {count} 只")
                print(f"      过期时间: {ttl_str}")
            return keys
        else:
            print("\n❌ 没有找到任何阴线都向上策略的数据")
            return []
    
    except Exception as e:
        print(f"❌ 查询失败: {e}")
        return []


def show_key_detail(r, key):
    """显示键的详细数据（支持Base64解码）"""
    try:
        print("\n" + "=" * 80)
        print(f"键详情: {key}")
        print("=" * 80)
        
        if not r.exists(key):
            print("❌ 键不存在")
            return
        
        # 获取所有字段
        hash_data = r.hgetall(key)
        
        if not hash_data:
            print("❌ 键为空")
            return
        
        print(f"\n共 {len(hash_data)} 只股票\n")
        
        stocks = []
        for code, value in hash_data.items():
            try:
                # 尝试Base64解码
                try:
                    decoded_json = base64.b64decode(value.encode('utf-8')).decode('utf-8')
                    stock_data = json.loads(decoded_json)
                except:
                    # 如果不是Base64编码，直接解析JSON
                    stock_data = json.loads(value)
                
                stocks.append(stock_data)
            except Exception as e:
                print(f"⚠️  解析失败: {code} - {e}")
        
        # 按涨跌幅排序
        stocks.sort(key=lambda x: x.get('today_change', 0), reverse=True)
        
        # 打印股票列表
        for i, stock in enumerate(stocks, 1):
            print(f"[{i}] {stock['code']} {stock['name']}")
            print(f"    策略: {', '.join(stock.get('strategies', []))}")
            print(f"    涨跌幅: {stock.get('today_change', 0):.2f}%")
            print(f"    拉升次数: {stock.get('ls_num', 0)}")
            print(f"    不下杀次数: {stock.get('no_xs_num', 0)}")
            print(f"    市值: {stock.get('market_cap', 0):.2f}亿")
            print(f"    预警时间: {stock.get('warning_time', 'N/A')}")
            print()
    
    except Exception as e:
        print(f"❌ 显示详情失败: {e}")


def main():
    """主函数"""
    print("=" * 80)
    print("Redis阴线都向上策略数据查看工具")
    print("=" * 80)
    
    # 连接Redis
    r = connect_redis()
    if not r:
        return
    
    # 列出所有键
    keys = list_yxdxs_keys(r)
    
    if not keys:
        return
    
    # 显示每个键的详细数据
    for key in sorted(keys):
        show_key_detail(r, key)
    
    print("\n" + "=" * 80)
    print("查询完成")
    print("=" * 80)


if __name__ == "__main__":
    main()
