from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins/codex-routecraft/scripts"))

from routecraft_local.git_tools import inspect_git, rule_based_session_summary
from routecraft_local.packs import build_context_pack, build_handoff_pack, estimate_tokens


class FakeService:
    def __init__(self, repo: str | None = None):
        self.project = {"name": "日本語プロジェクト", "summary": "現在の目的", "repo_path": repo}
        self.memories = [
            {"id": "m-high", "title": "重要な決定", "body": "現行証拠を優先する。", "importance": "high", "verified": True, "memory_type": "decision"},
            {"id": "m-dup", "title": "重要な決定", "body": "現行証拠を優先する。", "importance": "low"},
            {"id": "m-next", "title": "次作業", "body": "移行を確認する。", "memory_type": "next_action"},
            {"id": "m-fail", "title": "既知障害", "body": "同期失敗を再現する。", "memory_type": "failure"},
            {"id": "m-constraint", "title": "制約", "body": "外部送信しない。", "memory_type": "constraint"},
        ]

    def get_project(self, ref): return self.project
    def list_memories(self, ref): return self.memories


class RouteCraftLocalPackTests(unittest.TestCase):
    def test_git_nonrepo_and_summary_are_json_serializable(self):
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_git(directory)
            self.assertFalse(result["is_repository"])
            self.assertIsInstance(rule_based_session_summary(directory), dict)

    def test_git_combines_staged_and_unstaged_and_redacts_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "日本語 staged.txt").write_text("a\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "初回"], cwd=repo, check=True)
            (repo / "日本語 staged.txt").write_text("a\nb\n", encoding="utf-8")
            (repo / "未追跡.txt").write_text("c\n", encoding="utf-8")
            subprocess.run(["git", "remote", "add", "origin", "https://user:password@example.com/repo.git?token=secret#fragment"], cwd=repo, check=True)
            result = inspect_git(repo)
            self.assertEqual(result["diff"]["additions"], 1)
            self.assertIn("日本語 staged.txt", result["changed_files"])
            self.assertIn("未追跡.txt", result["new_files"])
            self.assertNotIn("password", result["remote_url"])
            self.assertNotIn("token", result["remote_url"])
            self.assertNotIn("author", result["recent_commits"][0])

    def test_git_rename_reports_target_and_previous_path_in_porcelain_order(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Path(directory)
            subprocess.run(["git","init","-q"],cwd=repo,check=True)
            subprocess.run(["git","config","user.email","test@example.invalid"],cwd=repo,check=True)
            subprocess.run(["git","config","user.name","Test"],cwd=repo,check=True)
            (repo/"old-name.txt").write_text("content\n",encoding="utf-8")
            subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","commit","-qm","initial"],cwd=repo,check=True)
            subprocess.run(["git","mv","old-name.txt","new-name.txt"],cwd=repo,check=True)
            result=inspect_git(repo); renamed=next(item for item in result["working_tree"] if item["status"].startswith("R"))
            self.assertEqual("new-name.txt",renamed["path"]); self.assertEqual("old-name.txt",renamed["previous_path"])
            self.assertIn("new-name.txt",result["changed_files"]); self.assertNotIn("old-name.txt",result["changed_files"])

    def test_context_formats_caps_and_deduplicates(self):
        service = FakeService()
        self.assertGreater(estimate_tokens("日本語 context"), 0)
        for fmt in ("markdown", "text", "json"):
            result = build_context_pack(service, "p", format=fmt, max_chars=500)
            self.assertLessEqual(result["char_count"], 500)
            self.assertEqual(result["included_memory_ids"][0], "m-high")
            if fmt == "json": json.loads(result["content"])
        with self.assertRaises(ValueError): build_context_pack(service, "p", max_chars=0)
        with self.assertRaises(ValueError): build_context_pack(service, "p", max_tokens=0)
        token_limited = build_context_pack(service, "p", max_tokens=80)
        self.assertLessEqual(token_limited["estimated_tokens"], 80)

    def test_handoff_has_exact_files_and_zip(self):
        service = FakeService()
        private_paths=("D:\\Clients\\Acme\\secret.txt","/srv/customer/private.txt","//server/share/private.txt","\\Users\\alice\\secret.txt","file:///Users/alice/secret.txt","file://server/share/secret.txt")
        service.memories[2]["body"]="Review "+", ".join(private_paths)+" and retain https://example.test/public/path"
        with tempfile.TemporaryDirectory() as directory:
            result = build_handoff_pack(service, "p", Path(directory) / "handoff.zip", as_zip=True)
            expected = {"HANDOFF.md", "PROJECT_STATE.json", "CHANGED_FILES.txt", "NEXT_TASKS.md", "KNOWN_ISSUES.md", "IMPORTANT_DECISIONS.md"}
            self.assertEqual(set(result["files"]), expected)
            with zipfile.ZipFile(result["zip"]) as archive:
                self.assertEqual(set(archive.namelist()), expected)
                self.assertNotIn(str(Path(directory)), archive.read("PROJECT_STATE.json").decode("utf-8"))
            self.assertIn("次作業", (Path(result["folder"]) / "NEXT_TASKS.md").read_text(encoding="utf-8"))
            self.assertIn("既知障害", (Path(result["folder"]) / "KNOWN_ISSUES.md").read_text(encoding="utf-8"))
            decisions = (Path(result["folder"]) / "IMPORTANT_DECISIONS.md").read_text(encoding="utf-8")
            self.assertIn("重要な決定", decisions)
            self.assertIn("制約", decisions)
            combined="\n".join((Path(result["folder"])/name).read_text(encoding="utf-8") for name in expected)
            for private_path in private_paths: self.assertNotIn(private_path,combined)
            self.assertIn("https://example.test/public/path",combined)
            with self.assertRaises(ValueError):
                build_handoff_pack(service, "p", Path(directory) / "handoff.zip", as_zip=True)


if __name__ == "__main__": unittest.main()
