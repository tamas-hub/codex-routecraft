"""Deterministic, read-only validation for RouteCraft security rules.

Fixture documents are evaluated in memory by ``routecraft_hardener``. They are
never created as executable files, followed through links, or sent over a
network. The resulting confusion matrix validates registered static signals;
a clean fixture or repository scan is not a security guarantee.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

import routecraft_hardener as hardener


SCHEMA_VERSION = 1
DEFAULT_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "samples" / "security-validation-fixtures.json"
VALID_EXPECTATIONS = {"vulnerable", "safe"}
LOCAL_STATUSES = {"PASSED", "FAILED", "INSUFFICIENT_EVIDENCE"}
D1_STATUSES = {"passed", "failed", "insufficient_evidence", "unavailable"}
DOGFOOD_CLASSIFICATIONS = {"useful", "false_positive", "unsupported", "uncertain"}
MAX_MANIFEST_BYTES = 1_000_000
MAX_CASES = 200
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$")
SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9-]{2,79}$")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

D1_SUMMARY_KEYS = (
    "validation_id",
    "device_id",
    "observed_at",
    "ruleset_version",
    "ruleset_digest",
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
    "status",
    "confidence",
    "repositories_scanned",
    "useful_findings",
    "false_positive_findings",
    "unsupported_findings",
    "uncertain_findings",
)

MANIFEST_KEYS = {"schema_version", "fixture_set_id", "manifest_scope", "unsupported_checks", "cases"}
SCOPE_KEYS = {"mode", "claim"}
UNSUPPORTED_KEYS = {"check_id", "category", "reason"}
CASE_KEYS = {"fixture_id", "rule_code", "category", "expectation", "expected_codes", "forbidden_codes", "files"}
FILE_SPEC_KEYS = {"scanner_marker", "content"}
SCANNER_FIXTURE_MARKER = "routecraft-security: scanner-test-fixture"


def _opaque(*parts: object, length: int = 32) -> str:
    encoded = "\x1f".join(str(part) for part in parts).encode("utf-8", "replace")
    return hashlib.sha256(encoded).hexdigest()[:length]


def validation_bundle_digest(fixture_set: Mapping[str, object] | None = None) -> str:
    """Fingerprint rules, matcher/validator source, and normalized fixtures."""
    digest = hashlib.sha256()
    digest.update(hardener.RULESET_DIGEST.encode("ascii"))
    for source in (Path(hardener.__file__), Path(__file__)):
        digest.update(source.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    if fixture_set is not None:
        digest.update(json.dumps(fixture_set, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii"))
    return digest.hexdigest()


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator > 0 else None


def _validated_path(value: object) -> str:
    if not isinstance(value, str) or "\\" in value or ":" in value:
        raise ValueError("fixture document paths must be relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("fixture document path escapes its in-memory root")
    return path.as_posix()


def _string_codes(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in hardener.RULE_REGISTRY for item in value):
        raise ValueError(f"{field} must contain registered rule codes")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} contains duplicate rule codes")
    return list(value)


def validate_manifest(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != MANIFEST_KEYS or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported security validation manifest")
    fixture_set_id = value.get("fixture_set_id")
    if not isinstance(fixture_set_id, str) or not SAFE_IDENTIFIER.fullmatch(fixture_set_id):
        raise ValueError("fixture_set_id is invalid")

    scope = value.get("manifest_scope")
    if not isinstance(scope, Mapping) or set(scope) != SCOPE_KEYS:
        raise ValueError("manifest_scope must explicitly describe its static claim")
    if scope.get("mode") != "static_local_text" or not isinstance(scope.get("claim"), str) or not str(scope["claim"]).strip():
        raise ValueError("manifest_scope must be static_local_text with a non-empty claim")

    unsupported = value.get("unsupported_checks")
    if not isinstance(unsupported, list):
        raise ValueError("unsupported_checks must be a list")
    normalized_unsupported: list[dict[str, str]] = []
    seen_checks: set[str] = set()
    for item in unsupported:
        if not isinstance(item, Mapping) or set(item) != UNSUPPORTED_KEYS:
            raise ValueError("unsupported check entries have an invalid shape")
        check_id = item.get("check_id")
        category = item.get("category")
        reason = item.get("reason")
        if not isinstance(check_id, str) or not SAFE_CODE.fullmatch(check_id) or check_id in seen_checks:
            raise ValueError("unsupported check id is invalid or duplicated")
        if not isinstance(category, str) or not SAFE_IDENTIFIER.fullmatch(category):
            raise ValueError("unsupported check category is invalid")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 240:
            raise ValueError("unsupported check reason is invalid")
        seen_checks.add(check_id)
        normalized_unsupported.append({"check_id": check_id, "category": category, "reason": reason})

    cases = value.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        raise ValueError("cases must be a non-empty bounded list")
    normalized_cases: list[dict[str, Any]] = []
    seen_cases: set[str] = set()
    for item in cases:
        if not isinstance(item, Mapping) or set(item) != CASE_KEYS:
            raise ValueError("fixture case has an invalid shape")
        fixture_id = item.get("fixture_id")
        rule_code = item.get("rule_code")
        category = item.get("category")
        expectation = item.get("expectation")
        if not isinstance(fixture_id, str) or not SAFE_IDENTIFIER.fullmatch(fixture_id) or fixture_id in seen_cases:
            raise ValueError("fixture_id is invalid or duplicated")
        if not isinstance(rule_code, str) or rule_code not in hardener.RULE_REGISTRY:
            raise ValueError("fixture rule_code is not registered")
        registered_category = str(hardener.RULE_REGISTRY[rule_code]["category"])
        if category != registered_category:
            raise ValueError("fixture category does not match the rule registry")
        if expectation not in VALID_EXPECTATIONS:
            raise ValueError("fixture expectation must be vulnerable or safe")
        expected = _string_codes(item.get("expected_codes"), "expected_codes")
        forbidden = _string_codes(item.get("forbidden_codes"), "forbidden_codes")
        if set(expected) & set(forbidden):
            raise ValueError("expected_codes and forbidden_codes must be disjoint")
        if expectation == "vulnerable" and rule_code not in expected:
            raise ValueError("vulnerable fixture must expect its primary rule")
        if expectation == "safe" and (expected or rule_code not in forbidden):
            raise ValueError("safe fixture must forbid its primary rule and expect no finding")
        files = item.get("files")
        if not isinstance(files, Mapping) or not files:
            raise ValueError("fixture files must be a non-empty mapping")
        normalized_files: dict[str, str] = {}
        total = 0
        for raw_path, raw_body in files.items():
            path = _validated_path(raw_path)
            if isinstance(raw_body, Mapping):
                if set(raw_body) != FILE_SPEC_KEYS or raw_body.get("scanner_marker") != SCANNER_FIXTURE_MARKER or not isinstance(raw_body.get("content"), str):
                    raise ValueError("fixture file wrapper is invalid")
                body = str(raw_body["content"])
            elif isinstance(raw_body, str):
                body = raw_body
            else:
                raise ValueError("fixture file is not text")
            if path in normalized_files:
                raise ValueError("fixture file is duplicated")
            size = len(body.encode("utf-8"))
            total += size
            if size > hardener.MAX_FILE_BYTES or total > hardener.MAX_TOTAL_BYTES:
                raise ValueError("fixture files exceed scanner bounds")
            normalized_files[path] = body
        seen_cases.add(fixture_id)
        normalized_cases.append(
            {
                "fixture_id": fixture_id,
                "rule_code": rule_code,
                "category": category,
                "expectation": expectation,
                "expected_codes": expected,
                "forbidden_codes": forbidden,
                "files": normalized_files,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "fixture_set_id": fixture_set_id,
        "manifest_scope": {"mode": scope["mode"], "claim": scope["claim"]},
        "unsupported_checks": normalized_unsupported,
        "cases": normalized_cases,
    }


def load_manifest(path: str | Path = DEFAULT_FIXTURE_PATH) -> dict[str, Any]:
    source = Path(path)
    if hardener._is_link_or_junction(source) or not source.is_file() or source.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("fixture manifest must be a bounded regular file")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("fixture manifest could not be read") from exc
    return validate_manifest(value)


def _empty_rule_result(code: str) -> dict[str, Any]:
    metadata = hardener.RULE_REGISTRY[code]
    return {
        "rule_code": code,
        "category": str(metadata["category"]),
        "vulnerable_fixtures": 0,
        "safe_fixtures": 0,
        "true_positive": 0,
        "true_negative": 0,
        "false_positive": 0,
        "false_negative": 0,
        "contract_failures": 0,
    }


def _finish_result(item: dict[str, Any]) -> dict[str, Any]:
    positive = int(item["true_positive"]) + int(item["false_negative"])
    negative = int(item["true_negative"]) + int(item["false_positive"])
    vulnerable = int(item["vulnerable_fixtures"])
    safe = int(item["safe_fixtures"])
    fixture_pairs = min(vulnerable, safe)
    fully_paired = vulnerable > 0 and vulnerable == safe
    if not fully_paired or positive != fixture_pairs or negative != fixture_pairs:
        status = "INSUFFICIENT_EVIDENCE"
    elif item["false_positive"] or item["false_negative"] or item["contract_failures"]:
        status = "FAILED"
    else:
        status = "PASSED"
    return {
        **item,
        "fixture_pairs": fixture_pairs,
        "paired_coverage": fully_paired,
        "fixture_coverage": 1.0 if fully_paired else 0.0,
        "detection_rate": _rate(int(item["true_positive"]), positive),
        "false_positive_rate": _rate(int(item["false_positive"]), negative),
        "status": status,
    }


def evaluate_manifest(
    manifest: Mapping[str, Any],
    *,
    scanner: Callable[[Mapping[str, str]], Mapping[str, object]] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    fixture_set = validate_manifest(manifest)
    scan_documents = scanner or hardener.scan_fixture_documents
    timestamp = observed_at or hardener._now()
    if not TIMESTAMP.fullmatch(timestamp):
        raise ValueError("observed_at must be a UTC timestamp with second precision")

    rule_results = {code: _empty_rule_result(code) for code in hardener.RULE_REGISTRY}
    fixture_results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    totals = {"true_positive": 0, "true_negative": 0, "false_positive": 0, "false_negative": 0}
    contract_failures = 0
    clean_fixture_scans = 0

    paired_fixture_ids: set[str] = set()
    for code in hardener.RULE_REGISTRY:
        vulnerable = [case["fixture_id"] for case in fixture_set["cases"] if case["rule_code"] == code and case["expectation"] == "vulnerable"]
        safe = [case["fixture_id"] for case in fixture_set["cases"] if case["rule_code"] == code and case["expectation"] == "safe"]
        pair_count = min(len(vulnerable), len(safe))
        paired_fixture_ids.update(vulnerable[:pair_count])
        paired_fixture_ids.update(safe[:pair_count])

    for case in fixture_set["cases"]:
        rule_result = rule_results[case["rule_code"]]
        paired_evidence = case["fixture_id"] in paired_fixture_ids
        if case["expectation"] == "vulnerable":
            rule_result["vulnerable_fixtures"] += 1
        else:
            rule_result["safe_fixtures"] += 1
        scan_error = False
        try:
            scan_report = scan_documents(case["files"])
            raw_findings = scan_report.get("findings", []) if isinstance(scan_report, Mapping) else []
            if not isinstance(raw_findings, list):
                raise ValueError("scanner findings must be a list")
            observed_codes = {
                str(finding.get("code"))
                for finding in raw_findings
                if isinstance(finding, Mapping) and isinstance(finding.get("code"), str)
            }
            if not observed_codes <= set(hardener.RULE_REGISTRY):
                raise ValueError("scanner returned an unregistered rule code")
            if str(scan_report.get("status", "error")) == "error":
                raise ValueError("scanner returned an error status")
        except Exception as exc:
            observed_codes = set()
            scan_error = True
            errors.append({"fixture_id": case["fixture_id"], "error_code": exc.__class__.__name__})

        expected = set(case["expected_codes"])
        forbidden = set(case["forbidden_codes"])
        matched_expected = expected & observed_codes
        missing_expected = expected - observed_codes
        observed_forbidden = forbidden & observed_codes
        unexpected = observed_codes - expected - forbidden
        classification: str | None = None
        contract_failure = False
        if paired_evidence and case["expectation"] == "vulnerable":
            classification = "FN" if scan_error or missing_expected else "TP"
            metric = "false_negative" if classification == "FN" else "true_positive"
            totals[metric] += 1
            rule_result[metric] += 1
            contract_failure = classification == "TP" and bool(observed_forbidden or unexpected)
        elif paired_evidence:
            classification = "FP" if scan_error or observed_codes else "TN"
            metric = "false_positive" if classification == "FP" else "true_negative"
            totals[metric] += 1
            rule_result[metric] += 1
        if contract_failure:
            contract_failures += 1
            rule_result["contract_failures"] += 1
        if not observed_codes and not scan_error:
            clean_fixture_scans += 1
        case_passed = not scan_error and not missing_expected and not observed_forbidden and not unexpected
        fixture_results.append(
            {
                "fixture_id": case["fixture_id"],
                "rule_code": case["rule_code"],
                "category": case["category"],
                "expectation": case["expectation"],
                "expected_codes": sorted(expected),
                "forbidden_codes": sorted(forbidden),
                "observed_codes": sorted(observed_codes),
                "matched_expected_codes": sorted(matched_expected),
                "missing_expected_codes": sorted(missing_expected),
                "observed_forbidden_codes": sorted(observed_forbidden),
                "unexpected_codes": sorted(unexpected),
                "paired_evidence": paired_evidence,
                "classification": classification,
                "contract_failure": contract_failure,
                "scan_error": scan_error,
                "passed": case_passed,
            }
        )

    finished_rules = [_finish_result(rule_results[code]) for code in sorted(rule_results)]
    supported_rules = len(finished_rules)
    covered_rules = sum(bool(item["paired_coverage"]) for item in finished_rules)
    rules_tested = covered_rules
    fixture_pairs = sum(int(item["fixture_pairs"]) for item in finished_rules)
    fixture_coverage = _rate(covered_rules, supported_rules)

    category_results: list[dict[str, Any]] = []
    categories = sorted({str(metadata["category"]) for metadata in hardener.RULE_REGISTRY.values()})
    for category in categories:
        members = [item for item in finished_rules if item["category"] == category]
        aggregate = {
            "category": category,
            "supported_rules": len(members),
            "covered_rules": sum(bool(item["paired_coverage"]) for item in members),
            "true_positive": sum(int(item["true_positive"]) for item in members),
            "true_negative": sum(int(item["true_negative"]) for item in members),
            "false_positive": sum(int(item["false_positive"]) for item in members),
            "false_negative": sum(int(item["false_negative"]) for item in members),
            "contract_failures": sum(int(item["contract_failures"]) for item in members),
            "vulnerable_fixtures": sum(int(item["vulnerable_fixtures"]) for item in members),
            "safe_fixtures": sum(int(item["safe_fixtures"]) for item in members),
            "fixture_pairs": sum(int(item["fixture_pairs"]) for item in members),
        }
        category_coverage = _rate(aggregate["covered_rules"], aggregate["supported_rules"])
        positive = aggregate["true_positive"] + aggregate["false_negative"]
        negative = aggregate["true_negative"] + aggregate["false_positive"]
        if category_coverage != 1.0 or positive == 0 or negative == 0:
            category_status = "INSUFFICIENT_EVIDENCE"
        elif aggregate["false_positive"] or aggregate["false_negative"] or aggregate["contract_failures"]:
            category_status = "FAILED"
        else:
            category_status = "PASSED"
        category_results.append(
            {
                **aggregate,
                "fixture_coverage": category_coverage,
                "detection_rate": _rate(aggregate["true_positive"], positive),
                "false_positive_rate": _rate(aggregate["false_positive"], negative),
                "status": category_status,
            }
        )

    positive = totals["true_positive"] + totals["false_negative"]
    negative = totals["true_negative"] + totals["false_positive"]
    sufficient = fixture_coverage == 1.0 and positive > 0 and negative > 0 and not errors
    if not sufficient:
        status = "INSUFFICIENT_EVIDENCE"
    elif totals["false_positive"] or totals["false_negative"] or contract_failures:
        status = "FAILED"
    else:
        status = "PASSED"
    # One vulnerable/safe pair per rule proves deterministic regression
    # coverage, not broad detection performance.  Small samples stay LOW.
    confidence = "high" if sufficient and fixture_pairs >= 100 else "medium" if sufficient and fixture_pairs >= 60 else "low"
    bundle_digest = validation_bundle_digest(fixture_set)
    validation_id = _opaque(fixture_set["fixture_set_id"], bundle_digest, timestamp)
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_id": validation_id,
        "observed_at": timestamp,
        "fixture_set_id": fixture_set["fixture_set_id"],
        "ruleset_version": hardener.RULESET_VERSION,
        "ruleset_digest": bundle_digest,
        "rule_registry_digest": hardener.RULESET_DIGEST,
        "digest_basis": "rule_registry+matcher_source+validator_source+normalized_fixture_manifest",
        "metric_basis": "paired_fixture_classifications",
        "status": status,
        "confidence": confidence,
        "supported_rules": supported_rules,
        "rules_tested": rules_tested,
        "covered_rules": covered_rules,
        "fixture_pairs": fixture_pairs,
        "fixture_coverage": fixture_coverage,
        **totals,
        "contract_failures": contract_failures,
        "detection_rate": _rate(totals["true_positive"], positive),
        "false_positive_rate": _rate(totals["false_positive"], negative),
        "clean_fixture_scans": clean_fixture_scans,
        "security_guarantee": False,
        "clean_scan_interpretation": "No registered signal was observed; this does not prove the fixture or repository is secure.",
        "rule_results": finished_rules,
        "category_results": category_results,
        "fixture_results": fixture_results,
        "unsupported_checks": fixture_set["unsupported_checks"],
        "errors": errors,
        "dogfood": None,
    }


def dogfood_repository(
    source_root: str | Path,
    *,
    classifications: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Read a repository with the bounded hardener and return counts only.

    Classifications are keyed by the hardener's local finding fingerprints.
    Unclassified registered findings remain ``uncertain``; scanner-boundary
    diagnostics are ``unsupported``. No repository name, path, or finding
    detail is returned from this interface.
    """
    labels = dict(classifications or {})
    if any(not isinstance(key, str) or value not in DOGFOOD_CLASSIFICATIONS for key, value in labels.items()):
        raise ValueError("dogfood classifications are invalid")
    report = hardener.scan(source_root)
    if report.get("status") == "error":
        return {
            "performed": False,
            "repositories_scanned": None,
            "useful_findings": None,
            "false_positive_findings": None,
            "unsupported_findings": None,
            "uncertain_findings": None,
            "clean_scan": None,
            "security_guarantee": False,
        }
    counts = {label: 0 for label in DOGFOOD_CLASSIFICATIONS}
    severities = {label: 0 for label in ("critical", "high", "medium", "low", "info")}
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        fingerprint = hardener._fingerprint(finding)
        code = str(finding.get("code", ""))
        default = "unsupported" if code in hardener.DIAGNOSTIC_REGISTRY else "uncertain"
        counts[labels.get(fingerprint, default)] += 1
        severity = str(finding.get("severity", "info")).lower()
        severities[severity if severity in severities else "info"] += 1
    return {
        "performed": True,
        "repositories_scanned": 1,
        "useful_findings": counts["useful"],
        "false_positive_findings": counts["false_positive"],
        "unsupported_findings": counts["unsupported"],
        "uncertain_findings": counts["uncertain"],
        **{f"{key}_findings": value for key, value in severities.items()},
        "clean_scan": len(findings) == 0,
        "security_guarantee": False,
    }


