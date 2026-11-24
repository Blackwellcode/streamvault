"""/api/live/ready: the player's poll survives a failing Twitch lookup."""

from __future__ import annotations

import time
import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401
except ImportError:  # run as tests.test_live_ready
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401

from core import config, twitch_api  # noqa: E402


class LiveReadyTests(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        webui.app.config.update(TESTING=True)
        webui.ensure_admin_user()
        patcher = mock.patch.object(webui, "add_live_channel")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = webui.app.test_client()
        with self.client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")

    def test_failed_twitch_lookup_is_not_a_server_error(self):
        with mock.patch.object(twitch_api, "get_stream", side_effect=OSError("offline")):
            resp = self.client.get("/api/live/ready/demochan")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["ready"])
        self.assertIsNone(data["twitch_live"])
        self.assertEqual(data["viewer_count"], 0)

    def test_outage_is_logged_once_per_minute_and_channel(self):
        from WEBUI import webui
        webui._RATE_LIMITED_LAST.clear()
        self.addCleanup(webui._RATE_LIMITED_LAST.clear)
        with mock.patch.object(twitch_api, "get_stream", side_effect=OSError("offline")):
            with self.assertLogs(level="WARNING") as logs:
                for channel in ("demochan", "otherchan") * 3:
                    self.assertEqual(self.client.get(f"/api/live/ready/{channel}").status_code, 200)
            lines = [line for line in logs.output if "Live status of" in line]
            self.assertEqual(len(lines), 2, lines)
            self.assertTrue(all("OSError: offline" in line for line in lines))
            webui._RATE_LIMITED_LAST["live-ready:demochan"] -= 61
            with self.assertLogs(level="WARNING") as logs:
                self.client.get("/api/live/ready/demochan")
        self.assertIn("2 more since the last warning", "".join(logs.output))


if __name__ == "__main__":
    unittest.main()
