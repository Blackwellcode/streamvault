"""AES-GCM encryption for provider credentials at rest.

The key is derived from ``STORAGE_ENCRYPTION_KEY`` when set, otherwise from
``VOD_SECRET_KEY``.  Credentials are never written to logs or returned to the
browser in clear text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

from core import config
from .base import StorageConfigError

_PREFIX = "sv1:"
_WEAK_KEYS = {
    "",
    "change-me",
    "changeme",
    "secret",
    "streamvault",
    # Public placeholders from .env.example / INSTALL.md must never become a
    # production encryption key, even if an operator forgets to replace them.
    "change-this-to-a-very-secure-random-secret-key-32-chars-minimum",
    "generate_a_random_32_character_string_here",
    "generate_another_random_32_character_string",
}


def is_weak_secret(value) -> bool:
    """Return whether a session/encryption secret is empty or a known placeholder."""
    raw = str(value or "").strip()
    return raw.lower() in _WEAK_KEYS or len(raw) < 16


def _secret_from_file() -> str:
    """Use the WebUI-generated secret file when no env secret is configured."""
    path = os.path.join(getattr(config, "META_DIR", ""), "secret_key.txt")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _secret() -> str:
    # An explicitly configured dedicated key is authoritative.  Falling back
    # to another source would silently make stored credentials depend on a
    # different key than the operator expects.
    dedicated = os.environ.get("STORAGE_ENCRYPTION_KEY", "").strip()
    if dedicated:
        return dedicated

    raw = (
        os.environ.get("VOD_SECRET_KEY", "").strip()
        or str(getattr(config, "SECRET_KEY", "") or "").strip()
    )
    if is_weak_secret(raw):
        raw = _secret_from_file()
    return raw


def _key() -> bytes:
    raw = _secret()
    if is_weak_secret(raw):
        raise StorageConfigError(
            "Für die verschlüsselte Speicherung von Zugangsdaten muss "
            "VOD_SECRET_KEY (oder STORAGE_ENCRYPTION_KEY) auf einen starken, "
            "einmaligen Wert gesetzt sein (mindestens 16 Zeichen)."
        )
    return hashlib.sha256(("streamvault-storage-v1:" + raw).encode("utf-8")).digest()


def encryption_ready() -> bool:
    try:
        _key()
        return True
    except StorageConfigError:
        return False


def signing_secret() -> str:
    """Raw secret used to sign short-lived OAuth state tokens."""
    raw = _secret()
    if is_weak_secret(raw):
        raise StorageConfigError(
            "Für OAuth-Status-Token muss ein starker VOD_SECRET_KEY gesetzt sein."
        )
    return raw


def encrypt_config(data: dict) -> str:
    plaintext = json.dumps(data or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    nonce = get_random_bytes(12)
    cipher = AES.new(_key(), AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    token = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")
    return _PREFIX + token


def decrypt_config(token: str) -> dict:
    raw = str(token or "").strip()
    if not raw:
        return {}
    if not raw.startswith(_PREFIX):
        raise StorageConfigError("Unbekanntes Format für gespeicherte Zugangsdaten.")
    try:
        payload = base64.urlsafe_b64decode(raw[len(_PREFIX):].encode("ascii"))
        nonce, tag, ciphertext = payload[:12], payload[12:28], payload[28:]
        cipher = AES.new(_key(), AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        data = json.loads(plaintext.decode("utf-8"))
    except StorageConfigError:
        raise
    except Exception as exc:
        raise StorageConfigError(
            "Zugangsdaten konnten nicht entschlüsselt werden. "
            "Wurde VOD_SECRET_KEY geändert?"
        ) from exc
    if not isinstance(data, dict):
        raise StorageConfigError("Ungültige gespeicherte Zugangsdaten.")
    return data
