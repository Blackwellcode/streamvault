# StreamVault — Installation & Deployment Guide

This document provides a comprehensive, production-tested deployment guide for **StreamVault** on Linux (Ubuntu/Debian) and Docker, as well as instructions for local developer setups.

---

## 1. System Requirements & Architecture

| Component | Minimum | Recommended |
| :--- | :--- | :--- |
| **Operating System** | Ubuntu 22.04+ / Debian 12+ / Windows 10+ (Dev) | Ubuntu 24.04 LTS (x86_64) |
| **CPU** | 2 vCPUs | 4+ vCPUs |
| **RAM** | 2 GB RAM | 4+ GB RAM (allows 1 GB RAM-Disk for live HLS) |
| **Storage** | 20 GB (App + DB) | Fast NVMe / SSD for VOD cache & recordings |
| **Software** | Python 3.11+, FFmpeg 6+, Nginx 1.22+, SQLite 3.37+ | Redis 7+ (optional high-concurrency caching) |

---

## 2. Option A: Automated 1-Command Production Installer (Recommended)

On any clean Ubuntu 22.04+ or Debian 12+ VPS:

```bash
git clone https://github.com/Blackwellcode/streamvault.git /tmp/streamvault
cd /tmp/streamvault
sudo ./install.sh
```

### What the installer handles automatically:
- Installs all apt dependencies (`ffmpeg`, `python3-venv`, `nginx`, `sqlite3`).
- Sets up system user `streamvault` and directories (`/opt/streamvault`, `/var/lib/streamvault`).
- Generates cryptographically secure secrets in `/etc/streamvault/env`.
- Deploys and enables systemd daemons (`streamvault-webui.service`, `streamvault-recorder.service`, `streamvault-worker.service`).
- Configures Nginx with `X-Accel-Redirect` byte-range media offloading.
- **Registers the global Linux management command** `/usr/local/bin/streamvault`:

```bash
streamvault status       # Check daemons and live health
streamvault logs webui   # View WebUI log stream
streamvault logs recorder # View Stream Recorder log stream
streamvault logs worker  # View background task worker log stream
streamvault restart      # Restart application daemons
streamvault update       # Pull updates, upgrade dependencies, and reload
streamvault check        # Validate Python syntax & Nginx configurations
```

---

## 3. Option B: Docker Compose Deployment

### Step 1: Clone Repository & Prepare Environment
```bash
git clone https://github.com/Blackwellcode/streamvault.git
cd streamvault
cp .env.example .env
```

### Step 2: Configure Secrets
Edit `.env` and set strong credentials:
```bash
nano .env
# Set VOD_USER, VOD_PASS, and VOD_SECRET_KEY
```

### Step 3: Launch Containers
```bash
docker compose up -d --build
```
Access the application at `http://localhost:8090` (proxied by Nginx). The
Compose stack runs WebUI, recorder and worker under supervisord; the `hls_live`
volume is shared between the app and Nginx containers.

---

## 4. Option C: Manual Bare-Metal Deployment (Ubuntu/Debian)

### Step 1: Install System Dependencies
```bash
sudo apt update && sudo apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    ffmpeg \
    nginx \
    sqlite3 \
    redis-server \
    git \
    curl
```

### Step 2: Create Dedicated Service User & Directories
```bash
sudo useradd -r -s /bin/bash -d /opt/streamvault streamvault
sudo mkdir -p /opt/streamvault /var/log/streamvault /var/lib/streamvault/data /dev/shm/streamvault_hls
sudo chown -R streamvault:streamvault /opt/streamvault /var/log/streamvault /var/lib/streamvault /dev/shm/streamvault_hls
sudo chmod 755 /opt/streamvault
```

### Step 3: Deploy Application Code
```bash
sudo -u streamvault git clone https://github.com/Blackwellcode/streamvault.git /opt/streamvault/app
cd /opt/streamvault/app
sudo -u streamvault python3 -m venv /opt/streamvault/venv
sudo -u streamvault /opt/streamvault/venv/bin/pip install --upgrade pip
sudo -u streamvault /opt/streamvault/venv/bin/pip install -r requirements.txt
```

