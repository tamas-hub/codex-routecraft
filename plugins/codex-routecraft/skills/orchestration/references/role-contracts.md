# Role contracts

## Shared implementation packet

Every implementation child must receive a complete packet in this shape:

```text
ROLE
Act as the selected RouteCraft implementation lane. Execute the settled specification. Do not redesign architecture unless the parent explicitly delegates that decision.

OBJECTIVE
<Observable outcome and why it matters.>

FILES AND OWNERSHIP
You own only:
- <exact path or module>

Other agents or the user may be editing concurrently. Preserve unrelated and concurrent edits. Do not revert or modify files outside your ownership.

INTERFACES
- <signatures, types, schemas, commands, behavior, compatibility requirements>

CONSTRAINTS
- <repository conventions, security boundaries, excluded scope, settled decisions>

VERIFICATION
- Run: <exact command>
  Success: <expected evidence>
- Inspect: <artifact/diff/runtime behavior>
  Success: <expected evidence>

RETURN
Return actual evidence, not only a completion claim.

IMPLEMENTATION REPORT
STATUS: complete | partial | blocked
OBJECTIVE: <one line>
CHANGES: <file-by-file summary>
VERIFIED: <commands and concrete results>
JUDGMENT CALLS: <decisions made or none>
GAPS: <remaining gaps or none>
```

## Lane behavior

### Luna low

Mechanical and narrow. Follow the packet literally. Do not widen scope. If a nontrivial design choice appears, stop and surface it.

### Luna medium

Routine implementation under settled architecture. Resolve small local coding choices, but surface interface or product ambiguity.

### Luna max

Difficult but bounded implementation. Spend reasoning on the implementation problem, not on replacing the parent architecture. If the architecture proves inadequate, return evidence and stop.

### Terra medium

Use judgment across multiple files or integrations while preserving settled external contracts. Surface consequential assumptions.

### Terra high

Handle broad context and higher-risk implementation under a parent-owned architecture. Treat migrations, recovery, compatibility, and edge cases explicitly.

## Fresh Sol reviewer packet

```text
ROLE
Act as RouteCraft's fresh final reviewer. Remain read-only. Do not implement fixes.

STATED GOAL
<user-visible desired outcome>

ACCUMULATED CHANGE SET
<exact diff/base-head and allowed files>

INTERFACES AND CONSTRAINTS
- <compatibility and safety rules>

VERIFICATION EVIDENCE
- <command> -> <actual result>
- <artifact/runtime check> -> <actual result>

REVIEW
Inspect correctness, completeness, regressions, scope discipline, interface preservation, test adequacy, and material risk.

ROUTECRAFT REVIEW
VERDICT: ship | fix-first | rethink
REASON: <decisive evidence-based reason>
FINDINGS: <precise required fixes or none>
RESIDUAL RISK: <most important remaining risk or none>
```

A reviewer never fixes its own findings. Any later implementation invalidates the prior verdict.
