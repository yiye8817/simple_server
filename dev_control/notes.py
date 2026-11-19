import time
from flask import Blueprint, g, jsonify, render_template, request
from .db import get_db
from .security import require_user

bp=Blueprint("notes",__name__)
TITLE_MAX=200
CONTENT_MAX=200000
KINDS=("text","table")
EMPTY_TABLE='{"headers":["列1","列2","列3"],"rows":[["","",""],["","",""]]}'

def as_note(r):
    kind=r["kind"] if "kind" in r.keys() else "text"
    return {"id":r["id"],"title":r["title"],"content":r["content"],"kind":kind or "text","created_at":r["created_at"],"updated_at":r["updated_at"]}

def parse_fields(d, partial=False):
    out={}
    if "title" in d or not partial:
        title=str(d.get("title") or "").strip()
        if not title: raise ValueError("标题不能为空")
        if len(title)>TITLE_MAX: raise ValueError("标题过长")
        out["title"]=title
    if "kind" in d or not partial:
        kind=str(d.get("kind") or "text").strip() or "text"
        if kind not in KINDS: raise ValueError("类型无效")
        out["kind"]=kind
    if "content" in d or not partial:
        content=str(d.get("content") or "")
        if len(content)>CONTENT_MAX: raise ValueError("正文过长")
        out["content"]=content
    if out.get("kind")=="table" and not (out.get("content") or "").strip():
        out["content"]=EMPTY_TABLE
    if not out: raise ValueError("没有可更新字段")
    return out

def owned(nid):
    return get_db().execute("SELECT * FROM notes WHERE id=? AND user_id=?",(nid,g.user["id"])).fetchone()

@bp.get("/notes/")
def page():
    return render_template("notes.html")

@bp.get("/api/notes")
@require_user()
def list_notes():
    rows=get_db().execute("SELECT * FROM notes WHERE user_id=? ORDER BY updated_at DESC,id DESC",(g.user["id"],)).fetchall()
    return jsonify(notes=[as_note(r) for r in rows])

@bp.post("/api/notes")
@require_user()
def create_note():
    try: fields=parse_fields(request.get_json(silent=True) or {}, partial=False)
    except ValueError as e: return jsonify(error=str(e)),400
    now=int(time.time())
    cur=get_db().execute("INSERT INTO notes(user_id,title,content,kind,created_at,updated_at) VALUES(?,?,?,?,?,?)",(g.user["id"],fields["title"],fields["content"],fields["kind"],now,now))
    get_db().commit()
    return jsonify(as_note(get_db().execute("SELECT * FROM notes WHERE id=?",(cur.lastrowid,)).fetchone())),201

@bp.get("/api/notes/<int:nid>")
@require_user()
def get_note(nid):
    r=owned(nid)
    if not r: return jsonify(error="小记不存在"),404
    return jsonify(as_note(r))

@bp.patch("/api/notes/<int:nid>")
@require_user()
def patch_note(nid):
    if not owned(nid): return jsonify(error="小记不存在"),404
    try: fields=parse_fields(request.get_json(silent=True) or {}, partial=True)
    except ValueError as e: return jsonify(error=str(e)),400
    now=int(time.time())
    get_db().execute("UPDATE notes SET "+",".join(k+"=?" for k in fields)+",updated_at=? WHERE id=?",list(fields.values())+[now,nid])
    get_db().commit()
    return jsonify(as_note(get_db().execute("SELECT * FROM notes WHERE id=?",(nid,)).fetchone()))

@bp.delete("/api/notes/<int:nid>")
@require_user()
def delete_note(nid):
    if not owned(nid): return jsonify(error="小记不存在"),404
    get_db().execute("DELETE FROM notes WHERE id=?",(nid,)); get_db().commit()
    return jsonify(ok=True)
