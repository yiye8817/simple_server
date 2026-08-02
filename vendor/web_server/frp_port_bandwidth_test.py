#!/usr/bin/env python3
"""
FRP 智能端口选择 + 带宽/延迟测试脚本
================================================

流程 (生产-消费流水线, 发现/扫描/测试三阶段并行):
  1. server 来源:
       - 数据库:  从 resources.db 的 public_ips 读取 server IP
       - 单 IP:   --ip
       - IP 段:   --cidr  扫描网段的 server-port(默认7000) 发现可用 server IP
  2. 对每个 IP 用 nmap 扫描最常用的 1000 个端口(结果带缓存, 已扫不重复),
     选择 frpc 隧道的远程端口:
       - 优先选择 closed 端口 (主机可达但端口空闲, 最适合 frps 绑定)
       - 其次选择没有被占用 (非 open) 的端口
  3. 在本地启动 HTTP 服务器共享 ~/51M, 并验证本地端口可用性。
  4. 生成 frpc 转发配置 (toml) 建立隧道: 本地 HTTP 端口 -> 远程端口。
     (失败时保留 frpc_configs/ 下的 .toml 与 .log 便于排查)
  5. 通过隧道下载 ~/51M, 测量网络延迟与下载带宽, 结果写入 JSON。

并行: IP 段发现(扫 7000) 与 端口扫描(top1000) 与 带宽测试 流水线并行进行。
缓存: top1000 扫描结果缓存于 scan_cache.json, 已完成扫描的 IP 不重复扫描。

注意: 运行本脚本需要真实外网访问 (nmap 扫描 + frpc 连接), 请在非沙箱环境执行。
"""

import argparse
import ipaddress
import json
import os
import queue
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.request import urlopen

FRPC_BIN = os.path.expanduser("~/bin/frpc")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "frpc_configs")
CACHE_PATH = os.path.join(BASE_DIR, "scan_cache.json")


# --------------------------------------------------------------------------- #
# 本地端口工具
# --------------------------------------------------------------------------- #
def is_local_port_free(port, host="127.0.0.1"):
    """验证本地端口是否可用 (可绑定)"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_free_local_port(host="127.0.0.1", attempts=100):
    """寻找一个可用的本地端口并验证其可用性"""
    for _ in range(attempts):
        port = random.randint(20000, 60000)
        if is_local_port_free(port, host):
            return port
    # 退化: 让内核分配
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
# 本地 HTTP 服务器 (共享 ~/ 目录下的测试文件)
# --------------------------------------------------------------------------- #
class _QuietHandler(SimpleHTTPRequestHandler):
    serve_dir = os.path.expanduser("~/")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=self.serve_dir, **kwargs)

    def log_message(self, fmt, *args):  # 静默
        pass

    def copyfile(self, source, outputfile):
        # 客户端提前断开(只读少量字节做访问验证)是正常现象, 静默忽略
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class _QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 抑制客户端断连导致的 traceback 噪音
        pass


def start_http_server():
    """启动本地 HTTP 服务器, 返回 (server, port)。端口已验证可用。"""
    port = find_free_local_port()
    httpd = _QuietHTTPServer(("127.0.0.1", port), _QuietHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
def get_servers(db_path, limit=None, shuffle=True):
    """读取 public_ips 中所有 server 及其控制端口 (去重, 过滤无效端口)"""
    import sqlite3

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT server, port FROM (
            SELECT server, port, ROW_NUMBER() OVER (
                PARTITION BY server ORDER BY id
            ) AS rn
            FROM public_ips
            WHERE port IS NOT NULL AND port != '' AND port != '0'
              AND server IS NOT NULL AND server != ''
        ) WHERE rn = 1
        """
    )
    rows = cur.fetchall()
    conn.close()

    servers = []
    for ip, port in rows:
        try:
            servers.append((ip.strip(), int(str(port).strip())))
        except (ValueError, AttributeError):
            continue

    if shuffle:
        random.shuffle(servers)
    if limit:
        servers = servers[:limit]
    return servers


