"""RouteCraft 0.7 graph-kernel constants.  No provider SDK is required."""

from __future__ import annotations

GRAPH_SCHEMA_VERSION = 1
STORE_SCHEMA_VERSION = 1
CONFIG_VERSION = 1

MODES = frozenset({"off", "observe", "enforce"})
EVENT_CLASSIFICATIONS = frozenset({
    "normal", "benchmark_run", "migration_event", "incident_response",
    "token_burn_event", "reset_expectation", "manual_stress_test", "release_validation",
})
NODE_TYPES = frozenset({
    "AGENT", "TOOL", "DETERMINISTIC", "GATE", "MERGE", "HUMAN_APPROVAL",
    "MEMORY_RECALL", "BENCHMARK", "SECURITY", "CHECKPOINT", "QUALITY",
})
NODE_STATUSES = frozenset({
    "PENDING", "READY", "RUNNING", "ACCEPTED", "FROZEN", "FAILED",
    "INVALIDATED", "BLOCKED", "SKIPPED", "CANCELLED",
})
SUCCESS_STATUSES = frozenset({"ACCEPTED", "FROZEN", "SKIPPED"})
EDGE_TYPES = frozenset({
    "depends_on", "fan_out", "sequence", "merge", "gate_pass", "gate_fail",
    "send_back", "constraint_feedback",
})
DEPENDENCY_EDGE_TYPES = frozenset({
    "depends_on", "fan_out", "sequence", "merge", "gate_pass", "gate_fail",
})
# `send_back` deliberately does not belong to this directed dependency DAG.
# It is a bounded control transition whose direction is checked separately by
# the compiler.  Keeping these names explicit avoids accidentally treating a
# retry loop as an ordinary cycle.
ORDERING_EDGE_TYPES = DEPENDENCY_EDGE_TYPES
CONDITIONAL_EDGE_TYPES = frozenset({"gate_pass", "gate_fail"})
CONTROL_EDGE_TYPES = frozenset({"send_back", "constraint_feedback"})
EVIDENCE_CLASSIFICATIONS = frozenset({
    "FACT", "HYPOTHESIS", "ASSUMPTION", "VERIFIED_CONSTRAINT", "RECOMMENDATION",
})
GATE_RESULTS = frozenset({"PASS", "FAIL", "INCONCLUSIVE"})
POLICY_STATUSES = frozenset({"DRAFT", "SHADOW", "CANDIDATE", "APPROVED", "REJECTED", "RETIRED"})
RECEIPT_STATUSES = frozenset({"PREPARED", "COMMITTED", "FAILED", "UNKNOWN"})

GRAPH_STATUSES = frozenset({"DRAFT", "COMPILED", "RUNNING", "BLOCKED", "FAILED", "ACCEPTED", "CANCELLED"})
