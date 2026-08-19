"""Decision index and bounded retrieval for RouteCraft memory."""
from __future__ import annotations

from .common import *  # noqa: F401,F403

def record_fingerprint(store: Path) -> str:
    digest = hashlib.sha256()
    for _, path in iter_record_paths(store):
        stat = path.stat()
        relative = path.relative_to(store).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def tokenize(value: str) -> list[str]:
    normalized = normalize_text(value)
    terms: list[str] = []
    for match in ASCII_TERM_RE.finditer(normalized):
        token = match.group(0)
        if token not in terms:
            terms.append(token)
    for sequence in CJK_RE.findall(normalized):
        if 2 <= len(sequence) <= 16 and sequence not in terms:
            terms.append(sequence)
        max_ngram = min(4, len(sequence))
        for width in range(2, max_ngram + 1):
            for index in range(0, len(sequence) - width + 1):
                token = sequence[index : index + width]
                if token not in terms:
                    terms.append(token)
                if len(terms) >= 80:
                    return terms
    return terms[:80]


def parse_sections(body: str) -> dict[str, str]:
    matches = list(HEADING_RE.finditer(body))
    if not matches:
        return {}
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group(1).strip().lower()] = body[start:end].strip()
    return sections


def first_paragraph(body: str, max_chars: int = 500) -> str:
    for block in re.split(r"\n\s*\n", body):
        clean = re.sub(r"^#+\s*", "", block.strip())
        if clean and not clean.startswith("```"):
            return clean[:max_chars]
    return ""


def decision_excerpt(record: Record, max_chars: int = 3500) -> str:
    sections = parse_sections(record.body)
    preferred: dict[str, tuple[str, ...]] = {
        "rule": ("decision", "when to apply", "when not to apply", "verification", "rationale"),
        "case": ("root cause", "reusable lesson", "verification", "failed approaches", "fix", "problem"),
        "candidate": ("observation", "possible decision value", "counterexamples / uncertainty", "promotion condition", "context"),
    }
    pieces: list[str] = []
    used = 0
    for heading in preferred.get(record.kind, ()):  # pragma: no branch - known kinds
        content = sections.get(heading)
        if not content:
            continue
        piece = f"## {heading.title()}\n\n{content.strip()}"
        if used + len(piece) > max_chars:
            remaining = max_chars - used
            if remaining > 120:
                pieces.append(piece[:remaining].rstrip() + "…")
            break
        pieces.append(piece)
        used += len(piece) + 2
    if not pieces:
        return record.body.strip()[:max_chars]
    return "\n\n".join(pieces)


def make_index_entry(store: Path, record: Record) -> dict[str, Any]:
    meta = record.metadata
    tags = [str(item) for item in meta.get("tags", [])]
    scope = [str(item) for item in meta.get("scope", [])]
    evidence = [str(item) for item in meta.get("evidence", [])]
    search_text = "\n".join(
        [record.record_id, record.kind, record.title, " ".join(tags), " ".join(scope), " ".join(evidence), record.body]
    )
    return {
        "id": record.record_id,
        "kind": record.kind,
        "title": record.title,
        "status": str(meta.get("status", "")),
        "confidence": float(meta.get("confidence", 0)),
        "observations": int(meta.get("observations", 1)),
        "tags": tags,
        "scope": scope,
        "evidence": evidence,
        "created_at": str(meta.get("created_at", meta.get("first_observed", ""))),
        "updated_at": str(meta.get("updated_at", meta.get("last_verified", meta.get("last_observed", "")))),
        "path": record.path.relative_to(store).as_posix(),
        "summary": first_paragraph(record.body),
        "search_text": normalize_text(search_text),
        "excerpt": decision_excerpt(record),
    }


def build_index(store: Path, *, write: bool = True, markdown: bool = False) -> dict[str, Any]:
    ensure_store_layout(store)
    records = load_records(store)
    payload = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "fingerprint": record_fingerprint(store),
        "record_count": len(records),
        "records": [make_index_entry(store, record) for record in records],
    }
    if write:
        atomic_write_json(store / ".routecraft" / "index.json", payload)
        if markdown:
            atomic_write_text(store / "INDEX.md", render_markdown_index(payload))
    return payload


