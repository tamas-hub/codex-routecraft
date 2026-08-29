"""Versioned, privacy-bounded interchange contracts for RouteCraft."""

from .events import (
    EVENT_SCHEMA_VERSION,
    EventValidationError,
    new_event,
    validate_event,
)
from .telemetry import (
    ROUTECRAFT_TELEMETRY_KEYS,
    ROUTECRAFT_TELEMETRY_SCHEMA_VERSION,
    TelemetryValidationError,
    new_routecraft_telemetry,
    validate_routecraft_telemetry,
)

__all__ = [
    "EVENT_SCHEMA_VERSION", "EventValidationError", "new_event", "validate_event",
    "ROUTECRAFT_TELEMETRY_KEYS", "ROUTECRAFT_TELEMETRY_SCHEMA_VERSION", "TelemetryValidationError",
    "new_routecraft_telemetry", "validate_routecraft_telemetry",
]
