"""Security hardening: session revocation, login throttling, uploads, small fixes.

The environment bootstrap (test credentials, temp BASE_DIR) lives in
test_recorder_reliability and runs on import.
"""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # run as tests.test_security_hardening
    from tests.test_recorder_reliability import IsolatedBaseTest

from werkzeug.security import generate_password_hash  # noqa: E402

from core import config, database  # noqa: E402


class WebTestBase(IsolatedBaseTest):
    """Real Flask app with per-test SQLite; Flask-Limiter's per-process memory is off."""

    PASSWORD = "Correct-Horse-42"

    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        webui.app.config.update(TESTING=True)
        patcher = mock.patch.object(webui.limiter, "enabled", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        webui.ensure_admin_user()

    def make_user(self, username="alice", role="admin"):
        self.webui.save_user(username, generate_password_hash(self.PASSWORD), role=role)
        return username

    def login(self, username, password=None, client=None, ip="198.51.100.7"):
        client = client or self.webui.app.test_client()
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "t"
        response = client.post(
            "/login",
            data={"csrf_token": "t", "username": username, "password": password or self.PASSWORD},
            environ_base={"REMOTE_ADDR": ip},
        )
        return client, response

    @staticmethod
    def logged_in(client):
        with client.session_transaction() as sess:
            return bool(sess.get("logged_in"))

    @staticmethod
    def clone(client, app):
        """Second browser holding a copy of the same session cookie."""
        other = app.test_client()
        cookie = client.get_cookie("session")
        other.set_cookie("session", cookie.value)
        return other


class SessionRevocationTests(WebTestBase):
    def test_default_max_age_is_thirty_days(self):
        if os.environ.get("VOD_SESSION_MAX_AGE_SEC"):
            self.skipTest("VOD_SESSION_MAX_AGE_SEC set in the environment")
        self.assertEqual(self.webui.SESSION_MAX_AGE_SEC, 30 * 86400)
        self.assertLessEqual(self.webui.app.permanent_session_lifetime.total_seconds(), 30 * 86400)

    def test_session_older_than_max_age_is_dropped(self):
        user = self.make_user()
        client, _ = self.login(user)
        old = time.time() - 31 * 86400
        with client.session_transaction() as sess:
            sess["login_time"] = old
            sess["last_active"] = old
        response = client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self.assertFalse(self.logged_in(client))

    def test_login_stamps_session_version(self):
        user = self.make_user()
        token = database.bump_session_version(user)
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        client, response = self.login(user)
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as sess:
            self.assertEqual(sess.get("sv"), token)

    def test_session_tokens_are_random_and_never_reused(self):
        user = self.make_user()
        seen = {database.bump_session_version(user) for _ in range(5)}
        self.assertEqual(len(seen), 5)
        self.assertNotIn("", seen)
        self.assertEqual(database.bump_session_version("no-such-user"), "")

    def test_logout_everywhere_revokes_a_copied_cookie(self):
        user = self.make_user()
        client, _ = self.login(user)
        stolen = self.clone(client, self.webui.app)
        self.assertEqual(stolen.get("/admin").status_code, 200)
        with client.session_transaction() as sess:
            token = sess["_csrf_token"]
        response = client.post("/logout/all", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.logged_in(client))
        response = stolen.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self.assertFalse(self.logged_in(stolen))

    def test_plain_logout_ends_only_this_browser(self):
        user = self.make_user()
        client, _ = self.login(user, ip="198.51.100.21")
        other, _ = self.login(user, ip="198.51.100.22")
        with client.session_transaction() as sess:
            token = sess["_csrf_token"]
        client.post("/logout", data={"csrf_token": token})
        self.assertFalse(self.logged_in(client))
        self.assertEqual(other.get("/admin").status_code, 200)
        self.assertTrue(self.logged_in(other))

    def test_admin_form_password_change_keeps_own_session_and_ends_others(self):
        user = self.make_user()
        client, _ = self.login(user, ip="198.51.100.23")
        other, _ = self.login(user, ip="198.51.100.24")
        with client.session_transaction() as sess:
            token = sess["_csrf_token"]
        response = client.post(
            f"/admin/users/update/{user}",
            data={"csrf_token": token, "new_pass": "Another-Horse-44", "role": "admin", "active": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get("/admin").status_code, 200)
        self.assertEqual(other.get("/admin").status_code, 302)
        self.assertFalse(self.logged_in(other))

    def test_password_change_revokes_other_sessions_but_keeps_current(self):
        user = self.make_user()
        client, _ = self.login(user)
        other, _ = self.login(user)
        with client.session_transaction() as sess:
            token = sess["_csrf_token"]
        response = client.post(
            "/api/user/change-password",
            json={"current_password": self.PASSWORD, "new_password": "Another-Horse-43", "csrf_token": token},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(client.get("/admin").status_code, 200)
        self.assertTrue(self.logged_in(client))
        self.assertEqual(other.get("/admin").status_code, 302)
        self.assertFalse(self.logged_in(other))

    def test_cookie_without_version_is_valid_until_first_revocation(self):
        user = self.make_user()
        client = self.webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=user,
                        login_time=now, last_active=now, _csrf_token="t")
        self.assertEqual(client.get("/admin").status_code, 200)
        database.bump_session_version(user)
        self.assertEqual(client.get("/admin").status_code, 302)


class LoginThrottleTests(WebTestBase):
    def test_ipv6_addresses_share_their_64(self):
        key = self.webui.login_rate_key
        self.assertEqual(key("2001:db8:1:2::1"), key("2001:db8:1:2:ffff::9"))
        self.assertEqual(key("2001:db8:1:2::1"), "2001:db8:1:2::/64")
        self.assertNotEqual(key("2001:db8:1:2::1"), key("2001:db8:1:3::1"))
        self.assertEqual(key("::ffff:192.0.2.5"), "192.0.2.5")
        self.assertEqual(key("192.0.2.5"), "192.0.2.5")

    def test_rotating_ipv6_inside_one_64_gets_blocked(self):
        user = self.make_user()
        statuses = []
        for i in range(self.webui.LOGIN_IP_MAX_FAILS + 1):
            _, response = self.login(user, password="wrong", ip=f"2001:db8:aa:bb::{i + 1:x}")
            statuses.append(response.status_code)
        self.assertEqual(statuses[-1], 429)
        # Even the right password from another address in the same /64 is refused.
        _, response = self.login(user, ip="2001:db8:aa:bb::ffff")
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)

    def test_failures_for_one_account_from_one_network_are_throttled(self):
        user = self.make_user()
        # Below the per-address ban (5) so the account counter is what triggers.
        with mock.patch.object(self.webui, "LOGIN_USER_MAX_FAILS", 3):
            for i in range(3):
                _, response = self.login(user, password="wrong", ip="203.0.113.7")
                self.assertNotEqual(response.status_code, 429, f"attempt {i + 1}")
            _, response = self.login(user, ip="203.0.113.7")
            self.assertEqual(response.status_code, 429)
            wait = int(response.headers["Retry-After"])
            self.assertGreater(wait, 0)
            self.assertLessEqual(wait, self.webui.LOGIN_USER_WINDOW_SEC)
            self.assertIn("Too many login attempts", response.get_data(as_text=True))
            # The owner on another network is not affected.
            _, response = self.login(user, ip="198.51.100.200")
            self.assertEqual(response.status_code, 302)
            # Other accounts are not affected either.
            _, response = self.login(config.VOD_USER, password=config.VOD_PASS, ip="203.0.113.8")
            self.assertEqual(response.status_code, 302)

    def test_failures_from_many_networks_cannot_lock_the_owner_out(self):
        user = self.make_user()
        for i in range(self.webui.LOGIN_USER_MAX_FAILS + 5):
            _, response = self.login(user, password="wrong", ip=f"203.0.113.{i + 1}")
            self.assertNotEqual(response.status_code, 429, f"attempt {i + 1}")
        _, response = self.login(user, ip="198.51.100.200")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.logged_in(_))

    def test_account_wide_safety_net_only_slows_networks_that_failed(self):
        user = self.make_user()
        with mock.patch.object(self.webui, "LOGIN_USER_GLOBAL_MAX_FAILS", 6):
            for i in range(6):
                self.login(user, password="wrong", ip=f"203.0.113.{i + 1}")
            # A network that already failed is throttled once the account-wide net is full ...
            _, response = self.login(user, password="wrong", ip="203.0.113.1")
            self.assertEqual(response.status_code, 429)
            # ... a clean network still gets through with the right password.
            client, response = self.login(user, ip="198.51.100.77")
            self.assertEqual(response.status_code, 302)
            self.assertTrue(self.logged_in(client))

    def test_successful_login_resets_the_name_counter(self):
        user = self.make_user()
        for i in range(self.webui.LOGIN_USER_MAX_FAILS - 1):
            self.login(user, password="wrong", ip=f"203.0.113.{i + 1}")
        _, response = self.login(user, ip="198.51.100.9")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.webui.username_lockout_wait(user), 0)

    def test_throttle_message_is_translated(self):
        user = self.make_user()
        for _ in range(self.webui.LOGIN_IP_MAX_FAILS):
            self.login(user, password="wrong", ip="198.51.100.50")
        client = self.webui.app.test_client()
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "t"
        response = client.post(
            "/login",
            data={"csrf_token": "t", "username": user, "password": self.PASSWORD},
            headers={"Accept-Language": "de"},
            environ_base={"REMOTE_ADDR": "198.51.100.50"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("Zu viele Anmeldeversuche", response.get_data(as_text=True))

    def test_flask_limiter_counts_only_post(self):
        with mock.patch.object(self.webui.limiter, "enabled", True):
            self.webui.limiter.reset()
            client = self.webui.app.test_client()
            for _ in range(15):
                response = client.get("/login", environ_base={"REMOTE_ADDR": "192.0.2.77"})
                self.assertEqual(response.status_code, 200)
            self.webui.limiter.reset()



class ResourceUploadTests(WebTestBase):
    """Shared files live on the app origin: allowlist uploads, always download."""

    def setUp(self):
        super().setUp()
        self.client = self.webui.app.test_client()
        with self.client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        accel = mock.patch.object(self.webui, "USE_X_ACCEL", False)
        accel.start()
        self.addCleanup(accel.stop)

    def upload(self, name, body=b"data"):
        import io
        return self.client.post(
            "/files/upload",
            data={"csrf_token": "t", "file": (io.BytesIO(body), name)},
            content_type="multipart/form-data",
        )

    def test_active_content_types_are_rejected(self):
        for name in ("page.html", "page.htm", "image.svg", "doc.xhtml", "feed.xml",
                     "script.js", "tool.exe", "noext"):
            with self.subTest(name=name):
                self.upload(name)
                self.assertFalse(os.path.exists(os.path.join(config.FILES_DIR, name)))

    def test_allowlisted_types_are_stored(self):
        for name in ("notes.pdf", "clip.mp4", "pic.png", "readme.txt"):
            with self.subTest(name=name):
                self.upload(name)
                self.assertTrue(os.path.exists(os.path.join(config.FILES_DIR, name)))

    def test_public_share_is_forced_download(self):
        self.upload("pic.png", b"PNG-header <script>alert(1)</script>")
        token = self.webui.create_share("_file_", "pic.png", "file")
        anon = self.webui.app.test_client()
        for path in (f"/public/media/{token}", f"/public/download/{token}"):
            with self.subTest(path=path):
                response = anon.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
                self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
                self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
                response.close()

    def test_x_accel_share_is_forced_download(self):
        self.upload("pic.png")
        token = self.webui.create_share("_file_", "pic.png", "file")
        with mock.patch.object(self.webui, "USE_X_ACCEL", True):
            response = self.webui.app.test_client().get(f"/public/media/{token}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("X-Accel-Redirect", response.headers)
        self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))

    def test_legacy_disallowed_file_is_not_served_but_deletable(self):
        legacy = os.path.join(config.FILES_DIR, "old.html")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("<script>alert(1)</script>")
        token = self.webui.create_share("_file_", "old.html", "file")
        self.assertEqual(self.webui.app.test_client().get(f"/public/media/{token}").status_code, 404)
        self.client.post("/files/delete/old.html", data={"csrf_token": "t"})
        self.assertFalse(os.path.exists(legacy))



class SmallHardeningTests(WebTestBase):
    def test_env_password_compared_in_constant_time(self):
        with mock.patch.object(self.webui.secrets, "compare_digest",
                               wraps=self.webui.secrets.compare_digest) as cmp:
            with mock.patch.object(self.webui, "load_users", return_value={}):
                self.assertEqual(self.webui.authenticate_user(config.VOD_USER, config.VOD_PASS), "admin")
                self.assertIsNone(self.webui.authenticate_user(config.VOD_USER, "wrong"))
        self.assertGreaterEqual(cmp.call_count, 2)

    def test_unknown_user_still_runs_a_hash_check(self):
        with mock.patch.object(self.webui, "check_password_hash", return_value=False) as check:
            self.assertIsNone(self.webui.authenticate_user("nobody-here", "pw"))
        check.assert_called_once()

    def test_inactive_and_env_accounts_also_pay_the_hash_cost(self):
        user = self.make_user("sleepy")
        self.webui.save_user(user, generate_password_hash(self.PASSWORD), active=False, role="admin")
        with mock.patch.object(self.webui, "check_password_hash", return_value=False) as check:
            self.assertIsNone(self.webui.authenticate_user(user, self.PASSWORD))
        check.assert_called_once()
        with mock.patch.object(self.webui, "load_users", return_value={}), \
                mock.patch.object(self.webui, "check_password_hash", return_value=False) as check:
            self.assertEqual(self.webui.authenticate_user(config.VOD_USER, config.VOD_PASS), "admin")
            self.assertIsNone(self.webui.authenticate_user(config.VOD_USER, "wrong"))
        self.assertEqual(check.call_count, 2)

    def test_metrics_need_an_admin_or_the_token(self):
        viewer = self.make_user("metricsviewer", role="viewer")
        client = self.webui.app.test_client()
        self.assertEqual(client.get("/metrics").status_code, 401)
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="viewer", username=viewer,
                        login_time=now, last_active=now, _csrf_token="t")
        self.assertEqual(client.get("/metrics").status_code, 403)

    def test_ack_endpoints_reject_get(self):
        client = self.webui.app.test_client()
        for path in ("/tts/api/ack?id=1", "/alerts/api/ack?id=1"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 405)

    def test_query_token_is_off_by_default(self):
        if os.environ.get("OVERLAY_ALLOW_QUERY_TOKEN"):
            self.skipTest("OVERLAY_ALLOW_QUERY_TOKEN set in the environment")
        self.assertFalse(self.webui.OVERLAY_ALLOW_QUERY_TOKEN)
        with self.webui.app.test_request_context("/alerts/api/next?token=secret"):
            self.assertEqual(self.webui._alert_token_from_request(), "")
        with self.webui.app.test_request_context("/alerts/api/next", headers={"X-Alert-Token": "secret"}):
            self.assertEqual(self.webui._alert_token_from_request(), "secret")

    def test_anonymous_healthz_has_no_disk_data(self):
        response = self.webui.app.test_client().get("/healthz")
        self.assertIn(response.status_code, (200, 503))
        body = response.get_json()
        self.assertIn("status", body)
        self.assertFalse({"disk_used_pct", "disk_free_gb", "disk"} & set(body))

    def test_admin_healthz_keeps_disk_data(self):
        client = self.webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        body = client.get("/healthz").get_json()
        self.assertIn("disk_used_pct", body)

    def test_vod_debug_log_lines_cannot_be_forged(self):
        client = self.webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        forged = "x\n2026-01-01 00:00:00 - INFO - Successful login: admin\r"
        with self.assertLogs(level="INFO") as logs:
            response = client.post("/api/vod/debug", json={
                "csrf_token": "t", "page": forged,
                "events": [{"id": forged, "src": forged, "kind": "\u2028" + forged}],
            })
        self.assertEqual(response.status_code, 200)
        vod_lines = [line for line in logs.output if "VODDBG" in line]
        self.assertTrue(vod_lines)
        for line in vod_lines:
            self.assertNotIn("\n", line)
            self.assertNotIn("\r", line)
            self.assertNotIn("\u2028", line)


if __name__ == "__main__":
    unittest.main()
