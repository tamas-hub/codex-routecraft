"""CLI adapter for the RouteCraft 0.7 durable graph kernel.

The kernel deliberately does not own model or tool execution.  ``graph run``
claims a ready node for the host, or records a host-produced structured result.
Every durable mutation goes through :class:`routecraft_graph.GraphEngine`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from routecraft_graph import (
    GraphEngine,
    GraphStore,
    GraphStoreError,
    GraphValidationError,
    default_config,
    doctor_snapshot,
    load_config,
    migration_preview,
    save_default_config,
    validate_graph,
)
from routecraft_graph.store import default_store_path
from routecraft_local.errors import RouteCraftLocalError


def default_config_path() -> Path:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return home / "routecraft" / "graph" / "config.json"


def _read_json(path: str | Path, label: str) -> Any:
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RouteCraftLocalError(f"{label} JSONを読めません: {exc}") from exc


def _write_json_new(path: str | Path, value: Any) -> Path:
    target = Path(path).expanduser().resolve()
    if target.exists():
        raise RouteCraftLocalError("出力先は既に存在します。新しいpathを指定してください。")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".routecraft-tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def _write_json_replace(path: str | Path, value: Any) -> Path:
    """Atomically replace a managed local summary file.

    Unlike a user-selected graph export, the collector snapshot is a rolling
    local cache.  It is deliberately replaced as one file so the optional
    collector never observes a half-written graph bundle.
    """
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".routecraft-tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


_COLLECTOR_GRAPH_FAMILIES = (
    "graph_runs",
    "graph_node_metrics",
    "graph_events",
)
MAX_COLLECTOR_GRAPH_ROWS = 75


def _collector_graph_bundle(telemetry: Any) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Return one coherent, privacy-safe Graph v4 bundle for the collector.

    Control Center accepts at most 75 rows for one graph projection.  Detailed
    node/event rows are not independently meaningful, so an oversized graph is
    represented by its exact graph-run summary only rather than split across
    requests.  This is a local export policy; Graph execution never depends on
    the optional Control Center cache being writable.
    """
    if not isinstance(telemetry, dict) or set(telemetry) != set(_COLLECTOR_GRAPH_FAMILIES):
        raise RouteCraftLocalError("Graph telemetry bundle is structurally invalid.")
    bundle: dict[str, list[dict[str, Any]]] = {}
    for family in _COLLECTOR_GRAPH_FAMILIES:
        rows = telemetry.get(family)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RouteCraftLocalError("Graph telemetry bundle is structurally invalid.")
        bundle[family] = [dict(row) for row in rows]
    if sum(len(bundle[family]) for family in _COLLECTOR_GRAPH_FAMILIES) <= MAX_COLLECTOR_GRAPH_ROWS:
        return bundle, False
    # A canonical export contains one latest graph run.  Retaining only its
    # run summary makes the boundary explicit without fabricating detail or
    # allowing a graph to be split incoherently.
    if len(bundle["graph_runs"]) > MAX_COLLECTOR_GRAPH_ROWS:
        raise RouteCraftLocalError("Graph telemetry bundle exceeds the supported summary limit.")
    return {
        "graph_runs": bundle["graph_runs"],
        "graph_node_metrics": [],
        "graph_events": [],
    }, True


def collector_bundle_path() -> Path:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return home / "routecraft" / "graph" / "latest-collector-v4.json"


def _materialize_collector_bundle(telemetry: Any) -> dict[str, Any]:
    """Best-effort local cache for the optional Unified Collector."""
    bundle, detail_downgraded = _collector_graph_bundle(telemetry)
    try:
        _write_json_replace(collector_bundle_path(), bundle)
    except OSError:
        # Control Center optionality is part of the Runtime product boundary.
        return {"collector_bundle_saved": False, "collector_bundle_detail_downgraded": detail_downgraded}
    return {"collector_bundle_saved": True, "collector_bundle_detail_downgraded": detail_downgraded}


def _refresh_collector_bundle(engine: GraphEngine, graph_id: str) -> dict[str, Any]:
    """Best-effort host-side projection after a durable CLI mutation.

    The Graph kernel never imports or calls the Collector.  This CLI/host
    adapter refreshes its rolling local bundle after state-changing commands;
    projection/cache failure is reported with a bounded code and cannot roll
    back or stop the already-durable local Graph transition.
    """
    try:
        return _materialize_collector_bundle(engine.export(graph_id)["telemetry"])
    except (GraphStoreError, RouteCraftLocalError, OSError, TypeError, ValueError, KeyError):
        return {
            "collector_bundle_saved": False,
            "collector_bundle_detail_downgraded": False,
            "collector_bundle_error": "projection_unavailable",
        }


