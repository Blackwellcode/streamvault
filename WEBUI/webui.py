#!/usr/bin/env python3

import bisect
import glob
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import shutil
import smtplib
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time

try:
    import fcntl
except ImportError:
    fcntl = None
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps

import yt_dlp
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    has_request_context,
    jsonify,
    make_response,
    redirect,
    render_template_string,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask_limiter import Limiter
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import safe_join, secure_filename

# `_` is used as a throwaway name throughout this module, so translations use the
# full names (both are extracted by pybabel by default).
from flask_babel import gettext, lazy_gettext
from recorder import chat_events
from recorder import clip_engine
from WEBUI import i18n as ui_i18n
from core import config
from core import database
from core import twitch_api
from core.health import (
    SYSTEMD_SHOW_PROPERTIES,
    parse_supervisor_status,
    parse_systemctl_show,
    systemd_unit_name,
)
from core import twitch_eventsub
from core import twitch_templates
from core import user_traffic
from core import vod_watch_slots
# Stalk module removed in open core
from core.mp4_utils import (
    BROWSER_MP4_MOVFLAGS,
    finalize_if_fragmented,
    is_archive_mp4_filename,
    is_fragmented_mp4,
)

BASE_DIR = config.BASE_DIR
HOST = os.environ.get("VOD_HOST", "0.0.0.0")
PORT = int(os.environ.get("VOD_PORT", "8080"))
PUBLIC_BASE = os.environ.get("VOD_PUBLIC_BASE", "").strip()
MAIL_DOMAIN = os.environ.get("MAIL_DOMAIN", "").strip().lower()
MAILCOW_API_URL = os.environ.get("MAILCOW_API_URL", "").strip().rstrip("/")
MAILCOW_API_KEY = os.environ.get("MAILCOW_API_KEY", "").strip()
MAIL_WEBMAIL_URL = os.environ.get("MAIL_WEBMAIL_URL", "").strip()
try:
    MAIL_DEFAULT_QUOTA_MB = int(os.environ.get("MAIL_DEFAULT_QUOTA_MB", "1024"))
except ValueError:
    MAIL_DEFAULT_QUOTA_MB = 1024
try:
    MAIL_API_TIMEOUT_SEC = int(os.environ.get("MAIL_API_TIMEOUT_SEC", "15"))
except ValueError:
    MAIL_API_TIMEOUT_SEC = 15
try:
    MAIL_SMTP_CHECK_TIMEOUT_SEC = float(os.environ.get("MAIL_SMTP_CHECK_TIMEOUT_SEC", "0.6"))
except ValueError:
    MAIL_SMTP_CHECK_TIMEOUT_SEC = 0.6
MAIL_SUBMISSION_HOST = os.environ.get("MAIL_SUBMISSION_HOST", "127.0.0.1").strip() or "127.0.0.1"
try:
    MAIL_SUBMISSION_PORT = int(os.environ.get("MAIL_SUBMISSION_PORT", "587"))
except ValueError:
    MAIL_SUBMISSION_PORT = 587


def detect_x_accel():
    if os.environ.get("VOD_USE_X_ACCEL", "false").lower() in ("true", "1", "yes"):
        return True
    candidates = [
        "/etc/nginx/conf.d/streamrecorder.conf",
        "/etc/nginx/conf.d/default.conf",
        "/etc/nginx/sites-enabled/streamvault.conf",
    ]
    try:
        candidates.extend(glob.glob("/etc/nginx/sites-enabled/*"))
    except OSError:
        pass
    for nginx_conf in dict.fromkeys(candidates):
        if not os.path.isfile(nginx_conf):
            continue
        try:
            with open(nginx_conf, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        if "/_protected/" in content and "proxy_pass" in content:
            return True
    return False

USE_X_ACCEL = detect_x_accel()
X_ACCEL_PREFIX = os.environ.get("VOD_X_ACCEL_PREFIX", "/_protected")
FILES_ACCEL_PREFIX = os.environ.get("VOD_FILES_ACCEL_PREFIX", "/_protected_files")
VOD_CACHE_ACCEL_PREFIX = os.environ.get("VOD_CACHE_ACCEL_PREFIX", "/_protected_vodcache")
HLS_TIME = int(os.environ.get("VOD_HLS_TIME", "4"))
HLS_GENERATE_TIMEOUT_SEC = int(os.environ.get("VOD_HLS_GENERATE_TIMEOUT_SEC", "7200"))
HLS_VIDEO_CRF = int(os.environ.get("VOD_HLS_VIDEO_CRF", "21"))
HLS_VIDEO_MAXRATE = os.environ.get("VOD_HLS_VIDEO_MAXRATE", "4500k")
HLS_VIDEO_BUFSIZE = os.environ.get("VOD_HLS_VIDEO_BUFSIZE", "9000k")
HLS_VIDEO_PRESET = os.environ.get("VOD_HLS_VIDEO_PRESET", "veryfast")
HLS_OLD_SEGMENT_TTL_SEC = int(os.environ.get("VOD_HLS_OLD_SEGMENT_TTL_SEC", "21600"))
HLS_ACCEL_PREFIX = os.environ.get("VOD_HLS_ACCEL_PREFIX", "/_protected_hls")
LIVE_HLS_ACCEL_PREFIX = os.environ.get("LIVE_HLS_ACCEL_PREFIX", "/_protected_hls_live")
LIVE_HLS_TTL_SEC = int(os.environ.get("LIVE_HLS_TTL_SEC", "24"))
CLIP_SUBDIR = clip_engine.CLIP_SUBDIR
CHAT_LOG_SUBDIR = os.environ.get("VOD_CHAT_SUBDIR", "chat_logs")
THUMB_ACCEL_PREFIX = os.environ.get("VOD_THUMB_ACCEL_PREFIX", "/_protected_thumbs")
HISTORY_LIMIT = int(os.environ.get("VOD_HISTORY_LIMIT", "20"))
PAGE_SIZE = int(os.environ.get("VOD_PAGE_SIZE", "50"))
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", GQL_CLIENT_ID).strip()
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_APP_ACCESS_TOKEN = (
    os.environ.get("TWITCH_APP_ACCESS_TOKEN", "").strip()
)
MAIL_ADMIN_ENABLED = os.environ.get("VOD_MAIL_ADMIN_ENABLED", "false").lower() in ("true", "1", "yes")
STALK_ENABLED = False


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# 0 = no time-based login expiry (cookie stays valid until logout or the account is gone).
# Browsers cap cookie lifetime near 400 days; SESSION_REFRESH_EACH_REQUEST extends it on use.
SESSION_MAX_AGE_SEC = max(0, _env_int("VOD_SESSION_MAX_AGE_SEC", 0))
SESSION_COOKIE_MAX_AGE_SEC = max(86400, _env_int("VOD_SESSION_COOKIE_MAX_AGE_SEC", 400 * 86400))
_MEDIA_HTTP_METHODS = ("GET", "HEAD", "OPTIONS")
LOGIN_BAN_TTL_SEC = int(os.environ.get("VOD_LOGIN_BAN_TTL_SEC", "1800"))
# OBS browser sources historically pass overlay tokens in the query string.
OVERLAY_ALLOW_QUERY_TOKEN = os.environ.get("OVERLAY_ALLOW_QUERY_TOKEN", "true").lower() in (
    "true",
    "1",
    "yes",
    "on",
)
_OVERLAY_QUERY_TOKEN_LOGGED = False


def _resolve_ratelimit_storage_uri(raw: str) -> str:
    """Flask-Limiter storage URI. Default memory://; use redis:// for multi-worker."""
    uri = (raw or "").strip() or "memory://"
    try:
        from limits.storage import SCHEMES

        scheme = uri.split("://", 1)[0].lower()
        if scheme not in SCHEMES:
            return "memory://"
    except Exception:
        return "memory://"
    return uri


RATELIMIT_STORAGE_URI = _resolve_ratelimit_storage_uri(
    os.environ.get("VOD_RATELIMIT_STORAGE_URI", "memory://")
)
# Default credentials removed for security - only environment variables are allowed
def _parse_trusted_proxies(raw):
    """Comma-separated IPs or CIDR networks, e.g. ``127.0.0.1,::1,172.16.0.0/12``."""
    networks = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logging.error("Ignoring invalid VOD_TRUSTED_PROXIES entry: %r", item)
    return tuple(networks)


TRUSTED_PROXY_NETWORKS = _parse_trusted_proxies(os.environ.get("VOD_TRUSTED_PROXIES", "127.0.0.1,::1"))
CHAT_INDEX_MIN_SIZE = int(os.environ.get("VOD_CHAT_INDEX_MIN_SIZE", "5000000"))  # 5 MB
CHAT_INDEX_STEP = int(os.environ.get("VOD_CHAT_INDEX_STEP", "200"))
MAX_CHAT_SEARCH_RESULTS = int(os.environ.get("VOD_CHAT_SEARCH_LIMIT", "500"))
LOG_BROWSER_WORD_RESULT_LIMIT = int(os.environ.get("VOD_LOG_BROWSER_WORD_LIMIT", "1000"))
GLOBAL_SEARCH_VIDEO_LIMIT = int(os.environ.get("VOD_GLOBAL_SEARCH_VIDEO_LIMIT", "24"))
GLOBAL_SEARCH_CHAT_LIMIT = int(os.environ.get("VOD_GLOBAL_SEARCH_CHAT_LIMIT", "40"))
STREAMER_ANALYTICS_DAYS = int(os.environ.get("VOD_STREAMER_ANALYTICS_DAYS", "90"))
STREAMER_ANALYTICS_DURATION_SCAN_LIMIT = int(os.environ.get("VOD_STREAMER_DURATION_SCAN_LIMIT", "180"))
TWITCH_BADGE_CACHE_TTL_SEC = int(os.environ.get("TWITCH_BADGE_CACHE_TTL_SEC", "86400"))
TWITCH_BADGE_LIBRARY_TTL_SEC = int(os.environ.get("TWITCH_BADGE_LIBRARY_TTL_SEC", str(7 * 86400)))
TWITCH_BADGE_PUBLIC_FALLBACK = os.environ.get("TWITCH_BADGE_PUBLIC_FALLBACK", "true").lower() in ("1", "true", "yes", "on")
TWITCH_BADGE_DOWNLOAD_TIMEOUT_SEC = float(os.environ.get("TWITCH_BADGE_DOWNLOAD_TIMEOUT_SEC", "8"))
TWITCH_BADGE_IMAGE_MAX_BYTES = int(os.environ.get("TWITCH_BADGE_IMAGE_MAX_BYTES", "262144"))
TWITCH_BADGE_SYNC_WORKERS = int(os.environ.get("TWITCH_BADGE_SYNC_WORKERS", "12"))
TWITCH_BADGE_AUTO_SYNC = os.environ.get("TWITCH_BADGE_AUTO_SYNC", "true").lower() in ("1", "true", "yes", "on")
TWITCH_BADGE_CACHE_DIR = os.path.join(config.META_DIR, "twitch_badges")
TWITCH_BADGE_ASSET_DIR = os.path.join(TWITCH_BADGE_CACHE_DIR, "assets")
TWITCH_BADGE_GLOBAL_KEY = "global"
_TWITCH_BADGE_SYNC_LOCK = threading.Lock()
_TWITCH_BADGE_SYNC_STATE = {"running": False, "last": None, "error": ""}
DEFAULT_THEME = "vault"
THEME_OPTIONS = [
    {"key": "vault", "name": "Graphit Lime"},
    {"key": "aurora", "name": "Aurora Mint"},
    {"key": "cyber", "name": "Neon Cyberpunk 2.0"},
    {"key": "ember", "name": "Ember Red"},
    {"key": "ocean", "name": "Ocean Blue"},
]
VALID_THEME_KEYS = {theme["key"] for theme in THEME_OPTIONS}
def _compute_asset_version(static_dir):
    """Cache-busting token for ?v= on static URLs, derived from the shipped files.

    Any edited, added or removed file under WEBUI/static changes the token, so
    browsers fetch new CSS/JS after an update without a manual version bump.
    """
    digest = hashlib.sha1()
    for root, dirs, files in os.walk(static_dir):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError as exc:
                logging.warning("Asset version: cannot stat %s: %s", path, exc)
                continue
            rel = os.path.relpath(path, static_dir).replace(os.sep, "/")
            digest.update(f"{rel}:{st.st_size}:{st.st_mtime_ns};".encode("utf-8"))
    return digest.hexdigest()[:12]


ASSET_VERSION = os.environ.get("VOD_ASSET_VERSION", "").strip() or _compute_asset_version(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
)
UI_FONTS_HREF = (
    "https://fonts.googleapis.com/css2?"
    "family=IBM+Plex+Mono:wght@400;500;600&"
    "family=Inter:wght@400;500;600;700&display=swap"
)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def downloads_enabled() -> bool:
    """File attachment downloads (VOD/clip/files). Streaming playback is unaffected."""
    return _env_flag("VOD_DOWNLOADS_ENABLED", True)


def public_shares_enabled() -> bool:
    """Login-free /public/<token> share links."""
    return _env_flag("VOD_PUBLIC_SHARES_ENABLED", True)

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

def load_html_template(name, fallback=""):
    path = os.path.join(TEMPLATES_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return fallback


TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "tiny")
TRANSCRIBE_THREADS = int(os.environ.get("TRANSCRIBE_THREADS", "1"))
TRANSCRIBE_LANGUAGE = os.environ.get("TRANSCRIBE_LANGUAGE", "de")
TRANSCRIBE_BACKEND = os.environ.get("TRANSCRIBE_BACKEND", "whisper").strip().lower() or "whisper"
TWITCH_EVENTSUB_ENABLED = os.environ.get("TWITCH_EVENTSUB_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
METRICS_TOKEN = os.environ.get("METRICS_TOKEN", "").strip()

def load_secret_key():
    from core.storage.crypto import is_weak_secret

    configured = str(config.SECRET_KEY or "").strip()
    if configured and not is_weak_secret(configured):
        return configured
    key_path = os.path.join(config.META_DIR, "secret_key.txt")
    try:
        if os.path.isfile(key_path):
            with open(key_path, "r", encoding="utf-8") as f:
                key = f.read().strip()
                if key and not is_weak_secret(key):
                    try:
                        os.chmod(key_path, 0o600)
                    except OSError:
                        pass
                    return key
    except Exception:
        pass
    os.makedirs(config.META_DIR, exist_ok=True)
    key = secrets.token_hex(32)
    try:
        fd = os.open(
            key_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(key)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.chmod(key_path, 0o600)
    except Exception:
        pass
    return key

app = Flask(__name__)
app.secret_key = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get("VOD_SECURE_COOKIES", "true").lower() == "true",
    SESSION_COOKIE_PATH="/",
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_COOKIE_MAX_AGE_SEC),
    SESSION_REFRESH_EACH_REQUEST=True,
)
ui_i18n.init_app(app)

# ============ SECURITY: Flask-Limiter Setup ============
# Rate limiting to prevent brute force and DoS attacks
limiter = Limiter(
    app=app,
    key_func=lambda: get_client_ip(),
    default_limits=["5000 per day", "1000 per hour"],
    storage_uri=RATELIMIT_STORAGE_URI,
)
# ========================================================

os.makedirs(config.FILES_DIR, exist_ok=True)

# DB Init beim Start
database.init_db()


def ensure_ban_schema():
    """Ensure bans.expires_at exists for TTL login lockouts."""
    conn = database.get_db()
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(bans)").fetchall()}
        if "expires_at" not in columns:
            conn.execute("ALTER TABLE bans ADD COLUMN expires_at REAL")
            conn.commit()
    finally:
        conn.close()


def ensure_login_attempts_schema():
    """Shared login-attempt counters for multi-worker gunicorn."""
    conn = database.get_db()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT NOT NULL, ts REAL NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_ts ON login_attempts(ip, ts)"
        )
        conn.commit()
    finally:
        conn.close()


ensure_ban_schema()
ensure_login_attempts_schema()

# Logging konfigurieren (verhindert Encoding-Crashes bei print)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

METADATA_CACHE = {"mtime": 0, "data": {}}
CHAT_INDEX_CACHE = {}

def get_sys_stats():
    # CPU Load (1 min avg)
    try:
        load = os.getloadavg()
        cpu_usage = f"{load[0]:.2f}"
    except Exception:
        cpu_usage = "0.00"
    
    # RAM Usage via /proc/meminfo
    ram_pct = 0
    try:
        with open('/proc/meminfo', 'r') as f:
            mem = {line.split(':')[0]: int(line.split(':')[1].strip().split()[0]) for line in f}
        total = mem.get('MemTotal', 1)
        avail = mem.get('MemAvailable', mem.get('MemFree', 0))
        ram_pct = int(((total - avail) / total) * 100)
    except Exception:
        pass
        
    # Disk
    disk = storage_info()
    return {"cpu": cpu_usage, "ram": ram_pct, "disk": int(disk['pct']) if disk else 0}

def _is_trusted_proxy(addr):
    try:
        ip = ipaddress.ip_address(str(addr or ""))
    except ValueError:
        return False
    return any(ip in net for net in TRUSTED_PROXY_NETWORKS)


def _valid_ip(value):
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return ""


def get_client_ip():
    """Client IP for rate limits and bans.

    Proxy headers are honoured only when the direct peer is a trusted proxy, and
    only the values that proxy itself wrote: X-Real-IP, else the right-most
    X-Forwarded-For hop. The left-most hop is client-controlled and must never be
    used, otherwise rotating it bypasses login rate limits.
    """
    remote = request.remote_addr
    if not _is_trusted_proxy(remote):
        return remote
    real_ip = _valid_ip(request.headers.get("X-Real-IP"))
    if real_ip:
        return real_ip
    hops = [h.strip() for h in (request.headers.get("X-Forwarded-For") or "").split(",") if h.strip()]
    if hops:
        last_hop = _valid_ip(hops[-1])
        if last_hop:
            return last_hop
    return remote

def check_rate_limit(ip):
    """Return True if IP is under the failed-login threshold (shared via SQLite)."""
    now = time.time()
    cutoff = now - 300  # last 5 minutes
    conn = database.get_db()
    try:
        conn.execute("DELETE FROM login_attempts WHERE ts <= ?", (cutoff,))
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM login_attempts WHERE ip=? AND ts > ?",
            (ip, cutoff),
        ).fetchone()
        conn.commit()
        count = int(row["c"] if row is not None else 0)
        return count < 5
    finally:
        conn.close()


def add_login_attempt(ip):
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO login_attempts (ip, ts) VALUES (?, ?)",
            (ip, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def clear_login_attempts(ip):
    conn = database.get_db()
    try:
        conn.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
        conn.commit()
    finally:
        conn.close()

def load_bans():
    conn = database.get_db()
    try:
        rows = conn.execute("SELECT ip, expires_at FROM bans").fetchall()
        now = time.time()
        active = []
        expired = []
        for row in rows:
            expires_at = row["expires_at"]
            if expires_at is not None and expires_at > 0 and expires_at <= now:
                expired.append(row["ip"])
            else:
                active.append(row["ip"])
        for ip in expired:
            conn.execute("DELETE FROM bans WHERE ip=?", (ip,))
        if expired:
            conn.commit()
        return active
    finally:
        conn.close()

def is_banned(ip):
    from flask import has_request_context, g
    if has_request_context() and hasattr(g, "_cached_bans") and ip in g._cached_bans:
        return g._cached_bans[ip]
    conn = database.get_db()
    try:
        try:
            row = conn.execute("SELECT ip, expires_at FROM bans WHERE ip=?", (ip,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return False
            logging.error("Database error checking bans: %s", exc)
            raise
        if not row:
            res = False
        else:
            expires_at = row["expires_at"]
            if expires_at is not None and expires_at > 0 and expires_at <= time.time():
                conn.execute("DELETE FROM bans WHERE ip=?", (ip,))
                conn.commit()
                res = False
            else:
                res = True
        if has_request_context():
            if not hasattr(g, "_cached_bans"):
                g._cached_bans = {}
            g._cached_bans[ip] = res
        return res
    finally:
        conn.close()

def ban_ip(ip, ttl_sec=None):
    """Ban an IP. ttl_sec=None uses LOGIN_BAN_TTL_SEC; ttl_sec=0 is permanent."""
    ensure_ban_schema()
    now = time.time()
    if ttl_sec is None:
        ttl_sec = LOGIN_BAN_TTL_SEC
    expires_at = None if ttl_sec == 0 else now + max(1, int(ttl_sec))
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO bans (ip, created_at, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(ip) DO UPDATE SET created_at=excluded.created_at, expires_at=excluded.expires_at",
            (ip, now, expires_at),
        )
        conn.commit()
    finally:
        conn.close()

def unban_ip(ip):
    conn = database.get_db()
    try:
        conn.execute("DELETE FROM bans WHERE ip=?", (ip,))
        conn.commit()
    finally:
        conn.close()

def log_login_attempt(ip, username, success):
    try:
        conn = database.get_db()
        try:
            conn.execute(
                "INSERT INTO login_logs (ip, username, success, ts) VALUES (?, ?, ?, ?)",
                (ip, username, 1 if success else 0, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logging.error("Konnte Login-Versuch nicht speichern: %s", exc)

def get_login_logs(limit=200):
    conn = database.get_db()
    try:
        rows = conn.execute("SELECT * FROM login_logs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def load_users():
    from flask import has_request_context, g
    if has_request_context() and hasattr(g, "_cached_users") and g._cached_users is not None:
        return g._cached_users
    users = {}
    for row in database.list_users():
        role = row.get("role") or "user"
        users[row["username"]] = {
            "hash": row["password_hash"],
            "active": bool(row.get("active")),
            "role": role,
            "group_id": row.get("group_id"),
            "email": row.get("email") or "",
            "discord_id": row.get("discord_id") or "",
            "discord_username": row.get("discord_username") or "",
            "created_via": row.get("created_via") or "",
            "created_at": row.get("created_at") or 0,
            "register_note": row.get("register_note") or "",
            "rules_accepted_at": float(row.get("rules_accepted_at") or 0),
            "rules_version": row.get("rules_version") or "",
            "terms_accepted_at": float(row.get("terms_accepted_at") or 0),
            "terms_version": row.get("terms_version") or "",
            "last_seen_at": float(row.get("last_seen_at") or 0),
            "last_login_at": float(row.get("last_login_at") or 0),
        }
    if has_request_context():
        g._cached_users = users
    return users

def save_user(username, password_hash, active=True, role='admin', group_id=None, email=None, discord_id=None, discord_username=None, created_via=None):
    database.upsert_user(
        username,
        password_hash,
        role=role,
        active=active,
        group_id=group_id,
        email=email,
        discord_id=discord_id,
        discord_username=discord_username,
        created_via=created_via,
        preserve_discord=True,
    )

def delete_user(username):
    database.delete_user(username)

def ensure_admin_user():
    """Bootstrap admin user from environment variables with atomic operations."""
    users = load_users()
    has_admin = any(data.get("role") == "admin" and data.get("active", True) for data in users.values())
    if has_admin:
        return
    
    # Only bootstrap config user - NEVER use defaults
    if config.VOD_USER and config.VOD_PASS:
        os.makedirs(config.META_DIR, exist_ok=True)
        marker = os.path.join(config.META_DIR, "bootstrap_config_admin.txt")
        lock_file = marker + ".lock"
        
        # Use file locking to prevent race conditions in multi-worker deployments
        try:
            with open(lock_file, "w") as f:
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_EX)  # Exclusive lock
                try:
                    # Double-check after acquiring lock
                    if os.path.exists(marker):
                        return
                    
                    # Create user
                    save_user(config.VOD_USER, generate_password_hash(config.VOD_PASS), role="admin")
                    
                    # Write marker file atomically
                    with tempfile.NamedTemporaryFile(
                        mode='w', 
                        dir=config.META_DIR, 
                        delete=False,
                        encoding='utf-8'
                    ) as tmp:
                        tmp.write(str(time.time()))
                        tmp_path = tmp.name
                    
                    # Atomic rename on POSIX systems
                    os.replace(tmp_path, marker)
                    logging.info(f"Bootstrap: Admin user '{config.VOD_USER}' created.")
                finally:
                    if fcntl:
                        fcntl.flock(f, fcntl.LOCK_UN)
        except Exception as e:
            logging.error(f"Failed to bootstrap admin user: {e}")
            raise

def authenticate_user(username, password):
    """Authenticate user - ONLY hashed passwords accepted, no plaintext fallback."""
    users = load_users()
    active_db_users = [
        u for u, data in users.items() if data.get("active", True)
    ]

    # Prefer database users whenever any are present.
    for u, data in users.items():
        if u.lower() == username.lower():
            if not data.get("active", True):
                logging.warning(f"Login attempt for inactive user: {u}")
                return None

            # Only accept properly hashed passwords
            if check_password_hash(data["hash"], password):
                return data.get("role") or "user"

            # REMOVED: Legacy plaintext password fallback (CRITICAL SECURITY FIX)
            # If password looks like plaintext in database, log security warning
            if isinstance(data.get("hash"), str) and len(data.get("hash", "")) < 50:
                logging.warning(f"SECURITY: Plaintext password detected for user {u}. Database requires migration.")
            return None

    # Optional env-login fallback when enabled or when no active DB users exist.
    allow_env = bool(getattr(config, "VOD_ALLOW_ENV_LOGIN", False))
    if (allow_env or not active_db_users) and config.VOD_USER and config.VOD_PASS:
        if username.lower() == config.VOD_USER.lower() and password == config.VOD_PASS:
            return "admin"

    return None


LOGIN_TEMPLATE = load_html_template("login.html")
MAINTENANCE_TEMPLATE = load_html_template("maintenance.html")
AGB_TEMPLATE = load_html_template("agb.html")
DATENSCHUTZ_TEMPLATE = load_html_template("datenschutz.html")
ADMIN_TEMPLATE = load_html_template("admin.html")
ADMIN_TRAFFIC_TEMPLATE = load_html_template("admin_traffic.html")
ADMIN_USERS_TEMPLATE = load_html_template("admin_users.html")
STORAGE_TEMPLATE = load_html_template("storage.html")
MAIL_ADMIN_TEMPLATE = None
STALK_ADMIN_TEMPLATE = None
STALK_DETAIL_TEMPLATE = None
CLIP_EDITOR_TEMPLATE = load_html_template("clip_editor.html")
DISCORD_TOOL_TEMPLATE = None
INDEX_TEMPLATE = load_html_template("index.html")
CHANNELS_TEMPLATE = load_html_template("channels.html")
STREAMER_TEMPLATE = load_html_template("streamer.html")
VOD_TEMPLATE = load_html_template("vod.html")
HLS_TEMPLATE = load_html_template("hls.html")
LIVE_TEMPLATE = load_html_template("live.html")
LIVE_OBS_TEMPLATE = load_html_template("live_obs.html")
CHAT_OBS_TEMPLATE = load_html_template("chat_obs.html")
FILES_TEMPLATE = load_html_template("files.html")
MOBILE_INDEX_TEMPLATE = load_html_template("mobile_index.html")
MOBILE_STREAMER_TEMPLATE = load_html_template("mobile_streamer.html")
MOBILE_PLAYER_TEMPLATE = load_html_template("mobile_player.html")
CHAT_EXPORT_TEMPLATE = load_html_template("chat_export.html")
SEARCH_TEMPLATE = load_html_template("search.html")

# Mythic+ web UI removed.

def _get_current_user_context() -> dict | None:
    """Request-level cache for user status, role, group, and permissions in flask.g.

    Guarantees that user status and permissions are queried at most ONCE per HTTP request
    from SQLite, avoiding repetitive queries in folder and VOD loops, while ensuring
    immediate session invalidation when active=0 or permissions change.
    """
    if not has_request_context():
        return None

    if hasattr(g, "_user_context"):
        return g._user_context

    if not session.get("logged_in"):
        g._user_context = None
        return None

    username = session.get("username")
    if not username:
        g._user_context = None
        return None

    user_row = database.get_user(username)
    if not user_row:
        allow_env = bool(getattr(config, "VOD_ALLOW_ENV_LOGIN", False))
        active_db_users = [u for u in database.list_users() if u.get("active", True)]
        if (allow_env or not active_db_users) and config.VOD_USER:
            if str(username).lower() == str(config.VOD_USER).lower():
                user_row = {
                    "username": config.VOD_USER,
                    "role": "admin",
                    "active": True,
                    "group_id": 1,
                }
            else:
                g._user_context = None
                return None
        else:
            g._user_context = None
            return None

    is_active = bool(user_row.get("active", True))
    if not is_active:
        g._user_context = {
            "username": username,
            "role": user_row.get("role") or "viewer",
            "active": False,
            "group_id": user_row.get("group_id"),
            "permissions": {},
            "disabled_channels": set(),
            "access_all_channels": False,
        }
        return g._user_context

    perms = database.get_user_permissions(username)
    disabled = perms.get("disabled_channels") or []
    disabled_set = {str(c).strip().lower() for c in disabled if str(c).strip()}
    access_all = bool(perms.get("access_all_channels", True))
    role = user_row.get("role") or ("admin" if perms.get("can_manage_users") else "viewer")

    g._user_context = {
        "username": username,
        "role": role,
        "active": True,
        "group_id": user_row.get("group_id"),
        "permissions": perms,
        "disabled_channels": disabled_set,
        "access_all_channels": access_all,
    }
    return g._user_context


def resolve_session_account(username=None):
    """Return {username, role} when the session user still exists and is active."""
    if username is None:
        ctx = _get_current_user_context()
        if ctx is not None:
            if not ctx.get("active", True):
                return None
            return {"username": ctx["username"], "role": ctx["role"]}

    name = str(username if username is not None else session.get("username") or "").strip()
    if not name:
        return None
    user_row = database.get_user(name)
    if user_row:
        if not user_row.get("active", True):
            return None
        return {"username": user_row["username"], "role": user_row.get("role") or "viewer"}

    allow_env = bool(getattr(config, "VOD_ALLOW_ENV_LOGIN", False))
    active_db_users = [u for u in database.list_users() if u.get("active", True)]
    if (allow_env or not active_db_users) and config.VOD_USER:
        if name.lower() == str(config.VOD_USER).lower():
            return {"username": config.VOD_USER, "role": "admin"}
    return None


def session_is_expired():
    """True when the login cookie must be dropped.

    Default: no clock timeout. Sessions stay valid until logout, account
    deletion, or deactivation. Optional VOD_SESSION_MAX_AGE_SEC restores a cap.
    """
    if not session.get("logged_in"):
        return False
    ctx = _get_current_user_context()
    if ctx is None or not ctx.get("active", True):
        return True
    if SESSION_MAX_AGE_SEC <= 0:
        return False
    now = time.time()
    login_time = session.get("login_time")
    if login_time is not None and (now - float(login_time)) > SESSION_MAX_AGE_SEC:
        return True
    last_active = session.get("last_active", login_time)
    if last_active is not None and (now - float(last_active)) > SESSION_MAX_AGE_SEC:
        return True
    return False


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in") or session_is_expired():
            if session.get("logged_in"):
                session.clear()
            return redirect(url_for("login", next=request.path))
        return func(*args, **kwargs)
    return wrapper


def absolute_public_url(path):
    """Absolute URL for links that leave the browser session (share links, OAuth callbacks)."""
    path = str(path or "")
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if path and not path.startswith("/"):
        path = "/" + path
    base = (PUBLIC_BASE or "").rstrip("/")
    if base:
        return f"{base}{path}"
    try:
        proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip().lower()
        host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
    except RuntimeError:
        return path
    if proto not in ("http", "https"):
        proto = "https"
    if not host:
        return path
    return f"{proto}://{host}{path}"


def hls_playlist_response(body, *, live=False):
    """Playlist MIME without charset; some HLS players reject mpegurl; charset=utf-8."""
    resp = Response(body, 200)
    resp.headers["Content-Type"] = "application/vnd.apple.mpegurl"
    return apply_hls_delivery_headers(resp, live=live)


def hls_segment_mimetype(filename: str) -> str:
    """MIME for HLS media segments (Python's mimetypes maps .ts wrongly)."""
    name = str(filename or "").lower()
    if name.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if name.endswith(".m4s") or name.endswith(".mp4"):
        return "video/mp4"
    if name.endswith(".aac"):
        return "audio/aac"
    # MPEG-TS — must NOT use mimetypes.guess_type (.ts → linguist text).
    return "video/mp2t"


def rewrite_hls_playlist_for_client(lines, segment_url_for):
    """Rewrite segment URIs and ensure mobile-safe HLS tags.

    Guarantees ``EXT-X-VERSION`` (>=3 for MPEG-TS) and ``EXT-X-INDEPENDENT-SEGMENTS``
    so Safari / hls.js treat every segment as independently decodable when possible.
    """
    raw = [str(line) for line in (lines or [])]
    has_version = any(line.startswith("#EXT-X-VERSION:") for line in raw)
    has_independent = any(line.startswith("#EXT-X-INDEPENDENT-SEGMENTS") for line in raw)
    output = []
    inserted_meta = False
    for line in raw:
        stripped = line.strip()
        if not inserted_meta and stripped.startswith("#EXTM3U"):
            output.append(line if line.endswith("\n") else line + "\n")
            if not has_version:
                output.append("#EXT-X-VERSION:3\n")
            if not has_independent:
                output.append("#EXT-X-INDEPENDENT-SEGMENTS\n")
            inserted_meta = True
            continue
        if stripped.startswith("#") or not stripped:
            output.append(line if line.endswith("\n") else line + "\n")
            continue
        seg = stripped
        url = segment_url_for(seg)
        output.append(url + "\n")
    if not inserted_meta and raw:
        # Playlist missing EXTM3U — still emit version hints at top.
        prefix = []
        if not has_version:
            prefix.append("#EXT-X-VERSION:3\n")
        if not has_independent:
            prefix.append("#EXT-X-INDEPENDENT-SEGMENTS\n")
        output = prefix + output
    return output


def apply_hls_delivery_headers(resp, *, live: bool = False, immutable: bool = False):
    """Byte-range + cache headers for HLS playlist/segment responses."""
    resp.headers["Accept-Ranges"] = "bytes"
    if live:
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    elif immutable:
        resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    else:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


def is_ios_client():
    ua = (request.headers.get("User-Agent") or "").lower()
    return "iphone" in ua or "ipad" in ua or "ipod" in ua


def media_login_required(func):
    """Session-only auth for media endpoints: 401 instead of a login redirect,
    because players fetch these URLs directly and cannot follow an HTML login."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if request.method == "OPTIONS":
            return Response(status=204)
        if session.get("logged_in") and not session_is_expired():
            return func(*args, **kwargs)
        if session.get("logged_in"):
            session.clear()
        return Response("Unauthorized", 401, mimetype="text/plain")
    return wrapper


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in") or session_is_expired():
            if session.get("logged_in"):
                session.clear()
            return redirect(url_for("login", next=request.path))
        if session.get("user_role") != "admin":
            return abort(403)
        return func(*args, **kwargs)
    return wrapper


def current_user_can_manage_users() -> bool:
    try:
        if not session.get("logged_in"):
            return False
        ctx = _get_current_user_context()
        if not ctx or not ctx.get("active", True):
            return False
        if ctx.get("role") == "admin":
            return True
        perms = ctx.get("permissions") or {}
        return bool(perms.get("can_manage_users"))
    except (RuntimeError, Exception):
        return False


def manage_users_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in") or session_is_expired():
            if session.get("logged_in"):
                session.clear()
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        if not current_user_can_manage_users():
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Forbidden"}), 403
            return abort(403)
        return func(*args, **kwargs)
    return wrapper


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def load_json(path, default):
    """Read a JSON state file; a missing file yields ``default``.

    A corrupt file is moved aside as ``<path>.corrupt-<ts>`` before returning
    ``default``. Callers save the value back later, so returning the default while
    leaving the file in place would silently overwrite the only copy of the data.
    Read errors other than "missing" are raised for the same reason.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError as exc:  # json.JSONDecodeError, UnicodeDecodeError
        backup = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, backup)
        except OSError as move_exc:
            logging.error("Corrupt JSON %s (%s); could not move it aside: %s", path, exc, move_exc)
            raise
        logging.error("Corrupt JSON %s (%s); moved to %s", path, exc, backup)
        return default


def save_json(path, data):
    ensure_dir(os.path.dirname(path))
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=os.path.dirname(path))
    try:
        if is_meta_path(path):
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        if is_meta_path(path):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def is_meta_path(path):
    try:
        meta_dir = os.path.realpath(config.META_DIR)
        target = os.path.realpath(path)
        return os.path.commonpath([meta_dir, target]) == meta_dir
    except (OSError, ValueError):
        return False


MAIL_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAIL_LOCAL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._+-]{0,62}[a-z0-9])?$")
MAIL_ADDRESS_RE = re.compile(r"^(?=.{3,254}$)[a-z0-9._%+-]{1,64}@[a-z0-9.-]+\.[a-z]{2,63}$")


def clean_mail_domain(domain):
    domain = str(domain or "").strip().strip(".").lower()
    if not MAIL_DOMAIN_RE.match(domain):
        return ""
    return domain


def get_mail_domain():
    domain = clean_mail_domain(MAIL_DOMAIN)
    if domain:
        return domain

    if PUBLIC_BASE:
        parsed = urllib.parse.urlparse(PUBLIC_BASE)
        domain = clean_mail_domain(parsed.hostname)
        if domain:
            return domain

    try:
        host = request.host.split(":", 1)[0]
    except RuntimeError:
        host = ""
    return clean_mail_domain(host)


def clean_external_url(value):
    value = str(value or "").strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return value.rstrip("/")


def get_mail_status():
    domain = get_mail_domain()
    api_base = clean_external_url(MAILCOW_API_URL)
    webmail_url = clean_external_url(MAIL_WEBMAIL_URL)
    if not webmail_url and api_base:
        webmail_url = f"{api_base}/SOGo"

    missing = []
    if not domain:
        missing.append("MAIL_DOMAIN")
    if not api_base:
        missing.append("MAILCOW_API_URL")
    if not MAILCOW_API_KEY:
        missing.append("MAILCOW_API_KEY")

    return {
        "domain": domain,
        "api_base": api_base,
        "api_configured": bool(api_base and MAILCOW_API_KEY),
        "ready": bool(domain and api_base and MAILCOW_API_KEY),
        "webmail_url": webmail_url,
        "missing": missing,
        "default_quota_mb": max(MAIL_DEFAULT_QUOTA_MB, 128),
    }


def normalize_mail_address(value, domain):
    value = str(value or "").strip().lower()
    domain = clean_mail_domain(domain)
    if not value or not domain:
        return None
    if "@" in value:
        local_part, address_domain = value.rsplit("@", 1)
        if clean_mail_domain(address_domain) != domain:
            return None
    else:
        local_part = value
    if ".." in local_part or not MAIL_LOCAL_RE.match(local_part):
        return None
    address = f"{local_part}@{domain}"
    if not MAIL_ADDRESS_RE.match(address):
        return None
    return address


def normalize_alias_address(value, domain):
    value = str(value or "").strip().lower()
    domain = clean_mail_domain(domain)
    if value in {"*", "catchall", f"@{domain}"} and domain:
        return f"@{domain}"
    return normalize_mail_address(value, domain)


def validate_mail_destinations(value):
    destinations = []
    for raw in str(value or "").replace(";", ",").split(","):
        address = raw.strip().lower()
        if not address:
            continue
        if not MAIL_ADDRESS_RE.match(address):
            return None
        destinations.append(address)
    return destinations or None


def parse_mail_quota(value):
    try:
        quota = int(value)
    except (TypeError, ValueError):
        quota = MAIL_DEFAULT_QUOTA_MB
    return max(128, min(quota, 102400))


def mail_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def mailcow_request(method, endpoint, payload=None):
    api_base = clean_external_url(MAILCOW_API_URL)
    if not api_base or not MAILCOW_API_KEY:
        raise RuntimeError("Mailcow API ist nicht konfiguriert.")

    url = f"{api_base}/api/v1/{endpoint.lstrip('/')}"
    data = None
    headers = {
        "Accept": "application/json",
        "X-API-Key": MAILCOW_API_KEY,
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=MAIL_API_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Mailcow API Fehler {exc.code}: {body or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Mailcow API nicht erreichbar: {exc.reason}") from exc

    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Mailcow API lieferte keine gueltige JSON-Antwort.") from exc


def mailcow_response_ok(response):
    items = response if isinstance(response, list) else [response]
    if not items:
        return False
    has_success = False
    for item in items:
        if not isinstance(item, dict):
            continue
        msg_type = str(item.get("type", "")).lower()
        if msg_type == "success":
            has_success = True
        if msg_type in {"danger", "error"}:
            return False
    return has_success


def mailcow_response_message(response):
    items = response if isinstance(response, list) else [response]
    parts = []
    for item in items:
        if isinstance(item, dict):
            msg = item.get("msg")
        else:
            msg = item
        if isinstance(msg, list):
            parts.extend(str(part) for part in msg if not isinstance(part, (dict, list)))
        elif msg:
            parts.append(str(msg))
    return " / ".join(parts)[:500] or "Mailcow API hat geantwortet."


def mailcow_queue_summary():
    summary = {
        "available": False,
        "count": 0,
        "deferred": 0,
        "sample": "",
        "error": "",
    }
    try:
        response = mailcow_request("GET", "get/mailq/all")
    except RuntimeError as exc:
        summary["error"] = str(exc)
        return summary

    if not isinstance(response, list):
        summary["error"] = mailcow_response_message(response)
        return summary

    summary["available"] = True
    summary["count"] = len(response)
    for item in response:
        if not isinstance(item, dict):
            continue
        if str(item.get("queue_name", "")).lower() == "deferred":
            summary["deferred"] += 1
        if not summary["sample"]:
            recipients = item.get("recipients") or []
            recipient = recipients[0] if recipients else ""
            sender = item.get("sender") or ""
            if sender or recipient:
                summary["sample"] = f"{sender} -> {recipient}".strip(" ->")
    return summary


def send_test_mail_via_submission(sender, password, recipient):
    sender = str(sender or "").strip().lower()
    password = str(password or "")
    recipient = str(recipient or "").strip().lower()
    if not MAIL_ADDRESS_RE.match(sender) or not MAIL_ADDRESS_RE.match(recipient):
        raise RuntimeError("Ungueltige Testmail-Adresse.")
    if not password:
        raise RuntimeError("Mailbox-Passwort ist erforderlich.")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = "StreamVault Mail-Test"
    message.set_content(
        "Das ist eine Testmail aus dem StreamVault Admin-Panel.\n\n"
        "Wenn diese Nachricht extern nicht ankommt, pruefe die Mailqueue "
        "und outbound TCP/25 oder konfiguriere einen SMTP-Relayhost."
    )

    try:
        with smtplib.SMTP(MAIL_SUBMISSION_HOST, MAIL_SUBMISSION_PORT, timeout=MAIL_API_TIMEOUT_SEC) as smtp:
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls(context=ssl._create_unverified_context())
                smtp.ehlo()
            smtp.login(sender, password)
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError("SMTP Login fehlgeschlagen. Pruefe Mailbox-Passwort.") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise RuntimeError(f"SMTP Testmail fehlgeschlagen: {exc}") from exc
    return True


def outbound_smtp_status():
    targets = (
        ("Gmail MX", "aspmx.l.google.com"),
        ("IONOS MX", "mx01.ionos.de"),
    )
    for label, host in targets:
        try:
            with socket.create_connection((host, 25), timeout=MAIL_SMTP_CHECK_TIMEOUT_SEC):
                return {
                    "open": True,
                    "label": label,
                    "host": host,
                    "message": f"Outbound TCP/25 erreichbar ({label}).",
                }
        except OSError as exc:
            last_error = str(exc)
    return {
        "open": False,
        "label": "Port 25",
        "host": targets[0][1],
        "message": f"Outbound TCP/25 blockiert oder nicht erreichbar: {last_error}",
    }


def mailcow_list_mailboxes(domain):
    domain = clean_mail_domain(domain)
    response = mailcow_request("GET", "get/mailbox/all")
    if isinstance(response, dict) and str(response.get("type", "")).lower() in {"danger", "error"}:
        raise RuntimeError(mailcow_response_message(response))
    entries = []
    for item in response if isinstance(response, list) else []:
        if not isinstance(item, dict):
            continue
        username = str(item.get("username") or item.get("email") or "").strip().lower()
        item_domain = clean_mail_domain(item.get("domain"))
        local_part = str(item.get("local_part") or "").strip().lower()
        if not username and item_domain and local_part:
            username = f"{local_part}@{item_domain}"
        if not username.endswith(f"@{domain}"):
            continue
        entries.append({
            "username": username,
            "local_part": username.rsplit("@", 1)[0],
            "name": item.get("name") or item.get("display_name") or "",
            "active": mail_bool(item.get("active")),
            "quota_mb": item.get("quota") or item.get("quota_mb") or "",
            "quota_used": item.get("percent_in_use") or item.get("quota_used") or "",
            "created": item.get("created") or "",
            "modified": item.get("modified") or "",
        })
    return sorted(entries, key=lambda entry: entry["username"])


def mailcow_list_aliases(domain):
    domain = clean_mail_domain(domain)
    response = mailcow_request("GET", "get/alias/all")
    if isinstance(response, dict) and str(response.get("type", "")).lower() in {"danger", "error"}:
        raise RuntimeError(mailcow_response_message(response))
    entries = []
    for item in response if isinstance(response, list) else []:
        if not isinstance(item, dict):
            continue
        address = str(item.get("address") or "").strip().lower()
        item_domain = clean_mail_domain(item.get("domain"))
        if item_domain != domain and address != f"@{domain}" and not address.endswith(f"@{domain}"):
            continue
        entries.append({
            "id": str(item.get("id") or ""),
            "address": address,
            "goto": item.get("goto") or "",
            "active": mail_bool(item.get("active")),
            "sogo_visible": mail_bool(item.get("sogo_visible")),
            "created": item.get("created") or "",
            "modified": item.get("modified") or "",
        })
    return sorted(entries, key=lambda entry: entry["address"])


def is_mobile():
    ua = request.headers.get('User-Agent', '').lower()
    return "mobile" in ua or "android" in ua or "iphone" in ua


def mobile_live_enabled():
    if current_user_is_admin():
        return True
    return bool(getattr(config, "MOBILE_LIVE_ENABLED", False))


def maintenance_mode_enabled(scope: str = "global"):
    from core import site_maintenance

    return site_maintenance.is_enabled(scope)


def current_user_bypasses_maintenance() -> bool:
    if current_user_is_admin():
        return True
    try:
        from core import site_maintenance

        username = session.get("username")
        if username and site_maintenance.is_user_exempt(username, current_user_role()):
            return True
    except Exception:
        pass
    return False


def site_maintenance_admin_context():
    from core import site_maintenance

    state = site_maintenance.load_state()
    updated_at = float(state.get("updated_at") or 0)
    scopes = state.get("scopes") or {}
    bypass_users = state.get("bypass_users") or []
    return {
        "enabled": bool(state.get("enabled")),
        "scopes": scopes,
        "bypass_users": bypass_users,
        "bypass_users_str": ", ".join(bypass_users),
        "updated_by": state.get("updated_by") or "",
        "updated_at": updated_at,
        "updated_label": (
            time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(updated_at))
            if updated_at > 0
            else ""
        ),
        "discord_channel_configured": bool(
            os.environ.get("DISCORD_COMMUNITY_CHANNEL_ID", "").strip()
            or os.environ.get("DISCORD_HELP_CHANNEL_ID", "").strip()
            or (
                os.path.isfile(os.path.join(config.META_DIR, "discord_community_channel_id"))
            )
        ),
    }


MOBILE_LIVE_DISABLED_MSG = lazy_gettext(
    "Mobile live is temporarily disabled. Please use the desktop version or the installed app."
)


def redirect_mobile_live_disabled():
    """Send phones/tablets away from Live watch until mobile Live is repaired."""
    return redirect(url_for("index", channels_msg=str(MOBILE_LIVE_DISABLED_MSG)))


def get_user_color(username):
    colors = [
        '#ef5350', '#ec407a', '#ab47bc', '#7e57c2', '#5c6bc0', '#42a5f5',
        '#29b6f6', '#26c6da', '#26a69a', '#66bb6a', '#9ccc65', '#d4e157',
        '#ffee58', '#ffca28', '#ffa726', '#ff7043', '#8d6e63', '#bdbdbd'
    ]
    hash_val = 0
    for char in username:
        hash_val = ord(char) + ((hash_val << 5) - hash_val)
    return colors[abs(hash_val) % len(colors)]

def normalize_channel_name(name):
    if not name:
        return None
    channel = str(name).strip().lower()
    channel = channel.removeprefix("@")
    channel = re.sub(r"[^a-z0-9_]", "", channel)
    return channel or None


def archive_name_for(streamer):
    """Map a source channel to its shared archive folder name."""
    name = normalize_channel_name(streamer) or str(streamer or "").strip().lower()
    if not name:
        return None
    conf = database.get_channel_config(name)
    if not conf:
        for alt in (f"{name}_", name.rstrip("_")):
            if alt != name:
                conf = database.get_channel_config(alt)
                if conf:
                    break
    if conf:
        return normalize_channel_name(conf.get("archive_name")) or name
    return name


def archive_display_name(streamer):
    """Human label for UI."""
    slug = archive_name_for(streamer) or normalize_channel_name(streamer) or str(streamer or "").strip()
    if not slug:
        return ""
    return slug.rstrip("_") or slug


def streamer_dir(slug):
    """On-disk archive directory for a source or archive name."""
    archive = archive_name_for(slug)
    if not archive:
        return None
    p = safe_join(BASE_DIR, archive)
    if p and os.path.isdir(p):
        return p
    files_dir = getattr(config, "FILES_DIR", None) or os.path.join(BASE_DIR, "files")
    p_files = safe_join(files_dir, archive)
    if p_files and os.path.isdir(p_files):
        return p_files
    return p


def _path_is_under(path, root):
    path_value = str(path or "").strip()
    root_value = str(root or "").strip()
    if not path_value or not root_value:
        return False
    try:
        path_real = os.path.realpath(path_value)
        root_real = os.path.realpath(root_value)
        common = os.path.commonpath([path_real, root_real])
        return os.path.normcase(common) == os.path.normcase(root_real)
    except (OSError, ValueError):
        return False


def media_accel_path(streamer, filename, *, clip=False, file_path=None):
    """X-Accel path using the on-disk archive slug (aliases map correctly)."""
    archive = archive_name_for(streamer) or normalize_channel_name(streamer) or str(streamer or "").strip()
    name = os.path.basename(str(filename or ""))
    if not archive or not name:
        return None
    if file_path is None:
        file_path = resolve_media_path(streamer, filename, "clip" if clip else "vod")
    prefix = X_ACCEL_PREFIX.rstrip("/")
    if file_path and _path_is_under(file_path, getattr(config, "FILES_DIR", "")):
        prefix = FILES_ACCEL_PREFIX.rstrip("/")
    if clip:
        return f"{prefix}/{urllib.parse.quote(archive)}/{CLIP_SUBDIR}/{urllib.parse.quote(name)}"
    return f"{prefix}/{urllib.parse.quote(archive)}/{urllib.parse.quote(name)}"


def vod_cache_accel_path(streamer, filename, cached_path=None):
    """X-Accel path for a local playback cache file."""
    _ = cached_path
    archive = archive_name_for(streamer) or str(streamer or "").strip()
    name = os.path.basename(str(filename or ""))
    if not archive or not name:
        return None
    prefix = VOD_CACHE_ACCEL_PREFIX.rstrip("/")
    return f"{prefix}/{urllib.parse.quote(archive)}/{urllib.parse.quote(name)}"


def suggest_archive_name(source_name, explicit=None):
    """Reuse an archive only on exact name/archive match or explicit archive_name."""
    explicit_norm = normalize_channel_name(explicit)
    if explicit_norm:
        return explicit_norm
    source_name = normalize_channel_name(source_name) or source_name
    if not source_name:
        return None
    try:
        configs = database.list_channel_configs()
    except Exception:
        configs = []
    for conf in configs:
        name = normalize_channel_name(conf.get("name")) or str(conf.get("name") or "")
        archive = normalize_channel_name(conf.get("archive_name")) or name
        # Exact reuse only — no fuzzy stem/prefix merging of unrelated channels.
        if name == source_name or archive == source_name:
            return archive
    return source_name


def is_youtube_host(hostname):
    host = str(hostname or "").lower()
    return host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")

def parse_channel_source_input(raw_value, archive_name=None):
    raw = str(raw_value or "").strip()
    if not raw:
        return None

    candidate_url = raw
    if raw.startswith("@"):
        candidate_url = f"https://www.youtube.com/{raw}"
    elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw) and "youtube." in raw.lower():
        candidate_url = f"https://{raw}"

    parsed = urllib.parse.urlsplit(candidate_url)
    if is_youtube_host(parsed.hostname):
        path = parsed.path.strip("/")
        source_label = ""
        if path.startswith("@"):
            source_label = path.split("/", 1)[0].lstrip("@")
        elif path.startswith("channel/") or path.startswith("c/") or path.startswith("user/"):
            source_label = path.split("/", 1)[1].split("/", 1)[0]
        elif parsed.hostname == "youtu.be" and path:
            source_label = path.split("/", 1)[0]
        elif path == "watch":
            query = urllib.parse.parse_qs(parsed.query)
            source_label = (query.get("v") or ["youtube"])[0]
        source_name = normalize_channel_name(source_label) or "youtube"
        archive = suggest_archive_name(source_name, archive_name)
        return {
            "name": f"{source_name}_youtube",
            "platform": "youtube",
            "source_url": urllib.parse.urlunsplit((parsed.scheme or "https", parsed.netloc, parsed.path.rstrip("/") or "/", parsed.query, "")),
            "archive_name": archive,
        }

    channel = normalize_channel_name(raw)
    if not channel:
        return None
    return {
        "name": channel,
        "platform": "twitch",
        "source_url": "",
        "archive_name": suggest_archive_name(channel, archive_name) or channel,
    }

def tail_matching_lines(file_path, limit, query=None):
    dq = deque(maxlen=limit)
    q = query.lower() if query else None
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if q and q not in line.lower():
                    continue
                dq.append(line.rstrip("\n"))
    except Exception:
        return []
    return list(dq)


def load_channels():
    return database.get_channels(mode='record')

def add_channel_db(channel, platform="twitch", source_url="", archive_name=None, record=1, live=0, notify=1):
    platform = "youtube" if str(platform or "").lower() in ("youtube", "yt") else "twitch"
    archive_name = normalize_channel_name(archive_name) or channel
    rec_val = 1 if record else 0
    live_val = 1 if live else 0
    not_val = 1 if notify else 0
    conn = database.get_db()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO channels (name, record, live, chat, platform, source_url, archive_name, notify)
            VALUES (?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (channel, rec_val, live_val, platform, source_url or "", archive_name, not_val),
        )
        conn.execute(
            """
            UPDATE channels
            SET record=?, platform=?, source_url=?, archive_name=?, notify=?
            WHERE name=?
            """,
            (rec_val, platform, source_url or "", archive_name, not_val, channel),
        )
        conn.commit()
    finally:
        conn.close()

def remove_channel_db(channel):
    conn = database.get_db()
    try:
        conn.execute(
            "UPDATE channels SET record=0, live=0, chat=0 WHERE name=?",
            (channel,),
        )
        conn.execute("DELETE FROM channels WHERE name=?", (channel,))
        conn.commit()
    finally:
        conn.close()

def active_recording_archives():
    """Archive/source names that are actively recorded — used to scope chat search."""
    names = set()
    try:
        configs = database.list_channel_configs(mode="record")
    except Exception:
        configs = []
    for conf in configs:
        name = str(conf.get("name") or "").strip()
        archive = str(conf.get("archive_name") or name).strip()
        if name:
            names.add(name)
        if archive:
            names.add(archive)
    return names

def load_live_channels():
    return database.get_channels(mode='live')


def add_live_channel(channel):
    conn = database.get_db()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO channels (name, record, live, chat, platform, archive_name)
            VALUES (?, 0, 1, 0, 'twitch', ?)
            """,
            (channel, channel),
        )
        conn.execute("UPDATE channels SET live=1 WHERE name=?", (channel,))
        conn.commit()
    finally:
        conn.close()

def remove_live_channel(channel):
    conn = database.get_db()
    try:
        conn.execute("UPDATE channels SET live=0 WHERE name=?", (channel,))
        conn.execute("DELETE FROM channels WHERE name=? AND record=0 AND live=0 AND chat=0", (channel,))
        conn.commit()
    finally:
        conn.close()

def clear_live_hls(except_channel=None):
    if not os.path.isdir(config.LIVE_HLS_ROOT):
        return
    for name in os.listdir(config.LIVE_HLS_ROOT):
        if except_channel and name == except_channel:
            continue
        live_dir = os.path.join(config.LIVE_HLS_ROOT, name)
        if not os.path.isdir(live_dir):
            continue
        for entry in os.listdir(live_dir):
            if entry.endswith(".ts") or entry == "index.m3u8":
                try:
                    os.remove(os.path.join(live_dir, entry))
                except OSError:
                    pass


def format_bytes(num_bytes):
    try:
        value = float(num_bytes)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 1024 ** 3:
        return f"{value / (1024 ** 3):.1f} GB"
    if value >= 1024 ** 2:
        return f"{value / (1024 ** 2):.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{int(value)} B"


def storage_info():
    try:
        # shutil.disk_usage works on every platform; free is the space available
        # to unprivileged users (statvfs f_bavail on POSIX).
        usage = shutil.disk_usage(BASE_DIR)
        total = usage.total
        free = usage.free
        used = total - free
        pct = (used / total) * 100 if total else 0
        return {
            "total": total,
            "free": free,
            "used": used,
            "pct": pct,
            "total_str": format_bytes(total),
            "free_str": format_bytes(free),
        }
    except OSError as exc:
        logging.warning("Storage info unavailable for %s: %s", BASE_DIR, exc)
        return {
            "total": 0,
            "free": 0,
            "used": 0,
            "pct": 0,
            "total_str": "N/A",
            "free_str": "N/A",
        }


def task_status_counts():
    """Best-effort full task queue status counts (not limited to recent rows)."""
    counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    try:
        conn = database.get_db()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
            for row in rows:
                status = str(row["status"] or "pending")
                counts[status] = int(row["n"] or 0)
        finally:
            conn.close()
    except Exception as exc:
        logging.debug("task_status_counts failed: %s", exc)
        summary = task_queue_summary(limit=500)
        return dict(summary.get("counts") or counts)
    return counts

def count_active_recording_locks():
    """Best-effort count of `.recording*.lock` files under the archive root."""
    count = 0
    try:
        for name in os.listdir(BASE_DIR):
            if name.startswith("."):
                continue
            folder = os.path.join(BASE_DIR, name)
            if not os.path.isdir(folder):
                continue
            try:
                entries = os.listdir(folder)
            except OSError:
                continue
            if any(
                entry == ".recording.lock"
                or (entry.startswith(".recording.") and entry.endswith(".lock"))
                for entry in entries
            ):
                count += 1
    except OSError:
        pass
    return count


def stalk_heartbeat_age_sec():
    return None


def _run_status_command(cmd):
    """Run a short status command; return (stdout, returncode, error)."""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", None, str(exc)[:300]
    return result.stdout, result.returncode, result.stderr.strip()[:300]


def collect_service_status(expected):
    """Service health from supervisor, falling back to systemd units.

    ``ok`` is None when neither supervisor nor the streamvault-* systemd
    units can be queried, so an unknown state never reads as healthy.
    """
    services = {name: {"state": "unknown"} for name in sorted(expected)}
    out, rc, sup_err = _run_status_command(["supervisorctl", "status"])
    # supervisorctl exits 3 when some programs are not RUNNING.
    if rc in (0, 3) and out.strip():
        services, missing, services_ok = parse_supervisor_status(out, expected)
        status = {"ok": services_ok, "manager": "supervisor", "programs": services}
        if missing:
            status["error"] = "missing supervisor programs: " + ", ".join(missing)
        return status

    units = [systemd_unit_name(name) for name in sorted(expected)]
    out, rc, sysd_err = _run_status_command(
        ["systemctl", "show", "-p", SYSTEMD_SHOW_PROPERTIES, *units]
    )
    if rc == 0:
        services, missing, services_ok = parse_systemctl_show(out, expected)
        if len(missing) < len(services):
            status = {"ok": services_ok, "manager": "systemd", "programs": services}
            if missing:
                status["error"] = "missing systemd units: " + ", ".join(
                    systemd_unit_name(name) for name in missing
                )
            return status
        sysd_err = "no streamvault-* systemd units loaded"
    error = "supervisorctl: " + (sup_err or "no output") + "; systemctl: " + (sysd_err or "no output")
    logging.warning("Health check cannot determine service state: %s", error)
    return {"ok": None, "error": error[:600], "programs": services}


def collect_health_status():
    checks = {}
    status_ok = True
    disk_used_pct = None
    disk_free_gb = None

    try:
        conn = database.get_db()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        checks["database"] = {"ok": True}
    except Exception as exc:
        status_ok = False
        checks["database"] = {"ok": False, "error": str(exc)}

    for name, path in {
        "base_storage": BASE_DIR,
        "live_hls": config.LIVE_HLS_ROOT,
    }.items():
        try:
            usage = shutil.disk_usage(path if os.path.exists(path) else os.path.dirname(path))
            used_pct = ((usage.total - usage.free) / usage.total) * 100 if usage.total else 0
            free_gb = round(usage.free / (1024 ** 3), 2)
            checks[name] = {
                "ok": usage.free > 1024 ** 3,
                "free_gb": free_gb,
                "used_pct": round(used_pct, 1),
            }
            if name == "base_storage":
                disk_used_pct = round(used_pct, 1)
                disk_free_gb = free_gb
            if not checks[name]["ok"]:
                status_ok = False
        except Exception as exc:
            status_ok = False
            checks[name] = {"ok": False, "error": str(exc)}

    checks["services"] = collect_service_status({"webui", "recorder", "worker"})
    if checks["services"].get("ok") is False:
        status_ok = False

    task_counts = task_status_counts()
    recording_locks = count_active_recording_locks()
    try:
        channels_record = len(database.get_channels("record"))
    except Exception:
        channels_record = 0

    checks["tasks"] = {
        "ok": True,
        "pending": int(task_counts.get("pending") or 0),
        "running": int(task_counts.get("running") or 0),
        "failed": int(task_counts.get("failed") or 0),
        "completed": int(task_counts.get("completed") or 0),
    }
    checks["recording_locks"] = {"ok": True, "count": int(recording_locks)}
    checks["channels_record"] = {"ok": True, "count": int(channels_record)}

    return {
        "status": "ok" if status_ok else "degraded",
        "checks": checks,
        "disk_used_pct": disk_used_pct,
        "disk_free_gb": disk_free_gb,
        "tasks": checks["tasks"],
        "recording_locks": recording_locks,
        "channels_record": channels_record,
        "ts": int(time.time()),
    }


def render_prometheus_metrics():
    health = collect_health_status()
    tasks = health.get("tasks") or {}
    lines = [
        "# HELP streamrecorder_disk_used_percent Archive disk used percent",
        "# TYPE streamrecorder_disk_used_percent gauge",
        f"streamrecorder_disk_used_percent {float(health.get('disk_used_pct') or 0)}",
        "# HELP streamrecorder_disk_free_gb Archive disk free gigabytes",
        "# TYPE streamrecorder_disk_free_gb gauge",
        f"streamrecorder_disk_free_gb {float(health.get('disk_free_gb') or 0)}",
        "# HELP streamrecorder_tasks_pending Pending background tasks",
        "# TYPE streamrecorder_tasks_pending gauge",
        f"streamrecorder_tasks_pending {int(tasks.get('pending') or 0)}",
        "# HELP streamrecorder_tasks_running Running background tasks",
        "# TYPE streamrecorder_tasks_running gauge",
        f"streamrecorder_tasks_running {int(tasks.get('running') or 0)}",
        "# HELP streamrecorder_tasks_failed Failed background tasks",
        "# TYPE streamrecorder_tasks_failed gauge",
        f"streamrecorder_tasks_failed {int(tasks.get('failed') or 0)}",
        "# HELP streamrecorder_channels_record Channels with record=1",
        "# TYPE streamrecorder_channels_record gauge",
        f"streamrecorder_channels_record {int(health.get('channels_record') or 0)}",
        "# HELP streamrecorder_recording_locks Active .recording.lock count",
        "# TYPE streamrecorder_recording_locks gauge",
        f"streamrecorder_recording_locks {int(health.get('recording_locks') or 0)}",
    ]
    lines.append("")
    return "\n".join(lines)


def eventsub_is_live(login):
    """Cached EventSub live flag for future recorder/livebot use (None if unknown)."""
    return twitch_eventsub.is_live(login)


def resolve_transcribe_backend(preferred=None):
    """Select whisper backend; fall back to openai-whisper if faster_whisper missing."""
    preferred = (
        preferred
        if preferred is not None
        else os.environ.get("TRANSCRIBE_BACKEND", TRANSCRIBE_BACKEND)
    )
    preferred = str(preferred or "whisper").strip().lower() or "whisper"
    if preferred == "faster_whisper":
        try:
            from faster_whisper import WhisperModel  # noqa: F401
            return "faster_whisper"
        except ImportError:
            logging.warning(
                "TRANSCRIBE_BACKEND=faster_whisper but faster-whisper is not installed; "
                "falling back to openai-whisper"
            )
            return "whisper"
    return "whisper"


def load_transcribe_model(backend=None):
    backend = resolve_transcribe_backend(backend)
    if backend == "faster_whisper":
        from faster_whisper import WhisperModel
        return backend, WhisperModel(TRANSCRIBE_MODEL, device="cpu", compute_type="int8")
    import whisper
    return backend, whisper.load_model(TRANSCRIBE_MODEL)


def run_transcribe_audio(backend, model, audio_path):
    if backend == "faster_whisper":
        segments, _info = model.transcribe(audio_path, language=TRANSCRIBE_LANGUAGE)
        return "".join(seg.text for seg in segments).strip()
    result = model.transcribe(audio_path, language=TRANSCRIBE_LANGUAGE, task="transcribe")
    return str(result.get("text") or "").strip()


def favorites_path():
    return os.path.join(config.META_DIR, "favorites.json")


def load_favorites():
    data = load_json(favorites_path(), [])
    return set(data) if isinstance(data, list) else set()


def save_favorites(favorites):
    save_json(favorites_path(), sorted(favorites))


def history_path():
    return os.path.join(config.META_DIR, "history.json")


def load_history():
    data = load_json(history_path(), [])
    return data if isinstance(data, list) else []


def profiles_path():
    return os.path.join(config.META_DIR, "profiles.json")


def load_profiles():
    return load_json(profiles_path(), {})


def save_profiles(data):
    save_json(profiles_path(), data)


def is_system_backup_slug(name):
    if not name:
        return False
    val = str(name).strip().lower().replace("-", "").replace("_", "")
    return val in ("systembackups", "systembackup", "backups", "backup")


def find_local_streamer_banner_file(streamer):
    """Returns (directory, filename) if a local banner file exists, else None."""
    archive_key = archive_name_for(streamer) or streamer
    slugs = []
    for s in [archive_key, streamer]:
        norm = normalize_channel_name(s) or str(s or "").strip()
        if norm and norm not in slugs:
            slugs.append(norm)
        if s and s not in slugs:
            slugs.append(s)

    exts = [".jpg", ".jpeg", ".png", ".webp", ".gif"]
    banners_dir = os.path.join(config.META_DIR, "banners")

    # 1. Check in .meta/banners/<slug>.<ext>
    if os.path.isdir(banners_dir):
        for slug in slugs:
            s_clean = secure_filename(slug)
            if not s_clean:
                continue
            for ext in exts:
                fname = f"{s_clean}{ext}"
                if os.path.isfile(os.path.join(banners_dir, fname)):
                    return banners_dir, fname

    # 2. Check in data/<slug>/banner.<ext>
    for slug in slugs:
        s_dir = os.path.join(config.BASE_DIR, slug)
        if os.path.isdir(s_dir):
            for ext in exts:
                for base in ("banner", "_banner", "header"):
                    fname = f"{base}{ext}"
                    if os.path.isfile(os.path.join(s_dir, fname)):
                        return s_dir, fname
    return None


def get_streamer_archive_fallback_banner(streamer):
    """Finds the thumbnail of the latest recorded VOD/clip for a streamer or folder."""
    try:
        items = collect_streamer_media_items(streamer)
    except Exception:
        items = []
    if not items:
        return None
    def _ts(it):
        try:
            return float(it.get("recorded_ts") or it.get("mtime") or 0)
        except Exception:
            return 0.0
    sorted_items = sorted(items, key=_ts, reverse=True)
    top = sorted_items[0]
    try:
        thumb_dir, thumb_path, _ = resolve_thumb_paths(streamer, top["name"])
        thumb_v = int(os.path.getmtime(thumb_path)) if thumb_path and os.path.exists(thumb_path) else int(top.get("mtime") or 0)
        return url_for("thumb_view", streamer=streamer, filename=top["name"], v=thumb_v)
    except Exception:
        return None


def resolve_streamer_banner(streamer, archive_key, p_data, stream_info, twitch_live, online, files=None):
    """
    Multi-Tier Banner Resolution Engine (Works for all channels & non-stream folders):
    1. Local custom file in .meta/banners/<slug>.* or data/<slug>/banner.*
    2. Explicit custom banner URL in profiles.json
    3. Official Twitch profile banner
    4. Active live broadcast preview (ONLY if live or recording)
    5. Dynamic archive fallback (latest VOD/media thumbnail from folder)
    6. None -> Native CSS cosmic nebula gradient
    """
    # Tier 1: Local custom file
    local = find_local_streamer_banner_file(streamer)
    if local:
        bdir, bname = local
        try:
            v = int(os.path.getmtime(os.path.join(bdir, bname)))
        except Exception:
            v = 1
        clean_slug = secure_filename(archive_key) or secure_filename(streamer)
        return url_for("streamer_banner_file", streamer=clean_slug, v=v), gettext("Custom file")

    # Tier 2: Explicit custom banner URL
    custom_url = (p_data.get("custom_banner_url") or "").strip()
    if custom_url:
        return custom_url, "Custom URL"

    # Tier 3: Twitch profile banner
    twitch_banner = (p_data.get("banner_url") or "").strip()
    if twitch_banner:
        return twitch_banner, gettext("Twitch profile banner")

    # Tier 4: Active live broadcast snapshot (ONLY when live/recording!)
    if twitch_live or online:
        thumb = (stream_info.get("thumbnail_url") or "").strip()
        if thumb:
            if "{width}" in thumb:
                thumb = thumb.format(width=1280, height=720)
            return thumb, "Live Stream"

    # Tier 5: Dynamic Archive VOD Backdrop (Offline fallback)
    if files and len(files) > 0 and files[0].get("thumb_url"):
        return files[0]["thumb_url"], gettext("Archive recording (latest VOD)")

    archive_fallback = get_streamer_archive_fallback_banner(streamer)
    if archive_fallback:
        return archive_fallback, gettext("Archive recording (latest VOD)")

    # Tier 6: None -> Pure CSS Cosmic Gradient
    return None, "Nebula Cosmic Gradient"

def find_local_streamer_avatar_file(streamer):
    """Returns (directory, filename) if a local avatar file exists, else None."""
    archive_key = archive_name_for(streamer) or streamer
    slugs = []
    for s in [archive_key, streamer]:
        norm = normalize_channel_name(s) or str(s or "").strip()
        if norm and norm not in slugs:
            slugs.append(norm)
        if s and s not in slugs:
            slugs.append(s)

    exts = [".jpg", ".jpeg", ".png", ".webp", ".gif"]
    avatars_dir = os.path.join(config.META_DIR, "avatars")

    # 1. Check in .meta/avatars/<slug>.<ext>
    if os.path.isdir(avatars_dir):
        for slug in slugs:
            s_clean = secure_filename(slug)
            if not s_clean:
                continue
            for ext in exts:
                fname = f"{s_clean}{ext}"
                if os.path.isfile(os.path.join(avatars_dir, fname)):
                    return avatars_dir, fname

    # 2. Check in data/<slug>/avatar.<ext>
    for slug in slugs:
        s_dir = os.path.join(config.BASE_DIR, slug)
        if os.path.isdir(s_dir):
            for ext in exts:
                for base in ("avatar", "_avatar", "profile", "logo"):
                    fname = f"{base}{ext}"
                    if os.path.isfile(os.path.join(s_dir, fname)):
                        return s_dir, fname
    return None


def resolve_streamer_avatar(streamer, archive_key=None, p_data=None):
    """
    Multi-Tier Avatar Resolution Engine:
    1. Local custom file in .meta/avatars/<slug>.* or data/<slug>/avatar.*
    2. Explicit custom avatar URL in profiles.json ('custom_avatar_url')
    3. Official Twitch profile image in profiles.json ('url')
    4. Fallback None
    """
    if p_data is None:
        try:
            profiles = load_profiles()
            p_data = profiles.get(archive_key or streamer) or profiles.get(streamer) or {}
        except Exception:
            p_data = {}

    # Tier 1: Local custom file
    local = find_local_streamer_avatar_file(streamer)
    if local:
        adir, aname = local
        try:
            v = int(os.path.getmtime(os.path.join(adir, aname)))
        except Exception:
            v = 1
        clean_slug = secure_filename(archive_key or streamer) or secure_filename(streamer)
        try:
            return url_for("streamer_avatar_file", streamer=clean_slug, v=v), gettext("Custom file")
        except RuntimeError:
            return f"/avatar/{clean_slug}?v={v}", gettext("Custom file")

    # Tier 2: Explicit custom avatar URL
    custom_url = (p_data.get("custom_avatar_url") or "").strip()
    if custom_url:
        return custom_url, "Custom URL"

    # Tier 3: Twitch profile image
    twitch_img = (p_data.get("url") or "").strip()
    if twitch_img:
        return twitch_img, gettext("Twitch profile")

    return None, gettext("Default")



def list_all_streamer_folders():
    """Returns a list of all local streamer/archive folders, excluding system backups."""
    folders = set()
    base_ok = os.path.isdir(BASE_DIR)
    if base_ok:
        for name in os.listdir(BASE_DIR):
            if name.startswith(".") or name in ("__pycache__", "streamrecorder.db", "tube.db"):
                continue
            if is_system_backup_slug(name):
                continue
            if os.path.isdir(os.path.join(BASE_DIR, name)):
                folders.add(name)
    try:
        for c in database.list_channel_configs():
            name = str(c.get("name") or "").strip()
            arch = str(c.get("archive_name") or "").strip()
            if name and not is_system_backup_slug(name):
                folders.add(name)
            if arch and not is_system_backup_slug(arch):
                folders.add(arch)
    except Exception:
        pass

    rows = []
    profiles = load_profiles()
    configs = {}
    try:
        configs = {c.get("name"): c for c in database.list_channel_configs()}
    except Exception:
        pass

    for f in sorted(folders, key=lambda s: s.lower()):
        p_data = profiles.get(f) or {}
        local_file = find_local_streamer_banner_file(f)
        custom_url = p_data.get("custom_banner_url")
        is_stream = f in configs or any(c.get("archive_name") == f for c in configs.values())

        banner_preview, source_type = resolve_streamer_banner(
            streamer=f,
            archive_key=archive_name_for(f) or f,
            p_data=p_data,
            stream_info={},
            twitch_live=False,
            online=False,
        )

        rows.append({
            "name": f,
            "display_name": archive_display_name(f),
            "is_stream_channel": is_stream,
            "channel_type": gettext("Stream channel") if is_stream else gettext("Archive / media folder"),
            "banner_preview": banner_preview,
            "source_type": source_type,
            "has_custom": bool(local_file or custom_url),
            "custom_url": custom_url or "",
        })
    return rows


def system_backups_dir():
    """Where streamrecorder_*.tar.gz backups are listed; a dot-dir so it is never an archive."""
    return os.environ.get("BACKUP_DIR") or os.path.join(config.META_DIR, "backups")


def list_system_backups():
    """Lists local system backups."""
    backups_dir = system_backups_dir()
    items = {}

    if os.path.isdir(backups_dir):
        for fname in os.listdir(backups_dir):
            if fname.startswith("streamrecorder_") and fname.endswith(".tar.gz"):
                fpath = os.path.join(backups_dir, fname)
                try:
                    st = os.stat(fpath)
                    items[fname] = {
                        "filename": fname,
                        "size": st.st_size,
                        "size_str": f"{st.st_size / (1024 * 1024):.1f} MB",
                        "mtime": st.st_mtime,
                        "date_str": time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(st.st_mtime)),
                        "on_disk": True,
                    }
                except OSError:
                    pass

    return sorted(items.values(), key=lambda x: x["mtime"], reverse=True)


def get_twitch_profile_and_banner(channel):
    query = {
        "query": """
        query ($login: String!) {
            user(login: $login) {
                profileImageURL(width: 300)
                bannerImageURL
            }
        }
        """,
        "variables": {"login": channel}
    }
    try:
        req = urllib.request.Request(
            "https://gql.twitch.tv/gql",
            data=json.dumps(query).encode('utf-8'),
            headers={
                "Client-ID": GQL_CLIENT_ID,
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data and "data" in data and data["data"].get("user"):
                u = data["data"]["user"]
                return u.get("profileImageURL"), u.get("bannerImageURL")
    except Exception:
        pass
    return None, None

def get_twitch_profile_image(channel):
    url, _ = get_twitch_profile_and_banner(channel)
    return url

TWITCH_USER_ID_CACHE = {}
TWITCH_TOKEN_CACHE = {"token": "", "expires_at": 0}
TWITCH_BADGE_ASSET_CACHE = {}
TWITCH_BADGE_IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
}


def sniff_twitch_badge_image_ext(content_type, payload):
    """Map Content-Type / magic bytes to a badge file extension."""
    ctype = str(content_type or "").split(";")[0].strip().lower()
    ext = TWITCH_BADGE_IMAGE_EXTENSIONS.get(ctype)
    if ext:
        return ext
    data = payload or b""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    # Twitch CDN sometimes serves valid PNGs as binary/octet-stream.
    if ctype in ("application/octet-stream", "binary/octet-stream", ""):
        return ".png" if data else ""
    return ""

def get_twitch_user_id(channel):
    login = str(channel or "").strip().lower()
    if not login:
        return None
    cached = TWITCH_USER_ID_CACHE.get(login)
    now = time.time()
    if cached and now - cached.get("ts", 0) < TWITCH_BADGE_CACHE_TTL_SEC:
        return cached.get("id")
    query = {
        "query": """
        query ($login: String!) {
            user(login: $login) {
                id
            }
        }
        """,
        "variables": {"login": login},
    }
    try:
        req = urllib.request.Request(
            "https://gql.twitch.tv/gql",
            data=json.dumps(query).encode("utf-8"),
            headers={
                "Client-ID": GQL_CLIENT_ID,
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            user_id = data.get("data", {}).get("user", {}).get("id") if isinstance(data, dict) else None
            if user_id:
                TWITCH_USER_ID_CACHE[login] = {"id": str(user_id), "ts": now}
                return str(user_id)
    except Exception:
        pass
    TWITCH_USER_ID_CACHE[login] = {"id": None, "ts": now}
    return None

def get_twitch_app_access_token():
    if TWITCH_APP_ACCESS_TOKEN:
        return TWITCH_APP_ACCESS_TOKEN
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return ""
    now = time.time()
    if TWITCH_TOKEN_CACHE.get("token") and now < TWITCH_TOKEN_CACHE.get("expires_at", 0):
        return TWITCH_TOKEN_CACHE["token"]
    try:
        form = urllib.parse.urlencode({
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://id.twitch.tv/oauth2/token",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        token = str(data.get("access_token", "") or "")
        expires_in = int(data.get("expires_in", 0) or 0)
        if token:
            TWITCH_TOKEN_CACHE["token"] = token
            TWITCH_TOKEN_CACHE["expires_at"] = now + max(300, expires_in - 300)
            return token
    except Exception as exc:
        logging.warning("Could not obtain Twitch app access token: %s", exc)
    return ""

def twitch_helix_json(url):
    token = get_twitch_app_access_token()
    if not token or not TWITCH_CLIENT_ID:
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Client-Id": TWITCH_CLIENT_ID,
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logging.info("Twitch badge fetch failed: %s", exc)
    return None

def is_twitch_badge_image_url(url):
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except Exception:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc == "static-cdn.jtvnw.net"
        and parsed.path.startswith("/badges/")
    )

def twitch_public_json(url):
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=TWITCH_BADGE_DOWNLOAD_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logging.info("Twitch public badge fetch failed: %s", exc)
    return None

def flatten_twitch_badge_sets(payload):
    badges = []
    if not isinstance(payload, dict):
        return badges
    for badge_set in payload.get("data", []) or []:
        if not isinstance(badge_set, dict):
            continue
        set_id = str(badge_set.get("set_id", "") or "").strip()
        if not CHAT_BADGE_PART_RE.match(set_id):
            continue
        for version in badge_set.get("versions", []) or []:
            if not isinstance(version, dict):
                continue
            badge_id = str(version.get("id", "") or "").strip()
            if not CHAT_BADGE_PART_RE.match(badge_id):
                continue
            image_url_1x = str(version.get("image_url_1x", "") or "")
            image_url_2x = str(version.get("image_url_2x", "") or "")
            image_url_4x = str(version.get("image_url_4x", "") or "")
            if not any(is_twitch_badge_image_url(url) for url in (image_url_1x, image_url_2x, image_url_4x)):
                continue
            badges.append({
                "set_id": set_id,
                "id": badge_id,
                "image_url_1x": image_url_1x if is_twitch_badge_image_url(image_url_1x) else "",
                "image_url_2x": image_url_2x if is_twitch_badge_image_url(image_url_2x) else "",
                "image_url_4x": image_url_4x if is_twitch_badge_image_url(image_url_4x) else "",
                "title": str(version.get("title", "") or "")[:120],
                "description": str(version.get("description", "") or "")[:240],
            })
    return badges

def flatten_public_twitch_badge_sets(payload):
    badges = []
    if not isinstance(payload, dict):
        return badges
    badge_sets = payload.get("badge_sets")
    if not isinstance(badge_sets, dict):
        return badges
    for set_id, set_data in badge_sets.items():
        set_id = str(set_id or "").strip()
        if not CHAT_BADGE_PART_RE.match(set_id):
            continue
        versions = set_data.get("versions") if isinstance(set_data, dict) else None
        if not isinstance(versions, dict):
            continue
        for badge_id, version in versions.items():
            badge_id = str(badge_id or "").strip()
            if not CHAT_BADGE_PART_RE.match(badge_id) or not isinstance(version, dict):
                continue
            image_url_1x = str(version.get("image_url_1x", "") or "")
            image_url_2x = str(version.get("image_url_2x", "") or "")
            image_url_4x = str(version.get("image_url_4x", "") or "")
            if not any(is_twitch_badge_image_url(url) for url in (image_url_1x, image_url_2x, image_url_4x)):
                continue
            badges.append({
                "set_id": set_id,
                "id": badge_id,
                "image_url_1x": image_url_1x if is_twitch_badge_image_url(image_url_1x) else "",
                "image_url_2x": image_url_2x if is_twitch_badge_image_url(image_url_2x) else "",
                "image_url_4x": image_url_4x if is_twitch_badge_image_url(image_url_4x) else "",
                "title": str(version.get("title", "") or "")[:120],
                "description": str(version.get("description", "") or "")[:240],
            })
    return badges

def twitch_badge_cache_key(streamer):
    key = str(streamer or "global").strip().lower()
    key = re.sub(r"[^a-z0-9_-]+", "_", key)[:80]
    return key or "global"

def twitch_badge_index_path(key):
    return os.path.join(TWITCH_BADGE_CACHE_DIR, f"{key}.json")

def read_twitch_badge_disk_cache(key, fresh_only=True):
    path = twitch_badge_index_path(key)
    data = load_json(path, None)
    if not isinstance(data, dict) or not isinstance(data.get("badges"), list):
        return None
    if TWITCH_BADGE_PUBLIC_FALLBACK and not data.get("available") and not data.get("badges"):
        return None
    fetched_at = float(data.get("fetched_at", 0) or 0)
    if fresh_only and time.time() - fetched_at > TWITCH_BADGE_CACHE_TTL_SEC:
        return None
    return data

def write_twitch_badge_disk_cache(key, data):
    payload = {
        "available": bool(data.get("available")),
        "badges": data.get("badges", []),
        "fetched_at": time.time(),
    }
    path = twitch_badge_index_path(key)
    save_json(path, payload)
    # Badge indexes are not secrets; keep them readable for the webui service user
    # even when a root-run sync wrote them under META_DIR (default 0600).
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return payload

def local_twitch_badge_url(set_id, badge_id, filename):
    return (
        "/chat/badge-assets/"
        + urllib.parse.quote(set_id)
        + "/"
        + urllib.parse.quote(badge_id)
        + "/"
        + urllib.parse.quote(filename)
    )

def find_local_twitch_badge_url(set_id, badge_id, scale):
    asset_dir = os.path.join(TWITCH_BADGE_ASSET_DIR, set_id, badge_id)
    for ext in TWITCH_BADGE_IMAGE_EXTENSIONS.values():
        filename = f"{scale}{ext}"
        path = os.path.join(asset_dir, filename)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return local_twitch_badge_url(set_id, badge_id, filename)
    return ""

def download_twitch_badge_image(url, set_id, badge_id, scale):
    if not is_twitch_badge_image_url(url):
        return ""
    existing = find_local_twitch_badge_url(set_id, badge_id, scale)
    if existing:
        return existing
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=TWITCH_BADGE_DOWNLOAD_TIMEOUT_SEC) as resp:
            content_type = str(resp.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            payload = resp.read(TWITCH_BADGE_IMAGE_MAX_BYTES + 1)
        if len(payload) > TWITCH_BADGE_IMAGE_MAX_BYTES or not payload:
            return ""
        ext = sniff_twitch_badge_image_ext(content_type, payload)
        if not ext:
            return ""
        asset_dir = os.path.join(TWITCH_BADGE_ASSET_DIR, set_id, badge_id)
        os.makedirs(asset_dir, exist_ok=True)
        filename = f"{scale}{ext}"
        tmp_path = os.path.join(asset_dir, f".{filename}.tmp")
        final_path = os.path.join(asset_dir, filename)
        with open(tmp_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, final_path)
        return local_twitch_badge_url(set_id, badge_id, filename)
    except Exception as exc:
        logging.info("Twitch badge image download failed: %s", exc)
    return ""

def localize_twitch_badges(badges, *, eager_download=False, download_all=False, local_only=False):
    """Resolve badge image URLs to local assets and/or official Twitch CDN URLs.

    - eager_download + download_all: download every badge image (library sync)
    - eager_download alone: only core global sets
    - local_only: omit CDN fallbacks (used after a full local sync)
    """
    core_sets = {
        "admin", "bits", "broadcaster", "founder", "moderator", "partner",
        "premium", "staff", "sub-gifter", "subscriber", "turbo", "vip",
    }
    localized = []
    for badge in badges:
        if not isinstance(badge, dict):
            continue
        set_id = str(badge.get("set_id", "") or "").strip()
        badge_id = str(badge.get("id", "") or "").strip()
        if not CHAT_BADGE_PART_RE.match(set_id) or not CHAT_BADGE_PART_RE.match(badge_id):
            continue
        remote = {
            "1x": str(badge.get("image_url_1x", "") or ""),
            "2x": str(badge.get("image_url_2x", "") or ""),
            "4x": str(badge.get("image_url_4x", "") or ""),
        }
        should_download = eager_download and (download_all or set_id in core_sets)
        resolved = {}
        for scale, remote_url in remote.items():
            local_url = find_local_twitch_badge_url(set_id, badge_id, scale)
            if not local_url and should_download and remote_url:
                local_url = download_twitch_badge_image(remote_url, set_id, badge_id, scale)
            if local_url:
                resolved[scale] = local_url
            elif (not local_only) and is_twitch_badge_image_url(remote_url):
                resolved[scale] = remote_url
            else:
                resolved[scale] = ""
        item = {
            "set_id": set_id,
            "id": badge_id,
            "image_url_1x": resolved.get("1x", ""),
            "image_url_2x": resolved.get("2x", ""),
            "image_url_4x": resolved.get("4x", ""),
            "title": str(badge.get("title", "") or "")[:120],
            "description": str(badge.get("description", "") or "")[:240],
        }
        if any(item.get(field) for field in ("image_url_1x", "image_url_2x", "image_url_4x")):
            localized.append(item)
    return localized

def dedupe_twitch_badges(badges):
    deduped = {}
    for badge in badges:
        key = (badge.get("set_id"), badge.get("id"))
        if key[0] and key[1]:
            deduped[key] = badge
    return list(deduped.values())


def merge_twitch_badge_lists(*lists):
    """Later lists override earlier ones for the same set_id/id."""
    merged = {}
    for badges in lists:
        if not isinstance(badges, list):
            continue
        for badge in badges:
            if not isinstance(badge, dict):
                continue
            key = (badge.get("set_id"), badge.get("id"))
            if key[0] and key[1]:
                merged[key] = badge
    return list(merged.values())


def twitch_badge_library_stats():
    global_data = read_twitch_badge_disk_cache(TWITCH_BADGE_GLOBAL_KEY, fresh_only=False) or {}
    badges = global_data.get("badges") if isinstance(global_data, dict) else []
    badge_count = len(badges) if isinstance(badges, list) else 0
    local_urls = 0
    if isinstance(badges, list):
        for badge in badges:
            if not isinstance(badge, dict):
                continue
            for field in ("image_url_1x", "image_url_2x", "image_url_4x"):
                if str(badge.get(field) or "").startswith("/chat/badge-assets/"):
                    local_urls += 1
    asset_files = 0
    asset_bytes = 0
    if os.path.isdir(TWITCH_BADGE_ASSET_DIR):
        for root, _dirs, files in os.walk(TWITCH_BADGE_ASSET_DIR):
            for name in files:
                if name.startswith("."):
                    continue
                path = os.path.join(root, name)
                try:
                    asset_bytes += os.path.getsize(path)
                    asset_files += 1
                except OSError:
                    continue
    return {
        "available": bool(global_data.get("available")) if isinstance(global_data, dict) else False,
        "badge_count": badge_count,
        "local_urls": local_urls,
        "asset_files": asset_files,
        "asset_bytes": asset_bytes,
        "fetched_at": float(global_data.get("fetched_at") or 0) if isinstance(global_data, dict) else 0.0,
        "sync_running": bool(_TWITCH_BADGE_SYNC_STATE.get("running")),
        "sync_error": str(_TWITCH_BADGE_SYNC_STATE.get("error") or ""),
        "last_sync": _TWITCH_BADGE_SYNC_STATE.get("last"),
    }


def sync_twitch_badge_library(logins=None, *, force=False):
    """Download all Twitch global (+ optional channel) badges into local storage."""
    if not _TWITCH_BADGE_SYNC_LOCK.acquire(blocking=False):
        return {"ok": False, "message": gettext("Badge sync is already running."), **twitch_badge_library_stats()}
    _TWITCH_BADGE_SYNC_STATE["running"] = True
    _TWITCH_BADGE_SYNC_STATE["error"] = ""
    started = time.time()
    try:
        stats = twitch_badge_library_stats()
        age = time.time() - float(stats.get("fetched_at") or 0)
        if (
            not force
            and stats.get("available")
            and stats.get("badge_count", 0) > 100
            and stats.get("local_urls", 0) > 100
            and age < TWITCH_BADGE_LIBRARY_TTL_SEC
        ):
            return {"ok": True, "message": gettext("Badge library is up to date."), "skipped": True, **stats}

        ensure_dir(TWITCH_BADGE_CACHE_DIR)
        ensure_dir(TWITCH_BADGE_ASSET_DIR)

        badges = []
        try:
            badges.extend(twitch_api.fetch_gql_chat_badges(""))
        except Exception as exc:
            logging.info("Global GQL badge sync failed: %s", exc)

        login_list = []
        if logins is None:
            try:
                for conf in database.list_channel_configs():
                    login = twitch_api.normalize_login(conf.get("name") or conf.get("archive_name") or "")
                    if login:
                        login_list.append(login)
            except Exception:
                login_list = []
        else:
            login_list = [twitch_api.normalize_login(x) for x in (logins or []) if twitch_api.normalize_login(x)]
        # Unique, stable order
        seen_logins = set()
        unique_logins = []
        for login in login_list:
            if login in seen_logins:
                continue
            seen_logins.add(login)
            unique_logins.append(login)

        for login in unique_logins[:40]:
            try:
                badges.extend(twitch_api.fetch_gql_chat_badges(login))
            except Exception as exc:
                logging.info("Channel GQL badge sync failed for %s: %s", login, exc)

        badges = dedupe_twitch_badges(badges)
        if not badges:
            _TWITCH_BADGE_SYNC_STATE["error"] = gettext("No badges received from Twitch.")
            return {"ok": False, "message": _TWITCH_BADGE_SYNC_STATE["error"], **twitch_badge_library_stats()}

        # Prefetch missing images concurrently for 1x/2x/4x.
        download_jobs = []
        for badge in badges:
            set_id = str(badge.get("set_id") or "").strip()
            badge_id = str(badge.get("id") or "").strip()
            if not set_id or not badge_id:
                continue
            for scale, field in (("1x", "image_url_1x"), ("2x", "image_url_2x"), ("4x", "image_url_4x")):
                remote = str(badge.get(field) or "").strip()
                if not remote or find_local_twitch_badge_url(set_id, badge_id, scale):
                    continue
                download_jobs.append((remote, set_id, badge_id, scale))

        downloaded = 0
        failed = 0
        workers = max(1, min(32, TWITCH_BADGE_SYNC_WORKERS))
        if download_jobs:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(download_twitch_badge_image, remote, set_id, badge_id, scale): (set_id, badge_id, scale)
                    for remote, set_id, badge_id, scale in download_jobs
                }
                for fut in as_completed(futures):
                    try:
                        url = fut.result()
                    except Exception:
                        failed += 1
                        continue
                    if url:
                        downloaded += 1
                    else:
                        failed += 1

        localized = localize_twitch_badges(
            badges,
            eager_download=False,
            download_all=False,
            local_only=True,
        )
        # Keep CDN fallback only for any images that failed to download.
        if len(localized) < len(badges):
            localized = localize_twitch_badges(
                badges,
                eager_download=False,
                download_all=False,
                local_only=False,
            )

        payload = write_twitch_badge_disk_cache(
            TWITCH_BADGE_GLOBAL_KEY,
            {"available": bool(localized), "badges": localized},
        )
        TWITCH_BADGE_ASSET_CACHE[TWITCH_BADGE_GLOBAL_KEY] = {"ts": time.time(), "data": payload}
        # Invalidate channel caches so they pick up the new global library.
        for cache_key in list(TWITCH_BADGE_ASSET_CACHE.keys()):
            if cache_key != TWITCH_BADGE_GLOBAL_KEY:
                TWITCH_BADGE_ASSET_CACHE.pop(cache_key, None)

        elapsed = round(time.time() - started, 2)
        # Mark idle before stats so callers don't see a stuck "running" flag.
        _TWITCH_BADGE_SYNC_STATE["running"] = False
        result = {
            "ok": True,
            "message": gettext("Twitch badge library synchronized."),
            "downloaded": downloaded,
            "failed": failed,
            "jobs": len(download_jobs),
            "channels": len(unique_logins),
            "elapsed_sec": elapsed,
            **twitch_badge_library_stats(),
        }
        _TWITCH_BADGE_SYNC_STATE["last"] = result
        logging.info(
            "Twitch badge library sync: %s badges, %s downloaded, %s failed, %.1fs",
            result.get("badge_count"),
            downloaded,
            failed,
            elapsed,
        )
        return result
    except Exception as exc:
        _TWITCH_BADGE_SYNC_STATE["error"] = str(exc)
        logging.exception("Twitch badge library sync failed")
        return {"ok": False, "message": str(exc), **twitch_badge_library_stats()}
    finally:
        _TWITCH_BADGE_SYNC_STATE["running"] = False
        _TWITCH_BADGE_SYNC_LOCK.release()


def maybe_start_twitch_badge_library_sync():
    """Start a background full badge sync when the local library is missing/stale."""
    if not TWITCH_BADGE_AUTO_SYNC:
        return False
    stats = twitch_badge_library_stats()
    age = time.time() - float(stats.get("fetched_at") or 0)
    needs = (
        not stats.get("available")
        or stats.get("badge_count", 0) < 100
        or stats.get("local_urls", 0) < 100
        or age > TWITCH_BADGE_LIBRARY_TTL_SEC
    )
    if not needs or stats.get("sync_running"):
        return False

    def _runner():
        try:
            sync_twitch_badge_library(force=False)
        except Exception:
            logging.exception("Background Twitch badge sync crashed")

    threading.Thread(target=_runner, name="twitch-badge-sync", daemon=True).start()
    return True


def fetch_twitch_badge_assets(streamer):
    key = str(streamer or "").strip().lower()
    badges = []
    has_helix_auth = bool(TWITCH_APP_ACCESS_TOKEN or (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET))
    source_responded = not has_helix_auth and not TWITCH_BADGE_PUBLIC_FALLBACK

    # Prefer the full local library as the base (1:1 Twitch badges).
    global_lib = read_twitch_badge_disk_cache(TWITCH_BADGE_GLOBAL_KEY, fresh_only=False) or {}
    if isinstance(global_lib, dict) and global_lib.get("badges"):
        source_responded = True
        badges.extend(global_lib.get("badges") or [])

    if has_helix_auth:
        global_data = twitch_helix_json("https://api.twitch.tv/helix/chat/badges/global")
        if global_data:
            source_responded = True
            badges.extend(flatten_twitch_badge_sets(global_data))

    broadcaster_id = None
    if key and (has_helix_auth or TWITCH_BADGE_PUBLIC_FALLBACK):
        broadcaster_id = get_twitch_user_id(key)
    if has_helix_auth and broadcaster_id:
        channel_url = "https://api.twitch.tv/helix/chat/badges?" + urllib.parse.urlencode({"broadcaster_id": broadcaster_id})
        channel_data = twitch_helix_json(channel_url)
        if channel_data:
            source_responded = True
            badges.extend(flatten_twitch_badge_sets(channel_data))

    if TWITCH_BADGE_PUBLIC_FALLBACK:
        public_global = twitch_public_json("https://badges.twitch.tv/v1/badges/global/display?language=en")
        if public_global:
            source_responded = True
            badges.extend(flatten_public_twitch_badge_sets(public_global))
        if broadcaster_id:
            public_channel = twitch_public_json(
                f"https://badges.twitch.tv/v1/badges/channels/{urllib.parse.quote(broadcaster_id)}/display?language=en"
            )
            if public_channel:
                source_responded = True
                badges.extend(flatten_public_twitch_badge_sets(public_channel))

    # Official badge images via public Twitch GQL (badges.twitch.tv is often unavailable).
    if TWITCH_BADGE_PUBLIC_FALLBACK:
        try:
            gql_badges = twitch_api.fetch_gql_chat_badges(key)
        except Exception as exc:
            logging.info("Twitch GQL badge fetch failed: %s", exc)
            gql_badges = []
        if gql_badges:
            source_responded = True
            badges.extend(gql_badges)

    if not source_responded:
        return None
    # Prefer already-local assets; keep CDN only as fallback.
    localized = localize_twitch_badges(dedupe_twitch_badges(badges), eager_download=False, local_only=False)
    if badges and not localized:
        return None
    return {"available": bool(localized), "badges": localized}

def get_twitch_badge_assets(streamer):
    key = twitch_badge_cache_key(streamer)
    now = time.time()
    cached = TWITCH_BADGE_ASSET_CACHE.get(key)
    if cached and now - cached.get("ts", 0) < TWITCH_BADGE_CACHE_TTL_SEC:
        return cached.get("data", {})

    # Always merge the full local global library under channel-specific badges.
    global_lib = read_twitch_badge_disk_cache(TWITCH_BADGE_GLOBAL_KEY, fresh_only=False) or {}
    disk_data = read_twitch_badge_disk_cache(key, fresh_only=True)
    if disk_data and key != TWITCH_BADGE_GLOBAL_KEY:
        merged = {
            "available": True,
            "badges": merge_twitch_badge_lists(global_lib.get("badges") or [], disk_data.get("badges") or []),
            "fetched_at": disk_data.get("fetched_at") or global_lib.get("fetched_at") or 0,
        }
        TWITCH_BADGE_ASSET_CACHE[key] = {"ts": now, "data": merged}
        return merged
    if disk_data:
        TWITCH_BADGE_ASSET_CACHE[key] = {"ts": now, "data": disk_data}
        return disk_data

    stale_data = read_twitch_badge_disk_cache(key, fresh_only=False)
    try:
        result = fetch_twitch_badge_assets(streamer)
        if result and result.get("badges"):
            # For channel keys, persist only channel deltas would be ideal; store merged for simplicity.
            result = write_twitch_badge_disk_cache(key, result)
        elif stale_data:
            result = {
                "available": True,
                "badges": merge_twitch_badge_lists(global_lib.get("badges") or [], stale_data.get("badges") or []),
                "fetched_at": stale_data.get("fetched_at") or global_lib.get("fetched_at") or 0,
            }
        elif global_lib.get("badges"):
            result = {
                "available": True,
                "badges": list(global_lib.get("badges") or []),
                "fetched_at": global_lib.get("fetched_at") or 0,
            }
        elif result is None:
            result = {"available": False, "badges": []}
        else:
            result = write_twitch_badge_disk_cache(key, {"available": False, "badges": []})
    except Exception as exc:
        logging.info("Twitch badge cache refresh failed: %s", exc)
        if stale_data or global_lib.get("badges"):
            result = {
                "available": True,
                "badges": merge_twitch_badge_lists(global_lib.get("badges") or [], (stale_data or {}).get("badges") or []),
                "fetched_at": (stale_data or {}).get("fetched_at") or global_lib.get("fetched_at") or 0,
            }
        else:
            result = {"available": False, "badges": []}

    TWITCH_BADGE_ASSET_CACHE[key] = {"ts": now, "data": result}
    return result


def update_profiles_background(channels_to_update):
    profiles = load_profiles()
    changed = False
    for channel in channels_to_update:
        url, banner = get_twitch_profile_and_banner(channel)
        if url or banner:
            current_entry = dict(profiles.get(channel) or {})
            if url:
                current_entry["url"] = url
            if banner:
                current_entry["banner_url"] = banner
            current_entry["ts"] = int(time.time())
            profiles[channel] = current_entry
            changed = True
        time.sleep(0.5)
    
    if changed:
        current = load_profiles()
        current.update(profiles)
        save_profiles(current)

def add_history_entry(streamer, filename):
    history = load_history()
    history = [h for h in history if not (h.get("streamer") == streamer and h.get("filename") == filename)]
    history.insert(0, {"streamer": streamer, "filename": filename, "ts": int(time.time())})
    save_json(history_path(), history[:HISTORY_LIMIT])


def shares_path():
    return os.path.join(config.META_DIR, "shares.json")


def _shares_lock_path():
    return shares_path() + ".lock"


def load_shares():
    lock_path = _shares_lock_path()
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        if fcntl:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            data = load_json(shares_path(), {})
            return data if isinstance(data, dict) else {}
        finally:
            if fcntl:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def save_shares(shares):
    lock_path = _shares_lock_path()
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        if fcntl:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            save_json(shares_path(), shares if isinstance(shares, dict) else {})
        finally:
            if fcntl:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def resources_path():
    return os.path.join(config.META_DIR, "resources.json")

def load_resources():
    return load_json(resources_path(), {"files": {}, "links": []})

def save_resources(data):
    save_json(resources_path(), data)

def remux_status_path():
    return os.path.join(config.META_DIR, "remux_all_status.json")

def load_remux_status():
    try:
        path = remux_status_path()
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def storage_alert_path():
    return os.path.join(config.META_DIR, "storage_alert.json")

def load_storage_alert_status():
    data = load_json(storage_alert_path(), {})
    return data if isinstance(data, dict) else {}

def is_allowed_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    return True

def is_safe_next(target):
    if not target:
        return False
    target = str(target).strip()
    if any(ch in target for ch in ("\r", "\n", "\t", "\\")):
        return False
    if target.startswith("//") or not target.startswith("/"):
        return False
    try:
        parsed = urllib.parse.urlsplit(target)
    except Exception:
        return False
    if parsed.scheme or parsed.netloc:
        return False
    return True

def create_share(streamer, filename, route, max_views=None):
    """Create a public share. Optional max_views; revoke anytime via revoke_share(token)."""
    if not public_shares_enabled():
        raise RuntimeError("Public shares are disabled")
    shares = load_shares()
    token = secrets.token_urlsafe(16)
    entry = {"streamer": streamer, "filename": filename, "route": route, "created_at": time.time(), "views": 0}
    try:
        if max_views is not None:
            limit = int(max_views)
            if limit > 0:
                entry["max_views"] = limit
    except (TypeError, ValueError):
        pass
    shares[token] = entry
    save_shares(shares)
    return token


def remove_shares_for(streamer, filename, route):
    shares = load_shares()
    updated = {k: v for k, v in shares.items() if not (v.get("streamer") == streamer and v.get("filename") == filename and v.get("route") == route)}
    if updated != shares:
        save_shares(updated)


def resolve_share(token):
    shares = load_shares()
    info = shares.get(token)
    if not info:
        return None
    ttl = int(os.environ.get("VOD_SHARE_TTL_SEC", "604800") or "0")
    if ttl > 0:
        created_at = info.get("created_at")
        if not isinstance(created_at, (int, float)) or created_at <= 0:
            info["created_at"] = time.time()
            shares[token] = info
            save_shares(shares)
        else:
            if time.time() - float(created_at) > ttl:
                shares.pop(token, None)
                save_shares(shares)
                return None
    try:
        max_views = int(info.get("max_views") or 0)
    except (TypeError, ValueError):
        max_views = 0
    if max_views > 0:
        try:
            views = int(info.get("views") or 0)
        except (TypeError, ValueError):
            views = 0
        if views >= max_views:
            shares.pop(token, None)
            save_shares(shares)
            return None
        info["views"] = views + 1
        shares[token] = info
        save_shares(shares)
    streamer = info.get("streamer")
    filename = info.get("filename")
    route = info.get("route")
    return streamer, filename, route


def share_ttl_seconds():
    try:
        return int(os.environ.get("VOD_SHARE_TTL_SEC", "604800") or "0")
    except ValueError:
        return 604800


def prune_expired_shares(shares=None):
    shares = dict(shares if isinstance(shares, dict) else load_shares())
    ttl = share_ttl_seconds()
    if ttl <= 0:
        return shares, False
    now = time.time()
    changed = False
    for token, info in list(shares.items()):
        created_at = info.get("created_at") if isinstance(info, dict) else None
        if not isinstance(created_at, (int, float)) or created_at <= 0:
            if isinstance(info, dict):
                info["created_at"] = now
                shares[token] = info
                changed = True
            continue
        if now - float(created_at) > ttl:
            shares.pop(token, None)
            changed = True
    if changed:
        save_shares(shares)
    return shares, changed


def public_share_link(token):
    path = url_for("public_view", token=token)
    if PUBLIC_BASE:
        return f"{PUBLIC_BASE.rstrip('/')}{path}"
    return url_for("public_view", token=token, _external=True)


def share_admin_rows(limit=100):
    shares, _ = prune_expired_shares()
    ttl = share_ttl_seconds()
    rows = []
    for token, info in shares.items():
        if not isinstance(info, dict):
            continue
        created_at = float(info.get("created_at") or 0)
        expires_at = created_at + ttl if ttl > 0 and created_at > 0 else 0
        rows.append({
            "token": token,
            "streamer": info.get("streamer") or "-",
            "filename": info.get("filename") or "-",
            "route": info.get("route") or "-",
            "created": time.strftime("%d.%m.%Y %H:%M", time.localtime(created_at)) if created_at else "-",
            "expires": time.strftime("%d.%m.%Y %H:%M", time.localtime(expires_at)) if expires_at else "nie",
            "created_at": created_at,
            "age": format_age_short(created_at) if created_at else "-",
            "url": public_share_link(token),
        })
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return rows[:limit]


def revoke_share(token):
    shares = load_shares()
    if token in shares:
        shares.pop(token, None)
        save_shares(shares)
        return True
    return False


def is_safe_resource_filename(filename):
    if not filename or filename in (".", ".."):
        return False
    if filename != os.path.basename(filename):
        return False
    return secure_filename(filename) == filename


DANGEROUS_RESOURCE_EXTENSIONS = {
    ".app", ".bat", ".cmd", ".com", ".cpl", ".dll", ".dmg", ".exe", ".hta",
    ".jar", ".js", ".jse", ".lnk", ".msi", ".msp", ".pif", ".ps1", ".scr",
    ".sh", ".sys", ".vb", ".vbe", ".vbs", ".wsf",
}


def is_allowed_resource_upload(filename):
    if not is_safe_resource_filename(filename):
        return False
    _, ext = os.path.splitext(filename.lower())
    return ext not in DANGEROUS_RESOURCE_EXTENSIONS


def resolve_resource_file_path(filename):
    if not is_allowed_resource_upload(filename):
        return None
    file_path = safe_join(config.FILES_DIR, filename)
    if not file_path or not os.path.isfile(file_path):
        return None
    return file_path


def send_mp4_with_range(file_path, accel_path=None, attachment_name=None):
    if accel_path:
        resp = Response(status=200, mimetype="video/mp4")
        resp.headers["X-Accel-Redirect"] = accel_path
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "private, max-age=86400"
        resp.headers["X-Accel-Buffering"] = "no"
        if attachment_name:
            # Safe ASCII filename
            safe_name = attachment_name.encode('ascii', 'ignore').decode('ascii').replace('"', '')
            # Full unicode filename
            try:
                encoded_name = urllib.parse.quote(attachment_name)
                resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{encoded_name}'
            except Exception:
                resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
        return resp

    directory = os.path.dirname(file_path)
    filename = os.path.basename(file_path)
    # conditional=True aktiviert automatisch Range-Requests (Spulen, Resume)
    rv = send_from_directory(directory, filename, conditional=True, as_attachment=bool(attachment_name))
    rv.headers["Accept-Ranges"] = "bytes"
    # Private VODs must not be stored in shared caches
    rv.headers["Cache-Control"] = "private, max-age=86400"
    return rv


def send_mp4_range_advertising_size(file_path, start, end, total_size):
    """Serve a local byte range while advertising the full object size.

    Growing Drive-cache ``.part`` files are smaller than the VOD. Browsers must
    still see ``Content-Range: bytes start-end/TOTAL`` or they treat the chunk
    as the whole video and restart from 0 after it ends.
    """
    try:
        start = int(start)
        end = int(end)
        total_size = int(total_size)
    except (TypeError, ValueError):
        abort(404)
    if start < 0 or end < start or total_size <= 0:
        abort(416)
    try:
        on_disk = os.path.getsize(file_path)
    except OSError:
        abort(404)
    if start >= on_disk:
        abort(404)
    end = min(end, on_disk - 1, total_size - 1)
    length = end - start + 1

    def generate():
        with open(file_path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{total_size}",
        "Content-Length": str(length),
        "Cache-Control": "private, max-age=3600",
        "X-Accel-Buffering": "no",
    }
    resp = Response(generate(), status=206, headers=headers, mimetype="video/mp4")
    return resp


def remux_media_file(file_path, reencode_audio=False):
    if not os.path.exists(file_path):
        return False, "Datei nicht gefunden"
    lock_path = f"{file_path}.remux.lock"
    for _attempt in range(2):
        try:
            # Prevent concurrent remux
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_path)
            except OSError:
                age = 0
            if age > 2 * 3600:
                try:
                    os.remove(lock_path)
                    continue
                except OSError:
                    pass
            return False, "Remux laeuft bereits"
    else:
        return False, "Remux laeuft bereits"
    try:
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        tmp_path = f"{file_path}.remux.mp4"
        cmd = [
            ffmpeg, "-y", "-i", file_path,
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "copy",
        ]
        if reencode_audio:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                    "-af", "aresample=async=1:first_pts=0"]
        else:
            cmd += ["-c:a", "copy"]
        cmd += [
            "-movflags", BROWSER_MP4_MOVFLAGS,
            "-avoid_negative_ts", "make_zero",
            "-fflags", "+genpts+igndts",
            tmp_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0 or not os.path.exists(tmp_path):
            err = result.stderr.strip() or "FFmpeg Fehler"
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return False, err
        finalize_if_fragmented(tmp_path)
        os.replace(tmp_path, file_path)
        return True, "Remux erfolgreich"
    except subprocess.TimeoutExpired:
        return False, "Remux Timeout"
    except Exception as e:
        try:
            os.remove(tmp_path)
        except (OSError, UnboundLocalError):
            pass
        return False, f"Remux Fehler: {e}"
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _setup_transcribe_threads():
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ.setdefault(key, str(TRANSCRIBE_THREADS))
    try:
        import torch
        torch.set_num_threads(TRANSCRIBE_THREADS)
    except Exception:
        pass


def transcript_path_for_media(file_path):
    base, _ = os.path.splitext(file_path)
    return f"{base}.transcript.txt"


def transcript_status_path(transcript_path):
    return f"{transcript_path}.status.json"


def transcript_lock_path(transcript_path):
    return f"{transcript_path}.lock"


def get_media_duration(file_path):
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        return float(result.stdout.strip())
    except Exception:
        return None


def load_transcript_status(transcript_path):
    status_path = transcript_status_path(transcript_path)
    if not os.path.exists(status_path):
        return None
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def set_transcript_status(transcript_path, state, percent=0, message="", error=None):
    status_path = transcript_status_path(transcript_path)
    data = {
        "state": state,
        "percent": max(0, min(100, int(percent or 0))),
        "message": message or "",
        "error": error,
        "updated": time.time(),
    }
    try:
        os.makedirs(os.path.dirname(status_path) or '.', exist_ok=True)
        tmp = f"{status_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp, status_path)
        except Exception:
            # final attempt: write directly
            with open(status_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
    except Exception as exc:
        logging.warning("Could not write transcript status %s: %s", status_path, exc)
    return data


def remove_stale_transcript_lock(transcript_path, max_age=7200):
    lock_path = transcript_lock_path(transcript_path)
    if not os.path.exists(lock_path):
        return False
    try:
        if time.time() - os.path.getmtime(lock_path) > max_age:
            logging.warning("Stale transcript lock removed: %s", lock_path)
            os.remove(lock_path)
            return True
    except Exception as exc:
        logging.warning("Could not inspect/remove stale transcript lock %s: %s", lock_path, exc)
    return False


def transcribe_media_file(file_path, transcript_path=None):
    if not file_path or not os.path.exists(file_path):
        return False, gettext("File not found")
    if transcript_path is None:
        transcript_path = transcript_path_for_media(file_path)
    if os.path.exists(transcript_path):
        logging.info("Transcript already exists for %s", file_path)
        set_transcript_status(transcript_path, "finished", 100, gettext("Transcript available"))
        return True, gettext("Transcript already exists")

    lock_path = transcript_lock_path(transcript_path)
    if os.path.exists(lock_path):
        if remove_stale_transcript_lock(transcript_path):
            logging.info("Removed stale transcript lock for %s", file_path)
        else:
            return False, gettext("Transcription is already running")

    try:
        # create lock file and write PID so we can cancel the job later
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            try:
                os.write(fd, str(os.getpid()).encode())
            except Exception:
                # best-effort write, not critical
                pass
        finally:
            os.close(fd)
    except FileExistsError:
        return False, gettext("Transcription is already running")

    tmpdir = tempfile.mkdtemp(prefix="transcribe_")
    try:
        set_transcript_status(transcript_path, "running", 5, gettext("Extracting audio..."))
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        audio_path = os.path.join(tmpdir, "audio.wav")
        ffmpeg_cmd = [ffmpeg, "-y", "-i", file_path, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", audio_path]
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0 or not os.path.exists(audio_path):
            err = result.stderr.strip() or gettext("Audio extraction error")
            set_transcript_status(transcript_path, "error", 0, gettext("Audio extraction failed"), err)
            return False, err

        duration = get_media_duration(audio_path) or 0.0
        segment_length = 60 if duration > 180 else 30
        total_segments = max(1, math.ceil(duration / segment_length))

        set_transcript_status(transcript_path, "running", 10, gettext("Loading model..."))
        backend, model = load_transcribe_model()

        transcripts = []
        for index in range(total_segments):
            start = index * segment_length
            if start >= duration:
                break
            segment_file = os.path.join(tmpdir, f"segment_{index}.wav")
            seg_duration = min(segment_length, duration - start)
            seg_cmd = [
                ffmpeg,
                "-y",
                "-ss",
                str(start),
                "-t",
                str(seg_duration),
                "-i",
                audio_path,
                "-f",
                "wav",
                segment_file,
            ]
            seg_result = subprocess.run(seg_cmd, capture_output=True, text=True, timeout=180)
            if seg_result.returncode != 0 or not os.path.exists(segment_file):
                err = seg_result.stderr.strip() or gettext("Error creating the audio segment")
                set_transcript_status(transcript_path, "error", 0, gettext("Segment creation failed"), err)
                return False, err

            text_seg = run_transcribe_audio(backend, model, segment_file)
            transcripts.append(text_seg)

            # Percent calculation: extraction 5% + model load 5% + segments 85% + save 5%
            seg_percent = int(((index + 1) / total_segments) * 85)
            percent = 5 + 5 + seg_percent
            percent = max(5, min(95, percent))
            set_transcript_status(transcript_path, "running", percent, gettext("Transcribing segment %(index)s/%(total)s (%(percent)s%%)", index=index + 1, total=total_segments, percent=percent))

            # Periodically persist a partial transcript so users see incremental output
            try:
                if (index + 1) % 5 == 0 or (index + 1) == total_segments:
                    os.makedirs(os.path.dirname(transcript_path) or '.', exist_ok=True)
                    partial_tmp = f"{transcript_path}.partial.tmp"
                    partial_final = f"{transcript_path}.partial"
                    with open(partial_tmp, 'w', encoding='utf-8') as pf:
                        pf.write("\n\n".join([t for t in transcripts if t]))
                        pf.flush()
                        os.fsync(pf.fileno())
                    try:
                        os.replace(partial_tmp, partial_final)
                    except Exception:
                        # best-effort
                        with open(partial_final, 'w', encoding='utf-8') as pf:
                            pf.write("\n\n".join([t for t in transcripts if t]))
            except Exception:
                pass

        transcript_text = "\n\n".join([t for t in transcripts if t])
        if not transcript_text:
            set_transcript_status(transcript_path, "error", 0, gettext("No transcript output received"))
            return False, gettext("No transcript output received")

        set_transcript_status(transcript_path, "running", 95, gettext("Saving transcript..."))
        os.makedirs(os.path.dirname(transcript_path) or '.', exist_ok=True)
        tmp_final = f"{transcript_path}.tmp"
        try:
            with open(tmp_final, "w", encoding="utf-8") as f:
                f.write(transcript_text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_final, transcript_path)
            # remove any partial file
            try:
                partial = f"{transcript_path}.partial"
                if os.path.exists(partial):
                    os.remove(partial)
            except Exception:
                pass
            set_transcript_status(transcript_path, "finished", 100, gettext("Transcript finished"))
            logging.info("Transcription finished: %s", transcript_path)
            return True, gettext("Transcript created")
        except Exception as exc:
            set_transcript_status(transcript_path, "error", 0, gettext("Saving transcript failed"), str(exc))
            return False, gettext("Saving transcript failed: %(error)s", error=exc)
    except subprocess.TimeoutExpired:
        err = gettext("Transcription timed out")
        set_transcript_status(transcript_path, "error", 0, err)
        return False, err
    except Exception as exc:
        err = str(exc)
        set_transcript_status(transcript_path, "error", 0, gettext("Transcription failed"), err)
        logging.error("Transcription failed for %s: %s", file_path, exc)
        return False, gettext("Transcription failed: %(error)s", error=exc)
    finally:
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except OSError:
            pass
        try:
            shutil.rmtree(tmpdir)
        except OSError:
            pass


def load_transcript(file_path):
    transcript_path = transcript_path_for_media(file_path)
    if os.path.exists(transcript_path):
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return None
    return None


def hls_status(streamer, filename):
    _, playlist, lock_path = hls_paths(streamer, filename)
    if not playlist or not lock_path:
        return "missing"
    if os.path.isfile(playlist) and not os.path.isfile(lock_path):
        return "ready"
    if os.path.isfile(lock_path):
        return "building"
    return "missing"


def thumb_paths(streamer, filename):
    if os.path.basename(filename) != filename:
        return None, None, None
    base = os.path.splitext(filename)[0]
    thumb_dir = safe_join(config.THUMB_ROOT, streamer)
    if not thumb_dir:
        return None, None, None
    thumb_path = os.path.join(thumb_dir, f"{base}.jpg")
    lock_path = os.path.join(thumb_dir, f"{base}.lock")
    return thumb_dir, thumb_path, lock_path


def resolve_thumb_paths(streamer, filename, slugs=None):
    """Prefer an existing thumb under the archive slug or related local aliases."""
    primary_dir, primary_path, primary_lock = thumb_paths(streamer, filename)
    if primary_path and os.path.isfile(primary_path):
        return primary_dir, primary_path, primary_lock
    base = os.path.splitext(str(filename or ""))[0]
    if not base:
        return primary_dir, primary_path, primary_lock
    if slugs is None:
        slugs = related_archive_slugs(streamer)
    for slug in slugs:
        thumb_dir = safe_join(config.THUMB_ROOT, slug)
        if not thumb_dir:
            continue
        thumb_path = os.path.join(thumb_dir, f"{base}.jpg")
        if os.path.isfile(thumb_path):
            return thumb_dir, thumb_path, (
                os.path.join(primary_dir, f"{base}.lock") if primary_dir else os.path.join(thumb_dir, f"{base}.lock")
            )
    return primary_dir, primary_path, primary_lock


def start_thumb_generate(input_path, thumb_dir, thumb_path, lock_path):
    if not input_path or not thumb_dir or not thumb_path or not lock_path:
        return
    if not is_archive_mp4_filename(input_path):
        return
    ensure_dir(thumb_dir)
    
    # Check for stale lock (older than 10 minutes)
    if os.path.exists(lock_path):
        try:
            if time.time() - os.path.getmtime(lock_path) > 600:
                os.remove(lock_path)
            else:
                return
        except OSError:
            return

    if os.path.exists(thumb_path):
        return

    try:
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        return

    # Task Queue statt ThreadPool
    try:
        streamer = os.path.basename(os.path.dirname(os.path.abspath(input_path)))
        filename = os.path.basename(input_path)
        database.add_task(
            "thumbnail",
            {
                "input_path": input_path,
                "thumb_path": thumb_path,
                "streamer": streamer,
                "filename": filename,
            },
        )
    except Exception as exc:
        logging.error("Konnte Thumbnail-Task nicht anlegen fuer %s: %s", input_path, exc)
        try:
            os.remove(lock_path)
        except OSError:
            pass


def chapter_path(streamer, filename):
    if os.path.basename(filename) != filename:
        return None
    safe_dir = streamer_dir(streamer)
    if not safe_dir:
        return None
    return os.path.join(safe_dir, f"{filename}.chapters.txt")


def parse_time_to_seconds(value):
    value = str(value).strip()
    try:
        return float(value)
    except ValueError:
        pass
    parts = value.split(":")
    if len(parts) == 2:
        mm, ss = parts
        return int(mm) * 60 + int(ss)
    if len(parts) == 3:
        hh, mm, ss = parts
        return int(hh) * 3600 + int(mm) * 60 + int(ss)
    raise ValueError("Invalid time format")


def sanitize_name(value):
    allowed = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_"):
            allowed.append(ch)
        elif ch.isspace():
            allowed.append("_")
    return "".join(allowed).strip("_")


def clip_output_path(streamer, source_name, start_sec, duration_sec, label=""):
    if os.path.basename(source_name) != source_name:
        return None
    safe_dir = streamer_dir(streamer)
    if not safe_dir:
        return None
    return clip_engine.make_output_path(safe_dir, streamer, label)


def resolve_vod_clip_source(streamer, filename):
    """Resolve a local archive file for VOD clipping."""
    local_path = resolve_media_path(streamer, filename, "vod")
    if local_path:
        return {
            "ok": True,
            "input_path": local_path,
            "http_headers": None,
            "source": "local",
            "error": None,
        }
    return {
        "ok": False,
        "input_path": None,
        "http_headers": None,
        "source": None,
        "error": gettext("Recording not available."),
    }


def vod_clip_ui_enabled(file_path):
    """Show clip controls for local VODs."""
    return bool(file_path)


def get_duration(file_path):
    if not file_path or not os.path.isfile(file_path):
        return 0.0
    if os.path.getsize(file_path) == 0:
        return 0.0
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", file_path
        ]
        # Timeout auf 1s reduziert fuer bessere Performance
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def format_seconds(seconds):
    try:
        value = float(seconds or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        return ""
    m, s = divmod(int(value), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def current_username():
    return str(session.get("username") or "").strip() or "unknown"


def vod_slot_holder():
    return str(session.get("username") or "").strip()


def vod_slot_exempt():
    return session.get("user_role") == "admin"


def enforce_vod_watch_slot(streamer="", filename=""):
    """Return a 429 response when VOD watcher slots are full. Live HLS is not gated."""
    if vod_slot_exempt() or request.method in {"HEAD", "OPTIONS"}:
        return None
    holder = vod_slot_holder()
    try:
        result = vod_watch_slots.claim(holder, streamer, filename)
    except Exception:
        logging.debug("vod slot claim failed", exc_info=True)
        return None
    if result.get("ok"):
        return None
    retry_after = int(result.get("retry_after") or vod_watch_slots.stale_sec())
    wants_json = _request_wants_json_response() or request.path.startswith("/api/")
    if wants_json:
        resp = jsonify(result)
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after)
        return resp
    resp = Response(result.get("message") or str(vod_watch_slots.BUSY_MESSAGE), status=429, mimetype="text/plain; charset=utf-8")
    resp.headers["Retry-After"] = str(retry_after)
    resp.headers["X-StreamVault-Vod-Slot"] = "full"
    return resp


def vod_slot_client_config(streamer, filename, *, enabled=None):
    limits = vod_watch_slots.public_limits()
    slot_enabled = limits["max"] > 0 and not vod_slot_exempt()
    if enabled is not None:
        slot_enabled = bool(enabled)
    return {
        "enabled": slot_enabled,
        "claim_url": url_for("vod_slot_claim"),
        "heartbeat_url": url_for("vod_slot_heartbeat"),
        "release_url": url_for("vod_slot_release"),
        "streamer": streamer,
        "filename": filename,
        "heartbeat_sec": limits["heartbeat_sec"],
        "pause_grace_sec": limits["pause_grace_sec"],
        "csrf_token": generate_csrf_token(),
        "message": str(vod_watch_slots.BUSY_MESSAGE),
    }


def user_settings_path():
    return os.path.join(config.META_DIR, "user_settings.json")


def load_user_settings():
    data = load_json(user_settings_path(), {})
    return data if isinstance(data, dict) else {}


def save_user_settings(settings):
    save_json(user_settings_path(), settings if isinstance(settings, dict) else {})


def canonical_account_name(username):
    raw = str(username or "").strip()
    if not raw:
        return "unknown"
    raw_lower = raw.lower()
    for stored_name in load_users():
        if stored_name.lower() == raw_lower:
            return stored_name
    if config.VOD_USER and raw_lower == config.VOD_USER.lower():
        return config.VOD_USER
    return raw


def current_account_name():
    return canonical_account_name(current_username())


def current_theme_key(username=None):
    account = canonical_account_name(username or current_username())
    settings = load_user_settings()
    account_settings = settings.get(account)
    if isinstance(account_settings, dict):
        theme = str(account_settings.get("theme") or "").strip().lower()
        if theme in VALID_THEME_KEYS:
            return theme
    return DEFAULT_THEME


def set_user_theme(username, theme_key):
    theme = str(theme_key or "").strip().lower()
    if theme not in VALID_THEME_KEYS:
        return False
    account = canonical_account_name(username)
    settings = load_user_settings()
    account_settings = settings.get(account)
    if not isinstance(account_settings, dict):
        account_settings = {}
    account_settings["theme"] = theme
    account_settings["updated_at"] = int(time.time())
    settings[account] = account_settings
    save_user_settings(settings)
    return True


def rename_user_settings(old_username, new_username):
    old_account = canonical_account_name(old_username)
    new_account = canonical_account_name(new_username)
    if not old_account or not new_account or old_account == new_account:
        return
    settings = load_user_settings()
    if old_account in settings:
        settings[new_account] = settings.pop(old_account)
        save_user_settings(settings)


def delete_user_settings(username):
    account = canonical_account_name(username)
    settings = load_user_settings()
    if account in settings:
        settings.pop(account, None)
        save_user_settings(settings)

def parse_date_from_filename(filename, mtime):
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", filename)
    if match:
        y, mo, d, h, mi, s = match.groups()
        return f"{d}.{mo}.{y} {h}:{mi}:{s}"
    match = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", filename)
    if match:
        y, mo, d, h, mi, s = match.groups()
        return f"{d}.{mo}.{y} {h}:{mi}:{s}"
    return time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(mtime))

def set_download_status(streamer, status, msg=None):
    path = os.path.join(config.META_DIR, f"dl_status_{streamer}.json")
    data = {"status": status, "msg": msg, "ts": time.time()}
    save_json(path, data)

def get_download_status(streamer):
    path = os.path.join(config.META_DIR, f"dl_status_{streamer}.json")
    if not os.path.exists(path):
        return None
    data = load_json(path, {})
    # Status nach 1 Stunde löschen wenn nicht running
    if data.get("status") != "running" and data.get("ts", 0) < time.time() - 3600:
        return None
    return data

class DL_Logger:
    def __init__(self, streamer):
        self.streamer = streamer
    def _handle_msg(self, msg):
        msg = str(msg).strip()
        if msg.startswith('[download] Downloading video'):
            clean = msg.replace("[download] ", "").strip()
            set_download_status(self.streamer, "running", clean)
        elif any(x in msg for x in ['[Merger]', '[Fixup]', '[ffmpeg]', 'Merging formats']):
            set_download_status(self.streamer, "running", gettext("Processing video (this may take a while)..."))
    def debug(self, msg):
        self._handle_msg(msg)
    def info(self, msg):
        self._handle_msg(msg)
    def warning(self, msg): pass
    def error(self, msg):
        set_download_status(self.streamer, "error", str(msg))

def run_vod_download_task(streamer):
    """Background task to download all VODs for a streamer."""
    safe_dir = streamer_dir(streamer)
    if not safe_dir:
        return
    os.makedirs(safe_dir, exist_ok=True)
    
    set_download_status(streamer, "running", gettext("Starting download..."))
    
    last_update = [0]
    def progress_hook(d):
        now = time.time()
        if d['status'] == 'downloading':
            p = d.get('_percent_str', '').strip()
            # Update sofort bei 100% oder alle 2 Sekunden
            if "100%" in p or now - last_update[0] >= 2:
                last_update[0] = now
                set_download_status(streamer, "running", gettext("Downloading... %(percent)s", percent=p))
        elif d['status'] == 'finished':
            set_download_status(streamer, "running", gettext("Processing video..."))

    channel_url = f"https://www.twitch.tv/{streamer}/videos?filter=archives&sort=time"
    
    ydl_opts = {
        'format': 'best[height<=720]',
        'outtmpl': os.path.join(safe_dir, '%(upload_date)s_%(title)s_%(id)s.%(ext)s'),
        'ignoreerrors': True,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [progress_hook],
        'logger': DL_Logger(streamer),
    }
    
    logging.info(f"Starting background VOD download for {streamer}...")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([channel_url])
        set_download_status(streamer, "finished", gettext("Download complete."))
        logging.info(f"Finished background VOD download for {streamer}.")
    except Exception as e:
        set_download_status(streamer, "error", gettext("Error: %(error)s", error=str(e)))
        logging.error(f"Error in background VOD download for {streamer}: {e}")

STREAMER_CACHE = {"ts": 0, "data": [], "token": 0}
STREAMER_CACHE_TTL = 30  # Online-Status soll sichtbar frisch bleiben.


_MEDIA_TOKEN_CACHE = {"ts": 0.0, "token": 0.0}


def _cache_bust_path():
    return os.path.join(config.META_DIR, "media_cache_bust.stamp")


def bust_media_cache():
    stamp = _cache_bust_path()
    try:
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
    except OSError:
        pass
    _MEDIA_TOKEN_CACHE["ts"] = 0.0


def _media_cache_token():
    from flask import has_request_context, g
    if has_request_context() and hasattr(g, "_media_token"):
        return g._media_token
    now = time.time()
    if now - _MEDIA_TOKEN_CACHE["ts"] < 2.0:
        token = _MEDIA_TOKEN_CACHE["token"]
    else:
        try:
            token = os.path.getmtime(_cache_bust_path())
        except (OSError, Exception):
            token = 0.0
        _MEDIA_TOKEN_CACHE["token"] = token
        _MEDIA_TOKEN_CACHE["ts"] = now
    if has_request_context():
        g._media_token = token
    return token


def current_user_is_admin():
    try:
        return session.get("user_role") == "admin"
    except RuntimeError:
        return False


def current_user_role():
    try:
        return session.get("user_role") or "user"
    except RuntimeError:
        return "user"


def streamer_visibility(streamer):
    primary = archive_name_for(streamer) or str(streamer or "").strip()
    profiles = load_profiles()
    entry = profiles.get(primary) or profiles.get(streamer) or {}
    vis = str(entry.get("visibility") or "").strip().lower()
    return "admin" if vis == "admin" else "members"


def can_view_streamer(streamer):
    if not streamer:
        return False
    if current_user_is_admin():
        return True

    from core import tube_auth
    role = current_user_role()
    if tube_auth.is_member(role):
        related = related_archive_slugs(streamer)
        if not tube_auth.streamer_allowed_for_member(
            streamer,
            related,
            role=role,
        ):
            return False

    ctx = _get_current_user_context()
    if not ctx or not ctx.get("active", True):
        return False

    perms = ctx.get("permissions") or {}
    if not perms.get("can_view_streams", True):
        return False

    candidates = {str(streamer).strip().lower()}
    try:
        norm = normalize_channel_name(streamer)
        if norm:
            candidates.add(norm.lower())
    except Exception:
        pass
    try:
        arch = archive_name_for(streamer)
        if arch:
            candidates.add(arch.lower())
    except Exception:
        pass
    try:
        for s in related_archive_slugs(streamer) or []:
            candidates.add(str(s).strip().lower())
    except Exception:
        pass

    # 1. Check disabled_channels (Folder explicitly shut off / disabled)
    disabled_set = ctx.get("disabled_channels") or set()
    if any(c in disabled_set for c in candidates):
        return False

    # 2. Check access_all_channels (Auto-enabled for all current and future streams)
    if ctx.get("access_all_channels", True):
        if streamer_visibility(streamer) == "admin":
            return current_user_is_admin()
        return True

    # 3. Whitelist check for allowed_channels
    allowed = perms.get("allowed_channels") or []
    allowed_set = {str(c).strip().lower() for c in allowed if str(c).strip()}

    if any(c in allowed_set for c in candidates):
        if streamer_visibility(streamer) == "admin":
            return current_user_is_admin()
        return True

    from core import tube_auth
    role = current_user_role()
    if tube_auth.is_member(role):
        related = related_archive_slugs(streamer)
        return tube_auth.streamer_allowed_for_member(streamer, related, role=role)

    return False


def abort_hidden_streamer(streamer):
    if not can_view_streamer(streamer):
        abort(404)


def current_user_can_access_vods() -> bool:
    from core import tube_auth

    return tube_auth.role_can_access_vods(current_user_role())


def abort_vod_denied():
    """Block archive/VOD/clip access for Live-only users."""
    if not current_user_can_access_vods():
        abort(404)


def abort_deleted_media(streamer, filename, file_id=None):
    name = os.path.basename(str(filename or ""))
    if not name:
        return
    slugs = set()
    try:
        slugs.update(related_archive_slugs(streamer) or [])
    except Exception:
        pass
    primary = archive_name_for(streamer) or str(streamer or "").strip()
    if primary:
        slugs.add(primary)
    if streamer:
        slugs.add(str(streamer).strip())
    try:
        if database.is_deleted_media(filename=name, file_id=file_id, streamers=slugs):
            abort(404)
    except Exception:
        pass


def visible_streamers(streamers=None):
    use_req_cache = streamers is None and has_request_context()
    if use_req_cache:
        cache_key = (
            session.get("username"),
            session.get("user_role"),
            current_user_is_admin(),
            _media_cache_token(),
        )
        cached = getattr(g, "_visible_streamers_cache", None)
        if cached is not None and cached[0] == cache_key:
            return list(cached[1])

    rows = streamers if streamers is not None else list_streamers()
    if current_user_is_admin():
        if use_req_cache:
            g._visible_streamers_cache = (cache_key, list(rows))
        return rows

    ctx = _get_current_user_context()
    if not ctx or not ctx.get("active", True):
        return []

    perms = ctx.get("permissions") or {}
    if not perms.get("can_view_streams", True):
        return []

    filtered = []
    for item in rows:
        name = item.get("name") if isinstance(item, dict) else item
        if can_view_streamer(name):
            filtered.append(item)
    if use_req_cache:
        g._visible_streamers_cache = (cache_key, list(filtered))
    return filtered


def format_bytes_short(size_bytes):
    try:
        value = float(size_bytes or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value < 1024:
        return f"{int(value)} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def related_archive_slugs(streamer):
    """Map archive names and channel aliases locally."""
    primary = archive_name_for(streamer) or str(streamer or "").strip()
    slugs = set()
    if primary:
        slugs.add(primary)
        base = primary.rstrip("_")
        if base:
            slugs.add(base)
            slugs.add(f"{base}_")
    return sorted(s for s in slugs if s)


def _iter_runtime_state_files(safe_dir, exact_name, prefix, suffix):
    if not safe_dir or not os.path.isdir(safe_dir):
        return []
    paths = []
    try:
        for name in os.listdir(safe_dir):
            if name == exact_name or (name.startswith(prefix) and name.endswith(suffix)):
                paths.append(os.path.join(safe_dir, name))
    except OSError:
        return []
    return paths


def _newest_json_dict(paths):
    best = None
    best_mtime = -1.0
    for path in paths:
        data = load_json(path, None)
        if not isinstance(data, dict):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        if mtime >= best_mtime:
            best_mtime = mtime
            best = data
    return best


def live_viewer_info(streamer):
    safe_dir = streamer_dir(streamer)
    if not safe_dir:
        return {"viewers": 0, "fresh": False}
    now = time.time()
    fresh_cutoff = 120.0
    readings = []
    candidates = list(
        _iter_runtime_state_files(
            safe_dir, ".viewer_count.json", ".viewer_count.", ".json"
        )
    )
    for path in _iter_runtime_state_files(
        safe_dir, ".recording_status.json", ".recording_status.", ".json"
    ):
        candidates.append(path)
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            viewers = max(int(data.get("viewers") or data.get("last_viewers") or 0), 0)
            ts = float(data.get("ts") or data.get("updated_ts") or data.get("viewer_updated_at") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if ts <= 0:
            continue
        readings.append((ts, viewers, os.path.basename(path)))
    if not readings:
        return {"viewers": 0, "fresh": False}
    fresh = [r for r in readings if now - r[0] <= fresh_cutoff]
    # Prefer the strongest live signal among fresh files so a newer YouTube
    # offline (0 viewers) cannot wipe an active Twitch viewer_count.
    pool = fresh or readings
    # When platform-specific viewer files exist, ignore legacy .viewer_count.json.
    platform_pool = [
        r for r in pool
        if r[2].startswith(".viewer_count.") and r[2] != ".viewer_count.json"
    ]
    if platform_pool:
        pool = platform_pool
    best_viewers = max(r[1] for r in pool)
    best_ts = max(r[0] for r in pool if r[1] == best_viewers) if best_viewers else max(r[0] for r in pool)
    return {"viewers": best_viewers, "fresh": now - best_ts <= fresh_cutoff}


def read_current_recording_raw(streamer):
    safe_dir = streamer_dir(streamer)
    if not safe_dir:
        return None
    paths = _iter_runtime_state_files(
        safe_dir, ".current_recording.json", ".current_recording.", ".json"
    )
    return _newest_json_dict(paths)


def read_recording_status_raw(streamer):
    safe_dir = streamer_dir(streamer)
    if not safe_dir:
        return None
    paths = _iter_runtime_state_files(
        safe_dir, ".recording_status.json", ".recording_status.", ".json"
    )
    now = time.time()
    best_active = None
    best_active_ts = -1.0
    best_any = None
    best_any_ts = -1.0
    for path in paths:
        data = load_json(path, None)
        if not isinstance(data, dict):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        try:
            updated = float(data.get("updated_ts") or mtime or 0)
        except (TypeError, ValueError):
            updated = mtime
        if updated >= best_any_ts:
            best_any_ts = updated
            best_any = data
        state = str(data.get("state") or "").lower()
        # Prefer a fresh recording/starting status over a newer sibling "offline".
        if state in ("recording", "starting") and now - updated <= 180 and updated >= best_active_ts:
            best_active_ts = updated
            best_active = data
    return best_active or best_any


def recording_file_status(streamer, info):
    safe_dir = streamer_dir(streamer)
    if not safe_dir or not isinstance(info, dict):
        return {"filename": "", "path": "", "size": 0, "size_str": "-", "updated_age": "-", "fresh": False}
    filename = os.path.basename(str(info.get("filename") or info.get("record_file") or ""))
    if not filename:
        return {"filename": "", "path": "", "size": 0, "size_str": "-", "updated_age": "-", "fresh": False}
    for path in _recording_media_candidates(safe_dir, info):
        if not os.path.exists(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        return {
            "filename": filename,
            "path": path,
            "size": stat.st_size,
            "size_str": format_bytes(stat.st_size),
            "updated_age": format_age_short(stat.st_mtime),
            "fresh": time.time() - stat.st_mtime <= 90,
        }
    return {"filename": filename, "path": "", "size": 0, "size_str": "-", "updated_age": "-", "fresh": False}


def recording_health_rows(streamers=None, limit=12, active_only=False):
    streamer_rows = streamers if isinstance(streamers, list) else list_streamers()
    by_name = {}
    names = []
    seen = set()
    for item in streamer_rows:
        if isinstance(item, dict):
            value = item.get("name")
            if value:
                by_name[value] = item
        else:
            value = item
        if value and value not in seen:
            names.append(value)
            seen.add(value)
    for name in load_channels() + load_live_channels():
        if name and name not in seen:
            names.append(name)
            seen.add(name)

    live_channels = set(load_live_channels())
    rows = []
    seen_archives = set()
    candidate_set = set(names if active_only else names[:limit])
    # When filtering to active recordings, scan all channels so a late name
    # is not dropped by the early names[:limit] cut.
    candidates = names if active_only else names[:limit]
    for name in candidates:
        meta = by_name.get(name) or {}
        active_info = current_recording_info(name)
        if active_only and not active_info:
            continue
        archive = archive_name_for(name) or name
        if active_only:
            preferred = str((active_info or {}).get("source") or "").strip()
            # Shared archive: show the recording source once.
            if preferred and preferred != name and preferred in candidate_set:
                continue
            if archive in seen_archives:
                continue
            seen_archives.add(archive)
        raw_info = read_current_recording_raw(name)
        status_info = read_recording_status_raw(name) or {}
        info = active_info or raw_info or {}
        viewer_info = live_viewer_info(name)
        file_status = recording_file_status(name, info)
        log_file = str(info.get("log_file") or "")
        chat_ok = bool(log_file and os.path.isfile(log_file))
        if chat_ok:
            try:
                chat_age = format_age_short(os.path.getmtime(log_file))
            except OSError:
                chat_age = "-"
        else:
            chat_age = "-"
        title = clean_metadata_label(info.get("title") or info.get("stream_title") or status_info.get("title"), 120)
        category = clean_metadata_label(info.get("category") or info.get("game_name") or status_info.get("category"), 80)
        recorder_state = str(status_info.get("state") or "").lower()
        if active_info:
            status = "recording"
            status_label = gettext("Recording")
        elif recorder_state == "starting":
            status = "starting"
            status_label = gettext("Starting")
        elif recorder_state == "offline":
            status = "offline"
            status_label = gettext("Offline")
        elif recorder_state == "error":
            status = "error"
            status_label = gettext("Error")
        elif raw_info:
            status = "stale"
            status_label = gettext("Check")
        elif name in live_channels:
            status = "watching"
            status_label = gettext("Live watch")
        else:
            status = "idle"
            status_label = gettext("Ready")
        if active_only and status != "recording":
            continue
        twitch_login = meta.get("twitch_login") or twitch_api.normalize_login(name)
        twitch_live = bool(meta.get("twitch_live"))
        watch_url = url_for("streamer_view", streamer=name)
        if twitch_live and twitch_login:
            watch_url = url_for("twitch_watch", channel=twitch_login)
        rows.append({
            "name": name,
            "status": status,
            "status_label": status_label,
            "filename": file_status["filename"],
            "size_str": file_status["size_str"],
            "updated_age": file_status["updated_age"],
            "fresh": file_status["fresh"],
            "viewers": viewer_info["viewers"] or meta.get("viewers") or 0,
            "viewer_fresh": viewer_info["fresh"] or twitch_live,
            "started_age": format_age_short(info.get("start_ts")) if info.get("start_ts") else "-",
            "chat_ok": chat_ok,
            "chat_age": chat_age,
            "title": title or meta.get("twitch_title") or "",
            "category": category or meta.get("twitch_game") or "",
            "status_message": clean_metadata_label(status_info.get("message"), 180),
            "url": watch_url,
            "twitch_login": twitch_login,
            "twitch_live": twitch_live,
        })
        if active_only and len(rows) >= limit:
            break
    rows.sort(key=lambda row: {"recording": 0, "starting": 1, "error": 2, "offline": 3, "stale": 4, "watching": 5, "idle": 6}.get(row["status"], 9))
    return rows[:limit] if not active_only else rows


def recent_task_error_rows(limit=12):
    """Recent failed/errored queue tasks for the admin Recording Health panel."""
    rows = []
    try:
        tasks = database.list_tasks(limit=80)
    except Exception:
        tasks = []
    for task in tasks:
        error = str(task.get("error") or "").strip()
        status = str(task.get("status") or "").strip().lower()
        if status == "completed":
            continue
        if status != "failed" and not error:
            continue
        updated = task.get("updated_at") or task.get("created_at") or 0
        rows.append({
            "type": task.get("type") or "-",
            "status": status or "failed",
            "error": error or gettext("Unknown error"),
            "age": format_age_short(updated) if updated else "-",
            "updated_at": float(updated or 0),
        })
        if len(rows) >= limit:
            break
    return rows


def is_streamer_online(streamer):
    if current_recording_info(streamer) is not None:
        return True
    viewer_info = live_viewer_info(streamer)
    return viewer_info["fresh"] and viewer_info["viewers"] > 0


def twitch_login_from_sources(sources, configs):
    """Return the first Twitch login for an archive's source list."""
    config_by_name = {}
    for conf in configs or []:
        key = normalize_channel_name(conf.get("name")) or str(conf.get("name") or "").strip()
        if key:
            config_by_name[key] = conf
    for src in sources or []:
        conf = config_by_name.get(src) or {}
        platform = str(conf.get("platform") or "twitch").strip().lower() or "twitch"
        if platform == "twitch":
            login = twitch_api.normalize_login(src)
            if login:
                return login
    # Fallback: source name itself when configs are unavailable (legacy flat channel list)
    if configs is None:
        for src in sources or []:
            login = twitch_api.normalize_login(src)
            if login:
                return login
    return ""


def _local_hls_live_ready(streamer):
    """True when the live HLS playlist for this archive is actively updating."""
    return live_playlist_fresh(streamer)


def enrich_streamers_with_twitch_live(streamers):
    """Attach twitch_login / twitch_live / stream metadata using batched status."""
    if not streamers:
        return streamers
    logins = []
    for row in streamers:
        login = twitch_api.normalize_login(row.get("twitch_login") or "")
        row["twitch_login"] = login
        if login:
            logins.append(login)
    status_map = twitch_api.get_streams_by_logins(logins, monitored=True) if logins else {}
    for row in streamers:
        login = row.get("twitch_login") or ""
        info = status_map.get(login) or {}
        source_recording = False
        if login:
            if current_recording_info(login) is not None:
                source_recording = True
            else:
                for src in row.get("sources") or []:
                    if twitch_api.normalize_login(src) == login and current_recording_info(src) is not None:
                        source_recording = True
                        break
        # Local signals: recorder/livebot viewer files and active HLS output.
        # Needed when Twitch Helix/GQL is unavailable or briefly wrong.
        local_viewers = int(row.get("viewers") or 0)
        local_online = bool(row.get("online"))
        if not local_online or local_viewers <= 0:
            viewer_info = live_viewer_info(row.get("name") or login)
            if viewer_info.get("fresh") and int(viewer_info.get("viewers") or 0) > 0:
                local_online = True
                local_viewers = int(viewer_info["viewers"])
        hls_ready = _local_hls_live_ready(row.get("name") or login)
        is_live = bool(info.get("is_live")) or source_recording or local_online or hls_ready
        row["twitch_live"] = bool(is_live and login)
        row["twitch_title"] = str(info.get("title") or "")
        row["twitch_game"] = str(info.get("game_name") or "")
        if info.get("viewer_count"):
            row["viewers"] = int(info.get("viewer_count") or 0)
        elif local_viewers:
            row["viewers"] = local_viewers
        elif not row.get("viewers"):
            row["viewers"] = 0
        row["twitch_thumbnail"] = info.get("thumbnail_url") or (
            twitch_api.thumbnail_url(login) if login else ""
        )
    return streamers


def list_streamers():
    now = time.time()
    token = _media_cache_token()
    if now - STREAMER_CACHE["ts"] < STREAMER_CACHE_TTL and STREAMER_CACHE.get("token") == token:
        return STREAMER_CACHE["data"]

    # Shared Redis cache across all Gunicorn workers
    try:
        from core import redis_client
        cached_streamers = redis_client.get_state("streamers_list")
        if isinstance(cached_streamers, list) and cached_streamers:
            STREAMER_CACHE["ts"] = now
            STREAMER_CACHE["token"] = token
            STREAMER_CACHE["data"] = cached_streamers
            return cached_streamers
    except Exception:
        pass

    streamers = []
    live_channels = set(load_live_channels())
    base_ok = os.path.isdir(BASE_DIR)

    profiles = load_profiles() if base_ok else {}
    missing_profiles = []
    seen = set()
    archive_sources = {}

    try:
        configs = database.list_channel_configs(mode="record")
    except Exception:
        configs = []
    if not configs:
        for name in load_channels():
            configs.append({"name": name, "archive_name": name, "platform": "twitch"})

    for conf in configs:
        name = normalize_channel_name(conf.get("name")) or str(conf.get("name") or "").strip()
        if not name:
            continue
        archive = normalize_channel_name(conf.get("archive_name")) or name
        archive_sources.setdefault(archive, []).append(name)

    def _scan_local(archive):
        path = streamer_dir(archive) if archive else None
        count = 0
        latest_ts = None
        local_names = set()
        if path and os.path.isdir(path):
            with os.scandir(path) as it:
                for entry in it:
                    if is_archive_mp4_filename(entry.name) and entry.is_file():
                        local_names.add(entry.name)
                        count += 1
                        ts = parse_timestamp_from_filename(entry.name, entry.stat().st_mtime)
                        if latest_ts is None or ts > latest_ts:
                            latest_ts = ts
        return count, latest_ts, local_names

    for archive, sources in sorted(archive_sources.items(), key=lambda item: item[0]):
        count, latest_ts, local_names = _scan_local(archive)
        latest_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_ts)) if latest_ts else None
        latest_label, latest_title = dashboard_time_label(latest_ts)
        live = any(src in live_channels for src in sources) or archive in live_channels
        online = any(is_streamer_online(src) for src in sources) or is_streamer_online(archive)

        p_data = profiles.get(archive)
        if not p_data:
            for src in list(sources) + related_archive_slugs(archive):
                p_data = profiles.get(src)
                if p_data:
                    break
        avatar_url, _ = resolve_streamer_avatar(archive, archive, p_data)
        if not p_data or (int(time.time()) - p_data.get("ts", 0) > 604800):
            missing_profiles.append(archive)

        viewer_info = live_viewer_info(archive)
        if not viewer_info.get("viewers"):
            for src in sources:
                alt = live_viewer_info(src)
                if alt.get("viewers"):
                    viewer_info = alt
                    break

        external_url = f"https://twitch.tv/{archive}"
        external_platform = "twitch"
        platforms = []
        for src in sources:
            conf = next((c for c in configs if (normalize_channel_name(c.get("name")) or c.get("name")) == src), None)
            if not conf:
                continue
            platform = str(conf.get("platform") or "twitch").lower()
            if platform not in platforms:
                platforms.append(platform)
            source_url = str(conf.get("source_url") or "").strip()
            if platform == "youtube" and source_url:
                external_url = source_url
                external_platform = "youtube"

        twitch_login = twitch_login_from_sources(sources, configs)
        streamers.append({
            "name": archive,
            "display_name": archive_display_name(archive) or archive,
            "visibility": streamer_visibility(archive),
            "count": count,
            "latest": latest_str,
            "latest_label": latest_label,
            "latest_title": latest_title,
            "last_date": latest_label,
            "last_stream_time": latest_label,
            "live": live,
            "online": online,
            "viewers": viewer_info["viewers"],
            "avatar_url": avatar_url,
            "banner_url": p_data.get("banner_url") if p_data else None,
            "sources": sources,
            "platforms": platforms or ([external_platform] if external_platform else ["twitch"]),
            "external_url": external_url,
            "external_platform": external_platform,
            "twitch_login": twitch_login,
            "twitch_live": False,
            "twitch_title": "",
            "twitch_game": "",
            "twitch_thumbnail": "",
        })
        seen.add(archive)
        seen.update(sources)

    if base_ok:
        for name in sorted(os.listdir(BASE_DIR)):
            if name in seen:
                continue
            if name.startswith(".") or name == "__pycache__":
                continue
            # Skip archive aliases of an already listed streamer.
            aliased = False
            try:
                for existing in list(seen):
                    if name in related_archive_slugs(existing):
                        aliased = True
                        break
            except Exception:
                aliased = False
            if aliased:
                continue
            if is_system_backup_slug(name):
                continue
            path = os.path.join(BASE_DIR, name)
            if not os.path.isdir(path):
                continue
            count, latest_ts, local_names = _scan_local(name)
            if count == 0:
                continue
            latest_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_ts)) if latest_ts else None
            latest_label, latest_title = dashboard_time_label(latest_ts)
            live = name in live_channels
            online = is_streamer_online(name)

            p_data = profiles.get(name)
            avatar_url, _ = resolve_streamer_avatar(name, name, p_data)
            if not p_data:
                missing_profiles.append(name)

            viewer_info = live_viewer_info(name)
            twitch_login = twitch_api.normalize_login(name)
            streamers.append({
                "name": name,
                "display_name": archive_display_name(name) or name,
                "visibility": streamer_visibility(name),
                "count": count,
                "latest": latest_str,
                "latest_label": latest_label,
                "latest_title": latest_title,
                "last_date": latest_label,
                "last_stream_time": latest_label,
                "live": live,
                "online": online,
                "viewers": viewer_info["viewers"],
                "avatar_url": avatar_url,
                "sources": [name],
                "platforms": ["twitch"],
                "external_url": f"https://twitch.tv/{name}",
                "external_platform": "twitch",
                "twitch_login": twitch_login,
                "twitch_live": False,
                "twitch_title": "",
                "twitch_game": "",
                "twitch_thumbnail": "",
            })
            seen.add(name)

    for row in streamers:
        if not row.get("display_name"):
            row["display_name"] = archive_display_name(row.get("name")) or row.get("name")
    streamers.sort(key=lambda item: str(item.get("display_name") or item.get("name") or "").lower())
    enrich_streamers_with_twitch_live(streamers)

    if missing_profiles:
        threading.Thread(target=update_profiles_background, args=(missing_profiles,), daemon=True).start()

    STREAMER_CACHE["ts"] = now
    STREAMER_CACHE["token"] = token
    STREAMER_CACHE["data"] = streamers
    try:
        from core import redis_client
        redis_client.set_state("streamers_list", streamers, ex=60)
    except Exception:
        pass
    return streamers

def get_metadata_cache():
    path = os.path.join(config.META_DIR, "metadata.json")
    if not os.path.exists(path):
        return {}
    mtime = os.path.getmtime(path)
    if mtime > METADATA_CACHE["mtime"]:
        METADATA_CACHE["data"] = load_json(path, {})
        METADATA_CACHE["mtime"] = mtime
    return METADATA_CACHE["data"]


def metadata_cache_path():
    return os.path.join(config.META_DIR, "metadata.json")


def save_metadata_cache(metadata):
    try:
        save_json(metadata_cache_path(), metadata)
        METADATA_CACHE["data"] = metadata
        try:
            METADATA_CACHE["mtime"] = os.path.getmtime(metadata_cache_path())
        except OSError:
            METADATA_CACHE["mtime"] = time.time()
    except Exception as exc:
        logging.error("Konnte Metadaten-Cache nicht speichern: %s", exc)


def parse_timestamp_from_filename(filename, mtime=None):
    name = os.path.basename(str(filename or ""))
    for pattern, order in (
        # archive: 2026-08-01_12-00-00 or 20260801_120000
        (r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", "ymdHMS"),
        (r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", "ymdHMS"),
        (r"(\d{4})_(\d{2})_(\d{2})_(\d{2})-(\d{2})-(\d{2})", "ymdHMS"),
        # legacy EU: 10.08.2025 or Channel_18.08.2025
        (r"(?<!\d)(\d{2})\.(\d{2})\.(\d{4})(?!\d)", "dmy"),
        (r"^(\d{4})(\d{2})(\d{2})_", "ymd"),
        (r"^(\d{4})-(\d{2})-(\d{2})", "ymd"),
    ):
        match = re.search(pattern, name)
        if not match:
            continue
        parts = match.groups()
        try:
            if order == "ymdHMS":
                y, mo, d, h, mi, s = parts
            elif order == "dmy":
                d, mo, y = parts
                h = mi = s = "0"
            else:
                y, mo, d = parts
                h = mi = s = "0"
            return time.mktime((int(y), int(mo), int(d), int(h), int(mi), int(s), -1, -1, -1))
        except (TypeError, ValueError, OverflowError):
            continue
    return float(mtime or time.time())


def compact_duration(seconds):
    try:
        value = int(float(seconds or 0))
    except (TypeError, ValueError):
        value = 0
    hours, remainder = divmod(value, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


MONTH_NAMES = [
    lazy_gettext("January"), lazy_gettext("February"), lazy_gettext("March"), lazy_gettext("April"), lazy_gettext("May"), lazy_gettext("June"),
    lazy_gettext("July"), lazy_gettext("August"), lazy_gettext("September"), lazy_gettext("October"), lazy_gettext("November"), lazy_gettext("December"),
]


def parse_local_timestamp(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d_%H-%M-%S", "%Y-%m-%d-%H-%M-%S"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def dashboard_time_label(value):
    ts = parse_local_timestamp(value)
    if not ts:
        return "-", ""
    dt = datetime.fromtimestamp(ts)
    now = datetime.now()
    title = dt.strftime("%d.%m.%Y %H:%M:%S")
    if dt.date() == now.date():
        return gettext("Today, %(time)s", time=dt.strftime("%H:%M")), title
    if dt.date() == (now - timedelta(days=1)).date():
        return gettext("Yesterday, %(time)s", time=dt.strftime("%H:%M")), title
    if dt.year == now.year:
        month = MONTH_NAMES[dt.month - 1]
        return gettext("%(month)s %(day)s, %(time)s", month=month, day=dt.day, time=dt.strftime("%H:%M")), title
    return dt.strftime("%d.%m.%Y, %H:%M"), title


def file_size_for_recording(streamer, filename):
    if not streamer or not filename:
        return ""
    safe_dir = streamer_dir(streamer)
    if not safe_dir:
        return ""
    for candidate in (filename, f"{filename}.part"):
        path = os.path.join(safe_dir, candidate)
        try:
            if os.path.isfile(path):
                return format_bytes(os.path.getsize(path))
        except OSError:
            pass
    return ""


def dashboard_context(streamers, live_channels, storage, storage_alert=None):
    vod_count = sum(int(item.get("count") or 0) for item in streamers)
    active_name = ""
    active_info = None
    for name in live_channels:
        info = current_recording_info(name)
        if info:
            active_name = name
            active_info = info
            break
    if not active_name and live_channels:
        active_name = live_channels[0]

    active_filename = str((active_info or {}).get("filename") or "").strip()
    active_started = (active_info or {}).get("start_ts")
    recorder_state = "recording" if active_info else "ready"
    recorder_label = gettext("Recording in progress") if active_info else gettext("Ready")
    recorder_detail = active_name if active_name else gettext("No active streams")
    storage_pct = float(storage.get("pct") or 0)
    storage_state = "critical" if storage_pct >= 90 else ("warning" if storage_pct >= 80 else "ok")
    if storage_alert and storage_alert.get("critical"):
        storage_state = "critical"
    storage_state_label = {"ok": gettext("OK"), "warning": gettext("Warning"), "critical": gettext("Critical")}.get(storage_state, gettext("OK"))
    return {
        "vod_count": vod_count,
        "recorder_state": recorder_state,
        "recorder_label": recorder_label,
        "recorder_detail": recorder_detail,
        "active_name": active_name,
        "active_filename": active_filename,
        "active_started": format_age_short(active_started) if active_started else "",
        "active_size": file_size_for_recording(active_name, active_filename),
        "storage_used_str": format_bytes(storage.get("used", 0)),
        "storage_pct": storage_pct,
        "storage_state": storage_state,
        "storage_state_label": storage_state_label,
        "storage_path": BASE_DIR,
    }


def clean_metadata_label(value, limit=120):
    text = re.sub(r"\s+", " ", str(value or "").replace("_", " ")).strip()
    return text[:limit]


def clean_vod_description(value, limit=4000):
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def parse_viewer_count(value):
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        for key in ("count", "viewers", "viewer_count", "value"):
            parsed = parse_viewer_count(value.get(key))
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            parsed = parse_viewer_count(item)
            if parsed is not None:
                return parsed
        return None
    try:
        text = str(value).strip().lower()
        if not text:
            return None
        multiplier = 1
        if text.endswith(("k", "m")):
            multiplier = 1000 if text.endswith("k") else 1000000
            text = text[:-1].strip()
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            parts = text.split(",")
            text = text.replace(",", "") if len(parts[-1]) == 3 else text.replace(",", ".")
        elif "." in text:
            parts = text.split(".")
            text = text.replace(".", "") if len(parts[-1]) == 3 else text
        return max(0, int(float(text) * multiplier))
    except (TypeError, ValueError, OverflowError):
        return None


def format_viewer_count(value):
    count = parse_viewer_count(value)
    if count is None:
        return ""
    return f"{count:,}".replace(",", ".")


def sidecar_metadata_candidates(media_path):
    if not media_path:
        return []
    base, _ = os.path.splitext(media_path)
    return [
        f"{media_path}.info.json",
        f"{base}.info.json",
        f"{base}.metadata.json",
        f"{base}.json",
    ]


def read_media_sidecar_metadata(media_path):
    for path in sidecar_metadata_candidates(media_path):
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > 2 * 1024 * 1024:
                continue
            data = load_json(path, {})
        except OSError:
            continue
        if not isinstance(data, dict):
            continue
        title = (
            data.get("title")
            or data.get("stream_title")
            or data.get("fulltitle")
            or data.get("display_title")
        )
        category = (
            data.get("category")
            or data.get("game")
            or data.get("game_name")
            or data.get("category_name")
            or data.get("categories")
        )
        if isinstance(category, list):
            category = category[0] if category else ""
        if isinstance(category, dict):
            category = category.get("name") or category.get("displayName") or ""
        platform = data.get("platform") or data.get("extractor_key") or data.get("source")
        started_at = data.get("started_at") or data.get("start_ts") or data.get("timestamp")
        viewer_count = None
        for key in (
            "viewers",
            "viewer_count",
            "viewerCount",
            "viewersCount",
            "view_count",
            "viewCount",
            "concurrent_viewers",
            "average_viewers",
            "max_viewers",
        ):
            viewer_count = parse_viewer_count(data.get(key))
            if viewer_count is not None:
                break
        if not title:
            try:
                base_cand = os.path.splitext(media_path)[0]
                streamer_dir_cand = os.path.dirname(media_path)
                chat_cand = os.path.join(streamer_dir_cand, "chat_logs", f"{os.path.basename(base_cand)}.jsonl")
                if os.path.isfile(chat_cand):
                    with open(chat_cand, "r", encoding="utf-8", errors="ignore") as cf:
                        for i, line in enumerate(cf):
                            if i > 60:
                                break
                            msg = ""
                            try:
                                msg = str(json.loads(line).get("msg") or "").strip()
                            except Exception:
                                continue
                            m_se = re.search(r"Streaming\s+([^:]+):\s*(.+)$", msg, re.IGNORECASE)
                            if m_se:
                                if not category:
                                    category = m_se.group(1).strip()
                                title = m_se.group(2).strip()
                                break
                            m_w = re.search(r"(?:Neuer Status|Stream-Titel):\s*(.+)$", msg, re.IGNORECASE)
                            if m_w and not title:
                                title = m_w.group(1).strip()
            except Exception:
                pass

        return {
            "title": clean_metadata_label(title, 180),
            "category": clean_metadata_label(category, 80),
            "platform": clean_metadata_label(platform or "Twitch", 40),
            "started_at": started_at,
            "viewer_count": viewer_count,
        }
    return {}


def infer_media_title(streamer, filename, is_clip=False):
    base = os.path.splitext(filename)[0]
    if is_clip:
        match = re.search(r"_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_(.+)_Clip$", base)
        if match:
            return clean_metadata_label(match.group(1), 140)
        return "Clip"
    if re.match(r"^\d{8}_", base):
        title_part = re.sub(r"^\d{8}_", "", base)
        title_part = re.sub(r"_[A-Za-z0-9_-]{6,}$", "", title_part)
        title = clean_metadata_label(title_part, 160)
        return title or ""
    streamer_prefix = re.escape(str(streamer or ""))
    if re.match(rf"^{streamer_prefix}_+\d{{4}}-\d{{2}}-\d{{2}}_", base, re.IGNORECASE):
        return ""
    if re.match(rf"^{streamer_prefix}_+\d{{8}}_\d{{6}}", base, re.IGNORECASE):
        return ""
    return ""


def apply_media_metadata(streamer, item, metadata):
    fid = f"{streamer}/{item['name']}"
    record = metadata.setdefault(fid, {})
    changed = False
    sidecar = read_media_sidecar_metadata(item.get("full_path") or "")
    title_locked = bool(record.get("title_locked"))
    if title_locked:
        title = record.get("title") or infer_media_title(streamer, item["name"], item.get("is_clip"))
    else:
        title = sidecar.get("title") or record.get("title") or infer_media_title(streamer, item["name"], item.get("is_clip"))
    category = sidecar.get("category") or record.get("category") or ""
    platform = sidecar.get("platform") or record.get("platform") or "Twitch"
    started_at = sidecar.get("started_at") or record.get("started_at")
    viewer_count = sidecar.get("viewer_count")
    if viewer_count is None:
        viewer_count = parse_viewer_count(record.get("viewer_count"))
    description = clean_vod_description(record.get("description") or "")

    updates = {
        "title": clean_metadata_label(title, 180),
        "category": clean_metadata_label(category, 80),
        "platform": clean_metadata_label(platform, 40),
    }
    if started_at:
        updates["started_at"] = started_at
    if viewer_count is not None:
        updates["viewer_count"] = viewer_count

    for key, value in updates.items():
        if value is not None and value != "" and record.get(key) != value:
            record[key] = value
            changed = True

    item["title"] = updates["title"]
    item["category"] = updates["category"]
    item["platform"] = updates["platform"] or "Twitch"
    item["viewer_count"] = viewer_count
    item["viewer_count_str"] = format_viewer_count(viewer_count)
    item["recorded_ts"] = parse_timestamp_from_filename(item["name"], item.get("mtime"))
    item["date_key"] = time.strftime("%Y-%m-%d", time.localtime(item["recorded_ts"]))
    item["display_title"] = item["title"] or item["name"]
    item["description"] = description
    item["title_locked"] = title_locked
    return changed


def media_display_metadata(streamer, filename, file_path, stat=None, is_clip=False):
    try:
        if stat is not None:
            mtime = stat.st_mtime
        elif file_path:
            mtime = os.path.getmtime(file_path)
        else:
            mtime = time.time()
    except OSError:
        mtime = time.time()
    item = {
        "name": filename,
        "full_path": file_path,
        "mtime": mtime,
        "is_clip": is_clip,
    }
    metadata = get_metadata_cache()
    if apply_media_metadata(streamer, item, metadata):
        save_metadata_cache(metadata)
    return item


def media_item_from_entry(entry, is_clip=False):
    stat = entry.stat()
    size_bytes = stat.st_size
    size_mb = size_bytes / (1024 * 1024)
    return {
        "name": entry.name,
        "mtime": stat.st_mtime,
        "size": f"{size_mb:.1f}",
        "size_bytes": size_bytes,
        "is_clip": is_clip,
        "route": "clip" if is_clip else "vod",
        "full_path": entry.path,
        "date_str": parse_date_from_filename(entry.name, stat.st_mtime),
    }


def collect_streamer_media_items(streamer):
    from flask import has_request_context, g
    if has_request_context():
        if not hasattr(g, "_cached_streamer_items"):
            g._cached_streamer_items = {}
        if streamer in g._cached_streamer_items:
            return [dict(it) for it in g._cached_streamer_items[streamer]]

    items = []
    try:
        suppress_slugs = set(related_archive_slugs(streamer) or [])
    except Exception:
        suppress_slugs = set()
    primary = archive_name_for(streamer) or str(streamer or "").strip()
    if primary:
        suppress_slugs.add(primary)
    if streamer:
        suppress_slugs.add(str(streamer).strip())

    try:
        del_names, del_fids = database.get_deleted_media_set(suppress_slugs)
    except Exception:
        del_names, del_fids = set(), set()

    def _suppressed(name, file_id=""):
        if name and name in del_names:
            return True
        if file_id and file_id in del_fids:
            return True
        return False

    safe_dir = streamer_dir(streamer)
    if safe_dir and os.path.isdir(safe_dir):
        with os.scandir(safe_dir) as it:
            for entry in it:
                if is_archive_mp4_filename(entry.name) and entry.is_file():
                    if _suppressed(entry.name):
                        continue
                    items.append(media_item_from_entry(entry, is_clip=False))

        clips_dir = os.path.join(safe_dir, CLIP_SUBDIR)
        if os.path.isdir(clips_dir):
            with os.scandir(clips_dir) as it:
                for entry in it:
                    if is_archive_mp4_filename(entry.name) and entry.is_file():
                        if _suppressed(entry.name):
                            continue
                        items.append(media_item_from_entry(entry, is_clip=True))

    if has_request_context():
        g._cached_streamer_items[streamer] = [dict(it) for it in items]
    return items


def streamer_archive_stats(streamer):
    from flask import has_request_context, g
    if has_request_context():
        if not hasattr(g, "_cached_streamer_stats"):
            g._cached_streamer_stats = {}
        if streamer in g._cached_streamer_stats:
            return dict(g._cached_streamer_stats[streamer])
    items = collect_streamer_media_items(streamer)
    metadata = get_metadata_cache()
    cache_changed = False
    for item in items:
        cache_changed = apply_media_metadata(streamer, item, metadata) or cache_changed

    vods = [item for item in items if item["route"] == "vod" and item.get("size_bytes", 0) > 0]
    clips = [item for item in items if item["route"] == "clip" and item.get("size_bytes", 0) > 0]
    total_size = sum(item.get("size_bytes", 0) for item in vods)
    active_days = {}
    weekday_counts = {}
    hour_counts = {}
    category_counts = {}
    platform_counts = {}
    total_duration = 0.0
    duration_known = 0
    should_scan_duration = len(vods) <= STREAMER_ANALYTICS_DURATION_SCAN_LIMIT

    for item in vods:
        fid = f"{streamer}/{item['name']}"
        record = metadata.setdefault(fid, {})
        if record.get("mtime") != item["mtime"]:
            record["mtime"] = item["mtime"]
            cache_changed = True
        duration = record.get("duration")
        if duration is None and should_scan_duration and item.get("full_path"):
            duration = get_duration(item["full_path"])
            if float(duration or 0) > 0:
                record["duration"] = duration
                cache_changed = True
        try:
            duration_value = float(duration or 0)
        except (TypeError, ValueError):
            duration_value = 0.0
        if duration_value > 0:
            total_duration += duration_value
            duration_known += 1

        date_key = item["date_key"]
        day = active_days.setdefault(date_key, {"count": 0, "seconds": 0.0, "date": date_key})
        day["count"] += 1
        day["seconds"] += duration_value
        weekday = time.strftime("%A", time.localtime(item["recorded_ts"]))
        hour = time.strftime("%H:00", time.localtime(item["recorded_ts"]))
        weekday_counts[weekday] = weekday_counts.get(weekday, 0) + 1
        hour_counts[hour] = hour_counts.get(hour, 0) + 1
        if item.get("category"):
            category_counts[item["category"]] = category_counts.get(item["category"], 0) + 1
        if item.get("platform"):
            platform_counts[item["platform"]] = platform_counts.get(item["platform"], 0) + 1

    today = datetime.fromtimestamp(time.time()).date()
    start_day = today - timedelta(days=max(1, STREAMER_ANALYTICS_DAYS) - 1)
    max_day_count = max((day["count"] for day in active_days.values()), default=1)
    calendar = []
    for offset in range(max(1, STREAMER_ANALYTICS_DAYS)):
        day_date = start_day + timedelta(days=offset)
        key = day_date.isoformat()
        day_data = active_days.get(key, {"count": 0, "seconds": 0.0})
        count = int(day_data.get("count") or 0)
        level = 0 if count == 0 else max(1, min(4, math.ceil((count / max_day_count) * 4)))
        calendar.append({
            "date": key,
            "label": day_date.strftime("%d.%m."),
            "count": count,
            "seconds": day_data.get("seconds", 0.0),
            "level": level,
            "url": url_for("streamer_view", streamer=streamer, q=key, type="vod"),
        })

    top_days = sorted(active_days.values(), key=lambda day: (day["count"], day["seconds"]), reverse=True)[:5]
    for day in top_days:
        day["label"] = datetime.strptime(day["date"], "%Y-%m-%d").strftime("%d.%m.%Y")
        day["duration_str"] = compact_duration(day.get("seconds", 0))

    if cache_changed:
        save_metadata_cache(metadata)

    res = {
        "total_vods": len(vods),
        "total_clips": len(clips),
        "total_size_str": format_bytes(total_size),
        "total_duration": total_duration,
        "total_duration_str": compact_duration(total_duration),
        "duration_known": duration_known,
        "duration_partial": duration_known < len(vods),
        "active_days": len(active_days),
        "avg_per_active_day": round(len(vods) / len(active_days), 1) if active_days else 0,
        "favorite_weekday": max(weekday_counts, key=weekday_counts.get) if weekday_counts else "-",
        "common_start": max(hour_counts, key=hour_counts.get) if hour_counts else "-",
        "calendar": calendar,
        "top_days": top_days,
        "categories": sorted(
            [{"name": key, "count": value} for key, value in category_counts.items()],
            key=lambda item: (-item["count"], item["name"].lower()),
        ),
        "platforms": sorted(
            [{"name": key, "count": value} for key, value in platform_counts.items()],
            key=lambda item: (-item["count"], item["name"].lower()),
        ),
    }
    if has_request_context():
        g._cached_streamer_stats[streamer] = dict(res)
    return res


_VOD_LIST_CACHE = {} # {streamer: {ts: time, items: []}}
VOD_CACHE_TTL = 180  # Cache auf 180 Sekunden (3 Minuten) erhoehen fuer bessere Performance

def list_vods(
    streamer,
    q=None,
    sort="newest",
    show_empty=False,
    page=1,
    page_size=PAGE_SIZE,
    filter_type="vod",
    category=None,
    platform=None,
):
    now = time.time()
    token = _media_cache_token()
    cached = _VOD_LIST_CACHE.get(streamer)
    if cached and (now - cached["ts"] < VOD_CACHE_TTL) and cached.get("token") == token:
        items = list(cached["items"])
    else:
        items = collect_streamer_media_items(streamer)
        _VOD_LIST_CACHE[streamer] = {"ts": now, "items": list(items), "token": token}

    metadata = get_metadata_cache()
    cache_changed = False
    for item in items:
        cache_changed = apply_media_metadata(streamer, item, metadata) or cache_changed

    # Counts for Tabs
    cnt_vod = sum(1 for i in items if i["route"] == "vod")
    cnt_clip = sum(1 for i in items if i["route"] == "clip")

    if filter_type == "vod":
        items = [i for i in items if i["route"] == "vod"]
    elif filter_type == "clip":
        items = [i for i in items if i["route"] == "clip"]

    if not show_empty:
        items = [i for i in items if i.get("size_bytes", 0) > 0]
    if q:
        q_l = q.casefold()
        items = [
            i for i in items
            if q_l in " ".join([
                i.get("name", ""),
                i.get("title", ""),
                i.get("category", ""),
                i.get("platform", ""),
                i.get("date_key", ""),
            ]).casefold()
        ]
    if category:
        category_l = category.casefold()
        items = [i for i in items if i.get("category", "").casefold() == category_l]
    if platform:
        platform_l = platform.casefold()
        items = [i for i in items if i.get("platform", "").casefold() == platform_l]

    def _sort_ts(item):
        try:
            return float(item.get("recorded_ts") or item.get("mtime") or 0)
        except (TypeError, ValueError):
            return 0.0

    # VODs/Clips: default and "newest" always by recording date (filename), newest first.
    sort_key = (sort or "newest").strip().lower()
    if sort_key == "oldest":
        items.sort(key=lambda x: (_sort_ts(x), str(x.get("name") or "").lower()))
    elif sort_key == "size_desc":
        items.sort(key=lambda x: x.get("size_bytes", 0), reverse=True)
    elif sort_key == "size_asc":
        items.sort(key=lambda x: x.get("size_bytes", 0))
    elif sort_key == "name":
        items.sort(key=lambda x: x["name"].lower())
    elif sort_key == "duration":
        items.sort(
            key=lambda x: (
                float(x.get("duration_sec") or (metadata.get(f"{streamer}/{x['name']}") or {}).get("duration") or 0),
                _sort_ts(x),
            ),
            reverse=True,
        )
    else:
        items.sort(key=lambda x: (_sort_ts(x), str(x.get("name") or "").lower()), reverse=True)
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    
    page_items = items[start:end]
    try:
        archive_slugs = related_archive_slugs(streamer)
    except Exception:
        archive_slugs = []

    for item in page_items:
        fid = f"{streamer}/{item['name']}"

        cached = metadata.get(fid)
        cached_dur = 0.0
        if cached and "duration" in cached:
            try:
                cached_dur = float(cached.get("duration") or 0)
            except (TypeError, ValueError):
                cached_dur = 0.0
        if cached and cached.get("mtime") == item["mtime"] and cached_dur > 0:
            dur = cached_dur
        elif item.get("full_path") and os.path.isfile(item.get("full_path") or ""):
            dur = get_duration(item.get("full_path") or "")
            if dur > 0:
                record = metadata.setdefault(fid, {})
                record["mtime"] = item["mtime"]
                record["duration"] = dur
                cache_changed = True
        else:
            dur = 0.0
        item["duration_sec"] = float(dur or 0)
        item["duration_str"] = format_seconds(dur)
        item["watch_key"] = vod_watch_key(streamer, item["name"])
        item["done_key"] = vod_done_key(streamer, item["name"])
        
        gen_dir, gen_path, lock_path = thumb_paths(streamer, item["name"])
        thumb_dir, thumb_path, _ = resolve_thumb_paths(streamer, item["name"], slugs=archive_slugs)
        if (
            gen_path
            and not os.path.exists(gen_path)
            and item.get("size_bytes", 0) > 0
            and item.get("full_path")
        ):
            start_thumb_generate(item["full_path"], gen_dir, gen_path, lock_path)
        thumb_version = int(os.path.getmtime(thumb_path)) if thumb_path and os.path.exists(thumb_path) else int(item["mtime"])
        try:
            item["thumb_url"] = url_for(
                "thumb_view",
                streamer=streamer,
                filename=item["name"],
                v=thumb_version,
            )
        except Exception:
            from urllib.parse import quote
            item["thumb_url"] = f"/thumb/{quote(streamer)}/{quote(item['name'])}?v={thumb_version}"

    if cache_changed:
        save_metadata_cache(metadata)

    return page_items, total, total_pages, page, cnt_vod, cnt_clip


def resolve_media_path(streamer, filename, route):
    if os.path.basename(filename) != filename:
        return None
    if route != "file" and not filename.endswith(".mp4"):
        return None
    if route == "file":
        return resolve_resource_file_path(filename)
    candidates = []
    arch = archive_name_for(streamer)
    if arch:
        candidates.append(arch)
    if streamer:
        candidates.append(streamer)
    try:
        candidates.extend(related_archive_slugs(streamer))
    except Exception:
        pass
    seen = set()
    files_dir = getattr(config, "FILES_DIR", None) or os.path.join(BASE_DIR, "files")
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        for root in (BASE_DIR, files_dir):
            safe_dir = safe_join(root, cand)
            if not safe_dir or not os.path.isdir(safe_dir):
                continue
            if route == "clip":
                clip_dir = safe_join(safe_dir, CLIP_SUBDIR)
                file_path = safe_join(clip_dir, filename) if clip_dir else None
            else:
                file_path = safe_join(safe_dir, filename)
            if file_path and os.path.isfile(file_path):
                return file_path
    return None


def chat_log_path(streamer, filename):
    """Local chat JSONL path for a VOD."""
    if os.path.basename(filename) != filename or not filename.endswith(".mp4"):
        return None
    safe_dir = streamer_dir(streamer)
    if not safe_dir:
        return None
    base = os.path.splitext(filename)[0]
    return os.path.join(safe_dir, CHAT_LOG_SUBDIR, f"{base}.jsonl")


def ensure_vod_chat_path(streamer, filename):
    """Return local chat log path if present."""
    path = chat_log_path(streamer, filename)
    return path if path and os.path.isfile(path) and os.path.getsize(path) > 0 else None


def _platform_kind(value):
    text = str(value or "").strip().lower()
    if "youtube" in text or text in ("yt", "youtu.be"):
        return "youtube"
    if "twitch" in text or text in ("tw",):
        return "twitch"
    if "_yt_" in text:
        return "youtube"
    if "_tw_" in text:
        return "twitch"
    return ""


def build_chat_jump_markers(log_path, duration_sec, bucket_sec=30, limit=24):
    """Find chat activity spikes for scrubber jump marks."""
    try:
        duration = float(duration_sec or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if not log_path or not os.path.isfile(log_path) or duration <= 30:
        return []
    bucket = max(10, int(bucket_sec or 30))
    buckets = {}
    samples = 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                rel = data.get("rel")
                if rel is None:
                    continue
                try:
                    rel_val = float(rel)
                except (TypeError, ValueError):
                    continue
                if rel_val < 0 or rel_val > duration + 5:
                    continue
                key = int(rel_val // bucket)
                slot = buckets.setdefault(key, {"count": 0, "emotes": 0, "mods": 0, "sample": ""})
                slot["count"] += 1
                samples += 1
                emotes = data.get("emotes") or []
                if isinstance(emotes, list) and emotes:
                    slot["emotes"] += 1
                badges = data.get("badges") or []
                if isinstance(badges, list):
                    for badge in badges:
                        set_id = str(
                            (badge or {}).get("set_id")
                            or (badge or {}).get("set")
                            or (badge or {}).get("name")
                            or ""
                        ).lower()
                        if set_id in ("moderator", "broadcaster", "staff", "admin"):
                            slot["mods"] += 1
                            break
                if not slot["sample"]:
                    user = str(data.get("user") or "").strip()
                    msg = str(data.get("msg") or "").strip()
                    if user and msg:
                        slot["sample"] = f"{user}: {msg[:60]}"
                if samples >= 80000:
                    break
    except OSError:
        return []
    if not buckets:
        return []
    scored = []
    for key, slot in buckets.items():
        score = slot["count"] + (slot["emotes"] * 1.5) + (slot["mods"] * 2.5)
        scored.append((score, key, slot))
    scored.sort(reverse=True)
    # Keep peaks that beat a robust baseline (not the peak itself as median).
    scores = [item[0] for item in scored]
    sorted_scores = sorted(scores)
    if not sorted_scores:
        threshold = 8.0
    elif len(sorted_scores) == 1:
        threshold = max(6.0, sorted_scores[0] * 0.5)
    else:
        # Baseline from the quieter majority so sparse logs still surface real spikes.
        cutoff = max(1, int(len(sorted_scores) * 0.7))
        baseline = sum(sorted_scores[:cutoff]) / cutoff
        threshold = max(8.0, baseline * 2.0)
    markers = []
    used = []
    for score, key, slot in scored:
        if score < threshold:
            continue
        t = min(duration, max(0.0, (key * bucket) + (bucket / 2.0)))
        if any(abs(t - prev) < bucket for prev in used):
            continue
        kind = "chat"
        if slot["mods"] >= 2:
            kind = "mod"
        elif slot["emotes"] >= max(3, slot["count"] // 3):
            kind = "emote"
        markers.append({
            "t": round(t, 1),
            "score": round(score, 1),
            "count": slot["count"],
            "kind": kind,
            "label": slot["sample"] or f"{slot['count']} Nachrichten",
            "pct": round((t / duration) * 100.0, 2) if duration else 0,
        })
        used.append(t)
        if len(markers) >= limit:
            break
    markers.sort(key=lambda item: item["t"])
    return markers


def find_related_recordings(streamer, filename, meta=None, limit=4):
    """Find likely same-session recordings on the other platform in a shared archive."""
    safe_dir = streamer_dir(streamer)
    if not safe_dir or not os.path.isdir(safe_dir):
        return []
    current_path = resolve_media_path(streamer, filename, "vod")
    try:
        current_mtime = os.path.getmtime(current_path) if current_path else None
    except OSError:
        current_mtime = None
    current_ts = parse_timestamp_from_filename(filename, current_mtime)
    current_platform = _platform_kind((meta or {}).get("platform")) or _platform_kind(filename)
    current_title = clean_metadata_label((meta or {}).get("title") or (meta or {}).get("display_title") or "")
    related = []
    metadata = get_metadata_cache()
    try:
        entries = [e for e in os.scandir(safe_dir) if e.is_file() and is_archive_mp4_filename(e.name)]
    except OSError:
        return []
    for entry in entries:
        if entry.name == filename:
            continue
        other_platform = _platform_kind(entry.name)
        fid = f"{streamer}/{entry.name}"
        record = metadata.get(fid) or {}
        if not other_platform:
            other_platform = _platform_kind(record.get("platform"))
        if current_platform and other_platform and other_platform == current_platform:
            continue
        try:
            other_mtime = entry.stat().st_mtime
        except OSError:
            continue
        other_ts = parse_timestamp_from_filename(entry.name, other_mtime)
        if abs(other_ts - current_ts) > 3 * 3600:
            continue
        other_title = clean_metadata_label(record.get("title") or record.get("display_title") or "")
        title_score = 0
        if current_title and other_title:
            a = set(current_title.casefold().split())
            b = set(other_title.casefold().split())
            if a and b:
                title_score = len(a & b) / max(1, len(a | b))
        score = (1.0 - min(1.0, abs(other_ts - current_ts) / (3 * 3600))) + (title_score * 0.5)
        if current_platform and other_platform and other_platform != current_platform:
            score += 0.35
        related.append({
            "name": entry.name,
            "platform": other_platform or record.get("platform") or "",
            "display_title": other_title or entry.name,
            "date_str": parse_date_from_filename(entry.name, other_mtime),
            "url": url_for("vod_view", streamer=streamer, filename=entry.name),
            "score": score,
            "delta_min": int(round(abs(other_ts - current_ts) / 60.0)),
        })
    related.sort(key=lambda item: item["score"], reverse=True)
    return related[:limit]


def build_chat_index(log_path):
    index = [(0.0, 0)]
    line_count = 0
    try:
        with open(log_path, "rb") as f:
            while True:
                offset = f.tell()
                line = f.readline()
                if not line:
                    break
                line_count += 1
                if line_count % CHAT_INDEX_STEP != 0:
                    continue
                try:
                    data = json.loads(line.decode("utf-8", errors="ignore"))
                    rel = data.get("rel")
                    if rel is None:
                        continue
                    rel_val = float(rel)
                    index.append((rel_val, offset))
                except Exception:
                    continue
    except Exception:
        return None
    return index

def get_chat_index(log_path):
    try:
        st = os.stat(log_path)
    except OSError:
        return None
    if st.st_size < CHAT_INDEX_MIN_SIZE:
        return None
    cached = CHAT_INDEX_CACHE.get(log_path)
    if cached and cached.get("mtime") == st.st_mtime and cached.get("size") == st.st_size:
        return cached.get("index")
    index = build_chat_index(log_path)
    if index:
        CHAT_INDEX_CACHE[log_path] = {"mtime": st.st_mtime, "size": st.st_size, "index": index}
    return index

CHAT_EMOTE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CHAT_BADGE_PART_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CHAT_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

def chat_emotes_from_row(data):
    raw_emotes = data.get("emotes")
    if not isinstance(raw_emotes, list):
        return []
    emotes = []
    for raw in raw_emotes[:80]:
        if not isinstance(raw, dict):
            continue
        emote_id = str(raw.get("id", "") or "").strip()
        if not CHAT_EMOTE_ID_RE.match(emote_id):
            continue
        try:
            start = int(raw.get("start"))
            end = int(raw.get("end"))
        except (TypeError, ValueError):
            continue
        if start < 0 or end < start or end - start > 128:
            continue
        emotes.append({"id": emote_id, "start": start, "end": end})
    return emotes

def chat_badges_from_row(data):
    raw_badges = data.get("badges")
    if not isinstance(raw_badges, list):
        return []
    badges = []
    for raw in raw_badges[:24]:
        if not isinstance(raw, dict):
            continue
        set_id = str(raw.get("set_id", "") or "").strip()
        badge_id = str(raw.get("id", "") or "").strip()
        if not CHAT_BADGE_PART_RE.match(set_id) or not CHAT_BADGE_PART_RE.match(badge_id):
            continue
        badge = {"set_id": set_id, "id": badge_id}
        info = str(raw.get("info", "") or "").strip()
        if info and CHAT_BADGE_PART_RE.match(info):
            badge["info"] = info
        badges.append(badge)
    return badges

def chat_color_from_row(data):
    color = str(data.get("color", "") or "").strip()
    if CHAT_COLOR_RE.match(color):
        return color
    return ""


def chat_reward_from_row(data):
    raw = data.get("reward") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    reward_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip() or "Channel Points"
    try:
        cost = int(raw.get("cost") or 0)
    except (TypeError, ValueError):
        cost = 0
    if not reward_id and title == "Channel Points":
        return None
    out = {"title": title}
    if reward_id:
        out["id"] = reward_id
    if cost > 0:
        out["cost"] = cost
    return out


def chat_event_from_row(data):
    return chat_events.sanitize_event(data.get("event") if isinstance(data, dict) else None)


def chat_item_from_row(data, *, time_key):
    """Build a sanitized chat API item. time_key is 't' (live) or 'rel' (vod)."""
    if not isinstance(data, dict):
        return None
    raw_time = data.get(time_key)
    if raw_time is None:
        return None
    try:
        time_val = float(raw_time)
    except Exception:
        return None
    user = str(data.get("user", "") or "").strip()
    msg = str(data.get("msg", "") or "")
    msg_key = " ".join(msg.split())
    reward = chat_reward_from_row(data)
    event = chat_event_from_row(data)
    if event and not msg_key:
        msg = chat_events.format_moderation_message(
            event.get("type"),
            user,
            event.get("seconds"),
        )
        msg_key = " ".join(msg.split())
    if not msg_key and not reward and not event:
        return None
    item = {time_key: time_val, "user": user, "msg": msg}
    if reward:
        item["reward"] = reward
    if event:
        item["event"] = event
    emotes = chat_emotes_from_row(data)
    if emotes:
        item["emotes"] = emotes
    badges = chat_badges_from_row(data)
    if badges:
        item["badges"] = badges
    color = chat_color_from_row(data)
    if color:
        item["color"] = color
    dedupe_token = msg_key
    if event:
        dedupe_token = (
            f"event|{event.get('type')}|{event.get('seconds') or ''}|"
            f"{user}|{msg_key}"
        )
    elif reward:
        dedupe_token = f"reward|{reward.get('id') or reward.get('title')}|{user}|{msg_key}"
    return item, dedupe_token, time_val, user


def chat_message_matches_word(message, query):
    needle = str(query or "").strip()
    if not needle:
        return True
    return needle.casefold() in str(message or "").casefold()

def highlight_chat_message(message, query):
    text = str(message or "")
    needle = str(query or "").strip()
    if not needle:
        return Markup(escape(text))
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    parts = []
    cursor = 0
    for match in pattern.finditer(text):
        parts.append(escape(text[cursor:match.start()]))
        parts.append(Markup('<mark class="log-word-hit">') + escape(match.group(0)) + Markup("</mark>"))
        cursor = match.end()
    parts.append(escape(text[cursor:]))
    return Markup("").join(parts)

def chat_vod_url_for_log(streamer, log_filename, rel, preroll_seconds=10):
    base_name = os.path.splitext(str(log_filename or ""))[0]
    if not base_name:
        return ""
    try:
        rel_float = float(rel)
    except (TypeError, ValueError):
        rel_float = 0.0
    start_at = max(0.0, rel_float - preroll_seconds)
    return url_for("vod_view", streamer=streamer, filename=f"{base_name}.mp4") + f"?t={start_at:g}"


def vod_watch_key(streamer, filename):
    return f"vod_pos_v2_{streamer}_{filename}"


def vod_done_key(streamer, filename):
    return f"vod_done_v2_{streamer}_{filename}"


def vod_seek_profile(file_path):
    profile = {"needs_repair": False, "container": "mp4", "error": ""}
    try:
        if is_fragmented_mp4(file_path):
            profile["needs_repair"] = True
            profile["container"] = "fragmented-mp4"
    except Exception as exc:
        logging.warning("Could not inspect MP4 seek profile for %s: %s", file_path, exc)
        profile["error"] = gettext("MP4 check not possible")
    return profile


def active_vod_remux_task(streamer, filename):
    try:
        rows = database.list_tasks(limit=200)
    except Exception as exc:
        logging.error("Could not inspect remux task queue: %s", exc)
        return None
    for row in rows:
        if row.get("type") != "remux_vod":
            continue
        if row.get("status") not in ("pending", "running"):
            continue
        try:
            payload = json.loads(row.get("payload") or "{}")
        except Exception:
            payload = {}
        if payload.get("streamer") == streamer and payload.get("filename") == filename:
            return row
    return None


def enqueue_vod_remux(streamer, filename, reencode_audio=False):
    existing = active_vod_remux_task(streamer, filename)
    if existing:
        status = existing.get("status") or "pending"
        return False, gettext("Seek repair is already running (%(status)s).", status=status)
    database.add_task(
        "remux_vod",
        {
            "streamer": streamer,
            "filename": filename,
            "reencode_audio": bool(reencode_audio),
        },
    )
    return True, gettext("Seek repair queued.")


def format_age_short(ts):
    try:
        seconds = max(0, int(time.time() - float(ts)))
    except (TypeError, ValueError):
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def task_payload_label(task_type, payload):
    if not isinstance(payload, dict):
        return ""
    candidates = []
    for key in ("media", "input_path", "thumb_path", "path", "transcript"):
        value = payload.get(key)
        if value:
            candidates.append(os.path.basename(str(value)))
    if payload.get("streamer"):
        candidates.insert(0, str(payload.get("streamer")))
    if payload.get("filename"):
        candidates.append(os.path.basename(str(payload.get("filename"))))
    if task_type == "remux_all":
        return gettext("All VODs")
    return " / ".join(candidates[:2])


def task_queue_summary(limit=30):
    counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    tasks = []
    try:
        rows = database.list_tasks(limit=limit)
    except Exception as exc:
        logging.error("Could not load task queue: %s", exc)
        return {"counts": counts, "tasks": [], "total_active": 0, "error": gettext("Queue not readable")}

    for row in rows:
        status = str(row.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
        try:
            payload = json.loads(row.get("payload") or "{}")
        except Exception:
            payload = {}
        updated_at = row.get("updated_at") or row.get("created_at") or 0
        tasks.append({
            "id": row.get("id"),
            "type": row.get("type") or "-",
            "status": status,
            "label": task_payload_label(row.get("type"), payload),
            "attempts": int(row.get("attempts") or 0),
            "error": str(row.get("error") or "")[:160],
            "age": format_age_short(updated_at),
            "updated": time.strftime("%d.%m. %H:%M", time.localtime(updated_at)) if updated_at else "-",
        })
    total_active = counts.get("pending", 0) + counts.get("running", 0)
    return {"counts": counts, "tasks": tasks, "total_active": total_active, "error": ""}


def log_display_from_filename(log_file):
    base_name = os.path.splitext(log_file)[0]
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", base_name)
    if not match:
        match = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", base_name)
    if match:
        y, mo, d, h, mi, s = match.groups()
        return f"{d}.{mo}.{y} {h}:{mi}:{s}"
    return base_name


def global_search(query):
    needle = str(query or "").strip()
    if not needle:
        return {"streamers": [], "videos": [], "chat": []}
    needle_l = needle.casefold()
    results = {"streamers": [], "videos": [], "chat": []}

    streamers = visible_streamers()
    for streamer in streamers:
        name = streamer.get("name", "")
        if needle_l in name.casefold():
            results["streamers"].append(streamer)

    for streamer in streamers:
        if len(results["videos"]) >= GLOBAL_SEARCH_VIDEO_LIMIT:
            break
        name = streamer.get("name", "")
        files, _, _, _, _, _ = list_vods(
            name,
            q=needle,
            sort="newest",
            filter_type="all",
            show_empty=False,
            page=1,
            page_size=max(1, GLOBAL_SEARCH_VIDEO_LIMIT - len(results["videos"])),
        )
        for item in files:
            results["videos"].append({
                "streamer": name,
                "name": item["name"],
                "route": item["route"],
                "date_str": item.get("date_str", ""),
                "duration_str": item.get("duration_str", ""),
                "url": url_for("vod_view" if item["route"] == "vod" else "clip_view", streamer=name, filename=item["name"]),
            })

    active_archives = active_recording_archives()
    if len(needle) >= 2 and os.path.isdir(BASE_DIR):
        for streamer_name in sorted(os.listdir(BASE_DIR)):
            if len(results["chat"]) >= GLOBAL_SEARCH_CHAT_LIMIT:
                break
            if active_archives and streamer_name not in active_archives:
                continue
            chat_dir = os.path.join(BASE_DIR, streamer_name, CHAT_LOG_SUBDIR)
            if not os.path.isdir(chat_dir):
                continue
            try:
                log_files = [f for f in os.listdir(chat_dir) if f.endswith(".jsonl")]
            except OSError:
                continue
            log_files.sort(key=lambda name: os.path.getmtime(os.path.join(chat_dir, name)), reverse=True)
            for log_file in log_files:
                if len(results["chat"]) >= GLOBAL_SEARCH_CHAT_LIMIT:
                    break
                full_path = os.path.join(chat_dir, log_file)
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as handle:
                        for line in handle:
                            if len(results["chat"]) >= GLOBAL_SEARCH_CHAT_LIMIT:
                                break
                            try:
                                data = json.loads(line)
                            except Exception:
                                continue
                            msg = str(data.get("msg", "") or "")
                            user = str(data.get("user", "") or "")
                            if needle_l not in msg.casefold() and needle_l not in user.casefold():
                                continue
                            rel = data.get("rel", 0)
                            results["chat"].append({
                                "streamer": streamer_name,
                                "user": user,
                                "time_str": format_seconds(rel),
                                "log_display": log_display_from_filename(log_file),
                                "msg_html": highlight_chat_message(msg, needle),
                                "link": chat_vod_url_for_log(streamer_name, log_file, rel),
                            })
                except OSError:
                    continue
    return results


# Cache fuer current_recording_info zur Performance-Optimierung
_RECORDING_INFO_CACHE = {}
_RECORDING_INFO_CACHE_TTL = 10  # 10 Sekunden

def _recording_media_candidates(safe_dir, data):
    """Local media paths for a .current_recording*.json payload (mp4/mkv + .part)."""
    if not safe_dir or not isinstance(data, dict):
        return []
    names = []
    for key in ("record_file", "filename"):
        name = os.path.basename(str(data.get(key) or ""))
        if name and name not in names:
            names.append(name)
    for name in list(names):
        stem, ext = os.path.splitext(name)
        if not stem:
            continue
        if ext.lower() == ".mp4":
            alt = f"{stem}.mkv"
        elif ext.lower() == ".mkv":
            alt = f"{stem}.mp4"
        else:
            continue
        if alt not in names:
            names.append(alt)
    paths = []
    for name in names:
        paths.append(os.path.join(safe_dir, name))
        paths.append(os.path.join(safe_dir, f"{name}.part"))
    return paths


def current_recording_info(streamer):
    safe_dir = streamer_dir(streamer)
    if not safe_dir:
        return None

    # Schneller Cache Check
    cached = _RECORDING_INFO_CACHE.get(streamer)
    now = time.time()
    if cached and (now - cached["ts"] < _RECORDING_INFO_CACHE_TTL):
        return cached["data"]

    best = None
    best_mtime = -1.0
    for path in _iter_runtime_state_files(
        safe_dir, ".current_recording.json", ".current_recording.", ".json"
    ):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            if not (data.get("filename") or data.get("record_file")):
                continue
            active_path = None
            media_mtime = -1.0
            for candidate in _recording_media_candidates(safe_dir, data):
                if not os.path.exists(candidate):
                    continue
                try:
                    candidate_mtime = os.path.getmtime(candidate)
                except OSError:
                    continue
                # If video hasn't been written to in 60 seconds, assume dead
                if now - candidate_mtime > 60:
                    continue
                if candidate_mtime >= media_mtime:
                    media_mtime = candidate_mtime
                    active_path = candidate
            if not active_path:
                continue
            try:
                state_mtime = os.path.getmtime(path)
            except OSError:
                state_mtime = media_mtime
            if state_mtime >= best_mtime:
                best_mtime = state_mtime
                best = data
        except Exception:
            continue

    _RECORDING_INFO_CACHE[streamer] = {"ts": now, "data": best}
    return best


def hls_paths(streamer, filename):
    if os.path.basename(filename) != filename:
        return None, None, None
    base = os.path.splitext(filename)[0]
    hls_dir = safe_join(config.HLS_ROOT, streamer, base)
    if not hls_dir:
        return None, None, None
    playlist = os.path.join(hls_dir, "index.m3u8")
    lock = os.path.join(hls_dir, ".lock")
    return hls_dir, playlist, lock


def live_paths(streamer):
    live_dir = safe_join(config.LIVE_HLS_ROOT, streamer)
    if not live_dir:
        return None, None
    for name in ("stream.m3u8", "index.m3u8"):
        candidate = os.path.join(live_dir, name)
        if os.path.isfile(candidate):
            return live_dir, candidate
    playlist = os.path.join(live_dir, "stream.m3u8")
    return live_dir, playlist

_LIVE_ASSERT_TS = {}
_LIVE_ASSERT_INTERVAL_SEC = 15.0


def touch_live_heartbeat(streamer):
    """Signal livebot that a viewer wants HLS (creates dir even before playlist exists)."""
    live_dir, _ = live_paths(streamer)
    if not live_dir:
        return
    try:
        os.makedirs(live_dir, exist_ok=True)
        try:
            os.chmod(live_dir, 0o777)
        except OSError:
            pass
        heartbeat = os.path.join(live_dir, ".heartbeat")
        with open(heartbeat, "a", encoding="utf-8"):
            os.utime(heartbeat, None)
        try:
            os.chmod(heartbeat, 0o666)
        except OSError:
            pass
        demand_dir = os.path.join(config.META_DIR, "live_watch_demand")
        os.makedirs(demand_dir, exist_ok=True)
        try:
            os.chmod(demand_dir, 0o777)
        except OSError:
            pass
        channel = os.path.basename(live_dir)
        demand_path = os.path.join(demand_dir, f"{channel}.ts")
        with open(demand_path, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
        try:
            os.chmod(demand_path, 0o666)
        except OSError:
            pass
        # Re-assert live=1 while watch UI is open, but throttle DB writes.
        now = time.time()
        last = float(_LIVE_ASSERT_TS.get(channel) or 0.0)
        if now - last >= _LIVE_ASSERT_INTERVAL_SEC:
            _LIVE_ASSERT_TS[channel] = now
            try:
                add_live_channel(channel)
            except Exception:
                pass
    except Exception:
        pass


def live_playlist_fresh(streamer):
    _, playlist = live_paths(streamer)
    if not playlist:
        return False
    if not os.path.isfile(playlist):
        return False
    try:
        age = time.time() - os.path.getmtime(playlist)
    except OSError:
        return False
    if age <= LIVE_HLS_TTL_SEC:
        return True
    # Playlist mtime can stall briefly while ffmpeg still writes segments.
    live_dir, _ = live_paths(streamer)
    if not live_dir or not os.path.isdir(live_dir):
        return False
    newest = 0.0
    try:
        for name in os.listdir(live_dir):
            if not name.endswith(".ts"):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(live_dir, name)))
            except OSError:
                continue
    except OSError:
        return False
    return newest > 0 and (time.time() - newest) <= LIVE_HLS_TTL_SEC


def live_playlist_exists(streamer):
    return live_playlist_fresh(streamer)


def delete_video_file(streamer, filename, is_clip):
    route = "clip" if is_clip else "vod"
    filename = os.path.basename(str(filename or ""))
    if not filename or filename in (".", ".."):
        return False
    file_path = resolve_media_path(streamer, filename, route)
    removed = False
    if file_path:
        try:
            os.remove(file_path)
            removed = True
        except OSError:
            return False

    try:
        deleted_by = current_username()
    except Exception:
        deleted_by = None
    tombstone_slugs = set()
    try:
        tombstone_slugs.update(related_archive_slugs(streamer) or [])
    except Exception:
        pass
    primary = archive_name_for(streamer) or str(streamer or "").strip()
    if primary:
        tombstone_slugs.add(primary)
    if streamer:
        tombstone_slugs.add(str(streamer).strip())
    chat_name = ""
    if not is_clip:
        stem, _ = os.path.splitext(filename)
        chat_name = f"{stem}.jsonl" if stem else ""
    for slug in tombstone_slugs:
        if not slug:
            continue
        database.upsert_deleted_media(slug, filename, file_id=None, deleted_by=deleted_by)
        if chat_name:
            database.upsert_deleted_media(slug, chat_name, deleted_by=deleted_by)

    if not removed:
        removed = True
    remove_shares_for(streamer, filename, route)
    if not is_clip:
        hls_dir, _, _ = hls_paths(streamer, filename)
        if hls_dir and os.path.isdir(hls_dir):
            shutil.rmtree(hls_dir, ignore_errors=True)
        thumb_dir, thumb_path, lock_path = thumb_paths(streamer, filename)
        paths_to_clear = {thumb_path, lock_path}
        try:
            for slug in related_archive_slugs(streamer):
                _td, alt_path, alt_lock = thumb_paths(slug, filename)
                paths_to_clear.add(alt_path)
                paths_to_clear.add(alt_lock)
        except Exception:
            pass
        for path in paths_to_clear:
            if not path:
                continue
            try:
                os.remove(path)
            except OSError:
                pass
        chap_path = chapter_path(streamer, filename)
        if chap_path:
            try:
                os.remove(chap_path)
            except OSError:
                pass
    _VOD_LIST_CACHE.pop(streamer, None)
    STREAMER_CACHE["ts"] = 0
    bust_media_cache()
    try:
        invalidate_dashboard_media_cache()
    except Exception:
        pass
    return True


def normalize_vod_rename_filename(new_name, old_name):
    """Return a safe basename for rename, preserving the original extension."""
    old = os.path.basename(str(old_name or "").strip())
    raw = os.path.basename(str(new_name or "").strip())
    if not old or not raw or raw in (".", "..") or "/" in raw or "\\" in raw:
        return None
    old_ext = os.path.splitext(old)[1].lower() or ".mp4"
    stem, ext = os.path.splitext(raw)
    if not stem:
        return None
    if not ext:
        ext = old_ext
    elif ext.lower() != old_ext:
        return None
    safe_stem = sanitize_name(stem)
    if not safe_stem:
        return None
    candidate = f"{safe_stem}{old_ext}"
    if len(candidate) > 180:
        return None
    return candidate


def _rename_local_path(src, dest):
    if not src or not dest or src == dest:
        return False
    if not os.path.exists(src):
        return False
    if os.path.exists(dest):
        raise OSError(gettext("Target already exists: %(path)s", path=dest))
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    os.rename(src, dest)
    return True


def retarget_shares_filename(streamer, old_filename, new_filename, route):
    shares = load_shares()
    changed = False
    for token, info in list(shares.items()):
        if not isinstance(info, dict):
            continue
        if (
            info.get("streamer") == streamer
            and info.get("filename") == old_filename
            and info.get("route") == route
        ):
            info["filename"] = new_filename
            shares[token] = info
            changed = True
    if changed:
        save_shares(shares)
    return changed


def update_vod_info_fields(streamer, filename, *, title=None, description=None):
    """Admin-editable display title + description in metadata.json."""
    filename = os.path.basename(str(filename or ""))
    if not streamer or not filename:
        return False, gettext("Invalid file.")
    metadata = get_metadata_cache()
    key = f"{streamer}/{filename}"
    record = metadata.setdefault(key, {})
    if title is not None:
        record["title"] = clean_metadata_label(title, 180)
        record["title_locked"] = True
    if description is not None:
        record["description"] = clean_vod_description(description)
    save_metadata_cache(metadata)
    _VOD_LIST_CACHE.pop(streamer, None)
    return True, gettext("Saved.")


def rename_video_file(streamer, old_filename, new_filename, is_clip=False):
    """Rename a VOD/clip locally; retarget metadata, chat, thumbs, shares."""
    route = "clip" if is_clip else "vod"
    old_name = os.path.basename(str(old_filename or "").strip())
    new_name = normalize_vod_rename_filename(new_filename, old_name)
    if not streamer or not old_name or not new_name:
        return False, gettext("Invalid file name."), old_name
    if new_name == old_name:
        return True, gettext("Unchanged."), new_name

    if resolve_media_path(streamer, new_name, route):
        return False, gettext("Target name already exists locally."), old_name

    old_path = resolve_media_path(streamer, old_name, route)
    if not old_path:
        return False, gettext("File not found locally."), old_name

    new_path = os.path.join(os.path.dirname(old_path), new_name)
    try:
        _rename_local_path(old_path, new_path)
    except OSError as exc:
        return False, gettext("Local rename failed: %(error)s", error=exc), old_name
    for sidecar in sidecar_metadata_candidates(old_path):
        if not os.path.isfile(sidecar):
            continue
        dest_side = sidecar.replace(old_name, new_name, 1)
        try:
            _rename_local_path(sidecar, dest_side)
        except OSError:
            pass

    if not is_clip:
        try:
            old_chat_path = chat_log_path(streamer, old_name)
            new_chat_path = chat_log_path(streamer, new_name)
            if old_chat_path and new_chat_path and os.path.isfile(old_chat_path):
                _rename_local_path(old_chat_path, new_chat_path)
        except OSError as exc:
            logging.warning("Chat-Rename lokal fehlgeschlagen: %s", exc)

        try:
            _td, old_thumb, old_lock = thumb_paths(streamer, old_name)
            _td2, new_thumb, new_lock = thumb_paths(streamer, new_name)
            if old_thumb and new_thumb and os.path.isfile(old_thumb):
                _rename_local_path(old_thumb, new_thumb)
            if old_lock and new_lock and os.path.isfile(old_lock):
                _rename_local_path(old_lock, new_lock)
        except OSError:
            pass
        hls_dir, _, _ = hls_paths(streamer, old_name)
        if hls_dir and os.path.isdir(hls_dir):
            shutil.rmtree(hls_dir, ignore_errors=True)
        chap_old = chapter_path(streamer, old_name)
        chap_new = chapter_path(streamer, new_name)
        if chap_old and chap_new and os.path.isfile(chap_old):
            try:
                _rename_local_path(chap_old, chap_new)
            except OSError:
                pass

    metadata = get_metadata_cache()
    old_key = f"{streamer}/{old_name}"
    new_key = f"{streamer}/{new_name}"
    if old_key in metadata:
        record = dict(metadata.get(old_key) or {})
        metadata.pop(old_key, None)
        metadata[new_key] = {**(metadata.get(new_key) or {}), **record}
        save_metadata_cache(metadata)

    retarget_shares_filename(streamer, old_name, new_name, route)

    _VOD_LIST_CACHE.pop(streamer, None)
    STREAMER_CACHE["ts"] = 0
    try:
        bust_media_cache()
    except Exception:
        pass

    return True, gettext("Renamed."), new_name


def start_hls_generate(input_path, hls_dir, lock_path):
    os.makedirs(hls_dir, exist_ok=True)
    if os.path.exists(lock_path):
        try:
            age = time.time() - os.path.getmtime(lock_path)
        except OSError:
            return
        if age <= 3600:
            return
        try:
            os.remove(lock_path)
        except OSError:
            return
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(str(time.time()))
    log_path = os.path.join(hls_dir, "hls.log")

    import threading
    def run_hls():
        version_token = f"{time.time_ns():x}"
        segment_prefix = f"seg_{version_token}_"
        staging_dir = f"{hls_dir}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        playlist_tmp = os.path.join(staging_dir, "index.m3u8")
        try:
            os.makedirs(staging_dir, exist_ok=False)
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-map", "0:v:0", "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", HLS_VIDEO_PRESET,
                # Main@L4.0 + yuv420p: Safari iOS / Android Chrome safe (avoid High@4.2).
                "-profile:v", "main",
                "-level:v", "4.0",
                "-pix_fmt", "yuv420p",
                "-crf", str(HLS_VIDEO_CRF),
                "-maxrate", HLS_VIDEO_MAXRATE,
                "-bufsize", HLS_VIDEO_BUFSIZE,
                "-g", str(max(2, HLS_TIME) * 30),
                "-keyint_min", str(max(2, HLS_TIME) * 30),
                "-sc_threshold", "0",
                "-force_key_frames", f"expr:gte(t,n_forced*{max(2, HLS_TIME)})",
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "128k",
                "-ac", "2",
                "-ar", "48000",
                "-hls_time", str(HLS_TIME), "-hls_list_size", "0",
                "-hls_playlist_type", "vod",
                "-hls_flags", "independent_segments+temp_file",
                "-hls_segment_filename", os.path.join(staging_dir, f"{segment_prefix}%05d.ts"),
                "-f", "hls", playlist_tmp,
            ]
            with open(log_path, "a", encoding="utf-8") as logf:
                result = subprocess.run(
                    cmd,
                    stdout=logf,
                    stderr=logf,
                    timeout=HLS_GENERATE_TIMEOUT_SEC,
                )
            if result.returncode == 0 and os.path.isfile(playlist_tmp):
                for name in os.listdir(staging_dir):
                    if name.startswith(segment_prefix) and name.endswith(".ts"):
                        os.replace(os.path.join(staging_dir, name), os.path.join(hls_dir, name))
                playlist_ready = os.path.join(hls_dir, f".index-{version_token}.m3u8")
                os.replace(playlist_tmp, playlist_ready)
                os.replace(playlist_ready, os.path.join(hls_dir, "index.m3u8"))

                cutoff = time.time() - HLS_OLD_SEGMENT_TTL_SEC
                for name in os.listdir(hls_dir):
                    if (
                        name.endswith(".ts")
                        and not name.startswith(segment_prefix)
                        and os.path.getmtime(os.path.join(hls_dir, name)) < cutoff
                    ):
                        try:
                            os.remove(os.path.join(hls_dir, name))
                        except OSError:
                            pass
            else:
                logging.error("HLS-Erzeugung fehlgeschlagen fuer %s", input_path)
        except subprocess.TimeoutExpired:
            logging.error("HLS-Erzeugung Timeout fuer %s", input_path)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
            try:
                os.remove(lock_path)
            except OSError:
                pass
    threading.Thread(target=run_hls, daemon=True).start()

def current_user_needs_rules_acceptance() -> bool:
    """True when a logged-in non-admin must accept the current AGB + Hausordnung."""
    from core import tube_terms
    from flask import has_request_context, g

    if not session.get("logged_in"):
        return False
    if (session.get("user_role") or "") == "admin":
        return False
    name = current_username()
    if not name or name == "unknown":
        return False
    if has_request_context() and hasattr(g, "_user_needs_rules"):
        return g._user_needs_rules
    row = database.get_user(name)
    res = not tube_terms.user_has_accepted_current_consent(row)
    if has_request_context():
        g._user_needs_rules = res
    return res


def current_user_needs_terms_acceptance() -> bool:
    return current_user_needs_rules_acceptance()


def _path_requires_rules_acceptance(path: str) -> bool:
    path = str(path or "")
    exempt_prefixes = (
        "/agb",
        "/datenschutz",
        "/regeln",
        "/static/",
        "/logout",
        "/login",
        "/wartung",
        "/maintenance",
        "/healthz",
        "/metrics",
        "/app-icon",
        "/manifest",
        "/sw.",
        "/favicon",
        "/tts/",
        "/alerts/",
        "/api/twitch/eventsub",
        "/public/",
    )
    return not any(path == p or path.startswith(p) for p in exempt_prefixes)


def _path_exempt_from_maintenance(path: str) -> bool:
    path = str(path or "")
    exempt_prefixes = (
        "/login",
        "/logout",
        "/wartung",
        "/maintenance",
        "/static/",
        "/healthz",
        "/metrics",
        "/app-icon",
        "/manifest",
        "/sw.",
        "/favicon",
        "/agb",
        "/datenschutz",
        "/tts/",
        "/alerts/",
        "/api/twitch/eventsub",
    )
    return any(path == p or path.startswith(p) for p in exempt_prefixes)


def _maintenance_response(custom_message: str | None = None):
    msg = custom_message or gettext(
        "StreamVault is temporarily unavailable due to maintenance. "
        "Please be patient — the site will be back soon."
    )
    if _request_wants_json_response():
        return jsonify(
            {
                "ok": False,
                "error": "maintenance",
                "message": msg,
            }
        ), 503
    return render_template_string(MAINTENANCE_TEMPLATE, maintenance_message=msg), 503


def _request_wants_json_response() -> bool:
    if request.path.startswith("/api/"):
        return True
    requested = request.headers.get("X-Requested-With") or ""
    if requested.lower() == "xmlhttprequest":
        return True
    if request.is_json:
        return True
    accept = request.headers.get("Accept") or ""
    return "application/json" in accept.lower() and "text/html" not in accept.lower()


def _terms_required_response():
    from core import tube_terms

    if _request_wants_json_response():
        return jsonify(
            {
                "ok": False,
                "error": "terms_acceptance_required",
                "terms_version": tube_terms.current_version(),
                "message": gettext(
                    "Terms version outdated or not accepted. "
                    "Acceptance required at /agb."
                ),
            }
        ), 400
    nxt = request.full_path or "/agb"
    return redirect(url_for("agb_view", next=nxt))


@app.route("/agb", methods=["GET"])
def agb_view():
    from core import tube_terms

    ctx = tube_terms.terms_web_context(site_url=PUBLIC_BASE or "")
    require_accept = False
    accepted_version = ""
    if session.get("logged_in"):
        row = database.get_user(current_username())
        accepted_version = (row or {}).get("terms_version") or ""
        require_accept = current_user_needs_terms_acceptance()
    nxt = request.args.get("next") or ""
    if nxt and not is_safe_next(nxt):
        nxt = ""
    return render_template_string(
        AGB_TEMPLATE,
        require_accept=require_accept,
        accepted_version=accepted_version,
        error=request.args.get("error"),
        ok=request.args.get("ok"),
        next_url=nxt,
        **ctx,
    )


@app.route("/datenschutz", methods=["GET"])
def datenschutz_view():
    from core import tube_terms

    ctx = tube_terms.privacy_web_context(site_url=PUBLIC_BASE or "")
    return render_template_string(DATENSCHUTZ_TEMPLATE, **ctx)


def _accept_terms_from_request():
    from core import tube_terms

    if (session.get("user_role") or "") == "admin":
        return redirect(url_for("index"))
    accepted = str(
        request.form.get("terms_accepted") or request.form.get("rules_accepted") or ""
    ).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    version = str(
        request.form.get("terms_version") or request.form.get("rules_version") or ""
    ).strip()
    if not accepted:
        if _request_wants_json_response():
            return jsonify({"ok": False, "error": "terms_not_accepted"}), 400
        return redirect(url_for("agb_view", error=gettext("Consent required.")))
    if version != tube_terms.current_version():
        if _request_wants_json_response():
            return jsonify(
                {
                    "ok": False,
                    "error": "terms_version_mismatch",
                    "terms_version": tube_terms.current_version(),
                }
            ), 400
        return redirect(
            url_for(
                "agb_view",
                error=gettext("Terms version outdated — reload the page and accept again."),
            )
        )
    name = current_username()
    row = database.get_user(name)
    if not row:
        abort(400)
    tube_terms.record_terms_acceptance(
        name,
        version=version,
        source="web",
        discord_id=row.get("discord_id") or "",
    )
    if _request_wants_json_response():
        return jsonify(
            {
                "ok": True,
                "terms_version": version,
                "username": name,
            }
        )
    target = request.args.get("next") or request.form.get("next")
    if target and is_safe_next(target):
        return redirect(target)
    return redirect(url_for("index"))


@app.route("/agb/accept", methods=["POST"])
@login_required
def agb_accept():
    return _accept_terms_from_request()


@app.route("/regeln", methods=["GET"])
def regeln_view():
    nxt = request.args.get("next") or ""
    if session.get("logged_in"):
        if nxt and is_safe_next(nxt):
            return redirect(url_for("agb_view", next=nxt) + "#hausordnung")
        return redirect(url_for("agb_view") + "#hausordnung")
    if nxt and is_safe_next(nxt):
        return redirect(url_for("login", next=nxt))
    return redirect(url_for("login", next=url_for("agb_view")))


@app.route("/regeln/accept", methods=["POST"])
@login_required
def regeln_accept():
    return _accept_terms_from_request()


def login_redirect_target():
    """Safe post-login redirect target from ?next= or the index."""
    target = request.args.get("next")
    if target and is_safe_next(target):
        return target
    return url_for("index")


def login_page_redirect():
    """Redirect back to the login form, preserving a safe ?next= if present."""
    target = request.args.get("next")
    if target and is_safe_next(target):
        return redirect(url_for("login", next=target))
    return redirect(url_for("login"))


@app.before_request
def csrf_protect():
    # Mail web routes removed — always 404 regardless of legacy enable flags.
    if request.path == "/admin/mail" or request.path.startswith("/admin/mail/"):
        abort(404)
    if request.path == "/admin/stalk" or request.path.startswith("/admin/stalk/"):
        abort(404)
    # Keep the login cookie alive while the account still exists and is active.
    if session.get("logged_in"):
        ctx = _get_current_user_context()
        if ctx is None or not ctx.get("active", True):
            session.clear()
            if request.endpoint not in ("login", "static", "logout"):
                if request.is_json or request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "Account deactivated or session invalid"}), 401
                return redirect(url_for("login", next=request.path if request.method == "GET" else None))

        now = time.time()
        login_time = session.get("login_time")
        last_active = session.get("last_active", login_time)
        timed_out = False
        if SESSION_MAX_AGE_SEC > 0:
            if login_time is not None and (now - float(login_time)) > SESSION_MAX_AGE_SEC:
                timed_out = True
            elif last_active is not None and (now - float(last_active)) > SESSION_MAX_AGE_SEC:
                timed_out = True
        if timed_out:
            session.clear()
            if request.endpoint not in ("login", "static", "logout"):
                if request.is_json or request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "Session expired"}), 401
                return redirect(url_for("login", next=request.path if request.method == "GET" else None))

        session.permanent = True
        session["username"] = ctx["username"]
        session["user_role"] = ctx["role"]
        session["last_active"] = now
        session["group_id"] = ctx["group_id"]
        session.pop("user_permissions", None)
        try:
            user_traffic.maybe_record_page(
                ctx["username"],
                request.path,
                request.method,
                wants_json=_request_wants_json_response(),
            )
        except Exception:
            pass

        # Soft-gate: current AGB + Hausordnung must be accepted (admins exempt).
        if _path_requires_rules_acceptance(request.path) and current_user_needs_terms_acceptance():
            if request.endpoint not in (
                "agb_view",
                "agb_accept",
                "datenschutz_view",
                "regeln_view",
                "regeln_accept",
                "logout",
                "static",
                "login",
                "maintenance",
            ):
                return _terms_required_response()
    # WIP / maintenance gate: everyone except admins and whitelisted bypass users.
    if (
        maintenance_mode_enabled("global")
        and not current_user_bypasses_maintenance()
        and not _path_exempt_from_maintenance(request.path)
    ):
        return _maintenance_response()

    # Granular Mobile maintenance gate
    if (
        maintenance_mode_enabled("mobile")
        and is_mobile()
        and not current_user_bypasses_maintenance()
        and not _path_exempt_from_maintenance(request.path)
    ):
        return _maintenance_response(gettext("The mobile version of StreamVault is currently under maintenance. Please use the desktop version."))
    # Token-auth TTS / alert overlay APIs (OBS browser source) — not session CSRF.
    # Twitch EventSub webhooks authenticate via HMAC signature, not CSRF.
    if (
        request.path.startswith("/tts/")
        or request.path.startswith("/alerts/")
        or request.path == "/api/twitch/eventsub"
    ):
        return
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # SECURITY: CSRF protection on ALL state-mutating endpoints including login
        token = session.get("_csrf_token")
        provided = request.form.get("csrf_token")
        if not provided:
            provided = request.headers.get("X-CSRFToken") or request.headers.get("X-CSRF-Token")
        if not provided and request.is_json:
            body = request.get_json(silent=True) or {}
            if isinstance(body, dict):
                provided = body.get("csrf_token")
        if (
            not token
            or not provided
            or not secrets.compare_digest(str(token), str(provided))
        ):
            # Successful login rotates the session; a duplicate form POST can then
            # arrive with the old CSRF token. Keep the user in the app instead of
            # showing a bare Forbidden page.
            if request.endpoint == "login":
                if session.get("logged_in"):
                    logging.info(
                        "Ignoring stale login CSRF after authenticated session from %s",
                        get_client_ip(),
                    )
                    return redirect(login_redirect_target())
                logging.warning(f"Login CSRF attempt from {get_client_ip()}")
                return login_page_redirect()
            logging.warning(f"CSRF violation attempt from {get_client_ip()} on {request.endpoint}")
            abort(403)

def generate_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(16)
    return session["_csrf_token"]

app.jinja_env.globals["csrf_token"] = generate_csrf_token


@app.context_processor
def inject_theme_context():
    from core import tube_auth

    theme = current_theme_key()
    role = current_user_role()
    mobile_flag = is_mobile()
    rail_streamers = []
    if session.get("logged_in") and not mobile_flag:
        try:
            rail_streamers = list(visible_streamers() or [])
            rail_streamers.sort(
                key=lambda s: (
                    not bool(s.get("twitch_live") or s.get("online")),
                    str(s.get("display_name") or s.get("name") or "").lower(),
                )
            )
        except Exception:
            rail_streamers = []

    return {
        "theme_options": THEME_OPTIONS,
        "current_theme": theme,
        "theme_class": f"theme-{theme}",
        "ASSET_VERSION": ASSET_VERSION,
        "UI_FONTS_HREF": UI_FONTS_HREF,
        "stalk_enabled": False,
        "clip_settings": clip_engine.public_settings(),
        "downloads_enabled": downloads_enabled(),
        "public_shares_enabled": public_shares_enabled(),
        "can_manage_users": current_user_can_manage_users(),
        "user_permissions": session.get("user_permissions") or (database.get_user_permissions(session.get("username")) if session.get("username") else {}),
        "can_access_vods": tube_auth.role_can_access_vods(role),
        "is_live_only": tube_auth.is_live_only(role),
        "role_label": tube_auth.role_label,
        "is_mobile": mobile_flag,
        "mobile_live_enabled": mobile_live_enabled(),
        "streamers": rail_streamers,
    }


@app.route("/wartung")
@app.route("/maintenance")
def maintenance():
    """Public WIP / maintenance landing; admins are sent back into the app."""
    if maintenance_mode_enabled() and current_user_is_admin():
        return redirect(url_for("index"))
    if not maintenance_mode_enabled():
        if session.get("logged_in"):
            return redirect(url_for("index"))
        return redirect(url_for("login"))
    return render_template_string(MAINTENANCE_TEMPLATE), 503


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")  # SECURITY: Rate limit login attempts (10 per minute)
def login():
    error = None
    ip = get_client_ip()
    if is_banned(ip):
        return render_template_string(
            LOGIN_TEMPLATE,
            error=gettext("Your IP has been temporarily blocked. Please try again later."),
        )

    # Already authenticated (e.g. duplicate submit after session rotation).
    if session.get("logged_in"):
        if maintenance_mode_enabled() and not current_user_is_admin():
            return redirect(url_for("maintenance"))
        return redirect(login_redirect_target())

    if request.method == "POST":
        # SECURITY: Validate CSRF token on login (defense in depth; before_request also checks)
        csrf_token = session.get("_csrf_token")
        if not csrf_token or not secrets.compare_digest(
            str(csrf_token), str(request.form.get("csrf_token") or "")
        ):
            logging.warning(f"Login CSRF attempt from {ip}")
            return login_page_redirect()

        username = request.form.get("username")
        password = request.form.get("password")

        if not check_rate_limit(ip):
            logging.warning(f"LOGIN RATELIMIT EXCEEDED: {ip}")
            ban_ip(ip)  # temporary lockout via LOGIN_BAN_TTL_SEC
            log_login_attempt(ip, username, False)
            error = gettext("Too many attempts. IP temporarily blocked. Please try again later.")
            return render_template_string(LOGIN_TEMPLATE, error=error)

        role = authenticate_user(username, password)
        if role:
            # Clear pre-login session data before setting authenticated state.
            session.clear()

            # Persistent login cookie: survives browser restart; server-side
            # validity follows the account, not a short clock timeout.
            now = time.time()
            session.permanent = True
            session["logged_in"] = True
            session.permanent = True
            session["logged_in"] = True
            session["user_role"] = role
            session["username"] = username
            session["login_time"] = now
            session["last_active"] = now
            user_row = database.get_user(username)
            session["group_id"] = user_row.get("group_id") if user_row else None
            session["user_permissions"] = database.get_user_permissions(username)
            # Fresh CSRF for post-login forms; old login-form token is intentionally invalid.
            session["_csrf_token"] = secrets.token_hex(16)

            clear_login_attempts(ip)
            log_login_attempt(ip, username, True)
            try:
                user_traffic.record(username, "login")
            except Exception:
                pass
            logging.info(f"Successful login: {username} from {ip}")

            if role != "admin" and maintenance_mode_enabled():
                return redirect(url_for("maintenance"))
            if current_user_needs_terms_acceptance():
                nxt = request.args.get("next") or request.form.get("next") or ""
                if nxt and is_safe_next(nxt):
                    return redirect(url_for("agb_view", next=nxt))
                return redirect(url_for("agb_view"))
            return redirect(login_redirect_target())

        logging.warning(f"LOGIN FAILED: {ip} (user: {username})")
        add_login_attempt(ip)
        log_login_attempt(ip, username, False)
        error = gettext("Login failed.")
    return render_template_string(LOGIN_TEMPLATE, error=error)


@app.route("/logout", methods=["POST"])
@login_required  # Ensure user is logged in before logout
def logout():
    """Secure logout with CSRF protection (POST only)."""
    username = session.get("username", "unknown")
    ip = get_client_ip()
    logging.info(f"Logout: {username} from {ip}")
    session.clear()
    return redirect(url_for("login"))


@app.route("/language", methods=["POST"])
@limiter.limit("60 per hour")
def language_update():
    """Remember the chosen UI language; also reachable from the login page."""
    nxt = request.form.get("next") or ""
    target = nxt if is_safe_next(nxt) else url_for("index")
    resp = redirect(target)
    locale = ui_i18n.normalize_locale(request.form.get("lang"))
    if locale:
        resp.set_cookie(
            ui_i18n.LANG_COOKIE, locale, max_age=365 * 86400, samesite="Lax",
            secure=app.config["SESSION_COOKIE_SECURE"], httponly=True,
        )
    else:
        resp.delete_cookie(ui_i18n.LANG_COOKIE)  # "automatic": follow the browser again
    return resp


@app.route("/settings/theme", methods=["POST"])
@login_required
@limiter.limit("120 per hour")
def theme_update():
    theme = str(request.form.get("theme") or "").strip().lower()
    if not set_user_theme(current_username(), theme):
        abort(400)
    target = request.form.get("next") or url_for("index")
    if not is_safe_next(target):
        target = url_for("index")
    return redirect(target)


@app.route("/admin", methods=["GET", "POST"])
@admin_required
@limiter.limit("100 per hour")  # SECURITY: Rate limit admin panel
def admin_view():
    try:
        from core import tube_terms, tube_auth
        error = request.args.get("error")
        msg = request.args.get("msg")
        
        logs = get_login_logs()
        for log in logs:
            log["date"] = time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(log["ts"]))

        terms_acceptances = database.list_terms_acceptances(limit=80)
        for row in terms_acceptances:
            row["date"] = time.strftime("%d.%m.%Y %H:%M:%S", time.gmtime(row["accepted_at"])) + " UTC"
        terms_info = {
            "version": tube_terms.current_version(),
            "effective_date": tube_terms.effective_date(),
            "title": tube_terms.terms_title(),
        }
        
        users = load_users()
        # FIX: VOD_USER -> config.VOD_USER
        env_user_in_db = any(u.lower() == config.VOD_USER.lower() for u in users)
        pending_users = {
            name: data for name, data in users.items() if not data.get("active")
        }
        vod_access_defaults = tube_auth.list_default_vod_access_slugs()
        vod_access_candidates = _vod_access_candidate_slugs()

        # System Stats
        try:
            sys_load = os.getloadavg()
        except Exception:
            sys_load = (0,0,0)
        
        return render_template_string(
            ADMIN_TEMPLATE, 
            logs=logs, 
            bans=load_bans(), 
            users=users,
            pending_users=pending_users,
            env_user=config.VOD_USER,
            env_user_in_db=env_user_in_db,
            error=error,
            msg=msg,
            sys_load=sys_load,
            storage=storage_info(),
            storage_alert=load_storage_alert_status(),
            remux_status=load_remux_status(),
            task_queue=task_queue_summary(),
            recording_health=recording_health_rows(),
            recent_task_errors=recent_task_error_rows(),
            share_rows=share_admin_rows(),
            role_label=tube_auth.role_label,
            vod_access_defaults=vod_access_defaults,
            vod_access_candidates=vod_access_candidates,
            terms_info=terms_info,
            terms_acceptances=terms_acceptances,
            traffic_when=user_traffic.format_when,
            maintenance=site_maintenance_admin_context(),
            backups=list_system_backups(),
            banner_folders=list_all_streamer_folders(),
        )
    except Exception as e:
        logging.error(f"Admin panel error: {e!s}", exc_info=True)
        return gettext("Error loading the admin panel. Please try again later."), 500


@app.route("/admin/traffic")
@admin_required
@limiter.limit("60 per hour")
def admin_traffic_view():
    try:
        days_raw = (request.args.get("days") or "30").strip()
        days = 90 if days_raw == "90" else 30
        show = (request.args.get("show") or "all").strip().lower()
        if show not in {"all", "inactive"}:
            show = "all"
        from core import tube_auth

        users = load_users()
        overview = user_traffic.build_overview(users, days=days)
        cards = overview["cards"]
        if show == "inactive":
            cards = [card for card in cards if card["inactive"]]
        return render_template_string(
            ADMIN_TRAFFIC_TEMPLATE,
            overview=overview,
            cards=cards,
            days=days,
            show=show,
            role_label=tube_auth.role_label,
        )
    except Exception as exc:
        logging.error("Admin traffic error: %s", exc, exc_info=True)
        return gettext("Error loading the traffic overview."), 500


# ============ CLOUD STORAGE (Google Drive, S3, Dropbox, OneDrive, WebDAV) ============

def _storage_service():
    from core.storage import service as storage_service

    return storage_service


def _storage_error(exc):
    from core.storage.base import mask_error

    return mask_error(exc)


def _storage_redirect(msg=None, error=None):
    from core.storage.base import mask_text

    params = {}
    if msg:
        params["msg"] = mask_text(msg)
    if error:
        params["error"] = mask_text(error)
    return redirect(url_for("admin_storage_view", **params))


def _storage_redirect_uri():
    return absolute_public_url(url_for("storage_oauth_callback"))


@app.route("/admin/storage")
@admin_required
@limiter.limit("120 per hour")
def admin_storage_view():
    try:
        svc = _storage_service()
        context = svc.admin_context()
        provider_names = {row["id"]: row.get("name") or "" for row in context["providers"]}
        return render_template_string(
            STORAGE_TEMPLATE,
            msg=request.args.get("msg"),
            error=request.args.get("error"),
            provider_names=provider_names,
            format_bytes=svc.format_bytes,
            format_ts=svc.format_timestamp,
            oauth_redirect_uri=_storage_redirect_uri(),
            **context,
        )
    except Exception as exc:
        logging.error("Admin storage error: %s", _storage_error(exc))
        return gettext("Error loading cloud storage management."), 500


@app.route("/admin/storage/settings", methods=["POST"])
@admin_required
@limiter.limit("60 per hour")
def admin_storage_settings():
    svc = _storage_service()
    values = {
        "auto_upload": request.form.get("auto_upload") == "1",
        "delete_local_after_upload": request.form.get("delete_local_after_upload") == "1",
        "verify_before_local_delete": request.form.get("verify_before_local_delete") == "1",
        "include_chat_logs": request.form.get("include_chat_logs") == "1",
        "include_thumbnails": request.form.get("include_thumbnails") == "1",
        "local_retention_days": request.form.get("local_retention_days", "14"),
        "auto_sync_interval_minutes": request.form.get("auto_sync_interval_minutes", "10"),
        "cleanup_interval_minutes": request.form.get("cleanup_interval_minutes", "360"),
        "max_sync_files": request.form.get("max_sync_files", "500"),
        "default_provider_id": request.form.get("default_provider_id") or None,
    }
    try:
        svc.save_settings(values)
        return _storage_redirect(msg=gettext("Settings saved."))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/providers/add", methods=["POST"])
@admin_required
@limiter.limit("30 per hour")
def admin_storage_provider_add():
    svc = _storage_service()
    from core.storage.registry import get_provider_class

    try:
        type_id = str(request.form.get("provider_type") or "").strip()
        cls = get_provider_class(type_id)
        values = {}
        for field in cls.config_fields:
            raw = request.form.get(f"cfg_{field.key}", "")
            if field.secret and not str(raw or "").strip():
                continue
            values[field.key] = raw
        provider_id = svc.create_provider(
            request.form.get("name", ""),
            type_id,
            values,
            enabled=request.form.get("enabled") == "1",
        )
        return _storage_redirect(
            msg=gettext("Provider #%(id)s created. Please test the connection.", id=provider_id)
        )
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/providers/<int:provider_id>/update", methods=["POST"])
@admin_required
@limiter.limit("60 per hour")
def admin_storage_provider_update(provider_id):
    svc = _storage_service()
    from core.storage.registry import get_provider_class

    try:
        row = svc.get_provider_record(provider_id)
        if not row:
            raise ValueError(gettext("Provider not found."))
        cls = get_provider_class(row["provider_type"])
        values = {}
        for field in cls.config_fields:
            values[field.key] = request.form.get(f"cfg_{field.key}", "")
        svc.update_provider(
            provider_id,
            name=request.form.get("name", row["name"]),
            values=values,
            enabled=request.form.get("enabled") == "1",
        )
        return _storage_redirect(msg=gettext("Provider #%(id)s saved.", id=provider_id))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/providers/<int:provider_id>/delete", methods=["POST"])
@admin_required
@limiter.limit("20 per hour")
def admin_storage_provider_delete(provider_id):
    svc = _storage_service()
    try:
        svc.delete_provider(provider_id)
        return _storage_redirect(
            msg=gettext("Provider #%(id)s removed. Cloud files are kept.", id=provider_id)
        )
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/providers/<int:provider_id>/test", methods=["POST"])
@admin_required
@limiter.limit("20 per hour")
def admin_storage_provider_test(provider_id):
    svc = _storage_service()
    try:
        result = svc.test_provider(provider_id)
        if result.get("ok"):
            return _storage_redirect(msg=result.get("message"))
        return _storage_redirect(error=result.get("message"))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/providers/<int:provider_id>/check", methods=["POST"])
@admin_required
@limiter.limit("20 per hour")
def admin_storage_provider_check(provider_id):
    svc = _storage_service()
    try:
        task_id = svc.enqueue_check(provider_id)
        return _storage_redirect(msg=gettext("Connection test queued (task #%(task)s).", task=task_id))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/providers/<int:provider_id>/oauth/start")
@admin_required
@limiter.limit("30 per hour")
def admin_storage_oauth_start(provider_id):
    svc = _storage_service()
    try:
        return redirect(svc.oauth_authorize_url(provider_id, _storage_redirect_uri()))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/oauth/callback")
@admin_required
@limiter.limit("30 per hour")
def storage_oauth_callback():
    svc = _storage_service()
    if request.args.get("error"):
        return _storage_redirect(error=gettext("Provider reported: %(error)s", error=request.args.get("error")))
    try:
        provider_id = svc.verify_oauth_state(request.args.get("state", ""))
        code = str(request.args.get("code") or "").strip()
        if not code:
            raise ValueError(gettext("OAuth code is missing."))
        svc.oauth_complete(provider_id, code, _storage_redirect_uri())
        result = svc.test_provider(provider_id)
        if result.get("ok"):
            return _storage_redirect(msg=result.get("message"))
        return _storage_redirect(error=result.get("message"))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/sync", methods=["POST"])
@admin_required
@limiter.limit("20 per hour")
def admin_storage_sync():
    svc = _storage_service()
    try:
        provider_id = request.form.get("provider_id") or None
        streamer = str(request.form.get("streamer") or "").strip() or None
        task_id = svc.enqueue_sync(provider_id, streamer)
        if task_id:
            return _storage_redirect(msg=gettext("Cloud sync queued (task #%(task)s).", task=task_id))
        return _storage_redirect(msg=gettext("A cloud sync is already running."))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/cleanup", methods=["POST"])
@admin_required
@limiter.limit("20 per hour")
def admin_storage_cleanup():
    svc = _storage_service()
    try:
        provider_id = request.form.get("provider_id") or None
        task_id = svc.enqueue_cleanup(provider_id)
        if task_id:
            return _storage_redirect(msg=gettext("Cleanup queued (task #%(task)s).", task=task_id))
        return _storage_redirect(msg=gettext("A cleanup is already running."))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/objects/<int:object_id>/upload", methods=["POST"])
@admin_required
@limiter.limit("60 per hour")
def admin_storage_object_upload(object_id):
    svc = _storage_service()
    try:
        obj = database.get_storage_object(object_id)
        if not obj:
            raise ValueError(gettext("Archive object not found."))
        if obj.get("local_deleted_at"):
            raise ValueError(gettext("The local file has already been removed. Please restore it first."))
        result = svc.enqueue_upload(
            obj["local_path"], obj["provider_id"], force=True
        )
        if result.get("task_id"):
            return _storage_redirect(msg=gettext("Upload queued (task #%(task)s).", task=result["task_id"]))
        return _storage_redirect(msg=gettext("An upload for this file is already running."))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/objects/<int:object_id>/restore", methods=["POST"])
@admin_required
@limiter.limit("30 per hour")
def admin_storage_object_restore(object_id):
    svc = _storage_service()
    try:
        task_id = svc.enqueue_restore(object_id)
        if task_id:
            return _storage_redirect(msg=gettext("Restore queued (task #%(task)s).", task=task_id))
        return _storage_redirect(msg=gettext("A restore is already running."))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/objects/<int:object_id>/delete-remote", methods=["POST"])
@admin_required
@limiter.limit("30 per hour")
def admin_storage_object_delete_remote(object_id):
    svc = _storage_service()
    try:
        task_id = svc.enqueue_delete(object_id)
        if task_id:
            return _storage_redirect(msg=gettext("Cloud deletion queued (task #%(task)s).", task=task_id))
        return _storage_redirect(msg=gettext("A cloud deletion is already running."))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/objects/<int:object_id>/delete-local", methods=["POST"])
@admin_required
@limiter.limit("30 per hour")
def admin_storage_object_delete_local(object_id):
    svc = _storage_service()
    try:
        ok, message = svc.delete_local_copy(object_id)
        if ok:
            return _storage_redirect(msg=message)
        return _storage_redirect(error=message)
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/storage/objects/<int:object_id>/forget", methods=["POST"])
@admin_required
@limiter.limit("30 per hour")
def admin_storage_object_forget(object_id):
    try:
        if database.delete_storage_object(object_id):
            return _storage_redirect(
                msg=gettext("Object #%(id)s removed from the database. Files are kept.", id=object_id)
            )
        return _storage_redirect(error=gettext("Archive object not found."))
    except Exception as exc:
        return _storage_redirect(error=_storage_error(exc))


@app.route("/admin/unban/<ip>", methods=["POST"])
@admin_required
def admin_unban(ip):
    unban_ip(ip)
    return redirect(url_for("admin_view"))

@app.route("/admin/ban/<ip>", methods=["POST"])
@admin_required
def admin_ban_ip(ip):
    ban_ip(ip, ttl_sec=0)  # permanent admin ban
    return redirect(url_for("admin_view"))

@app.route("/admin/remux_all", methods=["POST"])
@admin_required
def admin_remux_all():
    try:
        database.add_task("remux_all", {"reencode_audio": True})
        return redirect(url_for("admin_view", msg=gettext("Remux job started. Progress is shown in the admin panel.")))
    except Exception as e:
        return redirect(url_for("admin_view", error=gettext("Remux job could not be started: %(error)s", error=e)))


@app.route("/streamer/<streamer>/visibility", methods=["POST"])
@admin_required
def streamer_visibility_update(streamer):
    abort_hidden_streamer(streamer)
    archive = archive_name_for(streamer) or normalize_channel_name(streamer) or str(streamer or "").strip()
    raw_vis = str(request.form.get("visibility") or "").strip().lower()
    vis = "admin" if raw_vis in ("admin", "admins", "private", "hidden", "1") else "members"
    try:
        profiles = load_profiles()
        entry = dict(profiles.get(archive) or {})
        entry["visibility"] = vis
        profiles[archive] = entry
        save_profiles(profiles)
        bust_media_cache()
        STREAMER_CACHE["ts"] = 0
        visibility_msg = gettext("Visibility: admins only.") if vis == "admin" else gettext("Visibility: all users.")
        return redirect(url_for("streamer_view", streamer=archive, msg=visibility_msg))
    except Exception as exc:
        logging.error("Sichtbarkeit fehlgeschlagen: %s", exc)
        return redirect(url_for("streamer_view", streamer=archive, msg=gettext("Visibility could not be saved.")))


@app.route("/admin/share/revoke/<token>", methods=["POST"])
@admin_required
def admin_share_revoke(token):
    if revoke_share(token):
        return redirect(url_for("admin_view", msg=gettext("Share revoked.")))
    return redirect(url_for("admin_view", error=gettext("Share not found.")))


@app.route("/admin/chat", methods=["GET", "POST"])
@admin_required
def chat_tracker():
    return redirect(url_for("index"))
    results = []
    # Support both GET (search) and POST (export)
    query_user = request.args.get("user", "").strip() or request.form.get("user", "").strip()
    global_query = request.args.get("global", "").strip()
    global_results = global_search(global_query) if global_query else {"streamers": [], "videos": [], "chat": []}
    
    # Check if export requested
    do_export_html = request.form.get("export") == "html"
    selected_ids = request.form.getlist("selected_items") if do_export_html else None
    
    # --- Log Export (Full Log) ---
    if request.method == "POST" and request.form.get("export_log") == "1":
        s_streamer = request.form.get("streamer")
        s_date = request.form.get("date")
        s_word = request.form.get("word", "").strip()
        if s_streamer and s_date:
            safe_dir = streamer_dir(s_streamer)
            log_basename = os.path.basename(str(s_date))
            full_path = None
            if safe_dir and log_basename and log_basename == str(s_date):
                candidate = os.path.join(safe_dir, CHAT_LOG_SUBDIR, log_basename)
                try:
                    real = os.path.realpath(candidate)
                    base_real = os.path.realpath(BASE_DIR)
                    if real.startswith(base_real + os.sep) and os.path.isfile(real):
                        full_path = real
                except OSError:
                    full_path = None
            if full_path:
                msgs = []
                try:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        for line in f:
                            try:
                                d = json.loads(line)
                                if s_word and not chat_message_matches_word(d.get("msg", ""), s_word):
                                    continue
                                d['color'] = get_user_color(d.get('user',''))
                                d['timestamp'] = time.strftime("%H:%M:%S", time.localtime(d.get('t',0)))
                                msgs.append(d)
                            except Exception:
                                pass
                except Exception:
                    pass
                
                grouped_list = [{
                    "streamer": s_streamer,
                    "date": s_date,
                    "messages": msgs
                }]
                html_content = render_template_string(CHAT_EXPORT_TEMPLATE, user=f"Log {s_date}", grouped_results=grouped_list)
                response = make_response(html_content)
                response.headers["Content-Disposition"] = f"attachment; filename=chat_log_{s_streamer}_{s_date}.html"
                return response

    # --- Log Browser & Analysis Shared Logic ---
    # Only active recording archives — no orphan/chat-only streamer noise.
    active_archives = active_recording_archives()
    streamers_with_logs = []
    if os.path.isdir(BASE_DIR):
        for d in os.listdir(BASE_DIR):
            if active_archives and d not in active_archives:
                continue
            path = os.path.join(BASE_DIR, d, CHAT_LOG_SUBDIR)
            if os.path.isdir(path) and any(f.endswith('.jsonl') for f in os.listdir(path)):
                streamers_with_logs.append(d)
    streamers_with_logs.sort()
    
    # --- Log Browser Logic ---
    selected_streamer = request.args.get("streamer")
    selected_date = request.args.get("date")
    log_word_query = request.args.get("word", "").strip()
    
    available_logs = []
    log_content = []
    selected_log_display = ""
    log_browser_active = False
    selected_files_by_streamer = {}
    
    if selected_streamer and selected_streamer in streamers_with_logs:
        log_dir = os.path.join(BASE_DIR, selected_streamer, CHAT_LOG_SUBDIR)
        if os.path.isdir(log_dir):
            files = [f for f in os.listdir(log_dir) if f.endswith('.jsonl')]
            files.sort(reverse=True)
            
            for f in files:
                base_name = os.path.splitext(f)[0]
                display_name = base_name
                match = re.search(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", base_name)
                if not match:
                    match = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", base_name)
                if match:
                    y, mo, d, h, mi, s = match.groups()
                    display_name = f"{d}.{mo}.{y} {h}:{mi}:{s}"
                available_logs.append({"filename": f, "display": display_name})
                if selected_date == f:
                    selected_log_display = display_name
            
            if selected_date and selected_date in files:
                selected_files_by_streamer[selected_streamer] = [(selected_date, selected_log_display or selected_date)]
            elif log_word_query:
                selected_files_by_streamer[selected_streamer] = [(item["filename"], item["display"]) for item in available_logs]
    elif log_word_query:
        for streamer_name in streamers_with_logs:
            log_dir = os.path.join(BASE_DIR, streamer_name, CHAT_LOG_SUBDIR)
            if not os.path.isdir(log_dir):
                continue
            files = [f for f in os.listdir(log_dir) if f.endswith('.jsonl')]
            files.sort(reverse=True)
            selected_files_by_streamer[streamer_name] = [(f, os.path.splitext(f)[0]) for f in files]

    if selected_files_by_streamer:
        log_browser_active = True
        hit_count = 0
        for streamer_name, selected_files in selected_files_by_streamer.items():
            log_dir = os.path.join(BASE_DIR, streamer_name, CHAT_LOG_SUBDIR)
            for log_filename, log_display in selected_files:
                full_path = os.path.join(log_dir, log_filename)
                try:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        for line in f:
                            try:
                                data = json.loads(line)
                                msg = str(data.get("msg", "") or "")
                                if log_word_query and not chat_message_matches_word(msg, log_word_query):
                                    continue
                                rel = data.get("rel", 0)
                                item = {
                                    "time_str": time.strftime("%H:%M:%S", time.localtime(data.get("t", 0))),
                                    "user": str(data.get("user", "") or ""),
                                    "msg": msg,
                                    "msg_html": highlight_chat_message(msg, log_word_query),
                                    "streamer": streamer_name,
                                    "log_display": log_display,
                                    "context_label": "",
                                    "link": chat_vod_url_for_log(streamer_name, log_filename, rel),
                                }
                                if log_word_query:
                                    item["context_label"] = f"{streamer_name} / {log_display}"
                                log_content.append(item)
                                hit_count += 1
                                if log_word_query and hit_count >= LOG_BROWSER_WORD_RESULT_LIMIT:
                                    break
                            except Exception:
                                pass
                except Exception:
                    pass
                if log_word_query and hit_count >= LOG_BROWSER_WORD_RESULT_LIMIT:
                    break
            if log_word_query and hit_count >= LOG_BROWSER_WORD_RESULT_LIMIT:
                break
    # -------------------------
    
    # --- Cross-Streamer Analysis ---
    analysis_results = []
    selected_analysis_streamers = request.args.getlist("analysis_streamers")
    
    if selected_analysis_streamers:
        user_map = {} # user -> set of streamers
        for s in selected_analysis_streamers:
            if active_archives and s not in active_archives:
                continue
            log_dir = os.path.join(BASE_DIR, s, CHAT_LOG_SUBDIR)
            if os.path.isdir(log_dir):
                for log_file in os.listdir(log_dir):
                    if not log_file.endswith(".jsonl"):
                        continue
                    full_path = os.path.join(log_dir, log_file)
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            for line in f:
                                try:
                                    data = json.loads(line)
                                    u = data.get("user")
                                    if u:
                                        if u not in user_map:
                                            user_map[u] = set()
                                        user_map[u].add(s)
                                except Exception:
                                    pass
                    except Exception:
                        pass
        
        for user, streamers in user_map.items():
            analysis_results.append({
                "user": user,
                "count": len(streamers),
                "streamers": sorted(list(streamers))
            })
        analysis_results.sort(key=lambda x: x['count'], reverse=True)
        analysis_results = analysis_results[:200]

    if query_user:
        q_lower = query_user.lower()
        found = 0
        if os.path.isdir(BASE_DIR):
            for streamer in os.listdir(BASE_DIR):
                if active_archives and streamer not in active_archives:
                    continue
                streamer_path = os.path.join(BASE_DIR, streamer)
                if not os.path.isdir(streamer_path):
                    continue

                chat_dir = os.path.join(streamer_path, CHAT_LOG_SUBDIR)
                if not os.path.isdir(chat_dir):
                    continue

                for log_file in os.listdir(chat_dir):
                    if not log_file.endswith(".jsonl"):
                        continue
                    
                    base_name = os.path.splitext(log_file)[0]
                    vod_filename = f"{base_name}.mp4"
                    
                    # Datum aus Dateinamen parsen
                    vod_date_display = base_name
                    match = re.search(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", base_name)
                    if not match:
                        match = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", base_name)
                    if match:
                        y, mo, d, h, mi, s = match.groups()
                        vod_date_display = f"{d}.{mo}.{y}"
                    
                    full_log_path = os.path.join(chat_dir, log_file)
                    
                    try:
                        with open(full_log_path, "r", encoding="utf-8") as f:
                            for line in f:
                                try:
                                    data = json.loads(line)
                                    if data.get("user", "").lower() == q_lower:
                                        rel = data.get("rel", 0)
                                        # ID for selection
                                        item_id = f"{streamer}|{base_name}|{rel}"
                                        results.append({
                                            "id": item_id,
                                            "streamer": streamer,
                                            "vod_name": vod_filename,
                                            "vod_base": base_name,
                                            "vod_date": vod_date_display,
                                            "timestamp": format_seconds(rel),
                                            "rel": rel,
                                            "msg": data.get("msg", ""),
                                            "user": data.get("user", ""),
                                            "link": chat_vod_url_for_log(streamer, log_file, rel)
                                        })
                                        found += 1
                                        if found >= MAX_CHAT_SEARCH_RESULTS:
                                            break
                                except Exception:
                                    pass
                            if found >= MAX_CHAT_SEARCH_RESULTS:
                                break
                    except Exception:
                        pass
                if found >= MAX_CHAT_SEARCH_RESULTS:
                    break
        results.sort(key=lambda x: x['vod_name'], reverse=True)

    if do_export_html and results:
        # Filter results
        if selected_ids:
            # Create a set for faster lookup
            sel_set = set(selected_ids)
            filtered = [r for r in results if r['id'] in sel_set]
        else:
            filtered = []
        
        if filtered:
            # Group by VOD
            grouped = {} # key: streamer|vod_base
            for r in filtered:
                k = f"{r['streamer']}|{r['vod_base']}"
                if k not in grouped:
                    grouped[k] = {
                        "streamer": r['streamer'],
                        "date": r['vod_date'],
                        "filename": r['vod_name'],
                        "messages": []
                    }
                r['color'] = get_user_color(r['user'])
                r['rel_str'] = r['timestamp']
                grouped[k]["messages"].append(r)
            
            grouped_list = list(grouped.values())
            html_content = render_template_string(CHAT_EXPORT_TEMPLATE, user=query_user, grouped_results=grouped_list)
            response = make_response(html_content)
            response.headers["Content-Disposition"] = f"attachment; filename=chat_export_{secure_filename(query_user)}.html"
            return response
    
    return render_template_string(
        load_html_template("search.html"), 
        results=results, 
        vod_results=(global_results.get("videos") if isinstance(global_results, dict) else []),
        query=query_user,
        global_query=global_query,
        global_results=global_results,
        streamers_list=streamers_with_logs,
        selected_streamer=selected_streamer,
        available_logs=available_logs,
        selected_date=selected_date,
        selected_log_display=selected_log_display,
        log_word_query=log_word_query,
        log_browser_active=log_browser_active,
        log_content=log_content,
        analysis_results=analysis_results,
        selected_analysis_streamers=selected_analysis_streamers
    )

@app.route("/admin/ban/manual", methods=["POST"])
@admin_required
def admin_ban_manual():
    ip = request.form.get("ban_ip", "").strip()
    if ip:
        ban_ip(ip, ttl_sec=0)  # permanent admin ban
    return redirect(url_for("admin_view"))

@app.route("/admin/users/add", methods=["POST"])
@admin_required
@limiter.limit("50 per hour")  # SECURITY: Rate limit user creation
def admin_user_add():
    """Create new user with strict input validation."""
    from core import tube_auth

    username = request.form.get("new_user", "").strip()
    password = request.form.get("new_pass", "").strip()
    role = request.form.get("role", "viewer")

    VALID_USERNAME = tube_auth.USERNAME_RE
    VALID_ROLES = set(tube_auth.VALID_ROLES) - {"user"}
    RESERVED_USERNAMES = tube_auth.RESERVED_USERNAMES
    MIN_PASSWORD_LENGTH = tube_auth.MIN_PASSWORD_LENGTH

    if not username:
        return redirect(url_for("admin_view", error=gettext("Username is required.")))

    if not VALID_USERNAME.match(username):
        return redirect(url_for("admin_view", error=gettext("Username must be 3-32 alphanumeric characters (hyphens and underscores allowed).")))

    if username.lower() in RESERVED_USERNAMES:
        return redirect(url_for("admin_view", error=gettext("Username '%(username)s' is reserved.", username=username)))

    if not password:
        return redirect(url_for("admin_view", error=gettext("Password is required.")))

    if len(password) < MIN_PASSWORD_LENGTH:
        return redirect(url_for("admin_view", error=gettext("Password must be at least %(min)s characters long.", min=MIN_PASSWORD_LENGTH)))

    role = tube_auth.normalize_role(role, default="viewer")
    if role not in VALID_ROLES:
        return redirect(url_for("admin_view", error=f"Invalid role: {role}"))

    users = load_users()
    if any(u.lower() == username.lower() for u in users):
        return redirect(url_for("admin_view", error=gettext("User '%(username)s' already exists.", username=username)))

    try:
        save_user(username, generate_password_hash(password), role=role, created_via="admin")
        logging.info(f"New user created: {username} (role: {role}) by {session.get('username')}")
        return redirect(url_for("admin_view", msg=gettext("User '%(username)s' created successfully.", username=username)))
    except Exception as e:
        logging.error(f"Failed to create user {username}: {e}")
        return redirect(url_for("admin_view", error=gettext("Error creating the user.")))

@app.route("/admin/users/update/<username>", methods=["POST"])
@admin_required
@limiter.limit("50 per hour")  # SECURITY: Rate limit user updates
def admin_user_update(username):
    """Update user with validation and protection for config admin."""
    new_name = request.form.get("new_name", "").strip()
    new_pass = request.form.get("new_pass", "").strip()
    role = request.form.get("role", "viewer")
    active = True if request.form.get("active") else False
    
    from core import tube_auth

    # SECURITY: Validate role
    role = tube_auth.normalize_role(role, default="viewer")
    VALID_ROLES = set(tube_auth.VALID_ROLES) - {"user"}
    if role not in VALID_ROLES:
        return redirect(url_for("admin_view", error=f"Invalid role: {role}"))
    
    # SECURITY: Validate password if provided
    if new_pass and len(new_pass) < tube_auth.MIN_PASSWORD_LENGTH:
        return redirect(url_for("admin_view", error=gettext("Password must be at least %(min)s characters long.", min=tube_auth.MIN_PASSWORD_LENGTH)))
    
    users = load_users()
    # Case-insensitive lookup for username key
    matched = next((u for u in users if u.lower() == username.lower()), None)
    if not matched:
        return redirect(url_for("admin_view", error=gettext("User '%(username)s' not found.", username=username)))
    username = matched
    
    # Self-lockout protection
    if username.lower() == (session.get("username") or "").lower() and not active:
        return redirect(url_for("admin_view", error=gettext("You cannot deactivate your own administrator account.")))

    # Protect config admin user
    if username.lower() == config.VOD_USER.lower():
        new_name = username
        role = "admin"
        active = True
    
    current_data = users[username]
    pw_hash = current_data["hash"]
    
    if new_pass:
        pw_hash = generate_password_hash(new_pass)
    
    if new_name and new_name != username:
        # Rename: Check if target exists
        if any(u.lower() == new_name.lower() for u in users if u != username):
            return redirect(url_for("admin_view", error=gettext("User '%(username)s' already exists.", username=new_name)))

        # Delete old, create new — preserve Discord binding
        delete_user(username)
        save_user(
            new_name,
            pw_hash,
            active=active,
            role=role,
            discord_id=current_data.get("discord_id") or None,
            discord_username=current_data.get("discord_username") or None,
            created_via=current_data.get("created_via") or None,
        )
        rename_user_settings(username, new_name)
        logging.info(f"User renamed: {username} -> {new_name} by {session.get('username')}")
    else:
        # Update existing
        save_user(username, pw_hash, active=active, role=role)
        logging.info(f"User updated: {username} by {session.get('username')}")
    
    return redirect(url_for("admin_view", msg=gettext("User updated.")))


@app.route("/admin/users/delete/<username>", methods=["POST"])
@admin_required
@limiter.limit("20 per hour")  # SECURITY: Rate limit user deletion (strict)
def admin_user_delete(username):
    if username.lower() == (session.get("username") or "").lower():
        return redirect(url_for("admin_view", error=gettext("You cannot delete your own account.")))
    if username.lower() == config.VOD_USER.lower():
        return redirect(url_for("admin_view", error=gettext("This admin user is protected and cannot be deleted.")))
    delete_user(username)
    delete_user_settings(username)
    return redirect(url_for("admin_view", msg=gettext("User deleted.")))


# ==============================================================================
# RBAC: USER & GROUP MANAGEMENT (UI & API)
# ==============================================================================

@app.route("/admin/users", methods=["GET"])
@manage_users_required
def admin_users_view():
    raw_users = database.list_users()
    raw_groups = database.list_user_groups()
    group_map = {g["id"]: g for g in raw_groups}

    group_counts = {}
    for u in raw_users:
        gid = u.get("group_id")
        if gid:
            group_counts[gid] = group_counts.get(gid, 0) + 1
    for grp in raw_groups:
        grp["member_count"] = group_counts.get(grp["id"], 0)

    users_list = []
    for u in raw_users:
        gid = u.get("group_id")
        grp = group_map.get(gid)
        users_list.append({
            "username": u["username"],
            "email": u.get("email") or "",
            "role": u.get("role") or "viewer",
            "active": bool(u.get("active")),
            "group_id": gid,
            "group_name": grp["name"] if grp else ("Administrator" if u.get("role") == "admin" else "Viewer"),
            "is_system_admin": (u["username"].lower() == config.VOD_USER.lower()),
            "discord_username": u.get("discord_username") or "",
            "created_at": u.get("created_at") or 0,
            "last_seen_at": u.get("last_seen_at") or 0,
            "last_login_at": u.get("last_login_at") or 0,
            "permissions": database.get_user_permissions(u["username"]),
        })

    candidate_slugs = set(_vod_access_candidate_slugs())
    streamer_objs = list_streamers() or []
    for s in streamer_objs:
        s_name = s.get("name") if isinstance(s, dict) else str(s)
        if s_name:
            norm = database._normalize_vod_access_slug(s_name) or s_name.rstrip("_").lower()
            candidate_slugs.add(norm)
    all_streamers = sorted([s for s in candidate_slugs if s and not is_system_backup_slug(s)], key=lambda x: x.lower())

    return render_template_string(
        load_html_template("admin_users.html"),
        users=users_list,
        groups=raw_groups,
        all_streamers=all_streamers,
        env_user=config.VOD_USER,
        current_username=session.get("username"),
    )


@app.route("/api/admin/users", methods=["GET"])
@manage_users_required
def api_admin_users_list():
    raw_users = database.list_users()
    raw_groups = database.list_user_groups()
    group_map = {g["id"]: g for g in raw_groups}

    users_out = []
    for u in raw_users:
        gid = u.get("group_id")
        grp = group_map.get(gid)
        users_out.append({
            "username": u["username"],
            "email": u.get("email") or "",
            "role": u.get("role") or "viewer",
            "active": bool(u.get("active")),
            "is_active": bool(u.get("active")),
            "group_id": gid,
            "group_name": grp["name"] if grp else ("Administrator" if u.get("role") == "admin" else "Viewer"),
            "is_system_admin": (u["username"].lower() == config.VOD_USER.lower()),
            "discord_username": u.get("discord_username") or "",
            "created_at": u.get("created_at") or 0,
            "last_seen_at": u.get("last_seen_at") or 0,
            "last_login_at": u.get("last_login_at") or 0,
            "permissions": database.get_user_permissions(u["username"]),
        })
    return jsonify({"ok": True, "users": users_out})


@app.route("/api/admin/users", methods=["POST"])
@manage_users_required
@limiter.limit("50 per hour")
def api_admin_user_create():
    from core import tube_auth

    data = request.get_json(silent=True) or request.form.to_dict()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "").strip()
    email = str(data.get("email") or "").strip()
    group_id = data.get("group_id")
    active = bool(data.get("active", True))

    if not username:
        return jsonify({"ok": False, "error": gettext("Username is required.")}), 400
    if not tube_auth.USERNAME_RE.match(username):
        return jsonify({"ok": False, "error": gettext("Invalid username (3-32 characters: letters, numbers, _ and -).")}), 400
    if username.lower() in tube_auth.RESERVED_USERNAMES:
        return jsonify({"ok": False, "error": gettext("Username '%(username)s' is reserved.", username=username)}), 400
    if not password or len(password) < tube_auth.MIN_PASSWORD_LENGTH:
        return jsonify({"ok": False, "error": gettext("Password must be at least %(min)s characters long.", min=tube_auth.MIN_PASSWORD_LENGTH)}), 400

    existing = database.get_user(username)
    if existing:
        return jsonify({"ok": False, "error": gettext("User '%(username)s' already exists.", username=username)}), 409

    if group_id is not None:
        try:
            group_id = int(group_id)
            group = database.get_user_group(group_id)
            if not group:
                return jsonify({"ok": False, "error": gettext("The selected group does not exist.")}), 400
            role = "admin" if group.get("permissions", {}).get("can_manage_users") else "viewer"
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": gettext("Invalid group ID.")}), 400
    else:
        group_id = 3
        role = "viewer" 

    pw_hash = generate_password_hash(password)
    save_user(username, pw_hash, active=active, role=role, group_id=group_id, email=email, created_via="admin")
    logging.info(f"API: New user created: {username} (role: {role}, group: {group_id}) by {session.get('username')}")

    return jsonify({
        "ok": True,
        "msg": gettext("User '%(username)s' created successfully.", username=username),
        "user": {
            "username": username,
            "email": email,
            "role": role,
            "group_id": group_id,
            "active": active,
        }
    }), 201


@app.route("/api/admin/users/<username>", methods=["PUT"])
@app.route("/api/admin/users/<username>/update", methods=["POST"])
@manage_users_required
@limiter.limit("50 per hour")
def api_admin_user_update(username):
    from core import tube_auth

    data = request.get_json(silent=True) or request.form.to_dict()
    user = database.get_user(username)
    if not user:
        return jsonify({"ok": False, "error": gettext("User '%(username)s' not found.", username=username)}), 404

    current_uname = (session.get("username") or "").lower()
    is_self = (username.lower() == current_uname)
    is_env_admin = (username.lower() == config.VOD_USER.lower())

    new_name = str(data.get("new_name") or username).strip()
    email = str(data.get("email") if "email" in data else (user.get("email") or "")).strip()
    active = bool(data.get("active", user.get("active", True)))
    new_pass = str(data.get("password") or "").strip()
    group_id = data.get("group_id")

    if is_self:
        if not active:
            return jsonify({"ok": False, "error": gettext("You cannot deactivate your own administrator account.")}), 400

    role = user.get("role") or "viewer"
    if group_id is not None:
        try:
            group_id = int(group_id)
            group = database.get_user_group(group_id)
            if not group:
                return jsonify({"ok": False, "error": gettext("The selected group does not exist.")}), 400
            if is_self and not bool(group.get("permissions", {}).get("can_manage_users")):
                return jsonify({"ok": False, "error": gettext("You cannot move yourself into a group without user management permissions.")}), 400
            database.set_user_group(username, group_id)
            role = "admin" if group.get("permissions", {}).get("can_manage_users") else "viewer"
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": gettext("Invalid group ID.")}), 400

    if is_env_admin:
        role = "admin"
        active = True
        new_name = username

    if new_pass:
        if len(new_pass) < tube_auth.MIN_PASSWORD_LENGTH:
            return jsonify({"ok": False, "error": gettext("Password must be at least %(min)s characters long.", min=tube_auth.MIN_PASSWORD_LENGTH)}), 400
        database.set_user_password(username, generate_password_hash(new_pass))

    database.set_user_active(username, active)
    database.set_user_role(username, role)
    database.set_user_email(username, email)

    if new_name and new_name.lower() != username.lower():
        if not tube_auth.USERNAME_RE.match(new_name):
            return jsonify({"ok": False, "error": gettext("Invalid new username.")}), 400
        if database.get_user(new_name):
            return jsonify({"ok": False, "error": gettext("User '%(username)s' already exists.", username=new_name)}), 409
        pw_hash = (database.get_user(username) or {}).get("password_hash")
        database.delete_user(username)
        save_user(
            new_name,
            pw_hash,
            active=active,
            role=role,
            group_id=group_id,
            email=email,
            discord_id=user.get("discord_id") or None,
            discord_username=user.get("discord_username") or None,
            created_via=user.get("created_via") or None,
        )
        rename_user_settings(username, new_name)
        username = new_name

    logging.info(f"API: User updated: {username} by {session.get('username')}")
    return jsonify({"ok": True, "msg": gettext("User '%(username)s' updated successfully.", username=username)})


@app.route("/api/admin/users/<username>", methods=["DELETE"])
@app.route("/api/admin/users/<username>/delete", methods=["POST"])
@manage_users_required
@limiter.limit("20 per hour")
def api_admin_user_delete(username):
    current_uname = (session.get("username") or "").lower()
    if username.lower() == current_uname:
        return jsonify({"ok": False, "error": gettext("You cannot delete your own account.")}), 400
    if username.lower() == config.VOD_USER.lower():
        return jsonify({"ok": False, "error": gettext("The default system admin cannot be deleted.")}), 400

    user = database.get_user(username)
    if not user:
        return jsonify({"ok": False, "error": gettext("User '%(username)s' not found.", username=username)}), 404

    database.delete_user(username)
    delete_user_settings(username)
    logging.info(f"API: User deleted: {username} by {session.get('username')}")
    return jsonify({"ok": True, "msg": gettext("User '%(username)s' has been deleted.", username=username)})


@app.route("/api/user/change-password", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def api_user_change_password():
    data = request.get_json(silent=True) or request.form.to_dict()
    current_pass = str(data.get("current_password") or "").strip()
    new_pass = str(data.get("new_password") or "").strip()

    if not current_pass:
        return jsonify({"ok": False, "error": gettext("Please enter your current password.")}), 400
    if not new_pass or len(new_pass) < 8:
        return jsonify({"ok": False, "error": gettext("The new password must be at least 8 characters long.")}), 400
    if current_pass == new_pass:
        return jsonify({"ok": False, "error": gettext("The new password must differ from the current password.")}), 400

    username = session.get("username")
    if not username:
        return jsonify({"ok": False, "error": gettext("Not authenticated.")}), 401

    if config.VOD_USER and username.lower() == str(config.VOD_USER).lower():
        if config.VOD_PASS and current_pass != config.VOD_PASS:
            return jsonify({"ok": False, "error": gettext("The current password is incorrect.")}), 400
        return jsonify({"ok": False, "error": gettext("The system administrator's password can only be changed via environment variables.")}), 403

    user_row = database.get_user(username)
    if not user_row:
        return jsonify({"ok": False, "error": gettext("User not found.")}), 404

    pw_hash = user_row.get("password_hash") or ""
    if not check_password_hash(pw_hash, current_pass):
        return jsonify({"ok": False, "error": gettext("The current password is incorrect.")}), 400

    new_hash = generate_password_hash(new_pass)
    database.set_user_password(username, new_hash)
    logging.info(f"API: User '{username}' successfully changed their password.")
    return jsonify({"ok": True, "msg": gettext("Password changed successfully.")})


@app.route("/api/admin/groups", methods=["GET"])
@manage_users_required
def api_admin_groups_list():
    raw_groups = database.list_user_groups()
    raw_users = database.list_users()
    counts = {}
    for u in raw_users:
        gid = u.get("group_id")
        if gid:
            counts[gid] = counts.get(gid, 0) + 1
    for grp in raw_groups:
        grp["member_count"] = counts.get(grp["id"], 0)
    return jsonify({"ok": True, "groups": raw_groups})


@app.route("/api/admin/groups", methods=["POST"])
@manage_users_required
@limiter.limit("50 per hour")
def api_admin_group_create():
    data = request.get_json(silent=True) or request.form.to_dict()
    name = str(data.get("name") or "").strip()
    description = str(data.get("description") or "").strip()
    permissions = data.get("permissions") or {}

    if not (2 <= len(name) <= 50):
        return jsonify({"ok": False, "error": gettext("Group name must be between 2 and 50 characters long.")}), 400

    try:
        new_id = database.create_user_group(name, description, permissions)
        group = database.get_user_group(new_id)
        logging.info(f"API: User group created: '{name}' (ID: {new_id}) by {session.get('username')}")
        return jsonify({"ok": True, "msg": gettext("Group '%(name)s' created successfully.", name=name), "group": group}), 201
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception:
        logging.exception("Failed to create group")
        return jsonify({"ok": False, "error": gettext("Internal server error while creating the group.")}), 500


@app.route("/api/admin/groups/<int:group_id>", methods=["PUT"])
@app.route("/api/admin/groups/<int:group_id>/update", methods=["POST"])
@manage_users_required
@limiter.limit("50 per hour")
def api_admin_group_update(group_id):
    data = request.get_json(silent=True) or request.form.to_dict()
    name = str(data.get("name") or "").strip()
    description = str(data.get("description") or "").strip()
    permissions = data.get("permissions")

    try:
        database.update_user_group(group_id, name, description, permissions)
        group = database.get_user_group(group_id)
        logging.info(f"API: User group updated: ID {group_id} by {session.get('username')}")
        return jsonify({"ok": True, "msg": gettext("Group '%(name)s' updated successfully.", name=name), "group": group})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception:
        logging.exception("Failed to update group")
        return jsonify({"ok": False, "error": gettext("Internal server error while updating the group.")}), 500


@app.route("/api/admin/groups/<int:group_id>", methods=["DELETE"])
@app.route("/api/admin/groups/<int:group_id>/delete", methods=["POST"])
@manage_users_required
@limiter.limit("20 per hour")
def api_admin_group_delete(group_id):
    try:
        database.delete_user_group(group_id)
        logging.info(f"API: User group deleted: ID {group_id} by {session.get('username')}")
        return jsonify({
            "ok": True,
            "msg": gettext("Group deleted successfully. Existing members were moved to the 'Viewer' group.")
        })
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception:
        logging.exception("Failed to delete group")
        return jsonify({"ok": False, "error": gettext("Internal server error while deleting the group.")}), 500



def _vod_access_candidate_slugs():
    """Existing archive folder slugs admins can grant to Members."""
    found = set()
    try:
        for item in list_streamers() or []:
            name = str((item or {}).get("name") or "").strip()
            if name:
                found.add(database._normalize_vod_access_slug(name) or name.rstrip("_").lower())
    except Exception:
        pass
    try:
        for slug in database.list_vod_access_defaults():
            found.add(slug)
    except Exception:
        pass
    return sorted(s for s in found if s)


def _parse_vod_access_form_streamers():
    values = request.form.getlist("streamer")
    raw = str(request.form.get("streamer_add") or "").strip()
    if raw:
        values.append(raw)
    cleaned = []
    seen = set()
    for value in values:
        slug = database._normalize_vod_access_slug(value)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        cleaned.append(slug)
    return cleaned


@app.route("/admin/vod-access/defaults", methods=["POST"])
@app.route("/admin/vod-access/role", methods=["POST"])
@app.route("/admin/vod-access/role/<role>", methods=["POST"])
@admin_required
@limiter.limit("60 per hour")
def admin_vod_access_role(role=None):
    from core import tube_auth

    role_key = tube_auth.normalize_role(
        role or request.form.get("role") or tube_auth.MEMBER_ROLE,
        default=tube_auth.MEMBER_ROLE,
    )
    if role_key != tube_auth.MEMBER_ROLE:
        return redirect(url_for("admin_view", error=gettext("VOD access grants are currently only available for members.")))
    action = str(request.form.get("action") or "save").strip().lower()
    if action == "add":
        slug = database.add_role_vod_access(role_key, request.form.get("streamer_add"))
        if not slug:
            return redirect(url_for("admin_view", error=gettext("Invalid folder slug.")))
        return redirect(url_for("admin_view", msg=gettext("Role access '%(slug)s' added.", slug=slug)))
    if action == "remove":
        slug = database._normalize_vod_access_slug(request.form.get("streamer"))
        if not slug or not database.remove_role_vod_access(role_key, slug):
            return redirect(url_for("admin_view", error=gettext("Role access grant not found.")))
        return redirect(url_for("admin_view", msg=gettext("Role access '%(slug)s' removed.", slug=slug)))
    streamers = _parse_vod_access_form_streamers()
    database.set_role_vod_access(role_key, streamers)
    return redirect(url_for("admin_view", msg=gettext("Member VOD access saved.")))


@app.route("/admin/users/approve/<username>", methods=["POST"])
@admin_required
@limiter.limit("50 per hour")
def admin_user_approve(username):
    from core import tube_auth

    try:
        row = tube_auth.approve_username(username)
        logging.info(
            "User approved: %s by %s",
            row.get("username"),
            session.get("username"),
        )
        return redirect(url_for("admin_view", msg=gettext("User '%(username)s' approved.", username=row.get("username"))))
    except ValueError as exc:
        return redirect(url_for("admin_view", error=str(exc)))


@app.route("/admin/users/deny/<username>", methods=["POST"])
@admin_required
@limiter.limit("50 per hour")
def admin_user_deny(username):
    from core import tube_auth

    if username.lower() == config.VOD_USER.lower():
        return redirect(url_for("admin_view", error=gettext("This admin user is protected.")))
    try:
        row = tube_auth.deny_username(username, delete=True)
        delete_user_settings(row.get("username") or username)
        logging.info(
            "User denied/deleted: %s by %s",
            row.get("username"),
            session.get("username"),
        )
        return redirect(url_for("admin_view", msg=gettext("Request '%(username)s' denied and deleted.", username=row.get("username"))))
    except ValueError as exc:
        return redirect(url_for("admin_view", error=str(exc)))


@app.route("/channels/add", methods=["POST"])
@admin_required
def channels_add():
    source = parse_channel_source_input(
        request.form.get("channel"),
        archive_name=request.form.get("archive_name"),
    )
    channel = source.get("name") if source else None
    if not channel:
        return redirect(url_for("channels_view", channels_msg=gettext("Invalid channel name.")))

    # Mode: 'record' (default: full recording) vs 'live_only' (notify + live stream only)
    mode = str(request.form.get("mode") or "record").strip().lower()
    is_record = 0 if mode in ("live_only", "live", "no_record", "0") else 1
    archive = source.get("archive_name") or channel

    existing_configs = {c["name"]: c for c in database.list_channel_configs()}
    is_new = channel not in existing_configs

    add_channel_db(
        channel,
        platform=source.get("platform", "twitch"),
        source_url=source.get("source_url", ""),
        archive_name=archive,
        record=is_record,
        live=0,
        notify=1,
    )
    STREAMER_CACHE["ts"] = 0

    mode_label = gettext("Full mode: recording + live + Discord") if is_record else gettext("Live only: live stream + Discord (no recording)")
    if is_new:
        if source.get("platform") == "youtube":
            msg = gettext("%(channel)s added as a YouTube source (%(mode)s).", channel=channel, mode=mode_label)
        else:
            msg = gettext("%(channel)s added (%(mode)s).", channel=channel, mode=mode_label)
    else:
        msg = gettext("%(channel)s mode updated to: %(mode)s.", channel=channel, mode=mode_label)

    return redirect(url_for("channels_view", channels_msg=msg))

@app.route("/channels/remove/<channel>", methods=["POST"])
@admin_required
def channels_remove(channel):
    name = normalize_channel_name(channel)
    channels = load_channels()
    if name in channels:
        remove_channel_db(name)
        STREAMER_CACHE["ts"] = 0
        return redirect(url_for("channels_view", channels_msg=gettext("%(name)s removed.", name=name)))
    return redirect(url_for("channels_view", channels_msg=gettext("Channel not found.")))

@app.route("/live/watch", methods=["POST"])
@login_required
def live_watch():
    """Start anonymous HLS watch for any Twitch channel (recording optional)."""
    if is_mobile() and not mobile_live_enabled():
        return redirect_mobile_live_disabled()
    channel = twitch_api.normalize_login(request.form.get("channel"))
    if not channel:
        return redirect(url_for("index", channels_msg=gettext("Invalid Twitch channel.")))
    # Members may only open live channels from their VOD allowlist.
    from core import tube_auth

    if tube_auth.is_member(current_user_role()):
        if not tube_auth.streamer_allowed_for_member(
            channel, role=current_user_role()
        ):
            abort(403)
    try:
        add_live_channel(channel)
    except Exception:
        app.logger.exception("Could not enable live proxy for %s", channel)
        return redirect(url_for("index", channels_msg=gettext("Live start failed.")))
    return redirect(url_for("twitch_watch", channel=channel))

@app.route("/live/viewers/<streamer>")
@login_required
def live_viewers(streamer):
    safe_dir = streamer_dir(streamer)
    if not safe_dir:
        return Response("{}", 200, mimetype="application/json")
    path = os.path.join(safe_dir, ".viewer_count.json")
    if not os.path.isfile(path):
        return Response("{}", 200, mimetype="application/json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return Response(f.read(), 200, mimetype="application/json")
    except Exception:
        return Response("{}", 200, mimetype="application/json")

@app.route("/channels/restart", methods=["POST"])
@admin_required
def channels_restart():
    try:
        # Restart recorder (and worker) only — never supervisorctl restart all.
        targets = ["streamrecorder:recorder", "streamrecorder:worker"]
        result = subprocess.run(
            ["supervisorctl", "restart", *targets],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            msg = gettext("Recorder & worker restarted.")
        else:
            # Fallback for non-grouped program names.
            result = subprocess.run(
                ["supervisorctl", "restart", "recorder", "worker"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            msg = gettext("Recorder & worker restarted.") if result.returncode == 0 else gettext("Restart failed.")
    except Exception:
        msg = gettext("Restart failed.")
    return redirect(url_for("channels_view", channels_msg=msg))


@app.route("/live/control/start/<streamer>", methods=["POST"])
@admin_required
def live_control_start(streamer):
    channel = normalize_channel_name(streamer)
    if not channel:
        return redirect(url_for("streamer_view", streamer=streamer, live_msg=gettext("Invalid channel.")))
    add_live_channel(channel)
    return redirect(url_for("live_view", streamer=channel))


@app.route("/live/control/stop/<streamer>", methods=["POST"])
@admin_required
def live_control_stop(streamer):
    channel = normalize_channel_name(streamer)
    if not channel:
        return redirect(url_for("channels_view", channels_msg=gettext("Invalid channel.")))
    remove_live_channel(channel)
    # Wir löschen HLS nicht sofort, damit andere Viewer nicht unterbrochen werden, 
    # der Livebot räumt auf, wenn der Prozess stoppt.
    return redirect(url_for("channels_view", channels_msg=gettext("Live stopped.")))


_DASHBOARD_MEDIA_CACHE = {}
_DASHBOARD_MEDIA_LOCK = threading.Lock()
_DASHBOARD_SHM_PATH = "/dev/shm/streamrecorder_dashboard_media.json"
_TEST_FIXTURE_STREAMERS = frozenset({"hiddenchan", "publicchan", "cloudchan", "alpha", "beta", "gamma", "demochan"})

def _is_production_base_dir() -> bool:
    if app.config.get("TESTING"):
        return False
    base = os.path.abspath(str(getattr(config, "BASE_DIR", "") or ""))
    env_base = str(os.environ.get("VOD_BASE_DIR") or "")
    if "/tmp/" in base or "\\tmp\\" in base or "/tmp/" in env_base or "\\tmp\\" in env_base:
        return False
    return True

def _load_shared_dashboard_media():
    if not _is_production_base_dir():
        return None
    now = time.time()
    expected_base = os.path.abspath(str(getattr(config, "BASE_DIR", "") or ""))

    # 1. Primary: Redis (TTL enforced natively & checked)
    try:
        from core import redis_client
        cached_redis = redis_client.get_state("dashboard_media")
        if isinstance(cached_redis, dict) and cached_redis.get("recent_vods"):
            if not cached_redis.get("base_dir") or cached_redis.get("base_dir") == expected_base:
                if (now - float(cached_redis.get("ts", 0))) <= 120.0:
                    return cached_redis
    except Exception as exc:
        logging.warning("Failed to load dashboard media from Redis: %s", exc)

    # 2. Secondary fallback: shm (Strict TTL check <= 120s)
    try:
        if os.path.isfile(_DASHBOARD_SHM_PATH):
            with open(_DASHBOARD_SHM_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            if data.get("base_dir") and data.get("base_dir") != expected_base:
                return None
            # Reject stale shm data older than 120 seconds
            if (now - float(data.get("ts", 0))) > 120.0:
                return None
            for v in (data.get("recent_vods") or []):
                if str(v.get("streamer_name") or "").lower() in _TEST_FIXTURE_STREAMERS:
                    return None
            return data
    except Exception as exc:
        logging.warning("Failed to load dashboard media from shm: %s", exc)
    return None

def _save_shared_dashboard_media(data):
    if not _is_production_base_dir():
        return
    if not data or not data.get("recent_vods"):
        return
    for v in (data.get("recent_vods") or []):
        if str(v.get("streamer_name") or "").lower() in _TEST_FIXTURE_STREAMERS:
            return
    payload = dict(data)
    payload["base_dir"] = os.path.abspath(str(getattr(config, "BASE_DIR", "") or ""))

    # 1. Primary: Redis with 120s TTL
    try:
        from core import redis_client
        redis_client.set_state("dashboard_media", payload, ex=120)
    except Exception as exc:
        logging.warning("Failed to save dashboard media to Redis: %s", exc)

    # 2. Secondary: shm file
    try:
        tmp = f"{_DASHBOARD_SHM_PATH}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _DASHBOARD_SHM_PATH)
    except (OSError, TypeError, ValueError) as exc:
        logging.warning("Failed to save dashboard media to %s: %s", _DASHBOARD_SHM_PATH, exc)

def invalidate_dashboard_media_cache():
    """Clear memory, Redis, and shm dashboard media cache."""
    global _DASHBOARD_MEDIA_CACHE
    with _DASHBOARD_MEDIA_LOCK:
        _DASHBOARD_MEDIA_CACHE.clear()
    try:
        from core import redis_client
        redis_client.invalidate_dashboard_cache()
    except Exception:
        pass
    try:
        if os.path.exists(_DASHBOARD_SHM_PATH):
            os.remove(_DASHBOARD_SHM_PATH)
    except Exception:
        pass

def _compute_dashboard_media_sync(streamers=None):
    recent_vods = []
    recent_clips = []
    category_counts = {}
    is_prod = _is_production_base_dir()
    try:
        all_s = list_streamers()
    except Exception as exc:
        logging.warning("list_streamers failed in dashboard media sync: %s", exc)
        all_s = streamers or []
    for s in (all_s or []):
        name = s.get("name") if isinstance(s, dict) else str(s)
        if not name:
            continue
        if is_prod and name.lower() in _TEST_FIXTURE_STREAMERS:
            continue
        try:
            files, _, _, _, _, _ = list_vods(name, page_size=60, filter_type="all")
            for item in files:
                fp = str(item.get("full_path") or "")
                if "/tmp/" in fp or "\\tmp\\" in fp:
                    continue
                item["streamer_name"] = name
                cat = item.get("category") or "Gaming"
                category_counts[cat] = category_counts.get(cat, 0) + 1
                if item.get("route") == "clip":
                    recent_clips.append(item)
                else:
                    recent_vods.append(item)
        except Exception as exc:
            logging.warning("list_vods failed for %s in dashboard media sync: %s", name, exc)

    def _vod_timestamp(x):
        try:
            return float(x.get("recorded_ts") or x.get("mtime") or 0)
        except Exception:
            return 0.0

    recent_vods.sort(key=_vod_timestamp, reverse=True)
    recent_clips.sort(key=_vod_timestamp, reverse=True)
    recent_vods = recent_vods[:120]
    recent_clips = recent_clips[:8]

    return {
        "ts": time.time(),
        "recent_vods": recent_vods,
        "recent_clips": recent_clips,
        "category_counts": category_counts,
    }

def _refresh_dashboard_media_bg(streamers=None):
    try:
        with app.test_request_context("/"):
            data = _compute_dashboard_media_sync()
            _save_shared_dashboard_media(data)
            with _DASHBOARD_MEDIA_LOCK:
                _DASHBOARD_MEDIA_CACHE["media"] = data
    except Exception as e:
        logging.warning("Dashboard media background refresh failed: %s", e)
    finally:
        _DASHBOARD_MEDIA_CACHE["refreshing"] = False

def get_nebula_dashboard_media(streamers, live_channels):
    global _DASHBOARD_MEDIA_CACHE
    recent_vods = []
    recent_clips = []
    category_counts = {}

    can_vod = current_user_can_access_vods() if 'current_user_can_access_vods' in globals() else True
    if can_vod:
        now = time.time()
        cur_base = os.path.abspath(str(getattr(config, "BASE_DIR", "") or ""))
        cached = _DASHBOARD_MEDIA_CACHE.get("media")
        if cached and cached.get("base_dir") and cached.get("base_dir") != cur_base:
            cached = None
        if not cached or not cached.get("recent_vods") or (now - cached.get("ts", 0) > 45.0):
            shm_cached = _load_shared_dashboard_media()
            if shm_cached and shm_cached.get("recent_vods") and (now - float(shm_cached.get("ts", 0)) <= 120.0):
                cached = shm_cached
                _DASHBOARD_MEDIA_CACHE["media"] = shm_cached
            else:
                cached = None

        if cached and cached.get("recent_vods"):
            raw_vods = list(cached.get("recent_vods") or [])
            raw_clips = list(cached.get("recent_clips") or [])
            recent_vods = [v for v in raw_vods if can_view_streamer(v.get("streamer_name"))]
            if not recent_vods and current_user_is_admin():
                recent_vods = raw_vods
            recent_clips = [c for c in raw_clips if can_view_streamer(c.get("streamer_name"))]
            category_counts = dict(cached.get("category_counts") or {})
            if (now - cached.get("ts", 0) > 60.0) and not _DASHBOARD_MEDIA_CACHE.get("refreshing"):
                _DASHBOARD_MEDIA_CACHE["refreshing"] = True
                threading.Thread(target=_refresh_dashboard_media_bg, daemon=True).start()
        else:
            with _DASHBOARD_MEDIA_LOCK:
                data = _compute_dashboard_media_sync()
                data["base_dir"] = cur_base
                if data.get("recent_vods"):
                    _save_shared_dashboard_media(data)
                _DASHBOARD_MEDIA_CACHE["media"] = data
                raw_vods = list(data.get("recent_vods") or [])
                raw_clips = list(data.get("recent_clips") or [])
                recent_vods = [v for v in raw_vods if can_view_streamer(v.get("streamer_name"))]
                if not recent_vods and current_user_is_admin():
                    recent_vods = raw_vods
                recent_clips = [c for c in raw_clips if can_view_streamer(c.get("streamer_name"))]
                category_counts = dict(data.get("category_counts") or {})

    active_live = []
    live_names = set()
    for c in (live_channels or []):
        if isinstance(c, dict):
            c_name = c.get("channel") or c.get("name") or ""
            if c_name:
                c_item = dict(c)
                if not c_item.get("thumbnail_url") or not c_item.get("title") or c_item.get("title") == f"{c_name} ist jetzt live auf Twitch":
                    st_info = twitch_api.get_stream(c_name) if 'twitch_api' in globals() else {}
                    if isinstance(st_info, dict):
                        if not c_item.get("thumbnail_url"):
                            c_item["thumbnail_url"] = st_info.get("thumbnail_url") or twitch_api.thumbnail_url(c_name)
                        if (not c_item.get("title") or c_item.get("title") == f"{c_name} ist jetzt live auf Twitch") and st_info.get("title"):
                            c_item["title"] = st_info.get("title")
                        if (not c_item.get("game_name") or c_item.get("game_name") == "Live Stream") and st_info.get("game_name"):
                            c_item["game_name"] = st_info.get("game_name")
                        if not c_item.get("viewer_count") and st_info.get("viewer_count"):
                            c_item["viewer_count"] = st_info.get("viewer_count")
                if not c_item.get("thumbnail_url"):
                    c_item["thumbnail_url"] = twitch_api.thumbnail_url(c_name)
                active_live.append(c_item)
                live_names.add(c_name.lower())
        elif isinstance(c, str) and c.strip():
            c_name = c.strip()
            st_info = twitch_api.get_stream(c_name) if 'twitch_api' in globals() else {}
            title = (st_info.get("title") if isinstance(st_info, dict) else "") or f"{c_name} ist jetzt live auf Twitch"
            game = (st_info.get("game_name") if isinstance(st_info, dict) else "") or "Live Stream"
            viewers = (st_info.get("viewer_count") if isinstance(st_info, dict) else 0) or 0
            t_url = (st_info.get("thumbnail_url") if isinstance(st_info, dict) else "") or twitch_api.thumbnail_url(c_name)
            active_live.append({
                "channel": c_name,
                "name": c_name,
                "title": title,
                "game_name": game,
                "viewer_count": viewers,
                "thumbnail_url": t_url,
            })
            live_names.add(c_name.lower())

    for st in (streamers or []):
        if not isinstance(st, dict):
            continue
        st_name = st.get("twitch_login") or st.get("name")
        is_st_live = bool(st.get("twitch_live") or st.get("online"))
        if is_st_live and st_name and st_name.lower() not in live_names:
            t_thumb = st.get("twitch_thumbnail") or st.get("thumbnail_url") or twitch_api.thumbnail_url(st_name)
            active_live.append({
                "channel": st_name,
                "title": st.get("twitch_title") or st.get("title") or f"{st_name} ist jetzt live auf Twitch",
                "game_name": st.get("twitch_game") or st.get("category") or st.get("game_name") or "Live Stream",
                "viewer_count": st.get("viewer_count") or st.get("viewers") or 0,
                "thumbnail_url": t_thumb,
            })
            live_names.add(st_name.lower())

    profiles = load_profiles()
    hero_item = None
    if active_live:
        lc = active_live[0]
        c_name = (lc.get("channel") or lc.get("name") or "streamer") if isinstance(lc, dict) else str(lc)
        p_data = profiles.get(c_name) or {}
        avatar_url = p_data.get("url")
        if not avatar_url:
            for s in (streamers or []):
                if isinstance(s, dict) and (s.get("name") == c_name or s.get("twitch_login") == c_name):
                    avatar_url = s.get("avatar_url")
                    break
        hero_item = {
            "is_live": True,
            "streamer": c_name,
            "avatar_url": avatar_url,
            "title": (lc.get("title") if isinstance(lc, dict) else None) or f"{c_name} ist jetzt live auf Twitch",
            "category": (lc.get("game_name") or lc.get("category") if isinstance(lc, dict) else None) or "Live Stream",
            "viewers": lc.get("viewer_count", 0) if isinstance(lc, dict) else 0,
            "watch_url": url_for("twitch_watch", channel=c_name),
            "thumbnail_url": (lc.get("thumbnail_url") if isinstance(lc, dict) else "") or twitch_api.thumbnail_url(c_name),
        }
    elif can_vod and recent_vods:
        fv = recent_vods[0]
        st_name = fv.get("streamer_name")
        p_data = profiles.get(st_name) or {}
        avatar_url = p_data.get("url")
        if not avatar_url:
            for s in (streamers or []):
                if isinstance(s, dict) and (s.get("name") == st_name or s.get("twitch_login") == st_name):
                    avatar_url = s.get("avatar_url")
                    break
        hero_item = {
            "is_live": False,
            "streamer": st_name,
            "avatar_url": avatar_url,
            "title": fv.get("title") or fv.get("name"),
            "category": fv.get("category") or "VOD Archiv",
            "viewers": fv.get("viewers", 0),
            "duration": fv.get("duration_str", ""),
            "watch_url": url_for("vod_view", streamer=st_name, filename=fv.get("name")),
            "thumbnail_url": fv.get("thumb_url") or "",
        }

    popular_games = sorted(category_counts.items(), key=lambda x: x[1], reverse=True)[:6]
    return recent_vods, recent_clips[:4], hero_item, popular_games, active_live


@app.route("/")
@login_required
def index():
    live_channels = load_live_channels()
    streamers = visible_streamers()
    storage = storage_info()
    storage_alert = load_storage_alert_status()
    dashboard = dashboard_context(streamers, live_channels, storage, storage_alert)
    # Home strip: only channels with a running recording (not idle/offline).
    recording_health = recording_health_rows(streamers, active_only=True)
    all_recent_vods, recent_clips, hero_item, popular_games, active_live = get_nebula_dashboard_media(streamers, live_channels)
    try:
        vod_page = int(request.args.get("page", "1") or 1)
    except ValueError:
        vod_page = 1
    home_page_size = 12
    vod_total_count = len(all_recent_vods or [])
    vod_total_pages = max(1, (vod_total_count + home_page_size - 1) // home_page_size)
    vod_page = max(1, min(vod_page, vod_total_pages))
    vod_page_numbers = list(range(1, vod_total_pages + 1))
    recent_vods = (all_recent_vods or [])[(vod_page - 1) * home_page_size : vod_page * home_page_size]
    
    if is_mobile():
        mobile_live_names = [(x.get("channel") or x.get("name") if isinstance(x, dict) else x) for x in (active_live or [])]
        return render_template_string(
            load_html_template("mobile_index.html"),
            streamers=streamers,
            live_channels=mobile_live_names,
            storage=storage,
            channels_msg=request.args.get("channels_msg"),
            recent_vods=recent_vods,
            vod_page=vod_page,
            vod_total_pages=vod_total_pages,
            vod_page_numbers=vod_page_numbers,
            vod_total_count=vod_total_count,
            hero_item=hero_item,
            can_access_vods=current_user_can_access_vods(),
        )

    return render_template_string(
        load_html_template("index.html"),
        streamers=streamers,
        channels=load_channels(),
        channels_msg=request.args.get("channels_msg"),
        live_channels=active_live,
        storage=storage,
        storage_alert=storage_alert,
        dashboard=dashboard,
        recording_health=recording_health,
        recent_vods=recent_vods,
        vod_page=vod_page,
        vod_total_pages=vod_total_pages,
        vod_page_numbers=vod_page_numbers,
        vod_total_count=vod_total_count,
        recent_clips=recent_clips,
        hero_item=hero_item,
        popular_games=popular_games,
    )

@app.route("/api/dashboard/recent_vods")
@login_required
def api_dashboard_recent_vods():
    if not current_user_can_access_vods():
        return jsonify({"ok": False, "error": "VOD access denied"}), 403

    live_channels = load_live_channels()
    streamers = visible_streamers()
    all_recent_vods, recent_clips, hero_item, popular_games, active_live = get_nebula_dashboard_media(streamers, live_channels)

    try:
        page = max(1, int(request.args.get("page", "1") or 1))
    except ValueError:
        page = 1

    page_size = 12
    total_count = len(all_recent_vods or [])
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    page = min(page, total_pages)

    page_vods = (all_recent_vods or [])[(page - 1) * page_size : page * page_size]

    top_vod = (all_recent_vods or [{}])[0]
    top_vod_name = top_vod.get("name") or ""
    latest_ts = float(top_vod.get("recorded_ts") or top_vod.get("mtime") or 0.0)

    clean_vods = []
    for v in page_vods:
        s_name = v.get("streamer_name") or ""
        v_name = v.get("name") or ""
        clean_vods.append({
            "name": v_name,
            "streamer_name": s_name,
            "title": v.get("title") or v_name,
            "url": url_for("vod_view", streamer=s_name, filename=v_name),
            "thumb_url": v.get("thumb_url") or url_for("thumb_view", streamer=s_name, filename=v_name),
            "duration_str": v.get("duration_str") or "",
            "category": v.get("category") or "VOD",
            "date_str": v.get("date_str") or "",
            "recorded_ts": float(v.get("recorded_ts") or v.get("mtime") or 0.0),
        })

    return jsonify({
        "ok": True,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
        "top_vod_name": top_vod_name,
        "latest_ts": latest_ts,
        "active_live_count": len(active_live or []),
        "vods": clean_vods,
    })


@app.route("/channels")
@admin_required
def channels_view():
    all_configs = database.list_channel_configs()
    channels = [c["name"] for c in all_configs if not is_system_backup_slug(c["name"])]
    channel_archives = {}
    channel_groups = {}
    for conf in all_configs:
        name = conf.get("name")
        if is_system_backup_slug(name):
            continue
        archive = str(conf.get("archive_name") or name or "").strip()
        channel_archives[name] = archive
        channel_groups.setdefault(archive, []).append(conf)

    return render_template_string(
        CHANNELS_TEMPLATE,
        channels=channels,
        channel_archives=channel_archives,
        channel_groups=channel_groups,
        channels_msg=request.args.get("channels_msg"),
    )

@app.route("/streamer/<streamer>")
@login_required
def streamer_view(streamer):
    if is_system_backup_slug(streamer):
        if current_user_is_admin():
            return redirect(url_for("admin_view", tab="backups"))
        abort(404)
    archive = archive_name_for(streamer)
    normalized = normalize_channel_name(streamer) or str(streamer or "").strip()
    if archive and normalized and archive != normalized:
        return redirect(url_for("streamer_view", streamer=archive, **request.args.to_dict()))
    abort_vod_denied()
    abort_hidden_streamer(archive or streamer)

    # Common Logic
    live = live_playlist_fresh(streamer)
    archive_key = archive or streamer
    is_recording = any(
        (archive_name_for(name) or name) == archive_key for name in load_channels()
    )
    online = current_recording_info(streamer) is not None
    twitch_login = ""
    twitch_live = False
    try:
        archive_cfgs = database.channels_for_archive(archive_key)
    except Exception:
        archive_cfgs = []
    twitch_login = twitch_login_from_sources(
        [normalize_channel_name(c.get("name")) or str(c.get("name") or "") for c in archive_cfgs] or [archive_key],
        archive_cfgs,
    )
    stream_info = {}
    if twitch_login:
        stream_info = twitch_api.get_stream(twitch_login, monitored=True) or {}
        twitch_live = bool(stream_info.get("is_live")) or (
            current_recording_info(twitch_login) is not None
        )
    profiles = load_profiles()
    p_data = profiles.get(archive_key) or profiles.get(streamer) or profiles.get(twitch_login) or {}
    avatar_url, avatar_source = resolve_streamer_avatar(streamer, archive_key, p_data)
    custom_avatar_url = p_data.get("custom_avatar_url") or ""
    has_custom_avatar = bool(custom_avatar_url or find_local_streamer_avatar_file(archive_key or streamer))
    banner_url = p_data.get("banner_url")
    thumbnail_url = stream_info.get("thumbnail_url") if twitch_login else None

    hero_stream = None
    if twitch_login and (twitch_live or online):
        disp = archive_display_name(archive_key or streamer)
        title = stream_info.get("title") or f"{disp} ist jetzt live auf Twitch"
        category = stream_info.get("game_name") or stream_info.get("category") or "World of Warcraft"
        viewers = stream_info.get("viewer_count") or 0
        thumb = stream_info.get("thumbnail_url") or ""
        if "{width}" in thumb:
            thumb = thumb.format(width=640, height=360)
        hero_stream = {
            "title": title,
            "category": category,
            "viewers": viewers,
            "watch_url": url_for("twitch_watch", channel=twitch_login),
            "thumbnail_url": thumb,
            "streamer": disp,
            "avatar_url": avatar_url,
        }
    filter_type = request.args.get("type", "vod")
    if filter_type not in ("vod", "clip"):
        filter_type = "vod"
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "newest")
    if filter_type in ("vod", "clip") and not str(sort or "").strip():
        sort = "newest"
    # Keep date-newest as the automatic default for archive listings.
    if filter_type in ("vod", "clip") and str(sort).strip().lower() not in (
        "newest", "oldest", "size_desc", "size_asc", "name", "duration"
    ):
        sort = "newest"
    category = (request.args.get("category") or request.args.get("cat") or "").strip()
    platform = request.args.get("platform", "").strip()
    show_empty = request.args.get("show_empty", "").lower() in ("1", "true", "yes", "on")
    analytics = streamer_archive_stats(streamer)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1

    files, total_count, total_pages, page, cnt_vod, cnt_clip = list_vods(
        streamer,
        q=q,
        sort=sort,
        filter_type=filter_type,
        show_empty=show_empty,
        page=page,
        page_size=PAGE_SIZE,
        category=category,
        platform=platform,
    )

    page_numbers = list(range(1, int(total_pages) + 1))

    try:
        archive_sources = database.channels_for_archive(archive_key)
    except Exception:
        archive_sources = []
    for src in archive_sources:
        try:
            src["record"] = bool(int(src.get("record") or 0))
        except (TypeError, ValueError):
            src["record"] = bool(src.get("record"))

    banner_url, banner_source = resolve_streamer_banner(
        streamer=streamer,
        archive_key=archive_key,
        p_data=p_data,
        stream_info=stream_info,
        twitch_live=twitch_live,
        online=online,
        files=files,
    )
    custom_banner_url = p_data.get("custom_banner_url") or ""
    has_custom_banner = bool(custom_banner_url or find_local_streamer_banner_file(archive_key or streamer))

    cached_all_items = (_VOD_LIST_CACHE.get(streamer) or {}).get("items") or files or []
    categories = sorted({str(i.get("category") or "").strip() for i in cached_all_items if str(i.get("category") or "").strip()})

    if is_mobile():
        return render_template_string(
            load_html_template("mobile_streamer.html"),
            streamer=streamer,
            hero_stream=hero_stream,
            avatar_url=avatar_url,
            avatar_source=avatar_source,
            custom_avatar_url=custom_avatar_url,
            has_custom_avatar=has_custom_avatar,
            banner_url=banner_url,
            banner_source=banner_source,
            custom_banner_url=custom_banner_url,
            has_custom_banner=has_custom_banner,
            thumbnail_url=thumbnail_url,
            display_name=archive_display_name(archive_key or streamer),
            files=files,
            live=live,
            online=online,
            twitch_live=twitch_live,
            twitch_login=twitch_login,
            current_type=filter_type,
            total_count=total_count,
            total_pages=total_pages,
            page=page,
            page_numbers=page_numbers,
            cnt_vod=cnt_vod,
            cnt_clip=cnt_clip,
            q=q,
            sort=sort,
            category=category,
            selected_cat=category,
            categories=categories,
            platform=platform,
            show_empty=show_empty,
            archive_sources=archive_sources,
        )

    return render_template_string(
        load_html_template("streamer.html"),
        streamer=streamer,
        categories=categories,
        selected_cat=category,
        hero_stream=hero_stream,
        avatar_url=avatar_url,
        avatar_source=avatar_source,
        custom_avatar_url=custom_avatar_url,
        has_custom_avatar=has_custom_avatar,
        banner_url=banner_url,
        banner_source=banner_source,
        custom_banner_url=custom_banner_url,
        has_custom_banner=has_custom_banner,
        thumbnail_url=thumbnail_url,
        display_name=archive_display_name(archive_key or streamer),
        files=files,
        live=live,
        online=online,
        twitch_live=twitch_live,
        twitch_login=twitch_login,
        is_recording=is_recording,
        live_channels=load_live_channels(),
        live_msg=request.args.get("live_msg"),
        share_link=request.args.get("share_link"),
        action_msg=request.args.get("msg"),
        q=q,
        sort=sort,
        show_empty=show_empty,
        category=category,
        platform=platform,
        analytics=analytics,
        total_runtime=(analytics.get("total_duration_str") if analytics else None) or "—",
        total_size=(analytics.get("total_size_str") if analytics else None) or "—",
        last_date=(files[0].get("date_str") if files else None) or "—",
        total_count=total_count,
        total_pages=total_pages,
        page=page,
        page_numbers=page_numbers,
        download_status=get_download_status(streamer),
        current_type=filter_type,
        cnt_vod=cnt_vod,
        cnt_clip=cnt_clip,
        archive_sources=archive_sources,
        folder_visibility=streamer_visibility(archive_key),
    )

@app.route("/streamer/<streamer>/download_all", methods=["POST"])
@admin_required
def streamer_download_all(streamer):
    threading.Thread(target=run_vod_download_task, args=(streamer,)).start()
    return redirect(url_for("streamer_view", streamer=streamer, msg=gettext("Download of all VODs started in the background.")))




@app.route("/search")
@login_required
def search_view():
    q = (request.args.get("q") or request.args.get("global") or "").strip()
    g_res = global_search(q) if q else {"streamers": [], "videos": [], "chat": []}
    return render_template_string(
        load_html_template("search.html"),
        global_query=q,
        vod_results=g_res.get("videos", []),
        results=g_res.get("chat", []),
        streamers_list=g_res.get("streamers", []),
    )


@app.route("/api/search")
@login_required
@limiter.limit("120 per minute")
def api_global_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"streamers": [], "videos": []})
    g_res = global_search(q)
    return jsonify({
        "query": q,
        "streamers": g_res.get("streamers", [])[:5],
        "videos": g_res.get("videos", [])[:10],
    })

@app.route("/vod/<streamer>/<filename>")
@login_required
def vod_view(streamer, filename):
    if maintenance_mode_enabled("vod") and not current_user_bypasses_maintenance():
        return _maintenance_response(gettext("The VOD player and archive are currently under maintenance."))
    canonical = archive_name_for(streamer)
    normalized = normalize_channel_name(streamer) or str(streamer or "").strip()
    if canonical and normalized and canonical != normalized:
        return redirect(url_for("vod_view", streamer=canonical, filename=filename, **request.args.to_dict()))
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    abort_deleted_media(streamer, filename)
    clip_ok = request.args.get("clip_ok")
    clip_error = request.args.get("clip_error")
    share_link = request.args.get("share_link") if public_shares_enabled() else None
    action_msg = request.args.get("msg")
    file_path = resolve_media_path(streamer, filename, "vod")
    if not file_path:
        abort(404)
    vod_stat = None
    try:
        vod_stat = os.stat(file_path)
        vod_size_str = format_bytes(vod_stat.st_size)
        vod_recorded_str = parse_date_from_filename(filename, vod_stat.st_mtime)
    except OSError:
        vod_size_str = "0 B"
        vod_recorded_str = "-"
    vod_meta = media_display_metadata(streamer, filename, file_path or "", stat=vod_stat, is_clip=False)
    vod_duration_sec = get_duration(file_path)
    vod_duration_str = format_seconds(vod_duration_sec)
    seek_profile = vod_seek_profile(file_path) if file_path else {"needs_repair": False}
    seek_repair_pending = bool(file_path and active_vod_remux_task(streamer, filename))
    _, hls_playlist_path, _ = hls_paths(streamer, filename)
    hls_ready = bool(
        hls_playlist_path
        and os.path.isfile(hls_playlist_path)
    )
    hls_playlist_url = None
    if hls_ready:
        try:
            hls_version = str(os.stat(hls_playlist_path).st_mtime_ns)
        except OSError:
            hls_ready = False
        else:
            hls_playlist_url = url_for("hls_playlist", streamer=streamer, filename=filename, v=hls_version)
    transcript_text = load_transcript(file_path) if file_path else None
    transcript_status = None
    if file_path:
        transcript_path = transcript_path_for_media(file_path)
        transcript_status = load_transcript_status(transcript_path)
        if transcript_text and (not transcript_status or transcript_status.get("state") != "finished"):
            transcript_status = {"state": "finished", "percent": 100, "message": gettext("Transcript finished"), "error": None}
        elif transcript_status is None:
            lock_path = transcript_lock_path(transcript_path)
            if os.path.exists(lock_path):
                transcript_status = {"state": "running", "percent": 0, "message": gettext("Transcription running..."), "error": None}

        # If no final transcript yet, but a partial exists, show it in the UI
        if not transcript_text:
            try:
                partial_path = f"{transcript_path}.partial"
                if os.path.exists(partial_path):
                    with open(partial_path, 'r', encoding='utf-8') as pf:
                        transcript_text = pf.read().strip()
            except Exception:
                pass

    vod_display_title = vod_meta.get("display_title") or vod_meta.get("title") or filename
    vod_description = clean_vod_description(vod_meta.get("description") or "")
    vod_platform = vod_meta.get("platform", "")
    chat_path = ensure_vod_chat_path(streamer, filename)
    chat_available = bool(chat_path and os.path.isfile(chat_path))
    related_recordings = find_related_recordings(streamer, filename, meta=vod_meta)

    if is_mobile():
        return render_template_string(
            MOBILE_PLAYER_TEMPLATE,
            streamer=streamer,
            filename=filename,
            media_url=url_for("media", streamer=streamer, filename=filename),
            chat_url=url_for("chat_vod", streamer=streamer, filename=filename) if chat_available else None,
            back_url=url_for("streamer_view", streamer=streamer),
            is_live=False,
            is_hls=False,
            watch_key=vod_watch_key(streamer, filename),
            done_key=vod_done_key(streamer, filename),
            vod_category=vod_meta.get("category", ""),
            vod_viewer_count_str=vod_meta.get("viewer_count_str", ""),
            vod_display_title=vod_display_title,
            vod_description=vod_description,
            display_name=archive_display_name(streamer) or streamer,
            vod_platform=vod_platform,
            vod_duration_sec=vod_duration_sec,
            related_recordings=related_recordings,
            vod_slot=vod_slot_client_config(streamer, filename),
        )

    return render_template_string(
        VOD_TEMPLATE,
        streamer=streamer,
        filename=filename,
        clip_ok=clip_ok,
        clip_error=clip_error,
        share_link=share_link,
        action_msg=action_msg,
        media_url=url_for("media", streamer=streamer, filename=filename),
        hls_playlist_url=hls_playlist_url,
        hls_ready=False,
        download_url=(
            url_for("download", streamer=streamer, filename=filename)
            if downloads_enabled()
            else None
        ),
        hls_url=url_for("hls_view", streamer=streamer, filename=filename),
        back_url=url_for("streamer_view", streamer=streamer),
        show_clip_form=vod_clip_ui_enabled(file_path),
        show_hls=True,
        show_chat=chat_available,
        show_share=bool(public_shares_enabled()),
        route_type="vod",
        is_video=True,
        vod_size_str=vod_size_str,
        vod_duration_str=vod_duration_str,
        vod_recorded_str=vod_recorded_str,
        vod_category=vod_meta.get("category", ""),
        vod_viewer_count_str=vod_meta.get("viewer_count_str", ""),
        vod_display_title=vod_display_title,
        vod_description=vod_description,
        vod_platform=vod_platform,
        vod_duration_sec=vod_duration_sec,
        watch_key=vod_watch_key(streamer, filename),
        done_key=vod_done_key(streamer, filename),
        transcript_text=transcript_text,
        transcript_status=transcript_status,
        seek_profile=seek_profile,
        seek_repair_pending=seek_repair_pending,
        related_recordings=related_recordings,
        vod_slot=vod_slot_client_config(streamer, filename),
    )


@app.route("/vod/transcribe/status/<streamer>/<filename>")
@login_required
def vod_transcribe_status(streamer, filename):
    abort_vod_denied()
    file_path = resolve_media_path(streamer, filename, "vod")
    if not file_path:
        abort(404)
    transcript_path = transcript_path_for_media(file_path)
    status = load_transcript_status(transcript_path)
    if not status:
        lock_path = transcript_lock_path(transcript_path)
        if os.path.exists(lock_path):
            status = {"state": "running", "percent": 0, "message": gettext("Transcription running..."), "error": None}
        else:
            status = {"state": "idle", "percent": 0, "message": gettext("No transcription active"), "error": None}
    if status.get("state") == "finished" and not os.path.exists(transcript_path):
        status = {"state": "error", "percent": status.get("percent", 0), "message": gettext("Transcript missing despite finished status"), "error": None}
    return jsonify(status)


@app.route("/vod/transcribe/cancel/<streamer>/<filename>", methods=["POST"])
@admin_required
def vod_transcribe_cancel(streamer, filename):
    file_path = resolve_media_path(streamer, filename, "vod")
    if not file_path:
        abort(404)
    transcript_path = transcript_path_for_media(file_path)
    lock_path = transcript_lock_path(transcript_path)
    if not os.path.exists(lock_path):
        msg = gettext("No running transcription found.")
        return redirect(url_for("vod_view", streamer=streamer, filename=filename, msg=msg))

    # Try to read PID from lock and terminate process group
    pid = None
    try:
        with open(lock_path, 'r', encoding='utf-8') as f:
            data = f.read().strip()
            if data:
                try:
                    pid = int(data.split()[0])
                except Exception:
                    pid = None
    except Exception:
        pid = None

    try:
        import signal
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
        # ensure lock removed and status set
        try:
            os.remove(lock_path)
        except Exception:
            pass
        set_transcript_status(transcript_path, "error", 0, gettext("Transcription cancelled"), gettext("cancelled"))
        msg = gettext("Transcription cancelled.")
    except Exception as exc:
        logging.exception("Could not cancel transcribe: %s", exc)
        msg = gettext("Error cancelling the transcription")
    return redirect(url_for("vod_view", streamer=streamer, filename=filename, msg=msg))


@app.route("/vod/transcribe/<streamer>/<filename>", methods=["POST"])
@admin_required
def vod_transcribe(streamer, filename):
    file_path = resolve_media_path(streamer, filename, "vod")
    if not file_path:
        abort(404)
    transcript_path = transcript_path_for_media(file_path)
    lock_path = transcript_lock_path(transcript_path)
    if os.path.exists(transcript_path):
        msg = gettext("Transcript already exists.")
    elif os.path.exists(lock_path):
        if remove_stale_transcript_lock(transcript_path, max_age=7200):
            logging.info("Removed stale transcript lock before restart: %s", lock_path)
            try:
                database.add_task('transcribe', {'media': file_path, 'transcript': transcript_path})
                msg = gettext("Stale lock removed, transcription queued in the worker. The result appears on the next page load after completion.")
            except Exception as exc:
                logging.exception("Could not enqueue transcribe task: %s", exc)
                msg = gettext("Error queuing the transcription")
        else:
            msg = gettext("Transcription is already running. Please reload later.")
    else:
        try:
            database.add_task('transcribe', {'media': file_path, 'transcript': transcript_path})
            msg = gettext("Transcription queued in the worker. The result appears on the next page load after completion.")
        except Exception as exc:
            logging.exception("Could not enqueue transcribe task: %s", exc)
            msg = gettext("Error queuing the transcription")
    return redirect(url_for("vod_view", streamer=streamer, filename=filename, msg=msg))


@app.route("/vod/transcript/<streamer>/<filename>")
@login_required
def vod_transcript_download(streamer, filename):
    file_path = resolve_media_path(streamer, filename, "vod")
    if not file_path:
        abort(404)
    transcript_path = transcript_path_for_media(file_path)
    if not os.path.exists(transcript_path):
        abort(404)
    return send_file(transcript_path, mimetype="text/plain", as_attachment=True, download_name=os.path.basename(transcript_path))


@app.route("/vod/remux/<streamer>/<filename>", methods=["POST"])
@admin_required
def vod_remux(streamer, filename):
    file_path = resolve_media_path(streamer, filename, "vod")
    if not file_path:
        abort(404)
    reencode_audio = request.form.get("reencode_audio") == "1"
    ok, msg = enqueue_vod_remux(streamer, filename, reencode_audio=reencode_audio)
    if ok:
        return redirect(url_for("vod_view", streamer=streamer, filename=filename, msg=msg))
    return redirect(url_for("vod_view", streamer=streamer, filename=filename, msg=msg))


@app.route("/clip/<streamer>/<filename>")
@login_required
def clip_view(streamer, filename):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    abort_deleted_media(streamer, filename)
    share_link = request.args.get("share_link") if public_shares_enabled() else None
    file_path = resolve_media_path(streamer, filename, "clip")
    if not file_path:
        abort(404)
    clip_stat = None
    try:
        clip_stat = os.stat(file_path)
        clip_size_str = format_bytes(clip_stat.st_size)
        clip_recorded_str = parse_date_from_filename(filename, clip_stat.st_mtime)
    except OSError:
        clip_size_str = "0 B"
        clip_recorded_str = "-"
    clip_meta = media_display_metadata(streamer, filename, file_path, stat=clip_stat, is_clip=True)
    clip_duration_sec = get_duration(file_path)
    clip_duration_str = format_seconds(clip_duration_sec)
    
    clip_display_title = clip_meta.get("display_title") or clip_meta.get("title") or filename
    clip_description = clean_vod_description(clip_meta.get("description") or "")
    clip_platform = clip_meta.get("platform", "")

    if is_mobile():
        return render_template_string(
            MOBILE_PLAYER_TEMPLATE,
            streamer=streamer,
            filename=filename,
            media_url=url_for("clip_media", streamer=streamer, filename=filename),
            chat_url=url_for("chat_vod", streamer=streamer, filename=filename), # Clips might not have synced chat easily, but we leave it
            back_url=url_for("streamer_view", streamer=streamer),
            is_live=False,
            is_hls=False,
            watch_key=vod_watch_key(streamer, filename),
            done_key=vod_done_key(streamer, filename),
            vod_category=clip_meta.get("category", ""),
            vod_viewer_count_str=clip_meta.get("viewer_count_str", ""),
            vod_display_title=clip_display_title,
            vod_description=clip_description,
            display_name=archive_display_name(streamer) or streamer,
            vod_platform=clip_platform,
            vod_duration_sec=clip_duration_sec,
            vod_slot=vod_slot_client_config(streamer, filename, enabled=False),
        )

    return render_template_string(
        VOD_TEMPLATE,
        streamer=streamer,
        filename=filename,
        clip_ok=None,
        clip_error=None,
        action_msg=None,
        share_link=share_link,
        media_url=url_for("clip_media", streamer=streamer, filename=filename),
        hls_playlist_url=None,
        hls_ready=False,
        download_url=(
            url_for("clip_download", streamer=streamer, filename=filename)
            if downloads_enabled()
            else None
        ),
        back_url=url_for("streamer_view", streamer=streamer),
        show_clip_form=False,
        show_hls=False,
        hls_url=None,
        show_chat=False,
        show_share=True,
        route_type="clip",
        is_video=True,
        vod_size_str=clip_size_str,
        vod_duration_str=clip_duration_str,
        vod_recorded_str=clip_recorded_str,
        vod_category=clip_meta.get("category", ""),
        vod_viewer_count_str=clip_meta.get("viewer_count_str", ""),
        vod_display_title=clip_display_title,
        vod_description=clip_description,
        vod_platform=clip_platform,
        vod_duration_sec=clip_duration_sec,
        watch_key=vod_watch_key(streamer, filename),
        done_key=vod_done_key(streamer, filename),
        transcript_text=None,
        transcript_status=None,
        seek_profile={"needs_repair": False},
        seek_repair_pending=False,
        related_recordings=[],
        vod_slot=vod_slot_client_config(streamer, filename, enabled=False),
    )


@app.route("/thumb/<streamer>/<filename>")
@login_required
def thumb_view(streamer, filename):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    gen_dir, gen_path, lock_path = thumb_paths(streamer, filename)
    thumb_dir, thumb_path, _ = resolve_thumb_paths(streamer, filename)
    if not gen_dir or not gen_path:
        abort(404)
    if thumb_path and os.path.isfile(thumb_path):
        same_slug = os.path.dirname(thumb_path) == gen_dir
        if USE_X_ACCEL and same_slug and request.headers.get("X-Forwarded-For"):
            base = os.path.splitext(filename)[0]
            accel_path = f"{THUMB_ACCEL_PREFIX.rstrip('/')}/{urllib.parse.quote(streamer)}/{urllib.parse.quote(base)}.jpg"
            resp = Response(status=200, mimetype="image/jpeg")
            resp.headers["X-Accel-Redirect"] = accel_path
            return resp
        resp = send_from_directory(thumb_dir, os.path.basename(thumb_path))
        # Browser-Caching fuer Thumbnails (24h)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    media_path = resolve_media_path(streamer, filename, "vod") or resolve_media_path(streamer, filename, "clip")
    if media_path:
        start_thumb_generate(media_path, gen_dir, gen_path, lock_path)

    # Placeholder SVG wenn Thumbnail noch nicht existiert
    svg = """<svg width="320" height="180" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180" preserveAspectRatio="none">
    <rect width="100%" height="100%" fill="#1a1a1a"/>
    <text x="50%" y="50%" font-family="sans-serif" font-size="14" fill="#555" text-anchor="middle" dy=".3em">Processing...</text>
    </svg>"""
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.route("/media/<streamer>/<filename>", methods=_MEDIA_HTTP_METHODS)
@media_login_required
def media(streamer, filename):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    busy = enforce_vod_watch_slot(streamer, filename)
    if busy:
        logging.info(
            "VODDBG media slot-busy %s/%s range=%s",
            streamer,
            filename,
            request.headers.get("Range"),
        )
        return busy
    if request.method != "HEAD":
        try:
            user_traffic.record(current_username(), "media", key=f"{streamer}/{filename}")
        except Exception:
            pass
    file_path = resolve_media_path(streamer, filename, "vod")
    if file_path:
        accel_path = media_accel_path(streamer, filename, file_path=file_path) if USE_X_ACCEL else None
        return send_mp4_with_range(file_path, accel_path=accel_path)
    abort(404)


@app.route("/api/vod/debug", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def vod_debug_ingest():
    """Accept client VOD seek/playback debug events for log correlation."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raw = (request.form.get("payload") or "").strip()
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
        else:
            payload = {}
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        events = []
    events = events[-80:]
    user = current_username() or "?"
    page = str((payload or {}).get("page") or "")[:180]
    meta = (payload or {}).get("meta") if isinstance(payload, dict) else {}
    logging.info(
        "VODDBG client-batch user=%s page=%s meta=%s count=%s",
        user,
        page,
        json.dumps(meta, ensure_ascii=True)[:240] if meta else "{}",
        len(events),
    )
    for row in events[-40:]:
        if not isinstance(row, dict):
            continue
        logging.info(
            "VODDBG client user=%s #%s %s/%s detail=%s snap=%s",
            user,
            row.get("id"),
            row.get("src"),
            row.get("kind"),
            json.dumps(row.get("detail") or {}, ensure_ascii=True)[:300],
            json.dumps(row.get("snap") or {}, ensure_ascii=True)[:300],
        )
    return jsonify({"ok": True, "accepted": len(events)})


@app.route("/api/vod/slot", methods=["POST"])
@login_required
@limiter.limit("60 per minute")
def vod_slot_claim():
    abort_vod_denied()
    if vod_slot_exempt():
        return jsonify(vod_watch_slots._payload(True, holder=vod_slot_holder(), active=0))
    streamer = (request.form.get("streamer") or (request.get_json(silent=True) or {}).get("streamer") or "").strip()
    filename = (request.form.get("filename") or (request.get_json(silent=True) or {}).get("filename") or "").strip()
    result = vod_watch_slots.claim(vod_slot_holder(), streamer, filename)
    status = 200 if result.get("ok") else 429
    resp = jsonify(result)
    resp.status_code = status
    if not result.get("ok"):
        resp.headers["Retry-After"] = str(int(result.get("retry_after") or 90))
    return resp


@app.route("/api/vod/slot/heartbeat", methods=["POST"])
@login_required
@limiter.limit("120 per minute")
def vod_slot_heartbeat():
    if vod_slot_exempt():
        return jsonify(vod_watch_slots._payload(True, holder=vod_slot_holder(), active=0))
    result = vod_watch_slots.heartbeat(vod_slot_holder())
    status = 200 if result.get("ok") else 429
    resp = jsonify(result)
    resp.status_code = status
    return resp


@app.route("/api/vod/slot/release", methods=["POST"])
@login_required
@limiter.limit("60 per minute")
def vod_slot_release():
    result = vod_watch_slots.release(vod_slot_holder())
    return jsonify(result)


@app.route("/download/<streamer>/<filename>")
@login_required
def download(streamer, filename):
    if not downloads_enabled():
        abort(403)
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    file_path = resolve_media_path(streamer, filename, "vod")
    if file_path:
        accel_path = media_accel_path(streamer, filename, file_path=file_path) if USE_X_ACCEL else None
        return send_mp4_with_range(file_path, accel_path=accel_path, attachment_name=filename)
    abort(404)


@app.route("/clip/media/<streamer>/<filename>", methods=_MEDIA_HTTP_METHODS)
@media_login_required
def clip_media(streamer, filename):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    file_path = resolve_media_path(streamer, filename, "clip")
    if not file_path:
        abort(404)
    if USE_X_ACCEL:
        accel_path = media_accel_path(streamer, filename, clip=True, file_path=file_path)
        return send_mp4_with_range(file_path, accel_path=accel_path)
    return send_mp4_with_range(file_path)


@app.route("/clip/download/<streamer>/<filename>")
@login_required
def clip_download(streamer, filename):
    if not downloads_enabled():
        abort(403)
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    file_path = resolve_media_path(streamer, filename, "clip")
    if not file_path:
        abort(404)
    accel_path = media_accel_path(streamer, filename, clip=True, file_path=file_path) if USE_X_ACCEL else None
    return send_mp4_with_range(file_path, accel_path=accel_path, attachment_name=filename)


PWA_CACHE_VERSION = f"streamvault-{ASSET_VERSION}"
PWA_THEME_COLOR = "#0b0b0b"
PWA_HEAD_MARKER = "<!-- StreamVault mobile app -->"
_APP_ICON_CACHE = {}


def _png_chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _line_distance(px, py, x1, y1, x2, y2):
    dx = x2 - x1
    dy = y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq <= 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _inside_rounded_rect(px, py, left, top, right, bottom, radius):
    closest_x = min(max(px, left + radius), right - radius)
    closest_y = min(max(py, top + radius), bottom - radius)
    return (px - closest_x) ** 2 + (py - closest_y) ** 2 <= radius ** 2


def _draw_nt_mark(px, py, size, scale=1.0, pad=0.0):
    """Geometric NT mark. scale/pad keep the mark inside Android maskable safe zone."""
    s = size * scale
    ox = (size - s) * 0.5 + pad * size
    oy = (size - s) * 0.5 + pad * size

    def sx(v):
        return ox + s * v

    def sy(v):
        return oy + s * v

    mark = None
    if sx(0.25) <= px <= sx(0.31) and sy(0.29) <= py <= sy(0.72) or sx(0.43) <= px <= sx(0.49) and sy(0.29) <= py <= sy(0.72) or _line_distance(px, py, sx(0.31), sy(0.70), sx(0.43), sy(0.31)) <= s * 0.035:
        mark = (114, 221, 255)
    elif sx(0.54) <= px <= sx(0.78) and sy(0.29) <= py <= sy(0.36) or sx(0.625) <= px <= sx(0.695) and sy(0.34) <= py <= sy(0.72):
        mark = (198, 255, 106)
    return mark


def app_icon_png_bytes(size, maskable=False):
    size = max(32, min(512, int(size or 180)))
    cache_key = (size, bool(maskable))
    cached = _APP_ICON_CACHE.get(cache_key)
    if cached:
        return cached

    rows = []
    left = size * 0.12
    top = size * 0.12
    right = size * 0.88
    bottom = size * 0.88
    radius = size * 0.18
    inner_left = left + size * 0.025
    inner_top = top + size * 0.025
    inner_right = right - size * 0.025
    inner_bottom = bottom - size * 0.025
    inner_radius = radius * 0.82
    # Maskable icons must be full-bleed; keep the NT mark inside the 80% safe zone.
    mark_scale = 0.72 if maskable else 1.0

    for y in range(size):
        row = bytearray()
        v = y / max(1, size - 1)
        for x in range(size):
            u = x / max(1, size - 1)
            px = x + 0.5
            py = y + 0.5
            r = int(5 + 8 * (1 - v) + 4 * u)
            g = int(8 + 12 * (1 - u) + 6 * v)
            b = int(14 + 24 * u + 10 * (1 - v))

            if maskable or _inside_rounded_rect(px, py, left, top, right, bottom, radius):
                if maskable or _inside_rounded_rect(
                    px, py, inner_left, inner_top, inner_right, inner_bottom, inner_radius
                ):
                    r = int(8 + 6 * u)
                    g = int(13 + 10 * (1 - v))
                    b = int(21 + 16 * u)
                else:
                    r = int(60 + 30 * (1 - u))
                    g = int(190 + 40 * u)
                    b = int(175 + 20 * v)

            mark = _draw_nt_mark(px, py, size, scale=mark_scale)
            if mark:
                r, g, b = mark
            row.extend((r, g, b, 255))
        rows.append(b"\x00" + bytes(row))

    raw = b"".join(rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )
    _APP_ICON_CACHE[cache_key] = png
    return png


def app_icon_response(size, maskable=False):
    response = Response(app_icon_png_bytes(size, maskable=maskable), mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def _pwa_icon_entries():
    """Separate any + maskable icons — required for reliable Android Chrome install."""
    icons = []
    for size in (192, 512):
        icons.append(
            {
                "src": url_for("app_icon_png", size=size),
                "sizes": f"{size}x{size}",
                "type": "image/png",
                "purpose": "any",
            }
        )
        icons.append(
            {
                "src": url_for("app_icon_maskable_png", size=size),
                "sizes": f"{size}x{size}",
                "type": "image/png",
                "purpose": "maskable",
            }
        )
    return icons


def mobile_app_head_tags():
    return f"""{PWA_HEAD_MARKER}
    <meta name="theme-color" content="{PWA_THEME_COLOR}">
    <meta name="color-scheme" content="dark">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-title" content="StreamVault">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <link rel="manifest" href="{url_for('web_manifest')}">
    <link rel="apple-touch-icon" sizes="180x180" href="{url_for('apple_touch_icon')}">
    <link rel="icon" type="image/png" sizes="192x192" href="{url_for('app_icon_png', size=192)}">
    <link rel="icon" type="image/png" sizes="512x512" href="{url_for('app_icon_png', size=512)}">
    <script>(function(){{if(!Promise.allSettled){{Promise.allSettled=function(p){{return Promise.all(Array.prototype.map.call(p,function(x){{return Promise.resolve(x).then(function(v){{return{{status:"fulfilled",value:v}};}}).catch(function(r){{return{{status:"rejected",reason:r}};}});}}));}};}}if(window.fetch&&window.Request){{try{{if(new Request("/").credentials==="omit"){{var _f=window.fetch;window.fetch=function(u,o){{if((!o||!o.credentials)&&typeof u==="string"&&(u.charAt(0)==="/"||u.indexOf(location.origin)===0)){{o=Object.assign({{credentials:"same-origin"}},o||{{}});}}return _f.call(this,u,o);}};}}}}catch(e){{}}}}var MOBILE_TV_PARITY_SELECTORS=[".ns-command-rail",".ns-workspace-header .ns-search-box",".ns-workspace-header p","body.page-index .ns-workspace-header",".nebula-side-rail",".nebula-empty-live-banner",".nebula-hero-play-icon",".nebula-vod-play-overlay","#self-password-modal","#btnTranscriptToggle","#vodTranscriptPanel",".chat-filter-bar",".vod-description",".mobile-vod-description","#btnAdminMetaToggle","#adminMetaEditorPanel","#vodAdminMetaOverlay",".nebula-page-title","#btnOpenEditor","#vodEditorModal",".vod-file-actions form[action*=\x27vod_remux\x27]",".vod-file-actions a[title=\x27Download\x27]",".vod-file-actions button[title=\x27In Discord hochladen\x27]",".vod-file-actions button[title=\x27Teilen\x27]",".mobile-action-bar button[title=\x27Teilen\x27]",".kicker-type",".twitch-monitored-pill",".twitch-switcher"];function syncMobileTvParity(){{var isMob=(window.innerWidth>0&&window.innerWidth<=768)||/Mobi|Android/i.test(navigator.userAgent);document.documentElement.classList.toggle("is-mobile-view",isMob);if(!isMob||!document.body)return;var q=MOBILE_TV_PARITY_SELECTORS.join(",");var nodes=document.querySelectorAll(q);for(var i=0;i<nodes.length;i++){{if(nodes[i]&&nodes[i].parentNode)nodes[i].parentNode.removeChild(nodes[i]);}}}}window.__nsSyncMobileTvParity=syncMobileTvParity;if(document.readyState==="loading"){{document.addEventListener("DOMContentLoaded",syncMobileTvParity,{{once:true}});}}else{{syncMobileTvParity();}}var _rt=null;window.addEventListener("resize",function(){{if(_rt)clearTimeout(_rt);_rt=setTimeout(syncMobileTvParity,120);}},{{passive:true}});}})();</script>
    <script>
      (function() {{
        if ("serviceWorker" in navigator) {{
          navigator.serviceWorker.getRegistrations().then(function(regs) {{
            for (var i = 0; i < regs.length; i++) regs[i].unregister();
          }}).catch(function() {{}});
        }}

        var dismissedKey = "ns-pwa-install-dismissed-v1";
        var standalone = window.matchMedia("(display-mode: standalone)").matches
          || window.navigator.standalone === true;
        if (standalone) return;
        try {{
          if (window.localStorage && localStorage.getItem(dismissedKey) === "1") return;
        }} catch (err) {{}}

        var deferredPrompt = null;
        var banner = null;

        function dismissBanner(persist) {{
          if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
          banner = null;
          if (persist) {{
            try {{ localStorage.setItem(dismissedKey, "1"); }} catch (err) {{}}
          }}
        }}

        function showInstallBanner() {{
          if (banner || !deferredPrompt) return;
          banner = document.createElement("div");
          banner.id = "ns-pwa-install";
          banner.className = "ns-pwa-install";
          banner.setAttribute("role", "dialog");
          banner.setAttribute("aria-label", "App installieren");
          banner.innerHTML = ""
            + "<div class=\\"ns-pwa-install__copy\\">"
            + "<strong>StreamVault</strong>"
            + "<span>Als App auf dem Startbildschirm installieren</span>"
            + "</div>"
            + "<div class=\\"ns-pwa-install__actions\\">"
            + "<button type=\\"button\\" class=\\"ns-pwa-install__btn\\" data-action=\\"install\\">Installieren</button>"
            + "<button type=\\"button\\" class=\\"ns-pwa-install__dismiss\\" data-action=\\"dismiss\\" aria-label=\\"Schließen\\">×</button>"
            + "</div>";
          banner.addEventListener("click", function(ev) {{
            var action = ev.target && ev.target.getAttribute && ev.target.getAttribute("data-action");
            if (action === "dismiss") {{
              dismissBanner(true);
              return;
            }}
            if (action !== "install" || !deferredPrompt) return;
            var promptEvent = deferredPrompt;
            deferredPrompt = null;
            promptEvent.prompt();
            Promise.resolve(promptEvent.userChoice).then(function() {{
              dismissBanner(true);
            }}).catch(function() {{
              dismissBanner(false);
            }});
          }});
          (document.body || document.documentElement).appendChild(banner);
        }}

        window.addEventListener("beforeinstallprompt", function(event) {{
          event.preventDefault();
          deferredPrompt = event;
          if (document.readyState === "loading") {{
            document.addEventListener("DOMContentLoaded", showInstallBanner, {{ once: true }});
          }} else {{
            showInstallBanner();
          }}
        }});

        window.addEventListener("appinstalled", function() {{
          deferredPrompt = null;
          dismissBanner(true);
        }});
      }})();
    </script>"""


def inject_mobile_app_head(response):
    if response.direct_passthrough or response.status_code < 200 or response.status_code >= 300:
        return response
    if response.mimetype != "text/html":
        return response
    html = response.get_data(as_text=True)
    if not html or PWA_HEAD_MARKER in html or "</head>" not in html.lower():
        return response
    html = re.sub(r"</head>", mobile_app_head_tags() + "\n  </head>", html, count=1, flags=re.IGNORECASE)
    response.set_data(html)
    return response


@app.route("/manifest.webmanifest")
def web_manifest():
    manifest = {
        "id": "/",
        "lang": ui_i18n.LOCALES[ui_i18n.current_locale_id()][0],
        "name": "StreamVault",
        "short_name": "StreamVault",
        "description": gettext("Private video and stream archive console."),
        "start_url": url_for("index"),
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "browser"],
        "background_color": "#05070b",
        "theme_color": PWA_THEME_COLOR,
        "orientation": "any",
        "prefer_related_applications": False,
        "icons": _pwa_icon_entries(),
        "shortcuts": [
            {"name": "Dashboard", "url": url_for("index")},
            {"name": "Live", "url": url_for("index", filter="live")},
        ],
    }
    response = Response(json.dumps(manifest, ensure_ascii=False), mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.route("/sw.js")
def service_worker_js():
    js = """self.addEventListener("install", function(e) {
  self.skipWaiting();
});
self.addEventListener("activate", function(e) {
  e.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(keys.map(function(k) { return caches.delete(k); }));
    }).then(function() {
      return self.registration.unregister();
    })
  );
});
"""
    response = Response(js, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/offline")
def offline_page():
    html = """<!doctype html>
<html lang="__SV_LANG__">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>StreamVault offline</title>
    <style>
      :root{color-scheme:dark}
      body{margin:0;min-height:100vh;display:grid;place-items:center;background:#05070b;color:#f5f7fb;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
      main{width:min(420px,calc(100vw - 32px));border:1px solid rgba(148,163,184,.25);background:#0b1018;border-radius:12px;padding:24px}
      h1{font-size:1.25rem;margin:0 0 10px}
      p{margin:0;color:#aeb8c8;line-height:1.5}
    </style>
  </head>
  <body>
    <main>
      <h1>StreamVault</h1>
      <p>__SV_OFFLINE_TEXT__</p>
    </main>
  </body>
</html>"""
    offline_text = gettext("No connection. As soon as the device is back online, the app continues loading the page normally.")
    html = html.replace("__SV_LANG__", ui_i18n.LOCALES[ui_i18n.current_locale_id()][0])
    html = html.replace("__SV_OFFLINE_TEXT__", str(escape(offline_text)))
    response = Response(html, mimetype="text/html")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    return app_icon_response(180)


@app.route("/app-icon-<int:size>.png")
def app_icon_png(size):
    return app_icon_response(size)


@app.route("/app-icon-<int:size>-maskable.png")
def app_icon_maskable_png(size):
    return app_icon_response(size, maskable=True)


@app.route("/favicon.ico")
def favicon():
    return app_icon_response(32)


def csp_nonce():
    """Per-request nonce for inline <script> blocks (templates: nonce="{{ csp_nonce() }}")."""
    if not has_request_context():
        return ""
    nonce = getattr(g, "csp_nonce_value", None)
    if not nonce:
        nonce = secrets.token_urlsafe(18)
        g.csp_nonce_value = nonce
    return nonce


app.jinja_env.globals["csp_nonce"] = csp_nonce

_CSP_INJECTED_SCRIPT_HASHES = []


def csp_injected_script_hashes():
    """sha256 sources for the static inline scripts that inject_mobile_app_head() adds.

    Hashed from mobile_app_head_tags() itself (trusted server code), never from the
    response body, so injected markup cannot whitelist itself.
    """
    if not _CSP_INJECTED_SCRIPT_HASHES:
        import base64

        for body in re.findall(r"<script>(.*?)</script>", mobile_app_head_tags(), flags=re.S):
            digest = hashlib.sha256(body.encode("utf-8")).digest()
            _CSP_INJECTED_SCRIPT_HASHES.append("'sha256-" + base64.b64encode(digest).decode("ascii") + "'")
    return _CSP_INJECTED_SCRIPT_HASHES


# Third-party hosts the UI talks to (chat emotes: BTTV/FFZ/7TV, sub emotes: ivr.fi;
# clip editor slider: cdnjs). Keep in sync with static/chat_emotes.js.
CSP_SCRIPT_HOSTS = "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com"
CSP_STYLE_HOSTS = "https://fonts.googleapis.com https://cdnjs.cloudflare.com"
CSP_IMG_HOSTS = (
    "https://static-cdn.jtvnw.net https://badges.twitch.tv https://cdn.discordapp.com "
    "https://cdn.betterttv.net https://cdn.frankerfacez.com https://cdn.7tv.app"
)
CSP_CONNECT_HOSTS = (
    "https://*.googleapis.com https://clients3.google.com "
    "https://api.betterttv.net https://api.frankerfacez.com https://7tv.io https://api.ivr.fi"
)


@app.after_request
def set_security_headers(response):
    response = inject_mobile_app_head(response)
    response.headers["X-Content-Type-Options"] = "nosniff"
    path = request.path or ""
    if not (
        path.startswith("/live/obs/")
        or path.startswith("/chat/obs/")
        or path.startswith("/alerts/overlay")
        or path.startswith("/tts/overlay")
    ):
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers.pop("X-Frame-Options", None)
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # HSTS is set once by nginx (avoid duplicate Strict-Transport-Security headers).
    # Enforced by default; VOD_CSP_ENFORCE=false falls back to Report-Only.
    # Inline <script> blocks need nonce="{{ csp_nonce() }}"; inline event handlers
    # (onclick= ...) are blocked, so templates bind events with addEventListener.
    # worker-src / media blob: cover live clip overlay + HLS blob URLs.
    script_src = " ".join(["'self'", "'nonce-" + csp_nonce() + "'", *csp_injected_script_hashes(), CSP_SCRIPT_HOSTS])
    csp = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self'; "
        "object-src 'none'; "
        "frame-src 'self' https://player.twitch.tv https://www.twitch.tv; "
        "child-src 'self' blob: https://player.twitch.tv https://www.twitch.tv; "
        "worker-src 'self' blob:; "
        "script-src " + script_src + "; "
        "script-src-attr 'none'; "
        "style-src 'self' 'unsafe-inline' " + CSP_STYLE_HOSTS + "; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob: " + CSP_IMG_HOSTS + "; "
        "media-src 'self' blob:; "
        "connect-src 'self' blob: " + CSP_CONNECT_HOSTS + ";"
    )
    header = "Content-Security-Policy" if os.environ.get("VOD_CSP_ENFORCE", "true").lower() in ("true", "1", "yes") else "Content-Security-Policy-Report-Only"
    response.headers.setdefault(header, csp)
    return response


@app.route("/share/create/<streamer>/<filename>", methods=["POST"])
@admin_required
def share_create(streamer, filename):
    if not public_shares_enabled():
        abort(403)
    abort_hidden_streamer(streamer)
    route = request.form.get("route", "vod")
    if route not in ("vod", "clip"):
        abort(400)
    file_path = resolve_media_path(streamer, filename, route)
    if not file_path:
        abort(404)
    max_views = request.form.get("max_views")
    token = create_share(streamer, filename, route, max_views=max_views)
    path = url_for("public_view", token=token)
    if PUBLIC_BASE:
        link = f"{PUBLIC_BASE.rstrip('/')}{path}"
    else:
        link = url_for("public_view", token=token, _external=True)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accept_mimetypes.accept_json:
        return Response(json.dumps({"link": link}), 200, mimetype="application/json")

    next_target = request.form.get("next")
    if next_target == "vod_view":
        return redirect(url_for("vod_view", streamer=streamer, filename=filename, share_link=link))
    elif next_target == "clip_view":
        return redirect(url_for("clip_view", streamer=streamer, filename=filename, share_link=link))
        
    return redirect(url_for("streamer_view", streamer=streamer, share_link=link))


@app.route("/vod/delete/<streamer>/<filename>", methods=["POST"])
@admin_required
def vod_delete(streamer, filename):
    success = delete_video_file(streamer, filename, is_clip=False)
    
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return Response(json.dumps({"success": success}), 200, mimetype="application/json")
        
    msg = gettext("Video deleted.") if success else gettext("Video could not be deleted.")
    return redirect(url_for("streamer_view", streamer=streamer, msg=msg))


@app.route("/vod/rename/<streamer>/<filename>", methods=["POST"])
@admin_required
def vod_rename(streamer, filename):
    new_name = request.form.get("new_name") or request.form.get("filename") or ""
    ok, msg, final_name = rename_video_file(streamer, filename, new_name, is_clip=False)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return Response(
            json.dumps({"success": ok, "message": msg, "filename": final_name}),
            200 if ok else 400,
            mimetype="application/json",
        )
    if ok and final_name and final_name != filename:
        return redirect(url_for("vod_view", streamer=streamer, filename=final_name, msg=msg))
    return redirect(url_for("vod_view", streamer=streamer, filename=filename, msg=msg))


@app.route("/vod/info/<streamer>/<filename>", methods=["POST"])
@admin_required
def vod_info_update(streamer, filename):
    title = request.form.get("title")
    description = request.form.get("description")
    # Only update fields that were submitted.
    kwargs = {}
    if "title" in request.form:
        kwargs["title"] = title
    if "description" in request.form:
        kwargs["description"] = description
    ok, msg = update_vod_info_fields(streamer, filename, **kwargs)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return Response(
            json.dumps({"success": ok, "message": msg}),
            200 if ok else 400,
            mimetype="application/json",
        )
    target = "vod_view"
    if str(request.form.get("is_clip") or "") in ("1", "true", "yes"):
        target = "clip_view"
    return redirect(url_for(target, streamer=streamer, filename=filename, msg=msg))


@app.route("/clip/delete/<streamer>/<filename>", methods=["POST"])
@admin_required
def clip_delete(streamer, filename):
    success = delete_video_file(streamer, filename, is_clip=True)
    
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return Response(json.dumps({"success": success}), 200, mimetype="application/json")

    msg = gettext("Clip deleted.") if success else gettext("Clip could not be deleted.")
    return redirect(url_for("streamer_view", streamer=streamer, msg=msg))


@app.route("/clip/rename/<streamer>/<filename>", methods=["POST"])
@admin_required
def clip_rename(streamer, filename):
    new_name = request.form.get("new_name") or request.form.get("filename") or ""
    ok, msg, final_name = rename_video_file(streamer, filename, new_name, is_clip=True)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return Response(
            json.dumps({"success": ok, "message": msg, "filename": final_name}),
            200 if ok else 400,
            mimetype="application/json",
        )
    if ok and final_name and final_name != filename:
        return redirect(url_for("clip_view", streamer=streamer, filename=final_name, msg=msg))
    return redirect(url_for("clip_view", streamer=streamer, filename=filename, msg=msg))



@app.route("/clip/discord/<streamer>/<filename>", methods=["POST"])
@admin_required
def clip_discord_upload(streamer, filename):
    return Response(
        json.dumps({"ok": False, "message": gettext("Discord integration not active")}),
        400,
        mimetype="application/json",
    )

@app.route("/public/<token>")
def public_view(token):
    if not public_shares_enabled():
        abort(404)
    resolved = resolve_share(token)
    if not resolved:
        abort(404)
    streamer, filename, route = resolved
    file_path = resolve_media_path(streamer, filename, route)
    if not file_path:
        abort(404)
    is_video = filename.lower().endswith(".mp4")
    public_stat = None
    try:
        public_stat = os.stat(file_path)
        public_size_str = format_bytes(public_stat.st_size)
        public_recorded_str = parse_date_from_filename(filename, public_stat.st_mtime)
    except OSError:
        public_size_str = ""
        public_recorded_str = ""
    public_meta = media_display_metadata(streamer, filename, file_path, stat=public_stat, is_clip=(route == "clip"))
    public_duration_sec = get_duration(file_path) if is_video else 0
    return render_template_string(
        VOD_TEMPLATE,
        streamer=streamer,
        filename=filename,
        clip_ok=None,
        clip_error=None,
        action_msg=None,
        share_link=None,
        media_url=url_for("public_media", token=token),
        hls_playlist_url=None,
        hls_ready=False,
        download_url=(
            url_for("public_download", token=token) if downloads_enabled() else None
        ),
        back_url=None,
        show_clip_form=False,
        show_hls=False,
        hls_url=None,
        show_chat=False,
        show_share=False,
        route_type="public",
        is_video=is_video,
        vod_size_str=public_size_str,
        vod_duration_str=format_seconds(public_duration_sec) if is_video else "",
        vod_recorded_str=public_recorded_str,
        vod_category=public_meta.get("category", ""),
        vod_viewer_count_str=public_meta.get("viewer_count_str", ""),
        vod_display_title=public_meta.get("display_title") or public_meta.get("title") or filename,
        vod_platform=public_meta.get("platform", ""),
        vod_duration_sec=public_duration_sec,
        watch_key=vod_watch_key(streamer, filename),
        done_key=vod_done_key(streamer, filename),
        transcript_text=None,
        transcript_status=None,
        seek_profile={"needs_repair": False},
        seek_repair_pending=False,
        related_recordings=[],
        vod_slot=vod_slot_client_config(streamer, filename, enabled=False),
    )


@app.route("/public/media/<token>")
def public_media(token):
    if not public_shares_enabled():
        abort(404)
    resolved = resolve_share(token)
    if not resolved:
        abort(404)
    streamer, filename, route = resolved
    file_path = resolve_media_path(streamer, filename, route)
    if not file_path:
        abort(404)
    accel_path = None
    if USE_X_ACCEL:
        accel_path = media_accel_path(streamer, filename, clip=(route == "clip"), file_path=file_path)
    return send_mp4_with_range(file_path, accel_path=accel_path)


@app.route("/public/download/<token>")
def public_download(token):
    if not public_shares_enabled() or not downloads_enabled():
        abort(404)
    resolved = resolve_share(token)
    if not resolved:
        abort(404)
    streamer, filename, route = resolved
    file_path = resolve_media_path(streamer, filename, route)
    if not file_path:
        abort(404)
    accel_path = None
    if USE_X_ACCEL:
        accel_path = media_accel_path(streamer, filename, clip=(route == "clip"), file_path=file_path)
    return send_mp4_with_range(file_path, accel_path=accel_path, attachment_name=filename)


@app.route("/chat/vod/<streamer>/<filename>")
@login_required
def chat_vod(streamer, filename):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    log_path = ensure_vod_chat_path(streamer, filename)
    if not log_path or not os.path.isfile(log_path):
        return Response("[]", 200, mimetype="application/json")

    # Static 10-Minute Chunk Serving (Pfeiler 2)
    chunk_param = request.args.get("chunk")
    if chunk_param is not None:
        try:
            chunk_idx = int(chunk_param)
        except (ValueError, TypeError):
            return Response("[]", 400, mimetype="application/json")
        from core import chat_chunker
        chunk_file = chat_chunker.get_chunk_file_path(
            config.BASE_DIR, streamer, filename, chunk_idx, log_path=log_path
        )
        if not chunk_file or not os.path.isfile(chunk_file):
            return Response("[]", 200, mimetype="application/json")
        resp = send_file(chunk_file, mimetype="application/json")
        resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
        return resp

    if request.args.get("meta") == "1":
        from core import chat_chunker
        meta = chat_chunker.ensure_chat_chunks(
            config.BASE_DIR, streamer, filename, log_path
        )
        return jsonify(meta)

    try:
        start = float(request.args.get("start", "0"))
        end = float(request.args.get("end", "0"))
    except ValueError:
        return Response("[]", 200, mimetype="application/json")
    if end <= 0 or end < start:
        return Response("[]", 200, mimetype="application/json")
    items = []
    seen = set()
    recent = {}
    try:
        index = get_chat_index(log_path)
        if index:
            rels = [r for r, _ in index]
            i = bisect.bisect_right(rels, start) - 1
            offset = index[i][1] if i >= 0 else 0
            with open(log_path, "rb") as f:
                f.seek(offset)
                for raw in f:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                    rel = data.get("rel")
                    if rel is None:
                        continue
                    try:
                        rel_val = float(rel)
                    except Exception:
                        continue
                    if rel_val < start:
                        continue
                    if rel_val > end:
                        if items:
                            break
                        continue
                    parsed = chat_item_from_row(data, time_key="rel")
                    if not parsed:
                        continue
                    item, dedupe_token, _, user = parsed
                    key = f"{rel_val}|{user}|{dedupe_token}"
                    close_key = f"{user}|{dedupe_token}"
                    if key in seen:
                        continue
                    if close_key in recent and abs(rel_val - recent[close_key]) < 1.25:
                        continue
                    recent[close_key] = rel_val
                    seen.add(key)
                    items.append(item)
                    if len(items) >= 500:
                        break
        else:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                    rel = data.get("rel")
                    if rel is None:
                        continue
                    try:
                        rel_val = float(rel)
                    except Exception:
                        continue
                    if rel_val < start:
                        continue
                    if rel_val > end:
                        if items:
                            break
                        continue
                    parsed = chat_item_from_row(data, time_key="rel")
                    if not parsed:
                        continue
                    item, dedupe_token, _, user = parsed
                    key = f"{rel_val}|{user}|{dedupe_token}"
                    close_key = f"{user}|{dedupe_token}"
                    if key in seen:
                        continue
                    if close_key in recent and abs(rel_val - recent[close_key]) < 1.25:
                        continue
                    recent[close_key] = rel_val
                    seen.add(key)
                    items.append(item)
                    if len(items) >= 500:
                        break
    except Exception:
        return Response("[]", 200, mimetype="application/json")
    return Response(json.dumps(items, ensure_ascii=False), 200, mimetype="application/json")


@app.route("/chat/badges/<streamer>")
@login_required
def chat_badges(streamer):
    # Reserved alias for global badge warm-up from the client.
    channel = "" if str(streamer or "").strip().lower() in ("_global", "global", "-") else streamer
    data = get_twitch_badge_assets(channel)
    badges = data.get("badges", []) if isinstance(data, dict) else []
    available = bool(data.get("available")) if isinstance(data, dict) else False
    resp = jsonify({
        "available": available,
        "badges": badges,
    })
    if available and badges:
        resp.headers["Cache-Control"] = f"private, max-age={min(TWITCH_BADGE_CACHE_TTL_SEC, 3600)}"
    else:
        # Never let browsers keep empty badge payloads for a day.
        resp.headers["Cache-Control"] = "private, max-age=30, must-revalidate"
    return resp


@app.route("/admin/badges/sync", methods=["POST"])
@admin_required
def admin_badges_sync():
    force = str(request.form.get("force") or request.args.get("force") or "").lower() in (
        "1", "true", "yes", "on",
    )
    # Kick off in background so the admin UI stays responsive for full library syncs.
    if _TWITCH_BADGE_SYNC_STATE.get("running"):
        return jsonify({"ok": False, "message": gettext("Badge sync is already running."), **twitch_badge_library_stats()})

    def _runner():
        try:
            sync_twitch_badge_library(force=force)
        except Exception:
            logging.exception("Admin Twitch badge sync crashed")

    threading.Thread(target=_runner, name="twitch-badge-sync-admin", daemon=True).start()
    return jsonify({
        "ok": True,
        "message": gettext("Badge sync started."),
        "force": force,
        **twitch_badge_library_stats(),
    })


@app.route("/admin/badges/status")
@admin_required
def admin_badges_status():
    return jsonify({"ok": True, **twitch_badge_library_stats()})


@app.route("/chat/badge-assets/<set_id>/<badge_id>/<filename>")
@login_required
def chat_badge_asset(set_id, badge_id, filename):
    if not CHAT_BADGE_PART_RE.match(set_id) or not CHAT_BADGE_PART_RE.match(badge_id):
        abort(404)
    if not re.match(r"^[124]x\.(png|webp|gif|jpg)$", filename):
        abort(404)
    asset_dir = os.path.join(TWITCH_BADGE_ASSET_DIR, set_id, badge_id)
    resp = send_from_directory(asset_dir, filename, conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


def _live_chat_log_path(streamer):
    safe_dir = streamer_dir(streamer)
    best_path = None
    best_mtime = -1.0
    if safe_dir:
        for path in _iter_runtime_state_files(
            safe_dir, ".live_chat.jsonl", ".live_chat.", ".jsonl"
        ):
            if not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if size <= 0:
                continue
            if mtime >= best_mtime:
                best_mtime = mtime
                best_path = path
    if best_path:
        return best_path
    info = current_recording_info(streamer)
    candidate = info.get("log_file") if isinstance(info, dict) else None
    if candidate and safe_dir:
        try:
            real_candidate = os.path.realpath(str(candidate))
            real_base = os.path.realpath(safe_dir)
            if real_candidate.startswith(real_base + os.sep) and os.path.isfile(real_candidate):
                return real_candidate
        except OSError:
            pass
    return None


@app.route("/chat/live/<streamer>")
@media_login_required
def chat_live(streamer):
    abort_hidden_streamer(streamer)
    # Prefer the live chat jsonl (wall-clock `t`) so channel-point / mod events
    # from the IRC chat process show up immediately in Live/Watch views.
    log_path = _live_chat_log_path(streamer)

    if not log_path or not os.path.isfile(log_path):
        return Response("[]", 200, mimetype="application/json")
    try:
        since = float(request.args.get("since", "0"))
    except ValueError:
        since = 0.0
    items = []
    seen = set()
    recent = {}
    
    # Optimize: Read only last 500 lines to prevent memory issues with large logs
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            tail = deque(f, maxlen=500)
    except Exception:
        return Response("[]", 200, mimetype="application/json")
    lines = list(tail)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        parsed = chat_item_from_row(data, time_key="t")
        if not parsed:
            continue
        item, dedupe_token, t_val, user = parsed
        if since > 0 and t_val <= since:
            continue
        key = f"{t_val}|{user}|{dedupe_token}"
        close_key = f"{user}|{dedupe_token}"
        if key in seen:
            continue
        if close_key in recent and abs(t_val - recent[close_key]) < 1.25:
            continue
        recent[close_key] = t_val
        seen.add(key)
        items.append(item)
        if len(items) >= 200:
            break
    return Response(json.dumps(items, ensure_ascii=False), 200, mimetype="application/json")


@app.route("/editor/<streamer>/<filename>")
@login_required
def editor_view(streamer, filename):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    return render_template_string(
        CLIP_EDITOR_TEMPLATE,
        streamer=streamer,
        filename=filename,
        media_url=url_for("media", streamer=streamer, filename=filename),
        back_url=url_for("vod_view", streamer=streamer, filename=filename),
        clip_ok=request.args.get("clip_ok"),
        clip_error=request.args.get("clip_error"),
        initial_time=request.args.get("t", 0)
    )

@app.route("/editor/live/<streamer>")
@login_required
def editor_live_view(streamer):
    abort_hidden_streamer(streamer)
    return render_template_string(
        CLIP_EDITOR_TEMPLATE,
        streamer=streamer,
        filename=f"LIVE {streamer}",
        media_url=url_for("live_playlist", streamer=streamer),
        back_url=url_for("live_view", streamer=streamer),
        clip_ok=request.args.get("clip_ok"),
        clip_error=request.args.get("clip_error"),
        is_live=True,
        initial_time=request.args.get("t", 0)
    )

@app.route("/clip/vod/<streamer>/<filename>", methods=["POST"])
@admin_required
def clip_vod(streamer, filename):
    wants_json = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.accept_mimetypes.accept_json
    )

    def respond(ok, message, status=200, clip_filename=None, job=None):
        if wants_json:
            if job is not None:
                payload = _clip_job_payload(job)
                status_code = 200
                if payload.get("status") == "error":
                    status_code = 500 if "fehlgeschlagen" in (payload.get("message") or "").lower() else 400
                    if "nicht installiert" in (payload.get("message") or ""):
                        status_code = 503
                    elif "nicht verfügbar" in (payload.get("message") or ""):
                        status_code = 404
                return Response(json.dumps(payload), status=status_code, mimetype="application/json")
            payload = {"ok": ok, "message": message}
            if clip_filename:
                payload["clip_filename"] = clip_filename
                payload["clip_url"] = url_for("clip_view", streamer=streamer, filename=clip_filename)
            return Response(json.dumps(payload), status=status, mimetype="application/json")
        if request.form.get("from_editor"):
            target = "editor_view"
        else:
            target = "vod_view"
        key = "clip_ok" if ok else "clip_error"
        args = {key: message}
        if ok and clip_filename:
            args["clip_name"] = clip_filename
        return redirect(url_for(target, streamer=streamer, filename=filename, **args))

    if not filename.endswith(".mp4"):
        abort(404)
    source = resolve_vod_clip_source(streamer, filename)
    if not source.get("ok"):
        err = source.get("error") or gettext("Recording not available.")
        return respond(False, err, status=404)
    try:
        start_sec = parse_time_to_seconds(request.form.get("start", "0"))
        duration_sec = float(request.form.get("duration", "0"))
    except Exception:
        err = gettext("Invalid time value.")
        return respond(False, err, status=400)
    if not math.isfinite(start_sec) or not math.isfinite(duration_sec):
        err = gettext("Invalid time value.")
        return respond(False, err, status=400)
    if start_sec < 0:
        err = gettext("Start time must be >= 0.")
        return respond(False, err, status=400)
    if duration_sec <= 0:
        err = gettext("Duration must be > 0.")
        return respond(False, err, status=400)
    if duration_sec > clip_engine.MAX_DURATION_SEC:
        duration_sec = float(clip_engine.MAX_DURATION_SEC)
    label = request.form.get("clip_name", "")
    archive_dir = streamer_dir(streamer)
    if not archive_dir:
        err = gettext("Invalid clip path.")
        return respond(False, err, status=400)

    # Cloud/remote sources: queue so the request does not block on network + encode.
    # Local files (and complete Drive playback cache) stay synchronous for the classic editor form.
    if source.get("source") not in ("local", "cache") or wants_json:
        job = clip_engine.enqueue_vod_range(
            streamer,
            archive_dir,
            source["input_path"],
            start_sec,
            duration_sec,
            title=label,
            http_headers=source.get("http_headers"),
            source=source.get("source") or "vod",
        )
        if wants_json:
            return respond(job.get("status") != "error", job.get("message") or "", job=job)
        if job.get("status") == "done" and job.get("clip_filename"):
            return respond(True, gettext("Clip created."), clip_filename=job["clip_filename"])
        if job.get("status") == "error":
            return respond(False, job.get("message") or gettext("Clip failed."), status=500)
        return respond(True, gettext("Creating clip…"))

    output_path = clip_output_path(streamer, filename, start_sec, duration_sec, label)
    if not output_path:
        err = gettext("Invalid clip path.")
        return respond(False, err, status=400)
    ok = clip_engine.extract_vod_range(
        source["input_path"],
        output_path,
        start_sec,
        duration_sec,
        http_headers=source.get("http_headers"),
    )
    if ok and os.path.isfile(output_path) and os.path.getsize(output_path) > 1024:
        msg = gettext("Clip created.")
        return respond(True, msg, clip_filename=os.path.basename(output_path))
    err = gettext("Clip failed.")
    return respond(False, err, status=500)


@app.route("/clip/live/<streamer>", methods=["POST"])
@admin_required
def clip_live(streamer):
    wants_json = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.accept_mimetypes.accept_json
    )
    live_key = normalize_channel_name(streamer) or streamer
    output_streamer = normalize_channel_name(request.form.get("clip_streamer") or "") or live_key

    def respond(ok, message, status=200, clip_filename=None, job=None):
        if wants_json:
            if job is not None:
                payload = _clip_job_payload(job)
                status_code = 200
                if payload.get("status") == "error":
                    status_code = 500 if "fehlgeschlagen" in (payload.get("message") or "").lower() else 400
                    if "nicht installiert" in (payload.get("message") or ""):
                        status_code = 503
                    elif "nicht verfügbar" in (payload.get("message") or "") or "nicht aktiv" in (
                        payload.get("message") or ""
                    ):
                        status_code = 404
                return Response(json.dumps(payload), status=status_code, mimetype="application/json")
            payload = {"ok": ok, "message": message}
            if clip_filename:
                payload["clip_filename"] = clip_filename
                payload["clip_url"] = url_for(
                    "clip_view", streamer=output_streamer, filename=clip_filename
                )
            return Response(json.dumps(payload), status=status, mimetype="application/json")
        if request.form.get("from_editor"):
            target = "editor_live_view"
            key = "clip_ok" if ok else "clip_error"
            return redirect(url_for(target, streamer=live_key, **{key: message}))
        if ok and clip_filename:
            return redirect(url_for("clip_view", streamer=output_streamer, filename=clip_filename))
        return redirect(url_for("live_view", streamer=live_key, **{
            ("clip_ok" if ok else "clip_error"): message
        }))

    try:
        duration_sec = float(request.form.get("duration", "0"))
    except Exception:
        return respond(False, gettext("Invalid time value."), status=400)

    # Check if start is provided (Editor Mode) or None (Quick Mode)
    start_val = request.form.get("start")
    try:
        start_sec = float(start_val) if start_val is not None and start_val != "" else None
    except Exception:
        return respond(False, gettext("Invalid time value."), status=400)

    if start_sec is not None and (not math.isfinite(start_sec) or start_sec < 0):
        return respond(False, gettext("Start time must be >= 0."), status=400)
    if not math.isfinite(duration_sec):
        return respond(False, gettext("Invalid time value."), status=400)
    if duration_sec <= 0:
        duration_sec = 60
    if duration_sec > clip_engine.MAX_DURATION_SEC:
        duration_sec = float(clip_engine.MAX_DURATION_SEC)

    archive_dir = streamer_dir(output_streamer)
    if not archive_dir:
        return respond(False, gettext("Invalid clip path."), status=400)

    try:
        add_live_channel(live_key)
        touch_live_heartbeat(live_key)
    except Exception:
        app.logger.exception("Could not enable live HLS for live clip %s", live_key)

    _, playlist = live_paths(live_key)
    recording = clip_engine.find_active_recording_file(archive_dir, live_key)
    has_playlist = bool(playlist and os.path.isfile(playlist))
    has_recording = bool(recording and os.path.isfile(recording))
    if not has_playlist and not has_recording:
        return respond(False, gettext("Live stream not available."), status=404)

    label = request.form.get("clip_name", "")
    job = clip_engine.enqueue_live_range(
        output_streamer,
        archive_dir,
        playlist if has_playlist else "",
        recording if has_recording else "",
        start_sec,
        duration_sec,
        title=label,
    )
    if wants_json:
        return respond(job.get("status") != "error", job.get("message") or "", job=job)
    if job.get("status") == "done" and job.get("clip_filename"):
        return respond(True, gettext("Live clip created."), clip_filename=job["clip_filename"])
    if job.get("status") == "error":
        return respond(False, job.get("message") or gettext("Clip failed."), status=500)
    return respond(True, gettext("Creating clip…"))


def _clip_job_payload(job):
    payload = clip_engine.public_job(job)
    filename = payload.get("clip_filename")
    streamer = payload.get("streamer")
    if filename and streamer:
        payload["clip_url"] = url_for("clip_view", streamer=streamer, filename=filename)
    return payload


@app.route("/clip/instant/<streamer>", methods=["POST"])
@admin_required
@limiter.limit("30 per minute")
def clip_instant(streamer):
    live_key = normalize_channel_name(streamer) or streamer
    output_streamer = normalize_channel_name(request.form.get("clip_streamer") or "") or live_key
    source = str(request.form.get("source") or "live").strip().lower()
    title = request.form.get("title") or request.form.get("clip_name") or ""
    archive_dir = streamer_dir(output_streamer)
    if not archive_dir:
        payload = {"ok": False, "status": "error", "message": gettext("Invalid clip path.")}
        return Response(json.dumps(payload), status=400, mimetype="application/json")

    if source == "vod":
        filename = request.form.get("filename") or ""
        if os.path.basename(filename) != filename or not filename.endswith(".mp4"):
            payload = {"ok": False, "status": "error", "message": gettext("Recording not available.")}
            return Response(json.dumps(payload), status=404, mimetype="application/json")
        clip_source = resolve_vod_clip_source(output_streamer, filename)
        if not clip_source.get("ok"):
            payload = {
                "ok": False,
                "status": "error",
                "message": clip_source.get("error") or gettext("Recording not available."),
            }
            return Response(json.dumps(payload), status=404, mimetype="application/json")
        try:
            position = float(request.form.get("position") or 0)
        except (TypeError, ValueError):
            position = 0.0
        if not math.isfinite(position) or position < 0:
            position = 0.0
        job = clip_engine.enqueue_vod_instant(
            output_streamer,
            archive_dir,
            clip_source["input_path"],
            position,
            title=title,
            http_headers=clip_source.get("http_headers"),
            source=clip_source.get("source") or "vod",
        )
    else:
        # Embed-watch has no local player; still wake live HLS so clips can cut.
        try:
            add_live_channel(live_key)
            touch_live_heartbeat(live_key)
        except Exception:
            app.logger.exception("Could not enable live HLS for instant clip %s", live_key)
        _, playlist = live_paths(live_key)
        recording = clip_engine.find_active_recording_file(archive_dir, live_key)
        job = clip_engine.enqueue_live_instant(
            output_streamer,
            archive_dir,
            playlist if playlist and os.path.isfile(playlist) else "",
            recording,
            title=title,
        )
    payload = _clip_job_payload(job)
    status = 200
    if payload.get("status") == "error":
        message = payload.get("message") or ""
        if "nicht installiert" in message:
            status = 503
        elif "nicht verfügbar" in message or "nicht aktiv" in message:
            status = 404
        elif "Speicher" in message or "Pfad" in message:
            status = 400
        else:
            status = 400
    return Response(json.dumps(payload), status=status, mimetype="application/json")


@app.route("/clip/job/<job_id>")
@login_required
def clip_job(job_id):
    job = clip_engine.get_job(job_id)
    if not job:
        payload = {"ok": False, "status": "error", "message": gettext("Clip job not found.")}
        return Response(json.dumps(payload), status=404, mimetype="application/json")
    return Response(json.dumps(_clip_job_payload(job)), status=200, mimetype="application/json")


@app.route("/admin/clip-settings", methods=["POST"])
@admin_required
def admin_clip_settings():
    saved = clip_engine.save_settings({
        "preroll_sec": request.form.get("preroll_sec"),
        "postroll_sec": request.form.get("postroll_sec"),
        "hotkey": request.form.get("hotkey"),
        "quality": request.form.get("quality"),
        "include_audio": bool(request.form.get("include_audio")),
    })
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return Response(json.dumps({"ok": True, "settings": saved}), mimetype="application/json")
    return redirect(url_for("admin_view", msg=gettext("Clip settings saved.")))


@app.route("/admin/maintenance", methods=["POST"])
@admin_required
@limiter.limit("20 per hour")
def admin_maintenance():
    from core import site_maintenance

    action = (request.form.get("action") or "").strip().lower()
    if action == "granular":
        scopes = {
            "vod": bool(request.form.get("scope_vod")),
            "live": bool(request.form.get("scope_live")),
            "mobile": bool(request.form.get("scope_mobile")),
        }
        raw_bypass = request.form.get("bypass_users") or ""
        bypass_list = [u.strip() for u in raw_bypass.replace("\n", ",").split(",") if u.strip()]
        site_maintenance.save_state(
            scopes=scopes,
            bypass_users=bypass_list,
            updated_by=current_username(),
        )
        return redirect(url_for("admin_view", msg=gettext("Area maintenance and exempt users saved successfully.")))

    if action not in ("enable", "disable"):
        return redirect(url_for("admin_view", error=gettext("Invalid maintenance action.")))

    want_enabled = action == "enable"
    notify = request.form.get("notify_discord") == "1"

    result = site_maintenance.set_enabled(
        want_enabled,
        updated_by=current_username(),
        notify_discord=notify,
    )
    if want_enabled:
        if result.get("discord_ok") is True:
            msg = gettext("Maintenance mode active. Discord notice was sent.")
        elif result.get("discord_ok") is False:
            msg = gettext(
                "Maintenance mode active, but the Discord notice failed: %(error)s",
                error=result.get("discord_error") or gettext("unknown"),
            )
        else:
            msg = gettext("Maintenance mode active.")
    else:
        if result.get("discord_ok") is True:
            msg = gettext("Maintenance mode ended. Discord notice (available again) was sent.")
        elif result.get("discord_ok") is False:
            msg = gettext(
                "Maintenance mode ended, but the Discord notice failed: %(error)s",
                error=result.get("discord_error") or gettext("unknown"),
            )
        else:
            msg = gettext("Maintenance mode ended. The site is available to everyone again.")
    return redirect(url_for("admin_view", msg=msg))


@app.route("/hls/<streamer>/<filename>", methods=["GET"])
@login_required
def hls_view(streamer, filename):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    file_path = resolve_media_path(streamer, filename, "vod")
    if not file_path:
        abort(404)
    _, playlist, _ = hls_paths(streamer, filename)
    ready = os.path.isfile(playlist)
    playlist_url = url_for("hls_playlist", streamer=streamer, filename=filename) if ready else ""
    return render_template_string(
        HLS_TEMPLATE,
        streamer=streamer,
        filename=filename,
        ready=ready,
        playlist_url=playlist_url,
    )


@app.route("/hls/generate/<streamer>/<filename>", methods=["POST"])
@admin_required
def hls_generate(streamer, filename):
    file_path = resolve_media_path(streamer, filename, "vod")
    if not file_path:
        abort(404)
    hls_dir, _, lock_path = hls_paths(streamer, filename)
    start_hls_generate(file_path, hls_dir, lock_path)
    return redirect(url_for("hls_view", streamer=streamer, filename=filename))


@app.route("/hls/playlist/<streamer>/<filename>", methods=_MEDIA_HTTP_METHODS)
@media_login_required
def hls_playlist(streamer, filename):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    busy = enforce_vod_watch_slot(streamer, filename)
    if busy:
        return busy
    hls_dir, playlist, lock_path = hls_paths(streamer, filename)
    if not hls_dir or not playlist or not lock_path:
        abort(404)
    if not os.path.isfile(playlist):
        abort(404)
    try:
        hls_version = str(os.stat(playlist).st_mtime_ns)
        with open(playlist, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        abort(404)

    def segment_url_for(seg):
        return url_for("hls_segment", streamer=streamer, filename=filename, segment=seg, v=hls_version)

    output = rewrite_hls_playlist_for_client(lines, segment_url_for)
    return hls_playlist_response("".join(output), live=False)


@app.route("/hls/segment/<streamer>/<filename>/<segment>", methods=_MEDIA_HTTP_METHODS)
@media_login_required
def hls_segment(streamer, filename, segment):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    busy = enforce_vod_watch_slot(streamer, filename)
    if busy:
        return busy
    if os.path.basename(segment) != segment:
        abort(404)
    lower = segment.lower()
    if not (lower.endswith(".ts") or lower.endswith(".m4s") or lower.endswith(".mp4")):
        abort(404)
    hls_dir, _, lock_path = hls_paths(streamer, filename)
    if not hls_dir or not lock_path:
        abort(404)
    segment_path = os.path.join(hls_dir, segment)
    if not os.path.isfile(segment_path):
        abort(404)
    mime = hls_segment_mimetype(segment)
    if USE_X_ACCEL:
        accel_path = f"{HLS_ACCEL_PREFIX.rstrip('/')}/{urllib.parse.quote(streamer)}/{urllib.parse.quote(os.path.splitext(filename)[0])}/{urllib.parse.quote(segment)}"
        resp = Response(status=200, mimetype=mime)
        resp.headers["X-Accel-Redirect"] = accel_path
        return apply_hls_delivery_headers(resp, live=False, immutable=True)
    resp = send_from_directory(hls_dir, segment, conditional=False, max_age=0, mimetype=mime)
    return apply_hls_delivery_headers(resp, live=False, immutable=True)


def _recording_logins_set():
    logins = set()
    try:
        for conf in database.list_channel_configs(mode="record"):
            if str(conf.get("platform") or "twitch").lower() != "twitch":
                continue
            login = twitch_api.normalize_login(conf.get("name"))
            if login and current_recording_info(login) is not None:
                logins.add(login)
    except Exception:
        pass
    return logins


@app.route("/live")
@login_required
def twitch_live_hub():
    # Browse hub (Entdecken / Zuletzt angesehen) removed — keep route as bookmark-safe redirect.
    return redirect(url_for("index"))


@app.route("/api/live/debug")
@app.route("/api/live/debug/<streamer>")
@admin_required
def api_live_debug(streamer=None):
    """Admin-only live-watch diagnostics (no secrets)."""
    from core import live_watch_diag

    channels = None
    if streamer:
        channel = normalize_channel_name(streamer) or twitch_api.normalize_login(streamer)
        if not channel:
            abort(404)
        channels = [channel]
    try:
        logs = int(request.args.get("logs") or 20)
    except (TypeError, ValueError):
        logs = 20
    report = live_watch_diag.collect_report(channels, logs=max(5, min(logs, 80)))
    # Keep HTTP response lighter than CLI --json full tails unless ?full=1
    if (request.args.get("full") or "").strip() not in ("1", "true", "yes"):
        for ch in report.get("channels") or []:
            logs_obj = ch.get("logs") or {}
            logs_obj.pop("ffmpeg_tail", None)
            logs_obj.pop("streamlink_tail", None)
    return jsonify({"ok": True, "report": report})


@app.route("/api/live/client-debug", methods=["POST"])
@login_required
def api_live_client_debug():
    """Receive sanitized proxy-player telemetry from the watch page."""
    from core import live_watch_diag

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        abort(400)
    channel = normalize_channel_name(payload.get("channel")) or twitch_api.normalize_login(
        payload.get("channel")
    )
    event = {
        "channel": channel or "",
        "mode": payload.get("mode") or "proxy",
        "level": payload.get("level") or "info",
        "event": payload.get("event") or "unknown",
        "detail": payload.get("detail") or "",
        "ready": bool(payload.get("ready")),
        "waited_sec": payload.get("waited_sec"),
        "fatal_type": payload.get("fatal_type") or "",
        "fatal_details": payload.get("fatal_details") or "",
        "reconnect_attempt": payload.get("reconnect_attempt"),
        "user_agent": (request.headers.get("User-Agent") or "")[:160],
    }
    try:
        live_watch_diag.append_client_debug(event)
    except Exception:
        app.logger.exception("live client-debug append failed")
        return jsonify({"ok": False}), 500
    return jsonify({"ok": True})


@app.route("/api/live/ready/<streamer>")
@login_required
def api_live_ready(streamer):
    """Poll whether the anonymous livebot HLS proxy is ready for playback."""
    channel = normalize_channel_name(streamer) or twitch_api.normalize_login(streamer)
    if not channel:
        abort(404)
    abort_hidden_streamer(channel)
    # Keep demand warm and live=1 asserted while the watch UI polls.
    touch_live_heartbeat(channel)
    ready = live_playlist_fresh(channel)
    twitch_live = None
    try:
        info = twitch_api.get_stream(channel, monitored=True)
        twitch_live = bool(info.get("is_live"))
    except Exception:
        twitch_live = None
    # Recorder/HLS can be ahead of Helix; never tell the phone "offline" while we ingest.
    if twitch_live is not True:
        if ready or current_recording_info(channel) is not None:
            twitch_live = True
    return jsonify({
        "ok": True,
        "ready": ready,
        "channel": channel,
        "twitch_live": twitch_live,
        "title": (info.get("title") if isinstance(info, dict) else "") or "",
        "game_name": (info.get("game_name") if isinstance(info, dict) else "") or "",
        "viewer_count": (info.get("viewer_count") if isinstance(info, dict) else 0) or 0,
        "playlist_url": url_for("live_playlist", streamer=channel) if ready else "",
    })


@app.route("/live/watch/<channel>")
@login_required
def twitch_watch(channel):
    if maintenance_mode_enabled("live") and not current_user_bypasses_maintenance():
        return _maintenance_response(gettext("The live stream area is currently under maintenance."))
    if is_mobile() and not mobile_live_enabled():
        return redirect_mobile_live_disabled()
    login = twitch_api.normalize_login(channel)
    if not login:
        abort(404)
    from core import tube_auth

    if tube_auth.is_member(current_user_role()):
        if not tube_auth.streamer_allowed_for_member(
            login, role=current_user_role()
        ):
            abort(403)
    parents = twitch_api.embed_parent_hosts(request.host)
    stream = twitch_api.get_stream(login, monitored=login in set(twitch_api.monitored_twitch_logins()))
    if login in _recording_logins_set():
        stream = dict(stream)
        stream["is_live"] = True
        stream["monitored"] = True
        stream["recording"] = True

    archive_name = ""
    try:
        conf = database.get_channel_config(login)
        if conf:
            archive_name = normalize_channel_name(conf.get("archive_name")) or login
    except Exception:
        archive_name = ""
    if not archive_name:
        try:
            for conf in database.list_channel_configs():
                if twitch_api.normalize_login(conf.get("name")) == login:
                    archive_name = normalize_channel_name(conf.get("archive_name")) or login
                    break
        except Exception:
            pass

    # Default: Twitch embed without account cookies (iframe credentialless ≈ Incognito).
    # Optional ?proxy=1 for server HLS when embed is blocked or unsupported.
    # When a Turbo/sub OAuth token is configured, default to the server proxy so
    # live watch gets hide_ads without joining chat as that account.
    proxy_arg = (request.args.get("proxy") or "").strip().lower()
    embed_arg = (request.args.get("embed") or "").strip().lower()
    if proxy_arg in ("1", "true", "yes", "on") or embed_arg in ("0", "false", "no", "off"):
        use_hls_proxy = True
    elif proxy_arg in ("0", "false", "no", "off") or embed_arg in ("1", "true", "yes", "on"):
        use_hls_proxy = False
    elif is_mobile() or is_ios_client():
        use_hls_proxy = True
    elif config.LIVE_TWITCH_OAUTH_ENABLED:
        use_hls_proxy = True
    else:
        use_hls_proxy = False
    proxy_key = login
    hls_available = False
    if use_hls_proxy:
        # Ban-bypass: pull anonymously via livebot/streamlink (no viewer Twitch cookies).
        try:
            add_live_channel(proxy_key)
        except Exception:
            app.logger.exception("Could not enable live proxy for %s", proxy_key)
        touch_live_heartbeat(proxy_key)
        hls_available = live_playlist_fresh(proxy_key)

    monitored_rows = twitch_api.enrich_monitored_streams(recording_logins=_recording_logins_set())
    switcher = [row for row in monitored_rows if row.get("is_live")]
    # Always keep the currently opened channel in the tab strip.
    if not any(row.get("login") == login for row in switcher):
        switcher.insert(0, {
            "login": login,
            "display_name": stream.get("display_name") or login,
            "viewer_count": int(stream.get("viewer_count") or 0),
            "is_live": bool(stream.get("is_live")),
        })
    switcher = switcher[:8]

    chat_streamer = archive_name or login
    chat_feed_url = url_for("chat_live", streamer=chat_streamer)
    watch_embed_url = url_for("twitch_watch", channel=login, embed=1)
    watch_proxy_url = url_for("twitch_watch", channel=login, proxy=1)
    live_debug = (request.args.get("debug") or "").strip().lower() in ("1", "true", "yes", "on")
    context = {
        "channel": login,
        "display_name": stream.get("display_name") or login,
        "title": stream.get("title") or "",
        "game_name": stream.get("game_name") or "",
        "viewer_count": int(stream.get("viewer_count") or 0),
        "is_live": bool(stream.get("is_live")),
        "monitored": bool(stream.get("monitored")),
        "player_url": twitch_api.player_embed_url(login, parents),
        "chat_feed_url": chat_feed_url,
        "chat_streamer": chat_streamer,
        "archive_name": archive_name,
        "hls_available": hls_available,
        "use_hls_proxy": use_hls_proxy,
        "live_debug": live_debug,
        "proxy_key": proxy_key,
        "hls_playlist_url": url_for("live_playlist", streamer=proxy_key) if use_hls_proxy else "",
        "hls_ready_url": url_for("api_live_ready", streamer=proxy_key) if use_hls_proxy else "",
        "hls_client_debug_url": url_for("api_live_client_debug"),
        "watch_embed_url": watch_embed_url,
        "watch_proxy_url": watch_proxy_url,
        "live_oauth_enabled": bool(config.LIVE_TWITCH_OAUTH_ENABLED),
        "switcher": switcher,
        "embed_parents": parents,
        "live_player_profile": "mobile" if is_mobile() else "desktop",
    }
    template = twitch_templates.get_watch_template(mobile=is_mobile())
    resp = make_response(render_template_string(template, **context))
    resp.headers["Cache-Control"] = "no-store"
    if "live_player_mode" in request.cookies:
        resp.delete_cookie("live_player_mode", path="/")
    return resp


@app.route("/api/twitch/monitored")
@login_required
def api_twitch_monitored():
    streams = twitch_api.enrich_monitored_streams(recording_logins=_recording_logins_set())
    return jsonify({
        "ok": True,
        "helix": twitch_api.has_helix_auth(),
        "streams": streams,
        "live_count": sum(1 for row in streams if row.get("is_live")),
    })


@app.route("/api/twitch/search")
@login_required
def api_twitch_search():
    query = (request.args.get("q") or "").strip()
    try:
        first = int(request.args.get("first") or 20)
    except (TypeError, ValueError):
        first = 20
    payload = twitch_api.search_channels(query, first=first)
    return jsonify(payload)


@app.route("/api/twitch/streams")
@login_required
def api_twitch_streams():
    game_id = (request.args.get("game_id") or "").strip()
    language = (request.args.get("language") or "").strip()
    try:
        first = int(request.args.get("first") or 24)
    except (TypeError, ValueError):
        first = 24
    payload = twitch_api.list_streams(game_id=game_id, first=first, language=language)
    return jsonify(payload)


@app.route("/api/twitch/categories")
@login_required
def api_twitch_categories():
    query = (request.args.get("q") or "").strip()
    try:
        first = int(request.args.get("first") or 20)
    except (TypeError, ValueError):
        first = 20
    payload = twitch_api.search_categories(query, first=first)
    return jsonify(payload)


@app.route("/api/twitch/stream/<login>")
@login_required
def api_twitch_stream(login):
    channel = twitch_api.normalize_login(login)
    if not channel:
        abort(404)
    stream = twitch_api.get_stream(channel, monitored=channel in set(twitch_api.monitored_twitch_logins()))
    if channel in _recording_logins_set():
        stream = dict(stream)
        stream["is_live"] = True
        stream["recording"] = True
    return jsonify({"ok": True, "stream": stream})


@app.route("/live/<streamer>")
@login_required
def live_view(streamer):
    if maintenance_mode_enabled("live") and not current_user_bypasses_maintenance():
        return _maintenance_response(gettext("The live stream area is currently under maintenance."))
    channel = normalize_channel_name(streamer) or twitch_api.normalize_login(streamer) or streamer
    abort_hidden_streamer(channel)
    if is_mobile() and not mobile_live_enabled():
        return redirect_mobile_live_disabled()
    # Discord / bookmarks often hit bare /live/<name>. That HLS-studio page is easy
    # to leave stuck on „Suche Stream…“ — send users to the watch player instead.
    # Opt into the raw studio with ?studio=1.
    studio = (request.args.get("studio") or "").strip().lower() in ("1", "true", "yes", "on")
    if not studio:
        login = twitch_api.normalize_login(channel) or channel
        return redirect(url_for("twitch_watch", channel=login), code=302)
    # Signal watch demand immediately — otherwise LIVE_SKIP_WHEN_RECORDING keeps
    # livebot idle and the studio page polls forever on a missing playlist.
    try:
        add_live_channel(channel)
    except Exception:
        app.logger.exception("Could not enable live channel for studio %s", channel)
    touch_live_heartbeat(channel)
    ready = live_playlist_exists(channel)
    playlist_url = url_for("live_playlist", streamer=channel)

    if is_mobile():
        return render_template_string(
            MOBILE_PLAYER_TEMPLATE,
            streamer=channel,
            filename=f"LIVE: {channel}",
            media_url=playlist_url,
            chat_url=url_for("chat_live", streamer=channel),
            back_url=url_for("streamer_view", streamer=channel),
            is_live=True,
            is_hls=True,
            watch_key="",
            done_key="",
            vod_category="",
            vod_viewer_count_str="",
            vod_display_title=f"LIVE {channel}",
            display_name=archive_display_name(channel) or channel,
        )

    resp = make_response(render_template_string(
        LIVE_TEMPLATE,
        streamer=channel,
        ready=ready,
        playlist_url=playlist_url,
        live_ready_url=url_for("api_live_ready", streamer=channel),
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/live/obs/<streamer>")
@login_required
def live_obs(streamer):
    """Fullscreen picture and sound for an OBS browser source."""
    channel = normalize_channel_name(streamer) or ""
    if not channel or channel != str(streamer or "").strip().lower():
        abort(404)
    abort_hidden_streamer(channel)
    try:
        add_live_channel(channel)
    except Exception:
        app.logger.exception("Could not enable live proxy for obs %s", channel)
    touch_live_heartbeat(channel)
    playlist_url = url_for("live_playlist", streamer=channel)
    resp = make_response(render_template_string(
        LIVE_OBS_TEMPLATE,
        playlist_url=playlist_url,
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/chat/obs/<streamer>")
@login_required
def chat_obs(streamer):
    """Fullscreen transparent chat overlay for an OBS browser source."""
    channel = normalize_channel_name(streamer) or ""
    if not channel or channel != str(streamer or "").strip().lower():
        abort(404)
    abort_hidden_streamer(channel)
    resp = make_response(render_template_string(
        CHAT_OBS_TEMPLATE,
        streamer=channel,
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp



@app.route("/live/playlist/<streamer>", methods=_MEDIA_HTTP_METHODS)
@media_login_required
def live_playlist(streamer):
    abort_hidden_streamer(streamer)
    touch_live_heartbeat(streamer)
    if not live_playlist_fresh(streamer):
        abort(404)
    _, playlist = live_paths(streamer)
    if not playlist:
        abort(404)
    if not os.path.isfile(playlist):
        abort(404)
    try:
        with open(playlist, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        abort(404)

    def segment_url_for(seg):
        return url_for("live_segment", streamer=streamer, segment=seg)

    output = rewrite_hls_playlist_for_client(lines, segment_url_for)
    return hls_playlist_response("".join(output), live=True)


@app.route("/live/segment/<streamer>/<segment>", methods=_MEDIA_HTTP_METHODS)
@media_login_required
def live_segment(streamer, segment):
    abort_hidden_streamer(streamer)
    touch_live_heartbeat(streamer)
    if os.path.basename(segment) != segment:
        abort(404)
    lower = segment.lower()
    if not (lower.endswith(".ts") or lower.endswith(".m4s") or lower.endswith(".mp4")):
        abort(404)
    live_dir, _ = live_paths(streamer)
    if not live_dir:
        abort(404)
    segment_path = os.path.join(live_dir, segment)
    if not os.path.isfile(segment_path):
        abort(404)
    # Serve existing segments even if playlist mtime briefly stalls; reject only
    # when both playlist and this segment are stale.
    if not live_playlist_fresh(streamer):
        try:
            seg_age = time.time() - os.path.getmtime(segment_path)
        except OSError:
            abort(404)
        if seg_age > max(LIVE_HLS_TTL_SEC * 2, 60):
            abort(404)
    mime = hls_segment_mimetype(segment)
    if USE_X_ACCEL:
        accel_path = f"{LIVE_HLS_ACCEL_PREFIX.rstrip('/')}/{urllib.parse.quote(streamer)}/{urllib.parse.quote(segment)}"
        resp = Response(status=200, mimetype=mime)
        resp.headers["X-Accel-Redirect"] = accel_path
        try:
            resp.headers["Content-Length"] = str(os.path.getsize(segment_path))
        except OSError:
            pass
    else:
        resp = send_from_directory(live_dir, segment, as_attachment=False, conditional=True, mimetype=mime)
    return apply_hls_delivery_headers(resp, live=True)

@app.route("/files")
@admin_required
def files_view():
    return redirect(url_for("index"))
    resources = load_resources()
    file_meta = resources.get("files", {})
    links = resources.get("links", [])
    share_link = request.args.get("share_link")
    shared_file = request.args.get("shared_file")
    file_error = request.args.get("error")
    file_msg = request.args.get("msg")
    
    file_list = []
    if os.path.isdir(config.FILES_DIR):
        with os.scandir(config.FILES_DIR) as it:
            entries = sorted([e for e in it if not e.name.startswith(".")], key=lambda e: e.name)
            for entry in entries:
                if not entry.is_file():
                    continue
                if not is_allowed_resource_upload(entry.name):
                    continue
                size_mb = entry.stat().st_size / (1024 * 1024)
                meta = file_meta.get(entry.name, {})
                # Migration/Fallback if meta is string (old format)
                if isinstance(meta, str):
                    meta = {"desc": meta}
                
                file_list.append({
                    "name": entry.name,
                    "size_str": f"{size_mb:.2f} MB",
                    "title": meta.get("title", entry.name),
                    "description": meta.get("desc", "")
                })
    
    return render_template_string(
        FILES_TEMPLATE,
        files=file_list,
        links=links,
        share_link=share_link,
        shared_file=shared_file,
        file_error=file_error,
        file_msg=file_msg,
    )

@app.route("/files/upload", methods=["POST"])
@admin_required
def files_upload():
    if 'file' not in request.files:
        return redirect(url_for('index'))
    file = request.files['file']
    if file.filename == '':
        return redirect(url_for('index'))
    if file:
        filename = secure_filename(file.filename)
        if not is_allowed_resource_upload(filename):
            return redirect(url_for('index', error=gettext("This file type is not allowed for security reasons.")))
        file.save(os.path.join(config.FILES_DIR, filename))
        
        title = request.form.get("title", "").strip()
        desc = request.form.get("description", "").strip()

        res = load_resources()
        if "files" not in res:
            res["files"] = {}
        res["files"][filename] = {"title": title or filename, "desc": desc}
        save_resources(res)
        
    return redirect(url_for('index', msg=gettext("File was uploaded.")))

@app.route("/files/download/<path:filename>")
@admin_required
def files_download(filename):
    if not downloads_enabled():
        abort(403)
    if not resolve_resource_file_path(filename):
        abort(404)
    return send_from_directory(config.FILES_DIR, filename, as_attachment=True, conditional=True)

@app.route("/files/update", methods=["POST"])
@admin_required
def files_update():
    filename = request.form.get("filename")
    title = request.form.get("title", "").strip()
    desc = request.form.get("description", "").strip()
    if filename and resolve_resource_file_path(filename):
        res = load_resources()
        if "files" not in res:
            res["files"] = {}
        res["files"][filename] = {"title": title or filename, "desc": desc}
        save_resources(res)
    return redirect(url_for("index"))

@app.route("/files/delete/<filename>", methods=["POST"])
@admin_required
def files_delete(filename):
    success = False
    path = resolve_resource_file_path(filename)
    if path:
        try:
            os.remove(path)
            res = load_resources()
            if "files" in res and filename in res["files"]:
                del res["files"][filename]
                save_resources(res)
            # Remove shares
            remove_shares_for("_file_", filename, "file")
            success = True
        except Exception:
            pass
            
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return Response(json.dumps({"success": success}), 200, mimetype="application/json")
        
    return redirect(url_for("index"))

@app.route("/files/share/<filename>", methods=["POST"])
@admin_required
def files_share_create(filename):
    if not public_shares_enabled():
        abort(403)
    path = resolve_resource_file_path(filename)
    if not path:
        return redirect(url_for("index"))
    
    token = create_share("_file_", filename, "file")
    path_url = url_for("public_view", token=token)
    if PUBLIC_BASE:
        link = f"{PUBLIC_BASE.rstrip('/')}{path_url}"
    else:
        link = url_for("public_view", token=token, _external=True)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accept_mimetypes.accept_json:
        return Response(json.dumps({"link": link}), 200, mimetype="application/json")

    return redirect(url_for("index", share_link=link, shared_file=filename))

@app.route("/links/add", methods=["POST"])
@admin_required
def links_add():
    title = request.form.get("title", "").strip()
    url = request.form.get("url", "").strip()
    if title and url and is_allowed_url(url):
        res = load_resources()
        if "links" not in res:
            res["links"] = []
        res["links"].append({"title": title, "url": url})
        save_resources(res)
    return redirect(url_for("index"))

@app.route("/links/remove", methods=["POST"])
@admin_required
def links_remove():
    url = request.form.get("url")
    if url:
        res = load_resources()
        if "links" in res:
            res["links"] = [link for link in res["links"] if link.get("url") != url]
            save_resources(res)
    return redirect(url_for("index"))

@app.route("/api/stats")
@login_required
@limiter.limit("60 per minute")  # SECURITY: API rate limit
def api_stats():
    return Response(json.dumps(get_sys_stats()), 200, mimetype="application/json")


def _overlay_token_from_request(*preferred_headers):
    """Prefer overlay auth headers; query token remains OBS-compatible when allowed."""
    global _OVERLAY_QUERY_TOKEN_LOGGED
    g.overlay_query_token_used = False

    header_token = ""
    for name in preferred_headers:
        header_token = (request.headers.get(name) or "").strip()
        if header_token:
            return header_token

    body_token = ""
    if request.is_json:
        body = request.get_json(silent=True) or {}
        body_token = str(body.get("token") or "").strip()
    if body_token:
        return body_token

    query_token = (request.args.get("token") or "").strip()
    if not query_token:
        return ""
    if not OVERLAY_ALLOW_QUERY_TOKEN:
        return ""
    g.overlay_query_token_used = True
    if not _OVERLAY_QUERY_TOKEN_LOGGED:
        logging.info(
            "Overlay auth accepted query-string token (OBS compat); "
            "prefer X-TTS-Token / X-Alert-Token headers"
        )
        _OVERLAY_QUERY_TOKEN_LOGGED = True
    return query_token


def _tts_token_from_request():
    return _overlay_token_from_request("X-TTS-Token", "X-Alert-Token")


def _alert_token_from_request():
    return _overlay_token_from_request("X-Alert-Token", "X-TTS-Token")


@app.after_request
def _overlay_query_token_warning(response):
    if getattr(g, "overlay_query_token_used", False):
        response.headers["Warning"] = (
            '299 - "Overlay token passed via query string; '
            'prefer X-TTS-Token / X-Alert-Token header"'
        )
    return response


def _tts_streamer_from_request():
    name = (request.args.get("streamer") or "").strip()
    if not name and request.is_json:
        body = request.get_json(silent=True) or {}
        name = str(body.get("streamer") or "").strip()
    return name


@app.route("/tts/overlay")
@limiter.limit("120 per minute")
def tts_overlay():
    from core import tts_service

    if not tts_service.token_ok_for(_tts_token_from_request(), _tts_streamer_from_request()):
        abort(401)
    from core import config
    path = os.path.join(config.APP_DIR, "WEBUI", "static", "tts_overlay.html")
    return send_file(path, mimetype="text/html; charset=utf-8")


@app.route("/tts/api/next")
@limiter.limit("300 per minute")
def tts_api_next():
    from core import tts_service

    if not tts_service.token_ok_for(_tts_token_from_request(), _tts_streamer_from_request()):
        abort(401)
    streamer = _tts_streamer_from_request()
    item = tts_service.claim_next(streamer)
    if not item:
        return jsonify({"item": None})
    token = _tts_token_from_request()
    audio_url = (
        f"/tts/audio/{item.get('audio')}"
        f"?token={urllib.parse.quote(token)}&streamer={urllib.parse.quote(streamer)}"
    )
    return jsonify(
        {
            "item": {
                "id": item.get("id"),
                "user": item.get("user"),
                "text": item.get("text"),
                "voice": item.get("voice"),
                "audio_url": audio_url,
            }
        }
    )


@app.route("/tts/api/ack", methods=["POST", "GET"])
@limiter.limit("300 per minute")
def tts_api_ack():
    from core import tts_service

    if not tts_service.token_ok_for(_tts_token_from_request(), _tts_streamer_from_request()):
        abort(401)
    streamer = _tts_streamer_from_request()
    item_id = (request.args.get("id") or "").strip()
    if not item_id and request.is_json:
        body = request.get_json(silent=True) or {}
        item_id = str(body.get("id") or "").strip()
    if not item_id:
        abort(400)
    ok = tts_service.ack_item(streamer, item_id)
    return jsonify({"ok": ok})


@app.route("/tts/api/skipcheck")
@limiter.limit("300 per minute")
def tts_api_skipcheck():
    from core import tts_service

    if not tts_service.token_ok_for(_tts_token_from_request(), _tts_streamer_from_request()):
        abort(401)
    streamer = _tts_streamer_from_request()
    item_id = (request.args.get("id") or "").strip()
    if not item_id:
        abort(400)
    return jsonify({"skip": tts_service.should_skip_playing(streamer, item_id)})


@app.route("/tts/api/status")
@limiter.limit("60 per minute")
def tts_api_status():
    from core import tts_service

    if not tts_service.token_ok_for(_tts_token_from_request(), _tts_streamer_from_request()):
        abort(401)
    return jsonify(tts_service.queue_status(_tts_streamer_from_request()))


@app.route("/tts/audio/<path:filename>")
@limiter.limit("300 per minute")
def tts_audio(filename):
    from core import tts_service

    if not tts_service.token_ok_for(_tts_token_from_request(), _tts_streamer_from_request()):
        abort(401)
    safe = os.path.basename(filename)
    streamer = tts_service._norm(_tts_streamer_from_request())
    if streamer and not safe.startswith(streamer + "_"):
        abort(404)
    if not safe.endswith(".mp3") or ".." in safe or "/" in safe:
        abort(404)
    path = os.path.join(tts_service.tts_dir(), safe)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="audio/mpeg", conditional=True)


@app.route("/alerts/overlay")
@limiter.limit("120 per minute")
def alerts_overlay():
    from core import alert_sounds

    if not alert_sounds.token_ok(_alert_token_from_request()):
        abort(401)
    path = os.path.join(os.path.dirname(__file__), "static", "alerts_overlay.html")
    return send_file(path, mimetype="text/html; charset=utf-8")


@app.route("/alerts/api/next")
@limiter.limit("300 per minute")
def alerts_api_next():
    from core import alert_sounds

    if not alert_sounds.token_ok(_alert_token_from_request()):
        abort(401)
    item = alert_sounds.claim_next()
    if not item:
        return jsonify({"item": None})
    token = _alert_token_from_request()
    event = str(item.get("event") or "")
    audio_url = (
        f"/alerts/audio/{urllib.parse.quote(event)}.mp3"
        f"?token={urllib.parse.quote(token)}"
    )
    return jsonify(
        {
            "item": {
                "id": item.get("id"),
                "event": event,
                "label": item.get("label") or event,
                "audio_url": audio_url,
            }
        }
    )


@app.route("/alerts/api/ack", methods=["POST", "GET"])
@limiter.limit("300 per minute")
def alerts_api_ack():
    from core import alert_sounds

    if not alert_sounds.token_ok(_alert_token_from_request()):
        abort(401)
    item_id = (request.args.get("id") or "").strip()
    if not item_id and request.is_json:
        body = request.get_json(silent=True) or {}
        item_id = str(body.get("id") or "").strip()
    if not item_id:
        abort(400)
    return jsonify({"ok": alert_sounds.ack(item_id)})


@app.route("/alerts/audio/<path:filename>")
@limiter.limit("300 per minute")
def alerts_audio(filename):
    from core import alert_sounds

    if not alert_sounds.token_ok(_alert_token_from_request()):
        abort(401)
    safe = os.path.basename(filename)
    if not safe.endswith(".mp3") or ".." in safe:
        abort(404)
    stem = safe[:-4]
    if stem not in alert_sounds.KNOWN_EVENTS:
        abort(404)
    alert_sounds.ensure_sound_files()
    path = os.path.join(alert_sounds.sounds_dir(), safe)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="audio/mpeg", conditional=True)


@app.route("/healthz")
@limiter.exempt
def healthz():
    health = collect_health_status()
    is_admin = bool(session.get("logged_in") and session.get("user_role") == "admin")
    want_full = str(request.args.get("full") or "").strip().lower() in ("1", "true", "yes", "on")
    code = 200 if health["status"] == "ok" else 503
    # Full disk/service detail only for admin sessions (optionally via ?full=1).
    if is_admin and (want_full or session.get("logged_in")):
        return jsonify(health), code
    # Public summary stays lean; disk/task extras remain in admin/full payload.
    return jsonify({
        "status": health.get("status", "degraded"),
        "ts": health.get("ts"),
        "disk_used_pct": health.get("disk_used_pct"),
        "disk_free_gb": health.get("disk_free_gb"),
    }), code


@app.route("/metrics")
@limiter.exempt
def metrics():
    """Prometheus text metrics. Login required, or METRICS_TOKEN via X-Metrics-Token / Authorization Bearer."""
    token = METRICS_TOKEN
    provided = (
        request.headers.get("X-Metrics-Token")
        or ""
    ).strip()
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        provided = provided or auth[7:].strip()
    token_ok = bool(token) and provided and secrets.compare_digest(provided, token)
    logged_in = bool(session.get("logged_in") and not session_is_expired())
    if not token_ok and not logged_in:
        return abort(401)
    body = render_prometheus_metrics()
    return Response(body, mimetype="text/plain; version=0.0.4; charset=utf-8")


if TWITCH_EVENTSUB_ENABLED:
    @app.route("/api/twitch/eventsub", methods=["POST"])
    @limiter.exempt
    def twitch_eventsub_webhook():
        """Public Twitch EventSub callback (HMAC verified). Additive to GQL polling."""
        body = request.get_data(cache=False, as_text=False) or b""
        headers = {key: value for key, value in request.headers.items()}
        status, payload, content_type = twitch_eventsub.handle_webhook_payload(
            body=body,
            headers=headers,
        )
        if status == 204:
            return ("", 204)
        return Response(payload, status=status, mimetype=content_type)


@app.route('/stop_recording/<streamer>', methods=['POST'])
@admin_required
def stop_recording_route(streamer):
    archive = archive_name_for(streamer) or normalize_channel_name(streamer) or streamer
    stopped = database.stop_recording(archive)
    if stopped <= 0:
        stopped = database.stop_recording(streamer)
    flash(
        gettext("Recording stopped: %(count)s source(s) for %(archive)s disabled. "
        "The process will be terminated at the next check.", count=stopped, archive=archive),
        "success",
    )
    return redirect(url_for('streamer_view', streamer=archive))


@app.route("/channels/source_record/<channel>", methods=["POST"])
@admin_required
def source_record_toggle(channel):
    name = normalize_channel_name(channel) or str(channel or "").strip()
    if not name:
        flash(gettext("Invalid source."), "error")
        return redirect(url_for("index"))
    raw = str(request.form.get("enabled", "")).strip().lower()
    enabled = raw in ("1", "true", "yes", "on")
    ok = database.set_channel_record(name, enabled)
    archive = archive_name_for(name) or name
    mode_text = gettext("Recording enabled (full mode)") if enabled else gettext("Recording disabled (live & Discord only)")
    ref = request.referrer or ""
    if "/channels" in ref:
        return redirect(url_for("channels_view", channels_msg=f"{name}: {mode_text}."))
    if ok:
        flash(
            gettext("Recording enabled for source %(name)s.", name=name) if enabled else gettext("Recording disabled for source %(name)s.", name=name),
            "success",
        )
    else:
        flash(gettext("Source %(name)s not found.", name=name), "error")
    return redirect(url_for("streamer_view", streamer=archive))

def _direct_child_dir(parent, child_name):
    path = safe_join(parent, child_name)
    if not path or not os.path.isdir(path):
        return None
    parent_real = os.path.realpath(parent)
    path_real = os.path.realpath(path)
    if os.path.dirname(path_real) != parent_real:
        return None
    return path

def _pid_is_running(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False

def _folder_has_active_media(folder_path):
    try:
        for name in os.listdir(folder_path):
            if name.endswith(".part"):
                return True
    except OSError:
        return False
    return False

@app.route('/empty_folder/<streamer>', methods=['POST'])
@admin_required
def empty_folder_route(streamer):
    archive = archive_name_for(streamer) or normalize_channel_name(streamer) or streamer
    folder_path = _direct_child_dir(config.BASE_DIR, archive)
    if not folder_path:
        msg = gettext("Folder not found or invalid.")
        return redirect(url_for('index', channels_msg=msg))

    if _folder_has_active_media(folder_path) or current_recording_info(archive) is not None:
        return redirect(url_for(
            'streamer_view',
            streamer=archive,
            msg=gettext("The folder cannot be emptied while a recording is being written."),
        ))

    extra_paths = []
    for root in (config.THUMB_ROOT, config.HLS_ROOT):
        extra = safe_join(root, archive)
        if extra and os.path.exists(extra):
            extra_paths.append(extra)

    database.add_task('empty_folder', {
        'path': folder_path,
        'streamer': archive,
        'extra_paths': extra_paths,
    })

    _VOD_LIST_CACHE.pop(archive, None)
    _VOD_LIST_CACHE.pop(streamer, None)
    STREAMER_CACHE["ts"] = 0
    return redirect(url_for('streamer_view', streamer=archive, msg=gettext("Folder is being emptied in the background.")))

@app.route('/delete_folder/<streamer>', methods=['POST'])
@admin_required
def delete_folder_route(streamer):
    archive = archive_name_for(streamer) or normalize_channel_name(streamer) or streamer
    if config.is_reserved_archive_name(archive):
        logging.error("Refusing to delete reserved directory %r (requested for %r)", archive, streamer)
        abort(400)
    # Sicherheitscheck: Pfad muss im Basis-Verzeichnis liegen
    folder_path = safe_join(config.BASE_DIR, archive)
    # Strenge Prüfung: Muss ein Verzeichnis sein und direkt unter BASE_DIR liegen
    if folder_path and os.path.dirname(os.path.abspath(folder_path)) == os.path.abspath(config.BASE_DIR):
        # Alle Quellen entfernen, die denselben Archiv-Ordner nutzen
        conn = database.get_db()
        try:
            rows = conn.execute(
                "SELECT name, archive_name FROM channels WHERE name=? OR archive_name=? OR name=?",
                (archive, archive, streamer),
            ).fetchall()
            for row in rows:
                name = row["name"]
                row_archive = str(row["archive_name"] or name).strip() or name
                if name == archive or name == streamer or row_archive == archive:
                    conn.execute("DELETE FROM channels WHERE name=?", (name,))
            conn.commit()
        finally:
            conn.close()
        
        # Löschen passiert im Hintergrund durch den Worker
        database.add_task('delete_folder', {
            'path': folder_path,
            'streamer': archive,
        })
        
        # VOD-Cache für diesen Streamer invalidieren
        _VOD_LIST_CACHE.pop(archive, None)
        _VOD_LIST_CACHE.pop(streamer, None)
            
        # Dashboard-Cache aktualisieren: Streamer sofort aus der Liste entfernen
        STREAMER_CACHE["data"] = [s for s in STREAMER_CACHE.get("data", []) if s["name"] != archive and s["name"] != streamer]
        STREAMER_CACHE["ts"] = 0
        try:
            invalidate_dashboard_media_cache()
        except Exception:
            pass
        
        # Keine Meldung anzeigen, da Löschen im Hintergrund passiert
        return redirect(url_for('index'))
    else:
        # Channel already removed from disk but still listed via Drive index —
        # still enqueue a purge by archive name.
        archive = archive_name_for(streamer) or normalize_channel_name(streamer) or streamer
        if archive and archive not in {".", ".."} and "/" not in archive and "\\" not in archive:
            conn = database.get_db()
            try:
                conn.execute(
                    "DELETE FROM channels WHERE name=? OR archive_name=? OR name=?",
                    (archive, archive, streamer),
                )
                conn.commit()
            finally:
                conn.close()
            database.add_task("delete_folder", {
                "path": os.path.join(config.BASE_DIR, archive),
                "streamer": archive,
            })
            _VOD_LIST_CACHE.pop(archive, None)
            STREAMER_CACHE["data"] = [
                s for s in STREAMER_CACHE.get("data", [])
                if s["name"] != archive and s["name"] != streamer
            ]
            STREAMER_CACHE["ts"] = 0
            return redirect(url_for("index"))
        msg = gettext("Folder not found or invalid.")
        return redirect(url_for('index', channels_msg=msg))

# Ensure baseline users exist (runs on import for Gunicorn). The standalone
# transcription helper imports this module in a child process and must not
# mutate users or start background sync threads.
_IMPORT_ONLY = os.environ.get("STREAMVAULT_IMPORT_ONLY", "").lower() in (
    "1",
    "true",
    "yes",
)
if not _IMPORT_ONLY:
    try:
        ensure_admin_user()
    except Exception:
        logging.exception("Bootstrap: Admin-Benutzer konnte nicht sichergestellt werden")

    # Keep a full local Twitch badge library (~a few MB) for 1:1 chat icons.
    try:
        maybe_start_twitch_badge_library_sync()
    except Exception:
        logging.exception("Could not start Twitch badge library sync")



@app.route("/banner/<streamer>")
@login_required
def streamer_banner_file(streamer):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    found = find_local_streamer_banner_file(streamer)
    if not found:
        abort(404)
    bdir, bname = found
    resp = send_from_directory(bdir, bname)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/streamer/<streamer>/banner/update", methods=["POST"])
@admin_required
def streamer_banner_update(streamer):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    archive_key = archive_name_for(streamer) or streamer
    clean_slug = secure_filename(archive_key) or secure_filename(streamer)
    action = request.form.get("action", "save").strip().lower()
    banners_dir = os.path.join(config.META_DIR, "banners")
    os.makedirs(banners_dir, exist_ok=True)
    exts = [".jpg", ".jpeg", ".png", ".webp", ".gif"]
    profiles = load_profiles()
    entry = dict(profiles.get(archive_key) or profiles.get(streamer) or {})

    if action == "reset":
        entry.pop("custom_banner_url", None)
        for ext in exts:
            for s in [clean_slug, secure_filename(streamer)]:
                if not s:
                    continue
                target = os.path.join(banners_dir, f"{s}{ext}")
                if os.path.isfile(target):
                    try:
                        os.remove(target)
                    except Exception:
                        pass
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("streamer_view", streamer=streamer, msg=gettext("Banner reset to default.")))

    file = request.files.get("banner_file")
    if file and file.filename:
        filename = secure_filename(file.filename)
        _, ext = os.path.splitext(filename)
        ext = ext.lower()
        if ext not in exts:
            return redirect(url_for("streamer_view", streamer=streamer, error=gettext("Invalid image format (allowed: JPG, PNG, WEBP, GIF).")))
        for e in exts:
            target = os.path.join(banners_dir, f"{clean_slug}{e}")
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except Exception:
                    pass
        target_path = os.path.join(banners_dir, f"{clean_slug}{ext}")
        file.save(target_path)
        entry.pop("custom_banner_url", None)
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("streamer_view", streamer=streamer, msg=gettext("Banner image uploaded successfully.")))

    custom_url = request.form.get("custom_banner_url", "").strip()
    if custom_url:
        entry["custom_banner_url"] = custom_url
        for e in exts:
            target = os.path.join(banners_dir, f"{clean_slug}{e}")
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except Exception:
                    pass
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("streamer_view", streamer=streamer, msg=gettext("Custom banner URL saved successfully.")))
    elif "custom_banner_url" in request.form:
        entry.pop("custom_banner_url", None)
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("streamer_view", streamer=streamer, msg=gettext("Custom banner removed.")))

    return redirect(url_for("streamer_view", streamer=streamer))


@app.route("/admin/banner/update", methods=["POST"])
@admin_required
def admin_banner_update():
    streamer = request.form.get("streamer", "").strip()
    if not streamer:
        return redirect(url_for("admin_view", tab="banners", error=gettext("Streamer / folder missing.")))
    archive_key = archive_name_for(streamer) or streamer
    clean_slug = secure_filename(archive_key) or secure_filename(streamer)
    action = request.form.get("action", "save").strip().lower()
    banners_dir = os.path.join(config.META_DIR, "banners")
    os.makedirs(banners_dir, exist_ok=True)
    exts = [".jpg", ".jpeg", ".png", ".webp", ".gif"]
    profiles = load_profiles()
    entry = dict(profiles.get(archive_key) or profiles.get(streamer) or {})

    if action == "reset":
        entry.pop("custom_banner_url", None)
        for ext in exts:
            for s in [clean_slug, secure_filename(streamer)]:
                if not s:
                    continue
                target = os.path.join(banners_dir, f"{s}{ext}")
                if os.path.isfile(target):
                    try:
                        os.remove(target)
                    except Exception:
                        pass
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("admin_view", tab="banners", msg=gettext("Banner for %(streamer)s reset to default.", streamer=streamer)))

    file = request.files.get("banner_file")
    if file and file.filename:
        filename = secure_filename(file.filename)
        _, ext = os.path.splitext(filename)
        ext = ext.lower()
        if ext not in exts:
            return redirect(url_for("admin_view", tab="banners", error=gettext("Invalid image format (allowed: JPG, PNG, WEBP, GIF).")))
        for e in exts:
            target = os.path.join(banners_dir, f"{clean_slug}{e}")
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except Exception:
                    pass
        target_path = os.path.join(banners_dir, f"{clean_slug}{ext}")
        file.save(target_path)
        entry.pop("custom_banner_url", None)
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("admin_view", tab="banners", msg=gettext("Banner image for %(streamer)s uploaded successfully.", streamer=streamer)))

    custom_url = request.form.get("custom_banner_url", "").strip()
    if custom_url:
        entry["custom_banner_url"] = custom_url
        for e in exts:
            target = os.path.join(banners_dir, f"{clean_slug}{e}")
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except Exception:
                    pass
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("admin_view", tab="banners", msg=gettext("Banner URL for %(streamer)s saved.", streamer=streamer)))
    elif "custom_banner_url" in request.form:
        entry.pop("custom_banner_url", None)
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("admin_view", tab="banners", msg=gettext("Custom banner for %(streamer)s removed.", streamer=streamer)))

    return redirect(url_for("admin_view", tab="banners"))


@app.route("/admin/backups/download/<filename>")
@admin_required
def admin_backup_download(filename):
    clean = secure_filename(filename)
    if not clean.startswith("streamrecorder_") or not clean.endswith(".tar.gz"):
        abort(400)
    backups_dir = system_backups_dir()
    local_path = os.path.join(backups_dir, clean)
    if os.path.isfile(local_path):
        return send_from_directory(backups_dir, clean, as_attachment=True)
    abort(404)


@app.route("/admin/backups/create", methods=["POST"])
@admin_required
def admin_backup_create():
    # Operator-provided script that writes streamrecorder_*.tar.gz into BACKUP_DIR.
    script_path = os.environ.get("VOD_BACKUP_SCRIPT", "").strip()
    if not script_path or not os.path.isfile(script_path):
        return redirect(url_for(
            "admin_view", tab="backups",
            error=gettext("No backup script configured (VOD_BACKUP_SCRIPT)."),
        ))
    try:
        res = subprocess.run(["bash", script_path], capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            bust_media_cache()
            return redirect(url_for("admin_view", tab="backups", msg=gettext("System backup created successfully.")))
        else:
            logging.error("Backup script failed: %s", res.stderr)
            return redirect(url_for("admin_view", tab="backups", error=gettext("Backup error: %(error)s", error=res.stderr[:100])))
    except Exception as exc:
        logging.error("Backup execution failed: %s", exc)
        return redirect(url_for("admin_view", tab="backups", error=gettext("Backup error: %(error)s", error=str(exc)[:100])))


@app.route("/admin/backups/delete/<filename>", methods=["POST"])
@admin_required
def admin_backup_delete(filename):
    clean = secure_filename(filename)
    if not clean.startswith("streamrecorder_") or not clean.endswith(".tar.gz"):
        abort(400)
    local_path = os.path.join(system_backups_dir(), clean)
    deleted = False
    if os.path.isfile(local_path):
        try:
            os.remove(local_path)
            deleted = True
        except OSError as exc:
            logging.error("Could not delete backup %s: %s", local_path, exc)
    if deleted:
        bust_media_cache()
        return redirect(url_for("admin_view", tab="backups", msg=gettext("Backup %(name)s deleted.", name=clean)))
    return redirect(url_for("admin_view", tab="backups", error=gettext("Backup %(name)s could not be deleted.", name=clean)))


@app.route("/avatar/<streamer>")
@login_required
def streamer_avatar_file(streamer):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    found = find_local_streamer_avatar_file(streamer)
    if not found:
        abort(404)
    adir, aname = found
    resp = send_from_directory(adir, aname)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/streamer/<streamer>/avatar/update", methods=["POST"])
@admin_required
def streamer_avatar_update(streamer):
    abort_vod_denied()
    abort_hidden_streamer(streamer)
    archive_key = archive_name_for(streamer) or streamer
    clean_slug = secure_filename(archive_key) or secure_filename(streamer)
    action = request.form.get("action", "save").strip().lower()
    avatars_dir = os.path.join(config.META_DIR, "avatars")
    os.makedirs(avatars_dir, exist_ok=True)
    exts = [".jpg", ".jpeg", ".png", ".webp", ".gif"]
    profiles = load_profiles()
    entry = dict(profiles.get(archive_key) or profiles.get(streamer) or {})

    if action == "reset":
        entry.pop("custom_avatar_url", None)
        for ext in exts:
            for s in [clean_slug, secure_filename(streamer)]:
                if not s:
                    continue
                target = os.path.join(avatars_dir, f"{s}{ext}")
                if os.path.isfile(target):
                    try:
                        os.remove(target)
                    except Exception:
                        pass
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("streamer_view", streamer=streamer, msg=gettext("Profile picture reset to default.")))

    file = request.files.get("avatar_file")
    if file and file.filename:
        filename = secure_filename(file.filename)
        _, ext = os.path.splitext(filename)
        ext = ext.lower()
        if ext not in exts:
            return redirect(url_for("streamer_view", streamer=streamer, error=gettext("Invalid image format (allowed: JPG, PNG, WEBP, GIF).")))
        for e in exts:
            target = os.path.join(avatars_dir, f"{clean_slug}{e}")
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except Exception:
                    pass
        target_path = os.path.join(avatars_dir, f"{clean_slug}{ext}")
        file.save(target_path)
        entry.pop("custom_avatar_url", None)
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("streamer_view", streamer=streamer, msg=gettext("Profile picture uploaded successfully.")))

    custom_url = request.form.get("custom_avatar_url", "").strip()
    if custom_url:
        entry["custom_avatar_url"] = custom_url
        for e in exts:
            target = os.path.join(avatars_dir, f"{clean_slug}{e}")
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except Exception:
                    pass
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("streamer_view", streamer=streamer, msg=gettext("Custom profile picture URL saved successfully.")))
    elif "custom_avatar_url" in request.form:
        entry.pop("custom_avatar_url", None)
        profiles[archive_key] = entry
        save_profiles(profiles)
        bust_media_cache()
        return redirect(url_for("streamer_view", streamer=streamer, msg=gettext("Custom profile picture removed.")))

    return redirect(url_for("streamer_view", streamer=streamer))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="StreamVault (Open Core)")
    parser.add_argument("--host", default=HOST, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=PORT, help="Bind port (default: 8080)")
    args = parser.parse_args()

    if config.SECRET_KEY == "change-me":
        logging.warning("WARN: Setze VOD_SECRET_KEY fuer Sessions.")
    
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
