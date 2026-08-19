# Persistent Decision Layer

RouteCraft may use a persistent decision-memory store to avoid repeating expensive discovery work across sessions and projects.

## Pre-task retrieval

Before implementation begins:

1. Read `../../../intelligence/INDEX.md`.
2. Match the current task against indexed tags, symptoms, subsystems, frameworks, and verification patterns.
3. Load only the smallest relevant set of rules and cases.
4. Treat retrieved memory as prior evidence, not as unquestionable truth.
5. Re-verify applicability against the current repository and runtime before acting.

Do not load the full intelligence store by default.

## Decision precedence

Current repository evidence overrides stale memory. Authoritative documentation and reproducible tests override remembered heuristics. A validated rule has more weight than a candidate, but neither can override contradictory current evidence.

## Post-task learning

After a meaningful task, inspect the work for reusable decision value.

Capture a new case when the task produced at least one of:

- a verified non-obvious root cause;
- a failed path worth avoiding in the future;
- a reusable verification recipe;
- a decision pattern likely to recur;
- an integration constraint that materially changed the implementation.

Create a candidate when a reusable pattern is plausible but not yet independently reproduced.

Do not automatically promote a candidate to a rule. Promotion normally requires at least two independent observations, or one unusually strong authoritative case plus explicit human acceptance.

## Memory hygiene

Do not store:

- secrets, tokens, credentials, private keys, or private user data;
- raw transcripts or full tool logs;
- source code copied merely for archival purposes;
- generic documentation that is cheaper to retrieve from its authoritative source;
- duplicate knowledge already represented by a stronger rule.

Prefer concise decision records. The purpose is to reduce future search and reasoning, not to maximize stored text.

## Index maintenance

Whenever a candidate, rule, or case is added or materially changed, update `../../../intelligence/INDEX.md` with a one-line description, tags, and a relative link. Keep the index compact enough to read routinely.

## Reporting

When retrieved memory materially influenced the route or implementation, mention which rule/case was used in the completion report. When new reusable memory was produced, report whether it was stored as a case, candidate, or promoted rule.
