"""Frontend behaviour introduced by the 2026-11-12 UI audit (PR #13).

- css_url: the Jinja filter that guards banner/hero image URLs inside CSS url('...')
- chat fallbacks escape Twitch chat text when chat_emotes.js is unavailable
- destructive actions render the shared data-confirm dialog hooks
"""
import json
import re
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_recorder_reliability import IsolatedBaseTest  # noqa: E402  (sets VOD_* env first)

from core import config  # noqa: E402

TEMPLATES = Path(__file__).resolve().parents[1] / "WEBUI" / "templates"


class CssUrlFilterTests(IsolatedBaseTest):
    def test_accepts_same_origin_paths_and_https_urls(self):
        from WEBUI.webui import css_url

        for value in ("/static/banners/a.jpg", "/avatar/lunarlab", "https://cdn.example.org/b.png",
                      "HTTPS://cdn.example.org/b.png?x=1&y=2"):
            with self.subTest(value=value):
                self.assertEqual(css_url(value), value)

    def test_rejects_values_that_could_leave_the_url_token(self):
        from WEBUI.webui import css_url

        for value in (None, "", "//evil.example/x.png", "http://plain.example/x.png", "javascript:alert(1)",
                      "data:image/svg+xml,<svg>", "/a.png') ; background:url(//x", "/a\\b.png",
                      'https://x.example/a"b.png', "https://x.example/a b.png", "https://x.example/a\tb.png",
                      "https://x.example/a(b).png", "/a<b", "https://", "relative/path.png"):
            with self.subTest(value=value):
                self.assertEqual(css_url(value), "")

    def test_registered_as_jinja_filter_and_used_by_the_streamer_template(self):
        from WEBUI import webui

        rendered = webui.app.jinja_env.from_string(
            "url('{{ good|css_url }}') url('{{ bad|css_url }}')"
        ).render(good="https://cdn.example.org/b.png", bad="/a.png'); color:red")
        self.assertEqual(rendered, "url('https://cdn.example.org/b.png') url('')")
        source = (TEMPLATES / "streamer.html").read_text(encoding="utf-8")
        self.assertIn("{% set banner_css_url = banner_url|css_url %}", source)
        self.assertIn("|css_url %}", source.split("hero_css_url", 1)[1].split("\n", 1)[0])


class ChatFallbackEscapingTests(unittest.TestCase):
    """twitch_watch_chat.js.html and mobile_player.html render chat without chat_emotes.js."""

    FILES = ("twitch_watch_chat.js.html", "mobile_player.html")

    def _escape_function(self, name):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        match = re.search(r"function escapeChatText\(value\) \{.*?\n\s*\}\n", source, flags=re.S)
        self.assertIsNotNone(match, name)
        return source, match.group(0)

    def test_fallbacks_go_through_escape_chat_text(self):
        for name in self.FILES:
            with self.subTest(template=name):
                source, _ = self._escape_function(name)
                self.assertNotRegex(source, r"\|\|\s*\(item\.msg \|\| ''\)\s*;")
                self.assertNotRegex(source, r":\s*\(msg\.(msg|user) \|\| ''\)\s*;")
                self.assertIn("escapeChatText(", source.split("function escapeChatText", 1)[1])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_escape_chat_text_neutralises_markup(self):
        payload = "<img src=x onerror=alert(1)>&\"'"
        expected = "&lt;img src=x onerror=alert(1)&gt;&amp;&quot;&#39;"
        for name in self.FILES:
            with self.subTest(template=name):
                _, function = self._escape_function(name)
                script = function + "\nprocess.stdout.write(escapeChatText(%s) + '|' + escapeChatText(null));" % json.dumps(payload)
                out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
                self.assertEqual(out.stdout, expected + "|")


class ConfirmDialogTemplateTests(IsolatedBaseTest):
    """Destructive admin actions use the shared data-confirm dialog (static/sv_confirm.js)."""

    def _client(self):
        from WEBUI import webui
        webui.ensure_admin_user()
        webui.app.config.update(TESTING=True)
        client = webui.app.test_client()
        with client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        return client

    def test_admin_page_renders_confirm_hooks_and_loads_the_dialog_script(self):
        html = self._client().get("/admin").get_data(as_text=True)
        self.assertIn("sv_confirm.js", html)
        restart = re.search(r'<form method="post" action="[^"]*restart[^"]*"[^>]*>', html, flags=re.S)
        self.assertIsNotNone(restart)
        self.assertIn('data-confirm="', restart.group(0))
        self.assertIn('data-confirm-ok="', restart.group(0))
        self.assertNotIn("window.confirm(", html)
        self.assertNotIn("return confirm(", html)
        # svT() strings for the dialog come from JS_MESSAGES via components/js_i18n.html
        self.assertIn("window.SV_I18N", html)

    def test_no_template_uses_the_old_parallel_confirm_attributes(self):
        for path in TEMPLATES.rglob("*.html"):
            source = path.read_text(encoding="utf-8")
            with self.subTest(template=path.name):
                self.assertNotIn("data-streamer-confirm", source)
                self.assertNotIn("data-storage-confirm", source)
                self.assertNotRegex(source, r"\bonsubmit=|\bonclick=")

    def test_dialog_strings_are_registered_for_translation(self):
        from WEBUI import i18n

        for text in ("Please confirm", "Confirm", "Cancel"):
            self.assertIn(text, i18n.JS_MESSAGES)


if __name__ == "__main__":
    unittest.main()
