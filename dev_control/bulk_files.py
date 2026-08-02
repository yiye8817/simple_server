import shutil
from flask import Blueprint, jsonify, request
from .files import safe_path
from .security import require_user

bp=Blueprint("bulk_files",__name__)

@bp.post("/api/files/bulk-delete")
@require_user()
def bulk_delete():
    paths=(request.get_json(silent=True) or {}).get("paths") or []
    if not isinstance(paths,list) or not paths or len(paths)>500:return jsonify(error="paths 必须包含 1-500 个路径"),400
    root=safe_path();resolved=[]
    try:
        for raw in paths:
            p=safe_path(str(raw))
            if p==root:raise ValueError("禁止删除存储根目录")
            resolved.append((str(raw),p))
    except ValueError as e:return jsonify(error=str(e)),400
    results=[]
    for raw,p in resolved:
        try:
            if not p.exists():results.append({"path":raw,"ok":False,"error":"路径不存在"});continue
            shutil.rmtree(p) if p.is_dir() else p.unlink();results.append({"path":raw,"ok":True})
        except OSError as e:results.append({"path":raw,"ok":False,"error":str(e)})
    return jsonify(ok=all(x["ok"] for x in results),deleted=sum(x["ok"] for x in results),results=results)
