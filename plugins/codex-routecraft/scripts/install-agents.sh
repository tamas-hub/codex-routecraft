#!/usr/bin/env sh
set -eu

mode="install"
force="0"
for arg in "$@"; do
  case "$arg" in
    --check) mode="check" ;;
    --force) force="1" ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_dir="$script_dir/../agents"
codex_home=${CODEX_HOME:-"$HOME/.codex"}
dest_dir="$codex_home/agents"

files="
routecraft_luna_low.toml
routecraft_luna_medium.toml
routecraft_luna_max.toml
routecraft_terra_medium.toml
routecraft_terra_high.toml
routecraft_sol_reviewer.toml
"

[ -d "$source_dir" ] || { echo "Missing source directory: $source_dir" >&2; exit 1; }
mkdir -p "$dest_dir"

status=0
for name in $files; do
  src="$source_dir/$name"
  dst="$dest_dir/$name"
  [ -f "$src" ] || { echo "Missing source role: $src" >&2; exit 1; }

  if [ "$mode" = "check" ]; then
    if [ ! -f "$dst" ]; then
      echo "MISSING $dst"
      status=1
    elif cmp -s "$src" "$dst"; then
      echo "OK      $name"
    else
      echo "DIFFERS $dst"
      status=1
    fi
    continue
  fi

  if [ ! -e "$dst" ]; then
    cp "$src" "$dst"
    echo "INSTALLED $name"
  elif [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    echo "UNCHANGED $name"
  elif [ "$force" = "1" ] && [ -f "$dst" ]; then
    stamp=$(date +%Y%m%d%H%M%S)
    backup="$dst.bak.$stamp"
    cp "$dst" "$backup"
    cp "$src" "$dst"
    echo "REPLACED $name (backup: $backup)"
  else
    echo "REFUSED conflicting destination: $dst" >&2
    echo "Review it first. Re-run with --force to back it up and replace it." >&2
    status=1
  fi
done

exit "$status"
