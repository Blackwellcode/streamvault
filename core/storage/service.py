"""Database-backed external storage orchestration.

This module is the single integration point for the WebUI, worker and CLI.  It
owns provider configuration (encrypted at rest), archive object bookkeeping,
remote path building, upload/restore/delete operations and the safe local
retention rules.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import time
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from flask_babel import gettext

from core import config, database
from core.mp4_utils import is_transient_media_filename

from .base import (
    StorageConfigError,
    StorageError,
    mask_error,
    mask_text,
    normalize_remote_path,
)
from .crypto import (
    decrypt_config,
    encrypt_config,
    encryption_ready,
    signing_secret,
)
from .registry import PROVIDER_CLASSES, get_provider_class, provider_catalog

logger = logging.getLogger("storage.service")

SETTINGS_KEY = "storage_settings"
SCHEDULE_KEY = "storage_schedule"
OAUTH_SALT = "streamvault-storage-oauth-v1"
OAUTH_MAX_AGE = 900

DEFAULT_SETTINGS = {
    "auto_upload": False,
    "delete_local_after_upload": False,
    "local_retention_days": 14,
    "verify_before_local_delete": True,
    "default_provider_id": None,
    "auto_sync_interval_minutes": 10,
    "cleanup_interval_minutes": 360,
    "max_sync_files": 500,
    "include_chat_logs": False,
    "include_thumbnails": False,
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".flv",
    ".ts",
    ".mov",
    ".webm",
    ".m4a",
    ".mp3",
    ".aac",
    ".ogg",
    ".wav",
    ".flac",
}
SIDECAR_EXTENSIONS = {".txt", ".vtt", ".srt", ".json"}
THUMB_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SKIP_DIRS = {
    ".hls",
    ".hls_live",
    ".meta",
    ".ruff_cache",
    "__pycache__",
    "bin",
    "content",
    "core",
    "docs",
    "files",
    "node_modules",
    "recorder",
    "scripts",
    "static",
    "tests",
    "venv",
    "WEBUI",
}
SKIP_SUFFIXES = (".part", ".tmp", ".temp", ".lock", ".db-wal", ".db-shm")
THUMB_DIR = ".thumbs"


def _safe_error(exc: Exception) -> str:
    """Sanitised error text with the HTTP status kept for diagnostics."""
    message = mask_error(exc)
    status = getattr(exc, "status_code", None)
    if status and f"HTTP {status}" not in message:
        message = f"{message} (HTTP {status})"
    return message[:500]


def _as_bool(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value, default, minimum=0, maximum=100000):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(int(minimum), min(int(maximum), number))


def _clean_provider_id(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_settings() -> dict:
    raw = database.get_system_state(SETTINGS_KEY, {})
    if not isinstance(raw, dict):
        raw = {}
    settings = dict(DEFAULT_SETTINGS)
    settings["auto_upload"] = _as_bool(raw.get("auto_upload"), False)
    settings["delete_local_after_upload"] = _as_bool(
        raw.get("delete_local_after_upload"), False
    )
    settings["verify_before_local_delete"] = _as_bool(
        raw.get("verify_before_local_delete"), True
    )
    settings["include_chat_logs"] = _as_bool(raw.get("include_chat_logs"), False)
    settings["include_thumbnails"] = _as_bool(raw.get("include_thumbnails"), False)
    settings["local_retention_days"] = _as_int(
        raw.get("local_retention_days"), 14, 0, 3650
    )
    settings["auto_sync_interval_minutes"] = _as_int(
        raw.get("auto_sync_interval_minutes"), 10, 0, 10080
    )
    settings["cleanup_interval_minutes"] = _as_int(
        raw.get("cleanup_interval_minutes"), 360, 0, 10080
    )
    settings["max_sync_files"] = _as_int(raw.get("max_sync_files"), 500, 1, 5000)
    default_id = _clean_provider_id(raw.get("default_provider_id"))
    settings["default_provider_id"] = default_id
    return settings


def save_settings(values: dict) -> dict:
    settings = get_settings()
    values = values or {}
    for key in (
        "auto_upload",
        "delete_local_after_upload",
        "verify_before_local_delete",
        "include_chat_logs",
        "include_thumbnails",
    ):
        if key in values:
            settings[key] = _as_bool(values.get(key))
    if "local_retention_days" in values:
        settings["local_retention_days"] = _as_int(
            values.get("local_retention_days"), settings["local_retention_days"], 0, 3650
        )
    if "auto_sync_interval_minutes" in values:
        settings["auto_sync_interval_minutes"] = _as_int(
            values.get("auto_sync_interval_minutes"),
            settings["auto_sync_interval_minutes"],
            0,
            10080,
        )
    if "cleanup_interval_minutes" in values:
        settings["cleanup_interval_minutes"] = _as_int(
            values.get("cleanup_interval_minutes"),
            settings["cleanup_interval_minutes"],
            0,
            10080,
        )
    if "max_sync_files" in values:
        settings["max_sync_files"] = _as_int(
            values.get("max_sync_files"), settings["max_sync_files"], 1, 5000
        )
    if "default_provider_id" in values:
        provider_id = _clean_provider_id(values.get("default_provider_id"))
        if provider_id is not None:
            row = database.get_storage_provider(provider_id)
            if not row:
                raise StorageConfigError(gettext("The selected default provider does not exist."))
            if not row.get("enabled"):
                raise StorageConfigError(gettext(
                    "The selected default provider is disabled. "
                    "Enable it first or choose another provider."
                ))
        settings["default_provider_id"] = provider_id
    database.set_system_state(SETTINGS_KEY, settings)
    return settings


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

def _decrypt_provider_row(row: dict) -> dict:
    row = dict(row or {})
    encrypted = row.get("config")
    row["config_error"] = ""
    try:
        row["config"] = decrypt_config(encrypted)
    except StorageConfigError as exc:
        row["config"] = {}
        row["config_error"] = str(exc)
    return row


def get_provider_record(provider_id, *, include_config=True) -> Optional[dict]:
    row = database.get_storage_provider(provider_id)
    if not row:
        return None
    row = _decrypt_provider_row(row)
    if include_config:
        return row
    return _public_provider(row)


def _public_provider(row: dict) -> dict:
    row = dict(row or {})
    cls = PROVIDER_CLASSES.get(str(row.get("provider_type") or "").strip().lower())
    cfg = row.get("config") if isinstance(row.get("config"), dict) else {}
    row["type_label"] = str(cls.label) if cls else str(row.get("provider_type") or "")
    row["oauth_supported"] = bool(getattr(cls, "oauth_supported", False))
    row["oauth_label"] = str(getattr(cls, "oauth_label", "") or gettext("Connect account"))
    row["docs_url"] = getattr(cls, "docs_url", "")
    row["config"] = cls.public_config(cfg) if cls else {}
    row["config_set"] = bool(cfg)
    row["config_error"] = mask_text(row.get("config_error"))
    row["last_error"] = mask_text(row.get("last_error"))
    row["last_check_message"] = mask_text(row.get("last_check_message"))
    return row


def list_providers(*, include_config=False) -> list[dict]:
    out = []
    for row in database.list_storage_providers():
        row = _decrypt_provider_row(row)
        out.append(row if include_config else _public_provider(row))
    return out


def _apply_defaults(cls, values: dict) -> dict:
    """Fill defaults only for fields the caller did not provide.

    An explicitly submitted empty value is meaningful (for example
    ``base_path`` means "write directly into the target folder"), so it must
    not be silently replaced by a default.
    """
    merged = dict(values or {})
    for field in cls.config_fields:
        if not field.default:
            continue
        if field.key not in merged or merged[field.key] is None:
            merged[field.key] = field.default
    return merged


def _validate_provider_paths(cls, values: dict) -> dict:
    """Normalise and validate path-like provider settings at save time."""
    merged = dict(values or {})
    if "base_path" in {field.key for field in cls.config_fields}:
        base = str(merged.get("base_path") or "").strip()
        if base:
            try:
                merged["base_path"] = normalize_remote_path(base)
            except StorageConfigError as exc:
                raise StorageConfigError(gettext("Invalid subfolder: %(error)s", error=exc)) from exc
        else:
            merged["base_path"] = ""
    return merged


def _config_errors(cls, values: dict) -> list[str]:
    errors = cls.validate_config(values, require_secrets=True)
    if cls.oauth_supported:
        # OAuth providers are allowed to be saved before the account is linked.
        oauth_secret_keys = {"refresh_token", "access_token"}
        labels = {
            gettext("%(field)s is missing.", field=field.label)
            for field in cls.config_fields
            if field.key in oauth_secret_keys
        }
        errors = [error for error in errors if error not in labels]
    return errors


def _clean_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise StorageConfigError(gettext("Please enter a name for the storage provider."))
    if len(value) > 80:
        raise StorageConfigError(gettext("The provider name must be at most 80 characters long."))
    return value


def _ensure_unique_name(name: str, *, exclude_id=None):
    wanted = name.lower()
    for row in database.list_storage_providers():
        if row["id"] == exclude_id:
            continue
        if str(row.get("name") or "").strip().lower() == wanted:
            raise StorageConfigError(gettext("A provider named '%(name)s' already exists.", name=name))


def create_provider(name: str, provider_type: str, values: dict, *, enabled=True) -> int:
    if not encryption_ready():
        raise StorageConfigError(gettext(
            "VOD_SECRET_KEY must be set to a strong value before "
            "credentials can be stored."
        ))
    name = _clean_name(name)
    _ensure_unique_name(name)
    cls = get_provider_class(provider_type)
    values = _validate_provider_paths(cls, _apply_defaults(cls, values))
    errors = _config_errors(cls, values)
    if errors:
        raise StorageConfigError(" ".join(errors))
    provider_id = database.create_storage_provider(
        name, cls.type_id, encrypt_config(values), enabled=enabled
    )
    settings = get_settings()
    if enabled and not settings.get("default_provider_id"):
        save_settings({"default_provider_id": provider_id})
    return provider_id


def update_provider(
    provider_id: int,
    *,
    name: Optional[str] = None,
    provider_type: Optional[str] = None,
    values: Optional[dict] = None,
    enabled: Optional[bool] = None,
) -> dict:
    existing = get_provider_record(provider_id)
    if not existing:
        raise StorageConfigError(gettext("Storage provider not found."))
    new_type = str(provider_type or existing["provider_type"]).strip().lower()
    cls = get_provider_class(new_type)
    type_changed = new_type != str(existing["provider_type"]).strip().lower()
    if type_changed and database.list_storage_objects(
        provider_id=provider_id, limit=1
    ):
        raise StorageConfigError(gettext(
            "The provider type cannot be changed while archive objects "
            "are assigned. Create a new provider instead."
        ))
    current = {} if type_changed else dict(existing.get("config") or {})
    if type_changed:
        current = cls.default_config()
    values = values or {}
    valid_keys = {field.key for field in cls.config_fields}
    for key, value in values.items():
        if key not in valid_keys:
            continue
        field = next(item for item in cls.config_fields if item.key == key)
        text = "" if value is None else str(value)
        if field.secret and text in ("", "••••••••", "********"):
            continue
        current[key] = text
    current = _validate_provider_paths(cls, _apply_defaults(cls, current))
    errors = _config_errors(cls, current)
    if errors:
        raise StorageConfigError(" ".join(errors))
    clean_name = _clean_name(name if name is not None else existing["name"])
    _ensure_unique_name(clean_name, exclude_id=int(provider_id))
    database.update_storage_provider(
        provider_id,
        name=clean_name,
        provider_type=cls.type_id,
        config=encrypt_config(current),
        enabled=enabled if enabled is not None else existing["enabled"],
    )
    updated = get_provider_record(provider_id)
    settings = get_settings()
    if not updated.get("enabled") and settings.get("default_provider_id") == int(provider_id):
        fallback = next(
            (row["id"] for row in database.list_storage_providers() if row.get("enabled")),
            None,
        )
        save_settings({"default_provider_id": fallback})
    elif updated.get("enabled") and not settings.get("default_provider_id"):
        save_settings({"default_provider_id": provider_id})
    return updated


def delete_provider(provider_id: int) -> bool:
    existing = get_provider_record(provider_id)
    if not existing:
        return False
    removed = database.delete_storage_provider(provider_id)
    settings = get_settings()
    if settings.get("default_provider_id") == int(provider_id):
        fallback = next(
            (row["id"] for row in database.list_storage_providers() if row.get("enabled")),
            None,
        )
        save_settings({"default_provider_id": fallback})
    return removed


def resolve_provider(provider_id=None) -> dict:
    if provider_id is not None:
        row = get_provider_record(provider_id)
        if not row:
            raise StorageConfigError("Speicher-Anbieter nicht gefunden.")
        if not row.get("enabled"):
            raise StorageConfigError("Der Speicher-Anbieter ist deaktiviert.")
        if row.get("config_error"):
            raise StorageConfigError(row["config_error"])
        return row
    settings = get_settings()
    row = None
    default_id = settings.get("default_provider_id")
    if default_id:
        row = database.get_storage_provider(default_id)
        if row and not row.get("enabled"):
            row = None
    if row is None:
        row = next(
            (item for item in database.list_storage_providers() if item.get("enabled")),
            None,
        )
    if row is None:
        raise StorageConfigError("Kein aktiver Cloud-Speicher konfiguriert.")
    row = _decrypt_provider_row(row)
    if row.get("config_error"):
        raise StorageConfigError(row["config_error"])
    return row


def _persist_provider_config(provider_id, updated: dict) -> None:
    """Merge a provider's rotated credentials back into encrypted storage."""
    if not provider_id:
        return
    current = get_provider_record(provider_id)
    if not current:
        return
    merged = dict(current.get("config") or {})
    merged.update(updated or {})
    database.update_storage_provider(
        int(provider_id), config=encrypt_config(merged)
    )


