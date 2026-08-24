from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))

import routecraft_collector as collector
import routecraft_graph_cli as graph_cli
from routecraft_graph import GraphEngine, GraphStore, default_config
from routecraft_graph.ir import make_graph, make_node
from routecraft_graph.telemetry import privacy_projection


def intent() -> dict[str, object]:
    return {
        "request_summary": "collector contract fixture",
        "objectives": ["verify"],
        "non_goals": [],
        "constraints": [],
        "acceptance_criteria": [{"criterion_id": "AC-1", "statement": "global gate passes"}],
        "risk_level": "low",
        "external_mutations": [],
        "approval_requirements": [],
        "privacy_boundary": {"local_only": ["source"], "exportable": ["aggregate"]},
        "budget": {"max_tokens": None, "max_duration_seconds": None, "max_child_runs": None},
        "deadline_if_known": None,
    }


def wide_graph() -> dict[str, object]:
    nodes = [
        make_node(
            f"n_{index:02}",
            "GATE" if index == 24 else "DETERMINISTIC",
            "bounded fixture",
            dependencies=[] if index == 0 else [f"n_{index - 1:02}"],
        )
        for index in range(25)
    ]
    nodes[-1]["gate_policy"]["global"] = True
    edges = [
        {
            "from": f"n_{index - 1:02}",
            "to": f"n_{index:02}",
            "edge_type": "depends_on",
            "condition": None,
            "data_contract": {},
        }
        for index in range(1, 25)
    ]
    return make_graph(
        "small_bug_fix",
        nodes,
        edges,
        intent(),
        graph_id="g_collector_wide_fixture",
        mode="observe",
        now="2026-08-25T00:00:00Z",
    )


class GraphCollectorBundleTests(unittest.TestCase):
    def test_graph_plan_refreshes_collector_bundle_without_explicit_export(self) -> None:
        fixture = json.loads((ROOT / "samples" / "graph-ir-v1-fast-path.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "codex-home"
            graph_input = base / "graph.json"
            graph_input.write_text(json.dumps(fixture), encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False):
                planned = graph_cli.plan(
                    graph_input,
                    config_path=None,
                    store_path=base / "graph-state.sqlite3",
                    data_dir=None,
                    mode=None,
                )
            self.assertTrue(planned["collector_bundle_saved"])
            bundle = home / "routecraft" / "graph" / "latest-collector-v4.json"
            self.assertTrue(bundle.is_file())
            self.assertEqual(1, len(json.loads(bundle.read_text(encoding="utf-8"))["graph_runs"]))

    def test_optional_collector_cache_failure_does_not_fail_local_graph_export(self) -> None:
        fixture = json.loads((ROOT / "samples" / "graph-ir-v1-fast-path.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            store = base / "graph-state.sqlite3"
            GraphEngine(GraphStore(store), config=default_config()).plan(fixture)
            local_export = base / "graph-export.json"
            with mock.patch.object(graph_cli, "_write_json_replace", side_effect=OSError("unavailable")):
                exported = graph_cli.export(
                    fixture["graph_id"],
                    local_export,
                    config_path=None,
                    store_path=store,
                    data_dir=None,
                )

            self.assertTrue(local_export.is_file())
            self.assertFalse(exported["collector_bundle_saved"])

    def test_durable_graph_export_materializes_one_canonical_bundle_for_collection(self) -> None:
        fixture = json.loads((ROOT / "samples" / "graph-ir-v1-fast-path.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "codex-home"
            store = base / "graph-state.sqlite3"
            engine = GraphEngine(GraphStore(store), config=default_config())
            engine.plan(fixture)
            local_export = base / "graph-export.json"
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False):
                exported = graph_cli.export(
                    fixture["graph_id"],
                    local_export,
                    config_path=None,
                    store_path=store,
                    data_dir=None,
                )
                with mock.patch.object(
                    collector, "_memory_metrics_with_availability", return_value=(None, False, False)
                ), mock.patch.object(collector, "usage_snapshots", return_value=[]):
                    payload = collector.collect_v4(
                        source_root=ROOT,
                        codex_home=home,
                        sessions_dir=base / "sessions",
                    )

            self.assertTrue(local_export.is_file())
            self.assertTrue(exported["collector_bundle_saved"])
            self.assertFalse(exported["collector_bundle_detail_downgraded"])
            bundle = home / "routecraft" / "graph" / "latest-collector-v4.json"
            self.assertTrue(bundle.is_file())
            self.assertEqual(
                {"graph_runs", "graph_node_metrics", "graph_events"},
                set(json.loads(bundle.read_text(encoding="utf-8"))),
            )
            self.assertTrue(collector.validate_v4(payload))
            self.assertEqual(1, len(payload["graph_runs"]))
            self.assertEqual(2, len(payload["graph_node_metrics"]))
            self.assertTrue(payload["graph_events"])
            graph_run_id = payload["graph_runs"][0]["graph_run_id"]
            self.assertTrue(all(row["graph_run_id"] == graph_run_id for row in payload["graph_node_metrics"]))
            self.assertTrue(all(row["graph_run_id"] == graph_run_id for row in payload["graph_events"]))

    def test_25_node_76_row_bundle_is_rejected_then_downgraded_without_splitting(self) -> None:
        projection = privacy_projection(wide_graph(), checkpoint_count=1)
        total = sum(len(projection[family]) for family in ("graph_runs", "graph_node_metrics", "graph_events"))
        self.assertEqual(76, total)

        raw_payload = collector.fixture_payload_v4()
        raw_payload.update(projection)
        self.assertFalse(collector.validate_v4(raw_payload))

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False):
                materialized = graph_cli._materialize_collector_bundle(projection)
                with mock.patch.object(
                    collector, "_memory_metrics_with_availability", return_value=(None, False, False)
                ), mock.patch.object(collector, "usage_snapshots", return_value=[]):
                    payload = collector.collect_v4(source_root=ROOT, codex_home=home)

            self.assertTrue(materialized["collector_bundle_saved"])
            self.assertTrue(materialized["collector_bundle_detail_downgraded"])
            self.assertTrue(collector.validate_v4(payload))
            self.assertEqual(1, len(payload["graph_runs"]))
            self.assertEqual([], payload["graph_node_metrics"])
            self.assertEqual([], payload["graph_events"])
            self.assertLessEqual(
                sum(len(payload[family]) for family in ("graph_runs", "graph_node_metrics", "graph_events")),
                collector.MAX_GRAPH_BUNDLE_ROWS,
            )


if __name__ == "__main__":
    unittest.main()