def dogfood_repositories(
    source_roots: Sequence[str | Path],
    *,
    classifications: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Aggregate multiple read-only repository scans without repository labels."""
    roots = list(source_roots)
    if not roots:
        raise ValueError("dogfood repository list must not be empty")
    observations = [dogfood_repository(root, classifications=classifications) for root in roots]
    if any(item.get("performed") is not True for item in observations):
        return {
            "performed": False,
            "repositories_scanned": None,
            "useful_findings": None,
            "false_positive_findings": None,
            "unsupported_findings": None,
            "uncertain_findings": None,
            "clean_scan": None,
            "security_guarantee": False,
        }
    count_keys = (
        "repositories_scanned", "useful_findings", "false_positive_findings",
        "unsupported_findings", "uncertain_findings", "critical_findings",
        "high_findings", "medium_findings", "low_findings", "info_findings",
    )

    def aggregate_count(key: str) -> int | None:
        """Sum a measured count, preserving an unavailable field as null.

        Older or mocked scan observations may not expose severity counters.
        Treating that absence as zero would make a target gate look cleaner
        than its evidence allows, so the aggregate remains explicitly
        unavailable while the independently measured counters stay usable.
        """
        values = [item.get(key) for item in observations]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            return None
        return sum(values)

    return {
        "performed": True,
        **{key: aggregate_count(key) for key in count_keys},
        "clean_scan": all(item.get("clean_scan") is True for item in observations),
        "security_guarantee": False,
    }


def with_dogfood(validation: Mapping[str, Any], dogfood: Mapping[str, object]) -> dict[str, Any]:
    if validation.get("status") not in LOCAL_STATUSES or not isinstance(dogfood.get("performed"), bool):
        raise ValueError("validation or dogfood result is invalid")
    return {**dict(validation), "dogfood": dict(dogfood)}


def evaluate_fixture_set(
    path: str | Path = DEFAULT_FIXTURE_PATH,
    *,
    dogfood_root: str | Path | None = None,
    dogfood_roots: Sequence[str | Path] | None = None,
    dogfood_classifications: Mapping[str, str] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    if dogfood_root is not None and dogfood_roots is not None:
        raise ValueError("use dogfood_root or dogfood_roots, not both")
    result = evaluate_manifest(load_manifest(path), observed_at=observed_at)
    roots = list(dogfood_roots or ())
    if dogfood_root is not None:
        roots.append(dogfood_root)
    return with_dogfood(result, dogfood_repositories(roots, classifications=dogfood_classifications)) if roots else result


def _optional_count(report: Mapping[str, Any] | None, key: str) -> int | None:
    if report is None or key not in report:
        return None
    value = report[key]
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _optional_percentage(report: Mapping[str, Any] | None, key: str) -> float | None:
    if report is None or key not in report:
        return None
    value = report[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return round(number * 100, 6) if math.isfinite(number) and 0 <= number <= 1 else None


def to_d1_summary(
    report: Mapping[str, Any] | None,
    *,
    device_id: str = "00000000000000000000000000000000",
    observed_at: str | None = None,
) -> dict[str, object]:
    """Return the exact aggregate-only Control Center v4 validation row."""
    if not isinstance(device_id, str) or not re.fullmatch(r"[a-f0-9]{16,64}", device_id):
        raise ValueError("device_id must be an opaque lowercase hexadecimal id")
    timestamp = observed_at or (str(report.get("observed_at")) if report is not None else hardener._now())
    if not TIMESTAMP.fullmatch(timestamp):
        raise ValueError("observed_at must be a UTC timestamp with second precision")
    local_status = str(report.get("status")) if report is not None else ""
    status = {
        "PASSED": "passed",
        "FAILED": "failed",
        "INSUFFICIENT_EVIDENCE": "insufficient_evidence",
    }.get(local_status, "unavailable")
    confidence = str(report.get("confidence")) if report is not None else "low"
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    available = report is not None and status != "unavailable"
    measurements = report if available else None
    counts = {
        key: _optional_count(measurements, key)
        for key in ("rules_tested", "supported_rules", "fixture_pairs", "true_positive", "true_negative", "false_positive", "false_negative")
    }
    percentages = {
        key: _optional_percentage(measurements, key)
        for key in ("fixture_coverage", "detection_rate", "false_positive_rate")
    }
    if available:
        if any(value is None for value in counts.values()) or percentages["fixture_coverage"] is None:
            raise ValueError("non-unavailable validation requires complete measured metrics")
        tested = int(counts["rules_tested"])
        supported = int(counts["supported_rules"])
        pairs = int(counts["fixture_pairs"])
        true_positive = int(counts["true_positive"])
        true_negative = int(counts["true_negative"])
        false_positive = int(counts["false_positive"])
        false_negative = int(counts["false_negative"])
        if supported <= 0 or tested > supported:
            raise ValueError("non-unavailable validation has invalid evidence denominators")
        if true_positive + false_negative != pairs or true_negative + false_positive != pairs:
            raise ValueError("validation confusion matrix does not match fixture_pairs")
        if abs(float(percentages["fixture_coverage"]) - tested * 100 / supported) > 0.01:
            raise ValueError("validation rates do not match their measured denominators")
        if pairs:
            if percentages["detection_rate"] is None or percentages["false_positive_rate"] is None:
                raise ValueError("paired validation requires measured detection rates")
            if abs(float(percentages["detection_rate"]) - true_positive * 100 / pairs) > 0.01 or abs(float(percentages["false_positive_rate"]) - false_positive * 100 / pairs) > 0.01:
                raise ValueError("validation rates do not match their measured denominators")
        elif percentages["detection_rate"] is not None or percentages["false_positive_rate"] is not None:
            raise ValueError("zero-pair validation rates must be null")

    dogfood = report.get("dogfood") if available and isinstance(report.get("dogfood"), Mapping) else None
    dogfood_performed = bool(dogfood and dogfood.get("performed") is True)
    dogfood_counts = {
        key: _optional_count(dogfood if dogfood_performed else None, key)
        for key in ("repositories_scanned", "useful_findings", "false_positive_findings", "unsupported_findings", "uncertain_findings")
    }
    if dogfood_performed and any(value is None for value in dogfood_counts.values()):
        raise ValueError("performed dogfood requires complete measured counts")
    raw_validation_id = report.get("validation_id") if available else None
    raw_ruleset_digest = report.get("ruleset_digest") if available else None
    ruleset_digest = (
        str(raw_ruleset_digest)
        if isinstance(raw_ruleset_digest, str) and re.fullmatch(r"[a-f0-9]{64}", raw_ruleset_digest)
        else validation_bundle_digest()
    )
    validation_id = (
        raw_validation_id
        if isinstance(raw_validation_id, str) and re.fullmatch(r"[a-f0-9]{16,64}", raw_validation_id)
        else _opaque("security-validation", device_id, timestamp, ruleset_digest)
    )
    summary = {
        "validation_id": validation_id,
        "device_id": device_id,
        "observed_at": timestamp,
        "ruleset_version": hardener.RULESET_VERSION,
        "ruleset_digest": ruleset_digest,
        "rules_tested": counts["rules_tested"],
        "supported_rules": counts["supported_rules"],
        "fixture_pairs": counts["fixture_pairs"],
        "fixture_coverage": percentages["fixture_coverage"],
        "true_positive": counts["true_positive"],
        "true_negative": counts["true_negative"],
        "false_positive": counts["false_positive"],
        "false_negative": counts["false_negative"],
        "detection_rate": percentages["detection_rate"],
        "false_positive_rate": percentages["false_positive_rate"],
        "status": status,
        "confidence": confidence,
        "repositories_scanned": dogfood_counts["repositories_scanned"],
        "useful_findings": dogfood_counts["useful_findings"],
        "false_positive_findings": dogfood_counts["false_positive_findings"],
        "unsupported_findings": dogfood_counts["unsupported_findings"],
        "uncertain_findings": dogfood_counts["uncertain_findings"],
    }
    if tuple(summary) != D1_SUMMARY_KEYS or summary["status"] not in D1_STATUSES:
        raise ValueError("invalid Control Center v4 security validation summary")
    return summary


def to_d1_rule_metrics(report: Mapping[str, object] | None, *, device_id: str, observed_at: str | None = None) -> list[dict[str, object]]:
    """Return per-rule aggregate-only v4 evidence; no findings or source text."""
    if not re.fullmatch(r"[a-f0-9]{16,64}", device_id):
        raise ValueError("device_id must be an opaque lowercase hexadecimal id")
    if report is None:
        return []
    timestamp = observed_at or str(report.get("observed_at") or utc_now())
    ruleset = hardener.RULESET_VERSION
    confidence = str(report.get("confidence") or "low")
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    status = {"PASSED": "passed", "FAILED": "failed", "INSUFFICIENT_EVIDENCE": "insufficient_evidence"}.get(str(report.get("status") or ""), "unavailable")
    rows: list[dict[str, object]] = []
    for item in report.get("rule_results", []):
        if not isinstance(item, Mapping):
            continue
        code = item.get("rule_code")
        if not isinstance(code, str) or not SAFE_CODE.fullmatch(code):
            continue
        tp, tn = int(item.get("true_positive", 0)), int(item.get("true_negative", 0))
        fp, fn = int(item.get("false_positive", 0)), int(item.get("false_negative", 0))
        positive, negative = tp + fn, tn + fp
        rows.append({
            "security_rule_metric_id": _opaque("security-rule", device_id, timestamp, code),
            "device_id": device_id, "observed_at": timestamp, "ruleset_version": ruleset, "rule_id": code,
            "true_positive": tp, "true_negative": tn, "false_positive": fp, "false_negative": fn,
            "fixture_coverage": 100.0 if item.get("paired_coverage") is True else 0.0,
            "detection_rate": round(tp * 100 / positive, 2) if positive else None,
            "false_positive_rate": round(fp * 100 / negative, 2) if negative else None,
            "confidence": confidence, "status": status,
        })
    return rows


def security_gate_projection(report: Mapping[str, object] | None, target_scan: Mapping[str, object] | None = None) -> dict[str, object]:
    """Combine rule-engine validation and a target scan; neither substitutes for the other."""
    if report is None:
        return {"gate_result": "INCONCLUSIVE", "evidence_available": False, "security_guarantee": False, "message": "No security validation evidence is available. CLEAN does not mean SAFE."}
    status = str(report.get("status") or "")
    if status == "FAILED":
        return {"gate_result": "FAIL", "evidence_available": True, "security_guarantee": False, "rules_tested": report.get("rules_tested"), "fixture_coverage": report.get("fixture_coverage"), "message": "Security rule validation failed."}
    if status != "PASSED" or report.get("confidence") not in {"medium", "high"}:
        return {"gate_result": "INCONCLUSIVE", "evidence_available": True, "security_guarantee": False, "rules_tested": report.get("rules_tested"), "fixture_coverage": report.get("fixture_coverage"), "message": "Security rule evidence is insufficient for a target gate. CLEAN does not guarantee repository safety."}
    scan = target_scan if target_scan is not None else report.get("dogfood")
    if not isinstance(scan, Mapping) or scan.get("performed") is not True:
        return {"gate_result": "INCONCLUSIVE", "evidence_available": True, "security_guarantee": False, "rules_tested": report.get("rules_tested"), "fixture_coverage": report.get("fixture_coverage"), "message": "Target repository scan is unavailable."}
    critical = scan.get("critical_findings")
    high = scan.get("high_findings")
    if not isinstance(critical, int) or isinstance(critical, bool) or not isinstance(high, int) or isinstance(high, bool):
        return {"gate_result": "INCONCLUSIVE", "evidence_available": True, "security_guarantee": False, "message": "Target repository severity evidence is incomplete."}
    if critical > 0 or high > 0:
        return {"gate_result": "FAIL", "evidence_available": True, "security_guarantee": False, "critical_findings": critical, "high_findings": high, "message": "High-severity findings were detected by enabled rules."}
    if scan.get("clean_scan") is not True:
        return {"gate_result": "INCONCLUSIVE", "evidence_available": True, "security_guarantee": False, "critical_findings": critical, "high_findings": high, "message": "Findings require review before acceptance."}
    return {"gate_result": "PASS", "evidence_available": True, "security_guarantee": False, "critical_findings": 0, "high_findings": 0, "rules_tested": report.get("rules_tested"), "fixture_coverage": report.get("fixture_coverage"), "message": "No findings detected by enabled rules. This does not guarantee repository safety."}


def write_summary(path: Path, value: Mapping[str, object]) -> None:
    """Atomically write only the validated aggregate to a caller-selected path."""
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(dict(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only RouteCraft security rule validation")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--dogfood-root", type=Path, action="append", default=[], help="Read-only repository root; repeat to aggregate multiple repositories")
    parser.add_argument("--dogfood-classifications", type=Path, help="Optional local JSON map of finding fingerprint to reviewed classification")
    parser.add_argument("--d1-summary", action="store_true")
    parser.add_argument("--device-id", default="00000000000000000000000000000000")
    parser.add_argument("--output", type=Path, help="Atomically save the selected output; no raw findings are written with --d1-summary")
    args = parser.parse_args(argv)
    try:
        classifications = None
        if args.dogfood_classifications:
            loaded = json.loads(args.dogfood_classifications.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("dogfood classifications must be a JSON object")
            classifications = loaded
        result = evaluate_fixture_set(
            args.fixtures,
            dogfood_roots=args.dogfood_root,
            dogfood_classifications=classifications,
        )
        output: Mapping[str, object] = to_d1_summary(result, device_id=args.device_id) if args.d1_summary else result
        if args.output:
            write_summary(args.output, output)
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["status"] == "PASSED" else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if args.d1_summary:
            print(json.dumps(to_d1_summary(None, device_id=args.device_id), separators=(",", ":")))
        else:
            print(json.dumps({"ok": False, "status": "INSUFFICIENT_EVIDENCE", "error_code": exc.__class__.__name__}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
