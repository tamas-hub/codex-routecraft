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
SAFE_EMAIL = "routecraft-memory@users.noreply.github.com"


class RouteCraftGitPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env["ROUTECRAFT_MEMORY_CONFIG"] = str(self.base / "config.json")
        self.env["ROUTECRAFT_DEVICE_ID"] = "privacy-testbox"

        # Simulate a normal workstation whose global Git identity contains a
        # private email address. The RouteCraft launcher must not inherit it
        # into commits pushed to the private decision store.
        global_config = self.base / "private-gitconfig"
        global_config.write_text(
            "[user]\n\tname = Private User\n\temail = private@example.invalid\n",
            encoding="utf-8",
        )
        self.env["GIT_CONFIG_GLOBAL"] = str(global_config)

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

    def test_private_global_email_is_not_used_for_memory_commit(self) -> None:
        remote = self.base / "memory.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        store = self.base / "store"
        self.run_cli(
            "init",
            "--store",
            str(store),
            "--git-init",
            "--remote",
            str(remote),
        )

        packet = self.base / "case.json"
        packet.write_text(
            json.dumps(
                {
                    "kind": "case",
                    "title": "Privacy-safe Git identity",
                    "sections": {
                        "Problem": "A private global Git email could block the first push.",
                        "Root cause": "Git inherited the workstation identity.",
                        "Verification": "Inspect the author and committer email on the synchronized commit.",
                    },
                }
            ),
            encoding="utf-8",
        )
        self.run_cli("learn", "--store", str(store), "--input", str(packet))
        result = json.loads(self.run_cli("sync", "--store", str(store)).stdout)
        self.assertTrue(result["pushed"])

        author_email = subprocess.run(
            ["git", "-C", str(store), "show", "-s", "--format=%ae", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        committer_email = subprocess.run(
            ["git", "-C", str(store), "show", "-s", "--format=%ce", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

        self.assertEqual(author_email, SAFE_EMAIL)
        self.assertEqual(committer_email, SAFE_EMAIL)
        self.assertNotEqual(author_email, "private@example.invalid")


if __name__ == "__main__":
    unittest.main()
