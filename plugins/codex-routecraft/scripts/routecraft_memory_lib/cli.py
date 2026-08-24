"""Argument parsing and entry point for RouteCraft persistent decision memory."""
from __future__ import annotations

from .common import *  # noqa: F401,F403
from .commands import *  # noqa: F401,F403


def add_store_argument(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument(
        "--store",
        required=required,
        help="Memory-store path. Otherwise ROUTECRAFT_MEMORY_DIR, config, then bundled store is used.",
    )


def installed_plugin_version() -> str:
    """Report the actual installed plugin version without changing store formats."""
    manifest = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, json.JSONDecodeError, AttributeError):
        value = None
    return str(value or "unknown")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="routecraft-memory",
        description="Persistent decision memory for RouteCraft",
    )
    parser.add_argument("--version", action="version", version=f"routecraft-memory {installed_plugin_version()}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create or clone a dedicated memory store")
    add_store_argument(init_parser, required=True)
    init_parser.add_argument("--name")
    init_parser.add_argument("--git-init", action="store_true", help="Initialize a local Git repository")
    init_parser.add_argument("--remote", help="Set a Git remote URL on a new store")
    init_parser.add_argument("--remote-name", default=DEFAULT_REMOTE)
    init_parser.add_argument("--clone", help="Clone an existing shared memory repository")
    init_parser.add_argument("--adopt-existing", action="store_true", help="Explicitly initialize a non-empty directory")
    init_parser.add_argument("--branch", default=DEFAULT_BRANCH)
    init_parser.add_argument("--configure", action="store_true", help="Write this store to the user config")
    init_parser.add_argument("--auto-sync", choices=("off", "pull", "both"), default="off")
    init_parser.add_argument("--markdown-index", action="store_true")
    init_parser.set_defaults(func=cmd_init)

    configure_parser = subparsers.add_parser("configure", help="Configure the active store and device")
    add_store_argument(configure_parser)
    configure_parser.add_argument("--device-id")
    configure_parser.add_argument("--auto-sync", choices=("off", "pull", "both"))
    configure_parser.add_argument("--remote", "--remote-name", dest="remote", help="Configured Git remote name")
    configure_parser.add_argument("--branch")
    configure_parser.set_defaults(func=cmd_configure)

    status_parser = subparsers.add_parser("status", help="Show memory-store and sync status")
    add_store_argument(status_parser)
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    reindex_parser = subparsers.add_parser("reindex", help="Rebuild the local search index")
    add_store_argument(reindex_parser)
    reindex_parser.add_argument("--markdown", action="store_true", help="Also write a human-readable INDEX.md")
    reindex_parser.set_defaults(func=cmd_reindex)

    validate_parser = subparsers.add_parser("validate", help="Validate all memory records")
    add_store_argument(validate_parser)
    validate_parser.set_defaults(func=cmd_validate)

    recall_parser = subparsers.add_parser("recall", help="Retrieve relevant prior rules and cases")
    add_store_argument(recall_parser)
    recall_parser.add_argument("--query", default="")
    recall_parser.add_argument("--tag", action="append", default=[])
    recall_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    recall_parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    recall_parser.add_argument("--json", action="store_true")
    recall_parser.add_argument("--paths-only", action="store_true")
    recall_parser.add_argument("--sync-first", action="store_true")
    recall_parser.set_defaults(func=cmd_recall)

    learn_parser = subparsers.add_parser("learn", help="Capture a verified case or unverified candidate")
    add_store_argument(learn_parser)
    learn_parser.add_argument("--input", help="JSON packet path, or - for stdin")
    learn_parser.add_argument("--kind", choices=("case", "candidate"))
    learn_parser.add_argument("--title")
    learn_parser.add_argument("--body-file")
    learn_parser.add_argument("--tag", action="append")
    learn_parser.add_argument("--scope", action="append")
    learn_parser.add_argument("--evidence", action="append")
    learn_parser.add_argument("--repository")
    learn_parser.add_argument("--outcome")
    learn_parser.add_argument("--confidence", type=float)
    learn_parser.add_argument("--observations", type=int)
    learn_parser.add_argument("--reinforce-candidate", action="append")
    learn_parser.add_argument("--sync", action="store_true")
    learn_parser.add_argument("--dry-run", action="store_true")
    learn_parser.add_argument("--markdown-index", action="store_true")
    learn_parser.set_defaults(func=cmd_learn)

    promote_parser = subparsers.add_parser("promote", help="Promote a repeated candidate into a validated rule")
    add_store_argument(promote_parser)
    promote_parser.add_argument("--input", help="JSON rule packet path, or - for stdin")
    promote_parser.add_argument("--candidate-id")
    promote_parser.add_argument("--title")
    promote_parser.add_argument("--decision")
    promote_parser.add_argument("--when-to-apply")
    promote_parser.add_argument("--when-not-to-apply")
    promote_parser.add_argument("--rationale")
    promote_parser.add_argument("--verification")
    promote_parser.add_argument("--tag", action="append")
    promote_parser.add_argument("--scope", action="append")
    promote_parser.add_argument("--evidence", action="append")
    promote_parser.add_argument("--confidence", type=float)
    promote_parser.add_argument("--observations", type=int)
    promote_parser.add_argument("--min-observations", type=int, default=2)
    promote_parser.add_argument("--min-evidence", type=int, default=2)
    promote_parser.add_argument("--authoritative", action="store_true")
    promote_parser.add_argument("--human-approved", action="store_true")
    promote_parser.add_argument("--sync", action="store_true")
    promote_parser.add_argument("--dry-run", action="store_true")
    promote_parser.add_argument("--markdown-index", action="store_true")
    promote_parser.set_defaults(func=cmd_promote)

    sync_parser = subparsers.add_parser("sync", help="Commit, pull/rebase, and push a shared private store")
    add_store_argument(sync_parser)
    sync_parser.add_argument("--mode", choices=("pull", "push", "both"), default="both")
    sync_parser.add_argument("--remote", "--remote-name", dest="remote", help="Git remote name")
    sync_parser.add_argument("--branch")
    sync_parser.add_argument("--message")
    sync_parser.add_argument("--retries", type=int, default=2)
    sync_parser.set_defaults(func=cmd_sync)

    return parser


def configure_text_streams() -> None:
    """Use deterministic UTF-8 output on Windows consoles and captured pipes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    configure_text_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RouteCraftError as exc:
        print(f"routecraft-memory: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("routecraft-memory: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
