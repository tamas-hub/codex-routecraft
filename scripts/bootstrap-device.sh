#!/usr/bin/env sh
set -eu

SOURCE_REMOTE="https://github.com/tamas-hub/codex-routecraft.git"
SOURCE_BRANCH="main"
MEMORY_BRANCH="main"
SOURCE_DIR="$HOME/codex-routecraft"
MEMORY_DIR="$HOME/routecraft-memory"
ALLOW_FIRST_DEVICE=0
JSON_OUTPUT=0
MEMORY_REMOTE=""

usage() {
  cat <<'EOF'
Usage: bootstrap-device.sh --memory-remote URL [options]

Options:
  --source-remote URL
  --source-branch NAME
  --memory-branch NAME
  --source-dir PATH
  --memory-dir PATH
  --allow-first-device
  --json
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --memory-remote) MEMORY_REMOTE=$2; shift 2 ;;
    --source-remote) SOURCE_REMOTE=$2; shift 2 ;;
    --source-branch) SOURCE_BRANCH=$2; shift 2 ;;
    --memory-branch) MEMORY_BRANCH=$2; shift 2 ;;
    --source-dir) SOURCE_DIR=$2; shift 2 ;;
    --memory-dir) MEMORY_DIR=$2; shift 2 ;;
    --allow-first-device) ALLOW_FIRST_DEVICE=1; shift ;;
    --json) JSON_OUTPUT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$MEMORY_REMOTE" ] || { echo "--memory-remote is required" >&2; exit 2; }
command -v git >/dev/null 2>&1 || { echo "Required command not found: git" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "Required command not found: python3" >&2; exit 1; }
command -v codex >/dev/null 2>&1 || { echo "Required command not found: codex" >&2; exit 1; }

echo "=== RouteCraft device bootstrap ==="
echo "Source: $SOURCE_DIR"
echo "Memory: $MEMORY_DIR"

if [ ! -d "$SOURCE_DIR/.git" ]; then
  if [ -d "$SOURCE_DIR" ] && [ -n "$(ls -A "$SOURCE_DIR" 2>/dev/null || true)" ]; then
    echo "SourceDir exists but is not an empty Git checkout: $SOURCE_DIR" >&2
    exit 1
  fi
  [ ! -d "$SOURCE_DIR" ] || rmdir "$SOURCE_DIR"
  git clone --branch "$SOURCE_BRANCH" "$SOURCE_REMOTE" "$SOURCE_DIR"
else
  git -C "$SOURCE_DIR" fetch origin "$SOURCE_BRANCH"
  git -C "$SOURCE_DIR" checkout "$SOURCE_BRANCH"
  git -C "$SOURCE_DIR" pull --ff-only origin "$SOURCE_BRANCH"
fi

DEVICE_SCRIPT="$SOURCE_DIR/plugins/codex-routecraft/scripts/routecraft_device.py"
[ -f "$DEVICE_SCRIPT" ] || { echo "Missing device bootstrap script: $DEVICE_SCRIPT" >&2; exit 1; }

set -- \
  "$DEVICE_SCRIPT" bootstrap \
  --source-dir "$SOURCE_DIR" \
  --memory-dir "$MEMORY_DIR" \
  --source-remote "$SOURCE_REMOTE" \
  --source-branch "$SOURCE_BRANCH" \
  --memory-remote "$MEMORY_REMOTE" \
  --memory-branch "$MEMORY_BRANCH"

if [ "$ALLOW_FIRST_DEVICE" = "1" ]; then
  set -- "$@" --allow-first-device
fi
if [ "$JSON_OUTPUT" = "1" ]; then
  set -- "$@" --json
fi

python3 "$@"

echo
echo "Device bootstrap completed. Close existing Codex sessions and start a fresh local task."
