# RouteCraft Persistent Decision Layer

This directory contains the bundled seed store and record templates for RouteCraft's persistent decision memory.

The objective is not to load more text into every prompt. The objective is to let a new Codex session retrieve a small, relevant set of validated prior decisions before it repeats expensive discovery work.

## Lifecycle

```text
verified work
  -> reusable case
  -> possible recurring pattern (candidate)
  -> independent confirmation
  -> validated decision rule
```

A one-off success is not automatically a global rule. Candidates normally need at least two observations backed by two captured Case records before promotion. An exceptional authoritative path also requires explicit human approval.

## Store classes

- `cases/` — compact records of completed investigations and fixes.
- `candidates/` — plausible but not yet validated patterns.
- `rules/` — validated reusable decision rules.
- `templates/` — human-readable record examples.
- `.routecraft/` — generated local index and lock files.

## Retrieval model

The CLI builds a local programmatic index and returns only the highest-scoring rules/cases under a character budget. The complete store is not injected into the model context.

```sh
python scripts/routecraft_memory.py recall --query "state disappears after restart"
```

## Private external store required for learning

The bundled store is a public, read-only seed. RouteCraft refuses to write personal decision memory into it by default.

Create a dedicated private store:

```sh
python scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --git-init \
  --configure
```

For multiple computers, use a separate **private Git repository** as described in `docs/PERSISTENT_DECISION_LAYER.md`.

## Memory hygiene

Store only material that can change a future decision. Do not store:

- credentials, tokens, private keys, passwords, or personal data;
- full transcripts, raw logs, or complete tool histories;
- copied source code merely for archival purposes;
- generic documentation that is cheaper to retrieve from its authoritative source;
- duplicated or contradicted rules.

Current repository evidence, authoritative documentation, and reproducible tests always override remembered heuristics.
