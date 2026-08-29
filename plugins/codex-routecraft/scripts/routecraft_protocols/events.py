"""The small, dependency-free Praxis event contract (schema v1).

Events deliberately carry only bounded operational facts.  Task text, prompt
contents, paths, credentials, and provider output must remain with the owning
host and are never valid protocol metadata.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import secrets
from typing import Any, Mapping

from .privacy import contains_absolute_path, contains_secret_like


EVENT_SCHEMA_VERSION = "1"
CANONICAL_EVENT_KEYS = (
    "schema_version", "event", "event_id", "timestamp", "source", "provider",
    "agent", "model", "project", "task_id", "status", "event_classification",
    "metadata",
)
EVENT_FAMILIES = ("task", "routing", "memory", "execution", "evaluation", "usage", "system")
EVENT_CLASSIFICATIONS = {
    "normal", "token_burn_event", "reset_expectation", "benchmark_event",
    "migration_event", "stress_test", "manual_override",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EVENT = re.compile(r"^(?:task|routing|memory|execution|evaluation|usage|system)\.[a-z][a-z0-9_.-]{0,95}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_SECRET_KEY = re.compile(r"(?:secret|password|passwd|token|api[_-]?key|authorization|credential|cookie|private[_-]?key|bearer)", re.I)
_BODY_KEY = re.compile(r"(?:prompt|conversation|transcript|source(?:_code)?|file(?:_body|_content)?|raw[_-]?(?:output|response|log)|stdout|stderr)", re.I)
_SAFE_TOKEN_MEASUREMENTS = {
    "token_count", "total_tokens", "input_tokens", "cached_tokens", "cached_input_tokens",
    "output_tokens", "reasoning_tokens",
}
_MAX_METADATA_BYTES = 8_192
_MAX_DEPTH = 6
_MAX_ITEMS = 64
_MAX_STRING = 512


class EventValidationError(ValueError):
    """Raised when a value cannot be represented by the public event schema."""


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _bounded_json(value: Any, *, depth: int = 0, key: str = "") -> None:
    if depth > _MAX_DEPTH:
        raise EventValidationError("metadata exceeds nesting limit")
    normalized_key = key.casefold()
    if normalized_key in _SAFE_TOKEN_MEASUREMENTS:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise EventValidationError("token measurement must be a non-negative number")
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise EventValidationError("metadata number is invalid")
        return
    if _SECRET_KEY.search(key) or _BODY_KEY.search(key):
        raise EventValidationError("metadata key is not privacy-safe")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise EventValidationError("metadata number is invalid")
        return
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            raise EventValidationError("metadata string exceeds limit")
        if contains_secret_like(value) or contains_absolute_path(value):
            raise EventValidationError("metadata value is not privacy-safe")
        return
    if isinstance(value, list):
        if len(value) > _MAX_ITEMS:
            raise EventValidationError("metadata list exceeds limit")
        for item in value:
            _bounded_json(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_ITEMS:
            raise EventValidationError("metadata object exceeds limit")
        for child_key, item in value.items():
            if not isinstance(child_key, str) or not child_key or len(child_key) > 96:
                raise EventValidationError("metadata key is invalid")
            _bounded_json(item, depth=depth + 1, key=child_key)
        return
    raise EventValidationError("metadata must contain JSON values only")


def _validate_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EventValidationError("metadata must be an object")
    copied = dict(value)
    if "routecraft_telemetry" in copied:
        # Import lazily to keep the generic Common Event v1 contract independent
        # of callers that do not use this optional metadata namespace.
        from .telemetry import TelemetryValidationError, validate_routecraft_telemetry
        try:
            copied["routecraft_telemetry"] = validate_routecraft_telemetry(copied["routecraft_telemetry"])
        except TelemetryValidationError as exc:
            raise EventValidationError(str(exc)) from exc
    # The optional envelope has already applied stricter field-specific privacy
    # checks.  Its nullable counters and names such as ``decision_source`` are
    # intentionally not interpreted as generic metadata keys.
    for key, item in copied.items():
        if key != "routecraft_telemetry":
            _bounded_json(item, key=key)
    try:
        encoded = json.dumps(copied, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EventValidationError("metadata must be JSON serializable") from exc
    if len(encoded) > _MAX_METADATA_BYTES:
        raise EventValidationError("metadata exceeds byte limit")
    return copied


def _nullable_id(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ID.fullmatch(value) or contains_secret_like(value) or contains_absolute_path(value):
        raise EventValidationError(f"{name} must be a bounded opaque identifier or null")
    return value


def validate_event(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize an event without doing I/O or guessing facts."""
    if not isinstance(value, Mapping):
        raise EventValidationError("event must be an object")
    keys = set(value)
    required = set(CANONICAL_EVENT_KEYS)
    if keys != required:
        raise EventValidationError("event keys invalid")
    if value["schema_version"] != EVENT_SCHEMA_VERSION:
        raise EventValidationError("unsupported event schema version")
    event = value["event"]
    if not isinstance(event, str) or not _EVENT.fullmatch(event) or contains_secret_like(event) or contains_absolute_path(event):
        raise EventValidationError("event family or name is invalid")
    for name in ("event_id", "source"):
        if not isinstance(value[name], str) or not _ID.fullmatch(value[name]) or contains_secret_like(value[name]) or contains_absolute_path(value[name]):
            raise EventValidationError(f"{name} must be a bounded opaque identifier")
    if not _valid_timestamp(value["timestamp"]):
        raise EventValidationError("timestamp must be UTC ISO-8601")
    for name in ("provider", "agent", "model", "project", "task_id", "status"):
        _nullable_id(value[name], name)
    if value["event_classification"] not in EVENT_CLASSIFICATIONS:
        raise EventValidationError("event classification is invalid")
    return {key: _validate_metadata(value[key]) if key == "metadata" else value[key] for key in CANONICAL_EVENT_KEYS}


def new_event(event: str, source: str, **fields: Any) -> dict[str, Any]:
    """Build a canonical event, retaining unknown optional facts as ``null``."""
    unknown = set(fields) - (set(CANONICAL_EVENT_KEYS) - {"schema_version", "event", "source"})
    if unknown:
        raise EventValidationError("unknown event fields")
    value: dict[str, Any] = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event": event,
        "event_id": fields.pop("event_id", "evt_" + secrets.token_hex(12)),
        "timestamp": fields.pop("timestamp", _utc_timestamp()),
        "source": source,
        "provider": fields.pop("provider", None),
        "agent": fields.pop("agent", None),
        "model": fields.pop("model", None),
        "project": fields.pop("project", None),
        "task_id": fields.pop("task_id", None),
        "status": fields.pop("status", None),
        "event_classification": fields.pop("event_classification", "normal"),
        "metadata": fields.pop("metadata", {}),
    }
    return validate_event(value)
