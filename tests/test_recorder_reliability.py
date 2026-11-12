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
import urllib.parse
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
from core.health import parse_supervisor_status, parse_systemctl_show  # noqa: E402
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

        ports = recorder.LiveMonitorPorts(
            get_config=fake_config,
            live_presence=lambda _channel, _conf: {"live": False},
            should_stop=lambda: False,
            sleep=lambda *_args, **_kwargs: False,
            interval=lambda: 0,
        )
        recorder.monitor_stream_live_only("audit_channel", "audit_channel", conf, ports=ports)

        self.assertGreaterEqual(calls["count"], 2)

    def test_monitor_exits_immediately_during_shutdown(self):
        get_conf = mock.Mock()
        ports = recorder.LiveMonitorPorts(
            get_config=get_conf,
            live_presence=mock.Mock(),
            should_stop=lambda: True,
            sleep=mock.Mock(),
            interval=lambda: 0,
        )
        recorder.monitor_stream_live_only("audit_channel", "audit_channel", {"record": False}, ports=ports)
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
    def setUp(self):
        # run_transcribe.main sets STREAMVAULT_IMPORT_ONLY process-wide; restore it.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)

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


def _systemctl_block(program, load="loaded", active="active", sub="running"):
    return (
        f"Id=streamvault-{program}.service\n"
        f"LoadState={load}\nActiveState={active}\nSubState={sub}\n"
    )


class SystemdHealthTests(unittest.TestCase):
    EXPECTED = {"webui", "recorder", "worker"}

    def test_all_units_active(self):
        output = "\n".join(_systemctl_block(p) for p in ("worker", "webui", "recorder"))
        services, missing, ok = parse_systemctl_show(output, self.EXPECTED)
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        self.assertEqual(services["recorder"]["state"], "active/running")

    def test_failed_unit_is_degraded(self):
        output = "\n".join([
            _systemctl_block("webui"),
            _systemctl_block("recorder", active="failed", sub="failed"),
            _systemctl_block("worker"),
        ])
        services, missing, ok = parse_systemctl_show(output, self.EXPECTED)
        self.assertFalse(ok)
        self.assertEqual(missing, [])
        self.assertIs(services["recorder"]["ok"], False)

    def test_not_found_unit_is_missing(self):
        output = "\n".join([
            _systemctl_block("webui"),
            _systemctl_block("recorder"),
            _systemctl_block("worker", load="not-found", active="inactive", sub="dead"),
        ])
        _services, missing, ok = parse_systemctl_show(output, self.EXPECTED)
        self.assertFalse(ok)
        self.assertEqual(missing, ["worker"])

    def test_state_mapping(self):
        from core.health import systemd_state_ok
        self.assertIs(systemd_state_ok("activating", "auto-restart"), False)
        self.assertIsNone(systemd_state_ok("activating", "start"))
        self.assertIsNone(systemd_state_ok("deactivating", "stop-sigterm"))
        self.assertIs(systemd_state_ok("inactive", "dead"), False)
        self.assertIs(systemd_state_ok("active", "exited"), False)

    def test_crlf_output_is_parsed(self):
        output = "\n".join(_systemctl_block(p) for p in sorted(self.EXPECTED)).replace("\n", "\r\n")
        _services, missing, ok = parse_systemctl_show(output, self.EXPECTED)
        self.assertTrue(ok)
        self.assertEqual(missing, [])


