#!/usr/bin/env bash
# =============================================================
# Linux 巡检 Agent — 一键安装脚本（在每台被巡检的机器上执行）
# 用法（root 执行）:
#   bash setup-agent.sh <中央服务器地址> <上报Token>
# 示例:
#   bash setup-agent.sh https://inspect.example.com abc123...
# =============================================================
set -euo pipefail

APP_DIR="/opt/inspect-agent"
ENV_DIR="/etc/inspect-agent"
ENV_FILE="${ENV_DIR}/agent.env"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { echo -e "\033[32m[setup]\033[0m $*"; }
die()  { echo -e "\033[31m[error]\033[0m $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 执行本脚本"
[ $# -ge 2 ] || die "用法: bash setup-agent.sh <服务器地址> <上报Token>\n例如: bash setup-agent.sh https://inspect.example.com <token>"

SERVER_URL="${1%/}"
TOKEN="$2"
INTERVAL="${3:-300}"

command -v python3 >/dev/null || die "缺少 python3，请先安装"

# ── 1. 安装 Agent 依赖（只需 psutil + pyyaml）──
if command -v apt-get >/dev/null; then
    apt-get install -y -q python3-pip >/dev/null
    pip3 install -q "psutil>=5.9,<8" "pyyaml>=6.0,<7" --break-system-packages 2>/dev/null \
        || pip3 install -q "psutil>=5.9,<8" "pyyaml>=6.0,<7"
elif command -v dnf >/dev/null; then
    dnf install -y -q python3-pip >/dev/null
    pip3 install -q "psutil>=5.9,<8" "pyyaml>=6.0,<7"
else
    pip3 install -q "psutil>=5.9,<8" "pyyaml>=6.0,<7" || die "请手动安装 psutil 和 pyyaml"
fi
log "依赖安装完成"

# ── 2. 复制 Agent 代码 ──
mkdir -p "${APP_DIR}/agent"
cp "${SCRIPT_DIR}/../agent/inspect_agent.py" "${APP_DIR}/agent/"
cp "${SCRIPT_DIR}/../agent/config.yaml"      "${APP_DIR}/agent/"
chmod 755 "${APP_DIR}/agent/inspect_agent.py"
log "代码已安装到 ${APP_DIR}/agent"

# ── 3. 生成环境变量文件（密钥不进 config.yaml）──
mkdir -p "${ENV_DIR}"
cat > "${ENV_FILE}" <<EOF
# 巡检 Agent 配置 — 由 setup-agent.sh 生成
INSPECT_SERVER=${SERVER_URL}
INSPECT_TOKEN=${TOKEN}
INSPECT_INTERVAL=${INTERVAL}
# 自定义主机名可取消下行注释
#INSPECT_HOSTNAME=
EOF
chmod 600 "${ENV_FILE}"
log "配置已写入 ${ENV_FILE}"

# ── 4. 安装 systemd 服务 ──
cp "${SCRIPT_DIR}/systemd/inspect-agent.service" /etc/systemd/system/inspect-agent.service
systemctl daemon-reload
systemctl enable --now inspect-agent
log "服务已启动: inspect-agent"

# ── 5. 立即跑一次验证连通性 ──
log "正在执行一次上报测试..."
set +e
env "$(grep -v '^#' "${ENV_FILE}" | xargs)" python3 "${APP_DIR}/agent/inspect_agent.py" --once
RC=$?
set -e
if [ ${RC} -eq 0 ]; then
    log "安装完成，上报测试通过 ✔"
else
    echo ""
    echo "  [!] 上报测试失败，常见原因："
    echo "      - 服务器地址/Token 不对（检查 ${ENV_FILE}）"
    echo "      - 服务器 Nginx/HTTPS 还没配好"
    echo "      - 防火墙没放行 443"
    echo "  服务已设为开机自启并自动重试，修好服务器侧后无需重装。"
    echo "  查看日志: journalctl -u inspect-agent -n 50"
fi
