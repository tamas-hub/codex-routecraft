---
name: orchestration
description: "Sol-led adaptive Codex orchestration with cheapest-viable delegation, bounded parallelism, parent verification, risk-gated fresh Sol review, and persistent decision recall/learning."
---

# RouteCraft Orchestration

Act as the root architect and acceptance owner. Preserve the user's intent, choose the architecture, retrieve relevant prior decisions, decide whether delegation is worthwhile, assign exact ownership, verify the accumulated change set, and accept or reject the result.

Read these references before the first delegation:

- `references/routing-policy.md` for lane selection and risk gates.
- `references/role-contracts.md` for worker and reviewer packets.
- `references/compatibility.md` for capability-dependent spawning.
- `references/operations.md` for installation and runtime checks.
- `references/persistent-decision-layer.md` for bounded recall, learning, promotion, and cross-device synchronization.

## Root contract

Use GPT-5.6 Sol with high reasoning for the primary session when the surface permits explicit selection. If runtime metadata exposes the effective root model and effort, verify them. If the surface cannot prove the active root settings, do not claim proof; rely on the user's selected session configuration and report that limitation only when it materially affects routing.

The root owns:

- requirement resolution and material ambiguity;
- architecture, interfaces, and decomposition;
- route and lane selection;
- persistent-decision retrieval and applicability checks;
- child ownership boundaries;
- complete diff inspection;
- rerunning requested verification;
- escalation and review decisions;
- final acceptance;
- post-task extraction of reusable decision knowledge.

## Recall before rediscovering

Before substantial investigation or implementation, follow `references/persistent-decision-layer.md` and use the bundled `scripts/routecraft_memory.py` CLI when available.

Run a bounded recall using the task symptom, subsystem, technologies, and verification target. Load only relevant returned excerpts. Do not load the complete memory store by default.

Retrieved memory is prior evidence, not truth. Current repository evidence, current authoritative documentation, and reproducible tests take precedence. Preserve recalled record IDs for the completion report when they materially affect the route or solution.

## Declare the route before task tools

Before the first task tool call, emit exactly one declaration in this shape:

```text
ROUTECRAFT PLAN
execution: solo | delegate | parallel
lane: root | luna-low | luna-medium | luna-max | terra-medium | terra-high | mixed
review: self | fresh-sol-high
parallelism: 1 | 2 | 3
risk: low | medium | high | critical
reason: <short task-specific rationale>
```

Default to `solo`. Delegation must have a concrete benefit: lower expected cost, materially better specialization, or meaningful latency reduction through independent parallel work.

A later declaration may only escalate cost, capability, review strength, or parallelism when newly observed evidence justifies it. Never silently downgrade safeguards after discovering higher risk.

## Select the cheapest viable lane

Choose the least expensive lane that can complete the bounded implementation reliably:

- `luna-low`: tiny, mechanical, low-risk edits with settled behavior.
- `luna-medium`: routine bounded implementation with clear tests and interfaces.
- `luna-max`: difficult but fully specified implementation where architecture is already settled.
- `terra-medium`: multi-file or context-heavy work requiring moderate judgment.
- `terra-high`: judgment-heavy, high-blast-radius, migration-like, or difficult integration work under a settled architecture.
- `root`: architecture, unresolved product decisions, critical security decisions, or work whose delegation overhead exceeds the likely savings.

Do not choose a cheaper lane merely because it is cheaper. If a child would need to rediscover architecture or resolve major ambiguity, keep that work in the root or choose a stronger lane.

## Avoid delegation overhead

Use `solo` when the task is small enough that writing a worker packet, spawning a child, and re-verifying the result would likely cost more than direct root implementation.

Auxiliary implementation substitutes for root implementation. Do not have the root independently reimplement the same solution. Parent verification is required, but duplicate implementation is waste.

## Parallelize only independent work

Use `parallel` only when all of these are true:

