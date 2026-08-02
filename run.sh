#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-yiye.123}"
export SYNC_ADMIN_PASSWORD="${SYNC_ADMIN_PASSWORD:-1}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-5000}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "错误：找不到 Python 3，请先安装 Python 3。" >&2
    exit 1
fi

if [[ ! -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    echo "首次运行：创建 Python 虚拟环境..."
    python3 -m venv "$SCRIPT_DIR/.venv" || {
        echo "创建虚拟环境失败。Debian/Ubuntu 请先安装 python3-venv。" >&2
        exit 1
    }
fi

PYTHON="$SCRIPT_DIR/.venv/bin/python"
PIP="$SCRIPT_DIR/.venv/bin/pip"
REQ_HASH="$($PYTHON -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("requirements.txt").read_bytes()).hexdigest())')"
MARKER="$SCRIPT_DIR/.venv/.requirements-sha256"

if [[ "${SKIP_INSTALL:-0}" != "1" ]] && { [[ ! -f "$MARKER" ]] || [[ "$(<"$MARKER")" != "$REQ_HASH" ]]; }; then
    echo "安装项目依赖..."
    "$PIP" install --disable-pip-version-check -r "$SCRIPT_DIR/requirements.txt"
    printf '%s\n' "$REQ_HASH" > "$MARKER"
fi

if ! "$PYTHON" -c 'import flask' >/dev/null 2>&1; then
    echo "错误：依赖安装不完整。可删除 .venv 后重新运行。" >&2
    exit 1
fi

echo "Dev Control Server 启动中"
echo "  地址: http://${HOST}:${PORT}"
echo "  drawio: http://${HOST}:${PORT}/drawio/"
echo "  管理员: ${ADMIN_USERNAME}"

exec "$PYTHON" "$SCRIPT_DIR/run.py"

