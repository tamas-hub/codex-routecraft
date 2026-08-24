from __future__ import annotations

import sys
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))

from routecraft_graph import GraphStore, GraphValidationError, compile_graph
from routecraft_graph.ir import make_graph, make_node
from routecraft_graph.scheduler import ready_nodes
from routecraft_graph.state import (
    StateTransitionError,
    accept_node,
    apply_send_back,
    mark_ready,
    recompute_input_hashes,
    resolve_gate_result,
    retry_node,
    start_node,
)
from tests.graph_test_boundary import TestGraphEngine as GraphEngine


HASH = "sha256:" + "a" * 64


def intent() -> dict[str, object]:
    return {
        "request_summary": "typed edge control fixture",
        "objectives": ["verify deterministic branch semantics"],
        "non_goals": [],
        "constraints": [],
        "acceptance_criteria": [{"criterion_id": "AC-EDGE-1", "statement": "final gate passes"}],
        "risk_level": "low",
        "external_mutations": [],
        "approval_requirements": [],
        "privacy_boundary": {"local_only": ["source"], "exportable": ["aggregate"]},
        "budget": {"max_tokens": None, "max_duration_seconds": None, "max_child_runs": None},
        "deadline_if_known": None,
    }


def edge(source: str, target: str, kind: str, condition: object = None) -> dict[str, object]:
    return {"from": source, "to": target, "edge_type": kind, "condition": condition, "data_contract": {}}


def statuses(value: dict[str, object]) -> dict[str, str]:
    return {node["node_id"]: node["status"] for node in value["nodes"]}


def graph(*, with_send_back: bool = False) -> dict[str, object]:
    work = make_node(
        "n_work",
        "DETERMINISTIC",
        "produce",
        retry_policy={"max_attempts": 2, "max_tokens": None, "max_duration_seconds": 30, "max_failed_gates": 1} if with_send_back else None,
    )
    side = make_node("n_side", "DETERMINISTIC", "independent evidence")
    quality = make_node(
        "n_quality",
        "GATE",
        "quality decision",
        dependencies=["n_work"],
        retry_policy={"max_attempts": 2, "max_tokens": None, "max_duration_seconds": 30, "max_failed_gates": 1} if with_send_back else None,
    )
    passed = make_node("n_pass", "DETERMINISTIC", "pass branch", dependencies=["n_quality"])
    failed = make_node("n_fail", "DETERMINISTIC", "correction branch", dependencies=["n_quality"])
    merge = make_node("n_merge", "MERGE", "merge selected branch", dependencies=["n_pass", "n_fail", "n_side"])
    final = make_node("n_final", "GATE", "global acceptance", dependencies=["n_merge"])
    final["gate_policy"]["global"] = True
    edges = [
        edge("n_work", "n_quality", "sequence"),
        # Historical null is canonicalized at compile time; it is not a
        # free-form predicate and the compiled IR stores the typed object.
        edge("n_quality", "n_pass", "gate_pass"),
        edge("n_quality", "n_fail", "gate_fail"),
        edge("n_pass", "n_merge", "merge"),
        edge("n_fail", "n_merge", "merge"),
        edge("n_side", "n_merge", "merge"),
        edge("n_merge", "n_final", "sequence"),
    ]
    if with_send_back:
        edges.append(edge("n_quality", "n_work", "send_back", {"kind": "control_transition", "on": "FAIL", "max_transitions": 1}))
    return make_graph(
        "small_bug_fix",
        [work, side, quality, passed, failed, merge, final],
        edges,
        intent(),
        graph_id="g_typed_edge",
        mode="observe",
        now="2026-08-25T00:00:00Z",
    )


def compile_fixture(*, with_send_back: bool = False) -> dict[str, object]:
    return compile_graph(graph(with_send_back=with_send_back))["ir"]


def enforce_config() -> dict[str, object]:
    return {
        "config_version": 1,
        "graph": {"mode": "enforce", "max_parallelism": 3, "max_node_attempts": 3, "max_graph_revisions": 3, "state_store": None, "checkpoint": True},
        "policy": {"production_policy": "routecraft-production-v1", "allowlisted_task_classes": ["small_bug_fix"]},
        "control_center": {"enabled": False},
    }


