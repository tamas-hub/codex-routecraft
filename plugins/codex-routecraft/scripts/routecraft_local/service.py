"""Service API for RouteCraft Memory Local v1.0."""
from __future__ import annotations

import hashlib, json, os, re, sqlite3, tempfile, time, uuid, zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from . import IMPORTANCE_LEVELS, MEMORY_TYPES, SCHEMA_VERSION
from .core import LocalDatabase, json_load, json_value, utc_now
from .errors import ConfirmationRequiredError, ConflictError, IntegrityError, NotFoundError, RouteCraftLocalError
from .security import is_excluded_path, sanitize_text, sanitize_values

DEFAULT_SETTINGS={"language":"ja","telemetry_enabled":False,"excluded_globs":[],"excluded_directories":[],"excluded_extensions":[]}
MAX_TEXT=200_000
MAX_IMPORT_BYTES=50 * 1024 * 1024
MAX_BACKUP_BYTES=10 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES=1 * 1024 * 1024
SAFE_IDENTIFIER_RE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
CANONICAL_IDENTIFIER_RE=re.compile(r"^(?:MEM|PRJ)-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",re.I)
FAILURE_OUTCOMES={"failed","failure","unresolved","blocked","aborted","abandoned","regression"}
FAILURE_HEADINGS={"failure","failure mode","failure summary","incident","failed result","失敗","失敗内容","障害","障害概要"}
PACKAGE_MISSING=object()
UNQUOTED_ABSOLUTE_PATH_RE=re.compile(
    r"(?i:\bfile://+)[^\s<>\"'`|),;!?}\]]+"
    r"|(?<![A-Za-z0-9:/\\])\\(?!\\)[^\s<>\"'`|),;!?}\]]+"
    r"|"
    r"(?<![A-Za-z0-9])(?:\\\\\?\\)?[A-Za-z]:[\\/][^\s<>\"'`|),;!?}\]]+"
    r"|(?:\\\\|(?<![:/])//)[^\\/\s<>\"'`|),;!?}\]]+[\\/][^\s<>\"'`|),;!?}\]]+"
    r"|(?<![A-Za-z0-9:/\\])/(?!/)[^\s<>\"'`|),;!?}\]]+"
)
QUOTED_VALUE_RE=re.compile(r"(?P<quote>[\"'])(?P<value>[^\"'\r\n]+)(?P=quote)")

def _id(prefix): return f"{prefix}-{uuid.uuid4()}"
def _hash(*parts): return hashlib.sha256("\x1f".join(str(x) for x in parts).encode("utf-8")).hexdigest()
def _safe_identifier(value):
    if not isinstance(value,str) or not SAFE_IDENTIFIER_RE.fullmatch(value) or ".." in value: return False
    if CANONICAL_IDENTIFIER_RE.fullmatch(value): return True
    clean,findings=sanitize_text(value)
    return not findings and clean==value
def _portable_identifier(value,prefix,mapping):
    text=str(value or "")
    if _safe_identifier(text): return text,[]
    if text not in mapping: mapping[text]=_id(prefix)
    _,findings=sanitize_text(text)
    return mapping[text],list(dict.fromkeys(findings or ["unsafe_identifier"]))
def _opaque_source_ref(value): return "SRC-"+hashlib.sha256(str(value).encode("utf-8")).hexdigest()
def _normalize_source_ref(value):
    text=str(value or "")
    if not text: return "",[]
    clean,findings=sanitize_text(text)
    if findings or "[REDACTED:" in clean: return _opaque_source_ref(text),list(dict.fromkeys(findings or ["unsafe_source_ref"]))
    return clean,findings
def _portable_source_ref(value,*known_paths):
    text=str(value or "")
    if not text: return "",[]
    portable=_portable_path(_portable_text(text,*known_paths)); clean,findings=sanitize_text(portable)
    if findings or portable!=text or "[REDACTED:" in clean: return _opaque_source_ref(text),list(dict.fromkeys(findings or ["source_ref"]))
    return clean,findings
def _strict_bool(value,name):
    if isinstance(value,bool) or isinstance(value,int) and value in (0,1): return int(bool(value))
    raise RouteCraftLocalError(f"{name} must be a boolean")
def _package_bool(value,default,name):
    if value is PACKAGE_MISSING: return int(bool(default))
    return _strict_bool(value,name)
def _package_timestamp(value,default,name):
    if value is PACKAGE_MISSING: return default
    if not isinstance(value,str) or not value or len(value)>64: raise RouteCraftLocalError(f"{name} must be a timezone-aware ISO-8601 string")
    normalized=value[:-1]+"+00:00" if value.endswith("Z") else value
    try: parsed=datetime.fromisoformat(normalized)
    except ValueError as exc: raise RouteCraftLocalError(f"{name} must be a timezone-aware ISO-8601 string") from exc
    if parsed.tzinfo is None: raise RouteCraftLocalError(f"{name} must be a timezone-aware ISO-8601 string")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
