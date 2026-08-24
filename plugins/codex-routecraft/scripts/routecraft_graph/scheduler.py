"""Pure deterministic scheduling and critical-path calculations."""

from __future__ import annotations

from typing import Any

from .constants import DEPENDENCY_EDGE_TYPES, SUCCESS_STATUSES
from .ir import normalized_edge_condition


def dependencies(ir: dict[str, Any]) -> dict[str, set[str]]:
    result = {node["node_id"]: set(node["dependencies"]) for node in ir["nodes"]}
    for edge in ir["edges"]:
        if edge["edge_type"] in DEPENDENCY_EDGE_TYPES: result.setdefault(edge["to"], set()).add(edge["from"])
    return result


def gate_verdict(node: dict[str, Any]) -> str | None:
    """Return a deterministic branch verdict, never a model-evaluated value."""
    if node["node_type"] == "GATE":
        return node.get("gate_result")
    if node["status"] in {"ACCEPTED", "FROZEN"}:
        # A non-GATE `gate_pass` expresses successful completion, e.g. an
        # approved human boundary.  It cannot activate a gate-fail edge.
        return "PASS"
    return None


def effective_gate_verdict(node: dict[str, Any]) -> str | None:
    verdict = gate_verdict(node)
    if verdict == "INCONCLUSIVE":
        return "FAIL"
    return verdict


def edge_condition_state(edge: dict[str, Any], source: dict[str, Any]) -> str:
    """ACTIVE, WAITING, INACTIVE, or INVALID for one typed edge condition."""
    edge_type = edge["edge_type"]
    condition = normalized_edge_condition(edge_type, edge.get("condition"))
    if edge_type in {"depends_on", "fan_out", "sequence", "merge"}:
        return "ACTIVE" if condition is None else "INVALID"
    if edge_type in {"gate_pass", "gate_fail"}:
        if not isinstance(condition, dict):
            return "INVALID"
        verdict = effective_gate_verdict(source)
        if verdict is None:
            return "WAITING"
        return "ACTIVE" if verdict == condition.get("equals") else "INACTIVE"
    return "INVALID"


def incoming_edges(ir: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    return [edge for edge in ir["edges"] if edge["to"] == node_id and edge["edge_type"] in DEPENDENCY_EDGE_TYPES]


def inactive_conditional_targets(ir: dict[str, Any]) -> list[str]:
    """Targets whose AND prerequisite contains a resolved, unchosen branch."""
    by_id = {node["node_id"]: node for node in ir["nodes"]}
    return sorted({
        edge["to"]
        for edge in ir["edges"]
        if edge["edge_type"] in {"gate_pass", "gate_fail"}
        and edge_condition_state(edge, by_id[edge["from"]]) == "INACTIVE"
    })


def downstream(ir: dict[str, Any], source: str) -> list[str]:
    children: dict[str, list[str]] = {}
    for target, deps in dependencies(ir).items():
        for dep in deps: children.setdefault(dep, []).append(target)
    result: set[str] = set(); stack = list(children.get(source, []))
    while stack:
        current = stack.pop()
        if current not in result:
            result.add(current); stack.extend(children.get(current, []))
    return sorted(result)


def critical_path_lengths(ir: dict[str, Any]) -> dict[str, int]:
    deps = dependencies(ir); children: dict[str, list[str]] = {node: [] for node in deps}
    for node, parents in deps.items():
        for parent in parents: children.setdefault(parent, []).append(node)
    memo: dict[str, int] = {}
    def length(node: str) -> int:
        if node not in memo: memo[node] = 1 + max((length(child) for child in children.get(node, [])), default=0)
        return memo[node]
    return {node: length(node) for node in sorted(deps)}


def _scope_conflict(left: list[str], right: list[str]) -> bool:
    for a in left:
        for b in right:
            a, b = a.strip("/"), b.strip("/")
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"): return True
    return False


def ready_nodes(ir: dict[str, Any], *, active_node_ids: set[str] | None = None, max_parallelism: int = 3) -> list[str]:
    """Return a stable non-conflicting selection, not merely an unordered ready set."""
    if max_parallelism < 1: raise ValueError("max_parallelism must be positive")
    if active_node_ids is None:
        active_node_ids = {node["node_id"] for node in ir["nodes"] if node["status"] == "RUNNING"}
    available_slots = max_parallelism - len(active_node_ids)
    if available_slots <= 0:
        return []
    by_id = {node["node_id"]: node for node in ir["nodes"]}; deps = dependencies(ir); critical = critical_path_lengths(ir)
    candidates: list[dict[str, Any]] = []
    for node_id, node in by_id.items():
        if node["status"] not in {"PENDING", "INVALIDATED", "READY"}: continue
        if node["attempt"] >= node["retry_policy"]["max_attempts"]: continue
        edges = incoming_edges(ir, node_id)
        if any(edge_condition_state(edge, by_id[edge["from"]]) != "ACTIVE" for edge in edges):
            continue
        # A skipped branch is an intentional absence, not data.  It may be
        # consumed only by a deterministic MERGE node; all ordinary consumers
        # remain blocked until the state layer marks the branch skipped.
        def predecessor_ready(dependency: str) -> bool:
            status = by_id[dependency]["status"]
            return status in {"ACCEPTED", "FROZEN"} or (node["node_type"] == "MERGE" and status == "SKIPPED")
        if all(predecessor_ready(dependency) for dependency in deps[node_id]): candidates.append(node)
    risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    candidates.sort(key=lambda node: (-critical[node["node_id"]], risk_order[node["risk"]], node["node_id"]))
    selected: list[dict[str, Any]] = []
    active = [by_id[value] for value in active_node_ids if value in by_id]
    for node in candidates:
        conflicts = active + selected
        if any(_scope_conflict(node["ownership"]["write_scopes"], other["ownership"]["write_scopes"]) for other in conflicts): continue
        node_external = any(tool.startswith("external:") for tool in node["allowed_tools"])
        if node_external and any(any(tool.startswith("external:") for tool in other["allowed_tools"]) for other in conflicts): continue
        selected.append(node)
        if len(selected) >= available_slots: break
    return [node["node_id"] for node in selected]
