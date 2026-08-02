#!/usr/bin/env python3
"""file_manager 设备客户端 (device agent)。

* 启动后向服务器发起 ``/api/agent/register`` 报到。
* 在两个后台线程里持续:
    1. heartbeat: 每 ``--heartbeat-interval`` 秒上报一次 cpu/mem/disk/gpu/IP/uptime
    2. poll loop: 长轮询 ``/api/agent/poll`` 拿任务, 执行后回传 stdout/stderr/exit_code
* 支持的任务 kind:
    - ``exec``              远程执行 shell 命令
    - ``service_install``   下载服务器侧的 tar.gz 并解压, 然后 start
    - ``service_start``     在已解压目录下重新启动 run.sh
    - ``service_stop``      SIGTERM / SIGKILL 进程组
    - ``service_delete``    stop + 清理本地解压目录
    - ``service_log``       tail run.log 回传
    - ``service_update_diff``  下载新 tar.gz, 跟当前运行目录 diff (不改文件)
    - ``service_update_apply`` 用户确认后合入 diff + 重启服务
    - ``fs_list``           列目录 / stat
    - ``fs_delete``         删文件 / 目录
    - ``fs_mkdir``          mkdir -p
    - ``fs_pull``           读本地文件并 POST 到 server 的 blob cache (用户下载用)
    - ``fs_push``           从 server 的 blob cache 下载文件并写到本地 (用户上传用)
    - ``agent_self_update`` 拉新版本 tar.gz, 校验 + diff + 覆盖 + 重启自己

整个文件可单文件运行: ``python agent.py --server-url ... --device-token ...``。
不依赖第三方库就能工作 (psutil / requests 是可选, 都有标准库 fallback)。
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# 可选依赖: psutil 用于真实资源采集; requests 用于更友好的 HTTP
try:
    import psutil  # type: ignore
    HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore
    HAS_PSUTIL = False

try:
    import requests  # type: ignore
    HAS_REQUESTS = True
except ImportError:
    requests = None  # type: ignore
    HAS_REQUESTS = False


AGENT_VERSION = '0.3.0'

logger = logging.getLogger('device_agent')


class HttpUnauthorized(RuntimeError):
    """服务器返回 401; 上层捕获后会触发重 enroll。"""


# ---------------------------------------------------------------------------
# 简易 HTTP 客户端 (优先 requests, 回退到 urllib)
# ---------------------------------------------------------------------------

class HttpClient:
    """非常薄的封装: 仅暴露 ``json`` / ``download`` 两个方法。"""

    def __init__(self, base_url: str, token: str = '', *, timeout: float = 30.0,
                 verify_tls: bool = True):
        self.base_url = base_url.rstrip('/')
        self.token = token or ''
        self.timeout = timeout
        self.verify_tls = verify_tls
        self._session = requests.Session() if HAS_REQUESTS else None

    def set_token(self, token: str) -> None:
        """enroll 拿到 token 后回写; 后续所有请求都带上。"""
        self.token = token or ''

    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            'User-Agent': f'file-manager-device-agent/{AGENT_VERSION}',
        }
        if self.token:
            h['X-Device-Token'] = self.token
        if extra:
            h.update(extra)
        return h

    def request_json(self, method: str, path: str, *, payload: dict | None = None,
                     params: dict | None = None, timeout: float | None = None,
                     extra_headers: dict | None = None) -> dict:
        url = self.base_url + path
        if params:
            sep = '&' if '?' in url else '?'
            url = url + sep + urllib.parse.urlencode(params)
        timeout = timeout or self.timeout
        hdr = {'Content-Type': 'application/json'}
        if extra_headers:
            hdr.update(extra_headers)
        if self._session:
            kw = {'timeout': timeout, 'verify': self.verify_tls,
                  'headers': self._headers(hdr)}
            if payload is not None:
                kw['json'] = payload
            r = self._session.request(method, url, **kw)
            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.status_code == 401:
                raise HttpUnauthorized(data.get('error') or 'token 无效 (401)')
            if not r.ok:
                raise RuntimeError(f'{method} {path} → {r.status_code}: '
                                   f'{data.get("error", r.text)[:200]}')
            return data
        # urllib fallback
        body = None
        headers = self._headers(hdr)
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return json.loads(raw.decode('utf-8')) if raw else {}
                except json.JSONDecodeError:
                    return {}
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode('utf-8', errors='replace')
            try:
                j = json.loads(err_body)
                msg = j.get('error') or err_body
            except json.JSONDecodeError:
                msg = err_body
            if exc.code == 401:
                raise HttpUnauthorized(msg[:200])
            raise RuntimeError(f'{method} {path} → {exc.code}: {msg[:200]}')
        except urllib.error.URLError as exc:
            raise RuntimeError(f'{method} {path} 网络错误: {exc.reason}')

    def upload_file(self, path: str, src: str, *, timeout: float | None = None,
                    extra_headers: dict | None = None) -> dict:
        """把本地文件 ``src`` 以 ``application/octet-stream`` POST 到 ``path``。

        服务器端约定: 用 ``request.stream`` 流式落盘到 fs_tmp/<token>.bin。
        小文件 (<10MB) 也走这条路, 没必要再单独搞 base64 通道。
        """
        url = self.base_url + path
        timeout = timeout or 300.0
        headers = self._headers({'Content-Type': 'application/octet-stream'})
        if extra_headers:
            headers.update(extra_headers)
        if self._session:
            with open(src, 'rb') as fh:
                r = self._session.post(url, headers=headers, data=fh,
                                       timeout=timeout, verify=self.verify_tls)
            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.status_code == 401:
                raise HttpUnauthorized(data.get('error') or 'token 无效 (401)')
            if not r.ok:
                raise RuntimeError(f'POST {path} → {r.status_code}: '
                                   f'{(data.get("error") or r.text)[:200]}')
            return data
        # urllib fallback: 一次性读到内存里。我们已经在调用方限制文件大小
        with open(src, 'rb') as fh:
            body = fh.read()
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode('utf-8')) if raw else {}
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode('utf-8', errors='replace')
            try:
                j = json.loads(err_body); msg = j.get('error') or err_body
            except json.JSONDecodeError:
                msg = err_body
            if exc.code == 401:
                raise HttpUnauthorized(msg[:200])
            raise RuntimeError(f'POST {path} → {exc.code}: {msg[:200]}')
        except urllib.error.URLError as exc:
            raise RuntimeError(f'POST {path} 网络错误: {exc.reason}')

    def download(self, path: str, dest: str, *, timeout: float | None = None) -> int:
        url = self.base_url + path
        timeout = timeout or 120.0
        headers = self._headers()
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        bytes_written = 0
        if self._session:
            with self._session.get(url, headers=headers, stream=True,
                                   timeout=timeout, verify=self.verify_tls) as r:
                if not r.ok:
                    raise RuntimeError(f'GET {path} → {r.status_code}: {r.text[:200]}')
                with open(dest, 'wb') as fh:
                    for chunk in r.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            fh.write(chunk)
                            bytes_written += len(chunk)
            return bytes_written
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                with open(dest, 'wb') as fh:
                    while True:
                        chunk = resp.read(1024 * 64)
                        if not chunk:
                            break
                        fh.write(chunk)
                        bytes_written += len(chunk)
            return bytes_written
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'GET {path} → {exc.code}')


# ---------------------------------------------------------------------------
# 资源采集
# ---------------------------------------------------------------------------

# 模块级全局: agent 启动时由 main() 注入, collect_stats 读取
_FRPC_FALLBACK_CONFIG: str | None = None


def set_frpc_fallback_config(path: str | None) -> None:
    """主进程启动时调用一次, 把 ``--frpc-config`` 注入给 collect_frpc_tunnels。"""
    global _FRPC_FALLBACK_CONFIG  # noqa: PLW0603
    _FRPC_FALLBACK_CONFIG = (path or '').strip() or None


def collect_stats() -> dict:
    """采集 cpu/memory/disk/gpu/uptime/net 等; psutil 不可用则降级。

    返回结构 (字段都是可选, 没有的不出现):

    .. code-block:: python

        {
          'ts': int,
          'cpu': { 'percent', 'count_logical', 'count_physical',
                   'model', 'freq_current_mhz', 'freq_max_mhz',
                   'per_core_percent': [...] },
          'memory': { 'total', 'used', 'available', 'percent',
                      'swap_total', 'swap_used', 'swap_percent' },
          'disk':   { 'items': [ {'path', 'device', 'fstype', 'opts',
                                  'total', 'used', 'free', 'percent', 'model'}, ... ] },
          'gpus':   [ {'index', 'name', 'driver', 'util_percent',
                       'mem_total_mib', 'mem_used_mib', 'temp_c', 'power_w'} ],
          'net':    { 'interfaces': [ {'name', 'mac', 'ipv4': [...], 'ipv6': [...],
                                       'speed_mbps', 'is_up'} ] },
          'load_avg': {'1m', '5m', '15m'},
          'uptime_seconds': int,
        }
    """
    stats = {'ts': int(time.time())}

    if HAS_PSUTIL:
        stats['cpu'] = _collect_cpu_psutil()
        try:
            vm = psutil.virtual_memory(); sw = psutil.swap_memory()
            stats['memory'] = {
                'total': vm.total, 'used': vm.used, 'available': vm.available,
                'percent': vm.percent,
                'swap_total': sw.total, 'swap_used': sw.used, 'swap_percent': sw.percent,
            }
        except Exception as exc:  # noqa: BLE001
            stats['memory'] = {'error': str(exc)}
        stats['disk'] = _collect_disk_psutil()
        try:
            stats['uptime_seconds'] = int(time.time() - psutil.boot_time())
        except Exception:  # noqa: BLE001
            pass
        try:
            la = os.getloadavg(); stats['load_avg'] = {'1m': la[0], '5m': la[1], '15m': la[2]}
        except (OSError, AttributeError):
            pass
        stats['net'] = _collect_net_psutil()
    else:
        stats['cpu'] = _collect_cpu_fallback()
        stats['memory'] = _collect_memory_fallback()
        stats['disk'] = _collect_disk_fallback()
        try:
            la = os.getloadavg(); stats['load_avg'] = {'1m': la[0], '5m': la[1], '15m': la[2]}
        except (OSError, AttributeError):
            pass
        stats['net'] = {'interfaces': []}

    # 用 lsblk 给磁盘条目挂上型号 (psutil 不暴露这个)
    _enrich_disk_models(stats.get('disk') or {})

    # GPU: nvidia-smi 给一份, 其他厂商先不管 (用户有需求再加)
    gpus = _read_nvidia_smi()
    if gpus is not None:
        stats['gpus'] = gpus

    # frpc 隧道: 扫本机 frpc 进程 + 解析其 -c 指向的 toml; 失败/无 frpc 时返回空 list
    try:
        tunnels = collect_frpc_tunnels(fallback_config_path=_FRPC_FALLBACK_CONFIG)
        if tunnels:
            stats['frpc_tunnels'] = tunnels
    except Exception as exc:  # noqa: BLE001 — 任何采集异常都不能影响心跳
        stats['frpc_tunnels'] = []
        stats['frpc_error'] = str(exc)

    return stats


# ---------------------------------------------------------------------------
# frpc 隧道采集: 让 server 能在 /devices/<id> 详情页直接展示
# ---------------------------------------------------------------------------

_FRPC_CONFIG_FLAGS = ('-c', '--config', '-config')


def collect_frpc_tunnels(fallback_config_path: str | None = None) -> list[dict]:
    """枚举本设备上所有 frpc 进程, 把每个进程的 toml 配置 + proxies 都列出来。

    Args:
        fallback_config_path: 当 frpc 用相对路径 ``-c`` 且我们读不到其 cwd
            (典型场景: frpc 跑在 root, agent 是普通用户) 时, 作为 toml 的兜底绝对路径。
            由 ``--frpc-config`` / ``FM_AGENT_FRPC_CONFIG`` 注入。

    返回结构 (前端可以直接渲染):

    .. code-block:: python

        [
          {
            "pid": 12345,
            "config_path": "/etc/frpc.toml",
            "server_addr": "vps.example.com",
            "server_port": 7000,
            "error": null,        # 解析失败时给原因, proxies 为空
            "proxies": [
              {"name": "ssh", "type": "tcp",
               "local_port": 22, "remote_port": 22001},
              ...
            ]
          }, ...
        ]

    上层在心跳里把它扔进 ``stats['frpc_tunnels']``。**永远不抛**, 失败时返回 []。

    每个 proxy 还会带上端口占用信息: ``in_use`` (本机是否有进程在 LISTEN 该
    ``local_port``) 与 ``occupied_by`` ({pid, name, cmdline})。这样 Web 端能直接
    看出哪个 frpc 端口已被服务占用、被谁占用。
    """
    out: list[dict] = []
    seen_pids: set[int] = set()
    # 先扫一遍本机 LISTEN 端口 → 占用进程, 给每个 proxy 的 local_port 标占用情况
    listen_map = _scan_listening_ports()
    for pid, cmdline in _list_frpc_candidates():
        if pid in seen_pids:
            continue
        seen_pids.add(pid)
        raw_cfg_path = _extract_frpc_config_path(cmdline)
        # frpc 经常被这样启动: ``cd /opt/frpc && ./frpc -c frpc.toml``
        # 此时 ``-c`` 后是相对路径, 必须拼上 **frpc 进程自己的 cwd** (/proc/<pid>/cwd)
        # 而不是 agent.py 进程的 cwd, 否则解析必然 FileNotFoundError。
        proc_cwd = _get_process_cwd(pid)
        cfg_path = _resolve_frpc_config_path(raw_cfg_path, proc_cwd)
        # 走兜底: 用户在 agent 启动时指定了 --frpc-config
        used_fallback = False
        if (cfg_path is None or not os.path.isfile(cfg_path)) and fallback_config_path:
            if os.path.isfile(fallback_config_path):
                cfg_path = fallback_config_path
                used_fallback = True
        item: dict = {
            'pid': pid,
            'cmdline': list(cmdline),
            'config_path': cfg_path,
            'cwd': proc_cwd,
            'used_fallback_config': used_fallback,
            'server_addr': None,
            'server_port': None,
            'proxies': [],
            'error': None,
        }
        if not raw_cfg_path:
            item['error'] = '命令行未指定 -c <config>; 可以给 agent 加 --frpc-config /abs/path/to/frpc.toml 兜底'
            out.append(item)
            continue
        if cfg_path is None:
            # 相对路径 + 拿不到 cwd + 也没传 fallback
            item['error'] = (
                f"-c '{raw_cfg_path}' 是相对路径, 但读不到 /proc/{pid}/cwd (通常是 frpc 跑在 root 而 agent 不是 root)。"
                " 三选一即可解决: "
                "(1) frpc 启动时换成绝对路径 ./frpc -c $(pwd)/frpc.toml; "
                "(2) 用 root 跑 agent (sudo ...); "
                "(3) 给 agent 加 --frpc-config /abs/path/to/frpc.toml 或环境变量 FM_AGENT_FRPC_CONFIG。"
            )
            out.append(item)
            continue
        try:
            parsed = _parse_frpc_toml(cfg_path)
            item.update(parsed)
        except (FileNotFoundError, PermissionError, ValueError, OSError) as exc:
            item['error'] = f'{type(exc).__name__}: {exc}'
        _annotate_proxy_port_usage(item.get('proxies') or [], listen_map)
        out.append(item)
    return out


def _annotate_proxy_port_usage(proxies: list[dict], listen_map: dict[int, dict]) -> None:
    """给每个 proxy 补 ``in_use`` / ``occupied_by`` 字段 (原地修改)。"""
    for p in proxies:
        try:
            lp = int(p.get('local_port'))
        except (TypeError, ValueError):
            p['in_use'] = None
            p['occupied_by'] = None
            continue
        occ = listen_map.get(lp)
        p['in_use'] = bool(occ)
        p['occupied_by'] = occ


def _scan_listening_ports() -> dict[int, dict]:
    """扫本机所有 LISTEN 的 TCP 端口 → ``{port: {pid, name, cmdline}}``。

    优先 psutil (能拿到 pid / 进程名); 拿不到 pid 时 (非 root) 至少把端口标成被占用,
    ``occupied_by`` 给一个 ``{pid: None}`` 的占位。永远不抛, 失败返回 {}。
    """
    out: dict[int, dict] = {}
    if HAS_PSUTIL:
        try:
            conns = psutil.net_connections(kind='inet')
        except (psutil.AccessDenied, OSError, Exception):  # noqa: BLE001
            conns = []
        # pid → (name, cmdline) 缓存, 避免重复查询
        pid_cache: dict[int, dict] = {}
        for c in conns:
            try:
                if c.status != psutil.CONN_LISTEN:
                    continue
                laddr = c.laddr
                port = int(getattr(laddr, 'port', 0) or 0)
                if port <= 0:
                    continue
                pid = c.pid
                if port in out and out[port].get('pid'):
                    continue  # 已记录到带 pid 的占用
                info: dict = {'pid': pid, 'name': None, 'cmdline': None}
                if pid and pid not in pid_cache:
                    try:
                        proc = psutil.Process(pid)
                        pid_cache[pid] = {
                            'name': proc.name(),
                            'cmdline': ' '.join(proc.cmdline())[:300] or None,
                        }
                    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                        pid_cache[pid] = {'name': None, 'cmdline': None}
                if pid and pid in pid_cache:
                    info.update(pid_cache[pid])
                out[port] = info
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                continue
            except Exception:  # noqa: BLE001
                continue
        return out
    # psutil 没装 → 退到 ``ss -ltnp`` / ``netstat`` 解析 (Linux/macOS)
    return _scan_listening_ports_fallback()


def _scan_listening_ports_fallback() -> dict[int, dict]:
    """没有 psutil 时用 ``ss`` 解析 LISTEN 端口。拿不到就返回 {}。"""
    out: dict[int, dict] = {}
    try:
        import subprocess
        proc = subprocess.run(
            ['ss', '-ltnp'], capture_output=True, text=True, timeout=2.0, check=False,
        )
        if proc.returncode != 0:
            return out
    except (OSError, Exception):  # noqa: BLE001
        return out
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]  # 形如 0.0.0.0:22 / [::]:22 / *:8080
        try:
            port = int(local.rsplit(':', 1)[1])
        except (ValueError, IndexError):
            continue
        pid = None
        name = None
        # 末段形如 users:(("sshd",pid=1234,fd=3))
        m = re.search(r'\(\("([^"]+)",pid=(\d+)', line)
        if m:
            name = m.group(1)
            try:
                pid = int(m.group(2))
            except ValueError:
                pid = None
        if port not in out or (pid and not out[port].get('pid')):
            out[port] = {'pid': pid, 'name': name, 'cmdline': None}
    return out


def _resolve_frpc_config_path(raw: str | None, proc_cwd: str | None) -> str | None:
    """把 ``-c <path>`` 抽到的字符串解析成 frpc 真正在用的**绝对路径**。

    - 已经是绝对路径 → 原样返回
    - 相对路径 + 拿到了 frpc 进程的 cwd → ``os.path.join(cwd, raw)``
    - 相对路径 + 拿不到 cwd → ``None`` (上层把这个当作 "无法解析" 给错误提示)
    - raw 为空 → ``None``
    """
    if not raw:
        return None
    if os.path.isabs(raw):
        return raw
    if not proc_cwd:
        return None
    return os.path.normpath(os.path.join(proc_cwd, raw))


def _get_process_cwd(pid: int) -> str | None:
    """读 ``/proc/<pid>/cwd`` (Linux) 或用 psutil。读不到返回 None。

    需要满足两个权限条件之一: (1) 进程跟 agent 同 uid; (2) agent 是 root。
    """
    if HAS_PSUTIL:
        try:
            return psutil.Process(int(pid)).cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None
        except Exception:  # noqa: BLE001
            return None
    # psutil 没装 → Linux 用 /proc, macOS / Windows 退化
    proc_link = f'/proc/{int(pid)}/cwd'
    if os.path.exists(proc_link):
        try:
            return os.readlink(proc_link)
        except (OSError, PermissionError):
            return None
    return None


def _extract_frpc_config_path(cmdline: list[str]) -> str | None:
    """从 ``frpc -c /etc/frpc.toml`` 这种命令行里抽出 ``-c`` 后面的路径。

    兼容 ``-c x`` / ``--config x`` / ``--config=x`` / ``-c=x`` 4 种写法。
    """
    if not cmdline:
        return None
    n = len(cmdline)
    for i, tok in enumerate(cmdline):
        for flag in _FRPC_CONFIG_FLAGS:
            if tok == flag and i + 1 < n:
                return cmdline[i + 1]
            prefix = flag + '='
            if tok.startswith(prefix) and len(tok) > len(prefix):
                return tok[len(prefix):]
    return None


def _list_frpc_candidates() -> list:
    """``[(pid, cmdline_argv), ...]``, 只筛 ``frpc`` (不会带上 frps / frpcheck)。"""
    out = []
    if HAS_PSUTIL:
        try:
            for p in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = p.info.get('cmdline') or []
                    if not cmdline:
                        continue
                    if _is_frpc_cmdline(cmdline, p.info.get('name') or ''):
                        out.append((int(p.info['pid']), list(cmdline)))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                except Exception:  # noqa: BLE001
                    continue
            return out
        except Exception:  # noqa: BLE001
            return out

    # psutil 没装 → ps fallback (Linux/macOS); Windows 上没有 ps, 拿不到就空着
    try:
        import subprocess
        proc = subprocess.run(
            ['ps', '-eo', 'pid=,args='],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
        if proc.returncode != 0:
            return out
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid_str, args = line.split(None, 1)
                pid = int(pid_str)
            except ValueError:
                continue
            argv = args.split()
            if not argv:
                continue
            if _is_frpc_cmdline(argv, os.path.basename(argv[0])):
                out.append((pid, argv))
    except (OSError, Exception):  # noqa: BLE001
        pass
    return out


def _is_frpc_cmdline(cmdline: list, name_hint: str = '') -> bool:
    """严格区分 frpc 与 frps: 必须 argv[0] basename == 'frpc' (或 frpc.exe)。"""
    if not cmdline:
        return False
    name_hint = (name_hint or '').strip().lower()
    if name_hint in ('frpc', 'frpc.exe'):
        return True
    base = os.path.basename(cmdline[0] or '').lower()
    if base in ('frpc', 'frpc.exe'):
        return True
    return any(s.lower().endswith('/frpc') for s in cmdline[:2])


def _parse_frpc_toml(path: str) -> dict:
    """解析 ``frpc.toml``。Python 3.11+ 用 ``tomllib``; 3.10 退回到极简 ini 风格解析。

    返回:
        ``{"server_addr": ..., "server_port": ..., "proxies": [...]}``
    任何格式异常都抛 ``ValueError``。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f'frpc 配置不存在: {path}')
    try:
        import tomllib
    except ImportError:
        return _parse_frpc_toml_fallback(path)
    with open(path, 'rb') as fh:
        data = tomllib.load(fh)
    server_addr = data.get('serverAddr') or data.get('server_addr')
    server_port = data.get('serverPort') or data.get('server_port')
    raw_proxies = data.get('proxies') or []
    if not isinstance(raw_proxies, list):
        raise ValueError('frpc.toml 中 proxies 字段必须为数组')
    proxies = []
    for p in raw_proxies:
        if not isinstance(p, dict):
            continue
        name = (p.get('name') or '').strip()
        lp = p.get('localPort') or p.get('local_port')
        rp = p.get('remotePort') or p.get('remote_port')
        if not name or not lp or not rp:
            continue
        proxies.append({
            'name': name,
            'type': str(p.get('type') or '').strip() or None,
            'local_port': int(lp),
            'remote_port': int(rp),
        })
    return {
        'server_addr': str(server_addr) if server_addr else None,
        'server_port': int(server_port) if server_port else None,
        'proxies': proxies,
    }


