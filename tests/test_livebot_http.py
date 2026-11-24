"""livebot end to end without Twitch: FFmpeg's test generator replaces streamlink."""

from __future__ import annotations

import http.client
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_livebot import LivebotBase
except ImportError:  # run as tests.test_livebot_http
    from tests.test_livebot import LivebotBase

from core import config  # noqa: E402
from recorder import livebot  # noqa: E402

CHANNEL = "demochan"
ROOT = Path(__file__).resolve().parents[1]
FFMPEG = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
GENERATOR_ARGS = (
    "-hide_banner", "-loglevel", "error", "-re",
    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-g", "25",
    "-b:v", "1500k", "-minrate", "1500k", "-maxrate", "1500k", "-bufsize", "1500k", "-x264-params", "nal-hrd=cbr",
    "-c:a", "aac", "-f", "mpegts", "pipe:1",
)


def generator_command(_channel, _use_proxy=True):
    return [FFMPEG, *GENERATOR_ARGS]


def wait_until(check, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


class _ServerBase(LivebotBase):
    def serve(self, bot):
        server = livebot.LiveHttpServer(("127.0.0.1", 0), bot)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        return server

    def request(self, path, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request(method, path, headers={"Cookie": "session=secret-cookie"})
        return conn.getresponse()


class HttpRuleTests(_ServerBase):
    def setUp(self):
        super().setUp()
        self.bot = self.fake_bot()
        self.serve(self.bot)

    def test_only_the_plain_ts_route_exists(self):
        self.mark()
        for path in ("/", "/live/demochan", "/live/Demochan.ts", "/live/demochan.ts/", "/live/../demochan.ts",
                     "/live/%64emochan.ts", "/live/demo-chan.ts", "/live/" + "x" * 26 + ".ts",
                     "/live/demochan/stream.m3u8", "/api/live/stream/demochan"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path).status, 404)
        self.assertEqual(self.bot.streams, {})

    def test_no_marker_means_404_and_no_start(self):
        self.assertEqual(self.request("/live/demochan.ts").status, 404)
        self.assertEqual(self.bot.streams, {})

    def test_head_never_starts_a_pipeline(self):
        self.mark()
        self.assertEqual(self.request("/live/demochan.ts", "HEAD").status, 404)
        self.assertEqual(self.bot.streams, {})

    def test_other_methods_are_rejected(self):
        self.assertEqual(self.request("/live/demochan.ts", "POST").status, 501)

    def test_health_has_no_channel_names_and_no_cors(self):
        self.mark()
        self.bot.tick()
        response = self.request("/health?x=1")
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"demochan", response.read())
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_cookies_are_not_logged(self):
        with self.assertNoLogs("livebot"):
            with mock.patch.object(sys, "stderr") as stderr:
                self.request("/live/demochan.ts")
        stderr.write.assert_not_called()

    def test_connection_cap(self):
        with mock.patch.object(config, "LIVEBOT_MAX_CHANNELS", 1), mock.patch.object(config, "LIVEBOT_MAX_VIEWERS", 1):
            self.serve(self.bot)
        held = [socket.create_connection(("127.0.0.1", self.port), timeout=5) for _ in range(17)]
        for sock in held:
            self.addCleanup(sock.close)
        time.sleep(0.5)
        with self.assertRaises((http.client.HTTPException, OSError)):
            self.request("/health")
        for sock in held:
            sock.close()
        self.assertTrue(wait_until(self._healthy, timeout=10))

    def _healthy(self):
        try:
            return self.request("/health").status == 200
        except (http.client.HTTPException, OSError):
            return False


