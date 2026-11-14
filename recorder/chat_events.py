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
_ALLOWED_EVENT_TYPES = frozenset({"ban", "timeout", "delete", "clear", "notice"})

# --- USERNOTICE (subs, gifts, raids, announcements, ...) -------------------
_NOTICE_KIND_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_NOTICE_PLANS = frozenset({"Prime", "1000", "2000", "3000"})
_NOTICE_COLORS = frozenset({"PRIMARY", "BLUE", "GREEN", "ORANGE", "PURPLE"})
# Upper bounds keep a forged or corrupt log line from carrying absurd numbers.
_NOTICE_INT_CAPS = {
    "months": 1200,
    "streak": 1200,
    "multimonth": 120,
    "gift_months": 120,
    "sender_count": 1_000_000,
    "count": 100_000_000,
}
_NOTICE_TEXT_FIELDS = ("recipient", "other", "charity", "amount")
_NOTICE_TEXT_MAX = 80
_NOTICE_SYSTEM_MAX = 300
_ANON_GIFTER_LOGIN = "ananonymousgifter"
_ANON_KINDS = frozenset({"anonsubgift", "anonsubmysterygift"})

# --- PRIVMSG meta (bits, highlight, first message, reply, power-ups) -------
_META_POWER_BY_MSG_ID = {
    "gigantified-emote-message": "gigantified",
    "animated-message": "animated",
}
_META_POWERS = frozenset(_META_POWER_BY_MSG_ID.values())
_META_BITS_CAP = 10_000_000
_META_REPLY_USER_MAX = 80
_META_REPLY_MSG_MAX = 140


def irc_command(line: str) -> str:
    """Return the IRC command of a raw line (``PRIVMSG``, ``USERNOTICE``, ``PING`` ...).

    Skips the IRCv3 tag block (``@...``) and the source prefix (``:...``), so
    text inside a chat message can never be mistaken for a command.
    """
    rest = str(line or "").strip("\r\n").lstrip(" ")
    if rest.startswith("@"):
        _tags, _sep, rest = rest.partition(" ")
        rest = rest.lstrip(" ")
    if rest.startswith(":"):
        _prefix, _sep, rest = rest.partition(" ")
        rest = rest.lstrip(" ")
    command, _sep, _params = rest.partition(" ")
    return command.upper()


