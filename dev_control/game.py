import random, time
from pathlib import Path
from flask import Blueprint, g, jsonify, request
from .db import get_db
from .security import require_user

bp=Blueprint("typing_game",__name__)
WORDS=[]
TRANSLATIONS={}

def word_list():
    if not WORDS:
        path=Path(__file__).resolve().parent.parent/"assets"/"lemmas_zh.tsv"
        for line in path.read_text(encoding="utf-8").splitlines():
            parts=line.split("\t",2)
            if len(parts)>=2:
                word,chinese=parts[0].strip(),parts[1].strip();pronunciation=parts[2].strip() if len(parts)>2 else ""
                if word and chinese:TRANSLATIONS[word]={"chinese":chinese,"pronunciation":pronunciation}
        WORDS.extend(TRANSLATIONS)
    return WORDS

@bp.get("/api/typing/word")
@require_user()
def random_word():
    words=word_list()
    if not words:return jsonify(error="词库为空"),503
    word=random.choice(words);info=TRANSLATIONS[word]
    return jsonify(word=word,chinese=info["chinese"],pronunciation=info["pronunciation"],word_count=len(words))

@bp.post("/api/typing/results")
@require_user()
def save_result():
    d=request.get_json(silent=True) or {};word=str(d.get("word", ""))[:100];typed=str(d.get("typed", ""))[:200]
    if not word:return jsonify(error="word 不能为空"),400
    correct=bool(d.get("correct")) and typed.strip().lower()==word.lower()
    cur=get_db().execute("INSERT INTO typing_results(user_id,session_id,word,typed,correct,timed_out,elapsed_ms,word_limit_s,practice_limit_s,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(g.user["id"],str(d.get("session_id") or "")[:64],word,typed,int(correct),int(bool(d.get("timed_out"))),max(0,int(d.get("elapsed_ms") or 0)),d.get("word_limit_s"),d.get("practice_limit_s"),int(time.time())))
    get_db().commit();return jsonify(id=cur.lastrowid,correct=correct),201

@bp.get("/api/typing/history")
@require_user()
def history():
    limit=min(500,max(1,int(request.args.get("limit",100))));rows=get_db().execute("SELECT id,session_id,word,typed,correct,timed_out,elapsed_ms,word_limit_s,practice_limit_s,created_at FROM typing_results WHERE user_id=? ORDER BY id DESC LIMIT ?",(g.user["id"],limit)).fetchall()
    total=get_db().execute("SELECT COUNT(*),COALESCE(SUM(correct),0),COALESCE(AVG(CASE WHEN correct=1 THEN elapsed_ms END),0) FROM typing_results WHERE user_id=?",(g.user["id"],)).fetchone()
    return jsonify(results=[dict(r) for r in rows],stats={"total":total[0],"correct":total[1],"accuracy":round(total[1]*100/total[0],1) if total[0] else 0,"avg_correct_ms":round(total[2])})

@bp.get("/typing/")
def page():
    from flask import render_template
    return render_template("typing.html")
