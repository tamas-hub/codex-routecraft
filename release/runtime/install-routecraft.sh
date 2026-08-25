#!/usr/bin/env sh
set -eu

OFFICIAL_REPOSITORY='@ROUTECRAFT_REPOSITORY@'
RELEASE_TAG='@ROUTECRAFT_TAG@'
EXPECTED_COMMIT='@ROUTECRAFT_COMMIT@'
REQUIRED_CODEX_CLI_VERSION='@ROUTECRAFT_CODEX_CLI_VERSION@'
RELEASE_REF="refs/routecraft-release/$RELEASE_TAG"
SOURCE_DIR="$HOME/codex-routecraft"
MODE=plan
CONFIRM=''

usage() {
  cat <<'EOF'
Usage: install-routecraft.sh [--plan | --apply --confirm INSTALL] [--source-dir PATH]

--plan is the default and does not clone, fetch, checkout, or install.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --plan) MODE=plan; shift ;;
    --apply) MODE=apply; shift ;;
    --confirm) [ "$#" -ge 2 ] || { echo '--confirm requires a value' >&2; exit 2; }; CONFIRM=$2; shift 2 ;;
    --source-dir) [ "$#" -ge 2 ] || { echo '--source-dir requires a path' >&2; exit 2; }; SOURCE_DIR=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v git >/dev/null 2>&1 || { echo 'Required command not found: git' >&2; exit 1; }
command -v codex >/dev/null 2>&1 || { echo 'Required command not found: codex' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo 'Required command not found: python3' >&2; exit 1; }
observed_codex_version=$(codex --version) || { echo 'Codex CLI version lookup failed.' >&2; exit 1; }
[ "$observed_codex_version" = "codex-cli $REQUIRED_CODEX_CLI_VERSION" ] \
  || { echo "Codex CLI $REQUIRED_CODEX_CLI_VERSION is required; found: $observed_codex_version" >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info.major == 3 and sys.version_info >= (3, 11) else 1)' \
  || { echo 'Python 3.11 or newer is required.' >&2; exit 1; }

normalize_repository() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -e 's:/*$::' -e 's:\.git$::'
}

assert_official_origin() {
  repository=$1
  origin=$(git -C "$repository" remote get-url origin) || { echo 'RouteCraft origin lookup failed.' >&2; exit 1; }
  if [ "$(normalize_repository "$origin")" != "$(normalize_repository "$OFFICIAL_REPOSITORY")" ]; then
    echo "Unexpected RouteCraft origin. Expected: $OFFICIAL_REPOSITORY" >&2
    exit 1
  fi
}

if [ "$MODE" = 'plan' ]; then
  cat <<EOF
RouteCraft Local Runtime 0.7.4 install plan
repository: $OFFICIAL_REPOSITORY
tag: $RELEASE_TAG
expected_commit: $EXPECTED_COMMIT
codex_cli_version: $REQUIRED_CODEX_CLI_VERSION
source_dir: $SOURCE_DIR
actions: verify prerequisites; fetch official tag; verify immutable commit; verify source; install one plugin and 6 agents
excluded: Control Center; Decision Store connection; credentials; Graph or Memory databases
EOF
  exit 0
fi

[ "$CONFIRM" = 'INSTALL' ] || { echo 'Apply requires the exact confirmation: --confirm INSTALL' >&2; exit 2; }

