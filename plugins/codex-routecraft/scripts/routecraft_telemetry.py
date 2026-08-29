#!/usr/bin/env python3
"""Collect privacy-safe RouteCraft routing telemetry from local Codex rollouts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._+-]{1,80}$")
SAFE_SUMMARY = re.compile(r"^[\w +＋・、。！？!?（）()【】「」：:\-]{1,80}$", re.UNICODE)
FORBIDDEN_SUMMARY = re.compile(
    r"(?:\b(?:api[_ -]?key|access[_ -]?token|token|secret|password|credential|private[_ -]?key)\b|\bkey\s*:|APIキー|トークン|秘密鍵|秘密|パスワード|認証情報|\b(?:CASE|CAND|RULE|EVAL)-[A-Z0-9-]+\b|\b[0-9a-f]{32,64}\b|\b[0-9a-f]{8}-[0-9a-f-]{27,36}\b|[A-Za-z]:[\\/]|/(?:Users|home)/)",
    re.IGNORECASE,
)
LEGACY_ROLES = {"luna_light", "luna_max", "terra_worker", "reviewer"}
BUILTIN_ROLES = {"default", "worker", "explorer"}
TELEMETRY_SCHEMA_VERSION = 2
VALID_TASK_CLASSES = {"general", "debugging", "implementation", "ci", "refactor", "docs", "release", "integration", "test"}
VALID_MEMORY_MODES = {"off", "recall", "full"}
VALID_LEARN_STATUSES = {"learned", "skipped"}
VALID_SKIP_REASONS = {
    "mode_off",
    "mode_recall_only",
    "no_reusable_learning",
    "not_verified",
    "store_unavailable",
    "task_cancelled",
}
VALID_VERIFICATION_SETTINGS = {"auto_min", "none", "min", "strict", "release"}
VALID_VERIFICATION_BUDGETS = {"none", "min", "strict", "release"}
VALID_VERIFICATION_STATUSES = {"pass", "fail", "skipped", "not_required", "unknown"}
VALID_EVENT_CLASSIFICATIONS = {
    "normal", "token_burn_event", "reset_expectation", "benchmark_event",
    "migration_event", "stress_test", "manual_override",
}
VERIFICATION_COUNT_KEYS = (
    "tests_run", "targeted_tests", "full_suites", "builds", "lint_runs", "typechecks",
    "e2e_runs", "avoided_full_suites", "avoided_e2e", "avoided_builds", "avoided_lint",
    "avoided_typechecks", "verification_duration_ms",
)


@dataclass(frozen=True)
class SessionMeta:
    path: Path
    session_id: str
    started_at: str
    parent_id: str | None
    role: str | None


def read_json_line(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_session_meta(path: Path) -> SessionMeta | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first = read_json_line(handle.readline())
    except OSError:
        return None
    if not first or first.get("type") != "session_meta":
        return None
    payload = first.get("payload") or {}
    session_id = str(payload.get("id") or payload.get("session_id") or "")
    started_at = str(first.get("timestamp") or payload.get("timestamp") or "")
    source = payload.get("source") or {}
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = (subagent.get("thread_spawn") or {}) if isinstance(subagent, dict) else {}
    parent_id = str(spawn.get("parent_thread_id") or "") or None
    role = str(spawn.get("agent_role") or "") or None
    if not session_id or not started_at:
        return None
    return SessionMeta(path=path, session_id=session_id, started_at=started_at, parent_id=parent_id, role=role)


def first_context(path: Path, line_limit: int = 160) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= line_limit:
                    break
                row = read_json_line(line)
                if row and row.get("type") == "turn_context":
                    payload = row.get("payload")
                    return payload if isinstance(payload, dict) else {}
    except OSError:
        pass
    return {}


def final_usage(path: Path) -> tuple[dict[str, Any], dict[str, Any], str]:
    context: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    ended_at = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                row = read_json_line(line)
                if not row:
                    continue
                if row.get("timestamp"):
                    ended_at = str(row["timestamp"])
                if row.get("type") == "turn_context" and isinstance(row.get("payload"), dict):
                    context = row["payload"]
                payload = row.get("payload") or {}
                if payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
                    candidate = payload["info"].get("total_token_usage")
                    if isinstance(candidate, dict):
                        usage = candidate
    except OSError:
        pass
    return context, usage, ended_at


def safe_label(value: Any, fallback: str) -> str:
    candidate = str(value or "")
    return candidate if SAFE_TOKEN.fullmatch(candidate) else fallback


def nonnegative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def response_item_text(row: dict[str, Any]) -> str:
    """Read only assistant response text; do not inspect user prompts or context."""
    if row.get("type") != "response_item":
        return ""
    payload = row.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return ""
    if payload.get("role") != "assistant":
        return ""
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in {"text", "output_text"}:
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def safe_task_summary(value: str) -> str | None:
    candidate = value.strip()
    if not SAFE_SUMMARY.fullmatch(candidate) or FORBIDDEN_SUMMARY.search(candidate):
        return None
    return candidate


def parse_memory_marker(text: str) -> dict[str, Any] | None:
    """Parse a bounded, explicit RouteCraft marker without exporting its text."""
    lines = text.splitlines()
    marker: dict[str, str] | None = None
    for index, line in enumerate(lines):
        if line.strip() != "ROUTECRAFT MEMORY":
            continue
        candidate: dict[str, str] = {}
        for body_line in lines[index + 1:index + 10]:
            if body_line.strip() == "END ROUTECRAFT MEMORY":
                marker = candidate
                break
            if ":" not in body_line:
                marker = None
                break
            key, value = body_line.split(":", 1)
            key = key.strip()
            if key in candidate:
                marker = None
                break
            candidate[key] = value.strip()
        if marker is not None:
            break
    if marker is None:
        return None

    required = {
        "task_class",
        "task_summary",
        "memory_mode",
        "memory_recall_count",
        "memory_useful_count",
        "memory_learn_status",
    }
    if not required.issubset(marker):
        return None
    task_class = marker["task_class"].lower()
    task_summary = safe_task_summary(marker["task_summary"])
    mode = marker["memory_mode"].lower()
    learn_status = marker["memory_learn_status"].lower()
    if task_class not in VALID_TASK_CLASSES or task_summary is None or mode not in VALID_MEMORY_MODES or learn_status not in VALID_LEARN_STATUSES:
        return None
    recall_text = marker["memory_recall_count"]
    useful_text = marker["memory_useful_count"]
    if not re.fullmatch(r"\d{1,4}", recall_text) or not re.fullmatch(r"\d{1,4}", useful_text):
        return None
    recall_count = int(recall_text)
    useful_count = int(useful_text)
    if useful_count > recall_count:
        return None
    skip_reason = marker.get("memory_skip_reason")
    if mode == "off" and not (recall_count == useful_count == 0 and learn_status == "skipped" and skip_reason == "mode_off"):
        return None
    if mode == "recall" and not (learn_status == "skipped" and skip_reason == "mode_recall_only"):
        return None
    if mode == "full" and learn_status == "learned" and skip_reason is not None:
        return None
    if mode == "full" and learn_status == "skipped" and skip_reason not in {"no_reusable_learning", "not_verified", "store_unavailable", "task_cancelled"}:
        return None
    return {
        "task_class": task_class,
        "task_summary": task_summary,
        "memory_mode": mode,
        "memory_recall_count": recall_count,
        "memory_useful_count": useful_count,
        "memory_learn_status": learn_status,
        "memory_skip_reason": skip_reason,
    }


def routecraft_memory_markers(path: Path) -> list[dict[str, Any]]:
    """Return timestamped validated markers without retaining message text."""
    markers: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                row = read_json_line(line)
                if row is None:
                    continue
                parsed = parse_memory_marker(response_item_text(row))
                completed = parse_time(str(row.get("timestamp") or ""))
                if parsed is not None and completed is not None:
                    markers.append({**parsed, "completed_at": completed.isoformat().replace("+00:00", "Z")})
    except OSError:
        pass
    return sorted(markers, key=lambda item: str(item["completed_at"]))


def parse_verification_marker(text: str) -> dict[str, Any] | None:
    """Parse a finite assistant-only verification marker without retaining text."""
    lines = text.splitlines()
    marker: dict[str, str] | None = None
    for index, line in enumerate(lines):
        if line.strip() != "ROUTECRAFT VERIFICATION":
            continue
        candidate: dict[str, str] = {}
        for body_line in lines[index + 1:index + 28]:
            if body_line.strip() == "END ROUTECRAFT VERIFICATION":
                marker = candidate
                break
            if ":" not in body_line:
                marker = None
                break
            key, value = body_line.split(":", 1)
            key = key.strip()
            if key in candidate:
                marker = None
                break
            candidate[key] = value.strip()
        if marker is not None:
            break
    required = {
        "task_class", "task_summary", "setting", "budget", "status", "reason",
        "event_classification", *VERIFICATION_COUNT_KEYS,
    }
    if marker is None or set(marker) != required:
        return None
    task_class = marker["task_class"].lower()
    task_summary = safe_task_summary(marker["task_summary"])
    setting = marker["setting"].lower()
    budget = marker["budget"].lower()
    status = marker["status"].lower()
    reason = marker["reason"].lower()
    event_classification = marker["event_classification"].lower()
    if task_class not in VALID_TASK_CLASSES or task_summary is None:
        return None
    if setting not in VALID_VERIFICATION_SETTINGS or budget not in VALID_VERIFICATION_BUDGETS or status not in VALID_VERIFICATION_STATUSES:
        return None
    if event_classification not in VALID_EVENT_CLASSIFICATIONS or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
        return None
    counts: dict[str, int] = {}
    for key in VERIFICATION_COUNT_KEYS:
        value = marker[key]
        if not re.fullmatch(r"\d{1,10}", value):
            return None
        counts[key] = int(value)
    if counts["targeted_tests"] > counts["tests_run"] or budget == "none" and any(counts[key] for key in ("tests_run", "targeted_tests", "full_suites", "builds", "lint_runs", "typechecks", "e2e_runs")):
        return None
    return {
        "task_class": task_class, "task_summary": task_summary, "setting": setting,
        "budget": budget, "status": status, "reason": reason,
        "event_classification": event_classification, **counts,
    }


def routecraft_verification_markers(path: Path) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                row = read_json_line(line)
                if row is None:
                    continue
                parsed = parse_verification_marker(response_item_text(row))
                completed = parse_time(str(row.get("timestamp") or ""))
                if parsed is not None and completed is not None:
                    markers.append({**parsed, "completed_at": completed.isoformat().replace("+00:00", "Z")})
    except OSError:
        pass
    return sorted(markers, key=lambda item: str(item["completed_at"]))


def marker_for_run(markers: list[dict[str, Any]], started_at: str) -> dict[str, Any] | None:
    started = parse_time(started_at)
    if started is None:
        return None
    for marker in markers:
        completed = parse_time(str(marker.get("completed_at") or ""))
        if completed is not None and completed >= started:
            return {key: value for key, value in marker.items() if key != "completed_at"}
    return None


def has_routecraft_plan(path: Path, line_limit: int = 400) -> bool:
    """Detect only the exact declaration locally; never return message content."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= line_limit:
                    break
                row = read_json_line(line)
                if row and "ROUTECRAFT PLAN" in response_item_text(row):
                    return True
    except OSError:
        pass
    return False


