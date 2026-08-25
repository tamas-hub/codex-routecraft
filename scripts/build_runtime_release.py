#!/usr/bin/env python3
"""Build deterministic, pinned RouteCraft Runtime 0.7.4 starter packages.

The builder deliberately refuses to infer a release tag or commit. It reads the
source archive from the supplied Git commit, not from the working tree, and it
does not publish artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


PRODUCT = "RouteCraft Local Runtime"
VERSION = "0.7.4"
OFFICIAL_REPOSITORY = "https://github.com/tamas-hub/codex-routecraft.git"
PREFIX = f"routecraft-runtime-{VERSION}"
RELEASE_TAG = f"v{VERSION}"
TESTED_CODEX_CLI_VERSION = "0.148.0"
FIXED_TIME = (2026, 8, 25, 0, 0, 0)
SOURCE_ROOT = Path(__file__).resolve().parents[1]
SAFE_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
FULL_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
PLUGIN_RELEASE_VERSION = re.compile(rf"{re.escape(VERSION)}\+codex\.\d{{14}}\Z")
TEXT_TOKENS = {
    "@ROUTECRAFT_REPOSITORY@": OFFICIAL_REPOSITORY,
    "@ROUTECRAFT_VERSION@": VERSION,
    "@ROUTECRAFT_CODEX_CLI_VERSION@": TESTED_CODEX_CLI_VERSION,
}
SECRET_SIGNATURES = (
    # Split the literals so the release builder does not match its own source.
    re.compile(rb"-----BEGIN " + rb"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"gh" + rb"[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"sk" + rb"-[A-Za-z0-9_-]{32,}"),
    re.compile(rb"AK" + rb"IA[0-9A-Z]{16}"),
)


class ReleaseError(RuntimeError):
    """Raised when a release input or artifact violates the release contract."""


@dataclass(frozen=True)
class Entry:
    archive_path: str
    data: bytes
    mode: int = 0o644


def _run_git(source_tree: Path, *args: str, binary: bool = False) -> bytes | str:
    process = subprocess.run(
        ["git", "-C", str(source_tree), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
        check=False,
    )
    if process.returncode:
        stderr = process.stderr if isinstance(process.stderr, str) else process.stderr.decode("utf-8", "replace")
        raise ReleaseError(f"Git command failed: {' '.join(args)}\n{stderr.strip()}")
    return process.stdout


def _safe_archive_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in value:
        raise ReleaseError(f"Unsafe archive path: {value}")
    return path.as_posix()


def _forbidden_member(value: str) -> bool:
    parts = tuple(part.lower() for part in PurePosixPath(value).parts)
    name = parts[-1]
    if name in {
        ".env",
        ".envrc",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".sandbox-secrets",
        "auth.json",
        "credential.json",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "secret.json",
        "secrets.json",
        "service-account.json",
        "service_account.json",
        "token.json",
    } or name.startswith(".env."):
        return True
    if name.endswith((".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".jks", ".kdbx", ".ovpn")):
        return True
    return any(part in {"__pycache__", ".git", ".ssh", ".gnupg"} for part in parts)


def _normalize_repository(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if normalized.lower().endswith(".git"):
        normalized = normalized[:-4]
    return normalized.lower()


def _validate_release_source(source_tree: Path, tag: str, commit: str) -> None:
    if not source_tree.is_dir():
        raise ReleaseError(f"Source tree does not exist: {source_tree}")
    if not SAFE_TAG.fullmatch(tag):
        raise ReleaseError("Tag must contain only portable ref characters")
    if tag != RELEASE_TAG:
        raise ReleaseError(f"Tag must be exactly {RELEASE_TAG}")
    if not FULL_COMMIT.fullmatch(commit):
        raise ReleaseError("Commit must be a full lowercase 40-character SHA-1")

    root = Path(str(_run_git(source_tree, "rev-parse", "--show-toplevel")).strip()).resolve()
    if root != source_tree:
        raise ReleaseError(f"Source tree must be a dedicated Git root: {source_tree}")
    if root != SOURCE_ROOT.resolve():
        raise ReleaseError("Release builder must execute from the pinned source tree")
    origin = str(_run_git(source_tree, "remote", "get-url", "origin")).strip()
    if _normalize_repository(origin) != _normalize_repository(OFFICIAL_REPOSITORY):
        raise ReleaseError(f"Unexpected origin; expected {OFFICIAL_REPOSITORY}")

    commit_object = str(_run_git(source_tree, "rev-parse", f"{commit}^{{commit}}")).strip().lower()
    if commit_object != commit:
        raise ReleaseError(f"Commit did not resolve exactly: {commit_object}")
    tag_commit = str(_run_git(source_tree, "rev-parse", f"refs/tags/{tag}^{{commit}}")).strip().lower()
    if tag_commit != commit:
        raise ReleaseError(f"Tag {tag} resolves to {tag_commit}, not {commit}")
    head_commit = str(_run_git(source_tree, "rev-parse", "HEAD")).strip().lower()
    if head_commit != commit:
        raise ReleaseError(f"Release checkout HEAD is {head_commit}, not {commit}")
    if str(_run_git(source_tree, "status", "--porcelain", "--untracked-files=all")).strip():
        raise ReleaseError("Release checkout must be clean")

    builder_relative = Path(__file__).resolve().relative_to(source_tree).as_posix()
    pinned_builder = _run_git(source_tree, "show", f"{commit}:{builder_relative}", binary=True)
    assert isinstance(pinned_builder, bytes)
    working_builder = Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
    if pinned_builder.replace(b"\r\n", b"\n") != working_builder:
        raise ReleaseError("Release builder does not match the pinned commit")

    manifest_raw = _run_git(
        source_tree,
        "show",
        f"{commit}:plugins/codex-routecraft/.codex-plugin/plugin.json",
        binary=True,
    )
    assert isinstance(manifest_raw, bytes)
    try:
        plugin = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("Pinned plugin manifest is not valid UTF-8 JSON") from exc
    plugin_version = str(plugin.get("version", "")) if isinstance(plugin, dict) else ""
    if not PLUGIN_RELEASE_VERSION.fullmatch(plugin_version):
        raise ReleaseError(f"Pinned plugin version must match {VERSION}+codex.<timestamp>; found {plugin_version or '<missing>'}")


def _source_entries(source_tree: Path, commit: str) -> list[Entry]:
    output = _run_git(source_tree, "ls-tree", "-rz", "--full-tree", commit, binary=True)
    assert isinstance(output, bytes)
    entries: list[Entry] = []
    seen: set[str] = set()
    seen_casefold: dict[str, str] = {}
    for raw in output.split(b"\0"):
        if not raw:
            continue
        try:
            metadata, encoded_path = raw.split(b"\t", 1)
            mode_text, object_type, object_id = metadata.decode("ascii").split(" ", 2)
            relative = encoded_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ReleaseError("Pinned Git tree contains an unsupported entry") from exc
        if object_type != "blob" or mode_text not in {"100644", "100755"}:
            raise ReleaseError(f"Pinned Git tree contains unsupported mode/type: {relative}")
        archive_path = _safe_archive_path(f"{PREFIX}-source/{relative}")
        if _forbidden_member(archive_path):
            raise ReleaseError(f"Forbidden source payload: {relative}")
        if archive_path in seen:
            raise ReleaseError(f"Duplicate source path: {archive_path}")
        folded = archive_path.casefold()
        if folded in seen_casefold:
            raise ReleaseError(
                f"Case-insensitive source path collision: {seen_casefold[folded]} and {archive_path}"
            )
        seen.add(archive_path)
        seen_casefold[folded] = archive_path
        data = _run_git(source_tree, "cat-file", "blob", object_id, binary=True)
        assert isinstance(data, bytes)
        entries.append(Entry(archive_path, data, 0o755 if mode_text == "100755" else 0o644))
    if not entries:
        raise ReleaseError("Pinned Git tree is empty")
    return entries


def _pinned_file(source_tree: Path, commit: str, relative: str) -> bytes:
    value = _run_git(source_tree, "show", f"{commit}:{relative}", binary=True)
    assert isinstance(value, bytes)
    return value


def _render_template(source_tree: Path, name: str, tag: str, commit: str) -> bytes:
    try:
        text = _pinned_file(source_tree, commit, f"release/runtime/{name}").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseError(f"Pinned release template is not UTF-8: {name}") from exc
    replacements = dict(TEXT_TOKENS)
    replacements["@ROUTECRAFT_TAG@"] = tag
    replacements["@ROUTECRAFT_COMMIT@"] = commit
    for token, value in replacements.items():
        text = text.replace(token, value)
    if re.search(r"@ROUTECRAFT_[A-Z_]+@", text):
        raise ReleaseError(f"Unresolved release token in {name}")
    return text.replace("\r\n", "\n").encode("utf-8")


def _starter_entries(source_tree: Path, platform_name: str, tag: str, commit: str) -> list[Entry]:
    if platform_name not in {"windows", "macos"}:
        raise ReleaseError(f"Unsupported platform: {platform_name}")
    root = f"{PREFIX}-{platform_name}"
    pin = {
        "schema_version": 1,
        "product": PRODUCT,
        "version": VERSION,
        "repository": OFFICIAL_REPOSITORY,
        "tag": tag,
        "commit": commit,
        "codex_cli_version": TESTED_CODEX_CLI_VERSION,
        "control_center_included": False,
        "decision_store_included": False,
        "memory_local_included": False,
    }
    entries = [
        Entry(f"{root}/README-JA.md", _render_template(source_tree, "README-JA.md", tag, commit)),
        Entry(f"{root}/LICENSE", _pinned_file(source_tree, commit, "LICENSE")),
        Entry(
            f"{root}/release-pin.json",
            (json.dumps(pin, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        ),
    ]
    if platform_name == "windows":
        entries.append(Entry(f"{root}/install-routecraft.ps1", _render_template(source_tree, "install-routecraft.ps1", tag, commit)))
    else:
        entries.append(
            Entry(
                f"{root}/install-routecraft.sh",
                _render_template(source_tree, "install-routecraft.sh", tag, commit),
                0o755,
            )
        )
    return entries


def _assert_secret_free(entries: Iterable[Entry], source_tree: Path) -> None:
    local_markers = {
        str(source_tree),
        source_tree.as_posix(),
        str(Path.home()),
        Path.home().as_posix(),
    }
    markers = {value.encode("utf-8") for value in local_markers if len(value) >= 4}
    for entry in entries:
        if _forbidden_member(entry.archive_path):
            raise ReleaseError(f"Forbidden release member: {entry.archive_path}")
        for marker in markers:
            if marker in entry.data:
                raise ReleaseError(f"Local absolute path leaked into {entry.archive_path}")
        if any(pattern.search(entry.data) for pattern in SECRET_SIGNATURES):
            raise ReleaseError(f"Secret-like content detected in {entry.archive_path}")


def _write_archive(target: Path, entries: Iterable[Entry]) -> dict[str, object]:
    ordered = sorted(entries, key=lambda item: item.archive_path)
    if len({entry.archive_path for entry in ordered}) != len(ordered):
        raise ReleaseError(f"Duplicate archive member in {target.name}")
    folded: dict[str, str] = {}
    for entry in ordered:
        archive_path = _safe_archive_path(entry.archive_path)
        key = archive_path.casefold()
        if key in folded:
            raise ReleaseError(
                f"Case-insensitive archive member collision in {target.name}: "
                f"{folded[key]} and {archive_path}"
            )
        folded[key] = archive_path
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        # Stored members avoid platform/zlib-version differences in release bytes.
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for entry in ordered:
                archive_path = _safe_archive_path(entry.archive_path)
                info = zipfile.ZipInfo(archive_path, FIXED_TIME)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | entry.mode) << 16
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, entry.data)
        with zipfile.ZipFile(temporary, "r") as archive:
            bad = archive.testzip()
            if bad:
                raise ReleaseError(f"Corrupt archive member: {bad}")
            if archive.namelist() != sorted(archive.namelist()):
                raise ReleaseError(f"Archive order is not deterministic: {target.name}")
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {
        "file": target.name,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "bytes": target.stat().st_size,
    }


def _write_text_atomic(target: Path, content: str, *, encoding: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_output_target(source_tree: Path, output_dir: Path) -> None:
    if output_dir == source_tree or source_tree in output_dir.parents:
        raise ReleaseError("Output directory must be outside the pinned source tree")
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ReleaseError(f"Output path is not a directory: {output_dir}")
        if any(output_dir.iterdir()):
            raise ReleaseError(f"Output directory must be empty: {output_dir}")


def build(source_tree: Path, output_dir: Path, tag: str, commit: str) -> dict[str, object]:
    source_tree = source_tree.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    commit = commit.strip().lower()
    tag = tag.strip()
    _validate_release_source(source_tree, tag, commit)
    _validate_output_target(source_tree, output_dir)

    bundles: list[tuple[str, str, list[Entry]]] = [
        ("windows", "starter", _starter_entries(source_tree, "windows", tag, commit)),
        ("macos", "starter", _starter_entries(source_tree, "macos", tag, commit)),
        ("source", "source", _source_entries(source_tree, commit)),
    ]
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{PREFIX}-stage-", dir=output_dir.parent) as staging_name:
        staging = Path(staging_name)
        artifacts: list[dict[str, object]] = []
        for platform_name, artifact_type, entries in bundles:
            _assert_secret_free(entries, source_tree)
            target = staging / f"{PREFIX}-{platform_name}.zip"
            artifact = _write_archive(target, entries)
            artifact["platform"] = platform_name
            artifact["type"] = artifact_type
            artifacts.append(artifact)
        artifacts.sort(key=lambda item: str(item["file"]))

        checksums = "".join(f"{item['sha256']}  {item['file']}\n" for item in artifacts)
        _write_text_atomic(staging / "SHA256SUMS.txt", checksums, encoding="ascii")
        manifest: dict[str, object] = {
            "schema_version": 1,
            "product": PRODUCT,
            "version": VERSION,
            "source": {
                "repository": OFFICIAL_REPOSITORY,
                "tag": tag,
                "commit": commit,
            },
            "requirements": {
                "codex_cli": {
                    "tested_version": TESTED_CODEX_CLI_VERSION,
                    "required_commands": ["plugin list", "plugin marketplace list", "plugin add", "sandbox"],
                },
                "git": True,
                "python": ">=3.11,<4",
            },
            "privacy": {
                "credentials_included": False,
                "private_decision_store_included": False,
                "graph_state_included": False,
                "memory_database_included": False,
                "absolute_device_paths_included": False,
            },
            "product_boundaries": {
                "control_center_included": False,
                "control_center_required": False,
                "memory_local_version": "1.0.0",
                "memory_local_changed": False,
            },
            "installation": {
                "starter_requires_network": True,
                "runtime_offline_first": True,
                "control_center_optional": True,
            },
            "reproducible": True,
            "zip_compression": "stored",
            "zip_timestamp": "2026-08-25T00:00:00Z",
            "signed": False,
            "notarized": False,
            "license": "MIT",
            "published": False,
            "artifacts": artifacts,
        }
        _write_text_atomic(
            staging / "release-manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if output_dir.exists():
            output_dir.rmdir()  # validated empty; replacement below publishes the complete set at once
        os.replace(staging, output_dir)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build pinned RouteCraft Runtime 0.7.4 starter packages")
    parser.add_argument("--source-tree", required=True, help="Dedicated official RouteCraft Git checkout")
    parser.add_argument("--output-dir", required=True, help="Local output directory; no publication is performed")
    parser.add_argument("--tag", required=True, help="Existing immutable release tag")
    parser.add_argument("--commit", required=True, help="Expected full 40-character release commit")
    args = parser.parse_args(argv)
    try:
        manifest = build(Path(args.source_tree), Path(args.output_dir), args.tag, args.commit)
    except ReleaseError as exc:
        parser.error(str(exc))
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
