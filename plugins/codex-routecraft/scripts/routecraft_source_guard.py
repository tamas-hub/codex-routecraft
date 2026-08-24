#!/usr/bin/env python3
"""RouteCraft lifecycle guard for Git source control and Memory Loop closure.

The guard never stages, commits, pushes, creates repositories, or reads a
transcript. It records a local Git fingerprint at SessionStart and asks Codex
to continue at Stop when source is dirty/unpushed or a measured Memory Loop is
still open. It never learns records or reads a transcript.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

MAX_SESSION_CONTEXT_CHARS = 6000


def configure_text_streams() -> None:
    """Use the Codex hook's UTF-8 wire format regardless of Windows code page."""
    stdin_reconfigure = getattr(sys.stdin, "reconfigure", None)
    if callable(stdin_reconfigure):
        try:
            stdin_reconfigure(encoding="utf-8-sig", errors="strict")
        except (OSError, ValueError):
            pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


def _local_memory_call(method: str, event: Mapping[str, Any]) -> dict[str, Any]:
    """Call the optional Local bridge without making the existing hook depend on it."""
    try:
        module = importlib.import_module("routecraft_local.loop_bridge")
    except (ImportError, ModuleNotFoundError):
        return {}
    except Exception as exc:
        return {"systemMessage": f"RouteCraft Memory Local bridge was unavailable: {exc}"}
    try:
        result = getattr(module, method)(event)
        return dict(result) if isinstance(result, Mapping) else {}
    except Exception as exc:
        return {"systemMessage": f"RouteCraft Memory Local bridge failed safely: {exc}"}


def local_memory_start(event: Mapping[str, Any]) -> dict[str, Any]:
    return _local_memory_call("session_start", event)


def local_memory_stop(event: Mapping[str, Any]) -> dict[str, Any]:
    # A blocked Stop is retried with stop_hook_active=true. Source/evaluation
    # guards must not block again, but the Local bridge still has to finalize
    # its sidecar exactly once after those guards have yielded. The bridge's
    # state file and idempotent source_ref remain the duplicate-write guards.
    local_event = dict(event)
    local_event["stop_hook_active"] = False
    return _local_memory_call("session_stop", local_event)


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


def evaluation_dir() -> Path:
    configured = os.environ.get("ROUTECRAFT_EVALUATION_DIR")
    return Path(configured).expanduser().resolve() if configured else codex_home() / "routecraft" / "evaluation"


def evaluation_session_path(session_id: str) -> Path:
    return evaluation_dir() / "sessions" / f"{session_key(session_id)}.json"


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


def memory_policy_text() -> str:
    return (
        "ROUTECRAFT MEMORY LOOP (local evaluation is enabled): start one measured task after intent is clear; "
        "record exactly one bounded Recall result for recall/full mode; after verification, finish with learned record IDs "
        "or one finite skip reason. Emit the privacy-safe ROUTECRAFT MEMORY marker required by the orchestration skill. "
        "Never auto-learn from a transcript or store raw prompts, queries, paths, credentials, or raw session IDs."
    )


def unfinished_evaluation_tasks() -> list[str]:
    finished: set[str] = set()
    try:
        with (evaluation_dir() / "events.jsonl").open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                task_id = str(item.get("task_id", "")).strip() if isinstance(item, dict) else ""
                if task_id and item.get("event") == "task_finish":
                    finished.add(task_id)
    except OSError:
        pass

    open_tasks: set[str] = set()
    sessions_dir = evaluation_dir() / "sessions"
    try:
        states = sessions_dir.glob("*.json")
    except OSError:
        states = []
    for path in states:
        task_id = str(load_json(path).get("task_id", "")).strip()
        if task_id and task_id not in finished:
            open_tasks.add(task_id)
    return sorted(open_tasks)


def memory_start() -> dict[str, Any]:
    config = load_json(evaluation_dir() / "config.json")
    if config.get("enabled") is not True:
        return {}
    context = memory_policy_text()
    unfinished = unfinished_evaluation_tasks()
    if unfinished:
        preview = ", ".join(unfinished[:5])
        remainder = f" (ほか{len(unfinished) - 5}件)" if len(unfinished) > 5 else ""
        context += (
            " Previous sessions left unfinished local evaluation tasks: "
            f"{preview}{remainder}. Recover each task with finish after verification, or finish it as cancelled "
            "with the finite task_cancelled skip reason; do not silently abandon it."
        )
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


def memory_stop(event: Mapping[str, Any]) -> dict[str, Any]:
    if event.get("stop_hook_active") is True:
        return {}
    session_id = str(event.get("session_id", "")).strip()
    if not session_id:
        return {}
    target = evaluation_session_path(session_id)
    state = load_json(target)
    task_id = str(state.get("task_id", "")).strip()
    if not task_id:
        return {}
    finished = False
    try:
        with (evaluation_dir() / "events.jsonl").open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("event") == "task_finish" and item.get("task_id") == task_id:
                    finished = True
    except OSError:
        pass
    if finished:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return {}
    return {
        "decision": "block",
        "reason": (
            "RouteCraft Memory Loopが未完了です。検証後のLearnを実行した記録ID、または有限のスキップ理由を指定して"
            f"evaluation task {task_id} をfinishし、安全なROUTECRAFT MEMORYマーカーを出力してください。"
        ),
    }


def merge_results(event_name: object, *results: Mapping[str, Any]) -> dict[str, Any]:
    contexts: list[str] = []
    reasons: list[str] = []
    messages: list[str] = []
    for result in results:
        hook_output = result.get("hookSpecificOutput")
        if isinstance(hook_output, Mapping):
            context = str(hook_output.get("additionalContext", "")).strip()
            if context:
                contexts.append(context)
        if result.get("decision") == "block":
            reason = str(result.get("reason", "")).strip()
            if reason:
                reasons.append(reason)
        message = str(result.get("systemMessage", "")).strip()
        if message:
            messages.append(message)
    merged: dict[str, Any] = {}
    if contexts and event_name == "SessionStart":
        context = "\n\n".join(contexts)
        if len(context) > MAX_SESSION_CONTEXT_CHARS:
            suffix = "\n\n[Additional project memory was truncated to the RouteCraft hook budget.]"
            context = context[: MAX_SESSION_CONTEXT_CHARS - len(suffix)].rstrip() + suffix
        merged["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": context}
    if reasons:
        merged.update({"decision": "block", "reason": "\n\n".join(reasons)})
    if messages:
        merged["systemMessage"] = "\n".join(messages)
    return merged


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
    name = event.get("hook_event_name")
    if name == "SessionStart":
        source_result = start(event, config) if config else {}
        return merge_results(name, source_result, memory_start(), local_memory_start(event))
    if name == "Stop":
        source_result = stop(event, config) if config else {}
        evaluation_result = memory_stop(event)
        if source_result.get("decision") == "block" or evaluation_result.get("decision") == "block":
            return merge_results(name, source_result, evaluation_result)
        return merge_results(name, source_result, evaluation_result, local_memory_stop(event))
    return {}


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    configure_text_streams()
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
