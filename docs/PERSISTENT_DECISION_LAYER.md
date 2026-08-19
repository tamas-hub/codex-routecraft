# Persistent Decision Layer: V2/V3 Operations

RouteCraft V2/V3 adds a persistent decision-memory layer around Codex. It does not modify model weights and it is not a substitute for tests or authoritative documentation. It preserves compact, reusable decisions so a new session can start from a verified prior point instead of repeating the same search tree.

## Components

- **Recall** retrieves relevant rules, cases, and candidates under a bounded character budget.
- **Learn** stores verified cases and plausible candidates from a structured JSON packet.
- **Promote** converts a repeated candidate into a validated rule only after a promotion gate passes.
- **Sync** commits, pulls/rebases, and pushes a dedicated private Git store across computers.

The CLI uses only the Python standard library. Git is required only for synchronization.

## Privacy boundary

Do not use the public RouteCraft source repository as your personal memory store. The CLI refuses to write into the bundled store by default.

Create a separate private repository for decision memory. A record may expose project names, root causes, internal constraints, or links even when it contains no source code. The secret scanner is a safety net, not a complete data-loss-prevention system.

## One-computer setup

From the installed plugin directory or a RouteCraft checkout:

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --git-init \
  --configure
```

PowerShell:

```powershell
& .\plugins\codex-routecraft\scripts\routecraft-memory.ps1 init `
  --store "$HOME\routecraft-memory" `
  --git-init `
  --configure
```

This writes the active-store configuration to `~/.codex/routecraft/memory.json` unless `ROUTECRAFT_MEMORY_CONFIG` overrides it.

Store selection precedence is:

1. `--store`
2. `ROUTECRAFT_MEMORY_DIR`
3. configured store
4. bundled read-only seed

## Multi-computer V3 setup

Create an empty **private** GitHub repository first. Keep application source and decision memory in separate repositories.

### First computer

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --git-init \
  --remote git@github.com:OWNER/PRIVATE-MEMORY-REPO.git \
  --configure \
  --auto-sync both

python plugins/codex-routecraft/scripts/routecraft_memory.py sync
```

### Additional computers

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --clone git@github.com:OWNER/PRIVATE-MEMORY-REPO.git \
  --configure \
  --auto-sync both
```

Each machine receives a stable device identifier in the local configuration. Record IDs include UTC time, device identity, and a random suffix to reduce filename collisions during parallel work.

The shared repository tracks only:

- `.routecraft-store.json`
- `README.md`
- `cases/`
- `candidates/`
- `rules/`
- optional templates

Generated search indexes and lock files stay under `.routecraft/` and are ignored. This avoids cross-device conflicts in a single central index file.

## Recall

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py recall \
  --query "state disappears after process restart" \
  --tag persistence \
  --limit 5 \
  --budget 12000
```

For machine-readable output:

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py recall \
  --query "state disappears after process restart" \
  --json
```

Recall scores IDs, titles, tags, scope, evidence, and record bodies. English terms and Japanese character n-grams are supported. Validated rules receive a small priority boost, but current repository evidence always takes precedence.

The output includes only decision-relevant excerpts. Rules emphasize the decision and applicability. Cases emphasize root cause, reusable lesson, and verification. Candidates emphasize the observation, uncertainty, and promotion condition.

## Learn

Codex should produce a compact JSON packet only after the task has been verified.

