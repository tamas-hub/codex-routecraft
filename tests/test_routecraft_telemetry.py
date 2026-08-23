from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_telemetry.py"

SPEC = importlib.util.spec_from_file_location("routecraft_telemetry", SCRIPT)
assert SPEC and SPEC.loader
TELEMETRY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TELEMETRY
SPEC.loader.exec_module(TELEMETRY)


def write_rollout(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def parent_rows(session_id: str) -> list[dict]:
    return [
        {"timestamp": "2026-08-23T00:00:00Z", "type": "session_meta", "payload": {"id": session_id, "source": "vscode"}},
        {"timestamp": "2026-08-23T00:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "high"}},
        {"timestamp": "2026-08-23T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "private prompt must never be exported"}},
    ]


def child_rows(session_id: str, parent_id: str, role: str | None) -> list[dict]:
    return [
        {"timestamp": "2026-08-23T00:01:00Z", "type": "session_meta", "payload": {"id": session_id, "cwd": "C:/secret/project", "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "agent_path": "/root/private_task_name", "agent_role": role}}}}},
        {"timestamp": "2026-08-23T00:01:01Z", "type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "max"}},
        {"timestamp": "2026-08-23T00:01:20Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 800, "cache_write_input_tokens": 10, "output_tokens": 120, "reasoning_output_tokens": 40, "total_tokens": 1120}}}},
    ]


class RouteCraftTelemetryTests(unittest.TestCase):
    def test_maps_human_setting_to_actual_execution_without_private_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            codex_home = root / "codex"
            (codex_home / "routecraft").mkdir(parents=True)
            (codex_home / "routecraft" / "device.json").write_text('{"device_id":"fixture-device"}', encoding="utf-8")
            parent_id = "parent-session-raw"
            child_id = "child-session-raw"
            write_rollout(sessions / "parent.jsonl", parent_rows(parent_id))
            write_rollout(sessions / "child.jsonl", child_rows(child_id, parent_id, "routecraft_luna_max"))

            runs = TELEMETRY.collect_runs(sessions, codex_home, None, include_legacy=False)

            self.assertEqual(len(runs), 1)
            run = runs[0]
            self.assertEqual((run["human_model"], run["human_effort"]), ("gpt-5.6-sol", "high"))
            self.assertEqual((run["actual_model"], run["actual_effort"]), ("gpt-5.6-luna", "max"))
            self.assertEqual(run["route_family"], "routecraft")
            self.assertEqual(run["total_tokens"], 1120)
            self.assertEqual(run["duration_ms"], 20_000)
            rendered = json.dumps(runs)
            for private_value in (parent_id, child_id, "C:/secret/project", "private_task_name", "private prompt"):
                self.assertNotIn(private_value, rendered)
            self.assertRegex(run["run_id"], r"^[a-f0-9]{32}$")
            self.assertRegex(run["parent_run_id"], r"^[a-f0-9]{32}$")

    def test_legacy_runs_are_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            codex_home = root / "codex"
            write_rollout(sessions / "parent.jsonl", parent_rows("parent"))
            write_rollout(sessions / "child.jsonl", child_rows("child", "parent", "luna_max"))

            self.assertEqual(TELEMETRY.collect_runs(sessions, codex_home, None, include_legacy=False), [])
            runs = TELEMETRY.collect_runs(sessions, codex_home, None, include_legacy=True)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["route_family"], "legacy")

    def test_roleless_child_is_included_only_for_routecraft_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            codex_home = root / "codex"
            parent = parent_rows("parent")
            parent.append({"timestamp": "2026-08-23T00:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "ROUTECRAFT PLAN\nexecution: delegate"}})
            write_rollout(sessions / "parent.jsonl", parent)
            write_rollout(sessions / "child.jsonl", child_rows("child", "parent", None))

            runs = TELEMETRY.collect_runs(sessions, codex_home, None, include_legacy=False)

            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["route_family"], "routecraft")
            self.assertEqual(runs[0]["role"], "subagent")
            self.assertNotIn("ROUTECRAFT PLAN", json.dumps(runs))

    def test_exports_memory_fields_only_from_a_valid_explicit_parent_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            codex_home = root / "codex"
            parent = parent_rows("parent")
            parent.append({
                "timestamp": "2026-08-23T00:02:03Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": """ROUTECRAFT PLAN
execution: delegate
ROUTECRAFT MEMORY
task_class: implementation
task_summary: Add bounded memory loop metrics
memory_mode: full
memory_recall_count: 2
memory_useful_count: 1
memory_learn_status: skipped
memory_skip_reason: no_reusable_learning
END ROUTECRAFT MEMORY"""},
            })
            parent.append({
                "timestamp": "2026-08-23T00:00:04Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": "ROUTECRAFT MEMORY\ntask_summary: C:/secret/project"},
            })
            write_rollout(sessions / "parent.jsonl", parent)
            write_rollout(sessions / "child.jsonl", child_rows("child", "parent", None))

            run = TELEMETRY.collect_runs(sessions, codex_home, None, include_legacy=False)[0]

            self.assertEqual(run["task_class"], "implementation")
            self.assertEqual(run["task_summary"], "Add bounded memory loop metrics")
            self.assertEqual(run["memory_mode"], "full")
            self.assertEqual(run["memory_recall_count"], 2)
            self.assertEqual(run["memory_useful_count"], 1)
            self.assertEqual(run["memory_learn_status"], "skipped")
            self.assertEqual(run["memory_skip_reason"], "no_reusable_learning")
            self.assertNotIn("C:/secret/project", json.dumps(run))

    def test_missing_or_unsafe_marker_fields_remain_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            codex_home = root / "codex"
            parent = parent_rows("parent")
            parent.append({
                "timestamp": "2026-08-23T00:00:03Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": """ROUTECRAFT PLAN
execution: delegate
ROUTECRAFT MEMORY
task_class: implementation
task_summary: token: private-value
memory_mode: full
memory_recall_count: 1
memory_useful_count: 0
memory_learn_status: skipped
memory_skip_reason: no_reusable_learning
END ROUTECRAFT MEMORY"""},
            })
            write_rollout(sessions / "parent.jsonl", parent)
            write_rollout(sessions / "child.jsonl", child_rows("child", "parent", None))

            run = TELEMETRY.collect_runs(sessions, codex_home, None, include_legacy=False)[0]

            for field in (
                "task_class", "task_summary", "memory_mode", "memory_recall_count", "memory_useful_count",
                "memory_learn_status", "memory_skip_reason",
            ):
                self.assertIsNone(run[field])

    def test_marker_scan_reaches_late_explicit_marker_and_summary_validator_is_minimal(self) -> None:
        self.assertEqual(
            TELEMETRY.safe_task_summary("検証 ABC＋+・、。！？!?（）()【】「」：:-"),
            "検証 ABC＋+・、。！？!?（）()【】「」：:-",
        )
        for unsafe in ("https://example.test", "name@example.test", "C:/secret", "key: private", "it's-private", "a&b"):
            self.assertIsNone(TELEMETRY.safe_task_summary(unsafe))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            codex_home = root / "codex"
            parent = parent_rows("parent")
            parent.append({"timestamp": "2026-08-23T00:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "ROUTECRAFT PLAN\nexecution: delegate"}})
            parent.extend(
                {"timestamp": "2026-08-23T00:00:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "ordinary assistant update"}}
                for _ in range(801)
            )
            parent.append({
                "timestamp": "2026-08-23T00:02:05Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": """ROUTECRAFT MEMORY
task_class: test
task_summary: Late marker validation
memory_mode: recall
memory_recall_count: 0
memory_useful_count: 0
memory_learn_status: skipped
memory_skip_reason: mode_recall_only
END ROUTECRAFT MEMORY"""},
            })
            write_rollout(sessions / "parent.jsonl", parent)
            write_rollout(sessions / "child.jsonl", child_rows("child", "parent", None))

            run = TELEMETRY.collect_runs(sessions, codex_home, None, include_legacy=False)[0]
            self.assertEqual(run["task_summary"], "Late marker validation")
            self.assertEqual(run["memory_skip_reason"], "mode_recall_only")
            tasks = TELEMETRY.collect_memory_tasks(sessions, codex_home, None)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["task_summary"], "Late marker validation")
            self.assertNotIn('"parent"', json.dumps(tasks))

    def test_multiple_parent_tasks_are_time_matched_and_solo_tasks_are_preserved(self) -> None:
        def marker(summary: str, mode: str, reason: str) -> str:
            return f"""ROUTECRAFT MEMORY
task_class: integration
task_summary: {summary}
memory_mode: {mode}
memory_recall_count: 0
memory_useful_count: 0
memory_learn_status: skipped
memory_skip_reason: {reason}
END ROUTECRAFT MEMORY"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            codex_home = root / "codex"
            parent = parent_rows("parent")
            parent.append({"timestamp": "2026-08-23T00:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "ROUTECRAFT PLAN\nexecution: delegate"}})
            parent.append({"timestamp": "2026-08-23T00:02:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": marker("First task", "recall", "mode_recall_only")}})
            parent.append({"timestamp": "2026-08-23T00:04:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": marker("Second task", "full", "no_reusable_learning")}})
            first = child_rows("child-1", "parent", "routecraft_luna_medium")
            second = child_rows("child-2", "parent", "routecraft_terra_medium")
            second[0]["timestamp"] = "2026-08-23T00:03:00Z"
            second[1]["timestamp"] = "2026-08-23T00:03:01Z"
            second[2]["timestamp"] = "2026-08-23T00:03:20Z"
            write_rollout(sessions / "parent.jsonl", parent)
            write_rollout(sessions / "child-1.jsonl", first)
            write_rollout(sessions / "child-2.jsonl", second)

            runs = TELEMETRY.collect_runs(sessions, codex_home, None, include_legacy=False)
            by_role = {run["role"]: run for run in runs}
            self.assertEqual(by_role["routecraft_luna_medium"]["task_summary"], "First task")
            self.assertEqual(by_role["routecraft_terra_medium"]["task_summary"], "Second task")
            tasks = TELEMETRY.collect_memory_tasks(sessions, codex_home, None)
            self.assertEqual([task["task_summary"] for task in tasks], ["Second task", "First task"])
            self.assertEqual(len({task["task_run_id"] for task in tasks}), 2)

    def test_non_https_remote_endpoint_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TELEMETRY.send("http://example.invalid/api", "x" * 32, [])

    def test_sites_bypass_token_is_sent_in_separate_header(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true,"accepted":0}'
        with mock.patch.object(TELEMETRY.urllib.request, "urlopen", return_value=response) as urlopen:
            accepted = TELEMETRY.send(
                "https://example.invalid/api/ingest",
                "a" * 32,
                [{"run_id": "fixture"}],
                sites_bypass_token="b" * 32,
            )
        self.assertEqual(accepted, 0)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer " + "a" * 32)
        self.assertEqual(request.get_header("Oai-sites-authorization"), "Bearer " + "b" * 32)
        self.assertEqual(json.loads(request.data.decode("utf-8"))["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
