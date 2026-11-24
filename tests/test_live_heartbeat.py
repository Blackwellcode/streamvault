"""Live demand markers and live flags: safe modes, valid names, right platform, admin rows intact."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # run as tests.test_live_heartbeat
    from tests.test_recorder_reliability import IsolatedBaseTest

from core import config, database, twitch_api  # noqa: E402

CHANNEL = "demochan"
BAD_NAMES = ("%2e", "%2e%2e", "%0A", "demochan%0A", "demo%0Achan", "%20demochan", "demo%20chan",
             "demo-chan", "..%5cx", "dem%C3%B6chan", "a" * 26, "a" * 65)


class HeartbeatBase(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        webui.app.config.update(TESTING=True)
        webui.ensure_admin_user()
        patcher = mock.patch.object(config, "LIVE_HLS_ROOT", str(self.base / "live_hls"))
        patcher.start()
        self.addCleanup(patcher.stop)
        for state in (webui._LIVE_ASSERT_TS, webui._RATE_LIMITED_LAST):
            state.clear()
            self.addCleanup(state.clear)
        self.live_dir = os.path.join(config.LIVE_HLS_ROOT, CHANNEL)
        self.demand_dir = os.path.join(config.META_DIR, "live_watch_demand")
        self.markers = (os.path.join(self.live_dir, ".heartbeat"),
                        os.path.join(self.demand_dir, CHANNEL + ".ts"))
        self.client = webui.app.test_client()
        with self.client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")

    def _row(self, name=CHANNEL):
        with database.db_transaction() as conn:
            row = conn.execute("SELECT record, live, notify FROM channels WHERE name=?", (name,)).fetchone()
        return tuple(row) if row else None


class MarkerTests(HeartbeatBase):
    def test_both_markers_are_written_and_refreshed(self):
        self.webui.touch_live_heartbeat("DemoChan")
        for path in self.markers:
            os.utime(path, (1, 1))
        self.webui.touch_live_heartbeat(CHANNEL)
        for path in self.markers:
            with self.subTest(path=path):
                self.assertLess(time.time() - os.path.getmtime(path), 60)
                with open(path, encoding="ascii") as handle:
                    self.assertGreater(float(handle.read()), 1_000_000_000)

    def test_nothing_becomes_world_writable(self):
        with mock.patch.object(os, "chmod") as chmod:
            self.webui.touch_live_heartbeat(CHANNEL)
        self.assertTrue(os.path.isfile(self.markers[0]))
        chmod.assert_not_called()  # a path-based chmod follows symlinks

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_modes_are_0755_and_0644_even_with_a_strict_umask(self):
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        # Left world-writable by an older version.
        os.makedirs(self.live_dir)
        os.chmod(self.live_dir, 0o777)
        with open(self.markers[0], "w", encoding="ascii"):
            os.chmod(self.markers[0], 0o666)
        self.webui.touch_live_heartbeat(CHANNEL)
        for path, mode in ((self.live_dir, 0o755), (self.demand_dir, 0o755),
                           (self.markers[0], 0o644), (self.markers[1], 0o644)):
            with self.subTest(path=path):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), mode)

    def _symlink(self, target, link):
        try:
            os.symlink(target, link, target_is_directory=os.path.isdir(target))
        except OSError as exc:
            self.skipTest(f"symlinks not permitted: {exc}")

    def test_marker_symlink_is_not_followed(self):
        victim = self.base / "victim"
        victim.write_text("keep", encoding="utf-8")
        os.makedirs(self.demand_dir)
        self._symlink(str(victim), self.markers[1])
        mode = stat.S_IMODE(os.stat(victim).st_mode)
        with self.assertLogs(level="WARNING") as logs:
            self.webui.touch_live_heartbeat(CHANNEL)
        self.assertIn("Live heartbeat for demochan failed", "".join(logs.output))
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
        self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), mode)

    def test_directory_symlink_is_not_followed(self):
        victim = self.base / "victimdir"
        victim.mkdir()
        os.makedirs(config.LIVE_HLS_ROOT)
        self._symlink(str(victim), self.live_dir)
        with self.assertLogs(level="WARNING"):
            self.webui.touch_live_heartbeat(CHANNEL)
        self.assertEqual(os.listdir(victim), [])

    def test_symlinks_are_refused_without_o_nofollow(self):
        # Windows branch, also without the privilege to create symlinks.
        for target in (self.live_dir, self.markers[1]):
            with self.subTest(target=target), \
                    mock.patch.object(self.webui.os.path, "islink", side_effect=lambda p, t=target: os.path.normpath(p) == t), \
                    self.assertLogs(level="WARNING") as logs:
                if hasattr(os, "O_NOFOLLOW") and target != self.live_dir:
                    self.skipTest("the kernel checks the file there")
                self.webui._RATE_LIMITED_LAST.clear()
                self.webui.touch_live_heartbeat(CHANNEL)
                self.assertIn("is a symlink", "".join(logs.output))

    def test_unwritable_directory_is_logged(self):
        with mock.patch.object(self.webui.os, "makedirs", side_effect=PermissionError("denied")), \
                self.assertLogs(level="WARNING") as logs:
            self.webui.touch_live_heartbeat(CHANNEL)
        self.assertIn("Live heartbeat for demochan failed", "".join(logs.output))

    def test_invalid_names_create_nothing(self):
        for name in (None, "", ".", "..", "\n", "demochan\n", "demo\nchan", "a/b", "a\\b", "demöchan", "a" * 26, "a" * 65):
            with self.subTest(name=name):
                self.webui.touch_live_heartbeat(name)
        self.assertFalse(os.path.exists(config.LIVE_HLS_ROOT))
        self.assertFalse(os.path.exists(self.demand_dir))

    def test_youtube_rows_create_no_twitch_demand(self):
        self.webui.add_channel_db(CHANNEL, platform="youtube", source_url="https://youtube.com/@demochan")
        self.webui.touch_live_heartbeat(CHANNEL)
        self.assertFalse(os.path.exists(self.live_dir))
        self.assertFalse(os.path.exists(self.demand_dir))
        self.webui.add_channel_db("twitchchan", platform="twitch")
        self.webui.touch_live_heartbeat("twitchchan")
        self.assertEqual(os.listdir(self.demand_dir), ["twitchchan.ts"])

    def test_database_outage_keeps_the_demand(self):
        with mock.patch.object(database, "get_channel_config", side_effect=sqlite3.OperationalError("locked")), \
                mock.patch.object(self.webui, "add_live_channel"), self.assertLogs(level="WARNING"):
            self.webui.touch_live_heartbeat(CHANNEL)
        self.assertTrue(os.path.isfile(self.markers[1]))


class LiveRouteNameTests(HeartbeatBase):
    def _assert_nothing_created(self):
        self.assertFalse(os.path.exists(config.LIVE_HLS_ROOT))
        self.assertFalse(os.path.exists(self.demand_dir))

    def test_invalid_names_are_not_found(self):
        for name in BAD_NAMES:
            for route in ("/live/playlist/{}", "/live/segment/{}/seg_1.ts", "/api/live/ready/{}", "/live/obs/{}"):
                with self.subTest(name=name, route=route):
                    with mock.patch.object(twitch_api, "get_stream", return_value={}):
                        self.assertEqual(self.client.get(route.format(name)).status_code, 404)
        self._assert_nothing_created()

    def test_watch_and_studio_pages_reject_names_without_a_channel(self):
        for name in ("%2e", "%2e%2e", "%0A", "%C3%B6", "-"):
            for route in ("/live/watch/{}", "/live/{}?studio=1", "/live/{}"):
                with self.subTest(name=name, route=route):
                    self.assertEqual(self.client.get(route.format(name)).status_code, 404)
        self._assert_nothing_created()

    def test_clip_routes_reject_names_without_a_channel(self):
        for route in ("/clip/live/{}", "/clip/instant/{}"):
            for name in ("%2e", "%0A", "%C3%B6"):
                with self.subTest(route=route, name=name):
                    resp = self.client.post(route.format(name), data={
                        "csrf_token": "t", "duration": "30", "clip_streamer": CHANNEL})
                    self.assertEqual(resp.status_code, 404)
        self._assert_nothing_created()

    def test_playlist_accepts_any_case(self):
        os.makedirs(self.live_dir)
        with open(os.path.join(self.live_dir, "stream.m3u8"), "w", encoding="ascii") as handle:
            handle.write("#EXTM3U\nseg_1.ts\n")
        resp = self.client.get("/live/playlist/DemoChan")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("/live/segment/demochan/seg_1.ts", resp.get_data(as_text=True))


class LiveFlagTests(HeartbeatBase):
    def test_watching_does_not_mark_a_live_only_channel_as_live(self):
        self.webui.add_channel_db(CHANNEL, record=0)
        self.webui.add_live_channel(CHANNEL)
        self.webui.touch_live_heartbeat(CHANNEL)
        self.assertEqual(self._row(), (0, 0, 1))

    def test_watching_marks_recorded_and_visitor_channels(self):
        self.webui.add_channel_db("recorded", record=1)
        self.webui.add_live_channel("recorded")
        self.webui.add_live_channel("visitor")
        self.assertEqual(self._row("recorded"), (1, 1, 1))
        self.assertEqual(self._row("visitor"), (0, 1, 0))
        with database.db_transaction() as conn:
            conn.execute("UPDATE channels SET live=0 WHERE name='visitor'")
        self.webui.add_live_channel("visitor")
        self.assertEqual(self._row("visitor"), (0, 1, 0))

    def test_admin_start_still_sets_the_flag(self):
        self.webui.add_channel_db(CHANNEL, record=0)
        resp = self.client.post(f"/live/control/start/{CHANNEL}", data={"csrf_token": "t"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._row(), (0, 1, 1))

    def test_admin_stop_keeps_configured_channels(self):
        self.webui.add_channel_db(CHANNEL, record=0, live=1)
        self.webui.add_channel_db("recorded", record=1, live=1)
        self.webui.add_live_channel("visitor")
        for name in (CHANNEL, "recorded", "visitor"):
            self.client.post(f"/live/control/stop/{name}", data={"csrf_token": "t"})
        self.assertEqual(self._row(), (0, 0, 1))
        self.assertEqual(self._row("recorded"), (1, 0, 1))
        self.assertIsNone(self._row("visitor"))

    def test_a_row_without_notify_value_counts_as_configured(self):
        self.webui.add_channel_db(CHANNEL, record=0, live=1)
        with database.db_transaction() as conn:
            conn.execute("UPDATE channels SET notify=NULL WHERE name=?", (CHANNEL,))
        self.webui.remove_live_channel(CHANNEL)
        self.webui.add_live_channel(CHANNEL)
        self.assertEqual(self._row(), (0, 0, None))

    def test_locked_database_is_retried(self):
        real = database.db_transaction
        for func in (self.webui.add_live_channel, self.webui.remove_live_channel):
            with self.subTest(func=func.__name__), \
                    mock.patch.object(database.time, "sleep"), \
                    mock.patch.object(database, "db_transaction", side_effect=[
                        sqlite3.OperationalError("database is locked"), real()]) as transaction:
                func("visitor")
                self.assertEqual(transaction.call_count, 2)


if __name__ == "__main__":
    unittest.main()
