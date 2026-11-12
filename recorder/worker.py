import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from recorder import clip_engine
from core import config
from core import database
from core.database import get_db, init_db
from core.mp4_utils import (
    BROWSER_MP4_MOVFLAGS,
    finalize_if_fragmented,
    is_archive_mp4_filename,
    is_transient_media_filename,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - WORKER - %(message)s', force=True)
logger = logging.getLogger("worker")


class TaskDeferred(Exception):
    """Requeue later without consuming an attempt (e.g. recording still active)."""

    def __init__(self, message, delay_sec=60):
        super().__init__(str(message or "deferred"))
        self.delay_sec = max(1, int(delay_sec or 60))


STORAGE_ALERT_THRESHOLD_PCT = float(
    os.environ.get(
        "STORAGE_ALERT_THRESHOLD_PCT",
        os.environ.get("STREAM_RECORDER_STORAGE_PAUSE_PCT", "90"),
    )
)
STORAGE_ALERT_THRESHOLD_FREE_GB = float(
    os.environ.get(
        "STORAGE_ALERT_THRESHOLD_FREE_GB",
        os.environ.get("STREAM_RECORDER_STORAGE_PAUSE_FREE_GB", "20"),
    )
)
STORAGE_ALERT_CHECK_INTERVAL_SEC = int(os.environ.get("STORAGE_ALERT_CHECK_INTERVAL_SEC", "60"))
STORAGE_ALERT_COOLDOWN_SEC = int(os.environ.get("STORAGE_ALERT_COOLDOWN_SEC", "21600"))
STORAGE_ALERT_WEBHOOK_URL = os.environ.get("STORAGE_ALERT_WEBHOOK_URL", "").strip()
STORAGE_ALERT_STATE_FILE = os.path.join(config.META_DIR, "storage_alert.json")
STORAGE_PAUSE_FILE = os.path.join(config.META_DIR, "storage_pause.json")
TASK_CLEANUP_INTERVAL_SEC = int(os.environ.get("TASK_CLEANUP_INTERVAL_SEC", "3600"))
TASK_RETENTION_SEC = int(os.environ.get("TASK_RETENTION_SEC", str(7 * 24 * 3600)))
TASK_STALE_SEC = int(os.environ.get("TASK_STALE_SEC", "21600"))
THUMB_LOCK_STALE_SEC = int(os.environ.get("THUMB_LOCK_STALE_SEC", "3600"))
THUMB_LOCK_CLEANUP_INTERVAL_SEC = int(os.environ.get("THUMB_LOCK_CLEANUP_INTERVAL_SEC", "1800"))
THUMB_BACKFILL_INTERVAL_SEC = int(os.environ.get("THUMB_BACKFILL_INTERVAL_SEC", "300"))
THUMB_BACKFILL_MAX_PER_RUN = int(os.environ.get("THUMB_BACKFILL_MAX_PER_RUN", "20"))
# A current-recording pointer is considered live only while its media file is fresh.
ACTIVE_RECORDING_FRESH_SEC = int(os.environ.get("WORKER_ACTIVE_RECORDING_FRESH_SEC", "60"))
WORKER_ID = f"{os.uname().nodename}:{os.getpid()}" if hasattr(os, "uname") else f"local:{os.getpid()}"
# Parallel pools (env):
#   WORKER_THUMB_THREADS       — IO pool for thumbnail (+ delete/empty); default 2
#   WORKER_REMUX_THREADS       — CPU/media pool for remux_vod / remux_all; default 1
#   WORKER_TRANSCRIBE_THREADS  — ASR pool for Whisper; default 1 (keep low if RAM-heavy)
#   WORKER_STORAGE_THREADS     — network pool for cloud uploads/downloads; default 1
WORKER_THUMB_THREADS = max(1, int(os.environ.get("WORKER_THUMB_THREADS", "2")))
WORKER_REMUX_THREADS = max(1, int(os.environ.get("WORKER_REMUX_THREADS", "1")))
WORKER_TRANSCRIBE_THREADS = max(1, int(os.environ.get("WORKER_TRANSCRIBE_THREADS", "1")))
WORKER_STORAGE_THREADS = max(1, int(os.environ.get("WORKER_STORAGE_THREADS", "1")))
IO_TASK_TYPES = frozenset({
    "thumbnail", "delete_folder", "empty_folder",
})
REMUX_TASK_TYPES = frozenset({"remux_vod", "remux_all"})
ASR_TASK_TYPES = frozenset({"transcribe"})
STORAGE_TASK_TYPES = frozenset({
    "storage_upload",
    "storage_sync",
    "storage_delete",
    "storage_restore",
    "storage_check",
    "storage_cleanup",
})
TASK_MAX_ATTEMPTS = {
    "thumbnail": int(os.environ.get("TASK_MAX_ATTEMPTS_THUMBNAIL", "3")),
    "delete_folder": int(os.environ.get("TASK_MAX_ATTEMPTS_DELETE_FOLDER", "2")),
    "empty_folder": int(os.environ.get("TASK_MAX_ATTEMPTS_EMPTY_FOLDER", "2")),
    "transcribe": int(os.environ.get("TASK_MAX_ATTEMPTS_TRANSCRIBE", "1")),
    "remux_all": int(os.environ.get("TASK_MAX_ATTEMPTS_REMUX_ALL", "1")),
    "remux_vod": int(os.environ.get("TASK_MAX_ATTEMPTS_REMUX_VOD", "2")),
    "storage_upload": int(os.environ.get("TASK_MAX_ATTEMPTS_STORAGE_UPLOAD", "3")),
    "storage_sync": int(os.environ.get("TASK_MAX_ATTEMPTS_STORAGE_SYNC", "1")),
    "storage_delete": int(os.environ.get("TASK_MAX_ATTEMPTS_STORAGE_DELETE", "3")),
    "storage_restore": int(os.environ.get("TASK_MAX_ATTEMPTS_STORAGE_RESTORE", "3")),
    "storage_check": int(os.environ.get("TASK_MAX_ATTEMPTS_STORAGE_CHECK", "1")),
    "storage_cleanup": int(os.environ.get("TASK_MAX_ATTEMPTS_STORAGE_CLEANUP", "1")),
}
REMUX_FILE_TIMEOUT_SEC = int(os.environ.get("REMUX_FILE_TIMEOUT_SEC", "7200"))

_INFLIGHT = {"io": 0, "remux": 0, "asr": 0, "storage": 0}
_INFLIGHT_LOCK = threading.Lock()
_POOL_LIMITS = {
    "io": WORKER_THUMB_THREADS,
    "remux": WORKER_REMUX_THREADS,
    "asr": WORKER_TRANSCRIBE_THREADS,
    "storage": WORKER_STORAGE_THREADS,
}
_LAST_STORAGE_CHECK = 0.0
_LAST_TASK_CLEANUP = 0.0
_LAST_THUMB_LOCK_CLEANUP = 0.0
_LAST_THUMB_BACKFILL = 0.0
MEDIA_SCAN_SKIP_DIRS = {
    ".hls",
    ".hls_live",
    ".meta",
    ".ruff_cache",
    ".thumbs",
    "__pycache__",
    "files",
    "node_modules",
    "scripts",
    "static",
    "tests",
    "venv",
}

def _format_gb(num_bytes):
    return f"{num_bytes / (1024 ** 3):.1f} GB"

def _load_storage_alert_state():
    from core.database import get_system_state
    return get_system_state("storage_alert", default={})

def _write_private_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=os.path.dirname(path))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
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