def get_port_for_ip(db_path, ip):
    """查询单个 IP 的 frps 控制端口 (取最早一条有效记录)"""
    import sqlite3

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT port FROM public_ips
        WHERE server = ? AND port IS NOT NULL AND port != '' AND port != '0'
        ORDER BY id LIMIT 1
        """,
        (ip,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        return int(str(row[0]).strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# nmap 扫描 + 远程端口选择
# --------------------------------------------------------------------------- #
def _parse_services_spec(spec):
    """解析 nmap scaninfo 的 services 字段 (如 '1-4,80,443') 为端口集合"""
    ports = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                ports.update(range(int(a), int(b) + 1))
            except ValueError:
                pass
        else:
            try:
                ports.add(int(part))
            except ValueError:
                pass
    return ports


def nmap_scan(ip, top_ports=1000, host_timeout="60s"):
    """
    用 nmap 扫描最常用的 top_ports 端口。
    返回 dict: {'up': bool, 'states': {port: state}, 'scanned': set(port)}
    state 为每个被扫描端口的状态 (open/closed/filtered)。
    """
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
        xml_path = tf.name
    try:
        cmd = [
            "nmap", "-Pn", "-sT",
            "--top-ports", str(top_ports),
            "-T4", "--host-timeout", host_timeout,
            "-oX", xml_path, ip,
        ]
        subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=180,
        )
        return _parse_nmap_xml(xml_path)
    except subprocess.TimeoutExpired:
        return {"up": False, "states": {}, "scanned": set()}
    finally:
        try:
            os.unlink(xml_path)
        except OSError:
            pass


def nmap_service_scan(ip, ports, host_timeout="180s", timeout=300):
    """
    对指定端口做服务/版本详细扫描 (nmap -sV)。
    ports: 端口列表或 nmap 端口规格字符串。
    返回 {port: {state, service, product, version, extrainfo}}。
    """
    if isinstance(ports, (list, tuple, set)):
        spec = ",".join(str(p) for p in sorted(ports))
    else:
        spec = str(ports)
    if not spec:
        return {}
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
        xml_path = tf.name
    try:
        cmd = [
            "nmap", "-Pn", "-sT", "-sV", "--version-light",
            "-p", spec, "-T4", "--host-timeout", host_timeout,
            "-oX", xml_path, ip,
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout)
        return _parse_service_xml(xml_path)
    except subprocess.TimeoutExpired:
        return {}
    finally:
        try:
            os.unlink(xml_path)
        except OSError:
            pass


def _parse_service_xml(xml_path):
    info = {}
    try:
        root = ET.parse(xml_path).getroot()
        host = root.find("host")
        if host is None:
            return info
        ports_el = host.find("ports")
        if ports_el is None:
            return info
        for p in ports_el.findall("port"):
            pid = int(p.get("portid"))
            st = p.find("state")
            svc = p.find("service")
            cpes = [c.text for c in svc.findall("cpe")] if svc is not None else []
            info[pid] = {
                "state": st.get("state") if st is not None else None,
                "service": svc.get("name") if svc is not None else None,
                "product": svc.get("product") if svc is not None else None,
                "version": svc.get("version") if svc is not None else None,
                "extrainfo": svc.get("extrainfo") if svc is not None else None,
                "cpe": [c for c in cpes if c],
            }
    except (ET.ParseError, FileNotFoundError, ValueError):
        pass
    return info


def _parse_nmap_xml(xml_path):
    states = {}
    scanned = set()
    up = False
    default_state = None
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        scaninfo = root.find("scaninfo")
        if scaninfo is not None and scaninfo.get("services"):
            scanned = _parse_services_spec(scaninfo.get("services"))

        host = root.find("host")
        if host is None:
            return {"up": False, "states": {}, "scanned": scanned}

        status = host.find("status")
        if status is not None and status.get("state") == "up":
            up = True

        ports_el = host.find("ports")
        if ports_el is not None:
            # 显式上报的端口状态
            for p in ports_el.findall("port"):
                pid = int(p.get("portid"))
                st = p.find("state")
                if st is not None:
                    states[pid] = st.get("state")
            # 被折叠 (collapsed) 的大多数端口状态
            extras = ports_el.findall("extraports")
            if extras:
                # 取数量最多的一类作为未上报端口的默认状态
                extras.sort(key=lambda e: int(e.get("count", "0")), reverse=True)
                default_state = extras[0].get("state")

        # 用默认状态补全未显式上报的扫描端口
        if default_state and scanned:
            for p in scanned:
                states.setdefault(p, default_state)
    except (ET.ParseError, FileNotFoundError, ValueError):
        pass

    return {"up": up, "states": states, "scanned": scanned}


def choose_remote_ports(scan, max_candidates=4, exclude_ports=None):
    """
    根据扫描结果按优先级选择候选远程端口:
      1) closed 端口 (优先非特权 >=1024)
      2) 其它未被占用 (非 open) 端口
    返回有序候选端口列表。
    """
    exclude = set(exclude_ports or [])
    states = scan.get("states", {})
    if not states:
        return []

    closed_hi, closed_lo, other_hi, other_lo = [], [], [], []
    for port, st in states.items():
        if port in exclude:
            continue
        if st == "open":
            continue  # 已被占用, 跳过
        if st == "closed":
            (closed_hi if port >= 1024 else closed_lo).append(port)
        else:  # filtered / 其它: 视为"未被占用"的次选
            (other_hi if port >= 1024 else other_lo).append(port)

    for lst in (closed_hi, closed_lo, other_hi, other_lo):
        random.shuffle(lst)

    ordered = closed_hi + closed_lo + other_hi + other_lo
    return ordered[:max_candidates]


# --------------------------------------------------------------------------- #
# 扫描结果缓存 (避免重复扫描)
# --------------------------------------------------------------------------- #
class ScanCache:
    """nmap top-ports 扫描结果缓存, 线程安全, 持久化为 JSON"""

    def __init__(self, path=CACHE_PATH, enabled=True):
        self.path = path
        self.enabled = enabled
        self.lock = threading.Lock()
        self.data = {}
        if enabled and os.path.exists(path):
            try:
                with open(path, "r") as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                self.data = {}

    def get(self, ip):
        if not self.enabled:
            return None
        with self.lock:
            entry = self.data.get(ip)
        if not entry:
            return None
        # JSON 中端口 key 是字符串, 转回 int
        states = {int(k): v for k, v in entry.get("states", {}).items()}
        return {
            "up": entry.get("up", False),
            "states": states,
            "scanned": set(entry.get("scanned", [])),
        }

    def put(self, ip, scan):
        if not self.enabled:
            return
        with self.lock:
            self.data[ip] = {
                "up": scan.get("up", False),
                "states": {str(k): v for k, v in scan.get("states", {}).items()},
                "scanned": sorted(scan.get("scanned", [])),
                "ts": int(time.time()),
            }

    def save(self):
        if not self.enabled:
            return
        with self.lock:
            try:
                with open(self.path, "w") as f:
                    json.dump(self.data, f)
            except OSError:
                pass


def nmap_scan_cached(ip, cache, top_ports=1000, host_timeout="60s",
                     rescan=False):
    """带缓存的 top-ports 扫描。命中缓存(且非 rescan)时直接返回。"""
    if not rescan:
        cached = cache.get(ip)
        if cached is not None:
            cached["from_cache"] = True
            return cached
    scan = nmap_scan(ip, top_ports=top_ports, host_timeout=host_timeout)
    cache.put(ip, scan)
    scan["from_cache"] = False
    return scan


# --------------------------------------------------------------------------- #
# IP 段发现: 扫描指定端口(默认 7000)找出开放该端口的主机
# --------------------------------------------------------------------------- #
def expand_targets(spec):
    """
    将 --cidr 输入展开为 nmap 可扫描的目标块列表 (每块尽量 <= /24)。
    支持: CIDR(1.2.3.0/24), 单 IP, 逗号分隔, 以及 nmap 范围语法(1.2.3.1-254)。
    """
    blocks = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            # 非标准 CIDR (如 nmap 范围语法), 原样交给 nmap
            blocks.append(raw)
            continue
        if net.prefixlen >= 24 or net.version == 6:
            blocks.append(str(net))
        else:
            blocks.extend(str(sub) for sub in net.subnets(new_prefix=24))
    return blocks


def scan_open_port(target, ports=7000, host_timeout="40s", timeout=1200):
    """
    用 nmap 直接扫描 target(IP/CIDR/范围) 上设定的端口, 返回开放的 (ip, port) 列表。
    ports 支持 nmap 端口规格: 单端口 7000 / 多端口 "7000,8000" / 范围 "7000-7100"。
    """
    port_spec = str(ports)
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
        xml_path = tf.name
    try:
        cmd = [
            "nmap", "-Pn", "-sT", "-p", port_spec, "--open",
            "-T4", "--host-timeout", host_timeout,
            "-oX", xml_path, target,
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout)
        return _parse_open_hosts(xml_path)
    except subprocess.TimeoutExpired:
        return []
    finally:
        try:
            os.unlink(xml_path)
        except OSError:
            pass


def _parse_open_hosts(xml_path):
    """解析 nmap XML, 返回所有 open 端口的 (ip, port) 列表。"""
    found = []
    try:
        root = ET.parse(xml_path).getroot()
        for host in root.findall("host"):
            status = host.find("status")
            if status is None or status.get("state") != "up":
                continue
            addr_el = host.find("address")
            if addr_el is None:
                continue
            ip = addr_el.get("addr")
            ports_el = host.find("ports")
            if ports_el is None:
                continue
            for p in ports_el.findall("port"):
                st = p.find("state")
                if st is not None and st.get("state") == "open":
                    found.append((ip, int(p.get("portid"))))
    except (ET.ParseError, FileNotFoundError, ValueError):
        pass
    return found


# --------------------------------------------------------------------------- #
# frpc 隧道
# --------------------------------------------------------------------------- #
def build_frpc_config(server_ip, server_port, local_port, remote_port,
                      token=None, user=None, log_path=None, path=None):
    """生成 frpc 转发配置 (toml), 返回配置文件路径"""
    lines = [
        f'serverAddr = "{server_ip}"',
        f"serverPort = {server_port}",
        'loginFailExit = true',
    ]
    if user:
        lines.append(f'user = "{user}"')
    if token:
        lines.append('auth.method = "token"')
        lines.append(f'auth.token = "{token}"')
    if log_path:
        lines.append(f'log.to = "{log_path}"')
        lines.append('log.level = "info"')
    lines += [
        "",
        "[[proxies]]",
        f'name = "httptest_{remote_port}"',
        'type = "tcp"',
        'localIP = "127.0.0.1"',
        f"localPort = {local_port}",
        f"remotePort = {remote_port}",
    ]
    if path is None:
        fd, path = tempfile.mkstemp(suffix=".toml", prefix="frpc_")
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
    else:
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    return path


def write_multiproxy_config(server_ip, server_port, ports, token=None,
                            user=None, local_port=None, path=None):
    """
    为探测成功的多个远程端口生成一份可直接使用的 frpc.toml。
    每个端口一个 [[proxies]] 项; local_port 为 None 时 localPort=remotePort
    (用户按需改成自己要转发的本地服务端口)。
    """
    lines = [
        f"# 自动生成: {server_ip}:{server_port} 探测可用端口 {len(ports)} 个",
        f"# 请将各 proxy 的 localPort 改为你要转发的本地服务端口",
        f'serverAddr = "{server_ip}"',
        f"serverPort = {server_port}",
    ]
    if user:
        lines.append(f'user = "{user}"')
    if token:
        lines.append('auth.method = "token"')
        lines.append(f'auth.token = "{token}"')
    for rp in ports:
        lp = local_port if local_port else rp
        lines += [
            "",
            "[[proxies]]",
            f'name = "proxy_{rp}"',
            'type = "tcp"',
            'localIP = "127.0.0.1"',
            f"localPort = {lp}",
            f"remotePort = {rp}",
        ]
    if path is None:
        path = os.path.join(BASE_DIR, f"frpc_{server_ip}_{server_port}.toml")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def verify_frps_login(server_ip, server_port, token=None, user=None, wait=6):
    """
    用 frpc 实际发起 frp 协议握手, 判断目标是否为真正可用的 frps。
    云厂商安全设备会让整段 IP 所有端口都能 TCP 握手(假 open), 只有真正的
    frps 才会返回 'login to server success'。
    返回 (ok: bool, message: str)。
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)
    log_fd, log_path = tempfile.mkstemp(suffix=".log", prefix="frpcv_")
    os.close(log_fd)
    lines = [
        f'serverAddr = "{server_ip}"',
        f"serverPort = {server_port}",
        "loginFailExit = true",
        f'log.to = "{log_path}"',
        'log.level = "info"',
    ]
    if user:
        lines.append(f'user = "{user}"')
    if token:
        lines.append('auth.method = "token"')
        lines.append(f'auth.token = "{token}"')
    cfg_fd, cfg_path = tempfile.mkstemp(suffix=".toml", prefix="frpcv_")
    with os.fdopen(cfg_fd, "w") as f:
        f.write("\n".join(lines) + "\n")

    proc = subprocess.Popen([FRPC_BIN, "-c", cfg_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + wait
    ok, message = False, "握手超时(非 frps)"
    try:
        while time.time() < deadline:
            try:
                with open(log_path, "r", errors="ignore") as f:
                    log = f.read()
            except OSError:
                log = ""
            if "login to server success" in log:
                ok, message = True, "login success"
                break
            if proc.poll() is not None:
                m = re.search(r"login to the server failed[:.].*", log)
                message = (re.sub(r"\x1b\[[0-9;]*m", "", m.group(0)).strip()[:120]
                           if m else "frpc 退出(非 frps)")
                break
            time.sleep(0.3)
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for p in (cfg_path, log_path):
            try:
                os.unlink(p)
            except OSError:
                pass
    return ok, message


def start_frpc_tunnel(server_ip, server_port, local_port, remote_port,
                      token, user, wait=8):
    """
    启动 frpc 隧道, 等待代理建立成功。
    返回 (process, config_path, log_path, ok, message)
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)
    base = os.path.join(CONFIG_DIR, f"frpc_{server_ip}_{remote_port}")
    cfg_path = base + ".toml"
    log_path = base + ".log"
    build_frpc_config(
        server_ip, server_port, local_port, remote_port,
        token=token, user=user, log_path=log_path, path=cfg_path,
    )
    proc = subprocess.Popen(
        [FRPC_BIN, "-c", cfg_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    def read_log():
        try:
            with open(log_path, "r", errors="ignore") as f:
                return f.read()
        except OSError:
            return ""

    def extract_error(log):
        for pat in (r"login to the server failed[:.].*",
                    r"login to server failed[:.].*",
                    r"connect to server error[:.].*",
                    r"start error.*",
                    r"\[E\].*"):
            m = re.search(pat, log)
            if m:
                # 去掉 ANSI 颜色码
                return re.sub(r"\x1b\[[0-9;]*m", "", m.group(0)).strip()[:160]
        return None

    deadline = time.time() + wait
    ok, message = False, "超时未建立代理"
    while time.time() < deadline:
        log = read_log()
        if "start proxy success" in log:
            ok, message = True, "代理建立成功"
            break
        if proc.poll() is not None:
            err = extract_error(log)
            message = err or "frpc 进程退出 (连接/认证失败)"
            break
        err = extract_error(log)
        if err:
            message = err
            break
        time.sleep(0.4)

    return proc, cfg_path, log_path, ok, message


def stop_frpc(proc, cfg_path, log_path, keep=False):
    """停止 frpc 进程; keep=True 时保留配置和日志文件 (用于失败排查)"""
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if keep:
        return
    for p in (cfg_path, log_path):
        try:
            if p:
                os.unlink(p)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 延迟 & 带宽测量
# --------------------------------------------------------------------------- #
def measure_latency(host, port, samples=4, timeout=5):
    """TCP 连接延迟 (ms), 返回 (min, avg) 或 None"""
    lat = []
    for _ in range(samples):
        t0 = time.time()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                lat.append((time.time() - t0) * 1000)
        except OSError:
            pass
        time.sleep(0.2)
    if not lat:
        return None
    return min(lat), sum(lat) / len(lat)


def measure_bandwidth(url, duration=10, timeout=15):
    """下载测速, 返回 dict(speed_mbps, mb, seconds, ttfb_ms) 或 None"""
    try:
        t0 = time.time()
        resp = urlopen(url, timeout=timeout)
        ttfb_ms = (time.time() - t0) * 1000
        total = 0
        start = time.time()
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if time.time() - start > duration:
                break
        dur = time.time() - start
        if dur > 0 and total > 0:
            return {
                "speed_mbps": (total * 8) / dur / (1024 * 1024),
                "mb": total / (1024 * 1024),
                "seconds": round(dur, 3),
                "ttfb_ms": round(ttfb_ms, 2),
            }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return None


def verify_access(url, timeout=8):
    """通过映射后的远程端口访问本地服务, 验证是否可正常访问。
    返回 (ok, http_code 或 None, 说明/字节数)"""
    try:
        t0 = time.time()
        resp = urlopen(url, timeout=timeout)
        code = resp.getcode()
        data = resp.read(2048)
        rtt = (time.time() - t0) * 1000
        ok = (code == 200) and len(data) > 0
        return ok, code, {"bytes": len(data), "rtt_ms": round(rtt, 2)}
    except Exception as e:
        from urllib.error import HTTPError
        code = e.code if isinstance(e, HTTPError) else None
        return False, code, f"{type(e).__name__}: {e}"


def parse_port_range(spec):
    """解析端口范围 '2000-2010' 或单端口 '2000', 返回 (start, end)"""
    spec = spec.strip()
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", spec)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (min(a, b), max(a, b))
    if spec.isdigit():
        return (int(spec), int(spec))
    raise ValueError(f"无法解析端口范围: {spec}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
class Tester:
    def __init__(self, args):
        self.args = args
        self.results = []
        self.failures = []
        self.http_port = None

    def test_one(self, server_ip, server_port, candidates):
        """对单个服务器: 依次尝试候选远程端口, 成功则测延迟+带宽"""
        for remote_port in candidates:
            proc, cfg, log, ok, msg = start_frpc_tunnel(
                server_ip, server_port, self.http_port, remote_port,
                self.args.token, self.args.user, wait=self.args.wait,
            )
            keep = False
            try:
                if not ok:
                    keep = True
                    self.failures.append({
                        "server": server_ip, "server_port": server_port,
                        "remote_port": remote_port, "error": msg,
                        "config": cfg, "log": log,
                    })
                    print(f"    保留配置: {cfg}")
                    continue

                latency = measure_latency(server_ip, remote_port)
                url = f"http://{server_ip}:{remote_port}/{self.args.file}"
                bw = measure_bandwidth(url, duration=self.args.duration)

                if bw and "speed_mbps" in bw:
                    rec = {
                        "server": server_ip, "server_port": server_port,
                        "remote_port": remote_port,
                        "latency_min_ms": round(latency[0], 2) if latency else None,
                        "latency_avg_ms": round(latency[1], 2) if latency else None,
                        **bw, "url": url,
                    }
                    self.results.append(rec)
                    return rec
                else:
                    keep = True
                    err = bw.get("error") if isinstance(bw, dict) else "下载失败"
                    self.failures.append({
                        "server": server_ip, "server_port": server_port,
                        "remote_port": remote_port,
                        "error": f"隧道成功但下载失败: {err}",
                        "config": cfg, "log": log,
                    })
                    print(f"    保留配置: {cfg}")
            finally:
                stop_frpc(proc, cfg, log, keep=keep)
        return None

    def _log(self, msg):
        with self._plock:
            print(msg, flush=True)

    def run(self):
        self._plock = threading.Lock()
        self.cache = ScanCache(self.args.cache, enabled=not self.args.no_cache)
        self.discovered = []  # (ip, server_port)

        print("=" * 64)
        print("FRP 智能端口选择 + 带宽/延迟测试")
        print("=" * 64)
        auth_desc = "token 认证" if self.args.token else "无认证"
        print(f"serverPort={self.args.server_port} | {auth_desc} | "
              f"缓存={'关' if self.args.no_cache else self.args.cache}")

        # 端口范围映射探测模式: 测一个 IP 的远程端口范围哪些能映射并访问
        if self.args.probe_range:
            return self._run_probe_range()

        # 仅发现模式: 只扫指定端口找可用 server IP, 不做带宽测试
        if self.args.discover_only:
            return self._run_discover_only()

        httpd, self.http_port = start_http_server()
        print(f"本地 HTTP 服务器: 127.0.0.1:{self.http_port} (共享 ~/, 文件 {self.args.file})")

        # 流水线: scan_q -> [top1000 扫描+选端口] -> test_q -> [串行测试]
        scan_q = queue.Queue()
        test_q = queue.Queue()

        workers = [threading.Thread(target=self._scan_worker,
                                    args=(scan_q, test_q), daemon=True)
                   for _ in range(self.args.scan_concurrency)]
        for w in workers:
            w.start()

        prod = threading.Thread(target=self._producer, args=(scan_q,), daemon=True)
        prod.start()

        def finisher():
            prod.join()
            for _ in workers:
                scan_q.put(None)
            for w in workers:
                w.join()
            test_q.put(None)

        fin = threading.Thread(target=finisher, daemon=True)
        fin.start()

        # 消费者(主线程): 串行测试, 与扫描/发现并行
        tested = 0
        try:
            while True:
                item = test_q.get()
                if item is None:
                    break
                ip, cp, cands = item
                tested += 1
                self._log(f"\n[测试 #{tested}] {ip}:{cp} -> 候选 {cands}")
                rec = self.test_one(ip, cp, cands)
                if rec:
                    self._log(f"  OK  远程端口 {rec['remote_port']} | "
                              f"延迟 {rec.get('latency_avg_ms')} ms | "
                              f"TTFB {rec.get('ttfb_ms')} ms | "
                              f"{rec['speed_mbps']:.2f} Mbps "
                              f"({rec['mb']:.1f} MB / {rec['seconds']}s)")
                else:
                    self._log("  FAIL 所有候选端口均失败")
        finally:
            fin.join()
            httpd.shutdown()
            self.cache.save()
            if self.discovered:
                self._save_discovered()
            self.save()
            self.summary()

    # ---- 生产者: 决定 server 来源并放入 scan_q ---- #
    def _producer(self, scan_q):
        sp = self.args.server_port
        if self.args.cidr:
            blocks = expand_targets(self.args.cidr)
            self._log(f"\n[发现] 扫描 {len(blocks)} 个网段的 {sp} 端口 "
                      f"(并发 {self.args.discover_concurrency}) ...")
            with ThreadPoolExecutor(max_workers=self.args.discover_concurrency) as ex:
                futs = {ex.submit(scan_open_port, blk, sp): blk for blk in blocks}
                for fut in as_completed(futs):
                    blk = futs[fut]
                    try:
                        hits = fut.result() or []
                    except Exception:
                        hits = []
                    for ip, port in hits:
                        self.discovered.append((ip, port))
                        self._log(f"  [发现] {ip}:{port} 开放 -> 进入扫描队列")
                        scan_q.put((ip, port, None))
        elif self.args.ip:
            db_port = get_port_for_ip(self.args.db, self.args.ip)
            cp = self.args.port or sp
            scan_q.put((self.args.ip, cp, db_port))
        else:
            raw = get_servers(self.args.db, limit=self.args.max_servers)
            self._log(f"从数据库载入 {len(raw)} 个待测 IP")
            for ip, db_port in raw:
                scan_q.put((ip, sp, db_port))

    # ---- 扫描 worker: top1000 扫描(带缓存) + 选端口 ---- #
    def _scan_worker(self, scan_q, test_q):
        while True:
            item = scan_q.get()
            if item is None:
                scan_q.task_done()
                break
            ip, cp, ep = item
            try:
                scan = nmap_scan_cached(
                    ip, self.cache, top_ports=self.args.top_ports,
                    rescan=self.args.rescan)
            except Exception:
                scan = {"up": False, "states": {}}
            exclude = [p for p in (cp, ep) if p]
            cands = choose_remote_ports(
                scan, max_candidates=self.args.remote_candidates,
                exclude_ports=exclude)
            tag = " [缓存]" if scan.get("from_cache") else ""
            if self.args.verbose:
                from collections import Counter
                dist = dict(Counter(scan.get("states", {}).values()))
                self._log(f"  [扫描]{tag} {ip:<16} up={scan.get('up')} "
                          f"端口={len(scan.get('scanned', []))} 分布={dist} 候选={cands}")
            if cands:
                if not self.args.verbose:
                    self._log(f"  [扫描完成]{tag} {ip:<16} 候选远程端口: {cands}")
                test_q.put((ip, cp, cands))
            else:
                with self._plock:
                    self.failures.append({
                        "server": ip, "server_port": cp,
                        "error": "nmap 未找到可用(closed/未占用)端口或主机不可达",
                    })
                print(f"  [扫描完成]{tag} {ip:<16} 无可用端口/不可达", flush=True)
            scan_q.task_done()

    # ---- 端口范围映射探测 ---- #
    def _run_probe_range(self):
        ip = self.args.ip
        if not ip:
            sys.exit("端口范围探测需要 --ip 指定目标 frps 服务器")
        sp = self.args.port or self.args.server_port
        start, end = parse_port_range(self.args.probe_range)
        ports = list(range(start, end + 1))

        httpd, self.http_port = start_http_server()
        print(f"本地 HTTP 服务器: 127.0.0.1:{self.http_port} (共享 ~/)")
        print(f"\n[端口范围探测] {ip}:{sp} | 远程端口 {start}-{end} "
              f"({len(ports)} 个) | 并发 {self.args.probe_concurrency}")
        print("-" * 64)

        probe_results = []

        def probe(port):
            proc, cfg, log, ok, msg = start_frpc_tunnel(
                ip, sp, self.http_port, port,
                self.args.token, self.args.user, wait=self.args.wait)
            keep = False
            rec = {"remote_port": port, "mapped": ok, "accessible": False}
            try:
                if not ok:
                    rec["error"] = msg
                else:
                    url = f"http://{ip}:{port}/{self.args.file}"
                    acc, code, info = verify_access(url)
                    rec["accessible"] = acc
                    rec["http_code"] = code
                    rec["url"] = url
                    if acc:
                        rec["rtt_ms"] = info.get("rtt_ms")
                    else:
                        rec["error"] = info if isinstance(info, str) else f"HTTP {code}"
            finally:
                # 探测会建大量隧道, 不保留每端口临时配置 (成功端口最后统一生成配置)
                stop_frpc(proc, cfg, log, keep=keep)

            if rec["mapped"] and rec["accessible"]:
                status = f"OK   映射+访问成功 (rtt {rec.get('rtt_ms')} ms)"
            elif rec["mapped"]:
                status = f"WARN 映射成功但访问失败: {rec.get('error')}"
            else:
                status = f"FAIL 映射失败: {rec.get('error')}"
            self._log(f"  端口 {port:<6} {status}")
            return rec

        with ThreadPoolExecutor(max_workers=self.args.probe_concurrency) as ex:
            for rec in ex.map(probe, ports):
                probe_results.append(rec)

        probe_results.sort(key=lambda r: r["remote_port"])
        ok_ports = [r["remote_port"] for r in probe_results
                    if r["mapped"] and r["accessible"]]
        mapped_only = [r["remote_port"] for r in probe_results
                       if r["mapped"] and not r["accessible"]]

        # 自动生成 frpc 配置文件 (取访问成功的端口, 按 rtt 由快到慢取前 N 个)
        cfg_path = None
        if ok_ports:
            best = sorted(
                (r for r in probe_results if r["mapped"] and r["accessible"]),
                key=lambda r: (r.get("rtt_ms") is None, r.get("rtt_ms", 1e9)))
            n = self.args.config_ports
            sel = [r["remote_port"] for r in best][:n] if n > 0 else \
                [r["remote_port"] for r in best]
            cfg_path = write_multiproxy_config(
                ip, sp, sel, token=self.args.token, user=self.args.user,
                local_port=self.args.local_port)

        out = os.path.join(BASE_DIR, f"probe_{ip}_{start}-{end}.json")
        with open(out, "w") as f:
            json.dump({
                "server": ip, "server_port": sp,
                "range": [start, end],
                "ok_ports": ok_ports,
                "mapped_but_inaccessible": mapped_only,
                "frpc_config": cfg_path,
                "details": probe_results,
            }, f, indent=2, ensure_ascii=False)

        httpd.shutdown()
        print("-" * 64)
        print(f"可映射且可访问的端口 ({len(ok_ports)}/{len(ports)}): {ok_ports}")
        if mapped_only:
            print(f"仅映射成功但访问失败: {mapped_only}")
        print(f"结果已保存: {out}")
        if cfg_path:
            print(f"frpc 配置已生成: {cfg_path}")

    # ---- 仅发现模式 ---- #
    def _run_discover_only(self):
        sp = self.args.server_port
        targets = self.args.cidr or self.args.ip
        if not targets:
            sys.exit("发现模式需要 --cidr 指定 IP 段")
        blocks = expand_targets(targets)
        print(f"\n[仅发现] 扫描 {len(blocks)} 个网段的 {sp} 端口 "
              f"(并发 {self.args.discover_concurrency}) ...")
        nmap_hits = []
        with ThreadPoolExecutor(max_workers=self.args.discover_concurrency) as ex:
            futs = {ex.submit(scan_open_port, blk, sp): blk for blk in blocks}
            for fut in as_completed(futs):
                try:
                    hits = fut.result() or []
                except Exception:
                    hits = []
                for ip, port in hits:
                    nmap_hits.append((ip, port))
                    print(f"  [nmap开放] {ip}:{port}", flush=True)
        nmap_hits = sorted(set(nmap_hits))

        if self.args.no_verify_discover:
            self.discovered = nmap_hits
            print(f"\n共发现 {len(self.discovered)} 个开放端口(未验证)")
        else:
            print(f"\n[验证] 对 {len(nmap_hits)} 个开放端口做 frpc 握手验证 "
                  f"(只保留真正 frps) ...")
            vlock = threading.Lock()

            def verify_one(item):
                ip, port = item
                ok, msg = verify_frps_login(ip, port, token=self.args.token,
                                            user=self.args.user)
                if ok:
                    with vlock:
                        self.discovered.append((ip, port))
                    print(f"  {ip}:{port} ✓ 真 frps", flush=True)
                else:
                    print(f"  {ip}:{port} ✗ {msg}", flush=True)

            with ThreadPoolExecutor(max_workers=self.args.discover_concurrency) as ex:
                list(ex.map(verify_one, nmap_hits))
            self.discovered = sorted(set(self.discovered))
            print(f"\nnmap开放 {len(nmap_hits)} 个, 真正 frps {len(self.discovered)} 个")
        if self.discovered:
            self._save_discovered()

    def _save_discovered(self):
        out = os.path.join(BASE_DIR, "discovered_servers.json")
        data = [{"server": ip, "port": port} for ip, port in self.discovered]
        with open(out, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"可用 server IP 已保存: {out} ({len(data)} 个)")
        if self.args.save_db:
            self._insert_discovered_db()

    def _insert_discovered_db(self):
        import sqlite3
        conn = sqlite3.connect(self.args.db)
        cur = conn.cursor()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        added = 0
        for ip, port in self.discovered:
            cur.execute("SELECT 1 FROM public_ips WHERE server=? AND port=?",
                        (ip, str(port)))
            if cur.fetchone():
                continue
            cur.execute(
                "INSERT INTO public_ips (server, port, type, is_active, "
                "description, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (ip, str(port), "FRP", 1, "discover 7000 扫描", now, now))
            added += 1
        conn.commit()
        conn.close()
        print(f"已写入数据库 public_ips: 新增 {added} 条")

    def save(self):
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           self.args.output)
        data = {
            "successful": sorted(self.results,
                                 key=lambda x: x["speed_mbps"], reverse=True),
            "failed": self.failures,
            "summary": {
                "success": len(self.results),
                "failed": len(self.failures),
            },
        }
        with open(out, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"\n结果已保存: {out}")

    def summary(self):
        print("\n" + "=" * 64)
        print("汇总")
        print("=" * 64)
        if self.results:
            rs = sorted(self.results, key=lambda x: x["speed_mbps"], reverse=True)
            print(f"{'服务器':<18}{'远程端口':<10}{'延迟(ms)':<12}{'速度(Mbps)':<14}{'下载(MB)':<10}")
            print("-" * 64)
            for r in rs:
                print(f"{r['server']:<18}{r['remote_port']:<10}"
                      f"{str(r['latency_avg_ms']):<12}{r['speed_mbps']:<14.2f}"
                      f"{r['mb']:<10.1f}")
            speeds = [r["speed_mbps"] for r in self.results]
            print("-" * 64)
            print(f"平均 {sum(speeds)/len(speeds):.2f} Mbps | "
                  f"最高 {max(speeds):.2f} | 最低 {min(speeds):.2f}")
        print(f"\n成功 {len(self.results)} | 失败 {len(self.failures)}")
        total = len(self.results) + len(self.failures)
        if total:
            print(f"成功率 {len(self.results)/total*100:.1f}%")
        print("=" * 64)


def main():
    p = argparse.ArgumentParser(description="FRP 智能端口选择 + 带宽/延迟测试")
    p.add_argument("--db", default="resources.db", help="数据库路径")
    p.add_argument("--ip", default=None, help="只测试单个 IP")
    p.add_argument("--port", type=int, default=None,
                   help="单 IP 模式下覆盖 frps 控制端口 (默认用 --server-port)")
    p.add_argument("--server-port", type=int, default=7000,
                   help="frps 控制端口 serverPort (默认 7000)")
    p.add_argument("--verbose", action="store_true", help="打印每个 IP 的端口扫描详情")
    p.add_argument("--file", default="51M", help="测试文件名 (位于 ~/)")
    p.add_argument("--max-servers", type=int, default=10,
                   help="最大测试服务器数 (默认 10, 设为 0 表示全部)")
    p.add_argument("--top-ports", type=int, default=1000, help="nmap 扫描端口数")
    p.add_argument("--remote-candidates", type=int, default=4,
                   help="每个服务器尝试的候选远程端口数")
    p.add_argument("--scan-concurrency", type=int, default=8, help="nmap top-ports 扫描并发数")
    p.add_argument("--duration", type=int, default=10, help="单次下载测速时长(秒)")
    p.add_argument("--wait", type=int, default=8, help="等待 frpc 代理建立超时(秒)")
    p.add_argument("--token", default=None, help="frps 认证 token (默认无认证)")
    p.add_argument("--user", default=None, help="frps 认证用户 (默认无)")
    p.add_argument("--output", default="port_bandwidth_results.json", help="结果文件")
    # IP 段发现 (扫指定端口找可用 server IP)
    p.add_argument("--cidr", default=None,
                   help="IP 段(CIDR/逗号分隔/nmap范围), 扫 --server-port 端口发现可用 server")
    p.add_argument("--discover-only", action="store_true",
                   help="仅发现可用 server IP(扫 server-port 端口), 不做带宽测试")
    p.add_argument("--discover-concurrency", type=int, default=4,
                   help="IP 段发现的并发网段数")
    p.add_argument("--save-db", action="store_true",
                   help="将发现的可用 IP 写入数据库 public_ips")
    p.add_argument("--no-verify-discover", action="store_true",
                   help="发现模式跳过 frpc 握手验证(仅 nmap, 可能含假 open)")
    # 端口范围映射探测 (配合 --ip)
    p.add_argument("--probe-range", default=None,
                   help="远程端口范围(如 2000-2010), 测试哪些端口能被 frpc 映射并访问")
    p.add_argument("--probe-concurrency", type=int, default=8,
                   help="端口范围探测的并发数")
    p.add_argument("--config-ports", type=int, default=20,
                   help="探测后自动生成 frpc 配置纳入的端口数(按 rtt 取最快, 0=全部)")
    p.add_argument("--local-port", type=int, default=None,
                   help="生成配置时各 proxy 的本地端口(默认 localPort=remotePort)")
    # 扫描结果缓存
    p.add_argument("--cache", default=CACHE_PATH, help="扫描结果缓存文件")
    p.add_argument("--no-cache", action="store_true", help="禁用扫描缓存")
    p.add_argument("--rescan", action="store_true", help="忽略缓存强制重新扫描")
    args = p.parse_args()

    if args.max_servers == 0:
        args.max_servers = None

    if not args.discover_only:
        if not os.path.exists(FRPC_BIN):
            sys.exit(f"未找到 frpc: {FRPC_BIN}")
        if not os.path.exists(os.path.expanduser(f"~/{args.file}")):
            print(f"警告: ~/{args.file} 不存在, 下载将返回 404")

    Tester(args).run()


if __name__ == "__main__":
    main()
