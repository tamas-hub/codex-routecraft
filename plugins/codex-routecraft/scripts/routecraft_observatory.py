#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, platform, subprocess, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

def run(repo: Path, *args: str) -> str:
    p = subprocess.run(["git","-C",str(repo),*args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False)
    return p.stdout.strip() if p.returncode == 0 else ""

def git_state(repo: Path) -> dict:
    if not (repo/".git").exists():
        return {"present":False,"clean":False,"in_sync":False,"conflicts":[]}
    head=run(repo,"rev-parse","HEAD")
    upstream=run(repo,"rev-parse","@{u}")
    dirty=bool(run(repo,"status","--porcelain"))
    conflicts=[x for x in run(repo,"diff","--name-only","--diff-filter=U").splitlines() if x]
    ahead=run(repo,"rev-list","--count","@{u}..HEAD") if upstream else ""
    behind=run(repo,"rev-list","--count","HEAD..@{u}") if upstream else ""
    return {"present":True,"head":head,"upstream_head":upstream,"clean":not dirty,"conflicts":conflicts,"ahead":int(ahead or 0),"behind":int(behind or 0),"in_sync":bool(head and upstream and head==upstream and not dirty and not conflicts)}

def load_json(path: Path) -> dict:
    try:return json.loads(path.read_text(encoding="utf-8"))
    except Exception:return {}

def file_hash(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(65536),b""): h.update(chunk)
    return h.hexdigest()

def agents_ok(source: Path, codex_home: Path) -> bool:
    src=source/"plugins/codex-routecraft/agents"; dst=codex_home/"agents"
    names=["routecraft_luna_low.toml","routecraft_luna_medium.toml","routecraft_luna_max.toml","routecraft_terra_medium.toml","routecraft_terra_high.toml","routecraft_sol_reviewer.toml"]
    return all((src/n).is_file() and (dst/n).is_file() and file_hash(src/n)==file_hash(dst/n) for n in names)

def memory_status(source: Path, memory: Path, env: dict) -> dict:
    cli=source/"plugins/codex-routecraft/scripts/routecraft_memory.py"
    out={}
    if cli.is_file():
        p=subprocess.run([sys.executable,str(cli),"status","--store",str(memory),"--json"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",errors="replace",env=env,check=False)
        if p.returncode==0:
            try: out=json.loads(p.stdout)
            except Exception: pass
    g=git_state(memory)
    counts=(out.get("counts") or {})
    return {**g,"counts":{"case":int(counts.get("case",0)),"candidate":int(counts.get("candidate",0)),"rule":int(counts.get("rule",0))},"eligible_candidates":len(out.get("eligible_candidates") or []),"last_sync_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z")}

def evaluation_status(source: Path, env: dict) -> dict:
    evaluator=source/"plugins/codex-routecraft/scripts/routecraft_evaluation.py"
    if not evaluator.is_file():
        return {"available":False,"enabled":False}
    p=subprocess.run([sys.executable,str(evaluator),"summary","--json","--compact"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",errors="replace",env=env,check=False)
    if p.returncode!=0:
        return {"available":True,"enabled":False,"status":"error"}
    try:
        out=json.loads(p.stdout)
    except Exception:
        return {"available":True,"enabled":False,"status":"error"}
    if not isinstance(out,dict):
        return {"available":True,"enabled":False,"status":"error"}
    # Compact evaluator output is deliberately path/query/prompt free. Do not
    # forward local event files or repository names to the monitoring server.
    return {"available":True,**out}

def build_payload(alias: str|None=None) -> dict:
    home=Path.home()
    source=home/"codex-routecraft"; memory=home/"routecraft-memory"; codex_home=Path(os.environ.get("CODEX_HOME",home/".codex"))
    env=os.environ.copy()
    memcfg=load_json(codex_home/"routecraft/memory.json")
    devcfg=load_json(codex_home/"routecraft/device.json")
    device_id=str(memcfg.get("device_id") or devcfg.get("device_id") or platform.node()).lower()
    device_id="".join(c for c in device_id if c.isalnum() or c in "._-")[:32] or "device"
    manifest=load_json(source/"plugins/codex-routecraft/.codex-plugin/plugin.json")
    version=str(manifest.get("version") or "unknown")
    src=git_state(source)
    mem=memory_status(source,memory,env)
    evaluation=evaluation_status(source,env)
    return {
      "schema_version":2,
      "observed_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
      "device_id":device_id,
      "alias":alias or str(devcfg.get("alias") or device_id),
      "os":platform.system(),
      "source":src,
      "memory":mem,
      "evaluation":evaluation,
      "routecraft":{
        "version":version,
        "agents_ok":agents_ok(source,codex_home),
        "plugin_cache_ok":(codex_home/"plugins/cache/routecraft/codex-routecraft"/version).is_dir()
      }
    }

def send(endpoint: str, token: str, payload: dict) -> dict:
    body=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode("utf-8")
    req=urllib.request.Request(endpoint,data=body,headers={"Content-Type":"application/json","Authorization":"Bearer "+token,"User-Agent":"RouteCraft-Observatory/2"},method="POST")
    with urllib.request.urlopen(req,timeout=15) as r:return json.loads(r.read().decode("utf-8"))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--endpoint")
    ap.add_argument("--token-file")
    ap.add_argument("--alias")
    ap.add_argument("--print",action="store_true",dest="print_only")
    a=ap.parse_args()
    p=build_payload(a.alias)
    if a.print_only or not a.endpoint:
        print(json.dumps(p,ensure_ascii=False,indent=2)); return
    if not a.token_file: raise SystemExit("--token-file is required with --endpoint")
    token=Path(a.token_file).expanduser().read_text(encoding="utf-8").strip()
    if len(token)<32: raise SystemExit("token is too short")
    print(json.dumps(send(a.endpoint,token,p),ensure_ascii=False))
if __name__=="__main__": main()
