import time
from flask import Blueprint, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash
from .db import get_db
from .security import hash_token, new_token, require_user

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

@bp.post("/login")
def login():
    d = request.get_json(silent=True) or {}
    user = get_db().execute("SELECT * FROM users WHERE username=? AND active=1",(d.get("username", ""),)).fetchone()
    if not user or not check_password_hash(user["password_hash"], d.get("password", "")):
        return jsonify(error="用户名或密码错误"), 401
    token, now = new_token(), int(time.time())
    get_db().execute("INSERT INTO sessions VALUES(?,?,?,?)",(hash_token(token),user["id"],now+86400,now)); get_db().commit()
    return jsonify(token=token,expires_at=now+86400,user={"id":user["id"],"username":user["username"],"role":user["role"]})

@bp.post("/logout")
@require_user()
def logout():
    get_db().execute("DELETE FROM sessions WHERE token_hash=?",(hash_token(request.headers["Authorization"][7:].strip()),)); get_db().commit()
    return jsonify(ok=True)

@bp.get("/users")
@require_user(admin=True)
def list_users():
    return jsonify(users=[dict(r) for r in get_db().execute("SELECT id,username,role,active,created_at FROM users ORDER BY id")])

@bp.post("/users")
@require_user(admin=True)
def create_user():
    d = request.get_json(silent=True) or {}; username=d.get("username", "").strip(); password=d.get("password", ""); role=d.get("role", "user")
    if len(username)<3 or len(password)<8 or role not in ("admin","user"): return jsonify(error="用户名至少3字符、密码至少8字符，role 为 admin/user"),400
    try:
        cur=get_db().execute("INSERT INTO users(username,password_hash,role,active,created_at) VALUES(?,?,?,?,?)",(username,generate_password_hash(password),role,1,int(time.time()))); get_db().commit()
    except Exception: return jsonify(error="用户名已存在"),409
    return jsonify(id=cur.lastrowid,username=username,role=role),201

@bp.patch("/users/<int:user_id>")
@require_user(admin=True)
def update_user(user_id):
    d=request.get_json(silent=True) or {}; fields=[]; params=[]
    if "role" in d:
        if d["role"] not in ("admin","user"): return jsonify(error="role 无效"),400
        fields.append("role=?"); params.append(d["role"])
    if "active" in d: fields.append("active=?"); params.append(int(bool(d["active"])))
    if d.get("password"):
        if len(d["password"])<8: return jsonify(error="密码至少8字符"),400
        fields.append("password_hash=?"); params.append(generate_password_hash(d["password"]))
    if not fields: return jsonify(error="没有可更新字段"),400
    get_db().execute("UPDATE users SET "+",".join(fields)+" WHERE id=?",params+[user_id]); get_db().commit()
    return jsonify(ok=True)