### Step 4: Environment Configuration
Create `/etc/streamvault/env`:
```bash
sudo mkdir -p /etc/streamvault
sudo cp .env.example /etc/streamvault/env
sudo chown -R root:streamvault /etc/streamvault
sudo chmod 640 /etc/streamvault/env
```
Ensure `/etc/streamvault/env` contains:
```ini
VOD_HOST=127.0.0.1
VOD_PORT=8080
# Leave empty to derive from the request, or set the public HTTPS origin.
VOD_PUBLIC_BASE=
VOD_BASE_DIR=/var/lib/streamvault/data
LIVE_HLS_DIR=/dev/shm/streamvault_hls
VOD_USER=vaultadmin
VOD_PASS=
VOD_SECRET_KEY=
# The bundled Nginx site listens on HTTP port 80. Keep secure-only cookies
# disabled until HTTPS is enabled, otherwise login redirects loop.
VOD_SECURE_COOKIES=false
VOD_USE_X_ACCEL=true
# Optional dedicated key for cloud credentials (falls back to VOD_SECRET_KEY).
# STORAGE_ENCRYPTION_KEY=
```

Fill the two empty secret values with freshly generated ones:
```bash
python3 -c "import secrets; print('VOD_PASS=' + secrets.token_urlsafe(24))"
python3 -c "import secrets; print('VOD_SECRET_KEY=' + secrets.token_hex(32))"
```

---

## 5. Systemd Daemon Units

### WebUI Service (`/etc/systemd/system/streamvault-webui.service`)
```ini
[Unit]
Description=StreamVault - WebUI Daemon
After=network.target redis.service

[Service]
Type=simple
User=streamvault
Group=streamvault
WorkingDirectory=/opt/streamvault/app
EnvironmentFile=/etc/streamvault/env
Environment="PYTHONPATH=/opt/streamvault/app:/opt/streamvault/app/WEBUI"
ExecStart=/opt/streamvault/venv/bin/gunicorn \
    --workers 4 \
    --worker-class gthread \
    --threads 4 \
    --bind 127.0.0.1:8080 \
    --timeout 120 \
    --access-logfile /var/log/streamvault/access.log \
    --error-logfile /var/log/streamvault/error.log \
    WEBUI.webui:app
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

### Stream Recorder Daemon (`/etc/systemd/system/streamvault-recorder.service`)
```ini
[Unit]
Description=StreamVault - Stream Recorder Daemon
After=network.target streamvault-webui.service

[Service]
Type=simple
User=streamvault
Group=streamvault
WorkingDirectory=/opt/streamvault/app
EnvironmentFile=/etc/streamvault/env
Environment="PYTHONPATH=/opt/streamvault/app"
ExecStart=/opt/streamvault/venv/bin/python3 recorder/Streamrecorder.py
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

### Background Task Worker (`/etc/systemd/system/streamvault-worker.service`)
```ini
[Unit]
Description=StreamVault - Background Task Worker
After=network.target streamvault-webui.service streamvault-recorder.service

[Service]
Type=simple
User=streamvault
Group=streamvault
WorkingDirectory=/opt/streamvault/app
EnvironmentFile=/etc/streamvault/env
Environment="PYTHONPATH=/opt/streamvault/app"
ExecStart=/opt/streamvault/venv/bin/python3 recorder/worker.py
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

Enable and start services:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now streamvault-webui
sudo systemctl enable --now streamvault-recorder
sudo systemctl enable --now streamvault-worker
```

---

## 6. Nginx Reverse Proxy with X-Accel-Redirect

Nginx performs direct kernel-level offloading (`sendfile`) for video Range requests, preventing Python worker starvation during scrubbing:

