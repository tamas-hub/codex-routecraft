"""Read-only observation ledger for RouteCraft legacy components.

The command in this module does not inspect, stop, disable, delete, archive,
or supersede a process.  Callers provide a small, already-redacted facts
document and a caller-owned ledger path.  The ledger is append-only at the
cycle level and is replaced atomically after every successful write.  The
resulting rows are intentionally the exact v4 ``legacy_components`` D1
contract, so they can be handed to the collector without adaptation.

Facts input (one observation cycle)::

    {
      "schema_version": 1,
      "device_id": "caller-device-identity",
      "observed_at": "2026-08-24T00:00:00Z",
      "components": [
        {
          "component_kind": "ai_usage_updater",
          "status": "disabled",
          "replacement_kind": "unified_usage_adapter",
          "enabled": false,
          "running": false,
          "replacement_health": "healthy",
          "missing_snapshots": 0,
          "duplicate_ingestions": 0,
          "last_error_at": null
        }
      ]
    }

Only aggregate facts are accepted.  Raw prompts, source/path identifiers,
transcripts, credentials, and similar fields are rejected before anything is
written.  The module uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
MIN_HEALTHY_CYCLES = 3
MAX_INPUT_BYTES = 262_144
MAX_CYCLES = 512
MAX_COMPONENTS = 32

COMPONENT_KINDS = {
    "ai_usage_updater",
    "codex_meter_startup",
    "observatory_legacy",
    "collector_legacy",
}
REPLACEMENT_KINDS = {
    "unified_usage_adapter",
    "control_center",
    "unified_collector",
    "none",
}
STATUSES = {"active", "disabled", "observing", "superseded", "archived", "unknown"}
HEALTH = {"healthy", "degraded", "unavailable", "disabled", "unknown"}
CONFIDENCE = {"low", "medium", "high"}
ROW_KEYS = {
    "component_observation_id",
    "device_id",
    "observed_at",
    "component_kind",
    "status",
    "replacement_kind",
    "enabled",
    "running",
    "observation_cycles",
    "consecutive_healthy_cycles",
    "last_error_at",
    "missing_snapshots",
    "duplicate_ingestions",
    "replacement_health",
    "confidence",
}

# These are rejected as keys anywhere in facts and ledger input.  The output
# contract itself has no such fields, but rejecting them at the boundary makes
# accidental forwarding of a raw artifact fail closed.
FORBIDDEN_KEYS = {
    "prompt",
    "prompts",
    "conversation",
    "transcript",
    "body",
    "content",
    "source",
    "sources",
    "path",
    "paths",
    "file",
    "files",
    "session_id",
    "raw_session_id",
    "secret",
    "secrets",
    "password",
    "cookie",
    "header",
    "headers",
    "authorization",
    "credential",
    "credentials",
    "private_key",
}
_OPAQUE = re.compile(r"^[a-f0-9]{32,64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$")


class ObservationError(ValueError):
    """Raised when caller-provided observation data is not safe or valid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp(value: object, *, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ObservationError("observed_at and last_error_at must be UTC ISO timestamps")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservationError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ObservationError("timestamp must include UTC Z")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _int(value: object, *, nullable: bool = True) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ObservationError("counts must be non-negative integers or null")
    return value


def _bool(value: object, *, nullable: bool = True) -> bool | None:
    if value is None and nullable:
        return None
    if not isinstance(value, bool):
        raise ObservationError("enabled and running must be booleans or null")
    return value


def opaque_device(value: object) -> str:
    """Return a stable opaque device identifier without exposing input text."""

    if isinstance(value, str) and _OPAQUE.fullmatch(value):
        return value
    if value is None or isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes, bytearray)):
        raise ObservationError("device_id must be a scalar")
    text = str(value).strip()
    if not text or len(text) > 256:
        raise ObservationError("device_id must be a bounded non-empty scalar")
    return hashlib.sha256(f"routecraft-legacy-observation-device:{text}".encode("utf-8", "replace")).hexdigest()[:32]


def _opaque_id(namespace: str, value: object) -> str:
    return hashlib.sha256(f"routecraft-legacy-observation:{namespace}:{value}".encode("utf-8", "replace")).hexdigest()[:32]


