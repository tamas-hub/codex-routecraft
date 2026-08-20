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
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

VERSION = "0.4.0"
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


class FleetError(RuntimeError):
    pass


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
        detail = proc.stderr.strip() or proc.stdout.strip()
        message = f"Command failed ({proc.returncode}): {' '.join(args)}"
        raise FleetError(message + (f"\n{detail}" if detail else ""))
    return proc


def require(name: str) -> None:
    if shutil.which(name) is None:
        raise FleetError(f"Required command not found: {name}")


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
    current = git(repo, "remote", "get-url", "origin", check=False)
    if current.returncode:
        git(repo, "remote", "add", "origin", expected)
    elif normalize_remote(current.stdout) != normalize_remote(expected):
        raise FleetError(f"origin does not match the configured source of truth:\n{current.stdout.strip()}\n{expected}")


def update_source(source: Path, remote: str, branch: str) -> None:
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
    proc = run(("git", "ls-remote", "--heads", remote, branch), check=False)
    if proc.returncode:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise FleetError(
            "Could not access the private memory repository. Authenticate Git and retry."
            + (f"\n{detail}" if detail else "")
        )
    return bool(proc.stdout.strip())


def ensure_store(store: Path, remote: str, branch: str, allow_first: bool) -> str:
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
            raise FleetError(f"Shared fleet config mismatch: {current!r} != {wanted!r}")
    if actual["policy"] != expected["policy"]:
        raise FleetError("Shared fleet policy mismatch")
    return "verified"


def plugin_version() -> str:
    value = str(load_json(MANIFEST).get("version", "")).strip()
    if not value:
        raise FleetError(f"Plugin manifest has no version: {MANIFEST}")
    return value


def install_agents() -> list[str]:
    destination_dir = codex_home() / "agents"
    destination_dir.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for name in AGENTS:
        source = AGENT_SOURCE / name
        destination = destination_dir / name
        if not source.is_file():
            raise FleetError(f"Missing RouteCraft agent: {source}")
        digest = hashlib.sha256(source.read_bytes()).digest()
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).digest() == digest:
            continue
        if destination.exists():
            if not destination.is_file():
                raise FleetError(f"Agent destination is not a regular file: {destination}")
            shutil.copy2(destination, destination.with_name(destination.name + ".bak." + stamp()))
        shutil.copy2(source, destination)
        changed.append(name)
    return changed


def install_plugin(source: Path, version: str) -> dict[str, Any]:
    require("codex")
    run(("codex", "plugin", "remove", PLUGIN), check=False)
    run(("codex", "plugin", "marketplace", "remove", MARKETPLACE), check=False)
    cache_root = codex_home() / "plugins" / "cache" / MARKETPLACE / "codex-routecraft"
    backup = None
    if cache_root.exists():
        backup_path = cache_root.with_name(cache_root.name + ".bak." + stamp())
        cache_root.replace(backup_path)
        backup = str(backup_path)
    run(("codex", "plugin", "marketplace", "add", str(source)))
    run(("codex", "plugin", "add", PLUGIN))
    changed = install_agents()
    expected = cache_root / version
    if not expected.is_dir():
        raise FleetError(f"Codex did not materialize RouteCraft {version}: {expected}")
    return {"cache": str(expected), "cache_backup": backup, "agents_changed": changed}


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
    write_json(
        target,
        {
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
        },
    )
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def source_control_config(github_owner: str | None, enabled: bool) -> Path | None:
    target = codex_home() / "routecraft" / "source-control.json"
    if not enabled:
        return target if target.is_file() else None
    owner = (github_owner or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner):
        raise FleetError("--github-owner is required and must be a valid GitHub owner when Source Guard is enabled")
    write_json(
        target,
        {
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
        },
    )
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


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


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    require("git")
    source = path(args.source_dir)
    store = path(args.memory_dir)
    if source != REPO_ROOT.resolve():
        raise FleetError(f"Run from the configured RouteCraft checkout: {REPO_ROOT} != {source}")
    update_source(source, args.source_remote, args.source_branch)
    run((sys.executable, str(VERIFY)), cwd=source)
    version = plugin_version()
    store_action = ensure_store(store, args.memory_remote, args.memory_branch, args.allow_first_device)
    memory("sync", "--store", str(store), "--mode", "both")
    fleet_action = ensure_fleet_config(
        store,
        fleet_payload(args.source_remote, args.source_branch, args.memory_remote, args.memory_branch),
    )
    sync = memory("sync", "--store", str(store), "--mode", "both", json_result=True)
    memory("validate", "--store", str(store))
    plugin = install_plugin(source, version)
    status = memory("status", "--store", str(store), "--json", json_result=True)
    assert isinstance(status, dict)
    local = local_config(
        source,
        store,
        args.source_remote,
        args.source_branch,
        args.memory_remote,
        args.memory_branch,
        version,
        status,
    )
    source_control = source_control_config(args.github_owner, args.enable_project_source_guard)
    result = verify_state(source, store, args.source_branch, args.memory_branch, version)
    return {
        "ok": True,
        "store_action": store_action,
        "fleet_config": fleet_action,
        "shared_config_path": str(store / ".routecraft-store.json"),
        "local_config_path": str(local),
        "source_control_config_path": str(source_control) if source_control else None,
        "sync": sync,
        "plugin": plugin,
        **result,
    }


def status(args: argparse.Namespace) -> dict[str, Any]:
    return verify_state(
        path(args.source_dir),
        path(args.memory_dir),
        args.source_branch,
        args.memory_branch,
        plugin_version(),
    )


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
    setup.set_defaults(func=bootstrap)

    check = commands.add_parser("status", help="Verify the standardized local layout")
    common(check)
    check.set_defaults(func=status)
    return root


def print_summary(result: Mapping[str, Any]) -> None:
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
        print(f"routecraft-device: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("routecraft-device: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
