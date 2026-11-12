"""Former `except Exception: pass` paths: specific error classes, logged.

Each test pins two things: the expected failure is still absorbed (the caller
keeps its fallback) and it now leaves a log line behind.
The environment bootstrap (test credentials, temp BASE_DIR, fcntl shim on
Windows) lives in test_recorder_reliability and runs on import.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import time
import unittest
import urllib.error
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401
except ImportError:  # run as tests.test_error_handling
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401

from core import config  # noqa: E402
from core import chat_chunker, live_watch_diag, redis_client, tube_auth  # noqa: E402
from core import twitch_api, twitch_eventsub  # noqa: E402
from core.storage import http as storage_http  # noqa: E402
from core.storage.providers import google_drive as google_module  # noqa: E402
from core.storage.providers.google_drive import GoogleDriveProvider  # noqa: E402


class StorageResponseCloseTests(unittest.TestCase):
    def test_close_failure_is_logged_with_provider(self):
        response = mock.Mock()
        response.close.side_effect = ConnectionResetError("reset by peer")
        with self.assertLogs("storage.http", level="DEBUG") as logs:
            storage_http.close_quietly(response, provider="dropbox")
        self.assertTrue(any("dropbox" in line and "reset by peer" in line for line in logs.output))

    def test_unexpected_close_error_is_not_swallowed(self):
        response = mock.Mock()
        response.close.side_effect = RuntimeError("programming error")
        with self.assertRaises(RuntimeError):
            storage_http.close_quietly(response, provider="dropbox")

    def test_google_drive_401_retry_survives_a_failing_close(self):
        class Unauthorized:
            status_code = 401
            reason = "Unauthorized"
            text = "denied"
            content = b""
            headers = {}

            def json(self):
                raise ValueError("not json")

            def close(self):
                raise ConnectionResetError("socket gone")

        provider = GoogleDriveProvider({})
        token_calls = []
        provider._token = lambda force=False: token_calls.append(bool(force)) or "token"
        requests = []

        def fake_request(method, url, **kwargs):
            requests.append((method, url))
            return Unauthorized()

        with mock.patch.object(google_module.http, "request", fake_request), \
                self.assertLogs("storage.http", level="DEBUG"):
            response = provider._api("GET", "/files")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(requests), 2)
        self.assertIn(True, token_calls, "the 401 must still force a token refresh")


def _http_error_with_broken_body(code):
    exc = urllib.error.HTTPError("https://api.twitch.tv/helix/x", code, "err", {}, io.BytesIO(b""))
    exc.read = mock.Mock(side_effect=ConnectionResetError("body lost"))
    return exc


class TwitchHttpErrorBodyTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("get_app_access_token", lambda: "app-token"),
                            ("TWITCH_CLIENT_ID", "client")):
            patcher = mock.patch.object(twitch_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_helix_get_logs_unreadable_error_body_and_returns_none(self):
        with mock.patch.object(twitch_api.urllib.request, "urlopen",
                               side_effect=_http_error_with_broken_body(404)),                 self.assertLogs(twitch_api.logger, level="DEBUG") as logs:
            self.assertIsNone(twitch_api.helix_get("streams", {"user_login": "x"}))
        self.assertTrue(any("body lost" in line for line in logs.output))

    def test_eventsub_post_logs_unreadable_error_body_and_returns_none(self):
        with mock.patch.object(twitch_eventsub.urllib.request, "urlopen",
                               side_effect=_http_error_with_broken_body(409)),                 self.assertLogs(twitch_eventsub.logger, level="DEBUG") as logs:
            self.assertIsNone(twitch_eventsub._helix_post("eventsub/subscriptions", {}))
        self.assertTrue(any("body lost" in line for line in logs.output))


class CoreDatabaseFallbackTests(IsolatedBaseTest):
    def test_vod_access_bases_fall_back_to_defaults_and_log(self):
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(tube_auth.database, "list_role_vod_access", side_effect=locked),                 self.assertLogs("core.tube_auth", level="WARNING"):
            bases = tube_auth.effective_vod_access_bases()
        self.assertEqual(bases, sorted(tube_auth._VOD_ACCESS_SEED_BASES))

    def test_live_diag_candidates_survive_database_error(self):
        from core import database
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(database, "list_channel_configs", side_effect=locked),                 self.assertLogs("core.live_watch_diag", level="WARNING"):
            names = live_watch_diag.list_candidate_channels(extra=["Extra"])
        self.assertIn("extra", names)

    def test_live_diag_channel_report_survives_database_error(self):
        from core import database
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(database, "get_channel_config", side_effect=locked),                 mock.patch.object(live_watch_diag, "streamlink_counts", return_value={}),                 mock.patch.object(live_watch_diag, "ffmpeg_hls_pids", return_value={}),                 self.assertLogs("core.live_watch_diag", level="WARNING"):
            report = live_watch_diag.diagnose_channel("chan", helix={"chan": {}})
        self.assertEqual(report["db"], {"live": None, "record": None})


class ChatChunkCacheTests(IsolatedBaseTest):
    def test_corrupt_cached_meta_is_logged_and_rebuilt(self):
        log_path = self.base / "chan" / "chat_logs" / "vod.jsonl"
        log_path.parent.mkdir(parents=True)
        log_path.write_text(json.dumps({"t": 1.0, "msg": "hi"}) + "\n", encoding="utf-8")
        chunks_dir = chat_chunker.get_chunks_dir(str(self.base), "chan", "vod.mp4")
        os.makedirs(chunks_dir)
        meta = os.path.join(chunks_dir, "meta.json")
        with open(meta, "w", encoding="utf-8") as handle:
            handle.write("{truncated")
        os.utime(meta, (os.path.getmtime(log_path) + 60,) * 2)
        with self.assertLogs("streamvault.chat_chunker", level="WARNING"):
            result = chat_chunker.ensure_chat_chunks(str(self.base), "chan", "vod.mp4", str(log_path))
        self.assertTrue(result.get("ok"), result)
        with open(meta, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["total_chunks"], result["total_chunks"])


class RedisDashboardCacheTests(unittest.TestCase):
    def test_shm_cache_removal_failure_is_logged(self):
        with mock.patch.object(redis_client, "delete_state", return_value=False),                 mock.patch.object(redis_client, "publish_event", return_value=False),                 mock.patch.object(redis_client.os.path, "exists", return_value=True),                 mock.patch.object(redis_client.os, "remove", side_effect=PermissionError("busy")),                 self.assertLogs("streamvault.redis", level="WARNING"):
            self.assertTrue(redis_client.invalidate_dashboard_cache())



def _admin_client(webui):
    webui.app.config.update(TESTING=True)
    client = webui.app.test_client()
    with client.session_transaction() as sess:
        now = time.time()
        sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                    login_time=now, last_active=now, _csrf_token="t")
    return client


class DeletedMediaTombstoneTests(IsolatedBaseTest):
    """A tombstoned VOD stays hidden even while its file is still on disk."""

    def setUp(self):
        super().setUp()
        from core import database
        from WEBUI import webui
        self.webui = webui
        self.database = database
        patcher = mock.patch.object(webui, "BASE_DIR", str(self.base))
        patcher.start()
        self.addCleanup(patcher.stop)
        webui.ensure_admin_user()
        archive = self.base / "streamer"
        archive.mkdir()
        (archive / "vod.mp4").write_bytes(b"\0" * 32)

    def test_tombstoned_vod_page_is_404(self):
        client = _admin_client(self.webui)
        self.assertEqual(client.get("/vod/streamer/vod.mp4").status_code, 200)
        self.database.upsert_deleted_media("streamer", "vod.mp4", deleted_by="admin")
        self.assertEqual(client.get("/vod/streamer/vod.mp4").status_code, 404)

    def test_tombstone_lookup_failure_is_logged_and_page_still_served(self):
        client = _admin_client(self.webui)
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(self.webui.database, "is_deleted_media", side_effect=locked),                 self.assertLogs(level="WARNING") as logs:
            response = client.get("/vod/streamer/vod.mp4")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("database is locked" in line for line in logs.output))



class WebuiLookupFailureTests(IsolatedBaseTest):
    """Web UI lookups that used to swallow every error now log specific ones."""

    def setUp(self):
        super().setUp()
        from werkzeug.security import generate_password_hash

        from WEBUI import webui
        self.webui = webui
        patcher = mock.patch.object(webui, "BASE_DIR", str(self.base))
        patcher.start()
        self.addCleanup(patcher.stop)
        webui.ensure_admin_user()
        webui.save_user("plainviewer", generate_password_hash("Role-Test-Pass-1"), role="viewer")
        archive = self.base / "streamer"
        archive.mkdir()
        (archive / "vod.mp4").write_bytes(b"\0" * 32)
        self.locked = sqlite3.OperationalError("database is locked")

    def _viewer_request(self):
        ctx = self.webui.app.test_request_context("/")
        ctx.push()
        self.addCleanup(ctx.pop)
        now = time.time()
        self.webui.session.update(logged_in=True, user_role="viewer", username="plainviewer",
                                  login_time=now, last_active=now)

    def test_maintenance_bypass_check_failure_is_logged_and_denied(self):
        from core import site_maintenance
        self._viewer_request()
        with mock.patch.object(site_maintenance, "is_user_exempt", side_effect=OSError("state unreadable")), \
                self.assertLogs(level="WARNING") as logs:
            self.assertFalse(self.webui.current_user_bypasses_maintenance())
        self.assertTrue(any("state unreadable" in line for line in logs.output))

    def test_viewer_access_check_survives_channel_lookup_failure(self):
        self._viewer_request()
        expected = self.webui.can_view_streamer("streamer")
        with mock.patch.object(self.webui, "related_archive_slugs", side_effect=self.locked), \
                self.assertLogs(level="WARNING") as logs:
            self.assertEqual(self.webui.can_view_streamer("streamer"), expected)
        self.assertTrue(any("database is locked" in line for line in logs.output))

    def test_streamer_folders_survive_channel_config_failure(self):
        with mock.patch.object(self.webui.database, "list_channel_configs", side_effect=self.locked), \
                self.assertLogs(level="WARNING"):
            rows = self.webui.list_all_streamer_folders()
        self.assertIn("streamer", [row["name"] for row in rows])

    def test_media_path_resolves_when_alias_lookup_fails(self):
        with mock.patch.object(self.webui, "related_archive_slugs", side_effect=self.locked), \
                self.assertLogs(level="WARNING"):
            resolved = self.webui.resolve_media_path("streamer", "vod.mp4", "vod")
        self.assertEqual(os.path.normpath(resolved), os.path.normpath(str(self.base / "streamer" / "vod.mp4")))

    def test_vod_access_candidates_survive_failures(self):
        with mock.patch.object(self.webui, "list_streamers", side_effect=OSError("archive unreadable")), \
                mock.patch.object(self.webui.database, "list_vod_access_defaults", side_effect=self.locked), \
                self.assertLogs(level="WARNING") as logs:
            self.assertEqual(self.webui._vod_access_candidate_slugs(), [])
        self.assertEqual(len(logs.output), 2)

    def test_recording_logins_survive_database_error(self):
        with mock.patch.object(self.webui.database, "list_channel_configs", side_effect=self.locked), \
                self.assertLogs(level="WARNING"):
            self.assertEqual(self.webui._recording_logins_set(), set())

    def test_twitch_profile_lookup_failure_is_logged(self):
        offline = urllib.error.URLError("offline")
        with mock.patch.object(self.webui.urllib.request, "urlopen", side_effect=offline), \
                self.assertLogs(level="WARNING"):
            self.assertEqual(self.webui.get_twitch_profile_and_banner("someone"), (None, None))

    def test_twitch_profile_unexpected_payload_is_no_profile(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data": null}'
        with mock.patch.object(self.webui.urllib.request, "urlopen", return_value=response):
            self.assertEqual(self.webui.get_twitch_profile_and_banner("someone"), (None, None))

    def test_twitch_user_id_failure_is_logged_and_cached_as_unknown(self):
        self.webui.TWITCH_USER_ID_CACHE.pop("someone", None)
        self.addCleanup(self.webui.TWITCH_USER_ID_CACHE.pop, "someone", None)
        with mock.patch.object(self.webui.urllib.request, "urlopen", side_effect=TimeoutError("slow")), \
                self.assertLogs(level="WARNING"):
            self.assertIsNone(self.webui.get_twitch_user_id("someone"))
        self.assertIsNone(self.webui.TWITCH_USER_ID_CACHE["someone"]["id"])

    def test_delete_still_removes_file_and_writes_tombstone_when_alias_lookup_fails(self):
        self._viewer_request()
        with mock.patch.object(self.webui, "related_archive_slugs", side_effect=self.locked), \
                self.assertLogs(level="WARNING") as logs:
            self.assertTrue(self.webui.delete_video_file("streamer", "vod.mp4", False))
        # media lookup, tombstone aliases, thumbnail aliases
        self.assertEqual(sum("database is locked" in line for line in logs.output), 3)
        self.assertFalse((self.base / "streamer" / "vod.mp4").exists())
        self.assertTrue(self.webui.database.is_deleted_media(streamer="streamer", filename="vod.mp4"))


if __name__ == "__main__":
    unittest.main()
