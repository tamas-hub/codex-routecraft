from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))

import routecraft_collector as collector
import routecraft_graph_telemetry as telemetry


class GraphTelemetryTests(unittest.TestCase):
    def test_safe_projection_is_collector_v4_compatible(self) -> None:
        value = telemetry.project(
            "a" * 32, "b" * 32, "2026-08-24T00:00:00Z",
            [{"node_type": "AGENT", "lane": "sol", "status": "ACCEPTED", "attempt_count": 1, "gate_status": "PASS", "duration_ms": None, "total_tokens": None, "accepted": True}],
            [{"event_type": "checkpoint", "status": "RUNNING"}],
        )
        self.assertTrue(collector._valid_family("graph_node_metrics", value["graph_node_metrics"][0]))
        self.assertTrue(collector._valid_family("graph_events", value["graph_events"][0]))

    def test_private_semantic_content_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            telemetry.project("a" * 32, "b" * 32, "2026-08-24T00:00:00Z", [{"prompt": "private"}], [])

    def test_dependency_and_gate_events_contain_only_run_local_ordinals(self) -> None:
        value = telemetry.project(
            "a" * 32, "b" * 32, "2026-08-24T00:00:00Z", [],
            [
                {"event_type": "dependency", "status": "RUNNING", "source_node_ordinal": 1, "target_node_ordinal": 2},
                {"event_type": "gate", "status": "ACCEPTED", "node_ordinal": 2, "gate_status": "PASS"},
            ],
        )
        dependency, gate = value["graph_events"]
        self.assertEqual(1, dependency["event_sequence"])
        self.assertTrue(collector._valid_family("graph_events", dependency))
        self.assertTrue(collector._valid_family("graph_events", gate))


if __name__ == "__main__":
    unittest.main()
