#!/usr/bin/env python3
"""OBS alert-sound queue for stream notifications.

Events are enqueued after successful Discord/side effects. An OBS Browser Source
loads /alerts/overlay?token=… and plays short MP3s (Monitor and Output).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import uuid
from typing import Any

from core import config
from discord_bots import discord_notify

logger = logging.getLogger(__name__)

KNOWN_EVENTS = (
    "live",
    "stream_end",
    "clip",
    "twitch_clip",
    "giveaway",
    "giveaway_win",
    "contest",
    "contest_win",
)

# Simple melodic recipes: list of (hz, duration_sec)
_SOUND_RECIPES = {
    "live": [(784, 0.12), (988, 0.14), (1175, 0.18)],
    "stream_end": [(659, 0.14), (523, 0.16), (392, 0.22)],
    "clip": [(880, 0.1), (1320, 0.16)],
    "twitch_clip": [(740, 0.1), (990, 0.14)],
    "giveaway": [(523, 0.1), (659, 0.1), (784, 0.16)],
    "giveaway_win": [(523, 0.08), (659, 0.08), (784, 0.08), (1046, 0.22)],
    "contest": [(587, 0.1), (740, 0.14)],
    "contest_win": [(587, 0.08), (740, 0.08), (880, 0.08), (1175, 0.2)],
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", str(name or "").strip().lower())


def enabled() -> bool:
    return os.environ.get("ALERT_SOUNDS_ENABLED", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def overlay_token() -> str:
    return (
        os.environ.get("ALERT_OVERLAY_TOKEN", "").strip()
        or os.environ.get("TTS_OVERLAY_TOKEN", "").strip()
    )


def token_ok(provided: str) -> bool:
    import hmac
    expected = overlay_token()
    if not expected or not provided:
        return False

    return hmac.compare_digest(str(provided), expected)


def allowed_events() -> set[str]:
    raw = os.environ.get("ALERT_SOUND_EVENTS", "").strip()
    if not raw:
        return set(KNOWN_EVENTS)
    return {_norm(p).replace("-", "_") for p in raw.split(",") if p.strip()} & set(KNOWN_EVENTS)


def sounds_dir() -> str:
    path = os.path.join(config.APP_DIR, "WEBUI", "static", "alerts")
    os.makedirs(path, exist_ok=True)
    return path


def queue_path() -> str:
    os.makedirs(config.META_DIR, exist_ok=True)
    return os.path.join(config.META_DIR, "alert_sound_queue.json")


def _load_queue() -> dict[str, Any]:
    try:
        with open(queue_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data.setdefault("items", [])
            return data
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("alert queue load failed")
    return {"items": []}


def _save_queue(data: dict[str, Any]) -> None:
    path = queue_path()
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def ensure_sound_files() -> None:
    """Generate missing alert MP3s via ffmpeg (sine tones)."""
    root = sounds_dir()
    for event, notes in _SOUND_RECIPES.items():
        out = os.path.join(root, f"{event}.mp3")
        if os.path.isfile(out) and os.path.getsize(out) > 500:
            continue
        try:
            _render_tone_mp3(out, notes)
        except Exception:
            logger.exception("failed to render alert sound %s", event)


def _render_tone_mp3(path: str, notes: list[tuple[float, float]]) -> None:
    inputs = []
    filters = []
    for idx, (hz, dur) in enumerate(notes):
        inputs.extend(["-f", "lavfi", "-i", f"sine=frequency={hz}:duration={dur}"])
        filters.append(f"[{idx}:a]")
    filt = "".join(filters) + f"concat=n={len(notes)}:v=0:a=1[out]"
    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        filt,
        "-map",
        "[out]",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "4",
        path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def enqueue(
    event: str,
    *,
    streamer: str = "",
    label: str = "",
) -> bool:
    if not enabled():
        return False
    event = _norm(event).replace("-", "_")
    if event not in allowed_events():
        return False
    ensure_sound_files()
    sound = os.path.join(sounds_dir(), f"{event}.mp3")
    if not os.path.isfile(sound):
        logger.warning("alert sound missing: %s", event)
        return False

    item = {
        "id": uuid.uuid4().hex[:12],
        "event": event,
        "streamer": _norm(streamer),
        "label": str(label or "")[:80],
        "created_at": time.time(),
        "status": "ready",
    }
    with discord_notify._FileLock(queue_path()):
        data = _load_queue()
        items = [i for i in data.get("items") or [] if isinstance(i, dict)]
        # Drop stale / done
        now = time.time()
        items = [
            i
            for i in items
            if i.get("status") in ("ready", "playing")
            or now - float(i.get("created_at") or 0) < 300
        ]
        # Cooldown: same event within 3s
        recent = [
            i
            for i in items
            if i.get("event") == event and now - float(i.get("created_at") or 0) < 3
        ]
        if recent:
            return False
        items.append(item)
        data["items"] = items[-40:]
        _save_queue(data)
    logger.info("alert sound queued: %s (%s)", event, label or streamer)
    return True


def claim_next() -> dict[str, Any] | None:
    with discord_notify._FileLock(queue_path()):
        data = _load_queue()
        items = data.get("items") or []
        playing = next((i for i in items if i.get("status") == "playing"), None)
        if playing:
            # Auto-expire stuck playing (>15s)
            if time.time() - float(playing.get("started_at") or playing.get("created_at") or 0) > 15:
                playing["status"] = "done"
            else:
                _save_queue(data)
                return None
        item = next((i for i in items if i.get("status") == "ready"), None)
        if not item:
            _save_queue(data)
            return None
        item["status"] = "playing"
        item["started_at"] = time.time()
        _save_queue(data)
        return dict(item)


def ack(item_id: str) -> bool:
    with discord_notify._FileLock(queue_path()):
        data = _load_queue()
        for i in data.get("items") or []:
            if i.get("id") == item_id:
                i["status"] = "done"
                _save_queue(data)
                return True
        return False


def public_base_hint() -> str:
    base = os.environ.get("VOD_PUBLIC_BASE", "").strip().rstrip("/")
    return base or "http://localhost:5000"


def overlay_url() -> str:
    tok = overlay_token()
    return f"{public_base_hint()}/alerts/overlay?token={tok}"
