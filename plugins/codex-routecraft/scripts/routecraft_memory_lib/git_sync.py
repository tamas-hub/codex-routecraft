"""Dedicated Git-store synchronization for RouteCraft memory."""
from __future__ import annotations

from .common import *  # noqa: F401,F403
from .search import build_index

def run_git(store: Path, args: Sequence[str], *, check: bool = True) -> CommandResult:
    process = subprocess.run(
        ["git", *args],
        cwd=store,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    result = CommandResult(process.returncode, process.stdout, process.stderr)
    if check and process.returncode != 0:
        raise GitCommandError(args, process.returncode, process.stderr)
    return result


def require_git() -> None:
    if shutil.which("git") is None:
        raise RouteCraftError("git command not found; Git is required for sync")


def is_git_repository(store: Path) -> bool:
    if shutil.which("git") is None:
        return False
    result = run_git(store, ["rev-parse", "--is-inside-work-tree"], check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def git_root(store: Path) -> Path | None:
    if not is_git_repository(store):
        return None
    result = run_git(store, ["rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def ensure_dedicated_git_root(store: Path) -> None:
    root = git_root(store)
    if root is None:
        raise RouteCraftError(f"Memory store is not a Git repository: {store}")
    if root != store.resolve():
        raise RouteCraftError(
            f"Memory store must be the root of a dedicated Git repository. Git root is {root}, store is {store}."
        )


def git_is_detached(store: Path) -> bool:
    head_exists = run_git(store, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0
    symbolic = run_git(store, ["symbolic-ref", "--quiet", "HEAD"], check=False).returncode == 0
    return head_exists and not symbolic


def sync_path_allowed(path_value: str) -> bool:
    if not path_value or "\\" in path_value or path_value.startswith("/"):
        return False
    parts = path_value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if len(parts) == 1:
        return parts[0] in ALLOWED_SYNC_ROOT_FILES
    if len(parts) != 2 or parts[0] not in ALLOWED_SYNC_DIRECTORIES:
        return False
    filename = parts[1]
    return filename == ".gitkeep" or filename.lower().endswith(".md")


def porcelain_status_paths(store: Path) -> list[tuple[str, list[str]]]:
    result = run_git(
        store,
        ["-c", "core.quotepath=false", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=False,
    )
    entries = result.stdout.split("\0")
    parsed: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            parsed.append(("??", [entry]))
            continue
        status = entry[:2]
        paths = [entry[3:]]
        if "R" in status or "C" in status:
            if index >= len(entries) or not entries[index]:
                parsed.append((status, paths + ["<missing rename source>"]))
                continue
            paths.append(entries[index])
            index += 1
        parsed.append((status, paths))
    return parsed


def unexpected_git_paths(store: Path) -> list[str]:
    unexpected: list[str] = []
    for status, candidates in porcelain_status_paths(store):
        for path_text in candidates:
            if not sync_path_allowed(path_text):
                unexpected.append(f"{status} {path_text}")
                continue
            target = store.joinpath(*path_text.split("/"))
            if target.is_symlink():
                unexpected.append(f"{status} {path_text} (symlink)")
            elif target.exists() and not target.is_file():
                unexpected.append(f"{status} {path_text} (not a regular file)")
    return list(dict.fromkeys(unexpected))


def git_branch(store: Path, fallback: str = DEFAULT_BRANCH) -> str:
    result = run_git(store, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else fallback


def git_remote_exists(store: Path, remote: str) -> bool:
    result = run_git(store, ["remote", "get-url", remote], check=False)
    return result.returncode == 0


def remote_branch_exists(store: Path, remote: str, branch: str) -> bool:
    result = run_git(store, ["ls-remote", "--exit-code", "--heads", remote, branch], check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def git_conflicts(store: Path) -> list[str]:
    result = run_git(store, ["diff", "--name-only", "--diff-filter=U"], check=False)
    return [line for line in result.stdout.splitlines() if line.strip()]


def ensure_git_identity(store: Path, device_id: str) -> None:
    name = run_git(store, ["config", "--get", "user.name"], check=False).stdout.strip()
    email = run_git(store, ["config", "--get", "user.email"], check=False).stdout.strip()
    if not name:
        run_git(store, ["config", "user.name", f"RouteCraft Memory ({device_id})"])
    if not email:
        run_git(store, ["config", "user.email", "routecraft-memory@users.noreply.github.com"])


def stage_memory_paths(store: Path) -> None:
    paths = [
        ".routecraft-store.json",
        ".gitignore",
        "README.md",
        "cases",
        "candidates",
        "rules",
        "templates",
    ]
    existing = [path for path in paths if (store / path).exists()]
    if existing:
        run_git(store, ["add", "--", *existing])


def staged_changes(store: Path) -> bool:
    result = run_git(store, ["diff", "--cached", "--quiet"], check=False)
    return result.returncode == 1


def working_tree_dirty(store: Path) -> bool:
    result = run_git(store, ["status", "--porcelain"], check=False)
    return bool(result.stdout.strip())


def commit_memory_changes(store: Path, device_id: str, message: str | None = None) -> str | None:
    ensure_git_identity(store, device_id)
    stage_memory_paths(store)
    if not staged_changes(store):
        return None
    commit_message = message or f"routecraft memory sync from {device_id}"
    run_git(store, ["commit", "-m", commit_message])
    return run_git(store, ["rev-parse", "HEAD"]).stdout.strip()


def sync_store(
    store: Path,
    *,
    device_id: str,
    remote: str = DEFAULT_REMOTE,
    branch: str = DEFAULT_BRANCH,
    mode: str = "both",
    message: str | None = None,
    retries: int = 2,
) -> dict[str, Any]:
    require_git()
    ensure_store_layout(store)
    load_records(store)
    ensure_dedicated_git_root(store)
    validate_remote_name(remote)
    validate_branch_name(store, branch)
    if git_is_detached(store):
        raise RouteCraftError("Cannot sync from a detached HEAD; switch to the intended memory branch first")
    unexpected = unexpected_git_paths(store)
    if unexpected:
        raise RouteCraftError(
            "Memory store contains changes outside the allowed memory paths: " + ", ".join(unexpected)
        )
    if git_conflicts(store):
        raise RouteCraftError("Cannot sync while Git conflicts are unresolved")
    actual_branch = validate_branch_name(store, git_branch(store, branch))
    result: dict[str, Any] = {
        "store": str(store),
        "mode": mode,
        "branch": actual_branch,
        "remote": remote,
        "committed": None,
        "pulled": False,
        "pushed": False,
    }
    if mode == "pull":
        if working_tree_dirty(store):
            raise RouteCraftError("Pull-only sync requires a clean memory store; run sync --mode both to commit and publish changes")
        if not git_remote_exists(store, remote):
            raise RouteCraftError(f"Git remote does not exist: {remote}")
        if remote_branch_exists(store, remote, actual_branch):
            run_git(store, ["pull", "--rebase", remote, actual_branch])
            result["pulled"] = True
            load_records(store)
        build_index(store, write=True)
        return result

    result["committed"] = commit_memory_changes(store, device_id, message)
    if not git_remote_exists(store, remote):
        if mode == "push":
            raise RouteCraftError(f"Git remote does not exist: {remote}")
        build_index(store, write=True)
        return result

    if mode == "both" and remote_branch_exists(store, remote, actual_branch):
        run_git(store, ["pull", "--rebase", remote, actual_branch])
        result["pulled"] = True
        load_records(store)

    attempts = max(1, retries + 1)
    last_error: GitCommandError | None = None
    for attempt in range(attempts):
        push = run_git(store, ["push", "-u", remote, actual_branch], check=False)
        if push.returncode == 0:
            result["pushed"] = True
            break
        last_error = GitCommandError(["push", "-u", remote, actual_branch], push.returncode, push.stderr)
        if attempt + 1 >= attempts:
            break
        if remote_branch_exists(store, remote, actual_branch):
            pull = run_git(store, ["pull", "--rebase", remote, actual_branch], check=False)
            if pull.returncode != 0:
                raise GitCommandError(["pull", "--rebase", remote, actual_branch], pull.returncode, pull.stderr)
            result["pulled"] = True
            load_records(store)
    if not result["pushed"] and last_error:
        raise last_error
    build_index(store, write=True)
    return result


def maybe_sync_after_write(store: Path, config: Mapping[str, Any], args: argparse.Namespace, device_id: str) -> dict[str, Any] | None:
    requested = bool(getattr(args, "sync", False))
    auto_sync = str(config.get("auto_sync", "off"))
    if not requested and auto_sync != "both":
        return None
    remote = str(config.get("remote", DEFAULT_REMOTE))
    branch = str(config.get("branch", DEFAULT_BRANCH))
    with StoreLock(store, "auto-sync-after-write"):
        return sync_store(store, device_id=device_id, remote=remote, branch=branch, mode="both")


def maybe_sync_before_recall(store: Path, config: Mapping[str, Any], args: argparse.Namespace, device_id: str) -> dict[str, Any] | None:
    requested = bool(getattr(args, "sync_first", False))
    auto_sync = str(config.get("auto_sync", "off"))
    if not requested and auto_sync not in {"pull", "both"}:
        return None
    mode = "both" if auto_sync == "both" else "pull"
    remote = str(config.get("remote", DEFAULT_REMOTE))
    branch = str(config.get("branch", DEFAULT_BRANCH))
    with StoreLock(store, "auto-sync-before-recall"):
        return sync_store(store, device_id=device_id, remote=remote, branch=branch, mode=mode)
