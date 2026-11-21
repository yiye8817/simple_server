import hashlib, json, os, re, shutil, tarfile, tempfile, threading, time
from pathlib import Path, PurePosixPath
from flask import Blueprint, current_app, g, jsonify, render_template, request, send_file
from .db import get_db
from .security import require_user

bp=Blueprint("self_updates",__name__)
BLOCKED={".git",".venv","data","dist","__pycache__"}
MAX_FILES=15000
MAX_UNPACKED=1024*1024*1024
VERSION_PATTERN=re.compile(r"^v?(\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?)$")
UPDATE_LOCK = threading.Lock()

def version_from_filename(filename):
    name=Path(filename or "").name
    if not name.lower().endswith(".tar.gz"):return ""
    stem=name[:-7]
    for prefix in ("simple-server-","server-","update-"):
        if stem.lower().startswith(prefix):stem=stem[len(prefix):];break
    match=VERSION_PATTERN.fullmatch(stem)
    return match.group(1) if match else ""

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


def row_summary(row):
    update = dict(row)
    diff = json.loads(update.pop("diff_json") or "{}")
    update["summary"] = {key: len(diff.get(key, [])) for key in ("added", "modified", "unchanged", "deleted")}
    update["changes"] = {key: [item["path"] for item in diff.get(key, [])]
                         for key in ("added", "modified", "deleted")}
    return update


def package_directory(row):
    base = (Path(current_app.config["DATA_DIR"]) / "updates").resolve()
    archive = Path(row["archive_path"])
    directory = archive.parent
    if (archive.name != "update.tar.gz" or not directory.name.startswith("stage-")
            or directory.parent.resolve() != base or directory.is_symlink()
            or Path(row["stage_path"]) != directory / "payload"):
        raise ValueError("更新包路径无效")
    return base / directory.name


def remove_update_directory(directory):
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        if directory.exists():
            raise


@bp.get("/updates/")
def page():return render_template("updates.html")

@bp.get("/api/updates")
@require_user(admin=True)
def list_updates():
    summary = request.args.get("summary") == "1"
    columns = "id, version, status, created_at, diff_json" if summary else "*"
    rows = get_db().execute(f"SELECT {columns} FROM app_updates WHERE status <> 'deleted' ORDER BY id DESC LIMIT 100")
    version_file = project_root() / "VERSION"
    version = version_file.read_text().strip() if version_file.exists() else "unknown"
    serialize = row_summary if summary else row_data
    return jsonify(current_version=version, updates=[serialize(row) for row in rows])

@bp.post("/api/updates/upload")
@require_user(admin=True)
def upload():
    upload=request.files.get("file")
    if not upload or not upload.filename.lower().endswith(".tar.gz"):return jsonify(error="需要 .tar.gz 文件"),400
    version=version_from_filename(upload.filename)
    if not version:return jsonify(error="无法从文件名获取版本号，请使用 simple-server-1.2.3.tar.gz 格式"),400
    base=Path(current_app.config["DATA_DIR"])/"updates";base.mkdir(parents=True,exist_ok=True);temp=Path(tempfile.mkdtemp(prefix="stage-",dir=base));archive=temp/"update.tar.gz";upload.save(archive);stage=temp/"payload";stage.mkdir()
    try:manifest=extract_safe(archive,stage);diff=compare(manifest,stage)
    except Exception as e:shutil.rmtree(temp,ignore_errors=True);return jsonify(error=str(e)),400
    if not manifest:shutil.rmtree(temp,ignore_errors=True);return jsonify(error="归档中没有可更新文件"),400
    cur=get_db().execute("INSERT INTO app_updates(version,status,archive_path,stage_path,manifest_json,diff_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",(version,"staged",str(archive),str(stage),json.dumps(manifest),json.dumps(diff),g.user["id"],int(time.time())));get_db().commit()
    return jsonify(id=cur.lastrowid,version=version,status="staged",summary={k:len(v) for k,v in diff.items()},diff=diff),201


@bp.get("/api/updates/<int:update_id>/download")
@require_user(admin=True)
def download(update_id):
    with UPDATE_LOCK:
        row = get_db().execute("SELECT * FROM app_updates WHERE id=?", (update_id,)).fetchone()
        if not row or row["status"] in ("deleting", "deleted"):
            return jsonify(error="更新版本不存在"), 404
        try:
            archive = package_directory(row) / "update.tar.gz"
            if archive.is_symlink() or not archive.is_file():
                raise FileNotFoundError
            response = send_file(archive, mimetype="application/gzip", as_attachment=True,
                                 download_name=f"simple-server-{row['version']}.tar.gz")
        except (ValueError, FileNotFoundError):
            return jsonify(error="更新包文件不存在"), 404
        response.cache_control.no_store = True
        return response


