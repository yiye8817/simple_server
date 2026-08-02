import io, tarfile
from pathlib import Path
from flask import Blueprint, jsonify, request, send_file

bp=Blueprint("downloads",__name__)

@bp.get("/downloads/device-agent.tar.gz")
def device_agent_package():
    root=Path(__file__).resolve().parent.parent/"clients"/"device_agent"
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode="w:gz") as tar:
        for p in root.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc",".log") and p.name not in ("device.token","device.uid"):
                tar.add(p,arcname=str(Path("device_agent")/p.relative_to(root)))
    buf.seek(0)
    return send_file(buf,mimetype="application/gzip",as_attachment=True,download_name="device-agent.tar.gz")

@bp.get("/api/agent/install-info")
def install_info():
    base=request.url_root.rstrip("/")
    command=(f"mkdir -p /opt/dev-control-agent && cd /opt/dev-control-agent && "
             f"curl -fsSL {base}/downloads/device-agent.tar.gz | tar -xz --strip-components=1 && "
             f"chmod +x run.sh && ./run.sh --server-url {base}")
    return jsonify(download_url=base+"/downloads/device-agent.tar.gz",install_command=command)
