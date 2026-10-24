"""Google Drive archive for NachtSilo Tube.

Creates/uses a `nachtsilo.tube` root folder, one subfolder per streamer,
uploads local archive MP4s (+ matching chat JSONL sidecars), and indexes
Drive-only videos for private playback through NachtSilo Tube.
"""

from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import logging
import os
import random
import re
import secrets
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone

from core import config
from core import database
from core.mp4_utils import is_archive_mp4_filename, is_transient_media_filename

logger = logging.getLogger("gdrive")

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHAT_MIME = "application/x-ndjson"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
VISIBILITY_KEY = "nachtsilo_visibility"
ROOT_SENTINEL = "__root__"
# Größere Chunks (64MB statt 8MB) reduzieren die Anzahl der API-Calls massiv (Quota-Schonung)
UPLOAD_CHUNK = 64 * 1024 * 1024


def chat_subdir_name():
    return str(os.environ.get("VOD_CHAT_SUBDIR", "chat_logs") or "chat_logs")


def vods_subdir_name():
    """Drive subfolder under each streamer for archive VODs (Beatek/vods)."""
    return str(os.environ.get("GDRIVE_VODS_SUBDIR", "vods") or "vods").strip() or "vods"


def is_vods_folder_name(name):
    return str(name or "").strip().lower() == vods_subdir_name().lower()


SKIP_FOLDER_NAMES = {
    ".git",
    ".hls",
    ".hls_live",
    ".meta",
    ".ruff_cache",
    ".thumbs",
    ".vodcache",
    "__pycache__",
    "chat_logs",
    "clips",
    "files",
    "node_modules",
    "scripts",
    "static",
    "tests",
    "venv",
    "vods",
}

# Drive sync walks content folders (vods, Zellencasts, …) but skips system/junk dirs.
# chat_logs is handled separately for JSONL sidecars.
DRIVE_WALK_SKIP_FOLDERS = {
    ".git",
    ".hls",
    ".hls_live",
    ".meta",
    ".ruff_cache",
    ".thumbs",
    ".vodcache",
    "__pycache__",
    "node_modules",
    "scripts",
    "static",
    "tests",
    "venv",
}


def is_drive_walk_skip_folder(name):
    """True for folders that must not be walked when indexing Drive media."""
    text = str(name or "").strip().lower()
    if not text:
        return True
    if text == chat_subdir_name().lower():
        return True
    return text in DRIVE_WALK_SKIP_FOLDERS

_SESSION = None
_SESSION_ERROR = ""


class DriveError(RuntimeError):
    pass


def enabled():
    return bool(getattr(config, "GDRIVE_ENABLED", False))


def normalize_visibility(value):
    return database._normalize_visibility(value)


def cache_bust_path():
    return os.path.join(config.META_DIR, "gdrive_cache_bust")


def status_path():
    return os.path.join(config.META_DIR, "gdrive_status.json")


def bust_media_cache():
    path = cache_bust_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".bust", dir=os.path.dirname(path))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_status():
    from core import database
    return database.get_system_state("gdrive_status", default={})


def save_status(data):
    from core import database
    database.set_system_state("gdrive_status", data or {})


def _credential_paths():
    sa_path = str(getattr(config, "GDRIVE_SERVICE_ACCOUNT_FILE", "") or "").strip()
    token_path = str(getattr(config, "GDRIVE_OAUTH_TOKEN_FILE", "") or "").strip()
    return sa_path, token_path


def configured():
    if not enabled():
        return False
    sa_path, token_path = _credential_paths()
    return bool((sa_path and os.path.isfile(sa_path)) or (token_path and os.path.isfile(token_path)))


def auth_mode():
    sa_path, token_path = _credential_paths()
    if sa_path and os.path.isfile(sa_path):
        return "service_account"
    if token_path and os.path.isfile(token_path):
        return "oauth"
    return ""


def _load_credentials():
    sa_path, token_path = _credential_paths()
    try:
        if sa_path and os.path.isfile(sa_path):
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                sa_path,
                scopes=[DRIVE_SCOPE],
            )
        if token_path and os.path.isfile(token_path):
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(token_path, scopes=[DRIVE_SCOPE])
            if creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request

                creds.refresh(Request())
            return creds
    except Exception as exc:
        raise DriveError(f"Drive-Anmeldung fehlgeschlagen: {exc}") from exc
    raise DriveError("Keine Drive-Credentials gefunden.")


def get_session(force=False):
    global _SESSION, _SESSION_ERROR
    if _SESSION is not None and not force:
        return _SESSION
    if not configured():
        _SESSION_ERROR = "Drive ist nicht konfiguriert."
        raise DriveError(_SESSION_ERROR)
    try:
        from google.auth.transport.requests import AuthorizedSession

        from requests.adapters import HTTPAdapter
        creds = _load_credentials()
        sess = AuthorizedSession(creds)
        adapter = HTTPAdapter(pool_connections=25, pool_maxsize=50, max_retries=1)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _SESSION = sess
        _SESSION_ERROR = ""
        return _SESSION
    except DriveError:
        _SESSION = None
        raise
    except Exception as exc:
        _SESSION = None
        _SESSION_ERROR = str(exc)
        raise DriveError(f"Drive-Session fehlgeschlagen: {exc}") from exc


def _escape_q(value):
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def _rfc3339_ts(value):
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _drive_params(extra=None):
    params = {
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    drive_id = str(getattr(config, "GDRIVE_SHARED_DRIVE_ID", "") or "").strip()
    if drive_id:
        params["driveId"] = drive_id
        params["corpora"] = "drive"
    if extra:
        params.update(extra)
    return params


def _raise_for_drive(response, action):
    if response.status_code < 400:
        return
    detail = ""
    try:
        payload = response.json()
        detail = str(((payload.get("error") or {}).get("message")) or "")[:240]
    except Exception:
        detail = (response.text or "")[:240]
    raise DriveError(f"{action} fehlgeschlagen ({response.status_code}): {detail or 'Drive-Fehler'}")


def _request(method, url, **kwargs):
    session = get_session()
    timeout = kwargs.pop("timeout", 60)
    last = None
    for attempt in range(5):
        response = session.request(method, url, timeout=timeout, **kwargs)
        last = response
        if response.status_code == 401 and attempt == 0:
            session = get_session(force=True)
            continue
        if response.status_code in (403, 429, 503):
            detail = ""
            try:
                detail = str(((response.json().get("error") or {}).get("message")) or "")
            except Exception:
                detail = (response.text or "")[:120]
            retryable = response.status_code in (429, 503) or "rate limit" in detail.lower()
            if retryable and attempt < 4:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = min(60.0, (2 ** attempt) + random.uniform(0.1, 1.5))
                time.sleep(max(0.5, delay))
                continue
        return response
    return last


VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".webm")


def is_playable_video(filename, mime_type=""):
    name = os.path.basename(str(filename or ""))
    if not name or is_transient_media_filename(name):
        return False
    mime = str(mime_type or "").strip().lower()
    if mime == FOLDER_MIME or mime.startswith("application/vnd.google-apps."):
        return False
    lower = name.lower()
    if any(lower.endswith(ext) for ext in VIDEO_EXTS):
        return True
    if mime.startswith("video/"):
        return True
    return False


def is_chat_log(filename, mime_type=""):
    name = os.path.basename(str(filename or ""))
    if not name or not name.lower().endswith(".jsonl"):
        return False
    mime = str(mime_type or "").strip().lower()
    if mime == FOLDER_MIME:
        return False
    return True


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif")
AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus", ".wma")


def is_preview_image(filename, mime_type=""):
    mime = str(mime_type or "").strip().lower()
    if mime.startswith("image/") and "vnd.google-apps" not in mime:
        return True
    name = os.path.basename(str(filename or "")).lower()
    return any(name.endswith(ext) for ext in IMAGE_EXTS)


def is_audio_file(filename, mime_type=""):
    mime = str(mime_type or "").strip().lower()
    if mime.startswith("audio/") and "vnd.google-apps" not in mime:
        return True
    name = os.path.basename(str(filename or "")).lower()
    return any(name.endswith(ext) for ext in AUDIO_EXTS)


def is_drive_asset(filename, mime_type=""):
    """Non-video, non-chat files in a streamer folder (images, pdf, …)."""
    name = os.path.basename(str(filename or "")).strip()
    if not name or name.startswith("."):
        return False
    mime = str(mime_type or "").strip().lower()
    if mime == FOLDER_MIME:
        return False
    if is_playable_video(name, mime) or is_chat_log(name, mime):
        return False
    if mime.startswith("application/vnd.google-apps."):
        return False
    return True


def asset_kind(filename, mime_type=""):
    if is_preview_image(filename, mime_type):
        return "image"
    if is_audio_file(filename, mime_type):
        return "audio"
    mime = str(mime_type or "").strip().lower()
    name = os.path.basename(str(filename or "")).lower()
    if mime == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if mime.startswith("text/") or name.endswith((".txt", ".md", ".csv", ".log")):
        return "text"
    return "file"


def thumb_mirror_slugs(streamer):
    """Index + archive aliases that should share a Drive-cached thumbnail."""
    slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    aliases = set()
    if slug:
        aliases.add(slug)
        base = slug.rstrip("_")
        if base:
            aliases.add(base)
            aliases.add(f"{base}_")
    for src, title in folder_map().items():
        dest = normalize_folder_slug(title) or ""
        if slug and slug in (src, dest, (dest + "_") if dest else ""):
            aliases.add(src)
            if dest:
                aliases.add(dest)
                aliases.add(f"{dest}_")
        if slug and slug.rstrip("_") == (dest or "").rstrip("_"):
            aliases.add(src)
            if dest:
                aliases.add(dest)
    return sorted(a for a in aliases if a)


def local_archive_slug(streamer):
    """Prefer the on-disk archive folder (beatek_) for Drive index slug beatek."""
    slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    if not slug:
        return ""
    candidates = []
    for src, title in folder_map().items():
        dest = normalize_folder_slug(title) or ""
        if slug in (src, dest) or slug.rstrip("_") == (dest or "").rstrip("_"):
            candidates.append(src)
            if dest:
                candidates.append(dest)
    candidates.extend([slug, f"{slug}_", slug.rstrip("_")])
    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        path = os.path.join(config.BASE_DIR, cand)
        if os.path.isdir(path):
            # Prefer folders that already hold media, not empty chat-only mirrors.
            try:
                has_media = any(
                    name.endswith(".mp4") and not name.startswith(".")
                    for name in os.listdir(path)
                )
            except OSError:
                has_media = False
            if has_media:
                return cand
    for cand in seen:
        if os.path.isdir(os.path.join(config.BASE_DIR, cand)):
            return cand
    return slug


def chat_filename_for_vod(vod_filename):
    name = os.path.basename(str(vod_filename or ""))
    if not name.lower().endswith(".mp4"):
        return ""
    return f"{os.path.splitext(name)[0]}.jsonl"


def local_chat_path(streamer, vod_filename):
    slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    chat_name = chat_filename_for_vod(vod_filename)
    if not slug or not chat_name:
        return ""
    return os.path.join(config.BASE_DIR, slug, chat_subdir_name(), chat_name)


def normalize_folder_slug(name):
    raw = str(name or "").strip().lower()
    raw = raw.removeprefix("@")
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch == "_")
    if not cleaned or cleaned in SKIP_FOLDER_NAMES:
        return None
    if cleaned.startswith("."):
        return None
    return cleaned


def folder_map():
    raw = getattr(config, "GDRIVE_FOLDER_MAP", None)
    if isinstance(raw, dict):
        return {
            str(k).strip().lower(): str(v).strip()
            for k, v in raw.items()
            if str(k).strip() and str(v).strip()
        }
    return {}


def drive_folder_title(streamer):
    """Human Drive folder name for an archive slug (Beatek for beatek_)."""
    slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    mapped = folder_map().get(slug)
    if mapped:
        return mapped
    return slug


def drive_index_slug(streamer):
    """Canonical Drive index slug (beatek_ → beatek when mapped to Beatek)."""
    slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    title = drive_folder_title(slug)
    mapped_slug = normalize_folder_slug(title)
    return mapped_slug or slug


