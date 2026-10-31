"""Reliability regressions for recorder, worker, and task-dispatch fixes.

Runs with the standard library runner:

    python -m unittest discover -s tests -v

The recorder is Linux-oriented (``fcntl``) and the worker imports numpy at
module level. Both are shimmed here so the tests also run on a Windows audit
host without changing production code.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

_INITIAL_BASE = tempfile.mkdtemp(prefix="streamvault-reliability-tests-")
os.environ["VOD_USER"] = "vaultadmin"
os.environ["VOD_PASS"] = "VaultTestPass123!"
os.environ["VOD_SECRET_KEY"] = "test-session-secret-key-0123456789"
os.environ["STORAGE_ENCRYPTION_KEY"] = "test-storage-encryption-key-0123456789"
os.environ["VOD_BASE_DIR"] = _INITIAL_BASE
os.environ["STREAMRECORDER_LOG_DIR"] = _INITIAL_BASE
os.environ["VOD_ALLOW_ENV_LOGIN"] = "1"
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"
os.environ.pop("REDIS_URL", None)
os.environ.pop("VOD_RATELIMIT_STORAGE_URI", None)
# WEBUI.webui starts a network-bound badge sync daemon thread on import. It
# writes into whichever BASE_DIR is active and races TemporaryDirectory cleanup
# (intermittent 'Directory not empty' teardown errors), so keep it off in tests.
os.environ["TWITCH_BADGE_AUTO_SYNC"] = "0"

if os.name == "nt" and "fcntl" not in sys.modules:
    _fcntl = types.ModuleType("fcntl")
    _fcntl.LOCK_EX = 2
    _fcntl.LOCK_NB = 4
    _fcntl.LOCK_UN = 8
    _fcntl.flock = lambda *args, **kwargs: None
    sys.modules["fcntl"] = _fcntl

try:
    import numpy  # noqa: F401
except ImportError:
    _numpy = types.ModuleType("numpy")
    _numpy.ndarray = object
    sys.modules["numpy"] = _numpy

import WEBUI  # noqa: F401,E402  (parent package for the helper import test)
import run_transcribe  # noqa: E402
from core import config, database  # noqa: E402
from core.health import parse_supervisor_status  # noqa: E402
from recorder import Streamrecorder as recorder  # noqa: E402
from recorder import worker  # noqa: E402


class IsolatedBaseTest(unittest.TestCase):
    """Give every test its own base directory and patched config paths."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="streamvault-reliability-")
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._previous_base_env = os.environ.get("VOD_BASE_DIR")
        os.environ["VOD_BASE_DIR"] = str(self.base)
        self.addCleanup(self._restore_base_env)
        self._patches = [
            mock.patch.object(config, "BASE_DIR", str(self.base)),
            mock.patch.object(config, "DB_FILE", str(self.base / "streamrecorder.db")),
            mock.patch.object(config, "FILES_DIR", str(self.base / "files")),
            mock.patch.object(config, "HLS_ROOT", str(self.base / ".hls")),
            mock.patch.object(config, "THUMB_ROOT", str(self.base / ".thumbs")),
            mock.patch.object(config, "META_DIR", str(self.base / ".meta")),
            mock.patch.object(config, "APP_DIR", str(self.base)),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        os.makedirs(config.META_DIR, exist_ok=True)
        os.makedirs(config.FILES_DIR, exist_ok=True)
        database.init_db()

    def _restore_base_env(self):
        if self._previous_base_env is None:
            os.environ.pop("VOD_BASE_DIR", None)
        else:
            os.environ["VOD_BASE_DIR"] = self._previous_base_env


class LiveOnlyMonitorTests(IsolatedBaseTest):
    def test_check_interval_constant_is_defined(self):
        self.assertIsInstance(recorder.CHECK_INTERVAL, int)
        self.assertGreaterEqual(recorder.CHECK_INTERVAL, 1)

    def test_monitor_completes_iteration_without_name_error(self):
        conf = {"record": False, "platform": "twitch"}
        calls = {"count": 0}

        def fake_config(_channel):
            calls["count"] += 1
            return conf if calls["count"] == 1 else None

        with mock.patch.object(recorder, "get_source_config", side_effect=fake_config), \
             mock.patch.object(recorder, "cheap_live_presence", return_value={"live": False}), \
             mock.patch.object(recorder, "CHECK_INTERVAL", 0), \
             mock.patch.object(recorder, "shutdown_requested", False):
            recorder.monitor_stream_live_only("audit_channel", "audit_channel", conf)

        self.assertGreaterEqual(calls["count"], 2)

    def test_monitor_exits_immediately_during_shutdown(self):
        with mock.patch.object(recorder, "shutdown_requested", True), \
             mock.patch.object(recorder, "get_source_config") as get_conf:
            recorder.monitor_stream_live_only("audit_channel", "audit_channel", {"record": False})
        get_conf.assert_not_called()


class DuplicateCleanupTests(IsolatedBaseTest):
    def _recordings(self, directory: Path):
        first = directory / "shared_tw_2026-10-25_12-00-00.mp4"
        second = directory / "shared_tw_2026-10-25_12-00-05.mp4"
        for path in (first, second):
            path.write_bytes(b"\0" * 8192)
            os.utime(path, (time.time(), time.time()))
        return first, second

    def test_shared_archive_is_not_pruned(self):
        directory = self.base / "shared"
        directory.mkdir()
        first, second = self._recordings(directory)
        with mock.patch.object(
            recorder.database,
            "channels_for_archive",
            return_value=[{"name": "source_a"}, {"name": "source_b"}],
        ):
            recorder.cleanup_duplicate_recordings(
                str(directory), str(first), "source_a", archive_channel="shared"
            )
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_single_source_archive_still_prunes(self):
        directory = self.base / "single"
        directory.mkdir()
        first, second = self._recordings(directory)
        with mock.patch.object(
            recorder.database,
            "channels_for_archive",
            return_value=[{"name": "source_a"}],
        ):
            recorder.cleanup_duplicate_recordings(
                str(directory), str(first), "source_a", archive_channel="shared"
            )
        self.assertEqual(sum(path.exists() for path in (first, second)), 1)

    def test_lookup_failure_fails_closed(self):
        directory = self.base / "unknown"
        directory.mkdir()
        first, second = self._recordings(directory)
        with mock.patch.object(
            recorder.database,
            "channels_for_archive",
            side_effect=RuntimeError("database unavailable"),
        ):
            recorder.cleanup_duplicate_recordings(
                str(directory), str(first), "source_a", archive_channel="shared"
            )
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())


