"""Bounded Context and Handoff Packs."""
from __future__ import annotations
import json, re, zipfile
from pathlib import Path
from typing import Any
from . import CONTEXT_PROFILES
from .git_tools import inspect_git, rule_based_session_summary
from .security import sanitize_text
from .service import _portable_text

REQUIRED_HANDOFF_FILES = ("HANDOFF.md", "PROJECT_STATE.json", "CHANGED_FILES.txt", "NEXT_TASKS.md", "KNOWN_ISSUES.md", "IMPORTANT_DECISIONS.md")

def _sanitize(value: Any, *known_paths: Any) -> str:
    result = sanitize_text(_portable_text(value, *known_paths))
    clean = result[0] if isinstance(result, tuple) else str(result)
    home = str(Path.home())
    for variant in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
        if variant:
            clean = re.sub(re.escape(variant), "<PATH>", clean, flags=re.IGNORECASE)
    return clean

def estimate_tokens(text: str) -> int:
    ranges = ((0x3040, 0x30FF), (0x3400, 0x9FFF), (0xAC00, 0xD7AF))
    cjk = sum(1 for char in text if any(a <= ord(char) <= b for a, b in ranges))
    return cjk + (len(text) - cjk + 3) // 4

def _obj(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)
def _id(m: Any) -> str: return str(_obj(m, "id", _obj(m, "memory_id", "")))
def _kind(m: Any) -> str: return str(_obj(m, "memory_type", _obj(m, "type", "note"))).lower()
def _body(m: Any) -> str: return str(_obj(m, "body", _obj(m, "content", "")))

def _memories(service: Any, ref: Any) -> list[Any]:
    fn = getattr(service, "list_memories", None)
    if not fn: return []
    for call in (
        lambda: fn(project_ref=ref, limit=100_000),
        lambda: fn(ref, limit=100_000),
        lambda: fn(ref),
    ):
        try: return list(call() or [])
        except TypeError: continue
    return []

def _project(service: Any, ref: Any) -> Any:
    fn = getattr(service, "get_project", None)
    if not fn: return {"name": str(ref), "description": "", "current_objective": ""}
    try: return fn(ref)
    except TypeError: return fn(project_ref=ref)

def _rank(m: Any) -> tuple[int, int, str]:
    return ({"high": 3, "medium": 2, "low": 1}.get(str(_obj(m, "importance", "medium")).lower(), 2), 1 if _obj(m, "verified", False) else 0, str(_obj(m, "updated_at", _obj(m, "created_at", ""))))

def _dedup(ms: list[Any]) -> list[Any]:
    seen=set(); out=[]
    for m in sorted(ms, key=_rank, reverse=True):
        key=re.sub(r"\s+", " ", (str(_obj(m,"title",""))+"\n"+_body(m)).strip().lower())
        if key and key not in seen: seen.add(key); out.append(m)
    return out

def _section(kind: str) -> str:
    if kind in {"decision", "architecture"}: return "Decisions"
    if kind == "constraint": return "Constraints"
    if kind in {"session_summary", "deployment", "lesson"}: return "Recent work"
    if kind in {"failure", "security"}: return "Known issues / failures"
    if kind == "next_action": return "Next tasks"
    if kind in {"file_reference", "dependency"}: return "Important files"
    return "Recent work"

def _block(m: Any) -> str: return f"### {_obj(m,'title','Untitled')}\n[{_kind(m)}]\n{_body(m)}\n"
def _cap(chars):
    if chars is not None and chars <= 0: raise ValueError("max_chars must be positive")
    return chars or 10**9

def _markdown(project, git, selected):
    p={"name":_obj(project,"name",""),"description":_obj(project,"description",_obj(project,"summary","")),"current_objective":_obj(project,"current_objective",_obj(project,"objective",""))}
    lines=["# Context Pack","","## Project summary / current objective",f"Name: {p['name']}",f"Description: {p['description']}",f"Current objective: {p['current_objective']}",""]
    for section in ("Decisions","Constraints","Recent work","Known issues / failures","Next tasks","Important files"):
        lines += [f"## {section}",""]+[x for m in selected if _section(_kind(m))==section for x in (_block(m),"")]
    lines += ["## Recent commits",""]+[f"- {c['short_hash']}: {c['subject']} ({c['date']})" for c in (git or {}).get("recent_commits",[])]+["","## Git changed files",""]+[f"- {x}" for x in (git or {}).get("changed_files",[])]+["","## Instructions to receiving AI","Current repository evidence overrides memory. Verify assumptions and do not disclose secrets.",""]
    return "\n".join(lines)

