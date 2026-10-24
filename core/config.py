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
_load_env_file("/etc/streamvault/env")
_load_env_file("/etc/streamrecorder/env")
if os.environ.get("ENV_FILE"):
    _load_env_file(os.environ.get("ENV_FILE"))


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


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
        return ["--no-config", "--twitch-api-header", f"Authorization=OAuth {token}"]
    return ["--config", path]


def streamlink_twitch_auth_args(token):
    """Compatibility wrapper: Streamlink auth without putting the token in argv."""
    return streamlink_twitch_config_args(token)


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
MOBILE_LIVE_ENABLED = _env_flag("MOBILE_LIVE_ENABLED", True)

_default_base = os.path.join(APP_DIR, "data") if os.path.isdir(os.path.join(APP_DIR, "data")) else APP_DIR
BASE_DIR = os.environ.get("VOD_BASE_DIR", _default_base)
DB_FILE = os.path.join(BASE_DIR, "streamrecorder.db")

FILES_DIR = os.environ.get("VOD_FILES_DIR", os.path.join(BASE_DIR, "files"))
HLS_ROOT = os.environ.get("VOD_HLS_DIR", os.path.join(BASE_DIR, ".hls"))
THUMB_ROOT = os.environ.get("VOD_THUMB_DIR", os.path.join(BASE_DIR, ".thumbs"))
META_DIR = os.environ.get("VOD_META_DIR", os.path.join(BASE_DIR, ".meta"))

if os.path.isdir("/dev/shm"):
    DEFAULT_LIVE_HLS_ROOT = os.path.join("/dev/shm", "streamvault_hls")
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

MAINTENANCE_MODE = _env_flag("MAINTENANCE_MODE", False)
DISCORD_ERROR_WEBHOOK_URL = os.environ.get("DISCORD_ERROR_WEBHOOK_URL", "").strip()

SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.threading import ThreadingIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[FlaskIntegration(), ThreadingIntegration(propagate_hub=True)],
            traces_sample_rate=1.0,
            send_default_pii=True,
        )
    except ImportError:
        pass
