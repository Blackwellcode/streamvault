"""livebot decisions: demand, names, limits, commands, restarts, channels table.

Nothing here talks to Twitch: streamlink is never started and the live check is
always a fake.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401  (sets VOD_* env first)
except ImportError:  # run as tests.test_livebot
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401

from core import config, database, twitch_api  # noqa: E402
from recorder import livebot  # noqa: E402

CHANNEL = "demochan"
TOKEN = "dummy-livebot-token"


class FakeWorker:
    """StreamWorker stand-in without processes; ``alive`` decides the health check."""

    alive = True
    started = []

    def __init__(self, channel, source_command=None, runner=None):
        self.channel = channel
        self.was_ready = False
        self.viewer_count = 0
        self.stopped = False

    def start(self, use_proxy=True):
        type(self).started.append((self.channel, use_proxy, time.time()))
        return True

    def is_alive(self):
        return self.alive

    def viewers(self):
        return self.viewer_count

    def failure(self, _now):
        return "" if self.alive else "process ended"

    def playlist_ready(self):
        return False

    def log_tail(self, _name):
        return "-"

    def trim_logs(self):
        """No log files."""

    def stop(self):
        self.stopped = True
        livebot.clear_channel_files(self.channel)


class LivebotBase(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(config, "LIVE_HLS_ROOT", str(self.base / "live"))
        patcher.start()
        self.addCleanup(patcher.stop)
        livebot._LOG_THROTTLE.clear()

    def mark(self, channel=CHANNEL, age=0.0, now=None):
        """Same files as WebUI touch_live_heartbeat."""
        stamp = (time.time() if now is None else now) - age
        for path in (os.path.join(config.LIVE_HLS_ROOT, channel, ".heartbeat"),
                     os.path.join(config.META_DIR, "live_watch_demand", f"{channel}.ts")):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8"):
                os.utime(path, (stamp, stamp))

    def add_row(self, name, record=0, live=1, chat=0, notify=1):
        with database.db_transaction() as conn:
            conn.execute(
                "INSERT INTO channels (name, record, live, chat, notify) VALUES (?, ?, ?, ?, ?)",
                (name, record, live, chat, notify),
            )

    def fake_bot(self, alive=True, is_live=True):
        FakeWorker.started = []
        patchers = (mock.patch.object(livebot, "StreamWorker", FakeWorker),
                    mock.patch.object(FakeWorker, "alive", alive))
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        return livebot.LiveBot(is_live=mock.Mock(return_value=is_live))


class ChannelNameTests(LivebotBase):
    BAD = ("", ".", "..", "../x", "a/b", "a\\b", "Demo", "a-b", "a b", "a.ts", "x" * 26, "demochan\n", "café",
           "١", None, 5)

    def test_valid_names(self):
        for name in ("a", "demo_chan1", "x" * 25):
            self.assertTrue(livebot.valid_channel(name), name)

    def test_bad_names_never_reach_paths_or_commands(self):
        for name in self.BAD:
            with self.subTest(name=name):
                self.assertFalse(livebot.valid_channel(name))
                with self.assertRaises(ValueError):
                    livebot.hls_dir_for(name)
                with self.assertRaises(ValueError):
                    livebot.build_streamlink_command(name)
                self.assertFalse(livebot.has_demand(name))

    def test_markers_with_bad_names_are_ignored(self):
        self.mark("good_one")
        self.mark("Bad-Name")
        self.assertEqual(livebot.demanded_channels(), ["good_one"])


class DemandTests(LivebotBase):
    def test_either_marker_counts_until_it_is_stale(self):
        self.assertFalse(livebot.has_demand(CHANNEL))
        self.mark(age=config.LIVE_WATCH_DEMAND_SEC - 10)
        self.assertTrue(livebot.has_demand(CHANNEL))
        self.mark(age=config.LIVE_WATCH_DEMAND_SEC + 10)
        self.assertFalse(livebot.has_demand(CHANNEL))
        os.remove(os.path.join(config.META_DIR, "live_watch_demand", f"{CHANNEL}.ts"))
        os.utime(os.path.join(config.LIVE_HLS_ROOT, CHANNEL, ".heartbeat"))
        self.assertTrue(livebot.has_demand(CHANNEL))

    def test_webui_heartbeat_creates_demand(self):
        from WEBUI import webui
        with mock.patch.object(webui, "add_live_channel"):
            webui.touch_live_heartbeat(CHANNEL)
        self.assertEqual(livebot.demanded_channels(), [CHANNEL])

    def test_live_flag_alone_starts_nothing(self):
        bot = self.fake_bot()
        self.add_row(CHANNEL, record=1)
        bot.tick()
        self.assertEqual(bot.streams, {})

    def test_http_start_needs_a_fresh_marker(self):
        bot = self.fake_bot()
        self.assertEqual(bot.get_or_start(CHANNEL), (None, 404))
        self.mark(age=config.LIVE_WATCH_DEMAND_SEC + 10)
        self.assertEqual(bot.get_or_start(CHANNEL), (None, 404))
        self.assertEqual(FakeWorker.started, [])
        self.mark()
        worker, status = bot.get_or_start(CHANNEL)
        self.assertEqual((worker.channel, status), (CHANNEL, 200))
        self.assertIs(bot.get_or_start(CHANNEL)[0], worker)
        self.assertEqual(len(FakeWorker.started), 1)

    def test_pipeline_ends_without_demand_but_not_under_a_viewer(self):
        bot = self.fake_bot()
        self.mark()
        bot.tick()
        worker = bot.streams[CHANNEL]
        worker.viewer_count = 1
        self.mark(age=config.LIVE_WATCH_DEMAND_SEC + 10)
        bot.tick()
        self.assertIn(CHANNEL, bot.streams)
        worker.viewer_count = 0
        bot.tick()
        self.assertEqual(bot.streams, {})
        self.assertTrue(worker.stopped)


class LimitTests(LivebotBase):
    def test_channel_limit_for_loop_and_http(self):
        bot = self.fake_bot()
        with mock.patch.object(config, "LIVEBOT_MAX_CHANNELS", 2):
            for name in ("one", "two", "three"):
                self.mark(name)
            bot.tick()
            self.assertEqual(len(bot.streams), 2)
            missing = ({"one", "two", "three"} - set(bot.streams)).pop()
            self.assertEqual(bot.get_or_start(missing), (None, 503))
            self.assertEqual(len(bot.streams), 2)
            self.assertNotIn(missing, bot.retry, "waiting for a slot is not a failed start")

    def test_idle_pipeline_gives_up_its_slot(self):
        bot = self.fake_bot()
        with mock.patch.object(config, "LIVEBOT_MAX_CHANNELS", 1):
            self.mark("old")
            bot.tick()
            self.mark("new")
            bot.tick()
            self.assertEqual(list(bot.streams), ["old"], "a watched channel must not be replaced")
            self.mark("old", age=livebot.EVICT_IDLE_SEC + 10)
            bot.streams["old"].viewer_count = 1
            bot.tick()
            self.assertEqual(list(bot.streams), ["old"], "a connected viewer keeps the slot")
            bot.streams["old"].viewer_count = 0
            bot.tick()
            self.assertEqual(list(bot.streams), ["new"])

    def test_no_start_when_the_ram_disk_is_full(self):
        usage = mock.Mock(free=10 * 1024 * 1024)
        self.mark()
        with mock.patch.object(livebot.shutil, "disk_usage", return_value=usage), \
                mock.patch.object(livebot.SubprocessRunner, "popen") as popen:
            bot = livebot.LiveBot(is_live=mock.Mock())
            with self.assertLogs("livebot", level="ERROR") as logs:
                self.assertEqual(bot.get_or_start(CHANNEL), (None, 503))
        popen.assert_not_called()
        self.assertTrue(any("MB free" in line for line in logs.output))

    def test_no_new_pipeline_during_shutdown(self):
        bot = self.fake_bot()
        self.mark()
        bot.shutdown()
        self.assertEqual(bot.get_or_start(CHANNEL), (None, 503))


class CommandTests(LivebotBase):
    ENV_KEYS = ("TWITCH_PROXY_PLAYLIST", "STREAMLINK_PLUGIN_DIR", "STREAM_RECORDER_USE_PROXY")

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(livebot, "_tool", side_effect=lambda _env, name: name)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def _command(self, token="", **kwargs):
        with mock.patch.object(config, "LIVE_TWITCH_OAUTH_TOKEN", token):
            return livebot.build_streamlink_command(CHANNEL, **kwargs)

    def test_default_command_is_direct_and_anonymous(self):
        cmd = self._command()
        self.assertEqual(cmd[:2], ["streamlink", "--no-config"])
        self.assertEqual(cmd[-1], f"https://www.twitch.tv/{CHANNEL}")
        self.assertFalse([arg for arg in cmd if "proxy" in arg or "plugin" in arg], cmd)
        self.assertEqual(cmd[cmd.index("--default-stream") + 1].split(",")[0], "best")

    def test_token_stays_out_of_argv_and_out_of_the_recorder_config(self):
        cmd = self._command(TOKEN)
        self.assertFalse(any(TOKEN in arg for arg in cmd), cmd)
        path = cmd[cmd.index("--config") + 1]
        with open(path, encoding="utf-8") as handle:
            self.assertIn(TOKEN, handle.read())
        recorder_path = config.streamlink_twitch_config_args("recorder-token", config_dir=config.META_DIR)[1]
        self.assertNotEqual(os.path.abspath(path), os.path.abspath(recorder_path))
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_proxy_is_opt_in_and_needs_a_plugin(self):
        os.environ["TWITCH_PROXY_PLAYLIST"] = "https://proxy.example.com"
        with self.assertLogs("livebot", level="WARNING"):
            self.assertFalse([arg for arg in self._command() if "proxy" in arg])
        os.environ["STREAMLINK_PLUGIN_DIR"] = "/opt/plugins"
        cmd = self._command()
        self.assertEqual(cmd[cmd.index("--twitch-proxy-playlist") + 1], "https://proxy.example.com")
        self.assertNotIn("--twitch-proxy-playlist", self._command(use_proxy=False))
        # A token must never travel through somebody else's proxy.
        self.assertNotIn("--twitch-proxy-playlist", self._command(TOKEN))

    def test_streamlink_accepts_every_option(self):
        """Run the installed CLI with a URL it cannot handle: no network involved."""
        cmd = self._command()
        result = subprocess.run(
            [sys.executable, "-m", "streamlink", *cmd[1:-1], "--can-handle-url-no-redirect", "https://example.invalid/"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotIn("unrecognized arguments", result.stderr)
        self.assertEqual(result.returncode, 1, result.stderr)  # 1 = cannot handle, 2 = usage error

    def test_child_environment_has_no_secrets(self):
        secrets = {"VOD_PASS": "a", "VOD_SECRET_KEY": "b", "LIVE_TWITCH_OAUTH": "c", "TWITCH_CLIENT_SECRET": "d",
                   "STREAM_RECORDER_TWITCH_OAUTH": "e", "STORAGE_ENCRYPTION_KEY": "f", "STREAMLINK_TWITCH_API_HEADER": "g"}
        os.environ.update(secrets)
        env = livebot.child_env()
        self.assertFalse(set(secrets) & set(env))
        self.assertTrue(any(key.upper() == "PATH" for key in env))

    def test_ffmpeg_probe_window_and_both_outputs(self):
        cmd = livebot.build_ffmpeg_command("/x/hls")
        value = {flag: cmd[cmd.index(flag) + 1] for flag in
                 ("-probesize", "-analyzeduration", "-i", "-hls_segment_filename")}
        self.assertGreaterEqual(int(value["-probesize"]), 524288)
        self.assertGreaterEqual(int(value["-analyzeduration"]), 1000000)
        self.assertEqual(value["-i"], "pipe:0")
        self.assertEqual(value["-hls_segment_filename"], os.path.join("/x/hls", "seg_%05d.ts"))
        self.assertIn("pipe:1", cmd)
        self.assertEqual(cmd.count("copy"), 2)
        # An input option, so it has to stand before -i: remote data opens no files or URLs.
        self.assertEqual(cmd[cmd.index("-i") - 2:cmd.index("-i")], ["-protocol_whitelist", "pipe"])
        self.assertEqual(cmd[-1], os.path.join("/x/hls", "stream.m3u8"))


class ConfigTests(unittest.TestCase):
    def test_env_int(self):
        for raw, expected in (("", 4), ("7", 7), (" 9 ", 9), ("0", 1), ("99", 10)):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"LIVEBOT_TEST": raw}):
                self.assertEqual(config._env_int("LIVEBOT_TEST", 4, minimum=1, maximum=10), expected)

    def test_bad_env_int_is_logged(self):
        with mock.patch.dict(os.environ, {"LIVEBOT_TEST": "many"}), self.assertLogs(config.logger, level="WARNING"):
            self.assertEqual(config._env_int("LIVEBOT_TEST", 4), 4)

    @unittest.skipIf(any(key.startswith("LIVEBOT_") for key in os.environ), "LIVEBOT_* set in this environment")
    def test_defaults(self):
        self.assertTrue(config.LIVEBOT_ENABLED)
        self.assertEqual((config.LIVEBOT_HOST, config.LIVEBOT_PORT), ("127.0.0.1", 8085))
        self.assertEqual((config.LIVEBOT_MAX_CHANNELS, config.LIVEBOT_MAX_VIEWERS), (4, 20))


class RestartTests(LivebotBase):
    """A channel that went offline must not be restarted every few seconds."""

    def _simulate(self, seconds, live_after=None, poll_every=2.0):
        """Run the loop on a fake clock: every pipeline dies at once, the page keeps polling."""
        start = time.time()
        clock = {"now": start, "poll": start}
        checks = []

        def is_live(_channel):
            checks.append(clock["now"] - start)
            return live_after is not None and clock["now"] - start >= live_after

        bot = self.fake_bot(alive=False)
        bot.is_live = is_live
        self.mark(now=start)
        with mock.patch.object(livebot.time, "time", side_effect=lambda: clock["now"]):
            while clock["now"] - start < seconds:
                bot.tick()
                clock["now"] += 1.0
                if clock["now"] - clock["poll"] >= poll_every:
                    clock["poll"] = clock["now"]
                    self.mark(now=clock["now"])
        return bot, [stamp - start for _name, _proxy, stamp in FakeWorker.started], checks

    def test_offline_channel_is_rechecked_without_starting_streamlink(self):
        _bot, starts, checks = self._simulate(180)
        self.assertEqual(len(starts), 1, starts)
        rechecks = [stamp for stamp in checks if stamp > 2]
        self.assertTrue(4 <= len(rechecks) <= 8, checks)

    def test_channel_going_live_again_is_started_soon(self):
        _bot, starts, _checks = self._simulate(200, live_after=100)
        later = [stamp for stamp in starts if stamp >= 100]
        self.assertTrue(later, starts)
        self.assertLessEqual(later[0], 100 + config.LIVE_OFFLINE_RECHECK_SEC + 5)

    def test_backoff_grows_while_twitch_says_live(self):
        _bot, starts, _checks = self._simulate(120, live_after=0)
        gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
        self.assertGreaterEqual(len(gaps), 3)
        self.assertEqual(gaps, sorted(gaps))
        self.assertLessEqual(len(starts), 8, starts)

    def test_restart_keeps_heartbeat_and_failure_count(self):
        bot, _starts, _checks = self._simulate(10)
        self.assertGreaterEqual(bot.retry[CHANNEL]["fails"], 1)
        self.assertTrue(livebot.has_demand(CHANNEL))
        self.assertTrue(os.path.isfile(os.path.join(config.LIVE_HLS_ROOT, CHANNEL, ".heartbeat")))

    def test_offline_only_when_twitch_says_so(self):
        bot = self.fake_bot()
        for live, expected in ((False, True), (None, False), (True, False)):
            bot.retry.clear()
            bot._schedule_retry(CHANNEL, "process ended", live)
            self.assertEqual(bot.retry[CHANNEL]["offline"], expected)
            self.assertGreater(bot.retry[CHANNEL]["next"], time.time())

    def test_unknown_live_state_starts_normally(self):
        bot = self.fake_bot(is_live=None)
        bot.retry[CHANNEL] = {"fails": 1, "next": 0.0, "offline": True}
        self.mark()
        bot.tick()
        self.assertEqual(len(FakeWorker.started), 1)

    def test_http_start_respects_offline_and_backoff(self):
        bot = self.fake_bot()
        self.mark()
        for state in ({"fails": 1, "next": 0.0, "offline": True},
                      {"fails": 1, "next": time.time() + 60, "offline": False}):
            bot.retry[CHANNEL] = state
            self.assertEqual(bot.get_or_start(CHANNEL), (None, 503))
        self.assertEqual(FakeWorker.started, [])

    def test_http_client_finds_the_backoff_while_twitch_is_asked(self):
        """The slow part of a restart (stop, live check) must not be a way around it."""
        bot = self.fake_bot(alive=True)
        self.mark()
        bot.tick()
        seen = []

        def is_live(_channel):
            seen.append(bot.get_or_start(CHANNEL))
            return True

        def stop(worker):
            seen.append(bot.get_or_start(CHANNEL))

        bot.is_live = is_live
        with mock.patch.object(FakeWorker, "alive", False), mock.patch.object(FakeWorker, "stop", stop):
            bot.tick()
        self.assertEqual(seen, [(None, 503), (None, 503)])
        self.assertEqual(len(FakeWorker.started), 1)
        self.assertFalse(bot.retry[CHANNEL]["offline"], "Twitch said live")

    def test_backoff_survives_a_check_longer_than_itself(self):
        bot = self.fake_bot(alive=True)
        self.mark()
        bot.tick()
        seen = []

        def is_live(_channel):
            bot.retry[CHANNEL]["next"] = 0.0  # the check took longer than the backoff
            seen.append(bot.get_or_start(CHANNEL))
            return False

        bot.is_live = is_live
        with mock.patch.object(FakeWorker, "alive", False):
            bot.tick()
        self.assertEqual(seen, [(None, 503)])
        self.assertTrue(bot.retry[CHANNEL]["offline"])
        self.assertGreater(bot.retry[CHANNEL]["next"], time.time() + config.LIVE_OFFLINE_RECHECK_SEC - 5)

    def test_unanswered_live_check_does_not_hold_up_the_loop(self):
        bot = self.fake_bot()
        release = threading.Event()
        self.addCleanup(release.set)
        bot.is_live = lambda _channel: release.wait(20)
        started = time.monotonic()
        with mock.patch.object(livebot, "LIVE_CHECK_TIMEOUT_SEC", 0.2), \
                self.assertLogs("livebot", level="WARNING"):
            bot.retry[CHANNEL] = {"fails": 1, "next": 0.0, "offline": True}
            self.assertFalse(bot._still_offline(CHANNEL))
        self.assertLess(time.monotonic() - started, 5)

    def test_missing_thread_stops_the_children_and_schedules_a_retry(self):
        self.mark()
        proc = mock.Mock(pid=2 ** 22, stdout=mock.MagicMock())
        proc.poll.return_value = None
        real_start = threading.Thread.start

        def start(thread):
            if thread.name.startswith("ts-fanout"):
                raise RuntimeError("can't start new thread")
            real_start(thread)

        bot = livebot.LiveBot(source_command=lambda *_args: ["streamlink"], is_live=mock.Mock())
        with mock.patch.object(livebot, "_tool", return_value="ffmpeg"), \
                mock.patch.object(livebot.SubprocessRunner, "popen", return_value=proc), \
                mock.patch.object(livebot, "stop_process") as stop_process, \
                mock.patch.object(threading.Thread, "start", start), \
                self.assertLogs("livebot", level="ERROR"):
            self.assertEqual(bot.get_or_start(CHANNEL), (None, 503))
        self.assertEqual([call.args[0] for call in stop_process.call_args_list], [proc, proc])
        self.assertEqual(bot.retry[CHANNEL]["fails"], 1)
        self.assertEqual(bot.streams, {})

    def test_shutdown_without_free_threads_still_stops_everything(self):
        bot = self.fake_bot()
        self.mark()
        bot.tick()
        worker = bot.streams[CHANNEL]
        with mock.patch.object(livebot, "ThreadPoolExecutor", side_effect=RuntimeError("can't start new thread")):
            bot.shutdown()
        self.assertTrue(worker.stopped)

    def test_proxy_is_skipped_after_failed_starts(self):
        bot = self.fake_bot()
        for fails, use_proxy in ((0, True), (livebot.PROXY_DIRECT_AFTER_FAILS, False)):
            bot.streams.clear()
            bot.retry[CHANNEL] = {"fails": fails, "next": 0.0, "offline": False}
            bot._try_start(CHANNEL)
            self.assertEqual(FakeWorker.started[-1][1], use_proxy)

    def test_failed_start_keeps_viewer_demand(self):
        self.mark()
        worker = livebot.StreamWorker(CHANNEL, source_command=lambda *_args: ["streamlink"])
        with mock.patch.object(livebot, "_tool", side_effect=FileNotFoundError("no ffmpeg")), \
                self.assertLogs("livebot", level="ERROR"):
            self.assertFalse(worker.start())
        self.assertTrue(livebot.has_demand(CHANNEL))

    def test_stall_detection(self):
        worker = livebot.StreamWorker(CHANNEL)
        now = time.time()
        limit = config.LIVE_STALL_RESTART_SEC
        with mock.patch.object(worker, "is_alive", return_value=True), \
                mock.patch.object(worker, "playlist_ready", return_value=True), \
                mock.patch.object(worker, "playlist_age", return_value=1.0):
            worker.broadcaster.last_chunk_time = now - 5
            self.assertEqual(worker.failure(now), "")
            # A 3-minute ad break delivers nothing and is not a stall.
            worker.broadcaster.last_chunk_time = now - 180
            self.assertEqual(worker.failure(now), "")
            worker.broadcaster.last_chunk_time = now - limit - 1
            self.assertIn("stalled", worker.failure(now))
        self.assertEqual(worker.failure(now), "process ended")


class CleanupTests(LivebotBase):
    def _files(self, *names):
        directory = os.path.join(config.LIVE_HLS_ROOT, CHANNEL)
        os.makedirs(directory, exist_ok=True)
        for name in names:
            with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        return directory

    def test_waiting_viewer_keeps_markers(self):
        self.mark()
        directory = self._files("stream.m3u8", "seg_00001.ts", "seg_00002.ts.tmp", "ffmpeg.log")
        livebot.clear_channel_files(CHANNEL)
        self.assertEqual(os.listdir(directory), [".heartbeat"])
        self.assertTrue(livebot.has_demand(CHANNEL))

    def test_unwatched_channel_is_removed_completely(self):
        self.mark(age=config.LIVE_WATCH_DEMAND_SEC + 10)
        directory = self._files("stream.m3u8", "seg_00001.ts")
        livebot.purge_leftovers(set())
        self.assertFalse(os.path.exists(directory))
        self.assertFalse(os.path.exists(os.path.join(config.META_DIR, "live_watch_demand", f"{CHANNEL}.ts")))

    def test_foreign_files_are_never_deleted(self):
        directory = self._files("recording.mp4", "seg_1.ts", "notes.ts")
        livebot.purge_leftovers(set())
        self.assertEqual(sorted(os.listdir(directory)), ["notes.ts", "recording.mp4"])

    def test_running_channel_is_not_purged(self):
        directory = self._files("stream.m3u8")
        livebot.purge_leftovers({CHANNEL})
        self.assertEqual(os.listdir(directory), ["stream.m3u8"])

    def test_log_tail(self):
        self._files()
        worker = livebot.StreamWorker(CHANNEL)
        with open(os.path.join(worker.hls_dir, "ffmpeg.log"), "w", encoding="utf-8") as handle:
            handle.write("one\n\nCould not find codec parameters\nCould not write header\n")
        self.assertIn("Could not write header", worker.log_tail("ffmpeg.log"))
        self.assertEqual(worker.log_tail("missing.log"), "-")

    def test_trimming_never_follows_a_symlink(self):
        directory = self._files("ffmpeg.log", "streamlink.log")
        worker = livebot.StreamWorker(CHANNEL)
        real_open = os.open

        def nofollow(path, flags, *args):
            # What the kernel answers for a symlink opened with O_NOFOLLOW.
            if str(path).startswith(directory):
                raise OSError(errno.ELOOP, "Too many levels of symbolic links")
            return real_open(path, flags, *args)

        with mock.patch.object(livebot, "PIPELINE_LOG_MAX_BYTES", 0), \
                mock.patch.object(livebot.os, "open", nofollow), self.assertLogs("livebot", level="WARNING"):
            worker.trim_logs()
        self.assertEqual(os.path.getsize(os.path.join(directory, "ffmpeg.log")), 1)

    @unittest.skipUnless(os.name == "posix", "symlinks need POSIX")
    def test_planted_symlinks_are_removed_and_their_targets_stay(self):
        victim = self.base / "victim"
        victim.write_text("keep", encoding="utf-8")
        directory = self._files()
        for name in ("stream.m3u8", "seg_00000.ts.tmp", "ffmpeg.log"):
            os.symlink(victim, os.path.join(directory, name))
        with mock.patch.object(livebot, "PIPELINE_LOG_MAX_BYTES", 0):
            livebot.StreamWorker(CHANNEL).trim_logs()
        livebot.prepare_hls_dir(CHANNEL)
        self.assertEqual(os.listdir(directory), [])
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def _start_is_refused(self):
        worker = livebot.StreamWorker(CHANNEL, source_command=lambda *_args: ["streamlink"])
        with mock.patch.object(livebot.SubprocessRunner, "popen", side_effect=AssertionError("started")), \
                mock.patch.object(livebot, "_tool", return_value="ffmpeg"), \
                self.assertLogs("livebot", level="ERROR") as logs:
            self.assertFalse(worker.start())
        return " ".join(logs.output)

    def test_directory_of_another_user_is_refused(self):
        directory = self._files("seg_00001.ts")
        with mock.patch.object(livebot.os, "geteuid", return_value=os.stat(directory).st_uid + 1, create=True):
            self.assertIn("belongs to uid", self._start_is_refused())

    def test_leftover_that_cannot_be_removed_is_refused(self):
        directory = self._files()
        os.mkdir(os.path.join(directory, "stream.m3u8"))
        self._start_is_refused()

    @unittest.skipUnless(os.name == "posix", "symlinks need POSIX")
    def test_symlinked_directory_is_refused(self):
        target = self.base / "elsewhere"
        target.mkdir()
        (target / "seg_00001.ts").write_text("keep", encoding="utf-8")
        os.makedirs(config.LIVE_HLS_ROOT, exist_ok=True)
        os.symlink(target, os.path.join(config.LIVE_HLS_ROOT, CHANNEL))
        self.assertIn("symlink", self._start_is_refused())
        self.assertTrue((target / "seg_00001.ts").exists())

    def test_large_pipeline_log_is_trimmed(self):
        self._files("ffmpeg.log", "streamlink.log")
        worker = livebot.StreamWorker(CHANNEL)
        with mock.patch.object(livebot, "PIPELINE_LOG_MAX_BYTES", 0):
            worker.trim_logs()
        self.assertEqual(os.path.getsize(os.path.join(worker.hls_dir, "ffmpeg.log")), 0)


class ChannelTableTests(LivebotBase):
    def _rows(self):
        return {conf["name"]: conf["live"] for conf in database.list_channel_configs()}

    def test_flags_without_demand(self):
        self.add_row("watch_only", notify=0)
        self.add_row("watch_chat", chat=1, notify=0)
        self.add_row("recorded", record=1)
        self.add_row("live_only", notify=1)  # configured by the admin, flag set by the recorder
        self.add_row("watched", notify=0)
        self.add_row("running", notify=0)
        self.mark("watched")
        livebot.reset_unwatched_flags({"running"})
        self.assertEqual(self._rows(), {
            "watch_chat": False, "recorded": False, "live_only": True, "watched": True, "running": True,
        })

    def test_watch_rows_are_marked_and_configured_channels_are_not(self):
        from WEBUI import webui
        webui.add_channel_db("configured", record=0)
        webui.add_live_channel("configured")
        webui.add_live_channel("visitor")
        self.assertTrue(database.get_channel_config("configured")["notify"])
        self.assertFalse(database.get_channel_config("visitor")["notify"])
        livebot.reset_unwatched_flags(set())
        self.assertEqual(set(self._rows()), {"configured"})

    def test_watch_row_becomes_permanent_when_the_admin_adds_it(self):
        from WEBUI import webui
        webui.add_live_channel("visitor")
        webui.add_channel_db("visitor", record=0)
        livebot.reset_unwatched_flags(set())
        self.assertIn("visitor", self._rows())

    def test_locked_database_is_logged(self):
        import sqlite3
        with mock.patch.object(database, "get_channels", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs("livebot", level="ERROR"):
            livebot.reset_unwatched_flags(set())

    def test_maintenance_runs_from_the_loop(self):
        bot = self.fake_bot()
        self.add_row("watch_only", notify=0)
        bot.tick()
        self.assertEqual(self._rows(), {})


    def test_chat_clip_flag_starts_nothing_and_is_cleared(self):
        from recorder import chat_clip
        bot = self.fake_bot()
        self.add_row(CHANNEL, record=1, live=0)
        with mock.patch.object(chat_clip, "clip_enabled_for", return_value=True):
            chat_clip.ensure_live_hls_enabled(CHANNEL)
        self.assertEqual(self._rows(), {CHANNEL: True})
        bot.tick()
        self.assertEqual((bot.streams, self._rows()), ({}, {CHANNEL: False}))

    def test_streamer_page_shows_recording_not_the_live_flag(self):
        """live=1 only means that somebody opened the watch page."""
        from WEBUI import webui
        try:
            from webui_test_helpers import admin_test_client
        except ImportError:  # run as tests.test_livebot
            from tests.webui_test_helpers import admin_test_client
        self.add_row(CHANNEL, record=1, live=1)
        (self.base / CHANNEL).mkdir()
        client = admin_test_client()
        for recording, expected in ((None, False), ({"filename": "x.mp4"}, True)):
            with self.subTest(recording=recording), \
                    mock.patch.object(webui, "current_recording_info", return_value=recording),                     mock.patch.object(webui, "update_profiles_background"),                     mock.patch.object(webui, "get_twitch_profile_and_banner", return_value=("", "")), \
                    mock.patch.object(twitch_api, "get_stream", return_value={}), \
                    mock.patch.object(twitch_api, "get_streams_by_logins", return_value={}):
                body = client.get(f"/streamer/{CHANNEL}", headers={"Accept-Language": "en"}).get_data(as_text=True)
            self.assertIn("nebula-profile-tags", body)
            self.assertEqual('ns-pill ns-pill-live">' in body, expected)
            self.assertEqual("ns-status-indicator is-recording" in body, expected)


class LiveCheckTests(unittest.TestCase):
    def test_gql_answers(self):
        for payload, expected in (
            ({"data": {"user": {"stream": {"id": "1"}}}}, True),
            ({"data": {"user": {"stream": None}}}, False),
            ({"data": {"user": None}}, False),
            ({"errors": [{}], "data": {"user": None}}, None),
            (None, None),
            ([], None),
        ):
            with self.subTest(payload=payload), mock.patch.object(twitch_api, "_gql_post", return_value=payload):
                self.assertIs(twitch_api.gql_stream_is_live(CHANNEL), expected)


class StreamlinkCompatTests(unittest.TestCase):
    """streamlink patches urllib3 internals; an incompatible pair breaks every request."""

    PROBE = (
        "import requests\n"
        "from streamlink import Streamlink\n"
        "s = Streamlink()\n"
        "r = s.http.prepare_request(requests.Request('POST', 'https://gql.twitch.tv/gql?a=%20b', json={'q': 1}))\n"
        "assert r.url.startswith('https://gql.twitch.tv/gql'), r.url\n"
        "print('ok')\n"
    )

    def test_streamlink_can_prepare_twitch_requests(self):
        proc = subprocess.run([sys.executable, "-I", "-c", self.PROBE], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "ok", proc.stderr[-800:])


if __name__ == "__main__":
    unittest.main()
