"""JSON bodies are bounded before authentication; uploads keep their own limits."""

from __future__ import annotations

import io
import json
import os
import time
import unittest

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # run as tests.test_json_body_limit
    from tests.test_recorder_reliability import IsolatedBaseTest

from core import config  # noqa: E402

THIRTY_MB = 30 * 1024 * 1024


class CountingStream(io.RawIOBase):
    """Endless-looking request body that records how much of it was read."""

    def __init__(self, size):
        super().__init__()
        self.left = size
        self.consumed = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        count = min(len(buffer), self.left)
        buffer[:count] = b" " * count
        self.left -= count
        self.consumed += count
        return count


class JsonBodyLimitTests(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        webui.app.config.update(TESTING=True)
        webui.ensure_admin_user()
        self.limit = webui.MAX_JSON_BODY_BYTES
        self.anon = webui.app.test_client()

    def _post(self, path, stream, length=None, content_type="application/json"):
        """POST with Content-Length, or chunked the way gunicorn passes it on."""
        environ = {"wsgi.input": stream, "wsgi.input_terminated": True}
        if length is None:
            environ["HTTP_TRANSFER_ENCODING"] = "chunked"
        else:
            environ["CONTENT_LENGTH"] = str(length)
        return self.anon.post(path, content_type=content_type, environ_overrides=environ)

    def test_anonymous_oversized_json_is_rejected_unread(self):
        for path in ("/login", "/api/live/client-debug", "/alerts/api/ack", "/api/twitch/eventsub"):
            with self.subTest(path=path):
                stream = CountingStream(THIRTY_MB)
                resp = self._post(path, stream, THIRTY_MB)
                self.assertEqual(resp.status_code, 413)
                self.assertEqual(stream.consumed, 0)

    def test_json_suffix_types_are_bounded_too(self):
        stream = CountingStream(THIRTY_MB)
        resp = self._post("/login", stream, THIRTY_MB, "application/vnd.api+json; charset=utf-8")
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(stream.consumed, 0)

    def test_chunked_json_stops_at_the_limit(self):
        stream = CountingStream(THIRTY_MB)
        resp = self._post("/api/live/client-debug", stream)
        self.assertEqual(resp.status_code, 413)
        self.assertLessEqual(stream.consumed, self.limit + 64 * 1024)

    def test_understated_content_length_does_not_lift_the_limit(self):
        stream = CountingStream(THIRTY_MB)
        self.assertEqual(self._post("/api/live/client-debug", stream, 5).status_code, 413)
        self.assertLessEqual(stream.consumed, self.limit + 64 * 1024)

    def test_small_chunked_json_still_carries_the_csrf_token(self):
        with self.anon.session_transaction() as sess:
            sess["_csrf_token"] = "t"
        body = json.dumps({"csrf_token": "t"}).encode("ascii")
        resp = self._post("/api/live/client-debug", io.BytesIO(body))
        # Past CSRF (403 otherwise): login_required sends the visitor to /login.
        self.assertEqual(resp.status_code, 302)

    def test_json_up_to_the_limit_is_still_parsed(self):
        with self.anon.session_transaction() as sess:
            sess["_csrf_token"] = "t"
        body = json.dumps({"csrf_token": "t", "pad": ""})
        body = body.replace('""', '"' + "x" * (self.limit - len(body)) + '"')
        self.assertEqual(len(body), self.limit)
        for length in (self.limit, None):
            with self.subTest(length=length):
                resp = self._post("/api/live/client-debug", io.BytesIO(body.encode("ascii")), length)
                self.assertEqual(resp.status_code, 302)

    def test_multipart_upload_above_the_json_limit_still_works(self):
        with self.anon.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        payload = b"\0" * (self.limit * 3)
        resp = self.anon.post(
            "/files/upload",
            data={"csrf_token": "t", "file": (io.BytesIO(payload), "clip.mp4")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(os.path.getsize(os.path.join(config.FILES_DIR, "clip.mp4")), len(payload))


if __name__ == "__main__":
    unittest.main()
