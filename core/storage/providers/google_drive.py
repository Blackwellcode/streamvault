"""Google Drive provider (OAuth 2.0 + resumable uploads, no SDK required)."""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import Iterable, Optional

from flask_babel import lazy_gettext

from core.storage.base import (
    ProgressCallback,
    ProviderField,
    RemoteObject,
    StorageConfigError,
    StorageError,
    StorageProvider,
    file_size,
    guess_mime_type,
    iter_response_range,
    normalize_remote_path,
    remote_name,
    remote_parent,
)
from core.storage import http

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_CHUNK = 16 * 1024 * 1024


class GoogleDriveProvider(StorageProvider):
    type_id = "google_drive"
    label = "Google Drive"
    description = lazy_gettext("Google Drive / Workspace Drive with OAuth 2.0 and resumable uploads.")
    oauth_supported = True
    oauth_label = lazy_gettext("Connect with Google")
    docs_url = "https://developers.google.com/drive/api/guides/about-sdk"
    config_fields = (
        ProviderField(
            "client_id",
            "OAuth Client ID",
            required=True,
            placeholder="1234567890-xxxx.apps.googleusercontent.com",
            help=lazy_gettext("Google Cloud Console → APIs & Services → Credentials → OAuth client (web application)."),
        ),
        ProviderField(
            "client_secret",
            "OAuth Client Secret",
            type="password",
            required=True,
            secret=True,
        ),
        ProviderField(
            "refresh_token",
            "Refresh Token",
            type="password",
            required=True,
            secret=True,
            help=lazy_gettext("Saved automatically when connecting. Alternatively, paste it manually."),
        ),
        ProviderField(
            "folder_id",
            lazy_gettext("Target folder ID (optional)"),
            placeholder="1AbCdEfGh...",
            help=lazy_gettext("Leave empty to create a 'StreamVault' folder automatically."),
        ),
        ProviderField(
            "base_path",
            lazy_gettext("Subfolder"),
            default="StreamVault",
            placeholder="StreamVault",
            help=lazy_gettext("Path below the target folder ID. Empty = directly in the target folder ID."),
        ),
        ProviderField(
            "scope",
            "OAuth Scope",
            default=DEFAULT_SCOPE,
            help=lazy_gettext("drive.file = only files created by StreamVault (recommended). "
                 "For existing folders, use https://www.googleapis.com/auth/drive if needed."),
        ),
    )

    def __init__(self, config: dict):
        super().__init__(config)
        self._access_token = ""
        self._token_expires_at = 0.0
        # Only folder lookups are cached. File metadata (especially size) must
        # be fetched fresh so exists()-verification never trusts stale values.
        self._folder_cache: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    @classmethod
    def oauth_ready(cls, config: dict) -> bool:
        return bool(str(config.get("client_id") or "").strip() and str(config.get("client_secret") or "").strip())

    @classmethod
    def oauth_authorize_url(cls, config: dict, redirect_uri: str, state: str) -> str:
        if not cls.oauth_ready(config):
            raise StorageConfigError("Google Client ID und Client Secret fehlen.")
        scope = str(config.get("scope") or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE
        params = {
            "client_id": str(config.get("client_id") or "").strip(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    @classmethod
    def oauth_exchange_code(cls, config: dict, code: str, redirect_uri: str) -> dict:
        payload = {
            "client_id": str(config.get("client_id") or "").strip(),
            "client_secret": str(config.get("client_secret") or "").strip(),
            "code": str(code or "").strip(),
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        response = http.request("POST", TOKEN_URL, data=payload, provider=cls.type_id)
        data = http.json_response(response, "Google-Token-Austausch", provider=cls.type_id)
        refresh = str(data.get("refresh_token") or "").strip()
        if not refresh:
            raise StorageError(
                "Google hat keinen Refresh Token geliefert. Bitte Zugriff in den "
                "Google-Kontoeinstellungen entfernen und erneut verbinden.",
                provider=cls.type_id,
            )
        return {"refresh_token": refresh, "scope": data.get("scope") or config.get("scope") or DEFAULT_SCOPE}

    # ------------------------------------------------------------------
    # Auth / HTTP
    # ------------------------------------------------------------------
    def _token(self, force: bool = False) -> str:
        now = time.time()
        if not force and self._access_token and now < self._token_expires_at - 60:
            return self._access_token
        refresh = str(self.config.get("refresh_token") or "").strip()
        client_id = str(self.config.get("client_id") or "").strip()
        client_secret = str(self.config.get("client_secret") or "").strip()
        if not refresh or not client_id or not client_secret:
            raise StorageConfigError(
                "Google Drive ist nicht vollständig konfiguriert (Client ID, Client Secret, Refresh Token)."
            )
        response = http.request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            provider=self.type_id,
        )
        data = http.json_response(response, "Google-Token-Erneuerung", provider=self.type_id)
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise StorageError("Google lieferte kein Access Token.", provider=self.type_id)
        self._access_token = token
        try:
            self._token_expires_at = now + int(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            self._token_expires_at = now + 3600
        return token

    def _headers(self, **extra) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        headers.update(extra)
        return headers

    def _api(self, method: str, path: str, **kwargs):
        url = path if path.startswith("http") else f"{DRIVE_API}{path}"
        for attempt in range(2):
            kwargs["headers"] = self._headers(**(kwargs.get("headers") or {}))
            response = http.request(method, url, provider=self.type_id, **kwargs)
            if response.status_code == 401 and attempt == 0:
                try:
                    response.close()
                except Exception:
                    pass
                self._token(force=True)
                continue
            return response
        return response

    @staticmethod
    def _q(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace("'", "\\'")

    # ------------------------------------------------------------------
    # Folder handling
    # ------------------------------------------------------------------
    def _find_child(self, parent_id: str, name: str, *, folder_only: bool = False) -> Optional[dict]:
        cache_key = (parent_id, f"{'d' if folder_only else 'f'}:{name}")
        if folder_only:
            cached = self._folder_cache.get(cache_key)
            if cached:
                return dict(cached)
        query = f"name = '{self._q(name)}' and '{self._q(parent_id)}' in parents and trashed = false"
        if folder_only:
            query += f" and mimeType = '{FOLDER_MIME}'"
        else:
            # A folder with the same name must never be mistaken for a file
            # (Google Drive permits duplicate names of different types).
            query += f" and mimeType != '{FOLDER_MIME}'"
        response = self._api(
            "GET",
            "/files",
            params={
                "q": query,
                "fields": "files(id,name,mimeType,size,modifiedTime,md5Checksum)",
                "pageSize": "10",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        data = http.json_response(response, "Google-Dateisuche", provider=self.type_id)
        files = data.get("files") or []
        if not files:
            return None
        item = files[0]
        if folder_only and item.get("id"):
            self._folder_cache[cache_key] = dict(item)
        return item

    def _create_folder(self, parent_id: str, name: str) -> str:
        response = self._api(
            "POST",
            "/files",
            params={"fields": "id,name", "supportsAllDrives": "true"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        )
        data = http.json_response(response, "Google-Ordner anlegen", provider=self.type_id)
        folder_id = str(data.get("id") or "").strip()
        if not folder_id:
            raise StorageError("Google legte keinen Ordner an.", provider=self.type_id)
        self._folder_cache[(parent_id, f"d:{name}")] = {
            "id": folder_id,
            "name": name,
            "mimeType": FOLDER_MIME,
        }
        return folder_id

    def _root_folder_id(self) -> str:
        configured = str(self.config.get("folder_id") or "").strip()
        if configured:
            return configured
        # No explicit target folder: write below the Drive root. The optional
        # base_path is created by _folder_for_parent() like any other segment.
        return "root"

    def _folder_for_parent(self, remote_path: str, *, create: bool = True) -> Optional[str]:
        parent = remote_parent(remote_path)
        parts = parent.split("/") if parent else []
        current = self._root_folder_id()
        for part in parts:
            found = self._find_child(current, part, folder_only=True)
            if found and found.get("id"):
                current = str(found["id"])
            elif create:
                current = self._create_folder(current, part)
            else:
                return None
        return current

    # ------------------------------------------------------------------
    # Provider API
    # ------------------------------------------------------------------
    def test_connection(self) -> dict:
        response = self._api(
            "GET",
            "/about",
            params={"fields": "user(displayName,emailAddress),storageQuota(limit,usage,usageInDrive)"},
        )
        data = http.json_response(response, "Google-Verbindungstest", provider=self.type_id)
        quota = data.get("storageQuota") or {}
        return {
            "ok": True,
            "account": (data.get("user") or {}).get("emailAddress") or (data.get("user") or {}).get("displayName") or "",
            "quota": {
                "limit": int(quota.get("limit") or 0),
                "usage": int(quota.get("usage") or 0),
                "usage_in_drive": int(quota.get("usageInDrive") or 0),
            },
        }

    def quota(self) -> Optional[dict]:
        response = self._api("GET", "/about", params={"fields": "storageQuota(limit,usage,usageInDrive)"})
        data = http.json_response(response, "Google-Quota", provider=self.type_id)
        quota = data.get("storageQuota") or {}
        return {
            "limit": int(quota.get("limit") or 0),
            "usage": int(quota.get("usage") or 0),
            "usage_in_drive": int(quota.get("usageInDrive") or 0),
        }

    def _upload_empty(self, name: str, parent_id: str, existing_id: str, mime: str) -> dict:
        """Multipart upload for zero-byte files (resumable uploads need chunks)."""
        boundary = "streamvault-empty-" + os.urandom(12).hex()
        metadata = {"name": name}
        if existing_id:
            url = f"{DRIVE_UPLOAD_API}/files/{urllib.parse.quote(existing_id)}"
            method = "PATCH"
        else:
            url = f"{DRIVE_UPLOAD_API}/files"
            method = "POST"
            metadata["parents"] = [parent_id]
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime}\r\n\r\n"
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        response = None
        for attempt in range(2):
            response = http.request(
                method,
                url,
                params={
                    "uploadType": "multipart",
                    "supportsAllDrives": "true",
                    "fields": "id,name,size,md5Checksum,modifiedTime",
                },
                headers={
                    "Authorization": f"Bearer {self._token(force=attempt > 0)}",
                    "Content-Type": f"multipart/related; boundary={boundary}",
                },
                data=body,
                provider=self.type_id,
                timeout=(10, 120),
            )
            if response.status_code != 401 or attempt == 1:
                break
            try:
                response.close()
            except Exception:
                pass
        data = http.json_response(response, "Google-Upload (leer)", provider=self.type_id)
        return {
            "remote_id": str(data.get("id") or existing_id or ""),
            "size": int(data.get("size") or 0),
            "checksum": str(data.get("md5Checksum") or ""),
            "modified_at": str(data.get("modifiedTime") or ""),
        }

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
        _auth_retried: bool = False,
    ) -> dict:
        remote_path = normalize_remote_path(remote_path)
        size = file_size(local_path)
        name = remote_name(remote_path)
        parent_id = self._folder_for_parent(remote_path)
        existing = self._find_child(parent_id, name)
        existing_id = str((existing or {}).get("id") or "").strip()
        mime = guess_mime_type(name, "video/mp4")
        if size == 0:
            result = self._upload_empty(name, parent_id, existing_id, mime)
            if progress:
                progress(0, 0)
            return result

        params = {
            "uploadType": "resumable",
            "supportsAllDrives": "true",
            "fields": "id,name,size,md5Checksum,modifiedTime",
        }
        if existing_id:
            url = f"{DRIVE_UPLOAD_API}/files/{existing_id}"
            method = "PATCH"
            body = {"name": name}
        else:
            url = f"{DRIVE_UPLOAD_API}/files"
            method = "POST"
            body = {"name": name, "parents": [parent_id]}
        response = None
        for attempt in range(2):
            headers = {
                "Authorization": f"Bearer {self._token(force=attempt > 0)}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime,
                "X-Upload-Content-Length": str(size),
            }
            response = http.request(
                method,
                url,
                params=params,
                headers=headers,
                data=json.dumps(body),
                provider=self.type_id,
                timeout=(10, 120),
            )
            if response.status_code != 401 or attempt == 1:
                break
            try:
                response.close()
            except Exception:
                pass
        http.raise_for_status(response, "Google-Upload starten", provider=self.type_id)
        location = str(response.headers.get("Location") or "").strip()
        if not location:
            raise StorageError("Google lieferte keine Upload-URL.", provider=self.type_id)

        offset = 0
        attempts = 0
        while offset < size:
            end = min(offset + UPLOAD_CHUNK, size) - 1
            with open(local_path, "rb") as handle:
                handle.seek(offset)
                chunk = handle.read(end - offset + 1)
            chunk_headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{size}",
                "Content-Type": mime,
            }
            try:
                chunk_resp = http.request(
                    "PUT",
                    location,
                    headers=chunk_headers,
                    data=chunk,
                    provider=self.type_id,
                    timeout=(10, 300),
                    retries=2,
                )
            except StorageError:
                attempts += 1
                if attempts >= 4:
                    raise
                time.sleep(min(15, 2 ** attempts))
                continue
            if chunk_resp.status_code in (200, 201):
                data = chunk_resp.json() if chunk_resp.content else {}
                if progress:
                    progress(size, size)
                return {
                    "remote_id": str((data or {}).get("id") or existing_id or ""),
                    "size": int((data or {}).get("size") or size),
                    "checksum": str((data or {}).get("md5Checksum") or ""),
                    "modified_at": str((data or {}).get("modifiedTime") or ""),
                }
            if chunk_resp.status_code == 308:
                next_offset = offset + len(chunk)
                range_header = str(chunk_resp.headers.get("Range") or "")
                if range_header.startswith("bytes="):
                    try:
                        next_offset = int(range_header.split("-", 1)[1]) + 1
                    except (IndexError, ValueError):
                        pass
                offset = max(offset, min(size, next_offset))
                if progress:
                    progress(offset, size)
                attempts = 0
                continue
            if chunk_resp.status_code == 401:
                if _auth_retried:
                    http.raise_for_status(chunk_resp, "Google-Upload", provider=self.type_id)
                self._token(force=True)
                return self.upload_file(
                    local_path,
                    remote_path,
                    progress=progress,
                    _auth_retried=True,
                )
            if chunk_resp.status_code in http.RETRY_STATUS:
                attempts += 1
                if attempts >= 4:
                    http.raise_for_status(chunk_resp, "Google-Upload", provider=self.type_id)
                time.sleep(min(20, 2 ** attempts))
                continue
            http.raise_for_status(chunk_resp, "Google-Upload", provider=self.type_id)
        raise StorageError(
            "Google-Upload endete ohne Abschlussantwort.",
            provider=self.type_id,
            retryable=True,
        )

    def _download_response(self, remote_path: str, *, range_header: str = "") -> "object":
        remote_path = normalize_remote_path(remote_path)
        parent_id = self._folder_for_parent(remote_path, create=False)
        if not parent_id:
            raise StorageError(f"Datei nicht auf Google Drive gefunden: {remote_path}", provider=self.type_id)
        found = self._find_child(parent_id, remote_name(remote_path))
        if not found or not found.get("id"):
            raise StorageError(f"Datei nicht auf Google Drive gefunden: {remote_path}", provider=self.type_id)
        headers = self._headers()
        if range_header:
            headers["Range"] = range_header
        return http.request(
            "GET",
            f"{DRIVE_API}/files/{urllib.parse.quote(str(found['id']))}",
            params={"alt": "media", "supportsAllDrives": "true"},
            headers=headers,
            provider=self.type_id,
            stream=True,
            timeout=(10, 300),
        ), found

    def download_file(
        self,
        remote_path: str,
        dest_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        response, found = self._download_response(remote_path)
        http.raise_for_status(response, "Google-Download", provider=self.type_id)
        total = int(response.headers.get("Content-Length") or found.get("size") or 0)
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
        return {"remote_id": str(found.get("id") or ""), "size": done}

    def open_range(self, remote_path: str, start: int, end: int) -> Iterable[bytes]:
        start = max(0, int(start))
        end = max(start, int(end))
        response, _found = self._download_response(remote_path, range_header=f"bytes={start}-{end}")
        http.raise_for_status(response, "Google-Range-Stream", provider=self.type_id)
        try:
            yield from iter_response_range(response, start, end)
        finally:
            try:
                response.close()
            except Exception:
                pass

    def delete(self, remote_path: str) -> None:
        remote_path = normalize_remote_path(remote_path)
        parent_id = self._folder_for_parent(remote_path, create=False)
        if not parent_id:
            return
        found = self._find_child(parent_id, remote_name(remote_path))
        if not found or not found.get("id"):
            return
        response = self._api(
            "DELETE",
            f"/files/{urllib.parse.quote(str(found['id']))}",
            params={"supportsAllDrives": "true"},
        )
        if response.status_code == 404:
            return
        http.raise_for_status(response, "Google-Datei löschen", provider=self.type_id)

    def list(self, prefix: str = "") -> list[RemoteObject]:
        prefix = str(prefix or "").strip().strip("/")
        if prefix:
            parent_id = self._folder_for_parent(f"{prefix}/x", create=False)
        else:
            base_path = str(self.config.get("base_path") or "").strip().strip("/")
            parent_id = (
                self._folder_for_parent(f"{base_path}/x", create=False)
                if base_path
                else self._root_folder_id()
            )
        if not parent_id:
            return []
        params = {
            "q": f"'{self._q(parent_id)}' in parents and trashed = false",
            "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
            "pageSize": "1000",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        items = []
        while True:
            response = self._api("GET", "/files", params=params)
            data = http.json_response(response, "Google-Dateiliste", provider=self.type_id)
            for item in data.get("files") or []:
                items.append(
                    RemoteObject(
                        path=(f"{prefix}/" if prefix else "") + str(item.get("name") or ""),
                        remote_id=str(item.get("id") or ""),
                        size=int(item.get("size") or 0),
                        modified_at=str(item.get("modifiedTime") or ""),
                        is_folder=item.get("mimeType") == FOLDER_MIME,
                    )
                )
            token = str(data.get("nextPageToken") or "")
            if not token:
                break
            params["pageToken"] = token
        return items

    def exists(self, remote_path: str, expected_size: int = 0) -> bool:
        parent_id = self._folder_for_parent(remote_path, create=False)
        if not parent_id:
            return False
        found = self._find_child(parent_id, remote_name(remote_path))
        if not found or not found.get("id"):
            return False
        expected = int(expected_size or 0)
        if expected <= 0:
            return True
        return int(found.get("size") or 0) == expected
