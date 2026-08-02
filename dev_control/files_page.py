from flask import Blueprint, render_template
bp=Blueprint("files_page",__name__)
@bp.get("/files/")
def page():return render_template("files.html")
