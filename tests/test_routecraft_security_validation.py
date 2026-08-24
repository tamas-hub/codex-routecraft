from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-routecraft" / "scripts"
FIXTURES = ROOT / "samples" / "security-validation-fixtures.json"
DOGFOOD_CLASSIFICATIONS = ROOT / "samples" / "security-dogfood-classifications.json"
sys.path.insert(0, str(SCRIPTS))

import routecraft_hardener as HARDENER
import routecraft_security_validation as VALIDATION
import routecraft_collector as COLLECTOR


OBSERVED_AT = "2026-08-24T00:00:00Z"
DEVICE_ID = "a" * 32
REQUIRED_RULES = {
    "SECRET-STATIC-001",
    "LOG-CREDENTIAL-001",
    "CODE-EVAL-001",
    "SHELL-UNSAFE-001",
    "SQL-INTERPOLATION-001",
    "CORS-WILDCARD-001",
    "CSP-WEAK-001",
    "TLS-VERIFY-DISABLED-001",
    "AUTH-BYPASS-001",
    "INFRA-PRIVILEGED-001",
    "INFRA-PUBLIC-INGRESS-001",
    "DEP-LOCK-MISSING-001",
    "DEP-RISKY-SCRIPT-001",
    "GHA-PR-TARGET-001",
    "GHA-WRITE-ALL-001",
    "GHA-UNPINNED-ACTION-001",
    "GHA-PERMISSIONS-UNDECLARED-001",
    "CF-SECRET-IN-VARS-001",
    "TARGET-BLANK-NOOPENER-001",
    "PUBLIC-ENV-SECRET-001",
}


