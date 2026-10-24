#!/usr/bin/env python3
"""Twitch/Discord TTS for OBS Browser Source (stream + local monitor).

Flow:
  !tts text  →  synthesize (edge-tts)  →  queue JSON
  OBS loads /tts/overlay?token=…  →  polls /tts/api/next  →  plays MP3

Streamer hears it via OBS audio monitoring (Monitor and Output).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from core import config

logger = logging.getLogger(__name__)

TTS_RE = re.compile(r"^!tts(?:\s+|$)(.*)$", re.IGNORECASE)
TTS_SKIP_RE = re.compile(r"^!(?:ttsskip|skiptts)(?:\s+)?$", re.IGNORECASE)
TTS_ON_RE = re.compile(r"^!ttson(?:\s+)?$", re.IGNORECASE)
TTS_OFF_RE = re.compile(r"^!ttsoff(?:\s+)?$", re.IGNORECASE)

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
SAFE_TEXT_RE = re.compile(r"[^\w\s\-.,!?äöüÄÖÜß'\":;()]+", re.UNICODE)

VOICES = {
    "katja": "de-DE-KatjaNeural",
    "female": "de-DE-KatjaNeural",
    "conrad": "de-DE-ConradNeural",
    "klaus": "de-DE-ConradNeural",
    "male": "de-DE-ConradNeural",
    "amala": "de-DE-AmalaNeural",
    "killian": "de-DE-KillianNeural",
}

DEFAULT_VOICE_ALIAS = "katja"
_synth_lock = threading.Lock()
_synth_thread: threading.Thread | None = None


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", str(name or "").strip().lower())


def enabled() -> bool:
    return os.environ.get("TTS_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")


def overlay_token() -> str:
    return os.environ.get("TTS_OVERLAY_TOKEN", "").strip()


def token_ok(provided: str) -> bool:
    expected = overlay_token()
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), expected)


def token_for_streamer(streamer: str) -> str:
    """Overlay token that may read this streamer's queue only."""
    name = _norm(streamer)
    if not name:
        return ""
    specific = os.environ.get(f"TTS_OVERLAY_TOKEN_{name.upper()}", "").strip()
    if specific:
        return specific
    return overlay_token()


def token_ok_for(provided: str, streamer: str) -> bool:
    name = _norm(streamer)
    if not name:
        return token_ok(provided)
    expected = token_for_streamer(name)
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), expected)


def max_chars() -> int:
    try:
        return max(20, min(500, int(os.environ.get("TTS_MAX_CHARS", "200"))))
    except (TypeError, ValueError):
        return 200


def cooldown_sec() -> int:
    try:
        return max(0, min(600, int(os.environ.get("TTS_COOLDOWN_SEC", "20"))))
    except (TypeError, ValueError):
        return 20


def max_queue() -> int:
    try:
        return max(1, min(50, int(os.environ.get("TTS_MAX_QUEUE", "15"))))
    except (TypeError, ValueError):
        return 15


def default_voice() -> str:
    alias = os.environ.get("TTS_DEFAULT_VOICE", DEFAULT_VOICE_ALIAS).strip().lower()
    return VOICES.get(alias) or VOICES[DEFAULT_VOICE_ALIAS]


def resolve_voice(alias: str) -> str:
    key = str(alias or "").strip().lower()
    if key in VOICES:
        return VOICES[key]
    if key.startswith("de-") and key.endswith("Neural"):
        return key
    return default_voice()


def tts_dir() -> str:
    path = os.path.join(config.META_DIR, "tts")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _state_path(streamer: str) -> str:
    return os.path.join(tts_dir(), f"queue_{_norm(streamer)}.json")


def _load_state(streamer: str) -> dict[str, Any]:
    path = _state_path(streamer)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data.setdefault("enabled", True)
            data.setdefault("items", [])
            data.setdefault("cooldowns", {})
            data.setdefault("skip_current", False)
            return data
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("tts state load failed")
    return {"enabled": True, "items": [], "cooldowns": {}, "skip_current": False}


