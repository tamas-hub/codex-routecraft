"""Local-only, privacy-filtered RouteCraft Control Center collector.

This file imports only stdlib plus ``routecraft_telemetry.py``, so it works in
the exact installed tray directory without the source plugin tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

import routecraft_telemetry

SCHEMA_VERSION = 4
V3_SCHEMA_VERSION = 3
MAX_COLLECTION = 500
# Graph detail is one coherent run bundle, not a generic historical family.
# Keep this aligned with the Control Center v4 ingestion boundary.  The
# Runtime degrades an oversized bundle to graph_runs only before this point.
MAX_GRAPH_BUNDLE_ROWS = 75
_GRAPH_BUNDLE_FAMILIES = ("graph_runs", "graph_node_metrics", "graph_events")
_USAGE_LOCK = threading.Lock()
_OPAQUE = re.compile(r"^[a-f0-9]{16,64}$")
# Labels may be human-facing (for example ``RouteCraft + Memory``), but must
# still be bounded, single-line and incapable of carrying a path or URL.
_LABEL = re.compile(r"^[^\W_][\w .+\-]{0,79}$", re.UNICODE)
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$")
_HEALTH = {"healthy", "degraded", "unavailable", "disabled", "unknown"}
_OS_FAMILY = {"windows", "macos", "linux", "other"}
_WINDOW_KIND = {"five_hour", "weekly"}
_BENCHMARK_KIND = {"routing", "memory", "usage", "security"}
_BENCHMARK_STATUS = {"passed", "failed", "partial", "cancelled", "unavailable"}
_WINNER = {"current", "candidate", "tie", "inconclusive"}
_CONFIDENCE = {"low", "medium", "high"}
_SECURITY_STATUS = {"clean", "findings", "error", "unavailable"}
_BASELINE = {"initial", "previous", "policy"}
_REAL_BENCHMARK_MODES = {"off", "on_memory_off", "on_recall", "full_memory", "graph_observe", "graph_enforce"}
_REAL_BENCHMARK_METRICS = {"task_success", "test_pass", "acceptance_pass", "review_findings", "rework_count", "retry_count", "wall_time_ms", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens", "child_runs", "fresh_review_used", "memory_recall_count", "memory_useful_count", "sol_runs", "terra_runs", "luna_runs", "other_lane_runs"}
_EVIDENCE_STATUS = {"insufficient_evidence", "low_confidence", "measured", "unavailable", "failed"}
_SECURITY_VALIDATION_STATUS = {"passed", "failed", "insufficient_evidence", "unavailable"}
_GRAPH_MODE = {"off", "observe", "enforce"}
_GRAPH_STATUS = {"DRAFT", "COMPILED", "RUNNING", "ACCEPTED", "FAILED", "BLOCKED", "CANCELLED", "CONVERGENCE_FAILED"}
_GRAPH_GATE_STATUS = {"PASS", "FAIL", "INCONCLUSIVE", "NOT_RUN"}
_GRAPH_NODE_GATE_STATUS = {"PASS", "FAIL", "INCONCLUSIVE", "NOT_RUN"}
_EVENT_CLASSIFICATION = {"normal", "benchmark_run", "migration_event", "incident_response", "token_burn_event", "reset_expectation", "manual_stress_test", "release_validation"}
_GRAPH_NODE_TYPE = {"AGENT", "TOOL", "DETERMINISTIC", "GATE", "MERGE", "HUMAN_APPROVAL", "MEMORY_RECALL", "BENCHMARK", "SECURITY", "CHECKPOINT", "QUALITY"}
_GRAPH_NODE_STATUS = {"PENDING", "READY", "RUNNING", "ACCEPTED", "FROZEN", "FAILED", "INVALIDATED", "BLOCKED", "SKIPPED", "CANCELLED"}
_GRAPH_EVENT_TYPE = {"node_transition", "dependency", "checkpoint", "gate", "retry", "send_back", "constraint", "replan", "resume", "cancel", "external_mutation", "approval"}
_EVIDENCE_GATE_RESULT = {"PASS", "FAIL", "INCONCLUSIVE"}
_POLICY_CANDIDATE_STATUS = {"DRAFT", "SHADOW", "CANDIDATE", "APPROVED", "REJECTED", "RETIRED"}
_POLICY_CANDIDATE_CHANGE_KIND = {"routing_threshold", "lane_mapping", "parallelism", "graph_template", "memory_recall", "gate_policy", "retry_policy", "other"}
_RISK_LEVEL = {"low", "medium", "high", "critical"}
_CONVERGENCE_REASON = {"none", "max_attempts", "max_steps", "max_child_runs", "max_wall_time", "retry_budget", "invalid_graph"}
_GRAPH_TASK_CLASS = set(routecraft_telemetry.VALID_TASK_CLASSES) | {"security", "benchmark", "migration", "review", "investigation", "performance"}
_LEGACY_COMPONENT_KIND = {"ai_usage_updater", "codex_meter_startup", "observatory_legacy", "collector_legacy"}
_LEGACY_STATUS = {"active", "disabled", "observing", "superseded", "archived", "unknown"}
_REPLACEMENT_KIND = {"unified_usage_adapter", "control_center", "unified_collector", "none"}
_RAW_KEYS = {"prompt", "conversation", "transcript", "body", "content", "source", "path", "file", "file_content", "absolute_path", "session_id", "raw_session_id", "secret", "password", "cookie", "header", "authorization", "credential", "private_key", "raw_worker_packet", "raw_node_output", "memory_text", "decision_text"}

DEVICE_HEALTH_KEYS = {"health_id", "device_id", "observed_at", "os_family", "online", "routecraft_version", "plugin_health", "hook_health", "agents_healthy", "agents_total", "git_clean", "git_ahead", "git_behind", "git_conflicts", "memory_git_clean", "last_sync_at"}
MEMORY_METRICS_KEYS = {"metric_id", "device_id", "observed_at", "local_projects", "local_memories", "context_injections", "handoffs", "decision_cases", "candidates", "rules", "eligible_candidates", "recall_count", "useful_count", "learn_count", "skipped_count", "usefulness_rate", "last_backup_at", "last_sync_at"}
USAGE_SNAPSHOT_KEYS = {"snapshot_id", "device_id", "observed_at", "window_kind", "used_percent", "remaining_percent", "reset_at", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "sol_runs", "terra_runs", "luna_runs"}
BENCHMARK_RUN_KEYS = {"benchmark_run_id", "device_id", "observed_at", "comparison_kind", "status", "measured", "current_label", "candidate_label", "current_success_rate", "candidate_success_rate", "current_quality", "candidate_quality", "current_tokens", "candidate_tokens", "current_duration_ms", "candidate_duration_ms", "current_test_pass_rate", "candidate_test_pass_rate", "current_rework", "candidate_rework", "winner", "confidence"}
SECURITY_SCAN_KEYS = {"scan_id", "device_id", "observed_at", "repository_hint", "status", "baseline", "critical_count", "high_count", "medium_count", "low_count", "info_count", "new_count", "resolved_count", "confidence"}
SYSTEM_STATUS_KEYS = {"status_id", "device_id", "observed_at", "core_health", "plugin_version", "hook_health", "agents_healthy", "agents_total", "collector_health", "collector_version", "memory_local_health", "decision_health", "control_health", "benchmark_health", "security_health"}
BENCHMARK_METRIC_EVIDENCE_KEYS = {"evidence_id", "device_id", "observed_at", "suite_version", "mode", "metric", "case_count", "sample_size", "available_count", "mean_value", "median_value", "min_value", "max_value", "success_count", "success_rate", "confidence", "evidence_status"}
SECURITY_VALIDATION_KEYS = {"validation_id", "device_id", "observed_at", "ruleset_version", "ruleset_digest", "rules_tested", "supported_rules", "fixture_pairs", "fixture_coverage", "true_positive", "true_negative", "false_positive", "false_negative", "detection_rate", "false_positive_rate", "status", "confidence", "repositories_scanned", "useful_findings", "false_positive_findings", "unsupported_findings", "uncertain_findings"}
GRAPH_RUN_KEYS = {"graph_run_id", "device_id", "observed_at", "event_classification", "graph_schema_version", "mode", "status", "graph_revision_count", "node_count", "edge_count", "parallel_width", "critical_path_length", "attempt_count", "retry_count", "send_back_count", "accepted_count", "frozen_count", "failed_count", "invalidated_count", "constraint_count", "checkpoint_count", "gate_pass_count", "gate_fail_count", "gate_inconclusive_count", "duration_ms", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"}
GRAPH_NODE_METRIC_KEYS = {"node_metric_id", "graph_run_id", "device_id", "observed_at", "node_ordinal", "dependency_count", "node_type", "lane", "status", "attempt_count", "gate_status", "duration_ms", "total_tokens", "retry_count", "send_back_count", "accepted", "frozen", "invalidated"}
GRAPH_EVENT_KEYS = {"graph_event_id", "graph_run_id", "device_id", "observed_at", "event_sequence", "event_type", "status", "node_ordinal", "source_node_ordinal", "target_node_ordinal", "gate_status", "attempt_count", "affected_node_count", "constraint_count", "checkpoint_count"}
POLICY_CANDIDATE_KEYS = {"policy_candidate_id", "device_id", "observed_at", "base_policy_version", "candidate_version", "candidate_change_kind", "sample_size", "confidence", "expected_benefit", "known_risk", "status"}
SECURITY_RULE_METRIC_KEYS = {"security_rule_metric_id", "device_id", "observed_at", "ruleset_version", "rule_id", "true_positive", "true_negative", "false_positive", "false_negative", "fixture_coverage", "detection_rate", "false_positive_rate", "confidence", "status"}
LEGACY_COMPONENT_KEYS = {"component_observation_id", "device_id", "observed_at", "component_kind", "status", "replacement_kind", "enabled", "running", "observation_cycles", "consecutive_healthy_cycles", "last_error_at", "missing_snapshots", "duplicate_ingestions", "replacement_health", "confidence"}
FAMILY_KEYS = {"device_health": DEVICE_HEALTH_KEYS, "memory_metrics": MEMORY_METRICS_KEYS, "usage_snapshots": USAGE_SNAPSHOT_KEYS, "benchmark_runs": BENCHMARK_RUN_KEYS, "security_scans": SECURITY_SCAN_KEYS, "system_status": SYSTEM_STATUS_KEYS}
V4_FAMILY_KEYS = {**FAMILY_KEYS, "benchmark_metric_evidence": BENCHMARK_METRIC_EVIDENCE_KEYS, "security_validations": SECURITY_VALIDATION_KEYS, "graph_runs": GRAPH_RUN_KEYS, "graph_node_metrics": GRAPH_NODE_METRIC_KEYS, "graph_events": GRAPH_EVENT_KEYS, "policy_candidates": POLICY_CANDIDATE_KEYS, "security_rule_metrics": SECURITY_RULE_METRIC_KEYS, "legacy_components": LEGACY_COMPONENT_KEYS}
RUN_BASE_KEYS = {
    "run_id", "parent_run_id", "device_id", "route_family", "role", "human_model", "human_effort",
    "actual_model", "actual_effort", "started_at", "ended_at", "duration_ms", "input_tokens",
    "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens",
    "total_tokens", "observed_at",
}
RUN_MEMORY_KEYS = {
    "task_class", "task_summary", "memory_mode", "memory_recall_count", "memory_useful_count",
    "memory_learn_status", "memory_skip_reason",
}
RUN_KEYS = RUN_BASE_KEYS | RUN_MEMORY_KEYS
MEMORY_TASK_KEYS = {
    "task_run_id", "parent_run_id", "device_id", "human_model", "human_effort", "task_class",
    "task_summary", "memory_mode", "memory_recall_count", "memory_useful_count", "memory_learn_status",
    "memory_skip_reason", "completed_at", "observed_at",
}
_ROUTE_FAMILY = {"routecraft", "legacy", "builtin", "unclassified"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def opaque_id(namespace: str, value: object) -> str:
    return hashlib.sha256(f"routecraft-control-center-v3:{namespace}:{value}".encode("utf-8", "replace")).hexdigest()[:32]


def enabled() -> bool:
    return os.environ.get("CONTROL_CENTER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _count(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _percent(value: object) -> int:
    return min(100, _count(value))


def _label(value: object, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    return text if _LABEL.fullmatch(text) else fallback


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _family_id(family: str, device_id: str, observed_at: str, variant: str = "") -> str:
    # Retry in the same observation cycle is idempotent; later cycles retain history.
    return opaque_id(family, f"{device_id}:{observed_at}:{variant}")


def _device_id(codex_home: Path) -> str:
    identity = platform.node() or "routecraft-device"
    for config_name in ("device.json", "memory.json"):
        try:
            data = json.loads((codex_home / "routecraft" / config_name).read_text(encoding="utf-8"))
            candidate = data.get("device_id") if isinstance(data, dict) else None
            if isinstance(candidate, str) and candidate:
                identity = candidate
                break
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return opaque_id("device", identity)


def _git_state(root: Path) -> tuple[bool, int, int, int]:
    # Linked worktrees keep a .git *file*, while ordinary checkouts use a
    # directory.  Git itself remains the bounded source of truth below.
    if not (root / ".git").exists():
        return False, 0, 0, 0

    def run(*args: str) -> str:
        completed = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", check=False, timeout=5)
        return completed.stdout.strip() if completed.returncode == 0 else ""

    dirty = bool(run("status", "--porcelain"))
    conflicts = len([line for line in run("diff", "--name-only", "--diff-filter=U").splitlines() if line])
    upstream = bool(run("rev-parse", "@{u}"))
    ahead = _count(run("rev-list", "--count", "@{u}..HEAD")) if upstream else 0
    behind = _count(run("rev-list", "--count", "HEAD..@{u}")) if upstream else 0
    return not dirty and conflicts == 0, ahead, behind, conflicts


def _plugin_version(source_root: Path) -> str:
    try:
        manifest = json.loads((source_root / "plugins" / "codex-routecraft" / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        return _label(manifest.get("version"), "0.7.1")
    except (OSError, ValueError, json.JSONDecodeError):
        return "0.7.1"


def _file_digest(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _same_file(left: Path, right: Path) -> bool:
    left_digest = _file_digest(left)
    return left_digest is not None and left_digest == _file_digest(right)


def _installed_plugin(codex_home: Path, source_root: Path) -> tuple[str, Path | None]:
    source_version = _plugin_version(source_root)
    config = _json_object(codex_home / "routecraft" / "device.json")
    configured = _label(config.get("plugin_version"), source_version) if config else source_version
    cache_root = codex_home / "plugins" / "cache" / "routecraft" / "codex-routecraft"
    exact = cache_root / configured
    if exact.is_dir():
        return configured, exact
    try:
        candidates = sorted(
            (path for path in cache_root.iterdir() if path.is_dir() and path.name.startswith(source_version)),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        candidates = []
    return (_label(candidates[0].name, source_version), candidates[0]) if candidates else (source_version, None)


def _os_family() -> str:
    system = platform.system().lower()
    return "windows" if system.startswith("win") else "macos" if system == "darwin" else "linux" if system == "linux" else "other"


def _run_app_server(command: list[str]) -> Mapping[str, object]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + 12

    def send(payload: Mapping[str, object]) -> None:
        if process.stdin is None:
            raise RuntimeError("app_server_unavailable")
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def receive(response_id: int) -> Mapping[str, object]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("app_server_timeout")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise RuntimeError("app_server_timeout") from exc
            if line is None:
                raise RuntimeError("app_server_invalid_response")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("id") == response_id and isinstance(payload.get("result"), dict):
                return payload["result"]

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "routecraft-local", "version": "0.7.1"}}})
        receive(1)
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
        return receive(2)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def _default_app_server_command() -> list[str]:
    configured = os.environ.get("ROUTECRAFT_CODEX_APP_SERVER", "").strip()
    if configured:
        return [configured, "app-server", "--stdio"]
    if os.name == "nt":
        shim = shutil.which("codex.cmd")
        if shim:
            return ["cmd.exe", "/d", "/c", shim, "app-server", "--stdio"]
        native = shutil.which("codex.exe")
        if native:
            return [native, "app-server", "--stdio"]
        return ["cmd.exe", "/d", "/c", "codex.cmd", "app-server", "--stdio"]
    return ["codex", "app-server", "--stdio"]


def _usage_windows(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    root: object = result.get("rateLimits", result.get("rate_limits", result))
    if not isinstance(root, Mapping):
        return []
    by_limit = root.get("rateLimitsByLimitId")
    if isinstance(by_limit, Mapping):
        root = by_limit.get("codex", {})
    if isinstance(root, Mapping) and all(key in root for key in ("primary", "secondary")):
        output: list[Mapping[str, object]] = []
        for key in ("primary", "secondary"):
            value = root.get(key)
            if isinstance(value, Mapping):
                duration = value.get("windowDurationMins")
                kind = "fiveHour" if duration == 300 else "weekly" if duration == 10080 else value.get("kind", value.get("windowKind", key))
                output.append({**value, "kind": kind})
        return output
    if not isinstance(root, Mapping):
        return []
    if isinstance(root.get("windows"), list):
        return [item for item in root["windows"] if isinstance(item, Mapping)]
    output: list[Mapping[str, object]] = []
    for key in ("fiveHour", "five_hour", "weekly"):
        item = root.get(key)
        if isinstance(item, Mapping):
            output.append({"kind": key, **item})
    return output


def _reset_at(window: Mapping[str, object], now: str) -> str | None:
    for key in ("resetAt", "reset_at", "resetTime"):
        parsed = _timestamp(window.get(key))
        if parsed:
            return parsed
    try:
        if isinstance(window.get("resetsAt"), (int, float)):
            return datetime.fromtimestamp(window["resetsAt"], timezone.utc).isoformat().replace("+00:00", "Z")
        seconds = max(0, int(window.get("resetSeconds", window.get("reset_seconds"))))
        return (datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def _run_aggregates(runs: list[Mapping[str, object]]) -> dict[str, int]:
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "sol_runs": 0, "terra_runs": 0, "luna_runs": 0}
    for run in runs:
        totals["input_tokens"] += _count(run.get("input_tokens"))
        totals["cached_input_tokens"] += _count(run.get("cached_input_tokens"))
        totals["output_tokens"] += _count(run.get("output_tokens"))
        totals["reasoning_tokens"] += _count(run.get("reasoning_output_tokens"))
        model = str(run.get("actual_model", "")).lower()
        if "sol" in model:
            totals["sol_runs"] += 1
        elif "terra" in model:
            totals["terra_runs"] += 1
        elif "luna" in model:
            totals["luna_runs"] += 1
    return totals


def usage_snapshots(device_id: str, observed_at: str, runs: list[Mapping[str, object]], command: list[str] | None = None) -> list[dict[str, object]]:
    """Initialize then read account limits once; failures remain unknown.

    An unavailable App Server must not masquerade as a fully unused quota.
    Empty collections are accepted by v3 precisely for this condition.
    """
    with _USAGE_LOCK:
        try:
            result = _run_app_server(command or _default_app_server_command())
            aggregates = _run_aggregates(runs)
            records: list[dict[str, object]] = []
            for window in _usage_windows(result):
                raw_kind = str(window.get("kind", window.get("windowKind", ""))).lower()
                if "week" in raw_kind:
                    kind = "weekly"
                elif "five" in raw_kind or "hour" in raw_kind:
                    kind = "five_hour"
                else:
                    continue
                remaining = _percent(window.get("remainingPercent", window.get("remaining_percent")))
                used = _percent(window.get("usedPercent", window.get("used_percent", 100 - remaining)))
                if "remainingPercent" not in window and "remaining_percent" not in window:
                    remaining = 100 - used
                elif "usedPercent" not in window and "used_percent" not in window:
                    used = 100 - remaining
                records.append({"snapshot_id": _family_id("usage", device_id, observed_at, kind), "device_id": device_id, "observed_at": observed_at, "window_kind": kind, "used_percent": used, "remaining_percent": remaining, "reset_at": _reset_at(window, observed_at), **aggregates})
            return records[:MAX_COLLECTION]
        except Exception:
            return []


def unavailable_device_health(device_id: str, observed_at: str) -> dict[str, object]:
    return {"health_id": _family_id("device-health", device_id, observed_at), "device_id": device_id, "observed_at": observed_at, "os_family": "other", "online": False, "routecraft_version": "unknown", "plugin_health": "unavailable", "hook_health": "unavailable", "agents_healthy": 0, "agents_total": 0, "git_clean": False, "git_ahead": 0, "git_behind": 0, "git_conflicts": 0, "memory_git_clean": False, "last_sync_at": None}


def device_health(source_root: Path, device_id: str | None = None, observed_at: str | None = None, codex_home: Path | None = None) -> dict[str, object]:
    """Return the v3 health row.

    ``device_id`` and ``observed_at`` remain optional for the existing local
    doctor command, which historically called this helper with only a root.
    The optional form still emits the exact v3 shape; it never revives the
    old generic record format.
    """
    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    device_id = device_id or _device_id(home)
    observed_at = observed_at or utc_now()
    try:
        plugin = source_root / "plugins" / "codex-routecraft"
        version, installed = _installed_plugin(home, source_root)
        source_hooks = plugin / "hooks" / "hooks.json"
        installed_hooks = installed / "hooks" / "hooks.json" if installed else Path()
        hooks = bool(installed and _same_file(source_hooks, installed_hooks))
        agent_names = ("routecraft_luna_low.toml", "routecraft_luna_medium.toml", "routecraft_luna_max.toml", "routecraft_terra_medium.toml", "routecraft_terra_high.toml", "routecraft_sol_reviewer.toml")
        agents = sum(_same_file(plugin / "agents" / name, home / "agents" / name) for name in agent_names)
        clean, ahead, behind, conflicts = _git_state(source_root)
        memory_config = _json_object(home / "routecraft" / "memory.json")
        memory_store_value = memory_config.get("store") if memory_config else None
        memory_store = Path(memory_store_value).expanduser() if isinstance(memory_store_value, str) and memory_store_value else None
        memory_clean = _git_state(memory_store)[0] if memory_store and memory_store.is_dir() else False
        sync_at = None
        if memory_store and memory_store.is_dir():
            try:
                completed = subprocess.run(["git", "-C", str(memory_store), "log", "-1", "--format=%cI"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", check=False, timeout=5)
                if completed.returncode == 0:
                    sync_at = datetime.fromisoformat(completed.stdout.strip().replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except (OSError, subprocess.SubprocessError, ValueError):
                sync_at = None
        return {"health_id": _family_id("device-health", device_id, observed_at), "device_id": device_id, "observed_at": observed_at, "os_family": _os_family(), "online": True, "routecraft_version": version, "plugin_health": "healthy" if installed and (installed / ".codex-plugin" / "plugin.json").is_file() else "unavailable", "hook_health": "healthy" if hooks else "unavailable", "agents_healthy": agents, "agents_total": len(agent_names), "git_clean": clean, "git_ahead": ahead, "git_behind": behind, "git_conflicts": conflicts, "memory_git_clean": memory_clean, "last_sync_at": sync_at}
    except Exception:
        return unavailable_device_health(device_id, observed_at)


def _sqlite_count(connection: sqlite3.Connection, names: tuple[str, ...]) -> int:
    for name in names:
        try:
            return _count(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])  # routecraft-security: allowlisted-sql-shape
        except sqlite3.Error:
            continue
    return 0


def _json_object(path: Path) -> Mapping[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, Mapping) else None


def _configured_local_data_dir(codex_home: Path) -> str | None:
    config = _json_object(codex_home / "routecraft" / "local-memory.json")
    value = config.get("data_dir") if config else None
    return str(value) if isinstance(value, str) and value else None


def configured_source_root(codex_home: Path) -> Path | None:
    """Resolve the registered Runtime source without exporting its path."""
    config = _json_object(codex_home / "routecraft" / "device.json")
    value = config.get("source_dir") if config else None
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value).expanduser()
    return candidate if candidate.is_dir() else None


def _decision_counts(codex_home: Path, source_root: Path | None = None) -> dict[str, object] | None:
    config = _json_object(codex_home / "routecraft" / "memory.json")
    store_value = config.get("store") if config else None
    if not isinstance(store_value, str) or not store_value:
        return None
    store = Path(store_value).expanduser()
    if not (store / ".routecraft-store.json").is_file():
        return None
    try:
        direct: dict[str, object] = {
            "decision_cases": sum(1 for path in (store / "cases").glob("*.md") if path.is_file()),
            "candidates": sum(1 for path in (store / "candidates").glob("*.md") if path.is_file()),
            "rules": sum(1 for path in (store / "rules").glob("*.md") if path.is_file()),
            "eligible_candidates": 0,
            "last_sync_at": None,
        }
    except OSError:
        return None

    cli = source_root / "plugins" / "codex-routecraft" / "scripts" / "routecraft_memory.py" if source_root else None
    if cli and cli.is_file():
        try:
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            process = subprocess.run(
                [sys.executable, str(cli), "status", "--store", str(store), "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                check=False,
                timeout=10,
            )
            status = json.loads(process.stdout) if process.returncode == 0 else None
            if isinstance(status, Mapping):
                counts = status.get("counts")
                if isinstance(counts, Mapping):
                    direct.update({
                        "decision_cases": _count(counts.get("case")),
                        "candidates": _count(counts.get("candidate")),
                        "rules": _count(counts.get("rule")),
                    })
                eligible = status.get("eligible_candidates")
                if isinstance(eligible, list):
                    direct["eligible_candidates"] = len(eligible)
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            pass
    try:
        completed = subprocess.run(
            ["git", "-C", str(store), "log", "-1", "--format=%cI"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
        if completed.returncode == 0:
            raw_timestamp = completed.stdout.strip()
            try:
                direct["last_sync_at"] = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except ValueError:
                direct["last_sync_at"] = None
    except (OSError, subprocess.SubprocessError):
        pass
    return direct


def _evaluation_counts(source_root: Path | None) -> dict[str, int] | None:
    evaluator = source_root / "plugins" / "codex-routecraft" / "scripts" / "routecraft_evaluation.py" if source_root else None
    if not evaluator or not evaluator.is_file():
        return None
    try:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.run(
            [sys.executable, str(evaluator), "summary", "--json", "--compact"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            check=False,
            timeout=12,
        )
        summary = json.loads(process.stdout) if process.returncode == 0 else None
        if not isinstance(summary, Mapping) or not bool(summary.get("enabled")):
            return None
        recall = _count(summary.get("recall_tasks"))
        try:
            rate_value = float(summary.get("useful_task_rate", 0))
        except (TypeError, ValueError):
            rate_value = 0.0
        rate = min(100, max(0, int(round(rate_value * 100 if rate_value <= 1 else rate_value))))
        return {
            "recall_count": recall,
            "useful_count": min(recall, int(round(recall * rate / 100))),
            "learn_count": _count(summary.get("learned_tasks")),
            "skipped_count": _count(summary.get("skipped_tasks")),
            "usefulness_rate": rate,
        }
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return None


def _memory_metrics_with_availability(data_dir: str | None, device_id: str | None = None, observed_at: str | None = None, codex_home: Path | None = None, source_root: Path | None = None) -> tuple[dict[str, object] | None, bool, bool]:
    """Read aggregate-only Local Memory and Decision Store counts.

    No database or Markdown content, directory name, or configured path leaves
    this process. If no configured memory source or local evaluation aggregate
    is readable, the caller receives ``None`` and sends no synthetic metric row.
    """
    device_id = device_id or _device_id(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    observed_at = observed_at or utc_now()
    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    row: dict[str, object] = {"metric_id": _family_id("memory-metrics", device_id, observed_at), "device_id": device_id, "observed_at": observed_at, "local_projects": 0, "local_memories": 0, "context_injections": 0, "handoffs": 0, "decision_cases": 0, "candidates": 0, "rules": 0, "eligible_candidates": 0, "recall_count": 0, "useful_count": 0, "learn_count": 0, "skipped_count": 0, "usefulness_rate": 0, "last_backup_at": None, "last_sync_at": None}
    available = False
    local_available = False
    decision_available = False
    data_dir = data_dir or _configured_local_data_dir(home)
    try:
        candidates = [Path(data_dir) / name for name in ("routecraft-local.sqlite3", "routecraft.sqlite3", "routecraft.db")] if data_dir else []
        database = next(path for path in candidates if path.is_file())
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            row["local_projects"] = _sqlite_count(connection, ("projects",))
            row["local_memories"] = _sqlite_count(connection, ("memories", "memory_entries"))
            row["context_injections"] = _sqlite_count(connection, ("context_injections",))
            row["handoffs"] = _sqlite_count(connection, ("handoffs",))
        finally:
            connection.close()
        available = True
        local_available = True
    except Exception:
        pass
    runtime_source = source_root or configured_source_root(home)
    decision = _decision_counts(home, runtime_source)
    if decision is not None:
        row.update(decision)
        available = True
        decision_available = True
    evaluation = _evaluation_counts(runtime_source)
    if evaluation is not None:
        row.update(evaluation)
        available = True
    return (row if available else None, local_available, decision_available)


def memory_metrics(data_dir: str | None, device_id: str | None = None, observed_at: str | None = None, codex_home: Path | None = None, source_root: Path | None = None) -> dict[str, object] | None:
    return _memory_metrics_with_availability(data_dir, device_id, observed_at, codex_home, source_root)[0]


def _summary_row(path: Path, family: str, device_id: str, observed_at: str) -> dict[str, object] | None:
    """Read only an already privacy-safe exact v3 aggregate row.

    Benchmark and security engines own these local latest-summary files.  The
    collector never reads their raw reports, cases, findings, or source data.
    """
    try:
        if path.stat().st_size > 32768:
            return None
        result = _json_object(path)
        if not isinstance(result, Mapping) or not _valid_family(family, result):
            return None
        identity = "benchmark_run_id" if family == "benchmark_runs" else "scan_id"
        normalized = dict(result)
        normalized[identity] = _family_id(family, device_id, observed_at, str(result[identity]))
        normalized["device_id"] = device_id
        normalized["observed_at"] = observed_at
        return normalized
    except (OSError, TypeError, ValueError):
        return None


def benchmark_summary(device_id: str | Path, observed_at: str | None = None, result_file: Path | None = None) -> dict[str, object]:
    # The legacy doctor passed an evaluation directory.  It is intentionally
    # not serialized; the v3 fallback remains an aggregate unavailable row.
    if isinstance(device_id, Path):
        device_id = _device_id(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    observed_at = observed_at or utc_now()
    if result_file:
        adapted = _summary_row(result_file, "benchmark_runs", device_id, observed_at)
        if adapted is not None:
            return adapted
    # An unavailable observation is not a measured zero.  The v3/v4 transport
    # keeps the existing row shape and uses nullable metric columns so older
    # history remains compatible without inventing benchmark evidence.
    return {"benchmark_run_id": _family_id("benchmark", device_id, observed_at), "device_id": device_id, "observed_at": observed_at, "comparison_kind": "routing", "status": "unavailable", "measured": False, "current_label": "current", "candidate_label": "candidate", "current_success_rate": None, "candidate_success_rate": None, "current_quality": None, "candidate_quality": None, "current_tokens": None, "candidate_tokens": None, "current_duration_ms": None, "candidate_duration_ms": None, "current_test_pass_rate": None, "candidate_test_pass_rate": None, "current_rework": None, "candidate_rework": None, "winner": "inconclusive", "confidence": "low"}


def security_summary(device_id: str | Path, observed_at: str | None = None, result_file: Path | None = None) -> dict[str, object]:
    # See benchmark_summary: source roots are local-only input, never output.
    if isinstance(device_id, Path):
        device_id = _device_id(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    observed_at = observed_at or utc_now()
    if result_file:
        adapted = _summary_row(result_file, "security_scans", device_id, observed_at)
        if adapted is not None:
            return adapted
    return {"scan_id": _family_id("security", device_id, observed_at), "device_id": device_id, "observed_at": observed_at, "repository_hint": "routecraft", "status": "unavailable", "baseline": "previous", "critical_count": 0, "high_count": 0, "medium_count": 0, "low_count": 0, "info_count": 0, "new_count": 0, "resolved_count": 0, "confidence": "low"}


def _health_of(value: object) -> str:
    return value if isinstance(value, str) and value in _HEALTH else "unknown"


def _benchmark_health(benchmark: Mapping[str, object]) -> str:
    status = benchmark.get("status")
    if status == "passed" and benchmark.get("measured") is True:
        return "healthy"
    if status in {"failed", "partial"}:
        return "degraded"
    return "unavailable"


def _security_health(security: Mapping[str, object]) -> str:
    status = security.get("status")
    if status == "clean":
        return "healthy"
    if status in {"findings", "error"}:
        return "degraded"
    return "unavailable"


def system_status(device: Mapping[str, object], usage: list[Mapping[str, object]], memory_available: bool, benchmark: Mapping[str, object], security: Mapping[str, object], device_id: str, observed_at: str, *, local_memory_available: bool | None = None, decision_available: bool | None = None) -> dict[str, object]:
    plugin_health = _health_of(device.get("plugin_health"))
    hook_health = _health_of(device.get("hook_health"))
    core = "healthy" if plugin_health == hook_health == "healthy" else "degraded" if plugin_health != "unavailable" else "unavailable"
    usage_health = "healthy" if usage else "unavailable"
    local_memory_available = memory_available if local_memory_available is None else local_memory_available
    decision_available = memory_available if decision_available is None else decision_available
    memory_health = "healthy" if local_memory_available else "unavailable"
    decision_health = "healthy" if decision_available else "unavailable"
    control_health = "healthy" if usage and memory_available else "degraded"
    return {"status_id": _family_id("system-status", device_id, observed_at), "device_id": device_id, "observed_at": observed_at, "core_health": core, "plugin_version": _label(device.get("routecraft_version"), "unknown"), "hook_health": hook_health, "agents_healthy": _count(device.get("agents_healthy")), "agents_total": _count(device.get("agents_total")), "collector_health": "healthy" if usage and memory_available else "degraded", "collector_version": "3.0.0", "memory_local_health": memory_health, "decision_health": decision_health, "control_health": control_health, "benchmark_health": _benchmark_health(benchmark), "security_health": _security_health(security)}


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_OPAQUE.fullmatch(value))


def _valid_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_percent(value: object) -> bool:
    return _valid_count(value) and value <= 100


def _valid_finite(value: object, *, nullable: bool = False) -> bool:
    if nullable and value is None:
        return True
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _valid_rate(value: object, *, nullable: bool = False) -> bool:
    return _valid_finite(value, nullable=nullable) and (value is None or value <= 100)


def _valid_label(value: object) -> bool:
    return isinstance(value, str) and bool(_LABEL.fullmatch(value))


def _valid_token(value: object, *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (
        isinstance(value, str)
        and bool(routecraft_telemetry.SAFE_TOKEN.fullmatch(value))
    )


def _valid_summary(value: object, *, nullable: bool = False) -> bool:
    if nullable and value is None:
        return True
    return isinstance(value, str) and routecraft_telemetry.safe_task_summary(value) is not None


def _valid_memory_bundle(row: Mapping[str, object], *, required: bool) -> bool:
    present = RUN_MEMORY_KEYS & set(row)
    if not present:
        return not required
    if present != RUN_MEMORY_KEYS:
        return False
    values = [
        row["task_class"], row["task_summary"], row["memory_mode"], row["memory_recall_count"],
        row["memory_useful_count"], row["memory_learn_status"], row["memory_skip_reason"],
    ]
    if not required and all(value is None for value in values):
        return True
    task_class = row["task_class"]
    summary = row["task_summary"]
    mode = row["memory_mode"]
    recall = row["memory_recall_count"]
    useful = row["memory_useful_count"]
    learn_status = row["memory_learn_status"]
    skip_reason = row["memory_skip_reason"]
    if (
        not isinstance(task_class, str) or task_class not in routecraft_telemetry.VALID_TASK_CLASSES
        or not _valid_summary(summary)
        or not isinstance(mode, str) or mode not in routecraft_telemetry.VALID_MEMORY_MODES
        or not _valid_count(recall) or not _valid_count(useful) or useful > recall
        or not isinstance(learn_status, str) or learn_status not in routecraft_telemetry.VALID_LEARN_STATUSES
        or (skip_reason is not None and (not isinstance(skip_reason, str) or skip_reason not in routecraft_telemetry.VALID_SKIP_REASONS))
    ):
        return False
    if mode == "off":
        return recall == useful == 0 and learn_status == "skipped" and skip_reason == "mode_off"
    if mode == "recall":
        return learn_status == "skipped" and skip_reason == "mode_recall_only"
    if mode == "full" and learn_status == "learned":
        return skip_reason is None
    return mode == "full" and learn_status == "skipped" and skip_reason in {
        "no_reusable_learning", "not_verified", "store_unavailable", "task_cancelled",
    }


def _valid_run(row: Mapping[str, object]) -> bool:
    if not RUN_BASE_KEYS.issubset(row) or not set(row).issubset(RUN_KEYS) or any(key in _RAW_KEYS for key in row):
        return False
    return (
        _valid_id(row["run_id"])
        and (_valid_id(row["parent_run_id"]) or row["parent_run_id"] is None)
        and _valid_id(row["device_id"])
        and isinstance(row["route_family"], str) and row["route_family"] in _ROUTE_FAMILY
        and _valid_token(row["role"])
        and _valid_token(row["human_model"], nullable=True)
        and _valid_token(row["human_effort"], nullable=True)
        and _valid_token(row["actual_model"])
        and _valid_token(row["actual_effort"])
        and _timestamp(row["started_at"]) is not None
        and _timestamp(row["ended_at"]) is not None
        and _timestamp(row["observed_at"]) is not None
        and all(_valid_count(row[key]) for key in (
            "duration_ms", "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens",
        ))
        and _valid_memory_bundle(row, required=False)
    )


def _valid_memory_task(row: Mapping[str, object]) -> bool:
    if set(row) != MEMORY_TASK_KEYS or any(key in _RAW_KEYS for key in row):
        return False
    return (
        _valid_id(row["task_run_id"])
        and _valid_id(row["parent_run_id"])
        and _valid_id(row["device_id"])
        and _valid_token(row["human_model"], nullable=True)
        and _valid_token(row["human_effort"], nullable=True)
        and _timestamp(row["completed_at"]) is not None
        and _timestamp(row["observed_at"]) is not None
        and _valid_memory_bundle(row, required=True)
    )


def _valid_family(name: str, row: Mapping[str, object]) -> bool:
    keys = V4_FAMILY_KEYS.get(name)
    if keys is None or set(row) != keys or any(key in _RAW_KEYS for key in row):
        return False
    identities = {
        "device_health": "health_id", "memory_metrics": "metric_id", "usage_snapshots": "snapshot_id",
        "benchmark_runs": "benchmark_run_id", "security_scans": "scan_id", "system_status": "status_id",
        "benchmark_metric_evidence": "evidence_id", "security_validations": "validation_id",
        "graph_runs": "graph_run_id", "graph_node_metrics": "node_metric_id", "graph_events": "graph_event_id",
        "policy_candidates": "policy_candidate_id", "security_rule_metrics": "security_rule_metric_id",
        "legacy_components": "component_observation_id",
    }
    identity_key = identities[name]
    if not _valid_id(row.get(identity_key)) or not _valid_id(row.get("device_id")) or not isinstance(row.get("observed_at"), str) or not _TIMESTAMP.fullmatch(row["observed_at"]):
        return False
    if name == "device_health":
        return row["os_family"] in _OS_FAMILY and isinstance(row["online"], bool) and _valid_label(row["routecraft_version"]) and row["plugin_health"] in _HEALTH and row["hook_health"] in _HEALTH and all(_valid_count(row[key]) for key in ("agents_healthy", "agents_total", "git_ahead", "git_behind", "git_conflicts")) and row["agents_healthy"] <= row["agents_total"] and isinstance(row["git_clean"], bool) and isinstance(row["memory_git_clean"], bool) and (row["last_sync_at"] is None or _timestamp(row["last_sync_at"]) is not None)
    if name == "memory_metrics":
        counts = ("local_projects", "local_memories", "context_injections", "handoffs", "decision_cases", "candidates", "rules", "eligible_candidates", "recall_count", "useful_count", "learn_count", "skipped_count")
        return all(_valid_count(row[key]) for key in counts) and row["useful_count"] <= row["recall_count"] and _valid_percent(row["usefulness_rate"]) and all(row[key] is None or _timestamp(row[key]) is not None for key in ("last_backup_at", "last_sync_at"))
    if name == "usage_snapshots":
        counts = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "sol_runs", "terra_runs", "luna_runs")
        return row["window_kind"] in _WINDOW_KIND and _valid_percent(row["used_percent"]) and _valid_percent(row["remaining_percent"]) and row["used_percent"] + row["remaining_percent"] == 100 and all(_valid_count(row[key]) for key in counts) and (row["reset_at"] is None or _timestamp(row["reset_at"]) is not None)
    if name == "benchmark_runs":
        percentages = ("current_success_rate", "candidate_success_rate", "current_quality", "candidate_quality", "current_test_pass_rate", "candidate_test_pass_rate")
        counts = ("current_tokens", "candidate_tokens", "current_duration_ms", "candidate_duration_ms", "current_rework", "candidate_rework")
        # The legacy v3 D1 columns are NOT NULL. Never coerce unavailable local
        # values to zero merely to satisfy that physical schema: the collector
        # omits an unavailable row instead (see _benchmark_transport_rows).
        return row["comparison_kind"] in _BENCHMARK_KIND and row["status"] in _BENCHMARK_STATUS and isinstance(row["measured"], bool) and _valid_label(row["current_label"]) and _valid_label(row["candidate_label"]) and all(_valid_percent(row[key]) for key in percentages) and all(_valid_count(row[key]) for key in counts) and row["winner"] in _WINNER and row["confidence"] in _CONFIDENCE
    if name == "security_scans":
        counts = ("critical_count", "high_count", "medium_count", "low_count", "info_count", "new_count", "resolved_count")
        return _valid_label(row["repository_hint"]) and row["status"] in _SECURITY_STATUS and row["baseline"] in _BASELINE and all(_valid_count(row[key]) for key in counts) and row["confidence"] in _CONFIDENCE
    if name == "system_status":
        return row["core_health"] in _HEALTH and _valid_label(row["plugin_version"]) and row["hook_health"] in _HEALTH and _valid_count(row["agents_healthy"]) and _valid_count(row["agents_total"]) and row["agents_healthy"] <= row["agents_total"] and row["collector_health"] in _HEALTH and _valid_label(row["collector_version"]) and all(row[key] in _HEALTH for key in ("memory_local_health", "decision_health", "control_health", "benchmark_health", "security_health"))
    if name == "benchmark_metric_evidence":
        case_count, sample_size, available = row["case_count"], row["sample_size"], row["available_count"]
        statistics = [row[key] for key in ("mean_value", "median_value", "min_value", "max_value")]
        success_count, success_rate = row["success_count"], row["success_rate"]
        if not all(_valid_count(item) for item in (case_count, sample_size, available)) or available > sample_size:
            return False
        has_success = success_count is not None or success_rate is not None
        if available == 0:
            if any(item is not None for item in statistics) or has_success:
                return False
        elif has_success:
            if any(item is not None for item in statistics) or not _valid_count(success_count) or success_count > available or not _valid_rate(success_rate):
                return False
            if abs(success_rate - success_count * 100 / available) > 0.01:
                return False
        else:
            if not all(_valid_finite(item) for item in statistics):
                return False
            mean, median, minimum, maximum = statistics
            if minimum > median or median > maximum or mean < minimum or mean > maximum:
                return False
        return _valid_label(row["suite_version"]) and row["mode"] in _REAL_BENCHMARK_MODES and row["metric"] in _REAL_BENCHMARK_METRICS and row["confidence"] in _CONFIDENCE and row["evidence_status"] in _EVIDENCE_STATUS
    if name == "security_validations":
        counts = [row[key] for key in ("rules_tested", "supported_rules", "fixture_pairs", "true_positive", "true_negative", "false_positive", "false_negative")]
        coverage = row["fixture_coverage"]
        detection = row["detection_rate"]
        false_positive_rate = row["false_positive_rate"]
        core_metrics = [*counts, coverage, detection, false_positive_rate]
        if row["status"] == "unavailable":
            if any(item is not None for item in core_metrics):
                return False
            dogfood = [row[key] for key in ("repositories_scanned", "useful_findings", "false_positive_findings", "unsupported_findings", "uncertain_findings")]
            return all(item is None for item in dogfood) and _valid_label(row["ruleset_version"]) and _valid_id(row["ruleset_digest"]) and row["confidence"] in _CONFIDENCE
        if not all(_valid_count(item) for item in counts):
            return False
        tested, supported, pairs, true_positive, true_negative, false_positive, false_negative = counts
        if tested > supported or true_positive + false_negative != pairs or true_negative + false_positive != pairs:
            return False
        if not _valid_rate(coverage) or abs(coverage - (tested * 100 / supported if supported else 0)) > 0.01:
            return False
        if pairs:
            if not _valid_rate(detection) or not _valid_rate(false_positive_rate) or abs(detection - true_positive * 100 / pairs) > 0.01 or abs(false_positive_rate - false_positive * 100 / pairs) > 0.01:
                return False
        elif detection is not None or false_positive_rate is not None:
            return False
        dogfood = [row[key] for key in ("repositories_scanned", "useful_findings", "false_positive_findings", "unsupported_findings", "uncertain_findings")]
        if any(item is None for item in dogfood) != all(item is None for item in dogfood) or any(item is not None and not _valid_count(item) for item in dogfood):
            return False
        return _valid_label(row["ruleset_version"]) and _valid_id(row["ruleset_digest"]) and row["status"] in _SECURITY_VALIDATION_STATUS and row["confidence"] in _CONFIDENCE
    if name == "graph_runs":
        counts = [row[key] for key in ("graph_schema_version", "graph_revision_count", "node_count", "edge_count", "parallel_width", "critical_path_length", "attempt_count", "retry_count", "send_back_count", "accepted_count", "frozen_count", "failed_count", "invalidated_count", "constraint_count", "checkpoint_count", "gate_pass_count", "gate_fail_count", "gate_inconclusive_count")]
        if not all(_valid_count(item) for item in counts):
            return False
        _, _, nodes, _, width, critical_path, _, _, _, accepted, frozen, failed, invalidated, _, _, _, _, _ = counts
        if any(item > nodes for item in (accepted, frozen, failed, invalidated)) or accepted + frozen + failed + invalidated > nodes or width > 16 or critical_path > nodes:
            return False
        if any(row[key] is not None and not _valid_count(row[key]) for key in ("duration_ms", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")):
            return False
        if row["status"] == "ACCEPTED" and (failed or invalidated or row["gate_pass_count"] < 1):
            return False
        return row["event_classification"] in _EVENT_CLASSIFICATION and row["mode"] in _GRAPH_MODE and row["status"] in _GRAPH_STATUS
    if name == "graph_node_metrics":
        counts = ("attempt_count", "retry_count")
        return (_valid_id(row["graph_run_id"]) and _valid_count(row["node_ordinal"]) and _valid_count(row["dependency_count"]) and row["node_type"] in _GRAPH_NODE_TYPE and _valid_label(row["lane"])
                and row["status"] in _GRAPH_NODE_STATUS and all(_valid_count(row[key]) for key in counts)
                and (row["send_back_count"] is None or _valid_count(row["send_back_count"]))
                and row["gate_status"] in _GRAPH_NODE_GATE_STATUS and (row["duration_ms"] is None or _valid_count(row["duration_ms"]))
                and (row["total_tokens"] is None or _valid_count(row["total_tokens"]))
                and all(isinstance(row[key], bool) for key in ("accepted", "frozen", "invalidated")))
    if name == "graph_events":
        return (_valid_id(row["graph_run_id"]) and row["event_type"] in _GRAPH_EVENT_TYPE
                and _valid_count(row["event_sequence"]) and row["status"] in _GRAPH_STATUS | _GRAPH_NODE_STATUS
                and all(row[key] is None or _valid_count(row[key]) for key in ("node_ordinal", "source_node_ordinal", "target_node_ordinal"))
                and (row["gate_status"] is None or row["gate_status"] in _EVIDENCE_GATE_RESULT)
                and (row["event_type"] != "gate" or row["gate_status"] in _EVIDENCE_GATE_RESULT)
                and (row["event_type"] != "dependency" or (row["source_node_ordinal"] is not None and row["target_node_ordinal"] is not None))
                and all(_valid_count(row[key]) for key in ("attempt_count", "affected_node_count", "constraint_count", "checkpoint_count")))
    if name == "policy_candidates":
        return (_valid_label(row["base_policy_version"]) and _valid_label(row["candidate_version"]) and row["candidate_change_kind"] in _POLICY_CANDIDATE_CHANGE_KIND
                and _valid_count(row["sample_size"]) and row["confidence"] in _CONFIDENCE
                and _valid_finite(row["expected_benefit"], nullable=True) and row["known_risk"] in _RISK_LEVEL
                and row["status"] in _POLICY_CANDIDATE_STATUS)
    if name == "security_rule_metrics":
        counts = ("true_positive", "true_negative", "false_positive", "false_negative")
        if not all(_valid_count(row[key]) for key in counts) or not _valid_rate(row["fixture_coverage"]):
            return False
        pairs = row["true_positive"] + row["false_negative"]
        negatives = row["true_negative"] + row["false_positive"]
        return (_valid_label(row["ruleset_version"]) and _valid_label(row["rule_id"])
                and (row["detection_rate"] is None if pairs == 0 else _valid_rate(row["detection_rate"]))
                and (row["false_positive_rate"] is None if negatives == 0 else _valid_rate(row["false_positive_rate"]))
                and row["confidence"] in _CONFIDENCE and row["status"] in _SECURITY_VALIDATION_STATUS)
    if name == "legacy_components":
        nullable_counts = [row[key] for key in ("observation_cycles", "consecutive_healthy_cycles", "missing_snapshots", "duplicate_ingestions")]
        if any(item is not None and not _valid_count(item) for item in nullable_counts):
            return False
        cycles, healthy, _, _ = nullable_counts
        if cycles is not None and healthy is not None and healthy > cycles:
            return False
        return row["component_kind"] in _LEGACY_COMPONENT_KIND and row["status"] in _LEGACY_STATUS and row["replacement_kind"] in _REPLACEMENT_KIND and (row["enabled"] is None or isinstance(row["enabled"], bool)) and (row["running"] is None or isinstance(row["running"], bool)) and (row["last_error_at"] is None or _timestamp(row["last_error_at"]) is not None) and row["replacement_health"] in _HEALTH and row["confidence"] in _CONFIDENCE
    return False


def validate_v3(payload: Mapping[str, object]) -> bool:
    allowed = {"schema_version", "runs", "memory_tasks", *FAMILY_KEYS}
    if set(payload) != allowed or payload.get("schema_version") != V3_SCHEMA_VERSION or not isinstance(payload.get("runs"), list) or not isinstance(payload.get("memory_tasks"), list):
        return False
    if any(not isinstance(row, Mapping) or not _valid_run(row) for row in payload["runs"]):
        return False
    if any(not isinstance(row, Mapping) or not _valid_memory_task(row) for row in payload["memory_tasks"]):
        return False
    for name in FAMILY_KEYS:
        rows = payload.get(name)
        if not isinstance(rows, list) or len(rows) > MAX_COLLECTION or any(not isinstance(row, Mapping) or not _valid_family(name, row) for row in rows):
            return False
    return True


def validate_v4(payload: Mapping[str, object]) -> bool:
    allowed = {"schema_version", "runs", "memory_tasks", *V4_FAMILY_KEYS}
    if set(payload) != allowed or payload.get("schema_version") != SCHEMA_VERSION or not isinstance(payload.get("runs"), list) or not isinstance(payload.get("memory_tasks"), list):
        return False
    if any(not isinstance(row, Mapping) or not _valid_run(row) for row in payload["runs"]):
        return False
    if any(not isinstance(row, Mapping) or not _valid_memory_task(row) for row in payload["memory_tasks"]):
        return False
    for name in V4_FAMILY_KEYS:
        rows = payload.get(name)
        if not isinstance(rows, list) or len(rows) > MAX_COLLECTION or any(not isinstance(row, Mapping) or not _valid_family(name, row) for row in rows):
            return False
    graph_rows = sum(len(payload[name]) for name in _GRAPH_BUNDLE_FAMILIES)
    if graph_rows > MAX_GRAPH_BUNDLE_ROWS:
        return False
    return True


def validate_payload(payload: Mapping[str, object]) -> bool:
    return validate_v3(payload) if payload.get("schema_version") == V3_SCHEMA_VERSION else validate_v4(payload)


def payload_batches(payload: Mapping[str, object], size: int = 400) -> list[dict[str, object]]:
    """Partition v3/v4 telemetry deterministically without duplicating summaries.

    IDs are already stable in the source payload, so retries or partial uploads
    remain idempotent at the server.  The six aggregate families describe a
    collection cycle and are included only in the first request.
    """
    if not isinstance(size, int) or size < 1 or size > MAX_COLLECTION or not validate_payload(payload):
        raise ValueError("invalid collector payload batch")
    runs = payload["runs"]
    memory_tasks = payload["memory_tasks"]
    assert isinstance(runs, list) and isinstance(memory_tasks, list)
    count = max(1, (len(runs) + size - 1) // size, (len(memory_tasks) + size - 1) // size)
    batches: list[dict[str, object]] = []
    for index in range(count):
        batch: dict[str, object] = {
            "schema_version": payload["schema_version"],
            "runs": runs[index * size:(index + 1) * size],
            "memory_tasks": memory_tasks[index * size:(index + 1) * size],
        }
        families = FAMILY_KEYS if payload["schema_version"] == V3_SCHEMA_VERSION else V4_FAMILY_KEYS
        for name in families:
            batch[name] = payload[name] if index == 0 else []
        if not validate_payload(batch):
            raise ValueError("invalid collector payload batch")
        batches.append(batch)
    return batches


def _safe(factory: Callable[[], dict[str, object]], fallback: Callable[[], dict[str, object]]) -> dict[str, object]:
    try:
        return factory()
    except Exception:
        return fallback()


def _benchmark_transport_rows(summary: Mapping[str, object]) -> list[dict[str, object]]:
    """Adapt local null semantics to the immutable legacy D1 contract.

    A missing measurement is represented locally by null. Because the v3 D1
    table cannot store null metrics, absence is transported as an empty family
    rather than a fabricated all-zero observation.
    """
    return [dict(summary)] if _valid_family("benchmark_runs", summary) else []


def collect_v3(*, source_root: Path | None = None, data_dir: str | None = None, sessions_dir: Path | None = None, codex_home: Path | None = None, since_days: int | None = 30, benchmark_result: Path | None = None) -> dict[str, object]:
    observed_at = utc_now()
    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    source_root = source_root or configured_source_root(home) or Path(__file__).resolve().parents[3]
    device_id = _device_id(home)
    sessions = sessions_dir or home / "sessions"
    try:
        runs = routecraft_telemetry.collect_runs(sessions, home, since_days, include_legacy=False)
        memory_tasks = routecraft_telemetry.collect_memory_tasks(sessions, home, since_days)
    except Exception:
        runs, memory_tasks = [], []
    typed_runs = [row for row in runs if isinstance(row, Mapping)]
    device = _safe(lambda: device_health(source_root, device_id, observed_at, home), lambda: unavailable_device_health(device_id, observed_at))
    try:
        memory, local_memory_available, decision_available = _memory_metrics_with_availability(data_dir, device_id, observed_at, home, source_root)
    except Exception:
        memory, local_memory_available, decision_available = None, False, False
    usage = usage_snapshots(device_id, observed_at, typed_runs)
    benchmark_path = benchmark_result or home / "routecraft" / "benchmark" / "latest-summary.json"
    security_path = home / "routecraft" / "security" / "latest-summary.json"
    benchmark = _safe(lambda: benchmark_summary(device_id, observed_at, benchmark_path), lambda: benchmark_summary(device_id, observed_at))
    security = _safe(lambda: security_summary(device_id, observed_at, security_path), lambda: security_summary(device_id, observed_at))
    status = _safe(
        lambda: system_status(
            device,
            usage,
            memory is not None,
            benchmark,
            security,
            device_id,
            observed_at,
            local_memory_available=local_memory_available,
            decision_available=decision_available,
        ),
        lambda: system_status(unavailable_device_health(device_id, observed_at), [], False, benchmark_summary(device_id, observed_at), security_summary(device_id, observed_at), device_id, observed_at),
    )
    payload: dict[str, object] = {"schema_version": V3_SCHEMA_VERSION, "runs": runs, "memory_tasks": memory_tasks, "device_health": [device], "memory_metrics": [memory] if memory else [], "usage_snapshots": usage, "benchmark_runs": _benchmark_transport_rows(benchmark), "security_scans": [security], "system_status": [status]}
    if not validate_v3(payload):
        device = unavailable_device_health(device_id, observed_at)
        benchmark = benchmark_summary(device_id, observed_at)
        security = security_summary(device_id, observed_at)
        payload = {"schema_version": V3_SCHEMA_VERSION, "runs": [], "memory_tasks": [], "device_health": [device], "memory_metrics": [], "usage_snapshots": [], "benchmark_runs": [], "security_scans": [security], "system_status": [system_status(device, [], False, benchmark, security, device_id, observed_at)]}
    return payload


def _summary_rows(path: Path, family: str, device_id: str) -> list[dict[str, object]]:
    """Read a direct list of exact aggregate rows; never adapt raw artifacts."""
    identity_keys = {
        "benchmark_metric_evidence": "evidence_id",
        "security_validations": "validation_id",
        "graph_runs": "graph_run_id",
        "graph_node_metrics": "node_metric_id",
        "graph_events": "graph_event_id",
        "policy_candidates": "policy_candidate_id",
        "security_rule_metrics": "security_rule_metric_id",
        "legacy_components": "component_observation_id",
    }
    try:
        if path.stat().st_size > 262_144:
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        candidates = value if isinstance(value, list) else [value]
        if len(candidates) > MAX_COLLECTION:
            return []
        rows: list[dict[str, object]] = []
        identity_key = identity_keys[family]
        for candidate in candidates:
            if not isinstance(candidate, Mapping) or not _valid_family(family, candidate):
                return []
            normalized = dict(candidate)
            normalized[identity_key] = _family_id(
                family,
                device_id,
                str(candidate["observed_at"]),
                str(candidate[identity_key]),
            )
            normalized["device_id"] = device_id
            if not _valid_family(family, normalized):
                return []
            rows.append(normalized)
        return rows
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []


def _empty_graph_bundle() -> dict[str, list[dict[str, object]]]:
    return {family: [] for family in _GRAPH_BUNDLE_FAMILIES}


def _read_graph_bundle(path: Path) -> object | None:
    """Read the canonical graph bundle only; never inspect graph state."""
    try:
        if path.stat().st_size > 262_144:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _normalize_graph_bundle(value: object, device_id: str) -> dict[str, list[dict[str, object]]]:
    """Validate and normalize a single all-or-nothing Graph v4 bundle.

    The collector changes the opaque device id for this collection cycle.  It
    must consequently rewrite graph-run foreign keys together, rather than
    treating the three graph families as unrelated summary files.  Any invalid
    or mixed bundle is omitted in full.  Over 75 valid rows becomes exact
    graph-run summaries only; it is never split into partial node/event data.
    """
    if not isinstance(value, Mapping) or set(value) != set(_GRAPH_BUNDLE_FAMILIES):
        return _empty_graph_bundle()
    source: dict[str, list[Mapping[str, object]]] = {}
    for family in _GRAPH_BUNDLE_FAMILIES:
        rows = value.get(family)
        if not isinstance(rows, list) or len(rows) > MAX_COLLECTION:
            return _empty_graph_bundle()
        if any(not isinstance(row, Mapping) or not _valid_family(family, row) for row in rows):
            return _empty_graph_bundle()
        source[family] = rows

    run_ids = [str(row["graph_run_id"]) for row in source["graph_runs"]]
    if len(run_ids) != len(set(run_ids)):
        return _empty_graph_bundle()
    replacement_runs = {
        original: _family_id("graph_runs", device_id, str(row["observed_at"]), original)
        for original, row in zip(run_ids, source["graph_runs"])
    }
    # A node or event without a run in this exact bundle could be stale data
    # from another graph export, so fail closed instead of sending a dangling
    # foreign key to D1.
    if any(str(row["graph_run_id"]) not in replacement_runs for family in ("graph_node_metrics", "graph_events") for row in source[family]):
        return _empty_graph_bundle()

    normalized: dict[str, list[dict[str, object]]] = _empty_graph_bundle()
    for row in source["graph_runs"]:
        copied = dict(row)
        original = str(copied["graph_run_id"])
        copied["graph_run_id"] = replacement_runs[original]
        copied["device_id"] = device_id
        if not _valid_family("graph_runs", copied):
            return _empty_graph_bundle()
        normalized["graph_runs"].append(copied)
    for family, identity_key in (("graph_node_metrics", "node_metric_id"), ("graph_events", "graph_event_id")):
        for row in source[family]:
            copied = dict(row)
            original_identity = str(copied[identity_key])
            copied["graph_run_id"] = replacement_runs[str(copied["graph_run_id"])]
            copied[identity_key] = _family_id(family, device_id, str(copied["observed_at"]), original_identity)
            copied["device_id"] = device_id
            if not _valid_family(family, copied):
                return _empty_graph_bundle()
            normalized[family].append(copied)

    # The collector's record cap applies to the entire graph, not each family.
    # Preserve only its safe run-level aggregate if details no longer fit.
    if sum(len(normalized[family]) for family in _GRAPH_BUNDLE_FAMILIES) > MAX_GRAPH_BUNDLE_ROWS:
        if len(normalized["graph_runs"]) > MAX_GRAPH_BUNDLE_ROWS:
            return _empty_graph_bundle()
        return {"graph_runs": normalized["graph_runs"], "graph_node_metrics": [], "graph_events": []}
    return normalized


def _legacy_graph_bundle(
    graph_result: Path,
    graph_node_result: Path,
    graph_event_result: Path,
    device_id: str,
) -> dict[str, list[dict[str, object]]]:
    """Transition reader for pre-bundle cache files, with the same coherence gate."""
    values = {
        "graph_runs": _read_graph_bundle(graph_result),
        "graph_node_metrics": _read_graph_bundle(graph_node_result),
        "graph_events": _read_graph_bundle(graph_event_result),
    }
    bundle = {
        family: value if isinstance(value, list) else [value] if isinstance(value, Mapping) else []
        for family, value in values.items()
    }
    return _normalize_graph_bundle(bundle, device_id)


def collect_v4(
    *,
    source_root: Path | None = None,
    data_dir: str | None = None,
    sessions_dir: Path | None = None,
    codex_home: Path | None = None,
    since_days: int | None = 30,
    benchmark_result: Path | None = None,
    benchmark_evidence_result: Path | None = None,
    security_validation_result: Path | None = None,
    graph_bundle_result: Path | None = None,
    graph_result: Path | None = None,
    graph_node_result: Path | None = None,
    graph_event_result: Path | None = None,
    policy_candidate_result: Path | None = None,
    security_rule_result: Path | None = None,
    legacy_result: Path | None = None,
) -> dict[str, object]:
    """Collect the additive v4 evidence families while retaining every v3 row."""
    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    v3 = collect_v3(
        source_root=source_root,
        data_dir=data_dir,
        sessions_dir=sessions_dir,
        codex_home=home,
        since_days=since_days,
        benchmark_result=benchmark_result,
    )
    device_rows = v3.get("device_health")
    device_id = str(device_rows[0]["device_id"]) if isinstance(device_rows, list) and device_rows and isinstance(device_rows[0], Mapping) else _device_id(home)
    paths = {
        "benchmark_metric_evidence": benchmark_evidence_result or home / "routecraft" / "benchmark" / "real-d1-summary.json",
        "security_validations": security_validation_result or home / "routecraft" / "security" / "validation-d1-summary.json",
        "policy_candidates": policy_candidate_result or home / "routecraft" / "policy" / "candidates-d1-summary.json",
        "security_rule_metrics": security_rule_result or home / "routecraft" / "security" / "rule-metrics-d1-summary.json",
        "legacy_components": legacy_result or home / "routecraft" / "legacy" / "latest-d1-summary.json",
    }
    payload = {
        **v3,
        "schema_version": SCHEMA_VERSION,
        **{family: _summary_rows(path, family, device_id) for family, path in paths.items()},
    }
    canonical_graph_bundle = graph_bundle_result or home / "routecraft" / "graph" / "latest-collector-v4.json"
    if graph_bundle_result is not None or canonical_graph_bundle.is_file():
        graph_families = _normalize_graph_bundle(_read_graph_bundle(canonical_graph_bundle), device_id)
    else:
        graph_families = _legacy_graph_bundle(
            graph_result or home / "routecraft" / "graph" / "latest-d1-summary.json",
            graph_node_result or home / "routecraft" / "graph" / "latest-node-metrics.json",
            graph_event_result or home / "routecraft" / "graph" / "latest-events.json",
            device_id,
        )
    payload.update(graph_families)
    statuses = payload.get("system_status")
    if isinstance(statuses, list):
        for status in statuses:
            if isinstance(status, dict):
                status["collector_version"] = "4.0.0"
    if validate_v4(payload):
        return payload
    fallback = {
        **fixture_payload(),
        "schema_version": SCHEMA_VERSION,
        "benchmark_metric_evidence": [],
        "security_validations": [],
        "graph_runs": [],
        "graph_node_metrics": [],
        "graph_events": [],
        "policy_candidates": [],
        "security_rule_metrics": [],
        "legacy_components": [],
    }
    fallback["system_status"][0]["collector_version"] = "4.0.0"
    return fallback


def fixture_payload() -> dict[str, object]:
    observed_at = "2026-08-24T00:00:00Z"
    device_id = opaque_id("fixture-device", "routecraft")
    device = unavailable_device_health(device_id, observed_at)
    benchmark = benchmark_summary(device_id, observed_at)
    security = security_summary(device_id, observed_at)
    return {"schema_version": V3_SCHEMA_VERSION, "runs": [], "memory_tasks": [], "device_health": [device], "memory_metrics": [], "usage_snapshots": [], "benchmark_runs": [], "security_scans": [security], "system_status": [system_status(device, [], False, benchmark, security, device_id, observed_at)]}


def fixture_payload_v4() -> dict[str, object]:
    payload = {
        **fixture_payload(),
        "schema_version": SCHEMA_VERSION,
        "benchmark_metric_evidence": [],
        "security_validations": [],
        "graph_runs": [],
        "graph_node_metrics": [],
        "graph_events": [],
        "policy_candidates": [],
        "security_rule_metrics": [],
        "legacy_components": [],
    }
    payload["system_status"][0]["collector_version"] = "4.0.0"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="routecraft-collector", description="Local privacy-safe RouteCraft Control Center collector")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--data-dir")
    parser.add_argument("--sessions-dir", type=Path)
    parser.add_argument("--since-days", type=int, default=30)
    parser.add_argument("--benchmark-result", type=Path)
    parser.add_argument("--benchmark-evidence-result", type=Path)
    parser.add_argument("--security-validation-result", type=Path)
    parser.add_argument("--graph-bundle-result", type=Path, help="Canonical atomic Graph v4 bundle (preferred)")
    parser.add_argument("--graph-result", type=Path)
    parser.add_argument("--graph-node-result", type=Path)
    parser.add_argument("--graph-event-result", type=Path)
    parser.add_argument("--policy-candidate-result", type=Path)
    parser.add_argument("--security-rule-result", type=Path)
    parser.add_argument("--legacy-result", type=Path)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--schema-v3", action="store_true", help="Emit the legacy v3 collector contract")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.fixture:
        payload = fixture_payload() if args.schema_v3 else fixture_payload_v4()
    elif args.schema_v3:
        payload = collect_v3(source_root=args.source_root, data_dir=args.data_dir, sessions_dir=args.sessions_dir, since_days=args.since_days, benchmark_result=args.benchmark_result)
    else:
        payload = collect_v4(
            source_root=args.source_root,
            data_dir=args.data_dir,
            sessions_dir=args.sessions_dir,
            since_days=args.since_days,
            benchmark_result=args.benchmark_result,
            benchmark_evidence_result=args.benchmark_evidence_result,
            security_validation_result=args.security_validation_result,
            graph_bundle_result=args.graph_bundle_result,
            graph_result=args.graph_result,
            graph_node_result=args.graph_node_result,
            graph_event_result=args.graph_event_result,
            policy_candidate_result=args.policy_candidate_result,
            security_rule_result=args.security_rule_result,
            legacy_result=args.legacy_result,
        )
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