class ChannelLockOrderingTests(unittest.TestCase):
    def test_stop_happens_before_release(self):
        events = []
        with mock.patch.object(
            recorder, "stop_channel_process", side_effect=lambda *a, **k: events.append("stop")
        ), mock.patch.object(
            recorder,
            "release_held_channel_lock",
            side_effect=lambda *a, **k: events.append("release"),
        ):
            recorder.stop_channel_and_release_lock("/archive", "channel", object())
        self.assertEqual(events, ["stop", "release"])

    def test_lock_is_released_when_stop_raises(self):
        events = []
        with mock.patch.object(
            recorder,
            "stop_channel_process",
            side_effect=RuntimeError("terminate failed"),
        ), mock.patch.object(
            recorder,
            "release_held_channel_lock",
            side_effect=lambda *a, **k: events.append("release"),
        ):
            with self.assertRaises(RuntimeError):
                recorder.stop_channel_and_release_lock("/archive", "channel", object())
        self.assertEqual(events, ["release"])


class BoundedProcessWaitTests(unittest.TestCase):
    def test_normal_exit_does_not_kill(self):
        class Process:
            pid = 1

            def __init__(self):
                self.killed = False

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.killed = True

        process = Process()
        self.assertTrue(recorder.wait_for_process_exit(process, timeout=1))
        self.assertFalse(process.killed)

    def test_timeout_kills_and_reaps(self):
        class Process:
            pid = 2

            def __init__(self):
                self.killed = False
                self.waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
                return 1

            def kill(self):
                self.killed = True

        process = Process()
        self.assertFalse(recorder.wait_for_process_exit(process, timeout=1))
        self.assertTrue(process.killed)
        self.assertEqual(process.waits, 2)


