#!/usr/bin/env sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v python3 >/dev/null 2>&1 || { echo "python3 command not found" >&2; exit 1; }
export PYTHONUTF8=1
exec python3 -X utf8 "$script_dir/praxis-memory.py" "$@"