def evidence(node_id: str, result: str = "PASS") -> dict[str, object]:
    return {
        "evidence_id": "ev_" + node_id,
        "classification": "FACT",
        "evidence_type": "schema_result",
        "statement": "typed edge engine fixture",
        "source_kind": "local_command",
        "artifact_hash": HASH,
        "result": result,
        "created_at": "2026-08-25T00:00:00Z",
        "node_id": node_id,
    }


def usage() -> dict[str, int]:
    return {"duration_ms": 1, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "child_runs": 0}


class TypedEdgeCompilerTests(unittest.TestCase):
    def test_legacy_null_gate_edges_are_canonicalized_to_explicit_predicates(self) -> None:
        value = compile_fixture()
        conditions = {(item["from"], item["to"]): item["condition"] for item in value["edges"]}
        self.assertEqual({"kind": "gate_result", "equals": "PASS"}, conditions[("n_quality", "n_pass")])
        self.assertEqual({"kind": "gate_result", "equals": "FAIL"}, conditions[("n_quality", "n_fail")])

    def test_free_form_and_mismatched_conditions_fail_closed(self) -> None:
        value = graph()
        value["edges"][1]["condition"] = "let the evaluator decide"
        with self.assertRaisesRegex(GraphValidationError, "IR_SCHEMA_INVALID"):
            compile_graph(value)
        value = graph()
        value["edges"][1]["condition"] = {"kind": "gate_result", "equals": "FAIL"}
        with self.assertRaisesRegex(GraphValidationError, "IR_SCHEMA_INVALID"):
            compile_graph(value)

    def test_send_back_is_bounded_reverse_control_not_a_dag_cycle(self) -> None:
        value = compile_fixture(with_send_back=True)
        self.assertEqual(["n_side", "n_work", "n_quality", "n_fail", "n_pass", "n_merge", "n_final"], compile_graph(value)["node_order"])
        invalid = graph(with_send_back=True)
        next(item for item in invalid["edges"] if item["edge_type"] == "send_back")["condition"] = None
        with self.assertRaisesRegex(GraphValidationError, "IR_SCHEMA_INVALID"):
            compile_graph(invalid)
        invalid = graph(with_send_back=True)
        invalid["edges"][-1] = edge("n_work", "n_quality", "send_back", {"kind": "control_transition", "on": "FAIL", "max_transitions": 1})
        with self.assertRaisesRegex(GraphValidationError, "SEND_BACK_INVALID"):
            compile_graph(invalid)
        invalid = graph(with_send_back=True)
        next(item for item in invalid["edges"] if item["edge_type"] == "send_back")["condition"]["max_transitions"] = 2
        with self.assertRaisesRegex(GraphValidationError, "RETRY_BUDGET_INVALID"):
            compile_graph(invalid)
        invalid = graph(with_send_back=True)
        next(item for item in invalid["nodes"] if item["node_id"] == "n_quality")["retry_policy"]["max_attempts"] = 1
        with self.assertRaisesRegex(GraphValidationError, "RETRY_BUDGET_INVALID"):
            compile_graph(invalid)


