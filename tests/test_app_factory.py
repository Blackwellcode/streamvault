"""App factory, blueprints and a smoke run over every registered route (S2)."""

from __future__ import annotations

import time
import unittest
import urllib.error
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # run as tests.test_app_factory
    from tests.test_recorder_reliability import IsolatedBaseTest

from core import config  # noqa: E402
from core.cache import MemoryCache, RedisCache  # noqa: E402

REPO_ROOT = str(__import__("pathlib").Path(__file__).resolve().parents[1])

# Placeholder values per URL variable name; anything else gets "smoketest".
URL_VALUES = {
    "filename": "vod.mp4",
    "streamer": "streamer",
    "channel": "streamer",
    "token": "AAAAAAAAAAAAAAAAAAAAAA",
    "size": 192,
    "group_id": 999,
    "item_id": 1,
    "id": 1,
}


def _rules(flask_app):
    return sorted(
        (rule.rule, rule.endpoint, tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})))
        for rule in flask_app.url_map.iter_rules()
    )


class AppFactoryTests(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui

    def test_module_app_is_built_by_the_factory(self):
        fresh = self.webui.create_app({"TESTING": True})
        self.assertIsNot(fresh, self.webui.app)
        self.assertEqual(_rules(fresh), _rules(self.webui.app))
        self.assertTrue(fresh.config["TESTING"])
        self.assertEqual(set(fresh.blueprints), {bp.name for bp in self.webui.BLUEPRINTS})

    def test_endpoint_names_are_unprefixed(self):
        endpoints = {rule.endpoint for rule in self.webui.app.url_map.iter_rules()}
        self.assertFalse({e for e in endpoints if "." in e})
        for name in ("login", "logout", "index", "vod_view", "media", "public_media",
                     "admin_view", "healthz", "files_upload", "tts_api_ack"):
            with self.subTest(endpoint=name):
                self.assertIn(name, endpoints)

    def test_routes_are_grouped_by_feature(self):
        # Each route registration is one deferred setup function on its blueprint.
        by_endpoint = {name: len(bp.deferred_functions) for name, bp in self.webui.app.blueprints.items()}
        for name in ("auth", "admin", "storage", "users", "vod", "clip", "live", "overlays", "files"):
            with self.subTest(blueprint=name):
                self.assertGreater(by_endpoint.get(name, 0), 0)

    def test_app_wide_hooks_still_apply(self):
        response = self.webui.app.test_client().get("/login")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertTrue(
            response.headers.get("Content-Security-Policy")
            or response.headers.get("Content-Security-Policy-Report-Only")
        )


class SharedCacheTests(IsolatedBaseTest):
    def test_memory_cache_ttl(self):
        now = [100.0]
        cache = MemoryCache(clock=lambda: now[0])
        cache.set("a", [1], ttl=10)
        cache.set("b", "forever")
        self.assertEqual(cache.get("a"), [1])
        now[0] = 110.0
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("a", "dflt"), "dflt")
        self.assertEqual(cache.get("b"), "forever")
        self.assertTrue(cache.delete("b"))
        self.assertFalse(cache.delete("b"))

    def test_redis_cache_delegates_to_redis_client(self):
        from core import redis_client
        with mock.patch.object(redis_client, "get_state", return_value=None) as get_state, \
                mock.patch.object(redis_client, "set_state", return_value=True) as set_state, \
                mock.patch.object(redis_client, "delete_state", return_value=True) as delete_state:
            cache = RedisCache()
            self.assertEqual(cache.get("k", "d"), None)
            cache.set("k", {"v": 1}, ttl=60)
            cache.delete("k")
        get_state.assert_called_once_with("k", "d")
        set_state.assert_called_once_with("k", {"v": 1}, ex=60)
        delete_state.assert_called_once_with("k")

    def test_streamer_list_comes_from_the_shared_cache(self):
        from WEBUI import webui
        shared = MemoryCache()
        shared.set("streamers_list", [{"name": "from_other_worker"}], ttl=60)
        with mock.patch.object(webui, "SHARED_CACHE", shared), \
                mock.patch.dict(webui.STREAMER_CACHE, {"ts": 0, "token": None, "data": []}):
            self.assertEqual(webui.list_streamers(), [{"name": "from_other_worker"}])

    def test_computed_streamer_list_is_published(self):
        from WEBUI import webui
        shared = MemoryCache()
        with mock.patch.object(webui, "SHARED_CACHE", shared), \
                mock.patch.object(webui, "BASE_DIR", str(self.base)), \
                mock.patch.dict(webui.STREAMER_CACHE, {"ts": 0, "token": None, "data": []}):
            result = webui.list_streamers()
        self.assertEqual(shared.get("streamers_list"), result)


class RouteSmokeTests(IsolatedBaseTest):
    """Every registered route answers without a server error (admin session).

    External effects are cut at the system boundary: no network, no child
    processes, no background threads.
    """

    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        webui.ensure_admin_user()
        patches = [
            # Legal texts ship with the code; only the data directory is temporary.
            mock.patch.object(config, "APP_DIR", REPO_ROOT),
            mock.patch.object(webui, "BASE_DIR", str(self.base)),
            mock.patch.object(webui.limiter, "enabled", False),
            mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline (smoke test)")),
            mock.patch("subprocess.run", side_effect=FileNotFoundError("no processes in smoke test")),
            mock.patch("subprocess.Popen", side_effect=FileNotFoundError("no processes in smoke test")),
            mock.patch.object(webui.threading, "Thread"),
            mock.patch.object(webui, "SHARED_CACHE", MemoryCache()),
        ]
        try:
            import requests
            patches.append(mock.patch.object(requests.Session, "request",
                                             side_effect=requests.ConnectionError("offline (smoke test)")))
        except ImportError:
            pass
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _client(self):
        client = self.webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        return client

    def test_every_route_answers_without_server_error(self):
        failures = []
        checked = 0
        for rule in self.webui.app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            values = {
                arg: 1 if f"<int:{arg}>" in rule.rule else URL_VALUES.get(arg, "smoketest")
                for arg in rule.arguments
            }
            try:
                with self.webui.app.test_request_context():
                    url = self.webui.url_for(rule.endpoint, **values)
            except Exception as exc:  # report URL building problems as failures, keep going
                failures.append(f"{rule.rule}: url_for failed: {exc!r}")
                continue
            methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
            method = "GET" if "GET" in methods else methods[0]
            client = self._client()
            try:
                response = client.open(
                    url,
                    method=method,
                    data={"csrf_token": "t"} if method != "GET" else None,
                    headers={"X-CSRFToken": "t"},
                )
                status = response.status_code
                response.close()
            except Exception as exc:  # TESTING propagates view errors: that is the 500 case
                failures.append(f"{method} {url}: {type(exc).__name__}: {exc}")
                continue
            checked += 1
            if status >= 500 and status != 503:
                failures.append(f"{method} {url}: HTTP {status}")
        self.assertGreater(checked, 150)
        self.assertEqual(failures, [], "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
