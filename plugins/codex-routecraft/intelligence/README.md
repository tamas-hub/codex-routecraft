# RouteCraft Persistent Decision Layer

This directory defines the portable, reusable decision-memory layer used by RouteCraft.

The goal is not to stuff more text into every prompt. The goal is to let a new Codex session start from validated prior decisions when they are relevant.

## Design principles

1. Keep the always-loaded surface small.
2. Retrieve only task-relevant memory.
3. Separate observations from validated rules.
4. Preserve evidence and provenance.
5. Promote repeated patterns; do not turn one-off anecdotes into global rules.
6. Prefer decisions, failure modes, and verification recipes over raw logs.
7. Never store secrets, credentials, personal data, or proprietary content that does not belong in the repository.

## Memory classes

- `candidates/` — unverified observations and possible patterns.
- `rules/` — validated reusable decision rules.
- `cases/` — compact records of completed investigations and fixes.
- `templates/` — canonical formats for new entries.
- `INDEX.md` — compact retrieval map. Keep this much smaller than the full store.

## Promotion lifecycle

```text
observation
  -> candidate
  -> repeated independent observation
  -> validated rule
  -> optionally promoted into always-on orchestration guidance
```

A rule should normally require at least two independent cases, or one exceptionally strong case with direct authoritative evidence and explicit human acceptance.

## What belongs here

Good memory changes a future decision. Examples:

- a root-cause signature that reliably narrows debugging;
- a failed repair strategy and the conditions under which it fails;
- a verification sequence that catches a recurring regression;
- a repository pattern that changes routing or review strength;
- an integration constraint that repeatedly matters across projects.

Raw transcripts, full tool logs, generic documentation, marketing copy, and duplicated source code do not belong here.

## Retrieval budget

RouteCraft should read `INDEX.md` first, then load only the smallest set of relevant rules/cases. Avoid loading the entire store into context.
