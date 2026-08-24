"""Deterministic, privacy-safe RouteCraft execution graph core.

This module intentionally has no model, network, plugin, or persistence
dependencies.  It is the small state machine that a routing adapter can use
to validate a graph, select independent work, account for bounded retries,
and publish aggregate observations.  All state-transition helpers return a
deep copy of their input state; callers may safely retain the previous
snapshot for audit or comparison.

The public representation is JSON-compatible dictionaries and lists.  The
``Unit`` dataclass is provided as a convenience for callers that prefer a
typed builder, but graph state remains an ordinary dictionary so it can be
serialized without a custom encoder.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, MutableMapping, Sequence


GRAPH_SCHEMA_VERSION = 1
DEFAULT_MODE = "observe"
GRAPH_MODES = ("off", "observe", "enforce")
MODES = GRAPH_MODES
GRAPH_PRIMITIVES = (
    "split",
    "fan_out",
    "sequence",
    "merge",
    "gate",
    "send_back",
    "accept",
)
PRIMITIVES = GRAPH_PRIMITIVES

UNIT_STATUSES = (
    "pending",
    "ready",
    "running",
    "produced",
    "accepted",
    "failed",
    "reopened",
    "invalidated",
    "blocked",
)
GRAPH_STATUSES = (
    "pending",
    "running",
    "retry_pending",
    "accepted",
    "failed",
    "convergence_failed",
    "stalled",
)
ATTEMPT_STAGES = ("produce", "check", "correct")
GRAPH_RUN_STATUSES = ("planned", "running", "accepted", "failed", "convergence_failed", "fallback")
GATE_STATUSES = ("passed", "failed", "not_run")
CONVERGENCE_REASONS = ("none", "max_attempts", "max_steps", "max_child_runs", "max_wall_time", "retry_budget", "invalid_graph")
HARDENING_GATE_A_REQUIRED_CHECKS = frozenset((
    "real_model_benchmark_e2e",
    "security_rule_fixture_validation",
    "legacy_replacement_health",
    "runtime_regression",
    "control_center_regression",
    "memory_regression",
    "collector_regression",
))
GRAPH_RUN_SUMMARY_FIELDS = (
    "graph_run_id",
    "device_id",
    "observed_at",
    "event_classification",
    "graph_schema_version",
    "mode",
    "status",
    "graph_revision_count",
    "node_count",
    "edge_count",
    "parallel_width",
    "critical_path_length",
    "attempt_count",
    "retry_count",
    "send_back_count",
    "accepted_count",
    "frozen_count",
    "failed_count",
    "invalidated_count",
    "constraint_count",
    "checkpoint_count",
    "gate_pass_count",
    "gate_fail_count",
    "gate_inconclusive_count",
    "duration_ms",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
)

_REQUIRED_UNIT_FIELDS = (
    "unit_id",
    "objective",
    "dependencies",
    "ownership",
    "input_schema",
    "output_schema",
    "verification",
    "retry_policy",
    "lane",
    "risk",
    "status",
)
_REQUIRED_GRAPH_FIELDS = (
    "graph_id",
    "version",
    "task_class",
    "nodes",
    "edges",
    "status",
    "constraints",
    "gate_results",
    "accepted_outputs",
    "failed_outputs",
    "limits",
    "counters",
    "timestamps",
)

# Bounds are deliberately finite.  They protect the deterministic core from
# accidentally becoming a prompt/blob transport while leaving useful room for
# normal task schemas and error details.
MAX_NODES = 512
MAX_EDGES = 2048
MAX_STRING_LENGTH = 8192
MAX_REASON_LENGTH = 256
MAX_JSON_DEPTH = 12
MAX_COLLECTION_ITEMS = 2048
MAX_TOTAL_JSON_BYTES = 2_000_000
MAX_ATTEMPTS = 10_000
DEFAULT_LIMITS = {
    "max_attempt_per_unit": 3,
    "max_graph_steps": 1000,
    "max_total_child_runs": 1000,
    "max_wall_time_ms": None,
    "max_parallelism": 3,
}


class GraphError(ValueError):
    """Base exception for invalid graph operations."""


class GraphValidationError(GraphError):
    """Raised when a unit, graph, schema, or state violates the contract."""


class GraphConvergenceError(GraphError):
    """Raised by strict callers when a convergence limit has been exceeded."""


def _raise(message: str) -> None:
    raise GraphValidationError(message)


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _forbidden_key(key: str) -> bool:
    """Return whether *key* is an obvious raw/secrets transport field.

    Ordinary graph names such as ``objective`` and ``output_schema`` are
    intentionally allowed.  The check targets fields that commonly carry
    source material, prompts, packets, credentials, or filesystem paths.
    """

    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if normalized in {
        "prompt",
        "prompts",
        "source",
        "sources",
        "content",
        "contents",
        "packet",
        "packets",
        "raw_packet",
        "raw_request",
        "raw_response",
        "request_body",
        "response_body",
        "authorization",
        "cookie",
        "cookies",
        "password",
        "passwd",
        "secret",
        "secrets",
        "credential",
        "credentials",
        "api_key",
        "access_token",
        "refresh_token",
        "private_key",
        "client_secret",
        "absolute_path",
        "file_path",
        "filepath",
    }:
        return True
    if normalized.endswith("_path") or normalized.endswith("_prompt"):
        return True
    return False


def _looks_absolute_path(value: str) -> bool:
    # Windows drive paths, UNC paths, and POSIX absolute paths are rejected.
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", value)
        or value.startswith("\\\\")
        or value.startswith("/")
        or value.startswith("\\")
    )


def _validate_json_safe(
    value: Any,
    *,
    path: str = "$",
    depth: int = 0,
    total: list[int] | None = None,
) -> None:
    """Validate bounded JSON-safe data and reject raw/private fields."""

    if total is None:
        total = [0]
    if depth > MAX_JSON_DEPTH:
        _raise(f"{path}: maximum JSON depth exceeded")
    if value is None or isinstance(value, (bool, int)):
        total[0] += 1
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _raise(f"{path}: non-finite number")
        total[0] += 1
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            _raise(f"{path}: string is too long")
        if "\x00" in value:
            _raise(f"{path}: NUL is not JSON-safe")
        total[0] += len(value)
        if total[0] > MAX_TOTAL_JSON_BYTES:
            _raise("JSON-safe value exceeds bounded size")
        if _looks_absolute_path(value):
            _raise(f"{path}: absolute paths are not accepted")
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            _raise(f"{path}: too many object fields")
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                _raise(f"{path}: object keys must be bounded strings")
            if _forbidden_key(key):
                _raise(f"{path}.{key}: private/raw field is not accepted")
            _validate_json_safe(child, path=f"{path}.{key}", depth=depth + 1, total=total)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_COLLECTION_ITEMS:
            _raise(f"{path}: too many array items")
        for index, child in enumerate(value):
            _validate_json_safe(child, path=f"{path}[{index}]", depth=depth + 1, total=total)
        return
    _raise(f"{path}: value is not JSON-safe")


def canonical_json(value: Any) -> str:
    """Return deterministic compact JSON after privacy/bounds validation."""

    _validate_json_safe(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any, namespace: str = "routecraft") -> str:
    """Return a stable SHA-256 digest for bounded JSON-safe data."""

    payload = (namespace + "\0" + canonical_json(value)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    """Return an opaque, deterministic 32-hex identifier."""

    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", prefix):
        _raise("identifier prefix must be a short label")
    return stable_hash(list(parts), namespace=f"routecraft:{prefix}")[:32]


def opaque_id(namespace: str, value: Any) -> str:
    """Alias used by privacy-facing summaries and exports."""

    return stable_hash(value, namespace=f"routecraft:{namespace}")[:32]


def stable_sort(values: Iterable[Any], key: Any = None) -> list[Any]:
    """Return a deterministic stable ordering without mutating *values*."""

    if key is None:
        return sorted(list(values), key=canonical_json)
    return sorted(list(values), key=lambda value: canonical_json(key(value)))


def _bounded_label(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _raise(f"{field_name} must be a non-empty string")
    value = value.strip()
    if len(value) > 256 or "\x00" in value or _looks_absolute_path(value):
        _raise(f"{field_name} is not bounded")
    return value


def _safe_enum(value: Any, *, fallback: str = "unknown") -> str:
    if not isinstance(value, str):
        return fallback
    text = value.strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,63}", text):
        return text
    return fallback


@dataclass(frozen=True)
class Unit:
    """Convenience typed representation of a graph unit."""

    unit_id: str
    objective: str
    dependencies: tuple[str, ...] = ()
    ownership: Any = "default"
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    verification: Mapping[str, Any] = field(default_factory=dict)
    retry_policy: Mapping[str, Any] = field(default_factory=dict)
    lane: str = "default"
    risk: str = "low"
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "objective": self.objective,
            "dependencies": list(self.dependencies),
            "ownership": copy.deepcopy(self.ownership),
            "input_schema": copy.deepcopy(dict(self.input_schema)),
            "output_schema": copy.deepcopy(dict(self.output_schema)),
            "verification": copy.deepcopy(dict(self.verification)),
            "retry_policy": copy.deepcopy(dict(self.retry_policy)),
            "lane": self.lane,
            "risk": self.risk,
            "status": self.status,
            "attempts": {stage: 0 for stage in ATTEMPT_STAGES},
        }


def make_unit(
    unit_id: str,
    objective: str,
    *,
    dependencies: Iterable[str] = (),
    ownership: Any = "default",
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    verification: Mapping[str, Any] | None = None,
    retry_policy: Mapping[str, Any] | None = None,
    lane: str = "default",
    risk: str = "low",
    status: str = "pending",
) -> dict[str, Any]:
    """Build and strictly validate one unit contract."""

    unit = Unit(
        unit_id=unit_id,
        objective=objective,
        dependencies=tuple(dependencies),
        ownership=ownership,
        input_schema={} if input_schema is None else input_schema,
        output_schema={} if output_schema is None else output_schema,
        verification={} if verification is None else verification,
        retry_policy={} if retry_policy is None else retry_policy,
        lane=lane,
        risk=risk,
        status=status,
    ).to_dict()
    validate_unit_or_raise(unit)
    return unit


def _unit_dict(unit: Any) -> dict[str, Any]:
    if isinstance(unit, Unit):
        return unit.to_dict()
    if not isinstance(unit, Mapping):
        _raise("unit must be a mapping or Unit")
    return copy.deepcopy(dict(unit))


def _validate_schema_or_raise(schema: Any, path: str = "schema") -> None:
    if not isinstance(schema, Mapping):
        _raise(f"{path} must be an object")
    _validate_json_safe(schema, path=path)
    schema_type = schema.get("type")
    if schema_type is not None:
        valid_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
        if isinstance(schema_type, str):
            if schema_type not in valid_types:
                _raise(f"{path}.type is not supported")
        elif isinstance(schema_type, list):
            if not schema_type or any(item not in valid_types for item in schema_type):
                _raise(f"{path}.type must contain supported types")
        else:
            _raise(f"{path}.type must be a string or list")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) or not item for item in required):
        _raise(f"{path}.required must be a list of field names")
    if len(set(required)) != len(required):
        _raise(f"{path}.required contains duplicates")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        _raise(f"{path}.properties must be an object")
    for name, child in properties.items():
        if not isinstance(name, str) or not name:
            _raise(f"{path}.properties has an invalid field name")
        _validate_schema_or_raise(child, f"{path}.properties.{name}")
    items = schema.get("items")
    if items is not None:
        if isinstance(items, Mapping):
            _validate_schema_or_raise(items, f"{path}.items")
        elif isinstance(items, list):
            for index, child in enumerate(items):
                _validate_schema_or_raise(child, f"{path}.items[{index}]")
        else:
            _raise(f"{path}.items must be an object or list")
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, (bool, Mapping)):
        _raise(f"{path}.additionalProperties must be boolean or object")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (not isinstance(schema[key], int) or isinstance(schema[key], bool) or schema[key] < 0):
            _raise(f"{path}.{key} must be a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in schema and (not isinstance(schema[key], (int, float)) or isinstance(schema[key], bool) or not math.isfinite(float(schema[key]))):
            _raise(f"{path}.{key} must be finite numeric")
    if "enum" in schema:
        if not isinstance(schema["enum"], list) or not schema["enum"]:
            _raise(f"{path}.enum must be a non-empty list")


def validate_schema(schema: Any, *, raise_on_error: bool = False) -> bool:
    """Return whether a bounded JSON-schema subset is structurally valid."""

    try:
        _validate_schema_or_raise(schema)
        return True
    except GraphValidationError:
        if raise_on_error:
            raise
        return False


def validate_schema_or_raise(schema: Any) -> None:
    _validate_schema_or_raise(schema)


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _validate_value(value: Any, schema: Mapping[str, Any], path: str = "value") -> None:
    _validate_schema_or_raise(schema, path="schema")
    _validate_json_safe(value, path=path)
    expected = schema.get("type")
    if expected is not None:
        types = [expected] if isinstance(expected, str) else expected
        if not any(_type_matches(value, item) for item in types):
            _raise(f"{path}: does not match schema type")
    if "enum" in schema and value not in schema["enum"]:
        _raise(f"{path}: value is not in enum")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            _raise(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            _raise(f"{path}: longer than maxLength")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            _raise(f"{path}: less than minimum")
        if "maximum" in schema and value > schema["maximum"]:
            _raise(f"{path}: greater than maximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            _raise(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            _raise(f"{path}: more than maxItems")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, child in enumerate(value):
                _validate_value(child, items, f"{path}[{index}]")
        elif isinstance(items, list):
            for index, child_schema in enumerate(items):
                if index < len(value):
                    _validate_value(value[index], child_schema, f"{path}[{index}]")
    if isinstance(value, Mapping):
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                _raise(f"{path}: missing required field {name}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for name, child in value.items():
            if name in properties:
                _validate_value(child, properties[name], f"{path}.{name}")
            elif additional is False:
                _raise(f"{path}: unknown field {name}")
            elif isinstance(additional, Mapping):
                _validate_value(child, additional, f"{path}.{name}")


def validate_value(value: Any, schema: Any, *, raise_on_error: bool = False) -> bool:
    """Validate a value against the supported schema subset."""

    try:
        if not isinstance(schema, Mapping):
            _raise("schema must be an object")
        _validate_value(value, schema)
        return True
    except GraphValidationError:
        if raise_on_error:
            raise
        return False


validate_output = validate_value
validate_input = validate_value


def _validate_unit_or_raise(unit: Any) -> dict[str, Any]:
    value = _unit_dict(unit)
    _validate_json_safe(value, path="unit")
    missing = [field_name for field_name in _REQUIRED_UNIT_FIELDS if field_name not in value]
    if missing:
        _raise("unit missing required fields: " + ", ".join(missing))
    unit_id = _bounded_label(value["unit_id"], "unit_id")
    objective = value["objective"]
    if not isinstance(objective, str) or not objective.strip() or len(objective) > MAX_STRING_LENGTH:
        _raise("objective must be a bounded non-empty string")
    dependencies = value["dependencies"]
    if not isinstance(dependencies, list) or any(not isinstance(item, str) or not item for item in dependencies):
        _raise("dependencies must be a list of unit IDs")
    if len(set(dependencies)) != len(dependencies):
        _raise(f"unit {unit_id} has duplicate dependencies")
    if any(item == unit_id for item in dependencies):
        _raise(f"unit {unit_id} cannot depend on itself")
    if not isinstance(value["input_schema"], Mapping) or not isinstance(value["output_schema"], Mapping):
        _raise(f"unit {unit_id} schemas must be objects")
    _validate_schema_or_raise(value["input_schema"], f"unit {unit_id}.input_schema")
    _validate_schema_or_raise(value["output_schema"], f"unit {unit_id}.output_schema")
    if not isinstance(value["verification"], Mapping):
        _raise(f"unit {unit_id}.verification must be an object")
    if not isinstance(value["retry_policy"], Mapping):
        _raise(f"unit {unit_id}.retry_policy must be an object")
    for key, limit in value["retry_policy"].items():
        if key in {
            "max_attempts",
            "max_attempt_per_unit",
            "max_produce_attempts",
            "max_check_attempts",
            "max_correct_attempts",
        }:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > MAX_ATTEMPTS:
                _raise(f"unit {unit_id}.retry_policy.{key} is invalid")
    _bounded_label(value["lane"], "lane")
    _bounded_label(value["risk"], "risk")
    if value["status"] not in UNIT_STATUSES:
        _raise(f"unit {unit_id}.status is invalid")
    attempts = value.get("attempts", {stage: 0 for stage in ATTEMPT_STAGES})
    if not isinstance(attempts, Mapping):
        _raise(f"unit {unit_id}.attempts must be an object")
    for stage in ATTEMPT_STAGES:
        count = attempts.get(stage, 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count > MAX_ATTEMPTS:
            _raise(f"unit {unit_id}.attempts.{stage} is invalid")
    # Re-assign normalized values on the private copy, so callers get stable
    # defaults when they use ``normalize_unit``.
    value["unit_id"] = unit_id
    value["dependencies"] = list(dependencies)
    value["attempts"] = {stage: int(attempts.get(stage, 0)) for stage in ATTEMPT_STAGES}
    return value


def validate_unit(unit: Any, *, raise_on_error: bool = False) -> bool:
    """Return whether a unit satisfies the complete unit contract."""

    try:
        _validate_unit_or_raise(unit)
        return True
    except GraphValidationError:
        if raise_on_error:
            raise
        return False


def validate_unit_or_raise(unit: Any) -> dict[str, Any]:
    """Validate and return a normalized copy of *unit*."""

    return _validate_unit_or_raise(unit)


def normalize_unit(unit: Any) -> dict[str, Any]:
    return _validate_unit_or_raise(unit)


def _normalize_nodes(nodes: Any) -> list[dict[str, Any]]:
    if isinstance(nodes, Mapping):
        raw_nodes = []
        for key, value in nodes.items():
            candidate = _unit_dict(value)
            if "unit_id" not in candidate:
                candidate["unit_id"] = key
            raw_nodes.append(candidate)
    elif isinstance(nodes, (list, tuple)):
        raw_nodes = list(nodes)
    else:
        _raise("nodes must be a list or object")
    if len(raw_nodes) > MAX_NODES:
        _raise("graph has too many nodes")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_nodes:
        unit = _validate_unit_or_raise(raw)
        if unit["unit_id"] in seen:
            _raise(f"duplicate unit_id: {unit['unit_id']}")
        seen.add(unit["unit_id"])
        normalized.append(unit)
    return sorted(normalized, key=lambda item: item["unit_id"])


def _raw_edge_pairs(edges: Any) -> list[tuple[str, str]]:
    if edges is None:
        return []
    pairs: list[tuple[str, str]] = []
    if isinstance(edges, Mapping):
        # A mapping with explicit from/to is one edge; otherwise it is a
        # parent -> children adjacency mapping.
        if "from" in edges and "to" in edges:
            edges = [edges]
        else:
            for parent, children in edges.items():
                if isinstance(children, str):
                    children = [children]
                if not isinstance(children, (list, tuple)):
                    _raise("edge adjacency values must be lists")
                pairs.extend((parent, child) for child in children)
            return pairs
    if not isinstance(edges, (list, tuple)):
        _raise("edges must be a list or object")
    for edge in edges:
        if isinstance(edge, Mapping):
            if not isinstance(edge.get("from"), str) or not isinstance(edge.get("to"), str):
                _raise("edge objects require from and to")
            pairs.append((edge["from"], edge["to"]))
        elif isinstance(edge, (list, tuple)) and len(edge) == 2 and all(isinstance(item, str) for item in edge):
            pairs.append((edge[0], edge[1]))
        else:
            _raise("edges must contain [from, to] pairs or edge objects")
    return pairs


def _normalize_edges(edges: Any, units: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    unit_ids = {unit["unit_id"] for unit in units}
    explicit = _raw_edge_pairs(edges)
    seen_explicit: set[tuple[str, str]] = set()
    for parent, child in explicit:
        if parent not in unit_ids or child not in unit_ids:
            _raise(f"edge references unknown unit: {parent}->{child}")
        if parent == child:
            _raise(f"self edge: {parent}")
        if (parent, child) in seen_explicit:
            _raise(f"duplicate edge: {parent}->{child}")
        seen_explicit.add((parent, child))
    combined = set(explicit)
    # Dependencies are authoritative for readiness.  Including them in the
    # edge set lets cycle/topology checks work for either input style.
    for unit in units:
        for dependency in unit["dependencies"]:
            if dependency not in unit_ids:
                _raise(f"unit {unit['unit_id']} references unknown dependency {dependency}")
            combined.add((dependency, unit["unit_id"]))
    return [{"from": parent, "to": child} for parent, child in sorted(combined)]


def _edge_pairs(edges: Sequence[Mapping[str, str]]) -> list[tuple[str, str]]:
    return [(edge["from"], edge["to"]) for edge in edges]


def find_cycles(nodes: Any, edges: Any = None) -> list[list[str]]:
    """Return deterministic cycle components (empty when acyclic)."""

    if isinstance(nodes, Mapping) and "nodes" in nodes:
        state = nodes
        normalized_nodes = _normalize_nodes(state["nodes"])
        normalized_edges = _normalize_edges(state.get("edges", []), normalized_nodes)
    else:
        normalized_nodes = _normalize_nodes(nodes)
        normalized_edges = _normalize_edges(edges or [], normalized_nodes)
    adjacency: dict[str, list[str]] = defaultdict(list)
    for parent, child in _edge_pairs(normalized_edges):
        adjacency[parent].append(child)
    for values in adjacency.values():
        values.sort()
    visited: set[str] = set()
    active: list[str] = []
    active_set: set[str] = set()
    cycles: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        if node in active_set:
            start = active.index(node)
            cycle = active[start:]
            if cycle:
                rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
                cycles.add(min(rotations))
            return
        if node in visited:
            return
        active.append(node)
        active_set.add(node)
        for child in adjacency.get(node, []):
            visit(child)
        active.pop()
        active_set.remove(node)
        visited.add(node)

    for unit in normalized_nodes:
        visit(unit["unit_id"])
    return [list(cycle) for cycle in sorted(cycles)]


def detect_cycles(nodes: Any, edges: Any = None) -> list[list[str]]:
    return find_cycles(nodes, edges)


def has_cycle(nodes: Any, edges: Any = None) -> bool:
    return bool(find_cycles(nodes, edges))


def topological_order(nodes: Any, edges: Any = None) -> list[str]:
    """Return a deterministic Kahn topological order or raise on cycles."""

    if isinstance(nodes, Mapping) and "nodes" in nodes:
        normalized_nodes = _normalize_nodes(nodes["nodes"])
        normalized_edges = _normalize_edges(nodes.get("edges", []), normalized_nodes)
    else:
        normalized_nodes = _normalize_nodes(nodes)
        normalized_edges = _normalize_edges(edges or [], normalized_nodes)
    identifiers = sorted(unit["unit_id"] for unit in normalized_nodes)
    indegree = {identifier: 0 for identifier in identifiers}
    adjacency: dict[str, list[str]] = defaultdict(list)
    for parent, child in _edge_pairs(normalized_edges):
        indegree[child] += 1
        adjacency[parent].append(child)
    for children in adjacency.values():
        children.sort()
    ready = sorted(identifier for identifier in identifiers if indegree[identifier] == 0)
    result: list[str] = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for child in adjacency.get(current, []):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(result) != len(identifiers):
        _raise("graph contains a dependency cycle")
    return result


def validate_dependencies(nodes: Any, edges: Any = None, *, raise_on_error: bool = False) -> bool:
    try:
        normalized_nodes = _normalize_nodes(nodes)
        normalized_edges = _normalize_edges(edges or [], normalized_nodes)
        if find_cycles(normalized_nodes, normalized_edges):
            _raise("graph contains a dependency cycle")
        return True
    except GraphValidationError:
        if raise_on_error:
            raise
        return False


def _default_limits(limits: Mapping[str, Any] | None) -> dict[str, Any]:
    result = copy.deepcopy(DEFAULT_LIMITS)
    if limits is not None:
        if not isinstance(limits, Mapping):
            _raise("limits must be an object")
        for key, value in limits.items():
            if key not in result:
                _raise(f"unknown graph limit: {key}")
            result[key] = value
    for key in ("max_attempt_per_unit", "max_graph_steps", "max_total_child_runs", "max_parallelism"):
        value = result[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > MAX_ATTEMPTS:
            _raise(f"limits.{key} must be a bounded positive integer")
    wall = result["max_wall_time_ms"]
    if wall is not None and (not isinstance(wall, int) or isinstance(wall, bool) or wall < 1 or wall > 86_400_000):
        _raise("limits.max_wall_time_ms must be null or a bounded positive integer")
    return result


def _validate_limits_or_raise(limits: Any) -> dict[str, Any]:
    if not isinstance(limits, Mapping):
        _raise("limits must be an object")
    return _default_limits(limits)


def _timestamp_value(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _raise(f"timestamps.{field_name} must be a non-negative integer")
    return value


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_timestamps(timestamps: Mapping[str, Any] | None, *, now_ms: int | None = None) -> dict[str, Any]:
    current = _now_ms() if now_ms is None else _timestamp_value(now_ms, "now_ms")
    result = {"created_at_ms": current, "started_at_ms": current, "updated_at_ms": current, "ended_at_ms": None}
    if timestamps is not None:
        if not isinstance(timestamps, Mapping):
            _raise("timestamps must be an object")
        for key, value in timestamps.items():
            if key not in result:
                _raise(f"unknown timestamp: {key}")
            if key == "ended_at_ms" and value is None:
                result[key] = None
            else:
                result[key] = _timestamp_value(value, key)
    return result


def hardening_gate_a_passed(result: Any) -> bool:
    """Check a supplied Hardening Gate A result without trusting prose."""

    if not isinstance(result, Mapping):
        return False
    gate_name = str(result.get("gate", result.get("name", result.get("id", "")))).strip().lower()
    explicit_gate = gate_name in {"a", "gate_a", "hardening_gate_a", "hardening-a", "a_gate"}
    if not explicit_gate and not any(key in result for key in ("gate_a_passed", "hardening_gate_a_passed")):
        return False
    checks = result.get("required_checks")
    if not isinstance(checks, Mapping) or set(checks) != HARDENING_GATE_A_REQUIRED_CHECKS:
        return False
    if not all(type(checks[name]) is bool and checks[name] is True for name in HARDENING_GATE_A_REQUIRED_CHECKS):
        return False
    passed = result.get("passed", result.get("ok", result.get("gate_a_passed", result.get("hardening_gate_a_passed"))))
    return passed is True


def mode_gate(mode: str | None = None, hardening_gate: Any = None) -> dict[str, Any]:
    """Resolve the deprecated 0.6 JSON adapter mode.

    This compatibility engine may observe current routing, but it is not Graph
    IR v1, has no trusted executor boundary, and can never authorize enforce.
    """

    requested = DEFAULT_MODE if mode is None else str(mode).strip().lower()
    if requested not in GRAPH_MODES:
        _raise(f"unknown graph mode: {requested}")
    if requested in ("off", "observe"):
        return {
            "requested_mode": requested,
            "effective_mode": requested,
            "enforce_allowed": False,
            "current_routing_fallback": True,
            "reason": "current_routing_preserved",
        }
    return {
        "requested_mode": requested,
        "effective_mode": "observe",
        "enforce_allowed": False,
        "current_routing_fallback": True,
        "reason": "legacy_adapter_never_enforce",
    }


resolve_mode = mode_gate


def enforce_mode_allowed(_result: Any = None) -> bool:
    """Compatibility API: enforce belongs only to Graph IR v1 + trusted host."""
    return False


def create_graph(
    graph_id: str,
    task_class: str,
    nodes: Any,
    *,
    edges: Any = None,
    constraints: Any = None,
    limits: Mapping[str, Any] | None = None,
    mode: str = DEFAULT_MODE,
    hardening_gate: Any = None,
    gate_results: Any = None,
    timestamps: Mapping[str, Any] | None = None,
    now_ms: int | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Create a fully validated graph state from structured units."""

    graph_id = _bounded_label(graph_id, "graph_id")
    task_class = _bounded_label(task_class, "task_class")
    normalized_nodes = _normalize_nodes(nodes)
    normalized_edges = _normalize_edges(edges or [], normalized_nodes)
    if find_cycles(normalized_nodes, normalized_edges):
        _raise("graph contains a dependency cycle")
    normalized_limits = _default_limits(limits)
    gate = mode_gate(mode, hardening_gate)
    if constraints is None:
        normalized_constraints: list[Any] = []
    elif isinstance(constraints, list):
        normalized_constraints = copy.deepcopy(constraints)
    elif isinstance(constraints, Mapping):
        normalized_constraints = [copy.deepcopy(constraints)]
    else:
        _raise("constraints must be a list or object")
    _validate_json_safe(normalized_constraints, path="constraints")
    normalized_gate_results = {} if gate_results is None else copy.deepcopy(gate_results)
    if hardening_gate is not None:
        if isinstance(normalized_gate_results, Mapping):
            normalized_gate_results = dict(normalized_gate_results)
            normalized_gate_results["hardening_gate_a"] = copy.deepcopy(hardening_gate)
        else:
            _raise("gate_results must be an object when a hardening gate is supplied")
    _validate_json_safe(normalized_gate_results, path="gate_results")
    current = _normalize_timestamps(timestamps, now_ms=now_ms)
    status = "accepted" if not normalized_nodes else "pending"
    if status == "accepted":
        current["ended_at_ms"] = current["updated_at_ms"]
    state: dict[str, Any] = {
        "graph_id": graph_id,
        "version": GRAPH_SCHEMA_VERSION,
        "task_class": task_class,
        "nodes": normalized_nodes,
        "edges": normalized_edges,
        "status": status,
        "constraints": normalized_constraints,
        "gate_results": normalized_gate_results,
        "accepted_outputs": {},
        "failed_outputs": {},
        "working_outputs": {},
        "limits": normalized_limits,
        "counters": {
            "graph_steps": 0,
            "total_child_runs": 0,
            "attempts": 0,
            "retries": 0,
            "accepted_units": 0,
            "failed_units": 0,
            "reopened_units": 0,
            "invalidated_units": 0,
            "parallel_width": 0,
            "tokens_input": 0,
            "tokens_output": 0,
            "tokens_total": 0,
        },
        "timestamps": current,
        "mode": gate["effective_mode"],
        "requested_mode": gate["requested_mode"],
        "mode_gate": gate,
        "failure_reason": None,
        "send_back_count": 0,
        "shadow": {
            "eligible_count": 0,
            "predicted_accept_count": 0,
            "predicted_failure_count": 0,
            "predicted_retry_count": 0,
            "predicted_steps": 0,
            "predicted_child_runs": 0,
            "route_counts": {},
        },
    }
    if device_id is not None:
        state["device_id"] = _bounded_label(device_id, "device_id")
    _refresh_counts(state)
    validate_graph_or_raise(state)
    return state