class WorkerFolderSafetyTests(IsolatedBaseTest):
    def _folder(self):
        directory = self.base / "channel"
        directory.mkdir()
        return directory

    def test_empty_folder_defers_while_part_file_exists(self):
        directory = self._folder()
        part = directory / "recording.mp4.part"
        part.write_bytes(b"in progress")
        with self.assertRaises(worker.TaskDeferred):
            worker.process_empty_folder({"path": str(directory)})
        self.assertTrue(part.exists())

    def test_empty_folder_preserves_part_if_guard_races(self):
        directory = self._folder()
        part = directory / "recording.mp4.part"
        part.write_bytes(b"in progress")
        other = directory / "old.mp4"
        other.write_bytes(b"old")
        with mock.patch.object(worker, "_folder_has_active_recording", return_value=False):
            self.assertTrue(worker.process_empty_folder({"path": str(directory)}))
        self.assertTrue(part.exists())
        self.assertFalse(other.exists())

    def test_active_lock_file_blocks_cleanup(self):
        directory = self._folder()
        (directory / ".recording.channel.lock").write_text(
            str(os.getpid()), encoding="utf-8"
        )
        self.assertTrue(worker._folder_has_active_recording(str(directory)))

    def test_stale_part_file_does_not_block_forever(self):
        directory = self._folder()
        part = directory / "recording.mp4.part"
        part.write_bytes(b"stale")
        old = time.time() - (worker.ACTIVE_RECORDING_FRESH_SEC + 30)
        os.utime(part, (old, old))
        self.assertFalse(worker._folder_has_active_recording(str(directory)))

    def test_transcribe_missing_helper_fails_clearly(self):
        media = self.base / "media.mp4"
        media.write_bytes(b"video")
        missing_app = self.base / "missing-app"
        with mock.patch.object(worker.config, "APP_DIR", str(missing_app)):
            self.assertFalse(worker.process_transcribe({"media": str(media)}))

    def test_delete_folder_defers_while_recording_active(self):
        directory = self._folder()
        (directory / "recording.mp4.part").write_bytes(b"in progress")
        with mock.patch.object(worker.subprocess, "run") as run:
            with self.assertRaises(worker.TaskDeferred):
                worker.process_delete_folder(
                    {"path": str(directory), "streamer": "channel"}
                )
        run.assert_not_called()

    def test_delete_folder_never_invokes_fuser(self):
        directory = self._folder()
        (directory / "old.mp4").write_bytes(b"old")
        with mock.patch.object(worker.time, "sleep", return_value=None), \
             mock.patch.object(worker.subprocess, "run") as run:
            self.assertTrue(
                worker.process_delete_folder(
                    {"path": str(directory), "streamer": "channel"}
                )
            )
        run.assert_not_called()
        self.assertFalse(directory.exists())


class WorkerDispatchTests(unittest.TestCase):
    def test_dispatch_failure_releases_capacity_and_requeues(self):
        class ClosedPool:
            def submit(self, *args, **kwargs):
                raise RuntimeError("executor is shut down")

        with mock.patch.dict(worker._INFLIGHT, {"io": 0}, clear=False):
            with mock.patch.object(worker.database, "defer_task") as defer:
                with self.assertRaises(RuntimeError):
                    worker._submit_claimed_task(
                        {"io": ClosedPool()},
                        "io",
                        {"id": 17, "type": "thumbnail"},
                    )
            self.assertEqual(worker._INFLIGHT["io"], 0)
            defer.assert_called_once()


