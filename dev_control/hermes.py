"""Hermes remote operator API and conversation storage.

Hermes is intentionally treated as an OpenAI-compatible remote service. The
local conversation store keeps the UI useful when the remote service is
temporarily offline and makes conversation history queryable from the console.
"""
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, render_template, request, send_file

from .db import get_db
from .security import require_user

bp = Blueprint("hermes", __name__)

DEFAULT_SKILLS = [
    {"id": "file-manager", "name": "文件管理", "description": "列出、预览、上传和整理服务器文件", "icon": "▤"},
    {"id": "device-operator", "name": "设备远程操作", "description": "向已连接 Device Agent 下发命令并查看结果", "icon": "▣"},
    {"id": "port-tools", "name": "端口与网络", "description": "扫描端口、检查服务连通性和 FRP 状态", "icon": "⌁"},
    {"id": "drawio", "name": "Drawio 图表", "description": "创建、读取和更新服务器上的 Drawio 文件", "icon": "◇"},
    {"id": "notes", "name": "小记与知识", "description": "检索、整理和更新控制台小记", "icon": "✎"},
]
DEFAULT_FUNCTIONS = [
    {"id": "list_files", "name": "列出文件", "skill": "file-manager", "hint": "列出当前目录中的文件"},
    {"id": "inspect_device", "name": "检查设备", "skill": "device-operator", "hint": "查看在线设备和最近心跳"},
    {"id": "run_command", "name": "执行命令", "skill": "device-operator", "hint": "在选定设备上执行一条安全命令"},
    {"id": "scan_ports", "name": "扫描端口", "skill": "port-tools", "hint": "检查主机端口是否开放"},
    {"id": "summarize_file", "name": "总结文件", "skill": "file-manager", "hint": "读取已上传文件并提炼要点"},
]


def _now():
    return int(time.time())


def _base_url():
    return str(current_app.config.get("HERMES_BASE_URL", "")).strip().rstrip("/")


