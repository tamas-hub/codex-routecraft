"""Narrow, secret-safe Observatory endpoint migration helper."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENDPOINT_KEYS = ("dashboard_url", "endpoint", "telemetry_endpoint")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Observatory configuration must be a JSON object")
    return value


def _validate_urls(old_url: str, new_url: str) -> None:
    if not old_url.startswith("https://") or not new_url.startswith("https://") or old_url == new_url:
        raise ValueError("old and new URLs must be distinct HTTPS endpoints")


def _replace_origin(value: object, old_url: str, new_url: str) -> str | None:
    if not isinstance(value, str):
        return None
    old = old_url.rstrip("/")
    new = new_url.rstrip("/")
    if value == old:
        return new
    if value.startswith(old + "/"):
        return new + value[len(old):]
    return None


def preview(config_path: str | Path, old_url: str, new_url: str) -> dict[str, object]:
    _validate_urls(old_url, new_url)
    config = _load(Path(config_path))
    changed = [key for key in ENDPOINT_KEYS if _replace_origin(config.get(key), old_url, new_url) is not None]
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
        replacement = _replace_origin(config.get(str(key)), old_url, new_url)
        if replacement is None:
            raise ValueError("endpoint configuration changed during migration")
        config[str(key)] = replacement
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(target.name + ".before-endpoint-migration-" + stamp + ".bak")
    shutil.copy2(target, backup)
    temporary = target.with_suffix(target.suffix + ".routecraft-tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return {"state": "applied", "applied": True, "changed_endpoint_count": check["changed_endpoint_count"], "backup_path": str(backup)}