class TypedEdgeStateTests(unittest.TestCase):
    def _start_accept(self, value: dict[str, object], node_id: str) -> dict[str, object]:
        value = start_node(value, node_id)
        return accept_node(value, node_id, {}, ["ev_" + node_id])

    def _reach_quality(self, value: dict[str, object]) -> dict[str, object]:
        value = mark_ready(value)
        self.assertEqual(["n_work", "n_side"], ready_nodes(value))
        value = self._start_accept(value, "n_side")
        value = self._start_accept(value, "n_work")
        self.assertEqual(["n_quality"], ready_nodes(value))
        return start_node(value, "n_quality")

    def test_pass_activates_only_pass_branch_and_skips_fail_branch(self) -> None:
        value = self._reach_quality(compile_fixture())
        resolved = resolve_gate_result(value, "n_quality", "PASS", {}, ["ev_quality"])
        self.assertEqual("PASS", next(item for item in resolved["nodes"] if item["node_id"] == "n_quality")["gate_result"])
        self.assertEqual("SKIPPED", statuses(resolved)["n_fail"])
        self.assertEqual(["n_pass"], ready_nodes(resolved))
        resolved = self._start_accept(resolved, "n_pass")
        self.assertEqual(["n_merge"], ready_nodes(resolved))

    def test_fail_and_inconclusive_never_activate_pass_branch(self) -> None:
        for verdict in ("FAIL", "INCONCLUSIVE"):
            with self.subTest(verdict=verdict):
                value = self._reach_quality(compile_fixture())
                resolved = resolve_gate_result(value, "n_quality", verdict, {}, ["ev_quality"])
                self.assertEqual("SKIPPED", statuses(resolved)["n_pass"])
                self.assertEqual(["n_fail"], ready_nodes(resolved))
                self.assertEqual("RUNNING", resolved["status"])

    def test_gate_verdict_participates_in_downstream_input_hash(self) -> None:
        value = compile_fixture()
        quality = next(item for item in value["nodes"] if item["node_id"] == "n_quality")
        quality["status"], quality["output_hash"], quality["gate_result"] = "ACCEPTED", "sha256:" + "f" * 64, "PASS"
        passed = recompute_input_hashes(value)
        quality["gate_result"] = "FAIL"
        failed = recompute_input_hashes(value)
        pass_hash = next(item for item in passed["nodes"] if item["node_id"] == "n_pass")["input_hash"]
        fail_hash = next(item for item in failed["nodes"] if item["node_id"] == "n_pass")["input_hash"]
        self.assertNotEqual(pass_hash, fail_hash)

    def test_send_back_invalidates_only_affected_closure_and_preserves_side_work(self) -> None:
        value = self._reach_quality(compile_fixture(with_send_back=True))
        resolved = resolve_gate_result(value, "n_quality", "FAIL", {}, ["ev_quality"])
        sent = apply_send_back(resolved, "n_quality")
        state = statuses(sent)
        self.assertEqual("FAILED", state["n_work"])
        self.assertEqual("INVALIDATED", state["n_quality"])
        self.assertEqual("INVALIDATED", state["n_fail"])
        self.assertEqual("FROZEN", state["n_side"])
        self.assertEqual("RUNNING", sent["status"])

    def test_send_back_budget_exhaustion_escalates_convergence_failure(self) -> None:
        value = self._reach_quality(compile_fixture(with_send_back=True))
        first = apply_send_back(resolve_gate_result(value, "n_quality", "FAIL", {}, ["ev_quality"]) , "n_quality")
        retried = retry_node(first, "n_work")
        retried = self._start_accept(retried, "n_work")
        retried = start_node(retried, "n_quality")
        second = resolve_gate_result(retried, "n_quality", "FAIL", {}, ["ev_quality_2"])
        with self.assertRaisesRegex(StateTransitionError, "NODE_CONVERGENCE_FAILED"):
            apply_send_back(second, "n_quality")


