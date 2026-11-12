"""Shared helpers for WebUI tests (not a test module itself)."""
import time

from core import config


def admin_test_client(csrf_token="t"):
    """Flask test client with a logged-in admin session and a fixed CSRF token."""
    from WEBUI import webui

    webui.ensure_admin_user()
    webui.app.config.update(TESTING=True)
    client = webui.app.test_client()
    with client.session_transaction() as sess:
        now = time.time()
        sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                    login_time=now, last_active=now, _csrf_token=csrf_token)
    return client
