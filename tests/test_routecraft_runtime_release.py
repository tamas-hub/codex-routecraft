from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path, PurePosixPath
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build_runtime_release.py"


def load_builder(path: Path, module_name: str):
    # Execute a release-fixture builder without the import machinery writing a
    # __pycache__ entry into the very Git checkout whose clean-tree guard is
    # under test.  The production builder retains that guard unchanged.
    builder = types.ModuleType(module_name)
    builder.__file__ = str(path)
    builder.__package__ = ""
    sys.modules[module_name] = builder
    code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
    exec(code, builder.__dict__)  # routecraft-security: scanner-test-fixture
    return builder


ROOT_BUILDER = load_builder(BUILDER_PATH, "build_runtime_release_root")


class RouteCraftRuntimeReleaseTests(unittest.TestCase):
    def run_command(
        self,
        args: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        expected: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        self.assertEqual(expected, process.returncode, process.stdout + "\n" + process.stderr)
        return process

    def git(self, repository: Path, *args: str) -> str:
        return self.run_command(["git", "-C", str(repository), *args], cwd=repository).stdout.strip()

    def make_source(
        self,
        base: Path,
        *,
        version: str = "0.7.3+codex.20260825013909",
    ) -> tuple[Path, str, str, object]:
        source = base / "source"
        source.mkdir()
        self.run_command(["git", "init"], cwd=source)
        self.git(source, "config", "user.email", "release-test@example.invalid")
        self.git(source, "config", "user.name", "RouteCraft Release Test")
        self.git(source, "remote", "add", "origin", ROOT_BUILDER.OFFICIAL_REPOSITORY)
        fixture_builder = source / "scripts" / "build_runtime_release.py"
        fixture_builder.parent.mkdir(parents=True)
        shutil.copyfile(BUILDER_PATH, fixture_builder)
        fixture_templates = source / "release" / "runtime"
        fixture_templates.mkdir(parents=True)
        for name in ("README-JA.md", "install-routecraft.ps1", "install-routecraft.sh"):
            shutil.copyfile(ROOT / "release" / "runtime" / name, fixture_templates / name)
        (fixture_templates / "install-routecraft.sh").chmod(0o755)
        shutil.copyfile(ROOT / "LICENSE", source / "LICENSE")
        manifest = source / "plugins" / "codex-routecraft" / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": "codex-routecraft", "version": version}) + "\n", encoding="utf-8")
        scripts = source / "scripts"
        (scripts / "verify.py").write_text("print('verified')\n", encoding="utf-8")
        setup = scripts / "setup-local.sh"
        setup.write_text("#!/usr/bin/env sh\nset -eu\necho installed\n", encoding="utf-8")
        setup.chmod(0o755)
        (scripts / "setup-local.ps1").write_text("Write-Host 'installed'\n", encoding="utf-8")
        device = source / "plugins" / "codex-routecraft" / "scripts" / "routecraft_device.py"
        device.parent.mkdir(parents=True, exist_ok=True)
        device.write_text("print('fixture routecraft-device')\n", encoding="utf-8")
        (source / "README.md").write_text("# Fake pinned RouteCraft source\n", encoding="utf-8")
        self.git(source, "add", ".")
        self.git(source, "update-index", "--chmod=+x", "scripts/setup-local.sh")
        self.git(source, "update-index", "--chmod=+x", "release/runtime/install-routecraft.sh")
        self.git(source, "commit", "-m", "release fixture")
        commit = self.git(source, "rev-parse", "HEAD")
        tag = "v0.7.3"
        self.git(source, "tag", tag)
        builder = load_builder(fixture_builder, f"build_runtime_release_fixture_{id(base)}")
        self.assertEqual("", self.git(source, "status", "--porcelain", "--untracked-files=all"))
        return source.resolve(), tag, commit, builder

    def commit_and_retag(self, source: Path, tag: str, message: str) -> str:
        self.git(source, "tag", "-d", tag)
        self.git(source, "add", "-f", ".")
        self.git(source, "commit", "-m", message)
        commit = self.git(source, "rev-parse", "HEAD")
        self.git(source, "tag", tag)
        return commit

    def test_build_is_deterministic_pinned_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, tag, commit, builder = self.make_source(base)
            first = base / "first"
            second = base / "second"
            manifest1 = builder.build(source, first, tag, commit)
            manifest2 = builder.build(source, second, tag, commit)

            self.assertEqual(manifest1, manifest2)
            self.assertEqual("0.7.3", manifest1["version"])
            self.assertEqual(commit, manifest1["source"]["commit"])
            self.assertEqual("stored", manifest1["zip_compression"])
            self.assertEqual("0.148.0", manifest1["requirements"]["codex_cli"]["tested_version"])
            self.assertFalse(manifest1["privacy"]["credentials_included"])
            self.assertFalse(manifest1["product_boundaries"]["control_center_included"])
            self.assertFalse(manifest1["product_boundaries"]["control_center_required"])
            self.assertTrue(manifest1["installation"]["starter_requires_network"])
            self.assertTrue(manifest1["installation"]["runtime_offline_first"])
            self.assertEqual("1.0.0", manifest1["product_boundaries"]["memory_local_version"])
            self.assertFalse(manifest1["product_boundaries"]["memory_local_changed"])

            expected_names = {
                "routecraft-runtime-0.7.3-windows.zip",
                "routecraft-runtime-0.7.3-macos.zip",
                "routecraft-runtime-0.7.3-source.zip",
            }
            self.assertEqual(expected_names, {item["file"] for item in manifest1["artifacts"]})
            for name in expected_names | {"SHA256SUMS.txt", "release-manifest.json"}:
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)

            checksums: dict[str, str] = {}
            for line in (first / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines():
                digest, filename = line.split("  ", 1)
                checksums[filename] = digest
            self.assertEqual(expected_names, set(checksums))

            for artifact in manifest1["artifacts"]:
                archive_path = first / artifact["file"]
                self.assertEqual(artifact["sha256"], hashlib.sha256(archive_path.read_bytes()).hexdigest())
                self.assertEqual(artifact["sha256"], checksums[artifact["file"]])
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(sorted(archive.namelist()), archive.namelist())
                    self.assertTrue(any(name.endswith("/LICENSE") for name in archive.namelist()))
                    for info in archive.infolist():
                        path = PurePosixPath(info.filename)
                        self.assertFalse(path.is_absolute())
                        self.assertNotIn("..", path.parts)
                        self.assertEqual(builder.FIXED_TIME, info.date_time)
                        self.assertEqual(zipfile.ZIP_STORED, info.compress_type)
                        lowered = info.filename.lower()
                        self.assertNotIn("/.git/", lowered)
                        self.assertFalse(lowered.endswith(("/.env", "/auth.json", "/.sandbox-secrets")))
                        self.assertFalse(lowered.endswith((".db", ".sqlite", ".sqlite3", ".pem", ".key")))
                    combined = b"\n".join(archive.read(name) for name in archive.namelist())
                    self.assertNotIn(str(source).encode("utf-8"), combined)
                    self.assertNotIn(str(Path.home()).encode("utf-8"), combined)

            windows = first / "routecraft-runtime-0.7.3-windows.zip"
            with zipfile.ZipFile(windows) as archive:
                installer = next(name for name in archive.namelist() if name.endswith("/install-routecraft.ps1"))
                mode = archive.getinfo(installer).external_attr >> 16
                self.assertEqual(0o644, mode & 0o777)
                content = archive.read(installer).decode("utf-8")
                self.assertIn(builder.OFFICIAL_REPOSITORY, content)
                self.assertIn(tag, content)
                self.assertIn(commit, content)
                self.assertIn("-Confirm INSTALL", content)
                self.assertIn("routecraft_device.py", content)
                self.assertIn("'--expected-commit', $ExpectedCommit", content)
                self.assertIn("$RequiredCodexCliVersion = '0.148.0'", content)
                self.assertIn('$Required = "codex-cli $RequiredCodexCliVersion"', content)
                self.assertIn("Restore-OriginalCheckout", content)
                self.assertNotIn("setup-local.ps1", content)
                self.assertNotIn("@ROUTECRAFT_", content)
                readme = next(name for name in archive.namelist() if name.endswith("/README-JA.md"))
                readme_content = archive.read(readme).decode("utf-8")
                self.assertIn("Unblock-File", readme_content)
                self.assertIn("codex-cli 0.148.0", readme_content)
                release_pin = next(name for name in archive.namelist() if name.endswith("/release-pin.json"))
                self.assertEqual("0.148.0", json.loads(archive.read(release_pin))["codex_cli_version"])

            macos = first / "routecraft-runtime-0.7.3-macos.zip"
            with zipfile.ZipFile(macos) as archive:
                installer = next(name for name in archive.namelist() if name.endswith("/install-routecraft.sh"))
                info = archive.getinfo(installer)
                self.assertEqual(3, info.create_system)
                self.assertEqual(0o755, (info.external_attr >> 16) & 0o777)
                content = archive.read(installer).decode("utf-8")
                self.assertIn("--apply --confirm INSTALL", content)
                self.assertIn("routecraft_device.py", content)
                self.assertIn('--expected-commit "$EXPECTED_COMMIT"', content)
                self.assertIn("codex-cli $REQUIRED_CODEX_CLI_VERSION", content)
                self.assertIn("restore_on_exit", content)
                self.assertNotIn("setup-local.sh", content)
                self.assertLess(content.index("resolved_commit="), content.index('python3 "$VERIFY"'))

            source_archive = first / "routecraft-runtime-0.7.3-source.zip"
            with zipfile.ZipFile(source_archive) as archive:
                names = archive.namelist()
                self.assertTrue(any(name.endswith("/plugins/codex-routecraft/.codex-plugin/plugin.json") for name in names))
                setup = next(name for name in names if name.endswith("/scripts/setup-local.sh"))
                self.assertEqual(0o755, (archive.getinfo(setup).external_attr >> 16) & 0o777)
                self.assertTrue(any(name.endswith("/LICENSE") for name in names))

    def test_builder_rejects_untrusted_or_unpinned_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, tag, commit, builder = self.make_source(base)
            with self.assertRaisesRegex(builder.ReleaseError, "40-character"):
                builder.build(source, base / "short", tag, commit[:12])
            with self.assertRaisesRegex(builder.ReleaseError, "portable ref"):
                builder.build(source, base / "unsafe-tag", "../v0.7.3", commit)
            with self.assertRaisesRegex(builder.ReleaseError, "exactly v0.7.3"):
                builder.build(source, base / "wrong-tag", "v0.7.3-rc1", commit)

            self.git(source, "remote", "set-url", "origin", "https://example.invalid/not-routecraft.git")
            with self.assertRaisesRegex(builder.ReleaseError, "Unexpected origin"):
                builder.build(source, base / "wrong-origin", tag, commit)

    def test_builder_rejects_wrong_plugin_version_and_tag_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, tag, commit, builder = self.make_source(base, version="0.7.0")
            with self.assertRaisesRegex(builder.ReleaseError, "plugin version"):
                builder.build(source, base / "wrong-version", tag, commit)

            self.git(source, "tag", "-d", tag)
            (source / "README.md").write_text("second commit\n", encoding="utf-8")
            self.git(source, "add", "README.md")
            self.git(source, "commit", "-m", "second")
            self.git(source, "tag", tag)
            with self.assertRaisesRegex(builder.ReleaseError, "resolves to"):
                builder.build(source, base / "moved-tag", tag, commit)

    def test_builder_rejects_dirty_source_secret_payloads_and_case_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, tag, commit, builder = self.make_source(base)
            dirty = source / "untracked-local.txt"
            dirty.write_text("local only\n", encoding="utf-8")
            with self.assertRaisesRegex(builder.ReleaseError, "must be clean"):
                builder.build(source, base / "dirty-output", tag, commit)
            dirty.unlink()

            secret_file = source / ".env.local"
            secret_file.write_text("TOKEN=not-a-real-token\n", encoding="utf-8")
            commit = self.commit_and_retag(source, tag, "add forbidden secret filename")
            with self.assertRaisesRegex(builder.ReleaseError, "Forbidden source payload"):
                builder.build(source, base / "secret-name-output", tag, commit)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, tag, commit, builder = self.make_source(base)
            (source / "notes.txt").write_text("ghp_" + "A" * 40 + "\n", encoding="utf-8")
            commit = self.commit_and_retag(source, tag, "add secret-like content")
            with self.assertRaisesRegex(builder.ReleaseError, "Secret-like content"):
                builder.build(source, base / "secret-content-output", tag, commit)

            with self.assertRaisesRegex(builder.ReleaseError, "Case-insensitive archive member collision"):
                builder._write_archive(
                    base / "collision.zip",
                    [
                        builder.Entry("root/Config.json", b"one"),
                        builder.Entry("root/config.json", b"two"),
                    ],
                )

    def test_builder_publishes_atomically_and_refuses_mixed_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, tag, commit, builder = self.make_source(base)
            mixed = base / "mixed"
            mixed.mkdir()
            marker = mixed / "keep.txt"
            marker.write_text("preserve\n", encoding="utf-8")
            with self.assertRaisesRegex(builder.ReleaseError, "must be empty"):
                builder.build(source, mixed, tag, commit)
            self.assertEqual("preserve\n", marker.read_text(encoding="utf-8"))

            partial = base / "partial"
            real_write = builder._write_archive
            calls = 0

            def fail_second_archive(target, entries):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise builder.ReleaseError("injected archive failure")
                return real_write(target, entries)

            with mock.patch.object(builder, "_write_archive", side_effect=fail_second_archive):
                with self.assertRaisesRegex(builder.ReleaseError, "injected archive failure"):
                    builder.build(source, partial, tag, commit)
            self.assertFalse(partial.exists())

            with self.assertRaisesRegex(builder.ReleaseError, "outside the pinned source"):
                builder.build(source, source / "release-output", tag, commit)

    def test_starter_plan_is_non_mutating_and_apply_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, tag, commit, builder = self.make_source(base)
            output = base / "output"
            builder.build(source, output, tag, commit)

            windows_extract = base / "windows"
            with zipfile.ZipFile(output / "routecraft-runtime-0.7.3-windows.zip") as archive:
                archive.extractall(windows_extract)
            windows_root = next(windows_extract.iterdir())
            destination = base / "windows-destination"
            powershell = shutil.which("powershell") or shutil.which("pwsh")
            if powershell and os.name == "nt":
                fake_bin = base / "windows-fake-bin"
                fake_bin.mkdir()
                (fake_bin / "codex.cmd").write_text(
                    "@echo off\r\necho codex-cli %ROUTECRAFT_TEST_CODEX_VERSION%\r\nexit /b 0\r\n",
                    encoding="ascii",
                )
                (fake_bin / "git.cmd").write_text(
                    "@echo off\r\n"
                    '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
                    '-NoProfile -ExecutionPolicy Bypass -File "%~dp0fake-git.ps1" %*\r\n'
                    "exit /b %errorlevel%\r\n",
                    encoding="ascii",
                )
                (fake_bin / "fake-git.ps1").write_text(
                    """$GitArgs = @($args)
function Set-TestHead {
    param([string]$Value)
    if ($env:ROUTECRAFT_TEST_GIT_STATE) {
        [System.IO.File]::WriteAllText($env:ROUTECRAFT_TEST_GIT_STATE, $Value)
    }
}
function Get-TestHead {
    if ($env:ROUTECRAFT_TEST_GIT_STATE -and (Test-Path -LiteralPath $env:ROUTECRAFT_TEST_GIT_STATE)) {
        return ([System.IO.File]::ReadAllText($env:ROUTECRAFT_TEST_GIT_STATE)).Trim()
    }
    return $env:ROUTECRAFT_TEST_COMMIT
}
if ($GitArgs.Count -gt 0 -and $GitArgs[0] -eq 'clone') {
    $Destination = $GitArgs[-1]
    New-Item -ItemType Directory -Force -Path (Join-Path $Destination '.git') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $Destination 'scripts') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $Destination 'plugins\\codex-routecraft\\scripts') | Out-Null
    'print("verified")' | Set-Content -LiteralPath (Join-Path $Destination 'scripts\\verify.py') -Encoding utf8
    @'
import json
import os
import sys

if "apply" in sys.argv:
    if os.environ.get("ROUTECRAFT_TEST_FAIL_APPLY") == "1":
        raise SystemExit(7)
    with open(os.environ["ROUTECRAFT_TEST_SETUP_MARKER"], "w", encoding="utf-8") as handle:
        handle.write("installed")
print(json.dumps({"ok": True, "transaction_id": "install-fixture"}))
'@ | Set-Content -LiteralPath (Join-Path $Destination 'plugins\\codex-routecraft\\scripts\\routecraft_device.py') -Encoding utf8
    Set-TestHead $env:ROUTECRAFT_TEST_COMMIT
    exit 0
}
if ($GitArgs.Count -gt 2 -and $GitArgs[0] -eq '-C') {
    if ($GitArgs[2] -eq 'remote') {
        Write-Output $env:ROUTECRAFT_TEST_OFFICIAL_REPOSITORY
        exit 0
    }
    if ($GitArgs[2] -eq 'rev-parse') {
        if ($GitArgs[3] -eq '--show-toplevel') {
            Write-Output $GitArgs[1]
        } elseif ($GitArgs[3] -eq 'HEAD') {
            Write-Output (Get-TestHead)
        } else {
            Write-Output $env:ROUTECRAFT_TEST_COMMIT
        }
        exit 0
    }
    if ($GitArgs[2] -eq 'symbolic-ref') {
        if ($env:ROUTECRAFT_TEST_ORIGINAL_BRANCH) {
            Write-Output $env:ROUTECRAFT_TEST_ORIGINAL_BRANCH
            exit 0
        }
        exit 1
    }
    if ($GitArgs[2] -eq 'checkout') {
        if ($GitArgs[3] -eq '--detach') {
            Set-TestHead $GitArgs[4]
        } else {
            Set-TestHead $env:ROUTECRAFT_TEST_ORIGINAL_COMMIT
        }
        exit 0
    }
    if ($GitArgs[2] -in @('fetch', 'status')) {
        exit 0
    }
}
Write-Error ('Unexpected fake Git invocation: ' + ($GitArgs -join ' '))
exit 9
""",
                    encoding="utf-8",
                )
                windows_launcher = fake_bin / "invoke-installer.ps1"
                windows_launcher.write_text(
                    """[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Apply')][string]$Mode = 'Plan',
    [string]$Confirm,
    [Parameter(Mandatory = $true)][string]$SourceDir
)
function global:git {
    & $env:ROUTECRAFT_TEST_POWERSHELL -NoProfile -ExecutionPolicy Bypass -File $env:ROUTECRAFT_TEST_FAKE_GIT @args
}
function global:codex {
    Write-Output ('codex-cli ' + $env:ROUTECRAFT_TEST_CODEX_VERSION)
    $global:LASTEXITCODE = 0
}
if ($PSBoundParameters.ContainsKey('Confirm')) {
    & $env:ROUTECRAFT_TEST_INSTALLER -Mode $Mode -Confirm $Confirm -SourceDir $SourceDir
} else {
    & $env:ROUTECRAFT_TEST_INSTALLER -Mode $Mode -SourceDir $SourceDir
}
""",
                    encoding="utf-8",
                )
                windows_env = os.environ.copy()
                windows_env["PATH"] = str(fake_bin) + os.pathsep + windows_env.get("PATH", "")
                windows_env["ROUTECRAFT_TEST_POWERSHELL"] = powershell
                windows_env["ROUTECRAFT_TEST_FAKE_GIT"] = str(fake_bin / "fake-git.ps1")
                windows_env["ROUTECRAFT_TEST_INSTALLER"] = str(windows_root / "install-routecraft.ps1")
                marker = base / "windows-setup.marker"
                windows_env["ROUTECRAFT_TEST_SETUP_MARKER"] = str(marker)
                windows_env["ROUTECRAFT_TEST_OFFICIAL_REPOSITORY"] = builder.OFFICIAL_REPOSITORY
                windows_env["ROUTECRAFT_TEST_COMMIT"] = commit
                windows_env["ROUTECRAFT_TEST_CODEX_VERSION"] = "0.148.0"
                windows_state = base / "windows-git-state.txt"
                windows_env["ROUTECRAFT_TEST_GIT_STATE"] = str(windows_state)
                plan = self.run_command(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(windows_launcher),
                        "-Mode",
                        "Plan",
                        "-SourceDir",
                        str(destination),
                    ],
                    cwd=windows_root,
                    env=windows_env,
                )
                self.assertTrue(
                    plan.stdout.strip(),
                    f"installer plan returned no JSON; stderr={plan.stderr!r}",
                )
                plan_data = json.loads(plan.stdout)
                self.assertEqual("plan", plan_data["mode"])
                self.assertEqual(commit, plan_data["expected_commit"])
                self.assertFalse(destination.exists())
                wrong_codex_env = windows_env.copy()
                wrong_codex_env["ROUTECRAFT_TEST_CODEX_VERSION"] = "0.147.0"
                wrong_codex = self.run_command(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(windows_launcher),
                        "-Mode",
                        "Plan",
                        "-SourceDir",
                        str(destination),
                    ],
                    cwd=windows_root,
                    env=wrong_codex_env,
                    expected=1,
                )
                self.assertIn("Codex CLI 0.148.0 is required", wrong_codex.stderr)
                self.assertFalse(destination.exists())
                refused = self.run_command(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(windows_launcher),
                        "-Mode",
                        "Apply",
                        "-SourceDir",
                        str(destination),
                    ],
                    cwd=windows_root,
                    env=windows_env,
                    expected=1,
                )
                self.assertIn("-Confirm INSTALL", refused.stderr)
                self.assertFalse(destination.exists())
                applied = self.run_command(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(windows_launcher),
                        "-Mode",
                        "Apply",
                        "-Confirm",
                        "INSTALL",
                        "-SourceDir",
                        str(destination),
                    ],
                    cwd=windows_root,
                    env=windows_env,
                )
                self.assertIn(f"RouteCraft 0.7.3 installed from {commit}", applied.stdout)
                self.assertEqual("installed", marker.read_text(encoding="utf-8-sig").strip())

                existing = base / "windows-existing"
                (existing / ".git").mkdir(parents=True)
                (existing / "scripts").mkdir()
                (existing / "plugins" / "codex-routecraft" / "scripts").mkdir(parents=True)
                shutil.copyfile(destination / "scripts" / "verify.py", existing / "scripts" / "verify.py")
                shutil.copyfile(
                    destination / "plugins" / "codex-routecraft" / "scripts" / "routecraft_device.py",
                    existing / "plugins" / "codex-routecraft" / "scripts" / "routecraft_device.py",
                )
                original_commit = "b" * 40
                restore_state = base / "windows-restore-state.txt"
                restore_state.write_text(original_commit, encoding="ascii")
                restore_env = windows_env.copy()
                restore_env["ROUTECRAFT_TEST_GIT_STATE"] = str(restore_state)
                restore_env["ROUTECRAFT_TEST_ORIGINAL_COMMIT"] = original_commit
                restore_env["ROUTECRAFT_TEST_ORIGINAL_BRANCH"] = "main"
                restore_env["ROUTECRAFT_TEST_FAIL_APPLY"] = "1"
                failed = self.run_command(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(windows_launcher),
                        "-Mode",
                        "Apply",
                        "-Confirm",
                        "INSTALL",
                        "-SourceDir",
                        str(existing),
                    ],
                    cwd=windows_root,
                    env=restore_env,
                    expected=1,
                )
                self.assertEqual(original_commit, restore_state.read_text(encoding="ascii").strip())
                self.assertIn("restored the existing RouteCraft checkout", failed.stdout + failed.stderr)

            macos_extract = base / "macos"
            with zipfile.ZipFile(output / "routecraft-runtime-0.7.3-macos.zip") as archive:
                archive.extractall(macos_extract)
            macos_root = next(macos_extract.iterdir())
            launcher = macos_root / "install-routecraft.sh"
            launcher.chmod(0o755)
            shell = shutil.which("sh")
            if not shell and os.name == "nt":
                for candidate in (
                    Path(r"C:\Program Files\Git\bin\bash.exe"),
                    Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
                ):
                    if candidate.is_file():
                        shell = str(candidate)
                        break
            if shell:
                fake_bin = base / "fake-bin"
                fake_bin.mkdir()
                fake_codex = fake_bin / "codex"
                fake_codex.write_text(
                    "#!/usr/bin/env sh\nprintf 'codex-cli %s\\n' \"$ROUTECRAFT_TEST_CODEX_VERSION\"\n",
                    encoding="utf-8",
                )
                fake_codex.chmod(0o755)
                env = os.environ.copy()
                env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
                env["ROUTECRAFT_TEST_CODEX_VERSION"] = "0.148.0"
                destination = base / "macos-destination"
                plan = self.run_command(
                    [shell, str(launcher), "--plan", "--source-dir", str(destination)],
                    cwd=macos_root,
                    env=env,
                )
                self.assertIn("RouteCraft Local Runtime 0.7.3 install plan", plan.stdout)
                self.assertIn(commit, plan.stdout)
                self.assertFalse(destination.exists())
                wrong_codex_env = env.copy()
                wrong_codex_env["ROUTECRAFT_TEST_CODEX_VERSION"] = "0.147.0"
                wrong_codex = self.run_command(
                    [shell, str(launcher), "--plan", "--source-dir", str(destination)],
                    cwd=macos_root,
                    env=wrong_codex_env,
                    expected=1,
                )
                self.assertIn("Codex CLI 0.148.0 is required", wrong_codex.stderr)
                self.assertFalse(destination.exists())
                refused = self.run_command(
                    [shell, str(launcher), "--apply", "--source-dir", str(destination)],
                    cwd=macos_root,
                    env=env,
                    expected=2,
                )
                self.assertIn("--confirm INSTALL", refused.stderr)
                self.assertFalse(destination.exists())

                fake_git = fake_bin / "git"
                fake_git.write_bytes(
                    """#!/usr/bin/env sh
set -eu
[ "$1" = "-C" ] || { echo 'unexpected fake git form' >&2; exit 9; }
repository=$2
command_name=$3
shift 3
case "$command_name" in
  remote) printf '%s\n' "$ROUTECRAFT_TEST_OFFICIAL_REPOSITORY" ;;
  status|fetch) exit 0 ;;
  rev-parse)
    if [ "$1" = "--show-toplevel" ]; then
      printf '%s\n' "$repository"
    elif [ "$1" = "HEAD" ]; then
      cat "$ROUTECRAFT_TEST_GIT_STATE"
    else
      printf '%s\n' "$ROUTECRAFT_TEST_COMMIT"
    fi
    ;;
  symbolic-ref) printf '%s\n' "$ROUTECRAFT_TEST_ORIGINAL_BRANCH" ;;
  checkout)
    if [ "$1" = "--detach" ]; then
      printf '%s' "$2" > "$ROUTECRAFT_TEST_GIT_STATE"
    else
      printf '%s' "$ROUTECRAFT_TEST_ORIGINAL_COMMIT" > "$ROUTECRAFT_TEST_GIT_STATE"
    fi
    ;;
  *) echo "unexpected fake git command: $command_name" >&2; exit 9 ;;
esac
""".encode("utf-8"),
                )
                fake_git.chmod(0o755)
                existing = base / "macos-existing"
                (existing / ".git").mkdir(parents=True)
                (existing / "scripts").mkdir()
                (existing / "plugins" / "codex-routecraft" / "scripts").mkdir(parents=True)
                (existing / "scripts" / "verify.py").write_text("print('verified')\n", encoding="utf-8")
                (existing / "plugins" / "codex-routecraft" / "scripts" / "routecraft_device.py").write_text(
                    """import json
import os
import sys

if "apply" in sys.argv and os.environ.get("ROUTECRAFT_TEST_FAIL_APPLY") == "1":
    raise SystemExit(7)
print(json.dumps({"ok": True, "transaction_id": "install-fixture"}))
""",
                    encoding="utf-8",
                )
                original_commit = "c" * 40
                restore_state = base / "macos-restore-state.txt"
                restore_state.write_text(original_commit, encoding="ascii")
                restore_env = env.copy()
                restore_env["ROUTECRAFT_TEST_OFFICIAL_REPOSITORY"] = builder.OFFICIAL_REPOSITORY
                restore_env["ROUTECRAFT_TEST_COMMIT"] = commit
                restore_env["ROUTECRAFT_TEST_GIT_STATE"] = str(restore_state).replace("\\", "/")
                restore_env["ROUTECRAFT_TEST_ORIGINAL_COMMIT"] = original_commit
                restore_env["ROUTECRAFT_TEST_ORIGINAL_BRANCH"] = "main"
                restore_env["ROUTECRAFT_TEST_FAIL_APPLY"] = "1"
                if os.name == "nt" and Path(shell).name.lower() == "bash.exe":
                    bash_env = base / "routecraft-test-bash-env.sh"
                    bash_env.write_bytes(
                        b'git() { "$ROUTECRAFT_TEST_SHELL" "$ROUTECRAFT_TEST_FAKE_GIT" "$@"; }\n'
                    )
                    restore_env["BASH_ENV"] = str(bash_env).replace("\\", "/")
                    restore_env["ROUTECRAFT_TEST_SHELL"] = str(shell).replace("\\", "/")
                    restore_env["ROUTECRAFT_TEST_FAKE_GIT"] = str(fake_git).replace("\\", "/")
                failed = self.run_command(
                    [
                        shell,
                        str(launcher),
                        "--apply",
                        "--confirm",
                        "INSTALL",
                        "--source-dir",
                        str(existing).replace("\\", "/"),
                    ],
                    cwd=macos_root,
                    env=restore_env,
                    expected=1,
                )
                self.assertEqual(original_commit, restore_state.read_text(encoding="ascii").strip())
                self.assertIn("restored the existing RouteCraft checkout", failed.stderr)


if __name__ == "__main__":
    unittest.main()
