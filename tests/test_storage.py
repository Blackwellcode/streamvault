"""Storage-layer regression tests.

These tests deliberately use only the standard library test runner so they can
run in a clean deployment checkout:

    python -m unittest discover -s tests -v

The environment is configured before importing ``core.config`` because that
module intentionally refuses to start without credentials and a strong secret.
"""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_INITIAL_BASE = tempfile.mkdtemp(prefix="streamvault-tests-")
os.environ["VOD_USER"] = "vaultadmin"
os.environ["VOD_PASS"] = "VaultTestPass123!"
os.environ["VOD_SECRET_KEY"] = "test-session-secret-key-0123456789"
os.environ["STORAGE_ENCRYPTION_KEY"] = "test-storage-encryption-key-0123456789"
os.environ["VOD_BASE_DIR"] = _INITIAL_BASE
os.environ["VOD_ALLOW_ENV_LOGIN"] = "1"
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"
os.environ.pop("REDIS_URL", None)
os.environ.pop("VOD_RATELIMIT_STORAGE_URI", None)
# WEBUI.webui starts a network-bound badge sync daemon thread on import. It
# writes into whichever BASE_DIR is active and races TemporaryDirectory cleanup
# (intermittent 'Directory not empty' teardown errors), so keep it off in tests.
os.environ["TWITCH_BADGE_AUTO_SYNC"] = "0"

from core import config, database  # noqa: E402
from core.storage import cli, http, service  # noqa: E402
from core.storage.base import (  # noqa: E402
    StorageConfigError,
    StorageError,
    iter_file_chunks,
    mask_error,
    mask_text,
    normalize_remote_path,
    remote_name,
    remote_parent,
)
from core.storage.crypto import (  # noqa: E402
    decrypt_config,
    encrypt_config,
    encryption_ready,
    is_weak_secret,
)
from core.storage.providers import dropbox as dropbox_module  # noqa: E402
from core.storage.providers import google_drive as google_module  # noqa: E402
from core.storage.providers import onedrive as onedrive_module  # noqa: E402
from core.storage.providers import s3 as s3_module  # noqa: E402
from core.storage.providers.dropbox import DropboxProvider  # noqa: E402
from core.storage.providers.google_drive import GoogleDriveProvider  # noqa: E402
from core.storage.providers.onedrive import OneDriveProvider  # noqa: E402
from core.storage.providers.s3 import S3Provider  # noqa: E402
from core.storage.providers.webdav import WebDAVProvider  # noqa: E402
from core.storage.registry import provider_catalog  # noqa: E402


