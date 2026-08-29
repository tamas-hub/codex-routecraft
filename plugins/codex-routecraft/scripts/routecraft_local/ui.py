"""Loopback-only, dependency-free web UI for RouteCraft Memory Local."""
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import ConfirmationRequiredError, NotFoundError, RouteCraftLocalError
from .packs import build_context_pack, build_handoff_pack
from .git_tools import inspect_git

WEB_ROOT = Path(__file__).with_name("web")
MAX_BODY = 1_000_000
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "application/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"), "/responsive.css": ("responsive.css", "text/css; charset=utf-8")}


def _result(value: Any) -> Any:
    return value.to_dict() if hasattr(value, "to_dict") else value


def _call(service: Any, names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
    """Call a deliberately small compatibility surface while core lands."""
    for name in names:
        fn = getattr(service, name, None)
        if callable(fn):
            try:
                return _result(fn(*args, **kwargs))
            except TypeError:
                # Service methods which accept a single request mapping remain supported.
                if args and isinstance(args[-1], dict) and not kwargs:
                    return _result(fn(args[-1]))
                raise
    raise NotFoundError(f"機能はまだ利用できません: {names[0]}")


def _api(service: Any, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any], praxis: Any = None) -> Any:
    parts = [p for p in path.split("/") if p]
    if path.startswith("/api/praxis/") and method != "GET":
        raise RouteCraftLocalError("Praxis API は読み取り専用です")
    if path == "/api/praxis/v1/snapshot":
        from praxis_dashboard.query import PraxisDashboardQuery
        return (praxis or PraxisDashboardQuery()).snapshot()
    if path == "/api/praxis/v1/events":
        from praxis_dashboard.query import PraxisDashboardQuery
        try: limit = int((query.get("limit") or ["100"])[-1])
        except ValueError: raise RouteCraftLocalError("limit は整数で指定してください")
        return (praxis or PraxisDashboardQuery()).events(limit=limit, cursor=(query.get("cursor") or [None])[-1], source=(query.get("source") or [None])[-1])
    if path == "/api/praxis/v1/sources":
        from praxis_dashboard.query import PraxisDashboardQuery
        return (praxis or PraxisDashboardQuery()).sources()
    if path == "/api/dashboard":
        projects = _call(service, ("list_projects",))
        memories = _call(service, ("list_memories",), include_inactive=True, limit=1_000_000)
        return {"projects": len(projects), "memories": len(memories), "archived_projects": sum(1 for item in projects if item.get("archived")), "verified_memories": sum(1 for item in memories if item.get("verified"))}
    if path == "/api/doctor": return _call(service, ("doctor",))
    if path == "/api/settings":
        return _call(service, ("settings", "get_settings") if method == "GET" else ("update_settings", "set_settings"), *( [body] if method != "GET" else []))
    if path == "/api/git":
        project = _call(service, ("get_project",), body.get("project_id") or (query.get("project_id") or [None])[0])
        return inspect_git(project.get("repo_path")) if project.get("repo_path") else {"is_repository": False, "changed_files": [], "recent_commits": [], "errors": []}
    if path == "/api/context":
        project_id = body.get("project_id")
        if not project_id: raise RouteCraftLocalError("project_id is required")
        return build_context_pack(service, project_id, format=body.get("format", "markdown"), profile=body.get("profile", "standard"))
    if path == "/api/handoff":
        project_id = body.get("project_id")
        if not project_id: raise RouteCraftLocalError("project_id is required")
        output = body.get("output") or str(service.data_dir / f"handoff-pack-{secrets.token_hex(4)}")
        return build_handoff_pack(service, project_id, output, as_zip=bool(body.get("as_zip", False)))
    if path == "/api/project-package/import":
        if method != "POST":
            raise RouteCraftLocalError("project package import は POST でのみ実行できます")
        source = body.get("path")
        if not isinstance(source, str) or not source.strip():
            raise RouteCraftLocalError("import package path is required")
        return _call(service, ("import_project_package",), source, conflict=body.get("conflict", "detect"))
    if len(parts) == 4 and parts[1] == "projects" and parts[3] == "package":
        ident = parts[2]
        if method != "POST":
            raise RouteCraftLocalError("project package 操作は POST でのみ実行できます")
        action = body.get("action")
        if action == "export":
            output = body.get("output")
            if not isinstance(output, str) or not output.strip():
                raise RouteCraftLocalError("export output path is required")
            return _call(service, ("export_project_package",), ident, output, as_zip=bool(body.get("as_zip", True)))
        raise RouteCraftLocalError("project package action must be export")
    if path == "/api/backups":
        if method == "GET":
            return [{"name": p.name, "path": str(p), "size": p.stat().st_size} for p in sorted(service.data_dir.glob("routecraft-backup-*.zip"), reverse=True)]
        return _call(service, ("backup", "create_backup"), body.get("output") or None)
    if path == "/api/restore":
        if body.get("confirm") != "RESTORE": raise ConfirmationRequiredError("復元するには RESTORE と正確に入力してください")
        return _call(service, ("restore", "restore_backup"), body.get("archive") or body.get("backup_id"), "RESTORE")
    if path == "/api/import": return _call(service, ("import_data", "import_pack"), body)
    if path == "/api/export": return _call(service, ("export_data", "export_pack"), body)
    if path == "/api/projects":
        if method == "GET": return _call(service, ("list_projects", "projects"), include_archived=(query.get("archived") == ["true"]))
        return _call(service, ("add_project", "create_project", "project_create"), body.get("name", ""), repo_path=body.get("repo_path", body.get("path", "")), git_remote_url=body.get("git_remote_url", ""), ai_agents=body.get("ai_agents", ()), languages=body.get("languages", ()), tags=body.get("tags", ()), description=body.get("description", ""), current_objective=body.get("current_objective", ""))
    if len(parts) >= 3 and parts[1] == "projects":
        ident = parts[2]
        if len(parts) == 4 and parts[3] == "archive":
            if method != "POST": raise RouteCraftLocalError("archive は POST でのみ実行できます")
            return _call(service, ("archive_project",), ident, True)
        if method == "GET": return _call(service, ("get_project", "project_get"), ident)
        if method == "PATCH": return _call(service, ("update_project", "project_update"), ident, **{k:v for k,v in body.items() if k != "id"})
        if method == "DELETE":
            if body.get("confirm") != ident: raise ConfirmationRequiredError("削除するにはプロジェクトIDを正確に入力してください")
            return _call(service, ("delete_project", "project_delete"), ident, ident)
    if path == "/api/memories":
        if method == "GET":
            criteria = {key: values[-1] for key, values in query.items() if values}
            if "project_id" in criteria: criteria["project_ref"] = criteria.pop("project_id")
            if not criteria:
                return _call(service, ("list_memories", "memories"))
            search_keys = {"query", "types", "type", "tags", "importance", "created_from", "created_to", "filename", "commit", "active", "verified"}
            if set(criteria) & search_keys:
                if "type" in criteria: criteria["types"] = [criteria.pop("type")]
                for key in ("types", "tags", "importance"):
                    if isinstance(criteria.get(key), str): criteria[key] = [criteria[key]]
                for key in ("active", "verified"):
                    if key in criteria: criteria[key] = criteria[key].lower() == "true"
                return _call(service, ("search_memories", "memory_search"), **criteria)
            return _call(service, ("list_memories", "memories"), **criteria)
        return _call(service, ("add_memory", "create_memory", "memory_create"), body.get("project_id"), body.get("memory_type", body.get("type", "note")), body.get("title", ""), body.get("body", body.get("content", "")), importance=body.get("importance", "medium"), tags=body.get("tags", ()), source=body.get("source", "ui"), related_files=body.get("related_files", ()), related_commits=body.get("related_commits", ()), active=body.get("active", True), verified=body.get("verified", False))
    if path == "/api/memories/search":
        criteria = body if method != "GET" else {k:v[-1] for k,v in query.items()}
        criteria = dict(criteria)
        if "project_id" in criteria: criteria["project_ref"] = criteria.pop("project_id")
        if "type" in criteria: criteria = {**criteria, "types": criteria.pop("type")}
        if isinstance(criteria.get("types"), str): criteria["types"] = [criteria["types"]]
        if isinstance(criteria.get("tags"), str): criteria["tags"] = [criteria["tags"]]
        if isinstance(criteria.get("importance"), str): criteria["importance"] = [criteria["importance"]]
        return _call(service, ("search_memories", "memory_search"), **criteria)
    if len(parts) == 3 and parts[1] == "memories":
        ident = parts[2]
        if method == "GET": return _call(service, ("get_memory", "memory_get"), ident)
        if method == "PATCH":
            changes={k:v for k,v in body.items() if k != "id"};
            if "type" in changes: changes["memory_type"] = changes.pop("type")
            if "content" in changes: changes["body"] = changes.pop("content")
            return _call(service, ("update_memory", "memory_update"), ident, **changes)
        if method == "DELETE":
            if body.get("confirm") != ident: raise ConfirmationRequiredError("削除するには記憶IDを正確に入力してください")
            return _call(service, ("delete_memory", "memory_delete"), ident, ident)
    raise NotFoundError("APIエンドポイントが見つかりません")


