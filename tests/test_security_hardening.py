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
        database.bump_session_version(user)
        client, response = self.login(user)
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as sess:
            self.assertEqual(sess.get("sv"), 1)

    def test_logout_revokes_a_copied_cookie(self):
        user = self.make_user()
        client, _ = self.login(user)
        stolen = self.clone(client, self.webui.app)
        self.assertEqual(stolen.get("/admin").status_code, 200)
        with client.session_transaction() as sess:
            token = sess["_csrf_token"]
        client.post("/logout", data={"csrf_token": token})
        response = stolen.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self.assertFalse(self.logged_in(stolen))

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

    def test_failures_from_many_addresses_lock_the_account_name(self):
        user = self.make_user()
        limit = self.webui.LOGIN_USER_MAX_FAILS
        for i in range(limit):
            _, response = self.login(user, password="wrong", ip=f"203.0.113.{i + 1}")
            self.assertNotEqual(response.status_code, 429, f"attempt {i + 1}")
        _, response = self.login(user, ip="198.51.100.200")
        self.assertEqual(response.status_code, 429)
        wait = int(response.headers["Retry-After"])
        self.assertGreater(wait, 0)
        self.assertLessEqual(wait, self.webui.LOGIN_USER_WINDOW_SEC)
        self.assertIn("Too many login attempts", response.get_data(as_text=True))
        # Other accounts are not affected.
        _, response = self.login(config.VOD_USER, password=config.VOD_PASS, ip="198.51.100.201")
        self.assertEqual(response.status_code, 302)

    def test_successful_login_resets_the_name_counter(self):
        user = self.make_user()
        for i in range(self.webui.LOGIN_USER_MAX_FAILS - 1):
            self.login(user, password="wrong", ip=f"203.0.113.{i + 1}")
        _, response = self.login(user, ip="198.51.100.9")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.webui.username_lockout_wait(user), 0)

    def test_flask_limiter_counts_only_post(self):
        with mock.patch.object(self.webui.limiter, "enabled", True):
            self.webui.limiter.reset()
            client = self.webui.app.test_client()
            for _ in range(15):
                response = client.get("/login", environ_base={"REMOTE_ADDR": "192.0.2.77"})
                self.assertEqual(response.status_code, 200)
            self.webui.limiter.reset()


if __name__ == "__main__":
    unittest.main()
