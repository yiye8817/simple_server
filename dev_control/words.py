"""Word Atlas storage, APIs, importing and source synchronization."""
import concurrent.futures
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from flask import Blueprint, current_app, has_app_context, jsonify, request

from .db import get_db
from .security import require_user

bp = Blueprint("words", __name__)
DEFAULT_SOURCE_BASE = "https://0xlei.tech/words/atlas/"
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_LIST_FIELDS = ("col", "fam", "syn", "ant")


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _string(value, field, *, required=False, max_len=10000):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} 不能为空")
    if len(value) > max_len:
        raise ValueError(f"{field} 过长")
    return value


def _string_list(value, field):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{field} 必须是字符串数组")
    return [x.strip() for x in value if x.strip()]


def _object_list(value, field, keys):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是数组")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{field} 的元素必须是对象")
        result.append({key: _string(item.get(key), f"{field}.{key}") for key in keys})
    return result


def validate_entry(payload):
    if not isinstance(payload, dict):
        raise ValueError("词条必须是对象")
    source = _json_copy(payload)
    entry_id = _string(source.get("id"), "id", required=True, max_len=120).lower()
    if not _ID_RE.fullmatch(entry_id):
        raise ValueError("id 格式无效")
    word = _string(source.get("w", source.get("word")), "w", required=True, max_len=200)
    senses = _object_list(source.get("senses"), "senses", ("pos", "zh", "en"))
    examples = _object_list(source.get("ex", source.get("examples")), "ex", ("en", "zh"))
    collocations = _string_list(source.get("col", source.get("collocations")), "col")
    family = _string_list(source.get("fam", source.get("family")), "fam")
    synonyms = _string_list(source.get("syn", source.get("synonyms")), "syn")
    antonyms = _string_list(source.get("ant", source.get("antonyms")), "ant")
    zh = _string(source.get("zh") or (senses[0]["zh"] if senses else ""), "zh")
    uk = _string(source.get("uk"), "uk")
    us = _string(source.get("us"), "us")
    cefr = _string(source.get("cefr"), "cefr", max_len=20)
    topic = _string(source.get("topic"), "topic", max_len=100)
    media = source.get("m", source.get("media", 0))
    if isinstance(media, bool) or not isinstance(media, int) or media < 0:
        raise ValueError("media 必须是非负整数")
    tip = _string(source.get("tip"), "tip")
    canonical = {
        "id": entry_id, "w": word, "uk": uk, "us": us, "cefr": cefr,
        "topic": topic, "senses": senses, "ex": examples, "tip": tip,
        "col": collocations, "fam": family, "syn": synonyms, "ant": antonyms,
        "m": media,
    }
    canonical.update({k: v for k, v in source.items() if k not in canonical})
    normalized = {
        "id": entry_id, "word": word, "zh": zh, "uk_ipa": uk, "us_ipa": us,
        "cefr": cefr, "topic": topic, "media": media, "senses": senses,
        "examples": examples, "tip": tip, "collocations": collocations,
        "family": family, "synonyms": synonyms, "antonyms": antonyms,
        "raw_json": source,
    }
    normalized["content_hash"] = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return normalized


def asset_urls(entry_id, base_url=DEFAULT_SOURCE_BASE):
    entry_id = _string(entry_id, "id", required=True, max_len=120).lower()
    if not _ID_RE.fullmatch(entry_id):
        raise ValueError("id 格式无效")
    base = str(base_url or DEFAULT_SOURCE_BASE).rstrip("/")
    safe = quote(entry_id, safe="-")
    return {
        "images": {
            "atlas": f"{base}/img/{safe}.webp",
            "classic": f"{base}/classic/img/{safe}.webp",
        },
        "audio": {
            "uk": f"{base}/audio/uk/{safe}.mp3",
            "us": f"{base}/audio/us/{safe}.mp3",
        },
    }


