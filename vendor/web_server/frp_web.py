#!/usr/bin/env python3
"""
FRP 工具箱 Web UI
=================
将 frp_port_bandwidth_test.py 的全部功能封装为一个 Web 服务:
  - IP 段发现 (扫 server-port 端口找可用 frps server)
  - 端口范围探测 (哪些远程端口能被 frpc 映射并访问) + 自动生成 frpc 配置
  - 智能选端口(closed 优先) + 延迟/带宽测试 (单 IP / 列表 / 数据库批量)
  - 扫描缓存查看与清理
  - 已生成 frpc 配置文件的查看与下载

后端复用 frp_port_bandwidth_test.py (作为 core 模块) 的纯函数,
长任务在后台线程运行, 前端轮询 /api/job/<id> 获取实时日志与结果。

运行: python3 frp_web.py  (默认 http://0.0.0.0:5001)
"""

import glob
import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid

from flask import (Flask, jsonify, render_template, request,
                   send_file, abort)

import frp_port_bandwidth_test as core

app = Flask(__name__)

DB_PATH = os.path.join(core.BASE_DIR, "resources.db")
DEFAULT_FILE = "51M"

# 全局共享: 本地 HTTP 服务器(供带宽/探测复用) + 扫描缓存
HTTPD, HTTP_PORT = core.start_http_server()
CACHE = core.ScanCache(core.CACHE_PATH, enabled=True)


