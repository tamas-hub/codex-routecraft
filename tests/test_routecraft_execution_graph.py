from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-routecraft" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import routecraft_execution_graph as graph
import routecraft_collector as collector


class ExecutionGraphTests(unittest.TestCase):
    def unit(
        self,
        unit_id: str,
        *,
        dependencies: tuple[str, ...] = (),
        owner: str | None = None,
        output_schema: dict[str, object] | None = None,
        retry_policy: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return graph.make_unit(
            unit_id,
            f"objective-{unit_id}",
            dependencies=dependencies,
            ownership=owner or f"owner-{unit_id}",
            output_schema=output_schema or {},
            retry_policy=retry_policy or {},
        )

    def test_graph_validation_is_deterministic_and_rejects_duplicates_and_cycles(self) -> None:
        first = self.unit("a")
        second = self.unit("b", dependencies=("a",))
        left = graph.create_graph("graph-1", "implementation", [second, first], now_ms=100)
        right = graph.create_graph("graph-1", "implementation", [first, second], now_ms=100)
        self.assertEqual(left, right)
        self.assertEqual(["a", "b"], graph.topological_order(left))
        self.assertEqual([], graph.find_cycles(left))
        with self.assertRaises(graph.GraphValidationError):
            graph.create_graph("dup", "task", [self.unit("a"), self.unit("a")], now_ms=100)
        cyclic_a = self.unit("a", dependencies=("b",))
        cyclic_b = self.unit("b", dependencies=("a",))
        with self.assertRaises(graph.GraphValidationError):
            graph.create_graph("cycle", "task", [cyclic_a, cyclic_b], now_ms=100)

    def test_schema_type_and_completeness_validation(self) -> None:
        schema = {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "integer"}},
        }
        self.assertTrue(graph.validate_schema(schema))
        self.assertTrue(graph.validate_value({"value": 1}, schema))
        self.assertFalse(graph.validate_value({}, schema))
        self.assertFalse(graph.validate_value({"value": "1"}, schema))
        self.assertFalse(graph.validate_schema({"type": "not-json"}))

    def test_graph_rejects_raw_private_fields_and_absolute_paths(self) -> None:
        unsafe = self.unit("unsafe")
        unsafe["verification"] = {"source": "full source text"}
        self.assertFalse(graph.validate_unit(unsafe))
        with self.assertRaises(graph.GraphValidationError):
            graph.create_graph("C:\\private\\graph", "task", [self.unit("a")], now_ms=100)

    def test_ready_sorting_and_ownership_conflict_selection(self) -> None:
        state = graph.create_graph(
            "ready",
            "task",
            [self.unit("c", owner="same"), self.unit("a", owner="same"), self.unit("b", owner="other")],
            now_ms=100,
        )
        self.assertEqual(["a", "b", "c"], graph.ready_nodes(state))
        conflicts = graph.ownership_conflicts(state)
        self.assertEqual(["a", "c"], conflicts[0]["unit_ids"])
        self.assertEqual(["a", "b"], graph.parallel_ready_nodes(state), "same owner is never run in parallel")

    def test_selective_failure_preserves_independent_success_and_reopens_dependents(self) -> None:
        state = graph.create_graph(
            "retry",
            "task",
            [
                self.unit("a"),
                self.unit("b", dependencies=("a",)),
                self.unit("c", dependencies=("b",)),
                self.unit("independent"),
            ],
            now_ms=100,
        )
        for identifier, value in (("a", 1), ("b", 2), ("c", 3), ("independent", 4)):
            state = graph.complete_unit(state, identifier, {"value": value}, now_ms=101 + value)
        self.assertEqual("accepted", state["status"])
        state = graph.fail_unit(state, "a", "verification_failed", now_ms=110)
        statuses = {unit["unit_id"]: unit["status"] for unit in state["nodes"]}
        self.assertEqual("failed", statuses["a"])
        self.assertEqual("reopened", statuses["b"])
        self.assertEqual("reopened", statuses["c"])
        self.assertEqual("accepted", statuses["independent"])
        self.assertEqual({"value": 4}, state["accepted_outputs"]["independent"])
        self.assertNotIn("b", state["accepted_outputs"])
        self.assertNotIn("c", state["accepted_outputs"])
        state = graph.retry_unit(state, "a", now_ms=111)
        state = graph.complete_unit(state, "a", {"value": 10}, now_ms=112)
        self.assertEqual(["b"], graph.ready_nodes(state))

    def test_retry_and_convergence_limits_stop_finitely(self) -> None:
        state = graph.create_graph(
            "bounded",
            "implementation",
            [self.unit("a", retry_policy={"max_attempts": 1})],
            limits={"max_attempt_per_unit": 1, "max_graph_steps": 4, "max_total_child_runs": 4},
            now_ms=100,
        )
        state = graph.record_unit_attempt(state, "a", "produce", {"ok": False, "reason_code": "bad_check"}, now_ms=101)
        self.assertEqual("retry_pending", state["status"])
        state = graph.retry_unit(state, "a", now_ms=102)
        self.assertEqual("convergence_failed", state["status"])
        self.assertEqual("retry_budget_exhausted", state["failure_reason"])
        summary = graph.to_d1_summary(state)
        self.assertEqual(0, summary["gate_pass_count"])
        self.assertEqual(0, summary["gate_fail_count"])
        payload = collector.fixture_payload_v4()
        payload["graph_runs"] = [summary]
        self.assertTrue(collector.validate_v4(payload))

    def test_constraints_export_only_after_whole_task_acceptance(self) -> None:
        state = graph.create_graph(
            "constraints",
            "task",
            [self.unit("a")],
            constraints=[{"constraint_id": "not-verified", "statement": "discard", "verified": False}],
            now_ms=100,
        )
        verified = {"constraint_id": "c1", "statement": "use typed output", "verified": True, "reusable": True, "evidence": {"ok": True}}
        state = graph.record_verified_constraint(state, verified, now_ms=101)
        self.assertEqual([], graph.export_decision_store_constraints(state))
        state = graph.complete_unit(state, "a", {"value": 1}, now_ms=102)
        self.assertEqual(
            [{"constraint_id": "c1", "statement": "use typed output", "verified": True, "reusable": True}],
            graph.export_decision_store_constraints(state),
        )

    def test_privacy_summary_is_aggregate_only(self) -> None:
        state = graph.create_graph("private graph", "implementation", [self.unit("a")], now_ms=100)
        state = graph.record_shadow_predictions(state, {"a": {"status": "accepted", "route": "lane-a", "child_runs": 1}}, now_ms=101)
        summary = graph.to_d1_summary(state)
        rendered = json.dumps(summary, ensure_ascii=False).lower()
        for forbidden in ("objective-a", "owner-a", "input_schema", "output_schema", "prompt", "source", "packet", "use typed output"):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(1, summary["node_count"])
        self.assertEqual("COMPILED", summary["status"])
        self.assertEqual(1, summary["accepted_count"])
        self.assertEqual(0, summary["attempt_count"])
        self.assertEqual(1, summary["critical_path_length"])
        self.assertIsNone(summary["input_tokens"])
        self.assertEqual(
            {
                "graph_run_id", "device_id", "observed_at", "event_classification", "graph_schema_version", "mode", "status",
                "graph_revision_count", "node_count", "edge_count", "parallel_width", "critical_path_length", "attempt_count",
                "retry_count", "send_back_count", "accepted_count", "frozen_count", "failed_count", "invalidated_count",
                "constraint_count", "checkpoint_count", "gate_pass_count", "gate_fail_count", "gate_inconclusive_count",
                "duration_ms", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
            },
            set(summary),
        )
        opaque_input = "a" * 32
        opaque_device_input = "b" * 32
        opaque_state = graph.create_graph(opaque_input, "implementation", [self.unit("a")], device_id=opaque_device_input, now_ms=100)
        opaque_summary = graph.to_d1_summary(opaque_state)
        self.assertNotEqual(opaque_input, opaque_summary["graph_run_id"])
        self.assertNotEqual(opaque_device_input, opaque_summary["device_id"])

    def test_mode_gate_and_current_routing_fallback(self) -> None:
        self.assertEqual("observe", graph.mode_gate("enforce", {"gate": "A", "passed": False, "all_required_passed": False})["effective_mode"])
        passed = {"gate": "A", "passed": True, "required_checks": {name: True for name in graph.HARDENING_GATE_A_REQUIRED_CHECKS}}
        self.assertTrue(graph.hardening_gate_a_passed(passed))
        legacy = graph.mode_gate("enforce", passed)
        self.assertEqual("observe", legacy["effective_mode"])
        self.assertFalse(legacy["enforce_allowed"])
        self.assertEqual("legacy_adapter_never_enforce", legacy["reason"])
        self.assertFalse(graph.enforce_mode_allowed(passed))
        state = graph.create_graph("modes", "task", [self.unit("a")], mode="enforce", hardening_gate=passed, now_ms=100)
        self.assertEqual("observe", state["mode"])
        observe = graph.create_graph("observe", "task", [self.unit("a")], now_ms=100)
        self.assertTrue(observe["mode_gate"]["current_routing_fallback"])
        incomplete = {"gate": "A", "passed": True, "required_checks": {"schema": True, "privacy": True}}
        self.assertFalse(graph.hardening_gate_a_passed(incomplete))
        unknown = {"gate": "A", "passed": True, "required_checks": {**passed["required_checks"], "unknown": True}}
        self.assertFalse(graph.hardening_gate_a_passed(unknown))
        non_boolean = {"gate": "A", "passed": True, "required_checks": {**passed["required_checks"], "collector_regression": 1}}
        self.assertFalse(graph.hardening_gate_a_passed(non_boolean))

    def test_merge_is_structural_and_not_last_write_wins(self) -> None:
        self.assertEqual({"a": 1, "b": 2}, graph.merge_outputs([{"a": 1}, {"b": 2}]))
        with self.assertRaises(graph.GraphValidationError):
            graph.merge_outputs([{"a": 1}, {"a": 2}])

    def test_state_transitions_copy_input(self) -> None:
        state = graph.create_graph("copy", "task", [self.unit("a")], now_ms=100)
        updated = graph.complete_unit(state, "a", {"ok": True}, now_ms=101)
        self.assertEqual("pending", state["status"])
        self.assertEqual("pending", state["nodes"][0]["status"])
        self.assertEqual("accepted", updated["status"])


if __name__ == "__main__":
    unittest.main()