_ASSET_LAYOUT = {
    "images": {
        "atlas": ("img", ".webp"),
        "classic": ("classic/img", ".webp"),
    },
    "audio": {
        "uk": ("audio/uk", ".mp3"),
        "us": ("audio/us", ".mp3"),
    },
}


def _asset_file_map(entry_id, root):
    entry_id = _string(entry_id, "id", required=True, max_len=120).lower()
    if not _ID_RE.fullmatch(entry_id):
        raise ValueError("id 格式无效")
    root = Path(root)
    return {
        group: {
            name: (root / relative / f"{entry_id}{suffix}", f"/words/assets/{relative}/{entry_id}{suffix}")
            for name, (relative, suffix) in variants.items()
        }
        for group, variants in _ASSET_LAYOUT.items()
    }


def _local_asset_urls(entry_id):
    if not has_app_context():
        return {}
    root = current_app.config.get("WORDS_ASSET_DIR")
    if not root:
        return {}
    result = {group: {} for group in _ASSET_LAYOUT}
    for group, variants in _asset_file_map(entry_id, root).items():
        for name, (path, url) in variants.items():
            if path.is_file() and path.stat().st_size > 0:
                result[group][name] = url
    return result


def entry_from_row(row, base_url=None):
    if row is None:
        return None
    get = row.__getitem__
    assets = asset_urls(
        get("id"),
        (get("source_url") if isinstance(get("source_url"), str) and get("source_url").startswith("https://") else None)
        or base_url or DEFAULT_SOURCE_BASE,
    )
    for group, variants in _local_asset_urls(get("id")).items():
        for name, url in variants.items():
            assets[group][name] = url
    return {
        "id": get("id"), "word": get("word"), "zh": get("zh"),
        "uk_ipa": get("uk_ipa"), "us_ipa": get("us_ipa"), "cefr": get("cefr"),
        "topic": get("topic"), "media": get("media"),
        "senses": json.loads(get("senses_json") or "[]"),
        "examples": json.loads(get("examples_json") or "[]"),
        "tip": get("tip"), "collocations": json.loads(get("collocations_json") or "[]"),
        "family": json.loads(get("family_json") or "[]"),
        "synonyms": json.loads(get("synonyms_json") or "[]"),
        "antonyms": json.loads(get("antonyms_json") or "[]"),
        "source_url": get("source_url"), "source_updated_at": get("source_updated_at"),
        "updated_at": get("updated_at"), "assets": assets,
    }


def _allowed_source_hosts():
    configured = current_app.config.get("WORDS_SOURCE_ALLOWLIST", "0xlei.tech") if has_app_context() else "0xlei.tech"
    return {host.strip().lower().rstrip(".") for host in str(configured).split(",") if host.strip()}


def _validate_source_url(url):
    parsed = urlsplit(str(url))
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("source_base 必须是无凭据、无查询参数或片段的 HTTPS 地址")
    hostname = parsed.hostname.lower().rstrip(".")
    allowed = _allowed_source_hosts()
    if hostname not in allowed and not any(hostname.endswith("." + host) for host in allowed):
        raise ValueError("source_base 不在允许的来源域名中")
    return parsed


def _validate_source_base(base_url):
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("source_base 必须是字符串")
    parsed = _validate_source_url(base_url.strip())
    return parsed.geturl().rstrip("/") + "/"


def _source_base():
    return _validate_source_base(str(current_app.config.get("WORDS_SOURCE_BASE", DEFAULT_SOURCE_BASE)))


def _row_params(entry, source_url, source_updated_at, now):
    return (
        entry["id"], entry["word"], entry["zh"], entry["uk_ipa"], entry["us_ipa"], entry["cefr"], entry["topic"], entry["media"],
        json.dumps(entry["senses"], ensure_ascii=False), json.dumps(entry["examples"], ensure_ascii=False), entry["tip"],
        json.dumps(entry["collocations"], ensure_ascii=False), json.dumps(entry["family"], ensure_ascii=False),
        json.dumps(entry["synonyms"], ensure_ascii=False), json.dumps(entry["antonyms"], ensure_ascii=False),
        json.dumps(entry["raw_json"], ensure_ascii=False, sort_keys=True), entry["content_hash"], source_url, source_updated_at, now,
    )


