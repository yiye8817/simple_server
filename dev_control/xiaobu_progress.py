import json
import re
import time

from flask import Blueprint, g, jsonify, request

from .db import get_db
from .security import require_user

bp = Blueprint("xiaobu_progress", __name__, url_prefix="/api/xiaobu-words/progress")
DEVICE_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
RECORD_KEY = re.compile(r"[A-Za-z0-9:_-]{1,160}")
KINDS = {"course", "self_practice", "self_word", "review_word", "review_practice",
         "review_session", "town_session", "town_mastery", "town_round", "town_reading"}
MAX_BODY = 512 * 1024
MAX_INTEGER = (1 << 63) - 1


@bp.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("")
@require_user()
def devices():
    rows = get_db().execute("""
        SELECT d.device_id, d.name, d.last_synced_at, COUNT(r.id) AS record_count
        FROM xiaobu_progress_devices d
        LEFT JOIN xiaobu_progress_records r USING(user_id,device_id)
        WHERE d.user_id=? GROUP BY d.device_id ORDER BY d.last_synced_at DESC, d.device_id
    """, (g.user["id"],))
    return jsonify(schema_version=1, devices=[dict(row) for row in rows])


@bp.put("/<device_id>")
@require_user()
def upload(device_id):
    if not DEVICE_ID.fullmatch(device_id):
        return jsonify(error="设备标识无效"), 400
    if not request.is_json:
        return jsonify(error="需要 JSON 请求体"), 415
    if (request.content_length or 0) > MAX_BODY:
        return jsonify(error="进度批次超过 512 KiB"), 413
    raw = request.stream.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        return jsonify(error="进度批次超过 512 KiB"), 413

    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or type(body.get("schema_version")) is not int or body["schema_version"] != 1:
            raise ValueError
        name, revision, records = body.get("device_name"), body.get("revision"), body.get("records")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError
        if type(revision) is not int or not 1 <= revision <= MAX_INTEGER:
            raise ValueError
        if not isinstance(records, list) or len(records) > 50:
            raise ValueError
        encoded = {}
        for record in records:
            if not isinstance(record, dict):
                raise ValueError
            kind, key, data = record.get("kind"), record.get("key"), record.get("data")
            if not isinstance(kind, str) or kind not in KINDS:
                raise ValueError
            if not isinstance(key, str) or not RECORD_KEY.fullmatch(key) or (kind, key) in encoded:
                raise ValueError
            if not isinstance(data, dict):
                raise ValueError
            encoded[kind, key] = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        name = name.strip()
        name.encode("utf-8")
    except (ValueError, RecursionError):
        return jsonify(error="学习进度格式无效"), 400

    received = time.time_ns() // 1_000_000
    user_id = g.user["id"]
    db = get_db()
    with db:
        db.execute("""
            INSERT INTO xiaobu_progress_devices(user_id,device_id,name,last_synced_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id,device_id) DO UPDATE SET name=excluded.name,
              last_synced_at=MAX(last_synced_at,excluded.last_synced_at)
        """, (user_id, device_id, name, received))
        db.executemany("""
            INSERT INTO xiaobu_progress_records(user_id,device_id,kind,record_key,revision,data_json,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id,device_id,kind,record_key) DO UPDATE SET
              data_json=excluded.data_json, revision=excluded.revision,
              updated_at=CASE WHEN data_json<>excluded.data_json THEN excluded.updated_at ELSE updated_at END
            WHERE excluded.revision>revision
        """, ((user_id, device_id, kind, key, revision, data, received) for (kind, key), data in encoded.items()))
    return jsonify(schema_version=1, received=len(encoded))


@bp.get("/<device_id>")
@require_user()
def progress(device_id):
    try:
        after = int(request.args.get("after", "0"))
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        return jsonify(error="分页参数无效"), 400
    kind = request.args.get("kind")
    if not 0 <= after <= MAX_INTEGER or not 1 <= limit <= 50 or (kind is not None and kind not in KINDS):
        return jsonify(error="分页参数或记录类型无效"), 400

    db = get_db()
    scope = (g.user["id"], device_id)
    device = db.execute("""
        SELECT device_id,name,last_synced_at FROM xiaobu_progress_devices WHERE user_id=? AND device_id=?
    """, scope).fetchone()
    if device is None:
        return jsonify(error="未找到同步设备"), 404
    where = "WHERE user_id=? AND device_id=? AND id>?"
    params = (*scope, after)
    if kind is not None:
        where += " AND kind=?"
        params += (kind,)
    rows = db.execute(f"""
        SELECT id,kind,record_key,data_json,updated_at FROM xiaobu_progress_records
        {where} ORDER BY id LIMIT ?
    """, (*params, limit + 1)).fetchall()
    records = [dict(id=row["id"], kind=row["kind"], key=row["record_key"],
                    data=json.loads(row["data_json"]), updated_at=row["updated_at"]) for row in rows[:limit]]
    return jsonify(schema_version=1, device=dict(device), records=records,
                   next_after=records[-1]["id"] if len(rows) > limit else None)