def route_family(role: str | None, parent_is_routecraft: bool = False) -> str:
    if parent_is_routecraft:
        return "routecraft"
    if role and role.startswith("routecraft_"):
        return "routecraft"
    if role in LEGACY_ROLES:
        return "legacy"
    if role in BUILTIN_ROLES:
        return "builtin"
    return "unclassified"


def stable_hash(value: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()[:32]


def device_salt(codex_home: Path) -> str:
    for relative in ("routecraft/memory.json", "routecraft/device.json"):
        try:
            value = json.loads((codex_home / relative).read_text(encoding="utf-8"))
            candidate = str(value.get("device_id") or "")
            if candidate:
                return candidate
        except (OSError, ValueError, AttributeError):
            pass
    return platform.node() or "routecraft-device"


def index_sessions(sessions_dir: Path, since_days: int | None) -> dict[str, SessionMeta]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days) if since_days else None
    result: dict[str, SessionMeta] = {}
    if not sessions_dir.is_dir():
        return result
    for path in sessions_dir.rglob("*.jsonl"):
        if not path.is_file():
            continue
        if cutoff:
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                    continue
            except OSError:
                continue
        meta = read_session_meta(path)
        if meta:
            result[meta.session_id] = meta
    return result


def collect_runs(
    sessions_dir: Path,
    codex_home: Path,
    since_days: int | None,
    include_legacy: bool,
    include_unclassified: bool = False,
) -> list[dict[str, Any]]:
    sessions = index_sessions(sessions_dir, since_days)
    salt = device_salt(codex_home)
    device_id = stable_hash("device", salt)
    parent_contexts: dict[str, dict[str, Any]] = {}
    parent_routecraft: dict[str, bool] = {}
    parent_memory_markers: dict[str, list[dict[str, Any]]] = {}
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    runs: list[dict[str, Any]] = []
    for meta in sessions.values():
        if not meta.parent_id:
            continue
        parent = sessions.get(meta.parent_id)
        parent_is_routecraft = False
        if parent:
            parent_is_routecraft = parent_routecraft.setdefault(parent.session_id, has_routecraft_plan(parent.path))
        family = route_family(meta.role, parent_is_routecraft)
        if family in {"legacy", "builtin"} and not include_legacy:
            continue
        if family == "unclassified" and not include_unclassified:
            continue
        context, usage, ended_at = final_usage(meta.path)
        if not usage or not ended_at:
            continue
        parent_context: dict[str, Any] = {}
        memory_fields: dict[str, Any] = {
            "task_class": None,
            "task_summary": None,
            "memory_mode": None,
            "memory_recall_count": None,
            "memory_useful_count": None,
            "memory_learn_status": None,
            "memory_skip_reason": None,
        }
        if parent:
            parent_context = parent_contexts.setdefault(parent.session_id, first_context(parent.path))
            marker = marker_for_run(
                parent_memory_markers.setdefault(parent.session_id, routecraft_memory_markers(parent.path)),
                meta.started_at,
            )
            if marker is not None:
                memory_fields.update(marker)
        started = parse_time(meta.started_at)
        ended = parse_time(ended_at)
        duration_ms = max(0, int((ended - started).total_seconds() * 1000)) if started and ended else 0
        actual_model = safe_label(context.get("model"), "unknown-model")
        actual_effort = safe_label(context.get("effort") or context.get("reasoning_effort"), "unknown")
        human_model = safe_label(parent_context.get("model"), "") or None
        human_effort = safe_label(parent_context.get("effort") or parent_context.get("reasoning_effort"), "") or None
        runs.append({
            "run_id": stable_hash(meta.session_id, salt),
            "parent_run_id": stable_hash(meta.parent_id, salt),
            "device_id": device_id,
            "route_family": family,
            "role": safe_label(meta.role, "subagent"),
            "human_model": human_model,
            "human_effort": human_effort,
            "actual_model": actual_model,
            "actual_effort": actual_effort,
            "started_at": meta.started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "input_tokens": nonnegative_int(usage.get("input_tokens")),
            "cached_input_tokens": nonnegative_int(usage.get("cached_input_tokens")),
            "cache_write_input_tokens": nonnegative_int(usage.get("cache_write_input_tokens")),
            "output_tokens": nonnegative_int(usage.get("output_tokens")),
            "reasoning_output_tokens": nonnegative_int(usage.get("reasoning_output_tokens")),
            "total_tokens": nonnegative_int(usage.get("total_tokens")),
            "observed_at": observed_at,
            **memory_fields,
        })
    return sorted(runs, key=lambda item: item["started_at"], reverse=True)


