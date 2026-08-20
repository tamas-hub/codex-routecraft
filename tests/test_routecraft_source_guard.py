from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_source_guard.py"

SPEC = importlib.util.spec_from_file_location("routecraft_source_guard", SCRIPT)
assert SPEC and SPEC.loader
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)


class RouteCraftSourceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.codex_home = self.base / "codex-home"
        self.previous_codex_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.codex_home)
        config = self.codex_home / "routecraft" / "source-control.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "enabled": True,
                    "provider": "github",
                    "github_owner": "example-owner",
                    "default_visibility": "private",
                    "auto_commit": True,
                    "auto_push": True,
                    "allow_force_push": False,
                    "store_raw_transcripts": False,
                    "store_device_config": False,
                }
            ),
            encoding="utf-8",
        )
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "RouteCraft Test")
        self.git("config", "user.email", "routecraft-test@users.noreply.github.com")
        (self.repo / "README.md").write_text("baseline\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "baseline")

    def tearDown(self) -> None:
        if self.previous_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.previous_codex_home
        self.temp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def event(self, name: str, *, active: bool = False) -> dict:
        return {
            "hook_event_name": name,
            "session_id": "session-1",
            "cwd": str(self.repo),
            "stop_hook_active": active,
        }

    def test_start_records_local_baseline_and_injects_safe_policy(self) -> None:
        result = GUARD.evaluate(self.event("SessionStart"))
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("PRIVATE", context)
        self.assertIn("raw Codex transcripts", context)
        self.assertTrue(GUARD.state_path("session-1").is_file())

    def test_unchanged_preexisting_dirty_tree_does_not_block(self) -> None:
        (self.repo / "README.md").write_text("preexisting\n", encoding="utf-8")
        GUARD.evaluate(self.event("SessionStart"))
        self.assertEqual(GUARD.evaluate(self.event("Stop")), {})

    def test_new_dirty_change_blocks_stop(self) -> None:
        GUARD.evaluate(self.event("SessionStart"))
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        result = GUARD.evaluate(self.event("Stop"))
        self.assertEqual(result["decision"], "block")
        self.assertIn("commit", result["reason"])

    def test_committed_change_without_remote_requests_private_github(self) -> None:
        GUARD.evaluate(self.event("SessionStart"))
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "change")
        result = GUARD.evaluate(self.event("Stop"))
        self.assertEqual(result["decision"], "block")
        self.assertIn("Private GitHub", result["reason"])

    def test_pushed_state_passes_and_active_stop_never_loops(self) -> None:
        self.git("remote", "add", "origin", "https://github.com/example-owner/repo.git")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("branch", "--set-upstream-to", "origin/main", "main")
        GUARD.evaluate(self.event("SessionStart"))
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "change")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertEqual(GUARD.evaluate(self.event("Stop")), {})

        (self.repo / "README.md").write_text("again\n", encoding="utf-8")
        self.assertEqual(GUARD.evaluate(self.event("Stop", active=True)), {})


if __name__ == "__main__":
    unittest.main()
