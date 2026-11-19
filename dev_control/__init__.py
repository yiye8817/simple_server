import os
from pathlib import Path
from flask import Flask, jsonify, render_template

from .db import init_db


def create_app(config=None):
    app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent.parent / "templates"))
    root = Path(__file__).resolve().parent.parent
    app.config.from_mapping(
        DATA_DIR=os.getenv("DATA_DIR", str(root / "data")),
        SERVER_FILES_ROOT=os.getenv("SERVER_FILES_ROOT", str(root / "data" / "files")),
        DRAWIO_ROOT=str(root / "vendor" / "drawio" / "webapp"),
        DEVICE_AGENT_OPEN_ENROLL=os.getenv("DEVICE_AGENT_OPEN_ENROLL", "1") == "1",
        DEVICE_AGENT_ENROLL_TOKEN=os.getenv("DEVICE_AGENT_ENROLL_TOKEN", ""),
        ADMIN_USERNAME=os.getenv("ADMIN_USERNAME", "admin"),
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD", "admin-change-me"),
        SYNC_ADMIN_PASSWORD=os.getenv("SYNC_ADMIN_PASSWORD", "0") == "1",
        ENABLE_LEGACY_TOOLS=os.getenv("ENABLE_LEGACY_TOOLS", "1") == "1",
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_CONTENT_LENGTH", str(512 * 1024 * 1024))),
        HERMES_BASE_URL=os.getenv("HERMES_BASE_URL", ""),
        HERMES_API_KEY=os.getenv("HERMES_API_KEY", ""),
        HERMES_LOCAL_ENABLED=os.getenv("HERMES_LOCAL_ENABLED", "1") == "1",
        HERMES_LOCAL_COMMAND=os.getenv("HERMES_LOCAL_COMMAND", ""),
        HERMES_MODEL=os.getenv("HERMES_MODEL", "hermes"),
        HERMES_CHAT_PATH=os.getenv("HERMES_CHAT_PATH", "/v1/chat/completions"),
        HERMES_SKILLS_PATH=os.getenv("HERMES_SKILLS_PATH", "/skills"),
        HERMES_FUNCTIONS_PATH=os.getenv("HERMES_FUNCTIONS_PATH", "/functions"),
        WORDS_SOURCE_BASE=os.getenv("WORDS_SOURCE_BASE", "https://0xlei.tech/words/atlas/"),
        WORDS_SOURCE_ALLOWLIST=os.getenv("WORDS_SOURCE_ALLOWLIST", "0xlei.tech"),
        WORDS_SYNC_TIMEOUT=int(os.getenv("WORDS_SYNC_TIMEOUT", "180")),
        WORDS_MAX_IMPORT_BYTES=int(os.getenv("WORDS_MAX_IMPORT_BYTES", str(32 * 1024 * 1024))),
        WORDS_ASSET_DIR=os.getenv("WORDS_ASSET_DIR", ""),
        WORDS_ASSET_SYNC_TIMEOUT=int(os.getenv("WORDS_ASSET_SYNC_TIMEOUT", "30")),
        WORDS_ASSET_MAX_BYTES=int(os.getenv("WORDS_ASSET_MAX_BYTES", str(16 * 1024 * 1024))),
        WORDS_ASSET_WORKERS=int(os.getenv("WORDS_ASSET_WORKERS", "12")),
    )
    if config:
        app.config.update(config)
    if not app.config["WORDS_ASSET_DIR"]:
        app.config["WORDS_ASSET_DIR"] = str(Path(app.config["DATA_DIR"]) / "word_assets")
    for p in (app.config["DATA_DIR"], app.config["SERVER_FILES_ROOT"],
              str(Path(app.config["DATA_DIR"]) / "blobs"), str(Path(app.config["DATA_DIR"]) / "services"),
              app.config["WORDS_ASSET_DIR"]):
        Path(p).mkdir(parents=True, exist_ok=True)
    init_db(app)
    from .auth import bp as auth_bp
    from .agents import bp as agents_bp
    from .android import bp as android_bp
    from .agenda import bp as agenda_bp
    from .files import bp as files_bp
    from .drawio import bp as drawio_bp
    from .downloads import bp as downloads_bp
    from .game import bp as game_bp
    from .llm import bp as llm_bp
    from .policies import bp as policies_bp
    from .bulk_files import bp as bulk_files_bp
    from .files_page import bp as files_page_bp
    from .users import bp as users_bp
    from .updates import bp as updates_bp
    from .notes import bp as notes_bp
    from .apis import bp as apis_bp
    from .learning import bp as learning_bp
    from .words import bp as words_bp
    from .hermes import bp as hermes_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(agents_bp)
    app.register_blueprint(android_bp)
    app.register_blueprint(agenda_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(drawio_bp)
    app.register_blueprint(downloads_bp)
    app.register_blueprint(game_bp)
    app.register_blueprint(llm_bp)
    app.register_blueprint(policies_bp)
    app.register_blueprint(bulk_files_bp)
    app.register_blueprint(files_page_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(updates_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(apis_bp)
    app.register_blueprint(learning_bp)
    app.register_blueprint(words_bp)
    app.register_blueprint(hermes_bp)
    if app.config["ENABLE_LEGACY_TOOLS"]:
        from .legacy import mount_legacy
        mount_legacy(app)

    @app.get("/")
    def index():
        return render_template("control.html")

    @app.get("/api/health")
    def health():
        return jsonify(status="ok")
    return app