def build_provider(row: dict):
    cls = get_provider_class(row.get("provider_type"))
    provider = cls(row.get("config") or {})
    provider_id = row.get("id")
    if provider_id is not None:
        provider.set_config_changed_callback(
            lambda updated: _persist_provider_config(provider_id, updated)
        )
    return provider


def test_provider(provider_id: int) -> dict:
    row = get_provider_record(provider_id)
    if not row:
        raise StorageConfigError(gettext("Storage provider not found."))
    if row.get("config_error"):
        message = row["config_error"]
        database.set_storage_provider_check(provider_id, False, message)
        return {"ok": False, "message": message}
    try:
        provider = build_provider(row)
        result = provider.test_connection() or {}
        account = str(result.get("account") or "")
        message = f"Verbindung erfolgreich{f' ({account})' if account else ''}."
        database.set_storage_provider_check(provider_id, True, message)
        database.set_storage_provider_error(provider_id, "")
        return {
            "ok": True,
            "message": message,
            "account": account,
            "quota": result.get("quota"),
        }
    except StorageError as exc:
        message = _safe_error(exc)
        database.set_storage_provider_check(provider_id, False, message)
        return {"ok": False, "message": message}
    except Exception as exc:
        message = f"Verbindung fehlgeschlagen: {_safe_error(exc)}"
        database.set_storage_provider_check(provider_id, False, message)
        return {"ok": False, "message": message}


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _oauth_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(signing_secret(), salt=OAUTH_SALT)