new_graph = create_graph
initialize_graph = create_graph
build_graph = create_graph


def _normalize_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        _raise("graph state must be an object")
    value = copy.deepcopy(dict(state))
    _validate_graph_or_raise(value)
    return value


def _validate_graph_or_raise(state: Any) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        _raise("graph state must be an object")
    value = state
    _validate_json_safe(value, path="graph")
    missing = [field_name for field_name in _REQUIRED_GRAPH_FIELDS if field_name not in value]
    if missing:
        _raise("graph missing required fields: " + ", ".join(missing))
    if value["version"] != GRAPH_SCHEMA_VERSION:
        _raise(f"unsupported graph schema version: {value['version']}")
    _bounded_label(value["graph_id"], "graph_id")
    _bounded_label(value["task_class"], "task_class")
    normalized_nodes = _normalize_nodes(value["nodes"])
    normalized_edges = _normalize_edges(value["edges"], normalized_nodes)
    if find_cycles(normalized_nodes, normalized_edges):
        _raise("graph contains a dependency cycle")
    if value["status"] not in GRAPH_STATUSES:
        _raise("graph status is invalid")
    if not isinstance(value["constraints"], (list, Mapping)):
        _raise("constraints must be a list or object")
    if not isinstance(value["gate_results"], Mapping):
        _raise("gate_results must be an object")
    for key in ("accepted_outputs", "failed_outputs"):
        if not isinstance(value[key], Mapping):
            _raise(f"{key} must be an object")
        unknown = set(value[key]) - {unit["unit_id"] for unit in normalized_nodes}
        if unknown:
            _raise(f"{key} references unknown units")
    if "working_outputs" in value and not isinstance(value["working_outputs"], Mapping):
        _raise("working_outputs must be an object")
    _validate_limits_or_raise(value["limits"])
    if not isinstance(value["counters"], Mapping):
        _raise("counters must be an object")
    for key, count in value["counters"].items():
        if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count > MAX_ATTEMPTS * MAX_NODES:
            _raise(f"counters.{key} must be a bounded non-negative integer")
    if not isinstance(value["timestamps"], Mapping):
        _raise("timestamps must be an object")
    for key, timestamp in value["timestamps"].items():
        if key.endswith("_at_ms") and timestamp is not None:
            _timestamp_value(timestamp, key)
    mode = value.get("mode", DEFAULT_MODE)
    if mode not in GRAPH_MODES:
        _raise("graph mode is invalid")
    if "requested_mode" in value and value["requested_mode"] not in GRAPH_MODES:
        _raise("requested graph mode is invalid")
    if "mode_gate" in value and not isinstance(value["mode_gate"], Mapping):
        _raise("mode_gate must be an object")
    return dict(value)


