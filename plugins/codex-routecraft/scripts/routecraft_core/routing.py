"""Pure route planning plus a host-bound, privacy-safe execution controller."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from routecraft_protocols import TelemetryValidationError, new_event, new_routecraft_telemetry

from .contracts import (
    CORE_API_VERSION, EventSink, ExecutionResult, HostAdapter, MemoryPort,
    NullEventSink, NullHostAdapter, NullMemory, RoutingDecision, RoutingMode, RoutingRequest,
    resolve_routecraft_version,
)
from .host_capabilities import HostCapabilityRegistry
from .verification import VerificationPlan, select_verification_plan, verification_outcome


def _configuration_error(request: RoutingRequest, reason: str) -> RoutingDecision:
    return RoutingDecision(mode=request.mode, authority="core", status="invalid_configuration", reason=reason,
                           provider=request.provider, host=request.host, model=request.model)


def _routecraft_hints(request: RoutingRequest) -> tuple[str, str]:
    risk = request.config.get("risk_level")
    if risk in {"high", "critical"}:
        return "terra_high", "high"
    if request.config.get("parallel") is True:
        return "terra_medium", "medium"
    return "luna_medium", "medium"


def _plan_route_base(request: RoutingRequest | str | Mapping[str, Any], registry: HostCapabilityRegistry | Mapping[str, Any] | None = None) -> RoutingDecision:
    """Create a deterministic plan; no provider/model is inferred or dispatched here."""
    try:
        request = RoutingRequest.from_value(request)
    except (TypeError, ValueError) as exc:
        # Do not surface caller configuration values (which may contain task text or paths).
        return RoutingDecision(status="invalid_configuration", reason="invalid_routing_request")
    # Legacy routing predates the capability registry and must retain its
    # authority even when a later registry is empty, unknown, or malformed.
    if request.mode is RoutingMode.LEGACY:
        return RoutingDecision(mode=request.mode, authority="legacy", status="ok", reason="legacy_authority_preserved",
                               provider=request.provider, host=request.host, model=request.model)
    try:
        registry = registry if isinstance(registry, HostCapabilityRegistry) else HostCapabilityRegistry.from_mapping(registry)
    except (TypeError, ValueError):
        return RoutingDecision(status="invalid_configuration", reason="invalid_routing_request")
    if request.model and not registry.model_known(request.provider, request.host, request.model):
        return _configuration_error(request, "requested_model_is_unknown")
    available = registry.capability("available", provider=request.provider, host=request.host, model=request.model)
    if available is False:
        scope = "model" if request.model else "host" if request.host else "provider"
        return _configuration_error(request, f"requested_{scope}_is_unavailable")
    if request.mode is RoutingMode.ADVISORY:
        lane, effort = _routecraft_hints(request)
        return RoutingDecision(mode=request.mode, lane=lane, reasoning_effort=effort, dispatch=False, authority="host",
                               reason="advisory_hints_only", provider=request.provider, host=request.host, model=request.model,
                               hints={"native_routing": registry.capability("native_routing", provider=request.provider, host=request.host, model=request.model)})
    if request.mode is RoutingMode.NATIVE:
        if request.provider is None or request.host is None:
            lane, effort = _routecraft_hints(request)
            return RoutingDecision(mode=RoutingMode.ADVISORY, lane=lane, reasoning_effort=effort, dispatch=False, authority="host",
                                   status="fallback", reason="native_routing_context_unknown_fallback_to_advisory",
                                   provider=request.provider, host=request.host, model=request.model)
        native = registry.capability("native_routing", provider=request.provider, host=request.host, model=request.model)
        if native is True:
            return RoutingDecision(mode=request.mode, dispatch=True, authority="host", reason="native_routing_declared",
                                   provider=request.provider, host=request.host, model=request.model)
        lane, effort = _routecraft_hints(request)
        return RoutingDecision(mode=RoutingMode.ADVISORY, lane=lane, reasoning_effort=effort, dispatch=False, authority="host",
                               status="fallback", reason="native_routing_unavailable_fallback_to_advisory",
                               provider=request.provider, host=request.host, model=request.model)
    lane, effort = _routecraft_hints(request)
    return RoutingDecision(mode=RoutingMode.ROUTECRAFT, lane=lane, reasoning_effort=effort, dispatch=True, authority="routecraft",
                           reason="routecraft_lane_hint", provider=request.provider, host=request.host, model=request.model)


def plan_route(request: RoutingRequest | str | Mapping[str, Any], registry: HostCapabilityRegistry | Mapping[str, Any] | None = None) -> RoutingDecision:
    """Create a route plus an independently selected Verification Budget."""
    decision = _plan_route_base(request, registry)
    try:
        normalized = RoutingRequest.from_value(request)
    except (TypeError, ValueError):
        return decision
    return replace(decision, verification=select_verification_plan(normalized).to_dict())


class RouteCraftCore:
    """Core coordinates plans and best-effort observability; its adapter alone dispatches."""
    def __init__(self, *, registry: HostCapabilityRegistry | Mapping[str, Any] | None = None,
                 memory: MemoryPort | None = None, events: EventSink | None = None,
                 host: HostAdapter | None = None, max_retries: int = 1) -> None:
        self.registry = registry if isinstance(registry, HostCapabilityRegistry) else HostCapabilityRegistry.from_mapping(registry)
        self.memory = memory or NullMemory()
        self.events = events or NullEventSink()
        self.host = host or NullHostAdapter()
        configured_retries = max_retries if isinstance(max_retries, int) and not isinstance(max_retries, bool) else 1
        self.max_retries = max(0, min(configured_retries, 3))

    def plan(self, task: RoutingRequest | str | Mapping[str, Any]) -> RoutingDecision:
        return plan_route(task, self.registry)

    @staticmethod
    def _decision_source(request: RoutingRequest, decision: RoutingDecision) -> str:
        if decision.status == "fallback":
            return "fallback"
        if decision.mode is RoutingMode.ROUTECRAFT and decision.authority == "routecraft":
            return "routecraft"
        if request.mode is RoutingMode.NATIVE or decision.authority == "host":
            return "codex"
        if request.model is not None or request.requested_reasoning is not None:
            return "user"
        return "unknown"

    @staticmethod
    def _safe_host_telemetry(response: Mapping[str, Any] | None) -> dict[str, Any]:
        """Copy only host facts allowed by the telemetry contract, never raw output."""
        if not isinstance(response, Mapping):
            return {}
        aliases = {
            "actual_model": ("actual_model",),
            "actual_reasoning": ("actual_reasoning", "actual_reasoning_effort"),
            "input_tokens": ("input_tokens",),
            "cached_input_tokens": ("cached_input_tokens",),
            "output_tokens": ("output_tokens",),
            "reasoning_tokens": ("reasoning_tokens",),
            "total_tokens": ("total_tokens",),
            "execution_time_ms": ("execution_time_ms", "duration_ms"),
            "retry_count": ("retry_count",),
            "model_calls": ("model_calls",),
            "tool_calls": ("tool_calls",),
            "file_reads": ("file_reads",),
            "benchmark": ("benchmark",),
        }
        copied: dict[str, Any] = {}
        for target, names in aliases.items():
            for name in names:
                if name in response:
                    try:
                        copied[target] = new_routecraft_telemetry(**{target: response[name]})[target]
                    except TelemetryValidationError:
                        pass
                    break
        return copied

    def _telemetry(self, request: RoutingRequest, decision: RoutingDecision, *, recalled: tuple[bool | None, list[str] | None], host_facts: Mapping[str, Any] | None = None, retry_count: int | None = None) -> dict[str, Any]:
        # Current Core routes to a lane, not a provider-specific exact model.
        # ``decision.model`` mirrors the request for compatibility and is not
        # evidence of a distinct selected model.
        selected_model = None
        actual_model = (host_facts or {}).get("actual_model")
        actual_reasoning = (host_facts or {}).get("actual_reasoning")
        if request.model is None or actual_model is None:
            route_changed = None
        elif request.model != actual_model:
            route_changed = True
        elif request.requested_reasoning is None or actual_reasoning is None:
            route_changed = None
        else:
            route_changed = request.requested_reasoning != actual_reasoning
        values: dict[str, Any] = {
            "requested_model": request.model,
            "requested_reasoning": request.requested_reasoning,
            "selected_model": selected_model,
            "selected_reasoning": decision.reasoning_effort,
            "actual_model": actual_model,
            "actual_reasoning": actual_reasoning,
            "route_decision_model": None,
            "route_decision_reasoning": decision.reasoning_effort,
            "decision_source": self._decision_source(request, decision),
            "decision_reason": decision.reason,
            "decision_confidence": None,
            "route_changed": route_changed,
            "memory_recall_used": recalled[0],
            "memory_case_ids": recalled[1],
            "rules_applied": None,
            "routecraft_version": resolve_routecraft_version(),
            "memory_version": None,
            "collector_version": None,
            "dashboard_version": None,
        }
        if retry_count is not None:
            values["retry_count"] = retry_count
        values.update(host_facts or {})
        # A host-provided retry count is evidence about host retries; otherwise
        # Core's bounded dispatch attempt count is the only observed retry fact.
        if values.get("retry_count") is None and retry_count is not None:
            values["retry_count"] = retry_count
        return new_routecraft_telemetry(**values)

    def _emit(self, event: str, request: RoutingRequest, decision: RoutingDecision, *, status: str, metadata: Mapping[str, Any], recalled: tuple[bool | None, list[str] | None], host_facts: Mapping[str, Any] | None = None, retry_count: int | None = None) -> bool:
        try:
            payload_metadata = dict(metadata)
            payload_metadata["routecraft_telemetry"] = self._telemetry(
                request, decision, recalled=recalled, host_facts=host_facts, retry_count=retry_count,
            )
            self.events.emit(new_event(event, "routecraft_core", task_id=request.task_id, project=request.project,
                                       provider=request.provider, model=request.model, status=status,
                                       event_classification=select_verification_plan(request).event_classification,
                                       metadata=payload_metadata))
            return True
        except Exception:
            return False

    def _recall(self, request: RoutingRequest) -> tuple[bool | None, list[str] | None]:
        try:
            recalled = self.memory.recall(request)
            if not isinstance(recalled, list):
                return False, []
            case_ids: list[str] = []
            for item in recalled:
                identifier = item.get("id") if isinstance(item, Mapping) else None
                if isinstance(identifier, str) and len(identifier) <= 128 and identifier.replace("_", "a").replace("-", "a").replace(".", "a").replace(":", "a").isalnum():
                    if identifier not in case_ids and len(case_ids) < 32:
                        case_ids.append(identifier)
            return bool(recalled), case_ids
        except Exception:
            return None, None

    def execute(self, task: RoutingRequest | str | Mapping[str, Any], executor: object | None = None) -> ExecutionResult:
        """Execute only through the supplied host adapter and return secret-free evidence."""
        try:
            request = RoutingRequest.from_value(task)
        except (TypeError, ValueError):
            decision = RoutingDecision(status="invalid_configuration", reason="invalid_routing_request")
            return ExecutionResult(status="invalid_configuration", decision=decision, evidence={"payload_redacted": True})
        decision = self.plan(request)
        verification_plan = select_verification_plan(request)
        if decision.status == "invalid_configuration":
            return ExecutionResult(status="invalid_configuration", succeeded=False, attempts=0, decision=decision,
                                   evidence={"memory_recalled_count": 0, "events_emitted": 0, "payload_redacted": True})
        recalled = self._recall(request)
        started_emitted = self._emit("execution.started", request, decision, status="started", metadata={
            "mode": decision.mode.value,
            "fallback": decision.status == "fallback",
            "memory_recalled_count": len(recalled[1]) if recalled[1] is not None else 0,
            "verification": {"plan": verification_plan.to_dict(), "outcome": {"status": "planned"}},
            "sequence": 1,
        }, recalled=recalled)
        if not decision.dispatch:
            result_status = "invalid_configuration" if decision.status == "invalid_configuration" else "not_dispatched"
            outcome = verification_outcome(None, verification_plan)
            completed_emitted = self._emit("execution.completed", request, decision, status=result_status, metadata={"attempts": 0, "verification": {"plan": verification_plan.to_dict(), "outcome": outcome}, "sequence": 2}, recalled=recalled, retry_count=0)
            evidence = {"memory_recalled_count": len(recalled[1]) if recalled[1] is not None else 0, "events_emitted": int(started_emitted) + int(completed_emitted), "payload_redacted": True}
            return ExecutionResult(status=result_status, succeeded=False, attempts=0, decision=decision, evidence=evidence)
        retries = request.config.get("max_retries", self.max_retries)
        retries = retries if isinstance(retries, int) and not isinstance(retries, bool) else self.max_retries
        attempts_limit = max(1, min(retries, self.max_retries) + 1)
        succeeded = False
        host_status = "host_adapter_failed"
        attempts = 0
        host_facts: dict[str, Any] = {}
        response: Mapping[str, Any] | None = None
        for attempts in range(1, attempts_limit + 1):
            try:
                response = self.host.dispatch(request, decision, executor)
                if isinstance(response, Mapping):
                    host_facts = self._safe_host_telemetry(response)
                    succeeded = response.get("succeeded") is True
                    proposed = response.get("status", "succeeded" if succeeded else "host_adapter_failed")
                    host_status = proposed if proposed in {"succeeded", "host_adapter_unavailable", "host_adapter_failed", "rejected"} else "host_adapter_failed"
                else:
                    succeeded = response is True
                    host_status = "succeeded" if succeeded else "host_adapter_failed"
            except Exception:
                succeeded, host_status = False, "host_adapter_failed"
            if succeeded:
                break
        result_status = "succeeded" if succeeded else host_status
        outcome = verification_outcome(response, verification_plan)
        summary = {"task_id": request.task_id, "status": result_status, "attempts": attempts, "mode": decision.mode.value,
                   "verification": {"plan": verification_plan.to_dict(), "outcome": outcome}}
        for method in ("notify_outcome", "notify_experience"):
            try:
                getattr(self.memory, method)(summary)
            except Exception:
                pass
        completed_emitted = self._emit("execution.completed", request, decision, status=result_status, metadata={"attempts": attempts, "succeeded": succeeded, "verification": {"plan": verification_plan.to_dict(), "outcome": outcome}, "sequence": 2}, recalled=recalled, host_facts=host_facts, retry_count=attempts - 1)
        evidence = {"memory_recalled_count": len(recalled[1]) if recalled[1] is not None else 0, "events_emitted": int(started_emitted) + int(completed_emitted), "payload_redacted": True,
                    "host_status": host_status, "retry_bounded": attempts_limit <= 4, "verification": {"plan": verification_plan.to_dict(), "outcome": outcome}}
        return ExecutionResult(status=result_status, succeeded=succeeded, attempts=attempts, decision=decision, evidence=evidence)
