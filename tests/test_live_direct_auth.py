"""nginx auth_request target for the direct live routes.

nginx serves /live/<channel>.ts (MPEG-TS fan-out) and the LL-HLS files
/live/<channel>/{stream,index}.m3u8 and seg_N.ts itself; every request is
checked first by GET /api/live/auth with the original URI in X-Original-URI.
"""

from __future__ import annotations

import pathlib
import time
import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401
except ImportError:  # run as tests.test_live_direct_auth
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401

from core import config, twitch_api  # noqa: E402

AUTH = "/api/live/auth"
ROOT = pathlib.Path(__file__).resolve().parents[1]
DIRECT_URIS = (
    "/live/demochan.ts",
    "/live/demochan/stream.m3u8",
    "/live/demochan/index.m3u8",
    "/live/demochan/seg_00042.ts",
)


def _stream(login="demochan"):
    return {
        "login": login, "display_name": "DemoChan", "title": "", "game_name": "",
        "viewer_count": 1, "is_live": True, "monitored": True,
    }


class LiveDirectAuthTests(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        webui.app.config.update(TESTING=True)
        webui.ensure_admin_user()
        self.anon = webui.app.test_client()
        self.user = webui.app.test_client()
        with self.user.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")

    def _auth(self, client, uri):
        return client.get(AUTH, headers={"X-Original-URI": uri})

    def test_anonymous_is_denied_on_every_direct_route(self):
        for uri in DIRECT_URIS:
            with self.subTest(uri=uri):
                resp = self._auth(self.anon, uri)
                self.assertEqual(resp.status_code, 401)
                self.assertEqual(resp.get_data(), b"")
                self.assertIn("no-store", resp.headers.get("Cache-Control", ""))

    def test_logged_in_user_is_allowed(self):
        for uri in DIRECT_URIS + ("/live/demochan/seg_7.ts?t=1",):
            with self.subTest(uri=uri):
                resp = self._auth(self.user, uri)
                self.assertEqual(resp.status_code, 204)
                self.assertIn("no-store", resp.headers.get("Cache-Control", ""))

    def test_hidden_channel_is_forbidden_for_logged_in_user(self):
        with mock.patch.object(self.webui, "can_view_streamer", return_value=False) as check:
            resp = self._auth(self.user, "/live/DemoChan.ts")
        self.assertEqual(resp.status_code, 403)
        check.assert_called_once_with("demochan")

    def test_expired_session_is_denied(self):
        with mock.patch.object(self.webui, "session_is_expired", return_value=True):
            resp = self._auth(self.user, "/live/demochan/stream.m3u8")
        self.assertEqual(resp.status_code, 401)

    def test_unknown_or_malformed_uri_is_forbidden(self):
        for uri in ("", "/live/watch/demochan", "/live/../etc/passwd.ts", "/live/demo chan.ts",
                    "/vod/x.ts", "/live/demochan/seg_x.ts", "/live/demochan/other.m3u8"):
            with self.subTest(uri=uri):
                self.assertEqual(self._auth(self.user, uri).status_code, 403)

    def test_auth_endpoint_is_not_rate_limited(self):
        # One viewer causes about one auth subrequest per second (playlist and
        # segments); the global "1000 per hour" limit would cut streams after
        # minutes, and nginx turns a 429 from auth_request into a 500.
        limiter = self.webui.limiter
        with mock.patch.object(limiter, "enabled", True), \
                mock.patch.object(self.webui, "get_client_ip", return_value="203.0.113.7"):
            limiter.reset()
            self.addCleanup(limiter.reset)
            statuses = {self._auth(self.user, "/live/demochan/seg_1.ts").status_code for _ in range(1100)}
        self.assertEqual(statuses, {204})

    def test_native_hls_uses_flask_playlist(self):
        with mock.patch.object(twitch_api, "get_stream", return_value=_stream()), \
                mock.patch.object(twitch_api, "enrich_monitored_streams", return_value=[]), \
                mock.patch.object(self.webui, "add_live_channel"):
            body = self.user.get("/live/watch/demochan?proxy=1").get_data(as_text=True)
        # Native HLS (iOS/Safari) plays the session-checked Flask playlist.
        self.assertIn("targetVideo.src = playlistUrl || hlsUrl;", body)
        self.assertIn("/live/playlist/demochan", body)


class NginxDirectLiveRouteTests(unittest.TestCase):
    """Every direct live location in the shipped nginx configs sits behind auth_request."""

    CONFIGS = ("nginx.conf", "install.sh", "INSTALL.md")

    def _live_locations(self, text):
        blocks, current = [], None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("location ~ ^/live/") and stripped.endswith("{"):
                current = [stripped]
                continue
            if current is not None:
                current.append(stripped)
                if stripped == "}":
                    blocks.append("\n".join(current))
                    current = None
        return blocks

    def test_direct_live_locations_require_auth_request(self):
        for name in self.CONFIGS:
            text = (ROOT / name).read_text(encoding="utf-8")
            blocks = self._live_locations(text)
            with self.subTest(config=name):
                self.assertEqual(len(blocks), 2, "expected the .ts and the LL-HLS location")
                for block in blocks:
                    self.assertIn("auth_request /_live_auth;", block)
                self.assertIn("location = /_live_auth {", text)
                auth = text.split("location = /_live_auth {", 1)[1].split("}", 1)[0]
                self.assertIn("internal;", auth)
                self.assertIn("/api/live/auth;", auth)
                self.assertIn("X-Original-URI", auth)

    def test_access_log_has_no_cookies(self):
        for name in self.CONFIGS:
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(config=name):
                self.assertNotIn("http_cookie", text)
                self.assertRegex(text, r"log_format (streamvault_)?timed ")
                self.assertRegex(text, r"access_log \S+ (streamvault_)?timed;")


if __name__ == "__main__":
    unittest.main()