class ServiceStatusFallbackTests(unittest.TestCase):
    EXPECTED = {"webui", "recorder", "worker"}

    @classmethod
    def setUpClass(cls):
        from WEBUI import webui
        cls.webui = webui

    def _run(self, responses):
        calls = []

        def fake(cmd):
            calls.append(cmd[0])
            return responses[cmd[0]]

        with mock.patch.object(self.webui, "_run_status_command", side_effect=fake):
            return self.webui.collect_service_status(self.EXPECTED), calls

    def test_supervisor_exit_3_reports_degraded(self):
        out = (
            "webui RUNNING pid 1, uptime 1:00:00\n"
            "recorder FATAL Exited too quickly\n"
            "worker RUNNING pid 3, uptime 1:00:00\n"
        )
        status, calls = self._run({"supervisorctl": (out, 3, "")})
        self.assertIs(status["ok"], False)
        self.assertEqual(status["manager"], "supervisor")
        self.assertEqual(calls, ["supervisorctl"])

    def test_falls_back_to_systemd_without_supervisor(self):
        out = "\n".join([
            _systemctl_block("webui"),
            _systemctl_block("recorder", active="failed", sub="failed"),
            _systemctl_block("worker"),
        ])
        status, calls = self._run({
            "supervisorctl": ("", None, "No such file or directory"),
            "systemctl": (out, 0, ""),
        })
        self.assertIs(status["ok"], False)
        self.assertEqual(status["manager"], "systemd")
        self.assertEqual(calls, ["supervisorctl", "systemctl"])

    def test_unknown_when_no_manager_has_units(self):
        out = "\n".join(
            _systemctl_block(p, load="not-found", active="inactive", sub="dead")
            for p in sorted(self.EXPECTED)
        )
        with self.assertLogs(level="WARNING"):
            status, _calls = self._run({
                "supervisorctl": ("", None, "No such file or directory"),
                "systemctl": (out, 0, ""),
            })
        self.assertIsNone(status["ok"])
        self.assertIn("no streamvault-* systemd units loaded", status["error"])


class LiveHlsRootTests(unittest.TestCase):
    def test_recorder_writes_where_webui_serves(self):
        self.assertEqual(recorder.LIVE_HLS_ROOT, config.LIVE_HLS_ROOT)

    def test_chat_clip_playlist_uses_config_root(self):
        from recorder import chat_clip
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LIVE_HLS_DIR", None)
            playlist = chat_clip.live_hls_playlist("SomeChannel")
        self.assertEqual(
            playlist,
            os.path.join(config.LIVE_HLS_ROOT, "somechannel", "index.m3u8"),
        )

    def test_diag_finds_ffmpeg_writing_to_config_root(self):
        from core import live_watch_diag
        root = "/dev/shm/streamvault_hls"
        ps_output = (
            "USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND\n"
            f"vault 4242 1.0 0.5 1 1 ? S 00:00 0:01 ffmpeg -i pipe: {root}/alice/index.m3u8\n"
            "vault 4343 1.0 0.5 1 1 ? S 00:00 0:01 ffmpeg -i pipe: /dev/shm/other/bob/index.m3u8\n"
        )
        with mock.patch.object(live_watch_diag.config, "LIVE_HLS_ROOT", root), \
                mock.patch.object(live_watch_diag.subprocess, "check_output", return_value=ps_output):
            self.assertEqual(live_watch_diag.ffmpeg_hls_pids(), {"alice": 4242})


