"""Small EventSource adapters and bounded read-only Praxis queries."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping

from .projection import DASHBOARD_API_VERSION, _safe_event, build_snapshot, run_records, safe_run

try:
    from routecraft_protocols import validate_event  # type: ignore
except ImportError:  # Concurrent protocol package may not have landed yet.
    def validate_event(event: Mapping[str, Any]) -> None:
        required = {"schema_version", "event", "event_id", "timestamp", "source", "provider", "agent", "model", "project", "task_id", "status", "event_classification", "metadata"}
        if not isinstance(event, Mapping) or not required.issubset(event):
            raise ValueError("invalid event protocol")

try:
    from routecraft_protocols import ROUTECRAFT_TELEMETRY_KEYS, validate_routecraft_telemetry  # type: ignore
except ImportError:
    validate_routecraft_telemetry = None  # type: ignore
    ROUTECRAFT_TELEMETRY_KEYS = ()  # type: ignore

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LEGACY_FILES = ("routecraft-telemetry.json", "routecraft_telemetry.json", "routecraft-collector.json", "routecraft_collector.json")
_FALLBACK_TELEMETRY_KEYS = (
    "schema_version", "run_id", "session_id", "requested_model", "requested_reasoning", "selected_model",
    "selected_reasoning", "actual_model", "actual_reasoning", "route_decision_model", "route_decision_reasoning",
    "decision_source", "decision_reason", "decision_confidence", "route_changed", "memory_recall_used",
    "memory_case_ids", "rules_applied", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
    "total_tokens", "execution_time_ms", "retry_count", "model_calls", "tool_calls", "file_reads", "routecraft_version",
    "memory_version", "collector_version", "dashboard_version", "benchmark",
)
_TELEMETRY_KEYS = tuple(ROUTECRAFT_TELEMETRY_KEYS) or _FALLBACK_TELEMETRY_KEYS


def _safe_id(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and _ID.fullmatch(value) else fallback


def _timestamp(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value) else "1970-01-01T00:00:00Z"


def _bounded_label(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _observed_actual_label(value: Any) -> str | None:
    label = _bounded_label(value)
    return None if label is None or label.lower() in {"unknown", "unknown-model", "unobserved"} else label


def _legacy_envelope(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    role = _bounded_label(row.get("role"))
    requested_model = _bounded_label(row.get("human_model"))
    requested_reasoning = _bounded_label(row.get("human_effort"))
    actual_model = _observed_actual_label(row.get("actual_model"))
    actual_reasoning = _observed_actual_label(row.get("actual_effort"))
    selected = re.fullmatch(r"routecraft_(sol|terra|luna)_(low|medium|high|xhigh|max|ultra)", role or "", re.I)
    explicit_source = row.get("decision_source")
    explicit_source = explicit_source.strip().lower() if isinstance(explicit_source, str) else None
    decision_source = explicit_source if explicit_source in {"routecraft", "user", "codex", "fallback", "unknown"} else ("routecraft" if selected else "unknown")
    attributable = decision_source == "routecraft"
    selected_model = selected.group(1).lower() if selected else None
    selected_reasoning = selected.group(2).lower() if selected else None
    models_differ = requested_model is not None and actual_model is not None and requested_model != actual_model
    route_changed = True if attributable and models_differ else (
        requested_reasoning != actual_reasoning if attributable and requested_model and actual_model and requested_model == actual_model and requested_reasoning and actual_reasoning else None
    )
    values: dict[str, Any] = {key: None for key in _TELEMETRY_KEYS}
    values.update({
        "schema_version": "1", "run_id": _safe_id(row.get("run_id"), f"legacy-{index}"), "session_id": None,
        "requested_model": requested_model, "requested_reasoning": requested_reasoning,
        "selected_model": selected_model, "selected_reasoning": selected_reasoning,
        "actual_model": actual_model, "actual_reasoning": actual_reasoning,
        "route_decision_model": selected_model, "route_decision_reasoning": selected_reasoning,
        "decision_source": decision_source, "decision_reason": _bounded_label(row.get("decision_reason")),
        "decision_confidence": row.get("decision_confidence") if isinstance(row.get("decision_confidence"), (int, float)) and not isinstance(row.get("decision_confidence"), bool) and 0 <= row["decision_confidence"] <= 1 else None,
        "route_changed": route_changed,
        "memory_recall_used": bool(row.get("memory_recall_count", 0)) if isinstance(row.get("memory_recall_count"), int) else None,
        "memory_case_ids": [], "rules_applied": [],
        "input_tokens": row.get("input_tokens") if isinstance(row.get("input_tokens"), int) and row["input_tokens"] >= 0 else None,
        "cached_input_tokens": row.get("cached_input_tokens") if isinstance(row.get("cached_input_tokens"), int) and row["cached_input_tokens"] >= 0 else None,
        "output_tokens": row.get("output_tokens") if isinstance(row.get("output_tokens"), int) and row["output_tokens"] >= 0 else None,
        "reasoning_tokens": row.get("reasoning_output_tokens") if isinstance(row.get("reasoning_output_tokens"), int) and row["reasoning_output_tokens"] >= 0 else None,
        "total_tokens": row.get("total_tokens") if isinstance(row.get("total_tokens"), int) and row["total_tokens"] >= 0 else None,
        "execution_time_ms": row.get("duration_ms") if isinstance(row.get("duration_ms"), int) and row["duration_ms"] >= 0 else None,
        "retry_count": row.get("retry_count") if isinstance(row.get("retry_count"), int) and not isinstance(row.get("retry_count"), bool) and row["retry_count"] >= 0 else None,
        "tool_calls": row.get("tool_calls") if isinstance(row.get("tool_calls"), int) and not isinstance(row.get("tool_calls"), bool) and row["tool_calls"] >= 0 else None,
        "file_reads": row.get("file_reads") if isinstance(row.get("file_reads"), int) and not isinstance(row.get("file_reads"), bool) and row["file_reads"] >= 0 else None,
        "routecraft_version": _bounded_label(row.get("routecraft_version")), "memory_version": _bounded_label(row.get("memory_version")),
        "collector_version": _bounded_label(row.get("collector_version")), "dashboard_version": _bounded_label(row.get("dashboard_version")),
        "benchmark": row.get("benchmark") if isinstance(row.get("benchmark"), Mapping) else None,
    })
    if "model_calls" in values:
        value = row.get("model_calls")
        values["model_calls"] = value if isinstance(value, int) and value >= 0 else None
    return values


class PraxisDashboardQuery:
    def __init__(self, event_source: Any = None) -> None:
        self.event_source = event_source

    def _state(self) -> dict[str, Any]:
        if self.event_source is None:
            return {"available": False, "code": "unavailable", "sources": []}
        try:
            raw = self.event_source.sources() if callable(getattr(self.event_source, "sources", None)) else [{"id": "local", "available": True}]
            if not isinstance(raw, list):
                raise ValueError("sources must be a list")
            sources = [{"id": item["id"], "available": item.get("available")} for item in raw if isinstance(item, Mapping) and isinstance(item.get("id"), str) and _ID.fullmatch(item["id"]) and (item.get("available") is None or isinstance(item.get("available"), bool))]
            return {"available": any(item["available"] is True for item in sources), "code": None if any(item["available"] is True for item in sources) else "unavailable", "sources": sources}
        except Exception:
            return {"available": False, "code": "source_error", "sources": []}

    def _events(self, limit: int = 500, cursor: str | None = None, source: str | None = None, include_special_events: bool = True) -> tuple[list[dict[str, Any]], str | None, str | None]:
        if self.event_source is None:
            return [], None, "unavailable"
        try:
            result = self.event_source.list_events(limit=limit, cursor=cursor, source=source, include_special_events=include_special_events)
            rows, next_cursor = (result.get("events", result.get("items", [])), result.get("cursor") or result.get("next_cursor")) if isinstance(result, Mapping) else (result, None)
            clean = []
            for item in rows if isinstance(rows, list) else []:
                try:
                    if isinstance(item, Mapping) and isinstance(item.get("payload"), Mapping):
                        item = item["payload"]
                    validate_event(item)
                    metadata = item.get("metadata") if isinstance(item, Mapping) else None
                    envelope = metadata.get("routecraft_telemetry") if isinstance(metadata, Mapping) else None
                    if isinstance(envelope, Mapping) and callable(validate_routecraft_telemetry):
                        validate_routecraft_telemetry(envelope)
                    clean.append(dict(item))
                except Exception:
                    continue
            return clean, str(next_cursor) if next_cursor else None, None
        except Exception:
            return [], None, "source_error"

    def snapshot(self) -> dict[str, Any]:
        state = self._state()
        if not state["available"]:
            return {"api_version": DASHBOARD_API_VERSION, "available": False, "code": state["code"], "data": build_snapshot([], state["sources"])}
        rows, _, error = self._events(limit=500)
        return {"api_version": DASHBOARD_API_VERSION, "available": error is None, "code": error, "data": build_snapshot(rows if error is None else [], state["sources"])}

    def events(self, limit: int = 100, cursor: str | None = None, source: str | None = None) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500)); state = self._state()
        if not state["available"]:
            return {"api_version": DASHBOARD_API_VERSION, "available": False, "code": state["code"], "events": [], "cursor": None}
        rows, next_cursor, error = self._events(limit, cursor, source)
        return {"api_version": DASHBOARD_API_VERSION, "available": error is None, "code": error, "events": [_safe_event(row) for row in rows], "cursor": next_cursor}

    def runs(self, limit: int = 100, requested_model: str | None = None, actual_model: str | None = None, requested_reasoning: str | None = None, actual_reasoning: str | None = None) -> dict[str, Any]:
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 100
        state = self._state()
        if not state["available"]:
            return {"api_version": DASHBOARD_API_VERSION, "available": False, "code": state["code"], "runs": []}
        rows, _, error = self._events(limit=500)
        if error:
            return {"api_version": DASHBOARD_API_VERSION, "available": False, "code": error, "runs": []}
        filters = {"requested_model": requested_model, "actual_model": actual_model, "requested_reasoning": requested_reasoning, "actual_reasoning": actual_reasoning}
        if any(value is not None and (not isinstance(value, str) or not _ID.fullmatch(value)) for value in filters.values()):
            return {"api_version": DASHBOARD_API_VERSION, "available": False, "code": "invalid_filter", "runs": []}
        def filter_matches(value: Any, requested: str | None) -> bool:
            if requested is None:
                return True
            expected = requested.lower()
            return value is None if expected == "unknown" else value == expected
        def matches(record: Mapping[str, Any]) -> bool:
            return (
                filter_matches(record.get("requested_family"), requested_model)
                and filter_matches(record.get("actual_family"), actual_model)
                and filter_matches(record.get("requested_reasoning"), requested_reasoning)
                and filter_matches(record.get("actual_reasoning"), actual_reasoning)
            )
        result = [record for record in run_records(rows) if matches(record)]
        return {"api_version": DASHBOARD_API_VERSION, "available": True, "code": None, "runs": [safe_run(row) for row in result[:limit]], "total": len(result)}

    def sources(self) -> dict[str, Any]:
        return {"api_version": DASHBOARD_API_VERSION, **self._state()}


class JsonlEventSource:
    """Optional, side-effect-free local adapter for a pre-existing JSONL event file."""
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
    def sources(self) -> list[dict[str, Any]]:
        return [{"id": "local", "available": self.path.is_file() and not self.path.is_symlink()}]
    def list_events(self, *, limit: int, cursor: str | None = None, source: str | None = None, include_special_events: bool = True) -> dict[str, Any]:
        if not self.path.is_file() or self.path.is_symlink() or self.path.stat().st_size > 32 * 1024 * 1024:
            return {"events": [], "cursor": None}
        rows = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
                if isinstance(item, dict) and (source is None or item.get("source") == source):
                    rows.append(item)
            except json.JSONDecodeError:
                continue
        start = int(cursor or 0) if str(cursor or "").isdigit() else 0
        rows.reverse()
        return {"events": rows[start:start + limit], "cursor": str(start + limit) if start + limit < len(rows) else None}


class LegacyTelemetryEventSource:
    """Read schema 2 telemetry and schema 3/4 collector JSON without mutation."""
    def __init__(self, path: str | Path, source_id: str) -> None:
        self.path, self.source_id = Path(path), source_id
    def _payload(self) -> Mapping[str, Any] | None:
        if not self.path.is_file() or self.path.is_symlink() or self.path.stat().st_size > 8 * 1024 * 1024:
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping) or value.get("schema_version") not in (2, 3, 4, 5) or not isinstance(value.get("runs"), list):
                return None
            return value
        except (OSError, ValueError, json.JSONDecodeError):
            return None
    def sources(self) -> list[dict[str, Any]]:
        return [{"id": self.source_id, "available": self._payload() is not None}]
    def list_events(self, *, limit: int, cursor: str | None = None, source: str | None = None, include_special_events: bool = True) -> dict[str, Any]:
        payload = self._payload()
        if payload is None or (source is not None and source != self.source_id):
            return {"events": [], "cursor": None}
        events = []
        for index, row in enumerate(payload["runs"]):
            if not isinstance(row, Mapping):
                continue
            envelope = _legacy_envelope(row, index)
            # Legacy task summaries are intentionally not carried into dashboard events.
            event = {"schema_version": "1", "event": "execution.completed", "event_id": _safe_id(row.get("run_id"), f"legacy-{index}"),
                     "timestamp": _timestamp(row.get("ended_at", row.get("observed_at", row.get("started_at")))), "source": self.source_id,
                     "provider": None, "agent": _bounded_label(row.get("role")), "model": _bounded_label(row.get("actual_model")),
                     "project": None, "task_id": None, "status": "completed", "event_classification": "normal",
                     "metadata": {"routecraft_telemetry": envelope, "task_class": _bounded_label(row.get("task_class"))}}
            events.append(event)
        start = int(cursor or 0) if str(cursor or "").isdigit() else 0
        return {"events": events[start:start + limit], "cursor": str(start + limit) if start + limit < len(events) else None}


class CompositeEventSource:
    def __init__(self, sources: list[Any]) -> None:
        self._sources = sources
    def sources(self) -> list[dict[str, Any]]:
        return [item for source in self._sources for item in source.sources()]
    def list_events(self, *, limit: int, cursor: str | None = None, source: str | None = None, include_special_events: bool = True) -> dict[str, Any]:
        all_rows = []
        for event_source in self._sources:
            result = event_source.list_events(limit=500, cursor=None, source=source, include_special_events=include_special_events)
            all_rows.extend(result.get("events", []) if isinstance(result, Mapping) else [])
        all_rows.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
        start = int(cursor or 0) if str(cursor or "").isdigit() else 0
        return {"events": all_rows[start:start + limit], "cursor": str(start + limit) if start + limit < len(all_rows) else None}
