#!/usr/bin/env python3
"""Build deterministic Windows and macOS ZIPs for RouteCraft Memory Local."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCRIPTS = ROOT / "plugins" / "codex-routecraft" / "scripts"
PACKAGE = SOURCE_SCRIPTS / "routecraft_local"
RELEASE = ROOT / "release"
SAMPLES = ROOT / "samples"
VERSION = (RELEASE / "VERSION").read_text(encoding="utf-8").strip()
PREFIX = f"routecraft-memory-local-{VERSION}"
FIXED_TIME = (2026, 8, 23, 0, 0, 0)


@dataclass(frozen=True)
class Entry:
    source: Path
    archive_path: str
    executable: bool = False


def _safe_archive_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"Unsafe archive path: {value}")
    return path.as_posix()


def _entries(platform_name: str) -> list[Entry]:
    common = [
        Entry(SOURCE_SCRIPTS / "routecraft.py", f"{PREFIX}/app/routecraft.py"),
        Entry(ROOT / "LICENSE", f"{PREFIX}/LICENSE"),
        Entry(ROOT / "CHANGELOG.md", f"{PREFIX}/CHANGELOG.md"),
        Entry(RELEASE / "VERSION", f"{PREFIX}/VERSION"),
        Entry(RELEASE / "README-JA.md", f"{PREFIX}/README-JA.md"),
        Entry(RELEASE / "UNINSTALL-JA.md", f"{PREFIX}/UNINSTALL-JA.md"),
        Entry(RELEASE / "THIRD_PARTY_NOTICES.md", f"{PREFIX}/THIRD_PARTY_NOTICES.md"),
        Entry(SAMPLES / "demo-memories.jsonl", f"{PREFIX}/samples/demo-memories.jsonl"),
        Entry(SAMPLES / "session-end-template.md", f"{PREFIX}/samples/session-end-template.md"),
        Entry(SAMPLES / "evaluation-suite.json", f"{PREFIX}/samples/evaluation-suite.json"),
    ]
    docs = (
        "product-spec-v1.md",
        "architecture-v1.md",
        "data-model-v1.md",
        "security-and-privacy.md",
        "release-plan-v1.md",
        "test-plan-v1.md",
    )
    common.extend(Entry(ROOT / "docs" / name, f"{PREFIX}/docs/{name}") for name in docs)
    for path in sorted(PACKAGE.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(SOURCE_SCRIPTS).as_posix()
        common.append(Entry(path, f"{PREFIX}/app/{relative}"))
    if platform_name == "windows":
        common.extend(
            [
                Entry(RELEASE / "launchers" / "routecraft.cmd", f"{PREFIX}/routecraft.cmd"),
                Entry(RELEASE / "launchers" / "routecraft.ps1", f"{PREFIX}/routecraft.ps1"),
            ]
        )
    elif platform_name == "macos":
        common.append(Entry(RELEASE / "launchers" / "routecraft", f"{PREFIX}/routecraft", executable=True))
    else:  # pragma: no cover - parser controls choices
        raise ValueError(platform_name)
    return common


def _write_archive(target: Path, entries: Iterable[Entry]) -> dict[str, object]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp")
    if temp.exists():
        temp.unlink()
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            seen: set[str] = set()
            for entry in sorted(entries, key=lambda item: item.archive_path):
                arcname = _safe_archive_path(entry.archive_path)
                if arcname in seen:
                    raise ValueError(f"Duplicate archive path: {arcname}")
                seen.add(arcname)
                if not entry.source.is_file():
                    raise FileNotFoundError(entry.source)
                info = zipfile.ZipInfo(arcname, FIXED_TIME)
                # POSIX metadata is required for the macOS launcher executable bit.
                info.create_system = 3
                mode = 0o755 if entry.executable else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, entry.source.read_bytes())
        with zipfile.ZipFile(temp, "r") as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"Corrupt archive member: {bad}")
            for name in archive.namelist():
                _safe_archive_path(name)
                lowered = name.lower()
                if "/.env" in lowered or lowered.endswith((".sqlite", ".sqlite3", ".db", ".pem", ".key")):
                    raise ValueError(f"Forbidden release payload: {name}")
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {"file": target.name, "sha256": digest, "bytes": target.stat().st_size}


def build(output_dir: Path) -> dict[str, object]:
    if not PACKAGE.is_dir():
        raise FileNotFoundError(PACKAGE)
    artifacts = []
    for platform_name in ("windows", "macos"):
        target = output_dir / f"{PREFIX}-{platform_name}.zip"
        artifacts.append(_write_archive(target, _entries(platform_name)))
    checksums = "".join(f"{item['sha256']}  {item['file']}\n" for item in artifacts)
    (output_dir / "SHA256SUMS.txt").write_text(checksums, encoding="ascii", newline="\n")
    manifest = {
        "schema_version": 1,
        "product": "RouteCraft Memory Local",
        "version": VERSION,
        "runtime": "Python 3.11+ standard library",
        "signed": False,
        "notarized": False,
        "artifacts": artifacts,
    }
    (output_dir / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build RouteCraft Memory Local release ZIPs")
    parser.add_argument("--output-dir", default=str(ROOT / "dist"))
    args = parser.parse_args(argv)
    output = Path(args.output_dir).expanduser().resolve()
    manifest = build(output)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