```json
{
  "kind": "case",
  "title": "Progress reset after app restart",
  "tags": ["expo", "ios", "persistence"],
  "scope": ["react-native"],
  "repository": "owner/repository",
  "outcome": "fixed",
  "sections": {
    "Problem": "Progress disappeared after process restart.",
    "Root cause": "State was written to a volatile cache.",
    "Failed approaches": "The investigation initially focused on the OS update.",
    "Fix": "Moved state to the durable storage adapter.",
    "Verification": "Restarted twice and reran the persistence regression test.",
    "Reusable lesson": "Verify the persistence boundary before blaming the runtime."
  },
  "candidate": {
    "title": "Verify storage durability before broad runtime diagnosis",
    "tags": ["debugging", "persistence"],
    "scope": ["mobile"],
    "sections": {
      "Observation": "Volatile storage can mimic an OS regression.",
      "Possible decision value": "Inspect the storage adapter early.",
      "Counterexamples / uncertainty": "Migration and serialization failures can produce similar symptoms.",
      "Promotion condition": "Confirm in another independent repository."
    }
  }
}
```

Store it:

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py learn \
  --input /tmp/routecraft-learning.json \
  --sync
```

A case may reinforce existing candidates:

```json
{
  "kind": "case",
  "title": "Desktop state disappeared after restart",
  "reinforce_candidates": ["CAND-20260819T120000Z-MAC-A1B2"],
  "sections": {
    "Problem": "Saved state disappeared after restart.",
    "Root cause": "The application used a temporary directory.",
    "Verification": "Restarted the process and inspected the durable path.",
    "Reusable lesson": "Validate the persistence boundary first."
  }
}
```

The CLI adds the new case ID as independent evidence, increments observations only when the evidence is new, and reports candidates that are eligible for promotion.

## Promote

Normal promotion requires at least two observations backed by two captured Case records in the store.

```json
{
  "candidate_id": "CAND-20260819T120000Z-MAC-A1B2",
  "title": "Validate persistence boundaries before runtime diagnosis",
  "decision": "When state disappears after restart, verify the storage path and durability contract before blaming an OS or runtime update.",
  "when_to_apply": "State disappears after restart, suspension, or reboot.",
  "when_not_to_apply": "Evidence already proves a serialization or migration failure.",
  "rationale": "Independent mobile and desktop cases produced the same symptom from volatile storage.",
  "verification": "Restart the process and inspect the resolved durable storage location."
}
```

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py promote \
  --input /tmp/routecraft-promotion.json \
  --sync
```

The exceptional path requires all of the following:

- authoritative evidence;
- at least one evidence reference;
- `--authoritative`;
- `--human-approved`.

RouteCraft must not use the exceptional path autonomously.

## Sync behavior

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py sync --mode both
```

`both` performs:

1. validation;
2. staging of direct Markdown records/templates in known memory paths only;
3. a local commit when changes exist;
4. pull with rebase when the remote branch exists;
5. push with bounded retry;
6. local index rebuild.

`pull` requires a clean store. `push` commits and pushes, pulling/rebasing only after a rejected push.

The store must be the root of a dedicated Git repository. Sync refuses to operate when the store is merely a subdirectory of an application repository.

## Safety controls

- Common secret/token/private-key patterns are rejected.
- Only direct Markdown records/templates and approved root files are staged.
- A sentinel file identifies a RouteCraft store.
- Symlinks, non-Markdown payloads, Git remote-helper syntax, oversized records, and option-like Git inputs are rejected.
- Lock files reduce concurrent local writes.
- Generated indexes are not shared through Git.
- Current tests and repository evidence override remembered rules.
- Promotion is gated and candidates remain visibly provisional.

## Operational commands

```sh
# Inspect configuration, counts, promotion candidates, and Git state
python plugins/codex-routecraft/scripts/routecraft_memory.py status --json

# Validate all records
python plugins/codex-routecraft/scripts/routecraft_memory.py validate

# Rebuild the local search index
python plugins/codex-routecraft/scripts/routecraft_memory.py reindex

# Generate a human-readable index for inspection
python plugins/codex-routecraft/scripts/routecraft_memory.py reindex --markdown
```

## What V3 does not claim

V3 does not prove that prompt-cache reuse or quota consumption will improve by a fixed percentage. Its direct target is repeated search and repeated reasoning. Measure cache behavior, elapsed time, tool calls, failed hypotheses, and outcome quality separately.
