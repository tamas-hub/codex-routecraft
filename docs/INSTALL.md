# Installation

## RouteCraft Memory Local v1.0

Memory Local can be used from the repository without installing the Codex plugin. It requires Python 3.11 or later; Git is optional and only needed for repository status.

Windows PowerShell:

```powershell
$env:PYTHONUTF8 = '1'
python .\plugins\codex-routecraft\scripts\routecraft.py --version
python .\plugins\codex-routecraft\scripts\routecraft.py init
python .\plugins\codex-routecraft\scripts\routecraft.py ui
```

macOS:

```sh
python3 ./plugins/codex-routecraft/scripts/routecraft.py --version
python3 ./plugins/codex-routecraft/scripts/routecraft.py init
python3 ./plugins/codex-routecraft/scripts/routecraft.py ui
```

The release ZIP contains platform launchers and the same Python source. It does not install a service, background watcher, login item, runtime, or account. See `release/README-JA.md` and `release/UNINSTALL-JA.md`.

When the Codex RouteCraft plugin is also installed, enable the opt-in project-memory bridge after registering the repository:

```powershell
python .\plugins\codex-routecraft\scripts\routecraft.py project add --name "My project" --repo "C:\path\to\repo"
python .\plugins\codex-routecraft\scripts\routecraft.py loop configure --enable --context-profile compact
python .\plugins\codex-routecraft\scripts\routecraft.py loop status
```

The bridge remains OFF until configured, never auto-registers a repository, and needs a new Codex task after plugin reinstall. Disable it without deleting local data with `routecraft loop configure --disable`.

## Requirements

- Git
- Python 3.11+
- Codex CLI or desktop build with plugin support
- GPT-5.6 Sol for the primary session
- Luna/Terra access only when cross-model delegation is desired

## Install from the audited GitHub tag

```sh
codex plugin marketplace add tamas-hub/codex-routecraft --ref v0.7.3
codex plugin add codex-routecraft@routecraft
```

Do not use mutable `main` for a production or second-device install. Prefer the platform starter ZIP and verify its SHA-256 before running its read-only plan and confirmed local transaction. The starter package contains no Codex credentials, Decision Store, Memory Local database, Graph State, or Control Center data.

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
sh scripts/setup-local.sh --plan
sh scripts/setup-local.sh --apply --confirm INSTALL
```

Windows PowerShell:

```powershell
& .\scripts\setup-local.ps1 -Mode Plan
& .\scripts\setup-local.ps1 -Mode Apply -Confirm INSTALL
```

Planはread-onlyのRepository verificationまでを行い、Plugin、marketplace、cache、Agents、local configを変更しません。ApplyはUnified Pluginと6 Agentsをlocal transactionとして更新し、出力された`transaction_id`から明示rollbackできます。Decision Store、Memory Local、Control Center、認証情報はこの手順では変更しません。

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
