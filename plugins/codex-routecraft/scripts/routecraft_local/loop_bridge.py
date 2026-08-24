"""Opt-in bridge between RouteCraft's lifecycle hook and Memory Local.

The bridge reads only a registered project's bounded Context Pack at session
start.  At a successful Stop it may save a rule-based Git summary.  It never
reads transcripts, creates projects, commits, pushes, or contacts a network.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .core import decision_store_ancestor
from .errors import RouteCraftLocalError
from .git_tools import inspect_git, rule_based_session_summary
from .packs import build_context_pack
from .service import RouteCraftService


SCHEMA_VERSION = 1
ALLOWED_CONFIG_KEYS = {
    "schema_version",
    "enabled",
    "data_dir",
    "auto_context",
    "auto_session_summary",
    "context_profile",
    "max_context_chars",
}


def _codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    return Path(value).expanduser().resolve() if value else (Path.home() / ".codex").resolve()


def config_path() -> Path:
    override = os.environ.get("ROUTECRAFT_LOCAL_LOOP_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return _codex_home() / "routecraft" / "local-memory.json"


def _defaults() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": False,
        "data_dir": str((Path.home() / ".routecraft-memory-local").resolve()),
        "auto_context": True,
        "auto_session_summary": True,
        "context_profile": "compact",
        "max_context_chars": 4000,
    }


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteCraftLocalError(f"could not read Local Memory Loop config: {exc}") from exc
    if not isinstance(value, dict):
        raise RouteCraftLocalError("Local Memory Loop config must be a JSON object")
    return value


def _validate_config(value: Mapping[str, Any]) -> dict[str, Any]:
    unexpected = set(value) - ALLOWED_CONFIG_KEYS
    if unexpected:
        raise RouteCraftLocalError("unsupported Local Memory Loop setting: " + ", ".join(sorted(unexpected)))
    config = {**_defaults(), **dict(value)}
    if config["schema_version"] != SCHEMA_VERSION:
        raise RouteCraftLocalError("unsupported Local Memory Loop config schema")
    for key in ("enabled", "auto_context", "auto_session_summary"):
        if not isinstance(config[key], bool):
            raise RouteCraftLocalError(f"{key} must be a boolean")
    if config["context_profile"] not in {"compact", "standard", "full"}:
        raise RouteCraftLocalError("context_profile must be compact, standard, or full")
    if not isinstance(config["max_context_chars"], int) or not 500 <= config["max_context_chars"] <= 5000:
        raise RouteCraftLocalError("max_context_chars must be between 500 and 5000")
    data_dir = Path(str(config["data_dir"])).expanduser().resolve()
    if decision_store_ancestor(data_dir) is not None:
        raise RouteCraftLocalError("Memory Local data directory must not reuse a RouteCraft Decision Store")
    config["data_dir"] = str(data_dir)
    return config


def load_config(*, enabled_only: bool = True) -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}
    config = _validate_config(_read_object(path))
    return config if not enabled_only or config["enabled"] else {}


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def configure(
    *,
    enabled: bool,
    data_dir: str | Path | None = None,
    auto_context: bool | None = None,
    auto_session_summary: bool | None = None,
    context_profile: str | None = None,
    max_context_chars: int | None = None,
) -> dict[str, Any]:
    path = config_path()
    previous = _read_object(path) if path.is_file() else {}
    value = {**_defaults(), **previous, "enabled": bool(enabled)}
    if data_dir is not None:
        value["data_dir"] = str(Path(data_dir).expanduser().resolve())
    if auto_context is not None:
        value["auto_context"] = auto_context
    if auto_session_summary is not None:
        value["auto_session_summary"] = auto_session_summary
    if context_profile is not None:
        value["context_profile"] = context_profile
    if max_context_chars is not None:
        value["max_context_chars"] = max_context_chars
    value = _validate_config(value)
    backup = None
    if path.is_file() and previous != value:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.stem}-backup-{timestamp}{path.suffix}")
        shutil.copy2(path, backup)
    _atomic_write(path, value)
    return {"config": str(path), "backup": str(backup) if backup else None, **value}


def status() -> dict[str, Any]:
    path = config_path()
    value = load_config(enabled_only=False) if path.is_file() else _defaults()
    recall_allowed, learning_allowed, reason = _evaluation_permissions()
    return {
        "config": str(path),
        "configured": path.is_file(),
        "evaluation_gate": {"recall": recall_allowed, "learning": learning_allowed, "reason": reason},
        **value,
    }


def _evaluation_permissions() -> tuple[bool, bool, str]:
    path = _codex_home() / "routecraft" / "evaluation" / "config.json"
    value = _read_object(path)
    if value.get("enabled") is not True:
        return True, True, "evaluation_disabled"
    experiment = value.get("experiment")
    if isinstance(experiment, Mapping) and experiment.get("enabled") is True:
        return False, False, "round_robin_experiment"
    mode = str(value.get("mode", "full"))
    if mode == "off":
        return False, False, "mode_off"
    if mode == "recall":
        return True, False, "mode_recall_only"
    return True, True, "mode_full"


def _session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:32]


def _state_path(session_id: str) -> Path:
    return _codex_home() / "routecraft" / "local-memory" / "sessions" / f"{_session_key(session_id)}.json"


def _repository_root(cwd: Path) -> Path | None:
    try:
        process = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if process.returncode:
        return None
    raw = process.stdout.decode("utf-8", errors="replace").strip()
    return Path(raw).resolve() if raw else None


def _fingerprint(repo_path: str | Path) -> str:
    info = inspect_git(repo_path, recent_limit=1)
    payload = {
        "is_repository": info.get("is_repository"),
        "head": info.get("head"),
        "clean": info.get("clean"),
        "working_tree": info.get("working_tree"),
        "diff": info.get("diff"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _remove_state(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def session_start(event: Mapping[str, Any]) -> dict[str, Any]:
    try:
        config = load_config()
        if not config:
            return {}
        recall_allowed, _, _ = _evaluation_permissions()
        if not recall_allowed:
            return {}
        session_id = str(event.get("session_id", "")).strip()
        cwd = Path(str(event.get("cwd", os.getcwd()))).expanduser().resolve()
        root = _repository_root(cwd)
        if not session_id or root is None:
            return {}
        service = RouteCraftService(config["data_dir"])
        service.initialize()
        project = service.find_project_by_repo(root)
        if project is None:
            return {}
        target = _state_path(session_id)
        if not target.is_file():
            _atomic_write(
                target,
                {
                    "schema_version": SCHEMA_VERSION,
                    "project_id": project["id"],
                    "start_fingerprint": _fingerprint(project["repo_path"]),
                },
            )
        if not config["auto_context"]:
            return {}
        context = build_context_pack(
            service,
            project["id"],
            profile=config["context_profile"],
            max_chars=config["max_context_chars"],
        )
        text = (
            "ROUTECRAFT MEMORY LOCAL CONTEXT (local prior project evidence; verify against current files):\n\n"
            + str(context["content"])
            + "\n\nAt completion, save verified semantic decisions and next actions explicitly. "
            "The Stop hook stores only a rule-based Git summary and never reads the transcript."
        )
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    except Exception as exc:
        return {"systemMessage": f"RouteCraft Memory Local context was unavailable: {exc}"}


def session_stop(event: Mapping[str, Any]) -> dict[str, Any]:
    if event.get("stop_hook_active") is True:
        return {}
    try:
        config = load_config()
        if not config:
            return {}
        session_id = str(event.get("session_id", "")).strip()
        if not session_id:
            return {}
        target = _state_path(session_id)
        state = _read_object(target)
        project_id = str(state.get("project_id", "")).strip()
        if not project_id:
            return {}
        _, learning_allowed, _ = _evaluation_permissions()
        if not learning_allowed:
            _remove_state(target)
            return {}
        service = RouteCraftService(config["data_dir"])
        project = service.get_project(project_id)
        if _fingerprint(project["repo_path"]) == state.get("start_fingerprint"):
            _remove_state(target)
            return {}
        if not config["auto_session_summary"]:
            _remove_state(target)
            return {}
        source_ref = f"routecraft-loop:{_session_key(session_id)}"
        summary = rule_based_session_summary(project["repo_path"])
        existing = service.add_loop_session_summary(
            project_id,
            summary["title"],
            summary["body"],
            related_files=summary.get("related_files", ()),
            related_commits=summary.get("related_commits", ()),
            source_ref=source_ref,
        )
        _remove_state(target)
        return {"systemMessage": f"RouteCraft Memory Local saved Git session summary {existing['id']}."}
    except Exception as exc:
        return {"systemMessage": f"RouteCraft Memory Local could not save the Git session summary: {exc}"}
