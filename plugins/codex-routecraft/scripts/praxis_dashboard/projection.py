"""Privacy-bounded, evidence-first projections for Praxis Dashboard."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

DASHBOARD_API_VERSION = "1"
_REASONING = ("low", "medium", "high", "xhigh", "max", "ultra")
_ROLE = re.compile(r"^routecraft_(?:sol|terra|luna)(?:_[a-z0-9]+)*$", re.I)
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_COMPONENTS = ("routecraft-core", "praxis-memory", "praxis-dashboard", "collector", "telemetry-schema")


def _text(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _inc(counter: Counter[str], value: Any) -> None:
    text = _text(value)
    if text:
        counter[text] += 1


def _event_order(item: Mapping[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(item.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def _event_sequence(item: Mapping[str, Any]) -> int | None:
    metadata = item.get("metadata")
    value = metadata.get("sequence") if isinstance(metadata, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _safe_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return display-safe protocol fields; metadata can contain private text."""
    return {key: event.get(key) for key in (
        "schema_version", "event", "event_id", "timestamp", "source", "provider",
        "agent", "model", "project", "task_id", "status", "event_classification",
    ) if event.get(key) is not None}


def _model_family(value: Any) -> str | None:
    """Classify only bounded model labels; every other label remains unknown."""
    label = _text(value)
    if not _LABEL.fullmatch(label):
        return None
    for family in ("sol", "terra", "luna"):
        if label == family or re.search(rf"(?:^|[-_.]){family}(?:$|[-_.])", label):
            return family
    return "other"


def _reasoning(value: Any) -> str | None:
    label = _text(value)
    return label if label in _REASONING else None


