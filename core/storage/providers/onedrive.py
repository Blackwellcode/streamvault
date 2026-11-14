"""Microsoft OneDrive provider via Microsoft Graph (OAuth 2.0)."""

from __future__ import annotations

import logging
import os
import time
import urllib.parse
from typing import Iterable, Optional

from flask_babel import lazy_gettext

from core.storage.base import (
    close_quietly,
    mask_error,
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

logger = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
DEFAULT_SCOPE = "offline_access Files.ReadWrite"
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
SESSION_CHUNK = 10 * 1024 * 1024  # multiple of 320 KiB


class OneDriveProvider(StorageProvider):
    type_id = "onedrive"
    label = "Microsoft OneDrive"
    description = lazy_gettext("OneDrive / Microsoft 365 via Microsoft Graph with OAuth 2.0.")
    oauth_supported = True
    oauth_label = lazy_gettext("Connect with Microsoft")
    docs_url = "https://learn.microsoft.com/graph/api/resources/driveitem"
    config_fields = (
        ProviderField("client_id", "Application (Client) ID", required=True),
        ProviderField("client_secret", "Client Secret", type="password", required=True, secret=True),
        ProviderField("tenant_id", "Tenant ID", default="common", placeholder="common / contoso.onmicrosoft.com"),
        ProviderField("refresh_token", "Refresh Token", type="password", secret=True,
                      help=lazy_gettext("Saved automatically when connecting.")),
        ProviderField("scope", "OAuth Scope", default=DEFAULT_SCOPE),
        ProviderField("base_path", lazy_gettext("Subfolder"), default="StreamVault", placeholder="StreamVault"),
    )

    def __init__(self, config: dict):
        super().__init__(config)
        self._access = ""
        self._expires_at = 0.0

    @classmethod
    def oauth_ready(cls, config: dict) -> bool:
        return bool(str(config.get("client_id") or "").strip() and str(config.get("client_secret") or "").strip())

    @classmethod
    def _tenant(cls, config: dict) -> str:
        return str(config.get("tenant_id") or "common").strip() or "common"

    @classmethod
    def oauth_authorize_url(cls, config: dict, redirect_uri: str, state: str) -> str:
        if not cls.oauth_ready(config):
            raise StorageConfigError("OneDrive Client ID und Client Secret fehlen.")
        params = {
            "client_id": str(config.get("client_id") or "").strip(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": str(config.get("scope") or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE,
            "state": state,
        }
        return AUTH_URL.format(tenant=urllib.parse.quote(cls._tenant(config))) + "?" + urllib.parse.urlencode(params)

    @classmethod
    def oauth_exchange_code(cls, config: dict, code: str, redirect_uri: str) -> dict:
        response = http.request(
            "POST",
            TOKEN_URL.format(tenant=urllib.parse.quote(cls._tenant(config))),
            data={
                "client_id": str(config.get("client_id") or "").strip(),
                "client_secret": str(config.get("client_secret") or "").strip(),
                "code": str(code or "").strip(),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "scope": str(config.get("scope") or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE,
            },
            provider=cls.type_id,
        )
        data = http.json_response(response, "Microsoft-Token-Austausch", provider=cls.type_id)
        refresh = str(data.get("refresh_token") or "").strip()
        if not refresh:
            raise StorageError("Microsoft lieferte keinen Refresh Token.", provider=cls.type_id)
        return {"refresh_token": refresh}

    def _token(self, force: bool = False) -> str:
        now = time.time()
        if not force and self._access and now < self._expires_at - 60:
            return self._access
        refresh = str(self.config.get("refresh_token") or "").strip()
        client_id = str(self.config.get("client_id") or "").strip()
        client_secret = str(self.config.get("client_secret") or "").strip()
        if not refresh or not client_id or not client_secret:
            raise StorageConfigError("OneDrive ist nicht vollständig konfiguriert.")
        response = http.request(
            "POST",
            TOKEN_URL.format(tenant=urllib.parse.quote(self._tenant(self.config))),
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
                "scope": str(self.config.get("scope") or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE,
            },
            provider=self.type_id,
        )
        data = http.json_response(response, "Microsoft-Token-Erneuerung", provider=self.type_id)
        self._access = str(data.get("access_token") or "").strip()
        if not self._access:
            raise StorageError("Microsoft lieferte kein Access Token.", provider=self.type_id)
        try:
            self._expires_at = now + int(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            self._expires_at = now + 3600
        rotated = str(data.get("refresh_token") or "").strip()
        if rotated and rotated != refresh:
            # Microsoft may rotate refresh tokens. Persist the new value so a
            # later worker process does not keep using an invalidated token.
            self.config["refresh_token"] = rotated
            self._config_changed()
        return self._access

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if extra:
            headers.update(extra)
        return headers

    def _path(self, remote_path: str) -> str:
        path = normalize_remote_path(remote_path)
        return "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))

    def _item_url(self, remote_path: str, suffix: str = "") -> str:
        return f"{GRAPH}/me/drive/root:/{self._path(remote_path)}:{suffix}"

    def _api(self, method: str, url: str, **kwargs):
        for attempt in range(2):
            kwargs["headers"] = self._headers(**(kwargs.get("headers") or {}))
            response = http.request(method, url, provider=self.type_id, **kwargs)
            if response.status_code == 401 and attempt == 0:
                close_quietly(response, provider=self.type_id)
                self._token(force=True)
                continue
            return response
        return response

    def _ensure_dirs(self, remote_path: str) -> None:
        parent = remote_parent(remote_path)
        if not parent:
            return
        current = ""
        for part in parent.split("/"):
            # POST creates a child below the *existing* parent.  The first
            # segment therefore belongs to /root/children; later segments use
            # the already-created folder path.
            if current:
                url = f"{GRAPH}/me/drive/root:/{self._path(current)}:/children"
            else:
                url = f"{GRAPH}/me/drive/root/children"
            body = {"name": part, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
            response = self._api("POST", url, json=body, timeout=(10, 60))
            if response.status_code in (200, 201, 409):
                current = f"{current}/{part}" if current else part
                continue
            http.raise_for_status(response, "OneDrive-Ordner anlegen", provider=self.type_id)

    def test_connection(self) -> dict:
        response = self._api("GET", f"{GRAPH}/me/drive", params={"$select": "id,driveType,owner,quota"})
        data = http.json_response(response, "OneDrive-Verbindungstest", provider=self.type_id)
        owner = data.get("owner") or {}
        user = owner.get("user") or {}
        quota = data.get("quota") or {}
        return {
            "ok": True,
            "account": str(user.get("email") or user.get("displayName") or ""),
            "quota": {
                "limit": int(quota.get("total") or 0),
                "usage": int(quota.get("used") or 0),
            },
        }

    def quota(self) -> Optional[dict]:
        try:
            response = self._api("GET", f"{GRAPH}/me/drive", params={"$select": "quota"})
            data = http.json_response(response, "OneDrive-Quota", provider=self.type_id)
            quota = data.get("quota") or {}
            return {"limit": int(quota.get("total") or 0), "usage": int(quota.get("used") or 0)}
        except (StorageError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("%s quota unavailable: %s", self.type_id, mask_error(exc))
            return None

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        size = file_size(local_path)
        remote_path = normalize_remote_path(remote_path)
        self._ensure_dirs(remote_path)
        mime = guess_mime_type(remote_path, "video/mp4")
        if size <= SIMPLE_UPLOAD_MAX:
            with open(local_path, "rb") as handle:
                payload = handle.read()
            response = self._api(
                "PUT",
                self._item_url(remote_path, "/content"),
                data=payload,
                headers={"Content-Type": mime, "Content-Length": str(size)},
                timeout=(10, 300),
            )
            data = http.json_response(response, "OneDrive-Upload", provider=self.type_id)
            if progress:
                progress(size, size)
            return {
                "remote_id": str(data.get("id") or remote_path),
                "size": int(data.get("size") or size),
                "checksum": "",
                "modified_at": str(data.get("lastModifiedDateTime") or ""),
            }

        response = self._api(
            "POST",
            self._item_url(remote_path, "/createUploadSession"),
            json={"item": {"@microsoft.graph.conflictBehavior": "replace", "name": remote_name(remote_path)}},
            timeout=(10, 60),
        )
        data = http.json_response(response, "OneDrive-Upload-Session", provider=self.type_id)
        upload_url = str(data.get("uploadUrl") or "")
        if not upload_url:
            raise StorageError("OneDrive lieferte keine Upload-URL.", provider=self.type_id)
        offset = 0
        attempts = 0
        with open(local_path, "rb") as handle:
            while offset < size:
                handle.seek(offset)
                chunk = handle.read(min(SESSION_CHUNK, size - offset))
                end = offset + len(chunk) - 1
                try:
                    chunk_resp = http.request(
                        "PUT",
                        upload_url,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end}/{size}",
                            "Content-Type": mime,
                        },
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
                    result = chunk_resp.json() if chunk_resp.content else {}
                    if progress:
                        progress(size, size)
                    return {
                        "remote_id": str((result or {}).get("id") or remote_path),
                        "size": int((result or {}).get("size") or size),
                        "checksum": "",
                        "modified_at": str((result or {}).get("lastModifiedDateTime") or ""),
                    }
                if chunk_resp.status_code == 202:
                    offset = end + 1
                    attempts = 0
                    if progress:
                        progress(offset, size)
                    continue
                if chunk_resp.status_code in http.RETRY_STATUS:
                    attempts += 1
                    if attempts >= 4:
                        http.raise_for_status(chunk_resp, "OneDrive-Upload", provider=self.type_id)
                    time.sleep(min(20, 2 ** attempts))
                    continue
                http.raise_for_status(chunk_resp, "OneDrive-Upload", provider=self.type_id)
        raise StorageError("OneDrive-Upload endete unerwartet.", provider=self.type_id)

    def _download_response(self, remote_path: str, *, range_header: str = ""):
        headers = {"Range": range_header} if range_header else None
        response = self._api(
            "GET",
            self._item_url(remote_path, "/content"),
            headers=headers,
            allow_redirects=False,
            stream=True,
            timeout=(10, 600),
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location") or ""
            response.close()
            if not location:
                raise StorageError("OneDrive-Download ohne Redirect-Ziel.", provider=self.type_id)
            extra = {"Range": range_header} if range_header else None
            response = http.request(
                "GET",
                location,
                headers=extra,
                stream=True,
                provider=self.type_id,
                timeout=(10, 600),
                retries=2,
            )
        return response

    def download_file(
        self,
        remote_path: str,
        dest_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        response = self._download_response(remote_path)
        http.raise_for_status(response, "OneDrive-Download", provider=self.type_id)
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
            close_quietly(response, provider=self.type_id)
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        return {"remote_id": remote_path, "size": done}

    def open_range(self, remote_path: str, start: int, end: int) -> Iterable[bytes]:
        start = max(0, int(start))
        end = max(start, int(end))
        response = self._download_response(remote_path, range_header=f"bytes={start}-{end}")
        http.raise_for_status(response, "OneDrive-Range-Stream", provider=self.type_id)
        try:
            yield from iter_response_range(response, start, end)
        finally:
            close_quietly(response, provider=self.type_id)

    def delete(self, remote_path: str) -> None:
        response = self._api("DELETE", self._item_url(remote_path), timeout=(10, 60))
        if response.status_code == 404:
            return
        http.raise_for_status(response, "OneDrive-Datei löschen", provider=self.type_id)

    def list(self, prefix: str = "") -> list[RemoteObject]:
        prefix = str(prefix or "").strip().strip("/")
        if prefix:
            url = f"{GRAPH}/me/drive/root:/{self._path(prefix)}:/children"
        else:
            url = f"{GRAPH}/me/drive/root/children"
        items = []
        params = {"$top": "1000"}
        while url:
            response = self._api("GET", url, params=params, timeout=(10, 60))
            data = http.json_response(response, "OneDrive-Dateiliste", provider=self.type_id)
            for item in data.get("value") or []:
                name = str(item.get("name") or "")
                path = f"{prefix}/{name}" if prefix else name
                items.append(
                    RemoteObject(
                        path=path,
                        remote_id=str(item.get("id") or path),
                        size=int(item.get("size") or 0),
                        modified_at=str(item.get("lastModifiedDateTime") or ""),
                        is_folder=bool(item.get("folder")),
                    )
                )
            url = str(data.get("@odata.nextLink") or "")
            params = None
        return items

    def exists(self, remote_path: str, expected_size: int = 0) -> bool:
        response = self._api(
            "GET",
            self._item_url(remote_path),
            params={"$select": "size"},
            timeout=(10, 30),
        )
        if response.status_code == 404:
            return False
        data = http.json_response(response, "OneDrive-Existenzprüfung", provider=self.type_id)
        expected = int(expected_size or 0)
        if expected <= 0:
            return True
        return int(data.get("size") or 0) == expected
