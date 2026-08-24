from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-routecraft" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import routecraft_agents_optimizer as AGENTS
import routecraft_benchmark_lab as LAB
import routecraft_collector as COLLECTOR
import routecraft_control_center as CONTROL
import routecraft_endpoint_migration as ENDPOINT


class ControlCenterRuntimeTests(unittest.TestCase):
    @staticmethod
    def valid_run(index: int = 1) -> dict[str, object]:
        identifier = f"{index:032x}"
        return {
            "run_id": identifier,
            "parent_run_id": f"{index + 1000:032x}",
            "device_id": "a" * 32,
            "route_family": "routecraft",
            "role": "routecraft_terra_medium",
            "human_model": "gpt-5.6-sol",
            "human_effort": "high",
            "actual_model": "gpt-5.6-terra",
            "actual_effort": "medium",
            "started_at": "2026-08-24T00:00:00Z",
            "ended_at": "2026-08-24T00:00:01Z",
            "duration_ms": 1000,
            "input_tokens": 1,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_output_tokens": 0,
            "total_tokens": 2,
            "observed_at": "2026-08-24T00:00:01Z",
            "task_class": None,
            "task_summary": None,
            "memory_mode": None,
            "memory_recall_count": None,
            "memory_useful_count": None,
            "memory_learn_status": None,
            "memory_skip_reason": None,
        }

    @staticmethod
    def valid_memory_task(index: int = 1) -> dict[str, object]:
        return {
            "task_run_id": f"{index + 2000:032x}",
            "parent_run_id": f"{index + 1000:032x}",
            "device_id": "a" * 32,
            "human_model": "gpt-5.6-sol",
            "human_effort": "high",
            "task_class": "implementation",
            "task_summary": "Collector check",
            "memory_mode": "off",
            "memory_recall_count": 0,
            "memory_useful_count": 0,
            "memory_learn_status": "skipped",
            "memory_skip_reason": "mode_off",
            "completed_at": "2026-08-24T00:00:01Z",
            "observed_at": "2026-08-24T00:00:01Z",
        }

    def test_core_collector_has_no_control_center_import_and_disabled_is_supported(self) -> None:
        text = (SCRIPTS / "routecraft_collector.py").read_text(encoding="utf-8")
        self.assertNotIn("import routecraft_control_center", text)
        self.assertNotIn("from routecraft_control_center", text)
        with mock.patch.dict("os.environ", {"CONTROL_CENTER_ENABLED": "false"}, clear=False):
            self.assertFalse(COLLECTOR.enabled())

    def test_v3_collector_keeps_families_and_partial_failure_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(COLLECTOR.routecraft_telemetry, "collect_runs", return_value=[]), mock.patch.object(COLLECTOR.routecraft_telemetry, "collect_memory_tasks", return_value=[]), mock.patch.object(COLLECTOR, "_run_app_server", side_effect=RuntimeError("local adapter unavailable")):
                payload = COLLECTOR.collect_v3(source_root=root, data_dir=None, sessions_dir=root / "sessions", codex_home=root / "no-config")
            for name, expected_keys in COLLECTOR.FAMILY_KEYS.items():
                self.assertIsInstance(payload[name], list)
                if payload[name]:
                    self.assertEqual(set(payload[name][0]), expected_keys)
            self.assertEqual("unavailable", payload["device_health"][0]["plugin_health"])
            self.assertEqual([], payload["usage_snapshots"])
            self.assertEqual([], payload["memory_metrics"])
            self.assertEqual("degraded", payload["system_status"][0]["collector_health"])
            self.assertTrue(COLLECTOR.validate_v3(payload))
            rendered = json.dumps(payload)
            for forbidden in ("path", "session_id", "prompt", "content", "source", "authorization"):
                self.assertNotIn(forbidden, rendered)

    def test_v3_ids_are_opaque_retry_idempotent_and_history_preserving(self) -> None:
        fixed = "2026-08-24T00:00:00Z"
        device = COLLECTOR.opaque_id("device", "private-machine-name")
        first = COLLECTOR._family_id("usage", device, fixed, "weekly")
        self.assertEqual(first, COLLECTOR._family_id("usage", device, fixed, "weekly"))
        self.assertNotEqual(first, COLLECTOR._family_id("usage", device, "2026-08-24T00:00:01Z", "weekly"))
        self.assertRegex(device, r"^[a-f0-9]{32}$")
        self.assertNotIn("private-machine-name", device)

    def test_safe_labels_keep_semver_build_metadata_and_git_worktree_file_is_checked(self) -> None:
        self.assertEqual("0.6.0+codex.20260824", COLLECTOR._label("0.6.0+codex.20260824"))
        self.assertEqual("RouteCraft + Memory", COLLECTOR._label("RouteCraft + Memory"))
        self.assertEqual("unknown", COLLECTOR._label("C:/private/path"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text("gitdir: /not-exported", encoding="utf-8")
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(COLLECTOR.subprocess, "run", return_value=completed) as run:
                self.assertEqual((True, 0, 0, 0), COLLECTOR._git_state(root))
            self.assertTrue(run.called)

    def test_installed_health_resolves_registered_source_and_verifies_hooks_and_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            home = Path(tmp) / "codex"
            plugin = root / "plugins" / "codex-routecraft"
            cache = home / "plugins" / "cache" / "routecraft" / "codex-routecraft" / "0.6.0"
            (home / "routecraft").mkdir(parents=True)
            (home / "agents").mkdir(parents=True)
            (plugin / ".codex-plugin").mkdir(parents=True)
            (plugin / "hooks").mkdir()
            (plugin / "agents").mkdir()
            (cache / ".codex-plugin").mkdir(parents=True)
            (cache / "hooks").mkdir()
            manifest = json.dumps({"version": "0.6.0"})
            (plugin / ".codex-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
            (cache / ".codex-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
            (plugin / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
            (cache / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
            names = (
                "routecraft_luna_low.toml", "routecraft_luna_medium.toml", "routecraft_luna_max.toml",
                "routecraft_terra_medium.toml", "routecraft_terra_high.toml", "routecraft_sol_reviewer.toml",
            )
            for name in names:
                (plugin / "agents" / name).write_text(name, encoding="utf-8")
                (home / "agents" / name).write_text(name, encoding="utf-8")
            (home / "routecraft" / "device.json").write_text(json.dumps({"source_dir": str(root), "plugin_version": "0.6.0"}), encoding="utf-8")
            self.assertEqual(root, COLLECTOR.configured_source_root(home))
            row = COLLECTOR.device_health(root, "a" * 32, "2026-08-24T00:00:00Z", home)
            self.assertEqual("healthy", row["plugin_health"])
            self.assertEqual("healthy", row["hook_health"])
            self.assertEqual(6, row["agents_healthy"])

    def test_usage_adapter_uses_initialize_single_flight_exact_enums_and_percent_relation(self) -> None:
        completed = mock.Mock(returncode=0, stdout='{"jsonrpc":"2.0","id":1,"result":{}}\n{"jsonrpc":"2.0","id":2,"result":{"rateLimits":{"windows":[{"kind":"fiveHour","remainingPercent":80,"resetSeconds":60},{"kind":"weekly","usedPercent":25,"resetSeconds":120}]}}}\n', stderr="")
        runs = [{"input_tokens": 2, "cached_input_tokens": 3, "output_tokens": 5, "reasoning_output_tokens": 7, "actual_model": "gpt-5.6-sol"}]
        with mock.patch.object(COLLECTOR.subprocess, "run", return_value=completed) as run:
            rows = COLLECTOR.usage_snapshots("a" * 32, "2026-08-24T00:00:00Z", runs, ["codex", "app-server"])
        sent = run.call_args.kwargs["input"]
        self.assertLess(sent.index('"method":"initialize"'), sent.index('"method":"account/rateLimits/read"'))
        self.assertEqual({"five_hour", "weekly"}, {row["window_kind"] for row in rows})
        self.assertTrue(all(row["used_percent"] + row["remaining_percent"] == 100 for row in rows))
        self.assertEqual(2, rows[0]["input_tokens"])
        self.assertEqual(1, rows[0]["sol_runs"])

    def test_validation_rejects_non_boolean_benchmark_measurement_and_unknown_keys(self) -> None:
        payload = COLLECTOR.fixture_payload()
        self.assertTrue(COLLECTOR.validate_v3(payload))
        payload["benchmark_runs"][0]["measured"] = 1
        self.assertFalse(COLLECTOR.validate_v3(payload))
        payload = COLLECTOR.fixture_payload()
        payload["benchmark_runs"][0]["extra"] = "no"
        self.assertFalse(COLLECTOR.validate_v3(payload))

    def test_v3_validation_rejects_raw_run_and_memory_task_fields_before_delivery(self) -> None:
        for extra in ("prompt", "path", "authorization"):
            payload = COLLECTOR.fixture_payload()
            payload["runs"] = [self.valid_run()]
            self.assertTrue(COLLECTOR.validate_v3(payload))
            payload["runs"][0][extra] = "private"
            self.assertFalse(COLLECTOR.validate_v3(payload))

            payload = COLLECTOR.fixture_payload()
            payload["memory_tasks"] = [self.valid_memory_task()]
            self.assertTrue(COLLECTOR.validate_v3(payload))
            payload["memory_tasks"][0][extra] = "private"
            self.assertFalse(COLLECTOR.validate_v3(payload))

    def test_v3_run_token_fields_reject_paths_and_urls_before_delivery(self) -> None:
        for unsafe in (
            "C:/private/path", "/Users/private", "https://example.invalid/model", "../secret",
            "file:/Users/private/model", "C:private/model", "private/project/model",
        ):
            payload = COLLECTOR.fixture_payload()
            payload["runs"] = [self.valid_run()]
            payload["runs"][0]["actual_model"] = unsafe
            self.assertFalse(COLLECTOR.validate_v3(payload), unsafe)

    def test_memory_metrics_reads_only_configured_aggregate_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "codex"
            store = root / "decision-store"
            local = root / "local"
            (home / "routecraft").mkdir(parents=True)
            (store / ".routecraft-store.json").parent.mkdir(parents=True)
            (store / ".routecraft-store.json").write_text('{"schema_version":1}', encoding="utf-8")
            for directory, count in (("cases", 2), ("candidates", 1), ("rules", 3)):
                (store / directory).mkdir()
                for index in range(count):
                    (store / directory / f"{index}.md").write_text("aggregate test", encoding="utf-8")
            (home / "routecraft" / "memory.json").write_text(json.dumps({"store": str(store)}), encoding="utf-8")
            (home / "routecraft" / "local-memory.json").write_text(json.dumps({"data_dir": str(local)}), encoding="utf-8")
            local.mkdir()
            connection = sqlite3.connect(local / "routecraft-local.sqlite3")
            try:
                for table, count in (("projects", 1), ("memories", 4), ("context_injections", 2), ("handoffs", 3)):
                    connection.execute(f"CREATE TABLE {table} (id INTEGER)")
                    connection.executemany(f"INSERT INTO {table} VALUES (?)", [(index,) for index in range(count)])  # routecraft-security: allowlisted-sql-shape
                connection.commit()
            finally:
                connection.close()
            row = COLLECTOR.memory_metrics(None, "a" * 32, "2026-08-24T00:00:00Z", home)
            self.assertIsNotNone(row)
            self.assertEqual(2, row["decision_cases"])
            self.assertEqual(4, row["local_memories"])
            self.assertNotIn(str(root), json.dumps(row))
            self.assertIsNone(COLLECTOR.memory_metrics(None, "a" * 32, "2026-08-24T00:00:00Z", root / "no-config"))

    def test_engine_summary_files_must_already_match_exact_v3_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "codex"
            benchmark = COLLECTOR.fixture_payload()["benchmark_runs"][0]
            benchmark.update({"measured": True, "status": "passed", "current_label": "Old routing", "candidate_label": "RouteCraft + Memory"})
            security = COLLECTOR.fixture_payload()["security_scans"][0]
            security["status"] = "clean"
            for path, row in ((home / "routecraft" / "benchmark" / "latest-summary.json", benchmark), (home / "routecraft" / "security" / "latest-summary.json", security)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(row), encoding="utf-8")
            with mock.patch.object(COLLECTOR.routecraft_telemetry, "collect_runs", return_value=[]), mock.patch.object(COLLECTOR.routecraft_telemetry, "collect_memory_tasks", return_value=[]), mock.patch.object(COLLECTOR, "_run_app_server", side_effect=RuntimeError):
                payload = COLLECTOR.collect_v3(source_root=root, codex_home=home)
            self.assertTrue(payload["benchmark_runs"][0]["measured"])
            self.assertEqual("RouteCraft + Memory", payload["benchmark_runs"][0]["candidate_label"])
            self.assertEqual("clean", payload["security_scans"][0]["status"])
            self.assertEqual("healthy", payload["system_status"][0]["benchmark_health"])
            self.assertEqual("healthy", payload["system_status"][0]["security_health"])
            self.assertTrue(COLLECTOR.validate_v3(payload))

    def test_system_status_maps_benchmark_and_security_independently(self) -> None:
        payload = COLLECTOR.fixture_payload()
        device = payload["device_health"][0]
        benchmark = payload["benchmark_runs"][0]
        security = payload["security_scans"][0]
        benchmark.update({"status": "partial", "measured": False})
        security["status"] = "findings"
        status = COLLECTOR.system_status(device, [], False, benchmark, security, device["device_id"], device["observed_at"])
        self.assertEqual("degraded", status["benchmark_health"])
        self.assertEqual("degraded", status["security_health"])
        benchmark.update({"status": "cancelled", "measured": False})
        security["status"] = "unavailable"
        status = COLLECTOR.system_status(device, [], False, benchmark, security, device["device_id"], device["observed_at"])
        self.assertEqual("unavailable", status["benchmark_health"])
        self.assertEqual("unavailable", status["security_health"])
        status = COLLECTOR.system_status(
            device,
            [],
            True,
            benchmark,
            security,
            device["device_id"],
            device["observed_at"],
            local_memory_available=True,
            decision_available=False,
        )
        self.assertEqual("healthy", status["memory_local_health"])
        self.assertEqual("unavailable", status["decision_health"])

    def test_control_center_transport_is_opt_in_and_sends_redacted_sites_bypass_header(self) -> None:
        payload = COLLECTOR.fixture_payload()
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            bypass_file = Path(tmp) / "bypass"
            token_file.write_text("a" * 32, encoding="utf-8")
            bypass_file.write_text("b" * 32, encoding="utf-8")
            with mock.patch.dict("os.environ", {"CONTROL_CENTER_ENABLED": "false"}, clear=False), mock.patch.object(CONTROL.urllib.request, "urlopen") as urlopen:
                self.assertEqual("disabled", CONTROL.deliver("https://example.invalid/api", token_file, payload, bypass_file)["state"])
                urlopen.assert_not_called()
            response = mock.MagicMock(status=202)
            response.__enter__.return_value = response
            with mock.patch.dict("os.environ", {"CONTROL_CENTER_ENABLED": "true"}, clear=False), mock.patch.object(CONTROL.urllib.request, "urlopen", return_value=response) as urlopen:
                result = CONTROL.deliver("https://example.invalid/api", token_file, payload, bypass_file)
            request = urlopen.call_args.args[0]
            self.assertIsNotNone(request.get_header("Oai-sites-authorization"))
            self.assertTrue(result["delivered"])
            self.assertNotIn("b" * 32, json.dumps(result))

    def test_control_center_batches_large_v3_history_without_repeating_summaries(self) -> None:
        payload = COLLECTOR.fixture_payload()
        payload["runs"] = [self.valid_run(index) for index in range(1, 802)]
        payload["memory_tasks"] = [self.valid_memory_task(index) for index in range(1, 802)]
        self.assertTrue(COLLECTOR.validate_v3(payload))
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("a" * 32, encoding="utf-8")
            response = mock.MagicMock(status=202)
            response.__enter__.return_value = response
            with mock.patch.dict("os.environ", {"CONTROL_CENTER_ENABLED": "true"}, clear=False), mock.patch.object(
                CONTROL.urllib.request, "urlopen", return_value=response
            ) as urlopen:
                result = CONTROL.deliver("https://example.invalid/api", token_file, payload)
        self.assertTrue(result["delivered"])
        self.assertEqual(3, result["batches"])
        batches = [json.loads(call.args[0].data.decode("utf-8")) for call in urlopen.call_args_list]
        self.assertEqual([400, 400, 1], [len(batch["runs"]) for batch in batches])
        self.assertEqual([400, 400, 1], [len(batch["memory_tasks"]) for batch in batches])
        for name in COLLECTOR.FAMILY_KEYS:
            self.assertEqual(payload[name], batches[0][name])
            self.assertEqual([], batches[1][name])
            self.assertEqual([], batches[2][name])

    def test_installed_collector_needs_only_copied_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "installed"
            installed.mkdir()
            for name in ("routecraft_observatory_tray.ps1", "routecraft_observatory.py", "routecraft_collector.py", "routecraft_control_center.py", "routecraft_telemetry.py"):
                shutil.copy2(SCRIPTS / name, installed / name)
            control = subprocess.run([sys.executable, str(installed / "routecraft_control_center.py"), "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", check=False)
            fixture = subprocess.run([sys.executable, str(installed / "routecraft_collector.py"), "--fixture"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", check=False)
            self.assertEqual(0, control.returncode, control.stderr)
            self.assertEqual(0, fixture.returncode, fixture.stderr)
            self.assertTrue(COLLECTOR.validate_v3(json.loads(fixture.stdout)))
            collector_text = (installed / "routecraft_collector.py").read_text(encoding="utf-8")
            for disallowed in ("routecraft_evaluation", "routecraft_local", "routecraft_memory_lib"):
                self.assertNotIn(f"import {disallowed}", collector_text)
                self.assertNotIn(f"from {disallowed}", collector_text)

    def test_agents_preview_never_writes_and_apply_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "AGENTS.md"
            preview = AGENTS.preview(target)
            self.assertTrue(preview["changed"])
            self.assertFalse(target.exists())
            with self.assertRaises(ValueError):
                AGENTS.apply(target, "no")
            self.assertTrue(AGENTS.apply(target, "APPLY")["changed"])
            self.assertEqual(0, AGENTS.analyze(target).recommendation_count)

    def test_benchmark_counterfactual_is_not_measured(self) -> None:
        fixture = LAB.load_fixture(ROOT / "samples" / "benchmark-lab-fixture.json")
        result = LAB.compare(fixture)
        self.assertFalse(result["measured"])
        self.assertEqual("counterfactual", result["measurement"])

    def test_endpoint_migration_preserves_non_endpoint_config_and_uses_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "observatory-tray.json"
            old = "https://old.example/ingest"
            new = "https://routecraft.example/ingest"
            original = {"endpoint": old, "telemetry_endpoint": old, "token_file": "private-token-path", "interval_seconds": 300, "enabled": False}
            config.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(2, ENDPOINT.preview(config, old, new)["changed_endpoint_count"])
            result = ENDPOINT.apply(config, old, new, "APPLY")
            updated = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(new, updated["endpoint"])
            self.assertEqual(new, updated["telemetry_endpoint"])
            self.assertEqual(300, updated["interval_seconds"])
            self.assertFalse(updated["enabled"])
            self.assertEqual(original["token_file"], updated["token_file"])
            self.assertTrue(Path(str(result["backup_path"])).is_file())
