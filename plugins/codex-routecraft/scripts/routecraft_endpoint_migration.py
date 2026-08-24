"""Narrow, secret-safe Observatory endpoint migration helper."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENDPOINT_KEYS = ("endpoint", "telemetry_endpoint")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Observatory configuration must be a JSON object")
    return value


def _validate_urls(old_url: str, new_url: str) -> None:
    if not old_url.startswith("https://") or not new_url.startswith("https://") or old_url == new_url:
        raise ValueError("old and new URLs must be distinct HTTPS endpoints")


def preview(config_path: str | Path, old_url: str, new_url: str) -> dict[str, object]:
    _validate_urls(old_url, new_url)
    config = _load(Path(config_path))
    changed = [key for key in ENDPOINT_KEYS if config.get(key) == old_url]
    return {"state": "ready" if changed else "unchanged", "changed_endpoint_count": len(changed), "keys": changed}


def apply(config_path: str | Path, old_url: str, new_url: str, confirmation: str) -> dict[str, object]:
    if confirmation != "APPLY":
        raise ValueError("explicit confirmation APPLY is required")
    target = Path(config_path)
    check = preview(target, old_url, new_url)
    if check["changed_endpoint_count"] == 0:
        return {**check, "applied": False, "backup_path": None}
    config = _load(target)
    for key in check["keys"]:
        config[str(key)] = new_url
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(target.name + ".before-endpoint-migration-" + stamp + ".bak")
    shutil.copy2(target, backup)
    temporary = target.with_suffix(target.suffix + ".routecraft-tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return {"state": "applied", "applied": True, "changed_endpoint_count": check["changed_endpoint_count"], "backup_path": str(backup)}
