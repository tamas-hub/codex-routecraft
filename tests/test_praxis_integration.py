from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-routecraft" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from praxis_dashboard import PraxisDashboardQuery  # noqa: E402
from praxis_memory import PraxisMemory  # noqa: E402
from routecraft_core import RouteCraftCore, RoutingRequest  # noqa: E402


class RecordingHost:
    def __init__(self) -> None:
        self.calls = 0

    def dispatch(self, request, decision, executor=None):
        self.calls += 1
        return {"succeeded": True, "status": "succeeded", "private_output": "not exported"}


class PraxisIntegrationTests(unittest.TestCase):
    def test_task_core_memory_host_events_dashboard_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory = PraxisMemory(directory)
            memory.remember({
                "task": "Verify local migration",
                "strategy": "validate before apply",
                "result": "passed",
                "project": "sample",
                "tags": ["migration"],
                "success_rate": 1.0,
                "reliability": 0.9,
            })
            host = RecordingHost()
            core = RouteCraftCore(memory=memory, events=memory, host=host)
            result = core.execute(RoutingRequest(
                task="Verify local migration safely",
                task_id="task_integration_1",
                project="sample",
                mode="routecraft",
            ), executor=object())

            self.assertTrue(result.succeeded)
            self.assertEqual(1, host.calls)
            self.assertEqual(1, result.evidence["memory_recalled_count"])
            self.assertNotIn("private_output", json.dumps(result.to_dict()))

            snapshot = PraxisDashboardQuery(memory).snapshot()
            self.assertTrue(snapshot["available"])
            self.assertEqual(2, snapshot["data"]["events"]["total"])
            self.assertEqual(1, snapshot["data"]["runtime"]["completed"])
            self.assertEqual(0, snapshot["data"]["runtime"]["running"])
            rendered = json.dumps(snapshot, ensure_ascii=False)
            self.assertNotIn("Verify local migration safely", rendered)
            self.assertNotIn("metadata", snapshot["data"]["events"]["recent"][0])

    def test_non_dispatch_and_unavailable_host_are_terminal_dashboard_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory = PraxisMemory(directory)
            advisory = RouteCraftCore(memory=memory, events=memory).execute(RoutingRequest(
                task="Advisory only", task_id="task_advisory", mode="advisory",
            ))
            self.assertEqual("not_dispatched", advisory.status)
            unavailable = RouteCraftCore(memory=memory, events=memory).execute(RoutingRequest(
                task="Host unavailable", task_id="task_unavailable", mode="routecraft",
            ))
            self.assertEqual("host_adapter_unavailable", unavailable.status)
            snapshot = PraxisDashboardQuery(memory).snapshot()["data"]
            self.assertEqual(0, snapshot["runtime"]["running"])
            self.assertEqual(1, snapshot["runtime"]["unknown"])
            self.assertEqual(1, snapshot["runtime"]["failed"])
            self.assertGreaterEqual(snapshot["experience"]["failure"], 1)

    def test_component_packages_keep_dependency_direction(self) -> None:
        core_text = "\n".join(path.read_text(encoding="utf-8") for path in (SCRIPTS / "routecraft_core").glob("*.py"))
        memory_text = "\n".join(path.read_text(encoding="utf-8") for path in (SCRIPTS / "praxis_memory").glob("*.py"))
        dashboard_text = "\n".join(path.read_text(encoding="utf-8") for path in (SCRIPTS / "praxis_dashboard").glob("*.py"))
        self.assertNotIn("praxis_memory", core_text)
        self.assertNotIn("praxis_dashboard", core_text)
        self.assertNotIn("routecraft_core", memory_text)
        self.assertNotIn("routecraft_core", dashboard_text)
        self.assertNotIn("praxis_memory", dashboard_text)

    def test_existing_cli_exposes_additive_non_dispatching_routing_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "must-not-be-created"
            registry = Path(directory) / "registry.json"
            registry.write_text(json.dumps({"schema_version": "1", "providers": []}), encoding="utf-8")
            completed = subprocess.run([
                sys.executable, "-B", "-X", "utf8", str(SCRIPTS / "routecraft.py"),
                "--data-dir", str(data_dir),
                "--json", "routing", "plan", "--task", "互換ルート計画", "--mode", "advisory",
                "--registry", str(registry),
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=20)
            self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", "replace"))
            payload = json.loads(completed.stdout.decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual("advisory", payload["data"]["mode"])
            self.assertFalse(payload["data"]["dispatch"])
            self.assertEqual("host", payload["data"]["authority"])
            self.assertFalse(data_dir.exists())


if __name__ == "__main__":
    unittest.main()
