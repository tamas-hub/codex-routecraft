"""Public contracts for RouteCraft Core v1; provider dispatch stays host-owned."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, runtime_checkable


CORE_API_VERSION = "1"
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def resolve_routecraft_version() -> str | None:
    """Read the Core version from the plugin manifest; absence remains unknown."""
    manifest = Path(__file__).resolve().parents[2] / ".codex-plugin" / "plugin.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, str) and value.strip() else None


class RoutingMode(str, Enum):
    NATIVE = "native"
    ADVISORY = "advisory"
    ROUTECRAFT = "routecraft"
    LEGACY = "legacy"


def _safe_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


@dataclass(frozen=True)
class RoutingRequest:
    api_version: str = CORE_API_VERSION
    task_id: str | None = None
    task: str = ""
    mode: RoutingMode = RoutingMode.LEGACY
    provider: str | None = None
    host: str | None = None
    model: str | None = None
    requested_reasoning: str | None = None
    project: str | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.api_version != CORE_API_VERSION:
            raise ValueError("unsupported Core API version")
        if not isinstance(self.task, str) or not self.task.strip() or len(self.task) > 8_000:
            raise ValueError("task must be a non-empty bounded string")
        if not isinstance(self.mode, RoutingMode):
            object.__setattr__(self, "mode", RoutingMode(str(self.mode)))
        for name in ("task_id", "provider", "host", "model", "requested_reasoning", "project"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value)):
                raise ValueError(f"{name} must be a bounded opaque identifier or null")
        object.__setattr__(self, "config", _safe_mapping(self.config))
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    @classmethod
    def from_value(cls, value: "RoutingRequest | str | Mapping[str, Any]") -> "RoutingRequest":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(task=value)
        if not isinstance(value, Mapping):
            raise ValueError("routing request must be a string or object")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["mode"] = self.mode.value
        return value


@dataclass(frozen=True)
class RoutingDecision:
    api_version: str = CORE_API_VERSION
    mode: RoutingMode = RoutingMode.LEGACY
    lane: str | None = None
    reasoning_effort: str | None = None
    dispatch: bool = False
    authority: str = "legacy"
    status: str = "ok"
    reason: str = "legacy_authority_preserved"
    provider: str | None = None
    host: str | None = None
    model: str | None = None
    hints: Mapping[str, Any] = field(default_factory=dict)
    verification: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["mode"] = self.mode.value
        return value


@dataclass(frozen=True)
class ExecutionResult:
    api_version: str = CORE_API_VERSION
    status: str = "not_dispatched"
    succeeded: bool = False
    attempts: int = 0
    decision: RoutingDecision | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.decision is not None:
            value["decision"] = self.decision.to_dict()
        return value


@runtime_checkable
class MemoryPort(Protocol):
    def recall(self, request: RoutingRequest) -> list[Mapping[str, Any]]: ...
    def notify_outcome(self, outcome: Mapping[str, Any]) -> None: ...
    def notify_experience(self, experience: Mapping[str, Any]) -> None: ...


@runtime_checkable
class EventSink(Protocol):
    def emit(self, event: Mapping[str, Any]) -> None: ...


@runtime_checkable
class HostAdapter(Protocol):
    """The only Core boundary permitted to dispatch an executor."""
    def dispatch(
        self, request: RoutingRequest, decision: RoutingDecision, executor: object | None = None
    ) -> Mapping[str, Any] | bool | None: ...


class NullMemory:
    def recall(self, request: RoutingRequest) -> list[Mapping[str, Any]]:
        return []

    def notify_outcome(self, outcome: Mapping[str, Any]) -> None:
        return None

    def notify_experience(self, experience: Mapping[str, Any]) -> None:
        return None


class NullEventSink:
    def emit(self, event: Mapping[str, Any]) -> None:
        return None


class NullHostAdapter:
    def dispatch(
        self, request: RoutingRequest, decision: RoutingDecision, executor: object | None = None
    ) -> Mapping[str, Any]:
        return {"succeeded": False, "status": "host_adapter_unavailable"}
