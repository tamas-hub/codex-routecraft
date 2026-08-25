from __future__ import annotations

import importlib.util
import argparse
import hashlib
import json
import os
import shutil
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

    def test_public_version_matches_runtime_release(self) -> None:
        result = subprocess.run(
            [sys.executable, str(DEVICE_SCRIPT), "--version"],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

        self.assertEqual(result.stdout.strip(), "routecraft-device 0.7.3")
        self.assertEqual(result.stderr, "")

    def test_normalize_remote_accepts_common_github_forms(self) -> None:
        values = {
            DEVICE.normalize_remote("https://github.com/Example/Repo.git"),
            DEVICE.normalize_remote("git@github.com:example/repo.git"),
            DEVICE.normalize_remote("ssh://git@github.com/example/repo"),
        }
        self.assertEqual(values, {"github:example/repo"})

    def test_remote_validator_rejects_credentials_helpers_and_control_characters_without_echoing(self) -> None:
        unsafe = (
            "https://user:super-secret@example.test/repo.git",
            "https://user@example.test/repo.git",
            "https://example.test/repo.git?token=super-secret",
            "https://example.test/repo.git#super-secret",
            "ext::sh -c whoami",
            "file:///tmp/repo",
            "git@github.com:owner/repo\n--upload-pack=bad",
        )
        for remote in unsafe:
            with self.subTest(remote=remote):
                with self.assertRaises(DEVICE.FleetError) as caught:
                    DEVICE.validate_remote(remote, field="--memory-remote")
                self.assertNotIn("super-secret", str(caught.exception))
                self.assertIn("<redacted remote>", str(caught.exception))

    def test_remote_validator_accepts_noninteractive_https_and_git_ssh(self) -> None:
        for remote in (
            "https://github.com/Example/Repo.git",
            "git@github.com:example/repo.git",
            "ssh://git@github.com/example/repo.git",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(DEVICE.validate_remote(remote, field="remote"), remote)

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

    def bootstrap_args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "source_dir": str(self.base / "source"),
            "memory_dir": str(self.base / "memory"),
            "source_remote": "https://github.com/tamas-hub/codex-routecraft.git",
            "memory_remote": "https://github.com/example/private-memory.git",
            "source_branch": "main",
            "memory_branch": "main",
            "allow_first_device": False,
            "enable_project_source_guard": False,
            "github_owner": None,
            "confirm": None,
            "json": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_bootstrap_plan_is_non_mutating_and_does_not_need_install_confirmation(self) -> None:
        source = self.base / "source"
        source.mkdir()
        args = self.bootstrap_args(source_dir=str(source))
        before = sorted(item.relative_to(self.base).as_posix() for item in self.base.rglob("*"))
        with mock.patch.object(DEVICE, "REPO_ROOT", source):
            plan = DEVICE.bootstrap_plan(args)
        after = sorted(item.relative_to(self.base).as_posix() for item in self.base.rglob("*"))
        self.assertEqual(before, after)
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["confirmation_required"], "INSTALL")

    def test_bootstrap_requires_confirmation_before_any_mutation_or_command(self) -> None:
        args = self.bootstrap_args()
        with mock.patch.object(DEVICE, "require") as required:
            with self.assertRaises(DEVICE.FleetError) as caught:
                DEVICE.bootstrap(args)
        required.assert_not_called()
        self.assertIn("--confirm INSTALL", str(caught.exception))

    def test_install_plan_is_non_mutating(self) -> None:
        args = argparse.Namespace(source_dir=str(ROOT), expected_commit="a" * 40, json=True)
        before = sorted(item.relative_to(self.base).as_posix() for item in self.base.rglob("*"))
        with mock.patch.object(DEVICE, "validate_release_checkout"):
            result = DEVICE.install_plan(args)
        after = sorted(item.relative_to(self.base).as_posix() for item in self.base.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(result["mode"], "install-plan")
        self.assertEqual(result["confirmation_required"], "INSTALL")

    def test_install_apply_requires_confirmation_before_hook_or_checkout_work(self) -> None:
        args = argparse.Namespace(source_dir=str(ROOT), expected_commit="a" * 40, confirm=None, json=True)
        with mock.patch.object(DEVICE, "require_hook_python") as preflight, mock.patch.object(DEVICE, "validate_release_checkout") as checkout:
            with self.assertRaises(DEVICE.FleetError):
                DEVICE.install_apply(args)
        preflight.assert_not_called()
        checkout.assert_not_called()

    def test_hook_python_preflight_matches_platform_hook_command(self) -> None:
        for system, expected_command in (("Windows", "python"), ("Darwin", "python3"), ("Linux", "python3")):
            executable = str((self.base / expected_command).resolve())
            completed = subprocess.CompletedProcess([executable], 0, stdout="3.11\n", stderr="")
            with (
                mock.patch.object(DEVICE.platform, "system", return_value=system),
                mock.patch.object(DEVICE.shutil, "which", side_effect=lambda name, expected=expected_command, value=executable: value if name == expected else None) as which,
                mock.patch.object(DEVICE, "run", return_value=completed) as run,
            ):
                self.assertEqual(executable, DEVICE.require_hook_python())
            which.assert_called_once_with(expected_command)
            run.assert_called_once_with((executable, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"), check=False)

    def test_release_install_requires_exact_official_clean_verified_commit(self) -> None:
        expected = "a" * 40
        responses = iter((DEVICE.SOURCE_REMOTE, "", expected))
        with mock.patch.object(DEVICE, "validate_fixed_checkout"), mock.patch.object(DEVICE, "git_text", side_effect=lambda *_args: next(responses)), mock.patch.object(DEVICE, "run") as verify:
            DEVICE.validate_release_checkout(ROOT, expected)
        verify.assert_called_once_with((sys.executable, str(DEVICE.VERIFY)), cwd=ROOT)

        with self.assertRaises(DEVICE.FleetError):
            DEVICE.validate_release_checkout(ROOT, "short")

    def test_json_error_is_one_safe_object_without_private_remote(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(DEVICE_SCRIPT),
                "plan",
                "--memory-remote",
                "https://user:private-password@example.test/memory.git",
                "--json",
            ],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "ROUTECRAFT_DEVICE_ERROR")
        self.assertNotIn("private-password", result.stdout)
        self.assertEqual(result.stderr, "")

    def _state(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "codex": "fake-codex",
            "plugin_present": False,
            "marketplace_present": False,
            "plugin_version_match": False,
            "plugin_source_match": False,
            "marketplace_source_match": False,
            "cache_match": False,
            "agents_match": False,
            "local_config_match": False,
            "source_control_match": True,
        }
        value.update(overrides)
        return value

    def test_same_version_install_is_noop_without_cache_backup_growth(self) -> None:
        home = self.base / "codex-home"
        source = ROOT
        version = "0.7.3"
        expected = {"schema_version": 1, "plugin_version": version, "last_bootstrap_at": "ignored"}
        cache = home / "plugins" / "cache" / DEVICE.MARKETPLACE / "codex-routecraft" / version
        shutil.copytree(DEVICE.PLUGIN_ROOT, cache)
        for name in DEVICE.AGENTS:
            target = home / "agents" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((DEVICE.AGENT_SOURCE / name).read_bytes())
        DEVICE.write_json(home / "routecraft" / "device.json", expected)

        def fake_json(_codex: str, *args: str) -> dict:
            if args == ("plugin", "list"):
                return {"installed": [{"pluginId": DEVICE.PLUGIN, "version": version, "source": {"path": str(source / "plugins" / "codex-routecraft")}}]}
            return {"marketplaces": [{"name": DEVICE.MARKETPLACE, "root": str(source)}]}

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(DEVICE, "resolve_codex_executable", return_value="fake-codex"), mock.patch.object(DEVICE, "json_command", side_effect=fake_json):
            result = DEVICE.apply_plugin_transaction(source, version, expected, None)
            transaction_root = DEVICE.installation_root()
        self.assertEqual(result["action"], "no-op")
        self.assertFalse(transaction_root.exists())

    def test_same_version_cache_directory_is_not_enough_for_noop(self) -> None:
        home = self.base / "codex-home"
        source = ROOT
        version = "0.7.3"
        expected = {"schema_version": 1, "plugin_version": version}
        cache = home / "plugins" / "cache" / DEVICE.MARKETPLACE / "codex-routecraft" / version
        shutil.copytree(DEVICE.PLUGIN_ROOT, cache)
        (cache / "README.md").write_text("tampered-cache", encoding="utf-8")
        for name in DEVICE.AGENTS:
            target = home / "agents" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((DEVICE.AGENT_SOURCE / name).read_bytes())
        DEVICE.write_json(home / "routecraft" / "device.json", expected)

        def fake_json(_codex: str, *args: str) -> dict:
            if args == ("plugin", "list"):
                return {"installed": [{"pluginId": DEVICE.PLUGIN, "version": version, "source": {"path": str(source / "plugins" / "codex-routecraft")}}]}
            return {"marketplaces": [{"name": DEVICE.MARKETPLACE, "root": str(source)}]}

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(DEVICE, "resolve_codex_executable", return_value="fake-codex"), mock.patch.object(DEVICE, "json_command", side_effect=fake_json):
            state = DEVICE.inspect_install_state(source, version, expected, None)
        self.assertFalse(state["cache_match"])

    def test_plugin_transaction_rolls_back_agent_config_and_cache_after_staged_failure(self) -> None:
        home = self.base / "codex-home"
        source = ROOT
        version = "0.7.3"
        old_agent = home / "agents" / DEVICE.AGENTS[0]
        old_agent.parent.mkdir(parents=True)
        old_agent.write_text("old-agent", encoding="utf-8")
        DEVICE.write_json(home / "routecraft" / "device.json", {"old": True})
        DEVICE.write_json(home / "routecraft" / "source-control.json", {"old": True})
        old_cache = home / "plugins" / "cache" / DEVICE.MARKETPLACE / "codex-routecraft" / "old"
        old_cache.mkdir(parents=True)
        (old_cache / "marker").write_text("old-cache", encoding="utf-8")
        expected = {"schema_version": 1, "plugin_version": version, "last_bootstrap_at": "new"}

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(DEVICE, "inspect_install_state", return_value=self._state()), mock.patch.object(DEVICE, "install_plugin", side_effect=DEVICE.FleetError("injected install failure")), mock.patch.object(DEVICE, "resolve_codex_executable", return_value="fake-codex"), mock.patch.object(DEVICE, "_restore_registry"):
            with self.assertRaises(DEVICE.FleetError):
                DEVICE.apply_plugin_transaction(source, version, expected, {"enabled": True})
            manifests = list(DEVICE.installation_root().glob("install-*/manifest.json"))

        self.assertEqual(old_agent.read_text(encoding="utf-8"), "old-agent")
        self.assertEqual(DEVICE.load_json(home / "routecraft" / "device.json"), {"old": True})
        self.assertEqual(DEVICE.load_json(home / "routecraft" / "source-control.json"), {"old": True})
        self.assertEqual((old_cache / "marker").read_text(encoding="utf-8"), "old-cache")
        self.assertEqual(len(manifests), 1)
        manifest_text = manifests[0].read_text(encoding="utf-8")
        self.assertIn('"state": "AUTO_ROLLED_BACK"', manifest_text)
        self.assertNotIn("private-memory", manifest_text)
        self.assertNotIn("https://", manifest_text)

    def test_explicit_rollback_requires_token_and_restores_owned_agent(self) -> None:
        home = self.base / "codex-home"
        source = ROOT
        agent = home / "agents" / DEVICE.AGENTS[0]
        agent.parent.mkdir(parents=True)
        agent.write_text("before", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(DEVICE, "resolve_codex_executable", return_value="fake-codex"):
            identifier, root, manifest = DEVICE.create_install_transaction(source, self._state())
            DEVICE.update_transaction(root, manifest, "COMMITTED", post_state={})
            agent.write_text("after", encoding="utf-8")
            denied = argparse.Namespace(source_dir=str(source), transaction_id=identifier, confirm=None)
            with self.assertRaises(DEVICE.FleetError):
                DEVICE.rollback(denied)
            with mock.patch.object(DEVICE, "_restore_registry"), mock.patch.object(DEVICE, "_assert_post_state_unchanged"):
                accepted = argparse.Namespace(source_dir=str(source), transaction_id=identifier, confirm="ROLLBACK")
                result = DEVICE.rollback(accepted)
        self.assertEqual(result["state"], "ROLLED_BACK")
        self.assertEqual(agent.read_text(encoding="utf-8"), "before")

    def test_rollback_rejects_tampered_file_backup_before_deleting_current_file(self) -> None:
        home = self.base / "codex-home"
        source = ROOT
        agent = home / "agents" / DEVICE.AGENTS[0]
        agent.parent.mkdir(parents=True)
        agent.write_text("before", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(DEVICE, "resolve_codex_executable", return_value="fake-codex"):
            identifier, root, _manifest = DEVICE.create_install_transaction(source, self._state())
            agent.write_text("current", encoding="utf-8")
            backup = root / "backups" / "files" / hashlib.sha256(f"agent:{DEVICE.AGENTS[0]}".encode()).hexdigest()
            backup.write_text("tampered", encoding="utf-8")
            with mock.patch.object(DEVICE, "_restore_registry"), self.assertRaises(DEVICE.FleetError):
                DEVICE.rollback_installation(identifier, source, auto=True)
        self.assertEqual("current", agent.read_text(encoding="utf-8"))
        self.assertEqual("ROLLBACK_FAILED", DEVICE.load_json(root / "manifest.json")["state"])

    def test_rollback_prevalidates_all_file_backups_before_any_restore(self) -> None:
        home = self.base / "codex-home"
        first = home / "agents" / DEVICE.AGENTS[0]
        second = home / "agents" / DEVICE.AGENTS[1]
        first.parent.mkdir(parents=True)
        first.write_text("first-before", encoding="utf-8")
        second.write_text("second-before", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            identifier, root, _manifest = DEVICE.create_install_transaction(ROOT, self._state())
            first.write_text("first-current", encoding="utf-8")
            second.write_text("second-current", encoding="utf-8")
            second_backup = root / "backups" / "files" / hashlib.sha256(f"agent:{DEVICE.AGENTS[1]}".encode()).hexdigest()
            second_backup.write_text("tampered", encoding="utf-8")
            with mock.patch.object(DEVICE, "_restore_registry"), self.assertRaises(DEVICE.FleetError):
                DEVICE.rollback_installation(identifier, ROOT, auto=True)
        self.assertEqual(first.read_text(encoding="utf-8"), "first-current")
        self.assertEqual(second.read_text(encoding="utf-8"), "second-current")

    def test_stale_committed_rollback_refuses_before_mutation(self) -> None:
        home = self.base / "codex-home"
        agent = home / "agents" / DEVICE.AGENTS[0]
        agent.parent.mkdir(parents=True)
        agent.write_text("before", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            identifier, root, manifest = DEVICE.create_install_transaction(ROOT, self._state())
            DEVICE.update_transaction(root, manifest, "COMMITTED", post_state={"marker": "committed"})
            agent.write_text("user-change", encoding="utf-8")
            with mock.patch.object(DEVICE, "_capture_post_state", return_value={"marker": "changed"}), mock.patch.object(DEVICE, "_restore_file") as restore_file, mock.patch.object(DEVICE, "_restore_registry") as restore_registry:
                with self.assertRaises(DEVICE.FleetError) as caught:
                    DEVICE.rollback_installation(identifier, ROOT)
        self.assertIn("stale rollback refused", str(caught.exception))
        restore_file.assert_not_called()
        restore_registry.assert_not_called()
        self.assertEqual(agent.read_text(encoding="utf-8"), "user-change")

    def test_completed_transaction_cannot_be_rolled_back_twice(self) -> None:
        home = self.base / "codex-home"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(DEVICE, "resolve_codex_executable", return_value="fake-codex"):
            identifier, _root, _manifest = DEVICE.create_install_transaction(ROOT, self._state())
            with mock.patch.object(DEVICE, "_restore_registry"), mock.patch.object(DEVICE, "_restore_cache"):
                DEVICE.rollback_installation(identifier, ROOT, auto=True)
                with self.assertRaises(DEVICE.FleetError):
                    DEVICE.rollback_installation(identifier, ROOT, auto=True)

    def test_rollback_restores_prior_marketplace_source_from_private_restore_record(self) -> None:
        home = self.base / "codex-home"
        source = ROOT
        old_root = self.base / "previous-marketplace"
        old_root.mkdir()
        old_cache = home / "plugins" / "cache" / DEVICE.MARKETPLACE / "codex-routecraft" / "0.6.9"
        shutil.copytree(DEVICE.PLUGIN_ROOT, old_cache)
        old_manifest = DEVICE.load_json(old_cache / ".codex-plugin" / "plugin.json")
        old_manifest["version"] = "0.6.9"
        DEVICE.write_json(old_cache / ".codex-plugin" / "plugin.json", old_manifest)
        state = self._state(
            _restore_registry={
                "marketplace": {
                    "present": True,
                    "root": str(old_root),
                    "marketplaceSource": {"sourceType": "local", "source": str(old_root)},
                },
                "plugin": {"present": True, "version": "0.6.9", "source": {"path": str(old_root / "plugins" / "codex-routecraft")}},
            }
        )
        calls: list[tuple[str, ...]] = []
        def restored_json(_codex: str, *args: str) -> dict:
            if args == ("plugin", "list"):
                return {"installed": [{"pluginId": DEVICE.PLUGIN, "version": "0.6.9"}]}
            return {"marketplaces": [{"name": DEVICE.MARKETPLACE}]}

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(DEVICE, "resolve_codex_executable", return_value="fake-codex"), mock.patch.object(DEVICE, "run", side_effect=lambda args, **_kwargs: (calls.append(tuple(args)) or subprocess.CompletedProcess(args, 0, "", ""))), mock.patch.object(DEVICE, "json_command", side_effect=restored_json):
            identifier, root, manifest = DEVICE.create_install_transaction(source, state)
            DEVICE.update_transaction(root, manifest, "COMMITTED", post_state={})
            with mock.patch.object(DEVICE, "_assert_post_state_unchanged"):
                result = DEVICE.rollback_installation(identifier, source)
        self.assertEqual(result["state"], "ROLLED_BACK")
        snapshot_root = root / "registry-snapshot" / "marketplace"
        self.assertIn(("fake-codex", "plugin", "marketplace", "add", str(snapshot_root)), calls)
        self.assertIn(("fake-codex", "plugin", "add", DEVICE.PLUGIN), calls)
        self.assertNotIn(str(old_root), (root / "manifest.json").read_text(encoding="utf-8"))
        private_restore = json.loads((root / "registry-restore.private.json").read_text(encoding="utf-8"))
        self.assertEqual(private_restore["marketplace"]["root"], str(old_root))

    def test_registry_rollback_rejects_wrong_restored_plugin_version(self) -> None:
        snapshot = self.base / "snapshot-marketplace"
        DEVICE.write_json(snapshot / ".agents" / "plugins" / "marketplace.json", {"name": DEVICE.MARKETPLACE})
        digest = DEVICE._safe_tree_digest(snapshot, "test registry snapshot")
        restore = {
            "marketplace": {"present": True, "root": str(self.base)},
            "plugin": {"present": True, "version": "0.6.9"},
            "snapshot": {
                "present": True,
                "marketplace_root": str(snapshot),
                "version": "0.6.9",
                "sha256": digest,
            },
        }

        def wrong_version(_codex: str, *args: str) -> dict:
            if args == ("plugin", "list"):
                return {"installed": [{"pluginId": DEVICE.PLUGIN, "version": "0.7.3"}]}
            return {"marketplaces": [{"name": DEVICE.MARKETPLACE}]}

        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(DEVICE, "run", return_value=completed), mock.patch.object(DEVICE, "json_command", side_effect=wrong_version), self.assertRaises(DEVICE.FleetError):
            DEVICE._restore_registry("fake-codex", restore)

    def test_rollback_refuses_symlink_or_junction_cache_target(self) -> None:
        home = self.base / "codex-home"
        cache = home / "plugins" / "cache" / DEVICE.MARKETPLACE / "codex-routecraft"
        cache.mkdir(parents=True)
        marker = cache / "marker"
        marker.write_text("preserve", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(DEVICE, "is_reparse_or_symlink", return_value=True):
            with self.assertRaises(DEVICE.FleetError):
                DEVICE._restore_cache(cache, self.base / "backup", False)
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_rollback_rejects_tampered_cache_backup_before_deleting_current_cache(self) -> None:
        home = self.base / "codex-home"
        cache = home / "plugins" / "cache" / DEVICE.MARKETPLACE / "codex-routecraft"
        backup = self.base / "cache-backup"
        cache.mkdir(parents=True)
        backup.mkdir()
        (cache / "marker").write_text("current", encoding="utf-8")
        (backup / "marker").write_text("before", encoding="utf-8")
        expected = DEVICE._safe_tree_digest(backup, "test cache backup")
        (backup / "marker").write_text("tampered", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), self.assertRaises(DEVICE.FleetError):
            DEVICE._restore_cache(cache, backup, True, expected)
        self.assertEqual("current", (cache / "marker").read_text(encoding="utf-8"))

    def test_rollback_restores_cache_after_registry_reregistration(self) -> None:
        home = self.base / "codex-home"
        order: list[str] = []
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(DEVICE, "resolve_codex_executable", return_value="fake-codex"):
            identifier, _root, _manifest = DEVICE.create_install_transaction(ROOT, self._state())
            with mock.patch.object(DEVICE, "_restore_registry", side_effect=lambda *_args: order.append("registry")), mock.patch.object(DEVICE, "_restore_cache", side_effect=lambda *_args: order.append("cache")):
                DEVICE.rollback_installation(identifier, ROOT, auto=True)
        self.assertEqual(order, ["registry", "cache"])

    def test_rollback_marks_manifest_failed_and_wraps_os_error(self) -> None:
        home = self.base / "codex-home"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            identifier, root, _manifest = DEVICE.create_install_transaction(ROOT, self._state())
            with mock.patch.object(DEVICE, "_restore_registry", side_effect=OSError("injected")):
                with self.assertRaises(DEVICE.FleetError) as caught:
                    DEVICE.rollback_installation(identifier, ROOT, auto=True)
        self.assertNotIn("injected", str(caught.exception))
        self.assertEqual(DEVICE.load_json(root / "manifest.json")["state"], "ROLLBACK_FAILED")

    def test_apply_wraps_non_fleet_automatic_rollback_failure(self) -> None:
        home = self.base / "codex-home"
        expected = {"schema_version": 1, "plugin_version": "0.7.3", "last_bootstrap_at": "new"}
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(DEVICE, "inspect_install_state", return_value=self._state()), mock.patch.object(DEVICE, "install_plugin", side_effect=DEVICE.FleetError("injected install failure")), mock.patch.object(DEVICE, "rollback_installation", side_effect=OSError("injected rollback failure")):
            with self.assertRaises(DEVICE.FleetError) as caught:
                DEVICE.apply_plugin_transaction(ROOT, "0.7.3", expected, None)
        self.assertIn("automatic rollback failed", str(caught.exception))
        self.assertNotIn("injected rollback failure", str(caught.exception))

    def test_install_apply_writes_minimal_local_config_transactionally(self) -> None:
        home = self.base / "codex-home"
        source = ROOT
        version = "0.7.3"
        initial = self._state()
        verified = self._state(**{name: True for name in (
            "plugin_present", "marketplace_present", "plugin_version_match", "plugin_source_match",
            "marketplace_source_match", "cache_match", "agents_match", "local_config_match", "source_control_match",
        )})
        args = argparse.Namespace(source_dir=str(source), expected_commit="a" * 40, confirm="INSTALL", json=True)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(DEVICE, "require_hook_python"), mock.patch.object(DEVICE, "validate_release_checkout"), mock.patch.object(DEVICE, "plugin_version", return_value=version), mock.patch.object(DEVICE, "inspect_install_state", side_effect=[initial, verified, verified]), mock.patch.object(DEVICE, "install_plugin", return_value={"cache": "fake", "agents_changed": []}), mock.patch.object(DEVICE, "_capture_post_state", return_value={"verified": True}):
            result = DEVICE.install_apply(args)
            config = DEVICE.load_json(home / "routecraft" / "device.json")
        self.assertTrue(result["ok"])
        self.assertEqual(result["plugin"]["action"], "applied")
        self.assertEqual(config["plugin_version"], version)
        self.assertNotIn("memory_remote", config)

    def test_minimal_install_preserves_existing_decision_store_configuration(self) -> None:
        home = self.base / "codex-home"
        existing = {
            "schema_version": 1,
            "device_id": "safe-device",
            "memory_dir": "C:/private-memory",
            "memory_remote": "https://github.com/example/private-memory.git",
            "memory_branch": "main",
            "auto_sync": "both",
        }
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            DEVICE.write_json(home / "routecraft" / "device.json", existing)
            config = DEVICE.minimal_install_config(ROOT, "0.7.3")
        self.assertEqual(config["memory_remote"], existing["memory_remote"])
        self.assertEqual(config["device_id"], "safe-device")


if __name__ == "__main__":
    unittest.main()
