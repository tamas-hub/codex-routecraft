# Execution Graph runtime contract

The Execution Graph is an observe-by-default layer above current RouteCraft routing. It does not replace the root, worker packets, parent verification, fresh review, or Memory Loop.

## Resolve the mode

Use `ROUTECRAFT_GRAPH_MODE` when explicitly configured; otherwise use `observe`.

- `off`: do not create or report a graph observation.
- `observe`: compile and checkpoint Graph IR v1 in the dedicated SQLite Graph State Store, then execute through current routing.
- `enforce`: use the durable scheduler only when versioned config enables it, the task class is allowlisted, and a trusted host execution/evidence boundary resolves every executable node. Missing capability is `ENFORCE_BOUNDARY_UNAVAILABLE`; it never becomes a best-effort execution.

The seven checks are real-model benchmark end-to-end evidence, security rule fixture validation, legacy replacement health, runtime regression, Control Center regression, memory regression, and collector regression.

## Materialize a plan

After `ROUTECRAFT PLAN` and before delegation, create a caller-owned local Graph IR v1 JSON document with:

- `graph_id`, `graph_schema_version`, `graph_revision`, `policy_version`, bounded `task_class`, `mode`, timestamps, and `DRAFT` status;
- one or more `nodes`, each carrying the full Node contract: ID/type/objective/dependencies/ownership, input/output schema, lane/reasoning/risk/capability, allowed/denied operations, verification/gate/retry policy, and unexecuted runtime fields;
- typed edges, Intent Contract, Global Acceptance criteria, finite graph budgets, and structured constraints.

Never place prompts, conversations, source/file contents, absolute paths, secrets, raw worker packets, or raw outputs in graph state.

Validate and durably materialize it with the local runtime. Graph State is stored in RouteCraft's dedicated SQLite directory, physically separate from Memory Local and the Decision Store.

```powershell
python <plugin>/scripts/routecraft.py graph validate `
  --input <caller-work>/graph-ir-v1.json `
  --json

python <plugin>/scripts/routecraft.py graph plan `
  --input <caller-work>/graph-ir-v1.json `
  --mode observe `
  --json

python <plugin>/scripts/routecraft.py graph status `
  --graph-id <graph_id> `
  --json
```

`graph plan` must report a compile checkpoint. The Unified Collector may later project only the schema-v4 aggregate allowlist; the plan command itself does not require Control Center or network access. If observe compilation fails, continue through current routing and report the observation failure; never silently claim an observed plan. If enforce validation or boundary resolution fails, fail closed and require an explicit observe plan rather than executing the invalid graph.

`graph create --state-output` is a deprecated 0.6 compatibility/shadow adapter for old `units` JSON. It is not Graph IR v1, is not the canonical State Store, and must never be used for enforce, checkpoint/resume, or benchmark E/F evidence.

## Observe and compare

In `observe`, current routing is the actual execution. Shadow predictions must come from the validated plan and evidence available at route time. Do not fabricate duration, tokens, retries, rework, quality, or success. Leave unavailable values null and label a structural-only projection as insufficient evidence.

In observe, never claim scheduler execution merely because compilation succeeded. Use `graph export` only for a caller-owned local diagnostic artifact. Do not write Graph state directly to the Decision Store. Only independently verified reusable constraints may pass through the existing post-acceptance Memory gate.
