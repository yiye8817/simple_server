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
    )
    if config:
        app.config.update(config)
    for p in (app.config["DATA_DIR"], app.config["SERVER_FILES_ROOT"],
              str(Path(app.config["DATA_DIR"]) / "blobs"), str(Path(app.config["DATA_DIR"]) / "services")):
        Path(p).mkdir(parents=True, exist_ok=True)
    init_db(app)
    from .auth import bp as auth_bp
    from .agents import bp as agents_bp
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
    app.register_blueprint(auth_bp)
    app.register_blueprint(agents_bp)
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

