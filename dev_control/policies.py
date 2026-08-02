import json, time
from flask import Blueprint, jsonify, render_template, request
from .db import get_db
from .security import require_user

bp=Blueprint("access_policies",__name__)

def normalize(values):
    if isinstance(values,str):values=values.replace("\r","").split("\n")
    out=[]
    for value in values or []:
        value=str(value).strip()
        if value and value not in out:out.append(value)
    return out

def serialize(row):
    d=dict(row);d["whitelist"]=json.loads(d.pop("whitelist_json") or "[]");d["blacklist"]=json.loads(d.pop("blacklist_json") or "[]");d["enabled"]=bool(d["enabled"]);return d

@bp.get("/policies/")
def page():return render_template("policies.html")

@bp.get("/api/access-policies/scenarios")
@require_user()
def scenarios():return jsonify(scenarios=[serialize(r) for r in get_db().execute("SELECT * FROM access_scenarios ORDER BY name")])

@bp.get("/api/access-policies/scenarios/<path:name>")
@require_user()
def scenario(name):
    row=get_db().execute("SELECT * FROM access_scenarios WHERE name=?",(name,)).fetchone()
    return jsonify(serialize(row)) if row else (jsonify(error="场景不存在"),404)

@bp.post("/api/access-policies/scenarios")
@require_user(admin=True)
def create():
    d=request.get_json(silent=True) or {};name=str(d.get("name","")).strip()
    if not name:return jsonify(error="场景名称不能为空"),400
    try:
        cur=get_db().execute("INSERT INTO access_scenarios(name,description,whitelist_json,blacklist_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(name,str(d.get("description","")).strip(),json.dumps(normalize(d.get("whitelist")),ensure_ascii=False),json.dumps(normalize(d.get("blacklist")),ensure_ascii=False),int(bool(d.get("enabled",True))),int(time.time()),int(time.time())));get_db().commit()
    except Exception as e:return jsonify(error="场景名称已存在" if "UNIQUE" in str(e) else str(e)),409
    return jsonify(id=cur.lastrowid),201

@bp.patch("/api/access-policies/scenarios/<int:sid>")
@require_user(admin=True)
def update(sid):
    d=request.get_json(silent=True) or {};fields=[];params=[]
    for key in ("name","description","whitelist","blacklist","enabled"):
        if key not in d:continue
        col={"whitelist":"whitelist_json","blacklist":"blacklist_json"}.get(key,key);value=d[key]
        if key in ("whitelist","blacklist"):value=json.dumps(normalize(value),ensure_ascii=False)
        if key=="enabled":value=int(bool(value))
        fields.append(col+"=?");params.append(value)
    if not fields:return jsonify(error="没有可更新字段"),400
    fields.append("updated_at=?");params.extend([int(time.time()),sid]);get_db().execute("UPDATE access_scenarios SET "+",".join(fields)+" WHERE id=?",params);get_db().commit();return jsonify(ok=True)

@bp.delete("/api/access-policies/scenarios/<int:sid>")
@require_user(admin=True)
def delete(sid):get_db().execute("DELETE FROM access_scenarios WHERE id=?",(sid,));get_db().commit();return jsonify(ok=True)

@bp.post("/api/access-policies/evaluate")
@require_user()
def evaluate():
    d=request.get_json(silent=True) or {};name=str(d.get("scenario","")).strip();value=str(d.get("value","")).strip();row=get_db().execute("SELECT * FROM access_scenarios WHERE name=? AND enabled=1",(name,)).fetchone()
    if not row:return jsonify(error="场景不存在或未启用"),404
    policy=serialize(row);needle=value.casefold();black={x.casefold() for x in policy["blacklist"]};white={x.casefold() for x in policy["whitelist"]}
    if needle in black:allowed=False;reason="blacklist"
    elif white:allowed=needle in white;reason="whitelist" if allowed else "not_in_whitelist"
    else:allowed=True;reason="default_allow"
    return jsonify(scenario=name,value=value,allowed=allowed,reason=reason)

