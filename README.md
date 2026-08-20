# RouteCraft for Codex

**Sol-led adaptive routing plus persistent decision memory for Codex. Default to solo, delegate only when it pays, parallelize only independent work, verify in the parent, and let future sessions reuse validated decisions.**

RouteCraft is a Codex plugin/skill for software-delivery orchestration across the GPT-5.6 family. The primary Sol session owns architecture and acceptance. Bounded implementation can move to Luna, judgment-heavy implementation can move to Terra, and high-risk changes can receive a fresh Sol review.

V0.5.1 adds an explicitly opt-in, windowless Windows notification-area host for Observatory heartbeat. It starts once at sign-in, sends from the long-lived background process, and shows green ON, gray OFF, or orange delivery-error state in a small tray icon. It does not create a five-minute Scheduled Task, and the tray context menu provides pause/resume and send-now controls.

V0.4.0 adds an opt-in, private-by-default Source Guard for every Codex task on a managed device. It preserves pre-existing dirty work, injects the GitHub source-of-truth policy at session start, and prevents a task from stopping while its own durable source changes remain uncommitted or unpushed. Raw transcripts, credentials, `.env` files, databases, uploads, caches, and device-local configuration remain local. See [multi-device fleet operations](docs/MULTI_DEVICE_FLEET.md) for setup and the one-time hook trust review required on each device.

V0.3.0 adds a Persistent Decision Layer:

- retrieve relevant prior rules and cases before repeating investigation;
- store verified cases and provisional candidates after meaningful work;
- promote only repeatedly observed candidates into validated rules;
- synchronize a separate private memory repository across computers.

The goal is not "use more agents" or "store more text." The goal is **use the cheapest credible lane and avoid paying the same search/reasoning cost twice**.

> Status: v0.5.1. Codex multi-agent APIs are evolving; RouteCraft is capability-aware and fails conservatively when a requested model/effort lane cannot be selected or verified.

## Architecture

```text
                          Private Decision Store
                    Rules / Cases / Candidates / Evidence
                                   |
                           bounded recall only
                                   |
                           GPT-5.6 Sol / High
                         Architect + Acceptance
                                   |
                            ROUTECRAFT PLAN
                                   |
              +--------------------+--------------------+
              |                    |                    |
            SOLO               DELEGATE             PARALLEL
              |                    |                    |
           root Sol         cheapest viable        2-3 independent
                               worker lane            workstreams
                                   |
                  +----------------+----------------+
                  |                                 |
               Luna                             Terra
         low / medium / max                medium / high
                  |                                 |
                  +----------------+----------------+
                                   |
                          Parent Sol verifies
                      complete diff + tests + scope
                                   |
                          high-risk changes only
                                   |
                        Fresh Sol / High review
                                   |
                          verified learning packet
                                   |
                       Case -> Candidate -> Rule
```

## Design principles

1. **Solo first.** A child has orchestration overhead. Small work stays with the root.
2. **Cheapest viable lane.** Luna for bounded work; Terra when judgment/context increases; Sol owns architecture and acceptance.
3. **No duplicate implementation.** A delegated worker substitutes for root implementation; the root verifies rather than redoing it.
4. **Bounded parallelism.** At most three independent workstreams, with explicit file ownership and frozen interfaces.
5. **Parent verification.** Worker reports and memory records are claims; the root inspects current evidence and reruns checks.
6. **Risk-gated review.** Fresh Sol review is extra quality spend, not a default tax.
7. **Capability-aware runtime.** RouteCraft falls back to solo rather than pretending a cheaper model lane was used.
8. **Recall before rediscovery.** Retrieve only the smallest relevant prior decision surface.
9. **Evidence-gated learning.** One successful fix stays a case/candidate until independent evidence supports a rule.
10. **Private cross-device store.** Personal decision memory is isolated from public source repositories.

## Lane guide

| Lane | Best for |
|---|---|
| `root` (Sol / High) | architecture, unresolved ambiguity, acceptance, critical decisions |
| `luna-low` | tiny mechanical, low-risk bounded edits |
| `luna-medium` | routine feature/fix implementation |
| `luna-max` | difficult but fully specified implementation |
| `terra-medium` | multi-file/context-heavy work with moderate judgment |
| `terra-high` | broad/high-risk integration under settled architecture |
| `fresh-sol-high` | independent review of consequential changes |

## Install

### From GitHub

```sh
codex plugin marketplace add tamas-hub/codex-routecraft --ref main
codex plugin add codex-routecraft@routecraft
```

Custom-agent profiles currently need a companion install step on Codex builds that do not register agent TOMLs from a plugin package.

macOS / Linux:

```sh
plugin_dir="$(codex plugin list --json | jq -r '.installed[] | select(.pluginId == "codex-routecraft@routecraft") | .source.path')"
sh "$plugin_dir/scripts/install-agents.sh"
```

Windows PowerShell:

```powershell
$plugins = codex plugin list --json | ConvertFrom-Json
$pluginDir = ($plugins.installed | Where-Object { $_.pluginId -eq 'codex-routecraft@routecraft' }).source.path
& "$pluginDir/scripts/install-agents.ps1"
```

Start a fresh Codex task after installation.

### Local checkout

macOS / Linux:

```sh
sh scripts/setup-local.sh
```

Windows PowerShell:

```powershell
& .\scripts\setup-local.ps1
```

