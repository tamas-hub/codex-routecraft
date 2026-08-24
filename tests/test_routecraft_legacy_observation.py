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

import routecraft_collector as COLLECTOR
import routecraft_legacy_observation as OBSERVATION


class LegacyObservationTests(unittest.TestCase):
    def _facts(self, observed_at: str, *, health: str = "healthy", missing: int | None = 0, duplicate: int | None = 0) -> dict[str, object]:
        return {
            "schema_version": 1,
            "device_id": "test-device-identity",
            "observed_at": observed_at,
            "components": [
                {
                    "component_kind": "ai_usage_updater",
                    "status": "disabled",
                    "replacement_kind": "unified_usage_adapter",
                    "enabled": False,
                    "running": False,
                    "replacement_health": health,
                    "missing_snapshots": missing,
                    "duplicate_ingestions": duplicate,
                    "last_error_at": None,
                },
                {
                    "component_kind": "codex_meter_startup",
                    "status": "active",
                    "replacement_kind": "none",
                    "enabled": True,
                    "running": True,
                    "replacement_health": "unknown",
                    "missing_snapshots": None,
                    "duplicate_ingestions": None,
                    "last_error_at": None,
                },
            ],
        }

    def test_three_healthy_cycles_are_eligible_without_auto_superseding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            facts = root / "facts.json"
            ledger = root / "ledger.json"
            output = root / "legacy-d1-summary.json"
            for index in range(3):
                facts.write_text(json.dumps(self._facts(f"2026-08-24T00:00:0{index}Z")), encoding="utf-8")
                result = OBSERVATION.observe(facts, ledger, output)
            rows = json.loads(output.read_text(encoding="utf-8"))
            row = next(item for item in rows if item["component_kind"] == "ai_usage_updater")
            self.assertEqual(OBSERVATION.ROW_KEYS, set(row))
            self.assertEqual(3, row["observation_cycles"])
            self.assertEqual(3, row["consecutive_healthy_cycles"])
            self.assertEqual(0, row["missing_snapshots"])
            self.assertEqual(0, row["duplicate_ingestions"])
            self.assertEqual("high", row["confidence"])
            self.assertEqual("disabled", row["status"])
            summary = next(item for item in result["summary"] if item["component_kind"] == "ai_usage_updater")
            self.assertTrue(summary["supersede_eligible"])
            self.assertFalse(summary["archive_eligible"])
            self.assertEqual(3, result["ledger_cycles"])

    def test_missing_and_duplicate_evidence_blocks_eligibility_and_is_safe_to_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            facts = root / "facts.json"
            ledger = root / "ledger.json"
            output = root / "rows.json"
            first = self._facts("2026-08-24T00:00:00Z")
            facts.write_text(json.dumps(first), encoding="utf-8")
            OBSERVATION.observe(facts, ledger, output)
            # Retrying the exact cycle is recorded as duplicate ingestion; it
            # is never silently treated as a healthy migration observation.
            OBSERVATION.observe(facts, ledger, output)
            rows = json.loads(output.read_text(encoding="utf-8"))
            row = next(item for item in rows if item["component_kind"] == "ai_usage_updater")
            self.assertEqual(1, row["duplicate_ingestions"])
            self.assertEqual(0, row["consecutive_healthy_cycles"])
            self.assertFalse(next(item for item in OBSERVATION.summarize(ledger)["summary"] if item["component_kind"] == "ai_usage_updater")["supersede_eligible"])

    def test_unknown_facts_remain_nullable_low_confidence_and_status_is_not_derived(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            facts = root / "facts.json"
            ledger = root / "ledger.json"
            output = root / "rows.json"
            facts.write_text(json.dumps(self._facts("2026-08-24T00:00:00Z", health="unknown", missing=None, duplicate=None)), encoding="utf-8")
            OBSERVATION.observe(facts, ledger, output)
            row = next(item for item in json.loads(output.read_text(encoding="utf-8")) if item["component_kind"] == "ai_usage_updater")
            self.assertIsNone(row["missing_snapshots"])
            self.assertIsNone(row["duplicate_ingestions"])
            self.assertEqual("low", row["confidence"])
            self.assertEqual("disabled", row["status"])

    def test_privacy_boundary_rejects_raw_fields_and_device_is_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            facts = root / "facts.json"
            ledger = root / "ledger.json"
            output = root / "rows.json"
            unsafe = self._facts("2026-08-24T00:00:00Z")
            unsafe["source"] = "C:/private/source"
            facts.write_text(json.dumps(unsafe), encoding="utf-8")
            with self.assertRaises(OBSERVATION.ObservationError):
                OBSERVATION.observe(facts, ledger, output)
            self.assertFalse(ledger.exists())
            self.assertFalse(output.exists())

            safe = self._facts("2026-08-24T00:00:01Z")
            facts.write_text(json.dumps(safe), encoding="utf-8")
            OBSERVATION.observe(facts, ledger, output)
            rows = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(all(len(row["device_id"]) == 32 and set(row["device_id"]) <= set("0123456789abcdef") for row in rows))
            self.assertNotIn("test-device-identity", json.dumps(rows))

    def test_collector_accepts_exact_legacy_rows_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            facts = root / "facts.json"
            ledger = root / "ledger.json"
            output = root / "rows.json"
            facts.write_text(json.dumps(self._facts("2026-08-24T00:00:00Z")), encoding="utf-8")
            OBSERVATION.observe(facts, ledger, output)
            rows = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(rows)
            self.assertTrue(all(COLLECTOR._valid_family("legacy_components", row) for row in rows))
            payload = COLLECTOR.fixture_payload_v4()
            payload["legacy_components"] = rows
            self.assertTrue(COLLECTOR.validate_v4(payload))

    def test_cli_observe_and_summarize_only_write_caller_selected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            facts = root / "facts.json"
            ledger = root / "ledger.json"
            output = root / "rows.json"
            summary = root / "summary.json"
            facts.write_text(json.dumps(self._facts("2026-08-24T00:00:00Z")), encoding="utf-8")
            script = SCRIPTS / "routecraft_legacy_observation.py"
            observed = subprocess.run(
                [sys.executable, str(script), "observe", "--facts", str(facts), "--ledger", str(ledger), "--output", str(output)],
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, observed.returncode, observed.stderr)
            summarized = subprocess.run(
                [sys.executable, str(script), "summarize", "--ledger", str(ledger), "--output", str(summary)],
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, summarized.returncode, summarized.stderr)
            self.assertEqual(1, json.loads(summary.read_text(encoding="utf-8"))["ledger_cycles"])


if __name__ == "__main__":
    unittest.main()