def _legacy_case_type(meta,body):
    outcome=str(meta.get("outcome") or "").strip().casefold()
    status=str(meta.get("status") or "").strip().casefold()
    if outcome in FAILURE_OUTCOMES or status in FAILURE_OUTCOMES: return "failure"
    headings={re.sub(r"[：:]$","",heading.strip().casefold()) for heading in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$",str(body))}
    return "failure" if headings & FAILURE_HEADINGS else "lesson"
def _string_list(value, name):
    if value is None: return []
    if isinstance(value, str) or not isinstance(value, (list, tuple, set)):
        raise RouteCraftLocalError(f"{name} must be a list of strings")
    if len(value) > 1000: raise RouteCraftLocalError(f"{name} has too many values")
    result=[str(item) for item in value]
    if any(len(item) > 2000 for item in result): raise RouteCraftLocalError(f"{name} contains an overlong value")
    return result
def _portable_text(value,*known_paths):
    text=str(value or "")
    paths=[str(Path.home()),*(str(path) for path in known_paths if path)]
    for path in sorted(set(paths),key=len,reverse=True):
        if not path: continue
        variants={path,path.replace("\\","/"),path.replace("/","\\")}
        for variant in variants:
            text=re.sub(re.escape(variant),"<PATH>",text,flags=re.IGNORECASE)
    def replace_quoted(match):
        candidate=match.group("value"); normalized=candidate.replace("\\","/")
        is_absolute=bool(re.match(r"^(?:[A-Za-z]:/|//\?/+[A-Za-z]:/|//[^/]+/|/)",normalized))
        return f"{match.group('quote')}<PATH>{match.group('quote')}" if is_absolute and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://",candidate) else match.group(0)
    text=QUOTED_VALUE_RE.sub(replace_quoted,text)
    text=UNQUOTED_ABSOLUTE_PATH_RE.sub("<PATH>",text)
    return text
def _portable_path(value):
    text = _portable_text(value)
    normalized=text.replace("\\","/")
    if re.match(r"^[A-Za-z]:/", normalized) or normalized.startswith(("/", "~")) or re.search(r"(?i)(?:^|/)(?:users|home)/", normalized): return "<PATH>"
    return text
def _safe_remote(value):
    text = str(value or ""); findings = []
    try:
        parsed = urlsplit(text)
        if parsed.scheme.casefold()=="file":
            return "<PATH>",["path"]
        if parsed.scheme and parsed.netloc:
            host = parsed.hostname or ""
            if parsed.port: host += f":{parsed.port}"
            text = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        elif "@" in text and ":" in text and not text.startswith("git@"):
            text = text.rsplit("@", 1)[-1]
    except ValueError:
        text = "[REDACTED:remote]"; findings.append("remote")
    text, redactions = sanitize_text(text)
    return text, list(dict.fromkeys(findings + redactions))
def _safe_project_values(repo_path, git_remote_url, ai_agents, languages, tags):
    repo_path, f1 = sanitize_text(repo_path)
    git_remote_url, f2 = _safe_remote(git_remote_url)
    agents, f3 = sanitize_values(_string_list(ai_agents, "ai_agents"))
    langs, f4 = sanitize_values(_string_list(languages, "languages"))
    labels, f5 = sanitize_values(_string_list(tags, "tags"))
    return repo_path, git_remote_url, agents, langs, labels, list(dict.fromkeys(f1+f2+f3+f4+f5))
def _safe_metadata(value):
    findings=[]
    def walk(item):
        if isinstance(item,str):
            clean,found=sanitize_text(item); findings.extend(found); return clean
        if isinstance(item,list): return [walk(v) for v in item]
        if isinstance(item,dict):
            result={}
            for key,value in item.items():
                raw=str(key); clean,found=sanitize_text(raw); findings.extend(found)
                if found or "[REDACTED:" in clean: clean="KEY-"+hashlib.sha256(raw.encode("utf-8")).hexdigest()
                result[clean]=walk(value)
            return result
        return item
    return walk(value),list(dict.fromkeys(findings))
def _fts_query(value):
    terms=[term for term in re.split(r"\s+",str(value).strip()) if term]
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
def _row(row):
    if row is None: return None
    data=dict(row)
    for key in ("ai_agents","languages","tags","related_files","related_commits","legacy_metadata"):
        if key in data: data[key]=json_load(data[key], {} if key=="legacy_metadata" else [])
    for key in ("archived","active","verified"):
        if key in data: data[key]=bool(data[key])
    return data

class RouteCraftService:
    def __init__(self, data_dir: str|Path|None=None): self.db=LocalDatabase(data_dir); self._ready=False
    @property
    def data_dir(self): return self.db.data_dir
    def initialize(self):
        if not self._ready:
            result=self.db.initialize(); self._ensure_settings(); self._ready=True
        else:
            result={"data_dir":str(self.db.data_dir),"database":str(self.db.path),"schema_version":SCHEMA_VERSION}
        return result
    def _ensure_settings(self):
        with self.db.connect() as db:
            for key,value in DEFAULT_SETTINGS.items(): db.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",(key,json.dumps(value,ensure_ascii=False),utc_now()))
    def doctor(self):
        self.initialize()
        with self.db.connect() as db: memory_count=db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        return {"ok":self.db.integrity()=="ok","schema_version":SCHEMA_VERSION,"database":str(self.db.path),"fts5":self._fts(),"projects":len(self.list_projects(True)),"memories":memory_count}
    def _fts(self):
        with self.db.connect() as db: return bool(db.execute("SELECT name FROM sqlite_master WHERE name='memories_fts'").fetchone())
    def get_settings(self):
        self.initialize()
        with self.db.connect() as db: values={r["key"]:json_load(r["value"],r["value"]) for r in db.execute("SELECT * FROM settings")}
        return {**DEFAULT_SETTINGS,**values}
    def update_settings(self,values):
        if not isinstance(values,dict): raise RouteCraftLocalError("settings must be an object")
        current=self.get_settings(); allowed=set(DEFAULT_SETTINGS)
        for key,value in values.items():
            if key not in allowed: raise RouteCraftLocalError(f"unsupported setting: {key}")
            if key == "language" and (not isinstance(value,str) or not value.strip() or len(value) > 32): raise RouteCraftLocalError("language must be a short non-empty string")
            if key == "telemetry_enabled" and not isinstance(value,bool): raise RouteCraftLocalError("telemetry_enabled must be a boolean")
            if key.startswith("excluded_"):
                value = _string_list(value,key)
                if any(len(item) > 500 for item in value): raise RouteCraftLocalError(f"{key} contains an overlong value")
            current[key]=value
        with self.db.connect() as db:
            for key,value in current.items(): db.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,json.dumps(value,ensure_ascii=False),utc_now()))
        return self.get_settings()
    def _project(self,ref):
        self.initialize(); ref=str(ref)
        with self.db.connect() as db:
            row=db.execute("SELECT * FROM projects WHERE id=?",(ref,)).fetchone()
            if not row:
                rows=db.execute("SELECT * FROM projects WHERE name=? COLLATE NOCASE",(ref,)).fetchall()
                if len(rows)>1: raise ConflictError("project name is ambiguous; use exact ID")
                row=rows[0] if rows else None
        if not row: raise NotFoundError("project not found")
        return _row(row)
    def _prepare_project(self,name,repo_path='',git_remote_url='',ai_agents=(),languages=(),tags=(),description='',current_objective='',project_id=None):
        name,find=sanitize_text(name); description,f2=sanitize_text(description); objective,f3=sanitize_text(current_objective)
        repo_path,git_remote_url,ai_agents,languages,tags,f4=_safe_project_values(repo_path,git_remote_url,ai_agents,languages,tags)
        if not name.strip() or len(name)>300: raise RouteCraftLocalError("project name is required and at most 300 characters")
        if len(description)>MAX_TEXT or len(objective)>MAX_TEXT: raise RouteCraftLocalError("project description/objective is too large")
        if project_id is not None and not _safe_identifier(project_id): raise RouteCraftLocalError("project_id must be a safe identifier of at most 200 characters")
        now=utc_now(); item={"id":project_id or _id("PRJ"),"name":name.strip(),"repo_path":str(repo_path),"git_remote_url":str(git_remote_url),"ai_agents":json_value(list(ai_agents)),"languages":json_value(list(languages)),"tags":json_value(list(tags)),"description":description,"current_objective":objective,"archived":0,"created_at":now,"updated_at":now}
        return item,list(dict.fromkeys(find+f2+f3+f4))
    def add_project(self,name,repo_path='',git_remote_url='',ai_agents=(),languages=(),tags=(),description='',current_objective=''):
        self.initialize(); item,warnings=self._prepare_project(name,repo_path=repo_path,git_remote_url=git_remote_url,ai_agents=ai_agents,languages=languages,tags=tags,description=description,current_objective=current_objective)
        try:
            with self.db.connect() as db: db.execute("INSERT INTO projects VALUES(:id,:name,:repo_path,:git_remote_url,:ai_agents,:languages,:tags,:description,:current_objective,:archived,:created_at,:updated_at)",item)
        except Exception as exc:
            if "UNIQUE" in str(exc): raise ConflictError("active project name already exists") from exc
            raise
        out=self.get_project(item["id"]); out["warnings"]=warnings; return out
    def list_projects(self,include_archived=False):
        self.initialize(); sql="SELECT * FROM projects"+("" if include_archived else " WHERE archived=0")+" ORDER BY updated_at DESC,name"  # routecraft-security: allowlisted-sql-shape
        with self.db.connect() as db:return [_row(r) for r in db.execute(sql)]
    def get_project(self,ref): return self._project(ref)
    def find_project_by_repo(self,repo_path,include_archived=False):
        try: target=os.path.normcase(os.path.normpath(str(Path(repo_path).expanduser().resolve())))
        except (OSError,TypeError,ValueError): return None
        matches=[]
        for project in self.list_projects(include_archived=include_archived):
            if not project.get("repo_path"): continue
            try: candidate=os.path.normcase(os.path.normpath(str(Path(project["repo_path"]).expanduser().resolve())))
            except (OSError,TypeError,ValueError): continue
            if candidate==target: matches.append(project)
        if len(matches)>1: raise ConflictError("multiple projects use the same repository path")
        return matches[0] if matches else None
    def update_project(self,ref,**changes):
        old=self._project(ref); allowed={"name","repo_path","git_remote_url","ai_agents","languages","tags","description","current_objective","archived"}; bad=set(changes)-allowed
        if bad: raise RouteCraftLocalError("unsupported project field: "+", ".join(sorted(bad)))
        values={k:changes.get(k,old[k]) for k in allowed}; warnings=[]
        for k in ("name","description","current_objective"):
            values[k],f=sanitize_text(values[k]); warnings+=f
        if not values["name"].strip() or len(values["name"]) > 300: raise RouteCraftLocalError("project name is required and at most 300 characters")
        if len(values["description"]) > MAX_TEXT or len(values["current_objective"]) > MAX_TEXT: raise RouteCraftLocalError("project description/objective is too large")
        values["repo_path"],values["git_remote_url"],agents,langs,labels,found=_safe_project_values(values["repo_path"],values["git_remote_url"],values["ai_agents"],values["languages"],values["tags"]); warnings+=found
        values["ai_agents"],values["languages"],values["tags"]=json_value(agents),json_value(langs),json_value(labels)
        values["archived"]=_strict_bool(values["archived"],"archived"); values["updated_at"]=utc_now(); values["id"]=old["id"]
        try:
            with self.db.connect() as db: db.execute("UPDATE projects SET name=:name,repo_path=:repo_path,git_remote_url=:git_remote_url,ai_agents=:ai_agents,languages=:languages,tags=:tags,description=:description,current_objective=:current_objective,archived=:archived,updated_at=:updated_at WHERE id=:id",values)
        except __import__('sqlite3').IntegrityError as exc:
            raise ConflictError("active project name already exists") from exc
        out=self._project(old["id"]); out["warnings"]=list(dict.fromkeys(warnings)); return out
    def archive_project(self,ref,archived=True): return self.update_project(ref,archived=archived)
    def delete_project(self,ref,confirmation):
        with self.db.operation_lock():
            project=self._project(ref)
            if str(ref)!=project["id"] or confirmation!=project["id"]: raise ConfirmationRequiredError("delete_project requires the exact project ID as confirmation")
            safety_target=(self.data_dir/f"delete-safety-{uuid.uuid4().hex}.zip").resolve()
            if safety_target.parent != self.data_dir.resolve(): raise IntegrityError("delete safety target escaped the data directory")
            safety=self.export_project_package(project["id"],safety_target)
            with self.db.connect() as db:
                try: db.execute("DELETE FROM memories_fts WHERE memory_id IN (SELECT id FROM memories WHERE project_id=?)",(project["id"],))
                except __import__('sqlite3').OperationalError: pass
                db.execute("DELETE FROM projects WHERE id=?",(project["id"],))
        return {"deleted":project["id"],"safety_copy":safety["output"]}
    def _memory(self,ref):
        self.initialize()
        with self.db.connect() as db: row=db.execute("SELECT * FROM memories WHERE id=?",(str(ref),)).fetchone()
        if not row: raise NotFoundError("memory not found")
        return _row(row)
    def _save_fts(self,db,item):
        try:
            db.execute("DELETE FROM memories_fts WHERE memory_id=?",(item["id"],))
            db.execute("INSERT INTO memories_fts(memory_id,title,body,tags) VALUES(?,?,?,?)",(item["id"],item["title"],item["body"],item["tags"]))
        except __import__('sqlite3').OperationalError:
            pass
    def _prepare_memory(self,project_id,memory_type,title,body,importance='medium',tags=(),source='cli',related_files=(),related_commits=(),active=True,verified=False,memory_id=None,source_ref=None,legacy_metadata=None):
        if memory_type not in MEMORY_TYPES: raise RouteCraftLocalError("unsupported memory type")
        if importance not in IMPORTANCE_LEVELS: raise RouteCraftLocalError("unsupported importance")
        if memory_id is not None and not _safe_identifier(memory_id): raise RouteCraftLocalError("memory_id must be a safe non-secret identifier of at most 200 characters")
        if source_ref is not None and (not isinstance(source_ref, str) or len(source_ref) > 1000): raise RouteCraftLocalError("source_ref must be a string of at most 1000 characters")
        if legacy_metadata is not None and not isinstance(legacy_metadata, dict): raise RouteCraftLocalError("legacy_metadata must be an object")
        title,f1=sanitize_text(title); body,f2=sanitize_text(body); source,f3=sanitize_text(source); source_ref,f4=_normalize_source_ref(source_ref)
        if not title.strip() or not body.strip() or len(title)>500 or len(body)>MAX_TEXT or len(source)>500: raise RouteCraftLocalError("title/body/source is missing or too large")
        tag_list,ft=sanitize_values(_string_list(tags,"tags")); files,ff=sanitize_values(_string_list(related_files,"related_files")); commits,fc=sanitize_values(_string_list(related_commits,"related_commits")); now=utc_now(); ident=memory_id or _id("MEM")
        active_value=_strict_bool(active,"active"); verified_value=_strict_bool(verified,"verified")
        metadata,fm=_safe_metadata(legacy_metadata or {})
        item={"id":ident,"project_id":project_id,"memory_type":memory_type,"title":title,"body":body,"importance":importance,"tags":json_value(tag_list),"source":source,"related_files":json_value(files),"related_commits":json_value(commits),"active":active_value,"verified":verified_value,"source_ref":source_ref,"content_hash":_hash(memory_type,title,body),"legacy_metadata":json.dumps(metadata,ensure_ascii=False),"created_at":now,"updated_at":now}
        return item,list(dict.fromkeys(f1+f2+f3+f4+ft+ff+fc+fm))
    def _insert_memory(self,db,item):
        db.execute("INSERT INTO memories VALUES(:id,:project_id,:memory_type,:title,:body,:importance,:tags,:source,:related_files,:related_commits,:active,:verified,:source_ref,:content_hash,:legacy_metadata,:created_at,:updated_at)",item)
        self._save_fts(db,item)
    def add_memory(self,project_ref,memory_type,title,body,importance='medium',tags=(),source='cli',related_files=(),related_commits=(),active=True,verified=False,memory_id=None,source_ref=None,legacy_metadata=None):
        project=self._project(project_ref)
        item,warnings=self._prepare_memory(project["id"],memory_type,title,body,importance=importance,tags=tags,source=source,related_files=related_files,related_commits=related_commits,active=active,verified=verified,memory_id=memory_id,source_ref=source_ref,legacy_metadata=legacy_metadata)
        try:
            with self.db.connect() as db: self._insert_memory(db,item)
        except Exception as exc:
            if "UNIQUE" in str(exc): raise ConflictError("memory ID already exists") from exc
            raise
        out=self._memory(item["id"]); out["warnings"]=warnings; return out
    def get_memory(self,ref): return self._memory(ref)
    def find_memory_by_source_ref(self,project_ref,source_ref):
        project=self._project(project_ref); source_ref,_=_normalize_source_ref(source_ref)
        if not source_ref: return None
        with self.db.connect() as db: row=db.execute("SELECT * FROM memories WHERE project_id=? AND source_ref=? ORDER BY created_at DESC,id LIMIT 1",(project["id"],source_ref)).fetchone()
        return _row(row)
    def add_loop_session_summary(self,project_ref,title,body,*,related_files=(),related_commits=(),source_ref):
        """Create one loop summary per project/source reference, atomically.

        ``loop_session_summaries`` is an additive durable key table rather than
        a uniqueness constraint on ``memories.source_ref``: old Local databases
        may legitimately contain duplicate generic source references.  Legacy
        rows are preserved and the oldest matching row is adopted as the
        idempotent result.
        """
        project=self._project(project_ref)
        item,warnings=self._prepare_memory(
            project["id"], "session_summary", title, body,
            importance="medium", tags=("git", "session", "routecraft-loop"),
            source="routecraft-loop", related_files=related_files,
            related_commits=related_commits, verified=False,
            source_ref=source_ref,
        )
        if not item["source_ref"]:
            raise RouteCraftLocalError("loop session summary requires a source reference")
        for attempt in range(4):
            try:
                with self.db.connect(immediate=True) as db:
                    keyed=db.execute("SELECT memory_id FROM loop_session_summaries WHERE project_id=? AND source_ref=?",(project["id"],item["source_ref"])).fetchone()
                    if keyed:
                        row=db.execute("SELECT * FROM memories WHERE id=?",(keyed["memory_id"],)).fetchone()
                        if row:
                            out=_row(row); out["warnings"]=warnings; return out
                    legacy=db.execute("SELECT * FROM memories WHERE project_id=? AND source_ref=? ORDER BY created_at,id LIMIT 1",(project["id"],item["source_ref"])).fetchone()
                    if legacy:
                        db.execute("INSERT OR IGNORE INTO loop_session_summaries(project_id,source_ref,memory_id,created_at) VALUES(?,?,?,?)",(project["id"],item["source_ref"],legacy["id"],utc_now()))
                        row=db.execute("SELECT * FROM memories WHERE id=(SELECT memory_id FROM loop_session_summaries WHERE project_id=? AND source_ref=?)",(project["id"],item["source_ref"])).fetchone()
                        if row is None:
                            raise IntegrityError("loop session summary key is inconsistent")
                        out=_row(row); out["warnings"]=warnings; return out
                    self._insert_memory(db,item)
                    db.execute("INSERT INTO loop_session_summaries(project_id,source_ref,memory_id,created_at) VALUES(?,?,?,?)",(project["id"],item["source_ref"],item["id"],utc_now()))
                    out=_row(db.execute("SELECT * FROM memories WHERE id=?",(item["id"],)).fetchone())
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))
        out["warnings"]=warnings
        return out
    def update_memory(self,ref,**changes):
        old=self._memory(ref); allowed={"memory_type","title","body","importance","tags","source","related_files","related_commits","active","verified","source_ref","legacy_metadata"}; bad=set(changes)-allowed
        if bad: raise RouteCraftLocalError("unsupported memory field: "+", ".join(sorted(bad)))
        values={k:changes.get(k,old[k]) for k in allowed}
        if values["memory_type"] not in MEMORY_TYPES or values["importance"] not in IMPORTANCE_LEVELS: raise RouteCraftLocalError("invalid type or importance")
        warnings=[]
        for k in ("title","body","source"):
            values[k],found=sanitize_text(values[k]); warnings+=found
        if not isinstance(values["source_ref"],str) or len(values["source_ref"]) > 1000: raise RouteCraftLocalError("source_ref must be a string of at most 1000 characters")
        values["source_ref"],found=_normalize_source_ref(values["source_ref"]); warnings+=found
        if not values["title"].strip() or not values["body"].strip() or len(values["title"]) > 500 or len(values["body"]) > MAX_TEXT or len(values["source"]) > 500: raise RouteCraftLocalError("title/body/source is missing or too large")
        for k in ("tags","related_files","related_commits"):
            clean,found=sanitize_values(_string_list(values[k],k)); values[k]=json_value(clean); warnings+=found
        if not isinstance(values["legacy_metadata"], dict): raise RouteCraftLocalError("legacy_metadata must be an object")
        metadata, found = _safe_metadata(values["legacy_metadata"]); warnings+=found
        values["legacy_metadata"]=json.dumps(metadata,ensure_ascii=False); values["active"]=_strict_bool(values["active"],"active"); values["verified"]=_strict_bool(values["verified"],"verified"); values["content_hash"]=_hash(values["memory_type"],values["title"],values["body"]); values["updated_at"]=utc_now(); values["id"]=old["id"]
        with self.db.connect() as db: db.execute("UPDATE memories SET memory_type=:memory_type,title=:title,body=:body,importance=:importance,tags=:tags,source=:source,related_files=:related_files,related_commits=:related_commits,active=:active,verified=:verified,source_ref=:source_ref,content_hash=:content_hash,legacy_metadata=:legacy_metadata,updated_at=:updated_at WHERE id=:id",values); row=db.execute("SELECT * FROM memories WHERE id=?",(old["id"],)).fetchone(); self._save_fts(db,dict(row))
        out=self._memory(old["id"]); out["warnings"]=list(dict.fromkeys(warnings)); return out
    def delete_memory(self,ref,confirmation):
        item=self._memory(ref)
        if str(ref)!=item["id"] or confirmation!=item["id"]: raise ConfirmationRequiredError("delete_memory requires exact memory ID as confirmation")
        with self.db.connect() as db:
            try: db.execute("DELETE FROM memories_fts WHERE memory_id=?",(item["id"],))
            except __import__('sqlite3').OperationalError: pass
            db.execute("DELETE FROM memories WHERE id=?",(item["id"],))
        return {"deleted":item["id"]}
    def list_memories(self,project_ref=None,limit=100,offset=0,include_inactive=False,**filters):
        limit=max(0,min(int(limit),100000)); offset=max(0,int(offset)); sql="SELECT * FROM memories WHERE 1=1"; args=[]
        if project_ref: sql+=" AND project_id=?"; args.append(self._project(project_ref)["id"])
        if not include_inactive: sql+=" AND active=1"
        for key,column in (("memory_type","memory_type"),("importance","importance"),("verified","verified"),("active","active")):
            if key == "importance" and isinstance(filters.get(key), (list, tuple, set)): continue
            if filters.get(key) is not None: sql+=f" AND {column}=?"; args.append(_strict_bool(filters[key],key) if key in {"verified","active"} else filters[key])
        for key,column in (("types","memory_type"),("importance","importance")):
            plural = filters.get(key)
            if plural is not None and key == "types":
                values = _string_list(plural, key)
                if values: sql += f" AND {column} IN ({','.join('?' for _ in values)})"; args.extend(values)
            elif key == "importance" and isinstance(plural, (list,tuple,set)):
                values = _string_list(plural, key)
                if values: sql += f" AND {column} IN ({','.join('?' for _ in values)})"; args.extend(values)
        sql+=" ORDER BY created_at DESC,id LIMIT ? OFFSET ?"; args += [limit,offset]
        with self.db.connect() as db:return [_row(r) for r in db.execute(sql,args)]
    def _all_memories(self,project_ref=None):
        sql="SELECT * FROM memories"; args=[]
        if project_ref is not None: sql+=" WHERE project_id=?"; args.append(self._project(project_ref)["id"])
        sql+=" ORDER BY created_at DESC,id"
        with self.db.connect() as db: return [_row(row) for row in db.execute(sql,args)]
    def search_memories(self,project_ref=None,query='',types=(),tags=(),importance=(),created_from=None,created_to=None,filename=None,commit=None,active=PACKAGE_MISSING,verified=None,limit=50):
        q=str(query).casefold().strip(); wanted_types=set(types); wanted_tags={str(x).casefold() for x in tags}; wanted_importance=set(importance); out=[]
        where=["1=1"]; base_args=[]
        if project_ref: where.append("m.project_id=?"); base_args.append(self._project(project_ref)["id"])
        if active is PACKAGE_MISSING: where.append("m.active=1")
        elif active is not None: where.append("m.active=?"); base_args.append(_strict_bool(active,"active"))
        if verified is not None: where.append("m.verified=?"); base_args.append(_strict_bool(verified,"verified"))
        if wanted_types: where.append(f"m.memory_type IN ({','.join('?' for _ in wanted_types)})"); base_args.extend(sorted(wanted_types))
        if wanted_importance: where.append(f"m.importance IN ({','.join('?' for _ in wanted_importance)})"); base_args.extend(sorted(wanted_importance))
        if created_from: where.append("m.created_at>=?"); base_args.append(str(created_from))
        if created_to: where.append("m.created_at<=?"); base_args.append(str(created_to))
        candidates={}; fts_ranks={}; fts_expression=_fts_query(q) if q else ""
        if fts_expression and self._fts():
            sql="SELECT m.*,bm25(memories_fts) AS fts_rank FROM memories_fts JOIN memories m ON m.id=memories_fts.memory_id WHERE memories_fts MATCH ? AND "+" AND ".join(where)  # routecraft-security: allowlisted-sql-shape
            with self.db.connect() as db:
                for record in db.execute(sql,[fts_expression,*base_args]):
                    item=_row(record); fts_ranks[item["id"]]=float(record["fts_rank"]); candidates[item["id"]]=item
        fallback_where=list(where); fallback_args=list(base_args)
        if q:
            terms=[term for term in q.split() if term] or [q]; term_clauses=[]
            for term in terms:
                term_clauses.append("(instr(lower(m.title),lower(?))>0 OR instr(lower(m.body),lower(?))>0 OR instr(lower(m.tags),lower(?))>0)"); fallback_args.extend((term,term,term))
            fallback_where.append(" AND ".join(term_clauses))
        sql="SELECT m.* FROM memories m WHERE "+" AND ".join(fallback_where)  # routecraft-security: allowlisted-sql-shape
        with self.db.connect() as db:
            for record in db.execute(sql,fallback_args):
                item=_row(record); candidates.setdefault(item["id"],item)
        rows=list(candidates.values())
        for item in rows:
            if filename and not any(str(filename).casefold() in x.casefold() for x in item["related_files"]): continue
            if commit and not any(str(commit).casefold() in x.casefold() for x in item["related_commits"]): continue
            tagset={x.casefold() for x in item["tags"]}
            if wanted_tags and not wanted_tags <= tagset: continue
            text=(item["title"]+"\n"+item["body"]+"\n"+" ".join(item["tags"])).casefold()
            if q and item["id"] not in fts_ranks and q not in text and not all(term in text for term in q.split()): continue
            relevance=(10 if q and q in item["title"].casefold() else 0)+(4 if q and q in " ".join(item["tags"]).casefold() else 0)+(5 if item["id"] in fts_ranks else 0)+(3 if q else 0)+{"high":3,"medium":2,"low":1}[item["importance"]]+(1 if item["verified"] else 0)
            item["relevance"]=relevance; out.append(item)
        return sorted(out,key=lambda x:(x["relevance"],x["created_at"]),reverse=True)[:max(0,min(int(limit),1000))]
    def _parse_markdown(self,path):
        try: text=Path(path).read_text(encoding="utf-8-sig").replace("\r\n","\n")
        except (OSError, UnicodeError) as exc: raise RouteCraftLocalError("could not read UTF-8 Markdown import") from exc
        title=Path(path).stem; body=text
        if text.startswith("---\n") and "\n---\n" in text[4:]:
            raw,body=text[4:].split("\n---\n",1); meta={}
            for line in raw.splitlines():
                if ":" in line:
                    k,v=line.split(":",1)
                    try: meta[k.strip()]=json.loads(v.strip())
                    except ValueError: meta[k.strip()]=v.strip().strip("\"'")
            title=str(meta.get("title") or title); return title,body.strip(),meta
        return title,body.strip(),{}
    def import_file(self,project_ref,path,format='auto'):
        project=self._project(project_ref); self.initialize(); source=Path(path)
        if not source.is_file() or is_excluded_path(source,self.get_settings()): raise RouteCraftLocalError("import path is excluded or not a regular file")
        if source.stat().st_size > MAX_IMPORT_BYTES: raise RouteCraftLocalError("import file is too large")
        try: raw=source.read_text(encoding="utf-8-sig").replace("\r\n","\n")
        except (OSError, UnicodeError) as exc: raise RouteCraftLocalError("could not read UTF-8 import file") from exc
        fmt=(format if format!='auto' else ("jsonl" if source.suffix.lower()==".jsonl" else "json" if source.suffix.lower()==".json" else "markdown")).lower(); records=[]
        if fmt=="markdown":
            title,body,meta=self._parse_markdown(source); records=[{"title":title,"body":body,"memory_type":meta.get("memory_type","note"),"tags":meta.get("tags",[]),"legacy_metadata":meta}]
        elif fmt=="json":
            try: parsed=json.loads(raw)
            except json.JSONDecodeError as exc: raise RouteCraftLocalError(f"invalid JSON import at line {exc.lineno}") from exc
            records=parsed.get("memories",[]) if isinstance(parsed,dict) else parsed
        elif fmt=="jsonl":
            records=[]
            for number,line in enumerate(raw.splitlines(),start=1):
                if not line.strip(): continue
                try: records.append(json.loads(line))
                except json.JSONDecodeError as exc: raise RouteCraftLocalError(f"invalid JSONL import at line {number}") from exc
        else: raise RouteCraftLocalError("unsupported import format")
        if not isinstance(records,list): raise RouteCraftLocalError("import payload must contain a list")
        # Prepare the entire input before opening one write transaction.
        prepared=[]; seen_ids=set(); seen_refs=set()
        for number,record in enumerate(records,start=1):
            if not isinstance(record,dict): raise RouteCraftLocalError("import record must be an object")
            memory_type=record.get("memory_type",record.get("type","note")); title=record.get("title","Imported memory"); body=record.get("body",record.get("content","")); importance=record.get("importance","medium")
            memory_id=record.get("id"); source_ref=record.get("source_ref")
            if memory_type not in MEMORY_TYPES or importance not in IMPORTANCE_LEVELS or not isinstance(title,str) or not isinstance(body,str): raise RouteCraftLocalError(f"invalid import record at line {number}")
            try:
                item,row_warnings=self._prepare_memory(project["id"],memory_type,title,body,importance=importance,tags=record.get("tags",()),source=record.get("source","import"),related_files=record.get("related_files",()),related_commits=record.get("related_commits",()),active=record.get("active",True),verified=record.get("verified",False),memory_id=memory_id,source_ref=source_ref,legacy_metadata=record.get("legacy_metadata",{}))
            except RouteCraftLocalError as exc:
                raise RouteCraftLocalError(f"invalid import record at line {number}: {exc}") from exc
            if item["id"] in seen_ids: raise RouteCraftLocalError(f"duplicate memory ID at line {number}")
            if item["source_ref"] and item["source_ref"] in seen_refs: raise RouteCraftLocalError(f"duplicate source reference at line {number}")
            seen_ids.add(item["id"])
            if item["source_ref"]: seen_refs.add(item["source_ref"])
            prepared.append((number,item,row_warnings))
        created=[]; skipped=[]; conflicts=[]; warnings=[]
        try:
            with self.db.connect() as db:
                for _,item,row_warnings in prepared:
                    existing=db.execute("SELECT id,project_id,content_hash FROM memories WHERE id=?",(item["id"],)).fetchone()
                    if existing is None and item["source_ref"]: existing=db.execute("SELECT id,project_id,content_hash FROM memories WHERE project_id=? AND source_ref=?",(project["id"],item["source_ref"])).fetchone()
                    if existing:
                        if existing["project_id"] == project["id"] and existing["content_hash"] == item["content_hash"]: skipped.append(existing["id"]); continue
                        self._insert_conflict(db,"import_memory",item["id"] or item["source_ref"],existing["id"],"import memory differs from durable record",project_id=project["id"],source_ref=item["source_ref"],existing_hash=existing["content_hash"],incoming_hash=item["content_hash"]); conflicts.append(item["id"] or item["source_ref"]); continue
                    self._insert_memory(db,item); created.append(item["id"]); warnings+=row_warnings
        except Exception as exc:
            if "UNIQUE" in str(exc): raise ConflictError("memory ID already exists") from exc
            raise
        return {"format":fmt,"created":created,"skipped":skipped,"conflicts":conflicts,"warnings":list(dict.fromkeys(warnings))}
    def import_routecraft_store(self,project_ref,path):
        project=self._project(project_ref); root=Path(path); sentinel=root/".routecraft-store.json"
        if sentinel.is_symlink() or not sentinel.is_file(): raise RouteCraftLocalError("not a regular RouteCraft Markdown-store sentinel")
        try: sentinel_data=json.loads(sentinel.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc: raise RouteCraftLocalError("invalid RouteCraft Markdown-store sentinel") from exc
        if not isinstance(sentinel_data,dict) or sentinel_data.get("schema_version") != 1: raise RouteCraftLocalError("unsupported RouteCraft Markdown-store schema")
        created=[]; skipped=[]; conflicts=[]
        for folder,legacy_kind in (("rules","rule"),("cases","case"),("candidates","candidate")):
            directory=root/folder
            if not directory.exists(): continue
            if directory.is_symlink() or not directory.is_dir(): raise RouteCraftLocalError(f"invalid legacy payload directory: {folder}")
            for file in sorted(directory.iterdir()):
                if file.name == ".gitkeep": continue
                if file.is_symlink() or not file.is_file() or file.suffix.lower() != ".md": raise RouteCraftLocalError(f"invalid legacy payload: {file.name}")
                title,body,meta=self._parse_markdown(file); legacy,_=_normalize_source_ref(meta.get("id") or file.stem)
                detected={"rule":"decision","candidate":"note"}.get(legacy_kind) or _legacy_case_type(meta,body)
                safe_title,_=sanitize_text(title); safe_body,_=sanitize_text(body); incoming_hash=_hash(detected,safe_title,safe_body)
                with self.db.connect() as db: exists=db.execute("SELECT id,content_hash FROM memories WHERE project_id=? AND source_ref=?",(project["id"],legacy)).fetchone()
                if exists:
                    if exists["content_hash"] == incoming_hash: skipped.append(legacy); continue
                    self._conflict("legacy_source_changed",legacy,exists["id"],"legacy source changed",project_id=project["id"],source_ref=legacy,existing_hash=exists["content_hash"],incoming_hash=incoming_hash); conflicts.append(legacy); continue
                verified=legacy_kind=="case" or (legacy_kind=="rule" and str(meta.get("status") or "").strip().casefold()=="validated")
                item=self.add_memory(project_ref,detected,title,body,importance="high" if legacy_kind=="rule" else "medium",tags=meta.get("tags",()),source="routecraft-store",source_ref=legacy,verified=verified,legacy_metadata=meta); created.append(item["id"])
        return {"created":created,"skipped":skipped,"conflicts":conflicts,"source":str(root)}
    def _insert_conflict(self,db,kind,incoming,existing,detail,*,project_id=None,source_ref=None,existing_hash=None,incoming_hash=None):
        incoming,_=sanitize_text(str(incoming)); detail,_=sanitize_text(str(detail)); source_ref,_=sanitize_text(str(source_ref or ""))
        db.execute("INSERT INTO import_conflicts(kind,incoming_id,existing_id,project_id,source_ref,existing_hash,incoming_hash,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(kind,incoming,existing,project_id,source_ref,existing_hash,incoming_hash,detail,utc_now()))
    def _conflict(self,kind,incoming,existing,detail,*,project_id=None,source_ref=None,existing_hash=None,incoming_hash=None):
        with self.db.connect() as db: self._insert_conflict(db,kind,incoming,existing,detail,project_id=project_id,source_ref=source_ref,existing_hash=existing_hash,incoming_hash=incoming_hash)
    def export_memories(self,project_ref=None,fmt='jsonl',output=None,safe=False):
        records=self._all_memories(project_ref); safe_records=[]; warnings=[]; id_maps={"MEM":{},"PRJ":{}}
        repositories={project["id"]:project.get("repo_path","") for project in self.list_projects(include_archived=True)} if safe else {}
        for item in records:
            if safe:
                known_repo=repositories.get(item.get("project_id"),"")
                item["id"],f=_portable_identifier(item.get("id"),"MEM",id_maps["MEM"]); warnings+=f
                item["project_id"],f=_portable_identifier(item.get("project_id"),"PRJ",id_maps["PRJ"]); warnings+=f
                for key in ("title","body","source"):
                    item[key],f=sanitize_text(_portable_text(item[key],known_repo)); warnings+=f
                item["source_ref"],f=_portable_source_ref(item["source_ref"],known_repo); warnings+=f
                item["tags"],f=sanitize_values([_portable_path(v) for v in item["tags"]]); warnings+=f
                item["related_files"],f=sanitize_values([_portable_path(v) for v in item["related_files"]]); warnings+=f
                item["related_commits"],f=sanitize_values([_portable_path(v) for v in item["related_commits"]]); warnings+=f
                item["legacy_metadata"]={}
            safe_records.append(item)
        fmt=fmt.lower();
        if fmt=="json": text=json.dumps({"schema_version":1,"memories":safe_records},ensure_ascii=False,indent=2)+"\n"
        elif fmt=="jsonl": text="".join(json.dumps(v,ensure_ascii=False)+"\n" for v in safe_records)
        elif fmt=="markdown": text="\n\n".join(f"# {v['title']}\n\n{v['body']}" for v in safe_records)+("\n" if safe_records else "")
        else: raise RouteCraftLocalError("unsupported export format")
        target=Path(output).expanduser() if output else self.data_dir/f"routecraft-memories-{uuid.uuid4().hex}.{ 'md' if fmt=='markdown' else fmt}"
        if target.exists(): raise ConflictError("export target already exists")
        target.parent.mkdir(parents=True,exist_ok=True); target.write_text(text,encoding="utf-8",newline="\n")
        return {"output":str(target.resolve()),"format":fmt,"count":len(records),"warnings":list(dict.fromkeys(warnings))}
    def export_project_package(self,project_ref,output,as_zip=True):
        project=self._project(project_ref); target=Path(output).expanduser(); target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists(): raise ConflictError("export target already exists")
        safe_project={**project}; id_maps={"MEM":{},"PRJ":{}}; warnings=[]
        safe_project["id"],f=_portable_identifier(project.get("id"),"PRJ",id_maps["PRJ"]); warnings+=f
        for key in ("name","description","current_objective"):
            safe_project[key],_=sanitize_text(_portable_text(safe_project[key],project.get("repo_path","")))
        safe_project["repo_path"]="<REPO_PATH>"
        safe_project["git_remote_url"],_=_safe_remote(safe_project["git_remote_url"])
        safe_project["ai_agents"],_=sanitize_values([_portable_path(v) for v in _string_list(safe_project["ai_agents"],"ai_agents")]); safe_project["languages"],_=sanitize_values([_portable_path(v) for v in _string_list(safe_project["languages"],"languages")]); safe_project["tags"],_=sanitize_values([_portable_path(v) for v in _string_list(safe_project["tags"],"tags")])
        payload={"schema_version":1,"project":safe_project,"memories":self._all_memories(project["id"])}
        for mem in payload["memories"]:
            mem["id"],f=_portable_identifier(mem.get("id"),"MEM",id_maps["MEM"]); warnings+=f
            mem["project_id"]=safe_project["id"]
            for key in ("title","body","source"): mem[key],_=sanitize_text(_portable_text(mem[key],project.get("repo_path","")))
            mem["source_ref"],f=_portable_source_ref(mem["source_ref"],project.get("repo_path","")); warnings+=f
            mem["tags"],_=sanitize_values([_portable_path(v) for v in mem["tags"]])
            mem["related_files"],_=sanitize_values([_portable_path(v) for v in mem["related_files"]])
            mem["related_commits"],_=sanitize_values([_portable_path(v) for v in mem["related_commits"]])
            mem["legacy_metadata"]={}
        data=json.dumps(payload,ensure_ascii=False,indent=2)+"\n"
        if as_zip:
            with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as archive: archive.writestr("manifest.json",json.dumps({"schema_version":1,"kind":"routecraft-project-package","project_id":safe_project["id"]},ensure_ascii=False)); archive.writestr("project.json",data)
        else: target.write_text(data,encoding="utf-8",newline="\n")
        return {"output":str(target.resolve()),"project_id":safe_project["id"],"count":len(payload["memories"]),"zip":bool(as_zip),"warnings":list(dict.fromkeys(warnings))}
    def import_project_package(self,path,conflict='detect'):
        source=Path(path)
        if conflict not in {"detect","skip"}: raise RouteCraftLocalError("conflict must be detect or skip")
        if not source.is_file() or source.stat().st_size > MAX_IMPORT_BYTES: raise IntegrityError("project package is missing or too large")
        try:
            if source.suffix.lower()==".zip":
                with zipfile.ZipFile(source) as z:
                    if any(p.startswith("/") or ".." in Path(p).parts for p in z.namelist()) or set(z.namelist())!={"manifest.json","project.json"}: raise IntegrityError("unsafe project package")
                    if any(info.file_size > MAX_IMPORT_BYTES for info in z.infolist()) or sum(info.file_size for info in z.infolist()) > MAX_IMPORT_BYTES: raise IntegrityError("project package expands beyond the size limit")
                    manifest=json.loads(z.read("manifest.json")); payload=json.loads(z.read("project.json"))
            else: payload=json.loads(source.read_text(encoding="utf-8-sig")); manifest={"schema_version":1}
        except IntegrityError: raise
        except (OSError, UnicodeError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc: raise IntegrityError("invalid project package data") from exc
        if not isinstance(manifest,dict) or manifest.get("schema_version")!=1 or (source.suffix.lower()==".zip" and manifest.get("kind")!="routecraft-project-package") or not isinstance(payload,dict) or payload.get("schema_version")!=1 or not isinstance(payload.get("project"),dict): raise IntegrityError("invalid project package")
        project=payload["project"]; records=payload.get("memories")
        if not isinstance(records,list): raise IntegrityError("project package memories must be a list")
        if not _safe_identifier(project.get("id")): raise IntegrityError("project package has an invalid project ID")
        if source.suffix.lower()==".zip" and manifest.get("project_id") != project["id"]: raise IntegrityError("project package manifest ID mismatch")
        if not isinstance(project.get("name"),str): raise IntegrityError("invalid project package project fields")
        for field in ("repo_path","git_remote_url","description","current_objective"):
            if field in project and not isinstance(project[field],str): raise IntegrityError("invalid project package project fields")
        try:
            project_item,_=self._prepare_project(
                project.get("name"), repo_path=project.get("repo_path",""),
                git_remote_url=project.get("git_remote_url",""),
                ai_agents=project.get("ai_agents",()), languages=project.get("languages",()),
                tags=project.get("tags",()), description=project.get("description",""),
                current_objective=project.get("current_objective",""), project_id=project.get("id"),
            )
            project_item["archived"]=_package_bool(project.get("archived",PACKAGE_MISSING),project_item["archived"],"project.archived")
            project_item["created_at"]=_package_timestamp(project.get("created_at",PACKAGE_MISSING),project_item["created_at"],"project.created_at")
            project_item["updated_at"]=_package_timestamp(project.get("updated_at",PACKAGE_MISSING),project_item["updated_at"],"project.updated_at")
            if project_item["updated_at"] < project_item["created_at"]: raise RouteCraftLocalError("project.updated_at must not precede project.created_at")
        except RouteCraftLocalError as exc:
            raise IntegrityError("invalid project package project fields") from exc
        prepared=[]
        for number,mem in enumerate(records,start=1):
            if not isinstance(mem,dict): raise IntegrityError(f"invalid project package memory {number}")
            if not _safe_identifier(mem.get("id")): raise IntegrityError(f"invalid project package memory ID {number}")
            if not isinstance(mem.get("title"),str) or not isinstance(mem.get("body"),str): raise IntegrityError(f"invalid project package memory {number}")
            try:
                item,_=self._prepare_memory(
                    project_item["id"], mem.get("memory_type"), mem.get("title"), mem.get("body"),
                    importance=mem.get("importance"), tags=mem.get("tags",()), source=mem.get("source","package"),
                    related_files=mem.get("related_files",()), related_commits=mem.get("related_commits",()),
                    active=mem.get("active",True), verified=mem.get("verified",False), memory_id=mem.get("id"),
                    source_ref=mem.get("source_ref"), legacy_metadata=mem.get("legacy_metadata",{}),
                )
                item["active"]=_package_bool(mem.get("active",PACKAGE_MISSING),item["active"],f"memory {number}.active")
                item["verified"]=_package_bool(mem.get("verified",PACKAGE_MISSING),item["verified"],f"memory {number}.verified")
                item["created_at"]=_package_timestamp(mem.get("created_at",PACKAGE_MISSING),item["created_at"],f"memory {number}.created_at")
                item["updated_at"]=_package_timestamp(mem.get("updated_at",PACKAGE_MISSING),item["updated_at"],f"memory {number}.updated_at")
                if item["updated_at"] < item["created_at"]: raise RouteCraftLocalError(f"memory {number}.updated_at must not precede created_at")
            except RouteCraftLocalError as exc:
                raise IntegrityError(f"invalid project package memory {number}") from exc
            prepared.append(item)
        self.initialize()
        try:
            with self.db.connect() as db:
                existing=db.execute("SELECT * FROM projects WHERE id=?",(project_item["id"],)).fetchone()
                if existing and conflict=="detect":
                    self._insert_conflict(db,"project_id",project_item["id"],existing["id"],"package project ID already exists",project_id=existing["id"])
                    outcome={"imported":False,"conflict":project_item["id"]}
                else:
                    if not existing:
                        db.execute("INSERT INTO projects VALUES(:id,:name,:repo_path,:git_remote_url,:ai_agents,:languages,:tags,:description,:current_objective,:archived,:created_at,:updated_at)",project_item)
                        project_id=project_item["id"]
                    else:
                        project_id=existing["id"]
                    created=[]; skipped=[]; conflicts=[]
                    for item in prepared:
                        found=db.execute("SELECT id,project_id,content_hash FROM memories WHERE id=?",(item["id"],)).fetchone()
                        if found is None and item["source_ref"]:
                            found=db.execute("SELECT id,project_id,content_hash FROM memories WHERE project_id=? AND source_ref=? ORDER BY created_at,id LIMIT 1",(project_id,item["source_ref"])).fetchone()
                        if found:
                            if found["project_id"] == project_id and found["content_hash"] == item["content_hash"]:
                                skipped.append(found["id"]); continue
                            self._insert_conflict(db,"package_memory",item["id"],found["id"],"package memory differs from durable record",project_id=project_id,source_ref=item["source_ref"],existing_hash=found["content_hash"],incoming_hash=item["content_hash"])
                            conflicts.append(item["id"]); continue
                        self._insert_memory(db,item); created.append(item["id"])
                    outcome={"imported":True,"project_id":project_id,"created":created,"skipped":skipped,"conflicts":conflicts}
        except __import__('sqlite3').IntegrityError as exc:
            raise ConflictError("project package conflicts with durable data") from exc
        return outcome
    def backup(self,output=None):
        self.initialize(); self.db.integrity(); target=Path(output).expanduser() if output else self.data_dir/f"routecraft-backup-{utc_now().replace(':','')}-{uuid.uuid4().hex[:8]}.zip"; target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists(): raise ConflictError("backup target already exists")
        with tempfile.TemporaryDirectory(dir=self.data_dir) as temp:
            copy=Path(temp)/"routecraft-local.sqlite3"
            with self.db.connect() as source:
                dest=__import__('sqlite3').connect(copy)
                try: source.backup(dest); dest.commit()
                finally: dest.close()
            digest=hashlib.sha256(copy.read_bytes()).hexdigest(); manifest={"schema_version":1,"kind":"routecraft-local-backup","created_at":utc_now(),"database":"routecraft-local.sqlite3","sha256":digest}
            with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as z: z.writestr("manifest.json",json.dumps(manifest,ensure_ascii=False)); z.write(copy,"routecraft-local.sqlite3")
        return {"output":str(target.resolve()),"manifest":manifest}
    def restore(self,archive,confirmation):
        if confirmation!="RESTORE": raise ConfirmationRequiredError("restore requires exact confirmation RESTORE")
        source=Path(archive)
        if not source.is_file(): raise IntegrityError("backup archive does not exist")
        self.initialize(); cleanup_warning=None; retained_rollback=None
        with tempfile.NamedTemporaryFile(prefix=".routecraft-restore-",suffix=".sqlite3",dir=self.data_dir,delete=False) as handle:
            temp=Path(handle.name)
        try:
            with zipfile.ZipFile(source) as z:
                if set(z.namelist())!={"manifest.json","routecraft-local.sqlite3"} or any(p.startswith("/") or ".." in Path(p).parts for p in z.namelist()): raise IntegrityError("unsafe backup archive")
                manifest_info=z.getinfo("manifest.json"); database_info=z.getinfo("routecraft-local.sqlite3")
                if manifest_info.file_size > MAX_MANIFEST_BYTES or database_info.file_size > MAX_BACKUP_BYTES: raise IntegrityError("backup archive expands beyond the size limit")
                manifest=json.loads(z.read("manifest.json"))
                digest=hashlib.sha256(); written=0
                with z.open(database_info) as incoming, temp.open("wb") as output:
                    while True:
                        chunk=incoming.read(1024*1024)
                        if not chunk: break
                        written+=len(chunk)
                        if written > MAX_BACKUP_BYTES: raise IntegrityError("backup database exceeds the size limit")
                        digest.update(chunk); output.write(chunk)
            if not isinstance(manifest,dict) or manifest.get("kind")!="routecraft-local-backup" or manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("database") != "routecraft-local.sqlite3" or digest.hexdigest()!=manifest.get("sha256"): raise IntegrityError("backup manifest validation failed")
            probe=__import__('sqlite3').connect(temp)
            try:
                result=probe.execute("PRAGMA integrity_check").fetchone()[0]
                version=probe.execute("PRAGMA user_version").fetchone()[0]
                tables={row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required_columns={
                    "projects":{"id","name","repo_path","git_remote_url","ai_agents","languages","tags","description","current_objective","archived","created_at","updated_at"},
                    "memories":{"id","project_id","memory_type","title","body","importance","tags","source","related_files","related_commits","active","verified","source_ref","content_hash","legacy_metadata","created_at","updated_at"},
                    "import_conflicts":{"id","kind","incoming_id","existing_id","project_id","source_ref","existing_hash","incoming_hash","detail","created_at","resolved"},
                    "settings":{"key","value","updated_at"},
                }
                columns_ok=all(table in tables and columns <= {row[1] for row in probe.execute(f"PRAGMA table_info({table})")} for table,columns in required_columns.items())
                foreign_key_error=probe.execute("PRAGMA foreign_key_check").fetchone()
                if result!="ok" or version!=SCHEMA_VERSION or not columns_ok or foreign_key_error is not None: raise IntegrityError("restored database failed integrity/schema check")
                try:
                    probe.execute("BEGIN IMMEDIATE")
                    self.db._create(probe)
                    for key,value in DEFAULT_SETTINGS.items(): probe.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",(key,json.dumps(value,ensure_ascii=False),utc_now()))
                    probe.rollback()
                except sqlite3.DatabaseError as exc:
                    probe.rollback(); raise IntegrityError("restored database failed initialization preflight") from exc
            finally: probe.close()
            with self.db.operation_lock():
                pre=self.backup()
                with tempfile.NamedTemporaryFile(prefix=".routecraft-rollback-",suffix=".sqlite3",dir=self.data_dir,delete=False) as handle:
                    rollback=Path(handle.name)
                    with zipfile.ZipFile(pre["output"]) as archive, archive.open("routecraft-local.sqlite3") as previous:
                        while True:
                            chunk=previous.read(1024*1024)
                            if not chunk: break
                            handle.write(chunk)
                candidate_installed=False; preserve_rollback=False
                try:
                    os.replace(temp,self.db.path); candidate_installed=True; self._ready=False; self.initialize()
                except Exception as activation_exc:
                    self._ready=False
                    if candidate_installed:
                        try:
                            os.replace(rollback,self.db.path); self.initialize()
                        except Exception as rollback_exc:
                            preserve_rollback=rollback.exists()
                            retained=f"; raw rollback: {rollback}" if preserve_rollback else ""
                            raise IntegrityError(f"restore activation and automatic rollback failed; recovery backup retained at {pre['output']}{retained}") from rollback_exc
                    raise IntegrityError("restore activation failed and the previous database was retained") from activation_exc
                finally:
                    if rollback.exists() and not preserve_rollback:
                        try: rollback.unlink()
                        except OSError:
                            retained_rollback=str(rollback.resolve())
                            cleanup_warning="restore succeeded but the temporary rollback database could not be removed"
        except IntegrityError as exc:
            if temp.exists():
                try: temp.unlink()
                except OSError: raise IntegrityError(f"{exc}; temporary restore candidate retained at {temp.resolve()}") from exc
            raise
        except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
            if temp.exists():
                try: temp.unlink()
                except OSError: raise IntegrityError(f"invalid backup archive; temporary restore candidate retained at {temp.resolve()}") from exc
            raise IntegrityError("invalid backup archive") from exc
        result={"restored":str(source.resolve()),"pre_restore_backup":pre["output"]}
        if cleanup_warning: result.update({"warnings":[cleanup_warning],"retained_rollback":retained_rollback})
        return result
