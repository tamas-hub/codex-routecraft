"""Small durable facade.  It orchestrates state; model/tool execution remains a host boundary."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .canonical import sha256, stable_id, utc_now
from .compiler import compile_graph
from .constants import GATE_RESULTS
from .contracts import validate_attempt_usage, validate_evidence, validate_operation_descriptor, validate_value, validate_verified_constraint
from .execution_boundary import EXECUTION_BOUNDARY_VERSION, ExecutionBoundary, ExecutorBinding
from .ir import canonical_ir
from .policy import PolicyError, validate_config
from .scheduler import ready_nodes
from .state import StateTransitionError, accept_node, add_constraint, apply_send_back, fail_node, mark_ready, resolve_gate_result, retry_node, start_node
from .store import GraphStore, GraphStoreError
from .telemetry import privacy_projection


class GraphEngine:
    def __init__(
        self,
        store: GraphStore,
        *,
        config: dict[str, Any],
        lane_registry: dict[str, Any] | None = None,
        execution_boundary: ExecutionBoundary | None = None,
    ):
        self.store, self.config, self.lane_registry = store, config, lane_registry
        # Claims are deliberately process-local.  A process interruption rolls
        # an uncheckpointed RUNNING attempt back to READY; the host must claim
        # it again rather than replaying a caller-provided result.
        self.execution_boundary = execution_boundary
        self._attempt_claims: dict[tuple[str, int, str, int], tuple[ExecutorBinding, object]] = {}

    @staticmethod
    def _enforce_error(detail: str) -> GraphStoreError:
        return GraphStoreError(f"ENFORCE_BOUNDARY_UNAVAILABLE: {detail}")

    @staticmethod
    def _policy_error(detail: str) -> GraphStoreError:
        return GraphStoreError(f"ENFORCE_POLICY_REVOKED: {detail}")

    def _require_current_enforce_policy(self, graph: dict[str, Any]) -> None:
        """Re-check policy before every enforce execution transition.

        Graph policy ids are opaque versioned identifiers. The comparison is
        exact and case-sensitive; a changed policy must trigger a replan, not
        a compatibility guess for an already durable graph.
        """
        if graph.get("mode") != "enforce":
            return
        try:
            validate_config(self.config)
        except PolicyError as exc:
            raise self._policy_error("current policy configuration is invalid") from exc
        # A durable enforce graph retains its requested mode, but the local
        # configuration is the live execution authority.  If an operator (or
        # a newer process) downgrades the effective mode after planning, an
        # already-running attempt must not be allowed to commit its result.
        # Keep this under the same stable revocation prefix as policy and
        # allowlist changes so callers can handle every fail-closed authority
        # revocation uniformly.
        if self.config["graph"]["mode"] != "enforce":
            raise self._policy_error("configured graph mode is no longer enforce")
        policy = self.config["policy"]
        if graph.get("policy_version") != policy["production_policy"]:
            raise self._policy_error("graph policy_version no longer matches production_policy")
        if graph.get("task_class") not in policy["allowlisted_task_classes"]:
            raise self._policy_error("graph task_class is no longer allowlisted")

    def _resolve_executor(self, graph: dict[str, Any], node: dict[str, Any]) -> ExecutorBinding:
        """Resolve only a host-owned, node-contract-matching executor."""
        self._require_current_enforce_policy(graph)
        boundary = self.execution_boundary
        if boundary is None or getattr(boundary, "boundary_version", None) != EXECUTION_BOUNDARY_VERSION:
            raise self._enforce_error("trusted execution boundary v1 is not configured")
        try:
            binding = boundary.resolve_executor(graph, node)
        except Exception as exc:
            raise self._enforce_error("executor resolution failed") from exc
        if not isinstance(binding, ExecutorBinding):
            raise self._enforce_error("executor is unresolved")
        if (
            binding.boundary_version != EXECUTION_BOUNDARY_VERSION
            or not binding.executor_id
            or binding.node_type != node["node_type"]
            or binding.capability_profile != node["capability_profile"]
            or binding.allowed_tools != tuple(node["allowed_tools"])
            or binding.denied_operations != tuple(node["denied_operations"])
            or binding.write_scopes != tuple(node["ownership"]["write_scopes"])
            or binding.risk_limit not in {"low", "medium", "high", "critical"}
            or {"low": 0, "medium": 1, "high": 2, "critical": 3}[node["risk"]]
            > {"low": 0, "medium": 1, "high": 2, "critical": 3}[binding.risk_limit]
        ):
            raise self._enforce_error("executor does not match the node capability, tool, write, or risk contract")
        return binding

    def _require_plan_boundary(self, graph: dict[str, Any]) -> None:
        """Do not durable-plan enforce work that no trusted host can execute."""
        for node in graph["nodes"]:
            self._resolve_executor(graph, node)

    def _begin_trusted_attempt(self, graph: dict[str, Any], node: dict[str, Any]) -> tuple[ExecutorBinding, object]:
        executor = self._resolve_executor(graph, node)
        prospective = deepcopy(node)
        prospective["status"] = "RUNNING"
        prospective["attempt"] += 1
        try:
            claim = self.execution_boundary.begin_attempt(graph, prospective, executor) if self.execution_boundary else None
        except Exception as exc:
            raise self._enforce_error("trusted executor could not claim the attempt") from exc
        if claim is None:
            raise self._enforce_error("trusted executor did not claim the attempt")
        return executor, claim

    def _verify_trusted_result(
        self,
        graph: dict[str, Any],
        node: dict[str, Any],
        output: Any,
        evidence: list[dict[str, Any]],
        gate_result: str,
        usage: dict[str, Any] | None,
        attestation: object | None,
    ) -> None:
        if graph["mode"] != "enforce":
            return
        self._require_current_enforce_policy(graph)
        key = (graph["graph_id"], graph["graph_revision"], node["node_id"], node["attempt"])
        claimed = self._attempt_claims.get(key)
        if claimed is None:
            raise self._enforce_error("trusted attempt claim is unavailable")
        if attestation is None:
            raise self._enforce_error("trusted result attestation is required")
        executor, claim = claimed
        boundary = self.execution_boundary
        if boundary is None:
            raise self._enforce_error("trusted execution boundary v1 is not configured")
        try:
            verified = boundary.verify_result(
                graph, node, executor, claim, attestation, output, evidence, gate_result, usage
            )
        except Exception as exc:
            raise self._enforce_error("trusted result verification failed") from exc
        if verified is not True:
            raise self._enforce_error("untrusted result or evidence")

    def plan(self, ir: dict[str, Any]) -> dict[str, Any]:
        if ir.get("graph_revision") != 1 or ir.get("status") != "DRAFT":
            raise GraphStoreError("new graph plan must be DRAFT revision 1")
        if any(
            node.get("status") != "PENDING"
            or node.get("attempt") != 0
            or node.get("input_hash") is not None
            or node.get("output_hash") is not None
            or node.get("evidence_refs")
            or node.get("gate_result") is not None
            for node in ir.get("nodes", [])
        ):
            raise GraphStoreError("new graph plan contains pre-executed node state")
        compiled = compile_graph(ir, lane_registry=self.lane_registry, config=self.config)
        if compiled["ir"]["mode"] == "enforce": self._require_plan_boundary(compiled["ir"])
        graph = deepcopy(compiled["ir"]); graph["status"] = "COMPILED"
        if graph["mode"] == "enforce": graph = mark_ready(graph, self.config["graph"]["max_parallelism"])
        # Write the compile checkpoint first. If the process stops before the
        # revision row is committed, resume can materialize revision 1 from
        # this authenticated checkpoint instead of leaving an unresumable
        # graph with no checkpoint.
        self.store.checkpoint(graph, "compile_complete")
        self.store.save_revision(graph, reason="compile")
        self.store.append_ledger(stable_id("le", {"graph": graph["graph_id"], "revision": graph["graph_revision"], "kind": "intent"}), graph["graph_id"], graph["graph_revision"], "INTENT", graph["contracts"]["intent"])
        return graph

    def status(self, graph_id: str) -> dict[str, Any]: return self.store.load_revision(graph_id)

    def ready(self, graph_id: str) -> list[str]:
        graph = self.status(graph_id)
        if graph["mode"] != "enforce": return []
        self._require_current_enforce_policy(graph)
        return ready_nodes(graph, max_parallelism=self.config["graph"]["max_parallelism"])

    @staticmethod
    def _empty_usage() -> dict[str, int | None]:
        return {"duration_ms": None, "input_tokens": None, "cached_input_tokens": None, "output_tokens": None, "reasoning_tokens": None, "child_runs": None}

    def _attempt_usage_entry(self, graph: dict[str, Any], node: dict[str, Any], usage: dict[str, Any] | None) -> tuple[dict[str, int | None], tuple[str, str, dict[str, Any], str | None]]:
        """Build, but do not persist, an attempt-usage ledger entry.

        Result finalization writes this alongside evidence and the accepted or
        failed state in one transaction.  Callers that intentionally record a
        standalone failure retain ``_record_usage`` below.
        """
        measured = self._empty_usage() if usage is None else deepcopy(usage)
        errors = validate_attempt_usage(measured)
        if errors: raise GraphStoreError("invalid attempt usage: " + "; ".join(errors))
        if node["status"] != "RUNNING" or node["attempt"] < 1: raise GraphStoreError("attempt usage requires RUNNING node")
        payload = {"event": "ATTEMPT_USAGE", "node_id": node["node_id"], "attempt": node["attempt"], "usage": measured}
        entry_id = stable_id("le", {"graph": graph["graph_id"], "revision": graph["graph_revision"], "node": node["node_id"], "attempt": node["attempt"], "event": "ATTEMPT_USAGE"})
        return measured, (entry_id, "PROGRESS", payload, None)

    @staticmethod
    def _with_pending_usage(totals: dict[str, int | None], pending_usage: dict[str, int | None] | None) -> dict[str, int | None]:
        if pending_usage is None:
            return totals
        metrics = ("duration_ms", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "child_runs")
        existing_count = int(totals["attempts_with_usage"])
        result: dict[str, int | None] = {"attempts_with_usage": existing_count + 1}
        for metric in metrics:
            previous, pending = totals[metric], pending_usage[metric]
            result[metric] = pending if existing_count == 0 else previous + pending if isinstance(previous, int) and isinstance(pending, int) else None
        token_parts = [result["input_tokens"], result["output_tokens"], result["reasoning_tokens"]]
        result["total_tokens"] = sum(token_parts) if all(isinstance(value, int) for value in token_parts) else None
        return result

    def _budget_violation(self, graph: dict[str, Any], node: dict[str, Any], *, before_start: bool = False, pending_usage: dict[str, int | None] | None = None) -> str | None:
        node_totals = self.store.usage_totals(graph["graph_id"], graph["graph_revision"], node["node_id"])
        graph_totals = self.store.usage_totals(graph["graph_id"], graph["graph_revision"])
        node_totals = self._with_pending_usage(node_totals, pending_usage)
        graph_totals = self._with_pending_usage(graph_totals, pending_usage)

        def check(scope: str, totals: dict[str, int | None], limits: dict[str, int | None]) -> str | None:
            comparisons = (
                ("max_tokens", "total_tokens", "TOKEN"),
                ("max_duration_seconds", "duration_ms", "DURATION"),
                ("max_child_runs", "child_runs", "CHILD_RUN"),
            )
            for limit_key, total_key, label in comparisons:
                limit = limits.get(limit_key)
                if limit is None or scope == "NODE" and limit_key == "max_child_runs": continue
                total = totals.get(total_key)
                if total is None:
                    if before_start and not totals["attempts_with_usage"]: continue
                    return f"{scope}_USAGE_INCONCLUSIVE"
                ceiling = limit * 1000 if limit_key == "max_duration_seconds" else limit
                if total > ceiling or before_start and total >= ceiling:
                    return f"{scope}_{label}_BUDGET_EXCEEDED"
            return None

        return check("NODE", node_totals, node["retry_policy"]) or check("GRAPH", graph_totals, graph["budgets"])

    @staticmethod
    def _pending_send_back_sources(graph: dict[str, Any]) -> list[str]:
        """Completed failed Gates whose bounded control transition is durable but unapplied."""
        return sorted(
            node["node_id"]
            for node in graph["nodes"]
            if node["node_type"] == "GATE"
            and node["status"] in {"ACCEPTED", "FROZEN"}
            and node.get("gate_result") in {"FAIL", "INCONCLUSIVE"}
            and any(edge["edge_type"] == "send_back" and edge["from"] == node["node_id"] for edge in graph["edges"])
        )

    def _recover_pending_send_backs(self, graph: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Deterministically finish a committed Gate resolution after restart."""
        changed = False
        for source_node_id in self._pending_send_back_sources(graph):
            # An earlier control transition can invalidate a second gate.  It
            # is no longer a pending source and must not be replayed.
            source = next(item for item in graph["nodes"] if item["node_id"] == source_node_id)
            if source["status"] not in {"ACCEPTED", "FROZEN"}:
                continue
            graph = apply_send_back(graph, source_node_id)
            changed = True
        return graph, changed

    @staticmethod
    def _operation_hash(graph: dict[str, Any], node: dict[str, Any], descriptor: dict[str, Any], capability_prefix: str) -> str:
        errors = validate_operation_descriptor(descriptor)
        if errors: raise GraphStoreError("invalid operation descriptor: " + "; ".join(errors))
        kind = descriptor["kind"]
        if f"{capability_prefix}:{kind}" not in node["allowed_tools"]: raise GraphStoreError("operation descriptor capability not declared")
        declarations = [item for item in graph["contracts"]["intent"]["external_mutations"] if item["kind"] == kind]
        if len(declarations) != 1 or declarations[0]["target_scope"] != descriptor["target_scope"]: raise GraphStoreError("operation descriptor exceeds Intent Contract")
        return sha256({"graph_revision": graph["graph_revision"], "descriptor": descriptor})

    def start(self, graph_id: str, node_id: str) -> dict[str, Any]:
        graph = self.status(graph_id)
        node = next((item for item in graph["nodes"] if item["node_id"] == node_id), None)
        if graph["mode"] != "enforce": raise GraphStoreError("graph scheduler execution requires configured enforce mode")
        self._require_current_enforce_policy(graph)
        if node is None: raise GraphStoreError("unknown node")
        if node["node_type"] == "HUMAN_APPROVAL": raise GraphStoreError("human approval requires the dedicated approve transition")
        if node["attempt"] >= node["retry_policy"]["max_attempts"]: raise GraphStoreError("NODE_CONVERGENCE_FAILED")
        violation = self._budget_violation(graph, node, before_start=True)
        if violation: raise GraphStoreError(violation)
        if node_id not in self.ready(graph_id): raise GraphStoreError("node is not currently schedulable")
        executor, claim = self._begin_trusted_attempt(graph, node)
        graph = start_node(graph, node_id); self.store.save_revision(graph)
        running = next(item for item in graph["nodes"] if item["node_id"] == node_id)
        self._attempt_claims[(graph_id, graph["graph_revision"], node_id, running["attempt"])] = (executor, claim)
        self.store.append_ledger(stable_id("le", {"graph": graph_id, "node": node_id, "attempt": running["attempt"], "status": "RUNNING"}), graph_id, graph["graph_revision"], "PROGRESS", {"node_id": node_id, "status": "RUNNING", "attempt": running["attempt"]})
        return graph

    def record_result(self, graph_id: str, node_id: str, output: Any, evidence: list[dict[str, Any]], *, gate_result: str = "PASS", usage: dict[str, Any] | None = None, attestation: object | None = None) -> dict[str, Any]:
        if gate_result not in GATE_RESULTS: raise GraphStoreError("gate result invalid")
        invalid = [error for item in evidence for error in validate_evidence(item)]
        if invalid: raise GraphStoreError("invalid evidence: " + "; ".join(invalid))
        graph = self.status(graph_id)
        node = next((item for item in graph["nodes"] if item["node_id"] == node_id), None)
        if node is None: raise GraphStoreError("unknown node")
        if node["node_type"] == "HUMAN_APPROVAL": raise GraphStoreError("human approval requires the dedicated approve transition")
        if any(item["node_id"] != node_id for item in evidence): raise GraphStoreError("evidence node mismatch")
        if not validate_value(output, node["output_schema"]): raise GraphStoreError("node output contract mismatch")
        required = set(node["verification"].get("required_evidence_types", []))
        supplied = {item["evidence_type"] for item in evidence if item["classification"] in {"FACT", "VERIFIED_CONSTRAINT"} and item["result"] == gate_result}
        if not required.issubset(supplied): raise GraphStoreError("gate required evidence missing")
        self._verify_trusted_result(graph, node, output, evidence, gate_result, usage, attestation)
        measured_usage, usage_entry = self._attempt_usage_entry(graph, node, usage)
        ledger_entries = [
            (item["evidence_id"], "EVIDENCE", item, item["classification"])
            for item in evidence
        ]
        ledger_entries.append(usage_entry)
        violation = self._budget_violation(graph, node, pending_usage=measured_usage)
        if violation:
            failed = fail_node(graph, node_id, violation)
            failed_node = next(item for item in failed["nodes"] if item["node_id"] == node_id)
            ledger_entries.append((
                stable_id("le", {"graph": graph_id, "node": node_id, "attempt": failed_node["attempt"], "status": "FAILED", "reason": violation}),
                "PROGRESS",
                {"node_id": node_id, "status": "FAILED", "attempt": failed_node["attempt"], "reason_code": violation},
                None,
            ))
            self.store.commit_result(failed, "node_failure", ledger_entries)
            raise GraphStoreError(violation)
        refs = [item["evidence_id"] for item in evidence]
        if node["node_type"] == "GATE":
            graph = resolve_gate_result(graph, node_id, gate_result, output, refs)
            # A Gate verdict and its checkpoint are one SQLite transaction.
            # Resume can distinguish it from a later send-back state without
            # ever observing a newer snapshot paired with an older checkpoint.
            self.store.commit_result(graph, "gate_resolution", ledger_entries)
            has_send_back = gate_result != "PASS" and any(
                edge["edge_type"] == "send_back" and edge["from"] == node_id
                for edge in graph["edges"]
            )
            if has_send_back:
                try:
                    graph = apply_send_back(graph, node_id)
                except StateTransitionError as error:
                    if str(error) != "NODE_CONVERGENCE_FAILED":
                        raise GraphStoreError(str(error)) from error
                    graph["status"] = "FAILED"
                    convergence_entry = (
                        stable_id("le", {"graph": graph_id, "node": node_id, "attempt": node["attempt"], "status": "FAILED", "reason": "NODE_CONVERGENCE_FAILED"}),
                        "PROGRESS",
                        {"node_id": node_id, "status": "FAILED", "attempt": node["attempt"], "reason_code": "NODE_CONVERGENCE_FAILED"},
                        None,
                    )
                    self.store.commit_result(graph, "node_convergence_failed", [convergence_entry])
                    raise GraphStoreError("NODE_CONVERGENCE_FAILED") from error
                self.store.save_and_checkpoint(graph, "send_back")
                return graph
            if node["gate_policy"]["global"]:
                self.store.checkpoint(graph, "global_gate_complete")
            return graph
        if gate_result != "PASS":
            reason = f"GATE_{gate_result}"
            failed = fail_node(graph, node_id, reason)
            failed_node = next(item for item in failed["nodes"] if item["node_id"] == node_id)
            ledger_entries.append((
                stable_id("le", {"graph": graph_id, "node": node_id, "attempt": failed_node["attempt"], "status": "FAILED", "reason": reason}),
                "PROGRESS",
                {"node_id": node_id, "status": "FAILED", "attempt": failed_node["attempt"], "reason_code": reason},
                None,
            ))
            self.store.commit_result(failed, "node_failure", ledger_entries)
            return failed
        graph = accept_node(graph, node_id, output, refs)
        boundary = "node_acceptance"
        accepted = next(item for item in graph["nodes"] if item["node_id"] == node_id)
        if accepted["node_type"] == "MERGE": boundary = "merge_complete"
        if accepted["node_type"] == "CHECKPOINT": boundary = "checkpoint_node_complete"
        self.store.commit_result(graph, boundary, ledger_entries)
        return graph

    def approve_human(self, graph_id: str, node_id: str, confirmation: str, actor_ref: str, operation_descriptor: dict[str, Any], evidence: list[dict[str, Any]], *, usage: dict[str, Any] | None = None) -> dict[str, Any]:
        """Accept a Human Approval node only through an explicit, input-bound transition."""
        graph = self.status(graph_id)
        self._require_current_enforce_policy(graph)
        node = next((item for item in graph["nodes"] if item["node_id"] == node_id), None)
        if graph["mode"] != "enforce" or self.config["graph"]["mode"] != "enforce": raise GraphStoreError("human approval requires configured enforce mode")
        if node is None or node["node_type"] != "HUMAN_APPROVAL": raise GraphStoreError("node is not a human approval node")
        if node_id not in self.ready(graph_id) or node["status"] != "READY": raise GraphStoreError("human approval node is not ready")
        operation_hash = self._operation_hash(graph, node, operation_descriptor, "approve")
        expected = f"{graph_id}:{node_id}:{node['input_hash']}:{operation_hash}"
        if confirmation != expected: raise GraphStoreError("human approval confirmation does not match current input")
        if not isinstance(actor_ref, str) or not re.fullmatch(r"[A-Za-z0-9._:@+-]{1,80}", actor_ref): raise GraphStoreError("human approval actor reference invalid")
        invalid = [error for item in evidence for error in validate_evidence(item)]
        if invalid: raise GraphStoreError("invalid evidence: " + "; ".join(invalid))
        if any(item["node_id"] != node_id for item in evidence): raise GraphStoreError("evidence node mismatch")
        supplied = {
            item["evidence_type"] for item in evidence
            if item["classification"] in {"FACT", "VERIFIED_CONSTRAINT"} and item["result"] == "PASS"
        }
        required = set(node["verification"].get("required_evidence_types", []))
        if "human_approval" not in supplied or not required.issubset(supplied): raise GraphStoreError("human approval evidence missing")

        # The pre-approval checkpoint deliberately contains no approval fact.
        # The subsequent RUNNING transition, evidence, approval provenance,
        # usage, terminal state, and post-approval checkpoint commit together;
        # a crash can therefore leave only the explicitly resumable pre-state.
        self.store.checkpoint(graph, "human_approval_before")
        graph = start_node(graph, node_id)
        running = next(item for item in graph["nodes"] if item["node_id"] == node_id)
        measured_usage, usage_entry = self._attempt_usage_entry(graph, running, usage)
        ledger_entries = [
            (
                stable_id("le", {"graph": graph_id, "node": node_id, "attempt": running["attempt"], "status": "RUNNING"}),
                "PROGRESS",
                {"node_id": node_id, "status": "RUNNING", "attempt": running["attempt"]},
                None,
            ),
            *[(item["evidence_id"], "EVIDENCE", item, item["classification"]) for item in evidence],
            (
                stable_id("le", {"graph": graph_id, "node": node_id, "attempt": running["attempt"], "event": "HUMAN_APPROVAL", "actor": actor_ref}),
                "PROGRESS",
                {"event": "HUMAN_APPROVAL", "node_id": node_id, "attempt": running["attempt"], "actor_ref": actor_ref, "input_hash": running["input_hash"], "operation_hash": operation_hash},
                None,
            ),
            usage_entry,
        ]
        violation = self._budget_violation(graph, running, pending_usage=measured_usage)
        if violation:
            failed = fail_node(graph, node_id, violation)
            failed_node = next(item for item in failed["nodes"] if item["node_id"] == node_id)
            ledger_entries.append((
                stable_id("le", {"graph": graph_id, "node": node_id, "attempt": failed_node["attempt"], "status": "FAILED", "reason": violation}),
                "PROGRESS",
                {"node_id": node_id, "status": "FAILED", "attempt": failed_node["attempt"], "reason_code": violation},
                None,
            ))
            self.store.commit_result(failed, "node_failure", ledger_entries)
            raise GraphStoreError(violation)
        approved = accept_node(graph, node_id, {"approved": True, "actor_ref": actor_ref}, [item["evidence_id"] for item in evidence])
        self.store.commit_result(approved, "human_approval_after", ledger_entries)
        return approved

    def record_failure(self, graph_id: str, node_id: str, reason: str, *, usage: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self.status(graph_id)
        node = next((item for item in current["nodes"] if item["node_id"] == node_id), None)
        if node is None: raise GraphStoreError("unknown node")
        if not isinstance(reason, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", reason):
            raise GraphStoreError("failure reason must be a bounded reason code")
        if current["mode"] == "enforce": self._resolve_executor(current, node)
        ledger_entries: list[tuple[str, str, dict[str, Any], str | None]] = []
        if usage is not None:
            measured_usage, usage_entry = self._attempt_usage_entry(current, node, usage)
            ledger_entries.append(usage_entry)
            violation = self._budget_violation(current, node, pending_usage=measured_usage)
            if violation: reason = violation
        failed = fail_node(current, node_id, reason)
        failed_node = next(item for item in failed["nodes"] if item["node_id"] == node_id)
        ledger_entries.append((
            stable_id("le", {"graph": graph_id, "node": node_id, "attempt": failed_node["attempt"], "status": "FAILED", "reason": reason}),
            "PROGRESS",
            {"node_id": node_id, "status": "FAILED", "attempt": failed_node["attempt"], "reason_code": reason},
            None,
        ))
        self.store.commit_result(failed, "node_failure", ledger_entries)
        return failed

    def retry(self, graph_id: str, node_id: str) -> dict[str, Any]:
        current = self.status(graph_id)
        node = next(item for item in current["nodes"] if item["node_id"] == node_id)
        if current["mode"] == "enforce": self._resolve_executor(current, node)
        failed_gates = self.store.failed_gate_count(graph_id, current["graph_revision"], node_id)
        maximum_failed_gates = node["retry_policy"]["max_failed_gates"]
        if maximum_failed_gates is not None and failed_gates > maximum_failed_gates:
            raise GraphStoreError("NODE_CONVERGENCE_FAILED")
        graph = retry_node(current, node_id); self.store.save_revision(graph); return graph

    def add_verified_constraint(self, graph_id: str, constraint: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
        invalid_evidence = [error for item in evidence for error in validate_evidence(item)]
        if invalid_evidence: raise GraphStoreError("invalid evidence: " + "; ".join(invalid_evidence))
        by_id = {item["evidence_id"]: item for item in evidence}
        if len(by_id) != len(evidence): raise GraphStoreError("duplicate evidence id")
        errors = validate_verified_constraint(constraint, by_id)
        if errors: raise GraphStoreError("invalid verified constraint: " + "; ".join(errors))
        current = self.status(graph_id)
        if current["mode"] == "enforce":
            self._require_plan_boundary(current)
            persisted = self.store.evidence_entries(
                graph_id, current["graph_revision"], list(by_id)
            )
            if persisted != by_id:
                raise self._enforce_error(
                    "verified constraints require previously attested Evidence Ledger rows"
                )
        existing = next(
            (item for item in current["constraints"] if item["constraint_id"] == constraint["constraint_id"]),
            None,
        )
        if existing is not None:
            # A caller may retry after its process stopped immediately after a
            # durable commit.  Treat byte-identical input as an idempotent
            # success; a reused id with different content is never silently
            # accepted.
            if existing == constraint:
                return current
            raise GraphStoreError("constraint id conflicts with existing constraint")
        node_ids = {node["node_id"] for node in current["nodes"]}
        if not set(constraint["applies_to"] + constraint["invalidates"]).issubset(node_ids): raise GraphStoreError("constraint references unknown node")
        ledger_entries = (
            [
                (item["evidence_id"], "EVIDENCE", item, item["classification"])
                for item in evidence
            ]
            if current["mode"] != "enforce"
            else []
        )
        graph = add_constraint(current, constraint)
        self.store.commit_constraint(graph, constraint, ledger_entries)
        return graph

    def replan(self, graph_id: str, revised_ir: dict[str, Any], reason: str) -> dict[str, Any]:
        old = self.status(graph_id)
        if not isinstance(reason, str) or not reason.strip(): raise GraphStoreError("replan reason required")
        if any(node["status"] == "RUNNING" for node in old["nodes"]): raise GraphStoreError("replan requires no RUNNING nodes")
        if self.store.graph_has_uncertain_receipt(graph_id): raise GraphStoreError("replan blocked: external receipt needs reconciliation")
        if old["graph_revision"] >= self.config["graph"]["max_graph_revisions"]: raise GraphStoreError("graph revision budget exhausted")
        if revised_ir.get("graph_id") != graph_id or revised_ir.get("graph_revision") != old["graph_revision"] + 1: raise GraphStoreError("replan must provide revision N+1")
        revised_ir = deepcopy(revised_ir)
        if revised_ir.get("status") != "DRAFT": raise GraphStoreError("replan input must be DRAFT")
        old_constraints = {item["constraint_id"] for item in old["constraints"]}
        new_constraints = {item.get("constraint_id") for item in revised_ir.get("constraints", []) if isinstance(item, dict)}
        if not old_constraints.issubset(new_constraints): raise GraphStoreError("replan may not discard verified constraints")
        runtime_keys = {"status", "attempt", "input_hash", "output_hash", "evidence_refs", "gate_result"}
        old_by_id = {node["node_id"]: node for node in old["nodes"]}
        for node in revised_ir.get("nodes", []):
            previous = old_by_id.get(node.get("node_id"))
            if not previous or previous["status"] not in {"ACCEPTED", "FROZEN"}: continue
            if previous["node_type"] == "HUMAN_APPROVAL" or any(tool.startswith("external:") for tool in previous["allowed_tools"]):
                # Approval and side-effect authorization never carry across a
                # graph revision, even when the visible node contract is unchanged.
                continue
            old_contract = {key: value for key, value in previous.items() if key not in runtime_keys}
            new_contract = {key: value for key, value in node.items() if key not in runtime_keys}
            if old_contract == new_contract:
                for key in runtime_keys: node[key] = deepcopy(previous[key])
                node["status"] = "FROZEN"
        self.store.checkpoint(old, "replan_before")
        compiled = compile_graph(revised_ir, lane_registry=self.lane_registry, config=self.config)
        if compiled["ir"]["mode"] == "enforce": self._require_plan_boundary(compiled["ir"])
        revised = deepcopy(compiled["ir"]); revised["status"] = "COMPILED"
        if revised["mode"] == "enforce": revised = mark_ready(revised, self.config["graph"]["max_parallelism"])
        # The N+1 checkpoint is durable before the current-revision pointer is
        # advanced. Either crash ordering therefore has a valid resume point.
        self.store.checkpoint(revised, "replan_after")
        self.store.save_revision(revised, reason=reason)
        self.store.append_ledger(stable_id("le", {"graph": graph_id, "revision": revised["graph_revision"], "kind": "intent"}), graph_id, revised["graph_revision"], "INTENT", revised["contracts"]["intent"])
        return revised

    def resume(self, graph_id: str) -> dict[str, Any]:
        if self.store.graph_has_uncertain_receipt(graph_id): raise GraphStoreError("resume blocked: external receipt needs reconciliation")
        checkpoint = self.store.latest_checkpoint(graph_id)
        graph = checkpoint["payload"].get("ir")
        if not isinstance(graph, dict): raise GraphStoreError("checkpoint missing graph IR")
        if graph.get("graph_id") != graph_id or graph.get("graph_revision") != checkpoint["revision"]:
            raise GraphStoreError("checkpoint graph identity mismatch")
        # Re-run deterministic semantic validation at the trust boundary.
        compile_graph(graph, lane_registry=self.lane_registry, config=self.config)
        if graph["mode"] == "enforce":
            self._require_plan_boundary(graph)
        # Receipts in PREPARED/UNKNOWN are deliberately not retried by this local engine.
        try:
            graph, recovered_send_back = self._recover_pending_send_backs(graph)
        except StateTransitionError as error:
            if str(error) != "NODE_CONVERGENCE_FAILED":
                raise GraphStoreError(str(error)) from error
            graph["status"] = "FAILED"
            self.store.save_and_checkpoint(graph, "node_convergence_failed", reason="resume")
            raise GraphStoreError("NODE_CONVERGENCE_FAILED") from error
        if recovered_send_back:
            # A crash after gate_resolution but before send_back cannot follow
            # the gate-fail branch: the pending bounded transition is completed
            # and checkpointed atomically before this call returns.
            self.store.save_and_checkpoint(graph, "send_back", reason="resume")
        else:
            # Materialize the checkpoint-selected graph before appending an
            # Intent Ledger row.  A replan checkpoint can legitimately exist
            # just before its graph_revisions row; append-before-save would
            # make the integrity verifier reject that recoverable ordering.
            self.store.save_and_checkpoint(graph, "resume_state", reason="resume")
        # Keep the Intent Ledger head inside the final resume checkpoint,
        # including when a crash occurred between compile checkpoint and its
        # first idempotent intent-ledger append.
        self.store.append_ledger(stable_id("le", {"graph": graph_id, "revision": graph["graph_revision"], "kind": "intent"}), graph_id, graph["graph_revision"], "INTENT", graph["contracts"]["intent"])
        self.store.checkpoint(graph, "resume_complete")
        return graph

    def prepare_external_mutation(self, graph_id: str, node_id: str, operation_descriptor: dict[str, Any]) -> dict[str, Any]:
        graph = self.status(graph_id); node = next(item for item in graph["nodes"] if item["node_id"] == node_id)
        if graph["mode"] == "enforce": self._resolve_executor(graph, node)
        if node["status"] != "RUNNING" or not node["input_hash"]: raise GraphStoreError("external mutation must be RUNNING with input hash")
        operation_hash = self._operation_hash(graph, node, operation_descriptor, "external")
        if not self.store.human_approval_exists(graph_id, graph["graph_revision"], operation_hash): raise GraphStoreError("external mutation lacks matching human approval")
        return self.store.prepare_external_mutation(graph_id, graph["graph_revision"], node_id, node["attempt"], node["input_hash"], operation_hash)

    def commit_external_mutation(self, graph_id: str, node_id: str, operation_descriptor: dict[str, Any], result_ref: str) -> dict[str, Any]:
        graph = self.status(graph_id); node = next(item for item in graph["nodes"] if item["node_id"] == node_id)
        if graph["mode"] == "enforce": self._resolve_executor(graph, node)
        if node["status"] != "RUNNING": raise GraphStoreError("external mutation is not authorized for this node")
        operation_hash = self._operation_hash(graph, node, operation_descriptor, "external")
        if not self.store.human_approval_exists(graph_id, graph["graph_revision"], operation_hash): raise GraphStoreError("external mutation lacks matching human approval")
        receipt = self.store.commit_external_mutation(graph_id, graph["graph_revision"], node_id, node["attempt"], node["input_hash"], operation_hash, result_ref)
        self.store.checkpoint(graph, "external_mutation_complete")
        return receipt

    def cancel(self, graph_id: str) -> dict[str, Any]:
        graph = self.status(graph_id)
        if graph["status"] in {"ACCEPTED", "CANCELLED"}: raise GraphStoreError("terminal graph cannot be cancelled")
        graph["status"] = "CANCELLED"
        for node in graph["nodes"]:
            if node["status"] in {"PENDING", "READY", "RUNNING", "INVALIDATED", "BLOCKED"}: node["status"] = "CANCELLED"
        # Cancellation is a material graph transition.  It must be visible to
        # collector/D1 timestamps and must never be represented by a newer
        # state snapshot paired with an older resume checkpoint.
        graph["updated_at"] = utc_now()
        self.store.save_and_checkpoint(graph, "cancelled")
        return graph

    def export(self, graph_id: str) -> dict[str, Any]:
        graph = self.status(graph_id)
        revision = graph["graph_revision"]
        node_usage = {node["node_id"]: self.store.usage_totals(graph_id, revision, node["node_id"]) for node in graph["nodes"]}
        telemetry = privacy_projection(
            graph,
            gate_results=self.store.gate_results(graph_id, revision),
            transition_events=self.store.transition_events(graph_id, revision),
            graph_usage=self.store.usage_totals(graph_id, revision),
            node_usage=node_usage,
            checkpoint_count=self.store.checkpoint_count(graph_id, revision),
            send_back_count=self.store.checkpoint_boundary_count(graph_id, revision, "send_back"),
        )
        return {"graph": canonical_ir(graph), "telemetry": telemetry}
