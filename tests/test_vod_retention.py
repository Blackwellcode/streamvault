"""Tests for scripts/vod_retention.py (archive VOD retention).

The environment bootstrap (test credentials, temp BASE_DIR, fcntl shim on
Windows) lives in test_recorder_reliability and runs on import.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # pragma: no cover - when run as tests.test_vod_retention
    from tests.test_recorder_reliability import IsolatedBaseTest

from core import config, database, vod_watch_slots  # noqa: E402
from core.storage import service  # noqa: E402

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vod_retention.py"
SPEC = importlib.util.spec_from_file_location("vod_retention", SCRIPT_PATH)
vr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vr)

DAY = 86400.0


class RetentionTestBase(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        self.webui = vr._webui()
        patcher = mock.patch.object(self.webui, "BASE_DIR", str(self.base))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = time.time()

    def touch(self, rel: str, *, age_days: float, size: int = 64) -> Path:
        path = self.base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        mtime = self.now - age_days * DAY
        os.utime(path, (mtime, mtime))
        return path

    def names(self, **kwargs) -> set[str]:
        scan = vr.find_expired_archives(self.base, kwargs.pop("days", 14), self.now, **kwargs)
        return {c.path.name for c in scan.candidates}

    def run_quiet(self, *args, **kwargs) -> tuple[int, str]:
        with mock.patch("builtins.print") as fake_print:
            rc = vr.run(*args, now=self.now, **kwargs)
        return rc, "\n".join(str(c.args[0]) for c in fake_print.call_args_list)


class AgeAndModeTests(RetentionTestBase):
    def test_age_limit_is_exclusive_of_younger_files(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=14.01)
        young = self.touch("alpha/alpha_young.mp4", age_days=13.99)
        mkv = self.touch("alpha/alpha_copy.mkv", age_days=30)
        self.touch("alpha/alpha_x.mp4.remux.mp4", age_days=30)
        self.touch("alpha/clips/alpha_clip.mp4", age_days=30)
        self.touch(".meta/stray.mp4", age_days=30)
        self.touch("scripts/stray.mp4", age_days=30)
        self.assertEqual(self.names(days=14), {old.name, mkv.name})
        self.assertNotIn(young.name, self.names(days=14))

    def test_dry_run_deletes_nothing(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40, size=1000)
        rc, out = self.run_quiet(self.base, 14, False)
        self.assertEqual(rc, 0)
        self.assertTrue(old.exists())
        self.assertIn("DRY-RUN", out)
        self.assertIn("WOULD DELETE  alpha/alpha_old.mp4", out)
        self.assertIn("candidates=1", out)
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename=old.name))

    def test_cli_defaults_to_dry_run(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=400)
        with mock.patch("builtins.print"):
            rc = vr.main(["--base-dir", str(self.base), "--days", "14"])
        self.assertEqual(rc, 0)
        self.assertTrue(old.exists())

    def test_cli_rejects_dry_run_with_enforce_and_bad_days(self):
        with mock.patch("builtins.print"):
            self.assertEqual(vr.main(["--dry-run", "--enforce"]), 2)
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            vr.main(["--days", "0"])

    def test_enforce_deletes_like_webui_and_removes_sidecars(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        keep = self.touch("alpha/alpha_new.mp4", age_days=2)
        info = self.touch("alpha/alpha_old.mp4.info.json", age_days=40)
        chat = self.touch("alpha/chat_logs/alpha_old.jsonl", age_days=40)
        chunk = self.touch("alpha/chat_chunks/alpha_old/0.json", age_days=40)
        thumb = self.touch(".thumbs/alpha/alpha_old.jpg", age_days=40)
        hls = self.touch(".hls/alpha/alpha_old/index.m3u8", age_days=40)
        mkv = self.touch("alpha/alpha_copy.mkv", age_days=40)

        rc, out = self.run_quiet(self.base, 14, True)

        self.assertEqual(rc, 0, out)
        for gone in (old, info, chat, chunk.parent, thumb, hls.parent, mkv):
            self.assertFalse(gone.exists(), gone)
        self.assertTrue(keep.exists())
        self.assertTrue(database.is_deleted_media(streamer="alpha", filename=old.name))
        self.assertTrue(database.is_deleted_media(streamer="alpha", filename=mkv.name))
        self.assertIn("DELETED       alpha/alpha_old.mp4", out)

    def test_enforce_refuses_foreign_base_dir(self):
        other = self.base / "elsewhere"
        old = self.touch("elsewhere/alpha/alpha_old.mp4", age_days=40)
        rc, _ = self.run_quiet(other, 14, True)
        self.assertEqual(rc, 2)
        self.assertTrue(old.exists())


class OffloadTests(RetentionTestBase):
    def test_parse_offload_slugs_normalizes(self):
        self.assertEqual(vr.parse_offload_slugs("Foo_, bar ,,"), frozenset({"foo", "bar"}))
        self.assertEqual(vr.parse_offload_slugs(None), frozenset())
        self.assertTrue(vr.is_offload_slug("foo_", frozenset({"foo"})))
        self.assertFalse(vr.is_offload_slug("baz", frozenset({"foo"})))

    def test_manifest_missing_is_none_and_entries_are_channel_and_file(self):
        self.assertIsNone(vr.load_offload_manifest(self.base / "missing.txt"))
        manifest = self.base / "m.txt"
        manifest.write_text("﻿a.mp4\n# comment\nfoo/b.mp4\r\n\nSub_\\c.mp4\nx/y/d.mp4\n/e.mp4\n",
                            encoding="utf-8")
        with self.assertLogs(vr.logger, level="WARNING") as logs:
            keys = vr.load_offload_manifest(manifest)
        # Bare names and deeper paths cannot name the channel: ignored, not released.
        self.assertEqual(keys, {"foo/b.mp4", "sub/c.mp4"})
        self.assertTrue(any("'a.mp4' is not <channel>/<file>" in line for line in logs.output))

    def test_bare_manifest_name_never_releases_same_named_file_of_other_channel(self):
        foo = self.touch("foo/shared.mp4", age_days=20)
        bar = self.touch("bar/shared.mp4", age_days=20)
        slugs = frozenset({"foo", "bar"})
        manifest = {vr.manifest_key("foo", "shared.mp4")}
        scan = vr.find_expired_archives(self.base, 14, self.now, offload_slugs=slugs,
                                        offload_manifest=manifest)
        self.assertEqual({c.path for c in scan.candidates}, {foo})
        self.assertEqual(scan.held_back, 1)

        rc, out = self.run_quiet(self.base, 14, True, offload_slugs=slugs, offload_manifest=manifest)
        self.assertEqual(rc, 0, out)
        self.assertFalse(foo.exists())
        self.assertTrue(bar.exists())

    def test_offload_channel_needs_manifest_entry(self):
        listed = self.touch("foo_/foo_listed.mp4", age_days=20)
        unlisted = self.touch("foo_/foo_unlisted.mp4", age_days=20)
        other = self.touch("bar/bar_old.mp4", age_days=20)
        slugs = frozenset({"foo"})
        self.assertEqual(self.names(offload_slugs=slugs, offload_manifest=None), {other.name})
        listed_key = "foo/" + listed.name  # folder "foo_" normalizes like the slug
        self.assertEqual(self.names(offload_slugs=slugs, offload_manifest={listed_key}),
                         {other.name, listed.name})

        rc, out = self.run_quiet(self.base, 14, True, offload_slugs=slugs,
                                 offload_manifest={listed_key})
        self.assertEqual(rc, 0, out)
        self.assertFalse(listed.exists())
        self.assertTrue(unlisted.exists())
        self.assertFalse(other.exists())
        self.assertIn("offload_kept=1", out)

    def test_cli_reads_default_manifest_from_meta_dir(self):
        listed = self.touch("foo/foo_listed.mp4", age_days=20)
        unlisted = self.touch("foo/foo_unlisted.mp4", age_days=20)
        (self.base / ".meta" / vr.OFFLOAD_MANIFEST_NAME).write_text(
            f"foo/{listed.name}\n{unlisted.name}\n", encoding="utf-8")
        with mock.patch("builtins.print"), mock.patch.dict(os.environ, {"VOD_RETENTION_OFFLOAD_MANIFEST": ""}):
            rc = vr.main(["--base-dir", str(self.base), "--days", "14", "--offload-slugs", "foo", "--enforce"])
        self.assertEqual(rc, 0)
        self.assertFalse(listed.exists())
        self.assertTrue(unlisted.exists())


class ActiveRecordingTests(RetentionTestBase):
    def test_current_recording_pointer_protects_file_and_mkv_twin(self):
        active = self.touch("alpha/alpha_rec.mp4", age_days=30)
        twin = self.touch("alpha/alpha_rec.mkv", age_days=30)
        other = self.touch("alpha/alpha_old.mp4", age_days=30)
        (self.base / "alpha" / ".current_recording.json").write_text(
            '{"filename": "alpha_rec.mp4"}', encoding="utf-8")
        self.assertEqual(self.names(), {other.name})
        self.assertTrue(active.exists() and twin.exists())

    def test_part_and_remux_lock_siblings_protect_file(self):
        part_target = self.touch("alpha/alpha_part.mp4", age_days=30)
        self.touch("alpha/alpha_part.mp4.part", age_days=30)
        remux_target = self.touch("alpha/alpha_remux.mp4", age_days=30)
        self.touch("alpha/alpha_remux.mp4.remux.lock", age_days=30)
        self.assertEqual(self.names(), set())

        rc, _ = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 0)
        self.assertTrue(part_target.exists())
        self.assertTrue(remux_target.exists())

    def test_folder_with_fresh_part_is_skipped_entirely(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=30)
        part = self.touch("alpha/alpha_live.mkv.part", age_days=0)
        other = self.touch("bravo/bravo_old.mp4", age_days=30)

        rc, out = self.run_quiet(self.base, 14, True)

        self.assertEqual(rc, 0, out)
        self.assertTrue(old.exists())
        self.assertTrue(part.exists())
        self.assertFalse(other.exists())
        self.assertIn("SKIPPED       alpha/", out)

    def test_folder_with_fresh_remux_lock_is_skipped(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=30)
        self.touch("alpha/alpha_other.mp4.remux.lock", age_days=0)
        self.assertEqual(self.names(), set())
        self.assertTrue(old.exists())

    def test_recording_started_after_scan_keeps_candidate(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=30)
        scan = vr.find_expired_archives(self.base, 14, self.now)
        self.assertEqual([c.path for c in scan.candidates], [old])
        # A recording of the same stem starts between scan and delete.
        self.touch("alpha/alpha_old.mkv.part", age_days=0)
        self.assertEqual(vr.delete_candidate(scan.candidates[0]), "kept")
        self.assertTrue(old.exists())
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename=old.name))

    def test_pointer_written_after_scan_keeps_candidate(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=30)
        scan = vr.find_expired_archives(self.base, 14, self.now)
        (self.base / "alpha" / ".current_recording.json").write_text(
            '{"filename": "alpha_old.mp4"}', encoding="utf-8")
        with mock.patch.object(vr, "folder_is_busy", return_value=False):
            self.assertEqual(vr.delete_candidate(scan.candidates[0]), "kept")
        self.assertTrue(old.exists())


class SharedStemTests(RetentionTestBase):
    def test_expired_mkv_next_to_kept_mp4_removes_only_the_mkv(self):
        mkv = self.touch("alpha/alpha_x.mkv", age_days=40)
        mp4 = self.touch("alpha/alpha_x.mp4", age_days=2)
        kept = [
            mp4,
            self.touch("alpha/alpha_x.json", age_days=40),
            self.touch("alpha/chat_logs/alpha_x.jsonl", age_days=40),
            self.touch("alpha/chat_chunks/alpha_x/0.json", age_days=40),
            self.touch(".thumbs/alpha/alpha_x.jpg", age_days=40),
            self.touch(".hls/alpha/alpha_x/index.m3u8", age_days=40),
        ]

        rc, out = self.run_quiet(self.base, 14, True)

        self.assertEqual(rc, 0, out)
        self.assertIn("DELETED       alpha/alpha_x.mkv", out)
        self.assertFalse(mkv.exists())
        for path in kept:
            self.assertTrue(path.exists(), path)
        # No tombstone for the stem's chat or the kept file.
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename="alpha_x.jsonl"))
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename=mp4.name))

    def test_expired_mp4_next_to_kept_mkv_gets_tombstone_and_loses_shares(self):
        mp4 = self.touch("alpha/alpha_x.mp4", age_days=40)
        mkv = self.touch("alpha/alpha_x.mkv", age_days=2)
        chapters = self.touch("alpha/alpha_x.mp4.chapters.txt", age_days=40)
        kept = [
            mkv,
            self.touch("alpha/alpha_x.json", age_days=40),
            self.touch("alpha/chat_logs/alpha_x.jsonl", age_days=40),
            self.touch("alpha/chat_chunks/alpha_x/0.json", age_days=40),
            self.touch(".thumbs/alpha/alpha_x.jpg", age_days=40),
            self.touch(".hls/alpha/alpha_x/index.m3u8", age_days=40),
        ]
        share = {"streamer": "alpha", "filename": mp4.name, "route": "vod",
                 "created_at": time.time(), "views": 0}
        other = dict(share, filename="alpha_other.mp4")
        self.webui.save_shares({"tok_mp4": share, "tok_other": other})

        rc, out = self.run_quiet(self.base, 14, True)

        self.assertEqual(rc, 0, out)
        self.assertIn("DELETED       alpha/alpha_x.mp4", out)
        self.assertFalse(mp4.exists())
        self.assertFalse(chapters.exists())
        for path in kept:
            self.assertTrue(path.exists(), path)
        self.assertTrue(database.is_deleted_media(streamer="alpha", filename=mp4.name))
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename="alpha_x.jsonl"))
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename=mkv.name))
        self.assertEqual(set(self.webui.load_shares()), {"tok_other"})

    def test_both_expired_twins_are_deleted_with_sidecars(self):
        mkv = self.touch("alpha/alpha_x.mkv", age_days=40)
        mp4 = self.touch("alpha/alpha_x.mp4", age_days=40)
        chat = self.touch("alpha/chat_logs/alpha_x.jsonl", age_days=40)

        rc, out = self.run_quiet(self.base, 14, True)

        self.assertEqual(rc, 0, out)
        for gone in (mkv, mp4, chat):
            self.assertFalse(gone.exists(), gone)
        self.assertTrue(database.is_deleted_media(streamer="alpha", filename=mp4.name))

    def test_other_media_with_stem_ignores_other_stems_and_temporaries(self):
        mkv = self.touch("alpha/alpha_x.mkv", age_days=40)
        self.touch("alpha/alpha_x.mp4.part", age_days=40)
        self.touch("alpha/alpha_xy.mp4", age_days=40)
        self.touch("alpha/alpha_x.json", age_days=40)
        self.assertEqual(vr.other_media_with_stem(mkv), [])
        ts = self.touch("alpha/alpha_x.TS", age_days=40)
        self.assertEqual(vr.other_media_with_stem(mkv), [ts])


class ExtensionCaseTests(RetentionTestBase):
    def test_uppercase_extension_is_skipped_without_failure(self):
        # Distinct stems: Windows/macOS file systems ignore case.
        upper = self.touch("alpha/ALPHA_UPPER.MP4", age_days=40)
        mixed = self.touch("alpha/alpha_copy.Mkv", age_days=40)
        normal = self.touch("alpha/alpha_old.mp4", age_days=40)

        with self.assertLogs(vr.logger, level="WARNING") as logs:
            rc, out = self.run_quiet(self.base, 14, True)

        self.assertEqual(rc, 0, out)
        self.assertNotIn("FAILED", out)
        self.assertTrue(upper.exists())
        self.assertTrue(mixed.exists())
        self.assertFalse(normal.exists())
        self.assertTrue(any("ALPHA_UPPER.MP4" in line and "extension case" in line for line in logs.output))

    def test_extension_checks_match_the_webui_resolver(self):
        self.assertTrue(vr.is_archive_media_filename("a.mp4"))
        self.assertTrue(vr.is_archive_media_filename("a.mkv"))
        self.assertFalse(vr.is_archive_media_filename("a.MP4"))
        self.assertFalse(vr.is_archive_media_filename("a.MKV"))
        self.assertTrue(vr.has_foreign_extension_case("a.MP4"))
        self.assertTrue(vr.has_foreign_extension_case("a.mKv"))
        self.assertFalse(vr.has_foreign_extension_case("a.mp4"))
        self.assertFalse(vr.has_foreign_extension_case("a.MP4.part"))
        self.assertFalse(vr.has_foreign_extension_case("a.txt"))


class TombstoneOrderTests(RetentionTestBase):
    """B2: tombstone first; database errors fail one VOD, not the run."""

    def test_tombstone_exists_before_the_file_is_removed(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        real_delete = self.webui.delete_video_file
        seen = []

        def spy(streamer, filename, is_clip):
            seen.append((old.exists(), database.is_deleted_media(streamer="alpha", filename=filename)))
            return real_delete(streamer, filename, is_clip)

        with mock.patch.object(self.webui, "delete_video_file", side_effect=spy):
            rc, out = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen, [(True, True)])
        self.assertFalse(old.exists())

    def test_locked_database_keeps_that_vod_and_continues_with_the_next(self):
        files = {name: self.touch(f"alpha/{name}", age_days=40)
                 for name in ("alpha_a.mp4", "alpha_b.mkv", "alpha_c.mp4")}
        twin = self.touch("alpha/alpha_d.mkv", age_days=40)
        twin_kept = self.touch("alpha/alpha_d.mp4", age_days=1)
        real_upsert = database.upsert_deleted_media

        def upsert(streamer, filename, **kwargs):
            if not filename.startswith("alpha_c"):
                raise sqlite3.OperationalError("database is locked")
            return real_upsert(streamer, filename, **kwargs)

        with mock.patch.object(database, "upsert_deleted_media", side_effect=upsert), \
                self.assertLogs(vr.logger, level="ERROR") as logs:
            rc, out = self.run_quiet(self.base, 14, True)

        self.assertEqual(rc, 1, out)
        self.assertTrue(files["alpha_a.mp4"].exists())
        self.assertTrue(files["alpha_b.mkv"].exists())
        self.assertTrue(twin.exists() and twin_kept.exists())
        self.assertFalse(files["alpha_c.mp4"].exists())
        self.assertIn("FAILED        alpha/alpha_a.mp4", out)
        self.assertIn("DELETED       alpha/alpha_c.mp4", out)
        self.assertTrue(any("database is locked" in line for line in logs.output))

    def test_scan_database_error_fails_only_that_vod(self):
        bad = self.touch("alpha/alpha_bad.mp4", age_days=40)
        good = self.touch("alpha/alpha_good.mp4", age_days=40)
        real = vr.hold_reason

        def hold(slug, path, **kwargs):
            if path.name == bad.name:
                raise sqlite3.OperationalError("database is locked")
            return real(slug, path, **kwargs)

        with mock.patch.object(vr, "hold_reason", side_effect=hold), \
                self.assertLogs(vr.logger, level="ERROR"):
            rc, out = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 1, out)
        self.assertTrue(bad.exists())
        self.assertFalse(good.exists())
        self.assertIn("FAILED        alpha/alpha_bad.mp4", out)

    def test_failed_delete_takes_back_only_new_tombstones(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        mkv = self.touch("alpha/alpha_copy.mkv", age_days=40)
        database.upsert_deleted_media("alpha_", old.name)
        real_remove = vr._remove_below

        def remove(path, root):
            if path.name == mkv.name:
                raise PermissionError("read-only")
            return real_remove(path, root)

        with mock.patch.object(self.webui, "delete_video_file", return_value=False), \
                mock.patch.object(vr, "_remove_below", side_effect=remove), \
                self.assertLogs(vr.logger, level="ERROR"):
            rc, out = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 1, out)
        self.assertTrue(old.exists() and mkv.exists())
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename=old.name))
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename="alpha_old.jsonl"))
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename=mkv.name))
        # An older tombstone is not ours to remove.
        self.assertTrue(database.is_deleted_media(streamer="alpha_", filename=old.name))

    def test_error_after_removal_keeps_tombstone_and_removes_sidecars(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        chat = self.touch("alpha/chat_logs/alpha_old.jsonl", age_days=40)

        def delete(_streamer, _filename, _is_clip):
            old.unlink()
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(self.webui, "delete_video_file", side_effect=delete), \
                self.assertLogs(vr.logger, level="ERROR"):
            rc, out = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 1, out)
        self.assertTrue(database.is_deleted_media(streamer="alpha", filename=old.name))
        self.assertFalse(chat.exists())


class InUseTests(RetentionTestBase):
    """B3: uploads, unconfirmed remote copies and playback keep a VOD."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {
            "STORAGE_ENCRYPTION_KEY": "test-storage-encryption-key-0123456789"})
        env.start()
        self.addCleanup(env.stop)

    def provider(self) -> int:
        return service.create_provider("Dav", "webdav", {
            "base_url": "https://dav.example.com/dav/", "username": "u",
            "password": "dav-secret-password", "base_path": "SV", "verify_ssl": "1"})

    def finish_tasks(self) -> None:
        conn = database.get_db()
        try:
            conn.execute("UPDATE tasks SET status='completed'")
            conn.commit()
        finally:
            conn.close()

    def uploaded(self, path: Path) -> dict:
        self.provider()
        obj = service.enqueue_upload(str(path))
        self.finish_tasks()
        database.update_storage_object(obj["id"], status="uploaded", uploaded_at=time.time(),
                                       remote_size=path.stat().st_size)
        return obj

    def assert_kept(self, path: Path, reason: str) -> None:
        for enforce in (False, True):
            rc, out = self.run_quiet(self.base, 14, enforce)
            self.assertEqual(rc, 0, out)
            self.assertIn(f"KEPT          alpha/{path.name}", out)
            self.assertIn(reason, out)
            self.assertIn("in_use_kept=1", out)
        self.assertTrue(path.exists())
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename=path.name))

    def test_pending_or_running_upload_keeps_the_only_copy(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        other = self.touch("bravo/bravo_old.mp4", age_days=40)
        self.provider()
        service.enqueue_upload(str(old))
        self.assert_kept(old, "storage_upload")
        self.assertFalse(other.exists())
        conn = database.get_db()
        try:
            conn.execute("UPDATE tasks SET status='running'")
            conn.commit()
        finally:
            conn.close()
        self.assert_kept(old, "storage_upload")
        self.finish_tasks()
        rc, out = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 0, out)
        self.assertFalse(old.exists())

    def test_pending_upload_is_found_behind_a_long_queue(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        self.provider()
        service.enqueue_upload(str(old))
        conn = database.get_db()
        try:
            conn.executemany(
                "INSERT INTO tasks (type, payload, status, created_at, updated_at) "
                "VALUES ('storage_upload', '{\"object_id\": 999999}', 'pending', ?, ?)",
                [(time.time(), time.time())] * 1100)
            conn.commit()
        finally:
            conn.close()
        self.assertIn("storage_upload", service.local_delete_block_reason(str(old)))

    def test_pending_cloud_sync_keeps_its_archive(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        other = self.touch("bravo/bravo_old.mp4", age_days=40)
        self.provider()
        service.enqueue_sync(streamer="alpha")
        self.assert_kept(old, "storage_sync")
        self.assertFalse(other.exists())

    def test_auto_upload_without_remote_copy_keeps_the_vod(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        service.save_settings({"auto_upload": True})
        self.assert_kept(old, "no uploaded copy")

    def test_auto_upload_with_confirmed_copy_deletes(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        self.uploaded(old)
        service.save_settings({"auto_upload": True})
        remote = mock.Mock()
        remote.exists.return_value = True
        with mock.patch.object(service, "build_provider", return_value=remote):
            rc, out = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 0, out)
        self.assertFalse(old.exists())
        remote.exists.assert_called_once_with("SV/alpha/alpha_old.mp4", 64)

    def test_auto_upload_with_lost_or_outdated_copy_keeps(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        obj = self.uploaded(old)
        service.save_settings({"auto_upload": True})
        remote = mock.Mock()
        remote.exists.return_value = False
        with mock.patch.object(service, "build_provider", return_value=remote), \
                self.assertLogs(vr.logger, level="WARNING") as logs:
            rc, out = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 0, out)
        self.assertTrue(old.exists())
        self.assertIn("KEPT          alpha/alpha_old.mp4", out)
        self.assertTrue(any("could not be verified" in line for line in logs.output))
        # The uploaded version is not the local one any more.
        database.update_storage_object(obj["id"], local_mtime=1.0)
        self.assert_kept(old, "no uploaded copy")
        database.update_storage_object(obj["id"], local_mtime=old.stat().st_mtime,
                                       remote_deleted_at=time.time())
        self.assert_kept(old, "no uploaded copy")

    def test_watched_vod_is_kept_also_under_a_channel_alias(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        conn = database.get_db()
        try:
            conn.execute("INSERT INTO channels (name, archive_name) VALUES ('alphasource', 'alpha')")
            conn.commit()
        finally:
            conn.close()
        for streamer in ("alpha", "alphasource"):
            with self.subTest(streamer=streamer):
                self.assertTrue(vod_watch_slots.claim(f"viewer-{streamer}", streamer, old.name)["ok"])
                self.assert_kept(old, "being watched")
                vod_watch_slots.release(f"viewer-{streamer}")

    def test_playback_started_after_the_scan_keeps_the_vod(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        scan = vr.find_expired_archives(self.base, 14, self.now)
        vod_watch_slots.claim("viewer-late", "alpha", old.name)
        self.assertEqual(vr.delete_candidate(scan.candidates[0]), "kept")
        self.assertTrue(old.exists())

    def test_dry_run_of_a_foreign_base_dir_ignores_the_configured_database(self):
        service.save_settings({"auto_upload": True})
        other = self.base / "elsewhere"
        self.touch("elsewhere/alpha/alpha_old.mp4", age_days=40)
        with self.assertLogs(vr.logger, level="WARNING") as logs:
            rc, out = self.run_quiet(other, 14, False)
        self.assertEqual(rc, 0, out)
        self.assertIn("WOULD DELETE  alpha/alpha_old.mp4", out)
        self.assertTrue(any("are not checked" in line for line in logs.output))

    def test_dry_run_without_a_database_does_not_create_one(self):
        other = self.base / "fresh"
        self.touch("fresh/alpha/alpha_old.mp4", age_days=40)
        with mock.patch.object(config, "BASE_DIR", str(other)),                 mock.patch.dict(os.environ, {"VOD_BASE_DIR": str(other)}):
            rc, out = self.run_quiet(other, 14, False)
        self.assertEqual(rc, 0, out)
        self.assertIn("WOULD DELETE  alpha/alpha_old.mp4", out)
        self.assertFalse((other / "streamrecorder.db").exists())


class EnforceNeedsDatabaseTests(RetentionTestBase):
    """Without a usable database no upload or playback protection exists."""

    # Fresh interpreter: the WebUI import (which creates the database) must not
    # have happened yet. fcntl exists on POSIX only.
    _RUNNER = (
        "import runpy, sys, types\n"
        "try:\n"
        "    import fcntl\n"
        "except ImportError:\n"
        "    shim = types.ModuleType('fcntl')\n"
        "    shim.LOCK_EX = shim.LOCK_NB = shim.LOCK_UN = shim.LOCK_SH = 0\n"
        "    shim.flock = lambda *_args: None\n"
        "    sys.modules['fcntl'] = shim\n"
        "script = sys.argv[1]\n"
        "sys.argv = sys.argv[1:]\n"
        "runpy.run_path(script, run_name='__main__')\n"
    )

    def run_script(self, base: Path, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, VOD_BASE_DIR=str(base), PYTHONDONTWRITEBYTECODE="1")
        for name in ("VOD_META_DIR", "VOD_FILES_DIR", "VOD_HLS_DIR", "VOD_THUMB_DIR"):
            env.pop(name, None)
        return subprocess.run(
            [sys.executable, "-c", self._RUNNER, str(SCRIPT_PATH),
             "--base-dir", str(base), "--days", "14", *args],
            cwd=str(SCRIPT_PATH.parents[1]), env=env, capture_output=True, text=True,
            errors="replace", timeout=120)

    def test_script_refuses_enforce_without_creating_a_database(self):
        base = self.base / "fresh"
        files = [self.touch(f"fresh/alpha/alpha_{i}.mp4", age_days=40) for i in (1, 2)]
        result = self.run_script(base, "--enforce")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("missing", result.stderr)
        self.assertTrue(all(path.exists() for path in files))
        self.assertEqual(list(base.glob("streamrecorder.db*")), [])

        dry = self.run_script(base)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("WOULD DELETE  alpha/alpha_1.mp4", dry.stdout)
        self.assertIn("not checked", dry.stderr)
        self.assertEqual(list(base.glob("streamrecorder.db*")), [])

    def test_run_refuses_enforce_without_a_database(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        for path in self.base.glob("streamrecorder.db*"):
            path.unlink()
        with self.assertLogs(vr.logger, level="ERROR") as logs:
            rc, _ = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 2)
        self.assertTrue(old.exists())
        self.assertEqual(list(self.base.glob("streamrecorder.db*")), [])
        self.assertTrue(any("missing" in line for line in logs.output))

    def test_run_refuses_enforce_with_an_unusable_database(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        db_file = self.base / "streamrecorder.db"

        def empty():
            db_file.write_bytes(b"")

        def partial():
            conn = sqlite3.connect(db_file)
            try:
                conn.execute("CREATE TABLE channels (name TEXT)")
                conn.commit()
            finally:
                conn.close()

        def corrupt():
            db_file.write_bytes(b"not a database, just text " * 100)

        for create, message in ((empty, "lacks tables"), (partial, "lacks tables"),
                                (corrupt, "unreadable")):
            with self.subTest(create=create.__name__):
                for path in self.base.glob("streamrecorder.db*"):
                    path.unlink()
                create()
                before = db_file.read_bytes()
                with self.assertLogs(vr.logger, level="ERROR") as logs:
                    rc, _ = self.run_quiet(self.base, 14, True)
                self.assertEqual(rc, 2)
                self.assertTrue(old.exists())
                self.assertEqual(db_file.read_bytes(), before)
                self.assertTrue(any(message in line for line in logs.output))


class SystemdUnitTests(unittest.TestCase):
    """B4: the unit must not override VOD_RETENTION_DAYS."""

    def test_unit_leaves_the_age_limit_to_the_environment_and_stays_a_dry_run(self):
        unit = (SCRIPT_PATH.parent / "systemd" / "streamvault-vod-retention.service").read_text(
            encoding="utf-8")
        exec_start = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(len(exec_start), 1)
        self.assertTrue(exec_start[0].endswith("scripts/vod_retention.py"), exec_start[0])
        self.assertIn("EnvironmentFile=/etc/streamvault/env", unit)

    def test_environment_sets_the_age_limit(self):
        with mock.patch.object(vr, "run", return_value=0) as fake_run, \
                mock.patch.dict(os.environ, {"VOD_RETENTION_DAYS": "45"}):
            base = os.environ["VOD_BASE_DIR"]
            os.makedirs(base, exist_ok=True)
            self.assertEqual(vr.main(["--base-dir", base]), 0)
        self.assertEqual(fake_run.call_args.args[1], 45)


class OffloadSlugValidationTests(RetentionTestBase):
    """B5: a slug typo must not lift the protection silently."""

    def main_quiet(self, *args) -> int:
        with mock.patch("builtins.print"):
            return vr.main(["--base-dir", str(self.base), "--days", "14", *args])

    def test_invalid_characters_are_rc_2(self):
        old = self.touch("foo/foo_old.mp4", age_days=40)
        for raw in ("foo;bar", "foo bar", "fo/o", "foo,b*r", "___", ".."):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    vr.parse_offload_slugs(raw)
                self.assertEqual(self.main_quiet("--enforce", "--offload-slugs", raw), 2)
                self.assertEqual(self.main_quiet("--offload-slugs", raw), 2)
        self.assertTrue(old.exists())
        self.assertEqual(vr.parse_offload_slugs("my-chan, a.b"), frozenset({"my-chan", "a.b"}))

    def test_slug_without_folder_warns_in_dry_run_and_refuses_to_enforce(self):
        old = self.touch("foo/foo_old.mp4", age_days=40)
        with self.assertLogs(vr.logger, level="WARNING") as logs:
            rc, out = self.run_quiet(self.base, 14, False, offload_slugs=frozenset({"fooo"}))
        self.assertEqual(rc, 0, out)
        self.assertTrue(any("'fooo' has no archive folder" in line for line in logs.output))
        with self.assertLogs(vr.logger, level="ERROR"):
            rc, _ = self.run_quiet(self.base, 14, True, offload_slugs=frozenset({"fooo"}))
        self.assertEqual(rc, 2)
        self.assertTrue(old.exists())


class LinkedFolderTests(RetentionTestBase):
    """B6: folder links cannot escape the base directory or the offload protection."""

    def link_dir(self, link: Path, target: Path) -> None:
        if os.name == "nt":
            # Directory junctions need no privilege, unlike symlinks.
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            self.addCleanup(os.rmdir, link)
        else:
            os.symlink(target, link, target_is_directory=True)

    def test_link_does_not_bypass_offload_protection(self):
        old = self.touch("foo/foo_old.mp4", age_days=40)
        self.link_dir(self.base / "another", self.base / "foo")
        rc, out = self.run_quiet(self.base, 14, True, offload_slugs=frozenset({"foo"}))
        self.assertEqual(rc, 0, out)
        self.assertTrue(old.exists())
        # The listed name may also be the link.
        rc, out = self.run_quiet(self.base, 14, True, offload_slugs=frozenset({"another"}))
        self.assertEqual(rc, 0, out)
        self.assertTrue(old.exists())

    def test_same_real_folder_is_processed_once(self):
        old = self.touch("foo/foo_old.mp4", age_days=40)
        self.link_dir(self.base / "aaa", self.base / "foo")
        scan = vr.find_expired_archives(self.base, 14, self.now)
        self.assertEqual([(c.path, c.slug) for c in scan.candidates], [(old, "foo")])

    def test_link_outside_the_base_dir_is_refused(self):
        outside = Path(self._tmp.name + "-outside")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        outside.mkdir()
        target = outside / "foo_old.mp4"
        target.write_bytes(b"x")
        os.utime(target, (self.now - 40 * DAY,) * 2)
        self.link_dir(self.base / "foo", outside)
        with self.assertLogs(vr.logger, level="WARNING") as logs:
            rc, out = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 0, out)
        self.assertIn("candidates=0", out)
        self.assertTrue(target.exists())
        self.assertTrue(any("no archive folder" in line for line in logs.output))

    def test_link_into_clips_or_reserved_folder_is_refused(self):
        clip = self.touch("foo/clips/foo_clip.mp4", age_days=40)
        stray = self.touch("scripts/stray.mp4", age_days=40)
        self.link_dir(self.base / "bar", self.base / "foo" / "clips")
        self.link_dir(self.base / "baz", self.base / "scripts")
        self.assertEqual(self.names(), set())
        self.assertTrue(clip.exists() and stray.exists())


class ConfiguredFolderTests(RetentionTestBase):
    """B7: configured data folders below the base dir are no archives."""

    def test_configured_folders_are_excluded(self):
        settings = {"FILES_DIR": "resources", "META_DIR": "metadata", "THUMB_ROOT": "thumbs",
                    "HLS_ROOT": "hlscache", "LIVE_HLS_ROOT": "livehls"}
        patches = []
        for attr, name in settings.items():
            self.touch(f"{name}/{name}_old.mp4", age_days=40)
            # Not normalized on purpose: compared by real path.
            patches.append(mock.patch.object(
                config, attr, os.path.join(str(self.base), "alpha", "..", name)))
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertEqual(self.names(), {old.name})


class LateCheckTests(RetentionTestBase):
    """B12: re-check before deletion, unreadable pointer, transcript sidecar."""

    def test_file_changed_after_the_scan_is_kept(self):
        for change in ("mtime", "size"):
            with self.subTest(change=change):
                old = self.touch(f"alpha/alpha_{change}.mp4", age_days=40)
                scan = vr.find_expired_archives(self.base, 14, self.now)
                cand = next(c for c in scan.candidates if c.path == old)
                stamp = old.stat()
                if change == "size":
                    old.write_bytes(b"y" * 4096)
                    os.utime(old, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                else:
                    os.utime(old, (self.now, self.now))
                self.assertEqual(vr.delete_candidate(cand), "kept")
                self.assertTrue(old.exists())

    def test_change_while_the_tombstone_is_written_keeps_the_file(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        scan = vr.find_expired_archives(self.base, 14, self.now)
        real = vr._write_tombstones

        def write(*args):
            real(*args)
            os.utime(old, (self.now, self.now))

        with mock.patch.object(vr, "_write_tombstones", side_effect=write):
            self.assertEqual(vr.delete_candidate(scan.candidates[0]), "kept")
        self.assertTrue(old.exists())
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename=old.name))

    def test_unreadable_recording_pointer_makes_the_folder_busy(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        for name, data in ((".current_recording.json", b'{"filename": "alp'),
                           (".current_recording.alpha.json", b"\xff\xfe\x00"),
                           (".current_recording.json", b"[]")):
            with self.subTest(data=data):
                pointer = self.base / "alpha" / name
                pointer.write_bytes(data)
                rc, out = self.run_quiet(self.base, 14, True)
                pointer.unlink()
                self.assertEqual(rc, 0, out)
                self.assertTrue(old.exists())
                self.assertIn("SKIPPED       alpha/", out)

    def test_transcript_is_removed_with_its_vod(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        transcript = self.touch("alpha/alpha_old.transcript.txt", age_days=40)
        other = self.touch("alpha/alpha_other.transcript.txt", age_days=40)
        rc, out = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 0, out)
        self.assertFalse(old.exists())
        self.assertFalse(transcript.exists())
        self.assertTrue(other.exists())

    def test_transcript_stays_while_another_file_shares_the_stem(self):
        mkv = self.touch("alpha/alpha_x.mkv", age_days=40)
        self.touch("alpha/alpha_x.mp4", age_days=2)
        transcript = self.touch("alpha/alpha_x.transcript.txt", age_days=40)
        rc, out = self.run_quiet(self.base, 14, True)
        self.assertEqual(rc, 0, out)
        self.assertFalse(mkv.exists())
        self.assertTrue(transcript.exists())


class DeleteSafetyTests(RetentionTestBase):
    def _symlink(self, link: Path, target: Path, *, is_dir: bool = False) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, link, target_is_directory=is_dir)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

    def test_remove_below_refuses_paths_outside_root(self):
        outside = self.touch("bravo/bravo_note.json", age_days=1)
        with self.assertRaises(OSError):
            vr._remove_below(outside, self.base / "alpha")
        self.assertTrue(outside.exists())

    def test_remove_below_refuses_paths_reported_as_symlink(self):
        # Runs everywhere (Windows often cannot create symlinks); the real
        # symlink cases follow below.
        chat = self.touch("alpha/chat_logs/alpha_old.jsonl", age_days=40)
        with mock.patch.object(Path, "is_symlink", return_value=True), \
                self.assertRaises(OSError):
            vr._remove_below(chat, self.base / "alpha")
        self.assertTrue(chat.exists())

    def test_remove_below_refuses_symlinks(self):
        target = self.touch("bravo/bravo_note.json", age_days=1)
        link = self.base / "alpha" / "chat_logs" / "alpha_old.jsonl"
        self._symlink(link, target)
        with self.assertRaises(OSError):
            vr._remove_below(link, self.base / "alpha")
        self.assertTrue(target.exists())
        self.assertTrue(link.is_symlink())

    def test_symlinked_sidecars_survive_enforce(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        chat_target = self.touch("bravo/keep_chat.jsonl", age_days=40)
        chunk_target = self.touch("bravo/keep_chunks/0.json", age_days=40).parent
        self._symlink(self.base / "alpha" / "chat_logs" / "alpha_old.jsonl", chat_target)
        self._symlink(self.base / "alpha" / "chat_chunks" / "alpha_old", chunk_target, is_dir=True)

        with self.assertLogs(vr.logger, level="ERROR"):
            rc, out = self.run_quiet(self.base, 14, True)

        self.assertEqual(rc, 0, out)
        self.assertFalse(old.exists())
        self.assertTrue(chat_target.exists())
        self.assertTrue((chunk_target / "0.json").exists())

    def test_clips_are_untouched(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        clip_same_stem = self.touch("alpha/clips/alpha_old.mp4", age_days=40)
        clip = self.touch("alpha/clips/alpha_clip.mp4", age_days=400)

        rc, out = self.run_quiet(self.base, 14, True)

        self.assertEqual(rc, 0, out)
        self.assertFalse(old.exists())
        self.assertTrue(clip_same_stem.exists())
        self.assertTrue(clip.exists())
        self.assertNotIn("clips", out)

    def test_alias_resolving_to_another_file_fails_without_deleting(self):
        old = self.touch("alpha/alpha_old.mp4", age_days=40)
        alias_copy = self.touch("alpha_alias/alpha_old.mp4", age_days=1)
        with mock.patch.object(self.webui, "resolve_media_path", return_value=str(alias_copy)), \
                self.assertLogs(vr.logger, level="ERROR"):
            rc, out = self.run_quiet(self.base, 14, True)

        self.assertEqual(rc, 1, out)
        self.assertIn("FAILED        alpha/alpha_old.mp4", out)
        self.assertTrue(old.exists())
        self.assertTrue(alias_copy.exists())
        self.assertFalse(database.is_deleted_media(streamer="alpha", filename=old.name))


if __name__ == "__main__":
    unittest.main()
