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
        with mock.patch.object(config, "RECORDER_TWITCH_OAUTH_TOKEN", self.TOKEN):
            for build in (recorder.build_streamlink_command, recorder.build_streamlink_probe_command):
                with self.subTest(build=build.__name__):
                    cmd = build("somechannel", {}, executable="streamlink")
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


@unittest.skipUnless(os.name == "posix", "needs real fcntl.flock")
class ChannelLockRealFlockTests(IsolatedBaseTest):
    """Invariant: one recorder per channel, enforced by a real flock across processes."""

    HOLDER = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    )

    def test_second_process_cannot_take_a_held_channel_lock(self):
        recordings = self.base / "chan"
        recordings.mkdir()
        path = recorder.channel_lock_path(str(recordings), "chan")
        holder = subprocess.Popen([sys.executable, "-c", self.HOLDER, path],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: holder.poll() is None and (holder.kill(), holder.wait(timeout=5)))
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        self.assertIsNone(recorder.try_acquire_channel_lock(str(recordings), "chan"))
        holder.kill()
        holder.wait(timeout=5)
        fd = recorder.try_acquire_channel_lock(str(recordings), "chan", pid=4321)
        self.addCleanup(recorder.release_held_channel_lock, str(recordings), "chan")
        self.assertIsNotNone(fd)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "4321")
        recorder.release_held_channel_lock(str(recordings), "chan")
        self.assertFalse(os.path.exists(path))


class MediaPathTraversalTests(IsolatedBaseTest):
    """resolve_media_path / media route: nothing outside the archive is served."""

    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        patcher = mock.patch.object(webui, "BASE_DIR", str(self.base))
        patcher.start()
        self.addCleanup(patcher.stop)
        webui.ensure_admin_user()
        self.archive = self.base / "streamer"
        self.archive.mkdir()
        (self.archive / "vod.mp4").write_bytes(b"\0" * 32)
        self.outside = Path(self._tmp.name).parent / f"outside-{os.getpid()}-{id(self)}.mp4"
        self.outside.write_bytes(b"secret")
        self.addCleanup(self.outside.unlink)

    def test_regular_file_resolves(self):
        resolved = self.webui.resolve_media_path("streamer", "vod.mp4", "vod")
        self.assertEqual(os.path.normpath(resolved), os.path.normpath(str(self.archive / "vod.mp4")))

    def test_dot_dot_and_absolute_names_are_rejected(self):
        for name in ("../" + self.outside.name, "..\\" + self.outside.name,
                     str(self.outside), "/etc/passwd.mp4", "..", "."):
            with self.subTest(name=name):
                self.assertIsNone(self.webui.resolve_media_path("streamer", name, "vod"))
        for streamer in ("..", "../streamer", "/"):
            with self.subTest(streamer=streamer):
                self.assertIsNone(self.webui.resolve_media_path(streamer, self.outside.name, "vod"))

    def test_route_returns_404_for_traversal(self):
        client = _admin_client(self.webui)
        for url in (f"/media/streamer/..%2F{self.outside.name}",
                    f"/media/streamer/..%5C{self.outside.name}",
                    f"/media/..%2F/{self.outside.name}"):
            with self.subTest(url=url):
                # Werkzeug may 308-normalise slashes first; the final answer must be 404.
                response = client.get(url, follow_redirects=True)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(b"secret", response.data)

    def test_symlink_to_outside_file_is_404(self):
        link = self.archive / "link.mp4"
        try:
            os.symlink(self.outside, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertIsNone(self.webui.resolve_media_path("streamer", "link.mp4", "vod"))
        client = _admin_client(self.webui)
        self.assertEqual(client.get("/media/streamer/link.mp4").status_code, 404)


class SqlitePragmaTests(IsolatedBaseTest):
    """Invariant 4: every connection runs WAL, busy_timeout=10000, synchronous=NORMAL."""

    def test_get_db_pragmas(self):
        from core import database
        conn = database.get_db()
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)  # NORMAL
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            conn.close()


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



class ChannelLockFileTests(IsolatedBaseTest):
    """Lock-file bookkeeping: only the holder (or the PID inside) may release."""

    def setUp(self):
        super().setUp()
        self.recordings = self.base / "chan"
        self.recordings.mkdir()
        self.path = recorder.channel_lock_path(str(self.recordings), "chan")
        self.addCleanup(recorder.release_held_channel_lock, str(self.recordings), "chan")

    def _content(self):
        return Path(self.path).read_text(encoding="utf-8")

    def test_lock_records_the_child_pid_after_spawn(self):
        self.assertIsNotNone(recorder.try_acquire_channel_lock(str(self.recordings), "chan", pid=111))
        self.assertEqual(self._content(), "111")
        recorder.update_channel_lock_pid(str(self.recordings), 22222, "chan")
        self.assertEqual(self._content(), "22222")

    def test_release_for_a_foreign_pid_keeps_the_held_lock(self):
        recorder.try_acquire_channel_lock(str(self.recordings), "chan", pid=111)
        recorder.release_channel_lock(str(self.recordings), pid=999, source_channel="chan")
        self.assertIn(self.path, recorder._CHANNEL_LOCK_FDS)
        self.assertTrue(os.path.exists(self.path))
        recorder.release_channel_lock(str(self.recordings), pid=111, source_channel="chan")
        self.assertNotIn(self.path, recorder._CHANNEL_LOCK_FDS)
        self.assertFalse(os.path.exists(self.path))

    def test_unheld_lock_file_is_removed_only_for_its_own_pid(self):
        Path(self.path).write_text("999", encoding="utf-8")
        recorder.release_channel_lock(str(self.recordings), pid=111, source_channel="chan")
        self.assertEqual(self._content(), "999")
        recorder.release_channel_lock(str(self.recordings), pid=999, source_channel="chan")
        self.assertFalse(os.path.exists(self.path))