class SmartTvModeRemovedTests(unittest.TestCase):
    """The deployment-specific Smart-TV auto-login must not come back."""

    @classmethod
    def setUpClass(cls):
        from WEBUI import webui
        webui.app.config.update(TESTING=True)
        cls.client = webui.app.test_client()

    def test_tv_login_route_is_gone(self):
        self.assertEqual(self.client.get("/login/tv?token=x").status_code, 404)

    def test_smart_tv_user_agent_gets_regular_login_form(self):
        response = self.client.get(
            "/login?tv=1",
            headers={"User-Agent": "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0)"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="password"', response.get_data(as_text=True))

    def test_maintenance_has_no_default_bypass_user(self):
        from core import site_maintenance
        self.assertEqual(site_maintenance.DEFAULT_BYPASS_USERS, [])
        self.assertEqual(site_maintenance._default_state()["bypass_users"], [])


class CurrentRecordingFailClosedTests(IsolatedBaseTest):
    """_is_current_recording must never report a live file as finished."""

    def _channel(self):
        channel = self.base / "alice"
        channel.mkdir()
        return channel

    def test_pointer_claims_file(self):
        channel = self._channel()
        (channel / ".current_recording.json").write_text('{"filename": "a.mp4"}', encoding="utf-8")
        self.assertTrue(worker._is_current_recording(str(channel), "a.mp4"))
        self.assertFalse(worker._is_current_recording(str(channel), "b.mp4"))

    def test_half_written_pointer_counts_as_active(self):
        channel = self._channel()
        (channel / ".current_recording.json").write_text('{"filename": "a.mp', encoding="utf-8")
        with self.assertLogs(level="WARNING"):
            self.assertTrue(worker._is_current_recording(str(channel), "a.mp4"))

    def test_lock_owned_by_other_user_counts_as_active(self):
        channel = self._channel()
        (channel / "a.mp4.part").write_bytes(b"x")
        (channel / ".recording.lock").write_text("4242", encoding="utf-8")
        with mock.patch.object(worker.os, "kill", side_effect=PermissionError):
            self.assertTrue(worker._is_current_recording(str(channel), "a.mp4"))

    def test_dead_lock_pid_is_not_active(self):
        channel = self._channel()
        (channel / "a.mp4.part").write_bytes(b"x")
        (channel / ".recording.lock").write_text("4242", encoding="utf-8")
        with mock.patch.object(worker.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(worker._is_current_recording(str(channel), "a.mp4"))

    def test_listing_error_counts_as_active(self):
        channel = self._channel()
        with mock.patch.object(worker.os, "listdir", side_effect=OSError("EIO")), \
                self.assertLogs(level="ERROR"):
            self.assertTrue(worker._is_current_recording(str(channel), "a.mp4"))


class ReservedArchiveDirTests(IsolatedBaseTest):
    """BASE_DIR may be the app directory: code and dot-dirs are never archives."""

    def test_reserved_names(self):
        for name in ("core", "WEBUI", "recorder", "data", "files", ".meta", ".hls", "", ".."):
            with self.subTest(name=name):
                self.assertTrue(config.is_reserved_archive_name(name))
        for name in ("alice", "core_gaming", "webui2"):
            with self.subTest(name=name):
                self.assertFalse(config.is_reserved_archive_name(name))

    def test_worker_refuses_to_delete_code_directory(self):
        code_dir = self.base / "core"
        code_dir.mkdir()
        (code_dir / "config.py").write_text("x", encoding="utf-8")
        with self.assertLogs(level="ERROR"):
            self.assertFalse(worker.process_delete_folder({"path": str(code_dir), "streamer": "core"}))
        with self.assertLogs(level="ERROR"):
            self.assertFalse(worker.process_empty_folder({"path": str(code_dir)}))
        self.assertTrue((code_dir / "config.py").exists())

    def test_delete_route_rejects_reserved_name(self):
        from WEBUI import webui
        webui.app.config.update(TESTING=True)
        client = webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        with self.assertLogs(level="ERROR"):
            response = client.post("/delete_folder/core", data={"csrf_token": "t"})
        self.assertEqual(response.status_code, 400)


class DeploymentNeutralDefaultsTests(IsolatedBaseTest):
    """No built-in third-party proxies or host-specific /root paths."""

    def test_proxy_args_empty_without_configured_proxy(self):
        with mock.patch.dict(os.environ, {"TWITCH_PROXY_PLAYLIST": "", "STREAMLINK_PLUGIN_DIR": ""}), \
                self.assertLogs(level="WARNING"):
            self.assertEqual(recorder.streamlink_proxy_args(), [])

    def test_proxy_args_use_only_configured_proxy(self):
        with mock.patch.dict(os.environ, {"TWITCH_PROXY_PLAYLIST": "https://proxy.example.com",
                                          "STREAMLINK_PLUGIN_DIR": ""}):
            args = recorder.streamlink_proxy_args()
        self.assertIn("https://proxy.example.com", args)
        self.assertNotIn("--plugin-dirs", args)

    def test_backups_default_to_meta_dir(self):
        from WEBUI import webui
        with mock.patch.dict(os.environ, {"BACKUP_DIR": ""}):
            self.assertEqual(webui.system_backups_dir(), os.path.join(config.META_DIR, "backups"))

    def test_backup_create_without_script_reports_missing_config(self):
        from WEBUI import webui
        webui.app.config.update(TESTING=True)
        client = webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        with mock.patch.dict(os.environ, {"VOD_BACKUP_SCRIPT": ""}), \
                mock.patch.object(webui.subprocess, "run") as run:
            response = client.post("/admin/backups/create", data={"csrf_token": "t"})
        run.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertIn("VOD_BACKUP_SCRIPT", urllib.parse.unquote(response.headers["Location"]))


class LanguageSelectionTests(unittest.TestCase):
    """UI language: explicit choice (cookie) > browser Accept-Language > English."""

    @classmethod
    def setUpClass(cls):
        from WEBUI import i18n, webui
        cls.i18n, cls.webui = i18n, webui
        webui.app.config.update(TESTING=True)

    def _locale(self, accept=None, cookie=None):
        headers = {"Accept-Language": accept} if accept else {}
        if cookie:
            headers["Cookie"] = f"{self.i18n.LANG_COOKIE}={cookie}"
        with self.webui.app.test_request_context("/", headers=headers):
            return self.i18n.select_locale()

    def test_browser_language_is_used(self):
        self.assertEqual(self._locale("fr-FR,fr;q=0.9,en;q=0.8"), "fr")
        self.assertEqual(self._locale("pt-BR,pt;q=0.9"), "pt_BR")
        self.assertEqual(self._locale("zh-CN,zh;q=0.9"), "zh_Hans")

    def test_unsupported_region_falls_back_to_base_language(self):
        self.assertEqual(self._locale("de-AT"), "de")

    def test_quality_order_is_respected(self):
        self.assertEqual(self._locale("sv-SE,ja;q=0.7,de;q=0.5"), "ja")

    def test_unsupported_or_missing_language_falls_back_to_english(self):
        self.assertEqual(self._locale("sv-SE,fi;q=0.8"), "en")
        self.assertEqual(self._locale(), "en")

    def test_cookie_overrides_browser(self):
        self.assertEqual(self._locale("fr-FR", cookie="de"), "de")
        self.assertEqual(self._locale("fr-FR", cookie="bogus"), "fr")

    def test_language_route_sets_cookie_and_redirects_safely(self):
        client = self.webui.app.test_client()
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess["_csrf_token"]
        resp = client.post("/language", data={"lang": "ko", "next": "/login", "csrf_token": token})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/login"))
        self.assertIn(f"{self.i18n.LANG_COOKIE}=ko", resp.headers.get("Set-Cookie", ""))
        resp = client.post("/language", data={"lang": "ko", "next": "https://evil.example", "csrf_token": token})
        self.assertFalse(resp.headers["Location"].startswith("https://evil"))


class AssetVersionTests(IsolatedBaseTest):
    """?v= must change whenever a static file changes, without a manual bump."""

    def test_version_follows_static_files(self):
        from WEBUI import webui
        static = self.base / "static"
        (static / "css").mkdir(parents=True)
        style = static / "css" / "app.css"
        style.write_text("h1{font-size:24px}", encoding="utf-8")
        first = webui._compute_asset_version(str(static))
        self.assertEqual(first, webui._compute_asset_version(str(static)))
        style.write_text("h1{font-size:28px;}", encoding="utf-8")
        self.assertNotEqual(first, webui._compute_asset_version(str(static)))


class StorageInfoTests(IsolatedBaseTest):
    """Disk usage must work on every platform and log when it cannot be read."""

    def test_reports_real_disk_values(self):
        from WEBUI import webui
        with mock.patch.object(webui, "BASE_DIR", str(self.base)):
            info = webui.storage_info()
        self.assertGreater(info["total"], 0)
        self.assertNotEqual(info["free_str"], "N/A")

    def test_unreadable_path_is_logged(self):
        from WEBUI import webui
        missing = str(self.base / "does-not-exist")
        with mock.patch.object(webui, "BASE_DIR", missing), self.assertLogs(level="WARNING"):
            info = webui.storage_info()
        self.assertEqual(info["total_str"], "N/A")


class DemoSeedTests(IsolatedBaseTest):
    """The demo seeder must never write into an existing, unrelated archive."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "scripts" / "demo_seed.py"
        spec = importlib.util.spec_from_file_location("demo_seed", path)
        cls.demo_seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.demo_seed)

    def test_refuses_non_empty_foreign_directory(self):
        (self.base / "realchannel").mkdir()
        with self.assertRaises(SystemExit):
            self.demo_seed.check_target(str(self.base))

    def test_accepts_empty_and_own_directory(self):
        self.demo_seed.check_target(str(self.base / "fresh"))
        own = self.base / "own"
        own.mkdir()
        (own / self.demo_seed.MARKER_FILE).write_text("x", encoding="utf-8")
        (own / "pixelharbor").mkdir()
        self.demo_seed.check_target(str(own))
        # check_target creates a missing target and leaves its own data alone.
        self.assertTrue((self.base / "fresh").is_dir())
        self.assertEqual(sorted(p.name for p in own.iterdir()),
                         sorted([self.demo_seed.MARKER_FILE, "pixelharbor"]))


class LoadJsonTests(IsolatedBaseTest):
    """A corrupt state file must survive instead of being overwritten by the default."""

    @classmethod
    def setUpClass(cls):
        from WEBUI import webui
        cls.webui = webui

    def test_missing_file_returns_default(self):
        self.assertEqual(self.webui.load_json(str(self.base / "nope.json"), {"d": 1}), {"d": 1})

    def test_valid_file_is_loaded(self):
        path = self.base / "ok.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        self.assertEqual(self.webui.load_json(str(path), {}), {"a": 1})

    def test_corrupt_file_is_moved_aside_not_lost(self):
        path = self.base / "favorites.json"
        path.write_text('["half', encoding="utf-8")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(self.webui.load_json(str(path), []), [])
        self.assertFalse(path.exists())
        backups = list(self.base.glob("favorites.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), '["half')

    def test_unreadable_file_raises(self):
        path = self.base / "dir.json"
        path.mkdir()
        with self.assertRaises(OSError):
            self.webui.load_json(str(path), {})


class LegalPagesTests(IsolatedBaseTest):
    """The shipped content/*.md must parse; /agb and /datenschutz returned 500 before."""

    def setUp(self):
        super().setUp()  # own database, so the admin session below resolves
        # tube_terms resolves its paths from config.APP_DIR at import time; another
        # test may have imported it while APP_DIR pointed at a temp dir.
        from core import tube_terms
        content = Path(__file__).resolve().parents[1] / "content"
        for name, path in (("AGB_PATH", content / "agb.md"), ("PRIVACY_PATH", content / "datenschutz.md")):
            patcher = mock.patch.object(tube_terms, name, str(path))
            patcher.start()
            self.addCleanup(patcher.stop)
        tube_terms._cached_document.cache_clear()
        tube_terms._cached_privacy.cache_clear()

    def test_shipped_documents_parse(self):
        from core import tube_terms
        self.assertTrue(tube_terms.load_document(force=True)["terms_version"])
        self.assertTrue(tube_terms.load_privacy_document(force=True)["privacy_version"])
        self.assertIn("privacy_url", tube_terms.terms_web_context(site_url=""))

    def test_english_translations_match_binding_versions(self):
        from core import tube_rules, tube_terms
        content = Path(__file__).resolve().parents[1] / "content"
        with mock.patch.object(tube_terms, "AGB_PATH", str(content / "agb.md")), \
                mock.patch.object(tube_terms, "PRIVACY_PATH", str(content / "datenschutz.md")):
            de_terms = tube_terms.load_document(force=True)
            en_terms = tube_terms.load_document(lang="en")
            self.assertEqual(en_terms["terms_version"], de_terms["terms_version"])
            self.assertNotEqual(en_terms["body_md"], de_terms["body_md"])
            de_priv = tube_terms.load_privacy_document(force=True)
            fr_priv = tube_terms.load_privacy_document(lang="fr")  # non-German -> English
            self.assertEqual(fr_priv["privacy_version"], de_priv["privacy_version"])
            self.assertIn("Privacy", fr_priv["title"])
        self.assertEqual(len(tube_rules.RULES_EN["items"]), len(tube_rules.RULES_ITEMS))
        for phrase in ("AKZEPTIERE", "accept"):
            tube_rules.validate_accept_input(phrase)  # both languages' phrases are valid

    def test_legal_pages_render(self):
        from WEBUI import webui
        webui.ensure_admin_user()
        webui.app.config.update(TESTING=True)
        client = webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        for path in ("/agb", "/datenschutz"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)


class ClipEditorShareXssTests(IsolatedBaseTest):
    """?clip_name= lands in the Share button's data-clip-name; it must stay plain data.

    Under the enforced CSP (script-src-attr 'none') the button has no onclick; the
    listener reads data-clip-name. The value must round-trip exactly and the page
    must not contain any inline event handler the payload could have created.
    """

    def _share_clip_name(self, clip_name):
        from html.parser import HTMLParser
        from urllib.parse import quote

        from WEBUI import webui
        webui.ensure_admin_user()
        webui.app.config.update(TESTING=True)
        client = webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        response = client.get(
            "/editor/streamer/vod.mp4?clip_ok=1&clip_name=" + quote(clip_name, safe="")
        )
        self.assertEqual(response.status_code, 200)

        share_values = []
        inline_handlers = []

        class ShareCollector(HTMLParser):
            # HTMLParser decodes entities in attribute values exactly like a
            # browser does before the listener reads the data attribute.
            def handle_starttag(self, tag, attrs):
                attr_map = dict(attrs)
                inline_handlers.extend(name for name in attr_map if name.startswith("on"))
                if attr_map.get("id") == "btnShareClip":
                    share_values.append(attr_map.get("data-clip-name"))

        ShareCollector().feed(response.get_data(as_text=True))
        self.assertEqual(inline_handlers, [])
        self.assertEqual(len(share_values), 1)
        return share_values[0]

    def test_clip_name_cannot_break_out_of_the_share_handler(self):
        payloads = (
            "a');console.log('SVPOC');//",
            'a");console.log("SVPOC");//',
            "a\\');console.log(1);//",
            "</button><img src=x onerror=console.log(1)>",
            "plain_clip_2026-11-12.mp4",
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                # The attribute must decode to exactly the payload; anything else
                # means the payload escaped the attribute into markup.
                self.assertEqual(self._share_clip_name(payload), payload)


class ContentSecurityPolicyTests(IsolatedBaseTest):
    """The CSP is enforced by default and inline scripts run only with the request nonce."""

    def _get(self, path):
        from WEBUI import webui
        webui.ensure_admin_user()
        webui.app.config.update(TESTING=True)
        client = webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        return client.get(path)

    def _inline_scripts(self, html):
        from html.parser import HTMLParser

        scripts = []

        class Collector(HTMLParser):
            current = None

            def handle_starttag(self, tag, attrs):
                attr_map = dict(attrs)
                if tag == "script" and "src" not in attr_map:
                    Collector.current = {"attrs": attr_map, "body": ""}

            def handle_data(self, data):
                if Collector.current is not None:
                    Collector.current["body"] += data

            def handle_endtag(self, tag):
                if tag == "script" and Collector.current is not None:
                    scripts.append(Collector.current)
                    Collector.current = None

        Collector(convert_charrefs=True).feed(html)
        return scripts

    def test_policy_is_enforced_with_nonce_and_without_unsafe_inline_scripts(self):
        import base64
        import hashlib
        import re

        for path in ("/admin", "/admin/users", "/admin/storage", "/channels", "/search", "/"):
            with self.subTest(path=path):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("VOD_CSP_ENFORCE", None)
                    response = self._get(path)
                self.assertEqual(response.status_code, 200)
                policy = response.headers.get("Content-Security-Policy")
                self.assertIsNotNone(policy)
                script_src = re.search(r"script-src ([^;]*);", policy).group(1)
                self.assertNotIn("'unsafe-inline'", script_src)
                self.assertIn("script-src-attr 'none'", policy)
                nonce = re.search(r"'nonce-([^']+)'", script_src).group(1)
                hashes = set(re.findall(r"'(sha256-[^']+)'", script_src))
                scripts = self._inline_scripts(response.get_data(as_text=True))
                self.assertTrue(scripts)
                for script in scripts:
                    if script["attrs"].get("type") == "application/json":
                        continue
                    digest = "sha256-" + base64.b64encode(
                        hashlib.sha256(script["body"].encode("utf-8")).digest()).decode("ascii")
                    # Every executable inline script needs this response's nonce; only the
                    # static scripts injected by inject_mobile_app_head() may use a hash.
                    if script["attrs"].get("nonce") != nonce:
                        self.assertIn(digest, hashes, script["body"].strip()[:80])
                for host in ("https://api.betterttv.net", "https://api.frankerfacez.com", "https://7tv.io", "https://api.ivr.fi"):
                    self.assertIn(host, policy)

    def test_img_src_allows_https_images_for_custom_banners(self):
        import re

        response = self._get("/admin")
        policy = response.headers.get("Content-Security-Policy")
        img_src = re.search(r"img-src ([^;]*);", policy).group(1).split()
        self.assertIn("https:", img_src)
        self.assertNotIn("http:", img_src)
        self.assertNotIn("*", img_src)

    def test_report_only_when_explicitly_disabled(self):
        with mock.patch.dict(os.environ, {"VOD_CSP_ENFORCE": "false"}):
            response = self._get("/admin")
        self.assertIsNone(response.headers.get("Content-Security-Policy"))
        self.assertIsNotNone(response.headers.get("Content-Security-Policy-Report-Only"))


class ClientIpTests(unittest.TestCase):
    """Rate limits and bans key on get_client_ip(); it must not be spoofable."""

    @classmethod
    def setUpClass(cls):
        from WEBUI import webui
        cls.webui = webui

    def _ip(self, remote, headers=None):
        with self.webui.app.test_request_context(
            "/", headers=headers or {}, environ_base={"REMOTE_ADDR": remote}
        ):
            return self.webui.get_client_ip()

    def test_untrusted_peer_ignores_proxy_headers(self):
        ip = self._ip("203.0.113.9", {"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "1.2.3.4"})
        self.assertEqual(ip, "203.0.113.9")

    def test_trusted_proxy_uses_x_real_ip(self):
        ip = self._ip("127.0.0.1", {"X-Real-IP": "198.51.100.7", "X-Forwarded-For": "6.6.6.6, 198.51.100.7"})
        self.assertEqual(ip, "198.51.100.7")

    def test_spoofed_left_most_forwarded_hop_is_ignored(self):
        ip = self._ip("127.0.0.1", {"X-Forwarded-For": "6.6.6.6, 198.51.100.7"})
        self.assertEqual(ip, "198.51.100.7")

    def test_garbage_headers_fall_back_to_peer(self):
        ip = self._ip("127.0.0.1", {"X-Real-IP": "not-an-ip", "X-Forwarded-For": "also bad"})
        self.assertEqual(ip, "127.0.0.1")

    def test_trusted_proxies_accept_cidr(self):
        nets = self.webui._parse_trusted_proxies("10.0.0.0/8, ::1, bogus")
        self.assertEqual([str(n) for n in nets], ["10.0.0.0/8", "::1/128"])


class CastingRemovedTests(unittest.TestCase):
    """Media is session-only: no ?cast= bearer tokens, no wildcard CORS."""

    @classmethod
    def setUpClass(cls):
        from WEBUI import webui
        webui.app.config.update(TESTING=True)
        cls.client = webui.app.test_client()

    def test_cast_token_does_not_unlock_media(self):
        for path in (
            "/media/alice/video.mp4?cast=forged",
            "/hls/playlist/alice/video.mp4?cast=forged",
            "/live/playlist/alice?cast=forged",
            "/chat/live/alice?cast=forged",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_cast_url_endpoint_is_gone(self):
        self.assertEqual(self.client.get("/api/live/cast-url/alice").status_code, 404)

    def test_obs_pages_require_login(self):
        for path in ("/live/obs/alice", "/chat/obs/alice"):
            with self.subTest(path=path):
                response = self.client.get(path + "?cast=forged")
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])


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
        self.assertTrue(recorder.disk_blocks_recording(str(self.base), pause_flag=True))


class NetworkProbeTimeoutTests(unittest.TestCase):
    def test_streamlink_probe_timeout_returns_error_dict(self):
        from recorder.recording_runtime import FakeProcessRunner
        runner = FakeProcessRunner(error=subprocess.TimeoutExpired(cmd="streamlink", timeout=10))
        res = recorder.probe_streamlink_playable("testchannel", {"platform": "twitch"}, runner=runner)
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
        recorder.interruptible_sleep(10, should_stop=lambda: True)
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
