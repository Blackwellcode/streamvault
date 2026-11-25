"""Access-log escaping for gunicorn (--logger-class WEBUI.gunicorn_logging.EscapedPathLogger).

gunicorn logs %(U)s as the percent-DEcoded path: GET /x%0A1.2.3.4... would write a
forged second log line and /%1b[2J a terminal escape sequence. Paths and all other
client-controlled values are percent-encoded again before they are formatted.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

try:
    from gunicorn.glogging import Logger
except ImportError:  # Windows: gunicorn needs fcntl; only the functions below are usable
    Logger = object

# Printable ASCII without the characters that end a path (space, ?, #) or a quoted log
# field (", \). "%" is encoded too, so a decoded "%0A" and the text "%250A" stay distinct.
_PATH_SAFE = "/:@!$&'()*+,;=-._~[]|^`{}<>"
# Header values may already be percent-encoded and may contain spaces.
_TEXT_SAFE = _PATH_SAFE + " %?#"
_QUERY_OR_FRAGMENT = re.compile(r"[?#]")


def _quote(value: str, safe: str) -> str:
    # WSGI strings carry raw bytes as latin-1 (PEP 3333): U+2028 arrives as \xe2\x80\xa8.
    try:
        raw = value.encode("latin-1")
    except UnicodeEncodeError:  # e.g. the Basic-Auth user, which gunicorn decodes as UTF-8
        raw = value.encode("utf-8", errors="backslashreplace")
    return quote(raw, safe=safe)


def escape_path(value: str) -> str:
    """Percent-encode everything that is not plainly printable in a URL path."""
    return _quote(value, _PATH_SAFE)


def escape_text(value: str) -> str:
    """Percent-encode control characters, non-ASCII bytes, quotes and backslashes."""
    return _quote(value, _TEXT_SAFE)


def escape_referer(value: str) -> str:
    """Referer without query string and fragment: both can carry tokens."""
    return escape_text(_QUERY_OR_FRAGMENT.split(value, maxsplit=1)[0])


def escape_atoms(atoms: dict[str, Any]) -> dict[str, Any]:
    """Copy of gunicorn's log atoms, safe to write into a line-based log."""
    escaped = {key: escape_text(value) if isinstance(value, str) else value
               for key, value in atoms.items()}
    for key, escape in (("U", escape_path), ("u", escape_path), ("f", escape_referer)):
        value = atoms.get(key)
        if isinstance(value, str):
            escaped[key] = escape(value)
    return escaped


class EscapedPathLogger(Logger):
    """gunicorn logger whose access log cannot be forged by request data."""

    def atoms(self, resp, req, environ, request_time):
        return escape_atoms(super().atoms(resp, req, environ, request_time))
