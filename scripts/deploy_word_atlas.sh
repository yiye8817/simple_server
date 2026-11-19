#!/usr/bin/env bash
set -euo pipefail

DEPLOY_URL="${DEPLOY_URL:-http://8.138.86.215:8080}"
ARCHIVE="${1:-/tmp/simple-server-word-atlas.tar.gz}"
if [[ "$DEPLOY_URL" == http://* && "${ALLOW_INSECURE_DEPLOY:-0}" != "1" ]]; then
  echo "部署地址使用 HTTP；若已确认网络受信，请设置 ALLOW_INSECURE_DEPLOY=1" >&2
  exit 2
fi
: "${DEPLOY_TOKEN:?请设置已授权的 DEPLOY_TOKEN 环境变量}"
[ -f "$ARCHIVE" ] || { echo "找不到归档：$ARCHIVE" >&2; exit 2; }

upload_json="$(curl --fail --silent --show-error \
  -H "Authorization: Bearer ${DEPLOY_TOKEN}" \
  -F "file=@${ARCHIVE};type=application/gzip" \
  "${DEPLOY_URL}/api/updates/upload")"
update_id="$(printf '%s' "$upload_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

curl --fail --silent --show-error \
  -H "Authorization: Bearer ${DEPLOY_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"restart":true}' \
  "${DEPLOY_URL}/api/updates/${update_id}/apply"
printf '已安排更新 %s，等待服务重启后执行：\n' "$update_id"
printf 'curl -H "Authorization: Bearer $DEPLOY_TOKEN" -X POST %s/api/words/sync\n' "$DEPLOY_URL"
