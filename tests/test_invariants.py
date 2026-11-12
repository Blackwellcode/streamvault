"""Architecture invariants: process groups, tokens, locks, paths, SQLite.

The environment bootstrap (test credentials, temp BASE_DIR, fcntl shim on
Windows) lives in test_recorder_reliability and runs on import.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # run as tests.test_invariants
    from tests.test_recorder_reliability import IsolatedBaseTest

from core import config  # noqa: E402
from recorder import Streamrecorder as recorder  # noqa: E402

POSIX = os.name == "posix" and hasattr(os, "killpg")


def _admin_client(webui):
    webui.app.config.update(TESTING=True)
    client = webui.app.test_client()
    with client.session_transaction() as sess:
        now = time.time()
        sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                    login_time=now, last_active=now, _csrf_token="t")
    return client


class TranscribeCancelProcessGroupTests(IsolatedBaseTest):
    """Invariant 2: a PID read from a lock file never reaches killpg unchecked."""

    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        patcher = mock.patch.object(webui, "BASE_DIR", str(self.base))
        patcher.start()
        self.addCleanup(patcher.stop)
        webui.ensure_admin_user()
        channel = self.base / "streamer"
        channel.mkdir()
        self.vod = channel / "vod.mp4"
        self.vod.write_bytes(b"\0" * 16)
        self.lock = Path(webui.transcript_lock_path(webui.transcript_path_for_media(str(self.vod))))

    def _cancel_with_lock(self, content):
        self.lock.write_text(content, encoding="utf-8")
        client = _admin_client(self.webui)
        with mock.patch.object(self.webui.os, "killpg", create=True) as killpg, \
                mock.patch.object(self.webui.os, "kill", create=True) as kill:
            response = client.post("/vod/transcribe/cancel/streamer/vod.mp4", data={"csrf_token": "t"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.lock.exists(), "cancel must remove the lock")
        return killpg, kill

    def test_lock_with_own_pid_signals_nothing(self):
        killpg, kill = self._cancel_with_lock(str(os.getpid()))
        killpg.assert_not_called()
        kill.assert_not_called()

    def test_lock_with_pid_one_signals_nothing(self):
        killpg, kill = self._cancel_with_lock("1")
        killpg.assert_not_called()
        kill.assert_not_called()

    def test_lock_with_garbage_signals_nothing(self):
        killpg, kill = self._cancel_with_lock("not-a-pid")
        killpg.assert_not_called()
        kill.assert_not_called()

    def test_own_process_group_is_never_a_target(self):
        self.assertIsNone(self.webui.transcribe_cancel_pgid(os.getpid()))
        self.assertIsNone(self.webui.transcribe_cancel_pgid(1))
        self.assertIsNone(self.webui.transcribe_cancel_pgid(0))
        self.assertIsNone(self.webui.transcribe_cancel_pgid(-5))

    @unittest.skipUnless(POSIX and os.path.isdir("/proc"), "needs POSIX process groups and /proc")
    def test_unrelated_process_in_own_session_is_refused(self):
        # A live process that leads its own group but is not run_transcribe.py
        # (PID reuse after the runner died) must not be signalled.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
        self.addCleanup(lambda: (child.kill(), child.wait(timeout=5)))
        self.assertIsNone(self.webui.transcribe_cancel_pgid(child.pid, time.time()))
        self.assertIsNone(child.poll())

    @unittest.skipUnless(POSIX and os.path.isdir("/proc"), "needs POSIX process groups and /proc")
    def test_real_runner_group_is_terminated(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "run_transcribe.py"],
            start_new_session=True,
        )
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait(timeout=5)))
        time.sleep(0.2)  # let /proc/<pid>/cmdline settle after exec
        self.lock.write_text(str(child.pid), encoding="utf-8")
        client = _admin_client(self.webui)
        response = client.post("/vod/transcribe/cancel/streamer/vod.mp4", data={"csrf_token": "t"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)
        self.assertFalse(self.lock.exists())


class WorkerTranscribeSessionTests(IsolatedBaseTest):
    def test_runner_is_started_in_its_own_session(self):
        from recorder import worker
        media = self.base / "streamer" / "vod.mp4"
        media.parent.mkdir()
        media.write_bytes(b"\0")
        (self.base / "run_transcribe.py").write_text("", encoding="utf-8")
        done = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        with mock.patch.object(worker.subprocess, "run", return_value=done) as run:
            self.assertTrue(worker.process_transcribe({"media": str(media)}))
        self.assertTrue(run.call_args.kwargs.get("start_new_session"))
        self.assertIn("timeout", run.call_args.kwargs)


class TwitchTokenNeverInArgvTests(IsolatedBaseTest):
    """Invariant 1: the OAuth token reaches Streamlink only via a private config file."""

    TOKEN = "abcdef0123456789secrettoken"

    def _assert_token_absent(self, cmd):
        self.assertFalse(any(self.TOKEN in str(arg) for arg in cmd), cmd)

    def test_record_and_probe_commands_keep_token_out_of_argv(self):
        # The streamlink binary is an installation detail, not under test.
        with mock.patch.object(config, "RECORDER_TWITCH_OAUTH_TOKEN", self.TOKEN), \
                mock.patch.object(recorder, "find_streamlink", return_value="streamlink"):
            for build in (recorder.build_streamlink_command, recorder.build_streamlink_probe_command):
                with self.subTest(build=build.__name__):
                    cmd = build("somechannel", {})
                    self._assert_token_absent(cmd)
                    cfg = cmd[cmd.index("--config") + 1]
                    self.assertIn(self.TOKEN, Path(cfg).read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_config_file_is_private(self):
        args = config.streamlink_twitch_config_args(self.TOKEN, config_dir=str(self.base / "cfg"))
        path = Path(args[1])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_existing_world_readable_file_is_tightened(self):
        cfg_dir = self.base / "cfg"
        cfg_dir.mkdir()
        existing = cfg_dir / "streamlink-twitch-api-header.cfg"
        existing.write_text("old", encoding="utf-8")
        os.chmod(existing, 0o644)
        config.streamlink_twitch_config_args(self.TOKEN, config_dir=str(cfg_dir))
        self.assertEqual(existing.stat().st_mode & 0o777, 0o600)

    def test_unwritable_config_falls_back_to_anonymous_and_logs(self):
        with mock.patch.object(config.os, "open", side_effect=PermissionError("denied")), \
                self.assertLogs(config.logger, level="ERROR") as logs:
            args = config.streamlink_twitch_config_args(self.TOKEN, config_dir=str(self.base / "cfg"))
        self.assertEqual(args, ["--no-config"])
        self._assert_token_absent(args)
        self.assertFalse(any(self.TOKEN in line for line in logs.output))

    def test_no_token_means_no_config(self):
        self.assertEqual(config.streamlink_twitch_config_args(""), ["--no-config"])


@unittest.skipUnless(POSIX, "needs POSIX process groups")
class StopProcessRealChildTests(unittest.TestCase):
    """stop_process with a real child in its own session: reaped, caller alive."""

    def test_stops_and_reaps_child_group(self):
        child = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait(timeout=5)))
        recorder.stop_process(child)
        self.assertIsNotNone(child.returncode)
        self.assertEqual(child.returncode, -signal.SIGTERM)

    def test_child_in_callers_group_is_terminated_without_killpg(self):
        child = subprocess.Popen(["sleep", "30"])  # shares our process group
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait(timeout=5)))
        with mock.patch.object(recorder.os, "killpg") as killpg:
            recorder.stop_process(child)
        killpg.assert_not_called()
        self.assertEqual(child.returncode, -signal.SIGTERM)


class ClipKillProcessTreeTests(unittest.TestCase):
    """clip_engine._kill_process_tree must never killpg its own group."""

    def test_own_group_falls_back_to_kill(self):
        from recorder import clip_engine
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = None
        own = os.getpgrp() if hasattr(os, "getpgrp") else 4242
        with mock.patch.object(clip_engine.os, "getpgid", return_value=own, create=True), \
                mock.patch.object(clip_engine.os, "getpgrp", return_value=own, create=True), \
                mock.patch.object(clip_engine.os, "killpg", create=True) as killpg:
            clip_engine._kill_process_tree(proc)
        killpg.assert_not_called()
        proc.kill.assert_called_once()

    def test_failed_kill_is_logged(self):
        from recorder import clip_engine
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = None
        proc.kill.side_effect = OSError("gone")
        with mock.patch.object(clip_engine.os, "getpgid", return_value=1, create=True), \
                mock.patch.object(clip_engine.os, "killpg", create=True), \
                self.assertLogs(clip_engine.logger, level="WARNING"):
            clip_engine._kill_process_tree(proc)


class SilentFailureLoggingTests(IsolatedBaseTest):
    """Former `except Exception: pass` paths now log a specific failure."""

    def test_corrupt_group_permissions_are_logged_and_empty(self):
        from core import database
        group = database.get_user_group_by_name("Viewer")
        conn = database.get_db()
        try:
            conn.execute("UPDATE user_groups SET permissions='{not json' WHERE id=?", (group["id"],))
            conn.commit()
        finally:
            conn.close()
        with self.assertLogs("database", level="WARNING"):
            loaded = database.get_user_group(group["id"])
        self.assertEqual(loaded["name"], "Viewer")
        self.assertIsInstance(loaded["permissions"], dict)

    def test_corrupt_legacy_channel_file_is_logged_and_kept(self):
        from core import database
        legacy = self.base / ".channels.json"
        legacy.write_text("{broken", encoding="utf-8")
        with mock.patch.object(database, "LEGACY_CHANNELS_FILE", str(legacy)), \
                self.assertLogs("database", level="ERROR"):
            database.init_db()
        self.assertTrue(legacy.exists(), "unreadable legacy data must not be renamed away")

    def test_channel_lock_release_logs_fd_errors_and_removes_file(self):
        recordings = self.base / "chan"
        recordings.mkdir()
        path = recorder.channel_lock_path(str(recordings), "chan")
        Path(path).write_text("1", encoding="utf-8")
        recorder._CHANNEL_LOCK_FDS[path] = 987654  # not an open descriptor
        self.addCleanup(recorder._CHANNEL_LOCK_FDS.pop, path, None)
        with self.assertLogs(recorder.logger, level="WARNING") as logs:
            recorder.release_held_channel_lock(str(recordings), "chan")
        self.assertTrue(any("not closed" in line for line in logs.output))
        self.assertFalse(os.path.exists(path))
        self.assertNotIn(path, recorder._CHANNEL_LOCK_FDS)

    def test_failing_wake_check_does_not_end_the_sleep(self):
        calls = []

        def wake():
            calls.append(1)
            raise RuntimeError("probe broken")

        with mock.patch.object(recorder.time, "sleep"), \
                self.assertLogs(recorder.logger, level="WARNING"):
            recorder.LOG_THROTTLE.pop("wake-check", None)
            self.assertFalse(recorder.interruptible_sleep(0.01, wake_check=wake))
        self.assertTrue(calls)

    def test_unpersisted_secret_key_is_logged(self):
        from WEBUI import webui
        with mock.patch.object(config, "SECRET_KEY", ""), \
                mock.patch.object(webui.os, "open", side_effect=PermissionError("read-only")), \
                self.assertLogs(level="ERROR") as logs:
            key = webui.load_secret_key()
        self.assertEqual(len(key), 64)
        self.assertTrue(any("not persisted" in line for line in logs.output))

    def test_secret_key_file_is_reused(self):
        from WEBUI import webui
        with mock.patch.object(config, "SECRET_KEY", ""):
            first = webui.load_secret_key()
            self.assertEqual(webui.load_secret_key(), first)

    def test_dashboard_shm_cache_removal_failure_is_logged(self):
        from WEBUI import webui
        shm = self.base / "dash.json"
        shm.write_text("{}", encoding="utf-8")
        with mock.patch.object(webui, "_DASHBOARD_SHM_PATH", str(shm)), \
                mock.patch.object(webui.os, "remove", side_effect=PermissionError("busy")), \
                self.assertLogs(level="WARNING") as logs:
            webui.invalidate_dashboard_media_cache()
        self.assertTrue(any("shm cache" in line for line in logs.output))

    def test_worker_task_cleanup_logs_close_errors(self):
        from recorder import worker
        conn = mock.Mock()
        conn.close.side_effect = worker.sqlite3.ProgrammingError("closed")
        with mock.patch.object(worker, "get_db", return_value=conn), \
                self.assertLogs(level="WARNING"):
            worker.cleanup_old_tasks(force=True)


if __name__ == "__main__":
    unittest.main()