def _local_command():
    if not current_app.config.get("HERMES_LOCAL_ENABLED", True):
        return None
    configured = str(current_app.config.get("HERMES_LOCAL_COMMAND", "")).strip()
    candidates = [configured, shutil.which("hermes"), str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _remote(path, payload=None, method="GET", timeout=45):
    base = _base_url()
    if not base:
        return None, "未配置 HERMES_BASE_URL"
    url = base + (path if path.startswith("/") else "/" + path)
    body = None
    headers = {"Accept": "application/json", "User-Agent": "dev-control-hermes/1.0"}
    key = str(current_app.config.get("HERMES_API_KEY", ""))
    if key:
        headers["Authorization"] = "Bearer " + key
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024)
            if not raw:
                return {}, None
            return json.loads(raw.decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        detail = exc.read(1200).decode("utf-8", "replace")
        return None, "Hermes HTTP %s: %s" % (exc.code, detail or exc.reason)
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def _row_conversation(row):
    return {"id": row["id"], "title": row["title"], "remote_id": row["remote_id"], "created_at": row["created_at"], "updated_at": row["updated_at"]}


def _conversation(user_id, conversation_id=None, create=True):
    db = get_db()
    row = None
    if conversation_id:
        row = db.execute("SELECT * FROM hermes_conversations WHERE id=? AND user_id=?", (int(conversation_id), user_id)).fetchone()
    if row or not create:
        return row
    now = _now()
    cur = db.execute("INSERT INTO hermes_conversations(user_id,title,created_at,updated_at) VALUES(?,?,?,?)", (user_id, "新对话", now, now))
    db.commit()
    return db.execute("SELECT * FROM hermes_conversations WHERE id=?", (cur.lastrowid,)).fetchone()


def _message(row):
    return {"id": row["id"], "role": row["role"], "content": row["content"], "remote_id": row["remote_id"], "attachments": json.loads(row["attachments_json"] or "[]"), "created_at": row["created_at"]}


def _enrich_attachments(items, user_id):
    """Attach authenticated download URLs and small text previews for Hermes."""
    out = []
    for raw in items[:20]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        upload_id = item.get("id")
        row = None
        try:
            if upload_id:
                row = get_db().execute("SELECT * FROM hermes_uploads WHERE id=? AND user_id=?", (int(upload_id), user_id)).fetchone()
        except (TypeError, ValueError):
            row = None
        if row:
            item.update({"id": row["id"], "name": row["filename"], "mime": row["mime"], "size": row["size"], "url": request.host_url.rstrip("/") + "/api/hermes/uploads/" + str(row["id"])})
            if row["size"] <= 4 * 1024 * 1024:
                try:
                    item["content_base64"] = base64.b64encode(Path(row["stored_path"]).read_bytes()).decode("ascii")
                except OSError:
                    pass
            if row["size"] <= 512 * 1024 and (row["mime"].startswith("text/") or row["filename"].lower().endswith((".md", ".json", ".csv", ".log", ".py", ".js", ".ts", ".yaml", ".yml"))):
                try:
                    item["text_preview"] = Path(row["stored_path"]).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
        out.append(item)
    return out


@bp.get("/hermes/")
def page():
    return render_template("hermes.html")


@bp.get("/api/hermes/status")
@require_user()
def status():
    local = _local_command()
    remote = bool(_base_url())
    return jsonify(configured=bool(local or remote), base_url=_base_url(), local=bool(local), local_command=Path(local).name if local else "", backend="local" if local and not remote else ("remote" if remote else "none"), model=current_app.config.get("HERMES_MODEL", "hermes"), connected=bool(local or remote))


@bp.get("/api/hermes/catalog")
@require_user()
def catalog():
    skills, functions = list(DEFAULT_SKILLS), list(DEFAULT_FUNCTIONS)
    remote_skills, skill_error = _remote(current_app.config.get("HERMES_SKILLS_PATH", "/skills"), timeout=5)
    remote_functions, function_error = _remote(current_app.config.get("HERMES_FUNCTIONS_PATH", "/functions"), timeout=5)
    if isinstance(remote_skills, dict): remote_skills = remote_skills.get("skills", remote_skills.get("items", []))
    if isinstance(remote_functions, dict): remote_functions = remote_functions.get("functions", remote_functions.get("items", []))
    if isinstance(remote_skills, list) and remote_skills: skills = remote_skills
    if isinstance(remote_functions, list) and remote_functions: functions = remote_functions
    return jsonify(skills=skills, functions=functions, remote=bool(_base_url()), errors=[x for x in (skill_error, function_error) if x and _base_url()])


@bp.get("/api/hermes/conversations")
@require_user()
def conversations():
    query = request.args.get("q", "").strip()
    if query:
        term = "%" + query[:120] + "%"
        rows = get_db().execute("SELECT * FROM hermes_conversations WHERE user_id=? AND (title LIKE ? OR id IN (SELECT conversation_id FROM hermes_messages WHERE content LIKE ?)) ORDER BY updated_at DESC,id DESC", (g.user["id"], term, term)).fetchall()
    else:
        rows = get_db().execute("SELECT * FROM hermes_conversations WHERE user_id=? ORDER BY updated_at DESC,id DESC", (g.user["id"],)).fetchall()
    return jsonify(conversations=[_row_conversation(r) for r in rows])


@bp.post("/api/hermes/conversations")
@require_user()
def create_conversation():
    d = request.get_json(silent=True) or {}
    row = _conversation(g.user["id"])
    title = str(d.get("title", "")).strip()
    if title:
        get_db().execute("UPDATE hermes_conversations SET title=?,updated_at=? WHERE id=? AND user_id=?", (title[:120], _now(), row["id"], g.user["id"]))
        get_db().commit()
        row = get_db().execute("SELECT * FROM hermes_conversations WHERE id=?", (row["id"],)).fetchone()
    return jsonify(conversation=_row_conversation(row)), 201


@bp.get("/api/hermes/conversations/<int:conversation_id>")
@require_user()
def conversation_detail(conversation_id):
    row = _conversation(g.user["id"], conversation_id, create=False)
    if not row: return jsonify(error="对话不存在"), 404
    messages = get_db().execute("SELECT * FROM hermes_messages WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
    return jsonify(conversation=_row_conversation(row), messages=[_message(x) for x in messages])


@bp.post("/api/hermes/upload")
@require_user()
def upload():
    conversation_id = request.form.get("conversation_id") or None
    try:
        row = _conversation(g.user["id"], int(conversation_id) if conversation_id else None)
    except (TypeError, ValueError):
        return jsonify(error="conversation_id 无效"), 400
    if conversation_id and not row: return jsonify(error="对话不存在"), 404
    files = request.files.getlist("files")
    if not files: return jsonify(error="请选择文件"), 400
    root = Path(current_app.config["DATA_DIR"]) / "hermes_uploads" / str(g.user["id"])
    root.mkdir(parents=True, exist_ok=True)
    out = []
    for item in files[:20]:
        name = Path(item.filename or "upload.bin").name or "upload.bin"
        safe = "%d-%s" % (time.time_ns(), name)
        path = root / safe
        item.save(path)
        st = path.stat()
        cur = get_db().execute("INSERT INTO hermes_uploads(user_id,conversation_id,filename,stored_path,mime,size,created_at) VALUES(?,?,?,?,?,?,?)", (g.user["id"], row["id"], name, str(path), item.mimetype or mimetypes.guess_type(name)[0] or "application/octet-stream", st.st_size, _now()))
        out.append({"id": cur.lastrowid, "name": name, "mime": item.mimetype or "application/octet-stream", "size": st.st_size, "url": request.host_url.rstrip("/") + "/api/hermes/uploads/%d" % cur.lastrowid})
    get_db().commit()
    return jsonify(files=out, conversation_id=row["id"]), 201


@bp.get("/api/hermes/uploads/<int:upload_id>")
@require_user()
def uploaded_file(upload_id):
    row = get_db().execute("SELECT * FROM hermes_uploads WHERE id=? AND user_id=?", (upload_id, g.user["id"])).fetchone()
    if not row or not Path(row["stored_path"]).is_file(): return jsonify(error="文件不存在"), 404
    return send_file(row["stored_path"], download_name=row["filename"], mimetype=row["mime"])


def _remote_reply(messages, conversation, attachments=None, skill=None):
    payload = {"model": current_app.config.get("HERMES_MODEL", "hermes"), "messages": messages, "stream": False}
    if attachments:
        payload["attachments"] = attachments
    if skill:
        payload["skill"] = skill
    if conversation["remote_id"]: payload["conversation_id"] = conversation["remote_id"]
    data, error = _remote(current_app.config.get("HERMES_CHAT_PATH", "/v1/chat/completions"), payload, method="POST")
    if error: return None, error, None
    if not isinstance(data, dict): return str(data), None, None
    remote_id = data.get("conversation_id") or data.get("thread_id") or data.get("id")
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or choices[0]
        content = message.get("content") if isinstance(message, dict) else message
    else:
        content = data.get("reply") or data.get("content") or data.get("output") or data.get("text")
    if isinstance(content, list): content = "".join(str(x.get("text", x)) if isinstance(x, dict) else str(x) for x in content)
    return str(content or "Hermes 未返回文本内容"), None, remote_id


def _local_reply(messages, attachments=None, skill=None):
    command = _local_command()
    if not command:
        return None, "本机未找到 Hermes CLI，请设置 HERMES_LOCAL_COMMAND", None
    transcript = []
    for message in messages[-30:]:
        role = "用户" if message.get("role") == "user" else "Hermes"
        transcript.append(role + "：" + str(message.get("content", "")))
    prompt = "你正在通过 Dev Control 的局域网网页为用户工作。请直接完成请求并给出清晰结果。\n\n" + "\n".join(transcript)
    if skill:
        prompt += "\n\n当前选中的 Skill：" + skill
    if attachments:
        prompt += "\n\n本次附件："
        for item in attachments:
            prompt += "\n- " + str(item.get("name", "文件"))
            if item.get("text_preview"):
                prompt += "\n  文件内容预览：\n" + str(item["text_preview"])
    try:
        result = subprocess.run([command, "-z", prompt], capture_output=True, text=True, timeout=300, cwd=str(Path(current_app.root_path).parent), env=os.environ.copy())
    except subprocess.TimeoutExpired:
        return None, "本机 Hermes 执行超时（超过 300 秒）", None
    except OSError as exc:
        return None, "本机 Hermes 启动失败：" + str(exc), None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "进程退出码 %s" % result.returncode).strip()
        return None, "本机 Hermes 执行失败：" + detail[-1200:], None
    return (result.stdout or "Hermes 未返回文本内容").strip(), None, None


@bp.post("/api/hermes/chat")
@require_user()
def chat():
    d = request.get_json(silent=True) or {}
    text = str(d.get("message", "")).strip()
    if not text: return jsonify(error="消息不能为空"), 400
    try:
        requested_id = int(d.get("conversation_id")) if d.get("conversation_id") else None
    except (TypeError, ValueError):
        return jsonify(error="conversation_id 无效"), 400
    row = _conversation(g.user["id"], requested_id)
    if d.get("conversation_id") and not row: return jsonify(error="对话不存在"), 404
    attachments = _enrich_attachments(d.get("attachments") if isinstance(d.get("attachments"), list) else [], g.user["id"])
    stored_attachments = [{k: v for k, v in item.items() if k != "content_base64"} for item in attachments]
    skill = str(d.get("skill", "")).strip()[:120]
    db = get_db(); now = _now()
    db.execute("INSERT INTO hermes_messages(conversation_id,role,content,attachments_json,created_at) VALUES(?,?,?,?,?)", (row["id"], "user", text, json.dumps(stored_attachments, ensure_ascii=False), now))
    old = db.execute("SELECT role,content,attachments_json FROM hermes_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 30", (row["id"],)).fetchall()
    messages = []
    for x in reversed(old):
        content = x["content"]
        if x["role"] == "user":
            attached = json.loads(x["attachments_json"] or "[]")
            if attached:
                content += "\n\n附件：" + ", ".join(str(a.get("name", "文件")) for a in attached if isinstance(a, dict))
        messages.append({"role": x["role"], "content": content})
    if _local_command() and not _base_url():
        reply, error, remote_id = _local_reply(messages, attachments, skill)
    else:
        reply, error, remote_id = _remote_reply(messages, row, attachments, skill)
    if error: reply = "Hermes 后端暂不可用。" + error
    db.execute("INSERT INTO hermes_messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)", (row["id"], "assistant", reply, _now()))
    title = row["title"]
    if title == "新对话": title = text[:40]
    db.execute("UPDATE hermes_conversations SET title=?,remote_id=COALESCE(?,remote_id),updated_at=? WHERE id=?", (title, remote_id, _now(), row["id"]))
    db.commit()
    return jsonify(conversation_id=row["id"], reply=reply, degraded=bool(error), error=error, attachments=stored_attachments)
