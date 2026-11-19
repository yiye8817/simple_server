import hashlib, mimetypes, os, tempfile, time
from pathlib import Path
from flask import Blueprint, current_app, g, jsonify, request, send_file
from .security import require_user

bp=Blueprint("files",__name__,url_prefix="/api/files")
PREVIEW_MAX=100*1024*1024
IMAGE_EXT={".png",".jpg",".jpeg",".gif",".webp",".bmp",".ico",".svg",".avif"}
JSON_EXT={".json",".jsonl",".geojson"}
MD_EXT={".md",".markdown",".mdown"}
CODE_EXT={".py",".js",".mjs",".cjs",".ts",".tsx",".jsx",".css",".scss",".less",".html",".htm",".xml",".yml",".yaml",".toml",".ini",".conf",".sh",".bash",".zsh",".c",".h",".cpp",".hpp",".cc",".java",".kt",".go",".rs",".rb",".php",".sql",".r",".lua",".swift",".cs",".vue",".svelte",".pl",".ps1",".bat"}
TEXT_EXT={".txt",".log",".csv",".tsv",".rst",".env"}
PDF_EXT={".pdf"}
AUDIO_EXT={".mp3",".wav",".ogg",".oga",".m4a",".flac",".aac",".opus",".wma"}
VIDEO_EXT={".mp4",".webm",".ogv",".mov",".m4v"}

def preview_kind(name, is_dir=False):
    if is_dir: return None
    ext=Path(name).suffix.lower()
    if ext in IMAGE_EXT: return "image"
    if ext == ".drawio": return "drawio"
    if ext in JSON_EXT: return "json"
    if ext in MD_EXT: return "markdown"
    if ext in CODE_EXT: return "code"
    if ext in PDF_EXT: return "pdf"
    if ext in AUDIO_EXT: return "audio"
    if ext in VIDEO_EXT: return "video"
    if ext in TEXT_EXT: return "text"
    mime=mimetypes.guess_type(name)[0] or ""
    if mime=="application/json": return "json"
    if mime=="text/markdown": return "markdown"
    if mime.startswith("text/"): return "text"
    if mime.startswith("image/"): return "image"
    if mime.startswith("audio/"): return "audio"
    if mime.startswith("video/"): return "video"
    return None

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
        st=x.stat(); items.append({"name":x.name,"path":str(x.relative_to(safe_path())),"is_dir":x.is_dir(),"size":st.st_size if x.is_file() else 0,"mtime":int(st.st_mtime),"mime":mimetypes.guess_type(x.name)[0],"preview":preview_kind(x.name,x.is_dir())})
    return jsonify(path=str(p.relative_to(safe_path())) if p!=safe_path() else "",items=items)

@bp.get("/content")
@require_user()
def content():
    try: p=safe_path(request.args.get("path", ""))
    except ValueError as e: return jsonify(error=str(e)),400
    if not p.is_file(): return jsonify(error="文件不存在"),404
    if request.args.get("preview")=="1" and p.stat().st_size>PREVIEW_MAX:
        return jsonify(error="文件超过 100MB，无法预览，请下载后查看"),413
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