class StorageTestBase(unittest.TestCase):
    """Isolated archive root and database for every test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="streamvault-storage-test-")
        self.base = Path(self._tmp.name)
        os.environ["VOD_BASE_DIR"] = str(self.base)
        self._patches = [
            mock.patch.object(config, "BASE_DIR", str(self.base)),
            mock.patch.object(config, "DB_FILE", str(self.base / "streamrecorder.db")),
            mock.patch.object(config, "FILES_DIR", str(self.base / "files")),
            mock.patch.object(config, "HLS_ROOT", str(self.base / ".hls")),
            mock.patch.object(config, "THUMB_ROOT", str(self.base / ".thumbs")),
            mock.patch.object(config, "META_DIR", str(self.base / ".meta")),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        os.makedirs(config.META_DIR, exist_ok=True)
        os.makedirs(config.FILES_DIR, exist_ok=True)
        database.init_db()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _provider(self, name="Test WebDAV", *, base_path="StreamVault", password="dav-secret-password"):
        return service.create_provider(
            name,
            "webdav",
            {
                "base_url": "https://dav.example.com/remote.php/dav/files/vault/",
                "username": "vault",
                "password": password,
                "base_path": base_path,
                "verify_ssl": "1",
            },
        )

    def _write(self, relative_path: str, data: bytes = b"data", mtime: float | None = None) -> Path:
        path = self.base / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def _complete_tasks(self):
        conn = database.get_db()
        try:
            conn.execute(
                "UPDATE tasks SET status='completed' WHERE status IN ('pending', 'running')"
            )
            conn.commit()
        finally:
            conn.close()

    def _uploaded_object(
        self,
        relative_path: str,
        data: bytes = b"payload",
        *,
        provider_name: str = "Test WebDAV",
    ):
        pid = self._provider(provider_name)
        path = self._write(relative_path, data)
        obj = service.enqueue_upload(relative_path)
        self._complete_tasks()
        database.update_storage_object(
            obj["id"],
            status="uploaded",
            remote_size=len(data),
            uploaded_at=time.time(),
        )
        return pid, path, database.get_storage_object(obj["id"])


class PathAndCryptoTests(StorageTestBase):
    def test_normalize_remote_path(self):
        self.assertEqual(normalize_remote_path(" /foo//bar/./baz "), "foo/bar/baz")
        self.assertEqual(normalize_remote_path(r"foo\bar\baz"), "foo/bar/baz")
        self.assertEqual(remote_parent("foo/bar/baz.mp4"), "foo/bar")
        self.assertEqual(remote_name("foo/bar/baz.mp4"), "baz.mp4")
        for value in ("", "   ", "/", "foo/../bar", "..", "foo/\x00bar", "foo/\x01bar"):
            with self.subTest(value=value):
                with self.assertRaises(StorageConfigError):
                    normalize_remote_path(value)

    def test_iter_file_chunks_inclusive_range(self):
        path = self._write("chunks.bin", b"0123456789")
        chunks = list(iter_file_chunks(str(path), start=2, end=5, chunk_size=2))
        self.assertEqual(chunks, [b"23", b"45"])

    def test_local_path_traversal_is_rejected(self):
        local = self._write("streamer/file.mp4")
        self.assertEqual(
            os.path.normcase(service.local_abs_path("streamer/file.mp4")),
            os.path.normcase(str(local.resolve())),
        )
        outside = self.base.parent / "outside.mp4"
        for value in ("../outside.mp4", str(outside), ""):
            with self.subTest(value=value):
                with self.assertRaises(StorageConfigError):
                    service.local_abs_path(value)

    def test_classify_local_path(self):
        self.assertEqual(service.classify_local_path("streamer/file.mp4"), ("streamer", "file.mp4", "vod"))
        self.assertEqual(
            service.classify_local_path("streamer/clips/clip.mp4"),
            ("streamer", "clip.mp4", "clip"),
        )
        self.assertEqual(
            service.classify_local_path("streamer/chat_logs/chat.jsonl"),
            ("streamer", "chat.jsonl", "chat"),
        )
        self.assertEqual(
            service.classify_local_path(".thumbs/streamer/thumb.jpg"),
            ("streamer", "thumb.jpg", "thumb"),
        )
        self.assertEqual(service.classify_local_path("loose-file.mp4"), ("", "loose-file.mp4", "other"))

    def test_encrypt_decrypt_roundtrip(self):
        payload = {"client_secret": "super-secret-value", "refresh_token": "refresh-abc"}
        token = encrypt_config(payload)
        self.assertTrue(token.startswith("sv1:"))
        self.assertNotIn("super-secret-value", token)
        self.assertEqual(decrypt_config(token), payload)

    def test_wrong_key_and_tampered_token_are_rejected(self):
        token = encrypt_config({"password": "hunter2"})
        with mock.patch.dict(
            os.environ, {"STORAGE_ENCRYPTION_KEY": "a-different-strong-key-0123456789"}
        ):
            with self.assertRaises(StorageConfigError):
                decrypt_config(token)

        raw = base64.urlsafe_b64decode(token[len("sv1:") :])
        tampered = raw[:-1] + bytes([raw[-1] ^ 0x01])
        bad_token = "sv1:" + base64.urlsafe_b64encode(tampered).decode("ascii")
        with self.assertRaises(StorageConfigError):
            decrypt_config(bad_token)

    def test_weak_secret_blocks_encryption(self):
        with mock.patch.dict(
            os.environ, {"STORAGE_ENCRYPTION_KEY": "", "VOD_SECRET_KEY": ""}
        ), mock.patch.object(config, "SECRET_KEY", "change-me"):
            self.assertFalse(encryption_ready())
            with self.assertRaises(StorageConfigError):
                encrypt_config({"password": "x"})

    def test_mask_text_redacts_credentials(self):
        text = mask_text(
            "failed access_token=supersecret&password=hunter2 "
            "Authorization: Bearer abc.def.ghi client_secret: shhh"
        )
        for secret in ("supersecret", "hunter2", "abc.def.ghi", "shhh"):
            self.assertNotIn(secret, text)
        self.assertIn("***", text)
        self.assertEqual(mask_error(ValueError("token=abc")), "token=***")
        self.assertLessEqual(len(mask_text("x" * 2000)), 500)

    def test_mask_text_redacts_quoted_json_credentials(self):
        text = mask_text(
            '{"access_token": "supersecret", "password": "hello world", '
            '"nested": {"refresh_token": "refresh-value"}}'
        )
        for secret in ("supersecret", "hello world", "refresh-value"):
            self.assertNotIn(secret, text)
        self.assertIn('"access_token": "***"', text)
        self.assertIn('"password": "***"', text)
        self.assertIn('"refresh_token": "***"', text)

    def test_webui_secret_key_rejects_public_placeholder(self):
        from WEBUI import webui

        placeholder = (
            "change-this-to-a-very-secure-random-secret-key-32-chars-minimum"
        )
        key_path = Path(config.META_DIR) / "secret_key.txt"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("existing-strong-secret-0123456789", encoding="utf-8")
        with mock.patch.object(config, "SECRET_KEY", placeholder):
            self.assertEqual(
                webui.load_secret_key(), "existing-strong-secret-0123456789"
            )

        key_path.unlink()
        with mock.patch.object(config, "SECRET_KEY", placeholder):
            generated = webui.load_secret_key()
        self.assertNotEqual(generated, placeholder)
        self.assertFalse(is_weak_secret(generated))
        self.assertEqual(key_path.read_text(encoding="utf-8").strip(), generated)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)


class ProviderConfigTests(StorageTestBase):
    def test_create_provider_encrypts_and_masks_secrets(self):
        pid = self._provider()
        raw = database.get_storage_provider(pid)
        self.assertTrue(raw["config"].startswith("sv1:"))
        self.assertNotIn("dav-secret-password", raw["config"])

        public = service.list_providers()[0]
        self.assertEqual(public["config"]["password"], "••••••••")
        self.assertTrue(public["config"]["password__set"])
        self.assertNotIn("dav-secret-password", json.dumps(public))
        self.assertEqual(service.get_settings()["default_provider_id"], pid)

        record = service.get_provider_record(pid)
        self.assertEqual(record["config"]["password"], "dav-secret-password")

    def test_update_preserves_masked_secret(self):
        pid = self._provider()
        service.update_provider(
            pid, values={"password": "••••••••", "base_path": "Archive"}
        )
        record = service.get_provider_record(pid)
        self.assertEqual(record["config"]["password"], "dav-secret-password")
        self.assertEqual(record["config"]["base_path"], "Archive")

        service.update_provider(pid, values={"password": "new-secret-value"})
        self.assertEqual(
            service.get_provider_record(pid)["config"]["password"], "new-secret-value"
        )

    def test_duplicate_and_invalid_provider_values_are_rejected(self):
        self._provider()
        with self.assertRaises(StorageConfigError):
            self._provider("test webdav")
        with self.assertRaises(StorageConfigError):
            service.create_provider("", "webdav", {})
        with self.assertRaises(StorageConfigError):
            service.create_provider("Unknown", "not-a-provider", {})
        with self.assertRaises(StorageConfigError):
            service.create_provider(
                "Bad URL",
                "webdav",
                {
                    "base_url": "ftp://example.com/",
                    "username": "u",
                    "password": "p",
                },
            )

    def test_provider_urls_reject_embedded_credentials_query_and_fragment(self):
        bad_webdav = (
            "https://user:secret@dav.example.com/root/",
            "https://dav.example.com/root/?token=secret",
            "https://dav.example.com/root/#fragment",
        )
        for index, base_url in enumerate(bad_webdav):
            with self.subTest(base_url=base_url), self.assertRaises(
                StorageConfigError
            ):
                service.create_provider(
                    f"Bad WebDAV {index}",
                    "webdav",
                    {
                        "base_url": base_url,
                        "username": "vault",
                        "password": "secret",
                    },
                )

        bad_s3 = (
            "https://user:secret@s3.example.com",
            "https://s3.example.com/?token=secret",
            "https://s3.example.com/#fragment",
        )
        for index, endpoint_url in enumerate(bad_s3):
            with self.subTest(endpoint_url=endpoint_url), self.assertRaises(
                StorageConfigError
            ):
                service.create_provider(
                    f"Bad S3 {index}",
                    "s3",
                    {
                        "bucket": "archive",
                        "access_key": "key",
                        "secret_key": "secret",
                        "endpoint_url": endpoint_url,
                    },
                )
        self.assertEqual(service.list_providers(), [])

    def test_google_oauth_provider_can_be_saved_before_link(self):
        pid = service.create_provider(
            "Google",
            "google_drive",
            {"client_id": "client-id", "client_secret": "client-secret"},
        )
        record = service.get_provider_record(pid)
        self.assertEqual(record["config"]["client_id"], "client-id")
        self.assertEqual(record["config"]["base_path"], "StreamVault")
        self.assertFalse(str(record["config"].get("refresh_token") or "").strip())

    def test_provider_type_change_blocked_while_objects_exist(self):
        pid = self._provider()
        self._write("streamer/file.mp4")
        service.prepare_object("streamer/file.mp4", service.resolve_provider(pid))
        with self.assertRaises(StorageConfigError):
            service.update_provider(pid, provider_type="s3")

    def test_delete_provider_removes_objects_and_selects_fallback(self):
        first = self._provider("First")
        second = self._provider("Second")
        self._write("streamer/file.mp4")
        obj = service.prepare_object("streamer/file.mp4", service.resolve_provider(first))
        self.assertTrue(service.delete_provider(first))
        self.assertIsNone(database.get_storage_object(obj["id"]))
        self.assertEqual(service.get_settings()["default_provider_id"], second)

    def test_settings_defaults_and_clamping(self):
        settings = service.get_settings()
        self.assertFalse(settings["auto_upload"])
        self.assertFalse(settings["delete_local_after_upload"])
        self.assertTrue(settings["verify_before_local_delete"])
        self.assertEqual(settings["local_retention_days"], 14)
        self.assertEqual(settings["max_sync_files"], 500)
        self.assertFalse(settings["include_chat_logs"])
        self.assertFalse(settings["include_thumbnails"])

        saved = service.save_settings(
            {
                "auto_upload": "1",
                "delete_local_after_upload": "yes",
                "local_retention_days": "-5",
                "max_sync_files": "99999",
                "auto_sync_interval_minutes": "0",
            }
        )
        self.assertTrue(saved["auto_upload"])
        self.assertTrue(saved["delete_local_after_upload"])
        self.assertEqual(saved["local_retention_days"], 0)
        self.assertEqual(saved["max_sync_files"], 5000)
        self.assertEqual(saved["auto_sync_interval_minutes"], 0)
        with self.assertRaises(StorageConfigError):
            service.save_settings({"default_provider_id": "999999"})


class MediaCollectionTests(StorageTestBase):
    def test_collection_respects_flags_and_skips(self):
        t = 1_700_000_000
        self._write("streamer/main.mp4", b"main", t)
        self._write("streamer/main.mp4.txt", b"sidecar", t + 1)
        self._write("streamer/clips/clip.mp4", b"clip", t + 2)
        self._write("streamer/chat_logs/chat.jsonl", b"{}", t + 3)
        self._write(".thumbs/streamer/thumb.jpg", b"jpg", t + 4)
        self._write("streamer/main.mp4.part", b"part", t + 5)
        self._write("streamer/main.remux.mp4", b"remux", t + 6)
        self._write("streamer/notes.pdf", b"pdf", t + 7)
        self._write("core/code.mp4", b"code", t + 8)
        self._write("streamer/.hidden/secret.mp4", b"hidden", t + 9)
        self._write("streamer/thumb.png", b"png", t + 10)

        files = service.collect_media_files()
        self.assertEqual(
            files,
            ["streamer/main.mp4", "streamer/main.mp4.txt", "streamer/clips/clip.mp4"],
        )
        self.assertEqual(service.collect_media_files(limit=1), ["streamer/main.mp4"])
        self.assertEqual(service.collect_media_files("missing"), [])
        with self.assertRaises(StorageConfigError):
            service.collect_media_files("../escape")

        settings = dict(service.DEFAULT_SETTINGS)
        settings["include_chat_logs"] = True
        settings["include_thumbnails"] = True
        with_optional = service.collect_media_files(settings=settings)
        self.assertIn("streamer/chat_logs/chat.jsonl", with_optional)
        self.assertIn(".thumbs/streamer/thumb.jpg", with_optional)
        self.assertNotIn("streamer/main.mp4.part", with_optional)
        self.assertNotIn("streamer/thumb.png", with_optional)

    def test_remote_path_for_uses_base_path_and_rejects_traversal(self):
        self.assertEqual(
            service.remote_path_for({"config": {"base_path": "StreamVault"}}, "streamer/file.mp4"),
            "StreamVault/streamer/file.mp4",
        )
        self.assertEqual(
            service.remote_path_for({"config": {"base_path": "/Archive/"}}, "streamer/file.mp4"),
            "Archive/streamer/file.mp4",
        )
        self.assertEqual(
            service.remote_path_for({"config": {}}, "streamer/file.mp4"),
            "streamer/file.mp4",
        )
        with self.assertRaises(StorageConfigError):
            service.remote_path_for({"config": {"base_path": "../escape"}}, "streamer/file.mp4")

    def test_prepare_object_classifies_and_builds_remote_path(self):
        pid = self._provider()
        self._write("streamer/clips/clip.mp4", b"clip")
        obj = service.prepare_object("streamer/clips/clip.mp4", service.resolve_provider(pid))
        self.assertEqual(obj["streamer"], "streamer")
        self.assertEqual(obj["filename"], "clip.mp4")
        self.assertEqual(obj["kind"], "clip")
        self.assertEqual(obj["remote_path"], "StreamVault/streamer/clips/clip.mp4")
        self.assertEqual(obj["status"], "pending")


class UploadBookkeepingTests(StorageTestBase):
    def test_enqueue_upload_change_detection(self):
        self._provider()
        self._write("streamer/file.mp4", b"one")
        first = service.enqueue_upload("streamer/file.mp4")
        self.assertTrue(first.get("task_id"))
        self.assertEqual(first["status"], "pending")
        self._complete_tasks()
        database.update_storage_object(
            first["id"], status="uploaded", remote_size=3, uploaded_at=time.time()
        )

        second = service.enqueue_upload("streamer/file.mp4")
        self.assertFalse(second.get("task_id"))
        self.assertEqual(second["status"], "uploaded")

        database.update_storage_object(first["id"], local_deleted_at=time.time())
        self._write("streamer/file.mp4", b"one-two", mtime=time.time() + 5)
        third = service.enqueue_upload("streamer/file.mp4")
        self.assertTrue(third.get("task_id"))
        self.assertEqual(third["status"], "pending")
        self.assertIsNone(third["local_deleted_at"])

    def test_enqueue_upload_base_path_change_requeues(self):
        pid = self._provider()
        self._write("streamer/file.mp4", b"payload")
        first = service.enqueue_upload("streamer/file.mp4")
        self._complete_tasks()
        database.update_storage_object(
            first["id"],
            status="uploaded",
            remote_size=7,
            uploaded_at=time.time(),
            local_deleted_at=time.time(),
        )

        service.update_provider(pid, values={"base_path": "Archive"})
        second = service.enqueue_upload("streamer/file.mp4")
        self.assertTrue(second.get("task_id"))
        self.assertEqual(second["status"], "pending")
        self.assertEqual(second["remote_path"], "Archive/streamer/file.mp4")
        self.assertIsNone(second["local_deleted_at"])

    def test_enqueue_upload_does_not_duplicate_active_task(self):
        self._provider()
        self._write("streamer/file.mp4", b"payload")
        first = service.enqueue_upload("streamer/file.mp4")
        self.assertTrue(first.get("task_id"))
        second = service.enqueue_upload("streamer/file.mp4")
        self.assertFalse(second.get("task_id"))
        conn = database.get_db()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE type='storage_upload'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_sync_fairness_filters_current_files_before_limit(self):
        pid = self._provider()
        start = 1_700_000_000
        for index in range(4):
            self._write(f"streamer/v{index}.mp4", f"data-{index}".encode(), start + index)

        for index in (0, 1):
            rel = f"streamer/v{index}.mp4"
            obj = service.enqueue_upload(rel)
            self._complete_tasks()
            database.update_storage_object(
                obj["id"],
                status="uploaded",
                remote_size=len(f"data-{index}"),
                uploaded_at=time.time(),
            )

        service.save_settings({"max_sync_files": 2})
        result = service.run_sync({"provider_id": pid})
        self.assertEqual(result["files"], 4)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["queued"], 2)

        objects = {
            row["local_path"]: row
            for row in database.list_storage_objects(provider_id=pid, limit=100)
        }
        self.assertEqual(objects["streamer/v0.mp4"]["status"], "uploaded")
        self.assertEqual(objects["streamer/v1.mp4"]["status"], "uploaded")
        self.assertEqual(objects["streamer/v2.mp4"]["status"], "pending")
        self.assertEqual(objects["streamer/v3.mp4"]["status"], "pending")

    def test_run_sync_requeues_current_file_after_base_path_change(self):
        pid = self._provider()
        path = self._write("streamer/file.mp4", b"payload")
        obj = database.upsert_storage_object(
            pid,
            "streamer/file.mp4",
            "StreamVault/streamer/file.mp4",
            streamer="streamer",
            filename="file.mp4",
            kind="vod",
            local_size=len(b"payload"),
            local_mtime=os.path.getmtime(path),
            status="uploaded",
        )
        database.update_storage_object(
            obj["id"], remote_size=len(b"payload"), uploaded_at=time.time()
        )
        service.update_provider(pid, values={"base_path": "Archive"})

        result = service.run_sync({"provider_id": pid})

        self.assertEqual(result["files"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["queued"], 1)
        stored = database.get_storage_object(obj["id"])
        self.assertEqual(stored["remote_path"], "Archive/streamer/file.mp4")
        self.assertEqual(stored["status"], "pending")
        self.assertTrue(
            database.has_active_task("storage_upload", object_id=obj["id"])
        )

    def test_run_sync_preserves_existing_schedule_fields(self):
        pid = self._provider()
        self._write("streamer/file.mp4", b"payload")
        database.set_system_state(
            service.SCHEDULE_KEY,
            {"last_cleanup_at": 111.0, "custom": "keep", "last_sync_at": 222.0},
        )

        result = service.run_sync({"provider_id": pid})

        self.assertEqual(result["queued"], 1)
        schedule = database.get_system_state(service.SCHEDULE_KEY, {})
        self.assertEqual(schedule["last_cleanup_at"], 111.0)
        self.assertEqual(schedule["custom"], "keep")
        self.assertGreater(schedule["last_sync_at"], 222.0)
        self.assertEqual(schedule["queued"], 1)

    def test_run_sync_records_inline_cleanup_without_losing_schedule(self):
        pid = self._provider()
        self._write("streamer/file.mp4", b"payload")
        service.save_settings(
            {"delete_local_after_upload": True, "local_retention_days": 0}
        )
        database.set_system_state(
            service.SCHEDULE_KEY, {"last_cleanup_at": 5.0, "custom": "keep"}
        )

        with mock.patch.object(
            service,
            "cleanup_local_copies",
            return_value={"checked": 0, "deleted": 0, "kept": 0},
        ) as cleanup:
            service.run_sync({"provider_id": pid})

        cleanup.assert_called_once_with(pid)
        schedule = database.get_system_state(service.SCHEDULE_KEY, {})
        self.assertEqual(schedule["custom"], "keep")
        self.assertGreater(schedule["last_cleanup_at"], 5.0)

    def test_upsert_preserves_local_metadata_and_status(self):
        pid = self._provider()
        database.upsert_storage_object(
            pid,
            "streamer/file.mp4",
            "old/streamer/file.mp4",
            local_size=10,
            local_mtime=123.0,
            status="uploaded",
        )
        updated = database.upsert_storage_object(
            pid,
            "streamer/file.mp4",
            "new/streamer/file.mp4",
            local_size=0,
            local_mtime=0,
            status="pending",
        )
        self.assertEqual(updated["local_size"], 10)
        self.assertEqual(updated["local_mtime"], 123.0)
        self.assertEqual(updated["status"], "uploaded")
        self.assertEqual(updated["remote_path"], "new/streamer/file.mp4")

    def test_local_deleted_filter_and_task_object_filter(self):
        pid = self._provider()
        first = database.upsert_storage_object(pid, "a.mp4", "a.mp4", status="uploaded")
        second = database.upsert_storage_object(pid, "b.mp4", "b.mp4", status="uploaded")
        database.update_storage_object(second["id"], local_deleted_at=time.time())

        self.assertEqual(len(database.list_storage_objects(provider_id=pid)), 2)
        self.assertEqual(
            [row["id"] for row in database.list_storage_objects(provider_id=pid, local_deleted=False)],
            [first["id"]],
        )
        self.assertEqual(
            [row["id"] for row in database.list_storage_objects(provider_id=pid, local_deleted=True)],
            [second["id"]],
        )

        task_id = database.add_task(
            "storage_upload",
            {"object_id": first["id"], "streamer": "streamer", "filename": "a.mp4"},
        )
        self.assertTrue(task_id)
        self.assertTrue(database.has_active_task("storage_upload", object_id=first["id"]))
        self.assertFalse(database.has_active_task("storage_upload", object_id=second["id"]))
        self.assertTrue(
            database.has_active_task("storage_upload", "streamer", "a.mp4")
        )

    def test_sync_dedupe_is_provider_scoped(self):
        first = self._provider("First")
        second = self._provider("Second")
        first_task = service.enqueue_sync(first, "streamer")
        second_task = service.enqueue_sync(second, "streamer")
        self.assertTrue(first_task)
        self.assertTrue(second_task)
        self.assertIsNone(service.enqueue_sync(first, "streamer"))
        self.assertTrue(
            database.has_active_task(
                "storage_sync", "streamer", None, provider_id=first
            )
        )
        self.assertTrue(
            database.has_active_task(
                "storage_sync", "streamer", None, provider_id=second
            )
        )

    def test_auto_upload_helper_respects_setting(self):
        self._provider()
        path = self._write("streamer/auto.mp4", b"auto")
        self.assertIsNone(service.enqueue_auto_upload(str(path)))
        service.save_settings({"auto_upload": True})
        obj = service.enqueue_auto_upload(str(path))
        self.assertIsNotNone(obj)
        self.assertTrue(obj.get("task_id"))
        self.assertEqual(obj["status"], "pending")


class WorkerStorageHookTests(StorageTestBase):
    def _worker(self):
        import sys

        module = sys.modules.get("recorder.worker")
        if module is not None:
            return module
        with mock.patch.dict(sys.modules, {"numpy": mock.Mock()}):
            from recorder import worker

        return worker

    def test_successful_remux_enqueues_auto_upload(self):
        worker = self._worker()
        pid = self._provider()
        service.save_settings({"auto_upload": True})
        path = self._write("streamer/file.mp4", b"payload")

        with mock.patch.object(
            worker, "_safe_vod_task_path", return_value=str(path)
        ), mock.patch.object(
            worker, "_is_current_recording", return_value=False
        ), mock.patch.object(
            worker, "_is_recent", return_value=False
        ), mock.patch.object(
            worker, "_remux_file", return_value=(True, "")
        ), mock.patch.object(
            worker, "enqueue_thumbnail_if_missing"
        ):
            result = worker.process_remux_vod(
                {"streamer": "streamer", "filename": "file.mp4"}
            )

        self.assertTrue(result)
        obj = database.find_storage_object(pid, "streamer/file.mp4")
        self.assertIsNotNone(obj)
        self.assertEqual(obj["status"], "pending")
        self.assertTrue(
            database.has_active_task("storage_upload", object_id=obj["id"])
        )


class RetentionTests(StorageTestBase):
    class _FakeProvider:
        def __init__(self, exists: bool = True):
            self.exists_result = exists
            self.exists_calls = 0
            self.deleted = []

        def exists(self, remote_path, expected_size=0):
            self.exists_calls += 1
            return self.exists_result

        def delete(self, remote_path):
            self.deleted.append(remote_path)

    def test_delete_local_copy_requires_uploaded_status(self):
        self._provider()
        self._write("streamer/file.mp4", b"payload")
        obj = service.enqueue_upload("streamer/file.mp4")
        ok, message = service.delete_local_copy(obj["id"])
        self.assertFalse(ok)
        self.assertIn("hochgeladen", message)
        self.assertTrue((self.base / "streamer/file.mp4").exists())
        self.assertFalse(service.delete_local_copy(999999)[0])

    def test_delete_local_copy_keeps_file_when_remote_unverified(self):
        _, path, obj = self._uploaded_object("streamer/file.mp4")
        fake = self._FakeProvider(exists=False)
        with mock.patch.object(service, "build_provider", return_value=fake):
            ok, message = service.delete_local_copy(obj["id"])
        self.assertFalse(ok)
        self.assertIn("verifiziert", message)
        self.assertTrue(path.exists())
        self.assertEqual(fake.exists_calls, 1)
        self.assertIn("verifiziert", database.get_storage_object(obj["id"])["last_error"])

        with mock.patch.object(service, "build_provider", return_value=fake):
            ok, _ = service.delete_local_copy(obj["id"], verify=False)
        self.assertTrue(ok)
        self.assertFalse(path.exists())
        self.assertIsNotNone(database.get_storage_object(obj["id"])["local_deleted_at"])

    def test_delete_local_copy_refuses_changed_local_file(self):
        _, path, obj = self._uploaded_object("streamer/file.mp4")
        path.write_bytes(b"changed-payload")
        fake = self._FakeProvider(exists=True)
        with mock.patch.object(service, "build_provider", return_value=fake):
            ok, message = service.delete_local_copy(obj["id"])
        self.assertFalse(ok)
        self.assertIn("geändert", message)
        self.assertTrue(path.exists())
        stored = database.get_storage_object(obj["id"])
        self.assertIsNone(stored["local_deleted_at"])
        self.assertIn("geändert", stored["last_error"])

    def test_delete_local_copy_respects_active_watcher(self):
        _, path, obj = self._uploaded_object("streamer/file.mp4")
        fake = self._FakeProvider(exists=True)
        with mock.patch(
            "core.vod_watch_slots.is_vod_watched", return_value=True
        ), mock.patch.object(service, "build_provider", return_value=fake):
            ok, message = service.delete_local_copy(obj["id"])
        self.assertFalse(ok)
        self.assertIn("angesehen", message)
        self.assertTrue(path.exists())

    def test_cleanup_disabled_by_default(self):
        self._provider()
        result = service.cleanup_local_copies()
        self.assertTrue(result["disabled"])
        self.assertEqual(result["deleted"], 0)

    def test_cleanup_deletes_after_retention_when_enabled(self):
        _, path, obj = self._uploaded_object("streamer/file.mp4")
        service.save_settings(
            {"delete_local_after_upload": True, "local_retention_days": 0}
        )
        fake = self._FakeProvider(exists=True)
        with mock.patch.object(service, "build_provider", return_value=fake):
            result = service.cleanup_local_copies(limit=10)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(path.exists())
        self.assertIsNotNone(database.get_storage_object(obj["id"])["local_deleted_at"])

    def test_cleanup_continues_after_per_object_failure(self):
        _, _, first = self._uploaded_object("streamer/first.mp4", b"first")
        _, _, second = self._uploaded_object(
            "streamer/second.mp4", b"second", provider_name="Second WebDAV"
        )
        service.save_settings(
            {"delete_local_after_upload": True, "local_retention_days": 0}
        )
        seen = []

        def fake_delete(object_id):
            seen.append(object_id)
            if object_id == first["id"]:
                raise RuntimeError("provider exploded")
            return True, "entfernt"

        with mock.patch.object(
            service, "delete_local_copy", side_effect=fake_delete
        ):
            result = service.cleanup_local_copies(limit=10)

        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["kept"], 1)
        self.assertEqual(sorted(seen), sorted([first["id"], second["id"]]))
        self.assertTrue(
            any(
                "first.mp4" in reason and "provider exploded" in reason
                for reason in result["reasons"]
            )
        )


class ProviderBehaviourTests(StorageTestBase):
    def test_catalog_contains_all_supported_providers(self):
        catalog = provider_catalog()
        self.assertEqual(
            {entry["type_id"] for entry in catalog},
            {"google_drive", "s3", "dropbox", "onedrive", "webdav"},
        )
        for entry in catalog:
            self.assertTrue(entry["label"])
            self.assertTrue(entry["fields"])

    def test_s3_missing_dependency_is_explained(self):
        provider = S3Provider(
            {"bucket": "archive", "access_key": "key", "secret_key": "secret"}
        )
        with mock.patch.object(s3_module, "boto3", None):
            with self.assertRaises(StorageConfigError) as ctx:
                provider._client()
        self.assertIn("boto3", str(ctx.exception))

    def test_google_drive_root_and_folder_resolution(self):
        self.assertEqual(GoogleDriveProvider({})._root_folder_id(), "root")
        self.assertEqual(
            GoogleDriveProvider({"folder_id": "folder-123"})._root_folder_id(),
            "folder-123",
        )

        created = []
        provider = GoogleDriveProvider({})
        provider._find_child = lambda parent, name, folder_only=False: None
        provider._create_folder = lambda parent, name: (
            created.append((parent, name)) or f"id-{name}"
        )
        parent = provider._folder_for_parent("StreamVault/streamer/file.mp4")
        self.assertEqual(parent, "id-streamer")
        self.assertEqual(
            created, [("root", "StreamVault"), ("id-StreamVault", "streamer")]
        )

    def test_google_drive_zero_byte_upload_uses_multipart(self):
        empty = self._write("empty.mp4", b"")
        provider = GoogleDriveProvider(
            {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}
        )
        provider._token = lambda force=False: "token"
        provider._folder_for_parent = lambda remote_path, create=True: "parent-id"
        provider._find_child = lambda parent, name, folder_only=False: None

        calls = []

        class Response:
            status_code = 200
            content = b"{}"

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response()

        with mock.patch.object(google_module.http, "request", fake_request), mock.patch.object(
            google_module.http,
            "json_response",
            lambda response, action, provider=None: {
                "id": "file-1",
                "size": "0",
                "md5Checksum": "",
                "modifiedTime": "",
            },
        ):
            result = provider.upload_file(str(empty), "StreamVault/streamer/empty.mp4")

        self.assertEqual(result["remote_id"], "file-1")
        self.assertEqual(result["size"], 0)
        self.assertEqual(len(calls), 1)
        method, url, kwargs = calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/files"))
        self.assertEqual(kwargs["params"]["uploadType"], "multipart")
        self.assertTrue(kwargs["headers"]["Content-Type"].startswith("multipart/related"))

    def test_google_drive_upload_401_retry_is_bounded(self):
        class Unauthorized:
            status_code = 401
            reason = "Unauthorized"
            text = "denied"
            content = b""
            headers = {}

            def json(self):
                raise ValueError("not json")

            def close(self):
                pass

        empty = self._write("empty.mp4", b"")
        provider = GoogleDriveProvider({})
        token_calls = []
        provider._token = lambda force=False: token_calls.append(bool(force)) or "token"
        provider._folder_for_parent = lambda remote_path, create=True: "parent-id"
        provider._find_child = lambda parent, name, folder_only=False: None
        requests = []

        def fake_request(method, url, **kwargs):
            requests.append((method, url))
            return Unauthorized()

        with mock.patch.object(google_module.http, "request", fake_request):
            with self.assertRaises(StorageError):
                provider.upload_file(str(empty), "StreamVault/streamer/empty.mp4")

        self.assertEqual(len(requests), 2)
        self.assertEqual(token_calls, [False, True])

    def test_google_drive_chunk_401_retry_is_bounded(self):
        class Initiated:
            status_code = 200
            reason = "OK"
            text = ""
            content = b""
            headers = {"Location": "https://upload.example/session"}

            def json(self):
                return {}

            def close(self):
                pass

        class Unauthorized:
            status_code = 401
            reason = "Unauthorized"
            text = "denied"
            content = b""
            headers = {}

            def json(self):
                raise ValueError("not json")

            def close(self):
                pass

        local = self._write("clip.mp4", b"x")
        provider = GoogleDriveProvider({})
        token_calls = []
        provider._token = lambda force=False: token_calls.append(bool(force)) or "token"
        provider._folder_for_parent = lambda remote_path, create=True: "parent-id"
        provider._find_child = lambda parent, name, folder_only=False: None
        requests = []
        responses = [Initiated(), Unauthorized(), Initiated(), Unauthorized()]

        def fake_request(method, url, **kwargs):
            requests.append((method, url))
            return responses.pop(0)

        with mock.patch.object(google_module.http, "request", fake_request):
            with self.assertRaises(StorageError):
                provider.upload_file(str(local), "StreamVault/streamer/clip.mp4")

        self.assertEqual(len(requests), 4)
        # One forced refresh is enough; the repeated 401 must not recurse again.
        self.assertEqual(token_calls, [False, True, False])

    def test_google_drive_list_pagination(self):
        provider = GoogleDriveProvider({"base_path": "StreamVault"})
        provider._folder_for_parent = lambda remote_path, create=True: "parent-id"
        pages = [
            {"files": [{"id": "1", "name": "a.mp4", "size": "1"}], "nextPageToken": "next"},
            {"files": [{"id": "2", "name": "b.mp4", "size": "2"}]},
        ]
        seen = []

        def fake_api(method, path, **kwargs):
            seen.append(dict(kwargs.get("params") or {}))
            response = mock.Mock()
            response.page = pages[len(seen) - 1]
            return response

        with mock.patch.object(provider, "_api", fake_api), mock.patch.object(
            google_module.http,
            "json_response",
            lambda response, action, provider=None: response.page,
        ):
            items = provider.list("StreamVault/streamer")
        self.assertEqual([item.remote_id for item in items], ["1", "2"])
        self.assertEqual(seen[1]["pageToken"], "next")

    def test_google_drive_caches_folders_but_not_file_metadata(self):
        provider = GoogleDriveProvider({})
        calls = []
        payloads = [
            {
                "files": [
                    {
                        "id": "folder-1",
                        "name": "StreamVault",
                        "mimeType": google_module.FOLDER_MIME,
                    }
                ]
            },
            {"files": [{"id": "file-1", "name": "a.mp4", "size": "5"}]},
            {"files": [{"id": "file-1", "name": "a.mp4", "size": "6"}]},
        ]

        def fake_api(method, path, **kwargs):
            calls.append(kwargs.get("params") or {})
            response = mock.Mock()
            response.payload = payloads[len(calls) - 1]
            return response

        with mock.patch.object(provider, "_api", fake_api), mock.patch.object(
            google_module.http,
            "json_response",
            lambda response, action, provider=None: response.payload,
        ):
            first_folder = provider._find_child(
                "root", "StreamVault", folder_only=True
            )
            cached_folder = provider._find_child(
                "root", "StreamVault", folder_only=True
            )
            first_file = provider._find_child("root", "a.mp4")
            refreshed_file = provider._find_child("root", "a.mp4")

        self.assertEqual(first_folder["id"], "folder-1")
        self.assertEqual(cached_folder["id"], "folder-1")
        self.assertEqual(len(calls), 3)
        self.assertIn(
            f"mimeType = '{google_module.FOLDER_MIME}'", calls[0]["q"]
        )
        self.assertIn(
            f"mimeType != '{google_module.FOLDER_MIME}'", calls[1]["q"]
        )
        self.assertIn(
            f"mimeType != '{google_module.FOLDER_MIME}'", calls[2]["q"]
        )
        self.assertEqual(first_file["size"], "5")
        self.assertEqual(refreshed_file["size"], "6")
        self.assertEqual(
            provider._folder_cache[("root", "d:StreamVault")]["id"], "folder-1"
        )
        self.assertNotIn(("root", "f:a.mp4"), provider._folder_cache)

    def test_onedrive_folder_creation_uses_existing_parent_urls(self):
        provider = OneDriveProvider(
            {
                "client_id": "id",
                "client_secret": "secret",
                "refresh_token": "refresh",
            }
        )
        calls = []
        statuses = [201, 409, 201]

        class Response:
            def __init__(self, status_code):
                self.status_code = status_code

        def fake_api(method, url, **kwargs):
            calls.append((method, url, kwargs.get("json") or {}))
            return Response(statuses[len(calls) - 1])

        with mock.patch.object(provider, "_api", fake_api):
            provider._ensure_dirs("StreamVault/streamer/clips/file.mp4")

        self.assertEqual(
            [url for _, url, _ in calls],
            [
                f"{onedrive_module.GRAPH}/me/drive/root/children",
                f"{onedrive_module.GRAPH}/me/drive/root:/StreamVault:/children",
                f"{onedrive_module.GRAPH}/me/drive/root:/StreamVault/streamer:/children",
            ],
        )
        self.assertEqual(
            [body["name"] for _, _, body in calls],
            ["StreamVault", "streamer", "clips"],
        )
        self.assertEqual(
            [body["@microsoft.graph.conflictBehavior"] for _, _, body in calls],
            ["fail", "fail", "fail"],
        )

    def test_onedrive_list_follows_next_link(self):
        provider = OneDriveProvider(
            {
                "client_id": "id",
                "client_secret": "secret",
                "refresh_token": "refresh",
            }
        )
        pages = [
            {
                "value": [{"id": "1", "name": "a.mp4", "size": 1}],
                "@odata.nextLink": "https://graph.example/next",
            },
            {"value": [{"id": "2", "name": "b.mp4", "size": 2}]},
        ]
        seen = []

        def fake_api(method, url, **kwargs):
            seen.append((url, kwargs.get("params")))
            response = mock.Mock()
            response.page = pages[len(seen) - 1]
            return response

        with mock.patch.object(provider, "_api", fake_api), mock.patch.object(
            onedrive_module.http,
            "json_response",
            lambda response, action, provider=None: response.page,
        ):
            items = provider.list("StreamVault/streamer")
        self.assertEqual([item.remote_id for item in items], ["1", "2"])
        self.assertEqual(seen[1][0], "https://graph.example/next")
        self.assertIsNone(seen[1][1])

    def test_dropbox_upload_creates_missing_parent_folders(self):
        provider = DropboxProvider({"access_token": "token"})
        existing = {"/StreamVault"}
        created = []

        def fake_rpc(endpoint, payload, action):
            path = payload.get("path")
            if endpoint == "/files/get_metadata":
                if path in existing:
                    return {".tag": "folder"}
                raise StorageError("not found", status_code=409)
            if endpoint == "/files/create_folder_v2":
                existing.add(path)
                created.append(path)
                return {".tag": "folder"}
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        provider._rpc = fake_rpc
        provider._ensure_parent_folders("StreamVault/streamer/clips/file.mp4")
        self.assertEqual(
            created,
            ["/StreamVault/streamer", "/StreamVault/streamer/clips"],
        )

    def test_dropbox_upload_ensures_parent_folder_before_transfer(self):
        path = self._write("dropbox.mp4", b"data")
        provider = DropboxProvider({"access_token": "token"})
        provider._ensure_parent_folders = mock.Mock()

        class Response:
            status_code = 200

        with mock.patch.object(provider, "_content", return_value=Response()), mock.patch.object(
            dropbox_module.http,
            "json_response",
            return_value={"id": "file-1", "size": 4},
        ):
            result = provider.upload_file(
                str(path), "StreamVault/streamer/dropbox.mp4"
            )
        provider._ensure_parent_folders.assert_called_once_with(
            "/StreamVault/streamer/dropbox.mp4"
        )
        self.assertEqual(result["size"], 4)

    def test_dropbox_range_falls_back_when_server_ignores_range(self):
        provider = DropboxProvider({"access_token": "token"})

        class Response:
            status_code = 200

            def iter_content(self, chunk_size):
                yield b"01234"
                yield b"56789"

            def close(self):
                pass

        provider._download = lambda remote_path, range_header="": Response()
        self.assertEqual(
            b"".join(provider.open_range("StreamVault/streamer/file.mp4", 3, 7)),
            b"34567",
        )

    def test_webdav_url_quotes_path_segments(self):
        provider = WebDAVProvider(
            {
                "base_url": "https://dav.example.com/root/",
                "username": "u",
                "password": "p",
            }
        )
        self.assertEqual(
            provider._url("folder/a b#c.mp4"),
            "https://dav.example.com/root/folder/a%20b%23c.mp4",
        )


class ErrorHandlingTests(StorageTestBase):
    def test_test_provider_sanitizes_remote_errors(self):
        pid = self._provider()

        class BrokenProvider:
            def test_connection(self):
                raise StorageError(
                    "authentication failed access_token=supersecret password=hunter2",
                    status_code=401,
                )

        with mock.patch.object(service, "build_provider", return_value=BrokenProvider()):
            result = service.test_provider(pid)
        self.assertFalse(result["ok"])
        self.assertIn("HTTP 401", result["message"])
        self.assertNotIn("supersecret", result["message"])
        self.assertNotIn("hunter2", result["message"])
        row = database.get_storage_provider(pid)
        self.assertNotIn("supersecret", row["last_check_message"])
        self.assertEqual(row["last_check_ok"], False)

    def test_run_upload_marks_object_uploaded(self):
        self._provider()
        self._write("streamer/empty.mp4", b"")
        obj = service.enqueue_upload("streamer/empty.mp4")
        self._complete_tasks()

        class FakeUploadProvider:
            def upload_file(self, local_path, remote_path, *, progress=None):
                if progress:
                    progress(0, 0)
                return {"size": 0, "modified_at": "now"}

        with mock.patch.object(service, "build_provider", return_value=FakeUploadProvider()):
            result = service.run_upload({"object_id": obj["id"]})
        self.assertTrue(result["ok"])
        stored = database.get_storage_object(obj["id"])
        self.assertEqual(stored["status"], "uploaded")
        self.assertEqual(stored["remote_size"], 0)
        self.assertEqual(stored["progress"], 100)

    def test_run_upload_sanitizes_failure_and_persists_status(self):
        self._provider()
        self._write("streamer/file.mp4", b"payload")
        obj = service.enqueue_upload("streamer/file.mp4")
        self._complete_tasks()

        class BrokenUploadProvider:
            def upload_file(self, local_path, remote_path, *, progress=None):
                raise StorageError(
                    "upload failed access_token=supersecret", status_code=500
                )

        with mock.patch.object(service, "build_provider", return_value=BrokenUploadProvider()):
            with self.assertRaises(StorageError):
                service.run_upload({"object_id": obj["id"]})
        stored = database.get_storage_object(obj["id"])
        self.assertEqual(stored["status"], "failed")
        self.assertIn("HTTP 500", stored["last_error"])
        self.assertNotIn("supersecret", stored["last_error"])
        self.assertNotIn("supersecret", database.get_storage_provider(stored["provider_id"])["last_error"])


class ProviderDefaultsTests(StorageTestBase):
    def test_explicit_empty_base_path_is_not_replaced_by_default(self):
        pid = service.create_provider(
            "Empty Root",
            "webdav",
            {
                "base_url": "https://dav.example.com/root/",
                "username": "vault",
                "password": "secret",
                "base_path": "",
            },
        )
        record = service.get_provider_record(pid)
        self.assertEqual(record["config"]["base_path"], "")
        self.assertEqual(
            service.remote_path_for(record, "streamer/file.mp4"),
            "streamer/file.mp4",
        )

        service.update_provider(pid, values={"base_path": "Archive"})
        self.assertEqual(
            service.get_provider_record(pid)["config"]["base_path"], "Archive"
        )
        service.update_provider(pid, values={"base_path": "   "})
        self.assertEqual(service.get_provider_record(pid)["config"]["base_path"], "")

    def test_invalid_base_path_is_rejected_at_save_time(self):
        pid = self._provider()
        with self.assertRaises(StorageConfigError):
            service.update_provider(pid, values={"base_path": "../escape"})
        self.assertEqual(
            service.get_provider_record(pid)["config"]["base_path"], "StreamVault"
        )
        with self.assertRaises(StorageConfigError):
            service.create_provider(
                "Traversal",
                "webdav",
                {
                    "base_url": "https://dav.example.com/root/",
                    "username": "vault",
                    "password": "secret",
                    "base_path": "foo/../bar",
                },
            )


class PrepareObjectHardeningTests(StorageTestBase):
    def test_prepare_object_rejects_non_archive_paths(self):
        pid = self._provider()
        provider = service.resolve_provider(pid)
        rejected = (
            ".meta/secret.mp4",
            "core/code.mp4",
            "bin/tool.mp4",
            "streamer/main.mp4.part",
            "streamer/main.tmp",
            "streamer/chat_logs/chat.jsonl",
            ".thumbs/streamer/thumb.jpg",
            "streamer/.hidden/secret.mp4",
            "loose.mp4",
        )
        for rel in rejected:
            self._write(rel)
            with self.subTest(path=rel), self.assertRaises(StorageConfigError):
                service.prepare_object(rel, provider)

        settings = dict(service.DEFAULT_SETTINGS)
        settings["include_chat_logs"] = True
        settings["include_thumbnails"] = True
        self.assertEqual(
            service.prepare_object(
                "streamer/chat_logs/chat.jsonl", provider, settings=settings
            )["kind"],
            "chat",
        )
        self.assertEqual(
            service.prepare_object(
                ".thumbs/streamer/thumb.jpg", provider, settings=settings
            )["kind"],
            "thumb",
        )

    def test_collect_media_files_rejects_unsafe_streamer_names(self):
        for value in ("../escape", "foo/bar", "foo\\bar", "C:foo", ".", "..", "bad\x01name"):
            with self.subTest(value=value), self.assertRaises(StorageConfigError):
                service.collect_media_files(value)

    def test_collect_media_files_skips_symlinks_and_vanishing_files(self):
        self._write("streamer/real.mp4", b"real")
        with tempfile.TemporaryDirectory(prefix="streamvault-outside-") as outside_dir:
            outside = Path(outside_dir) / "outside.mp4"
            outside.write_bytes(b"outside")
            link = self.base / "streamer" / "link.mp4"
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks are not available on this platform")
            self.assertNotIn("streamer/link.mp4", service.collect_media_files())

        self._write("streamer/vanish.mp4", b"vanish")
        real_stat = os.stat

        def flaky_stat(path, *args, **kwargs):
            if os.path.normcase(str(path)).endswith(os.path.normcase("vanish.mp4")):
                raise FileNotFoundError(str(path))
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(service.os, "stat", side_effect=flaky_stat):
            files = service.collect_media_files()
        self.assertIn("streamer/real.mp4", files)
        self.assertNotIn("streamer/vanish.mp4", files)


class RemoteDeleteSafetyTests(StorageTestBase):
    class _FakeDeleteProvider:
        def __init__(self):
            self.deleted = []

        def delete(self, remote_path):
            self.deleted.append(remote_path)

    def test_enqueue_delete_blocks_unsafe_objects_and_dedupes(self):
        _, path, obj = self._uploaded_object("streamer/file.mp4", b"payload")
        database.add_task(
            "storage_upload",
            {
                "object_id": obj["id"],
                "streamer": "streamer",
                "filename": "file.mp4",
            },
        )
        with self.assertRaises(StorageConfigError) as ctx:
            service.enqueue_delete(obj["id"])
        self.assertIn("Upload", str(ctx.exception))
        self._complete_tasks()

        database.update_storage_object(obj["id"], local_deleted_at=time.time())
        with self.assertRaises(StorageConfigError) as ctx:
            service.enqueue_delete(obj["id"])
        self.assertIn("lokale Kopie", str(ctx.exception))
        database.update_storage_object(obj["id"], local_deleted_at=None)

        path.unlink()
        with self.assertRaises(StorageConfigError) as ctx:
            service.enqueue_delete(obj["id"])
        self.assertIn("fehlt", str(ctx.exception))

        path.write_bytes(b"different-size")
        with self.assertRaises(StorageConfigError) as ctx:
            service.enqueue_delete(obj["id"])
        self.assertIn("geändert", str(ctx.exception))

        # Simulate a fresh upload of the original content before deleting.
        path.write_bytes(b"payload")
        database.update_storage_object(
            obj["id"],
            local_size=len(b"payload"),
            local_mtime=os.path.getmtime(path),
        )
        first = service.enqueue_delete(obj["id"])
        self.assertTrue(first)
        self.assertEqual(service.enqueue_delete(obj["id"]), 0)

    def test_run_delete_refuses_missing_local_and_records_error(self):
        _, path, obj = self._uploaded_object("streamer/file.mp4", b"payload")
        path.unlink()
        fake = self._FakeDeleteProvider()
        with mock.patch.object(service, "build_provider", return_value=fake):
            with self.assertRaises(StorageConfigError) as ctx:
                service.run_delete({"object_id": obj["id"]})
        self.assertIn("fehlt", str(ctx.exception))
        self.assertEqual(fake.deleted, [])
        stored = database.get_storage_object(obj["id"])
        self.assertEqual(stored["status"], "uploaded")
        self.assertIn("fehlt", stored["last_error"])
        self.assertIsNone(stored["remote_deleted_at"])

    def test_run_delete_marks_remote_deleted_and_requeues_upload(self):
        _, _, obj = self._uploaded_object("streamer/file.mp4", b"payload")
        fake = self._FakeDeleteProvider()
        with mock.patch.object(service, "build_provider", return_value=fake):
            result = service.run_delete({"object_id": obj["id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(fake.deleted, ["StreamVault/streamer/file.mp4"])
        stored = database.get_storage_object(obj["id"])
        self.assertEqual(stored["status"], "remote_deleted")
        self.assertIsNotNone(stored["remote_deleted_at"])

        again = service.run_delete({"object_id": obj["id"]})
        self.assertTrue(again.get("already_deleted"))
        self.assertEqual(fake.deleted, ["StreamVault/streamer/file.mp4"])

        requeued = service.enqueue_upload("streamer/file.mp4")
        self.assertTrue(requeued.get("task_id"))
        self.assertEqual(requeued["status"], "pending")
        self.assertIsNone(requeued["remote_deleted_at"])

    def test_run_upload_clears_remote_deleted_marker(self):
        _, _, obj = self._uploaded_object("streamer/file.mp4", b"payload")
        database.update_storage_object(
            obj["id"], status="remote_deleted", remote_deleted_at=time.time()
        )
        observed = {}

        class InspectingProvider:
            def upload_file(self, local_path, remote_path, *, progress=None):
                row = database.get_storage_object(obj["id"])
                observed["status"] = row["status"]
                observed["remote_deleted_at"] = row["remote_deleted_at"]
                return {"size": len(b"payload")}

        with mock.patch.object(
            service, "build_provider", return_value=InspectingProvider()
        ):
            result = service.run_upload({"object_id": obj["id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(observed["status"], "uploading")
        self.assertIsNone(observed["remote_deleted_at"])
        stored = database.get_storage_object(obj["id"])
        self.assertEqual(stored["status"], "uploaded")
        self.assertIsNone(stored["remote_deleted_at"])

    def test_run_upload_rejects_provider_size_mismatch(self):
        _, _, obj = self._uploaded_object("streamer/file.mp4", b"payload")

        class ShortUploadProvider:
            def upload_file(self, local_path, remote_path, *, progress=None):
                return {"size": 0}

        with mock.patch.object(
            service, "build_provider", return_value=ShortUploadProvider()
        ):
            with self.assertRaises(StorageError) as ctx:
                service.run_upload({"object_id": obj["id"], "force": True})
        self.assertIn("unvollständig", str(ctx.exception))
        stored = database.get_storage_object(obj["id"])
        self.assertEqual(stored["status"], "failed")
        self.assertIn("unvollständig", stored["last_error"])
        provider = database.get_storage_provider(stored["provider_id"])
        self.assertIn("unvollständig", provider["last_error"])


class RestoreSafetyTests(StorageTestBase):
    class _FakeDownloadProvider:
        def __init__(self, data=b"", *, error=None, write_partial=False):
            self.data = data
            self.error = error
            self.write_partial = write_partial
            self.calls = 0
            self.remote_path = ""

        def download_file(self, remote_path, local_path, *, progress=None):
            self.calls += 1
            self.remote_path = remote_path
            if self.write_partial:
                Path(local_path).write_bytes(b"partial")
            if self.error:
                raise self.error
            Path(local_path).write_bytes(self.data)
            if progress:
                progress(len(self.data), len(self.data))
            return {"size": len(self.data)}

    def _restore_object(
        self,
        data=b"payload",
        *,
        remote_size=None,
        local=b"old-local",
        remote_deleted_at=None,
    ):
        pid = self._provider()
        path = self._write("streamer/file.mp4", local)
        obj = database.upsert_storage_object(
            pid,
            "streamer/file.mp4",
            "StreamVault/streamer/file.mp4",
            streamer="streamer",
            filename="file.mp4",
            kind="vod",
            local_size=len(local),
            local_mtime=os.path.getmtime(path),
            status="uploaded",
        )
        fields = {
            "remote_size": len(data) if remote_size is None else remote_size,
            "uploaded_at": time.time(),
        }
        if remote_deleted_at is not None:
            fields["remote_deleted_at"] = remote_deleted_at
        database.update_storage_object(obj["id"], **fields)
        return path, database.get_storage_object(obj["id"])

    def test_restore_unknown_remote_size_redownloads_existing_file(self):
        path, obj = self._restore_object(
            b"new-remote-data", remote_size=0, local=b"old-local"
        )
        fake = self._FakeDownloadProvider(b"new-remote-data")
        with mock.patch.object(service, "build_provider", return_value=fake):
            result = service.run_restore({"object_id": obj["id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(fake.calls, 1)
        self.assertEqual(path.read_bytes(), b"new-remote-data")
        stored = database.get_storage_object(obj["id"])
        self.assertEqual(stored["status"], "restored")
        self.assertEqual(stored["remote_size"], len(b"new-remote-data"))
        self.assertIsNone(stored["local_deleted_at"])

    def test_restore_trusts_matching_local_copy_without_download(self):
        path, obj = self._restore_object(b"payload", remote_size=7, local=b"payload")
        fake = self._FakeDownloadProvider(b"payload")
        with mock.patch.object(service, "build_provider", return_value=fake):
            result = service.run_restore({"object_id": obj["id"]})
        self.assertTrue(result.get("already_local"))
        self.assertEqual(fake.calls, 0)
        self.assertEqual(path.read_bytes(), b"payload")

    def test_restore_failure_preserves_local_copy_and_cleans_temp(self):
        path, obj = self._restore_object(b"new-data", remote_size=8, local=b"old-local")
        fake = self._FakeDownloadProvider(
            error=StorageError("download failed token=supersecret", status_code=500),
            write_partial=True,
        )
        with mock.patch.object(service, "build_provider", return_value=fake):
            with self.assertRaises(StorageError):
                service.run_restore({"object_id": obj["id"]})
        self.assertEqual(path.read_bytes(), b"old-local")
        self.assertFalse(Path(str(path) + ".restore").exists())
        stored = database.get_storage_object(obj["id"])
        self.assertEqual(stored["status"], "uploaded")
        self.assertIn("HTTP 500", stored["last_error"])
        self.assertNotIn("supersecret", stored["last_error"])

    def test_restore_size_mismatch_does_not_replace_local_copy(self):
        path, obj = self._restore_object(
            b"1234567890", remote_size=10, local=b"old-local"
        )
        fake = self._FakeDownloadProvider(b"short")
        with mock.patch.object(service, "build_provider", return_value=fake):
            with self.assertRaises(StorageError) as ctx:
                service.run_restore({"object_id": obj["id"]})
        self.assertIn("unvollständig", str(ctx.exception))
        self.assertEqual(path.read_bytes(), b"old-local")
        self.assertFalse(Path(str(path) + ".restore").exists())

    def test_restore_refuses_remote_deleted_object(self):
        _, obj = self._restore_object(remote_deleted_at=time.time())
        with mock.patch.object(service, "build_provider") as build_provider:
            with self.assertRaises(StorageConfigError) as ctx:
                service.run_restore({"object_id": obj["id"]})
        self.assertIn("gelöscht", str(ctx.exception))
        build_provider.assert_not_called()
        self.assertIn(
            "gelöscht", database.get_storage_object(obj["id"])["last_error"]
        )

    def test_enqueue_restore_dedupes_by_object_id(self):
        _, obj = self._restore_object()
        first = service.enqueue_restore(obj["id"])
        self.assertTrue(first)
        self.assertEqual(service.enqueue_restore(obj["id"]), 0)


class BookkeepingHardeningTests(StorageTestBase):
    def test_summary_excludes_remote_deleted_objects(self):
        pid = self._provider()
        first = database.upsert_storage_object(pid, "a.mp4", "a.mp4", status="uploaded")
        database.update_storage_object(first["id"], remote_size=10)
        second = database.upsert_storage_object(
            pid, "b.mp4", "b.mp4", status="remote_deleted"
        )
        database.update_storage_object(
            second["id"], remote_size=99, remote_deleted_at=time.time()
        )

        result = database.storage_object_summary(pid)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["bytes"], 10)
        self.assertIn("uploaded", result["statuses"])
        self.assertNotIn("remote_deleted", result["statuses"])
        self.assertEqual(
            database.storage_object_summary("not-a-provider"),
            {"total": 0, "bytes": 0, "statuses": {}},
        )


class CryptoAndHttpHardeningTests(StorageTestBase):
    def test_public_placeholder_dedicated_key_is_rejected(self):
        placeholder = (
            "change-this-to-a-very-secure-random-secret-key-32-chars-minimum"
        )
        with mock.patch.dict(
            os.environ,
            {
                "STORAGE_ENCRYPTION_KEY": placeholder,
                "VOD_SECRET_KEY": "strong-fallback-secret-0123456789",
            },
        ):
            self.assertFalse(encryption_ready())
            with self.assertRaises(StorageConfigError):
                encrypt_config({"password": "x"})

    def test_weak_env_secret_uses_strong_secret_file(self):
        secret_file = self.base / ".meta" / "secret_key.txt"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(
            "strong-generated-secret-0123456789", encoding="utf-8"
        )
        with mock.patch.dict(
            os.environ, {"STORAGE_ENCRYPTION_KEY": "", "VOD_SECRET_KEY": "change-me"}
        ), mock.patch.object(config, "SECRET_KEY", "change-me"):
            self.assertTrue(encryption_ready())
            token = encrypt_config({"password": "x"})
            self.assertEqual(decrypt_config(token), {"password": "x"})

    def test_http_errors_strip_urls_and_upload_tokens(self):
        text = http.sanitize_error_text(
            "POST https://content.dropboxapi.com/2/files/upload?token=supersecret failed"
        )
        self.assertIn("[URL]", text)
        self.assertNotIn("dropboxapi", text)
        self.assertNotIn("supersecret", text)

        response = mock.Mock()
        response.status_code = 500
        response.reason = "Server Error"
        response.json.side_effect = ValueError("no json")
        response.text = "<html>secret=supersecret</html>"
        self.assertEqual(
            http.error_text(response),
            "Antwort des Anbieters konnte nicht gelesen werden.",
        )

        response.json.side_effect = None
        response.json.return_value = {
            "error": {"message": "failed https://api.example/upload?token=abc"}
        }
        message = http.error_text(response)
        self.assertIn("[URL]", message)
        self.assertNotIn("api.example", message)
        self.assertNotIn("token=abc", message)

    def test_http_session_uses_wildcard_accept(self):
        previous = getattr(http._LOCAL, "session", None)
        http._LOCAL.session = None
        self.addCleanup(setattr, http._LOCAL, "session", previous)
        with mock.patch.object(http.requests, "Session") as session_cls:
            fake = mock.Mock()
            fake.headers = {}
            session_cls.return_value = fake
            current = http.session()
        self.assertIs(current, fake)
        self.assertEqual(fake.headers["Accept"], "*/*")

    def test_dropbox_api_arg_header_is_ascii_safe(self):
        provider = DropboxProvider({"access_token": "token"})
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(kwargs)
            return mock.Mock()

        with mock.patch.object(dropbox_module.http, "request", fake_request):
            provider._content(
                "/files/upload",
                {"path": "/StreamVault/ü.mp4"},
                data=b"x",
                action="Dropbox-Test",
            )
        raw = captured["headers"]["Dropbox-API-Arg"]
        self.assertTrue(raw.isascii())
        self.assertIn("\\u00fc", raw)


class ProviderExistsTests(StorageTestBase):
    def test_webdav_exists_uses_head_and_strict_size(self):
        provider = WebDAVProvider(
            {
                "base_url": "https://dav.example.com/root/",
                "username": "u",
                "password": "p",
            }
        )
        response = mock.Mock(status_code=200, headers={"Content-Length": "5"})
        with mock.patch.object(provider, "_request", return_value=response):
            self.assertTrue(provider.exists("a/b.mp4", 5))
            self.assertFalse(provider.exists("a/b.mp4", 6))
        response.headers = {"Content-Length": "0"}
        with mock.patch.object(provider, "_request", return_value=response):
            self.assertFalse(provider.exists("a/b.mp4", 5))
        not_found = mock.Mock(status_code=404, headers={})
        with mock.patch.object(provider, "_request", return_value=not_found):
            self.assertFalse(provider.exists("a/b.mp4", 5))

    def test_google_drive_exists_checks_size_and_missing_folder(self):
        provider = GoogleDriveProvider({"base_path": "StreamVault"})
        provider._folder_for_parent = lambda remote_path, create=True: "parent"
        provider._find_child = lambda parent, name, folder_only=False: {
            "id": "file",
            "size": "5",
        }
        self.assertTrue(provider.exists("StreamVault/a.mp4", 5))
        self.assertFalse(provider.exists("StreamVault/a.mp4", 6))

        provider._find_child = lambda parent, name, folder_only=False: {
            "id": "file",
            "size": "0",
        }
        self.assertFalse(provider.exists("StreamVault/a.mp4", 5))

        provider._folder_for_parent = lambda remote_path, create=True: None
        self.assertFalse(provider.exists("StreamVault/a.mp4", 5))

    def test_onedrive_exists_checks_size_and_not_found(self):
        provider = OneDriveProvider(
            {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}
        )
        response = mock.Mock(status_code=200)
        with mock.patch.object(provider, "_api", return_value=response), mock.patch.object(
            onedrive_module.http, "json_response", return_value={"size": 5}
        ):
            self.assertTrue(provider.exists("StreamVault/a.mp4", 5))
            self.assertFalse(provider.exists("StreamVault/a.mp4", 6))

        not_found = mock.Mock(status_code=404)
        with mock.patch.object(provider, "_api", return_value=not_found):
            self.assertFalse(provider.exists("StreamVault/a.mp4", 5))

    def test_dropbox_exists_checks_size_and_conflict(self):
        provider = DropboxProvider({"access_token": "token"})
        with mock.patch.object(provider, "_rpc", return_value={"size": 5}):
            self.assertTrue(provider.exists("StreamVault/a.mp4", 5))
            self.assertFalse(provider.exists("StreamVault/a.mp4", 6))

        with mock.patch.object(
            provider,
            "_rpc",
            side_effect=StorageError("not found", status_code=409),
        ):
            self.assertFalse(provider.exists("StreamVault/a.mp4", 5))

    def test_s3_exists_uses_head_content_length(self):
        provider = S3Provider(
            {"bucket": "archive", "access_key": "key", "secret_key": "secret"}
        )
        client = mock.Mock()
        client.head_object.return_value = {"ContentLength": 5}
        with mock.patch.object(provider, "_client", return_value=client):
            self.assertTrue(provider.exists("StreamVault/a.mp4", 5))
            self.assertFalse(provider.exists("StreamVault/a.mp4", 6))


class CliSafetyTests(StorageTestBase):
    def test_upload_resolves_cwd_relative_path(self):
        pid = self._provider()
        self._write("streamer/file.mp4", b"payload")
        previous = os.getcwd()
        os.chdir(self.base)
        self.addCleanup(os.chdir, previous)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["upload", "streamer/file.mp4"])
        self.assertEqual(code, 0)
        self.assertIn("Upload eingeordnet", output.getvalue())
        self.assertIsNotNone(database.find_storage_object(pid, "streamer/file.mp4"))

    def test_delete_remote_prechecks_before_confirmation(self):
        _, path, obj = self._uploaded_object("streamer/file.mp4", b"payload")
        path.unlink()
        output = io.StringIO()
        with redirect_stdout(output), mock.patch(
            "builtins.input", side_effect=AssertionError("prompted")
        ):
            code = cli.main(["delete-remote", str(obj["id"])])
        self.assertEqual(code, 2)
        self.assertIn("lokale Kopie fehlt", output.getvalue())
        self.assertIn("Datenverlust", output.getvalue())

    def test_cli_masks_provider_errors(self):
        pid = self._provider()
        output = io.StringIO()
        with mock.patch.object(
            cli.service,
            "test_provider",
            side_effect=StorageError("access_token=supersecret", status_code=401),
        ), redirect_stdout(output):
            code = cli.main(["test", str(pid)])
        self.assertEqual(code, 2)
        self.assertNotIn("supersecret", output.getvalue())
        self.assertIn("token=***", output.getvalue())

    def test_add_skips_auto_test_for_unlinked_oauth_provider(self):
        output = io.StringIO()
        with mock.patch.object(
            cli.sys, "stdin", mock.Mock(isatty=lambda: False)
        ), mock.patch.object(
            cli.service, "test_provider"
        ) as test_provider, redirect_stdout(output):
            code = cli.main(
                [
                    "add",
                    "--type",
                    "google_drive",
                    "--name",
                    "Unlinked Google",
                    "--set",
                    "client_id=client-id",
                    "--set",
                    "client_secret=client-secret",
                ]
            )
        self.assertEqual(code, 0)
        test_provider.assert_not_called()
        self.assertIn("übersprungen", output.getvalue())
        self.assertEqual(service.list_providers()[0]["name"], "Unlinked Google")


class StoragePageTests(StorageTestBase):
    def _client(self):
        from WEBUI import webui

        webui.ensure_admin_user()
        webui.app.config.update(TESTING=True)
        client = webui.app.test_client()
        with client.session_transaction() as session:
            now = time.time()
            session["logged_in"] = True
            session["user_role"] = "admin"
            session["username"] = config.VOD_USER
            session["login_time"] = now
            session["last_active"] = now
            session["_csrf_token"] = "test-csrf-token"
        return client

    def test_cloud_only_object_offers_restore_not_remote_delete(self):
        _, path, obj = self._uploaded_object("streamer/file.mp4", b"payload")
        path.unlink()
        database.update_storage_object(obj["id"], local_deleted_at=time.time())

        # The UI defaults to English since i18n; these assertions check the German UI.
        response = self._client().get("/admin/storage", headers={"Accept-Language": "de"})
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Nur Cloud", body)
        self.assertIn("Wiederherstellen", body)
        self.assertNotIn("Lokal löschen", body)
        self.assertNotIn("Cloud löschen", body)

    def test_storage_page_escapes_provider_supplied_values(self):
        from WEBUI import webui

        webui.ensure_admin_user()
        pid = service.create_provider(
            "<script>alert(1)</script>",
            "webdav",
            {
                "base_url": "https://dav.example.com/root/",
                "username": "vault",
                "password": "secret",
                "base_path": "<img src=x onerror=alert(2)>",
            },
        )
        database.set_storage_provider_check(
            pid, False, "</script><script>alert(3)</script>"
        )

        webui.app.config.update(TESTING=True)
        client = webui.app.test_client()
        with client.session_transaction() as session:
            now = time.time()
            session["logged_in"] = True
            session["user_role"] = "admin"
            session["username"] = config.VOD_USER
            session["login_time"] = now
            session["last_active"] = now
            session["_csrf_token"] = "test-csrf-token"

        response = client.get("/admin/storage")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertNotIn("<script>alert(3)</script>", body)
        self.assertNotIn("<img src=x onerror=alert(2)>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertIn("\\u003cscript\\u003ealert(1)\\u003c/script\\u003e", body)
        self.assertIn(f'data-update-url="/admin/storage/providers/{pid}/update"', body)

    def test_media_accel_path_uses_legacy_files_alias(self):
        from WEBUI import webui

        base_file = self._write("legacy/file.mp4", b"base")
        files_file = self._write("files/legacy/file.mp4", b"files")
        self.assertEqual(
            webui.media_accel_path(
                "legacy", "file.mp4", file_path=str(base_file)
            ),
            f"{webui.X_ACCEL_PREFIX}/legacy/file.mp4",
        )
        self.assertEqual(
            webui.media_accel_path(
                "legacy", "file.mp4", file_path=str(files_file)
            ),
            f"{webui.FILES_ACCEL_PREFIX}/legacy/file.mp4",
        )

        clip_file = self._write("files/legacy/clips/clip.mp4", b"clip")
        self.assertEqual(
            webui.media_accel_path(
                "legacy", "clip.mp4", clip=True, file_path=str(clip_file)
            ),
            f"{webui.FILES_ACCEL_PREFIX}/legacy/clips/clip.mp4",
        )

    def test_unchecked_enable_creates_disabled_provider(self):
        from WEBUI import webui

        webui.ensure_admin_user()
        client = self._client()
        response = client.post(
            "/admin/storage/providers/add",
            data={
                "csrf_token": "test-csrf-token",
                "provider_type": "webdav",
                "name": "Disabled WebDAV",
                "cfg_base_url": "https://dav.example.com/root/",
                "cfg_username": "vault",
                "cfg_password": "secret",
                "cfg_base_path": "Archive",
                "cfg_verify_ssl": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        providers = [
            row for row in service.list_providers() if row["name"] == "Disabled WebDAV"
        ]
        self.assertEqual(len(providers), 1)
        self.assertFalse(providers[0]["enabled"])

    def test_storage_admin_error_log_omits_traceback_and_secrets(self):
        from WEBUI import webui

        client = self._client()
        with mock.patch.object(
            service,
            "admin_context",
            side_effect=StorageError("access_token=supersecret", status_code=500),
        ), mock.patch.object(webui.logging, "error") as log_error:
            response = client.get("/admin/storage")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(log_error.call_count, 1)
        self.assertNotIn("exc_info", log_error.call_args.kwargs)
        self.assertNotIn("supersecret", str(log_error.call_args))


if __name__ == "__main__":
    unittest.main()
