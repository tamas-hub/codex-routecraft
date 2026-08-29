from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-routecraft" / "scripts"
LAUNCHER = SCRIPTS / "praxis-dashboard.py"
sys.path.insert(0, str(SCRIPTS))
from praxis_dashboard.server import _static, query_for_directory  # noqa: E402


def request(url: str, method: str = "GET") -> tuple[int, dict]:
    try:
        with urlopen(Request(url, method=method), timeout=4) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


class PraxisDashboardServerTests(unittest.TestCase):
    def test_packaged_static_assets_are_available(self):
        page = _static("/")
        styles = _static("/styles.css")
        script = _static("/app.js")
        self.assertIsNotNone(page); self.assertIsNotNone(styles); self.assertIsNotNone(script)
        assert page is not None and styles is not None and script is not None
        self.assertIn(b"System Status", page[1])
        self.assertIn(b"RouteCraft ON / OFF benchmark basis", page[1])
        self.assertIn(b'id="system" class="metric-grid"', page[1])
        self.assertIn(b"font", styles[1])
        self.assertIn(b"/api/praxis/v1/runs", script[1])
        self.assertIn(b"Requested model", script[1])
        self.assertIn(b"Useful Recall", script[1])
        self.assertIn(b"Sol Offload Rate", script[1])
        self.assertIn(b"Ultra Optimization Rate", script[1])
        self.assertIn(b"Test result", script[1])
        self.assertIn(b"decision_source", script[1])
        self.assertIn(b"aria-label", script[1])

    def test_standalone_server_creates_no_routecraft_artifacts_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            process = subprocess.Popen(
                [sys.executable, "-X", "utf8", str(LAUNCHER), "--data-dir", str(directory), "--port", "0", "--no-browser"],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            try:
                assert process.stdout is not None
                url = process.stdout.readline().strip()
                self.assertRegex(url, r"^http://127\.0\.0\.1:\d+$")
                status, snapshot = request(url + "/api/praxis/v1/snapshot")
                self.assertEqual(200, status); self.assertFalse(snapshot["available"])
                status, sources = request(url + "/api/praxis/v1/sources")
                self.assertEqual(200, status); self.assertEqual([], sources["sources"])
                status, denied = request(url + "/api/praxis/v1/snapshot", "POST")
                self.assertEqual(405, status); self.assertEqual("read_only", denied["error"]["code"])
                self.assertEqual([], list(directory.iterdir()))
            finally:
                process.terminate()
                try: process.wait(timeout=4)
                except subprocess.TimeoutExpired: process.kill()
                if process.stdout: process.stdout.close()
                if process.stderr: process.stderr.close()

    def test_launcher_help_and_wrapper_contracts(self):
        completed = subprocess.run([sys.executable, "-X", "utf8", str(LAUNCHER), "--help"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(0, completed.returncode)
        self.assertIn("--open-browser", completed.stdout)
        ps1 = (LAUNCHER.with_suffix(".ps1")).read_text(encoding="utf-8")
        sh = (LAUNCHER.with_suffix(".sh")).read_text(encoding="utf-8")
        self.assertIn("exit $LASTEXITCODE", ps1)
        self.assertIn("python3, python, or py", ps1)
        self.assertIn("set -eu", sh)
        self.assertIn("exec python3", sh)

    def test_corrupt_and_unknown_sqlite_sources_fail_closed_without_writes(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            database = directory / "praxis-memory.sqlite3"
            database.write_bytes(b"not-a-sqlite-database")
            before = database.read_bytes(); names = {item.name for item in directory.iterdir()}
            corrupt = query_for_directory(directory).snapshot()
            self.assertFalse(corrupt["available"]); self.assertEqual("source_error", corrupt["code"])
            self.assertEqual(before, database.read_bytes()); self.assertEqual(names, {item.name for item in directory.iterdir()})

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            database = directory / "praxis-memory.sqlite3"
            db = sqlite3.connect(database)
            try:
                db.execute("CREATE TABLE events(id TEXT,source TEXT,event_classification TEXT,payload TEXT,created_at TEXT)")
                db.execute("PRAGMA user_version=99")
                db.commit()
            finally:
                db.close()
            before = database.read_bytes(); names = {item.name for item in directory.iterdir()}
            unknown = query_for_directory(directory).snapshot()
            self.assertFalse(unknown["available"]); self.assertEqual("source_error", unknown["code"])
            self.assertEqual(before, database.read_bytes()); self.assertEqual(names, {item.name for item in directory.iterdir()})