def _clean_text(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _positive_int(value, cap: int) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return min(number, cap)


def _charity_amount(tags: dict) -> str:
    raw_amount = _positive_int(tags.get("msg-param-donation-amount"), 10**12)
    if raw_amount is None:
        return ""
    try:
        exponent = int(str(tags.get("msg-param-exponent") or "0").strip())
    except (TypeError, ValueError):
        exponent = 0
    exponent = max(0, min(exponent, 6))
    value = raw_amount / (10**exponent)
    amount = f"{value:.{exponent}f}"
    currency = _clean_text(tags.get("msg-param-donation-currency"), 8).upper()
    if currency and re.fullmatch(r"[A-Z]{3}", currency):
        return f"{amount} {currency}"
    return amount


def parse_usernotice(tags: dict | None, message: str = "") -> dict | None:
    """Turn a Twitch USERNOTICE into a chat-log payload.

    Returns ``{"user", "msg", "event"}`` where ``msg`` is the user's optional
    text and ``event`` is a sanitized ``{"type": "notice", "kind": ...}`` dict,
    or None when the notice has no usable msg-id.
    """
    tags = tags if isinstance(tags, dict) else {}
    kind = str(tags.get("msg-id") or "").strip().lower()
    if kind == "sharedchatnotice":
        kind = str(tags.get("source-msg-id") or "").strip().lower()
    if not _NOTICE_KIND_RE.match(kind):
        return None
    login = str(tags.get("login") or "").strip()
    user = str(tags.get("display-name") or "").strip() or login
    event: dict = {"type": "notice", "kind": kind}

    plan = str(tags.get("msg-param-sub-plan") or "").strip()
    if plan:
        event["plan"] = plan

    if kind in ("sub", "resub"):
        event["months"] = tags.get("msg-param-cumulative-months")
        if str(tags.get("msg-param-should-share-streak") or "").strip() == "1":
            event["streak"] = tags.get("msg-param-streak-months")
        event["multimonth"] = tags.get("msg-param-multimonth-duration")
    elif kind in ("subgift", "anonsubgift"):
        event["recipient"] = tags.get("msg-param-recipient-display-name") or tags.get(
            "msg-param-recipient-user-name"
        )
        event["gift_months"] = tags.get("msg-param-gift-months")
        event["months"] = tags.get("msg-param-months")
        event["sender_count"] = tags.get("msg-param-sender-count")
    elif kind in ("submysterygift", "anonsubmysterygift"):
        event["count"] = tags.get("msg-param-mass-gift-count")
        event["sender_count"] = tags.get("msg-param-sender-count")
    elif kind == "giftpaidupgrade":
        event["other"] = tags.get("msg-param-sender-name") or tags.get("msg-param-sender-login")
    elif kind in ("standardpayforward", "communitypayforward"):
        prior_anonymous = str(tags.get("msg-param-prior-gifter-anonymous") or "").strip().lower()
        if prior_anonymous != "true":
            event["other"] = tags.get("msg-param-prior-gifter-display-name")
        event["recipient"] = tags.get("msg-param-recipient-display-name")
    elif kind == "raid":
        user = str(tags.get("msg-param-displayName") or "").strip() or str(
            tags.get("msg-param-login") or ""
        ).strip() or user
        event["count"] = tags.get("msg-param-viewerCount")
    elif kind == "bitsbadgetier":
        event["count"] = tags.get("msg-param-threshold")
    elif kind == "announcement":
        event["color"] = str(tags.get("msg-param-color") or "").strip().upper()
    elif kind == "viewermilestone":
        event["count"] = tags.get("msg-param-value")
        event["other"] = tags.get("msg-param-category")
    elif kind == "charitydonation":
        event["charity"] = tags.get("msg-param-charity-name")
        event["amount"] = _charity_amount(tags)

    if login.lower() == _ANON_GIFTER_LOGIN or kind in _ANON_KINDS:
        event["anon"] = True
    system = str(tags.get("system-msg") or "")
    if system:
        event["system"] = system

    clean = sanitize_event(event)
    if not clean:
        return None
    msg = str(message or "")
    if msg.startswith("\x01ACTION "):
        msg = msg[len("\x01ACTION "):].removesuffix("\x01")
    return {"user": user, "msg": msg.strip(), "event": clean}


def _sanitize_notice(raw: dict) -> dict | None:
    kind = str(raw.get("kind") or "").strip().lower()
    if not _NOTICE_KIND_RE.match(kind):
        return None
    out: dict = {"type": "notice", "kind": kind}
    plan = str(raw.get("plan") or "").strip()
    if plan in _NOTICE_PLANS:
        out["plan"] = plan
    color = str(raw.get("color") or "").strip().upper()
    if color in _NOTICE_COLORS:
        out["color"] = color
    for field, cap in _NOTICE_INT_CAPS.items():
        number = _positive_int(raw.get(field), cap)
        if number is not None:
            out[field] = number
    for field in _NOTICE_TEXT_FIELDS:
        text = _clean_text(raw.get(field), _NOTICE_TEXT_MAX)
        if text:
            out[field] = text
    if raw.get("anon") is True:
        out["anon"] = True
    system = _clean_text(raw.get("system"), _NOTICE_SYSTEM_MAX)
    if system:
        out["system"] = system
    return out


def parse_privmsg_meta(tags: dict | None) -> dict | None:
    """Extract Twitch-only PRIVMSG decorations (bits, highlight, first, reply, power-ups)."""
    tags = tags if isinstance(tags, dict) else {}
    meta: dict = {}
    bits = _positive_int(tags.get("bits"), _META_BITS_CAP)
    if bits is not None:
        meta["bits"] = bits
    msg_id = str(tags.get("msg-id") or "").strip().lower()
    if msg_id == "highlighted-message":
        meta["highlight"] = True
    if msg_id in _META_POWER_BY_MSG_ID:
        meta["power"] = _META_POWER_BY_MSG_ID[msg_id]
    if str(tags.get("first-msg") or "").strip() == "1":
        meta["first"] = True
    parent_user = str(
        tags.get("reply-parent-display-name") or tags.get("reply-parent-user-login") or ""
    ).strip()
    parent_msg = str(tags.get("reply-parent-msg-body") or "")
    if parent_user or parent_msg.strip():
        meta["reply"] = {"user": parent_user, "msg": parent_msg}
    return sanitize_meta(meta)


def sanitize_meta(raw) -> dict | None:
    """Allowlist filter for the optional ``meta`` block of a chat-log entry."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    bits = _positive_int(raw.get("bits"), _META_BITS_CAP)
    if bits is not None:
        out["bits"] = bits
    if raw.get("highlight") is True:
        out["highlight"] = True
    if raw.get("first") is True:
        out["first"] = True
    power = str(raw.get("power") or "").strip().lower()
    if power in _META_POWERS:
        out["power"] = power
    reply = raw.get("reply")
    if isinstance(reply, dict):
        reply_user = _clean_text(reply.get("user"), _META_REPLY_USER_MAX)
        reply_msg = _clean_text(reply.get("msg"), _META_REPLY_MSG_MAX)
        if reply_user or reply_msg:
            clean_reply = {}
            if reply_user:
                clean_reply["user"] = reply_user
            if reply_msg:
                clean_reply["msg"] = reply_msg
            out["reply"] = clean_reply
    return out or None


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
    if etype == "notice":
        return _sanitize_notice(raw)
    out: dict = {"type": etype}
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
