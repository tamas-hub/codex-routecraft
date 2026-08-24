from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))

from routecraft_graph import GraphStore, GraphValidationError, compile_graph
from routecraft_graph.ir import make_graph, make_node
from routecraft_graph.scheduler import ready_nodes
from routecraft_graph.store import GraphStoreError, StoreIntegrityError
from routecraft_graph.canonical import sha256
from tests.graph_test_boundary import TestGraphEngine as GraphEngine


HASH = "sha256:" + "b" * 64


def intent(mutations: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "request_summary": "durable execution regression fixture",
        "objectives": ["verify durable graph behavior"],
        "non_goals": [],
        "constraints": [],
        "acceptance_criteria": [
            {"criterion_id": "AC-DURABLE-1", "statement": "global gate passes"}
        ],
        "risk_level": "low",
        "external_mutations": mutations or [],
        "approval_requirements": [],
        "privacy_boundary": {
            "local_only": ["source"],
            "exportable": ["aggregate"],
        },
        "budget": {
            "max_tokens": None,
            "max_duration_seconds": None,
            "max_child_runs": None,
        },
        "deadline_if_known": None,
    }


def evidence(
    node_id: str,
    *,
    evidence_id: str | None = None,
    kind: str = "schema_result",
    classification: str = "FACT",
    result: str = "PASS",
) -> dict[str, object]:
    return {
        "evidence_id": evidence_id or "ev_" + node_id,
        "classification": classification,
        "evidence_type": kind,
        "statement": "verified by the local regression fixture",
        "source_kind": "local_command",
        "artifact_hash": HASH,
        "result": result,
        "created_at": "2026-08-24T00:00:00Z",
        "node_id": node_id,
    }


def usage(**overrides: int | None) -> dict[str, int | None]:
    measured: dict[str, int | None] = {
        "duration_ms": 1,
        "input_tokens": 2,
        "cached_input_tokens": 0,
        "output_tokens": 1,
        "reasoning_tokens": 0,
        "child_runs": 0,
    }
    measured.update(overrides)
    return measured


def config(*, max_parallelism: int = 3) -> dict[str, object]:
    return {
        "config_version": 1,
        "graph": {
            # The production default remains observe.  These execution tests
            # explicitly opt their isolated fixture config into enforce so the
            # compiler's mode ceiling cannot be bypassed implicitly.
            "mode": "enforce",
            "max_parallelism": max_parallelism,
            "max_node_attempts": 3,
            "max_graph_revisions": 3,
            "state_store": None,
            "checkpoint": True,
        },
        "policy": {
            "production_policy": "routecraft-production-v1",
            "allowlisted_task_classes": ["small_bug_fix"],
        },
        "control_center": {"enabled": False},
    }