def make_oauth_state(provider_id: int) -> str:
    return _oauth_serializer().dumps({"pid": int(provider_id)})


def verify_oauth_state(state: str) -> int:
    try:
        payload = _oauth_serializer().loads(str(state or ""), max_age=OAUTH_MAX_AGE)
    except SignatureExpired as exc:
        raise StorageConfigError(gettext("The OAuth link has expired. Please start again.")) from exc
    except (BadSignature, TypeError, ValueError) as exc:
        raise StorageConfigError(gettext("Invalid OAuth state.")) from exc
    if not isinstance(payload, dict):
        raise StorageConfigError(gettext("Invalid OAuth state."))
    provider_id = _clean_provider_id(payload.get("pid"))
    if not provider_id:
        raise StorageConfigError(gettext("Invalid OAuth state."))
    return provider_id


def oauth_authorize_url(provider_id: int, redirect_uri: str) -> str:
    row = get_provider_record(provider_id)
    if not row:
        raise StorageConfigError(gettext("Storage provider not found."))
    cls = get_provider_class(row["provider_type"])
    if not cls.oauth_supported:
        raise StorageConfigError(gettext("%(provider)s does not support OAuth.", provider=cls.label))
    if not cls.oauth_ready(row.get("config") or {}):
        raise StorageConfigError(
            gettext("Please save the client ID and client secret first, then connect.")
        )
    state = make_oauth_state(provider_id)
    return cls.oauth_authorize_url(row.get("config") or {}, redirect_uri, state)


def oauth_complete(provider_id: int, code: str, redirect_uri: str) -> dict:
    row = get_provider_record(provider_id)
    if not row:
        raise StorageConfigError(gettext("Storage provider not found."))
    cls = get_provider_class(row["provider_type"])
    if not cls.oauth_supported:
        raise StorageConfigError(gettext("%(provider)s does not support OAuth.", provider=cls.label))
    values = dict(row.get("config") or {})
    received = cls.oauth_exchange_code(values, code, redirect_uri)
    if not isinstance(received, dict):
        raise StorageError(gettext("The provider did not return valid OAuth data."))
    values.update({key: value for key, value in received.items() if value})
    database.update_storage_provider(
        provider_id, config=encrypt_config(values)
    )
    return get_provider_record(provider_id) or {}


# ---------------------------------------------------------------------------
# Local archive paths
# ---------------------------------------------------------------------------

def _base_dir() -> str:
    return os.path.realpath(config.BASE_DIR)


def local_abs_path(local_path: str) -> str:
    base = _base_dir()
    raw = str(local_path or "").strip()
    if not raw:
        raise StorageConfigError("Leerer lokaler Pfad.")
    if os.path.isabs(raw):
        full = os.path.realpath(raw)
    else:
        full = os.path.realpath(os.path.join(base, raw.replace("/", os.sep)))
    try:
        common = os.path.commonpath([base, full])
    except ValueError as exc:
        raise StorageConfigError("Ungültiger lokaler Pfad.") from exc
    if common != base:
        raise StorageConfigError("Der lokale Pfad liegt außerhalb des Archivs.")
    return full


