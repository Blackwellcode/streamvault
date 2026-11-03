"""Provider registry used by WebUI, worker and CLI."""

from __future__ import annotations

from .base import StorageConfigError, StorageProvider
from .providers.dropbox import DropboxProvider
from .providers.google_drive import GoogleDriveProvider
from .providers.onedrive import OneDriveProvider
from .providers.s3 import S3Provider
from .providers.webdav import WebDAVProvider

PROVIDER_CLASSES: dict[str, type[StorageProvider]] = {
    cls.type_id: cls
    for cls in (
        GoogleDriveProvider,
        S3Provider,
        DropboxProvider,
        OneDriveProvider,
        WebDAVProvider,
    )
}


def get_provider_class(type_id: str) -> type[StorageProvider]:
    key = str(type_id or "").strip().lower()
    cls = PROVIDER_CLASSES.get(key)
    if cls is None:
        raise StorageConfigError(f"Unbekannter Speicher-Anbieter: {type_id}")
    return cls


def provider_catalog() -> list[dict]:
    """Serialisable provider metadata for templates and CLI output."""
    catalog = []
    for type_id, cls in PROVIDER_CLASSES.items():
        catalog.append(
            {
                "type_id": type_id,
                "label": cls.label,
                "description": cls.description,
                "oauth_supported": bool(cls.oauth_supported),
                "oauth_label": cls.oauth_label,
                "docs_url": cls.docs_url,
                "fields": [field.as_dict() for field in cls.config_fields],
                "secret_keys": sorted(cls.secret_keys()),
                "default_config": cls.default_config(),
            }
        )
    return catalog
