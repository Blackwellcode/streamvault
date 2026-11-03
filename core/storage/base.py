"""Provider-agnostic external storage interface for StreamVault.

The storage layer is intentionally independent from Flask and the recorder so
that the same code can be used by the WebUI, the background worker and the
console CLI.  Providers only deal with bytes and remote paths; database state,
encryption and task scheduling live in :mod:`core.storage.service`.
"""

from __future__ import annotations

import mimetypes
import os
import posixpath
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Optional

from flask_babel import gettext, lazy_gettext


class StorageError(RuntimeError):
    """A provider/configuration error with optional retry semantics."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: Optional[int] = None,
        retryable: bool = False,
        details: str = "",
    ):
        super().__init__(str(message or "storage error"))
        self.provider = str(provider or "")
        self.status_code = status_code
        self.retryable = bool(retryable)
        self.details = str(details or "")

    def user_message(self) -> str:
        base = str(self) or "Speicherfehler"
        if self.status_code:
            base = f"{base} (HTTP {self.status_code})"
        return base


class StorageConfigError(StorageError):
    """Invalid or incomplete provider configuration."""


@dataclass(frozen=True)
class ProviderField:
    key: str
    label: str
    type: str = "text"
    required: bool = False
    secret: bool = False
    placeholder: str = ""
    help: str = ""
    default: str = ""
    choices: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": str(self.label),
            "type": self.type,
            "required": self.required,
            "secret": self.secret,
            "placeholder": str(self.placeholder),
            "help": str(self.help),
            "default": self.default,
            "choices": [{"value": value, "label": str(label)} for value, label in self.choices],
        }


@dataclass
class RemoteObject:
    path: str
    remote_id: str = ""
    size: int = 0
    modified_at: str = ""
    is_folder: bool = False


ProgressCallback = Callable[[int, int], None]


def normalize_remote_path(path: str) -> str:
    """Normalise a POSIX-style remote path and reject traversal attempts."""
    raw = str(path or "").replace("\\", "/").strip()
    if "\x00" in raw:
        raise StorageConfigError(gettext("Invalid path (null byte)."))
    parts = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise StorageConfigError(gettext("Invalid path (path traversal)."))
        if any(ord(ch) < 32 for ch in part):
            raise StorageConfigError(gettext("Invalid path (control characters)."))
        parts.append(part)
    if not parts:
        raise StorageConfigError(gettext("The remote path must not be empty."))
    return "/".join(parts)


def remote_parent(path: str) -> str:
    path = normalize_remote_path(path)
    parent = posixpath.dirname(path)
    return parent


def remote_name(path: str) -> str:
    return posixpath.basename(normalize_remote_path(path))


def guess_mime_type(filename: str, default: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(str(filename or ""))[0] or default


def file_size(path: str) -> int:
    try:
        return int(os.path.getsize(path))
    except OSError as exc:
        raise StorageError(f"Lokale Datei nicht lesbar: {exc}") from exc


def iter_file_chunks(
    path: str,
    *,
    start: int = 0,
    end: Optional[int] = None,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    """Yield a byte range from a local file (end inclusive)."""
    with open(path, "rb") as handle:
        handle.seek(max(0, int(start)))
        remaining = None if end is None else max(0, int(end) - int(start) + 1)
        while True:
            size = int(chunk_size)
            if remaining is not None:
                if remaining <= 0:
                    break
                size = min(size, remaining)
            data = handle.read(size)
            if not data:
                break
            if remaining is not None:
                remaining -= len(data)
            yield data


def iter_response_range(
    response,
    start: int,
    end: int,
    *,
    chunk_size: int = 256 * 1024,
) -> Iterator[bytes]:
    """Yield exactly the requested inclusive byte range from an HTTP response.

    Some storage servers ignore ``Range`` and answer with ``200`` plus the whole
    file.  Skip the prefix and stop at ``end`` so callers never receive extra
    bytes beyond the range they asked for.
    """
    start = max(0, int(start))
    end = max(start, int(end))
    remaining = end - start + 1
    skip = start if getattr(response, "status_code", 200) == 200 else 0
    for chunk in response.iter_content(chunk_size=chunk_size):
        if not chunk or remaining <= 0:
            continue
        if skip:
            if len(chunk) <= skip:
                skip -= len(chunk)
                continue
            chunk = chunk[skip:]
            skip = 0
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        remaining -= len(chunk)
        if chunk:
            yield chunk
        if remaining <= 0:
            break


class StorageProvider:
    """Base class implemented by every external storage backend."""

    type_id = ""
    label = ""
    description = ""
    config_fields: tuple[ProviderField, ...] = ()
    oauth_supported = False
    oauth_label = lazy_gettext("Connect account")
    docs_url = ""

    def __init__(self, config: dict):
        self.config = dict(config or {})
        self._config_changed_callback = None

    def set_config_changed_callback(self, callback) -> None:
        """Register a callback used to persist rotated OAuth tokens."""
        self._config_changed_callback = callback

    def _config_changed(self) -> None:
        callback = self._config_changed_callback
        if callback is None:
            return
        try:
            callback(dict(self.config))
        except Exception:
            # Token rotation must never make a request fail just because the
            # optional persistence callback is unavailable.
            pass

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    @classmethod
    def fields(cls) -> tuple[ProviderField, ...]:
        return cls.config_fields

    @classmethod
    def secret_keys(cls) -> set[str]:
        return {field.key for field in cls.config_fields if field.secret}

    @classmethod
    def validate_config(cls, config: dict, *, require_secrets: bool = True) -> list[str]:
        errors: list[str] = []
        values = config or {}
        for field in cls.config_fields:
            if not field.required:
                continue
            if field.secret and not require_secrets:
                continue
            value = values.get(field.key)
            if value is None or str(value).strip() == "":
                errors.append(gettext("%(field)s is missing.", field=field.label))
        return errors

    @classmethod
    def default_config(cls) -> dict:
        return {field.key: field.default for field in cls.config_fields if field.default}

    @classmethod
    def public_config(cls, config: dict) -> dict:
        """Return config values safe for the browser (secrets masked)."""
        out = {}
        for field in cls.config_fields:
            value = (config or {}).get(field.key, "")
            if field.secret:
                out[field.key] = "••••••••" if str(value or "") else ""
                out[f"{field.key}__set"] = bool(str(value or ""))
            else:
                out[field.key] = value if value is not None else ""
        return out

    def masked_config(self) -> dict:
        return self.public_config(self.config)

    # ------------------------------------------------------------------
    # Provider operations
    # ------------------------------------------------------------------
    def test_connection(self) -> dict:
        raise NotImplementedError

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        raise NotImplementedError

    def download_file(
        self,
        remote_path: str,
        dest_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        raise NotImplementedError

    def open_range(self, remote_path: str, start: int, end: int) -> Iterable[bytes]:
        """Yield bytes for an inclusive range. Optional for restore-only use."""
        raise StorageError(
            f"{self.label} unterstützt kein direktes Range-Streaming.",
            provider=self.type_id,
        )

    def delete(self, remote_path: str) -> None:
        raise NotImplementedError

    def list(self, prefix: str = "") -> list[RemoteObject]:
        raise NotImplementedError

    def exists(self, remote_path: str, expected_size: int = 0) -> bool:
        """Return whether a remote object exists (optionally with a size check).

        The default implementation lists the parent folder. Providers with a
        cheaper metadata endpoint should override this method.
        """
        wanted_path = normalize_remote_path(remote_path)
        parent = remote_parent(wanted_path)
        for item in self.list(parent):
            if item.path.rstrip("/") != wanted_path:
                continue
            expected = int(expected_size or 0)
            if expected <= 0:
                return True
            # Unknown remote size (0) must never satisfy a known expected size.
            return int(item.size or 0) == expected
        return False

    def quota(self) -> Optional[dict]:
        return None

    # ------------------------------------------------------------------
    # Optional OAuth hooks
    # ------------------------------------------------------------------
    @classmethod
    def oauth_ready(cls, config: dict) -> bool:
        return False

    @classmethod
    def oauth_linked(cls, config: dict) -> bool:
        """Whether an OAuth account/token has been stored for this provider."""
        values = config or {}
        return bool(
            str(values.get("refresh_token") or "").strip()
            or str(values.get("access_token") or "").strip()
        )

    @classmethod
    def oauth_authorize_url(cls, config: dict, redirect_uri: str, state: str) -> str:
        raise StorageConfigError(gettext("%(provider)s does not support OAuth.", provider=cls.label))

    @classmethod
    def oauth_exchange_code(cls, config: dict, code: str, redirect_uri: str) -> dict:
        raise StorageConfigError(gettext("%(provider)s does not support OAuth.", provider=cls.label))


_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(?P<prefix>(?P<qkey>[\"']?)\b(?:access_token|refresh_token|"
        r"id_token|token|client_secret|code|password|passwd|secret|api_key|"
        r"apikey|authorization)\b(?P=qkey)(?P<sep>\s*[=:]\s*|\s+)"
        r"(?:bearer\s+)?)"
        r"(?:(?P<qval>[\"'])(?P<val>.*?)(?P=qval)|(?P<uval>[^\s&,;<>]*))"
    ),
)


def _mask_secret_match(match) -> str:
    quote = match.group("qval") or ""
    return f"{match.group('prefix')}{quote}***{quote}"


def mask_text(value) -> str:
    """Redact credential-looking key/value pairs from arbitrary error text."""
    text = str(value or "").strip()
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_mask_secret_match, text)
    text = re.sub(
        r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+",
        r"\1 ***",
        text,
    )
    return text[:500]


def mask_error(exc: Exception) -> str:
    """Never leak credential-looking values into logs/UI."""
    return mask_text(exc) or "Storage-Fehler"
