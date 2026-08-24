"""Fail-closed SQLite durable state store for Graph IR v1."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json, sha256, utc_now
from .constants import STORE_SCHEMA_VERSION
from .contracts import validate_attempt_usage, validate_evidence, validate_intent
from .ir import validate_ir_shape
from .policy import PolicyError, validate_policy_candidate, validate_policy_transition


class GraphStoreError(RuntimeError): pass
class StoreIntegrityError(GraphStoreError): pass


def _static_ir(ir: dict[str, Any]) -> dict[str, Any]:
    runtime_keys = {"status", "attempt", "input_hash", "output_hash", "evidence_refs", "gate_result"}
    return {
        **ir,
        "nodes": [{key: value for key, value in node.items() if key not in runtime_keys} for node in ir.get("nodes", [])],
        "status": None,
        "updated_at": None,
        "constraints": None,
    }


def _validate_ledger_payload(ledger_kind: str, payload: Any, classification: str | None) -> None:
    if not isinstance(payload, dict): raise GraphStoreError("ledger payload must be an object")
    if ledger_kind == "INTENT":
        if classification is not None or validate_intent(payload): raise GraphStoreError("Intent Ledger payload is invalid")
        return
    if ledger_kind == "EVIDENCE":
        if classification != payload.get("classification") or validate_evidence(payload): raise GraphStoreError("Evidence Ledger payload is invalid")
        return
    if classification is not None: raise GraphStoreError("Progress Ledger classification must be null")
    if payload.get("event") == "ATTEMPT_USAGE":
        if set(payload) != {"event", "node_id", "attempt", "usage"} or not isinstance(payload.get("node_id"), str) or not isinstance(payload.get("attempt"), int) or payload["attempt"] < 1 or validate_attempt_usage(payload.get("usage")):
            raise GraphStoreError("Progress Ledger usage payload is invalid")
        return
    if payload.get("event") == "HUMAN_APPROVAL":
        expected = {"event", "node_id", "attempt", "actor_ref", "input_hash", "operation_hash"}
        if set(payload) != expected or not isinstance(payload.get("node_id"), str) or not isinstance(payload.get("attempt"), int) or payload["attempt"] < 1 or not all(isinstance(payload.get(key), str) and payload[key] for key in ("actor_ref", "input_hash", "operation_hash")):
            raise GraphStoreError("Progress Ledger approval payload is invalid")
        return
    expected = {"node_id", "status", "attempt"} if payload.get("status") == "RUNNING" else {"node_id", "status", "attempt", "reason_code"}
    if set(payload) != expected or payload.get("status") not in {"RUNNING", "FAILED"} or not isinstance(payload.get("node_id"), str) or not isinstance(payload.get("attempt"), int) or payload["attempt"] < 1 or payload["status"] == "FAILED" and (not isinstance(payload.get("reason_code"), str) or not payload["reason_code"]):
        raise GraphStoreError("Progress Ledger transition payload is invalid")


def _append_receipt_event(con: sqlite3.Connection, idempotency_key: str, payload: dict[str, Any]) -> str:
    encoded, digest = canonical_json(payload), sha256(payload)
    previous = con.execute("SELECT sequence,chain_hash FROM idempotency_receipt_events WHERE idempotency_key=? ORDER BY sequence DESC LIMIT 1", (idempotency_key,)).fetchone()
    sequence, previous_hash = (previous["sequence"] + 1, previous["chain_hash"]) if previous else (1, None)
    chain = sha256({"idempotency_key": idempotency_key, "sequence": sequence, "status": payload["status"], "previous_hash": previous_hash, "payload_hash": digest})
    event_id = sha256({"idempotency_key": idempotency_key, "sequence": sequence, "chain_hash": chain})
    con.execute("INSERT INTO idempotency_receipt_events(event_id,idempotency_key,sequence,status,payload_json,payload_hash,previous_hash,chain_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (event_id, idempotency_key, sequence, payload["status"], encoded, digest, previous_hash, chain, utc_now()))
    return chain


def default_store_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "routecraft" / "graph" / "state.sqlite3"


def ensure_separate_path(path: str | Path, forbidden_roots: list[str | Path] | None = None) -> Path:
    result = Path(path).expanduser().resolve()
    for root in forbidden_roots or []:
        try:
            result.relative_to(Path(root).expanduser().resolve())
            raise GraphStoreError("graph store may not share Memory or Decision Store directory")
        except ValueError: pass
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS store_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS graphs (graph_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, current_revision INTEGER NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS graph_revisions (graph_id TEXT NOT NULL, revision INTEGER NOT NULL, ir_json TEXT NOT NULL, ir_hash TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, PRIMARY KEY(graph_id, revision), FOREIGN KEY(graph_id) REFERENCES graphs(graph_id));
CREATE TABLE IF NOT EXISTS node_states (graph_id TEXT NOT NULL, revision INTEGER NOT NULL, node_id TEXT NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL, input_hash TEXT, output_hash TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(graph_id, revision, node_id));
CREATE TABLE IF NOT EXISTS edge_states (graph_id TEXT NOT NULL, revision INTEGER NOT NULL, edge_ordinal INTEGER NOT NULL, state_json TEXT NOT NULL, PRIMARY KEY(graph_id, revision, edge_ordinal));
CREATE TABLE IF NOT EXISTS ledger_entries (entry_id TEXT PRIMARY KEY, graph_id TEXT NOT NULL, revision INTEGER NOT NULL, ledger_kind TEXT NOT NULL, sequence INTEGER NOT NULL, classification TEXT, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, previous_hash TEXT, chain_hash TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(graph_id, revision, ledger_kind, sequence));
CREATE TABLE IF NOT EXISTS constraints (constraint_id TEXT PRIMARY KEY, graph_id TEXT NOT NULL, revision INTEGER NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints (graph_id TEXT NOT NULL, revision INTEGER NOT NULL, sequence INTEGER NOT NULL, boundary TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, previous_hash TEXT, chain_hash TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(graph_id, revision, sequence));
CREATE TABLE IF NOT EXISTS idempotency_receipts (idempotency_key TEXT PRIMARY KEY, graph_id TEXT NOT NULL, node_id TEXT NOT NULL, attempt INTEGER NOT NULL, input_hash TEXT NOT NULL, operation_scope TEXT NOT NULL, status TEXT NOT NULL, result_ref TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency_receipt_events (event_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, sequence INTEGER NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, previous_hash TEXT, chain_hash TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(idempotency_key, sequence));
CREATE TABLE IF NOT EXISTS graph_events (event_id TEXT PRIMARY KEY, graph_id TEXT NOT NULL, revision INTEGER NOT NULL, event_type TEXT NOT NULL, sequence INTEGER NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, previous_hash TEXT, chain_hash TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(graph_id, revision, event_type, sequence));
CREATE TABLE IF NOT EXISTS outcomes (outcome_id TEXT PRIMARY KEY, graph_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policy_candidates (policy_id TEXT NOT NULL, sequence INTEGER NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, previous_hash TEXT, chain_hash TEXT NOT NULL, actor_ref TEXT, approval_evidence_ref TEXT, created_at TEXT NOT NULL, PRIMARY KEY(policy_id, sequence));
"""

REQUIRED_TABLES = {"store_metadata", "graphs", "graph_revisions", "node_states", "edge_states", "ledger_entries", "constraints", "checkpoints", "idempotency_receipts", "idempotency_receipt_events", "graph_events", "outcomes", "policy_candidates"}


