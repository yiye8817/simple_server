import sqlite3
from pathlib import Path
from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'user',active INTEGER NOT NULL DEFAULT 1,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires_at INTEGER NOT NULL,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS user_api_tokens(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,label TEXT NOT NULL,token TEXT UNIQUE NOT NULL,token_hash TEXT UNIQUE NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at INTEGER NOT NULL,last_used_at INTEGER);
CREATE INDEX IF NOT EXISTS idx_user_api_tokens_hash ON user_api_tokens(token_hash,active);
CREATE TABLE IF NOT EXISTS devices(id INTEGER PRIMARY KEY,device_uid TEXT UNIQUE NOT NULL,name TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,owner_id INTEGER REFERENCES users(id),active INTEGER NOT NULL DEFAULT 1,online INTEGER NOT NULL DEFAULT 0,last_seen INTEGER,stats_json TEXT NOT NULL DEFAULT '{}',created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,kind TEXT NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'pending',claim_token TEXT,result_json TEXT,created_by INTEGER REFERENCES users(id),created_at INTEGER NOT NULL,claimed_at INTEGER,finished_at INTEGER);
CREATE TABLE IF NOT EXISTS blobs(token TEXT PRIMARY KEY,device_id INTEGER REFERENCES devices(id),owner_id INTEGER REFERENCES users(id),direction TEXT NOT NULL,filename TEXT,size INTEGER,sha256 TEXT,expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),name TEXT NOT NULL,archive_path TEXT NOT NULL,sha256 TEXT NOT NULL,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS llm_providers(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,base_uri TEXT NOT NULL,api_key TEXT NOT NULL DEFAULT '',official_url TEXT NOT NULL DEFAULT '',keywords TEXT NOT NULL DEFAULT '',api_style TEXT NOT NULL DEFAULT 'openai',models_path TEXT NOT NULL DEFAULT '',headers_json TEXT NOT NULL DEFAULT '{}',models_json TEXT NOT NULL DEFAULT '[]',enabled INTEGER NOT NULL DEFAULT 1,last_check_at INTEGER,last_check_ok INTEGER,last_error TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS access_scenarios(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,description TEXT NOT NULL DEFAULT '',whitelist_json TEXT NOT NULL DEFAULT '[]',blacklist_json TEXT NOT NULL DEFAULT '[]',enabled INTEGER NOT NULL DEFAULT 1,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS typing_results(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),session_id TEXT NOT NULL,word TEXT NOT NULL,typed TEXT NOT NULL,correct INTEGER NOT NULL,timed_out INTEGER NOT NULL,elapsed_ms INTEGER NOT NULL,word_limit_s INTEGER,practice_limit_s INTEGER,created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_typing_user ON typing_results(user_id,id);
CREATE INDEX IF NOT EXISTS idx_tasks_poll ON tasks(device_id,status,id);
"""

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(str(Path(current_app.config["DATA_DIR"]) / "control.db"), timeout=20)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

def init_db(app):
    with app.app_context():
        import time
        db = get_db(); db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO users(username,password_hash,role,active,created_at) VALUES(?,?,?,?,?)", (app.config["ADMIN_USERNAME"],generate_password_hash(app.config["ADMIN_PASSWORD"]),"admin",1,int(time.time())))
        if app.config.get("SYNC_ADMIN_PASSWORD"):
            db.execute("UPDATE users SET password_hash=?, role='admin', active=1 WHERE username=?", (generate_password_hash(app.config["ADMIN_PASSWORD"]), app.config["ADMIN_USERNAME"]))
        db.commit()
    @app.teardown_appcontext
    def close(_exc):
        db = g.pop("db", None)
        if db is not None: db.close()