def relative_local_path(local_path: str) -> str:
    base = _base_dir()
    full = local_abs_path(local_path)
    rel = os.path.relpath(full, base).replace(os.sep, "/")
    if rel in (".", "") or rel.startswith("../"):
        raise StorageConfigError("Ungültiger lokaler Pfad.")
    return rel


def classify_local_path(relative_path: str) -> tuple[str, str, str]:
    rel = str(relative_path or "").strip().strip("/")
    parts = [part for part in rel.split("/") if part]
    if not parts:
        return "", "", "other"
    filename = parts[-1]
    if parts[0] == THUMB_DIR:
        if len(parts) < 3:
            return "", filename, "other"
        return parts[1], filename, "thumb"
    if len(parts) < 2:
        return "", filename, "other"
    streamer = parts[0]
    middle = parts[1:-1]
    if "clips" in middle:
        return streamer, filename, "clip"
    if "chat_logs" in middle:
        return streamer, filename, "chat"
    return streamer, filename, "vod"


def _file_allowed(rel: str, settings: dict) -> bool:
    parts = rel.split("/")
    if len(parts) < 2:
        return False
    name = parts[-1]
    if name.startswith(".") or name.lower().endswith(SKIP_SUFFIXES):
        return False
    if is_transient_media_filename(name):
        return False
    if any(part in SKIP_DIRS for part in parts[:-1]):
        return False
    if any(part.startswith(".") and part != THUMB_DIR for part in parts[:-1]):
        return False
    streamer, filename, kind = classify_local_path(rel)
    if not streamer or not filename:
        return False
    ext = os.path.splitext(filename)[1].lower()
    if kind == "chat":
        return bool(settings.get("include_chat_logs")) and ext == ".jsonl"
    if kind == "thumb":
        return bool(settings.get("include_thumbnails")) and ext in THUMB_EXTENSIONS
    if kind in ("vod", "clip"):
        if ext in VIDEO_EXTENSIONS:
            return True
        if kind == "vod" and ext in SIDECAR_EXTENSIONS:
            return True
    return False


def _streamer_name(value: str) -> str:
    """Validate a streamer directory name without allowing drive/path tricks."""
    name = str(value or "").strip()
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or ":" in name
        or os.path.isabs(name)
        or os.path.splitdrive(name)[0]
        or any(ord(ch) < 32 for ch in name)
    ):
        raise StorageConfigError(gettext("Invalid streamer name."))
    return name


def collect_media_files(
    streamer: Optional[str] = None,
    *,
    settings: Optional[dict] = None,
    limit: Optional[int] = None,
) -> list[str]:
    settings = settings or get_settings()
    base = _base_dir()
    root = base
    if streamer:
        name = _streamer_name(streamer)
        root = os.path.join(base, name)
        try:
            root_real = os.path.realpath(root)
            if os.path.commonpath([base, root_real]) != base:
                return []
        except (OSError, ValueError):
            return []
        if os.path.islink(root) or not os.path.isdir(root):
            return []
    allow_thumbs = bool(settings.get("include_thumbnails"))
    found = []
    for dirpath, dirnames, filenames in os.walk(
        root, onerror=lambda _exc: None, followlinks=False
    ):
        dirnames[:] = [
            item
            for item in dirnames
            if (
                (item == THUMB_DIR and allow_thumbs)
                or (item not in SKIP_DIRS and not item.startswith("."))
            )
            and not os.path.islink(os.path.join(dirpath, item))
        ]
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            # Never archive symlinks: they can point outside the archive and
            # their target may disappear independently of the link.
            if os.path.islink(full):
                continue
            try:
                info = os.stat(full)
                if not stat.S_ISREG(info.st_mode):
                    continue
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                if not _file_allowed(rel, settings):
                    continue
                # Resolve once so a file that is swapped for a symlink while
                # walking cannot escape the archive.
                local_abs_path(rel)
            except (OSError, ValueError, StorageConfigError):
                continue
            found.append((info.st_mtime, rel))
    found.sort(key=lambda item: (item[0], item[1]))
    files = [rel for _mtime, rel in found]
    if limit is not None:
        files = files[: max(0, int(limit))]
    return files


def remote_path_for(provider_row: dict, relative_path: str) -> str:
    rel = str(relative_path or "").strip().strip("/")
    if not rel:
        raise StorageConfigError("Leerer Remote-Pfad.")
    cfg = provider_row.get("config") or {}
    base_path = str(cfg.get("base_path") or "").strip().strip("/")
    joined = f"{base_path}/{rel}" if base_path else rel
    return normalize_remote_path(joined)


# ---------------------------------------------------------------------------
# Object bookkeeping
# ---------------------------------------------------------------------------

def prepare_object(
    local_path: str,
    provider_row: dict,
    *,
    settings: Optional[dict] = None,
) -> dict:
    rel = relative_local_path(local_path)
    full = local_abs_path(rel)
    if not os.path.isfile(full):
        raise StorageConfigError(f"Lokale Datei nicht gefunden: {rel}")
    settings = settings or get_settings()
    if not _file_allowed(rel, settings):
        raise StorageConfigError(
            f"Die Datei ist kein zulässiges Archiv-Objekt: {rel}"
        )
    streamer, filename, kind = classify_local_path(rel)
    remote_path = remote_path_for(provider_row, rel)
    info = os.stat(full)
    return database.upsert_storage_object(
        provider_row["id"],
        rel,
        remote_path,
        streamer=streamer,
        filename=filename,
        kind=kind,
        local_size=info.st_size,
        local_mtime=info.st_mtime,
        status="pending",
    )


def _object_is_current(obj: dict, stat) -> bool:
    """True when the stored object already matches the local file exactly."""
    return bool(
        obj
        and obj.get("status") in ("uploaded", "restored")
        and not obj.get("local_deleted_at")
        and not obj.get("remote_deleted_at")
        and int(obj.get("local_size") or 0) == int(stat.st_size)
        and abs(float(obj.get("local_mtime") or 0) - float(stat.st_mtime)) < 1.0
    )


