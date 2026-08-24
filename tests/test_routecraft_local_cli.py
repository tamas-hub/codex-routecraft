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
        self.assertEqual("routecraft 1.0.0", version.stdout.decode("utf-8").strip())

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
        with mock.patch.object(LOCAL_CLI.os, "name", "nt"), mock.patch.object(
            LOCAL_CLI.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(1, LOCAL_CLI._routecraft_plugin_registration_count())
        self.assertEqual(
            ["cmd.exe", "/d", "/c", "codex.cmd", "plugin", "list", "--json"],
            run.call_args.args[0],
        )

    def test_plugin_registration_count_returns_none_for_invalid_json(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
        with mock.patch.object(LOCAL_CLI.subprocess, "run", return_value=completed):
            self.assertIsNone(LOCAL_CLI._routecraft_plugin_registration_count())


if __name__ == "__main__":
    unittest.main()
