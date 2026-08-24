#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, platform, re, subprocess, sys, urllib.error, urllib.request
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


class DeliveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


SAFE_SERVER_ERROR = re.compile(r"^[A-Za-z0-9_. -]{1,160}$")
SENSITIVE_ERROR_DETAIL = re.compile(r"\b(?:authorization|bearer|credential|password|secret|token)\b", re.IGNORECASE)


def read_token(path_value: str | None, destination: str) -> str:
    if not path_value:
        raise DeliveryError("configuration_error", f"{destination} token file is not configured")
    try:
        token = Path(path_value).expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DeliveryError("configuration_error", f"{destination} token file is unavailable") from exc
    if len(token) < 32:
        raise DeliveryError("configuration_error", f"{destination} token is invalid")
    return token


def safe_http_detail(exc: urllib.error.HTTPError) -> str | None:
    try:
        body = exc.read(4096).decode("utf-8", errors="replace")
        parsed = json.loads(body)
    except Exception:
        return None
    finally:
        try:
            exc.close()
        except Exception:
            pass
    detail = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(detail, str) and SAFE_SERVER_ERROR.fullmatch(detail) and not SENSITIVE_ERROR_DETAIL.search(detail):
        return detail
    return None


def failure_result(destination: str, exc: Exception) -> dict:
    result = {"ok": False, "error": f"{destination} upload failed", "code": "unexpected_error"}
    try:
        if isinstance(exc, DeliveryError):
            result["code"] = exc.code
            result["detail"] = str(exc)
            if exc.http_status is not None:
                result["http_status"] = exc.http_status
            return result
        if isinstance(exc, urllib.error.HTTPError):
            result["code"] = "http_error"
            result["http_status"] = int(exc.code)
            detail = safe_http_detail(exc)
            if detail:
                result["detail"] = detail
            return result
        if isinstance(exc, (urllib.error.URLError, TimeoutError)):
            result["code"] = "network_error"
            return result
        if isinstance(exc, (json.JSONDecodeError, UnicodeError)):
            result["code"] = "invalid_response"
            return result
    except Exception:
        return result
    return result

def send_telemetry(args: argparse.Namespace) -> dict:
    # The Control Center is an optional product boundary.  Its disabled or
    # unset configuration is a supported local-only state: never invoke either
    # the v3 adapter or the former v2 telemetry process in that state.  The
    # legacy heartbeat below remains governed by its separate setting.
    if os.environ.get("CONTROL_CENTER_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "delivered": False, "state": "disabled"}
    if not args.unified_collector_script:
        raise DeliveryError("configuration_error", "unified collector is not configured")
    command = [
        sys.executable,
        str(Path(args.unified_collector_script).expanduser()),
        "--endpoint",
        args.telemetry_endpoint,
        "--token-file",
        str(Path(args.telemetry_token_file).expanduser()),
    ]
    if args.telemetry_sites_bypass_token_file:
        command.extend(["--sites-bypass-token-file", str(Path(args.telemetry_sites_bypass_token_file).expanduser())])
    return _run_collector(command)


def _run_collector(command: list[str]) -> dict:
    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode != 0:
        http_match = re.search(r"HTTP Error\s+(\d{3})", process.stderr)
        if http_match:
            status = int(http_match.group(1))
            raise DeliveryError("http_error", f"HTTP {status}", http_status=status)
        raise DeliveryError("collector_error", f"telemetry collector exited with code {process.returncode}")
    lines = [line for line in process.stdout.splitlines() if line.strip()]
    if not lines:
        raise DeliveryError("invalid_response", "telemetry collector returned no result")
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise DeliveryError("invalid_response", "telemetry collector returned invalid JSON") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise DeliveryError("invalid_response", "telemetry collector returned an invalid result")
    return result


def deliver(args: argparse.Namespace, payload: dict) -> dict:
    result: dict = {"ok": True}
    if getattr(args, "legacy_heartbeat_enabled", True):
        try:
            token = read_token(args.token_file, "heartbeat")
            if not args.endpoint:
                raise DeliveryError("configuration_error", "heartbeat endpoint is not configured")
            heartbeat = send(args.endpoint, token, payload)
            if not isinstance(heartbeat, dict) or not heartbeat.get("ok"):
                raise DeliveryError("invalid_response", "heartbeat endpoint returned an invalid result")
            result["heartbeat"] = heartbeat
        except Exception as exc:
            result["ok"] = False
            result["heartbeat"] = failure_result("heartbeat", exc)

    if args.telemetry_endpoint:
        try:
            if not args.telemetry_token_file or not args.telemetry_script:
                raise DeliveryError("configuration_error", "telemetry configuration is incomplete")
            result["telemetry"] = send_telemetry(args)
        except Exception as exc:
            result["ok"] = False
            result["telemetry"] = failure_result("telemetry", exc)
    return result


def main(argv: list[str] | None = None) -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--endpoint")
    ap.add_argument("--token-file")
    ap.add_argument("--alias")
    ap.add_argument("--telemetry-endpoint")
    ap.add_argument("--telemetry-token-file")
    ap.add_argument("--telemetry-sites-bypass-token-file")
    ap.add_argument("--telemetry-script")
    ap.add_argument("--unified-collector-script")
    ap.add_argument("--telemetry-since-days", type=int, default=30)
    ap.add_argument("--telemetry-include-legacy", action="store_true")
    ap.add_argument("--disable-legacy-heartbeat", action="store_true")
    ap.add_argument("--print",action="store_true",dest="print_only")
    a=ap.parse_args(argv)
    a.legacy_heartbeat_enabled = not a.disable_legacy_heartbeat
    p=build_payload(a.alias)
    if a.print_only or (not a.endpoint and not a.telemetry_endpoint):
        print(json.dumps(p,ensure_ascii=False,indent=2)); return 0
    result = deliver(a, p)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__=="__main__": raise SystemExit(main())
