from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEVICE_SCRIPT = ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_device.py"
MEMORY_SCRIPT = ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_memory.py"

SPEC = importlib.util.spec_from_file_location("routecraft_device", DEVICE_SCRIPT)
assert SPEC and SPEC.loader
DEVICE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DEVICE
SPEC.loader.exec_module(DEVICE)


class RouteCraftFleetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config = self.base / "memory-config.json"
        self.env = os.environ.copy()
        self.env["ROUTECRAFT_MEMORY_CONFIG"] = str(self.config)
        self.env["ROUTECRAFT_DEVICE_ID"] = "fleettest"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_memory(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(MEMORY_SCRIPT), *args],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                f"memory command failed: {args}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def payload(self) -> dict:
        return DEVICE.fleet_payload(
            source_remote="https://github.com/tamas-hub/codex-routecraft.git",
            source_branch="main",
            memory_remote="https://github.com/example/routecraft-memory-private.git",
            memory_branch="main",
        )

    def test_normalize_remote_accepts_common_github_forms(self) -> None:
        values = {
            DEVICE.normalize_remote("https://github.com/Example/Repo.git"),
            DEVICE.normalize_remote("git@github.com:example/repo.git"),
            DEVICE.normalize_remote("ssh://git@github.com/example/repo"),
        }
        self.assertEqual(values, {"github:example/repo"})

    def test_fleet_payload_is_portable_and_non_device_specific(self) -> None:
        payload = self.payload()
        self.assertEqual(payload["source"]["local_path"], "~/codex-routecraft")
        self.assertEqual(payload["memory"]["local_path"], "~/routecraft-memory")
        self.assertEqual(payload["memory"]["auto_sync"], "both")
        self.assertEqual(payload["policy"]["source_of_truth"], "github")
        rendered = json.dumps(payload)
        self.assertNotIn("C:\\\\", rendered)
        self.assertNotIn("/Users/", rendered)
        self.assertNotIn("device_id", rendered)

    def test_fleet_config_lives_in_tracked_store_sentinel(self) -> None:
        store = self.base / "store"
        self.run_memory("init", "--store", str(store), "--git-init")

        self.assertEqual(DEVICE.ensure_fleet_config(store, self.payload()), "created")
        self.assertEqual(DEVICE.ensure_fleet_config(store, self.payload()), "verified")

        sentinel = json.loads((store / ".routecraft-store.json").read_text(encoding="utf-8"))
        self.assertEqual(sentinel["fleet"]["layout_version"], 1)
        self.assertEqual(sentinel["fleet"]["source"]["local_path"], "~/codex-routecraft")

        validation = self.run_memory("validate", "--store", str(store))
        self.assertIn("validation OK", validation.stdout)
        sync = json.loads(self.run_memory("sync", "--store", str(store), "--mode", "both").stdout)
        self.assertIsNotNone(sync["committed"])

        tracked = subprocess.run(
            ["git", "-C", str(store), "ls-files", ".routecraft-store.json"],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        self.assertEqual(tracked, ".routecraft-store.json")

        status = json.loads(self.run_memory("status", "--store", str(store), "--json").stdout)
        self.assertFalse(status["git"]["dirty"])
        self.assertTrue(status["git"]["dedicated_root"])

    def test_fleet_config_rejects_unexpected_shared_fields(self) -> None:
        store = self.base / "strict-store"
        self.run_memory("init", "--store", str(store))
        DEVICE.ensure_fleet_config(store, self.payload())

        sentinel_path = store / ".routecraft-store.json"
        sentinel = json.loads(sentinel_path.read_text(encoding="utf-8"))
        sentinel["fleet"]["token"] = "must-not-be-stored"
        sentinel_path.write_text(json.dumps(sentinel), encoding="utf-8")

        with self.assertRaises(DEVICE.FleetError):
            DEVICE.ensure_fleet_config(store, self.payload())

    def test_source_guard_config_is_local_private_and_has_no_secret_fields(self) -> None:
        codex_home = self.base / "codex-home"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            target = DEVICE.source_control_config("example-owner", True)
            self.assertIsNotNone(target)
            assert target is not None
            value = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(value["provider"], "github")
        self.assertEqual(value["default_visibility"], "private")
        self.assertFalse(value["allow_force_push"])
        self.assertFalse(value["store_raw_transcripts"])
        self.assertNotIn("token", value)
        self.assertNotIn("password", value)

    def test_source_guard_requires_valid_owner_when_enabled(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.base / "codex-home-invalid")}):
            with self.assertRaises(DEVICE.FleetError):
                DEVICE.source_control_config("invalid owner", True)

    def test_windows_codex_shim_resolves_to_packaged_native_executable(self) -> None:
        npm = self.base / "npm"
        shim = npm / "codex.cmd"
        native = (
            npm
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )
        native.parent.mkdir(parents=True)
        shim.write_text("@echo off\n", encoding="utf-8")
        native.write_bytes(b"native")

        # Simulate only the Windows branch inside routecraft_device. Patching
        # os.name alone also changes pathlib.Path's factory process-wide on a
        # Linux CI runner, so pin DEVICE.Path to the native concrete path type
        # created before the patch.
        host_path_type = type(shim)
        with mock.patch.object(DEVICE.os, "name", "nt"), mock.patch.object(
            DEVICE, "Path", host_path_type
        ), mock.patch.object(DEVICE.shutil, "which", return_value=str(shim)):
            resolved = host_path_type(DEVICE.resolve_codex_executable())
            self.assertEqual(resolved, native.resolve())


if __name__ == "__main__":
    unittest.main()
