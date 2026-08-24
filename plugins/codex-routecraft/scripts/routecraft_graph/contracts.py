"""Strict typed contracts and small JSON-schema subset for Graph IR v1."""

from __future__ import annotations

import re
from typing import Any, Iterable

from .canonical import is_absolute_or_pathlike
from .constants import EVIDENCE_CLASSIFICATIONS, GATE_RESULTS


class GraphContractError(ValueError):
    pass


def require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GraphContractError(f"{name} must be an object")
    return value


def require_keys(value: dict[str, Any], keys: Iterable[str], name: str) -> None:
    missing = sorted(set(keys) - set(value))
    if missing:
        raise GraphContractError(f"{name} missing: {', '.join(missing)}")


def validate_schema(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    kind = schema.get("type")
    if kind not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        return False
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        return isinstance(properties, dict) and isinstance(required, list) and all(isinstance(k, str) and validate_schema(v) for k, v in properties.items()) and all(isinstance(k, str) for k in required)
    if kind == "array":
        return "items" not in schema or validate_schema(schema["items"])
    return True


def validate_value(value: Any, schema: dict[str, Any]) -> bool:
    if not validate_schema(schema):
        return False
    kind = schema["type"]
    if kind == "null": return value is None
    if kind == "boolean": return isinstance(value, bool)
    if kind == "integer": return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number": return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "string": return isinstance(value, str)
    if kind == "array": return isinstance(value, list) and ("items" not in schema or all(validate_value(v, schema["items"]) for v in value))
    if not isinstance(value, dict): return False
    return all(key in value for key in schema.get("required", [])) and all(key not in schema.get("properties", {}) or validate_value(item, schema["properties"][key]) for key, item in value.items())


INTENT_KEYS = {
    "request_summary", "objectives", "non_goals", "constraints", "acceptance_criteria", "risk_level",
    "external_mutations", "approval_requirements", "privacy_boundary", "budget", "deadline_if_known",
}

ATTEMPT_USAGE_KEYS = {
    "duration_ms", "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_tokens", "child_runs",
}


def validate_attempt_usage(value: Any) -> list[str]:
    """Validate nullable measured usage without turning unknown values into zero."""
    if not isinstance(value, dict):
        return ["attempt usage must be object"]
    missing, unknown = ATTEMPT_USAGE_KEYS - set(value), set(value) - ATTEMPT_USAGE_KEYS
    errors = ["attempt usage missing " + ", ".join(sorted(missing))] if missing else []
    if unknown:
        errors.append("attempt usage unknown " + ", ".join(sorted(unknown)))
    for key in ATTEMPT_USAGE_KEYS:
        item = value.get(key)
        if item is not None and (not isinstance(item, int) or isinstance(item, bool) or item < 0):
            errors.append(f"attempt usage {key} invalid")
    cached = value.get("cached_input_tokens")
    input_tokens = value.get("input_tokens")
    if cached is not None and input_tokens is not None and cached > input_tokens:
        errors.append("attempt usage cached_input_tokens exceeds input_tokens")
    return errors


def validate_operation_descriptor(value: Any) -> list[str]:
    """Validate the exact non-semantic descriptor bound to an external mutation."""
    if not isinstance(value, dict): return ["operation descriptor must be object"]
    required = {"kind", "target_scope", "parameters_hash"}
    errors: list[str] = []
    if set(value) != required: errors.append("operation descriptor keys invalid")
    for key in ("kind", "target_scope"):
        if not isinstance(value.get(key), str) or not value[key].strip() or len(value[key]) > 120: errors.append(f"operation descriptor {key} invalid")
    if not isinstance(value.get("parameters_hash"), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["parameters_hash"]): errors.append("operation descriptor parameters_hash invalid")
    return errors


def validate_intent(intent: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(intent, dict): return ["intent must be object"]
    missing = INTENT_KEYS - set(intent)
    if missing: errors.append("intent missing " + ", ".join(sorted(missing)))
    unknown = set(intent) - INTENT_KEYS
    if unknown: errors.append("intent unknown " + ", ".join(sorted(unknown)))
    for key in ("request_summary",):
        if not isinstance(intent.get(key), str) or not intent[key].strip(): errors.append(f"intent {key} invalid")
    for key in ("objectives", "non_goals", "constraints"):
        if not isinstance(intent.get(key), list) or not all(isinstance(item, str) for item in intent[key]): errors.append(f"intent {key} invalid")
    if intent.get("risk_level") not in {"low", "medium", "high", "critical"}: errors.append("intent risk_level invalid")
    criteria = intent.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria: errors.append("intent requires acceptance criteria")
    elif any(not isinstance(v, dict) or set(v) != {"criterion_id", "statement"} or not isinstance(v.get("criterion_id"), str) or not isinstance(v.get("statement"), str) for v in criteria): errors.append("intent acceptance criterion invalid")
    mutations = intent.get("external_mutations")
    if not isinstance(mutations, list) or any(not isinstance(item, dict) or set(item) != {"kind", "target_scope", "reversible"} or not isinstance(item.get("kind"), str) or not isinstance(item.get("target_scope"), str) or not isinstance(item.get("reversible"), bool) for item in mutations): errors.append("intent external_mutations invalid")
    approvals = intent.get("approval_requirements")
    if not isinstance(approvals, list) or any(not isinstance(item, dict) or set(item) != {"operation", "required"} or not isinstance(item.get("operation"), str) or not isinstance(item.get("required"), bool) for item in approvals): errors.append("intent approval requirements invalid")
    privacy = intent.get("privacy_boundary")
    if not isinstance(privacy, dict) or set(privacy) != {"local_only", "exportable"} or any(not isinstance(privacy.get(key), list) or not all(isinstance(item, str) for item in privacy[key]) for key in privacy): errors.append("intent privacy boundary invalid")
    budget = intent.get("budget")
    if not isinstance(budget, dict) or set(budget) != {"max_tokens", "max_duration_seconds", "max_child_runs"} or any(budget.get(key) is not None and (not isinstance(budget[key], int) or isinstance(budget[key], bool) or budget[key] < 0) for key in budget): errors.append("intent budget invalid")
    if intent.get("deadline_if_known") is not None and not _timestamp(intent["deadline_if_known"]): errors.append("intent deadline invalid")
    return errors


def validate_evidence(evidence: Any) -> list[str]:
    if not isinstance(evidence, dict): return ["evidence must be object"]
    required = {"evidence_id", "classification", "evidence_type", "statement", "source_kind", "artifact_hash", "result", "created_at", "node_id"}
    missing, unknown = required - set(evidence), set(evidence) - required
    errors = ["evidence missing " + ", ".join(sorted(missing))] if missing else []
    if unknown: errors.append("evidence unknown " + ", ".join(sorted(unknown)))
    if evidence.get("classification") not in EVIDENCE_CLASSIFICATIONS: errors.append("evidence classification invalid")
    if evidence.get("result") not in GATE_RESULTS: errors.append("evidence result invalid")
    for key in ("evidence_id", "evidence_type", "statement", "source_kind", "node_id"):
        if not isinstance(evidence.get(key), str) or not evidence[key]: errors.append(f"evidence {key} invalid")
    if not isinstance(evidence.get("artifact_hash"), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", evidence["artifact_hash"]): errors.append("evidence artifact_hash invalid")
    if not _timestamp(evidence.get("created_at")): errors.append("evidence created_at invalid")
    if isinstance(evidence.get("statement"), str) and is_absolute_or_pathlike(evidence["statement"]): errors.append("evidence statement must not expose path")
    return errors


def validate_verified_constraint(value: Any, evidence_by_id: dict[str, dict[str, Any]] | None = None) -> list[str]:
    if not isinstance(value, dict): return ["constraint must be object"]
    required = {"constraint_id", "scope", "statement", "evidence_refs", "confidence", "applies_to", "invalidates", "created_by"}
    errors = ["constraint missing " + ", ".join(sorted(required - set(value)))] if required - set(value) else []
    unknown = set(value) - required
    if unknown: errors.append("constraint unknown " + ", ".join(sorted(unknown)))
    if value.get("scope") not in {"graph", "node", "artifact", "interface"}: errors.append("constraint scope invalid")
    if value.get("confidence") not in {"high", "medium", "low"}: errors.append("constraint confidence invalid")
    refs = value.get("evidence_refs")
    if not isinstance(refs, list) or not refs: errors.append("constraint needs evidence refs")
    elif evidence_by_id is not None and not any(evidence_by_id.get(ref, {}).get("result") == "PASS" for ref in refs): errors.append("constraint needs PASS evidence")
    for key in ("constraint_id", "statement", "created_by"):
        if not isinstance(value.get(key), str) or not value[key]: errors.append(f"constraint {key} invalid")
    for key in ("applies_to", "invalidates"):
        if not isinstance(value.get(key), list) or not all(isinstance(item, str) for item in value[key]): errors.append(f"constraint {key} invalid")
    return errors


def _timestamp(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", value))
