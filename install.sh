#!/usr/bin/env bash
# ==============================================================================
# StreamVault — Automated Linux Installer & System Registration
# Compatible with Ubuntu 22.04+, Debian 12+, and Linux systems with systemd.
# ==============================================================================
set -euo pipefail

# Visual styles
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${CYAN}${BOLD}"
echo "======================================================================"
echo "    StreamVault — Automated Production Installer & Registration       "
echo "======================================================================"
echo -e "${NC}"

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[ERROR] This installer must be executed as root. Please run with sudo.${NC}"
    exit 1
fi

INSTALL_DIR="/opt/streamvault"
APP_DIR="${INSTALL_DIR}/app"
VENV_DIR="${INSTALL_DIR}/venv"
DATA_DIR="/var/lib/streamvault/data"
LOG_DIR="/var/log/streamvault"
CONFIG_DIR="/etc/streamvault"
RAMDISK_DIR="/dev/shm/streamvault_hls"
SERVICE_USER="streamvault"
REPO_URL="https://github.com/Blackwellcode/streamvault.git"

# 1. System Package Installation
echo -e "${BLUE}[1/8] Installing system dependencies (apt)...${NC}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 \
    python3-venv \
    python3-pip \
    ffmpeg \
    nginx \
    sqlite3 \
    curl \
    git \
    procps \
    ca-certificates

# 2. System Service User Creation
echo -e "${BLUE}[2/8] Setting up system user '${SERVICE_USER}'...${NC}"
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd -r -s /bin/bash -d "${INSTALL_DIR}" "${SERVICE_USER}"
    echo -e "${GREEN}[OK] User '${SERVICE_USER}' created.${NC}"
else
    echo -e "${GREEN}[OK] User '${SERVICE_USER}' exists.${NC}"
fi

# 3. Directory Structure
echo -e "${BLUE}[3/8] Creating directory structure...${NC}"
mkdir -p "${INSTALL_DIR}" "${APP_DIR}" "${DATA_DIR}" "${LOG_DIR}" "${CONFIG_DIR}" "${RAMDISK_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}" "${DATA_DIR}" "${LOG_DIR}" "${RAMDISK_DIR}"
chmod 755 "${INSTALL_DIR}" "${RAMDISK_DIR}"
# Nginx (www-data) must be able to traverse and read the archive when it
# serves X-Accel-Redirect responses. Access is still gated by the internal
# Nginx location and the application's authentication checks.
chmod 755 "${DATA_DIR}"
chmod 750 "${LOG_DIR}"

