"""RouteCraft Memory Local v1.0.

This package is deliberately separate from ``routecraft_memory_lib``.  The
existing Markdown Decision Store remains a supported, unchanged compatibility
surface and can be imported into the local project database explicitly.
"""

from __future__ import annotations

RUNTIME_VERSION = "0.7.1"
VERSION = "1.0.0"
MEMORY_LOCAL_VERSION = VERSION
SCHEMA_VERSION = 1

MEMORY_TYPES = (
    "decision",
    "failure",
    "lesson",
    "next_action",
    "constraint",
    "architecture",
    "file_reference",
    "dependency",
    "deployment",
    "security",
    "note",
    "session_summary",
)

IMPORTANCE_LEVELS = ("high", "medium", "low")
CONTEXT_PROFILES = {"compact": 4_000, "standard": 12_000, "full": 50_000}

__all__ = [
    "CONTEXT_PROFILES",
    "IMPORTANCE_LEVELS",
    "MEMORY_TYPES",
    "MEMORY_LOCAL_VERSION",
    "RUNTIME_VERSION",
    "SCHEMA_VERSION",
    "VERSION",
]
