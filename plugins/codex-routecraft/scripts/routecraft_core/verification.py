"""Deterministic Verification Budget selection and privacy-safe outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import re
from typing import Any, Mapping

from .contracts import RoutingRequest


_SAFE_CHECK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,95}$")
_TASK_CLASSES = {"general", "debugging", "implementation", "ci", "refactor", "docs", "release", "integration", "test"}
_CHANGE_SCOPES = {"none", "single_file", "module", "cross_module", "repository", "release"}
_RISK_LEVELS = {"low", "medium", "high", "critical"}
_EVENT_CLASSIFICATIONS = {"normal", "token_burn_event", "reset_expectation", "benchmark_event", "migration_event", "stress_test", "manual_override"}


class VerificationBudget(str, Enum):
    NONE = "none"
    MIN = "min"
    STRICT = "strict"
    RELEASE = "release"


class VerificationSetting(str, Enum):
    AUTO_MIN = "auto_min"
    NONE = "none"
    MIN = "min"
    STRICT = "strict"
    RELEASE = "release"


class VerificationStatus(str, Enum):
    PLANNED = "planned"
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    NOT_REQUIRED = "not_required"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VerificationPlan:
    setting: VerificationSetting
    budget: VerificationBudget
    reason: str
    task_class: str
    change_scope: str
    risk_level: str
    targeted_checks: tuple[str, ...]
    skipped_checks: tuple[str, ...]
    max_targeted_checks: int
    full_suite_allowed: bool
    stop_condition: str
    event_classification: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["setting"] = self.setting.value
        value["budget"] = self.budget.value
        value["targeted_checks"] = list(self.targeted_checks)
        value["skipped_checks"] = list(self.skipped_checks)
        return value


def _enum_token(value: Any, allowed: set[str], fallback: str) -> str:
    candidate = str(value or "").strip().lower().replace("-", "_")
    return candidate if candidate in allowed else fallback


def _safe_checks(value: Any, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and _SAFE_CHECK.fullmatch(item) and item not in result:
            result.append(item)
        if len(result) >= limit:
            break
    return tuple(result)


def select_verification_plan(request: RoutingRequest) -> VerificationPlan:
    """Select a bounded budget. Auto never escalates to RELEASE."""
    config = request.config
    raw_setting = config.get("test_budget", config.get("verification_budget", "auto_min"))
    normalized = str(raw_setting or "auto_min").strip().lower().replace("-", "_")
    if normalized in {"auto", "default"}:
        normalized = "auto_min"
    try:
        setting = VerificationSetting(normalized)
    except ValueError:
        setting = VerificationSetting.AUTO_MIN

    task_class = _enum_token(config.get("task_class"), _TASK_CLASSES, "general")
    change_scope = _enum_token(config.get("change_scope"), _CHANGE_SCOPES, "module")
    risk_level = _enum_token(config.get("risk_level"), _RISK_LEVELS, "medium")
    event_classification = _enum_token(config.get("event_classification"), _EVENT_CLASSIFICATIONS, "normal")
    verification_required = config.get("verification_required") is not False

    if setting is not VerificationSetting.AUTO_MIN:
        budget = VerificationBudget(setting.value)
        reason = "explicit_test_budget_override"
    elif not verification_required or change_scope == "none" or task_class == "docs":
        budget = VerificationBudget.NONE
        reason = "verification_not_required"
    elif risk_level in {"high", "critical"} or change_scope in {"cross_module", "repository", "release"}:
        budget = VerificationBudget.STRICT
        reason = "auto_risk_or_scope_requires_strict"
    else:
        budget = VerificationBudget.MIN
        reason = "auto_min_targeted_verification"

    limits = {
        VerificationBudget.NONE: 0,
        VerificationBudget.MIN: 3,
        VerificationBudget.STRICT: 8,
        VerificationBudget.RELEASE: 12,
    }
    limit = limits[budget]
    targeted = _safe_checks(config.get("verification_tests"), limit)
    standard = ("repository_full_suite", "full_e2e", "all_lint", "coverage", "multi_platform_build")
    if budget is VerificationBudget.NONE:
        targeted = ()
    skipped = () if budget is VerificationBudget.RELEASE else standard
    full_suite_allowed = budget in {VerificationBudget.STRICT, VerificationBudget.RELEASE} and bool(config.get("full_suite_reason"))
    stop_condition = {
        VerificationBudget.NONE: "no_check_required",
        VerificationBudget.MIN: "targeted_checks_pass",
        VerificationBudget.STRICT: "risk_checks_pass",
        VerificationBudget.RELEASE: "release_gate_pass",
    }[budget]
    return VerificationPlan(
        setting=setting, budget=budget, reason=reason, task_class=task_class,
        change_scope=change_scope, risk_level=risk_level, targeted_checks=targeted,
        skipped_checks=skipped, max_targeted_checks=limit,
        full_suite_allowed=full_suite_allowed, stop_condition=stop_condition,
        event_classification=event_classification,
    )


_OUTCOME_COUNTS = (
    "tests_run", "targeted_tests", "full_suites", "builds", "lint_runs", "typechecks",
    "e2e_runs", "avoided_full_suites", "avoided_e2e", "avoided_builds", "avoided_lint",
    "avoided_typechecks", "verification_duration_ms",
)


def verification_outcome(response: Mapping[str, Any] | None, plan: VerificationPlan) -> dict[str, Any]:
    """Copy only categorical/numeric observed verification facts from a host."""
    if plan.budget is VerificationBudget.NONE:
        return {
            "status": VerificationStatus.NOT_REQUIRED.value,
            "reason": "verification_not_required",
            **{name: 0 for name in _OUTCOME_COUNTS},
        }
    raw = response.get("verification") if isinstance(response, Mapping) else None
    if not isinstance(raw, Mapping):
        return {"status": VerificationStatus.UNKNOWN.value, "reason": "host_did_not_report", **{name: None for name in _OUTCOME_COUNTS}}
    status = str(raw.get("status") or "unknown").lower()
    if status not in {item.value for item in VerificationStatus}:
        status = VerificationStatus.UNKNOWN.value
    reason = str(raw.get("reason") or "host_reported").lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
        reason = "host_reported"
    outcome: dict[str, Any] = {"status": status, "reason": reason}
    for name in _OUTCOME_COUNTS:
        value = raw.get(name)
        outcome[name] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    return outcome