def _list_children(parent_id, folders_only=False):
    items = []
    page_token = None
    query = f"'{_escape_q(parent_id)}' in parents and trashed = false"
    if folders_only:
        query += f" and mimeType = '{FOLDER_MIME}'"
    fields = (
        "nextPageToken, files(id,name,mimeType,md5Checksum,size,modifiedTime,"
        "thumbnailLink,videoMediaMetadata,appProperties)"
    )
    while True:
        params = _drive_params({
            "q": query,
            "pageSize": "1000",
            "fields": fields,
            "pageToken": page_token or "",
        })
        if not page_token:
            params.pop("pageToken", None)
        response = _request("GET", f"{DRIVE_API}/files", params=params)
        _raise_for_drive(response, "Drive-Liste")
        payload = response.json() if response.content else {}
        items.extend(payload.get("files") or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return items


def iter_drive_tree_files(folder_id, *, max_depth=6, _depth=0, _seen=None):
    """Yield non-folder Drive items under ``folder_id``, including nested content folders.

    Skips ``chat_logs`` and other system folders. Walks user folders such as
    ``vods`` and ``Zellencasts`` so their videos appear on the streamer page.
    """
    folder_id = str(folder_id or "").strip()
    if not folder_id:
        return
    try:
        depth_limit = max(0, int(max_depth))
    except (TypeError, ValueError):
        depth_limit = 6
    if _depth > depth_limit:
        return
    seen = _seen if _seen is not None else set()
    if folder_id in seen:
        return
    seen.add(folder_id)
    try:
        children = _list_children(folder_id)
    except DriveError as exc:
        logger.warning("Drive-Walk %s: %s", folder_id, exc)
        return
    for item in children:
        mime = str(item.get("mimeType") or "")
        name = str(item.get("name") or "")
        file_id = str(item.get("id") or "").strip()
        if not file_id:
            continue
        if mime == FOLDER_MIME:
            if is_drive_walk_skip_folder(name):
                continue
            yield from iter_drive_tree_files(
                file_id,
                max_depth=depth_limit,
                _depth=_depth + 1,
                _seen=seen,
            )
            continue
        yield item


def _create_folder(name, parent_id=None):
    body = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    response = _request(
        "POST",
        f"{DRIVE_API}/files",
        params=_drive_params({"fields": "id,name"}),
        json=body,
    )
    _raise_for_drive(response, "Drive-Ordner")
    data = response.json()
    folder_id = str(data.get("id") or "").strip()
    if not folder_id:
        raise DriveError("Drive-Ordner ohne ID.")
    return folder_id


def _find_named_folder(name, parent_id=None):
    query = (
        f"name = '{_escape_q(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    if parent_id:
        query += f" and '{_escape_q(parent_id)}' in parents"
    response = _request(
        "GET",
        f"{DRIVE_API}/files",
        params=_drive_params({
            "q": query,
            "pageSize": "10",
            "fields": "files(id,name)",
        }),
    )
    _raise_for_drive(response, "Drive-Ordnersuche")
    files = (response.json() or {}).get("files") or []
    if not files:
        return ""
    return str(files[0].get("id") or "").strip()


def ensure_root_folder():
    configured_id = str(getattr(config, "GDRIVE_ROOT_FOLDER_ID", "") or "").strip()
    if configured_id:
        return configured_id
    status = load_status()
    cached = str(status.get("root_id") or "").strip()
    root_name = str(getattr(config, "GDRIVE_ROOT_NAME", "nachtsilo.tube") or "nachtsilo.tube").strip()
    if cached:
        return cached
    found = _find_named_folder(root_name)
    folder_id = found or _create_folder(root_name)
    status.update({"root_id": folder_id, "root_name": root_name})
    save_status(status)
    return folder_id


def ensure_streamer_folder(streamer, title=None):
    source = normalize_folder_slug(streamer)
    if not source:
        raise DriveError("Ungueltiger Streamer-Ordner.")
    index_slug = drive_index_slug(source)
    label = str(title or drive_folder_title(source)).strip() or index_slug
    existing = database.get_drive_folder(index_slug)
    if existing and existing.get("folder_id"):
        if label and existing.get("title") != label:
            return database.upsert_drive_folder(
                index_slug,
                existing["folder_id"],
                visibility=existing.get("visibility"),
                title=label,
            )
        return existing
    root_id = ensure_root_folder()
    folder_id = _find_named_folder(label, parent_id=root_id) or _create_folder(label, parent_id=root_id)
    vis = (existing or {}).get("visibility") or "members"
    return database.upsert_drive_folder(index_slug, folder_id, visibility=vis, title=label)


def ensure_chat_folder(streamer):
    """Ensure `<streamer>/chat_logs` exists on Drive; return folder_id."""
    parent = ensure_streamer_folder(streamer)
    parent_id = str((parent or {}).get("folder_id") or "").strip()
    if not parent_id:
        raise DriveError("Streamer-Ordner fehlt.")
    found = _find_named_folder(chat_subdir_name(), parent_id=parent_id)
    if found:
        return found
    return _create_folder(chat_subdir_name(), parent_id=parent_id)


def _find_vods_folder_id(parent_id):
    """Find vods/VODS (case-insensitive) under a streamer folder."""
    parent_id = str(parent_id or "").strip()
    if not parent_id:
        return ""
    canonical = vods_subdir_name()
    found = _find_named_folder(canonical, parent_id=parent_id)
    if found:
        return found
    # Existing Drive folders may be named VODS / Vods.
    for child in _list_children(parent_id, folders_only=True):
        if is_vods_folder_name(child.get("name")):
            return str(child.get("id") or "").strip()
    return ""


def ensure_vods_folder(streamer):
    """Ensure `<streamer>/vods` exists on Drive; return folder_id."""
    parent = ensure_streamer_folder(streamer)
    parent_id = str((parent or {}).get("folder_id") or "").strip()
    if not parent_id:
        raise DriveError("Streamer-Ordner fehlt.")
    found = _find_vods_folder_id(parent_id)
    if found:
        return found
    return _create_folder(vods_subdir_name(), parent_id=parent_id)


def migrate_root_vods_into_subdir(streamer, limit=80):
    """Move playable videos from streamer root into vods/ (keeps assets at root)."""
    parent = ensure_streamer_folder(streamer)
    parent_id = str((parent or {}).get("folder_id") or "").strip()
    if not parent_id:
        return 0
    vods_id = ensure_vods_folder(streamer)
    if not vods_id or vods_id == parent_id:
        return 0
    moved = 0
    for item in _list_children(parent_id):
        if moved >= max(1, int(limit or 80)):
            break
        mime = str(item.get("mimeType") or "")
        name = str(item.get("name") or "")
        file_id = str(item.get("id") or "").strip()
        if not file_id or mime == FOLDER_MIME:
            continue
        if not is_playable_video(name, mime):
            continue
        try:
            _move_file_to_folder(file_id, vods_id, parent_id)
            moved += 1
        except DriveError as exc:
            logger.warning("VOD-Migrate %s/%s: %s", streamer, name, exc)
    if moved:
        logger.info("Drive-VODs nach %s/%s verschoben: %s", streamer, vods_subdir_name(), moved)
        bust_media_cache()
    return moved


def _move_file_to_folder(file_id, new_parent_id, old_parent_id):
    file_id = str(file_id or "").strip()
    new_parent_id = str(new_parent_id or "").strip()
    old_parent_id = str(old_parent_id or "").strip()
    if not file_id or not new_parent_id:
        return False
    params = _drive_params({
        "addParents": new_parent_id,
        "fields": "id,parents",
    })
    if old_parent_id:
        params["removeParents"] = old_parent_id
    response = _request(
        "PATCH",
        f"{DRIVE_API}/files/{file_id}",
        params=params,
        json={},
        timeout=60,
    )
    _raise_for_drive(response, "Drive-Verschieben")
    return True


def merge_drive_folder(source_streamer, dest_title):
    """Move all files from source Drive folder into dest folder title, then remove source."""
    src_slug = normalize_folder_slug(source_streamer)
    dest_slug = normalize_folder_slug(dest_title)
    dest_label = str(dest_title or "").strip() or dest_slug
    if not src_slug or not dest_slug:
        raise DriveError("Ungueltige Ordner fuer Merge.")
    if src_slug == dest_slug:
        return {"ok": True, "moved": 0, "skipped": 0, "message": "bereits canonical"}

    dest = ensure_streamer_folder(dest_slug, title=dest_label)
    dest_id = str(dest.get("folder_id") or "").strip()
    src = database.get_drive_folder(src_slug)
    if not src or not src.get("folder_id"):
        return {"ok": True, "moved": 0, "skipped": 0, "message": "Quellordner fehlt"}

    src_id = str(src.get("folder_id") or "").strip()
    if not dest_id or not src_id or src_id == dest_id:
        database.delete_drive_folder(src_slug)
        return {"ok": True, "moved": 0, "skipped": 0, "message": "gleicher Ordner"}

    moved = 0
    skipped = 0

    def _merge_file_into_dest(file_id, name, mime, old_parent_id):
        nonlocal moved, skipped
        try:
            target_id = ensure_vods_folder(dest_slug) if is_playable_video(name, mime) else dest_id
            _move_file_to_folder(file_id, target_id, old_parent_id)
            moved += 1
        except DriveError as exc:
            logger.warning("Merge %s/%s: %s", src_slug, name, exc)
            skipped += 1

    def _merge_tree(parent_id, *, depth=0):
        nonlocal moved, skipped
        if depth > 6:
            return
        children = _list_children(parent_id)
        for item in children:
            mime = str(item.get("mimeType") or "")
            file_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            if not file_id:
                continue
            if mime == FOLDER_MIME and name == chat_subdir_name():
                dest_chat = ensure_chat_folder(dest_slug)
                for chat_item in _list_children(file_id):
                    cid = str(chat_item.get("id") or "").strip()
                    if not cid:
                        continue
                    try:
                        _move_file_to_folder(cid, dest_chat, file_id)
                        moved += 1
                    except DriveError as exc:
                        logger.warning("Chat-Merge %s: %s", chat_item.get("name"), exc)
                        skipped += 1
                try:
                    _request("DELETE", f"{DRIVE_API}/files/{file_id}", params=_drive_params(), timeout=30)
                except Exception:
                    pass
                continue
            if mime == FOLDER_MIME:
                if is_drive_walk_skip_folder(name) and not is_vods_folder_name(name):
                    skipped += 1
                    continue
                # Recurse into vods / Zellencasts / other content folders.
                _merge_tree(file_id, depth=depth + 1)
                try:
                    remaining = _list_children(file_id)
                    if not remaining:
                        _request("DELETE", f"{DRIVE_API}/files/{file_id}", params=_drive_params(), timeout=30)
                except Exception:
                    pass
                continue
            _merge_file_into_dest(file_id, name, mime, parent_id)

    _merge_tree(src_id)

    try:
        remaining = _list_children(src_id)
        if not remaining:
            _request("DELETE", f"{DRIVE_API}/files/{src_id}", params=_drive_params(), timeout=30)
    except Exception as exc:
        logger.warning("Quellordner nicht geloescht (%s): %s", src_slug, exc)

    database.delete_drive_folder(src_slug)
    bust_media_cache()
    return {
        "ok": True,
        "moved": moved,
        "skipped": skipped,
        "dest": dest_label,
        "dest_slug": dest_slug,
    }


def _patch_visibility(folder_id, visibility):
    vis = normalize_visibility(visibility)
    response = _request(
        "PATCH",
        f"{DRIVE_API}/files/{folder_id}",
        params=_drive_params({"fields": "id,appProperties"}),
        json={"appProperties": {VISIBILITY_KEY: vis}},
    )
    _raise_for_drive(response, "Drive-Sichtbarkeit")


def rename_remote_file(file_id, new_name):
    """Rename a Drive file in place. Returns updated metadata dict."""
    fid = str(file_id or "").strip()
    name = os.path.basename(str(new_name or "").strip())
    if not fid or not name or name in (".", ".."):
        raise DriveError("Ungueltiger Drive-Rename.")
    response = _request(
        "PATCH",
        f"{DRIVE_API}/files/{fid}",
        params=_drive_params({"fields": "id,name"}),
        json={"name": name},
        timeout=60,
    )
    _raise_for_drive(response, "Drive-Umbenennung")
    try:
        payload = response.json() if response is not None else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("id", fid)
    payload.setdefault("name", name)
    return payload


def set_folder_visibility(streamer, visibility):
    slug = drive_index_slug(streamer) or normalize_folder_slug(streamer) or str(streamer or "").strip()
    vis = normalize_visibility(visibility)
    if not slug:
        return None
    folder = database.get_drive_folder(slug)
    if enabled() and configured():
        try:
            folder = ensure_streamer_folder(streamer)
            if folder and folder.get("folder_id"):
                _patch_visibility(folder["folder_id"], vis)
        except DriveError as exc:
            logger.warning("Drive-Sichtbarkeit nicht geschrieben: %s", exc)
    if database.get_drive_folder(slug):
        row = database.set_drive_folder_visibility(slug, vis)
        bust_media_cache()
        return row
    row = database.upsert_drive_folder(
        slug,
        (folder or {}).get("folder_id") or "",
        visibility=vis,
        title=drive_folder_title(streamer),
    )
    bust_media_cache()
    return row


def _file_record(streamer, item, source="remote"):
    video_meta = item.get("videoMediaMetadata") or {}
    duration_ms = 0
    try:
        duration_ms = int(video_meta.get("durationMillis") or 0)
    except (TypeError, ValueError):
        duration_ms = 0
    app_props = item.get("appProperties") or {}
    return {
        "file_id": str(item.get("id") or "").strip(),
        "streamer": streamer,
        "filename": os.path.basename(str(item.get("name") or "").strip()),
        "md5": str(item.get("md5Checksum") or "").strip(),
        "size": int(item.get("size") or 0),
        "modified_at": _rfc3339_ts(item.get("modifiedTime")),
        "duration_ms": duration_ms,
        "mime_type": str(item.get("mimeType") or "").strip(),
        "thumbnail_url": str(item.get("thumbnailLink") or "").strip(),
        "source": source,
        "visibility": normalize_visibility(app_props.get(VISIBILITY_KEY)),
    }


def _mirror_thumb_file(src_path, dest_streamer, filename):
    """Hardlink/copy an existing thumb into an alias streamer folder."""
    if not src_path or not os.path.isfile(src_path) or not dest_streamer or not filename:
        return
    base = os.path.splitext(filename)[0]
    dest_dir = os.path.join(config.THUMB_ROOT, dest_streamer)
    dest_path = os.path.join(dest_dir, f"{base}.jpg")
    if os.path.isfile(dest_path):
        return
    try:
        os.makedirs(dest_dir, exist_ok=True)
        try:
            os.link(src_path, dest_path)
        except OSError:
            import shutil
            shutil.copy2(src_path, dest_path)
    except OSError as exc:
        logger.info("Thumb-Mirror uebersprungen (%s/%s): %s", dest_streamer, filename, exc)


def thumb_file_path(streamer, filename):
    """Primary local JPEG path for a streamer/filename pair."""
    slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    name = os.path.basename(str(filename or ""))
    if not slug or not name:
        return ""
    base = os.path.splitext(name)[0]
    return os.path.join(config.THUMB_ROOT, slug, f"{base}.jpg")


def local_thumb_ready(streamer, filename):
    """True when any alias folder already has a non-empty JPEG thumb."""
    name = os.path.basename(str(filename or ""))
    if not name:
        return False
    base = os.path.splitext(name)[0]
    for slug in thumb_mirror_slugs(streamer) or [streamer]:
        if not slug:
            continue
        candidate = os.path.join(config.THUMB_ROOT, slug, f"{base}.jpg")
        try:
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                return True
        except OSError:
            continue
    return False


def _cache_thumbnail(streamer, filename, thumbnail_url):
    if not streamer or not filename:
        return False
    base = os.path.splitext(filename)[0]
    mirrors = thumb_mirror_slugs(streamer) or [streamer]
    existing = ""
    for slug in mirrors:
        candidate = os.path.join(config.THUMB_ROOT, slug, f"{base}.jpg")
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            existing = candidate
            break
    if existing:
        for slug in mirrors:
            _mirror_thumb_file(existing, slug, filename)
        return True
    if not thumbnail_url:
        return False
    primary = mirrors[0]
    thumb_dir = os.path.join(config.THUMB_ROOT, primary)
    thumb_path = os.path.join(thumb_dir, f"{base}.jpg")
    try:
        session = get_session()
        response = session.get(thumbnail_url, timeout=30)
        if response.status_code != 200 or not response.content:
            return False
        os.makedirs(thumb_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".jpg", dir=thumb_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(response.content)
            os.replace(tmp_path, thumb_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        for slug in mirrors[1:]:
            _mirror_thumb_file(thumb_path, slug, filename)
        return True
    except Exception as exc:
        logger.info("Drive-Thumbnail uebersprungen (%s/%s): %s", streamer, filename, exc)
        return False


def enqueue_thumbnail(streamer, filename, input_path="", drive_file_id="", force=False):
    """Queue ffmpeg/Drive thumbnail generation when no local JPEG exists yet."""
    slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    index_slug = drive_index_slug(slug) if slug else ""
    name = os.path.basename(str(filename or ""))
    if not slug or not name:
        return False
    if not force and local_thumb_ready(slug, name):
        return False
    for check_slug in {slug, index_slug}:
        if check_slug and database.has_active_task("thumbnail", streamer=check_slug, filename=name):
            return False
    thumb_path = thumb_file_path(slug, name) or thumb_file_path(index_slug, name)
    if not thumb_path:
        return False
    local_path = str(input_path or "").strip()
    if not local_path:
        for candidate_slug in (slug, local_archive_slug(slug), index_slug):
            if not candidate_slug:
                continue
            candidate = os.path.join(config.BASE_DIR, candidate_slug, name)
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                local_path = candidate
                break
    file_id = str(drive_file_id or "").strip()
    if not file_id:
        row = database.get_drive_file(index_slug, name) or database.get_drive_file(slug, name)
        file_id = str((row or {}).get("file_id") or "").strip()
        if not local_path and row and row.get("thumbnail_url"):
            if _cache_thumbnail(slug, name, row.get("thumbnail_url")):
                return False
    if not local_path and not file_id:
        return False
    payload = {
        "thumb_path": thumb_path,
        "streamer": index_slug or slug,
        "filename": name,
    }
    if local_path:
        payload["input_path"] = local_path
    if file_id:
        payload["drive_file_id"] = file_id
    database.add_task("thumbnail", payload)
    logger.info("Thumbnail eingereiht: %s/%s", index_slug or slug, name)
    return True


def enqueue_missing_thumbnails(limit=30):
    """Queue thumbs for Drive videos the server has indexed but not rendered yet."""
    if not enabled():
        return 0
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 30
    if limit <= 0:
        return 0
    queued = 0
    for row in database.list_drive_files():
        if queued >= limit:
            break
        name = str(row.get("filename") or "")
        mime = str(row.get("mime_type") or "")
        streamer = str(row.get("streamer") or "")
        if not is_playable_video(name, mime):
            continue
        if local_thumb_ready(streamer, name):
            continue
        if enqueue_thumbnail(streamer, name, drive_file_id=row.get("file_id") or ""):
            queued += 1
    return queued


def _is_recording_lock_name(name):
    name = str(name or "")
    return name == ".recording.lock" or (
        name.startswith(".recording.") and name.endswith(".lock")
    )


def _is_active_recording(streamer):
    folder = os.path.join(config.BASE_DIR, streamer)
    if not os.path.isdir(folder):
        return False
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return any(_is_recording_lock_name(name) for name in names)


def _part_file_looks_active(path, *, max_age_sec=900):
    """True when a .part file was touched recently (likely still being written)."""
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return False
    return age <= max(60, int(max_age_sec or 900))


def _active_recording_basenames(streamer):
    """Basenames (*.mp4) currently being recorded in this archive folder."""
    folder = os.path.join(config.BASE_DIR, streamer)
    if not os.path.isdir(folder):
        return set()
    names = set()
    try:
        entries = os.listdir(folder)
    except OSError:
        return set()
    for entry in entries:
        if entry in (".current_recording.json",) or (
            entry.startswith(".current_recording.") and entry.endswith(".json")
        ) or (
            entry.startswith(".recording_status") and entry.endswith(".json")
        ):
            try:
                with open(os.path.join(folder, entry), "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            state = str(payload.get("state") or "").strip().lower()
            if state in ("offline", "idle", "stopped", "error"):
                continue
            raw = str(payload.get("filename") or payload.get("record_file") or "").strip()
            if not raw:
                continue
            base = os.path.basename(raw)
            stem, ext = os.path.splitext(base)
            if ext.lower() in (".mkv", ".ts", ".part"):
                names.add(f"{stem}.mp4")
            elif base.lower().endswith(".mp4"):
                names.add(base)
            else:
                names.add(base if base.lower().endswith(".mp4") else f"{base}.mp4")
        if entry.endswith(".mkv.part") or entry.endswith(".mp4.part"):
            part_path = os.path.join(folder, entry)
            if not _part_file_looks_active(part_path):
                continue
            stem = entry[: -len(".mkv.part")] if entry.endswith(".mkv.part") else entry[: -len(".mp4.part")]
            if stem:
                names.add(f"{stem}.mp4")
    return {n for n in names if n}


def _is_filename_active_recording(streamer, filename):
    name = os.path.basename(str(filename or ""))
    if not name:
        return False
    active = _active_recording_basenames(streamer)
    if name in active:
        return True
    stem = os.path.splitext(name)[0]
    return any(os.path.splitext(a)[0] == stem for a in active)


def _file_md5_hex(path):
    digest = hashlib.md5()
    buf = bytearray(4 * 1024 * 1024)
    with open(path, "rb") as handle:
        while True:
            n = handle.readinto(buf)
            if not n:
                break
            digest.update(memoryview(buf)[:n])
    return digest.hexdigest()


def iter_local_vods():
    base = config.BASE_DIR
    if not os.path.isdir(base):
        return
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return
    for name in names:
        slug = normalize_folder_slug(name)
        if not slug or slug != name:
            continue
        folder = os.path.join(base, name)
        if not os.path.isdir(folder):
            continue
        active_names = _active_recording_basenames(name)
        try:
            entries = os.scandir(folder)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                if not is_archive_mp4_filename(entry.name):
                    continue
                if entry.name in active_names or _is_filename_active_recording(name, entry.name):
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    continue
                if size <= 0:
                    continue
                yield name, entry.name, entry.path, size


def _start_resumable(folder_id, filename, size, mime="video/mp4", file_id=None):
    metadata = {"name": filename}
    if not file_id:
        metadata["parents"] = [folder_id]
    url = f"{DRIVE_UPLOAD_API}/files"
    params = _drive_params({"uploadType": "resumable", "fields": "id,name,md5Checksum,size,modifiedTime,mimeType,thumbnailLink,videoMediaMetadata,appProperties"})
    if file_id:
        url = f"{DRIVE_UPLOAD_API}/files/{file_id}"
        method = "PATCH"
    else:
        method = "POST"
    headers = {
        "X-Upload-Content-Type": mime,
        "X-Upload-Content-Length": str(int(size)),
        "Content-Type": "application/json; charset=UTF-8",
    }
    response = _request(method, url, params=params, json=metadata, headers=headers, timeout=60)
    _raise_for_drive(response, "Drive-Upload-Start")
    location = response.headers.get("Location") or response.headers.get("location")
    if not location:
        raise DriveError("Drive-Upload ohne Session-URL.")
    return location


def _put_file_chunks(upload_url, path, size):
    offset = 0
    with open(path, "rb") as handle:
        while offset < size:
            chunk = handle.read(UPLOAD_CHUNK)
            if not chunk:
                break
            end = offset + len(chunk) - 1
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{size}",
            }
            
            response = None
            for attempt in range(3):
                try:
                    response = get_session().put(upload_url, data=chunk, headers=headers, timeout=300)
                    if response.status_code in (200, 201, 308):
                        break
                    if response.status_code >= 500:
                        logger.warning(f"Drive-Upload 5xx Error (Versuch {attempt+1}): {response.status_code}")
                        time.sleep(2 ** attempt)
                        continue
                    _raise_for_drive(response, "Drive-Upload")
                except Exception as exc:
                    if attempt == 2:
                        raise DriveError(f"Drive-Upload fehlgeschlagen nach 3 Versuchen: {exc}")
                    logger.warning(f"Drive-Upload Exception (Versuch {attempt+1}): {exc}")
                    time.sleep(2 ** attempt)
            
            if response is None or response.status_code not in (200, 201, 308):
                raise DriveError("Drive-Upload fehlgeschlagen.")
            
            offset = end + 1
            if response.status_code in (200, 201):
                return response.json() if response.content else {}
    raise DriveError("Drive-Upload unvollstaendig.")


def upload_bytes_or_file(folder_id, filename, path, mime, existing=None, force=False):
    size = os.path.getsize(path)
    if existing and int(existing.get("size") or 0) == size and not force:
        return True, "bereits vorhanden", existing
    file_id = existing.get("file_id") if existing else None
    upload_url = _start_resumable(folder_id, filename, size, mime=mime, file_id=file_id)
    payload = _put_file_chunks(upload_url, path, size)
    return True, "hochgeladen", payload or {"id": file_id, "name": filename, "size": size, "mimeType": mime}


def upload_local_chat(streamer, vod_filename, force=False):
    """Upload matching chat JSONL next to the VOD under streamer/chat_logs/."""
    local_slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    index_slug = drive_index_slug(local_slug)
    chat_name = chat_filename_for_vod(vod_filename)
    if not local_slug or not chat_name:
        return False, "Kein Chat"
    path = local_chat_path(local_slug, vod_filename)
    if not path or not os.path.isfile(path):
        return False, "Chat fehlt lokal"
    size = os.path.getsize(path)
    if size <= 0:
        return False, "Chat leer"
    existing = database.get_drive_file(index_slug, chat_name) or database.get_drive_file(local_slug, chat_name)
    if existing and int(existing.get("size") or 0) == size and not force:
        return True, "bereits vorhanden"
    chat_folder_id = ensure_chat_folder(local_slug)
    ok, msg, payload = upload_bytes_or_file(
        chat_folder_id,
        chat_name,
        path,
        CHAT_MIME,
        existing=existing,
        force=force,
    )
    if not ok:
        return False, msg
    record = _file_record(
        index_slug,
        payload or {"id": (existing or {}).get("file_id"), "name": chat_name, "size": size, "mimeType": CHAT_MIME},
        source="upload",
    )
    if not record.get("file_id") and existing:
        record["file_id"] = existing.get("file_id")
    record["size"] = size
    record["mime_type"] = CHAT_MIME
    database.upsert_drive_file(record)
    return True, msg


def download_drive_file(file_id, dest_path):
    file_id = str(file_id or "").strip()
    if not file_id or not dest_path:
        return False
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    response = open_media(file_id)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".part", dir=os.path.dirname(dest_path))
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest_path)
        try:
            os.chmod(dest_path, 0o640)
        except OSError:
            pass
        return True
    finally:
        try:
            response.close()
        except Exception:
            pass
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def ensure_local_chat(streamer, vod_filename):
    """Return local chat path, downloading from Drive when only remote copy exists."""
    local_slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    index_slug = drive_index_slug(local_slug)
    chat_name = chat_filename_for_vod(vod_filename)
    path = local_chat_path(local_slug, vod_filename) if local_slug and chat_name else ""
    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    if not enabled() or not configured() or not local_slug or not chat_name:
        return path if path and os.path.isfile(path) else ""
    row = database.get_drive_file(index_slug, chat_name) or database.get_drive_file(local_slug, chat_name)
    if not row or not row.get("file_id"):
        return ""
    try:
        if download_drive_file(row["file_id"], path):
            logger.info("Chat von Drive geladen: %s/%s", local_slug, chat_name)
            return path
    except Exception as exc:
        logger.warning("Chat-Download fehlgeschlagen %s/%s: %s", local_slug, chat_name, exc)
    return ""


def upload_local_vod(streamer, filename, force=False):
    local_slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    index_slug = drive_index_slug(local_slug)
    name = os.path.basename(str(filename or ""))
    if not local_slug or not is_archive_mp4_filename(name):
        return False, "Ungueltige Datei"
    if _is_filename_active_recording(local_slug, name):
        return False, "Aufnahme laeuft noch"
    path = os.path.realpath(os.path.join(config.BASE_DIR, local_slug, name))
    base = os.path.realpath(os.path.join(config.BASE_DIR, local_slug))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        return False, "Datei nicht gefunden"
    size = os.path.getsize(path)
    existing = database.get_drive_file(index_slug, name) or database.get_drive_file(local_slug, name)
    if existing and int(existing.get("size") or 0) == size and not force:
        chat_ok, chat_msg = upload_local_chat(local_slug, name, force=force)
        if chat_ok and chat_msg != "bereits vorhanden":
            logger.info("Drive-Chat %s/%s: %s", index_slug, chat_filename_for_vod(name), chat_msg)
        elif not chat_ok and chat_msg not in ("Kein Chat", "Chat fehlt lokal", "Chat leer"):
            logger.warning("Drive-Chat %s: %s", index_slug, chat_msg)
        try:
            vods_id = ensure_vods_folder(local_slug)
            parent = ensure_streamer_folder(local_slug)
            fid = str(existing.get("file_id") or "").strip()
            if fid and vods_id:
                _move_file_to_folder(fid, vods_id, parent.get("folder_id"))
        except DriveError as exc:
            logger.info("VOD bereits auf Drive, Ordner-Move uebersprungen: %s", exc)
        deleted, delete_msg = maybe_delete_local_after_upload(local_slug, name)
        if deleted:
            return True, "bereits vorhanden; lokal geloescht"
        if delete_msg and delete_msg not in ("disabled", "nicht lokal"):
            logger.info("Lokales VOD behalten %s/%s: %s", local_slug, name, delete_msg)
        return True, "bereits vorhanden"
    folder = ensure_streamer_folder(local_slug)
    vods_folder_id = ensure_vods_folder(local_slug)
    file_id = existing.get("file_id") if existing else None
    upload_url = _start_resumable(vods_folder_id, name, size, file_id=file_id)
    payload = _put_file_chunks(upload_url, path, size)
    record = _file_record(index_slug, payload or {"id": file_id, "name": name, "size": size}, source="upload")
    if not record.get("file_id") and file_id:
        record["file_id"] = file_id
    record["size"] = size
    if int(record.get("duration_ms") or 0) <= 0:
        try:
            from recorder import clip_engine

            local_sec = float(clip_engine.probe_duration_sec(path) or 0.0)
            if local_sec > 0:
                record["duration_ms"] = int(local_sec * 1000)
        except Exception as exc:
            logger.debug("Lokale Dauer vor Upload %s/%s: %s", local_slug, name, exc)
    database.upsert_drive_file(record)
    # If this was an update of a root-level file, ensure parent is vods/.
    try:
        final_id = str(record.get("file_id") or "").strip()
        if final_id and vods_folder_id:
            _move_file_to_folder(final_id, vods_folder_id, folder.get("folder_id"))
    except DriveError:
        pass
    chat_ok, chat_msg = upload_local_chat(local_slug, name, force=force)
    if chat_ok and chat_msg != "bereits vorhanden":
        logger.info("Drive-Chat %s/%s: %s", index_slug, chat_filename_for_vod(name), chat_msg)
    elif not chat_ok and chat_msg not in ("Kein Chat", "Chat fehlt lokal", "Chat leer"):
        logger.warning("Drive-Chat %s: %s", index_slug, chat_msg)
    bust_media_cache()
    deleted, delete_msg = maybe_delete_local_after_upload(local_slug, name)
    if deleted:
        return True, "hochgeladen; lokal geloescht"
    if delete_msg and delete_msg not in ("disabled", "nicht lokal"):
        logger.info("Lokales VOD behalten %s/%s: %s", local_slug, name, delete_msg)
    return True, "hochgeladen"


def _local_vod_busy(streamer, path):
    slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    if path and _is_filename_active_recording(slug, os.path.basename(path)):
        return "Aufnahme laeuft"
    if not path:
        return "kein Pfad"
    remux_tmp = f"{path}.remux.mp4"
    if os.path.isfile(remux_tmp) or os.path.isfile(f"{path}.part"):
        return "Remux/Part aktiv"
    try:
        if database.has_active_task("remux_vod", streamer=slug, filename=os.path.basename(path)):
            return "Remux-Task aktiv"
    except Exception:
        pass
    return ""


def seed_playback_cache_from_local(streamer, filename, local_path, *, pin_days=None):
    """Place a local VOD into the playback cache (link/copy) and pin it.

    Used before delete-after-upload so the newest files stay hot for the
    pin window without burning Drive download quota.
    """
    if not playback_cache_enabled():
        return False
    src = os.path.realpath(str(local_path or ""))
    if not src or not os.path.isfile(src):
        return False
    try:
        size = os.path.getsize(src)
    except OSError:
        return False
    if size <= 0:
        return False
    final_path, _part = playback_cache_paths(streamer, filename)
    if not final_path:
        return False
    if complete_playback_cache(streamer, filename, expected_size=size):
        pin_playback_cache(streamer, filename, days=pin_days)
        return True
    try:
        evict_playback_cache(size)
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        tmp = f"{final_path}.seed.tmp"
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        linked = False
        try:
            os.link(src, tmp)
            linked = True
        except OSError:
            shutil.copy2(src, tmp)
        os.replace(tmp, final_path)
        pin_playback_cache(streamer, filename, days=pin_days)
        logger.info(
            "Playback-Cache geseedet (%s) %s/%s (%s bytes)",
            "hardlink" if linked else "copy",
            streamer,
            os.path.basename(str(filename or "")),
            size,
        )
        return True
    except OSError as exc:
        logger.warning(
            "Playback-Cache Seed fehlgeschlagen %s/%s: %s",
            streamer,
            filename,
            exc,
        )
        try:
            if os.path.isfile(f"{final_path}.seed.tmp"):
                os.remove(f"{final_path}.seed.tmp")
        except OSError:
            pass
        return False


def keep_local_days():
    """How long local archive MP4s stay after Drive upload (playback source of truth)."""
    try:
        return max(0, min(90, int(getattr(config, "GDRIVE_KEEP_LOCAL_DAYS", 14))))
    except (TypeError, ValueError):
        return 14


def playback_hydrate_enabled():
    """Full Drive→cache downloads. Off by default — keep-local + seed instead."""
    return bool(getattr(config, "GDRIVE_PLAYBACK_HYDRATE", False))


def playback_hydrate_resume_enabled():
    """Continue incomplete .chunks for pinned VODs without starting cold full hydrates."""
    return bool(getattr(config, "GDRIVE_PLAYBACK_HYDRATE_RESUME", True))


def _vod_age_seconds(path, filename=None, row=None):
    """Best-effort age of a VOD for the keep-local window (0 = brand new)."""
    now = time.time()
    candidates = []
    if path:
        try:
            candidates.append(now - float(os.path.getmtime(path)))
        except OSError:
            pass
    if row is not None:
        try:
            modified = float((row or {}).get("modified_at") or 0)
            if modified > 0:
                candidates.append(max(0.0, now - modified))
        except (TypeError, ValueError):
            pass
    name = os.path.basename(str(filename or (path or "")))
    # archive pattern: …_YYYY-MM-DD_HH-MM-SS.mp4
    match = re.search(r"(20\d{2})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", name)
    if match:
        try:
            stamp = datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                int(match.group(5)),
                int(match.group(6)),
                tzinfo=timezone.utc,
            ).timestamp()
            candidates.append(max(0.0, now - stamp))
        except (TypeError, ValueError, OverflowError):
            pass
    if not candidates:
        return 0.0
    return min(candidates)


def local_within_keep_window(path, filename=None, row=None):
    days = keep_local_days()
    if days <= 0:
        return False
    return _vod_age_seconds(path, filename=filename, row=row) < (days * 86400.0)


def maybe_delete_local_after_upload(streamer, filename):
    """Remove local VOD (+ chat) after Drive copy is size+MD5 verified. Opt-in via config.

    Recent VODs stay local for ``GDRIVE_KEEP_LOCAL_DAYS`` so playback never hits
    Google Drive download quota during the hot window.
    """
    if not bool(getattr(config, "GDRIVE_DELETE_LOCAL_AFTER_UPLOAD", False)):
        return False, "disabled"
    local_slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    index_slug = drive_index_slug(local_slug)
    name = os.path.basename(str(filename or ""))
    if not local_slug or not name:
        return False, "ungueltig"
    path = os.path.realpath(os.path.join(config.BASE_DIR, local_slug, name))
    base = os.path.realpath(os.path.join(config.BASE_DIR, local_slug))
    if not path.startswith(base + os.sep):
        return False, "Pfad unsicher"
    if not os.path.isfile(path):
        return False, "nicht lokal"
    busy = _local_vod_busy(local_slug, path)
    if busy:
        return False, busy
    # Never drop the only local copy before a thumbnail exists — otherwise the
    # grid stays empty for Drive-only VODs without Google thumbnailLink.
    if not local_thumb_ready(local_slug, name):
        try:
            enqueue_thumbnail(local_slug, name, input_path=path)
        except Exception as exc:
            logger.warning("Thumbnail vor Delete enqueue %s/%s: %s", local_slug, name, exc)
        return False, "thumbnail fehlt"
    size = os.path.getsize(path)
    if size <= 0:
        return False, "leer"
    row = database.get_drive_file(index_slug, name) or database.get_drive_file(local_slug, name)
    if not row or not row.get("file_id"):
        return False, "nicht auf Drive"
    if int(row.get("size") or 0) != size:
        return False, "Drive-Groesse weicht ab"
    remote_md5 = str(row.get("md5") or "").strip().lower()
    if remote_md5:
        try:
            local_md5 = _file_md5_hex(path)
        except OSError as exc:
            return False, f"MD5 lokal fehlgeschlagen: {exc}"
        if local_md5.lower() != remote_md5:
            return False, "Drive-MD5 weicht ab"
    if local_within_keep_window(path, filename=name, row=row):
        # Warm the pin window cache early, but keep the archive file for playback.
        try:
            seed_playback_cache_from_local(local_slug, name, path)
        except Exception as exc:
            logger.warning("Playback-Cache Seed (keep-local) %s/%s: %s", local_slug, name, exc)
        return False, f"keep-local {keep_local_days()}d"
    # Past the keep window: seed a complete playback cache, then drop the archive.
    # Never delete while .vodcache is incomplete — that forces live Drive Ranges.
    try:
        seeded = seed_playback_cache_from_local(local_slug, name, path)
    except Exception as exc:
        logger.warning("Playback-Cache Seed vor Delete %s/%s: %s", local_slug, name, exc)
        seeded = False
    if not complete_playback_cache(local_slug, name, expected_size=size):
        if not seeded:
            try:
                seed_playback_cache_from_local(local_slug, name, path)
            except Exception:
                pass
        if not complete_playback_cache(local_slug, name, expected_size=size):
            return False, "cache unvollstaendig"
    try:
        os.remove(path)
    except OSError as exc:
        return False, f"Loeschen fehlgeschlagen: {exc}"
    logger.info("Lokales VOD geloescht nach Drive-Upload: %s/%s", local_slug, name)

    if bool(getattr(config, "GDRIVE_DELETE_LOCAL_CHAT", True)):
        chat_name = chat_filename_for_vod(name)
        chat_path = local_chat_path(local_slug, name)
        if chat_name and chat_path and os.path.isfile(chat_path):
            chat_row = (
                database.get_drive_file(index_slug, chat_name)
                or database.get_drive_file(local_slug, chat_name)
            )
            chat_size = os.path.getsize(chat_path)
            chat_ok = (
                chat_row
                and chat_row.get("file_id")
                and int(chat_row.get("size") or 0) == chat_size
                and chat_size > 0
            )
            chat_md5 = str((chat_row or {}).get("md5") or "").strip().lower()
            if chat_ok and chat_md5:
                try:
                    chat_ok = _file_md5_hex(chat_path).lower() == chat_md5
                except OSError:
                    chat_ok = False
            if chat_ok:
                try:
                    os.remove(chat_path)
                    logger.info("Lokaler Chat geloescht nach Drive-Upload: %s/%s", local_slug, chat_name)
                except OSError as exc:
                    logger.warning("Chat-Loeschen fehlgeschlagen %s/%s: %s", local_slug, chat_name, exc)

    # Small sidecar metadata is safe to drop once the VOD is Drive-only.
    for sidecar in (f"{path}.info.json", f"{os.path.splitext(path)[0]}.info.json"):
        if os.path.isfile(sidecar):
            try:
                os.remove(sidecar)
            except OSError:
                pass

    bust_media_cache()
    return True, "geloescht"


def delete_remote_file(file_id):
    file_id = str(file_id or "").strip()
    if not file_id:
        return False
    response = _request(
        "DELETE",
        f"{DRIVE_API}/files/{file_id}",
        params=_drive_params(),
        timeout=30,
    )
    if response.status_code in (200, 204, 404):
        database.delete_drive_file(file_id=file_id)
        bust_media_cache()
        return True
    _raise_for_drive(response, "Drive-Loeschen")
    return False


def get_file_metadata(file_id):
    response = _request(
        "GET",
        f"{DRIVE_API}/files/{file_id}",
        params=_drive_params({
            "fields": "id,name,mimeType,size,md5Checksum,videoMediaMetadata,modifiedTime,thumbnailLink,appProperties",
        }),
        timeout=30,
    )
    _raise_for_drive(response, "Drive-Metadaten")
    return response.json() if response.content else {}


def probe_drive_duration_ms(file_id):
    """ffprobe duration via authenticated Drive HTTP (Range-seekable)."""
    file_id = str(file_id or "").strip()
    if not file_id:
        return 0
    try:
        from recorder import clip_engine

        url, headers = media_http_source(file_id)
        sec = float(clip_engine.probe_duration_sec(url, http_headers=headers) or 0.0)
        if sec > 0:
            return int(sec * 1000)
    except Exception as exc:
        logger.warning("Drive-Dauer-Probe fehlgeschlagen %s: %s", file_id, exc)
    return 0


def refresh_file_duration(streamer, filename, force=False):
    """Fill missing duration_ms from Drive videoMediaMetadata or ffprobe."""
    local_slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    index_slug = drive_index_slug(local_slug)
    name = os.path.basename(str(filename or ""))
    row = database.get_drive_file(index_slug, name) or database.get_drive_file(local_slug, name)
    if not row or not row.get("file_id"):
        return 0
    existing_ms = int(row.get("duration_ms") or 0)
    if existing_ms > 0 and not force:
        return existing_ms
    file_id = str(row.get("file_id") or "").strip()
    duration_ms = 0
    try:
        meta = get_file_metadata(file_id)
        video_meta = (meta or {}).get("videoMediaMetadata") or {}
        duration_ms = int(video_meta.get("durationMillis") or 0)
    except Exception as exc:
        logger.debug("Drive-Metadaten-Dauer %s: %s", file_id, exc)
        duration_ms = 0
    if duration_ms <= 0:
        duration_ms = probe_drive_duration_ms(file_id)
    if duration_ms <= 0:
        return 0
    row = dict(row)
    row["duration_ms"] = duration_ms
    row["streamer"] = index_slug or row.get("streamer") or local_slug
    database.upsert_drive_file(row)
    bust_media_cache()
    return duration_ms


def enqueue_duration_probe(streamer, filename, force=False):
    if not enabled() or not configured():
        return False
    slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    name = os.path.basename(str(filename or ""))
    if not slug or not name:
        return False
    index_slug = drive_index_slug(slug)
    row = database.get_drive_file(index_slug, name) or database.get_drive_file(slug, name)
    if not row or not row.get("file_id"):
        return False
    if int(row.get("duration_ms") or 0) > 0 and not force:
        return False
    if database.has_active_task("drive_duration", streamer=slug, filename=name):
        return False
    if index_slug != slug and database.has_active_task("drive_duration", streamer=index_slug, filename=name):
        return False
    database.add_task("drive_duration", {
        "streamer": index_slug or slug,
        "filename": name,
        "force": bool(force),
    })
    return True


def enqueue_missing_durations(limit=40):
    """Queue ffprobe backfill for Drive videos that lack duration_ms."""
    if not enabled() or not configured():
        return 0
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 40
    if limit <= 0:
        return 0
    queued = 0
    for row in database.list_drive_files():
        if queued >= limit:
            break
        name = str(row.get("filename") or "")
        mime = str(row.get("mime_type") or "")
        if not is_playable_video(name, mime):
            continue
        if int(row.get("duration_ms") or 0) > 0:
            continue
        if enqueue_duration_probe(row.get("streamer") or "", name):
            queued += 1
    return queued


def playback_chunk_bytes():
    try:
        return max(256 * 1024, int(getattr(config, "GDRIVE_PLAYBACK_CHUNK_BYTES", 64 * 1024 * 1024)))
    except (TypeError, ValueError):
        return 64 * 1024 * 1024


def playback_readahead_bytes():
    try:
        return max(
            playback_chunk_bytes(),
            int(getattr(config, "GDRIVE_PLAYBACK_READAHEAD_BYTES", 128 * 1024 * 1024)),
        )
    except (TypeError, ValueError):
        return 128 * 1024 * 1024


def playback_live_range_bytes():
    """Closed Range size for interactive browser seeks (Drive alt=media proxy).

    Keep this much smaller than hydrate chunks so a scrub to mid-VOD can start
    streaming in milliseconds instead of blocking on a 64 MiB cache fill.
    """
    try:
        return max(
            256 * 1024,
            min(
                16 * 1024 * 1024,
                int(getattr(config, "GDRIVE_PLAYBACK_LIVE_RANGE_BYTES", 8 * 1024 * 1024)),
            ),
        )
    except (TypeError, ValueError):
        return 8 * 1024 * 1024


def playback_cache_enabled():
    return bool(getattr(config, "GDRIVE_PLAYBACK_CACHE", True))


def playback_cache_root():
    root = str(getattr(config, "GDRIVE_PLAYBACK_CACHE_DIR", "") or "").strip()
    if not root:
        root = os.path.join(getattr(config, "BASE_DIR", "."), ".vodcache")
    return os.path.realpath(root)


def playback_cache_max_bytes():
    try:
        return max(512 * 1024 * 1024, int(getattr(config, "GDRIVE_PLAYBACK_CACHE_MAX_BYTES", 256 * 1024 * 1024 * 1024)))
    except (TypeError, ValueError):
        return 256 * 1024 * 1024 * 1024


def playback_cache_jobs():
    try:
        return max(1, min(6, int(getattr(config, "GDRIVE_PLAYBACK_CACHE_JOBS", 6))))
    except (TypeError, ValueError):
        return 6


def playback_pin_newest_days():
    try:
        return max(0, min(90, int(getattr(config, "GDRIVE_PLAYBACK_PIN_NEWEST_DAYS", 7))))
    except (TypeError, ValueError):
        return 7


def _cache_slug_candidates(streamer):
    slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    if not slug:
        return []
    candidates = []
    local = local_archive_slug(slug)
    if local:
        candidates.append(local)
    candidates.extend([slug, f"{slug}_", slug.rstrip("_")])
    for src, title in folder_map().items():
        dest = normalize_folder_slug(title) or ""
        if slug in (src, dest) or slug.rstrip("_") == dest.rstrip("_"):
            candidates.extend([src, dest])
    seen = set()
    result = []
    for cand in candidates:
        if cand and cand not in seen:
            seen.add(cand)
            result.append(cand)
    return result


def _playback_cache_slug(streamer):
    candidates = _cache_slug_candidates(streamer)
    if not candidates:
        return normalize_folder_slug(streamer) or str(streamer or "").strip()
    cache_root = playback_cache_root()
    for cand in candidates:
        if os.path.isdir(os.path.join(cache_root, cand)):
            return cand
    base_dir = getattr(config, "BASE_DIR", ".")
    for cand in candidates:
        if os.path.isdir(os.path.join(base_dir, cand)):
            return cand
    return candidates[0]


def playback_cache_paths(streamer, filename):
    slug = _playback_cache_slug(streamer)
    name = os.path.basename(str(filename or ""))
    if not slug or not name or name != str(filename or ""):
        return None, None
    cache_dir = os.path.join(playback_cache_root(), slug)
    final_path = os.path.join(cache_dir, name)
    return final_path, f"{final_path}.part"


def playback_pin_path(streamer, filename):
    final_path, _part = playback_cache_paths(streamer, filename)
    if not final_path:
        return None
    return f"{final_path}.pin"


def pin_playback_cache(streamer, filename, days=None):
    """Pin a VOD cache entry against eviction until now+days."""
    path = playback_pin_path(streamer, filename)
    if not path:
        return False
    try:
        days = playback_pin_newest_days() if days is None else float(days)
    except (TypeError, ValueError):
        days = playback_pin_newest_days()
    if days <= 0:
        return False
    until = time.time() + (days * 86400.0)
    try:
        existing = playback_pin_until(streamer, filename)
        if existing > until:
            return True
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(str(until))
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def playback_pin_until(streamer, filename):
    path = playback_pin_path(streamer, filename)
    if not path or not os.path.isfile(path):
        return 0.0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            until = float((handle.read() or "").strip() or 0)
    except (OSError, TypeError, ValueError):
        return 0.0
    if until <= time.time():
        try:
            os.remove(path)
        except OSError:
            pass
        return 0.0
    return until


def playback_cache_is_pinned(streamer, filename):
    return playback_pin_until(streamer, filename) > time.time()


def _final_path_from_cache_file(path):
    """Map any cache artifact path back to the complete ``.mp4`` final path."""
    try:
        root = os.path.realpath(playback_cache_root())
        real = os.path.realpath(path)
        if not real.startswith(root + os.sep):
            return None
        rel = os.path.relpath(real, root)
    except OSError:
        return None
    parts = rel.split(os.sep)
    if not parts or parts[0] in {".", "..", ".quota"}:
        return None
    if len(parts) >= 3 and parts[1].endswith(".chunks"):
        return os.path.join(root, parts[0], parts[1][: -len(".chunks")])
    name = parts[1] if len(parts) > 1 else ""
    for suffix in (".pin", ".part", ".tmp"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if not name:
        return None
    return os.path.join(root, parts[0], name)


def _cache_path_is_pinned(path):
    final = _final_path_from_cache_file(path)
    if not final:
        # Never evict quota circuit files via LRU.
        try:
            if os.path.realpath(path).startswith(
                os.path.realpath(os.path.join(playback_cache_root(), ".quota")) + os.sep
            ):
                return True
        except OSError:
            pass
        return False
    pin_path = f"{final}.pin"
    if not os.path.isfile(pin_path):
        return False
    try:
        with open(pin_path, "r", encoding="utf-8") as handle:
            until = float((handle.read() or "").strip() or 0)
    except (OSError, TypeError, ValueError):
        return False
    if until <= time.time():
        try:
            os.remove(pin_path)
        except OSError:
            pass
        return False
    return True


def complete_playback_cache(streamer, filename, expected_size=None):
    """Return a finished local cache path if it matches the Drive object size."""
    name = os.path.basename(str(filename or ""))
    if not name or name != str(filename or ""):
        return None
    cache_root = playback_cache_root()
    candidates = _cache_slug_candidates(streamer) or [_playback_cache_slug(streamer)]
    for slug in candidates:
        final_path = os.path.join(cache_root, slug, name)
        if not os.path.isfile(final_path):
            continue
        try:
            size = os.path.getsize(final_path)
        except OSError:
            continue
        if size <= 0:
            continue
        if expected_size:
            try:
                expected = int(expected_size)
            except (TypeError, ValueError):
                expected = 0
            if expected > 0 and size != expected:
                continue
        try:
            os.utime(final_path, None)
        except OSError:
            pass
        return final_path
    return None


def playback_cache_progress(streamer, filename):
    """Return (readable_path, downloaded_bytes) for a complete or growing cache."""
    final_path, part_path = playback_cache_paths(streamer, filename)
    if final_path and os.path.isfile(final_path):
        try:
            size = os.path.getsize(final_path)
        except OSError:
            size = 0
        if size > 0:
            return final_path, size
    if part_path and os.path.isfile(part_path):
        try:
            size = os.path.getsize(part_path)
        except OSError:
            size = 0
        if size > 0:
            return part_path, size
    return None, 0


def _chunk_dir(streamer, filename):
    name = os.path.basename(str(filename or ""))
    if not name or name != str(filename or ""):
        return None
    cache_root = playback_cache_root()
    candidates = _cache_slug_candidates(streamer) or [_playback_cache_slug(streamer)]
    for slug in candidates:
        cand_dir = os.path.join(cache_root, slug, f"{name}.chunks")
        if os.path.isdir(cand_dir):
            return cand_dir
    primary = _playback_cache_slug(streamer)
    return os.path.join(cache_root, primary, f"{name}.chunks") if primary else None


def _chunk_span(index, total_size, chunk=None):
    size = int(chunk or playback_chunk_bytes())
    start = int(index) * size
    last = max(0, int(total_size) - 1)
    if start > last:
        return None
    end = min(start + size - 1, last)
    return start, end, end - start + 1


def _chunk_paths(streamer, filename, index):
    folder = _chunk_dir(streamer, filename)
    if not folder:
        return None, None
    name = f"{int(index):08d}"
    return os.path.join(folder, name), os.path.join(folder, f"{name}.part")


_CHUNK_GUARD = threading.Lock()
_CHUNK_INFLIGHT = {}
_CHUNK_LAST_ERROR = {}
_CHUNK_ERROR_COOLDOWN_SEC = 20.0
_DRIVE_QUOTA_BLOCKED_UNTIL = {}
# Shared circuit for background hydrate / blocked live Ranges. Google's
# downloadQuotaExceeded often lasts far longer than a few minutes — keep the
# local circuit long so we stop probing Drive (each 403 resets the ban).
_DRIVE_QUOTA_COOLDOWN_SEC = 10800.0
_HYDRATE_FAIL_COOLDOWN_SEC = 6 * 3600.0
_DRIVE_FETCH_SEMA = None
_DRIVE_FETCH_SEMA_LOCK = threading.Lock()


def playback_quota_cooldown_sec():
    try:
        return float(
            max(
                300,
                min(
                    86400,
                    int(getattr(config, "GDRIVE_PLAYBACK_QUOTA_COOLDOWN_SEC", _DRIVE_QUOTA_COOLDOWN_SEC)),
                ),
            )
        )
    except (TypeError, ValueError):
        return float(_DRIVE_QUOTA_COOLDOWN_SEC)


def _quota_state_path(file_id):
    file_id = str(file_id or "").strip()
    if not file_id:
        return None
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in file_id)[:48]
    return os.path.join(playback_cache_root(), ".quota", f"{safe}.until")


def _read_quota_until(file_id):
    path = _quota_state_path(file_id)
    if not path or not os.path.isfile(path):
        return 0.0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return float((handle.read() or "").strip() or 0)
    except (OSError, TypeError, ValueError):
        return 0.0


def _write_quota_until(file_id, until):
    path = _quota_state_path(file_id)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(str(float(until)))
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Quota-Circuit write failed %s: %s", path, exc)


def _clear_quota_file(file_id):
    path = _quota_state_path(file_id)
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        return


def _quota_blocked(file_id):
    """Shared across gunicorn workers via ``.vodcache/.quota/*.until`` files."""
    file_id = str(file_id or "").strip()
    if not file_id:
        return False
    now = time.time()
    until = float(_DRIVE_QUOTA_BLOCKED_UNTIL.get(file_id) or 0)
    try:
        from core import redis_client
        redis_until = redis_client.get_state(f"gdrive_quota:{file_id}")
        if redis_until is not None:
            until = max(until, float(redis_until))
            _DRIVE_QUOTA_BLOCKED_UNTIL[file_id] = until
    except Exception:
        pass
    file_until = _read_quota_until(file_id)
    if file_until > until:
        until = file_until
        _DRIVE_QUOTA_BLOCKED_UNTIL[file_id] = until
    if until <= 0:
        return False
    if now >= until:
        _DRIVE_QUOTA_BLOCKED_UNTIL.pop(file_id, None)
        _clear_quota_file(file_id)
        return False
    return True


def playback_quota_blocked(file_id):
    """True while the per-file Drive download-quota circuit is open."""
    return _quota_blocked(file_id)


def playback_quota_retry_after(file_id):
    file_id = str(file_id or "").strip()
    if not file_id:
        return 0
    _quota_blocked(file_id)  # refresh/clear expired
    until = float(_DRIVE_QUOTA_BLOCKED_UNTIL.get(file_id) or 0)
    file_until = _read_quota_until(file_id)
    until = max(until, file_until)
    if until <= 0:
        return 0
    return max(0, int(until - time.time()))


def _note_quota_error(file_id):
    file_id = str(file_id or "").strip()
    if not file_id:
        return
    cooldown = playback_quota_cooldown_sec()
    until = time.time() + cooldown
    _DRIVE_QUOTA_BLOCKED_UNTIL[file_id] = until
    try:
        from core import redis_client
        redis_client.set_state(f"gdrive_quota:{file_id}", until, ex=int(cooldown) + 60)
    except Exception:
        pass
    _write_quota_until(file_id, until)
    _note_hydrate_failure(file_id)
    logger.warning(
        "Drive download-quota circuit open for %s (%.0fs, shared)",
        file_id[:16],
        cooldown,
    )


def _hydrate_fail_path(file_id):
    file_id = str(file_id or "").strip()
    if not file_id:
        return None
    safe = hashlib.sha1(file_id.encode("utf-8")).hexdigest()[:24]
    return os.path.join(playback_cache_root(), ".quota", f"{safe}.hydrate_fail")


def _note_hydrate_failure(file_id, seconds=None):
    """Skip full-file hydrate for this Drive object for several hours."""
    path = _hydrate_fail_path(file_id)
    if not path:
        return
    try:
        cooldown = float(seconds if seconds is not None else _HYDRATE_FAIL_COOLDOWN_SEC)
    except (TypeError, ValueError):
        cooldown = _HYDRATE_FAIL_COOLDOWN_SEC
    until = time.time() + max(60.0, cooldown)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(str(until))
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Hydrate-Backoff write failed %s: %s", path, exc)
        return


def hydrate_recently_failed(file_id):
    path = _hydrate_fail_path(file_id)
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            until = float((handle.read() or "").strip() or 0)
    except (OSError, TypeError, ValueError):
        return False
    if until <= time.time():
        try:
            os.remove(path)
        except OSError:
            pass
        return False
    return True


def any_drive_quota_circuits_open():
    """True while any shared per-file download-quota circuit is still open."""
    root = os.path.join(playback_cache_root(), ".quota")
    if not os.path.isdir(root):
        return bool(_DRIVE_QUOTA_BLOCKED_UNTIL)
    now = time.time()
    try:
        for name in os.listdir(root):
            if not name.endswith(".until"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    until = float((handle.read() or "").strip() or 0)
            except (OSError, TypeError, ValueError):
                continue
            if until > now:
                return True
    except OSError:
        pass
    return any(float(until) > now for until in _DRIVE_QUOTA_BLOCKED_UNTIL.values())


def _vod_is_actively_watched(streamer, filename):
    try:
        from core import vod_watch_slots
    except Exception:
        return False
    try:
        if vod_watch_slots.is_vod_watched(streamer, filename):
            return True
        # Archive slug vs Drive index slug (beatek_ vs beatek).
        alt = drive_index_slug(streamer)
        if alt and alt != streamer and vod_watch_slots.is_vod_watched(alt, filename):
            return True
        local = local_archive_slug(streamer)
        if local and local not in {streamer, alt} and vod_watch_slots.is_vod_watched(local, filename):
            return True
    except Exception:
        return False
    return False


def playback_hydrate_allowed(file_id=None, streamer=None, filename=None, *, resume_ok=False):
    """Background full-file hydrate must not compete with live Ranges."""
    if playback_hydrate_enabled() or resume_ok and playback_hydrate_resume_enabled():
        pass
    else:
        return False
    if any_drive_quota_circuits_open():
        return False
    if file_id and (_quota_blocked(file_id) or hydrate_recently_failed(file_id)):
        return False
    if streamer and filename and _vod_is_actively_watched(streamer, filename):
        return False
    return True


def has_partial_playback_chunks(streamer, filename):
    """True when aligned .chunks already exist (safe to resume without a cold start)."""
    final_path, _part = playback_cache_paths(streamer, filename)
    if not final_path:
        return False
    chunks_dir = f"{final_path}.chunks"
    if not os.path.isdir(chunks_dir):
        return False
    try:
        for name in os.listdir(chunks_dir):
            if name.endswith((".lock", ".part", ".tmp")):
                continue
            path = os.path.join(chunks_dir, name)
            try:
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def _chunk_key(streamer, filename, index):
    return f"{_playback_cache_slug(streamer)}/{os.path.basename(str(filename or ''))}/{int(index)}"


def _drive_fetch_sema():
    global _DRIVE_FETCH_SEMA
    with _DRIVE_FETCH_SEMA_LOCK:
        if _DRIVE_FETCH_SEMA is None:
            # Keep concurrency low — parallel chunk fetches burn Drive download quota.
            # Interactive live Ranges must not compete with many hydrate jobs.
            _DRIVE_FETCH_SEMA = threading.BoundedSemaphore(1)
        return _DRIVE_FETCH_SEMA


def _chunk_ready(path, expected_len):
    try:
        return bool(path and os.path.isfile(path) and os.path.getsize(path) == int(expected_len))
    except OSError:
        return False


def _chunk_available_bytes(streamer, filename, index, expected_len):
    final_path, part_path = _chunk_paths(streamer, filename, index)
    if _chunk_ready(final_path, expected_len):
        return int(expected_len), final_path
    if part_path and os.path.isfile(part_path):
        try:
            return os.path.getsize(part_path), part_path
        except OSError:
            return 0, part_path
    return 0, final_path


def _chunk_error_is_fresh(key):
    with _CHUNK_GUARD:
        error = _CHUNK_LAST_ERROR.get(key)
    if not error:
        return False
    ts, _exc = error
    try:
        return (time.time() - float(ts)) < _CHUNK_ERROR_COOLDOWN_SEC
    except (TypeError, ValueError):
        return False


def contiguous_cached_end(streamer, filename, start, expected_size):
    """Last byte index reachable contiguously from ``start`` via local cache."""
    chunk = playback_chunk_bytes()
    try:
        start = max(0, int(start))
        expected = int(expected_size or 0)
    except (TypeError, ValueError):
        return -1
    if expected <= 0 or start >= expected:
        return -1
    pos = start
    while pos < expected:
        index = pos // chunk
        span = _chunk_span(index, expected, chunk)
        if not span:
            break
        cstart, _cend, clen = span
        available, _path = _chunk_available_bytes(streamer, filename, index, clen)
        if available <= 0:
            break
        have_until = cstart + available - 1
        if have_until < pos:
            break
        pos = have_until + 1
    return pos - 1


def ingest_playback_bytes(streamer, filename, start, data, expected_size):
    """Write live Range bytes into aligned chunk parts (contiguous extend only).

    Lets successful interactive seeks warm the cache without a parallel hydrate
    storm, so jumping back to the same region works even after Drive quota trips.
    """
    if not playback_cache_enabled() or not data:
        return 0
    try:
        expected = int(expected_size or 0)
        pos = max(0, int(start))
    except (TypeError, ValueError):
        return 0
    if expected <= 0 or pos >= expected:
        return 0
    chunk = playback_chunk_bytes()
    written_total = 0
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        index = pos // chunk
        span = _chunk_span(index, expected, chunk)
        if not span:
            break
        cstart, _cend, clen = span
        local_off = pos - cstart
        take = min(len(view) - offset, clen - local_off)
        if take <= 0:
            break
        final_path, part_path = _chunk_paths(streamer, filename, index)
        if not final_path or not part_path:
            break
        if _chunk_ready(final_path, clen):
            offset += take
            pos += take
            continue
        try:
            os.makedirs(os.path.dirname(part_path), exist_ok=True)
            have = 0
            if os.path.isfile(part_path):
                have = os.path.getsize(part_path)
            # Only contiguous writes — never create holes inside a chunk.
            if local_off > have:
                break
            if local_off < have:
                skip = min(have - local_off, take)
                offset += skip
                pos += skip
                continue
            with open(part_path, "ab" if have else "wb") as handle:
                piece = view[offset : offset + take]
                handle.write(piece.tobytes() if hasattr(piece, "tobytes") else bytes(piece))
            written_total += take
            have_after = have + take
            if have_after >= clen:
                os.replace(part_path, final_path)
        except OSError:
            break
        offset += take
        pos += take
    return written_total


def _iter_drive_gap_bytes(file_id, start, end):
    """Yield a closed Drive byte range for gap-fill during playback."""
    start = max(0, int(start))
    end = max(start, int(end))
    upstream = open_media(file_id, range_header=f"bytes={start}-{end}")
    try:
        for piece in upstream.iter_content(64 * 1024):
            if piece:
                yield piece
    finally:
        upstream.close()


def _download_chunk_file_once(file_id, part_path, final_path, start, end, expected_len):
    os.makedirs(os.path.dirname(part_path), exist_ok=True)
    evict_playback_cache(expected_len)
    written = 0
    if os.path.isfile(part_path):
        try:
            written = os.path.getsize(part_path)
        except OSError:
            written = 0
        if written >= expected_len:
            os.replace(part_path, final_path)
            return final_path
    mode = "ab" if written else "wb"
    with _drive_fetch_sema():
        fetch_start = start + written
        if fetch_start > end:
            os.replace(part_path, final_path)
            return final_path
        upstream = open_media(file_id, range_header=f"bytes={fetch_start}-{end}")
        try:
            with open(part_path, mode) as handle:
                for piece in upstream.iter_content(512 * 1024):
                    if not piece:
                        continue
                    need = expected_len - written
                    if need <= 0:
                        break
                    if len(piece) > need:
                        piece = piece[:need]
                    handle.write(piece)
                    written += len(piece)
                    handle.flush()
        finally:
            upstream.close()
    if written != expected_len:
        raise DriveError(f"Chunk unvollstaendig ({written}/{expected_len})")
    os.replace(part_path, final_path)
    return final_path


def _download_chunk_file(file_id, part_path, final_path, start, end, expected_len):
    if _quota_blocked(file_id):
        raise DriveError(
            "Drive-Playback fehlgeschlagen (403): The download quota for this file has been exceeded."
        )
    last_exc = None
    # Quota burns faster with long retry loops; keep attempts short.
    for attempt in range(2):
        try:
            return _download_chunk_file_once(
                file_id, part_path, final_path, start, end, expected_len
            )
        except DriveError as exc:
            last_exc = exc
            if is_download_quota_error(exc):
                _note_quota_error(file_id)
                raise
            retryable = "429" in str(exc) or "503" in str(exc)
            if not retryable or attempt >= 1:
                raise
            delay = min(12.0, (2 ** attempt) + random.uniform(0.4, 1.8))
            logger.info(
                "Playback-Chunk Retry %s/%s nach %.1fs (%s)",
                attempt + 1,
                2,
                delay,
                exc,
            )
            time.sleep(delay)
    raise last_exc or DriveError("Playback-Chunk fehlgeschlagen")


def ensure_playback_chunk(streamer, filename, file_id, index, expected_size):
    """Download one aligned cache chunk. Concurrent callers share the same job."""
    span = _chunk_span(index, expected_size)
    if not span:
        return None
    start, end, length = span
    final_path, part_path = _chunk_paths(streamer, filename, index)
    if not final_path:
        return None
    if _chunk_ready(final_path, length):
        return final_path
    key = _chunk_key(streamer, filename, index)
    with _CHUNK_GUARD:
        inflight = _CHUNK_INFLIGHT.get(key)
        if inflight is None:
            inflight = {"event": threading.Event(), "error": None, "path": None}
            _CHUNK_INFLIGHT[key] = inflight
            _CHUNK_LAST_ERROR.pop(key, None)
            owner = True
        else:
            owner = False
    if not owner:
        inflight["event"].wait(timeout=120)
        if inflight["error"]:
            raise inflight["error"]
        return inflight["path"] or (final_path if _chunk_ready(final_path, length) else None)
    try:
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        lock_path = f"{final_path}.lock"
        with open(lock_path, "w") as lock:
            if fcntl:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if _chunk_ready(final_path, length):
                inflight["path"] = final_path
                return final_path
            path = _download_chunk_file(file_id, part_path, final_path, start, end, length)
        inflight["path"] = path
        assemble_playback_cache(streamer, filename, expected_size)
        return path
    except Exception as exc:
        inflight["error"] = exc
        with _CHUNK_GUARD:
            _CHUNK_LAST_ERROR[key] = (time.time(), exc)
        logger.warning("Playback-Chunk %s/%s #%s fehlgeschlagen: %s", streamer, filename, index, exc)
        raise
    finally:
        inflight["event"].set()
        with _CHUNK_GUARD:
            if _CHUNK_INFLIGHT.get(key) is inflight:
                _CHUNK_INFLIGHT.pop(key, None)


def wait_playback_range(streamer, filename, start, end, expected_size, timeout=20):
    """True when local chunks already contain ``start..end`` (growing .part counts)."""
    chunk = playback_chunk_bytes()
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 0.0
    start = max(0, int(start))
    end = min(int(end), int(expected_size) - 1)
    if end < start:
        return False
    deadline = time.time() if timeout <= 0 else time.time() + timeout
    while True:
        pos = start
        ok = True
        blocked_index = None
        while pos <= end:
            index = pos // chunk
            span = _chunk_span(index, expected_size, chunk)
            if not span:
                return False
            cstart, _cend, clen = span
            available, _path = _chunk_available_bytes(streamer, filename, index, clen)
            have_until = cstart + available - 1
            if available <= 0 or have_until < pos:
                ok = False
                blocked_index = index
                break
            pos = have_until + 1
        if ok:
            return True
        if timeout <= 0 or time.time() > deadline:
            return False
        if blocked_index is not None:
            key = _chunk_key(streamer, filename, blocked_index)
            with _CHUNK_GUARD:
                inflight = key in _CHUNK_INFLIGHT
            # Fail fast only while the error is fresh; after cooldown allow
            # callers to retry / fall back to a live Drive range.
            if (not inflight) and _chunk_error_is_fresh(key):
                return False
        time.sleep(0.05)


def playback_range_cached(streamer, filename, start, end, expected_size):
    return wait_playback_range(streamer, filename, start, end, expected_size, timeout=0)


def enqueue_chunk_prefetch(streamer, filename, file_id, index, expected_size):
    if _quota_blocked(file_id):
        return False
    span = _chunk_span(index, expected_size)
    if not span:
        return False
    _start, _end, length = span
    final_path, _part = _chunk_paths(streamer, filename, index)
    if _chunk_ready(final_path, length):
        return False

    def _run():
        try:
            ensure_playback_chunk(streamer, filename, file_id, index, expected_size)
        except Exception:
            return

    threading.Thread(
        target=_run,
        name=f"vodchunk-{index}-{_playback_cache_slug(streamer)}",
        daemon=True,
    ).start()
    return True


def prefetch_playhead_chunk(streamer, filename, file_id, start, expected_size):
    """Warm only the aligned chunk under the playhead — never a readahead storm.

    Interactive ``/media`` Ranges used to call ``prefetch_playback_window`` which
    enqueued playhead + 128 MiB readahead on every scrub. A few seeks burned the
    consumer Drive download quota; the next live 4 MiB Range then 403'd and the
    player hung after ~10s of buffered playback.
    """
    if not playback_cache_enabled() or not enabled() or not file_id:
        return False
    if _quota_blocked(file_id):
        return False
    try:
        expected = int(expected_size or 0)
        start = max(0, int(start))
    except (TypeError, ValueError):
        return False
    if expected <= 0 or start >= expected:
        return False
    index = start // playback_chunk_bytes()
    return enqueue_chunk_prefetch(streamer, filename, file_id, index, expected)


def prefetch_next_chunk(streamer, filename, file_id, after_byte, expected_size):
    """Warm the next 2 chunks immediately after a served range (sliding window).

    Safe for cache-hit / live ingest paths. Keeps playhead smooth with zero buffer stalls.
    """
    if not playback_cache_enabled() or not enabled() or not file_id:
        return False
    if _quota_blocked(file_id):
        return False
    try:
        expected = int(expected_size or 0)
        after = max(0, int(after_byte))
    except (TypeError, ValueError):
        return False
    if expected <= 0:
        return False
    chunk_sz = playback_chunk_bytes()
    next_start = after + 1
    if next_start >= expected:
        return False
    first_idx = next_start // chunk_sz
    second_idx = (next_start + chunk_sz) // chunk_sz
    ok1 = enqueue_chunk_prefetch(streamer, filename, file_id, first_idx, expected)
    if (next_start + chunk_sz) < expected:
        enqueue_chunk_prefetch(streamer, filename, file_id, second_idx, expected)
    return ok1


def prefetch_playback_window(streamer, filename, file_id, start, end, expected_size, *, readahead=True):
    """Enqueue playhead (+ optional readahead) chunks without blocking HTTP."""
    if not playback_cache_enabled() or not enabled() or not file_id:
        return False
    try:
        expected = int(expected_size or 0)
        start = max(0, int(start))
        end = min(int(end), expected - 1)
    except (TypeError, ValueError):
        return False
    if expected <= 0 or end < start:
        return False
    if _quota_blocked(file_id):
        return False
    if not readahead:
        return prefetch_playhead_chunk(streamer, filename, file_id, start, expected)
    chunk = playback_chunk_bytes()
    ahead_end = min(expected - 1, end + playback_readahead_bytes())
    first = start // chunk
    extra_last = ahead_end // chunk
    # Cap interactive-adjacent prefetch to at most 2 chunks total.
    extra_last = min(extra_last, first + 1)
    for index in range(first, extra_last + 1):
        enqueue_chunk_prefetch(streamer, filename, file_id, index, expected)
    return True


def ensure_playback_window(streamer, filename, file_id, start, end, expected_size, timeout=2.5):
    """Start aligned chunk downloads for the playhead plus readahead.

    Returns True once the first requested byte is local. Interactive ``/media``
    must not block long here — cold seeks should fall through to a live Drive
    Range instead of waiting on a 64 MiB hydrate (that leaves the player paused
    at the scrub time with no buffered bytes).
    """
    if not prefetch_playback_window(streamer, filename, file_id, start, end, expected_size):
        return False
    try:
        expected = int(expected_size or 0)
        start = max(0, int(start))
    except (TypeError, ValueError):
        return False
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 2.5
    # Fail fast: a few seconds max, then callers proxy Drive directly.
    timeout = max(0.0, min(timeout, 4.0))
    return wait_playback_range(streamer, filename, start, start, expected, timeout=timeout)


def iter_cached_range(
    streamer,
    filename,
    start,
    end,
    expected_size,
    stall_timeout=45,
    file_id=None,
):
    """Yield local bytes for ``start..end`` from aligned cache chunks.

    When a hole cannot be filled in time and ``file_id`` is set, stream that
    gap directly from Drive so the HTTP body is not truncated mid-range
    (which freezes HTML5 players despite earlier buffered media).
    """
    chunk = playback_chunk_bytes()
    pos = max(0, int(start))
    last = min(int(end), int(expected_size) - 1)
    file_id = str(file_id or "").strip() or None
    while pos <= last:
        wait_budget = stall_timeout
        if file_id and _quota_blocked(file_id):
            wait_budget = min(float(stall_timeout), 1.5)
        elif file_id:
            wait_budget = min(float(stall_timeout), 8.0)
        if not wait_playback_range(
            streamer, filename, pos, pos, expected_size, timeout=wait_budget
        ):
            index = pos // chunk
            span = _chunk_span(index, expected_size, chunk)
            if not span:
                raise DriveError("Playback-Chunk fehlt")
            _cstart, cend, _clen = span
            gap_end = min(last, cend)
            if file_id and not _quota_blocked(file_id):
                if not _chunk_error_is_fresh(_chunk_key(streamer, filename, index)):
                    try:
                        ensure_playback_chunk(
                            streamer, filename, file_id, index, expected_size
                        )
                    except DriveError:
                        pass
                    if wait_playback_range(
                        streamer, filename, pos, pos, expected_size, timeout=2
                    ):
                        continue
                try:
                    logger.warning(
                        "Playback-Gap Drive-Fallback %s/%s bytes %s-%s",
                        streamer,
                        filename,
                        pos,
                        gap_end,
                    )
                    yield from _iter_drive_gap_bytes(file_id, pos, gap_end)
                    pos = gap_end + 1
                    continue
                except DriveError as exc:
                    if is_download_quota_error(exc):
                        _note_quota_error(file_id)
                    raise
            raise DriveError("Playback-Chunk fehlt")
        index = pos // chunk
        span = _chunk_span(index, expected_size, chunk)
        if not span:
            raise DriveError("Playback-Chunk fehlt")
        cstart, _cend, clen = span
        available, path = _chunk_available_bytes(streamer, filename, index, clen)
        if not path or available <= 0:
            raise DriveError("Playback-Chunk fehlt")
        offset = pos - cstart
        can_read = min(last, cstart + available - 1) - pos + 1
        if can_read <= 0:
            raise DriveError("Playback-Chunk unvollstaendig")
        with open(path, "rb") as handle:
            handle.seek(offset)
            remaining = can_read
            while remaining > 0:
                piece = handle.read(min(512 * 1024, remaining))
                if not piece:
                    raise DriveError("Playback-Chunk EOF")
                remaining -= len(piece)
                yield piece
        pos += can_read


def assemble_playback_cache(streamer, filename, expected_size):
    """If every chunk is present, stitch them into the complete cache file."""
    final_path, _part = playback_cache_paths(streamer, filename)
    if not final_path:
        return None
    existing = complete_playback_cache(streamer, filename, expected_size=expected_size)
    if existing:
        return existing
    chunk = playback_chunk_bytes()
    try:
        expected = int(expected_size or 0)
    except (TypeError, ValueError):
        return None
    if expected <= 0:
        return None
    last_index = (expected - 1) // chunk
    paths = []
    for index in range(last_index + 1):
        span = _chunk_span(index, expected, chunk)
        if not span:
            return None
        _start, _end, length = span
        path, _part_chunk = _chunk_paths(streamer, filename, index)
        if not _chunk_ready(path, length):
            return None
        paths.append(path)
    tmp_path = f"{final_path}.assemble"
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    try:
        with open(tmp_path, "wb") as out:
            for path in paths:
                with open(path, "rb") as handle:
                    while True:
                        piece = handle.read(1024 * 1024)
                        if not piece:
                            break
                        out.write(piece)
            out.flush()
            os.fsync(out.fileno())
        if os.path.getsize(tmp_path) != expected:
            os.remove(tmp_path)
            return None
        os.replace(tmp_path, final_path)
        os.utime(final_path, None)
        return final_path
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return None


def parse_closed_bytes_range(range_header):
    """Parse a closed ``bytes=start-end`` header into inclusive ints."""
    raw = str(range_header or "").strip()
    unit, _, spec = raw.partition("=")
    if unit.strip().lower() != "bytes" or not spec or "," in spec:
        return None
    start_s, sep, end_s = spec.partition("-")
    if not sep or not start_s or not end_s:
        return None
    try:
        start = int(start_s)
        end = int(end_s)
    except ValueError:
        return None
    if start < 0 or end < start:
        return None
    return start, end


_HYDRATE_GUARD = threading.Lock()
_HYDRATE_INFLIGHT = set()
_HYDRATE_SEMA = None
_HYDRATE_SEMA_LOCK = threading.Lock()


def _hydrate_sema():
    global _HYDRATE_SEMA
    with _HYDRATE_SEMA_LOCK:
        if _HYDRATE_SEMA is None:
            _HYDRATE_SEMA = threading.BoundedSemaphore(playback_cache_jobs())
        return _HYDRATE_SEMA


def _iter_cache_files(root):
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {".", ".."}]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            yield path, stat.st_size, stat.st_mtime


def evict_playback_cache(needed_bytes=0):
    """Drop oldest unpinned cache files until there is room for ``needed_bytes``."""
    root = playback_cache_root()
    budget = playback_cache_max_bytes()
    needed = max(0, int(needed_bytes or 0))
    entries = list(_iter_cache_files(root))
    used = sum(size for _path, size, _mtime in entries)
    target = max(0, budget - needed)
    if used <= target:
        return
    for path, size, _mtime in sorted(entries, key=lambda item: item[2]):
        if used <= target:
            break
        if path.endswith(".pin"):
            continue
        if _cache_path_is_pinned(path):
            continue
        try:
            os.remove(path)
            used -= size
        except OSError:
            continue
        sidecar = path[:-5] if path.endswith(".part") else f"{path}.part"
        if sidecar != path:
            try:
                os.remove(sidecar)
            except OSError:
                pass


def ensure_vod_playback_cache(
    streamer,
    filename,
    file_id=None,
    expected_size=None,
    *,
    pin_days=None,
    hydrate=True,
):
    """Pin a VOD for the newest-window and optionally enqueue a full hydrate."""
    if not playback_cache_enabled() or not enabled():
        return {"ok": False, "reason": "disabled"}
    streamer = str(streamer or "").strip()
    filename = os.path.basename(str(filename or "").strip())
    if not streamer or not filename:
        return {"ok": False, "reason": "bad-args"}
    row = None
    if not file_id or not expected_size:
        row = database.get_drive_file(streamer, filename) or database.get_drive_file(
            drive_index_slug(streamer), filename
        )
    file_id = str(file_id or (row or {}).get("file_id") or "").strip()
    if not expected_size:
        try:
            expected_size = int((row or {}).get("size") or 0) or None
        except (TypeError, ValueError):
            expected_size = None
    if not file_id:
        return {"ok": False, "reason": "no-file-id"}
    pin_playback_cache(streamer, filename, days=pin_days)
    if complete_playback_cache(streamer, filename, expected_size=expected_size):
        return {"ok": True, "ready": True, "queued": False}
    if not hydrate:
        return {"ok": True, "ready": False, "queued": False}
    if not playback_hydrate_allowed(file_id, streamer, filename):
        return {"ok": True, "ready": False, "queued": False, "reason": "hydrate-paused"}
    queued = enqueue_playback_hydrate(streamer, filename, file_id, expected_size)
    return {"ok": True, "ready": False, "queued": bool(queued)}


def list_recent_drive_vods(days=None):
    """Playable Drive VODs newer than the pin window (plus the absolute newest)."""
    try:
        days = playback_pin_newest_days() if days is None else float(days)
    except (TypeError, ValueError):
        days = playback_pin_newest_days()
    cutoff = time.time() - max(0.0, days) * 86400.0
    rows = []
    try:
        for row in database.list_drive_files() or []:
            name = str(row.get("filename") or "")
            mime = str(row.get("mime_type") or "")
            if not is_playable_video(name, mime):
                continue
            rows.append(row)
    except Exception:
        return []
    if not rows:
        return []
    recent = [row for row in rows if float(row.get("modified_at") or 0) >= cutoff]
    newest = max(rows, key=lambda row: float(row.get("modified_at") or 0))
    if newest not in recent:
        recent.append(newest)
    recent.sort(key=lambda row: float(row.get("modified_at") or 0), reverse=True)
    return recent


def maintain_recent_playback_cache(*, hydrate_limit=1):
    """Pin newest VODs for N days and hydrate a few of them in the background.

    Safe to call from the worker after Drive sync. Keeps interactive /media free
    of hydrate storms — only one (or few) full-file fills at a time.
    Prefer seeding from a still-local archive file over Drive download.
    """
    if not playback_cache_enabled() or not enabled() or not configured():
        return {"pinned": 0, "ready": 0, "queued": 0, "seeded": 0}
    try:
        hydrate_limit = max(0, min(3, int(hydrate_limit)))
    except (TypeError, ValueError):
        hydrate_limit = 1
    # Never burn Drive download quota while any circuit is open — that freezes
    # the VOD the user is currently watching.
    full_hydrate = playback_hydrate_enabled() and not any_drive_quota_circuits_open()
    resume_hydrate = (
        (not full_hydrate)
        and playback_hydrate_resume_enabled()
        and not any_drive_quota_circuits_open()
    )
    if not full_hydrate and not resume_hydrate:
        hydrate_limit = 0
    recent = list_recent_drive_vods()
    pinned = 0
    ready = 0
    queued = 0
    seeded = 0
    resumed = 0
    for row in recent:
        streamer = str(row.get("streamer") or "").strip()
        filename = str(row.get("filename") or "").strip()
        file_id = str(row.get("file_id") or "").strip()
        if not streamer or not filename or not file_id:
            continue
        try:
            expected = int(row.get("size") or 0) or None
        except (TypeError, ValueError):
            expected = None
        if pin_playback_cache(streamer, filename):
            pinned += 1
        if complete_playback_cache(streamer, filename, expected_size=expected):
            ready += 1
            continue
        # Prefer local archive copy → cache seed (no Drive download quota).
        local_slug = local_archive_slug(streamer) or streamer
        local_path = os.path.join(config.BASE_DIR, local_slug, os.path.basename(filename))
        if os.path.isfile(local_path):
            try:
                if seed_playback_cache_from_local(local_slug, filename, local_path):
                    seeded += 1
                    ready += 1
                    continue
            except Exception as exc:
                logger.warning("Playback-Cache Seed (maintain) %s/%s: %s", streamer, filename, exc)
        if queued >= hydrate_limit:
            continue
        resume_only = resume_hydrate and not full_hydrate
        if resume_only and not has_partial_playback_chunks(streamer, filename):
            continue
        if not playback_hydrate_allowed(
            file_id, streamer, filename, resume_ok=resume_only
        ):
            continue
        if enqueue_playback_hydrate(
            streamer, filename, file_id, expected, resume_ok=resume_only
        ):
            queued += 1
            if resume_only:
                resumed += 1
            logger.info(
                "Playback-Cache Hydrate geplant (%s, neueste %sd) %s/%s",
                "resume" if resume_only else "full",
                playback_pin_newest_days(),
                streamer,
                filename,
            )
    return {
        "pinned": pinned,
        "ready": ready,
        "queued": queued,
        "seeded": seeded,
        "resumed": resumed,
        "recent": len(recent),
        "days": playback_pin_newest_days(),
        "hydrate_paused": hydrate_limit <= 0,
        "hydrate_mode": (
            "full" if full_hydrate else ("resume" if resume_hydrate else "off")
        ),
    }


def _hydrate_playback_file(streamer, filename, file_id, expected_size):
    try:
        expected = int(expected_size or 0)
    except (TypeError, ValueError):
        expected = 0
    if expected <= 0 or not file_id:
        return
    pin_playback_cache(streamer, filename)
    if assemble_playback_cache(streamer, filename, expected):
        return
    chunk = playback_chunk_bytes()
    last_index = (expected - 1) // chunk
    try:
        evict_playback_cache(expected)
        for index in range(last_index + 1):
            if _quota_blocked(file_id) or hydrate_recently_failed(file_id):
                logger.warning(
                    "Playback-Cache gestoppt (Quota) %s/%s at chunk #%s",
                    streamer,
                    filename,
                    index,
                )
                _note_hydrate_failure(file_id)
                return
            ensure_playback_chunk(streamer, filename, file_id, index, expected)
        assembled = assemble_playback_cache(streamer, filename, expected)
        if assembled:
            pin_playback_cache(streamer, filename)
            logger.info("Playback-Cache bereit %s/%s (%s bytes)", streamer, filename, expected)
    except Exception as exc:
        if is_download_quota_error(exc):
            _note_quota_error(file_id)
        else:
            _note_hydrate_failure(file_id, seconds=1800)
        logger.warning("Playback-Cache fehlgeschlagen %s/%s: %s", streamer, filename, exc)


def enqueue_playback_hydrate(streamer, filename, file_id, expected_size=None, *, resume_ok=False):
    """Download a Drive VOD into the local playback cache (one job per file)."""
    if not playback_cache_enabled() or not enabled():
        return False
    if not str(file_id or "").strip():
        return False
    if not playback_hydrate_allowed(file_id, streamer, filename, resume_ok=resume_ok):
        return False
    final_path, _part_path = playback_cache_paths(streamer, filename)
    if not final_path:
        return False
    pin_playback_cache(streamer, filename)
    if complete_playback_cache(streamer, filename, expected_size=expected_size):
        return False
    key = f"{_playback_cache_slug(streamer)}/{os.path.basename(str(filename or ''))}"
    with _HYDRATE_GUARD:
        if key in _HYDRATE_INFLIGHT:
            return False
        _HYDRATE_INFLIGHT.add(key)

    def _run():
        sema = _hydrate_sema()
        acquired = sema.acquire(timeout=30)
        try:
            if acquired:
                if not playback_hydrate_allowed(
                    file_id, streamer, filename, resume_ok=resume_ok
                ):
                    logger.info(
                        "Playback-Cache Hydrate abgebrochen (Quota/Watch) %s/%s",
                        streamer,
                        filename,
                    )
                    return
                _hydrate_playback_file(streamer, filename, file_id, expected_size)
        finally:
            if acquired:
                sema.release()
            with _HYDRATE_GUARD:
                _HYDRATE_INFLIGHT.discard(key)

    threading.Thread(target=_run, name=f"vodcache-{key[:40]}", daemon=True).start()
    return True


def is_download_quota_error(exc):
    text = str(exc or "").lower()
    return "download quota" in text or "downloadquotaexceeded" in text


def bound_media_range_header(range_header, *, chunk_size=None, total_size=None):
    """Force a closed byte range for Drive alt=media requests.

    Consumer Google Drive often returns 403 downloadQuotaExceeded for full-file
    downloads and open-ended ranges (``bytes=0-``) after repeated access.
    Bounded ranges continue to work and HTML5 players will request the next chunk.
    """
    chunk = int(chunk_size or playback_chunk_bytes())
    chunk = max(256 * 1024, chunk)
    total = None
    if total_size is not None:
        try:
            total = max(0, int(total_size))
        except (TypeError, ValueError):
            total = None

    def _clamp_end(start, end):
        if total is not None and total > 0:
            last = total - 1
            start = min(max(0, start), last)
            end = min(max(start, end), last)
        else:
            start = max(0, start)
            end = max(start, end)
        return start, end

    raw = str(range_header or "").strip()
    if not raw:
        start, end = _clamp_end(0, chunk - 1)
        return f"bytes={start}-{end}"

    # Only the first range is used (browsers send a single byte range for media).
    unit, _, spec = raw.partition("=")
    if unit.strip().lower() != "bytes" or not spec:
        start, end = _clamp_end(0, chunk - 1)
        return f"bytes={start}-{end}"
    first = spec.split(",", 1)[0].strip()
    if first.startswith("-"):
        # suffix length: bytes=-N → last N bytes when size known, else first chunk
        try:
            suffix = max(1, int(first[1:]))
        except ValueError:
            start, end = _clamp_end(0, chunk - 1)
            return f"bytes={start}-{end}"
        if total is not None and total > 0:
            start, end = _clamp_end(max(0, total - suffix), total - 1)
            return f"bytes={start}-{end}"
        start, end = _clamp_end(0, min(suffix, chunk) - 1)
        return f"bytes={start}-{end}"

    start_s, _, end_s = first.partition("-")
    try:
        start = int(start_s)
    except ValueError:
        start, end = _clamp_end(0, chunk - 1)
        return f"bytes={start}-{end}"
    if end_s == "":
        start, end = _clamp_end(start, start + chunk - 1)
        return f"bytes={start}-{end}"
    try:
        end = int(end_s)
    except ValueError:
        start, end = _clamp_end(start, start + chunk - 1)
        return f"bytes={start}-{end}"
    if end < start:
        start, end = _clamp_end(start, start + chunk - 1)
        return f"bytes={start}-{end}"
    # Cap extremely large closed ranges that are effectively full-file downloads.
    if (end - start + 1) > max(chunk * 4, 32 * 1024 * 1024):
        start, end = _clamp_end(start, start + chunk - 1)
        return f"bytes={start}-{end}"
    start, end = _clamp_end(start, end)
    return f"bytes={start}-{end}"


def open_media(file_id, range_header=None, *, bound_range=False, total_size=None, chunk_size=None, ignore_quota_circuit=False):
    file_id = str(file_id or "").strip()
    if not file_id:
        raise DriveError("Keine Datei-ID.")
    # Interactive live Ranges may probe Google even while the hydrate circuit is
    # open — a hard local block made seeking fail for minutes after one 403.
    if (not ignore_quota_circuit) and _quota_blocked(file_id):
        raise DriveError(
            "Drive-Playback fehlgeschlagen (403): The download quota for this file has been exceeded."
        )
    headers = {}
    if bound_range or range_header:
        if bound_range:
            range_header = bound_media_range_header(
                range_header,
                chunk_size=chunk_size,
                total_size=total_size,
            )
        if range_header:
            headers["Range"] = range_header
    last_exc = None
    for attempt in range(3):
        response = get_session(force=attempt > 0).get(
            f"{DRIVE_API}/files/{file_id}",
            params=_drive_params({"alt": "media"}),
            headers=headers,
            stream=True,
            timeout=(15, 300),
        )
        if response.status_code == 401 and attempt == 0:
            response.close()
            continue
        if response.status_code < 400:
            return response
        detail = ""
        try:
            detail = str(((response.json().get("error") or {}).get("message")) or "")
        except Exception:
            detail = (response.text or "")[:120]
        response.close()
        last_exc = DriveError(
            f"Drive-Playback fehlgeschlagen ({response.status_code}): {detail or 'Drive-Fehler'}"
        )
        if is_download_quota_error(last_exc):
            _note_quota_error(file_id)
            raise last_exc
        retryable = response.status_code in (429, 503) or "rate limit" in detail.lower()
        if not retryable or attempt >= 2:
            raise last_exc
        delay = min(20.0, (2 ** attempt) + random.uniform(0.4, 1.8))
        time.sleep(max(0.5, delay))
    raise last_exc or DriveError("Drive-Playback fehlgeschlagen")


def iter_drive_media(file_id, *, total_size=None, chunk_size=None):
    """Yield an entire Drive file via successive closed ranges (download-safe)."""
    file_id = str(file_id or "").strip()
    if not file_id:
        raise DriveError("Keine Datei-ID.")
    chunk = int(chunk_size or playback_chunk_bytes())
    size = total_size
    if size is None:
        meta = get_file_metadata(file_id)
        try:
            size = int(meta.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
    if size <= 0:
        raise DriveError("Drive-Datei ohne Groesse.")
    start = 0
    while start < size:
        end = min(start + chunk, size) - 1
        upstream = open_media(file_id, range_header=f"bytes={start}-{end}")
        try:
            for piece in upstream.iter_content(64 * 1024):
                if piece:
                    yield piece
        finally:
            upstream.close()
        start = end + 1


def bearer_access_token():
    """Return a fresh Drive access token for external HTTP clients (FFmpeg/ffprobe)."""
    creds = _load_credentials()
    token = str(getattr(creds, "token", "") or "").strip()
    valid = bool(getattr(creds, "valid", False))
    if not token or not valid:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
        token = str(getattr(creds, "token", "") or "").strip()
    if not token:
        raise DriveError("Kein Drive-Access-Token.")
    return token


def media_http_source(file_id):
    """Return (url, headers) for range-seekable Drive media suitable for FFmpeg.

    Prefer this over a full download when clipping cloud-only VODs: FFmpeg can
    seek with HTTP Range against the Drive alt=media endpoint.

    Note: Consumer Google Drive often 403s open-ended / full-file ranges.
    For clipping prefer :func:`clip_http_source`, which proxies through a
    localhost server that always uses closed bounded ranges.
    """
    from urllib.parse import urlencode

    file_id = str(file_id or "").strip()
    if not file_id:
        raise DriveError("Keine Datei-ID.")
    get_session()  # validate credentials / warm AuthorizedSession
    token = bearer_access_token()
    url = f"{DRIVE_API}/files/{file_id}?{urlencode(_drive_params({'alt': 'media'}))}"
    return url, {"Authorization": f"Bearer {token}"}


_CLIP_PROXY_LOCK = threading.Lock()
_CLIP_PROXY_PORT = None
_CLIP_PROXY_TOKEN = None
_CLIP_PROXY_SERVER = None
_CLIP_PROXY_THREAD = None


def _clip_proxy_chunk_bytes():
    """Chunk size for FFmpeg→localhost→Drive stitching (keep Drive ranges closed)."""
    try:
        # Slightly larger than playback default is fine; still force closed ranges.
        return max(1024 * 1024, min(playback_chunk_bytes(), 32 * 1024 * 1024))
    except Exception:
        return 8 * 1024 * 1024


def _iter_drive_byte_range(file_id, start, end, total_size):
    """Yield inclusive ``start..end`` via successive closed Drive Range requests."""
    chunk = _clip_proxy_chunk_bytes()
    pos = max(0, int(start))
    last = min(int(end), int(total_size) - 1)
    while pos <= last:
        piece_end = min(pos + chunk - 1, last)
        upstream = open_media(file_id, range_header=f"bytes={pos}-{piece_end}")
        try:
            for data in upstream.iter_content(64 * 1024):
                if data:
                    yield data
        finally:
            upstream.close()
        pos = piece_end + 1


def ensure_clip_media_proxy():
    """Start a localhost Range proxy for FFmpeg Drive clipping; return listen port."""
    global _CLIP_PROXY_PORT, _CLIP_PROXY_TOKEN, _CLIP_PROXY_SERVER, _CLIP_PROXY_THREAD
    with _CLIP_PROXY_LOCK:
        if _CLIP_PROXY_PORT and _CLIP_PROXY_SERVER is not None:
            return _CLIP_PROXY_PORT

        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        token = secrets.token_urlsafe(24)

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def _unauthorized(self):
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _bad_request(self):
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _not_found(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _check_auth(self):
                got = (self.headers.get("X-Clip-Proxy-Token") or "").strip()
                return got and secrets.compare_digest(got, token)

            def _file_id(self):
                path = (self.path or "").split("?", 1)[0]
                prefix = "/drive/"
                if not path.startswith(prefix):
                    return ""
                return path[len(prefix) :].strip().strip("/")

            def _total_size(self, file_id):
                meta = get_file_metadata(file_id)
                try:
                    return max(0, int(meta.get("size") or 0))
                except (TypeError, ValueError):
                    return 0

            def do_HEAD(self):
                self._serve(head_only=True)

            def do_GET(self):
                self._serve(head_only=False)

            def _serve(self, *, head_only):
                if not self._check_auth():
                    self._unauthorized()
                    return
                file_id = self._file_id()
                if not file_id or "/" in file_id or ".." in file_id:
                    self._bad_request()
                    return
                try:
                    total = self._total_size(file_id)
                except DriveError:
                    self._not_found()
                    return
                if total <= 0:
                    self._not_found()
                    return

                # Bound like browser playback so Drive never sees open-ended ranges.
                # Still answer with the *requested* span by stitching closed chunks —
                # FFmpeg needs coherent Content-Range to seek the moov / media.
                raw_range = self.headers.get("Range")
                if raw_range:
                    closed = parse_closed_bytes_range(
                        bound_media_range_header(
                            raw_range,
                            chunk_size=_clip_proxy_chunk_bytes(),
                            total_size=total,
                        )
                    )
                    # If the client asked for a closed range within cap, honor it fully
                    # by stitching; if open-ended, expand to a single bounded window
                    # with correct total so FFmpeg re-requests the next window.
                    unit, _, spec = str(raw_range).partition("=")
                    first = spec.split(",", 1)[0].strip() if unit.strip().lower() == "bytes" else ""
                    if first and "-" in first and not first.startswith("-"):
                        start_s, _, end_s = first.partition("-")
                        try:
                            req_start = int(start_s)
                        except ValueError:
                            req_start = 0
                        if end_s == "":
                            # Open-ended: serve one bounded window; FFmpeg continues.
                            start, end = closed or (0, min(_clip_proxy_chunk_bytes() - 1, total - 1))
                        else:
                            try:
                                req_end = int(end_s)
                            except ValueError:
                                req_end = req_start + _clip_proxy_chunk_bytes() - 1
                            # Cap extremely large closed asks (effectively full download).
                            max_span = max(_clip_proxy_chunk_bytes() * 4, 64 * 1024 * 1024)
                            if (req_end - req_start + 1) > max_span:
                                start = max(0, min(req_start, total - 1))
                                end = min(total - 1, start + _clip_proxy_chunk_bytes() - 1)
                            else:
                                start = max(0, min(req_start, total - 1))
                                end = max(start, min(req_end, total - 1))
                    else:
                        start, end = closed or (0, min(_clip_proxy_chunk_bytes() - 1, total - 1))
                else:
                    start, end = 0, min(_clip_proxy_chunk_bytes() - 1, total - 1)

                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if head_only:
                    return
                try:
                    for piece in _iter_drive_byte_range(file_id, start, end, total):
                        self.wfile.write(piece)
                except (BrokenPipeError, ConnectionResetError, DriveError, OSError):
                    return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, name="clip-drive-proxy", daemon=True)
        thread.start()
        _CLIP_PROXY_SERVER = server
        _CLIP_PROXY_THREAD = thread
        _CLIP_PROXY_PORT = port
        _CLIP_PROXY_TOKEN = token
        logger.info("Clip Drive proxy listening on 127.0.0.1:%s", port)
        return port


def clip_http_source(file_id):
    """Return (url, headers) for FFmpeg via localhost proxy with bounded Drive ranges.

    Google Drive consumer accounts reject open-ended ``alt=media`` fetches with
    403 downloadQuotaExceeded. Playback already bounds ranges; FFmpeg must go
    through the same policy via this proxy.
    """
    file_id = str(file_id or "").strip()
    if not file_id:
        raise DriveError("Keine Datei-ID.")
    get_session()  # validate credentials / warm AuthorizedSession
    port = ensure_clip_media_proxy()
    token = _CLIP_PROXY_TOKEN
    if not port or not token:
        raise DriveError("Clip-Proxy nicht verfügbar.")
    url = f"http://127.0.0.1:{port}/drive/{file_id}"
    return url, {"X-Clip-Proxy-Token": token}


def reset_clip_media_proxy_for_tests():
    """Stop the clip proxy (unit tests only)."""
    global _CLIP_PROXY_PORT, _CLIP_PROXY_TOKEN, _CLIP_PROXY_SERVER, _CLIP_PROXY_THREAD
    with _CLIP_PROXY_LOCK:
        server = _CLIP_PROXY_SERVER
        _CLIP_PROXY_SERVER = None
        _CLIP_PROXY_THREAD = None
        _CLIP_PROXY_PORT = None
        _CLIP_PROXY_TOKEN = None
    if server is not None:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass


def _sync_single_streamer_folder(item):
    """Process a single streamer folder concurrently: list media, chats, and metadata."""
    title = str(item.get("name") or "").strip()
    slug = normalize_folder_slug(title)
    folder_id = str(item.get("id") or "").strip()
    if not slug or not folder_id:
        return None, [], 0, 0, 0, 0
    app_props = item.get("appProperties") or {}
    vis = app_props.get(VISIBILITY_KEY)
    folder_row = {
        "streamer": slug,
        "folder_id": folder_id,
        "title": title,
        "visibility": vis,
    }
    f_rows = []
    disc = 0
    ast_idx = 0
    cht_idx = 0
    cht_dl = 0
    chat_folder_id = ""
    vods_folder_id = ""
    for media in _list_children(folder_id, folders_only=True):
        name = str(media.get("name") or "")
        child_id = str(media.get("id") or "").strip()
        if not child_id:
            continue
        if name == chat_subdir_name():
            chat_folder_id = child_id
        elif is_vods_folder_name(name):
            vods_folder_id = child_id
    seen_file_ids = set()
    for media in iter_drive_tree_files(folder_id):
        mime = str(media.get("mimeType") or "")
        name = str(media.get("name") or "")
        file_id = str(media.get("id") or "").strip()
        if not file_id or file_id in seen_file_ids:
            continue
        if database.is_deleted_media(streamer=slug, filename=name, file_id=file_id):
            seen_file_ids.add(file_id)
            try:
                delete_remote_file(file_id)
                logger.info("Drive-Sync entfernt geloeschtes Medium: %s/%s", slug, name)
            except Exception as exc:
                logger.warning("Drive-Sync konnte geloeschtes Medium nicht entfernen %s/%s: %s", slug, name, exc)
            continue
        if is_playable_video(name, mime):
            record = _file_record(slug, media, source="remote")
            if not record.get("file_id") or not record.get("filename"):
                continue
            seen_file_ids.add(file_id)
            f_rows.append(record)
            disc += 1
            _cache_thumbnail(slug, record["filename"], record.get("thumbnail_url"))
            continue
        if is_drive_asset(name, mime):
            record = _file_record(slug, media, source="remote")
            if not record.get("file_id") or not record.get("filename"):
                continue
            seen_file_ids.add(file_id)
            f_rows.append(record)
            ast_idx += 1
            if is_preview_image(name, mime):
                _cache_thumbnail(slug, record["filename"], record.get("thumbnail_url"))
    if not vods_folder_id:
        try:
            vods_folder_id = ensure_vods_folder(slug)
        except DriveError as exc:
            logger.warning("vods-Ordner %s: %s", slug, exc)
    if vods_folder_id:
        try:
            migrate_root_vods_into_subdir(slug, limit=60)
        except Exception as exc:
            logger.warning("VOD-Migrate %s: %s", slug, exc)
    if chat_folder_id:
        for chat_item in _list_children(chat_folder_id):
            if not is_chat_log(chat_item.get("name"), chat_item.get("mimeType")):
                continue
            record = _file_record(slug, chat_item, source="remote")
            record["mime_type"] = record.get("mime_type") or CHAT_MIME
            if not record.get("file_id") or not record.get("filename"):
                continue
            if database.is_deleted_media(streamer=slug, filename=record["filename"], file_id=record.get("file_id")):
                try:
                    delete_remote_file(record["file_id"])
                except Exception as exc:
                    logger.warning("Drive-Sync konnte geloeschten Chat nicht entfernen %s/%s: %s", slug, record["filename"], exc)
                continue
            f_rows.append(record)
            cht_idx += 1
            if not bool(getattr(config, "GDRIVE_DELETE_LOCAL_AFTER_UPLOAD", False)):
                local_slug = local_archive_slug(slug)
                dest = os.path.join(config.BASE_DIR, local_slug, chat_subdir_name(), record["filename"])
                if not os.path.isfile(dest) or os.path.getsize(dest) <= 0:
                    try:
                        if download_drive_file(record["file_id"], dest):
                            cht_dl += 1
                    except Exception as exc:
                        logger.warning("Chat-Sync Download %s/%s: %s", local_slug, record["filename"], exc)
    return folder_row, f_rows, disc, ast_idx, cht_idx, cht_dl

def sync_once(upload_limit=None):
    """Pull Drive folders/files into SQLite, then upload missing local VODs (+ chats)."""
    if not enabled():
        return {"ok": False, "error": "Drive ist deaktiviert."}
    if not configured():
        error = "Drive-Credentials fehlen."
        save_status({**load_status(), "connected": False, "last_error": error, "auth_mode": auth_mode()})
        return {"ok": False, "error": error}

    started = time.time()
    uploaded = 0
    skipped = 0
    discovered = 0
    chats_indexed = 0
    chats_downloaded = 0
    assets_indexed = 0
    try:
        get_session()
        root_id = ensure_root_folder()
        remote_folders = _list_children(root_id, folders_only=True)
        folder_rows = []
        file_rows = []
        known_slugs = set()
        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_crawl_workers = max(1, min(6, len(remote_folders) or 1))
        with ThreadPoolExecutor(max_workers=max_crawl_workers) as executor:
            future_to_folder = {executor.submit(_sync_single_streamer_folder, item): item for item in remote_folders}
            for future in as_completed(future_to_folder):
                try:
                    f_row, f_list, disc, ast_idx, cht_idx, cht_dl = future.result()
                    if f_row:
                        folder_rows.append(f_row)
                        known_slugs.add(f_row["streamer"])
                    file_rows.extend(f_list)
                    discovered += disc
                    assets_indexed += ast_idx
                    chats_indexed += cht_idx
                    chats_downloaded += cht_dl
                except Exception as exc:
                    item_name = str(future_to_folder[future].get("name") or "")
                    logger.warning("Paralleler Drive-Sync Fehler fuer %s: %s", item_name, exc)

        for streamer, filename, path, size in iter_local_vods():
            index_slug = drive_index_slug(streamer)
            if database.is_deleted_media(streamer=streamer, filename=filename) or database.is_deleted_media(
                streamer=index_slug, filename=filename
            ):
                # Keep Tube clean: do not re-upload admin-deleted locals.
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        logger.info("Lokales geloeschtes VOD entfernt: %s/%s", streamer, filename)
                except OSError as exc:
                    logger.warning("Lokales geloeschtes VOD bleibt: %s/%s (%s)", streamer, filename, exc)
                continue
            if index_slug not in known_slugs:
                folder = ensure_streamer_folder(streamer)
                folder_rows.append({
                    "streamer": index_slug,
                    "folder_id": folder["folder_id"],
                    "title": folder.get("title") or drive_folder_title(streamer),
                    "visibility": folder.get("visibility"),
                })
                known_slugs.add(index_slug)

        database.replace_drive_index(folder_rows, file_rows)

        limit = int(upload_limit if upload_limit is not None else getattr(config, "GDRIVE_UPLOAD_BATCH", 4) or 4)
        for streamer, filename, path, size in iter_local_vods():
            index_slug = drive_index_slug(streamer)
            if database.is_deleted_media(streamer=streamer, filename=filename) or database.is_deleted_media(
                streamer=index_slug, filename=filename
            ):
                continue
            existing = database.get_drive_file(index_slug, filename) or database.get_drive_file(streamer, filename)
            chat_name = chat_filename_for_vod(filename)
            chat_existing = None
            if chat_name:
                chat_existing = (
                    database.get_drive_file(index_slug, chat_name)
                    or database.get_drive_file(streamer, chat_name)
                )
            chat_path = local_chat_path(streamer, filename)
            chat_needs = bool(
                chat_path
                and os.path.isfile(chat_path)
                and os.path.getsize(chat_path) > 0
                and (
                    not chat_existing
                    or int(chat_existing.get("size") or 0) != os.path.getsize(chat_path)
                )
            )
            vod_ok = existing and int(existing.get("size") or 0) == size
            if vod_ok and not chat_needs:
                skipped += 1
                deleted, delete_msg = maybe_delete_local_after_upload(streamer, filename)
                if deleted:
                    logger.info("Lokales VOD nach Drive-Index geloescht: %s/%s", streamer, filename)
                elif delete_msg and delete_msg not in ("disabled", "nicht lokal"):
                    logger.debug("Lokales VOD behalten %s/%s: %s", streamer, filename, delete_msg)
                continue
            if uploaded >= limit:
                break
            if not vod_ok:
                ok, msg = upload_local_vod(streamer, filename)
                if ok:
                    uploaded += 1
                    if msg != "bereits vorhanden":
                        logger.info("Drive-Upload %s/%s (%s)", index_slug, filename, msg)
                else:
                    logger.warning("Drive-Upload uebersprungen %s/%s: %s", streamer, filename, msg)
            elif chat_needs:
                ok, msg = upload_local_chat(streamer, filename)
                if ok:
                    uploaded += 1
                    logger.info("Drive-Chat %s/%s (%s)", index_slug, chat_name, msg)
                    maybe_delete_local_after_upload(streamer, filename)
                else:
                    logger.warning("Drive-Chat uebersprungen %s/%s: %s", streamer, chat_name, msg)

        bust_media_cache()
        try:
            duration_queued = enqueue_missing_durations(
                limit=int(getattr(config, "GDRIVE_DURATION_BACKFILL_BATCH", 40) or 40)
            )
        except Exception as exc:
            logger.warning("Dauer-Backfill enqueue: %s", exc)
            duration_queued = 0
        try:
            thumb_queued = enqueue_missing_thumbnails(
                limit=int(getattr(config, "GDRIVE_THUMB_BACKFILL_BATCH", 30) or 30)
            )
        except Exception as exc:
            logger.warning("Thumbnail-Backfill enqueue: %s", exc)
            thumb_queued = 0
        status = {
            "connected": True,
            "auth_mode": auth_mode(),
            "root_id": root_id,
            "root_name": getattr(config, "GDRIVE_ROOT_NAME", "nachtsilo.tube"),
            "last_sync_at": time.time(),
            "last_error": "",
            "uploaded": uploaded,
            "skipped": skipped,
            "discovered": discovered,
            "chats_indexed": chats_indexed,
            "chats_downloaded": chats_downloaded,
            "assets_indexed": assets_indexed,
            "duration_queued": duration_queued,
            "thumb_queued": thumb_queued,
            "folders": len(folder_rows),
            "elapsed_sec": round(time.time() - started, 1),
        }
        save_status(status)
        return {"ok": True, **status}
    except Exception as exc:
        logger.error("Drive-Sync fehlgeschlagen: %s", exc)
        save_status({
            **load_status(),
            "connected": False,
            "auth_mode": auth_mode(),
            "last_error": str(exc)[:400],
            "last_sync_at": time.time(),
        })
        raise


def ensure_system_backup_folder():
    """Ensure ``System-Backups`` (configurable) exists under the Drive root."""
    label = str(getattr(config, "GDRIVE_SYSTEM_BACKUP_FOLDER", "System-Backups") or "System-Backups").strip()
    if not label:
        raise DriveError("System-Backup-Ordnername fehlt.")
    root_id = ensure_root_folder()
    found = _find_named_folder(label, parent_id=root_id)
    if found:
        return found
    return _create_folder(label, parent_id=root_id)


def upload_system_backup(path, retention_days=7):
    """Upload a local system backup archive and prune older Drive copies.

    Returns ``{ok, message, file_id, pruned}``.
    """
    if not enabled():
        return {"ok": False, "message": "Drive deaktiviert", "file_id": "", "pruned": 0}
    if not configured():
        return {"ok": False, "message": "Drive-Credentials fehlen", "file_id": "", "pruned": 0}
    if not bool(getattr(config, "GDRIVE_SYSTEM_BACKUP", True)):
        return {"ok": False, "message": "System-Backup-Upload deaktiviert", "file_id": "", "pruned": 0}
    path = os.path.realpath(str(path or ""))
    name = os.path.basename(path)
    if not path or not name.startswith("streamrecorder_") or not name.endswith(".tar.gz"):
        return {"ok": False, "message": "Ungueltige Backup-Datei", "file_id": "", "pruned": 0}
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        return {"ok": False, "message": "Backup-Datei fehlt/leer", "file_id": "", "pruned": 0}

    folder_id = ensure_system_backup_folder()
    existing = None
    for item in _list_children(folder_id):
        if str(item.get("name") or "") == name and str(item.get("mimeType") or "") != FOLDER_MIME:
            existing = {
                "file_id": str(item.get("id") or "").strip(),
                "size": int(item.get("size") or 0),
            }
            break
    ok, msg, payload = upload_bytes_or_file(
        folder_id,
        name,
        path,
        mime="application/gzip",
        existing=existing,
        force=False,
    )
    file_id = str((payload or {}).get("id") or (existing or {}).get("file_id") or "").strip()
    pruned = 0
    try:
        pruned = prune_system_backups(retention_days=retention_days, folder_id=folder_id)
    except Exception as exc:
        logger.warning("System-Backup Drive-Prune fehlgeschlagen: %s", exc)
    return {
        "ok": bool(ok),
        "message": msg,
        "file_id": file_id,
        "pruned": int(pruned or 0),
    }


def prune_system_backups(retention_days=7, folder_id=None):
    """Delete Drive system backups older than retention_days (by modifiedTime)."""
    try:
        keep_days = max(1, int(retention_days or 7))
    except (TypeError, ValueError):
        keep_days = 7
    if folder_id is None:
        folder_id = ensure_system_backup_folder()
    cutoff = time.time() - (keep_days * 86400)
    deleted = 0
    for item in _list_children(folder_id):
        name = str(item.get("name") or "")
        if not name.startswith("streamrecorder_") or not name.endswith(".tar.gz"):
            continue
        file_id = str(item.get("id") or "").strip()
        if not file_id:
            continue
        modified = str(item.get("modifiedTime") or "").strip()
        ts = 0.0
        if modified:
            try:
                # 2026-09-04T04:00:00.000Z
                from datetime import datetime

                ts = datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0.0
        if ts and ts >= cutoff:
            continue
        if delete_remote_file(file_id):
            deleted += 1
            logger.info("Altes System-Backup auf Drive geloescht: %s", name)
    return deleted


def enqueue_upload(streamer, filename, force=False):
    if not enabled() or not configured():
        return False
    slug = normalize_folder_slug(streamer) or str(streamer or "").strip()
    name = os.path.basename(str(filename or ""))
    if not slug or not name:
        return False
    if database.has_active_task("drive_upload", streamer=slug, filename=name):
        return False
    database.add_task("drive_upload", {
        "streamer": slug,
        "filename": name,
        "force": bool(force),
    })
    return True


def enqueue_sync(force=False):
    if not enabled():
        return False
    if not force and database.has_active_task("drive_sync"):
        return False
    database.add_task("drive_sync", {"force": bool(force)})
    return True


def public_status():
    data = load_status()
    last_sync = data.get("last_sync_at")
    last_sync_str = ""
    if last_sync:
        try:
            last_sync_str = time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(float(last_sync)))
        except (TypeError, ValueError, OSError):
            last_sync_str = ""
    folders = []
    try:
        folders = database.list_drive_folders()
    except Exception:
        folders = []
    return {
        "enabled": enabled(),
        "configured": configured(),
        "connected": bool(data.get("connected")) and configured(),
        "auth_mode": data.get("auth_mode") or auth_mode(),
        "root_name": data.get("root_name") or getattr(config, "GDRIVE_ROOT_NAME", "nachtsilo.tube"),
        "last_sync_at": last_sync,
        "last_sync_str": last_sync_str,
        "last_error": str(data.get("last_error") or "")[:240],
        "uploaded": int(data.get("uploaded") or 0),
        "discovered": int(data.get("discovered") or 0),
        "folders": folders,
        "folder_count": len(folders),
        "session_error": _SESSION_ERROR,
    }