def local_file_is_current(obj: dict) -> bool:
    if not obj:
        return False
    try:
        full = local_abs_path(obj.get("local_path"))
        stat = os.stat(full)
    except (StorageConfigError, OSError):
        return False
    return _object_is_current(obj, stat)


def enqueue_upload(
    local_path: str,
    provider_id=None,
    *,
    force: bool = False,
    settings: Optional[dict] = None,
) -> dict:
    settings = settings or get_settings()
    provider_row = resolve_provider(provider_id)
    rel = relative_local_path(local_path)
    previous = database.find_storage_object(provider_row["id"], rel)
    obj = prepare_object(local_path, provider_row, settings=settings)
    if previous and str(previous.get("remote_path") or "") != str(obj.get("remote_path") or ""):
        # base_path changed: the old remote copy is not at the new location.
        database.update_storage_object(
            obj["id"],
            status="pending",
            progress=0,
            last_error="",
            remote_deleted_at=None,
        )
        obj = database.get_storage_object(obj["id"]) or obj
    full = local_abs_path(obj["local_path"])
    info = os.stat(full)
    if _object_is_current(obj, info) and not force:
        return obj
    if database.has_active_task(
        "storage_upload",
        obj.get("streamer"),
        obj.get("filename"),
        object_id=obj.get("id"),
    ):
        return obj
    database.update_storage_object(
        obj["id"],
        status="pending",
        local_size=info.st_size,
        local_mtime=info.st_mtime,
        local_deleted_at=None,
        remote_deleted_at=None,
        progress=0,
        last_error="",
    )
    task_id = database.add_task(
        "storage_upload",
        {
            "object_id": obj["id"],
            "provider_id": provider_row["id"],
            "streamer": obj.get("streamer") or "",
            "filename": obj.get("filename") or "",
        },
    )
    obj = database.get_storage_object(obj["id"]) or obj
    obj["task_id"] = task_id
    return obj


def enqueue_auto_upload(
    local_path: str,
    *,
    settings: Optional[dict] = None,
) -> Optional[dict]:
    """Queue an upload only when automatic cloud upload is enabled."""
    settings = settings or get_settings()
    if not settings.get("auto_upload"):
        return None
    if not local_path or not os.path.isfile(local_path):
        return None
    return enqueue_upload(local_path, settings=settings)


def enqueue_sync(provider_id=None, streamer: Optional[str] = None) -> Optional[int]:
    provider_row = resolve_provider(provider_id)
    if database.has_active_task(
        "storage_sync",
        streamer or None,
        None,
        provider_id=provider_row["id"],
    ):
        return None
    return database.add_task(
        "storage_sync",
        {
            "provider_id": provider_row["id"],
            "streamer": streamer or "",
        },
    )


def enqueue_cleanup(provider_id=None) -> Optional[int]:
    if database.has_active_task("storage_cleanup", None, None):
        return None
    payload = {}
    if provider_id is not None:
        payload["provider_id"] = int(provider_id)
    return database.add_task("storage_cleanup", payload)


def _local_copy_is_current(obj: dict, full_path: str) -> bool:
    """Check that a local file still matches the version that was uploaded.

    Older rows may not have local metadata yet; in that case the caller falls
    back to the previous existence/size checks instead of blocking forever.
    """
    expected_mtime = float(obj.get("local_mtime") or 0)
    if not expected_mtime:
        return True
    try:
        info = os.stat(full_path)
    except OSError:
        return False
    return (
        info.st_size == int(obj.get("local_size") or 0)
        and abs(info.st_mtime - expected_mtime) < 1.0
    )


def remote_delete_block_reason(obj: dict) -> str:
    """Return why deleting the remote copy would risk the only remaining copy."""
    if not obj:
        return "Archiv-Objekt nicht gefunden."
    if obj.get("remote_deleted_at"):
        return "Die Cloud-Kopie wurde bereits gelöscht."
    if obj.get("local_deleted_at"):
        return (
            "Die lokale Kopie wurde bereits entfernt. Stelle sie zuerst aus der "
            "Cloud wieder her, bevor du die Cloud-Kopie löschst."
        )
    if obj.get("status") not in ("uploaded", "restored"):
        return "Die Datei wurde noch nicht erfolgreich in die Cloud hochgeladen."
    if database.has_active_task("storage_upload", object_id=obj.get("id")):
        return "Für dieses Objekt läuft gerade ein Upload."
    try:
        full = local_abs_path(obj.get("local_path"))
    except StorageConfigError:
        return "Der lokale Pfad ist ungültig; die Cloud-Kopie bleibt geschützt."
    if not os.path.isfile(full):
        return (
            "Die lokale Kopie fehlt. Die Cloud-Kopie wird nicht gelöscht, damit "
            "kein Datenverlust entsteht."
        )
    if not _local_copy_is_current(obj, full):
        return (
            "Die lokale Kopie wurde nach dem Upload geändert. Lade sie erneut "
            "hoch, bevor du die Cloud-Kopie löschst."
        )
    remote_size = int(obj.get("remote_size") or 0)
    if remote_size > 0:
        try:
            local_size = os.path.getsize(full)
        except OSError:
            return "Die lokale Kopie ist nicht lesbar; die Cloud-Kopie bleibt erhalten."
        if local_size != remote_size:
            return (
                "Die lokale Kopie stimmt nicht mit der Cloud-Kopie überein. "
                "Die Cloud-Kopie bleibt erhalten."
            )
    return ""


def enqueue_delete(object_id: int) -> int:
    obj = database.get_storage_object(object_id)
    if not obj:
        raise StorageConfigError("Archiv-Objekt nicht gefunden.")
    if obj.get("remote_deleted_at"):
        return 0
    reason = remote_delete_block_reason(obj)
    if reason:
        raise StorageConfigError(reason)
    if database.has_active_task("storage_delete", object_id=obj["id"]):
        return 0
    return database.add_task(
        "storage_delete",
        {
            "object_id": obj["id"],
            "provider_id": obj["provider_id"],
            "streamer": obj.get("streamer") or "",
            "filename": obj.get("filename") or "",
        },
    )


