#!/usr/bin/env bash
# device_agent 一键安装脚本: 把当前目录的 agent 装到 /opt/fm-agent,
# 注册一个开机自启的 systemd 服务, 并验证服务确实跑起来了。
#
# 用法:
#   sudo bash install.sh
#       # 交互式询问 FM_SERVER_URL, 其它走默认值
#   sudo FM_SERVER_URL=http://192.168.1.10:5000 bash install.sh
#   sudo bash install.sh \
#       --server-url http://192.168.1.10:5000 \
#       --enroll-token <可选, 公网部署用> \
#       --install-dir /opt/fm-agent \
#       --service-name fm-agent \
#       --run-user root
#
# 全部可用 CLI 参数 / 环境变量:
#   --server-url    / FM_SERVER_URL       (必填) file_manager 服务器 base URL
#   --enroll-token  / FM_ENROLL_TOKEN     (可选) 服务端 DEVICE_AGENT_ENROLL_TOKEN
#   --install-dir   / INSTALL_DIR         默认 /opt/fm-agent
#   --service-name  / SERVICE_NAME        默认 fm-agent
#   --run-user      / RUN_USER            默认 root (用 root 是为了能读 /proc/<frpc>/cwd)
#   --python-bin    / PYTHON_BIN          默认探测 python3
#   --no-pip                              不自动 pip install requirements.txt
#   --uninstall                           反向操作: stop + disable + 删除 unit (不删数据)

set -euo pipefail

# ----------------------------------------------------------------------
# 0. 必须 root
# ----------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    echo "[install] 需要 root 权限, 请用: sudo bash $0 $*" >&2
    exit 1
fi

# ----------------------------------------------------------------------
# 1. 默认值 + CLI 参数解析
# ----------------------------------------------------------------------
FM_SERVER_URL="${FM_SERVER_URL:-}"
FM_ENROLL_TOKEN="${FM_ENROLL_TOKEN:-}"
INSTALL_DIR="${INSTALL_DIR:-/opt/fm-agent}"
SERVICE_NAME="${SERVICE_NAME:-fm-agent}"
RUN_USER="${RUN_USER:-root}"
PYTHON_BIN="${PYTHON_BIN:-}"
DO_PIP=1
DO_UNINSTALL=0

usage() {
    sed -n '2,28p' "$0"
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --server-url)    FM_SERVER_URL="${2:-}"; shift 2;;
        --enroll-token)  FM_ENROLL_TOKEN="${2:-}"; shift 2;;
        --install-dir)   INSTALL_DIR="${2:-}"; shift 2;;
        --service-name)  SERVICE_NAME="${2:-}"; shift 2;;
        --run-user)      RUN_USER="${2:-}"; shift 2;;
        --python-bin)    PYTHON_BIN="${2:-}"; shift 2;;
        --no-pip)        DO_PIP=0; shift;;
        --uninstall)     DO_UNINSTALL=1; shift;;
        -h|--help)       usage 0;;
        *) echo "[install] 未知参数: $1" >&2; usage 1;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----------------------------------------------------------------------
# 卸载分支
# ----------------------------------------------------------------------
if [ "$DO_UNINSTALL" -eq 1 ]; then
    echo "[install] uninstall: 停止并清理 ${SERVICE_NAME}"
    systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    echo "[install] 已移除 systemd unit; 安装目录 ${INSTALL_DIR} 保留 (含 device.uid/token, 防止误删数据)。"
    echo "[install] 如果需要彻底清空, 手工: rm -rf ${INSTALL_DIR}"
    exit 0
fi

# ----------------------------------------------------------------------
# 2. systemd 可用性
# ----------------------------------------------------------------------
if ! command -v systemctl >/dev/null 2>&1; then
    echo "[install] 当前系统没有 systemctl, 无法自动创建开机启动服务。" >&2
    echo "[install] 请自行参考 README 的 systemd 单元模板手工部署。" >&2
    exit 1
fi
if [ ! -d /run/systemd/system ]; then
    echo "[install] 当前没有以 systemd 作为 init (没有 /run/systemd/system)。" >&2
    exit 1
fi

# ----------------------------------------------------------------------
# 3. 必填: FM_SERVER_URL (允许交互式输入)
# ----------------------------------------------------------------------
if [ -z "$FM_SERVER_URL" ]; then
    if [ -t 0 ]; then
        read -rp "请输入 FM_SERVER_URL (例: http://192.168.1.10:5000): " FM_SERVER_URL
    fi
fi
if [ -z "$FM_SERVER_URL" ]; then
    echo "[install] FM_SERVER_URL 必填, 已中止。" >&2
    echo "[install] 用法: sudo FM_SERVER_URL=http://host:5000 bash install.sh" >&2
    exit 1
