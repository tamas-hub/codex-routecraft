from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))

from routecraft_graph import GraphEngine as RawGraphEngine, GraphStore, GraphValidationError, doctor_snapshot, privacy_projection, validate_graph
from routecraft_graph.ir import make_graph, make_node
from routecraft_graph.scheduler import critical_path_lengths, ready_nodes
from routecraft_graph.store import GraphStoreError, StoreIntegrityError
from routecraft_graph.migration import migration_preview
from routecraft_graph.policy import PolicyError
from routecraft_graph.canonical import sha256
import routecraft_collector as collector
import routecraft_graph_cli as graph_cli
from tests.graph_test_boundary import TestGraphEngine as GraphEngine

HASH = "sha256:" + "a" * 64


def intent(mutations: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {"request_summary": "bounded fixture", "objectives": ["verify"], "non_goals": [], "constraints": [], "acceptance_criteria": [{"criterion_id": "AC-1", "statement": "global gate passes"}], "risk_level": "low", "external_mutations": mutations or [], "approval_requirements": [], "privacy_boundary": {"local_only": ["source"], "exportable": ["aggregate"]}, "budget": {"max_tokens": None, "max_duration_seconds": None, "max_child_runs": None}, "deadline_if_known": None}


def evidence(node_id: str, kind: str = "schema_result", *, result: str = "PASS") -> dict[str, object]:
    return {"evidence_id": "ev_" + node_id, "classification": "FACT", "evidence_type": kind, "statement": "valid", "source_kind": "local_command", "artifact_hash": HASH, "result": result, "created_at": "2026-08-24T00:00:00Z", "node_id": node_id}


def usage() -> dict[str, int]:
    return {"duration_ms": 1, "input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_tokens": 0, "child_runs": 0}


def graph() -> dict[str, object]:
    first = make_node("n_a", "DETERMINISTIC", "first", output_schema={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]})
    second = make_node("n_b", "DETERMINISTIC", "second", dependencies=["n_a"], input_schema={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]})
    gate = make_node("n_gate", "GATE", "accept", dependencies=["n_b"], input_schema={"type": "object"})
    gate["gate_policy"]["global"] = True
    return make_graph("small_bug_fix", [first, second, gate], [{"from": "n_a", "to": "n_b", "edge_type": "depends_on", "condition": None, "data_contract": {}}, {"from": "n_b", "to": "n_gate", "edge_type": "gate_pass", "condition": None, "data_contract": {}}], intent(), graph_id="g_kernel_test", mode="enforce", now="2026-08-24T00:00:00Z")


def config() -> dict[str, object]:
    # Runtime defaults remain observe.  This isolated kernel fixture explicitly
    # opts into enforce because it exercises scheduler execution paths.
    return {"config_version": 1, "graph": {"mode": "enforce", "max_parallelism": 3, "max_node_attempts": 3, "max_graph_revisions": 3, "state_store": None, "checkpoint": True}, "policy": {"production_policy": "routecraft-production-v1", "allowlisted_task_classes": ["small_bug_fix"]}, "control_center": {"enabled": False}}


def external_graph() -> dict[str, object]:
    approval = make_node("n_approval", "HUMAN_APPROVAL", "approve deploy", allowed_tools=["approve:deploy"])
    approval["verification"] = {"required_evidence_types": ["human_approval"]}
    tool = make_node("n_tool", "TOOL", "deploy", dependencies=["n_approval"], allowed_tools=["external:deploy"])
    gate = make_node("n_gate", "GATE", "accept", dependencies=["n_tool"])
    gate["gate_policy"]["global"] = True
    contract = intent([{"kind": "deploy", "target_scope": "site", "reversible": True}])
    contract["approval_requirements"] = [{"operation": "deploy", "required": True}]
    return make_graph(
        "small_bug_fix",
        [approval, tool, gate],
        [
            {"from": "n_approval", "to": "n_tool", "edge_type": "gate_pass", "condition": None, "data_contract": {}},
            {"from": "n_tool", "to": "n_gate", "edge_type": "gate_pass", "condition": None, "data_contract": {}},
        ],
        contract,
        graph_id="g_external_test",
        mode="enforce",
        now="2026-08-24T00:00:00Z",
    )


