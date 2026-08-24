"""Fail-closed static compiler for Graph IR v1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .constants import DEPENDENCY_EDGE_TYPES
from .contracts import validate_intent
from .ir import canonical_ir, validate_edge_condition, validate_ir_shape
from .policy import DEFAULT_LANE_REGISTRY, PolicyError, validate_config, validate_lane_registry


@dataclass(frozen=True)
class CompileIssue:
    code: str
    detail: str


class GraphValidationError(ValueError):
    def __init__(self, issues: list[CompileIssue]):
        self.issues = issues
        super().__init__("GRAPH_VALIDATION_FAILED: " + "; ".join(f"{i.code}: {i.detail}" for i in issues))


def _overlap(left: str, right: str) -> bool:
    a, b = left.strip("/"), right.strip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _reachable(node_ids: set[str], edges: list[dict[str, Any]]) -> set[str]:
    predecessors = {identifier: set() for identifier in node_ids}
    for edge in edges:
        if edge["edge_type"] in DEPENDENCY_EDGE_TYPES and edge["to"] in predecessors: predecessors[edge["to"]].add(edge["from"])
    roots = sorted(node for node, deps in predecessors.items() if not deps)
    seen, stack = set(roots), list(roots)
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        if edge["edge_type"] in DEPENDENCY_EDGE_TYPES: outgoing.setdefault(edge["from"], []).append(edge["to"])
    while stack:
        for target in outgoing.get(stack.pop(), []):
            if target not in seen: seen.add(target); stack.append(target)
    return seen


def _cycle(node_ids: set[str], edges: list[dict[str, Any]]) -> bool:
    outgoing = {node: [] for node in node_ids}
    for edge in edges:
        if edge["edge_type"] in DEPENDENCY_EDGE_TYPES and edge["from"] in outgoing and edge["to"] in outgoing: outgoing[edge["from"]].append(edge["to"])
    visiting, visited = set(), set()
    def visit(node: str) -> bool:
        if node in visiting: return True
        if node in visited: return False
        visiting.add(node)
        found = any(visit(next_node) for next_node in outgoing[node])
        visiting.remove(node); visited.add(node)
        return found
    return any(visit(node) for node in sorted(node_ids))


def _ordered_transitively(left: str, right: str, edges: list[dict[str, Any]]) -> bool:
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        if edge["edge_type"] in DEPENDENCY_EDGE_TYPES: outgoing.setdefault(edge["from"], []).append(edge["to"])
    def reaches(source: str, target: str) -> bool:
        seen, stack = set(), [source]
        while stack:
            current = stack.pop()
            if current == target and current != source: return True
            if current not in seen: seen.add(current); stack.extend(outgoing.get(current, []))
        return False
    return reaches(left, right) or reaches(right, left)


def _reaches(source: str, target: str, edges: list[dict[str, Any]]) -> bool:
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        if edge["edge_type"] in DEPENDENCY_EDGE_TYPES:
            outgoing.setdefault(edge["from"], []).append(edge["to"])
    seen: set[str] = set()
    stack = list(outgoing.get(source, []))
    while stack:
        current = stack.pop()
        if current == target:
            return True
        if current not in seen:
            seen.add(current)
            stack.extend(outgoing.get(current, []))
    return False


def _topological_order(node_ids: set[str], edges: list[dict[str, Any]]) -> list[str]:
    """Return a stable dependency-respecting order for deterministic execution."""
    incoming = {node_id: 0 for node_id in node_ids}
    outgoing = {node_id: [] for node_id in node_ids}
    for edge in edges:
        if edge["edge_type"] in DEPENDENCY_EDGE_TYPES and edge["from"] in node_ids and edge["to"] in node_ids:
            incoming[edge["to"]] += 1
            outgoing[edge["from"]].append(edge["to"])
    ready = sorted(node_id for node_id, count in incoming.items() if count == 0)
    result: list[str] = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for target in sorted(outgoing[current]):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
                ready.sort()
    return result


def _schema_compatible(producer: dict[str, Any], consumer: dict[str, Any]) -> bool:
    if producer.get("type") != "object" or consumer.get("type") != "object": return producer.get("type") == consumer.get("type")
    p, c = producer.get("properties", {}), consumer.get("properties", {})
    for name in consumer.get("required", []):
        if name not in p or p[name].get("type") != c.get(name, {}).get("type"): return False
    return True


def compile_graph(ir: dict[str, Any], *, lane_registry: dict[str, Any] | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    issues: list[CompileIssue] = [CompileIssue("IR_SCHEMA_INVALID", detail) for detail in validate_ir_shape(ir)]
    if issues: raise GraphValidationError(issues)
    try:
        validate_lane_registry(lane_registry or DEFAULT_LANE_REGISTRY)
        if config is not None: validate_config(config)
    except PolicyError as error:
        raise GraphValidationError([CompileIssue("CAPABILITY_INVALID", str(error))]) from error
    ir = canonical_ir(ir)
    nodes = ir["nodes"]; node_by_id = {node["node_id"]: node for node in nodes}; ids = [node["node_id"] for node in nodes]
    if len(ids) != len(set(ids)): issues.append(CompileIssue("NODE_ID_DUPLICATE", "duplicate node_id"))
    edge_keys = [(edge["from"], edge["to"], edge["edge_type"], str(edge["condition"]), tuple(sorted(edge["data_contract"].items()))) for edge in ir["edges"]]
    if len(edge_keys) != len(set(edge_keys)): issues.append(CompileIssue("IR_SCHEMA_INVALID", "duplicate edge"))
    intent_errors = validate_intent(ir["contracts"].get("intent"))
    if intent_errors: issues.extend(CompileIssue("IR_SCHEMA_INVALID", value) for value in intent_errors)
    if not ir["contracts"].get("global_acceptance"): issues.append(CompileIssue("ACCEPTANCE_CRITERIA_MISSING", "global acceptance empty"))
    dependency_pairs = {(edge["from"], edge["to"]) for edge in ir["edges"] if edge["edge_type"] in DEPENDENCY_EDGE_TYPES}
    for node in nodes:
        for dep in node["dependencies"]:
            if dep not in node_by_id: issues.append(CompileIssue("DEPENDENCY_MISSING", f"{node['node_id']} -> {dep}"))
            elif (dep, node["node_id"]) not in dependency_pairs: issues.append(CompileIssue("DEPENDENCY_MISSING", f"missing edge {dep} -> {node['node_id']}"))
        if node["gate_policy"].get("required") and node["node_type"] not in {"GATE", "HUMAN_APPROVAL"} and not node["verification"].get("required_evidence_types"):
            issues.append(CompileIssue("GATE_MISSING", node["node_id"]))
        if node["node_type"] == "HUMAN_APPROVAL" and "human_approval" not in node["verification"].get("required_evidence_types", []):
            issues.append(CompileIssue("APPROVAL_REQUIRED", f"{node['node_id']} requires human_approval evidence"))
        retry = node["retry_policy"]
        if retry["max_attempts"] < 1 or retry.get("max_duration_seconds") is not None and retry["max_duration_seconds"] < 0: issues.append(CompileIssue("RETRY_BUDGET_INVALID", node["node_id"]))
    for edge in ir["edges"]:
        if edge["from"] not in node_by_id or edge["to"] not in node_by_id: issues.append(CompileIssue("EDGE_ENDPOINT_INVALID", f"{edge['from']} -> {edge['to']}"))
        elif edge["edge_type"] in DEPENDENCY_EDGE_TYPES:
            if edge["from"] not in node_by_id[edge["to"]]["dependencies"]:
                issues.append(CompileIssue("DEPENDENCY_MISSING", f"node contract missing {edge['from']} -> {edge['to']}"))
            if not _schema_compatible(node_by_id[edge["from"]]["output_schema"], node_by_id[edge["to"]]["input_schema"]):
                issues.append(CompileIssue("DATA_CONTRACT_MISMATCH", f"{edge['from']} -> {edge['to']}"))
        condition_errors = validate_edge_condition(edge["edge_type"], edge["condition"])
        if condition_errors:
            issues.extend(CompileIssue("EDGE_CONDITION_INVALID", item) for item in condition_errors)
        if edge["edge_type"] == "gate_fail" and edge["from"] in node_by_id and node_by_id[edge["from"]]["node_type"] != "GATE":
            issues.append(CompileIssue("EDGE_CONDITION_INVALID", "gate_fail source must be a GATE node"))
        if edge["edge_type"] == "send_back":
            source, target = node_by_id.get(edge["from"]), node_by_id.get(edge["to"])
            if source and source["node_type"] != "GATE":
                issues.append(CompileIssue("SEND_BACK_INVALID", "send_back source must be a GATE node"))
            if source and target and not _reaches(target["node_id"], source["node_id"], ir["edges"]):
                issues.append(CompileIssue("SEND_BACK_INVALID", "send_back target must be upstream of its gate"))
            if target and isinstance(edge.get("condition"), dict):
                maximum = edge["condition"].get("max_transitions")
                # A send-back occurs after an already completed target
                # attempt, so each control transition needs one remaining
                # target attempt.  Equality would compile a dead correction
                # path that can never retry.
                if isinstance(maximum, int) and maximum >= target["retry_policy"]["max_attempts"]:
                    issues.append(CompileIssue("RETRY_BUDGET_INVALID", "send_back exceeds remaining target retry budget"))
                if source and isinstance(maximum, int) and maximum >= source["retry_policy"]["max_attempts"]:
                    issues.append(CompileIssue("RETRY_BUDGET_INVALID", "send_back exceeds remaining Gate evaluation budget"))
            if edge["data_contract"]:
                issues.append(CompileIssue("DATA_CONTRACT_MISMATCH", "send_back may not transfer data"))
    node_ids = set(ids)
    constraint_ids: set[str] = set()
    for constraint in ir["constraints"]:
        constraint_id = constraint["constraint_id"]
        if constraint_id in constraint_ids: issues.append(CompileIssue("IR_SCHEMA_INVALID", f"duplicate constraint {constraint_id}"))
        constraint_ids.add(constraint_id)
        unknown_nodes = (set(constraint["applies_to"]) | set(constraint["invalidates"])) - node_ids
        if unknown_nodes: issues.append(CompileIssue("DEPENDENCY_MISSING", f"constraint references {', '.join(sorted(unknown_nodes))}"))
    if node_ids and _cycle(node_ids, ir["edges"]): issues.append(CompileIssue("DEPENDENCY_CYCLE", "dependency graph contains cycle"))
    if node_ids and len(_reachable(node_ids, ir["edges"])) != len(node_ids): issues.append(CompileIssue("NODE_UNREACHABLE", "node disconnected from roots"))
    global_gates = [node for node in nodes if node["node_type"] == "GATE" and node["gate_policy"]["global"]]
    if len(global_gates) != 1: issues.append(CompileIssue("GATE_MISSING", "exactly one global gate is required"))
    else:
        global_id = global_gates[0]["node_id"]
        for node in nodes:
            if node["node_id"] != global_id and not _reaches(node["node_id"], global_id, ir["edges"]):
                issues.append(CompileIssue("NODE_UNREACHABLE", f"{node['node_id']} does not reach global gate"))
        if any(edge["from"] == global_id and edge["edge_type"] in DEPENDENCY_EDGE_TYPES for edge in ir["edges"]):
            issues.append(CompileIssue("GATE_MISSING", "global gate must be the dependency sink"))
    registry = lane_registry or DEFAULT_LANE_REGISTRY
    lanes = registry.get("lanes", {})
    for node in nodes:
        if node["lane"] != "none" and node["lane"] not in lanes: issues.append(CompileIssue("LANE_INVALID", node["node_id"]))
        elif node["lane"] != "none":
            lane = lanes[node["lane"]]
            if node["reasoning_effort"] not in lane["reasoning_levels"]: issues.append(CompileIssue("LANE_INVALID", f"{node['node_id']} reasoning"))
            order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
            if order[node["risk"]] > order[lane["risk_limit"]]: issues.append(CompileIssue("LANE_INVALID", f"{node['node_id']} risk"))
        elif node["reasoning_effort"] != "none": issues.append(CompileIssue("LANE_INVALID", f"{node['node_id']} deterministic reasoning"))
        if not node["capability_profile"]: issues.append(CompileIssue("CAPABILITY_INVALID", node["node_id"]))
        if node["node_type"] in {"TOOL", "AGENT"} and not isinstance(node["allowed_tools"], list): issues.append(CompileIssue("CAPABILITY_INVALID", node["node_id"]))
        if set(node["allowed_tools"]) & set(node["denied_operations"]): issues.append(CompileIssue("CAPABILITY_INVALID", f"{node['node_id']} allows a denied operation"))
        if config and node["retry_policy"]["max_attempts"] > config["graph"]["max_node_attempts"]: issues.append(CompileIssue("RETRY_BUDGET_INVALID", node["node_id"]))
    # potential concurrency must not share write scope unless an explicit dependency orders it.
    for index, left in enumerate(nodes):
        for right in nodes[index + 1:]:
            ordered = _ordered_transitively(left["node_id"], right["node_id"], ir["edges"])
            if not ordered and any(_overlap(a, b) for a in left["ownership"]["write_scopes"] for b in right["ownership"]["write_scopes"]): issues.append(CompileIssue("PARALLEL_WRITE_CONFLICT", f"{left['node_id']}, {right['node_id']}"))
            if left["ownership"].get("workstream") == right["ownership"].get("workstream") and left["ownership"].get("write_scopes") and right["ownership"].get("write_scopes") and not ordered: issues.append(CompileIssue("OWNERSHIP_CONFLICT", f"{left['node_id']}, {right['node_id']}"))
    mutations = ir["contracts"]["intent"].get("external_mutations", [])
    mutation_kinds = {item["kind"] for item in mutations}
    if len(mutation_kinds) != len(mutations): issues.append(CompileIssue("CAPABILITY_INVALID", "external mutation kinds must be unique"))
    for node in nodes:
        declared = {tool.split(":", 1)[1] for tool in node["allowed_tools"] if tool.startswith("external:") and ":" in tool}
        malformed = [tool for tool in node["allowed_tools"] if tool.startswith("external:") and tool.split(":", 1)[1] not in mutation_kinds]
        if malformed: issues.append(CompileIssue("CAPABILITY_INVALID", node["node_id"]))
        for kind in declared:
            if node["node_type"] not in {"TOOL", "AGENT"}: issues.append(CompileIssue("CAPABILITY_INVALID", node["node_id"]))
            approvals = [candidate for candidate in nodes if candidate["node_type"] == "HUMAN_APPROVAL" and f"approve:{kind}" in candidate["allowed_tools"]]
            if not any((candidate["node_id"], node["node_id"]) in {(edge["from"], edge["to"]) for edge in ir["edges"] if edge["edge_type"] == "gate_pass"} for candidate in approvals): issues.append(CompileIssue("APPROVAL_REQUIRED", f"{node['node_id']}:{kind}"))
    # An intent may not silently describe an operation that no node is capable of performing.
    for kind in mutation_kinds:
        if not any(f"external:{kind}" in node["allowed_tools"] for node in nodes): issues.append(CompileIssue("CAPABILITY_INVALID", f"external:{kind}"))
    budgets = ir["budgets"]
    for key in ("max_tokens", "max_duration_seconds", "max_child_runs"):
        if key in budgets and budgets[key] is not None and (not isinstance(budgets[key], int) or budgets[key] < 0): issues.append(CompileIssue("RESOURCE_BUDGET_INVALID", key))
    if budgets != ir["contracts"]["intent"].get("budget"):
        issues.append(CompileIssue("RESOURCE_BUDGET_INVALID", "graph budget must equal Intent Contract budget"))
    if config and ir["mode"] == "enforce":
        # Policy ids are opaque, versioned identifiers. Enforce mode uses
        # strict case-sensitive equality: no prefix, range, or "latest"
        # compatibility interpretation is permitted at this trust boundary.
        if ir["policy_version"] != config["policy"]["production_policy"]:
            issues.append(CompileIssue("POLICY_VERSION_MISMATCH", "graph policy_version does not match current production_policy"))
        if ir["task_class"] not in config["policy"]["allowlisted_task_classes"]:
            issues.append(CompileIssue("TASK_CLASS_NOT_ALLOWLISTED", ir["task_class"]))
    mode_order = {"off": 0, "observe": 1, "enforce": 2}
    if config and mode_order[ir["mode"]] > mode_order[config["graph"]["mode"]]:
        issues.append(CompileIssue("GRAPH_MODE_NOT_ENABLED", f"{ir['mode']} exceeds configured {config['graph']['mode']}"))
    if config and ir["mode"] == "enforce" and not config["graph"]["checkpoint"]: issues.append(CompileIssue("CAPABILITY_INVALID", "enforce mode requires checkpointing"))
    if issues: raise GraphValidationError(issues)
    return {"ir": ir, "node_order": _topological_order(node_ids, ir["edges"]), "dependency_pairs": sorted(dependency_pairs)}