EXISTING_CHECKOUT=0
CHECKOUT_CHANGED=0
ORIGINAL_HEAD=''
ORIGINAL_BRANCH=''
if [ -d "$SOURCE_DIR/.git" ]; then
  root=$(git -C "$SOURCE_DIR" rev-parse --show-toplevel) || { echo 'RouteCraft Git root lookup failed.' >&2; exit 1; }
  canonical_root=$(CDPATH= cd -- "$root" && pwd -P)
  canonical_source=$(CDPATH= cd -- "$SOURCE_DIR" && pwd -P)
  [ "$canonical_root" = "$canonical_source" ] || { echo "SourceDir must be a dedicated Git root: $SOURCE_DIR" >&2; exit 1; }
  assert_official_origin "$SOURCE_DIR"
  [ -z "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=all)" ] \
    || { echo "RouteCraft source has local changes; no files were overwritten: $SOURCE_DIR" >&2; exit 1; }
  EXISTING_CHECKOUT=1
  ORIGINAL_HEAD=$(git -C "$SOURCE_DIR" rev-parse HEAD) \
    || { echo 'Existing RouteCraft HEAD lookup failed.' >&2; exit 1; }
  case "$ORIGINAL_HEAD" in
    *[!0-9a-f]*|'') echo 'Existing RouteCraft HEAD is not a full lowercase commit.' >&2; exit 1 ;;
  esac
  [ "${#ORIGINAL_HEAD}" -eq 40 ] \
    || { echo 'Existing RouteCraft HEAD is not a full 40-character commit.' >&2; exit 1; }
  if ORIGINAL_BRANCH=$(git -C "$SOURCE_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null); then
    :
  else
    branch_status=$?
    [ "$branch_status" -eq 1 ] || { echo 'Existing RouteCraft branch lookup failed.' >&2; exit 1; }
    ORIGINAL_BRANCH=''
  fi
else
  if [ -e "$SOURCE_DIR" ] && [ -n "$(ls -A "$SOURCE_DIR" 2>/dev/null || true)" ]; then
    echo "SourceDir exists but is not an empty Git checkout: $SOURCE_DIR" >&2
    exit 1
  fi
  git clone --no-checkout --origin origin "$OFFICIAL_REPOSITORY" "$SOURCE_DIR" \
    || { echo 'RouteCraft clone failed.' >&2; exit 1; }
  assert_official_origin "$SOURCE_DIR"
fi

restore_on_exit() {
  status=$?
  trap - 0
  if [ "$status" -ne 0 ] && [ "$EXISTING_CHECKOUT" -eq 1 ] && [ "$CHECKOUT_CHANGED" -eq 1 ]; then
    if [ -n "$ORIGINAL_BRANCH" ]; then
      if ! git -C "$SOURCE_DIR" checkout "$ORIGINAL_BRANCH"; then
        echo "RouteCraft install failed and original branch restore failed: $ORIGINAL_BRANCH" >&2
        exit 1
      fi
    else
      if ! git -C "$SOURCE_DIR" checkout --detach "$ORIGINAL_HEAD"; then
        echo "RouteCraft install failed and original detached HEAD restore failed: $ORIGINAL_HEAD" >&2
        exit 1
      fi
    fi
    if ! restored_head=$(git -C "$SOURCE_DIR" rev-parse HEAD); then
      echo 'RouteCraft install failed and restored HEAD could not be verified.' >&2
      exit 1
    fi
    if [ "$restored_head" != "$ORIGINAL_HEAD" ]; then
      echo "RouteCraft install failed and source restore mismatched. Expected $ORIGINAL_HEAD; found $restored_head" >&2
      exit 1
    fi
    echo "Install failed; restored the existing RouteCraft checkout to $ORIGINAL_HEAD." >&2
  fi
  exit "$status"
}
trap 'restore_on_exit' 0

git -C "$SOURCE_DIR" fetch --no-tags origin "refs/tags/$RELEASE_TAG:$RELEASE_REF" \
  || { echo 'Release tag fetch failed.' >&2; exit 1; }
resolved_commit=$(git -C "$SOURCE_DIR" rev-parse "$RELEASE_REF^{commit}") \
  || { echo 'Fetched release tag could not be resolved.' >&2; exit 1; }
[ "$resolved_commit" = "$EXPECTED_COMMIT" ] \
  || { echo "Release pin mismatch. Expected $EXPECTED_COMMIT; fetched tag resolved to $resolved_commit" >&2; exit 1; }

[ "$EXISTING_CHECKOUT" -eq 0 ] || CHECKOUT_CHANGED=1
git -C "$SOURCE_DIR" checkout --detach "$EXPECTED_COMMIT" \
  || { echo 'Pinned release checkout failed.' >&2; exit 1; }
head_commit=$(git -C "$SOURCE_DIR" rev-parse HEAD) || { echo 'HEAD lookup failed.' >&2; exit 1; }
[ "$head_commit" = "$EXPECTED_COMMIT" ] || { echo "Checked out HEAD does not match the expected commit: $head_commit" >&2; exit 1; }
assert_official_origin "$SOURCE_DIR"

VERIFY="$SOURCE_DIR/scripts/verify.py"
DEVICE="$SOURCE_DIR/plugins/codex-routecraft/scripts/routecraft_device.py"
[ -f "$VERIFY" ] && [ -f "$DEVICE" ] \
  || { echo 'Pinned source is missing the verified RouteCraft setup entrypoints.' >&2; exit 1; }

python3 "$VERIFY" || { echo 'RouteCraft repository verification failed.' >&2; exit 1; }
python3 "$DEVICE" install plan --source-dir "$SOURCE_DIR" --expected-commit "$EXPECTED_COMMIT" --json \
  || { echo 'RouteCraft transactional install plan failed.' >&2; exit 1; }
python3 "$DEVICE" install apply --source-dir "$SOURCE_DIR" --expected-commit "$EXPECTED_COMMIT" --confirm INSTALL --json \
  || { echo 'RouteCraft transactional install failed.' >&2; exit 1; }

trap - 0

echo
echo "RouteCraft 0.7.4 installed from $EXPECTED_COMMIT."
echo 'Close existing Codex tasks and start a fresh task before verification.'
echo 'Private Decision Store and Control Center were not configured.'
echo 'The local transaction id in the JSON output can be used with routecraft-device rollback.'
