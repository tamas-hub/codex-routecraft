from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_local_release.py"
EVALUATOR = ROOT / "scripts" / "evaluate_routecraft_local.py"


class RouteCraftLocalReleaseTests(unittest.TestCase):
    def run_python(self, *args: str, cwd: Path | None = None, expected: int = 0) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.run(
            [sys.executable, *args],
            cwd=str(cwd or ROOT),
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=60,
            check=False,
        )
        self.assertEqual(expected, process.returncode, msg=process.stdout + "\n" + process.stderr)
        return process

    def test_evaluation_harness(self) -> None:
        result = json.loads(self.run_python(str(EVALUATOR)).stdout)
        self.assertTrue(result["passed"], result)
        self.assertEqual(1.0, result["hit_at_k"])
        self.assertTrue(result["inactive_excluded"])
        self.assertEqual([], result["context_duplicate_titles"])

    def test_orchestration_reference_uses_the_canonical_durable_graph_path(self) -> None:
        reference = (ROOT / "plugins" / "codex-routecraft" / "skills" / "orchestration" / "references" / "execution-graph.md").read_text(encoding="utf-8")
        skill = (ROOT / "plugins" / "codex-routecraft" / "skills" / "orchestration" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("graph validate", reference)
        self.assertIn("graph plan", reference)
        self.assertIn("graph status", reference)
        self.assertIn("dedicated SQLite Graph State Store", reference)
        self.assertNotIn("graph create `", reference)
        self.assertNotIn("--state-output <caller-work>", reference)
        self.assertIn("Graph IR v1", skill)
        self.assertIn("trusted host execution/evidence boundary", skill)

    def test_release_builder_is_deterministic_and_smoke_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = base / "first"
            second = base / "second"
            manifest1 = json.loads(self.run_python(str(BUILDER), "--output-dir", str(first)).stdout)
            manifest2 = json.loads(self.run_python(str(BUILDER), "--output-dir", str(second)).stdout)
            self.assertEqual(
                [item["sha256"] for item in manifest1["artifacts"]],
                [item["sha256"] for item in manifest2["artifacts"]],
            )
            checksums = {}
            for line in (first / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines():
                digest, name = line.split("  ", 1)
                checksums[name] = digest
            for item in manifest1["artifacts"]:
                archive_path = first / item["file"]
                self.assertEqual(item["sha256"], hashlib.sha256(archive_path.read_bytes()).hexdigest())
                self.assertEqual(item["sha256"], checksums[item["file"]])
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertIsNone(archive.testzip())
                    names = archive.namelist()
                    self.assertTrue(any(name.endswith("/app/routecraft_local/loop_bridge.py") for name in names))
                    self.assertTrue(any(name.endswith("/app/routecraft_execution_graph.py") for name in names))
                    self.assertTrue(any(name.endswith("/app/routecraft_graph_cli.py") for name in names))
                    self.assertTrue(any(name.endswith("/app/routecraft_graph/store.py") for name in names))
                    self.assertTrue(any(name.endswith("/app/routecraft_graph/engine.py") for name in names))
                    self.assertTrue(any(name.endswith("/app/routecraft_graph_telemetry.py") for name in names))
                    self.assertTrue(any(name.endswith("/app/routecraft_legacy_observation.py") for name in names))
                    self.assertTrue(any(name.endswith("/app/routecraft_real_benchmark.py") for name in names))
                    self.assertTrue(any(name.endswith("/app/routecraft_security_validation.py") for name in names))
                    self.assertTrue(any(name.endswith("/docs/HARDENING_GRAPH_FOUNDATION.ja.md") for name in names))
                    self.assertTrue(any(name.endswith("/docs/ADR-0007-EVIDENCE-DRIVEN-DURABLE-GRAPH.ja.md") for name in names))
                    for name in archive.namelist():
                        path = PurePosixPath(name)
                        self.assertFalse(path.is_absolute())
                        self.assertNotIn("..", path.parts)
                        lowered = name.lower()
                        self.assertNotIn("/.env", lowered)
                        self.assertFalse(lowered.endswith((".db", ".sqlite", ".sqlite3", ".pem", ".key")))
                    combined = b"\n".join(
                        archive.read(name)
                        for name in archive.namelist()
                        if name.endswith((".py", ".js", ".css", ".html", ".md", ".json", ".jsonl", "VERSION"))
                    )
                    self.assertNotIn(b"C:\\Users", combined)
                    if item["file"].endswith("macos.zip"):
                        launcher = next(name for name in archive.namelist() if name.endswith("/routecraft"))
                        info = archive.getinfo(launcher)
                        self.assertEqual(3, info.create_system)
                        mode = info.external_attr >> 16
                        self.assertTrue(mode & stat.S_IXUSR)
                        self.assertEqual(0o755, mode & 0o777)

            windows = next(first / item["file"] for item in manifest1["artifacts"] if item["file"].endswith("windows.zip"))
            extract = base / "extract"
            with zipfile.ZipFile(windows) as archive:
                archive.extractall(extract)
            root = next(extract.iterdir())
            launcher = root / "app" / "routecraft.py"
            version = self.run_python(str(launcher), "--version", cwd=root)
            self.assertEqual("routecraft 0.7.4 (memory-local 1.0.0)", version.stdout.strip())
            data = base / "smoke-data"
            self.run_python(str(launcher), "--data-dir", str(data), "init", cwd=root)
            project = json.loads(
                self.run_python(
                    str(launcher),
                    "--data-dir",
                    str(data),
                    "project",
                    "add",
                    "--name",
                    "配布テスト",
                    "--json",
                    cwd=root,
                ).stdout
            )["data"]
            self.run_python(
                str(launcher),
                "--data-dir",
                str(data),
                "memory",
                "import",
                "--project",
                project["id"],
                "--input",
                str(root / "samples" / "demo-memories.jsonl"),
                "--format",
                "jsonl",
                cwd=root,
            )
            search = self.run_python(
                str(launcher),
                "--data-dir",
                str(data),
                "memory",
                "search",
                "--project",
                project["id"],
                "日本語 stdin",
                cwd=root,
            )
            self.assertIn("日本語stdin", search.stdout)


if __name__ == "__main__":
    unittest.main()
