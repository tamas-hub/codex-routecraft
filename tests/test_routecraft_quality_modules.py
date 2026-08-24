from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))

import routecraft_agents_optimizer as agents
import routecraft_benchmark_lab as benchmark
from routecraft_local.context_engine import compile_context


class _Service:
    def get_project(self, *_args, **_kwargs):
        return {"name": "fixture", "description": "local", "current_objective": "test"}

    def list_memories(self, *_args, **_kwargs):
        return [{"id": "m1", "title": "Local", "body": "local memory", "importance": "high"}]


class QualityModuleTests(unittest.TestCase):
    def test_benchmark_fixture_has_measured_and_counterfactual_paths(self) -> None:
        fixture = benchmark.load_fixture(ROOT / "samples" / "benchmark-lab-fixture.json")
        counterfactual = benchmark.compare(fixture)
        self.assertFalse(counterfactual["measured"])
        self.assertIsNone(counterfactual["recommendation"]["winner"])
        measured = benchmark.compare(fixture, fixture["measured_example"])
        self.assertTrue(measured["current"]["measured"])
        self.assertTrue(measured["candidate"]["measured"])
        self.assertEqual("New routing", measured["recommendation"]["winner"])
        for field in benchmark.METRIC_FIELDS:
            self.assertIn(field, measured["current"]["metrics"])
            self.assertIn(field, measured["candidate"]["metrics"])

    def test_benchmark_d1_summary_is_aggregate_only(self) -> None:
        fixture = benchmark.load_fixture(ROOT / "samples" / "benchmark-lab-fixture.json")
        record = benchmark.to_d1_summary(
            benchmark.compare(fixture, fixture["measured_example"]),
            device_id="0123456789abcdef",
        )
        encoded = json.dumps(record)
        self.assertEqual(set(record), {
            "benchmark_run_id", "device_id", "observed_at", "comparison_kind", "status", "measured",
            "current_label", "candidate_label", "current_success_rate", "candidate_success_rate",
            "current_quality", "candidate_quality", "current_tokens", "candidate_tokens",
            "current_duration_ms", "candidate_duration_ms", "current_test_pass_rate",
            "candidate_test_pass_rate", "current_rework", "candidate_rework", "winner", "confidence",
        })
        self.assertIn("candidate_tokens", encoded)
        self.assertNotIn("content", encoded)
        self.assertNotIn("prompt", encoded)
        self.assertNotIn("absolute", encoded)

    def test_agents_analysis_duplicates_bloat_obsolete_and_apply_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "AGENTS.md"
            target.write_text("\n".join(["- Keep changes scoped", "- keep   changes scoped", "[obsolete] old rule"] + ["- filler"] * 10), encoding="utf-8")
            before = target.read_bytes()
            report = agents.analyze(target, bloat_threshold=5)
            self.assertTrue(report.bloat)
            self.assertTrue(report.duplicate_rules)
            self.assertTrue(report.obsolete_candidates)
            preview = agents.preview(target)
            self.assertTrue(preview["changed"])
            self.assertEqual(before, target.read_bytes())
            with self.assertRaises(ValueError):
                agents.apply(target, "yes")
            result = agents.apply(target, "APPLY")
            self.assertTrue(result["changed"])
            self.assertIn(agents.MARKER_START, target.read_text(encoding="utf-8"))
            self.assertIn("[obsolete] old rule", target.read_text(encoding="utf-8"))

    def test_context_engine_ranks_dedupes_and_isolates_adapter_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = compile_context(
                _Service(),
                "fixture",
                max_chars=900,
                repository_state=[
                    {"id": "r", "title": "Repo", "body": "Important repository state", "importance": "high"},
                    {"id": "r2", "title": "Duplicate", "body": "Important   repository state", "importance": "low"},
                ],
                agents_context=None,
                decision_store_results=[{"id": "d", "title": "Decision", "body": "Verified decision", "importance": "medium"}],
                adapters={"agents": lambda **_: (_ for _ in ()).throw(RuntimeError("offline"))},
            )
            self.assertIn("Important repository state", result["pack"]["content"])
            self.assertIn("Verified decision", result["pack"]["content"])
            self.assertIn("agents:RuntimeError", result["summary"]["adapter_errors"])
            self.assertLessEqual(result["pack"]["char_count"], 900)
            self.assertEqual(1, result["summary"]["source_counts"]["repository"]["included"])


if __name__ == "__main__":
    unittest.main()
