#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端口扫描和FRP带宽测试 Web应用
后端服务 - Flask
"""

import os
import subprocess
import json
import threading
import time
import random
import tempfile
import shutil
import sqlite3
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError
from flask import Flask, render_template, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import socket
import ipaddress

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

# 全局状态
scan_results = {}
frpc_processes = {}
http_servers = {}
test_results = {}

# 资源数据库路径
RESOURCES_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources.db')

# 端口分组
PORT_GROUPS = {
    "常用Web端口": [80, 443, 8080, 8443, 8888, 9090],
    "数据库端口": [3306, 5432, 1433, 1521, 27017, 6379],
    "远程管理端口": [22, 23, 3389, 5900, 5901],
    "文件传输端口": [20, 21, 69, 445, 139],
    "邮件端口": [25, 110, 143, 465, 587, 993, 995],
    "FRP端口": [7000, 7001, 7500, 9999],
    "DNS端口": [53, 5353],
    "游戏/媒体端口": [1935, 3478, 5060, 6000, 8000, 8080],
    "自定义常用": [10000, 20000, 30000, 40000, 50000]
}

# 常见端口说明
COMMON_PORTS = {
    20: "FTP数据传输",
    21: "FTP控制连接",
    22: "SSH安全登录",
    23: "Telnet远程登录",
    25: "SMTP邮件发送",
    53: "DNS域名解析",
    80: "HTTP网页服务",
    110: "POP3邮件接收",
    111: "RPCBind远程调用",
    135: "MSRPC Windows远程",
    139: "NetBIOS网络服务",
    143: "IMAP邮件访问",
    443: "HTTPS加密网页",
    445: "SMB文件共享",
    993: "IMAPS加密邮件",
    995: "POP3S加密邮件",
    1433: "MSSQL数据库",
    1521: "Oracle数据库",
    2049: "NFS网络文件系统",
    3306: "MySQL数据库",
    3389: "RDP远程桌面",
    5432: "PostgreSQL数据库",
    5900: "VNC远程控制",
    6379: "Redis缓存",
    6999: "FRP服务端",
    7000: "FRP服务端(默认)",
    7001: "FRP服务端(备用)",
    7500: "FRP服务端(高级)",
    8080: "HTTP代理/Web服务",
    8443: "HTTPS代理",
    8888: "HTTP代理(备用)",
    9090: "Web管理界面",
    9200: "Elasticsearch",
    9999: "FRP服务端(自定义)",
    10000: "Webmin管理",
    27017: "MongoDB数据库"
}

def get_common_port_description(port):
    """获取端口的常见用途描述"""
    return COMMON_PORTS.get(port, "未知端口")

def run_nmap_scan(ip, top_ports=1000, custom_ports=''):
    """运行nmap扫描 - 使用TCP connect扫描，不需要root权限"""
    try:
        # 使用-sT (TCP连接扫描) 不需要root
        cmd = [
            "nmap",
            "-sT",  # TCP连接扫描（不需要root）
        ]
        
        # 如果有自定义端口，使用-p参数
        if custom_ports:
            cmd.extend(["-p", custom_ports])
        else:
            cmd.extend(["--top-ports", str(top_ports)])
        
        cmd.extend([
            "-oX", "-",  # XML输出到stdout
            "--max-retries", "1",  # 减少重试次数加快扫描
            "--host-timeout", "30s",  # 单个主机超时
            ip
        ])
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120  # 2分钟超时
        )
        
        return result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return "", "扫描超时"
    except Exception as e:
        return "", str(e)

def parse_nmap_output(xml_output):
    """解析nmap XML输出"""
    ports = []
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_output)
        
        for host in root.findall('.//host'):
            for port_elem in host.findall('.//port'):
                portid = int(port_elem.get('portid', 0))
                protocol = port_elem.get('protocol', 'tcp')
                
                state_elem = port_elem.find('state')
                state = state_elem.get('state', 'unknown') if state_elem is not None else 'unknown'
                
                service_elem = port_elem.find('service')
                service = service_elem.get('name', 'unknown') if service_elem is not None else 'unknown'
                product = service_elem.get('product', '') if service_elem is not None else ''
                version = service_elem.get('version', '') if service_elem is not None else ''
                
                ports.append({
                    'port': portid,
                    'protocol': protocol,
                    'state': state,
                    'service': service,
                    'product': product,
                    'version': version,
                    'description': get_common_port_description(portid)
                })
    except Exception as e:
        print(f"解析nmap输出错误: {e}")
    
    return ports

@app.route('/')
def index():
    """主页"""
    return render_template('index.html')

@app.route('/api/scan', methods=['POST'])
def scan():
    """执行端口扫描"""
    data = request.json
    ip = data.get('ip', '')
    top_ports = data.get('top_ports', 1000)
    custom_ports = data.get('custom_ports', '')
    
    if not ip:
        return jsonify({'error': '请输入IP地址'}), 400
    
    # 运行nmap扫描
    xml_output, error = run_nmap_scan(ip, top_ports, custom_ports)
    
    if error and not xml_output:
        return jsonify({'error': f'扫描失败: {error}'}), 500
    
    # 解析结果
    parsed_ports = parse_nmap_output(xml_output)
    
    # 分类端口
    open_ports = [p for p in parsed_ports if p['state'] == 'open']
    closed_ports = [p for p in parsed_ports if p['state'] == 'closed']
    filtered_ports = [p for p in parsed_ports if p['state'] == 'filtered']
    
    # 存储结果
    scan_id = f"{ip}_{int(time.time())}"
    scan_results[scan_id] = {
        'ip': ip,
        'scan_time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'all_ports': parsed_ports,
        'open_ports': open_ports,
        'closed_ports': closed_ports,
        'filtered_ports': filtered_ports,
        'total_scanned': len(parsed_ports)
    }
    
    return jsonify({
        'scan_id': scan_id,
        'ip': ip,
        'scan_time': scan_results[scan_id]['scan_time'],
        'total_scanned': len(parsed_ports),
        'open_ports': open_ports,
        'closed_ports': closed_ports,
        'filtered_ports': filtered_ports
    })

@app.route('/api/scan/suggest-ports/<scan_id>')
def suggest_ports(scan_id):
    """推荐可用的映射端口"""
    if scan_id not in scan_results:
        return jsonify({'error': '扫描结果不存在'}), 404
    
    result = scan_results[scan_id]
    
    # 收集已使用的端口（扫描到的open端口）
    open_ports_set = set()
    for port in result['open_ports']:
        open_ports_set.add(port['port'])
    
    # 收集closed端口
    closed_ports_list = [p['port'] for p in result['closed_ports']]
    
    # 推荐远程端口（优先closed，然后是未被占用的）
    suggested_remote = []
    
    # 1. 优先选择closed的端口
    for port in closed_ports_list:
        suggested_remote.append({
            'port': port,
            'reason': '端口已关闭，可用作映射',
            'priority': 1,
            'type': 'closed'
        })
    
    # 2. 然后选择未扫描的常用端口（未被占用）
    common_ports = [8080, 8888, 9090, 10000, 12345, 20000, 25000, 30000, 40000, 50000]
    for port in common_ports:
        if port not in open_ports_set and port not in closed_ports_list:
            suggested_remote.append({
                'port': port,
                'reason': '未被占用的常用端口',
                'priority': 2,
                'type': 'available'
            })
    
    # 按优先级排序
    suggested_remote.sort(key=lambda x: (x['priority'], x['port']))
    
    # 检测本地可用端口
    suggested_local = find_available_ports(8000, 3)
    
    return jsonify({
        'scan_id': scan_id,
        'suggested_remote_ports': suggested_remote[:15],
        'suggested_local_ports': suggested_local
    })

def find_available_ports(start_port=8000, count=3):
    """检测本地可用端口"""
    available = []
    port = start_port
    
    while len(available) < count and port < 65535:
        if is_port_available(port):
            available.append({
                'port': port,
                'reason': '端口未被占用',
                'available': True
            })
        port += 1
    
    return available

def is_port_available(port):
    """检测端口是否可用"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.1)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        return result != 0  # 连接失败表示端口未被占用
    except:
        return True

