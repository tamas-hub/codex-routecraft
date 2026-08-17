# Installation guide

## Recommended setup

Use GPT-5.6 Sol / High for the primary Codex session. RouteCraft itself does not overwrite your global Codex model settings.

### 1. Install the marketplace and plugin

When published on GitHub:

```sh
codex plugin marketplace add tamas-hub/codex-routecraft --ref main
codex plugin add codex-routecraft@routecraft
```

### 2. Install companion custom agents

Plugins may not automatically register custom agent TOMLs on all Codex builds, so RouteCraft ships a separate safe installer.

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

### 3. Restart the task

Start a fresh Codex task so the new agent profiles are discovered.

### 4. Verify

From a cloned repository:

```sh
python scripts/verify.py
sh plugins/codex-routecraft/scripts/install-agents.sh --check
```

Windows:

```powershell
python .\scripts\verify.py
& .\plugins\codex-routecraft\scripts\install-agents.ps1 -Check
```

## Conflicts

Agent names are prefixed with `routecraft_` to reduce collision risk. The installer refuses to overwrite a different existing file by default.

If you intentionally want to replace a conflicting RouteCraft profile:

macOS/Linux:

```sh
sh plugins/codex-routecraft/scripts/install-agents.sh --force
```

PowerShell:

```powershell
& .\plugins\codex-routecraft\scripts\install-agents.ps1 -Force
```

Force mode creates a timestamped backup first.

## Uninstall

Remove the plugin with your Codex plugin management command, then delete only the namespaced files you installed from `~/.codex/agents/`:

- routecraft_luna_low.toml
- routecraft_luna_medium.toml
- routecraft_luna_max.toml
- routecraft_terra_medium.toml
- routecraft_terra_high.toml
- routecraft_sol_reviewer.toml

Do not delete unrelated agent profiles.
