"""Helpers for Twitch IRC moderation / system chat events."""

from __future__ import annotations

import re

_CLEARCHAT_RE = re.compile(
    r"(?:^|\s)CLEARCHAT\s+#\S+(?:\s+:([^\r\n]+))?$",
    re.IGNORECASE,
)
_CLEARMSG_RE = re.compile(
    r"(?:^|\s)CLEARMSG\s+#\S+(?:\s+:([\s\S]*))?$",
    re.IGNORECASE,
)
_ALLOWED_EVENT_TYPES = frozenset({"ban", "timeout", "delete", "clear"})


def format_timeout_duration(seconds: int) -> str:
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        total = 0
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, rem = divmod(total, 60)
        return f"{minutes}m" if rem == 0 else f"{minutes}m {rem}s"
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def format_moderation_message(event_type: str, user: str = "", seconds: int | None = None) -> str:
    etype = str(event_type or "").strip().lower()
    name = str(user or "").strip()
    if etype == "timeout":
        dur = format_timeout_duration(seconds or 0)
        if name:
            return f"{name} wurde für {dur} getimeoutet"
        return f"Timeout ({dur})"
    if etype == "ban":
        return f"{name} wurde gebannt" if name else "Nutzer wurde gebannt"
    if etype == "delete":
        return f"Nachricht von {name} wurde gelöscht" if name else "Nachricht wurde gelöscht"
    if etype == "clear":
        return "Chat wurde geleert"
    return name


def sanitize_event(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    etype = str(raw.get("type") or "").strip().lower()
    if etype not in _ALLOWED_EVENT_TYPES:
        return None
    out = {"type": etype}
    if etype == "timeout":
        try:
            seconds = int(raw.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0
        if seconds > 0:
            out["seconds"] = seconds
    return out


def parse_clearchat(tags: dict | None, line: str) -> dict | None:
    """Parse Twitch CLEARCHAT into a chat-log payload fragment."""
    match = _CLEARCHAT_RE.search(line or "")
    if not match:
        return None
    user = str(match.group(1) or "").strip()
    tags = tags if isinstance(tags, dict) else {}
    raw_duration = tags.get("ban-duration")
    if user:
        if raw_duration is not None and str(raw_duration).strip() != "":
            try:
                seconds = max(0, int(raw_duration))
            except (TypeError, ValueError):
                seconds = 0
            event = {"type": "timeout", "seconds": seconds} if seconds > 0 else {"type": "ban"}
            if event["type"] == "timeout":
                msg = format_moderation_message("timeout", user, seconds)
            else:
                msg = format_moderation_message("ban", user)
        else:
            event = {"type": "ban"}
            msg = format_moderation_message("ban", user)
    else:
        event = {"type": "clear"}
        msg = format_moderation_message("clear")
        user = ""
    return {"user": user, "msg": msg, "event": event}


def parse_clearmsg(tags: dict | None, line: str) -> dict | None:
    """Parse Twitch CLEARMSG (single-message delete) into a chat-log payload."""
    match = _CLEARMSG_RE.search(line or "")
    if not match:
        return None
    tags = tags if isinstance(tags, dict) else {}
    user = str(tags.get("login") or "").strip()
    deleted = str(match.group(1) or "").strip()
    msg = format_moderation_message("delete", user)
    if deleted:
        # Keep a short preview so VOD chat context stays useful.
        preview = " ".join(deleted.split())
        if len(preview) > 120:
            preview = preview[:117] + "..."
        msg = f"{msg}: {preview}"
    return {"user": user, "msg": msg, "event": {"type": "delete"}}