def enqueue_restore(object_id: int) -> int:
    obj = database.get_storage_object(object_id)
    if not obj:
        raise StorageConfigError("Archiv-Objekt nicht gefunden.")
    if database.has_active_task("storage_restore", object_id=obj["id"]):
        return 0
    return database.add_task(
        "storage_restore",
        {
            "object_id": obj["id"],
            "provider_id": obj["provider_id"],
            "streamer": obj.get("streamer") or "",
            "filename": obj.get("filename") or "",
        },
    )


def enqueue_check(provider_id: int) -> int:
    return database.add_task("storage_check", {"provider_id": int(provider_id)})


# ---------------------------------------------------------------------------
# Remote verification and local retention
# ---------------------------------------------------------------------------

def remote_exists(provider, remote_path: str, expected_size: int = 0) -> bool:
    try:
        return bool(provider.exists(remote_path, expected_size))
    except Exception as exc:
        logger.warning(
            "Remote-Verifikation fehlgeschlagen für %s: %s",
            remote_path,
            _safe_error(exc),
        )
        return False


def delete_local_copy(object_id: int, *, verify: Optional[bool] = None) -> tuple[bool, str]:
    obj = database.get_storage_object(object_id)
    if not obj:
        return False, "Archiv-Objekt nicht gefunden."
    if obj.get("local_deleted_at"):
        return False, "Lokale Kopie wurde bereits entfernt."
    if obj.get("status") not in ("uploaded", "restored"):
        return False, "Die Datei wurde noch nicht erfolgreich hochgeladen."
    full = local_abs_path(obj["local_path"])
    if not os.path.isfile(full):
        database.update_storage_object(
            object_id,
            local_deleted_at=time.time(),
            last_error="Lokale Datei existiert nicht mehr.",
        )
        return False, "Lokale Datei existiert nicht mehr."
    if not _local_copy_is_current(obj, full):
        message = "Die lokale Datei wurde nach dem Upload geändert und bleibt erhalten."
        database.update_storage_object(object_id, last_error=message)
        return False, message
    if obj.get("kind") == "vod":
        try:
            from core import vod_watch_slots

            if vod_watch_slots.is_vod_watched(obj.get("streamer"), obj.get("filename")):
                return False, "Das VOD wird gerade angesehen und bleibt lokal."
        except Exception:
            return False, "Wiedergabestatus konnte nicht geprüft werden."
    settings = get_settings()
    should_verify = (
        settings.get("verify_before_local_delete", True)
        if verify is None
        else bool(verify)
    )
    if should_verify:
        try:
            provider_row = resolve_provider(obj["provider_id"])
            provider = build_provider(provider_row)
        except StorageError as exc:
            message = f"Cloud-Anbieter nicht verfügbar: {_safe_error(exc)}"
            database.update_storage_object(object_id, last_error=message)
            return False, message
        if not remote_exists(provider, obj["remote_path"], int(obj.get("remote_size") or 0)):
            message = "Cloud-Kopie konnte nicht verifiziert werden; lokale Datei bleibt erhalten."
            database.update_storage_object(object_id, last_error=message)
            return False, message
    try:
        os.remove(full)
    except OSError as exc:
        message = f"Lokale Datei konnte nicht gelöscht werden: {exc}"
        database.update_storage_object(object_id, last_error=message)
        return False, message
    database.update_storage_object(
        object_id,
        local_deleted_at=time.time(),
        last_error="",
    )
    return True, "Lokale Kopie entfernt."


def cleanup_local_copies(provider_id=None, *, limit: int = 100) -> dict:
    settings = get_settings()
    if not settings.get("delete_local_after_upload"):
        return {"checked": 0, "deleted": 0, "kept": 0, "disabled": True}
    retention = int(settings.get("local_retention_days") or 0)
    cutoff = time.time() - (retention * 86400)
    rows = database.list_storage_objects(
        provider_id=provider_id,
        status="uploaded",
        uploaded_before=cutoff,
        local_deleted=False,
        limit=limit,
    )
    deleted = 0
    kept = 0
    reasons = []
    for obj in rows:
        try:
            ok, message = delete_local_copy(obj["id"])
        except Exception as exc:
            ok, message = False, _safe_error(exc)
        if ok:
            deleted += 1
        else:
            kept += 1
            if message:
                reasons.append(f"{obj.get('filename')}: {message}")
    return {
        "checked": len(rows),
        "deleted": deleted,
        "kept": kept,
        "reasons": reasons[:20],
    }


# ---------------------------------------------------------------------------
# Worker task implementations
# ---------------------------------------------------------------------------

def _progress_callback(object_id: int, total: int):
    state = {"last": 0.0, "last_pct": -1.0, "warned": False}

    def callback(done: int, size: int):
        now = time.time()
        if now - state["last"] < 2.0:
            return
        state["last"] = now
        denominator = int(size or total or 0)
        pct = (float(done) / denominator * 100.0) if denominator else 0.0
        pct = max(0.0, min(100.0, pct))
        if abs(pct - state["last_pct"]) < 1.0:
            return
        state["last_pct"] = pct
        try:
            database.update_storage_object(object_id, progress=round(pct, 1))
        except database.DB_ACCESS_ERRORS as exc:
            # Progress is cosmetic and rewritten every two seconds; a busy or
            # read-only database must not abort the transfer itself. Warn
            # once per transfer, then stay at debug so a long upload during a
            # lock does not write a line every two seconds.
            level = logging.DEBUG if state["warned"] else logging.WARNING
            state["warned"] = True
            logger.log(level, "storage object %s: progress not saved: %s", object_id, exc)

    return callback


