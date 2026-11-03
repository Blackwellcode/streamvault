"""S3-compatible object storage (AWS, R2, B2, Wasabi, MinIO, Hetzner, ...)."""

from __future__ import annotations

import os
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
    guess_mime_type,
    normalize_remote_path,
)

try:  # optional dependency: the UI/CLI explains how to install it
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError
except Exception:  # pragma: no cover - exercised only when boto3 is missing
    boto3 = None
    TransferConfig = None
    BotoConfig = None
    BotoCoreError = Exception
    # An empty tuple keeps isinstance() checks valid when boto3 is absent.
    ClientError = ()


class S3Provider(StorageProvider):
    type_id = "s3"
    label = "S3-kompatibel"
    description = "AWS S3, Cloudflare R2, Backblaze B2, Wasabi, MinIO, Hetzner oder jeder S3-kompatible Endpunkt."
    docs_url = "https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html"
    config_fields = (
        ProviderField("bucket", "Bucket", required=True, placeholder="streamvault-archive"),
        ProviderField("access_key", "Access Key ID", required=True, secret=True),
        ProviderField("secret_key", "Secret Access Key", type="password", required=True, secret=True),
        ProviderField(
            "region",
            "Region",
            placeholder="us-east-1 / eu-central-1 / auto",
            help="Leer = us-east-1 für AWS. Cloudflare R2 verwendet 'auto'; "
                 "andere S3-Anbieter ihre jeweilige Region.",
        ),
        ProviderField(
            "endpoint_url",
            "Endpunkt-URL (optional)",
            placeholder="https://<account>.r2.cloudflarestorage.com",
            help="Leer = AWS. Für R2, B2, Wasabi, MinIO etc. den S3-Endpunkt eintragen.",
        ),
        ProviderField(
            "addressing_style",
            "Adressierung",
            type="select",
            default="auto",
            choices=(("auto", "Automatisch"), ("path", "Path-Style"), ("virtual", "Virtual-Hosted")),
        ),
        ProviderField("base_path", "Präfix", default="StreamVault", placeholder="StreamVault"),
        ProviderField(
            "server_side_encryption",
            "Serverseitige Verschlüsselung",
            type="select",
            default="",
            choices=(("", "Anbieter-Standard"), ("AES256", "S3 AES256"), ("aws:kms", "AWS KMS")),
        ),
    )

    def __init__(self, config: dict):
        super().__init__(config)
        self._client_obj = None

    @classmethod
    def validate_config(cls, config: dict, *, require_secrets: bool = True) -> list[str]:
        errors = super().validate_config(config, require_secrets=require_secrets)
        endpoint = str((config or {}).get("endpoint_url") or "").strip()
        if endpoint:
            parsed = urllib.parse.urlparse(endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                errors.append("Endpunkt-URL muss eine gültige http(s)-URL sein.")
            if parsed.username is not None or parsed.password is not None:
                errors.append(
                    "Endpunkt-URL darf keine Zugangsdaten enthalten; "
                    "nutze Access Key und Secret Key."
                )
            if parsed.query or parsed.fragment:
                errors.append("Endpunkt-URL darf keine Query oder Fragment enthalten.")
        bucket = str((config or {}).get("bucket") or "").strip()
        if bucket and ("/" in bucket or " " in bucket):
            errors.append("Bucket-Name enthält ungültige Zeichen.")
        return errors

    def _client(self):
        if boto3 is None:
            raise StorageConfigError(
                "S3-Unterstützung benötigt das Python-Paket 'boto3'. "
                "Installation: pip install -r requirements.txt"
            )
        if self._client_obj is not None:
            return self._client_obj
        cfg = self.config
        bucket = str(cfg.get("bucket") or "").strip()
        access = str(cfg.get("access_key") or "").strip()
        secret = str(cfg.get("secret_key") or "").strip()
        if not bucket or not access or not secret:
            raise StorageConfigError("S3 ist nicht vollständig konfiguriert (Bucket, Access Key, Secret Key).")
        addressing = str(cfg.get("addressing_style") or "auto").strip().lower()
        if addressing not in ("auto", "path", "virtual"):
            addressing = "auto"
        boto_cfg = BotoConfig(
            retries={"max_attempts": 4, "mode": "standard"},
            connect_timeout=10,
            read_timeout=120,
            signature_version="s3v4",
            s3={"addressing_style": addressing},
        )
        kwargs = {
            "aws_access_key_id": access,
            "aws_secret_access_key": secret,
            "region_name": str(cfg.get("region") or "").strip() or "us-east-1",
            "config": boto_cfg,
        }
        endpoint = str(cfg.get("endpoint_url") or "").strip()
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        self._client_obj = boto3.client("s3", **kwargs)
        return self._client_obj

    def _key(self, remote_path: str) -> str:
        return normalize_remote_path(remote_path)

    def _extra_args(self) -> dict:
        args = {}
        encryption = str(self.config.get("server_side_encryption") or "").strip()
        if encryption:
            args["ServerSideEncryption"] = encryption
        return args

    @staticmethod
    def _error(action: str, exc: Exception) -> StorageError:
        if isinstance(exc, ClientError):
            error = (exc.response or {}).get("Error") or {}
            code = str(error.get("Code") or "")
            message = str(error.get("Message") or exc)
            status = (exc.response or {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            retryable = code in {"SlowDown", "RequestTimeout", "InternalError", "ServiceUnavailable"} or (
                isinstance(status, int) and status >= 500
            )
            return StorageError(
                f"{action} fehlgeschlagen: {message}",
                provider="s3",
                status_code=status if isinstance(status, int) else None,
                retryable=retryable,
                details=code,
            )
        return StorageError(f"{action} fehlgeschlagen: {exc}", provider="s3", retryable=True)

    def test_connection(self) -> dict:
        try:
            client = self._client()
            bucket = str(self.config.get("bucket") or "").strip()
            client.head_bucket(Bucket=bucket)
            client.list_objects_v2(Bucket=bucket, Prefix=str(self.config.get("base_path") or "").strip("/"), MaxKeys=1)
            return {"ok": True, "account": bucket, "quota": None}
        except StorageError:
            raise
        except Exception as exc:
            raise self._error("S3-Verbindungstest", exc) from exc

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        size = file_size(local_path)
        key = self._key(remote_path)
        callback = None
        if progress:
            def callback(bytes_amount):
                progress(min(size, int(bytes_amount)), size)

        try:
            client = self._client()
            config = TransferConfig(
                multipart_threshold=64 * 1024 * 1024,
                multipart_chunksize=64 * 1024 * 1024,
                max_concurrency=4,
                use_threads=True,
            )
            extra = {"ContentType": guess_mime_type(key, "video/mp4")}
            extra.update(self._extra_args())
            client.upload_file(
                local_path,
                str(self.config.get("bucket") or "").strip(),
                key,
                ExtraArgs=extra,
                Config=config,
                Callback=callback,
            )
            return {"remote_id": key, "size": size, "checksum": "", "modified_at": ""}
        except StorageError:
            raise
        except Exception as exc:
            raise self._error("S3-Upload", exc) from exc

    def download_file(
        self,
        remote_path: str,
        dest_path: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        key = self._key(remote_path)
        tmp_path = f"{dest_path}.part"
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        callback = None
        if progress:
            def callback(bytes_amount):
                progress(int(bytes_amount), 0)
        try:
            client = self._client()
            client.download_file(
                str(self.config.get("bucket") or "").strip(),
                key,
                tmp_path,
                Callback=callback,
            )
            os.replace(tmp_path, dest_path)
            return {"remote_id": key, "size": os.path.getsize(dest_path)}
        except StorageError:
            raise
        except Exception as exc:
            raise self._error("S3-Download", exc) from exc
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def open_range(self, remote_path: str, start: int, end: int) -> Iterable[bytes]:
        key = self._key(remote_path)
        try:
            client = self._client()
            response = client.get_object(
                Bucket=str(self.config.get("bucket") or "").strip(),
                Key=key,
                Range=f"bytes={max(0, int(start))}-{max(0, int(end))}",
            )
        except Exception as exc:
            raise self._error("S3-Range-Stream", exc) from exc
        body = response.get("Body")
        if body is None:
            raise StorageError("S3 lieferte keinen Stream.", provider="s3")
        try:
            while True:
                chunk = body.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                body.close()
            except Exception:
                pass

    def delete(self, remote_path: str) -> None:
        key = self._key(remote_path)
        try:
            client = self._client()
            client.delete_object(Bucket=str(self.config.get("bucket") or "").strip(), Key=key)
        except Exception as exc:
            raise self._error("S3-Datei löschen", exc) from exc

    def list(self, prefix: str = "") -> list[RemoteObject]:
        base = normalize_remote_path(prefix) if str(prefix or "").strip() else ""
        try:
            client = self._client()
            paginator = client.get_paginator("list_objects_v2")
            kwargs = {"Bucket": str(self.config.get("bucket") or "").strip()}
            if base:
                kwargs["Prefix"] = base.rstrip("/") + "/"
            items = []
            for page in paginator.paginate(**kwargs):
                for item in page.get("Contents") or []:
                    items.append(
                        RemoteObject(
                            path=str(item.get("Key") or ""),
                            remote_id=str(item.get("Key") or ""),
                            size=int(item.get("Size") or 0),
                            modified_at=str(item.get("LastModified") or ""),
                        )
                    )
            return items
        except Exception as exc:
            raise self._error("S3-Dateiliste", exc) from exc

    def exists(self, remote_path: str, expected_size: int = 0) -> bool:
        key = self._key(remote_path)
        try:
            client = self._client()
            head = client.head_object(
                Bucket=str(self.config.get("bucket") or "").strip(),
                Key=key,
            )
        except Exception as exc:
            if isinstance(exc, ClientError):
                error = (exc.response or {}).get("Error") or {}
                code = str(error.get("Code") or "")
                status = (exc.response or {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                    return False
            raise self._error("S3-Existenzprüfung", exc) from exc
        expected = int(expected_size or 0)
        if expected <= 0:
            return True
        return int(head.get("ContentLength") or 0) == expected
