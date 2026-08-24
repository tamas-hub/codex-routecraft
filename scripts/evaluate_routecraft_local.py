#!/usr/bin/env python3
"""Small deterministic retrieval/Context Pack evaluation for Memory Local."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCRIPTS = ROOT / "plugins" / "codex-routecraft" / "scripts"
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))

from routecraft_local.packs import build_context_pack  # noqa: E402
from routecraft_local.service import RouteCraftService  # noqa: E402


def evaluate(suite_path: Path, data_dir: Path, limit: int) -> dict[str, Any]:
    suite = json.loads(suite_path.read_text(encoding="utf-8-sig"))
    service = RouteCraftService(data_dir)
    service.initialize()
    project_spec = suite["project"]
    project = service.add_project(
        name=project_spec["name"],
        description=project_spec.get("description", ""),
        current_objective=project_spec.get("current_objective", ""),
    )
    for item in suite["memories"]:
        service.add_memory(
            project["id"],
            item["type"],
            item["title"],
            item["body"],
            importance=item.get("importance", "medium"),
            tags=item.get("tags", ()),
            source="evaluation-fixture",
            active=item.get("active", True),
            verified=item.get("verified", False),
        )

    query_results: list[dict[str, Any]] = []
    reciprocal_ranks: list[float] = []
    hits = 0
    for query in suite["queries"]:
        matches = service.search_memories(project["id"], query["query"], limit=limit)
        titles = [item["title"] for item in matches]
        try:
            rank = titles.index(query["expected_title"]) + 1
        except ValueError:
            rank = None
        if rank is not None:
            hits += 1
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)
        query_results.append({
            "expected_title": query["expected_title"],
            "rank": rank,
            "top_titles": titles,
        })

    inactive_title = suite["inactive_must_not_match"]
    inactive_excluded = all(
        item["title"] != inactive_title
        for item in service.search_memories(project["id"], "同期 SQLite Git", limit=limit)
    )
    context = build_context_pack(service, project["id"], profile="standard", format="markdown")
    content = str(context["content"])
    required_context_titles = [
        "SQLite migration前にbackupする",
        "日本語stdinの文字化け",
        "外部interfaceへbindしない",
        "macOS実機で起動確認",
    ]
    context_coverage = sum(title in content for title in required_context_titles) / len(required_context_titles)
    duplicated_titles = [title for title in required_context_titles if content.count(title) > 1]

    count = len(suite["queries"])
    result = {
        "schema_version": 1,
        "query_count": count,
        "limit": limit,
        "hit_at_k": hits / count if count else 0.0,
        "mrr": sum(reciprocal_ranks) / count if count else 0.0,
        "inactive_excluded": inactive_excluded,
        "context_coverage": context_coverage,
        "context_duplicate_titles": duplicated_titles,
        "queries": query_results,
    }
    result["passed"] = (
        result["hit_at_k"] == 1.0
        and result["mrr"] >= 0.75
        and inactive_excluded
        and context_coverage == 1.0
        and not duplicated_titles
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate RouteCraft Memory Local retrieval without external APIs")
    parser.add_argument("--suite", default=str(ROOT / "samples" / "evaluation-suite.json"))
    parser.add_argument("--data-dir", help="Keep the evaluation DB here; default is a temporary directory")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.data_dir:
        result = evaluate(Path(args.suite), Path(args.data_dir), args.limit)
    else:
        with tempfile.TemporaryDirectory(prefix="routecraft-local-eval-") as temp:
            result = evaluate(Path(args.suite), Path(temp), args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
