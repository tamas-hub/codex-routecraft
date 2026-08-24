from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_source_guard.py"

SPEC = importlib.util.spec_from_file_location("routecraft_source_guard", SCRIPT)
assert SPEC and SPEC.loader
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)

from routecraft_local import loop_bridge as LOCAL_BRIDGE
from routecraft_local.service import RouteCraftService


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

    def test_memory_loop_context_and_unfinished_stop_guard(self) -> None:
        evaluation = GUARD.evaluation_dir()
        evaluation.mkdir(parents=True)
        (evaluation / "config.json").write_text(json.dumps({"enabled": True, "mode": "full"}), encoding="utf-8")
        state = GUARD.evaluation_session_path("session-1")
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"schema_version": 1, "task_id": "EVAL-TEST"}), encoding="utf-8")

        context = GUARD.evaluate(self.event("SessionStart"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ROUTECRAFT MEMORY LOOP", context)
        self.assertIn("EVAL-TEST", context)
        self.assertIn("task_cancelled", context)
        blocked = GUARD.evaluate(self.event("Stop"))
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("EVAL-TEST", blocked["reason"])
        self.assertEqual(GUARD.evaluate(self.event("Stop", active=True)), {})

        (evaluation / "events.jsonl").write_text(
            json.dumps({"event": "task_finish", "task_id": "EVAL-TEST"}) + "\n", encoding="utf-8"
        )
        self.assertEqual(GUARD.evaluate(self.event("Stop")), {})
        self.assertFalse(state.exists())

    def test_local_memory_context_and_idempotent_git_summary(self) -> None:
        source_config = self.codex_home / "routecraft" / "source-control.json"
        source_config.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        data_dir = self.base / "local-memory-data"
        local_config = LOCAL_BRIDGE.config_path()
        local_config.parent.mkdir(parents=True, exist_ok=True)
        local_config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "enabled": True,
                    "data_dir": str(data_dir),
                    "auto_context": True,
                    "auto_session_summary": True,
                    "context_profile": "compact",
                    "max_context_chars": 4000,
                }
            ),
            encoding="utf-8",
        )
        service = RouteCraftService(data_dir)
        project = service.add_project("Loop project", repo_path=str(self.repo), current_objective="continue safely")
        service.add_memory(project["id"], "decision", "Keep the existing interface", "Do not break the CLI", importance="high", verified=True)

        started = GUARD.evaluate(self.event("SessionStart"))
        context = started["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ROUTECRAFT MEMORY LOCAL CONTEXT", context)
        self.assertIn("Keep the existing interface", context)

        (self.repo / "README.md").write_text("local memory change\n", encoding="utf-8")
        stopped = GUARD.evaluate(self.event("Stop"))
        self.assertIn("saved Git session summary", stopped["systemMessage"])
        summaries = service.list_memories(project["id"], memory_type="session_summary")
        self.assertEqual(1, len(summaries))
        self.assertEqual("routecraft-loop", summaries[0]["source"])
        self.assertNotIn("session-1", summaries[0]["source_ref"])

        GUARD.evaluate(self.event("Stop"))
        self.assertEqual(1, len(service.list_memories(project["id"], memory_type="session_summary")))

    def test_local_memory_summary_finalizes_after_blocked_stop_reentry(self) -> None:
        data_dir = self.base / "reentry-local-memory-data"
        local_config = LOCAL_BRIDGE.config_path()
        local_config.parent.mkdir(parents=True, exist_ok=True)
        local_config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "enabled": True,
                    "data_dir": str(data_dir),
                    "auto_context": False,
                    "auto_session_summary": True,
                    "context_profile": "compact",
                    "max_context_chars": 4000,
                }
            ),
            encoding="utf-8",
        )
        service = RouteCraftService(data_dir)
        project = service.add_project("Reentry project", repo_path=str(self.repo))

        GUARD.evaluate(self.event("SessionStart"))
        state = LOCAL_BRIDGE._state_path("session-1")
        self.assertTrue(state.is_file())
        (self.repo / "README.md").write_text("blocked local memory change\n", encoding="utf-8")

        blocked = GUARD.evaluate(self.event("Stop"))
        self.assertEqual("block", blocked["decision"])
        self.assertEqual([], service.list_memories(project["id"], memory_type="session_summary"))
        self.assertTrue(state.is_file())

        finalized = GUARD.evaluate(self.event("Stop", active=True))
        self.assertIn("saved Git session summary", finalized["systemMessage"])
        self.assertEqual(1, len(service.list_memories(project["id"], memory_type="session_summary")))
        self.assertFalse(state.exists())

    def test_local_memory_rejects_decision_store_as_data_directory(self) -> None:
        decision_store = self.base / "decision-store"
        decision_store.mkdir()
        (decision_store / ".routecraft-store.json").write_text('{"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(Exception, "must not reuse"):
            RouteCraftService(decision_store)

    def test_local_memory_does_not_open_database_during_round_robin_experiment(self) -> None:
        source_config = self.codex_home / "routecraft" / "source-control.json"
        source_config.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        data_dir = self.base / "must-not-be-created"
        local_config = LOCAL_BRIDGE.config_path()
        local_config.parent.mkdir(parents=True, exist_ok=True)
        local_config.write_text(
            json.dumps({"schema_version": 1, "enabled": True, "data_dir": str(data_dir)}),
            encoding="utf-8",
        )
        evaluation = GUARD.evaluation_dir()
        evaluation.mkdir(parents=True)
        (evaluation / "config.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "enabled": True,
                    "mode": "full",
                    "experiment": {"enabled": True, "strategy": "round-robin", "sequence": ["off", "recall", "full"]},
                }
            ),
            encoding="utf-8",
        )
        result = GUARD.evaluate(self.event("SessionStart"))
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("ROUTECRAFT MEMORY LOCAL CONTEXT", context)
        self.assertFalse(data_dir.exists())

    def test_local_memory_disabled_does_not_create_database_or_change_legacy_context(self) -> None:
        baseline = GUARD.evaluate(self.event("SessionStart"))
        data_dir = self.base / "disabled-local-memory"
        local_config = LOCAL_BRIDGE.config_path()
        local_config.parent.mkdir(parents=True, exist_ok=True)
        local_config.write_text(
            json.dumps({"schema_version": 1, "enabled": False, "data_dir": str(data_dir)}),
            encoding="utf-8",
        )
        actual = GUARD.evaluate(self.event("SessionStart"))
        self.assertEqual(baseline, actual)
        self.assertFalse(data_dir.exists())

    def test_missing_local_package_preserves_existing_hook(self) -> None:
        expected = GUARD.evaluate(self.event("SessionStart"))
        original = GUARD.importlib.import_module

        def missing(name: str):
            if name == "routecraft_local.loop_bridge":
                raise ModuleNotFoundError(name)
            return original(name)

        with mock.patch.object(GUARD.importlib, "import_module", side_effect=missing):
            actual = GUARD.evaluate(self.event("SessionStart"))
        self.assertEqual(expected, actual)

    def test_merged_context_respects_hook_budget(self) -> None:
        first = {"hookSpecificOutput": {"additionalContext": "A" * 3500}}
        second = {"hookSpecificOutput": {"additionalContext": "B" * 3500}}
        result = GUARD.merge_results("SessionStart", first, second)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(context), GUARD.MAX_SESSION_CONTEXT_CHARS)
        self.assertIn("truncated", context)

    def test_hook_process_uses_utf8_when_windows_code_page_is_not_utf8(self) -> None:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        env.pop("PYTHONUTF8", None)
        env.pop("PYTHONIOENCODING", None)
        payload = json.dumps(self.event("SessionStart"), ensure_ascii=False).encode("utf-8")
        process = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=15,
            check=False,
        )
        self.assertEqual(0, process.returncode, process.stderr.decode("utf-8", errors="replace"))
        result = json.loads(process.stdout.decode("utf-8"))
        self.assertIn("GITHUB SOURCE-OF-TRUTH POLICY", result["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
