"""Case/candidate capture, reinforcement, and promotion helpers."""
from __future__ import annotations

from .common import *  # noqa: F401,F403

def render_sections(kind: str, title: str, sections: Mapping[str, Any]) -> str:
    heading = {"case": "Case", "candidate": "Candidate", "rule": "Rule"}[kind]
    lines = [f"# {heading}: {title}", ""]
    for section, content in sections.items():
        clean = str(content).strip()
        if not clean:
            continue
        lines.extend([f"## {section}", "", clean, ""])
    return "\n".join(lines).rstrip() + "\n"


def packet_body(packet: Mapping[str, Any], kind: str, title: str, body_file: str | None = None) -> str:
    if body_file:
        try:
            body = Path(body_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise RouteCraftError(f"Could not read body file {body_file}: {exc}") from exc
    else:
        packet_body_value = packet.get("body")
        if isinstance(packet_body_value, str) and packet_body_value.strip():
            body = packet_body_value.strip() + "\n"
        else:
            sections = packet.get("sections")
            if isinstance(sections, dict) and sections:
                body = render_sections(kind, title, sections)
            else:
                raise RouteCraftError("Learning packet must provide a non-empty body or sections object")
    if len(body) > MAX_RECORD_CHARS:
        raise RouteCraftError(
            f"Memory record body is too large ({len(body)} characters; maximum {MAX_RECORD_CHARS}). "
            "Store a compact decision summary instead of a transcript or raw log."
        )
    return body


def parse_int_value(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RouteCraftError(f"{field} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise RouteCraftError(f"{field} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise RouteCraftError(f"{field} must be at most {maximum}")
    return parsed


def parse_float_value(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RouteCraftError(f"{field} must be numeric") from exc
    if minimum is not None and parsed < minimum:
        raise RouteCraftError(f"{field} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise RouteCraftError(f"{field} must be at most {maximum}")
    return parsed


def normalize_string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise RouteCraftError(f"{field} must be a string or list of strings")
    result: list[str] = []
    for item in value:
        clean = str(item).strip()
        if clean and clean not in result:
            result.append(clean)
        if len(result) > MAX_LIST_ITEMS:
            raise RouteCraftError(f"{field} must contain at most {MAX_LIST_ITEMS} unique items")
    return result


def make_record_id(kind: str, device_id: str) -> str:
    prefix = KIND_TO_PREFIX[kind]
    device = SAFE_DEVICE_RE.sub("", device_id).upper()[:12] or "DEVICE"
    suffix = f"{random.SystemRandom().randrange(0, 65536):04X}"
    return f"{prefix}-{utc_id_timestamp()}-{device}-{suffix}"


def record_path_for_id(store: Path, kind: str, record_id: str) -> Path:
    return store / KIND_TO_DIR[kind] / f"{record_id.lower()}.md"


def write_record(store: Path, kind: str, metadata: Mapping[str, Any], body: str, *, dry_run: bool = False) -> Path:
    record_id = str(metadata.get("id", ""))
    path = record_path_for_id(store, kind, record_id)
    if path.exists():
        raise RouteCraftError(f"Memory record already exists: {path}")
    content = render_frontmatter(metadata, body)
    sensitive = check_sensitive_text(content)
    if sensitive:
        raise RouteCraftError(
            "Refusing to store possible sensitive data "
            f"({', '.join(sensitive)}). Redact the learning packet and retry."
        )
    record = Record(path=path, metadata=dict(metadata), body=body)
    errors = validate_record(record, kind)
    if errors:
        raise RouteCraftError("Invalid memory record:\n- " + "\n- ".join(errors))
    if not dry_run:
        atomic_write_text(path, content)
    return path


def update_record(record: Record, metadata: Mapping[str, Any], body: str | None = None, *, dry_run: bool = False) -> None:
    content_body = record.body if body is None else body
    content = render_frontmatter(metadata, content_body)
    sensitive = check_sensitive_text(content)
    if sensitive:
        raise RouteCraftError(
            "Refusing to update record with possible sensitive data "
            f"({', '.join(sensitive)}). Redact and retry."
        )
    updated = Record(path=record.path, metadata=dict(metadata), body=content_body)
    errors = validate_record(updated, record.kind)
    if errors:
        raise RouteCraftError("Invalid updated memory record:\n- " + "\n- ".join(errors))
    if not dry_run:
        atomic_write_text(record.path, content)


def rollback_memory_mutations(created_paths: Sequence[Path], original_contents: Mapping[Path, str]) -> None:
    for path in reversed(list(created_paths)):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    for path, content in original_contents.items():
        with contextlib.suppress(OSError):
            atomic_write_text(path, content)


def find_record(store: Path, record_id: str, expected_kind: str | None = None) -> Record:
    for record in load_records(store):
        if record.record_id == record_id:
            if expected_kind and record.kind != expected_kind:
                raise RouteCraftError(f"Record {record_id} is {record.kind}, expected {expected_kind}")
            return record
    raise RouteCraftError(f"Memory record not found: {record_id}")


def load_json_packet(path_value: str | None) -> dict[str, Any]:
    if not path_value:
        return {}
    try:
        if path_value == "-":
            data = json.load(sys.stdin)
        else:
            packet_path = Path(path_value).expanduser()
            if packet_path.stat().st_size > MAX_PACKET_BYTES:
                raise RouteCraftError(
                    f"JSON packet exceeds {MAX_PACKET_BYTES} bytes; provide a compact learning packet"
                )
            data = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteCraftError(f"Could not read JSON packet {path_value}: {exc}") from exc
    if not isinstance(data, dict):
        raise RouteCraftError("JSON packet must be an object")
    return data


def merge_cli_packet(args: argparse.Namespace, packet: dict[str, Any]) -> dict[str, Any]:
    merged = dict(packet)
    scalar_fields = (
        "kind",
        "title",
        "repository",
        "outcome",
        "confidence",
        "observations",
        "candidate_id",
        "decision",
        "when_to_apply",
        "when_not_to_apply",
        "rationale",
        "verification",
    )
    for field in scalar_fields:
        value = getattr(args, field, None)
        if value is not None:
            merged[field] = value
    for field in ("tag", "scope", "evidence", "reinforce_candidate"):
        value = getattr(args, field, None)
        if value:
            packet_field = {
                "tag": "tags",
                "scope": "scope",
                "evidence": "evidence",
                "reinforce_candidate": "reinforce_candidates",
            }[field]
            existing = normalize_string_list(merged.get(packet_field), packet_field)
            merged[packet_field] = list(dict.fromkeys(existing + list(value)))
    return merged


def create_learning_record(
    store: Path,
    packet: Mapping[str, Any],
    *,
    device_id: str,
    body_file: str | None = None,
    dry_run: bool = False,
) -> tuple[Record, Path]:
    kind = str(packet.get("kind", "")).strip().lower()
    if kind not in {"case", "candidate"}:
        raise RouteCraftError("learn accepts kind 'case' or 'candidate'")
    title = str(packet.get("title", "")).strip()
    if not title:
        raise RouteCraftError("Learning packet title is required")
    if len(title) > 300:
        raise RouteCraftError("Learning packet title must be at most 300 characters")
    body = packet_body(packet, kind, title, body_file)
    now = utc_now()
    observations = parse_int_value(packet.get("observations", 1), "observations", minimum=1, maximum=1_000_000)
    confidence_default = 0.65 if kind == "case" else 0.5
    confidence = parse_float_value(packet.get("confidence", confidence_default), "confidence", minimum=0, maximum=1)
    evidence = normalize_string_list(packet.get("evidence"), "evidence")
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": make_record_id(kind, device_id),
        "kind": kind,
        "title": title,
        "status": "closed" if kind == "case" else "candidate",
        "confidence": round(confidence, 3),
        "observations": observations,
        "tags": normalize_string_list(packet.get("tags"), "tags"),
        "scope": normalize_string_list(packet.get("scope"), "scope"),
        "created_at": now,
        "updated_at": now,
        "device_id": device_id,
        "evidence": evidence,
    }
    repository = str(packet.get("repository", "")).strip()
    if repository:
        metadata["repository"] = repository
    outcome = str(packet.get("outcome", "")).strip()
    if outcome:
        metadata["outcome"] = outcome
    related_rules = normalize_string_list(packet.get("related_rules"), "related_rules")
    if related_rules:
        metadata["related_rules"] = related_rules
    path = write_record(store, kind, metadata, body, dry_run=dry_run)
    return Record(path=path, metadata=metadata, body=body), path


def reinforce_candidate(
    candidate: Record,
    evidence: str,
    *,
    confidence: float | None = None,
    dry_run: bool = False,
) -> Record:
    if candidate.kind != "candidate":
        raise RouteCraftError(f"Cannot reinforce non-candidate record: {candidate.record_id}")
    if str(candidate.metadata.get("status")) == "promoted":
        return candidate
    metadata = dict(candidate.metadata)
    evidence_list = normalize_string_list(metadata.get("evidence"), "evidence")
    added = evidence not in evidence_list
    if added:
        evidence_list.append(evidence)
    metadata["evidence"] = evidence_list
    observations = parse_int_value(metadata.get("observations", 1), "candidate observations", minimum=1)
    if added:
        observations += 1
    metadata["observations"] = max(observations, len([item for item in evidence_list if item.startswith("CASE-")]))
    if confidence is not None:
        current_confidence = parse_float_value(metadata.get("confidence", 0), "candidate confidence", minimum=0, maximum=1)
        supplied_confidence = parse_float_value(confidence, "candidate confidence", minimum=0, maximum=1)
        metadata["confidence"] = round(max(current_confidence, supplied_confidence), 3)
    elif added:
        current_confidence = parse_float_value(metadata.get("confidence", 0.5), "candidate confidence", minimum=0, maximum=1)
        metadata["confidence"] = round(min(0.95, current_confidence + 0.1), 3)
    metadata["last_observed"] = utc_now()
    metadata["updated_at"] = utc_now()
    update_record(candidate, metadata, dry_run=dry_run)
    return Record(path=candidate.path, metadata=metadata, body=candidate.body)


def candidate_eligible(record: Record, valid_case_ids: set[str] | None = None) -> bool:
    if record.kind != "candidate" or str(record.metadata.get("status")) != "candidate":
        return False
    observations = parse_int_value(record.metadata.get("observations", 1), "candidate observations", minimum=1)
    evidence = set(normalize_string_list(record.metadata.get("evidence"), "evidence"))
    case_evidence = {item for item in evidence if item.startswith("CASE-")}
    if valid_case_ids is not None:
        case_evidence &= valid_case_ids
    return observations >= 2 and len(case_evidence) >= 2