def operation() -> dict[str, str]:
    return {"kind": "deploy", "target_scope": "site", "parameters_hash": HASH}


class Graph07CompilerTests(unittest.TestCase):
    def test_default_graph_id_is_unique_per_execution(self) -> None:
        first = graph(); second = graph()
        first.pop("graph_id"); second.pop("graph_id")
        generated_first = make_graph("small_bug_fix", first["nodes"], first["edges"], first["contracts"]["intent"], mode="enforce", now="2026-08-24T00:00:00Z")
        generated_second = make_graph("small_bug_fix", second["nodes"], second["edges"], second["contracts"]["intent"], mode="enforce", now="2026-08-24T00:00:00Z")
        self.assertNotEqual(generated_first["graph_id"], generated_second["graph_id"])

    def invalid(self, change, code: str) -> None:
        value = graph(); change(value)
        with self.assertRaises(GraphValidationError) as caught: validate_graph(value)
        self.assertIn(code, str(caught.exception))

    def test_schema_duplicate_missing_cycle_and_unreachable(self) -> None:
        self.invalid(lambda value: value.__setitem__("bogus", True), "IR_SCHEMA_INVALID")
        self.invalid(lambda value: value["nodes"].append(copy.deepcopy(value["nodes"][0])), "NODE_ID_DUPLICATE")
        self.invalid(lambda value: value["nodes"][1].__setitem__("dependencies", ["gone"]), "DEPENDENCY_MISSING")
        def cycle(value):
            value["nodes"][0]["dependencies"] = ["n_gate"]
            value["edges"].append({"from": "n_gate", "to": "n_a", "edge_type": "depends_on", "condition": None, "data_contract": {}})
        self.invalid(cycle, "DEPENDENCY_CYCLE")
        self.invalid(lambda value: value["nodes"].append(make_node("n_lost", "DETERMINISTIC", "lost")), "NODE_UNREACHABLE")

    def test_contract_capability_lane_budget_and_write_conflict(self) -> None:
        self.invalid(lambda value: value["nodes"][1]["input_schema"]["properties"].__setitem__("value", {"type": "string"}), "DATA_CONTRACT_MISMATCH")
        self.invalid(lambda value: value["nodes"][0].__setitem__("capability_profile", ""), "CAPABILITY_INVALID")
        self.invalid(lambda value: value["nodes"][0].__setitem__("lane", "unknown"), "LANE_INVALID")
        self.invalid(lambda value: value["budgets"].__setitem__("max_tokens", -1), "RESOURCE_BUDGET_INVALID")
        def conflict(value):
            parallel = make_node("n_parallel", "DETERMINISTIC", "parallel", write_scopes=["src"])
            value["nodes"][0]["ownership"]["write_scopes"] = ["src/module"]
            value["nodes"].append(parallel)
            value["edges"].append({"from": "n_parallel", "to": "n_gate", "edge_type": "gate_pass", "condition": None, "data_contract": {}})
            value["nodes"][2]["dependencies"].append("n_parallel")
        self.invalid(conflict, "PARALLEL_WRITE_CONFLICT")

    def test_enforce_policy_version_is_an_exact_opaque_identifier(self) -> None:
        value = graph()
        value["policy_version"] = "routecraft-production-v1.0"
        with self.assertRaises(GraphValidationError) as caught:
            validate_graph(value, config=config())
        self.assertIn("POLICY_VERSION_MISMATCH", str(caught.exception))

    def test_global_gate_and_external_approval_contract(self) -> None:
        self.invalid(lambda value: value["nodes"][2]["gate_policy"].__setitem__("global", False), "GATE_MISSING")
        value = graph(); value["contracts"]["intent"] = intent([{"kind": "deploy", "target_scope": "site", "reversible": True}]); value["nodes"][1]["node_type"] = "TOOL"; value["nodes"][1]["allowed_tools"] = ["external:deploy"]
        with self.assertRaises(GraphValidationError) as caught: validate_graph(value)
        self.assertIn("APPROVAL_REQUIRED", str(caught.exception))
        approval = make_node("n_approval", "HUMAN_APPROVAL", "approve", dependencies=["n_a"], allowed_tools=["approve:deploy"], output_schema={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]})
        approval["verification"] = {"required_evidence_types": ["human_approval"]}
        value["nodes"].append(approval); value["edges"].extend([{"from": "n_a", "to": "n_approval", "edge_type": "depends_on", "condition": None, "data_contract": {}}, {"from": "n_approval", "to": "n_b", "edge_type": "gate_pass", "condition": None, "data_contract": {}}]); value["nodes"][1]["dependencies"].append("n_approval")
        validate_graph(value)

    def test_fixture_and_exact_evidence_contract(self) -> None:
        fixture = json.loads((ROOT / "samples" / "graph-ir-v1-fast-path.json").read_text(encoding="utf-8"))
        validate_graph(fixture)
        bad = evidence("n_a"); bad["artifact_hash"] = "sha256:short"
        with tempfile.TemporaryDirectory() as temp:
            engine = GraphEngine(GraphStore(Path(temp) / "state.sqlite3"), config=config()); engine.plan(graph()); engine.start("g_kernel_test", "n_a")
            with self.assertRaises(GraphStoreError): engine.record_result("g_kernel_test", "n_a", {}, [bad])
        with self.assertRaises(PolicyError): migration_preview({"config_version": 99})

    def test_enforce_requires_a_trusted_boundary_executor_and_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw = RawGraphEngine(GraphStore(Path(temp) / "missing.sqlite3"), config=config())
            with self.assertRaisesRegex(GraphStoreError, "ENFORCE_BOUNDARY_UNAVAILABLE"):
                raw.plan(graph())

        with tempfile.TemporaryDirectory() as temp:
            class NoExecutor:
                boundary_version = 1

                @staticmethod
                def resolve_executor(_graph, _node): return None

            raw = RawGraphEngine(
                GraphStore(Path(temp) / "unresolved.sqlite3"), config=config(), execution_boundary=NoExecutor()
            )
            with self.assertRaisesRegex(GraphStoreError, "executor is unresolved"):
                raw.plan(graph())

        with tempfile.TemporaryDirectory() as temp:
            from tests.graph_test_boundary import FakeTrustedExecutionBoundary

            class OverbroadBoundary(FakeTrustedExecutionBoundary):
                @staticmethod
                def resolve_executor(graph_value, node_value):
                    binding = FakeTrustedExecutionBoundary.resolve_executor(graph_value, node_value)
                    return replace(binding, allowed_tools=("tool:undeclared",))

            raw = RawGraphEngine(
                GraphStore(Path(temp) / "overbroad.sqlite3"), config=config(), execution_boundary=OverbroadBoundary()
            )
            with self.assertRaisesRegex(GraphStoreError, "tool, write, or risk contract"):
                raw.plan(graph())

        with tempfile.TemporaryDirectory() as temp:
            from tests.graph_test_boundary import FakeTrustedExecutionBoundary

            boundary = FakeTrustedExecutionBoundary()
            raw = RawGraphEngine(
                GraphStore(Path(temp) / "attestation.sqlite3"), config=config(), execution_boundary=boundary
            )
            raw.plan(graph())
            raw.start("g_kernel_test", "n_a")
            # A syntactically valid hash is not an attestation issued by the
            # injected boundary and can never accept an enforce node.
            with self.assertRaisesRegex(GraphStoreError, "untrusted result or evidence"):
                raw.record_result(
                    "g_kernel_test", "n_a", {"value": 1}, [evidence("n_a")], usage=usage(),
                    attestation="sha256:" + "c" * 64,
                )
            self.assertEqual("RUNNING", next(node for node in raw.status("g_kernel_test")["nodes"] if node["node_id"] == "n_a")["status"])

    def test_plan_then_policy_or_allowlist_revocation_blocks_ready_and_start(self) -> None:
        from tests.graph_test_boundary import FakeTrustedExecutionBoundary

        for revoked_field in ("production_policy", "allowlisted_task_classes"):
            with self.subTest(revoked_field=revoked_field), tempfile.TemporaryDirectory() as temp:
                active = config()
                state_path = Path(temp) / "state.sqlite3"
                engine = RawGraphEngine(
                    GraphStore(state_path),
                    config=active,
                    execution_boundary=FakeTrustedExecutionBoundary(),
                )
                engine.plan(graph())
                if revoked_field == "production_policy":
                    active["policy"]["production_policy"] = "routecraft-production-v2"
                    expected = "policy_version"
                else:
                    active["policy"]["allowlisted_task_classes"] = []
                    expected = "task_class"
                # The next CLI/API process reads the current policy afresh;
                # a durable graph may not retain authority from its plan-time
                # config object.
                engine = RawGraphEngine(
                    GraphStore(state_path),
                    config=active,
                    execution_boundary=FakeTrustedExecutionBoundary(),
                )
                with self.assertRaisesRegex(GraphStoreError, f"ENFORCE_POLICY_REVOKED: .*{expected}"):
                    engine.ready("g_kernel_test")
                with self.assertRaisesRegex(GraphStoreError, f"ENFORCE_POLICY_REVOKED: .*{expected}"):
                    engine.start("g_kernel_test", "n_a")
                with self.assertRaisesRegex(GraphStoreError, f"ENFORCE_POLICY_REVOKED: .*{expected}"):
                    engine.approve_human(
                        "g_kernel_test", "n_a", "", "owner-local", {}, []
                    )
                node = next(item for item in engine.status("g_kernel_test")["nodes"] if item["node_id"] == "n_a")
                self.assertEqual("READY", node["status"])

    def test_plan_start_then_mode_downgrade_rejects_result(self) -> None:
        """A live enforce-mode downgrade revokes an already claimed attempt."""
        from tests.graph_test_boundary import FakeTrustedExecutionBoundary

        with tempfile.TemporaryDirectory() as temp:
            active = config()
            state_path = Path(temp) / "state.sqlite3"
            engine = GraphEngine(
                GraphStore(state_path),
                config=active,
                execution_boundary=FakeTrustedExecutionBoundary(),
            )
            engine.plan(graph())
            # Simulate a newer config snapshot being applied while the host
            # the previously claimed result under the downgraded authority.
            active["graph"]["mode"] = "observe"
            with self.assertRaisesRegex(
                GraphStoreError,
                "ENFORCE_POLICY_REVOKED: .*configured graph mode is no longer enforce",
            ):
                engine.ready("g_kernel_test")
            with self.assertRaisesRegex(
                GraphStoreError,
                "ENFORCE_POLICY_REVOKED: .*configured graph mode is no longer enforce",
            ):
                engine.start("g_kernel_test", "n_a")

            # Restore the authority only to create a real claimed attempt;
            # then downgrade again before result acceptance.
            active["graph"]["mode"] = "enforce"
            engine.start("g_kernel_test", "n_a")
            active["graph"]["mode"] = "observe"
            with self.assertRaisesRegex(
                GraphStoreError,
                "ENFORCE_POLICY_REVOKED: .*configured graph mode is no longer enforce",
            ):
                engine.record_result(
                    "g_kernel_test",
                    "n_a",
                    {"value": 1},
                    [evidence("n_a")],
                    usage=usage(),
                )

            node = next(item for item in engine.status("g_kernel_test")["nodes"] if item["node_id"] == "n_a")
            self.assertEqual("RUNNING", node["status"])

    def test_cli_rejects_raw_result_evidence_for_an_existing_enforce_graph(self) -> None:
        from routecraft_local.errors import RouteCraftLocalError
        from tests.graph_test_boundary import FakeTrustedExecutionBoundary

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            configuration = config()
            config_path = base / "config.json"
            config_path.write_text(json.dumps(configuration), encoding="utf-8")
            store_path = base / "state.sqlite3"
            trusted = RawGraphEngine(
                GraphStore(store_path), config=configuration, execution_boundary=FakeTrustedExecutionBoundary()
            )
            trusted.plan(graph())
            trusted.start("g_kernel_test", "n_a")
            result_path = base / "result.json"; result_path.write_text('{"value": 1}', encoding="utf-8")
            evidence_path = base / "evidence.json"; evidence_path.write_text(json.dumps([evidence("n_a")]), encoding="utf-8")
            with self.assertRaisesRegex(RouteCraftLocalError, "ENFORCE_BOUNDARY_UNAVAILABLE"):
                graph_cli.run(
                    "g_kernel_test", config_path=config_path, store_path=store_path, data_dir=None,
                    node_id="n_a", result_path=result_path, evidence_path=evidence_path,
                    usage_path=None, gate_result="PASS", failure=None, retry=False,
                )
            with self.assertRaisesRegex(RouteCraftLocalError, "ENFORCE_BOUNDARY_UNAVAILABLE"):
                graph_cli.run(
                    "g_kernel_test", config_path=config_path, store_path=store_path, data_dir=None,
                    node_id="n_a", result_path=None, evidence_path=None,
                    usage_path=None, gate_result="PASS", failure="TOOL_FAILURE", retry=False,
                )
            with self.assertRaisesRegex(RouteCraftLocalError, "ENFORCE_BOUNDARY_UNAVAILABLE"):
                graph_cli.run(
                    "g_kernel_test", config_path=config_path, store_path=store_path, data_dir=None,
                    node_id="n_a", result_path=None, evidence_path=None,
                    usage_path=None, gate_result="PASS", failure=None, retry=True,
                )
            with self.assertRaisesRegex(RouteCraftLocalError, "ENFORCE_BOUNDARY_UNAVAILABLE"):
                graph_cli.resume(
                    "g_kernel_test", config_path=config_path, store_path=store_path, data_dir=None,
                )