def linear_graph(
    *,
    graph_id: str = "g_durable_linear",
    mode: str = "enforce",
    retry_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    first = make_node(
        "n_a",
        "DETERMINISTIC",
        "produce a typed value",
        output_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
        retry_policy=retry_policy,
    )
    second = make_node(
        "n_b",
        "DETERMINISTIC",
        "consume the typed value",
        dependencies=["n_a"],
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
    )
    gate = make_node("n_gate", "GATE", "accept", dependencies=["n_b"])
    gate["gate_policy"]["global"] = True
    return make_graph(
        "small_bug_fix",
        [first, second, gate],
        [
            {
                "from": "n_a",
                "to": "n_b",
                "edge_type": "sequence",
                "condition": None,
                "data_contract": {},
            },
            {
                "from": "n_b",
                "to": "n_gate",
                "edge_type": "gate_pass",
                "condition": None,
                "data_contract": {},
            },
        ],
        intent(),
        graph_id=graph_id,
        mode=mode,
        now="2026-08-24T00:00:00Z",
    )


def branch_graph(*, graph_id: str = "g_durable_branches") -> dict[str, object]:
    left = make_node("n_left", "DETERMINISTIC", "left root")
    left_child = make_node(
        "n_left_child", "DETERMINISTIC", "left child", dependencies=["n_left"]
    )
    right = make_node("n_right", "DETERMINISTIC", "independent root")
    gate = make_node(
        "n_gate",
        "GATE",
        "accept both branches",
        dependencies=["n_left_child", "n_right"],
    )
    gate["gate_policy"]["global"] = True
    return make_graph(
        "small_bug_fix",
        [left, left_child, right, gate],
        [
            {
                "from": "n_left",
                "to": "n_left_child",
                "edge_type": "sequence",
                "condition": None,
                "data_contract": {},
            },
            {
                "from": "n_left_child",
                "to": "n_gate",
                "edge_type": "merge",
                "condition": None,
                "data_contract": {},
            },
            {
                "from": "n_right",
                "to": "n_gate",
                "edge_type": "merge",
                "condition": None,
                "data_contract": {},
            },
        ],
        intent(),
        graph_id=graph_id,
        mode="enforce",
        now="2026-08-24T00:00:00Z",
    )


def wide_graph() -> dict[str, object]:
    roots = [
        make_node(node_id, "DETERMINISTIC", f"parallel root {node_id}")
        for node_id in ("n_a", "n_b", "n_c")
    ]
    gate = make_node(
        "n_gate", "GATE", "accept all roots", dependencies=[node["node_id"] for node in roots]
    )
    gate["gate_policy"]["global"] = True
    edges = [
        {
            "from": node["node_id"],
            "to": "n_gate",
            "edge_type": "merge",
            "condition": None,
            "data_contract": {},
        }
        for node in roots
    ]
    return make_graph(
        "small_bug_fix",
        roots + [gate],
        edges,
        intent(),
        graph_id="g_durable_wide",
        mode="enforce",
        now="2026-08-24T00:00:00Z",
    )


def external_graph() -> dict[str, object]:
    approval = make_node(
        "n_approval",
        "HUMAN_APPROVAL",
        "approve deployment fixture",
        allowed_tools=["approve:deploy"],
    )
    approval["verification"] = {"required_evidence_types": ["human_approval"]}
    tool = make_node(
        "n_tool",
        "TOOL",
        "perform deployment fixture",
        dependencies=["n_approval"],
        allowed_tools=["external:deploy"],
    )
    gate = make_node("n_gate", "GATE", "accept", dependencies=["n_tool"])
    gate["gate_policy"]["global"] = True
    contract = intent(
        [{"kind": "deploy", "target_scope": "test fixture", "reversible": True}]
    )
    contract["approval_requirements"] = [{"operation": "deploy", "required": True}]
    return make_graph(
        "small_bug_fix",
        [approval, tool, gate],
        [
            {
                "from": "n_approval",
                "to": "n_tool",
                "edge_type": "gate_pass",
                "condition": None,
                "data_contract": {},
            },
            {
                "from": "n_tool",
                "to": "n_gate",
                "edge_type": "gate_pass",
                "condition": None,
                "data_contract": {},
            },
        ],
        contract,
        graph_id="g_durable_external",
        mode="enforce",
        now="2026-08-24T00:00:00Z",
    )


def operation() -> dict[str, str]:
    return {"kind": "deploy", "target_scope": "test fixture", "parameters_hash": HASH}


def statuses(graph: dict[str, object]) -> dict[str, str]:
    return {node["node_id"]: node["status"] for node in graph["nodes"]}


class Graph07ExecutionBoundaryTests(unittest.TestCase):
    def test_observe_and_off_modes_refuse_scheduler_start(self) -> None:
        for mode in ("observe", "off"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                value = linear_graph(graph_id=f"g_durable_{mode}", mode=mode)
                engine = GraphEngine(
                    GraphStore(Path(temp) / "state.sqlite3"), config=config()
                )
                planned = engine.plan(value)
                self.assertEqual("PENDING", statuses(planned)["n_a"])
                self.assertEqual([], engine.ready(value["graph_id"]))
                with self.assertRaisesRegex(
                    GraphStoreError, "scheduler execution requires configured enforce mode"
                ):
                    engine.start(value["graph_id"], "n_a")
                self.assertEqual("PENDING", statuses(engine.status(value["graph_id"]))["n_a"])

    def test_running_nodes_consume_max_parallelism_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            engine = GraphEngine(
                GraphStore(Path(temp) / "state.sqlite3"),
                config=config(max_parallelism=2),
            )
            planned = engine.plan(wide_graph())
            self.assertEqual(["n_a", "n_b"], engine.ready(planned["graph_id"]))

            engine.start(planned["graph_id"], "n_a")
            self.assertEqual(["n_b"], engine.ready(planned["graph_id"]))
            engine.start(planned["graph_id"], "n_b")
            self.assertEqual([], engine.ready(planned["graph_id"]))
            self.assertEqual(
                2,
                sum(
                    node["status"] == "RUNNING"
                    for node in engine.status(planned["graph_id"])["nodes"]
                ),
            )

    def test_compile_and_scheduler_orders_are_dependency_stable(self) -> None:
        value = linear_graph()
        value["nodes"][0]["node_id"] = "n_z_root"
        value["nodes"][1]["dependencies"] = ["n_z_root"]
        value["edges"][0]["from"] = "n_z_root"
        compiled = compile_graph(value, config=config())
        self.assertEqual(
            ["n_z_root", "n_b", "n_gate"], compiled["node_order"]
        )

        branched = branch_graph()
        # n_left has the longer remaining path, so it wins even though n_right
        # and the canonical node list are independently sorted.
        self.assertEqual(["n_left"], ready_nodes(branched, max_parallelism=1))
        self.assertEqual(["n_left"], ready_nodes(copy.deepcopy(branched), max_parallelism=1))


class Graph07DurableStoreTests(unittest.TestCase):
    def test_constraint_commit_is_atomic_idempotent_and_advances_timestamp(self) -> None:
        """A constraint never outlives its evidence, state, or checkpoint."""
        constraint = {
            "constraint_id": "constraint_atomic_interface",
            "scope": "interface",
            "statement": "the local fixture interface is fixed",
            "evidence_refs": ["ev_n_a"],
            "confidence": "high",
            "applies_to": ["n_b"],
            "invalidates": [],
            "created_by": "n_a",
        }

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "constraint-before.sqlite3"
            store = GraphStore(path)
            engine = GraphEngine(store, config=config())
            planned = engine.plan(linear_graph(graph_id="g_constraint_before", mode="observe"))
            original_checkpoint = store._checkpoint_in

            def crash_before_constraint_checkpoint(*args, **kwargs):
                raise RuntimeError("simulated crash before constraint checkpoint")

            store._checkpoint_in = crash_before_constraint_checkpoint
            with self.assertRaisesRegex(RuntimeError, "before constraint checkpoint"):
                engine.add_verified_constraint(
                    "g_constraint_before", constraint, [evidence("n_a")]
                )
            store._checkpoint_in = original_checkpoint

            reopened = GraphStore(path)
            restored = reopened.load_revision("g_constraint_before")
            self.assertEqual([], restored["constraints"])
            self.assertEqual(planned["updated_at"], restored["updated_at"])
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM constraints WHERE graph_id=?",
                        ("g_constraint_before",),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM ledger_entries WHERE graph_id=? AND ledger_kind='EVIDENCE'",
                        ("g_constraint_before",),
                    ).fetchone()[0],
                )
            finally:
                connection.close()
            applied = engine.add_verified_constraint(
                "g_constraint_before", constraint, [evidence("n_a")]
            )
            self.assertGreater(applied["updated_at"], planned["updated_at"])
            self.assertEqual([constraint], applied["constraints"])

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "constraint-after.sqlite3"
            store = GraphStore(path)
            engine = GraphEngine(store, config=config())
            engine.plan(linear_graph(graph_id="g_constraint_after", mode="observe"))
            original_commit = store.commit_constraint

            def crash_after_constraint_commit(*args, **kwargs):
                original_commit(*args, **kwargs)
                raise RuntimeError("simulated crash after constraint commit")

            store.commit_constraint = crash_after_constraint_commit
            with self.assertRaisesRegex(RuntimeError, "after constraint commit"):
                engine.add_verified_constraint(
                    "g_constraint_after", constraint, [evidence("n_a")]
                )

            recovered = GraphEngine(GraphStore(path), config=config()).resume(
                "g_constraint_after"
            )
            self.assertEqual([constraint], recovered["constraints"])
            durable = GraphStore(path)
            count_before_retry = durable.checkpoint_count("g_constraint_after", 1)
            # Retry after the caller-side crash is intentionally idempotent;
            # it neither loses the durable fact nor trips SQLite UNIQUE.
            repeated = GraphEngine(durable, config=config()).add_verified_constraint(
                "g_constraint_after", constraint, [evidence("n_a")]
            )
            self.assertEqual(recovered, repeated)
            self.assertEqual(
                count_before_retry,
                GraphStore(path).checkpoint_count("g_constraint_after", 1),
            )
            with self.assertRaisesRegex(GraphStoreError, "constraint id conflicts"):
                GraphEngine(GraphStore(path), config=config()).add_verified_constraint(
                    "g_constraint_after",
                    {**constraint, "statement": "a conflicting statement"},
                    [evidence("n_a")],
                )
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM constraints WHERE graph_id=?",
                        ("g_constraint_after",),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM ledger_entries WHERE graph_id=? AND ledger_kind='EVIDENCE'",
                        ("g_constraint_after",),
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_cancel_commit_is_atomic_and_advances_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cancel-before.sqlite3"
            store = GraphStore(path)
            engine = GraphEngine(store, config=config())
            planned = engine.plan(linear_graph(graph_id="g_cancel_before", mode="observe"))
            original_checkpoint = store._checkpoint_in

            def crash_before_cancel_checkpoint(*args, **kwargs):
                raise RuntimeError("simulated crash before cancel checkpoint")

            store._checkpoint_in = crash_before_cancel_checkpoint
            with self.assertRaisesRegex(RuntimeError, "before cancel checkpoint"):
                engine.cancel("g_cancel_before")
            store._checkpoint_in = original_checkpoint
            restored = GraphStore(path).load_revision("g_cancel_before")
            self.assertEqual("COMPILED", restored["status"])
            self.assertEqual(planned["updated_at"], restored["updated_at"])

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cancel-after.sqlite3"
            store = GraphStore(path)
            engine = GraphEngine(store, config=config())
            planned = engine.plan(linear_graph(graph_id="g_cancel_after", mode="observe"))
            original_commit = store.save_and_checkpoint

            def crash_after_cancel_commit(*args, **kwargs):
                original_commit(*args, **kwargs)
                raise RuntimeError("simulated crash after cancel commit")

            store.save_and_checkpoint = crash_after_cancel_commit
            with self.assertRaisesRegex(RuntimeError, "after cancel commit"):
                engine.cancel("g_cancel_after")

            recovered = GraphEngine(GraphStore(path), config=config()).resume(
                "g_cancel_after"
            )
            self.assertEqual("CANCELLED", recovered["status"])
            self.assertTrue(
                all(node["status"] == "CANCELLED" for node in recovered["nodes"])
            )
            self.assertGreater(recovered["updated_at"], planned["updated_at"])
            with self.assertRaisesRegex(GraphStoreError, "terminal graph"):
                GraphEngine(GraphStore(path), config=config()).cancel("g_cancel_after")

    def test_result_commit_is_atomic_before_and_after_process_crash(self) -> None:
        """Evidence, usage, state, and checkpoint form one result boundary."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "before-commit.sqlite3"
            store = GraphStore(path)
            engine = GraphEngine(store, config=config())
            engine.plan(linear_graph(graph_id="g_result_before"))
            engine.start("g_result_before", "n_a")
            original_checkpoint = store._checkpoint_in

            def crash_before_checkpoint(*args, **kwargs):
                raise RuntimeError("simulated crash before result checkpoint")

            store._checkpoint_in = crash_before_checkpoint
            with self.assertRaisesRegex(RuntimeError, "before result checkpoint"):
                engine.record_result(
                    "g_result_before", "n_a", {"value": 1}, [evidence("n_a")],
                    usage=usage(input_tokens=2),
                )
            store._checkpoint_in = original_checkpoint

            reopened = GraphStore(path)
            self.assertEqual("RUNNING", statuses(reopened.load_revision("g_result_before"))["n_a"])
            self.assertEqual(0, reopened.usage_totals("g_result_before", 1, "n_a")["attempts_with_usage"])
            connection = sqlite3.connect(path)
            try:
                evidence_rows = connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries WHERE graph_id=? AND ledger_kind='EVIDENCE'",
                    ("g_result_before",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(0, evidence_rows)

            # The same claimed attempt may safely be completed after restart
            # with corrected measurements: no stale usage ledger collides.
            replacement = usage(input_tokens=7)
            accepted = engine.record_result(
                "g_result_before", "n_a", {"value": 1}, [evidence("n_a")],
                usage=replacement,
            )
            self.assertEqual("ACCEPTED", statuses(accepted)["n_a"])
            self.assertEqual(8, GraphStore(path).usage_totals("g_result_before", 1, "n_a")["total_tokens"])

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "after-commit.sqlite3"
            store = GraphStore(path)
            engine = GraphEngine(store, config=config())
            engine.plan(linear_graph(graph_id="g_result_after"))
            engine.start("g_result_after", "n_a")
            original_commit = store.commit_result

            def crash_after_commit(ir, boundary, ledger_entries, **kwargs):
                result = original_commit(ir, boundary, ledger_entries, **kwargs)
                if boundary == "node_acceptance":
                    raise RuntimeError("simulated crash after result commit")
                return result

            store.commit_result = crash_after_commit
            measured = usage(input_tokens=3)
            with self.assertRaisesRegex(RuntimeError, "after result commit"):
                engine.record_result(
                    "g_result_after", "n_a", {"value": 1}, [evidence("n_a")],
                    usage=measured,
                )

            resumed = GraphEngine(GraphStore(path), config=config()).resume("g_result_after")
            self.assertEqual("ACCEPTED", statuses(resumed)["n_a"])
            durable = GraphStore(path)
            self.assertEqual(4, durable.usage_totals("g_result_after", 1, "n_a")["total_tokens"])
            # A caller that did not receive the first return value cannot
            # overwrite the immutable attempt facts with a different usage.
            with self.assertRaisesRegex(Exception, "attempt usage requires RUNNING node"):
                engine.record_result(
                    "g_result_after", "n_a", {"value": 1}, [evidence("n_a")],
                    usage=usage(input_tokens=99),
                )
            self.assertEqual(4, GraphStore(path).usage_totals("g_result_after", 1, "n_a")["total_tokens"])

    def test_human_approval_and_record_failure_share_the_result_commit_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "approval.sqlite3"
            store = GraphStore(path)
            engine = GraphEngine(store, config=config())
            planned = engine.plan(external_graph())
            approval = next(node for node in planned["nodes"] if node["node_id"] == "n_approval")
            descriptor = operation()
            operation_hash = sha256({"graph_revision": 1, "descriptor": descriptor})
            confirmation = f"g_durable_external:n_approval:{approval['input_hash']}:{operation_hash}"
            original_checkpoint = store._checkpoint_in

            def crash_approval_checkpoint(con, ir, boundary, *args, **kwargs):
                if boundary == "human_approval_after":
                    raise RuntimeError("simulated crash before human approval commit")
                return original_checkpoint(con, ir, boundary, *args, **kwargs)

            store._checkpoint_in = crash_approval_checkpoint
            with self.assertRaisesRegex(RuntimeError, "before human approval commit"):
                engine.approve_human(
                    "g_durable_external", "n_approval", confirmation, "owner-local", descriptor,
                    [evidence("n_approval", kind="human_approval")], usage=usage(),
                )
            store._checkpoint_in = original_checkpoint
            reopened = GraphStore(path)
            self.assertEqual("READY", statuses(reopened.load_revision("g_durable_external"))["n_approval"])
            connection = sqlite3.connect(path)
            try:
                approval_rows = connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries WHERE graph_id=? AND ledger_kind='EVIDENCE'",
                    ("g_durable_external",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(0, approval_rows)
            approved = engine.approve_human(
                "g_durable_external", "n_approval", confirmation, "owner-local", descriptor,
                [evidence("n_approval", kind="human_approval")], usage=usage(input_tokens=7),
            )
            self.assertEqual("ACCEPTED", statuses(approved)["n_approval"])
            self.assertEqual(8, GraphStore(path).usage_totals("g_durable_external", 1, "n_approval")["total_tokens"])

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "failure.sqlite3"
            store = GraphStore(path)
            engine = GraphEngine(store, config=config())
            engine.plan(linear_graph(graph_id="g_failure_atomic"))
            engine.start("g_failure_atomic", "n_a")
            original_checkpoint = store._checkpoint_in

            def crash_failure_checkpoint(con, ir, boundary, *args, **kwargs):
                if boundary == "node_failure":
                    raise RuntimeError("simulated crash before failure commit")
                return original_checkpoint(con, ir, boundary, *args, **kwargs)

            store._checkpoint_in = crash_failure_checkpoint
            with self.assertRaisesRegex(RuntimeError, "before failure commit"):
                engine.record_failure("g_failure_atomic", "n_a", "TOOL_FAILURE", usage=usage())
            store._checkpoint_in = original_checkpoint
            reopened = GraphStore(path)
            self.assertEqual("RUNNING", statuses(reopened.load_revision("g_failure_atomic"))["n_a"])
            self.assertEqual(0, reopened.usage_totals("g_failure_atomic", 1, "n_a")["attempts_with_usage"])
            failed = engine.record_failure(
                "g_failure_atomic", "n_a", "TOOL_FAILURE", usage=usage(input_tokens=6),
            )
            self.assertEqual("FAILED", statuses(failed)["n_a"])
            self.assertEqual(7, GraphStore(path).usage_totals("g_failure_atomic", 1, "n_a")["total_tokens"])

    def test_new_engine_resumes_last_checkpoint_after_process_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            first_process = GraphEngine(GraphStore(path), config=config())
            first_process.plan(linear_graph())
            first_process.start("g_durable_linear", "n_a")
            first_process.record_result(
                "g_durable_linear",
                "n_a",
                {"value": 1},
                [evidence("n_a")],
                usage=usage(),
            )
            first_process.start("g_durable_linear", "n_b")
            self.assertEqual(
                "RUNNING", statuses(first_process.status("g_durable_linear"))["n_b"]
            )

            # A new store/engine instance represents a process restart.  The
            # uncheckpointed RUNNING transition must not be treated as done.
            resumed_process = GraphEngine(GraphStore(path), config=config())
            resumed = resumed_process.resume("g_durable_linear")
            self.assertEqual("ACCEPTED", statuses(resumed)["n_a"])
            self.assertEqual("READY", statuses(resumed)["n_b"])
            self.assertNotIn("RUNNING", statuses(resumed).values())
            resumed_b = next(node for node in resumed["nodes"] if node["node_id"] == "n_b")
            self.assertEqual(0, resumed_b["attempt"])
            restarted = resumed_process.start("g_durable_linear", "n_b")
            restarted_b = next(
                node for node in restarted["nodes"] if node["node_id"] == "n_b"
            )
            self.assertEqual(1, restarted_b["attempt"])

    def test_corrupt_checkpoint_payload_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            engine = GraphEngine(GraphStore(path), config=config())
            engine.plan(linear_graph())
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE checkpoints SET payload_json = ? WHERE sequence = 1",
                    ('{"ir":{"tampered":true}}',),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(StoreIntegrityError, "checkpoint payload"):
                GraphStore(path)

    def test_corrupt_or_rehashed_state_snapshot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            engine = GraphEngine(GraphStore(path), config=config())
            engine.plan(linear_graph())
            connection = sqlite3.connect(path)
            try:
                row = connection.execute(
                    "SELECT sequence,payload_json,previous_hash FROM graph_events "
                    "WHERE event_type='STATE_SNAPSHOT' ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                payload = json.loads(row[1])
                payload["nodes"][0]["status"] = "ACCEPTED"
                encoded = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                digest = sha256(payload)
                chain = sha256(
                    {
                        "event_type": "STATE_SNAPSHOT",
                        "previous_hash": row[2],
                        "payload_hash": digest,
                        "sequence": row[0],
                    }
                )
                connection.execute(
                    "UPDATE graph_events SET payload_json=?,payload_hash=?,chain_hash=? "
                    "WHERE event_type='STATE_SNAPSHOT' AND sequence=?",
                    (encoded, digest, chain, row[0]),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(StoreIntegrityError, "node-state mirror"):
                GraphStore(path)

    def test_deleted_evidence_tail_cannot_survive_by_deleting_its_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            engine = GraphEngine(GraphStore(path), config=config())
            engine.plan(linear_graph())
            engine.start("g_durable_linear", "n_a")
            engine.record_result(
                "g_durable_linear",
                "n_a",
                {"value": 1},
                [evidence("n_a")],
                usage=usage(),
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute("DELETE FROM ledger_entries WHERE entry_id='ev_n_a'")
                connection.execute(
                    "DELETE FROM checkpoints WHERE graph_id='g_durable_linear' AND sequence=(SELECT MAX(sequence) FROM checkpoints WHERE graph_id='g_durable_linear')"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(StoreIntegrityError, "node evidence reference is missing"):
                GraphStore(path)

    def test_replan_checkpoint_recovers_both_crash_orderings(self) -> None:
        for crash_after_save in (False, True):
            with self.subTest(crash_after_save=crash_after_save), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "state.sqlite3"
                store = GraphStore(path)
                engine = GraphEngine(store, config=config())
                engine.plan(linear_graph(graph_id="g_replan_crash"))
                revised = linear_graph(graph_id="g_replan_crash")
                revised["graph_revision"] = 2
                revised["updated_at"] = "2026-08-24T00:00:01Z"

                if crash_after_save:
                    real_append = store.append_ledger

                    def crash_append(entry_id, graph_id, revision, ledger_kind, payload, **kwargs):
                        if revision == 2 and ledger_kind == "INTENT":
                            raise RuntimeError("simulated crash after revision save")
                        return real_append(entry_id, graph_id, revision, ledger_kind, payload, **kwargs)

                    store.append_ledger = crash_append
                else:
                    real_save = store.save_revision

                    def crash_save(ir, **kwargs):
                        if ir["graph_revision"] == 2:
                            raise RuntimeError("simulated crash before revision save")
                        return real_save(ir, **kwargs)

                    store.save_revision = crash_save

                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    engine.replan("g_replan_crash", revised, "hidden dependency")

                resumed = GraphEngine(GraphStore(path), config=config()).resume("g_replan_crash")
                self.assertEqual(2, resumed["graph_revision"])
                self.assertEqual("COMPILED", resumed["status"])

    def test_external_receipt_prevents_prepare_and_commit_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            engine = GraphEngine(GraphStore(path), config=config())
            planned = engine.plan(external_graph())
            approval = next(node for node in planned["nodes"] if node["node_id"] == "n_approval")
            with self.assertRaisesRegex(GraphStoreError, "dedicated approve"):
                engine.start("g_durable_external", "n_approval")
            descriptor = operation()
            operation_hash = sha256({"graph_revision": 1, "descriptor": descriptor})
            engine.approve_human(
                "g_durable_external",
                "n_approval",
                f"g_durable_external:n_approval:{approval['input_hash']}:{operation_hash}",
                "owner-local",
                descriptor,
                [evidence("n_approval", kind="human_approval")],
                usage=usage(),
            )
            engine.start("g_durable_external", "n_tool")

            prepared = engine.prepare_external_mutation(
                "g_durable_external", "n_tool", descriptor
            )
            self.assertEqual("PREPARED", prepared["status"])
            with self.assertRaisesRegex(GraphStoreError, "needs reconciliation"):
                engine.prepare_external_mutation(
                    "g_durable_external", "n_tool", descriptor
                )

            committed = engine.commit_external_mutation(
                "g_durable_external", "n_tool", descriptor, "artifact-original"
            )
            replayed_commit = engine.commit_external_mutation(
                "g_durable_external", "n_tool", descriptor, "artifact-replay"
            )
            self.assertEqual(committed, replayed_commit)
            self.assertEqual("artifact-original", replayed_commit["result_ref"])
            self.assertEqual(
                committed,
                engine.prepare_external_mutation(
                    "g_durable_external", "n_tool", descriptor
                ),
            )

            connection = sqlite3.connect(path)
            try:
                receipt_count = connection.execute(
                    "SELECT COUNT(*) FROM idempotency_receipts"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(1, receipt_count)


class Graph07EvidenceAndRetryTests(unittest.TestCase):
    def test_output_evidence_schema_and_required_evidence_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            engine = GraphEngine(
                GraphStore(Path(temp) / "state.sqlite3"), config=config()
            )
            engine.plan(linear_graph())
            engine.start("g_durable_linear", "n_a")

            with self.assertRaisesRegex(GraphStoreError, "output contract mismatch"):
                engine.record_result(
                    "g_durable_linear", "n_a", {"value": "not-an-integer"}, [evidence("n_a")]
                )
            with self.assertRaisesRegex(GraphStoreError, "evidence node mismatch"):
                engine.record_result(
                    "g_durable_linear", "n_a", {"value": 1}, [evidence("n_b")]
                )
            with self.assertRaisesRegex(GraphStoreError, "required evidence missing"):
                engine.record_result(
                    "g_durable_linear",
                    "n_a",
                    {"value": 1},
                    [evidence("n_a", kind="lint_result")],
                )
            invalid = evidence("n_a")
            invalid["unexpected"] = True
            with self.assertRaisesRegex(GraphStoreError, "invalid evidence"):
                engine.record_result(
                    "g_durable_linear", "n_a", {"value": 1}, [invalid]
                )

            self.assertEqual(
                "RUNNING", statuses(engine.status("g_durable_linear"))["n_a"]
            )
            accepted = engine.record_result(
                "g_durable_linear",
                "n_a",
                {"value": 1},
                [evidence("n_a")],
                usage=usage(),
            )
            self.assertEqual("ACCEPTED", statuses(accepted)["n_a"])

    def test_usage_unknown_zero_and_budget_ceiling_remain_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = GraphStore(Path(temp) / "unknown.sqlite3")
            engine = GraphEngine(store, config=config())
            engine.plan(linear_graph(graph_id="g_usage_unknown"))
            engine.start("g_usage_unknown", "n_a")
            with self.assertRaisesRegex(GraphStoreError, "NODE_USAGE_INCONCLUSIVE"):
                engine.record_result(
                    "g_usage_unknown",
                    "n_a",
                    {"value": 1},
                    [evidence("n_a")],
                    usage=usage(duration_ms=None),
                )
            self.assertEqual("FAILED", statuses(engine.status("g_usage_unknown"))["n_a"])
            self.assertIsNone(
                store.usage_totals("g_usage_unknown", 1, "n_a")["duration_ms"]
            )

        with tempfile.TemporaryDirectory() as temp:
            store = GraphStore(Path(temp) / "zero.sqlite3")
            engine = GraphEngine(store, config=config())
            engine.plan(linear_graph(graph_id="g_usage_zero"))
            engine.start("g_usage_zero", "n_a")
            zero_usage = usage(
                duration_ms=0,
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                child_runs=0,
            )
            accepted = engine.record_result(
                "g_usage_zero",
                "n_a",
                {"value": 1},
                [evidence("n_a")],
                usage=zero_usage,
            )
            self.assertEqual("ACCEPTED", statuses(accepted)["n_a"])
            self.assertEqual(
                0, store.usage_totals("g_usage_zero", 1, "n_a")["total_tokens"]
            )

        with tempfile.TemporaryDirectory() as temp:
            store = GraphStore(Path(temp) / "budget.sqlite3")
            engine = GraphEngine(store, config=config())
            value = linear_graph(graph_id="g_usage_budget")
            value["budgets"]["max_tokens"] = 2
            value["contracts"]["intent"]["budget"]["max_tokens"] = 2
            engine.plan(value)
            engine.start("g_usage_budget", "n_a")
            with self.assertRaisesRegex(
                GraphStoreError, "GRAPH_TOKEN_BUDGET_EXCEEDED"
            ):
                engine.record_result(
                    "g_usage_budget",
                    "n_a",
                    {"value": 1},
                    [evidence("n_a")],
                    usage=usage(input_tokens=2, output_tokens=1, reasoning_tokens=0),
                )
            self.assertEqual("FAILED", statuses(engine.status("g_usage_budget"))["n_a"])
            self.assertEqual(
                3, store.usage_totals("g_usage_budget", 1)["total_tokens"]
            )

    def test_invalid_usage_contract_does_not_complete_running_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            engine = GraphEngine(
                GraphStore(Path(temp) / "state.sqlite3"), config=config()
            )
            engine.plan(linear_graph(graph_id="g_usage_invalid"))
            engine.start("g_usage_invalid", "n_a")
            invalid_usage = usage()
            del invalid_usage["child_runs"]
            with self.assertRaisesRegex(GraphStoreError, "attempt usage missing"):
                engine.record_result(
                    "g_usage_invalid",
                    "n_a",
                    {"value": 1},
                    [evidence("n_a")],
                    usage=invalid_usage,
                )
            self.assertEqual("RUNNING", statuses(engine.status("g_usage_invalid"))["n_a"])

    def test_failure_invalidates_only_downstream_and_freezes_independent_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            engine = GraphEngine(
                GraphStore(Path(temp) / "state.sqlite3"), config=config()
            )
            engine.plan(branch_graph())
            for node_id in ("n_left", "n_left_child", "n_right", "n_gate"):
                engine.start("g_durable_branches", node_id)
                accepted = engine.record_result(
                    "g_durable_branches",
                    node_id,
                    {},
                    [evidence(node_id)],
                    usage=usage(),
                )
            self.assertEqual("ACCEPTED", accepted["status"])

            failed = engine.record_failure(
                "g_durable_branches", "n_left", "GATE_FAIL"
            )
            self.assertEqual(
                {
                    "n_left": "FAILED",
                    "n_left_child": "INVALIDATED",
                    "n_right": "FROZEN",
                    "n_gate": "INVALIDATED",
                },
                statuses(failed),
            )
            left_child = next(
                node for node in failed["nodes"] if node["node_id"] == "n_left_child"
            )
            gate = next(node for node in failed["nodes"] if node["node_id"] == "n_gate")
            self.assertIsNone(left_child["output_hash"])
            self.assertIsNone(gate["output_hash"])

    def test_verified_constraint_updates_remaining_node_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            engine = GraphEngine(
                GraphStore(Path(temp) / "state.sqlite3"), config=config()
            )
            engine.plan(linear_graph())
            engine.start("g_durable_linear", "n_a")
            accepted_evidence = evidence("n_a")
            before = engine.record_result(
                "g_durable_linear",
                "n_a",
                {"value": 1},
                [accepted_evidence],
                usage=usage(),
            )
            old_input_hash = next(
                node for node in before["nodes"] if node["node_id"] == "n_b"
            )["input_hash"]
            constraint = {
                "constraint_id": "constraint_interface_fixed",
                "scope": "interface",
                "statement": "the integer interface is fixed for the remaining consumer",
                "evidence_refs": [accepted_evidence["evidence_id"]],
                "confidence": "high",
                "applies_to": ["n_b"],
                "invalidates": [],
                "created_by": "n_a",
            }
            after = engine.add_verified_constraint(
                "g_durable_linear", constraint, [accepted_evidence]
            )
            new_input_hash = next(
                node for node in after["nodes"] if node["node_id"] == "n_b"
            )["input_hash"]
            self.assertNotEqual(old_input_hash, new_input_hash)
            self.assertEqual([constraint], after["constraints"])
            self.assertEqual("READY", statuses(after)["n_b"])
            self.assertEqual("ACCEPTED", statuses(after)["n_a"])

            injected = evidence(
                "n_a",
                evidence_id="ev_unattested_constraint",
                classification="VERIFIED_CONSTRAINT",
            )
            injected_constraint = {
                **constraint,
                "constraint_id": "constraint_unattested",
                "evidence_refs": [injected["evidence_id"]],
            }
            with self.assertRaisesRegex(
                GraphStoreError, "previously attested Evidence Ledger"
            ):
                engine.add_verified_constraint(
                    "g_durable_linear", injected_constraint, [injected]
                )

    def test_attempt_and_failed_gate_limits_end_in_convergence_failure(self) -> None:
        attempt_policy = {
            "max_attempts": 2,
            "max_tokens": None,
            "max_duration_seconds": 30,
            "max_failed_gates": None,
        }
        with tempfile.TemporaryDirectory() as temp:
            engine = GraphEngine(
                GraphStore(Path(temp) / "attempts.sqlite3"), config=config()
            )
            engine.plan(
                linear_graph(graph_id="g_attempt_limit", retry_policy=attempt_policy)
            )
            for attempt in (1, 2):
                engine.start("g_attempt_limit", "n_a")
                failed = engine.record_failure(
                    "g_attempt_limit", "n_a", "TOOL_FAILURE"
                )
                self.assertEqual(attempt, next(
                    node for node in failed["nodes"] if node["node_id"] == "n_a"
                )["attempt"])
                if attempt == 1:
                    engine.retry("g_attempt_limit", "n_a")
            with self.assertRaisesRegex(Exception, "NODE_CONVERGENCE_FAILED"):
                engine.retry("g_attempt_limit", "n_a")

        gate_policy = {
            "max_attempts": 3,
            "max_tokens": None,
            "max_duration_seconds": 30,
            "max_failed_gates": 1,
        }
        with tempfile.TemporaryDirectory() as temp:
            engine = GraphEngine(
                GraphStore(Path(temp) / "gates.sqlite3"), config=config()
            )
            engine.plan(
                linear_graph(graph_id="g_gate_limit", retry_policy=gate_policy)
            )
            engine.start("g_gate_limit", "n_a")
            engine.record_result(
                "g_gate_limit",
                "n_a",
                {"value": 0},
                [evidence("n_a", result="FAIL")],
                gate_result="FAIL",
                usage=usage(),
            )
            engine.retry("g_gate_limit", "n_a")
            engine.start("g_gate_limit", "n_a")
            engine.record_result(
                "g_gate_limit",
                "n_a",
                {"value": 0},
                [evidence("n_a", evidence_id="ev_n_a_attempt_2", result="FAIL")],
                gate_result="FAIL",
                usage=usage(),
            )
            with self.assertRaisesRegex(GraphStoreError, "NODE_CONVERGENCE_FAILED"):
                engine.retry("g_gate_limit", "n_a")


if __name__ == "__main__":
    unittest.main()
