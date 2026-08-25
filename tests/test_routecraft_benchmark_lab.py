from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))

import routecraft_benchmark_lab as benchmark


SUMMARY_METRICS = {
    "task_success_rate": "success_rate",
    "test_pass_rate": "test_pass_rate",
    "quality_score": "quality",
    "tokens": "tokens",
    "duration_ms": "duration_ms",
    "rework": "rework",
}


class BenchmarkNullSemanticsTests(unittest.TestCase):
    def _assert_side_aliases(self, result: dict[str, object], side_name: str, expected: object) -> None:
        side = result[side_name]
        self.assertIsInstance(side, dict)
        sides = result["sides"]
        self.assertIsInstance(sides, dict)
        side_alias = sides[side_name]
        self.assertIsInstance(side_alias, dict)
        metrics = side["metrics"]
        self.assertIsInstance(metrics, dict)
        for field in benchmark.METRIC_FIELDS:
            self.assertEqual(expected, metrics[field], field)
            self.assertEqual(metrics[field], side[field], field)
            self.assertEqual(metrics[field], side_alias[field], field)
            self.assertEqual(metrics[field], side_alias["metrics"][field], field)

    def _assert_summary_aliases(self, result: dict[str, object], expected: object) -> None:
        summary = benchmark.to_d1_summary(result)
        for side_name in ("current", "candidate"):
            side = result[side_name]
            for metric, summary_suffix in SUMMARY_METRICS.items():
                summary_key = f"{side_name}_{summary_suffix}"
                side_value = side["metrics"][metric]
                self.assertEqual(expected, side_value, metric)
                self.assertEqual(expected, summary[summary_key], summary_key)

    def test_absent_metrics_are_null_across_all_aliases_and_summary(self) -> None:
        result = benchmark.compare({"schema_version": 2, "fixture_id": "missing"})

        self.assertFalse(result["measured"])
        self.assertEqual("counterfactual", result["measurement_mode"])
        self.assertIsNone(result["case_count"])
        self.assertIsNone(result["baseline_score"])
        self.assertIsNone(result["candidate_score"])
        self.assertIsNone(result["score_delta"])
        self.assertEqual("insufficient_measured_inputs", result["recommendation"]["basis"])
        self.assertIsNone(result["recommendation"]["confidence"])
        self._assert_side_aliases(result, "current", None)
        self._assert_side_aliases(result, "candidate", None)
        self._assert_summary_aliases(result, None)
        for side_name in ("current", "candidate"):
            side = result[side_name]
            self.assertEqual([], side["estimated_metrics"])
            self.assertEqual(list(benchmark.METRIC_FIELDS), side["unavailable_metrics"])
            self.assertTrue(all(value == "unavailable" for value in side["metric_status"].values()))

    def test_counterfactual_fixture_values_are_marked_estimated_not_measured(self) -> None:
        fixture = benchmark.load_fixture(ROOT / "samples" / "benchmark-lab-fixture.json")
        result = benchmark.compare(fixture)

        for side_name in ("current", "candidate"):
            side = result[side_name]
            self.assertFalse(side["measured"])
            self.assertEqual(70.0, side["quality_score"])
            self.assertEqual(12, side["sample_count"])
            self.assertEqual("estimated", side["metric_status"]["quality_score"])
            self.assertEqual("estimated", side["metric_status"]["sample_count"])
            self.assertEqual("unavailable", side["metric_status"]["tokens"])
            self.assertIn("quality_score", side["estimated_metrics"])
            self.assertIn("tokens", side["unavailable_metrics"])
        self.assertEqual(70.0, result["baseline_score"])
        self.assertEqual(70.0, result["candidate_score"])
        self.assertEqual(0.0, result["score_delta"])
        summary = benchmark.to_d1_summary(result)
        self.assertEqual(70, summary["current_quality"])
        self.assertEqual(70, summary["candidate_quality"])
        self.assertIsNone(summary["current_tokens"])
        self.assertIsNone(summary["candidate_duration_ms"])

    def test_explicit_observed_zero_is_preserved_across_aliases_and_summary(self) -> None:
        zero_metrics = {field: 0 for field in benchmark.METRIC_FIELDS}
        observed = {
            "current": {"label": "Current", "metrics": dict(zero_metrics)},
            "candidate": {"label": "Candidate", "metrics": dict(zero_metrics)},
        }
        result = benchmark.compare({"schema_version": 2, "fixture_id": "zero"}, observed)

        self.assertTrue(result["measured"])
        self.assertEqual("measured", result["measurement_mode"])
        self.assertEqual(0, result["case_count"])
        self.assertEqual(0, result["baseline_score"])
        self.assertEqual(0, result["candidate_score"])
        self.assertEqual(0.0, result["score_delta"])
        self.assertEqual("tie", result["recommendation"]["winner"])
        self.assertEqual(0.0, result["recommendation"]["confidence"])
        self._assert_side_aliases(result, "current", 0)
        self._assert_side_aliases(result, "candidate", 0)
        self._assert_summary_aliases(result, 0)
        for side_name in ("current", "candidate"):
            side = result[side_name]
            self.assertEqual([], side["estimated_metrics"])
            self.assertEqual([], side["unavailable_metrics"])
            self.assertTrue(all(value == "measured" for value in side["metric_status"].values()))

    def test_measured_but_incomplete_inputs_do_not_produce_a_winner(self) -> None:
        observed = {
            "current": {"metrics": {"quality_score": 0}},
            "candidate": {"metrics": {"quality_score": 1}},
        }
        result = benchmark.compare({"schema_version": 2, "fixture_id": "partial"}, observed)

        self.assertTrue(result["measured"])
        self.assertEqual("insufficient_metric_inputs", result["recommendation"]["basis"])
        self.assertIsNone(result["recommendation"]["winner"])
        self.assertIsNone(result["recommendation"]["confidence"])
        self.assertIsNone(result["current"]["task_success_rate"])
        self.assertIsNone(result["candidate"]["test_pass_rate"])


if __name__ == "__main__":
    unittest.main()
