"""LIVE_TWITCH_EMBED: the live watch page without the Twitch player embed.

Off (default): every device plays the server stream; ?embed=/?proxy= cannot
bring the Twitch player back, its URLs never reach the template and the CSP
does not allow Twitch frames. On: the previous behaviour (embed on desktop).
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401
except ImportError:  # run as tests.test_live_adfree
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401

from core import config, twitch_api  # noqa: E402

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
ANDROID_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36"
EMBED_ONLY_KEYS = ("player_url", "watch_embed_url", "embed_parents")


def _stream(is_live):
    return {
        "login": "demochan",
        "display_name": "DemoChan",
        "title": "Test Stream",
        "game_name": "Games",
        "viewer_count": 5 if is_live else 0,
        "is_live": is_live,
        "monitored": True,
    }


class _WatchPageBase(IsolatedBaseTest):
    embed = False

    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        webui.app.config.update(TESTING=True)
        webui.ensure_admin_user()
        for patcher in (
            mock.patch.object(config, "LIVE_TWITCH_EMBED", self.embed),
            mock.patch.object(config, "LIVE_TWITCH_OAUTH_ENABLED", False),
            mock.patch.object(twitch_api, "enrich_monitored_streams", return_value=[]),
            mock.patch.object(webui, "add_live_channel"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = webui.app.test_client()
        with self.client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")

    def _get(self, path, ua=DESKTOP_UA, is_live=True):
        with mock.patch.object(twitch_api, "get_stream", return_value=_stream(is_live)):
            return self.client.get(path, headers={"User-Agent": ua})

    def _context(self, path, ua=DESKTOP_UA):
        captured = {}
        real_render = self.webui.render_template_string

        def capture(source, **context):
            captured.update(context)
            return real_render(source, **context)

        with mock.patch.object(self.webui, "render_template_string", side_effect=capture):
            resp = self._get(path, ua)
        self.assertEqual(resp.status_code, 200)
        return captured

    def _assert_server_stream_only(self, resp):
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn("player.twitch.tv", body)
        self.assertNotIn("<iframe", body)
        self.assertIn('id="proxyPlayer"', body)
        self.assertIn("/live/playlist/demochan", body)
        return body


class EmbedOffTests(_WatchPageBase):
    embed = False

    def test_flag_defaults_to_off(self):
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("LIVE_TWITCH_EMBED", None)
            self.assertFalse(config._env_flag("LIVE_TWITCH_EMBED", False))

    def test_every_device_gets_the_server_stream(self):
        for label, ua in (("desktop", DESKTOP_UA), ("iphone", IPHONE_UA), ("android", ANDROID_UA)):
            with self.subTest(device=label):
                self._assert_server_stream_only(self._get("/live/watch/demochan", ua))

    def test_embed_and_proxy_params_cannot_restore_the_twitch_player(self):
        for query in ("?embed=1", "?embed=true", "?proxy=0", "?proxy=off&embed=on"):
            with self.subTest(query=query):
                self._assert_server_stream_only(self._get("/live/watch/demochan" + query))

    def test_context_has_no_embed_urls(self):
        context = self._context("/live/watch/demochan?embed=1")
        self.assertTrue(context["use_hls_proxy"])
        for key in EMBED_ONLY_KEYS:
            self.assertNotIn(key, context)
        self.assertNotIn("proxy=", context["watch_proxy_url"])

    def test_csp_does_not_allow_twitch_frames(self):
        resp = self._get("/live/watch/demochan")
        csp = resp.headers.get("Content-Security-Policy") or resp.headers.get(
            "Content-Security-Policy-Report-Only", "")
        self.assertIn("frame-src 'self';", csp)
        self.assertIn("child-src 'self' blob:;", csp)
        self.assertNotIn("twitch.tv", csp)

    def test_realtime_ts_url_is_absolute_for_worker_fetch(self):
        # mpegts.js fetches inside a blob: Web Worker on desktop (enableWorker); a
        # relative URL cannot be parsed there ("Failed to parse URL from /live/<channel>.ts").
        body = self._assert_server_stream_only(self._get("/live/watch/demochan"))
        self.assertIn(
            "new URL('/live/' + encodeURIComponent(channelName) + '.ts', window.location.href).href",
            body,
        )
        self.assertNotIn("const tsUrl = '/live/' + encodeURIComponent(channelName) + '.ts';", body)

    def test_player_falls_back_to_hls_without_ts_fan_out(self):
        body = self._assert_server_stream_only(self._get("/live/watch/demochan"))
        self.assertIn("preferLowLatencyHls = true;", body)
        self.assertIn("mseSupported && !nativeHlsPreferred && !preferLowLatencyHls", body)

    def test_offline_channel_still_without_twitch_player(self):
        body = self._assert_server_stream_only(self._get("/live/watch/demochan", is_live=False))
        self.assertIn("offlineBanner", body)


class EmbedOnTests(_WatchPageBase):
    embed = True

    def test_desktop_default_is_the_twitch_embed(self):
        resp = self._get("/live/watch/demochan")
        body = resp.get_data(as_text=True)
        self.assertIn("player.twitch.tv", body)
        self.assertIn("<iframe", body)
        self.assertNotIn('id="proxyPlayer"', body)

    def test_proxy_param_switches_to_the_server_stream(self):
        self._assert_server_stream_only(self._get("/live/watch/demochan?proxy=1"))

    def test_mobile_keeps_the_server_stream(self):
        self._assert_server_stream_only(self._get("/live/watch/demochan", IPHONE_UA))

    def test_context_has_embed_urls(self):
        context = self._context("/live/watch/demochan")
        self.assertFalse(context["use_hls_proxy"])
        for key in EMBED_ONLY_KEYS:
            self.assertIn(key, context)
        self.assertIn("player.twitch.tv", context["player_url"])

    def test_csp_allows_twitch_frames(self):
        resp = self._get("/live/watch/demochan")
        csp = resp.headers.get("Content-Security-Policy") or resp.headers.get(
            "Content-Security-Policy-Report-Only", "")
        self.assertIn("frame-src 'self' https://player.twitch.tv https://www.twitch.tv;", csp)
        self.assertIn("child-src 'self' blob: https://player.twitch.tv https://www.twitch.tv;", csp)


if __name__ == "__main__":
    unittest.main()
