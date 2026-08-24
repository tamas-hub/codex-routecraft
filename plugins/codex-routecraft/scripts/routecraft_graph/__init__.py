"""Public, host-friendly RouteCraft 0.7 graph kernel facade."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from .compiler import GraphValidationError, compile_graph
from .constants import GRAPH_SCHEMA_VERSION, STORE_SCHEMA_VERSION
from .engine import GraphEngine
from .execution_boundary import EXECUTION_BOUNDARY_VERSION, ExecutionBoundary, ExecutorBinding
from .migration import load_config, migration_preview, save_default_config
from .policy import DEFAULT_LANE_REGISTRY, default_config, validate_config, validate_lane_registry
from .store import GraphStore, GraphStoreError, StoreIntegrityError
from .telemetry import privacy_projection


def validate_graph(ir: dict[str, Any], **kwargs: Any) -> dict[str, Any]: return compile_graph(ir, **kwargs)


def doctor_snapshot(config_path: str | Path | None = None, store_path: str | Path | None = None) -> dict[str, Any]:
    """Read-only status: missing config/store are reported, never created."""
    config = default_config(); config_status = "DEFAULT (not created)"
    if config_path is not None and Path(config_path).exists():
        config = load_config(config_path); config_status = "OK"
    store_status, checkpoint, resume = "NOT CREATED", "NOT AVAILABLE", "NOT AVAILABLE"
    candidate = Path(store_path).expanduser() if store_path is not None else None
    if candidate is not None and candidate.exists():
        try:
            store = GraphStore.open_read_only(candidate)
            store.verify_integrity(read_only=True); store_status, checkpoint, resume = "OK", "OK", "READY"
        except (GraphStoreError, sqlite3.Error, ValueError): store_status = checkpoint = resume = "FAIL CLOSED"
    return {"graph_engine": "OK", "graph_mode": config["graph"]["mode"], "graph_schema": f"v{GRAPH_SCHEMA_VERSION}", "state_store": store_status, "checkpoint": checkpoint, "resume": resume, "lane_registry": f"v{DEFAULT_LANE_REGISTRY['registry_version']}", "execution_boundary": "UNAVAILABLE", "trusted_evidence": "UNAVAILABLE", "policy_version": config["policy"]["production_policy"], "allowlist": list(config["policy"]["allowlisted_task_classes"]), "config": config_status}


__all__ = ["EXECUTION_BOUNDARY_VERSION", "ExecutionBoundary", "ExecutorBinding", "GraphEngine", "GraphStore", "GraphStoreError", "GraphValidationError", "StoreIntegrityError", "compile_graph", "default_config", "doctor_snapshot", "load_config", "migration_preview", "privacy_projection", "save_default_config", "validate_config", "validate_graph", "validate_lane_registry"]