def render_markdown_index(index: Mapping[str, Any]) -> str:
    grouped: dict[str, list[Mapping[str, Any]]] = {kind: [] for kind in KIND_TO_DIR}
    for entry in index.get("records", []):
        kind = str(entry.get("kind", ""))
        if kind in grouped:
            grouped[kind].append(entry)
    lines = [
        "# RouteCraft Decision Index",
        "",
        "> Generated by `routecraft_memory.py reindex --markdown`. Do not hand-edit.",
        "",
    ]
    for kind in ("rule", "case", "candidate"):
        lines.append(f"## {kind.title()}s")
        lines.append("")
        entries = sorted(grouped[kind], key=lambda item: (str(item.get("updated_at", "")), str(item.get("id", ""))), reverse=True)
        if not entries:
            lines.append("_No entries._")
        else:
            for entry in entries:
                tags = ", ".join(entry.get("tags", [])) or "no tags"
                lines.append(
                    f"- [{entry.get('id')}]({entry.get('path')}) — {entry.get('title')} "
                    f"(`{entry.get('status')}`, {tags})"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load_or_build_index(store: Path) -> dict[str, Any]:
    index_path = store / ".routecraft" / "index.json"
    current_fingerprint = record_fingerprint(store)
    if index_path.is_file():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            if (
                payload.get("schema_version") == INDEX_SCHEMA_VERSION
                and payload.get("fingerprint") == current_fingerprint
                and isinstance(payload.get("records"), list)
            ):
                return payload
    try:
        return build_index(store, write=True)
    except OSError:
        return build_index(store, write=False)


def score_entry(entry: Mapping[str, Any], query: str, requested_tags: Sequence[str]) -> tuple[float, list[str]]:
    query_norm = normalize_text(query)
    terms = tokenize(query)
    title = normalize_text(str(entry.get("title", "")))
    record_id = normalize_text(str(entry.get("id", "")))
    tags = [normalize_text(str(item)) for item in entry.get("tags", [])]
    scope = [normalize_text(str(item)) for item in entry.get("scope", [])]
    search_text = str(entry.get("search_text", ""))
    score = 0.0
    matched: list[str] = []
    if query_norm:
        if query_norm == title:
            score += 80
        elif query_norm in title:
            score += 45
        elif query_norm in search_text:
            score += 12
    for term in terms:
        contribution = 0.0
        if term in record_id:
            contribution += 16
        if term in title:
            contribution += 14
        if any(term in tag for tag in tags):
            contribution += 11
        if any(term in item for item in scope):
            contribution += 8
        if term in search_text:
            contribution += min(6.0, 1.5 + search_text.count(term) * 0.5)
        if contribution:
            score += contribution
            matched.append(term)
    for requested in requested_tags:
        requested_norm = normalize_text(requested)
        if any(requested_norm == tag or requested_norm in tag for tag in tags):
            score += 25
            matched.append(f"tag:{requested}")
        else:
            score -= 8
    kind = str(entry.get("kind", ""))
    status = str(entry.get("status", ""))
    if kind == "rule" and status == "validated":
        score += 8
    elif kind == "case":
        score += 4
    confidence = float(entry.get("confidence", 0) or 0)
    score += confidence * 3
    observations = int(entry.get("observations", 1) or 1)
    score += min(3, max(0, observations - 1))
    return score, list(dict.fromkeys(matched))[:12]


def recall_records(
    store: Path,
    query: str,
    requested_tags: Sequence[str],
    *,
    limit: int = DEFAULT_LIMIT,
    budget: int = DEFAULT_BUDGET,
) -> dict[str, Any]:
    index = load_or_build_index(store)
    scored: list[tuple[float, list[str], Mapping[str, Any]]] = []
    for entry in index.get("records", []):
        score, matched = score_entry(entry, query, requested_tags)
        if score > 0:
            scored.append((score, matched, entry))
    scored.sort(
        key=lambda item: (
            item[0],
            2 if item[2].get("kind") == "rule" else 1 if item[2].get("kind") == "case" else 0,
            str(item[2].get("updated_at", "")),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    remaining = max(0, budget)
    for score, matched, entry in scored[: max(0, limit)]:
        excerpt = str(entry.get("excerpt", ""))
        reserve = 300
        allowed = max(0, min(len(excerpt), remaining - reserve)) if remaining > reserve else 0
        if allowed <= 0 and selected:
            break
        if allowed and allowed < len(excerpt):
            excerpt = excerpt[:allowed].rstrip() + "…"
        elif not allowed:
            excerpt = ""
        output_entry = {key: value for key, value in entry.items() if key not in {"search_text", "excerpt"}}
        output_entry.update({"score": round(score, 2), "matched": matched, "excerpt": excerpt})
        selected.append(output_entry)
        remaining -= len(excerpt) + reserve
    return {
        "schema_version": 1,
        "store": str(store),
        "query": query,
        "requested_tags": list(requested_tags),
        "matches": selected,
        "match_count": len(selected),
        "total_candidates": len(scored),
        "budget": budget,
        "budget_remaining": max(0, remaining),
    }


def format_recall_markdown(result: Mapping[str, Any], *, paths_only: bool = False) -> str:
    lines = [
        "ROUTECRAFT RECALL",
        f"store: {result.get('store')}",
        f"query: {result.get('query')}",
        f"matches: {result.get('match_count')} of {result.get('total_candidates')}",
    ]
    tags = result.get("requested_tags", [])
    if tags:
        lines.append("tags: " + ", ".join(tags))
    matches = result.get("matches", [])
    if not matches:
        lines.append("\nNo relevant persistent decisions found.")
        return "\n".join(lines) + "\n"
    for index, entry in enumerate(matches, start=1):
        lines.extend(
            [
                "",
                f"## {index}. {entry.get('id')} — {entry.get('title')}",
                f"kind: {entry.get('kind')} | status: {entry.get('status')} | score: {entry.get('score')}",
                f"path: {entry.get('path')}",
                "matched: " + (", ".join(entry.get("matched", [])) or "metadata/relevance"),
            ]
        )
        if not paths_only and entry.get("excerpt"):
            lines.extend(["", str(entry.get("excerpt"))])
    return "\n".join(lines).rstrip() + "\n"
