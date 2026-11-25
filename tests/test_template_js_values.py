"""Template values reach inline JavaScript as JSON literals, not inside quoted strings."""

from __future__ import annotations

import json
import pathlib
import re
import time
import unittest

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # run as tests.test_template_js_values
    from tests.test_recorder_reliability import IsolatedBaseTest

from core import config  # noqa: E402

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "WEBUI" / "templates"
FILES = ("clip_editor.html", "live.html", "vod.html", "mobile_player.html")
HOSTILE = "a'b\"c\\d</script> e&f"


class TemplateSourceTests(unittest.TestCase):
    def test_no_plain_value_inside_a_quoted_js_string(self):
        # url_for() output is percent-encoded and the CSRF token is hex.
        allowed = re.compile(r"\{\{\s*(url_for\(|csrf_token\(\))")
        quoted = re.compile(r"'[^'\n]*?(\{\{.*?\}\})[^'\n]*'" r'|"[^"\n]*?(\{\{.*?\}\})[^"\n]*"')
        for name in FILES:
            text = (TEMPLATES / name).read_text(encoding="utf-8")
            scripts = re.findall(r"<script nonce[^>]*>(.*?)</script>", text, flags=re.DOTALL)
            self.assertGreaterEqual(len(scripts), 1, name)
            for line in "\n".join(scripts).splitlines():
                if "|tojson" in line:
                    continue
                for match in quoted.finditer(line):
                    with self.subTest(template=name, line=line.strip()):
                        self.assertRegex(match.group(1) or match.group(2), allowed)


class TemplateRenderTests(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        webui.app.config.update(TESTING=True)
        webui.ensure_admin_user()
        self.client = webui.app.test_client()
        with self.client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")

    def _literal(self, template, pattern, **values):
        """JS value of the one template line matching `pattern`, rendered with `values`."""
        text = (TEMPLATES / template).read_text(encoding="utf-8")
        sources = re.findall(pattern + r"(.*);$", text, flags=re.MULTILINE)
        self.assertEqual(len(sources), 1, (template, sources))
        # ".html" name: autoescaping is on, as for the page templates.
        rendered = self.webui.app.jinja_env.overlay(autoescape=True).from_string(sources[0]).render(**values)
        for raw in ("<", ">", "'", "&", " "):
            self.assertNotIn(raw, rendered)
        return json.loads(rendered)

    def test_hostile_names_round_trip(self):
        for template, pattern, values, expected in (
            ("live.html", r"const currentStreamer = ", {"streamer": HOSTILE}, HOSTILE),
            ("vod.html", r"const key = (?=.*vod_pos)", {"streamer": HOSTILE, "filename": "f.mp4"},
             f"vod_pos_v2_{HOSTILE}_f.mp4"),
            ("vod.html", r"titleInput\.value\.trim\(\) \|\| ", {"filename": HOSTILE}, HOSTILE),
            ("mobile_player.html", r"const chatUrl = ", {"chat_url": "/c?a=1&b=" + HOSTILE},
             "/c?a=1&b=" + HOSTILE),
            ("mobile_player.html", r"const chatUrl = ", {"chat_url": None}, ""),
            ("mobile_player.html", r"const chatUrl = ", {}, ""),
            ("mobile_player.html", r"const legacyVodPositionKey = ", {"filename": HOSTILE},
             "vod_pos_v2_" + HOSTILE),
        ):
            with self.subTest(template=template, pattern=pattern, values=values):
                self.assertEqual(self._literal(template, pattern, **values), expected)

    def test_clip_editor_start_time_is_a_number(self):
        # Python writes nan/inf as the JS literals NaN/Infinity; the page maps them to 0.
        for raw, literal in (("12.5", "12.5"), ("90", "90.0"), ("", "0.0"), ("abc", "0.0"),
                             ("1);alert(1", "0.0"), ("</script>", "0.0"), ("nan", "NaN"),
                             ("inf", "Infinity")):
            with self.subTest(t=raw):
                body = self.client.get("/editor/demochan/v.mp4", query_string={"t": raw}).get_data(as_text=True)
                self.assertIn(f"const initialTime = {literal};", body)
                self.assertNotIn('parseFloat("', body)
                self.assertIn("Number.isFinite(initialTime)", body)
        body = self.client.get("/editor/demochan/v.mp4").get_data(as_text=True)
        self.assertIn("const initialTime = 0.0;", body)


if __name__ == "__main__":
    unittest.main()
