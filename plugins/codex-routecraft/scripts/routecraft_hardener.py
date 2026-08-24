"""Bounded, local-only security review with safe config-only remediation.

This is deliberately a static signal collector, not a vulnerability scanner:
it never executes files, follows links, calls a service, or includes matching
source text and secret values in its report.
"""
from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

SAFE_DEFAULTS = {
    "enabled": True,
    "provider": "github",
    "default_visibility": "private",
    "allow_force_push": False,
    "store_raw_transcripts": False,
    "store_device_config": False,
}
MAX_FILES = 500
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 8_000_000
EXCLUDED_DIRS = frozenset({".git", ".ccc", ".venv", "__pycache__", "build", "coverage", "dist", "node_modules", "vendor"})
TEXT_SUFFIXES = frozenset({".cjs", ".css", ".env", ".html", ".ini", ".js", ".json", ".lock", ".mjs", ".ps1", ".py", ".sh", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml"})
TEXT_NAMES = frozenset({"Dockerfile", "Gemfile", "Pipfile", "Pipfile.lock", "package-lock.json", "package.json", "pnpm-lock.yaml", "requirements.txt", "wrangler.toml", "yarn.lock"})
SECRET_ASSIGNMENT = re.compile(r"(?i)\b(?:api[_-]?(?:key|token)|access[_-]?key|(?:[A-Za-z0-9]+[_-])?(?:secret|token|password)|private[_-]?key)\b\s*[:=]\s*['\"][^'\"\r\n]{8,}['\"]")
SECRET_PREFIX = re.compile(r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
PLACEHOLDER = re.compile(r"(?i)(?:example|placeholder|changeme|not[-_ ]?a[-_ ]?real|your[_-]?(?:token|secret|key)|dummy)")
CLIENT_EXPOSED_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Z0-9_])['\"]?(?:NEXT_PUBLIC|VITE|REACT_APP|NUXT_PUBLIC|PUBLIC)_[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PRIVATE[_-]?KEY)[A-Z0-9_]*['\"]?\s*(?P<separator>[:=])\s*(?P<value>.*)$"
)
RULESET_VERSION = "1.0.0"


def _rule(category: str, severity: str, confidence: str, recommendation: str) -> dict[str, object]:
    return {
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "recommendation": recommendation,
        "validation_required": True,
    }


RULE_REGISTRY: dict[str, dict[str, object]] = {
    "SECRET-STATIC-001": _rule("secrets", "high", "high", "Move the credential to a local secret store or platform secret binding; do not commit it."),
    "LOG-CREDENTIAL-001": _rule("credential_logging", "medium", "medium", "Log a redacted event code or aggregate, never credential-like values."),
    "CODE-EVAL-001": _rule("eval", "high", "high", "Replace dynamic code evaluation with structured parsing and an allowlisted dispatcher."),
    "SHELL-UNSAFE-001": _rule("shell", "high", "high", "Use argument arrays with shell disabled and validate untrusted input."),
    "SQL-INTERPOLATION-001": _rule("sql", "high", "medium", "Use parameterized queries; keep SQL structure constant and bind values separately."),
    "CORS-WILDCARD-001": _rule("cors", "medium", "high", "Use an explicit origin allowlist and do not combine credentialed CORS with a wildcard."),
    "CSP-WEAK-001": _rule("csp", "medium", "high", "Remove unsafe CSP sources where possible; use hashes or nonces for required scripts."),
    "TLS-VERIFY-DISABLED-001": _rule("tls", "high", "high", "Keep certificate verification enabled outside an explicitly isolated test fixture."),
    "AUTH-BYPASS-001": _rule("auth", "high", "high", "Require authentication and authorization checks on the protected operation."),
    "INFRA-PRIVILEGED-001": _rule("infrastructure", "high", "high", "Remove privileged or unconfined execution unless a reviewed isolation exception exists."),
    "INFRA-PUBLIC-INGRESS-001": _rule("infrastructure", "medium", "medium", "Restrict ingress CIDRs to the smallest documented allowlist."),
    "CF-SECRET-IN-VARS-001": _rule("cloudflare", "high", "high", "Use a Cloudflare secret binding, not a value in vars/config."),
    "GHA-PR-TARGET-001": _rule("github_actions", "high", "high", "Avoid pull_request_target for untrusted pull-request code; use a least-privilege pull_request workflow."),
    "GHA-WRITE-ALL-001": _rule("github_actions", "high", "high", "Declare the minimal job or workflow permissions required."),
    "GHA-UNPINNED-ACTION-001": _rule("github_actions", "medium", "high", "Pin GitHub Actions uses references to a reviewed full commit SHA."),
    "GHA-PERMISSIONS-UNDECLARED-001": _rule("github_actions", "low", "medium", "Declare least-privilege workflow permissions explicitly."),
    "DEP-LOCK-MISSING-001": _rule("dependency", "medium", "high", "Commit a supported dependency lockfile before installing or releasing."),
    "DEP-RISKY-SCRIPT-001": _rule("dependency", "medium", "medium", "Review lifecycle/download scripts locally; pin inputs and avoid piping remote content to a shell."),
    "TARGET-BLANK-NOOPENER-001": _rule("browser_navigation", "low", "medium", "Add rel=\"noopener noreferrer\" to target=_blank links that open an untrusted browsing context."),
    "PUBLIC-ENV-SECRET-001": _rule("public_environment", "high", "medium", "Keep credential-like values out of client-public environment variable namespaces."),
}

DIAGNOSTIC_REGISTRY: dict[str, dict[str, object]] = {
    "SCAN-BOUNDED-001": {
        "category": "scanner_boundary",
        "severity": "info",
        "confidence": "high",
        "recommendation": "Review excluded or size-capped files manually if they are security-relevant.",
        "validation_required": False,
    }
}
RULESET_DIGEST = hashlib.sha256(
    json.dumps(RULE_REGISTRY, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _opaque(*parts: str, length: int = 32) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:length]


def _relative(root: Path, target: Path) -> str:
    return target.relative_to(root).as_posix()


def _text_candidate(path: Path) -> bool:
    return path.name in TEXT_NAMES or path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith(".env")


def _is_link_or_junction(path: Path) -> bool:
    """Reject links and Windows reparse points without resolving their target."""
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _tracked_files(root: Path) -> list[Path] | None:
    """Return tracked and unignored local paths without fetching or following links."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    files: list[Path] = []
    for raw in result.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not raw:
            continue
        relative = Path(raw)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        candidate = root / relative
        if _text_candidate(candidate) and candidate.is_file() and not _is_link_or_junction(candidate):
            files.append(candidate)
    return files


def _bounded_files(root: Path) -> Iterable[Path]:
    """Fallback for non-git fixtures: bounded text files only, with no links."""
    for directory, dirs, names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS and not _is_link_or_junction(directory_path / name)]
        for name in names:
            candidate = directory_path / name
            if _text_candidate(candidate) and candidate.is_file() and not _is_link_or_junction(candidate):
                yield candidate


def _select_files(root: Path) -> tuple[list[Path], bool]:
    selected: list[Path] = []
    total = 0
    limited = False
    candidates = _tracked_files(root)
    for candidate in candidates if candidates is not None else _bounded_files(root):
        try:
            size = candidate.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            continue
        if len(selected) >= MAX_FILES or total + size > MAX_TOTAL_BYTES:
            limited = True
            break
        selected.append(candidate)
        total += size
    return sorted(selected, key=lambda item: _relative(root, item)), limited


def _finding(
    code: str,
    relative_file: str,
    line: int,
    *,
    severity: str | None = None,
    confidence: str | None = None,
    recommendation: str | None = None,
) -> dict[str, object]:
    metadata = RULE_REGISTRY.get(code) or DIAGNOSTIC_REGISTRY.get(code)
    if metadata is None:
        raise ValueError(f"unregistered security rule: {code}")
    return {
        "code": code,
        "severity": severity or str(metadata["severity"]),
        "confidence": confidence or str(metadata["confidence"]),
        "relative_file": relative_file,
        "line": max(1, line),
        "recommendation": recommendation or str(metadata["recommendation"]),
    }


def _target_blank_without_noopener(line: str) -> bool:
    for match in re.finditer(r"(?i)<a\b[^>]*>", line):
        tag = match.group(0)
        if not re.search(r"(?i)\btarget\s*=\s*(?:(['\"])_blank\1|_blank(?=\s|>))", tag):
            continue
        rel = re.search(r"(?i)\brel\s*=\s*(['\"])(.*?)\1", tag)
        tokens = {value.casefold() for value in re.split(r"\s+", rel.group(2).strip())} if rel else set()
        if not {"noopener", "noreferrer"} & tokens:
            return True
    return False


def _target_blank_without_noopener_lines(text: str) -> set[int]:
    """Return opening-tag line numbers missing a safe opener policy.

    JSX/HTML attributes are often formatted across several lines. The
    scanner remains bounded to a single source document and a 4 KiB opening
    tag, but does not make a line-break turn a dangerous ``target`` into an
    invisible one.
    """
    violating_lines: set[int] = set()
    for match in re.finditer(r"(?is)<a\b[^>]{0,4096}>", text):
        tag = match.group(0)
        if not re.search(r"(?i)\btarget\s*=\s*(?:(['\"])_blank\1|_blank(?=\s|>))", tag):
            continue
        rel = re.search(r"(?i)\brel\s*=\s*(['\"])(.*?)\1", tag, re.DOTALL)
        tokens = {value.casefold() for value in re.split(r"\s+", rel.group(2).strip())} if rel else set()
        if not {"noopener", "noreferrer"} & tokens:
            violating_lines.add(text.count("\n", 0, match.start()) + 1)
    return violating_lines


def _public_env_credential_assignment(relative_file: str, line: str) -> bool:
    # Vite replaces ``import.meta.env.VITE_*`` at build time. A secret-like
    # name is therefore public even when it is only referenced in a define or
    # a client module rather than assigned as a literal in this line.
    vite_secret_name = re.compile(
        r"(?i)\bVITE_[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PRIVATE[_-]?KEY)[A-Z0-9_]*\b"
    )
    if vite_secret_name.search(line) and re.search(r"(?i)(?:import\.meta\.env|process\.env|VITE_)", line):
        assignment = CLIENT_EXPOSED_CREDENTIAL_ASSIGNMENT.search(line)
        if assignment is None:
            return True
    match = CLIENT_EXPOSED_CREDENTIAL_ASSIGNMENT.search(line)
    if not match:
        return False
    value = re.split(r"\s+(?:#|//)", match.group("value"), maxsplit=1)[0].strip().rstrip(";,}")
    if not value or PLACEHOLDER.search(value):
        return False
    if match.group("separator") == "=":
        return True
    suffix = Path(relative_file).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return True
    return bool(re.match(r"^(?:['\"`]|[-+]?\d|true\b|false\b|null\b|\[|\{)", value, re.IGNORECASE))


def _line_findings(relative_file: str, text: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    is_workflow = relative_file.startswith(".github/workflows/")
    is_cloudflare = Path(relative_file).name in {"wrangler.toml", "wrangler.json", "wrangler.jsonc"}
    target_blank_lines = _target_blank_without_noopener_lines(text)
    for number, line in enumerate(text.splitlines(), start=1):
        if "routecraft-security: scanner-test-fixture" in line or "routecraft-security: scanner-pattern" in line:
            continue
        if SECRET_PREFIX.search(line) or (SECRET_ASSIGNMENT.search(line) and not PLACEHOLDER.search(line)):
            findings.append(_finding("SECRET-STATIC-001", relative_file, number))
        if re.search(
            r"(?i)\b(?:print|console\.(?:log|info|debug)|logger\.(?:debug|info|warning|error))\s*\([^\n]*"
            r"(?<![A-Za-z0-9_])(?:api[_-]?(?:key|token)|access[_-]?token|auth(?:orization|[_-]?token)?|refresh[_-]?token|"
            r"client[_-]?secret|private[_-]?key|token|secret|password|cookie)(?![A-Za-z0-9_])",
            line,
        ):
            findings.append(_finding("LOG-CREDENTIAL-001", relative_file, number))
        if re.search(r"(?<![A-Za-z0-9_.])(?:eval|exec)\s*\(|\bnew\s+Function\s*\(", line):
            findings.append(_finding("CODE-EVAL-001", relative_file, number))
        if re.search(r"(?i)\b(?:subprocess\.(?:run|call|Popen)|os\.system)\s*\([^\n]*shell\s*=\s*True", line):
            findings.append(_finding("SHELL-UNSAFE-001", relative_file, number))
        elif re.search(r"\bos\.system\s*\(", line):
            findings.append(
                _finding(
                    "SHELL-UNSAFE-001",
                    relative_file,
                    number,
                    severity="medium",
                    confidence="medium",
                    recommendation="Use a fixed argument array via subprocess with shell disabled.",
                )
            )
        dynamic_sql = bool(
            re.search(r"(?i)\bf[\"']\s*(?:select|insert|update|delete)\s+", line)
            or re.search(
                r"(?i)[\"']\s*(?:select|insert|update|delete)\s+[^\"']*[\"']\s*(?:\+|\.format\()",
                line,
            )
        )
        if dynamic_sql and "routecraft-security: allowlisted-sql-shape" not in line:
            findings.append(_finding("SQL-INTERPOLATION-001", relative_file, number))
        if re.search(r"(?i)access-control-allow-origin", line) and re.search(r"(?i)(?:[:=,]\s*['\"]?\*|['\"]\s*\*\s*['\"])", line):
            findings.append(_finding("CORS-WILDCARD-001", relative_file, number))
        if "Content-Security-Policy" in line and ("unsafe-eval" in line or "unsafe-inline" in line):  # routecraft-security: scanner-pattern
            findings.append(_finding("CSP-WEAK-001", relative_file, number))
        if re.search(r"(?i)(?:verify_ssl|verify)\s*=\s*False", line):
            findings.append(_finding("TLS-VERIFY-DISABLED-001", relative_file, number))
        if re.search(r"(?i)\b(?:allow_anonymous|skip_authorization|bypass_auth)\s*[:=]\s*true", line):
            findings.append(_finding("AUTH-BYPASS-001", relative_file, number))
        if re.search(r"(?i)\bprivileged\s*:\s*true\b|security_opt\s*:\s*.*unconfined", line):
            findings.append(_finding("INFRA-PRIVILEGED-001", relative_file, number))
        if re.search(r"(?i)(?:cidr_blocks|source_ranges)\s*=?.*0\.0\.0\.0/0", line):
            findings.append(_finding("INFRA-PUBLIC-INGRESS-001", relative_file, number))
        if is_cloudflare and re.search(r"(?i)\b(?:api[_-]?(?:key|token)|(?:[A-Za-z0-9]+[_-])?(?:token|secret|password))\b\s*=", line):
            findings.append(_finding("CF-SECRET-IN-VARS-001", relative_file, number))
        if number in target_blank_lines:
            findings.append(_finding("TARGET-BLANK-NOOPENER-001", relative_file, number))
        if _public_env_credential_assignment(relative_file, line):
            findings.append(_finding("PUBLIC-ENV-SECRET-001", relative_file, number))
        if is_workflow:
            stripped = line.split("#", 1)[0].strip()
            if re.match(r"(?i)pull_request_target\s*:", stripped) or (
                re.match(r"(?i)(?:['\"]?on['\"]?)\s*:", stripped)
                and re.search(r"(?i)\bpull_request_target\b", stripped)
            ):
                findings.append(_finding("GHA-PR-TARGET-001", relative_file, number))
            if re.search(r"\bpermissions\s*:\s*write-all", line):
                findings.append(_finding("GHA-WRITE-ALL-001", relative_file, number))
            match = re.search(r"\buses\s*:\s*[^@\s]+@([^\s#]+)", line)
            if match and not re.fullmatch(r"[0-9a-fA-F]{40}", match.group(1)):
                findings.append(_finding("GHA-UNPINNED-ACTION-001", relative_file, number))
    if is_workflow and not re.search(r"(?m)^\s*permissions\s*:", text):
        findings.append(_finding("GHA-PERMISSIONS-UNDECLARED-001", relative_file, 1))
    return findings


def _manifest_findings(relative_file: str, text: str, available: set[str]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    if relative_file == "package.json":
        if not {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"} & available:
            findings.append(_finding("DEP-LOCK-MISSING-001", relative_file, 1))
        try:
            scripts = json.loads(text).get("scripts", {})
        except (TypeError, ValueError, json.JSONDecodeError):
            scripts = {}
        if isinstance(scripts, dict):
            for command in scripts.values():
                if isinstance(command, str) and re.search(r"(?i)\b(?:curl|wget|invoke-webrequest|iwr)\b", command):
                    findings.append(_finding("DEP-RISKY-SCRIPT-001", relative_file, 1))
    if relative_file == "requirements.txt" and not {"poetry.lock", "Pipfile.lock", "requirements.lock", "uv.lock"} & available:
        findings.append(
            _finding(
                "DEP-LOCK-MISSING-001",
                relative_file,
                1,
                confidence="medium",
                recommendation="Use a reviewed, pinned lockfile for production dependency resolution.",
            )
        )
    return findings


def _document_findings(documents: Mapping[str, str]) -> list[dict[str, object]]:
    available = set(documents)
    findings: list[dict[str, object]] = []
    for relative_file in sorted(documents):
        text = documents[relative_file]
        findings.extend(_line_findings(relative_file, text))
        findings.extend(_manifest_findings(relative_file, text, available))
    unique = {(item["code"], item["relative_file"], item["line"]): item for item in findings}
    return sorted(unique.values(), key=lambda item: (str(item["relative_file"]), int(item["line"]), str(item["code"])))


def scan_fixture_documents(documents: Mapping[str, str], *, observed_at: str | None = None) -> dict[str, object]:
    """Run registered rules against bounded in-memory validation documents.

    This interface does not create or execute fixture files, follow links, invoke
    Git, or use the network. It validates rule behavior only; a clean result is
    not a security guarantee for the supplied text or any repository.
    """
    if not isinstance(documents, Mapping) or not documents or len(documents) > MAX_FILES:
        raise ValueError("fixture documents must be a non-empty bounded mapping")
    normalized: dict[str, str] = {}
    total = 0
    for raw_path, body in documents.items():
        if not isinstance(raw_path, str) or not isinstance(body, str):
            raise ValueError("fixture paths and document bodies must be strings")
        if "\\" in raw_path or ":" in raw_path:
            raise ValueError("fixture paths must be portable relative POSIX paths")
        relative = PurePosixPath(raw_path)
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("fixture paths must stay inside the in-memory fixture root")
        normalized_path = relative.as_posix()
        if normalized_path in normalized or not _text_candidate(Path(normalized_path)):
            raise ValueError("fixture path is duplicate or outside the text scanner scope")
        encoded_size = len(body.encode("utf-8"))
        if encoded_size > MAX_FILE_BYTES:
            raise ValueError("fixture document exceeds the per-file limit")
        total += encoded_size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("fixture documents exceed the total-size limit")
        normalized[normalized_path] = body
    timestamp = observed_at or _now()
    findings = _document_findings(normalized)
    return _report(_opaque("security-fixture-validation", length=16), timestamp, "policy", findings, set())


def _fingerprint(finding: Mapping[str, object]) -> str:
    return _opaque(str(finding["code"]), str(finding["relative_file"]), str(finding["line"]))


def _baseline_fingerprints(baseline: str | Path | Mapping[str, object] | None) -> tuple[str, set[str]]:
    if baseline is None:
        return "initial", set()
    try:
        value: object = json.loads(Path(baseline).read_text(encoding="utf-8")) if isinstance(baseline, (str, Path)) else baseline
        if not isinstance(value, Mapping):
            raise ValueError("baseline must be an object")
        known = value.get("finding_fingerprints", [])
        if not isinstance(known, list) or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{16,64}", item) for item in known):
            raise ValueError("baseline fingerprints are invalid")
        kind = str(value.get("baseline", "previous"))
        return kind if kind in {"previous", "policy"} else "previous", set(known)
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return "initial", set()


def scan(source_root: str | Path, baseline: str | Path | Mapping[str, object] | None = None) -> dict[str, object]:
    """Inspect bounded local text and emit source-free, stable findings."""
    root = Path(source_root)
    observed_at = _now()
    repository_hint = _opaque(root.name or "repository", length=16)
    baseline_kind, previous = _baseline_fingerprints(baseline)
    if not root.is_dir() or _is_link_or_junction(root):
        return _report(repository_hint, observed_at, baseline_kind, [], previous, error=True)
    try:
        files, limited = _select_files(root)
        documents: dict[str, str] = {}
        for path in files:
            relative_file = _relative(root, path)
            try:
                documents[relative_file] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        findings = _document_findings(documents)
        if limited:
            findings.append(_finding("SCAN-BOUNDED-001", ".", 1))
        unique = {(item["code"], item["relative_file"], item["line"]): item for item in findings}
        return _report(repository_hint, observed_at, baseline_kind, sorted(unique.values(), key=lambda item: (str(item["relative_file"]), int(item["line"]), str(item["code"]))), previous)
    except (OSError, ValueError):
        return _report(repository_hint, observed_at, baseline_kind, [], previous, error=True)


def _report(repository_hint: str, observed_at: str, baseline_kind: str, findings: list[dict[str, object]], previous: set[str], *, error: bool = False) -> dict[str, object]:
    counts = {severity: 0 for severity in ("critical", "high", "medium", "low", "info")}
    for item in findings:
        counts[str(item["severity"])] += 1
    fingerprints = sorted(_fingerprint(item) for item in findings)
    current = set(fingerprints)
    status = "error" if error else ("findings" if findings else "clean")
    confidence = "low" if error else ("high" if findings else "medium")
    return {"scan_id": _opaque(repository_hint, observed_at), "repository_hint": repository_hint, "observed_at": observed_at, "status": status, "baseline": baseline_kind, **counts, "confidence": confidence, "existing": len(current & previous), "new": len(current - previous), "resolved": len(previous - current), "findings": findings, "finding_fingerprints": fingerprints}


def control_center_summary(report: Mapping[str, object]) -> dict[str, object]:
    """Safe adapter for a collector: aggregate state and counts only."""
    return {key: report.get(key, 0 if key != "status" else "error") for key in ("status", "critical", "high", "medium", "low", "info", "existing", "new", "resolved")}


def to_d1_summary(report: Mapping[str, object], *, device_id: str = "0000000000000000") -> dict[str, object]:
    """Return one exact, detail-free schema-v3 ``security_scans`` row."""
    status = str(report.get("status", "error"))
    baseline = str(report.get("baseline", "initial"))
    confidence = str(report.get("confidence", "low"))
    return {
        "scan_id": str(report.get("scan_id") or _opaque("security", str(report.get("observed_at", _now()))))[:64],
        "device_id": device_id,
        "observed_at": str(report.get("observed_at") or _now()),
        "repository_hint": str(report.get("repository_hint") or "repository")[:80],
        "status": status if status in {"clean", "findings", "error", "unavailable"} else "error",
        "baseline": baseline if baseline in {"initial", "previous", "policy"} else "initial",
        "critical_count": max(0, int(report.get("critical", 0) or 0)),
        "high_count": max(0, int(report.get("high", 0) or 0)),
        "medium_count": max(0, int(report.get("medium", 0) or 0)),
        "low_count": max(0, int(report.get("low", 0) or 0)),
        "info_count": max(0, int(report.get("info", 0) or 0)),
        "new_count": max(0, int(report.get("new", 0) or 0)),
        "resolved_count": max(0, int(report.get("resolved", 0) or 0)),
        "confidence": confidence if confidence in {"low", "medium", "high"} else "low",
    }


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("security configuration must be a JSON object")
    return value


def static_analysis(source_root: str | Path | None = None, baseline: str | Path | Mapping[str, object] | None = None) -> dict[str, object]:
    root = Path(source_root) if source_root else Path(__file__).resolve().parents[3]
    return scan(root, baseline)


def analyze(path: str | Path, source_root: str | Path | None = None, baseline: str | Path | Mapping[str, object] | None = None) -> dict[str, object]:
    report = static_analysis(source_root, baseline)
    try:
        current = _load(Path(path))
    except (OSError, ValueError, json.JSONDecodeError):
        return {**report, "status": "error", "safe_fix_count": 0}
    fixes = [key for key, expected in SAFE_DEFAULTS.items() if current.get(key) != expected]
    return {**report, "safe_fix_count": len(fixes)}


def preview(path: str | Path, source_root: str | Path | None = None, baseline: str | Path | Mapping[str, object] | None = None) -> dict[str, object]:
    target = Path(path)
    current = _load(target)
    proposed = {**current, **SAFE_DEFAULTS}
    before = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    after = json.dumps(proposed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return {"analysis": analyze(target, source_root, baseline), "dry_run": True, "changed": before != after, "diff": "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="source-control.json", tofile="source-control.json (proposed)"))}


def apply(path: str | Path, confirmation: str) -> dict[str, object]:
    """Apply only SAFE_DEFAULTS after an explicit caller confirmation."""
    if confirmation != "APPLY":
        raise ValueError("explicit confirmation APPLY is required")
    target = Path(path)
    current = _load(target)
    proposed = {**current, **SAFE_DEFAULTS}
    if current == proposed:
        return {"changed": False}
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".routecraft-tmp")
    temporary.write_text(json.dumps(proposed, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)
    return {"changed": True}
