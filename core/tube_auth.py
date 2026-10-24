#!/usr/bin/env python3
"""StreamVault authentication & RBAC integration helpers.

Note: Direct Discord registration bot is decommissioned.
Account creation and role assignments are managed via WebUI RBAC (/admin/users).
Legacy helpers are maintained for internal test suite compatibility.
"""

from __future__ import annotations

import os
import re
import secrets
import string
import time
from typing import Any

from werkzeug.security import generate_password_hash

from core import database

VALID_ROLES = {"admin", "viewer", "user", "member", "live_only"}
MEMBER_ROLE = "member"
LIVE_ONLY_ROLE = "live_only"
DEFAULT_REGISTER_ROLE = MEMBER_ROLE
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")
RESERVED_USERNAMES = {"admin", "root", "system", "anonymous", "guest", "bot", "discord"}
MIN_PASSWORD_LENGTH = 8
LOGIN_URL = (os.environ.get("VOD_PUBLIC_BASE") or "http://localhost:5000").rstrip("/") + "/login"

# UI labels
ROLE_LABELS = {
    "member": "Member",
    "live_only": "Live-only",
    "viewer": "Viewer",
    "user": "Viewer",
    "admin": "Admin",
}

# Seeded vod access bases (empty by default; configured per installation)
_VOD_ACCESS_SEED_BASES = frozenset()


def role_label(role: str | None) -> str:
    key = normalize_role(role, default="viewer")
    return ROLE_LABELS.get(key, key)


def is_member(role: str | None) -> bool:
    return str(role or "").strip().lower() == MEMBER_ROLE


def is_live_only(role: str | None) -> bool:
    return str(role or "").strip().lower() == LIVE_ONLY_ROLE


def role_can_access_vods(role: str | None) -> bool:
    """False for Live-only — may watch live streams, not archives/VODs/clips."""
    return not is_live_only(role)


def normalize_role(role: str | None, *, default: str = "viewer") -> str:
    value = str(role or default).strip().lower() or default
    if value == "user":
        return "viewer"
    if value not in VALID_ROLES:
        return default
    return value


def _slug_base(slug: str) -> str:
    return str(slug or "").strip().rstrip("_").lower()


def _expand_access_slugs(raw_slugs) -> set[str]:
    """Expand stored bases into common archive aliases."""
    slugs: set[str] = set()
    for raw in raw_slugs or []:
        text = str(raw or "").strip()
        if not text:
            continue
        base = _slug_base(text)
        if not base:
            continue
        slugs.add(base)
        slugs.add(f"{base}_")
        slugs.add(text)
        slugs.add(text.lower())
    return {s for s in slugs if s}


def list_default_vod_access_slugs() -> list[str]:
    try:
        rows = database.list_role_vod_access(MEMBER_ROLE)
    except Exception:
        rows = []
    if rows:
        return list(rows)
    return sorted(_VOD_ACCESS_SEED_BASES)


def effective_vod_access_bases(role: str | None = None) -> list[str]:
    """Stored allowlist bases for a role (Member by default)."""
    role_key = normalize_role(role or MEMBER_ROLE, default=MEMBER_ROLE)
    if role_key != MEMBER_ROLE:
        # Only Member uses a restricted VOD allowlist.
        return []
    try:
        rows = database.list_role_vod_access(role_key)
        if rows:
            return rows
    except Exception:
        pass
    return list_default_vod_access_slugs()


def member_related_slugs(role: str | None = None) -> set[str]:
    """Slugs the Member role may access."""
    return _expand_access_slugs(effective_vod_access_bases(role or MEMBER_ROLE))


def streamer_allowed_for_member(
    streamer: str,
    related_slugs: list[str] | None = None,
    username: str | None = None,
    role: str | None = None,
) -> bool:
    # username kept for call-site compatibility; allowlist is per role.
    _ = username
    allow = member_related_slugs(role or MEMBER_ROLE)
    allow_bases = {_slug_base(s) for s in allow}
    candidates = set(related_slugs or [])
    raw = str(streamer or "").strip()
    if raw:
        candidates.add(raw)
        candidates.add(raw.rstrip("_"))
        candidates.add(f"{raw.rstrip('_')}_")
    for slug in candidates:
        if not slug:
            continue
        if slug in allow or _slug_base(slug) in allow_bases:
            return True
    return False


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    # Avoid ambiguous characters for DM copy/paste.
    alphabet = alphabet.replace("O", "").replace("0", "").replace("l", "").replace("I", "").replace("1", "")
    return "".join(secrets.choice(alphabet) for _ in range(max(MIN_PASSWORD_LENGTH, length)))