fi
# 形如 http(s)://host[:port] 的最简校验
if ! [[ "$FM_SERVER_URL" =~ ^https?:// ]]; then
    echo "[install] FM_SERVER_URL 必须以 http:// 或 https:// 开头, 当前: $FM_SERVER_URL" >&2
    exit 1
fi
FM_SERVER_URL="${FM_SERVER_URL%/}"  # 去掉末尾斜杠

# ----------------------------------------------------------------------
# 4. python3 探测
# ----------------------------------------------------------------------
if [ -z "$PYTHON_BIN" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python)"
    fi
fi
if [ -z "$PYTHON_BIN" ] || ! "$PYTHON_BIN" --version >/dev/null 2>&1; then
    echo "[install] 找不到 python3, 请先 apt install python3 (或同等) 再重试。" >&2
    exit 1
fi
PY_VER="$("$PYTHON_BIN" -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')"
echo "[install] 使用 python: $PYTHON_BIN ($PY_VER)"

# ----------------------------------------------------------------------
# 5. 用户校验
# ----------------------------------------------------------------------
if ! id "$RUN_USER" >/dev/null 2>&1; then
    echo "[install] 用户 $RUN_USER 不存在, 请先 useradd, 或改用 --run-user root" >&2
    exit 1
fi

# ----------------------------------------------------------------------
# 6. 把当前目录拷贝到 INSTALL_DIR (rsync 优先, 退而求其次用 cp)
# ----------------------------------------------------------------------
echo "[install] 安装目录: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
if command -v rsync >/dev/null 2>&1; then
    rsync -a \
        --exclude='__pycache__' \
        --exclude='.deps_installed' \
        --exclude='agent.log' \
        --exclude='venv' \
        "$SCRIPT_DIR"/ "$INSTALL_DIR"/
else
    # cp 兜底: 不能精细排除, 之后手动清一下
    cp -a "$SCRIPT_DIR"/. "$INSTALL_DIR"/
    rm -rf "$INSTALL_DIR/__pycache__" "$INSTALL_DIR/agent.log" "$INSTALL_DIR/.deps_installed" || true
fi
chmod +x "$INSTALL_DIR/run.sh" "$INSTALL_DIR/agent.py" 2>/dev/null || true
chown -R "$RUN_USER":"$(id -gn "$RUN_USER")" "$INSTALL_DIR"

# ----------------------------------------------------------------------
# 7. 可选: 安装 python 依赖 (pip)
# ----------------------------------------------------------------------
if [ "$DO_PIP" -eq 1 ] && [ -f "$INSTALL_DIR/requirements.txt" ]; then
    echo "[install] 安装 python 依赖 (失败不影响启动)..."
    if "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
        "$PYTHON_BIN" -m pip install -q -r "$INSTALL_DIR/requirements.txt" || \
            "$PYTHON_BIN" -m pip install -q --break-system-packages -r "$INSTALL_DIR/requirements.txt" || true
    else
        echo "[install] 当前 python 没装 pip, 跳过依赖安装 (agent 仍可工作, 只是没有 psutil/requests)"
    fi
    touch "$INSTALL_DIR/.deps_installed"
    chown "$RUN_USER":"$(id -gn "$RUN_USER")" "$INSTALL_DIR/.deps_installed" 2>/dev/null || true
fi

# ----------------------------------------------------------------------
# 8. 生成 systemd unit
# ----------------------------------------------------------------------
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
echo "[install] 写入 systemd unit: $UNIT_FILE"

ENROLL_LINE=""
if [ -n "$FM_ENROLL_TOKEN" ]; then
    ENROLL_LINE="Environment=FM_ENROLL_TOKEN=${FM_ENROLL_TOKEN}"
fi

cat > "$UNIT_FILE" <<UNIT_EOF
[Unit]
Description=file_manager device agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHON_BIN=${PYTHON_BIN}
Environment=FM_SERVER_URL=${FM_SERVER_URL}
${ENROLL_LINE}
ExecStart=/usr/bin/env bash ${INSTALL_DIR}/run.sh
Restart=on-failure
RestartSec=5s
KillMode=mixed
TimeoutStopSec=15s

[Install]
WantedBy=multi-user.target
UNIT_EOF

chmod 0644 "$UNIT_FILE"

# ----------------------------------------------------------------------
# 9. enable + (re)start
# ----------------------------------------------------------------------
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service" >/dev/null
echo "[install] 启动服务: ${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}.service"

# ----------------------------------------------------------------------
# 10. 验证: 等几秒, 然后看 systemctl is-active + 抓 agent.log 末尾
# ----------------------------------------------------------------------
echo "[install] 等待 6s 让 agent 完成首次 enroll / heartbeat ..."
sleep 6

if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    ACTIVE_STATE="$(systemctl show -p ActiveState --value "${SERVICE_NAME}.service")"
    MAIN_PID="$(systemctl show -p MainPID --value "${SERVICE_NAME}.service")"
    echo "[install] OK: ${SERVICE_NAME} 已运行 (ActiveState=${ACTIVE_STATE}, PID=${MAIN_PID})"
    echo
    echo "--- systemctl status ${SERVICE_NAME} (节选) ---"
    systemctl status "${SERVICE_NAME}.service" --no-pager -l | head -15 || true
    echo
    echo "--- agent.log 末尾 30 行 ---"
    if [ -f "${INSTALL_DIR}/agent.log" ]; then
        tail -n 30 "${INSTALL_DIR}/agent.log" || true
    else
        echo "(agent.log 还没生成, 这是正常的 — 看上面 systemctl status 即可)"
    fi
    echo
    echo "[install] 完成。开机启动已启用。"
    echo "[install] 常用命令:"
    echo "  systemctl status  ${SERVICE_NAME}"
    echo "  systemctl restart ${SERVICE_NAME}"
    echo "  journalctl -u ${SERVICE_NAME} -f"
    echo "  tail -f ${INSTALL_DIR}/agent.log"
    exit 0
else
    echo "[install] FAIL: ${SERVICE_NAME} 没起来 — 下面是诊断信息:" >&2
    echo
    echo "--- systemctl status ${SERVICE_NAME} ---" >&2
    systemctl status "${SERVICE_NAME}.service" --no-pager -l | head -30 >&2 || true
    echo >&2
    echo "--- journalctl -u ${SERVICE_NAME} (末尾 50 行) ---" >&2
    journalctl -u "${SERVICE_NAME}.service" --no-pager -n 50 >&2 || true
    if [ -f "${INSTALL_DIR}/agent.log" ]; then
        echo >&2
        echo "--- agent.log 末尾 50 行 ---" >&2
        tail -n 50 "${INSTALL_DIR}/agent.log" >&2 || true
    fi
    exit 2
fi
