"""Tests for scripts/vod_retention.py (archive VOD retention).

The environment bootstrap (test credentials, temp BASE_DIR, fcntl shim on
Windows) lives in test_recorder_reliability and runs on import.
"""
from __future__ import annotations

import importlib.util
import os
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # pragma: no cover - when run as tests.test_vod_retention
    from tests.test_recorder_reliability import IsolatedBaseTest

from core import database  # noqa: E402

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

    def test_manifest_missing_is_none_and_entries_are_basenames(self):
        self.assertIsNone(vr.load_offload_manifest(self.base / "missing.txt"))
        manifest = self.base / "m.txt"
        manifest.write_text("﻿a.mp4\n# comment\nfoo/b.mp4\r\n\nsub\\c.mp4\n", encoding="utf-8")
        self.assertEqual(vr.load_offload_manifest(manifest), {"a.mp4", "b.mp4", "c.mp4"})

    def test_offload_channel_needs_manifest_entry(self):
        listed = self.touch("foo_/foo_listed.mp4", age_days=20)
        unlisted = self.touch("foo_/foo_unlisted.mp4", age_days=20)
        other = self.touch("bar/bar_old.mp4", age_days=20)
        slugs = frozenset({"foo"})
        self.assertEqual(self.names(offload_slugs=slugs, offload_manifest=None), {other.name})
        self.assertEqual(self.names(offload_slugs=slugs, offload_manifest={listed.name}),
                         {other.name, listed.name})

        rc, out = self.run_quiet(self.base, 14, True, offload_slugs=slugs,
                                 offload_manifest={listed.name})
        self.assertEqual(rc, 0, out)
        self.assertFalse(listed.exists())
        self.assertTrue(unlisted.exists())
        self.assertFalse(other.exists())
        self.assertIn("offload_kept=1", out)

    def test_cli_reads_default_manifest_from_meta_dir(self):
        listed = self.touch("foo/foo_listed.mp4", age_days=20)
        unlisted = self.touch("foo/foo_unlisted.mp4", age_days=20)
        (self.base / ".meta" / vr.OFFLOAD_MANIFEST_NAME).write_text(listed.name + "\n", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
