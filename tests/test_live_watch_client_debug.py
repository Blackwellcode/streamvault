"""Client telemetry log: bounded, typed, unforgeable, and written without os.chown (Windows)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401  (sets the test env)
except ImportError:  # run as tests.test_live_watch_client_debug
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401

from core import config, live_watch_diag  # noqa: E402


class AppendClientDebugTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.meta = Path(tmp.name)
        self.path = self.meta / "live_watch_client.jsonl"
        for patcher in (
            mock.patch.object(live_watch_diag, "CLIENT_DEBUG_PATH", self.path),
            mock.patch.object(live_watch_diag.config, "META_DIR", str(self.meta)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _lines(self):
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines()]

    def test_without_chown_the_event_is_still_written(self):
        fake_os = types.SimpleNamespace(chmod=mock.Mock())
        self.assertFalse(hasattr(fake_os, "chown"))
        with mock.patch.object(live_watch_diag, "os", fake_os):
            live_watch_diag.append_client_debug({"channel": "demochan", "event": "stall"})
        self.assertEqual(self._lines()[-1]["channel"], "demochan")
        fake_os.chmod.assert_called_once_with(self.path, 0o640)

    def test_with_chown_the_file_gets_the_meta_dir_owner(self):
        st = os.stat(self.meta)
        fake_os = types.SimpleNamespace(chmod=mock.Mock(), chown=mock.Mock())
        with mock.patch.object(live_watch_diag, "os", fake_os):
            live_watch_diag.append_client_debug({"channel": "demochan"})
        fake_os.chown.assert_called_once_with(self.path, st.st_uid, st.st_gid)
        self.assertEqual(len(self._lines()), 1)

    def test_numbers_are_finite_and_bounded(self):
        for value, waited, attempt in (
            (3.5, 3.5, 3), ("7", 7.0, 7), (-5, 0.0, 0), (None, None, None),
            (1e300, 86400.0, 100000), (10 ** 400, None, None),
            (float("nan"), None, None), (float("inf"), None, None), ("-inf", None, None),
            (True, None, None), ("A" * 9000, None, None), (["x"] * 1000, None, None),
            ({"a": "b" * 9000}, None, None),
        ):
            with self.subTest(value=str(value)[:40]):
                live_watch_diag.append_client_debug({"waited_sec": value, "reconnect_attempt": value})
                record = self._lines()[-1]
                self.assertEqual(record["waited_sec"], waited)
                self.assertEqual(record["reconnect_attempt"], attempt)
                self.assertNotIsInstance(record["reconnect_attempt"], (bool, float))

    def test_rejected_number_is_logged_without_its_value(self):
        with self.assertLogs(live_watch_diag.logger, "WARNING") as logs:
            live_watch_diag.append_client_debug({"waited_sec": "x\nforged", "reconnect_attempt": float("inf")})
        self.assertEqual(len(logs.output), 2)
        self.assertNotIn("forged", "".join(logs.output))

    def test_text_cannot_forge_lines_or_terminal_escapes(self):
        hostile = "a\n{}\r\x1b[2J\u2028\u0085\u202e\ud800z"
        fields = ("channel", "mode", "level", "event", "detail", "fatal_type", "fatal_details", "user_agent")
        live_watch_diag.append_client_debug(dict.fromkeys(fields, hostile))
        raw = self.path.read_bytes()
        self.assertTrue(raw.isascii())
        self.assertEqual(raw.count(b"\n"), 1)
        record = live_watch_diag.read_client_debug_tail()[-1]
        for field in fields:
            self.assertTrue(record[field].isprintable(), field)
            self.assertTrue(record[field].startswith("a {}"), field)

    def test_old_entries_with_control_characters_print_clean(self):
        report = {"client_debug_tail": [{"ts": "t", "channel": "c\x1b[2J", "event": "e\nx", "detail": "d\u2028"}]}
        for line in live_watch_diag.human_print(report).split("\n"):
            self.assertTrue(line.isprintable(), ascii(line))
        self.assertEqual(len(live_watch_diag.human_print(report).split("\n")), 3)

    def test_reader_skips_non_dict_records_and_logs_them(self):
        good = json.dumps({"event": "ok\u2028still one record"}, ensure_ascii=False)
        lines = ["[1]", '"text"', "3", "null", "{broken", "[" * 100000, good]
        self.path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        with self.assertLogs(live_watch_diag.logger, "WARNING") as logs:
            records = live_watch_diag.read_client_debug_tail(50)
        self.assertEqual(records, [{"event": "ok\u2028still one record"}])
        self.assertIn("skipped 6 malformed line(s)", "".join(logs.output))
        live_watch_diag.human_print({"client_debug_tail": records})

    def test_unreadable_log_is_logged(self):
        self.path.write_text("{}\n", encoding="utf-8")
        with mock.patch("builtins.open", side_effect=PermissionError("denied")), \
                self.assertLogs(live_watch_diag.logger, "WARNING"):
            self.assertEqual(live_watch_diag.read_client_debug_tail(), [])

    def test_failed_chmod_is_logged(self):
        fake_os = types.SimpleNamespace(chmod=mock.Mock(side_effect=PermissionError("denied")))
        with mock.patch.object(live_watch_diag, "os", fake_os), \
                self.assertLogs(live_watch_diag.logger, "WARNING"):
            live_watch_diag.append_client_debug({"channel": "demochan"})
        self.assertEqual(len(self._lines()), 1)

    def test_file_size_is_bounded(self):
        # A file grown before the limits existed shrinks with the next event.
        self.path.write_bytes(b'{"detail": "' + b"x" * (3 * 1024 * 1024) + b'"}\n' + b'{"event": "keep"}\n')
        live_watch_diag.append_client_debug({"event": "new"})
        self.assertEqual([r["event"] for r in self._lines()], ["keep", "new"])
        with mock.patch.object(live_watch_diag, "CLIENT_DEBUG_MAX_BYTES", 5000):
            for number in range(40):
                live_watch_diag.append_client_debug({"event": f"e{number}", "detail": "\u00e9" * 500})
                self.assertLessEqual(self.path.stat().st_size, 5000)
            self.assertEqual(self._lines()[-1]["event"], "e39")

    def test_line_count_is_bounded(self):
        with mock.patch.object(live_watch_diag, "CLIENT_DEBUG_KEEP", 5):
            for number in range(9):
                live_watch_diag.append_client_debug({"event": f"e{number}"})
        self.assertEqual([r["event"] for r in self._lines()], ["e4", "e5", "e6", "e7", "e8"])


class ClientDebugEndpointTests(IsolatedBaseTest):
    URL = "/api/live/client-debug"

    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        webui.app.config.update(TESTING=True)
        webui.ensure_admin_user()
        self.path = Path(config.META_DIR) / "live_watch_client.jsonl"
        patcher = mock.patch.object(live_watch_diag, "CLIENT_DEBUG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = webui.app.test_client()
        with self.client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")

    def _post(self, body, **kwargs):
        kwargs.setdefault("content_type", "application/json")
        return self.client.post(self.URL, data=body, headers={"X-CSRFToken": "t"}, **kwargs)

    def test_event_is_stored(self):
        resp = self._post(json.dumps({"channel": "demochan", "event": "stall", "waited_sec": 4,
                                      "reconnect_attempt": 2}))
        self.assertEqual(resp.status_code, 200)
        record = live_watch_diag.read_client_debug_tail()[-1]
        self.assertEqual((record["channel"], record["event"], record["waited_sec"], record["reconnect_attempt"]),
                         ("demochan", "stall", 4.0, 2))

    def test_body_above_16_kb_is_rejected(self):
        body = json.dumps({"waited_sec": "A" * (16 * 1024)})
        for kwargs in ({}, {"content_type": "text/plain"}):
            with self.subTest(**kwargs):
                self.assertEqual(self._post(body, **kwargs).status_code, 413)
        self.assertFalse(self.path.exists())

    def test_chunked_body_above_16_kb_is_rejected(self):
        body = json.dumps({"detail": "A" * (16 * 1024)}).encode("ascii")
        resp = self.client.post(
            self.URL, content_type="application/json", headers={"X-CSRFToken": "t"},
            environ_overrides={"wsgi.input": io.BytesIO(body), "wsgi.input_terminated": True,
                               "HTTP_TRANSFER_ENCODING": "chunked"},
        )
        self.assertEqual(resp.status_code, 413)
        self.assertFalse(self.path.exists())

    def test_endpoint_has_its_own_rate_limit(self):
        limiter = self.webui.limiter
        with mock.patch.object(limiter, "enabled", True), \
                mock.patch.object(self.webui, "get_client_ip", return_value="203.0.113.8"):
            limiter.reset()
            self.addCleanup(limiter.reset)
            statuses = [self._post("{}").status_code for _ in range(61)]
        self.assertEqual(statuses, [200] * 60 + [429])


if __name__ == "__main__":
    unittest.main()
