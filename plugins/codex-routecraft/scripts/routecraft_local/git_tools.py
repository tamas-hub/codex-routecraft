"""Read-only Git inspection helpers for Memory Local packs."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


_TIMEOUT = 8


def _run(repo: Path, *args: str) -> tuple[str, int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(repo), shell=False,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=_TIMEOUT, check=False)
        return p.stdout, p.returncode, p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", 1, str(exc)


def _remote(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if re.match(r"^[^/@\s]+@[^:]+:", value):
        value = value.split("@", 1)[1]
        return value
    try:
        p = urlsplit(value)
        if p.scheme and p.netloc:
            return urlunsplit((p.scheme, p.hostname or "", p.path, "", ""))
    except ValueError:
        pass
    return re.sub(r"//[^/@]+@", "//", value)


def inspect_git(repo_path: str | Path, recent_limit: int = 10) -> dict:
    repo = Path(repo_path)
    out = {"is_repository": False, "branch": None, "head": None,
           "remote_url": None, "clean": None, "working_tree": [],
           "changed_files": [], "new_files": [], "deleted_files": [],
           "diff": {"additions": 0, "deletions": 0}, "recent_commits": [],
           "tags": [], "errors": []}
    if not repo.exists() or not repo.is_dir():
        out["errors"].append("repository path does not exist")
        return out
    probe, rc, err = _run(repo, "rev-parse", "--is-inside-work-tree")
    if rc or probe.strip().lower() != "true":
        out["errors"].append(err or "not a Git repository")
        return out
    out["is_repository"] = True
    for key, args in (("branch", ("branch", "--show-current")),
                      ("head", ("rev-parse", "HEAD"))):
        text, rc, err = _run(repo, *args)
        if rc: out["errors"].append(err or f"git {args[0]} failed")
        else: out[key] = text.strip() or None
    remotes, rc, err = _run(repo, "remote", "-v")
    if rc: out["errors"].append(err or "unable to inspect remotes")
    else:
        for line in remotes.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "(fetch)":
                # remote -v is ``name URL (fetch)``; keep the URL only.
                out["remote_url"] = _remote(parts[1])
                break
    status, rc, err = _run(repo, "status", "--porcelain=v1", "-z")
    if rc: out["errors"].append(err or "unable to inspect status")
    else:
        fields = status.split("\0")
        i = 0
        while i < len(fields):
            item = fields[i]
            i += 1
            if not item: continue
            code, name = (item[:2], item[3:]) if len(item) >= 3 else ("??", item)
            if "R" in code or "C" in code:
                target = name
                source = fields[i] if i < len(fields) else ""
                if i < len(fields): i += 1
                out["working_tree"].append({"status": code, "path": target, "previous_path": source})
            else:
                out["working_tree"].append({"status": code, "path": name})
    for entry in out["working_tree"]:
        path, code = entry["path"], entry["status"]
        if code == "??" or "A" in code: out["new_files"].append(path)
        if "D" in code: out["deleted_files"].append(path)
        if path not in out["changed_files"]: out["changed_files"].append(path)
    out["clean"] = not bool(out["working_tree"])
    out["working_tree_state"] = "clean" if out["clean"] else "dirty"
    stats = []
    for stat_args in (("diff", "--numstat"), ("diff", "--cached", "--numstat")):
        stat, rc, err = _run(repo, *stat_args)
        if rc:
            out["errors"].append(err or "unable to inspect diff")
        else:
            stats.append(stat)
    for stat in stats:
        for line in stat.splitlines():
            p = line.split("\t", 2)
            if len(p) >= 2:
                try: out["diff"]["additions"] += int(p[0]) if p[0].isdigit() else 0
                except ValueError: pass
                try: out["diff"]["deletions"] += int(p[1]) if p[1].isdigit() else 0
                except ValueError: pass
    log, rc, err = _run(repo, "log", f"-{max(0, min(int(recent_limit), 50))}", "--format=%H%x1f%h%x1f%ad%x1f%s", "--date=iso-strict")
    if rc: out["errors"].append(err or "unable to inspect log")
    else:
        for line in log.splitlines():
            p = line.split("\x1f")
            if len(p) == 4: out["recent_commits"].append({"hash": p[0], "short_hash": p[1], "date": p[2], "subject": p[3]})
    tags, rc, err = _run(repo, "tag", "--sort=-creatordate")
    if rc: out["errors"].append(err or "unable to inspect tags")
    else: out["tags"] = tags.splitlines()[:50]
    return out


def rule_based_session_summary(repo_path: str | Path) -> dict:
    info = inspect_git(repo_path)
    latest = info["recent_commits"][0] if info["recent_commits"] else None
    title = (latest["subject"] if latest else "Git session")[:120]
    state = "clean working tree" if info["clean"] else f"{len(info['working_tree'])} uncommitted change(s)"
    body = f"Latest commit: {latest['subject'] if latest else 'none'}. Current state: {state}. Diff: +{info['diff']['additions']}/-{info['diff']['deletions']}."
    return {"title": title, "body": body, "related_files": info["changed_files"],
            "related_commits": [c["hash"] for c in info["recent_commits"][:3]], "git": info}