class RouteCraftSecurityValidationTests(unittest.TestCase):
    def test_manifest_pairs_every_registered_rule_and_declares_runtime_header_limit(self) -> None:
        manifest = VALIDATION.load_manifest(FIXTURES)
        self.assertTrue(REQUIRED_RULES <= set(HARDENER.RULE_REGISTRY))
        coverage = {code: set() for code in HARDENER.RULE_REGISTRY}
        for case in manifest["cases"]:
            coverage[case["rule_code"]].add(case["expectation"])

        self.assertEqual(
            {code: {"vulnerable", "safe"} for code in HARDENER.RULE_REGISTRY},
            coverage,
        )
        self.assertGreaterEqual(len(manifest["cases"]), 2 * len(HARDENER.RULE_REGISTRY))
        self.assertEqual("static_local_text", manifest["manifest_scope"]["mode"])
        self.assertIn("not a security guarantee", manifest["manifest_scope"]["claim"])
        self.assertEqual("HTTP-RESPONSE-HEADERS-001", manifest["unsupported_checks"][0]["check_id"])
        self.assertIn("no-network", manifest["unsupported_checks"][0]["reason"])

    def test_known_fixture_matrix_passes_with_measured_rates_and_rule_results(self) -> None:
        report = VALIDATION.evaluate_fixture_set(FIXTURES, observed_at=OBSERVED_AT)
        supported = len(HARDENER.RULE_REGISTRY)

        self.assertEqual("PASSED", report["status"])
        self.assertEqual("low", report["confidence"])
        self.assertEqual(supported, report["supported_rules"])
        self.assertEqual(supported, report["rules_tested"])
        self.assertGreater(report["fixture_pairs"], supported)
        self.assertEqual(1.0, report["fixture_coverage"])
        self.assertEqual(report["fixture_pairs"], report["true_positive"])
        self.assertEqual(report["fixture_pairs"], report["true_negative"])
        self.assertEqual(0, report["false_positive"])
        self.assertEqual(0, report["false_negative"])
        self.assertEqual(1.0, report["detection_rate"])
        self.assertEqual(0.0, report["false_positive_rate"])
        self.assertFalse(report["security_guarantee"])
        self.assertEqual(64, len(report["ruleset_digest"]))
        self.assertNotEqual(HARDENER.RULESET_DIGEST, report["ruleset_digest"])
        self.assertIn("normalized_fixture_manifest", report["digest_basis"])
        self.assertIn("does not prove", report["clean_scan_interpretation"])
        self.assertTrue(all(item["status"] == "PASSED" for item in report["rule_results"]))
        self.assertTrue(all(item["status"] == "PASSED" for item in report["category_results"]))
        rows = VALIDATION.to_d1_rule_metrics(report, device_id=DEVICE_ID)
        self.assertEqual(supported, len(rows))
        self.assertTrue(all(COLLECTOR._valid_family("security_rule_metrics", row) for row in rows))
        gate = VALIDATION.security_gate_projection(report)
        self.assertEqual("INCONCLUSIVE", gate["gate_result"])
        self.assertFalse(gate["security_guarantee"])
        self.assertIn("does not guarantee", gate["message"])

    def test_validation_digest_changes_with_normalized_fixture_manifest(self) -> None:
        manifest = VALIDATION.load_manifest(FIXTURES)
        changed = copy.deepcopy(manifest)
        first_path = next(iter(changed["cases"][0]["files"]))
        changed["cases"][0]["files"][first_path] += "\n# digest probe"
        self.assertNotEqual(
            VALIDATION.validation_bundle_digest(manifest),
            VALIDATION.validation_bundle_digest(changed),
        )

    def test_injected_missing_and_extra_codes_measure_false_negative_and_false_positive(self) -> None:
        manifest = VALIDATION.load_manifest(FIXTURES)

        def clean_scanner(_documents: object) -> dict[str, object]:
            return {"status": "clean", "findings": []}

        missing = VALIDATION.evaluate_manifest(manifest, scanner=clean_scanner, observed_at=OBSERVED_AT)
        self.assertEqual("FAILED", missing["status"])
        self.assertEqual(missing["fixture_pairs"], missing["false_negative"])
        self.assertEqual(0, missing["false_positive"])

        def extra_scanner(_documents: object) -> dict[str, object]:
            return {"status": "findings", "findings": [{"code": "SECRET-STATIC-001"}]}

        extra = VALIDATION.evaluate_manifest(manifest, scanner=extra_scanner, observed_at=OBSERVED_AT)
        self.assertEqual("FAILED", extra["status"])
        self.assertEqual(extra["fixture_pairs"], extra["false_positive"])
        self.assertEqual(extra["fixture_pairs"] - 1, extra["false_negative"])
        self.assertEqual(extra["fixture_pairs"], extra["true_positive"] + extra["false_negative"])
        self.assertEqual(extra["fixture_pairs"], extra["true_negative"] + extra["false_positive"])
        safe_extra = next(
            item
            for item in extra["fixture_results"]
            if item["expectation"] == "safe" and item["rule_code"] != "SECRET-STATIC-001"
        )
        self.assertIn("SECRET-STATIC-001", safe_extra["unexpected_codes"])
        self.assertEqual("FP", safe_extra["classification"])
        self.assertFalse(safe_extra["passed"])
        d1 = VALIDATION.to_d1_summary(extra, device_id=DEVICE_ID)
        self.assertTrue(COLLECTOR._valid_family("security_validations", d1))

    def test_missing_rule_pair_is_insufficient_evidence(self) -> None:
        manifest = VALIDATION.load_manifest(FIXTURES)
        missing_code = next(iter(HARDENER.RULE_REGISTRY))
        incomplete = copy.deepcopy(manifest)
        incomplete["cases"] = [
            case
            for case in incomplete["cases"]
            if not (case["rule_code"] == missing_code and case["expectation"] == "safe")
        ]

        report = VALIDATION.evaluate_manifest(incomplete, observed_at=OBSERVED_AT)
        self.assertEqual("INSUFFICIENT_EVIDENCE", report["status"])
        self.assertLess(report["fixture_coverage"], 1.0)
        self.assertEqual(report["fixture_pairs"], report["true_positive"] + report["false_negative"])
        self.assertEqual(report["fixture_pairs"], report["true_negative"] + report["false_positive"])
        rule = next(item for item in report["rule_results"] if item["rule_code"] == missing_code)
        self.assertEqual("INSUFFICIENT_EVIDENCE", rule["status"])
        self.assertFalse(rule["paired_coverage"])
        d1 = VALIDATION.to_d1_summary(report, device_id=DEVICE_ID)
        self.assertTrue(COLLECTOR._valid_family("security_validations", d1))

    def test_zero_paired_evidence_keeps_measured_counts_and_null_denominator_rates(self) -> None:
        manifest = VALIDATION.load_manifest(FIXTURES)
        vulnerable_only = copy.deepcopy(manifest)
        vulnerable_only["cases"] = [case for case in vulnerable_only["cases"] if case["expectation"] == "vulnerable"]

        report = VALIDATION.evaluate_manifest(vulnerable_only, observed_at=OBSERVED_AT)
        self.assertEqual("INSUFFICIENT_EVIDENCE", report["status"])
        self.assertEqual(0, report["rules_tested"])
        self.assertEqual(0, report["fixture_pairs"])
        self.assertEqual(0.0, report["fixture_coverage"])
        self.assertEqual(0, report["true_positive"])
        self.assertEqual(0, report["true_negative"])
        self.assertEqual(0, report["false_positive"])
        self.assertEqual(0, report["false_negative"])
        self.assertIsNone(report["detection_rate"])
        self.assertIsNone(report["false_positive_rate"])

        d1 = VALIDATION.to_d1_summary(report, device_id=DEVICE_ID)
        self.assertEqual("insufficient_evidence", d1["status"])
        self.assertEqual(0.0, d1["fixture_coverage"])
        self.assertIsNone(d1["detection_rate"])
        self.assertIsNone(d1["false_positive_rate"])
        self.assertTrue(COLLECTOR._valid_family("security_validations", d1))

    def test_d1_summary_is_exact_aggregate_only_and_preserves_measured_zero(self) -> None:
        report = VALIDATION.evaluate_fixture_set(FIXTURES, observed_at=OBSERVED_AT)
        injected = {
            **report,
            "validation_id": "C:/private/repository",
            "ruleset_version": "raw-repository-name",
            "ruleset_digest": "raw-prompt-or-source",
            "repository": "private-repository",
            "path": "C:/private/path",
            "source": "raw source",
            "detail": "raw finding",
        }
        summary = VALIDATION.to_d1_summary(injected, device_id=DEVICE_ID)
        rendered = json.dumps(summary, sort_keys=True)

        self.assertEqual(VALIDATION.D1_SUMMARY_KEYS, tuple(summary))
        self.assertEqual("passed", summary["status"])
        self.assertEqual(100.0, summary["fixture_coverage"])
        self.assertEqual(100.0, summary["detection_rate"])
        self.assertEqual(0, summary["false_positive"])
        self.assertEqual(0, summary["false_negative"])
        self.assertEqual(0.0, summary["false_positive_rate"])
        for field in (
            "repositories_scanned",
            "useful_findings",
            "false_positive_findings",
            "unsupported_findings",
            "uncertain_findings",
        ):
            self.assertIsNone(summary[field])
        for forbidden in (
            "private-repository",
            "C:/private",
            "raw source",
            "raw finding",
            "raw-prompt-or-source",
            report["fixture_set_id"],
        ):
            self.assertNotIn(forbidden, rendered)
        payload = COLLECTOR.fixture_payload_v4()
        payload["security_validations"] = [summary]
        self.assertTrue(COLLECTOR.validate_v4(payload))

        incomplete_measurement = {**report, "true_positive": None}
        with self.assertRaises(ValueError):
            VALIDATION.to_d1_summary(incomplete_measurement, device_id=DEVICE_ID)

        unavailable = VALIDATION.to_d1_summary(None, device_id=DEVICE_ID, observed_at=OBSERVED_AT)
        self.assertEqual("unavailable", unavailable["status"])
        for field in (
            "rules_tested",
            "supported_rules",
            "fixture_pairs",
            "fixture_coverage",
            "true_positive",
            "true_negative",
            "false_positive",
            "false_negative",
            "detection_rate",
            "false_positive_rate",
            "repositories_scanned",
            "useful_findings",
            "false_positive_findings",
            "unsupported_findings",
            "uncertain_findings",
        ):
            self.assertIsNone(unavailable[field])
        unavailable_payload = COLLECTOR.fixture_payload_v4()
        unavailable_payload["security_validations"] = [unavailable]
        self.assertTrue(COLLECTOR.validate_v4(unavailable_payload))

        measured_dogfood = VALIDATION.with_dogfood(
            report,
            {
                "performed": True,
                "repositories_scanned": 1,
                "useful_findings": 0,
                "false_positive_findings": 0,
                "unsupported_findings": 0,
                "uncertain_findings": 0,
                "clean_scan": True,
                "security_guarantee": False,
            },
        )
        measured_summary = VALIDATION.to_d1_summary(measured_dogfood, device_id=DEVICE_ID)
        self.assertEqual(1, measured_summary["repositories_scanned"])
        self.assertEqual(0, measured_summary["useful_findings"])
        self.assertEqual(0, measured_summary["false_positive_findings"])
        self.assertEqual(0, measured_summary["unsupported_findings"])
        self.assertEqual(0, measured_summary["uncertain_findings"])

    def test_readonly_dogfood_counts_clean_scan_without_security_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "safe.py"
            source.write_text("print('safe')\n", encoding="utf-8")
            before = source.read_bytes()
            with mock.patch.object(HARDENER, "_tracked_files", return_value=None):
                dogfood = VALIDATION.dogfood_repository(root)

            self.assertEqual(before, source.read_bytes())
            self.assertTrue(dogfood["performed"])
            self.assertEqual(1, dogfood["repositories_scanned"])
            self.assertEqual(0, dogfood["useful_findings"])
            self.assertEqual(0, dogfood["false_positive_findings"])
            self.assertEqual(0, dogfood["unsupported_findings"])
            self.assertEqual(0, dogfood["uncertain_findings"])
            self.assertTrue(dogfood["clean_scan"])
            self.assertFalse(dogfood["security_guarantee"])

        with mock.patch.object(HARDENER, "scan", return_value={"status": "error"}):
            unavailable = VALIDATION.dogfood_repository("missing")
        self.assertFalse(unavailable["performed"])
        self.assertIsNone(unavailable["repositories_scanned"])
        self.assertIsNone(unavailable["uncertain_findings"])

    def test_multiple_dogfood_repositories_are_aggregated_without_names_or_paths(self) -> None:
        clean = {
            "performed": True,
            "repositories_scanned": 1,
            "useful_findings": 1,
            "false_positive_findings": 0,
            "unsupported_findings": 0,
            "uncertain_findings": 1,
            "clean_scan": False,
            "security_guarantee": False,
        }
        with mock.patch.object(VALIDATION, "dogfood_repository", side_effect=[clean, {**clean, "useful_findings": 0, "uncertain_findings": 0, "clean_scan": True}]):
            result = VALIDATION.dogfood_repositories(("first", "second"))
        self.assertEqual(2, result["repositories_scanned"])
        self.assertEqual(1, result["useful_findings"])
        self.assertEqual(1, result["uncertain_findings"])
        self.assertFalse(result["clean_scan"])
        self.assertNotIn("repository", result)
        self.assertNotIn("path", result)

    def test_d1_summary_can_be_written_atomically_to_a_caller_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "security-summary.json"
            summary = VALIDATION.to_d1_summary(
                VALIDATION.evaluate_fixture_set(FIXTURES, observed_at=OBSERVED_AT),
                device_id=DEVICE_ID,
            )
            VALIDATION.write_summary(target, summary)
            self.assertEqual(summary, json.loads(target.read_text(encoding="utf-8")))
            self.assertEqual([], list(target.parent.glob("*.tmp")))

    def test_fixture_corpus_is_inert_to_repository_scanner(self) -> None:
        raw_manifest = FIXTURES.read_text(encoding="utf-8")
        with mock.patch.object(HARDENER.subprocess, "run") as subprocess_run:
            report = HARDENER.scan_fixture_documents(
                {"samples/security-validation-fixtures.json": raw_manifest},
                observed_at=OBSERVED_AT,
            )
        subprocess_run.assert_not_called()
        self.assertEqual("clean", report["status"])
        self.assertEqual([], report["findings"])

    def test_reviewed_dogfood_false_positives_remain_explicit_and_complete(self) -> None:
        classifications = json.loads(DOGFOOD_CLASSIFICATIONS.read_text(encoding="utf-8"))
        self.assertEqual({"false_positive"}, set(classifications.values()))
        result = VALIDATION.dogfood_repository(ROOT, classifications=classifications)
        self.assertTrue(result["performed"])
        self.assertEqual(len(classifications), result["false_positive_findings"])
        self.assertEqual(0, result["useful_findings"])
        self.assertEqual(0, result["uncertain_findings"])


if __name__ == "__main__":
    unittest.main()
