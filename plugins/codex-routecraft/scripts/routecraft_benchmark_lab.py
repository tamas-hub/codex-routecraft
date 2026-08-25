"""Deterministic local Benchmark Lab summaries.

The Benchmark Lab deliberately keeps benchmark cases and raw observations
local. This module only exposes aggregate values and a privacy-safe summary
which can be handed to the optional Control Center adapter. A result is
never labelled ``measured`` unless the corresponding side explicitly says so
or is supplied through the ``observed`` argument.

The first version exposed :func:`load_fixture` and :func:`compare` with
``baseline_score``/``candidate_score`` fields. Those keys remain part of the
compatibility surface; the richer side-by-side scorecard is additive. Missing
measurements are represented by JSON ``null``; only an explicitly supplied
numeric zero is represented by ``0``.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 2
D1_SCHEMA_VERSION = 3
METRIC_FIELDS = (
    "task_success_rate",
    "test_pass_rate",
    "quality_score",
    "tokens",
    "duration_ms",
    "retries",
    "rework",
    "reviewer_findings",
    "sample_count",
)
RATE_FIELDS = {"task_success_rate", "test_pass_rate"}
DEFAULT_LABELS = {"current": "RouteCraft OFF", "candidate": "RouteCraft ON"}
ALLOWED_MEASUREMENT = {"measured", "counterfactual"}
SAFE_LABEL = re.compile(r"^[^\W_][\w .+\-]{0,79}$", re.UNICODE)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _number(value: object, *, integer: bool = False) -> int | float:
    """Return a bounded finite non-negative scalar for internal calculations."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if not math.isfinite(number) or number < 0:
        number = 0.0
    if integer:
        return max(0, int(round(number)))
    return round(number, 6)


def _count(value: object) -> int:
    return int(_number(value, integer=True))


def _rate(value: object) -> float:
    """Normalize rates to [0, 1], accepting human-entered percentages."""
    result = float(_number(value))
    if result > 1:
        result /= 100.0
    return round(min(1.0, max(0.0, result)), 6)


