# -*- coding: utf-8 -*-
"""
Redis数据查看工具
连接到远程Redis服务器并查看数据结构
"""
import redis
import json

# Redis连接配置
REDIS_HOST = "175.24.178.113"
REDIS_PORT = 5268
REDIS_PASSWORD = "lyy654321@REDIS"
REDIS_DB = 0  # 默认数据库

def connect_redis():
    """连接Redis"""
    try:
        r = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            db=REDIS_DB,
            decode_responses=True,  # 自动解码为字符串
            socket_timeout=5,
            socket_connect_timeout=5
        )
        # 测试连接
        r.ping()
        print("✅ Redis连接成功！")
        print(f"服务器: {REDIS_HOST}:{REDIS_PORT}")
        print(f"数据库: DB{REDIS_DB}")
        print("=" * 60)
        return r
    except redis.ConnectionError as e:
        print(f"❌ Redis连接失败: {e}")
        return None
    except redis.AuthenticationError as e:
        print(f"❌ Redis认证失败: {e}")
        return None
    except Exception as e:
        print(f"❌ 连接异常: {e}")
        return None


def get_redis_info(r):
    """获取Redis服务器信息"""
    try:
        info = r.info()
        print("\n【Redis服务器信息】")
        print(f"Redis版本: {info.get('redis_version', 'N/A')}")
        print(f"运行模式: {info.get('redis_mode', 'N/A')}")
        print(f"已用内存: {info.get('used_memory_human', 'N/A')}")
        print(f"连接客户端数: {info.get('connected_clients', 'N/A')}")
        print(f"总键数: {info.get('db0', {}).get('keys', 0) if 'db0' in info else 0}")
        print("=" * 60)
    except Exception as e:
        print(f"获取服务器信息失败: {e}")


def scan_all_keys(r, pattern="*", count=100):
    """扫描所有键"""
    try:
        print(f"\n【扫描所有键】(pattern={pattern})")
        cursor = 0
        all_keys = []
        
        while True:
            cursor, keys = r.scan(cursor, match=pattern, count=count)
            all_keys.extend(keys)
            if cursor == 0:
                break
        
        print(f"找到 {len(all_keys)} 个键")
        return all_keys
    except Exception as e:
        print(f"扫描键失败: {e}")
        return []


def analyze_key_types(r, keys):
    """分析键的类型分布"""
    try:
        print("\n【键类型分布】")
        type_count = {}
        
        for key in keys[:100]:  # 只分析前100个键
            key_type = r.type(key)
            type_count[key_type] = type_count.get(key_type, 0) + 1
        
        for key_type, count in sorted(type_count.items(), key=lambda x: x[1], reverse=True):
            print(f"  {key_type}: {count} 个")
        
        print("=" * 60)
        return type_count
    except Exception as e:
        print(f"分析键类型失败: {e}")
        return {}


def show_sample_data(r, keys, sample_count=10):
    """显示示例数据"""
    try:
        print(f"\n【示例数据】(显示前{sample_count}个键)")
        
        for i, key in enumerate(keys[:sample_count], 1):
            key_type = r.type(key)
            ttl = r.ttl(key)
            ttl_str = f"{ttl}秒" if ttl > 0 else ("永久" if ttl == -1 else "已过期")
            
            print(f"\n[{i}] 键名: {key}")
            print(f"    类型: {key_type}")
            print(f"    TTL: {ttl_str}")
            
            try:
                if key_type == "string":
                    value = r.get(key)
                    # 尝试解析JSON
                    try:
                        json_value = json.loads(value)
                        print(f"    值(JSON): {json.dumps(json_value, ensure_ascii=False, indent=2)[:200]}")
                    except:
                        print(f"    值: {value[:200]}")
                
                elif key_type == "hash":
                    hash_data = r.hgetall(key)
                    print(f"    字段数: {len(hash_data)}")
                    # 显示前3个字段
                    for j, (field, value) in enumerate(list(hash_data.items())[:3], 1):
                        print(f"      {field}: {value[:100]}")
                    if len(hash_data) > 3:
                        print(f"      ... 还有 {len(hash_data) - 3} 个字段")
                
                elif key_type == "list":
                    list_len = r.llen(key)
                    print(f"    长度: {list_len}")
                    # 显示前3个元素
                    items = r.lrange(key, 0, 2)
                    for j, item in enumerate(items, 1):
                        print(f"      [{j}]: {item[:100]}")
                    if list_len > 3:
                        print(f"      ... 还有 {list_len - 3} 个元素")
                
                elif key_type == "set":
                    set_size = r.scard(key)
                    print(f"    大小: {set_size}")
                    # 显示前3个成员
                    members = list(r.sscan_iter(key, count=3))[:3]
                    for j, member in enumerate(members, 1):
                        print(f"      {member[:100]}")
                    if set_size > 3:
                        print(f"      ... 还有 {set_size - 3} 个成员")
                
                elif key_type == "zset":
                    zset_size = r.zcard(key)
                    print(f"    大小: {zset_size}")
                    # 显示前3个成员（带分数）
                    items = r.zrange(key, 0, 2, withscores=True)
                    for j, (member, score) in enumerate(items, 1):
                        print(f"      {member[:100]} (分数: {score})")
                    if zset_size > 3:
                        print(f"      ... 还有 {zset_size - 3} 个成员")
                
            except Exception as e:
                print(f"    读取值失败: {e}")
        
        print("\n" + "=" * 60)
    except Exception as e:
        print(f"显示示例数据失败: {e}")


