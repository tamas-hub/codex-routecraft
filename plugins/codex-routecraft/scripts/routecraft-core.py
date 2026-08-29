#!/usr/bin/env python3
"""Offline JSON command line for RouteCraft Core's declared seams."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from routecraft_core import HostCapabilityRegistry, RoutingRequest, plan_route


def _json_object(raw: str, name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError(f"{name} must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RouteCraft Core v1 offline planner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    capabilities = subparsers.add_parser("capabilities", help="render a declared capability registry")
    capabilities.add_argument("--registry-json", default='{"schema_version":"1","providers":[]}')
    plan = subparsers.add_parser("plan", help="create a routing plan without dispatching")
    plan.add_argument("--task", required=True)
    plan.add_argument("--task-id")
    plan.add_argument("--project")
    plan.add_argument("--mode", default="legacy", choices=("native", "advisory", "routecraft", "legacy"))
    plan.add_argument("--provider")
    plan.add_argument("--host")
    plan.add_argument("--model")
    plan.add_argument("--config-json", default="{}")
    plan.add_argument("--test-budget", choices=("auto_min", "none", "min", "strict", "release"))
    plan.add_argument("--task-class", choices=("general", "debugging", "implementation", "ci", "refactor", "docs", "release", "integration", "test"))
    plan.add_argument("--change-scope", choices=("none", "single_file", "module", "cross_module", "repository", "release"))
    plan.add_argument("--risk-level", choices=("low", "medium", "high", "critical"))
    plan.add_argument("--registry-json", default='{"schema_version":"1","providers":[]}')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        registry = HostCapabilityRegistry.from_mapping(_json_object(args.registry_json, "registry"))
        if args.command == "capabilities":
            result: dict[str, Any] = {"ok": True, "data": registry.to_dict()}
        else:
            config = _json_object(args.config_json, "config")
            for key in ("test_budget", "task_class", "change_scope", "risk_level"):
                value = getattr(args, key)
                if value is not None:
                    config[key] = value
            request = RoutingRequest(task=args.task, task_id=args.task_id, project=args.project, mode=args.mode,
                                     provider=args.provider, host=args.host, model=args.model, config=config)
            result = {"ok": True, "data": plan_route(request, registry).to_dict()}
    except (ValueError, TypeError, argparse.ArgumentTypeError):
        # CLI input can contain private task text; never echo it in failures.
        result = {"ok": False, "error": {"code": "InvalidInput", "message": "invalid RouteCraft Core input"}}
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