class Graph07SchedulerStateTests(unittest.TestCase):
    def test_sequence_fanout_fanin_critical_parallel_and_blocked(self) -> None:
        value = graph(); fan = make_node("n_fan", "DETERMINISTIC", "fan", dependencies=["n_a"])
        value["nodes"].append(fan); value["nodes"][2]["dependencies"].append("n_fan"); value["edges"].extend([{"from": "n_a", "to": "n_fan", "edge_type": "fan_out", "condition": None, "data_contract": {}}, {"from": "n_fan", "to": "n_gate", "edge_type": "merge", "condition": None, "data_contract": {}}])
        validate_graph(value)
        self.assertEqual(["n_a"], ready_nodes(value, max_parallelism=3))
        self.assertGreater(critical_path_lengths(value)["n_a"], critical_path_lengths(value)["n_gate"])
        value["nodes"][0]["status"] = "ACCEPTED"
        self.assertEqual(["n_b", "n_fan"], ready_nodes(value, max_parallelism=3))
        value["nodes"][3]["ownership"]["write_scopes"] = ["work"]; value["nodes"][1]["ownership"]["write_scopes"] = ["work"]
        self.assertEqual(["n_b"], ready_nodes(value, max_parallelism=3))

    def test_selective_retry_hash_invalidation_and_global_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            engine = GraphEngine(GraphStore(Path(temp) / "state.sqlite3"), config=config()); engine.plan(graph())
            for node_id, output in (("n_a", {"value": 1}), ("n_b", {"value": 2}), ("n_gate", {})):
                engine.start("g_kernel_test", node_id); state = engine.record_result("g_kernel_test", node_id, output, [evidence(node_id)], usage=usage())
            self.assertEqual("ACCEPTED", state["status"])
            engine.record_failure("g_kernel_test", "n_a", "gate")
            state = engine.status("g_kernel_test"); statuses = {node["node_id"]: node["status"] for node in state["nodes"]}
            self.assertEqual("FAILED", statuses["n_a"]); self.assertEqual("INVALIDATED", statuses["n_b"])
            self.assertIsNotNone(next(node for node in state["nodes"] if node["node_id"] == "n_a")["input_hash"])
            with self.assertRaisesRegex(Exception, "NODE_CONVERGENCE_FAILED"):
                engine.retry("g_kernel_test", "n_a")

    def test_gate_inconclusive_and_retry_convergence_are_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            engine = GraphEngine(GraphStore(Path(temp) / "state.sqlite3"), config=config()); engine.plan(graph()); engine.start("g_kernel_test", "n_a")
            failed = engine.record_result("g_kernel_test", "n_a", {"value": 0}, [evidence("n_a", result="INCONCLUSIVE")], gate_result="INCONCLUSIVE", usage=usage())
            self.assertEqual("FAILED", next(node for node in failed["nodes"] if node["node_id"] == "n_a")["status"])
            with self.assertRaises(Exception): engine.record_result("g_kernel_test", "n_a", {}, [evidence("n_a")])


