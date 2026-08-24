"""Local-only redaction and import exclusion helpers."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----[\s\S]*?-----END(?: [A-Z]+)? PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github_fine_grained_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("cookie_token", re.compile(r"(?i)\b(?:cookie|set-cookie|authorization|oauth(?:_token)?|access[_-]?token|refresh[_-]?token)\s*[:=]\s*[^\s;,'\"]{12,}")),
    ("credential", re.compile(r"(?im)^\s*(?:api[_-]?key|client[_-]?secret|password|passwd|secret|token)\s*[:=]\s*[^\s#]{6,}")),
    ("env_credential", re.compile(r"(?im)^(\s*[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|DATABASE_URL|CONNECTION_STRING)[A-Z0-9_]*\s*=)\s*[^\s#]+")),
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    # Delimiters are mandatory: compact identifiers/timestamps must never be redacted as phones.
    ("phone", re.compile(r"(?<!\d)(?:\+\d{1,3}[- .]|0\d{1,4}[- .])\d{1,4}[- .]\d{3,4}(?!\d)")),
)
_DEFAULT_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", ".cache", "dist", "build"}
_DEFAULT_EXTS = {".pem", ".key", ".p12", ".pfx", ".crt", ".cer"}

def scan_text(text: object) -> list[str]:
    value = str(text or "")
    return [kind for kind, pattern in _PATTERNS if pattern.search(value)]

def sanitize_text(text: object) -> tuple[str, list[str]]:
    value = str(text or "")
    findings: list[str] = []
    for kind, pattern in _PATTERNS:
        if pattern.search(value):
            findings.append(kind)
            if kind == "env_credential":
                value = pattern.sub(lambda match: f"{match.group(1)}[REDACTED:{kind}]", value)
            else:
                value = pattern.sub(f"[REDACTED:{kind}]", value)
    return value, findings

def is_excluded_path(path: str | Path, settings: dict | None = None) -> bool:
    item = Path(path)
    cfg = settings or {}
    dirs = _DEFAULT_DIRS | set(cfg.get("excluded_directories") or ())
    exts = _DEFAULT_EXTS | {str(v).lower() for v in (cfg.get("excluded_extensions") or ())}
    if any(part in dirs for part in item.parts):
        return True
    name = item.name.lower()
    if name == ".env" or name.startswith(".env.") or item.suffix.lower() in exts:
        return True
    return any(item.match(str(pattern)) for pattern in (cfg.get("excluded_globs") or ()))

def sanitize_values(values: Iterable[object]) -> tuple[list[str], list[str]]:
    clean, findings = [], []
    for value in values:
        text, found = sanitize_text(value)
        clean.append(text)
        findings.extend(found)
    return clean, list(dict.fromkeys(findings))
