"""Module boundaries: shared channel module, Flask-free core, no import cycles, no dead code."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

try:
    from test_recorder_reliability import IsolatedBaseTest
except ImportError:  # run as tests.test_architecture
    from tests.test_recorder_reliability import IsolatedBaseTest

from core import channels, database  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


class ChannelModuleTests(IsolatedBaseTest):
    def test_youtube_hosts(self):
        for host in ("youtube.com", "WWW.YouTube.com", "m.youtube.com", "youtu.be"):
            with self.subTest(host=host):
                self.assertTrue(channels.is_youtube_host(host))
        for host in ("", None, "youtube.com.evil.example", "notyoutube.com", "twitch.tv"):
            with self.subTest(host=host):
                self.assertFalse(channels.is_youtube_host(host))

    def test_loaders_follow_channel_flags(self):
        conn = database.get_db()
        try:
            conn.executemany(
                "INSERT INTO channels (name, record, live) VALUES (?, ?, ?)",
                [("rec_only", 1, 0), ("live_only", 0, 1), ("both", 1, 1), ("idle", 0, 0)],
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(sorted(channels.load_channels()), ["both", "rec_only"])
        self.assertEqual(sorted(channels.load_live_channels()), ["both", "live_only"])
        self.assertEqual(sorted(channels.load_all_channels()), ["both", "idle", "live_only", "rec_only"])

    def test_web_and_recorder_share_one_implementation(self):
        from recorder import Streamrecorder as recorder
        from WEBUI import webui
        for name in ("is_youtube_host", "load_channels", "load_live_channels"):
            with self.subTest(module="webui", name=name):
                self.assertIs(getattr(webui, name), getattr(channels, name))
        for name in ("is_youtube_host", "load_all_channels"):
            with self.subTest(module="recorder", name=name):
                self.assertIs(getattr(recorder, name), getattr(channels, name))


class LayeringTests(unittest.TestCase):
    def test_core_database_imports_without_flask(self):
        code = (
            "import sys; sys.modules['flask'] = None; sys.modules['flask_babel'] = None\n"
            "import core.database, core.channels\n"
            "print('ok')\n"
        )
        env = dict(os.environ, VOD_USER="layeruser", VOD_PASS="Layer-Test-Pass-1")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                                text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_no_tube_rules_tube_terms_cycle(self):
        rules = _imports(ROOT / "core" / "tube_rules.py")
        self.assertFalse({"core.tube_terms", "tube_terms"} & rules)
        self.assertIn("core.tube_rules", _imports(ROOT / "core" / "tube_terms.py"))

    def test_no_statements_after_unconditional_return(self):
        terminal = (ast.Return, ast.Raise, ast.Continue, ast.Break)
        offenders = []
        for rel in ("WEBUI/webui.py", "recorder/Streamrecorder.py", "recorder/worker.py", "core/database.py"):
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                for field in ("body", "orelse", "finalbody"):
                    block = getattr(node, field, None)
                    if not isinstance(block, list):
                        continue
                    for index, stmt in enumerate(block[:-1]):
                        if isinstance(stmt, terminal):
                            offenders.append(f"{rel}:{block[index + 1].lineno}")
                            break
        self.assertEqual(offenders, [])


class GroupErrorTranslationTests(IsolatedBaseTest):
    def test_error_keeps_english_text_and_msgid(self):
        with self.assertRaises(database.GroupError) as ctx:
            database.create_user_group("Viewer", "", {})
        self.assertEqual(ctx.exception.msgid, "A group named '%(name)s' already exists.")
        self.assertEqual(str(ctx.exception), "A group named 'Viewer' already exists.")
        self.assertIsInstance(ctx.exception, ValueError)

    def test_web_layer_translates_group_errors(self):
        from WEBUI import webui
        err = database.GroupError("Group not found.")
        with webui.app.test_request_context("/", headers={"Accept-Language": "de"}):
            text = webui.translated_error(err)
        self.assertNotEqual(text, "")
        self.assertEqual(webui.translated_error(ValueError("plain")), "plain")


if __name__ == "__main__":
    unittest.main()
