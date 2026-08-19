#!/usr/bin/env python3
"""Idempotent multi-device bootstrap and health checks for RouteCraft.

GitHub is the source of truth for the public RouteCraft source tree and the
private decision/config store. Device-specific absolute paths and generated
Codex installation files stay local.
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
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

FLEET_SCHEMA_VERSION = 1
LAYOUT_VERSION = 1
DEFAULT_SOURCE_REMOTE = "https://github.com/tamas-hub/codex-routecraft.git"
DEFAULT_SOURCE_BRANCH = "main"
DEFAULT_MEMORY_BRANCH = "main"
DEFAULT_SOURCE_DIR = "~/codex-routecraft"
DEFAULT_MEMORY_DIR = "~/routecraft-memory"
DEFAULT_MARKETPLACE = "routecraft"
DEFAULT_PLUGIN = "codex-routecraft@routecraft"
SHARED_CONFIG_NAME = "routecraft-fleet.json"
LOCAL_CONFIG_NAME = "device.json"

SCRIPT_PATH = Path(__file__).resolve()
PLUGIN_ROOT = SCRIPT_PATH.parents[1]
REPO_ROOT = SCRIPT_PATH.parents[3]
MEMORY_CLI = SCRIPT_PATH.with_name("routecraft_memory.py")
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify.py"
PLUGIN_MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
AGENT_SOURCE = PLUGIN_ROOT / "agents"
AGENT_NAMES = (
    "routecraft_luna_low.toml",
    "routecraft_luna_medium.toml",
    "routecraft_luna_max.toml",
    "routecraft_terra_medium.toml",
    "routecraft_terra_high.toml",
    "routecraft_sol_reviewer.toml",
)


class FleetError(RuntimeError):
    """Expected user-facing bootstrap error."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d%H%M%S%f")


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    process = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None,
        check=False,
    )
    result = CommandResult(process.returncode, process.stdout, process.stderr)
    if check and process.returncode != 0:
        command = " ".join(args)
        detail = process.stderr.strip() or process.stdout.strip()
        if detail:
            raise FleetError(f"Command failed ({process.returncode}): {command}\n{detail}")
        raise FleetError(f"Command failed ({process.returncode}): {command}")
    return result


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise FleetError(f"Required command not found: {name}")


def expand_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    return expand_path(value) if value else (Path.home() / ".codex").resolve()