def _save_storage_alert_state(data):
    try:
        from core.database import set_system_state
        set_system_state("storage_alert", data)
    except Exception as exc:
        logging.error("Konnte Speicherwarnungs-Status nicht schreiben: %s", exc)

def _post_storage_alert(message):
    if not STORAGE_ALERT_WEBHOOK_URL:
        return False
    payload = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        STORAGE_ALERT_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception as exc:
        logging.error("Konnte Speicherwarnung nicht senden: %s", exc)
        return False

def check_storage_alert(force=False):
    global _LAST_STORAGE_CHECK
    now = time.time()
    if not force and (now - _LAST_STORAGE_CHECK) < STORAGE_ALERT_CHECK_INTERVAL_SEC:
        return
    _LAST_STORAGE_CHECK = now

    try:
        usage = shutil.disk_usage(config.BASE_DIR)
    except Exception as exc:
        logging.error("Konnte Speicherstand nicht ermitteln: %s", exc)
        return

    total = usage.total
    free = usage.free
    used = total - free
    used_pct = (used / total) * 100 if total else 0.0
    free_gb = free / (1024 ** 3)
    critical = used_pct >= STORAGE_ALERT_THRESHOLD_PCT or free_gb <= STORAGE_ALERT_THRESHOLD_FREE_GB

    state = _load_storage_alert_state()
    previous_critical = bool(state.get("critical"))
    last_sent_at = float(state.get("last_sent_at", 0) or 0)

    state.update({
        "checked_at": now,
        "critical": critical,
        "used_pct": round(used_pct, 2),
        "free_bytes": free,
        "free_str": _format_gb(free),
        "total_bytes": total,
        "total_str": _format_gb(total),
        "threshold_pct": STORAGE_ALERT_THRESHOLD_PCT,
        "threshold_free_gb": STORAGE_ALERT_THRESHOLD_FREE_GB,
        "webhook_configured": bool(STORAGE_ALERT_WEBHOOK_URL),
    })

    should_send = critical and ((not previous_critical) or (now - last_sent_at >= STORAGE_ALERT_COOLDOWN_SEC))
    if should_send:
        message = (
            f"StreamRecorder Speicherwarnung: {used_pct:.1f}% belegt, "
            f"nur noch {free_gb:.1f} GB frei auf {config.BASE_DIR}."
        )
        if _post_storage_alert(message):
            state["last_sent_at"] = now
            state["last_message"] = message
            logging.warning(message)

    if previous_critical and not critical:
        state["resolved_at"] = now
        state["last_message"] = (
            f"Speicherlage wieder entspannt: {used_pct:.1f}% belegt, {free_gb:.1f} GB frei."
        )

    _save_storage_alert_state(state)
    _update_storage_pause_flag(
        critical=critical,
        used_pct=used_pct,
        free_gb=free_gb,
        now=now,
    )


def _update_storage_pause_flag(critical, used_pct, free_gb, now):
    """Write/clear .meta/storage_pause.json so the recorder can honor disk pause."""
    payload = {
        "paused": bool(critical),
        "critical": bool(critical),
        "checked_at": now,
        "used_pct": round(float(used_pct), 2),
        "free_gb": round(float(free_gb), 2),
        "threshold_pct": STORAGE_ALERT_THRESHOLD_PCT,
        "threshold_free_gb": STORAGE_ALERT_THRESHOLD_FREE_GB,
    }
    try:
        if critical:
            _write_private_json(STORAGE_PAUSE_FILE, payload)
            if not getattr(_update_storage_pause_flag, "_last_paused", False):
                logging.warning(
                    "STORAGE_PAUSE gesetzt (%.1f%% belegt, %.1f GB frei) → %s",
                    used_pct,
                    free_gb,
                    STORAGE_PAUSE_FILE,
                )
            _update_storage_pause_flag._last_paused = True
        else:
            # Keep a resolved marker so operators can see last clear, but paused=false.
            payload["resolved_at"] = now
            _write_private_json(STORAGE_PAUSE_FILE, payload)
            if getattr(_update_storage_pause_flag, "_last_paused", False):
                logging.info(
                    "STORAGE_PAUSE aufgehoben (%.1f%% belegt, %.1f GB frei)",
                    used_pct,
                    free_gb,
                )
            _update_storage_pause_flag._last_paused = False
    except Exception as exc:
        logging.error("Konnte STORAGE_PAUSE Flag nicht schreiben: %s", exc)

def cleanup_old_tasks(force=False):
    global _LAST_TASK_CLEANUP
    now = time.time()
    if not force and (now - _LAST_TASK_CLEANUP) < TASK_CLEANUP_INTERVAL_SEC:
        return
    _LAST_TASK_CLEANUP = now
    cutoff = now - TASK_RETENTION_SEC
    conn = None
    try:
        conn = get_db()
        conn.execute(
            "DELETE FROM tasks WHERE status IN ('completed', 'failed') AND updated_at < ?",
            (cutoff,),
        )
        conn.commit()
    except Exception as exc:
        logging.error("Konnte alte Tasks nicht bereinigen: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def thumbnail_lock_path(thumb_path):
    base, _ = os.path.splitext(str(thumb_path or ""))
    return f"{base}.lock" if base else ""


def remove_thumbnail_lock(thumb_path):
    lock_path = thumbnail_lock_path(thumb_path)
    if not lock_path:
        return
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError:
        pass


def cleanup_stale_thumbnail_locks(force=False):
    global _LAST_THUMB_LOCK_CLEANUP
    now = time.time()
    if not force and (now - _LAST_THUMB_LOCK_CLEANUP) < THUMB_LOCK_CLEANUP_INTERVAL_SEC:
        return
    _LAST_THUMB_LOCK_CLEANUP = now
    root = config.THUMB_ROOT
    if not os.path.isdir(root):
        return
    cutoff = now - max(60, THUMB_LOCK_STALE_SEC)
    removed = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".lock"):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    if removed:
        logging.info("Stale Thumbnail-Locks entfernt: %s", removed)