def _parse_frpc_toml_fallback(path: str) -> dict:
    """Python 3.10 (没有 tomllib) 时的兜底: 用正则抓必要字段, 仅识别新版 array-of-tables 写法。"""
    import re

    text_lines: list[str] = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            text_lines = fh.readlines()
    except OSError as exc:
        raise ValueError(f'读 frpc 配置失败: {exc}') from exc
    server_addr = None
    server_port = None
    proxies: list[dict] = []
    cur: dict | None = None
    for raw in text_lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line == '[[proxies]]':
            if cur:
                proxies.append(cur)
            cur = {}
            continue
        if line.startswith('[') and line.endswith(']') and line != '[[proxies]]':
            if cur:
                proxies.append(cur)
                cur = None
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$', line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip().strip(',')
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        if cur is not None:
            if key in ('localPort', 'local_port'):
                try:
                    cur['local_port'] = int(val)
                except ValueError:
                    pass
            elif key in ('remotePort', 'remote_port'):
                try:
                    cur['remote_port'] = int(val)
                except ValueError:
                    pass
            elif key == 'name':
                cur['name'] = val
            elif key == 'type':
                cur['type'] = val or None
        else:
            if key in ('serverAddr', 'server_addr'):
                server_addr = val
            elif key in ('serverPort', 'server_port'):
                try:
                    server_port = int(val)
                except ValueError:
                    pass
    if cur:
        proxies.append(cur)
    proxies = [p for p in proxies if p.get('name') and p.get('local_port') and p.get('remote_port')]
    for p in proxies:
        p.setdefault('type', None)
    return {
        'server_addr': server_addr,
        'server_port': server_port,
        'proxies': proxies,
    }


