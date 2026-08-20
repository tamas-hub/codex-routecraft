# Memory effectiveness evaluation

RouteCraft can measure whether persistent decision memory actually improves work instead of assuming that retrieval is useful.

## Local-only evaluation

Use `scripts/routecraft_evaluation.py`. Evaluation is opt-in and stores sanitized events under `~/.codex/routecraft/evaluation/` by default. These events are local-only and are not synchronized through the Decision Store.

The evaluator must never store raw prompts, recall queries, conversations, source code, logs, credentials, absolute user paths, or secrets.

## Start a measured task

For a substantive task, run:

```text
python <plugin>/scripts/routecraft_evaluation.py start --repo-path . --task-class <class> --risk <risk> --json
```

If `tracking` is false, continue normally and do not add evaluation overhead.

If tracking is true, preserve the returned `task_id` and obey the returned experiment `mode`:

- `off`: do not use persistent memory recall or learning for this task. RouteCraft orchestration and verification still apply.
- `recall`: use bounded recall, but do not create/reinforce/promote memory records from this task.
- `full`: use normal bounded recall plus verified post-task learning.

Do not override an assigned experiment mode merely because another mode is more convenient. The point of an opt-in experiment is to produce a baseline.

## Record recall without storing the query

After a bounded recall, record only returned record IDs and ranks:

```text
python <plugin>/scripts/routecraft_evaluation.py recall \
  --task-id <TASK_ID> \
  --store <DECISION_STORE> \
  --record <RECORD_ID>:1 \
  --record <RECORD_ID>:2 \
  --json
```

The evaluator loads repository/device metadata from the records themselves. It does not persist the raw recall query.

## Classify recalled memory after verification

At task completion, classify only what current evidence established:

- `useful`: materially shortened or improved the verified route/solution.
- `misleading`: pushed the task toward an incorrect path before current evidence corrected it.
- `stale`: contradicted current evidence and was correctly rejected as outdated.
- omitted: neutral/not materially used.

Example:

```text
python <plugin>/scripts/routecraft_evaluation.py finish \
  --task-id <TASK_ID> \
  --outcome success \
  --useful-record <CASE_ID> \
  --stale-record <RULE_ID> \
  --learned-record <NEW_CASE_ID> \
  --json
```

Elapsed time is computed from task start when not supplied explicitly. Record tool-call counts, failed-hypothesis counts, source character counts, or memory-record character counts only when they are directly observable. Omit them rather than guessing.

## Scorecard

`summary` reports:

- useful recall task rate;
- observed precision from post-verification usefulness labels;
- MRR of the first useful recalled record;
- useful cross-project and cross-device transfer;
- `off` versus `recall` / `full` time, tool-call, and failed-hypothesis comparisons;
- estimated saved seconds when a task-class baseline exists;
- optional Decision Compression Ratio;
- local privacy-integrity violations;
- a coverage-aware RouteCraft Memory Score.

The 100-point score is withheld while score coverage is below 50%. A provisional score may be computed internally, but it must not be presented as an established effectiveness claim without adequate coverage and sample size.

## Retrieval benchmark

Live usefulness is not the same as standard retrieval recall. For Precision@K, Recall@K, Hit@K, and MRR, create a local benchmark suite with expected record IDs and run:

```text
python <plugin>/scripts/routecraft_evaluation.py benchmark \
  --store <DECISION_STORE> \
  --suite <LOCAL_SUITE.json> \
  --limit 5 \
  --json
```

The benchmark suite may contain private query text, so keep it local. Only aggregate benchmark metrics are persisted to `benchmark-last.json`.

## Experimental modes

Evaluation defaults to disabled. Enable normal measurement:

```text
python <plugin>/scripts/routecraft_evaluation.py configure --enable --mode full --json
```

For deliberate A/B/C trials:

```text
python <plugin>/scripts/routecraft_evaluation.py configure \
  --enable \
  --experiment round-robin \
  --sequence off recall full \
  --json
```

Use round-robin experiments only when temporarily accepting baseline tasks without memory is appropriate. Do not enable them for urgent or safety-critical work merely to collect metrics.

## Evaluation precedence

Evaluation must not change acceptance criteria. Current code, tests, authoritative documentation, and reproducible runtime evidence remain above memory. Metrics are evidence about the memory system, not a reason to preserve a bad recalled decision.
