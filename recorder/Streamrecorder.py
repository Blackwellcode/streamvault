#!/usr/bin/env python3

import atexit
import fcntl
import json
import logging
import os
import random
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from logging.handlers import RotatingFileHandler
from multiprocessing import Process

from recorder import chat_clip
from recorder import chat_events
from core import config
from core import database
from core import twitch_api
from core import twitch_eventsub
from core.mp4_utils import (
    BROWSER_MP4_MOVFLAGS,
    finalize_if_fragmented,
    is_archive_mp4_filename,
)

# Setup logging
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("STREAMRECORDER_LOG_DIR", SCRIPT_DIR)
os.makedirs(LOG_DIR, exist_ok=True)
if True:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            RotatingFileHandler(os.path.join(LOG_DIR, 'restream.log'), maxBytes=10*1024*1024, backupCount=5),
            logging.StreamHandler()
        ],
        force=True
    )
logger = logging.getLogger(__name__)
LOG_THROTTLE = {}

# Konfiguration
RECORDINGS_BASE_DIR = (
    os.environ.get("RECORDINGS_BASE_DIR")
    or os.environ.get("VOD_BASE_DIR")
    or config.BASE_DIR
)
os.makedirs(RECORDINGS_BASE_DIR, exist_ok=True)
RETRY_DELAY = 30
HEARTBEAT_INTERVAL = 60
# Poll interval for live-only monitors (no recording, notifications only).
CHECK_INTERVAL = int(os.environ.get('STREAM_RECORDER_CHECK_INTERVAL', '30'))
TEMP_OUTPUT_SUFFIX = ".part"
MIN_RECORD_FILE_SIZE = 1024
SALVAGE_MIN_BYTES = int(os.environ.get('STREAM_RECORDER_SALVAGE_MIN_BYTES', str(5 * 1024 * 1024)))
# Prefer STREAM_RECORDER_STORAGE_PAUSE_*; fall back to worker STORAGE_ALERT_* names.
STORAGE_PAUSE_PCT = float(
    os.environ.get(
        'STREAM_RECORDER_STORAGE_PAUSE_PCT',
        os.environ.get('STORAGE_ALERT_THRESHOLD_PCT', '90'),
    )
)
STORAGE_PAUSE_FREE_GB = float(
    os.environ.get(
        'STREAM_RECORDER_STORAGE_PAUSE_FREE_GB',
        os.environ.get('STORAGE_ALERT_THRESHOLD_FREE_GB', '20'),
    )
)
STORAGE_PAUSE_FILE = os.path.join(config.META_DIR, 'storage_pause.json')
MAX_CONCURRENT_RECORDINGS = int(os.environ.get('STREAM_RECORDER_MAX_CONCURRENT', '12'))
STALE_RECORDING_AGE = 60 * 60
INSTANCE_LOCK_FILE = os.path.join(RECORDINGS_BASE_DIR, ".streamrecorder.pid")
RECORDING_TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"
LEGACY_RECORDING_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

# Bild-Konfiguration
MAX_CHAT_MESSAGES = 15

# Stream-Qualität (bevorzugt 720p für kleinere Dateien bei guter Qualität)
STREAM_QUALITY = ["720p", "720p60", "480p", "best", "worst"]
STREAMLINK_BIN = os.environ.get(
    'STREAMLINK_BIN',
    os.path.join(os.path.dirname(sys.executable), 'streamlink'),
)
YTDLP_BIN = os.environ.get(
    'YTDLP_BIN',
    os.path.join(os.path.dirname(sys.executable), 'yt-dlp'),
)
STREAMLINK_RETRY_OPEN = int(os.environ.get('STREAM_RECORDER_STREAMLINK_RETRY_OPEN', '5'))
STREAMLINK_TIMEOUT = int(os.environ.get('STREAM_RECORDER_STREAMLINK_TIMEOUT', '360'))
STREAMLINK_PROBE_TIMEOUT = int(os.environ.get('STREAM_RECORDER_STREAMLINK_PROBE_TIMEOUT', '25'))
STREAMLINK_PROBE_RETRY_OPEN = int(os.environ.get('STREAM_RECORDER_STREAMLINK_PROBE_RETRY_OPEN', '2'))
STREAMLINK_TWITCH_OAUTH = config.RECORDER_TWITCH_OAUTH_TOKEN
YTDLP_PROBE_TIMEOUT = int(os.environ.get('STREAM_RECORDER_YTDLP_PROBE_TIMEOUT', '45'))
YTDLP_STREAM_TIMEOUT = int(os.environ.get('STREAM_RECORDER_YTDLP_STREAM_TIMEOUT', '30'))
YTDLP_CHAT_IDLE_TIMEOUT = int(os.environ.get('STREAM_RECORDER_YTDLP_CHAT_IDLE_TIMEOUT', '90'))
FFMPEG_OUTPUT_TIMEOUT = int(os.environ.get('STREAM_RECORDER_FFMPEG_OUTPUT_TIMEOUT', '90'))
# Bound the final wait after stdout EOF; a wedged FFmpeg must not hang the channel forever.
FFMPEG_FINALIZE_TIMEOUT = int(os.environ.get('STREAM_RECORDER_FFMPEG_FINALIZE_TIMEOUT', '30'))
# Cheap live-poll while the channel is actually offline. Do not exponential-backoff
# this to minutes — that was the missing VOD-start gap (up to 10 min).
STREAM_OFFLINE_RETRY_BASE = int(os.environ.get('STREAM_RECORDER_OFFLINE_RETRY_BASE', '5'))
STREAM_OFFLINE_RETRY_MAX = int(os.environ.get('STREAM_RECORDER_OFFLINE_RETRY_MAX', '12'))
STREAM_LIVE_UNPLAYABLE_RETRY_SEC = int(os.environ.get('STREAM_RECORDER_LIVE_UNPLAYABLE_RETRY_SEC', '3'))
STREAM_POST_SUCCESS_RETRY_SEC = int(os.environ.get('STREAM_RECORDER_POST_SUCCESS_RETRY_SEC', '1'))
# After a recording ends or a start fails, keep trying even if GQL still says
# offline. Twitch restarts often have a 1–3 min API/HLS gap.
STREAM_RESTART_GRACE_SEC = int(os.environ.get('STREAM_RECORDER_RESTART_GRACE_SEC', '180'))
# GQL/HLS can still look live for a few seconds after a real stream end.
# Wait before starting a new file so leftover playlists do not become ghost VODs.
STREAM_SAME_SESSION_CONFIRM_SEC = int(os.environ.get('STREAM_RECORDER_SAME_SESSION_CONFIRM_SEC', '15'))
# Discord LIVE ping only after ffmpeg has actually muxed this much media.
LIVE_NOTIFY_MIN_MEDIA_SEC = float(os.environ.get('STREAM_RECORDER_LIVE_NOTIFY_MIN_MEDIA_SEC', '20'))
# Skip Discord VOD / remux for leftover HLS snippets after stream end.
MIN_VOD_NOTIFY_SEC = int(os.environ.get('STREAM_RECORDER_MIN_VOD_NOTIFY_SEC', '60'))
SKIP_PROBE_WHEN_LIVE = os.environ.get('STREAM_RECORDER_SKIP_PROBE_WHEN_LIVE', 'true').lower() in (
    'true', '1', 'yes',
)
STREAM_FAILURE_RETRY_MAX = int(os.environ.get('STREAM_RECORDER_FAILURE_RETRY_MAX', '300'))

# Encoding-Performance (Preset beeinflusst primär Bitrate/CPU, nicht die CRF-Qualität)
ENC_PRESET = os.environ.get('STREAM_RECORDER_PRESET', 'veryfast')
ENC_CRF = os.environ.get('STREAM_RECORDER_CRF', '26')

# Live HLS-Konfiguration
# Single source of truth shared with the WebUI, which serves these segments.
LIVE_HLS_ROOT = config.LIVE_HLS_ROOT
LIVE_HLS_TIME = int(os.environ.get('LIVE_HLS_TIME', '4'))
LIVE_HLS_LIST_SIZE = int(os.environ.get('LIVE_HLS_LIST_SIZE', '40'))
LIVE_HLS_TTL_SEC = int(os.environ.get('LIVE_HLS_TTL_SEC', '900'))
CHAT_LOG_SUBDIR = os.environ.get(
    'STREAM_RECORDER_CHAT_SUBDIR',
    os.environ.get('VOD_CHAT_SUBDIR', 'chat_logs')
)

# Debug-Modus
DEBUG = os.environ.get('STREAM_RECORDER_DEBUG', 'false').lower() in ('true', '1', 'yes')
if DEBUG:
    logger.setLevel(logging.DEBUG)

STREAMLINK_NO_SSL_VERIFY = os.environ.get('STREAMLINK_NO_SSL_VERIFY', 'false').lower() in ('true', '1', 'yes')

# Hardware-Encode (VAAPI)
USE_VAAPI = os.environ.get('STREAM_RECORDER_VAAPI', 'false').lower() in ('true', '1', 'yes')

CPU_COUNT = os.cpu_count() or 4
FFMPEG_THREADS = os.environ.get('STREAM_RECORDER_THREADS', str(min(8, CPU_COUNT)))

# Post-processing for A/V stability
AUTO_REMUX = os.environ.get('STREAM_RECORDER_AUTO_REMUX', 'true').lower() in ('true', '1', 'yes')
# Sync remux in the recorder process is opt-in; default is enqueue worker remux_vod.
SYNC_REMUX = os.environ.get('STREAM_RECORDER_SYNC_REMUX', 'false').lower() in ('true', '1', 'yes')
REENCODE_AUDIO = os.environ.get('STREAM_RECORDER_REENCODE_AUDIO', 'false').lower() in ('true', '1', 'yes')
REMUX_TIMEOUT = int(os.environ.get('STREAM_RECORDER_REMUX_TIMEOUT', '600'))
AUTO_TRANSCRIBE = os.environ.get('STREAM_RECORDER_AUTO_TRANSCRIBE', 'true').lower() in ('true', '1', 'yes')
THUMB_LOCK_STALE_SEC = int(os.environ.get('THUMB_LOCK_STALE_SEC', '3600'))
# Stream copy (no transcode) when possible; fall back to software encode on failure.
COPY_MODE = os.environ.get('STREAM_RECORDER_COPY_MODE', 'true').lower() in ('true', '1', 'yes')
FORCE_1080 = os.environ.get('STREAM_RECORDER_FORCE_1080', 'false').lower() in ('true', '1', 'yes')

# Twitch GQL Client ID (Web)
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
YOUTUBE_STREAM_QUALITY = os.environ.get(
    'STREAM_RECORDER_YOUTUBE_QUALITY',
    # Prefer muxed HLS for pipe→ffmpeg. DASH (live-from-start) splits A/V and breaks stdout.
    'best[height<=720][protocol^=m3u8]/best[height<=1080][protocol^=m3u8]/best[height<=720]/best[height<=1080]/best',
)
YOUTUBE_LIVE_FROM_START = os.environ.get('STREAM_RECORDER_YOUTUBE_LIVE_FROM_START', 'false').lower() in ('true', '1', 'yes')

