from __future__ import annotations

import codecs
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_memory.py"


class RouteCraftUtf8StdinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env["ROUTECRAFT_MEMORY_CONFIG"] = str(self.base / "config.json")
        self.env["ROUTECRAFT_DEVICE_ID"] = "stdinbox"

    def run_cli(
        self,
        *args: str,
        input_bytes: bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or self.env,
            check=False,
        )

    def init_store(self, name: str) -> Path:
        store = self.base / name
        proc = self.run_cli("init", "--store", str(store))
        self.assertEqual(
            proc.returncode,
            0,
            proc.stderr.decode("utf-8", errors="replace"),
        )
        return store

    def test_japanese_utf8_packet_survives_legacy_redirected_stdin_encoding(self) -> None:
        legacy_env = self.env.copy()
        legacy_env["PYTHONIOENCODING"] = "cp1252"
        legacy_env["PYTHONUTF8"] = "0"

        for name, prefix in (("plain", b""), ("bom", codecs.BOM_UTF8)):
            with self.subTest(input=name):
                store = self.init_store(name)
                packet = {
                    "kind": "case",
                    "title": f"日本語stdin確認-{name}",
                    "tags": ["windows", "utf-8", "日本語"],
                    "sections": {
                        "Problem": "Windowsのリダイレクト入力で日本語JSONが壊れる。",
                        "Root cause": "Pythonがstdinへレガシーコードページを継承していた。",
                        "Verification": "UTF-8の標準入力から保存し、同じ日本語で検索する。",
                        "Reusable lesson": "JSON stdinは端末コードページに依存せずUTF-8として読む。",
                    },
                }
                payload = prefix + json.dumps(packet, ensure_ascii=False).encode("utf-8")
                learned = self.run_cli(
                    "learn",
                    "--store",
                    str(store),
                    "--input",
                    "-",
                    input_bytes=payload,
                    env=legacy_env,
                )
                stderr = learned.stderr.decode("utf-8", errors="replace")
                self.assertEqual(learned.returncode, 0, stderr)
                result = json.loads(learned.stdout.decode("utf-8-sig"))
                record_id = result["created"][0]
                record_path = Path(result["primary_path"])
                saved = record_path.read_text(encoding="utf-8")
                self.assertIn(packet["title"], saved)
                self.assertIn("レガシーコードページ", saved)

                recalled = self.run_cli(
                    "recall",
                    "--store",
                    str(store),
                    "--query",
                    "日本語 stdin レガシーコードページ",
                    "--json",
                    env=legacy_env,
                )
                recall_stderr = recalled.stderr.decode("utf-8", errors="replace")
                self.assertEqual(recalled.returncode, 0, recall_stderr)
                recall_result = json.loads(recalled.stdout.decode("utf-8-sig"))
                self.assertIn(record_id, [item["id"] for item in recall_result["matches"]])


if __name__ == "__main__":
    unittest.main()
