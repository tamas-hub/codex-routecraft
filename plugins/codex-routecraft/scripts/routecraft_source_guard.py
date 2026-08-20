#!/usr/bin/env python3
"""Advisory GitHub source-of-truth guard for Codex lifecycle hooks.

The guard never stages, commits, pushes, creates repositories, or reads a
transcript. It records a local Git fingerprint at SessionStart and asks Codex
to continue at Stop only when the current task left source dirty or unpushed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def config_path() -> Path:
    return codex_home() / "routecraft" / "source-control.json"


def run_git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def git_text(cwd: Path, *args: str) -> str:
    proc = run_git(cwd, *args)
    return proc.stdout.decode("utf-8", errors="replace").strip() if proc.returncode == 0 else ""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_config() -> dict[str, Any]:
    value = load_json(config_path())
    if value.get("enabled") is not True:
        return {}
    if value.get("provider") != "github" or value.get("default_visibility") != "private":
        return {}
    if value.get("allow_force_push") is not False:
        return {}
    if value.get("store_raw_transcripts") is not False or value.get("store_device_config") is not False:
        return {}
    return value


def repository_root(cwd: Path) -> Path | None:
    proc = run_git(cwd, "rev-parse", "--show-toplevel")
    if proc.returncode:
        return None
    raw = proc.stdout.decode("utf-8", errors="replace").strip()
    return Path(raw).resolve() if raw else None


def working_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for args in (
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ("diff", "--binary", "--no-ext-diff", "HEAD", "--"),
    ):
        proc = run_git(root, *args)
        digest.update(proc.stdout)
        digest.update(b"\0")
    return digest.hexdigest()


def session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:32]


def state_path(session_id: str) -> Path:
    return codex_home() / "routecraft" / "source-guard" / f"{session_key(session_id)}.json"


def write_state(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def remote_is_github(value: str) -> bool:
    text = value.strip().rstrip("/")
    return any(
        re.fullmatch(pattern, text, flags=re.IGNORECASE)
        for pattern in (
            r"https?://github\.com/[^/]+/[^/]+(?:\.git)?",
            r"ssh://git@github\.com/[^/]+/[^/]+(?:\.git)?",
            r"git@github\.com:[^/]+/[^/]+(?:\.git)?",
        )
    )


def ahead_behind(root: Path) -> tuple[int, int] | None:
    upstream = git_text(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if not upstream:
        return None
    counts = git_text(root, "rev-list", "--left-right", "--count", f"{upstream}...HEAD").split()
    if len(counts) != 2 or not all(part.isdigit() for part in counts):
        return None
    behind, ahead = (int(part) for part in counts)
    return ahead, behind


def policy_text(config: Mapping[str, Any]) -> str:
    owner = str(config.get("github_owner", "")).strip() or "the configured GitHub owner"
    return (
        "GITHUB SOURCE-OF-TRUTH POLICY (standing user authorization): "
        "This policy applies to durable source code, project documents, tests, and intended deliverables, "
        "not to raw Codex transcripts, credentials, .env files, databases, uploads, caches, or device-local configuration. "
        "Before editing a repository, inspect its current branch, working tree, GitHub remote, and upstream; fetch and use "
        "fast-forward-only updates when safe. Preserve unrelated pre-existing changes. After the requested change is verified, "
        "review the complete diff, stage only task-owned safe files, commit with a clear message, and push without force. "
        f"For a durable project that has no remote, the standing default is to create a PRIVATE repository under {owner}; "
        "never make a repository public without separate explicit approval. If pull/rebase, secret scanning, tests, repository "
        "identity, or a safe push cannot be completed, stop and report the exact blocker instead of claiming synchronization."
    )


def start(event: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    session_id = str(event.get("session_id", "")).strip()
    cwd = Path(str(event.get("cwd", os.getcwd()))).expanduser().resolve()
    root = repository_root(cwd)
    if session_id and root is not None:
        target = state_path(session_id)
        if not target.exists():
            write_state(
                target,
                {
                    "schema_version": 1,
                    "repository_root": str(root),
                    "head": git_text(root, "rev-parse", "HEAD"),
                    "working_fingerprint": working_fingerprint(root),
                },
            )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": policy_text(config),
        }
    }


def stop(event: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    if event.get("stop_hook_active") is True:
        return {}
    session_id = str(event.get("session_id", "")).strip()
    if not session_id:
        return {}
    baseline = load_json(state_path(session_id))
    if not baseline:
        return {}
    cwd = Path(str(event.get("cwd", os.getcwd()))).expanduser().resolve()
    root = repository_root(cwd)
    if root is None or str(root) != baseline.get("repository_root"):
        return {}

    current_head = git_text(root, "rev-parse", "HEAD")
    current_fingerprint = working_fingerprint(root)
    changed = current_head != baseline.get("head") or current_fingerprint != baseline.get("working_fingerprint")
    if not changed:
        return {}

    dirty = bool(git_text(root, "status", "--porcelain=v1", "--untracked-files=all"))
    if dirty:
        reason = (
            "GitHub原本化が未完了です。このタスクで変更した安全なファイルだけを選び、秘密情報・生の会話・"
            "端末設定を除外して、必要な検証後にcommitしてください。開始前から存在した無関係な変更は保持してください。"
        )
        return {"decision": "block", "reason": reason}

    remote = git_text(root, "remote", "get-url", "origin")
    owner = str(config.get("github_owner", "")).strip() or "設定済みowner"
    if not remote:
        return {
            "decision": "block",
            "reason": (
                f"変更はcommit済みですがoriginがありません。内容と秘密情報を確認し、{owner}配下にPrivate GitHub "
                "Repositoryを作成してoriginを設定し、pushしてください。公開Repositoryは別の明示許可なしに作成しません。"
            ),
        }
    if not remote_is_github(remote):
        return {
            "decision": "block",
            "reason": "originがGitHubではありません。既存remoteを破壊せず、GitHub原本への安全な接続とpushを完了してください。",
        }

    divergence = ahead_behind(root)
    if divergence is None:
        return {
            "decision": "block",
            "reason": "現在branchにGitHub upstreamがありません。forceを使わずupstreamを設定してpushし、remote HEADを確認してください。",
        }
    ahead, behind = divergence
    if ahead or behind:
        return {
            "decision": "block",
            "reason": (
                f"GitHub原本との同期が未完了です（ahead={ahead}, behind={behind}）。fetch後に競合を安全に解消し、"
                "必要な検証を再実行して、forceを使わずpushしてください。"
            ),
        }
    return {}


def evaluate(event: Mapping[str, Any]) -> dict[str, Any]:
    config = load_config()
    if not config:
        return {}
    name = event.get("hook_event_name")
    if name == "SessionStart":
        return start(event, config)
    if name == "Stop":
        return stop(event, config)
    return {}


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            event = {}
        result = evaluate(event)
    except Exception as exc:  # Hooks must fail closed as a visible warning, not crash Codex.
        result = {"systemMessage": f"RouteCraft Source Guard could not verify Git state: {exc}"}
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
