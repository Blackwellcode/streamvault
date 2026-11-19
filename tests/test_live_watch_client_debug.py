"""append_client_debug works on platforms without os.chown (Windows)."""

from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401  (sets the test env)
except ImportError:  # run as tests.test_live_watch_client_debug
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401

from core import live_watch_diag  # noqa: E402


class AppendClientDebugTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.meta = Path(tmp.name)
        self.path = self.meta / "live_watch_client.jsonl"
        for patcher in (
            mock.patch.object(live_watch_diag, "CLIENT_DEBUG_PATH", self.path),
            mock.patch.object(live_watch_diag.config, "META_DIR", str(self.meta)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _lines(self):
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines()]

    def test_without_chown_the_event_is_still_written(self):
        fake_os = types.SimpleNamespace(chmod=mock.Mock())
        self.assertFalse(hasattr(fake_os, "chown"))
        with mock.patch.object(live_watch_diag, "os", fake_os):
            live_watch_diag.append_client_debug({"channel": "demochan", "event": "stall"})
        self.assertEqual(self._lines()[-1]["channel"], "demochan")
        fake_os.chmod.assert_called_once_with(self.path, 0o640)

    def test_with_chown_the_file_gets_the_meta_dir_owner(self):
        st = os.stat(self.meta)
        fake_os = types.SimpleNamespace(chmod=mock.Mock(), chown=mock.Mock())
        with mock.patch.object(live_watch_diag, "os", fake_os):
            live_watch_diag.append_client_debug({"channel": "demochan"})
        fake_os.chown.assert_called_once_with(self.path, st.st_uid, st.st_gid)
        self.assertEqual(len(self._lines()), 1)


if __name__ == "__main__":
    unittest.main()
