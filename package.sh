#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
VERSION="${VERSION:-$(<VERSION)}"
OUTPUT="${1:-$SCRIPT_DIR/dist/simple-server-$VERSION.tar.gz}"
mkdir -p "$(dirname "$OUTPUT")"

echo "正在打包到：$OUTPUT"
tar -czf "$OUTPUT" \
    --exclude='./.git' \
    --exclude='./.venv' \
    --exclude='./data' \
    --exclude='./dist' \
    --exclude='./vendor/drawio/webapp/.codegraph' \
    --exclude='./vendor/drawio/webapp/WEB-INF/*_secret' \
    --exclude='./vendor/drawio/webapp/WEB-INF/*_api_key' \
    --exclude='./vendor/drawio/webapp/WEB-INF/pusher_properties' \
    --exclude='./__pycache__' \
    --exclude='*/__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.log' \
    --exclude='*.db-wal' \
    --exclude='*.db-shm' \
    .

echo "打包完成：$(du -h "$OUTPUT" | awk '{print $1}')"
echo "部署后直接执行：./run.sh"
