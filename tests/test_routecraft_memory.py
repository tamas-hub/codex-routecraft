from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_memory.py"


class RouteCraftMemoryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config = self.base / "config.json"
        self.env = os.environ.copy()
        self.env["ROUTECRAFT_MEMORY_CONFIG"] = str(self.config)
        self.env["ROUTECRAFT_DEVICE_ID"] = "testbox"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            check=False,
        )
        if check and proc.returncode != 0:
            self.fail(f"command failed: {args}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return proc

    def write_packet(self, name: str, payload: dict) -> Path:
        path = self.base / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def init_store(self, name: str = "store") -> Path:
        store = self.base / name
        self.run_cli("init", "--store", str(store))
        return store

    def test_learn_recall_reinforce_and_promote(self) -> None:
        store = self.init_store()
        first = self.write_packet(
            "first.json",
            {
                "kind": "case",
                "title": "Expo persistence reset after OS update",
                "tags": ["expo", "ios", "persistence"],
                "scope": ["react-native"],
                "repository": "example/app",
                "outcome": "fixed",
                "sections": {
                    "Problem": "Learning progress reset after the application was suspended.",
                    "Root cause": "State was written to a volatile cache instead of durable storage.",
                    "Verification": "Restarted the app and reran the persistence regression test.",
                    "Reusable lesson": "Check storage durability before blaming the OS update.",
                },
                "candidate": {
                    "title": "Check storage durability before OS regression hypotheses",
                    "tags": ["persistence", "debugging"],
                    "scope": ["mobile"],
                    "sections": {
                        "Observation": "Persistence reports can be caused by volatile storage selection.",
                        "Possible decision value": "Inspect the storage adapter before broad OS debugging.",
                        "Promotion condition": "Confirm in another independent repository.",
                    },
                },
            },
        )
        first_result = json.loads(
            self.run_cli("learn", "--store", str(store), "--input", str(first)).stdout
        )
        self.assertEqual(len(first_result["created"]), 2)
        case1, candidate_id = first_result["created"]
        self.assertTrue(case1.startswith("CASE-"))
        self.assertTrue(candidate_id.startswith("CAND-"))

        recall = self.run_cli(
            "recall",
            "--store",
            str(store),
            "--query",
            "iOS persistence durable storage",
            "--json",
        )
        recall_result = json.loads(recall.stdout)
        self.assertGreaterEqual(recall_result["match_count"], 1)
        self.assertIn(case1, [item["id"] for item in recall_result["matches"]])

        second = self.write_packet(
            "second.json",
            {
                "kind": "case",
                "title": "Desktop state disappeared after restart",
                "tags": ["persistence", "desktop"],
                "sections": {
                    "Problem": "Saved state disappeared after a process restart.",
                    "Root cause": "The implementation used a temporary directory.",
                    "Verification": "Restarted twice and checked the durable data path.",
                    "Reusable lesson": "Validate the persistence boundary before runtime hypotheses.",
                },
                "reinforce_candidates": [candidate_id],
            },
        )
        second_result = json.loads(
            self.run_cli("learn", "--store", str(store), "--input", str(second)).stdout
        )
        self.assertIn(candidate_id, second_result["updated_candidates"])
        self.assertIn(candidate_id, second_result["eligible_for_promotion"])

        promote_packet = self.write_packet(
            "promote.json",
            {
                "candidate_id": candidate_id,
                "title": "Validate storage durability before broad runtime diagnosis",
                "decision": "When state disappears after restart, verify the storage path and durability contract before blaming an OS or runtime update.",
                "when_to_apply": "State or progress disappears after process restart, suspension, or device reboot.",
                "when_not_to_apply": "Evidence already proves a serialization or migration failure.",
                "rationale": "Two independent cases showed that volatile storage created the same symptom.",
                "verification": "Restart the process and inspect the resolved durable storage location.",
            },
        )
        promoted = json.loads(
            self.run_cli("promote", "--store", str(store), "--input", str(promote_packet)).stdout
        )
        self.assertTrue(promoted["rule"].startswith("RULE-"))

        validation = self.run_cli("validate", "--store", str(store))
        self.assertIn("validation OK", validation.stdout)

        rule_recall = json.loads(
            self.run_cli(
                "recall",
                "--store",
                str(store),
                "--query",
                "storage durability restart",
                "--json",
            ).stdout
        )
        self.assertEqual(rule_recall["matches"][0]["kind"], "rule")

    def test_promotion_gate_rejects_single_observation(self) -> None:
        store = self.init_store()
        packet = self.write_packet(
            "candidate.json",
            {
                "kind": "candidate",
                "title": "One-off hypothesis",
                "evidence": ["CASE-ONLY-ONE"],
                "sections": {
                    "Observation": "Observed once.",
                    "Possible decision value": "Might matter.",
                    "Promotion condition": "Needs another case.",
                },
            },
        )
        candidate = json.loads(
            self.run_cli("learn", "--store", str(store), "--input", str(packet)).stdout
        )["created"][0]
        failed = self.run_cli(
            "promote",
            "--store",
            str(store),
            "--candidate-id",
            candidate,
            "--decision",
            "Do the thing.",
            check=False,
        )
        self.assertEqual(failed.returncode, 2)
        self.assertIn("Promotion gate not met", failed.stderr)

    def test_sensitive_data_is_rejected(self) -> None:
        store = self.init_store()
        packet = self.write_packet(
            "secret.json",
            {
                "kind": "case",
                "title": "Accidental token",
                "sections": {
                    "Problem": "A token was pasted.",
                    "Root cause": "api_key=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
                },
            },
        )
        failed = self.run_cli("learn", "--store", str(store), "--input", str(packet), check=False)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("sensitive data", failed.stderr)

    def test_init_and_clone_refuse_unrelated_repositories(self) -> None:
        non_empty = self.base / "non-empty"
        non_empty.mkdir()
        (non_empty / "unrelated.txt").write_text("data", encoding="utf-8")
        refused = self.run_cli("init", "--store", str(non_empty), check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--adopt-existing", refused.stderr)

        unrelated = self.base / "unrelated-repo"
        unrelated.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(unrelated)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        subprocess.run(["git", "-C", str(unrelated), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(unrelated), "config", "user.email", "test@example.com"], check=True)
        (unrelated / "README.md").write_text("not a memory store", encoding="utf-8")
        subprocess.run(["git", "-C", str(unrelated), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(unrelated), "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        bare = self.base / "unrelated.git"
        subprocess.run(["git", "clone", "--bare", str(unrelated), str(bare)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        clone_dest = self.base / "clone-unrelated"
        clone_refused = self.run_cli(
            "init", "--store", str(clone_dest), "--clone", str(bare), "--branch", "main", check=False
        )
        self.assertEqual(clone_refused.returncode, 2)
        self.assertIn("not a RouteCraft memory store", clone_refused.stderr)

    def test_sync_refuses_unexpected_files_and_option_like_names(self) -> None:
        store = self.base / "safe-store"
        self.run_cli("init", "--store", str(store), "--git-init")
        packet = self.write_packet(
            "safe.json",
            {
                "kind": "case",
                "title": "Safe store",
                "sections": {"Problem": "Need safe sync.", "Root cause": "Unknown files must not be staged."},
            },
        )
        self.run_cli("learn", "--store", str(store), "--input", str(packet))
        (store / "unrelated.txt").write_text("do not commit", encoding="utf-8")
        refused = self.run_cli("sync", "--store", str(store), check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("Unexpected file in memory-store root", refused.stderr)

        invalid_branch = self.run_cli(
            "sync", "--store", str(store), "--branch", "--delete", check=False
        )
        self.assertNotEqual(invalid_branch.returncode, 0)

    def test_configured_store_is_used_and_bundled_write_is_refused(self) -> None:
        refused_packet = self.write_packet(
            "bundled-refused.json",
            {
                "kind": "case",
                "title": "Must not enter bundled store",
                "sections": {"Problem": "Private memory in public plugin.", "Root cause": "No external store configured."},
            },
        )
        refused = self.run_cli("learn", "--input", str(refused_packet), check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("bundled plugin store", refused.stderr)

        store = self.base / "configured"
        self.run_cli("init", "--store", str(store), "--configure")
        packet = self.write_packet(
            "configured.json",
            {
                "kind": "case",
                "title": "Configured store works",
                "tags": ["configuration"],
                "sections": {"Problem": "No explicit store argument.", "Root cause": "The configured store is resolved."},
            },
        )
        created = json.loads(self.run_cli("learn", "--input", str(packet)).stdout)["created"][0]
        result = json.loads(self.run_cli("recall", "--query", "configured store", "--json").stdout)
        self.assertIn(created, [item["id"] for item in result["matches"]])

    def test_japanese_recall(self) -> None:
        store = self.init_store()
        packet = self.write_packet(
            "ja.json",
            {
                "kind": "case",
                "title": "再起動後に学習履歴が消える問題",
                "tags": ["永続化", "iOS"],
                "sections": {
                    "Problem": "アプリを再起動すると学習履歴が消えた。",
                    "Root cause": "一時領域に状態を保存していた。",
                    "Reusable lesson": "OS更新を疑う前に永続ストレージの保存先を確認する。",
                },
            },
        )
        created = json.loads(
            self.run_cli("learn", "--store", str(store), "--input", str(packet)).stdout
        )["created"][0]
        result = json.loads(
            self.run_cli(
                "recall",
                "--store",
                str(store),
                "--query",
                "学習履歴が消える 永続ストレージ",
                "--json",
            ).stdout
        )
        self.assertIn(created, [item["id"] for item in result["matches"]])

    def test_sync_rejects_non_dedicated_parent_repository(self) -> None:
        parent = self.base / "parent"
        parent.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(parent)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        store = parent / "memory"
        self.run_cli("init", "--store", str(store))
        packet = self.write_packet(
            "nested.json",
            {
                "kind": "case",
                "title": "Nested store",
                "sections": {"Problem": "Nested in a product repository.", "Root cause": "No dedicated Git root."},
            },
        )
        self.run_cli("learn", "--store", str(store), "--input", str(packet))
        failed = self.run_cli("sync", "--store", str(store), check=False)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("dedicated Git repository", failed.stderr)

    def test_git_sync_across_two_devices(self) -> None:
        remote = self.base / "memory.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        store1 = self.base / "device1"
        self.run_cli(
            "init",
            "--store",
            str(store1),
            "--git-init",
            "--remote",
            str(remote),
        )
        packet1 = self.write_packet(
            "sync-case.json",
            {
                "kind": "case",
                "title": "Shared cross-device case",
                "tags": ["sync"],
                "sections": {
                    "Problem": "A reusable finding existed on one machine.",
                    "Root cause": "The memory store was previously local only.",
                    "Reusable lesson": "Use a dedicated private Git repository.",
                },
            },
        )
        created = json.loads(
            self.run_cli("learn", "--store", str(store1), "--input", str(packet1)).stdout
        )["created"][0]
        sync1 = json.loads(self.run_cli("sync", "--store", str(store1)).stdout)
        self.assertTrue(sync1["pushed"])

        store2 = self.base / "device2"
        self.run_cli("init", "--store", str(store2), "--clone", str(remote), "--branch", "main")
        recalled = json.loads(
            self.run_cli(
                "recall",
                "--store",
                str(store2),
                "--query",
                "private Git repository",
                "--json",
            ).stdout
        )
        self.assertIn(created, [item["id"] for item in recalled["matches"]])

        packet2 = self.write_packet(
            "sync-candidate.json",
            {
                "kind": "candidate",
                "title": "Cross-device memory should remain repository-isolated",
                "tags": ["sync", "privacy"],
                "evidence": [created],
                "sections": {
                    "Observation": "A dedicated store avoids adding private memory to product repositories.",
                    "Possible decision value": "Keep memory synchronization separate from application source.",
                    "Promotion condition": "Confirm on another project.",
                },
            },
        )
        candidate2 = json.loads(
            self.run_cli("learn", "--store", str(store2), "--input", str(packet2)).stdout
        )["created"][0]
        self.run_cli("sync", "--store", str(store2))
        self.run_cli("sync", "--store", str(store1), "--mode", "pull")
        pulled = json.loads(
            self.run_cli(
                "recall",
                "--store",
                str(store1),
                "--query",
                "repository isolated privacy",
                "--json",
            ).stdout
        )
        self.assertIn(candidate2, [item["id"] for item in pulled["matches"]])

    def test_promotion_requires_captured_case_records(self) -> None:
        store = self.init_store()
        packet = self.write_packet(
            "forged-candidate.json",
            {
                "kind": "candidate",
                "title": "Forged evidence should not promote",
                "observations": 99,
                "evidence": ["external-one", "external-two"],
                "sections": {
                    "Observation": "A pattern was claimed without captured cases.",
                    "Possible decision value": "None until verified.",
                    "Promotion condition": "Capture independent cases.",
                },
            },
        )
        candidate = json.loads(
            self.run_cli("learn", "--store", str(store), "--input", str(packet)).stdout
        )["created"][0]
        failed = self.run_cli(
            "promote",
            "--store",
            str(store),
            "--candidate-id",
            candidate,
            "--decision",
            "Do not promote this.",
            check=False,
        )
        self.assertEqual(failed.returncode, 2)
        self.assertIn("captured Case records", failed.stderr)

    def test_remote_helper_and_conflicting_init_options_are_rejected(self) -> None:
        store = self.base / "remote-helper"
        unsafe_marker = self.base / "routecraft-unsafe"
        helper = self.run_cli(
            "init",
            "--store",
            str(store),
            "--git-init",
            "--remote",
            f"ext::sh -c touch {unsafe_marker}",
            check=False,
        )
        self.assertEqual(helper.returncode, 2)
        self.assertIn("remote-helper syntax", helper.stderr)
        self.assertFalse(unsafe_marker.exists())

        conflict = self.run_cli(
            "init",
            "--store",
            str(self.base / "conflict"),
            "--clone",
            str(self.base / "missing.git"),
            "--git-init",
            check=False,
        )
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("cannot be combined", conflict.stderr)

    def test_sync_rejects_non_markdown_and_symlink_payloads(self) -> None:
        store = self.base / "strict-store"
        self.run_cli("init", "--store", str(store), "--git-init")
        (store / "cases" / "raw.log").write_text("raw log", encoding="utf-8")
        failed = self.run_cli("sync", "--store", str(store), check=False)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("Only Markdown files are allowed", failed.stderr)
        (store / "cases" / "raw.log").unlink()

        target = self.base / "outside.md"
        target.write_text("outside", encoding="utf-8")
        link = store / "cases" / "linked.md"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        failed_link = self.run_cli("sync", "--store", str(store), check=False)
        self.assertEqual(failed_link.returncode, 2)
        self.assertIn("symlink", failed_link.stderr)

    def test_invalid_numeric_and_oversized_packets_fail_cleanly(self) -> None:
        store = self.init_store()
        invalid = self.write_packet(
            "invalid-number.json",
            {
                "kind": "case",
                "title": "Invalid observation count",
                "observations": "many",
                "sections": {"Problem": "Bad input.", "Root cause": "Bad input."},
            },
        )
        failed = self.run_cli("learn", "--store", str(store), "--input", str(invalid), check=False)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("observations must be an integer", failed.stderr)
        self.assertNotIn("Traceback", failed.stderr)

        oversized = self.write_packet(
            "oversized.json",
            {
                "kind": "case",
                "title": "Oversized transcript",
                "body": "x" * 50_001,
            },
        )
        too_large = self.run_cli("learn", "--store", str(store), "--input", str(oversized), check=False)
        self.assertEqual(too_large.returncode, 2)
        self.assertIn("compact decision summary", too_large.stderr)

    def test_failed_nested_candidate_rolls_back_primary_case(self) -> None:
        store = self.init_store()
        packet = self.write_packet(
            "rollback.json",
            {
                "kind": "case",
                "title": "Primary should roll back",
                "sections": {"Problem": "Nested data is invalid.", "Root cause": "Nested secret."},
                "candidate": {
                    "title": "Bad nested candidate",
                    "sections": {
                        "Observation": "api_key=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
                        "Possible decision value": "Must not persist.",
                    },
                },
            },
        )
        failed = self.run_cli("learn", "--store", str(store), "--input", str(packet), check=False)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(list((store / "cases").glob("*.md")), [])
        self.assertEqual(list((store / "candidates").glob("*.md")), [])


if __name__ == "__main__":
    unittest.main()