def validate_graph(state: Any, *, raise_on_error: bool = False) -> bool:
    """Return whether graph state is complete, bounded, acyclic, and safe."""

    try:
        _validate_graph_or_raise(state)
        return True
    except GraphValidationError:
        if raise_on_error:
            raise
        return False


def validate_graph_or_raise(state: Any) -> dict[str, Any]:
    return _validate_graph_or_raise(state)


def normalize_graph(state: Any) -> dict[str, Any]:
    """Return a validated deep copy with canonical node/edge ordering."""

    value = _normalize_state(state)
    value["nodes"] = _normalize_nodes(value["nodes"])
    value["edges"] = _normalize_edges(value["edges"], value["nodes"])
    return value


def _nodes_by_id(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {unit["unit_id"]: unit for unit in state["nodes"]}


def _dependency_map(state: Mapping[str, Any]) -> dict[str, set[str]]:
    dependencies = {unit["unit_id"]: set(unit["dependencies"]) for unit in state["nodes"]}
    for parent, child in _edge_pairs(state["edges"]):
        dependencies.setdefault(child, set()).add(parent)
    return dependencies


def _dependents_map(state: Mapping[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for child, parents in _dependency_map(state).items():
        for parent in parents:
            result[parent].add(child)
    return result


def transitive_dependents(state: Any, unit_id: str) -> list[str]:
    """Return all downstream units in deterministic breadth-first order."""

    value = _normalize_state(state)
    if unit_id not in _nodes_by_id(value):
        _raise(f"unknown unit: {unit_id}")
    dependents = _dependents_map(value)
    result: list[str] = []
    queue = list(sorted(dependents.get(unit_id, set())))
    visited: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        queue.extend(sorted(dependents.get(current, set())))
    return result


def _dependency_ready(state: Mapping[str, Any], unit: Mapping[str, Any]) -> bool:
    dependencies = _dependency_map(state).get(unit["unit_id"], set())
    by_id = _nodes_by_id(state)
    return all(by_id[dependency]["status"] == "accepted" for dependency in dependencies)


def ready_nodes(state: Any) -> list[str]:
    """Return dependency-ready unit IDs, sorted independently of insertion order."""

    value = _normalize_state(state)
    ready: list[str] = []
    for unit in value["nodes"]:
        if unit["status"] in {"pending", "ready", "reopened", "invalidated"} and _dependency_ready(value, unit):
            ready.append(unit["unit_id"])
    return sorted(ready)


def _ownership_key(ownership: Any) -> str:
    return canonical_json(ownership)


def ownership_conflicts(state: Any, node_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Report ownership collisions among dependency-ready parallel candidates."""

    value = _normalize_state(state)
    candidates = ready_nodes(value) if node_ids is None else sorted(set(node_ids))
    by_id = _nodes_by_id(value)
    owners: dict[str, list[str]] = defaultdict(list)
    for identifier in candidates:
        if identifier not in by_id:
            _raise(f"unknown unit: {identifier}")
        owners[_ownership_key(by_id[identifier]["ownership"])].append(identifier)
    conflicts = []
    for owner, identifiers in sorted(owners.items()):
        if len(identifiers) > 1:
            conflicts.append({"ownership_key": opaque_id("ownership", owner), "unit_ids": sorted(identifiers)})
    return conflicts


detect_ownership_conflicts = ownership_conflicts


def parallel_ready_nodes(state: Any, max_parallelism: int | None = None) -> list[str]:
    """Select a bounded, ownership-conflict-free ready set."""

    value = _normalize_state(state)
    limit = value["limits"]["max_parallelism"] if max_parallelism is None else max_parallelism
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        _raise("max_parallelism must be positive")
    by_id = _nodes_by_id(value)
    selected: list[str] = []
    owners: set[str] = set()
    for identifier in ready_nodes(value):
        owner = _ownership_key(by_id[identifier]["ownership"])
        if owner in owners:
            continue
        selected.append(identifier)
        owners.add(owner)
        if len(selected) >= limit:
            break
    return selected


select_ready_nodes = parallel_ready_nodes


def retry_accounting(state: Any, unit_id: str | None = None) -> dict[str, Any]:
    """Return bounded attempt/retry accounting for one unit or the graph."""

    value = _normalize_state(state)
    by_id = _nodes_by_id(value)
    if unit_id is None:
        units = list(by_id.values())
    else:
        if unit_id not in by_id:
            _raise(f"unknown unit: {unit_id}")
        units = [by_id[unit_id]]
    rows = []
    for unit in sorted(units, key=lambda item: item["unit_id"]):
        attempts = unit.get("attempts", {})
        total = sum(int(attempts.get(stage, 0)) for stage in ATTEMPT_STAGES)
        policy = unit["retry_policy"]
        configured = policy.get("max_attempt_per_unit", policy.get("max_attempts", value["limits"]["max_attempt_per_unit"]))
        maximum = min(value["limits"]["max_attempt_per_unit"], int(configured))
        rows.append(
            {
                "unit_id": unit["unit_id"],
                "attempts": {stage: int(attempts.get(stage, 0)) for stage in ATTEMPT_STAGES},
                "total_attempts": total,
                "max_attempts": maximum,
                "remaining": max(0, maximum - total),
                "exhausted": total >= maximum,
                "can_retry": total < maximum,
            }
        )
    if unit_id is not None:
        return rows[0]
    return {
        "units": rows,
        "total_attempts": sum(row["total_attempts"] for row in rows),
        "total_remaining": sum(row["remaining"] for row in rows),
        "exhausted_units": sum(1 for row in rows if row["exhausted"]),
    }


def _effective_attempt_limit(state: Mapping[str, Any], unit: Mapping[str, Any]) -> int:
    policy = unit["retry_policy"]
    configured = policy.get("max_attempt_per_unit", policy.get("max_attempts", state["limits"]["max_attempt_per_unit"]))
    return min(state["limits"]["max_attempt_per_unit"], int(configured))


def _stage_attempt_limit(state: Mapping[str, Any], unit: Mapping[str, Any], stage: str) -> int:
    """Return the smaller of graph, unit-total, and unit-stage limits."""

    policy = unit["retry_policy"]
    stage_limit = policy.get(f"max_{stage}_attempts", _effective_attempt_limit(state, unit))
    return min(_effective_attempt_limit(state, unit), int(stage_limit))


def _refresh_counts(state: MutableMapping[str, Any]) -> None:
    statuses = Counter(unit["status"] for unit in state["nodes"])
    counters = state.setdefault("counters", {})
    counters["accepted_units"] = statuses.get("accepted", 0)
    counters["failed_units"] = statuses.get("failed", 0)
    counters["reopened_units"] = statuses.get("reopened", 0)
    counters["invalidated_units"] = statuses.get("invalidated", 0)
    counters["attempts"] = sum(
        sum(int(unit.get("attempts", {}).get(stage, 0)) for stage in ATTEMPT_STAGES) for unit in state["nodes"]
    )


def _set_timestamp(state: MutableMapping[str, Any], now_ms: int | None, *, end: bool = False) -> int:
    current = _now_ms() if now_ms is None else _timestamp_value(now_ms, "now_ms")
    state["timestamps"]["updated_at_ms"] = current
    if end:
        state["timestamps"]["ended_at_ms"] = current
    return current


def _mark_convergence_failed(state: MutableMapping[str, Any], reason: str, now_ms: int | None = None) -> None:
    state["status"] = "convergence_failed"
    state["failure_reason"] = reason[:MAX_REASON_LENGTH]
    _set_timestamp(state, now_ms, end=True)


def _guard_step(state: MutableMapping[str, Any], now_ms: int | None = None, *, child_run: bool = False) -> bool:
    if state["status"] == "convergence_failed":
        return False
    limits = state["limits"]
    counters = state["counters"]
    next_step = int(counters.get("graph_steps", 0)) + 1
    if next_step > limits["max_graph_steps"]:
        _mark_convergence_failed(state, "max_graph_steps", now_ms)
        return False
    if child_run:
        next_run = int(counters.get("total_child_runs", 0)) + 1
        if next_run > limits["max_total_child_runs"]:
            _mark_convergence_failed(state, "max_total_child_runs", now_ms)
            return False
        counters["total_child_runs"] = next_run
    counters["graph_steps"] = next_step
    started = state["timestamps"].get("started_at_ms")
    current = _set_timestamp(state, now_ms)
    wall_limit = limits.get("max_wall_time_ms")
    if wall_limit is not None and isinstance(started, int) and current - started > wall_limit:
        _mark_convergence_failed(state, "max_wall_time_ms", current)
        return False
    return True


def _affected_dependents(state: MutableMapping[str, Any], failed_id: str) -> list[str]:
    affected = transitive_dependents(state, failed_id)
    by_id = _nodes_by_id(state)
    for identifier in affected:
        unit = by_id[identifier]
        if unit["status"] == "accepted":
            state["accepted_outputs"].pop(identifier, None)
        state.get("working_outputs", {}).pop(identifier, None)
        state["failed_outputs"].pop(identifier, None)
        unit["status"] = "reopened"
    return affected


def _failure_code(reason: Any) -> str:
    if isinstance(reason, Mapping):
        reason = reason.get("code", reason.get("reason", "unit_failed"))
    if not isinstance(reason, str):
        return "unit_failed"
    text = reason.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.:-]{1,128}", text):
        return "unit_failed"
    if _forbidden_key(text):
        return "unit_failed"
    return text


def fail_unit(state: Any, unit_id: str, reason: Any = "unit_failed", *, now_ms: int | None = None, output: Any = None) -> dict[str, Any]:
    """Fail one unit and reopen only its transitive dependents."""

    value = _normalize_state(state)
    # An accepted whole graph can still receive a deterministic send-back
    # (for example, a later verification pass can invalidate one unit).  The
    # failure transition deliberately reopens the graph; only convergence
    # failure is immutable.
    if value["status"] == "convergence_failed":
        _raise("convergence-failed graph cannot accept a new failure")
    if not _guard_step(value, now_ms, child_run=True):
        return value
    by_id = _nodes_by_id(value)
    if unit_id not in by_id:
        _raise(f"unknown unit: {unit_id}")
    unit = by_id[unit_id]
    attempts = sum(unit.get("attempts", {}).values())
    if attempts >= _effective_attempt_limit(value, unit):
        _mark_convergence_failed(value, "retry_budget_exhausted", now_ms)
        return value
    # ``fail_unit`` is the explicit send-back boundary, so count it as a
    # check/correction attempt just like a failed record_unit_attempt call.
    unit["attempts"]["check"] = min(MAX_ATTEMPTS, int(unit["attempts"].get("check", 0)) + 1)
    if output is not None:
        _validate_json_safe(output, path=f"unit {unit_id}.failed_output")
    unit["status"] = "failed"
    value["failed_outputs"][unit_id] = {"reason_code": _failure_code(reason)} if output is None else copy.deepcopy(output)
    value["accepted_outputs"].pop(unit_id, None)
    value.get("working_outputs", {}).pop(unit_id, None)
    _affected_dependents(value, unit_id)
    value["status"] = "retry_pending"
    value["counters"]["retries"] = int(value["counters"].get("retries", 0)) + 1
    value["send_back_count"] = int(value.get("send_back_count", 0)) + 1
    _refresh_counts(value)
    return value


def retry_unit(state: Any, unit_id: str, *, now_ms: int | None = None) -> dict[str, Any]:
    """Reopen a failed unit when its bounded retry budget remains."""

    value = _normalize_state(state)
    by_id = _nodes_by_id(value)
    if unit_id not in by_id:
        _raise(f"unknown unit: {unit_id}")
    unit = by_id[unit_id]
    if unit["status"] != "failed":
        _raise(f"unit {unit_id} is not failed")
    if sum(unit.get("attempts", {}).values()) >= _effective_attempt_limit(value, unit):
        _mark_convergence_failed(value, "retry_budget_exhausted", now_ms)
        return value
    unit["status"] = "reopened"
    value["failed_outputs"].pop(unit_id, None)
    value["status"] = "retry_pending"
    _set_timestamp(value, now_ms)
    _refresh_counts(value)
    return value


def _check_unit_ready(state: Mapping[str, Any], unit: Mapping[str, Any]) -> None:
    if unit["status"] not in {"pending", "ready", "running", "reopened", "invalidated", "produced", "accepted"}:
        _raise(f"unit {unit['unit_id']} is not ready for execution")
    if not _dependency_ready(state, unit):
        _raise(f"unit {unit['unit_id']} has incomplete dependencies")


def complete_unit(state: Any, unit_id: str, output: Any = None, *, now_ms: int | None = None) -> dict[str, Any]:
    """Accept a unit after deterministic schema/completeness validation."""

    value = _normalize_state(state)
    if value["status"] == "convergence_failed":
        _raise("convergence-failed graph cannot accept a new unit")
    if not _guard_step(value, now_ms, child_run=True):
        return value
    by_id = _nodes_by_id(value)
    if unit_id not in by_id:
        _raise(f"unknown unit: {unit_id}")
    unit = by_id[unit_id]
    _check_unit_ready(value, unit)
    if sum(int(unit.get("attempts", {}).get(item, 0)) for item in ATTEMPT_STAGES) >= _effective_attempt_limit(value, unit):
        _mark_convergence_failed(value, "retry_budget_exhausted", now_ms)
        return value
    if not validate_value(output, unit["output_schema"]):
        unit["status"] = "failed"
        value["failed_outputs"][unit_id] = {"reason_code": "output_schema_invalid"}
        value["accepted_outputs"].pop(unit_id, None)
        value.get("working_outputs", {}).pop(unit_id, None)
        _affected_dependents(value, unit_id)
        value["status"] = "retry_pending"
        value["counters"]["retries"] = int(value["counters"].get("retries", 0)) + 1
        value["send_back_count"] = int(value.get("send_back_count", 0)) + 1
        _refresh_counts(value)
        return value
    unit["status"] = "accepted"
    unit["attempts"]["check"] = min(MAX_ATTEMPTS, int(unit["attempts"].get("check", 0)) + 1)
    value["accepted_outputs"][unit_id] = copy.deepcopy(output)
    value["failed_outputs"].pop(unit_id, None)
    value.get("working_outputs", {}).pop(unit_id, None)
    _refresh_counts(value)
    _refresh_graph_status(value, now_ms)
    return value


accept_unit = complete_unit


def _outcome_parts(outcome: Any) -> tuple[bool, Any, str]:
    if isinstance(outcome, bool):
        return outcome, None, "unit_failed"
    if isinstance(outcome, Mapping):
        ok = outcome.get("ok", outcome.get("passed", outcome.get("success", outcome.get("verified", False))))
        reason = outcome.get("reason_code", outcome.get("reason", "unit_failed"))
        output = outcome.get("output")
        return ok is True, copy.deepcopy(output), _failure_code(reason)
    return False, None, "unit_failed"


def record_unit_attempt(
    state: Any,
    unit_id: str,
    stage: str,
    outcome: Any,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Record one bounded produce/check/correct attempt.

    ``outcome`` may be a boolean or ``{"ok": bool, "output": ...}`` mapping.
    Only deterministic bookkeeping happens here; no model or external call is
    made by this function.
    """

    if stage not in ATTEMPT_STAGES:
        _raise(f"unknown attempt stage: {stage}")
    value = _normalize_state(state)
    if value["status"] == "convergence_failed":
        _raise("convergence-failed graph cannot record an attempt")
    by_id = _nodes_by_id(value)
    if unit_id not in by_id:
        _raise(f"unknown unit: {unit_id}")
    unit = by_id[unit_id]
    _check_unit_ready(value, unit)
    attempts_total = sum(int(unit.get("attempts", {}).get(item, 0)) for item in ATTEMPT_STAGES)
    if attempts_total >= _effective_attempt_limit(value, unit):
        _mark_convergence_failed(value, "retry_budget_exhausted", now_ms)
        return value
    if int(unit.get("attempts", {}).get(stage, 0)) >= _stage_attempt_limit(value, unit, stage):
        _mark_convergence_failed(value, "retry_budget_exhausted", now_ms)
        return value
    if not _guard_step(value, now_ms, child_run=True):
        return value
    unit["attempts"][stage] = int(unit["attempts"].get(stage, 0)) + 1
    ok, output, reason = _outcome_parts(outcome)
    if output is not None:
        _validate_json_safe(output, path=f"unit {unit_id}.output")
    if ok:
        if stage == "produce":
            unit["status"] = "produced"
            if output is not None:
                value.setdefault("working_outputs", {})[unit_id] = output
        elif stage == "correct":
            if isinstance(outcome, Mapping) and outcome.get("verified") is True:
                candidate = output if output is not None else value.get("working_outputs", {}).get(unit_id)
                if not validate_value(candidate, unit["output_schema"]):
                    unit["status"] = "failed"
                    value["failed_outputs"][unit_id] = {"reason_code": "output_schema_invalid"}
                    value["accepted_outputs"].pop(unit_id, None)
                    value.get("working_outputs", {}).pop(unit_id, None)
                    _affected_dependents(value, unit_id)
                    value["status"] = "retry_pending"
                    value["counters"]["retries"] = int(value["counters"].get("retries", 0)) + 1
                    value["send_back_count"] = int(value.get("send_back_count", 0)) + 1
                    _refresh_counts(value)
                    return value
                unit["status"] = "accepted"
                value["accepted_outputs"][unit_id] = copy.deepcopy(candidate)
                value.get("working_outputs", {}).pop(unit_id, None)
            else:
                unit["status"] = "produced"
                if output is not None:
                    value.setdefault("working_outputs", {})[unit_id] = output
        else:  # check
            candidate = output if output is not None else value.get("working_outputs", {}).get(unit_id)
            if not validate_value(candidate, unit["output_schema"]):
                unit["status"] = "failed"
                value["failed_outputs"][unit_id] = {"reason_code": "output_schema_invalid"}
                value["accepted_outputs"].pop(unit_id, None)
                value.get("working_outputs", {}).pop(unit_id, None)
                _affected_dependents(value, unit_id)
                value["status"] = "retry_pending"
                value["counters"]["retries"] = int(value["counters"].get("retries", 0)) + 1
                value["send_back_count"] = int(value.get("send_back_count", 0)) + 1
                _refresh_counts(value)
                return value
            unit["status"] = "accepted"
            value["accepted_outputs"][unit_id] = copy.deepcopy(candidate)
            value.get("working_outputs", {}).pop(unit_id, None)
        value["failed_outputs"].pop(unit_id, None)
        _refresh_counts(value)
        _refresh_graph_status(value, now_ms)
        return value
    # A failed attempt reopens downstream accepted work.  The failed unit is
    # left failed until retry_unit/advance_graph invokes it again.
    unit["status"] = "failed"
    value["failed_outputs"][unit_id] = {"reason_code": reason}
    value["accepted_outputs"].pop(unit_id, None)
    value.get("working_outputs", {}).pop(unit_id, None)
    _affected_dependents(value, unit_id)
    value["status"] = "retry_pending"
    value["counters"]["retries"] = int(value["counters"].get("retries", 0)) + 1
    value["send_back_count"] = int(value.get("send_back_count", 0)) + 1
    _refresh_counts(value)
    return value


record_attempt = record_unit_attempt


def _refresh_graph_status(state: MutableMapping[str, Any], now_ms: int | None = None) -> None:
    if state["status"] == "convergence_failed":
        return
    statuses = {unit["status"] for unit in state["nodes"]}
    if not state["nodes"] or all(unit["status"] == "accepted" for unit in state["nodes"]):
        state["status"] = "accepted"
        _set_timestamp(state, now_ms, end=True)
    elif "failed" in statuses:
        state["status"] = "retry_pending"
    elif any(status in statuses for status in ("pending", "ready", "reopened", "invalidated", "produced", "running")):
        state["status"] = "running"
    else:
        state["status"] = "stalled"


def finalize_graph(state: Any, *, now_ms: int | None = None) -> dict[str, Any]:
    """Mark a graph accepted only when every unit is accepted."""

    value = _normalize_state(state)
    if all(unit["status"] == "accepted" for unit in value["nodes"]):
        value["status"] = "accepted"
        _set_timestamp(value, now_ms, end=True)
        return value
    _refresh_graph_status(value, now_ms)
    return value


accept_graph = finalize_graph
graph_is_accepted = lambda state: _normalize_state(state)["status"] == "accepted"


def advance_graph(
    state: Any,
    outcomes: Mapping[str, Any] | None = None,
    *,
    now_ms: int | None = None,
    max_parallelism: int | None = None,
) -> dict[str, Any]:
    """Advance one deterministic ready round using supplied outcomes.

    Missing outcomes are intentionally left as ``ready``; this function never
    invokes a model.  An outcome boolean is treated as a direct completion or
    failure.  A mapping may specify ``stage`` plus the fields accepted by
    :func:`record_unit_attempt`.
    """

    value = _normalize_state(state)
    if value["status"] in {"accepted", "convergence_failed"}:
        return value
    if outcomes is not None and not isinstance(outcomes, Mapping):
        _raise("outcomes must be an object")
    selected = parallel_ready_nodes(value, max_parallelism)
    value["counters"]["parallel_width"] = max(int(value["counters"].get("parallel_width", 0)), len(selected))
    for identifier in selected:
        by_id = _nodes_by_id(value)
        by_id[identifier]["status"] = "ready"
    _refresh_graph_status(value, now_ms)
    if outcomes:
        for identifier in selected:
            if identifier not in outcomes:
                continue
            outcome = outcomes[identifier]
            if isinstance(outcome, Mapping) and "stage" in outcome:
                stage = outcome["stage"]
                payload = {key: copy.deepcopy(child) for key, child in outcome.items() if key != "stage"}
                value = record_unit_attempt(value, identifier, stage, payload, now_ms=now_ms)
            else:
                ok, output, reason = _outcome_parts(outcome)
                if ok:
                    value = complete_unit(value, identifier, output, now_ms=now_ms)
                else:
                    value = fail_unit(value, identifier, reason, now_ms=now_ms, output=None)
            if value["status"] == "convergence_failed":
                break
    else:
        _guard_step(value, now_ms, child_run=False)
    _refresh_counts(value)
    _refresh_graph_status(value, now_ms)
    return value


advance = advance_graph


def structurally_compatible_merge(parts: Any, *, schema: Mapping[str, Any] | None = None) -> Any:
    """Deterministically merge compatible JSON structures.

    Mapping key collisions are accepted only when values are equal or can be
    recursively merged.  Scalar/list shape conflicts are rejected rather than
    silently overwritten.
    """

    if isinstance(parts, Mapping):
        # Treat a mapping of named parts as a set of parts when every value is a
        # mapping; otherwise it is already one object and is copied as-is.
        values = list(parts.values())
        if values and all(isinstance(item, Mapping) for item in values):
            source_parts = [parts[key] for key in sorted(parts)]
        else:
            source_parts = [parts]
    elif isinstance(parts, (list, tuple)):
        source_parts = list(parts)
    else:
        _raise("merge input must be an object or list")
    if not source_parts:
        merged: Any = {}
    elif all(isinstance(item, Mapping) for item in source_parts):
        merged = {}
        for item in source_parts:
            for key in sorted(item):
                child = copy.deepcopy(item[key])
                if key not in merged:
                    merged[key] = child
                else:
                    merged[key] = _merge_values(merged[key], child, f"merge.{key}")
    else:
        merged = copy.deepcopy(source_parts[0])
        for child in source_parts[1:]:
            merged = _merge_values(merged, child, "merge")
    _validate_json_safe(merged, path="merged")
    if schema is not None and not validate_value(merged, schema):
        _raise("merged value does not match output schema")
    return merged


def _merge_values(left: Any, right: Any, path: str) -> Any:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        result = copy.deepcopy(dict(left))
        for key in sorted(right):
            if key in result:
                result[key] = _merge_values(result[key], right[key], f"{path}.{key}")
            else:
                result[key] = copy.deepcopy(right[key])
        return result
    if isinstance(left, list) and isinstance(right, list):
        if left == right:
            return copy.deepcopy(left)
        _raise(f"{path}: incompatible list values")
    if type(left) is type(right) and left == right:
        return copy.deepcopy(left)
    _raise(f"{path}: incompatible merge values")


merge_outputs = structurally_compatible_merge
compatible_merge = structurally_compatible_merge


def merge_accepted_outputs(state: Any, unit_ids: Iterable[str] | None = None, *, schema: Mapping[str, Any] | None = None) -> Any:
    value = _normalize_state(state)
    identifiers = sorted(value["accepted_outputs"] if unit_ids is None else set(unit_ids))
    for identifier in identifiers:
        if identifier not in value["accepted_outputs"]:
            _raise(f"unit {identifier} has no accepted output")
    return structurally_compatible_merge([value["accepted_outputs"][identifier] for identifier in identifiers], schema=schema)


def record_verified_constraint(state: Any, constraint: Mapping[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """Add one structured constraint; only verified reusable rows export."""

    value = _normalize_state(state)
    if not isinstance(constraint, Mapping):
        _raise("constraint must be an object")
    candidate = copy.deepcopy(dict(constraint))
    _validate_json_safe(candidate, path="constraint")
    if candidate.get("verified") is not True:
        _raise("only verified constraints may be recorded")
    statement = candidate.get("statement", candidate.get("rule"))
    if not isinstance(statement, str) or not statement.strip() or len(statement) > MAX_STRING_LENGTH:
        _raise("verified constraint requires a bounded statement")
    identifier = candidate.get("constraint_id", candidate.get("id"))
    if identifier is None:
        identifier = stable_id("constraint", statement, candidate.get("scope"), candidate.get("kind"))
    if not isinstance(identifier, str) or not identifier:
        _raise("constraint_id must be a string")
    candidate["constraint_id"] = identifier
    candidate["statement"] = statement
    candidate["reusable"] = candidate.get("reusable", True) is True
    existing = value["constraints"] if isinstance(value["constraints"], list) else [value["constraints"]]
    if any(isinstance(item, Mapping) and item.get("constraint_id", item.get("id")) == identifier for item in existing):
        _raise(f"duplicate constraint_id: {identifier}")
    value["constraints"] = [copy.deepcopy(item) for item in existing] + [candidate]
    _set_timestamp(value, now_ms)
    return value


add_verified_constraint = record_verified_constraint


def export_decision_store_constraints(state: Any) -> list[dict[str, Any]]:
    """Return only reusable verified constraints after whole-task acceptance."""

    value = _normalize_state(state)
    if value["status"] != "accepted":
        return []
    raw = value["constraints"] if isinstance(value["constraints"], list) else [value["constraints"]]
    exported: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping) or item.get("verified") is not True or item.get("reusable", True) is not True:
            continue
        identifier = item.get("constraint_id", item.get("id"))
        statement = item.get("statement", item.get("rule"))
        if not isinstance(identifier, str) or not isinstance(statement, str):
            continue
        row: dict[str, Any] = {
            "constraint_id": identifier,
            "statement": statement,
            "verified": True,
            "reusable": True,
        }
        for key in ("scope", "kind", "value", "confidence"):
            if key in item:
                row[key] = copy.deepcopy(item[key])
        _validate_json_safe(row, path="decision_store_constraint")
        exported.append(row)
    return sorted(exported, key=lambda item: item["constraint_id"])


decision_store_export = export_decision_store_constraints
export_verified_constraints = export_decision_store_constraints


def shadow_prediction_aggregates(state: Any, predictions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Compute observe/shadow aggregates without executing a model."""

    value = _normalize_state(state)
    if predictions is None:
        predictions = {}
    if not isinstance(predictions, Mapping):
        _raise("predictions must be an object")
    by_id = _nodes_by_id(value)
    route_counts: Counter[str] = Counter()
    aggregate = {
        "eligible_count": len(ready_nodes(value)),
        "predicted_accept_count": 0,
        "predicted_failure_count": 0,
        "predicted_retry_count": 0,
        "predicted_steps": len(predictions),
        "predicted_child_runs": 0,
        "route_counts": route_counts,
    }
    for identifier, prediction in predictions.items():
        if identifier not in by_id:
            _raise(f"prediction references unknown unit: {identifier}")
        if isinstance(prediction, Mapping):
            status = str(prediction.get("status", prediction.get("prediction", "unknown"))).lower()
            route = prediction.get("route", prediction.get("lane"))
            runs = prediction.get("child_runs", 1)
        else:
            status = str(prediction).lower()
            route = None
            runs = 1
        if status in {"accept", "accepted", "success", "pass", "passed"}:
            aggregate["predicted_accept_count"] += 1
        elif status in {"retry", "reopened", "correct"}:
            aggregate["predicted_retry_count"] += 1
        elif status in {"fail", "failed", "failure", "error"}:
            aggregate["predicted_failure_count"] += 1
        if isinstance(runs, int) and not isinstance(runs, bool) and runs >= 0:
            aggregate["predicted_child_runs"] += min(runs, MAX_ATTEMPTS)
        if isinstance(route, str):
            route_counts[_safe_enum(route)] += 1
    aggregate["route_counts"] = dict(sorted(route_counts.items()))
    return aggregate


observe_prediction_aggregates = shadow_prediction_aggregates
shadow_predictions = shadow_prediction_aggregates


def record_shadow_predictions(state: Any, predictions: Mapping[str, Any] | None = None, *, now_ms: int | None = None) -> dict[str, Any]:
    value = _normalize_state(state)
    value["shadow"] = shadow_prediction_aggregates(value, predictions)
    _set_timestamp(value, now_ms)
    return value


def to_d1_summary(state: Any) -> dict[str, Any]:
    """Export the exact graph-runs v4 aggregate boundary for D1.

    The adapter contains only counts, bounded enums, timing/token aggregates,
    and opaque identifiers.  Objectives, ownership, schemas, prompts, source
    contents, paths, packets, outputs, and constraint statements never cross
    this boundary.
    """

    value = _normalize_state(state)
    status_counts = Counter(unit["status"] for unit in value["nodes"])
    counters = value["counters"]
    shadow = value.get("shadow") if isinstance(value.get("shadow"), Mapping) else {}
    shadow_active = (
        value.get("mode") == "observe"
        and int(shadow.get("predicted_steps", 0) or 0) > 0
        and int(counters.get("graph_steps", 0) or 0) == 0
    )
    timestamps = value["timestamps"]
    raw_status = value["status"]
    summary_status = {
        "pending": "COMPILED",
        "running": "RUNNING",
        "retry_pending": "RUNNING",
        "accepted": "ACCEPTED",
        "failed": "FAILED",
        "convergence_failed": "CONVERGENCE_FAILED",
        "stalled": "FAILED",
    }.get(raw_status, "FAILED")
    mode_gate_value = value.get("mode_gate", {})
    fallback_used = bool(mode_gate_value.get("current_routing_fallback", True))
    if value.get("requested_mode") == "enforce" and fallback_used and summary_status in {"COMPILED", "RUNNING"}:
        summary_status = "BLOCKED"
    requested_mode = value.get("requested_mode", value.get("mode", DEFAULT_MODE))
    if requested_mode not in GRAPH_MODES:
        requested_mode = DEFAULT_MODE
    gate_status = "not_run"
    if requested_mode == "enforce":
        gate_status = "passed" if bool(mode_gate_value.get("enforce_allowed")) else "failed"
    started = timestamps.get("started_at_ms")
    observed_at = value.get("observed_at")
    if not isinstance(observed_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T[^\s]{8,32}Z", observed_at):
        updated_at_ms = timestamps.get("updated_at_ms")
        if not isinstance(updated_at_ms, int) or isinstance(updated_at_ms, bool):
            updated_at_ms = int(time.time() * 1000)
        observed_at = dt.datetime.fromtimestamp(updated_at_ms / 1000, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    ended = timestamps.get("ended_at_ms")
    duration = None
    if isinstance(started, int) and isinstance(ended, int):
        duration = max(0, ended - started)
    token_input = counters.get("tokens_input", counters.get("input_tokens"))
    token_output = counters.get("tokens_output", counters.get("output_tokens"))
    token_total = counters.get("tokens_total", counters.get("total_tokens"))
    tokens_measured = counters.get("tokens_measured") is True or any(
        isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in (token_input, token_output, token_total)
    )
    if tokens_measured:
        token_total = token_total if isinstance(token_total, int) else int(token_input or 0) + int(token_output or 0)
    else:
        token_total = None
    reason = value.get("failure_reason")
    reason_map = {
        None: "none",
        "": "none",
        "max_graph_steps": "max_steps",
        "max_total_child_runs": "max_child_runs",
        "max_wall_time_ms": "max_wall_time",
        "retry_budget_exhausted": "retry_budget",
        "max_attempts": "max_attempts",
    }
    convergence_reason = reason_map.get(reason, "invalid_graph" if reason == "invalid_graph" else "none")
    # D1 receives only an aggregate structural projection.  ``graph_id`` and
    # node IDs stay local; ordinals/timeline data live in the companion node
    # and event families.
    gate_values = value.get("gate_results") if isinstance(value.get("gate_results"), Mapping) else {}
    gate_states = [str(item.get("status", item.get("result", ""))).upper() for item in gate_values.values() if isinstance(item, Mapping)]
    dependencies = {str(node["unit_id"]): [str(dep) for dep in node.get("dependencies", [])] for node in value["nodes"]}
    depths: dict[str, int] = {}
    def depth(identifier: str) -> int:
        if identifier in depths:
            return depths[identifier]
        depths[identifier] = 1 + max((depth(dep) for dep in dependencies.get(identifier, []) if dep in dependencies), default=0)
        return depths[identifier]
    critical_path = max((depth(identifier) for identifier in dependencies), default=0)
    event_classification = value.get("event_classification", "normal")
    if event_classification not in {"normal", "benchmark_run", "migration_event", "incident_response", "token_burn_event", "reset_expectation", "manual_stress_test", "release_validation"}:
        event_classification = "normal"
    summary = {
        "graph_run_id": opaque_id("graph", value["graph_id"]),
        "device_id": opaque_id("device", value.get("device_id", value["graph_id"])),
        "observed_at": observed_at,
        "event_classification": event_classification,
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "mode": value.get("mode", DEFAULT_MODE),
        "status": summary_status,
        "graph_revision_count": max(1, int(value.get("graph_revision", 1))),
        "node_count": len(value["nodes"]),
        "edge_count": len(value["edges"]),
        "parallel_width": min(int(shadow.get("eligible_count", 0)), int(value["limits"]["max_parallelism"])) if shadow_active else int(counters.get("parallel_width", 0)),
        "critical_path_length": critical_path,
        "attempt_count": int(counters.get("attempts", 0)),
        "retry_count": int(shadow.get("predicted_retry_count", 0)) if shadow_active else int(counters.get("retries", 0)),
        "send_back_count": int(value.get("send_back_count", 0)),
        "accepted_count": int(shadow.get("predicted_accept_count", 0)) if shadow_active else status_counts.get("accepted", 0),
        "frozen_count": status_counts.get("frozen", 0),
        "failed_count": int(shadow.get("predicted_failure_count", 0)) if shadow_active else status_counts.get("failed", 0),
        "invalidated_count": int(shadow.get("predicted_retry_count", 0)) if shadow_active else status_counts.get("invalidated", 0) + status_counts.get("reopened", 0),
        "constraint_count": len(value["constraints"] if isinstance(value["constraints"], list) else [value["constraints"]]),
        "checkpoint_count": int(counters.get("checkpoints", 0)),
        "gate_pass_count": sum(item == "PASS" or item == "PASSED" for item in gate_states),
        "gate_fail_count": sum(item == "FAIL" or item == "FAILED" for item in gate_states),
        "gate_inconclusive_count": sum(item == "INCONCLUSIVE" for item in gate_states),
        "duration_ms": duration,
        "input_tokens": token_input if tokens_measured and isinstance(token_input, int) else None,
        "cached_input_tokens": counters.get("tokens_cached") if tokens_measured and isinstance(counters.get("tokens_cached"), int) else None,
        "output_tokens": token_output if tokens_measured and isinstance(token_output, int) else None,
        "reasoning_tokens": counters.get("tokens_reasoning") if tokens_measured and isinstance(counters.get("tokens_reasoning"), int) else None,
    }
    _validate_json_safe(summary, path="d1_summary")
    return summary


summary_for_d1 = to_d1_summary
to_d1 = to_d1_summary


def apply_primitive(state: Any, primitive: str, *, unit_id: str | None = None, payload: Any = None, now_ms: int | None = None) -> Any:
    """Apply a deterministic graph primitive at the adapter boundary."""

    if primitive not in GRAPH_PRIMITIVES:
        _raise(f"unknown graph primitive: {primitive}")
    value = _normalize_state(state)
    if primitive in {"split", "fan_out", "sequence"}:
        return advance_graph(value, now_ms=now_ms)
    if primitive == "merge":
        return merge_accepted_outputs(value, payload if isinstance(payload, list) else None)
    if primitive == "gate":
        return mode_gate(value.get("requested_mode", DEFAULT_MODE), payload)
    if primitive == "send_back":
        if not isinstance(unit_id, str):
            _raise("send_back requires unit_id")
        return fail_unit(value, unit_id, payload or "send_back", now_ms=now_ms)
    # accept
    return finalize_graph(value, now_ms=now_ms)


def validate_primitive(primitive: Any, *, raise_on_error: bool = False) -> bool:
    try:
        if primitive not in GRAPH_PRIMITIVES:
            _raise("unknown graph primitive")
        return True
    except GraphValidationError:
        if raise_on_error:
            raise
        return False


__all__ = [
    "GRAPH_SCHEMA_VERSION",
    "DEFAULT_MODE",
    "GRAPH_MODES",
    "MODES",
    "GRAPH_PRIMITIVES",
    "PRIMITIVES",
    "UNIT_STATUSES",
    "GRAPH_STATUSES",
    "ATTEMPT_STAGES",
    "GRAPH_RUN_STATUSES",
    "GATE_STATUSES",
    "CONVERGENCE_REASONS",
    "HARDENING_GATE_A_REQUIRED_CHECKS",
    "GRAPH_RUN_SUMMARY_FIELDS",
    "GraphError",
    "GraphValidationError",
    "GraphConvergenceError",
    "Unit",
    "make_unit",
    "canonical_json",
    "stable_hash",
    "stable_id",
    "opaque_id",
    "stable_sort",
    "validate_schema",
    "validate_schema_or_raise",
    "validate_value",
    "validate_output",
    "validate_input",
    "validate_unit",
    "validate_unit_or_raise",
    "normalize_unit",
    "create_graph",
    "new_graph",
    "initialize_graph",
    "build_graph",
    "validate_graph",
    "validate_graph_or_raise",
    "normalize_graph",
    "find_cycles",
    "detect_cycles",
    "has_cycle",
    "topological_order",
    "validate_dependencies",
    "transitive_dependents",
    "ready_nodes",
    "ownership_conflicts",
    "detect_ownership_conflicts",
    "parallel_ready_nodes",
    "select_ready_nodes",
    "retry_accounting",
    "hardening_gate_a_passed",
    "mode_gate",
    "resolve_mode",
    "enforce_mode_allowed",
    "fail_unit",
    "retry_unit",
    "complete_unit",
    "accept_unit",
    "record_unit_attempt",
    "record_attempt",
    "advance_graph",
    "advance",
    "finalize_graph",
    "accept_graph",
    "graph_is_accepted",
    "structurally_compatible_merge",
    "merge_outputs",
    "compatible_merge",
    "merge_accepted_outputs",
    "record_verified_constraint",
    "add_verified_constraint",
    "export_decision_store_constraints",
    "decision_store_export",
    "export_verified_constraints",
    "shadow_prediction_aggregates",
    "observe_prediction_aggregates",
    "shadow_predictions",
    "record_shadow_predictions",
    "to_d1_summary",
    "summary_for_d1",
    "to_d1",
    "apply_primitive",
    "validate_primitive",
]
