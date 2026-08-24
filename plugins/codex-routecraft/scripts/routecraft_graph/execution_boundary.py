"""Trusted host boundary required to execute an ``enforce`` graph.

The graph kernel deliberately does not execute models or tools itself.  In
``observe`` it may record a plan, but in ``enforce`` it must not treat a JSON
payload supplied by a caller as proof that a node ran.  A host integration has
to inject this versioned boundary, resolve a declared executor, and attest the
specific attempt/result/evidence tuple it observed.

This is a small interface rather than a provider dependency.  It keeps the
local runtime offline-first while making the missing production adapter an
explicit fail-closed state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


EXECUTION_BOUNDARY_VERSION = 1


@dataclass(frozen=True)
class ExecutorBinding:
    """A resolved host executor constrained to one node contract."""

    boundary_version: int
    executor_id: str
    node_type: str
    capability_profile: str
    allowed_tools: tuple[str, ...]
    denied_operations: tuple[str, ...]
    write_scopes: tuple[str, ...]
    risk_limit: str


@runtime_checkable
class ExecutionBoundary(Protocol):
    """Host-owned trust boundary for an enforce-mode attempt.

    ``claim`` and ``attestation`` are deliberately opaque to the graph kernel.
    A concrete adapter may bind them to a local worker, a signed tool receipt,
    or a provider trace.  The kernel only accepts an attestation after the same
    injected boundary verifies the exact graph/node/attempt/input tuple.
    """

    boundary_version: int

    def resolve_executor(
        self, graph: dict[str, Any], node: dict[str, Any]
    ) -> ExecutorBinding | None: ...

    def begin_attempt(
        self,
        graph: dict[str, Any],
        node: dict[str, Any],
        executor: ExecutorBinding,
    ) -> object | None: ...

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
    ) -> bool: ...