def collect_memory_tasks(
    sessions_dir: Path,
    codex_home: Path,
    since_days: int | None,
) -> list[dict[str, Any]]:
    sessions = index_sessions(sessions_dir, since_days)
    salt = device_salt(codex_home)
    device_id = stable_hash("device", salt)
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tasks: list[dict[str, Any]] = []
    for meta in sessions.values():
        if not has_routecraft_plan(meta.path):
            continue
        context = first_context(meta.path)
        human_model = safe_label(context.get("model"), "") or None
        human_effort = safe_label(context.get("effort") or context.get("reasoning_effort"), "") or None
        for index, marker in enumerate(routecraft_memory_markers(meta.path)):
            completed_at = str(marker["completed_at"])
            tasks.append({
                "task_run_id": stable_hash(f"task|{meta.session_id}|{completed_at}|{index}", salt),
                "parent_run_id": stable_hash(meta.session_id, salt),
                "device_id": device_id,
                "human_model": human_model,
                "human_effort": human_effort,
                "task_class": marker["task_class"],
                "task_summary": marker["task_summary"],
                "memory_mode": marker["memory_mode"],
                "memory_recall_count": marker["memory_recall_count"],
                "memory_useful_count": marker["memory_useful_count"],
                "memory_learn_status": marker["memory_learn_status"],
                "memory_skip_reason": marker["memory_skip_reason"],
                "completed_at": completed_at,
                "observed_at": observed_at,
            })
    return sorted(tasks, key=lambda item: item["completed_at"], reverse=True)


