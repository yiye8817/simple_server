import hashlib, json, secrets, time
from pathlib import Path
from flask import Blueprint, current_app, g, jsonify, request, send_file
from .db import get_db
from .security import hash_token, new_token, require_device, require_user

bp=Blueprint("agents",__name__,url_prefix="/api")

def public_device(d, include_token=None):
    out={"id":d["id"],"device_uid":d["device_uid"],"name":d["name"],"online":bool(d["online"]),"active":bool(d["active"]),"last_seen":d["last_seen"],"stats":json.loads(d["stats_json"] or "{}")}
    if include_token: out["device_token"]=include_token
    return out

def update_seen(payload):
    now=int(time.time()); name=payload.get("hostname") or payload.get("name") or g.device["name"]
    get_db().execute("UPDATE devices SET name=?,online=1,last_seen=?,stats_json=? WHERE id=?",(name,now,json.dumps(payload,ensure_ascii=False),g.device["id"])); get_db().commit()
    return now

@bp.post("/agent/enroll")
def enroll():
    configured=current_app.config["DEVICE_AGENT_ENROLL_TOKEN"]
    if not current_app.config["DEVICE_AGENT_OPEN_ENROLL"] and request.headers.get("X-Enroll-Token","")!=configured: return jsonify(error="enroll token 无效"),403
    d=request.get_json(silent=True) or {}; uid=(d.get("device_uid") or "").strip()
    if not uid: return jsonify(error="device_uid 不能为空"),400
    old=get_db().execute("SELECT * FROM devices WHERE device_uid=?",(uid,)).fetchone(); token=new_token(); now=int(time.time()); name=d.get("hostname") or d.get("name") or uid
    if old:
        get_db().execute("UPDATE devices SET token_hash=?,name=?,active=1,last_seen=?,stats_json=? WHERE id=?",(hash_token(token),name,now,json.dumps(d,ensure_ascii=False),old["id"])); created=False
    else:
        get_db().execute("INSERT INTO devices(device_uid,name,token_hash,active,online,last_seen,stats_json,created_at) VALUES(?,?,?,?,?,?,?,?)",(uid,name,hash_token(token),1,0,now,json.dumps(d,ensure_ascii=False),now)); created=True
    get_db().commit(); dev=get_db().execute("SELECT * FROM devices WHERE device_uid=?",(uid,)).fetchone()
    return jsonify(created=created,device=public_device(dev,token))

@bp.post("/agent/register")
@require_device
def register():
    now=update_seen(request.get_json(silent=True) or {}); dev=get_db().execute("SELECT * FROM devices WHERE id=?",(g.device["id"],)).fetchone()
    return jsonify(device=public_device(dev),server_time=now)

@bp.post("/agent/heartbeat")
@require_device
def heartbeat():
    return jsonify(ok=True,server_time=update_seen(request.get_json(silent=True) or {}))

@bp.get("/agent/poll")
@require_device
def poll():
    wait=max(0,min(int(request.args.get("wait",0)),25)); deadline=time.time()+wait; rows=[]; first=True
    while first or time.time()<=deadline:
        first=False
        db=get_db(); db.execute("BEGIN IMMEDIATE")
        rows=db.execute("SELECT * FROM tasks WHERE device_id=? AND status='pending' ORDER BY id LIMIT 10",(g.device["id"],)).fetchall()
        if rows:
            now=int(time.time())
            for r in rows:
                claim=new_token(); db.execute("UPDATE tasks SET status='claimed',claim_token=?,claimed_at=? WHERE id=? AND status='pending'",(claim,now,r["id"]))
            db.commit(); rows=db.execute("SELECT * FROM tasks WHERE device_id=? AND status='claimed' AND claimed_at=? ORDER BY id",(g.device["id"],now)).fetchall(); break
        db.commit()
        if time.time()>=deadline: break
        time.sleep(.25)
    return jsonify(tasks=[{"id":r["id"],"kind":r["kind"],"payload":json.loads(r["payload_json"]),"claim_token":r["claim_token"]} for r in rows])

@bp.post("/agent/tasks/<int:task_id>/result")
@require_device
def task_result(task_id):
    d=request.get_json(silent=True) or {}; row=get_db().execute("SELECT * FROM tasks WHERE id=? AND device_id=?",(task_id,g.device["id"])).fetchone()
    if not row: return jsonify(error="任务不存在"),404
    if row["claim_token"] and d.get("claim_token")!=row["claim_token"]: return jsonify(error="claim_token 无效"),409
    status=d.get("status","done"); get_db().execute("UPDATE tasks SET status=?,result_json=?,finished_at=? WHERE id=?",(status,json.dumps(d,ensure_ascii=False),int(time.time()),task_id)); get_db().commit()
    return jsonify(ok=True)

