#!/usr/bin/env python3
"""Chat command !clip — cut last N seconds from live HLS and post to Discord."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse

from recorder import clip_engine
from core import config
from core import database

logger = logging.getLogger(__name__)

CLIP_CMD_RE = re.compile(r"^!clip(?:\s+|$)(.*)$", re.IGNORECASE)
DEFAULT_DURATION_SEC = 20
DEFAULT_COOLDOWN_SEC = 45
MAX_DURATION_SEC = 60

_LOCK = threading.Lock()
_LAST_CLIP_TS: dict[str, float] = {}
_IN_FLIGHT: set[str] = set()


def clip_duration_sec() -> int:
    try:
        value = int(os.environ.get("CHAT_CLIP_DURATION_SEC", str(DEFAULT_DURATION_SEC)))
    except (TypeError, ValueError):
        value = DEFAULT_DURATION_SEC
    return max(5, min(MAX_DURATION_SEC, value))


def clip_cooldown_sec() -> int:
    try:
        value = int(os.environ.get("CHAT_CLIP_COOLDOWN_SEC", str(DEFAULT_COOLDOWN_SEC)))
    except (TypeError, ValueError):
        value = DEFAULT_COOLDOWN_SEC
    return max(5, value)


def clip_enabled_for(streamer: str) -> bool:
    """Allowlisted streamers, or every recorded Twitch channel if unset."""
    raw = (
        os.environ.get("CHAT_CLIP_STREAMERS", "").strip()
        or os.environ.get("DISCORD_CLIP_STREAMERS", "").strip()
        or os.environ.get("DISCORD_NOTIFY_STREAMERS", "").strip()
    )
    name = re.sub(r"[^a-z0-9_]+", "", str(streamer or "").strip().lower())
    if not name:
        return False
    if not raw:
        conf = database.get_channel_config(name)
        return bool(conf and conf.get("record"))
    allowed = {
        re.sub(r"[^a-z0-9_]+", "", part.strip().lower())
        for part in raw.split(",")
        if part.strip()
    }
    return name in allowed


def parse_clip_command(message: str) -> str | None:
    """Return optional title if message is a !clip command, else None."""
    text = str(message or "").strip()
    match = CLIP_CMD_RE.match(text)
    if not match:
        return None
    title = str(match.group(1) or "").strip()
    # Ignore accidental "!!clip" style only if not starting with !clip
    if text.lower().startswith("!!"):
        return None
    return title[:80]


def live_hls_playlist(source_channel: str) -> str:
    root = os.environ.get("LIVE_HLS_DIR") or config.LIVE_HLS_ROOT
    login = re.sub(r"[^a-z0-9_]+", "", str(source_channel or "").strip().lower())
    if not login:
        return ""
    return os.path.join(root, login, "index.m3u8")


def ensure_live_hls_enabled(source_channel: str) -> None:
    """Make livebot keep an HLS buffer for chat-clip streamers while they record."""
    login = re.sub(r"[^a-z0-9_]+", "", str(source_channel or "").strip().lower())
    if not login or not clip_enabled_for(login):
        return
    try:
        from core import database

        # Only flip live=1 for channels that already record. Never INSERT record=1
        # for a watch-only / unknown login (that caused Discord pings for random watches).
        conf = database.get_channel_config(login)
        if not conf or not int(conf.get("record") or 0):
            return
        conn = database.get_db()
        conn.execute("UPDATE channels SET live=1 WHERE name=? AND record=1", (login,))
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Could not enable live HLS for %s", login)


def find_active_recording_file(archive_dir: str, source_channel: str = "") -> str:
    return clip_engine.find_active_recording_file(archive_dir, source_channel)


def clip_output_path(archive_dir: str, source_channel: str, username: str, title: str, duration: int) -> str:
    clips_dir = os.path.join(archive_dir, clip_engine.CLIP_SUBDIR)
    os.makedirs(clips_dir, exist_ok=True)
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    user_part = clip_engine.sanitize_label(username) or "chat"
    title_part = clip_engine.sanitize_label(title)
    label = f"{user_part}" + (f"_{title_part}" if title_part else "")
    name = f"chatclip_{source_channel}_{label}_{ts}_{duration}s.mp4"
    return clip_engine.unique_path(os.path.join(clips_dir, name))


def _compress_clip(input_path: str, max_bytes: int = 8_000_000) -> str:
    """Re-encode oversized clips so Discord's default upload limit accepts them."""
    try:
        size = os.path.getsize(input_path)
    except OSError:
        return input_path
    if size <= max_bytes:
        return input_path
    ffmpeg = clip_engine.ffmpeg_bin() or "ffmpeg"
    out_path = f"{os.path.splitext(input_path)[0]}_small.mp4"
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-fflags",
        "+genpts+igndts",
        "-i",
        input_path,
        "-vf",
        "setpts=PTS-STARTPTS",
        "-af",
        "asetpts=PTS-STARTPTS",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-shortest",
        "-movflags",
        "+faststart",
        out_path,
    ]
    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
            text=True,
        )
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 1024:
            logger.info(
                "!clip komprimiert: %.1fMB -> %.1fMB",
                size / 1e6,
                os.path.getsize(out_path) / 1e6,
            )
            return out_path
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
        except OSError:
            pass
    except Exception:
        logger.exception("!clip Kompression fehlgeschlagen")
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
        except OSError:
            pass
    return input_path