## Configure a private decision store

The bundled store is a public read-only seed. RouteCraft refuses to write personal memory into it by default.

One computer:

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --git-init \
  --configure
```

For multiple computers, create an empty **private GitHub repository** first.

First computer:

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --git-init \
  --remote git@github.com:OWNER/PRIVATE-MEMORY-REPO.git \
  --configure \
  --auto-sync both

python plugins/codex-routecraft/scripts/routecraft_memory.py sync
```

Additional computers:

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --clone git@github.com:OWNER/PRIVATE-MEMORY-REPO.git \
  --configure \
  --auto-sync both
```

See [Persistent Decision Layer](docs/PERSISTENT_DECISION_LAYER.md) and the [Japanese guide](docs/PERSISTENT_DECISION_LAYER.ja.md).

## Use RouteCraft

Run the primary task on **GPT-5.6 Sol / High** and use:

```text
Use $codex-routecraft:orchestration to build this feature. Recall relevant prior decisions, declare the RouteCraft plan before task tools, choose the cheapest safe lane, parallelize only independent work, verify the complete diff, and capture reusable verified learning.
```

RouteCraft emits:

```text
ROUTECRAFT PLAN
execution: solo | delegate | parallel
lane: root | luna-low | luna-medium | luna-max | terra-medium | terra-high | mixed
review: self | fresh-sol-high
parallelism: 1 | 2 | 3
risk: low | medium | high | critical
reason: ...
```

For store administration, Codex can also use:

```text
Use $codex-routecraft:memory to inspect, validate, recall, or synchronize my private RouteCraft decision store.
```

## Memory CLI

```sh
# Retrieve a bounded relevant decision surface
python plugins/codex-routecraft/scripts/routecraft_memory.py recall \
  --query "state disappears after restart" \
  --limit 5 \
  --budget 12000

# Store a verified case/candidate packet
python plugins/codex-routecraft/scripts/routecraft_memory.py learn \
  --input docs/examples/case-packet.json

# Promote a repeatedly observed candidate
python plugins/codex-routecraft/scripts/routecraft_memory.py promote \
  --input docs/examples/promotion-packet.json

# Inspect and validate
python plugins/codex-routecraft/scripts/routecraft_memory.py status --json
python plugins/codex-routecraft/scripts/routecraft_memory.py validate
```

The local search index may contain the complete searchable text, but it stays outside model context. Recall returns only a few decision-relevant excerpts under the requested budget.

## Learning lifecycle

```text
verified task
   -> Case
   -> possible recurring pattern (Candidate)
   -> second independent case reinforces Candidate
   -> promotion gate passes
   -> Validated Rule
```

Normal promotion requires at least two observations backed by two captured Case records. The exceptional authoritative path also requires explicit human approval and must not be used autonomously.

## What happens when Codex cannot route models

Codex multi-agent tool schemas vary across builds and surfaces. RouteCraft does not silently claim cross-model routing.

- If spawn-time `model` + `reasoning_effort` are exposed, RouteCraft uses them directly.
- Otherwise, if named `agent_type` is exposed, RouteCraft uses installed namespaced custom agents.
- If neither is available, RouteCraft falls back to solo (or same-model parallelism only for latency) and reports that model/effort lane selection was not verified.

See [Compatibility](plugins/codex-routecraft/skills/orchestration/references/compatibility.md).

## Cost and cache expectations

RouteCraft does **not** promise a fixed savings percentage or a specific prompt-cache hit rate. Real usage depends on task shape, context stability, retries, model routing, parent verification, and platform quota accounting.

The intended mechanisms are:

- move bounded implementation away from Sol only when credible;
- avoid duplicate parent/child implementation;
- retrieve prior decisions instead of repeating the same investigation;
- preserve failed paths and verification recipes;
- keep the always-loaded surface small;
- synchronize reusable knowledge rather than raw transcripts.

Measure cache reuse, elapsed time, tool calls, rejected hypotheses, rework, and outcome quality separately.

## Safety

RouteCraft is a workflow policy, not a security boundary. Repository sandboxing, tool permissions, approvals, secrets handling, and source-system permissions still come from Codex and your environment.

The memory CLI rejects common token/private-key patterns and oversized record bodies, stages only direct Markdown records/templates in known memory paths, rejects Git remote-helper syntax and symlinks, and refuses to sync a store that is merely a subdirectory of an application repository. These are safeguards, not complete data-loss prevention.

See [SECURITY.md](SECURITY.md).

## Repository layout

```text
.agents/plugins/marketplace.json
plugins/codex-routecraft/
  .codex-plugin/plugin.json
  agents/
  intelligence/
    cases/
    candidates/
    rules/
    templates/
  scripts/
    routecraft_memory.py
    routecraft-memory.sh
    routecraft-memory.ps1
  skills/orchestration/
    SKILL.md
    references/
docs/
  PERSISTENT_DECISION_LAYER.md
  PERSISTENT_DECISION_LAYER.ja.md
tests/
  test_routecraft_memory.py
scripts/verify.py
```

## Acknowledgements

RouteCraft's safety model is independently implemented but is influenced by the broader Codex community's work on selective multi-agent routing, including DannyMac180/sol-advisor. OpenAI's Codex plugin-creator examples and open-source Codex implementation are the primary references for package structure and runtime capability checks.

## License

MIT