def create_server(service: Any, host: str = "127.0.0.1", port: int = 8765, *, praxis: Any = None) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("RouteCraft UI は 127.0.0.1 にのみ bind できます")
    if praxis is None:
        candidate = Path(getattr(service, "data_dir", "")) / "praxis-memory.sqlite3"
        if candidate.is_file():
            try:
                from praxis_dashboard.query import PraxisDashboardQuery
                from praxis_dashboard.server import SQLiteEventSource
                praxis = PraxisDashboardQuery(SQLiteEventSource(candidate))
            except Exception:
                praxis = None
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        server_version = "RouteCraftLocal/1.0"
        def log_message(self, _format: str, *_args: Any) -> None: pass
        def _headers(self, api: bool = False) -> None:
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if api: self.send_header("Cache-Control", "no-store")
        def _json(self, status: int, payload: Any) -> None:
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status); self._headers(True); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def _fail(self, status: int, code: str, message: str) -> None: self._json(status, {"ok":False,"error":{"code":code,"message":message}})
        def _host_ok(self) -> bool:
            host_value = self.headers.get("Host", "").split(":", 1)[0]
            return host_value == "127.0.0.1"
        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return not origin or secrets.compare_digest(origin.rstrip("/"), self.server.url)
        def _read(self) -> dict[str, Any]:
            if self.headers.get("Content-Type", "").split(";",1)[0].lower() != "application/json": raise ValueError("Content-Type は application/json が必要です")
            try: length = int(self.headers.get("Content-Length", "0"))
            except ValueError: raise ValueError("不正な Content-Length")
            if length < 0 or length > MAX_BODY: raise ValueError("リクエストが大きすぎます")
            value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if not isinstance(value, dict): raise ValueError("JSON object が必要です")
            return value
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/bootstrap":
                if not self._host_ok(): return self._fail(HTTPStatus.FORBIDDEN, "host", "許可されていないHostです")
                return self._json(200, {"ok":True,"data":{"csrf_token":token,"url":self.server.url}})
            if parsed.path in STATIC:
                if not self._host_ok(): return self._fail(HTTPStatus.FORBIDDEN, "host", "許可されていないHostです")
                filename, content_type = STATIC[parsed.path]; raw = (WEB_ROOT / filename).read_bytes()
                self.send_response(200); self._headers(False); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            if not parsed.path.startswith("/api/"): return self._fail(404,"not_found","ページが見つかりません")
            if not self._host_ok(): return self._fail(403,"host","許可されていないHostです")
            try: self._json(200,{"ok":True,"data":_api(service,"GET",parsed.path,parse_qs(parsed.query),{},praxis)})
            except NotFoundError as exc: self._fail(404,"not_found",str(exc))
            except RouteCraftLocalError as exc: self._fail(400,"request",str(exc))
            except Exception: self._fail(500,"internal","処理に失敗しました")
        def do_POST(self) -> None: self._write("POST")
        def do_PATCH(self) -> None: self._write("PATCH")
        def do_DELETE(self) -> None: self._write("DELETE")
        def _write(self, method: str) -> None:
            if not self.path.startswith("/api/"): return self._fail(404,"not_found","ページが見つかりません")
            if not self._host_ok(): return self._fail(403,"host","許可されていないHostです")
            if not self._origin_ok(): return self._fail(403,"origin","許可されていないOriginです")
            if not secrets.compare_digest(self.headers.get("X-RouteCraft-CSRF", ""), token): return self._fail(403,"csrf","CSRF token が一致しません")
            try: self._json(200,{"ok":True,"data":_api(service,method,urlparse(self.path).path,parse_qs(urlparse(self.path).query),self._read(),praxis)})
            except ConfirmationRequiredError as exc: self._fail(409,"confirmation_required",str(exc))
            except NotFoundError as exc: self._fail(404,"not_found",str(exc))
            except (ValueError,RouteCraftLocalError) as exc: self._fail(400,"request",str(exc))
            except Exception: self._fail(500,"internal","処理に失敗しました")
    server = ThreadingHTTPServer((host, port), Handler)
    server.csrf_token = token; server.url = f"http://127.0.0.1:{server.server_address[1]}"; server.service = service
    return server


def run_ui(service: Any, port: int = 8765, open_browser: bool = True) -> int:
    server = create_server(service, port=port)
    print(f"RouteCraft Memory Local を起動しました: {server.url}", flush=True)
    if open_browser: threading.Timer(0.1, lambda: webbrowser.open(server.url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0
