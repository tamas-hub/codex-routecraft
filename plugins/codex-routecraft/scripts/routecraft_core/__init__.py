"""Standalone RouteCraft routing/execution-control seam (API v1)."""

from .contracts import (
    CORE_API_VERSION, EventSink, ExecutionResult, HostAdapter, MemoryPort,
    NullEventSink, NullHostAdapter, NullMemory, RoutingDecision, RoutingMode,
    RoutingRequest, resolve_routecraft_version,
)
from .host_capabilities import HostCapabilityRegistry
from .routing import RouteCraftCore, plan_route
from .verification import (
    VerificationBudget, VerificationPlan, VerificationSetting, VerificationStatus,
    select_verification_plan, verification_outcome,
)

__all__ = [
    "CORE_API_VERSION", "EventSink", "ExecutionResult", "HostAdapter", "MemoryPort",
    "NullEventSink", "NullHostAdapter", "NullMemory", "RoutingDecision", "RoutingMode",
    "RoutingRequest", "resolve_routecraft_version", "HostCapabilityRegistry", "RouteCraftCore", "plan_route",
    "VerificationBudget", "VerificationPlan", "VerificationSetting", "VerificationStatus",
    "select_verification_plan", "verification_outcome",
]
