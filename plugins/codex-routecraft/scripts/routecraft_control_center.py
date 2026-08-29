"""Optional RouteCraft Control Center transport boundary.

No RouteCraft core module imports this file.  A user or tray host must set
``CONTROL_CENTER_ENABLED=true`` and invoke this adapter explicitly.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import urllib.request
import urllib.parse
from pathlib import Path

from routecraft_collector import collect_v5, enabled, payload_batches, validate_payload

DEFAULT_CONTROL_CENTER_ORIGIN = "https://routecraft.tama812.chatgpt.site"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())


def _validated_endpoint(endpoint: str) -> str | None:
    try:
        target = urllib.parse.urlsplit(endpoint)
        allowed = urllib.parse.urlsplit(os.environ.get("ROUTECRAFT_CONTROL_CENTER_ALLOWED_ORIGIN", DEFAULT_CONTROL_CENTER_ORIGIN))
    except ValueError:
        return None
    if target.scheme != "https" or allowed.scheme != "https" or target.username or target.password or target.fragment or target.query:
        return None
    if target.port not in {None, 443} or allowed.port not in {None, 443}:
        return None
    if target.hostname != allowed.hostname or target.path.rstrip("/") != "/api/ingest":
        return None
    return urllib.parse.urlunsplit(("https", target.hostname or "", "/api/ingest", "", ""))


def _read_token(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink(): return None
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077: return None
        value = path.read_text(encoding="utf-8").strip()
        return value if len(value) >= 32 else None
    except OSError:
        return None


def deliver(endpoint: str, token_file: Path, payload: dict[str, object], sites_bypass_token_file: Path | None = None) -> dict[str, object]:
    if not enabled():
        return {"ok": True, "delivered": False, "state": "disabled"}
    endpoint = _validated_endpoint(endpoint)
    if endpoint is None:
        return {"ok": False, "delivered": False, "state": "unavailable"}
    try:
        token = _read_token(token_file)
        if token is None or not validate_payload(payload):
            return {"ok": False, "delivered": False, "state": "unavailable"}
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token, "User-Agent": "RouteCraft-Collector/5"}
        if sites_bypass_token_file is not None:
            bypass = _read_token(sites_bypass_token_file)
            if bypass is None:
                return {"ok": False, "delivered": False, "state": "unavailable"}
            headers["OAI-Sites-Authorization"] = "Bearer " + bypass
        batches = payload_batches(payload)
        for batch in batches:
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with _NO_REDIRECT_OPENER.open(request, timeout=15) as response:
                if not 200 <= int(response.status) < 300:
                    return {"ok": False, "delivered": False, "state": "unavailable"}
        return {"ok": True, "delivered": True, "state": "ok", "batches": len(batches)}
    except Exception:
        return {"ok": False, "delivered": False, "state": "unavailable"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="routecraft-control-center")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--data-dir")
    parser.add_argument("--sites-bypass-token-file", type=Path)
    args = parser.parse_args(argv)
    payload = collect_v5(source_root=args.source_root, data_dir=args.data_dir)
    result = deliver(args.endpoint, args.token_file, payload, args.sites_bypass_token_file)
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