def collect_verification_tasks(
    sessions_dir: Path,
    codex_home: Path,
    since_days: int | None,
) -> list[dict[str, Any]]:
    sessions = index_sessions(sessions_dir, since_days)
    salt = device_salt(codex_home)
    device_id = stable_hash("device", salt)
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tasks: list[dict[str, Any]] = []
    for meta in sessions.values():
        if not has_routecraft_plan(meta.path):
            continue
        for index, marker in enumerate(routecraft_verification_markers(meta.path)):
            completed_at = str(marker["completed_at"])
            tasks.append({
                "verification_task_id": stable_hash(f"verification|{meta.session_id}|{completed_at}|{index}", salt),
                "parent_run_id": stable_hash(meta.session_id, salt),
                "device_id": device_id,
                **{key: value for key, value in marker.items() if key != "completed_at"},
                "completed_at": completed_at,
                "observed_at": observed_at,
            })
    return sorted(tasks, key=lambda item: item["completed_at"], reverse=True)


def batches(items: list[dict[str, Any]], size: int = 400) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def send(endpoint: str, token: str, runs: list[dict[str, Any]], sites_bypass_token: str | None = None, memory_tasks: list[dict[str, Any]] | None = None) -> int:
    if not endpoint.startswith("https://") and not endpoint.startswith("http://localhost") and not endpoint.startswith("http://127.0.0.1"):
        raise ValueError("endpoint must use HTTPS unless it is localhost")
    accepted = 0
    run_groups = list(batches(runs))
    task_groups = list(batches(memory_tasks or []))
    for index in range(max(1, len(run_groups), len(task_groups))):
        run_group = run_groups[index] if index < len(run_groups) else []
        task_group = task_groups[index] if index < len(task_groups) else []
        body = json.dumps({"schema_version": TELEMETRY_SCHEMA_VERSION, "runs": run_group, "memory_tasks": task_group}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "RouteCraft-Telemetry/2",
        }
        if sites_bypass_token:
            headers["OAI-Sites-Authorization"] = f"Bearer {sites_bypass_token}"
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError("telemetry endpoint rejected the batch")
        accepted += nonnegative_int(result.get("accepted_runs", result.get("accepted")))
        accepted += nonnegative_int(result.get("accepted_memory_tasks"))
    return accepted