def local_config_path() -> Path:
    return codex_home() / "routecraft" / LOCAL_CONFIG_NAME


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FleetError(f"Could not read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FleetError(f"JSON root must be an object: {path}")
    return value


def manifest_version() -> str:
    data = load_json(PLUGIN_MANIFEST)
    version = str(data.get("version", "")).strip()
    if not version:
        raise FleetError(f"Plugin manifest has no version: {PLUGIN_MANIFEST}")
    return version


def normalize_remote(value: str) -> str:
    """Normalize common GitHub HTTPS/SSH spellings for safe equality checks."""
    text = value.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    patterns = (
        r"https?://github\.com/(?P<slug>[^/]+/[^/]+)$",
        r"ssh://git@github\.com/(?P<slug>[^/]+/[^/]+)$",
        r"git@github\.com:(?P<slug>[^/]+/[^/]+)$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if match:
            return f"github:{match.group('slug').lower()}"
    return text


def git_output(repo: Path, *args: str) -> str:
    return run(("git", "-C", str(repo), *args)).stdout.strip()


def ensure_clean_source(source_dir: Path) -> None:
    if not (source_dir / ".git").exists():
        raise FleetError(f"RouteCraft source checkout was not found: {source_dir}")
    root = Path(git_output(source_dir, "rev-parse", "--show-toplevel")).resolve()
    if root != source_dir.resolve():
        raise FleetError(f"RouteCraft source must be a dedicated Git root: {source_dir} (root is {root})")
    dirty = git_output(source_dir, "status", "--porcelain")
    if dirty:
        raise FleetError(
            "RouteCraft source has local changes. GitHub is the source of truth, so bootstrap will not overwrite them.\n"
            + dirty
        )


def ensure_remote(repo: Path, name: str, expected: str, *, replace: bool = False) -> None:
    current = run(("git", "-C", str(repo), "remote", "get-url", name), check=False)
    if current.returncode != 0:
        run(("git", "-C", str(repo), "remote", "add", name, expected))
        return
    current_url = current.stdout.strip()
    if normalize_remote(current_url) == normalize_remote(expected):
        return
    if not replace:
        raise FleetError(
            f"Git remote {name!r} does not match the configured source of truth.\n"
            f"Current:  {current_url}\nExpected: {expected}"
        )
    run(("git", "-C", str(repo), "remote", "set-url", name, expected))


def update_source(source_dir: Path, source_remote: str, source_branch: str) -> None:
    ensure_clean_source(source_dir)
    ensure_remote(source_dir, "origin", source_remote)
    run(("git", "-C", str(source_dir), "fetch", "origin", source_branch))
    run(("git", "-C", str(source_dir), "checkout", source_branch))
    run(("git", "-C", str(source_dir), "pull", "--ff-only", "origin", source_branch))
    ensure_clean_source(source_dir)


def remote_branch_state(remote: str, branch: str) -> str:
    result = run(("git", "ls-remote", "--heads", remote, branch), check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise FleetError(
            "Could not access the private RouteCraft store. Authenticate Git for this device and retry."
            + (f"\n{detail}" if detail else "")
        )
    return "existing" if result.stdout.strip() else "empty"


def run_memory(*args: str, capture_json: bool = False) -> dict[str, Any] | CommandResult:
    result = run((sys.executable, str(MEMORY_CLI), *args))
    if not capture_json:
        return result
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FleetError(f"RouteCraft memory returned invalid JSON: {exc}\n{result.stdout}") from exc
    if not isinstance(value, dict):
        raise FleetError("RouteCraft memory JSON result must be an object")
    return value


def ensure_memory_store(
    memory_dir: Path,
    memory_remote: str,
    memory_branch: str,
    *,
    allow_first_device: bool,
) -> str:
    sentinel = memory_dir / ".routecraft-store.json"
    if sentinel.is_file():
        root = Path(git_output(memory_dir, "rev-parse", "--show-toplevel")).resolve()
        if root != memory_dir.resolve():
            raise FleetError(f"Decision Store must be a dedicated Git root: {memory_dir} (root is {root})")
        ensure_remote(memory_dir, "origin", memory_remote)
        run_memory(
            "configure",
            "--store",
            str(memory_dir),
            "--auto-sync",
            "both",
            "--remote",
            "origin",
            "--branch",
            memory_branch,
        )
        return "existing-local"

    if memory_dir.exists() and any(memory_dir.iterdir()):
        raise FleetError(f"Memory path is non-empty but is not a RouteCraft store: {memory_dir}")
    if memory_dir.exists():
        memory_dir.rmdir()

    state = remote_branch_state(memory_remote, memory_branch)
    if state == "existing":
        run_memory(
            "init",
            "--store",
            str(memory_dir),
            "--clone",
            memory_remote,
            "--branch",
            memory_branch,
            "--configure",
            "--auto-sync",
            "both",
        )
        return "cloned"

    if not allow_first_device:
        raise FleetError(
            "The private memory remote has no configured branch. Initialize it on the first device, "
            "or rerun with --allow-first-device only when this is intentionally the first device."
        )
    run_memory(
        "init",
        "--store",
        str(memory_dir),
        "--git-init",
        "--remote",
        memory_remote,
        "--branch",
        memory_branch,
        "--configure",
        "--auto-sync",
        "both",
    )
    return "initialized-first"


def shared_config_payload(
    *,
    source_remote: str,
    source_branch: str,
    memory_remote: str,
    memory_branch: str,
) -> dict[str, Any]:
    return {
        "schema_version": FLEET_SCHEMA_VERSION,
        "layout_version": LAYOUT_VERSION,
        "source": {
            "repository": source_remote,
            "branch": source_branch,
            "local_path": DEFAULT_SOURCE_DIR,
        },
        "memory": {
            "repository": memory_remote,
            "branch": memory_branch,
            "local_path": DEFAULT_MEMORY_DIR,
            "auto_sync": "both",
        },
        "codex": {
            "marketplace": DEFAULT_MARKETPLACE,
            "plugin": DEFAULT_PLUGIN,
        },
        "policy": {
            "source_of_truth": "github",
            "local_checkouts_are_working_copies": True,
            "shared_config_must_not_contain_secrets": True,
            "device_specific_absolute_paths_are_local_only": True,
        },
    }


def ensure_shared_config(
    memory_dir: Path,
    *,
    source_remote: str,
    source_branch: str,
    memory_remote: str,
    memory_branch: str,
) -> str:
    path = memory_dir / SHARED_CONFIG_NAME
    expected = shared_config_payload(
        source_remote=source_remote,
        source_branch=source_branch,
        memory_remote=memory_remote,
        memory_branch=memory_branch,
    )
    if not path.exists():
        atomic_write_json(path, expected)
        return "created"

    actual = load_json(path)
    if actual.get("schema_version") != FLEET_SCHEMA_VERSION:
        raise FleetError(f"Unsupported fleet config schema in {path}")
    if actual.get("layout_version") != LAYOUT_VERSION:
        raise FleetError(f"Unsupported fleet layout version in {path}")

    source = actual.get("source")
    memory = actual.get("memory")
    codex = actual.get("codex")
    policy = actual.get("policy")
    if not isinstance(source, dict) or not isinstance(memory, dict) or not isinstance(codex, dict) or not isinstance(policy, dict):
        raise FleetError(f"Fleet config sections are invalid: {path}")

    if set(actual) != set(expected):
        raise FleetError(f"Fleet config has unexpected or missing top-level keys: {path}")
    for section_name in ("source", "memory", "codex", "policy"):
        actual_section = actual.get(section_name)
        expected_section = expected.get(section_name)
        if not isinstance(actual_section, dict) or not isinstance(expected_section, dict):
            raise FleetError(f"Fleet config section is invalid: {section_name}")
        if set(actual_section) != set(expected_section):
            raise FleetError(f"Fleet config section has unexpected or missing keys: {section_name}")

    checks = (
        ("source.repository", source.get("repository"), source_remote, True),
        ("source.branch", source.get("branch"), source_branch, False),
        ("memory.repository", memory.get("repository"), memory_remote, True),
        ("memory.branch", memory.get("branch"), memory_branch, False),
        ("memory.auto_sync", memory.get("auto_sync"), "both", False),
        ("codex.marketplace", codex.get("marketplace"), DEFAULT_MARKETPLACE, False),
        ("codex.plugin", codex.get("plugin"), DEFAULT_PLUGIN, False),
        ("source.local_path", source.get("local_path"), DEFAULT_SOURCE_DIR, False),
        ("memory.local_path", memory.get("local_path"), DEFAULT_MEMORY_DIR, False),
        ("policy.source_of_truth", policy.get("source_of_truth"), "github", False),
        (
            "policy.local_checkouts_are_working_copies",
            policy.get("local_checkouts_are_working_copies"),
            True,
            False,
        ),
        (
            "policy.shared_config_must_not_contain_secrets",
            policy.get("shared_config_must_not_contain_secrets"),
            True,
            False,
        ),
        (
            "policy.device_specific_absolute_paths_are_local_only",
            policy.get("device_specific_absolute_paths_are_local_only"),
            True,
            False,
        ),
    )
    for label, current, wanted, remote_value in checks:
        current_text = str(current or "")
        wanted_text = str(wanted)
        equal = normalize_remote(current_text) == normalize_remote(wanted_text) if remote_value else current_text == wanted_text
        if not equal:
            raise FleetError(f"Shared fleet config mismatch for {label}: {current_text!r} != {wanted_text!r}")
    return "verified"


def backup_path(path: Path) -> Path:
    destination = path.with_name(path.name + f".bak.{timestamp()}")
    path.replace(destination)
    return destination


def install_agents() -> list[str]:
    destination_dir = codex_home() / "agents"
    destination_dir.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for name in AGENT_NAMES:
        source = AGENT_SOURCE / name
        destination = destination_dir / name
        if not source.is_file():
            raise FleetError(f"Missing RouteCraft agent source: {source}")
        source_digest = hashlib.sha256(source.read_bytes()).digest()
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).digest() == source_digest:
            continue
        if destination.exists():
            if not destination.is_file():
                raise FleetError(f"Agent destination is not a regular file: {destination}")
            backup = destination.with_name(destination.name + f".bak.{timestamp()}")
            shutil.copy2(destination, backup)
        shutil.copy2(source, destination)
        changed.append(name)
    return changed


def install_plugin(source_dir: Path, version: str) -> dict[str, Any]:
    require_command("codex")
    remove_plugin = run(("codex", "plugin", "remove", DEFAULT_PLUGIN), check=False)
    remove_market = run(("codex", "plugin", "marketplace", "remove", DEFAULT_MARKETPLACE), check=False)

    cache_root = codex_home() / "plugins" / "cache" / DEFAULT_MARKETPLACE / "codex-routecraft"
    cache_backup: str | None = None
    if cache_root.exists():
        cache_backup = str(backup_path(cache_root))

    run(("codex", "plugin", "marketplace", "add", str(source_dir)))
    run(("codex", "plugin", "add", DEFAULT_PLUGIN))
    agents_changed = install_agents()

    expected_cache = cache_root / version
    if not expected_cache.is_dir():
        raise FleetError(f"Codex did not materialize RouteCraft {version}: {expected_cache}")

    return {
        "removed_plugin": remove_plugin.returncode == 0,
        "removed_marketplace": remove_market.returncode == 0,
        "cache_backup": cache_backup,
        "cache": str(expected_cache),
        "agents_changed": agents_changed,
    }


def write_local_config(
    *,
    source_dir: Path,
    memory_dir: Path,
    source_remote: str,
    source_branch: str,
    memory_remote: str,
    memory_branch: str,
    version: str,
    status: Mapping[str, Any],
) -> Path:
    payload = {
        "schema_version": 1,
        "layout_version": LAYOUT_VERSION,
        "device_id": status.get("device_id"),
        "platform": platform.system(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "source_dir": str(source_dir),
        "source_remote": source_remote,
        "source_branch": source_branch,
        "memory_dir": str(memory_dir),
        "memory_remote": memory_remote,
        "memory_branch": memory_branch,
        "auto_sync": "both",
        "plugin_version": version,
        "last_bootstrap_at": utc_now(),
        "source_of_truth": "github",
    }
    path = local_config_path()
    atomic_write_json(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def verify_final_state(
    *,
    source_dir: Path,
    memory_dir: Path,
    source_branch: str,
    memory_branch: str,
    version: str,
) -> dict[str, Any]:
    status = run_memory("status", "--store", str(memory_dir), "--json", capture_json=True)
    assert isinstance(status, dict)
    git_status = status.get("git")
    counts = status.get("counts")
    if not isinstance(git_status, dict) or not isinstance(counts, dict):
        raise FleetError("RouteCraft memory status is incomplete")
    if git_status.get("dirty"):
        raise FleetError("Decision Store is dirty after bootstrap")
    if git_status.get("conflicts"):
        raise FleetError("Decision Store has conflicts after bootstrap")
    if git_status.get("branch") != memory_branch:
        raise FleetError(f"Decision Store branch mismatch: {git_status.get('branch')} != {memory_branch}")
    if not git_status.get("dedicated_root"):
        raise FleetError("Decision Store is not a dedicated Git root")

    source_head = git_output(source_dir, "rev-parse", "HEAD")
    source_tracking = run(
        ("git", "-C", str(source_dir), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"),
        check=False,
    ).stdout.strip()
    memory_head = git_output(memory_dir, "rev-parse", "HEAD")
    memory_tracking = run(
        ("git", "-C", str(memory_dir), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"),
        check=False,
    ).stdout.strip()

    expected_cache = codex_home() / "plugins" / "cache" / DEFAULT_MARKETPLACE / "codex-routecraft" / version
    missing_agents = [name for name in AGENT_NAMES if not (codex_home() / "agents" / name).is_file()]
    if missing_agents:
        raise FleetError("Missing installed RouteCraft agents: " + ", ".join(missing_agents))
    if not expected_cache.is_dir():
        raise FleetError(f"Missing RouteCraft plugin cache: {expected_cache}")
    if not (memory_dir / SHARED_CONFIG_NAME).is_file():
        raise FleetError(f"Missing shared fleet config: {memory_dir / SHARED_CONFIG_NAME}")

    return {
        "plugin_version": version,
        "source": {
            "path": str(source_dir),
            "branch": source_branch,
            "head": source_head,
            "tracking": source_tracking,
            "clean": not bool(git_output(source_dir, "status", "--porcelain")),
        },
        "memory": {
            "path": str(memory_dir),
            "branch": memory_branch,
            "head": memory_head,
            "tracking": memory_tracking,
            "counts": counts,
            "clean": not bool(git_output(memory_dir, "status", "--porcelain")),
        },
        "codex": {
            "home": str(codex_home()),
            "plugin_cache": str(expected_cache),
            "agents": len(AGENT_NAMES),
        },
    }


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    require_command("git")
    source_dir = expand_path(args.source_dir)
    memory_dir = expand_path(args.memory_dir)

    if source_dir.resolve() != REPO_ROOT.resolve():
        raise FleetError(
            f"This bootstrap must run from the configured RouteCraft source checkout.\n"
            f"Script repository: {REPO_ROOT}\nConfigured path:  {source_dir}"
        )

    update_source(source_dir, args.source_remote, args.source_branch)
    run((sys.executable, str(VERIFY_SCRIPT)), cwd=source_dir)
    version = manifest_version()

    memory_action = ensure_memory_store(
        memory_dir,
        args.memory_remote,
        args.memory_branch,
        allow_first_device=args.allow_first_device,
    )

    # Pull existing knowledge/config before validating or creating shared fleet config.
    run_memory("sync", "--store", str(memory_dir), "--mode", "both")
    shared_action = ensure_shared_config(
        memory_dir,
        source_remote=args.source_remote,
        source_branch=args.source_branch,
        memory_remote=args.memory_remote,
        memory_branch=args.memory_branch,
    )
    # Publish routecraft-fleet.json when it was created, or validate a clean store when present.
    sync_result = run_memory("sync", "--store", str(memory_dir), "--mode", "both", capture_json=True)
    run_memory("validate", "--store", str(memory_dir))

    plugin_result = install_plugin(source_dir, version)

    status = run_memory("status", "--store", str(memory_dir), "--json", capture_json=True)
    assert isinstance(status, dict)
    local_path = write_local_config(
        source_dir=source_dir,
        memory_dir=memory_dir,
        source_remote=args.source_remote,
        source_branch=args.source_branch,
        memory_remote=args.memory_remote,
        memory_branch=args.memory_branch,
        version=version,
        status=status,
    )

    final = verify_final_state(
        source_dir=source_dir,
        memory_dir=memory_dir,
        source_branch=args.source_branch,
        memory_branch=args.memory_branch,
        version=version,
    )
    return {
        "ok": True,
        "memory_action": memory_action,
        "shared_config": shared_action,
        "shared_config_path": str(memory_dir / SHARED_CONFIG_NAME),
        "local_config_path": str(local_path),
        "sync": sync_result,
        "plugin": plugin_result,
        **final,
    }


def status_command(args: argparse.Namespace) -> dict[str, Any]:
    source_dir = expand_path(args.source_dir)
    memory_dir = expand_path(args.memory_dir)
    version = manifest_version()
    return verify_final_state(
        source_dir=source_dir,
        memory_dir=memory_dir,
        source_branch=args.source_branch,
        memory_branch=args.memory_branch,
        version=version,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="routecraft-device",
        description="Idempotent RouteCraft multi-device bootstrap and health check",
    )
    parser.add_argument("--version", action="version", version="routecraft-device 0.4.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--source-dir", default=DEFAULT_SOURCE_DIR)
        target.add_argument("--memory-dir", default=DEFAULT_MEMORY_DIR)
        target.add_argument("--source-branch", default=DEFAULT_SOURCE_BRANCH)
        target.add_argument("--memory-branch", default=DEFAULT_MEMORY_BRANCH)
        target.add_argument("--json", action="store_true")

    bootstrap_parser = subparsers.add_parser("bootstrap", help="Install, configure, sync, and verify one device")
    common(bootstrap_parser)
    bootstrap_parser.add_argument("--source-remote", default=DEFAULT_SOURCE_REMOTE)
    bootstrap_parser.add_argument("--memory-remote", required=True)
    bootstrap_parser.add_argument("--allow-first-device", action="store_true")
    bootstrap_parser.set_defaults(func=bootstrap)

    status_parser = subparsers.add_parser("status", help="Verify the standardized local layout")
    common(status_parser)
    status_parser.set_defaults(func=status_command)
    return parser


def print_summary(result: Mapping[str, Any]) -> None:
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    memory = result.get("memory") if isinstance(result.get("memory"), dict) else {}
    codex = result.get("codex") if isinstance(result.get("codex"), dict) else {}
    counts = memory.get("counts") if isinstance(memory.get("counts"), dict) else {}
    print("RouteCraft device setup OK")
    print(f"- plugin: {result.get('plugin_version')}")
    print(f"- source: {source.get('path')} ({source.get('tracking')}, clean={source.get('clean')})")
    print(f"- memory: {memory.get('path')} ({memory.get('tracking')}, clean={memory.get('clean')})")
    print(
        "- records: "
        f"case={counts.get('case', 0)} candidate={counts.get('candidate', 0)} rule={counts.get('rule', 0)}"
    )
    print(f"- shared config: {result.get('shared_config_path', 'verified')}")
    print(f"- local config: {result.get('local_config_path', local_config_path())}")
    print(f"- Codex home: {codex.get('home')}")
    print("Start a fresh local Codex task before using RouteCraft.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_summary(result)
        return 0
    except FleetError as exc:
        print(f"routecraft-device: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("routecraft-device: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
