"""Independent SQLite implementation for Praxis Memory.

The module deliberately has no RouteCraft Core or dashboard dependency.  Import
adapters inspect legacy sources read-only and only write this module's database.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # The protocols package is supplied by the RouteCraft integration layer.
    from routecraft_protocols import validate_event  # type: ignore
except ModuleNotFoundError:  # Frozen local contract so the standalone tool remains usable.
    def validate_event(payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("event must be an object")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 65_536:
            raise ValueError("event too large")

try:  # Canonical Decision Store parsing/validation; this is a read-only adapter.
    from routecraft_memory_lib.records import load_record, validate_record
except ModuleNotFoundError:  # pragma: no cover - package releases include the adapter.
    load_record = None  # type: ignore[assignment]
    validate_record = None  # type: ignore[assignment]

PACKAGE = "praxis-memory"
PRODUCT_NAME = os.environ.get("PRAXIS_MEMORY_PRODUCT_NAME", "Praxis")
API_VERSION = "1"
PACKAGE_API_VERSION = API_VERSION
SCHEMA_VERSION = 1
RECORD_TYPES = (
    "fact", "case", "decision", "failure", "solution", "project_state", "session",
    "event", "policy", "skill_metadata", "experience",
)
_CLASSIFICATIONS = {
    "normal", "benchmark_run", "migration_event", "incident_response", "token_burn_event",
    "reset_expectation", "manual_stress_test", "release_validation",
    # RouteCraft Protocol v1 spellings are accepted as well.  Records stay
    # standalone, but event ingestion must not reinterpret a valid protocol.
    "benchmark_event", "stress_test", "manual_override",
}
_LOCKS: dict[str, threading.RLock] = {}
_LOCK_GUARD = threading.Lock()
_MAX_TEXT = 20_000
_MAX_TITLE = 500
_MAX_LIST = 64
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----[\s\S]*?-----END(?: [A-Z]+)? PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?im)^\s*(?:api[_-]?key|client[_-]?secret|password|passwd|secret|token)\s*[:=]\s*[^\s#]{6,}"),
)
_PATH_RE = re.compile(
    r"(?:^|[\s\"'(<\[{:;,=])(?:[A-Za-z]:[\\/]|/(?!/)[^\s/]+(?:/[^\s]*)?|\\\\[^\s\\]+\\[^\s]+)"
)


class PraxisMemoryError(RuntimeError):
    exit_code = 2


class IntegrityError(PraxisMemoryError):
    exit_code = 3


class ConflictError(PraxisMemoryError):
    exit_code = 4


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _lock(path: Path) -> threading.RLock:
    with _LOCK_GUARD:
        return _LOCKS.setdefault(str(path), threading.RLock())


def _assert_clean(value: Any, field: str) -> str:
    text = str(value if value is not None else "")
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        raise PraxisMemoryError(f"secret-like input rejected in {field}")
    return text


def _safe_source_label(value: str) -> str:
    # Receipts intentionally contain no source paths. A stable opaque identity
    # still permits audit and duplicate detection.
    return "SRC-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _bounded_text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise PraxisMemoryError(f"{field} must be a string")
    value = _assert_clean(value, field)
    if required and not value.strip():
        raise PraxisMemoryError(f"{field} is required")
    if len(value) > maximum:
        raise PraxisMemoryError(f"{field} exceeds {maximum} characters")
    return value


def _string_list(value: Iterable[Any], field: str) -> list[str]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise PraxisMemoryError(f"{field} must be a list")
    out = []
    for item in value:
        text = _bounded_text(item, field, 160, required=True)
        if text not in out:
            out.append(text)
    if len(out) > _MAX_LIST:
        raise PraxisMemoryError(f"{field} has too many values")
    return out


def _finite(value: Any, field: str, *, minimum: float = 0, maximum: float = 1, integer: bool = False) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PraxisMemoryError(f"{field} must be a finite number")
    if value < minimum or value > maximum:
        raise PraxisMemoryError(f"{field} must be between {minimum} and {maximum}")
    if integer and int(value) != value:
        raise PraxisMemoryError(f"{field} must be an integer")
    return int(value) if integer else float(value)


def _object(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise PraxisMemoryError(f"{field} must be an object")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PraxisMemoryError(f"{field} must contain JSON values") from exc
    _assert_clean(encoded, field)
    if len(encoded) > _MAX_TEXT:
        raise PraxisMemoryError(f"{field} exceeds {_MAX_TEXT} characters")
    return dict(value)


def _validate_failure(value: Any) -> dict[str, Any] | None:
    failure = _object(value, "failure")
    if failure is None:
        return None
    allowed = {"trigger", "action", "result", "root_cause", "mitigation", "avoid_next_time"}
    unknown = set(failure) - allowed
    if unknown:
        raise PraxisMemoryError("unsupported failure fields")
    for name in allowed:
        if name in failure:
            failure[name] = _bounded_text(failure[name], f"failure.{name}", 4_000, required=True)
    return failure


def _validate_experience(value: Any) -> dict[str, Any] | None:
    experience = _object(value, "experience")
    if experience is None:
        return None
    allowed = {"task", "context", "strategy", "execution", "result", "evaluation", "reuse_count", "success_rate", "reliability", "environment"}
    unknown = set(experience) - allowed
    if unknown:
        raise PraxisMemoryError("unsupported experience fields")
    for name in ("task", "context", "strategy", "execution", "result", "evaluation", "environment"):
        if name in experience:
            experience[name] = _bounded_text(experience[name], f"experience.{name}", 4_000, required=True)
    if "reuse_count" in experience:
        experience["reuse_count"] = _finite(experience["reuse_count"], "experience.reuse_count", maximum=1_000_000, integer=True)
    for name in ("success_rate", "reliability"):
        if name in experience:
            experience[name] = _finite(experience[name], f"experience.{name}")
    return experience


def _parse_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class PraxisMemory:
    """Versioned SQLite Praxis store, intentionally independent from RouteCraft Core."""

    def __init__(self, directory: str | Path | None = None):
        base = Path(directory) if directory is not None else Path.home() / ".praxis-memory"
        if base.expanduser().is_symlink():
            raise IntegrityError("Praxis data directory must not be a symlink")
        self.directory = base.expanduser().resolve()
        if any((candidate / ".routecraft-store.json").is_file() for candidate in (self.directory, *self.directory.parents)):
            raise IntegrityError("Praxis data directory must not reuse a Decision Store")
        self.path = self.directory / "praxis-memory.sqlite3"

    @contextmanager
    def _db(self, *, write: bool = False):
        if self.path.is_symlink():
            raise IntegrityError("Praxis database must not be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True)
        with _lock(self.path):
            db = sqlite3.connect(self.path, timeout=8, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA busy_timeout=8000")
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()
            finally:
                db.close()

    def initialize(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise IntegrityError("Praxis database must not be a symlink")
        existed = self.path.is_file() and self.path.stat().st_size > 0
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._db(write=True) as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if existed:
                if version != SCHEMA_VERSION:
                    raise IntegrityError(f"existing database schema {version} is not supported")
                required_columns = {
                    "records": {"id", "record_type", "title", "body", "project", "tags", "confidence", "status", "event_classification", "experience", "failure", "source", "source_ref", "source_identity", "verified", "content_hash", "created_at", "updated_at"},
                    "events": {"id", "event_hash", "source", "event_classification", "payload", "created_at"},
                    "import_runs": {"id", "source_kind", "source_label", "source_hash", "applied", "result", "created_at"},
                    "import_items": {"source_identity", "content_hash", "record_id", "source_kind", "imported_at"},
                    "import_conflicts": {"id", "source_identity", "existing_hash", "incoming_hash", "source_kind", "created_at"},
                    "quarantine": {"id", "source_kind", "reason", "content_hash", "created_at"},
                }
                tables = {str(row["name"]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not set(required_columns).issubset(tables):
                    raise IntegrityError("existing Praxis database tables are incomplete")
                for table, required in required_columns.items():
                    columns = {str(row["name"]) for row in db.execute("SELECT name FROM pragma_table_info(?)", (table,))}
                    if not required.issubset(columns):
                        raise IntegrityError(f"existing Praxis database table {table} is incompatible")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, record_type TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                    project TEXT, tags TEXT NOT NULL, confidence REAL, status TEXT, event_classification TEXT NOT NULL,
                    experience TEXT, failure TEXT, source TEXT NOT NULL, source_ref TEXT, source_identity TEXT,
                    verified INTEGER NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS records_source_identity ON records(source_identity) WHERE source_identity IS NOT NULL;
                CREATE INDEX IF NOT EXISTS records_recall ON records(event_classification, created_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, event_hash TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE, event_classification TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_runs (
                    id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, source_label TEXT NOT NULL, source_hash TEXT NOT NULL,
                    applied INTEGER NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_items (
                    source_identity TEXT PRIMARY KEY, content_hash TEXT NOT NULL, record_id TEXT,
                    source_kind TEXT NOT NULL, imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_conflicts (
                    id TEXT PRIMARY KEY, source_identity TEXT NOT NULL, existing_hash TEXT NOT NULL,
                    incoming_hash TEXT NOT NULL, source_kind TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quarantine (
                    id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, reason TEXT NOT NULL, content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            event_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(events)")}
            if "event_id" not in event_columns:
                # Existing v1 event rows predate protocol identity. They remain
                # readable; future writes use the immutable protocol event_id.
                db.execute("ALTER TABLE events ADD COLUMN event_id TEXT")
                for row in db.execute("SELECT id,payload FROM events WHERE event_id IS NULL"):
                    payload = _parse_json(row["payload"], {})
                    event_id = payload.get("event_id") if isinstance(payload, Mapping) else None
                    if isinstance(event_id, str) and event_id:
                        db.execute("UPDATE events SET event_id=? WHERE id=?", (event_id, row["id"]))
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_event_id ON events(event_id) WHERE event_id IS NOT NULL")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return {"package": PACKAGE, "product_name": PRODUCT_NAME, "api_version": "1", "schema_version": SCHEMA_VERSION, "database": self.path.name}

    def _record(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key, fallback in (("tags", []), ("experience", None), ("failure", None)):
            item[key] = _parse_json(item[key], fallback) if item[key] is not None else fallback
        item["verified"] = bool(item["verified"])
        item.pop("source_identity", None)
        return item

    def _prepare_record(self, record_type: str, title: Any, body: Any, *, project: Any = None, tags: Iterable[Any] = (), confidence: Any = None, status: Any = None, event_classification: str = "normal", experience: Any = None, failure: Any = None, source: Any = "api", source_ref: Any = None, verified: Any = False, source_identity: str | None = None) -> dict[str, Any]:
        if record_type not in RECORD_TYPES:
            raise PraxisMemoryError("unsupported record_type")
        if event_classification not in _CLASSIFICATIONS:
            raise PraxisMemoryError("unsupported event_classification")
        if not isinstance(verified, bool):
            raise PraxisMemoryError("verified must be boolean")
        title = _bounded_text(title, "title", _MAX_TITLE, required=True)
        body = _bounded_text(body, "body", _MAX_TEXT, required=True)
        project_value = None if project is None else _bounded_text(project, "project", 500, required=True)
        status_value = None if status is None else _bounded_text(status, "status", 160, required=True)
        source_value = _bounded_text(source, "source", 160, required=True)
        source_ref_value = None if source_ref is None else _bounded_text(source_ref, "source_ref", 500, required=True)
        if source_ref_value and _PATH_RE.search(source_ref_value):
            source_ref_value = _safe_source_label(source_ref_value)
        experience_value = _validate_experience(experience)
        failure_value = _validate_failure(failure)
        if record_type == "failure" and failure_value is None:
            raise PraxisMemoryError("failure records require failure details")
        if record_type == "experience" and experience_value is None:
            raise PraxisMemoryError("experience records require experience details")
        content = {"record_type": record_type, "title": title, "body": body, "project": project_value, "tags": _string_list(tags, "tags"), "confidence": _finite(confidence, "confidence"), "status": status_value, "event_classification": event_classification, "experience": experience_value, "failure": failure_value, "source": source_value, "source_ref": source_ref_value, "verified": verified}
        now = _now()
        return {"id": "PM-" + uuid.uuid4().hex, **content, "tags": json.dumps(content["tags"], ensure_ascii=False), "experience": json.dumps(experience_value, ensure_ascii=False, sort_keys=True) if experience_value is not None else None, "failure": json.dumps(failure_value, ensure_ascii=False, sort_keys=True) if failure_value is not None else None, "source_identity": source_identity, "content_hash": _hash(content), "verified": int(verified), "created_at": now, "updated_at": now}

    def add_record(self, record_type: str, title: str, body: str, *, project: str | None = None, tags: Iterable[str] = (), confidence: float | None = None, status: str | None = None, event_classification: str = "normal", experience: Mapping[str, Any] | None = None, failure: Mapping[str, Any] | None = None, source: str = "api", source_ref: str | None = None, verified: bool = False) -> dict[str, Any]:
        item = self._prepare_record(record_type, title, body, project=project, tags=tags, confidence=confidence, status=status, event_classification=event_classification, experience=experience, failure=failure, source=source, source_ref=source_ref, verified=verified)
        self.initialize()
        with self._db(write=True) as db:
            db.execute("""INSERT INTO records(id,record_type,title,body,project,tags,confidence,status,event_classification,experience,failure,source,source_ref,source_identity,verified,content_hash,created_at,updated_at)
                VALUES(:id,:record_type,:title,:body,:project,:tags,:confidence,:status,:event_classification,:experience,:failure,:source,:source_ref,:source_identity,:verified,:content_hash,:created_at,:updated_at)""", item)
            row = db.execute("SELECT * FROM records WHERE id=?", (item["id"],)).fetchone()
        return self._record(row)

    def recall(self, query: str | Any, *, limit: int = 5, tags: Iterable[str] = (), include_special_events: bool = False) -> list[dict[str, Any]]:
        # Accept the public string API and duck-type a Core request without
        # importing Core.  This keeps the standalone package dependency-free.
        requested_project = None
        if not isinstance(query, str):
            if isinstance(query, Mapping):
                requested_project = query.get("project")
                query = query.get("task", "")
            else:
                requested_project = getattr(query, "project", None)
                query = getattr(query, "task", "")
        query = _bounded_text(query, "query", 8_000)
        requested_project = None if requested_project is None else _bounded_text(requested_project, "project", 500, required=True)
        requested_tags = {tag.casefold() for tag in _string_list(tags, "tags")}
        try:
            limit = max(0, min(int(limit), 100))
        except (TypeError, ValueError) as exc:
            raise PraxisMemoryError("limit must be an integer") from exc
        self.initialize()
        with self._db() as db:
            if include_special_events:
                selected = db.execute("SELECT * FROM records")
            else:
                selected = db.execute("SELECT * FROM records WHERE event_classification='normal'")
            rows = [self._record(row) for row in selected]
        terms = [part.casefold() for part in query.split() if part]
        now = datetime.now(timezone.utc)
        output = []
        for item in rows:
            tagset = {tag.casefold() for tag in item["tags"]}
            if requested_tags and not requested_tags.issubset(tagset):
                continue
            text = " ".join((item["title"], item["body"], " ".join(item["tags"]))).casefold()
            matches = sum(1 for term in terms if term in text)
            if terms and not matches:
                continue
            try:
                age_days = max(0.0, (now - datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))).total_seconds() / 86400)
                recency = round(1 / (1 + age_days / 30), 6)
            except (TypeError, ValueError):
                recency = None
            exp = item["experience"] or {}
            components = {
                "relevance": round(matches / len(terms), 6) if terms else 0.0,
                "recency": recency,
                "confidence": item["confidence"],
                "success_rate": exp.get("success_rate"),
                "reuse": exp.get("reuse_count"),
                "project_similarity": (1.0 if item["project"] == requested_project else 0.0) if requested_project is not None and item["project"] is not None else None,
                "environment_similarity": None,
                "reliability": exp.get("reliability"),
            }
            numeric = [value for value in (components["relevance"], recency, item["confidence"], exp.get("success_rate"), exp.get("reliability")) if isinstance(value, (int, float))]
            score = round(sum(numeric) / len(numeric), 6) if numeric else 0.0
            item["score"] = score
            item["score_components"] = components
            output.append(item)
        return sorted(output, key=lambda value: (value["score"], value["created_at"], value["id"]), reverse=True)[:limit]

    def store_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(event, Mapping):
            raise PraxisMemoryError("event must be an object")
        event_copy = dict(event)
        encoded = json.dumps(event_copy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        _assert_clean(encoded, "event")
        if _PATH_RE.search(encoded):
            raise PraxisMemoryError("raw paths are not accepted in events")
        try:
            validate_event(event_copy)
        except Exception as exc:
            raise PraxisMemoryError(f"invalid event: {exc}") from exc
        classification = str(event_copy.get("event_classification", "normal"))
        if classification not in _CLASSIFICATIONS:
            raise PraxisMemoryError("unsupported event_classification")
        source = _bounded_text(event_copy.get("source", "api"), "event.source", 160, required=True)
        event_id = _bounded_text(event_copy.get("event_id"), "event.event_id", 200, required=True)
        event_hash = _hash(event_copy)
        self.initialize()
        with self._db(write=True) as db:
            existing = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if existing:
                if existing["event_hash"] == event_hash:
                    return {"stored": False, "duplicate": True, "event": self._event(existing)}
                raise ConflictError("event_id is already stored with different content")
            item = {"id": "EVT-" + uuid.uuid4().hex, "event_hash": event_hash, "source": source, "event_id": event_id, "event_classification": classification, "payload": encoded, "created_at": _now()}
            db.execute("INSERT INTO events(id,event_hash,source,event_id,event_classification,payload,created_at) VALUES(:id,:event_hash,:source,:event_id,:event_classification,:payload,:created_at)", item)
            return {"stored": True, "duplicate": False, "event": self._event(item)}

    @staticmethod
    def _event(row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result.pop("event_hash", None)
        result["payload"] = _parse_json(result["payload"], {})
        return result

    def list_events(self, *, limit: int = 100, cursor: str | None = None, source: str | None = None, include_special_events: bool = True) -> dict[str, Any]:
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError) as exc:
            raise PraxisMemoryError("limit must be an integer") from exc
        after = None
        if cursor:
            try:
                after = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
                if not isinstance(after, list) or len(after) != 2 or not all(isinstance(item, str) for item in after):
                    raise ValueError
            except Exception as exc:
                raise PraxisMemoryError("invalid cursor") from exc
        self.initialize()
        source_value = None
        if source is not None:
            source_value = _bounded_text(source, "source", 160, required=True)
        after_time = after[0] if after else None
        after_id = after[1] if after else None
        with self._db() as db:
            rows = [self._event(row) for row in db.execute(
                """SELECT * FROM events
                   WHERE (?=1 OR event_classification='normal')
                     AND (? IS NULL OR source=?)
                     AND (? IS NULL OR created_at<? OR (created_at=? AND id<?))
                   ORDER BY created_at DESC,id DESC LIMIT ?""",
                (
                    int(include_special_events),
                    source_value, source_value,
                    after_time, after_time, after_time, after_id,
                    limit + 1,
                ),
            )]
        more = len(rows) > limit
        items = rows[:limit]
        next_cursor = None
        if more and items:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last["created_at"], last["id"]], separators=(",", ":")).encode("utf-8")).decode("ascii")
        return {"items": items, "next_cursor": next_cursor}

    def remember(self, experience: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(experience, Mapping):
            raise PraxisMemoryError("experience must be an object")
        detail_keys = {"task", "context", "strategy", "execution", "result", "evaluation", "reuse_count", "success_rate", "reliability", "environment"}
        details = _validate_experience({key: value for key, value in experience.items() if key in detail_keys})
        assert details is not None
        title = str(details.get("task") or "Experience")
        body = "\n".join(f"{name}: {details[name]}" for name in ("task", "context", "strategy", "execution", "result", "evaluation") if details.get(name)) or title
        return self.add_record("experience", title[:_MAX_TITLE], body, project=experience.get("project"), tags=experience.get("tags", ()), confidence=experience.get("confidence"), status=experience.get("status"), event_classification=experience.get("event_classification", "normal"), experience=details, source="core_adapter", source_ref=experience.get("source_ref"), verified=bool(experience.get("verified", False)))

    def notify_experience(self, experience: Mapping[str, Any]) -> dict[str, Any] | None:
        """Duck-typed Core port hook; ignore summaries lacking reusable detail."""
        if not isinstance(experience, Mapping):
            raise PraxisMemoryError("experience must be an object")
        if not any(key in experience for key in ("task", "context", "strategy", "execution", "result", "evaluation")):
            return None
        return self.remember(experience)

    def notify_outcome(self, outcome: Mapping[str, Any]) -> None:
        # Core's bounded outcome summary has no reusable experience by itself.
        if not isinstance(outcome, Mapping):
            raise PraxisMemoryError("outcome must be an object")

    def emit(self, event: Mapping[str, Any]) -> None:
        """Duck-typed EventSink hook for the shared protocol contract."""
        self.store_event(event)

    def status(self) -> dict[str, Any]:
        self.initialize()
        with self._db() as db:
            counts = {row["record_type"]: row["count"] for row in db.execute("SELECT record_type,COUNT(*) AS count FROM records GROUP BY record_type")}
            event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            conflicts = db.execute("SELECT COUNT(*) FROM import_conflicts").fetchone()[0]
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
        if integrity != "ok":
            raise IntegrityError("SQLite integrity check failed")
        return {"package": PACKAGE, "product_name": PRODUCT_NAME, "api_version": "1", "schema_version": version, "records": counts, "events": event_count, "import_conflicts": conflicts, "integrity": integrity, "database": self.path.name}

    def _source_database(self, source_database: str | Path) -> tuple[Path, sqlite3.Connection]:
        path = Path(source_database).expanduser()
        if path.is_symlink() or not path.is_file() or path.resolve() == self.path.resolve():
            raise IntegrityError("source database must be a distinct regular non-symlink file")
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError("integrity check failed")
        except (OSError, sqlite3.DatabaseError) as exc:
            if db is not None:
                db.close()
            raise IntegrityError("source database is unreadable or corrupt") from exc
        assert db is not None
        return path.resolve(), db

    def _local_candidates(self, source_database: str | Path) -> tuple[list[dict[str, Any]], str]:
        path, db = self._source_database(source_database)
        try:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            columns = {row["name"] for row in db.execute("PRAGMA table_info(memories)")}
            required = {
                "id", "project_id", "memory_type", "title", "body", "importance",
                "tags", "source", "related_files", "related_commits", "active",
                "verified", "source_ref", "content_hash", "legacy_metadata",
            }
            if version != 1 or not required.issubset(columns):
                raise IntegrityError("unknown RouteCraft Local schema")
            projects = {
                str(row["id"]): str(row["name"])
                for row in db.execute("SELECT id,name FROM projects")
            }
            rows = list(db.execute(
                "SELECT id,project_id,memory_type,title,body,importance,tags,source,"
                "related_files,related_commits,active,verified,source_ref,content_hash,legacy_metadata FROM memories"
            ))
        except sqlite3.DatabaseError as exc:
            raise IntegrityError("RouteCraft Local schema cannot be read") from exc
        finally:
            db.close()
        candidates = []
        for row in rows:
            legacy_type = str(row["memory_type"])
            record_type = legacy_type
            if record_type not in RECORD_TYPES:
                record_type = "fact"
            tags = _parse_json(row["tags"], [])
            if not isinstance(tags, list):
                raise IntegrityError("RouteCraft Local tags are malformed")
            related_files = _parse_json(row["related_files"], [])
            related_commits = _parse_json(row["related_commits"], [])
            if not isinstance(related_files, list) or not isinstance(related_commits, list):
                raise IntegrityError("RouteCraft Local relation fields are malformed")
            tags = [*tags, f"legacy_type:{legacy_type}", f"importance:{row['importance']}"]
            if not bool(row["active"]):
                tags.append("legacy_inactive")
            if related_files:
                tags.append(f"legacy_related_files:{len(related_files)}")
            if related_commits:
                tags.append(f"legacy_related_commits:{len(related_commits)}")
            meta = _parse_json(row["legacy_metadata"], {})
            if not isinstance(meta, dict):
                meta = {}
            project = meta.get("project") or projects.get(str(row["project_id"])) or str(row["project_id"])
            status = meta.get("status") or ("inactive" if not bool(row["active"]) else None)
            failure = meta.get("failure")
            if record_type == "failure" and failure is None:
                # Legacy Local allowed unstructured failure notes. Preserve the
                # record body and mark structured fields as unknown, rather than
                # inventing a cause or dropping the record.
                failure = {}
            candidates.append(self._prepare_record(record_type, row["title"], row["body"], project=project, tags=tags, confidence=meta.get("confidence"), status=status, event_classification=meta.get("event_classification", "normal"), experience=meta.get("experience"), failure=failure, source="routecraft-local", source_ref=row["source_ref"] or None, verified=bool(row["verified"]), source_identity="routecraft-local:" + _safe_source_label(str(row["id"]))))
        return candidates, _hash({"source": "routecraft-local", "bytes": hashlib.sha256(path.read_bytes()).hexdigest()})

    @staticmethod
    def _decision_records(source_directory: str | Path, project: str) -> tuple[list[dict[str, Any]], str]:
        if load_record is None or validate_record is None:
            raise IntegrityError("canonical Decision Store validator is unavailable")
        directory = Path(source_directory).expanduser()
        if directory.is_symlink() or not directory.is_dir() or (directory / ".routecraft-store.json").is_symlink():
            raise IntegrityError("Decision Store source must be a non-symlink directory")
        marker = directory / ".routecraft-store.json"
        if not marker.is_file():
            raise IntegrityError("Decision Store marker is missing")
        try:
            config = json.loads(marker.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IntegrityError("Decision Store marker is corrupt") from exc
        if not isinstance(config, Mapping) or config.get("schema_version") != 1:
            raise IntegrityError("unknown Decision Store schema")
        output, fingerprints, seen_ids = [], [], set()
        # Candidates are hypotheses, not authoritative decisions.  This mirrors
        # RouteCraft Local's import which keeps candidates as notes.  Praxis
        # has no note type, so a fact plus provenance tag retains that boundary.
        mapping = {
            "cases": ("case", "case"),
            "candidates": ("candidate", "fact"),
            "rules": ("rule", "policy"),
        }
        for folder, (expected_kind, record_type) in mapping.items():
            location = directory / folder
            if location.exists() and (location.is_symlink() or not location.is_dir()):
                raise IntegrityError("Decision Store directory is unsafe")
            if not location.exists():
                continue
            for file in sorted(location.glob("*.md")):
                if file.is_symlink() or not file.is_file():
                    raise IntegrityError("Decision Store record is unsafe")
                try:
                    record = load_record(file)
                    errors = validate_record(record, expected_kind=expected_kind)
                except Exception as exc:
                    raise IntegrityError("Decision Store record cannot be parsed") from exc
                if errors:
                    raise IntegrityError("Decision Store record validation failed: " + "; ".join(errors))
                identifier = record.record_id
                if identifier in seen_ids:
                    raise IntegrityError("Decision Store contains duplicate record IDs")
                seen_ids.add(identifier)
                meta, body = record.metadata, record.body.strip()
                tags = meta.get("tags") or []
                if not isinstance(tags, list):  # Defensive; canonical validation also checks this.
                    raise IntegrityError("Decision Store tags are malformed")
                tags = [*(str(tag) for tag in tags), f"decision_store_kind:{expected_kind}"]
                verified = expected_kind == "case" or (
                    expected_kind == "rule" and str(meta.get("status") or "").strip().casefold() == "validated"
                )
                output.append({"record_type": record_type, "title": str(meta["title"]), "body": body, "project": project, "tags": tags, "confidence": meta.get("confidence"), "status": None if meta.get("status") is None else str(meta["status"]), "source": "decision-store", "source_ref": identifier, "verified": verified, "source_identity": "decision-store:" + _safe_source_label(identifier)})
                try:
                    text = file.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise IntegrityError("Decision Store record cannot be read") from exc
                fingerprints.append({"id": identifier, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
        return output, _hash({"source": "decision-store", "records": fingerprints})

    def _migration(self, source_kind: str, candidates: list[dict[str, Any]], source_hash: str, *, apply: bool, confirmation: str | None) -> dict[str, Any]:
        # Candidates are fully parsed and validated before the target database is touched.
        prepared = [candidate if "id" in candidate else self._prepare_record(**candidate) for candidate in candidates]
        report: dict[str, Any] = {"source": source_kind, "before": 0, "created": 0, "skipped": 0, "conflict": 0, "after": 0, "backup": None, "dry_run": not apply}
        if not apply:
            existing_items: dict[str, str] = {}
            if self.path.is_file() and self.path.stat().st_size:
                try:
                    db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
                    db.row_factory = sqlite3.Row
                    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or int(db.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
                        raise sqlite3.DatabaseError("target integrity or schema mismatch")
                    report["before"] = int(db.execute("SELECT COUNT(*) FROM records").fetchone()[0])
                    existing_items = {str(row["source_identity"]): str(row["content_hash"]) for row in db.execute("SELECT source_identity,content_hash FROM import_items")}
                except sqlite3.DatabaseError as exc:
                    raise IntegrityError("existing Praxis database cannot be previewed") from exc
                finally:
                    if "db" in locals():
                        db.close()
            for item in prepared:
                identity = str(item["source_identity"])
                current = existing_items.get(identity)
                if current is None:
                    report["created"] += 1
                    existing_items[identity] = str(item["content_hash"])
                elif current == item["content_hash"]:
                    report["skipped"] += 1
                else:
                    report["conflict"] += 1
            report["after"] = report["before"] + report["created"]
            return report
        if confirmation != "MIGRATE":
            raise PraxisMemoryError("apply requires exact confirmation MIGRATE")
        existed = self.path.exists() and self.path.stat().st_size > 0
        if existed:
            backup = self.directory / f"praxis-memory-backup-{_now().replace(':', '').replace('+', '')}-{uuid.uuid4().hex[:8]}.sqlite3"
            source = None
            destination = None
            backup_error: Exception | None = None
            try:
                source = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("target integrity check failed")
                destination = sqlite3.connect(backup)
                source.backup(destination)
                destination.commit()
            except (OSError, sqlite3.DatabaseError) as exc:
                backup_error = exc
            finally:
                if destination is not None:
                    destination.close()
                if source is not None:
                    source.close()
            if backup_error is not None:
                if backup.exists():
                    backup.unlink()
                raise IntegrityError("existing Praxis database cannot be backed up") from backup_error
            report["backup"] = backup.name
        self.initialize()
        with self._db(write=True) as db:
            report["before"] = int(db.execute("SELECT COUNT(*) FROM records").fetchone()[0])
            for item in prepared:
                existing = db.execute("SELECT content_hash,record_id FROM import_items WHERE source_identity=?", (item["source_identity"],)).fetchone()
                if existing:
                    if existing["content_hash"] == item["content_hash"]:
                        report["skipped"] += 1
                        continue
                    db.execute("INSERT INTO import_conflicts VALUES(?,?,?,?,?,?)", ("IC-" + uuid.uuid4().hex, item["source_identity"], existing["content_hash"], item["content_hash"], source_kind, _now()))
                    report["conflict"] += 1
                    continue
                db.execute("""INSERT INTO records(id,record_type,title,body,project,tags,confidence,status,event_classification,experience,failure,source,source_ref,source_identity,verified,content_hash,created_at,updated_at)
                    VALUES(:id,:record_type,:title,:body,:project,:tags,:confidence,:status,:event_classification,:experience,:failure,:source,:source_ref,:source_identity,:verified,:content_hash,:created_at,:updated_at)""", item)
                db.execute("INSERT INTO import_items VALUES(?,?,?,?,?)", (item["source_identity"], item["content_hash"], item["id"], source_kind, _now()))
                report["created"] += 1
            report["after"] = int(db.execute("SELECT COUNT(*) FROM records").fetchone()[0])
            result = {key: report[key] for key in ("before", "created", "skipped", "conflict", "after")}
            db.execute("INSERT INTO import_runs VALUES(?,?,?,?,?,?,?)", ("IR-" + uuid.uuid4().hex, source_kind, _safe_source_label(source_kind + source_hash), source_hash, 1, json.dumps(result, sort_keys=True), _now()))
        return report

    def migrate_from_routecraft_local(self, source_database: str | Path, *, apply: bool = False, confirmation: str | None = None) -> dict[str, Any]:
        candidates, source_hash = self._local_candidates(source_database)
        return self._migration("routecraft-local", candidates, source_hash, apply=apply, confirmation=confirmation)

    def migrate_from_decision_store(self, source_directory: str | Path, *, apply: bool = False, confirmation: str | None = None, project: str = "legacy") -> dict[str, Any]:
        project = _bounded_text(project, "project", 500, required=True)
        raw, source_hash = self._decision_records(source_directory, project)
        candidates = [self._prepare_record(**item) for item in raw]
        return self._migration("decision-store", candidates, source_hash, apply=apply, confirmation=confirmation)
