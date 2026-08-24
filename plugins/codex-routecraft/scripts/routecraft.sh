#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONUTF8=1
exec python3 "$SCRIPT_DIR/routecraft.py" "$@"