def _effective_config(path: str | Path | None) -> tuple[dict[str, Any], Path, str]:
    target = Path(path).expanduser() if path else default_config_path()
    if target.exists():
        return load_config(target), target, "configured"
    return default_config(), target, "default_not_written"


def _decision_store_path() -> Path | None:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        value = json.loads((home / "routecraft" / "memory.json").read_text(encoding="utf-8-sig"))
        store = value.get("store") if isinstance(value, dict) else None
        return Path(store).expanduser().resolve() if isinstance(store, str) and store else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _store_path(config: dict[str, Any], override: str | Path | None) -> Path:
    configured = config.get("graph", {}).get("state_store")
    if override:
        return Path(override).expanduser().resolve()
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser().resolve()
    return default_store_path().resolve()


def _engine(
    *,
    config_path: str | Path | None,
    store_path: str | Path | None,
    data_dir: str | Path | None,
    create: bool,
) -> tuple[GraphEngine, dict[str, Any], Path, str]:
    config, resolved_config, config_state = _effective_config(config_path)
    resolved_store = _store_path(config, store_path)
    if not create and not resolved_store.exists():
        raise RouteCraftLocalError("Graph State Storeはまだ作成されていません。先に graph plan を実行してください。")
    forbidden: list[Path] = []
    if data_dir:
        forbidden.append(Path(data_dir).expanduser().resolve())
    decision = _decision_store_path()
    if decision:
        forbidden.append(decision)
    try:
        store = GraphStore(resolved_store, forbidden_roots=forbidden, create=create)
    except TypeError:
        # Compatibility during a source upgrade; a missing store is already
        # rejected above, so the older constructor cannot create unexpectedly.
        store = GraphStore(resolved_store, forbidden_roots=forbidden)
    return GraphEngine(store, config=config), config, resolved_config, config_state


