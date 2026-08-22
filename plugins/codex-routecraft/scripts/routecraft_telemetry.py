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

SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._+:/-]{1,80}$")
LEGACY_ROLES = {"luna_light", "luna_max", "terra_worker", "reviewer"}
BUILTIN_ROLES = {"default", "worker", "explorer"}


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


def has_routecraft_plan(path: Path, line_limit: int = 400) -> bool:
    """Detect only the exact declaration locally; never return message content."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= line_limit:
                    break
                if "ROUTECRAFT PLAN" not in line:
                    continue
                row = read_json_line(line)
                if row and row.get("type") == "response_item":
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
        if parent:
            parent_context = parent_contexts.setdefault(parent.session_id, first_context(parent.path))
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
        })
    return sorted(runs, key=lambda item: item["started_at"], reverse=True)


def batches(items: list[dict[str, Any]], size: int = 400) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def send(endpoint: str, token: str, runs: list[dict[str, Any]], sites_bypass_token: str | None = None) -> int:
    if not endpoint.startswith("https://") and not endpoint.startswith("http://localhost") and not endpoint.startswith("http://127.0.0.1"):
        raise ValueError("endpoint must use HTTPS unless it is localhost")
    accepted = 0
    for group in batches(runs):
        body = json.dumps({"schema_version": 1, "runs": group}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "RouteCraft-Telemetry/1",
        }
        if sites_bypass_token:
            headers["OAI-Sites-Authorization"] = f"Bearer {sites_bypass_token}"
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError("telemetry endpoint rejected the batch")
        accepted += nonnegative_int(result.get("accepted"))
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
    payload = {"schema_version": 1, "runs": runs}
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
        accepted = send(args.endpoint, token, runs, sites_bypass_token)
        print(json.dumps({"ok": True, "collected": len(runs), "accepted": accepted}, separators=(",", ":")))
    elif not args.print_payload:
        print(json.dumps({"ok": True, "collected": len(runs), "output": bool(args.output)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
