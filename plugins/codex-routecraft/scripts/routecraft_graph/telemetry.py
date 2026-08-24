"""Exact privacy-safe telemetry-v4 projection for durable Graph IR v1.

Only structural counts, bounded enums, opaque identifiers and nullable measured
metrics cross this boundary. Semantic contracts, objectives, paths, outputs,
evidence statements and ledger payloads deliberately remain local.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from .constants import DEPENDENCY_EDGE_TYPES
from .scheduler import critical_path_lengths, dependencies


def _id(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8", "replace")).hexdigest()[:32]


def _structural_width(ir: dict[str, Any]) -> int:
    deps = dependencies(ir)
    depth: dict[str, int] = {}

    def visit(node_id: str) -> int:
        if node_id not in depth:
            depth[node_id] = 1 + max((visit(parent) for parent in deps[node_id]), default=0)
        return depth[node_id]

    layers: dict[int, int] = {}
    for node_id in sorted(deps):
        level = visit(node_id)
        layers[level] = layers.get(level, 0) + 1
    return max(layers.values(), default=0)


def _usage_row(value: Mapping[str, int | None] | None) -> dict[str, int | None]:
    value = value or {}
    return {
        "duration_ms": value.get("duration_ms"),
        "input_tokens": value.get("input_tokens"),
        "cached_input_tokens": value.get("cached_input_tokens"),
        "output_tokens": value.get("output_tokens"),
        "reasoning_tokens": value.get("reasoning_tokens"),
        "total_tokens": value.get("total_tokens"),
    }


def privacy_projection(
    ir: dict[str, Any],
    *,
    gate_results: dict[str, str] | None = None,
    transition_events: list[Mapping[str, Any]] | None = None,
    graph_usage: Mapping[str, int | None] | None = None,
    node_usage: Mapping[str, Mapping[str, int | None]] | None = None,
    checkpoint_count: int = 0,
    send_back_count: int = 0,
    device_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return exact collector/Control Center v4 graph families."""
    gate_results = gate_results or {}
    node_usage = node_usage or {}
    graph_usage_row = _usage_row(graph_usage)
    nodes = sorted(ir["nodes"], key=lambda item: item["node_id"])
    ordinal = {node["node_id"]: index for index, node in enumerate(nodes, start=1)}
    graph_run_id = _id("graph-run", ir["graph_id"], ir["graph_revision"])
    transport_device_id = device_id or _id("local-device-placeholder", ir["graph_id"])
    observed_at = ir["updated_at"]
    critical = critical_path_lengths(ir)
    deps = dependencies(ir)

    def gate_result(node: dict[str, Any]) -> str:
        explicit = gate_results.get(node["node_id"])
        if explicit in {"PASS", "FAIL", "INCONCLUSIVE", "NOT_RUN"}:
            return explicit
        persisted = node.get("gate_result")
        if persisted in {"PASS", "FAIL", "INCONCLUSIVE"}:
            return persisted
        if node["node_type"] == "GATE" and node["status"] in {"ACCEPTED", "FROZEN"}:
            return "PASS"
        return "NOT_RUN"

    node_rows: list[dict[str, Any]] = []
    for node in nodes:
        measured = _usage_row(node_usage.get(node["node_id"]))
        status = node["status"]
        node_rows.append({
            "node_metric_id": _id("graph-node", graph_run_id, ordinal[node["node_id"]]),
            "graph_run_id": graph_run_id,
            "device_id": transport_device_id,
            "observed_at": observed_at,
            "node_ordinal": ordinal[node["node_id"]],
            "dependency_count": len(deps[node["node_id"]]),
            "node_type": node["node_type"],
            "lane": node["lane"],
            "status": status,
            "attempt_count": node["attempt"],
            "gate_status": gate_result(node),
            "duration_ms": measured["duration_ms"],
            "total_tokens": measured["total_tokens"],
            "retry_count": max(0, node["attempt"] - 1),
            # The durable store currently proves only the run-level total.
            # Do not invent per-node attribution from a declared control edge.
            "send_back_count": None,
            "accepted": status == "ACCEPTED",
            "frozen": status == "FROZEN",
            "invalidated": status == "INVALIDATED",
        })

    event_rows: list[dict[str, Any]] = []

    def event(event_type: str, status: str, *, node: int | None = None, source: int | None = None, target: int | None = None, gate: str | None = None, attempt: int = 0, affected: int = 0) -> None:
        sequence = len(event_rows) + 1
        event_rows.append({
            "graph_event_id": _id("graph-event", graph_run_id, sequence),
            "graph_run_id": graph_run_id,
            "device_id": transport_device_id,
            "observed_at": observed_at,
            "event_sequence": sequence,
            "event_type": event_type,
            "status": status,
            "node_ordinal": node,
            "source_node_ordinal": source,
            "target_node_ordinal": target,
            "gate_status": gate,
            "attempt_count": attempt,
            "affected_node_count": affected,
            "constraint_count": len(ir["constraints"]),
            "checkpoint_count": checkpoint_count,
        })

    by_id = {node["node_id"]: node for node in nodes}
    for edge in sorted(ir["edges"], key=lambda item: (item["from"], item["to"], item["edge_type"])):
        if edge["edge_type"] in DEPENDENCY_EDGE_TYPES:
            event("dependency", by_id[edge["to"]]["status"], source=ordinal[edge["from"]], target=ordinal[edge["to"]])
    for node in nodes:
        event("node_transition", node["status"], node=ordinal[node["node_id"]], attempt=node["attempt"], affected=1)
        result = gate_result(node)
        if transition_events is None and node["node_type"] == "GATE" and result != "NOT_RUN":
            event("gate", node["status"], node=ordinal[node["node_id"]], gate=result, attempt=node["attempt"], affected=1)
    if transition_events is not None:
        for item in transition_events:
            event_type = item.get("event_type")
            node_id = item.get("node_id")
            status = item.get("status")
            attempt = item.get("attempt_count")
            affected = item.get("affected_node_count")
            gate = item.get("gate_status")
            if (
                event_type not in {"gate", "send_back"}
                or node_id is not None and node_id not in ordinal
                or status not in {"PENDING", "READY", "RUNNING", "ACCEPTED", "FROZEN", "FAILED", "INVALIDATED", "BLOCKED", "SKIPPED", "CANCELLED"}
                or not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0
                or not isinstance(affected, int) or isinstance(affected, bool) or affected < 0
                or event_type == "gate" and gate not in {"PASS", "FAIL", "INCONCLUSIVE"}
                or event_type != "gate" and gate is not None
            ):
                raise ValueError("invalid authenticated transition projection")
            event(
                event_type,
                status,
                node=ordinal[node_id] if node_id is not None else None,
                gate=gate,
                attempt=attempt,
                affected=affected,
            )
    if checkpoint_count:
        event("checkpoint", "ACCEPTED" if ir["status"] == "ACCEPTED" else "RUNNING")

    accepted_count = sum(node["status"] == "ACCEPTED" for node in nodes)
    gate_values = (
        [item["gate_status"] for item in transition_events if item.get("event_type") == "gate"]
        if transition_events is not None
        else [gate_result(node) for node in nodes if node["node_type"] == "GATE"]
    )
    if transition_events is not None and sum(item.get("event_type") == "send_back" for item in transition_events) != send_back_count:
        raise ValueError("authenticated send-back history does not match checkpoint count")
    run_row = {
        "graph_run_id": graph_run_id,
        "device_id": transport_device_id,
        "observed_at": observed_at,
        "event_classification": ir["event_classification"],
        "graph_schema_version": ir["graph_schema_version"],
        "mode": ir["mode"],
        "status": ir["status"],
        "graph_revision_count": ir["graph_revision"],
        "node_count": len(nodes),
        "edge_count": len(ir["edges"]),
        "parallel_width": _structural_width(ir),
        "critical_path_length": max(critical.values(), default=0),
        "attempt_count": sum(node["attempt"] for node in nodes),
        "retry_count": sum(max(0, node["attempt"] - 1) for node in nodes),
        # This is the count of hash-chained `send_back` checkpoints, never the
        # number of declared control edges.
        "send_back_count": send_back_count,
        "accepted_count": accepted_count,
        "frozen_count": sum(node["status"] == "FROZEN" for node in nodes),
        "failed_count": sum(node["status"] == "FAILED" for node in nodes),
        "invalidated_count": sum(node["status"] == "INVALIDATED" for node in nodes),
        "constraint_count": len(ir["constraints"]),
        "checkpoint_count": checkpoint_count,
        "gate_pass_count": sum(value == "PASS" for value in gate_values),
        "gate_fail_count": sum(value == "FAIL" for value in gate_values),
        "gate_inconclusive_count": sum(value == "INCONCLUSIVE" for value in gate_values),
        "duration_ms": graph_usage_row["duration_ms"],
        "input_tokens": graph_usage_row["input_tokens"],
        "cached_input_tokens": graph_usage_row["cached_input_tokens"],
        "output_tokens": graph_usage_row["output_tokens"],
        "reasoning_tokens": graph_usage_row["reasoning_tokens"],
    }
    return {"graph_runs": [run_row], "graph_node_metrics": node_rows, "graph_events": event_rows}
