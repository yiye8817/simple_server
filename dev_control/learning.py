from pathlib import Path

from flask import Blueprint, current_app, send_from_directory


bp = Blueprint("learning", __name__)
ROOT = Path(__file__).resolve().parent.parent


def _send(site, filename="index.html"):
    return send_from_directory(ROOT / site, filename)


@bp.get("/learn_en/")
def learn_en():
    return _send("learn_en")


@bp.get("/learn_en/<path:filename>")
def learn_en_file(filename):
    return _send("learn_en", filename)


@bp.get("/shuxue/")
def shuxue():
    return _send("shuxue")


@bp.get("/shuxue/<path:filename>")
def shuxue_file(filename):
    return _send("shuxue", filename)


@bp.get("/words/")
def words():
    return _send("words")


@bp.get("/words/assets/<path:filename>")
def words_asset(filename):
    return send_from_directory(current_app.config["WORDS_ASSET_DIR"], filename, max_age=86400)


@bp.get("/words/<path:filename>")
def words_file(filename):
    return _send("words", filename)
