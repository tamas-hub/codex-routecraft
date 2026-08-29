"""Shared fail-closed privacy predicates for RouteCraft protocol values."""

from __future__ import annotations

import re
from typing import Any


_SECRET_PATTERNS = (
    # OpenAI key families include sk-, sk-proj-, and sk-svcacct- forms.
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh(?:p|o|u|s|r)_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(?:cookie|set-cookie|authorization|oauth(?:_token)?|access[_-]?token|refresh[_-]?token)\s*[:=]\s*[^\s;,'\"]{12,}"),
    re.compile(r"(?i)(?:api[_-]?(?:key|token)|access[_-]?key|(?:[A-Za-z0-9]+[_-])?(?:secret|token|password)|private[_-]?key)\s*[:=]\s*['\"][^'\"\r\n]{8,}['\"]"),
    re.compile(r"(?i)(?:^|[\s;,])(?:api[_-]?key|client[_-]?secret|password|passwd|secret|token)\s*[:=]\s*[^\s#]{6,}"),
    re.compile(r"(?i)(?:^|[\s;,])[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|DATABASE_URL|CONNECTION_STRING)[A-Z0-9_]*\s*=\s*[^\s#]+"),
)
_WINDOWS_OR_UNC_PATH = re.compile(r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\[^\s\\]+\\[^\s]+)")
_POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\s_\-\"'(<\[{:;,=])/(?!/)[^\s/]+(?:/[^\s]*)?")


def contains_secret_like(value: Any) -> bool:
    """Return whether text resembles a credential without retaining or exposing it."""
    return isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def contains_absolute_path(value: Any) -> bool:
    """Return whether text contains a host path unsuitable for public telemetry."""
    return isinstance(value, str) and (
        _WINDOWS_OR_UNC_PATH.search(value) is not None or _POSIX_ABSOLUTE_PATH.search(value) is not None
    )
