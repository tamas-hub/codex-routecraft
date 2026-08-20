from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMORY = ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_memory.py"
EVALUATOR = ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_evaluation.py"


class RouteCraftEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = self.base / "memory"
        self.eval_dir = self.base / "evaluation"
        self.config = self.base / "memory-config.json"
        self.env = os.environ.copy()
        self.env["ROUTECRAFT_MEMORY_CONFIG"] = str(self.config)
        self.env["ROUTECRAFT_EVALUATION_DIR"] = str(self.eval_dir)
        self.env["ROUTECRAFT_DEVICE_ID"] = "devicea"
        self.run_memory("init", "--store", str(self.store), "--git-init")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cmd(self, script: Path, *args: str, env: dict[str, str] | None = None, check: bool = True):
        process = subprocess.run(
            [sys.executable, str(script), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or self.env,
            check=False,
        )
        if check and process.returncode != 0:
            self.fail(
                f"command failed: {script.name} {args}\n"
                f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
            )
        return process

    def run_memory(self, *args: str, env: dict[str, str] | None = None):
        return self.run_cmd(MEMORY, *args, env=env)

    def run_eval(self, *args: str, env: dict[str, str] | None = None):
        return self.run_cmd(EVALUATOR, "--dir", str(self.eval_dir), *args, env=env)

    def create_case(self, *, device: str, repository: str, title: str) -> str:
        env = dict(self.env)
        env["ROUTECRAFT_DEVICE_ID"] = device
        packet = self.base / f"{device}-{title.replace(' ', '-')}.json"
        packet.write_text(
            json.dumps(
                {
                    "kind": "case",
                    "title": title,
                    "repository": repository,
                    "sections": {
                        "Problem": "A verified problem occurred.",
                        "Root cause": "The root cause was independently verified.",
                        "Verification": "A deterministic regression check passed.",
                        "Reusable lesson": "Reuse the verified diagnostic path before rediscovery.",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = json.loads(
            self.run_memory("learn", "--store", str(self.store), "--input", str(packet), env=env).stdout
        )
        return result["created"][0]

    def test_evaluation_is_local_opt_in(self) -> None:
        summary = json.loads(self.run_eval("summary", "--json").stdout)
        self.assertFalse(summary["tracking"]["enabled"])
        self.assertEqual(summary["metrics"]["completed_tasks"], 0)
        self.assertEqual(summary["metrics"]["privacy_violations"], 0)

        configured = json.loads(self.run_eval("configure", "--enable", "--mode", "full", "--json").stdout)
        self.assertTrue(configured["enabled"])
        self.assertEqual(configured["mode"], "full")

    def test_task_feedback_detects_cross_project_and_cross_device_reuse_without_raw_query(self) -> None:
        local_case = self.create_case(device="devicea", repository="example/repo-a", title="Local case")
        remote_case = self.create_case(device="deviceb", repository="example/repo-b", title="Transferred case")
        self.run_eval("configure", "--enable", "--mode", "full", "--json")

        product = self.base / "product"
        subprocess.run(["git", "init", "-b", "main", str(product)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(
            ["git", "-C", str(product), "remote", "add", "origin", "https://github.com/example/repo-a.git"],
            check=True,
        )
        started = json.loads(
            self.run_eval(
                "start", "--repo-path", str(product), "--task-class", "debugging", "--risk", "low", "--json"
            ).stdout
        )
        task_id = started["task_id"]
        self.run_eval(
            "recall",
            "--task-id", task_id,
            "--store", str(self.store),
            "--record", f"{local_case}:1",
            "--record", f"{remote_case}:2",
            "--json",
        )
        self.run_eval(
            "finish",
            "--task-id", task_id,
            "--outcome", "success",
            "--elapsed-seconds", "90",
            "--tool-calls", "8",
            "--failed-hypotheses", "1",
            "--useful-record", remote_case,
            "--learned-record", local_case,
            "--source-chars", "10000",
            "--record-chars", "600",
            "--json",
        )
        summary = json.loads(self.run_eval("summary", "--json").stdout)
        metrics = summary["metrics"]
        self.assertEqual(metrics["completed_tasks"], 1)
        self.assertEqual(metrics["cross_project_useful"], 1)
        self.assertEqual(metrics["cross_device_useful"], 1)
        self.assertEqual(metrics["privacy_violations"], 0)
        self.assertAlmostEqual(metrics["decision_compression_ratio"], 0.94, places=2)
        self.assertIsNone(summary["scorecard"]["score_100"])
        self.assertEqual(summary["scorecard"]["status"], "insufficient-data")

        raw = (self.eval_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(str(self.base), raw)
        self.assertNotIn("github.com/example/repo-a.git", raw)
        self.assertNotIn('"query"', raw)
        self.assertIn('"repository":"example/repo-a"', raw)

    def test_round_robin_experiment_assigns_off_recall_full(self) -> None:
        self.run_eval(
            "configure",
            "--enable",
            "--experiment", "round-robin",
            "--sequence", "off", "recall", "full",
            "--json",
        )
        modes = []
        for _ in range(3):
            started = json.loads(
                self.run_eval(
                    "start",
                    "--repository", "example/repo",
                    "--task-class", "implementation",
                    "--risk", "low",
                    "--json",
                ).stdout
            )
            modes.append(started["mode"])
        self.assertEqual(modes, ["off", "recall", "full"])

    def test_retrieval_benchmark_reports_hit_recall_and_mrr_without_persisting_queries(self) -> None:
        case_id = self.create_case(
            device="devicea",
            repository="example/repo",
            title="Durable storage survives restart",
        )
        suite = self.base / "benchmark.json"
        private_query = "durable storage restart private benchmark phrase"
        suite.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "cases": [
                        {
                            "name": "durability",
                            "query": private_query,
                            "tags": [],
                            "expected": [case_id],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = json.loads(
            self.run_eval(
                "benchmark",
                "--store", str(self.store),
                "--suite", str(suite),
                "--limit", "1",
                "--json",
            ).stdout
        )
        self.assertEqual(result["cases"], 1)
        self.assertEqual(result["hit_at_k"], 1.0)
        self.assertEqual(result["recall_at_k"], 1.0)
        self.assertEqual(result["mrr"], 1.0)
        persisted = (self.eval_dir / "benchmark-last.json").read_text(encoding="utf-8")
        self.assertNotIn(private_query, persisted)

    def test_rejects_ambiguous_multi_verdict_feedback(self) -> None:
        case_id = self.create_case(device="devicea", repository="example/repo", title="Case")
        self.run_eval("configure", "--enable", "--json")
        started = json.loads(
            self.run_eval(
                "start", "--repository", "example/repo", "--task-class", "general", "--risk", "low", "--json"
            ).stdout
        )
        failed = self.run_cmd(
            EVALUATOR,
            "--dir", str(self.eval_dir),
            "finish",
            "--task-id", started["task_id"],
            "--outcome", "success",
            "--useful-record", case_id,
            "--misleading-record", case_id,
            "--json",
            env=self.env,
            check=False,
        )
        self.assertEqual(failed.returncode, 2)
        self.assertIn("more than one final verdict", failed.stderr)


if __name__ == "__main__":
    unittest.main()
