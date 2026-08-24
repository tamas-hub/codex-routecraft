"""Explicit fake host boundary for graph-kernel execution tests only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from routecraft_graph import EXECUTION_BOUNDARY_VERSION, ExecutorBinding, GraphEngine
from routecraft_graph.canonical import sha256


@dataclass(frozen=True)
class _Claim:
    graph_id: str
    revision: int
    node_id: str
    attempt: int
    input_hash: str | None


@dataclass(frozen=True)
class _Attestation:
    claim: _Claim
    digest: str


class FakeTrustedExecutionBoundary:
    """Test double that binds an opaque attestation to one claimed attempt."""

    boundary_version = EXECUTION_BOUNDARY_VERSION

    @staticmethod
    def resolve_executor(graph: dict[str, Any], node: dict[str, Any]) -> ExecutorBinding:
        return ExecutorBinding(
            boundary_version=EXECUTION_BOUNDARY_VERSION,
            executor_id=f"test:{node['node_type'].lower()}",
            node_type=node["node_type"],
            capability_profile=node["capability_profile"],
            allowed_tools=tuple(node["allowed_tools"]),
            denied_operations=tuple(node["denied_operations"]),
            write_scopes=tuple(node["ownership"]["write_scopes"]),
            risk_limit=node["risk"],
        )

    @staticmethod
    def begin_attempt(
        graph: dict[str, Any], node: dict[str, Any], executor: ExecutorBinding
    ) -> _Claim:
        return _Claim(graph["graph_id"], graph["graph_revision"], node["node_id"], node["attempt"], node["input_hash"])

    @staticmethod
    def _digest(
        claim: _Claim,
        output: Any,
        evidence: list[dict[str, Any]],
        gate_result: str,
        usage: dict[str, Any] | None,
    ) -> str:
        return sha256({"claim": claim.__dict__, "output": output, "evidence": evidence, "gate_result": gate_result, "usage": usage})

    def attest(
        self,
        graph: dict[str, Any],
        node: dict[str, Any],
        output: Any,
        evidence: list[dict[str, Any]],
        gate_result: str,
        usage: dict[str, Any] | None,
    ) -> _Attestation:
        claim = _Claim(graph["graph_id"], graph["graph_revision"], node["node_id"], node["attempt"], node["input_hash"])
        return _Attestation(claim, self._digest(claim, output, evidence, gate_result, usage))

    def verify_result(
        self,
        graph: dict[str, Any],
        node: dict[str, Any],
        executor: ExecutorBinding,
        claim: object,
        attestation: object,
        output: Any,
        evidence: list[dict[str, Any]],
        gate_result: str,
        usage: dict[str, Any] | None,
    ) -> bool:
        return (
            isinstance(claim, _Claim)
            and isinstance(attestation, _Attestation)
            and attestation.claim == claim
            and claim == _Claim(graph["graph_id"], graph["graph_revision"], node["node_id"], node["attempt"], node["input_hash"])
            and attestation.digest == self._digest(claim, output, evidence, gate_result, usage)
            and executor.executor_id == f"test:{node['node_type'].lower()}"
        )


class TestGraphEngine(GraphEngine):
    """Existing state-machine tests exercise a real explicit fake boundary."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.test_boundary = FakeTrustedExecutionBoundary()
        kwargs.setdefault("execution_boundary", self.test_boundary)
        super().__init__(*args, **kwargs)

    def record_result(
        self,
        graph_id: str,
        node_id: str,
        output: Any,
        evidence: list[dict[str, Any]],
        *,
        gate_result: str = "PASS",
        usage: dict[str, Any] | None = None,
        attestation: object | None = None,
    ) -> dict[str, Any]:
        if attestation is None:
            graph = self.status(graph_id)
            node = next(item for item in graph["nodes"] if item["node_id"] == node_id)
            attestation = self.test_boundary.attest(graph, node, output, evidence, gate_result, usage)
        return super().record_result(
            graph_id, node_id, output, evidence,
            gate_result=gate_result, usage=usage, attestation=attestation,
        )
