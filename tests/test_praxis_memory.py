from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))

from praxis_memory import ConflictError, IntegrityError, PraxisMemory, PraxisMemoryError  # noqa: E402
from routecraft_local.service import RouteCraftService  # noqa: E402
from routecraft_protocols import new_event  # noqa: E402


class PraxisMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.target = self.base / "praxis"
        self.memory = PraxisMemory(self.target)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def local_source(self) -> Path:
        service = RouteCraftService(self.base / "routecraft-local")
        project = service.add_project("Migrated project")
        service.add_memory(
            project["id"], "failure", "Local persistence case", "Restart test passed",
            importance="high", tags=["sqlite", "persistence"], source="cli",
            source_ref="local-case", verified=True,
            legacy_metadata={"confidence": 0.8, "status": "verified"},
        )
        return service.db.path

    def decision_source(self) -> Path:
        base = self.base / "decision"
        (base / "cases").mkdir(parents=True)
        (base / ".routecraft-store.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        (base / "cases" / "CASE-1.md").write_text(
            "---\n"
            "schema_version: 1\n"
            "id: CASE-20260827T000000Z-TEST-ABCD\n"
            "kind: case\n"
            "title: Legacy decision case\n"
            "status: fixed\n"
            "confidence: 0.75\n"
            "observations: 1\n"
            "tags: [\"legacy\", \"migration\"]\n"
            "scope: []\n"
            "evidence: []\n"
            "---\n\n"
            "A Markdown Decision Store case.\n",
            encoding="utf-8",
        )
        return base

    def test_case_failure_tags_recall_and_special_event_isolation(self) -> None:
        case = self.memory.add_record("case", "SQLite recovery", "Use backups before migration", tags=["sqlite", "backup"], confidence=0.8)
        failure = self.memory.add_record(
            "failure", "Migration failed", "Recover the target safely", tags=["migration"],
            failure={"trigger": "schema mismatch", "action": "stopped", "result": "unchanged", "root_cause": "old schema", "mitigation": "validate first", "avoid_next_time": "dry run"},
        )
        self.memory.add_record("event", "Benchmark only", "Do not use as production evidence", event_classification="benchmark_event")
        recalled = self.memory.recall("SQLite backup", tags=["sqlite"])
        self.assertEqual(case["id"], recalled[0]["id"])
        self.assertEqual(0.8, recalled[0]["score_components"]["confidence"])
        self.assertIsNone(recalled[0]["score_components"]["project_similarity"])
        self.assertEqual("dry run", failure["failure"]["avoid_next_time"])
        self.assertNotIn("Benchmark only", [item["title"] for item in self.memory.recall("evidence")])
        self.assertEqual(1, len(self.memory.recall("evidence", include_special_events=True)))

    def test_protocol_events_are_deduplicated_and_paged_without_special_by_default(self) -> None:
        normal = new_event("memory.stored", "praxis", event_id="evt-normal", timestamp="2026-01-01T00:00:00Z", metadata={})
        special = new_event("memory.stored", "praxis", event_id="evt-special", timestamp="2026-01-02T00:00:00Z", event_classification="benchmark_event", metadata={})
        self.assertTrue(self.memory.store_event(normal)["stored"])
        self.assertTrue(self.memory.store_event(special)["stored"])
        self.assertTrue(self.memory.store_event(normal)["duplicate"])
        changed = dict(normal)
        changed["metadata"] = {"changed": True}
        with self.assertRaises(ConflictError):
            self.memory.store_event(changed)
        self.assertEqual(2, self.memory.status()["events"])
        listed = self.memory.list_events(include_special_events=False)
        self.assertEqual(["evt-normal"], [item["payload"]["event_id"] for item in listed["items"]])

    def test_local_dry_run_apply_backup_duplicate_and_conflict(self) -> None:
        source = self.local_source()
        source_before = source.read_bytes()
        dry = self.memory.migrate_from_routecraft_local(source)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(source_before, source.read_bytes())
        self.assertFalse((self.target / "praxis-memory.sqlite3").exists())
        self.memory.add_record("fact", "Existing", "Target has existing data")
        applied = self.memory.migrate_from_routecraft_local(source, apply=True, confirmation="MIGRATE")
        self.assertEqual(1, applied["created"])
        self.assertEqual(source_before, source.read_bytes())
        self.assertTrue(applied["backup"])
        self.assertTrue((self.target / applied["backup"]).is_file())
        imported = self.memory.recall("Local persistence")[0]
        self.assertEqual("Migrated project", imported["project"])
        self.assertEqual({}, imported["failure"])
        self.assertIn("importance:high", imported["tags"])
        replay = self.memory.migrate_from_routecraft_local(source, apply=True, confirmation="MIGRATE")
        self.assertEqual(1, replay["skipped"])
        preview = self.memory.migrate_from_routecraft_local(source)
        self.assertEqual(1, preview["skipped"])
        self.assertEqual(0, preview["created"])
        self.assertEqual(applied["after"], preview["before"])
        self.assertEqual(preview["before"], preview["after"])
        import sqlite3
        db = sqlite3.connect(source)
        try:
            db.execute("UPDATE memories SET title='Changed Local persistence case'")
            db.commit()
        finally:
            db.close()
        conflict = self.memory.migrate_from_routecraft_local(source, apply=True, confirmation="MIGRATE")
        self.assertEqual(1, conflict["conflict"])
        self.assertEqual(1, self.memory.status()["import_conflicts"])

    def test_decision_store_apply_and_corrupt_input_leave_target_unchanged(self) -> None:
        decision = self.decision_source()
        result = self.memory.migrate_from_decision_store(decision, apply=True, confirmation="MIGRATE")
        self.assertEqual(1, result["created"])
        self.assertEqual("legacy", self.memory.recall("Markdown")[0]["project"])
        before = self.memory.status()["records"]
        corrupt = self.base / "corrupt.sqlite3"
        corrupt.write_bytes(b"not sqlite")
        with self.assertRaises(IntegrityError):
            self.memory.migrate_from_routecraft_local(corrupt, apply=True, confirmation="MIGRATE")
        self.assertEqual(before, self.memory.status()["records"])

    def test_decision_store_candidate_stays_non_authoritative_and_validated_rule_is_verified(self) -> None:
        decision = self.decision_source()
        (decision / "candidates").mkdir()
        (decision / "rules").mkdir()
        (decision / "candidates" / "CAND-20260827T000001Z-TEST-ABCD.md").write_text(
            "---\n"
            "schema_version: 1\n"
            "id: CAND-20260827T000001Z-TEST-ABCD\n"
            "kind: candidate\n"
            "title: Unproven route\n"
            "status: proposed\n"
            "confidence: 0.2\n"
            "observations: 1\n"
            "tags: [\"route\"]\n"
            "scope: []\n"
            "evidence: []\n"
            "---\n\nCandidate only.\n",
            encoding="utf-8",
        )
        (decision / "rules" / "RULE-20260827T000002Z-TEST-ABCD.md").write_text(
            "---\n"
            "schema_version: 1\n"
            "id: RULE-20260827T000002Z-TEST-ABCD\n"
            "kind: rule\n"
            "title: Validated safety rule\n"
            "status: validated\n"
            "confidence: 0.9\n"
            "observations: 2\n"
            "tags: [\"safety\"]\n"
            "scope: []\n"
            "evidence: []\n"
            "---\n\nAlways preview first.\n",
            encoding="utf-8",
        )
        preview = self.memory.migrate_from_decision_store(decision)
        self.assertEqual(3, preview["created"])
        self.assertFalse((self.target / "praxis-memory.sqlite3").exists())
        self.memory.migrate_from_decision_store(decision, apply=True, confirmation="MIGRATE")
        candidate = self.memory.recall("Candidate")[0]
        rule = self.memory.recall("preview")[0]
        self.assertEqual("fact", candidate["record_type"])
        self.assertIn("decision_store_kind:candidate", candidate["tags"])
        self.assertFalse(candidate["verified"])
        self.assertEqual("policy", rule["record_type"])
        self.assertTrue(rule["verified"])

    def test_invalid_decision_store_record_fails_closed_before_dry_run_or_apply_writes(self) -> None:
        decision = self.decision_source()
        case = decision / "cases" / "CASE-1.md"
        case.write_text(case.read_text(encoding="utf-8").replace("kind: case", "kind: rule"), encoding="utf-8")
        with self.assertRaises(IntegrityError):
            self.memory.migrate_from_decision_store(decision)
        self.assertFalse(self.target.exists())
        with self.assertRaises(IntegrityError):
            self.memory.migrate_from_decision_store(decision, apply=True, confirmation="MIGRATE")
        self.assertFalse(self.target.exists())

    def test_secret_rejection_and_confirmation(self) -> None:
        with self.assertRaises(PraxisMemoryError):
            self.memory.add_record("fact", "Token", "api_key=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890")
        with self.assertRaises(PraxisMemoryError):
            self.memory.migrate_from_routecraft_local(self.local_source(), apply=True, confirmation="migrate")

    def test_target_refuses_decision_store_directory(self) -> None:
        decision = self.decision_source()
        with self.assertRaises(IntegrityError):
            PraxisMemory(decision)

    def test_existing_unknown_database_is_not_rewritten_as_schema_v1(self) -> None:
        self.target.mkdir(parents=True)
        path = self.target / "praxis-memory.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE unrelated(value TEXT)")
            db.execute("INSERT INTO unrelated VALUES('keep')")
            db.commit()
        finally:
            db.close()
        before = path.read_bytes()
        with self.assertRaises(IntegrityError):
            self.memory.initialize()
        self.assertEqual(before, path.read_bytes())

    def test_remember_core_adapter_and_status_version(self) -> None:
        item = self.memory.remember({
            "task": "Verify a local migration", "strategy": "validate before apply",
            "result": "passed", "project": "sample", "tags": ["migration"],
            "reuse_count": 2, "success_rate": 1.0, "reliability": 0.9,
        })
        self.assertEqual("experience", item["record_type"])
        self.assertEqual(2, item["experience"]["reuse_count"])
        status = self.memory.status()
        self.assertEqual(1, status["schema_version"])
        self.assertEqual(1, status["records"]["experience"])
        self.assertEqual(item["id"], self.memory.recall({"task": "local migration", "project": "sample"})[0]["id"])
        self.assertEqual(1.0, self.memory.recall({"task": "local migration", "project": "sample"})[0]["score_components"]["project_similarity"])
        self.assertIsNone(self.memory.notify_experience({"task_id": "opaque", "status": "succeeded"}))


if __name__ == "__main__":
    unittest.main()
