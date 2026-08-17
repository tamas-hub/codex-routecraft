# RouteCraft for Codex

**Sol-led adaptive routing for Codex: default to solo, delegate only when it pays, parallelize only independent work, and always verify in the parent.**

RouteCraft is a Codex plugin/skill for software-delivery orchestration across the GPT-5.6 family. The primary Sol session owns architecture and acceptance. Bounded implementation can move to Luna, judgment-heavy implementation can move to Terra, and high-risk changes can receive a fresh Sol review.

The goal is not "use more agents." The goal is **use the cheapest credible lane without duplicating expensive work**.

> Status: v0.1.0 initial public-ready scaffold. Codex multi-agent APIs are evolving; RouteCraft is deliberately capability-aware and fails conservatively when a requested model/effort lane cannot be selected or verified.

## Architecture

```text
                           GPT-5.6 Sol / High
                         Architect + Acceptance
                                  |
                           ROUTECRAFT PLAN
                                  |
              +-------------------+-------------------+
              |                   |                   |
            SOLO              DELEGATE            PARALLEL
              |                   |                   |
           root Sol        cheapest viable       2-3 independent
                              worker lane           workstreams
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
```

## Design principles

1. **Solo first.** A child has orchestration overhead. Small work stays with the root.
2. **Cheapest viable lane.** Luna for bounded work; Terra when judgment/context increases; Sol owns architecture and acceptance.
3. **No duplicate implementation.** A delegated worker substitutes for root implementation; the root verifies rather than redoing it.
4. **Bounded parallelism.** At most three independent workstreams, with explicit file ownership and frozen interfaces.
5. **Parent verification.** Worker reports are claims; the root inspects the actual diff and reruns checks.
6. **Risk-gated review.** Fresh Sol review is extra quality spend, not a default tax.
7. **Capability-aware runtime.** RouteCraft uses direct model/effort overrides when exposed, named custom agents when available, and falls back to solo rather than pretending a cheaper lane was used.

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

OpenAI's current model guidance positions Sol for frontier capability, Terra for intelligence/cost balance, and Luna for efficient high-volume workloads. RouteCraft applies that hierarchy to Codex delivery while treating reasoning effort as a separate quality/cost control.

## Install

### From GitHub

After the repository is published:

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

For the quickest self-install from this checkout:

macOS / Linux:

```sh
sh scripts/setup-local.sh
```

Windows PowerShell:

```powershell
& .\scripts\setup-local.ps1
```

Or run the individual commands manually:

```sh
git clone https://github.com/tamas-hub/codex-routecraft.git
cd codex-routecraft
codex plugin marketplace add "$(pwd)"
codex plugin add codex-routecraft@routecraft
sh plugins/codex-routecraft/scripts/install-agents.sh
python scripts/verify.py
```

Windows PowerShell:

```powershell
git clone https://github.com/tamas-hub/codex-routecraft.git
Set-Location codex-routecraft
codex plugin marketplace add (Get-Location).Path
codex plugin add codex-routecraft@routecraft
& .\plugins\codex-routecraft\scripts\install-agents.ps1
python .\scripts\verify.py
```

## Use

Run the primary task on **GPT-5.6 Sol / High** and use:

```text
Use $codex-routecraft:orchestration to build this feature. Declare the RouteCraft plan before task tools, choose the cheapest safe lane, parallelize only independent work, verify the complete diff, and add fresh Sol review only when risk warrants it.
```

RouteCraft emits a declaration before work:

```text
ROUTECRAFT PLAN
execution: solo | delegate | parallel
lane: root | luna-low | luna-medium | luna-max | terra-medium | terra-high | mixed
review: self | fresh-sol-high
parallelism: 1 | 2 | 3
risk: low | medium | high | critical
reason: ...
```

## What happens when Codex cannot route models

Codex multi-agent tool schemas have changed across builds and surfaces. RouteCraft does not silently claim cross-model routing.

- If spawn-time `model` + `reasoning_effort` are exposed, RouteCraft uses them directly.
- Otherwise, if named `agent_type` is exposed, RouteCraft uses installed namespaced custom agents.
- If neither is available, RouteCraft falls back to solo (or same-model parallelism only for latency) and reports that model/effort lane selection was not verified.

See [Compatibility](plugins/codex-routecraft/skills/orchestration/references/compatibility.md).

## Cost expectations

RouteCraft does **not** promise a fixed savings percentage. Real savings depend on the share of implementation that can move from Sol to Luna/Terra, context size, reasoning effort, retries, parent verification, and review frequency.

The intended savings mechanism is simple:

- keep architecture and acceptance in Sol;
- move only bounded implementation to cheaper lanes;
- avoid repeating the same implementation in the parent;
- avoid fresh review unless risk justifies the additional spend;
- avoid spawning agents for tasks too small to amortize orchestration overhead.

## Safety

RouteCraft is a workflow policy, not a security boundary. Repository sandboxing, tool permissions, approvals, secrets handling, and source-system permissions still come from Codex and your environment. A reviewer requesting read-only behavior is not equivalent to enforced read-only isolation unless the runtime reports that isolation.

See [SECURITY.md](SECURITY.md).

## Repository layout

```text
.agents/plugins/marketplace.json
plugins/codex-routecraft/
  .codex-plugin/plugin.json
  agents/
  scripts/
  skills/orchestration/
    SKILL.md
    references/
scripts/verify.py
```

## Compatibility notes

The public Codex runtime and documentation are evolving. In particular, custom-agent registration and the model-visible spawn schema can vary by surface/version. RouteCraft intentionally separates routing policy from spawn mechanics so it can support both direct overrides and pinned custom roles.

## Acknowledgements

RouteCraft's safety model is independently implemented but is influenced by the broader Codex community's work on selective multi-agent routing, including [DannyMac180/sol-advisor](https://github.com/DannyMac180/sol-advisor). OpenAI's Codex plugin-creator examples and open-source Codex implementation are the primary references for package structure and runtime capability checks.

## License

MIT