class Graph07StoreTests(unittest.TestCase):
    def test_checkpoint_payload_chain_schema_and_readonly_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"; store = GraphStore(path); engine = GraphEngine(store, config=config()); engine.plan(graph())
            self.assertEqual("OK", doctor_snapshot(store_path=path)["state_store"])
            con = sqlite3.connect(path)
            try: con.execute("UPDATE checkpoints SET payload_hash=?", (HASH,)); con.commit()
            finally: con.close()
            with self.assertRaises(StoreIntegrityError): store.verify_integrity()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "truncated-tail.sqlite3"
            store = GraphStore(path)
            engine = GraphEngine(store, config=config())
            engine.plan(graph())
            con = sqlite3.connect(path)
            try:
                con.execute(
                    "DELETE FROM checkpoints WHERE graph_id=? AND revision=? AND sequence=(SELECT MAX(sequence) FROM checkpoints WHERE graph_id=? AND revision=?)",
                    ("g_kernel_test", 1, "g_kernel_test", 1),
                )
                con.commit()
            finally:
                con.close()
            with self.assertRaisesRegex(StoreIntegrityError, "durable anchor"):
                store.verify_integrity()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "boundary.sqlite3"; store = GraphStore(path); engine = GraphEngine(store, config=config()); engine.plan(graph())
            con = sqlite3.connect(path)
            try: con.execute("UPDATE checkpoints SET boundary='send_back'"); con.commit()
            finally: con.close()
            with self.assertRaises(StoreIntegrityError): store.verify_integrity()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite3"; con = sqlite3.connect(path); con.execute("CREATE TABLE legacy(value TEXT)"); con.close()
            with self.assertRaises(StoreIntegrityError): GraphStore(path)

    def test_resume_idempotency_ledgers_outcome_policy_and_replan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = GraphStore(Path(temp) / "state.sqlite3"); engine = GraphEngine(store, config=config()); planned = engine.plan(external_graph())
            approval = next(node for node in planned["nodes"] if node["node_id"] == "n_approval")
            with self.assertRaises(GraphStoreError): engine.start("g_external_test", "n_approval")
            descriptor = operation()
            operation_hash = sha256({"graph_revision": 1, "descriptor": descriptor})
            engine.approve_human(
                "g_external_test", "n_approval", f"g_external_test:n_approval:{approval['input_hash']}:{operation_hash}", "owner-local",
                descriptor, [evidence("n_approval", "human_approval")], usage=usage(),
            )
            engine.start("g_external_test", "n_tool")
            receipt = engine.prepare_external_mutation("g_external_test", "n_tool", descriptor)
            self.assertEqual("PREPARED", receipt["status"])
            with self.assertRaises(GraphStoreError): engine.resume("g_external_test")
            engine.commit_external_mutation("g_external_test", "n_tool", descriptor, "artifact-ref")
            self.assertEqual("COMMITTED", engine.prepare_external_mutation("g_external_test", "n_tool", descriptor)["status"])
            engine.record_result("g_external_test", "n_tool", {}, [evidence("n_tool")], usage=usage())
            store.save_outcome("outcome-1", "g_external_test", {"task_class": "small_bug_fix", "event_classification": "normal", "duration": None})
            store.save_policy_candidate({"policy_id": "pc-1", "base_policy": "p", "candidate_change": "shadow", "evidence": [], "sample_size": 0, "confidence": "low", "expected_benefit": None, "known_risk": "unknown", "status": "DRAFT"})
            revised = external_graph(); revised["graph_revision"] = 2; revised["updated_at"] = "2026-08-24T00:00:01Z"
            replanned = engine.replan("g_external_test", revised, "hidden dependency")
            self.assertEqual(2, replanned["graph_revision"])
            # External side-effect nodes are deliberately never frozen across
            # a revision: approval and idempotency binding must be renewed.
            self.assertEqual("PENDING", next(node for node in replanned["nodes"] if node["node_id"] == "n_tool")["status"])

    def test_store_separation_and_privacy_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(GraphStoreError): GraphStore(root / "memory" / "x.sqlite3", forbidden_roots=[root / "memory"])
        projection = privacy_projection(graph())
        rendered = json.dumps(projection, ensure_ascii=False).lower()
        for forbidden in ("objective", "source_code", "workstream", "request_summary", "raw_node_output"): self.assertNotIn(forbidden, rendered)
        for family in ("graph_runs", "graph_node_metrics", "graph_events"):
            self.assertTrue(all(collector._valid_family(family, row) for row in projection[family]))
