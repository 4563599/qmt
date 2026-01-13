# -*- coding: utf-8 -*-
"""
前端API示例 - 用于前端轮询检测Redis数据更新
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
        return r
    except Exception as e:
        return None


def get_timestamp(date_str=None):
    """
    API: 获取指定日期的时间戳
    
    参数:
        date_str: 日期字符串，格式 YYYY-MM-DD，默认为今天
    
    返回:
        {
            "success": True/False,
            "timestamp": 时间戳（毫秒）,
            "date": 日期,
            "message": 消息
        }
    """
    r = connect_redis()
    if not r:
        return {"success": False, "message": "Redis连接失败"}
    
    if not date_str:
        date_str = datetime.now().strftime('%Y-%m-%d')
    
    try:
        timestamp_key = f"stocks_yxdxs:{date_str}:timestamp"
        timestamp = r.get(timestamp_key)
        
        if timestamp:
            return {
                "success": True,
                "timestamp": int(timestamp),
                "date": date_str,
                "message": "获取成功"
            }
        else:
            return {
                "success": False,
                "timestamp": None,
                "date": date_str,
                "message": "时间戳不存在"
            }
    except Exception as e:
        return {"success": False, "message": f"获取失败: {e}"}


def get_stocks_data(date_str=None):
    """
    API: 获取指定日期的股票数据
    
    参数:
        date_str: 日期字符串，格式 YYYY-MM-DD，默认为今天
    
    返回:
        {
            "success": True/False,
            "data": [股票列表],
            "count": 股票数量,
            "timestamp": 时间戳,
            "date": 日期,
            "message": 消息
        }
    """
    r = connect_redis()
    if not r:
        return {"success": False, "message": "Redis连接失败"}
    
    if not date_str:
        date_str = datetime.now().strftime('%Y-%m-%d')
    
    try:
        key = f"stocks_yxdxs:{date_str}"
        timestamp_key = f"{key}:timestamp"
        
        # 获取时间戳
        timestamp = r.get(timestamp_key)
        
        # 获取股票数据
        hash_data = r.hgetall(key)
        
        if not hash_data:
            return {
                "success": False,
                "data": [],
                "count": 0,
                "timestamp": int(timestamp) if timestamp else None,
                "date": date_str,
                "message": "暂无数据"
            }
        
        # 解析股票数据
        stocks = []
        for code, value in hash_data.items():
            try:
                # Base64解码
                decoded_json = base64.b64decode(value.encode('utf-8')).decode('utf-8')
                stock_data = json.loads(decoded_json)
                stocks.append(stock_data)
            except:
                # 如果不是Base64，直接解析
                try:
                    stock_data = json.loads(value)
                    stocks.append(stock_data)
                except:
                    pass
        
        # 按涨跌幅排序
        stocks.sort(key=lambda x: x.get('today_change', 0), reverse=True)
        
        return {
            "success": True,
            "data": stocks,
            "count": len(stocks),
            "timestamp": int(timestamp) if timestamp else None,
            "date": date_str,
            "message": "获取成功"
        }
    
    except Exception as e:
        return {"success": False, "message": f"获取失败: {e}"}


def check_update(date_str=None, last_timestamp=None):
    """
    API: 检查数据是否有更新
    
    参数:
        date_str: 日期字符串，格式 YYYY-MM-DD，默认为今天
        last_timestamp: 上次的时间戳
    
    返回:
        {
            "success": True/False,
            "has_update": True/False,
            "timestamp": 当前时间戳,
            "last_timestamp": 上次时间戳,
            "message": 消息
        }
    """
    result = get_timestamp(date_str)
    
    if not result["success"]:
        return result
    
    current_timestamp = result["timestamp"]
    has_update = False
    
    if last_timestamp is None:
        has_update = True
        message = "首次获取"
    elif current_timestamp != last_timestamp:
        has_update = True
        message = "检测到更新"
    else:
        has_update = False
        message = "无更新"
    
    return {
        "success": True,
        "has_update": has_update,
        "timestamp": current_timestamp,
        "last_timestamp": last_timestamp,
        "message": message
    }


# ==================== 使用示例 ====================

if __name__ == "__main__":
    print("=" * 80)
    print("前端API使用示例")
    print("=" * 80)
    
    # 示例1：获取时间戳
    print("\n【示例1：获取时间戳】")
    result = get_timestamp()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # 示例2：检查是否有更新
    print("\n【示例2：检查是否有更新】")
    last_ts = None  # 模拟首次访问
    result = check_update(last_timestamp=last_ts)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # 示例3：获取股票数据
    print("\n【示例3：获取股票数据】")
    result = get_stocks_data()
    if result["success"]:
        print(f"成功获取 {result['count']} 只股票")
        print(f"时间戳: {result['timestamp']}")
        print(f"前3只股票:")
        for i, stock in enumerate(result['data'][:3], 1):
            print(f"  {i}. {stock['code']} {stock['name']} - {stock['today_change']}%")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    
    print("\n" + "=" * 80)
    print("前端轮询伪代码:")
    print("=" * 80)
    print("""
// JavaScript 前端轮询示例
let lastTimestamp = null;

// 每3秒检查一次
setInterval(async () => {
    const response = await fetch('/api/check_update', {
        method: 'POST',
        body: JSON.stringify({ last_timestamp: lastTimestamp })
    });
    
    const result = await response.json();
    
    if (result.has_update) {
        console.log('检测到数据更新，重新加载...');
        lastTimestamp = result.timestamp;
        
        // 重新加载数据
        loadStocksData();
    }
}, 3000);

async function loadStocksData() {
    const response = await fetch('/api/get_stocks_data');
    const result = await response.json();
    
    if (result.success) {
        // 更新页面显示
        updateUI(result.data);
    }
}
    """)
    print("=" * 80 + "\n")