@bp.route("/agent/fs-blobs/<token>",methods=["GET","POST"])
@require_device
def agent_blob(token):
    row=get_db().execute("SELECT * FROM blobs WHERE token=? AND device_id=? AND expires_at>?",(token,g.device["id"],int(time.time()))).fetchone()
    if not row: return jsonify(error="blob 不存在或过期"),404
    path=Path(current_app.config["DATA_DIR"])/"blobs"/(token+".bin")
    if request.method=="GET":
        if row["direction"]!="push" or not path.is_file(): return jsonify(error="blob 尚未准备好"),404
        return send_file(path,download_name=row["filename"] or "blob.bin")
    if row["direction"]!="pull": return jsonify(error="blob 方向错误"),409
    sha=hashlib.sha256(); size=0
    with path.open("wb") as f:
        for chunk in iter(lambda:request.stream.read(65536),b""): f.write(chunk);sha.update(chunk);size+=len(chunk)
    get_db().execute("UPDATE blobs SET size=?,sha256=? WHERE token=?",(size,sha.hexdigest(),token));get_db().commit()
    return jsonify(ok=True,size=size,sha256=sha.hexdigest())

@bp.get("/agent/services/<int:sid>/archive")
@require_device
def service_archive(sid):
    row=get_db().execute("SELECT * FROM services WHERE id=?",(sid,)).fetchone()
    if not row:return jsonify(error="服务不存在"),404
    return send_file(row["archive_path"],as_attachment=True,download_name=Path(row["archive_path"]).name)

@bp.get("/devices")
@require_user()
def devices():
    where="" if g.user["role"]=="admin" else " WHERE owner_id=?"; args=() if not where else (g.user["id"],)
    return jsonify(devices=[public_device(r) for r in get_db().execute("SELECT * FROM devices"+where+" ORDER BY id",args)])

@bp.post("/devices/<int:device_id>/tasks")
@require_user()
def create_task(device_id):
    d=request.get_json(silent=True) or {}; kind=(d.get("kind") or "").strip()
    if not kind:return jsonify(error="kind 不能为空"),400
    dev=get_db().execute("SELECT * FROM devices WHERE id=?",(device_id,)).fetchone()
    if not dev:return jsonify(error="设备不存在"),404
    if g.user["role"]!="admin" and dev["owner_id"]!=g.user["id"]:return jsonify(error="无设备权限"),403
    cur=get_db().execute("INSERT INTO tasks(device_id,kind,payload_json,status,created_by,created_at) VALUES(?,?,?,?,?,?)",(device_id,kind,json.dumps(d.get("payload") or {},ensure_ascii=False),"pending",g.user["id"],int(time.time())));get_db().commit()
    return jsonify(id=cur.lastrowid,status="pending"),201

@bp.get("/devices/<int:device_id>/tasks")
@require_user()
def tasks(device_id):
    rows=get_db().execute("SELECT * FROM tasks WHERE device_id=? ORDER BY id DESC LIMIT 200",(device_id,)).fetchall()
    return jsonify(tasks=[{"id":r["id"],"kind":r["kind"],"status":r["status"],"payload":json.loads(r["payload_json"]),"result":json.loads(r["result_json"]) if r["result_json"] else None} for r in rows])

@bp.post("/devices/<int:device_id>/blobs")
@require_user()
def create_blob(device_id):
    direction=request.args.get("direction","push"); token=secrets.token_urlsafe(24); filename=request.args.get("filename","blob.bin"); path=Path(current_app.config["DATA_DIR"])/"blobs"/(token+".bin"); size=None;sha=None
    if direction=="push":
        shaobj=hashlib.sha256();size=0
        with path.open("wb") as f:
            for chunk in iter(lambda:request.stream.read(65536),b""):f.write(chunk);shaobj.update(chunk);size+=len(chunk)
        sha=shaobj.hexdigest()
    elif direction!="pull":return jsonify(error="direction 只能是 push/pull"),400
    get_db().execute("INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",(token,device_id,g.user["id"],direction,filename,size,sha,int(time.time())+3600));get_db().commit()
    return jsonify(token=token,direction=direction,size=size,sha256=sha),201