# 4. Deploying Source Code
echo -e "${BLUE}[4/8] Deploying application codebase...${NC}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/WEBUI/webui.py" ]]; then
    echo -e "${CYAN}[*] Copying from local source tree (${SCRIPT_DIR})...${NC}"
    cp -ru "${SCRIPT_DIR}"/* "${APP_DIR}/"
else
    echo -e "${CYAN}[*] Cloning from GitHub repository (${REPO_URL})...${NC}"
    if [[ -d "${APP_DIR}/.git" ]]; then
        cd "${APP_DIR}" && git pull --ff-only
    else
        git clone "${REPO_URL}" "${APP_DIR}"
    fi
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

# 5. Virtualenv & Dependencies
echo -e "${BLUE}[5/8] Configuring Python virtual environment...${NC}"
if [[ ! -d "${VENV_DIR}" ]]; then
    sudo -u "${SERVICE_USER}" python3 -m venv "${VENV_DIR}"
fi
sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip -q
sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt" -q

# 6. Environment Configuration
echo -e "${BLUE}[6/8] Generating production environment configuration...${NC}"
ENV_FILE="${CONFIG_DIR}/env"
if [[ ! -f "${ENV_FILE}" ]]; then
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    INITIAL_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
    cat > "${ENV_FILE}" <<EOF
# StreamVault Production Environment
VOD_HOST=127.0.0.1
VOD_PORT=8080
# Leave empty to derive from the request, or set the public HTTPS origin.
VOD_PUBLIC_BASE=
VOD_BASE_DIR=${DATA_DIR}
LIVE_HLS_DIR=${RAMDISK_DIR}
VOD_USER=vaultadmin
VOD_PASS=${INITIAL_PASS}
VOD_SECRET_KEY=${SECRET_KEY}
# Nginx in this installer terminates plain HTTP on port 80. Secure-only
# cookies would make login fail until TLS is configured; set this to true
# after enabling HTTPS.
VOD_SECURE_COOKIES=false
VOD_USE_X_ACCEL=true
EOF
    chown root:"${SERVICE_USER}" "${ENV_FILE}"
    chmod 640 "${ENV_FILE}"
    echo -e "${YELLOW}${BOLD}[!] Generated initial admin credentials in ${ENV_FILE}:${NC}"
    echo -e "${YELLOW}    Username: vaultadmin${NC}"
    echo -e "${YELLOW}    Password: ${INITIAL_PASS}${NC}"
else
    echo -e "${GREEN}[OK] Existing configuration found at ${ENV_FILE}.${NC}"
fi

# 7. Systemd Service Registration
echo -e "${BLUE}[7/8] Registering systemd daemons...${NC}"

cat > /etc/systemd/system/streamvault-webui.service <<EOF
[Unit]
Description=StreamVault - WebUI Daemon
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment="PYTHONPATH=${APP_DIR}:${APP_DIR}/WEBUI"
ExecStart=${VENV_DIR}/bin/gunicorn --workers 4 --worker-class gthread --threads 4 --bind 127.0.0.1:8080 --timeout 120 --access-logfile ${LOG_DIR}/webui_access.log --error-logfile ${LOG_DIR}/webui_error.log WEBUI.webui:app
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/streamvault-recorder.service <<EOF
[Unit]
Description=StreamVault - Stream Recorder Daemon
After=network.target streamvault-webui.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment="PYTHONPATH=${APP_DIR}"
ExecStart=${VENV_DIR}/bin/python3 recorder/Streamrecorder.py
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/streamvault-worker.service <<EOF
[Unit]
Description=StreamVault - Background Task Worker
After=network.target streamvault-webui.service streamvault-recorder.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment="PYTHONPATH=${APP_DIR}"
ExecStart=${VENV_DIR}/bin/python3 recorder/worker.py
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable streamvault-webui.service streamvault-recorder.service streamvault-worker.service

# 8. Registering Global CLI Binary
echo -e "${BLUE}[8/8] Registering global command '/usr/local/bin/streamvault'...${NC}"
chmod +x "${APP_DIR}/bin/streamvault"
ln -sf "${APP_DIR}/bin/streamvault" /usr/local/bin/streamvault

# Nginx Site Setup
if [[ -d /etc/nginx/sites-available ]]; then
    echo -e "${CYAN}[*] Configuring Nginx reverse proxy...${NC}"
    cat > /etc/nginx/sites-available/streamvault.conf <<EOF
server {
    listen 80;
    server_name localhost;

    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    client_max_body_size 4G;

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /_protected/ {
        internal;
        alias ${DATA_DIR}/;
        mp4;
        mp4_buffer_size 1m;
        mp4_max_buffer_size 10m;
    }

    location /_protected_files/ {
        internal;
        alias ${DATA_DIR}/files/;
        mp4;
        mp4_buffer_size 1m;
        mp4_max_buffer_size 10m;
    }

    location /_protected_vodcache/ {
        internal;
        alias ${DATA_DIR}/.vodcache/;
    }

    location /_protected_thumbs/ {
        internal;
        alias ${DATA_DIR}/.thumbs/;
        expires 1d;
        add_header Cache-Control "public";
    }

    location /_protected_hls/ {
        internal;
        alias ${DATA_DIR}/.hls/;
        types {
            application/vnd.apple.mpegurl m3u8;
            video/mp2t ts;
            video/mp4 m4s mp4;
        }
        default_type application/octet-stream;
        add_header Cache-Control "no-cache";
    }

    location /_protected_hls_live/ {
        internal;
        alias ${RAMDISK_DIR}/;
        open_file_cache off;
        types {
            application/vnd.apple.mpegurl m3u8;
            video/mp2t ts;
            video/mp4 m4s mp4;
        }
        default_type application/octet-stream;
        add_header Cache-Control "no-cache, no-store, must-revalidate";
        add_header Access-Control-Allow-Origin *;
    }
}
EOF
    ln -sf /etc/nginx/sites-available/streamvault.conf /etc/nginx/sites-enabled/
    nginx -t && systemctl reload nginx || true
fi

# Start services
systemctl restart streamvault-webui.service streamvault-recorder.service streamvault-worker.service

echo -e "\n${GREEN}${BOLD}======================================================================${NC}"
echo -e "${GREEN}${BOLD}      StreamVault Installation & Registration Complete!               ${NC}"
echo -e "${GREEN}${BOLD}======================================================================${NC}"
echo -e "You can now control the installation from anywhere using the command:"
echo -e "  ${CYAN}${BOLD}streamvault status${NC}     - View running daemons and health"
echo -e "  ${CYAN}${BOLD}streamvault logs${NC}       - Stream real-time logs"
echo -e "  ${CYAN}${BOLD}streamvault restart${NC}    - Restart services"
echo -e "\nConfiguration: ${CONFIG_DIR}/env"
echo -e "Web Access:    http://localhost"
