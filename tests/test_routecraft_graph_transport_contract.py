from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))

from routecraft_graph import GraphStore
from routecraft_graph.constants import DEPENDENCY_EDGE_TYPES
from routecraft_graph.state import accept_node, mark_ready, resolve_gate_result, start_node
from routecraft_graph.telemetry import privacy_projection
from tests.graph_test_boundary import TestGraphEngine as GraphEngine
from tests.test_routecraft_graph_07_edge_control import compile_fixture, enforce_config, evidence, graph, usage


def accepted_typed_branch_projection() -> dict[str, object]:
    graph = mark_ready(compile_fixture())
    for node_id in ("n_side", "n_work"):
        graph = start_node(graph, node_id)
        graph = accept_node(graph, node_id, {}, ["ev_" + node_id])
    graph = start_node(graph, "n_quality")
    graph = resolve_gate_result(graph, "n_quality", "PASS", {}, ["ev_n_quality"])
    graph = start_node(graph, "n_pass")
    graph = accept_node(graph, "n_pass", {}, ["ev_n_pass"])
    graph = start_node(graph, "n_merge")
    graph = accept_node(graph, "n_merge", {}, ["ev_n_merge"])
    graph = start_node(graph, "n_final")
    graph = resolve_gate_result(graph, "n_final", "PASS", {}, ["ev_n_final"])
    graph["updated_at"] = "2026-08-25T00:00:00Z"
    projected = privacy_projection(
        graph,
        checkpoint_count=7,
        send_back_count=0,
        device_id="b" * 32,
    )
    return {
        "schema_version": 4,
        "runs": [],
        "graph_runs": projected["graph_runs"],
        "graph_node_metrics": projected["graph_node_metrics"],
        "graph_events": [],
    }


def accepted_send_back_projection() -> dict[str, object]:
    """Run a real durable FAIL -> send-back -> retry -> global PASS path."""
    with tempfile.TemporaryDirectory() as temporary:
        value = graph(with_send_back=True)
        value["mode"] = "enforce"
        engine = GraphEngine(GraphStore(Path(temporary) / "state.sqlite3"), config=enforce_config())
        engine.plan(value)

        def complete(node_id: str, *, verdict: str = "PASS", suffix: str = "") -> None:
            engine.start("g_typed_edge", node_id)
            proof = evidence(node_id, verdict)
            proof["evidence_id"] += suffix
            engine.record_result(
                "g_typed_edge", node_id, {}, [proof], gate_result=verdict, usage=usage(),
            )

        complete("n_side")
        complete("n_work", suffix="_first")
        complete("n_quality", verdict="FAIL", suffix="_fail")
        engine.retry("g_typed_edge", "n_work")
        complete("n_work", suffix="_retry")
        complete("n_quality", verdict="PASS", suffix="_pass")
        complete("n_pass")
        complete("n_merge")
        complete("n_final", verdict="PASS")

        projected = engine.export("g_typed_edge")["telemetry"]
        # State transitions use real wall-clock timestamps.  Only normalize the
        # public observation timestamp so this cross-repository contract stays
        # byte-for-byte reproducible; every count/event comes from the durable
        # Runtime execution above.
        for family in ("graph_runs", "graph_node_metrics", "graph_events"):
            for row in projected[family]:
                row["observed_at"] = "2026-08-25T00:00:00Z"
        return {"schema_version": 4, "runs": [], **projected}


class GraphTransportContractTests(unittest.TestCase):
    def test_runtime_projection_matches_control_center_contract_fixture(self) -> None:
        expected = json.loads(
            (ROOT / "samples" / "telemetry-v4-typed-branch.json").read_text(
                encoding="utf-8"
            )
        )
        actual = accepted_typed_branch_projection()
        self.assertEqual(expected, actual)
        run = actual["graph_runs"][0]
        self.assertEqual(6, run["accepted_count"])
        self.assertEqual(1, sum(node["status"] == "SKIPPED" for node in actual["graph_node_metrics"]))
        self.assertTrue(all(node["send_back_count"] is None for node in actual["graph_node_metrics"]))

    def test_control_edges_are_not_projected_as_dependencies(self) -> None:
        graph = compile_fixture(with_send_back=True)
        graph["updated_at"] = "2026-08-25T00:00:00Z"
        projected = privacy_projection(graph, checkpoint_count=1, send_back_count=0)
        dependencies = [
            event for event in projected["graph_events"]
            if event["event_type"] == "dependency"
        ]
        expected = sum(edge["edge_type"] in DEPENDENCY_EDGE_TYPES for edge in graph["edges"])
        self.assertEqual(expected, len(dependencies))
        self.assertLess(expected, len(graph["edges"]))

    def test_runtime_send_back_history_matches_control_center_fixture(self) -> None:
        expected = json.loads(
            (ROOT / "samples" / "telemetry-v4-send-back-recovery.json").read_text(
                encoding="utf-8"
            )
        )
        actual = accepted_send_back_projection()
        self.assertEqual(expected, actual)
        run = actual["graph_runs"][0]
        self.assertEqual("ACCEPTED", run["status"])
        self.assertEqual(1, run["send_back_count"])
        self.assertEqual(1, run["gate_fail_count"])
        self.assertEqual(2, run["gate_pass_count"])
        self.assertEqual(
            ["FAIL", "PASS", "PASS"],
            [
                event["gate_status"] for event in actual["graph_events"]
                if event["event_type"] == "gate"
            ],
        )
        self.assertEqual(1, sum(event["event_type"] == "send_back" for event in actual["graph_events"]))


if __name__ == "__main__":
    unittest.main()
