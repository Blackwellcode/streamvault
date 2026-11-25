"""UI fonts are self-hosted: no page, template or CSP points at Google Fonts.

Inter and IBM Plex Mono ship as woff2 in WEBUI/static/fonts (SIL OFL 1.1) and
are loaded through fonts/fonts.css, so a visitor's IP never reaches Google.
"""

from __future__ import annotations

import os
import re
import time
import unittest

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401
except ImportError:  # run as tests.test_self_hosted_fonts
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401

from core import config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBUI_DIR = os.path.join(ROOT, "WEBUI")
FONTS_DIR = os.path.join(WEBUI_DIR, "static", "fonts")
GOOGLE_FONT_HOSTS = re.compile(r"fonts\.(googleapis|gstatic)\.com")
FONT_URL = re.compile(r"url\(\./([A-Za-z0-9._-]+\.woff2)\)")
FONTS_CSS_LINK = re.compile(r'<link rel="stylesheet" href="/static/fonts/fonts\.css\?v=[^"]+">')

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class FontFilesTests(unittest.TestCase):
    def test_fonts_css_references_existing_woff2_files(self):
        css = _read(os.path.join(FONTS_DIR, "fonts.css"))
        referenced = set(FONT_URL.findall(css))
        self.assertTrue(referenced)
        for name in sorted(referenced):
            with self.subTest(font=name):
                with open(os.path.join(FONTS_DIR, name), "rb") as fh:
                    self.assertEqual(fh.read(4), b"wOF2")
        on_disk = {n for n in os.listdir(FONTS_DIR) if n.endswith(".woff2")}
        self.assertEqual(on_disk, referenced)

    def test_fonts_css_declares_the_families_the_ui_uses(self):
        css = _read(os.path.join(FONTS_DIR, "fonts.css"))
        for family, weights in (("Inter", (400, 500, 600, 700)), ("IBM Plex Mono", (400, 500, 600))):
            for weight in weights:
                with self.subTest(family=family, weight=weight):
                    self.assertRegex(
                        css,
                        r"font-family: '" + re.escape(family) + r"';\s+font-style: normal;\s+"
                        r"font-display: swap;\s+font-weight: " + str(weight) + ";",
                    )
        self.assertNotRegex(css, r"url\((https?:)?//")

    def test_license_texts_ship_with_the_fonts(self):
        for name in ("LICENSE-Inter.txt", "LICENSE-IBM-Plex-Mono.txt"):
            with self.subTest(license=name):
                self.assertIn("SIL OPEN FONT LICENSE Version 1.1", _read(os.path.join(FONTS_DIR, name)))

    def test_mono_token_names_ibm_plex_mono_before_courier(self):
        tokens = _read(os.path.join(WEBUI_DIR, "static", "css", "tokens.css"))
        for token in ("--nebula-font-mono", "--ns-font-mono"):
            with self.subTest(token=token):
                stack = re.search(re.escape(token) + r":\s*([^;]+);", tokens).group(1)
                self.assertTrue(stack.startswith("'IBM Plex Mono'"), stack)


class StandalonePagesLoadFontsTests(unittest.TestCase):
    TEMPLATES_DIR = os.path.join(WEBUI_DIR, "templates")
    # OBS overlays render in system fonts on a transparent background.
    SYSTEM_FONT_OVERLAYS = {"chat_obs.html", "live_obs.html"}

    def test_shared_stylesheet_include_links_fonts_css(self):
        styles = _read(os.path.join(self.TEMPLATES_DIR, "components", "styles.html"))
        self.assertEqual(styles.count('<link rel="stylesheet" href="{{ UI_FONTS_HREF }}">'), 1)

    def test_every_standalone_page_includes_the_shared_stylesheets(self):
        for name in sorted(os.listdir(self.TEMPLATES_DIR)):
            if not name.endswith(".html"):
                continue
            source = _read(os.path.join(self.TEMPLATES_DIR, name))
            if "{% extends" in source or "<head" not in source.lower():
                continue
            with self.subTest(template=name):
                if name in self.SYSTEM_FONT_OVERLAYS:
                    self.assertNotRegex(source, r"font-family:[^;]*(Inter|IBM Plex|--ns-font|--nebula-font)")
                else:
                    self.assertIn("{% include 'components/styles.html' %}", source)
                    self.assertNotIn("UI_FONTS_HREF", source)

    def test_code_elements_use_the_mono_token(self):
        css = _read(os.path.join(WEBUI_DIR, "static", "style.css"))
        self.assertRegex(css, r":where\(code, kbd, samp, pre\) \{\s+font-family: var\(--ns-font-mono\);")

    def test_mobile_live_page_shares_wait_card_and_chat_empty_styles(self):
        css = _read(os.path.join(WEBUI_DIR, "static", "style.css"))
        for selector in (".twitch-proxy-wait-card {", ".twitch-chat-empty {", ".twitch-chat-empty.compact {"):
            with self.subTest(selector=selector):
                self.assertIn("body:is(.page-twitch-watch, .page-twitch-watch-mobile) " + selector, css)


class NoGoogleFontsInSourcesTests(unittest.TestCase):
    def test_templates_and_inline_templates_do_not_reference_google_fonts(self):
        for base, _dirs, files in os.walk(WEBUI_DIR):
            if "__pycache__" in base:
                continue
            for name in files:
                if not name.endswith((".html", ".py", ".css", ".js")):
                    continue
                path = os.path.join(base, name)
                with self.subTest(path=os.path.relpath(path, ROOT)):
                    self.assertIsNone(GOOGLE_FONT_HOSTS.search(_read(path)))


class RenderedPagesUseLocalFontsTests(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        self.webui = webui
        webui.app.config.update(TESTING=True)
        webui.ensure_admin_user()
        self.client = webui.app.test_client()

    def _login(self):
        with self.client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")

    def _assert_local_fonts(self, resp):
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIsNone(GOOGLE_FONT_HOSTS.search(body))
        self.assertEqual(len(FONTS_CSS_LINK.findall(body)), 1)
        csp = resp.headers.get("Content-Security-Policy") or resp.headers.get(
            "Content-Security-Policy-Report-Only", "")
        self.assertIsNone(GOOGLE_FONT_HOSTS.search(csp))
        self.assertIn("font-src 'self' data:;", csp)
        return body

    def test_login_page(self):
        self._assert_local_fonts(self.client.get("/login", headers={"User-Agent": DESKTOP_UA}))

    def test_index_page_preloads_inter(self):
        self._login()
        body = self._assert_local_fonts(self.client.get("/", headers={"User-Agent": DESKTOP_UA}))
        self.assertRegex(
            body,
            # Same URL as url(./inter-latin-wght-normal.woff2) in fonts.css (no ?v=),
            # otherwise the browser fetches the font twice.
            r'<link rel="preload" href="/static/fonts/inter-latin-wght-normal\.woff2" '
            r'as="font" type="font/woff2" crossorigin>',
        )

    def test_mobile_index_page(self):
        self._login()
        body = self._assert_local_fonts(self.client.get("/", headers={"User-Agent": IPHONE_UA}))
        self.assertIn("StreamVault · Mobile", body)

    def test_woff2_is_served_as_font_woff2(self):
        resp = self.client.get("/static/fonts/inter-latin-wght-normal.woff2")
        try:
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.mimetype, "font/woff2")
            self.assertEqual(resp.get_data()[:4], b"wOF2")
        finally:
            resp.close()


if __name__ == "__main__":
    unittest.main()
