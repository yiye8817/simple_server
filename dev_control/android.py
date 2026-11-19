"""Owned Android devices, validated policy/tasks and absolute hourly usage snapshots."""
import hashlib
import json
import mimetypes
import os
import re
import secrets
import time
from datetime import date, datetime, time as day_time, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, current_app, g, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

from .db import get_db
from .security import device_for_user, hash_token, new_token, require_device, require_user

bp = Blueprint("android", __name__)
MAX_FILE_SIZE = 512 * 1024 * 1024
MAX_INVENTORY = 2000
HOUR_MS = 3600000
FILE_TTL = 30 * 86400
PACKAGE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
UID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
PROTECTED_PACKAGES = frozenset({
    "android", "com.android.systemui", "com.android.settings", "app.lawnchair.managed",
    "com.android.permissioncontroller", "com.android.packageinstaller",
    "com.google.android.permissioncontroller", "com.google.android.packageinstaller",
    "com.hihonor.android.launcher", "local.lawnchair.homebridge",
    "com.huawei.android.launcher", "com.hihonor.packageinstaller",
})
# These stores can advertise installer handlers. Older clients reported them
# as recovery apps; that stale bit must not exempt ordinary store launches.
CONTROLLABLE_MARKETS = frozenset({"com.hihonor.appmarket", "com.huawei.appmarket"})
ANDROID_KINDS = frozenset({"android.schedule.sync", "android.policy.sync", "android.file.receive", "android.file.open", "android.app.install", "android.app.force_stop"})
OBSERVATION = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}:[1-9][0-9]{0,18}$")
RUNTIME_TTL_MS = 30000


def object_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return body


