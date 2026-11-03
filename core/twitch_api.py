#!/usr/bin/env python3
"""Server-side Twitch Helix/GQL helpers for StreamVault live viewing.

Secrets never leave this module toward the browser. Discovery uses Helix
Client Credentials when configured; monitored live status can fall back to
the public Twitch GQL endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from flask_babel import gettext

GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", GQL_CLIENT_ID).strip()
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_APP_ACCESS_TOKEN = (
    os.environ.get("TWITCH_APP_ACCESS_TOKEN", "").strip()
    or os.environ.get("TWITCH_ACCESS_TOKEN", "").strip()
)
PUBLIC_BASE = os.environ.get("VOD_PUBLIC_BASE", "").strip()
TWITCH_EMBED_PARENTS_RAW = os.environ.get("TWITCH_EMBED_PARENTS", "").strip()

STREAMS_CACHE_TTL_SEC = float(os.environ.get("TWITCH_STREAMS_CACHE_TTL_SEC", "45"))
SEARCH_CACHE_TTL_SEC = float(os.environ.get("TWITCH_SEARCH_CACHE_TTL_SEC", "90"))
REWARDS_CACHE_TTL_SEC = float(os.environ.get("TWITCH_REWARDS_CACHE_TTL_SEC", "600"))
HELIX_TIMEOUT_SEC = float(os.environ.get("TWITCH_HELIX_TIMEOUT_SEC", "8"))
GQL_TIMEOUT_SEC = float(os.environ.get("TWITCH_GQL_TIMEOUT_SEC", "8"))
# HELIX_MAX_RETRIES — extra attempts after the first failure (default 3)
# HELIX_RETRY_BACKOFF_SEC — base backoff for 5xx / fallback when Retry-After missing (default 5)
HELIX_MAX_RETRIES = max(0, int(os.environ.get("HELIX_MAX_RETRIES", "3")))
HELIX_RETRY_BACKOFF_SEC = max(0.5, float(os.environ.get("HELIX_RETRY_BACKOFF_SEC", "5")))
HELIX_STREAMS_CHUNK = 100

_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_RATE_LIMIT_UNTIL = 0.0
_RATE_LIMIT_LOCK = threading.Lock()

logger = logging.getLogger(__name__)


def reload_env():
    """Refresh module-level config from environment (used by tests)."""
    global TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_APP_ACCESS_TOKEN
    global PUBLIC_BASE, TWITCH_EMBED_PARENTS_RAW
    global HELIX_MAX_RETRIES, HELIX_RETRY_BACKOFF_SEC, _RATE_LIMIT_UNTIL
    TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", GQL_CLIENT_ID).strip()
    TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    TWITCH_APP_ACCESS_TOKEN = (
        os.environ.get("TWITCH_APP_ACCESS_TOKEN", "").strip()
        or os.environ.get("TWITCH_ACCESS_TOKEN", "").strip()
    )
    PUBLIC_BASE = os.environ.get("VOD_PUBLIC_BASE", "").strip()
    TWITCH_EMBED_PARENTS_RAW = os.environ.get("TWITCH_EMBED_PARENTS", "").strip()
    HELIX_MAX_RETRIES = max(0, int(os.environ.get("HELIX_MAX_RETRIES", "3")))
    HELIX_RETRY_BACKOFF_SEC = max(0.5, float(os.environ.get("HELIX_RETRY_BACKOFF_SEC", "5")))
    with _CACHE_LOCK:
        _CACHE.clear()
        _TOKEN_CACHE["token"] = ""
        _TOKEN_CACHE["expires_at"] = 0.0
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_UNTIL = 0.0


def has_helix_auth() -> bool:
    return bool(TWITCH_APP_ACCESS_TOKEN or (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET))


def normalize_login(value: Any) -> str:
    login = str(value or "").strip().lower()
    login = login.removeprefix("@")
    if "twitch.tv/" in login:
        login = login.rstrip("/").split("twitch.tv/")[-1]
        login = login.split("?")[0].split("/")[0]
    return "".join(ch for ch in login if ch.isalnum() or ch == "_")[:40]


def thumbnail_url(login: str, width: int = 640, height: int = 360) -> str:
    login = normalize_login(login)
    if not login:
        return ""
    return (
        f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}"
        f"-{int(width)}x{int(height)}.jpg"
    )


def embed_parent_hosts(request_host: str | None = None) -> list[str]:
    """Hostnames allowed as Twitch embed parent= parameters (no scheme)."""
    hosts: list[str] = []

    def add(raw: str):
        value = str(raw or "").strip().lower()
        if not value:
            return
        if "://" in value:
            try:
                value = urllib.parse.urlparse(value).hostname or ""
            except Exception:
                value = ""
        value = value.split("/")[0].split(":")[0].strip().lower()
        if value and value not in hosts:
            hosts.append(value)

    if TWITCH_EMBED_PARENTS_RAW:
        for part in TWITCH_EMBED_PARENTS_RAW.split(","):
            add(part)
    add(PUBLIC_BASE)
    add(request_host or "")
    if not hosts:
        add("localhost")
    return hosts


def player_embed_url(login: str, parents: list[str] | None = None, autoplay: bool = True) -> str:
    login = normalize_login(login)
    parents = parents or embed_parent_hosts()
    params = [("channel", login), ("autoplay", "true" if autoplay else "false")]
    for parent in parents:
        params.append(("parent", parent))
    return "https://player.twitch.tv/?" + urllib.parse.urlencode(params)


def chat_embed_url(login: str, parents: list[str] | None = None) -> str:
    login = normalize_login(login)
    parents = parents or embed_parent_hosts()
    params = [("darkpopout", "")]
    for parent in parents:
        params.append(("parent", parent))
    query = urllib.parse.urlencode(params)
    return f"https://www.twitch.tv/embed/{urllib.parse.quote(login)}/chat?{query}"


def _cache_get(key: str):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        if time.time() < float(entry.get("expires_at", 0) or 0):
            return entry.get("value")
    return None


def _cache_get_stale(key: str):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        return entry.get("value")


def _cache_set(key: str, value: Any, ttl: float):
    with _CACHE_LOCK:
        _CACHE[key] = {"value": value, "expires_at": time.time() + max(1.0, float(ttl))}


def get_app_access_token() -> str:
    if TWITCH_APP_ACCESS_TOKEN:
        return TWITCH_APP_ACCESS_TOKEN
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return ""
    now = time.time()
    if _TOKEN_CACHE.get("token") and now < float(_TOKEN_CACHE.get("expires_at", 0) or 0):
        return str(_TOKEN_CACHE["token"])
    try:
        form = urllib.parse.urlencode({
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://id.twitch.tv/oauth2/token",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HELIX_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        token = str(data.get("access_token", "") or "")
        expires_in = int(data.get("expires_in", 0) or 0)
        if token:
            _TOKEN_CACHE["token"] = token
            _TOKEN_CACHE["expires_at"] = now + max(300, expires_in - 300)
            return token
    except Exception as exc:
        logger.warning("Could not obtain Twitch app access token: %s", exc)
    return ""


def _helix_retry_after_seconds(exc: urllib.error.HTTPError) -> float:
    """Parse Retry-After (seconds) or fall back to 5–15s jittered wait."""
    raw = ""
    try:
        raw = str(exc.headers.get("Retry-After") or "").strip()
    except Exception:
        raw = ""
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    base = HELIX_RETRY_BACKOFF_SEC
    return random.uniform(max(5.0, base), max(15.0, base + 10.0))


def _mark_rate_limited(seconds: float):
    global _RATE_LIMIT_UNTIL
    until = time.time() + max(0.0, float(seconds))
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_UNTIL = max(_RATE_LIMIT_UNTIL, until)


def _wait_if_circuit_open():
    """If recently rate-limited, delay briefly before attempting Helix again."""
    with _RATE_LIMIT_LOCK:
        until = float(_RATE_LIMIT_UNTIL or 0.0)
    wait = until - time.time()
    if wait <= 0:
        return
    # Cap single sleep so the caller is not stuck forever on a stale stamp.
    delay = min(wait, 15.0)
    logger.warning("Twitch Helix circuit open; delaying %.1fs before request", delay)
    time.sleep(delay)


def helix_get(path: str, params: list[tuple[str, str]] | dict[str, Any] | None = None) -> dict | None:
    token = get_app_access_token()
    if not token or not TWITCH_CLIENT_ID:
        return None
    if isinstance(params, dict):
        pairs: list[tuple[str, str]] = []
        for key, value in params.items():
            if isinstance(value, (list, tuple)):
                for item in value:
                    pairs.append((str(key), str(item)))
            elif value is not None:
                pairs.append((str(key), str(value)))
        params = pairs
    query = urllib.parse.urlencode(params or [])
    url = f"https://api.twitch.tv/helix/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"

    _wait_if_circuit_open()
    max_attempts = 1 + HELIX_MAX_RETRIES
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Client-Id": TWITCH_CLIENT_ID,
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=HELIX_TIMEOUT_SEC) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            if code == 429:
                sleep_for = _helix_retry_after_seconds(exc)
                _mark_rate_limited(sleep_for)
                logger.warning(
                    "Twitch Helix rate-limited (429) on %s; retry-after=%.1fs attempt=%s/%s",
                    path,
                    sleep_for,
                    attempt + 1,
                    max_attempts,
                )
                try:
                    exc.read()
                except Exception:
                    pass
                if attempt + 1 < max_attempts:
                    time.sleep(sleep_for + random.uniform(0.0, 0.5))
                    continue
                return None
            if 500 <= code < 600:
                sleep_for = HELIX_RETRY_BACKOFF_SEC * (2 ** attempt) + random.uniform(0.0, 0.5)
                logger.info(
                    "Twitch Helix server error %s on %s; backing off %.1fs attempt=%s/%s",
                    code,
                    path,
                    sleep_for,
                    attempt + 1,
                    max_attempts,
                )
                try:
                    exc.read()
                except Exception:
                    pass
                if attempt + 1 < max_attempts:
                    time.sleep(sleep_for)
                    continue
                return None
            logger.info("Twitch Helix request failed (%s): HTTP %s", path, code)
            try:
                exc.read()
            except Exception:
                pass
            return None
        except Exception as exc:
            logger.info("Twitch Helix request failed (%s): %s", path, exc)
            return None
    return None


def _empty_stream(login: str, *, monitored: bool = False, display_name: str = "") -> dict:
    login = normalize_login(login)
    return {
        "login": login,
        "user_login": login,
        "display_name": display_name or login,
        "user_name": display_name or login,
        "title": "",
        "game_name": "",
        "game_id": "",
        "viewer_count": 0,
        "thumbnail_url": thumbnail_url(login),
        "started_at": "",
        "is_live": False,
        "monitored": bool(monitored),
        "user_id": "",
        "language": "",
    }


def _normalize_helix_stream(item: dict, *, monitored: bool = False) -> dict:
    login = normalize_login(item.get("user_login") or item.get("login"))
    display = str(item.get("user_name") or item.get("display_name") or login)
    thumb = str(item.get("thumbnail_url") or "")
    if thumb:
        thumb = thumb.replace("{width}", "640").replace("{height}", "360")
    else:
        thumb = thumbnail_url(login)
    return {
        "login": login,
        "user_login": login,
        "display_name": display,
        "user_name": display,
        "title": str(item.get("title") or ""),
        "game_name": str(item.get("game_name") or ""),
        "game_id": str(item.get("game_id") or ""),
        "viewer_count": int(item.get("viewer_count") or 0),
        "thumbnail_url": thumb,
        "started_at": str(item.get("started_at") or ""),
        "is_live": True,
        "monitored": bool(monitored),
        "user_id": str(item.get("user_id") or ""),
        "language": str(item.get("language") or ""),
    }


def _normalize_gql_stream(login: str, stream: dict, *, monitored: bool = False) -> dict:
    login = normalize_login(login)
    game = stream.get("game") or {}
    return {
        "login": login,
        "user_login": login,
        "display_name": login,
        "user_name": login,
        "title": str(stream.get("title") or ""),
        "game_name": str(game.get("name") or ""),
        "game_id": str(game.get("id") or ""),
        "viewer_count": int(stream.get("viewersCount") or 0),
        "thumbnail_url": thumbnail_url(login),
        "started_at": str(stream.get("createdAt") or ""),
        "is_live": True,
        "monitored": bool(monitored),
        "user_id": "",
        "language": "",
    }


def _helix_streams_by_logins(logins: list[str]) -> dict[str, dict] | None:
    if not has_helix_auth():
        return None
    result: dict[str, dict] = {}
    unique = []
    seen = set()
    for login in logins:
        norm = normalize_login(login)
        if norm and norm not in seen:
            seen.add(norm)
            unique.append(norm)
    for i in range(0, len(unique), HELIX_STREAMS_CHUNK):
        chunk = unique[i:i + HELIX_STREAMS_CHUNK]
        params = [("user_login", login) for login in chunk]
        data = helix_get("streams", params)
        if data is None:
            return None
        for item in data.get("data") or []:
            stream = _normalize_helix_stream(item)
            if stream["login"]:
                result[stream["login"]] = stream
    return result


def _gql_streams_by_logins(logins: list[str]) -> dict[str, dict]:
    unique = []
    seen = set()
    for login in logins:
        norm = normalize_login(login)
        if norm and norm not in seen:
            seen.add(norm)
            unique.append(norm)
    result: dict[str, dict] = {}
    if not unique:
        return result

    # Twitch GQL accepts a JSON array of operations.
    for i in range(0, len(unique), 20):
        chunk = unique[i:i + 20]
        payload = []
        for idx, login in enumerate(chunk):
            op_name = f"StreamStatus{idx}"
            payload.append({
                "operationName": op_name,
                # operationName must match the named query or Twitch returns
                # "no operation with name ..." and every channel looks offline.
                "query": (
                    f"query {op_name}($login: String!) {{"
                    " user(login: $login) {"
                    "  login displayName"
                    "  stream { viewersCount title createdAt game { id name } }"
                    " }"
                    "}"
                ),
                "variables": {"login": login},
            })
        try:
            req = urllib.request.Request(
                "https://gql.twitch.tv/gql",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Client-ID": GQL_CLIENT_ID,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=GQL_TIMEOUT_SEC) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, list):
                data = [data]
            for entry in data:
                user = ((entry or {}).get("data") or {}).get("user") or {}
                login = normalize_login(user.get("login"))
                if not login:
                    continue
                stream = user.get("stream")
                if not stream:
                    continue
                normalized = _normalize_gql_stream(login, stream)
                display = str(user.get("displayName") or login)
                normalized["display_name"] = display
                normalized["user_name"] = display
                result[login] = normalized
        except Exception as exc:
            logger.info("Twitch GQL batch status failed: %s", exc)
    return result


def get_streams_by_logins(logins: list[str], *, monitored: bool = False) -> dict[str, dict]:
    """Return {login: stream_dict} for currently live logins; offline omitted or is_live False.

    Always returns an entry for every requested login.
    """
    unique = []
    seen = set()
    for login in logins:
        norm = normalize_login(login)
        if norm and norm not in seen:
            seen.add(norm)
            unique.append(norm)
    if not unique:
        return {}

    cache_key = "streams:" + ",".join(sorted(unique))
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        out = {}
        for login in unique:
            item = dict(cached.get(login) or _empty_stream(login, monitored=monitored))
            item["monitored"] = bool(monitored or item.get("monitored"))
            out[login] = item
        return out

    live_map = _helix_streams_by_logins(unique)
    if live_map is None:
        live_map = _gql_streams_by_logins(unique)

    full: dict[str, dict] = {}
    for login in unique:
        if login in live_map:
            item = dict(live_map[login])
            item["monitored"] = bool(monitored)
            full[login] = item
        else:
            full[login] = _empty_stream(login, monitored=monitored)

    _cache_set(cache_key, full, STREAMS_CACHE_TTL_SEC)
    return full


def get_stream(login: str, *, monitored: bool = False) -> dict:
    login = normalize_login(login)
    if not login:
        return _empty_stream("", monitored=monitored)
    return get_streams_by_logins([login], monitored=monitored).get(login) or _empty_stream(login, monitored=monitored)


def list_streams(*, game_id: str = "", first: int = 24, language: str = "") -> dict:
    """Discovery: currently live streams. Requires Helix auth."""
    first = max(1, min(100, int(first or 24)))
    game_id = str(game_id or "").strip()
    language = str(language or "").strip()
    cache_key = f"list_streams:{game_id}:{language}:{first}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not has_helix_auth():
        # No Helix app credentials: discovery falls back to monitored live list in the hub.
        payload = {
            "ok": False,
            "error": "helix_unavailable",
            "message": gettext("Top streams are currently unavailable. Use monitored channels or the search."),
            "streams": [],
        }
        _cache_set(cache_key, payload, 30)
        return payload

    params: list[tuple[str, str]] = [("first", str(first))]
    if game_id:
        params.append(("game_id", game_id))
    if language:
        params.append(("language", language))
    data = helix_get("streams", params)
    if data is None:
        stale = _cache_get_stale(cache_key)
        if isinstance(stale, dict):
            return stale
        return {
            "ok": False,
            "error": "helix_request_failed",
            "message": gettext("Twitch streams could not be loaded."),
            "streams": [],
        }

    streams = [_normalize_helix_stream(item) for item in (data.get("data") or [])]
    payload = {"ok": True, "error": "", "message": "", "streams": streams}
    _cache_set(cache_key, payload, STREAMS_CACHE_TTL_SEC)
    return payload


def search_channels(query: str, first: int = 20) -> dict:
    query = str(query or "").strip()
    first = max(1, min(100, int(first or 20)))
    if not query:
        return {"ok": True, "error": "", "message": "", "channels": [], "streams": []}

    cache_key = f"search_channels:{query.lower()}:{first}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not has_helix_auth():
        # GQL fallback: resolve exact login + status (good enough for direct watch).
        login = normalize_login(query)
        streams = []
        channels = []
        if login:
            status = get_stream(login)
            channels.append({
                "login": login,
                "display_name": status.get("display_name") or login,
                "is_live": bool(status.get("is_live")),
                "thumbnail_url": status.get("thumbnail_url") or thumbnail_url(login),
                "title": status.get("title") or "",
                "game_name": status.get("game_name") or "",
                "viewer_count": int(status.get("viewer_count") or 0),
            })
            if status.get("is_live"):
                streams.append(status)
        payload = {
            "ok": True,
            "error": "",
            "message": "",
            "channels": channels,
            "streams": streams,
        }
        _cache_set(cache_key, payload, SEARCH_CACHE_TTL_SEC)
        return payload

    data = helix_get("search/channels", [("query", query), ("first", str(first)), ("live_only", "false")])
    if data is None:
        stale = _cache_get_stale(cache_key)
        if isinstance(stale, dict):
            return stale
        return {
            "ok": False,
            "error": "helix_request_failed",
            "message": gettext("Twitch search failed."),
            "channels": [],
            "streams": [],
        }

    channels = []
    live_logins = []
    for item in data.get("data") or []:
        login = normalize_login(item.get("broadcaster_login") or item.get("login"))
        if not login:
            continue
        is_live = bool(item.get("is_live"))
        if is_live:
            live_logins.append(login)
        channels.append({
            "login": login,
            "display_name": str(item.get("display_name") or login),
            "is_live": is_live,
            "thumbnail_url": str(item.get("thumbnail_url") or thumbnail_url(login)),
            "title": str(item.get("title") or ""),
            "game_name": str(item.get("game_name") or ""),
            "viewer_count": 0,
            "game_id": str(item.get("game_id") or ""),
        })

    live_map = get_streams_by_logins(live_logins) if live_logins else {}
    streams = []
    for channel in channels:
        live = live_map.get(channel["login"])
        if live and live.get("is_live"):
            channel["viewer_count"] = int(live.get("viewer_count") or 0)
            channel["title"] = live.get("title") or channel["title"]
            channel["game_name"] = live.get("game_name") or channel["game_name"]
            channel["thumbnail_url"] = live.get("thumbnail_url") or channel["thumbnail_url"]
            streams.append(live)

    payload = {"ok": True, "error": "", "message": "", "channels": channels, "streams": streams}
    _cache_set(cache_key, payload, SEARCH_CACHE_TTL_SEC)
    return payload


def search_categories(query: str, first: int = 20) -> dict:
    query = str(query or "").strip()
    first = max(1, min(100, int(first or 20)))
    if not query:
        return {"ok": True, "error": "", "message": "", "categories": []}

    cache_key = f"search_categories:{query.lower()}:{first}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not has_helix_auth():
        payload = {
            "ok": False,
            "error": "helix_unavailable",
            "message": gettext("Category search is currently unavailable."),
            "categories": [],
        }
        _cache_set(cache_key, payload, 30)
        return payload

    data = helix_get("search/categories", [("query", query), ("first", str(first))])
    if data is None:
        return {
            "ok": False,
            "error": "helix_request_failed",
            "message": gettext("Category search failed."),
            "categories": [],
        }

    categories = []
    for item in data.get("data") or []:
        box = str(item.get("box_art_url") or "")
        if box:
            box = box.replace("{width}", "144").replace("{height}", "192")
        categories.append({
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "box_art_url": box,
        })
    payload = {"ok": True, "error": "", "message": "", "categories": categories}
    _cache_set(cache_key, payload, SEARCH_CACHE_TTL_SEC)
    return payload


def monitored_twitch_logins(channel_configs: list[dict] | None = None) -> list[str]:
    """Extract unique Twitch logins from channel config rows."""
    configs = channel_configs
    if configs is None:
        try:
            from core import database
            configs = database.list_channel_configs(mode="record")
        except Exception:
            configs = []
    logins = []
    seen = set()
    for conf in configs or []:
        platform = str(conf.get("platform") or "twitch").strip().lower() or "twitch"
        if platform != "twitch":
            continue
        login = normalize_login(conf.get("name"))
        if login and login not in seen:
            seen.add(login)
            logins.append(login)
    return logins


def enrich_monitored_streams(
    channel_configs: list[dict] | None = None,
    *,
    recording_logins: set[str] | None = None,
) -> list[dict]:
    """Return stream dicts for all monitored Twitch logins (live first)."""
    logins = monitored_twitch_logins(channel_configs)
    status = get_streams_by_logins(logins, monitored=True)
    recording_logins = {normalize_login(x) for x in (recording_logins or set()) if normalize_login(x)}
    rows = []
    for login in logins:
        item = dict(status.get(login) or _empty_stream(login, monitored=True))
        if login in recording_logins:
            item["is_live"] = True
            item["recording"] = True
        else:
            item["recording"] = False
        item["monitored"] = True
        rows.append(item)
    rows.sort(key=lambda r: (not r.get("is_live"), -int(r.get("viewer_count") or 0), r.get("login") or ""))
    return rows


_CLIP_PERIODS = {
    "last_day": "LAST_DAY",
    "day": "LAST_DAY",
    "last_week": "LAST_WEEK",
    "week": "LAST_WEEK",
    "last_month": "LAST_MONTH",
    "month": "LAST_MONTH",
    "all": "ALL_TIME",
    "all_time": "ALL_TIME",
}


def fetch_recent_clips(login: str, *, first: int = 20, period: str = "LAST_DAY") -> list[dict]:
    """Fetch recent Twitch clips for a broadcaster via public GQL (no Helix needed)."""
    login = normalize_login(login)
    if not login:
        return []
    first = max(1, min(50, int(first or 20)))
    period_key = str(period or "LAST_DAY").strip()
    period_enum = _CLIP_PERIODS.get(period_key.lower(), period_key.upper())
    if period_enum not in {"LAST_DAY", "LAST_WEEK", "LAST_MONTH", "ALL_TIME"}:
        period_enum = "LAST_DAY"

    query = (
        "query($login: String!, $first: Int!, $period: ClipsPeriod!) {"
        "  user(login: $login) {"
        "    login displayName"
        "    clips(criteria: {period: $period}, first: $first) {"
        "      edges {"
        "        node {"
        "          id slug title url embedURL createdAt viewCount durationSeconds"
        "          thumbnailURL"
        "          curator { login displayName }"
        "          game { name }"
        "          broadcaster { login displayName }"
        "        }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    try:
        req = urllib.request.Request(
            "https://gql.twitch.tv/gql",
            data=json.dumps(
                {
                    "query": query,
                    "variables": {
                        "login": login,
                        "first": first,
                        "period": period_enum,
                    },
                }
            ).encode("utf-8"),
            headers={
                "Client-ID": GQL_CLIENT_ID,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=GQL_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.info("Twitch GQL clips failed for %s: %s", login, exc)
        return []

    user = ((payload or {}).get("data") or {}).get("user") or {}
    edges = ((user.get("clips") or {}).get("edges")) or []
    out: list[dict] = []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        slug = str(node.get("slug") or node.get("id") or "").strip()
        if not slug:
            continue
        curator = node.get("curator") or {}
        broadcaster = node.get("broadcaster") or {}
        game = node.get("game") or {}
        url = str(node.get("url") or "").strip()
        if not url:
            url = f"https://clips.twitch.tv/{slug}"
        thumb = str(node.get("thumbnailURL") or "").strip()
        out.append(
            {
                "id": str(node.get("id") or slug),
                "slug": slug,
                "title": str(node.get("title") or "Clip").strip() or "Clip",
                "url": url,
                "embed_url": str(node.get("embedURL") or "").strip(),
                "created_at": str(node.get("createdAt") or "").strip(),
                "view_count": int(node.get("viewCount") or 0),
                "duration": float(node.get("durationSeconds") or 0),
                "thumbnail_url": thumb,
                "curator_login": normalize_login(curator.get("login")),
                "curator_name": str(curator.get("displayName") or curator.get("login") or "").strip(),
                "broadcaster_login": normalize_login(broadcaster.get("login") or login),
                "broadcaster_name": str(
                    broadcaster.get("displayName") or user.get("displayName") or login
                ).strip(),
                "game_name": str(game.get("name") or "").strip(),
            }
        )
    # Newest first for notify ordering.
    out.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return out


def clip_mp4_url_from_thumbnail(thumbnail_url: str) -> str:
    """Guess a clip MP4 URL from the Twitch thumbnail (best-effort fallback)."""
    thumb = str(thumbnail_url or "").strip()
    if not thumb:
        return ""
    # Legacy clips CDN: ...-preview-480x272.jpg → ...mp4
    updated = re.sub(
        r"-preview-\d+x\d+\.(?:jpg|jpeg|png|webp)(?:\?.*)?$",
        ".mp4",
        thumb,
        flags=re.IGNORECASE,
    )
    if updated != thumb and updated.lower().endswith(".mp4"):
        return updated
    # Newer VAP assets store thumbs under .../landscape/thumb/thumb-...jpg;
    # corresponding MP4s live under .../landscape/h264/<quality>/index.mp4.
    vap = re.sub(
        r"/landscape/thumb/thumb-\d+-\d+x\d+\.(?:jpg|jpeg|png|webp)(?:\?.*)?$",
        "/landscape/h264/480/index.mp4",
        thumb,
        flags=re.IGNORECASE,
    )
    if vap != thumb and vap.lower().endswith(".mp4"):
        return vap
    return ""


def _pick_clip_source_url(
    qualities,
    *,
    max_quality: int | None = None,
) -> str:
    """Pick a clip sourceURL from GQL videoQualities.

    When max_quality is set, prefer the highest available rung that does not
    exceed it (Discord uploads stay small); otherwise pick the highest rung.
    """
    candidates: list[tuple[int, str]] = []
    for item in qualities or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("sourceURL") or "").strip()
        if not source:
            continue
        try:
            quality = int(str(item.get("quality") or "0").rstrip("p"))
        except (TypeError, ValueError):
            quality = 0
        candidates.append((quality, source))
    if not candidates:
        return ""
    if max_quality is None:
        return max(candidates, key=lambda item: item[0])[1]
    under = [item for item in candidates if item[0] <= int(max_quality)]
    if under:
        return max(under, key=lambda item: item[0])[1]
    return min(candidates, key=lambda item: item[0])[1]


def fetch_clip_mp4_url(
    slug: str,
    *,
    thumbnail_url: str = "",
    max_quality: int | None = None,
) -> str:
    """Resolve a downloadable MP4 URL for a Twitch clip slug via public GQL."""
    slug = str(slug or "").strip()
    if not slug:
        return clip_mp4_url_from_thumbnail(thumbnail_url)
    query = (
        "query($slug: ID!) {"
        "  clip(slug: $slug) {"
        "    playbackAccessToken(params: {"
        "      platform: \"web\", playerBackend: \"mediaplayer\", playerType: \"embed\""
        "    }) { signature value }"
        "    videoQualities { quality sourceURL }"
        "  }"
        "}"
    )
    try:
        req = urllib.request.Request(
            "https://gql.twitch.tv/gql",
            data=json.dumps({"query": query, "variables": {"slug": slug}}).encode("utf-8"),
            headers={
                "Client-ID": GQL_CLIENT_ID,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=GQL_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.info("Twitch GQL clip source failed for %s: %s", slug, exc)
        return clip_mp4_url_from_thumbnail(thumbnail_url)

    clip = ((payload or {}).get("data") or {}).get("clip") or {}
    token = clip.get("playbackAccessToken") or {}
    signature = str(token.get("signature") or "").strip()
    value = str(token.get("value") or "").strip()
    best_url = _pick_clip_source_url(clip.get("videoQualities") or [], max_quality=max_quality)
    if not best_url:
        return clip_mp4_url_from_thumbnail(thumbnail_url)
    if signature and value:
        sep = "&" if "?" in best_url else "?"
        best_url = (
            f"{best_url}{sep}sig={urllib.parse.quote(signature)}"
            f"&token={urllib.parse.quote(value)}"
        )
    return best_url


def download_clip(
    clip: dict,
    dest_path: str,
    *,
    timeout: int = 60,
    max_quality: int | None = None,
) -> str:
    """Download a Twitch clip to dest_path. Returns the path on success, else ''."""
    if not isinstance(clip, dict) or not dest_path:
        return ""
    slug = str(clip.get("slug") or clip.get("id") or "").strip()
    url = fetch_clip_mp4_url(
        slug,
        thumbnail_url=str(clip.get("thumbnail_url") or ""),
        max_quality=max_quality,
    )
    if not url:
        return ""
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    tmp_path = dest_path + ".part"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (StreamVault clip fetch)",
                "Accept": "*/*",
                "Referer": "https://clips.twitch.tv/",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp_path, "wb") as handle:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
        if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 1024:
            os.replace(tmp_path, dest_path)
            return dest_path
    except Exception as exc:
        logger.info("Twitch clip download failed for %s: %s", slug or dest_path, exc)
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return ""


def fetch_channel_point_rewards(login: str, *, force: bool = False) -> dict[str, dict]:
    """Map custom-reward-id -> {id, title, cost} via public Twitch GQL."""
    login = normalize_login(login)
    if not login:
        return {}
    cache_key = f"channel_point_rewards:{login}"
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if (
            not force
            and isinstance(cached, dict)
            and now - float(cached.get("ts") or 0) < REWARDS_CACHE_TTL_SEC
        ):
            rewards = cached.get("rewards")
            if isinstance(rewards, dict):
                return dict(rewards)

    query = (
        "query($login: String!) {"
        "  user(login: $login) {"
        "    channel {"
        "      communityPointsSettings {"
        "        customRewards { id title cost isEnabled }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    rewards: dict[str, dict] = {}
    try:
        req = urllib.request.Request(
            "https://gql.twitch.tv/gql",
            data=json.dumps({"query": query, "variables": {"login": login}}).encode("utf-8"),
            headers={
                "Client-ID": GQL_CLIENT_ID,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=GQL_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        settings = (
            (((payload or {}).get("data") or {}).get("user") or {}).get("channel") or {}
        ).get("communityPointsSettings") or {}
        for raw in settings.get("customRewards") or []:
            if not isinstance(raw, dict):
                continue
            reward_id = str(raw.get("id") or "").strip()
            if not reward_id:
                continue
            title = str(raw.get("title") or "").strip() or "Channel Points"
            try:
                cost = int(raw.get("cost") or 0)
            except (TypeError, ValueError):
                cost = 0
            rewards[reward_id] = {
                "id": reward_id,
                "title": title,
                "cost": max(0, cost),
                "enabled": bool(raw.get("isEnabled", True)),
            }
    except Exception as exc:
        logger.info("Twitch GQL channel-point rewards failed for %s: %s", login, exc)

    with _CACHE_LOCK:
        _CACHE[cache_key] = {"ts": now, "rewards": dict(rewards)}
    return rewards


def resolve_channel_point_reward(login: str, reward_id: str) -> dict | None:
    """Resolve a custom-reward-id to title/cost for chat display."""
    reward_id = str(reward_id or "").strip()
    if not reward_id:
        return None
    rewards = fetch_channel_point_rewards(login)
    found = rewards.get(reward_id)
    if found:
        return dict(found)
    # Unknown / disabled / deleted reward: still expose the id for UI fallback.
    return {"id": reward_id, "title": "Channel Points", "cost": 0, "enabled": False}


def reward_payload_from_irc_tags(login: str, tags: dict | None) -> dict | None:
    """Build a compact chat-log reward object from Twitch IRC tags."""
    if not isinstance(tags, dict):
        return None
    reward_id = str(tags.get("custom-reward-id") or "").strip()
    if not reward_id:
        return None
    info = resolve_channel_point_reward(login, reward_id) or {}
    title = str(info.get("title") or "").strip() or "Channel Points"
    try:
        cost = int(info.get("cost") or 0)
    except (TypeError, ValueError):
        cost = 0
    payload = {"id": reward_id, "title": title}
    if cost > 0:
        payload["cost"] = cost
    return payload


def _normalize_gql_badge_row(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    set_id = str(raw.get("setID") or raw.get("set_id") or "").strip()
    badge_id = str(raw.get("version") or raw.get("id") or "").strip()
    if not set_id or not badge_id:
        return None
    image_1x = str(raw.get("u1") or raw.get("imageURL") or raw.get("image_url_1x") or "").strip()
    image_2x = str(raw.get("u2") or raw.get("image_url_2x") or "").strip()
    image_4x = str(raw.get("u4") or raw.get("image_url_4x") or "").strip()
    if not image_1x and not image_2x and not image_4x:
        return None
    if image_1x and not image_1x.startswith("https://static-cdn.jtvnw.net/badges/"):
        return None
    return {
        "set_id": set_id,
        "id": badge_id,
        "image_url_1x": image_1x,
        "image_url_2x": image_2x,
        "image_url_4x": image_4x,
        "title": str(raw.get("title") or "").strip()[:120],
        "description": str(raw.get("description") or "").strip()[:240],
    }


def _gql_post(query: str, variables: dict | None = None) -> dict | None:
    try:
        req = urllib.request.Request(
            "https://gql.twitch.tv/gql",
            data=json.dumps({"query": query, "variables": variables or {}}).encode("utf-8"),
            headers={
                "Client-ID": GQL_CLIENT_ID,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=GQL_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.info("Twitch GQL request failed: %s", exc)
    return None


def fetch_gql_chat_badges(login: str = "") -> list[dict]:
    """Fetch official Twitch chat badge assets via public GQL (Helix/public badges API fallback)."""
    badges: list[dict] = []
    global_query = (
        "query {"
        "  badges {"
        "    setID version title description"
        "    u1: imageURL(size: NORMAL)"
        "    u2: imageURL(size: DOUBLE)"
        "    u4: imageURL(size: QUADRUPLE)"
        "  }"
        "}"
    )
    payload = _gql_post(global_query)
    for raw in ((payload or {}).get("data") or {}).get("badges") or []:
        item = _normalize_gql_badge_row(raw)
        if item:
            badges.append(item)

    login = normalize_login(login)
    if login:
        channel_query = (
            "query($login: String!) {"
            "  user(login: $login) {"
            "    broadcastBadges {"
            "      setID version title description"
            "      u1: imageURL(size: NORMAL)"
            "      u2: imageURL(size: DOUBLE)"
            "      u4: imageURL(size: QUADRUPLE)"
            "    }"
            "  }"
            "}"
        )
        channel_payload = _gql_post(channel_query, {"login": login})
        channel_badges = (
            (((channel_payload or {}).get("data") or {}).get("user") or {}).get("broadcastBadges")
            or []
        )
        for raw in channel_badges:
            item = _normalize_gql_badge_row(raw)
            if item:
                badges.append(item)

    # Prefer later (channel) entries when deduping in caller.
    return badges