class StreamEndTests(_ServerBase):
    """The end of the stream must reach the player as EOF, otherwise it freezes."""

    def setUp(self):
        super().setUp()
        self.bot = self.fake_bot()
        self.mark()
        self.bot.tick()
        worker = self.bot.streams[CHANNEL]
        worker.broadcaster = self.broadcaster = livebot.MpegTsBroadcaster(CHANNEL)
        self.broadcaster.running = True
        self.broadcaster.first_byte_time = time.time()
        self.serve(self.bot)

    def test_stream_end_closes_the_connection(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"GET /live/demochan.ts HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(wait_until(self.broadcaster.active_viewers, timeout=5))
        self.broadcaster._publish(b"\x47" * 188, time.time())
        self.broadcaster.stop()
        data = b""
        while True:
            part = sock.recv(65536)  # a timeout here means: no EOF
            if not part:
                break
            data += part
        head, body = data.split(b"\r\n\r\n", 1)
        self.assertIn(b"200 OK", head)
        self.assertIn(b"Connection: close", head)
        self.assertNotIn(b"Access-Control", head)
        self.assertEqual(body, b"\x47" * 188)

    def test_viewer_limit_answers_503(self):
        with mock.patch.object(config, "LIVEBOT_MAX_VIEWERS", 0):
            self.assertEqual(self.request("/live/demochan.ts").status, 503)

    def test_silent_client_that_left_frees_its_slot(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.sendall(b"GET /live/demochan.ts HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(wait_until(self.broadcaster.active_viewers, timeout=5))
        sock.close()
        self.assertTrue(wait_until(lambda: not self.broadcaster.active_viewers(), timeout=10))


@unittest.skipUnless(FFMPEG or os.environ.get("CI"), "ffmpeg not installed")
class PipelineTests(_ServerBase):
    """Real processes: generator -> ffmpeg copy -> HLS files + HTTP fan-out."""

    def setUp(self):
        super().setUp()
        if not FFMPEG:
            # GitHub's runners ship ffmpeg; the service images install it.
            self.skipTest("ffmpeg not installed")
        self.bot = livebot.LiveBot(source_command=generator_command, is_live=mock.Mock(return_value=True))
        self.addCleanup(self.bot.shutdown)
        self.hls_dir = os.path.join(config.LIVE_HLS_ROOT, CHANNEL)
        space = mock.patch.object(config, "LIVE_HLS_MIN_FREE_MB", 1)
        space.start()
        self.addCleanup(space.stop)

    def _read_ts(self, size=188 * 400):
        response = self.request("/live/demochan.ts")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "video/mp2t")
        return response.read(size)

    def test_generator_to_hls_and_ts_and_stop(self):
        self.serve(self.bot)
        self.mark()
        data = self._read_ts()  # the request itself starts the pipeline
        self.assertEqual(len(data), 188 * 400)
        self.assertEqual({data[off] for off in range(0, len(data), 188)}, {0x47})

        worker = self.bot.streams[CHANNEL]
        playlist = Path(self.hls_dir, "stream.m3u8")
        self.assertTrue(wait_until(lambda: playlist.is_file() and playlist.read_text().count("seg_") >= 2))
        text = playlist.read_text(encoding="utf-8")
        self.assertNotIn("#EXT-X-ENDLIST", text)
        segments = [line for line in text.splitlines() if line.startswith("seg_")]
        for name in segments:
            self.assertRegex(name, r"^seg_\d{5}\.ts$")
            self.assertEqual(Path(self.hls_dir, name).read_bytes()[0], 0x47)
        if os.name == "posix":
            self.assertTrue(os.path.islink(os.path.join(self.hls_dir, "index.m3u8")))
        self.assertEqual(worker.failure(time.time()), "")

        # A late viewer starts with PAT and PMT, then the keyframe packet.
        late = self._read_ts(188 * 3)
        self.assertEqual(((late[1] & 0x1F) << 8) | late[2], 0)
        self.assertEqual(late[188], 0x47)

        pids = [worker.process_streamlink.pid, worker.process_ffmpeg.pid]
        processes = [worker.process_streamlink, worker.process_ffmpeg]
        self.mark()
        self.bot.shutdown()
        self.assertTrue(all(proc.poll() is not None for proc in processes), pids)
        self.assertFalse(worker.broadcaster.thread.is_alive())
        self.assertEqual(os.listdir(self.hls_dir), [".heartbeat"], "viewer marker stays, files go")
        self.assertEqual(self.bot.streams, {})

    def test_end_of_the_source_never_marks_the_playlist_as_finished(self):
        # hls.js and Safari stop reloading at #EXT-X-ENDLIST and freeze.
        command = [FFMPEG, *GENERATOR_ARGS[:-1], "-t", "5", GENERATOR_ARGS[-1]]
        worker = livebot.StreamWorker(CHANNEL, source_command=lambda *_args: command)
        self.addCleanup(worker.stop)
        self.assertTrue(worker.start())
        processes = [worker.process_streamlink, worker.process_ffmpeg]
        self.assertTrue(wait_until(lambda: all(proc.poll() is not None for proc in processes)))
        self.assertEqual([proc.returncode for proc in processes], [0, 0])
        text = Path(self.hls_dir, "stream.m3u8").read_text(encoding="utf-8")
        self.assertIn("seg_00000.ts", text)
        self.assertNotIn("#EXT-X-ENDLIST", text)

    def test_dead_source_is_detected_and_restarted_after_backoff(self):
        self.mark()
        self.bot.tick()
        worker = self.bot.streams[CHANNEL]
        worker.process_streamlink.kill()
        self.assertTrue(wait_until(lambda: not worker.is_alive()))
        self.bot.tick()
        self.assertNotIn(CHANNEL, self.bot.streams)
        self.assertEqual(self.bot.retry[CHANNEL]["fails"], 1)
        self.assertTrue(livebot.has_demand(CHANNEL))
        self.bot.retry[CHANNEL]["next"] = 0.0
        self.bot.tick()
        self.assertIsNot(self.bot.streams[CHANNEL], worker)

    @unittest.skipUnless(os.name == "posix", "process groups and SIGTERM need POSIX")
    def test_child_ignoring_sigterm_is_killed(self):
        proc = livebot.SubprocessRunner().popen(
            [sys.executable, "-c",
             "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)"],
            stdout=subprocess.PIPE,
        )
        self.addCleanup(proc.stdout.close)
        proc.stdout.readline()
        self.assertTrue(livebot.stop_process(proc, "stubborn", timeout=1))
        self.assertEqual(proc.returncode, -signal.SIGKILL)

    @unittest.skipUnless(os.name == "posix", "SIGTERM cannot be handled on Windows")
    def test_daemon_exits_cleanly_on_sigterm(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        fake = self.base / "streamlink"
        args = " ".join(f"'{arg}'" for arg in GENERATOR_ARGS)
        fake.write_text(f"#!/bin/sh\nexec '{FFMPEG}' {args}\n", encoding="utf-8")
        fake.chmod(0o755)
        self.mark()
        env = dict(
            os.environ, VOD_BASE_DIR=str(self.base), VOD_META_DIR=str(config.META_DIR),
            LIVE_HLS_DIR=str(config.LIVE_HLS_ROOT), LIVEBOT_PORT=str(port), STREAMLINK_BIN=str(fake),
            LIVEBOT_ENABLED="true", LIVE_HLS_MIN_FREE_MB="32", PYTHONDONTWRITEBYTECODE="1",
        )
        proc = subprocess.Popen([sys.executable, "-m", "recorder.livebot"], cwd=ROOT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(proc.stderr.close)
        self.addCleanup(livebot.stop_process, proc, "livebot under test")
        started = wait_until(lambda: os.path.isfile(os.path.join(self.hls_dir, "stream.m3u8")) or proc.poll() is not None)
        if proc.poll() is not None:
            self.fail(proc.stderr.read().decode(errors="replace")[-1500:])
        self.assertTrue(started)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 0)
        self.assertEqual(os.listdir(self.hls_dir), [".heartbeat"])


if __name__ == "__main__":
    unittest.main()