def text(value, name, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or any(ord(c) < 32 for c in value):
        raise ValueError(name + " 无效")
    return value


def package(value):
    if not isinstance(value, str) or len(value) > 255 or not PACKAGE.fullmatch(value):
        raise ValueError("应用包名无效")
    return value


def integer(value, name, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or value < minimum or value > maximum:
        raise ValueError(name + " 必须是有效整数")
    return value


def timezone_name(value):
    text(value, "timezone", 100)
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone 必须是 IANA 时区") from None
    return value


def capabilities(value):
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError("capabilities 必须是有限的对象")
    for key, val in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key):
            raise ValueError("capabilities 名称无效")
        if key in ("usage_access", "accessibility_enabled", "install_permission", "weekly_schedule") and type(val) is not bool:
            raise ValueError("权限状态必须是布尔值")
        if type(val) is bool:
            continue
        if type(val) is int and 0 <= val <= 2**63 - 1:
            continue
        if isinstance(val, str) and len(val) <= 500 and not any(ord(c) < 32 for c in val):
            continue
        raise ValueError("capabilities 值无效")
    return value


def validated(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as error:
            return jsonify(error=str(error)), 400
    return wrapped


def require_android(fn):
    @wraps(fn)
    @require_device
    def wrapped(*args, **kwargs):
        row = get_db().execute("SELECT * FROM android_devices WHERE device_id=?", (g.device["id"],)).fetchone()
        if row is None:
            return jsonify(error="此设备尚未配对 Android 管理"), 403
        g.android = row
        return fn(*args, **kwargs)
    return wrapped


def managed_device(device_id):
    dev, error = device_for_user(device_id)
    if error:
        return None, error
    android = get_db().execute("SELECT * FROM android_devices WHERE device_id=?", (device_id,)).fetchone()
    if android is None:
        return None, (jsonify(error="Android 设备不存在"), 404)
    return (dev, android), None


def policy(row):
    return {"revision": row["policy_revision"], "layout": "paged", "apps": json.loads(row["policy_apps_json"])}


def app_protected(package_name, reported):
    """Keep genuine recovery protection while ignoring stale store reports."""
    return package_name in PROTECTED_PACKAGES or (reported and package_name not in CONTROLLABLE_MARKETS)


def effective_apps(android):
    # Normalize reads as well as new uploads so a server upgrade immediately
    # enables existing store controls without mutating the stored inventory.
    return [{**app, "protected": app_protected(app["package_name"], app["protected"])}
            for app in json.loads(android["apps_json"])]


def summary(dev, android):
    return {"id": dev["id"], "device_uid": dev["device_uid"], "name": dev["name"],
            "online": bool(dev["online"] and dev["last_seen"] and dev["last_seen"] >= int(time.time()) - 120),
            "last_seen": dev["last_seen"], "timezone": android["timezone"],
            "policy_revision": android["policy_revision"], "capabilities": json.loads(android["capabilities_json"]),
            "apps_count": len(json.loads(android["apps_json"]))}


def enqueue(device_id, kind, payload):
    cur = get_db().execute("INSERT INTO tasks(device_id,kind,payload_json,status,created_by,created_at) VALUES(?,?,?,?,?,?)",
                           (device_id, kind, json.dumps(payload, ensure_ascii=False), "pending", g.user["id"], int(time.time())))
    return cur.lastrowid


def observation_id(value):
    if not isinstance(value, str) or not OBSERVATION.fullmatch(value) or int(value.rsplit(":", 1)[1]) > 2**63 - 1:
        raise ValueError("observation_id 必须为 UUID 和正整数序号")
    return value


def runtime_status(dev, android, row, now):
    """Use receipt age, never the device wall clock, for action eligibility."""
    apps = {app["package_name"]: app for app in effective_apps(android)}
    def enrichment(pkg):
        app = apps.get(pkg)
        return {"label": (app["label"] or pkg) if app else pkg,
                "protected": app["protected"] if app else pkg in PROTECTED_PACKAGES,
                "launchable": bool(app and app["launchable"])}
    runtime = None
    stale = row is None or not 0 <= now - row["received_at"] < RUNTIME_TTL_MS
    online = bool(dev["active"] and summary(dev, android)["online"])
    if row is not None:
        runtime = {key: row[key] for key in ("observation_id", "state", "package_name", "observed_at", "received_at")}
        runtime.update(can_force_stop=bool(row["can_force_stop"]), **enrichment(row["package_name"]))
        runtime["recent"] = [{**recent, **enrichment(recent["package_name"])} for recent in json.loads(row["recent_json"])]
    if not online:
        reason = "设备离线，等待重新连接并上报当前应用"
    elif row is None:
        reason = "等待当前应用报告；请更新平板 Launcher 并开启无障碍服务"
    elif stale:
        reason = "当前应用报告已过期，等待平板重新上报"
    elif row["state"] != "foreground":
        reason = {"locked": "平板已锁屏，无法强行停止", "unavailable": "无障碍服务未连接，请在平板开启服务", "unknown": "尚无法确认当前前台应用"}[row["state"]]
    elif not row["can_force_stop"]:
        reason = "当前无法强行停止，请检查平板无障碍服务或等待本地操作完成"
    elif runtime["protected"]:
        reason = "关键系统或恢复应用受到保护"
    elif not runtime["launchable"]:
        reason = "当前应用不在可启动应用清单中，请等待清单更新"
    else:
        reason = ""
    return {"runtime": runtime, "stale": stale, "online": online, "server_time": now,
            "force_stop_allowed": not reason, "blocked_reason": reason}


@bp.post("/api/android/agent/runtime")
@require_android
@validated
def agent_runtime():
    data = object_body()
    if set(data) != {"observation_id", "state", "package_name", "observed_at", "can_force_stop", "recent"}:
        raise ValueError("当前应用报告字段无效")
    identity = observation_id(data["observation_id"])
    state = data["state"]
    if not isinstance(state, str) or state not in ("foreground", "locked", "unavailable", "unknown"):
        raise ValueError("当前应用状态无效")
    pkg = package(data["package_name"]) if state == "foreground" else data["package_name"]
    observed = integer(data["observed_at"], "observed_at")
    capable = data["can_force_stop"]
    if type(capable) is not bool or (state != "foreground" and (pkg != "" or capable)):
        raise ValueError("非前台状态须清空包名且不能强行停止")
    recent = data["recent"]
    if not isinstance(recent, list) or len(recent) > 10:
        raise ValueError("最近活动须为至多 10 项的列表")
    seen = set()
    for item in recent:
        if not isinstance(item, dict) or set(item) != {"package_name", "last_seen"}:
            raise ValueError("最近活动字段无效")
        name = package(item["package_name"])
        integer(item["last_seen"], "last_seen")
        if name in seen:
            raise ValueError("最近活动包名不能重复")
        seen.add(name)
    db = get_db()
    now = int(time.time() * 1000)
    db.execute("BEGIN IMMEDIATE")
    try:
        old = db.execute("SELECT * FROM android_runtime WHERE device_id=?", (g.device["id"],)).fetchone()
        if old:
            reporter, sequence = identity.rsplit(":", 1)
            old_reporter, old_sequence = old["observation_id"].rsplit(":", 1)
            if (reporter.lower() == old_reporter.lower() and int(sequence) < int(old_sequence)) or (
                    identity.lower() == old["observation_id"].lower() and (state != old["state"] or pkg != old["package_name"])):
                db.rollback()
                return jsonify(error="当前应用观察已被更新，不能复用旧观察标识"), 409
        db.execute("INSERT INTO android_runtime(device_id,observation_id,state,package_name,observed_at,can_force_stop,recent_json,received_at) "
                   "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET observation_id=excluded.observation_id,state=excluded.state,"
                   "package_name=excluded.package_name,observed_at=excluded.observed_at,can_force_stop=excluded.can_force_stop,"
                   "recent_json=excluded.recent_json,received_at=excluded.received_at",
                   (g.device["id"], identity, state, pkg, observed, int(capable), json.dumps(recent), now))
        db.execute("UPDATE devices SET online=1,last_seen=? WHERE id=?", (now // 1000, g.device["id"]))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify(ok=True, server_time=now)


@bp.get("/api/android/devices/<int:device_id>/runtime")
@require_user()
def runtime(device_id):
    pair, error = managed_device(device_id)
    if error:
        return error
    dev, android = pair
    db = get_db()
    result = runtime_status(dev, android, db.execute("SELECT * FROM android_runtime WHERE device_id=?", (device_id,)).fetchone(), int(time.time() * 1000))
    labels = {app["package_name"]: app["label"] for app in effective_apps(android)}
    tasks = []
    for row in db.execute("SELECT * FROM tasks WHERE device_id=? AND kind='android.app.force_stop' ORDER BY id DESC LIMIT 10", (device_id,)):
        pkg = json.loads(row["payload_json"]).get("package_name", "")
        outcome = json.loads(row["result_json"]) if row["result_json"] else {}
        tasks.append({"id": row["id"], "package_name": pkg, "label": labels.get(pkg) or pkg,
                      "status": row["status"], "result": outcome.get("result"), "created_at": row["created_at"], "finished_at": row["finished_at"]})
    result["exit_tasks"] = tasks
    if db.execute("SELECT 1 FROM tasks WHERE device_id=? AND kind='android.app.force_stop' AND status IN ('pending','claimed','waiting_user') LIMIT 1", (device_id,)).fetchone():
        result.update(force_stop_allowed=False, blocked_reason="已有强行停止任务等待平板处理")
    return jsonify(result)


@bp.post("/api/android/devices/<int:device_id>/apps/force-stop")
@require_user()
@validated
def force_stop(device_id):
    _, error = managed_device(device_id)
    if error:
        return error
    data = object_body()
    if set(data) != {"package_name", "observation_id"}:
        raise ValueError("强行停止须且只能包含 package_name 和 observation_id")
    pkg = package(data["package_name"])
    identity = observation_id(data["observation_id"])
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        dev = db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        android = db.execute("SELECT * FROM android_devices WHERE device_id=?", (device_id,)).fetchone()
        row = db.execute("SELECT * FROM android_runtime WHERE device_id=?", (device_id,)).fetchone()
        now = int(time.time() * 1000)
        status = runtime_status(dev, android, row, now)
        if not status["force_stop_allowed"]:
            db.rollback()
            return jsonify(error=status["blocked_reason"]), 409
        if pkg != row["package_name"] or identity != row["observation_id"]:
            db.rollback()
            return jsonify(error="当前前台应用已变化，请刷新后重试"), 409
        if db.execute("SELECT 1 FROM tasks WHERE device_id=? AND kind='android.app.force_stop' AND status IN ('pending','claimed','waiting_user')", (device_id,)).fetchone():
            db.rollback()
            return jsonify(error="已有强行停止任务等待平板处理"), 409
        expires = now + RUNTIME_TTL_MS
        task_id = enqueue(device_id, "android.app.force_stop", {"package_name": pkg, "observation_id": identity, "expires_at": expires})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify(id=task_id, status="pending", expires_at=expires), 201


@bp.get("/android/")
def page():
    return render_template("android.html")


@bp.post("/api/android/pairing-codes")
@require_user()
def pairing_code():
    now = int(time.time())
    code = secrets.token_hex(6)
    db = get_db()
    db.execute("DELETE FROM android_pairing_codes WHERE expires_at<=? OR consumed_at IS NOT NULL", (now,))
    db.execute("INSERT INTO android_pairing_codes(code_hash,owner_id,expires_at,created_at) VALUES(?,?,?,?)",
               (hash_token(code), g.user["id"], now + 600, now))
    db.commit()
    return jsonify(code=code, expires_at=now + 600)


@bp.post("/api/android/pair")
@validated
def pair():
    data = object_body()
    code = data.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[0-9a-f]{12}", code):
        raise ValueError("配对码无效或已过期")
    uid = data.get("device_uid")
    if not isinstance(uid, str) or not UID.fullmatch(uid):
        raise ValueError("device_uid 无效")
    name = text(data.get("name"), "设备名称", 200)
    caps = capabilities(data.get("capabilities", {}))
    now = int(time.time())
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        pairing = db.execute("SELECT p.* FROM android_pairing_codes p JOIN users u ON p.owner_id=u.id "
                             "WHERE p.code_hash=? AND p.expires_at>? AND p.consumed_at IS NULL AND u.active=1",
                             (hash_token(code), now)).fetchone()
        if pairing is None:
            db.rollback()
            return jsonify(error="配对码无效、已使用或已过期"), 400
        if db.execute("SELECT id FROM devices WHERE device_uid=?", (uid,)).fetchone():
            db.rollback()
            return jsonify(error="设备标识已注册，请在设备本地断开并重新配对"), 409
        token = new_token()
        cur = db.execute("INSERT INTO devices(device_uid,name,token_hash,owner_id,online,last_seen,created_at) VALUES(?,?,?,?,?,?,?)",
                         (uid, name, hash_token(token), pairing["owner_id"], 1, now, now))
        device_id = cur.lastrowid
        db.execute("INSERT INTO android_devices(device_id,capabilities_json) VALUES(?,?)",
                   (device_id, json.dumps(caps, ensure_ascii=False)))
        db.execute("UPDATE android_pairing_codes SET consumed_at=? WHERE code_hash=?", (now, hash_token(code)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify(device={"id": device_id, "device_uid": uid, "name": name, "device_token": token}, server_time=now)


@bp.get("/api/android/devices")
@require_user()
def devices():
    clause = "" if g.user["role"] == "admin" else " AND d.owner_id=?"
    args = () if not clause else (g.user["id"],)
    rows = get_db().execute("SELECT d.* FROM devices d JOIN android_devices a ON d.id=a.device_id WHERE d.active=1" + clause + " ORDER BY d.id", args).fetchall()
    return jsonify(devices=[summary(r, get_db().execute("SELECT * FROM android_devices WHERE device_id=?", (r["id"],)).fetchone()) for r in rows])


@bp.get("/api/android/devices/<int:device_id>")
@require_user()
def device(device_id):
    pair, error = managed_device(device_id)
    if error:
        return error
    dev, android = pair
    return jsonify(device=summary(dev, android), policy=policy(android), apps=effective_apps(android))


@bp.put("/api/android/devices/<int:device_id>/policy")
@require_user()
@validated
def put_policy(device_id):
    pair, error = managed_device(device_id)
    if error:
        return error
    rules = object_body().get("apps")
    if not isinstance(rules, dict) or len(rules) > MAX_INVENTORY:
        raise ValueError("apps 必须是至多 2000 项的规则对象")
    db = get_db()
    # Serialize inventory/protection updates with policy replacement.
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM android_devices WHERE device_id=?", (device_id,)).fetchone()
        protected = PROTECTED_PACKAGES | {app["package_name"] for app in json.loads(row["apps_json"]) if app_protected(app["package_name"], app["protected"])}
        for pkg, rule in rules.items():
            package(pkg)
            if not isinstance(rule, dict) or set(rule) != {"allowed", "visible"} or type(rule["allowed"]) is not bool or type(rule["visible"]) is not bool:
                raise ValueError("每条规则必须包含 allowed 和 visible 布尔值")
            if pkg in protected and (not rule["allowed"] or not rule["visible"]):
                raise ValueError("恢复及系统关键应用不能停用或隐藏")
        db.execute("UPDATE android_devices SET policy_apps_json=?,policy_revision=policy_revision+1 WHERE device_id=?",
                   (json.dumps(rules, ensure_ascii=False), device_id))
        row = db.execute("SELECT * FROM android_devices WHERE device_id=?", (device_id,)).fetchone()
        enqueue(device_id, "android.policy.sync", {})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify(policy=policy(row))


@bp.get("/api/android/agent/state")
@require_android
def agent_state():
    return jsonify(device_id=g.device["id"], policy=policy(g.android), server_time=int(time.time()))


@bp.post("/api/android/agent/inventory")
@require_android
@validated
def agent_inventory():
    data = object_body()
    name = text(data.get("name"), "设备名称", 200)
    zone = timezone_name(data.get("timezone"))
    caps = capabilities(data.get("capabilities"))
    apps = data.get("apps")
    if not isinstance(apps, list) or len(apps) > MAX_INVENTORY:
        raise ValueError("apps 必须为至多 2000 项的列表")
    seen = set()
    normalized = []
    for app in apps:
        if not isinstance(app, dict):
            raise ValueError("应用信息必须为对象")
        pkg = package(app.get("package_name"))
        if pkg in seen:
            raise ValueError("应用列表包名不能重复")
        seen.add(pkg)
        label = text(app.get("label"), "应用名称", 500, empty=True)
        version_name = text(app.get("version_name"), "应用版本", 200, empty=True)
        version_code = integer(app.get("version_code"), "version_code")
        if any(type(app.get(key)) is not bool for key in ("system", "launchable", "protected")):
            raise ValueError("应用状态必须为布尔值")
        normalized.append({"package_name": pkg, "label": label, "version_name": version_name, "version_code": version_code,
                           "system": app["system"], "launchable": app["launchable"], "protected": app_protected(pkg, app["protected"])})
    db = get_db()
    now = int(time.time())
    db.execute("BEGIN IMMEDIATE")
    try:
        old = db.execute("SELECT * FROM android_devices WHERE device_id=?", (g.device["id"],)).fetchone()
        rules = json.loads(old["policy_apps_json"])
        # A newly active keyboard/recovery app must remain reachable even if it had an old rule.
        filtered = {k: v for k, v in rules.items() if k not in {a["package_name"] for a in normalized if a["protected"]} or (v["allowed"] and v["visible"])}
        db.execute("UPDATE android_devices SET apps_json=?,timezone=?,capabilities_json=?,inventory_updated_at=?,policy_apps_json=?,policy_revision=policy_revision+? WHERE device_id=?",
                   (json.dumps(normalized, ensure_ascii=False), zone, json.dumps(caps, ensure_ascii=False), now,
                    json.dumps(filtered, ensure_ascii=False), int(filtered != rules), g.device["id"]))
        db.execute("UPDATE devices SET name=?,last_seen=?,online=1 WHERE id=?", (name, now, g.device["id"]))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify(ok=True)


@bp.post("/api/android/agent/usage")
@require_android
@validated
def agent_usage():
    data = object_body()
    zone = timezone_name(data.get("timezone"))
    granted = data.get("permission_granted")
    if type(granted) is not bool:
        raise ValueError("permission_granted 必须为布尔值")
    generated = integer(data.get("generated_at"), "generated_at")
    start = integer(data.get("window_start"), "window_start")
    end = integer(data.get("window_end"), "window_end")
    if start % HOUR_MS or not start < end <= generated or end - start > 72 * HOUR_MS:
        raise ValueError("统计窗口须从 UTC 整点开始、至多 72 小时且不晚于生成时间")
    buckets = data.get("buckets")
    if not isinstance(buckets, list) or len(buckets) > 10000:
        raise ValueError("buckets 必须为至多 10000 项的列表")
    normalized = []
    seen = set()
    for bucket in buckets:
        if not isinstance(bucket, dict):
            raise ValueError("每个统计时段必须为对象")
        pkg = package(bucket.get("package_name"))
        hour = integer(bucket.get("bucket_start"), "bucket_start")
        duration = integer(bucket.get("duration_ms"), "duration_ms", maximum=HOUR_MS)
        if hour % HOUR_MS or not start <= hour < end:
            raise ValueError("统计时段须为窗口内的 UTC 整点")
        if (pkg, hour) in seen:
            raise ValueError("同一应用时段不能重复")
        seen.add((pkg, hour))
        normalized.append((g.device["id"], pkg, hour, duration))
    if not granted and buckets:
        raise ValueError("未授权时不能上报统计值")
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        if granted:
            db.execute("DELETE FROM android_usage WHERE device_id=? AND bucket_start>=? AND bucket_start<?", (g.device["id"], start, end))
            db.executemany("INSERT INTO android_usage(device_id,package_name,bucket_start,duration_ms) VALUES(?,?,?,?)", normalized)
        db.execute("UPDATE android_devices SET timezone=?,usage_permission=?,usage_last_synced_at=? WHERE device_id=?",
                   (zone, int(granted), int(time.time()), g.device["id"]))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify(ok=True)


@bp.get("/api/android/devices/<int:device_id>/usage")
@require_user()
@validated
def usage(device_id):
    pair, error = managed_device(device_id)
    if error:
        return error
    _, android = pair
    raw = request.args.get("date", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ValueError("date 必须为 YYYY-MM-DD")
    try:
        requested = date.fromisoformat(raw)
        zone = ZoneInfo(android["timezone"])
        start = int(datetime.combine(requested, day_time(), zone).timestamp() * 1000)
        end = int(datetime.combine(requested + timedelta(days=1), day_time(), zone).timestamp() * 1000)
    except (ValueError, OverflowError):
        raise ValueError("日期无效") from None
    # Assign UTC-hour aggregates by their local start date; repeated DST hours retain distinct epochs.
    first = ((start + HOUR_MS - 1) // HOUR_MS) * HOUR_MS
    hours = {hour: 0 for hour in range(first, end, HOUR_MS)}
    apps = {}
    labels = {a["package_name"]: a["label"] for a in json.loads(android["apps_json"])}
    for row in get_db().execute("SELECT * FROM android_usage WHERE device_id=? AND bucket_start>=? AND bucket_start<? ORDER BY bucket_start,package_name", (device_id, start, end)):
        if not row["duration_ms"]:
            continue
        pkg = row["package_name"]
        app = apps.setdefault(pkg, {"package_name": pkg, "label": labels.get(pkg, pkg), "total_ms": 0, "hours": []})
        app["total_ms"] += row["duration_ms"]
        app["hours"].append({"bucket_start": row["bucket_start"], "duration_ms": row["duration_ms"]})
        hours[row["bucket_start"]] += row["duration_ms"]
    return jsonify(date=raw, timezone=android["timezone"],
                   permission_granted=None if android["usage_permission"] is None else bool(android["usage_permission"]),
                   last_synced_at=android["usage_last_synced_at"], apps=sorted(apps.values(), key=lambda a: (-a["total_ms"], a["package_name"])),
                   hours=[{"bucket_start": key, "duration_ms": value} for key, value in hours.items()])


def filename_arg(value):
    text(value, "文件名", 240)
    if value in (".", "..") or "/" in value or "\\" in value or len(value.encode("utf-8")) > 240:
        raise ValueError("文件名不能包含路径且不得超过 240 字节")
    return value


def flag_arg(name, default="0"):
    value = request.args.get(name, default)
    if value not in ("0", "1"):
        raise ValueError(name + " 必须为 0 或 1")
    return value == "1"


def file_payload(row, open_file):
    return {"file_id": row["id"], "filename": row["filename"], "mime": row["mime"], "size": row["size"],
            "sha256": row["sha256"], "blob_token": row["blob_token"], "open": open_file}


def file_summary(row):
    result = json.loads(row["result_json"]) if row["result_json"] else None
    return {"id": row["id"], "filename": row["filename"], "mime": row["mime"], "size": row["size"], "sha256": row["sha256"],
            "blob_token": row["blob_token"], "task_id": row["task_id"], "status": row["status"],
            "result": result.get("result") if isinstance(result, dict) else None,
            "created_at": row["created_at"], "expires_at": row["expires_at"]}


def files_query():
    return "SELECT f.*,t.status,t.result_json FROM android_files f LEFT JOIN tasks t ON t.id=f.task_id "


@bp.route("/api/android/devices/<int:device_id>/files", methods=["GET", "POST"])
@require_user()
@validated
def files(device_id):
    pair, error = managed_device(device_id)
    if error:
        return error
    db = get_db()
    if request.method == "GET":
        rows = db.execute(files_query() + "WHERE f.device_id=? ORDER BY f.id DESC LIMIT 500", (device_id,)).fetchall()
        return jsonify(files=[file_summary(row) for row in rows])
    name = filename_arg(request.args.get("filename", ""))
    open_file = flag_arg("open")
    install = flag_arg("install")
    if install and not name.lower().endswith(".apk"):
        raise ValueError("仅 APK 文件支持安装")
    if request.content_length is not None and request.content_length > MAX_FILE_SIZE:
        raise RequestEntityTooLarge()
    mime = "application/vnd.android.package-archive" if name.lower().endswith(".apk") else mimetypes.guess_type(name)[0] or "application/octet-stream"
    token = secrets.token_urlsafe(24)
    directory = Path(current_app.config["DATA_DIR"]) / "blobs"
    temporary = directory / (token + ".part")
    target = directory / (token + ".bin")
    sha = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as stream:
            while True:
                chunk = request.stream.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise RequestEntityTooLarge()
                stream.write(chunk)
                sha.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        now = int(time.time())
        expires = now + FILE_TTL
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO blobs(token,device_id,owner_id,direction,filename,size,sha256,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                   (token, device_id, g.user["id"], "push", name, size, sha.hexdigest(), expires))
        cur = db.execute("INSERT INTO android_files(device_id,filename,mime,size,sha256,blob_token,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                         (device_id, name, mime, size, sha.hexdigest(), token, now, expires))
        row = db.execute("SELECT * FROM android_files WHERE id=?", (cur.lastrowid,)).fetchone()
        task_id = enqueue(device_id, "android.app.install" if install else "android.file.receive", file_payload(row, open_file))
        db.execute("UPDATE android_files SET task_id=? WHERE id=?", (task_id, row["id"]))
        db.commit()
    except Exception:
        db.rollback()
        target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    row = db.execute(files_query() + "WHERE f.id=?", (row["id"],)).fetchone()
    return jsonify(file=file_summary(row)), 201


@bp.post("/api/android/devices/<int:device_id>/files/<int:file_id>/open")
@require_user()
def reopen(device_id, file_id):
    pair, error = managed_device(device_id)
    if error:
        return error
    db = get_db()
    row = db.execute("SELECT * FROM android_files WHERE device_id=? AND id=?", (device_id, file_id)).fetchone()
    if row is None:
        return jsonify(error="文件不存在"), 404
    # The device may still retain a local copy after the server download expires.
    task_id = enqueue(device_id, "android.file.open", file_payload(row, True))
    db.execute("UPDATE android_files SET task_id=? WHERE id=?", (task_id, file_id))
    db.commit()
    return jsonify(task_id=task_id, status="pending"), 201


@bp.get("/api/android/agent/recover-tasks")
@require_android
def recover_tasks():
    raw_after = request.args.get("after_id", "0")
    try:
        if not raw_after.isascii() or not raw_after.isdecimal() or len(raw_after) > 19:
            raise ValueError("after_id 必须是非负整数")
        after = integer(int(raw_after), "after_id")
    except ValueError as error:
        return jsonify(error=str(error)), 400
    rows = get_db().execute("SELECT * FROM tasks WHERE device_id=? AND status IN ('claimed','waiting_user') "
                            "AND claim_token IS NOT NULL AND id>? AND kind IN (" + ",".join("?" for _ in ANDROID_KINDS) + ") ORDER BY id LIMIT 101",
                            (g.device["id"], after, *sorted(ANDROID_KINDS))).fetchall()
    more = len(rows) > 100
    rows = rows[:100]
    return jsonify(tasks=[{"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload_json"]),
                           "claim_token": row["claim_token"], "status": row["status"]} for row in rows],
                   next_after=rows[-1]["id"] if more else None)
