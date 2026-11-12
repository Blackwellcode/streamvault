"""RecordingPolicy decision table and the ProcessRunner adapters (S4)."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

try:
    import test_recorder_reliability  # noqa: F401  (env bootstrap, fcntl shim)
except ImportError:  # run as tests.test_recording_policy
    from tests import test_recorder_reliability  # noqa: F401

from recorder import Streamrecorder as recorder  # noqa: E402
from recorder.recording_policy import STATE_OFFLINE, STATE_STARTING, RecordingPolicy  # noqa: E402
from recorder.recording_runtime import FakeProcessRunner, SubprocessRunner  # noqa: E402


def policy():
    # Deterministic delays: backoff = 10 * failures, idle = 5, unplayable = 7.
    return RecordingPolicy(backoff=lambda n: 10 * n, idle_delay=lambda: 5, unplayable_delay=7)


class PresenceTests(unittest.TestCase):
    def test_restart_grace_turns_offline_into_unknown(self):
        self.assertIsNone(RecordingPolicy.effective_presence(False, True))
        self.assertIs(RecordingPolicy.effective_presence(False, False), False)
        self.assertIs(RecordingPolicy.effective_presence(True, True), True)
        self.assertIsNone(RecordingPolicy.effective_presence(None, False))


class OfflineSeriesTests(unittest.TestCase):
    def test_offline_polls_at_fixed_interval_and_resets_other_counters(self):
        p = policy()
        p.recording_failures = 3
        p.playable_failures = 2
        delays = [p.on_offline() for _ in range(4)]
        self.assertEqual(delays, [5, 5, 5, 5])  # never exponential while offline
        self.assertEqual(p.offline_failures, 4)
        self.assertEqual((p.recording_failures, p.playable_failures), (0, 0))

    def test_unplayable_while_live_keeps_polling_fast(self):
        p = policy()
        self.assertEqual(p.on_unplayable(True, False), (STATE_STARTING, 7))
        self.assertEqual(p.on_unplayable(None, True), (STATE_STARTING, 7))
        self.assertEqual(p.playable_failures, 2)
        self.assertEqual(p.offline_failures, 0)

    def test_unplayable_and_not_live_counts_as_offline(self):
        p = policy()
        self.assertEqual(p.on_unplayable(None, False), (STATE_OFFLINE, 5))
        self.assertEqual(p.offline_failures, 1)

    def test_playable_resets_offline_and_unplayable_counters(self):
        p = policy()
        p.offline_failures, p.playable_failures, p.recording_failures = 4, 2, 1
        p.on_playable()
        self.assertEqual((p.offline_failures, p.playable_failures, p.recording_failures), (0, 0, 1))


class CopyFallbackTests(unittest.TestCase):
    def test_copy_mode_only_without_vaapi_and_without_previous_failure(self):
        p = policy()
        self.assertTrue(p.use_copy(True, False))
        self.assertFalse(p.use_copy(True, True))
        self.assertFalse(p.use_copy(False, False))

    def test_empty_copy_output_switches_to_transcode_until_success(self):
        p = policy()
        self.assertEqual(p.on_empty_output(used_copy=True), 10)
        self.assertTrue(p.copy_fallback)
        self.assertFalse(p.use_copy(True, False))
        p.on_completed()
        self.assertFalse(p.copy_fallback)
        self.assertEqual(p.recording_failures, 0)
        self.assertTrue(p.use_copy(True, False))

    def test_failed_copy_run_with_data_keeps_copy_mode(self):
        p = policy()
        self.assertEqual(p.on_ffmpeg_failed(used_copy=True, temp_size=5000, min_size=1000), 10)
        self.assertFalse(p.copy_fallback)
        self.assertEqual(p.on_ffmpeg_failed(used_copy=True, temp_size=10, min_size=1000), 20)
        self.assertTrue(p.copy_fallback)

    def test_transcode_failures_back_off(self):
        p = policy()
        self.assertEqual([p.on_ffmpeg_failed(False, 0, 1000) for _ in range(3)], [10, 20, 30])
        self.assertFalse(p.copy_fallback)
        self.assertEqual(p.on_exception(), 40)
        self.assertEqual(p.on_disk_full(), 50)


class ErrorMessageTests(unittest.TestCase):
    def test_messages(self):
        self.assertEqual(RecordingPolicy.error_message(1, ""), "FFmpeg Fehlercode 1")
        self.assertEqual(RecordingPolicy.error_message(1, "x Requested format is not available"),
                         "YouTube-Format nicht verfuegbar (live-from-start/DASH?)")
        self.assertEqual(RecordingPolicy.error_message(183, "bad format"),
                         "YouTube-Quelle lieferte kein gueltiges Stream-Format")
        self.assertEqual(RecordingPolicy.error_message(183, "network down"), "FFmpeg Fehlercode 183")


class OffloadTests(unittest.TestCase):
    def test_integrity_failure_never_offloads(self):
        for auto_remux in (False, True):
            for sync_remux in (False, True):
                for short in (False, True):
                    with self.subTest(auto_remux=auto_remux, sync_remux=sync_remux, short=short):
                        self.assertFalse(RecordingPolicy.upload_now(
                            integrity_ok=False, auto_remux=auto_remux,
                            sync_remux=sync_remux, short_recording=short))

    def test_async_remux_defers_upload(self):
        self.assertFalse(RecordingPolicy.upload_now(integrity_ok=True, auto_remux=True,
                                                    sync_remux=False, short_recording=False))
        self.assertTrue(RecordingPolicy.upload_now(integrity_ok=True, auto_remux=True,
                                                   sync_remux=True, short_recording=False))
        self.assertTrue(RecordingPolicy.upload_now(integrity_ok=True, auto_remux=False,
                                                   sync_remux=False, short_recording=False))
        self.assertTrue(RecordingPolicy.upload_now(integrity_ok=True, auto_remux=True,
                                                   sync_remux=False, short_recording=True))


class FfmpegCommandTests(unittest.TestCase):
    def test_copy_mode_remuxes_to_matroska(self):
        cmd = recorder.build_ffmpeg_record_command("ffmpeg", "out.mkv.part", use_copy=True)
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertEqual(cmd[-3:], ["-f", "matroska", "out.mkv.part"])
        self.assertIn("copy", cmd)
        self.assertNotIn("-filter_complex", cmd)

    def test_transcode_burns_in_overlay(self):
        cmd = recorder.build_ffmpeg_record_command("ffmpeg", "out.mp4.part", use_copy=False, filter_str="F")
        self.assertEqual(cmd[cmd.index("-filter_complex") + 1], "F")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cmd[-1], "out.mp4.part")
        self.assertIn("+faststart", cmd)
        self.assertNotIn("-vaapi_device", cmd)

    def test_vaapi_uses_hardware_encoder(self):
        cmd = recorder.build_ffmpeg_record_command("ffmpeg", "o.part", use_copy=False, filter_str="F",
                                                   vaapi_device="/dev/dri/renderD128")
        self.assertEqual(cmd[cmd.index("-vaapi_device") + 1], "/dev/dri/renderD128")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "h264_vaapi")
        # The device must be declared before the input.
        self.assertLess(cmd.index("-vaapi_device"), cmd.index("-i"))


class RepairRecordingTests(test_recorder_reliability.IsolatedBaseTest):
    def test_successful_repair_replaces_file(self):
        target = self.base / "rec.mp4"
        target.write_bytes(b"broken")

        def fake_ffmpeg(cmd, **kwargs):
            # The repaired copy appears where the command says.
            with open(cmd[-1], "wb") as fh:
                fh.write(b"r" * (recorder.MIN_RECORD_FILE_SIZE + 1))
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        runner = FakeProcessRunner()
        runner.run = lambda cmd, *, timeout, **kw: fake_ffmpeg(cmd, timeout=timeout, **kw)
        self.assertTrue(recorder.repair_recording(runner, "ffmpeg", str(target)))
        self.assertEqual(target.stat().st_size, recorder.MIN_RECORD_FILE_SIZE + 1)
        self.assertFalse((self.base / "rec.mp4.repaired.mp4").exists())

    def test_timeout_keeps_original_and_cleans_up(self):
        target = self.base / "rec.mp4"
        target.write_bytes(b"broken")
        runner = FakeProcessRunner(error=subprocess.TimeoutExpired("ffmpeg", 60))
        with self.assertLogs(recorder.logger, level="WARNING"):
            self.assertFalse(recorder.repair_recording(runner, "ffmpeg", str(target)))
        self.assertEqual(target.read_bytes(), b"broken")
        self.assertEqual(runner.calls[0][2]["timeout"], 60)

    def test_source_error_hint_reads_tail(self):
        suffix = recorder.source_state_suffix("chan") or "src"
        log = self.base / f".source_stderr.{suffix}.log"
        log.write_text("a\nb\nRequested format is not available\n", encoding="utf-8")
        with self.assertLogs(recorder.logger, level="ERROR"):
            hint = recorder.source_error_hint(str(self.base), "chan")
        self.assertIn("Requested format is not available", hint)
        self.assertEqual(recorder.source_error_hint(str(self.base / "missing"), "chan"), "")


class DefaultWiringTests(test_recorder_reliability.IsolatedBaseTest):
    """The production defaults behind the new seams are wired to the real collaborators."""

    def test_default_monitor_ports_use_the_recorder_functions(self):
        ports = recorder.default_monitor_ports()
        self.assertIs(ports.get_config, recorder.get_source_config)
        self.assertIs(ports.live_presence, recorder.cheap_live_presence)
        self.assertIs(ports.sleep, recorder.interruptible_sleep)
        self.assertEqual(ports.interval(), recorder.CHECK_INTERVAL)
        self.assertEqual(ports.should_stop(), recorder.shutdown_requested)

    def test_default_runner_resolves_the_real_tools(self):
        runner = recorder.default_runner()
        self.assertIsInstance(runner, SubprocessRunner)
        resolved = 0
        for tool in ("streamlink", "yt-dlp", "ffmpeg"):
            with self.subTest(tool=tool):
                try:
                    path = runner.resolve(tool)
                except FileNotFoundError:
                    continue  # tool not installed on this host
                resolved += 1
                self.assertTrue(os.path.isfile(path))
        if not resolved:
            self.skipTest("none of streamlink, yt-dlp, ffmpeg is installed here")

    def test_default_policy_backs_off_exponentially(self):
        policy = recorder.default_recording_policy()
        first = policy.on_exception()
        for _ in range(3):
            later = policy.on_exception()
        self.assertGreater(later, first)
        self.assertEqual(policy.idle_delay(), recorder.compute_idle_poll_delay())

    def test_pause_flag_none_reads_the_worker_file(self):
        import json
        from pathlib import Path
        from unittest import mock
        flag = self.base / "storage_pause.json"
        plenty = mock.Mock(total=100 * 1024 ** 3, used=10 * 1024 ** 3, free=90 * 1024 ** 3)
        with mock.patch.object(recorder, "STORAGE_PAUSE_FILE", str(flag)), \
                mock.patch("shutil.disk_usage", return_value=plenty):
            self.assertFalse(recorder.disk_blocks_recording(str(self.base)))
            flag.write_text(json.dumps({"paused": True}), encoding="utf-8")
            self.assertTrue(recorder.disk_blocks_recording(str(self.base)))
            self.assertFalse(recorder.disk_blocks_recording(str(self.base), pause_flag=False))
            Path(flag).write_text("{broken", encoding="utf-8")
            recorder.LOG_THROTTLE.pop("storage-pause-flag", None)
            with self.assertLogs(recorder.logger, level="WARNING"):
                self.assertFalse(recorder.disk_blocks_recording(str(self.base)))

    def test_live_flag_is_written_and_connection_closed(self):
        from core import database
        conn = database.get_db()
        try:
            conn.execute("INSERT INTO channels (name, live) VALUES ('flagchan', 0)")
            conn.commit()
        finally:
            conn.close()
        recorder.set_channel_live_flag("flagchan", True)
        self.assertIn("flagchan", database.get_channels(mode="live"))
        recorder.set_channel_live_flag("flagchan", False)
        self.assertNotIn("flagchan", database.get_channels(mode="live"))


class ProcessRunnerTests(unittest.TestCase):
    def test_fake_records_commands_and_answers(self):
        done = subprocess.CompletedProcess(["x"], 0, stdout='{"url": "u", "metadata": {"title": "t"}}', stderr="")
        runner = FakeProcessRunner(results=[done])
        res = recorder.probe_streamlink_playable("chan", {"platform": "twitch"}, runner=runner)
        self.assertTrue(res["ok"])
        self.assertEqual(res["metadata"], {"title": "t"})
        kind, cmd, kwargs = runner.calls[0]
        self.assertEqual(kind, "run")
        self.assertEqual(cmd[0], "streamlink")
        self.assertIn("timeout", kwargs)

    def test_youtube_probe_uses_runner(self):
        done = subprocess.CompletedProcess(["x"], 0, stdout='{"live_status": "is_upcoming"}', stderr="")
        runner = FakeProcessRunner(results=[done])
        res = recorder.probe_source_playable("chan", {"platform": "youtube"}, runner=runner)
        self.assertFalse(res["ok"])
        self.assertIn("geplant", res["error"])
        self.assertEqual(runner.calls[0][1][0], "yt-dlp")

    def test_real_runner_refuses_shell_strings(self):
        runner = SubprocessRunner({})
        with self.assertRaises(ValueError):
            runner.run("echo hi", timeout=5)
        with self.assertRaises(ValueError):
            runner.popen(["echo", "hi"], shell=True)
        with self.assertRaises(FileNotFoundError):
            runner.resolve("ffmpeg")

    def test_real_runner_executes_argument_lists(self):
        runner = SubprocessRunner({"python": lambda: sys.executable})
        res = runner.run([runner.resolve("python"), "-c", "print(6 * 7)"], timeout=30,
                         capture_output=True, text=True)
        self.assertEqual(res.stdout.strip(), "42")

    @unittest.skipUnless(os.name == "posix", "POSIX sessions")
    def test_popen_children_lead_their_own_group(self):
        runner = SubprocessRunner({})
        child = runner.popen(["sleep", "30"])
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait(timeout=5)))
        self.assertEqual(os.getpgid(child.pid), child.pid)
        self.assertNotEqual(os.getpgid(child.pid), os.getpgrp())


if __name__ == "__main__":
    unittest.main()