def run_upload(payload: dict) -> dict:
    object_id = _clean_provider_id(payload.get("object_id"))
    if not object_id:
        raise StorageConfigError("storage_upload ohne object_id.")
    obj = database.get_storage_object(object_id)
    if not obj:
        raise StorageConfigError("Archiv-Objekt nicht gefunden.")
    provider_row = resolve_provider(obj["provider_id"])
    full = local_abs_path(obj["local_path"])
    if not os.path.isfile(full):
        database.update_storage_object(
            object_id, status="failed", last_error="Lokale Datei fehlt."
        )
        raise StorageConfigError("Lokale Datei fehlt.")
    stat = os.stat(full)
    if _object_is_current(obj, stat) and not payload.get("force"):
        return {"ok": True, "skipped": True}
    database.update_storage_object(
        object_id,
        status="uploading",
        attempts=int(obj.get("attempts") or 0) + 1,
        progress=0,
        last_error="",
        local_size=stat.st_size,
        local_mtime=stat.st_mtime,
        local_deleted_at=None,
        remote_deleted_at=None,
    )
    provider = build_provider(provider_row)
    try:
        result = provider.upload_file(
            full,
            obj["remote_path"],
            progress=_progress_callback(object_id, stat.st_size),
        ) or {}
    except Exception as exc:
        message = _safe_error(exc)
        database.update_storage_object(
            object_id,
            status="failed",
            last_error=message,
        )
        database.set_storage_provider_error(provider_row["id"], message)
        raise
    try:
        remote_size = int(result.get("size") if result.get("size") is not None else stat.st_size)
    except (TypeError, ValueError):
        remote_size = stat.st_size
    if remote_size != stat.st_size:
        message = (
            f"Upload unvollständig: Anbieter meldet {remote_size} Bytes, "
            f"lokal sind {stat.st_size} Bytes vorhanden."
        )
        database.update_storage_object(
            object_id, status="failed", last_error=message
        )
        database.set_storage_provider_error(provider_row["id"], message)
        raise StorageError(message, provider=provider_row.get("provider_type"))
    database.update_storage_object(
        object_id,
        status="uploaded",
        remote_size=remote_size,
        remote_modified=str(result.get("modified_at") or ""),
        remote_checksum=str(result.get("checksum") or ""),
        uploaded_at=time.time(),
        progress=100,
        last_error="",
        local_deleted_at=None,
        remote_deleted_at=None,
    )
    database.set_storage_provider_error(provider_row["id"], "")
    settings = get_settings()
    if settings.get("delete_local_after_upload") and int(
        settings.get("local_retention_days") or 0
    ) <= 0:
        try:
            delete_local_copy(object_id)
        except Exception as exc:
            logger.warning(
                "Lokale Löschung nach Upload fehlgeschlagen: %s", _safe_error(exc)
            )
    return {"ok": True, "remote_path": obj["remote_path"], "size": stat.st_size}


def run_sync(payload: dict) -> dict:
    provider_id = _clean_provider_id(payload.get("provider_id"))
    streamer = str(payload.get("streamer") or "").strip() or None
    provider_row = resolve_provider(provider_id)
    settings = get_settings()
    max_files = int(settings.get("max_sync_files") or 500)
    files = collect_media_files(streamer, settings=settings)
    queued = 0
    skipped = 0
    candidates = 0
    errors = []
    for rel in files:
        try:
            existing = database.find_storage_object(provider_row["id"], rel)
            if existing and local_file_is_current(existing):
                # A changed base_path moves the remote target.  Do not skip a
                # current local file when its stored remote path is stale.
                if str(existing.get("remote_path") or "") == remote_path_for(provider_row, rel):
                    skipped += 1
                    continue
            if candidates >= max_files:
                break
            candidates += 1
            obj = enqueue_upload(rel, provider_row["id"], settings=settings)
            if obj.get("task_id"):
                queued += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append(f"{rel}: {_safe_error(exc)}")
    schedule = database.get_system_state(SCHEDULE_KEY, {}) or {}
    if not isinstance(schedule, dict):
        schedule = {}
    schedule.update(
        {
            "last_sync_at": time.time(),
            "provider_id": provider_row["id"],
            "queued": queued,
            "skipped": skipped,
            "files": len(files),
            "errors": errors[:20],
        }
    )
    database.set_system_state(SCHEDULE_KEY, schedule)
    if settings.get("delete_local_after_upload"):
        try:
            cleanup_local_copies(provider_row["id"])
            schedule["last_cleanup_at"] = time.time()
            database.set_system_state(SCHEDULE_KEY, schedule)
        except Exception as exc:
            logger.warning("Lokale Aufräumung nach Sync fehlgeschlagen: %s", _safe_error(exc))
    return {
        "ok": True,
        "provider_id": provider_row["id"],
        "files": len(files),
        "candidates": candidates,
        "queued": queued,
        "skipped": skipped,
        "errors": errors[:20],
    }


def run_delete(payload: dict) -> dict:
    object_id = _clean_provider_id(payload.get("object_id"))
    if not object_id:
        raise StorageConfigError("storage_delete ohne object_id.")
    obj = database.get_storage_object(object_id)
    if not obj:
        raise StorageConfigError("Archiv-Objekt nicht gefunden.")
    if obj.get("remote_deleted_at"):
        return {"ok": True, "already_deleted": True}
    reason = remote_delete_block_reason(obj)
    if reason:
        database.update_storage_object(object_id, last_error=reason)
        raise StorageConfigError(reason)
    try:
        provider_row = resolve_provider(obj["provider_id"])
        provider = build_provider(provider_row)
        provider.delete(obj["remote_path"])
    except Exception as exc:
        message = _safe_error(exc)
        database.update_storage_object(object_id, last_error=message)
        database.set_storage_provider_error(obj["provider_id"], message)
        raise
    database.update_storage_object(
        object_id,
        status="remote_deleted",
        remote_deleted_at=time.time(),
        last_error="",
    )
    database.set_storage_provider_error(obj["provider_id"], "")
    return {"ok": True, "remote_path": obj["remote_path"]}


