"""Deployment script guards: never cut active recordings, never leak data into the code tree."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name == "posix" else None


def _cli_functions():
    """bin/streamvault without its dispatch block, so the helpers can be sourced."""
    text = (ROOT / "bin" / "streamvault").read_text(encoding="utf-8")
    return text.split("# Subcommand dispatch", 1)[0]


@unittest.skipUnless(BASH, "needs bash on a POSIX host")
class CliRecordingGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sv-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name) / "data"
        (self.base / "chan").mkdir(parents=True)
        self.env_file = Path(self._tmp.name) / "env"
        self.env_file.write_text(f"VOD_BASE_DIR={self.base}\n", encoding="utf-8")

    def _guard(self, force):
        script = _cli_functions() + f"\nguard_recordings {force}\necho passed\n"
        env = dict(os.environ, STREAMVAULT_ENV=str(self.env_file), STREAMVAULT_DIR=str(self.base.parent))
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, timeout=30)

    def test_fresh_part_file_blocks_restart(self):
        (self.base / "chan" / "rec.mp4.part").write_bytes(b"x")
        result = self._guard(0)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("rec.mp4.part", result.stdout)
        self.assertNotIn("passed", result.stdout)

    def test_force_overrides_the_guard(self):
        (self.base / "chan" / "rec.mp4.part").write_bytes(b"x")
        result = self._guard(1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("passed", result.stdout)

    def test_stale_part_file_does_not_block(self):
        part = self.base / "chan" / "old.mp4.part"
        part.write_bytes(b"x")
        old = time.time() - 600
        os.utime(part, (old, old))
        result = self._guard(0)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads every directory")
    def test_unreadable_archive_fails_closed(self):
        locked = self.base / "locked"
        locked.mkdir()
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        result = self._guard(0)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("passed", result.stdout)
        self.assertIn("Could not check for active recordings", result.stdout)
        forced = self._guard(1)
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertIn("passed", forced.stdout)

    def test_has_force_detects_flag(self):
        script = _cli_functions() + '\nhas_force restart --force\nhas_force restart webui\n'
        result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.stdout.split(), ["1", "0"])


class InstallerStaticTests(unittest.TestCase):
    """Text-level checks for install.sh; the script itself only runs on a server as root."""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "install.sh").read_text(encoding="utf-8")

    def test_nginx_test_failure_aborts(self):
        self.assertNotIn("|| true\nfi", self.text.split("nginx -t", 1)[1][:200])
        self.assertIn("if ! nginx -t; then", self.text)

    def test_copy_excludes_runtime_data(self):
        # One list for install.sh and streamvault_vps.sh (scripts/deploy-excludes.txt).
        excludes = (ROOT / "scripts" / "deploy-excludes.txt").read_text(encoding="utf-8").split()
        for pattern in ("*.db", "/demo-data/", "/files/", "/data/", "/.env"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, excludes)
        self.assertIn('rsync -a --exclude-from="${EXCLUDES_FILE}"', self.text)
        vps = (ROOT / "scripts" / "streamvault_vps.sh").read_text(encoding="utf-8")
        self.assertIn('CODE_EXCLUDES=(--exclude-from="$EXCLUDES_FILE")', vps)
        self.assertNotIn('cp -ru "${SCRIPT_DIR}"/*', self.text)

    @unittest.skipUnless(shutil.which("rsync") and os.name == "posix", "needs rsync")
    def test_exclude_list_keeps_runtime_data_out_of_a_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "src", Path(tmp) / "dst"
            for rel in ("WEBUI/webui.py", "core/config.py", "scripts/demo_seed.py", "data/streamrecorder.db",
                        "demo-data/x/vod.mp4", "files/up.pdf", ".env", "local.db", "core/x.db-wal",
                        "chan/rec.mp4.part", ".venv/bin/python", "core/__pycache__/c.pyc"):
                path = src / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            subprocess.run(["rsync", "-a", f"--exclude-from={ROOT / 'scripts' / 'deploy-excludes.txt'}",
                            f"{src}/", f"{dst}/"], check=True, timeout=60)
            copied = sorted(str(p.relative_to(dst)).replace(os.sep, "/") for p in dst.rglob("*") if p.is_file())
        self.assertEqual(copied, ["WEBUI/webui.py", "core/config.py", "scripts/demo_seed.py"])

    def test_git_runs_as_service_user(self):
        import re
        root_git = re.compile(r"^(cd \S+ && )?git (-C \S+ )?(pull|clone|fetch|checkout)\b")
        for line in self.text.splitlines():
            stripped = line.strip()
            if root_git.match(stripped):
                self.fail(f"git as root: {stripped}")
        self.assertIn('runuser -u "${SERVICE_USER}" -- git -C "${APP_DIR}" pull --ff-only', self.text)

    def test_initial_password_is_not_printed(self):
        self.assertNotIn("Password: ${INITIAL_PASS}", self.text)

    @unittest.skipUnless(BASH, "needs bash on a POSIX host")
    def test_syntax(self):
        for script in ("install.sh", "bin/streamvault", "scripts/streamvault_vps.sh"):
            with self.subTest(script=script):
                result = subprocess.run([BASH, "-n", str(ROOT / script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


class ContainerConfigTests(unittest.TestCase):
    def test_supervisor_programs_do_not_run_as_root(self):
        import configparser
        parser = configparser.RawConfigParser(strict=False)
        parser.read(ROOT / "docker-worker.conf", encoding="utf-8")
        programs = [s for s in parser.sections() if s.startswith("program:")]
        self.assertTrue(programs)
        for section in programs:
            with self.subTest(program=section):
                user = parser.get(section, "user", fallback="root").strip()
                self.assertNotEqual(user, "root")


class NginxExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "nginx.conf").read_text(encoding="utf-8")

    def _locations(self):
        blocks, current, depth = {}, None, 0
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("location ") and stripped.endswith("{"):
                current, depth = stripped.split()[1], 0
                blocks[current] = []
            if current:
                blocks[current].append(stripped)
                depth += stripped.count("{") - stripped.count("}")
                if depth == 0 and stripped.endswith("}"):
                    current = None
        return blocks

    def test_locations_with_add_header_repeat_security_headers(self):
        for name, lines in self._locations().items():
            if any(line.startswith("add_header") for line in lines):
                with self.subTest(location=name):
                    body = "\n".join(lines)
                    self.assertIn("X-Content-Type-Options", body)
                    self.assertIn("X-Frame-Options", body)

    def test_connection_header_uses_map(self):
        self.assertIn("map $http_upgrade", self.text)
        self.assertNotIn('proxy_set_header Connection "upgrade";', self.text)

    def test_protected_thumbnails_are_private(self):
        thumbs = "\n".join(self._locations()["/_protected_thumbs/"])
        self.assertIn('Cache-Control "private', thumbs)
        self.assertNotIn('"public"', thumbs)

    def test_proxied_css_and_js_are_compressed(self):
        self.assertIn("gzip_proxied any;", self.text)
        types_line = next(line for line in self.text.splitlines() if line.strip().startswith("gzip_types"))
        self.assertIn("text/css", types_line)
        self.assertIn("application/javascript", types_line)


if __name__ == "__main__":
    unittest.main()
