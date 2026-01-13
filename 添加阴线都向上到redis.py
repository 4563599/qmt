# -*- coding: utf-8 -*-
"""
添加阴线都向上策略数据到Redis（支持Base64编码）
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
        print("✅ Redis连接成功！")
        return r
    except Exception as e:
        print(f"❌ Redis连接失败: {e}")
        return None


def add_stock_to_redis(r, date_str, stock_data):
    """
    添加股票数据到Redis（使用Base64编码）
    
    参数:
        r: Redis连接对象
        date_str: 日期字符串，格式 YYYY-MM-DD
        stock_data: 股票数据字典
            {
                "code": "股票代码",
                "name": "股票名称",
                "strategies": ["策略名称"],
                "today_change": 涨跌幅,
                "ls_num": 拉升次数,
                "no_xs_num": 不下杀次数,
                "market_cap": 市值,
                "warning_time": 预警时间
            }
    """
    try:
        # 键名格式：stocks_yxdxs:YYYY-MM-DD
        key = f"stocks_yxdxs:{date_str}"
        
        # 字段名：股票代码
        field = stock_data['code']
        
        # 值：JSON字符串 -> Base64编码
        json_str = json.dumps(stock_data, ensure_ascii=False)
        value = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
        
        # 设置hash字段
        r.hset(key, field, value)
        
        # 设置过期时间（30天）
        ttl = 30 * 24 * 60 * 60  # 30天
        r.expire(key, ttl)
        
        print(f"✅ 添加成功: {stock_data['code']} {stock_data['name']} (Base64编码)")
        return True
    except Exception as e:
        print(f"❌ 添加失败: {e}")
        return False


def add_multiple_stocks(r, date_str, stocks_list):
    """
    批量添加股票数据
    
    参数:
        r: Redis连接对象
        date_str: 日期字符串，格式 YYYY-MM-DD
        stocks_list: 股票数据列表
    """
    try:
        key = f"stocks_yxdxs:{date_str}"
        
        # 使用pipeline批量操作
        pipe = r.pipeline()
        
        for stock_data in stocks_list:
            field = stock_data['code']
            value = json.dumps(stock_data, ensure_ascii=False)
            pipe.hset(key, field, value)
        
        # 设置过期时间（30天）
        ttl = 30 * 24 * 60 * 60
        pipe.expire(key, ttl)
        
        # 执行批量操作
        pipe.execute()
        
        print(f"✅ 批量添加成功: {len(stocks_list)} 只股票")
        return True
    except Exception as e:
        print(f"❌ 批量添加失败: {e}")
        return False


def get_stocks_by_date(r, date_str):
    """
    获取指定日期的股票数据（支持Base64解码）
    
    参数:
        r: Redis连接对象
        date_str: 日期字符串，格式 YYYY-MM-DD
    """
    try:
        key = f"stocks_yxdxs:{date_str}"
        
        if not r.exists(key):
            print(f"❌ 键不存在: {key}")
            return None
        
        # 获取所有字段
        hash_data = r.hgetall(key)
        
        print(f"\n【{date_str} 阴线都向上策略】")
        print(f"共 {len(hash_data)} 只股票")
        print("=" * 80)
        
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
            print(f"    涨跌幅: {stock.get('today_change', 0):.2f}%")
            print(f"    拉升次数: {stock.get('ls_num', 0)}")
            print(f"    不下杀次数: {stock.get('no_xs_num', 0)}")
            print(f"    市值: {stock.get('market_cap', 0):.2f}亿")
            print(f"    预警时间: {stock.get('warning_time', 'N/A')}")
        
        return stocks
    except Exception as e:
        print(f"❌ 获取数据失败: {e}")
        return None


def delete_stocks_by_date(r, date_str):
    """
    删除指定日期的股票数据
    
    参数:
        r: Redis连接对象
        date_str: 日期字符串，格式 YYYY-MM-DD
    """
    try:
        key = f"stocks_yxdxs:{date_str}"
        
        if not r.exists(key):
            print(f"❌ 键不存在: {key}")
            return False
        
        r.delete(key)
        print(f"✅ 删除成功: {key}")
        return True
    except Exception as e:
        print(f"❌ 删除失败: {e}")
        return False


def main():
    """主函数"""
    print("=" * 80)
    print("阴线都向上策略 - Redis数据管理")
    print("=" * 80)
    
    # 连接Redis
    r = connect_redis()
    if not r:
        return
    
    # 示例：添加单只股票
    print("\n【示例1：添加单只股票】")
    today = datetime.now().strftime('%Y-%m-%d')
    
    stock_data = {
        "code": "000001",
        "name": "平安银行",
        "strategies": ["阴线都向上"],
        "today_change": 5.23,
        "ls_num": 5,
        "no_xs_num": 5,
        "market_cap": 3500.50,
        "warning_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    add_stock_to_redis(r, today, stock_data)
    
    # 示例：批量添加股票
    print("\n【示例2：批量添加股票】")
    stocks_list = [
        {
            "code": "600000",
            "name": "浦发银行",
            "strategies": ["阴线都向上"],
            "today_change": 3.45,
            "ls_num": 6,
            "no_xs_num": 6,
            "market_cap": 4200.30,
            "warning_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        },
        {
            "code": "000002",
            "name": "万科A",
            "strategies": ["阴线都向上"],
            "today_change": 2.18,
            "ls_num": 4,
            "no_xs_num": 4,
            "market_cap": 2800.60,
            "warning_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        },
        {
            "code": "300001",
            "name": "特锐德",
            "strategies": ["阴线都向上"],
            "today_change": 7.89,
            "ls_num": 7,
            "no_xs_num": 7,
            "market_cap": 150.20,
            "warning_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
    ]
    
    add_multiple_stocks(r, today, stocks_list)
    
    # 查询数据
    print("\n【示例3：查询数据】")
    get_stocks_by_date(r, today)
    
    # 交互式操作
    print("\n" + "=" * 80)
    print("交互式操作")
    print("=" * 80)
    print("命令:")
    print("  1. add <date>        - 添加股票（如: add 2026-01-13）")
    print("  2. get <date>        - 查询股票（如: get 2026-01-13）")
    print("  3. delete <date>     - 删除数据（如: delete 2026-01-13）")
    print("  4. list              - 列出所有阴线都向上的键")
    print("  5. quit              - 退出")
    print("=" * 80)
    
    while True:
        try:
            cmd = input("\n请输入命令: ").strip()
            
            if not cmd:
                continue
            
            if cmd.lower() == "quit":
                print("再见！")
                break
            
            elif cmd.lower() == "list":
                # 列出所有阴线都向上的键
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
                        ttl_str = f"{ttl//86400}天" if ttl > 0 else "永久"
                        print(f"  [{i}] {key} - {count}只股票 - TTL:{ttl_str}")
                else:
                    print("没有找到任何键")
            
            elif cmd.lower().startswith("add "):
                date_str = cmd[4:].strip()
                if not date_str:
                    date_str = datetime.now().strftime('%Y-%m-%d')
                
                print(f"\n添加股票到 {date_str}")
                print("请输入股票信息（输入空行结束）:")
                
                stocks_to_add = []
                while True:
                    code = input("  股票代码: ").strip()
                    if not code:
                        break
                    
                    name = input("  股票名称: ").strip()
                    change = float(input("  涨跌幅(%): ").strip() or 0)
                    ls_num = int(input("  拉升次数: ").strip() or 0)
                    no_xs_num = int(input("  不下杀次数: ").strip() or 0)
                    market_cap = float(input("  市值(亿): ").strip() or 0)
                    
                    stock_data = {
                        "code": code,
                        "name": name,
                        "strategies": ["阴线都向上"],
                        "today_change": change,
                        "ls_num": ls_num,
                        "no_xs_num": no_xs_num,
                        "market_cap": market_cap,
                        "warning_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    }
                    
                    stocks_to_add.append(stock_data)
                    print(f"  ✅ 已添加: {code} {name}")
                
                if stocks_to_add:
                    add_multiple_stocks(r, date_str, stocks_to_add)
            
            elif cmd.lower().startswith("get "):
                date_str = cmd[4:].strip()
                if not date_str:
                    date_str = datetime.now().strftime('%Y-%m-%d')
                
                get_stocks_by_date(r, date_str)
            
            elif cmd.lower().startswith("delete "):
                date_str = cmd[7:].strip()
                if date_str:
                    confirm = input(f"确认删除 {date_str} 的数据？(yes/no): ").strip().lower()
                    if confirm == "yes":
                        delete_stocks_by_date(r, date_str)
                else:
                    print("请提供日期，如: delete 2026-01-13")
            
            else:
                print("未知命令")
        
        except KeyboardInterrupt:
            print("\n\n再见！")
            break
        except Exception as e:
            print(f"执行命令失败: {e}")


if __name__ == "__main__":
    main()