def upsert_entries(entries, replace=False, dry_run=False, source_url="", source_updated_at=""):
    normalized = [validate_entry(entry) for entry in entries]
    ids = [entry["id"] for entry in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("批次包含重复 id")
    db = get_db()
    existing = {row["id"]: row for row in db.execute("SELECT id,content_hash FROM word_entries").fetchall()}
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0, "errors": []}
    for entry in normalized:
        if entry["id"] not in existing:
            counts["inserted"] += 1
        elif existing[entry["id"]]["content_hash"] == entry["content_hash"]:
            counts["unchanged"] += 1
        else:
            counts["updated"] += 1
    if dry_run:
        return counts
    now = int(time.time())
    try:
        db.execute("BEGIN IMMEDIATE")
        for entry in normalized:
            db.execute("""INSERT INTO word_entries
                (id,word,zh,uk_ipa,us_ipa,cefr,topic,media,senses_json,examples_json,tip,
                 collocations_json,family_json,synonyms_json,antonyms_json,raw_json,content_hash,
                 source_url,source_updated_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET word=excluded.word,zh=excluded.zh,uk_ipa=excluded.uk_ipa,
                 us_ipa=excluded.us_ipa,cefr=excluded.cefr,topic=excluded.topic,media=excluded.media,
                 senses_json=excluded.senses_json,examples_json=excluded.examples_json,tip=excluded.tip,
                 collocations_json=excluded.collocations_json,family_json=excluded.family_json,
                 synonyms_json=excluded.synonyms_json,antonyms_json=excluded.antonyms_json,raw_json=excluded.raw_json,
                 content_hash=excluded.content_hash,source_url=excluded.source_url,source_updated_at=excluded.source_updated_at,
                 updated_at=excluded.updated_at""", _row_params(entry, source_url, source_updated_at, now))
        if replace:
            keep = set(ids)
            rows = db.execute("SELECT id FROM word_entries WHERE source_url=?", (source_url,)).fetchall()
            for row in rows:
                if row["id"] not in keep:
                    db.execute("DELETE FROM word_entries WHERE id=?", (row["id"],))
                    counts["deleted"] += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return counts


def _get_word(word_id):
    return get_db().execute("SELECT * FROM word_entries WHERE id=?", (word_id.lower(),)).fetchone()


@bp.post("/api/words")
@require_user(admin=True)
def create_word():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="请求体必须是单个词光 JSON 对象"), 400
    try:
        normalized = validate_entry(payload)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if _get_word(normalized["id"]) is not None:
        return jsonify(error="单词 ID 已存在"), 409
    try:
        upsert_entries([payload], source_url="manual")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(item=entry_from_row(_get_word(normalized["id"]), _source_base())), 201


@bp.get("/api/words")
def list_words():
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = min(100, max(1, int(request.args.get("page_size", 40))))
    except (TypeError, ValueError):
        return jsonify(error="page 和 page_size 必须是整数"), 400
    q = str(request.args.get("q", "")).strip().lower()
    topic = str(request.args.get("topic", "")).strip()
    cefr = str(request.args.get("cefr", "")).strip()
    clauses, params = [], []
    if q:
        escaped_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(LOWER(word) LIKE ? ESCAPE '\\' OR LOWER(zh) LIKE ? ESCAPE '\\')")
        params.extend([f"%{escaped_q}%", f"%{escaped_q}%"])
    if topic:
        clauses.append("topic=?"); params.append(topic)
    if cefr:
        clauses.append("cefr=?"); params.append(cefr)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    db = get_db()
    total = db.execute(f"SELECT COUNT(*) FROM word_entries{where}", params).fetchone()[0]
    pages = math.ceil(total / page_size) if total else 0
    offset = (page - 1) * page_size
    rows = db.execute(f"SELECT * FROM word_entries{where} ORDER BY word COLLATE NOCASE, id LIMIT ? OFFSET ?", params + [page_size, offset]).fetchall()
    return jsonify(items=[entry_from_row(row, _source_base()) for row in rows], total=total, page=page, page_size=page_size, pages=pages)