class TypedEdgeEngineIntegrationTests(unittest.TestCase):
    def _running_quality_gate(self, store: GraphStore) -> GraphEngine:
        value = graph(with_send_back=True)
        value["mode"] = "enforce"
        engine = GraphEngine(store, config=enforce_config())
        engine.plan(value)
        for node_id in ("n_side", "n_work"):
            engine.start("g_typed_edge", node_id)
            engine.record_result("g_typed_edge", node_id, {}, [evidence(node_id)], usage=usage())
        engine.start("g_typed_edge", "n_quality")
        return engine

    def test_gate_failure_is_checkpointed_then_applies_bounded_send_back(self) -> None:
        value = graph(with_send_back=True)
        value["mode"] = "enforce"
        with tempfile.TemporaryDirectory() as temp:
            store = GraphStore(Path(temp) / "state.sqlite3")
            engine = GraphEngine(store, config=enforce_config())
            engine.plan(value)
            for node_id in ("n_side", "n_work"):
                engine.start("g_typed_edge", node_id)
                engine.record_result("g_typed_edge", node_id, {}, [evidence(node_id)], usage=usage())
            engine.start("g_typed_edge", "n_quality")
            sent = engine.record_result(
                "g_typed_edge",
                "n_quality",
                {},
                [evidence("n_quality", "FAIL")],
                gate_result="FAIL",
                usage=usage(),
            )
            self.assertEqual("FAILED", statuses(sent)["n_work"])
            self.assertEqual("INVALIDATED", statuses(sent)["n_quality"])
            self.assertEqual("FROZEN", statuses(sent)["n_side"])
            retried = engine.retry("g_typed_edge", "n_work")
            self.assertEqual("READY", statuses(retried)["n_work"])

            connection = sqlite3.connect(store.path)
            try:
                row = connection.execute(
                    "SELECT payload_json FROM checkpoints WHERE graph_id=? AND boundary='gate_resolution' ORDER BY sequence DESC LIMIT 1",
                    ("g_typed_edge",),
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNotNone(row)
            checkpoint = json.loads(row[0])
            gate = next(item for item in checkpoint["nodes"] if item["node_id"] == "n_quality")
            self.assertEqual("ACCEPTED", gate["status"])
            self.assertEqual("FAIL", gate["gate_result"])
            self.assertEqual(1, engine.export("g_typed_edge")["telemetry"]["graph_runs"][0]["send_back_count"])

    def test_resume_completes_gate_resolution_crash_without_following_gate_fail_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            store = GraphStore(path)
            engine = self._running_quality_gate(store)
            real_commit = store.commit_result

            def crash_after_gate_resolution(ir, boundary, ledger_entries, **kwargs):
                result = real_commit(ir, boundary, ledger_entries, **kwargs)
                if boundary == "gate_resolution":
                    raise RuntimeError("simulated crash after gate resolution commit")
                return result

            store.commit_result = crash_after_gate_resolution
            with self.assertRaisesRegex(RuntimeError, "gate resolution"):
                engine.record_result(
                    "g_typed_edge", "n_quality", {}, [evidence("n_quality", "FAIL")],
                    gate_result="FAIL", usage=usage(),
                )

            resumed = GraphEngine(GraphStore(path), config=enforce_config()).resume("g_typed_edge")
            state = statuses(resumed)
            self.assertEqual("FAILED", state["n_work"])
            self.assertEqual("INVALIDATED", state["n_quality"])
            self.assertEqual("INVALIDATED", state["n_fail"])
            self.assertEqual("FROZEN", state["n_side"])
            self.assertNotEqual("READY", state["n_fail"])
            self.assertEqual(1, GraphStore(path).checkpoint_boundary_count("g_typed_edge", 1, "send_back"))

    def test_resume_preserves_post_send_back_state_after_crash_before_return(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            store = GraphStore(path)
            engine = self._running_quality_gate(store)
            real_commit = store.save_and_checkpoint

            def crash_after_send_back(ir, boundary, **kwargs):
                result = real_commit(ir, boundary, **kwargs)
                if boundary == "send_back":
                    raise RuntimeError("simulated crash after send-back commit")
                return result

            store.save_and_checkpoint = crash_after_send_back
            with self.assertRaisesRegex(RuntimeError, "send-back"):
                engine.record_result(
                    "g_typed_edge", "n_quality", {}, [evidence("n_quality", "FAIL")],
                    gate_result="FAIL", usage=usage(),
                )

            resumed = GraphEngine(GraphStore(path), config=enforce_config()).resume("g_typed_edge")
            state = statuses(resumed)
            self.assertEqual("FAILED", state["n_work"])
            self.assertEqual("INVALIDATED", state["n_quality"])
            self.assertEqual("INVALIDATED", state["n_fail"])
            self.assertEqual("FROZEN", state["n_side"])
            # The post-send-back snapshot is the newest checkpointed truth;
            # resume cannot overwrite it with the earlier gate-resolution IR.
            reopened = GraphStore(path)
            self.assertEqual(1, reopened.checkpoint_boundary_count("g_typed_edge", 1, "send_back"))
            self.assertEqual("resume_complete", reopened.latest_checkpoint("g_typed_edge")["boundary"])


if __name__ == "__main__":
    unittest.main()