def run_restore(payload: dict) -> dict:
    object_id = _clean_provider_id(payload.get("object_id"))
    if not object_id:
        raise StorageConfigError("storage_restore ohne object_id.")
    obj = database.get_storage_object(object_id)
    if not obj:
        raise StorageConfigError("Archiv-Objekt nicht gefunden.")
    if obj.get("remote_deleted_at"):
        message = "Die Cloud-Kopie wurde bereits gelöscht."
        database.update_storage_object(object_id, last_error=message)
        raise StorageConfigError(message)
    try:
        provider_row = resolve_provider(obj["provider_id"])
        full = local_abs_path(obj["local_path"])
        expected = int(obj.get("remote_size") or 0)
        if os.path.isfile(full):
            size = os.path.getsize(full)
            if expected > 0 and size == expected:
                database.update_storage_object(
                    object_id,
                    status="restored",
                    local_deleted_at=None,
                    local_size=size,
                    local_mtime=os.path.getmtime(full),
                    last_error="",
                )
                return {"ok": True, "already_local": True, "size": size}
            # If the remote size is unknown we cannot prove that an existing
            # local file is complete. Download it again instead of trusting it.
        parent = os.path.dirname(full)
        os.makedirs(parent, exist_ok=True)
        if expected > 0:
            free = shutil.disk_usage(parent).free
            if free < expected + (64 * 1024 * 1024):
                raise StorageConfigError(
                    "Nicht genügend lokaler Speicherplatz für die Wiederherstellung."
                )
        provider = build_provider(provider_row)
        temp_path = full + ".restore"
        try:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            provider.download_file(
                obj["remote_path"],
                temp_path,
                progress=_progress_callback(object_id, expected),
            )
            if not os.path.isfile(temp_path):
                raise StorageError("Die Cloud-Datei wurde nicht lokal geschrieben.")
            size = os.path.getsize(temp_path)
            if expected > 0 and size != expected:
                raise StorageError(
                    f"Wiederherstellung unvollständig ({size} von {expected} Bytes)."
                )
            os.replace(temp_path, full)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        database.update_storage_object(
            object_id,
            status="restored",
            local_deleted_at=None,
            local_size=size,
            local_mtime=os.path.getmtime(full),
            remote_size=size if expected <= 0 else expected,
            progress=100,
            last_error="",
        )
        database.set_storage_provider_error(obj["provider_id"], "")
        return {"ok": True, "size": size}
    except Exception as exc:
        message = _safe_error(exc)
        database.update_storage_object(object_id, last_error=message)
        try:
            database.set_storage_provider_error(obj["provider_id"], message)
        except database.DB_ACCESS_ERRORS as db_exc:
            # Keep the restore error for the caller; the provider status line
            # is only a convenience for the storage page.
            logger.warning(
                "provider %s: restore error not recorded: %s",
                obj["provider_id"],
                db_exc,
            )
        raise


def run_check(payload: dict) -> dict:
    provider_id = _clean_provider_id(payload.get("provider_id"))
    if not provider_id:
        raise StorageConfigError("storage_check ohne provider_id.")
    result = test_provider(provider_id)
    if not result.get("ok"):
        raise StorageError(result.get("message") or "Verbindungstest fehlgeschlagen.")
    return result


def run_cleanup(payload: dict) -> dict:
    provider_id = _clean_provider_id(payload.get("provider_id"))
    return cleanup_local_copies(provider_id)


# ---------------------------------------------------------------------------
# Periodic scheduling and admin context
# ---------------------------------------------------------------------------

def tick() -> list[str]:
    """Called from the worker maintenance loop; enqueues due background work."""
    actions = []
    settings = get_settings()
    providers = [row for row in database.list_storage_providers() if row.get("enabled")]
    if not providers:
        return actions
    now = time.time()
    schedule = database.get_system_state(SCHEDULE_KEY, {}) or {}
    if not isinstance(schedule, dict):
        schedule = {}

    if settings.get("auto_upload"):
        interval = int(settings.get("auto_sync_interval_minutes") or 0)
        if interval > 0:
            last = float(schedule.get("last_sync_at") or 0)
            if now - last >= interval * 60:
                try:
                    task_id = enqueue_sync()
                    if task_id:
                        # Record the attempt immediately so a failing sync is
                        # retried on the configured interval, not every loop.
                        schedule["last_sync_at"] = now
                        database.set_system_state(SCHEDULE_KEY, schedule)
                        actions.append(f"storage_sync:{task_id}")
                except Exception as exc:
                    logger.warning("Automatischer Speicher-Sync fehlgeschlagen: %s", _safe_error(exc))

    if settings.get("delete_local_after_upload"):
        interval = int(settings.get("cleanup_interval_minutes") or 0)
        last_cleanup = float(schedule.get("last_cleanup_at") or 0)
        if interval > 0 and now - last_cleanup >= interval * 60:
            try:
                task_id = enqueue_cleanup()
                if task_id:
                    schedule["last_cleanup_at"] = now
                    database.set_system_state(SCHEDULE_KEY, schedule)
                    actions.append(f"storage_cleanup:{task_id}")
            except Exception as exc:
                logger.warning("Automatische Speicher-Aufräumung fehlgeschlagen: %s", _safe_error(exc))
    return actions


def summary(provider_id=None) -> dict:
    return database.storage_object_summary(provider_id)


def admin_context() -> dict:
    settings = get_settings()
    providers = list_providers()
    default_id = settings.get("default_provider_id")
    default_provider = next(
        (
            row
            for row in providers
            if row["id"] == default_id and row.get("enabled")
        ),
        None,
    )
    if default_provider is None:
        default_provider = next((row for row in providers if row.get("enabled")), None)
    objects = database.list_storage_objects(limit=150)
    for obj in objects:
        try:
            obj["local_exists"] = os.path.isfile(local_abs_path(obj.get("local_path")))
        except (StorageConfigError, OSError):
            obj["local_exists"] = False
    return {
        "providers": providers,
        "catalog": provider_catalog(),
        "settings": settings,
        "summary": summary(),
        "objects": objects,
        "default_provider": default_provider,
        "encryption_ready": encryption_ready(),
        "schedule": database.get_system_state(SCHEDULE_KEY, {}) or {},
    }


def format_timestamp(value) -> str:
    if not value:
        return "–"
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return "–"


def format_bytes(value) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(number) < 1024.0 or unit == "PB":
            return f"{number:.1f} {unit}" if unit != "B" else f"{int(number)} B"
        number /= 1024.0
    return f"{number:.1f} PB"
