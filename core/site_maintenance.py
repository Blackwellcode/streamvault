"""Runtime maintenance / WIP gate for StreamVault.

Persists in META_DIR so admins can toggle without restarting the process.
Env ``MAINTENANCE_MODE`` is only a bootstrap default when no state file exists.
Supports global maintenance as well as granular scopes:
- vod: VOD playback and archive
- live: Live streams and Twitch viewer
- mobile: Mobile version
Also supports a whitelist of bypass usernames (e.g. gatekeeper_streamvault).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from core import config

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE = None
_CACHE_MTIME = None

DEFAULT_BYPASS_USERS = ["gatekeeper_streamvault"]
VALID_SCOPES = ("vod", "live", "mobile")


def state_path():
    return os.path.join(config.META_DIR, "maintenance.json")


def _bootstrap_enabled():
    return bool(getattr(config, "MAINTENANCE_MODE", False))


def _default_state():
    return {
        "enabled": _bootstrap_enabled(),
        "scopes": {scope: False for scope in VALID_SCOPES},
        "bypass_users": list(DEFAULT_BYPASS_USERS),
        "updated_at": 0,
        "updated_by": "",
        "discord_notified_at": 0,
    }


def _read_file():
    path = state_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("maintenance state unreadable: %s", exc)
    return None


def load_state():
    """Return current maintenance state (file or env bootstrap)."""
    global _CACHE, _CACHE_MTIME
    path = state_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    with _LOCK:
        if _CACHE is not None and mtime is not None and mtime == _CACHE_MTIME:
            return dict(_CACHE)
        data = _read_file()
        if data is None:
            state = _default_state()
        else:
            state = _default_state()
            state["enabled"] = bool(data.get("enabled"))
            raw_scopes = data.get("scopes")
            if isinstance(raw_scopes, dict):
                state["scopes"] = {
                    scope: bool(raw_scopes.get(scope)) for scope in VALID_SCOPES
                }
            raw_bypass = data.get("bypass_users")
            if isinstance(raw_bypass, list):
                state["bypass_users"] = [
                    str(u).strip() for u in raw_bypass if str(u).strip()
                ]
            if not state["bypass_users"]:
                state["bypass_users"] = list(DEFAULT_BYPASS_USERS)
            try:
                state["updated_at"] = float(data.get("updated_at") or 0)
            except (TypeError, ValueError):
                state["updated_at"] = 0
            state["updated_by"] = str(data.get("updated_by") or "")[:80]
            try:
                state["discord_notified_at"] = float(data.get("discord_notified_at") or 0)
            except (TypeError, ValueError):
                state["discord_notified_at"] = 0
        _CACHE = dict(state)
        _CACHE_MTIME = mtime
        return dict(state)


def is_enabled(scope: str = "global") -> bool:
    """Check if maintenance is active for a given scope.

    If global 'enabled' is True, all scopes return True.
    Otherwise, granular scopes ('vod', 'live', 'mobile') are checked individually.
    """
    state = load_state()
    if state.get("enabled"):
        return True
    if scope and scope != "global":
        return bool(state.get("scopes", {}).get(scope, False))
    return False


def is_scope_enabled(scope: str) -> bool:
    """Check if a specific scope is enabled, independent of global state."""
    state = load_state()
    return bool(state.get("scopes", {}).get(scope, False))


def is_user_exempt(username: str | None, role: str = "user") -> bool:
    """Check if a user is exempt from maintenance mode (admin or in bypass list)."""
    if str(role or "").lower() == "admin":
        return True
    if not username:
        return False
    state = load_state()
    user_lower = str(username).strip().lower()
    exempt_list = [str(u).lower() for u in state.get("bypass_users", [])]
    return user_lower in exempt_list


def save_state(
    enabled=None,
    *,
    scopes=None,
    bypass_users=None,
    updated_by="",
    discord_notified_at=None,
):
    """Persist maintenance state and refresh cache."""
    global _CACHE, _CACHE_MTIME
    previous = load_state()
    state = dict(previous)
    if enabled is not None:
        state["enabled"] = bool(enabled)
    if scopes is not None and isinstance(scopes, dict):
        current_scopes = dict(state.get("scopes") or {})
        for k in VALID_SCOPES:
            if k in scopes:
                current_scopes[k] = bool(scopes[k])
        state["scopes"] = current_scopes
    if bypass_users is not None and isinstance(bypass_users, list):
        state["bypass_users"] = [
            str(u).strip() for u in bypass_users if str(u).strip()
        ]
        if not state["bypass_users"]:
            state["bypass_users"] = list(DEFAULT_BYPASS_USERS)
    state["updated_at"] = time.time()
    if updated_by:
        state["updated_by"] = str(updated_by)[:80]
    if discord_notified_at is not None:
        state["discord_notified_at"] = float(discord_notified_at)

    path = state_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp"
    with _LOCK:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o660)
        except OSError:
            pass
        try:
            _CACHE_MTIME = os.path.getmtime(path)
        except OSError:
            _CACHE_MTIME = None
        _CACHE = dict(state)
    return dict(state)


def set_enabled(enabled, *, updated_by="", notify_discord=False):
    """Enable/disable global maintenance."""
    _ = notify_discord
    was_enabled = is_enabled("global")
    want = bool(enabled)
    changing = want != was_enabled
    state = save_state(want, updated_by=updated_by)
    return {
        "state": state,
        "changed": changing,
        "discord_ok": None,
        "discord_error": "",
    }
