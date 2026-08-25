from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))

import routecraft_real_benchmark as benchmark


class RealBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suite_path = ROOT / "samples" / "real-agent-benchmark-suite.json"
        self.suite = benchmark.load_suite(self.suite_path)

    def test_bundled_suite_authorization_is_stable_across_git_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            crlf_suite = Path(temporary) / "suite.json"
            source = benchmark.DEFAULT_SUITE_PATH.read_text(encoding="utf-8")
            crlf_suite.write_bytes(source.replace("\n", "\r\n").encode("utf-8"))

            benchmark._authorize_executable_suite(crlf_suite, allow_custom=False, confirmation=None)

    def test_suite_has_all_required_categories_and_non_shell_acceptance(self) -> None:
        self.assertGreaterEqual(len(self.suite["cases"]), 10)
        categories = {case["category"] for case in self.suite["cases"]}
        self.assertTrue({
            "small_bug_fix", "multi_file_bug_fix", "refactor", "failing_test_investigation",
            "new_bounded_feature", "ci_fix", "security_configuration_fix",
            "context_heavy_investigation", "docs_code_sync", "migration_compatibility",
        }.issubset(categories))
        for case in self.suite["cases"]:
            self.assertTrue(case["acceptance"])
            self.assertTrue(all(isinstance(command, list) and command for command in case["acceptance"]))

    def test_materialize_creates_disposable_git_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "fixture"
            benchmark.materialize_case(self.suite["cases"][0], target)
            self.assertTrue((target / ".git").is_dir())
            self.assertTrue((target / "metrics.py").is_file())
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.materialize_case(self.suite["cases"][0], target)

    def test_materialize_commits_the_routecraft_contract_into_on_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "fixture"
            benchmark.materialize_case(self.suite["cases"][0], target, routecraft_contract="# Contract\n")
            self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), "# Contract\n")
            status = benchmark.subprocess.run(
                ["git", "-C", str(target), "status", "--short"],
                stdout=benchmark.subprocess.PIPE,
                stderr=benchmark.subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=True,
            )
            self.assertEqual(status.stdout, "")

    def test_mode_commands_keep_routecraft_off_isolated(self) -> None:
        off = benchmark.codex_command(codex_bin="codex", prompt="task", model="m", reasoning_effort="medium", routecraft_off=True)
        evaluation_dir = Path("evaluation")
        on = benchmark.codex_command(
            codex_bin="codex",
            prompt="task",
            model="m",
            reasoning_effort="medium",
            routecraft_off=False,
            evaluation_dir=evaluation_dir,
        )
        routecraft_override = 'plugins."codex-routecraft@routecraft".enabled=false'
        self.assertIn(routecraft_override, off)
        self.assertNotIn(routecraft_override, on)
        self.assertNotIn("--ignore-user-config", off)
        self.assertNotIn("--ignore-user-config", on)
        self.assertNotIn("--add-dir", off)
        self.assertNotIn("--add-dir", on)
        self.assertIn("--ephemeral", off)
        self.assertNotIn("workspace-write", off)
        self.assertIn("--strict-config", off)
        self.assertNotIn("--yolo", off)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", off)

    def test_codex_binary_resolution_uses_packaged_native_not_mutable_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "codex.cmd"
            launcher.write_text("untrusted launcher", encoding="utf-8")
            native = root / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"trusted native")
            host_path_type = type(native)
            expected = str(native.resolve())
            with (
                patch.object(benchmark.os, "name", "nt"),
                patch.object(benchmark, "Path", host_path_type),
                patch.object(benchmark.shutil, "which", side_effect=lambda value, **_kwargs: str(launcher) if value == "codex.cmd" else None),
            ):
                self.assertEqual(benchmark.resolve_codex_bin("codex"), expected)

    def test_native_windows_permission_profile_generation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("workspace", "evaluation", "harness", "broker"):
                (root / name).mkdir()
            with (
                patch.object(benchmark, "_is_native_windows", return_value=True),
                self.assertRaises(benchmark.NativeWindowsBrokerUnsupported) as raised,
            ):
                benchmark._permission_profile_config(
                    broker_home=root / "broker",
                    workspace=root / "workspace",
                    evaluation_dir=root / "evaluation",
                    harness=root / "harness",
                    codex_bin=sys.executable,
                )
        self.assertEqual(raised.exception.code, benchmark.NATIVE_WINDOWS_BROKER_ERROR_CODE)

    def test_posix_permission_profiles_use_canonical_deny(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("workspace", "evaluation", "harness", "broker"):
                (root / name).mkdir()
            with patch.object(benchmark, "_is_native_windows", return_value=False):
                config = benchmark._permission_profile_config(
                    broker_home=root / "broker",
                    workspace=root / "workspace",
                    evaluation_dir=root / "evaluation",
                    harness=root / "harness",
                    codex_bin=sys.executable,
                )
        self.assertIn('[permissions.benchmark-solver.filesystem]', config)
        self.assertIn('[permissions.benchmark-acceptance.filesystem]', config)
        self.assertIn('[marketplaces.routecraft]', config)
        broker_rule = f'{json.dumps(str((root / "broker").resolve()))} = "deny"'
        self.assertEqual(config.count(broker_rule), 2)
        harness_rule = f'{json.dumps(str((root / "harness").resolve()))} = "deny"'
        self.assertEqual(config.count(harness_rule), 2)
        self.assertNotIn('[windows]', config)

    def test_native_windows_preflight_stops_before_auth_copy_uac_or_model(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch.object(benchmark, "_is_native_windows", return_value=True),
            patch.object(benchmark, "resolve_codex_bin") as resolve,
            patch.object(benchmark, "_isolated_codex_home") as isolated,
            patch.object(benchmark.shutil, "copyfile") as copyfile,
            patch.object(benchmark.subprocess, "run") as process,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = benchmark.main(["preflight"])
        self.assertEqual(code, 2)
        resolve.assert_not_called()
        isolated.assert_not_called()
        copyfile.assert_not_called()
        process.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        diagnostic = json.loads(errors.getvalue())
        self.assertEqual(diagnostic["code"], benchmark.NATIVE_WINDOWS_BROKER_ERROR_CODE)
        self.assertIn("Codex CLI 0.148.0", diagnostic["message"])
        self.assertIn("WSL2", diagnostic["message"])
        self.assertFalse(diagnostic["model_invoked"])

    def test_native_windows_run_one_stops_before_fixture_or_subprocess(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(benchmark, "_is_native_windows", return_value=True),
            patch.object(benchmark, "_start_routecraft_condition") as condition,
            patch.object(benchmark, "materialize_case") as materialize,
            patch.object(benchmark, "_isolated_codex_home") as isolated,
            patch.object(benchmark.subprocess, "run") as process,
            self.assertRaises(benchmark.NativeWindowsBrokerUnsupported),
        ):
            benchmark.run_one(
                self.suite["cases"][0], "A", output_dir=Path(temporary),
                codex_bin="codex", model="gpt-5.6-luna",
                reasoning_effort="medium", timeout_seconds=5,
            )
        condition.assert_not_called()
        materialize.assert_not_called()
        isolated.assert_not_called()
        process.assert_not_called()

    def test_native_windows_run_command_stops_before_artifact_creation_or_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "must-not-exist"
            output = io.StringIO()
            errors = io.StringIO()
            with (
                patch.object(benchmark, "_is_native_windows", return_value=True),
                patch.object(benchmark, "resolve_codex_bin") as resolve,
                patch.object(benchmark, "_authorize_executable_suite") as authorize,
                patch.object(benchmark, "run_one") as run_one,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                code = benchmark.main([
                    "run", "--case", str(self.suite["cases"][0]["id"]),
                    "--mode", "A", "--output-dir", str(output_dir),
                    "--confirm-token-guard", "POST_RUN_ACCOUNTING_ONLY",
                ])
            self.assertEqual(2, code)
            self.assertFalse(output_dir.exists())
            resolve.assert_not_called()
            authorize.assert_not_called()
            run_one.assert_not_called()
            self.assertEqual("", output.getvalue())
            diagnostic = json.loads(errors.getvalue())
            self.assertEqual(benchmark.NATIVE_WINDOWS_BROKER_ERROR_CODE, diagnostic["code"])
            self.assertFalse(diagnostic["model_invoked"])

    def test_model_free_preflight_reports_only_proven_boundaries(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(benchmark, "_is_native_windows", return_value=False),
            patch.object(benchmark, "resolve_codex_bin", return_value="codex"),
            patch.object(benchmark, "_is_native_windows", return_value=False),
            patch.object(benchmark, "_isolated_codex_home", return_value=contextlib.nullcontext(Path(temporary) / "broker")),
            patch.object(
                benchmark,
                "_verify_routecraft_plugin_registration",
                return_value={"plugin_id": "codex-routecraft@routecraft", "registration_count": 1, "version": "0.7.2"},
            ),
            patch.object(benchmark, "_verify_sandbox_profiles") as verify,
        ):
            result = benchmark.benchmark_sandbox_preflight("codex")
        verify.assert_called_once()
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["model_invoked"])
        self.assertFalse(result["private_home_readable"])

    def test_preflight_failure_withholds_private_paths_and_raw_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            harness = root / "acceptance-harness"
            broker = root / "broker"
            private = root / "private-user" / ".codex" / "auth.json"
            for directory in (workspace, harness, broker, private.parent):
                directory.mkdir(parents=True, exist_ok=True)
            leaked_stderr = f"launcher failed near {private} with raw diagnostic"
            with (
                patch.object(benchmark, "_model_process_tree", return_value=(91, f"required-read-denied,{private}", leaked_stderr, False)),
                self.assertRaises(benchmark.BenchmarkError) as raised,
            ):
                benchmark._verify_sandbox_profiles(
                    codex_bin="codex",
                    broker_home=broker,
                    workspace=workspace,
                    harness=harness,
                    private_sentinel=private,
                    environment={},
                )
        message = str(raised.exception)
        self.assertIn(benchmark.PREFLIGHT_ISOLATION_ERROR_CODE, message)
        self.assertIn("required-read-denied", message)
        self.assertIn("raw child output withheld", message)
        self.assertNotIn(str(private), message)
        self.assertNotIn("raw diagnostic", message)

    def test_preflight_probes_solver_outer_and_acceptance_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            harness = root / "acceptance-harness"
            broker = root / "broker"
            private = root / "private" / "auth.json"
            for directory in (workspace / ".git", harness, broker, private.parent):
                directory.mkdir(parents=True, exist_ok=True)
            (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            with patch.object(benchmark, "_model_process_tree", return_value=(0, "ROUTECRAFT_SANDBOX_OK\n", "", False)) as process:
                benchmark._verify_sandbox_profiles(
                    codex_bin="codex", broker_home=broker, workspace=workspace,
                    harness=harness, private_sentinel=private, environment={},
                )
        profiles = [call.args[0][3] for call in process.call_args_list]
        self.assertEqual(profiles, ["benchmark-solver", "benchmark-outer", "benchmark-acceptance"])
        self.assertFalse((harness / ".routecraft-boundary-probe").exists())

    def test_isolated_posix_broker_never_copies_windows_sandbox_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_home = root / "source-codex-home"
            workspace = root / "workspace"
            evaluation = root / "evaluation"
            harness = root / "harness"
            for directory in (source_home, workspace, evaluation, harness):
                directory.mkdir()
            (source_home / "auth.json").write_text('{"test": true}', encoding="utf-8")
            sandbox_secret = source_home / ".sandbox-secrets"
            sandbox_secret.write_text("must-not-copy", encoding="utf-8")
            real_copyfile = benchmark.shutil.copyfile
            with (
                patch.dict(benchmark.os.environ, {"CODEX_HOME": str(source_home)}, clear=False),
                patch.object(benchmark, "_is_native_windows", return_value=False),
                patch.object(benchmark, "_verify_codex_cli_identity", return_value=sys.executable),
                patch.object(benchmark, "_install_isolated_routecraft_plugin"),
                patch.object(benchmark.shutil, "copyfile", wraps=real_copyfile) as copyfile,
                patch.object(
                    benchmark.shutil,
                    "copytree",
                    side_effect=lambda _source, destination, **_kwargs: Path(destination).mkdir(parents=True),
                ),
            ):
                with benchmark._isolated_codex_home(
                    workspace=workspace,
                    evaluation_dir=evaluation,
                    harness=harness,
                    codex_bin=sys.executable,
                ) as broker_home:
                    self.assertFalse((broker_home / ".sandbox-secrets").exists())
            copied_sources = {Path(call.args[0]).resolve() for call in copyfile.call_args_list}
            self.assertNotIn(sandbox_secret.resolve(), copied_sources)

    def test_graph_observe_condition_compiles_a_real_durable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evaluation = Path(temporary) / "evaluation"
            evaluation.mkdir()
            result = benchmark._start_graph_condition(self.suite["cases"][0], "observe", evaluation)
            graph_store = Path(temporary) / "graph-state" / "graph.sqlite3"
            self.assertTrue(graph_store.is_file())
            self.assertTrue(result["condition_pass"])
            self.assertEqual(result["mode"], "observe")
            self.assertEqual(result["graph_schema_version"], 1)
            self.assertGreaterEqual(result["checkpoint_count"], 1)
            self.assertNotIn(str(Path(temporary).resolve()), json.dumps(result))

    def test_graph_enforce_condition_is_unavailable_without_trusted_host_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evaluation = Path(temporary) / "evaluation"
            evaluation.mkdir()
            with self.assertRaisesRegex(benchmark.BenchmarkError, "trusted host execution/evidence boundary"):
                benchmark._start_graph_condition(self.suite["cases"][0], "enforce", evaluation)

    def test_graph_enforce_selection_stops_before_model_invocation(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(benchmark, "_is_native_windows", return_value=False),
            patch.object(benchmark, "resolve_codex_bin", return_value="codex"),
            patch.object(benchmark, "run_one") as run_one,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = benchmark.main([
                "run",
                "--case", str(self.suite["cases"][0]["id"]),
                "--mode", "F",
                "--output-dir", temporary,
                "--confirm-token-guard", "POST_RUN_ACCOUNTING_ONLY",
            ])
        self.assertEqual(code, 2)
        run_one.assert_not_called()
        planned = json.loads(output.getvalue().splitlines()[0])
        self.assertFalse(planned["graph_enforce_available"])
        self.assertIn("trusted_host_execution_and_evidence_boundary", planned["graph_enforce_requirement"])
        self.assertIn("no model was invoked", errors.getvalue())

    def test_real_run_requires_accounting_only_token_guard_acknowledgement(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(benchmark, "_is_native_windows", return_value=False),
            patch.object(benchmark, "resolve_codex_bin", return_value="codex"),
            patch.object(benchmark, "run_one") as run_one,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = benchmark.main([
                "run",
                "--case", str(self.suite["cases"][0]["id"]),
                "--mode", "A",
                "--output-dir", temporary,
            ])
        self.assertEqual(2, code)
        run_one.assert_not_called()
        planned = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual("post_run_accounting", planned["token_ceiling_enforcement"])
        self.assertFalse(planned["hard_provider_token_cap"])
        self.assertIn("POST_RUN_ACCOUNTING_ONLY", errors.getvalue())

    def test_run_one_configures_unique_memory_directory_and_preserves_unavailable_metrics(self) -> None:
        captured: list[tuple[list[str], dict[str, str] | None]] = []
        policy_sha256 = "a" * 64
        category = str(self.suite["cases"][0]["category"])
        risk = benchmark._benchmark_risk(category)
        plan = (
            "ROUTECRAFT PLAN\nexecution: solo\nlane: luna-medium\nreview: self\nparallelism: 1\n"
            f"risk: {risk}\nmemory: recall\npolicy_sha256: {policy_sha256}\n"
            "reason: bounded benchmark policy fixes one isolated solver lane\nEND ROUTECRAFT PLAN"
        )
        condition = (
            "done\n\nROUTECRAFT CONDITION\nmode: recall\n"
            f"policy_sha256: {policy_sha256}\nmemory_useful_ranks: none\n"
            "learning_gate: mode_recall_only\nstatus: applied\nEND ROUTECRAFT CONDITION"
        )
        codex_stdout = "\n".join((
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": plan}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": condition}}),
        )) + "\n"

        def materialize(_case, target, *, routecraft_contract=None):
            target.mkdir(parents=True, exist_ok=True)
            self.assertIsNotNone(routecraft_contract)
            return target

        def fake_run(command, **kwargs):
            command = list(command)
            captured.append((command, kwargs.get("env")))
            if command[0] == sys.executable:
                return SimpleNamespace(returncode=0, stdout="{}", stderr="")
            if command[0] == "codex":
                return SimpleNamespace(returncode=0, stdout=codex_stdout, stderr="")
            if command[0] == "git":
                return SimpleNamespace(returncode=0, stdout="metrics.py\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        def fake_model(command, *, cwd, environment, timeout_seconds):
            captured.append((list(command), dict(environment)))
            return 0, codex_stdout, "", False

        routecraft_state = {
            "enabled": True,
            "condition_pass": True,
            "prompt_context": "ROUTECRAFT HARNESS\nmode recall\n",
            "task_id": None,
            "mode": "recall",
            "error": None,
            "record_ids": [],
            "policy_sha256": policy_sha256,
            "contract": benchmark._routecraft_contract(mode="C", category=category, policy_sha256=policy_sha256),
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(benchmark, "materialize_case", side_effect=materialize),
            patch.object(benchmark, "_is_native_windows", return_value=False),
            patch.object(benchmark, "_verify_codex_cli_identity", return_value="codex"),
            patch.object(benchmark.subprocess, "run", side_effect=fake_run),
            patch.object(benchmark, "_model_process_tree", side_effect=fake_model),
            patch.object(benchmark, "_start_routecraft_condition", return_value=routecraft_state),
            patch.object(benchmark, "_finish_routecraft_condition"),
            patch.object(benchmark, "_evaluation_metrics", return_value=(None, 0)),
            patch.object(benchmark, "_isolated_codex_home", return_value=contextlib.nullcontext(Path(temporary) / "broker")),
            patch.object(
                benchmark,
                "_verify_routecraft_plugin_registration",
                return_value={"plugin_id": "codex-routecraft@routecraft", "registration_count": 1, "version": "0.7.2"},
            ),
            patch.object(benchmark, "_verify_sandbox_profiles"),
        ):
            result = benchmark.run_one(self.suite["cases"][0], "C", output_dir=Path(temporary), codex_bin="codex", model="gpt-5.6-luna", reasoning_effort="medium", timeout_seconds=5)
            codex_calls = [(command, env) for command, env in captured if command[0] == "codex" and "exec" in command]
            self.assertEqual(len(codex_calls), 1)
            self.assertNotIn("--ignore-user-config", codex_calls[0][0])
            self.assertIn("ROUTECRAFT_EVALUATION_DIR", codex_calls[0][1] or {})
            self.assertEqual((codex_calls[0][1] or {}).get("ROUTECRAFT_PYTHON"), str(Path(sys.executable).resolve()))
            self.assertEqual(result["input_tokens"], 12)
            self.assertEqual(result["output_tokens"], 4)
            self.assertEqual(result["total_tokens"], 16)
            self.assertIsNone(result["cached_tokens"])
            self.assertIsNone(result["memory_recall_count"])
            self.assertIn("ROUTECRAFT HARNESS", codex_calls[0][0][-1])
            self.assertTrue(result["routecraft_condition_pass"])
            self.assertTrue(result["routecraft_marker_pass"])
            self.assertEqual(result["lane_distribution"], {"luna": 1})
            self.assertTrue((Path(temporary) / "artifacts" / result["run_id"] / "codex.ndjson").is_file())

    def test_model_timeout_terminates_the_posix_process_group(self) -> None:
        process = mock.Mock(pid=4242, returncode=None)
        process.communicate.side_effect = [
            benchmark.subprocess.TimeoutExpired(["codex"], 1),
            ("partial", "stopped"),
        ]
        with (
            patch.object(benchmark.os, "name", "posix"),
            patch.object(benchmark.subprocess, "Popen", return_value=process) as popen,
            patch.object(benchmark.os, "killpg", create=True) as killpg,
            patch.object(benchmark.signal, "SIGKILL", 9, create=True),
        ):
            result = benchmark._model_process_tree(
                ["codex", "sandbox"], cwd=ROOT, environment={}, timeout_seconds=1
            )
        self.assertEqual((None, "partial", "stopped", True), result)
        self.assertEqual(mock.call(4242, benchmark.signal.SIGTERM), killpg.call_args_list[0])
        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_readonly_recall_builds_an_in_memory_index_without_sync(self) -> None:
        entry = {
            "id": "CASE-20260823T160101Z-DESKTOPM29B5-4680",
            "title": "Bounded prior case",
            "kind": "case",
            "updated_at": "2026-08-23T16:01:01Z",
            "excerpt": "Current evidence still takes precedence.",
        }
        with (
            patch.object(benchmark, "build_index", return_value={"records": [entry]}) as build,
            patch.object(benchmark, "score_entry", return_value=(9.0, ["bounded"])),
            patch.object(benchmark.subprocess, "run") as process,
        ):
            result = benchmark._readonly_recall(Path("memory"), "bounded task")
        build.assert_called_once_with(Path("memory"), write=False)
        process.assert_not_called()
        self.assertEqual(result["match_count"], 1)
        self.assertNotIn("path", result["matches"][0])

    def test_ndjson_drift_and_null_semantics(self) -> None:
        result = benchmark.parse_ndjson("not-json\n{\"type\":\"event\",\"usage\":{\"total_tokens\":9}}\n")
        self.assertEqual(result["total_tokens"], 9)
        self.assertIsNone(result["input_tokens"])
        self.assertEqual(result["ndjson_malformed_lines"], 1)

    def test_routecraft_markers_prove_on_policy_and_reject_off_contamination(self) -> None:
        policy_sha256 = "b" * 64
        category = "security_configuration_fix"
        plan = (
            "ROUTECRAFT PLAN\nexecution: solo\nlane: luna-medium\nreview: self\nparallelism: 1\n"
            f"risk: high\nmemory: full\npolicy_sha256: {policy_sha256}\n"
            "reason: bounded benchmark policy fixes one isolated solver lane\nEND ROUTECRAFT PLAN"
        )
        final = (
            "verified\nROUTECRAFT CONDITION\nmode: full\n"
            f"policy_sha256: {policy_sha256}\nmemory_useful_ranks: 1,3\n"
            "learning_gate: no_reusable_learning\nstatus: applied\nEND ROUTECRAFT CONDITION"
        )
        stream = "\n".join((
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": plan}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": final}}),
        ))
        inspected = benchmark.inspect_routecraft_markers(
            stream,
            mode="D",
            category=category,
            policy_sha256=policy_sha256,
            record_ids=("record-1", "record-2", "record-3"),
        )
        self.assertTrue(inspected["routecraft_marker_pass"])
        self.assertEqual(inspected["routecraft_memory_useful_count"], 2)
        self.assertEqual(inspected["routecraft_memory_useful_ids"], ["record-1", "record-3"])
        contaminated = benchmark.inspect_routecraft_markers(
            stream,
            mode="A",
            category=category,
            policy_sha256=None,
        )
        self.assertFalse(contaminated["routecraft_marker_pass"])

    def test_statistics_and_aggregate_do_not_leak_prompt_or_paths(self) -> None:
        summary = benchmark.summarize([
            {"schema_version": benchmark.SCHEMA_VERSION, "routecraft_condition_pass": True, "mode": "A", "case_id": "a", "task_success": True, "tests_pass": True, "acceptance_pass": True, "wall_time_ms": 10, "total_tokens": None},
            {"schema_version": benchmark.SCHEMA_VERSION, "routecraft_condition_pass": True, "mode": "A", "case_id": "b", "task_success": False, "tests_pass": False, "acceptance_pass": False, "wall_time_ms": 30, "total_tokens": 8},
        ], suite_id="suite")
        mode = summary["modes"]["A"]
        self.assertEqual(mode["sample_size"], 2)
        self.assertEqual(mode["wall_time_ms"]["median"], 20.0)
        self.assertEqual(mode["confidence"], "low")
        self.assertEqual(mode["evidence_status"], "insufficient_evidence")
        aggregate = benchmark.to_d1_aggregate(summary, device_id="0123456789abcdef")
        encoded = json.dumps(aggregate)
        self.assertNotIn("prompt", encoded.lower())
        self.assertNotIn("workspace", encoded.lower())
        self.assertNotIn("artifact", encoded.lower())
        self.assertNotIn("path", encoded.lower())
        self.assertEqual(len(aggregate), len(benchmark.MODE_SPECS) * len(benchmark.D1_METRICS))
        total = next(row for row in aggregate if row["mode"] == "off" and row["metric"] == "total_tokens")
        self.assertEqual(set(total), {
            "evidence_id", "device_id", "observed_at", "suite_version", "mode", "metric", "case_count", "sample_size",
            "available_count", "mean_value", "median_value", "min_value", "max_value", "success_count", "success_rate",
            "confidence", "evidence_status",
        })
        self.assertEqual(total["mean_value"], 8.0)
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.to_d1_aggregate(summary, device_id="private-device-name")
        sensitive_suite = json.loads(json.dumps(summary))
        sensitive_suite["suite_id"] = "C:/Users/private/benchmark"
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.to_d1_aggregate(sensitive_suite, device_id="0123456789abcdef")
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.to_d1_aggregate(summary, device_id="0123456789abcdef", observed_at="2026-08-25T99:99:99Z")
        impossible_rate = json.loads(json.dumps(summary))
        impossible_rate["modes"]["A"]["metric_evidence"]["total_tokens"]["success_rate"] = 101
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.to_d1_aggregate(impossible_rate, device_id="0123456789abcdef")

    def test_invalid_routecraft_condition_cannot_be_aggregated_as_measured(self) -> None:
        records = [
            {
                "schema_version": benchmark.SCHEMA_VERSION,
                "routecraft_condition_pass": index != 9,
                "mode": "C",
                "case_id": f"case-{index}",
                "task_success": True,
                "tests_pass": True,
                "acceptance_pass": True,
                "total_tokens": 100,
            }
            for index in range(10)
        ]
        summary = benchmark.summarize(records, suite_id="suite", planned_case_count=10)
        mode = summary["modes"]["C"]
        self.assertEqual(mode["sample_size"], 10)
        self.assertEqual(mode["valid_condition_count"], 9)
        self.assertEqual(mode["evidence_status"], "failed")
        self.assertIsNone(mode["task_success_rate"])
        self.assertEqual(mode["metric_evidence"]["total_tokens"]["available_count"], 0)

    def test_atomic_d1_output_contains_only_exact_rows(self) -> None:
        summary = benchmark.summarize([], suite_id="suite", planned_case_count=10)
        rows = benchmark.to_d1_aggregate(summary, device_id="0123456789abcdef")
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "nested" / "real-d1-summary.json"
            benchmark._write_json(target, rows)
            self.assertEqual(rows, json.loads(target.read_text(encoding="utf-8")))

    def test_invalid_fixture_path_is_rejected(self) -> None:
        self.assertEqual(benchmark._relative_path('.github/workflows/ci.yml').as_posix(), '.github/workflows/ci.yml')
        bad = json.loads(json.dumps(self.suite))
        bad["cases"][0]["files"][0]["path"] = "../outside.py"
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "suite.json"
            target.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.load_suite(target)


if __name__ == "__main__":
    unittest.main()
