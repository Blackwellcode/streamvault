"""Hostile requests before login: no 500, no forged log lines."""

from __future__ import annotations

import io
import logging
import pathlib
import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # run as tests.test_request_robustness
    from tests.test_recorder_reliability import IsolatedBaseTest

NESTED = "[" * 200000
ROOT = pathlib.Path(__file__).resolve().parents[1]


class RequestRobustnessTests(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        # Production behaviour: unhandled exceptions become a logged 500.
        for key, value in (("TESTING", False), ("PROPAGATE_EXCEPTIONS", False)):
            patcher = mock.patch.dict(webui.app.config, {key: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        limiter = mock.patch.object(webui.limiter, "enabled", False)
        limiter.start()
        self.addCleanup(limiter.stop)
        webui.ensure_admin_user()
        self.client = webui.app.test_client()

    def _capture(self, logger):
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        return sink

    def test_deeply_nested_json_is_a_bad_request(self):
        for path in ("/login", "/api/live/client-debug", "/alerts/api/ack", "/channels/add"):
            for prefix in ("", '{"csrf_token": ', '{"token": '):
                with self.subTest(path=path, prefix=prefix):
                    resp = self.client.post(path, data=prefix + NESTED, content_type="application/json")
                    self.assertEqual(resp.status_code, 400)

    def test_json_of_the_wrong_type_is_no_server_error(self):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "t"
        for path, status in (("/alerts/api/ack", 401), ("/api/live/client-debug", 403)):
            for body in ("[1]", '"x"', "1", '{"token": [1]}', '{"csrf_token": {"a": 1}}',
                         '{"csrf_token": "\\u00e4"}', '{"csrf_token": "\\ud800"}'):
                with self.subTest(path=path, body=body):
                    resp = self.client.post(path, data=body, content_type="application/json")
                    self.assertEqual(resp.status_code, status)

    def test_alert_ack_body_of_the_wrong_type_is_a_bad_request(self):
        from core import alert_sounds
        with mock.patch.object(alert_sounds, "token_ok", return_value=True):
            for body in ("[1]", '{"id": [1]}', '{"id": {"a": 1}}'):
                with self.subTest(body=body):
                    resp = self.client.post("/alerts/api/ack", data=body, content_type="application/json")
                    self.assertEqual(resp.status_code, 400)

    def test_error_log_escapes_path_and_exception_text(self):
        def broken(**_values):
            raise ValueError("boom\nFORGED by exception\x1b[2J x")

        sink = self._capture(self.webui.app.logger)
        with mock.patch.dict(self.webui.app.view_functions, {"twitch_watch": broken}):
            resp = self.client.get("/live/watch/x%0AFORGED%20by%20path%1b%5b2J%E2%80%A8y")
        self.assertEqual(resp.status_code, 500)
        lines = sink.getvalue().rstrip("\n").split("\n")
        self.assertIn("/live/watch/x\\nFORGED by path\\x1b[2J\\u2028y", lines[0])
        self.assertTrue(lines[0].startswith("ERROR "))
        self.assertIn("Traceback (most recent call last):", lines[1])
        self.assertTrue(any("FORGED by exception" in line for line in lines[1:]))
        for line in lines[1:]:
            self.assertTrue(line.startswith("  "), ascii(line))
        for line in lines:
            self.assertTrue(line.isprintable(), ascii(line))

    def test_log_filter_is_attached_once_and_keeps_ordinary_records(self):
        self.webui.create_app()
        filters = [f for f in self.webui.app.logger.filters if isinstance(f, self.webui._LogEscapeFilter)]
        self.assertEqual(len(filters), 1)
        sink = self._capture(self.webui.app.logger)
        self.webui.app.logger.warning("Grüße an %s, %d", "demochan", 3)
        self.assertEqual(sink.getvalue(), "WARNING Grüße an demochan, 3\n")

    def test_failed_login_name_cannot_forge_a_line(self):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "t"
        with self.assertLogs(level="WARNING") as logs:
            self.client.post("/login", data={"csrf_token": "t", "username": "eve\nFORGED\x1b[2J", "password": "x"})
        lines = [line for line in logs.output if "LOGIN FAILED" in line]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].isprintable(), ascii(lines[0]))


class GitignoreTests(unittest.TestCase):
    def test_every_env_variant_is_ignored_but_the_example(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", lines)
        self.assertIn(".env.*", lines)
        # The last matching pattern wins.
        self.assertGreater(lines.index("!.env.example"), lines.index(".env.*"))
        self.assertFalse([x for x in lines[lines.index("!.env.example") + 1:] if x in (".env*", ".env.*", "*.example")])


if __name__ == "__main__":
    unittest.main()