class GraphStore:
    def __init__(self, path: str | Path | None = None, *, forbidden_roots: list[str | Path] | None = None, create: bool = True):
        self.path = ensure_separate_path(path or default_store_path(), forbidden_roots)
        if not create and not self.path.is_file(): raise GraphStoreError("graph state store does not exist")
        if not create:
            self.verify_integrity(read_only=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        self._initialize()
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, isolation_level=None) if read_only else sqlite3.connect(str(self.path), isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        if not read_only:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        con = self._connect()
        try:
            user_version = con.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if user_version not in {0, STORE_SCHEMA_VERSION} or (user_version == 0 and tables): raise StoreIntegrityError("unknown graph store schema")
            if user_version == STORE_SCHEMA_VERSION:
                if not REQUIRED_TABLES.issubset(tables): raise StoreIntegrityError("graph store schema is incomplete")
                event_columns = {row[1] for row in con.execute("PRAGMA table_info(graph_events)")}
                ledger_columns = {row[1] for row in con.execute("PRAGMA table_info(ledger_entries)")}
                policy_columns = {row[1] for row in con.execute("PRAGMA table_info(policy_candidates)")}
                if not {"sequence", "payload_hash", "previous_hash", "chain_hash"}.issubset(event_columns):
                    raise StoreIntegrityError("graph store state-event schema is incomplete")
                if not {"sequence", "status", "payload_hash", "previous_hash", "chain_hash", "actor_ref", "approval_evidence_ref"}.issubset(policy_columns):
                    raise StoreIntegrityError("graph store policy-event schema is incomplete")
                if not {"sequence", "payload_hash", "previous_hash", "chain_hash"}.issubset(ledger_columns):
                    raise StoreIntegrityError("graph store ledger schema is incomplete")
            else:
                con.executescript(SCHEMA)
                con.execute(f"PRAGMA user_version={STORE_SCHEMA_VERSION}")
                con.execute("INSERT INTO store_metadata(key,value) VALUES('schema_version',?)", (str(STORE_SCHEMA_VERSION),))
        finally:
            con.close()
        self.verify_integrity()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self.verify_integrity()
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally: con.close()

    def verify_integrity(self, *, read_only: bool = False) -> None:
        con = self._connect(read_only=read_only)
        try:
            quick = con.execute("PRAGMA quick_check").fetchone()[0]
            version = con.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if quick != "ok" or version != STORE_SCHEMA_VERSION or not REQUIRED_TABLES.issubset(tables): raise StoreIntegrityError("graph store integrity check failed")
            metadata = con.execute("SELECT value FROM store_metadata WHERE key='schema_version'").fetchone()
            if not metadata or metadata[0] != str(STORE_SCHEMA_VERSION): raise StoreIntegrityError("graph store metadata is invalid")
            revisions = con.execute("SELECT graph_id,revision,ir_json,ir_hash FROM graph_revisions ORDER BY graph_id,revision").fetchall()
            revision_sources: dict[tuple[str, int], dict[str, Any]] = {}
            for row in revisions:
                try: source = json.loads(row["ir_json"])
                except Exception as error: raise StoreIntegrityError("graph revision payload is corrupt") from error
                if sha256(source) != row["ir_hash"] or source.get("graph_id") != row["graph_id"] or source.get("graph_revision") != row["revision"]:
                    raise StoreIntegrityError("graph revision hash is corrupt")
                if validate_ir_shape(source): raise StoreIntegrityError("graph revision schema is corrupt")
                revision_sources[(row["graph_id"], row["revision"])] = source
            events = con.execute("SELECT graph_id,revision,event_type,sequence,payload_json,payload_hash,previous_hash,chain_hash FROM graph_events ORDER BY graph_id,revision,event_type,sequence").fetchall()
            event_last: dict[tuple[str, int, str], tuple[int, str] | None] = {}
            for row in events:
                key = (row["graph_id"], row["revision"], row["event_type"])
                previous_event = event_last.get(key)
                expected_sequence = previous_event[0] + 1 if previous_event else 1
                expected_previous = previous_event[1] if previous_event else None
                try: payload = json.loads(row["payload_json"])
                except Exception as error: raise StoreIntegrityError("state-event payload is corrupt") from error
                if row["sequence"] != expected_sequence or sha256(payload) != row["payload_hash"]:
                    raise StoreIntegrityError("state-event payload hash is corrupt")
                expected_chain = sha256({"event_type": row["event_type"], "previous_hash": expected_previous, "payload_hash": row["payload_hash"], "sequence": row["sequence"]})
                if row["previous_hash"] != expected_previous or row["chain_hash"] != expected_chain:
                    raise StoreIntegrityError("state-event chain is corrupt")
                if row["event_type"] == "STATE_SNAPSHOT" and (payload.get("graph_id") != row["graph_id"] or payload.get("graph_revision") != row["revision"]):
                    raise StoreIntegrityError("state-event graph identity is corrupt")
                if row["event_type"] == "STATE_SNAPSHOT" and validate_ir_shape(payload):
                    raise StoreIntegrityError("state-event graph schema is corrupt")
                event_last[key] = (row["sequence"], row["chain_hash"])
            # The latest snapshot is the only mutable runtime truth for a
            # revision. Cross-check its static contract and its materialized
            # node/edge mirrors so partial writes or recomputed standalone
            # hashes cannot be accepted as a coherent state.
            for row in revisions:
                snapshot_row = con.execute("SELECT payload_json FROM graph_events WHERE graph_id=? AND revision=? AND event_type='STATE_SNAPSHOT' ORDER BY sequence DESC LIMIT 1", (row["graph_id"], row["revision"])).fetchone()
                if not snapshot_row: raise StoreIntegrityError("graph revision has no state snapshot")
                source, snapshot = json.loads(row["ir_json"]), json.loads(snapshot_row[0])
                if _static_ir(source) != _static_ir(snapshot): raise StoreIntegrityError("state-event static contract is corrupt")
                node_rows = con.execute("SELECT node_id,status,attempt,input_hash,output_hash FROM node_states WHERE graph_id=? AND revision=? ORDER BY node_id", (row["graph_id"], row["revision"])).fetchall()
                materialized_nodes = [(node["node_id"], node["status"], node["attempt"], node["input_hash"], node["output_hash"]) for node in sorted(snapshot["nodes"], key=lambda item: item["node_id"])]
                if [tuple(item) for item in node_rows] != materialized_nodes: raise StoreIntegrityError("node-state mirror is corrupt")
                edge_rows = con.execute("SELECT state_json FROM edge_states WHERE graph_id=? AND revision=? ORDER BY edge_ordinal", (row["graph_id"], row["revision"])).fetchall()
                if [item[0] for item in edge_rows] != [canonical_json(edge) for edge in snapshot["edges"]]: raise StoreIntegrityError("edge-state mirror is corrupt")
            graphs = con.execute("SELECT graph_id,current_revision,status FROM graphs").fetchall()
            for graph in graphs:
                current = con.execute("SELECT payload_json FROM graph_events WHERE graph_id=? AND revision=? AND event_type='STATE_SNAPSHOT' ORDER BY sequence DESC LIMIT 1", (graph["graph_id"], graph["current_revision"])).fetchone()
                if not current or json.loads(current[0]).get("status") != graph["status"]: raise StoreIntegrityError("graph current-state pointer is corrupt")
            ledgers = con.execute("SELECT entry_id,graph_id,revision,ledger_kind,sequence,classification,payload_json,payload_hash,previous_hash,chain_hash FROM ledger_entries ORDER BY graph_id,revision,ledger_kind,sequence").fetchall()
            ledger_last: dict[tuple[str, int, str], tuple[int, str] | None] = {}
            evidence_by_id: dict[tuple[str, int, str], dict[str, Any]] = {}
            for row in ledgers:
                key = (row["graph_id"], row["revision"], row["ledger_kind"])
                previous = ledger_last.get(key)
                expected_sequence = previous[0] + 1 if previous else 1
                expected_previous = previous[1] if previous else None
                try:
                    payload = json.loads(row["payload_json"])
                    _validate_ledger_payload(row["ledger_kind"], payload, row["classification"])
                except (ValueError, TypeError, GraphStoreError) as error:
                    raise StoreIntegrityError("ledger payload is corrupt") from error
                if key[:2] not in revision_sources: raise StoreIntegrityError("ledger references unknown graph revision")
                source = revision_sources[key[:2]]
                node_ids = {node["node_id"] for node in source["nodes"]}
                if row["ledger_kind"] == "INTENT" and payload != source["contracts"]["intent"]: raise StoreIntegrityError("Intent Ledger diverges from graph contract")
                if row["ledger_kind"] == "EVIDENCE" and (row["entry_id"] != payload["evidence_id"] or payload["node_id"] not in node_ids): raise StoreIntegrityError("Evidence Ledger identity is corrupt")
                if row["ledger_kind"] == "PROGRESS" and payload["node_id"] not in node_ids: raise StoreIntegrityError("Progress Ledger identity is corrupt")
                if row["sequence"] != expected_sequence or sha256(payload) != row["payload_hash"]: raise StoreIntegrityError("ledger payload hash is corrupt")
                expected_chain = sha256({"graph_id": row["graph_id"], "revision": row["revision"], "ledger_kind": row["ledger_kind"], "sequence": row["sequence"], "classification": row["classification"], "previous_hash": expected_previous, "payload_hash": row["payload_hash"]})
                if row["previous_hash"] != expected_previous or row["chain_hash"] != expected_chain: raise StoreIntegrityError("ledger chain is corrupt")
                ledger_last[key] = (row["sequence"], row["chain_hash"])
                if row["ledger_kind"] == "EVIDENCE":
                    evidence_by_id[(row["graph_id"], row["revision"], row["entry_id"])] = payload
            for (graph_id, revision), _source in revision_sources.items():
                snapshot_row = con.execute(
                    "SELECT payload_json FROM graph_events WHERE graph_id=? AND revision=? AND event_type='STATE_SNAPSHOT' ORDER BY sequence DESC LIMIT 1",
                    (graph_id, revision),
                ).fetchone()
                if not snapshot_row:
                    continue
                snapshot = json.loads(snapshot_row[0])
                for node in snapshot["nodes"]:
                    refs = node.get("evidence_refs", [])
                    if not isinstance(refs, list):
                        raise StoreIntegrityError("node evidence references are corrupt")
                    for reference in refs:
                        evidence = evidence_by_id.get((graph_id, revision, reference))
                        if evidence is None or evidence.get("node_id") != node["node_id"]:
                            raise StoreIntegrityError("node evidence reference is missing")
            receipt_events = con.execute("SELECT event_id,idempotency_key,sequence,status,payload_json,payload_hash,previous_hash,chain_hash FROM idempotency_receipt_events ORDER BY idempotency_key,sequence").fetchall()
            receipt_last: dict[str, tuple[int, str, dict[str, Any]] | None] = {}
            for row in receipt_events:
                previous = receipt_last.get(row["idempotency_key"])
                expected_sequence = previous[0] + 1 if previous else 1
                expected_previous = previous[1] if previous else None
                try: payload = json.loads(row["payload_json"])
                except Exception as error: raise StoreIntegrityError("receipt event payload is corrupt") from error
                expected_keys = {"graph_id", "revision", "node_id", "attempt", "input_hash", "operation_scope", "status", "result_ref"}
                if set(payload) != expected_keys or payload.get("status") != row["status"] or payload["status"] not in {"PREPARED", "COMMITTED"}: raise StoreIntegrityError("receipt event schema is corrupt")
                if previous is None and payload["status"] != "PREPARED" or previous is not None and (previous[2]["status"], payload["status"]) != ("PREPARED", "COMMITTED"): raise StoreIntegrityError("receipt status transition is corrupt")
                if payload["status"] == "PREPARED" and payload["result_ref"] is not None or payload["status"] == "COMMITTED" and (not isinstance(payload["result_ref"], str) or not payload["result_ref"]): raise StoreIntegrityError("receipt result invariant is corrupt")
                computed_key = sha256({"graph_id": payload["graph_id"], "graph_revision": payload["revision"], "node_id": payload["node_id"], "attempt": payload["attempt"], "input_hash": payload["input_hash"], "operation_hash": payload["operation_scope"]})
                if computed_key != row["idempotency_key"] or row["sequence"] != expected_sequence or sha256(payload) != row["payload_hash"]: raise StoreIntegrityError("receipt event payload hash is corrupt")
                expected_chain = sha256({"idempotency_key": row["idempotency_key"], "sequence": row["sequence"], "status": row["status"], "previous_hash": expected_previous, "payload_hash": row["payload_hash"]})
                if row["previous_hash"] != expected_previous or row["chain_hash"] != expected_chain: raise StoreIntegrityError("receipt event chain is corrupt")
                receipt_last[row["idempotency_key"]] = (row["sequence"], row["chain_hash"], payload)
            receipts = con.execute("SELECT idempotency_key,graph_id,node_id,attempt,input_hash,operation_scope,status,result_ref FROM idempotency_receipts ORDER BY idempotency_key").fetchall()
            if {row["idempotency_key"] for row in receipts} != set(receipt_last): raise StoreIntegrityError("receipt history and materialized state diverge")
            for row in receipts:
                latest = receipt_last[row["idempotency_key"]][2]
                materialized = (row["graph_id"], row["node_id"], row["attempt"], row["input_hash"], row["operation_scope"], row["status"], row["result_ref"])
                expected = (latest["graph_id"], latest["node_id"], latest["attempt"], latest["input_hash"], latest["operation_scope"], latest["status"], latest["result_ref"])
                if materialized != expected: raise StoreIntegrityError("receipt materialized state is corrupt")
            policies = con.execute("SELECT policy_id,sequence,status,payload_json,payload_hash,previous_hash,chain_hash,actor_ref,approval_evidence_ref FROM policy_candidates ORDER BY policy_id,sequence").fetchall()
            policy_last: dict[str, tuple[int, str, dict[str, Any]] | None] = {}
            for row in policies:
                previous = policy_last.get(row["policy_id"])
                expected_sequence = previous[0] + 1 if previous else 1
                expected_previous = previous[1] if previous else None
                try:
                    candidate = json.loads(row["payload_json"])
                    validate_policy_transition(previous[2] if previous else None, candidate)
                except (ValueError, TypeError, PolicyError) as error:
                    raise StoreIntegrityError("policy candidate history is corrupt") from error
                if row["sequence"] != expected_sequence or row["status"] != candidate["status"] or sha256(candidate) != row["payload_hash"]:
                    raise StoreIntegrityError("policy candidate payload hash is corrupt")
                expected_chain = sha256({"policy_id": row["policy_id"], "sequence": row["sequence"], "status": row["status"], "previous_hash": expected_previous, "payload_hash": row["payload_hash"], "actor_ref": row["actor_ref"], "approval_evidence_ref": row["approval_evidence_ref"]})
                if row["previous_hash"] != expected_previous or row["chain_hash"] != expected_chain:
                    raise StoreIntegrityError("policy candidate chain is corrupt")
                if candidate["status"] == "APPROVED":
                    evidence_refs = {item["evidence_ref"] for item in candidate["evidence"]}
                    if not row["actor_ref"] or row["approval_evidence_ref"] not in evidence_refs: raise StoreIntegrityError("policy approval provenance is corrupt")
                elif row["actor_ref"] is not None or row["approval_evidence_ref"] is not None:
                    raise StoreIntegrityError("policy approval provenance is misplaced")
                policy_last[row["policy_id"]] = (row["sequence"], row["chain_hash"], candidate)
            rows = con.execute("SELECT graph_id,revision,sequence,boundary,payload_hash,previous_hash,chain_hash FROM checkpoints ORDER BY graph_id,revision,sequence").fetchall()
            last: dict[tuple[str, int], tuple[int, str]] = {}
            for row in rows:
                key = (row["graph_id"], row["revision"])
                previous_checkpoint = last.get(key)
                expected_previous = previous_checkpoint[1] if previous_checkpoint else None
                if not isinstance(row["boundary"], str) or not row["boundary"] or len(row["boundary"]) > 64 or not row["boundary"].replace("_", "").isalnum():
                    raise StoreIntegrityError("checkpoint boundary is corrupt")
                try: payload = json.loads(con.execute("SELECT payload_json FROM checkpoints WHERE graph_id=? AND revision=? AND sequence=?", (row["graph_id"], row["revision"], row["sequence"])).fetchone()[0])
                except Exception as error: raise StoreIntegrityError("checkpoint payload is corrupt") from error
                expected_keys = {"ir", "nodes", "constraints", "receipt_refs", "ledger_heads"}
                if not isinstance(payload, dict) or frozenset(payload) not in {frozenset(expected_keys), frozenset(expected_keys | {"boundary_payload"})}: raise StoreIntegrityError("checkpoint payload schema is corrupt")
                checkpoint_ir = payload.get("ir")
                if not isinstance(checkpoint_ir, dict) or checkpoint_ir.get("graph_id") != row["graph_id"] or checkpoint_ir.get("graph_revision") != row["revision"] or validate_ir_shape(checkpoint_ir): raise StoreIntegrityError("checkpoint graph identity is corrupt")
                expected_nodes = [{field: node.get(field) for field in ("node_id", "status", "attempt", "input_hash", "output_hash", "evidence_refs", "gate_result")} for node in checkpoint_ir["nodes"]]
                if payload.get("nodes") != expected_nodes or payload.get("constraints") != checkpoint_ir["constraints"]: raise StoreIntegrityError("checkpoint state summary is corrupt")
                if not isinstance(payload.get("ledger_heads"), list) or not isinstance(payload.get("receipt_refs"), list): raise StoreIntegrityError("checkpoint integrity references are corrupt")
                for head in payload["ledger_heads"]:
                    if not isinstance(head, dict) or set(head) != {"ledger_kind", "sequence", "chain_hash"}: raise StoreIntegrityError("checkpoint ledger reference is corrupt")
                    found = con.execute("SELECT 1 FROM ledger_entries WHERE graph_id=? AND revision=? AND ledger_kind=? AND sequence=? AND chain_hash=?", (row["graph_id"], row["revision"], head["ledger_kind"], head["sequence"], head["chain_hash"])).fetchone()
                    if not found: raise StoreIntegrityError("checkpoint ledger reference is missing")
                for reference in payload["receipt_refs"]:
                    if not isinstance(reference, dict) or set(reference) != {"idempotency_key", "status", "chain_hash"}: raise StoreIntegrityError("checkpoint receipt reference is corrupt")
                    found = con.execute("SELECT 1 FROM idempotency_receipt_events WHERE idempotency_key=? AND status=? AND chain_hash=?", (reference["idempotency_key"], reference["status"], reference["chain_hash"])).fetchone()
                    if not found: raise StoreIntegrityError("checkpoint receipt reference is missing")
                if sha256(payload) != row["payload_hash"]: raise StoreIntegrityError("checkpoint payload hash is corrupt")
                expected_chain = sha256({"boundary": row["boundary"], "previous_hash": expected_previous, "payload_hash": row["payload_hash"], "sequence": row["sequence"]})
                if row["previous_hash"] != expected_previous or row["chain_hash"] != expected_chain: raise StoreIntegrityError("checkpoint chain is corrupt")
                last[key] = (row["sequence"], row["chain_hash"])
            anchored_rows = con.execute(
                "SELECT key,value FROM store_metadata WHERE key LIKE 'checkpoint_head:%' ORDER BY key"
            ).fetchall()
            anchored: dict[tuple[str, int], tuple[int, str]] = {}
            for row in anchored_rows:
                try:
                    value = json.loads(row["value"])
                    graph_id = value["graph_id"]
                    revision = value["revision"]
                    sequence = value["sequence"]
                    chain_hash = value["chain_hash"]
                except (ValueError, TypeError, KeyError) as error:
                    raise StoreIntegrityError("checkpoint head anchor is corrupt") from error
                expected_key = f"checkpoint_head:{sha256({'graph_id': graph_id, 'revision': revision})}"
                if (
                    row["key"] != expected_key
                    or not isinstance(graph_id, str)
                    or not isinstance(revision, int)
                    or not isinstance(sequence, int)
                    or sequence < 1
                    or not isinstance(chain_hash, str)
                    or (graph_id, revision) in anchored
                ):
                    raise StoreIntegrityError("checkpoint head anchor is corrupt")
                anchored[(graph_id, revision)] = (sequence, chain_hash)
            if anchored != last:
                raise StoreIntegrityError("checkpoint chain head does not match its durable anchor")
        finally:
            con.close()

    def _save_revision_in(self, con: sqlite3.Connection, ir: dict[str, Any], *, reason: str | None = None) -> None:
        """Materialize one state snapshot inside an already-open write transaction."""
        graph_id, revision = ir["graph_id"], ir["graph_revision"]
        encoded, digest = canonical_json(ir), sha256(ir)
        current = con.execute("SELECT current_revision FROM graphs WHERE graph_id=?", (graph_id,)).fetchone()
        if current and revision < current[0]: raise GraphStoreError("cannot replace newer graph revision")
        if current and revision > current[0] + 1: raise GraphStoreError("graph revisions must be contiguous")
        if not current and revision != 1: raise GraphStoreError("new graph must start at revision 1")
        existing = con.execute("SELECT ir_hash FROM graph_revisions WHERE graph_id=? AND revision=?", (graph_id, revision)).fetchone()
        if existing and existing["ir_hash"] != digest:
            # The source IR for a revision is immutable. Runtime transitions
            # are kept as append-only STATE_SNAPSHOT events below.
            source = json.loads(con.execute("SELECT ir_json FROM graph_revisions WHERE graph_id=? AND revision=?", (graph_id, revision)).fetchone()[0])
            if _static_ir(source) != _static_ir(ir): raise GraphStoreError("graph revision source IR is immutable")
        con.execute("INSERT INTO graphs(graph_id,created_at,current_revision,status) VALUES(?,?,?,?) ON CONFLICT(graph_id) DO UPDATE SET current_revision=excluded.current_revision,status=excluded.status", (graph_id, ir["created_at"], revision, ir["status"]))
        con.execute("INSERT OR IGNORE INTO graph_revisions(graph_id,revision,ir_json,ir_hash,reason,created_at) VALUES(?,?,?,?,?,?)", (graph_id, revision, encoded, digest, reason, utc_now()))
        for node in ir["nodes"]:
            con.execute("INSERT OR REPLACE INTO node_states(graph_id,revision,node_id,status,attempt,input_hash,output_hash,updated_at) VALUES(?,?,?,?,?,?,?,?)", (graph_id, revision, node["node_id"], node["status"], node["attempt"], node["input_hash"], node["output_hash"], utc_now()))
        for ordinal, edge in enumerate(ir["edges"]): con.execute("INSERT OR REPLACE INTO edge_states(graph_id,revision,edge_ordinal,state_json) VALUES(?,?,?,?)", (graph_id, revision, ordinal, canonical_json(edge)))
        # Runtime state is append-only, hash-chained, and separate from the
        # immutable source IR. A syntactically valid SQLite edit must not be
        # able to become a trusted resume point.
        previous = con.execute("SELECT sequence,chain_hash FROM graph_events WHERE graph_id=? AND revision=? AND event_type='STATE_SNAPSHOT' ORDER BY sequence DESC LIMIT 1", (graph_id, revision)).fetchone()
        sequence, previous_hash = (previous["sequence"] + 1, previous["chain_hash"]) if previous else (1, None)
        chain = sha256({"event_type": "STATE_SNAPSHOT", "previous_hash": previous_hash, "payload_hash": digest, "sequence": sequence})
        event_id = sha256({"graph_id": graph_id, "revision": revision, "event_type": "STATE_SNAPSHOT", "sequence": sequence, "chain_hash": chain})
        con.execute("INSERT INTO graph_events(event_id,graph_id,revision,event_type,sequence,payload_json,payload_hash,previous_hash,chain_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (event_id, graph_id, revision, "STATE_SNAPSHOT", sequence, encoded, digest, previous_hash, chain, utc_now()))

    def save_revision(self, ir: dict[str, Any], *, reason: str | None = None) -> None:
        with self._write() as con:
            self._save_revision_in(con, ir, reason=reason)

    def load_revision(self, graph_id: str, revision: int | None = None) -> dict[str, Any]:
        self.verify_integrity()
        con = self._connect(read_only=True)
        try:
            selected_revision = revision
            if selected_revision is None:
                current = con.execute("SELECT current_revision FROM graphs WHERE graph_id=?", (graph_id,)).fetchone()
                if not current: raise GraphStoreError("graph revision not found")
                selected_revision = int(current[0])
            row = con.execute("SELECT ir_json FROM graph_revisions WHERE graph_id=? AND revision=?", (graph_id, selected_revision)).fetchone()
            if not row: raise GraphStoreError("graph revision not found")
            snapshot = con.execute("SELECT payload_json FROM graph_events WHERE graph_id=? AND revision=? AND event_type='STATE_SNAPSHOT' ORDER BY sequence DESC LIMIT 1", (graph_id, selected_revision)).fetchone()
            return json.loads(snapshot[0] if snapshot else row[0])
        finally:
            con.close()

    def _checkpoint_in(self, con: sqlite3.Connection, ir: dict[str, Any], boundary: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(boundary, str) or not boundary or len(boundary) > 64 or not boundary.replace("_", "").isalnum():
            raise GraphStoreError("checkpoint boundary invalid")
        graph_id, revision = ir["graph_id"], ir["graph_revision"]
        boundary_payload = payload
        receipts = [
                {"idempotency_key": row["idempotency_key"], "status": row["status"], "chain_hash": row["chain_hash"]}
                for row in con.execute(
                    "SELECT receipt.idempotency_key,receipt.status,event.chain_hash FROM idempotency_receipts AS receipt "
                    "JOIN idempotency_receipt_events AS event ON event.idempotency_key=receipt.idempotency_key "
                    "AND event.sequence=(SELECT MAX(latest.sequence) FROM idempotency_receipt_events AS latest WHERE latest.idempotency_key=receipt.idempotency_key) "
                    "WHERE receipt.graph_id=? ORDER BY receipt.idempotency_key",
                    (graph_id,),
                )
        ]
        ledger_heads = [
                {"ledger_kind": row["ledger_kind"], "sequence": row["sequence"], "chain_hash": row["chain_hash"]}
                for row in con.execute(
                    "SELECT ledger_kind,sequence,chain_hash FROM ledger_entries AS entry WHERE graph_id=? AND revision=? "
                    "AND sequence=(SELECT MAX(latest.sequence) FROM ledger_entries AS latest WHERE latest.graph_id=entry.graph_id AND latest.revision=entry.revision AND latest.ledger_kind=entry.ledger_kind) "
                    "ORDER BY ledger_kind",
                    (graph_id, revision),
                )
        ]
        payload = {"ir": ir, "nodes": [{key: node.get(key) for key in ("node_id", "status", "attempt", "input_hash", "output_hash", "evidence_refs", "gate_result")} for node in ir["nodes"]], "constraints": ir["constraints"], "receipt_refs": receipts, "ledger_heads": ledger_heads}
        if boundary_payload is not None: payload["boundary_payload"] = boundary_payload
        encoded, digest = canonical_json(payload), sha256(payload)
        previous = con.execute("SELECT sequence,chain_hash FROM checkpoints WHERE graph_id=? AND revision=? ORDER BY sequence DESC LIMIT 1", (graph_id, revision)).fetchone()
        sequence, previous_hash = (previous["sequence"] + 1, previous["chain_hash"]) if previous else (1, None)
        chain = sha256({"boundary": boundary, "previous_hash": previous_hash, "payload_hash": digest, "sequence": sequence})
        con.execute("INSERT INTO checkpoints(graph_id,revision,sequence,boundary,payload_json,payload_hash,previous_hash,chain_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (graph_id, revision, sequence, boundary, encoded, digest, previous_hash, chain, utc_now()))
        anchor_key = f"checkpoint_head:{sha256({'graph_id': graph_id, 'revision': revision})}"
        anchor_value = canonical_json({
            "graph_id": graph_id,
            "revision": revision,
            "sequence": sequence,
            "chain_hash": chain,
        })
        con.execute(
            "INSERT INTO store_metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (anchor_key, anchor_value),
        )
        return {"sequence": sequence, "payload_hash": digest, "chain_hash": chain}

    def checkpoint(self, ir: dict[str, Any], boundary: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._write() as con:
            return self._checkpoint_in(con, ir, boundary, payload)

    def save_and_checkpoint(self, ir: dict[str, Any], boundary: str, *, reason: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Atomically commit a materialized runtime state and its checkpoint.

        Resume may only select a checkpointed state.  This couples the two
        records so a process interruption cannot leave a newer snapshot that a
        later resume overwrites from an older branch checkpoint.
        """
        with self._write() as con:
            self._save_revision_in(con, ir, reason=reason)
            return self._checkpoint_in(con, ir, boundary, payload)

    @classmethod
    def open_read_only(cls, path: str | Path) -> "GraphStore":
        """Verify an existing store without creating directories, DB files, WAL, or metadata."""
        result = cls.__new__(cls)
        result.path = Path(path).expanduser().resolve()
        if not result.path.is_file(): raise GraphStoreError("graph state store does not exist")
        result.verify_integrity(read_only=True)
        return result

    def latest_checkpoint(self, graph_id: str, revision: int | None = None) -> dict[str, Any]:
        self.verify_integrity()
        con = self._connect(read_only=True)
        try:
            if revision is None: row = con.execute("SELECT * FROM checkpoints WHERE graph_id=? ORDER BY revision DESC,sequence DESC LIMIT 1", (graph_id,)).fetchone()
            else: row = con.execute("SELECT * FROM checkpoints WHERE graph_id=? AND revision=? ORDER BY sequence DESC LIMIT 1", (graph_id, revision)).fetchone()
            if not row: raise GraphStoreError("checkpoint not found")
            return {"revision": row["revision"], "sequence": row["sequence"], "boundary": row["boundary"], "payload": json.loads(row["payload_json"]), "chain_hash": row["chain_hash"]}
        finally:
            con.close()

    @staticmethod
    def idempotency_key(graph_id: str, revision: int, node_id: str, attempt: int, input_hash: str, operation_hash: str) -> str:
        return sha256({"graph_id": graph_id, "graph_revision": revision, "node_id": node_id, "attempt": attempt, "input_hash": input_hash, "operation_hash": operation_hash})

    def graph_has_uncertain_receipt(self, graph_id: str) -> bool:
        self.verify_integrity()
        con = self._connect(read_only=True)
        try:
            return con.execute("SELECT 1 FROM idempotency_receipts WHERE graph_id=? AND status IN ('PREPARED','UNKNOWN') LIMIT 1", (graph_id,)).fetchone() is not None
        finally: con.close()

    def prepare_external_mutation(self, graph_id: str, revision: int, node_id: str, attempt: int, input_hash: str, operation_hash: str) -> dict[str, Any]:
        key = self.idempotency_key(graph_id, revision, node_id, attempt, input_hash, operation_hash)
        # Claim and inspection are one BEGIN IMMEDIATE transaction.  A second
        # process can therefore never receive a fresh PREPARED claim for the
        # same idempotency key.
        with self._write() as con:
            existing = con.execute("SELECT status,result_ref FROM idempotency_receipts WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                if existing["status"] == "COMMITTED":
                    return {"idempotency_key": key, "status": "COMMITTED", "result_ref": existing["result_ref"]}
                raise GraphStoreError("external operation receipt needs reconciliation")
            con.execute(
                "INSERT INTO idempotency_receipts(idempotency_key,graph_id,node_id,attempt,input_hash,operation_scope,status,result_ref,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (key, graph_id, node_id, attempt, input_hash, operation_hash, "PREPARED", None, utc_now()),
            )
            _append_receipt_event(con, key, {"graph_id": graph_id, "revision": revision, "node_id": node_id, "attempt": attempt, "input_hash": input_hash, "operation_scope": operation_hash, "status": "PREPARED", "result_ref": None})
        return {"idempotency_key": key, "status": "PREPARED", "result_ref": None}

    def commit_external_mutation(self, graph_id: str, revision: int, node_id: str, attempt: int, input_hash: str, operation_hash: str, result_ref: str) -> dict[str, Any]:
        if not isinstance(result_ref, str) or not result_ref: raise GraphStoreError("result reference required")
        key = self.idempotency_key(graph_id, revision, node_id, attempt, input_hash, operation_hash)
        with self._write() as con:
            existing = con.execute("SELECT status,result_ref FROM idempotency_receipts WHERE idempotency_key=?", (key,)).fetchone()
            if not existing: raise GraphStoreError("external mutation was not prepared")
            if existing["status"] == "COMMITTED":
                return {"idempotency_key": key, "status": "COMMITTED", "result_ref": existing["result_ref"]}
            if existing["status"] != "PREPARED": raise GraphStoreError("external mutation receipt needs reconciliation")
            con.execute("UPDATE idempotency_receipts SET status='COMMITTED',result_ref=?,updated_at=? WHERE idempotency_key=? AND status='PREPARED'", (result_ref, utc_now(), key))
            if con.total_changes != 1: raise GraphStoreError("external mutation receipt race detected")
            _append_receipt_event(con, key, {"graph_id": graph_id, "revision": revision, "node_id": node_id, "attempt": attempt, "input_hash": input_hash, "operation_scope": operation_hash, "status": "COMMITTED", "result_ref": result_ref})
        return {"idempotency_key": key, "status": "COMMITTED", "result_ref": result_ref}

    def human_approval_exists(self, graph_id: str, revision: int, operation_hash: str) -> bool:
        self.verify_integrity()
        con = self._connect(read_only=True)
        try:
            rows = con.execute("SELECT payload_json FROM ledger_entries WHERE graph_id=? AND revision=? AND ledger_kind='PROGRESS'", (graph_id, revision)).fetchall()
        finally:
            con.close()
        for row in rows:
            payload = json.loads(row[0])
            if payload.get("event") == "HUMAN_APPROVAL" and payload.get("operation_hash") == operation_hash:
                return True
        return False

    def _append_ledger_in(self, con: sqlite3.Connection, entry_id: str, graph_id: str, revision: int, ledger_kind: str, payload: dict[str, Any], *, classification: str | None = None) -> None:
        """Append one immutable ledger row inside an existing transaction."""
        if ledger_kind not in {"INTENT", "EVIDENCE", "PROGRESS"}: raise GraphStoreError("ledger kind invalid")
        _validate_ledger_payload(ledger_kind, payload, classification)
        encoded, digest = canonical_json(payload), sha256(payload)
        existing = con.execute("SELECT graph_id,revision,ledger_kind,classification,payload_json FROM ledger_entries WHERE entry_id=?", (entry_id,)).fetchone()
        expected = (graph_id, revision, ledger_kind, classification, encoded)
        if existing:
            if tuple(existing) != expected: raise GraphStoreError("ledger entry is immutable")
            return
        previous = con.execute("SELECT sequence,chain_hash FROM ledger_entries WHERE graph_id=? AND revision=? AND ledger_kind=? ORDER BY sequence DESC LIMIT 1", (graph_id, revision, ledger_kind)).fetchone()
        sequence, previous_hash = (previous["sequence"] + 1, previous["chain_hash"]) if previous else (1, None)
        chain = sha256({"graph_id": graph_id, "revision": revision, "ledger_kind": ledger_kind, "sequence": sequence, "classification": classification, "previous_hash": previous_hash, "payload_hash": digest})
        con.execute("INSERT INTO ledger_entries(entry_id,graph_id,revision,ledger_kind,sequence,classification,payload_json,payload_hash,previous_hash,chain_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (entry_id, graph_id, revision, ledger_kind, sequence, classification, encoded, digest, previous_hash, chain, utc_now()))

    def append_ledger(self, entry_id: str, graph_id: str, revision: int, ledger_kind: str, payload: dict[str, Any], *, classification: str | None = None) -> None:
        with self._write() as con:
            self._append_ledger_in(con, entry_id, graph_id, revision, ledger_kind, payload, classification=classification)

    def commit_result(
        self,
        ir: dict[str, Any],
        boundary: str,
        ledger_entries: list[tuple[str, str, dict[str, Any], str | None]],
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Atomically accept one result's ledgers, state mirror, and checkpoint.

        Evidence and attempt usage are immutable facts about a particular
        attempt.  They must never outlive the matching durable state snapshot:
        otherwise a process crash could restore ``RUNNING`` while a later
        result for that attempt collides with a stale immutable ledger row.
        ``ledger_entries`` are idempotent only when byte-for-byte identical.
        """
        graph_id, revision = ir["graph_id"], ir["graph_revision"]
        entry_ids = [entry[0] for entry in ledger_entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise GraphStoreError("duplicate ledger entry id in result commit")
        with self._write() as con:
            for entry_id, ledger_kind, payload, classification in ledger_entries:
                self._append_ledger_in(
                    con, entry_id, graph_id, revision, ledger_kind, payload,
                    classification=classification,
                )
            self._save_revision_in(con, ir, reason=reason)
            return self._checkpoint_in(con, ir, boundary)

    def commit_constraint(
        self,
        ir: dict[str, Any],
        constraint: dict[str, Any],
        evidence_entries: list[tuple[str, str, dict[str, Any], str | None]],
    ) -> dict[str, Any]:
        """Atomically apply one verified constraint and its durable evidence.

        Observe mode may introduce an Evidence Ledger row at the same time as
        the constraint which cites it.  A constraint row, the append-only
        ledger facts, the mutable state snapshot, and its checkpoint must
        therefore have exactly one commit boundary.  Otherwise a process stop
        can leave a constraint row without the state that contains it (or the
        inverse), which makes a later retry either lose the constraint or hit
        a raw SQLite uniqueness error.

        ``evidence_entries`` is empty for enforce mode: enforce constraints
        must reference Evidence Ledger facts that were attested earlier.
        """
        graph_id, revision = ir["graph_id"], ir["graph_revision"]
        entry_ids = [entry[0] for entry in evidence_entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise GraphStoreError("duplicate ledger entry id in constraint commit")
        encoded_constraint = canonical_json(constraint)
        with self._write() as con:
            for entry_id, ledger_kind, payload, classification in evidence_entries:
                self._append_ledger_in(
                    con, entry_id, graph_id, revision, ledger_kind, payload,
                    classification=classification,
                )
            existing = con.execute(
                "SELECT graph_id,revision,payload_json FROM constraints WHERE constraint_id=?",
                (constraint["constraint_id"],),
            ).fetchone()
            expected = (graph_id, revision, encoded_constraint)
            if existing:
                if tuple(existing) != expected:
                    raise GraphStoreError("constraint is immutable")
            else:
                con.execute(
                    "INSERT INTO constraints(constraint_id,graph_id,revision,payload_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        constraint["constraint_id"], graph_id, revision,
                        encoded_constraint, utc_now(),
                    ),
                )
            self._save_revision_in(con, ir)
            return self._checkpoint_in(con, ir, "constraint_applied")

    def failed_gate_count(self, graph_id: str, revision: int, node_id: str) -> int:
        self.verify_integrity()
        con = self._connect(read_only=True)
        try:
            rows = con.execute("SELECT payload_json FROM ledger_entries WHERE graph_id=? AND revision=? AND ledger_kind='PROGRESS'", (graph_id, revision)).fetchall()
            count = 0
            for row in rows:
                payload = json.loads(row[0])
                if payload.get("node_id") == node_id and str(payload.get("reason_code", "")).startswith("GATE_"): count += 1
            return count
        finally: con.close()

    def usage_totals(self, graph_id: str, revision: int, node_id: str | None = None) -> dict[str, int | None]:
        """Aggregate measured attempt usage while preserving unknown as ``None``."""
        self.verify_integrity()
        con = self._connect(read_only=True)
        try:
            rows = con.execute(
                "SELECT payload_json FROM ledger_entries WHERE graph_id=? AND revision=? AND ledger_kind='PROGRESS' ORDER BY created_at,entry_id",
                (graph_id, revision),
            ).fetchall()
        finally:
            con.close()
        usage_rows: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row[0])
            if payload.get("event") != "ATTEMPT_USAGE" or node_id is not None and payload.get("node_id") != node_id:
                continue
            usage = payload.get("usage")
            if isinstance(usage, dict):
                usage_rows.append(usage)
        metrics = ("duration_ms", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "child_runs")
        result: dict[str, int | None] = {"attempts_with_usage": len(usage_rows)}
        for metric in metrics:
            values = [usage.get(metric) for usage in usage_rows]
            result[metric] = sum(values) if values and all(isinstance(value, int) and not isinstance(value, bool) for value in values) else None
        token_parts = [result["input_tokens"], result["output_tokens"], result["reasoning_tokens"]]
        result["total_tokens"] = sum(token_parts) if all(isinstance(value, int) for value in token_parts) else None
        return result

    def checkpoint_count(self, graph_id: str, revision: int) -> int:
        self.verify_integrity()
        con = self._connect(read_only=True)
        try:
            return int(con.execute("SELECT COUNT(*) FROM checkpoints WHERE graph_id=? AND revision=?", (graph_id, revision)).fetchone()[0])
        finally:
            con.close()

    def checkpoint_boundary_count(self, graph_id: str, revision: int, boundary: str) -> int:
        """Count authenticated checkpoint boundaries as observed transitions."""
        self.verify_integrity()
        con = self._connect(read_only=True)
        try:
            return int(con.execute("SELECT COUNT(*) FROM checkpoints WHERE graph_id=? AND revision=? AND boundary=?", (graph_id, revision, boundary)).fetchone()[0])
        finally:
            con.close()

    def gate_results(self, graph_id: str, revision: int) -> dict[str, str]:
        """Return only bounded gate verdicts extracted from structured Evidence Ledger rows."""
        self.verify_integrity()
        con = self._connect(read_only=True)
        try:
            rows = con.execute("SELECT payload_json FROM ledger_entries WHERE graph_id=? AND revision=? AND ledger_kind='EVIDENCE' ORDER BY sequence", (graph_id, revision)).fetchall()
        finally:
            con.close()
        result: dict[str, str] = {}
        for row in rows:
            payload = json.loads(row[0])
            node_id, verdict = payload.get("node_id"), payload.get("result")
            if isinstance(node_id, str) and verdict in {"PASS", "FAIL", "INCONCLUSIVE"}:
                result[node_id] = verdict
        return result

    def transition_events(self, graph_id: str, revision: int) -> list[dict[str, Any]]:
        """Derive bounded Gate/send-back history from authenticated checkpoints.

        Checkpoint payloads contain the exact IR committed at each durable
        boundary.  This method exposes only node ids, finite verdicts, attempt
        counts and affected counts to the local privacy projector; semantic
        objectives, evidence statements and outputs never leave the store.
        """
        self.verify_integrity()
        con = self._connect(read_only=True)
        try:
            rows = con.execute(
                "SELECT sequence,boundary,payload_json FROM checkpoints "
                "WHERE graph_id=? AND revision=? ORDER BY sequence",
                (graph_id, revision),
            ).fetchall()
        finally:
            con.close()

        events: list[dict[str, Any]] = []
        seen_gates: set[tuple[str, int, str]] = set()
        pending_gate: dict[str, Any] | None = None
        pending_ir: dict[str, Any] | None = None
        for row in rows:
            payload = json.loads(row["payload_json"])
            ir = payload.get("ir") if isinstance(payload, dict) else None
            if not isinstance(ir, dict) or not isinstance(ir.get("nodes"), list):
                continue
            if row["boundary"] == "gate_resolution":
                new_gates: list[dict[str, Any]] = []
                for node in sorted(ir["nodes"], key=lambda item: str(item.get("node_id"))):
                    node_id = node.get("node_id")
                    attempt = node.get("attempt")
                    verdict = node.get("gate_result")
                    if (
                        node.get("node_type") != "GATE"
                        or node.get("status") not in {"ACCEPTED", "FROZEN"}
                        or not isinstance(node_id, str)
                        or not isinstance(attempt, int)
                        or isinstance(attempt, bool)
                        or attempt < 1
                        or verdict not in {"PASS", "FAIL", "INCONCLUSIVE"}
                    ):
                        continue
                    signature = (node_id, attempt, verdict)
                    if signature in seen_gates:
                        continue
                    seen_gates.add(signature)
                    item = {
                        "event_type": "gate",
                        "node_id": node_id,
                        "status": node["status"],
                        "gate_status": verdict,
                        "attempt_count": attempt,
                        "affected_node_count": 1,
                    }
                    events.append(item)
                    new_gates.append(item)
                if new_gates:
                    pending_gate = new_gates[-1]
                    pending_ir = ir
            elif row["boundary"] == "send_back":
                before = {
                    node.get("node_id"): node.get("status")
                    for node in (pending_ir or {}).get("nodes", [])
                    if isinstance(node, dict) and isinstance(node.get("node_id"), str)
                }
                after = {
                    node.get("node_id"): node.get("status")
                    for node in ir["nodes"]
                    if isinstance(node, dict) and isinstance(node.get("node_id"), str)
                }
                affected = sum(before.get(node_id) != status for node_id, status in after.items())
                events.append({
                    "event_type": "send_back",
                    "node_id": pending_gate.get("node_id") if pending_gate else None,
                    "status": ir.get("status") if ir.get("status") in {"RUNNING", "ACCEPTED", "FAILED", "BLOCKED", "CANCELLED"} else "RUNNING",
                    "gate_status": None,
                    "attempt_count": pending_gate.get("attempt_count", 0) if pending_gate else 0,
                    "affected_node_count": affected,
                })
                pending_gate = None
                pending_ir = None
        return events

    def evidence_entries(
        self, graph_id: str, revision: int, evidence_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Load immutable Evidence Ledger rows after verifying the local store."""
        self.verify_integrity()
        if not evidence_ids or len(evidence_ids) != len(set(evidence_ids)):
            raise GraphStoreError("evidence references must be unique and non-empty")
        con = self._connect(read_only=True)
        try:
            rows = con.execute(
                "SELECT entry_id,payload_json FROM ledger_entries "
                "WHERE graph_id=? AND revision=? AND ledger_kind='EVIDENCE'",
                (graph_id, revision),
            ).fetchall()
        finally:
            con.close()
        requested = set(evidence_ids)
        return {
            row["entry_id"]: json.loads(row["payload_json"])
            for row in rows
            if row["entry_id"] in requested
        }

    def save_constraint(self, constraint: dict[str, Any], graph_id: str, revision: int) -> None:
        with self._write() as con: con.execute("INSERT INTO constraints(constraint_id,graph_id,revision,payload_json,created_at) VALUES(?,?,?,?,?)", (constraint["constraint_id"], graph_id, revision, canonical_json(constraint), utc_now()))

    def save_outcome(self, outcome_id: str, graph_id: str, outcome: dict[str, Any]) -> None:
        with self._write() as con: con.execute("INSERT INTO outcomes(outcome_id,graph_id,payload_json,created_at) VALUES(?,?,?,?)", (outcome_id, graph_id, canonical_json(outcome), utc_now()))

    def save_policy_candidate(self, candidate: dict[str, Any], *, actor_ref: str | None = None, approval_evidence_ref: str | None = None) -> dict[str, Any]:
        validate_policy_candidate(candidate)
        if candidate["status"] == "APPROVED":
            evidence_refs = {item["evidence_ref"] for item in candidate["evidence"]}
            if not isinstance(actor_ref, str) or not actor_ref or approval_evidence_ref not in evidence_refs:
                raise GraphStoreError("approved policy candidate requires bound human provenance")
        elif actor_ref is not None or approval_evidence_ref is not None:
            raise GraphStoreError("approval provenance is only valid for APPROVED status")
        encoded, digest = canonical_json(candidate), sha256(candidate)
        with self._write() as con:
            previous_row = con.execute("SELECT sequence,payload_json,chain_hash,actor_ref,approval_evidence_ref FROM policy_candidates WHERE policy_id=? ORDER BY sequence DESC LIMIT 1", (candidate["policy_id"],)).fetchone()
            previous = json.loads(previous_row["payload_json"]) if previous_row else None
            if previous == candidate:
                if previous_row["actor_ref"] != actor_ref or previous_row["approval_evidence_ref"] != approval_evidence_ref:
                    raise GraphStoreError("policy candidate provenance is immutable")
                return {"policy_id": candidate["policy_id"], "sequence": previous_row["sequence"], "status": candidate["status"], "chain_hash": previous_row["chain_hash"]}
            try: validate_policy_transition(previous, candidate)
            except PolicyError as error: raise GraphStoreError(str(error)) from error
            sequence = previous_row["sequence"] + 1 if previous_row else 1
            previous_hash = previous_row["chain_hash"] if previous_row else None
            chain = sha256({"policy_id": candidate["policy_id"], "sequence": sequence, "status": candidate["status"], "previous_hash": previous_hash, "payload_hash": digest, "actor_ref": actor_ref, "approval_evidence_ref": approval_evidence_ref})
            con.execute("INSERT INTO policy_candidates(policy_id,sequence,status,payload_json,payload_hash,previous_hash,chain_hash,actor_ref,approval_evidence_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (candidate["policy_id"], sequence, candidate["status"], encoded, digest, previous_hash, chain, actor_ref, approval_evidence_ref, utc_now()))
        return {"policy_id": candidate["policy_id"], "sequence": sequence, "status": candidate["status"], "chain_hash": chain}

    def list_policy_candidates(self, *, normal_only: bool = True) -> list[dict[str, Any]]:
        """Candidates are already built from applicability-reviewed evidence.

        The normal-only flag is retained at this boundary so callers cannot
        accidentally opt into special-event evidence by changing defaults.
        """
        self.verify_integrity()
        con = self._connect(read_only=True)
        try:
            values = [json.loads(row[0]) for row in con.execute("SELECT candidate.payload_json FROM policy_candidates AS candidate WHERE candidate.sequence=(SELECT MAX(latest.sequence) FROM policy_candidates AS latest WHERE latest.policy_id=candidate.policy_id) ORDER BY candidate.created_at DESC,candidate.policy_id")]
        finally: con.close()
        if normal_only:
            values = [candidate for candidate in values if all(item["event_classification"] == "normal" for item in candidate["evidence"])]
        return values

    def backup_to(self, destination: str | Path) -> Path:
        destination = Path(destination).resolve(); destination.parent.mkdir(parents=True, exist_ok=True)
        self.verify_integrity()
        source = self._connect()
        try:
            with sqlite3.connect(str(destination)) as target: source.backup(target)
        finally:
            source.close()
        return destination
