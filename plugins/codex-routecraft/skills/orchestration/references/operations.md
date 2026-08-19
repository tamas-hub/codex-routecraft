# Operations

## Prerequisites

- Current Codex CLI or Codex desktop app with plugin support.
- GPT-5.6 Sol for the primary session.
- Luna/Terra access when cross-model delegation is desired.
- Native multi-agent/subagent support on the active surface.
- Python 3.11 or later for repository verification and persistent memory.
- Git for cross-device memory synchronization.

## Public installation from GitHub

```sh
codex plugin marketplace add tamas-hub/codex-routecraft --ref main
codex plugin add codex-routecraft@routecraft
```

Then install companion custom-agent profiles because Codex plugins may not automatically register bundled agent TOMLs:

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

Start a fresh Codex task after agent installation.

## Local development installation

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

Installers are namespaced and fail closed on conflicting existing files. Force mode creates a timestamped backup before replacement.

## Configure persistent decision memory

The bundled store is a public read-only seed. Create a separate private store before enabling learning.

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --git-init \
  --configure
```

For multiple computers, create an empty private remote repository, then configure the first device with `--remote` and later devices with `--clone`.

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py status --json
python plugins/codex-routecraft/scripts/routecraft_memory.py validate
```

See `docs/PERSISTENT_DECISION_LAYER.md` or `docs/PERSISTENT_DECISION_LAYER.ja.md`.

## Validate the repository

```sh
python scripts/verify.py
python -m unittest discover -s tests -v
```

The verifier checks:

- JSON and TOML parsing;
- manifest/marketplace identity and version;
- expected role/model/effort pins;
- orchestration and persistent-memory contract text;
- memory store sentinel and templates;
- Python compilation;
- absence of unfinished placeholders.

The test suite covers:

- learning, recall, candidate reinforcement, and promotion;
- Japanese retrieval;
- promotion-gate rejection;
- sensitive-data rejection;
- local Git synchronization across two devices;
- refusal to sync a non-dedicated application-repository subdirectory.

## Invocation

Start the primary Codex task on Sol / High and prompt:

```text
Use $codex-routecraft:orchestration to implement this task. Recall relevant prior decisions, declare the RouteCraft plan before task tools, choose the cheapest safe lane, verify the complete diff, and capture reusable verified learning.
```