# --------------------------------------------------------------------------- #
# 数据库辅助
# --------------------------------------------------------------------------- #
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_results_table():
    conn = db_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bandwidth_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server TEXT, server_port INTEGER, remote_port INTEGER,
            latency_ms REAL, ttfb_ms REAL, speed_mbps REAL,
            mb REAL, seconds REAL, url TEXT, tested_at TEXT
        )""")
    conn.commit()
    conn.close()


def save_bandwidth_results(records):
    if not records:
        return 0
    ensure_results_table()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = db_conn()
    for r in records:
        conn.execute(
            "INSERT INTO bandwidth_results (server, server_port, remote_port, "
            "latency_ms, ttfb_ms, speed_mbps, mb, seconds, url, tested_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r.get("server"), r.get("server_port"), r.get("remote_port"),
             r.get("latency_avg_ms"), r.get("ttfb_ms"), r.get("speed_mbps"),
             r.get("mb"), r.get("seconds"), r.get("url"), now))
    conn.commit()
    conn.close()
    return len(records)


ensure_results_table()

# --------------------------------------------------------------------------- #
# 后台任务系统
# --------------------------------------------------------------------------- #
JOBS = {}
JOBS_LOCK = threading.Lock()


class JobCancelled(Exception):
    """任务被用户主动中断时抛出。"""


class Job:
    def __init__(self, kind):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "running"   # running / paused / done / error / cancelled
        self.logs = []
        self.result = None
        self.error = None
        self.created = time.time()
        self.lock = threading.Lock()
        self._paused = threading.Event()
        self._cancelled = threading.Event()

    def log(self, msg):
        with self.lock:
            self.logs.append({"t": round(time.time() - self.created, 1),
                              "msg": str(msg)})

    # ---- 控制 ----
    def pause(self):
        if self.status in ("running",):
            self._paused.set()
            self.status = "paused"
            self.log("⏸ 已暂停 (当前进行中的子任务会先完成)")

    def resume(self):
        if self.status == "paused":
            self._paused.clear()
            self.status = "running"
            self.log("▶ 已继续")

    def cancel(self):
        if self.status in ("running", "paused"):
            self._cancelled.set()
            self._paused.clear()  # 唤醒可能正卡在暂停中的线程
            self.log("⏹ 收到中断请求, 正在停止...")

    def gate(self):
        """协作式检查点: 暂停时阻塞, 中断时抛出 JobCancelled。"""
        while self._paused.is_set() and not self._cancelled.is_set():
            time.sleep(0.2)
        if self._cancelled.is_set():
            raise JobCancelled()

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def to_dict(self):
        with self.lock:
            return {
                "id": self.id, "kind": self.kind, "status": self.status,
                "logs": list(self.logs), "result": self.result,
                "error": self.error,
            }


def run_pool(job, items, fn, concurrency):
    """并发执行 fn(item), 支持暂停/继续/中断。

    采用滚动调度: 最多保持 concurrency 个在途任务, 调度新任务前检查
    暂停/中断状态, 因此暂停最多等当前在途子任务完成即可生效。
    返回已完成项的结果列表 (顺序不保证)。
    """
    from concurrent.futures import (ThreadPoolExecutor, FIRST_COMPLETED,
                                    wait as fwait)
    items = list(items)
    n = len(items)
    idx = 0
    results = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = set()
        while idx < n or futures:
            job.gate()  # 暂停则阻塞; 中断则抛出 (in-flight 会在退出时收尾)
            while idx < n and len(futures) < concurrency:
                futures.add(ex.submit(fn, items[idx]))
                idx += 1
            if not futures:
                break
            done, futures = fwait(futures, timeout=0.3,
                                  return_when=FIRST_COMPLETED)
            for f in done:
                results.append(f.result())
    return results


def start_job(kind, target, *args):
    job = Job(kind)
    with JOBS_LOCK:
        JOBS[job.id] = job

    def runner():
        try:
            job.result = target(job, *args)
            job.status = "done"
        except JobCancelled:
            job.status = "cancelled"
            job.log("⏹ 任务已中断")
        except Exception as e:  # noqa
            import traceback
            job.error = f"{type(e).__name__}: {e}"
            job.log("错误: " + job.error)
            job.log(traceback.format_exc())
            job.status = "error"

    threading.Thread(target=runner, daemon=True).start()
    return job.id


# --------------------------------------------------------------------------- #
# 任务实现
# --------------------------------------------------------------------------- #
def task_discover(job, cidr, port_spec, concurrency, verify, token, user, wait):
    blocks = core.expand_targets(cidr)
    job.log(f"展开为 {len(blocks)} 个网段, nmap 直接扫描端口 [{port_spec}] (并发 {concurrency})")
    found = []  # (ip, port)
    slock = threading.Lock()
    scount = {"n": 0}

    def scan_block(blk):
        try:
            hits = core.scan_open_port(blk, port_spec) or []
        except Exception:
            hits = []
        with slock:
            scount["n"] += 1
            done = scount["n"]
            for ip, port in hits:
                found.append((ip, port))
        for ip, port in hits:
            job.log(f"[nmap开放] {ip}:{port}")
        job.log(f"进度 {done}/{len(blocks)} ({blk}) -> nmap开放累计 {len(found)} 个")

    run_pool(job, blocks, scan_block, concurrency)
    found = sorted(set(found))

    if not verify:
        return {"port_spec": port_spec, "verified": False, "count": len(found),
                "hits": [{"ip": ip, "port": port} for ip, port in found]}

    # frpc 握手验证: 过滤云安全设备造成的"假 open", 只保留真正的 frps
    job.log(f"\n[验证] 对 {len(found)} 个开放端口做 frpc 握手验证 "
            f"(只保留真正的 frps) ...")
    real = []
    vlock = threading.Lock()
    vcount = {"n": 0}

    def verify_one(item):
        ip, port = item
        ok, msg = core.verify_frps_login(ip, port, token=token, user=user,
                                         wait=wait)
        with vlock:
            vcount["n"] += 1
            n = vcount["n"]
            if ok:
                real.append((ip, port))
        if ok:
            job.log(f"[{n}/{len(found)}] {ip}:{port} ✓ 真 frps (login success)")
        else:
            job.log(f"[{n}/{len(found)}] {ip}:{port} ✗ 非 frps: {msg}")
        return ok

    run_pool(job, found, verify_one, concurrency)

    real = sorted(set(real))
    job.log(f"\n验证完成: nmap开放 {len(found)} 个, 真正 frps {len(real)} 个")
    return {"port_spec": port_spec, "verified": True,
            "nmap_open": len(found), "count": len(real),
            "hits": [{"ip": ip, "port": port} for ip, port in real]}


def task_probe(job, ip, server_port, start, end, concurrency, wait,
               token, user, file, config_ports, local_port):
    ports = list(range(start, end + 1))
    job.log(f"探测 {ip}:{server_port} 远程端口 {start}-{end} ({len(ports)} 个) "
            f"并发 {concurrency}")
    counter = {"n": 0}
    clock = threading.Lock()

    def probe(port):
        proc, cfg, log, ok, msg = core.start_frpc_tunnel(
            ip, server_port, HTTP_PORT, port, token, user, wait=wait)
        rec = {"remote_port": port, "mapped": ok, "accessible": False}
        try:
            if not ok:
                rec["error"] = msg
            else:
                url = f"http://{ip}:{port}/{file}"
                acc, code, info = core.verify_access(url)
                rec["accessible"] = acc
                rec["http_code"] = code
                rec["url"] = url
                if acc:
                    rec["rtt_ms"] = info.get("rtt_ms")
                else:
                    rec["error"] = info if isinstance(info, str) else f"HTTP {code}"
        finally:
            core.stop_frpc(proc, cfg, log, keep=False)
        with clock:
            counter["n"] += 1
            n = counter["n"]
        if rec["mapped"] and rec["accessible"]:
            job.log(f"[{n}/{len(ports)}] 端口 {port} OK 映射+访问成功 "
                    f"(rtt {rec.get('rtt_ms')} ms)")
        elif rec["mapped"]:
            job.log(f"[{n}/{len(ports)}] 端口 {port} WARN 映射成功但访问失败")
        else:
            job.log(f"[{n}/{len(ports)}] 端口 {port} FAIL 映射失败")
        return rec

    results = run_pool(job, ports, probe, concurrency)

    results.sort(key=lambda r: r["remote_port"])
    ok_ports = [r["remote_port"] for r in results
                if r["mapped"] and r["accessible"]]
    mapped_only = [r["remote_port"] for r in results
                   if r["mapped"] and not r["accessible"]]

    cfg_path, cfg_text = None, None
    if ok_ports:
        best = sorted((r for r in results if r["mapped"] and r["accessible"]),
                      key=lambda r: (r.get("rtt_ms") is None,
                                     r.get("rtt_ms", 1e9)))
        sel = [r["remote_port"] for r in best]
        if config_ports > 0:
            sel = sel[:config_ports]
        cfg_path = core.write_multiproxy_config(
            ip, server_port, sel, token=token, user=user,
            local_port=local_port)
        with open(cfg_path) as f:
            cfg_text = f.read()
        job.log(f"已生成 frpc 配置: {os.path.basename(cfg_path)} ({len(sel)} 个端口)")

    return {
        "server": ip, "server_port": server_port, "range": [start, end],
        "ok_ports": ok_ports, "mapped_but_inaccessible": mapped_only,
        "config_file": os.path.basename(cfg_path) if cfg_path else None,
        "config_text": cfg_text,
        "details": results,
    }


def _build_server_list(mode, ip, ips, server_port, max_servers):
    servers = []  # (ip, connect_port, db_port)
    if mode == "single":
        db_port = core.get_port_for_ip(DB_PATH, ip)
        servers.append((ip, server_port, db_port))
    elif mode == "list":
        for one in ips:
            one = one.strip()
            if one:
                servers.append((one, server_port, None))
    else:  # db
        raw = core.get_servers(DB_PATH, limit=max_servers or None)
        servers = [(s_ip, server_port, dbp) for s_ip, dbp in raw]
    return servers


def task_bandwidth(job, mode, ip, ips, server_port, max_servers, top_ports,
                   remote_candidates, duration, wait, token, user, file,
                   rescan):
    servers = _build_server_list(mode, ip, ips, server_port, max_servers)
    job.log(f"待测 {len(servers)} 个 server | serverPort={server_port} | "
            f"{'token认证' if token else '无认证'}")
    results, failures = [], []

    for idx, (s_ip, cp, ep) in enumerate(servers, 1):
        try:
            job.gate()
        except JobCancelled:
            CACHE.save()
            raise
        job.log(f"\n[{idx}/{len(servers)}] {s_ip}:{cp}  nmap top-{top_ports} 扫描...")
        try:
            scan = core.nmap_scan_cached(s_ip, CACHE, top_ports=top_ports,
                                         rescan=rescan)
        except Exception as e:
            scan = {"up": False, "states": {}}
            job.log(f"  扫描异常: {e}")
        tag = "缓存" if scan.get("from_cache") else "实扫"
        exclude = [p for p in (cp, ep) if p]
        cands = core.choose_remote_ports(
            scan, max_candidates=remote_candidates, exclude_ports=exclude)
        job.log(f"  [{tag}] up={scan.get('up')} 候选远程端口(closed优先): {cands}")
        if not cands:
            failures.append({"server": s_ip, "server_port": cp,
                             "error": "无可用端口/不可达"})
            job.log("  FAIL 无可用端口")
            continue

        ok_rec = None
        for rp in cands:
            proc, cfg, log, ok, msg = core.start_frpc_tunnel(
                s_ip, cp, HTTP_PORT, rp, token, user, wait=wait)
            try:
                if not ok:
                    job.log(f"  端口 {rp} 映射失败: {msg}")
                    continue
                latency = core.measure_latency(s_ip, rp)
                url = f"http://{s_ip}:{rp}/{file}"
                bw = core.measure_bandwidth(url, duration=duration)
                if bw and "speed_mbps" in bw:
                    ok_rec = {
                        "server": s_ip, "server_port": cp, "remote_port": rp,
                        "latency_avg_ms": round(latency[1], 2) if latency else None,
                        "speed_mbps": round(bw["speed_mbps"], 2),
                        "mb": round(bw["mb"], 1), "seconds": bw["seconds"],
                        "ttfb_ms": bw.get("ttfb_ms"), "url": url,
                    }
                    break
                else:
                    err = bw.get("error") if isinstance(bw, dict) else "下载失败"
                    job.log(f"  端口 {rp} 映射成功但下载失败: {err}")
            finally:
                core.stop_frpc(proc, cfg, log, keep=False)

        if ok_rec:
            results.append(ok_rec)
            job.log(f"  OK 远程端口 {ok_rec['remote_port']} | "
                    f"延迟 {ok_rec['latency_avg_ms']} ms | "
                    f"{ok_rec['speed_mbps']} Mbps")
        else:
            failures.append({"server": s_ip, "server_port": cp,
                             "error": "所有候选端口均失败"})
            job.log("  FAIL 所有候选端口均失败")

    CACHE.save()
    results.sort(key=lambda r: r["speed_mbps"], reverse=True)
    saved = save_bandwidth_results(results)
    if saved:
        job.log(f"已保存 {saved} 条带宽测试结果到数据库 bandwidth_results")
    return {"success": len(results), "failed": len(failures),
            "saved": saved, "results": results, "failures": failures}


def task_portscan(job, ip, top_ports):
    job.gate()
    job.log(f"nmap 扫描 {ip} 最常用的 {top_ports} 个端口 ...")
    scan = core.nmap_scan(ip, top_ports=top_ports)
    states = scan.get("states", {})
    rows = [{"port": p, "state": s} for p, s in sorted(states.items())]
    from collections import Counter
    dist = dict(Counter(states.values()))
    job.log(f"主机存活: {scan.get('up')} | 扫描 {len(rows)} 个端口 | 状态分布: {dist}")
    return {"server": ip, "up": scan.get("up"), "top_ports": top_ports,
            "dist": dist, "rows": rows}


def task_portdetail(job, ip, ports):
    job.gate()
    job.log(f"对 {ip} 的 {len(ports)} 个端口做服务/版本详细扫描 (nmap -sV) ...")
    info = core.nmap_service_scan(ip, ports)
    rows = []
    for p in sorted(ports):
        d = info.get(p, {})
        rows.append({"port": p, "state": d.get("state"),
                     "service": d.get("service"), "product": d.get("product"),
                     "version": d.get("version"), "extrainfo": d.get("extrainfo"),
                     "cpe": d.get("cpe") or []})
        svc = " ".join(x for x in [d.get("service"), d.get("product"),
                                   d.get("version")] if x)
        job.log(f"  端口 {p}: {d.get('state')} {svc or ''}")
    return {"server": ip, "rows": rows}


# CVE 数据源: Shodan CVEDB 公共 API (只读查询, 免费/无需 key, 不向目标发送任何流量)
CVE_API = "https://cvedb.shodan.io/cves"

# 通用的非破坏性验证方法 (不攻击目标, 仅核对/在授权环境复现)
VERIFY_METHODS = [
    "版本比对: 核对目标 banner/版本号是否落在该 CVE 的受影响版本区间 (nmap -sV / 官方公告)。",
    "配置确认: 多数 CVE 仅在特定配置/模块/选项启用时才可触发, 确认目标是否满足前提条件。",
    "补丁核对: 查看厂商安全公告与修复版本, 判断目标版本是否已包含补丁(发行版常做向后移植)。",
    "授权环境复现: 在你自己搭建的同版本隔离环境中, 用官方/公开的非破坏性检测脚本验证 PoC, 切勿对他人目标发送利用流量。",
    "被动指纹: 通过返回头、错误信息、协议特征等只读信息侧面印证, 不发送任何可能致崩溃的载荷。",
]


def to_cpe23(cpe):
    """将 nmap 输出的 CPE 2.2 (cpe:/a:vendor:product:ver) 规整为 CPE 2.3 字符串。"""
    if cpe.startswith("cpe:2.3:"):
        return cpe
    if cpe.startswith("cpe:/"):
        parts = cpe[len("cpe:/"):].split(":")
    elif cpe.startswith("cpe:"):
        parts = cpe[len("cpe:"):].lstrip("/").split(":")
    else:
        parts = cpe.split(":")
    parts = [p if p else "*" for p in parts]
    while len(parts) < 11:
        parts.append("*")
    return "cpe:2.3:" + ":".join(parts[:11])


def lookup_cves(cpe, max_results=15, timeout=15):
    """按 CPE 查询已知 CVE (只读)。返回 [{id, cvss, summary, references, kev}]。"""
    cpe23 = to_cpe23(cpe)
    url = CVE_API + "?" + urllib.parse.urlencode({"cpe23": cpe23})
    req = urllib.request.Request(url, headers={"User-Agent": "frp-toolbox"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    items = data.get("cves") if isinstance(data, dict) else data
    out = []
    for it in (items or []):
        cvss = it.get("cvss_v3")
        if cvss is None:
            cvss = it.get("cvss")
        out.append({
            "id": it.get("cve_id") or it.get("id"),
            "cvss": cvss,
            "summary": (it.get("summary") or "")[:300],
            "references": (it.get("references") or [])[:3],
            "kev": bool(it.get("kev")),
        })
    out.sort(key=lambda x: (x["cvss"] is None, -(x["cvss"] or 0)))
    return out[:max_results]


def task_cve(job, ip, ports):
    job.log(f"对 {ip} 的 {len(ports)} 个端口做服务识别 + 已知 CVE 查询 (只读, 不攻击)")
    info = core.nmap_service_scan(ip, ports)
    rows = []
    for p in sorted(ports):
        job.gate()
        d = info.get(p, {})
        cpes = d.get("cpe") or []
        cves, seen = [], set()
        for cpe in cpes:
            try:
                for c in lookup_cves(cpe):
                    cid = c["id"] if isinstance(c["id"], str) else str(c["id"])
                    if cid not in seen:
                        seen.add(cid)
                        cves.append(c)
            except Exception as e:
                job.log(f"  端口 {p} CVE 查询失败({cpe}): {type(e).__name__}: {e}")
        cves.sort(key=lambda x: (x["cvss"] is None, -(x["cvss"] or 0)))
        rows.append({
            "port": p, "state": d.get("state"), "service": d.get("service"),
            "product": d.get("product"), "version": d.get("version"),
            "cpe": cpes, "cves": cves[:15],
        })
        svc = " ".join(x for x in [d.get("product"), d.get("version")] if x)
        job.log(f"  端口 {p} {d.get('service')} {svc} -> {len(cves)} 个已知 CVE")
    return {"server": ip, "rows": rows, "verify_methods": VERIFY_METHODS}


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("frp_ui.html", http_port=HTTP_PORT)


@app.route("/api/job/<job_id>")
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    return jsonify(job.to_dict())


@app.route("/api/job/<job_id>/<action>", methods=["POST"])
def job_control(job_id, action):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    if action == "pause":
        job.pause()
    elif action == "resume":
        job.resume()
    elif action == "cancel":
        job.cancel()
    else:
        abort(400)
    return jsonify({"id": job.id, "status": job.status})


@app.route("/api/discover", methods=["POST"])
def api_discover():
    d = request.get_json(force=True)
    cidr = (d.get("cidr") or "").strip()
    if not cidr:
        return jsonify({"error": "需要 cidr"}), 400
    port_spec = str(d.get("port_spec") or d.get("server_port") or "7000").strip()
    jid = start_job("discover", task_discover, cidr, port_spec,
                    int(d.get("concurrency", 4)),
                    bool(d.get("verify", True)),
                    d.get("token") or None, d.get("user") or None,
                    int(d.get("wait", 6)))
    return jsonify({"job_id": jid})


@app.route("/api/probe", methods=["POST"])
def api_probe():
    d = request.get_json(force=True)
    ip = (d.get("ip") or "").strip()
    if not ip:
        return jsonify({"error": "需要 ip"}), 400
    try:
        start, end = core.parse_port_range(str(d.get("range", "")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    jid = start_job("probe", task_probe, ip,
                    int(d.get("server_port", 7000)), start, end,
                    int(d.get("concurrency", 8)), int(d.get("wait", 6)),
                    d.get("token") or None, d.get("user") or None,
                    d.get("file") or DEFAULT_FILE,
                    int(d.get("config_ports", 20)),
                    int(d["local_port"]) if d.get("local_port") else None)
    return jsonify({"job_id": jid})


@app.route("/api/bandwidth", methods=["POST"])
def api_bandwidth():
    d = request.get_json(force=True)
    mode = d.get("mode", "single")
    ip = (d.get("ip") or "").strip()
    ips = d.get("ips") or []
    if mode == "single" and not ip:
        return jsonify({"error": "需要 ip"}), 400
    jid = start_job("bandwidth", task_bandwidth, mode, ip, ips,
                    int(d.get("server_port", 7000)),
                    int(d.get("max_servers", 10)),
                    int(d.get("top_ports", 1000)),
                    int(d.get("remote_candidates", 4)),
                    int(d.get("duration", 8)), int(d.get("wait", 6)),
                    d.get("token") or None, d.get("user") or None,
                    d.get("file") or DEFAULT_FILE,
                    bool(d.get("rescan", False)))
    return jsonify({"job_id": jid})


@app.route("/api/files")
def api_files():
    """列出 ~/ 下可作为测试文件的普通文件 (HTTP 服务器共享的是 ~/ )"""
    home = os.path.expanduser("~")
    files = []
    try:
        for name in sorted(os.listdir(home)):
            if name.startswith("."):
                continue
            p = os.path.join(home, name)
            if os.path.isfile(p):
                files.append({"name": name, "size": os.path.getsize(p)})
    except OSError:
        pass
    return jsonify({"files": files, "default": DEFAULT_FILE})


@app.route("/api/portscan", methods=["POST"])
def api_portscan():
    d = request.get_json(force=True)
    ip = (d.get("ip") or "").strip()
    if not ip:
        return jsonify({"error": "需要 ip"}), 400
    jid = start_job("portscan", task_portscan, ip,
                    int(d.get("top_ports", 100)))
    return jsonify({"job_id": jid})


@app.route("/api/portdetail", methods=["POST"])
def api_portdetail():
    d = request.get_json(force=True)
    ip = (d.get("ip") or "").strip()
    ports = d.get("ports") or []
    ports = [int(p) for p in ports]
    if not ip or not ports:
        return jsonify({"error": "需要 ip 和 ports"}), 400
    jid = start_job("portdetail", task_portdetail, ip, ports)
    return jsonify({"job_id": jid})


@app.route("/api/cve", methods=["POST"])
def api_cve():
    d = request.get_json(force=True)
    ip = (d.get("ip") or "").strip()
    ports = [int(p) for p in (d.get("ports") or [])]
    if not ip or not ports:
        return jsonify({"error": "需要 ip 和 ports"}), 400
    jid = start_job("cve", task_cve, ip, ports)
    return jsonify({"job_id": jid})


@app.route("/api/db/stats")
def api_db_stats():
    try:
        servers = core.get_servers(DB_PATH, limit=None, shuffle=False)
        return jsonify({"count": len(servers)})
    except Exception as e:
        return jsonify({"error": str(e), "count": 0})


@app.route("/api/db/list")
def api_db_list():
    """查看 resources.db 的 public_ips 内容 (IP / 描述 等), 支持搜索分页"""
    q = (request.args.get("q") or "").strip()
    page = max(1, int(request.args.get("page", 1)))
    size = min(200, max(1, int(request.args.get("size", 50))))
    where, params = "", []
    if q:
        where = "WHERE server LIKE ? OR description LIKE ?"
        params = [f"%{q}%", f"%{q}%"]
    conn = db_conn()
    total = conn.execute(f"SELECT COUNT(*) FROM public_ips {where}",
                         params).fetchone()[0]
    rows = conn.execute(
        f"SELECT id, server, port, type, is_active, speed, description, "
        f"created_at FROM public_ips {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [size, (page - 1) * size]).fetchall()
    conn.close()
    return jsonify({"total": total, "page": page, "size": size,
                    "rows": [dict(r) for r in rows]})


@app.route("/api/results/list")
def api_results_list():
    """查看保存的带宽测试结果, 支持搜索分页"""
    ensure_results_table()
    q = (request.args.get("q") or "").strip()
    page = max(1, int(request.args.get("page", 1)))
    size = min(200, max(1, int(request.args.get("size", 50))))
    where, params = "", []
    if q:
        where = "WHERE server LIKE ?"
        params = [f"%{q}%"]
    conn = db_conn()
    total = conn.execute(f"SELECT COUNT(*) FROM bandwidth_results {where}",
                         params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM bandwidth_results {where} "
        f"ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [size, (page - 1) * size]).fetchall()
    conn.close()
    return jsonify({"total": total, "page": page, "size": size,
                    "rows": [dict(r) for r in rows]})


@app.route("/api/results/clear", methods=["POST"])
def api_results_clear():
    ensure_results_table()
    conn = db_conn()
    conn.execute("DELETE FROM bandwidth_results")
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/cache/stats")
def api_cache_stats():
    size = 0
    if os.path.exists(core.CACHE_PATH):
        size = os.path.getsize(core.CACHE_PATH)
    with CACHE.lock:
        n = len(CACHE.data)
    return jsonify({"cached_ips": n, "file": core.CACHE_PATH,
                    "size_mb": round(size / 1024 / 1024, 2)})


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    with CACHE.lock:
        CACHE.data = {}
    CACHE.save()
    return jsonify({"ok": True})


@app.route("/api/configs")
def api_configs():
    files = []
    for p in sorted(glob.glob(os.path.join(core.BASE_DIR, "frpc_*.toml"))):
        st = os.stat(p)
        files.append({"name": os.path.basename(p),
                      "size": st.st_size,
                      "mtime": time.strftime("%Y-%m-%d %H:%M",
                                             time.localtime(st.st_mtime))})
    return jsonify({"configs": files})


@app.route("/api/configs/<name>")
def api_config_get(name):
    if "/" in name or ".." in name or not name.endswith(".toml"):
        abort(400)
    path = os.path.join(core.BASE_DIR, name)
    if not os.path.exists(path):
        abort(404)
    if request.args.get("download"):
        return send_file(path, as_attachment=True, download_name=name)
    with open(path) as f:
        return jsonify({"name": name, "content": f.read()})


def main():
    import argparse
    ap = argparse.ArgumentParser(description="FRP 工具箱 Web UI")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    print(f"本地 HTTP 文件服务器: 127.0.0.1:{HTTP_PORT} (共享 ~/)")
    print(f"Web UI: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug,
            threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
