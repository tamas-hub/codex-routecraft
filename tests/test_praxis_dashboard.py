from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))

from praxis_dashboard.projection import build_snapshot
from praxis_dashboard.query import LegacyTelemetryEventSource, PraxisDashboardQuery
from routecraft_protocols import new_event, new_routecraft_telemetry


def event(name: str, **changes):
    changes.setdefault("event_id", "evt-" + name.replace(".", "-")); changes.setdefault("timestamp", "2026-08-27T00:00:00Z")
    data = new_event(name, "test", **changes)
    return data


class Source:
    def __init__(self, rows): self.rows = rows
    def sources(self): return [{"id": "fixture", "available": True}]
    def list_events(self, **kwargs):
        start = int(kwargs.get("cursor") or 0); limit = kwargs["limit"]
        rows = self.rows[start:start + limit]
        return {"events": rows, "cursor": str(start + limit) if start + limit < len(self.rows) else None}


class PraxisDashboardTests(unittest.TestCase):
    def test_routecraft_impact_is_dynamic_and_excludes_unknown_attribution(self):
        routed = new_routecraft_telemetry(
            run_id="run-1", requested_model="gpt-5.6-sol", requested_reasoning="ultra",
            selected_model="gpt-5.6-terra", selected_reasoning="high", actual_model="gpt-5.6-terra",
            actual_reasoning="high", decision_source="routecraft", decision_reason="bounded_offload",
            input_tokens=100, cached_input_tokens=25, total_tokens=120, execution_time_ms=50,
            memory_recall_used=True, memory_case_ids=["case-1"], rules_applied=["route_rule"],
        )
        unknown = dict(routed); unknown["run_id"] = "run-2"; unknown["actual_model"] = "gpt-5.6-luna"; unknown["selected_model"] = "gpt-5.6-luna"; unknown["memory_recall_used"] = False; unknown["decision_source"] = "unknown"
        data = build_snapshot([
            event("execution.completed", event_id="evt-routed", agent="routecraft_terra_high", metadata={"routecraft_telemetry": routed}),
            event("execution.completed", event_id="evt-unknown", agent="parent_plan", metadata={"routecraft_telemetry": unknown}),
        ])
        impact = data["routecraft_impact"]
        self.assertEqual(2, impact["observed_runs"]); self.assertEqual(1, impact["attributable_runs"])
        self.assertEqual(1, impact["route_changes"]["changed"]); self.assertEqual(1, impact["sol_ultra"]["classifications"]["terra_offload"])
        self.assertEqual(0.25, data["platform_efficiency"]["prompt_cache_hit_rate"])
        self.assertEqual(1, data["memory_effect"]["recall_assisted"])
        self.assertIsNone(impact["estimated_savings"]["level_2"])

    def test_ultra_offload_does_not_require_unobserved_actual_reasoning(self):
        offloaded = new_routecraft_telemetry(
            run_id="ultra-offload", requested_model="gpt-5.6-sol", requested_reasoning="ultra",
            actual_model="gpt-5.6-terra", actual_reasoning=None, decision_source="routecraft",
        )
        other_offload = new_routecraft_telemetry(
            run_id="ultra-other", requested_model="gpt-5.6-sol", requested_reasoning="ultra",
            actual_model="custom-model", actual_reasoning=None, decision_source="routecraft",
        )
        unresolved_sol = new_routecraft_telemetry(
            run_id="ultra-sol-unknown", requested_model="gpt-5.6-sol", requested_reasoning="ultra",
            actual_model="gpt-5.6-sol", actual_reasoning=None, decision_source="routecraft",
        )
        rows = [
            event("execution.completed", event_id="evt-ultra-offload", metadata={"routecraft_telemetry": offloaded}),
            event("execution.completed", event_id="evt-ultra-other", metadata={"routecraft_telemetry": other_offload}),
            event("execution.completed", event_id="evt-ultra-sol-unknown", metadata={"routecraft_telemetry": unresolved_sol}),
        ]
        snapshot = build_snapshot(rows)
        ultra = snapshot["routecraft_impact"]["sol_ultra"]
        self.assertEqual(3, ultra["requested"])
        self.assertEqual(2, ultra["denominator"])
        self.assertEqual(1, ultra["excluded"])
        self.assertEqual(1, ultra["classifications"]["terra_offload"])
        self.assertEqual(1, ultra["classifications"]["other"])
        self.assertEqual(2, ultra["offloaded"])
        self.assertEqual(1.0, ultra["optimization_rate"])
        drill_down = PraxisDashboardQuery(Source(rows)).runs(
            requested_model="sol", requested_reasoning="ultra",
            actual_model="terra", actual_reasoning="unknown",
        )
        self.assertEqual(1, drill_down["total"])
        self.assertEqual("ultra-offload", drill_down["runs"][0]["run_id"])

    def test_legacy_telemetry_adapter_and_safe_filtered_runs(self):
        row = {"run_id": "legacy-run", "role": "routecraft_luna_max", "human_model": "gpt-5.6-sol", "human_effort": "ultra", "actual_model": "gpt-5.6-luna", "actual_effort": "max", "started_at": "2026-08-27T00:00:00Z", "ended_at": "2026-08-27T00:00:02Z", "observed_at": "2026-08-27T00:00:02Z", "duration_ms": 2, "input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3, "reasoning_output_tokens": 4, "total_tokens": 17, "task_class": "review", "task_summary": "must-not-leak"}
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "routecraft-telemetry.json"
            path.write_text(json.dumps({"schema_version": 2, "runs": [row]}), encoding="utf-8")
            query = PraxisDashboardQuery(LegacyTelemetryEventSource(path, "legacy-telemetry"))
            snapshot = query.snapshot()
            self.assertTrue(snapshot["available"]); self.assertEqual(1, snapshot["data"]["routecraft_impact"]["attributable_runs"])
            runs = query.runs(requested_model="sol", actual_model="luna")
            self.assertEqual(1, runs["total"]); self.assertEqual("review", runs["runs"][0]["task_class"])
            self.assertIsNone(runs["runs"][0]["confidence"])
            self.assertNotIn("task_summary", json.dumps(runs))
            self.assertNotIn("metadata", runs["runs"][0])
            overridden = dict(row); overridden["decision_source"] = "codex"
            path.write_text(json.dumps({"schema_version": 2, "runs": [overridden]}), encoding="utf-8")
            overridden_snapshot = PraxisDashboardQuery(LegacyTelemetryEventSource(path, "legacy-telemetry")).snapshot()
            self.assertEqual(0, overridden_snapshot["data"]["routecraft_impact"]["attributable_runs"])
            self.assertEqual(1, overridden_snapshot["data"]["routecraft_impact"]["attribution_mix"]["codex"])
            path.write_text(json.dumps({"schema_version": 4, "runs": [row], "memory_tasks": []}), encoding="utf-8")
            self.assertTrue(PraxisDashboardQuery(LegacyTelemetryEventSource(path, "legacy-telemetry")).snapshot()["available"])

    def test_lifecycle_duplicate_merges_and_uses_independent_denominators(self):
        started = new_routecraft_telemetry(run_id="same-run", requested_model="gpt-5.6-sol", requested_reasoning="ultra", actual_model=None, actual_reasoning=None, decision_source="routecraft", input_tokens=9, total_tokens=9)
        finished = new_routecraft_telemetry(run_id="same-run", requested_model="gpt-5.6-sol", requested_reasoning="ultra", actual_model="custom-model", actual_reasoning="high", decision_source="routecraft", execution_time_ms=20, total_tokens=50)
        model_only = new_routecraft_telemetry(run_id="model-only", requested_model="gpt-5.6-sol", requested_reasoning=None, actual_model="gpt-5.6-terra", actual_reasoning=None, decision_source="routecraft", total_tokens=7)
        snapshot = build_snapshot([
            event("execution.started", event_id="evt-start", timestamp="2026-08-27T00:00:00Z", agent="routecraft_sol_ultra", metadata={"routecraft_telemetry": started}),
            event("execution.completed", event_id="evt-finish", timestamp="2026-08-27T00:01:00Z", agent="routecraft_sol_ultra", metadata={"routecraft_telemetry": finished}),
            event("execution.completed", event_id="evt-model-only", timestamp="2026-08-27T00:02:00Z", agent="routecraft_terra_high", metadata={"routecraft_telemetry": model_only}),
        ])
        impact = snapshot["routecraft_impact"]
        self.assertEqual(2, snapshot["execution"]["observed_runs"])
        self.assertEqual(57, snapshot["execution"]["tokens"])
        self.assertEqual(2, impact["requested_model_mix"]["denominator"])
        self.assertEqual(1, impact["requested_reasoning_mix"]["denominator"])
        self.assertEqual(2, impact["route_changes"]["changed"])
        self.assertEqual(1, impact["actual_model_mix"]["values"]["other"])
        self.assertEqual(2, impact["sol_offload"]["offloaded"])

    def test_safe_runs_include_only_allowed_fields(self):
        envelope = new_routecraft_telemetry(run_id="safe-run", requested_model="gpt-5.6-sol", requested_reasoning="high", actual_model="gpt-5.6-terra", actual_reasoning="medium", decision_source="routecraft", decision_reason="offload", decision_confidence=1.0, execution_time_ms=12, memory_recall_used=True)
        result = PraxisDashboardQuery(Source([event("execution.completed", agent="routecraft_terra_medium", metadata={"routecraft_telemetry": envelope})])).runs()
        self.assertEqual({"run_id", "event_id", "timestamp", "task_class", "requested_model", "requested_reasoning", "actual_model", "actual_reasoning", "decision_source", "decision_reason", "confidence", "tokens", "duration_ms", "memory_used"}, set(result["runs"][0]))

    def test_attribution_mix_and_ab_basis_are_explicit(self):
        def envelope(run_id, source, mode):
            value = new_routecraft_telemetry(run_id=run_id, requested_model="gpt-5.6-sol", requested_reasoning="high", actual_model="gpt-5.6-terra", actual_reasoning="medium", decision_source=source, input_tokens=10, cached_input_tokens=2, output_tokens=3, reasoning_tokens=4, total_tokens=17, execution_time_ms=5, retry_count=1, model_calls=1, tool_calls=2, file_reads=3, benchmark={"schema_version": "1", "mode": mode, "test_result": "passed", "final_success": True})
            return value
        snapshot = build_snapshot([
            event("execution.completed", event_id="ab-on", agent="routecraft_terra_medium", metadata={"routecraft_telemetry": envelope("ab-on", "routecraft", "on")}),
            event("execution.completed", event_id="ab-off", agent="routecraft_terra_medium", metadata={"routecraft_telemetry": envelope("ab-off", "routecraft", "off")}),
            event("execution.completed", event_id="ab-user", agent="routecraft_terra_medium", metadata={"routecraft_telemetry": envelope("ab-user", "user", "off")}),
        ])
        impact = snapshot["routecraft_impact"]
        self.assertEqual({"routecraft": 2, "user": 1, "codex": 0, "fallback": 0, "unknown": 0}, impact["attribution_mix"])
        self.assertEqual(0, impact["unknown_attribution"]); self.assertEqual(1, impact["excluded_non_routecraft"])
        self.assertEqual("unavailable", snapshot["ab_basis"]["status"])
        self.assertEqual("pair/scope identity unavailable", snapshot["ab_basis"]["basis"])
        self.assertEqual({"on": 1, "off": 2}, snapshot["ab_basis"]["observed_groups"])
        self.assertIsNone(snapshot["ab_basis"]["on"]["uncached_input_tokens"])
        self.assertEqual(3, snapshot["ab_basis"]["excluded"]["v1"])

    def test_ab_v2_requires_exact_identity_and_paired_metrics(self):
        def row(run_id, mode, pair, scope, **fields):
            benchmark = {"schema_version": "2", "mode": mode, "pair_id": pair, "scope_id": scope, "test_result": "passed", "final_success": True}
            telemetry = new_routecraft_telemetry(run_id=run_id, requested_model="gpt-5.6-sol", actual_model="gpt-5.6-terra", decision_source="routecraft", benchmark=benchmark, **fields)
            return event("execution.completed", event_id="evt-" + run_id, agent="routecraft_terra_high", metadata={"routecraft_telemetry": telemetry})
        snapshot = build_snapshot([
            row("pair-on", "on", "pair-1", "scope-1", total_tokens=10, input_tokens=8, cached_input_tokens=2, model_calls=1),
            row("pair-off", "off", "pair-1", "scope-1", total_tokens=20, input_tokens=10, cached_input_tokens=3, model_calls=2),
            row("alone", "on", "pair-2", "scope-1", total_tokens=99),
            row("dup-1", "on", "pair-3", "scope-1", total_tokens=99),
            row("dup-2", "on", "pair-3", "scope-1", total_tokens=99),
            row("dup-off", "off", "pair-3", "scope-1", total_tokens=99),
            row("asym-on", "on", "pair-4", "scope-1", total_tokens=7, model_calls=4),
            row("asym-off", "off", "pair-4", "scope-1", total_tokens=8),
        ])
        ab = snapshot["ab_basis"]
        self.assertEqual("measured", ab["status"])
        self.assertEqual(2, ab["on"]["runs"])
        self.assertEqual(17, ab["on"]["total_tokens"]); self.assertEqual(28, ab["off"]["total_tokens"])
        self.assertEqual(2, ab["paired_observed"]["total_tokens"])
        self.assertEqual(1, ab["paired_observed"]["model_calls"])
        self.assertEqual(1, ab["on"]["model_calls"]); self.assertEqual(2, ab["off"]["model_calls"])
        self.assertEqual(1, ab["excluded"]["unpaired"])
        self.assertEqual(3, ab["excluded"]["duplicate"])

    def test_unknown_or_selected_legacy_route_is_not_actual_evidence(self):
        rows = [
            {"run_id": "legacy-unknown", "role": "routecraft_terra_high", "human_model": "gpt-5.6-sol", "human_effort": "ultra", "actual_model": "unknown-model", "actual_effort": "unknown", "started_at": "2026-08-27T00:00:00Z", "ended_at": "2026-08-27T00:00:02Z", "observed_at": "2026-08-27T00:00:02Z"},
            {"run_id": "legacy-selected-only", "role": "routecraft_luna_max", "human_model": "gpt-5.6-sol", "human_effort": "ultra", "actual_model": None, "actual_effort": None, "started_at": "2026-08-27T00:00:00Z", "ended_at": "2026-08-27T00:00:02Z", "observed_at": "2026-08-27T00:00:02Z"},
        ]
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "routecraft-telemetry.json"
            path.write_text(json.dumps({"schema_version": 2, "runs": rows}), encoding="utf-8")
            snapshot = PraxisDashboardQuery(LegacyTelemetryEventSource(path, "legacy-telemetry")).snapshot()["data"]
        impact = snapshot["routecraft_impact"]
        self.assertEqual(0, impact["route_changes"]["denominator"])
        self.assertEqual(0, impact["sol_offload"]["requested_sol_runs"])
        self.assertEqual(2, impact["actual_model_mix"]["excluded"])

    def test_component_versions_use_component_manifests_and_safe_version_source(self):
        versions = build_snapshot([])["system_status"]["component_versions"]
        plugin_manifest = json.loads(
            (ROOT / "plugins" / "codex-routecraft" / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(plugin_manifest["version"], versions["routecraft-core"]["version"])
        self.assertEqual("0.1.0", versions["praxis-memory"]["version"])
        self.assertEqual("0.2.0", versions["praxis-dashboard"]["version"])
        self.assertEqual("5.0.0", versions["collector"]["version"])
        self.assertEqual("1.1.0", versions["telemetry-schema"]["version"])

    def test_empty_and_missing_source_are_explicit(self):
        absent = PraxisDashboardQuery().snapshot()
        self.assertFalse(absent["available"]); self.assertEqual(absent["code"], "unavailable")
        present = PraxisDashboardQuery(Source([])).snapshot()
        self.assertTrue(present["available"]); self.assertEqual(present["data"]["events"]["total"], 0)
        class Offline(Source):
            def sources(self): return [{"id": "fixture", "available": False, "path": "private", "token": "hidden"}]
        offline = PraxisDashboardQuery(Offline([]))
        self.assertFalse(offline.snapshot()["available"])
        self.assertFalse(offline.events()["available"])
        self.assertEqual([{"id": "fixture", "available": False}], offline.sources()["sources"])

    def test_failed_special_and_nullable_usage(self):
        rows = [
            event("task.started", status="running"),
            event("task.finished", status="failed", event_classification="benchmark_event"),
            event("memory.recalled", status="completed", metadata={"token_count": 12, "usage_units": 3, "elapsed_ms": 7}),
        ]
        data = build_snapshot(rows)
        self.assertEqual(data["runtime"]["running"], 1); self.assertEqual(data["runtime"]["failed"], 1)
        self.assertEqual(data["events"]["special"]["benchmark"], 1)
        self.assertEqual(data["usage"]["tokens"], 12); self.assertEqual(data["usage"]["usage_units"], 3)
        self.assertEqual(data["usage"]["duration_ms"], 7); self.assertIsNone(data["usage"]["duration_seconds"])
        self.assertIsNone(build_snapshot([])["usage"]["tokens"])

    def test_snapshot_uses_latest_task_state_and_orders_recent_events(self):
        rows = [
            event("execution.completed", event_id="evt-complete", timestamp="2026-08-27T00:02:00Z", task_id="task-1", status="succeeded"),
            event("task.started", event_id="evt-start", timestamp="2026-08-27T00:01:00Z", task_id="task-1", status="running"),
        ]
        data = build_snapshot(rows)
        self.assertEqual({"running": 0, "completed": 1, "failed": 0, "unknown": 0}, data["runtime"])
        self.assertEqual("evt-complete", data["events"]["recent"][0]["event_id"])
        self.assertEqual(1, data["usage"]["task_count"])

    def test_equal_timestamp_requires_source_sequence_for_current_state(self):
        ambiguous = build_snapshot([
            event("execution.completed", event_id="evt-a", task_id="task-1", status="succeeded"),
            event("task.started", event_id="evt-z", task_id="task-1", status="running"),
        ])
        self.assertEqual({"running": 0, "completed": 0, "failed": 0, "unknown": 1}, ambiguous["runtime"])
        sequenced = build_snapshot([
            event("execution.completed", event_id="evt-a", task_id="task-1", status="succeeded", metadata={"sequence": 2, "memory_recalled_count": 3}),
            event("task.started", event_id="evt-z", task_id="task-1", status="running", metadata={"sequence": 1}),
        ])
        self.assertEqual(1, sequenced["runtime"]["completed"])
        self.assertEqual(0, sequenced["runtime"]["running"])
        self.assertEqual(3, sequenced["memory"]["recalled"])

    def test_bounded_pagination_and_malformed_isolation(self):
        rows = [event("task.started", event_id=f"evt-{i}") for i in range(510)]
        rows.append({"invalid": True})
        query = PraxisDashboardQuery(Source(rows))
        page = query.events(999)
        self.assertEqual(len(page["events"]), 500); self.assertEqual(page["cursor"], "500")
        next_page = query.events(500, cursor=page["cursor"])
        self.assertEqual(len(next_page["events"]), 10)

    def test_safe_event_output_excludes_metadata(self):
        row = event("task.started", metadata={"safe_metric": 1})
        returned = PraxisDashboardQuery(Source([row])).events()
        self.assertNotIn("metadata", returned["events"][0])
