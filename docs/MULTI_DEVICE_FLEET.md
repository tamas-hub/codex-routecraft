# RouteCraft Multi-Device Fleet

This layout separates RouteCraft source, reusable decision intelligence, and device-local state so the same setup can run on Windows, macOS, and future computers.

## Principles

- **GitHub is the source of truth for source code.** Local source checkouts are working copies.
- **Shareable configuration and decision intelligence live in a private GitHub repository.**
- **Absolute paths, device IDs, generated Codex cache, and credentials remain local.**
- **Source Guard applies to every Codex session** and verifies commit/push completion only when a task changes durable project artifacts.
- Bootstrap refuses to overwrite a RouteCraft source checkout that has local changes.
- Never store credentials, private keys, personal data, raw logs, or full transcripts in the Decision Store.

## Standard layout

| Kind | Logical path on every device | Purpose |
|---|---|---|
| RouteCraft source | `~/codex-routecraft` | Working copy cloned from the public GitHub repository |
| Decision Store | `~/routecraft-memory` | Private Git repository for Cases, Candidates, and Rules |
| Device profile | `~/.codex/routecraft/device.json` | Device-local absolute paths, OS metadata, installed version |
| Source Guard config | `~/.codex/routecraft/source-control.json` | GitHub owner, private default, and commit/push policy |
| Memory config | `~/.codex/routecraft/memory.json` | Active store, device ID, and synchronization policy |
| Agent profiles | `~/.codex/agents/routecraft_*.toml` | Generated from templates tracked in the public repository |
| Plugin cache | `~/.codex/plugins/cache/...` | Generated locally by Codex |
| Product repositories | `~/Projects/<repository>` recommended | Each GitHub repository remains its own source of truth |

On Windows, `~` normally expands to `C:\Users\<user>`. On macOS it expands to `/Users/<user>`.

## GitHub-backed state

### Public repository

`tamas-hub/codex-routecraft`

This stores the plugin, skills, agent templates, CLIs, bootstrap launchers, tests, and documentation. Devices normally fast-forward the `main` branch.

### Private repository

The private RouteCraft Decision Store tracks:

- `cases/`
- `candidates/`
- `rules/`
- `templates/`
- `.routecraft-store.json`

The shared fleet configuration is stored in the `fleet` object inside `.routecraft-store.json`. It contains repository locations, branches, portable `~/...` paths, and synchronization policy. It must not contain device-specific absolute paths or credentials.

## Device-local state

The following remain local and are never committed to the Decision Store:

- GitHub and Codex authentication state
- operating-system credential stores
- `device.json` and `memory.json`
- Codex plugin cache
- backups of installed agent profiles
- Source Guard baseline fingerprints (Git-state hashes only, never transcript content)
- cloned product repositories and build artifacts

## What bootstrap does

`scripts/bootstrap-device.ps1` and `scripts/bootstrap-device.sh` are idempotent. They:

1. clone or fast-forward RouteCraft at `~/codex-routecraft`;
2. run repository verification;
3. clone or attach the private Decision Store at `~/routecraft-memory`;
4. configure `auto_sync=both`, pull/rebase, and push;
5. create or strictly verify the shared fleet configuration;
6. safely back up and reinstall the RouteCraft plugin cache;
7. update the six RouteCraft agent profiles with timestamped backups;
8. write the local `device.json` profile;
9. verify source, memory, plugin, agent, and Git state.
10. write local Source Guard configuration when explicitly enabled.

Use `--allow-first-device` only when deliberately initializing an empty private Decision Store. Additional devices must not use it.

## Windows

```powershell
Set-ExecutionPolicy -Scope Process Bypass

& "$HOME\codex-routecraft\scripts\bootstrap-device.ps1" `
  -MemoryRemote "https://github.com/OWNER/routecraft-memory-private.git" `
  -EnableProjectSourceGuard `
  -GitHubOwner "OWNER"
```

## macOS

```sh
sh "$HOME/codex-routecraft/scripts/bootstrap-device.sh" \
  --memory-remote "https://github.com/OWNER/routecraft-memory-private.git" \
  --enable-project-source-guard \
  --github-owner "OWNER"
```

## Source Guard

Source Guard does not blindly stage or push files from a hook. At session start it injects a standing source-of-truth policy and stores a device-local fingerprint of the Git state. At Stop it asks Codex to continue only when this task left durable source uncommitted or unpushed.

- pre-existing dirty work is preserved as the baseline;
- only task-owned safe files are staged after verification;
- repositories without a remote default to a private repository under the configured GitHub owner;
- force push is prohibited and divergence stops for review;
- raw transcripts, `.env`, credentials, databases, uploads, caches, and device-local settings are excluded.

Sessions that only answer questions or do not change durable files create no commit. Non-managed hooks require a one-time trust review on each device through `/hooks`, and changed hook definitions require review again.

## Codex-assisted installation

Device-specific ZIP packages can contain only a launcher and `START_WITH_CODEX.md`. Open the extracted folder as a local Codex task and ask Codex to follow that file. Codex can check prerequisites, guide the one-time GitHub authentication step when necessary, run bootstrap, and verify final status.

## Product repository policy

Clone new or re-created product repositories under:

```text
~/Projects/<repository-name>
```

Do not synchronize source folders directly through OneDrive or iCloud Drive. Each computer clones and pulls from GitHub; branches, commits, and pull requests are the cross-device synchronization mechanism.

Bootstrap intentionally does not move existing repositories. Moving them automatically could break IDE settings, signing configuration, build caches, or absolute-path references. For cleanup, confirm a repository is clean and fully pushed, then re-clone it into the standard location.

## Recovery

- Broken RouteCraft source: move the checkout aside and clone the public repository again.
- Broken Decision Store: inspect unsynchronized changes, then clone the private repository again.
- Broken plugin cache: rerun bootstrap; the prior cache is retained with a timestamped backup name.
- Agent conflict: bootstrap backs up the existing profile before replacement.
- Git conflict: bootstrap stops instead of overwriting; resolve the Decision Store conflict and rerun.

## Adding future devices

The same bootstrap works for a fourth device and beyond. The only prerequisites are Git, Python 3, Codex, and access to the private Decision Store. No new hand-built configuration bundle is required.
