"""Dropbox provider (OAuth 2.0 refresh tokens + upload sessions)."""

from __future__ import annotations

import json
import os
import posixpath
import time
import urllib.parse
from typing import Iterable, Optional

from core.storage.base import (
    ProgressCallback,
    ProviderField,
    RemoteObject,
    StorageConfigError,
    StorageError,
    StorageProvider,
    file_size,
    iter_response_range,
    normalize_remote_path,
)
from core.storage import http

API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"
AUTH_URL = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
SESSION_CHUNK = 32 * 1024 * 1024
SIMPLE_UPLOAD_MAX = 140 * 1024 * 1024


class DropboxProvider(StorageProvider):
    type_id = "dropbox"
    label = "Dropbox"
    description = "Dropbox mit OAuth 2.0, Refresh Token und großen Upload-Sessions."
    oauth_supported = True
    oauth_label = "Mit Dropbox verbinden"
    docs_url = "https://www.dropbox.com/developers/documentation/http/documentation"
    config_fields = (
        ProviderField("app_key", "App Key", placeholder="Dropbox App Console → App Key"),
        ProviderField("app_secret", "App Secret", type="password", secret=True),
        ProviderField("refresh_token", "Refresh Token", type="password", secret=True,
                      help="Wird beim Verbinden automatisch gespeichert."),
        ProviderField("access_token", "Access Token (Alternative)", type="password", secret=True,
                      help="Nur nötig, wenn kein Refresh Token verwendet wird."),
        ProviderField("base_path", "Unterordner", default="StreamVault", placeholder="StreamVault"),
    )

    def __init__(self, config: dict):
        super().__init__(config)
        self._access = ""
        self._expires_at = 0.0

    @classmethod
    def validate_config(cls, config: dict, *, require_secrets: bool = True) -> list[str]:
        cfg = config or {}
        if str(cfg.get("access_token") or "").strip():
            return []
        errors = []
        for key, label in (("app_key", "App Key"), ("app_secret", "App Secret"), ("refresh_token", "Refresh Token")):
            if require_secrets and not str(cfg.get(key) or "").strip():
                errors.append(f"{label} fehlt.")
        return errors

    @classmethod
    def oauth_ready(cls, config: dict) -> bool:
        return bool(str(config.get("app_key") or "").strip() and str(config.get("app_secret") or "").strip())

    @classmethod
    def oauth_authorize_url(cls, config: dict, redirect_uri: str, state: str) -> str:
        if not cls.oauth_ready(config):
            raise StorageConfigError("Dropbox App Key und App Secret fehlen.")
        params = {
            "client_id": str(config.get("app_key") or "").strip(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "token_access_type": "offline",
            "state": state,
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    @classmethod
    def oauth_exchange_code(cls, config: dict, code: str, redirect_uri: str) -> dict:
        response = http.request(
            "POST",
            TOKEN_URL,
            data={
                "code": str(code or "").strip(),
                "grant_type": "authorization_code",
                "client_id": str(config.get("app_key") or "").strip(),
                "client_secret": str(config.get("app_secret") or "").strip(),
                "redirect_uri": redirect_uri,
            },
            provider=cls.type_id,
        )
        data = http.json_response(response, "Dropbox-Token-Austausch", provider=cls.type_id)
        refresh = str(data.get("refresh_token") or "").strip()
        if not refresh:
            raise StorageError("Dropbox lieferte keinen Refresh Token.", provider=cls.type_id)
        return {"refresh_token": refresh}

    def _token(self, force: bool = False) -> str:
        access = str(self.config.get("access_token") or "").strip()
        if access and not str(self.config.get("refresh_token") or "").strip():
            return access
        now = time.time()
        if not force and self._access and now < self._expires_at - 60:
            return self._access
        refresh = str(self.config.get("refresh_token") or "").strip()
        key = str(self.config.get("app_key") or "").strip()
        secret = str(self.config.get("app_secret") or "").strip()
        if not refresh or not key or not secret:
            raise StorageConfigError("Dropbox ist nicht vollständig konfiguriert.")
        response = http.request(
            "POST",
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": key,
                "client_secret": secret,
            },
            provider=self.type_id,
        )
        data = http.json_response(response, "Dropbox-Token-Erneuerung", provider=self.type_id)
        self._access = str(data.get("access_token") or "").strip()
        if not self._access:
            raise StorageError("Dropbox lieferte kein Access Token.", provider=self.type_id)
        try:
            self._expires_at = now + int(data.get("expires_in") or 14400)
        except (TypeError, ValueError):
            self._expires_at = now + 14400
        return self._access

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if extra:
            headers.update(extra)
        return headers

    def _rpc(self, endpoint: str, payload: dict, action: str) -> dict:
        for attempt in range(2):
            response = http.request(
                "POST",
                f"{API}{endpoint}",
                headers=self._headers({"Content-Type": "application/json"}),
                data=http.dumps(payload),
                provider=self.type_id,
                timeout=(10, 60),
            )
            if response.status_code == 401 and attempt == 0:
                try:
                    response.close()
                except Exception:
                    pass
                self._token(force=True)
                continue
            return http.json_response(response, action, provider=self.type_id)
        raise StorageError(f"{action}: Authentifizierung fehlgeschlagen.", provider=self.type_id)

    def _content(
        self,
        endpoint: str,
        arg: dict,
        *,
        data=None,
        action: str,
        stream=False,
        headers=None,
    ):
        request_headers = self._headers(
            {
                # HTTP headers are latin-1/ASCII; non-ASCII filenames must be
                # JSON-escaped here (Dropbox decodes the escapes server-side).
                "Dropbox-API-Arg": json.dumps(
                    arg, ensure_ascii=True, separators=(",", ":")
                ),
                "Content-Type": "application/octet-stream",
            }
        )
        if headers:
            request_headers.update(headers)
        return http.request(
            "POST",
            f"{CONTENT}{endpoint}",
            headers=request_headers,
            data=data,
            provider=self.type_id,
            stream=stream,
            timeout=(10, 600),
            retries=1,
        )

    def _path(self, remote_path: str) -> str:
        return "/" + normalize_remote_path(remote_path)

    def _ensure_folder(self, folder_path: str) -> None:
        """Create a Dropbox folder and any missing ancestors.

        Dropbox ``/files/upload`` does not create parent folders.  Checking the
        immediate parent first keeps repeated uploads to an existing archive
        cheap while still creating a complete base_path on first use.
        """
        folder = str(folder_path or "").strip()
        if folder in ("", "/"):
            return
        try:
            data = self._rpc(
                "/files/get_metadata",
                {"path": folder},
                "Dropbox-Ordnerpruefung",
            )
        except StorageError as exc:
            if exc.status_code != 409:
                raise
            self._ensure_folder(posixpath.dirname(folder))
            try:
                self._rpc(
                    "/files/create_folder_v2",
                    {"path": folder, "autorename": False},
                    "Dropbox-Ordner anlegen",
                )
            except StorageError as create_exc:
                if create_exc.status_code != 409:
                    raise
                # Another worker may have created the folder concurrently.
                data = self._rpc(
                    "/files/get_metadata",
                    {"path": folder},
                    "Dropbox-Ordnerpruefung",
                )
            else:
                return
        if str(data.get(".tag") or "") != "folder":
            raise StorageError(
                f"Dropbox-Pfad ist kein Ordner: {folder}",
                provider=self.type_id,
            )

    def _ensure_parent_folders(self, remote_path: str) -> None:
        self._ensure_folder(posixpath.dirname(self._path(remote_path)))

    def test_connection(self) -> dict:
        data = self._rpc("/users/get_current_account", {}, "Dropbox-Verbindungstest")
        return {
            "ok": True,
            "account": (data.get("email") or data.get("display_name") or ""),
            "quota": self.quota(),
        }

    def quota(self) -> Optional[dict]:
        try:
            data = self._rpc("/users/get_space_usage", {}, "Dropbox-Quota")
            return {
                "usage": int(data.get("used") or 0),
                "limit": int((data.get("allocation") or {}).get("allocated") or 0),
            }
        except Exception:
            return None

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        size = file_size(local_path)
        path = self._path(remote_path)
        self._ensure_parent_folders(path)
        commit = {"path": path, "mode": {".tag": "overwrite"}, "autorename": False, "mute": False}
        if size <= SIMPLE_UPLOAD_MAX:
            with open(local_path, "rb") as handle:
                response = self._content(
                    "/files/upload",
                    {**commit, "strict_conflict": False},
                    data=handle,
                    action="Dropbox-Upload",
                )
            data = http.json_response(response, "Dropbox-Upload", provider=self.type_id)
            if progress:
                progress(size, size)
            return {"remote_id": str(data.get("id") or path), "size": int(data.get("size") or size),
                    "checksum": str(data.get("content_hash") or ""), "modified_at": str(data.get("server_modified") or "")}

        with open(local_path, "rb") as handle:
            first = handle.read(SESSION_CHUNK)
            response = self._content(
                "/files/upload_session/start",
                {"close": False},
                data=first,
                action="Dropbox-Upload-Session",
            )
            data = http.json_response(response, "Dropbox-Upload-Session", provider=self.type_id)
            session_id = str(data.get("session_id") or "")
            if not session_id:
                raise StorageError("Dropbox lieferte keine Upload-Session.", provider=self.type_id)
            offset = len(first)
            if progress:
                progress(offset, size)
            while offset < size:
                remaining = size - offset
                chunk = handle.read(min(SESSION_CHUNK, remaining))
                cursor = {"session_id": session_id, "offset": offset}
                if offset + len(chunk) >= size:
                    response = self._content(
                        "/files/upload_session/finish",
                        {"cursor": cursor, "commit": commit},
                        data=chunk,
                        action="Dropbox-Upload abschließen",
                    )
                    data = http.json_response(response, "Dropbox-Upload abschließen", provider=self.type_id)
                    if progress:
                        progress(size, size)
                    return {
                        "remote_id": str(data.get("id") or path),
                        "size": int(data.get("size") or size),
                        "checksum": str(data.get("content_hash") or ""),
                        "modified_at": str(data.get("server_modified") or ""),
                    }
                response = self._content(
                    "/files/upload_session/append_v2",
                    {"cursor": cursor, "close": False},
                    data=chunk,
                    action="Dropbox-Upload fortsetzen",
                )
                if response.status_code not in (200, 201):
                    http.raise_for_status(response, "Dropbox-Upload fortsetzen", provider=self.type_id)
                offset += len(chunk)
                if progress:
                    progress(offset, size)
        raise StorageError("Dropbox-Upload endete unerwartet.", provider=self.type_id)

    def _download(self, remote_path: str, *, range_header: str = "", stream: bool = True):
        return self._content(
            "/files/download",
            {"path": self._path(remote_path)},
            action="Dropbox-Download",
            stream=stream,
            headers={"Range": range_header} if range_header else None,
        )

    def download_file(
        self,
        remote_path: str,
        dest_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        response = self._download(remote_path)
        http.raise_for_status(response, "Dropbox-Download", provider=self.type_id)
        total = int(response.headers.get("Content-Length") or 0)
        tmp_path = f"{dest_path}.part"
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        done = 0
        try:
            with open(tmp_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, dest_path)
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
        return {"remote_id": self._path(remote_path), "size": done}

    def open_range(self, remote_path: str, start: int, end: int) -> Iterable[bytes]:
        start = max(0, int(start))
        end = max(start, int(end))
        response = self._download(remote_path, range_header=f"bytes={start}-{end}")
        http.raise_for_status(response, "Dropbox-Range-Stream", provider=self.type_id)
        try:
            yield from iter_response_range(response, start, end)
        finally:
            try:
                response.close()
            except Exception:
                pass

    def delete(self, remote_path: str) -> None:
        try:
            self._rpc("/files/delete_v2", {"path": self._path(remote_path)}, "Dropbox-Datei löschen")
        except StorageError as exc:
            if exc.status_code == 409:
                return
            raise

    def list(self, prefix: str = "") -> list[RemoteObject]:
        path = ""
        if str(prefix or "").strip():
            path = self._path(prefix)
        data = self._rpc(
            "/files/list_folder",
            {"path": path, "recursive": True, "include_deleted": False, "limit": 2000},
            "Dropbox-Dateiliste",
        )
        entries = list(data.get("entries") or [])
        cursor = data.get("cursor")
        while data.get("has_more") and cursor:
            data = self._rpc(
                "/files/list_folder/continue",
                {"cursor": cursor},
                "Dropbox-Dateiliste",
            )
            entries.extend(data.get("entries") or [])
            cursor = data.get("cursor")
        items = []
        for entry in entries:
            if entry.get(".tag") != "file":
                continue
            display = str(entry.get("path_display") or entry.get("path_lower") or "").lstrip("/")
            items.append(
                RemoteObject(
                    path=display,
                    remote_id=str(entry.get("id") or display),
                    size=int(entry.get("size") or 0),
                    modified_at=str(entry.get("server_modified") or ""),
                )
            )
        return items

    def exists(self, remote_path: str, expected_size: int = 0) -> bool:
        try:
            data = self._rpc(
                "/files/get_metadata",
                {"path": self._path(remote_path)},
                "Dropbox-Existenzprüfung",
            )
        except StorageError as exc:
            if exc.status_code == 409:
                return False
            raise
        expected = int(expected_size or 0)
        if expected <= 0:
            return True
        return int(data.get("size") or 0) == expected
