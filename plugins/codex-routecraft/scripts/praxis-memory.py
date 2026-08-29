#!/usr/bin/env python3
"""Standalone JSON CLI for Praxis Memory."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from praxis_memory import PraxisMemory, PraxisMemoryError, RECORD_TYPES  # noqa: E402


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="praxis-memory")
    parser.add_argument("--directory", help="Praxis data directory (default: ~/.praxis-memory)")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    status = commands.add_parser("status")
    add = commands.add_parser("add")
    add.add_argument("--type", required=True, choices=RECORD_TYPES)
    add.add_argument("--title", required=True)
    add.add_argument("--body", required=True)
    add.add_argument("--project")
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--confidence", type=float)
    add.add_argument("--status")
    add.add_argument("--event-classification", default="normal")
    add.add_argument("--verified", action="store_true")
    recall = commands.add_parser("recall")
    recall.add_argument("query")
    recall.add_argument("--limit", type=int, default=5)
    recall.add_argument("--tag", action="append", default=[])
    recall.add_argument("--include-special-events", action="store_true")
    events = commands.add_parser("events")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--cursor")
    events.add_argument("--source")
    events.add_argument("--normal-only", action="store_true")
    local = commands.add_parser("migrate-local")
    local.add_argument("source_database", nargs="?")
    local.add_argument("--input", dest="source_input")
    decision = commands.add_parser("migrate-decision-store")
    decision.add_argument("source_directory", nargs="?")
    decision.add_argument("--input", dest="source_input")
    decision.add_argument("--project", default="legacy")
    for migration in (local, decision):
        migration.add_argument("--apply", action="store_true")
        migration.add_argument("--confirm")
    for command in (init, status, add, recall, events, local, decision):
        command.add_argument("--data-dir", dest="command_directory")
    args = parser.parse_args(argv)
    memory = PraxisMemory(args.command_directory or args.directory)
    try:
        if args.command == "init": result = memory.initialize()
        elif args.command == "status": result = memory.status()
        elif args.command == "add": result = memory.add_record(args.type, args.title, args.body, project=args.project, tags=args.tag, confidence=args.confidence, status=args.status, event_classification=args.event_classification, verified=args.verified)
        elif args.command == "recall": result = memory.recall(args.query, limit=args.limit, tags=args.tag, include_special_events=args.include_special_events)
        elif args.command == "events": result = memory.list_events(limit=args.limit, cursor=args.cursor, source=args.source, include_special_events=not args.normal_only)
        elif args.command == "migrate-local":
            source = args.source_input or args.source_database
            if not source: raise PraxisMemoryError("migrate-local requires --input or source_database")
            result = memory.migrate_from_routecraft_local(source, apply=args.apply, confirmation=args.confirm)
        else:
            source = args.source_input or args.source_directory
            if not source: raise PraxisMemoryError("migrate-decision-store requires --input or source_directory")
            result = memory.migrate_from_decision_store(source, apply=args.apply, confirmation=args.confirm, project=args.project)
    except PraxisMemoryError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return exc.exit_code
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
