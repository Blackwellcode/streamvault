"""Former `except Exception: pass` paths: specific error classes, logged.

Each test pins two things: the expected failure is still absorbed (the caller
keeps its fallback) and it now leaves a log line behind.
The environment bootstrap (test credentials, temp BASE_DIR, fcntl shim on
Windows) lives in test_recorder_reliability and runs on import.
"""

from __future__ import annotations

import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401
except ImportError:  # run as tests.test_error_handling
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401

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


if __name__ == "__main__":
    unittest.main()
