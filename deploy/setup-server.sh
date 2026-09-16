#!/usr/bin/env bash
# =============================================================
# Linux 巡检中央服务器 — 一键初始化脚本
# 用法（root 执行）:
#   1. 把整个项目上传到服务器 /opt/linux-inspect-agent
#   2. bash /opt/linux-inspect-agent/deploy/setup-server.sh
# 脚本做的事:
#   创建低权限用户 → 建 venv 装依赖 → 生成随机密钥(env 文件)
#   → 准备数据库目录 → 安装并启动 systemd 服务
# =============================================================
set -euo pipefail

APP_DIR="/opt/linux-inspect-agent"
ENV_DIR="/etc/inspect-server"
ENV_FILE="${ENV_DIR}/inspect.env"
DB_DIR="/var/lib/inspect-server"
SERVICE_USER="inspect"
PORT="${INSPECT_PORT:-8000}"

log()  { echo -e "\033[32m[setup]\033[0m $*"; }
warn() { echo -e "\033[33m[warn ]\033[0m $*"; }
die()  { echo -e "\033[31m[error]\033[0m $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 执行本脚本"
[ -f "${APP_DIR}/server/server.py" ] || die "未找到 ${APP_DIR}/server/server.py，请先把项目放到 ${APP_DIR}"
command -v python3 >/dev/null || die "缺少 python3，请先安装（Debian/Ubuntu: apt install python3 python3-venv）"

# ── 1. 创建专用运行用户（无登录 shell）──
if ! id "${SERVICE_USER}" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
    log "已创建系统用户 ${SERVICE_USER}"
fi

# ── 2. Python 虚拟环境 + 依赖 ──
if [ ! -d "${APP_DIR}/venv" ]; then
    python3 -m venv "${APP_DIR}/venv"
    log "已创建 venv"
fi
"${APP_DIR}/venv/bin/pip" install --upgrade pip -q
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q
log "依赖安装完成"

# ── 3. 生成密钥环境变量文件（已存在则跳过，避免覆盖在用密钥）──
mkdir -p "${ENV_DIR}"
if [ -f "${ENV_FILE}" ]; then
    warn "${ENV_FILE} 已存在，跳过密钥生成（如需重置请手动删除后重跑）"
else
    REPORT_TOKEN="$(openssl rand -hex 32)"
    DASH_PASS="$(openssl rand -hex 16)"
    cat > "${ENV_FILE}" <<EOF
# 巡检服务器密钥配置 — 由 setup-server.sh 自动生成
# 监听地址：只监听本机，对外由 Nginx 反代（HTTPS）
INSPECT_HOST=127.0.0.1
INSPECT_PORT=${PORT}
INSPECT_DB=${DB_DIR}/inspect.db

# Agent 上报 Token（Agent 端必须配置成同一个值！）
INSPECT_TOKEN=${REPORT_TOKEN}

# 看板/API 的浏览器 Basic Auth 账号
INSPECT_DASHBOARD_USER=admin
INSPECT_DASHBOARD_PASS=${DASH_PASS}
EOF
    chown "${SERVICE_USER}:${SERVICE_USER}" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    log "已生成密钥文件 ${ENV_FILE}"
    echo ""
    echo "  ======================================================"
    echo "  请妥善保存以下凭据（之后也可在 ${ENV_FILE} 查看）："
    echo ""
    echo "  Agent 上报 Token : ${REPORT_TOKEN}"
    echo "  看板登录账号     : admin"
    echo "  看板登录密码     : ${DASH_PASS}"
    echo "  ======================================================"
    echo ""
fi

# ── 4. 数据库目录 ──
mkdir -p "${DB_DIR}"
chown "${SERVICE_USER}:${SERVICE_USER}" "${DB_DIR}"

# ── 5. 文件归属（代码目录只读即可，运行用户能读）──
chown -R root:root "${APP_DIR}"
chmod -R a+rX "${APP_DIR}"

# ── 6. 安装 systemd 服务 ──
cp "${APP_DIR}/deploy/systemd/inspect-server.service" /etc/systemd/system/inspect-server.service
systemctl daemon-reload
systemctl enable --now inspect-server
sleep 2
if systemctl is-active --quiet inspect-server; then
    log "服务已启动: inspect-server (127.0.0.1:${PORT})"
else
    warn "服务启动失败，请查看日志: journalctl -u inspect-server -n 50"
    exit 1
fi

# ── 7. 本机连通性自测 ──
if command -v curl >/dev/null; then
    CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/" || true)
    # 配置了 Basic Auth 时返回 401 属正常（说明鉴权生效）
    case "${CODE}" in
        200|401) log "本机自测通过: HTTP ${CODE}" ;;
        *) warn "本机自测异常: HTTP ${CODE}，请检查 journalctl -u inspect-server" ;;
    esac
fi

cat <<EOF

下一步：
  1) 配置 Nginx 反代 + HTTPS（参考 deploy/DEPLOY.md 第 4 步）
  2) 防火墙只放行 22/80/443，不要对外开放 ${PORT}
  3) 在每台被巡检机器上执行 deploy/setup-agent.sh

EOF
