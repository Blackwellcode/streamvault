"""Frontend behaviour introduced by the 2026-11-12 UI audit (PR #13).

- css_url: the Jinja filter that guards banner/hero image URLs inside CSS url('...')
- chat fallbacks escape Twitch chat text when chat_emotes.js is unavailable
- destructive actions render the shared data-confirm dialog hooks
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_recorder_reliability import IsolatedBaseTest  # noqa: E402  (sets VOD_* env first)
from webui_test_helpers import admin_test_client  # noqa: E402

TEMPLATES = Path(__file__).resolve().parents[1] / "WEBUI" / "templates"


def require_node(test):
    """node runs the JS logic tests; in CI (CI=true) a missing node is a failure, not a skip."""
    if shutil.which("node"):
        return
    if os.environ.get("CI"):
        test.fail("node is required in CI for the JavaScript logic tests")
    test.skipTest("node not installed")


def run_node(script):
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return out.stdout


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

    def test_escape_chat_text_neutralises_markup(self):
        require_node(self)
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
        return admin_test_client()

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


class OverlayTokenTransportTests(unittest.TestCase):
    """OBS overlays: token from #token= (legacy ?token= moved into the fragment), header only,
    no polling without a token, 401 stops polling, errors back off."""

    STATIC = TEMPLATES.parent / "static"
    COMMON = (TEMPLATES.parent / "static" / "overlay_common.js").as_posix()

    def test_overlay_scripts_use_the_shared_helper_and_send_headers_only(self):
        for name, header in (("tts_overlay.js", "X-TTS-Token"), ("alerts_overlay.js", "X-Alert-Token")):
            with self.subTest(script=name):
                source = (self.STATIC / name).read_text(encoding="utf-8")
                self.assertIn("overlay.readToken()", source)
                self.assertIn('"%s": token' % header, source)
                self.assertIn("overlay.createPoller(", source)
                self.assertNotIn("setInterval(", source)
                self.assertNotRegex(source, r'searchParams\.set\("token"')
                # no token: the script returns before any poller exists
                guard = source.index("if (!token) {")
                self.assertLess(guard, source.index("overlay.createPoller("))
                self.assertIn("return;", source[guard:guard + 200])
        for page in ("tts_overlay.html", "alerts_overlay.html"):
            html = (self.STATIC / page).read_text(encoding="utf-8")
            self.assertLess(html.index("/static/overlay_common.js"), html.index("_overlay.js\"></script>", html.index("/static/overlay_common.js") + 1))

    def test_parse_token_prefers_fragment_and_moves_query_token_into_it(self):
        require_node(self)
        script = """
const o = require(%s);
const cases = [
  "https://h/tts/overlay?streamer=a&token=Q#toast=0",
  "https://h/alerts/overlay#token=F",
  "https://h/alerts/overlay?token=Q#token=F",
  "https://h/alerts/overlay",
];
process.stdout.write(JSON.stringify(cases.map(function (u) {
  const r = o.parseToken(u);
  return [r.token, r.fromQuery, r.cleanUrl, r.params.get("streamer"), r.params.get("toast"), r.params.has("token")];
})));
""" % json.dumps(self.COMMON)
        result = json.loads(run_node(script))
        self.assertEqual(result, [
            ["Q", True, "https://h/tts/overlay?streamer=a#toast=0&token=Q", "a", "0", False],
            ["F", False, "https://h/alerts/overlay#token=F", None, None, False],
            ["F", False, "https://h/alerts/overlay#token=F", None, None, False],
            ["", False, "https://h/alerts/overlay", None, None, False],
        ])

    def test_poller_backs_off_on_errors_and_stops_on_401(self):
        require_node(self)
        script = """
