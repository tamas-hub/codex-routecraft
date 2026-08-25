#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mode=plan
confirm=''

usage() {
  cat <<'EOF'
Usage: sh scripts/setup-local.sh [--plan | --apply --confirm INSTALL]

--plan is the default and does not change plugin, marketplace, cache, agents, or config.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --plan) mode=plan; shift ;;
    --apply) mode=apply; shift ;;
    --confirm) [ "$#" -ge 2 ] || { echo '--confirm requires a value' >&2; exit 2; }; confirm=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v git >/dev/null 2>&1 || { echo 'git command not found' >&2; exit 1; }
command -v codex >/dev/null 2>&1 || { echo 'codex command not found' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo 'python3 command not found' >&2; exit 1; }

expected_commit=$(git -C "$repo_root" rev-parse HEAD) || { echo 'RouteCraft checkout HEAD could not be resolved.' >&2; exit 1; }
case "$expected_commit" in
  *[!0-9a-f]*|'') echo 'RouteCraft checkout HEAD is not a full lowercase commit id.' >&2; exit 1 ;;
esac
commit_length=$(printf '%s' "$expected_commit" | wc -c | tr -d ' ')
[ "$commit_length" -eq 40 ] || { echo 'RouteCraft checkout HEAD is not a full commit id.' >&2; exit 1; }

if [ "$mode" = 'apply' ]; then
  [ "$confirm" = 'INSTALL' ] || { echo 'Apply requires the exact confirmation: --confirm INSTALL' >&2; exit 2; }
  python3 "$repo_root/plugins/codex-routecraft/scripts/routecraft_device.py" install apply \
    --source-dir "$repo_root" --expected-commit "$expected_commit" --confirm INSTALL --json
  echo 'RouteCraft local setup complete. Start a fresh Codex task on GPT-5.6 Sol / High.'
  echo 'Persistent learning remains unchanged; no Decision Store remote was created or modified.'
else
  python3 "$repo_root/plugins/codex-routecraft/scripts/routecraft_device.py" install plan \
    --source-dir "$repo_root" --expected-commit "$expected_commit" --json
  echo 'No installation state was changed. Review the JSON, then rerun with --apply --confirm INSTALL.'
fi
