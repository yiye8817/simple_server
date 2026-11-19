#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
export ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-yiye.123}"
export SYNC_ADMIN_PASSWORD="${SYNC_ADMIN_PASSWORD:-1}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-5000}"

if ! command -v python3 >/dev/null 2>&1; then echo "错误：找不到 Python 3。" >&2;exit 1;fi
if [[ ! -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    echo "首次运行：创建 Python 虚拟环境..."
    python3 -m venv "$SCRIPT_DIR/.venv" || { echo "创建失败，请安装 python3-venv。" >&2;exit 1; }
fi

PYTHON="$SCRIPT_DIR/.venv/bin/python";PIP="$SCRIPT_DIR/.venv/bin/pip"
REQ_HASH="$($PYTHON -c 'import hashlib,pathlib;print(hashlib.sha256(pathlib.Path("requirements.txt").read_bytes()).hexdigest())')";MARKER="$SCRIPT_DIR/.venv/.requirements-sha256"
if [[ "${SKIP_INSTALL:-0}" != "1" ]] && { [[ ! -f "$MARKER" ]] || [[ "$(<"$MARKER")" != "$REQ_HASH" ]]; }; then
    echo "安装项目依赖...";"$PIP" install --disable-pip-version-check -r requirements.txt;printf '%s\n' "$REQ_HASH" > "$MARKER"
fi
"$PYTHON" -c 'import flask' >/dev/null 2>&1 || { echo "错误：依赖安装不完整。" >&2;exit 1; }

echo "Dev Control Server 启动中（版本 $(<VERSION)）"
echo "  地址: http://${HOST}:${PORT}"
if [[ "${HOST}" == "0.0.0.0" ]]; then
    LAN_HOST="${LAN_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
    [[ -n "${LAN_HOST}" ]] && echo "  局域网访问: http://${LAN_HOST}:${PORT}"
fi
echo "  管理员: ${ADMIN_USERNAME}"
set +e
"$PYTHON" "$SCRIPT_DIR/run.py"
STATUS=$?
set -e
if [[ "$STATUS" == "75" ]]; then
    echo "检测到自更新，重新加载启动脚本和依赖..."
    exec bash "$SCRIPT_DIR/run.sh"
fi
exit "$STATUS"