def thumbnail_file_ready(thumb_path):
    try:
        return os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0
    except OSError:
        return False


def thumbnail_path_for_media(media_path):
    try:
        rel = os.path.relpath(media_path, config.BASE_DIR)
    except (TypeError, ValueError):
        return None
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    parts = rel.split(os.sep)
    if not parts or parts[0] in MEDIA_SCAN_SKIP_DIRS or parts[0].startswith("."):
        return None
    streamer = parts[0]
    filename = os.path.basename(media_path)
    if not is_archive_mp4_filename(filename):
        return None
    return os.path.join(config.THUMB_ROOT, streamer, f"{os.path.splitext(filename)[0]}.jpg")


def enqueue_thumbnail_if_missing(media_path, discord_notify_ready=False):
    _ = discord_notify_ready
    if is_transient_media_filename(media_path):
        return False
    thumb_path = thumbnail_path_for_media(media_path)
    if not thumb_path:
        return False
    try:
        if not os.path.isfile(media_path) or os.path.getsize(media_path) <= 0:
            return False
    except OSError:
        return False

    streamer = os.path.basename(os.path.dirname(os.path.abspath(media_path)))
    filename = os.path.basename(media_path)

    if thumbnail_file_ready(thumb_path):
        return False

    lock_path = thumbnail_lock_path(thumb_path)
    if os.path.exists(lock_path):
        try:
            if time.time() - os.path.getmtime(lock_path) <= THUMB_LOCK_STALE_SEC:
                return False
            os.remove(lock_path)
        except OSError:
            return False

    try:
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as handle:
            handle.write(f"{time.time()} {WORKER_ID} {media_path}\n")
        payload = {"input_path": media_path, "thumb_path": thumb_path}
        if streamer and filename:
            payload["streamer"] = streamer
            payload["filename"] = filename
        database.add_task("thumbnail", payload)
        logging.info("Fehlendes Thumbnail eingereiht: %s", media_path)
        return True
    except Exception as exc:
        logging.error("Konnte fehlendes Thumbnail nicht einreihen: %s: %s", media_path, exc)
        try:
            os.remove(lock_path)
        except OSError:
            pass
        return False


def backfill_missing_thumbnails(force=False):
    global _LAST_THUMB_BACKFILL
    now = time.time()
    if not force and (now - _LAST_THUMB_BACKFILL) < THUMB_BACKFILL_INTERVAL_SEC:
        return 0
    _LAST_THUMB_BACKFILL = now
    if not os.path.isdir(config.BASE_DIR):
        return 0

    queued = 0
    for dirpath, dirnames, filenames in os.walk(config.BASE_DIR):
        dirnames[:] = [
            name for name in dirnames
            if name not in MEDIA_SCAN_SKIP_DIRS and not name.startswith(".")
        ]
        for filename in filenames:
            if not is_archive_mp4_filename(filename):
                continue
            if enqueue_thumbnail_if_missing(os.path.join(dirpath, filename)):
                queued += 1
                if queued >= max(1, THUMB_BACKFILL_MAX_PER_RUN):
                    break
        if queued >= max(1, THUMB_BACKFILL_MAX_PER_RUN):
            break
    return queued

def media_duration(input_path, http_headers=None):
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
        ]
        if http_headers:
            from recorder import clip_engine
            cmd.extend(clip_engine._ffmpeg_header_args(http_headers))
        cmd.append(input_path)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode != 0:
            return 0.0
        return max(0.0, float(result.stdout.strip() or 0))
    except subprocess.TimeoutExpired:
        logger.warning("ffprobe timed out after 45s on %s", input_path)
        return 0.0
    except Exception as exc:
        logger.debug("ffprobe failed on %s: %s", input_path, exc)
        return 0.0


def thumbnail_candidate_times(duration):
    if duration <= 0:
        return [30, 120, 300]
    fractions = (0.50, 0.62, 0.38, 0.75, 0.25, 0.88, 0.12)
    times = []
    for fraction in fractions:
        ts = max(10.0, min(duration - 5.0, duration * fraction))
        if duration > 1800:
            ts = max(ts, 300.0)
        if 0 < ts < duration:
            times.append(round(ts, 2))
    deduped = []
    for ts in times:
        if all(abs(ts - existing) > 2 for existing in deduped):
            deduped.append(ts)
    return deduped or [min(30.0, max(1.0, duration / 2))]


def read_thumbnail_frame(input_path, timestamp, width=320, height=180, http_headers=None):
    cmd = [
        "ffmpeg",
        "-v", "error",
    ]
    if http_headers:
        from recorder import clip_engine
        cmd.extend(clip_engine._ffmpeg_header_args(http_headers))
    cmd.extend([
        "-ss", f"{timestamp:.2f}",
        "-i", input_path,
        "-frames:v", "1",
        "-threads", "1",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "pipe:1",
    ])
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    except Exception:
        return None
    expected = width * height * 3
    if result.returncode != 0 or len(result.stdout) != expected:
        return None
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape((height, width, 3))


def score_thumbnail_frame(frame):
    if frame is None:
        return -1.0
    pixels = frame.astype(np.float32)
    mean = pixels.mean(axis=(0, 1))
    brightness = float(pixels.mean())
    contrast = float(pixels.std())
    gray = pixels.mean(axis=2)
    edge = float(np.abs(np.diff(gray, axis=0)).mean() + np.abs(np.diff(gray, axis=1)).mean())
    channel_spread = float(mean.max() - mean.min())

    dark_penalty = max(0.0, 35.0 - brightness) * 2.5
    bright_penalty = max(0.0, brightness - 225.0)
    flat_penalty = max(0.0, 32.0 - contrast) * 1.8

    # Twitch ad break frames are often smooth blue/purple gradients.
    blue_purple = mean[2] > mean[0] + 18 and mean[2] > mean[1] + 10
    ad_like_penalty = 95.0 if blue_purple and edge < 9.0 and contrast < 70.0 else 0.0

    return (contrast * 1.7) + (edge * 8.0) + (channel_spread * 0.35) - dark_penalty - bright_penalty - flat_penalty - ad_like_penalty