@bp.get("/api/words/<word_id>")
def get_word(word_id):
    row = _get_word(word_id)
    if row is None:
        return jsonify(error="单词不存在"), 404
    return jsonify(item=entry_from_row(row, _source_base()))


def _batch_values(value, field):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,\n]+", value) if item.strip()]
    raise ValueError(f"{field} 必须是数组或逗号/换行分隔的字符串")


def _batch_lookup(ids, words):
    if len(ids) + len(words) > 2000:
        raise ValueError("批量查询最多 2000 项")
    requested = []
    for item in ids + words:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 120:
            raise ValueError("批量查询项必须是 1 到 120 个字符")
        item = item.strip().lower()
        if item not in requested:
            requested.append(item)
    if not requested:
        raise ValueError("至少提供一个 id 或单词")
    if len(requested) > 2000:
        raise ValueError("批量查询最多 2000 项")
    db = get_db()
    by_id = {row["id"]: row for row in db.execute(
        "SELECT * FROM word_entries WHERE id IN (%s)" % ",".join("?" * len(requested)), requested
    ).fetchall()}
    by_word = {row["word"].lower(): row for row in db.execute(
        "SELECT * FROM word_entries WHERE LOWER(word) IN (%s)" % ",".join("?" * len(requested)), requested
    ).fetchall()}
    items, missing = [], []
    for requested_id in requested:
        row = by_id.get(requested_id) or by_word.get(requested_id)
        if row is None:
            missing.append(requested_id)
        else:
            items.append(entry_from_row(row, _source_base()))
    return jsonify(items=items, missing=missing)