def _collect_cpu_psutil() -> dict:
    out: dict = {}
    try:
        out['percent'] = psutil.cpu_percent(interval=None)
        out['count_logical'] = psutil.cpu_count(logical=True) or 0
        out['count_physical'] = psutil.cpu_count(logical=False) or 0
    except Exception as exc:  # noqa: BLE001
        out['error'] = str(exc)
    try:
        # interval=None 第一次调用都是 0, 之后才有数据; per-core 数据用于详情页的小条
        per = psutil.cpu_percent(percpu=True, interval=None)
        if per:
            out['per_core_percent'] = list(per)
    except Exception:  # noqa: BLE001
        pass
    try:
        f = psutil.cpu_freq()
        if f:
            out['freq_current_mhz'] = round(f.current, 1)
            if f.max:
                out['freq_max_mhz'] = round(f.max, 1)
            if f.min:
                out['freq_min_mhz'] = round(f.min, 1)
    except Exception:  # noqa: BLE001
        pass
    model = _read_cpu_model()
    if model:
        out['model'] = model
    try:
        st = psutil.cpu_stats()
        out['ctx_switches'] = st.ctx_switches
        out['interrupts'] = st.interrupts
    except Exception:  # noqa: BLE001
        pass
    return out


def _read_cpu_model() -> str | None:
    """Linux 从 /proc/cpuinfo 抓 model name; macOS 用 sysctl; 其他平台 None。"""
    if platform.system() == 'Linux' and os.path.isfile('/proc/cpuinfo'):
        try:
            with open('/proc/cpuinfo', 'r', encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    if ':' in line:
                        k, _, v = line.partition(':')
                        k = k.strip()
                        if k in ('model name', 'Hardware', 'cpu model'):
                            return v.strip()
        except OSError:
            return None
    if platform.system() == 'Darwin':
        try:
            r = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'],
                               capture_output=True, text=True, timeout=1.0, check=False)
            if r.returncode == 0:
                return r.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
    # 兜底用 platform.processor()
    p = (platform.processor() or '').strip()
    return p or None


def _collect_cpu_fallback() -> dict:
    out: dict = {'count_logical': os.cpu_count() or 0}
    model = _read_cpu_model()
    if model:
        out['model'] = model
    # 读 /proc/loadavg 估个粗 percent (没有 psutil 时只能给这种近似)
    return out


def _collect_memory_fallback() -> dict:
    if platform.system() == 'Linux' and os.path.isfile('/proc/meminfo'):
        info: dict = {}
        try:
            with open('/proc/meminfo', 'r', encoding='utf-8') as fh:
                for line in fh:
                    k, _, v = line.partition(':')
                    v = v.strip().split()
                    if len(v) >= 1 and v[0].isdigit():
                        info[k.strip()] = int(v[0]) * 1024  # kB → B
        except OSError:
            return {}
        total = info.get('MemTotal', 0)
        avail = info.get('MemAvailable', info.get('MemFree', 0))
        used = max(0, total - avail)
        pct = round(used / total * 100, 1) if total else 0.0
        return {
            'total': total, 'used': used, 'available': avail, 'percent': pct,
            'swap_total': info.get('SwapTotal', 0),
            'swap_used': max(0, info.get('SwapTotal', 0) - info.get('SwapFree', 0)),
            'swap_percent': round(
                ((info.get('SwapTotal', 0) - info.get('SwapFree', 0))
                 / info.get('SwapTotal', 1)) * 100, 1
            ) if info.get('SwapTotal') else 0.0,
        }
    return {}


def _collect_disk_psutil() -> dict:
    items = []
    seen: set[str] = set()
    try:
        # all=False 已经过滤掉一些虚拟 fs; 但我们再叠加一个黑名单避免噪声
        for part in psutil.disk_partitions(all=False):
            if part.mountpoint in seen:
                continue
            if (part.fstype or '').lower() in ('squashfs', 'overlay', 'tmpfs', 'devtmpfs'):
                continue
            seen.add(part.mountpoint)
            try:
                du = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            items.append({
                'path': part.mountpoint,
                'device': part.device,
                'fstype': part.fstype,
                'opts': part.opts,
                'total': du.total, 'used': du.used, 'free': du.free, 'percent': du.percent,
            })
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}
    # 限制到 16 条; 一般机器够用了, 再多前端也展示不下
    return {'items': items[:16]}


def _collect_disk_fallback() -> dict:
    try:
        du = shutil.disk_usage('/')
        return {'items': [{
            'path': '/', 'device': '', 'fstype': '',
            'total': du.total, 'used': du.used, 'free': du.free,
            'percent': round(du.used / du.total * 100, 1) if du.total else 0.0,
        }]}
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


def _enrich_disk_models(disk_block: dict) -> None:
    """用 ``lsblk -dno NAME,MODEL,ROTA,SIZE`` 给每个条目挂 model / rota / disk_size。

    Linux 专属; 失败时静默跳过。这里查一次然后按 device prefix 匹配, 避免逐条 lsblk。
    """
    items = disk_block.get('items') if isinstance(disk_block, dict) else None
    if not items or platform.system() != 'Linux':
        return
    if not shutil.which('lsblk'):
        return
    try:
        r = subprocess.run(
            ['lsblk', '-dno', 'NAME,MODEL,ROTA,SIZE,TYPE'],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if r.returncode != 0:
        return
    # NAME 列在第一个; 后面的列宽不定, 用空格切再合并 MODEL
    name_info: dict[str, dict] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        # 最后两列是 SIZE TYPE (容量字符串如 477G), MODEL 在中间, 名字在第一个
        nm = parts[0]
        ts = parts[-1]
        sz = parts[-2]
        rota = parts[-3]
        model = ' '.join(parts[1:-3]).strip() or None
        name_info[nm] = {'model': model, 'rota': rota == '1', 'size_text': sz, 'type': ts}
    for it in items:
        dev = (it.get('device') or '').split('/')[-1]
        # /dev/nvme0n1p2 → 父盘 nvme0n1
        parent = dev
        if dev.startswith('nvme'):
            # nvme0n1p2 → nvme0n1; nvme0n1 → nvme0n1
            import re as _re
            m = _re.match(r'^(nvme\d+n\d+)', dev)
            if m:
                parent = m.group(1)
        else:
            # sda1 → sda
            parent = dev.rstrip('0123456789') or dev
        info = name_info.get(parent) or name_info.get(dev)
        if info:
            if info.get('model'):
                it['model'] = info['model']
            it['rotational'] = info.get('rota')
            it['disk_size_text'] = info.get('size_text')


def _collect_net_psutil() -> dict:
    interfaces = []
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}
    for name, addr_list in addrs.items():
        ipv4, ipv6, mac = [], [], None
        for a in addr_list:
            fam = a.family
            # AF_INET=2, AF_INET6=10, AF_PACKET=17 (Linux), AF_LINK=18 (macOS)
            if fam == socket.AF_INET:
                ipv4.append(a.address)
            elif fam == socket.AF_INET6:
                # 去掉 fe80::xxx%eth0 这种链路本地后缀
                ipv6.append(a.address.split('%')[0])
            elif hasattr(socket, 'AF_PACKET') and fam == socket.AF_PACKET:
                mac = a.address
            elif getattr(a, 'family', None) and str(a.family).endswith('AF_LINK'):
                mac = a.address
        st = stats.get(name)
        # 过滤掉纯 loopback + 没有任何 ipv4/ipv6 的 (减噪)
        if name == 'lo' and not ipv4 and not ipv6:
            continue
        interfaces.append({
            'name': name,
            'mac': mac,
            'ipv4': ipv4,
            'ipv6': ipv6[:3],
            'is_up': bool(st.isup) if st else None,
            'speed_mbps': st.speed if st and st.speed > 0 else None,
            'mtu': st.mtu if st else None,
        })
    return {'interfaces': interfaces[:16]}