def write_thumbnail(input_path, thumb_path, timestamp, http_headers=None):
    cmd = [
        "ffmpeg",
        "-y",
    ]
    if http_headers:
        from recorder import clip_engine
        cmd.extend(clip_engine._ffmpeg_header_args(http_headers))
    cmd.extend([
        "-ss", f"{timestamp:.2f}",
        "-i", input_path,
        "-frames:v", "1",
        "-threads", "1",
        "-q:v", "3",
        "-vf", "scale=480:270:force_original_aspect_ratio=decrease,pad=480:270:(ow-iw)/2:(oh-ih)/2",
        thumb_path,
    ])
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    return result.returncode == 0 and thumbnail_file_ready(thumb_path)


def _safe_path_under(path, *roots):
    """Return realpath if it resolves under one of the allowlisted roots."""
    if not path:
        return None
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    for root in roots:
        if not root:
            continue
        try:
            root_real = os.path.realpath(root)
        except OSError:
            continue
        if real == root_real or real.startswith(root_real + os.sep):
            return real
    return None


def _resolve_thumbnail_source(payload, input_path):
    """Resolve local media source for thumbnail generation."""
    source = input_path
    if source:
        try:
            if os.path.isfile(source) and os.path.getsize(source) > 0:
                return source, None
        except OSError:
            pass
    return source, None


def process_thumbnail(payload):
    raw_input = payload.get("input_path")
    raw_thumb = payload.get("thumb_path")
    # Remux/part temps are never archive VODs; finish quietly so old queued tasks
    # do not keep filling "Letzte Task-Fehler".
    if raw_input and is_transient_media_filename(raw_input):
        logging.info("Thumbnail übersprungen (Temp-Datei): %s", raw_input)
        if raw_thumb:
            remove_thumbnail_lock(_safe_path_under(raw_thumb, config.THUMB_ROOT) or raw_thumb)
        return True
    input_path = _safe_path_under(raw_input, config.BASE_DIR) if raw_input else None
    thumb_path = _safe_path_under(raw_thumb, config.THUMB_ROOT)
    if thumb_path and thumbnail_file_ready(thumb_path):
        remove_thumbnail_lock(thumb_path)
        return True
    if raw_input and not input_path:
        logging.error("Unsicherer Thumbnail-Input abgelehnt: %s", raw_input)
        raise RuntimeError("thumbnail unsafe input path")
    if raw_thumb and not thumb_path:
        logging.error("Unsicherer Thumbnail-Zielpfad abgelehnt: %s", raw_thumb)
        raise RuntimeError("thumbnail unsafe thumb path")
    if not thumb_path:
        return True

    filename = str((payload or {}).get("filename") or "").strip()
    if not filename and input_path:
        filename = os.path.basename(input_path)

    source, http_headers = _resolve_thumbnail_source(payload, input_path)
    if not source:
        raise RuntimeError(
            f"thumbnail input missing: {filename or os.path.basename(str(raw_input or '')) or 'unknown'}"
        )

    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    try:
        if not http_headers:
            if not os.path.isfile(source):
                raise RuntimeError(
                    f"thumbnail input missing: {filename or os.path.basename(str(source))}"
                )
            try:
                if os.path.getsize(source) <= 0:
                    raise RuntimeError(f"thumbnail input empty: {os.path.basename(source)}")
            except OSError as exc:
                raise RuntimeError(f"thumbnail input unreadable: {os.path.basename(source)}") from exc
        duration = media_duration(source, http_headers=http_headers)
        candidates = thumbnail_candidate_times(duration)
        scored = []
        for timestamp in candidates:
            frame = read_thumbnail_frame(source, timestamp, http_headers=http_headers)
            scored.append((score_thumbnail_frame(frame), timestamp))
        scored.sort(reverse=True)
        for _, timestamp in scored:
            if write_thumbnail(source, thumb_path, timestamp, http_headers=http_headers):
                logging.info("Thumbnail erstellt: %s bei %.1fs", thumb_path, timestamp)
                return True
        fallback = min(30.0, max(1.0, duration / 2 if duration else 30.0))
        if write_thumbnail(source, thumb_path, fallback, http_headers=http_headers):
            logging.info("Thumbnail erstellt (fallback): %s bei %.1fs", thumb_path, fallback)
            return True
        raise RuntimeError(f"thumbnail ffmpeg failed: {filename or os.path.basename(str(source))}")
    except RuntimeError:
        raise
    except Exception as e:
        logging.error(f"Thumbnail failed: {e}")
        raise RuntimeError(f"thumbnail failed: {e}") from e
    finally:
        remove_thumbnail_lock(thumb_path)