@bp.get("/api/words/batch")
def batch_words_get():
    try:
        return _batch_lookup(_batch_values(request.args.get("ids"), "ids"), _batch_values(request.args.get("words"), "words"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@bp.post("/api/words/batch")
def batch_words():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="请求体必须是 JSON 对象"), 400
    try:
        ids = _batch_values(data.get("ids"), "ids")
        words = _batch_values(data.get("words"), "words")
        return _batch_lookup(ids, words)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


def _fetch_json(url, timeout=20):
    _validate_source_url(url)
    last = None
    max_bytes = int(current_app.config.get("WORDS_MAX_RESPONSE_BYTES", 16 * 1024 * 1024)) if has_app_context() else 16 * 1024 * 1024
    for _ in range(3):
        try:
            req = Request(url, headers={"User-Agent": "simple-server-word-atlas/1.0"})
            with urlopen(req, timeout=max(1, timeout)) as response:
                _validate_source_url(response.geturl())
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ValueError("来源响应体过大")
                chunks, total = [], 0
                while True:
                    chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("来源响应体过大")
                return json.loads(b"".join(chunks).decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last = exc
    raise ValueError(f"无法读取来源 {url}: {last}")


def _fetch_binary(url, timeout=20):
    _validate_source_url(url)
    max_bytes = int(current_app.config.get("WORDS_ASSET_MAX_BYTES", 16 * 1024 * 1024))
    req = Request(url, headers={"User-Agent": "simple-server-word-atlas/1.0"})
    with urlopen(req, timeout=max(1, timeout)) as response:
        _validate_source_url(response.geturl())
        length = response.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise ValueError("词光资源过大")
        chunks, total = [], 0
        while True:
            chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("词光资源过大")
        return b"".join(chunks)


def sync_word_assets(ids=None, *, dry_run=False, limit=None, source_base=None):
    base = _validate_source_base(source_base or _source_base())
    if ids is not None:
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ValueError("ids 必须是字符串数组")
        ids = [item.strip().lower() for item in ids if item.strip()]
        if len(ids) > 2000 or len(ids) != len(set(ids)):
            raise ValueError("ids 最多 2000 项且不能重复")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 10000:
            raise ValueError("limit 必须是 1 到 10000 的整数")
    db = get_db()
    if ids:
        placeholders = ",".join("?" * len(ids))
        rows = db.execute(f"SELECT id FROM word_entries WHERE id IN ({placeholders}) ORDER BY word COLLATE NOCASE, id", ids).fetchall()
        found = {row["id"] for row in rows}
        missing = [item for item in ids if item not in found]
    else:
        rows = db.execute("SELECT id FROM word_entries ORDER BY word COLLATE NOCASE, id").fetchall()
        missing = []
    if limit is not None:
        rows = rows[:limit]
    root = Path(current_app.config["WORDS_ASSET_DIR"])
    plans, unchanged = [], 0
    for row in rows:
        entry_id = row["id"]
        files = _asset_file_map(entry_id, root)
        urls = asset_urls(entry_id, base)
        for group, variants in files.items():
            for name, (path, _) in variants.items():
                if path.is_file() and path.stat().st_size > 0:
                    unchanged += 1
                else:
                    plans.append((entry_id, group, name, path, urls[group][name]))
    result = {
        "entries": len(rows), "assets": unchanged + len(plans), "unchanged": unchanged,
        "downloaded": 0, "failed": 0, "missing": missing, "errors": [],
    }
    if dry_run or not plans:
        result["pending"] = len(plans)
        return result

    app = current_app._get_current_object()

    def download(plan):
        entry_id, group, name, path, url = plan
        with app.app_context():
            try:
                payload = _fetch_binary(url, timeout=int(current_app.config.get("WORDS_ASSET_SYNC_TIMEOUT", 30)))
                if not payload:
                    raise ValueError("资源为空")
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
                    temp_path = Path(handle.name)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, path)
                return None
            except Exception as exc:
                try:
                    if "temp_path" in locals() and temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass
                return {"id": entry_id, "kind": f"{group}.{name}", "error": str(exc)}

    workers = max(1, min(32, int(current_app.config.get("WORDS_ASSET_WORKERS", 12))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(plans))) as pool:
        for error in pool.map(download, plans):
            if error:
                result["failed"] += 1
                if len(result["errors"]) < 100:
                    result["errors"].append(error)
            else:
                result["downloaded"] += 1
    result["pending"] = len(plans)
    return result


def fetch_source_snapshot(base_url=None):
    base = _validate_source_base(base_url or _source_base())
    deadline = time.monotonic() + (int(current_app.config.get("WORDS_SYNC_TIMEOUT", 180)) if has_app_context() else 180)
    index_url = base + "index.json"
    index = _fetch_json(index_url)
    if not isinstance(index, dict) or not isinstance(index.get("words"), list):
        raise ValueError("来源索引格式无效")
    count = index.get("count")
    chunk_size = index.get("chunk")
    if not isinstance(count, int) or not isinstance(chunk_size, int) or count < 1 or chunk_size < 1:
        raise ValueError("来源索引缺少 count/chunk")
    words = index["words"]
    if len(words) != count:
        raise ValueError("来源索引 count 不匹配")
    expected = {}
    for item in words:
        if not isinstance(item, list) or len(item) < 2 or not isinstance(item[0], str) or not isinstance(item[1], str):
            raise ValueError("来源索引词条格式无效")
        if item[0] in expected:
            raise ValueError("来源索引包含重复 id")
        expected[item[0]] = item
    chunk_numbers = list(range((count + chunk_size - 1) // chunk_size))
    def load_chunk(number):
        remaining = max(1, int(deadline - time.monotonic()))
        payload = _fetch_json(f"{base}entries/{number}.json", timeout=min(20, remaining))
        if not isinstance(payload, list):
            raise ValueError(f"entries/{number}.json 格式无效")
        return number, payload
    chunks = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(chunk_numbers)))) as pool:
        futures = [pool.submit(load_chunk, number) for number in chunk_numbers]
        for future in futures:
            remaining = max(1, int(deadline - time.monotonic()))
            number, payload = future.result(timeout=remaining)
            chunks[number] = payload
    entries = []
    seen = set()
    for number in chunk_numbers:
        expected_chunk = words[number * chunk_size:(number + 1) * chunk_size]
        payload = chunks[number]
        if len(payload) != len(expected_chunk):
            raise ValueError(f"entries/{number}.json 数量不匹配")
        for item, index_item in zip(payload, expected_chunk):
            normalized = validate_entry(item)
            if (normalized["id"] != index_item[0] or normalized["word"].casefold() != index_item[1].casefold()
                    or normalized["id"] in seen):
                raise ValueError(f"entries/{number}.json 与索引不匹配")
            seen.add(normalized["id"])
            entries.append(item)
    if len(entries) != count or seen != set(expected):
        raise ValueError("来源 entries 未覆盖全部词条")
    return index, entries


