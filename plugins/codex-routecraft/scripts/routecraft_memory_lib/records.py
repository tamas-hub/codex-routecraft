"""Record parsing, validation, and loading for RouteCraft memory."""
from __future__ import annotations

from .base import *  # noqa: F401,F403

def check_sensitive_text(text: str) -> list[str]:
    hits: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            hits.append(label)
    return hits


def parse_frontmatter(text: str, path: Path | None = None) -> tuple[dict[str, Any], str]:
    label = str(path) if path else "record"
    if not text.startswith("---\n"):
        raise RouteCraftError(f"Missing front matter in {label}")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RouteCraftError(f"Unterminated front matter in {label}")
    raw_meta = text[4:end]
    body = text[end + 5 :].lstrip("\n")
    metadata: dict[str, Any] = {}
    for line_number, line in enumerate(raw_meta.splitlines(), start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in line:
            raise RouteCraftError(f"Invalid front matter line {line_number} in {label}: {line}")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            raise RouteCraftError(f"Empty front matter key on line {line_number} in {label}")
        if not raw_value:
            value: Any = ""
        else:
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError:
                lowered = raw_value.lower()
                if lowered == "true":
                    value = True
                elif lowered == "false":
                    value = False
                elif lowered == "null":
                    value = None
                else:
                    with contextlib.suppress(ValueError):
                        value = int(raw_value)
                        metadata[key] = value
                        continue
                    with contextlib.suppress(ValueError):
                        value = float(raw_value)
                        metadata[key] = value
                        continue
                    value = raw_value.strip("\"'")
        metadata[key] = value
    return metadata, body


def render_frontmatter(metadata: Mapping[str, Any], body: str) -> str:
    preferred_order = [
        "schema_version",
        "id",
        "kind",
        "title",
        "status",
        "confidence",
        "observations",
        "tags",
        "scope",
        "created_at",
        "updated_at",
        "first_observed",
        "last_observed",
        "last_verified",
        "device_id",
        "repository",
        "outcome",
        "evidence",
        "related_rules",
        "source_candidate",
        "promoted_to",
    ]
    keys = [key for key in preferred_order if key in metadata]
    keys.extend(sorted(key for key in metadata if key not in keys))
    lines = ["---"]
    for key in keys:
        value = metadata[key]
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    clean_body = body.strip() + "\n"
    return "\n".join(lines) + "\n\n" + clean_body


def validate_record(record: Record, expected_kind: str | None = None) -> list[str]:
    errors: list[str] = []
    meta = record.metadata
    record_id = str(meta.get("id", ""))
    kind = str(meta.get("kind", ""))
    if meta.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{record.path}: schema_version must be {SCHEMA_VERSION}")
    if not VALID_ID_RE.fullmatch(record_id):
        errors.append(f"{record.path}: invalid id {record_id!r}")
    else:
        prefix = record_id.split("-", 1)[0]
        inferred = PREFIX_TO_KIND.get(prefix)
        if inferred != kind:
            errors.append(f"{record.path}: id prefix {prefix} does not match kind {kind}")
    if kind not in KIND_TO_DIR:
        errors.append(f"{record.path}: unsupported kind {kind!r}")
    if expected_kind and kind != expected_kind:
        errors.append(f"{record.path}: record kind {kind!r} does not match directory {expected_kind!r}")
    title = str(meta.get("title", "")).strip()
    if not title:
        errors.append(f"{record.path}: title is required")
    elif len(title) > 300:
        errors.append(f"{record.path}: title must be at most 300 characters")
    status = str(meta.get("status", "")).strip()
    if not status:
        errors.append(f"{record.path}: status is required")
    try:
        confidence = float(meta.get("confidence", 0))
        if confidence < 0 or confidence > 1:
            errors.append(f"{record.path}: confidence must be between 0 and 1")
    except (TypeError, ValueError):
        errors.append(f"{record.path}: confidence must be numeric")
    try:
        observations = int(meta.get("observations", 0))
        if observations < 1:
            errors.append(f"{record.path}: observations must be at least 1")
    except (TypeError, ValueError):
        errors.append(f"{record.path}: observations must be an integer")
    for field in ("tags", "scope", "evidence"):
        value = meta.get(field, [])
        if value is not None and not isinstance(value, list):
            errors.append(f"{record.path}: {field} must be a JSON-style list")
    if not record.body.strip():
        errors.append(f"{record.path}: body is empty")
    elif len(record.body) > MAX_RECORD_CHARS:
        errors.append(f"{record.path}: body exceeds {MAX_RECORD_CHARS} characters")
    sensitive = check_sensitive_text(render_frontmatter(meta, record.body))
    if sensitive:
        errors.append(f"{record.path}: possible sensitive data detected ({', '.join(sensitive)})")
    return errors


def iter_record_paths(store: Path) -> Iterator[tuple[str, Path]]:
    for kind, directory in KIND_TO_DIR.items():
        base = store / directory
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            if path.name.upper() == "README.MD":
                continue
            if path.is_symlink():
                raise RouteCraftError(f"Memory record must not be a symlink: {path}")
            yield kind, path


def load_record(path: Path) -> Record:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RouteCraftError(f"Could not read memory record {path}: {exc}") from exc
    metadata, body = parse_frontmatter(text, path)
    return Record(path=path, metadata=metadata, body=body)


def load_records(store: Path, *, validate: bool = True) -> list[Record]:
    validate_store_file_surface(store)
    records: list[Record] = []
    errors: list[str] = []
    seen_ids: dict[str, Path] = {}
    for expected_kind, path in iter_record_paths(store):
        try:
            record = load_record(path)
        except RouteCraftError as exc:
            errors.append(str(exc))
            continue
        if validate:
            errors.extend(validate_record(record, expected_kind))
        if record.record_id:
            previous = seen_ids.get(record.record_id)
            if previous:
                errors.append(f"Duplicate record id {record.record_id}: {previous} and {path}")
            else:
                seen_ids[record.record_id] = path
        records.append(record)
    if errors:
        raise RouteCraftError("Memory record validation failed:\n- " + "\n- ".join(errors))
    return records