@bp.delete("/api/updates/<int:update_id>")
@require_user(admin=True)
def delete(update_id):
    with UPDATE_LOCK:
        db = get_db()
        row = db.execute("SELECT * FROM app_updates WHERE id=?", (update_id,)).fetchone()
        if not row:
            return jsonify(error="更新版本不存在"), 404
        if row["status"] == "deleted":
            return jsonify(ok=True)

        backup = (Path(current_app.config["DATA_DIR"]) / "update_backups").resolve() / str(update_id)
        try:
            directory = package_directory(row)
            if backup.is_symlink() or (row["backup_path"] and Path(row["backup_path"]).resolve() != backup):
                raise ValueError("回滚备份路径无效")
        except ValueError as exc:
            return jsonify(error=str(exc)), 409

        # Retain the in-progress record so cleanup can be retried after a failure.
        with db:
            db.execute("UPDATE app_updates SET status='deleting' WHERE id=?", (update_id,))
        try:
            remove_update_directory(directory)
            remove_update_directory(backup)
        except OSError:
            current_app.logger.exception("Failed to delete update %s", update_id)
            return jsonify(error="更新文件清理失败，请重试删除"), 500

        # Preserve the ID so delayed requests cannot target a newly uploaded version.
        with db:
            db.execute("""UPDATE app_updates SET status='deleted', archive_path='', stage_path='',
                          backup_path=NULL, manifest_json='[]', diff_json='{}' WHERE id=?""", (update_id,))
        return jsonify(ok=True)


@bp.post("/api/updates/<int:update_id>/apply")
@require_user(admin=True)
def apply(update_id):
    with UPDATE_LOCK:
        db = get_db()
        row = db.execute("SELECT * FROM app_updates WHERE id=?", (update_id,)).fetchone()
        if not row or row["status"] != "staged":
            return jsonify(error="更新不存在或状态不是 staged"), 409
        manifest = json.loads(row["manifest_json"])
        stage = Path(row["stage_path"])
        root = project_root()
        backup = Path(current_app.config["DATA_DIR"]) / "update_backups" / str(update_id)
        backup.mkdir(parents=True, exist_ok=True)
        added = []
        try:
            for item in manifest:
                rel = Path(item["path"])
                src, dst = stage / rel, root / rel
                if not src.is_file():
                    raise ValueError(f"暂存文件缺失：{rel}")
                if dst.exists():
                    old = backup / rel
                    old.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, old)
                else:
                    added.append(str(rel))
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".update-tmp")
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
            meta = {"added": added, "backup": str(backup)}
            (backup / "rollback.json").write_text(json.dumps(meta), encoding="utf-8")
            with db:
                db.execute("UPDATE app_updates SET status='applied',backup_path=?,applied_at=? WHERE id=?",
                           (str(backup), int(time.time()), update_id))
        except Exception as exc:
            return jsonify(error=f"应用失败：{exc}"), 500
    restart=bool((request.get_json(silent=True) or {}).get("restart",True))
    if restart and not current_app.config.get("TESTING"):
        def stop():time.sleep(.8);os._exit(75)
        threading.Thread(target=stop,daemon=True).start()
    return jsonify(ok=True,version=row["version"],restart_scheduled=restart)

@bp.post("/api/updates/<int:update_id>/rollback")
@require_user(admin=True)
def rollback(update_id):
    with UPDATE_LOCK:
        db = get_db()
        row = db.execute("SELECT * FROM app_updates WHERE id=?", (update_id,)).fetchone()
        if not row or row["status"] != "applied" or not row["backup_path"]:
            return jsonify(error="该版本不可回滚"), 409
        root = project_root()
        backup = Path(row["backup_path"])
        meta = json.loads((backup / "rollback.json").read_text())
        for rel in meta["added"]:
            p = root / rel
            if p.is_file():
                p.unlink()
        for p in backup.rglob("*"):
            if p.is_file() and p.name != "rollback.json":
                dst = root / p.relative_to(backup)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
        with db:
            db.execute("UPDATE app_updates SET status='rolled_back',rolled_back_at=? WHERE id=?",
                       (int(time.time()), update_id))
    if not current_app.config.get("TESTING"):
        threading.Thread(target=lambda:(time.sleep(.8),os._exit(75)),daemon=True).start()
    return jsonify(ok=True,restart_scheduled=True)
