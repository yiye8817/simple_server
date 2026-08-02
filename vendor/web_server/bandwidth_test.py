#!/usr/bin/env python3
"""
FRP带宽测试脚本 - 最终版
从resources.db获取FRP服务器列表，使用frpc建立隧道测试下载带宽
记录成功和失败的结果
"""

import sqlite3
import subprocess
import time
import random
import os
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
import json
import tempfile
import shutil
import socket

class QuietHTTPHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.serve_dir = os.path.expanduser("~/")
        super().__init__(*args, directory=self.serve_dir, **kwargs)
    
    def log_message(self, format, *args):
        pass

class BandwidthTester:
    def __init__(self, db_path, file_path="50M", test_duration=5, auth_token=None, auth_user=None):
        self.db_path = db_path
        self.file_path = file_path
        self.test_duration = test_duration
        self.http_port = None
        self.results = []
        self.failures = []
        self.http_server = None
        self.auth_token = auth_token
        self.auth_user = auth_user
        
    def start_http_server(self):
        """启动本地HTTP服务器"""
        self.http_port = random.randint(10000, 60000)
        self.http_server = HTTPServer(('127.0.0.1', self.http_port), QuietHTTPHandler)
        thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        thread.start()
        print(f"HTTP服务器启动在端口 {self.http_port}")
        return self.http_port
    
    def get_servers(self, limit=10):
        """从数据库获取FRP服务器列表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT server, port 
            FROM public_ips 
            WHERE port != '0' AND port IS NOT NULL AND port != ''
            ORDER BY RANDOM()
            LIMIT ?
        """, (limit,))
        servers = cursor.fetchall()
        conn.close()
        return servers
    
    def test_server(self, server_ip, server_port, max_retries=2):
        """测试单个FRP服务器"""
        print(f"\n测试服务器: {server_ip}:{server_port}")
        
        for attempt in range(max_retries):
            remote_port = random.randint(20000, 60000)
            print(f"  尝试 {attempt + 1}: 映射端口 {remote_port} -> {self.http_port}")
            
            frpc_process = None
            try:
                # 构建frpc命令
                frpc_cmd = [
                    os.path.expanduser("~/bin/frpc"),
                    "tcp",
                    "-s", server_ip,
                    "-P", str(server_port),
                    "-l", str(self.http_port),
                    "-r", str(remote_port),
                    "-n", f"test_{remote_port}",
                    "--log-level", "warn"
                ]
                
                # 添加认证
                if self.auth_token:
                    frpc_cmd.extend(["-t", self.auth_token])
                if self.auth_user:
                    frpc_cmd.extend(["-u", self.auth_user])
                
                frpc_process = subprocess.Popen(
                    frpc_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                
                # 等待连接建立
                time.sleep(4)
                
                # 检查进程是否还在运行
                if frpc_process.poll() is not None:
                    stderr = frpc_process.stderr.read().decode()
                    error_msg = stderr.strip() if stderr else "frpc启动后立即退出"
                    print(f"  frpc失败: {error_msg[:100]}")
                    
                    # 记录失败
                    self.failures.append({
                        'server': server_ip,
                        'server_port': server_port,
                        'error': error_msg,
                        'attempt': attempt + 1
                    })
                    continue
                
                # frpc正在运行，测试下载
                test_url = f"http://{server_ip}:{remote_port}/50M"
                print(f"  测试URL: {test_url}")
                
                result = self.measure_download_speed(test_url)
                
                if result:
                    result['server'] = server_ip
                    result['server_port'] = server_port
                    result['remote_port'] = remote_port
                    self.results.append(result)
                    return result
                else:
                    # 下载失败
                    self.failures.append({
                        'server': server_ip,
                        'server_port': server_port,
                        'error': 'frpc连接成功但下载失败',
                        'remote_port': remote_port,
                        'attempt': attempt + 1
                    })
                    
            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                print(f"  测试出错: {error_msg}")
                self.failures.append({
                    'server': server_ip,
                    'server_port': server_port,
                    'error': error_msg,
                    'attempt': attempt + 1
                })
            finally:
                if frpc_process:
                    try:
                        frpc_process.terminate()
                        frpc_process.wait(timeout=3)
                    except:
                        try:
                            frpc_process.kill()
                        except:
                            pass
        
        print(f"  所有尝试都失败")
        return None
    
    def measure_download_speed(self, url, timeout=10):
        """测量下载速度"""
        try:
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout)
            
            start_download = time.time()
            response = urlopen(url, timeout=timeout)
            
            total_bytes = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if time.time() - start_download > self.test_duration:
                    break
            
            duration = time.time() - start_download
            socket.setdefaulttimeout(old_timeout)
            
            if duration > 0 and total_bytes > 0:
                speed_mbps = (total_bytes * 8) / duration / (1024 * 1024)
                return {
                    'total_bytes': total_bytes,
                    'duration': duration,
                    'speed_mbps': speed_mbps,
                    'url': url
                }
        except Exception as e:
            print(f"    下载失败: {type(e).__name__}: {e}")
        return None
    
    def run(self, max_servers=10):
        """运行测试"""
        print("=" * 60)
        print("FRP带宽测试工具")
        print("=" * 60)
        
        self.start_http_server()
        servers = self.get_servers(limit=max_servers)
        print(f"找到 {len(servers)} 个FRP服务器")
        
        try:
            for i, (server_ip, server_port) in enumerate(servers, 1):
                print(f"\n[{i}/{len(servers)}] 测试 {server_ip}:{server_port}")
                result = self.test_server(server_ip, int(server_port))
                if result:
                    print(f"  ✓ 成功: {result['speed_mbps']:.2f} Mbps ({result['total_bytes'] / 1024 / 1024:.1f} MB)")
                else:
                    print(f"  ✗ 失败")
        finally:
            self.save_results()
            self.print_summary()
            if self.http_server:
                self.http_server.shutdown()
    
    def save_results(self):
        """保存测试结果"""
        output_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bandwidth_results.json')
        data = {
            'successful': self.results,
            'failed': self.failures,
            'summary': {
                'total_tested': len(self.results) + len(self.failures),
                'successful_count': len(self.results),
                'failed_count': len(self.failures)
            }
        }
        with open(output_file, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"\n结果已保存到: {output_file}")
    
    def print_summary(self):
        """打印测试汇总"""
        print("\n" + "=" * 60)
        print("测试结果汇总")
        print("=" * 60)
        
        # 成功的结果
        if self.results:
            print(f"\n成功的测试 ({len(self.results)} 个):")
            print("-" * 60)
            sorted_results = sorted(self.results, key=lambda x: x['speed_mbps'], reverse=True)
            print(f"{'服务器':<20} {'端口':<10} {'速度 (Mbps)':<15} {'下载量 (MB)':<15}")
            print("-" * 60)
            for result in sorted_results:
                print(f"{result['server']:<20} {result['remote_port']:<10} "
                      f"{result['speed_mbps']:<15.2f} {result['total_bytes'] / 1024 / 1024:<15.1f}")
            
            speeds = [r['speed_mbps'] for r in self.results]
            print("-" * 60)
            print(f"平均速度: {sum(speeds) / len(speeds):.2f} Mbps")
            print(f"最高速度: {max(speeds):.2f} Mbps")
            print(f"最低速度: {min(speeds):.2f} Mbps")
        
        # 失败的结果
        if self.failures:
            print(f"\n失败的测试 ({len(self.failures)} 个):")
            print("-" * 60)
            # 按服务器分组统计失败原因
            error_counts = {}
            for f in self.failures:
                error = f['error'][:50]
                error_counts[error] = error_counts.get(error, 0) + 1
            
            print("失败原因统计:")
            for error, count in sorted(error_counts.items(), key=lambda x: -x[1]):
                print(f"  {error}: {count} 次")
        
        # 总体统计
        print("\n" + "=" * 60)
        print(f"总计测试: {len(self.results) + len(self.failures)} 个服务器")
        print(f"成功: {len(self.results)} 个")
        print(f"失败: {len(self.failures)} 个")
        if self.results:
            print(f"成功率: {len(self.results) / (len(self.results) + len(self.failures)) * 100:.1f}%")
        print("=" * 60)

def main():
    import argparse
    parser = argparse.ArgumentParser(description='FRP带宽测试工具')
    parser.add_argument('--db', default='resources.db', help='数据库文件路径')
    parser.add_argument('--file', default='50M', help='测试文件名')
    parser.add_argument('--max-servers', type=int, default=10, help='最大测试服务器数')
    parser.add_argument('--duration', type=int, default=5, help='每个测试的持续时间(秒)')
    parser.add_argument('--token', default='yiye.123', help='FRP认证token')
    parser.add_argument('--user', default='yiye', help='FRP认证用户')
    
    args = parser.parse_args()
    
    tester = BandwidthTester(
        db_path=args.db,
        file_path=args.file,
        test_duration=args.duration,
        auth_token=args.token,
        auth_user=args.user
    )
    
    tester.run(max_servers=args.max_servers)

if __name__ == '__main__':
    main()
