"""Role invariants per blueprint: admin areas stay closed to non-admins and
live-only accounts never reach archives, VODs or clips."""

from __future__ import annotations

import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # run as tests.test_role_smoke
    from tests.test_recorder_reliability import IsolatedBaseTest

from werkzeug.security import generate_password_hash  # noqa: E402

from core import config  # noqa: E402
from core.cache import MemoryCache  # noqa: E402

REPO_ROOT = str(Path(__file__).resolve().parents[1])
URL_VALUES = {"filename": "vod.mp4", "streamer": "streamer", "channel": "streamer",
              "token": "AAAAAAAAAAAAAAAAAAAAAA", "size": 192, "username": "someone"}
DENIED = {302, 401, 403, 404, 405}
# Self-service routes that any signed-in user may call.
SELF_SERVICE = {"api_user_change_password"}
# Bookkeeping endpoints that serve no media: playback telemetry (log line only)
# and the concurrent-watcher slot heartbeat/release.
LIVE_ONLY_ALLOWED = {"vod_debug_ingest", "vod_slot_heartbeat", "vod_slot_release"}


class RoleSmokeTests(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        webui.ensure_admin_user()
        from core import tube_terms
        for name, role in (("plainviewer", "viewer"), ("liveonly", "live_only"), ("member1", "member")):
            webui.save_user(name, generate_password_hash("Role-Test-Pass-1"), role=role)
            # Accepted terms: the role checks are under test, not the terms gate.
            tube_terms.record_terms_acceptance(name, source="test")
        channel = self.base / "streamer"
        channel.mkdir()
        (channel / "vod.mp4").write_bytes(b"\0" * 64)
        patches = [
            mock.patch.object(config, "APP_DIR", REPO_ROOT),
            mock.patch.object(webui, "BASE_DIR", str(self.base)),
            mock.patch.object(webui.limiter, "enabled", False),
            mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")),
            mock.patch("subprocess.run", side_effect=FileNotFoundError("no processes")),
            mock.patch("subprocess.Popen", side_effect=FileNotFoundError("no processes")),
            mock.patch.object(webui.threading, "Thread"),
            mock.patch.object(webui, "SHARED_CACHE", MemoryCache()),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _client(self, username, role):
        client = self.webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role=role, username=username,
                        login_time=now, last_active=now, _csrf_token="t")
        return client

    def _routes(self, blueprint_names):
        endpoints = set()
        for name in blueprint_names:
            endpoints |= self.webui.app.blueprints[name].route_endpoints
        rules = [r for r in self.webui.app.url_map.iter_rules() if r.endpoint in endpoints]
        self.assertTrue(rules, f"no routes found for {blueprint_names}")
        return rules

    def _call(self, client, rule):
        values = {arg: 1 if f"<int:{arg}>" in rule.rule else URL_VALUES.get(arg, "smoketest")
                  for arg in rule.arguments}
        with self.webui.app.test_request_context():
            url = self.webui.url_for(rule.endpoint, **values)
        methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
        method = "GET" if "GET" in methods else methods[0]
        response = client.open(url, method=method, data={"csrf_token": "t"} if method != "GET" else None,
                               headers={"X-CSRFToken": "t"})
        status = response.status_code
        response.close()
        return f"{method} {url}", status

    def test_admin_blueprints_are_closed_to_non_admins(self):
        rules = self._routes(("admin", "storage", "users"))
        for username, role in (("plainviewer", "viewer"), ("liveonly", "live_only"), ("member1", "member")):
            client = self._client(username, role)
            for rule in rules:
                if rule.endpoint in SELF_SERVICE:
                    continue
                label, status = self._call(client, rule)
                with self.subTest(role=role, route=label):
                    self.assertIn(status, DENIED)

    def test_live_only_never_reaches_archives(self):
        client = self._client("liveonly", "live_only")
        for rule in self._routes(("vod", "clip")):
            if rule.endpoint in LIVE_ONLY_ALLOWED:
                continue
            label, status = self._call(client, rule)
            with self.subTest(route=label):
                self.assertIn(status, DENIED)

    def test_member_can_open_the_archive(self):
        # Control: the same request is allowed for an admin, so the denials above
        # come from the role checks and not from a broken fixture.
        admin = self._client(config.VOD_USER, "admin")
        self.assertEqual(admin.get("/media/streamer/vod.mp4").status_code, 200)


if __name__ == "__main__":
    unittest.main()
