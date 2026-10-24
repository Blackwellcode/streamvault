import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env_file(path):
    """Load KEY=VALUE lines into os.environ without logging values."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                # Do not override an already-set process environment.
                if key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        return
    except OSError:
        return


_load_env_file(os.path.join(APP_DIR, ".env"))
_load_env_file("/etc/streamrecorder/env")
if os.environ.get("ENV_FILE"):
    _load_env_file(os.environ.get("ENV_FILE"))


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


DEMO_MODE = _env_flag("DEMO_MODE", False)


def _normalize_twitch_oauth_token(token):
    token = str(token or "").strip()
    if token.lower().startswith("oauth:"):
        token = token.split(":", 1)[1].strip()
    return token


def streamlink_twitch_config_args(token, config_dir=None):
    """Return Streamlink CLI args that load Twitch OAuth from a private config file.

    The token must never appear in argv/ps; only the config path is visible.
    When no token is set, return ``--no-config`` so system configs cannot leak auth.
    """
    token = _normalize_twitch_oauth_token(token)
    if not token:
        return ["--no-config"]
    root = config_dir or os.environ.get("VOD_META_DIR") or os.path.join(
        os.environ.get("VOD_BASE_DIR", APP_DIR), ".meta"
    )
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
    except OSError:
        root = APP_DIR
    path = os.path.join(root, "streamlink-twitch-api-header.cfg")
    content = f"twitch-api-header=Authorization=OAuth {token}\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        # Last resort: keep streams working, but prefer config-file path above.
        return ["--no-config", "--twitch-api-header", f"Authorization=OAuth {token}"]
    return ["--config", path]


def streamlink_twitch_auth_args(token):
    """Compatibility wrapper: Streamlink auth without putting the token in argv."""
    return streamlink_twitch_config_args(token)


# Captured at import (tests restore os.environ after load). Used only for
# Streamlink HLS access tokens — chat stays anonymous justinfan, so this
# account does not appear in the streamer's chatter/viewer list.
RECORDER_TWITCH_OAUTH_TOKEN = _normalize_twitch_oauth_token(
    os.environ.get("STREAM_RECORDER_TWITCH_OAUTH", "")
)
if _env_flag("LIVE_TWITCH_ANONYMOUS"):
    LIVE_TWITCH_OAUTH_TOKEN = ""
else:
    LIVE_TWITCH_OAUTH_TOKEN = _normalize_twitch_oauth_token(
        os.environ.get("LIVE_TWITCH_OAUTH")
        or os.environ.get("STREAM_RECORDER_TWITCH_OAUTH")
        or ""
    )
LIVE_TWITCH_OAUTH_ENABLED = bool(LIVE_TWITCH_OAUTH_TOKEN)
# Mobile Live player is temporarily off until the mobile HLS/watch path is fixed.
# Re-enable with MOBILE_LIVE_ENABLED=1 in /etc/streamrecorder/env.
MOBILE_LIVE_ENABLED = _env_flag("MOBILE_LIVE_ENABLED", True)

_default_base = os.path.join(APP_DIR, "data") if os.path.isdir(os.path.join(APP_DIR, "data")) else APP_DIR
BASE_DIR = os.environ.get("VOD_BASE_DIR", _default_base)
DB_FILE = os.path.join(BASE_DIR, "streamrecorder.db")

FILES_DIR = os.environ.get("VOD_FILES_DIR", os.path.join(BASE_DIR, "files"))
HLS_ROOT = os.environ.get("VOD_HLS_DIR", os.path.join(BASE_DIR, ".hls"))
THUMB_ROOT = os.environ.get("VOD_THUMB_DIR", os.path.join(BASE_DIR, ".thumbs"))
META_DIR = os.environ.get("VOD_META_DIR", os.path.join(BASE_DIR, ".meta"))

if os.path.isdir("/dev/shm"):
    DEFAULT_LIVE_HLS_ROOT = os.path.join("/dev/shm", "streamrecorder_hls")
else:
    DEFAULT_LIVE_HLS_ROOT = os.path.join(BASE_DIR, ".hls_live")
LIVE_HLS_ROOT = os.environ.get("LIVE_HLS_DIR", DEFAULT_LIVE_HLS_ROOT)

SECRET_KEY = os.environ.get("VOD_SECRET_KEY", "change-me")

# SECURITY: Require secure credentials - no defaults allowed in production
VOD_USER = os.environ.get("VOD_USER")
VOD_PASS = os.environ.get("VOD_PASS")
VOD_ALLOW_ENV_LOGIN = str(os.environ.get("VOD_ALLOW_ENV_LOGIN", "")).strip().lower() in (
    "1", "true", "yes", "on",
)

if DEMO_MODE:
    if not VOD_USER:
        VOD_USER = "demoadmin"
    if not VOD_PASS:
        VOD_PASS = "demopassword123"
    if SECRET_KEY == "change-me":
        SECRET_KEY = "demo-secret-key-12345"
else:
    if not VOD_USER or not VOD_PASS:
        raise RuntimeError(
            "CRITICAL: VOD_USER and VOD_PASS environment variables are REQUIRED and must be set to secure values. "
            "Default credentials are disabled for security. "
            "Set them in your environment before starting the application."
        )

    if VOD_USER.lower() == "admin" or VOD_PASS == "admin":
        raise RuntimeError(
            "SECURITY ERROR: VOD_USER cannot be 'admin' and VOD_PASS cannot be 'admin'. "
            "Please set secure, unique credentials."
        )

LEGACY_CHANNELS_FILE = os.path.join(BASE_DIR, ".channels.json")
LEGACY_LIVE_CHANNEL_FILE = os.path.join(BASE_DIR, ".live_channels.json")
LEGACY_USERS_FILE = os.path.join(META_DIR, "users.json")
LEGACY_BANS_FILE = os.path.join(META_DIR, "bans.json")

# Google Drive archive (opt-in). Secrets live in /etc/streamrecorder, never in the UI.
GDRIVE_ENABLED = _env_flag("GDRIVE_ENABLED", False)
GDRIVE_ROOT_NAME = (os.environ.get("GDRIVE_ROOT_NAME", "streamvault.archive") or "streamvault.archive").strip()
GDRIVE_ROOT_FOLDER_ID = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "").strip()
GDRIVE_SERVICE_ACCOUNT_FILE = os.environ.get(
    "GDRIVE_SERVICE_ACCOUNT_FILE",
    "/etc/streamrecorder/gdrive-service-account.json",
).strip()
GDRIVE_OAUTH_TOKEN_FILE = os.environ.get(
    "GDRIVE_OAUTH_TOKEN_FILE",
    "/etc/streamrecorder/gdrive-token.json",
).strip()
GDRIVE_OAUTH_CLIENT_FILE = os.environ.get(
    "GDRIVE_OAUTH_CLIENT_FILE",
    "/etc/streamrecorder/gdrive-oauth-client.json",
).strip()
GDRIVE_SHARED_DRIVE_ID = os.environ.get("GDRIVE_SHARED_DRIVE_ID", "").strip()
try:
    GDRIVE_SYNC_INTERVAL_SEC = max(60, int(os.environ.get("GDRIVE_SYNC_INTERVAL_SEC", "300")))
except ValueError:
    GDRIVE_SYNC_INTERVAL_SEC = 300
try:
    GDRIVE_UPLOAD_BATCH = max(1, int(os.environ.get("GDRIVE_UPLOAD_BATCH", "4")))
except ValueError:
    GDRIVE_UPLOAD_BATCH = 4
try:
    GDRIVE_DURATION_BACKFILL_BATCH = max(0, int(os.environ.get("GDRIVE_DURATION_BACKFILL_BATCH", "40")))
except ValueError:
    GDRIVE_DURATION_BACKFILL_BATCH = 40


def _parse_folder_map(raw):
    """Parse GDRIVE_FOLDER_MAP like 'beatek_:Beatek,foo:Bar' → {slug: Title}."""
    mapping = {}
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        src, dst = part.split(":", 1)
        src = "".join(ch for ch in src.strip().lower() if ch.isalnum() or ch == "_")
        dst = dst.strip()
        if src and dst:
            mapping[src] = dst
    return mapping


# Local archive slug → Drive folder display name (e.g. channel_a uploads into "Channel A").
GDRIVE_FOLDER_MAP = _parse_folder_map(os.environ.get("GDRIVE_FOLDER_MAP", ""))
# After a successful Drive upload (size+MD5 verified), remove the local VOD (and chat).
# KEEP_LOCAL_DAYS postpones that delete so recent VODs play from disk (no Drive quota).
# Delete is also blocked until .vodcache holds a complete copy (seed/hydrate).
GDRIVE_DELETE_LOCAL_AFTER_UPLOAD = _env_flag("GDRIVE_DELETE_LOCAL_AFTER_UPLOAD", False)
GDRIVE_DELETE_LOCAL_CHAT = _env_flag("GDRIVE_DELETE_LOCAL_CHAT", True)
try:
    GDRIVE_KEEP_LOCAL_DAYS = max(
        0, min(90, int(os.environ.get("GDRIVE_KEEP_LOCAL_DAYS", "14")))
    )
except ValueError:
    GDRIVE_KEEP_LOCAL_DAYS = 14
# Full-file Drive→cache hydrate for newest pins. Prefer local keep + seed; use 1 job.
GDRIVE_PLAYBACK_HYDRATE = _env_flag("GDRIVE_PLAYBACK_HYDRATE", False)
# Resume incomplete .chunks for pinned VODs even when full hydrate is off.
GDRIVE_PLAYBACK_HYDRATE_RESUME = _env_flag("GDRIVE_PLAYBACK_HYDRATE_RESUME", True)
# Shared Drive downloadQuotaExceeded circuit (seconds). Too short → Play retries extend the ban.
try:
    GDRIVE_PLAYBACK_QUOTA_COOLDOWN_SEC = max(
        300,
        min(86400, int(os.environ.get("GDRIVE_PLAYBACK_QUOTA_COOLDOWN_SEC", "10800"))),
    )
except ValueError:
    GDRIVE_PLAYBACK_QUOTA_COOLDOWN_SEC = 10800
try:
    # Closed byte ranges avoid Google's downloadQuotaExceeded on full/open-ended fetches.
    # 64 MiB is ~60-90s of typical VOD bitrate so the player is not waiting on Drive
    # every few seconds.
    GDRIVE_PLAYBACK_CHUNK_BYTES = max(
        256 * 1024,
        int(os.environ.get("GDRIVE_PLAYBACK_CHUNK_BYTES", str(64 * 1024 * 1024))),
    )
except ValueError:
    GDRIVE_PLAYBACK_CHUNK_BYTES = 64 * 1024 * 1024
try:
    GDRIVE_PLAYBACK_READAHEAD_BYTES = max(
        GDRIVE_PLAYBACK_CHUNK_BYTES,
        int(os.environ.get("GDRIVE_PLAYBACK_READAHEAD_BYTES", str(128 * 1024 * 1024))),
    )
except ValueError:
    GDRIVE_PLAYBACK_READAHEAD_BYTES = 128 * 1024 * 1024
# Browser-facing live Ranges stay small so deep seeks start streaming immediately
# instead of blocking on a full 64 MiB hydrate chunk (paused clock, no playback).
try:
    GDRIVE_PLAYBACK_LIVE_RANGE_BYTES = max(
        256 * 1024,
        min(
            16 * 1024 * 1024,
            int(os.environ.get("GDRIVE_PLAYBACK_LIVE_RANGE_BYTES", str(8 * 1024 * 1024))),
        ),
    )
except ValueError:
    GDRIVE_PLAYBACK_LIVE_RANGE_BYTES = 8 * 1024 * 1024
GDRIVE_PLAYBACK_CACHE = _env_flag("GDRIVE_PLAYBACK_CACHE", True)
GDRIVE_PLAYBACK_CACHE_DIR = (
    os.environ.get("GDRIVE_PLAYBACK_CACHE_DIR") or os.path.join(BASE_DIR, ".vodcache")
).strip()
try:
    GDRIVE_PLAYBACK_CACHE_MAX_BYTES = max(
        512 * 1024 * 1024,
        int(os.environ.get("GDRIVE_PLAYBACK_CACHE_MAX_BYTES", str(256 * 1024 * 1024 * 1024))),
    )
except ValueError:
    GDRIVE_PLAYBACK_CACHE_MAX_BYTES = 256 * 1024 * 1024 * 1024
try:
    GDRIVE_PLAYBACK_CACHE_JOBS = max(1, min(6, int(os.environ.get("GDRIVE_PLAYBACK_CACHE_JOBS", "6"))))
except ValueError:
    GDRIVE_PLAYBACK_CACHE_JOBS = 6
# Keep VODs newer than this many days fully cached (pinned against eviction).
try:
    GDRIVE_PLAYBACK_PIN_NEWEST_DAYS = max(
        0, min(90, int(os.environ.get("GDRIVE_PLAYBACK_PIN_NEWEST_DAYS", "7")))
    )
except ValueError:
    GDRIVE_PLAYBACK_PIN_NEWEST_DAYS = 7
try:
    VOD_MAX_CONCURRENT_WATCHERS = max(0, int(os.environ.get("VOD_MAX_CONCURRENT_WATCHERS", "20")))
except ValueError:
    VOD_MAX_CONCURRENT_WATCHERS = 20
try:
    VOD_SLOT_STALE_SEC = max(30, int(os.environ.get("VOD_SLOT_STALE_SEC", "90")))
except ValueError:
    VOD_SLOT_STALE_SEC = 90
try:
    VOD_SLOT_PAUSE_GRACE_SEC = max(30, int(os.environ.get("VOD_SLOT_PAUSE_GRACE_SEC", "240")))
except ValueError:
    VOD_SLOT_PAUSE_GRACE_SEC = 240
try:
    VOD_SLOT_HEARTBEAT_SEC = max(5, int(os.environ.get("VOD_SLOT_HEARTBEAT_SEC", "15")))
except ValueError:
    VOD_SLOT_HEARTBEAT_SEC = 15
# Bootstrap only: used when META_DIR/maintenance.json does not exist yet.
# Prefer the Admin → Wartungsmodus toggle (runtime state) over this env flag.
MAINTENANCE_MODE = _env_flag("MAINTENANCE_MODE", False)
# Daily metadata/system backup upload into Drive root (see scripts/backup_streamrecorder.sh).
GDRIVE_SYSTEM_BACKUP = _env_flag("GDRIVE_SYSTEM_BACKUP", True)
GDRIVE_SYSTEM_BACKUP_FOLDER = (
    os.environ.get("GDRIVE_SYSTEM_BACKUP_FOLDER", "System-Backups") or "System-Backups"
).strip()

DISCORD_ERROR_WEBHOOK_URL = os.environ.get("DISCORD_ERROR_WEBHOOK_URL", "").strip()

_public_base = os.environ.get("VOD_PUBLIC_BASE", "http://localhost:5000").strip().rstrip("/")
DISCORD_ERROR_WEBHOOK_AVATAR = os.environ.get(
    "DISCORD_ERROR_WEBHOOK_AVATAR", 
    f"{_public_base}/static/error_logger_avatar.jpg"
).strip()

def setup_discord_logging():
    import logging
    try:
        from discord_bots.discord_notify import DiscordLogHandler
        handler = DiscordLogHandler()
        handler.setLevel(logging.ERROR)
        logging.getLogger().addHandler(handler)
    except Exception as e:
        print(f"Failed to setup Discord logging: {e}")

SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
    from sentry_sdk.integrations.threading import ThreadingIntegration
    
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration(), ThreadingIntegration(propagate_hub=True)],
        traces_sample_rate=1.0,
        send_default_pii=True
    )
