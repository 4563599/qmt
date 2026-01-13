# -*- coding: utf-8 -*-
"""
测试Redis日志功能
"""
import redis
import json
from datetime import datetime
import os

# Redis连接配置
REDIS_HOST = "175.24.178.113"
REDIS_PORT = 5268
REDIS_PASSWORD = "lyy654321@REDIS"
REDIS_DB = 0
LOG_FILE = r"C:\Users\lyy\Desktop\股票\预警\阴线都向上.log"


def log_redis(msg, level="INFO"):
    """Redis专用日志，同时打印到控制台和文件"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_msg = f"[{timestamp}] [{level}] {msg}"
    
    # 打印到控制台
    print(log_msg)
    
    # 写入日志文件
    try:
        # 确保日志目录存在
        log_dir = os.path.dirname(LOG_FILE)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_msg + '\n')
    except Exception as e:
        print(f"[ERROR] 写入日志文件失败: {e}")


def test_redis_connection():
    """测试Redis连接"""
    log_redis("=" * 60)
    log_redis("开始测试Redis连接")
    log_redis("=" * 60)
    
    if redis is None:
        log_redis("❌ redis模块未安装，无法连接Redis", "ERROR")
        log_redis("请运行: pip install redis", "ERROR")
        return None
    
    log_redis(f"Redis模块已安装，版本: {redis.__version__ if hasattr(redis, '__version__') else '未知'}")
    log_redis(f"连接信息: {REDIS_HOST}:{REDIS_PORT}")
    
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
        
        log_redis("正在测试连接...")
        r.ping()
        
        # 获取Redis服务器信息
        info = r.info()
        log_redis("✅ Redis连接成功！")
        log_redis(f"Redis版本: {info.get('redis_version', 'N/A')}")
        log_redis(f"已用内存: {info.get('used_memory_human', 'N/A')}")
        log_redis(f"数据库: DB{REDIS_DB}")
        log_redis("=" * 60)
        
        return r
    except redis.ConnectionError as e:
        log_redis(f"❌ Redis连接失败（网络错误）: {e}", "ERROR")
        return None
    except redis.AuthenticationError as e:
        log_redis(f"❌ Redis认证失败（密码错误）: {e}", "ERROR")
        return None
    except Exception as e:
        log_redis(f"❌ Redis连接异常: {e}", "ERROR")
        return None


def test_redis_storage(r):
    """测试Redis存储"""
    if not r:
        log_redis("Redis客户端未初始化，跳过存储测试", "ERROR")
        return
    
    log_redis("\n" + "=" * 60)
    log_redis("开始测试Redis存储")
    log_redis("=" * 60)
    
    date_str = datetime.now().strftime('%Y-%m-%d')
    
    # 测试数据
    stock_data = {
        "code": "TEST001",
        "name": "测试股票",
        "strategies": ["阴线都向上"],
        "today_change": 5.5,
        "ls_num": 6,
        "no_xs_num": 8,
        "market_cap": 100.5,
        "warning_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    try:
        key = f"stocks_yxdxs:{date_str}"
        field = stock_data['code']
        value = json.dumps(stock_data, ensure_ascii=False)
        
        log_redis(f"准备存储到Redis: {key}")
        log_redis(f"  股票: {stock_data['code']} {stock_data['name']}")
        log_redis(f"  涨跌幅: {stock_data['today_change']:.2f}%")
        log_redis(f"  拉升次数: {stock_data['ls_num']}")
        log_redis(f"  不下杀次数: {stock_data['no_xs_num']}")
        log_redis(f"  市值: {stock_data['market_cap']}亿")
        
        # 设置hash字段
        result = r.hset(key, field, value)
        log_redis(f"  HSET结果: {'新增' if result == 1 else '更新'}")
        
        # 设置过期时间（30天）
        ttl = 30 * 24 * 60 * 60
        r.expire(key, ttl)
        log_redis(f"  过期时间: 30天")
        
        # 验证存储
        stored_value = r.hget(key, field)
        if stored_value:
            log_redis(f"✅ Redis存储成功: {stock_data['code']} {stock_data['name']}")
            
            # 读取并验证数据
            stored_data = json.loads(stored_value)
            log_redis(f"  验证数据: {stored_data}")
        else:
            log_redis(f"⚠️  Redis存储验证失败: {stock_data['code']} {stock_data['name']}", "WARN")
        
        log_redis("=" * 60)
        
    except redis.RedisError as e:
        log_redis(f"❌ Redis存储失败（Redis错误）: {e}", "ERROR")
    except Exception as e:
        log_redis(f"❌ Redis存储失败（未知错误）: {e}", "ERROR")


def main():
    """主函数"""
    print("\n" + "=" * 80)
    print("Redis日志测试工具")
    print("=" * 80 + "\n")
    
    # 测试连接
    r = test_redis_connection()
    
    # 测试存储
    if r:
        test_redis_storage(r)
    
    print("\n" + "=" * 80)
    print(f"测试完成，日志已保存到: {LOG_FILE}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
