"""Graph IR v1 parsing and canonical construction."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .canonical import sha256, stable_id, utc_now
from .constants import EDGE_TYPES, EVENT_CLASSIFICATIONS, GATE_RESULTS, GRAPH_SCHEMA_VERSION, GRAPH_STATUSES, MODES, NODE_STATUSES, NODE_TYPES
from .contracts import validate_schema, validate_verified_constraint


class GraphIRError(ValueError): pass

TOP_LEVEL_KEYS = {"graph_id", "graph_schema_version", "graph_revision", "policy_version", "task_class", "mode", "event_classification", "nodes", "edges", "contracts", "constraints", "budgets", "created_at", "updated_at", "status"}
NODE_KEYS = {"node_id", "node_type", "objective", "dependencies", "ownership", "input_schema", "output_schema", "lane", "reasoning_effort", "risk", "capability_profile", "allowed_tools", "denied_operations", "verification", "gate_policy", "retry_policy", "status", "attempt", "input_hash", "output_hash", "evidence_refs", "gate_result"}
EDGE_KEYS = {"from", "to", "edge_type", "condition", "data_contract"}


def _exact(mapping: Any, keys: set[str], name: str, *, optional: set[str] | None = None) -> list[str]:
    if not isinstance(mapping, dict): return [f"{name} must be object"]
    optional = optional or set()
    missing, unknown = keys - optional - set(mapping), set(mapping) - keys
    result: list[str] = []
    if missing: result.append(f"{name} missing keys: {', '.join(sorted(missing))}")
    if unknown: result.append(f"{name} unknown keys: {', '.join(sorted(unknown))}")
    return result


def validate_node(node: Any) -> list[str]:
    # gate_result was added as a runtime-only field after Graph IR v1 shipped.
    # It is optional on input and canonicalized to null, so historical v1 plans
    # remain readable while all compiled plans have an explicit verdict slot.
    errors = _exact(node, NODE_KEYS, "node", optional={"gate_result"})
    if not isinstance(node, dict): return errors
    if not isinstance(node.get("node_id"), str) or not node.get("node_id"): errors.append("node_id invalid")
    if node.get("node_type") not in NODE_TYPES: errors.append("node_type invalid")
    if node.get("status") not in NODE_STATUSES: errors.append("node status invalid")
    if node.get("gate_result") is not None and node.get("gate_result") not in GATE_RESULTS: errors.append("node gate result invalid")
    if node.get("node_type") != "GATE" and node.get("gate_result") is not None: errors.append("non-gate has gate result")
    if node.get("risk") not in {"low", "medium", "high", "critical"}: errors.append("node risk invalid")
    if not isinstance(node.get("dependencies"), list) or not all(isinstance(v, str) for v in node.get("dependencies", [])): errors.append("node dependencies invalid")
    ownership = node.get("ownership")
    if not isinstance(ownership, dict) or set(ownership) != {"workstream", "write_scopes"} or not isinstance(ownership.get("workstream"), str) or not isinstance(ownership.get("write_scopes"), list) or not all(isinstance(v, str) and v and not v.startswith(("/", "\\")) and ":" not in v for v in ownership.get("write_scopes", [])): errors.append("node ownership invalid")
    if not validate_schema(node.get("input_schema")) or not validate_schema(node.get("output_schema")): errors.append("node schema invalid")
    if not isinstance(node.get("attempt"), int) or isinstance(node["attempt"], bool) or node["attempt"] < 0: errors.append("node attempt invalid")
    if not isinstance(node.get("objective"), str) or not node["objective"]: errors.append("node objective invalid")
    if not isinstance(node.get("lane"), str) or not isinstance(node.get("reasoning_effort"), str) or not isinstance(node.get("capability_profile"), str): errors.append("node capability invalid")
    if not all(isinstance(node.get(key), list) and all(isinstance(item, str) for item in node[key]) for key in ("allowed_tools", "denied_operations", "evidence_refs")): errors.append("node list invalid")
    digest = r"sha256:[0-9a-f]{64}"
    if node.get("input_hash") is not None and (not isinstance(node.get("input_hash"), str) or not re.fullmatch(digest, node["input_hash"])): errors.append("node input hash invalid")
    if node.get("output_hash") is not None and (not isinstance(node.get("output_hash"), str) or not re.fullmatch(digest, node["output_hash"])): errors.append("node output hash invalid")
    verification = node.get("verification")
    if not isinstance(verification, dict) or set(verification) != {"required_evidence_types"} or not isinstance(verification.get("required_evidence_types"), list) or not all(isinstance(v, str) and v for v in verification.get("required_evidence_types", [])): errors.append("node verification invalid")
    gate = node.get("gate_policy")
    if not isinstance(gate, dict) or set(gate) != {"required", "inconclusive", "global"} or not isinstance(gate.get("required"), bool) or gate.get("inconclusive") != "FAIL" or not isinstance(gate.get("global"), bool): errors.append("node gate policy invalid")
    policy = node.get("retry_policy")
    expected_retry = {"max_attempts", "max_tokens", "max_duration_seconds", "max_failed_gates"}
    if not isinstance(policy, dict) or set(policy) != expected_retry or not isinstance(policy.get("max_attempts"), int) or isinstance(policy.get("max_attempts"), bool) or policy.get("max_attempts", 0) < 1 or any(policy.get(key) is not None and (not isinstance(policy[key], int) or isinstance(policy[key], bool) or policy[key] < 0) for key in expected_retry - {"max_attempts"}): errors.append("node retry policy invalid")
    if node.get("node_type") == "AGENT" and node.get("lane") == "none": errors.append("agent needs lane")
    if node.get("node_type") in {"MERGE", "QUALITY"} and node.get("lane") == "none" and node.get("capability_profile") != "deterministic-v1": errors.append("deterministic merge/quality capability invalid")
    return errors


def normalized_edge_condition(edge_type: str, condition: Any) -> Any:
    """Canonical, deterministic condition form for Graph IR v1.

    `null` on historical gate edges was always intended to mean the matching
    edge type.  It is accepted only at the parser boundary and rewritten here;
    a planner cannot use it to smuggle in a natural-language predicate.
    """
    if edge_type == "gate_pass" and condition is None:
        return {"kind": "gate_result", "equals": "PASS"}
    if edge_type == "gate_fail" and condition is None:
        return {"kind": "gate_result", "equals": "FAIL"}
    return condition


def validate_edge_condition(edge_type: Any, condition: Any) -> list[str]:
    if edge_type in {"depends_on", "fan_out", "sequence", "merge"}:
        return [] if condition is None else ["ordinary dependency condition must be null"]
    if edge_type in {"gate_pass", "gate_fail"}:
        condition = normalized_edge_condition(edge_type, condition)
        expected = "PASS" if edge_type == "gate_pass" else "FAIL"
        return [] if condition == {"kind": "gate_result", "equals": expected} else ["gate edge condition must be typed gate_result predicate"]
    if edge_type == "send_back":
        if not isinstance(condition, dict) or set(condition) != {"kind", "on", "max_transitions"}:
            return ["send_back condition must be typed bounded control transition"]
        if condition.get("kind") != "control_transition" or condition.get("on") != "FAIL":
            return ["send_back condition must trigger on FAIL"]
        maximum = condition.get("max_transitions")
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
            return ["send_back max_transitions invalid"]
        return []
    if edge_type == "constraint_feedback":
        if not isinstance(condition, dict) or set(condition) != {"kind", "constraint_id"}:
            return ["constraint feedback condition must be typed"]
        if condition.get("kind") != "verified_constraint" or not isinstance(condition.get("constraint_id"), str) or not condition["constraint_id"]:
            return ["constraint feedback condition invalid"]
        return []
    return ["edge type invalid"]


def validate_edge(edge: Any) -> list[str]:
    errors = _exact(edge, EDGE_KEYS, "edge")
    if not isinstance(edge, dict): return errors
    if not isinstance(edge.get("from"), str) or not isinstance(edge.get("to"), str): errors.append("edge endpoint invalid")
    if edge.get("edge_type") not in EDGE_TYPES: errors.append("edge type invalid")
    errors.extend(validate_edge_condition(edge.get("edge_type"), edge.get("condition")))
    if not isinstance(edge.get("data_contract"), dict) or set(edge["data_contract"]) - {"producer", "consumer"}: errors.append("edge data contract invalid")
    return errors


def validate_ir_shape(ir: Any) -> list[str]:
    errors = _exact(ir, TOP_LEVEL_KEYS, "graph")
    if not isinstance(ir, dict): return errors
    if ir.get("graph_schema_version") != GRAPH_SCHEMA_VERSION: errors.append("unknown graph schema version")
    if not isinstance(ir.get("graph_id"), str) or not ir["graph_id"].startswith("g_"): errors.append("graph_id invalid")
    if not isinstance(ir.get("graph_revision"), int) or ir["graph_revision"] < 1: errors.append("graph revision invalid")
    if ir.get("mode") not in MODES: errors.append("mode invalid")
    if ir.get("event_classification") not in EVENT_CLASSIFICATIONS: errors.append("event classification invalid")
    if ir.get("status") not in GRAPH_STATUSES: errors.append("graph status invalid")
    if not isinstance(ir.get("nodes"), list) or not ir["nodes"]: errors.append("nodes invalid")
    else:
        for node in ir["nodes"]: errors.extend(validate_node(node))
    if not isinstance(ir.get("edges"), list): errors.append("edges invalid")
    else:
        for edge in ir["edges"]: errors.extend(validate_edge(edge))
    contracts = ir.get("contracts")
    if not isinstance(contracts, dict) or set(contracts) != {"intent", "global_acceptance"} or not isinstance(contracts.get("global_acceptance"), list):
        errors.append("graph contracts invalid")
    elif isinstance(contracts.get("intent"), dict) and contracts["global_acceptance"] != contracts["intent"].get("acceptance_criteria"):
        errors.append("global acceptance must match intent acceptance criteria")
    budgets = ir.get("budgets")
    if not isinstance(budgets, dict) or set(budgets) != {"max_tokens", "max_duration_seconds", "max_child_runs"}:
        errors.append("graph budgets invalid")
    if not isinstance(ir.get("constraints"), list):
        errors.append("graph constraints invalid")
    else:
        for constraint in ir["constraints"]:
            errors.extend(validate_verified_constraint(constraint))
    if not isinstance(ir.get("policy_version"), str) or not ir["policy_version"]: errors.append("policy version invalid")
    if not isinstance(ir.get("task_class"), str) or not ir["task_class"]: errors.append("task class invalid")
    timestamp = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z"
    if not isinstance(ir.get("created_at"), str) or not re.fullmatch(timestamp, ir["created_at"]): errors.append("created_at invalid")
    if not isinstance(ir.get("updated_at"), str) or not re.fullmatch(timestamp, ir["updated_at"]): errors.append("updated_at invalid")
    return errors


def canonical_ir(ir: dict[str, Any]) -> dict[str, Any]:
    """Sort only order-insensitive collections; never mutate caller state."""
    result = deepcopy(ir)
    result["nodes"] = sorted(result["nodes"], key=lambda node: node["node_id"])
    result["edges"] = sorted(result["edges"], key=lambda edge: (edge["from"], edge["to"], edge["edge_type"]))
    for node in result["nodes"]:
        node["dependencies"] = sorted(set(node["dependencies"]))
        node["evidence_refs"] = sorted(set(node["evidence_refs"]))
        node["allowed_tools"] = sorted(set(node["allowed_tools"]))
        node["denied_operations"] = sorted(set(node["denied_operations"]))
        node["ownership"]["write_scopes"] = sorted(set(node["ownership"]["write_scopes"]))
        node.setdefault("gate_result", None)
    for edge in result["edges"]:
        edge["condition"] = normalized_edge_condition(edge["edge_type"], edge["condition"])
    return result


def ir_hash(ir: dict[str, Any]) -> str: return sha256(canonical_ir(ir))


def make_node(node_id: str, node_type: str, objective: str, *, dependencies: list[str] | None = None, lane: str = "none", write_scopes: list[str] | None = None, input_schema: dict[str, Any] | None = None, output_schema: dict[str, Any] | None = None, risk: str = "low", retry_policy: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    node = {"node_id": node_id, "node_type": node_type, "objective": objective, "dependencies": dependencies or [], "ownership": {"workstream": node_id, "write_scopes": write_scopes or []}, "input_schema": input_schema or {"type": "object"}, "output_schema": output_schema or {"type": "object"}, "lane": lane, "reasoning_effort": "none" if lane == "none" else "medium", "risk": risk, "capability_profile": "deterministic-v1" if lane == "none" else "agent-v1", "allowed_tools": [], "denied_operations": [], "verification": {"required_evidence_types": ["schema_result"]}, "gate_policy": {"required": True, "inconclusive": "FAIL", "global": False}, "retry_policy": retry_policy or {"max_attempts": 1, "max_tokens": None, "max_duration_seconds": 30, "max_failed_gates": 0}, "status": "PENDING", "attempt": 0, "input_hash": None, "output_hash": None, "evidence_refs": [], "gate_result": None}
    node.update(overrides)
    return node


def make_graph(task_class: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], intent: dict[str, Any], *, graph_id: str | None = None, mode: str = "observe", policy_version: str = "routecraft-production-v1", event_classification: str = "normal", budgets: dict[str, Any] | None = None, now: str | None = None) -> dict[str, Any]:
    created = now or utc_now()
    # graph_id identifies one execution, not a reusable template. Reusing a
    # deterministic task-class/node hash would collide ledgers, receipts and
    # Control Center history across otherwise distinct runs.
    graph_id = graph_id or stable_id("g")
    return canonical_ir({"graph_id": graph_id, "graph_schema_version": GRAPH_SCHEMA_VERSION, "graph_revision": 1, "policy_version": policy_version, "task_class": task_class, "mode": mode, "event_classification": event_classification, "nodes": nodes, "edges": edges, "contracts": {"intent": intent, "global_acceptance": intent.get("acceptance_criteria", [])}, "constraints": [], "budgets": budgets or {"max_tokens": None, "max_duration_seconds": None, "max_child_runs": None}, "created_at": created, "updated_at": created, "status": "DRAFT"})