def build_context_pack(service, project_ref, format="markdown", profile="standard", max_chars=None, max_tokens=None):
    if format not in {"markdown","text","json"}: raise ValueError("format must be markdown, text, or json")
    if profile not in CONTEXT_PROFILES: raise ValueError(f"unknown context profile: {profile}")
    if max_tokens is not None and max_tokens <= 0: raise ValueError("max_tokens must be positive")
    cap=min(CONTEXT_PROFILES[profile],_cap(max_chars)); project=_project(service,project_ref); ms=_dedup(_memories(service,project_ref)); repo=_obj(project,"repo_path",_obj(project,"repository_path",None)); git=inspect_git(repo) if repo else None
    def fits(value):
        clean=_sanitize(value)
        return len(clean)<=cap and (max_tokens is None or estimate_tokens(clean)<=max_tokens)
    def json_content(items):
        return json.dumps({"project":{"name":_obj(project,"name",str(project_ref)),"description":_obj(project,"description",_obj(project,"summary","")),"current_objective":_obj(project,"current_objective","")},"memories":[{"id":_id(x),"type":_kind(x),"title":_obj(x,"title",""),"body":_body(x)} for x in items],"recent_commits":(git or {}).get("recent_commits",[]),"important_files":(git or {}).get("changed_files",[])},ensure_ascii=False)
    if format == "json" and not fits(json_content([])):
        raise ValueError("max_chars/max_tokens is too small for a valid JSON Context Pack")
    selected=[]
    for m in ms:
        candidate=selected+[m]
        if format=="json":
            raw=json_content(candidate)
        else: raw=_markdown(project,git,candidate)
        if fits(raw): selected=candidate
    if format=="json": raw=json_content(selected)
    else: raw=_markdown(project,git,selected); raw=re.sub(r"^#+\s*","",raw,flags=re.M) if format=="text" else raw
    content=_sanitize(raw)
    if not fits(content) and format != "json":
        low,high=0,min(len(content),cap)
        while low<high:
            middle=(low+high+1)//2
            if fits(content[:middle]): low=middle
            else: high=middle-1
        content=content[:low]
    return {"content":content,"format":format,"char_count":len(content),"estimated_tokens":estimate_tokens(content),"max_chars":cap,"max_tokens":max_tokens,"included_memory_ids":[_id(x) for x in selected],"omitted_count":len(ms)-len(selected)}

def build_handoff_pack(service, project_ref, output, as_zip=False):
    project=_project(service,project_ref); repo=_obj(project,"repo_path",_obj(project,"repository_path",None)); git=inspect_git(repo) if repo else {"is_repository":False,"changed_files":[],"recent_commits":[]}; ms=_dedup(_memories(service,project_ref)); context=build_context_pack(service,project_ref)
    grouped={s:[_block(m) for m in ms if _section(_kind(m))==s] for s in ("Next tasks","Known issues / failures","Decisions","Constraints")}
    files={"HANDOFF.md":"# Handoff Pack\n\n"+context["content"],"PROJECT_STATE.json":json.dumps({"project_ref":str(project_ref),"name":_obj(project,"name",str(project_ref)),"repository":"<REPO_PATH>","git":git},ensure_ascii=False,indent=2),"CHANGED_FILES.txt":"\n".join(git.get("changed_files",[])),"NEXT_TASKS.md":"# Next tasks\n\n"+"\n".join(grouped["Next tasks"]),"KNOWN_ISSUES.md":"# Known issues\n\n"+"\n".join(grouped["Known issues / failures"]),"IMPORTANT_DECISIONS.md":"# Important decisions\n\n"+"\n".join(grouped["Decisions"]+grouped["Constraints"])}
    clean={n:_sanitize(v,repo) for n,v in files.items()}; target=Path(output).expanduser(); folder=target.with_suffix("") if as_zip or target.suffix.lower()==".zip" else target
    archive=target if as_zip and target.suffix.lower()==".zip" else folder.with_suffix(".zip") if as_zip else None
    if archive and archive.exists(): raise ValueError("handoff ZIP already exists")
    if any((folder/name).exists() for name in REQUIRED_HANDOFF_FILES): raise ValueError("handoff target contains an existing generated file")
    folder.mkdir(parents=True,exist_ok=True)
    for n,v in clean.items(): (folder/n).write_text(v,encoding="utf-8",newline="\n")
    if as_zip:
        with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as z:
            for n in REQUIRED_HANDOFF_FILES:
                if Path(n).is_absolute() or Path(n).name!=n: raise ValueError("unsafe handoff entry")
                z.write(folder/n,arcname=n)
    return {"folder":str(folder),"zip":str(archive) if archive else None,"files":list(clean),"char_count":sum(map(len,clean.values()))}
