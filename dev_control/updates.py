import hashlib, json, os, shutil, tarfile, tempfile, threading, time
from pathlib import Path, PurePosixPath
from flask import Blueprint, current_app, g, jsonify, render_template, request
from .db import get_db
from .security import require_user

bp=Blueprint("self_updates",__name__)
BLOCKED={".git",".venv","data","dist","__pycache__"}
MAX_FILES=15000
MAX_UNPACKED=1024*1024*1024

def project_root():return Path(__file__).resolve().parent.parent
def sha(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):h.update(chunk)
    return h.hexdigest()

def clean_name(name):
    raw=name
    while raw.startswith("./"):raw=raw[2:]
    if raw.startswith("/"):return None
    p=PurePosixPath(raw)
    if not str(p) or str(p)=="." or p.is_absolute() or ".." in p.parts:return None
    if p.parts[0] in BLOCKED or any(x=="__pycache__" for x in p.parts):return None
    if p.name.endswith((".pyc",".log",".db-wal",".db-shm")):return None
    if "WEB-INF" in p.parts and (p.name.endswith(("_secret","_api_key")) or p.name=="pusher_properties"):return None
    return p

def extract_safe(archive,dest):
    manifest=[];total=0
    with tarfile.open(archive,"r:gz") as tar:
        members=tar.getmembers()
        if len(members)>MAX_FILES:raise ValueError(f"归档条目过多：{len(members)} > {MAX_FILES}")
        for m in members:
            if m.issym() or m.islnk() or (not m.isfile() and not m.isdir()):raise ValueError(f"不允许链接或特殊文件：{m.name}")
            rel=clean_name(m.name)
            if rel is None:continue
            target=(dest/Path(*rel.parts)).resolve()
            if dest.resolve() not in target.parents and target!=dest.resolve():raise ValueError("归档路径越界")
            if m.isdir():target.mkdir(parents=True,exist_ok=True);continue
            total+=m.size
            if total>MAX_UNPACKED:raise ValueError("归档解压体积超过 1GiB")
            target.parent.mkdir(parents=True,exist_ok=True)
            src=tar.extractfile(m)
            if src is None:continue
            with target.open("wb") as out:shutil.copyfileobj(src,out)
            os.chmod(target,m.mode & 0o755 if m.mode & 0o111 else 0o644)
            manifest.append({"path":str(rel),"size":m.size,"sha256":sha(target)})
    return manifest

def compare(manifest,stage):
    root=project_root();added=[];modified=[];unchanged=[]
    for item in manifest:
        current=root/item["path"]
        if not current.is_file():added.append(item)
        elif sha(current)!=item["sha256"]:modified.append(item)
        else:unchanged.append(item)
    return {"added":added,"modified":modified,"unchanged":unchanged,"deleted":[]}

def row_data(row):
    d=dict(row);d["manifest"]=json.loads(d.pop("manifest_json") or "[]");d["diff"]=json.loads(d.pop("diff_json") or "{}");return d

@bp.get("/updates/")
def page():return render_template("updates.html")

@bp.get("/api/updates")
@require_user(admin=True)
def list_updates():
    rows=get_db().execute("SELECT * FROM app_updates ORDER BY id DESC LIMIT 100").fetchall();version=(project_root()/"VERSION").read_text().strip() if (project_root()/"VERSION").exists() else "unknown"
    return jsonify(current_version=version,updates=[row_data(r) for r in rows])

@bp.post("/api/updates/upload")
@require_user(admin=True)
def upload():
    version=str(request.form.get("version","")).strip();upload=request.files.get("file")
    if not version or not upload or not upload.filename.endswith(".tar.gz"):return jsonify(error="需要版本号和 .tar.gz 文件"),400
    base=Path(current_app.config["DATA_DIR"])/"updates";base.mkdir(parents=True,exist_ok=True);temp=Path(tempfile.mkdtemp(prefix="stage-",dir=base));archive=temp/"update.tar.gz";upload.save(archive);stage=temp/"payload";stage.mkdir()
    try:manifest=extract_safe(archive,stage);diff=compare(manifest,stage)
    except Exception as e:shutil.rmtree(temp,ignore_errors=True);return jsonify(error=str(e)),400
    if not manifest:shutil.rmtree(temp,ignore_errors=True);return jsonify(error="归档中没有可更新文件"),400
    cur=get_db().execute("INSERT INTO app_updates(version,status,archive_path,stage_path,manifest_json,diff_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",(version,"staged",str(archive),str(stage),json.dumps(manifest),json.dumps(diff),g.user["id"],int(time.time())));get_db().commit()
    return jsonify(id=cur.lastrowid,version=version,status="staged",summary={k:len(v) for k,v in diff.items()},diff=diff),201

@bp.post("/api/updates/<int:update_id>/apply")
@require_user(admin=True)
def apply(update_id):
    row=get_db().execute("SELECT * FROM app_updates WHERE id=?",(update_id,)).fetchone()
    if not row or row["status"]!="staged":return jsonify(error="更新不存在或状态不是 staged"),409
    manifest=json.loads(row["manifest_json"]);stage=Path(row["stage_path"]);root=project_root();backup=Path(current_app.config["DATA_DIR"])/"update_backups"/str(update_id);backup.mkdir(parents=True,exist_ok=True);added=[]
    try:
        for item in manifest:
            rel=Path(item["path"]);src=stage/rel;dst=root/rel
            if not src.is_file():raise ValueError(f"暂存文件缺失：{rel}")
            if dst.exists():
                old=backup/rel;old.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dst,old)
            else:added.append(str(rel))
            dst.parent.mkdir(parents=True,exist_ok=True);tmp=dst.with_name(dst.name+".update-tmp");shutil.copy2(src,tmp);os.replace(tmp,dst)
        meta={"added":added,"backup":str(backup)};(backup/"rollback.json").write_text(json.dumps(meta),encoding="utf-8")
        get_db().execute("UPDATE app_updates SET status='applied',backup_path=?,applied_at=? WHERE id=?",(str(backup),int(time.time()),update_id));get_db().commit()
    except Exception as e:get_db().rollback();return jsonify(error=f"应用失败：{e}"),500
    restart=bool((request.get_json(silent=True) or {}).get("restart",True))
    if restart and not current_app.config.get("TESTING"):
        def stop():time.sleep(.8);os._exit(75)
        threading.Thread(target=stop,daemon=True).start()
    return jsonify(ok=True,version=row["version"],restart_scheduled=restart)

@bp.post("/api/updates/<int:update_id>/rollback")
@require_user(admin=True)
def rollback(update_id):
    row=get_db().execute("SELECT * FROM app_updates WHERE id=?",(update_id,)).fetchone()
    if not row or row["status"]!="applied" or not row["backup_path"]:return jsonify(error="该版本不可回滚"),409
    root=project_root();backup=Path(row["backup_path"]);meta=json.loads((backup/"rollback.json").read_text())
    for rel in meta["added"]:
        p=root/rel
        if p.is_file():p.unlink()
    for p in backup.rglob("*"):
        if p.is_file() and p.name!="rollback.json":
            rel=p.relative_to(backup);dst=root/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dst)
    get_db().execute("UPDATE app_updates SET status='rolled_back',rolled_back_at=? WHERE id=?",(int(time.time()),update_id));get_db().commit()
    if not current_app.config.get("TESTING"):
        threading.Thread(target=lambda:(time.sleep(.8),os._exit(75)),daemon=True).start()
    return jsonify(ok=True,restart_scheduled=True)
