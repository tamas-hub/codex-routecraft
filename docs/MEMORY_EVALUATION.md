# RouteCraft Memory Effectiveness Evaluation

RouteCraft should not assume persistent memory is useful merely because records can be stored and recalled. The evaluation layer measures whether memory actually reduces rediscovery, surfaces useful decisions early, transfers knowledge across repositories/devices, and avoids misleading the current task.

## What is measured

Keep three questions separate:

1. **Does the mechanism work?** Store integrity, recall, sync, cross-device inheritance, secret rejection.
2. **Does retrieval work?** Hit@K, Precision@K, Recall@K, MRR.
3. **Does development improve?** Elapsed time, tool calls, failed hypotheses, verified-use rate, transfer, misleading/stale guidance.

Record count by itself is not an effectiveness metric.

## Privacy boundary

Evaluation is opt-in and local-only. By default it lives at:

```text
~/.codex/routecraft/evaluation/
├─ config.json
├─ events.jsonl
└─ benchmark-last.json
```

The evaluator does not persist raw prompts, recall queries, conversations, source code, raw logs, credentials, secrets, or absolute user paths. Evaluation events are not synchronized through the private Decision Store.

Observatory receives only compact aggregate metrics.

## Enable measurement

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  configure --enable --mode full --json
```

Disable it with `configure --disable`.

## Modes

- `off`: baseline. No persistent recall or learning; RouteCraft planning/routing/verification still applies.
- `recall`: bounded recall is allowed, but the task does not update Decision Memory.
- `full`: normal recall plus verified post-task learning.

For deliberate A/B/C collection:

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  configure --enable --experiment round-robin \
  --sequence off recall full --json
```

Do not enable baseline experiments for urgent or safety-critical work merely to collect metrics.

## Live task measurement

Start:

Free-form task labels are normalized into the fixed evaluation taxonomy; the original label is not persisted. When evaluation is enabled, `start` also opens a local sidecar keyed only by a one-way session hash.

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  start --repo-path . --task-class debugging --risk low --json
```

Record recalled IDs and ranks, not the query:

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  recall --task-id EVAL-... --store ~/routecraft-memory \
  --record CASE-...:1 --record RULE-...:2 --json
```

After parent verification, finish the task and classify recalled records:

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  finish --task-id EVAL-... --outcome success \
  --useful-record CASE-... \
  --stale-record RULE-... \
  --learned-record CASE-... --json
```

Verdicts:

- `useful`: materially improved or shortened the verified path.
- `misleading`: pushed the task toward a wrong path.
- `stale`: contradicted current evidence and was correctly rejected.
- omitted: neutral/not materially used.

Only record tool-call counts, failed-hypothesis counts, or character counts when directly observable. Omit them rather than guessing.

Every tracked task must close. In `full`, use `--learned-record` after a verified manual Learn, or `--skip-reason no_reusable_learning|not_verified|store_unavailable|task_cancelled`. `recall` uses `mode_recall_only`; `off` uses `mode_off`. A first Stop with an open sidecar is blocked so the parent can finish it; hook re-entry is not blocked and the hook never learns automatically. A later `SessionStart` reports unfinished tasks left by previous sessions and asks for verified completion or explicit cancellation with `task_cancelled`.

## Live metrics

`summary --json` reports:

- completed/recall tasks;
- useful task rate;
- observed precision from verified-use feedback;
- MRR of the first useful record;
- misleading and stale counts;
- useful cross-project and cross-device transfer;
- `off` versus `recall`/`full` elapsed time, tool-call and failed-hypothesis comparisons;
- estimated saved seconds when a task-class baseline exists;
- optional Decision Compression Ratio;
- privacy violations;
- a coverage-aware scorecard.

Observed precision is a live-use metric, not standard benchmark Precision@K.

## Retrieval benchmark

Use a local suite with expected record IDs for standard Hit@K / Precision@K / Recall@K / MRR:

```json
{
  "schema_version": 1,
  "cases": [
    {
      "name": "example",
      "query": "symptom and technical terms",
      "tags": ["git"],
      "expected": ["CASE-..."]
    }
  ]
}
```

Run:

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  benchmark --store ~/routecraft-memory \
  --suite ~/.codex/routecraft/evaluation/benchmark.json \
  --limit 5 --json
```

The suite remains local. Only aggregate benchmark values are stored in `benchmark-last.json`.

## RouteCraft Memory Score

The scorecard uses these maximum weights:

| Component | Max |
|---|---:|
| Retrieval quality | 20 |
| Task time reduction | 20 |
| Failed-hypothesis reduction | 15 |
| Cross-project transfer | 10 |
| Cross-device transfer | 10 |
| Memory correctness | 10 |
| Stale resistance | 5 |
| Privacy integrity | 5 |
| Decision compression | 5 |
| Total | 100 |

Missing evidence is marked unavailable, not zero. The public 100-point score is withheld while evaluated coverage is below 50%.

Statuses:

- `insufficient-data`: coverage < 50%
- `provisional`: coverage >= 50%, fewer than 20 completed measured tasks
- `established`: coverage >= 50%, at least 20 completed measured tasks

This prevents a tiny sample from being presented as a mature effectiveness claim.

## Observatory integration

The device heartbeat can include a compact evaluator summary: task counts, useful rate, observed precision, MRR, cross-project/device reuse, estimated saved time, compression ratio, privacy violations, score coverage/status, and aggregate benchmark values.

Do not upload repository names, task IDs, record IDs, queries, prompts, or local paths to Observatory.

## Limits

This is an operational evaluation, not a controlled scientific benchmark. Task difficulty, model changes, repository changes, and agent routing can affect outcomes. Interpret the score together with sample size, coverage, mode-level statistics, and the retrieval benchmark.