@unittest.skipUnless(os.name == "posix", "needs real fcntl.flock")
class InstanceLockRealFlockTests(IsolatedBaseTest):
    """Invariant: a second recorder instance exits instead of recording twice."""

    def setUp(self):
        super().setUp()
        self.lock_file = str(self.base / ".streamrecorder.pid")
        for patcher in (mock.patch.object(recorder, "INSTANCE_LOCK_FILE", self.lock_file),
                        mock.patch.object(recorder, "_INSTANCE_LOCK_FD", None),
                        mock.patch.object(recorder.atexit, "register")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(recorder.release_instance_lock)

    def test_second_instance_exits_while_the_first_holds_the_lock(self):
        holder = subprocess.Popen([sys.executable, "-c", ChannelLockRealFlockTests.HOLDER, self.lock_file],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: holder.poll() is None and (holder.kill(), holder.wait(timeout=5)))
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        with self.assertLogs(recorder.logger, level="ERROR"), self.assertRaises(SystemExit) as ctx:
            recorder.acquire_instance_lock()
        self.assertEqual(ctx.exception.code, 1)
        holder.kill()
        holder.wait(timeout=5)
        recorder.acquire_instance_lock()
        self.assertEqual(Path(self.lock_file).read_text(encoding="utf-8"), str(os.getpid()))
        recorder.release_instance_lock()
        self.assertFalse(os.path.exists(self.lock_file))


def _ignore_sigterm_and_wait(ready):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ready.set()
    time.sleep(30)


@unittest.skipUnless(POSIX and hasattr(os, "fork"), "needs fork and POSIX signals")
class StopChannelProcessRealChildTests(unittest.TestCase):
    """A channel worker that ignores SIGTERM is killed within the deadline."""

    def test_worker_ignoring_sigterm_is_killed(self):
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        ready = ctx.Event()
        proc = ctx.Process(target=_ignore_sigterm_and_wait, args=(ready,))
        proc.start()
        self.addCleanup(lambda: proc.is_alive() and (proc.kill(), proc.join(5)))
        self.assertTrue(ready.wait(10))
        started = time.monotonic()
        with self.assertLogs(recorder.logger, level="WARNING"):
            recorder.stop_channel_process(proc, "chan", timeout=0.5)
        self.assertFalse(proc.is_alive())
        self.assertEqual(proc.exitcode, -signal.SIGKILL)
        self.assertLess(time.monotonic() - started, 10)


class WorkerVodTaskPathTests(IsolatedBaseTest):
    """worker._safe_vod_task_path: remux/thumbnail tasks never leave the archive."""

    def setUp(self):
        super().setUp()
        from recorder import worker
        self.worker = worker
        self.channel = self.base / "streamer"
        self.channel.mkdir()
        self.vod = self.channel / "vod.mp4"
        self.vod.write_bytes(b"\0" * 16)
        self.outside = Path(self._tmp.name).parent / f"outside-{os.getpid()}-{id(self)}.mp4"
        self.outside.write_bytes(b"secret")
        self.addCleanup(self.outside.unlink)

    def test_archive_file_resolves(self):
        resolved = self.worker._safe_vod_task_path("streamer", "vod.mp4")
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(os.path.realpath(self.vod)))

    def test_traversal_and_non_archive_names_are_rejected(self):
        cases = [
            ("streamer", "../" + self.outside.name),
            ("streamer", "..\\" + self.outside.name),
            ("streamer", str(self.outside)),
            ("..", self.outside.name),
            ("../streamer", "vod.mp4"),
            ("streamer", "missing.mp4"),
            ("streamer", "vod.mp4.part"),
            ("streamer", "notes.txt"),
            ("", "vod.mp4"),
            ("streamer", ""),
        ]
        for streamer, filename in cases:
            with self.subTest(streamer=streamer, filename=filename):
                self.assertIsNone(self.worker._safe_vod_task_path(streamer, filename))

    def test_symlink_out_of_the_archive_is_rejected(self):
        link = self.channel / "link.mp4"
        try:
            os.symlink(self.outside, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertIsNone(self.worker._safe_vod_task_path("streamer", "link.mp4"))


if __name__ == "__main__":
    unittest.main()