@bp.post("/api/words/import")
@require_user(admin=True)
def import_words():
    upload = request.files.get("file")
    if not upload:
        return jsonify(error="需要上传 JSON 或 JSONL 文件"), 400
    try:
        max_bytes = int(current_app.config.get("WORDS_MAX_IMPORT_BYTES", 32 * 1024 * 1024))
        raw_bytes = upload.read(max_bytes + 1)
        if len(raw_bytes) > max_bytes:
            raise ValueError("导入文件过大")
        raw = raw_bytes.decode("utf-8")
        filename = (upload.filename or "").lower()
        if filename.endswith(".jsonl"):
            entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
        else:
            payload = json.loads(raw)
            entries = payload if isinstance(payload, list) else payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ValueError("文件必须包含非空词条数组")
        if len(entries) > 10000:
            raise ValueError("一次最多导入 10000 个词条")
        counts = upsert_entries(entries, replace=str(request.args.get("replace", "")).lower() in ("1", "true", "yes"), source_url="import")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, **counts), 200


@bp.post("/api/words/sync")
@require_user(admin=True)
def sync_words():
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify(error="请求体必须是 JSON 对象"), 400
    base = data.get("source_base") or _source_base()
    if not isinstance(base, str):
        return jsonify(error="source_base 必须是字符串"), 400
    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return jsonify(error="dry_run 必须是布尔值"), 400
    try:
        base = _validate_source_base(base)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    try:
        index, entries = fetch_source_snapshot(base)
        current_count = get_db().execute("SELECT COUNT(*) FROM word_entries WHERE source_url=?", (base.rstrip("/"),)).fetchone()[0]
        if current_count >= 100 and len(entries) < max(100, current_count // 2):
            return jsonify(error="来源词条数量异常减少，已保留现有词库；如确需替换请先清理对应来源", preserved=True), 409
        counts = upsert_entries(entries, replace=True, dry_run=dry_run, source_url=base.rstrip("/"), source_updated_at=str(index.get("version", "")))
    except (ValueError, OSError, concurrent.futures.TimeoutError) as exc:
        return jsonify(error=str(exc), preserved=True), 502
    return jsonify(ok=True, count=len(entries), source_version=index.get("version"), **counts)


@bp.post("/api/words/assets/sync")
@require_user(admin=True)
def sync_word_assets_api():
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify(error="请求体必须是 JSON 对象"), 400
    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return jsonify(error="dry_run 必须是布尔值"), 400
    try:
        result = sync_word_assets(
            data.get("ids"), dry_run=dry_run, limit=data.get("limit"), source_base=data.get("source_base")
        )
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, **result)