def process_delete_folder(payload):
    folder_path = payload.get("path")
    streamer = str(payload.get("streamer") or "").strip()
    if not folder_path and streamer:
        folder_path = os.path.join(config.BASE_DIR, streamer)
    if not folder_path:
        return False
    if not _is_direct_child(config.BASE_DIR, folder_path):
        logging.error("Unsicherer delete_folder Pfad abgelehnt: %s", folder_path)
        return False
    if config.is_reserved_archive_name(os.path.basename(os.path.normpath(folder_path))):
        logging.error("delete_folder rejected for reserved directory: %s", folder_path)
        return False

    if not streamer:
        streamer = os.path.basename(os.path.realpath(folder_path))

    if _folder_has_active_recording(folder_path) or _folder_has_active_remux(folder_path):
        raise TaskDeferred(
            f"Ordner wird noch beschrieben: {folder_path}", delay_sec=120
        )

    logging.info("Löschauftrag für: %s", folder_path)
    # Wartezeit, damit der Recorder-Prozess Zeit hat, sauber zu stoppen
    time.sleep(8)

    def _rm_tree(path):
        if not path or not os.path.exists(path):
            return True
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            return True
        except Exception as exc:
            logging.warning("Konnte %s nicht löschen: %s", path, exc)
            return False

    ok = True
    for i in range(5):
        if not os.path.exists(folder_path):
            break
        try:
            # Full purge — including chat_logs. "Ordner Löschen" must remove the
            # channel from disk; leaving chat_logs kept ghost cards on the dashboard.
            if not _rm_tree(folder_path):
                # Fallback: delete children then rmdir
                for item in os.listdir(folder_path):
                    _rm_tree(os.path.join(folder_path, item))
                try:
                    os.rmdir(folder_path)
                except OSError as exc:
                    logging.warning("Löschversuch %s fehlgeschlagen: %s", i + 1, exc)
                    ok = False
                    time.sleep(2)
                    continue
            logging.info("Ordner erfolgreich gelöscht: %s", folder_path)
            ok = True
            break
        except Exception as e:
            logging.warning("Löschversuch %s fehlgeschlagen: %s", i + 1, e)
            ok = False
            time.sleep(2)
    else:
        ok = not os.path.exists(folder_path)

    # Sidecar caches / thumbs for this archive (including trailing underscores)
    for root in (
        getattr(config, "THUMB_ROOT", None),
        getattr(config, "HLS_ROOT", None),
        os.path.join(config.BASE_DIR, ".vodcache"),
    ):
        if not root or not os.path.exists(root):
            continue
        if streamer:
            for cand in (streamer, streamer + "_", streamer + "__", streamer.rstrip("_")):
                extra = os.path.join(root, cand)
                if extra and _is_direct_child(root, extra):
                    _rm_tree(extra)

    # Clean profiles.json and DB streamer references
    try:
        profiles_path = os.path.join(config.BASE_DIR, ".meta", "profiles.json")
        if os.path.exists(profiles_path) and streamer:
            with open(profiles_path, "r", encoding="utf-8") as pf:
                pdata = json.load(pf)
            del_keys = [k for k in pdata.keys() if k.lower().strip() == streamer.lower().strip() or k.lower().strip().rstrip("_") == streamer.lower().strip().rstrip("_")]
            for dk in del_keys:
                del pdata[dk]
            with open(profiles_path, "w", encoding="utf-8") as pf:
                json.dump(pdata, pf, indent=2, ensure_ascii=False)
    except Exception as exc:
        logging.warning("profiles.json Bereinigung fuer %s fehlgeschlagen: %s", streamer, exc)

    try:
        conn = database.get_db()
        for tbl, cl in [
            ("vod_access_defaults", "streamer"),
            ("user_vod_access", "streamer"),
            ("role_vod_access", "streamer"),
        ]:
            conn.execute(f"DELETE FROM {tbl} WHERE LOWER({cl})=?", (streamer.lower().strip(),))
        conn.commit()
        conn.close()
    except Exception as exc:
        logging.warning("DB-Tabellen Bereinigung fuer %s fehlgeschlagen: %s", streamer, exc)

    return ok and not os.path.exists(folder_path)


def _is_direct_child(parent, path):
    try:
        parent_real = os.path.realpath(parent)
        path_real = os.path.realpath(path)
        return os.path.dirname(path_real) == parent_real
    except Exception:
        return False


