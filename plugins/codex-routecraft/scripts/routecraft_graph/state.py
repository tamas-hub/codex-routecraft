"""Immutable-ish graph state transitions and selective retry semantics."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .canonical import sha256, utc_now
from .constants import GATE_RESULTS, SUCCESS_STATUSES
from .scheduler import dependencies, downstream, inactive_conditional_targets, ready_nodes


class StateTransitionError(ValueError): pass


def _node(ir: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in ir["nodes"]:
        if node["node_id"] == node_id: return node
    raise StateTransitionError(f"unknown node {node_id}")


def recompute_input_hashes(ir: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(ir); by_id = {node["node_id"]: node for node in value["nodes"]}
    all_dependencies = dependencies(value)
    for node in value["nodes"]:
        # A Gate can produce the same artifact on PASS and FAIL.  Include its
        # persisted verdict so a branch/input idempotency key cannot be reused
        # across materially different control decisions.
        upstream = {
            dep: {
                "output_hash": by_id[dep].get("output_hash"),
                "gate_result": by_id[dep].get("gate_result"),
            }
            for dep in sorted(all_dependencies[node["node_id"]])
        }
        constraints = [constraint for constraint in value["constraints"] if node["node_id"] in constraint.get("applies_to", [])]
        contract = {key: node[key] for key in ("node_id", "node_type", "objective", "ownership", "input_schema", "output_schema", "lane", "reasoning_effort", "risk", "capability_profile", "allowed_tools", "denied_operations")}
        node["input_hash"] = sha256({"graph_id": value["graph_id"], "graph_revision": value["graph_revision"], "policy_version": value["policy_version"], "contract": contract, "upstream": upstream, "constraints": constraints})
    return value


def _clear_result(node: dict[str, Any]) -> None:
    node["output_hash"] = None
    node["gate_result"] = None
    # Historical Evidence Ledger rows remain append-only, but an invalidated
    # node must not carry their references forward as proof for a new input.
    node["evidence_refs"] = []


def _skip_unselected_branches(ir: dict[str, Any]) -> dict[str, Any]:
    """Make resolved-but-unselected gate paths terminal without executing them.

    Graph IR dependencies are AND prerequisites.  A non-selected gate edge
    therefore makes its target impossible.  We propagate that fact through
    ordinary nodes, stopping at MERGE because deterministic merge explicitly
    supports a skipped compatible input.
    """
    value = deepcopy(ir)
    by_id = {node["node_id"]: node for node in value["nodes"]}
    changed = True
    while changed:
        changed = False
        for node_id in inactive_conditional_targets(value):
            node = by_id[node_id]
            if node["status"] in {"PENDING", "READY", "INVALIDATED", "BLOCKED"}:
                node["status"] = "SKIPPED"; _clear_result(node); changed = True
        for node in value["nodes"]:
            if node["node_type"] == "MERGE" or node["status"] not in {"PENDING", "READY", "INVALIDATED", "BLOCKED"}:
                continue
            parents = dependencies(value)[node["node_id"]]
            if any(by_id[parent]["status"] == "SKIPPED" for parent in parents):
                node["status"] = "SKIPPED"; _clear_result(node); changed = True
    return value


def mark_ready(ir: dict[str, Any], max_parallelism: int = 3) -> dict[str, Any]:
    value = _skip_unselected_branches(recompute_input_hashes(ir))
    # READY is a derived scheduler state.  Clear stale claims first so an
    # upstream invalidation can never leave a startable downstream node.
    for node in value["nodes"]:
        if node["status"] == "READY":
            node["status"] = "PENDING"
    for node_id in ready_nodes(value, max_parallelism=max_parallelism): _node(value, node_id)["status"] = "READY"
    return value


def start_node(ir: dict[str, Any], node_id: str) -> dict[str, Any]:
    value = deepcopy(ir); node = _node(value, node_id)
    if node["status"] != "READY": raise StateTransitionError("node is not ready")
    node["status"] = "RUNNING"; node["attempt"] += 1; value["status"] = "RUNNING"; value["updated_at"] = utc_now()
    return value


def accept_node(
    ir: dict[str, Any],
    node_id: str,
    output: Any,
    evidence_refs: list[str],
    *,
    gate_result: str | None = None,
) -> dict[str, Any]:
    value = deepcopy(ir); node = _node(value, node_id)
    if node["status"] != "RUNNING": raise StateTransitionError("node cannot be accepted before RUNNING")
    if node["gate_policy"].get("required") and not evidence_refs: raise StateTransitionError("gate evidence required")
    if node["node_type"] != "GATE" and gate_result is not None:
        raise StateTransitionError("only GATE nodes may record a gate result")
    if gate_result not in GATE_RESULTS | {None}:
        raise StateTransitionError("gate result invalid")
    # Existing host callers accepted a GATE node without a separate result.
    # Treat that legacy call as PASS; the engine integration below always sends
    # the explicit verdict received from the gate evaluator.
    if node["node_type"] == "GATE":
        node["gate_result"] = "PASS" if gate_result is None else gate_result
    node["status"] = "ACCEPTED"; node["output_hash"] = sha256(output); node["evidence_refs"] = sorted(set(node["evidence_refs"] + evidence_refs)); value["updated_at"] = utc_now()
    value = mark_ready(value)
    global_gate = next((item for item in value["nodes"] if item["node_type"] == "GATE" and item["gate_policy"].get("global")), None)
    if global_gate and global_gate["status"] == "ACCEPTED" and global_gate.get("gate_result") == "PASS" and all(item["status"] in SUCCESS_STATUSES for item in value["nodes"]): value["status"] = "ACCEPTED"
    return value


def resolve_gate_result(
    ir: dict[str, Any],
    node_id: str,
    gate_result: str,
    output: Any,
    evidence_refs: list[str],
) -> dict[str, Any]:
    """Persist a PASS/FAIL/INCONCLUSIVE verdict and activate one typed branch.

    A failed evaluator is still a completed Gate node.  The verdict, rather
    than a broad FAILED state, selects `gate_fail`; `INCONCLUSIVE` is treated
    as FAIL for routing and never activates `gate_pass`.
    """
    if gate_result not in GATE_RESULTS:
        raise StateTransitionError("gate result invalid")
    value = accept_node(ir, node_id, output, evidence_refs, gate_result=gate_result)
    gate = _node(value, node_id)
    if gate["gate_policy"].get("global") and gate_result != "PASS":
        value["status"] = "FAILED"
    elif gate_result != "PASS":
        value["status"] = "RUNNING"
    return value


def fail_node(ir: dict[str, Any], node_id: str, reason: str) -> dict[str, Any]:
    value = deepcopy(ir); node = _node(value, node_id)
    if node["status"] not in {"RUNNING", "READY", "ACCEPTED", "FROZEN"}: raise StateTransitionError("node cannot fail")
    # Failure detail belongs in the append-only Progress Ledger, not the exact IR.
    node["status"] = "FAILED"
    for target in downstream(value, node_id):
        target_node = _node(value, target)
        if target_node["status"] in SUCCESS_STATUSES | {"READY", "RUNNING"}:
            target_node["status"] = "INVALIDATED"; _clear_result(target_node)
    for other in value["nodes"]:
        if other["node_id"] != node_id and other["status"] == "ACCEPTED" and other["node_id"] not in downstream(value, node_id): other["status"] = "FROZEN"
    value["status"] = "FAILED"; value["updated_at"] = utc_now()
    return value


def apply_send_back(ir: dict[str, Any], source_node_id: str) -> dict[str, Any]:
    """Apply a bounded `send_back` control edge without creating a DAG cycle.

    The source must already be a completed Gate with an effective failure.  A
    transition consumes the Gate attempt number, then fails only the target
    and its static downstream closure.  Accepted work outside that closure is
    frozen by `fail_node` and remains reusable.
    """
    value = deepcopy(ir)
    source = _node(value, source_node_id)
    if source["node_type"] != "GATE" or source["status"] not in {"ACCEPTED", "FROZEN"}:
        raise StateTransitionError("send_back requires completed GATE source")
    if source.get("gate_result") not in {"FAIL", "INCONCLUSIVE"}:
        raise StateTransitionError("send_back requires failed or inconclusive gate")
    matches = [
        edge for edge in value["edges"]
        if edge["edge_type"] == "send_back" and edge["from"] == source_node_id
        and isinstance(edge.get("condition"), dict)
        and edge["condition"].get("kind") == "control_transition"
        and edge["condition"].get("on") == "FAIL"
    ]
    if not matches:
        raise StateTransitionError("no active send_back transition")
    exhausted = [edge for edge in matches if source["attempt"] > edge["condition"]["max_transitions"]]
    if exhausted:
        raise StateTransitionError("NODE_CONVERGENCE_FAILED")
    # v1 permits multiple bounded correction targets; their effects compose as
    # a union of downstream closures.  Sort first so every state result is
    # stable irrespective of JSON edge order.
    for edge in sorted(matches, key=lambda item: item["to"]):
        target = _node(value, edge["to"])
        if target["status"] not in {"RUNNING", "READY", "ACCEPTED", "FROZEN"}:
            raise StateTransitionError("send_back target is not retryable")
        value = fail_node(value, target["node_id"], f"SEND_BACK:{source_node_id}")
    value["status"] = "RUNNING"
    value["updated_at"] = utc_now()
    return value


def retry_node(ir: dict[str, Any], node_id: str) -> dict[str, Any]:
    value = deepcopy(ir); node = _node(value, node_id)
    if node["status"] != "FAILED": raise StateTransitionError("only failed node can retry")
    if node["attempt"] >= node["retry_policy"]["max_attempts"]:
        raise StateTransitionError("NODE_CONVERGENCE_FAILED")
    node["status"] = "READY"; value["status"] = "RUNNING"; value["updated_at"] = utc_now(); return value


def add_constraint(ir: dict[str, Any], constraint: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(ir); value["constraints"] = sorted(value["constraints"] + [constraint], key=lambda item: item["constraint_id"])
    affected: set[str] = set(constraint.get("invalidates", []))
    for node_id in list(affected):
        affected.update(downstream(value, node_id))
    for node_id in sorted(affected):
        node = _node(value, node_id)
        if node["status"] in SUCCESS_STATUSES: node["status"] = "INVALIDATED"; _clear_result(node)
    # A verified constraint is a material execution transition even when it
    # invalidates no node.  Telemetry must be able to distinguish it from the
    # preceding state snapshot without relying on a checkpoint sequence.
    value["updated_at"] = utc_now()
    return recompute_input_hashes(value)


def replan(ir: dict[str, Any], reason: str) -> dict[str, Any]:
    value = deepcopy(ir); value["graph_revision"] += 1; value["status"] = "DRAFT"; value["updated_at"] = utc_now()
    return value
