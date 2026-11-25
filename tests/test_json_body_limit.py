"""Request bodies are bounded before authentication; admin uploads keep the nginx limit."""

from __future__ import annotations

import io
import json
import os
import time
import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # run as tests.test_json_body_limit
    from tests.test_recorder_reliability import IsolatedBaseTest

from core import config  # noqa: E402

THIRTY_MB = 30 * 1024 * 1024


class UnreadableStream:
    """A body that counts read attempts and delivers nothing."""

    def __init__(self):
        self.reads = 0

    def _count(self, *_args):
        self.reads += 1
        return b""

    read = readline = readinto = _count


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


class BodyLimitBase(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        webui.app.config.update(TESTING=True)
        webui.ensure_admin_user()
        self.limit = webui.MAX_REQUEST_BODY_BYTES
        self.anon = webui.app.test_client()

    def _post(self, path, stream, length=None, content_type="application/json"):
        """POST with Content-Length, or chunked the way gunicorn passes it on."""
        # Not content_type=: the test client replaces the boundary of multipart types.
        environ = {"wsgi.input": stream, "wsgi.input_terminated": True, "CONTENT_TYPE": content_type}
        if length is None:
            environ["HTTP_TRANSFER_ENCODING"] = "chunked"
        else:
            environ["CONTENT_LENGTH"] = str(length)
        return self.anon.post(path, environ_overrides=environ)


class JsonBodyLimitTests(BodyLimitBase):
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


class AnyBodyLimitTests(BodyLimitBase):
    """No Content-Type, method or path is a way around the limit."""

    TYPES = ("application/x-www-form-urlencoded", "application/x-www-form-urlencoded; charset=utf-8",
             "multipart/form-data; boundary=b", "multipart/mixed; boundary=b", "text/plain",
             "application/octet-stream", "", "nonsense")
    PATHS = ("/login", "/files/upload", "/alerts/api/ack", "/api/twitch/eventsub", "/no/such/page")

    def _login(self, username, role):
        with self.anon.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role=role, username=username,
                        login_time=now, last_active=now, _csrf_token="t")

    def test_oversized_body_of_any_type_is_rejected_unread(self):
        for path in self.PATHS:
            for content_type in self.TYPES:
                with self.subTest(path=path, content_type=content_type):
                    stream = CountingStream(THIRTY_MB)
                    resp = self._post(path, stream, THIRTY_MB, content_type)
                    self.assertEqual(resp.status_code, 413)
                    self.assertEqual(stream.consumed, 0)

    def test_chunked_body_of_any_type_stops_at_the_limit(self):
        for path in self.PATHS:
            for content_type in self.TYPES:
                with self.subTest(path=path, content_type=content_type):
                    stream = CountingStream(THIRTY_MB)
                    resp = self._post(path, stream, None, content_type)
                    self.assertEqual(resp.status_code, 413)
                    self.assertLessEqual(stream.consumed, self.limit + 64 * 1024)

    def test_get_with_a_body_is_bounded_too(self):
        stream = CountingStream(THIRTY_MB)
        resp = self.anon.get("/login", content_type="application/json", environ_overrides={
            "wsgi.input": stream, "wsgi.input_terminated": True, "CONTENT_LENGTH": str(THIRTY_MB)})
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(stream.consumed, 0)

    def test_form_with_too_many_fields_is_rejected(self):
        for fields, status in ((self.webui.MAX_FORM_FIELDS, 302), (self.webui.MAX_FORM_FIELDS + 1, 413)):
            with self.subTest(fields=fields):
                body = b"&".join([b"a=1"] * fields)
                resp = self._post("/login", io.BytesIO(body), len(body), "application/x-www-form-urlencoded")
                self.assertEqual(resp.status_code, status)

    def test_small_forms_are_parsed_as_before(self):
        with self.anon.session_transaction() as sess:
            sess["_csrf_token"] = "t"
        for body, content_type in (
            (b"csrf_token=t&language=de", "application/x-www-form-urlencoded"),
            (b'--b\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\nt\r\n--b--\r\n',
             "multipart/form-data; boundary=b"),
        ):
            for length in (len(body), None):
                with self.subTest(content_type=content_type, length=length):
                    # Past CSRF (403 otherwise): login_required sends the visitor to /login.
                    resp = self._post("/agb/accept", io.BytesIO(body), length, content_type)
                    self.assertEqual(resp.status_code, 302)

    def test_only_admins_may_upload_more(self):
        from werkzeug.security import generate_password_hash
        self.webui.save_user("bob", generate_password_hash("Correct-Horse-42"), role="user")
        # The role in the session is replaced by the role of the account.
        self._login("bob", "admin")
        with mock.patch.object(self.webui, "current_user_needs_terms_acceptance", return_value=False):
            stream = CountingStream(THIRTY_MB)
            resp = self._post("/files/upload", stream, THIRTY_MB, "multipart/form-data; boundary=b")
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(stream.consumed, 0)

    def test_upload_routes_read_no_byte_from_a_non_admin(self):
        for path in ("/files/upload", "/admin/banner/update", "/streamer/demochan/banner/update",
                     "/streamer/demochan/avatar/update"):
            for content_type in ("multipart/form-data; boundary=b", "application/x-www-form-urlencoded", "text/plain"):
                with self.subTest(path=path, content_type=content_type):
                    stream = UnreadableStream()
                    resp = self._post(path, stream, 1000, content_type)
                    self.assertEqual(resp.status_code, 403)
                    self.assertEqual(stream.reads, 0)

    def test_admin_multipart_outside_the_upload_routes_is_bounded(self):
        self._login(config.VOD_USER, "admin")
        stream = CountingStream(THIRTY_MB)
        resp = self._post("/api/vod/debug", stream, THIRTY_MB, "multipart/form-data; boundary=b")
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(stream.consumed, 0)

    def test_admin_upload_routes_accept_large_files(self):
        self._login(config.VOD_USER, "admin")
        payload = b"\0" * (self.limit * 2)
        for path, field, name in (
            ("/files/upload", "file", "clip.mp4"), ("/streamer/demochan/banner/update", "banner_file", "a.png"),
            ("/admin/banner/update", "banner_file", "b.png"),
            ("/streamer/demochan/avatar/update", "avatar_file", "c.png"),
        ):
            with self.subTest(path=path):
                resp = self.anon.post(path, data={"csrf_token": "t", field: (io.BytesIO(payload), name)},
                                      content_type="multipart/form-data")
                self.assertEqual(resp.status_code, 302)

    def test_upload_endpoints_are_exactly_the_views_that_read_files(self):
        import inspect
        readers = {name for name, view in self.webui.app.view_functions.items()
                   if "request.files" in inspect.getsource(inspect.unwrap(view))}
        self.assertEqual(readers, set(self.webui.UPLOAD_ENDPOINTS))


if __name__ == "__main__":
    unittest.main()
