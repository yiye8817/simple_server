import time
from flask import Blueprint, g, jsonify, render_template, request
from .db import get_db
from .security import hash_token, new_token, require_user

bp=Blueprint("user_management",__name__)

def can_access(user_id):return g.user["role"]=="admin" or g.user["id"]==user_id

def user_rows(where="",params=()):
    sql="""SELECT u.id,u.username,u.role,u.active,u.created_at,
    (SELECT COUNT(*) FROM sessions s WHERE s.user_id=u.id AND s.expires_at>?) active_sessions,
    (SELECT COUNT(*) FROM user_api_tokens t WHERE t.user_id=u.id AND t.active=1) active_tokens,
    (SELECT COUNT(*) FROM devices d WHERE d.owner_id=u.id) devices,
    (SELECT COUNT(*) FROM tasks t WHERE t.created_by=u.id) tasks,
    (SELECT COUNT(*) FROM typing_results r WHERE r.user_id=u.id) typing_total,
    (SELECT COALESCE(SUM(r.correct),0) FROM typing_results r WHERE r.user_id=u.id) typing_correct
    FROM users u """+where+" ORDER BY u.id"
    return get_db().execute(sql,(int(time.time()),)+tuple(params)).fetchall()

def serialize(row):
    d=dict(row);d["active"]=bool(d["active"]);d["typing_accuracy"]=round(d["typing_correct"]*100/d["typing_total"],1) if d["typing_total"] else 0;return d

@bp.get("/users/")
def page():return render_template("users.html")

@bp.get("/api/user-management/users")
@require_user()
def users():
    rows=user_rows() if g.user["role"]=="admin" else user_rows("WHERE u.id=?",(g.user["id"],))
    return jsonify(users=[serialize(r) for r in rows])

@bp.get("/api/user-management/me/stats")
@require_user()
def me_stats():return jsonify(serialize(user_rows("WHERE u.id=?",(g.user["id"],))[0]))

@bp.get("/api/user-management/users/<int:user_id>/tokens")
@require_user()
def tokens(user_id):
    if not can_access(user_id):return jsonify(error="无权读取该用户 Token"),403
    rows=get_db().execute("SELECT id,label,token,active,created_at,last_used_at FROM user_api_tokens WHERE user_id=? ORDER BY id DESC",(user_id,)).fetchall();return jsonify(tokens=[dict(r) for r in rows])

@bp.post("/api/user-management/users/<int:user_id>/tokens")
@require_user()
def create_token(user_id):
    if not can_access(user_id):return jsonify(error="无权为该用户创建 Token"),403
    if not get_db().execute("SELECT 1 FROM users WHERE id=? AND active=1",(user_id,)).fetchone():return jsonify(error="用户不存在或已停用"),404
    d=request.get_json(silent=True) or {};token="dcs_"+new_token();now=int(time.time());cur=get_db().execute("INSERT INTO user_api_tokens(user_id,label,token,token_hash,active,created_at) VALUES(?,?,?,?,?,?)",(user_id,str(d.get("label") or "API Token")[:100],token,hash_token(token),1,now));get_db().commit();return jsonify(id=cur.lastrowid,token=token,created_at=now),201

@bp.delete("/api/user-management/users/<int:user_id>/tokens/<int:token_id>")
@require_user()
def revoke_token(user_id,token_id):
    if not can_access(user_id):return jsonify(error="无权撤销该用户 Token"),403
    get_db().execute("UPDATE user_api_tokens SET active=0 WHERE id=? AND user_id=?",(token_id,user_id));get_db().commit();return jsonify(ok=True)
