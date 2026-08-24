"""Optional RouteCraft Control Center transport boundary.

No RouteCraft core module imports this file.  A user or tray host must set
``CONTROL_CENTER_ENABLED=true`` and invoke this adapter explicitly.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

from routecraft_collector import collect_v3, enabled, payload_batches, validate_v3


def deliver(endpoint: str, token_file: Path, payload: dict[str, object], sites_bypass_token_file: Path | None = None) -> dict[str, object]:
    if not enabled():
        return {"ok": True, "delivered": False, "state": "disabled"}
    if not endpoint.startswith("https://"):
        return {"ok": False, "delivered": False, "state": "unavailable"}
    try:
        token = token_file.read_text(encoding="utf-8").strip()
        if len(token) < 32 or not validate_v3(payload):
            return {"ok": False, "delivered": False, "state": "unavailable"}
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token, "User-Agent": "RouteCraft-Collector/3"}
        if sites_bypass_token_file is not None:
            bypass = sites_bypass_token_file.read_text(encoding="utf-8").strip()
            if len(bypass) < 32:
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
            with urllib.request.urlopen(request, timeout=15) as response:
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
    payload = collect_v3(source_root=args.source_root, data_dir=args.data_dir)
    result = deliver(args.endpoint, args.token_file, payload, args.sites_bypass_token_file)
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
