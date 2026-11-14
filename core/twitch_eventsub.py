#!/usr/bin/env python3
"""Twitch EventSub webhook helpers (stream.online / stream.offline).

Additive live-status cache for recorder/livebot. Does not replace GQL/Helix polling.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from core import config
from core import database
from core import twitch_api

logger = logging.getLogger(__name__)

EVENTSUB_ENABLED = os.environ.get("TWITCH_EVENTSUB_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
EVENTSUB_SECRET = os.environ.get("TWITCH_EVENTSUB_SECRET", "").strip()
EVENTSUB_CALLBACK_URL = os.environ.get("TWITCH_EVENTSUB_CALLBACK_URL", "").strip()
HELIX_TIMEOUT_SEC = float(os.environ.get("TWITCH_HELIX_TIMEOUT_SEC", "8"))

_STATE_LOCK = threading.Lock()
_STATE_CACHE: dict[str, Any] = {"by_login": {}, "by_user_id": {}, "updated_at": 0.0}


def reload_env() -> None:
    """Refresh module-level config from environment (used by tests)."""
    global EVENTSUB_ENABLED, EVENTSUB_SECRET, EVENTSUB_CALLBACK_URL
    EVENTSUB_ENABLED = os.environ.get("TWITCH_EVENTSUB_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    EVENTSUB_SECRET = os.environ.get("TWITCH_EVENTSUB_SECRET", "").strip()
    EVENTSUB_CALLBACK_URL = os.environ.get("TWITCH_EVENTSUB_CALLBACK_URL", "").strip()
    with _STATE_LOCK:
        _STATE_CACHE.clear()
        _STATE_CACHE.update({"by_login": {}, "by_user_id": {}, "updated_at": 0.0})
    from core import redis_client

    # delete_state() absorbs Redis errors itself and returns False.
    redis_client.delete_state("sysstate:eventsub_state")


def enabled() -> bool:
    return bool(EVENTSUB_ENABLED and EVENTSUB_SECRET and EVENTSUB_CALLBACK_URL)


def state_path() -> str:
    return os.path.join(config.META_DIR, "eventsub_state.json")


def _empty_state() -> dict[str, Any]:
    return {"by_login": {}, "by_user_id": {}, "updated_at": 0.0}


def load_state() -> dict[str, Any]:
    with _STATE_LOCK:
        if _STATE_CACHE.get("by_login") or _STATE_CACHE.get("updated_at"):
            return {
                "by_login": dict(_STATE_CACHE.get("by_login") or {}),
                "by_user_id": dict(_STATE_CACHE.get("by_user_id") or {}),
                "updated_at": float(_STATE_CACHE.get("updated_at") or 0),
            }
    data = database.get_system_state("eventsub_state", default={})
    if not data:
        return _empty_state()
    by_login = data.get("by_login") if isinstance(data.get("by_login"), dict) else {}
    by_user_id = data.get("by_user_id") if isinstance(data.get("by_user_id"), dict) else {}
    updated_at = float(data.get("updated_at") or 0)
    with _STATE_LOCK:
        _STATE_CACHE["by_login"] = dict(by_login)
        _STATE_CACHE["by_user_id"] = dict(by_user_id)
        _STATE_CACHE["updated_at"] = updated_at
    return {"by_login": dict(by_login), "by_user_id": dict(by_user_id), "updated_at": updated_at}


def save_state(state: dict[str, Any]) -> None:
    payload = {
        "by_login": dict(state.get("by_login") or {}),
        "by_user_id": dict(state.get("by_user_id") or {}),
        "updated_at": float(state.get("updated_at") or time.time()),
    }
    from core import database
    database.set_system_state("eventsub_state", payload)
    with _STATE_LOCK:
        _STATE_CACHE.clear()
        _STATE_CACHE.update(payload)


def is_live(login: str) -> bool | None:
    """Return True/False from cache, or None if unknown."""
    key = twitch_api.normalize_login(login)
    if not key:
        return None
    state = load_state()
    entry = (state.get("by_login") or {}).get(key)
    if not isinstance(entry, dict) or "live" not in entry:
        return None
    return bool(entry.get("live"))


def set_live_status(
    *,
    login: str = "",
    user_id: str = "",
    live: bool,
    event_type: str = "",
) -> None:
    login_key = twitch_api.normalize_login(login)
    uid = str(user_id or "").strip()
    now = time.time()
    state = load_state()
    by_login = dict(state.get("by_login") or {})
    by_user_id = dict(state.get("by_user_id") or {})

    if not login_key and uid:
        existing = by_user_id.get(uid) or {}
        login_key = twitch_api.normalize_login(existing.get("login") or "")

    entry = {
        "live": bool(live),
        "updated_at": now,
        "user_id": uid,
        "login": login_key,
        "event_type": str(event_type or ""),
    }
    if login_key:
        by_login[login_key] = entry
    if uid:
        by_user_id[uid] = entry
    save_state({"by_login": by_login, "by_user_id": by_user_id, "updated_at": now})


def verify_webhook_signature(
    *,
    secret: str,
    message_id: str,
    timestamp: str,
    body: bytes,
    signature_header: str,
    max_age_sec: int | None = None,
) -> bool:
    """Verify Twitch EventSub HMAC-SHA256 signature header."""
    secret = str(secret or "").strip()
    sig = str(signature_header or "").strip()
    if not secret or not sig or not message_id or not timestamp:
        return False
    if not sig.lower().startswith("sha256="):
        return False
    if max_age_sec is None:
        try:
            max_age_sec = int(os.environ.get("TWITCH_EVENTSUB_MAX_AGE_SEC", "0"))
        except (TypeError, ValueError):
            max_age_sec = 0
    if max_age_sec > 0:
        try:
            from datetime import datetime, timezone
            ts_clean = timestamp.rstrip("Z")
            ts_dt = datetime.fromisoformat(ts_clean).replace(tzinfo=timezone.utc)
            age = abs((datetime.now(timezone.utc) - ts_dt).total_seconds())
            if age > max_age_sec:
                logger.warning("EventSub message %s rejected: timestamp age %.1fs exceeds %ss", message_id, age, max_age_sec)
                return False
        except Exception as exc:
            logger.debug("EventSub timestamp check skipped: %s", exc)
    expected_hex = sig.split("=", 1)[1].strip().lower()
    message = f"{message_id}{timestamp}".encode() + (body or b"")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected_hex)


def build_signature_header(
    *,
    secret: str,
    message_id: str,
    timestamp: str,
    body: bytes,
) -> str:
    """Build a Twitch-style signature header (for tests / local fixtures)."""
    message = f"{message_id}{timestamp}".encode() + (body or b"")
    digest = hmac.new(str(secret).encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _helix_post(path: str, payload: dict[str, Any]) -> dict | None:
    token = twitch_api.get_app_access_token()
    client_id = twitch_api.TWITCH_CLIENT_ID
    if not token or not client_id:
        return None
    url = f"https://api.twitch.tv/helix/{path.lstrip('/')}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Client-Id": client_id,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HELIX_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except (OSError, http.client.HTTPException) as read_exc:
            # The HTTP status below is the result and is logged as a warning
            # on the next line; the unreadable body is only detail (debug).
            logger.debug("EventSub Helix POST %s: error body unreadable: %s", path, read_exc)
        logger.warning("EventSub Helix POST %s failed: HTTP %s %s", path, exc.code, detail)
        return None
    except Exception as exc:
        logger.warning("EventSub Helix POST %s failed: %s", path, exc)
        return None


def resolve_user_id(login: str) -> str:
    login = twitch_api.normalize_login(login)
    if not login:
        return ""
    data = twitch_api.helix_get("users", [("login", login)])
    if not data:
        return ""
    rows = data.get("data") or []
    if not rows:
        return ""
    return str(rows[0].get("id") or "").strip()


def create_stream_subscription(login: str, event_type: str) -> dict | None:
    """Create a Helix EventSub subscription for stream.online or stream.offline."""
    event_type = str(event_type or "").strip()
    if event_type not in ("stream.online", "stream.offline"):
        raise ValueError(f"unsupported event type: {event_type}")
    if not EVENTSUB_SECRET or not EVENTSUB_CALLBACK_URL:
        logger.warning("EventSub create skipped: secret/callback not configured")
        return None
    user_id = resolve_user_id(login)
    if not user_id:
        logger.warning("EventSub create skipped: no user id for %s", login)
        return None
    payload = {
        "type": event_type,
        "version": "1",
        "condition": {"broadcaster_user_id": user_id},
        "transport": {
            "method": "webhook",
            "callback": EVENTSUB_CALLBACK_URL,
            "secret": EVENTSUB_SECRET,
        },
    }
    result = _helix_post("eventsub/subscriptions", payload)
    if result:
        logger.info("EventSub subscribed %s for %s (user_id=%s)", event_type, login, user_id)
    return result


def create_stream_online_subscription(login: str) -> dict | None:
    return create_stream_subscription(login, "stream.online")


def create_stream_offline_subscription(login: str) -> dict | None:
    return create_stream_subscription(login, "stream.offline")


def create_stream_subscriptions(login: str) -> dict[str, Any]:
    """Create both online and offline subscriptions for a login."""
    return {
        "online": create_stream_online_subscription(login),
        "offline": create_stream_offline_subscription(login),
    }


def handle_webhook_payload(
    *,
    body: bytes,
    headers: dict[str, str],
    secret: str | None = None,
) -> tuple[int, str | bytes, str]:
    """Process an EventSub webhook request.

    Returns (status_code, response_body, content_type).
    """
    secret = (secret if secret is not None else EVENTSUB_SECRET) or ""
    message_id = str(headers.get("Twitch-Eventsub-Message-Id") or headers.get("twitch-eventsub-message-id") or "")
    timestamp = str(
        headers.get("Twitch-Eventsub-Message-Timestamp")
        or headers.get("twitch-eventsub-message-timestamp")
        or ""
    )
    signature = str(
        headers.get("Twitch-Eventsub-Message-Signature")
        or headers.get("twitch-eventsub-message-signature")
        or ""
    )
    message_type = str(
        headers.get("Twitch-Eventsub-Message-Type")
        or headers.get("twitch-eventsub-message-type")
        or ""
    ).strip().lower()

    if not verify_webhook_signature(
        secret=secret,
        message_id=message_id,
        timestamp=timestamp,
        body=body or b"",
        signature_header=signature,
    ):
        return 403, "invalid signature", "text/plain"

    try:
        payload = json.loads((body or b"{}").decode("utf-8"))
    except Exception:
        return 400, "invalid json", "text/plain"

    if message_type == "webhook_callback_verification":
        challenge = str(payload.get("challenge") or "")
        if not challenge:
            return 400, "missing challenge", "text/plain"
        return 200, challenge, "text/plain"

    if message_type == "revocation":
        return 204, "", "text/plain"

    if message_type != "notification":
        return 400, "unsupported message type", "text/plain"

    subscription = payload.get("subscription") or {}
    event = payload.get("event") or {}
    event_type = str(subscription.get("type") or "").strip()
    login = twitch_api.normalize_login(
        event.get("broadcaster_user_login") or event.get("user_login") or ""
    )
    user_id = str(event.get("broadcaster_user_id") or event.get("user_id") or "").strip()

    if event_type == "stream.online":
        set_live_status(login=login, user_id=user_id, live=True, event_type=event_type)
    elif event_type == "stream.offline":
        set_live_status(login=login, user_id=user_id, live=False, event_type=event_type)
    else:
        logger.info("EventSub notification ignored type=%s", event_type)

    return 204, "", "text/plain"