class TaskRequeueTests(IsolatedBaseTest):
    def test_stale_task_over_attempt_limit_is_failed(self):
        task_id = database.add_task("thumbnail", {"input_path": "ignored"})
        claimed = database.claim_next_task(
            "worker-a",
            stale_after=60,
            allowed_types=["thumbnail"],
            max_attempts_by_type={"thumbnail": 3},
        )
        self.assertIsNotNone(claimed)
        conn = database.get_db()
        try:
            conn.execute(
                "UPDATE tasks SET attempts=3, locked_at=? WHERE id=?",
                (time.time() - 3600, task_id),
            )
            conn.commit()
        finally:
            conn.close()

        result = database.claim_next_task(
            "worker-b",
            stale_after=60,
            allowed_types=["thumbnail"],
            max_attempts_by_type={"thumbnail": 3},
        )
        self.assertIsNone(result)

        conn = database.get_db()
        try:
            row = conn.execute(
                "SELECT status, error FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "failed")
        self.assertIn("max attempts", row["error"])


class RunTranscribeTests(unittest.TestCase):
    def test_usage_error(self):
        self.assertEqual(run_transcribe.main([]), 2)

    def test_delegates_to_existing_webui_implementation(self):
        fake_module = types.ModuleType("WEBUI.webui")
        fake_module.transcribe_media_file = mock.Mock(return_value=(True, "fertig"))
        with mock.patch.dict(sys.modules, {"WEBUI.webui": fake_module}):
            self.assertEqual(
                run_transcribe.main(["media.mp4", "transcript.txt"]),
                0,
            )
        fake_module.transcribe_media_file.assert_called_once_with(
            "media.mp4", "transcript.txt"
        )
        self.assertEqual(os.environ.get("STREAMVAULT_IMPORT_ONLY"), "1")


class HealthStatusTests(unittest.TestCase):
    EXPECTED = {"webui", "recorder", "worker"}

    def test_all_expected_programs_running(self):
        output = (
            "webui:webui               RUNNING   pid 1, uptime 1:00:00\n"
            "recorder:recorder         RUNNING   pid 2, uptime 1:00:00\n"
            "worker:worker             RUNNING   pid 3, uptime 1:00:00\n"
        )
        services, missing, ok = parse_supervisor_status(output, self.EXPECTED)
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        self.assertEqual(set(services), self.EXPECTED)

    def test_missing_worker_is_degraded(self):
        output = (
            "webui:webui               RUNNING   pid 1, uptime 1:00:00\n"
            "recorder:recorder         RUNNING   pid 2, uptime 1:00:00\n"
        )
        services, missing, ok = parse_supervisor_status(output, self.EXPECTED)
        self.assertFalse(ok)
        self.assertEqual(missing, ["worker"])

    def test_optional_livebot_is_included_when_present(self):
        output = (
            "webui:webui               RUNNING   pid 1, uptime 1:00:00\n"
            "recorder:recorder         RUNNING   pid 2, uptime 1:00:00\n"
            "worker:worker             RUNNING   pid 3, uptime 1:00:00\n"
            "livebot:livebot           RUNNING   pid 4, uptime 1:00:00\n"
        )
        services, missing, ok = parse_supervisor_status(output, self.EXPECTED)
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        self.assertIn("livebot", services)

    def test_stopped_program_is_degraded(self):
        output = (
            "webui:webui               RUNNING   pid 1, uptime 1:00:00\n"
            "recorder:recorder         RUNNING   pid 2, uptime 1:00:00\n"
            "worker:worker             STOPPED   Not started\n"
        )
        _services, missing, ok = parse_supervisor_status(output, self.EXPECTED)
        self.assertFalse(ok)
        self.assertEqual(missing, [])


class DiskSpaceGuardTests(IsolatedBaseTest):
    def test_disk_blocks_recording_on_nonexistent_dir_when_disk_full(self):
        from collections import namedtuple
        Usage = namedtuple("Usage", ["total", "used", "free"])
        fake_usage = Usage(total=100 * (1024 ** 3), used=98 * (1024 ** 3), free=2 * (1024 ** 3))
        nonexistent = self.base / "does_not_exist_yet" / "streamer"
        with mock.patch("shutil.disk_usage", return_value=fake_usage):
            self.assertTrue(recorder.disk_blocks_recording(str(nonexistent)))

    def test_disk_allows_recording_on_nonexistent_dir_when_space_plentiful(self):
        from collections import namedtuple
        Usage = namedtuple("Usage", ["total", "used", "free"])
        fake_usage = Usage(total=100 * (1024 ** 3), used=20 * (1024 ** 3), free=80 * (1024 ** 3))
        nonexistent = self.base / "does_not_exist_yet" / "streamer"
        with mock.patch("shutil.disk_usage", return_value=fake_usage):
            self.assertFalse(recorder.disk_blocks_recording(str(nonexistent)))

    def test_disk_pause_flag_blocks_immediately(self):
        with mock.patch.object(recorder, "_storage_pause_flag_active", return_value=True):
            self.assertTrue(recorder.disk_blocks_recording(str(self.base)))


class NetworkProbeTimeoutTests(unittest.TestCase):
    def test_streamlink_probe_timeout_returns_error_dict(self):
        with mock.patch.object(recorder, "find_streamlink", return_value="streamlink"), \
             mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="streamlink", timeout=10)):
            res = recorder.probe_streamlink_playable("testchannel", {"platform": "twitch"})
            self.assertFalse(res["ok"])
            self.assertIn("Timeout", res["error"])

    def test_ytdlp_probe_timeout_returns_error_dict(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="yt-dlp", timeout=10)):
            res = recorder.probe_ytdlp_playable("testchannel", {"platform": "youtube"})
            self.assertFalse(res["ok"])
            self.assertIn("Timeout", res["error"])

    def test_gql_timeout_returns_error_dict(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
            res = recorder.get_twitch_stream_status("testchannel")
            self.assertFalse(res["ok"])
            self.assertIn("timed out", res["error"])


class ShutdownControlTests(unittest.TestCase):
    def test_interruptible_sleep_exits_early_on_shutdown(self):
        start = time.time()
        with mock.patch.object(recorder, "shutdown_requested", True):
            recorder.interruptible_sleep(10)
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.0)

    def test_request_shutdown_sets_flag(self):
        original = recorder.shutdown_requested
        try:
            recorder.shutdown_requested = False
            recorder.request_shutdown()
            self.assertTrue(recorder.shutdown_requested)
        finally:
            recorder.shutdown_requested = original


if __name__ == "__main__":
    unittest.main()
