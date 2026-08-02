"""把原 web_server 的两个 Flask 应用挂载到统一 WSGI 进程。"""
import hashlib, importlib.util, sqlite3, sys, time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs
from werkzeug.middleware.dispatcher import DispatcherMiddleware

class TokenGate:
    def __init__(self, app, db_path): self.app,self.db_path=app,db_path
    def valid(self, token):
        if not token:return False
        db=sqlite3.connect(self.db_path)
        row=db.execute("SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",(hashlib.sha256(token.encode()).hexdigest(),int(time.time()))).fetchone();db.close()
        return bool(row)
    def __call__(self,environ,start_response):
        query=parse_qs(environ.get("QUERY_STRING", "")); token=(query.get("access_token") or [""])[0]
        if not token:
            c=SimpleCookie();c.load(environ.get("HTTP_COOKIE", ""));token=c.get("legacy_token").value if c.get("legacy_token") else ""
        if not self.valid(token):
            start_response("401 Unauthorized",[("Content-Type","text/plain; charset=utf-8")]);return ["需要有效用户 token；首次访问请添加 ?access_token=<token>".encode()]
        def sr(status,headers,exc_info=None):
            if query.get("access_token"):headers.append(("Set-Cookie",f"legacy_token={token}; Path=/tools; HttpOnly; SameSite=Lax"))
            return start_response(status,headers,exc_info)
        return self.app(environ,sr)

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

def mount_legacy(app):
    root=Path(__file__).resolve().parent.parent/"vendor"/"web_server"
    sys.path.insert(0,str(root))
    port_app=load("integrated_port_server",root/"app.py").app
    port_app.template_folder=str(root/"templates")
    frp_app=load("integrated_frp_server",root/"frp_web.py").app
    frp_app.template_folder=str(root/"templates")
    gate=lambda target:TokenGate(target.wsgi_app,str(Path(app.config["DATA_DIR"])/"control.db"))
    app.wsgi_app=DispatcherMiddleware(app.wsgi_app,{"/tools/ports":gate(port_app),"/tools/frp":gate(frp_app)})
