"""
股票数据Redis存储模块
用于tdx.py调用，将股票数据存储到Redis
"""
import os
import json
import redis
from datetime import datetime

from redis_config import REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_DB

class StockRedisClient:
    def __init__(self):
        try:
            self.client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                db=REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=5
            )
            # 测试连接
            self.client.ping()
            print(f"✅ Redis连接成功: {REDIS_HOST}:{REDIS_PORT}")
        except Exception as e:
            print(f"❌ Redis连接失败: {REDIS_HOST}:{REDIS_PORT} - {e}")
            self.client = None
    
    def save_stock(self, stock_data):
        """
        保存股票数据到Redis
        """
        if self.client is None:
            print(f"❌ Redis未连接，无法保存股票 {stock_data.get('name', 'unknown')}")
            return False
            
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            key = f"stocks:{today}"
            code = stock_data['code']
            
            # 检查是否已存在该股票
            existing = self.client.hget(key, code)
            if existing:
                existing_data = json.loads(existing)
                # 合并策略（去重）
                existing_strategies = set(existing_data.get('strategies', []))
                new_strategies = set(stock_data.get('strategies', []))
                merged_strategies = list(existing_strategies | new_strategies)
                stock_data['strategies'] = merged_strategies
                # 保留首次预警时间
                stock_data['first_alert_time'] = existing_data.get('first_alert_time', existing_data.get('alert_time'))
            else:
                stock_data['first_alert_time'] = stock_data.get('alert_time')
            
            # 添加更新时间
            stock_data['update_time'] = datetime.now().strftime('%H:%M:%S')
            
            # 保存到Redis
            self.client.hset(key, code, json.dumps(stock_data, ensure_ascii=False))
            
            # 设置过期时间（30天）
            self.client.expire(key, 30 * 24 * 3600)
            
            # 发布实时更新消息
            self.client.publish('stock_updates', json.dumps({
                'type': 'new_stock',
                'data': stock_data,
                'date': today
            }, ensure_ascii=False))
            
            print(f"✅ 股票 {stock_data['name']}({code}) 已保存到Redis，策略: {stock_data['strategies']}")
            return True
        except Exception as e:
            print(f"❌ 保存股票到Redis失败: {stock_data.get('name', 'unknown')} - {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_today_stocks(self):
        """获取今日所有股票"""
        today = datetime.now().strftime('%Y-%m-%d')
        key = f"stocks:{today}"
        stocks_data = self.client.hgetall(key)
        stocks = []
        for code, data_str in stocks_data.items():
            stocks.append(json.loads(data_str))
        return stocks
    
    def get_stock(self, code, date=None):
        """获取指定股票数据"""
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        key = f"stocks:{date}"
        data = self.client.hget(key, code)
        if data:
            return json.loads(data)
        return None

# 全局实例
stock_redis = StockRedisClient()

def save_stock_to_redis(stock_data):
    """便捷函数：保存股票到Redis"""
    return stock_redis.save_stock(stock_data)
