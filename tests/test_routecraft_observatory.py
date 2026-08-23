from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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

    def delivery_args(self, token_file: Path) -> SimpleNamespace:
        return SimpleNamespace(
            endpoint="https://heartbeat.example.invalid/api",
            token_file=str(token_file),
            telemetry_endpoint="https://telemetry.example.invalid/api",
            telemetry_token_file=str(token_file),
            telemetry_script="routecraft_telemetry.py",
            telemetry_sites_bypass_token_file=None,
            telemetry_since_days=30,
            telemetry_include_legacy=False,
        )

    def test_heartbeat_failure_does_not_block_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token.txt"
            token_file.write_text("a" * 64, encoding="utf-8")
            error = urllib.error.HTTPError(
                "https://heartbeat.example.invalid/api",
                400,
                "Bad Request",
                None,
                io.BytesIO(b'{"ok":false,"error":"invalid evaluation.schema_version"}'),
            )
            with mock.patch.object(OBS, "send", side_effect=error) as heartbeat, mock.patch.object(
                OBS, "send_telemetry", return_value={"ok": True, "accepted": 3}
            ) as telemetry:
                result = OBS.deliver(self.delivery_args(token_file), {"schema_version": 2})

            heartbeat.assert_called_once()
            telemetry.assert_called_once()
            self.assertFalse(result["ok"])
            self.assertEqual(result["heartbeat"]["code"], "http_error")
            self.assertEqual(result["heartbeat"]["http_status"], 400)
            self.assertEqual(result["heartbeat"]["detail"], "invalid evaluation.schema_version")
            self.assertTrue(result["telemetry"]["ok"])

    def test_unreadable_http_error_body_does_not_block_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token.txt"
            token_file.write_text("a" * 64, encoding="utf-8")
            broken_body = mock.MagicMock()
            broken_body.read.side_effect = http.client.IncompleteRead(b"partial")
            error = urllib.error.HTTPError(
                "https://heartbeat.example.invalid/api",
                502,
                "Bad Gateway",
                None,
                broken_body,
            )
            with mock.patch.object(OBS, "send", side_effect=error), mock.patch.object(
                OBS, "send_telemetry", return_value={"ok": True, "accepted": 1}
            ) as telemetry:
                result = OBS.deliver(self.delivery_args(token_file), {"schema_version": 2})

            telemetry.assert_called_once()
            self.assertFalse(result["ok"])
            self.assertEqual(result["heartbeat"]["code"], "http_error")
            self.assertEqual(result["heartbeat"]["http_status"], 502)
            self.assertNotIn("detail", result["heartbeat"])
            self.assertTrue(result["telemetry"]["ok"])

    def test_token_like_http_error_detail_is_not_persistable_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token.txt"
            token_file.write_text("a" * 64, encoding="utf-8")
            error = urllib.error.HTTPError(
                "https://heartbeat.example.invalid/api",
                403,
                "Forbidden",
                None,
                io.BytesIO(b'{"ok":false,"error":"token privatevalue"}'),
            )
            with mock.patch.object(OBS, "send", side_effect=error), mock.patch.object(
                OBS, "send_telemetry", return_value={"ok": True, "accepted": 1}
            ):
                result = OBS.deliver(self.delivery_args(token_file), {"schema_version": 2})

            rendered = json.dumps(result)
            self.assertNotIn("privatevalue", rendered)
            self.assertNotIn("detail", result["heartbeat"])

    def test_telemetry_failure_preserves_heartbeat_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token.txt"
            token_file.write_text("a" * 64, encoding="utf-8")
            with mock.patch.object(OBS, "send", return_value={"ok": True, "received_at": "now"}) as heartbeat, mock.patch.object(
                OBS,
                "send_telemetry",
                side_effect=OBS.DeliveryError("collector_error", "telemetry collector exited with code 1"),
            ) as telemetry:
                result = OBS.deliver(self.delivery_args(token_file), {"schema_version": 2})

            heartbeat.assert_called_once()
            telemetry.assert_called_once()
            self.assertFalse(result["ok"])
            self.assertTrue(result["heartbeat"]["ok"])
            self.assertEqual(result["telemetry"]["code"], "collector_error")

    def test_both_destinations_are_attempted_when_both_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token.txt"
            token_file.write_text("a" * 64, encoding="utf-8")
            with mock.patch.object(OBS, "send", side_effect=TimeoutError) as heartbeat, mock.patch.object(
                OBS, "send_telemetry", side_effect=RuntimeError("private raw error")
            ) as telemetry:
                result = OBS.deliver(self.delivery_args(token_file), {"schema_version": 2})

            heartbeat.assert_called_once()
            telemetry.assert_called_once()
            self.assertEqual(result["heartbeat"]["code"], "network_error")
            self.assertEqual(result["telemetry"]["code"], "unexpected_error")
            self.assertNotIn("private raw error", json.dumps(result))

    def test_main_prints_structured_result_and_returns_nonzero_for_partial_failure(self) -> None:
        partial = {
            "ok": False,
            "heartbeat": {"ok": False, "error": "heartbeat upload failed", "code": "http_error", "http_status": 400},
            "telemetry": {"ok": True, "accepted": 2},
        }
        output = io.StringIO()
        with mock.patch.object(OBS, "build_payload", return_value={"schema_version": 2}), mock.patch.object(
            OBS, "deliver", return_value=partial
        ), contextlib.redirect_stdout(output):
            exit_code = OBS.main(["--endpoint", "https://heartbeat.example.invalid/api"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue()), partial)


if __name__ == "__main__":
    unittest.main()
