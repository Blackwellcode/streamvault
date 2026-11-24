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


if __name__ == "__main__":
    unittest.main()