def _metric_number(value: object, *, integer: bool = False) -> int | float | None:
    """Normalize an available metric without inventing a zero for missing data.

    ``None`` and invalid/non-finite/negative values mean that no usable
    observation is available.  An explicit numeric zero remains zero.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if integer:
        return max(0, int(round(number)))
    return round(number, 6)


def _metric_rate(value: object) -> float | None:
    """Normalize an available rate to [0, 1], preserving unavailable values."""
    number = _metric_number(value)
    if number is None:
        return None
    result = float(number)
    if result > 1:
        result /= 100.0
    return round(min(1.0, result), 6)


def _safe_label(value: object, default: str) -> str:
    label = str(value or default).strip()
    return label if SAFE_LABEL.fullmatch(label) else default


def _side_value(side: Mapping[str, Any] | None, key: str, default: object = None) -> object:
    if not isinstance(side, Mapping):
        return default
    if key in side:
        return side[key]
    metrics = side.get("metrics")
    if isinstance(metrics, Mapping) and key in metrics:
        return metrics[key]
    return default


_MISSING = object()


def _metric_input(side: Mapping[str, Any], aliases: tuple[str, ...]) -> tuple[bool, object]:
    """Return the first explicitly present alias, including an explicit null."""
    for alias in aliases:
        value = _side_value(side, alias, _MISSING)
        if value is not _MISSING:
            return True, value
    return False, None


def _normalize_metrics(
    side: Mapping[str, Any] | None,
    fallback: Mapping[str, Any] | None = None,
) -> dict[str, int | float | None]:
    """Normalize one side while retaining every required scorecard metric."""
    source = side if isinstance(side, Mapping) else {}
    fallback = fallback if isinstance(fallback, Mapping) else {}
    result: dict[str, int | float | None] = {}
    aliases = {
        "task_success_rate": ("task_success_rate", "success_rate", "task_success"),
        "test_pass_rate": ("test_pass_rate", "tests_pass_rate", "test_pass"),
        "quality_score": ("quality_score", "score", "quality"),
        "tokens": ("tokens", "total_tokens", "token_count"),
        "duration_ms": ("duration_ms", "duration", "elapsed_ms"),
        "retries": ("retries", "retry_count"),
        "rework": ("rework", "rework_count"),
        "reviewer_findings": ("reviewer_findings", "review_findings", "findings"),
        "sample_count": ("sample_count", "cases", "case_count", "samples"),
    }
    for field in METRIC_FIELDS:
        present, value = _metric_input(source, aliases[field])
        if not present:
            present, value = _metric_input(fallback, aliases[field])
        if not present:
            value = None
        result[field] = (
            _metric_rate(value)
            if field in RATE_FIELDS
            else _metric_number(value, integer=True)
            if field != "quality_score"
            else _metric_number(value)
        )
    return result


def _explicit_measured(side: Mapping[str, Any] | None, *, fallback: bool = False) -> bool:
    if not isinstance(side, Mapping):
        return fallback
    mode = str(side.get("measurement_mode", side.get("measurement", ""))).strip().lower()
    if mode in ALLOWED_MEASUREMENT:
        return mode == "measured"
    if "measured" in side:
        return bool(side.get("measured"))
    return fallback


def _side(raw: Mapping[str, Any] | None, *, default_label: str, fallback: Mapping[str, Any] | None = None, observed: bool = False) -> dict[str, Any]:
    side = raw if isinstance(raw, Mapping) else {}
    metrics = _normalize_metrics(side, fallback)
    measured = _explicit_measured(side, fallback=observed)
    mode = "measured" if measured else "counterfactual"
    if str(side.get("measurement_mode", side.get("measurement", ""))).strip().lower() == "counterfactual":
        measured = False
        mode = "counterfactual"
    metric_status = {
        field: "unavailable" if value is None else "measured" if measured else "estimated"
        for field, value in metrics.items()
    }
    return {
        "label": _safe_label(side.get("label", side.get("policy", side.get("mode"))), default_label),
        "measured": measured,
        "measurement_mode": mode,
        "metric_status": metric_status,
        "estimated_metrics": [field for field in METRIC_FIELDS if metric_status[field] == "estimated"],
        "unavailable_metrics": [field for field in METRIC_FIELDS if metric_status[field] == "unavailable"],
        "metrics": metrics,
        **metrics,
    }


def _candidate_input(fixture: Mapping[str, Any], observed: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], bool]:
    if isinstance(observed, Mapping):
        candidate = observed.get("candidate") if isinstance(observed.get("candidate"), Mapping) else observed
        return candidate, True
    for key in ("candidate", "new_policy", "counterfactual"):
        value = fixture.get(key)
        if isinstance(value, Mapping):
            return value, False
    return {}, False


def _recommendation(current: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Recommend only when *both* sides are explicitly measured."""
    if not bool(current.get("measured")) or not bool(candidate.get("measured")):
        return {"winner": None, "confidence": None, "basis": "insufficient_measured_inputs"}
    current_metrics = current["metrics"]
    candidate_metrics = candidate["metrics"]
    comparison_fields = ("quality_score", "task_success_rate", "test_pass_rate")
    if any(current_metrics.get(field) is None or candidate_metrics.get(field) is None for field in comparison_fields):
        return {"winner": None, "confidence": None, "basis": "insufficient_metric_inputs"}
    current_score = float(current_metrics["quality_score"])
    candidate_score = float(candidate_metrics["quality_score"])
    current_vector = (current_score, float(current_metrics["task_success_rate"]), float(current_metrics["test_pass_rate"]))
    candidate_vector = (candidate_score, float(candidate_metrics["task_success_rate"]), float(candidate_metrics["test_pass_rate"]))
    if candidate_vector == current_vector:
        return {"winner": "tie", "confidence": 0.0, "basis": "measured"}
    winner = candidate if candidate_vector > current_vector else current
    delta = abs(candidate_vector[0] - current_vector[0])
    samples = min(_count(current_metrics.get("sample_count")), _count(candidate_metrics.get("sample_count")))
    sample_confidence = min(1.0, samples / 30.0) if samples else 0.0
    effect_confidence = min(1.0, delta / 20.0)
    confidence = round(min(1.0, 0.5 * sample_confidence + 0.5 * effect_confidence), 6)
    return {"winner": winner["label"], "confidence": confidence, "basis": "measured"}


