"""Loopback-only, read-only server for the standalone Praxis Dashboard."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .query import CompositeEventSource, JsonlEventSource, LegacyTelemetryEventSource, PraxisDashboardQuery

_ASSETS = Path(__file__).with_name("assets")
_STATIC_FILES = {"/": ("text/html; charset=utf-8", "index.html"), "/styles.css": ("text/css; charset=utf-8", "styles.css"), "/app.js": ("application/javascript; charset=utf-8", "app.js")}


def _static(path: str) -> tuple[str, bytes] | None:
    item = _STATIC_FILES.get(path)
    if item is None:
        return None
    try:
        return item[0], (_ASSETS / item[1]).read_bytes()
    except OSError:
        return None


class SQLiteEventSource:
    """Read a pre-existing Praxis SQLite file without initializing it."""
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
    def _uri(self) -> str:
        return self.path.resolve().as_uri() + "?mode=ro"
    def _validate(self) -> bool:
        if not self.path.is_file() or self.path.is_symlink():
            return False
        try:
            with closing(sqlite3.connect(self._uri(), uri=True, timeout=2)) as db:
                db.execute("PRAGMA query_only=ON")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or int(db.execute("PRAGMA user_version").fetchone()[0]) != 1:
                    raise sqlite3.DatabaseError("unsupported schema")
                required = {"id", "source", "event_classification", "payload", "created_at"}
                if not required.issubset({str(row[1]) for row in db.execute("PRAGMA table_info(events)") }):
                    raise sqlite3.DatabaseError("events table is incompatible")
        except (OSError, sqlite3.DatabaseError) as exc:
            raise ValueError("Praxis SQLite source is invalid") from exc
        return True
    def sources(self) -> list[dict[str, Any]]:
        return [{"id": "praxis-memory", "available": self._validate()}]
    def list_events(self, *, limit: int, cursor: str | None = None, source: str | None = None, include_special_events: bool = True) -> dict[str, Any]:
        if not self._validate():
            return {"events": [], "cursor": None}
        try:
            offset = max(0, int(cursor or "0"))
        except ValueError:
            offset = 0
        try:
            with closing(sqlite3.connect(self._uri(), uri=True, timeout=2)) as db:
                db.execute("PRAGMA query_only=ON")
                rows = db.execute(
                    "SELECT payload FROM events "
                    "WHERE (? IS NULL OR source=?) AND (?=1 OR event_classification='normal') "
                    "ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                    (source, source, int(include_special_events), limit + 1, offset),
                ).fetchall()
        except (OSError, sqlite3.DatabaseError) as exc:
            raise ValueError("Praxis SQLite source cannot be read") from exc
        events = []
        for (payload,) in rows:
            try:
                value = json.loads(payload)
                if isinstance(value, dict):
                    events.append(value)
            except (TypeError, json.JSONDecodeError):
                continue
        return {"events": events[:limit], "cursor": str(offset + limit) if len(events) > limit else None}


def query_for_directory(directory: str | Path) -> PraxisDashboardQuery:
    base = Path(directory).expanduser()
    if base.is_symlink():
        return PraxisDashboardQuery()
    sources: list[Any] = []
    jsonl, database = base / "praxis-events.jsonl", base / "praxis-memory.sqlite3"
    if jsonl.is_file() and not jsonl.is_symlink():
        sources.append(JsonlEventSource(jsonl))
    elif database.is_file() and not database.is_symlink():
        sources.append(SQLiteEventSource(database))
    for name in ("routecraft-telemetry.json", "routecraft_telemetry.json", "routecraft-collector.json", "routecraft_collector.json"):
        payload = base / name
        if payload.is_file() and not payload.is_symlink():
            sources.append(LegacyTelemetryEventSource(payload, "legacy-" + name.rsplit(".", 1)[0].replace("_", "-")))
    return PraxisDashboardQuery(sources[0] if len(sources) == 1 else CompositeEventSource(sources) if sources else None)


def create_server(query: PraxisDashboardQuery, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("Praxis Dashboard は 127.0.0.1 にのみ bind できます")
    class Handler(BaseHTTPRequestHandler):
        server_version = "PraxisDashboard/1"
        def log_message(self, _format: str, *_args: Any) -> None:
            pass
        def _headers(self, api: bool = False) -> None:
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if api:
                self.send_header("Cache-Control", "no-store")
        def _json(self, status: int, payload: Any) -> None:
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status); self._headers(True); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def _host_ok(self) -> bool:
            return self.headers.get("Host", "").split(":", 1)[0] == "127.0.0.1"
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": {"code": "host"}})
            static = _static(parsed.path)
            if static is not None:
                content_type, raw = static
                self.send_response(200); self._headers(False); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
                return
            args = parse_qs(parsed.query)
            if parsed.path == "/api/praxis/v1/snapshot":
                return self._json(200, query.snapshot())
            if parsed.path == "/api/praxis/v1/sources":
                return self._json(200, query.sources())
            if parsed.path == "/api/praxis/v1/events":
                try: limit = int((args.get("limit") or ["100"])[-1])
                except ValueError: return self._json(400, {"ok": False, "error": {"code": "limit"}})
                return self._json(200, query.events(limit, cursor=(args.get("cursor") or [None])[-1], source=(args.get("source") or [None])[-1]))
            if parsed.path == "/api/praxis/v1/runs":
                try: limit = int((args.get("limit") or ["100"])[-1])
                except ValueError: return self._json(400, {"ok": False, "error": {"code": "limit"}})
                return self._json(200, query.runs(limit, requested_model=(args.get("requested_model") or [None])[-1], actual_model=(args.get("actual_model") or [None])[-1], requested_reasoning=(args.get("requested_reasoning") or [None])[-1], actual_reasoning=(args.get("actual_reasoning") or [None])[-1]))
            self._json(404, {"ok": False, "error": {"code": "not_found"}})
        def do_POST(self) -> None:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": {"code": "read_only"}})
        do_PATCH = do_POST
        do_DELETE = do_POST
    server = ThreadingHTTPServer((host, port), Handler)
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    return server
