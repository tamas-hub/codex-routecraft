from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft.py"
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))
from routecraft_local import cli as LOCAL_CLI
import routecraft_execution_graph as EXECUTION_GRAPH


class RouteCraftLocalCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.data = self.base / "日本語 データ"

    def run_cli(
        self,
        *args: str,
        input_bytes: bytes | None = None,
        expected: int = 0,
        legacy_encoding: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        if legacy_encoding:
            env["PYTHONUTF8"] = "0"
            env["PYTHONIOENCODING"] = "cp932"
        else:
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
        env["CODEX_HOME"] = str(self.base / "codex-home")
        process = subprocess.run(
            [sys.executable, str(SCRIPT), "--data-dir", str(self.data), *args],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            expected,
            process.returncode,
            msg=f"stdout={process.stdout.decode('utf-8', 'replace')}\nstderr={process.stderr.decode('utf-8', 'replace')}",
        )
        return process

    @staticmethod
    def payload(process: subprocess.CompletedProcess[bytes]) -> dict:
        return json.loads(process.stdout.decode("utf-8"))

    def add_project(self) -> str:
        result = self.payload(
            self.run_cli(
                "project",
                "add",
                "--name",
                "日本語プロジェクト",
                "--description",
                "引き継ぎの確認",
                "--objective",
                "v1.0を完成する",
                "--json",
            )
        )
        return result["data"]["id"]

    def test_restore_human_output_includes_cleanup_warning_and_retained_path(self) -> None:
        class WarningService:
            def initialize(self): return {"ok":True}
            def restore(self, archive, confirmation):
                return {"restored":archive,"pre_restore_backup":"backup.zip","warnings":["cleanup failed"],"retained_rollback":"rollback.sqlite3"}
        args=__import__('argparse').Namespace(command="restore",input="source.zip",confirm="RESTORE")
        output=io.StringIO()
        with contextlib.redirect_stdout(output): self.assertEqual(0,LOCAL_CLI._handle(args,WarningService(),False))
        rendered=output.getvalue(); self.assertIn("cleanup failed",rendered); self.assertIn("rollback.sqlite3",rendered)

    def test_help_init_and_project_lifecycle(self) -> None:
        version = subprocess.run(
            [sys.executable, str(SCRIPT), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, version.returncode)
        self.assertEqual("routecraft 0.7.4 (memory-local 1.0.0)", version.stdout.decode("utf-8").strip())

        for args in (
            ("--help",),
            ("project", "--help"),
            ("memory", "--help"),
            ("context", "build", "--help"),
            ("handoff", "build", "--help"),
            ("loop", "configure", "--help"),
            ("ui", "--help"),
        ):
            self.run_cli(*args)

        initialized = self.payload(self.run_cli("init", "--json"))
        self.assertEqual(1, initialized["data"]["schema_version"])
        project_id = self.add_project()
        shown = self.payload(self.run_cli("project", "show", "--project", project_id, "--json"))
        self.assertEqual("日本語プロジェクト", shown["data"]["name"])
        self.run_cli("project", "rename", "--project", project_id, "--name", "改名後", "--json")
        self.run_cli("project", "archive", "--project", project_id, "--json")
        projects = self.payload(self.run_cli("project", "list", "--include-archived", "--json"))
        self.assertEqual(1, len(projects["data"]))
        failed = self.payload(
            self.run_cli(
                "project", "delete", "--project", project_id, "--confirm", "wrong", "--json", expected=4
            )
        )
        self.assertFalse(failed["ok"])
        self.assertEqual("ConfirmationRequiredError", failed["error"]["code"])

        enabled = self.payload(
            self.run_cli("loop", "configure", "--enable", "--context-profile", "compact", "--json")
        )["data"]
        self.assertTrue(enabled["enabled"]); self.assertEqual(str(self.data.resolve()), enabled["data_dir"])
        status = self.payload(self.run_cli("loop", "status", "--json"))["data"]
        self.assertTrue(status["configured"]); self.assertTrue(status["enabled"])
        disabled = self.payload(self.run_cli("loop", "configure", "--disable", "--json"))["data"]
        self.assertFalse(disabled["enabled"]); self.assertTrue(Path(disabled["backup"]).is_file())

    def test_utf8_bom_stdin_search_context_handoff_and_backup(self) -> None:
        self.run_cli("init", "--json")
        project_id = self.add_project()
        secret = "sk-" + "abcdefghijklmnopqrstuvwxyz012345"
        body = f"日本語stdin本文\r\n秘密 {secret}"
        added = self.payload(
            self.run_cli(
                "memory",
                "add",
                "--project",
                project_id,
                "--type",
                "decision",
                "--title",
                "UTF-8 BOMの判断",
                "--body",
                "-",
                "--importance",
                "high",
                "--verified",
                "--json",
                input_bytes=b"\xef\xbb\xbf" + body.encode("utf-8"),
                legacy_encoding=True,
            )
        )
        memory = added["data"]
        self.assertIn("[REDACTED:openai_key]", memory["body"])
        self.assertNotIn(secret, added["data"]["body"])

        searched = self.payload(
            self.run_cli("memory", "search", "--project", project_id, "日本語 stdin", "--json")
        )
        self.assertEqual(memory["id"], searched["data"][0]["id"])

        context_path = self.base / "context.md"
        context = self.payload(
            self.run_cli(
                "context",
                "build",
                "--project",
                project_id,
                "--profile",
                "compact",
                "--output",
                str(context_path),
                "--json",
            )
        )
        self.assertTrue(context_path.is_file())
        self.assertLessEqual(context["data"]["char_count"], 4_000)
        self.assertNotIn(secret, context_path.read_text(encoding="utf-8"))

        handoff = self.base / "handoff.zip"
        made = self.payload(
            self.run_cli(
                "handoff", "build", "--project", project_id, "--output", str(handoff), "--zip", "--json"
            )
        )
        self.assertTrue(Path(made["data"]["zip"]).is_file())
        with zipfile.ZipFile(handoff) as archive:
            self.assertEqual(
                {
                    "HANDOFF.md",
                    "PROJECT_STATE.json",
                    "CHANGED_FILES.txt",
                    "NEXT_TASKS.md",
                    "KNOWN_ISSUES.md",
                    "IMPORTANT_DECISIONS.md",
                },
                set(archive.namelist()),
            )
            combined = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
            self.assertNotIn(secret, combined)

        backup = self.base / "backup.zip"
        result = self.payload(self.run_cli("backup", "--output", str(backup), "--json"))
        self.assertTrue(backup.is_file())
        self.assertEqual(str(backup.resolve()), result["data"]["output"])
        self.run_cli("restore", "--input", str(backup), "--confirm", "no", "--json", expected=4)
        restored = self.payload(
            self.run_cli("restore", "--input", str(backup), "--confirm", "RESTORE", "--json")
        )
        self.assertTrue(Path(restored["data"]["pre_restore_backup"]).is_file())
        self.run_cli("memory","edit","--id",memory["id"],"--active","no","--json")
        active_only=self.payload(self.run_cli("memory","search","--project",project_id,"UTF-8 BOM","--json")); self.assertEqual([],active_only["data"])
        any_state=self.payload(self.run_cli("memory","search","--project",project_id,"UTF-8 BOM","--active","any","--json")); self.assertEqual(memory["id"],any_state["data"][0]["id"])

    def test_demo_import_json_output_and_invalid_input(self) -> None:
        self.run_cli("init", "--json")
        project_id = self.add_project()
        imported = self.payload(
            self.run_cli(
                "memory",
                "import",
                "--project",
                project_id,
                "--input",
                str(ROOT / "samples" / "demo-memories.jsonl"),
                "--format",
                "jsonl",
                "--json",
            )
        )
        self.assertEqual(12, len(imported["data"]["created"]))
        listed = self.payload(
            self.run_cli(
                "memory",
                "list",
                "--project",
                project_id,
                "--type",
                "security",
                "--importance",
                "high",
                "--json",
            )
        )
        self.assertEqual(1, len(listed["data"]))
        doctor = self.payload(self.run_cli("doctor", "--scope", "local", "--json"))
        self.assertTrue(doctor["data"]["ok"])

        invalid = self.base / "invalid.jsonl"
        invalid.write_text("{not-json}\n", encoding="utf-8")
        failed = self.payload(
            self.run_cli(
                "memory",
                "import",
                "--project",
                project_id,
                "--input",
                str(invalid),
                "--format",
                "jsonl",
                "--json",
                expected=2,
            )
        )
        self.assertFalse(failed["ok"])
        self.assertNotIn("Traceback", json.dumps(failed))

    def test_benchmark_and_security_write_only_exact_aggregate_summaries(self) -> None:
        benchmark_result = self.payload(self.run_cli("benchmark", "--json"))
        self.assertTrue(benchmark_result["data"]["control_center_summary_saved"])
        benchmark_path = self.base / "codex-home" / "routecraft" / "benchmark" / "latest-summary.json"
        benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
        self.assertIn("benchmark_run_id", benchmark)
        self.assertNotIn("sides", benchmark)
        self.assertFalse(benchmark["measured"])

        config = self.base / "source-control.json"
        config.write_text('{"enabled":true}\n', encoding="utf-8")
        security_result = self.payload(self.run_cli(
            "security", "analyze", "--config", str(config), "--source-root", str(self.base), "--json",
        ))
        self.assertTrue(security_result["data"]["control_center_summary_saved"])
        security_path = self.base / "codex-home" / "routecraft" / "security" / "latest-summary.json"
        security = json.loads(security_path.read_text(encoding="utf-8"))
        self.assertIn("scan_id", security)
        self.assertNotIn("findings", security)

    def test_real_benchmark_preflight_is_explicit_and_model_free(self) -> None:
        class BenchmarkService:
            def initialize(self):
                return {"ok": True}

        expected = {
            "status": "PASS",
            "model_invoked": False,
            "unified_plugin": {"registration_count": 1},
        }
        args = LOCAL_CLI.build_parser().parse_args(
            ["benchmark", "--real-preflight", "--codex-bin", "codex", "--json"]
        )
        output = io.StringIO()
        with mock.patch(
            "routecraft_real_benchmark.benchmark_sandbox_preflight",
            return_value=expected,
        ) as preflight:
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, LOCAL_CLI._handle(args, BenchmarkService(), True))
        preflight.assert_called_once_with("codex")
        result = json.loads(output.getvalue())["data"]
        self.assertEqual(expected, result)

    def test_real_benchmark_preflight_maps_private_broker_failure_to_safe_cli_error(self) -> None:
        import routecraft_real_benchmark as real_benchmark

        output = io.StringIO()
        with (
            mock.patch.object(
                real_benchmark,
                "benchmark_sandbox_preflight",
                side_effect=real_benchmark.BenchmarkError("private C:/Users/name/.codex/auth.json stderr details"),
            ),
            contextlib.redirect_stdout(output),
        ):
            code = LOCAL_CLI.main([
                "--data-dir", str(self.data), "benchmark", "--real-preflight", "--codex-bin", "codex", "--json",
            ])
        payload = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertEqual("RouteCraftLocalError", payload["error"]["code"])
        self.assertIn("REAL_BENCHMARK_PREFLIGHT_FAILED", payload["error"]["message"])
        self.assertNotIn("C:/Users/name", payload["error"]["message"])
        self.assertNotIn("InternalError", json.dumps(payload))

    def test_real_benchmark_preflight_preserves_native_windows_safe_code(self) -> None:
        import routecraft_real_benchmark as real_benchmark

        output = io.StringIO()
        with (
            mock.patch.object(
                real_benchmark,
                "benchmark_sandbox_preflight",
                side_effect=real_benchmark.NativeWindowsBrokerUnsupported(),
            ),
            contextlib.redirect_stdout(output),
        ):
            code = LOCAL_CLI.main([
                "--data-dir", str(self.data), "benchmark", "--real-preflight", "--codex-bin", "codex", "--json",
            ])
        payload = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertIn(real_benchmark.NATIVE_WINDOWS_BROKER_ERROR_CODE, payload["error"]["message"])
        self.assertNotIn("auth.json", payload["error"]["message"])

    def test_graph_cli_validates_selects_ready_units_and_gates_enforce(self) -> None:
        plan_path = self.base / "graph-plan.json"
        plan_path.write_text(json.dumps({
            "graph_id": "cli-created-graph",
            "task_class": "implementation",
            "units": [
                EXECUTION_GRAPH.make_unit("a", "produce", ownership="left"),
                EXECUTION_GRAPH.make_unit("b", "check", dependencies=["a"], ownership="right"),
            ],
        }), encoding="utf-8")
        created_state = self.base / "created-graph.json"
        created = self.payload(self.run_cli(
            "graph", "create", "--input", str(plan_path), "--state-output", str(created_state), "--no-summary", "--json",
        ))
        self.assertTrue(created["data"]["valid"])
        self.assertTrue(created["data"]["state_saved"])
        self.assertFalse(created["data"]["control_center_summary_saved"])
        self.assertEqual("observe", created["data"]["mode"])
        self.assertTrue(created_state.is_file())
        graph_path = self.base / "graph.json"
        graph_path.write_text(json.dumps(EXECUTION_GRAPH.create_graph(
            "cli-graph",
            "implementation",
            [
                EXECUTION_GRAPH.make_unit("a", "produce", ownership="left"),
                EXECUTION_GRAPH.make_unit("b", "check", dependencies=["a"], ownership="right"),
            ],
            now_ms=100,
        )), encoding="utf-8")
        validated = self.payload(self.run_cli("graph", "validate", "--input", str(graph_path), "--json"))
        self.assertTrue(validated["data"]["valid"])
        self.assertEqual(1, validated["data"]["graph_schema_version"])
        ready = self.payload(self.run_cli("graph", "ready", "--input", str(graph_path), "--json"))
        self.assertEqual(["a"], ready["data"]["selected"])
        predictions_path = self.base / "predictions.json"
        predictions_path.write_text(json.dumps({"a": {"status": "accepted", "child_runs": 1}}), encoding="utf-8")
        shadow = self.payload(self.run_cli("graph", "shadow", "--input", str(graph_path), "--predictions", str(predictions_path), "--no-summary", "--json"))
        self.assertEqual("COMPILED", shadow["data"]["status"])
        self.assertEqual(1, shadow["data"]["accepted_count"])
        self.assertNotIn("child_run_count", shadow["data"])
        gated = self.payload(self.run_cli("graph", "mode", "--mode", "enforce", "--json", expected=2))
        self.assertIn("ENFORCE_BOUNDARY_UNAVAILABLE", gated["error"]["message"])
        summary = self.payload(self.run_cli("graph", "summary", "--input", str(graph_path), "--no-summary", "--json"))
        self.assertEqual(set(EXECUTION_GRAPH.GRAPH_RUN_SUMMARY_FIELDS), set(summary["data"]))

    def test_unified_doctor_exposes_v4_graph_and_evidence_fields(self) -> None:
        doctor = self.payload(self.run_cli("doctor", "--json", expected=1))["data"]
        self.assertEqual("OK", doctor["Graph Engine"])
        self.assertEqual("v1", doctor["Graph Schema"])
        self.assertIn("Graph State Store", doctor)
        self.assertIn("Checkpoint", doctor)
        self.assertIn("Lane Registry", doctor)
        self.assertEqual("UNAVAILABLE", doctor["Execution Boundary"])
        self.assertEqual("UNAVAILABLE", doctor["Trusted Evidence"])
        self.assertIn(doctor["Graph Mode"], {"off", "observe"})
        self.assertIn(doctor["Benchmark Evidence"], {"LOW", "MEDIUM", "HIGH"})
        self.assertEqual(4, doctor["details"]["collector_schema"])

    def test_durable_graph_cli_enforce_fails_closed_and_observe_plan_is_manageable(self) -> None:
        fixture = json.loads((ROOT / "samples" / "graph-ir-v1-fast-path.json").read_text(encoding="utf-8"))
        fixture["mode"] = "enforce"
        graph_input = self.base / "durable-graph.json"
        graph_input.write_text(json.dumps(fixture), encoding="utf-8")
        config = {
            "config_version": 1,
            "graph": {"mode": "enforce", "max_parallelism": 3, "max_node_attempts": 3, "max_graph_revisions": 3, "state_store": None, "checkpoint": True},
            "policy": {"production_policy": "routecraft-production-v1", "allowlisted_task_classes": ["small_bug_fix"]},
            "control_center": {"enabled": False},
        }
        config_path = self.base / "graph-config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        store = self.base / "graph-state.sqlite3"
        common = ("--config", str(config_path), "--store", str(store))

        # The standalone CLI owns no trusted executor/evidence adapter.  It
        # must refuse to create an enforce graph instead of accepting later
        # caller-provided --result/--evidence JSON.
        refused = self.payload(self.run_cli("graph", "plan", "--input", str(graph_input), *common, "--json", expected=2))
        self.assertIn("ENFORCE_BOUNDARY_UNAVAILABLE", refused["error"]["message"])

        fixture["mode"] = "observe"
        graph_input.write_text(json.dumps(fixture), encoding="utf-8")
        config["graph"]["mode"] = "observe"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        planned = self.payload(self.run_cli("graph", "plan", "--input", str(graph_input), *common, "--json"))["data"]
        self.assertTrue(planned["checkpointed"])
        self.assertEqual([], planned["ready"])
        self.assertTrue(store.is_file())
        status = self.payload(self.run_cli("graph", "status", "--graph-id", fixture["graph_id"], "--include-graph", *common, "--json"))["data"]
        self.assertEqual("COMPILED", status["graph"]["status"])
        exported = self.base / "graph-export.json"
        self.run_cli("graph", "export", "--graph-id", fixture["graph_id"], "--output", str(exported), *common, "--json")
        self.assertTrue(exported.is_file())

        candidate_status = self.payload(self.run_cli("policy", "status", *common, "--json"))["data"]
        self.assertEqual("routecraft-production-v1", candidate_status["production_policy"])
        candidates = self.payload(self.run_cli("policy", "candidates", *common, "--json"))["data"]
        self.assertEqual(0, candidates["count"])

        cancel_fixture = json.loads(json.dumps(fixture))
        cancel_fixture["graph_id"] = "g_cancel_fixture"
        cancel_input = self.base / "cancel-graph.json"
        cancel_input.write_text(json.dumps(cancel_fixture), encoding="utf-8")
        self.run_cli("graph", "plan", "--input", str(cancel_input), *common, "--json")
        self.run_cli("graph", "cancel", "--graph-id", "g_cancel_fixture", "--confirm", "wrong", *common, "--json", expected=2)
        cancelled = self.payload(self.run_cli("graph", "cancel", "--graph-id", "g_cancel_fixture", "--confirm", "g_cancel_fixture", *common, "--json"))["data"]
        self.assertEqual("CANCELLED", cancelled["status"])

        migration = self.payload(self.run_cli("migrate", "graph-config", "--config", str(self.base / "new-config.json"), "--json"))["data"]
        self.assertFalse(migration["applied"])

    def test_git_doctor_separates_tracked_changes_from_local_context(self) -> None:
        repository = self.base / "repo"
        repository.mkdir()
        subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(repository), "config", "user.name", "RouteCraft Test"], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.email", "routecraft-test@users.noreply.github.com"], check=True)
        tracked = repository / "tracked.txt"
        tracked.write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-m", "baseline"], check=True, stdout=subprocess.DEVNULL)
        local_context = repository / ".ccc"
        local_context.mkdir()
        (local_context / "last-pack.json").write_text("{}\n", encoding="utf-8")
        (repository / "other.tmp").write_text("local\n", encoding="utf-8")

        clean = LOCAL_CLI._git_worktree_breakdown(repository)
        self.assertTrue(clean["tracked_clean"])
        self.assertEqual(0, clean["tracked_changes"])
        self.assertEqual(1, clean["local_context_untracked"])
        self.assertEqual(1, clean["other_untracked"])

        tracked.write_text("changed\n", encoding="utf-8")
        dirty = LOCAL_CLI._git_worktree_breakdown(repository)
        self.assertFalse(dirty["tracked_clean"])
        self.assertEqual(1, dirty["tracked_changes"])

    def test_hardening_gate_requires_complete_successful_real_benchmark_evidence(self) -> None:
        rows = []
        for mode in ("off", "on_memory_off", "on_recall", "full_memory"):
            for metric in ("task_success", "test_pass", "acceptance_pass", "total_tokens"):
                row = {
                    "mode": mode,
                    "metric": metric,
                    "case_count": 10,
                    "sample_size": 10,
                    "available_count": 10,
                    "confidence": "medium",
                    "evidence_status": "measured",
                    "success_rate": 100.0 if metric != "total_tokens" else None,
                    "mean_value": 100.0 if metric == "total_tokens" else None,
                }
                rows.append(row)
        self.assertTrue(LOCAL_CLI._real_benchmark_gate_ready(rows))
        invalid = [dict(row) for row in rows]
        next(row for row in invalid if row["mode"] == "on_recall" and row["metric"] == "acceptance_pass")["success_rate"] = 90.0
        self.assertFalse(LOCAL_CLI._real_benchmark_gate_ready(invalid))
        invalid = [dict(row) for row in rows]
        next(row for row in invalid if row["mode"] == "full_memory" and row["metric"] == "total_tokens")["evidence_status"] = "failed"
        self.assertFalse(LOCAL_CLI._real_benchmark_gate_ready(invalid))
        self.assertFalse(LOCAL_CLI._real_benchmark_gate_ready(rows[:-1]))

    def test_doctor_defaults_to_unified_health_scope(self) -> None:
        class DoctorService:
            def initialize(self):
                return {"ok": True}

            def doctor(self):
                return {"ok": True}

        parser = LOCAL_CLI.build_parser()
        args = parser.parse_args(["doctor", "--json"])
        expected = {
            "ok": True,
            "Core": "OK",
            "Control": "DISABLED",
        }
        output = io.StringIO()
        with mock.patch.object(LOCAL_CLI, "_unified_doctor", return_value=expected):
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, LOCAL_CLI._handle(args, DoctorService(), True))
        rendered = json.loads(output.getvalue())
        self.assertEqual(expected, rendered["data"])

    def test_plugin_registration_count_reads_json_on_windows(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "installed": [
                        {"pluginId": "other@marketplace"},
                        {"pluginId": "codex-routecraft@routecraft"},
                    ],
                    "available": [],
                }
            ),
            stderr="",
        )
        with mock.patch.object(LOCAL_CLI, "_resolve_codex_executable", return_value="C:/trusted/codex.exe"), mock.patch.object(
            LOCAL_CLI.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(1, LOCAL_CLI._routecraft_plugin_registration_count())
        self.assertEqual(
            ["C:/trusted/codex.exe", "plugin", "list", "--json"],
            run.call_args.args[0],
        )

    def test_plugin_registration_count_returns_none_for_invalid_json(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
        with mock.patch.object(LOCAL_CLI, "_resolve_codex_executable", return_value="/trusted/codex"), mock.patch.object(LOCAL_CLI.subprocess, "run", return_value=completed):
            self.assertIsNone(LOCAL_CLI._routecraft_plugin_registration_count())

    def test_codex_resolution_drops_relative_path_entries_and_never_uses_cmd_shell(self) -> None:
        trusted = Path(self.base) / "trusted" / "codex.exe"
        trusted.parent.mkdir(parents=True)
        trusted.write_bytes(b"native")
        host_path_type = type(trusted)
        expected = str(trusted.resolve())

        def resolve(_name: str, *, path: str) -> str:
            self.assertNotIn(".", path.split(os.pathsep))
            return str(trusted)

        # Patching os.name changes pathlib.Path's process-wide factory on a
        # POSIX runner.  Keep this module's filesystem operations pinned to
        # the host concrete path class while exercising the Windows branch.
        with mock.patch.dict(os.environ, {"PATH": "." + os.pathsep + str(trusted.parent)}), mock.patch.object(LOCAL_CLI.os, "name", "nt"), mock.patch.object(LOCAL_CLI, "Path", host_path_type), mock.patch.object(LOCAL_CLI.shutil, "which", side_effect=resolve):
            self.assertEqual(expected, LOCAL_CLI._resolve_codex_executable())


if __name__ == "__main__":
    unittest.main()
