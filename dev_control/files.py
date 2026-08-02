import hashlib, os, tempfile, time
from pathlib import Path
from flask import Blueprint, current_app, g, jsonify, request, send_file
from .security import require_user

bp=Blueprint("files",__name__,url_prefix="/api/files")

def safe_path(rel=""):
    root=Path(current_app.config["SERVER_FILES_ROOT"]).resolve(); target=(root/rel.lstrip("/")).resolve()
    if target != root and root not in target.parents: raise ValueError("路径越界")
    return target

@bp.get("")
@require_user()
def listing():
    try: p=safe_path(request.args.get("path", ""))
    except ValueError as e: return jsonify(error=str(e)),400
    if not p.exists(): return jsonify(error="路径不存在"),404
    if not p.is_dir(): return jsonify(error="不是目录"),400
    items=[]
    for x in sorted(p.iterdir(),key=lambda v:(not v.is_dir(),v.name.lower())):
        st=x.stat(); items.append({"name":x.name,"path":str(x.relative_to(safe_path())),"is_dir":x.is_dir(),"size":st.st_size if x.is_file() else 0,"mtime":int(st.st_mtime)})
    return jsonify(path=str(p.relative_to(safe_path())) if p!=safe_path() else "",items=items)

@bp.get("/content")
@require_user()
def content():
    try: p=safe_path(request.args.get("path", ""))
    except ValueError as e: return jsonify(error=str(e)),400
    if not p.is_file(): return jsonify(error="文件不存在"),404
    return send_file(p,as_attachment=request.args.get("download")=="1",download_name=p.name)

@bp.put("/content")
@require_user()
def save():
    try: p=safe_path(request.args.get("path", ""))
    except ValueError as e: return jsonify(error=str(e)),400
    if p==safe_path(): return jsonify(error="缺少文件名"),400
    p.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=".upload-",dir=p.parent)
    sha=hashlib.sha256(); size=0
    try:
        with os.fdopen(fd,"wb") as f:
            while True:
                chunk=request.stream.read(65536)
                if not chunk: break
                f.write(chunk); sha.update(chunk); size+=len(chunk)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp,p)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return jsonify(ok=True,path=str(p.relative_to(safe_path())),size=size,sha256=sha.hexdigest())

@bp.post("/directory")
@require_user()
def mkdir():
    d=request.get_json(silent=True) or {}
    try: p=safe_path(d.get("path", "")); p.mkdir(parents=True,exist_ok=bool(d.get("parents",True)))
    except (ValueError,OSError) as e: return jsonify(error=str(e)),400
    return jsonify(ok=True)

@bp.delete("")
@require_user()
def delete():
    import shutil
    try: p=safe_path(request.args.get("path", ""))
    except ValueError as e: return jsonify(error=str(e)),400
    if p==safe_path(): return jsonify(error="禁止删除存储根目录"),400
    if not p.exists(): return jsonify(error="路径不存在"),404
    if p.is_dir(): shutil.rmtree(p)
    else: p.unlink()
    return jsonify(ok=True)
