#!/usr/bin/env python3
"""Idempotent RouteCraft bootstrap for Windows, macOS, and future devices.

The public RouteCraft repository and the private memory repository are the
sources of truth. Absolute paths, generated Codex cache, and device metadata
remain local to each computer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

VERSION = "0.7.2"
FLEET_SCHEMA = 1
LAYOUT_VERSION = 1
SOURCE_REMOTE = "https://github.com/tamas-hub/codex-routecraft.git"
SOURCE_BRANCH = "main"
MEMORY_BRANCH = "main"
SOURCE_DIR = "~/codex-routecraft"
MEMORY_DIR = "~/routecraft-memory"
MARKETPLACE = "routecraft"
PLUGIN = "codex-routecraft@routecraft"

SCRIPT = Path(__file__).resolve()
PLUGIN_ROOT = SCRIPT.parents[1]
REPO_ROOT = SCRIPT.parents[3]
MEMORY_CLI = SCRIPT.with_name("routecraft_memory.py")
VERIFY = REPO_ROOT / "scripts" / "verify.py"
MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
AGENT_SOURCE = PLUGIN_ROOT / "agents"
AGENTS = (
    "routecraft_luna_low.toml",
    "routecraft_luna_medium.toml",
    "routecraft_luna_max.toml",
    "routecraft_terra_medium.toml",
    "routecraft_terra_high.toml",
    "routecraft_sol_reviewer.toml",
)
INSTALL_CONFIRMATION = "INSTALL"
ROLLBACK_CONFIRMATION = "ROLLBACK"
INSTALL_SCHEMA = 2
MIN_HOOK_PYTHON = (3, 11)
PLUGIN_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}")
GENERATED_CACHE_PARTS = {"__pycache__", ".DS_Store"}


class FleetError(RuntimeError):
    pass


def redacted_remote() -> str:
    return "<redacted remote>"


def redact_text(value: str) -> str:
    """Keep transport errors useful without echoing private Git endpoints."""
    value = re.sub(r"(?:https?|ssh)://[^\s'\"]+", redacted_remote(), value, flags=re.IGNORECASE)
    value = re.sub(r"git@[^\s:'\"]+:[^\s'\"]+", redacted_remote(), value, flags=re.IGNORECASE)
    return value


def validate_remote(value: str, *, field: str) -> str:
    """Accept only non-interactive HTTPS or Git-over-SSH remotes.

    Git remote helpers (for example ``ext::``) execute local programs.  They
    are deliberately outside the cross-device bootstrap contract.  Error
    messages must never reflect a submitted private remote.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise FleetError(f"{field} must be a non-empty supported Git remote ({redacted_remote()})")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise FleetError(f"{field} contains a control character ({redacted_remote()})")
    if "::" in value:
        raise FleetError(f"{field} uses unsupported Git remote-helper syntax ({redacted_remote()})")

    # SCP-like SSH is intentionally restricted to the non-interactive git
    # account form.  It is compatible with GitHub and GitHub Enterprise.
    scp = re.fullmatch(r"git@(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*):(?P<path>[^\s?#:]+)", value)
    if scp:
        if not scp.group("host") or not scp.group("path").strip("/"):
            raise FleetError(f"{field} is not a supported Git remote ({redacted_remote()})")
        return value

    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "ssh"} or not parsed.netloc:
        raise FleetError(f"{field} uses an unsafe or unsupported Git transport ({redacted_remote()})")
    if parsed.query or parsed.fragment or not parsed.path.strip("/"):
        raise FleetError(f"{field} must not include query, fragment, or an empty repository path ({redacted_remote()})")
    if parsed.password is not None:
        raise FleetError(f"{field} must not include credentials ({redacted_remote()})")
    if parsed.username is not None:
        if parsed.scheme != "ssh" or parsed.username != "git":
            raise FleetError(f"{field} must not include credentials ({redacted_remote()})")
    try:
        hostname = parsed.hostname
        parsed.port  # Validate malformed port syntax without retaining it.
    except ValueError as exc:
        raise FleetError(f"{field} is not a supported Git remote ({redacted_remote()})") from exc
    if not hostname:
        raise FleetError(f"{field} is not a supported Git remote ({redacted_remote()})")
    if any(char.isspace() for char in parsed.netloc + parsed.path):
        raise FleetError(f"{field} is not a supported Git remote ({redacted_remote()})")
    return value


