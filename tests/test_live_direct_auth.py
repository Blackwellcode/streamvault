"""nginx auth_request target for the direct live routes.

nginx serves /live/<channel>.ts (MPEG-TS fan-out) and the LL-HLS files
/live/<channel>/{stream,index}.m3u8 and seg_N.ts itself; every request is
checked first by GET /api/live/auth with the original URI in X-Original-URI.
"""

from __future__ import annotations

import pathlib
import re
import time
import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401
except ImportError:  # run as tests.test_live_direct_auth
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401

from core import config, twitch_api  # noqa: E402

AUTH = "/api/live/auth"
MARKER = "X-StreamVault-Live-Auth"
MOBILE_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Mobile Safari/537.36"
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

    def _auth(self, client, uri, **headers):
        # nginx marks its auth subrequest; without the marker Flask answers 404.
        headers = {"X-Original-URI": uri, MARKER: "1", **headers}
        return client.get(AUTH, headers=headers)

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

    def test_request_without_nginx_marker_is_not_found(self):
        for marker in (None, "", "0", "true"):
            with self.subTest(marker=marker):
                headers = {"X-Original-URI": "/live/demochan.ts"}
                if marker is not None:
                    headers[MARKER] = marker
                resp = self.user.get(AUTH, headers=headers)
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.get_data(), b"")

    def test_uri_is_parsed_as_path_only(self):
        # urlsplit read "//host/live/x.ts" as netloc "host" plus a matching path.
        for uri in ("//evil.example/live/demochan.ts", "http://h/live/demochan.ts",
                    "/live/demochan.ts#x", "/x/live/demochan.ts"):
            with self.subTest(uri=uri):
                self.assertEqual(self._auth(self.user, uri).status_code, 403)
        self.assertEqual(self._auth(self.user, "/live/demochan.ts?token=a/b").status_code, 204)
        # "$" also matched before a trailing newline (HTTP headers cannot carry one).
        self.assertIsNone(self.webui._LIVE_DIRECT_URI_RE.fullmatch("/live/demochan.ts\n"))

    def test_live_maintenance_blocks_unless_user_bypasses(self):
        with mock.patch.object(self.webui, "maintenance_mode_enabled",
                               side_effect=lambda scope="global": scope == "live"):
            with mock.patch.object(self.webui, "current_user_bypasses_maintenance", return_value=False):
                self.assertEqual(self._auth(self.user, "/live/demochan.ts").status_code, 403)
            with mock.patch.object(self.webui, "current_user_bypasses_maintenance", return_value=True):
                self.assertEqual(self._auth(self.user, "/live/demochan.ts").status_code, 204)

    def test_mobile_device_blocked_while_mobile_live_is_off(self):
        uri = "/live/demochan/stream.m3u8"
        with mock.patch.object(self.webui, "mobile_live_enabled", return_value=False):
            self.assertEqual(self._auth(self.user, uri, **{"User-Agent": MOBILE_UA}).status_code, 403)
            self.assertEqual(self._auth(self.user, uri, **{"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}).status_code, 204)
        with mock.patch.object(self.webui, "mobile_live_enabled", return_value=True):
            self.assertEqual(self._auth(self.user, uri, **{"User-Agent": MOBILE_UA}).status_code, 204)

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
                self.assertIn("proxy_set_header X-StreamVault-Live-Auth 1;", auth)

    def _location_blocks(self, text):
        """(header, body) of every location block, nested braces included."""
        blocks, current, depth = [], None, 0
        for line in text.splitlines():
            stripped = line.strip()
            if current is None:
                if stripped.startswith("location ") and stripped.endswith("{"):
                    current, depth = [stripped], 1
                continue
            current.append(stripped)
            depth += stripped.count("{") - stripped.count("}")
            if depth == 0:
                blocks.append((current[0], "\n".join(current[1:])))
                current = None
        return blocks

    def test_app_proxy_locations_clear_the_live_auth_marker(self):
        # A client must not be able to send the marker through nginx.
        for name in self.CONFIGS:
            text = (ROOT / name).read_text(encoding="utf-8")
            app_blocks = [
                (head, body) for head, body in self._location_blocks(text)
                if re.search(r"proxy_pass http://(app|127\.0\.0\.1):8080[;/]", body)
                and head != "location = /_live_auth {"
            ]
            with self.subTest(config=name):
                self.assertGreaterEqual(len(app_blocks), 1)
                for head, body in app_blocks:
                    self.assertIn('proxy_set_header X-StreamVault-Live-Auth "";', body, head)

    def test_access_log_has_no_cookies(self):
        for name in self.CONFIGS:
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(config=name):
                self.assertNotIn("http_cookie", text)
                self.assertRegex(text, r"log_format (streamvault_)?timed ")
                self.assertRegex(text, r"access_log \S+ (streamvault_)?timed;")

    def test_access_log_has_no_query_strings(self):
        # $request / $request_uri / $args would log ?token= and other secrets.
        for name in self.CONFIGS:
            text = (ROOT / name).read_text(encoding="utf-8")
            formats = re.findall(r"^log_format .*$", text, flags=re.MULTILINE)
            with self.subTest(config=name):
                self.assertEqual(len(formats), 1)
                fmt = formats[0].replace("\\$", "$")
                for variable in ("request", "request_uri", "args", "query_string", "http_cookie"):
                    self.assertNotRegex(fmt, r"\$" + variable + r"(?![A-Za-z0-9_])", variable)
                self.assertIn('"$request_method $uri $server_protocol"', fmt)


class GunicornAccessLogTests(unittest.TestCase):
    """gunicorn's default access format logs %(r)s, the request line with ?token=."""

    # All three run gunicorn through systemd or supervisor, which both expand
    # "%" themselves: the format must be written with "%%".
    CONFIGS = ("install.sh", "INSTALL.md", "docker-worker.conf")
    FORMAT = '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(f)s" "%(a)s"'

    def _gunicorn_commands(self, text):
        joined = re.sub(r"\\\n\s*", " ", text)  # shell/systemd line continuations
        return [line for line in joined.splitlines()
                if re.search(r"(ExecStart=|command=)\S*/gunicorn ", line)]

    def test_every_gunicorn_start_logs_without_query_string(self):
        for name in self.CONFIGS:
            text = (ROOT / name).read_text(encoding="utf-8")
            commands = self._gunicorn_commands(text)
            with self.subTest(config=name):
                self.assertGreaterEqual(len(commands), 1)
                for command in commands:
                    self.assertIn("--access-logfile", command)
                    match = re.search(r"--access-logformat '([^']*)'", command)
                    self.assertIsNotNone(match, command)
                    raw = match.group(1)
                    self.assertNotRegex(raw, r"(?<!%)%(?!%)", "unescaped % for systemd/supervisor")
                    fmt = raw.replace("%%", "%")
                    self.assertEqual(fmt, self.FORMAT)
                    for atom in ("%(r)s", "%(q)s"):
                        self.assertNotIn(atom, fmt)

    def test_no_other_gunicorn_start_without_format(self):
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in ("", ".sh", ".md", ".conf", ".yml", ".service"):
                continue
            if {".git", ".venv", "venv", "node_modules", "tests"} & set(path.relative_to(ROOT).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # binary file, cannot hold a start command
            for command in self._gunicorn_commands(text):
                with self.subTest(path=str(path.relative_to(ROOT))):
                    self.assertIn("--access-logformat", command)


if __name__ == "__main__":
    unittest.main()
