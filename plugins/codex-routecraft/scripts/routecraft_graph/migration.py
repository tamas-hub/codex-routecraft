"""Non-destructive config migration helpers; callers decide when to write."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .policy import PolicyError, default_config, validate_config


def migration_preview(existing: dict[str, Any] | None) -> dict[str, Any]:
    if existing is not None and (not isinstance(existing, dict) or existing.get("config_version") not in {None, 1}): raise PolicyError("unknown config version")
    if isinstance(existing, dict) and existing.get("config_version") == 1: validate_config(existing)
    target = default_config()
    if existing and isinstance(existing.get("control_center"), dict) and isinstance(existing["control_center"].get("enabled"), bool): target["control_center"]["enabled"] = existing["control_center"]["enabled"]
    return {"from_version": existing.get("config_version") if isinstance(existing, dict) else None, "to_version": 1, "config": target, "destructive": False}


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle: value = json.load(handle)
    validate_config(value); return value


def save_default_config(path: str | Path, *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    target = Path(path)
    if target.exists(): raise FileExistsError("config already exists; use explicit migration flow")
    preview = migration_preview(existing); target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with handle: handle.write(json.dumps(preview["config"], ensure_ascii=False, indent=2) + "\n")
        os.replace(handle.name, target)
    finally:
        if os.path.exists(handle.name): os.unlink(handle.name)
    return preview["config"]
