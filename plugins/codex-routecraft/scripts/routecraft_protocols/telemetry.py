"""Canonical, privacy-bounded RouteCraft routing telemetry envelope v1."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .privacy import contains_absolute_path, contains_secret_like


ROUTECRAFT_TELEMETRY_SCHEMA_VERSION = "1"
ROUTECRAFT_TELEMETRY_KEYS = (
    "schema_version", "run_id", "session_id", "requested_model", "requested_reasoning",
    "selected_model", "selected_reasoning", "actual_model", "actual_reasoning",
    "route_decision_model", "route_decision_reasoning", "decision_source", "decision_reason",
    "decision_confidence", "route_changed", "memory_recall_used", "memory_case_ids",
    "rules_applied", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
    "total_tokens", "execution_time_ms", "retry_count", "model_calls", "tool_calls", "file_reads",
    "routecraft_version", "memory_version", "collector_version", "dashboard_version", "benchmark",
)
DECISION_SOURCES = frozenset({"routecraft", "user", "codex", "fallback", "unknown"})
BENCHMARK_MODES = frozenset({"on", "off"})
BENCHMARK_RESULTS = frozenset({"passed", "failed", "unknown"})
BENCHMARK_V1_KEYS = frozenset({"schema_version", "mode", "test_result", "final_success"})
BENCHMARK_V2_KEYS = frozenset({"schema_version", "mode", "pair_id", "scope_id", "test_result", "final_success"})

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")
_MAX_COUNT = 1_000_000_000_000
_MAX_LIST_ITEMS = 32


class TelemetryValidationError(ValueError):
    """Raised when telemetry cannot be represented safely in the v1 envelope."""


def _nullable_id(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value) or contains_secret_like(value) or contains_absolute_path(value):
        raise TelemetryValidationError(f"{name} must be a bounded opaque identifier or null")
    return value


def _nullable_label(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _LABEL.fullmatch(value) or contains_secret_like(value) or contains_absolute_path(value):
        raise TelemetryValidationError(f"{name} must be a bounded label or null")
    return value


def _nullable_count(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        raise TelemetryValidationError(f"{name} must be a bounded non-negative integer or null")
    return value


def _nullable_probability(value: Any, name: str) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise TelemetryValidationError(f"{name} must be a number from zero through one or null")
    if isinstance(value, float) and value != value:
        raise TelemetryValidationError(f"{name} must be finite")
    return value


def _nullable_bool(value: Any, name: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise TelemetryValidationError(f"{name} must be true, false, or null")


def _required_id(value: Any, name: str) -> str:
    result = _nullable_id(value, name)
    if result is None:
        raise TelemetryValidationError(f"{name} must be a bounded opaque identifier")
    return result


def _nullable_label_list(value: Any, name: str, *, identifier: bool = False) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > _MAX_LIST_ITEMS:
        raise TelemetryValidationError(f"{name} must be a bounded list or null")
    if any(not isinstance(item, str) for item in value):
        raise TelemetryValidationError(f"{name} must contain bounded labels only")
    validator = _nullable_id if identifier else _nullable_label
    result = [validator(item, name) for item in value]
    if len(set(result)) != len(result):
        raise TelemetryValidationError(f"{name} must not contain duplicates")
    return result


def _validate_benchmark(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TelemetryValidationError("benchmark must be a canonical object or null")
    version = value.get("schema_version")
    if version == "1":
        if set(value) != BENCHMARK_V1_KEYS:
            raise TelemetryValidationError("benchmark v1 keys are invalid")
    elif version == "2":
        if set(value) != BENCHMARK_V2_KEYS:
            raise TelemetryValidationError("benchmark v2 keys are invalid")
    else:
        raise TelemetryValidationError("benchmark schema version is unsupported")
    if value["mode"] not in BENCHMARK_MODES:
        raise TelemetryValidationError("benchmark mode is invalid")
    result = value["test_result"]
    if result is not None and result not in BENCHMARK_RESULTS:
        raise TelemetryValidationError("benchmark test_result is invalid")
    result_value = {
        "schema_version": version,
        "mode": value["mode"],
        "test_result": result,
        "final_success": _nullable_bool(value["final_success"], "benchmark final_success"),
    }
    if version == "2":
        result_value["pair_id"] = _required_id(value["pair_id"], "benchmark pair_id")
        result_value["scope_id"] = _required_id(value["scope_id"], "benchmark scope_id")
    return result_value


def validate_routecraft_telemetry(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize the nested RouteCraft telemetry envelope.

    This intentionally does not infer routing, host, version, or benchmark facts.
    """
    if not isinstance(value, Mapping) or set(value) != set(ROUTECRAFT_TELEMETRY_KEYS):
        raise TelemetryValidationError("routecraft telemetry must have exact canonical keys")
    if value["schema_version"] != ROUTECRAFT_TELEMETRY_SCHEMA_VERSION:
        raise TelemetryValidationError("unsupported routecraft telemetry schema version")
    result: dict[str, Any] = {"schema_version": ROUTECRAFT_TELEMETRY_SCHEMA_VERSION}
    for name in ("run_id", "session_id"):
        result[name] = _nullable_id(value[name], name)
    for name in (
        "requested_model", "requested_reasoning", "selected_model", "selected_reasoning",
        "actual_model", "actual_reasoning", "route_decision_model", "route_decision_reasoning",
        "decision_reason", "routecraft_version", "memory_version", "collector_version", "dashboard_version",
    ):
        result[name] = _nullable_label(value[name], name)
    source = value["decision_source"]
    if source is None:
        source = "unknown"
    if source not in DECISION_SOURCES:
        raise TelemetryValidationError("decision_source is invalid")
    result["decision_source"] = source
    result["decision_confidence"] = _nullable_probability(value["decision_confidence"], "decision_confidence")
    result["route_changed"] = _nullable_bool(value["route_changed"], "route_changed")
    if result["route_changed"] is not None:
        requested_model, actual_model = result["requested_model"], result["actual_model"]
        if requested_model is None or actual_model is None:
            raise TelemetryValidationError("route_changed requires observed requested_model and actual_model")
        if requested_model != actual_model:
            if result["route_changed"] is not True:
                raise TelemetryValidationError("route_changed must reflect requested-to-actual model difference")
        else:
            requested_reasoning, actual_reasoning = result["requested_reasoning"], result["actual_reasoning"]
            if requested_reasoning is None or actual_reasoning is None:
                raise TelemetryValidationError("route_changed requires requested and actual reasoning when models match")
            if result["route_changed"] != (requested_reasoning != actual_reasoning):
                raise TelemetryValidationError("route_changed must reflect requested-to-actual reasoning difference")
    result["memory_recall_used"] = _nullable_bool(value["memory_recall_used"], "memory_recall_used")
    result["memory_case_ids"] = _nullable_label_list(value["memory_case_ids"], "memory_case_ids", identifier=True)
    result["rules_applied"] = _nullable_label_list(value["rules_applied"], "rules_applied")
    for name in (
        "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
        "execution_time_ms", "retry_count", "model_calls", "tool_calls", "file_reads",
    ):
        result[name] = _nullable_count(value[name], name)
    if result["cached_input_tokens"] is not None and result["input_tokens"] is not None:
        if result["cached_input_tokens"] > result["input_tokens"]:
            raise TelemetryValidationError("cached_input_tokens cannot exceed input_tokens")
    if result["total_tokens"] is not None:
        # Providers differ on whether reasoning/cached tokens are also included
        # in their aggregate.  Do not guess that accounting relationship.
        for name in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
            if result[name] is not None and result["total_tokens"] < result[name]:
                raise TelemetryValidationError("total_tokens cannot be less than an observed token measure")
    result["benchmark"] = _validate_benchmark(value["benchmark"])
    return result


def new_routecraft_telemetry(**fields: Any) -> dict[str, Any]:
    """Build a complete telemetry envelope, retaining unobserved facts as null."""
    unknown = set(fields) - (set(ROUTECRAFT_TELEMETRY_KEYS) - {"schema_version"})
    if unknown:
        raise TelemetryValidationError("unknown telemetry fields")
    value = {key: None for key in ROUTECRAFT_TELEMETRY_KEYS}
    value["schema_version"] = ROUTECRAFT_TELEMETRY_SCHEMA_VERSION
    value["decision_source"] = "unknown"
    value.update(fields)
    return validate_routecraft_telemetry(value)
