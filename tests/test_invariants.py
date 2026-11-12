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


if __name__ == "__main__":
    unittest.main()
