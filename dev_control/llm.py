import json, time, urllib.error, urllib.parse, urllib.request
from flask import Blueprint, g, jsonify, render_template, request
from .db import get_db
from .security import require_user

bp=Blueprint("llm",__name__)
STYLES={"openai","anthropic","google","custom"}

def mask(key):
    if not key:return ""
    return key[:4]+"…"+key[-4:] if len(key)>10 else "••••••••"

def public(row):
    d=dict(row);d["api_key_masked"]=mask(d.pop("api_key", ""));d["keywords"]=[x for x in (d.get("keywords") or "").split(",") if x];d["models"]=json.loads(d.pop("models_json") or "[]");d["headers_configured"]=bool(json.loads(d.pop("headers_json") or "{}"));return d

def endpoint_and_headers(row):
    base=row["base_uri"].rstrip("/");style=row["api_style"];path=(row["models_path"] or "").strip()
    if not path:path="/models" if style in ("openai","custom") else ("/v1/models" if style=="anthropic" else "/v1beta/models")
    url=base+path if path.startswith("/") else base+"/"+path
    headers={"Accept":"application/json","User-Agent":"dev-control-llm-check/1.0"};key=row["api_key"] or ""
    if style in ("openai","custom") and key:headers["Authorization"]="Bearer "+key
    elif style=="anthropic":headers.update({"x-api-key":key,"anthropic-version":"2023-06-01"})
    elif style=="google" and key:url+=("&" if "?" in url else "?")+urllib.parse.urlencode({"key":key})
    extra=json.loads(row["headers_json"] or "{}")
    for k,v in extra.items():headers[str(k)]=str(v).replace("${API_KEY}",key)
    return url,headers

def extract_models(data):
    arr=data if isinstance(data,list) else data.get("data",data.get("models",data.get("items",[]))) if isinstance(data,dict) else []
    out=[]
    for item in arr if isinstance(arr,list) else []:
        if isinstance(item,str):mid=item
        elif isinstance(item,dict):mid=item.get("id") or item.get("name") or item.get("model")
        else:mid=None
        if mid:
            mid=str(mid);mid=mid.split("models/",1)[-1] if mid.startswith("models/") else mid
            if mid not in out:out.append(mid)
    return out

def run_check(row):
    url,headers=endpoint_and_headers(row);started=time.time();req=urllib.request.Request(url,headers=headers,method="GET")
    try:
        with urllib.request.urlopen(req,timeout=15) as resp:
            raw=resp.read(4*1024*1024);data=json.loads(raw.decode("utf-8"));models=extract_models(data)
            return {"ok":True,"status_code":resp.status,"latency_ms":round((time.time()-started)*1000),"models":models,"model_count":len(models),"endpoint":url.split("?",1)[0]}
    except urllib.error.HTTPError as e:
        body=e.read(1000).decode("utf-8","replace");return {"ok":False,"status_code":e.code,"latency_ms":round((time.time()-started)*1000),"error":body or str(e),"models":[]}
    except Exception as e:return {"ok":False,"status_code":None,"latency_ms":round((time.time()-started)*1000),"error":f"{type(e).__name__}: {e}","models":[]}

@bp.get("/llm/")
def page():return render_template("llm.html")

@bp.get("/api/llm/providers")
@require_user()
def providers():return jsonify(providers=[public(r) for r in get_db().execute("SELECT * FROM llm_providers ORDER BY name")])

@bp.post("/api/llm/providers")
@require_user(admin=True)
def create():
    d=request.get_json(silent=True) or {};name=str(d.get("name","")).strip();base=str(d.get("base_uri","")).strip();style=d.get("api_style","openai")
    if not name or not base.startswith(("http://","https://")) or style not in STYLES:return jsonify(error="名称、有效的 http(s) Base URI 和协议类型必填"),400
    try:
        cur=get_db().execute("INSERT INTO llm_providers(name,base_uri,api_key,official_url,keywords,api_style,models_path,headers_json,models_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(name,base,str(d.get("api_key","")).strip(),str(d.get("official_url","")).strip(),",".join(d.get("keywords",[])) if isinstance(d.get("keywords"),list) else str(d.get("keywords","")).strip(),style,str(d.get("models_path","")).strip(),json.dumps(d.get("headers") or {}),"[]",int(bool(d.get("enabled",True))),int(time.time()),int(time.time())));get_db().commit()
    except Exception as e:return jsonify(error="名称已存在" if "UNIQUE" in str(e) else str(e)),409
    return jsonify(id=cur.lastrowid),201

@bp.patch("/api/llm/providers/<int:pid>")
@require_user(admin=True)
def update(pid):
    d=request.get_json(silent=True) or {};allowed={"name","base_uri","api_key","official_url","keywords","api_style","models_path","headers","enabled"};fields=[];params=[]
    for k in allowed:
        if k not in d:continue
        col="headers_json" if k=="headers" else k;v=d[k]
        if k=="api_key" and not v:continue
        if k=="keywords" and isinstance(v,list):v=",".join(v)
        if k=="headers":v=json.dumps(v or {})
        if k=="enabled":v=int(bool(v))
        fields.append(col+"=?");params.append(v)
    if not fields:return jsonify(error="没有可更新字段"),400
    fields.append("updated_at=?");params.extend([int(time.time()),pid]);get_db().execute("UPDATE llm_providers SET "+",".join(fields)+" WHERE id=?",params);get_db().commit();return jsonify(ok=True)

@bp.delete("/api/llm/providers/<int:pid>")
@require_user(admin=True)
def delete(pid):get_db().execute("DELETE FROM llm_providers WHERE id=?",(pid,));get_db().commit();return jsonify(ok=True)

@bp.post("/api/llm/providers/<int:pid>/check")
@require_user(admin=True)
def check(pid):
    row=get_db().execute("SELECT * FROM llm_providers WHERE id=?",(pid,)).fetchone()
    if not row:return jsonify(error="配置不存在"),404
    result=run_check(row);now=int(time.time());get_db().execute("UPDATE llm_providers SET models_json=?,last_check_at=?,last_check_ok=?,last_error=?,updated_at=? WHERE id=?",(json.dumps(result.get("models",[])),now,int(result["ok"]),result.get("error"),now,pid));get_db().commit();return jsonify(result)

@bp.get("/api/llm/providers/<int:pid>/config")
@require_user(admin=True)
def config(pid):
    row=get_db().execute("SELECT * FROM llm_providers WHERE id=?",(pid,)).fetchone()
    if not row:return jsonify(error="配置不存在"),404
    url,headers=endpoint_and_headers(row);return jsonify(id=row["id"],name=row["name"],base_uri=row["base_uri"],api_key=row["api_key"],official_url=row["official_url"],keywords=[x for x in row["keywords"].split(",") if x],api_style=row["api_style"],models_path=row["models_path"],headers=json.loads(row["headers_json"] or "{}"),models=json.loads(row["models_json"] or "[]"),enabled=bool(row["enabled"]))
