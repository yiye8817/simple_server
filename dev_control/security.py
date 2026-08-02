import hashlib, secrets, time
from functools import wraps
from flask import g, jsonify, request
from .db import get_db

def hash_token(token): return hashlib.sha256(token.encode()).hexdigest()
def new_token(): return secrets.token_urlsafe(32)

def current_user():
    h = request.headers.get("Authorization", "")
    token = h[7:].strip() if h.lower().startswith("bearer ") else ""
    if not token: return None
    row=get_db().execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",(hash_token(token),int(time.time()))).fetchone()
    if row:return row
    row=get_db().execute("SELECT u.* FROM user_api_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND t.active=1 AND u.active=1",(hash_token(token),)).fetchone()
    if row:
        get_db().execute("UPDATE user_api_tokens SET last_used_at=? WHERE token_hash=?",(int(time.time()),hash_token(token)));get_db().commit()
    return row

def require_user(admin=False):
    def deco(fn):
        @wraps(fn)
        def wrapped(*a, **kw):
            user = current_user()
            if not user: return jsonify(error="需要 Bearer 用户令牌"), 401
            if admin and user["role"] != "admin": return jsonify(error="需要管理员权限"), 403
            g.user = user
            return fn(*a, **kw)
        return wrapped
    return deco

def require_device(fn):
    @wraps(fn)
    def wrapped(*a, **kw):
        token = request.headers.get("X-Device-Token", "")
        dev = get_db().execute("SELECT * FROM devices WHERE token_hash=? AND active=1",(hash_token(token),)).fetchone() if token else None
        if not dev: return jsonify(error="设备 token 无效"), 401
        g.device = dev
        return fn(*a, **kw)
    return wrapped