def _reject_forbidden(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = key.casefold() if isinstance(key, str) else ""
            if lowered in FORBIDDEN_KEYS or any(
                marker in lowered
                for marker in ("prompt", "transcript", "conversation", "source", "path", "secret", "credential", "cookie")
            ):
                raise ObservationError(f"forbidden raw field: {key}")
            _reject_forbidden(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_forbidden(child)


def _load_json(path: Path) -> object:
    try:
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise ObservationError("input JSON exceeds bounded size")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ObservationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObservationError(f"cannot read JSON input: {path.name}") from exc
    _reject_forbidden(value)
    return value


def _write_json_atomic(path: Path, value: object) -> None:
    """Write only the caller-selected file using a same-directory atomic swap."""

    path = path.expanduser()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ObservationError(f"cannot atomically write caller output: {path.name}") from exc
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _component_facts(value: object, cycle_observed_at: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ObservationError("each component must be an object")
    allowed = {
        "component_kind",
        "status",
        "replacement_kind",
        "enabled",
        "running",
        "replacement_health",
        "missing_snapshots",
        "duplicate_ingestions",
        "last_error_at",
    }
    if set(value) - allowed:
        raise ObservationError("component contains an unsupported field")
    kind = value.get("component_kind")
    replacement = value.get("replacement_kind", "none")
    status = value.get("status", "observing")
    health = value.get("replacement_health", "unknown")
    if not isinstance(kind, str) or kind not in COMPONENT_KINDS:
        raise ObservationError("unknown legacy component kind")
    if not isinstance(replacement, str) or replacement not in REPLACEMENT_KINDS:
        raise ObservationError("unknown replacement kind")
    if not isinstance(status, str) or status not in STATUSES:
        raise ObservationError("unknown component status")
    if not isinstance(health, str) or health not in HEALTH:
        raise ObservationError("unknown replacement health")
    return {
        "component_kind": kind,
        "status": status,
        "replacement_kind": replacement,
        "enabled": _bool(value.get("enabled")),
        "running": _bool(value.get("running")),
        "replacement_health": health,
        "missing_snapshots": _int(value.get("missing_snapshots")),
        "duplicate_ingestions": _int(value.get("duplicate_ingestions")),
        "last_error_at": _timestamp(value.get("last_error_at")),
    }


def _fact_cycles(value: object) -> tuple[str, list[dict[str, object]]]:
    """Normalize a facts document to an opaque device and cycle records."""

    if not isinstance(value, Mapping):
        raise ObservationError("facts must be a JSON object")
    if value.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ObservationError("unsupported facts schema_version")
    device_id = opaque_device(value.get("device_id"))
    root_observed = _timestamp(value.get("observed_at", utc_now()), nullable=False)
    raw_cycles = value.get("cycles")
    if raw_cycles is None:
        raw_cycles = [value]
    if not isinstance(raw_cycles, list) or not raw_cycles or len(raw_cycles) > MAX_CYCLES:
        raise ObservationError("facts must contain one to bounded cycles")
    cycles: list[dict[str, object]] = []
    for raw_cycle in raw_cycles:
        if not isinstance(raw_cycle, Mapping):
            raise ObservationError("each cycle must be an object")
        cycle_at = _timestamp(raw_cycle.get("observed_at", root_observed), nullable=False)
        components = raw_cycle.get("components")
        if not isinstance(components, list) or len(components) > MAX_COMPONENTS:
            raise ObservationError("each cycle needs a bounded components list")
        normalized = [_component_facts(item, cycle_at) for item in components]
        kinds = [str(item["component_kind"]) for item in normalized]
        if len(set(kinds)) != len(kinds):
            raise ObservationError("a cycle may contain each component kind once")
        cycle_key = _opaque_id(
            "cycle",
            f"{cycle_at}:{json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}",
        )
        cycles.append({"cycle_id": cycle_key, "observed_at": cycle_at, "components": normalized})
    return device_id, cycles


def _load_ledger(path: Path, device_id: str) -> dict[str, object]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "device_id": device_id, "cycles": []}
    value = _load_json(path)
    if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA_VERSION:
        raise ObservationError("invalid observation ledger schema")
    if value.get("device_id") != device_id:
        raise ObservationError("ledger device_id does not match facts")
    cycles = value.get("cycles")
    if not isinstance(cycles, list) or len(cycles) > MAX_CYCLES:
        raise ObservationError("invalid observation ledger cycles")
    normalized_cycles: list[dict[str, object]] = []
    for cycle in cycles:
        if not isinstance(cycle, Mapping):
            raise ObservationError("invalid observation ledger cycle")
        cycle_at = _timestamp(cycle.get("observed_at"), nullable=False)
        cycle_id = cycle.get("cycle_id")
        components = cycle.get("components")
        if not isinstance(cycle_id, str) or not _OPAQUE.fullmatch(cycle_id):
            raise ObservationError("invalid observation ledger cycle id")
        if not isinstance(components, list) or len(components) > MAX_COMPONENTS:
            raise ObservationError("invalid observation ledger components")
        normalized_components = [_component_facts(item, cycle_at) for item in components]
        normalized_cycles.append({"cycle_id": cycle_id, "observed_at": cycle_at, "components": normalized_components})
    return {"schema_version": SCHEMA_VERSION, "device_id": device_id, "cycles": normalized_cycles}


def _healthy_component(component: Mapping[str, object]) -> bool:
    return (
        component.get("replacement_kind") != "none"
        and component.get("replacement_health") == "healthy"
        and component.get("last_error_at") is None
        and component.get("missing_snapshots") == 0
        and component.get("duplicate_ingestions") == 0
    )


def _rows(ledger: Mapping[str, object]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    device_id = str(ledger["device_id"])
    cycles = [item for item in ledger.get("cycles", []) if isinstance(item, Mapping)]
    if not cycles:
        return [], []
    latest_at = max(str(cycle["observed_at"]) for cycle in cycles)
    by_kind: dict[str, list[tuple[Mapping[str, object], Mapping[str, object] | None]]] = {}
    for cycle in cycles:
        components = cycle.get("components", [])
        present = {str(item.get("component_kind")): item for item in components if isinstance(item, Mapping)}
        for kind in COMPONENT_KINDS:
            by_kind.setdefault(kind, []).append((cycle, present.get(kind)))

    output: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    for kind in sorted(by_kind):
        history = sorted(by_kind[kind], key=lambda item: str(item[0]["observed_at"]))
        present = [(cycle, component) for cycle, component in history if component is not None]
        if not present:
            continue
        latest_cycle, latest = max(present, key=lambda item: str(item[0]["observed_at"]))
        assert latest is not None
        observation_cycles = len(present)
        missing_from_ledger = len(history) - observation_cycles
        missing_values = [component.get("missing_snapshots") for _, component in present]
        missing_snapshots = None if any(value is None for value in missing_values) else missing_from_ledger + sum(int(value) for value in missing_values)
        cycle_ids: dict[str, int] = {}
        for cycle, component in present:
            cycle_id = str(cycle["cycle_id"])
            cycle_ids[cycle_id] = cycle_ids.get(cycle_id, 0) + 1
        duplicate_values = [component.get("duplicate_ingestions") for _, component in present]
        duplicate_cycles = sum(max(0, count - 1) for count in cycle_ids.values())
        duplicate_ingestions = None if any(value is None for value in duplicate_values) else duplicate_cycles + sum(int(value) for value in duplicate_values)
        healthy = 0
        for cycle, component in reversed(history):
            cycle_id = str(cycle["cycle_id"])
            if component is None or cycle_ids.get(cycle_id, 0) > 1 or not _healthy_component(component):
                break
            healthy += 1
        error_times = [str(component["last_error_at"]) for _, component in present if component.get("last_error_at")]
        last_error_at = max(error_times) if error_times else None
        unknown_fields = any(
            component.get(field) is None
            for _, component in present
            for field in ("enabled", "running", "replacement_health", "missing_snapshots", "duplicate_ingestions")
        )
        if observation_cycles < 1 or unknown_fields or missing_snapshots or duplicate_ingestions or healthy < MIN_HEALTHY_CYCLES:
            confidence = "low" if observation_cycles < MIN_HEALTHY_CYCLES or unknown_fields else "medium"
        else:
            confidence = "high"
        # Status is always the explicitly observed status.  In particular, a
        # healthy replacement never silently changes a legacy component to
        # superseded or archived.
        row = {
            "component_observation_id": _opaque_id("component", f"{device_id}:{kind}:{latest_cycle['observed_at']}"),
            "device_id": device_id,
            "observed_at": latest_at,
            "component_kind": kind,
            "status": latest["status"],
            "replacement_kind": latest["replacement_kind"],
            "enabled": latest["enabled"],
            "running": latest["running"],
            "observation_cycles": observation_cycles,
            "consecutive_healthy_cycles": healthy,
            "last_error_at": last_error_at,
            "missing_snapshots": missing_snapshots,
            "duplicate_ingestions": duplicate_ingestions,
            "replacement_health": latest["replacement_health"],
            "confidence": confidence,
        }
        if set(row) != ROW_KEYS:
            raise ObservationError("internal legacy row contract drift")
        eligible = (
            healthy >= MIN_HEALTHY_CYCLES
            and missing_snapshots == 0
            and duplicate_ingestions == 0
            and latest["replacement_kind"] != "none"
            and latest["replacement_health"] == "healthy"
        )
        summaries.append(
            {
                "component_kind": kind,
                "replacement_kind": latest["replacement_kind"],
                "supersede_eligible": eligible,
                "archive_eligible": False,
                "status_unchanged": True,
                "reason": "minimum healthy observation threshold met" if eligible else "insufficient consecutive healthy evidence",
            }
        )
        output.append(row)
    return output, summaries


def observe(facts_path: Path, ledger_path: Path, output_path: Path) -> dict[str, object]:
    """Append facts cycles and atomically write exact D1 rows to output_path."""

    facts = _load_json(facts_path)
    device_id, incoming = _fact_cycles(facts)
    ledger = _load_ledger(ledger_path, device_id)
    cycles = list(ledger.get("cycles", []))
    if len(cycles) + len(incoming) > MAX_CYCLES:
        raise ObservationError("observation ledger exceeds bounded cycle count")
    cycles.extend(incoming)
    ledger = {"schema_version": SCHEMA_VERSION, "device_id": device_id, "cycles": cycles}
    rows, summaries = _rows(ledger)
    # The ledger deliberately contains only normalized aggregate facts.  The
    # output is a direct list so collector._summary_rows can round-trip it.
    _write_json_atomic(ledger_path, ledger)
    _write_json_atomic(output_path, rows)
    return {"schema_version": SCHEMA_VERSION, "rows": rows, "summary": summaries, "ledger_cycles": len(cycles)}


def summarize(ledger_path: Path, output_path: Path | None = None) -> dict[str, object]:
    value = _load_json(ledger_path)
    if not isinstance(value, Mapping):
        raise ObservationError("invalid observation ledger")
    device_id = opaque_device(value.get("device_id"))
    ledger = _load_ledger(ledger_path, device_id)
    rows, summaries = _rows(ledger)
    result = {"schema_version": SCHEMA_VERSION, "rows": rows, "summary": summaries, "ledger_cycles": len(ledger.get("cycles", []))}
    if output_path is not None:
        _write_json_atomic(output_path, result)
    return result


def _json_print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RouteCraft read-only legacy observation ledger")
    subparsers = parser.add_subparsers(dest="command", required=True)
    observe_parser = subparsers.add_parser("observe", help="append a redacted observation cycle")
    observe_parser.add_argument("--facts", type=Path, required=True)
    observe_parser.add_argument("--ledger", type=Path, required=True)
    observe_parser.add_argument("--output", type=Path, required=True)
    summarize_parser = subparsers.add_parser("summarize", help="summarize a caller-owned ledger")
    summarize_parser.add_argument("--ledger", type=Path, required=True)
    summarize_parser.add_argument("--output", type=Path)
    validate_parser = subparsers.add_parser("validate", help="validate facts without writing")
    validate_parser.add_argument("--facts", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "observe":
            _json_print(observe(args.facts, args.ledger, args.output))
        elif args.command == "summarize":
            _json_print(summarize(args.ledger, args.output))
        else:
            value = _load_json(args.facts)
            device_id, cycles = _fact_cycles(value)
            _json_print({"valid": True, "schema_version": SCHEMA_VERSION, "device_id": device_id, "cycles": len(cycles)})
        return 0
    except ObservationError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