def notify_discord_chat_clip(
    streamer: str,
    file_path: str,
    *,
    username: str,
    title: str,
    duration: int,
) -> bool:
    _ = (streamer, file_path, username, title, duration)
    return False


def _create_and_post(
    source_channel: str,
    archive_dir: str,
    username: str,
    title: str,
) -> None:
    streamer = re.sub(r"[^a-z0-9_]+", "", str(source_channel or "").strip().lower())
    key = streamer
    duration = clip_duration_sec()
    try:
        ensure_live_hls_enabled(streamer)
        out_path = clip_output_path(archive_dir, streamer, username, title, duration)
        ok_space, space_err = clip_engine.check_disk_space(archive_dir)
        if not ok_space:
            logger.warning("!clip: %s (%s)", space_err, streamer)
            return
        playlist = live_hls_playlist(source_channel)
        recording = find_active_recording_file(archive_dir, streamer)
        ok, source_used = clip_engine.create_live_clip(
            playlist=playlist,
            recording=recording,
            output_path=out_path,
            duration_sec=float(duration),
            start_sec=None,
            quality=clip_engine.load_settings().get("quality") or "fast",
        )

        if not ok:
            logger.warning("!clip: Clip-Erstellung fehlgeschlagen für %s", source_channel)
            return
        logger.info("!clip erstellt: %s (von %s, source=%s)", out_path, username, source_used)

        try:
            from core import alert_sounds

            alert_sounds.enqueue(
                "clip",
                streamer=streamer,
                label=(title or username)[:40],
            )
        except Exception:
            logger.exception("alert sound hook failed for chat clip")
    finally:
        with _LOCK:
            _IN_FLIGHT.discard(key)
            _LAST_CLIP_TS[key] = time.time()


def stream_chat_channel_id() -> str:
    """Optional Discord channel to poll for !clip. Unset = Twitch chat only."""
    return os.environ.get("DISCORD_STREAM_CHAT_CHANNEL_ID", "").strip()


def archive_dir_for_streamer(streamer: str) -> str:
    login = re.sub(r"[^a-z0-9_]+", "", str(streamer or "").strip().lower())
    if not login:
        return ""
    base = os.environ.get("RECORDINGS_BASE_DIR") or config.BASE_DIR
    return os.path.join(base, login)


def poll_discord_clip_commands(limit: int = 30) -> int:
    _ = limit
    return 0


def clip_streamers_primary() -> list[str]:
    raw = (
        os.environ.get("CHAT_CLIP_STREAMERS", "").strip()
        or os.environ.get("DISCORD_CLIP_STREAMERS", "").strip()
        or os.environ.get("DISCORD_NOTIFY_STREAMERS", "").strip()
    )
    if raw:
        return [
            re.sub(r"[^a-z0-9_]+", "", part.strip().lower())
            for part in raw.split(",")
            if part.strip()
        ]
    return [str(c.get("name") or "") for c in database.list_channel_configs(mode="record") if c.get("name")]


def request_clip_from_chat(
    source_channel: str,
    archive_dir: str,
    username: str,
    message: str,
) -> bool:
    """If message is !clip, start async clip job.

    Returns True when the message was a !clip command (even if skipped).
    Use clip_job_started() / last outcome via request_clip_outcome for acks.
    """
    outcome = request_clip_outcome(source_channel, archive_dir, username, message)
    return outcome != ""


def request_clip_outcome(
    source_channel: str,
    archive_dir: str,
    username: str,
    message: str,
) -> str:
    """Return '' if not !clip, else started|cooldown|busy|disabled|no_archive."""
    title = parse_clip_command(message)
    if title is None:
        return ""

    streamer = re.sub(r"[^a-z0-9_]+", "", str(source_channel or "").strip().lower())
    if not clip_enabled_for(streamer):
        return "disabled"

    user = str(username or "chat").strip() or "chat"
    archive_dir = str(archive_dir or "").strip()
    if not archive_dir or not os.path.isdir(archive_dir):
        logger.warning("!clip: archive_dir fehlt für %s", streamer)
        return "no_archive"

    now = time.time()
    with _LOCK:
        if streamer in _IN_FLIGHT:
            logger.info("!clip ignoriert (läuft bereits): %s", streamer)
            return "busy"
        last = float(_LAST_CLIP_TS.get(streamer) or 0)
        if now - last < clip_cooldown_sec():
            logger.info(
                "!clip Cooldown aktiv für %s (noch %.0fs)",
                streamer,
                clip_cooldown_sec() - (now - last),
            )
            return "cooldown"
        _IN_FLIGHT.add(streamer)

    thread = threading.Thread(
        target=_create_and_post,
        args=(streamer, archive_dir, user, title),
        name=f"chat-clip-{streamer}",
        daemon=True,
    )
    thread.start()
    return "started"
