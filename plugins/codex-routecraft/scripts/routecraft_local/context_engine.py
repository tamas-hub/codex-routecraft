"""Higher-level Context Engine over the stable Context Pack builder.

The Engine keeps Memory Local and the private Decision Store physically
separate.  Adapters are read-only and optional; an unavailable adapter is
reported in the summary while the local pack remains usable.  The nested
``pack`` object retains the existing Context Pack keys used by Loop and hook
callers.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from .packs import _sanitize, build_context_pack, estimate_tokens

ENGINE_VERSION = 2
_SOURCE_NAMES = ("memory_local", "repository", "agents", "decision_store")
_DEFAULT_SOURCE_LIMIT = 100_000


@dataclass(frozen=True)
class _Item:
    source: str
    text: str
    title: str
    identity: str
    importance: float
    recency: float
    relevance: float
    memory_id: str = ""

    @property
    def score(self) -> float:
        return round(self.importance * 0.5 + self.recency * 0.25 + self.relevance * 0.25, 6)


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _as_items(value: Any, source: str) -> list[_Item]:
    """Convert adapter output to aggregate candidates without writing data."""
    if value is None:
        return []
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, Mapping):
        # A mapping can be one item or a source envelope containing items.
        nested = value.get("items", value.get("results", value.get("records")))
        values = nested if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)) else [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = value
    else:
        values = [value]
    items: list[_Item] = []
    for index, raw in enumerate(values):
        body = _get(raw, "body", _get(raw, "content", _get(raw, "text", raw if isinstance(raw, str) else "")))
        title = str(_get(raw, "title", _get(raw, "name", source.title())))
        body = str(body or "").strip()
        if not body:
            continue
        identity = str(_get(raw, "id", _get(raw, "memory_id", f"{source}-{index}")))
        importance_raw = _get(raw, "importance", _get(raw, "priority", "medium"))
        importance = {"critical": 1.0, "high": 0.85, "medium": 0.6, "normal": 0.5, "low": 0.25}.get(str(importance_raw).casefold(), 0.5)
        try:
            importance = min(1.0, max(0.0, float(importance_raw))) if isinstance(importance_raw, (int, float)) else importance
        except (TypeError, ValueError):
            pass
        relevance_raw = _get(raw, "relevance", _get(raw, "score", 0.5))
        try:
            relevance = min(1.0, max(0.0, float(relevance_raw)))
        except (TypeError, ValueError):
            relevance = 0.5
        updated = str(_get(raw, "updated_at", _get(raw, "created_at", _get(raw, "timestamp", ""))))
        recency = _recency(updated)
        items.append(_Item(source, body, title[:160], identity[:160], importance, recency, relevance, str(_get(raw, "memory_id", _get(raw, "id", "")))))
    return items


def _recency(value: str) -> float:
    if not value:
        return 0.5
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400)
        return round(1.0 / (1.0 + age_days / 30.0), 6)
    except (TypeError, ValueError, OverflowError):
        return 0.5


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _call_adapter(adapter: Any, *, service: Any, project_ref: str) -> Any:
    if not callable(adapter):
        return adapter
    for kwargs in (
        {"service": service, "project_ref": project_ref},
        {"project_ref": project_ref},
        {"service": service},
        {},
    ):
        try:
            return adapter(**kwargs)
        except TypeError:
            continue
    return adapter(service, project_ref)


def _adapter_value(explicit: Any, adapters: Mapping[str, Any] | None, name: str, *, service: Any, project_ref: str) -> tuple[Any, str | None]:
    value = explicit
    if value is None and isinstance(adapters, Mapping):
        value = adapters.get(name)
    if value is None:
        return None, None
    try:
        return _call_adapter(value, service=service, project_ref=project_ref), None
    except Exception as exc:  # adapter failure must not break local context
        return None, f"{name}:{type(exc).__name__}"


def _render_item(item: _Item, *, format: str) -> str:
    if format == "json":
        return json.dumps({"source": item.source, "title": item.title, "content": item.text}, ensure_ascii=False, separators=(",", ":"))
    return f"### {item.title}\n[{item.source}]\n{item.text}\n"


def _budget_ok(text: str, max_chars: int | None, max_tokens: int | None) -> bool:
    if max_chars is not None and len(text) > max_chars:
        return False
    return max_tokens is None or estimate_tokens(text) <= max_tokens


def compile_context(
    service: Any,
    project_ref: str,
    *,
    profile: str = "standard",
    max_chars: int | None = None,
    max_tokens: int | None = None,
    format: str = "markdown",
    repository_state: Any = None,
    repository_context: Any = None,
    agents_context: Any = None,
    agents: Any = None,
    decision_store_results: Any = None,
    decision_store: Any = None,
    decision_store_recall: Any = None,
    relevance_query: str | None = None,
    adapters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile local and optional context sources with isolated adapters.

    ``repository_state``, ``agents_context`` and ``decision_store_results``
    accept either records/text or a callable adapter. ``decision_store`` is a
    backwards-friendly alias. The values are read and ranked only; no source
    is merged or persisted by this function.
    """
    if max_tokens is not None and max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if max_chars is not None and max_chars <= 0:
        raise ValueError("max_chars must be positive")
    # Existing builder remains the canonical Memory Local implementation.
    pack = build_context_pack(service, project_ref, profile=profile, max_chars=max_chars, max_tokens=max_tokens, format=format)
    errors: list[str] = []
    source_values: dict[str, Any] = {
        "repository": repository_state if repository_state is not None else repository_context,
        "agents": agents_context if agents_context is not None else agents,
        "decision_store": decision_store_results if decision_store_results is not None else decision_store if decision_store is not None else decision_store_recall,
    }
    source_items: dict[str, list[_Item]] = {name: [] for name in _SOURCE_NAMES}
    local_count = len(pack.get("included_memory_ids", [])) + int(pack.get("omitted_count", 0) or 0)
    source_counts: dict[str, dict[str, int]] = {name: {"available": 0, "included": 0, "omitted": 0} for name in _SOURCE_NAMES}
    source_counts["memory_local"]["available"] = local_count
    source_counts["memory_local"]["included"] = len(pack.get("included_memory_ids", []))
    source_counts["memory_local"]["omitted"] = int(pack.get("omitted_count", 0) or 0)
    for name, explicit in source_values.items():
        value, error = _adapter_value(explicit, adapters, name, service=service, project_ref=project_ref)
        if error:
            errors.append(error)
            continue
        source_items[name] = _as_items(value, name)
        source_counts[name]["available"] = len(source_items[name])

    # Relevance query is intentionally aggregate only; it influences ranking
    # locally and is not emitted in the result.
    query_words = set(re.findall(r"[\w-]+", (relevance_query or "").casefold()))
    ranked: list[_Item] = []
    seen: set[str] = set()
    for name in ("repository", "agents", "decision_store"):
        for item in source_items[name]:
            normalized = _normalize(item.text)
            if not normalized or normalized in seen:
                source_counts[name]["omitted"] += 1
                continue
            seen.add(normalized)
            relevance = item.relevance
            if query_words:
                words = set(re.findall(r"[\w-]+", normalized))
                relevance = min(1.0, relevance + 0.25 * len(query_words & words) / max(1, len(query_words)))
                item = _Item(item.source, item.text, item.title, item.identity, item.importance, item.recency, relevance, item.memory_id)
            ranked.append(item)
    ranked.sort(key=lambda item: (-item.score, item.source, item.identity))

    base_content = str(pack.get("content", ""))
    cap_chars = max_chars if max_chars is not None else int(pack.get("max_chars", 10**9))
    cap_tokens = max_tokens
    content = base_content
    json_base: dict[str, Any] | None = None
    json_sources: list[dict[str, str]] = []
    if format == "json":
        try:
            parsed = json.loads(base_content)
            json_base = parsed if isinstance(parsed, dict) else {"context": parsed}
        except (TypeError, ValueError):
            json_base = {"context": base_content}
    included_external: list[_Item] = []
    for item in ranked:
        if json_base is not None:
            trial_sources = [*json_sources, {"source": item.source, "title": item.title, "content": item.text}]
            trial_object = {**json_base, "context_sources": trial_sources}
            candidate = json.dumps(trial_object, ensure_ascii=False, separators=(",", ":"))
        else:
            heading = f"\n\n## {item.source.replace('_', ' ').title()}\n" if not included_external or included_external[-1].source != item.source else ""
            candidate = content + heading + _render_item(item, format=format)
        if _budget_ok(candidate, cap_chars, cap_tokens):
            content = candidate
            if json_base is not None:
                json_sources.append({"source": item.source, "title": item.title, "content": item.text})
            included_external.append(item)
            source_counts[item.source]["included"] += 1
        else:
            source_counts[item.source]["omitted"] += 1
    if not _budget_ok(content, cap_chars, cap_tokens):
        # Preserve the local pack contract while enforcing the requested cap.
        if format == "json":
            raise ValueError("max_chars/max_tokens is too small for a valid JSON Context Pack")
        low, high = 0, min(len(content), cap_chars)
        while low < high:
            middle = (low + high + 1) // 2
            if _budget_ok(content[:middle], cap_chars, cap_tokens):
                low = middle
            else:
                high = middle - 1
        content = content[:low]
    pack = dict(pack)
    content = _sanitize(content)
    if not _budget_ok(content, cap_chars, cap_tokens):
        if format == "json":
            raise ValueError("max_chars/max_tokens is too small for a valid JSON Context Pack")
        low, high = 0, min(len(content), cap_chars)
        while low < high:
            middle = (low + high + 1) // 2
            if _budget_ok(content[:middle], cap_chars, cap_tokens):
                low = middle
            else:
                high = middle - 1
        content = content[:low]
    pack["content"] = content
    pack["char_count"] = len(pack["content"])
    pack["estimated_tokens"] = estimate_tokens(pack["content"])
    summary = {
        "included_count": len(pack.get("included_memory_ids", [])) + len(included_external),
        "omitted_count": int(pack.get("omitted_count", 0)) + sum(counts["omitted"] for name, counts in source_counts.items() if name != "memory_local"),
        "char_count": int(pack.get("char_count", 0)),
        "estimated_tokens": int(pack.get("estimated_tokens", 0)),
        "source_counts": source_counts,
        "adapter_errors": errors,
        "budget": {"max_chars": max_chars, "max_tokens": max_tokens},
        "ranked_sources": [item.source for item in included_external],
    }
    return {"engine_version": ENGINE_VERSION, "pack": pack, "summary": summary}


__all__ = ["ENGINE_VERSION", "compile_context"]