def _pid_is_alive(pid):
    """Best-effort PID liveness check that is safe on Windows test hosts."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) can terminate the target on Windows; fail closed.
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _folder_has_active_recording(folder_path):
    """Execution-time guard before destructive folder cleanup.

    The WebUI check only runs when the task is enqueued; a recording can start
    while the task waits in the queue.
    """
    try:
        names = os.listdir(folder_path)
    except OSError:
        return False
    now = time.time()
    for name in names:
        if not name.endswith(".part"):
            continue
        try:
            if now - os.path.getmtime(os.path.join(folder_path, name)) <= ACTIVE_RECORDING_FRESH_SEC:
                return True
        except OSError:
            continue
    for name in names:
        if name != ".recording.lock" and not (
            name.startswith(".recording.") and name.endswith(".lock")
        ):
            continue
        try:
            with open(os.path.join(folder_path, name), "r", encoding="utf-8") as handle:
                content = handle.read().strip()
        except OSError:
            continue
        if _pid_is_alive(content):
            return True
    for name in names:
        if name != ".current_recording.json" and not (
            name.startswith(".current_recording.") and name.endswith(".json")
        ):
            continue
        try:
            with open(os.path.join(folder_path, name), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        filename = os.path.basename(str((data or {}).get("filename") or ""))
        if not filename:
            continue
        for candidate in (
            os.path.join(folder_path, filename),
            os.path.join(folder_path, f"{filename}.part"),
        ):
            try:
                if time.time() - os.path.getmtime(candidate) <= ACTIVE_RECORDING_FRESH_SEC:
                    return True
            except OSError:
                continue
    return False


def _folder_has_active_remux(folder_path):
    """True while a remux lock in this archive is still fresh."""
    try:
        names = os.listdir(folder_path)
    except OSError:
        return False
    now = time.time()
    for name in names:
        if not name.endswith(".remux.lock"):
            continue
        try:
            if now - os.path.getmtime(os.path.join(folder_path, name)) <= REMUX_FILE_TIMEOUT_SEC + 300:
                return True
        except OSError:
            continue
    return False


def process_empty_folder(payload):
    folder_path = payload.get("path")
    if not folder_path or not os.path.isdir(folder_path):
        return False
    if not _is_direct_child(config.BASE_DIR, folder_path):
        logging.error("Unsicherer empty_folder Pfad abgelehnt: %s", folder_path)
        return False
    if config.is_reserved_archive_name(os.path.basename(os.path.normpath(folder_path))):
        logging.error("empty_folder rejected for reserved directory: %s", folder_path)
        return False
    if _folder_has_active_recording(folder_path) or _folder_has_active_remux(folder_path):
        raise TaskDeferred(
            f"Ordner wird noch beschrieben: {folder_path}", delay_sec=120
        )

    logging.info("Leere Ordnerinhalt: %s", folder_path)
    deleted = 0
    keep_names = {
        ".recording.lock",
        ".current_recording.json",
        ".recording_timeline.json",
        ".recording_status.json",
        "chat_overlay.txt",
        "viewer_overlay.txt",
        ".viewer_count.json",
        ".live_chat.jsonl",
    }
    keep_prefixes = (
        (".recording.", ".lock"),
        (".current_recording.", ".json"),
        (".recording_timeline.", ".json"),
        (".recording_status.", ".json"),
        ("chat_overlay.", ".txt"),
        ("viewer_overlay.", ".txt"),
        (".live_chat.", ".jsonl"),
        (".viewer_count.", ".json"),
    )

    def _keep_runtime_file(name):
        # Preserve in-progress FFmpeg output even if the active check raced.
        if name.endswith(".part"):
            return True
        if name in keep_names:
            return True
        for prefix, suffix in keep_prefixes:
            if name.startswith(prefix) and name.endswith(suffix):
                return True
        return False

    for item in os.listdir(folder_path):
        if _keep_runtime_file(item):
            continue
        item_path = os.path.join(folder_path, item)
        try:
            if os.path.isfile(item_path) or os.path.islink(item_path):
                os.unlink(item_path)
                deleted += 1
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)
                deleted += 1
        except Exception as exc:
            logging.warning("Konnte %s nicht löschen: %s", item_path, exc)

    for extra_path in payload.get("extra_paths", []):
        if not extra_path or not os.path.exists(extra_path):
            continue
        allowed_parent = None
        if _is_direct_child(config.THUMB_ROOT, extra_path):
            allowed_parent = config.THUMB_ROOT
        elif _is_direct_child(config.HLS_ROOT, extra_path):
            allowed_parent = config.HLS_ROOT
        if not allowed_parent:
            logging.warning("Unsicherer Zusatzpfad ignoriert: %s", extra_path)
            continue
        try:
            if os.path.isdir(extra_path):
                shutil.rmtree(extra_path)
            else:
                os.unlink(extra_path)
            deleted += 1
        except Exception as exc:
            logging.warning("Konnte Zusatzpfad %s nicht löschen: %s", extra_path, exc)

    logging.info("Ordner geleert: %s (%s Einträge entfernt)", folder_path, deleted)
    return True


def process_transcribe(payload):
    media = _safe_path_under(payload.get('media'), config.BASE_DIR)
    transcript = payload.get('transcript')
    if transcript:
        transcript = _safe_path_under(transcript, config.BASE_DIR)
        if not transcript:
            logging.error("Unsicherer Transcribe-Zielpfad abgelehnt: %s", payload.get("transcript"))
            return False
    if not media:
        logging.error("Unsicherer oder fehlender Transcribe-Medienpfad: %s", payload.get("media"))
        return False
    logging.info(f"Starting transcribe task for: {media}")
    try:
        # Run the helper script so transcription runs in a separate process
        run_py = os.path.join(config.APP_DIR, 'run_transcribe.py')
        if not os.path.isfile(run_py):
            logging.error("Transcribe-Helfer fehlt: %s", run_py)
            return False
        cmd = [sys.executable, run_py, media]
        if transcript:
            cmd.append(transcript)
        # run synchronously so worker marks task complete/failed
        # Own session: the runner leads its process group, so a cancel from the
        # web UI can signal exactly this group and never the worker's own.
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3600,
            start_new_session=True,
        )
        if result.returncode != 0:
            logging.error(f"Transcribe subprocess failed: {result.stderr}")
            return False
        logging.info(f"Transcribe subprocess finished: {result.stdout}")
        return True
    except Exception as exc:
        logging.error(f"Transcribe task exception: {exc}")
        return False

def _write_remux_status(data):
    try:
        path = os.path.join(config.META_DIR, "remux_all_status.json")
        _write_private_json(path, data)
    except Exception as e:
        logging.error(f"Remux-Status konnte nicht geschrieben werden: {e}")

def _is_recent(path, seconds=120):
    try:
        return (time.time() - os.path.getmtime(path)) < seconds
    except OSError:
        return False

def _is_current_recording(channel_dir, filename):
    """True if any current-recording pointer claims filename, or .part + active lock.

    Fails closed: callers skip remux/cleanup on True, so an unreadable pointer or
    an unexpected error must count as "still recording", never as "finished".
    """
    filename = os.path.basename(str(filename or ""))
    if not filename or not channel_dir or not os.path.isdir(channel_dir):
        return False
    try:
        for name in os.listdir(channel_dir):
            if name == ".current_recording.json" or (
                name.startswith(".current_recording.") and name.endswith(".json")
            ):
                cur_path = os.path.join(channel_dir, name)
                try:
                    with open(cur_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except FileNotFoundError:
                    continue  # pointer removed between listdir and open
                except (OSError, ValueError) as exc:
                    # Half-written pointer while a recording starts or rotates.
                    logging.warning(
                        "Unreadable recording pointer %s, treating %s as active: %s",
                        cur_path, filename, exc,
                    )
                    return True
                if isinstance(data, dict) and data.get("filename") == filename:
                    return True

        part_path = os.path.join(channel_dir, f"{filename}.part")
        if not os.path.exists(part_path):
            return False

        for name in os.listdir(channel_dir):
            if name == ".recording.lock" or (
                name.startswith(".recording.") and name.endswith(".lock")
            ):
                lock_path = os.path.join(channel_dir, name)
                try:
                    with open(lock_path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    if not content:
                        continue
                    pid = int(content)
                    os.kill(pid, 0)
                    return True
                except PermissionError:
                    return True  # process exists but belongs to another user
                except (OSError, ValueError, TypeError):
                    continue
        return False
    except OSError as exc:
        logging.error(
            "Could not check active recording for %s in %s, treating as active: %s",
            filename, channel_dir, exc,
        )
        return True

def _is_remuxable_vod_filename(filename):
    name = os.path.basename(str(filename or ""))
    if not name or is_transient_media_filename(name):
        return False
    lower = name.lower()
    if lower.endswith(".mp4"):
        return is_archive_mp4_filename(name)
    if lower.endswith(".mkv"):
        return True
    return False


def _remux_file(path, reencode_audio, output_path=None):
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    final_path = output_path or path
    if path.lower().endswith(".mkv") and not output_path:
        final_path = os.path.splitext(path)[0] + ".mp4"
    tmp_path = f"{final_path}.remux.mp4"
    lock_path = f"{path}.remux.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return False, "Remux laeuft bereits"

    try:
        cmd = [
            ffmpeg, "-y", "-i", path,
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
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=REMUX_FILE_TIMEOUT_SEC)
            if result.returncode != 0 or not os.path.exists(tmp_path):
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                return False, (result.stderr.strip() or "FFmpeg Fehler")
            try:
                finalize_if_fragmented(tmp_path)
            except Exception as exc:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                return False, f"MP4-Index fehlgeschlagen: {exc}"
            os.replace(tmp_path, final_path)
            if final_path != path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return True, ""
        except subprocess.TimeoutExpired:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False, "Remux Timeout"
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _safe_vod_task_path(streamer, filename):
    streamer = str(streamer or "").strip()
    filename = str(filename or "").strip()
    if not streamer or not filename:
        return None
    if os.path.basename(streamer) != streamer or os.path.basename(filename) != filename:
        return None
    if not _is_remuxable_vod_filename(filename):
        return None

    base_dir = os.path.realpath(config.BASE_DIR)
    channel_dir = os.path.realpath(os.path.join(base_dir, streamer))
    if not channel_dir.startswith(base_dir + os.sep) or not os.path.isdir(channel_dir):
        return None
    path = os.path.realpath(os.path.join(channel_dir, filename))
    if not path.startswith(channel_dir + os.sep) or not os.path.isfile(path):
        return None
    return path


def _maybe_enqueue_storage_upload(path):
    """Queue the final remuxed archive when automatic cloud upload is enabled."""
    try:
        from core.storage import service as storage_service

        obj = storage_service.enqueue_auto_upload(path)
        if obj and obj.get("task_id"):
            logging.info(
                "Cloud-Upload nach Remux eingeordnet: %s (Task %s)",
                path,
                obj.get("task_id"),
            )
            return True
    except Exception as exc:
        from core.storage.base import mask_error

        logging.warning("Cloud-Upload nach Remux fehlgeschlagen: %s", mask_error(exc))
    return False


def process_remux_vod(payload):
    path = _safe_vod_task_path(payload.get("streamer"), payload.get("filename"))
    if not path:
        logging.error("Unsicherer oder fehlender VOD-Remux-Pfad: %s", payload)
        return False

    filename = os.path.basename(path)
    channel_dir = os.path.dirname(path)
    force = bool(payload.get("force"))
    if _is_current_recording(channel_dir, filename):
        logging.error("VOD-Remux uebersprungen, Datei ist noch aktiv: %s", path)
        return False
    if not force and _is_recent(path, 180):
        logging.error("VOD-Remux uebersprungen, Datei ist noch zu frisch: %s", path)
        return False

    output_path = None
    out_name = str(payload.get("output_filename") or "").strip()
    if out_name and os.path.basename(out_name) == out_name:
        candidate = os.path.join(channel_dir, out_name)
        output_path = candidate
    elif path.lower().endswith(".mkv"):
        output_path = os.path.splitext(path)[0] + ".mp4"

    ok, err = _remux_file(path, bool(payload.get("reencode_audio", False)), output_path=output_path)
    if not ok:
        logging.error("VOD-Remux fehlgeschlagen: %s: %s", path, err)
        return False
    final_path = output_path or path
    logging.info("VOD-Remux abgeschlossen: %s", final_path)
    # Copy-mode recordings finalize as .mkv; Discord/thumb were deferred until MP4 exists.
    if str(final_path).lower().endswith(".mp4") and os.path.isfile(final_path):
        try:
            enqueue_thumbnail_if_missing(final_path, discord_notify_ready=True)
        except Exception as exc:
            logging.error("Post-remux Discord/thumb enqueue failed: %s: %s", final_path, exc)
        try:
            from core import redis_client
            redis_client.invalidate_dashboard_cache()
        except Exception:
            pass
    _maybe_enqueue_storage_upload(final_path)
    return True


def process_remux_all(payload):
    reencode_audio = bool(payload.get("reencode_audio", True))
    base_dir = config.BASE_DIR
    skip_dirs = {".hls", ".hls_live", ".thumbs", ".meta", "venv", "static", "__pycache__"}
    clip_dir = clip_engine.CLIP_SUBDIR
    totals = {"total": 0, "ok": 0, "failed": 0, "skipped": 0}

    status = {
        "state": "running",
        "started_at": time.time(),
        "reencode_audio": reencode_audio,
        "progress": totals,
        "current": "",
    }
    _write_remux_status(status)

    for root, dirs, files in os.walk(base_dir):
        # prune dirs
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        if clip_dir in root.split(os.sep):
            continue
        for name in files:
            if not is_archive_mp4_filename(name):
                continue
            path = os.path.join(root, name)
            channel_dir = os.path.dirname(path)
            totals["total"] += 1
            status["current"] = path
            _write_remux_status(status)
            if _is_current_recording(channel_dir, name) or _is_recent(path, 180):
                totals["skipped"] += 1
                continue
            ok, err = _remux_file(path, reencode_audio)
            if ok:
                totals["ok"] += 1
            else:
                totals["failed"] += 1
                logging.error(f"Remux failed: {path}: {err}")

    status["state"] = "completed"
    status["finished_at"] = time.time()
    status["progress"] = totals
    status["current"] = ""
    _write_remux_status(status)


def process_task(task_type, payload):
    if task_type == 'thumbnail':
        process_thumbnail(payload)
        return
    if task_type == 'delete_folder':
        ok = process_delete_folder(payload)
        if not ok:
            raise RuntimeError("delete_folder failed")
        return
    if task_type == 'empty_folder':
        ok = process_empty_folder(payload)
        if not ok:
            raise RuntimeError("empty_folder failed")
        return
    if task_type == 'transcribe':
        ok = process_transcribe(payload)
        if not ok:
            raise RuntimeError("transcribe failed")
        return
    if task_type == 'remux_all':
        process_remux_all(payload)
        return
    if task_type == 'remux_vod':
        ok = process_remux_vod(payload)
        if not ok:
            raise RuntimeError("remux_vod failed")
        return
    if task_type in STORAGE_TASK_TYPES:
        from core.storage import service as storage_service

        if task_type == 'storage_upload':
            storage_service.run_upload(payload)
        elif task_type == 'storage_sync':
            storage_service.run_sync(payload)
        elif task_type == 'storage_delete':
            storage_service.run_delete(payload)
        elif task_type == 'storage_restore':
            storage_service.run_restore(payload)
        elif task_type == 'storage_check':
            storage_service.run_check(payload)
        elif task_type == 'storage_cleanup':
            storage_service.run_cleanup(payload)
        return
    raise RuntimeError(f"unknown task type: {task_type}")


def _pool_name_for_task(task_type):
    if task_type in REMUX_TASK_TYPES:
        return "remux"
    if task_type in ASR_TASK_TYPES:
        return "asr"
    if task_type in STORAGE_TASK_TYPES:
        return "storage"
    # thumbnail, delete/empty, and unknown → IO pool
    return "io"


def _types_with_capacity():
    """Return task types whose pool still has free slots."""
    allowed = []
    with _INFLIGHT_LOCK:
        if _INFLIGHT["io"] < _POOL_LIMITS["io"]:
            allowed.extend(IO_TASK_TYPES)
        if _INFLIGHT["remux"] < _POOL_LIMITS["remux"]:
            allowed.extend(REMUX_TASK_TYPES)
        if _INFLIGHT["asr"] < _POOL_LIMITS["asr"]:
            allowed.extend(ASR_TASK_TYPES)
        if _INFLIGHT["storage"] < _POOL_LIMITS["storage"]:
            allowed.extend(STORAGE_TASK_TYPES)
    return allowed


def storage_tick():
    """Enqueue due cloud sync/cleanup tasks from the maintenance loop."""
    from core.storage import service as storage_service

    return storage_service.tick()


def _submit_claimed_task(pools, pool_name, task):
    """Submit a claimed task, releasing its capacity slot if dispatch fails."""
    with _INFLIGHT_LOCK:
        _INFLIGHT[pool_name] += 1
    try:
        pools[pool_name].submit(_execute_claimed_task, task)
    except Exception:
        with _INFLIGHT_LOCK:
            _INFLIGHT[pool_name] = max(0, _INFLIGHT[pool_name] - 1)
        try:
            database.defer_task(
                task["id"],
                "Worker dispatch failed",
                delay_sec=30,
                worker_id=WORKER_ID,
            )
        except Exception as exc:
            logging.error(
                "Konnte Task %s nach Dispatch-Fehler nicht zurückstellen: %s",
                task.get("id"),
                exc,
            )
        raise


def _execute_claimed_task(task):
    """Run one claimed task in a pool thread; each DB op uses its own get_db()."""
    task_id = task["id"]
    task_type = task["type"]
    pool_name = _pool_name_for_task(task_type)
    try:
        payload = json.loads(task["payload"] or "{}")
        process_task(task_type, payload)
        database.complete_task(task_id, worker_id=WORKER_ID)
    except TaskDeferred as e:
        status = database.defer_task(
            task_id, str(e), delay_sec=e.delay_sec, worker_id=WORKER_ID
        )
        logging.info(
            "Task %s verschoben (%ss): %s [%s]",
            task_id,
            e.delay_sec,
            e,
            status,
        )
    except Exception as e:
        if task_type in STORAGE_TASK_TYPES:
            from core.storage.base import mask_error

            error_message = mask_error(e)
        else:
            error_message = str(e)
        logging.error(f"Task {task_id} failed: {error_message}")
        max_attempts = TASK_MAX_ATTEMPTS.get(task_type, 1)
        status = database.fail_task(
            task_id, error_message, max_attempts=max_attempts, worker_id=WORKER_ID
        )
        if status == "pending":
            logging.info(
                "Task %s wird erneut versucht (%s/%s).",
                task_id,
                task.get("attempts", 0),
                max_attempts,
            )
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT[pool_name] = max(0, _INFLIGHT[pool_name] - 1)


def main():
    init_db()
    logging.info(
        "Worker gestartet: %s (thumb=%s remux=%s asr=%s storage=%s)",
        WORKER_ID,
        WORKER_THUMB_THREADS,
        WORKER_REMUX_THREADS,
        WORKER_TRANSCRIBE_THREADS,
        WORKER_STORAGE_THREADS,
    )
    with ThreadPoolExecutor(max_workers=WORKER_THUMB_THREADS, thread_name_prefix="worker-io") as io_pool, \
         ThreadPoolExecutor(max_workers=WORKER_REMUX_THREADS, thread_name_prefix="worker-remux") as remux_pool, \
         ThreadPoolExecutor(max_workers=WORKER_TRANSCRIBE_THREADS, thread_name_prefix="worker-asr") as asr_pool, \
         ThreadPoolExecutor(max_workers=WORKER_STORAGE_THREADS, thread_name_prefix="worker-storage") as storage_pool:
        pools = {"io": io_pool, "remux": remux_pool, "asr": asr_pool, "storage": storage_pool}
        while True:
            try:
                def _safe_run(func):
                    try:
                        func()
                    except Exception as exc:
                        logging.error("Maintenance task %s failed: %s", func.__name__, exc)

                _safe_run(check_storage_alert)
                _safe_run(cleanup_old_tasks)
                _safe_run(cleanup_stale_thumbnail_locks)
                _safe_run(backfill_missing_thumbnails)
                _safe_run(storage_tick)

                allowed = _types_with_capacity()
                if not allowed:
                    time.sleep(0.5)
                    continue

                # 1. High-speed event-driven dispatch via Redis
                task = None
                try:
                    from core import redis_client
                    redis_item = redis_client.dequeue_task(timeout=1)
                    if redis_item and redis_item.get("id"):
                        task_type = redis_item.get("type")
                        if task_type in allowed:
                            task = database.claim_task_by_id(redis_item["id"], WORKER_ID)
                        else:
                            # Re-enqueue if current pool is saturated
                            redis_client.enqueue_task(task_type, redis_item.get("payload", {}), redis_item["id"])
                except Exception as r_err:
                    logging.debug("Redis dequeue skipped: %s", r_err)

                # 2. SQLite fallback / backlog claim
                if not task and "thumbnail" in allowed:
                    task = database.claim_next_task(
                        WORKER_ID,
                        stale_after=TASK_STALE_SEC,
                        allowed_types=["thumbnail"],
                        max_attempts_by_type=TASK_MAX_ATTEMPTS,
                    )
                if not task:
                    task = database.claim_next_task(
                        WORKER_ID,
                        stale_after=TASK_STALE_SEC,
                        allowed_types=allowed,
                        max_attempts_by_type=TASK_MAX_ATTEMPTS,
                    )
                if task:
                    pool_name = _pool_name_for_task(task["type"])
                    _submit_claimed_task(pools, pool_name, task)
                else:
                    time.sleep(2)
            except Exception as e:
                logging.error(f"Worker Loop Error: {e}")
                time.sleep(5)

if __name__ == "__main__":
    main()
