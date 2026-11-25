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
    rsync \
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
# Runtime data, databases, secrets and local environments never belong in the
# code tree: a source checkout used for testing may contain all of them. The
# list is shared with scripts/streamvault_vps.sh.
EXCLUDES_FILE="${SCRIPT_DIR}/scripts/deploy-excludes.txt"
if [[ -f "${SCRIPT_DIR}/WEBUI/webui.py" ]]; then
    echo -e "${CYAN}[*] Copying from local source tree (${SCRIPT_DIR})...${NC}"
    [[ -r "${EXCLUDES_FILE}" ]] || { echo -e "${RED}[ERROR] ${EXCLUDES_FILE} missing.${NC}"; exit 1; }
    rsync -a --exclude-from="${EXCLUDES_FILE}" "${SCRIPT_DIR}/" "${APP_DIR}/"
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
    # 'streamvault update' pulls with git, so the history of an official clone
    # comes along. Hooks and the config stay behind: they may hold credentials
    # or commands. Checkouts of other remotes are copied without history.
    SOURCE_ORIGIN="$(git -c safe.directory='*' -C "${SCRIPT_DIR}" config --get remote.origin.url 2>/dev/null || true)"
    if [[ -d "${SCRIPT_DIR}/.git" && ! -L "${SCRIPT_DIR}/.git" && "${SCRIPT_DIR}" != "${APP_DIR}" \
        && "${SOURCE_ORIGIN}" =~ ^https://github\.com/Blackwellcode/streamvault(\.git)?/?$ ]]; then
        rsync -a --exclude=/hooks/ --exclude=/config "${SCRIPT_DIR}/.git/" "${APP_DIR}/.git/"
        chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}/.git"
        runuser -u "${SERVICE_USER}" -- git -C "${APP_DIR}" init -q
        runuser -u "${SERVICE_USER}" -- git -C "${APP_DIR}" config remote.origin.url "${REPO_URL}"
        runuser -u "${SERVICE_USER}" -- git -C "${APP_DIR}" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
    fi
else
    echo -e "${CYAN}[*] Cloning from GitHub repository (${REPO_URL})...${NC}"
    # The checkout belongs to the service user; git runs as that user so root
    # never executes repository hooks or leaves root-owned files behind.
    chown "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
    if [[ -d "${APP_DIR}/.git" ]]; then
        runuser -u "${SERVICE_USER}" -- git -C "${APP_DIR}" pull --ff-only
    else
        runuser -u "${SERVICE_USER}" -- git clone "${REPO_URL}" "${APP_DIR}"
    fi
fi

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
    unset INITIAL_PASS SECRET_KEY
    # The password stays out of terminal scrollback, CI logs and screen shares.
    echo -e "${YELLOW}${BOLD}[!] Generated initial admin credentials in ${ENV_FILE}.${NC}"
    echo -e "${YELLOW}    Username: vaultadmin${NC}"
    echo -e "${YELLOW}    Password: sudo grep '^VOD_PASS=' ${ENV_FILE}${NC}"
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
ExecStart=${VENV_DIR}/bin/gunicorn --workers 4 --worker-class gthread --threads 4 --bind 127.0.0.1:8080 --timeout 120 --logger-class WEBUI.gunicorn_logging.EscapedPathLogger --access-logfile ${LOG_DIR}/webui_access.log --access-logformat '%%(h)s %%(l)s %%(u)s %%(t)s "%%(m)s %%(U)s %%(H)s" %%(s)s %%(b)s "%%(f)s" "%%(a)s"' --error-logfile ${LOG_DIR}/webui_error.log WEBUI.webui:app
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

cat > /etc/systemd/system/streamvault-livebot.service <<EOF
[Unit]
Description=StreamVault - Live Restream Daemon
After=network.target streamvault-webui.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment="PYTHONPATH=${APP_DIR}"
ExecStart=${VENV_DIR}/bin/python3 -m recorder.livebot
# Stops its streamlink/ffmpeg children first.
TimeoutStopSec=30s
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable streamvault-webui.service streamvault-recorder.service streamvault-worker.service streamvault-livebot.service

# 8. Registering Global CLI Binary
echo -e "${BLUE}[8/8] Registering global command '/usr/local/bin/streamvault'...${NC}"
chmod +x "${APP_DIR}/bin/streamvault"
ln -sf "${APP_DIR}/bin/streamvault" /usr/local/bin/streamvault

# Nginx Site Setup
if [[ -d /etc/nginx/sites-available ]]; then
    echo -e "${CYAN}[*] Configuring Nginx reverse proxy...${NC}"
    SITE_FILE=/etc/nginx/sites-available/streamvault.conf
    SITE_LINK=/etc/nginx/sites-enabled/streamvault.conf
    SITE_HASH_FILE="${CONFIG_DIR}/nginx-site.sha256"
    # The untouched site of 0.1.0 and 0.1.1, which kept no hash file.
    SITE_HASH_0_1=6672e16dbe3102c7601876703cff0c4a22fa49de973b3c1d3fdf87e21cb2b60a
    SITE_TARGET="${SITE_FILE}"
    SITE_BACKUP=""
    DISABLED_STOCK=0
    # Only a file this installer wrote and nobody changed is replaced. A site with
    # TLS, its own server_name or any other edit stays in use.
    if [[ -f "${SITE_FILE}" ]]; then
        SITE_HASH="$(sha256sum "${SITE_FILE}" | cut -d' ' -f1)"
        if [[ -r "${SITE_HASH_FILE}" ]]; then
            [[ "$(cat "${SITE_HASH_FILE}")" == "${SITE_HASH}" ]] || SITE_TARGET="${SITE_FILE}.new"
        elif [[ "${SITE_HASH}" != "${SITE_HASH_0_1}" ]]; then
            SITE_TARGET="${SITE_FILE}.new"
        fi
        if [[ "${SITE_TARGET}" == "${SITE_FILE}" ]]; then
            SITE_BACKUP="${SITE_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
            cp -a "${SITE_FILE}" "${SITE_BACKUP}"
        fi
    fi
    # The stock Debian/Ubuntu site answers requests by IP address with the nginx
    # welcome page. Disable it only while the package's own file is unchanged and
    # its web root holds nothing but the placeholder page.
    STOCK_LINK=/etc/nginx/sites-enabled/default
    STOCK_FILE=/etc/nginx/sites-available/default
    if [[ "${SITE_TARGET}" == "${SITE_FILE}" && -L "${STOCK_LINK}" && -f "${STOCK_FILE}" \
        && "$(readlink -f "${STOCK_LINK}")" == "${STOCK_FILE}" ]]; then
        # Not in a pipe: pipefail would abort when the package is unknown.
        STOCK_CONFFILES="$(dpkg-query -W -f='${Conffiles}\n' nginx-common 2>/dev/null || true)"
        STOCK_MD5="$(awk '$1 == "/etc/nginx/sites-available/default" {print $2}' <<<"${STOCK_CONFFILES}")"
        STOCK_ROOT="$(ls -A /var/www/html 2>/dev/null || true)"
        if [[ -n "${STOCK_MD5}" && "$(md5sum "${STOCK_FILE}" | cut -d' ' -f1)" == "${STOCK_MD5}" \
            && ( -z "${STOCK_ROOT}" || "${STOCK_ROOT}" == "index.nginx-debian.html" ) ]]; then
            rm -f "${STOCK_LINK}"
            DISABLED_STOCK=1
            echo -e "${CYAN}[*] Disabled the stock nginx site (still in ${STOCK_FILE}).${NC}"
        fi
    fi
    # Only one site may be the default server of port 80. Ask nginx itself, so
    # conf.d and nginx.conf count as well; our own file does not.
    LISTEN_DEFAULT=" default_server"
    NGINX_DUMP="$(nginx -T 2>/dev/null || true)"
    OTHER_DEFAULT="$(awk '
        /^# configuration file / { own = ($4 ~ /streamvault\.conf:?$/); next }
        !own { sub(/#.*/, ""); while (match($0, /listen[^;]*default_server[^;]*;/)) {
                   entry = substr($0, RSTART, RLENGTH); $0 = substr($0, RSTART + RLENGTH)
                   if (entry !~ /:?443|unix:/) print entry } }' <<<"${NGINX_DUMP}" | head -n 1)"
    if [[ -n "${OTHER_DEFAULT}" ]]; then
        LISTEN_DEFAULT=""
        echo -e "${YELLOW}[!] Another site is the nginx default server; set server_name in ${SITE_FILE}.${NC}"
    fi
    cat > "${SITE_TARGET}" <<EOF
# WebSocket upgrades only when the client asks for one.
map \$http_upgrade \$streamvault_connection_upgrade {
    default upgrade;
    ''      close;
}

# Access log without cookies and query strings (no ?token= in the log);
# rt/urt show slow requests and upstreams.
map \$http_referer \$streamvault_referer {
    default "-";
    "~^(?<sv_referer_path>[^?#]+)" \$sv_referer_path;
}
log_format streamvault_timed '\$remote_addr - \$remote_user [\$time_local] "\$request_method \$uri \$server_protocol" \$status \$body_bytes_sent "\$streamvault_referer" "\$http_user_agent" rt=\$request_time urt=\$upstream_response_time';

server {
    listen 80${LISTEN_DEFAULT};
    server_name _;
    access_log /var/log/nginx/streamvault.access.log streamvault_timed;

    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    # Uploads have their own location below.
    client_max_body_size 2m;

    gzip on;
    # Everything goes through the proxy; without gzip_proxied nothing is compressed.
    gzip_proxied any;
    gzip_vary on;
    gzip_types text/plain text/css application/json application/javascript text/javascript text/xml;

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$streamvault_connection_upgrade;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        # Only the /_live_auth subrequest may carry this marker.
        proxy_set_header X-StreamVault-Live-Auth "";
    }

    # Only these admin uploads may be large. Without request buffering nginx spools
    # nothing to disk; the app refuses everybody but admins before it reads a byte.
    location ~ ^/(files/upload|admin/banner/update|streamer/[^/]+/(banner|avatar)/update)\$ {
        client_max_body_size 4G;
        proxy_request_buffering off;
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-StreamVault-Live-Auth "";
        proxy_connect_timeout 600;
        proxy_send_timeout 600;
        proxy_read_timeout 600;
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

    # A location with its own add_header drops the server-level ones, so the
    # security headers are repeated wherever add_header is used.
    location /_protected_thumbs/ {
        internal;
        alias ${DATA_DIR}/.thumbs/;
        # Thumbnails sit behind login: never in shared caches.
        add_header Cache-Control "private, max-age=86400" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
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
        add_header Cache-Control "no-cache" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
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
        add_header Cache-Control "no-cache, no-store, must-revalidate" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    }

    # Direct live routes: the WebUI checks session and channel visibility
    # for every request (auth_request), otherwise 401/403.
    location = /_live_auth {
        internal;
        proxy_pass http://127.0.0.1:8080/api/live/auth;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Original-URI \$request_uri;
        # Marks the request as nginx's auth subrequest; /api/live/auth answers 404 without it.
        proxy_set_header X-StreamVault-Live-Auth 1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Real-time MPEG-TS from the livebot service (127.0.0.1:8085). If it is not
    # running nginx answers 502 and the player switches to HLS.
    location ~ ^/live/[A-Za-z0-9_-]+\.ts\$ {
        auth_request /_live_auth;
        proxy_pass http://127.0.0.1:8085;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # The session was checked above; the livebot never needs the cookie.
        proxy_set_header Cookie "";
        proxy_buffering off;
        proxy_cache off;
        proxy_request_buffering off;
        proxy_read_timeout 300s;
        tcp_nodelay on;
        chunked_transfer_encoding on;
        add_header Cache-Control "no-store, no-cache, must-revalidate, max-age=0" always;
        add_header X-Accel-Buffering "no" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    }

    # LL-HLS playlist and segments straight from the live RAM disk.
    location ~ ^/live/(?<hls_chan>[A-Za-z0-9_-]+)/(?<hls_file>(stream|index)\.m3u8|seg_[0-9]+\.ts)\$ {
        auth_request /_live_auth;
        alias ${RAMDISK_DIR}/\$hls_chan/\$hls_file;
        open_file_cache off;
        types {
            application/vnd.apple.mpegurl m3u8;
            video/mp2t ts;
        }
        default_type application/octet-stream;
        add_header Cache-Control "no-store, no-cache, must-revalidate, max-age=0" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    }
}
EOF
    if [[ "${SITE_TARGET}" == "${SITE_FILE}" ]]; then
        # At once, so a file of an interrupted run is not mistaken for an edit.
        sha256sum "${SITE_FILE}" | cut -d' ' -f1 > "${SITE_HASH_FILE}"
        LINK_EXISTED=0
        [[ -e "${SITE_LINK}" ]] && LINK_EXISTED=1
        ln -sf "${SITE_FILE}" "${SITE_LINK}"
    fi
    if ! nginx -t; then
        # Leave nothing behind that keeps nginx from starting after a reboot.
        if [[ "${SITE_TARGET}" == "${SITE_FILE}" ]]; then
            mv -f "${SITE_FILE}" "${SITE_FILE}.rejected"
            rm -f "${SITE_HASH_FILE}"
            if [[ -n "${SITE_BACKUP}" ]]; then
                cp -a "${SITE_BACKUP}" "${SITE_FILE}"
                sha256sum "${SITE_FILE}" | cut -d' ' -f1 > "${SITE_HASH_FILE}"
            fi
            [[ "${LINK_EXISTED}" == "1" ]] || rm -f "${SITE_LINK}"
            [[ "${DISABLED_STOCK}" == "0" ]] || ln -s "${STOCK_FILE}" "${STOCK_LINK}"
        fi
        echo -e "${RED}[ERROR] nginx -t failed; the previous sites are active again. Fix the configuration and re-run.${NC}"
        exit 1
    fi
    if [[ "${SITE_TARGET}" != "${SITE_FILE}" ]]; then
        echo -e "${YELLOW}${BOLD}[!] ${SITE_FILE} was edited and stays in use.${NC}"
        echo -e "${YELLOW}    Merge the new live locations from ${SITE_TARGET}; until then live playback does not work.${NC}"
    fi
    systemctl enable nginx >/dev/null 2>&1 || echo -e "${YELLOW}[!] nginx does not start at boot (systemctl enable nginx).${NC}"
    # Also starts nginx when it is installed but not running.
    systemctl reload-or-restart nginx
fi

# Start services. A re-run on a live system must not split running recordings.
ACTIVE_BASE="$(grep -E '^VOD_BASE_DIR=' "${ENV_FILE}" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
ACTIVE_BASE="${ACTIVE_BASE:-${DATA_DIR}}"
FORCE_RESTART="${STREAMVAULT_FORCE_RESTART:-0}"
# shellcheck source=scripts/recording-guard.sh
source "${APP_DIR}/scripts/recording-guard.sh"
GUARD_RC=0
sv_recording_guard "${ACTIVE_BASE}" "${FORCE_RESTART}" || GUARD_RC=$?
if [[ "${GUARD_RC}" == "2" ]]; then
    # Fail closed: an unreadable archive could hide running recordings.
    echo -e "${RED}[ERROR] Services were NOT restarted. Fix the permissions or re-run with STREAMVAULT_FORCE_RESTART=1.${NC}"
    exit 1
elif [[ "${GUARD_RC}" == "1" ]]; then
    echo -e "${YELLOW}[!] Services were NOT restarted. Run 'streamvault restart' once the recordings"
    echo -e "    are finished, or re-run with STREAMVAULT_FORCE_RESTART=1.${NC}"
else
    systemctl restart streamvault-webui.service streamvault-recorder.service streamvault-worker.service streamvault-livebot.service
fi

echo -e "\n${GREEN}${BOLD}======================================================================${NC}"
echo -e "${GREEN}${BOLD}      StreamVault Installation & Registration Complete!               ${NC}"
echo -e "${GREEN}${BOLD}======================================================================${NC}"
echo -e "You can now control the installation from anywhere using the command:"
echo -e "  ${CYAN}${BOLD}streamvault status${NC}     - View running daemons and health"
echo -e "  ${CYAN}${BOLD}streamvault logs${NC}       - Stream real-time logs"
echo -e "  ${CYAN}${BOLD}streamvault restart${NC}    - Restart services"
echo -e "\nConfiguration: ${CONFIG_DIR}/env"
echo -e "Web Access:    http://<server address>"
echo -e "${YELLOW}Logins travel unencrypted until HTTPS is set up: INSTALL.md, section 6.${NC}"