def validate_branch(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", value):
        raise FleetError(f"{field} is not a safe Git branch name")
    if ".." in value or value.endswith(("/", ".", ".lock")):
        raise FleetError(f"{field} is not a safe Git branch name")
    return value


def validate_bootstrap_inputs(args: argparse.Namespace) -> None:
    args.source_remote = validate_remote(args.source_remote, field="--source-remote")
    args.memory_remote = validate_remote(args.memory_remote, field="--memory-remote")
    args.source_branch = validate_branch(args.source_branch, field="--source-branch")
    args.memory_branch = validate_branch(args.memory_branch, field="--memory-branch")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d%H%M%S%f")


def path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def codex_home() -> Path:
    return path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else (Path.home() / ".codex").resolve()


def run(args: Sequence[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode:
        detail = redact_text(proc.stderr.strip() or proc.stdout.strip())
        rendered_args = " ".join(redacted_remote() if "://" in arg or re.match(r"git@[^:]+:", arg) else arg for arg in args)
        message = f"Command failed ({proc.returncode}): {rendered_args}"
        raise FleetError(message + (f"\n{detail}" if detail else ""))
    return proc


def require(name: str) -> None:
    if shutil.which(name) is None:
        raise FleetError(f"Required command not found: {name}")


def require_hook_python() -> str:
    """Preflight the literal hook command without invoking a shell."""
    command = "python" if platform.system().lower() == "windows" else "python3"
    executable = shutil.which(command)
    if not executable:
        raise FleetError(f"RouteCraft hooks require a '{command}' command (Python 3.11 or later)")
    probe = run((executable, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"), check=False)
    if probe.returncode:
        raise FleetError(f"RouteCraft hooks require a working '{command}' command (Python 3.11 or later)")
    try:
        major, minor = (int(item) for item in probe.stdout.strip().split(".", 1))
    except ValueError as exc:
        raise FleetError(f"RouteCraft hooks require a working '{command}' command (Python 3.11 or later)") from exc
    if (major, minor) < MIN_HOOK_PYTHON:
        raise FleetError("RouteCraft hooks require Python 3.11 or later")
    return str(Path(executable).resolve())


def resolve_codex_executable() -> str:
    resolved = shutil.which("codex")
    if not resolved:
        raise FleetError("Required command not found: codex")
    candidate = Path(resolved).resolve()
    if os.name != "nt" or candidate.suffix.lower() == ".exe":
        return str(candidate)

    npm_package = candidate.parent / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
    patterns = (
        "codex-win32-*/vendor/*/bin/codex.exe",
        "codex-win32-*/vendor/*/codex/codex.exe",
    )
    for pattern in patterns:
        matches = sorted(npm_package.glob(pattern))
        if matches:
            return str(matches[-1].resolve())

    native = shutil.which("codex.exe")
    if native:
        return str(Path(native).resolve())
    raise FleetError("Codex resolves to a Windows script shim, but the native codex.exe was not found")


def load_json(target: Path) -> dict[str, Any]:
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FleetError(f"Could not read JSON {target}: {exc}") from exc
    if not isinstance(value, dict):
        raise FleetError(f"JSON root must be an object: {target}")
    return value


def write_json(target: Path, value: Mapping[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def normalize_remote(value: str) -> str:
    text = value.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    for pattern in (
        r"https?://github\.com/(?P<slug>[^/]+/[^/]+)$",
        r"ssh://git@github\.com/(?P<slug>[^/]+/[^/]+)$",
        r"git@github\.com:(?P<slug>[^/]+/[^/]+)$",
    ):
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if match:
            return "github:" + match.group("slug").lower()
    return text


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(("git", "-C", str(repo), *args), check=check)


def git_text(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def ensure_remote(repo: Path, expected: str) -> None:
    expected = validate_remote(expected, field="configured remote")
    current = git(repo, "remote", "get-url", "origin", check=False)
    if current.returncode:
        git(repo, "remote", "add", "origin", expected)
    elif normalize_remote(current.stdout) != normalize_remote(expected):
        raise FleetError("origin does not match the configured source of truth (remote values redacted)")


def update_source(source: Path, remote: str, branch: str) -> None:
    remote = validate_remote(remote, field="source remote")
    branch = validate_branch(branch, field="source branch")
    if not (source / ".git").is_dir():
        raise FleetError(f"RouteCraft source checkout was not found: {source}")
    root = Path(git_text(source, "rev-parse", "--show-toplevel")).resolve()
    if root != source:
        raise FleetError(f"RouteCraft source must be a dedicated Git root: {source}")
    dirty = git_text(source, "status", "--porcelain")
    if dirty:
        raise FleetError("RouteCraft source has local changes; GitHub is the source of truth.\n" + dirty)
    ensure_remote(source, remote)
    git(source, "fetch", "origin", branch)
    git(source, "checkout", branch)
    git(source, "pull", "--ff-only", "origin", branch)


def memory(*args: str, json_result: bool = False) -> dict[str, Any] | subprocess.CompletedProcess[str]:
    proc = run((sys.executable, str(MEMORY_CLI), *args))
    if not json_result:
        return proc
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FleetError(f"RouteCraft memory returned invalid JSON: {exc}\n{proc.stdout}") from exc
    if not isinstance(value, dict):
        raise FleetError("RouteCraft memory result must be a JSON object")
    return value


def remote_has_branch(remote: str, branch: str) -> bool:
    remote = validate_remote(remote, field="memory remote")
    branch = validate_branch(branch, field="memory branch")
    proc = run(("git", "ls-remote", "--heads", remote, branch), check=False)
    if proc.returncode:
        detail = redact_text(proc.stderr.strip() or proc.stdout.strip())
        raise FleetError(
            "Could not access the private memory repository. Authenticate Git and retry."
            + (f"\n{detail}" if detail else "")
        )
    return bool(proc.stdout.strip())


def ensure_store(store: Path, remote: str, branch: str, allow_first: bool) -> str:
    remote = validate_remote(remote, field="memory remote")
    branch = validate_branch(branch, field="memory branch")
    sentinel = store / ".routecraft-store.json"
    if sentinel.is_file():
        root = Path(git_text(store, "rev-parse", "--show-toplevel")).resolve()
        if root != store:
            raise FleetError(f"Decision Store must be a dedicated Git root: {store}")
        ensure_remote(store, remote)
        memory(
            "configure",
            "--store",
            str(store),
            "--auto-sync",
            "both",
            "--remote",
            "origin",
            "--branch",
            branch,
        )
        return "existing-local"

    if store.exists() and any(store.iterdir()):
        raise FleetError(f"Memory path is non-empty but is not a RouteCraft store: {store}")
    if store.exists():
        store.rmdir()

    if remote_has_branch(remote, branch):
        memory(
            "init",
            "--store",
            str(store),
            "--clone",
            remote,
            "--branch",
            branch,
            "--configure",
            "--auto-sync",
            "both",
        )
        return "cloned"
    if not allow_first:
        raise FleetError("The private memory repository is empty. Initialize it on the first device first.")
    memory(
        "init",
        "--store",
        str(store),
        "--git-init",
        "--remote",
        remote,
        "--branch",
        branch,
        "--configure",
        "--auto-sync",
        "both",
    )
    return "initialized-first"


def fleet_payload(source_remote: str, source_branch: str, memory_remote: str, memory_branch: str) -> dict[str, Any]:
    validate_remote(source_remote, field="source repository")
    validate_remote(memory_remote, field="memory repository")
    validate_branch(source_branch, field="source branch")
    validate_branch(memory_branch, field="memory branch")
    return {
        "schema_version": FLEET_SCHEMA,
        "layout_version": LAYOUT_VERSION,
        "source": {"repository": source_remote, "branch": source_branch, "local_path": SOURCE_DIR},
        "memory": {
            "repository": memory_remote,
            "branch": memory_branch,
            "local_path": MEMORY_DIR,
            "auto_sync": "both",
        },
        "codex": {"marketplace": MARKETPLACE, "plugin": PLUGIN},
        "policy": {
            "source_of_truth": "github",
            "local_checkouts_are_working_copies": True,
            "shared_config_must_not_contain_secrets": True,
            "device_specific_absolute_paths_are_local_only": True,
        },
    }


def ensure_fleet_config(store: Path, expected: Mapping[str, Any]) -> str:
    sentinel_path = store / ".routecraft-store.json"
    sentinel = load_json(sentinel_path)
    actual = sentinel.get("fleet")
    if actual is None:
        sentinel["fleet"] = dict(expected)
        write_json(sentinel_path, sentinel)
        return "created"
    if not isinstance(actual, dict):
        raise FleetError(f"fleet in {sentinel_path} must be an object")
    if set(actual) != set(expected):
        raise FleetError("Shared fleet config has unexpected or missing top-level keys")
    for name in ("source", "memory", "codex", "policy"):
        if not isinstance(actual.get(name), dict) or set(actual[name]) != set(expected[name]):
            raise FleetError(f"Shared fleet config section mismatch: {name}")
    if actual.get("schema_version") != FLEET_SCHEMA or actual.get("layout_version") != LAYOUT_VERSION:
        raise FleetError("Unsupported shared fleet config version")

    pairs = (
        (actual["source"]["repository"], expected["source"]["repository"], True),
        (actual["memory"]["repository"], expected["memory"]["repository"], True),
        (actual["source"]["branch"], expected["source"]["branch"], False),
        (actual["memory"]["branch"], expected["memory"]["branch"], False),
        (actual["source"]["local_path"], SOURCE_DIR, False),
        (actual["memory"]["local_path"], MEMORY_DIR, False),
        (actual["memory"]["auto_sync"], "both", False),
        (actual["codex"]["marketplace"], MARKETPLACE, False),
        (actual["codex"]["plugin"], PLUGIN, False),
    )
    for current, wanted, is_remote in pairs:
        equal = normalize_remote(str(current)) == normalize_remote(str(wanted)) if is_remote else current == wanted
        if not equal:
            if is_remote:
                raise FleetError("Shared fleet config remote mismatch (remote values redacted)")
            raise FleetError(f"Shared fleet config mismatch: {current!r} != {wanted!r}")
    if actual["policy"] != expected["policy"]:
        raise FleetError("Shared fleet policy mismatch")
    return "verified"


def plugin_version() -> str:
    value = str(load_json(MANIFEST).get("version", "")).strip()
    if not value:
        raise FleetError(f"Plugin manifest has no version: {MANIFEST}")
    return value


def sha256_file(target: Path) -> str:
    return hashlib.sha256(target.read_bytes()).hexdigest()


def path_key(value: str | Path) -> str:
    text = str(value)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    resolved = str(Path(text).expanduser().resolve())
    return resolved.casefold() if os.name == "nt" else resolved


def json_command(codex: str, *args: str) -> dict[str, Any]:
    proc = run((codex, *args, "--json"), check=False)
    if proc.returncode:
        raise FleetError(f"Codex inspection failed: {' '.join(args)}")
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FleetError(f"Codex inspection returned invalid JSON: {' '.join(args)}") from exc
    if not isinstance(value, dict):
        raise FleetError(f"Codex inspection returned an invalid JSON root: {' '.join(args)}")
    return value


def expected_source_control(github_owner: str | None, enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None
    owner = (github_owner or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner):
        raise FleetError("--github-owner is required and must be a valid GitHub owner when Source Guard is enabled")
    return {
        "schema_version": 1,
        "enabled": True,
        "provider": "github",
        "github_owner": owner,
        "default_visibility": "private",
        "auto_commit": True,
        "auto_push": True,
        "allow_force_push": False,
        "store_raw_transcripts": False,
        "store_device_config": False,
    }


def local_config_payload(
    source: Path,
    store: Path,
    source_remote: str,
    source_branch: str,
    memory_remote: str,
    memory_branch: str,
    version: str,
    status: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "layout_version": LAYOUT_VERSION,
        "device_id": status.get("device_id"),
        "platform": platform.system(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "source_dir": str(source),
        "source_remote": source_remote,
        "source_branch": source_branch,
        "memory_dir": str(store),
        "memory_remote": memory_remote,
        "memory_branch": memory_branch,
        "auto_sync": "both",
        "plugin_version": version,
        "last_bootstrap_at": now(),
        "source_of_truth": "github",
    }


def local_config_matches(target: Path, expected: Mapping[str, Any]) -> bool:
    if not target.is_file():
        return False
    try:
        actual = load_json(target)
    except FleetError:
        return False
    # The timestamp is deliberately not rewritten on a same-version no-op.
    return all(actual.get(key) == value for key, value in expected.items() if key != "last_bootstrap_at")


def _plugin_entry(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    installed = payload.get("installed")
    if not isinstance(installed, list):
        return None
    for entry in installed:
        if isinstance(entry, dict) and entry.get("pluginId") == PLUGIN:
            return entry
    return None


def _marketplace_entry(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    entries = payload.get("marketplaces")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == MARKETPLACE:
            return entry
    return None


def inspect_install_state(
    source: Path,
    version: str,
    local_expected: Mapping[str, Any],
    source_control_expected: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Read the Codex registries without changing an installation."""
    codex = resolve_codex_executable()
    plugin_payload = json_command(codex, "plugin", "list")
    marketplace_payload = json_command(codex, "plugin", "marketplace", "list")
    plugin = _plugin_entry(plugin_payload)
    marketplace = _marketplace_entry(marketplace_payload)
    if not PLUGIN_VERSION_PATTERN.fullmatch(version):
        raise FleetError("RouteCraft plugin version is not safe for a cache path")
    expected_cache = codex_home() / "plugins" / "cache" / MARKETPLACE / "codex-routecraft" / version
    expected_plugin_source = source / "plugins" / "codex-routecraft"
    for name in AGENTS:
        _assert_owned_tree_path(codex_home() / "agents" / name, "RouteCraft agent destination")
    _assert_owned_tree_path(expected_cache, "RouteCraft plugin cache version")
    _assert_owned_tree_path(codex_home() / "routecraft" / "device.json", "RouteCraft device config")
    _assert_owned_tree_path(codex_home() / "routecraft" / "source-control.json", "RouteCraft source-control config")
    plugin_source = plugin.get("source", {}).get("path") if isinstance(plugin, dict) and isinstance(plugin.get("source"), dict) else None
    marketplace_root = marketplace.get("root") if isinstance(marketplace, dict) else None
    agents_match = all(
        (codex_home() / "agents" / name).is_file()
        and sha256_file(codex_home() / "agents" / name) == sha256_file(AGENT_SOURCE / name)
        for name in AGENTS
        if (AGENT_SOURCE / name).is_file()
    ) and all((AGENT_SOURCE / name).is_file() for name in AGENTS)
    local_target = codex_home() / "routecraft" / "device.json"
    source_control_target = codex_home() / "routecraft" / "source-control.json"
    # Disabled means "do not manage or remove an existing opt-in Source Guard".
    source_control_match = (
        True
        if source_control_expected is None
        else source_control_target.is_file() and load_json(source_control_target) == dict(source_control_expected)
    )
    marketplace_restore: dict[str, Any] = {"present": marketplace is not None}
    if isinstance(marketplace, dict):
        for key in ("root", "marketplaceSource"):
            if key in marketplace:
                marketplace_restore[key] = marketplace[key]
    plugin_restore: dict[str, Any] = {"present": plugin is not None}
    if isinstance(plugin, dict):
        for key in ("version", "source", "marketplaceSource"):
            if key in plugin:
                plugin_restore[key] = plugin[key]
    cache_match = False
    if expected_cache.is_dir():
        cache_match = _safe_tree_digest(
            expected_cache,
            "RouteCraft plugin cache version",
            ignore_generated=True,
        ) == _safe_tree_digest(
            expected_plugin_source,
            "RouteCraft plugin source",
            ignore_generated=True,
        )
    return {
        "codex": codex,
        "plugin_present": plugin is not None,
        "marketplace_present": marketplace is not None,
        "plugin_version_match": bool(isinstance(plugin, dict) and plugin.get("version") == version),
        "plugin_source_match": bool(plugin_source and path_key(str(plugin_source)) == path_key(expected_plugin_source)),
        "marketplace_source_match": bool(marketplace_root and path_key(str(marketplace_root)) == path_key(source)),
        "cache_match": cache_match,
        "agents_match": agents_match,
        "local_config_match": local_config_matches(local_target, local_expected),
        "source_control_match": source_control_match,
        # This is intentionally never printed or copied into manifest.json.
        # It is held only in the local 0600 rollback record.
        "_restore_registry": {"marketplace": marketplace_restore, "plugin": plugin_restore},
    }


def installation_root() -> Path:
    target = codex_home() / "routecraft" / "installations"
    _assert_owned_tree_path(target, "RouteCraft installation store")
    return target


def validate_private_registry_record(value: Mapping[str, Any]) -> None:
    """Reject credentials/remote helpers even when they originated in Codex state."""
    marketplace = value.get("marketplace")
    plugin = value.get("plugin")
    if not isinstance(marketplace, dict) or not isinstance(plugin, dict):
        raise FleetError("Invalid private RouteCraft registry restore record")
    if bool(plugin.get("present")) and not bool(marketplace.get("present")):
        raise FleetError("RouteCraft rollback cannot restore a plugin without its marketplace")
    if bool(plugin.get("present")):
        version = plugin.get("version")
        if not isinstance(version, str) or not PLUGIN_VERSION_PATTERN.fullmatch(version):
            raise FleetError("Private RouteCraft registry restore record has no safe plugin version")
    source = marketplace.get("marketplaceSource")
    candidate = marketplace.get("root")
    if isinstance(source, dict) and isinstance(source.get("source"), str):
        candidate = source["source"]
    if bool(marketplace.get("present")):
        if not isinstance(candidate, str) or not candidate.strip():
            raise FleetError("Private RouteCraft registry restore record has no marketplace source")
        if "://" in candidate or candidate.startswith("git@"):
            validate_remote(candidate, field="existing RouteCraft marketplace")
        else:
            local_source = Path(candidate).expanduser()
            if not local_source.is_absolute() or is_reparse_or_symlink(local_source):
                raise FleetError("Existing RouteCraft marketplace local source is unsafe")


def transaction_id() -> str:
    return "install-" + uuid.uuid4().hex


def _copy_file_backup(source: Path, backup: Path) -> dict[str, Any]:
    if is_reparse_or_symlink(source):
        raise FleetError("RouteCraft installation target is a symlink or junction")
    if not source.exists():
        return {"present": False}
    if not source.is_file():
        raise FleetError("RouteCraft installation target is not a regular file")
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    digest = sha256_file(source)
    if sha256_file(backup) != digest:
        raise FleetError("RouteCraft installation file backup verification failed")
    return {"present": True, "sha256": digest}


def is_reparse_or_symlink(target: Path) -> bool:
    if target.is_symlink():
        return True
    is_junction = getattr(target, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError:
            return True
    try:
        attributes = target.lstat().st_file_attributes
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse)
    except (AttributeError, OSError):
        return False


def _assert_owned_tree_path(target: Path, label: str) -> None:
    """Reject RouteCraft-owned paths that escape CODEX_HOME via reparse points."""
    home = codex_home()
    try:
        relative = target.relative_to(home)
    except ValueError as exc:
        raise FleetError(f"{label} is outside CODEX_HOME") from exc
    current = home
    for part in relative.parts:
        current = current / part
        if is_reparse_or_symlink(current):
            raise FleetError(f"{label} contains a symlink or junction")
    try:
        target.resolve().relative_to(home)
    except ValueError as exc:
        raise FleetError(f"{label} resolves outside CODEX_HOME") from exc


def _safe_tree_digest(root: Path, label: str, *, ignore_generated: bool = False) -> str:
    """Hash only a regular, non-linked tree using stable relative names."""
    if not root.is_dir() or is_reparse_or_symlink(root):
        raise FleetError(f"{label} is not a safe directory")
    digest = hashlib.sha256()
    pending = [root]
    files: list[Path] = []
    while pending:
        current = pending.pop()
        try:
            children = list(current.iterdir())
        except OSError as exc:
            raise FleetError(f"{label} could not be inspected") from exc
        for child in children:
            if ignore_generated and (child.name in GENERATED_CACHE_PARTS or child.suffix.lower() in {".pyc", ".pyo"}):
                continue
            if is_reparse_or_symlink(child):
                raise FleetError(f"{label} contains a symlink or junction")
            if child.is_dir():
                pending.append(child)
            elif child.is_file():
                files.append(child)
            else:
                raise FleetError(f"{label} contains a non-regular entry")
    for item in sorted(files, key=lambda candidate: candidate.relative_to(root).as_posix()):
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _copy_cache_backup(cache_root: Path, backup: Path) -> dict[str, Any]:
    _assert_owned_tree_path(cache_root, "RouteCraft plugin cache")
    if not cache_root.exists():
        return {"present": False}
    if not cache_root.is_dir():
        raise FleetError("RouteCraft plugin cache root is not a directory")
    if is_reparse_or_symlink(cache_root):
        raise FleetError("RouteCraft plugin cache root is a symlink or junction; refusing recursive backup")
    source_digest = _safe_tree_digest(cache_root, "RouteCraft plugin cache")
    shutil.copytree(cache_root, backup, symlinks=True)
    if _safe_tree_digest(backup, "RouteCraft plugin cache backup") != source_digest:
        raise FleetError("RouteCraft plugin cache backup verification failed")
    return {"present": True, "sha256": source_digest}


def _owned_file_targets() -> dict[str, Path]:
    targets = {
        **{f"agent:{name}": codex_home() / "agents" / name for name in AGENTS},
        "device_config": codex_home() / "routecraft" / "device.json",
        "source_control": codex_home() / "routecraft" / "source-control.json",
    }
    for label, target in targets.items():
        _assert_owned_tree_path(target, f"RouteCraft owned file {label}")
    return targets


def _file_state(target: Path) -> dict[str, Any]:
    if is_reparse_or_symlink(target):
        raise FleetError("RouteCraft owned file is a symlink or junction")
    if not target.exists():
        return {"present": False}
    if not target.is_file():
        raise FleetError("RouteCraft owned file is not a regular file")
    return {"present": True, "sha256": sha256_file(target)}


def _registry_state(source: Path, version: str) -> dict[str, Any]:
    codex = resolve_codex_executable()
    plugin = _plugin_entry(json_command(codex, "plugin", "list"))
    marketplace = _marketplace_entry(json_command(codex, "plugin", "marketplace", "list"))
    plugin_source = plugin.get("source", {}).get("path") if isinstance(plugin, dict) and isinstance(plugin.get("source"), dict) else None
    marketplace_root = marketplace.get("root") if isinstance(marketplace, dict) else None
    return {
        "plugin_present": plugin is not None,
        "marketplace_present": marketplace is not None,
        "plugin_version": plugin.get("version") if isinstance(plugin, dict) else None,
        "plugin_version_match": bool(isinstance(plugin, dict) and plugin.get("version") == version),
        "plugin_source_match": bool(
            plugin_source
            and path_key(str(plugin_source)) == path_key(source / "plugins" / "codex-routecraft")
        ),
        "marketplace_source_match": bool(marketplace_root and path_key(str(marketplace_root)) == path_key(source)),
    }


def _capture_post_state(source: Path, version: str) -> dict[str, Any]:
    cache_root = codex_home() / "plugins" / "cache" / MARKETPLACE / "codex-routecraft"
    _assert_owned_tree_path(cache_root, "RouteCraft plugin cache")
    if not cache_root.is_dir():
        raise FleetError("RouteCraft plugin cache is missing after installation")
    registry = _registry_state(source, version)
    if not all(registry.get(name) is True for name in (
        "plugin_present", "marketplace_present", "plugin_version_match",
        "plugin_source_match", "marketplace_source_match",
    )):
        raise FleetError("RouteCraft registry does not match the committed installation")
    return {
        "version": version,
        "registry": registry,
        "files": {label: _file_state(target) for label, target in _owned_file_targets().items()},
        "cache_sha256": _safe_tree_digest(cache_root, "RouteCraft committed plugin cache", ignore_generated=True),
    }


def _assert_post_state_unchanged(source: Path, version: str, expected: object) -> None:
    if not isinstance(expected, dict):
        raise FleetError("RouteCraft committed transaction has no post-state guard")
    current = _capture_post_state(source, version)
    if current != expected:
        raise FleetError(
            "RouteCraft installation changed after this transaction; stale rollback refused to preserve current state"
        )


def _create_registry_snapshot(
    root: Path,
    private_restore: Mapping[str, Any],
    cache_backup: Path,
    cache_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Create an immutable local marketplace for the exact previous plugin."""
    restore = json.loads(json.dumps(dict(private_restore), ensure_ascii=False))
    validate_private_registry_record(restore)
    plugin = restore["plugin"]
    if not bool(plugin.get("present")):
        restore["snapshot"] = {"present": False}
        return restore
    if not bool(cache_record.get("present")):
        raise FleetError("Previous RouteCraft plugin is registered but its cache is missing; exact rollback is impossible")
    version = str(plugin["version"])
    cached_plugin = cache_backup / version
    cached_digest = _safe_tree_digest(cached_plugin, "Previous RouteCraft plugin cache", ignore_generated=True)
    cached_manifest = load_json(cached_plugin / ".codex-plugin" / "plugin.json")
    if cached_manifest.get("version") != version:
        raise FleetError("Previous RouteCraft plugin cache version does not match its registry")

    marketplace_root = root / "registry-snapshot" / "marketplace"
    plugin_target = marketplace_root / "plugins" / "codex-routecraft"
    plugin_target.parent.mkdir(parents=True, exist_ok=False)
    shutil.copytree(cached_plugin, plugin_target, symlinks=True)
    if _safe_tree_digest(plugin_target, "Previous RouteCraft plugin snapshot", ignore_generated=True) != cached_digest:
        raise FleetError("Previous RouteCraft plugin snapshot verification failed")
    write_json(
        marketplace_root / ".agents" / "plugins" / "marketplace.json",
        {
            "name": MARKETPLACE,
            "interface": {"displayName": "RouteCraft rollback snapshot"},
            "plugins": [
                {
                    "name": "codex-routecraft",
                    "source": {"source": "local", "path": "./plugins/codex-routecraft"},
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL", "products": ["CODEX"]},
                    "category": "Productivity",
                }
            ],
        },
    )
    snapshot_digest = _safe_tree_digest(marketplace_root, "RouteCraft registry rollback snapshot")
    restore["snapshot"] = {
        "present": True,
        "marketplace_root": str(marketplace_root),
        "version": version,
        "sha256": snapshot_digest,
    }
    return restore


def create_install_transaction(
    source: Path,
    state: Mapping[str, Any],
    target_version: str | None = None,
) -> tuple[str, Path, dict[str, Any]]:
    target_version = target_version or plugin_version()
    if not PLUGIN_VERSION_PATTERN.fullmatch(target_version):
        raise FleetError("RouteCraft target plugin version is unsafe")
    identifier = transaction_id()
    root = installation_root() / identifier
    root.mkdir(parents=True, exist_ok=False)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    backups = root / "backups"
    cache_root = codex_home() / "plugins" / "cache" / MARKETPLACE / "codex-routecraft"
    file_targets = _owned_file_targets()
    backed_up = {
        label: _copy_file_backup(target, backups / "files" / hashlib.sha256(label.encode()).hexdigest())
        for label, target in file_targets.items()
    }
    cache_record = _copy_cache_backup(cache_root, backups / "cache")
    private_restore = state.get("_restore_registry")
    if not isinstance(private_restore, dict):
        private_restore = {"marketplace": {"present": False}, "plugin": {"present": False}}
    private_restore = _create_registry_snapshot(root, private_restore, backups / "cache", cache_record)
    private_restore_path = root / "registry-restore.private.json"
    write_json(private_restore_path, private_restore)
    try:
        private_restore_path.chmod(0o600)
    except OSError:
        pass
    manifest: dict[str, Any] = {
        "schema_version": INSTALL_SCHEMA,
        "transaction_id": identifier,
        "created_at": now(),
        "state": "PREPARED",
        "target_version": target_version,
        "source_fingerprint": hashlib.sha256(path_key(source).encode("utf-8")).hexdigest(),
        "cache": cache_record,
        "files": backed_up,
        # Registry state deliberately contains booleans/fingerprints only: no
        # remote URL, authentication material, absolute path, or raw CLI JSON.
        "registry": {
            "plugin_present": bool(state.get("plugin_present")),
            "marketplace_present": bool(state.get("marketplace_present")),
            "plugin_version_match": bool(state.get("plugin_version_match")),
            "plugin_source_match": bool(state.get("plugin_source_match")),
            "marketplace_source_match": bool(state.get("marketplace_source_match")),
            "restore_record_sha256": sha256_file(private_restore_path),
            "snapshot_sha256": private_restore.get("snapshot", {}).get("sha256"),
        },
    }
    write_json(root / "manifest.json", manifest)
    return identifier, root, manifest


def update_transaction(root: Path, manifest: Mapping[str, Any], state: str, **extra: Any) -> dict[str, Any]:
    updated = dict(manifest)
    updated["state"] = state
    updated.update(extra)
    write_json(root / "manifest.json", updated)
    return updated


def install_agents() -> list[str]:
    destination_dir = codex_home() / "agents"
    _assert_owned_tree_path(destination_dir, "RouteCraft agent directory")
    destination_dir.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for name in AGENTS:
        source = AGENT_SOURCE / name
        destination = destination_dir / name
        _assert_owned_tree_path(destination, "RouteCraft agent destination")
        if not source.is_file() or is_reparse_or_symlink(source):
            raise FleetError(f"Missing RouteCraft agent: {source}")
        if is_reparse_or_symlink(destination):
            raise FleetError(f"Agent destination is a symlink or junction: {destination}")
        digest = hashlib.sha256(source.read_bytes()).digest()
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).digest() == digest:
            continue
        if destination.exists():
            if not destination.is_file():
                raise FleetError(f"Agent destination is not a regular file: {destination}")
        _atomic_copy_file(source, destination)
        changed.append(name)
    return changed


def install_plugin(source: Path, version: str, *, codex: str | None = None) -> dict[str, Any]:
    codex = codex or resolve_codex_executable()
    run((codex, "plugin", "remove", PLUGIN), check=False)
    run((codex, "plugin", "marketplace", "remove", MARKETPLACE), check=False)
    cache_root = codex_home() / "plugins" / "cache" / MARKETPLACE / "codex-routecraft"
    run((codex, "plugin", "marketplace", "add", str(source)))
    run((codex, "plugin", "add", PLUGIN))
    changed = install_agents()
    expected = cache_root / version
    if not expected.is_dir():
        raise FleetError(f"Codex did not materialize RouteCraft {version}: {expected}")
    return {"cache": str(expected), "agents_changed": changed}


def local_config(
    source: Path,
    store: Path,
    source_remote: str,
    source_branch: str,
    memory_remote: str,
    memory_branch: str,
    version: str,
    status: Mapping[str, Any],
) -> Path:
    target = codex_home() / "routecraft" / "device.json"
    write_json(target, local_config_payload(
        source, store, source_remote, source_branch, memory_remote, memory_branch, version, status
    ))
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def source_control_config(github_owner: str | None, enabled: bool) -> Path | None:
    target = codex_home() / "routecraft" / "source-control.json"
    if not enabled:
        return target if target.is_file() else None
    expected = expected_source_control(github_owner, True)
    assert expected is not None
    write_json(target, expected)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def _restore_file(target: Path, backup: Path, present: bool, expected_sha256: object = None) -> None:
    _assert_owned_tree_path(target, "RouteCraft rollback file")
    if present:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise FleetError("RouteCraft rollback backup digest is missing")
        if not backup.is_file() or is_reparse_or_symlink(backup) or sha256_file(backup) != expected_sha256:
            raise FleetError("RouteCraft rollback backup is missing or altered")
    if is_reparse_or_symlink(target):
        raise FleetError("RouteCraft rollback refused a symlink or junction where an owned file belongs")
    if present:
        _atomic_copy_file(backup, target)
        if sha256_file(target) != expected_sha256:
            raise FleetError("RouteCraft rollback file verification failed")
    elif target.exists():
        if target.is_dir():
            raise FleetError("RouteCraft rollback refused a directory where an owned file belongs")
        target.unlink()


def _atomic_copy_file(source: Path, target: Path) -> None:
    _assert_owned_tree_path(target, "RouteCraft owned file")
    if not source.is_file() or is_reparse_or_symlink(source):
        raise FleetError("RouteCraft source file is missing or unsafe")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    os.close(fd)
    temporary = Path(temp_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _restore_cache(cache_root: Path, backup: Path, present: bool, expected_sha256: object = None) -> None:
    _assert_owned_tree_path(cache_root, "RouteCraft plugin cache")
    if present:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise FleetError("RouteCraft rollback cache digest is missing")
        backup_digest = _safe_tree_digest(backup, "RouteCraft plugin cache backup")
        if backup_digest != expected_sha256:
            raise FleetError("RouteCraft rollback cache backup is altered")
    if cache_root.exists():
        if not cache_root.is_dir():
            raise FleetError("RouteCraft rollback refused a non-directory plugin cache root")
        if is_reparse_or_symlink(cache_root):
            raise FleetError("RouteCraft rollback refused a symlink or junction plugin cache root")
        _safe_tree_digest(cache_root, "RouteCraft plugin cache")
    cache_root.parent.mkdir(parents=True, exist_ok=True)
    staged = cache_root.parent / f".codex-routecraft.restore.{uuid.uuid4().hex}"
    displaced = cache_root.parent / f".codex-routecraft.displaced.{uuid.uuid4().hex}"
    _assert_owned_tree_path(staged, "RouteCraft staged cache restore")
    _assert_owned_tree_path(displaced, "RouteCraft displaced cache")
    moved_current = False
    installed_staged = False
    try:
        if present:
            shutil.copytree(backup, staged, symlinks=True)
            if _safe_tree_digest(staged, "RouteCraft staged cache restore") != expected_sha256:
                raise FleetError("RouteCraft staged cache verification failed")
        if cache_root.exists():
            os.replace(cache_root, displaced)
            moved_current = True
        if present:
            os.replace(staged, cache_root)
            installed_staged = True
            if _safe_tree_digest(cache_root, "RouteCraft restored plugin cache") != expected_sha256:
                raise FleetError("RouteCraft rollback cache verification failed")
        if displaced.exists():
            shutil.rmtree(displaced)
    except Exception:
        if installed_staged and cache_root.exists():
            shutil.rmtree(cache_root)
        if moved_current and displaced.exists() and not cache_root.exists():
            os.replace(displaced, cache_root)
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def _restore_registry(codex: str, restore: Mapping[str, Any]) -> None:
    marketplace = restore.get("marketplace")
    plugin = restore.get("plugin")
    if not isinstance(marketplace, dict) or not isinstance(plugin, dict):
        raise FleetError("Invalid private RouteCraft registry restore record")
    run((codex, "plugin", "remove", PLUGIN), check=False)
    run((codex, "plugin", "marketplace", "remove", MARKETPLACE), check=False)
    validate_private_registry_record(restore)
    if marketplace.get("present"):
        snapshot = restore.get("snapshot")
        if plugin.get("present"):
            if not isinstance(snapshot, dict) or snapshot.get("present") is not True:
                raise FleetError("Exact RouteCraft registry rollback snapshot is missing")
            source_value = snapshot.get("marketplace_root")
            expected_digest = snapshot.get("sha256")
            if not isinstance(source_value, str) or not isinstance(expected_digest, str):
                raise FleetError("Exact RouteCraft registry rollback snapshot is invalid")
            snapshot_root = Path(source_value)
            if is_reparse_or_symlink(snapshot_root):
                raise FleetError("Exact RouteCraft registry rollback snapshot is unsafe")
            if _safe_tree_digest(snapshot_root, "RouteCraft registry rollback snapshot") != expected_digest:
                raise FleetError("Exact RouteCraft registry rollback snapshot is altered")
        else:
            marketplace_source = marketplace.get("marketplaceSource")
            source_value = marketplace.get("root")
            if isinstance(marketplace_source, dict) and isinstance(marketplace_source.get("source"), str):
                source_value = marketplace_source["source"]
        if not isinstance(source_value, str) or not source_value:
            raise FleetError("Private RouteCraft registry restore record has no marketplace source")
        run((codex, "plugin", "marketplace", "add", source_value))
    if plugin.get("present"):
        if not marketplace.get("present"):
            raise FleetError("RouteCraft rollback cannot restore a plugin without its marketplace")
        run((codex, "plugin", "add", PLUGIN))
    restored_plugin = _plugin_entry(json_command(codex, "plugin", "list"))
    restored_marketplace = _marketplace_entry(json_command(codex, "plugin", "marketplace", "list"))
    if bool(marketplace.get("present")) != (restored_marketplace is not None):
        raise FleetError("RouteCraft marketplace registry rollback verification failed")
    if bool(plugin.get("present")) != (restored_plugin is not None):
        raise FleetError("RouteCraft plugin registry rollback verification failed")
    if plugin.get("present") and isinstance(restored_plugin, dict):
        if restored_plugin.get("version") != plugin.get("version"):
            raise FleetError("RouteCraft plugin registry rollback restored the wrong version")


def _validate_rollback_artifacts(
    root: Path,
    files: Mapping[str, Any],
    cache: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> dict[str, Any]:
    private_restore_path = root / "registry-restore.private.json"
    if (
        not private_restore_path.is_file()
        or is_reparse_or_symlink(private_restore_path)
        or sha256_file(private_restore_path) != registry.get("restore_record_sha256")
    ):
        raise FleetError("RouteCraft private registry restore record is missing or altered")
    private_restore = load_json(private_restore_path)
    validate_private_registry_record(private_restore)
    snapshot = private_restore.get("snapshot")
    if isinstance(snapshot, dict) and snapshot.get("present") is True:
        snapshot_root = Path(str(snapshot.get("marketplace_root", "")))
        digest = snapshot.get("sha256")
        if (
            not isinstance(digest, str)
            or digest != registry.get("snapshot_sha256")
            or _safe_tree_digest(snapshot_root, "RouteCraft registry rollback snapshot") != digest
        ):
            raise FleetError("RouteCraft registry rollback snapshot is missing or altered")
    for label in _owned_file_targets():
        data = files.get(label)
        if not isinstance(data, dict):
            raise FleetError("Invalid RouteCraft installation rollback manifest")
        if bool(data.get("present")):
            expected = data.get("sha256")
            backup = root / "backups" / "files" / hashlib.sha256(label.encode()).hexdigest()
            if (
                not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or not backup.is_file()
                or is_reparse_or_symlink(backup)
                or sha256_file(backup) != expected
            ):
                raise FleetError("RouteCraft rollback file backup is missing or altered")
    if bool(cache.get("present")):
        expected_cache = cache.get("sha256")
        if (
            not isinstance(expected_cache, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_cache)
            or _safe_tree_digest(root / "backups" / "cache", "RouteCraft plugin cache backup") != expected_cache
        ):
            raise FleetError("RouteCraft rollback cache backup is missing or altered")
    return private_restore


def rollback_installation(identifier: str, source: Path, *, auto: bool = False) -> dict[str, Any]:
    if not re.fullmatch(r"install-[0-9a-f]{32}", identifier):
        raise FleetError("Invalid RouteCraft installation transaction id")
    root = installation_root() / identifier
    manifest: dict[str, Any] | None = None
    try:
        manifest = load_json(root / "manifest.json")
        if manifest.get("schema_version") != INSTALL_SCHEMA or manifest.get("transaction_id") != identifier:
            raise FleetError("Invalid RouteCraft installation rollback manifest")
        if manifest.get("source_fingerprint") != hashlib.sha256(path_key(source).encode("utf-8")).hexdigest():
            raise FleetError("Rollback source does not match the installation transaction")
        transaction_state = manifest.get("state")
        allowed_states = {"PREPARED", "APPLYING"} if auto else {"COMMITTED"}
        if transaction_state not in allowed_states:
            mode = "automatic" if auto else "manual"
            raise FleetError(f"RouteCraft {mode} rollback is not allowed from transaction state {transaction_state!r}")
        version = manifest.get("target_version")
        if not isinstance(version, str) or not PLUGIN_VERSION_PATTERN.fullmatch(version):
            raise FleetError("RouteCraft installation rollback manifest has no safe target version")
        if not auto:
            _assert_post_state_unchanged(source, version, manifest.get("post_state"))
        files = manifest.get("files")
        cache = manifest.get("cache")
        registry = manifest.get("registry")
        if not isinstance(files, dict) or not isinstance(cache, dict) or not isinstance(registry, dict):
            raise FleetError("Invalid RouteCraft installation rollback manifest")
        # Validate every backup and the current-state guard before the first
        # owned file, registry entry, or cache directory is changed.
        private_restore = _validate_rollback_artifacts(root, files, cache, registry)
        for name in AGENTS:
            data = files.get(f"agent:{name}")
            if not isinstance(data, dict):
                raise FleetError("Invalid RouteCraft installation rollback manifest")
            _restore_file(
                codex_home() / "agents" / name,
                root / "backups" / "files" / hashlib.sha256(f"agent:{name}".encode()).hexdigest(),
                bool(data.get("present")),
                data.get("sha256"),
            )
        for label, target in {
            "device_config": codex_home() / "routecraft" / "device.json",
            "source_control": codex_home() / "routecraft" / "source-control.json",
        }.items():
            data = files.get(label)
            if not isinstance(data, dict):
                raise FleetError("Invalid RouteCraft installation rollback manifest")
            _restore_file(target, root / "backups" / "files" / hashlib.sha256(label.encode()).hexdigest(), bool(data.get("present")), data.get("sha256"))
        _restore_registry(resolve_codex_executable(), private_restore)
        _restore_cache(
            codex_home() / "plugins" / "cache" / MARKETPLACE / "codex-routecraft",
            root / "backups" / "cache",
            bool(cache.get("present")),
            cache.get("sha256"),
        )
        updated = update_transaction(root, manifest, "AUTO_ROLLED_BACK" if auto else "ROLLED_BACK", rolled_back_at=now())
        return {"ok": True, "transaction_id": identifier, "state": updated["state"]}
    except Exception as exc:
        if manifest is not None:
            try:
                update_transaction(root, manifest, "ROLLBACK_FAILED", rollback_failed_at=now())
            except Exception:
                # The original failure remains the only safe user-facing fact.
                pass
        if isinstance(exc, FleetError):
            raise
        raise FleetError("RouteCraft rollback failed; inspect the local transaction manifest before retrying") from exc


def apply_plugin_transaction(
    source: Path,
    version: str,
    local_expected: Mapping[str, Any],
    source_control_expected: Mapping[str, Any] | None,
) -> dict[str, Any]:
    state = inspect_install_state(source, version, local_expected, source_control_expected)
    required = (
        "plugin_present", "marketplace_present", "plugin_version_match", "plugin_source_match",
        "marketplace_source_match", "cache_match", "agents_match", "local_config_match", "source_control_match",
    )
    if all(state.get(name) is True for name in required):
        return {"action": "no-op", "transaction_id": None, "agents_changed": []}
    identifier, root, manifest = create_install_transaction(source, state, version)
    try:
        manifest = update_transaction(root, manifest, "APPLYING", applying_at=now(), target_version=version)
        plugin = install_plugin(source, version, codex=str(state["codex"]))
        device_target = codex_home() / "routecraft" / "device.json"
        write_json(device_target, local_expected)
        try:
            device_target.chmod(0o600)
        except OSError:
            pass
        source_target = codex_home() / "routecraft" / "source-control.json"
        if source_control_expected is not None:
            write_json(source_target, source_control_expected)
            try:
                source_target.chmod(0o600)
            except OSError:
                pass
        verified = inspect_install_state(source, version, local_expected, source_control_expected)
        required = (
            "plugin_present", "marketplace_present", "plugin_version_match", "plugin_source_match",
            "marketplace_source_match", "cache_match", "agents_match", "local_config_match", "source_control_match",
        )
        if not all(verified.get(name) is True for name in required):
            raise FleetError("RouteCraft plugin transaction verification failed")
        post_state = _capture_post_state(source, version)
        manifest = update_transaction(
            root,
            manifest,
            "COMMITTED",
            committed_at=now(),
            target_version=version,
            post_state=post_state,
        )
        return {"action": "applied", "transaction_id": identifier, **plugin}
    except Exception as exc:
        try:
            rollback_installation(identifier, source, auto=True)
        except Exception as rollback_exc:
            raise FleetError("RouteCraft plugin transaction failed and automatic rollback failed") from rollback_exc
        if isinstance(exc, FleetError):
            raise
        raise FleetError("RouteCraft plugin transaction failed") from exc


def verify_state(
    source: Path,
    store: Path,
    source_branch: str,
    memory_branch: str,
    version: str,
) -> dict[str, Any]:
    status = memory("status", "--store", str(store), "--json", json_result=True)
    assert isinstance(status, dict)
    git_status = status.get("git")
    counts = status.get("counts")
    if not isinstance(git_status, dict) or not isinstance(counts, dict):
        raise FleetError("Decision Store status is incomplete")
    if git_status.get("dirty") or git_status.get("conflicts"):
        raise FleetError("Decision Store is dirty or conflicted after bootstrap")
    if git_status.get("branch") != memory_branch or not git_status.get("dedicated_root"):
        raise FleetError("Decision Store branch/root verification failed")
    if not isinstance(load_json(store / ".routecraft-store.json").get("fleet"), dict):
        raise FleetError("Shared fleet config is missing from .routecraft-store.json")

    cache = codex_home() / "plugins" / "cache" / MARKETPLACE / "codex-routecraft" / version
    if not cache.is_dir():
        raise FleetError(f"RouteCraft plugin cache is missing: {cache}")
    missing = [name for name in AGENTS if not (codex_home() / "agents" / name).is_file()]
    if missing:
        raise FleetError("Missing RouteCraft agents: " + ", ".join(missing))

    source_control_path = codex_home() / "routecraft" / "source-control.json"
    source_control = load_json(source_control_path) if source_control_path.is_file() else {}
    if source_control and source_control.get("enabled") is True:
        if source_control.get("provider") != "github":
            raise FleetError("Source Guard provider must be github")
        if source_control.get("default_visibility") != "private":
            raise FleetError("Source Guard default visibility must be private")
        if source_control.get("allow_force_push") is not False:
            raise FleetError("Source Guard must not allow force push")
        if source_control.get("store_raw_transcripts") is not False:
            raise FleetError("Source Guard must not store raw transcripts")

    return {
        "plugin_version": version,
        "source": {
            "path": str(source),
            "branch": source_branch,
            "head": git_text(source, "rev-parse", "HEAD"),
            "tracking": git(
                source,
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{u}",
                check=False,
            ).stdout.strip(),
            "clean": not bool(git_text(source, "status", "--porcelain")),
        },
        "memory": {
            "path": str(store),
            "branch": memory_branch,
            "head": git_text(store, "rev-parse", "HEAD"),
            "tracking": git(
                store,
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{u}",
                check=False,
            ).stdout.strip(),
            "counts": counts,
            "clean": not bool(git_text(store, "status", "--porcelain")),
        },
        "codex": {
            "home": str(codex_home()),
            "plugin_cache": str(cache),
            "agents": len(AGENTS),
            "source_guard": {
                "enabled": source_control.get("enabled") is True,
                "config": str(source_control_path) if source_control_path.is_file() else None,
            },
        },
    }


def bootstrap_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Construct a local, model-free plan.  It intentionally performs no Git or Codex mutation."""
    validate_bootstrap_inputs(args)
    source = path(args.source_dir)
    store = path(args.memory_dir)
    if source != REPO_ROOT.resolve():
        raise FleetError(f"Run from the configured RouteCraft checkout: {REPO_ROOT} != {source}")
    source_control = expected_source_control(args.github_owner, args.enable_project_source_guard)
    return {
        "ok": True,
        "mode": "plan",
        "version": plugin_version(),
        "confirmation_required": INSTALL_CONFIRMATION,
        "allow_first_device": bool(args.allow_first_device),
        "source": {"path": str(source), "branch": args.source_branch},
        "memory": {"path": str(store), "branch": args.memory_branch},
        "source_guard": {"enabled": source_control is not None},
        "mutations": [
            "update public RouteCraft checkout",
            "clone or validate private Decision Store",
            "install RouteCraft marketplace/plugin/cache and six agents transactionally",
            "write local RouteCraft device configuration",
            "only after plugin transaction succeeds: update and sync shared fleet sentinel",
        ],
    }


def minimal_install_config(source: Path, version: str) -> dict[str, Any]:
    """Local-only configuration for package installers before a memory remote exists."""
    target = codex_home() / "routecraft" / "device.json"
    preserved: dict[str, Any] = {}
    if target.is_file():
        current = load_json(target)
        # A package installer must not erase an already configured private
        # Decision Store merely because this particular command has no remote.
        for key in (
            "device_id", "source_remote", "source_branch", "memory_dir", "memory_remote",
            "memory_branch", "auto_sync",
        ):
            if key in current:
                preserved[key] = current[key]
    return {
        **preserved,
        "schema_version": 1,
        "layout_version": LAYOUT_VERSION,
        "platform": platform.system(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "source_dir": str(source),
        "plugin_version": version,
        "source_of_truth": "github",
    }


def validate_fixed_checkout(source: Path) -> None:
    if source != REPO_ROOT.resolve():
        raise FleetError(f"Run from the configured RouteCraft checkout: {REPO_ROOT} != {source}")
    if not (source / ".git").exists():
        raise FleetError("RouteCraft install requires a dedicated Git checkout")
    root = Path(git_text(source, "rev-parse", "--show-toplevel")).resolve()
    if root != source:
        raise FleetError("RouteCraft install requires the checkout root")


def validate_release_checkout(source: Path, expected_commit: str) -> None:
    """Fail closed unless an installer is pointed at its audited release commit."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_commit or ""):
        raise FleetError("--expected-commit must be a full 40-character Git commit id")
    validate_fixed_checkout(source)
    origin = git_text(source, "remote", "get-url", "origin")
    try:
        origin = validate_remote(origin, field="RouteCraft origin")
    except FleetError as exc:
        raise FleetError("RouteCraft checkout origin is not an approved official remote") from exc
    if normalize_remote(origin) != normalize_remote(SOURCE_REMOTE):
        raise FleetError("RouteCraft checkout origin is not the official RouteCraft repository")
    if git_text(source, "status", "--porcelain"):
        raise FleetError("RouteCraft release checkout has local changes")
    if git_text(source, "rev-parse", "HEAD").lower() != expected_commit.lower():
        raise FleetError("RouteCraft checkout HEAD does not match --expected-commit")
    # verify.py is deterministic and local.  Run it before marketplace/plugin
    # mutation so a package cannot install an unverified source tree.
    run((sys.executable, str(VERIFY)), cwd=source)


def install_plan(args: argparse.Namespace) -> dict[str, Any]:
    source = path(args.source_dir)
    validate_release_checkout(source, args.expected_commit)
    return {
        "ok": True,
        "mode": "install-plan",
        "version": plugin_version(),
        "expected_commit": args.expected_commit.lower(),
        "confirmation_required": INSTALL_CONFIRMATION,
        "source": {"path": str(source)},
        "mutations": [
            "install RouteCraft marketplace/plugin/cache and six agents transactionally",
            "write local plugin-only RouteCraft configuration",
        ],
        "does_not_require": ["Decision Store remote", "Memory content", "Control Center"],
    }


def install_apply(args: argparse.Namespace) -> dict[str, Any]:
    if args.confirm != INSTALL_CONFIRMATION:
        raise FleetError("install apply is a mutating operation; rerun with --confirm INSTALL after reviewing 'install plan'")
    require_hook_python()
    source = path(args.source_dir)
    validate_release_checkout(source, args.expected_commit)
    version = plugin_version()
    local_expected = minimal_install_config(source, version)
    plugin = apply_plugin_transaction(source, version, local_expected, None)
    verified = inspect_install_state(source, version, local_expected, None)
    if not all(verified.get(name) is True for name in (
        "plugin_present", "marketplace_present", "plugin_version_match", "plugin_source_match",
        "marketplace_source_match", "cache_match", "agents_match", "local_config_match", "source_control_match",
    )):
        raise FleetError("RouteCraft install verification failed")
    return {
        "ok": True,
        "mode": "install-apply",
        "plugin_version": version,
        "local_config_path": str(codex_home() / "routecraft" / "device.json"),
        "plugin": plugin,
    }


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    validate_bootstrap_inputs(args)
    if args.confirm != INSTALL_CONFIRMATION:
        raise FleetError("bootstrap is a mutating operation; rerun with --confirm INSTALL after reviewing 'plan'")
    require("git")
    hook_python = require_hook_python()
    source = path(args.source_dir)
    store = path(args.memory_dir)
    if source != REPO_ROOT.resolve():
        raise FleetError(f"Run from the configured RouteCraft checkout: {REPO_ROOT} != {source}")
    update_source(source, args.source_remote, args.source_branch)
    run((sys.executable, str(VERIFY)), cwd=source)
    version = plugin_version()
    store_action = ensure_store(store, args.memory_remote, args.memory_branch, args.allow_first_device)
    if store_action != "initialized-first":
        memory("sync", "--store", str(store), "--mode", "pull")
    status = memory("status", "--store", str(store), "--json", json_result=True)
    assert isinstance(status, dict)
    local_expected = local_config_payload(
        source, store, args.source_remote, args.source_branch, args.memory_remote, args.memory_branch, version, status
    )
    source_control_expected = expected_source_control(args.github_owner, args.enable_project_source_guard)
    plugin = apply_plugin_transaction(source, version, local_expected, source_control_expected)
    # The shared sentinel is intentionally touched only after the local Codex
    # installation transaction has committed.  A failed device install cannot
    # publish fleet metadata or push a half-configured machine.
    fleet_action = ensure_fleet_config(
        store,
        fleet_payload(args.source_remote, args.source_branch, args.memory_remote, args.memory_branch),
    )
    sync = memory("sync", "--store", str(store), "--mode", "both", json_result=True)
    memory("validate", "--store", str(store))
    result = verify_state(source, store, args.source_branch, args.memory_branch, version)
    return {
        "ok": True,
        "store_action": store_action,
        "fleet_config": fleet_action,
        "shared_config_path": str(store / ".routecraft-store.json"),
        "local_config_path": str(codex_home() / "routecraft" / "device.json"),
        "source_control_config_path": str(codex_home() / "routecraft" / "source-control.json") if source_control_expected else None,
        "sync": sync,
        "hook_python": hook_python,
        "plugin": plugin,
        **result,
    }


def status(args: argparse.Namespace) -> dict[str, Any]:
    args.source_branch = validate_branch(args.source_branch, field="--source-branch")
    args.memory_branch = validate_branch(args.memory_branch, field="--memory-branch")
    return verify_state(
        path(args.source_dir),
        path(args.memory_dir),
        args.source_branch,
        args.memory_branch,
        plugin_version(),
    )


def rollback(args: argparse.Namespace) -> dict[str, Any]:
    if args.confirm != ROLLBACK_CONFIRMATION:
        raise FleetError("rollback is a mutating operation; rerun with --confirm ROLLBACK and the transaction id")
    source = path(args.source_dir)
    validate_fixed_checkout(source)
    return rollback_installation(args.transaction_id, source)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="routecraft-device", description="RouteCraft multi-device bootstrap")
    root.add_argument("--version", action="version", version=f"routecraft-device {VERSION}")
    commands = root.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--source-dir", default=SOURCE_DIR)
        target.add_argument("--memory-dir", default=MEMORY_DIR)
        target.add_argument("--source-branch", default=SOURCE_BRANCH)
        target.add_argument("--memory-branch", default=MEMORY_BRANCH)
        target.add_argument("--json", action="store_true")

    setup = commands.add_parser("bootstrap", help="Install, configure, sync, and verify one device")
    common(setup)
    setup.add_argument("--source-remote", default=SOURCE_REMOTE)
    setup.add_argument("--memory-remote", required=True)
    setup.add_argument("--allow-first-device", action="store_true")
    setup.add_argument("--enable-project-source-guard", action="store_true")
    setup.add_argument("--github-owner")
    setup.add_argument("--confirm", metavar="TOKEN")
    setup.set_defaults(func=bootstrap)

    plan = commands.add_parser("plan", help="Show the bootstrap plan without changing local or remote state")
    common(plan)
    plan.add_argument("--source-remote", default=SOURCE_REMOTE)
    plan.add_argument("--memory-remote", required=True)
    plan.add_argument("--allow-first-device", action="store_true")
    plan.add_argument("--enable-project-source-guard", action="store_true")
    plan.add_argument("--github-owner")
    plan.set_defaults(func=bootstrap_plan)

    install = commands.add_parser("install", help="Install local RouteCraft runtime without a Decision Store remote")
    install_commands = install.add_subparsers(dest="install_command", required=True)
    install_plan_parser = install_commands.add_parser("plan", help="Show the local installer plan without mutation")
    install_plan_parser.add_argument("--source-dir", default=SOURCE_DIR)
    install_plan_parser.add_argument("--expected-commit", required=True)
    install_plan_parser.add_argument("--json", action="store_true")
    install_plan_parser.set_defaults(func=install_plan)
    install_apply_parser = install_commands.add_parser("apply", help="Apply the local installer transaction")
    install_apply_parser.add_argument("--source-dir", default=SOURCE_DIR)
    install_apply_parser.add_argument("--expected-commit", required=True)
    install_apply_parser.add_argument("--confirm", metavar="TOKEN")
    install_apply_parser.add_argument("--json", action="store_true")
    install_apply_parser.set_defaults(func=install_apply)

    check = commands.add_parser("status", help="Verify the standardized local layout")
    common(check)
    check.set_defaults(func=status)

    undo = commands.add_parser("rollback", help="Restore RouteCraft-owned files from one failed installation transaction")
    undo.add_argument("--source-dir", default=SOURCE_DIR)
    undo.add_argument("--transaction-id", required=True)
    undo.add_argument("--confirm", metavar="TOKEN")
    undo.add_argument("--json", action="store_true")
    undo.set_defaults(func=rollback)
    return root


def print_summary(result: Mapping[str, Any]) -> None:
    if result.get("mode") == "plan":
        print("RouteCraft device setup plan")
        print(f"- runtime: {result.get('version')}")
        print(f"- confirmation required: --confirm {result.get('confirmation_required')}")
        print("- no local or remote state was changed")
        return
    source = result.get("source", {})
    store = result.get("memory", {})
    counts = store.get("counts", {}) if isinstance(store, dict) else {}
    print("RouteCraft device setup OK")
    print(f"- plugin: {result.get('plugin_version')}")
    print(f"- source: {source.get('path')} ({source.get('tracking')}, clean={source.get('clean')})")
    print(f"- memory: {store.get('path')} ({store.get('tracking')}, clean={store.get('clean')})")
    print(
        f"- records: case={counts.get('case', 0)} "
        f"candidate={counts.get('candidate', 0)} rule={counts.get('rule', 0)}"
    )
    print(f"- shared config: {result.get('shared_config_path', 'verified in .routecraft-store.json')}")
    print(f"- local config: {result.get('local_config_path', codex_home() / 'routecraft' / 'device.json')}")
    print("Start a fresh local Codex task before using RouteCraft.")


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = args.func(args)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_summary(result)
        return 0
    except FleetError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": {"code": "ROUTECRAFT_DEVICE_ERROR", "message": redact_text(str(exc))}}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"routecraft-device: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("routecraft-device: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