def _save_state(streamer: str, data: dict[str, Any]) -> None:
    path = _state_path(streamer)
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@contextmanager
def _state_lock(streamer: str) -> Iterator[None]:
    """Cross-process + in-process lock around TTS queue JSON."""
    lock_path = f"{_state_path(streamer)}.lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with _synth_lock:
        try:
            import fcntl  # type: ignore
            with open(lock_path, "a", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except ImportError:
            yield


def sanitize_text(text: str) -> str:
    cleaned = URL_RE.sub("", str(text or ""))
    cleaned = SAFE_TEXT_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[: max_chars()]


def parse_tts_payload(raw: str) -> tuple[str, str]:
    """Return (voice_id, text). Supports `!tts [voice] message`."""
    rest = (raw or "").strip()
    if not rest:
        return default_voice(), ""
    parts = rest.split(None, 1)
    first = parts[0].lower()
    if first in VOICES and len(parts) > 1:
        return resolve_voice(first), sanitize_text(parts[1])
    return default_voice(), sanitize_text(rest)


def is_mod_or_broadcaster(tags: dict | None, username: str = "", streamer: str = "") -> bool:
    if isinstance(tags, dict):
        if str(tags.get("mod") or "") == "1":
            return True
        badges = str(tags.get("badges") or "")
        if "broadcaster/" in badges or "moderator/" in badges:
            return True
    user = _norm(username)
    if streamer and user == _norm(streamer):
        return True
    mods = {
        _norm(p)
        for p in os.environ.get("TTS_MOD_USERS", "").split(",")
        if p.strip()
    }
    # Always allow configured mods; if unset, Discord skips need TTS_MOD_USERS or streamer name.
    return user in mods if mods else False


def channel_point_triggers_tts(tags: dict | None) -> bool:
    if not isinstance(tags, dict):
        return False
    reward_id = str(tags.get("custom-reward-id") or "").strip()
    if not reward_id:
        return False
    allowed = os.environ.get("TWITCH_TTS_REWARD_IDS", "").strip()
    if not allowed:
        return False
    ids = {p.strip() for p in allowed.split(",") if p.strip()}
    return reward_id in ids


def set_enabled(streamer: str, on: bool) -> None:
    with _state_lock(streamer):
        state = _load_state(streamer)
        state["enabled"] = bool(on)
        _save_state(streamer, state)


def request_skip(streamer: str) -> bool:
    with _state_lock(streamer):
        state = _load_state(streamer)
        playing = next((i for i in state["items"] if i.get("status") == "playing"), None)
        if playing:
            state["skip_current"] = True
            playing["status"] = "done"
            _save_state(streamer, state)
            return True
        ready = next((i for i in state["items"] if i.get("status") == "ready"), None)
        if ready:
            ready["status"] = "skipped"
            _save_state(streamer, state)
            return True
        return False


def enqueue(
    streamer: str,
    username: str,
    text: str,
    *,
    voice: str = "",
    force: bool = False,
) -> tuple[bool, str]:
    """Queue a TTS line. Returns (ok, message_for_logs_or_chat)."""
    if not enabled():
        return False, "TTS ist deaktiviert (Server)."
    streamer = _norm(streamer)
    if not streamer:
        return False, "Ungültiger Streamer."
    voice_id = voice or default_voice()
    clean = sanitize_text(text)
    if len(clean) < 2:
        return False, "Text zu kurz / leer."

    with _state_lock(streamer):
        state = _load_state(streamer)
        if not state.get("enabled") and not force:
            return False, "TTS ist aus (!ttson zum Aktivieren)."
        pending = [
            i
            for i in state["items"]
            if i.get("status") in ("queued", "ready", "playing", "synthesizing")
        ]
        if len(pending) >= max_queue():
            return False, "TTS-Queue voll."
        user_key = _norm(username) or "anon"
        now = time.time()
        # Same viewer text a moment later is a double delivery (two IRC
        # readers, or the same line arriving twice). This also applies when
        # force=True, because the standalone Twitch bot uses force to queue
        # long-message chunks and would otherwise replay the first chunk.
        for existing in reversed(state.get("items") or []):
            if now - float(existing.get("created_at") or 0) > 8:
                break
            if _norm(existing.get("user")) == user_key and _texts_overlap(
                existing.get("text") or "", clean
            ):
                return False, "TTS doppelt, ignoriert."
        last = float((state.get("cooldowns") or {}).get(user_key) or 0)
        if not force and cooldown_sec() and now - last < cooldown_sec():
            wait = int(cooldown_sec() - (now - last))
            return False, f"Cooldown ({wait}s)."
        item_id = uuid.uuid4().hex[:12]
        item = {
            "id": item_id,
            "streamer": streamer,
            "user": username,
            "text": clean,
            "voice": voice_id,
            "status": "queued",
            "created_at": now,
            "audio": "",
        }
        state["items"].append(item)
        state.setdefault("cooldowns", {})[user_key] = now
        # prune old done items
        cutoff = now - 3600
        state["items"] = [
            i
            for i in state["items"]
            if i.get("status") in ("queued", "ready", "playing", "synthesizing")
            or float(i.get("created_at") or 0) > cutoff
        ][-80:]
        _save_state(streamer, state)

    _ensure_synth_worker()
    return True, f"TTS #{item_id} in Queue."


def start_synth_worker() -> None:
    """Start the background edge-tts worker if it is not already running."""
    _ensure_synth_worker()


def _ensure_synth_worker() -> None:
    global _synth_thread
    if _synth_thread and _synth_thread.is_alive():
        return

    def loop() -> None:
        while True:
            try:
                worked = _synthesize_next()
            except Exception:
                logger.exception("tts synth loop error")
                worked = False
            time.sleep(0.25 if worked else 1.0)

    _synth_thread = threading.Thread(target=loop, name="tts-synth", daemon=True)
    _synth_thread.start()


def _synthesize_next() -> bool:
    # Find any streamer with queued items
    root = tts_dir()
    try:
        names = [n for n in os.listdir(root) if n.startswith("queue_") and n.endswith(".json")]
    except OSError:
        return False
    for name in names:
        streamer = name[len("queue_") : -len(".json")]
        with _state_lock(streamer):
            state = _load_state(streamer)
            item = next((i for i in state["items"] if i.get("status") == "queued"), None)
            if not item:
                continue
            item["status"] = "synthesizing"
            _save_state(streamer, state)
            item_id = item["id"]
            text = item["text"]
            voice = item["voice"]
        audio_path = os.path.join(tts_dir(), f"{streamer}_{item_id}.mp3")
        try:
            asyncio.run(_edge_save(text, voice, audio_path))
            ok = os.path.isfile(audio_path) and os.path.getsize(audio_path) > 0
        except Exception:
            logger.exception("edge-tts failed for %s", item_id)
            ok = False
        with _state_lock(streamer):
            state = _load_state(streamer)
            for i in state["items"]:
                if i.get("id") == item_id:
                    if ok:
                        i["status"] = "ready"
                        i["audio"] = os.path.basename(audio_path)
                    else:
                        i["status"] = "error"
                    break
            _save_state(streamer, state)
        return True
    return False


async def _edge_save(text: str, voice: str, path: str) -> None:
    import edge_tts

    tmp = path + ".part"
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(tmp)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _texts_overlap(left: str, right: str) -> bool:
    """True when two TTS strings are the same line, including a 200-char cut
    versus a word-wrapped cut of the same opening."""
    a = (left or "").strip()
    b = (right or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 40:
        return False
    return long_.startswith(short)


def _same_line(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if _norm(a.get("user")) != _norm(b.get("user")):
        return False
    if not _texts_overlap(a.get("text") or "", b.get("text") or ""):
        return False
    return abs(float(a.get("created_at") or 0) - float(b.get("created_at") or 0)) <= 8


def claim_next(streamer: str) -> dict[str, Any] | None:
    """OBS overlay claims the next ready item (marks playing)."""
    streamer = _norm(streamer)
    with _state_lock(streamer):
        state = _load_state(streamer)
        # If something is still playing and not skipped, wait
        playing = next((i for i in state["items"] if i.get("status") == "playing"), None)
        if playing:
            if state.get("skip_current"):
                playing["status"] = "done"
                state["skip_current"] = False
            else:
                started = float(playing.get("started_at") or 0)
                # Autoplay-block in OBS can abandon a claim; recover after 20s
                if started and (time.time() - started) > 20:
                    playing["status"] = "ready"
                else:
                    return None
        while True:
            item = next((i for i in state["items"] if i.get("status") == "ready"), None)
            if not item:
                _save_state(streamer, state)
                return None
            earlier = [
                other
                for other in state["items"]
                if other.get("id") != item.get("id")
                and other.get("status") != "error"
                and _same_line(item, other)
                and float(other.get("created_at") or 0) <= float(item.get("created_at") or 0)
            ]
            if earlier:
                logger.info(
                    "TTS duplicate suppressed at playback %s #%s",
                    streamer,
                    item.get("id"),
                )
                item["status"] = "done"
                item["finished_at"] = time.time()
                continue
            item["status"] = "playing"
            item["started_at"] = time.time()
            state["skip_current"] = False
            _save_state(streamer, state)
            return dict(item)


def ack_item(streamer: str, item_id: str) -> bool:
    streamer = _norm(streamer)
    with _state_lock(streamer):
        state = _load_state(streamer)
        for i in state["items"]:
            if i.get("id") == item_id:
                i["status"] = "done"
                i["finished_at"] = time.time()
                state["skip_current"] = False
                _save_state(streamer, state)
                # cleanup audio later
                audio = i.get("audio") or ""
                if audio:
                    try:
                        os.remove(os.path.join(tts_dir(), audio))
                    except OSError:
                        pass
                return True
        return False


def should_skip_playing(streamer: str, item_id: str) -> bool:
    with _state_lock(streamer):
        state = _load_state(streamer)
        if state.get("skip_current"):
            return True
        for i in state["items"]:
            if i.get("id") == item_id and i.get("status") != "playing":
                return True
        return False


def queue_status(streamer: str) -> dict[str, Any]:
    with _state_lock(streamer):
        state = _load_state(streamer)
        pending = [
            i
            for i in state["items"]
            if i.get("status") in ("queued", "ready", "playing", "synthesizing")
        ]
        return {
            "enabled": bool(state.get("enabled", True)),
            "pending": len(pending),
            "items": [
                {
                    "id": i.get("id"),
                    "user": i.get("user"),
                    "text": i.get("text"),
                    "status": i.get("status"),
                }
                for i in pending[:20]
            ],
        }


def handle_tts_command(
    streamer: str,
    username: str,
    message: str,
    *,
    tags: dict | None = None,
) -> tuple[bool, str]:
    """Parse chat command. Returns (handled, reply)."""
    text = str(message or "").strip()
    if not text:
        return False, ""

    if TTS_SKIP_RE.match(text):
        if not is_mod_or_broadcaster(tags, username, streamer):
            return True, "Nur Mod/Broadcaster."
        ok = request_skip(streamer)
        return True, "TTS übersprungen." if ok else "Nichts zum Skippen."

    if TTS_OFF_RE.match(text):
        if not is_mod_or_broadcaster(tags, username, streamer):
            return True, "Nur Mod/Broadcaster."
        set_enabled(streamer, False)
        return True, "TTS aus."

    if TTS_ON_RE.match(text):
        if not is_mod_or_broadcaster(tags, username, streamer):
            return True, "Nur Mod/Broadcaster."
        set_enabled(streamer, True)
        return True, "TTS an."

    m = TTS_RE.match(text)
    if not m:
        return False, ""
    voice, body = parse_tts_payload(m.group(1) or "")
    ok, info = enqueue(streamer, username, body, voice=voice)
    return True, info


def public_base_hint() -> str:
    base = os.environ.get("VOD_PUBLIC_BASE", "").strip().rstrip("/")
    if base:
        return base
    return "http://localhost:5000"
