"""The recorder starts on an empty data directory, before the WebUI created the database."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix", "the recorder needs fcntl")
class FreshStartTests(unittest.TestCase):
    def test_recorder_survives_a_missing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            env = dict(
                os.environ, PYTHONPATH=str(ROOT), VOD_BASE_DIR=str(base), RECORDINGS_BASE_DIR=str(base),
                STREAMRECORDER_LOG_DIR=str(Path(tmp) / "logs"), LIVE_HLS_DIR=str(Path(tmp) / "hls"),
                VOD_USER="demo", VOD_PASS="fresh-start-pass", VOD_SECRET_KEY="0123456789abcdef" * 4,
            )
            proc = subprocess.Popen(
                [sys.executable, "recorder/Streamrecorder.py"], cwd=ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                time.sleep(8)
                alive = proc.poll() is None
            finally:
                proc.terminate()
                try:
                    out, _ = proc.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, _ = proc.communicate(timeout=10)
            self.assertTrue(alive, out)
            self.assertNotIn("no such table", out)
            self.assertTrue((base / "streamrecorder.db").is_file())


if __name__ == "__main__":
    unittest.main()
