#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
command -v codex >/dev/null 2>&1 || { echo "codex command not found" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 command not found" >&2; exit 1; }

cd "$repo_root"
python3 scripts/verify.py

echo "Adding local RouteCraft marketplace: $repo_root"
if ! codex plugin marketplace add "$repo_root"; then
  echo "Marketplace add returned non-zero. If RouteCraft is already registered, inspect 'codex plugin marketplace' state before retrying." >&2
  exit 1
fi
codex plugin add codex-routecraft@routecraft
sh plugins/codex-routecraft/scripts/install-agents.sh

echo "RouteCraft local setup complete. Start a fresh Codex task on GPT-5.6 Sol / High."
echo "Persistent learning is disabled until you create a separate private store."
echo "Example: python3 plugins/codex-routecraft/scripts/routecraft_memory.py init --store \"$HOME/routecraft-memory\" --git-init --configure"
