"""SQLite persistence for RouteCraft Memory Local; intentionally separate from Markdown memory."""
from __future__ import annotations

import json, os, sqlite3, threading, time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from . import SCHEMA_VERSION
from .errors import IntegrityError

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_LOCK_DEPTH = threading.local()

def _process_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())

def _try_lock_file(handle) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        try: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1); return True
        except OSError: return False
    import fcntl
    try: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB); return True
    except BlockingIOError: return False

def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def json_value(value) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, separators=(",", ":"))

def json_load(value, default=None):
    try: return json.loads(value) if value else (default if default is not None else [])
    except (TypeError, ValueError): return default if default is not None else []

def decision_store_ancestor(data_dir: str | Path) -> Path | None:
    """Return the nearest Decision Store containing ``data_dir``, if any."""
    target = Path(data_dir).expanduser().resolve()
    for candidate in (target, *target.parents):
        if (candidate / ".routecraft-store.json").is_file():
            return candidate
    return None

class LocalDatabase:
    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir or Path.home() / ".routecraft-memory-local").expanduser().resolve()
        if decision_store_ancestor(self.data_dir) is not None:
            raise IntegrityError("Memory Local data directory must not reuse a RouteCraft Decision Store")
        self.path = self.data_dir / "routecraft-local.sqlite3"
        self.lock_path = self.data_dir / ".routecraft-local.lock"

    @contextmanager
    def operation_lock(self, timeout: float = 8.0):
        """Serialize internal readers/writers and multi-step database replacement."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key=str(self.lock_path); depths=getattr(_LOCK_DEPTH,"values",{})
        local_lock=_process_lock(self.lock_path)
        with local_lock:
            if depths.get(key,0):
                depths[key]+=1; _LOCK_DEPTH.values=depths
                try: yield
                finally: depths[key]-=1
                return
            with self.lock_path.open("a+b") as handle:
                if handle.seek(0,os.SEEK_END)==0: handle.write(b"0"); handle.flush()
                deadline=time.monotonic()+timeout
                while not _try_lock_file(handle):
                    if time.monotonic()>=deadline: raise sqlite3.OperationalError("database operation lock timeout")
                    time.sleep(0.05)
                depths[key]=1; _LOCK_DEPTH.values=depths
                try: yield
                finally:
                    depths.pop(key,None)
                    _unlock_file(handle)

    def initialize(self) -> dict:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Take any pre-migration backup from a read transaction: SQLite's
        # backup API can block while the source has an immediate write lock.
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION: raise IntegrityError(f"database schema {version} is newer than supported {SCHEMA_VERSION}")
            if version < SCHEMA_VERSION and self.path.exists() and self.path.stat().st_size:
                backup = self.data_dir / f"pre-migration-v{version}-{utc_now().replace(':','')}.sqlite3"
                destination = sqlite3.connect(backup)
                try:
                    db.backup(destination)
                    destination.commit()
                finally:
                    destination.close()
        # Schema checks issue DDL (including additive indexes/tables), so take
        # the writer lock up front when independent hook processes initialize
        # the same existing database concurrently.
        with self.connect(immediate=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION: raise IntegrityError(f"database schema {version} is newer than supported {SCHEMA_VERSION}")
            self._create(db)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return {"data_dir": str(self.data_dir), "database": str(self.path), "schema_version": SCHEMA_VERSION}

    @contextmanager
    def connect(self, *, immediate: bool = False):
        with self.operation_lock():
            db = sqlite3.connect(self.path, timeout=8, isolation_level="DEFERRED")
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON"); db.execute("PRAGMA busy_timeout=8000")
            try:
                # Most callers can use a deferred transaction.  A small number of
                # compare-and-create paths need an immediate writer lock so that a
                # second process cannot observe the same missing durable key and
                # create a duplicate record.
                db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield db
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()
            finally:
                db.close()

    def _create(self, db):
        statements = (
            "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY,name TEXT NOT NULL COLLATE NOCASE,repo_path TEXT NOT NULL DEFAULT '',git_remote_url TEXT NOT NULL DEFAULT '',ai_agents TEXT NOT NULL DEFAULT '[]',languages TEXT NOT NULL DEFAULT '[]',tags TEXT NOT NULL DEFAULT '[]',description TEXT NOT NULL DEFAULT '',current_objective TEXT NOT NULL DEFAULT '',archived INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE UNIQUE INDEX IF NOT EXISTS projects_active_name ON projects(name) WHERE archived=0",
            "CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,memory_type TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,importance TEXT NOT NULL,tags TEXT NOT NULL DEFAULT '[]',source TEXT NOT NULL DEFAULT 'cli',related_files TEXT NOT NULL DEFAULT '[]',related_commits TEXT NOT NULL DEFAULT '[]',active INTEGER NOT NULL DEFAULT 1,verified INTEGER NOT NULL DEFAULT 0,source_ref TEXT NOT NULL DEFAULT '',content_hash TEXT NOT NULL,legacy_metadata TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS memories_project_date ON memories(project_id,created_at DESC)",
            "CREATE INDEX IF NOT EXISTS memories_hash ON memories(project_id,content_hash)",
            "CREATE TABLE IF NOT EXISTS loop_session_summaries (project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,source_ref TEXT NOT NULL,memory_id TEXT NOT NULL UNIQUE REFERENCES memories(id) ON DELETE CASCADE,created_at TEXT NOT NULL,PRIMARY KEY(project_id,source_ref))",
            "CREATE TABLE IF NOT EXISTS import_conflicts (id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,incoming_id TEXT NOT NULL,existing_id TEXT,project_id TEXT,source_ref TEXT,existing_hash TEXT,incoming_hash TEXT,detail TEXT NOT NULL,created_at TEXT NOT NULL,resolved INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL)",
        )
        for statement in statements: db.execute(statement)
        existing_columns = {row[1] for row in db.execute("PRAGMA table_info(import_conflicts)")}
        for name in ("project_id", "source_ref", "existing_hash", "incoming_hash"):
            if name not in existing_columns:
                db.execute(f"ALTER TABLE import_conflicts ADD COLUMN {name} TEXT")
        try: db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(memory_id UNINDEXED,title,body,tags)")
        except sqlite3.OperationalError: pass

    def integrity(self) -> str:
        self.initialize()
        with self.connect() as db:
            result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok": raise IntegrityError(f"SQLite integrity check failed: {result}")
        return result
