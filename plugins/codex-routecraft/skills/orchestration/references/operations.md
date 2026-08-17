# Operations

## Prerequisites

- Current Codex CLI or Codex desktop app with plugin support.
- GPT-5.6 Sol for the primary session.
- Luna/Terra access if you want cross-model delegation.
- Native multi-agent/subagent support on the active surface.

## Public installation from GitHub

After this repository is published:

```sh
codex plugin marketplace add tamas-hub/codex-routecraft --ref main
codex plugin add codex-routecraft@routecraft
```

Then install the companion custom-agent profiles because Codex plugins may not automatically register bundled agent TOMLs:

```sh
plugin_dir="$(codex plugin list --json | jq -r '.installed[] | select(.pluginId == "codex-routecraft@routecraft") | .source.path')"
sh "$plugin_dir/scripts/install-agents.sh"
```

PowerShell:

```powershell
$plugins = codex plugin list --json | ConvertFrom-Json
$pluginDir = ($plugins.installed | Where-Object { $_.pluginId -eq 'codex-routecraft@routecraft' }).source.path
& "$pluginDir/scripts/install-agents.ps1"
```

Start a fresh Codex task after agent installation so discovery is clean.

## Local development installation

From a cloned checkout:

```sh
codex plugin marketplace add "$(pwd)"
codex plugin add codex-routecraft@routecraft
sh plugins/codex-routecraft/scripts/install-agents.sh
```

PowerShell:

```powershell
codex plugin marketplace add (Get-Location).Path
codex plugin add codex-routecraft@routecraft
& .\plugins\codex-routecraft\scripts\install-agents.ps1
```

## Verify companion agents

```sh
sh plugins/codex-routecraft/scripts/install-agents.sh --check
```

PowerShell:

```powershell
& .\plugins\codex-routecraft\scripts\install-agents.ps1 -Check
```

The installers are namespaced and fail closed on conflicting existing files. Use the explicit force option only after reviewing the conflict; force mode creates a timestamped backup before replacement.

## Validate the repository

```sh
python scripts/verify.py
```

The verifier checks:

- JSON and TOML parsing;
- manifest/marketplace identity;
- expected role/model/effort pins;
- required orchestration contract terms;
- absence of unfinished placeholders.

## Invocation

Start the primary Codex task on Sol / High and prompt:

```text
Use $codex-routecraft:orchestration to implement this task. Declare the RouteCraft plan before task tools, choose the cheapest safe lane, verify the complete diff, and use fresh Sol review only when risk warrants it.
```
