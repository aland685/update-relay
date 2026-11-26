#!/usr/bin/env bash
set -euo pipefail

# Update Relay Debian 一键部署脚本（systemd + Python venv）
# 用法：sudo bash deploy/install-debian.sh

INSTALL_DIR="${INSTALL_DIR:-/opt/update-relay}"
SERVICE_NAME="update-relay"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 或 sudo 运行此脚本。"
  exit 1
fi

echo "[1/5] 安装系统依赖 (python3, python3-venv, openssl)..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip openssl >/dev/null

echo "[2/5] 同步项目文件到 ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}/data/cache"
cp -r app requirements.txt .env.example "${INSTALL_DIR}/"

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  RAND_PASS="$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 16)"
  RAND_SECRET="$(openssl rand -hex 32)"
  sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${RAND_PASS}|" "${INSTALL_DIR}/.env"
  sed -i "s|^SESSION_SECRET=.*|SESSION_SECRET=${RAND_SECRET}|" "${INSTALL_DIR}/.env"
  echo "已生成初始 .env 配置文件："
  echo "  管理员账号: admin"
  echo "  管理员密码: ${RAND_PASS}"
  echo "  （请登录后及时修改或编辑 ${INSTALL_DIR}/.env）"
fi

echo "[3/5] 创建 Python 虚拟环境并安装依赖..."
python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip -q
"${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"

echo "[4/5] 创建 systemd 服务 (${SERVICE_NAME})..."
cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Update Relay - GitHub Release Proxy & Cache
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 18080 --proxy-headers --forwarded-allow-ips=*
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "[5/5] 启动并设置开机自启..."
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
systemctl status "${SERVICE_NAME}" --no-pager -l | head -n 20

echo ""
echo "部署完成！服务监听在 0.0.0.0:18080（NPM 可反代至 172.17.0.1:18080）。"
