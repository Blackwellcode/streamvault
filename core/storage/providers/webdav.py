"""Generic WebDAV provider (Nextcloud, ownCloud, Hetzner Storage Box, ...)."""

from __future__ import annotations

import logging
import os
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Iterable, Optional

from flask_babel import gettext, lazy_gettext

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
    remote_parent,
)
from core.storage import http

logger = logging.getLogger(__name__)
DAV = "{DAV:}"


class WebDAVProvider(StorageProvider):
    type_id = "webdav"
    label = "WebDAV"
    description = lazy_gettext("Nextcloud, ownCloud, Hetzner Storage Box or any WebDAV server with an app password.")
    docs_url = "https://datatracker.ietf.org/doc/html/rfc4918"
    config_fields = (
        ProviderField(
            "base_url",
            lazy_gettext("WebDAV base URL"),
            required=True,
            placeholder="https://cloud.example.com/remote.php/dav/files/user/",
            help=lazy_gettext("Must point to a writable folder and end with / (added automatically)."),
        ),
        ProviderField("username", lazy_gettext("Username"), required=True),
        ProviderField("password", lazy_gettext("Password / app password"), type="password", required=True, secret=True),
        ProviderField("base_path", lazy_gettext("Subfolder"), default="StreamVault", placeholder="StreamVault"),
        ProviderField(
            "verify_ssl",
            lazy_gettext("Verify TLS certificate"),
            type="select",
            default="1",
            choices=(("1", lazy_gettext("Yes (recommended)")), ("0", lazy_gettext("No (self-signed only)"))),
        ),
    )

    @classmethod
    def validate_config(cls, config: dict, *, require_secrets: bool = True) -> list[str]:
        errors = super().validate_config(config, require_secrets=require_secrets)
        base = str((config or {}).get("base_url") or "").strip()
        if base:
            parsed = urllib.parse.urlparse(base)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                errors.append(gettext("WebDAV base URL must be a valid http(s) URL."))
            if parsed.username is not None or parsed.password is not None:
                errors.append(gettext(
                    "WebDAV base URL must not contain credentials; "
                    "use the username and password fields."
                ))
            if parsed.query or parsed.fragment:
                errors.append(gettext("WebDAV base URL must not contain a query or fragment."))
        return errors

    def _base(self) -> str:
        base = str(self.config.get("base_url") or "").strip()
        if not base:
            raise StorageConfigError("WebDAV-Basis-URL fehlt.")
        return base.rstrip("/") + "/"

    def _auth(self):
        user = str(self.config.get("username") or "")
        password = str(self.config.get("password") or "")
        return (user, password) if user else None

    def _verify(self) -> bool:
        return str(self.config.get("verify_ssl") or "1").strip().lower() not in ("0", "false", "no", "off")

    def _url(self, remote_path: str) -> str:
        path = normalize_remote_path(remote_path)
        quoted = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))
        return self._base() + quoted

    def _request(self, method: str, url: str, **kwargs):
        kwargs.setdefault("auth", self._auth())
        kwargs.setdefault("verify", self._verify())
        kwargs.setdefault("provider", self.type_id)
        return http.request(method, url, **kwargs)

    def _ensure_dirs(self, remote_path: str) -> None:
        parent = remote_parent(remote_path)
        if not parent:
            return
        parts = parent.split("/")
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            url = self._url(current)
            response = self._request("MKCOL", url, timeout=(10, 30), retries=2)
            if response.status_code in (200, 201, 204, 301, 302, 405):
                continue
            if response.status_code == 409:
                # Parent was just created; retry once.
                response = self._request("MKCOL", url, timeout=(10, 30), retries=1)
                if response.status_code in (200, 201, 204, 405):
                    continue
            http.raise_for_status(response, "WebDAV-Ordner anlegen", provider=self.type_id)

    def test_connection(self) -> dict:
        response = self._request(
            "PROPFIND",
            self._base(),
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            data='<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>',
            timeout=(10, 30),
        )
        if response.status_code not in (200, 207):
            http.raise_for_status(response, "WebDAV-Verbindungstest", provider=self.type_id)
        return {"ok": True, "account": str(self.config.get("username") or ""), "quota": self.quota()}

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        remote_path = normalize_remote_path(remote_path)
        size = file_size(local_path)
        self._ensure_dirs(remote_path)
        url = self._url(remote_path)
        with open(local_path, "rb") as handle:
            response = self._request(
                "PUT",
                url,
                data=handle,
                headers={
                    "Content-Type": guess_mime_type(remote_path, "video/mp4"),
                    "Content-Length": str(size),
                },
                timeout=(10, 600),
                retries=1,
            )
        http.raise_for_status(response, "WebDAV-Upload", provider=self.type_id)
        if progress:
            progress(size, size)
        return {"remote_id": remote_path, "size": size, "checksum": "", "modified_at": ""}

    def download_file(
        self,
        remote_path: str,
        dest_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        remote_path = normalize_remote_path(remote_path)
        response = self._request(
            "GET",
            self._url(remote_path),
            stream=True,
            timeout=(10, 600),
        )
        http.raise_for_status(response, "WebDAV-Download", provider=self.type_id)
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
        remote_path = normalize_remote_path(remote_path)
        response = self._request(
            "GET",
            self._url(remote_path),
            headers={"Range": f"bytes={max(0, int(start))}-{max(0, int(end))}"},
            stream=True,
            timeout=(10, 300),
        )
        http.raise_for_status(response, "WebDAV-Range-Stream", provider=self.type_id)
        try:
            yield from iter_response_range(response, start, end)
        finally:
            close_quietly(response, provider=self.type_id)

    def delete(self, remote_path: str) -> None:
        response = self._request("DELETE", self._url(remote_path), timeout=(10, 60), retries=2)
        if response.status_code == 404:
            return
        http.raise_for_status(response, "WebDAV-Datei löschen", provider=self.type_id)

    def list(self, prefix: str = "") -> list[RemoteObject]:
        prefix = str(prefix or "").strip().strip("/")
        url = self._url(prefix) if prefix else self._base()
        response = self._request(
            "PROPFIND",
            url,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            data='<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>',
            timeout=(10, 60),
        )
        if response.status_code not in (200, 207):
            http.raise_for_status(response, "WebDAV-Dateiliste", provider=self.type_id)
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise StorageError("WebDAV lieferte ungültiges XML.", provider=self.type_id) from exc
        items = []
        base_path = urllib.parse.unquote(
            urllib.parse.urlparse(self._base()).path.rstrip("/")
        )
        for node in root.findall(f".//{DAV}response"):
            href = node.findtext(f"{DAV}href") or ""
            path = urllib.parse.unquote(urllib.parse.urlparse(href).path)
            if path.startswith(base_path):
                path = path[len(base_path):].lstrip("/")
            if not path or path.rstrip("/") == prefix.rstrip("/"):
                continue
            prop = node.find(f"{DAV}propstat/{DAV}prop")
            if prop is None:
                continue
            is_folder = prop.find(f"{DAV}resourcetype/{DAV}collection") is not None
            size_text = prop.findtext(f"{DAV}getcontentlength") or "0"
            try:
                size = int(size_text)
            except ValueError:
                size = 0
            items.append(
                RemoteObject(
                    path=path.rstrip("/"),
                    remote_id=path.rstrip("/"),
                    size=size,
                    modified_at=prop.findtext(f"{DAV}getlastmodified") or "",
                    is_folder=is_folder,
                )
            )
        return items

    def exists(self, remote_path: str, expected_size: int = 0) -> bool:
        response = self._request(
            "HEAD",
            self._url(remote_path),
            timeout=(10, 30),
        )
        if response.status_code == 404:
            return False
        if response.status_code in (405, 501):
            return super().exists(remote_path, expected_size)
        if response.status_code not in (200, 204):
            http.raise_for_status(response, "WebDAV-Existenzprüfung", provider=self.type_id)
        size_text = str(response.headers.get("Content-Length") or "0").strip()
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        expected = int(expected_size or 0)
        if expected <= 0:
            return True
        return size == expected

    def quota(self) -> Optional[dict]:
        try:
            response = self._request(
                "PROPFIND",
                self._base(),
                headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
                data=(
                    '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                    '<d:quota-available-bytes/><d:quota-used-bytes/>'
                    '</d:prop></d:propfind>'
                ),
                timeout=(10, 30),
            )
            if response.status_code not in (200, 207):
                return None
            root = ET.fromstring(response.content)
            available = root.findtext(f".//{DAV}quota-available-bytes")
            used = root.findtext(f".//{DAV}quota-used-bytes")
            if available is None and used is None:
                return None
            available_i = int(available) if available and available.isdigit() else 0
            used_i = int(used) if used and used.isdigit() else 0
            return {"limit": available_i + used_i, "usage": used_i}
        except (StorageError, ET.ParseError, ValueError) as exc:
            logger.warning("%s quota unavailable: %s", self.type_id, mask_error(exc))
            return None