def load_fixture(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or int(value.get("schema_version", 0)) not in {1, SCHEMA_VERSION}:
        raise ValueError("unsupported benchmark lab fixture")
    if not str(value.get("fixture_id", "")).strip():
        raise ValueError("benchmark lab fixture requires fixture_id")
    return value


def compare(fixture: Mapping[str, Any], observed: Mapping[str, Any] | None = None) -> dict[str, object]:
    """Compare a current policy with a candidate using aggregate observations."""
    if not isinstance(fixture, Mapping):
        raise TypeError("fixture must be a mapping")
    observed_current = observed.get("current") if isinstance(observed, Mapping) and isinstance(observed.get("current"), Mapping) else None
    baseline_raw = observed_current if observed_current is not None else fixture.get("baseline", fixture.get("current", {}))
    candidate_raw, observed_input = _candidate_input(fixture, observed)
    baseline_measured = bool(fixture.get("baseline_measured", False))
    current = _side(
        baseline_raw if isinstance(baseline_raw, Mapping) else {},
        default_label=_safe_label(fixture.get("current_label", fixture.get("baseline_label")), DEFAULT_LABELS["current"]),
        observed=observed_current is not None or baseline_measured,
    )
    candidate = _side(
        candidate_raw,
        default_label=_safe_label(fixture.get("candidate_label", fixture.get("new_policy_label")), DEFAULT_LABELS["candidate"]),
        observed=observed_input,
    )
    recommendation = _recommendation(current, candidate)
    fixture_id = str(fixture.get("fixture_id", "fixture"))
    current_score = current["metrics"].get("quality_score")
    candidate_score = candidate["metrics"].get("quality_score")
    measurement = "measured" if current["measured"] and candidate["measured"] else "counterfactual"
    candidate_sample_count = candidate["metrics"].get("sample_count")
    current_sample_count = current["metrics"].get("sample_count")
    case_count = candidate_sample_count if candidate_sample_count is not None else current_sample_count
    return {
        "schema_version": SCHEMA_VERSION,
        "id": hashlib.sha256(("benchmark:" + fixture_id).encode("utf-8")).hexdigest()[:32],
        "timestamp": _now(),
        "measurement": measurement,
        "measurement_mode": measurement,
        "measured": measurement == "measured",
        "case_count": case_count,
        "baseline_score": current_score,
        "candidate_score": candidate_score,
        "score_delta": round(float(candidate_score) - float(current_score), 6)
        if candidate_score is not None and current_score is not None
        else None,
        "current": current,
        "candidate": candidate,
        "sides": {"current": current, "candidate": candidate},
        "recommendation": recommendation,
        "comparison_labels": [current["label"], candidate["label"]],
    }


def _percent_integer(value: object) -> int | None:
    normalized = _metric_number(value)
    if normalized is None:
        return None
    number = float(normalized)
    if number <= 1:
        number *= 100
    return min(100, max(0, int(round(number))))


def _summary_count(value: object) -> int | None:
    normalized = _metric_number(value, integer=True)
    return int(normalized) if normalized is not None else None


def _confidence_level(value: object) -> str:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return "low"
    return "high" if confidence >= 0.75 else "medium" if confidence >= 0.4 else "low"


def to_d1_summary(
    result: Mapping[str, Any],
    *,
    device_id: str = "0000000000000000",
    timestamp: str | None = None,
    comparison_kind: str = "routing",
) -> dict[str, object]:
    """Return one aggregate-only schema-v3 ``benchmark_runs`` row.

    Nullable metric columns remain null when the local result has no usable
    observation; this preserves the difference between unavailable and zero.
    """
    if not isinstance(result, Mapping):
        raise TypeError("result must be a mapping")
    current = result.get("current") if isinstance(result.get("current"), Mapping) else {}
    candidate = result.get("candidate") if isinstance(result.get("candidate"), Mapping) else {}
    current_metrics = current.get("metrics") if isinstance(current.get("metrics"), Mapping) else {}
    candidate_metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), Mapping) else {}
    measured = bool(current.get("measured")) and bool(candidate.get("measured"))
    observed_at = str(timestamp or result.get("timestamp") or _now())
    result_id = str(result.get("id", "benchmark"))
    benchmark_run_id = hashlib.sha256(
        f"benchmark-run:{device_id}:{result_id}:{observed_at}".encode("utf-8", "replace")
    ).hexdigest()[:32]
    recommendation = result.get("recommendation") if isinstance(result.get("recommendation"), Mapping) else {}
    winner_label = recommendation.get("winner") if measured else None
    if winner_label == current.get("label"):
        winner = "current"
    elif winner_label == candidate.get("label"):
        winner = "candidate"
    elif winner_label == "tie":
        winner = "tie"
    else:
        winner = "inconclusive"
    return {
        "benchmark_run_id": benchmark_run_id,
        "device_id": device_id,
        "observed_at": observed_at,
        "comparison_kind": comparison_kind if comparison_kind in {"routing", "memory", "usage", "security"} else "routing",
        "status": "passed" if measured else "unavailable",
        "measured": measured,
        "current_label": _safe_label(current.get("label"), DEFAULT_LABELS["current"]),
        "candidate_label": _safe_label(candidate.get("label"), DEFAULT_LABELS["candidate"]),
        "current_success_rate": _percent_integer(current_metrics.get("task_success_rate")),
        "candidate_success_rate": _percent_integer(candidate_metrics.get("task_success_rate")),
        "current_quality": _percent_integer(current_metrics.get("quality_score")),
        "candidate_quality": _percent_integer(candidate_metrics.get("quality_score")),
        "current_tokens": _summary_count(current_metrics.get("tokens")),
        "candidate_tokens": _summary_count(candidate_metrics.get("tokens")),
        "current_duration_ms": _summary_count(current_metrics.get("duration_ms")),
        "candidate_duration_ms": _summary_count(candidate_metrics.get("duration_ms")),
        "current_test_pass_rate": _percent_integer(current_metrics.get("test_pass_rate")),
        "candidate_test_pass_rate": _percent_integer(candidate_metrics.get("test_pass_rate")),
        "current_rework": _summary_count(current_metrics.get("rework")),
        "candidate_rework": _summary_count(candidate_metrics.get("rework")),
        "winner": winner,
        "confidence": _confidence_level(recommendation.get("confidence")) if measured else "low",
    }


benchmark_run_summary = to_d1_summary
privacy_safe_summary = to_d1_summary


__all__ = [
    "D1_SCHEMA_VERSION",
    "METRIC_FIELDS",
    "SCHEMA_VERSION",
    "benchmark_run_summary",
    "compare",
    "load_fixture",
    "privacy_safe_summary",
    "to_d1_summary",
]
