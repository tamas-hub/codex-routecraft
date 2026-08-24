"""Conservative AGENTS.md optimizer.

The default operation is read-only analysis.  Recommendations are evidence
based (size, duplicate normalized rules, and explicit obsolete markers); no
semantic rule is silently deleted.  ``preview`` is also read-only.  Only an
explicit ``APPLY`` can update the RouteCraft-managed block.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

MARKER_START = "<!-- routecraft-optimizer:start -->"
MARKER_END = "<!-- routecraft-optimizer:end -->"
RECOMMENDED_BLOCK = """<!-- routecraft-optimizer:start -->
## RouteCraft task routing

- Start solo; delegate only independent, bounded work with clear ownership.
- Keep external writes, deployments, and credential handling parent-owned.
- Verify complete diffs and preserve existing uncommitted user changes.
<!-- routecraft-optimizer:end -->
"""
DEFAULT_BLOAT_LINES = 250
DEFAULT_BLOAT_TOKENS = 2_000
DEFAULT_OBSOLETE_PATTERNS = (
    r"(?i)\broutecraft[- _]?obsolete\b",
    r"(?i)\[obsolete\]",
    r"(?i)\bobsolete\s*:\s*true\b",
    r"(?i)\bdeprecated\s*:\s*true\b",
)
_RULE_RE = re.compile(r"^\s*(?:[-*+] |\d+[.)] |(?:rule|policy)\s*:\s*)(?P<body>.+?)\s*$", re.I)
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")


def estimate_tokens(text: str) -> int:
    """Small deterministic estimate used for context-cost analysis."""
    cjk = sum(1 for char in text if "\u3040" <= char <= "\u9fff")
    return cjk + (len(text) - cjk + 3) // 4


def _normalize_rule(value: str) -> str:
    body = _HEADING_RE.sub("", value)
    body = re.sub(r"^\s*(?:[-*+] |\d+[.)] )", "", body)
    body = re.sub(r"\s+", " ", body).strip().casefold()
    body = re.sub(r"[`*_]", "", body)
    return body


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _is_global_path(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    home = Path.home().resolve()
    return resolved in {home / "AGENTS.md", home / ".codex" / "AGENTS.md"}


def _rule_candidates(text: str) -> list[dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        match = _RULE_RE.match(line)
        if not match:
            continue
        normalized = _normalize_rule(match.group("body"))
        if not normalized or normalized.startswith("<!-- routecraft-optimizer:"):
            continue
        entry = groups.setdefault(normalized, {"normalized": normalized, "line_numbers": [], "lines": []})
        entry["line_numbers"].append(line_number)
        entry["lines"].append(line.strip())
    duplicates = [item for item in groups.values() if len(item["line_numbers"]) > 1]
    duplicates.sort(key=lambda item: (item["line_numbers"][0], item["normalized"]))
    for item in duplicates:
        item["count"] = len(item["line_numbers"])
        item["id"] = hashlib.sha256(str(item["normalized"]).encode("utf-8")).hexdigest()[:12]
    return duplicates


def _obsolete_candidates(text: str, patterns: Sequence[str]) -> list[dict[str, object]]:
    compiled = [re.compile(pattern) for pattern in patterns]
    output: list[dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        matched = [pattern for pattern, compiled_pattern in zip(patterns, compiled) if compiled_pattern.search(line)]
        if matched:
            output.append({"line_number": line_number, "text": line.strip(), "markers": matched, "action": "review_only"})
    return output


def _recommendations(*, exists: bool, has_block: bool, line_count: int, token_count: int, bloat_threshold: int, bloat_token_threshold: int, duplicates: list[dict[str, object]], obsolete: list[dict[str, object]], global_scope: bool) -> list[dict[str, object]]:
    recommendations: list[dict[str, object]] = []
    if not exists:
        recommendations.append({"kind": "missing_file", "severity": "info", "action": "preview_managed_block", "safe_to_apply": True})
    elif not has_block:
        recommendations.append({"kind": "managed_block", "severity": "info", "action": "preview_managed_block", "safe_to_apply": True})
    if line_count > bloat_threshold or token_count > bloat_token_threshold:
        recommendations.append({"kind": "bloat", "severity": "warning", "action": "split_or_reduce_after_review", "safe_to_apply": False, "line_threshold": bloat_threshold, "token_threshold": bloat_token_threshold})
    if duplicates:
        recommendations.append({"kind": "duplicate_rules", "severity": "warning", "action": "review_duplicate_rules", "safe_to_apply": False, "candidate_count": len(duplicates)})
    if obsolete:
        recommendations.append({"kind": "explicit_obsolete_markers", "severity": "warning", "action": "review_marked_rules", "safe_to_apply": False, "candidate_count": len(obsolete)})
    if global_scope:
        recommendations.append({"kind": "scope_separation", "severity": "info", "action": "separate_project_rules_from_global_rules", "safe_to_apply": False})
    return recommendations


@dataclass(frozen=True)
class Analysis:
    """Stable analysis result; first three fields preserve the original API."""

    exists: bool
    has_routecraft_block: bool
    recommendation_count: int
    path: str = ""
    byte_count: int = 0
    size_bytes: int = 0
    char_count: int = 0
    line_count: int = 0
    estimated_tokens: int = 0
    token_estimate: int = 0
    bloat: bool = False
    duplicate_rules: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    obsolete_candidates: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    scope: str = "project"
    context_cost: Mapping[str, object] = field(default_factory=dict)
    recommendations: tuple[Mapping[str, object], ...] = field(default_factory=tuple)


def analyze(
    path: str | Path,
    *,
    bloat_threshold: int = DEFAULT_BLOAT_LINES,
    bloat_token_threshold: int = DEFAULT_BLOAT_TOKENS,
    obsolete_patterns: Sequence[str] | None = None,
    scope: str | None = None,
) -> Analysis:
    """Inspect an AGENTS file without writing it."""
    target = Path(path).expanduser()
    if bloat_threshold <= 0 or bloat_token_threshold <= 0:
        raise ValueError("bloat thresholds must be positive")
    text = _read(target)
    exists = target.is_file()
    has_block = MARKER_START in text and MARKER_END in text
    line_count = len(text.splitlines()) if text else 0
    token_count = estimate_tokens(text)
    patterns = tuple(obsolete_patterns or DEFAULT_OBSOLETE_PATTERNS)
    duplicates = _rule_candidates(text)
    obsolete = _obsolete_candidates(text, patterns)
    resolved_scope = scope if scope in {"project", "global"} else "global" if _is_global_path(target) else "project"
    recommendations = _recommendations(
        exists=exists,
        has_block=has_block,
        line_count=line_count,
        token_count=token_count,
        bloat_threshold=bloat_threshold,
        bloat_token_threshold=bloat_token_threshold,
        duplicates=duplicates,
        obsolete=obsolete,
        global_scope=resolved_scope == "global",
    )
    return Analysis(
        exists,
        has_block,
        len(recommendations),
        path=str(target),
        byte_count=len(text.encode("utf-8")),
        size_bytes=len(text.encode("utf-8")),
        char_count=len(text),
        line_count=line_count,
        estimated_tokens=token_count,
        token_estimate=token_count,
        bloat=line_count > bloat_threshold or token_count > bloat_token_threshold,
        duplicate_rules=tuple(duplicates),
        obsolete_candidates=tuple(obsolete),
        scope=resolved_scope,
        context_cost={"chars": len(text), "tokens": token_count, "line_count": line_count},
        recommendations=tuple(recommendations),
    )


def recommended_text(path: str | Path) -> str:
    """Return a proposed file with only the managed block changed."""
    target = Path(path).expanduser()
    current = _read(target)
    start = current.find(MARKER_START)
    end = current.find(MARKER_END, start + len(MARKER_START)) if start >= 0 else -1
    if start >= 0 and end >= 0:
        end += len(MARKER_END)
        replacement = RECOMMENDED_BLOCK.rstrip("\n")
        return current[:start] + replacement + current[end:]
    return (current.rstrip() + "\n\n" if current.strip() else "") + RECOMMENDED_BLOCK


def preview(
    path: str | Path,
    *,
    bloat_threshold: int = DEFAULT_BLOAT_LINES,
    bloat_token_threshold: int = DEFAULT_BLOAT_TOKENS,
    obsolete_patterns: Sequence[str] | None = None,
    scope: str | None = None,
) -> dict[str, object]:
    """Produce a diff and recommendations without creating a file."""
    target = Path(path).expanduser()
    current = _read(target)
    proposed = recommended_text(target)
    analysis = analyze(target, bloat_threshold=bloat_threshold, bloat_token_threshold=bloat_token_threshold, obsolete_patterns=obsolete_patterns, scope=scope)
    diff = "".join(difflib.unified_diff(current.splitlines(True), proposed.splitlines(True), fromfile="AGENTS.md", tofile="AGENTS.md (proposed)"))
    return {"analysis": analysis.__dict__, "changed": current != proposed, "diff": diff, "apply_required": True, "managed_block_only": True}


def _replace_managed_block(current: str, proposed: str) -> str:
    start = current.find(MARKER_START)
    end = current.find(MARKER_END, start + len(MARKER_START)) if start >= 0 else -1
    proposed_start = proposed.find(MARKER_START)
    proposed_end = proposed.find(MARKER_END, proposed_start + len(MARKER_START))
    if proposed_start < 0 or proposed_end < 0:
        raise ValueError("managed block proposal is invalid")
    block = proposed[proposed_start : proposed_end + len(MARKER_END)].rstrip("\n")
    if start >= 0 and end >= 0:
        return current[:start] + block + current[end + len(MARKER_END):]
    return proposed


def apply(path: str | Path, confirmation: str) -> dict[str, object]:
    """Apply only the RouteCraft-managed block after exact confirmation."""
    if confirmation != "APPLY":
        raise ValueError("explicit confirmation APPLY is required")
    target = Path(path).expanduser()
    current = _read(target)
    proposed = _replace_managed_block(current, recommended_text(target))
    if target.is_file() and current == proposed:
        return {"changed": False, "managed_block_only": True}
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".routecraft-tmp")
    temporary.write_text(proposed, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return {"changed": True, "managed_block_only": True, "path": str(target)}
