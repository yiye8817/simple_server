from pathlib import Path
from functools import wraps
from flask import Blueprint, current_app, g, jsonify, request, send_from_directory
from .files import safe_path
from .security import current_user

bp=Blueprint("drawio",__name__)

def ext_user(fn):
    @wraps(fn)
    def wrapped(*a,**kw):
        user=current_user()
        if not user:return jsonify(error="请填写登录后得到的 token"),401
        g.user=user;return fn(*a,**kw)
    return wrapped

@bp.get("/drawio/")
@bp.get("/drawio/<path:name>")
def drawio_static(name="index.html"):
    return send_from_directory(current_app.config["DRAWIO_ROOT"],name)

@bp.get("/api/external/folder-items")
@ext_user
def folder_items():
    try:p=safe_path(request.args.get("path","/") or "/")
    except ValueError as e:return jsonify(error=str(e)),400
    if not p.is_dir():return jsonify(error="目录不存在"),404
    root=safe_path();items=[]
    for x in sorted(p.iterdir(),key=lambda v:(not v.is_dir(),v.name.lower())):
        rel=str(x.relative_to(root));items.append({"id":rel,"file_id":None if x.is_dir() else rel,"folder_id":rel if x.is_dir() else None,"name":x.name,"path":rel,"is_dir":x.is_dir(),"size":x.stat().st_size if x.is_file() else 0})
    return jsonify(items=items,path=str(p.relative_to(root)) if p!=root else "/")

@bp.get("/api/external/files/<path:file_id>/content")
@ext_user
def external_read(file_id):
    try:p=safe_path(file_id)
    except ValueError as e:return jsonify(error=str(e)),400
    if not p.is_file():return jsonify(error="文件不存在"),404
    return jsonify(content=p.read_text(encoding="utf-8"),file_id=file_id,name=p.name)

@bp.post("/api/external/file")
@ext_user
def external_write():
    d=request.get_json(silent=True) or {};name=Path(d.get("name","")).name
    if not name:return jsonify(error="文件名不能为空"),400
    target=safe_path(str(Path(d.get("path") or "")/name))
    if target.exists() and not d.get("overwrite"):return jsonify(error="文件已存在"),409
    target.parent.mkdir(parents=True,exist_ok=True);target.write_text(d.get("content", ""),encoding="utf-8")
    rel=str(target.relative_to(safe_path()));return jsonify(id=rel,file_id=rel,name=name,path=rel)

@bp.post("/api/external/files/<path:file_id>/share")
@ext_user
def no_share(file_id):return jsonify(error="未启用匿名公开分享"),501
