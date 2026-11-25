"""gunicorn access log: request data cannot forge lines, send terminal escapes or leak queries."""

from __future__ import annotations

import base64
import datetime
import io
import logging
import pathlib
import re
import sys
import types
import unittest

from WEBUI import gunicorn_logging

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOGGER_FLAG = "--logger-class WEBUI.gunicorn_logging.EscapedPathLogger"
LOG_FORMAT = '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(f)s" "%(a)s"'


def wsgi_str(text: str) -> str:
    """Text the way PEP 3333 servers pass it: UTF-8 bytes decoded as latin-1."""
    return text.encode("utf-8").decode("latin-1")


class EscapeFunctionTests(unittest.TestCase):
    def test_decoded_path_is_encoded_again(self):
        for decoded, expected in (
            ("/live/watch/demochan", "/live/watch/demochan"),
            ("/x\n203.0.113.9 - admin", "/x%0A203.0.113.9%20-%20admin"),
            ("/x\r\x1b[2J", "/x%0D%1B[2J"),
            ("/x\x00\x7f\x85", "/x%00%7F%85"),
            (wsgi_str("/x y"), "/x%E2%80%A8y"),
            ("/x y", "/x%E2%80%A8y"),
            ('/x" 200 1 "\\', "/x%22%20200%201%20%22%5C"),
            ("/x%0A", "/x%250A"),
        ):
            with self.subTest(path=decoded):
                self.assertEqual(gunicorn_logging.escape_path(decoded), expected)

    def test_referer_loses_query_and_fragment(self):
        for referer, expected in (
            ("https://h.example/vod?token=s3cret", "https://h.example/vod"),
            ("https://h.example/vod#token=s3cret", "https://h.example/vod"),
            ("https://h.example/#a?token=s3cret", "https://h.example/"),
            ("https://h.example/a%20b", "https://h.example/a%20b"),
            ("\x1b[2J\x85", "%1B[2J%85"),
            ("-", "-"),
        ):
            with self.subTest(referer=referer):
                self.assertEqual(gunicorn_logging.escape_referer(referer), expected)

    def test_user_agent_stays_readable(self):
        agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        self.assertEqual(gunicorn_logging.escape_text(agent), agent)
        self.assertEqual(gunicorn_logging.escape_text('a\x1b[31m"\\\x85'), "a%1B[31m%22%5C%85")

    def test_atoms_of_other_types_are_kept(self):
        atoms = {"U": None, "B": 12, "s": 200, "{wsgi.version}e": (1, 0), "a": "x\n", "u": "a b "}
        self.assertEqual(
            gunicorn_logging.escape_atoms(atoms),
            {"U": None, "B": 12, "s": 200, "{wsgi.version}e": (1, 0), "a": "x%0A", "u": "a%20b%E2%80%A8"},
        )


# Not "skip if gunicorn is missing": on Linux it is a requirement, so a missing
# gunicorn must fail there instead of skipping the only end-to-end check.
@unittest.skipIf(sys.platform == "win32", "gunicorn does not run on Windows (fcntl)")
class EscapedPathLoggerTests(unittest.TestCase):
    """Real gunicorn logger: what ends up in the access log."""

    def _log(self, **environ):
        from gunicorn.config import Config

        cfg = Config()
        cfg.set("logger_class", "WEBUI.gunicorn_logging.EscapedPathLogger")
        cfg.set("access_log_format", LOG_FORMAT)
        cfg.set("accesslog", "-")
        self.assertIs(cfg.logger_class, gunicorn_logging.EscapedPathLogger)
        log = cfg.logger_class(cfg)
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        handler.setFormatter(logging.Formatter("%(message)s"))
        previous = log.access_log.handlers
        log.access_log.handlers = [handler]
        self.addCleanup(setattr, log.access_log, "handlers", previous)
        env = {
            "REMOTE_ADDR": "203.0.113.7", "REQUEST_METHOD": "GET", "RAW_URI": "/",
            "SERVER_PROTOCOL": "HTTP/1.1", "PATH_INFO": "/", "QUERY_STRING": "", **environ,
        }
        log.access(
            types.SimpleNamespace(status="200 OK", sent=5, headers=[]),
            types.SimpleNamespace(headers=[]), env, datetime.timedelta(milliseconds=3),
        )
        return sink.getvalue()

    def _assert_one_clean_line(self, output):
        self.assertTrue(output.endswith("\n"))
        self.assertTrue(output[:-1].isprintable(), ascii(output))
        self.assertEqual(len(output.splitlines()), 1, ascii(output))

    def test_path_cannot_forge_a_line(self):
        for raw, logged in (("/x\n1.2.3.4 - - ok", "/x%0A1.2.3.4%20-%20-%20ok"),
                            ("/x\x1b[2J", "/x%1B[2J"),
                            (wsgi_str("/x y"), "/x%E2%80%A8y"),
                            ('/x" 200 5 "-" "-', "/x%22%20200%205%20%22-%22%20%22-")):
            with self.subTest(path=raw):
                output = self._log(PATH_INFO=raw)
                self._assert_one_clean_line(output)
                self.assertIn(f'"GET {logged} HTTP/1.1" 200 5', output)

    def test_headers_cannot_forge_a_line_or_leak_a_query(self):
        user = base64.b64encode("eve\nadmin :pw".encode("utf-8")).decode("ascii")
        output = self._log(
            HTTP_REFERER="https://h.example/vod?token=s3cret", HTTP_USER_AGENT='UA\x1b[2J\x85"',
            HTTP_AUTHORIZATION="Basic " + user,
        )
        self._assert_one_clean_line(output)
        self.assertNotIn("s3cret", output)
        self.assertIn('"https://h.example/vod" "UA%1B[2J%85%22"', output)
        self.assertIn(" eve%0Aadmin%E2%80%A8 [", output)


class LoggerClassConfiguredTests(unittest.TestCase):
    CONFIGS = ("install.sh", "INSTALL.md", "docker-worker.conf")

    def test_gunicorn_starts_with_the_escaping_logger(self):
        for name in self.CONFIGS:
            text = re.sub(r"\\\n\s*", " ", (ROOT / name).read_text(encoding="utf-8"))
            commands = [line for line in text.splitlines()
                        if re.search(r"(ExecStart=|command=)\S*/gunicorn ", line)]
            with self.subTest(config=name):
                self.assertGreaterEqual(len(commands), 1)
                for command in commands:
                    self.assertIn(LOGGER_FLAG + " ", command)


if __name__ == "__main__":
    unittest.main()