Save to `/etc/nginx/sites-available/streamvault.conf`:
```nginx
server {
    listen 80;
    server_name vault.your-domain.example;

    # Performance Tuning
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    client_max_body_size 4G;

    # Gzip Compression
    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;

    # 1. Reverse Proxy to Python WebUI
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 2. X-Accel-Redirect media offload.
    # WebUI emits /_protected/<archive>/<file>; archives live directly below
    # VOD_BASE_DIR (not below VOD_BASE_DIR/files).
    location /_protected/ {
        internal;
        alias /var/lib/streamvault/data/;
        sendfile on;
        sendfile_max_chunk 1m;
        output_buffers 2 1m;
        gzip off;
        add_header Accept-Ranges bytes always;
    }

    # Legacy archives below VOD_FILES_DIR (default VOD_BASE_DIR/files).
    location /_protected_files/ {
        internal;
        alias /var/lib/streamvault/data/files/;
        sendfile on;
        sendfile_max_chunk 1m;
        output_buffers 2 1m;
        gzip off;
        add_header Accept-Ranges bytes always;
    }

    location /_protected_vodcache/ {
        internal;
        alias /var/lib/streamvault/data/.vodcache/;
        sendfile on;
        sendfile_max_chunk 1m;
        output_buffers 2 1m;
        gzip off;
    }

    location /_protected_thumbs/ {
        internal;
        alias /var/lib/streamvault/data/.thumbs/;
        expires 1d;
        add_header Cache-Control "public";
    }

    location /_protected_hls/ {
        internal;
        alias /var/lib/streamvault/data/.hls/;
        types {
            application/vnd.apple.mpegurl m3u8;
            video/mp2t ts;
            video/mp4 m4s mp4;
        }
        default_type application/octet-stream;
        add_header Cache-Control "no-cache";
    }

    # 3. Live HLS RAM-Disk offload
    location /_protected_hls_live/ {
        internal;
        alias /dev/shm/streamvault_hls/;
        open_file_cache off;
        types {
            application/vnd.apple.mpegurl m3u8;
            video/mp2t ts;
            video/mp4 m4s mp4;
        }
        default_type application/octet-stream;
        add_header Cache-Control "no-cache, no-store, must-revalidate";
    }
}
```

Keep the aliases in sync with `VOD_BASE_DIR`, `VOD_FILES_DIR`, `VOD_THUMB_DIR`,
`VOD_HLS_DIR` and `LIVE_HLS_DIR` if you changed those environment variables.
The `/_protected_files/` alias is only needed for legacy archives stored below
`VOD_FILES_DIR`; new recordings normally live directly below `VOD_BASE_DIR`.

Enable site and restart Nginx:
```bash
sudo ln -sf /etc/nginx/sites-available/streamvault.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

## 7. Cloud / External Storage

Cloud offloading is optional and requires the `streamvault-worker` daemon
(installed by `install.sh`). Configure it in **Systemsteuerung → Cloud-Speicher**
(`/admin/storage`) or via the console:

```bash
streamvault storage types
streamvault storage add --type google_drive --name "Drive Archive"
streamvault storage add --type webdav --name "Nextcloud" \
  --set base_url=https://cloud.example.com/remote.php/dav/files/vault/ \
  --set username=vault --set password=app-password
streamvault storage test 1
streamvault storage sync
streamvault storage objects
streamvault storage restore 42
```

For Google Drive, Dropbox and OneDrive, register this redirect URI in the
provider console and set `VOD_PUBLIC_BASE` to the public HTTPS origin:

```
https://<your-public-host>/admin/storage/oauth/callback
```

Credentials are encrypted with AES-GCM. Set a dedicated
`STORAGE_ENCRYPTION_KEY` (32+ random characters) or rely on the existing strong
`VOD_SECRET_KEY`. Keep the key stable; changing it makes stored credentials
unreadable. Safety defaults are automatic upload **off**, local deletion
**off**, and 14 days local retention.

---

## 8. Local Developer Setup

To run the application locally for development:

```bash
# Clone the repository
git clone https://github.com/Blackwellcode/streamvault.git
cd streamvault

# Setup Python virtual environment
python -m venv .venv
source .venv/bin/activate  # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Edit .env and set VOD_USER, VOD_PASS, and VOD_SECRET_KEY.
# Generate them with:
python -c "import secrets; print('VOD_PASS=' + secrets.token_urlsafe(24))"
python -c "import secrets; print('VOD_SECRET_KEY=' + secrets.token_hex(32))"

# Start WebUI server
python WEBUI/webui.py --port 8080
```
Open `http://localhost:8080/login` and log in with the credentials set in your `.env`.
