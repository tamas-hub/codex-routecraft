"""Privacy-safe projections from local graph state to collector v4 families.

This module deliberately accepts only already-structured graph summaries.  It
never reads prompts, files, memory/decision text, worker packets, or outputs.
"""
from __future__ import annotations

import hashlib
from typing import Mapping, Sequence

FORBIDDEN = {"prompt", "conversation", "source", "path", "file", "secret", "raw_worker_packet", "raw_node_output", "memory_text", "decision_text"}
NODE_GATE = {"passed": "PASS", "pass": "PASS", "failed": "FAIL", "fail": "FAIL", "inconclusive": "INCONCLUSIVE", "not_run": "NOT_RUN"}
NODE_TYPE = {value.lower(): value for value in ("AGENT", "TOOL", "DETERMINISTIC", "GATE", "MERGE", "HUMAN_APPROVAL", "MEMORY_RECALL", "BENCHMARK", "SECURITY", "CHECKPOINT", "QUALITY")}
NODE_STATUS = {value.lower(): value for value in ("PENDING", "READY", "RUNNING", "ACCEPTED", "FROZEN", "FAILED", "INVALIDATED", "BLOCKED", "SKIPPED", "CANCELLED")}
NODE_STATUS.update({"reopened": "INVALIDATED", "retry_pending": "FAILED"})


def _enum(value: object, mapping: Mapping[str, str], label: str) -> str:
    normalized = mapping.get(str(value).lower())
    if normalized is None:
        raise ValueError(f"invalid {label}")
    return normalized


def _id(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:32]


def _safe(value: Mapping[str, object]) -> bool:
    return not any(key.lower() in FORBIDDEN for key in value)


def project(graph_run_id: str, device_id: str, observed_at: str, nodes: Sequence[Mapping[str, object]], events: Sequence[Mapping[str, object]]) -> dict[str, list[dict[str, object]]]:
    """Project safe node/event aggregates. Invalid or semantic payloads fail closed."""
    if not all(isinstance(value, str) and value for value in (graph_run_id, device_id, observed_at)):
        raise ValueError("graph telemetry identity is required")
    node_rows: list[dict[str, object]] = []
    event_rows: list[dict[str, object]] = []
    for index, node in enumerate(nodes, start=1):
        if not isinstance(node, Mapping) or not _safe(node):
            raise ValueError("unsafe graph node telemetry")
        node_rows.append({
            "node_metric_id": _id("node", graph_run_id, index), "graph_run_id": graph_run_id, "device_id": device_id, "observed_at": observed_at,
            "node_ordinal": index, "dependency_count": int(node.get("dependency_count", 0)),
            "node_type": _enum(node.get("node_type", "deterministic"), NODE_TYPE, "node type"), "lane": str(node.get("lane", "none")), "status": _enum(node.get("status", "pending"), NODE_STATUS, "node status"),
            "attempt_count": int(node.get("attempt_count", 0)), "gate_status": NODE_GATE.get(str(node.get("gate_status", "not_run")).lower(), str(node.get("gate_status", "NOT_RUN"))),
            "duration_ms": node.get("duration_ms"), "total_tokens": node.get("total_tokens"), "retry_count": int(node.get("retry_count", 0)),
            "send_back_count": None if node.get("send_back_count") is None else int(node["send_back_count"]),
            "accepted": bool(node.get("accepted", False)), "frozen": bool(node.get("frozen", False)), "invalidated": bool(node.get("invalidated", False)),
        })
    for index, event in enumerate(events, start=1):
        if not isinstance(event, Mapping) or not _safe(event):
            raise ValueError("unsafe graph event telemetry")
        event_rows.append({
            "graph_event_id": _id("event", graph_run_id, index), "graph_run_id": graph_run_id, "device_id": device_id, "observed_at": observed_at,
            "event_sequence": index, "event_type": str(event.get("event_type", "checkpoint")), "status": _enum(event.get("status", "running"), NODE_STATUS, "event status"),
            "node_ordinal": event.get("node_ordinal"), "source_node_ordinal": event.get("source_node_ordinal"), "target_node_ordinal": event.get("target_node_ordinal"), "gate_status": event.get("gate_status"), "attempt_count": int(event.get("attempt_count", 0)),
            "affected_node_count": int(event.get("affected_node_count", 0)), "constraint_count": int(event.get("constraint_count", 0)), "checkpoint_count": int(event.get("checkpoint_count", 0)),
        })
    return {"graph_node_metrics": node_rows, "graph_events": event_rows}