const o = require(%s);
const results = ["error", "error", "error", "error", "error", "error", "ok", "unauthorized", "ok"];
const delays = [];
let pending = null;
let unauthorized = 0;
let calls = 0;
const poller = o.createPoller(async function () { return results[calls++]; }, {
  interval: 1000, maxInterval: 8000,
  schedule: function (fn, ms) { delays.push(ms); pending = fn; return delays.length; },
  cancel: function () { pending = null; },
  onUnauthorized: function () { unauthorized += 1; },
});
(async function () {
  const flush = function () { return new Promise(function (r) { setImmediate(r); }); };
  poller.start();
  await flush();
  for (let i = 0; i < 20 && pending; i++) {
    const fn = pending; pending = null;
    await fn();
  }
  await flush();
  process.stdout.write(JSON.stringify({delays: delays, calls: calls, stopped: poller.stopped, unauthorized: unauthorized}));
})();
""" % json.dumps(self.COMMON)
        result = json.loads(run_node(script))
        self.assertEqual(result["delays"], [2000, 4000, 8000, 8000, 8000, 8000, 1000])
        self.assertEqual(result["calls"], 8)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["unauthorized"], 1)


class LogoutEverywhereTemplateTests(IsolatedBaseTest):
    """POST /logout/all (components/logout_everywhere.html) in the rail, the bottom nav and both
    mobile templates, rendered with CSRF token and confirmation."""

    DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0"
    MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148 Safari/604.1"

    def _forms(self, html):
        return re.findall(r'<form method="post" action="/logout/all"([^>]*)>\s*'
                          r'<input type="hidden" name="csrf_token" value="t">', html)

    def test_dashboard_and_mobile_streamer_render_the_form(self):
        (self.base / "teststreamer").mkdir()
        client = admin_test_client()
        cases = (("/", self.DESKTOP, 2), ("/", self.MOBILE, 1), ("/streamer/teststreamer", self.MOBILE, 1))
        for path, ua, expected in cases:
            with self.subTest(path=path, mobile=ua is self.MOBILE):
                response = client.get(path, headers={"User-Agent": ua})
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                if path.startswith("/streamer/"):
                    self.assertIn("mobile-body", html)  # mobile_streamer.html, not the desktop page
                forms = self._forms(html)
                self.assertEqual(len(forms), expected)
                for attrs in forms:
                    self.assertIn("data-confirm=", attrs)
                    self.assertIn("data-confirm-danger", attrs)
        for name in ("components/sidebar.html", "mobile_index.html", "mobile_streamer.html"):
            source = (TEMPLATES / name).read_text(encoding="utf-8")
            self.assertIn("components/logout_everywhere.html", source)
            self.assertNotIn("url_for('logout_everywhere')", source)


class ObsOverlayPanelTests(IsolatedBaseTest):
    """Options > Settings recommends overlay URLs with the token in the fragment."""

    def test_admin_panel_shows_fragment_token_urls(self):
        html = admin_test_client().get("/admin").get_data(as_text=True)
        for field_id, path in (("obsTtsUrl", "/tts/overlay?streamer=CHANNEL#token=TOKEN"),
                               ("obsAlertsUrl", "/alerts/overlay#token=TOKEN")):
            with self.subTest(field=field_id):
                match = re.search(r'<input id="%s"[^>]*value="([^"]+)"' % field_id, html)
                self.assertIsNotNone(match)
                value = match.group(1)
                self.assertTrue(value.endswith(path), value)
                self.assertNotIn("?token=", value)
                self.assertNotIn("&token=", value)


class NoNativeConfirmTests(unittest.TestCase):
    def test_templates_use_the_confirmation_dialog(self):
        for path in TEMPLATES.rglob("*.html"):
            source = path.read_text(encoding="utf-8")
            with self.subTest(template=path.name):
                self.assertNotRegex(source, r"(?<![\w.])confirm\(|window\.confirm\(")


class StorageProviderFieldIdTests(unittest.TestCase):
    """Add and edit provider modals render the same fields; their ids must differ."""

    def test_field_ids_are_prefixed_with_the_modal_container(self):
        source = (TEMPLATES / "storage.html").read_text(encoding="utf-8")
        self.assertIn("const id = idPrefix + '-cfg_' + key;", source)
        self.assertIn("return fieldMarkup(field, values || {}, containerId);", source)
        self.assertIn("<label for=\"' + id + '\">", source)
        self.assertIn("renderFields('add-provider-fields'", source)
        self.assertIn("renderFields('edit-provider-fields'", source)


if __name__ == "__main__":
    unittest.main()