1. there are at least two meaningful workstreams;
2. owned files are disjoint or interfaces are frozen first;
3. children do not need each other's intermediate output;
4. merge/reconciliation cost is lower than the expected latency benefit;
5. each child can receive a complete bounded packet.

Default maximum parallelism is 3. Do not exceed 3 in this skill. Prefer 2 unless the third workstream is clearly independent.

## Capability-aware spawning

Before spawning, inspect the available spawn tool schema and follow `references/compatibility.md`.

Preferred order:

1. If direct `model` and `reasoning_effort` overrides are exposed, spawn a fresh generic child with the chosen model/effort and include the full role packet.
2. Otherwise, if named `agent_type` selection is exposed, use the matching installed RouteCraft role from `agents/*.toml` with `fork_turns: none`.
3. If neither mechanism can prove a different child model/effort, do not pretend routing occurred. Use `solo`, or use a same-model child only for latency when that is explicitly beneficial and report that no cost lane change was verified.

Never silently substitute a stronger or weaker model than the declared lane.

## Worker ownership

Every implementation child receives the exact packet from `references/role-contracts.md`, including:

- OBJECTIVE
- FILES AND OWNERSHIP
- INTERFACES
- CONSTRAINTS
- VERIFICATION
- RETURN

Children must preserve concurrent edits, avoid unrelated files, and surface ambiguity instead of widening scope.

Relevant recalled rules/cases may be included in a worker packet only when they apply directly to that worker's bounded scope. Do not forward the entire recall result automatically.

## Parent verification is mandatory

Treat every worker report and every retrieved memory record as a claim. In the root session:

1. inspect the complete working-tree diff or exact base/head comparison;
2. confirm changed files match ownership;
3. rerun the requested tests/checks;
4. inspect generated artifacts or runtime evidence when relevant;
5. resolve integration conflicts explicitly;
6. decide whether new risk requires escalation or fresh review;
7. confirm that any recalled rule actually matched the current evidence.

A child saying "done" and a memory record saying "validated" are never sufficient evidence by themselves.

## Risk-gated fresh Sol review

Set `review: fresh-sol-high` when independent scrutiny is worth the additional cost. Strong triggers include:

- authentication, authorization, secrets, cryptography, payments, or personal data;
- schema/data migrations or destructive operations;
- public API or persistence-format changes;
- broad refactors with wide blast radius;
- concurrency or recovery logic with difficult failure modes;
- changes where a regression would be expensive to detect after release.

For review, use a fresh context with `fork_turns: none`. Prefer the installed `routecraft_sol_reviewer` role when selectable. If read-only sandbox enforcement cannot be observed, instruct behavioral read-only review and capture before/after repository state. Never claim hard read-only isolation without evidence.

The reviewer returns exactly one verdict: `ship`, `fix-first`, or `rethink`.

Any implementation change after a review invalidates that verdict. Re-verify and obtain a fresh review when the route still requires one.

## Learn after verified meaningful work

After verification, follow `references/persistent-decision-layer.md`.

When the task produced reusable decision value, create a compact learning packet and invoke `scripts/routecraft_memory.py learn`. Store verified facts as a case. Store a plausible but not yet repeated pattern as a candidate. Reinforce an existing candidate only when the current task supplies independent evidence.

Do not store secrets, personal data, raw transcripts, full logs, copied source code, or generic documentation. Do not write personal memory into the bundled public plugin store.

When a candidate becomes eligible for promotion, inspect the evidence and promote only a bounded rule that survives counterexamples. Never invoke the exceptional `--authoritative --human-approved` path without explicit human approval.

## Completion report

End with:

- declared route and any escalation;
- files changed;
- exact verification performed and outcome;
- reviewer verdict when used;
- persistent rule/case IDs that materially influenced the work;
- new case/candidate/rule IDs captured or updated;
- memory synchronization outcome when attempted;
- residual risk or compatibility limitations;
- whether the intended child model/effort was verified or only requested.
