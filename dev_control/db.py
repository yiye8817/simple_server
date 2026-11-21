import sqlite3
from pathlib import Path
from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS android_schedule_state(device_id INTEGER PRIMARY KEY REFERENCES android_devices(device_id) ON DELETE CASCADE,revision INTEGER NOT NULL DEFAULT 0,ack_revision INTEGER,ack_at INTEGER,timezone TEXT NOT NULL,through_date TEXT,week_start TEXT);
CREATE TABLE IF NOT EXISTS android_schedule_rules(id INTEGER PRIMARY KEY AUTOINCREMENT,device_id INTEGER NOT NULL REFERENCES android_devices(device_id) ON DELETE CASCADE,title TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',weekdays_json TEXT NOT NULL,start_minute INTEGER NOT NULL,end_minute INTEGER NOT NULL,valid_from TEXT NOT NULL,valid_until TEXT,version INTEGER NOT NULL DEFAULT 1,active INTEGER NOT NULL DEFAULT 1,created_ms INTEGER NOT NULL,changed_ms INTEGER NOT NULL,materialized_version INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_schedule_rules_device ON android_schedule_rules(device_id,active);
CREATE TABLE IF NOT EXISTS android_schedule_occurrences(id INTEGER PRIMARY KEY AUTOINCREMENT,device_id INTEGER NOT NULL REFERENCES android_devices(device_id) ON DELETE CASCADE,rule_id INTEGER NOT NULL REFERENCES android_schedule_rules(id) ON DELETE CASCADE,local_date TEXT NOT NULL,timezone TEXT NOT NULL DEFAULT 'UTC',manual_override INTEGER NOT NULL DEFAULT 0,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,title TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'pending',version INTEGER NOT NULL DEFAULT 0,completed_ms INTEGER,UNIQUE(rule_id,local_date));
CREATE INDEX IF NOT EXISTS idx_schedule_occurrences_device ON android_schedule_occurrences(device_id,local_date,status);
CREATE TABLE IF NOT EXISTS android_schedule_operations(device_id INTEGER NOT NULL REFERENCES android_devices(device_id) ON DELETE CASCADE,operation_id TEXT NOT NULL,payload_json TEXT NOT NULL,created_ms INTEGER NOT NULL,applied INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(device_id,operation_id));
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'user',active INTEGER NOT NULL DEFAULT 1,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires_at INTEGER NOT NULL,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS user_api_tokens(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,label TEXT NOT NULL,token TEXT UNIQUE NOT NULL,token_hash TEXT UNIQUE NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at INTEGER NOT NULL,last_used_at INTEGER);
CREATE INDEX IF NOT EXISTS idx_user_api_tokens_hash ON user_api_tokens(token_hash,active);
CREATE TABLE IF NOT EXISTS devices(id INTEGER PRIMARY KEY,device_uid TEXT UNIQUE NOT NULL,name TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,owner_id INTEGER REFERENCES users(id),active INTEGER NOT NULL DEFAULT 1,online INTEGER NOT NULL DEFAULT 0,last_seen INTEGER,stats_json TEXT NOT NULL DEFAULT '{}',created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,kind TEXT NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'pending',claim_token TEXT,result_json TEXT,created_by INTEGER REFERENCES users(id),created_at INTEGER NOT NULL,claimed_at INTEGER,finished_at INTEGER);
CREATE TABLE IF NOT EXISTS blobs(token TEXT PRIMARY KEY,device_id INTEGER REFERENCES devices(id),owner_id INTEGER REFERENCES users(id),direction TEXT NOT NULL,filename TEXT,size INTEGER,sha256 TEXT,expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS android_pairing_codes(code_hash TEXT PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires_at INTEGER NOT NULL,created_at INTEGER NOT NULL,consumed_at INTEGER);
CREATE TABLE IF NOT EXISTS android_devices(device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,timezone TEXT NOT NULL DEFAULT 'UTC',policy_revision INTEGER NOT NULL DEFAULT 0,policy_apps_json TEXT NOT NULL DEFAULT '{}',foreground_kill_enabled INTEGER NOT NULL DEFAULT 1,apps_json TEXT NOT NULL DEFAULT '[]',capabilities_json TEXT NOT NULL DEFAULT '{}',inventory_updated_at INTEGER,usage_permission INTEGER,usage_last_synced_at INTEGER);
CREATE TABLE IF NOT EXISTS android_runtime(device_id INTEGER PRIMARY KEY REFERENCES android_devices(device_id) ON DELETE CASCADE,observation_id TEXT NOT NULL,state TEXT NOT NULL,package_name TEXT NOT NULL,observed_at INTEGER NOT NULL,can_force_stop INTEGER NOT NULL,recent_json TEXT NOT NULL,received_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS android_files(id INTEGER PRIMARY KEY,device_id INTEGER NOT NULL REFERENCES android_devices(device_id) ON DELETE CASCADE,filename TEXT NOT NULL,mime TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,blob_token TEXT NOT NULL REFERENCES blobs(token),task_id INTEGER REFERENCES tasks(id),created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_android_files_device ON android_files(device_id,id);
CREATE TABLE IF NOT EXISTS android_usage(device_id INTEGER NOT NULL REFERENCES android_devices(device_id) ON DELETE CASCADE,package_name TEXT NOT NULL,bucket_start INTEGER NOT NULL,duration_ms INTEGER NOT NULL,PRIMARY KEY(device_id,package_name,bucket_start));
CREATE INDEX IF NOT EXISTS idx_android_usage_window ON android_usage(device_id,bucket_start);
CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),name TEXT NOT NULL,archive_path TEXT NOT NULL,sha256 TEXT NOT NULL,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS llm_providers(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,base_uri TEXT NOT NULL,api_key TEXT NOT NULL DEFAULT '',official_url TEXT NOT NULL DEFAULT '',keywords TEXT NOT NULL DEFAULT '',api_style TEXT NOT NULL DEFAULT 'openai',models_path TEXT NOT NULL DEFAULT '',headers_json TEXT NOT NULL DEFAULT '{}',models_json TEXT NOT NULL DEFAULT '[]',enabled INTEGER NOT NULL DEFAULT 1,last_check_at INTEGER,last_check_ok INTEGER,last_error TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS app_updates(id INTEGER PRIMARY KEY,version TEXT NOT NULL,status TEXT NOT NULL,archive_path TEXT NOT NULL,stage_path TEXT NOT NULL,backup_path TEXT,manifest_json TEXT NOT NULL,diff_json TEXT NOT NULL,created_by INTEGER REFERENCES users(id),created_at INTEGER NOT NULL,applied_at INTEGER,rolled_back_at INTEGER);
CREATE TABLE IF NOT EXISTS access_scenarios(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,description TEXT NOT NULL DEFAULT '',whitelist_json TEXT NOT NULL DEFAULT '[]',blacklist_json TEXT NOT NULL DEFAULT '[]',enabled INTEGER NOT NULL DEFAULT 1,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS typing_results(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),session_id TEXT NOT NULL,word TEXT NOT NULL,typed TEXT NOT NULL,correct INTEGER NOT NULL,timed_out INTEGER NOT NULL,elapsed_ms INTEGER NOT NULL,word_limit_s INTEGER,practice_limit_s INTEGER,created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_typing_user ON typing_results(user_id,id);
CREATE TABLE IF NOT EXISTS xiaobu_progress_devices(
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 device_id TEXT NOT NULL,
 name TEXT NOT NULL,
 last_synced_at INTEGER NOT NULL,
 PRIMARY KEY(user_id,device_id)
);
CREATE TABLE IF NOT EXISTS xiaobu_progress_records(
 id INTEGER PRIMARY KEY,
 user_id INTEGER NOT NULL,
 device_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 record_key TEXT NOT NULL,
 revision INTEGER NOT NULL,
 data_json TEXT NOT NULL,
 updated_at INTEGER NOT NULL,
 UNIQUE(user_id,device_id,kind,record_key),
 FOREIGN KEY(user_id,device_id) REFERENCES xiaobu_progress_devices(user_id,device_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_xiaobu_progress_device_page ON xiaobu_progress_records(user_id,device_id,id);
CREATE INDEX IF NOT EXISTS idx_tasks_poll ON tasks(device_id,status,id);
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,title TEXT NOT NULL,content TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT 'text',created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id,updated_at);
CREATE TABLE IF NOT EXISTS hermes_conversations(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,title TEXT NOT NULL DEFAULT '新对话',remote_id TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_hermes_conversations_user ON hermes_conversations(user_id,updated_at);
CREATE TABLE IF NOT EXISTS hermes_messages(id INTEGER PRIMARY KEY,conversation_id INTEGER NOT NULL REFERENCES hermes_conversations(id) ON DELETE CASCADE,role TEXT NOT NULL,content TEXT NOT NULL DEFAULT '',remote_id TEXT,attachments_json TEXT NOT NULL DEFAULT '[]',created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_hermes_messages_conversation ON hermes_messages(conversation_id,id);
CREATE TABLE IF NOT EXISTS hermes_uploads(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,conversation_id INTEGER REFERENCES hermes_conversations(id) ON DELETE SET NULL,filename TEXT NOT NULL,stored_path TEXT NOT NULL,mime TEXT NOT NULL DEFAULT 'application/octet-stream',size INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS word_entries(
 id TEXT PRIMARY KEY,
 word TEXT NOT NULL,
 zh TEXT NOT NULL DEFAULT '',
 uk_ipa TEXT NOT NULL DEFAULT '',
 us_ipa TEXT NOT NULL DEFAULT '',
 cefr TEXT NOT NULL DEFAULT '',
 topic TEXT NOT NULL DEFAULT '',
 media INTEGER NOT NULL DEFAULT 0,
 senses_json TEXT NOT NULL DEFAULT '[]',
 examples_json TEXT NOT NULL DEFAULT '[]',
 tip TEXT NOT NULL DEFAULT '',
 collocations_json TEXT NOT NULL DEFAULT '[]',
 family_json TEXT NOT NULL DEFAULT '[]',
 synonyms_json TEXT NOT NULL DEFAULT '[]',
 antonyms_json TEXT NOT NULL DEFAULT '[]',
 raw_json TEXT NOT NULL DEFAULT '{}',
 content_hash TEXT NOT NULL DEFAULT '',
 source_url TEXT NOT NULL DEFAULT '',
 source_updated_at TEXT NOT NULL DEFAULT '',
 updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_word_entries_word ON word_entries(word COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_word_entries_cefr ON word_entries(cefr);
CREATE INDEX IF NOT EXISTS idx_word_entries_topic ON word_entries(topic);
CREATE INDEX IF NOT EXISTS idx_hermes_uploads_user ON hermes_uploads(user_id,created_at);
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
        db.execute("BEGIN IMMEDIATE")
        android_cols={r[1] for r in db.execute("PRAGMA table_info(android_devices)")}
        if "foreground_kill_enabled" not in android_cols:
            db.execute("ALTER TABLE android_devices ADD COLUMN foreground_kill_enabled INTEGER NOT NULL DEFAULT 1")
        occurrence_cols={r[1] for r in db.execute("PRAGMA table_info(android_schedule_occurrences)")}
        if "timezone" not in occurrence_cols:
            # The first agenda schema did not retain provenance. Use the best
            # available device zone for those rows and invalidate old caches.
            db.execute("ALTER TABLE android_schedule_occurrences ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")
            db.execute("UPDATE android_schedule_occurrences SET timezone=COALESCE((SELECT timezone FROM android_devices WHERE device_id=android_schedule_occurrences.device_id),'UTC'),version=version+1")
            db.execute("UPDATE android_schedule_state SET revision=revision+1 WHERE device_id IN (SELECT DISTINCT device_id FROM android_schedule_occurrences)")
        if "manual_override" not in occurrence_cols:
            db.execute("ALTER TABLE android_schedule_occurrences ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0")
        cols={r[1] for r in db.execute("PRAGMA table_info(notes)").fetchall()}
        if "kind" not in cols: db.execute("ALTER TABLE notes ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'")
        db.execute("INSERT OR IGNORE INTO users(username,password_hash,role,active,created_at) VALUES(?,?,?,?,?)", (app.config["ADMIN_USERNAME"],generate_password_hash(app.config["ADMIN_PASSWORD"]),"admin",1,int(time.time())))
        if app.config.get("SYNC_ADMIN_PASSWORD"):
            db.execute("UPDATE users SET password_hash=?, role='admin', active=1 WHERE username=?", (generate_password_hash(app.config["ADMIN_PASSWORD"]), app.config["ADMIN_USERNAME"]))
        db.commit()
    @app.teardown_appcontext
    def close(_exc):
        db = g.pop("db", None)
        if db is not None: db.close()
