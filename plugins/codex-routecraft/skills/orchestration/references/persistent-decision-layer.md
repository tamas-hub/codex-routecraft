# Persistent Decision Layer

RouteCraft can use a persistent decision-memory store to avoid repeating expensive discovery across sessions and projects. The memory layer is evidence-aware and retrieval-bounded; it is not an instruction hierarchy above the current repository.

## CLI location

The memory CLI is bundled at:

```text
../../../scripts/routecraft_memory.py
```

relative to this reference file. Shell and PowerShell wrappers are in the same directory.

The active store is resolved in this order:

1. explicit `--store`;
2. `ROUTECRAFT_MEMORY_DIR`;
3. `~/.codex/routecraft/memory.json`;
4. bundled read-only seed.

Personal learning must use a dedicated external store. The CLI refuses to write into the public bundled seed by default.

## Pre-task recall

For a substantial investigation or implementation:

1. Run `status --json` to discover the active store and sync state.
2. Build a compact query from the observable symptom, subsystem, framework/runtime, failure mode, and required verification.
3. Run `recall --limit 5 --budget 12000`.
4. Load only the returned excerpts or exact paths that materially match the current task.
5. Treat all retrieved memory as prior evidence, not truth.
6. Re-verify applicability against the current repository, authoritative documentation, and reproducible tests.

Do not read the complete store or inject a generated full index into context.

If no external store is configured, recall may use the bundled seed, but post-task learning must not attempt to write there.

## Decision precedence

Use this precedence when sources conflict:

1. current reproducible repository/runtime evidence;
2. authoritative current documentation or specification;
3. validated RouteCraft rule with matching scope;
4. verified RouteCraft case;
5. unverified candidate.

A stale rule must be corrected, superseded, or demoted when current evidence contradicts it.

## Post-task learning

After parent verification, capture memory only when the task produced reusable decision value, such as:

- a verified non-obvious root cause;
- a failed path likely to waste future work;
- a reusable verification recipe;
- a recurring decision pattern;
- an integration constraint that materially changed the solution.

Create a compact JSON learning packet in a temporary file and call `learn --input <file>`.

A case may contain:

- a nested new `candidate` when the current task suggests a plausible recurring pattern; or
- `reinforce_candidates` when recall found an existing candidate and the current case supplies independent evidence.

Do not store raw transcripts, full logs, copied source code, generic documentation, credentials, private keys, tokens, passwords, or personal data.

## Promotion

The normal promotion gate is enforced by the CLI:

- at least two observations;
- at least two captured Case records as unique evidence;
- a concrete decision statement;
- explicit applicability and verification guidance.

When `learn` reports `eligible_for_promotion`, promotion may proceed only if the root has inspected the supporting independent evidence and can write a bounded rule that does not overgeneralize.

The exceptional authoritative path requires both `--authoritative` and `--human-approved`. Never use that exceptional path without explicit human approval in the current task.

## Cross-device synchronization

V3 supports a separate private Git repository as the memory store. The CLI stages only known memory paths, commits local records, pulls with rebase, and pushes with bounded retry.

The store must be the root of a dedicated Git repository. Never use an application/source repository subdirectory as the shared store.

Generated search indexes and lock files remain local under `.routecraft/` to avoid cross-device index conflicts.

## Reporting

When memory materially influenced the task, include:

- the recalled rule/case IDs;
- how current evidence confirmed or rejected them;
- the case/candidate/rule IDs created or updated;
- whether synchronization succeeded;
- any memory-store limitation that prevented recall or learning.
