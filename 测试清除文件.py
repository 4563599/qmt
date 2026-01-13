# -*- coding: utf-8 -*-
"""
测试清除今天的文件功能
"""
import os
from datetime import datetime

OUTPUT_DIR = r"C:\Users\lyy\Desktop\股票\预警"
LOG_FILE = r"C:\Users\lyy\Desktop\股票\预警\阴线都向上.log"


def clear_today_files():
    """清除今天的输出文件和日志文件"""
    try:
        # 获取今天的日期
        today = datetime.now().strftime('%Y%m%d')  # 20260113
        
        # 文件1：YYYYMMDD阴线都向上.txt
        output_filename = f"{today}阴线都向上.txt"
        output_file = os.path.join(OUTPUT_DIR, output_filename)
        
        # 文件2：阴线都向上.log
        log_file = LOG_FILE
        
        files_to_clear = [output_file, log_file]
        
        print("=" * 80)
        print("开始清除今天的文件")
        print("=" * 80)
        print(f"今天日期: {today}")
        print("")
        
        for file_path in files_to_clear:
            print(f"处理文件: {file_path}")
            
            if os.path.exists(file_path):
                # 显示文件大小
                file_size = os.path.getsize(file_path)
                print(f"  当前大小: {file_size} 字节")
                
                try:
                    # 清空文件内容（保留文件）
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write('')
                    
                    # 验证清空
                    new_size = os.path.getsize(file_path)
                    print(f"  清空后大小: {new_size} 字节")
                    print(f"  ✓ 已清空文件")
                except Exception as e:
                    print(f"  ✗ 清空文件失败: {e}")
            else:
                print(f"  ○ 文件不存在，跳过")
            
            print("")
        
        print("=" * 80)
        print("清除完成")
        print("=" * 80)
    
    except Exception as e:
        print(f"清除文件时发生错误: {e}")


def create_test_files():
    """创建测试文件"""
    try:
        # 确保目录存在
        if not os.path.exists(OUTPUT_DIR):
            os.makedirs(OUTPUT_DIR)
        
        today = datetime.now().strftime('%Y%m%d')
        output_filename = f"{today}阴线都向上.txt"
        output_file = os.path.join(OUTPUT_DIR, output_filename)
        
        print("=" * 80)
        print("创建测试文件")
        print("=" * 80)
        
        # 创建输出文件
        with open(output_file, 'w', encoding='gbk') as f:
            f.write("测试数据1\n")
            f.write("测试数据2\n")
            f.write("测试数据3\n")
        print(f"✓ 已创建: {output_file}")
        
        # 创建日志文件
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write("[2026-01-13 12:00:00] [INFO] 测试日志1\n")
            f.write("[2026-01-13 12:00:01] [INFO] 测试日志2\n")
            f.write("[2026-01-13 12:00:02] [INFO] 测试日志3\n")
        print(f"✓ 已创建: {LOG_FILE}")
        
        print("=" * 80)
        print("")
    
    except Exception as e:
        print(f"创建测试文件失败: {e}")


def main():
    """主函数"""
    print("\n" + "=" * 80)
    print("测试清除今天的文件功能")
    print("=" * 80 + "\n")
    
    # 步骤1：创建测试文件
    create_test_files()
    
    # 步骤2：清除文件
    clear_today_files()
    
    print("\n测试完成！\n")


if __name__ == "__main__":
    main()
