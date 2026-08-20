---
name: memory
description: "Manage and evaluate RouteCraft's private persistent decision store: initialize, inspect, recall, learn, promote, validate, synchronize, and measure effectiveness across computers."
---

# RouteCraft Memory Operations

Use the bundled `../../scripts/routecraft_memory.py` CLI for Decision Store operations. Use `../../scripts/routecraft_evaluation.py` for opt-in local effectiveness measurement. Prefer the shell or PowerShell memory wrapper in the same directory when appropriate.

## Safety contract

- Personal memory must use a separate private store.
- Never write personal records into the bundled public plugin store.
- Never use an application/source repository subdirectory as the synchronized store.
- Never store credentials, tokens, private keys, personal data, full transcripts, or raw logs.
- Current repository evidence and authoritative documentation override remembered decisions.
- Never use the exceptional promotion path without explicit human approval in the current task.
- Evaluation events stay local by default and must not contain raw prompts, recall queries, conversations, source code, logs, secrets, or absolute user paths.

## Store setup

For one computer, initialize a dedicated local store and configure it as active.

For multiple computers, use a separate private Git repository. The first device initializes a local Git store and adds the remote. Later devices clone the same private store. Do not create or use a public remote for personal memory.

After setup, run `status --json` and `validate`.

## Recall

Build a concise query from the task symptom, subsystem, framework/runtime, failure mode, and verification target. Use a small result limit and bounded character budget. Return the matched IDs and explain that they are prior evidence, not current proof.

## Learn

Accept only a structured case/candidate packet produced after verification. Prefer:

- one compact case for a verified non-obvious result;
- an optional nested candidate for a plausible recurring pattern;
- `reinforce_candidates` when a new independent case supports an existing candidate.

Run with `--dry-run` first when the packet contains sensitive project context or broad scope. Report created IDs and promotion eligibility.

## Promote

Use normal promotion only after the CLI gate confirms repeated observations backed by captured Case records. Ensure the rule includes a bounded decision, applicability, counterconditions, rationale, and verification.

The `--authoritative --human-approved` path requires explicit human approval and must never be inferred from silence.

## Sync

Before synchronization, validate the store and inspect Git status. Use `sync --mode both` for normal operation. Report local commit, pull/rebase, push, and any conflict.

Do not resolve semantic record conflicts by blindly choosing one side. Preserve both evidence paths, then reconcile the candidate or rule explicitly.

## Evaluate

Evaluation is optional and local-only. Do not claim that Memory is effective merely from record counts.

Use:

```text
python ../../scripts/routecraft_evaluation.py summary --json
```

when the user asks whether RouteCraft Memory is helping, how much it is reused, or what its score is.

Report the score together with `score_status`, coverage, completed measured tasks, and retrieval benchmark sample size. If `score_status` is `insufficient-data`, state that there is not yet enough evidence for a 100-point effectiveness score.

For deliberate measurement, see `../orchestration/references/memory-evaluation.md`. The supported modes are:

- `off`: no persistent recall or learning, used as a baseline;
- `recall`: bounded recall without updating memory;
- `full`: normal recall and verified learning.

Never fabricate tool calls, failed hypotheses, elapsed time, usefulness labels, or benchmark expectations. Record only observable values and post-verification judgments.
