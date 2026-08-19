# Installation

## Requirements

- Git
- Python 3.11+
- Codex CLI or desktop build with plugin support
- GPT-5.6 Sol for the primary session
- Luna/Terra access only when cross-model delegation is desired

## Install from GitHub

```sh
codex plugin marketplace add tamas-hub/codex-routecraft --ref main
codex plugin add codex-routecraft@routecraft
```

Install bundled custom-agent profiles.

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

## Install from a local checkout

macOS / Linux:

```sh
sh scripts/setup-local.sh
```

Windows PowerShell:

```powershell
& .\scripts\setup-local.ps1
```

## Create a private decision store

The public plugin store is read-only for personal learning.

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

For three or more computers, create an empty private GitHub repository. Configure the first device with `--remote`, then use `--clone` on later devices. Full commands are in:

- `docs/PERSISTENT_DECISION_LAYER.md`
- `docs/PERSISTENT_DECISION_LAYER.ja.md`

## Verify

```sh
python scripts/verify.py
python -m unittest discover -s tests -v
```

Agent-profile checks:

```sh
sh plugins/codex-routecraft/scripts/install-agents.sh --check
```

PowerShell:

```powershell
& .\plugins\codex-routecraft\scripts\install-agents.ps1 -Check
```