def sanitize_discord_username(raw: str) -> str:
    text = str(raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_-")
    if len(text) < 3:
        text = (text + "user")[:3]
    return text[:24] or "user"


def allocate_username(discord_username: str, discord_id: str) -> str:
    base = sanitize_discord_username(discord_username)
    if base.lower() in RESERVED_USERNAMES:
        base = f"u_{base}"[:24]
    candidate = base[:32]
    if USERNAME_RE.match(candidate) and not database.get_user(candidate):
        return candidate
    suffix = str(discord_id)[-4:]
    candidate = f"{base[:27]}_{suffix}"[:32]
    if USERNAME_RE.match(candidate) and not database.get_user(candidate):
        return candidate
    for _ in range(20):
        candidate = f"{base[:20]}_{secrets.token_hex(2)}"[:32]
        if USERNAME_RE.match(candidate) and not database.get_user(candidate):
            return candidate
    raise RuntimeError("Kein freier Username verfügbar.")


def validate_chosen_username(raw: str) -> str:
    """Validate a user-picked Tube username. Returns normalized username or raises ValueError."""
    username = str(raw or "").strip()
    if not username:
        raise ValueError("Username ist erforderlich.")
    if not USERNAME_RE.match(username):
        raise ValueError(
            "Username muss 3–32 Zeichen sein: nur Buchstaben, Zahlen, `_` und `-`."
        )
    if username.lower() in RESERVED_USERNAMES:
        raise ValueError(f"Username `{username}` ist reserviert.")
    existing = database.get_user(username)
    if existing:
        raise ValueError(f"Username `{username}` ist bereits vergeben.")
    return username


def register_from_discord(
    discord_id: str,
    discord_username: str,
    *,
    chosen_username: str | None = None,
    role: str | None = None,
    note: str | None = None,
    rules_accepted: bool = False,
    rules_accept_input: str | None = None,
) -> dict[str, Any]:
    """Create a pending Tube account bound to a Discord user.

    Returns dict with username, password (plaintext once), role, active=False.
    Raises ValueError on conflict / validation errors.
    """
    from core import tube_rules
    from core import tube_terms

    discord_id = str(discord_id or "").strip()
    if not discord_id.isdigit():
        raise ValueError("Ungültige Discord-ID.")
    existing = database.get_user_by_discord_id(discord_id)
    if existing:
        raise ValueError(
            f"Discord-Account bereits registriert als `{existing['username']}` "
            f"({'aktiv' if existing.get('active') else 'wartet auf Freigabe'})."
        )

    if rules_accept_input is not None or rules_accepted:
        tube_rules.validate_accept_input(rules_accept_input if rules_accept_input is not None else (
            tube_rules.accept_phrase() if rules_accepted else ""
        ))
    else:
        raise ValueError(
            f"AGB inkl. Nutzungsregeln nicht akzeptiert. Bitte `{tube_rules.accept_phrase()}` eingeben "
            f"(siehe #regeln / #agb / {tube_terms.LOGIN_AGB_HINT})."
        )

    if chosen_username is not None and str(chosen_username).strip():
        username = validate_chosen_username(chosen_username)
    else:
        username = allocate_username(discord_username, discord_id)
    password = generate_password()
    role_n = normalize_role(role or DEFAULT_REGISTER_ROLE, default=DEFAULT_REGISTER_ROLE)
    if role_n == "admin":
        role_n = DEFAULT_REGISTER_ROLE
    note_clean = " ".join(str(note or "").split())[:300]
    now = time.time()

    database.upsert_user(
        username,
        generate_password_hash(password),
        role=role_n,
        active=False,
        discord_id=discord_id,
        discord_username=str(discord_username or "")[:80],
        created_via="discord",
        register_note=note_clean or None,
        rules_accepted_at=now,
        rules_version=tube_rules.current_version(),
        terms_accepted_at=now,
        terms_version=tube_terms.current_version(),
    )
    row = database.get_user(username) or {}
    database.add_terms_acceptance(
        username=username,
        discord_id=discord_id,
        terms_version=tube_terms.current_version(),
        rules_version=tube_rules.current_version(),
        accepted_at=now,
        source="discord",
    )
    return {
        "username": username,
        "password": password,
        "role": role_n,
        "active": False,
        "discord_id": discord_id,
        "discord_username": row.get("discord_username") or discord_username,
        "register_note": row.get("register_note") or note_clean,
        "rules_accepted_at": row.get("rules_accepted_at") or now,
        "rules_version": row.get("rules_version") or tube_rules.current_version(),
        "terms_accepted_at": row.get("terms_accepted_at") or now,
        "terms_version": row.get("terms_version") or tube_terms.current_version(),
        "created_at": row.get("created_at") or now,
    }


def approve_by_discord_id(discord_id: str) -> dict[str, Any]:
    row = database.get_user_by_discord_id(str(discord_id).strip())
    if not row:
        raise ValueError("Kein Account für diese Discord-ID.")
    return approve_username(row["username"])


def approve_username(username: str) -> dict[str, Any]:
    from core import tube_rules
    from core import tube_terms

    row = database.get_user(username)
    if not row:
        raise ValueError("Benutzer nicht gefunden.")
    if not tube_terms.user_has_accepted_current_consent(row):
        raise ValueError(
            f"AGB (Version {tube_terms.current_version()}) inkl. Nutzungsregeln "
            f"({tube_rules.current_version()}) noch nicht akzeptiert — "
            "Freigabe erst nach Zustimmung auf der Website oder bei erneuter Registrierung."
        )
    database.set_user_active(row["username"], True)
    role = normalize_role(row.get("role"), default=DEFAULT_REGISTER_ROLE)
    if role not in ("admin", "viewer", MEMBER_ROLE, LIVE_ONLY_ROLE):
        role = DEFAULT_REGISTER_ROLE
        database.set_user_role(row["username"], role)
    updated = database.get_user(row["username"]) or row
    return dict(updated)


def deny_username(username: str, *, delete: bool = True) -> dict[str, Any]:
    row = database.get_user(username)
    if not row:
        raise ValueError("Benutzer nicht gefunden.")
    if delete:
        database.delete_user(row["username"])
    else:
        database.set_user_active(row["username"], False)
    return dict(row)


def deny_by_discord_id(discord_id: str, *, delete: bool = True) -> dict[str, Any]:
    row = database.get_user_by_discord_id(str(discord_id).strip())
    if not row:
        raise ValueError("Kein Account für diese Discord-ID.")
    return deny_username(row["username"], delete=delete)


def reset_password_for_discord(discord_id: str) -> dict[str, Any]:
    row = database.get_user_by_discord_id(str(discord_id).strip())
    if not row:
        raise ValueError("Kein Account für diese Discord-ID.")
    password = generate_password()
    database.set_user_password(row["username"], generate_password_hash(password))
    return {
        "username": row["username"],
        "password": password,
        "active": bool(row.get("active")),
        "role": row.get("role"),
    }


def status_for_discord(discord_id: str) -> dict[str, Any] | None:
    from core import tube_terms

    row = database.get_user_by_discord_id(str(discord_id).strip())
    if not row:
        return None

    consent = tube_terms.status_for_user_row(row)
    return {
        "username": row["username"],
        "active": bool(row.get("active")),
        "role": normalize_role(row.get("role")),
        "discord_username": row.get("discord_username") or "",
        "created_at": row.get("created_at"),
        "rules_ok": consent["rules_ok"],
        "rules_version": consent["rules_version"],
        "rules_required_version": consent["rules_required_version"],
        "terms_ok": consent["terms_ok"],
        "terms_version": consent["terms_version"],
        "terms_required_version": consent["required_version"],
        "consent_ok": consent["consent_ok"],
    }


def accept_rules_for_discord(discord_id: str, *, accept_input: str | None = None) -> dict[str, Any]:
    """Record current AGB + Hausordnung acceptance for an existing Discord-linked account."""
    from core import tube_rules
    from core import tube_terms

    if accept_input is not None:
        tube_rules.validate_accept_input(accept_input)
    row = database.get_user_by_discord_id(str(discord_id or "").strip())
    if not row:
        raise ValueError("Kein Account für diese Discord-ID.")
    updated = tube_terms.record_terms_acceptance(
        row["username"],
        source="discord",
        discord_id=str(discord_id or ""),
    )
    return dict(updated or row)


def credentials_message(username: str, password: str, *, active: bool) -> str:
    state = "freigeschaltet" if active else "wartet auf Freigabe durch einen Administrator"
    return (
        f"**Stream Vault Account**\n"
        f"Login: {LOGIN_URL}\n"
        f"Username: `{username}`\n"
        f"Passwort: `{password}`\n"
        f"Status: {state}\n"
        f"Zugang: Standard-VODs (nach Freigabe).\n"
        f"Passwort sicher speichern — es wird nur einmal angezeigt."
    )
