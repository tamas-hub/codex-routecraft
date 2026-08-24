"""Versioned graph config, lane registry and human-gated policy candidates."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .constants import CONFIG_VERSION, MODES, POLICY_STATUSES

DEFAULT_LANE_REGISTRY = {"registry_version": 1, "lanes": {
    "luna": {"capability_class": "bounded", "cost_class": "low", "context_class": "small", "reasoning_levels": ["low", "medium", "max"], "allowed_task_types": ["bounded_implementation"], "risk_limit": "medium", "provider_mapping": "local-profile:luna"},
    "terra": {"capability_class": "judgment", "cost_class": "medium", "context_class": "large", "reasoning_levels": ["medium", "high"], "allowed_task_types": ["integration", "reviewed_implementation"], "risk_limit": "high", "provider_mapping": "local-profile:terra"},
    "sol": {"capability_class": "architecture", "cost_class": "high", "context_class": "largest", "reasoning_levels": ["high", "ultra"], "allowed_task_types": ["architecture", "acceptance", "fresh_review"], "risk_limit": "critical", "provider_mapping": "host:parent-sol"},
}}

DEFAULT_CONFIG = {"config_version": CONFIG_VERSION, "graph": {"mode": "observe", "max_parallelism": 3, "max_node_attempts": 3, "max_graph_revisions": 3, "state_store": None, "checkpoint": True}, "policy": {"production_policy": "routecraft-production-v1", "allowlisted_task_classes": []}, "control_center": {"enabled": False}}


class PolicyError(ValueError): pass


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict) or set(config) != {"config_version", "graph", "policy", "control_center"} or config.get("config_version") != CONFIG_VERSION: raise PolicyError("unknown config version")
    graph = config.get("graph")
    if not isinstance(graph, dict) or set(graph) != {"mode", "max_parallelism", "max_node_attempts", "max_graph_revisions", "state_store", "checkpoint"} or graph.get("mode") not in MODES: raise PolicyError("graph mode invalid")
    if any(not isinstance(graph.get(key), int) or isinstance(graph[key], bool) or graph[key] < 1 for key in ("max_parallelism", "max_node_attempts", "max_graph_revisions")): raise PolicyError("graph limits invalid")
    if graph.get("state_store") is not None and not isinstance(graph["state_store"], str): raise PolicyError("state store invalid")
    if not isinstance(graph.get("checkpoint"), bool): raise PolicyError("checkpoint invalid")
    policy = config.get("policy")
    if not isinstance(policy, dict) or set(policy) != {"production_policy", "allowlisted_task_classes"} or not isinstance(policy.get("production_policy"), str) or not policy["production_policy"] or not isinstance(policy.get("allowlisted_task_classes"), list) or not all(isinstance(value, str) for value in policy["allowlisted_task_classes"]): raise PolicyError("allowlist invalid")
    control = config.get("control_center")
    if not isinstance(control, dict) or set(control) != {"enabled"} or not isinstance(control.get("enabled"), bool): raise PolicyError("control center invalid")


def default_config() -> dict[str, Any]: return deepcopy(DEFAULT_CONFIG)


def validate_lane_registry(registry: dict[str, Any]) -> None:
    if registry.get("registry_version") != 1 or not isinstance(registry.get("lanes"), dict): raise PolicyError("lane registry invalid")
    required = {"capability_class", "cost_class", "context_class", "reasoning_levels", "allowed_task_types", "risk_limit", "provider_mapping"}
    for name, lane in registry["lanes"].items():
        if not isinstance(name, str) or not isinstance(lane, dict) or set(lane) != required or not all(isinstance(lane.get(k), str) and lane[k] for k in ("capability_class", "cost_class", "context_class", "risk_limit", "provider_mapping")) or lane["risk_limit"] not in {"low", "medium", "high", "critical"} or not isinstance(lane["reasoning_levels"], list) or not all(isinstance(value, str) for value in lane["reasoning_levels"]) or not isinstance(lane["allowed_task_types"], list) or not all(isinstance(value, str) for value in lane["allowed_task_types"]): raise PolicyError("lane entry invalid")


def validate_policy_candidate(candidate: dict[str, Any]) -> None:
    keys = {"policy_id", "base_policy", "candidate_change", "evidence", "sample_size", "confidence", "expected_benefit", "known_risk", "status"}
    if not isinstance(candidate, dict) or set(candidate) != keys: raise PolicyError("policy candidate schema invalid")
    if not isinstance(candidate["policy_id"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", candidate["policy_id"]): raise PolicyError("policy candidate id invalid")
    if not isinstance(candidate["base_policy"], str) or not candidate["base_policy"]: raise PolicyError("policy base invalid")
    change = candidate["candidate_change"]
    if not (isinstance(change, str) and change.strip() or isinstance(change, dict) and change): raise PolicyError("policy change invalid")
    evidence = candidate["evidence"]
    if not isinstance(evidence, list): raise PolicyError("policy evidence invalid")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"evidence_ref", "event_classification"} or not isinstance(item["evidence_ref"], str) or not item["evidence_ref"] or item["event_classification"] not in {"normal", "benchmark_run", "migration_event", "incident_response", "token_burn_event", "reset_expectation", "manual_stress_test", "release_validation"}:
            raise PolicyError("policy evidence invalid")
    if len({item["evidence_ref"] for item in evidence}) != len(evidence): raise PolicyError("policy evidence duplicate")
    if not isinstance(candidate["sample_size"], int) or isinstance(candidate["sample_size"], bool) or candidate["sample_size"] < 0: raise PolicyError("policy sample invalid")
    if candidate["confidence"] not in {"insufficient", "low", "medium", "high"}: raise PolicyError("policy confidence invalid")
    if candidate["expected_benefit"] is not None and (not isinstance(candidate["expected_benefit"], (int, float)) or isinstance(candidate["expected_benefit"], bool)): raise PolicyError("policy benefit invalid")
    if not isinstance(candidate["known_risk"], str) or not candidate["known_risk"].strip(): raise PolicyError("policy risk invalid")
    if candidate["status"] not in POLICY_STATUSES: raise PolicyError("policy status invalid")
    if candidate["status"] in {"SHADOW", "CANDIDATE", "APPROVED"} and (candidate["sample_size"] < 1 or not evidence): raise PolicyError("policy evidence is insufficient for active status")
    if candidate["status"] in {"CANDIDATE", "APPROVED"} and any(item["event_classification"] != "normal" for item in evidence): raise PolicyError("production policy evidence must be normal-only")
    if candidate["status"] == "APPROVED" and candidate["confidence"] not in {"medium", "high"}: raise PolicyError("approved policy confidence is insufficient")


def validate_policy_transition(previous: dict[str, Any] | None, candidate: dict[str, Any]) -> None:
    validate_policy_candidate(candidate)
    if previous is None:
        if candidate["status"] != "DRAFT": raise PolicyError("policy history must start at DRAFT")
        return
    validate_policy_candidate(previous)
    transitions = {
        "DRAFT": {"SHADOW", "REJECTED"},
        "SHADOW": {"CANDIDATE", "REJECTED", "RETIRED"},
        "CANDIDATE": {"APPROVED", "REJECTED", "RETIRED"},
        "APPROVED": {"RETIRED"},
        "REJECTED": set(),
        "RETIRED": set(),
    }
    if candidate["status"] not in transitions[previous["status"]]: raise PolicyError("policy status transition invalid")
    for key in ("policy_id", "base_policy", "candidate_change"):
        if candidate[key] != previous[key]: raise PolicyError("policy identity is immutable")
    if candidate["sample_size"] < previous["sample_size"]: raise PolicyError("policy sample size may not decrease")
    previous_refs = {item["evidence_ref"] for item in previous["evidence"]}
    current_refs = {item["evidence_ref"] for item in candidate["evidence"]}
    if not previous_refs.issubset(current_refs): raise PolicyError("policy evidence may not be removed")


def can_promote(candidate: dict[str, Any], approval: dict[str, Any] | None) -> bool:
    """Return eligibility only; this module never writes Production Policy.

    A caller-supplied boolean is deliberately insufficient. The approval must
    carry local actor, timestamp and an evidence reference that is already
    bound into the immutable candidate history.
    """
    validate_policy_candidate(candidate)
    if not isinstance(approval, dict) or set(approval) != {"actor_ref", "evidence_ref", "approved_at", "decision"}: return False
    if approval.get("decision") != "APPROVE" or not isinstance(approval.get("actor_ref"), str) or not re.fullmatch(r"[A-Za-z0-9._:@+-]{1,80}", approval["actor_ref"]): return False
    if not isinstance(approval.get("approved_at"), str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", approval["approved_at"]): return False
    evidence_refs = {item["evidence_ref"] for item in candidate["evidence"]}
    return candidate["status"] == "APPROVED" and approval.get("evidence_ref") in evidence_refs
