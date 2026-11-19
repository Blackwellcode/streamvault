"""Removed features stay removed.

Whisper transcription and the TTS overlay were removed. Their routes must be
gone, a ``transcribe`` task still queued by an older release must be discarded
once by the worker (no crash, no endless requeue), and the alert overlay keeps
reading the old ``TTS_OVERLAY_TOKEN`` only as a deprecated fallback.
"""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # run as tests.test_removed_features
    from tests.test_recorder_reliability import IsolatedBaseTest

from core import alert_sounds, config, database  # noqa: E402
from recorder import Streamrecorder as recorder  # noqa: E402
from recorder import worker  # noqa: E402

REMOVED_ENDPOINTS = (
    "vod_transcribe",
    "vod_transcribe_status",
    "vod_transcribe_cancel",
    "vod_transcript_download",
    "tts_overlay",
    "tts_api_next",
    "tts_api_ack",
    "tts_api_skipcheck",
    "tts_api_status",
    "tts_audio",
)


class RemovedRoutesTests(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        patcher = mock.patch.object(webui, "BASE_DIR", str(self.base))
        patcher.start()
        self.addCleanup(patcher.stop)
        webui.ensure_admin_user()

    def _admin_client(self):
        self.webui.app.config.update(TESTING=True)
        client = self.webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        return client

    def test_endpoints_are_not_registered(self):
        endpoints = {rule.endpoint for rule in self.webui.app.url_map.iter_rules()}
        for name in REMOVED_ENDPOINTS:
            with self.subTest(endpoint=name):
                self.assertNotIn(name, endpoints)

    def test_removed_paths_return_404(self):
        client = self._admin_client()
        requests = (
            ("GET", "/vod/transcribe/status/streamer/vod.mp4"),
            ("POST", "/vod/transcribe/streamer/vod.mp4"),
            ("POST", "/vod/transcribe/cancel/streamer/vod.mp4"),
            ("GET", "/vod/transcript/streamer/vod.mp4"),
            ("GET", "/tts/overlay"),
            ("GET", "/tts/api/next?streamer=streamer"),
            ("POST", "/tts/api/ack?id=1"),
            ("GET", "/tts/api/status"),
            ("GET", "/tts/audio/streamer_1.mp3"),
        )
        for method, path in requests:
            with self.subTest(method=method, path=path):
                response = client.open(path, method=method, data={"csrf_token": "t"})
                self.assertEqual(response.status_code, 404)
                response.close()

    def test_transcription_and_tts_helpers_are_gone(self):
        for name in ("transcribe_media_file", "transcript_path_for_media", "TRANSCRIBE_MODEL",
                     "_tts_token_from_request", "STALK_ENABLED"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(self.webui, name))
        self.assertFalse(hasattr(worker, "process_transcribe"))
        self.assertFalse(hasattr(recorder, "AUTO_TRANSCRIBE"))


class RetiredTranscribeTaskTests(IsolatedBaseTest):
    def _row(self, task_id):
        conn = database.get_db()
        try:
            return conn.execute("SELECT status, attempts, error FROM tasks WHERE id=?", (task_id,)).fetchone()
        finally:
            conn.close()

    def test_queued_transcribe_task_is_failed_once_and_logged(self):
        task_id = database.add_task("transcribe", {"media": "streamer/vod.mp4"})
        self.assertEqual(worker._pool_name_for_task("transcribe"), "io")
        with mock.patch.dict(worker._INFLIGHT, {"io": 0}):
            allowed = worker._types_with_capacity()
            self.assertIn("transcribe", allowed)
            claimed = database.claim_next_task(
                worker.WORKER_ID,
                allowed_types=allowed,
                max_attempts_by_type=worker.TASK_MAX_ATTEMPTS,
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["id"], task_id)
            worker._INFLIGHT["io"] = 1  # as _submit_claimed_task does
            with self.assertLogs(level="WARNING") as logs:
                worker._execute_claimed_task(claimed)
            self.assertEqual(worker._INFLIGHT["io"], 0)
        self.assertTrue(any("verworfen" in line for line in logs.output), logs.output)
        row = self._row(task_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 1)
        self.assertIn("removed", row["error"])
        # Not requeued: nothing left to claim.
        self.assertIsNone(database.claim_next_task(
            worker.WORKER_ID,
            allowed_types=worker._types_with_capacity(),
            max_attempts_by_type=worker.TASK_MAX_ATTEMPTS,
        ))


class AlertOverlayTokenTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ALERT_OVERLAY_TOKEN", None)
        os.environ.pop("TTS_OVERLAY_TOKEN", None)
        warned = mock.patch.object(alert_sounds, "_LEGACY_TOKEN_WARNED", False)
        warned.start()
        self.addCleanup(warned.stop)

    def test_alert_token_is_used_without_warning(self):
        os.environ["ALERT_OVERLAY_TOKEN"] = "new-token"
        os.environ["TTS_OVERLAY_TOKEN"] = "old-token"
        with self.assertNoLogs(alert_sounds.logger, level="WARNING"):
            self.assertEqual(alert_sounds.overlay_token(), "new-token")
        self.assertTrue(alert_sounds.token_ok("new-token"))
        self.assertFalse(alert_sounds.token_ok("old-token"))

    def test_legacy_tts_token_is_a_deprecated_fallback(self):
        os.environ["TTS_OVERLAY_TOKEN"] = "old-token"
        with self.assertLogs(alert_sounds.logger, level="WARNING") as logs:
            self.assertEqual(alert_sounds.overlay_token(), "old-token")
        self.assertIn("ALERT_OVERLAY_TOKEN", logs.output[0])
        with self.assertNoLogs(alert_sounds.logger, level="WARNING"):
            self.assertTrue(alert_sounds.token_ok("old-token"))

    def test_no_token_configured_rejects_everything(self):
        self.assertEqual(alert_sounds.overlay_token(), "")
        self.assertFalse(alert_sounds.token_ok(""))
        self.assertFalse(alert_sounds.token_ok("anything"))


if __name__ == "__main__":
    unittest.main()