def _summary(engine: GraphEngine, graph: dict[str, Any]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for node in graph["nodes"]:
        statuses[node["status"]] = statuses.get(node["status"], 0) + 1
    try:
        ready = engine.ready(graph["graph_id"])
    except (GraphStoreError, ValueError):
        ready = []
    return {
        "graph_id": graph["graph_id"],
        "graph_schema_version": graph["graph_schema_version"],
        "graph_revision": graph["graph_revision"],
        "mode": graph["mode"],
        "event_classification": graph["event_classification"],
        "status": graph["status"],
        "node_count": len(graph["nodes"]),
        "edge_count": len(graph["edges"]),
        "node_statuses": dict(sorted(statuses.items())),
        "ready": ready,
    }


def validate_file(path: str | Path, *, config_path: str | Path | None = None) -> dict[str, Any]:
    value = _read_json(path, "Graph IR")
    if not isinstance(value, dict):
        raise RouteCraftLocalError("Graph IRはJSON objectでなければなりません。")
    config, _, _ = _effective_config(config_path)
    try:
        compiled = validate_graph(value, config=config)
    except GraphValidationError as exc:
        issues = [{"code": issue.code, "detail": issue.detail} for issue in exc.issues]
        raise RouteCraftLocalError("Graph validation failed: " + json.dumps(issues, ensure_ascii=False)) from exc
    return {
        "valid": True,
        "graph_schema_version": value["graph_schema_version"],
        "graph_revision": value["graph_revision"],
        "node_count": len(value["nodes"]),
        "edge_count": len(value["edges"]),
        "node_order": compiled["node_order"],
    }


def plan(
    path: str | Path,
    *,
    config_path: str | Path | None,
    store_path: str | Path | None,
    data_dir: str | Path | None,
    mode: str | None = None,
) -> dict[str, Any]:
    ir = _read_json(path, "Graph IR")
    if not isinstance(ir, dict):
        raise RouteCraftLocalError("Graph IRはJSON objectでなければなりません。")
    engine, config, resolved_config, config_state = _engine(
        config_path=config_path, store_path=store_path, data_dir=data_dir, create=True,
    )
    if mode is not None:
        ir = dict(ir)
        ir["mode"] = mode
    elif ir.get("mode") is None:
        ir = dict(ir)
        ir["mode"] = config["graph"]["mode"]
    try:
        graph = engine.plan(ir)
    except GraphValidationError as exc:
        issues = [{"code": issue.code, "detail": issue.detail} for issue in exc.issues]
        raise RouteCraftLocalError("Graph validation failed: " + json.dumps(issues, ensure_ascii=False)) from exc
    except GraphStoreError as exc:
        # A CLI user needs the fail-closed execution-boundary reason, not the
        # generic top-level InternalError wrapper.
        raise RouteCraftLocalError(str(exc)) from exc
    return {
        **_summary(engine, graph),
        "config": str(resolved_config),
        "config_state": config_state,
        "state_store": str(engine.store.path),
        "checkpointed": True,
        **_refresh_collector_bundle(engine, graph["graph_id"]),
    }


def run(
    graph_id: str,
    *,
    config_path: str | Path | None,
    store_path: str | Path | None,
    data_dir: str | Path | None,
    node_id: str | None,
    result_path: str | Path | None,
    evidence_path: str | Path | None,
    usage_path: str | Path | None,
    gate_result: str,
    failure: str | None,
    retry: bool,
) -> dict[str, Any]:
    engine, _, _, _ = _engine(config_path=config_path, store_path=store_path, data_dir=data_dir, create=False)
    try:
        if node_id is None:
            graph = engine.status(graph_id)
            return _summary(engine, graph)
        graph_mode = engine.status(graph_id).get("mode")
        if graph_mode == "enforce" and (retry or failure is not None):
            raise RouteCraftLocalError(
                "ENFORCE_BOUNDARY_UNAVAILABLE: graph run --retry/--failure は"
                "trusted execution boundaryなしでは使用できません。"
            )
        if retry:
            graph = engine.retry(graph_id, node_id)
        elif failure is not None:
            usage = _read_json(usage_path, "Attempt usage") if usage_path is not None else None
            graph = engine.record_failure(graph_id, node_id, failure, usage=usage)
        elif result_path is not None or evidence_path is not None:
            if result_path is None or evidence_path is None:
                raise RouteCraftLocalError("result記録には --result と --evidence の両方が必要です。")
            if engine.status(graph_id).get("mode") == "enforce":
                raise RouteCraftLocalError("ENFORCE_BOUNDARY_UNAVAILABLE: graph run --result/--evidence はtrusted execution boundaryなしでは使用できません。")
            result = _read_json(result_path, "Node result")
            evidence = _read_json(evidence_path, "Evidence")
            if not isinstance(evidence, list):
                raise RouteCraftLocalError("EvidenceはJSON arrayでなければなりません。")
            usage = _read_json(usage_path, "Attempt usage") if usage_path is not None else None
            graph = engine.record_result(graph_id, node_id, result, evidence, gate_result=gate_result, usage=usage)
        elif usage_path is not None:
            raise RouteCraftLocalError("--usage は --result/--evidence または --failure と一緒に指定してください。")
        else:
            graph = engine.start(graph_id, node_id)
    except GraphStoreError as exc:
        raise RouteCraftLocalError(str(exc)) from exc
    return {**_summary(engine, graph), **_refresh_collector_bundle(engine, graph_id)}


def resume(graph_id: str, *, config_path: str | Path | None, store_path: str | Path | None, data_dir: str | Path | None) -> dict[str, Any]:
    engine, _, _, _ = _engine(config_path=config_path, store_path=store_path, data_dir=data_dir, create=False)
    try:
        graph = engine.resume(graph_id)
    except GraphStoreError as exc:
        raise RouteCraftLocalError(str(exc)) from exc
    return {**_summary(engine, graph), "resumed_from_checkpoint": True, **_refresh_collector_bundle(engine, graph_id)}


def approve(
    graph_id: str,
    node_id: str,
    confirmation: str,
    actor_ref: str,
    operation_path: str | Path,
    evidence_path: str | Path,
    usage_path: str | Path,
    *,
    config_path: str | Path | None,
    store_path: str | Path | None,
    data_dir: str | Path | None,
) -> dict[str, Any]:
    engine, _, _, _ = _engine(config_path=config_path, store_path=store_path, data_dir=data_dir, create=False)
    evidence = _read_json(evidence_path, "Human approval evidence")
    operation = _read_json(operation_path, "Operation descriptor")
    usage = _read_json(usage_path, "Attempt usage")
    if not isinstance(evidence, list): raise RouteCraftLocalError("Human approval evidenceはJSON arrayでなければなりません。")
    graph = engine.approve_human(graph_id, node_id, confirmation, actor_ref, operation, evidence, usage=usage)
    return {**_summary(engine, graph), "human_approval_recorded": True, **_refresh_collector_bundle(engine, graph_id)}


def status(graph_id: str, *, config_path: str | Path | None, store_path: str | Path | None, data_dir: str | Path | None, include_graph: bool = False) -> dict[str, Any]:
    engine, _, _, _ = _engine(config_path=config_path, store_path=store_path, data_dir=data_dir, create=False)
    graph = engine.status(graph_id)
    result = _summary(engine, graph)
    if include_graph:
        result["graph"] = graph
    return result


def cancel(graph_id: str, confirmation: str, *, config_path: str | Path | None, store_path: str | Path | None, data_dir: str | Path | None) -> dict[str, Any]:
    if confirmation != graph_id:
        raise RouteCraftLocalError("cancelには --confirm <graph_id> の完全一致が必要です。")
    engine, _, _, _ = _engine(config_path=config_path, store_path=store_path, data_dir=data_dir, create=False)
    graph = engine.cancel(graph_id)
    return {**_summary(engine, graph), **_refresh_collector_bundle(engine, graph_id)}


def export(graph_id: str, output: str | Path, *, config_path: str | Path | None, store_path: str | Path | None, data_dir: str | Path | None) -> dict[str, Any]:
    engine, _, _, _ = _engine(config_path=config_path, store_path=store_path, data_dir=data_dir, create=False)
    exported = engine.export(graph_id)
    target = _write_json_new(output, exported)
    # The caller's complete local export is authoritative.  The separate
    # rolling collector snapshot is intentionally best effort and may not turn
    # a successful local graph export into a Control Center dependency.
    collector = _materialize_collector_bundle(exported["telemetry"])
    return {"graph_id": graph_id, "output": str(target), "local_only": True, **collector}


def policy_status(*, config_path: str | Path | None, store_path: str | Path | None) -> dict[str, Any]:
    config, resolved, state = _effective_config(config_path)
    snapshot = doctor_snapshot(resolved if resolved.exists() else None, _store_path(config, store_path))
    return {
        "production_policy": config["policy"]["production_policy"],
        "graph_mode": config["graph"]["mode"],
        "allowlisted_task_classes": list(config["policy"]["allowlisted_task_classes"]),
        "config": str(resolved),
        "config_state": state,
        "state_store": snapshot["state_store"],
    }


def doctor(*, config_path: str | Path | None = None, store_path: str | Path | None = None) -> dict[str, Any]:
    """Return a read-only durable graph health snapshot without creating files."""
    config, resolved, state = _effective_config(config_path)
    snapshot = doctor_snapshot(resolved if resolved.exists() else None, _store_path(config, store_path))
    return {**snapshot, "config_path": str(resolved), "config_state": state}


def policy_candidates(*, config_path: str | Path | None, store_path: str | Path | None, data_dir: str | Path | None, normal_only: bool) -> dict[str, Any]:
    config, _, _ = _effective_config(config_path)
    resolved_store = _store_path(config, store_path)
    if not resolved_store.exists():
        return {"normal_only": normal_only, "count": 0, "candidates": [], "state_store": "NOT CREATED"}
    engine, _, _, _ = _engine(config_path=config_path, store_path=store_path, data_dir=data_dir, create=False)
    rows = engine.store.list_policy_candidates(normal_only=normal_only)
    return {"normal_only": normal_only, "count": len(rows), "candidates": rows, "state_store": "OK"}


def migrate_config(
    *,
    config_path: str | Path | None,
    existing: dict[str, Any] | None,
    apply: bool,
    confirmation: str | None,
) -> dict[str, Any]:
    target = Path(config_path).expanduser() if config_path else default_config_path()
    preview = migration_preview(existing)
    if not apply:
        return {**preview, "target": str(target), "applied": False}
    if confirmation != "MIGRATE":
        raise RouteCraftLocalError("Graph config migrationには --confirm MIGRATE が必要です。")
    config = save_default_config(target, existing=existing)
    return {"from_version": preview["from_version"], "to_version": preview["to_version"], "target": str(target), "config": config, "destructive": False, "applied": True}


__all__ = [
    "approve", "cancel", "default_config_path", "doctor", "export", "migrate_config", "plan", "policy_candidates",
    "policy_status", "resume", "run", "status", "validate_file",
]
