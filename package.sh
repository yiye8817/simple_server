#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

bump_patch() {
    local current="${1//[[:space:]]/}"
    [[ "$current" =~ ^([0-9]+(\.[0-9]+)*) ]] || { echo "VERSION 格式无效：$1" >&2; exit 1; }
    local prefix="${BASH_REMATCH[1]}"
    local head="${prefix%.*}"
    local last="${prefix##*.}"
    if [[ "$head" == "$prefix" ]]; then
        echo "$((10#$last + 1))"
    else
        echo "${head}.$((10#$last + 1))"
    fi
}

CURRENT="$(<VERSION)"
CURRENT="${CURRENT//[[:space:]]/}"
if [[ -n "${VERSION:-}" ]]; then
    echo "使用指定版本：$VERSION"
else
    VERSION="$(bump_patch "$CURRENT")"
    echo "版本已更新：$CURRENT -> $VERSION"
fi
printf '%s\n' "$VERSION" > VERSION

OUTPUT="${1:-$SCRIPT_DIR/dist/simple-server-$VERSION.tar.gz}"
mkdir -p "$(dirname "$OUTPUT")"

echo "正在打包到：$OUTPUT"
tar -czf "$OUTPUT" \
    --exclude='./.git' \
    --exclude='./.venv' \
    --exclude='./.test-tmp' \
    --exclude='./.superpowers' \
    --exclude='./.env' \
    --exclude='./.env.*' \
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
    --exclude='*.tar.gz' \
    --exclude='*.db-wal' \
    --exclude='*.db-shm' \
    .

echo "打包完成：$(du -h "$OUTPUT" | awk '{print $1}')  版本 $VERSION"
echo "部署后直接执行：./run.sh"