def _telemetry(item: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = item.get("metadata")
    if not isinstance(metadata, Mapping):
        return {}
    nested = metadata.get("routecraft_telemetry")
    # The envelope is the preferred v1 protocol. Direct metadata is retained for
    # old Event v1 producers that predate the envelope.
    return nested if isinstance(nested, Mapping) else metadata


def _attribution(data: Mapping[str, Any], item: Mapping[str, Any]) -> str:
    if "decision_source" in data:
        source = _text(data.get("decision_source"))
        return source if source in {"routecraft", "user", "codex", "fallback", "unknown"} else "unknown"
    role = data.get("role", data.get("route_role", item.get("agent")))
    return "routecraft" if isinstance(role, str) and _ROLE.fullmatch(role) else "unknown"


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _safe_reason(value: Any) -> str | None:
    # Decision reasons are categorical labels, never free-form explanations.
    return value.strip() if isinstance(value, str) and _LABEL.fullmatch(value.strip()) else None


def _observed_actual(value: Any) -> str | None:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        return None
    return None if value.strip().lower() in {"unknown", "unknown-model", "unobserved"} else value


def _run_from_event(item: Mapping[str, Any]) -> dict[str, Any] | None:
    data = _telemetry(item)
    if not any(field in data for field in ("requested_model", "actual_model", "human_model", "selected_model")):
        return None
    requested_model = data.get("requested_model", data.get("human_model"))
    requested_reasoning = data.get("requested_reasoning", data.get("human_effort"))
    # selected route is a decision, not evidence that the host actually ran it.
    actual_model = _observed_actual(data.get("actual_model"))
    actual_reasoning = _observed_actual(data.get("actual_reasoning", data.get("actual_effort")))
    requested_family, actual_family = _model_family(requested_model), _model_family(actual_model)
    requested_level, actual_level = _reasoning(requested_reasoning), _reasoning(actual_reasoning)
    data_run_id = data.get("run_id")
    run_id = data_run_id if isinstance(data_run_id, str) and _LABEL.fullmatch(data_run_id) else item.get("event_id")
    if not isinstance(run_id, str) or not _LABEL.fullmatch(run_id):
        return None
    metadata = item.get("metadata")
    task_class = data.get("task_class", metadata.get("task_class") if isinstance(metadata, Mapping) else None)
    if not isinstance(task_class, str) or not _LABEL.fullmatch(task_class):
        task_class = None
    return {
        "run_id": run_id, "event_id": item.get("event_id") if isinstance(item.get("event_id"), str) else None,
        "timestamp": item.get("timestamp") if isinstance(item.get("timestamp"), str) else None, "task_class": task_class,
        "requested_model": requested_model if isinstance(requested_model, str) and _LABEL.fullmatch(requested_model) else None,
        "requested_reasoning": requested_level, "requested_family": requested_family,
        "actual_model": actual_model,
        "actual_reasoning": actual_level, "actual_family": actual_family,
        "decision_source": _attribution(data, item), "decision_reason": _safe_reason(data.get("decision_reason")),
        "confidence": data.get("decision_confidence") if _number(data.get("decision_confidence")) is not None and 0 <= data["decision_confidence"] <= 1 else None,
        "tokens": _number(data.get("total_tokens")), "duration_ms": _number(data.get("execution_time_ms", data.get("duration_ms"))),
        "input_tokens": _number(data.get("input_tokens")), "cached_input_tokens": _number(data.get("cached_input_tokens")),
        "output_tokens": _number(data.get("output_tokens")), "reasoning_tokens": _number(data.get("reasoning_tokens")),
        "memory_used": _bool(data.get("memory_recall_used")),
        "memory_case_ids": data.get("memory_case_ids") if isinstance(data.get("memory_case_ids"), list) else [],
        "rules_applied": data.get("rules_applied") if isinstance(data.get("rules_applied"), list) else [],
        "retry_count": _number(data.get("retry_count")), "tool_calls": _number(data.get("tool_calls")),
        "file_reads": _number(data.get("file_reads")), "model_calls": _number(data.get("model_calls")),
        "benchmark": data.get("benchmark") if isinstance(data.get("benchmark"), Mapping) else None,
        "_source": item.get("source") if isinstance(item.get("source"), str) else "unknown",
        "_order": _event_order(item), "_sequence": _event_sequence(item),
    }


def run_records(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Merge duplicate lifecycle events by source/run ID without exposing either source."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in events:
        if not isinstance(item, Mapping):
            continue
        record = _run_from_event(item)
        if record is None:
            continue
        key = (str(record["_source"]), str(record["run_id"]))
        previous = merged.get(key)
        if previous is None:
            merged[key] = record
            continue
        newer = (record["_order"], record["_sequence"] if record["_sequence"] is not None else -1) >= (previous["_order"], previous["_sequence"] if previous["_sequence"] is not None else -1)
        latest, older = (record, previous) if newer else (previous, record)
        merged[key] = {field: latest.get(field) if latest.get(field) is not None else older.get(field) for field in latest}
    return sorted(merged.values(), key=lambda row: (row["_order"], row["run_id"]), reverse=True)


def safe_run(record: Mapping[str, Any]) -> dict[str, Any]:
    """Projection deliberately excludes prompt, metadata, session and path fields."""
    return {key: record.get(key) for key in (
        "run_id", "event_id", "timestamp", "task_class", "requested_model", "requested_reasoning",
        "actual_model", "actual_reasoning", "decision_source", "decision_reason", "confidence", "tokens", "duration_ms", "memory_used",
    )}


def _component_versions() -> dict[str, dict[str, Any]]:
    root = Path(__file__).resolve().parents[2] / "components"
    plugin_root = root.parent
    output: dict[str, dict[str, Any]] = {}
    for component_id in _COMPONENTS:
        manifest = root / component_id / "manifest.json"
        unknown = {"version": None, "build": None, "commit": None, "date": None, "status": "unknown"}
        try:
            if not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_size > 65536:
                output[component_id] = unknown
                continue
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping) or payload.get("schema_version") != "1" or payload.get("component") != component_id:
                output[component_id] = unknown
                continue
            version = payload.get("version")
            source = payload.get("version_source")
            if version is None and isinstance(source, str) and source.endswith("#/version"):
                relative = source[:-len("#/version")]
                candidate = (manifest.parent / relative).resolve()
                if candidate.is_relative_to(plugin_root.resolve()) and candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size <= 65536:
                    referenced = json.loads(candidate.read_text(encoding="utf-8"))
                    version = referenced.get("version") if isinstance(referenced, Mapping) else None
            valid_version = version if isinstance(version, str) and _VERSION.fullmatch(version) else None
            output[component_id] = {
                "version": valid_version,
                "build": payload.get("build") if isinstance(payload.get("build"), str) and _LABEL.fullmatch(payload["build"]) else None,
                "commit": payload.get("commit") if isinstance(payload.get("commit"), str) and _LABEL.fullmatch(payload["commit"]) else None,
                "date": payload.get("date") if isinstance(payload.get("date"), str) and len(payload["date"]) <= 64 else None,
                "status": "observed" if valid_version is not None else "unknown",
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            output[component_id] = unknown
    return output


def _impact(records: list[dict[str, Any]]) -> dict[str, Any]:
    attributable = [row for row in records if row["decision_source"] == "routecraft"]
    attribution_mix = Counter(row["decision_source"] if row["decision_source"] in {"routecraft", "user", "codex", "fallback", "unknown"} else "unknown" for row in records)
    for source in ("routecraft", "user", "codex", "fallback", "unknown"):
        attribution_mix.setdefault(source, 0)
    unknown_attribution = attribution_mix["unknown"]
    excluded_non_routecraft = attribution_mix["user"] + attribution_mix["codex"] + attribution_mix["fallback"]
    requested_models, actual_models, requested_reasoning, actual_reasoning = Counter(), Counter(), Counter(), Counter()
    model_comparable = [row for row in attributable if row["requested_family"] is not None and row["actual_family"] is not None and row["requested_model"] and row["actual_model"]]
    reasoning_comparable = [row for row in attributable if row["requested_reasoning"] is not None and row["actual_reasoning"] is not None]
    for row in attributable:
        if row["requested_family"] is not None:
            requested_models[row["requested_family"]] += 1
        if row["actual_family"] is not None:
            actual_models[row["actual_family"]] += 1
        if row["requested_reasoning"] is not None:
            requested_reasoning[row["requested_reasoning"]] += 1
        if row["actual_reasoning"] is not None:
            actual_reasoning[row["actual_reasoning"]] += 1
    transitions: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    changed_rows: list[dict[str, Any]] = []
    unchanged = family_changed = reasoning_only = 0
    route_comparable = []
    for row in model_comparable:
        models_differ = row["requested_model"] != row["actual_model"]
        comparable = models_differ or (row["requested_reasoning"] is not None and row["actual_reasoning"] is not None)
        if not comparable:
            continue
        route_comparable.append(row)
        is_changed = models_differ or row["requested_reasoning"] != row["actual_reasoning"]
        if is_changed:
            changed_rows.append(row)
        else:
            unchanged += 1
        if row["requested_family"] != row["actual_family"]:
            family_changed += 1
        elif not models_differ and row["requested_reasoning"] != row["actual_reasoning"]:
            reasoning_only += 1
        transitions.setdefault((row["requested_family"], row["requested_reasoning"] or "unknown", row["actual_family"], row["actual_reasoning"] or "unknown"), []).append(row)
    total = len(route_comparable)
    matrix = []
    for key, transition_rows in sorted(transitions.items(), key=lambda item: (-len(item[1]), item[0])):
        token_values = [row["tokens"] for row in transition_rows if row["tokens"] is not None]
        duration_values = [row["duration_ms"] for row in transition_rows if row["duration_ms"] is not None]
        matrix.append({"requested_model": key[0], "requested_reasoning": key[1], "actual_model": key[2], "actual_reasoning": key[3],
                       "runs": len(transition_rows), "percent": len(transition_rows) / total * 100 if total else None,
                       "tokens": sum(token_values) if token_values else None, "duration_ms": sum(duration_values) if duration_values else None,
                       "summary": f"{key[0].title()} / {key[1]} → {key[2].title()} / {key[3]}"})
    reasons = Counter(row["decision_reason"] for row in changed_rows if row["decision_reason"])
    sol_ultra_requests = [
        row for row in attributable
        if row["requested_family"] == "sol" and row["requested_reasoning"] == "ultra"
    ]
    # A non-Sol actual family proves an offload even when the host did not
    # expose its reasoning setting.  A Sol actual still needs reasoning
    # evidence to distinguish retained Ultra from a reasoning reduction.
    sol_ultra = [
        row for row in sol_ultra_requests
        if row["actual_family"] is not None
        and (row["actual_family"] != "sol" or row["actual_reasoning"] is not None)
    ]
    classifications = Counter({key: 0 for key in ("retained", "reasoning_reduced", "terra_offload", "luna_offload", "other")})
    for row in sol_ultra:
        if row["actual_family"] == "sol" and row["actual_reasoning"] == "ultra":
            bucket = "retained"
        elif row["actual_family"] == "sol" and _REASONING.index(row["actual_reasoning"]) < _REASONING.index("ultra"):
            bucket = "reasoning_reduced"
        elif row["actual_family"] == "terra":
            bucket = "terra_offload"
        elif row["actual_family"] == "luna":
            bucket = "luna_offload"
        else:
            bucket = "other"
        classifications[bucket] += 1
    ultra_offloaded = sum(row["actual_family"] != "sol" for row in sol_ultra)
    offloaded = sum(1 for row in model_comparable if row["requested_family"] == "sol" and row["actual_family"] != "sol")
    sol_requested = sum(1 for row in model_comparable if row["requested_family"] == "sol")
    optimised = len(sol_ultra) - classifications["retained"]
    return {
        "observed_runs": len(records), "attributable_runs": len(attributable), "eligible_runs": total,
        "unknown_attribution": unknown_attribution, "excluded_non_routecraft": excluded_non_routecraft, "attribution_mix": dict(attribution_mix), "excluded_missing_fields": len(attributable) - total,
        "requested_model_mix": {"values": dict(requested_models), "denominator": sum(requested_models.values()), "excluded": len(attributable) - sum(requested_models.values())},
        "actual_model_mix": {"values": dict(actual_models), "denominator": sum(actual_models.values()), "excluded": len(attributable) - sum(actual_models.values())},
        "requested_reasoning_mix": {"values": dict(requested_reasoning), "denominator": sum(requested_reasoning.values()), "excluded": len(attributable) - sum(requested_reasoning.values())},
        "actual_reasoning_mix": {"values": dict(actual_reasoning), "denominator": sum(actual_reasoning.values()), "excluded": len(attributable) - sum(actual_reasoning.values())},
        "route_changes": {"changed": len(changed_rows), "unchanged": unchanged, "model_family_changed": family_changed, "reasoning_only_changed": reasoning_only, "denominator": total, "excluded": len(attributable) - total},
        "sol_offload": {"requested_sol_runs": sol_requested, "offloaded": offloaded, "avoided_count": offloaded, "rate": offloaded / sol_requested if sol_requested else None, "excluded": sum(1 for row in attributable if row["requested_family"] == "sol") - sol_requested},
        "sol_ultra": {
            "requested": len(sol_ultra_requests),
            "denominator": len(sol_ultra),
            "excluded": len(sol_ultra_requests) - len(sol_ultra),
            "classifications": dict(classifications),
            "optimization_rate": optimised / len(sol_ultra) if sol_ultra else None,
            "offloaded": ultra_offloaded,
        },
        "transition_matrix": matrix, "why_routes_changed": [{"reason": reason, "runs": count, "percent": count / len(changed_rows) * 100 if changed_rows else None} for reason, count in reasons.most_common()],
        "estimated_savings": {"level": 1, "observed_avoided_count": offloaded, "label": "OBSERVED", "level_2": None, "level_3": None},
        "counterfactual_metrics": {"retry_reduction": None, "repeated_investigation_avoided": None, "context_reduction": None, "status": "unavailable"},
        "routing_efficiency": {"score": None, "status": "withheld", "coverage": total / len(attributable) if attributable else None,
                               "help": "品質・反実仮想の観測がないため、ルーティング効率スコアは表示しません。"},
    }


def _memory_effect(records: list[dict[str, Any]]) -> dict[str, Any]:
    observed = [row for row in records if row["memory_used"] is not None]
    if not observed:
        return {"status": "insufficient_evidence", "observed_runs": 0, "recall_assisted": 0, "useful_recall": None, "case_reuse": 0, "rules_applied": 0, "rate": None, "coverage": None}
    assisted = sum(row["memory_used"] is True for row in observed)
    cases = sum(1 for row in observed if row["memory_case_ids"])
    rules = sum(len(row["rules_applied"]) for row in observed)
    return {"status": "observed", "observed_runs": len(observed), "recall_assisted": assisted, "useful_recall": None,
            "case_reuse": cases, "rules_applied": rules, "rate": assisted / len(observed), "coverage": len(observed) / len(records) if records else None}


def _platform_efficiency(records: list[dict[str, Any]]) -> dict[str, Any]:
    inputs = [row["input_tokens"] for row in records if row["input_tokens"] is not None]
    cached = [row["cached_input_tokens"] for row in records if row["cached_input_tokens"] is not None]
    compatible = [row for row in records if row["input_tokens"] is not None and row["cached_input_tokens"] is not None and row["input_tokens"] > 0]
    denominator = sum(row["input_tokens"] for row in compatible)
    return {"source": "OpenAI/Codex prompt caching", "status": "observed" if compatible else "insufficient_evidence",
            "input_tokens": sum(inputs) if inputs else None, "cached_input_tokens": sum(cached) if cached else None,
            "prompt_cache_hit_rate": sum(row["cached_input_tokens"] for row in compatible) / denominator if denominator else None,
            "compatible_run_count": len(compatible)}


def _verification_budget(events: list[dict[str, Any]]) -> dict[str, Any]:
    budgets = Counter[str]()
    settings = Counter[str]()
    statuses = Counter[str]()
    normal_tasks = 0
    special_tasks = 0
    totals = Counter[str]()
    count_keys = (
        "tests_run", "targeted_tests", "full_suites", "builds", "lint_runs", "typechecks",
        "e2e_runs", "avoided_full_suites", "avoided_e2e", "avoided_builds", "avoided_lint",
        "avoided_typechecks", "verification_duration_ms",
    )
    recent: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "execution.completed":
            continue
        metadata = event.get("metadata")
        verification = metadata.get("verification") if isinstance(metadata, Mapping) else None
        plan = verification.get("plan") if isinstance(verification, Mapping) else None
        outcome = verification.get("outcome") if isinstance(verification, Mapping) else None
        if not isinstance(plan, Mapping) or not isinstance(outcome, Mapping):
            continue
        budget = plan.get("budget") if plan.get("budget") in {"none", "min", "strict", "release"} else "unknown"
        setting = plan.get("setting") if plan.get("setting") in {"auto_min", "none", "min", "strict", "release"} else "unknown"
        status = outcome.get("status") if outcome.get("status") in {"pass", "fail", "skipped", "not_required", "unknown"} else "unknown"
        classification = plan.get("event_classification") if plan.get("event_classification") in {"normal", "token_burn_event", "reset_expectation", "benchmark_event", "migration_event", "stress_test", "manual_override"} else "normal"
        budgets[budget] += 1
        settings[setting] += 1
        statuses[status] += 1
        if classification == "normal":
            normal_tasks += 1
            for key in count_keys:
                value = outcome.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    totals[key] += value
        else:
            special_tasks += 1
        recent.append({"timestamp": event.get("timestamp"), "budget": budget, "status": status, "event_classification": classification})
    avoided = sum(totals[key] for key in ("avoided_full_suites", "avoided_e2e", "avoided_builds", "avoided_lint", "avoided_typechecks"))
    performed = totals["tests_run"] + totals["builds"] + totals["lint_runs"] + totals["typechecks"] + totals["e2e_runs"]
    return {
        "setting_default": "auto_min", "budgets": dict(budgets), "settings": dict(settings),
        "statuses": dict(statuses), "normal_tasks": normal_tasks, "special_tasks": special_tasks,
        "performed_checks": performed if normal_tasks else None, "avoided_checks": avoided if normal_tasks else None,
        "avoidance_rate": avoided / (performed + avoided) if performed + avoided else None,
        "totals": {key: totals[key] if normal_tasks else None for key in count_keys},
        "recent": sorted(recent, key=lambda row: str(row.get("timestamp") or ""), reverse=True)[:20],
    }


def _ab_basis(records: list[dict[str, Any]]) -> dict[str, Any]:
    output_fields = {
        "execution_time_ms": "duration_ms", "total_tokens": "tokens", "cached_input_tokens": "cached_input_tokens",
        "output_tokens": "output_tokens", "reasoning_tokens": "reasoning_tokens", "model_calls": "model_calls",
        "tool_calls": "tool_calls", "file_reads": "file_reads", "retry_count": "retry_count",
    }
    evidence = [row for row in records if isinstance(row["benchmark"], Mapping) and row["benchmark"].get("mode") in {"on", "off"} and row["benchmark"].get("test_result") in {"passed", "failed", "unknown"} and isinstance(row["benchmark"].get("final_success"), bool)]
    observed_groups = {mode: sum(row["benchmark"].get("mode") == mode for row in evidence) for mode in ("on", "off")}
    indexed: dict[str, dict[tuple[str, str], list[dict[str, Any]]]] = {"on": {}, "off": {}}
    excluded = {"v1": 0, "unpaired": 0, "duplicate": 0, "missing_identity": 0}
    for row in evidence:
        benchmark = row["benchmark"]
        if benchmark.get("schema_version") != "2":
            excluded["v1"] += 1
            continue
        pair_id, scope_id = benchmark.get("pair_id"), benchmark.get("scope_id")
        if not isinstance(pair_id, str) or not isinstance(scope_id, str):
            excluded["missing_identity"] += 1
            continue
        indexed[benchmark["mode"]].setdefault((pair_id, scope_id), []).append(row)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for identity in set(indexed["on"]) | set(indexed["off"]):
        on_rows, off_rows = indexed["on"].get(identity, []), indexed["off"].get(identity, [])
        if len(on_rows) == len(off_rows) == 1:
            pairs.append((on_rows[0], off_rows[0]))
        elif on_rows and off_rows:
            excluded["duplicate"] += len(on_rows) + len(off_rows)
        else:
            excluded["unpaired"] += len(on_rows) + len(off_rows)
    groups: dict[str, dict[str, Any]] = {"on": {"runs": len(pairs), "evidence_runs": len(pairs)}, "off": {"runs": len(pairs), "evidence_runs": len(pairs)}}
    paired_observed: dict[str, int] = {}
    for output_name, record_name in output_fields.items():
        valid = [(on, off) for on, off in pairs if on.get(record_name) is not None and off.get(record_name) is not None]
        paired_observed[output_name] = len(valid)
        for index, mode in enumerate(("on", "off")):
            values = [pair[index][record_name] for pair in valid]
            groups[mode][output_name] = sum(values) if values else None
    uncached_pairs = [(on, off) for on, off in pairs if all(row["input_tokens"] is not None and row["cached_input_tokens"] is not None and row["input_tokens"] >= row["cached_input_tokens"] for row in (on, off))]
    paired_observed["uncached_input_tokens"] = len(uncached_pairs)
    for index, mode in enumerate(("on", "off")):
        values = [pair[index]["input_tokens"] - pair[index]["cached_input_tokens"] for pair in uncached_pairs]
        groups[mode]["uncached_input_tokens"] = sum(values) if values else None
    for index, mode in enumerate(("on", "off")):
        paired_rows = [pair[index] for pair in pairs]
        results = [str(row["benchmark"]["test_result"]) for row in paired_rows]
        successes = [row["benchmark"]["final_success"] for row in paired_rows]
        groups[mode]["test_result"] = dict(Counter(results)) if results else None
        groups[mode]["final_success"] = {"true": sum(value is True for value in successes), "observed": len(successes)} if successes else None
    paired_observed["test_result"] = len(pairs)
    paired_observed["final_success"] = len(pairs)
    return {
        "status": "measured" if pairs else "unavailable",
        "basis": "paired A/B v2 identity" if pairs else "pair/scope identity unavailable",
        "fields": [*output_fields, "uncached_input_tokens", "test_result", "final_success"],
        "paired_observed": paired_observed,
        "observed_groups": observed_groups,
        "excluded": excluded,
        "on": groups["on"], "off": groups["off"],
    }


def build_snapshot(events: Iterable[Mapping[str, Any]], sources: Any = None) -> dict[str, Any]:
    """Build aggregate-only dashboard data without inferring unknown facts."""
    input_rows = [dict(item) for item in events if isinstance(item, Mapping)]
    rows = sorted(input_rows, key=_event_order)
    runtime = Counter[str]()
    task_latest: dict[str, tuple[float, int | None, str]] = {}
    modes = Counter[str]()
    fallbacks = 0
    memory = Counter[str]()
    experience = Counter[str]()
    classifications = Counter[str]()
    special = Counter[str]()
    agents = Counter[str]()
    models = Counter[str]()
    providers = Counter[str]()
    tokens: list[int | float] = []
    usage_units: list[int | float] = []
    durations_ms: list[int | float] = []
    durations_seconds: list[int | float] = []
    task_ids: set[str] = set()
    for item in rows:
        name, state, classification = _text(item.get("event")), _text(item.get("status")), _text(item.get("event_classification"))
        meta = item.get("metadata")
        meta = meta if isinstance(meta, Mapping) else {}
        _inc(classifications, classification)
        _inc(agents, item.get("agent"))
        _inc(models, item.get("model"))
        _inc(providers, item.get("provider"))
        task_id = item.get("task_id") if isinstance(item.get("task_id"), str) else None
        if task_id:
            task_ids.add(task_id)
        runtime_state = "running" if state in {"running", "started"} else "completed" if state in {"success", "succeeded", "completed", "complete"} else "failed" if state in {"failed", "failure", "error", "rejected", "host_adapter_failed", "host_adapter_unavailable"} else "unknown" if state == "not_dispatched" else None
        if runtime_state and (name.startswith("task.") or name.startswith("execution.")):
            if task_id:
                observed, sequence = _event_order(item), _event_sequence(item)
                current = task_latest.get(task_id)
                if current is None or observed > current[0]:
                    task_latest[task_id] = (observed, sequence, runtime_state)
                elif observed == current[0]:
                    if sequence is not None and current[1] is not None and sequence > current[1]:
                        task_latest[task_id] = (observed, sequence, runtime_state)
                    elif sequence is not None and current[1] is not None and sequence < current[1]:
                        pass
                    elif runtime_state != current[2]:
                        task_latest[task_id] = (observed, None, "unknown")
            else:
                runtime[runtime_state] += 1
        _inc(modes, meta.get("routing_mode", meta.get("mode")))
        if bool(meta.get("fallback")) or "fallback" in name or "fallback" in classification:
            fallbacks += 1
        if "recall" in name:
            memory["recalled"] += 1
        recalled_count = _number(meta.get("memory_recalled_count"))
        if recalled_count is not None and recalled_count >= 0:
            memory["recalled"] += int(recalled_count)
        if "create" in name or "learn" in name:
            memory["created"] += 1
        if "update" in name:
            memory["updated"] += 1
        if "reuse" in name:
            memory["reuse"] += 1
        if "hit" in name:
            memory["hits"] += 1
        if any(x in name for x in ("evaluation", "evaluate")):
            experience["evaluations"] += 1
        if "reuse" in name:
            experience["reuse"] += 1
        if state in {"success", "succeeded", "completed", "complete"}:
            experience["success"] += 1
        if state in {"failed", "failure", "error", "rejected", "host_adapter_failed", "host_adapter_unavailable"}:
            experience["failure"] += 1
        for kind in ("special", "migration", "benchmark", "error", "warning"):
            if kind in name or kind in classification or state == kind:
                special[kind] += 1
        token_value = _number(meta.get("total_tokens", meta.get("token_count")))
        if token_value is None:
            parts = [_number(meta.get(key)) for key in ("input_tokens", "output_tokens")]
            token_value = sum(value for value in parts if value is not None) if any(value is not None for value in parts) else None
        if token_value is not None:
            tokens.append(token_value)
        for key in ("usage_units", "measured_units"):
            value = _number(meta.get(key, item.get(key)))
            if value is not None:
                usage_units.append(value)
        elapsed_ms = _number(meta.get("elapsed_ms", item.get("elapsed_ms")))
        elapsed_seconds = _number(meta.get("elapsed_seconds", item.get("elapsed_seconds")))
        if elapsed_ms is not None:
            durations_ms.append(elapsed_ms)
        if elapsed_seconds is not None:
            durations_seconds.append(elapsed_seconds)
    for _, _, latest in task_latest.values():
        runtime[latest] += 1
    recall_total = memory["recalled"] + memory["hits"]
    records = run_records(rows)
    run_tokens = [row["tokens"] for row in records if row["tokens"] is not None]
    run_duration = [row["duration_ms"] for row in records if row["duration_ms"] is not None]
    return {
        "api_version": DASHBOARD_API_VERSION,
        "system_status": {"sources": sources if sources is not None else [], "component_versions": _component_versions(),
                          "health": {"system": None, "collector": None, "agents": None, "devices": None}},
        "routecraft_impact": _impact(records),
        "execution": {"observed_runs": len(records), "tokens": sum(run_tokens) if run_tokens else (sum(tokens) if tokens else None), "duration_ms": sum(run_duration) if run_duration else (sum(durations_ms) if durations_ms else None)},
        "platform_efficiency": _platform_efficiency(records),
        "ab_basis": _ab_basis(records),
        "memory_effect": _memory_effect(records),
        "verification": _verification_budget(rows),
        "runtime": {"running": runtime["running"], "completed": runtime["completed"], "failed": runtime["failed"], "unknown": runtime["unknown"]},
        "routing": {"modes": dict(modes), "fallbacks": fallbacks},
        "memory": {"recalled": memory["recalled"], "created": memory["created"], "updated": memory["updated"], "reuse": memory["reuse"], "hits": memory["hits"], "hit_rate": (memory["hits"] / recall_total) if recall_total else None},
        "experience": {"success": experience["success"], "failure": experience["failure"], "reuse": experience["reuse"], "evaluations": experience["evaluations"]},
        "usage": {"tokens": sum(tokens) if tokens else None, "usage_units": sum(usage_units) if usage_units else None, "task_count": len(task_ids) if task_ids else None, "duration_ms": sum(durations_ms) if durations_ms else None, "duration_seconds": sum(durations_seconds) if durations_seconds else None, "models": dict(models), "agents": dict(agents), "providers": dict(providers)},
        "events": {"total": len(rows), "classifications": dict(classifications), "special": dict(special), "recent": [_safe_event(item) for item in sorted(input_rows, key=_event_order, reverse=True)[:20]], "runs": len(records)},
        "sources": sources if sources is not None else [],
    }
