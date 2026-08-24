"""Canonical, deterministic data primitives used by the graph kernel."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    """Return strict stable JSON; NaN and arbitrary objects are rejected."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(value: Any) -> str:
    encoded = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def stable_id(prefix: str, value: Any | None = None) -> str:
    """Opaque IDs are deterministic when a value is supplied, random otherwise."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
        raise ValueError("stable ID prefix is invalid")
    suffix = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:20] if value is not None else uuid.uuid4().hex
    return f"{prefix}_{suffix}"


def is_absolute_or_pathlike(value: str) -> bool:
    return bool(re.search(r"(?:^[A-Za-z]:[\\/]|^/|^\\\\|[\\/](?:Users|home|workspace)[\\/])", value))
