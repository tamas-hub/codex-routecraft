from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_observatory.py"

SPEC = importlib.util.spec_from_file_location("routecraft_observatory", SCRIPT)
assert SPEC and SPEC.loader
OBS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OBS
SPEC.loader.exec_module(OBS)


class RouteCraftObservatoryTests(unittest.TestCase):
    def test_git_state_reports_clean_and_divergence_without_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            bare = Path(tmp) / "remote.git"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
            (repo / "README.md").write_text("ok\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "clone", "--bare", str(repo), str(bare)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            state = OBS.git_state(repo)
            self.assertTrue(state["clean"])
            self.assertTrue(state["in_sync"])
            self.assertEqual(state["ahead"], 0)
            self.assertEqual(state["behind"], 0)
            rendered = repr(state)
            self.assertNotIn(str(repo), rendered)
            self.assertNotIn(str(bare), rendered)

            (repo / "README.md").write_text("changed\n", encoding="utf-8")
            dirty = OBS.git_state(repo)
            self.assertFalse(dirty["clean"])
            self.assertFalse(dirty["in_sync"])


if __name__ == "__main__":
    unittest.main()