@app.route('/api/ports/find-available', methods=['POST'])
def find_available():
    """查找可用端口"""
    data = request.json
    start_port = data.get('start_port', 8000)
    count = data.get('count', 3)
    
    available = find_available_ports(start_port, count)
    return jsonify({
        'available_ports': available,
        'count': len(available)
    })

@app.route('/api/port-groups')
def get_port_groups():
    """获取端口分组"""
    return jsonify({'port_groups': PORT_GROUPS})

@app.route('/api/ips/from-db')
def get_ips_from_db():
    """从resources.db获取IP列表（分页+排序）"""
    try:
        if not os.path.exists(RESOURCES_DB):
            return jsonify({'error': '数据库文件不存在'}), 404
        
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        sort_by = request.args.get('sort', 'server')
        sort_dir = request.args.get('dir', 'asc')
        
        allowed_sorts = {'server': 'server', 'port': 'port', 'speed': 'speed', 'description': 'description'}
        sort_col = allowed_sorts.get(sort_by, 'server')
        direction = 'ASC' if sort_dir == 'asc' else 'DESC'
        
        conn = sqlite3.connect(RESOURCES_DB)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(DISTINCT server) FROM public_ips WHERE server IS NOT NULL")
        total = cursor.fetchone()[0]
        
        offset = (page - 1) * per_page
        query = f"""SELECT server, 
                           GROUP_CONCAT(DISTINCT port) as ports,
                           MIN(speed) as speed,
                           MIN(description) as description
                    FROM public_ips 
                    WHERE server IS NOT NULL 
                    GROUP BY server 
                    ORDER BY {sort_col} {direction}
                    LIMIT ? OFFSET ?"""
        cursor.execute(query, (per_page, offset))
        rows = cursor.fetchall()
        conn.close()
        
        ips = []
        for row in rows:
            ips.append({
                'server': row[0],
                'port': row[1],
                'speed': row[2] or '',
                'description': row[3] or ''
            })
        
        total_pages = (total + per_page - 1) // per_page
        
        return jsonify({
            'ips': ips,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
            'sort': sort_by,
            'dir': sort_dir
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/ips/parse', methods=['POST'])
def parse_ips():
    """解析IP地址列表，支持CIDR格式"""
    data = request.json
    ip_input = data.get('ip', '')
    
    if not ip_input:
        return jsonify({'error': '请输入IP地址'}), 400
    
    ips = []
    
    # 支持多行输入
    for line in ip_input.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        
        # 检查是否是CIDR格式
        if '/' in line:
            try:
                network = ipaddress.ip_network(line, strict=False)
                # 限制最多扩展到/24
                if network.prefixlen >= 24:
                    for ip in network.hosts():
                        ips.append(str(ip))
                else:
                    # 对于更大的网络，只返回前100个
                    for i, ip in enumerate(network.hosts()):
                        if i >= 100:
                            break
                        ips.append(str(ip))
            except ValueError:
                # 如果不是有效的CIDR，当作普通IP处理
                ips.append(line)
        else:
            # 普通IP地址
            try:
                ipaddress.ip_address(line)
                ips.append(line)
            except ValueError:
                # 可能是IP范围如 192.168.1.1-100
                if '-' in line:
                    parts = line.split('-')
                    if len(parts) == 2:
                        base_ip = parts[0].rsplit('.', 1)[0]
                        start = int(parts[0].rsplit('.', 1)[1])
                        end = int(parts[1])
                        for i in range(start, min(end + 1, start + 100)):
                            ips.append(f"{base_ip}.{i}")
                else:
                    ips.append(line)
    
    return jsonify({'ips': ips[:200], 'count': len(ips[:200])})

@app.route('/api/ports/check', methods=['POST'])
def check_port():
    """检测端口是否可用"""
    data = request.json
    port = data.get('port')
    
    if not port:
        return jsonify({'error': '请提供端口号'}), 400
    
    available = is_port_available(int(port))
    return jsonify({
        'port': port,
        'available': available,
        'status': '可用' if available else '已被占用'
    })

@app.route('/api/folder/list', methods=['POST'])
def list_folder():
    """列出文件夹内容"""
    data = request.json
    path = data.get('path', os.path.expanduser('~'))
    
    try:
        if not os.path.exists(path):
            return jsonify({'error': '路径不存在'}), 400
        
        items = []
        for item in sorted(os.listdir(path)):
            full_path = os.path.join(path, item)
            is_dir = os.path.isdir(full_path)
            items.append({
                'name': item,
                'path': full_path,
                'is_dir': is_dir,
                'size': os.path.getsize(full_path) if not is_dir else 0
            })
        
        return jsonify({
            'current_path': path,
            'parent_path': os.path.dirname(path),
            'items': items
        })
    except PermissionError:
        return jsonify({'error': '没有访问权限'}), 403
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/frpc/create', methods=['POST'])
def create_frpc():
    """创建frpc配置"""
    data = request.json
    server_ip = data.get('server_ip')
    server_port = data.get('server_port')
    token = data.get('token', '')
    user = data.get('user', '')
    mappings = data.get('mappings', [])  # 端口映射列表 [{remote: 8080, local: 8001}, ...]
    
    if not server_ip or not server_port:
        return jsonify({'error': '缺少FRP服务器地址'}), 400
    
    if not mappings:
        return jsonify({'error': '请至少配置一个端口映射'}), 400
    
    # 创建配置目录
    config_dir = tempfile.mkdtemp(prefix='frpc_')
    config_path = os.path.join(config_dir, 'frpc.toml')
    
    # 生成配置文件 - TOML格式
    config_content = f'serverAddr = "{server_ip}"\n'
    config_content += f'serverPort = {server_port}\n'
    
    if token:
        config_content += f'auth.method = "token"\n'
        config_content += f'auth.token = "{token}"\n'
    
    config_content += "\n"
    
    # 为每个映射生成代理配置
    for i, mapping in enumerate(mappings):
        remote_port = mapping.get('remote')
        local_port = mapping.get('local')
        proxy_name = f"tunnel_{i+1}"
        
        config_content += f"""[[proxies]]
name = "{proxy_name}"
type = "tcp"
localIP = "127.0.0.1"
localPort = {local_port}
remotePort = {remote_port}
"""
    
    with open(config_path, 'w') as f:
        f.write(config_content)
    
    return jsonify({
        'config_dir': config_dir,
        'config_path': config_path,
        'config_content': config_content,
        'mapping_count': len(mappings)
    })

@app.route('/api/frpc/start', methods=['POST'])
def start_frpc():
    """启动frpc"""
    data = request.json
    config_path = data.get('config_path')
    
    if not config_path or not os.path.exists(config_path):
        return jsonify({'error': '配置文件不存在', 'status': 'failed'}), 400
    
    try:
        # 读取配置文件内容
        with open(config_path, 'r') as f:
            config_content = f.read()
        
        # 启动frpc
        frpc_cmd = [
            os.path.expanduser("~/bin/frpc"),
            "-c", config_path
        ]
        
        process = subprocess.Popen(
            frpc_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # 生成唯一ID
        frpc_id = f"frpc_{int(time.time())}_{random.randint(1000, 9999)}"
        frpc_processes[frpc_id] = {
            'process': process,
            'config_path': config_path,
            'start_time': time.strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # 等待连接建立
        time.sleep(3)
        
        # 检查进程状态
        if process.poll() is not None:
            stdout = process.stdout.read().decode()
            stderr = process.stderr.read().decode()
            return jsonify({
                'error': f'frpc启动失败',
                'stdout': stdout,
                'stderr': stderr,
                'config_content': config_content,
                'status': 'failed'
            }), 500
        
        return jsonify({
            'frpc_id': frpc_id,
            'status': 'running',
            'start_time': frpc_processes[frpc_id]['start_time'],
            'config_content': config_content
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/frpc/stop/<frpc_id>', methods=['POST'])
def stop_frpc(frpc_id):
    """停止frpc"""
    if frpc_id not in frpc_processes:
        return jsonify({'error': 'frpc进程不存在'}), 404
    
    try:
        process = frpc_processes[frpc_id]['process']
        process.terminate()
        process.wait(timeout=5)
        
        # 清理配置目录
        config_path = frpc_processes[frpc_id]['config_path']
        config_dir = os.path.dirname(config_path)
        shutil.rmtree(config_dir, ignore_errors=True)
        
        del frpc_processes[frpc_id]
        
        return jsonify({'status': 'stopped'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/frpc/status')
def frpc_status():
    """获取所有frpc进程状态"""
    status = {}
    for frpc_id, info in frpc_processes.items():
        process = info['process']
        status[frpc_id] = {
            'config_path': info['config_path'],
            'start_time': info['start_time'],
            'running': process.poll() is None
        }
    return jsonify(status)

@app.route('/api/httpserver/start', methods=['POST'])
def start_httpserver():
    """启动HTTP服务器"""
    data = request.json
    folder_path = data.get('folder_path')
    port = data.get('port', 0)
    
    if not folder_path or not os.path.exists(folder_path):
        return jsonify({'error': '文件夹不存在'}), 400
    
    if port == 0:
        port = random.randint(10000, 60000)
    
    try:
        # 创建HTTP服务器
        class QuietHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=folder_path, **kwargs)
            
            def log_message(self, format, *args):
                pass
            
            def copyfile(self, source, outputfile):
                try:
                    shutil.copyfileobj(source, outputfile)
                except (ConnectionResetError, BrokenPipeError, OSError):
                    pass
        
        server = HTTPServer(('127.0.0.1', port), QuietHandler)
        
        # 在后台线程运行
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        
        # 生成唯一ID
        server_id = f"http_{int(time.time())}_{random.randint(1000, 9999)}"
        http_servers[server_id] = {
            'server': server,
            'folder_path': folder_path,
            'port': port,
            'start_time': time.strftime('%Y-%m-%d %H:%M:%S')
        }
        
        return jsonify({
            'server_id': server_id,
            'port': port,
            'folder_path': folder_path,
            'start_time': http_servers[server_id]['start_time']
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/httpserver/stop/<server_id>', methods=['POST'])
def stop_httpserver(server_id):
    """停止HTTP服务器"""
    if server_id not in http_servers:
        return jsonify({'error': 'HTTP服务器不存在'}), 404
    
    try:
        http_servers[server_id]['server'].shutdown()
        del http_servers[server_id]
        return jsonify({'status': 'stopped'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/httpserver/status')
def httpserver_status():
    """获取所有HTTP服务器状态"""
    status = {}
    for server_id, info in http_servers.items():
        status[server_id] = {
            'folder_path': info['folder_path'],
            'port': info['port'],
            'start_time': info['start_time']
        }
    return jsonify(status)

@app.route('/api/test/download', methods=['POST'])
def test_download():
    """测试HTTP下载速度 - 流式进度，保证下载完成"""
    data = request.json
    url = data.get('url')
    
    if not url:
        return jsonify({'error': '请提供URL'}), 400
    
    def generate():
        try:
            start_time = time.time()
            total_bytes = 0
            last_report_time = start_time
            last_report_bytes = 0
            
            req = Request(url)
            with urlopen(req, timeout=60) as response:
                content_length = int(response.headers.get('Content-Length', 0))
                
                # 发送第一个进度，包含文件大小
                init_data = json.dumps({
                    'type': 'progress',
                    'total_bytes': 0,
                    'content_length': content_length,
                    'elapsed': 0,
                    'speed': 0,
                    'avg_speed': 0
                })
                yield f"data: {init_data}\n\n"
                
                while True:
                    chunk = response.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    
                    now = time.time()
                    elapsed = now - start_time
                    
                    # 每0.5秒报告一次进度
                    if now - last_report_time >= 0.5:
                        interval_bytes = total_bytes - last_report_bytes
                        interval_time = now - last_report_time
                        speed = interval_bytes / interval_time if interval_time > 0 else 0
                        avg_speed = total_bytes / elapsed if elapsed > 0 else 0
                        
                        progress_data = json.dumps({
                            'type': 'progress',
                            'total_bytes': total_bytes,
                            'content_length': content_length,
                            'elapsed': round(elapsed, 2),
                            'speed': round(speed / 1024 / 1024, 2),
                            'avg_speed': round(avg_speed / 1024 / 1024, 2)
                        })
                        yield f"data: {progress_data}\n\n"
                        
                        last_report_time = now
                        last_report_bytes = total_bytes
            
            elapsed = time.time() - start_time
            avg_speed = total_bytes / elapsed / 1024 / 1024 if elapsed > 0 else 0
            
            result_data = json.dumps({
                'type': 'complete',
                'url': url,
                'total_bytes': total_bytes,
                'content_length': content_length,
                'elapsed': round(elapsed, 2),
                'avg_speed': round(avg_speed, 2),
                'status': 'success'
            })
            yield f"data: {result_data}\n\n"
            
        except Exception as e:
            error_data = json.dumps({
                'type': 'error',
                'url': url,
                'error': str(e),
                'status': 'failed'
            })
            yield f"data: {error_data}\n\n"
    
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/test/connect', methods=['POST'])
def test_connect():
    """测试端口连接"""
    data = request.json
    ip = data.get('ip')
    port = data.get('port')
    timeout = data.get('timeout', 5)
    
    if not ip or not port:
        return jsonify({'error': '请提供IP和端口'}), 400
    
    try:
        start_time = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, int(port)))
        end_time = time.time()
        sock.close()
        
        latency = (end_time - start_time) * 1000  # 转换为毫秒
        
        return jsonify({
            'ip': ip,
            'port': port,
            'reachable': result == 0,
            'latency_ms': latency,
            'status': 'open' if result == 0 else 'closed'
        })
    except Exception as e:
        return jsonify({
            'ip': ip,
            'port': port,
            'reachable': False,
            'error': str(e),
            'status': 'error'
        })

@app.route('/api/test/batch', methods=['POST'])
def batch_test():
    """批量测试下载 - 下载完整文件"""
    data = request.json
    tests = data.get('tests', [])
    
    results = []
    for test in tests:
        url = test.get('url')
        if url:
            try:
                start_download = time.time()
                response = urlopen(url, timeout=60)
                
                total_bytes = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                
                duration_actual = time.time() - start_download
                
                if duration_actual > 0 and total_bytes > 0:
                    speed_mbps = (total_bytes * 8) / duration_actual / (1024 * 1024)
                    results.append({
                        'url': url,
                        'total_bytes': total_bytes,
                        'duration': duration_actual,
                        'speed_mbps': speed_mbps,
                        'status': 'success'
                    })
                else:
                    results.append({
                        'url': url,
                        'status': 'failed',
                        'error': '下载失败或超时'
                    })
            except Exception as e:
                results.append({
                    'url': url,
                    'status': 'failed',
                    'error': str(e)
                })
    
    return jsonify({'results': results})

# ==================== 历史记录 ====================

HISTORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'history')
os.makedirs(HISTORY_DIR, exist_ok=True)

@app.route('/api/history/save', methods=['POST'])
def save_history():
    """保存扫描和配置结果"""
    data = request.json
    record_type = data.get('type', 'scan')
    record = data.get('record', {})
    
    if not record:
        return jsonify({'error': '无数据可保存'}), 400
    
    record['saved_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    record['type'] = record_type
    
    filename = f"{record_type}_{int(time.time())}_{random.randint(1000, 9999)}.json"
    filepath = os.path.join(HISTORY_DIR, filename)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    
    return jsonify({'filename': filename, 'filepath': filepath})

@app.route('/api/history/list')
def list_history():
    """列出历史记录"""
    record_type = request.args.get('type', '')
    
    files = []
    for fn in sorted(os.listdir(HISTORY_DIR), reverse=True):
        if not fn.endswith('.json'):
            continue
        if record_type and not fn.startswith(record_type):
            continue
        filepath = os.path.join(HISTORY_DIR, fn)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                record = json.load(f)
            files.append({
                'filename': fn,
                'type': record.get('type', ''),
                'saved_at': record.get('saved_at', ''),
                'summary': record.get('summary', fn)
            })
        except:
            pass
    
    return jsonify({'files': files})

@app.route('/api/history/load/<filename>')
def load_history(filename):
    """加载历史记录"""
    filepath = os.path.join(HISTORY_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({'error': '记录不存在'}), 404
    
    with open(filepath, 'r', encoding='utf-8') as f:
        record = json.load(f)
    
    return jsonify(record)

@app.route('/api/history/delete/<filename>', methods=['POST'])
def delete_history(filename):
    """删除历史记录"""
    filepath = os.path.join(HISTORY_DIR, filename)
    if os.path.exists(filepath):
        os.remove(filepath)
    return jsonify({'status': 'deleted'})

# ==================== 批量下载测试 ====================

@app.route('/api/batch/download-test', methods=['POST'])
def batch_download_test():
    """批量下载测试 - 完整流程：扫描→端口映射→frpc→httpserver→下载测试"""
    data = request.json
    ips = data.get('ips', [])
    test_file = data.get('test_file', '50M')
    http_folder = data.get('http_folder', '')
    frpc_server_ip = data.get('frpc_server_ip', '')
    frpc_server_port = data.get('frpc_server_port', 7000)
    frpc_remote_port = data.get('frpc_remote_port', None)  # 手动指定的远程端口
    frpc_token = data.get('frpc_token', '')
    frpc_user = data.get('frpc_user', '')
    top_ports = data.get('top_ports', 1000)
    
    if not ips:
        return jsonify({'error': '请选择IP地址'}), 400
    
    def generate():
        active_frpc = []
        active_http = []
        
        try:
            # 步骤1: 启动本地HTTP服务器
            if http_folder and os.path.exists(http_folder):
                yield _sse('progress', {'step': 'httpserver', 'message': f'启动本地HTTP服务器 目录:{http_folder}'})
                
                http_port = random.randint(10000, 60000)
                while not _is_port_available(http_port):
                    http_port = random.randint(10000, 60000)
                
                class QuietHandler(SimpleHTTPRequestHandler):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, directory=http_folder, **kwargs)
                    def log_message(self, format, *args):
                        pass
                    def copyfile(self, source, outputfile):
                        try:
                            shutil.copyfileobj(source, outputfile)
                        except (ConnectionResetError, BrokenPipeError, OSError):
                            pass
                
                http_server = HTTPServer(('127.0.0.1', http_port), QuietHandler)
                threading.Thread(target=http_server.serve_forever, daemon=True).start()
                active_http.append(http_server)
                yield _sse('progress', {'step': 'httpserver', 'message': f'HTTP服务器已启动 端口:{http_port} 目录:{http_folder}'})
            else:
                http_port = 8080
                yield _sse('progress', {'step': 'httpserver', 'message': f'未指定目录，使用已有HTTP服务器 端口:{http_port}'})
            
            # 步骤2: 逐个测试IP
            for idx, ip_info in enumerate(ips):
                server = ip_info.get('server', '')
                db_port = ip_info.get('port', '')
                yield _sse('progress', {
                    'step': 'start',
                    'index': idx,
                    'total': len(ips),
                    'server': server,
                    'message': f'[{idx+1}/{len(ips)}] 开始测试 {server}'
                })
                
                result = {
                    'server': server,
                    'db_port': db_port,
                    'status': 'failed',
                    'scan_result': None,
                    'frpc_config': None,
                    'download_result': None,
                    'error': None,
                    'logs': []
                }
                
                def add_log(msg):
                    result['logs'].append(msg)
                    yield _sse('progress', {
                        'step': 'log',
                        'index': idx,
                        'server': server,
                        'message': msg
                    })
                
                try:
                    # 步骤2a: nmap扫描
                    add_log(f'开始扫描 {server} (top {top_ports} 端口)...')
                    
                    xml_output, error = _run_nmap_scan(server, top_ports)
                    if error and not xml_output:
                        result['error'] = f'扫描失败: {error}'
                        add_log(f'扫描失败: {error}')
                        yield _sse('result', result)
                        continue
                    
                    parsed_ports = _parse_nmap_output(xml_output)
                    open_ports = [p for p in parsed_ports if p['state'] == 'open']
                    closed_ports = [p for p in parsed_ports if p['state'] == 'closed']
                    filtered_ports = [p for p in parsed_ports if p['state'] == 'filtered']
                    
                    add_log(f'扫描完成: 开放={len(open_ports)} 关闭={len(closed_ports)} 过滤={len(filtered_ports)}')
                    if open_ports:
                        add_log(f'开放端口: {[p["port"] for p in open_ports[:15]]}')
                    if closed_ports:
                        add_log(f'关闭端口: {[p["port"] for p in closed_ports[:15]]}')
                    
                    result['scan_result'] = {
                        'open_count': len(open_ports),
                        'closed_count': len(closed_ports),
                        'filtered_count': len(filtered_ports),
                        'open_ports': [p['port'] for p in open_ports[:20]],
                        'closed_ports': [p['port'] for p in closed_ports[:20]]
                    }
                    
                    # 步骤2b: 选择映射端口 - 优先closed端口
                    used_ports = set(p['port'] for p in open_ports)
                    remote_port = None
                    port_source = ''
                    
                    # 0. 如果手动指定了远程端口，直接使用
                    if frpc_remote_port:
                        remote_port = frpc_remote_port
                        port_source = f'手动配置端口 {frpc_remote_port}'
                        add_log(f'使用手动配置的远程端口: {frpc_remote_port}')
                    
                    # 1. 优先选择closed端口
                    if not remote_port:
                        for cp in closed_ports:
                            p = cp['port']
                            if p not in used_ports and p != 22 and p != 23:
                                remote_port = p
                                port_source = f'closed端口 {p}'
                                break
                    
                    # 2. 如果没有closed端口，检查db中的端口是否可用
                    if not remote_port and db_port:
                        try:
                            db_port_int = int(db_port.split(',')[0])
                            if db_port_int not in used_ports and db_port_int > 1024:
                                # 尝试连接检查端口是否真正可用
                                if _check_remote_port_available(server, db_port_int):
                                    remote_port = db_port_int
                                    port_source = f'db端口 {db_port_int} (可用)'
                                else:
                                    add_log(f'db端口 {db_port_int} 已被占用，跳过')
                        except:
                            pass
                    
                    # 3. 尝试常用端口
                    if not remote_port:
                        for p in [8080, 8888, 9090, 10000, 12345, 20000, 25000, 30000, 40000, 50000]:
                            if p not in used_ports:
                                if _check_remote_port_available(server, p):
                                    remote_port = p
                                    port_source = f'常用端口 {p} (可用)'
                                    break
                    
                    # 4. 最后随机选择
                    if not remote_port:
                        for _ in range(20):
                            p = random.randint(40000, 50000)
                            if p not in used_ports and _check_remote_port_available(server, p):
                                remote_port = p
                                port_source = f'随机端口 {p} (可用)'
                                break
                    
                    if not remote_port:
                        remote_port = random.randint(40000, 50000)
                        port_source = f'随机端口 {remote_port} (未验证)'
                    
                    local_port = http_port
                    
                    add_log(f'端口映射: {server}:{remote_port} → 127.0.0.1:{local_port} (来源: {port_source})')
                    
                    # 步骤2c: 生成frpc配置
                    config_dir = tempfile.mkdtemp(prefix='frpc_')
                    config_path = os.path.join(config_dir, 'frpc.toml')
                    
                    config_content = f'serverAddr = "{frpc_server_ip}"\n'
                    config_content += f'serverPort = {frpc_server_port}\n'
                    if frpc_token:
                        config_content += f'auth.method = "token"\n'
                        config_content += f'auth.token = "{frpc_token}"\n'
                    config_content += f'\n[[proxies]]\n'
                    config_content += f'name = "test_{server}"\n'
                    config_content += f'type = "tcp"\n'
                    config_content += f'localIP = "127.0.0.1"\n'
                    config_content += f'localPort = {local_port}\n'
                    config_content += f'remotePort = {remote_port}\n'
                    
                    with open(config_path, 'w') as f:
                        f.write(config_content)
                    
                    # 保存配置到history目录
                    config_save_path = os.path.join(HISTORY_DIR, f'frpc_{server}_{remote_port}.toml')
                    with open(config_save_path, 'w') as f:
                        f.write(config_content)
                    add_log(f'配置已保存: {config_save_path}')
                    
                    result['frpc_config'] = {
                        'remote_port': remote_port,
                        'local_port': local_port,
                        'config_path': config_path,
                        'config_content': config_content,
                        'saved_path': config_save_path
                    }
                    
                    add_log(f'frpc配置已生成: {config_path}')
                    add_log(f'配置内容:\n{config_content}')
                    
                    # 步骤2d: 启动frpc
                    add_log(f'启动frpc: ~/bin/frpc -c {config_path}')
                    
                    frpc_process = subprocess.Popen(
                        [os.path.expanduser("~/bin/frpc"), "-c", config_path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE
                    )
                    active_frpc.append((frpc_process, config_dir))
                    
                    # 等待frpc启动并检查状态
                    time.sleep(4)
                    
                    if frpc_process.poll() is not None:
                        stdout = frpc_process.stdout.read().decode()
                        stderr = frpc_process.stderr.read().decode()
                        # 清理ANSI颜色码
                        import re
                        clean_stdout = re.sub(r'\x1b\[[0-9;]*m', '', stdout)
                        clean_stderr = re.sub(r'\x1b\[[0-9;]*m', '', stderr)
                        
                        error_msg = f'frpc启动失败 (退出码:{frpc_process.returncode})'
                        add_log(error_msg)
                        if clean_stdout:
                            add_log(f'stdout:\n{clean_stdout.strip()[:1000]}')
                        if clean_stderr:
                            add_log(f'stderr:\n{clean_stderr.strip()[:1000]}')
                        add_log(f'配置文件路径: {config_path}')
                        add_log(f'配置文件内容:\n{config_content}')
                        
                        result['error'] = error_msg
                        result['frpc_config'] = {
                            'remote_port': remote_port,
                            'local_port': local_port,
                            'config_path': config_path,
                            'config_content': config_content,
                            'stdout': clean_stdout.strip(),
                            'stderr': clean_stderr.strip()
                        }
                        add_log(error_msg)
                        yield _sse('result', result)
                        continue
                    
                    add_log(f'frpc已启动 PID:{frpc_process.pid} 隧道:{remote_port}→{local_port}')
                    
                    # 步骤2e: 验证本地HTTP服务器可访问
                    add_log(f'验证本地HTTP服务器 127.0.0.1:{local_port}...')
                    time.sleep(1)
                    
                    local_test_url = f'http://127.0.0.1:{local_port}/{test_file}'
                    try:
                        req = Request(local_test_url)
                        with urlopen(req, timeout=5) as resp:
                            local_ok = True
                            add_log(f'本地HTTP服务器正常 状态码:{resp.status}')
                    except Exception as e:
                        local_ok = False
                        add_log(f'本地HTTP服务器异常: {e}')
                        result['error'] = f'本地HTTP服务器不可用: {e}'
                        yield _sse('result', result)
                        continue
                    
                    # 步骤2f: 下载测试
                    download_url = f'http://{server}:{remote_port}/{test_file}'
                    add_log(f'开始远程下载测试 {download_url}')
                    
                    dl_start = time.time()
                    dl_bytes = 0
                    dl_error = None
                    
                    # 尝试3次
                    for attempt in range(3):
                        try:
                            add_log(f'尝试 {attempt+1}/3...')
                            req = Request(download_url)
                            with urlopen(req, timeout=30) as response:
                                add_log(f'连接成功 状态码:{response.status}')
                                while True:
                                    chunk = response.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    dl_bytes += len(chunk)
                                    if time.time() - dl_start > 60:
                                        break
                            break
                        except Exception as e:
                            dl_error = str(e)
                            add_log(f'尝试 {attempt+1} 失败: {e}')
                            if attempt < 2:
                                time.sleep(2)
                    
                    dl_elapsed = time.time() - dl_start
                    
                    if dl_bytes > 0 and dl_elapsed > 0:
                        speed = dl_bytes / dl_elapsed / 1024 / 1024
                        result['download_result'] = {
                            'url': download_url,
                            'total_bytes': dl_bytes,
                            'elapsed': round(dl_elapsed, 2),
                            'speed_mbps': round(speed, 2)
                        }
                        result['status'] = 'success'
                        add_log(f'下载完成 速度:{round(speed, 2)} MB/s 大小:{round(dl_bytes/1024/1024, 2)} MB 耗时:{round(dl_elapsed, 2)}s')
                    else:
                        result['error'] = dl_error or '下载失败: 未收到数据'
                        add_log(f'下载失败: {dl_error or "未收到数据"}')
                    
                    yield _sse('result', result)
                    
                except Exception as e:
                    result['error'] = str(e)
                    add_log(f'异常: {e}')
                    yield _sse('result', result)
                
                finally:
                    # 清理当前IP的frpc进程
                    for proc, cdir in active_frpc[:]:
                        if proc.poll() is None:
                            proc.terminate()
                            try:
                                proc.wait(timeout=3)
                            except:
                                proc.kill()
                        try:
                            shutil.rmtree(cdir, ignore_errors=True)
                        except:
                            pass
                    active_frpc.clear()
            
            # 清理HTTP服务器
            for srv in active_http:
                try:
                    srv.shutdown()
                except:
                    pass
            
            yield _sse('complete', {'message': '所有测试完成'})
            
        except Exception as e:
            yield _sse('error', {'error': str(e)})
    
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _sse(event_type, data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

def _is_port_available(port):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.1)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        return result != 0
    except:
        return True

def _check_remote_port_available(ip, port):
    """检查远程端口是否可用（连接测试）"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result != 0  # 连接失败表示端口未被占用
    except:
        return True  # 无法判断时假设可用

def _run_nmap_scan(ip, top_ports=1000):
    try:
        cmd = ["nmap", "-sT", "--top-ports", str(top_ports),
               "-oX", "-", "--max-retries", "1", "--host-timeout", "30s", ip]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return "", "扫描超时"
    except Exception as e:
        return "", str(e)

def _parse_nmap_output(xml_output):
    import xml.etree.ElementTree as ET
    ports = []
    try:
        root = ET.fromstring(xml_output)
        for host in root.findall('.//host'):
            for port_elem in host.findall('.//port'):
                portid = int(port_elem.get('portid', 0))
                protocol = port_elem.get('protocol', 'tcp')
                state_elem = port_elem.find('state')
                state = state_elem.get('state', 'unknown') if state_elem is not None else 'unknown'
                service_elem = port_elem.find('service')
                service = service_elem.get('name', 'unknown') if service_elem is not None else 'unknown'
                ports.append({
                    'port': portid, 'protocol': protocol,
                    'state': state, 'service': service
                })
    except:
        pass
    return ports


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
