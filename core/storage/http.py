"""Small, dependency-free HTTP helper shared by storage providers."""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from typing import Any, Optional

import requests
from .base import StorageError, close_quietly

logger = logging.getLogger("storage.http")

DEFAULT_TIMEOUT = (10, 90)
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
_LOCAL = threading.local()

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_URL_FIELD_RE = re.compile(r"(?i)\burl:\s*[^\s,)]+")
_QUERY_RE = re.compile(r"\?[^\s'\"<>)]*")


def sanitize_error_text(value, limit: int = 500) -> str:
    """Remove URLs (which may contain upload-session tokens) from errors."""
    text = str(value or "").strip()
    text = _URL_RE.sub("[URL]", text)
    text = _URL_FIELD_RE.sub("url: [URL]", text)
    text = _QUERY_RE.sub("?***", text)
    return text[:limit]


def session() -> requests.Session:
    """One session per thread (requests sessions are not fully thread-safe)."""
    current = getattr(_LOCAL, "session", None)
    if current is None:
        current = requests.Session()
        current.headers.update({
            "User-Agent": "StreamVault-Storage/1.0",
            # Providers explicitly set their own Content-Type where needed.
            # A wildcard Accept avoids accidentally forcing a JSON response
            # from download endpoints that inspect the header.
            "Accept": "*/*",
        })
        _LOCAL.session = current
    return current


def _retry_after_seconds(response: requests.Response, attempt: int) -> float:
    raw = (response.headers.get("Retry-After") or "").strip()
    if raw:
        try:
            return min(60.0, max(1.0, float(raw)))
        except ValueError:
            pass
    return min(30.0, (2 ** attempt) + random.uniform(0, 0.4))


def error_text(response: requests.Response) -> str:
    """Extract a short, non-secret error message from common API formats."""
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        for path in (
            ("error", "message"),
            ("error_description",),
            ("error_summary",),
            ("message",),
            ("error",),
        ):
            value: Any = body
            for key in path:
                if not isinstance(value, dict) or key not in value:
                    value = None
                    break
                value = value[key]
            if value:
                return sanitize_error_text(value)
    text = (response.text or "").strip()
    if "<" in text and ">" in text:
        # Do not dump full XML/HTML error pages into logs/UI.
        text = "Antwort des Anbieters konnte nicht gelesen werden."
    return sanitize_error_text(text)


def request(
    method: str,
    url: str,
    *,
    provider: str = "",
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    retries: int = 3,
    retry_status: Optional[set[int]] = None,
    stream: bool = False,
    **kwargs,
) -> requests.Response:
    """HTTP request with bounded retries for transient failures.

    The response is returned for all statuses; callers decide whether a status
    is acceptable (uploads need to handle 308, for example).  Network failures
    and retryable statuses are retried.
    """
    statuses = RETRY_STATUS if retry_status is None else retry_status
    last_exc: Optional[Exception] = None
    for attempt in range(max(1, int(retries))):
        try:
            response = session().request(
                method,
                url,
                timeout=timeout,
                stream=stream,
                **kwargs,
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt + 1 >= retries:
                break
            time.sleep(min(20.0, (2 ** attempt) + random.uniform(0, 0.4)))
            continue

        if response.status_code not in statuses or attempt + 1 >= retries:
            return response
        delay = _retry_after_seconds(response, attempt)
        close_quietly(response, provider=provider)
        time.sleep(delay)

    raise StorageError(
        "Netzwerkfehler bei "
        f"{provider or 'Storage'}: {sanitize_error_text(last_exc) or 'unbekannt'}",
        provider=provider,
        retryable=True,
    )


def raise_for_status(
    response: requests.Response,
    action: str,
    *,
    provider: str = "",
    retryable_status: Optional[set[int]] = None,
) -> None:
    if 200 <= response.status_code < 300:
        return
    retryable = response.status_code in (retryable_status or RETRY_STATUS)
    detail = error_text(response) or response.reason or "unbekannter Fehler"
    raise StorageError(
        f"{action} fehlgeschlagen: {sanitize_error_text(detail)}",
        provider=provider,
        status_code=response.status_code,
        retryable=retryable,
    )


def json_response(response: requests.Response, action: str, *, provider: str = "") -> dict:
    raise_for_status(response, action, provider=provider)
    try:
        data = response.json()
    except ValueError as exc:
        raise StorageError(
            f"{action}: ungültige JSON-Antwort.",
            provider=provider,
            status_code=response.status_code,
        ) from exc
    if not isinstance(data, dict):
        raise StorageError(f"{action}: unerwartete Antwort.", provider=provider)
    return data


def dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
