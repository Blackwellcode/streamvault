# NachtSilo Hub / StreamVault — Installation & Deployment Guide

This document provides a comprehensive, production-tested deployment guide for **NachtSilo Hub** on Linux (Ubuntu/Debian) and Docker, as well as instructions for local developer setups.

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
git clone https://github.com/Blackwellcode/nachtsilo-hub.git /tmp/nachtsilo
cd /tmp/nachtsilo
sudo ./install.sh
```

### What the installer handles automatically:
- Installs all apt dependencies (`ffmpeg`, `python3-venv`, `nginx`, `sqlite3`).
- Sets up system user `nachtsilo` and directories (`/opt/nachtsilo`, `/var/lib/nachtsilo`).
- Generates cryptographically secure secrets in `/etc/nachtsilo/env`.
- Deploys and enables systemd daemons (`nachtsilo-webui.service`, `nachtsilo-recorder.service`).
- Configures Nginx with `X-Accel-Redirect` byte-range media offloading.
- **Registers the global Linux management command** `/usr/local/bin/nachtsilo`:

```bash
nachtsilo status       # Check daemons and live health
nachtsilo logs webui   # View WebUI log stream
nachtsilo logs recorder# View Stream Recorder log stream
nachtsilo restart      # Restart application daemons
nachtsilo demo         # Run standalone sandbox demo mode
nachtsilo update       # Pull updates, upgrade dependencies, and reload
nachtsilo check        # Validate Python syntax & Nginx configurations
```

---

## 3. Option B: Docker Compose Deployment

### Step 1: Clone Repository & Prepare Environment
```bash
git clone https://github.com/Blackwellcode/nachtsilo-hub.git
cd nachtsilo-hub
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
Access the application at `http://localhost:8090` (proxied by Nginx) or directly at `http://localhost:8080`.

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
sudo mkdir -p /opt/streamvault /var/log/streamvault /var/lib/streamvault/data /dev/shm/streamrecorder_hls
sudo chown -R streamvault:streamvault /opt/streamvault /var/log/streamvault /var/lib/streamvault /dev/shm/streamrecorder_hls
sudo chmod 755 /opt/streamvault
```

### Step 3: Deploy Application Code
```bash
sudo -u streamvault git clone https://github.com/Blackwellcode/nachtsilo-hub.git /opt/streamvault/app
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
VOD_BASE_DIR=/var/lib/streamvault/data
LIVE_HLS_DIR=/dev/shm/streamrecorder_hls
VOD_USER=vaultadmin
VOD_PASS=YourSuperSecretPasswordHere987!
VOD_SECRET_KEY=generate_a_random_32_character_string_here
VOD_USE_X_ACCEL=true
```

---

## 4. Systemd Daemon Units

### WebUI Service (`/etc/systemd/system/streamvault-webui.service`)
```ini
[Unit]
Description=NachtSilo Hub - WebUI Daemon
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
Description=NachtSilo Hub - Stream Recorder Daemon
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

Enable and start services:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now streamvault-webui
sudo systemctl enable --now streamvault-recorder
```

---

## 5. Nginx Reverse Proxy with X-Accel-Redirect

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

    # 2. X-Accel-Redirect Internal Media Offload
    location /_protected/ {
        internal;
        alias /var/lib/streamvault/data/files/;
        mp4;
        mp4_buffer_size 1m;
        mp4_max_buffer_size 10m;
    }

    # 3. Live HLS RAM-Disk Offload
    location /_hls_live/ {
        alias /dev/shm/streamrecorder_hls/;
        add_header Cache-Control "no-cache, no-store, must-revalidate";
        add_header Access-Control-Allow-Origin *;
    }
}
```

Enable site and restart Nginx:
```bash
sudo ln -sf /etc/nginx/sites-available/streamvault.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

## 6. Turnkey Demo Mode (Developer / Local Evaluation)

To run the application locally without any real Twitch streams or credentials:

```bash
# Windows / Linux / macOS
python -m venv .venv
source .venv/bin/activate  # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

# Start in Demo Mode
python WEBUI/webui.py --demo --port 8080
```

1. Open `http://localhost:8080/login`.
2. Credentials: Username `admin`, Password `admin`.
3. The demo environment automatically seeds neutral streamers, playable VODs, thumbnails, synchronized chat replay, and live HLS streams.