def _read_nvidia_smi() -> list | None:
    smi = shutil.which('nvidia-smi')
    if not smi:
        return None
    try:
        proc = subprocess.run(
            [smi,
             '--query-gpu=index,name,driver_version,utilization.gpu,memory.total,'
             'memory.used,memory.free,temperature.gpu,power.draw,power.limit,'
             'fan.speed,clocks.current.sm,clocks.current.memory',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    items = []

    def _f(s, default=None):
        s = (s or '').strip()
        if s in ('', '[Not Supported]', '[N/A]', 'N/A'):
            return default
        try:
            return float(s)
        except ValueError:
            return default

    for line in proc.stdout.splitlines():
        parts = [s.strip() for s in line.split(',')]
        if len(parts) < 7:
            continue
        try:
            items.append({
                'index': int(parts[0]),
                'name': parts[1],
                'driver': parts[2] or None,
                'util_percent': _f(parts[3]),
                'mem_total_mib': _f(parts[4]),
                'mem_used_mib': _f(parts[5]),
                'mem_free_mib': _f(parts[6]) if len(parts) > 6 else None,
                'temp_c': _f(parts[7]) if len(parts) > 7 else None,
                'power_w': _f(parts[8]) if len(parts) > 8 else None,
                'power_limit_w': _f(parts[9]) if len(parts) > 9 else None,
                'fan_percent': _f(parts[10]) if len(parts) > 10 else None,
                'sm_clock_mhz': _f(parts[11]) if len(parts) > 11 else None,
                'mem_clock_mhz': _f(parts[12]) if len(parts) > 12 else None,
            })
        except ValueError:
            continue
    return items


def detect_local_ip() -> str | None:
    """常用 hack: 用 UDP socket 连一下 8.8.8.8, 不真发包, 拿到本地出口 IP。

    沙箱 / 容器环境可能连 ``socket.socket(SOCK_DGRAM)`` 都会被拒绝 (例如
    seccomp 限制了 ``socket(2)``), 这里把构造也包进 try, 让 detect 始终非致命。
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def detect_public_ip(timeout: float = 3.0) -> str | None:
    """best-effort 查外网 IP; 静默失败不阻塞主流程。"""
    endpoints = [
        'https://api.ipify.org', 'https://ifconfig.me/ip', 'https://ipv4.icanhazip.com',
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'agent/' + AGENT_VERSION})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ip = r.read().decode('utf-8').strip()
                if ip:
                    return ip
        except Exception:
            continue
    return None


def build_heartbeat_payload(state: 'AgentState', *, query_public_ip: bool = False) -> dict:
    stats = collect_stats()
    # 系统信息也合并到 stats 里, 这样设备详情页一份 JSON 就能取到
    stats['system'] = _collect_system_info()
    payload = {
        'hostname': socket.gethostname(),
        'os_name': platform.system(),
        'os_version': _better_os_version(),
        'arch': platform.machine(),
        'agent_version': AGENT_VERSION,
        'local_ip': detect_local_ip(),
        'stats': stats,
        'device_uid': state.device_uid,
    }
    if query_public_ip or state.public_ip is None:
        ip = detect_public_ip()
        if ip:
            state.public_ip = ip
    if state.public_ip:
        payload['public_ip'] = state.public_ip
    if state.tunnel_info:
        payload['tunnel_info'] = state.tunnel_info
    return payload


def _collect_system_info() -> dict:
    """汇总不会经常变的硬件 / 系统信息, 给前端的"系统"卡片用。"""
    info = {
        'hostname': socket.gethostname(),
        'os_name': platform.system(),
        'os_release': platform.release(),
        'os_version_full': _better_os_version(),
        'arch': platform.machine(),
        'platform': platform.platform(),
        'python_version': platform.python_version(),
        'agent_version': AGENT_VERSION,
        'agent_pid': os.getpid(),
    }
    # Linux 上读 /etc/os-release 拿 PRETTY_NAME / VERSION_ID
    if platform.system() == 'Linux' and os.path.isfile('/etc/os-release'):
        try:
            with open('/etc/os-release', 'r', encoding='utf-8') as fh:
                for line in fh:
                    if '=' in line:
                        k, _, v = line.partition('=')
                        info[f'distro_{k.strip().lower()}'] = v.strip().strip('"')
        except OSError:
            pass
    # 内核版本 / 主板信息 (最佳努力)
    try:
        if shutil.which('uname'):
            r = subprocess.run(['uname', '-a'], capture_output=True, text=True,
                               timeout=1.0, check=False)
            if r.returncode == 0:
                info['uname'] = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    if HAS_PSUTIL:
        try:
            info['boot_time'] = int(psutil.boot_time())
        except Exception:  # noqa: BLE001
            pass
        try:
            users = psutil.users()
            info['active_users'] = [{'name': u.name, 'host': u.host, 'started': int(u.started)}
                                    for u in users[:10]]
        except Exception:  # noqa: BLE001
            pass
    return info


def _better_os_version() -> str:
    """优先用 ``/etc/os-release`` 的 ``PRETTY_NAME``, 否则回退到 ``platform.release()``。

    这样 Web UI 能直接看到 "Ubuntu 22.04 LTS" 而不是 "5.15.0-91-generic"。
    """
    if platform.system() == 'Linux' and os.path.isfile('/etc/os-release'):
        try:
            with open('/etc/os-release', 'r', encoding='utf-8') as fh:
                for line in fh:
                    if line.startswith('PRETTY_NAME='):
                        return line.split('=', 1)[1].strip().strip('"')
        except OSError:
            pass
    if platform.system() == 'Darwin':
        ver = platform.mac_ver()[0]
        if ver:
            return f'macOS {ver}'
    return platform.release()


# ---------------------------------------------------------------------------
# 服务进程管理 (设备本地)
# ---------------------------------------------------------------------------

class LocalService:
    """每个 service_id 一份元数据 + pid 跟踪。"""

    def __init__(self, service_id: int, name: str, root_dir: str):
        self.service_id = service_id
        self.name = name
        self.root = root_dir       # 解压根目录 (含 run.sh)
        self.archive_path = os.path.join(root_dir, '_archive.tar.gz')
        self.work_dir = os.path.join(root_dir, 'work')
        self.log_path = os.path.join(root_dir, 'run.log')
        self.pid_path = os.path.join(root_dir, 'run.pid')

    @property
    def pid(self) -> int | None:
        if not os.path.isfile(self.pid_path):
            return None
        try:
            with open(self.pid_path, 'r', encoding='utf-8') as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return None

    def write_pid(self, pid: int) -> None:
        with open(self.pid_path, 'w', encoding='utf-8') as fh:
            fh.write(str(pid))

    def clear_pid(self) -> None:
        try:
            os.remove(self.pid_path)
        except OSError:
            pass

    def is_alive(self) -> bool:
        pid = self.pid
        if not pid or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False


SERVICES: dict[int, LocalService] = {}
SERVICES_LOCK = threading.Lock()


def get_local_service(state: 'AgentState', service_id: int, name: str) -> LocalService:
    with SERVICES_LOCK:
        s = SERVICES.get(service_id)
        if s is None:
            root = os.path.join(state.workdir, 'services', str(service_id))
            os.makedirs(root, exist_ok=True)
            s = LocalService(service_id, name, root)
            SERVICES[service_id] = s
        return s


def safe_extract_tarball(archive_path: str, target_dir: str) -> str:
    """与服务器 ``service_manager_service.safe_extract_tarball`` 同语义的设备侧实现。"""
    os.makedirs(target_dir, exist_ok=True)
    target_real = os.path.realpath(target_dir)
    if not tarfile.is_tarfile(archive_path):
        raise ValueError('归档不是合法 tar(.gz)')
    extracted_top: set[str] = set()
    with tarfile.open(archive_path, 'r:*') as tf:
        for member in tf.getmembers():
            if member.name.startswith('/') or '..' in member.name.split('/'):
                raise ValueError(f'非法路径: {member.name}')
            dest = os.path.realpath(os.path.join(target_real, member.name))
            if not (dest == target_real or dest.startswith(target_real + os.sep)):
                raise ValueError(f'越界成员: {member.name}')
            if member.issym() or member.islnk():
                link_target = os.path.realpath(
                    os.path.join(os.path.dirname(dest), member.linkname))
                if not (link_target == target_real
                        or link_target.startswith(target_real + os.sep)):
                    raise ValueError(f'链接越界: {member.name}')
            top = member.name.split('/', 1)[0]
            if top:
                extracted_top.add(top)
        try:
            tf.extractall(target_real, filter='data')  # type: ignore[arg-type]
        except TypeError:
            tf.extractall(target_real)
    if len(extracted_top) == 1:
        cand = os.path.join(target_real, next(iter(extracted_top)))
        if os.path.isdir(cand):
            return cand
    return target_real


def find_run_script(work_root: str) -> str | None:
    p = os.path.join(work_root, 'run.sh')
    return p if os.path.isfile(p) else None


def start_service_process(svc: LocalService, *, port: int | None,
                          run_args: str, env_extra: dict) -> int:
    work_root = svc.work_dir
    run_sh = find_run_script(work_root)
    # 如果 work_dir 直接不是 run.sh 所在层, 看是否只有一个子目录
    if not run_sh and os.path.isdir(work_root):
        subs = [e for e in os.listdir(work_root)
                if os.path.isdir(os.path.join(work_root, e))]
        if len(subs) == 1:
            cand = os.path.join(work_root, subs[0])
            if os.path.isfile(os.path.join(cand, 'run.sh')):
                run_sh = os.path.join(cand, 'run.sh')
                work_root = cand
    if not run_sh:
        raise RuntimeError('未找到 run.sh')

    try:
        os.chmod(run_sh, 0o755)
    except OSError:
        pass

    args = ['bash', run_sh]
    if port is not None:
        args.append(str(port))
    for tok in (run_args or '').split():
        args.append(tok)

    env = os.environ.copy()
    if port is not None:
        env['LOCAL_PORT'] = str(port)
        env['PORT'] = str(port)
    env['PYTHONUNBUFFERED'] = '1'
    for k, v in (env_extra or {}).items():
        env[str(k)] = str(v)

    log_fp = open(svc.log_path, 'ab', buffering=0)
    log_fp.write(f"\n===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} START {svc.name} =====\n".encode())
    log_fp.flush()
    proc = subprocess.Popen(
        args, cwd=work_root, stdout=log_fp, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True, env=env,
    )
    time.sleep(0.4)
    if proc.poll() is not None:
        log_fp.close()
        raise RuntimeError(f'run.sh 启动后立即退出 (exit={proc.returncode})')
    svc.write_pid(proc.pid)
    return proc.pid


def stop_service_process(svc: LocalService, *, grace: float = 5.0) -> int | None:
    pid = svc.pid
    if not pid or not svc.is_alive():
        svc.clear_pid()
        return None
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        svc.clear_pid()
        return None
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        svc.clear_pid()
        return None
    deadline = time.time() + grace
    while time.time() < deadline:
        if not svc.is_alive():
            svc.clear_pid()
            return 0
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    time.sleep(0.2)
    svc.clear_pid()
    return -9


def tail_log(path: str, lines: int = 200, max_bytes: int = 128 * 1024) -> str:
    if not os.path.isfile(path):
        return ''
    size = os.path.getsize(path)
    start = max(0, size - max_bytes)
    with open(path, 'rb') as fh:
        fh.seek(start)
        data = fh.read()
    text = data.decode('utf-8', errors='replace')
    if start > 0:
        idx = text.find('\n')
        if idx >= 0:
            text = text[idx + 1:]
    parts = text.splitlines()
    if len(parts) > lines:
        parts = parts[-lines:]
    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# 任务执行
# ---------------------------------------------------------------------------

def handle_exec_task(payload: dict) -> dict:
    cmd = payload.get('command') or ''
    cwd = payload.get('cwd') or None
    timeout = int(payload.get('timeout') or 30)
    shell = bool(payload.get('shell', True))
    if not cmd:
        return {'status': 'error', 'error': 'command 为空'}
    try:
        proc = subprocess.run(
            cmd if shell else cmd.split(),
            shell=shell, cwd=cwd if cwd and os.path.isdir(cwd) else None,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return {
            'status': 'done',
            'exit_code': proc.returncode,
            'stdout': proc.stdout or '',
            'stderr': proc.stderr or '',
            'result': {'command': cmd, 'cwd': cwd, 'timeout': timeout},
        }
    except subprocess.TimeoutExpired as exc:
        return {
            'status': 'timeout',
            'exit_code': -1,
            'stdout': exc.stdout or '',
            'stderr': (exc.stderr or '') + f'\n[agent] 命令超时 ({timeout}s)',
            'error': 'timeout',
        }
    except FileNotFoundError as exc:
        return {'status': 'error', 'error': f'命令不存在: {exc}'}
    except Exception as exc:  # noqa: BLE001
        return {'status': 'error', 'error': str(exc), 'stderr': traceback.format_exc()[:5000]}


def handle_service_install(http: HttpClient, state: 'AgentState', payload: dict) -> dict:
    sid = payload.get('service_id')
    if not sid:
        return {'status': 'error', 'error': 'service_id 缺失'}
    svc = get_local_service(state, sid, payload.get('name') or f'svc-{sid}')
    # 先 stop (可能是覆盖安装)
    stop_service_process(svc)

    try:
        n = http.download(f'/api/agent/services/{sid}/archive', svc.archive_path)
    except Exception as exc:  # noqa: BLE001
        return {'status': 'error', 'error': f'下载归档失败: {exc}'}

    expected_sha = payload.get('archive_sha256')
    if expected_sha:
        try:
            actual = _sha256_file(svc.archive_path)
            if actual.lower() != expected_sha.lower():
                return {'status': 'error', 'error': f'sha256 不一致: 期望 {expected_sha[:12]}..., 实际 {actual[:12]}...'}
        except OSError as exc:
            return {'status': 'error', 'error': f'校验失败: {exc}'}

    if os.path.isdir(svc.work_dir):
        shutil.rmtree(svc.work_dir, ignore_errors=True)
    try:
        safe_extract_tarball(svc.archive_path, svc.work_dir)
    except (ValueError, OSError) as exc:
        return {'status': 'error', 'error': f'解压失败: {exc}'}

    return _start_and_report(svc, payload)


def handle_service_start(state: 'AgentState', payload: dict) -> dict:
    sid = payload.get('service_id')
    if not sid:
        return {'status': 'error', 'error': 'service_id 缺失'}
    svc = get_local_service(state, sid, payload.get('name') or f'svc-{sid}')
    if not os.path.isdir(svc.work_dir):
        return {'status': 'error', 'error': 'work_dir 不存在, 请先 install'}
    if svc.is_alive():
        return {'status': 'done', 'result': {'pid': svc.pid, 'message': '已在运行'}}
    return _start_and_report(svc, payload)


def _start_and_report(svc: LocalService, payload: dict) -> dict:
    try:
        pid = start_service_process(
            svc,
            port=payload.get('local_port'),
            run_args=payload.get('run_args') or '',
            env_extra=payload.get('env') or {},
        )
        return {
            'status': 'done',
            'result': {'pid': pid, 'work_dir': svc.work_dir, 'log_path': svc.log_path},
            'stdout': tail_log(svc.log_path, lines=20),
        }
    except Exception as exc:  # noqa: BLE001
        return {'status': 'error', 'error': str(exc),
                'stderr': tail_log(svc.log_path, lines=20),
                'stdout': ''}


def handle_service_stop(state: 'AgentState', payload: dict) -> dict:
    sid = payload.get('service_id')
    if not sid:
        return {'status': 'error', 'error': 'service_id 缺失'}
    svc = get_local_service(state, sid, payload.get('name') or f'svc-{sid}')
    code = stop_service_process(svc)
    return {'status': 'done', 'result': {'exit_code': code if code is not None else 0}}


def handle_service_delete(state: 'AgentState', payload: dict) -> dict:
    sid = payload.get('service_id')
    if not sid:
        return {'status': 'error', 'error': 'service_id 缺失'}
    svc = get_local_service(state, sid, payload.get('name') or f'svc-{sid}')
    stop_service_process(svc)
    try:
        shutil.rmtree(svc.root, ignore_errors=True)
    except OSError as exc:
        return {'status': 'error', 'error': str(exc)}
    with SERVICES_LOCK:
        SERVICES.pop(sid, None)
    return {'status': 'done', 'result': {'removed': True}}


def handle_service_log(state: 'AgentState', payload: dict) -> dict:
    sid = payload.get('service_id')
    if not sid:
        return {'status': 'error', 'error': 'service_id 缺失'}
    svc = get_local_service(state, sid, payload.get('name') or f'svc-{sid}')
    lines = max(10, min(int(payload.get('lines') or 200), 5000))
    return {
        'status': 'done',
        'stdout': tail_log(svc.log_path, lines=lines),
        'result': {'alive': svc.is_alive(), 'pid': svc.pid},
    }


def _sha256_file(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 服务「选文件更新」: 先 diff, 确认后再合入 + 重启
# ---------------------------------------------------------------------------
# 两阶段协议:
#   service_update_diff   下载新 tar.gz → 解压到 staging → 跟当前运行目录做 sha256
#                         对比 → 回传 {added, modified, removed, unchanged} 差异,
#                         **不改动任何文件**, staging 保留在 svc.root/update_staging/<token>/
#   service_update_apply  用户确认后下发: 复用 staging (没有就按 blob_token 重新下载解压),
#                         备份将被改/删的文件 → 把 added/modified 合入当前运行目录 →
#                         (可选) 重启服务。removed 默认保留, 仅在 diff 里提示。
SERVICE_UPDATE_STAGING = 'update_staging'
SERVICE_UPDATE_BACKUP = 'update_backup'
SERVICE_UPDATE_MAX_LIST = 800   # diff 列表单类最多回传多少条 (受 result_json 64KB 限制)


def _service_run_root(svc: 'LocalService') -> str:
    """当前服务真正含 run.sh 的目录 (跟 start_service_process 同逻辑)。"""
    work_root = svc.work_dir
    if find_run_script(work_root):
        return work_root
    if os.path.isdir(work_root):
        subs = [e for e in os.listdir(work_root)
                if os.path.isdir(os.path.join(work_root, e))]
        if len(subs) == 1 and os.path.isfile(os.path.join(work_root, subs[0], 'run.sh')):
            return os.path.join(work_root, subs[0])
    return work_root


def _ensure_service_update_staging(http: HttpClient, svc: 'LocalService',
                                   payload: dict) -> tuple[str, str]:
    """确保新版本已下载并解压到 staging, 返回 ``(new_root, archive_path)``。

    若 staging 已存在 (diff 阶段下过) 直接复用; 否则按 blob_token 重新下载 + 校验 + 解压。
    """
    blob_token = (payload.get('blob_token') or '').strip()
    if not blob_token:
        raise ValueError('缺少 blob_token')
    staging = os.path.join(svc.root, SERVICE_UPDATE_STAGING, blob_token)
    archive_path = os.path.join(staging, 'update.tar.gz')
    extract_dir = os.path.join(staging, 'extracted')
    # extracted 已经在 → 直接复用
    if os.path.isdir(extract_dir) and os.listdir(extract_dir):
        return _detect_update_root(extract_dir), archive_path
    os.makedirs(staging, exist_ok=True)
    if not os.path.isfile(archive_path):
        http.download(f'/api/agent/fs-blobs/{blob_token}', archive_path)
    expected_sha = (payload.get('archive_sha256') or payload.get('sha256') or '').strip().lower() or None
    if expected_sha:
        actual = _sha256_file(archive_path)
        if actual.lower() != expected_sha:
            raise ValueError(f'sha256 不一致: 期望 {expected_sha[:12]}..., 实际 {actual[:12]}...')
    if os.path.isdir(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    safe_extract_tarball(archive_path, extract_dir)
    return _detect_update_root(extract_dir), archive_path


def _diff_service_trees(cur_root: str, new_root: str) -> dict:
    """对比两棵目录树 (sha256), 返回结构化 diff。"""
    cur = _walk_tree(cur_root) if os.path.isdir(cur_root) else {}
    new = _walk_tree(new_root)
    added = sorted(p for p in new if p not in cur)
    modified = sorted(p for p in new if p in cur and new[p] != cur[p])
    removed = sorted(p for p in cur if p not in new)
    unchanged = sum(1 for p in new if p in cur and new[p] == cur[p])

    def _with_size(rel: str, root: str) -> dict:
        try:
            sz = os.path.getsize(os.path.join(root, rel))
        except OSError:
            sz = None
        return {'path': rel, 'size': sz}

    diff = {
        'added': [_with_size(p, new_root) for p in added[:SERVICE_UPDATE_MAX_LIST]],
        'modified': [_with_size(p, new_root) for p in modified[:SERVICE_UPDATE_MAX_LIST]],
        'removed': [{'path': p} for p in removed[:SERVICE_UPDATE_MAX_LIST]],
        'added_count': len(added),
        'modified_count': len(modified),
        'removed_count': len(removed),
        'unchanged_count': unchanged,
        'truncated': (len(added) > SERVICE_UPDATE_MAX_LIST
                      or len(modified) > SERVICE_UPDATE_MAX_LIST
                      or len(removed) > SERVICE_UPDATE_MAX_LIST),
    }
    # server 端 result_json 会被截到 64KB; 这里主动把列表收敛到 ~56KB 以内,
    # 避免截断后变成非法 JSON 导致前端解析失败 (计数字段始终保留, 不影响汇总)。
    _shrink_diff_to_budget(diff, budget=56 * 1024)
    return diff


def _shrink_diff_to_budget(diff: dict, budget: int) -> None:
    """逐步砍掉 added/modified/removed 列表里的尾部条目, 直到 JSON 体积 < budget。"""
    def _size() -> int:
        try:
            return len(json.dumps(diff, ensure_ascii=False).encode('utf-8'))
        except (TypeError, ValueError):
            return budget + 1
    keys = ('removed', 'added', 'modified')
    while _size() > budget:
        # 找当前最长的那个列表砍 10%, 最少砍 1 条
        longest = max(keys, key=lambda k: len(diff.get(k) or []))
        lst = diff.get(longest) or []
        if not lst:
            break
        cut = max(1, len(lst) // 10)
        diff[longest] = lst[:-cut]
        diff['truncated'] = True


def handle_service_update_diff(http: HttpClient, state: 'AgentState', payload: dict) -> dict:
    sid = payload.get('service_id')
    if not sid:
        return {'status': 'error', 'error': 'service_id 缺失'}
    svc = get_local_service(state, sid, payload.get('name') or f'svc-{sid}')
    try:
        new_root, _ = _ensure_service_update_staging(http, svc, payload)
    except (ValueError, OSError) as exc:
        return {'status': 'error', 'error': f'准备更新包失败: {exc}'}
    except Exception as exc:  # noqa: BLE001
        return {'status': 'error', 'error': f'下载/解压失败: {exc}'}
    cur_root = _service_run_root(svc)
    diff = _diff_service_trees(cur_root, new_root)
    diff['blob_token'] = (payload.get('blob_token') or '').strip()
    diff['cur_root'] = cur_root
    diff['installed'] = bool(find_run_script(cur_root))
    return {
        'status': 'done',
        'result': diff,
        'stdout': (f"added={diff['added_count']} modified={diff['modified_count']} "
                   f"removed={diff['removed_count']} unchanged={diff['unchanged_count']}"),
    }


def handle_service_update_apply(http: HttpClient, state: 'AgentState', payload: dict) -> dict:
    sid = payload.get('service_id')
    if not sid:
        return {'status': 'error', 'error': 'service_id 缺失'}
    svc = get_local_service(state, sid, payload.get('name') or f'svc-{sid}')
    try:
        new_root, _ = _ensure_service_update_staging(http, svc, payload)
    except (ValueError, OSError) as exc:
        return {'status': 'error', 'error': f'准备更新包失败: {exc}'}
    except Exception as exc:  # noqa: BLE001
        return {'status': 'error', 'error': f'下载/解压失败: {exc}'}

    cur_root = _service_run_root(svc)
    os.makedirs(cur_root, exist_ok=True)
    diff = _diff_service_trees(cur_root, new_root)

    # 先停服务再合入, 避免覆盖运行中的二进制 / 脚本
    stop_service_process(svc)

    backup_dir = os.path.join(svc.root, SERVICE_UPDATE_BACKUP,
                              datetime.now().strftime('%Y%m%d-%H%M%S'))
    applied: list[str] = []
    errors: list[dict] = []
    to_apply = [e['path'] for e in diff['added']] + [e['path'] for e in diff['modified']]
    modified_set = {e['path'] for e in diff['modified']}
    for rel in to_apply:
        src = os.path.join(new_root, rel)
        dst = os.path.join(cur_root, rel)
        try:
            # 仅备份会被覆盖的旧文件 (modified), added 没有旧文件不用备份
            if rel in modified_set and os.path.isfile(dst):
                bdst = os.path.join(backup_dir, rel)
                os.makedirs(os.path.dirname(bdst), exist_ok=True)
                shutil.copy2(dst, bdst)
            os.makedirs(os.path.dirname(dst) or cur_root, exist_ok=True)
            shutil.copy2(src, dst)
            applied.append(rel)
        except OSError as exc:
            errors.append({'path': rel, 'error': str(exc)[:200]})

    result: dict = {
        'applied': applied,
        'applied_count': len(applied),
        'added_count': diff['added_count'],
        'modified_count': diff['modified_count'],
        'removed_count': diff['removed_count'],
        'unchanged_count': diff['unchanged_count'],
        'errors': errors,
        'backup_dir': backup_dir if applied else None,
        'cur_root': cur_root,
    }
    if errors:
        result['restarted'] = False
        return {'status': 'error',
                'error': f'部分文件合入失败 ({len(errors)} 个)',
                'result': result,
                'stdout': tail_log(svc.log_path, lines=20)}

    do_restart = bool(payload.get('restart', True))
    if not do_restart:
        result['restarted'] = False
        return {'status': 'done', 'result': result,
                'stdout': '已合入, 未重启 (restart=0)'}

    start = _start_and_report(svc, payload)
    result['restarted'] = (start.get('status') == 'done')
    if start.get('result'):
        result['restart_pid'] = start['result'].get('pid')
    return {
        'status': start.get('status', 'done'),
        'result': result,
        'stdout': start.get('stdout', ''),
        'stderr': start.get('stderr', ''),
        'error': start.get('error'),
    }


# ---------------------------------------------------------------------------
# 文件管理 (fs_*) 任务
# ---------------------------------------------------------------------------
# 设计要点:
# - fs_list / fs_delete / fs_mkdir 都是结构化输出, 走 task.result_json (≤64KB);
# - fs_pull / fs_push 是 server-side 「blob cache」中转: 用户下载 → agent 把文件
#   POST 到 /api/agent/fs-blobs/<token>; 用户上传 → server 把 blob 准备好, agent
#   GET /api/agent/fs-blobs/<token> 写到设备本地;
# - 默认所有路径都按 agent 进程身份去访问 (没有额外 chroot), 由 server 端 / 部署者
#   通过 RUN_USER 控制权限边界。
# - 受 result_json 64KB 限制, 一次 list 最多返回 ``FS_LIST_MAX_ENTRIES`` 条。

FS_LIST_MAX_ENTRIES = 5000
FS_PULL_MAX_BYTES = 512 * 1024 * 1024   # 单文件下载上限 512MB
FS_PUSH_MAX_BYTES = 512 * 1024 * 1024


def _resolve_user_path(path: str) -> str:
    """把 ``~`` / 相对路径展开成可用的绝对路径。"""
    if not path:
        raise ValueError('path 不能为空')
    p = os.path.expanduser(path)
    return os.path.abspath(p)


def _stat_entry(parent: str, name: str) -> dict:
    """list 时填一行的元数据; 失败时只回 ``error``, 不抛。"""
    import stat as _stat
    full = os.path.join(parent, name)
    try:
        st = os.lstat(full)
        if _stat.S_ISLNK(st.st_mode):
            kind = 'link'
            try:
                target = os.readlink(full)
            except OSError:
                target = ''
        elif _stat.S_ISDIR(st.st_mode):
            kind = 'dir'; target = None
        elif _stat.S_ISREG(st.st_mode):
            kind = 'file'; target = None
        else:
            kind = 'other'; target = None
        return {
            'name': name,
            'type': kind,
            'size': st.st_size,
            'mtime': int(st.st_mtime),
            'mode': _stat.filemode(st.st_mode),
            'mode_octal': oct(st.st_mode & 0o777),
            'uid': st.st_uid, 'gid': st.st_gid,
            'link_target': target,
        }
    except OSError as exc:
        return {'name': name, 'error': str(exc)[:200]}


def handle_fs_list(payload: dict) -> dict:
    raw_path = payload.get('path') or os.path.expanduser('~')
    show_hidden = bool(payload.get('show_hidden', True))
    try:
        path = _resolve_user_path(raw_path)
    except ValueError as exc:
        return {'status': 'error', 'error': str(exc)}
    if not os.path.exists(path):
        return {'status': 'error', 'error': f'路径不存在: {path}'}
    if not os.path.isdir(path):
        # 单文件 → 返回它的 stat
        st = _stat_entry(os.path.dirname(path), os.path.basename(path))
        return {'status': 'done', 'result': {'path': path, 'is_dir': False, 'entries': [st]}}
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        return {'status': 'error', 'error': str(exc)}
    if not show_hidden:
        names = [n for n in names if not n.startswith('.')]
    truncated = False
    if len(names) > FS_LIST_MAX_ENTRIES:
        names = names[:FS_LIST_MAX_ENTRIES]
        truncated = True
    entries = [_stat_entry(path, n) for n in names]
    # 排序: dir 在前, 同类按名字
    entries.sort(key=lambda e: (0 if e.get('type') == 'dir' else 1, e.get('name', '')))
    # 父目录指针 (方便前端做面包屑)
    parent = os.path.dirname(path) if path not in ('/', '') else None
    return {
        'status': 'done',
        'result': {
            'path': path,
            'parent': parent,
            'is_dir': True,
            'entries': entries,
            'truncated': truncated,
            'total': len(entries),
        },
    }


def handle_fs_delete(payload: dict) -> dict:
    try:
        path = _resolve_user_path(payload.get('path') or '')
    except ValueError as exc:
        return {'status': 'error', 'error': str(exc)}
    recursive = bool(payload.get('recursive', False))
    if not os.path.lexists(path):
        return {'status': 'error', 'error': f'路径不存在: {path}'}
    # 简单防护: 拒绝几个明显危险的根
    for danger in ('/', '/etc', '/usr', '/bin', '/sbin', '/boot', '/lib', '/var'):
        if path == danger:
            return {'status': 'error', 'error': f'拒绝删除系统目录: {path}'}
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            if recursive:
                shutil.rmtree(path)
            else:
                os.rmdir(path)
        else:
            return {'status': 'error', 'error': f'未知文件类型: {path}'}
    except OSError as exc:
        return {'status': 'error', 'error': str(exc)}
    return {'status': 'done', 'result': {'removed': True, 'path': path}}


def handle_fs_mkdir(payload: dict) -> dict:
    try:
        path = _resolve_user_path(payload.get('path') or '')
    except ValueError as exc:
        return {'status': 'error', 'error': str(exc)}
    parents = bool(payload.get('parents', True))
    if os.path.exists(path):
        return {'status': 'done', 'result': {'created': False, 'path': path, 'message': '已存在'}}
    try:
        if parents:
            os.makedirs(path)
        else:
            os.mkdir(path)
    except OSError as exc:
        return {'status': 'error', 'error': str(exc)}
    return {'status': 'done', 'result': {'created': True, 'path': path}}


def handle_fs_pull(http: HttpClient, payload: dict) -> dict:
    """把设备本地某个文件 POST 到 ``/api/agent/fs-blobs/<blob_token>``, 让 server 缓存好,
    用户后续就可以 ``GET /api/devices/<id>/fs/blob/<blob_token>`` 直接下载。"""
    try:
        path = _resolve_user_path(payload.get('path') or '')
    except ValueError as exc:
        return {'status': 'error', 'error': str(exc)}
    blob_token = (payload.get('blob_token') or '').strip()
    if not blob_token:
        return {'status': 'error', 'error': '缺少 blob_token'}
    if not os.path.isfile(path):
        return {'status': 'error', 'error': f'不是常规文件: {path}'}
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return {'status': 'error', 'error': str(exc)}
    if size > FS_PULL_MAX_BYTES:
        return {'status': 'error',
                'error': f'文件过大 ({size} > {FS_PULL_MAX_BYTES}); 单文件下载上限 512MB'}
    try:
        sha = _sha256_file(path)
        resp = http.upload_file(f'/api/agent/fs-blobs/{blob_token}', path)
    except HttpUnauthorized:
        raise
    except Exception as exc:  # noqa: BLE001
        return {'status': 'error', 'error': f'上传到 server 失败: {exc}'}
    return {
        'status': 'done',
        'result': {
            'path': path, 'size': size, 'sha256': sha,
            'server_received': resp.get('size'),
            'blob_token': blob_token,
        },
    }


def handle_fs_push(http: HttpClient, state: 'AgentState', payload: dict) -> dict:
    """从 server 下载用户上传的 blob, 写到设备指定路径。"""
    try:
        dest = _resolve_user_path(payload.get('dest_path') or '')
    except ValueError as exc:
        return {'status': 'error', 'error': str(exc)}
    blob_token = (payload.get('blob_token') or '').strip()
    if not blob_token:
        return {'status': 'error', 'error': '缺少 blob_token'}
    expected_sha = (payload.get('sha256') or '').strip().lower() or None
    expected_size = payload.get('size')
    overwrite = bool(payload.get('overwrite', False))
    if os.path.lexists(dest) and not overwrite:
        return {'status': 'error', 'error': f'目标已存在 (overwrite=false): {dest}'}
    if os.path.isdir(dest):
        return {'status': 'error', 'error': f'目标是目录, 拒绝写入: {dest}'}
    parent = os.path.dirname(dest)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            return {'status': 'error', 'error': f'创建父目录失败: {exc}'}
    # 先写到 .partial 再 rename, 避免半个文件
    tmp_path = dest + '.partial'
    try:
        n = http.download(f'/api/agent/fs-blobs/{blob_token}', tmp_path)
    except Exception as exc:  # noqa: BLE001
        try: os.remove(tmp_path)
        except OSError: pass
        return {'status': 'error', 'error': f'下载失败: {exc}'}
    if expected_size is not None and int(expected_size) != n:
        try: os.remove(tmp_path)
        except OSError: pass
        return {'status': 'error',
                'error': f'大小不一致: 期望 {expected_size}, 实际 {n}'}
    actual_sha = ''
    try:
        actual_sha = _sha256_file(tmp_path)
    except OSError:
        pass
    if expected_sha and actual_sha and actual_sha != expected_sha:
        try: os.remove(tmp_path)
        except OSError: pass
        return {'status': 'error',
                'error': f'sha256 不一致: 期望 {expected_sha[:12]}..., 实际 {actual_sha[:12]}...'}
    try:
        os.replace(tmp_path, dest)
    except OSError as exc:
        try: os.remove(tmp_path)
        except OSError: pass
        return {'status': 'error', 'error': f'rename 失败: {exc}'}
    # 可选: 设置文件模式 (默认 0644)
    try:
        mode = int(payload.get('mode') or 0o644)
        os.chmod(dest, mode)
    except (OSError, TypeError, ValueError):
        pass
    return {
        'status': 'done',
        'result': {'dest_path': dest, 'size': n, 'sha256': actual_sha},
    }


# ---------------------------------------------------------------------------
# Agent 自更新 (agent_self_update)
# ---------------------------------------------------------------------------
# 协议:
# - server 把新版本打成 tar.gz 上传到自己的 blob cache, 拿到 blob_token
# - 给 agent 派一个 ``agent_self_update`` 任务, payload: {blob_token, sha256?,
#                                                        version_hint, restart?}
# - agent 拉到任务后:
#     1) 下载 tar.gz 到 workdir/update_staging/<token>.tar.gz, 校验 sha256
#     2) 安全解压到 workdir/update_staging/<token>/
#     3) 自动剥离最外层 ``device_agent/`` 目录 (如有), 得到「新版本根」
#     4) 列出新版本根下所有相对路径; 跟当前 agent 源码目录做 sha256 对比, 得 diff
#     5) 把当前 agent 源码 backup 到 backup/<timestamp>/, 然后把新文件覆盖过去
#     6) py_compile 校验新的 agent.py 能否编译; 不能则回滚
#     7) result 里回传 {applied, changed, added, unchanged, errors, ...}
# - poll_loop 看到 result 里有 ``_restart_after`` → sleep 2s → os.execv 自己

AGENT_UPDATE_STAGING = 'update_staging'
AGENT_UPDATE_BACKUP = 'update_backup'
# 安全防护: tar.gz 解压时拒绝这些 path
_UPDATE_FORBIDDEN_NAME_PARTS = {'..', ''}


def _agent_source_dir() -> str:
    """当前 agent.py 所在目录 (我们要更新的目标)。"""
    return os.path.dirname(os.path.abspath(__file__))


def _walk_tree(root: str) -> dict[str, str]:
    """收集 root 下所有常规文件 → {rel_path: sha256}, 不跟随软链接。"""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # 跳过常见垃圾目录
        dirnames[:] = [d for d in dirnames if d not in ('__pycache__', '.git', 'venv', 'yenv', '.deps_installed')]
        for fn in filenames:
            if fn in ('agent.log',) or fn.endswith('.pyc'):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                out[rel] = _sha256_file(full)
            except OSError:
                continue
    return out


def _safe_extract_update(archive: str, dest: str) -> None:
    """tar.gz 解压防护: 绝对路径 / ``..`` / 符号链接 / 设备节点 一律拒绝。"""
    with tarfile.open(archive, 'r:*') as tf:
        for m in tf.getmembers():
            name = m.name or ''
            if name.startswith('/') or any(part in _UPDATE_FORBIDDEN_NAME_PARTS for part in name.split('/')):
                raise ValueError(f'tar 成员名非法 (绝对路径或 ..): {name!r}')
            if m.islnk() or m.issym():
                raise ValueError(f'tar 含符号 / 硬链接, 拒绝解压: {name!r}')
            if not (m.isfile() or m.isdir()):
                raise ValueError(f'tar 含非常规类型, 拒绝: {name!r} type={m.type}')
        tf.extractall(dest)


def _detect_update_root(extract_dir: str) -> str:
    """如果 tar.gz 里就一层目录 (e.g. ``device_agent/``), 自动剥到那一层。"""
    entries = [e for e in os.listdir(extract_dir) if not e.startswith('.')]
    if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
        return os.path.join(extract_dir, entries[0])
    return extract_dir


def _validate_new_agent(new_root: str) -> tuple[bool, str]:
    """新版本里至少要包含 agent.py 且能 py_compile, 否则禁止应用。"""
    new_agent = os.path.join(new_root, 'agent.py')
    if not os.path.isfile(new_agent):
        return False, '新版本里不存在 agent.py'
    import py_compile
    try:
        py_compile.compile(new_agent, doraise=True)
    except py_compile.PyCompileError as exc:
        return False, f'新版 agent.py 语法错误: {exc}'
    return True, ''


def handle_agent_self_update(http: HttpClient, state: 'AgentState', payload: dict) -> dict:
    blob_token = (payload.get('blob_token') or '').strip()
    if not blob_token:
        return {'status': 'error', 'error': '缺少 blob_token'}
    expected_sha = (payload.get('sha256') or '').strip().lower() or None
    expected_size = payload.get('size')
    version_hint = (payload.get('version_hint') or '').strip()[:64]
    do_restart = bool(payload.get('restart', True))

    src_dir = _agent_source_dir()
    staging_root = os.path.join(state.workdir, AGENT_UPDATE_STAGING)
    os.makedirs(staging_root, exist_ok=True)
    job_dir = os.path.join(staging_root, blob_token)
    if os.path.isdir(job_dir):
        shutil.rmtree(job_dir, ignore_errors=True)
    os.makedirs(job_dir, exist_ok=True)
    archive_path = os.path.join(job_dir, 'agent.tar.gz')
    extract_dir = os.path.join(job_dir, 'extracted')

    # 1) 下载
    try:
        n = http.download(f'/api/agent/fs-blobs/{blob_token}', archive_path)
    except Exception as exc:  # noqa: BLE001
        return {'status': 'error', 'error': f'下载新版本归档失败: {exc}'}
    if expected_size is not None and int(expected_size) != n:
        return {'status': 'error',
                'error': f'下载大小不一致: 期望 {expected_size}, 实际 {n}'}
    if expected_sha:
        actual_sha = _sha256_file(archive_path)
        if actual_sha != expected_sha:
            return {'status': 'error',
                    'error': f'sha256 不一致 (期望 {expected_sha[:12]}.. 实际 {actual_sha[:12]}..)'}

    # 2) 安全解压
    try:
        _safe_extract_update(archive_path, extract_dir)
    except (ValueError, OSError, tarfile.TarError) as exc:
        return {'status': 'error', 'error': f'解压失败: {exc}'}

    # 3) 找新版根, 校验 agent.py 能编译
    try:
        new_root = _detect_update_root(extract_dir)
    except OSError as exc:
        return {'status': 'error', 'error': f'读取解压目录失败: {exc}'}
    ok, why = _validate_new_agent(new_root)
    if not ok:
        return {'status': 'error', 'error': f'新版本拒绝应用: {why}'}

    # 4) Diff
    old_files = _walk_tree(src_dir)
    new_files = _walk_tree(new_root)
    added, modified, unchanged = [], [], []
    for rel, sha in new_files.items():
        if rel not in old_files:
            added.append(rel)
        elif old_files[rel] != sha:
            modified.append(rel)
        else:
            unchanged.append(rel)
    # 注: 我们不主动删除 old 里多出来的文件 (保守; 用户清理 backup 后还能滚回去)

    # 5) Backup 当前文件 → backup/<ts>/
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    backup_root = os.path.join(state.workdir, AGENT_UPDATE_BACKUP, ts)
    os.makedirs(backup_root, exist_ok=True)
    touched = list({*added, *modified})
    for rel in touched:
        old_full = os.path.join(src_dir, rel)
        if not os.path.isfile(old_full):
            continue
        bak = os.path.join(backup_root, rel)
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        try:
            shutil.copy2(old_full, bak)
        except OSError as exc:
            logger.warning('备份 %s 失败: %s', rel, exc)

    # 6) 应用 (复制覆盖)
    applied: list[str] = []; errors: list[dict] = []
    for rel in (added + modified):
        new_full = os.path.join(new_root, rel)
        old_full = os.path.join(src_dir, rel)
        try:
            os.makedirs(os.path.dirname(old_full), exist_ok=True)
            shutil.copy2(new_full, old_full)
            applied.append(rel)
        except OSError as exc:
            errors.append({'path': rel, 'error': str(exc)})
            logger.error('apply %s 失败: %s', rel, exc)

    # 7) 再次 py_compile, 确认应用后的 agent.py 是好的
    final_ok, final_why = _validate_new_agent(src_dir)
    if not final_ok:
        # 回滚
        logger.error('应用后校验失败 (%s), 开始回滚', final_why)
        for rel in applied:
            bak = os.path.join(backup_root, rel)
            cur = os.path.join(src_dir, rel)
            try:
                if os.path.isfile(bak):
                    shutil.copy2(bak, cur)
                elif rel in added:
                    os.remove(cur)  # 新增的, 删掉
            except OSError as exc:
                logger.error('回滚 %s 失败: %s', rel, exc)
        return {'status': 'error',
                'error': f'应用后 py_compile 失败, 已回滚: {final_why}',
                'result': {'applied': applied, 'errors': errors,
                           'backup_dir': backup_root}}

    summary = {
        'version_hint': version_hint,
        'agent_source_dir': src_dir,
        'archive_size': n,
        'archive_sha256': expected_sha or _sha256_file(archive_path),
        'added': added, 'modified': modified, 'unchanged_count': len(unchanged),
        'applied': applied, 'errors': errors,
        'backup_dir': backup_root,
        'will_restart': do_restart,
    }
    logger.info('agent self-update 完成: added=%s modified=%s applied=%s',
                len(added), len(modified), len(applied))

    out: dict = {'status': 'done', 'result': summary,
                 'stdout': f'added={len(added)} modified={len(modified)} '
                          f'errors={len(errors)} backup={backup_root}'}
    if do_restart:
        # 让 poll_loop 在回传结果后重启进程
        out['_restart_after'] = True
    return out


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

class AgentState:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.workdir = os.path.abspath(args.workdir)
        os.makedirs(self.workdir, exist_ok=True)
        self.public_ip: str | None = None
        self.tunnel_info: dict | None = None
        if args.tunnel_info:
            try:
                self.tunnel_info = json.loads(args.tunnel_info)
            except json.JSONDecodeError:
                logger.warning('tunnel_info 不是合法 JSON, 忽略')
        self.stop = threading.Event()
        # device_uid / device_token 优先用 CLI / ENV 传入; 否则从 workdir 持久化文件读 / 自动生成。
        self.device_uid: str = self._resolve_device_uid()
        self.device_token: str = self._resolve_device_token()

    # ------------------------------------------------------------------
    # device_uid / token 持久化
    # ------------------------------------------------------------------
    @property
    def uid_path(self) -> str:
        return os.path.join(self.workdir, 'device.uid')

    @property
    def token_path(self) -> str:
        return os.path.join(self.workdir, 'device.token')

    def _resolve_device_uid(self) -> str:
        # 1) CLI / env (FM_DEVICE_UID) 优先
        cli_uid = (getattr(self.args, 'device_uid', None) or '').strip().lower()
        if cli_uid:
            self._write_file_chmod(self.uid_path, cli_uid)
            return cli_uid
        # 2) 本地持久化文件
        if os.path.isfile(self.uid_path):
            try:
                with open(self.uid_path, 'r', encoding='utf-8') as fh:
                    uid = fh.read().strip().lower()
                if uid:
                    return uid
            except OSError as exc:
                logger.warning('读取 device.uid 失败 (%s), 将重新生成', exc)
        # 3) 自动生成: 优先用机器指纹 (hostname+mac), 缺失就退化到 secrets 随机 hex
        uid = _generate_device_uid()
        self._write_file_chmod(self.uid_path, uid)
        logger.info('生成新的 device_uid = %s (写入 %s)', uid, self.uid_path)
        return uid

    def _resolve_device_token(self) -> str:
        cli_tok = (getattr(self.args, 'device_token', None) or '').strip()
        if cli_tok:
            return cli_tok
        if os.path.isfile(self.token_path):
            try:
                with open(self.token_path, 'r', encoding='utf-8') as fh:
                    tok = fh.read().strip()
                if tok:
                    return tok
            except OSError:
                pass
        return ''

    def save_token(self, token: str) -> None:
        self.device_token = (token or '').strip()
        if self.device_token:
            self._write_file_chmod(self.token_path, self.device_token)

    @staticmethod
    def _write_file_chmod(path: str, content: str) -> None:
        """token / uid 都按 0600 写入 (only owner readable), 避免误泄漏。"""
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(content)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _generate_device_uid() -> str:
    """生成稳定的 device_uid: hostname + MAC 的 sha1 截断, 失败回退到 secrets。

    优点: 同一台机器换 workdir / 重装 agent 后, 如果 hostname/MAC 不变, 拿到的
    uid 一样, 服务器侧会复用同一个设备记录 (不会重复创建)。
    """
    import hashlib
    import secrets as _sec
    try:
        host = socket.gethostname() or 'unknown'
        mac = None
        try:
            import uuid as _u
            mac = f'{_u.getnode():012x}'
        except Exception:  # noqa: BLE001
            mac = ''
        fp = f'{host}|{mac}|{platform.machine()}|{platform.system()}'
        digest = hashlib.sha1(fp.encode('utf-8', errors='ignore')).hexdigest()
        return digest[:24]  # 24 hex chars 已经够全宇宙不冲突
    except Exception:  # noqa: BLE001
        return _sec.token_hex(12)


def task_executor(http: HttpClient, state: AgentState, task: dict) -> dict:
    kind = task.get('kind')
    payload = task.get('payload') or {}
    try:
        if kind == 'exec':
            return handle_exec_task(payload)
        if kind == 'service_install':
            return handle_service_install(http, state, payload)
        if kind == 'service_start':
            return handle_service_start(state, payload)
        if kind == 'service_stop':
            return handle_service_stop(state, payload)
        if kind == 'service_delete':
            return handle_service_delete(state, payload)
        if kind == 'service_log':
            return handle_service_log(state, payload)
        if kind == 'service_update_diff':
            return handle_service_update_diff(http, state, payload)
        if kind == 'service_update_apply':
            return handle_service_update_apply(http, state, payload)
        if kind == 'fs_list':
            return handle_fs_list(payload)
        if kind == 'fs_delete':
            return handle_fs_delete(payload)
        if kind == 'fs_mkdir':
            return handle_fs_mkdir(payload)
        if kind == 'fs_pull':
            return handle_fs_pull(http, payload)
        if kind == 'fs_push':
            return handle_fs_push(http, state, payload)
        if kind == 'agent_self_update':
            return handle_agent_self_update(http, state, payload)
        return {'status': 'error', 'error': f'未知任务类型: {kind}'}
    except Exception as exc:  # noqa: BLE001
        return {'status': 'error', 'error': str(exc), 'stderr': traceback.format_exc()[:5000]}


def _maybe_reenroll_on_401(http: HttpClient, state: AgentState, exc: Exception) -> bool:
    """碰到 401 时, 用本地 device_uid 重新 enroll 拿一份新 token。

    返回 ``True`` 表示成功 re-enroll, 调用方可以继续; ``False`` 表示放弃这一轮。
    """
    if not isinstance(exc, HttpUnauthorized):
        return False
    enroll_token = (state.args.enroll_token or '').strip()
    logger.warning('token 失效 (%s), 尝试自助 re-enroll', exc)
    return _do_enroll(http, state, enroll_token=enroll_token)


def heartbeat_loop(http: HttpClient, state: AgentState) -> None:
    interval = max(5, state.args.heartbeat_interval)
    while not state.stop.is_set():
        try:
            payload = build_heartbeat_payload(state)
            http.request_json('POST', '/api/agent/heartbeat', payload=payload, timeout=15)
        except HttpUnauthorized as exc:
            _maybe_reenroll_on_401(http, state, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning('heartbeat 失败: %s', exc)
        state.stop.wait(interval)


def poll_loop(http: HttpClient, state: AgentState) -> None:
    wait = max(0, min(state.args.poll_wait, 25))
    backoff = 1.0
    while not state.stop.is_set():
        try:
            data = http.request_json('GET', '/api/agent/poll',
                                     params={'wait': wait}, timeout=wait + 15)
            backoff = 1.0
            tasks = data.get('tasks') or []
            for t in tasks:
                tid = t.get('id'); ctok = t.get('claim_token')
                logger.info('收到任务 #%s kind=%s', tid, t.get('kind'))
                result = task_executor(http, state, t)
                result.setdefault('status', 'done')
                if ctok:
                    result['claim_token'] = ctok
                # ``_restart_after`` 是 agent_self_update 用的内部标记, 不要发给 server
                restart_after = bool(result.pop('_restart_after', False))
                try:
                    http.request_json('POST', f'/api/agent/tasks/{tid}/result',
                                      payload=result, timeout=30)
                    logger.info('任务 #%s 完成 status=%s', tid, result.get('status'))
                except Exception as exc:  # noqa: BLE001
                    logger.warning('回传任务 #%s 失败: %s', tid, exc)
                if restart_after and result.get('status') == 'done':
                    logger.warning('收到自更新成功标记, 2s 后重启 agent (execv 自己)')
                    state.stop.set()
                    time.sleep(2.0)
                    try:
                        py = sys.executable
                        os.execv(py, [py] + list(sys.argv))
                    except OSError as exc:
                        logger.error('os.execv 失败 (%s); 用退出码 42 让 systemd 重启', exc)
                        os._exit(42)
            if not tasks:
                state.stop.wait(0.5)
        except HttpUnauthorized as exc:
            if _maybe_reenroll_on_401(http, state, exc):
                continue
            state.stop.wait(min(backoff, 30))
            backoff = min(backoff * 2, 30)
        except Exception as exc:  # noqa: BLE001
            logger.warning('poll 失败: %s', exc)
            state.stop.wait(min(backoff, 30))
            backoff = min(backoff * 2, 30)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def install_signal_handlers(state: AgentState) -> None:
    def _stop(signum, _frame):
        logger.info('收到信号 %s, 关闭中…', signum)
        state.stop.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _stop)
        except (OSError, ValueError):
            pass


def load_env_config(args: argparse.Namespace) -> argparse.Namespace:
    """允许通过 ENV 覆盖 CLI 中未设置的字段, 方便 systemd 部署。"""
    env_map = {
        'server_url': 'FM_SERVER_URL',
        'device_token': 'FM_DEVICE_TOKEN',
        'device_uid': 'FM_DEVICE_UID',
        'workdir': 'FM_AGENT_WORKDIR',
        'tunnel_info': 'FM_TUNNEL_INFO',
        'enroll_token': 'FM_ENROLL_TOKEN',
        'frpc_config': 'FM_AGENT_FRPC_CONFIG',
    }
    for attr, env in env_map.items():
        if not getattr(args, attr, None):
            v = os.environ.get(env)
            if v:
                setattr(args, attr, v)
    return args


def _do_enroll(http: HttpClient, state: 'AgentState', enroll_token: str = '') -> bool:
    """调用 ``/api/agent/enroll`` 自助注册; 成功后把 token 写回 state + 落盘。"""
    payload = build_heartbeat_payload(state, query_public_ip=True)
    payload['device_uid'] = state.device_uid  # 显式带, 万一 build 那边没塞
    headers = {'X-Enroll-Token': enroll_token} if enroll_token else None
    try:
        data = http.request_json('POST', '/api/agent/enroll',
                                 payload=payload, timeout=15,
                                 extra_headers=headers)
    except Exception as exc:  # noqa: BLE001
        logger.error('enroll 失败: %s', exc)
        return False
    dev = data.get('device') or {}
    tok = dev.get('device_token') or ''
    if not tok:
        logger.error('enroll 返回里没有 device_token: %s', data)
        return False
    state.save_token(tok)
    http.set_token(tok)
    logger.info('enroll 成功: name=%s uid=%s created=%s', dev.get('name'),
                dev.get('device_uid'), data.get('created'))
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='file_manager 设备客户端 (agent)')
    ap.add_argument('--server-url', help='服务器 base URL, e.g. http://example.com:5000')
    ap.add_argument('--device-token', help='设备 token (X-Device-Token 头); 不填则启用自助 enroll')
    ap.add_argument('--device-uid', help='设备 UID; 不填则从 workdir/device.uid 读取或自动生成')
    ap.add_argument('--enroll-token', default='', help='服务器要求的 enroll 密钥 (X-Enroll-Token), 公网部署用')
    ap.add_argument('--workdir', default=os.path.expanduser('~/.fm_device_agent'),
                    help='本地工作目录 (服务解压 / 日志 / device.uid / device.token); 默认 ~/.fm_device_agent')
    ap.add_argument('--heartbeat-interval', type=int, default=20,
                    help='心跳间隔秒数, 默认 20')
    ap.add_argument('--poll-wait', type=int, default=20,
                    help='长轮询服务器等待秒数, 默认 20')
    ap.add_argument('--no-verify-tls', action='store_true', help='HTTPS 不校验证书')
    ap.add_argument('--tunnel-info', default='', help='可选: JSON 字符串, 注入到心跳的 tunnel_info')
    ap.add_argument('--frpc-config', default='',
                    help='可选: frpc.toml 绝对路径; 当 frpc 用相对路径 -c 且其 cwd 读不到时 (root 跑的 frpc + agent 是普通用户) 作 fallback; 也可用 FM_AGENT_FRPC_CONFIG 环境变量')
    ap.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = ap.parse_args(argv)
    args = load_env_config(args)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s %(levelname)s %(name)s | %(message)s',
    )
    if not args.server_url:
        logger.error('--server-url 不能为空 (也可通过 FM_SERVER_URL 环境变量传入)')
        return 2

    # frpc 配置兜底路径: collect_stats 读这个模块全局变量
    if args.frpc_config:
        set_frpc_fallback_config(args.frpc_config)
        logger.info('frpc 配置兜底路径已设置: %s (相对路径 -c 解析失败时使用)', args.frpc_config)

    state = AgentState(args)
    install_signal_handlers(state)
    http = HttpClient(args.server_url, state.device_token,
                      verify_tls=not args.no_verify_tls)

    logger.info('启动 agent: workdir=%s device_uid=%s token=%s',
                state.workdir, state.device_uid,
                '已配置' if state.device_token else '(将自助 enroll)')

    # 步骤 1: 如果没 token, 先 enroll
    if not state.device_token:
        ok = False
        for attempt in range(3):
            if _do_enroll(http, state, enroll_token=args.enroll_token):
                ok = True; break
            logger.warning('enroll 重试 (%s/3)…', attempt + 1)
            time.sleep(2 ** attempt)
        if not ok:
            logger.error('enroll 多次失败, 请检查 --server-url / --enroll-token 配置')
            return 1

    # 步骤 2: register; 401 时自动重 enroll 一次
    for attempt in range(3):
        try:
            data = http.request_json('POST', '/api/agent/register',
                                     payload=build_heartbeat_payload(state, query_public_ip=True),
                                     timeout=15)
            d = data.get('device') or {}
            logger.info('注册成功 device=%s online=%s server_time=%s',
                        d.get('name'), d.get('online'), data.get('server_time'))
            break
        except HttpUnauthorized:
            logger.warning('token 失效, 触发重 enroll')
            if not _do_enroll(http, state, enroll_token=args.enroll_token):
                return 1
        except Exception as exc:  # noqa: BLE001
            logger.warning('注册失败 (第 %s 次): %s', attempt + 1, exc)
            time.sleep(2 ** attempt)
    else:
        logger.error('多次注册失败, 退出')
        return 1

    hb_thread = threading.Thread(target=heartbeat_loop, args=(http, state),
                                 name='hb', daemon=True)
    poll_thread = threading.Thread(target=poll_loop, args=(http, state),
                                   name='poll', daemon=True)
    hb_thread.start()
    poll_thread.start()
    logger.info('agent 已启动 workdir=%s heartbeat=%ss poll_wait=%ss',
                state.workdir, args.heartbeat_interval, args.poll_wait)

    try:
        while not state.stop.is_set():
            state.stop.wait(1)
    except KeyboardInterrupt:
        state.stop.set()

    logger.info('退出中, 等待 5s …')
    hb_thread.join(timeout=5)
    poll_thread.join(timeout=5)
    return 0


if __name__ == '__main__':
    sys.exit(main())
