from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-routecraft" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import routecraft_collector as collector
import routecraft_telemetry as telemetry
from routecraft_core import RouteCraftCore, RoutingRequest, select_verification_plan
from praxis_dashboard.projection import build_snapshot


class VerificationBudgetTests(unittest.TestCase):
    def test_auto_min_none_strict_and_explicit_release(self) -> None:
        minimum = select_verification_plan(RoutingRequest(task="work", config={"task_class": "implementation", "risk_level": "low", "change_scope": "module"}))
        self.assertEqual("min", minimum.budget.value)
        self.assertFalse(minimum.full_suite_allowed)
        none = select_verification_plan(RoutingRequest(task="docs", config={"task_class": "docs", "change_scope": "single_file"}))
        self.assertEqual("none", none.budget.value)
        strict = select_verification_plan(RoutingRequest(task="work", config={"risk_level": "high", "change_scope": "module"}))
        self.assertEqual("strict", strict.budget.value)
        self.assertNotEqual("release", strict.budget.value)
        release = select_verification_plan(RoutingRequest(task="release", config={"test_budget": "release", "full_suite_reason": "release_gate"}))
        self.assertEqual("release", release.budget.value)
        self.assertTrue(release.full_suite_allowed)

    def test_core_keeps_unknown_outcome_and_memory_receives_budget(self) -> None:
        class Memory:
            def __init__(self): self.rows = []
            def recall(self, request): return []
            def notify_outcome(self, row): self.rows.append(row)
            def notify_experience(self, row): self.rows.append(row)
        class Host:
            def dispatch(self, request, decision, executor=None): return {"succeeded": True, "status": "succeeded"}
        memory = Memory()
        result = RouteCraftCore(host=Host(), memory=memory).execute(RoutingRequest(task="work", mode="routecraft"))
        self.assertEqual("min", result.decision.verification["budget"])
        self.assertEqual("unknown", result.evidence["verification"]["outcome"]["status"])
        self.assertEqual("min", memory.rows[0]["verification"]["plan"]["budget"])

    def test_marker_and_schema_v5_are_privacy_bounded(self) -> None:
        marker = """ROUTECRAFT VERIFICATION
task_class: implementation
task_summary: Verification budget integration
setting: auto_min
budget: min
status: pass
reason: targeted_checks_passed
tests_run: 3
targeted_tests: 3
full_suites: 0
builds: 0
lint_runs: 0
typechecks: 0
e2e_runs: 0
avoided_full_suites: 1
avoided_e2e: 1
avoided_builds: 1
avoided_lint: 1
avoided_typechecks: 1
verification_duration_ms: 1200
event_classification: normal
END ROUTECRAFT VERIFICATION"""
        parsed = telemetry.parse_verification_marker(marker)
        self.assertIsNotNone(parsed)
        self.assertEqual(5, sum(parsed[key] for key in ("avoided_full_suites", "avoided_e2e", "avoided_builds", "avoided_lint", "avoided_typechecks")))
        self.assertIsNone(telemetry.parse_verification_marker(marker.replace("Verification budget integration", "C:\\private\\file")))
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            payload = collector.collect_v5(codex_home=home, sessions_dir=home / "sessions", source_root=ROOT)
        self.assertEqual(5, payload["schema_version"])
        self.assertEqual("5.0.0", payload["system_status"][0]["collector_version"])
        self.assertTrue(collector.validate_v5(payload))
        self.assertEqual(1, len(collector.payload_batches(payload)))

    def test_praxis_dashboard_separates_normal_and_special_verification(self) -> None:
        def event(event_id: str, classification: str) -> dict:
            return {
                "schema_version": "1", "event": "execution.completed", "event_id": event_id,
                "timestamp": "2026-08-29T00:00:00Z", "source": "routecraft_core",
                "provider": None, "agent": None, "model": None, "project": None,
                "task_id": event_id, "status": "succeeded", "event_classification": "normal",
                "metadata": {"verification": {"plan": {"setting": "auto_min", "budget": "min", "event_classification": classification}, "outcome": {
                    "status": "pass", "tests_run": 1, "targeted_tests": 1, "full_suites": 0,
                    "builds": 0, "lint_runs": 0, "typechecks": 0, "e2e_runs": 0,
                    "avoided_full_suites": 1, "avoided_e2e": 1, "avoided_builds": 1,
                    "avoided_lint": 1, "avoided_typechecks": 1, "verification_duration_ms": 10,
                }}},
            }
        snapshot = build_snapshot([event("normal", "normal"), event("stress", "stress_test")])
        self.assertEqual(1, snapshot["verification"]["normal_tasks"])
        self.assertEqual(1, snapshot["verification"]["special_tasks"])
        self.assertEqual(1, snapshot["verification"]["performed_checks"])
        self.assertEqual(5, snapshot["verification"]["avoided_checks"])


if __name__ == "__main__":
    unittest.main()