def main() -> None:
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions-dir", type=Path, default=codex_home / "sessions")
    parser.add_argument("--codex-home", type=Path, default=codex_home)
    parser.add_argument("--since-days", type=int, default=30)
    parser.add_argument("--include-legacy", action="store_true")
    parser.add_argument("--include-unclassified", action="store_true")
    parser.add_argument("--endpoint")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--sites-bypass-token-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--print", action="store_true", dest="print_payload")
    args = parser.parse_args()
    since_days = args.since_days if args.since_days > 0 else None
    runs = collect_runs(args.sessions_dir, args.codex_home, since_days, args.include_legacy, args.include_unclassified)
    memory_tasks = collect_memory_tasks(args.sessions_dir, args.codex_home, since_days)
    payload = {"schema_version": TELEMETRY_SCHEMA_VERSION, "runs": runs, "memory_tasks": memory_tasks}
    if args.output:
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.print_payload:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.endpoint:
        if not args.token_file:
            raise SystemExit("--token-file is required with --endpoint")
        token = args.token_file.expanduser().read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise SystemExit("token is too short")
        sites_bypass_token = None
        if args.sites_bypass_token_file:
            sites_bypass_token = args.sites_bypass_token_file.expanduser().read_text(encoding="utf-8").strip()
            if len(sites_bypass_token) < 32:
                raise SystemExit("Sites bypass token is too short")
        accepted = send(args.endpoint, token, runs, sites_bypass_token, memory_tasks)
        print(json.dumps({"ok": True, "collected": len(runs), "collected_memory_tasks": len(memory_tasks), "accepted": accepted}, separators=(",", ":")))
    elif not args.print_payload:
        print(json.dumps({"ok": True, "collected": len(runs), "collected_memory_tasks": len(memory_tasks), "output": bool(args.output)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