def search_keys_by_pattern(r, pattern):
    """按模式搜索键"""
    try:
        print(f"\n【搜索键】(pattern={pattern})")
        keys = []
        cursor = 0
        
        while True:
            cursor, batch = r.scan(cursor, match=pattern, count=100)
            keys.extend(batch)
            if cursor == 0:
                break
        
        print(f"找到 {len(keys)} 个匹配的键")
        
        if keys:
            print("\n匹配的键:")
            for i, key in enumerate(keys[:20], 1):
                print(f"  [{i}] {key}")
            if len(keys) > 20:
                print(f"  ... 还有 {len(keys) - 20} 个键")
        
        return keys
    except Exception as e:
        print(f"搜索键失败: {e}")
        return []


def get_key_detail(r, key):
    """获取键的详细信息"""
    try:
        print(f"\n【键详细信息】")
        print(f"键名: {key}")
        
        if not r.exists(key):
            print("❌ 键不存在")
            return
        
        key_type = r.type(key)
        ttl = r.ttl(key)
        memory = r.memory_usage(key) if hasattr(r, 'memory_usage') else None
        
        print(f"类型: {key_type}")
        print(f"TTL: {ttl}秒" if ttl > 0 else ("永久" if ttl == -1 else "已过期"))
        if memory:
            print(f"内存占用: {memory} 字节")
        
        print("\n完整数据:")
        
        if key_type == "string":
            value = r.get(key)
            try:
                json_value = json.loads(value)
                print(json.dumps(json_value, ensure_ascii=False, indent=2))
            except:
                print(value)
        
        elif key_type == "hash":
            hash_data = r.hgetall(key)
            print(json.dumps(hash_data, ensure_ascii=False, indent=2))
        
        elif key_type == "list":
            list_data = r.lrange(key, 0, -1)
            print(json.dumps(list_data, ensure_ascii=False, indent=2))
        
        elif key_type == "set":
            set_data = list(r.smembers(key))
            print(json.dumps(set_data, ensure_ascii=False, indent=2))
        
        elif key_type == "zset":
            zset_data = r.zrange(key, 0, -1, withscores=True)
            print(json.dumps([{"member": m, "score": s} for m, s in zset_data], ensure_ascii=False, indent=2))
        
        print("=" * 60)
    except Exception as e:
        print(f"获取键详细信息失败: {e}")


def main():
    """主函数"""
    print("=" * 60)
    print("Redis数据查看工具")
    print("=" * 60)
    
    # 连接Redis
    r = connect_redis()
    if not r:
        return
    
    # 获取服务器信息
    get_redis_info(r)
    
    # 扫描所有键
    all_keys = scan_all_keys(r)
    
    if not all_keys:
        print("\n数据库为空，没有找到任何键")
        return
    
    # 分析键类型
    analyze_key_types(r, all_keys)
    
    # 显示示例数据
    show_sample_data(r, all_keys, sample_count=10)
    
    # 交互式查询
    print("\n" + "=" * 60)
    print("交互式查询（输入命令）")
    print("=" * 60)
    print("命令:")
    print("  1. search <pattern>  - 搜索键（如: search user:*）")
    print("  2. get <key>         - 查看键详情（如: get user:123）")
    print("  3. keys              - 列出所有键")
    print("  4. info              - 显示服务器信息")
    print("  5. quit              - 退出")
    print("=" * 60)
    
    while True:
        try:
            cmd = input("\n请输入命令: ").strip()
            
            if not cmd:
                continue
            
            if cmd.lower() == "quit":
                print("再见！")
                break
            
            elif cmd.lower() == "keys":
                keys = scan_all_keys(r)
                for i, key in enumerate(keys[:50], 1):
                    print(f"  [{i}] {key}")
                if len(keys) > 50:
                    print(f"  ... 还有 {len(keys) - 50} 个键")
            
            elif cmd.lower() == "info":
                get_redis_info(r)
            
            elif cmd.lower().startswith("search "):
                pattern = cmd[7:].strip()
                if pattern:
                    search_keys_by_pattern(r, pattern)
                else:
                    print("请提供搜索模式，如: search user:*")
            
            elif cmd.lower().startswith("get "):
                key = cmd[4:].strip()
                if key:
                    get_key_detail(r, key)
                else:
                    print("请提供键名，如: get user:123")
            
            else:
                print("未知命令，请输入 quit 退出")
        
        except KeyboardInterrupt:
            print("\n\n再见！")
            break
        except Exception as e:
            print(f"执行命令失败: {e}")


if __name__ == "__main__":
    main()