def find_ffmpeg():
    """Findet den Pfad zu FFmpeg."""
    possible_paths = ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/snap/bin/ffmpeg"]
    for path in possible_paths:
        if os.path.isfile(path):
            return path
    try:
        result = subprocess.run(['which', 'ffmpeg'], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    raise FileNotFoundError("FFmpeg nicht gefunden. Installiere es mit 'sudo apt install ffmpeg' oder 'sudo snap install ffmpeg'.")

def find_streamlink():
    """Findet die Streamlink-CLI, die HLS-Werbesegmente filtert."""
    candidates = [
        STREAMLINK_BIN,
        os.path.join(os.path.dirname(sys.executable), "streamlink"),
        shutil.which("streamlink"),
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise FileNotFoundError("Streamlink nicht gefunden. Installiere es im aktiven Python-Venv.")

def find_ytdlp():
    """Findet die yt-dlp-CLI fuer YouTube-Livestreams."""
    candidates = [
        YTDLP_BIN,
        os.path.join(os.path.dirname(sys.executable), "yt-dlp"),
        shutil.which("yt-dlp"),
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise FileNotFoundError("yt-dlp nicht gefunden. Installiere es im aktiven Python-Venv.")

def ytdlp_js_runtime_args():
    """yt-dlp enables only Deno by default; StreamVault has Node (/usr/bin/node), not Deno."""
    runtime = os.environ.get("STREAM_RECORDER_YTDLP_JS_RUNTIME", "").strip()
    if runtime.lower() in ("none", "off", "0"):
        return []
    if not runtime:
        node = "/usr/bin/node" if os.path.isfile("/usr/bin/node") else "node"
        runtime = f"node:{node}" if node.startswith("/") else "node"
    return ["--js-runtimes", runtime]

def find_font(font_name):
    """Findet eine Schriftart oder gibt None zurück."""
    possible_dirs = ["/usr/share/fonts", "/usr/local/share/fonts", "/usr/share/fonts/truetype"]
    for directory in possible_dirs:
        if os.path.exists(directory):
            for root, _, files in os.walk(directory):
                for file in files:
                    if font_name.lower() in file.lower() and file.lower().endswith('.ttf'):
                        return os.path.join(root, file)
    # Fallback zu DejaVu Sans (meist verfügbar auf Linux)
    dejavu_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if os.path.exists(dejavu_path):
        logger.warning(f"Schriftart {font_name} nicht gefunden. Verwende DejaVu Sans.")
        return dejavu_path
    logger.warning(f"Schriftart {font_name} nicht gefunden. Verwende Standard-Schriftart.")
    return None

def normalize_channel(name):
    if not name:
        return None
    channel = str(name).strip().lower()
    channel = channel.removeprefix("@")
    channel = re.sub(r"[^a-z0-9_]", "", channel)
    return channel or None

def normalize_platform(value):
    platform = str(value or "twitch").strip().lower()
    if platform in ("youtube", "yt", "youtu.be"):
        return "youtube"
    return "twitch"

def get_source_config(source_channel):
    try:
        conf = database.get_channel_config(source_channel)
    except Exception:
        conf = None
    conf = conf or {"name": source_channel}
    conf["name"] = conf.get("name") or source_channel
    conf["platform"] = normalize_platform(conf.get("platform"))
    conf["source_url"] = str(conf.get("source_url") or "").strip()
    archive_name = normalize_channel(conf.get("archive_name")) or normalize_channel(source_channel)
    if not archive_name:
        safe_slug = re.sub(r"[^a-zA-Z0-9_]", "_", str(source_channel or "unknown")).strip("_")
        archive_name = safe_slug or "unknown_channel"
    conf["archive_name"] = archive_name
    return conf

def is_youtube_host(hostname):
    host = str(hostname or "").lower()
    return host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")

def youtube_live_url(source_channel, conf=None):
    conf = conf or get_source_config(source_channel)
    raw_url = str(conf.get("source_url") or source_channel or "").strip()
    if not raw_url:
        raw_url = str(source_channel or "").strip()
    if raw_url.startswith("@"):
        raw_url = f"https://www.youtube.com/{raw_url}"
    elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw_url):
        raw_url = f"https://www.youtube.com/@{raw_url.lstrip('@')}"

    parsed = urllib.parse.urlsplit(raw_url)
    if not parsed.scheme:
        parsed = urllib.parse.urlsplit(f"https://{raw_url}")
    path = parsed.path.rstrip("/") or "/"
    query = parsed.query
    if is_youtube_host(parsed.hostname):
        channel_like = (
            path.startswith("/@")
            or path.startswith("/channel/")
            or path.startswith("/c/")
            or path.startswith("/user/")
        )
        if channel_like and not path.endswith("/live"):
            path = f"{path}/live"
            query = ""
    return urllib.parse.urlunsplit((parsed.scheme or "https", parsed.netloc, path, query, ""))

def twitch_login_from_source(source_channel, conf=None):
    """Resolve the Twitch login used for GQL/EventSub from channel config."""
    conf = conf or get_source_config(source_channel)
    twitch_login = str(source_channel or "").strip()
    raw_url = str(conf.get("source_url") or "").strip()
    if raw_url and "twitch.tv" in raw_url.lower():
        path = urllib.parse.urlsplit(raw_url).path.strip("/")
        if path:
            twitch_login = path.split("/")[0]
    return normalize_channel(twitch_login) or twitch_login.lower()


def twitch_stream_url(source_channel, conf=None):
    conf = conf or get_source_config(source_channel)
    raw_url = str(conf.get("source_url") or "").strip()
    if raw_url and "twitch.tv" in raw_url.lower():
        return raw_url
    return f"https://twitch.tv/{source_channel}"

def source_public_url(source_channel, conf=None):
    conf = conf or get_source_config(source_channel)
    if conf.get("source_url"):
        return conf["source_url"]
    if conf.get("platform") == "youtube":
        return youtube_live_url(source_channel, conf)
    return twitch_stream_url(source_channel, conf)

def atomic_write(path, content, is_json=False):
    """Schreibt Daten atomar in eine Datei (verhindert Lesefehler durch andere Prozesse)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if is_json:
                json.dump(content, f, ensure_ascii=False, indent=2)
            else:
                f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception as e:
        logger.error(f"Fehler beim Schreiben von {path}: {e}")
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

def throttled_log(level, key, message, interval=300):
    now = time.time()
    last = LOG_THROTTLE.get(key, 0)
    if now - last < interval:
        return
    LOG_THROTTLE[key] = now
    # Bound throttle map — keys are channel-scoped and can accumulate over weeks.
    if len(LOG_THROTTLE) > 400:
        cutoff = now - 3600
        for stale_key, ts in list(LOG_THROTTLE.items()):
            if ts < cutoff:
                LOG_THROTTLE.pop(stale_key, None)
        if len(LOG_THROTTLE) > 400:
            keep = sorted(LOG_THROTTLE.items(), key=lambda item: item[1], reverse=True)[:200]
            LOG_THROTTLE.clear()
            LOG_THROTTLE.update(keep)
    getattr(logger, level)(message)

def load_channels():
    return database.get_channels(mode='record')

def load_all_channels():
    return database.get_channels(mode='all')

def load_live_channels():
    return database.get_channels(mode='live')

def build_filter_str(font_path, chat_text_file, viewer_text_file, use_vaapi):
    # VOD overlay komplett entfernt: nur skalieren (default max 720, no upscale pad)
    if FORCE_1080:
        filter_str = (
            "[0:v]"
            "scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
        )
        if use_vaapi:
            filter_str += ",format=nv12,hwupload[v]"
        else:
            filter_str += "[v]"
        return filter_str
    if use_vaapi:
        return "[0:v]scale=-2:'min(720,ih)',format=nv12,hwupload[v]"
    return "[0:v]scale=-2:'min(720,ih)',format=yuv420p[v]"


def _storage_pause_flag_active():
    """True when worker wrote .meta/storage_pause.json with paused/critical."""
    try:
        with open(STORAGE_PAUSE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return False
        return bool(data.get("paused") or data.get("critical"))
    except FileNotFoundError:
        return False
    except Exception:
        return False


def disk_blocks_recording(path):
    """True when free space is critically low — pause starting new recordings."""
    if _storage_pause_flag_active():
        return True
    target = path or RECORDINGS_BASE_DIR
    try:
        check_path = os.path.abspath(str(target))
        while check_path and not os.path.exists(check_path):
            parent = os.path.dirname(check_path)
            if parent == check_path:
                break
            check_path = parent
        if not check_path or not os.path.exists(check_path):
            check_path = (
                RECORDINGS_BASE_DIR
                if os.path.exists(RECORDINGS_BASE_DIR)
                else os.path.dirname(os.path.abspath(RECORDINGS_BASE_DIR))
            )
        usage = shutil.disk_usage(check_path)
        if usage.total <= 0:
            return False
        used_pct = (float(usage.used) / float(usage.total)) * 100.0
        free_gb = float(usage.free) / (1024.0 ** 3)
        if used_pct >= STORAGE_PAUSE_PCT:
            return True
        if free_gb <= STORAGE_PAUSE_FREE_GB:
            return True
        return False
    except Exception as exc:
        logger.warning(
            "Fehler bei der Speicherplatzprüfung für %s: %s", target, exc
        )
        return False


def enqueue_remux_vod(file_path, reencode_audio=None, force=True):
    """Enqueue worker remux_vod for a finalized recording (preferred over sync remux)."""
    if reencode_audio is None:
        reencode_audio = REENCODE_AUDIO
    path = os.path.abspath(str(file_path or ""))
    if not path or not os.path.isfile(path):
        return False
    filename = os.path.basename(path)
    streamer = os.path.basename(os.path.dirname(path))
    if not streamer or not filename:
        return False
    payload = {
        "streamer": streamer,
        "filename": filename,
        "reencode_audio": bool(reencode_audio),
        "force": bool(force),
    }
    if filename.lower().endswith(".mkv"):
        payload["output_filename"] = os.path.splitext(filename)[0] + ".mp4"
    try:
        database.add_task("remux_vod", payload)
        logger.info(f"Remux-Task eingeordnet: {path}")
        return True
    except Exception as exc:
        logger.error(f"Konnte Remux-Task nicht einreihen: {exc}")
        return False


def salvage_temp_recording(temp_path, output_file, reason="", source_channel=None):
    """Keep large incomplete .part files instead of silently deleting them."""
    if not temp_path or not os.path.exists(temp_path):
        return False
    try:
        size = os.path.getsize(temp_path)
    except OSError:
        return False

    reason_txt = f" ({reason})" if reason else ""

    if size < MIN_RECORD_FILE_SIZE:
        try:
            os.remove(temp_path)
            logger.info(f"Tiny temp recording deleted{reason_txt}: {temp_path}")
        except OSError:
            pass
        return False

    if size < SALVAGE_MIN_BYTES:
        try:
            os.remove(temp_path)
            logger.info(
                f"Temp recording too small to salvage ({size} bytes){reason_txt}: {temp_path}"
            )
        except OSError:
            pass
        return False

    final_path = None
    try:
        if output_file and not os.path.exists(output_file):
            os.replace(temp_path, output_file)
            final_path = output_file
        else:
            raise OSError("output missing or already exists")
    except Exception:
        candidates = []
        if output_file:
            candidates.append(f"{output_file}.salvage.mp4")
        candidates.append(f"{temp_path}.salvage")
        for dest in candidates:
            try:
                if os.path.exists(dest):
                    continue
                os.replace(temp_path, dest)
                final_path = dest
                break
            except Exception:
                continue
        if final_path is None:
            # NEVER silently delete large .part files
            final_path = temp_path
            logger.error(
                f"Could not move salvaged recording; keeping in place{reason_txt}: {temp_path}"
            )

    recordings_dir = os.path.dirname(output_file or temp_path)
    src = source_channel or os.path.basename(recordings_dir)
    try:
        save_recording_status(
            recordings_dir,
            {
                "state": "incomplete",
                "message": f"Salvaged incomplete recording{reason_txt}",
                "filename": os.path.basename(final_path),
                "size": size,
                "finished_at": time.time(),
                "source": src,
            },
            source_channel=src,
        )
        clear_current_recording(recordings_dir, filename=os.path.basename(output_file or temp_path), source_channel=src)
    except Exception:
        pass

    try:
        enqueue_remux_vod(final_path)
    except Exception as exc:
        logger.warning(f"Could not enqueue remux for salvage: {exc}")

    logger.warning(
        f"Salvaged incomplete recording ({size} bytes){reason_txt}: {final_path}"
    )
    return True


def unique_recording_output_path(recordings_dir, archive_channel, platform, timestamp, ext=".mp4"):
    tag = platform_file_tag(platform)
    if not str(ext).startswith("."):
        ext = f".{ext}"
    base = f"{archive_channel}_{tag}_{timestamp}{ext}"
    candidate = os.path.join(recordings_dir, base)
    if not os.path.exists(candidate) and not os.path.exists(f"{candidate}{TEMP_OUTPUT_SUFFIX}"):
        return candidate
    for index in range(2, 50):
        alt = os.path.join(recordings_dir, f"{archive_channel}_{tag}_{timestamp}_{index}{ext}")
        if not os.path.exists(alt) and not os.path.exists(f"{alt}{TEMP_OUTPUT_SUFFIX}"):
            return alt
    return os.path.join(recordings_dir, f"{archive_channel}_{tag}_{timestamp}_{os.getpid()}{ext}")


def overlay_chat_path(recordings_dir, source_channel=None):
    suffix = source_state_suffix(source_channel)
    if suffix:
        return os.path.join(recordings_dir, f"chat_overlay.{suffix}.txt")
    return os.path.join(recordings_dir, "chat_overlay.txt")


def overlay_viewer_path(recordings_dir, source_channel=None):
    suffix = source_state_suffix(source_channel)
    if suffix:
        return os.path.join(recordings_dir, f"viewer_overlay.{suffix}.txt")
    return os.path.join(recordings_dir, "viewer_overlay.txt")


def live_chat_log_path(recordings_dir, source_channel=None):
    suffix = source_state_suffix(source_channel)
    if suffix:
        return os.path.join(recordings_dir, f".live_chat.{suffix}.jsonl")
    return os.path.join(recordings_dir, ".live_chat.jsonl")


def platform_file_tag(platform):
    return "yt" if normalize_platform(platform) == "youtube" else "tw"


def get_chat_text_file(channel):
    conf = get_source_config(channel)
    archive_channel = conf.get("archive_name") or channel
    recordings_dir = os.path.join(RECORDINGS_BASE_DIR, archive_channel)
    os.makedirs(recordings_dir, exist_ok=True)
    return overlay_chat_path(recordings_dir, channel)

def get_viewer_text_file(channel):
    conf = get_source_config(channel)
    archive_channel = conf.get("archive_name") or channel
    recordings_dir = os.path.join(RECORDINGS_BASE_DIR, archive_channel)
    os.makedirs(recordings_dir, exist_ok=True)
    return overlay_viewer_path(recordings_dir, channel)

def thumbnail_paths_for_recording(channel, media_path):
    channel = normalize_channel(channel)
    filename = os.path.basename(str(media_path or ""))
    if not channel or not is_archive_mp4_filename(filename):
        return None, None, None
    base = os.path.splitext(filename)[0]
    thumb_dir = os.path.join(config.THUMB_ROOT, channel)
    thumb_path = os.path.join(thumb_dir, f"{base}.jpg")
    lock_path = os.path.join(thumb_dir, f"{base}.lock")
    return thumb_dir, thumb_path, lock_path

def thumbnail_file_ready(thumb_path):
    try:
        return os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0
    except OSError:
        return False

def enqueue_thumbnail_task(media_path, channel, discord_notify_ready=False):
    _ = discord_notify_ready
    if not media_path or not os.path.isfile(media_path):
        return False
    if not is_archive_mp4_filename(media_path):
        return False
    try:
        if os.path.getsize(media_path) <= 0:
            return False
    except OSError:
        return False

    thumb_dir, thumb_path, lock_path = thumbnail_paths_for_recording(channel, media_path)
    if not thumb_dir or not thumb_path or not lock_path:
        return False
    os.makedirs(thumb_dir, exist_ok=True)
    filename = os.path.basename(media_path)
    streamer = normalize_channel(channel) or channel

    if thumbnail_file_ready(thumb_path):
        return False

    if os.path.exists(lock_path):
        try:
            if time.time() - os.path.getmtime(lock_path) <= THUMB_LOCK_STALE_SEC:
                return False
            os.remove(lock_path)
        except OSError:
            return False

    try:
        with open(lock_path, "w", encoding="utf-8") as handle:
            handle.write(f"{time.time()} {os.getpid()} {media_path}\n")
        payload = {
            "input_path": media_path,
            "thumb_path": thumb_path,
            "streamer": streamer,
            "filename": filename,
        }
        database.add_task("thumbnail", payload)
        logger.info(f"Thumbnail-Task eingereiht: {media_path}")
        return True
    except Exception as exc:
        logger.error(f"Konnte Thumbnail-Task nicht einreihen fuer {media_path}: {exc}")
        try:
            os.remove(lock_path)
        except OSError:
            pass
        return False


def maybe_enqueue_storage_upload(media_path):
    """Queue a cloud upload after recording finalisation when enabled."""
    try:
        from core.storage import service as storage_service

        settings = storage_service.get_settings()
        if not settings.get("auto_upload"):
            return False
        if not media_path or not os.path.isfile(media_path):
            return False
        obj = storage_service.enqueue_upload(media_path, settings=settings)
        if obj.get("task_id"):
            logger.info(
                "Cloud-Upload eingereiht: %s -> %s (Task %s)",
                obj.get("local_path"),
                obj.get("remote_path"),
                obj.get("task_id"),
            )
            return True
        return False
    except Exception as exc:
        from core.storage.base import mask_error

        logger.warning(
            "Cloud-Upload konnte nicht eingeordnet werden: %s", mask_error(exc)
        )
        return False


def notify_discord_streamer_online(archive_channel, source_channel, recording_metadata, conf, recording_started_ts):
    _ = (archive_channel, source_channel, recording_metadata, conf, recording_started_ts)


def clear_discord_live_session(archive_channel):
    _ = archive_channel

def source_state_suffix(source_channel):
    safe = normalize_channel(source_channel)
    return safe or None

def current_recording_path(recordings_dir, source_channel=None):
    suffix = source_state_suffix(source_channel)
    if suffix:
        return os.path.join(recordings_dir, f".current_recording.{suffix}.json")
    return os.path.join(recordings_dir, ".current_recording.json")

def recording_status_path(recordings_dir, source_channel=None):
    suffix = source_state_suffix(source_channel)
    if suffix:
        return os.path.join(recordings_dir, f".recording_status.{suffix}.json")
    return os.path.join(recordings_dir, ".recording_status.json")

def recording_timeline_path(recordings_dir, source_channel=None):
    suffix = source_state_suffix(source_channel)
    if suffix:
        return os.path.join(recordings_dir, f".recording_timeline.{suffix}.json")
    return os.path.join(recordings_dir, ".recording_timeline.json")

def iter_current_recording_paths(recordings_dir):
    if not recordings_dir or not os.path.isdir(recordings_dir):
        return []
    paths = []
    try:
        for name in os.listdir(recordings_dir):
            if name == ".current_recording.json" or (
                name.startswith(".current_recording.") and name.endswith(".json")
            ):
                paths.append(os.path.join(recordings_dir, name))
    except OSError:
        return []
    return sorted(paths)

def list_current_recordings(recordings_dir):
    items = []
    for path in iter_current_recording_paths(recordings_dir):
        data = load_json_file(path, None)
        if not isinstance(data, dict):
            continue
        if not data.get("start_ts") or not data.get("filename"):
            continue
        items.append(data)
    return items

def read_recording_media_time(current_info):
    timeline_file = current_info.get("timeline_file") if current_info else None
    if not timeline_file:
        return None
    try:
        with open(timeline_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        media_time = float(data.get("media_time"))
        if media_time < 0:
            return None
        return media_time
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None

def parse_ffmpeg_media_time(line):
    if not line.startswith("out_time_us="):
        return None
    try:
        return max(0.0, int(line.split("=", 1)[1]) / 1_000_000.0)
    except (TypeError, ValueError):
        return None

def load_current_recording(recordings_dir, source_channel=None):
    path = current_recording_path(recordings_dir, source_channel)
    # Never fall back to the unsuffixed legacy pointer for a different source —
    # that would cross-contaminate shared-archive metadata.
    if source_channel and not os.path.isfile(path):
        return None
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if not data.get("start_ts") or not data.get("filename"):
            return None
        if source_channel:
            info_source = str(data.get("source") or "").strip()
            if info_source and info_source != source_channel:
                return None
        return data
    except Exception:
        return None

def save_current_recording(recordings_dir, info, source_channel=None):
    payload = dict(info or {})
    if source_channel and not payload.get("source"):
        payload["source"] = source_channel
    # Always write per-source state when a source is known. Keep the unsuffixed
    # pointer only as a compatibility mirror for that same source.
    if source_channel:
        atomic_write(current_recording_path(recordings_dir, source_channel), payload, is_json=True)
        legacy = load_json_file(current_recording_path(recordings_dir), {})
        legacy_source = str((legacy or {}).get("source") or "").strip()
        if not legacy or not legacy_source or legacy_source == source_channel:
            atomic_write(current_recording_path(recordings_dir), payload, is_json=True)
        return
    atomic_write(current_recording_path(recordings_dir), payload, is_json=True)

def save_recording_status(recordings_dir, info, source_channel=None):
    source = source_channel or str((info or {}).get("source") or "").strip() or None
    path = recording_status_path(recordings_dir, source)
    payload = {"updated_ts": time.time(), **(info or {})}
    if source and not payload.get("source"):
        payload["source"] = source
    atomic_write(path, payload, is_json=True)
    # Compatibility mirror for tools that still read the unsuffixed status file.
    legacy = load_json_file(recording_status_path(recordings_dir), {})
    legacy_source = str((legacy or {}).get("source") or "").strip()
    if not legacy or not legacy_source or (source and legacy_source == source):
        atomic_write(recording_status_path(recordings_dir), payload, is_json=True)

def load_json_file(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return default

def parse_positive_int(value, default=0):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return default

def merge_viewer_sample(metadata, status, now=None):
    updated = dict(metadata or {})
    viewers = parse_positive_int((status or {}).get("viewers"))
    previous_peak = max(
        parse_positive_int(updated.get("viewers")),
        parse_positive_int(updated.get("max_viewers")),
    )
    peak = max(previous_peak, viewers)
    samples = parse_positive_int(updated.get("viewer_samples")) + 1
    total = parse_positive_int(updated.get("viewer_total")) + viewers

    updated.update({
        "viewers": peak,
        "max_viewers": peak,
        "last_viewers": viewers,
        "viewer_samples": samples,
        "viewer_total": total,
        "average_viewers": round(total / samples) if samples else viewers,
        "viewer_updated_at": float(now if now is not None else time.time()),
    })
    return updated

def update_recording_viewer_metadata(recordings_dir, status, source_channel=None):
    current = load_current_recording(recordings_dir, source_channel)
    if not current:
        return False
    filename = os.path.basename(str(current.get("filename") or ""))
    if not filename:
        return False

    now = time.time()
    changed = False
    merged_current = merge_viewer_sample(current, status, now=now)
    new_title = str((status or {}).get("title") or "").strip()
    new_cat = str((status or {}).get("category") or "").strip()
    if new_title and (not merged_current.get("title") or merged_current.get("title") != new_title):
        merged_current["title"] = new_title
        changed = True
    if new_cat and (not merged_current.get("category") or merged_current.get("category") != new_cat):
        merged_current["category"] = new_cat
        changed = True

    if merged_current != current or changed:
        save_current_recording(recordings_dir, merged_current, source_channel=source_channel)
        changed = True

    media_path = os.path.join(recordings_dir, filename)
    info_path = f"{media_path}.info.json"
    sidecar = load_json_file(info_path, {})
    if sidecar is not None:
        merged_sidecar = merge_viewer_sample(sidecar, status, now=now)
        if new_title and (not sidecar.get("title") or sidecar.get("title") != new_title):
            merged_sidecar["title"] = new_title
        if new_cat and (not sidecar.get("category") or sidecar.get("category") != new_cat):
            merged_sidecar["category"] = new_cat
        if merged_sidecar != sidecar:
            atomic_write(info_path, merged_sidecar, is_json=True)
            changed = True

    status_path = recording_status_path(recordings_dir, source_channel)
    recording_status = load_json_file(status_path, {})
    if recording_status:
        merged_status = merge_viewer_sample(recording_status, status, now=now)
        if new_title and (not recording_status.get("title") or recording_status.get("title") != new_title):
            merged_status["title"] = new_title
        if new_cat and (not recording_status.get("category") or recording_status.get("category") != new_cat):
            merged_status["category"] = new_cat
        merged_status["updated_ts"] = now
        if merged_status != recording_status:
            save_recording_status(recordings_dir, merged_status, source_channel=source_channel)
            changed = True

    return changed

def clear_current_recording(recordings_dir, filename=None, source_channel=None):
    # Only touch this source's state file. Optionally clear the unsuffixed
    # compatibility pointer when it still belongs to the same source/file.
    paths = [current_recording_path(recordings_dir, source_channel)]
    if source_channel:
        legacy_path = current_recording_path(recordings_dir)
        legacy = load_json_file(legacy_path, {})
        legacy_source = str((legacy or {}).get("source") or "").strip()
        legacy_name = str((legacy or {}).get("filename") or "").strip()
        if legacy and (
            (legacy_source and legacy_source == source_channel)
            or (filename and legacy_name == filename and not legacy_source)
        ):
            if not filename or not legacy_name or legacy_name == filename:
                paths.append(legacy_path)
    seen = set()
    for path in paths:
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        info = load_json_file(path, {})
        if filename and info and info.get("filename") != filename:
            continue
        if source_channel and info:
            info_source = str(info.get("source") or "").strip()
            if info_source and info_source != source_channel:
                continue
        timeline_file = info.get("timeline_file") if info else None
        try:
            os.remove(path)
        except Exception:
            pass
        if timeline_file:
            try:
                os.remove(timeline_file)
            except OSError:
                pass

def is_process_running(pid):
    try:
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True

_INSTANCE_LOCK_FD = None

def acquire_instance_lock():
    global _INSTANCE_LOCK_FD
    try:
        os.makedirs(os.path.dirname(INSTANCE_LOCK_FILE) or ".", exist_ok=True)
        fd = os.open(INSTANCE_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            logger.error("Eine andere Streamrecorder-Instanz hält bereits den Instance-Lock. Beende diese Instanz.")
            sys.exit(1)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.fsync(fd)
        _INSTANCE_LOCK_FD = fd
    except SystemExit:
        raise
    except Exception as exc:
        logger.warning("Konnte Instance-Lock nicht exklusiv setzen (%s). Fallback auf PID-Datei.", exc)
        try:
            if os.path.exists(INSTANCE_LOCK_FILE):
                with open(INSTANCE_LOCK_FILE, "r", encoding="utf-8") as handle:
                    content = handle.read().strip()
                if content:
                    old_pid = int(content)
                    if old_pid != os.getpid() and is_process_running(old_pid):
                        logger.error(
                            "Eine andere Streamrecorder-Instanz läuft bereits (pid=%s). Beende diese Instanz.",
                            old_pid,
                        )
                        sys.exit(1)
            with open(INSTANCE_LOCK_FILE, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
        except SystemExit:
            raise
        except Exception:
            logger.warning("Konnte Lock-Datei nicht schreiben. Es kann zu doppelten Recorder-Instanzen kommen.")
    atexit.register(release_instance_lock)

def drop_inherited_instance_lock():
    """Child processes must not keep the parent's flock fd (blocks recorder restart)."""
    global _INSTANCE_LOCK_FD
    if _INSTANCE_LOCK_FD is None:
        return
    try:
        os.close(_INSTANCE_LOCK_FD)
    except Exception:
        pass
    _INSTANCE_LOCK_FD = None

def release_instance_lock():
    global _INSTANCE_LOCK_FD
    try:
        if _INSTANCE_LOCK_FD is not None:
            try:
                fcntl.flock(_INSTANCE_LOCK_FD, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(_INSTANCE_LOCK_FD)
            except Exception:
                pass
            _INSTANCE_LOCK_FD = None
        if os.path.exists(INSTANCE_LOCK_FILE):
            with open(INSTANCE_LOCK_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            if content == str(os.getpid()):
                os.remove(INSTANCE_LOCK_FILE)
    except Exception:
        pass

def channel_lock_path(recordings_dir, source_channel=None):
    suffix = source_state_suffix(source_channel)
    if suffix:
        return os.path.join(recordings_dir, f".recording.{suffix}.lock")
    return os.path.join(recordings_dir, ".recording.lock")


# Parent holds flock fds for the lifetime of each channel child process.
_CHANNEL_LOCK_FDS = {}


def try_acquire_channel_lock(recordings_dir, source_channel=None, pid=None):
    """Acquire exclusive flock on channel lock file. Returns fd or None if busy."""
    path = channel_lock_path(recordings_dir, source_channel)
    if path in _CHANNEL_LOCK_FDS:
        return _CHANNEL_LOCK_FDS[path]
    try:
        os.makedirs(recordings_dir, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        os.ftruncate(fd, 0)
        os.write(fd, str(pid or os.getpid()).encode("utf-8"))
        try:
            os.fsync(fd)
        except OSError:
            pass
        _CHANNEL_LOCK_FDS[path] = fd
        return fd
    except Exception as exc:
        logger.warning(f"Channel-Lock acquire failed: {path}: {exc}")
        return None


def update_channel_lock_pid(recordings_dir, pid, source_channel=None):
    path = channel_lock_path(recordings_dir, source_channel)
    fd = _CHANNEL_LOCK_FDS.get(path)
    if fd is None or pid is None:
        return
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(pid).encode("utf-8"))
        try:
            os.fsync(fd)
        except OSError:
            pass
    except Exception:
        logger.warning(f"Konnte Channel-Lock PID nicht aktualisieren: {path}")


def release_held_channel_lock(recordings_dir, source_channel=None):
    """Release flock held by this process and remove the lock file."""
    path = channel_lock_path(recordings_dir, source_channel)
    fd = _CHANNEL_LOCK_FDS.pop(path, None)
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def release_channel_lock(recordings_dir, pid=None, source_channel=None):
    """Release channel lock. Prefer release_held_channel_lock from the parent."""
    path = channel_lock_path(recordings_dir, source_channel)
    if path in _CHANNEL_LOCK_FDS:
        # Only the holder should release via flock path.
        if pid is None:
            release_held_channel_lock(recordings_dir, source_channel)
            return
        try:
            fd = _CHANNEL_LOCK_FDS[path]
            os.lseek(fd, 0, os.SEEK_SET)
            content = os.read(fd, 64).decode("utf-8", errors="replace").strip()
            if not content or content == str(pid):
                release_held_channel_lock(recordings_dir, source_channel)
        except Exception:
            release_held_channel_lock(recordings_dir, source_channel)
        return
    if not os.path.exists(path):
        return
    try:
        if pid is None:
            os.remove(path)
            return
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if content and content == str(pid):
            os.remove(path)
    except Exception:
        pass

def cleanup_stale_recording_metadata(recordings_dir, source_channel=None):
    info = load_current_recording(recordings_dir, source_channel)
    if not info:
        return
    info_source = str(info.get("source") or "").strip()
    if source_channel and info_source and info_source != source_channel:
        return
    # Never delete a file that another source currently claims.
    claimed_elsewhere = {
        str(item.get("filename") or "")
        for item in list_current_recordings(recordings_dir)
        if str(item.get("source") or "").strip()
        and (not source_channel or str(item.get("source") or "").strip() != source_channel)
    }

    file_path = os.path.join(recordings_dir, info.get("filename", ""))
    temp_path = f"{file_path}{TEMP_OUTPUT_SUFFIX}"
    # Also consider mkv.part when metadata still points at the mp4 name.
    mkv_path = os.path.splitext(file_path)[0] + ".mkv" if file_path.endswith(".mp4") else ""
    mkv_temp = f"{mkv_path}{TEMP_OUTPUT_SUFFIX}" if mkv_path else ""
    now = time.time()
    owned_name = os.path.basename(file_path)

    def _cleanup_temp(candidate_temp, candidate_output):
        if not candidate_temp or not os.path.exists(candidate_temp):
            return
        if owned_name in claimed_elsewhere:
            return
        try:
            age = now - os.path.getmtime(candidate_temp)
            size = os.path.getsize(candidate_temp)
        except OSError:
            return
        if size < MIN_RECORD_FILE_SIZE:
            try:
                os.remove(candidate_temp)
                logger.info(f"Stale tiny temp recording gelöscht: {candidate_temp}")
            except Exception:
                pass
            return
        if age <= RETRY_DELAY * 2:
            return
        if size >= SALVAGE_MIN_BYTES:
            salvage_temp_recording(
                candidate_temp,
                candidate_output,
                reason="stale_temp",
                source_channel=source_channel,
            )
            return
        try:
            os.remove(candidate_temp)
            logger.info(f"Stale temp recording gelöscht: {candidate_temp}")
        except Exception:
            pass

    _cleanup_temp(temp_path, file_path)
    if mkv_temp and mkv_temp != temp_path:
        _cleanup_temp(mkv_temp, mkv_path or file_path)

    if os.path.exists(file_path) and owned_name not in claimed_elsewhere:
        if os.path.getsize(file_path) < MIN_RECORD_FILE_SIZE:
            try:
                os.remove(file_path)
                logger.info(f"Defekte oder zu kleine Aufnahme gelöscht: {file_path}")
            except Exception:
                pass
            clear_current_recording(recordings_dir, filename=owned_name, source_channel=source_channel)
            return
        if now - info.get("start_ts", 0) > STALE_RECORDING_AGE:
            logger.warning(f"Alte aktuelle Aufnahme-Metadaten gefunden, bereinige sie: {file_path}")
            clear_current_recording(recordings_dir, filename=owned_name, source_channel=source_channel)


def parse_recording_timestamp(filename, source_channel=None, archive_channel=None):
    """Parse timestamps from legacy and platform-tagged recording names.

    Supported:
      {archive}_{YYYY-MM-DD_HH-MM-SS}.mp4
      {archive}_{tw|yt}_{YYYY-MM-DD_HH-MM-SS}.mp4
      {archive}_{tw|yt}_{YYYY-MM-DD_HH-MM-SS}_{n}.mp4
    """
    if not filename or not filename.endswith(".mp4"):
        return None
    stem = filename[:-4]
    match = re.search(
        r"(?:^|_)(?:tw_|yt_)?(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}|\d{8}_\d{6})(?:_\d+)?$",
        stem,
    )
    if not match:
        return None
    timestamp = match.group(1)
    # Optional: require known archive/source prefix when provided.
    prefixes = []
    for candidate in (archive_channel, source_channel):
        name = normalize_channel(candidate) if candidate else None
        if name and name not in prefixes:
            prefixes.append(name)
    if prefixes and not any(stem.startswith(f"{prefix}_") for prefix in prefixes):
        return None
    for fmt in (RECORDING_TIMESTAMP_FORMAT, LEGACY_RECORDING_TIMESTAMP_FORMAT):
        try:
            return time.mktime(time.strptime(timestamp, fmt))
        except Exception:
            continue
    return None


def recording_platform_tag(filename):
    name = f"_{os.path.basename(str(filename or ''))}"
    if "_yt_" in name:
        return "yt"
    if "_tw_" in name:
        return "tw"
    return None


def archive_is_shared(archive_channel):
    """True when more than one configured source writes into this archive.

    Duplicate cleanup must fail closed here: a same-platform file may belong to
    a sibling source that shares the archive directory.
    """
    archive = str(archive_channel or "").strip()
    if not archive:
        return True
    try:
        channels = database.channels_for_archive(archive)
    except Exception as exc:
        logger.warning(
            "Archiv-Zuordnung für %s nicht prüfbar - Duplikat-Bereinigung übersprungen: %s",
            archive,
            exc,
        )
        return True
    return len(channels) > 1


def cleanup_duplicate_recordings(recordings_dir, recording_file, source_channel, archive_channel=None):
    try:
        if not os.path.exists(recording_file) or os.path.getsize(recording_file) < MIN_RECORD_FILE_SIZE:
            return
        recording_name = os.path.basename(recording_file)
        archive = archive_channel or source_channel
        if archive_is_shared(archive):
            logger.info(
                "Duplikat-Bereinigung übersprungen: Archiv %s wird von mehreren Quellen genutzt.",
                archive,
            )
            return
        recording_ts = parse_recording_timestamp(recording_name, source_channel, archive_channel=archive)
        if recording_ts is None:
            return
        platform_tag = recording_platform_tag(recording_name)

        candidates = []
        for entry in os.listdir(recordings_dir):
            if not entry.endswith('.mp4'):
                continue
            if entry == recording_name:
                continue
            entry_tag = recording_platform_tag(entry)
            if platform_tag and entry_tag and entry_tag != platform_tag:
                continue
            candidate_ts = parse_recording_timestamp(entry, source_channel, archive_channel=archive)
            if candidate_ts is None:
                continue
            if abs(candidate_ts - recording_ts) > 90:
                continue
            candidate_path = os.path.join(recordings_dir, entry)
            if not os.path.exists(candidate_path):
                continue
            if abs(os.path.getsize(candidate_path) - os.path.getsize(recording_file)) <= max(1024, os.path.getsize(recording_file) * 0.02):
                candidates.append(candidate_path)

        if not candidates:
            return

        files = [recording_file] + candidates
        keep = max(files, key=lambda p: (os.path.getsize(p), os.path.getmtime(p)))
        for path in files:
            if path == keep:
                continue
            try:
                os.remove(path)
                logger.info(f"Doppelte Aufnahme entfernt: {path}")
            except Exception:
                logger.warning(f"Konnte doppelte Aufnahme nicht löschen: {path}")
    except Exception as e:
        logger.warning(f"Fehler beim Bereinigen doppelter Aufnahmen: {e}")


def stop_process(process):
    if process and process.poll() is None:
        try:
            if os.name != 'nt':
                try:
                    pgid = os.getpgid(process.pid)
                except (ProcessLookupError, OSError):
                    pgid = -1
                if pgid != os.getpgrp() and pgid > 1:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    process.terminate()
            else:
                process.terminate()
            
            # Warte kurz auf sauberes Beenden, sonst Kill
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    try:
                        pgid = os.getpgid(process.pid)
                    except (ProcessLookupError, OSError):
                        pgid = -1
                    if pgid != os.getpgrp() and pgid > 1:
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        process.kill()
                else:
                    process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    logger.error("Prozess %s nach SIGKILL nicht beendet.", process.pid)
        except (ProcessLookupError, OSError) as exc:
            logger.debug("Process already exited during stop: %s", exc)
        except Exception as exc:
            logger.warning("Unexpected error stopping process: %s", exc)

def wait_for_process_exit(process, timeout=FFMPEG_FINALIZE_TIMEOUT):
    """Wait for a child with a hard deadline; kill and reap if it is wedged.

    Returns True when the process exited before the deadline.
    """
    if process is None:
        return True
    try:
        process.wait(timeout=max(1, int(timeout or 1)))
        return True
    except subprocess.TimeoutExpired:
        logger.warning(
            "Prozess %s reagiert nicht innerhalb von %ss - erzwinge Beendigung.",
            getattr(process, "pid", "?"),
            timeout,
        )
        try:
            process.kill()
        except OSError as exc:
            # ProcessLookupError: child exited between the timeout and kill().
            logger.warning(
                "Kill fuer Prozess %s fehlgeschlagen: %s", getattr(process, "pid", "?"), exc
            )
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("Prozess %s konnte nicht beendet werden.", getattr(process, "pid", "?"))
        return False
    except Exception as exc:
        logger.warning("Fehler beim Warten auf Prozess %s: %s", getattr(process, "pid", "?"), exc)
        return False

def cleanup_live_hls(except_channel=None):
    if not os.path.isdir(LIVE_HLS_ROOT):
        return
    now = time.time()
    for name in os.listdir(LIVE_HLS_ROOT):
        if except_channel and name == except_channel:
            continue
        live_dir = os.path.join(LIVE_HLS_ROOT, name)
        if not os.path.isdir(live_dir):
            continue
        for entry in os.listdir(live_dir):
            if not (entry.endswith(".ts") or entry == "index.m3u8"):
                continue
            path = os.path.join(live_dir, entry)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if now - mtime > LIVE_HLS_TTL_SEC:
                try:
                    os.remove(path)
                except OSError:
                    pass

def get_vaapi_device():
    """Gibt ein VAAPI-Device zurück, wenn vorhanden."""
    env_device = os.environ.get('STREAM_RECORDER_VAAPI_DEVICE')
    if env_device:
        return env_device
    for path in ("/dev/dri/renderD128", "/dev/dri/card0"):
        if os.path.exists(path):
            return path
    return None

def streamlink_proxy_args():
    """Opt-in playlist proxy (STREAM_RECORDER_USE_PROXY=true).

    No third-party proxy is built in: the operator must name one in
    TWITCH_PROXY_PLAYLIST (and a plugin directory in STREAMLINK_PLUGIN_DIR if the
    proxy needs a patched Twitch plugin). Without a proxy list nothing is added.
    """
    proxy_list = os.environ.get("TWITCH_PROXY_PLAYLIST", "").strip()
    if not proxy_list:
        logger.warning("STREAM_RECORDER_USE_PROXY is set but TWITCH_PROXY_PLAYLIST is empty; recording without proxy")
        return []
    args = []
    plugin_dir = os.environ.get("STREAMLINK_PLUGIN_DIR", "").strip()
    if plugin_dir:
        args += ["--plugin-dirs", plugin_dir]
    return args + [
        "--twitch-proxy-playlist", proxy_list,
        "--twitch-proxy-playlist-fallback",
        "--twitch-disable-ads",
        "--twitch-low-latency",
        "--hls-live-edge", "2",
        "--stream-segment-threads", "3",
    ]


def streamlink_twitch_auth_args():
    """Optional Streamlink Twitch auth for sub-only / restricted streams. Never log the token."""
    return config.streamlink_twitch_auth_args(getattr(config, "RECORDER_TWITCH_OAUTH_TOKEN", ""))


def build_streamlink_command(source_channel, conf=None):
    """Baut die gefilterte, werbefreie und anonyme Twitch-HLS-Quelle für FFmpeg."""
    conf = conf or get_source_config(source_channel)
    auth_args = streamlink_twitch_auth_args()
    has_oauth = any("--config" == arg for arg in auth_args)
    cmd = [
        find_streamlink(),
        *auth_args,
    ]
    use_proxy = os.environ.get("STREAM_RECORDER_USE_PROXY", "false").lower() in ("true", "1", "yes")
    if use_proxy and not has_oauth:
        cmd.extend(streamlink_proxy_args())
    cmd += [
        "--stdout",
        "--loglevel", "info",
        "--retry-open", str(STREAMLINK_RETRY_OPEN),
        "--stream-timeout", str(STREAMLINK_TIMEOUT),
    ]
    if STREAMLINK_NO_SSL_VERIFY:
        cmd.append("--http-no-ssl-verify")
    cmd += [
        twitch_stream_url(source_channel, conf),
        ",".join(STREAM_QUALITY),
    ]
    return cmd

def build_streamlink_probe_command(source_channel, conf=None):
    """Prueft vor FFmpeg, ob Streamlink einen spielbaren Stream findet (Ad-Free & Ban-Bypass)."""
    conf = conf or get_source_config(source_channel)
    auth_args = streamlink_twitch_auth_args()
    has_oauth = any("--config" == arg for arg in auth_args)
    cmd = [
        find_streamlink(),
        *auth_args,
    ]
    use_proxy = os.environ.get("STREAM_RECORDER_USE_PROXY", "false").lower() in ("true", "1", "yes")
    if use_proxy and not has_oauth:
        cmd.extend(streamlink_proxy_args())
    cmd += [
        "--json",
        "--loglevel", "error",
        "--retry-open", str(max(1, STREAMLINK_PROBE_RETRY_OPEN)),
        "--stream-timeout", str(min(STREAMLINK_TIMEOUT, STREAMLINK_PROBE_TIMEOUT)),
    ]
    if STREAMLINK_NO_SSL_VERIFY:
        cmd.append("--http-no-ssl-verify")
    cmd += [
        twitch_stream_url(source_channel, conf),
        ",".join(STREAM_QUALITY),
    ]
    return cmd

def probe_streamlink_playable(source_channel, conf=None):
    cmd = build_streamlink_probe_command(source_channel, conf)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=STREAMLINK_PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Streamlink-Probe Timeout", "metadata": {}}
    except Exception as exc:
        return {"ok": False, "error": f"Streamlink-Probe Fehler: {exc}", "metadata": {}}

    output = (result.stdout or "").strip()
    payload = {}
    if output:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = {}
    if result.returncode == 0 and payload.get("url"):
        return {"ok": True, "error": "", "metadata": payload.get("metadata") or {}, "platform": "twitch"}

    error = ""
    if isinstance(payload, dict):
        error = str(payload.get("error") or "").strip()
    if not error:
        error = (result.stderr or output or "Keine spielbaren Streams gefunden").strip()
    return {"ok": False, "error": error[:500], "metadata": payload.get("metadata") if isinstance(payload, dict) else {}}

def build_ytdlp_probe_command(source_channel, conf=None):
    conf = conf or get_source_config(source_channel)
    return [
        find_ytdlp(),
        "--no-config",
        "--dump-single-json",
        "--no-playlist",
        "--skip-download",
        "--socket-timeout", str(YTDLP_STREAM_TIMEOUT),
        *ytdlp_js_runtime_args(),
        youtube_live_url(source_channel, conf),
    ]

def build_ytdlp_command(source_channel, conf=None, live_from_start=None):
    conf = conf or get_source_config(source_channel)
    use_live_from_start = YOUTUBE_LIVE_FROM_START if live_from_start is None else bool(live_from_start)
    cmd = [
        find_ytdlp(),
        "--no-config",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--newline",
        "--retries", "infinite",
        "--fragment-retries", "infinite",
        "--socket-timeout", str(YTDLP_STREAM_TIMEOUT),
        *ytdlp_js_runtime_args(),
    ]
    if use_live_from_start:
        # DASH + stdout is unreliable (A/V downloaded sequentially). Prefer HLS live edge.
        logger.warning(
            "[%s] STREAM_RECORDER_YOUTUBE_LIVE_FROM_START ist aktiv — "
            "für Pipe-Aufnahme oft problematisch; bei Fehlern auf false setzen.",
            source_channel,
        )
        cmd.append("--live-from-start")
    cmd += [
        "-f", YOUTUBE_STREAM_QUALITY,
        "-o", "-",
        youtube_live_url(source_channel, conf),
    ]
    return cmd

def youtube_status_from_metadata(metadata):
    metadata = metadata or {}
    categories = metadata.get("categories") or []
    category = categories[0] if isinstance(categories, list) and categories else metadata.get("category") or ""
    viewers = (
        metadata.get("concurrent_view_count")
        or metadata.get("viewers")
        or metadata.get("viewer_count")
        or metadata.get("view_count")
        or 0
    )
    try:
        viewers = int(viewers or 0)
    except (TypeError, ValueError, OverflowError):
        viewers = 0
    return {
        "viewers": viewers,
        "title": str(metadata.get("title") or metadata.get("fulltitle") or ""),
        "category": str(category or ""),
        "started_at": metadata.get("release_timestamp") or metadata.get("timestamp") or metadata.get("upload_date") or "",
        "platform": "YouTube",
        "live_status": str(metadata.get("live_status") or ""),
        "video_id": str(metadata.get("id") or ""),
        "channel": str(metadata.get("channel") or metadata.get("uploader") or ""),
        "thumbnail": str(metadata.get("thumbnail") or ""),
        "ts": time.time(),
    }

def probe_ytdlp_playable(source_channel, conf=None):
    cmd = build_ytdlp_probe_command(source_channel, conf)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=YTDLP_PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "yt-dlp-Probe Timeout", "metadata": {}, "platform": "youtube"}
    except Exception as exc:
        return {"ok": False, "error": f"yt-dlp-Probe Fehler: {exc}", "metadata": {}, "platform": "youtube"}

    output = (result.stdout or "").strip()
    payload = {}
    if output:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = {}

    if result.returncode == 0 and isinstance(payload, dict):
        live_status = str(payload.get("live_status") or "").lower()
        is_live = payload.get("is_live")
        if is_live is True or live_status == "is_live":
            return {"ok": True, "error": "", "metadata": payload, "platform": "youtube"}
        message = live_status or "YouTube-Quelle ist aktuell nicht live"
        if live_status == "is_upcoming":
            message = "YouTube-Livestream ist geplant, aber noch nicht live"
        return {"ok": False, "error": message, "metadata": payload, "platform": "youtube"}

    error = (result.stderr or output or "Keine spielbare YouTube-Livequelle gefunden").strip()
    return {"ok": False, "error": error[:500], "metadata": payload if isinstance(payload, dict) else {}, "platform": "youtube"}

def probe_source_playable(source_channel, conf=None):
    conf = conf or get_source_config(source_channel)
    if conf.get("platform") == "youtube":
        return probe_ytdlp_playable(source_channel, conf)
    return probe_streamlink_playable(source_channel, conf)

def build_source_command(source_channel, conf=None):
    conf = conf or get_source_config(source_channel)
    if conf.get("platform") == "youtube":
        return build_ytdlp_command(source_channel, conf)
    return build_streamlink_command(source_channel, conf)

def compute_retry_backoff(failures, base_delay=RETRY_DELAY, max_delay=STREAM_FAILURE_RETRY_MAX, jitter=False):
    try:
        failures = max(1, int(failures))
        base_delay = max(1, int(base_delay))
        max_delay = max(base_delay, int(max_delay))
    except (TypeError, ValueError):
        return RETRY_DELAY
    delay = min(max_delay, base_delay * (2 ** min(failures - 1, 8)))
    if jitter:
        # Optional spread to avoid synchronized reconnect storms across channels.
        delay = int(delay + random.uniform(0, min(3.0, max(0.5, delay * 0.1))))
    return delay


def compute_idle_poll_delay():
    """Fixed short poll while a channel is not live. Never exponential."""
    try:
        base_delay = max(2, int(STREAM_OFFLINE_RETRY_BASE))
        max_delay = max(base_delay, int(STREAM_OFFLINE_RETRY_MAX))
    except (TypeError, ValueError):
        return 5
    return min(max_delay, base_delay)


def in_restart_grace(deadline):
    """True while we should keep retrying after a stream end / failed start."""
    try:
        return time.time() < float(deadline or 0)
    except (TypeError, ValueError):
        return False


def mark_restart_grace(now=None):
    try:
        grace = max(0, int(STREAM_RESTART_GRACE_SEC))
    except (TypeError, ValueError):
        grace = 180
    return float(now if now is not None else time.time()) + grace


def same_live_session(live_info, session_started_at):
    """True when GQL/Helix still reports the stream we just finished."""
    started = str(session_started_at or "").strip()
    if not started:
        return False
    status = (live_info or {}).get("status") if isinstance(live_info, dict) else {}
    if not isinstance(status, dict):
        status = {}
    current = str(status.get("started_at") or "").strip()
    return bool(current) and current == started


def post_recording_retry_delay(live_info, *, recording_failures=0, session_started_at=""):
    """Backoff after a file ends. Longer when the same Twitch session still looks live."""
    after_live = (live_info or {}).get("live") if isinstance(live_info, dict) else None
    if after_live is False or recording_failures:
        return max(1, int(STREAM_LIVE_UNPLAYABLE_RETRY_SEC))
    if after_live is True or same_live_session(live_info, session_started_at):
        return max(1, int(STREAM_SAME_SESSION_CONFIRM_SEC))
    return max(1, int(STREAM_POST_SUCCESS_RETRY_SEC))


def recording_too_short(started_ts, min_sec=None):
    """True for leftover HLS / failed-start snippets that should not hit Discord."""
    try:
        threshold = int(MIN_VOD_NOTIFY_SEC if min_sec is None else min_sec)
    except (TypeError, ValueError):
        threshold = 60
    try:
        duration = time.time() - float(started_ts or 0)
    except (TypeError, ValueError):
        return True
    return duration < max(1, threshold)


def should_emit_deferred_live_notify(source_channel, conf, recording_metadata=None):
    """False when the source already flipped offline (leftover playlist)."""
    try:
        info = cheap_live_presence(source_channel, conf)
    except Exception:
        return True
    return info.get("live") is not False


def source_eventsub_is_live(source_channel, conf=None):
    """True/False from EventSub cache, or None if unknown/unavailable."""
    conf = conf or get_source_config(source_channel)
    if conf.get("platform") == "youtube":
        return None
    try:
        login = twitch_login_from_source(source_channel, conf)
        return twitch_eventsub.is_live(login)
    except Exception:
        return None


def make_live_wake_check(source_channel, conf=None):
    """Wake idle sleep as soon as EventSub says the Twitch channel is live."""
    conf = conf or get_source_config(source_channel)

    def _check():
        if shutdown_requested:
            return True
        return source_eventsub_is_live(source_channel, conf) is True

    return _check


def cheap_youtube_is_live(source_channel, conf=None):
    """Lightweight YouTube /live page check. Avoids a 45s yt-dlp dump while offline.

    Returns (live, error) where live is True/False/None (unknown).
    """
    url = youtube_live_url(source_channel, conf)
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept-Language": "en-US,en;q=0.9",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            final_url = str(resp.geturl() or "")
            body = resp.read(262144).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if int(getattr(exc, "code", 0) or 0) in (404, 410):
            return False, "YouTube-Quelle ist aktuell nicht live"
        return None, f"YouTube-Livecheck HTTP {getattr(exc, 'code', '?')}"
    except Exception as exc:
        return None, str(exc)[:200]

    compact = body.replace(" ", "")
    if '"isLiveNow":true' in compact or '"isLive":true' in compact:
        return True, ""
    lowered = body.lower()
    if "not currently live" in lowered or '"islivenow":false' in compact.lower():
        return False, "YouTube-Quelle ist aktuell nicht live"
    if "/watch" in final_url and ("LIVE_STREAM" in body or "isLiveNow" in body):
        return True, ""
    return None, ""


def _helix_live_status(source_channel, conf=None):
    """Helix/GQL fallback used when the cheap GQL check is negative or failed."""
    try:
        if not twitch_api.has_helix_auth():
            return None
        login = twitch_login_from_source(source_channel, conf)
        stream = twitch_api.get_stream(login, monitored=True)
    except Exception:
        return None
    if not isinstance(stream, dict) or not stream.get("is_live"):
        return None
    return {
        "viewers": int(stream.get("viewer_count") or 0),
        "title": str(stream.get("title") or ""),
        "category": str(stream.get("game_name") or ""),
        "started_at": str(stream.get("started_at") or ""),
        "platform": "Twitch",
        "ts": time.time(),
    }


def cheap_live_presence(source_channel, conf=None):
    """Fast live check without Streamlink/yt-dlp. live is True/False/None."""
    conf = conf or get_source_config(source_channel)
    result = {"live": None, "source": "unknown", "status": {}, "error": ""}
    if conf.get("platform") == "youtube":
        live, error = cheap_youtube_is_live(source_channel, conf)
        result["live"] = live
        result["source"] = "youtube"
        result["error"] = error or ""
        return result

    eventsub_live = source_eventsub_is_live(source_channel, conf)
    if eventsub_live is True:
        result["live"] = True
        result["source"] = "eventsub"
        return result

    status = get_twitch_stream_status(source_channel, conf)
    if status.get("ok") is False:
        helix_status = _helix_live_status(source_channel, conf)
        if helix_status:
            result["live"] = True
            result["source"] = "helix"
            result["status"] = helix_status
            return result
        result["live"] = None
        result["source"] = "gql-error"
        result["status"] = status or {}
        result["error"] = status.get("error") or "gql_failed"
        return result

    live = stream_status_is_live(status)
    result["status"] = status or {}
    result["source"] = "gql"
    result["live"] = bool(live)
    if not live:
        helix_status = _helix_live_status(source_channel, conf)
        if helix_status:
            result["live"] = True
            result["source"] = "helix"
            result["status"] = helix_status
            return result
    if eventsub_live is False and not live:
        result["source"] = "eventsub+gql"
    return result


def should_probe_source(live_info):
    """Probe Streamlink/yt-dlp only when live status is unknown or probing is forced."""
    if not SKIP_PROBE_WHEN_LIVE:
        return True
    return live_info.get("live") is not True

def unescape_irc_tag(value):
    replacements = {
        r'\s': ' ',
        r'\:': ';',
        r'\r': '\r',
        r'\n': '\n',
        r'\\': '\\',
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    return value

def parse_twitch_emotes(raw_value):
    emotes = []
    if not raw_value:
        return emotes
    for item in raw_value.split('/'):
        if ':' not in item:
            continue
        emote_id, ranges = item.split(':', 1)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', emote_id or ''):
            continue
        for range_part in ranges.split(','):
            if '-' not in range_part:
                continue
            start_raw, end_raw = range_part.split('-', 1)
            try:
                start = int(start_raw)
                end = int(end_raw)
            except ValueError:
                continue
            if start < 0 or end < start:
                continue
            emotes.append({"id": emote_id, "start": start, "end": end})
    return emotes

def parse_twitch_badge_pairs(raw_value):
    pairs = []
    if not raw_value:
        return pairs
    for item in raw_value.split(','):
        if '/' not in item:
            continue
        set_id, version = item.split('/', 1)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', set_id or ''):
            continue
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', version or ''):
            continue
        pairs.append((set_id, version))
    return pairs

def parse_twitch_badges(raw_badges, raw_badge_info=''):
    info = {set_id: version for set_id, version in parse_twitch_badge_pairs(raw_badge_info)}
    badges = []
    for set_id, version in parse_twitch_badge_pairs(raw_badges):
        badge = {"set_id": set_id, "id": version}
        if set_id in info:
            badge["info"] = info[set_id]
        badges.append(badge)
    return badges

def parse_twitch_color(raw_value):
    value = str(raw_value or '').strip()
    if re.fullmatch(r'#[0-9A-Fa-f]{6}', value):
        return value
    return ''

def parse_irc_tags(resp):
    tags = {}
    if not resp.startswith('@'):
        return tags, resp
    try:
        tag_str, rest = resp[1:].split(' ', 1)
    except ValueError:
        return tags, resp
    for tag in tag_str.split(';'):
        if '=' not in tag:
            continue
        key, value = tag.split('=', 1)
        tags[key] = unescape_irc_tag(value)
    return tags, rest

def process_twitch_message(resp):
    """Parst Twitch-IRC-Nachrichten."""
    try:
        tags, resp = parse_irc_tags(resp)
        username = tags.get('display-name') or 'unknown'
        if username == 'unknown':
            match = re.match(r':([a-zA-Z0-9_]+)!', resp)
            if match:
                username = match.group(1)
        msg_idx = resp.find(' :')
        message = resp[msg_idx + 2:] if msg_idx != -1 else ''
        if message.startswith('\x01ACTION '):
            message = message[len('\x01ACTION '):]
            message = message.removesuffix('\x01')
        return (
            username,
            message,
            parse_twitch_emotes(tags.get('emotes', '')),
            parse_twitch_badges(tags.get('badges', ''), tags.get('badge-info', '')),
            parse_twitch_color(tags.get('color', '')),
            tags,
        )
    except Exception as e:
        logger.error(f"Fehler beim Parsen der IRC-Nachricht: {e!s}")
        return ('unknown', '', [], [], '', {})

def format_chat_content(chat_history):
    """Formatiert den Chat-Verlauf fuer das Overlay."""
    try:
        visible_history = list(chat_history)[-MAX_CHAT_MESSAGES:]
        maxlen = 60
        maxlines = 15
        lines = []
        for username, message in visible_history:
            if not message:
                continue
            prefix = f"{username}: "
            words = message.split()
            line = prefix
            for word in words:
                if len(line) + len(word) + 1 <= maxlen:
                    if line != prefix:
                        line += " "
                    line += word
                else:
                    lines.append(line)
                    line = " " * len(prefix) + word
            lines.append(line)
        return "\n".join(lines[-maxlines:]) + "\n"
    except Exception:
        return ""

def write_chat_text(chat_history, output_path):
    """Schreibt die letzten Chatnachrichten in eine Textdatei für das Overlay."""
    try:
        content = format_chat_content(chat_history)
        atomic_write(output_path, content, is_json=False)
        return True
    except Exception as e:
        logger.error(f"Fehler beim Schreiben der Chat-Textdatei: {e!s}")
        return False

def chat_process(source_channel, chat_text_file):
    """Verarbeitet Twitch-Chat-Nachrichten."""
    drop_inherited_instance_lock()
    server = 'irc.chat.twitch.tv'
    port = 6667
    nickname = f"justinfan{random.randint(100000, 999999)}"
    channel = f'#{source_channel.lower()}'
    chat_history = deque(maxlen=MAX_CHAT_MESSAGES)
    recent_chat_entries = deque(maxlen=200)
    recordings_dir = os.path.dirname(chat_text_file)
    
    # Live-Log fuer WebUI (immer schreiben, auch ohne Aufnahme) — per source
    live_log_file = live_chat_log_path(recordings_dir, source_channel)
    try:
        with open(live_log_file, "w", encoding="utf-8"):
            pass
    except Exception:
        pass

    current_info = None
    current_mtime = None
    last_info_check = 0

    def refresh_recording_info():
        nonlocal current_info, current_mtime, last_info_check
        now = time.time()
        if now - last_info_check < 10: # Nur alle 10 Sekunden prüfen
            return
        last_info_check = now
        path = current_recording_path(recordings_dir, source_channel)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            current_info = None
            current_mtime = None
            return
        if current_mtime is None or mtime != current_mtime:
            current_info = load_current_recording(recordings_dir, source_channel)
            current_mtime = mtime

    # Persistente File-Handles um Open/Close Overhead zu minimieren
    handles = {'live': None, 'session': None, 'session_path': None}

    def append_chat_log(username, message, emotes=None, badges=None, color=None, reward=None, event=None):
        now = time.time()
        entry_base = {"user": username or "", "msg": message or ""}
        if emotes:
            entry_base["emotes"] = emotes
        if badges:
            entry_base["badges"] = badges
        if color:
            entry_base["color"] = color
        if isinstance(reward, dict) and reward.get("id"):
            entry_base["reward"] = {
                "id": str(reward.get("id") or "").strip(),
                "title": str(reward.get("title") or "Channel Points").strip() or "Channel Points",
            }
            try:
                cost = int(reward.get("cost") or 0)
            except (TypeError, ValueError):
                cost = 0
            if cost > 0:
                entry_base["reward"]["cost"] = cost
        clean_event = chat_events.sanitize_event(event)
        if clean_event:
            entry_base["event"] = clean_event
        
        try:
            # 1. Live Log (WebUI)
            if not handles['live']:
                handles['live'] = open(live_log_file, "a", encoding="utf-8", buffering=1)
            
            live_entry = {"t": now, **entry_base}
            handles['live'].write(json.dumps(live_entry, ensure_ascii=False) + "\n")
            try:
                handles['live'].flush()
                os.fsync(handles['live'].fileno())
            except Exception:
                pass

            # 2. Session Log (Archiv)
            if current_info:
                log_file = current_info.get("log_file")
                start_ts = current_info.get("start_ts")
                
                if log_file and start_ts:
                    # Handle wechseln wenn Pfad sich ändert
                    if handles['session_path'] != log_file:
                        if handles['session']:
                            handles['session'].close()
                        os.makedirs(os.path.dirname(log_file), exist_ok=True)
                        handles['session'] = open(log_file, "a", encoding="utf-8", buffering=1)
                        handles['session_path'] = log_file
                    
                    media_time = read_recording_media_time(current_info)
                    session_entry = {
                        "t": now,
                        "rel": (
                            media_time
                            if media_time is not None
                            else max(0.0, now - float(start_ts))
                        ),
                        **entry_base
                    }
                    handles['session'].write(json.dumps(session_entry, ensure_ascii=False) + "\n")
                    try:
                        handles['session'].flush()
                        os.fsync(handles['session'].fileno())
                    except Exception:
                        pass
            else:
                # Keine aktive Session -> Handle schließen falls offen
                if handles['session']:
                    handles['session'].close()
                    handles['session'] = None
                    handles['session_path'] = None
                    
        except Exception as e:
            logger.error(f"Fehler beim Schreiben des Chat-Logs: {e}")
            # Handles zurücksetzen bei Fehler
            for k in ['live', 'session']:
                if handles.get(k):
                    try:
                        handles[k].close()
                    except Exception:
                        pass
                    handles[k] = None
            handles['session_path'] = None
    
    try:
        while True:
            sock = None
            try:
                sock = socket.socket()
                sock.settimeout(30)  # Erhöhtes Timeout
                sock.connect((server, port))
                sock.sendall(b"PASS lucky\r\n")
                sock.sendall(b'CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership\r\n')
                sock.sendall(f"NICK {nickname}\r\n".encode())
                sock.sendall(f"JOIN {channel}\r\n".encode())
                sock.settimeout(10) # Kurzes Timeout für recv loop
                logger.info(f"[{source_channel}] IRC-Verbindung hergestellt als {nickname}")
                
                buffer = ''
                last_ping = time.time()
                last_overlay_update = 0
                last_written_content = None
                msg_count = 0
                last_alive_log = time.time()
                
                while True:
                    try:
                        data = sock.recv(4096).decode('utf-8', errors='ignore')
                        if not data:
                            logger.warning(f"[{source_channel}] Verbindung verloren, reconnecting...")
                            break
                        buffer += data
                        
                        # Periodisches Lebenszeichen im Log (alle 5 Min)
                        if time.time() - last_alive_log > 300:
                            logger.info(f"[{source_channel}] Chat-Prozess aktiv. Empfangen: {msg_count} Nachrichten.")
                            last_alive_log = time.time()

                        if time.time() - last_ping > 30: # Häufiger PING
                            sock.sendall(b"PING :tmi.twitch.tv\r\n")
                            last_ping = time.time()
                        while '\r\n' in buffer:
                            line, buffer = buffer.split('\r\n', 1)
                            if line.startswith('PING'):
                                sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                            elif 'CLEARCHAT #' in line:
                                irc_tags, command_line = parse_irc_tags(line)
                                parsed_event = chat_events.parse_clearchat(irc_tags, command_line)
                                if parsed_event:
                                    refresh_recording_info()
                                    try:
                                        append_chat_log(
                                            parsed_event.get("user") or "",
                                            parsed_event.get("msg") or "",
                                            event=parsed_event.get("event"),
                                        )
                                        msg_count += 1
                                    except Exception as chat_exc:
                                        logger.error(
                                            "[%s] CLEARCHAT-Log fehlgeschlagen: %s",
                                            source_channel,
                                            chat_exc,
                                        )
                            elif 'CLEARMSG #' in line:
                                irc_tags, command_line = parse_irc_tags(line)
                                parsed_event = chat_events.parse_clearmsg(irc_tags, command_line)
                                if parsed_event:
                                    refresh_recording_info()
                                    try:
                                        append_chat_log(
                                            parsed_event.get("user") or "",
                                            parsed_event.get("msg") or "",
                                            event=parsed_event.get("event"),
                                        )
                                        msg_count += 1
                                    except Exception as chat_exc:
                                        logger.error(
                                            "[%s] CLEARMSG-Log fehlgeschlagen: %s",
                                            source_channel,
                                            chat_exc,
                                        )
                            elif 'PRIVMSG' in line:
                                parsed = process_twitch_message(line)
                                username, message, emotes, badges, color = parsed[:5]
                                irc_tags = parsed[5] if len(parsed) > 5 else {}
                                reward_id = str((irc_tags or {}).get("custom-reward-id") or "").strip()
                                if username != 'unknown' and (message or reward_id):
                                    refresh_recording_info()
                                    now = time.time()
                                    normalized_username = username.strip()
                                    normalized_message = " ".join((message or "").split())
                                    reward_meta = None
                                    if reward_id:
                                        try:
                                            reward_meta = twitch_api.reward_payload_from_irc_tags(
                                                source_channel, irc_tags
                                            )
                                        except Exception:
                                            reward_meta = {
                                                "id": reward_id,
                                                "title": "Channel Points",
                                            }
                                        if not reward_meta:
                                            reward_meta = {
                                                "id": reward_id,
                                                "title": "Channel Points",
                                            }
                                    # Deduplication: ignore repeated identical messages in a short window
                                    recent_chat_entries = [
                                        (u, m, ts) for u, m, ts in recent_chat_entries if now - ts < 4.0
                                    ]
                                    duplicate = False
                                    if normalized_message and not reward_id:
                                        for u, m, ts in recent_chat_entries:
                                            if u == normalized_username and m == normalized_message and abs(now - ts) < 1.5:
                                                duplicate = True
                                                break
                                    if duplicate:
                                        continue
                                    overlay_message = normalized_message
                                    if reward_meta:
                                        title = str(reward_meta.get("title") or "Channel Points")
                                        cost = reward_meta.get("cost")
                                        cost_part = f" ({cost})" if cost else ""
                                        overlay_message = (
                                            f"[Punkte] {title}{cost_part}"
                                            + (f": {normalized_message}" if normalized_message else "")
                                        )
                                    if overlay_message:
                                        recent_chat_entries.append((normalized_username, overlay_message, now))
                                        chat_history.append((username, overlay_message))

                                    # Performance: Overlay nur max alle 0.5s schreiben
                                    if overlay_message and now - last_overlay_update > 0.5:
                                        # Optimierung: Nur schreiben wenn sich Inhalt geaendert hat
                                        content = format_chat_content(chat_history)
                                        if content != last_written_content:
                                            atomic_write(chat_text_file, content, is_json=False)
                                            last_written_content = content
                                        last_overlay_update = now
                                    # !clip (skip if channel-points already handled above).
                                    try:
                                        if message and not reward_id:
                                            chat_clip.request_clip_from_chat(
                                                source_channel,
                                                recordings_dir,
                                                username,
                                                message,
                                            )
                                    except Exception:
                                        logger.exception(
                                            "[%s] !clip Handling fehlgeschlagen",
                                            source_channel,
                                        )
                                    try:
                                        if message or reward_meta:
                                            append_chat_log(
                                                username,
                                                message or "",
                                                emotes,
                                                badges,
                                                color,
                                                reward=reward_meta,
                                            )
                                    except Exception as chat_exc:
                                        logger.error(
                                            "[%s] Chat-Log fehlgeschlagen: %s",
                                            source_channel,
                                            chat_exc,
                                        )
                                    msg_count += 1
                    except TimeoutError:
                        # Timeout ist normal bei Inaktivität, sende PING zur Sicherheit
                        try:
                            sock.sendall(b"PING :tmi.twitch.tv\r\n")
                        except Exception:
                            pass
                        last_ping = time.time()
                    except Exception as e:
                        logger.error(f"Fehler im Chat-Prozess: {e!s}")
                        break
            except Exception as e:
                logger.error(f"Fehler bei der Verbindung zum Chat: {e!s}")
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
            time.sleep(5)
    finally:
        # Sicherstellen, dass offene Datei-Handles sauber geschlossen werden
        for k in ['live', 'session']:
            h = handles.get(k)
            if h:
                try:
                    try:
                        h.flush()
                    except Exception:
                        pass
                    try:
                        os.fsync(h.fileno())
                    except Exception:
                        pass
                    try:
                        h.close()
                    except Exception:
                        pass
                except Exception:
                    pass

YOUTUBE_CHAT_RENDERER_KEYS = (
    "liveChatTextMessageRenderer",
    "liveChatPaidMessageRenderer",
    "liveChatPaidStickerRenderer",
    "liveChatMembershipItemRenderer",
    "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer",
)

def youtube_text_runs_to_text(value):
    if not isinstance(value, dict):
        return ""
    if value.get("simpleText") is not None:
        return str(value.get("simpleText") or "").strip()
    parts = []
    for run in value.get("runs") or []:
        if not isinstance(run, dict):
            continue
        if run.get("text") is not None:
            parts.append(str(run.get("text") or ""))
            continue
        emoji = run.get("emoji") if isinstance(run.get("emoji"), dict) else None
        if emoji:
            shortcuts = emoji.get("shortcuts") if isinstance(emoji.get("shortcuts"), list) else []
            search_terms = emoji.get("searchTerms") if isinstance(emoji.get("searchTerms"), list) else []
            if shortcuts:
                parts.append(str(shortcuts[0]))
            elif emoji.get("emojiId"):
                parts.append(f":{emoji.get('emojiId')}:")
            elif search_terms:
                parts.append(str(search_terms[0]))
    return "".join(parts).strip()

def iter_youtube_chat_renderers(node):
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in YOUTUBE_CHAT_RENDERER_KEYS:
                renderer = current.get(key)
                if isinstance(renderer, dict):
                    yield key, renderer
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(reversed(current))

def parse_youtube_chat_timestamp(renderer):
    raw = renderer.get("timestampUsec") or renderer.get("timestamp_usec")
    try:
        if raw is not None:
            return max(0.0, int(raw) / 1_000_000.0)
    except (TypeError, ValueError, OverflowError):
        pass
    return time.time()

def extract_youtube_chat_entries(payload):
    entries = []
    for renderer_type, renderer in iter_youtube_chat_renderers(payload):
        username = youtube_text_runs_to_text(renderer.get("authorName"))
        message = youtube_text_runs_to_text(renderer.get("message"))
        if not message:
            message = youtube_text_runs_to_text(renderer.get("headerSubtext"))
        if not message:
            message = youtube_text_runs_to_text(renderer.get("body"))
        amount = youtube_text_runs_to_text(renderer.get("purchaseAmountText"))
        if amount and message and renderer_type in ("liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer"):
            message = f"{amount} {message}"
        if not username or not message:
            continue
        entries.append({
            "t": parse_youtube_chat_timestamp(renderer),
            "user": username[:80],
            "msg": message,
        })
    return entries

def build_ytdlp_chat_command(source_channel, output_template, conf=None):
    conf = conf or get_source_config(source_channel)
    cmd = [
        find_ytdlp(),
        "--no-config",
        "--skip-download",
        "--no-playlist",
        "--write-subs",
        "--sub-langs", "live_chat",
        "--sub-format", "json",
        "--no-progress",
        "--newline",
        "--socket-timeout", str(YTDLP_STREAM_TIMEOUT),
        *ytdlp_js_runtime_args(),
    ]
    if YOUTUBE_LIVE_FROM_START:
        cmd.append("--live-from-start")
    cmd += [
        "-o", output_template,
        youtube_live_url(source_channel, conf),
    ]
    return cmd

def youtube_chat_files(chat_work_dir, prefix):
    try:
        names = os.listdir(chat_work_dir)
    except OSError:
        return []
    paths = []
    for name in names:
        if not name.startswith(prefix):
            continue
        if "live_chat" not in name:
            continue
        if not (name.endswith(".json") or name.endswith(".json.part")):
            continue
        path = os.path.join(chat_work_dir, name)
        if os.path.isfile(path):
            paths.append(path)
    paths.sort(key=lambda path: os.path.getmtime(path))
    return paths

def read_new_youtube_chat_entries(path, offset):
    entries = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            handle.seek(offset)
            while True:
                pos = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    handle.seek(pos)
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entries.extend(extract_youtube_chat_entries(payload))
            return handle.tell(), entries
    except OSError:
        return offset, []

def youtube_chat_process(source_channel, chat_text_file):
    """Archiviert YouTube-Livechat im gleichen JSONL-Format wie Twitch."""
    drop_inherited_instance_lock()
    recordings_dir = os.path.dirname(chat_text_file)
    live_log_file = live_chat_log_path(recordings_dir, source_channel)
    chat_work_dir = os.path.join(recordings_dir, ".youtube_live_chat")
    prefix = source_state_suffix(source_channel) or "youtube"
    output_template = os.path.join(chat_work_dir, prefix)
    chat_history = deque(maxlen=MAX_CHAT_MESSAGES)
    recent_chat_entries = deque(maxlen=200)
    os.makedirs(chat_work_dir, exist_ok=True)
    try:
        with open(live_log_file, "w", encoding="utf-8"):
            pass
    except Exception:
        pass

    current_info = None
    current_mtime = None
    last_info_check = 0
    handles = {"live": None, "session": None, "session_path": None}

    def refresh_recording_info(force=False):
        nonlocal current_info, current_mtime, last_info_check
        now = time.time()
        if not force and now - last_info_check < 10:
            return
        last_info_check = now
        path = current_recording_path(recordings_dir, source_channel)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            current_info = None
            current_mtime = None
            return
        if force or current_mtime is None or mtime != current_mtime:
            current_info = load_current_recording(recordings_dir, source_channel)
            current_mtime = mtime

    def append_chat_log(entry):
        refresh_recording_info()
        event_ts = float(entry.get("t") or time.time())
        entry_base = {
            "user": str(entry.get("user") or "YouTube").strip()[:80],
            "msg": str(entry.get("msg") or ""),
            "platform": "YouTube",
        }
        if not entry_base["msg"]:
            return
        try:
            if not handles["live"]:
                handles["live"] = open(live_log_file, "a", encoding="utf-8", buffering=1)
            handles["live"].write(json.dumps({"t": event_ts, **entry_base}, ensure_ascii=False) + "\n")
            try:
                handles["live"].flush()
                os.fsync(handles["live"].fileno())
            except Exception:
                pass

            if current_info:
                log_file = current_info.get("log_file")
                start_ts = current_info.get("start_ts")
                if log_file and start_ts:
                    if handles["session_path"] != log_file:
                        if handles["session"]:
                            handles["session"].close()
                        os.makedirs(os.path.dirname(log_file), exist_ok=True)
                        handles["session"] = open(log_file, "a", encoding="utf-8", buffering=1)
                        handles["session_path"] = log_file
                    media_time = read_recording_media_time(current_info)
                    rel = media_time if media_time is not None else max(0.0, event_ts - float(start_ts))
                    handles["session"].write(json.dumps({"t": event_ts, "rel": rel, **entry_base}, ensure_ascii=False) + "\n")
                    try:
                        handles["session"].flush()
                        os.fsync(handles["session"].fileno())
                    except Exception:
                        pass
            elif handles["session"]:
                handles["session"].close()
                handles["session"] = None
                handles["session_path"] = None
        except Exception as exc:
            logger.error(f"Fehler beim Schreiben des YouTube-Chat-Logs: {exc}")

    def is_duplicate(entry):
        now = time.time()
        username = str(entry.get("user") or "").strip()
        message = " ".join(str(entry.get("msg") or "").split())
        while recent_chat_entries and now - recent_chat_entries[0][2] > 4.0:
            recent_chat_entries.popleft()
        for prev_user, prev_msg, prev_ts in recent_chat_entries:
            if prev_user == username and prev_msg == message and abs(now - prev_ts) < 1.5:
                return True
        recent_chat_entries.append((username, message, now))
        return False

    try:
        while True:
            refresh_recording_info(force=True)
            if not current_info:
                time.sleep(5)
                continue
            proc = None
            offsets = {}
            try:
                for old_path in youtube_chat_files(chat_work_dir, prefix):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
                conf = get_source_config(source_channel)
                cmd = build_ytdlp_chat_command(source_channel, output_template, conf)
                logger.info(f"[{source_channel}] YouTube-Chat-Prozess gestartet: {youtube_live_url(source_channel, conf)}")
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid if os.name != 'nt' else None,
                )
                last_overlay_update = 0
                last_written_content = None
                while proc.poll() is None:
                    for path in youtube_chat_files(chat_work_dir, prefix):
                        offset, entries = read_new_youtube_chat_entries(path, offsets.get(path, 0))
                        offsets[path] = offset
                        for entry in entries:
                            if is_duplicate(entry):
                                continue
                            chat_history.append((entry["user"], entry["msg"]))
                            now = time.time()
                            if now - last_overlay_update > 0.5:
                                content = format_chat_content(chat_history)
                                if content != last_written_content:
                                    atomic_write(chat_text_file, content, is_json=False)
                                    last_written_content = content
                                last_overlay_update = now
                            append_chat_log(entry)
                    time.sleep(1)

                for path in youtube_chat_files(chat_work_dir, prefix):
                    offset, entries = read_new_youtube_chat_entries(path, offsets.get(path, 0))
                    offsets[path] = offset
                    for entry in entries:
                        if not is_duplicate(entry):
                            append_chat_log(entry)
                logger.info(f"[{source_channel}] YouTube-Chat-Prozess beendet, starte neu falls Stream weiterlaeuft.")
            except Exception as exc:
                logger.error(f"[{source_channel}] Fehler im YouTube-Chat-Prozess: {exc}")
            finally:
                if proc and proc.poll() is None:
                    stop_process(proc)
            time.sleep(5)
    finally:
        for key in ("live", "session"):
            handle = handles.get(key)
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass

def get_twitch_stream_status(channel, conf=None):
    """Holt Live-Metadaten via Twitch GQL."""
    conf = conf or get_source_config(channel)
    twitch_login = twitch_login_from_source(channel, conf)
    query = {
        "query": """
        query ($login: String!) {
            user(login: $login) {
                stream {
                    viewersCount
                    title
                    createdAt
                    game {
                        name
                    }
                }
            }
        }
        """,
        "variables": {"login": twitch_login}
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
                stream = data["data"]["user"].get("stream")
                if stream:
                    game = stream.get("game") or {}
                    return {
                        "viewers": int(stream.get("viewersCount") or 0),
                        "title": str(stream.get("title") or ""),
                        "category": str(game.get("name") or ""),
                        "started_at": str(stream.get("createdAt") or ""),
                        "platform": "Twitch",
                        "ts": time.time(),
                        "ok": True,
                    }
                return {
                    "viewers": 0,
                    "title": "",
                    "category": "",
                    "started_at": "",
                    "platform": "Twitch",
                    "ts": time.time(),
                    "ok": True,
                }
    except Exception as exc:
        return {
            "viewers": 0,
            "title": "",
            "category": "",
            "started_at": "",
            "platform": "Twitch",
            "ts": time.time(),
            "ok": False,
            "error": str(exc)[:200],
        }
    return {
        "viewers": 0,
        "title": "",
        "category": "",
        "started_at": "",
        "platform": "Twitch",
        "ts": time.time(),
        "ok": False,
        "error": "gql_unexpected",
    }

def get_youtube_stream_status(channel, conf=None, probe_metadata=None):
    if probe_metadata:
        return youtube_status_from_metadata(probe_metadata)
    probe = probe_ytdlp_playable(channel, conf)
    return youtube_status_from_metadata(probe.get("metadata") or {})

def get_stream_status(channel, conf=None, probe_metadata=None):
    conf = conf or get_source_config(channel)
    if conf.get("platform") == "youtube":
        return get_youtube_stream_status(channel, conf, probe_metadata=probe_metadata)
    return get_twitch_stream_status(channel, conf)


def stream_status_is_live(status):
    """True when Twitch/YouTube metadata indicates an active live session."""
    if not isinstance(status, dict):
        return False
    if str(status.get("started_at") or "").strip():
        return True
    live_status = str(status.get("live_status") or "").strip().lower()
    if live_status in ("is_live", "live", "was_live"):
        # was_live can appear briefly around end; still treat as live for notify edge.
        if live_status != "was_live":
            return True
    try:
        if int(status.get("viewers") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def viewer_process(source_channel, viewer_text_file):
    """Aktualisiert periodisch die Zuschauerzahl in einer Textdatei."""
    recordings_dir = os.path.dirname(viewer_text_file)
    suffix = source_state_suffix(source_channel)
    json_file = (
        os.path.join(recordings_dir, f".viewer_count.{suffix}.json")
        if suffix
        else os.path.join(recordings_dir, ".viewer_count.json")
    )
    was_live = False
    while True:
        try:
            conf = get_source_config(source_channel)
            status = get_stream_status(source_channel, conf)
            viewers = status.get("viewers", 0)
            text = f"Viewers: {viewers}"
            atomic_write(viewer_text_file, text)
            atomic_write(json_file, status, is_json=True)
            # Compatibility mirror for UI that still reads the unsuffixed file.
            legacy = load_json_file(os.path.join(recordings_dir, ".viewer_count.json"), {})
            legacy_source = str((legacy or {}).get("source") or (legacy or {}).get("channel") or "").strip()
            if not legacy or not legacy_source or legacy_source == source_channel:
                status_out = dict(status)
                status_out["source"] = source_channel
                atomic_write(os.path.join(recordings_dir, ".viewer_count.json"), status_out, is_json=True)
            update_recording_viewer_metadata(recordings_dir, status, source_channel=source_channel)

            # Discord live notify on online edge for recorded channels only,
            # even when Streamlink/yt-dlp cannot start yet.
            is_live = stream_status_is_live(status)
            if is_live and not was_live and conf.get("record"):
                archive_channel = conf.get("archive_name") or source_channel
                meta = {
                    "title": status.get("title") or "",
                    "category": status.get("category") or "",
                    "viewers": int(status.get("viewers") or 0),
                    "platform": status.get("platform") or conf.get("platform") or "Twitch",
                    "started_at": status.get("started_at") or "",
                    "source_url": source_public_url(source_channel, conf),
                    "video_id": status.get("video_id") or "",
                    "thumbnail": status.get("thumbnail") or "",
                }
                notify_discord_streamer_online(
                    archive_channel,
                    source_channel,
                    meta,
                    conf,
                    time.time(),
                )
            was_live = is_live
        except Exception as e:
            logger.error(f"Fehler im Viewer-Prozess: {e}")
        
        # Alle 30 Sekunden aktualisieren
        time.sleep(30)

def remux_recording(file_path, reencode_audio=False):
    """Remuxt MP4/MKV zur Stabilisierung von Timestamps/Audio.

    Optional O_EXCL lock file prevents concurrent remux of the same path.
    MKV inputs are remuxed to the sibling .mp4 archive name.
    """
    if not os.path.exists(file_path):
        return False
    lock_path = f"{file_path}.remux.lock"
    lock_held = False
    for _attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            lock_held = True
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
            logger.warning(f"Remux übersprungen (Lock aktiv): {file_path}")
            return False
    if not lock_held:
        logger.warning(f"Remux übersprungen (Lock aktiv): {file_path}")
        return False

    tmp_path = None
    try:
        ffmpeg_path = find_ffmpeg()
        lower = file_path.lower()
        if lower.endswith(".mkv"):
            final_path = os.path.splitext(file_path)[0] + ".mp4"
            tmp_path = f"{final_path}.remux.mp4"
            replace_input = False
        else:
            final_path = file_path
            tmp_path = f"{file_path}.remux.mp4"
            replace_input = True
        cmd = [
            ffmpeg_path, "-y", "-i", file_path,
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
            # Keep startup metadata small for multi-hour, high-frame-rate VODs.
            "-movflags", BROWSER_MP4_MOVFLAGS,
            "-avoid_negative_ts", "make_zero",
            "-fflags", "+genpts+igndts",
            tmp_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=REMUX_TIMEOUT)
        if result.returncode == 0 and os.path.exists(tmp_path):
            finalize_if_fragmented(tmp_path)
            os.replace(tmp_path, final_path)
            if not replace_input and final_path != file_path:
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            logger.info(f"Remux erfolgreich: {final_path}")
            return True
        else:
            logger.error(f"Remux fehlgeschlagen: {result.stderr}")
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
    except Exception as e:
        logger.error(f"Remux Fehler: {e}")
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass
    return False

def record_stream(source_channel, chat_text_file, recordings_dir):
    """Zeichnet den Stream mit Chat-Overlay in eine Videodatei auf."""
    ffmpeg_path = find_ffmpeg()
    font_path = find_font("DejaVuSans-Bold.ttf") or "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    process = None
    source_process = None
    offline_failures = 0
    recording_failures = 0
    playable_failures = 0
    copy_fallback = False
    restart_grace_until = 0.0
    last_session_started_at = ""
    archive_channel = os.path.basename(os.path.normpath(recordings_dir)) or source_channel
    
    try:
        while True:
            if shutdown_requested:
                logger.info("Aufnahme-Loop unterbrochen.")
                return
            output_file = None
            temp_output_file = None
            record_file = None
            use_copy = False
            next_delay = RETRY_DELAY
            
            # Check ob Aufnahme noch aktiv ist (fuer Wechsel zu Chat-Only ohne Restart)
            conf = get_source_config(source_channel)
            if not conf or not conf.get('record'):
                logger.info(f"Aufnahme deaktiviert für {source_channel}")
                return
            archive_channel = conf.get("archive_name") or archive_channel

            process = None
            source_process = None
            cleanup_stale_recording_metadata(recordings_dir, source_channel=source_channel)

            if disk_blocks_recording(recordings_dir):
                next_delay = compute_retry_backoff(
                    max(1, recording_failures + 1),
                    RETRY_DELAY,
                    STREAM_FAILURE_RETRY_MAX,
                    jitter=True,
                )
                save_recording_status(
                    recordings_dir,
                    {
                        "state": "paused_disk",
                        "message": (
                            f"Aufnahme pausiert: Speicher voll "
                            f"(used>={STORAGE_PAUSE_PCT}% oder free<={STORAGE_PAUSE_FREE_GB}GB)."
                        ),
                        "source": source_channel,
                        "platform": conf.get("platform"),
                        "next_retry_at": time.time() + next_delay,
                    },
                    source_channel=source_channel,
                )
                throttled_log(
                    "warning",
                    f"disk-pause-{source_channel}",
                    f"{source_channel}: Low disk — pausing new recordings for {next_delay}s.",
                    interval=120,
                )
                interruptible_sleep(next_delay)
                continue

            try:
                if not os.path.exists(chat_text_file):
                    write_chat_text([], chat_text_file)
                viewer_text_file = overlay_viewer_path(recordings_dir, source_channel)
                if not os.path.exists(viewer_text_file):
                    atomic_write(viewer_text_file, "Viewers: 0")

                wake_check = make_live_wake_check(source_channel, conf)
                live_info = cheap_live_presence(source_channel, conf)
                is_live = live_info.get("live")
                keep_trying = in_restart_grace(restart_grace_until)
                if is_live is False and keep_trying:
                    throttled_log(
                        "info",
                        f"stream-restart-grace-{source_channel}",
                        f"{source_channel}: GQL sagt offline, Restart-Grace aktiv — versuche Stream weiter.",
                        interval=30,
                    )
                    is_live = None
                if is_live is False:
                    offline_failures += 1
                    playable_failures = 0
                    recording_failures = 0
                    next_delay = compute_idle_poll_delay()
                    message = live_info.get("error") or "Kanal ist aktuell nicht live"
                    clear_current_recording(recordings_dir, source_channel=source_channel)
                    save_recording_status(
                        recordings_dir,
                        {
                            "state": "offline",
                            "message": message,
                            "source": source_channel,
                            "platform": conf.get("platform"),
                            "failures": offline_failures,
                            "checked_at": time.time(),
                            "next_retry_at": time.time() + next_delay,
                            "live_source": live_info.get("source"),
                        },
                        source_channel=source_channel,
                    )
                    throttled_log(
                        "info",
                        f"stream-offline-{source_channel}",
                        f"{source_channel}: offline ({message}). Naechster Versuch in {next_delay}s.",
                        interval=120,
                    )
                    interruptible_sleep(next_delay, wake_check=wake_check)
                    continue

                stream_probe = {
                    "ok": True,
                    "error": "",
                    "metadata": live_info.get("status") or {},
                    "platform": conf.get("platform") or "twitch",
                }
                if should_probe_source(live_info):
                    stream_probe = probe_source_playable(source_channel, conf)
                if not stream_probe.get("ok"):
                    message = stream_probe.get("error") or "Keine spielbaren Streams gefunden"
                    clear_current_recording(recordings_dir, source_channel=source_channel)
                    if is_live is True or keep_trying:
                        playable_failures += 1
                        next_delay = max(1, int(STREAM_LIVE_UNPLAYABLE_RETRY_SEC))
                        save_recording_status(
                            recordings_dir,
                            {
                                "state": "starting",
                                "message": "Kanal ist live, warte auf spielbaren Stream.",
                                "source": source_channel,
                                "platform": conf.get("platform"),
                                "failures": playable_failures,
                                "checked_at": time.time(),
                                "next_retry_at": time.time() + next_delay,
                                "live_source": live_info.get("source"),
                            },
                            source_channel=source_channel,
                        )
                        throttled_log(
                            "info",
                            f"stream-unplayable-{source_channel}",
                            f"{source_channel}: live, aber noch nicht spielbar ({message}). "
                            f"Naechster Versuch in {next_delay}s.",
                            interval=30,
                        )
                        interruptible_sleep(next_delay, wake_check=wake_check)
                        continue
                    offline_failures += 1
                    next_delay = compute_idle_poll_delay()
                    save_recording_status(
                        recordings_dir,
                        {
                            "state": "offline",
                            "message": message,
                            "source": source_channel,
                            "platform": conf.get("platform"),
                            "failures": offline_failures,
                            "checked_at": time.time(),
                            "next_retry_at": time.time() + next_delay,
                            "live_source": live_info.get("source"),
                        },
                        source_channel=source_channel,
                    )
                    throttled_log(
                        "info",
                        f"stream-offline-{source_channel}",
                        f"{source_channel}: offline/kein spielbarer Stream ({message}). "
                        f"Naechster Versuch in {next_delay}s.",
                        interval=120,
                    )
                    interruptible_sleep(next_delay, wake_check=wake_check)
                    continue

                offline_failures = 0
                playable_failures = 0

                # Erstelle Dateinamen mit Zeitstempel + Plattform-Tag (keine Kollision im Shared Archive)
                timestamp = time.strftime(RECORDING_TIMESTAMP_FORMAT)
                output_file = unique_recording_output_path(
                    recordings_dir,
                    archive_channel,
                    conf.get("platform"),
                    timestamp,
                )
                use_vaapi = USE_VAAPI and get_vaapi_device() is not None
                use_copy = bool(COPY_MODE and not use_vaapi and not copy_fallback)
                if use_copy:
                    record_file = os.path.splitext(output_file)[0] + ".mkv"
                else:
                    record_file = output_file
                temp_output_file = f"{record_file}{TEMP_OUTPUT_SUFFIX}"
                base_name = os.path.splitext(os.path.basename(output_file))[0]
                chat_log_dir = os.path.join(recordings_dir, CHAT_LOG_SUBDIR)
                chat_log_file = os.path.join(chat_log_dir, f"{base_name}.jsonl")
                timeline_file = recording_timeline_path(recordings_dir, source_channel)
                probe_metadata = stream_probe.get("metadata") or {}
                stream_status = get_stream_status(source_channel, conf, probe_metadata=probe_metadata)
                if not stream_status.get("title") and probe_metadata.get("title"):
                    stream_status["title"] = str(probe_metadata.get("title") or "")
                if not stream_status.get("category") and probe_metadata.get("category"):
                    stream_status["category"] = str(probe_metadata.get("category") or "")
                recording_started_ts = time.time()
                recording_metadata = {
                    "streamer": archive_channel,
                    "source": source_channel,
                    "source_url": source_public_url(source_channel, conf),
                    "filename": os.path.basename(output_file),
                    "title": stream_status.get("title", ""),
                    "category": stream_status.get("category", ""),
                    "viewers": int(stream_status.get("viewers") or 0),
                    "platform": stream_status.get("platform", "Twitch"),
                    "started_at": stream_status.get("started_at", ""),
                    "start_ts": recording_started_ts,
                }
                if stream_status.get("video_id"):
                    recording_metadata["video_id"] = stream_status["video_id"]
                last_session_started_at = str(recording_metadata.get("started_at") or "").strip()
                if stream_status.get("channel"):
                    recording_metadata["channel"] = stream_status["channel"]
                if stream_status.get("thumbnail"):
                    recording_metadata["thumbnail"] = stream_status["thumbnail"]
                # Prefer the platform-correct watch/live URL for Discord posts.
                if str(recording_metadata.get("platform") or "").lower() in ("youtube", "yt", "youtu.be"):
                    if recording_metadata.get("video_id"):
                        recording_metadata["source_url"] = (
                            f"https://www.youtube.com/watch?v={recording_metadata['video_id']}"
                        )
                    else:
                        recording_metadata["source_url"] = youtube_live_url(source_channel, conf)
                os.makedirs(chat_log_dir, exist_ok=True)
                atomic_write(
                    timeline_file,
                    {"media_time": 0.0, "updated_ts": time.time()},
                    is_json=True,
                )
                atomic_write(f"{output_file}.info.json", recording_metadata, is_json=True)
                save_recording_status(
                    recordings_dir,
                    {
                        "state": "starting",
                        "message": "Streamlink-Probe erfolgreich, FFmpeg startet.",
                        "filename": os.path.basename(output_file),
                        "title": recording_metadata["title"],
                        "category": recording_metadata["category"],
                        "viewers": recording_metadata["viewers"],
                        "platform": recording_metadata["platform"],
                        "source": source_channel,
                    },
                )

                source_cmd = build_source_command(source_channel, conf)
                logger.info(
                    f"Starte {recording_metadata['platform']}-Aufnahme in: {record_file}"
                    + (" [copy/mkv]" if use_copy else "")
                )
                source_stderr_path = os.path.join(
                    recordings_dir,
                    f".source_stderr.{source_state_suffix(source_channel) or 'src'}.log",
                )
                try:
                    source_stderr_handle = open(source_stderr_path, "w", encoding="utf-8", errors="replace")
                except OSError:
                    source_stderr_handle = subprocess.DEVNULL
                source_process = subprocess.Popen(
                    source_cmd,
                    stdout=subprocess.PIPE,
                    stderr=source_stderr_handle,
                    bufsize=0,
                    preexec_fn=os.setsid if os.name != 'nt' else None,
                )
                if source_stderr_handle not in (None, subprocess.DEVNULL):
                    try:
                        source_stderr_handle.close()
                    except Exception:
                        pass
                if source_process.stdout is None:
                    raise RuntimeError("Streamlink stdout ist nicht verfügbar.")

                ffmpeg_cmd = [
                    ffmpeg_path, '-y',
                    '-nostdin',
                    '-nostats',
                    '-stats_period', '1',
                    '-progress', 'pipe:1',
                    '-thread_queue_size', '4096',
                    '-probesize', '512k',
                    '-analyzeduration', '5000000',
                    '-rtbufsize', '512M',
                    '-threads', FFMPEG_THREADS,
                ]
                if use_copy:
                    ffmpeg_cmd += [
                        '-i', 'pipe:0',
                        '-map', '0:v:0',
                        '-map', '0:a?',
                        '-c:v', 'copy',
                        '-c:a', 'copy',
                        '-avoid_negative_ts', 'make_zero',
                        '-fflags', '+genpts+discardcorrupt',
                        '-f', 'matroska',
                        temp_output_file,
                    ]
                else:
                    filter_str = build_filter_str(font_path, chat_text_file, viewer_text_file, use_vaapi)
                    output_args = ['-f', 'mp4', '-movflags', '+faststart', temp_output_file]
                    if use_vaapi:
                        ffmpeg_cmd += ['-vaapi_device', get_vaapi_device()]
                    ffmpeg_cmd += [
                        '-i', 'pipe:0',
                        '-filter_complex', filter_str,
                        '-map', '[v]',
                        '-map', '0:a?',
                    ]
                    if use_vaapi:
                        ffmpeg_cmd += ['-c:v', 'h264_vaapi', '-qp', '26']
                    else:
                        ffmpeg_cmd += ['-c:v', 'libx264', '-preset', ENC_PRESET, '-tune', 'zerolatency', '-crf', ENC_CRF]
                    ffmpeg_cmd += [
                        '-vsync', '1',
                        '-af', 'aresample=async=1:min_hard_comp=0.1:first_pts=0',
                        '-pix_fmt', 'yuv420p', '-g', '60', '-keyint_min', '60',
                        '-force_key_frames', f"expr:gte(t,n_forced*{LIVE_HLS_TIME})",
                        '-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', '128k', '-ar', '48000', '-ac', '2',
                        '-avoid_negative_ts', 'make_zero',
                        '-fflags', '+genpts+discardcorrupt',
                    ] + output_args
                
                try:
                    process = subprocess.Popen(
                        ffmpeg_cmd,
                        stdin=source_process.stdout,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                        preexec_fn=os.setsid if os.name != 'nt' else None
                    )
                except Exception:
                    stop_process(source_process)
                    raise
                source_process.stdout.close()
                save_current_recording(
                    recordings_dir,
                    {
                        "filename": os.path.basename(output_file),
                        "start_ts": recording_started_ts,
                        "log_file": chat_log_file,
                        "timeline_file": timeline_file,
                        "title": recording_metadata["title"],
                        "category": recording_metadata["category"],
                        "viewers": recording_metadata["viewers"],
                        "platform": recording_metadata["platform"],
                        "source": source_channel,
                        "source_url": recording_metadata["source_url"],
                        "record_file": os.path.basename(record_file),
                    },
                    source_channel=source_channel,
                )
                save_recording_status(
                    recordings_dir,
                    {
                        "state": "recording",
                        "message": "Aufnahme laeuft.",
                        "filename": os.path.basename(output_file),
                        "title": recording_metadata["title"],
                        "category": recording_metadata["category"],
                        "viewers": recording_metadata["viewers"],
                        "platform": recording_metadata["platform"],
                        "source": source_channel,
                        "started_at": recording_started_ts,
                    },
                )
                live_notify_sent = False
                try:
                    chat_clip.ensure_live_hls_enabled(source_channel)
                except Exception:
                    logger.exception(
                        "[%s] Live-HLS für !clip konnte nicht aktiviert werden",
                        source_channel,
                    )
                
                last_output_time = time.time()
                last_disk_check_time = time.time()
                hang_detected = False
                
                if process.stdout is not None:
                    while True:
                        if shutdown_requested:
                            logger.info("Shutdown angefordert, beende laufende FFmpeg-Aufnahme...")
                            stop_process(process)
                            break
                        if process.poll() is not None:
                            break
                        
                        reads = [process.stdout.fileno()]
                        try:
                            ret = select.select(reads, [], [], 1.0)
                        except OSError:
                            break

                        if reads and reads[0] in ret[0]:
                            line = process.stdout.readline()
                            if line:
                                line_stripped = line.strip()
                                media_time = parse_ffmpeg_media_time(line_stripped)
                                if media_time is not None:
                                    atomic_write(
                                        timeline_file,
                                        {
                                            "media_time": media_time,
                                            "updated_ts": time.time(),
                                        },
                                        is_json=True,
                                    )
                                    if (
                                        not live_notify_sent
                                        and media_time >= LIVE_NOTIFY_MIN_MEDIA_SEC
                                    ):
                                        live_notify_sent = True
                                        if should_emit_deferred_live_notify(
                                            source_channel,
                                            conf,
                                            recording_metadata,
                                        ):
                                            notify_discord_streamer_online(
                                                archive_channel,
                                                source_channel,
                                                recording_metadata,
                                                conf,
                                                recording_started_ts,
                                            )
                                        else:
                                            logger.info(
                                                "%s: Discord LIVE übersprungen "
                                                "(Quelle nach %.0fs Media bereits offline).",
                                                source_channel,
                                                media_time,
                                            )
                                if DEBUG:
                                    logger.info(f"FFmpeg: {line_stripped}")
                                elif 'error' in line_stripped.lower() or 'warning' in line_stripped.lower():
                                    logger.warning(f"FFmpeg: {line_stripped}")
                                else:
                                    logger.debug(f"FFmpeg: {line_stripped}")
                                last_output_time = time.time()
                            else:
                                # EOF auf stdout erreicht - Beende die Lese-Schleife.
                                break

                        if time.time() - last_output_time > FFMPEG_OUTPUT_TIMEOUT:
                            logger.warning("FFmpeg scheint hängen zu bleiben, beende Aufnahme...")
                            hang_detected = True
                            stop_process(process)
                            stop_process(source_process)
                            break
                else:
                    logger.error("FFmpeg-Prozess stdout ist None. Kann keine Ausgabe lesen.")
                    
                if hang_detected and process.poll() is None:
                    wait_for_process_exit(process, timeout=10)
                else:
                    wait_for_process_exit(process, timeout=FFMPEG_FINALIZE_TIMEOUT)
                stop_process(source_process)
                
                if process.returncode == 0:
                    if os.path.exists(temp_output_file) and os.path.getsize(temp_output_file) > MIN_RECORD_FILE_SIZE:
                        os.replace(temp_output_file, record_file)
                        finalized_path = record_file
                        logger.info(f"Aufnahme erfolgreich beendet: {finalized_path}")
                        logger.info(f"Video-Datei erstellt: {os.path.getsize(finalized_path)} Bytes")
                        recording_failures = 0
                        copy_fallback = False
                        save_recording_status(
                            recordings_dir,
                            {
                                "state": "completed",
                                "message": "Aufnahme erfolgreich beendet.",
                                "filename": os.path.basename(output_file),
                                "size": os.path.getsize(finalized_path),
                                "finished_at": time.time(),
                                "source": source_channel,
                                "platform": conf.get("platform"),
                            },
                            source_channel=source_channel,
                        )
                        try:
                            from core import redis_client
                            redis_client.invalidate_dashboard_cache()
                        except Exception as exc:
                            logger.warning(f"Dashboard-Cache-Invalidierung fehlgeschlagen: {exc}")
                        short_recording = recording_too_short(recording_started_ts)
                        if short_recording:
                            logger.info(
                                "%s: Aufnahme zu kurz (%.0fs) — kein Remux.",
                                source_channel,
                                max(0.0, time.time() - float(recording_started_ts or 0)),
                            )
                        if AUTO_REMUX and not short_recording:
                            if SYNC_REMUX:
                                logger.info("Starte Remux zur A/V-Stabilisierung (sync)...")
                                remux_recording(finalized_path, reencode_audio=REENCODE_AUDIO)
                                if finalized_path.lower().endswith(".mkv") and os.path.exists(output_file):
                                    finalized_path = output_file
                            else:
                                logger.info("Remux wird als Worker-Task eingeordnet...")
                                enqueue_remux_vod(finalized_path, reencode_audio=REENCODE_AUDIO)
                        thumb_target = output_file if os.path.exists(output_file) else finalized_path
                        if (not short_recording) and thumb_target.lower().endswith(".mp4"):
                            enqueue_thumbnail_task(
                                thumb_target,
                                archive_channel,
                                discord_notify_ready=False,
                            )
                        # Never offload a file that failed integrity validation.
                        if not test_video_file(finalized_path):
                            logger.warning("Video-Datei scheint beschädigt zu sein")
                        else:
                            # An asynchronous remux may replace the file later; in
                            # that case the periodic storage sync uploads the final
                            # version instead of this intermediate file.
                            if not (AUTO_REMUX and not SYNC_REMUX and not short_recording):
                                maybe_enqueue_storage_upload(finalized_path)
                            if (not short_recording) and AUTO_TRANSCRIBE and (
                                finalized_path.lower().endswith(".mp4") or os.path.exists(output_file)
                            ):
                                media_for_tx = output_file if os.path.exists(output_file) else finalized_path
                                if media_for_tx.lower().endswith(".mp4"):
                                    transcript_path = os.path.splitext(output_file)[0] + ".transcript.txt"
                                    try:
                                        database.add_task(
                                            'transcribe',
                                            {'media': output_file, 'transcript': transcript_path},
                                        )
                                        logger.info(f"Automatische Transkription eingeordnet: {output_file}")
                                    except Exception as exc:
                                        logger.error(f"Konnte automatische Transkription nicht einreihen: {exc}")
                        cleanup_duplicate_recordings(
                            recordings_dir,
                            output_file,
                            source_channel,
                            archive_channel=archive_channel,
                        )
                    else:
                        logger.error(f"Video-Datei ist zu klein oder fehlt: {output_file}")
                        if use_copy:
                            copy_fallback = True
                            logger.warning(
                                "COPY_MODE lieferte keine gueltige Datei — naechster Versuch nutzt Transcode."
                            )
                        recording_failures += 1
                        next_delay = compute_retry_backoff(
                            recording_failures, RETRY_DELAY, STREAM_FAILURE_RETRY_MAX, jitter=True
                        )
                        save_recording_status(
                            recordings_dir,
                            {
                                "state": "error",
                                "message": "FFmpeg beendete ohne gueltige Videodatei.",
                                "filename": os.path.basename(output_file),
                                "failures": recording_failures,
                                "next_retry_at": time.time() + next_delay,
                                "source": source_channel,
                                "platform": conf.get("platform"),
                            },
                            source_channel=source_channel,
                        )
                        if temp_output_file and os.path.exists(temp_output_file):
                            salvage_temp_recording(
                                temp_output_file,
                                record_file or output_file,
                                reason="empty_or_tiny_after_success_code",
                            )
                else:
                    source_hint = ""
                    try:
                        stderr_path = os.path.join(
                            recordings_dir,
                            f".source_stderr.{source_state_suffix(source_channel) or 'src'}.log",
                        )
                        if os.path.isfile(stderr_path):
                            with open(stderr_path, "r", encoding="utf-8", errors="replace") as handle:
                                tail = [line.strip() for line in handle.readlines()[-12:] if line.strip()]
                            if tail:
                                source_hint = " | Quelle: " + " / ".join(tail[-3:])
                                logger.error(
                                    "Quellprozess-Fehler (%s):\n%s",
                                    source_channel,
                                    "\n".join(tail),
                                )
                    except Exception:
                        source_hint = ""
                    logger.error(f"FFmpeg beendet mit Fehlercode {process.returncode}{source_hint}")
                    if use_copy:
                        temp_size = 0
                        try:
                            if temp_output_file and os.path.exists(temp_output_file):
                                temp_size = os.path.getsize(temp_output_file)
                        except OSError:
                            temp_size = 0
                        if temp_size < MIN_RECORD_FILE_SIZE:
                            copy_fallback = True
                            logger.warning(
                                "COPY_MODE schnell fehlgeschlagen — naechster Versuch nutzt Transcode."
                            )
                    recording_failures += 1
                    next_delay = compute_retry_backoff(
                        recording_failures, RETRY_DELAY, STREAM_FAILURE_RETRY_MAX, jitter=True
                    )
                    err_msg = f"FFmpeg Fehlercode {process.returncode}"
                    if "Requested format is not available" in source_hint:
                        err_msg = "YouTube-Format nicht verfuegbar (live-from-start/DASH?)"
                    elif "Invalid data found" in source_hint or process.returncode == 183:
                        if "format" in source_hint.lower():
                            err_msg = "YouTube-Quelle lieferte kein gueltiges Stream-Format"
                    save_recording_status(
                        recordings_dir,
                        {
                            "state": "error",
                            "message": err_msg,
                            "filename": os.path.basename(output_file),
                            "failures": recording_failures,
                            "next_retry_at": time.time() + next_delay,
                            "source": source_channel,
                            "platform": conf.get("platform"),
                        },
                        source_channel=source_channel,
                    )
                    if temp_output_file and os.path.exists(temp_output_file):
                        salvage_temp_recording(
                            temp_output_file,
                            record_file or output_file,
                            reason=f"ffmpeg_exit_{process.returncode}",
                        )

            except Exception as e:
                logger.error(f"Fehler bei der Aufnahme: {e!s}")
                recording_failures += 1
                next_delay = compute_retry_backoff(
                    recording_failures, RETRY_DELAY, STREAM_FAILURE_RETRY_MAX, jitter=True
                )
                save_recording_status(
                    recordings_dir,
                    {
                        "state": "error",
                        "message": str(e)[:500],
                        "filename": os.path.basename(output_file) if output_file else "",
                        "failures": recording_failures,
                        "next_retry_at": time.time() + next_delay,
                        "source": source_channel,
                        "platform": (get_source_config(source_channel) or {}).get("platform"),
                    },
                    source_channel=source_channel,
                )
                # Always tear down FFmpeg/source process groups after disconnect/errors.
                stop_process(process)
                stop_process(source_process)
                process = None
                source_process = None

                if temp_output_file and os.path.exists(temp_output_file):
                    salvage_temp_recording(
                        temp_output_file,
                        record_file or output_file,
                        reason=f"exception:{type(e).__name__}",
                    )

                final_output_file = locals().get('output_file')
                if final_output_file and os.path.exists(final_output_file) and os.path.getsize(final_output_file) < MIN_RECORD_FILE_SIZE:
                    try:
                        os.remove(final_output_file)
                        logger.info(f"Defekte Aufnahme gelöscht: {final_output_file}")
                    except Exception:
                        pass

                if 'output_file' in locals() and output_file is not None and os.path.exists(output_file):
                    logger.info("Versuche Video-Datei zu reparieren...")
                    repaired_tmp = f"{output_file}.repaired.mp4"
                    repair_cmd = [
                        ffmpeg_path, '-y', '-i', output_file, 
                        '-c', 'copy', '-avoid_negative_ts', 'make_zero',
                        repaired_tmp
                    ]
                    try:
                        res = subprocess.run(repair_cmd, capture_output=True, timeout=60)
                        if res.returncode == 0 and os.path.isfile(repaired_tmp) and os.path.getsize(repaired_tmp) > MIN_RECORD_FILE_SIZE:
                            os.replace(repaired_tmp, output_file)
                            logger.info("Video-Datei erfolgreich repariert")
                        else:
                            logger.warning(
                                "Video-Reparatur fehlgeschlagen (exit %s): %s",
                                res.returncode,
                                res.stderr[:500] if res.stderr else "Unbekannter Fehler",
                            )
                            if os.path.exists(repaired_tmp):
                                try:
                                    os.remove(repaired_tmp)
                                except OSError:
                                    pass
                    except subprocess.TimeoutExpired:
                        logger.warning("Video-Reparatur per FFmpeg nach 60s Timeout abgebrochen")
                        if os.path.exists(repaired_tmp):
                            try:
                                os.remove(repaired_tmp)
                            except OSError:
                                pass
                    except Exception as repair_error:
                        if os.path.exists(repaired_tmp):
                            try:
                                os.remove(repaired_tmp)
                            except OSError:
                                pass
                        logger.error(f"Reparatur fehlgeschlagen: {repair_error!s}")

            if output_file:
                clear_current_recording(recordings_dir, os.path.basename(output_file), source_channel=source_channel)
            if shutdown_requested:
                logger.info("Aufnahme-Loop unterbrochen.")
                return
            restart_grace_until = mark_restart_grace()
            live_after = cheap_live_presence(source_channel, get_source_config(source_channel))
            next_delay = post_recording_retry_delay(
                live_after,
                recording_failures=recording_failures,
                session_started_at=last_session_started_at,
            )
            logger.info(f"Warte {next_delay}s vor nächster Aufnahme...")
            interruptible_sleep(
                next_delay,
                wake_check=make_live_wake_check(source_channel, get_source_config(source_channel)),
            )
    except (KeyboardInterrupt, SystemExit):
        logger.info("Aufnahme-Loop unterbrochen.")
    finally:
        stop_process(process)
        stop_process(source_process)

def test_video_file(file_path):
    """Testet ob eine Video-Datei abspielbar ist."""
    try:
        ffmpeg_path = find_ffmpeg()
        # Probe only the first ~15s — full decode of large VODs timed out at 30s
        # and produced false "corrupt" warnings in production logs.
        test_cmd = [
            ffmpeg_path, '-v', 'error',
            '-t', '15',
            '-i', file_path,
            '-f', 'null', '-'
        ]
        result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=90)
        if result.returncode == 0:
            logger.info(f"Video-Datei ist gültig: {file_path}")
            return True
        else:
            logger.error(f"Video-Datei ist beschädigt: {file_path}")
            logger.error(f"FFmpeg Fehler: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        logger.warning(f"Video-Validierung Timeout (behandle als OK): {file_path}")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Testen der Video-Datei: {e!s}")
        return False

channel_procs = {}
channel_modes = {}
shutdown_requested = False


def stop_channel_process(proc, channel, timeout=5):
    """Terminate a channel worker; SIGKILL if it ignores SIGTERM (prevents Discord zombie pings)."""
    if not proc:
        return
    try:
        if not proc.is_alive():
            return
        proc.terminate()
        proc.join(timeout=timeout)
        if proc.is_alive():
            logger.warning(
                "Channel-Prozess %s reagiert nicht auf SIGTERM — sende SIGKILL",
                channel,
            )
            proc.kill()
            proc.join(timeout=5)
    except Exception as exc:
        logger.error("Channel-Prozess %s konnte nicht gestoppt werden: %s", channel, exc)


def stop_channel_and_release_lock(recordings_dir, channel, proc, timeout=5):
    """Stop the channel worker *before* releasing its flock.

    Releasing first lets a new recorder process acquire the same channel while
    the old FFmpeg child is still writing, producing duplicate recordings.
    """
    try:
        stop_channel_process(proc, channel, timeout=timeout)
    finally:
        try:
            release_held_channel_lock(recordings_dir, channel)
        except Exception as exc:
            # Must not mask an exception raised by stop_channel_process().
            logger.warning(
                "Channel-Lock fuer %s konnte nicht freigegeben werden: %s", channel, exc
            )


def request_shutdown(signum=None, frame=None):
    global shutdown_requested
    shutdown_requested = True

def interruptible_sleep(seconds, wake_check=None):
    """Sleep up to `seconds`, returning True if wake_check fired (e.g. EventSub live)."""
    try:
        delay = max(0.0, float(seconds or 0))
    except (TypeError, ValueError):
        delay = float(RETRY_DELAY)
    deadline = time.time() + delay
    while not shutdown_requested and time.time() < deadline:
        if wake_check:
            try:
                if wake_check():
                    return True
            except Exception:
                pass
        time.sleep(min(1.0, max(0.0, deadline - time.time())))
    return False

def signal_handler(sig, frame):
    """Behandelt das Herunterfahren des Bots."""
    logger.info("Fahre Stream-Recorder herunter...")
    # Explizit alle Kind-Prozesse beenden
    for proc in channel_procs.values():
        if proc and proc.is_alive():
            proc.terminate()
    # Wir warten nicht hier, sondern lassen den finally-Block aufräumen
    # sys.exit löst SystemExit aus, was den finally-Block aktiviert
    sys.exit(0)


def monitor_stream_live_only(source_channel, archive_channel, conf=None):
    """
    Monitors a streamer for Discord live notifications and Tube live-watch status
    WITHOUT recording any video/audio to disk.
    """
    conf = conf or get_source_config(source_channel)
    is_live_now = False
    logger.info(f"Live-Only Monitor aktiv für {source_channel} (Discord-Alert + Live-Stream, keine Aufnahme).")

    while not shutdown_requested:
        try:
            current_conf = get_source_config(source_channel)
            if not current_conf:
                logger.info(f"Kanal {source_channel} entfernt. Monitor beendet.")
                break
            if current_conf.get("record"):
                logger.info(f"Aufnahme für {source_channel} aktiviert. Wechsle in Aufnahmemodus.")
                break

            presence = cheap_live_presence(source_channel, current_conf)
            stream_live = presence.get("live") is True

            if stream_live and not is_live_now:
                is_live_now = True
                status = presence.get("status") or {}
                meta = {
                    "title": status.get("title") or "",
                    "category": status.get("category") or "",
                    "started_at": status.get("started_at") or "",
                    "platform": current_conf.get("platform", "Twitch"),
                }
                logger.info(f"Streamer {source_channel} ist JETZT LIVE (Live-Only). Sende Discord-Notification...")
                try:
                    notify_discord_streamer_online(archive_channel, source_channel, meta, current_conf, time.time())
                except Exception as exc:
                    logger.error(f"Discord Live-Notify fehlgeschlagen für {source_channel}: {exc}")
                try:
                    conn = database.get_db()
                    conn.execute("UPDATE channels SET live=1 WHERE name=?", (source_channel,))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
            elif not stream_live and is_live_now:
                is_live_now = False
                logger.info(f"Streamer {source_channel} ist offline (Live-Only).")
                try:
                    clear_discord_live_session(archive_channel or source_channel)
                except Exception:
                    pass
                try:
                    conn = database.get_db()
                    conn.execute("UPDATE channels SET live=0 WHERE name=?", (source_channel,))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

            interruptible_sleep(CHECK_INTERVAL)
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as exc:
            logger.error(f"Fehler im Live-Only Monitor für {source_channel}: {exc}")
            interruptible_sleep(CHECK_INTERVAL)

def run_channel(source_channel):
    """Startet Aufnahme und Chat-Prozess für einen Channel."""
    drop_inherited_instance_lock()
    # Signal Handler für sauberes Beenden (verhindert Zombie-FFmpeg)
    signal.signal(signal.SIGTERM, request_shutdown)

    conf = get_source_config(source_channel)
    archive_channel = conf.get("archive_name") or source_channel
    recordings_dir = os.path.join(RECORDINGS_BASE_DIR, archive_channel)
    os.makedirs(recordings_dir, exist_ok=True)
    chat_text_file = overlay_chat_path(recordings_dir, source_channel)
    viewer_text_file = overlay_viewer_path(recordings_dir, source_channel)

    logger.info("============================================")
    logger.info("Starte Stream-Recorder mit folgender Konfiguration:")
    logger.info(f"Quelle: {source_channel}")
    logger.info(f"Plattform: {conf.get('platform', 'twitch')}")
    logger.info(f"Quell-URL: {source_public_url(source_channel, conf)}")
    logger.info(f"Archiv: {archive_channel}")
    logger.info(f"Aufnahme-Verzeichnis: {recordings_dir}")
    logger.info(f"Debug-Modus: {'AN' if DEBUG else 'AUS'}")
    logger.info(f"Log-Verzeichnis: {LOG_DIR}")
    logger.info(f"Chat-Verlauf: {MAX_CHAT_MESSAGES} Nachrichten")
    logger.info("============================================")
    
    try:
        # Konfiguration prüfen: Aufnahme oder nur Chat?
        conf = get_source_config(source_channel)
        should_record = conf and conf.get('record')

        if should_record:
            chat_target = youtube_chat_process if conf.get("platform") == "youtube" else chat_process
            chat_proc = Process(target=chat_target, args=(source_channel, chat_text_file))
            chat_proc.daemon = True
            chat_proc.start()
            
            viewer_thread = threading.Thread(target=viewer_process, args=(source_channel, viewer_text_file))
            viewer_thread.daemon = True
            viewer_thread.start()
            record_stream(source_channel, chat_text_file, recordings_dir)
        else:
            logger.info(f"Kanal {source_channel} läuft im Live-Only Modus (Discord-Alert + Live-Stream, keine Aufnahme).")
            monitor_stream_live_only(source_channel, archive_channel, conf)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Recorder wird beendet")
    finally:
        try:
            if 'chat_proc' in locals() and chat_proc.is_alive():
                chat_proc.terminate()
                chat_proc.join(timeout=5)
        except Exception:
            pass
        # Only remove this source's overlay files — siblings may still need theirs.
        for path in (chat_text_file, viewer_text_file):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        # Channel flock is held by the parent process for the child lifetime.
        logger.info("Stream-Recorder beendet.")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    acquire_instance_lock()

    # Phase 1: Reconcile orphaned recordings and stale metadata across all channels on startup
    try:
        startup_channels = load_all_channels()
        logger.info(f"Reconciliation beim Daemon-Start: Prüfe {len(startup_channels)} Kanäle...")
        for ch in startup_channels:
            try:
                ch_conf = get_source_config(ch)
                ch_archive = ch_conf.get("archive_name") or ch
                ch_dir = os.path.join(RECORDINGS_BASE_DIR, ch_archive)
                if os.path.isdir(ch_dir):
                    cleanup_stale_recording_metadata(ch_dir, source_channel=ch)
            except Exception as ch_err:
                logger.debug(f"Reconciliation-Prüfung für {ch} übersprungen: {ch_err}")
    except Exception as rec_err:
        logger.warning(f"Reconciliation-Startroutine Fehler: {rec_err}")

    channel_procs = {}
    channel_modes = {}

    try:
        while True:
            channels = load_all_channels()
            
            # Start / refresh channel processes
            for channel in channels:
                conf = get_source_config(channel)
                archive_channel = conf.get("archive_name") or channel
                recordings_dir = os.path.join(RECORDINGS_BASE_DIR, archive_channel)
                if not conf:
                    continue
                mode = "record" if conf.get('record') else "live_only"

                # Mode change or dead process handling
                if channel in channel_procs:
                    proc = channel_procs[channel]
                    old_mode = channel_modes.get(channel)
                    if proc.is_alive() and old_mode == mode:
                        continue
                    stop_channel_and_release_lock(recordings_dir, channel, proc)
                    channel_procs.pop(channel, None)
                    channel_modes.pop(channel, None)

                alive_count = sum(1 for p in channel_procs.values() if p and p.is_alive())
                if alive_count >= MAX_CONCURRENT_RECORDINGS:
                    throttled_log(
                        "warning",
                        "max-concurrent-recordings",
                        f"Concurrent recording cap reached ({alive_count}/{MAX_CONCURRENT_RECORDINGS}); "
                        f"skipping new start for {channel}.",
                        interval=60,
                    )
                    continue

                # Atomic flock acquire before Process.start (closes TOCTOU race).
                lock_fd = try_acquire_channel_lock(recordings_dir, channel, pid=os.getpid())
                if lock_fd is None:
                    throttled_log(
                        "info",
                        f"channel-lock-{channel}",
                        f"Channel {channel} ist bereits durch eine andere Aufnahme gesperrt. Starte keinen zweiten Prozess.",
                        interval=300,
                    )
                    continue

                proc = Process(target=run_channel, args=(channel,))
                proc.start()
                try:
                    update_channel_lock_pid(recordings_dir, proc.pid, channel)
                except Exception:
                    logger.warning(f"Konnte Lock-PID für Channel {channel} nach Start nicht schreiben.")
                channel_procs[channel] = proc
                channel_modes[channel] = mode
                logger.info(f"Channel-Prozess gestartet: {channel} (Modus: {mode})")

            # Stop removed channels
            for channel in list(channel_procs.keys()):
                if channel not in channels:
                    proc = channel_procs.pop(channel)
                    channel_modes.pop(channel, None)
                    conf = get_source_config(channel)
                    archive_channel = conf.get("archive_name") or channel
                    recordings_dir = os.path.join(RECORDINGS_BASE_DIR, archive_channel)
                    stop_channel_and_release_lock(recordings_dir, channel, proc)
                    logger.info(f"Channel-Prozess beendet: {channel}")
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Recorder wird durch Benutzer beendet")
    finally:
        for channel, proc in list(channel_procs.items()):
            conf = get_source_config(channel) or {}
            archive_channel = conf.get("archive_name") or channel
            recordings_dir = os.path.join(RECORDINGS_BASE_DIR, archive_channel)
            stop_channel_and_release_lock(recordings_dir, channel, proc)
        
        for proc in channel_procs.values():
            if proc and proc.is_alive():
                proc.join(timeout=1)
        logger.info("Stream-Recorder beendet.")
# TWITCH_CLIENT_ID und TWITCH_ACCESS_TOKEN bekommst du im Twitch Developer Portal.
# Siehe Anleitung oben.
