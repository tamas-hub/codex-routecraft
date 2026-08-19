"""Core types, configuration, storage, validation, and safety for RouteCraft memory."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

SCHEMA_VERSION = 1
INDEX_SCHEMA_VERSION = 1
DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "origin"
DEFAULT_LIMIT = 5
DEFAULT_BUDGET = 12_000
MAX_RECORD_CHARS = 50_000
MAX_PACKET_BYTES = 1_000_000
MAX_LIST_ITEMS = 100
LOCK_STALE_SECONDS = 15 * 60

SCRIPT_PATH = Path(__file__).resolve()
PLUGIN_ROOT = SCRIPT_PATH.parents[2]
BUNDLED_STORE = PLUGIN_ROOT / "intelligence"

KIND_TO_DIR = {
    "candidate": "candidates",
    "case": "cases",
    "rule": "rules",
}
PREFIX_TO_KIND = {
    "CAND": "candidate",
    "CASE": "case",
    "RULE": "rule",
}
KIND_TO_PREFIX = {value: key for key, value in PREFIX_TO_KIND.items()}

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("OpenAI-style secret key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Bearer token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "credential assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9_./+\-=]{16,}"
        ),
    ),
)

CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]+")
ASCII_TERM_RE = re.compile(r"[a-z0-9][a-z0-9_.+/#:-]{1,}")
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
VALID_ID_RE = re.compile(r"^(CAND|CASE|RULE)-\d{8}T\d{6}Z-[A-Z0-9]{4,12}-[A-F0-9]{4}$")
SAFE_DEVICE_RE = re.compile(r"[^a-zA-Z0-9]+")
SAFE_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REMOTE_HELPER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")
ALLOWED_SYNC_ROOT_FILES = {".routecraft-store.json", ".gitignore", "README.md"}
ALLOWED_SYNC_DIRECTORIES = {"cases", "candidates", "rules", "templates"}


class RouteCraftError(RuntimeError):
    """Expected user-facing error."""


class GitCommandError(RouteCraftError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str = "") -> None:
        command = "git " + " ".join(args)
        detail = stderr.strip()
        message = f"Git command failed ({returncode}): {command}"
        if detail:
            message += f"\n{detail}"
        super().__init__(message)
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr_text = stderr


@dataclass(frozen=True)
class Record:
    path: Path
    metadata: dict[str, Any]
    body: str

    @property
    def record_id(self) -> str:
        return str(self.metadata.get("id", ""))

    @property
    def kind(self) -> str:
        return str(self.metadata.get("kind", ""))

    @property
    def title(self) -> str:
        return str(self.metadata.get("title", ""))


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class StoreLock:
    """Portable best-effort lock based on atomic file creation."""

    def __init__(self, store: Path, purpose: str) -> None:
        self.store = store
        self.purpose = purpose
        self.path = store / ".routecraft" / "lock"
        self.acquired = False

    def __enter__(self) -> "StoreLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                payload = {
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "purpose": self.purpose,
                    "created_at": utc_now(),
                }
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > LOCK_STALE_SECONDS:
                    with contextlib.suppress(FileNotFoundError):
                        self.path.unlink()
                    continue
                owner = "unknown"
                with contextlib.suppress(Exception):
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    owner = f"{data.get('host', 'unknown')} pid={data.get('pid', 'unknown')} ({data.get('purpose', 'unknown')})"
                raise RouteCraftError(f"Memory store is locked by {owner}: {self.path}")
        raise RouteCraftError(f"Could not acquire memory-store lock: {self.path}")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.acquired:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            self.acquired = False


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_id_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def default_config_path() -> Path:
    override = os.environ.get("ROUTECRAFT_MEMORY_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".codex" / "routecraft" / "memory.json").resolve()


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or default_config_path()
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteCraftError(f"Could not read RouteCraft memory config {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RouteCraftError(f"RouteCraft memory config must be a JSON object: {config_path}")
    return data


def save_config(config: Mapping[str, Any], path: Path | None = None) -> Path:
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(config_path, dict(config))
    return config_path


def generate_device_id() -> str:
    host = SAFE_DEVICE_RE.sub("", platform.node() or socket.gethostname() or "device").lower()
    host = host[:8] or "device"
    digest_source = f"{platform.node()}|{platform.system()}|{platform.machine()}|{uuid_node_hint()}"
    digest = hashlib.sha256(digest_source.encode("utf-8", errors="replace")).hexdigest()[:6]
    return f"{host}-{digest}"


def uuid_node_hint() -> str:
    # Avoid importing uuid solely for formatting in constrained Python builds.
    try:
        import uuid

        return str(uuid.getnode())
    except Exception:
        return "unknown"


def resolve_device_id(config: Mapping[str, Any] | None = None) -> str:
    env = os.environ.get("ROUTECRAFT_DEVICE_ID")
    if env:
        value = SAFE_DEVICE_RE.sub("", env).lower()[:12]
        if value:
            return value
    config = config or load_config()
    configured = str(config.get("device_id", "")).strip()
    if configured:
        value = SAFE_DEVICE_RE.sub("", configured).lower()[:12]
        if value:
            return value
    return generate_device_id()


def resolve_store(cli_store: str | None = None, config: Mapping[str, Any] | None = None) -> Path:
    if cli_store:
        return Path(cli_store).expanduser().resolve()
    env_store = os.environ.get("ROUTECRAFT_MEMORY_DIR")
    if env_store:
        return Path(env_store).expanduser().resolve()
    config = config or load_config()
    configured = config.get("store")
    if configured:
        return Path(str(configured)).expanduser().resolve()
    return BUNDLED_STORE.resolve()


def ensure_external_write_store(store: Path) -> None:
    if store.resolve() == BUNDLED_STORE.resolve() and os.environ.get("ROUTECRAFT_ALLOW_BUNDLED_MEMORY_WRITE") != "1":
        raise RouteCraftError(
            "Refusing to write personal decision memory into the bundled plugin store. "
            "Create a dedicated private store with `routecraft-memory init --store <path> --configure` first."
        )


def validate_remote_name(remote: str) -> str:
    if not SAFE_REMOTE_NAME_RE.fullmatch(remote):
        raise RouteCraftError(f"Invalid Git remote name: {remote!r}")
    return remote


def validate_remote_location(value: str) -> str:
    if not value or value.startswith("-") or any(ord(char) < 32 for char in value):
        raise RouteCraftError("Git remote/clone location is invalid or option-like")
    if REMOTE_HELPER_RE.match(value):
        raise RouteCraftError("Git remote-helper syntax is not allowed for memory-store remotes")
    return value


def validate_branch_name(store: Path, branch: str) -> str:
    del store  # Kept in the signature for call-site clarity.
    if not branch or branch.startswith("-") or any(ord(char) < 32 for char in branch):
        raise RouteCraftError(f"Invalid Git branch name: {branch!r}")
    if shutil.which("git") is not None:
        process = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if process.returncode != 0:
            raise RouteCraftError(f"Invalid Git branch name: {branch!r}")
    return branch


def store_is_writable(store: Path) -> bool:
    routecraft_dir = store / ".routecraft"
    return os.access(store, os.W_OK) and routecraft_dir.is_dir() and os.access(routecraft_dir, os.W_OK)


def store_sentinel(store: Path) -> Path:
    return store / ".routecraft-store.json"


def ensure_store_layout(store: Path, *, create: bool = False, name: str | None = None) -> None:
    if create:
        store.mkdir(parents=True, exist_ok=True)
        for directory in KIND_TO_DIR.values():
            (store / directory).mkdir(parents=True, exist_ok=True)
        template_dir = store / "templates"
        template_dir.mkdir(parents=True, exist_ok=True)
        if store.resolve() != BUNDLED_STORE.resolve():
            bundled_templates = BUNDLED_STORE / "templates"
            for template_name in ("case.md", "candidate.md", "rule.md"):
                source = bundled_templates / template_name
                destination = template_dir / template_name
                if source.is_file() and not destination.exists():
                    atomic_write_text(destination, source.read_text(encoding="utf-8"))
        (store / ".routecraft").mkdir(parents=True, exist_ok=True)
        sentinel = store_sentinel(store)
        if not sentinel.exists():
            atomic_write_json(
                sentinel,
                {
                    "schema_version": SCHEMA_VERSION,
                    "name": name or store.name or "routecraft-memory",
                    "created_at": utc_now(),
                    "purpose": "RouteCraft persistent decision memory",
                },
            )
        gitignore = store / ".gitignore"
        if not gitignore.exists():
            atomic_write_text(gitignore, ".routecraft/\nINDEX.md\n*.lock\n.DS_Store\nThumbs.db\n")
        readme = store / "README.md"
        if not readme.exists():
            atomic_write_text(readme, external_store_readme())
    if not store.is_dir():
        raise RouteCraftError(f"Memory store does not exist: {store}")
    sentinel = store_sentinel(store)
    if sentinel.is_symlink():
        raise RouteCraftError(f"Memory-store sentinel must not be a symlink: {sentinel}")
    if not sentinel.is_file():
        raise RouteCraftError(
            f"Not a RouteCraft memory store (missing {sentinel.name}): {store}. "
            "Run the init command first."
        )
    try:
        data = json.loads(sentinel.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteCraftError(f"Invalid memory-store sentinel {sentinel}: {exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise RouteCraftError(
            f"Unsupported memory-store schema {data.get('schema_version')}; expected {SCHEMA_VERSION}: {store}"
        )
    for directory in KIND_TO_DIR.values():
        path = store / directory
        if path.is_symlink():
            raise RouteCraftError(f"Memory-store directory must not be a symlink: {path}")
        if not path.is_dir():
            if create:
                path.mkdir(parents=True, exist_ok=True)
            else:
                raise RouteCraftError(f"Memory store is missing directory: {path}")
    local_dir = store / ".routecraft"
    if local_dir.is_symlink():
        raise RouteCraftError(f"Memory-store local directory must not be a symlink: {local_dir}")
    local_dir.mkdir(parents=True, exist_ok=True)


def validate_store_file_surface(store: Path) -> None:
    allowed_root_files = ALLOWED_SYNC_ROOT_FILES | {"INDEX.md", ".DS_Store", "Thumbs.db"}
    allowed_root_dirs = ALLOWED_SYNC_DIRECTORIES | {".routecraft"}
    for child in store.iterdir():
        if child.name == ".git":
            if child.is_symlink():
                raise RouteCraftError(f"Git metadata path must not be a symlink: {child}")
            continue
        if child.is_symlink():
            raise RouteCraftError(f"Memory-store paths must not be symlinks: {child}")
        if child.is_file():
            if child.name not in allowed_root_files:
                raise RouteCraftError(f"Unexpected file in memory-store root: {child.name}")
            continue
        if child.is_dir():
            if child.name not in allowed_root_dirs:
                raise RouteCraftError(f"Unexpected directory in memory-store root: {child.name}")
            continue
        raise RouteCraftError(f"Unsupported filesystem entry in memory store: {child}")

    ignored_names = {".DS_Store", "Thumbs.db"}
    for directory in ALLOWED_SYNC_DIRECTORIES:
        base = store / directory
        if not base.exists():
            continue
        if base.is_symlink() or not base.is_dir():
            raise RouteCraftError(f"Memory-store directory must be a regular directory: {base}")
        for child in base.iterdir():
            if child.is_symlink():
                raise RouteCraftError(f"Memory-store payload must not be a symlink: {child}")
            if not child.is_file():
                raise RouteCraftError(f"Nested directories are not allowed in memory-store payloads: {child}")
            if child.name in ignored_names:
                continue
            if child.name != ".gitkeep" and child.suffix.lower() != ".md":
                raise RouteCraftError(f"Only Markdown files are allowed in memory-store payload directories: {child}")


def external_store_readme() -> str:
    return textwrap.dedent(
        """\
        # RouteCraft Private Decision Store

        This repository stores compact, reusable decision records for RouteCraft.

        Keep this repository private when records may reveal project structure, incidents, internal constraints, or links to private work. Do not store credentials, private keys, access tokens, personal data, full transcripts, or raw logs.

        Tracked content lives in `cases/`, `candidates/`, and `rules/`. Local generated indexes and lock files live under `.routecraft/` and are intentionally ignored to reduce cross-device merge conflicts.
        """
    )
