from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
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

    def test_normalize_remote_accepts_common_github_forms(self) -> None:
        values = {
            DEVICE.normalize_remote("https://github.com/Example/Repo.git"),
            DEVICE.normalize_remote("git@github.com:example/repo.git"),
            DEVICE.normalize_remote("ssh://git@github.com/example/repo"),
        }
        self.assertEqual(values, {"github:example/repo"})

    def test_shared_config_is_portable_and_strict(self) -> None:
        payload = DEVICE.shared_config_payload(
            source_remote="https://github.com/tamas-hub/codex-routecraft.git",
            source_branch="main",
            memory_remote="https://github.com/example/routecraft-memory-private.git",
            memory_branch="main",
        )
        self.assertEqual(payload["source"]["local_path"], "~/codex-routecraft")
        self.assertEqual(payload["memory"]["local_path"], "~/routecraft-memory")
        self.assertEqual(payload["memory"]["auto_sync"], "both")
        self.assertEqual(payload["policy"]["source_of_truth"], "github")
        rendered = json.dumps(payload)
        self.assertNotIn("C:\\\\", rendered)
        self.assertNotIn("/Users/", rendered)

        store = self.base / "shared"
        store.mkdir()
        kwargs = {
            "source_remote": payload["source"]["repository"],
            "source_branch": payload["source"]["branch"],
            "memory_remote": payload["memory"]["repository"],
            "memory_branch": payload["memory"]["branch"],
        }
        self.assertEqual(DEVICE.ensure_shared_config(store, **kwargs), "created")
        self.assertEqual(DEVICE.ensure_shared_config(store, **kwargs), "verified")

        path = store / DEVICE.SHARED_CONFIG_NAME
        modified = json.loads(path.read_text(encoding="utf-8"))
        modified["token"] = "must-not-be-stored"
        path.write_text(json.dumps(modified), encoding="utf-8")
        with self.assertRaises(DEVICE.FleetError):
            DEVICE.ensure_shared_config(store, **kwargs)

    def test_memory_sync_tracks_shared_fleet_config(self) -> None:
        store = self.base / "store"
        self.run_memory("init", "--store", str(store), "--git-init")
        DEVICE.ensure_shared_config(
            store,
            source_remote="https://github.com/tamas-hub/codex-routecraft.git",
            source_branch="main",
            memory_remote="https://github.com/example/routecraft-memory-private.git",
            memory_branch="main",
        )

        validation = self.run_memory("validate", "--store", str(store))
        self.assertIn("validation OK", validation.stdout)
        sync = json.loads(self.run_memory("sync", "--store", str(store), "--mode", "both").stdout)
        self.assertIsNotNone(sync["committed"])

        tracked = subprocess.run(
            ["git", "-C", str(store), "ls-files", DEVICE.SHARED_CONFIG_NAME],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        self.assertEqual(tracked, DEVICE.SHARED_CONFIG_NAME)

        status = json.loads(self.run_memory("status", "--store", str(store), "--json").stdout)
        self.assertFalse(status["git"]["dirty"])
        self.assertTrue(status["git"]["dedicated_root"])


if __name__ == "__main__":
    unittest.main()
