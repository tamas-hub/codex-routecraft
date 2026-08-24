from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import sqlite3
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))
from routecraft_local.errors import ConfirmationRequiredError, ConflictError, IntegrityError, NotFoundError, RouteCraftLocalError
from routecraft_local import loop_bridge as LOCAL_BRIDGE
from routecraft_local.service import RouteCraftService

class LocalDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.service = RouteCraftService(self.base / "data")
        win_path="C:"+"/Users/test"; self.service.initialize(); self.project = self.service.add_project("日本語プロジェクト", repo_path=win_path+"/private", git_remote_url="https://user:password@example.test/repo.git?token=x#frag", ai_agents=("agent",), languages=("Python",), tags=("app",))
    def test_crud_delete_and_filters(self):
        item = self.service.add_memory(self.project["id"], "lesson", "秘密 " + "sk-" + "abcdefghijklmnopqrstuvwxyz" + " を隠す", "電話 090-1234-5678", importance="high", tags=("日本語",), verified=True)
        self.assertIn("[REDACTED:openai_key]", item["title"]); self.assertTrue(item["warnings"])
        fine_grained="github_"+"pat_"+"abcdefghijklmnopqrstuvwxyz0123456789"
        protected=self.service.add_memory(self.project["id"],"security",fine_grained,"fine-grained token",active=False)
        self.assertNotIn(fine_grained,protected["title"]); self.assertIn("github_fine_grained_token",protected["warnings"])
        found = self.service.search_memories(self.project["id"], query="秘密", tags=("日本語",), importance=("high",))
        self.assertEqual(found[0]["id"], item["id"]); self.assertIn("relevance", found[0])
        updated = self.service.update_memory(item["id"], active=False); self.assertFalse(updated["active"])
        self.assertEqual([], self.service.list_memories(self.project["id"]))
        with self.assertRaises(ConfirmationRequiredError): self.service.delete_memory(item["id"], "no")
        self.assertEqual(item["id"], self.service.delete_memory(item["id"], item["id"])["deleted"])
        if self.service.doctor()["fts5"]:
            with self.service.db.connect() as db: self.assertIsNone(db.execute("SELECT 1 FROM memories_fts WHERE memory_id=?",(item["id"],)).fetchone())
    def test_search_uses_fts_with_partial_match_fallback(self):
        fts_item=self.service.add_memory(self.project["id"],"note","ordinary title","ordinary body")
        partial=self.service.add_memory(self.project["id"],"note","prefix-middle-suffix","partial fallback")
        if not self.service.doctor()["fts5"]: self.skipTest("SQLite FTS5 is unavailable")
        with self.service.db.connect() as db:
            db.execute("DELETE FROM memories_fts WHERE memory_id=?",(fts_item["id"],))
            db.execute("INSERT INTO memories_fts(memory_id,title,body,tags) VALUES(?,?,?,?)",(fts_item["id"],"fts-only-marker","","[]"))
        self.assertEqual(fts_item["id"],self.service.search_memories(self.project["id"],"fts-only")[0]["id"])
        self.assertEqual(partial["id"],self.service.search_memories(self.project["id"],"middle")[0]["id"])
    def test_project_redaction_and_plural_list_filters(self):
        self.assertEqual("https://example.test/repo.git", self.project["git_remote_url"])
        self.assertIn("C:"+"/Users/test", self.project["repo_path"])
        self.project=self.service.update_project(self.project["id"], git_remote_url="https://token:secret@example.test/x?key=y", ai_agents=("SERVICE_TOKEN=masked",), languages=("ja",), tags=("x",))
        self.assertEqual("https://example.test/x", self.project["git_remote_url"])
        self.assertIn("[REDACTED:env_credential]", self.project["ai_agents"][0])
        ordinary=self.service.add_project("ordinary", tags=("MODE=development",))
        self.assertEqual("MODE=development", ordinary["tags"][0])
        high=self.service.add_memory(self.project["id"], "decision", "a", "a", importance="high")
        low=self.service.add_memory(self.project["id"], "note", "b", "b", importance="low")
        selected=self.service.list_memories(self.project["id"], types=("decision",), importance=("high", "medium"))
        self.assertEqual([high["id"]], [item["id"] for item in selected])
        self.assertEqual([low["id"]], [item["id"] for item in self.service.list_memories(self.project["id"], importance="low")])
    def test_import_export_legacy_conflict_backup_restore(self):
        legacy = self.base / "legacy"; (legacy / "cases").mkdir(parents=True); (legacy / "candidates").mkdir(); (legacy / "rules").mkdir()
        (legacy / ".routecraft-store.json").write_text('{"schema_version":1}', encoding="utf-8")
        (legacy / "rules" / "rule.md").write_text('---\nid: "RULE-20260823T000000Z-TEST-0001"\nkind: "rule"\ntitle: "legacy rule"\ntags: ["legacy"]\n---\n\nDecision text', encoding="utf-8")
        result = self.service.import_routecraft_store(self.project["id"], legacy); self.assertEqual(1, len(result["created"]))
        again = self.service.import_routecraft_store(self.project["id"], legacy); self.assertEqual(["RULE-20260823T000000Z-TEST-0001"], again["skipped"])
        (legacy / "rules" / "rule.md").write_text('---\nid: "RULE-20260823T000000Z-TEST-0001"\nkind: "rule"\ntitle: "legacy rule"\ntags: ["legacy"]\n---\n\nChanged text', encoding="utf-8")
        changed=self.service.import_routecraft_store(self.project["id"], legacy); self.assertEqual(1, len(changed["conflicts"]))
        self.service.add_memory(self.project["id"], "file_reference", "portable", "portable", related_files=("C:"+"/Users/test/private/file.txt",), related_commits=("/"+"home/test/private",))
        exported = self.service.export_memories(self.project["id"], "jsonl", self.base / "out.jsonl", safe=True); self.assertEqual(2, exported["count"])
        package = self.service.export_project_package(self.project["id"], self.base / "project.zip")
        self.assertTrue(Path(package["output"]).is_file())
        with zipfile.ZipFile(package["output"]) as archive:
            portable=json.loads(archive.read("project.json"))
        self.assertEqual("<REPO_PATH>", portable["project"]["repo_path"])
        self.assertEqual("https://example.test/repo.git", portable["project"]["git_remote_url"])
        self.assertNotIn("C:"+"/Users", json.dumps(portable,ensure_ascii=False))
        self.assertEqual(["<PATH>"], [m["related_files"][0] for m in portable["memories"] if m["title"]=="portable"])
        conflict = self.service.import_project_package(package["output"]); self.assertFalse(conflict["imported"])
        other=RouteCraftService(self.base / "other-data"); imported=other.import_project_package(package["output"])
        self.assertTrue(imported["imported"]); self.assertEqual(self.project["id"], imported["project_id"])
        imported_memory=next(m for m in other.list_memories(imported["project_id"],include_inactive=True) if m["source_ref"].startswith("RULE-")); self.assertEqual("routecraft-store", imported_memory["source"])
        self.assertFalse(other.import_project_package(package["output"])["imported"])
        backup = self.service.backup(self.base / "backup.zip"); self.service.add_memory(self.project["id"], "note", "later", "later")
        restored = self.service.restore(backup["output"], "RESTORE"); self.assertTrue(Path(restored["pre_restore_backup"]).is_file())
        self.assertEqual(2, len(self.service.list_memories(self.project["id"], include_inactive=True)))
    def test_routecraft_store_case_and_candidate_mapping_uses_explicit_metadata(self):
        legacy=self.base/"mapping-store"; (legacy/"rules").mkdir(parents=True); (legacy/"cases").mkdir(); (legacy/"candidates").mkdir()
        (legacy/".routecraft-store.json").write_text('{"schema_version":1}',encoding="utf-8")
        (legacy/"cases"/"fixed.md").write_text('---\nid: "CASE-fixed"\nkind: "case"\ntitle: "fixed case"\noutcome: "fixed"\n---\n\n## Failed approaches\n\nAn attempted workaround failed.\n\n## Fix\n\nVerified repair.',encoding="utf-8")
        (legacy/"cases"/"failed.md").write_text('---\nid: "CASE-failed"\nkind: "case"\ntitle: "failed case"\noutcome: "failed"\n---\n\n## Problem\n\nNo safe resolution was found.',encoding="utf-8")
        (legacy/"candidates"/"candidate.md").write_text('---\nid: "CAND-observation"\nkind: "candidate"\ntitle: "candidate"\n---\n\nPossible reusable observation.',encoding="utf-8")
        result=self.service.import_routecraft_store(self.project["id"],legacy); self.assertEqual(3,len(result["created"]))
        imported={item["source_ref"]:item for item in self.service.list_memories(self.project["id"],include_inactive=True)}
        self.assertEqual("lesson",imported["CASE-fixed"]["memory_type"]); self.assertTrue(imported["CASE-fixed"]["verified"])
        self.assertEqual("failure",imported["CASE-failed"]["memory_type"]); self.assertTrue(imported["CASE-failed"]["verified"])
        self.assertEqual("note",imported["CAND-observation"]["memory_type"]); self.assertFalse(imported["CAND-observation"]["verified"])
    def test_secret_legacy_source_refs_are_distinct_idempotent_and_portable(self):
        first_secret="sk-"+"aaaaaaaaaaaaaaaaaaaaaaaa"; second_secret="sk-"+"bbbbbbbbbbbbbbbbbbbbbbbb"
        legacy=self.base/"secret-source-store"; (legacy/"rules").mkdir(parents=True); (legacy/"cases").mkdir(); (legacy/"candidates").mkdir()
        (legacy/".routecraft-store.json").write_text('{"schema_version":1}',encoding="utf-8")
        for name,ident in (("first",first_secret),("second",second_secret)):
            (legacy/"cases"/f"{name}.md").write_text(f'---\nid: "{ident}"\nkind: "case"\ntitle: "{name}"\noutcome: "fixed"\n---\n\nVerified lesson.',encoding="utf-8")
        first=self.service.import_routecraft_store(self.project["id"],legacy); self.assertEqual(2,len(first["created"]))
        refs={item["source_ref"] for item in self.service.list_memories(self.project["id"],include_inactive=True)}
        self.assertEqual(2,len(refs)); self.assertTrue(all(ref.startswith("SRC-") for ref in refs))
        self.assertIsNotNone(self.service.find_memory_by_source_ref(self.project["id"],first_secret))
        repeated=self.service.import_routecraft_store(self.project["id"],legacy); self.assertEqual(refs,set(repeated["skipped"])); self.assertEqual([],repeated["created"])
        output=self.service.export_memories(self.project["id"],"jsonl",self.base/"secret-source-safe.jsonl",safe=True)
        raw=Path(output["output"]).read_text(encoding="utf-8"); self.assertNotIn(first_secret,raw); self.assertNotIn(second_secret,raw)
        exported=[json.loads(line) for line in raw.splitlines()]; self.assertEqual(2,len({item["source_ref"] for item in exported}))
        other=RouteCraftService(self.base/"secret-source-jsonl"); target=other.add_project("jsonl target")
        self.assertEqual(2,len(other.import_file(target["id"],output["output"])["created"])); self.assertEqual(2,len(other.import_file(target["id"],output["output"])["skipped"]))
        portable=self.service.export_project_package(self.project["id"],self.base/"secret-source.zip")
        with zipfile.ZipFile(portable["output"]) as archive: package_raw=archive.read("project.json").decode("utf-8")
        self.assertNotIn(first_secret,package_raw); self.assertNotIn(second_secret,package_raw)
        packaged=RouteCraftService(self.base/"secret-source-package"); self.assertTrue(packaged.import_project_package(portable["output"])["imported"])
        third_secret="sk-"+"cccccccccccccccccccccccc"; manual=self.service.add_memory(self.project["id"],"note","manual source","manual source")
        updated=self.service.update_memory(manual["id"],source_ref=third_secret)
        self.assertTrue(updated["source_ref"].startswith("SRC-")); self.assertEqual(manual["id"],self.service.find_memory_by_source_ref(self.project["id"],third_secret)["id"])
    def test_project_package_rejects_path_like_project_id(self):
        package=self.base/"unsafe-project.json"
        package.write_text(json.dumps({"schema_version":1,"project":{"id":"..\\..\\..\\escape","name":"unsafe"},"memories":[]}),encoding="utf-8")
        with self.assertRaisesRegex(IntegrityError,"invalid project ID"): self.service.import_project_package(package)
        self.assertFalse(any(project["name"]=="unsafe" for project in self.service.list_projects(True)))
    def test_secret_bearing_memory_ids_are_rejected_and_legacy_ids_are_rekeyed_for_safe_exports(self):
        secret_id="sk-"+"abcdefghijklmnopqrstuvwxyz"
        fine_grained_id="github_"+"pat_"+"abcdefghijklmnopqrstuvwxyz0123456789"
        for unsafe_id in (secret_id,fine_grained_id):
            with self.assertRaisesRegex(RouteCraftLocalError,"non-secret identifier"):
                self.service.add_memory(self.project["id"],"note","rejected","rejected",memory_id=unsafe_id)
        package=self.base/"secret-id-package.json"
        package.write_text(json.dumps({"schema_version":1,"project":{"id":"PRJ-secret-id-test","name":"secret id"},"memories":[{"id":secret_id,"memory_type":"note","importance":"medium","title":"unsafe","body":"unsafe"}]}),encoding="utf-8")
        with self.assertRaisesRegex(IntegrityError,"invalid project package memory ID"): self.service.import_project_package(package)
        legacy=self.service.add_memory(self.project["id"],"note","legacy identifier","safe body")
        with self.service.db.connect() as db: db.execute("UPDATE memories SET id=? WHERE id=?",(secret_id,legacy["id"]))
        safe_jsonl=self.service.export_memories(self.project["id"],"jsonl",self.base/"safe-identifiers.jsonl",safe=True)
        raw=Path(safe_jsonl["output"]).read_text(encoding="utf-8"); exported=json.loads(raw)
        self.assertNotIn(secret_id,raw); self.assertNotEqual(secret_id,exported["id"]); self.assertIn("openai_key",safe_jsonl["warnings"])
        portable=self.service.export_project_package(self.project["id"],self.base/"safe-identifiers.zip")
        with zipfile.ZipFile(portable["output"]) as archive:
            project_raw=archive.read("project.json").decode("utf-8"); project_payload=json.loads(project_raw)
        self.assertNotIn(secret_id,project_raw); self.assertNotEqual(secret_id,project_payload["memories"][0]["id"])
        other=RouteCraftService(self.base/"safe-identifier-import"); self.assertTrue(other.import_project_package(portable["output"])["imported"])
        metadata_key="github_"+"pat_"+"metadataabcdefghijklmnopqrstuvwxyz"
        metadata_item=self.service.add_memory(self.project["id"],"note","metadata key","metadata key",legacy_metadata={metadata_key:{"safe":"value"}})
        metadata_raw=json.dumps(metadata_item["legacy_metadata"],ensure_ascii=False)
        self.assertNotIn(metadata_key,metadata_raw); self.assertTrue(next(iter(metadata_item["legacy_metadata"])).startswith("KEY-")); self.assertIn("github_fine_grained_token",metadata_item["warnings"])
    def test_safe_exports_replace_unc_and_extended_windows_paths(self):
        unc="\\\\"+"server\\share\\private.txt"; extended="\\\\?\\"+"C:\\Users\\test\\secret.txt"
        item=self.service.add_memory(self.project["id"],"file_reference","portable Windows paths","portable Windows paths",tags=(unc,),related_files=(extended,),related_commits=(unc,),source_ref=unc)
        output=self.service.export_memories(self.project["id"],"jsonl",self.base/"windows-paths.jsonl",safe=True)
        exported=json.loads(Path(output["output"]).read_text(encoding="utf-8")); self.assertEqual(item["id"],exported["id"])
        self.assertEqual(["<PATH>"],exported["tags"]); self.assertEqual(["<PATH>"],exported["related_files"]); self.assertEqual(["<PATH>"],exported["related_commits"]); self.assertTrue(exported["source_ref"].startswith("SRC-"))
        portable=self.service.export_project_package(self.project["id"],self.base/"windows-paths.zip")
        with zipfile.ZipFile(portable["output"]) as archive: packaged=json.loads(archive.read("project.json"))
        memory=next(record for record in packaged["memories"] if record["id"]==item["id"])
        self.assertEqual(["<PATH>"],memory["tags"]); self.assertEqual(["<PATH>"],memory["related_files"]); self.assertEqual(["<PATH>"],memory["related_commits"]); self.assertTrue(memory["source_ref"].startswith("SRC-"))
    def test_safe_exports_replace_embedded_absolute_paths_without_redacting_urls(self):
        windows="D:"+"\\Clients\\Acme\\secret.txt"; posix="/srv/customer/private.txt"; unc="//server/share/private.txt"
        root_relative="\\Users\\alice\\secret.txt"; file_posix="file:///Users/alice/secret.txt"; file_unc="file://server/share/secret.txt"
        self.service.update_project(self.project["id"],description=f"workspace at {windows}; docs https://example.test/public/path",git_remote_url=file_unc)
        item=self.service.add_memory(
            self.project["id"],"file_reference",f"review {windows}",f"compare {posix}, '{unc}', {root_relative}, {file_posix}, and {file_unc} with https://example.test/public/path",
            tags=(f"artifact={unc}",),related_files=(f"input:{windows}",),related_commits=(f"source {posix}",),source_ref=f"source:{windows}",
        )
        output=self.service.export_memories(self.project["id"],"jsonl",self.base/"embedded-paths.jsonl",safe=True)
        raw=Path(output["output"]).read_text(encoding="utf-8"); exported=json.loads(raw)
        for private_path in (windows,posix,unc,root_relative,file_posix,file_unc): self.assertNotIn(private_path,raw)
        self.assertIn("https://example.test/public/path",raw); self.assertIn("<PATH>",exported["title"]); self.assertIn("<PATH>",exported["body"])
        self.assertIn("<PATH>",exported["tags"][0]); self.assertIn("<PATH>",exported["related_files"][0]); self.assertIn("<PATH>",exported["related_commits"][0]); self.assertTrue(exported["source_ref"].startswith("SRC-"))
        portable=self.service.export_project_package(self.project["id"],self.base/"embedded-paths.zip")
        with zipfile.ZipFile(portable["output"]) as archive: packaged_raw=archive.read("project.json").decode("utf-8"); packaged=json.loads(packaged_raw)
        for private_path in (windows,posix,unc,root_relative,file_posix,file_unc): self.assertNotIn(private_path,packaged_raw)
        self.assertIn("https://example.test/public/path",packaged_raw); self.assertIn("<PATH>",packaged["project"]["description"])
        self.assertEqual("<PATH>",packaged["project"]["git_remote_url"])
        packaged_memory=next(record for record in packaged["memories"] if record["id"]==item["id"])
        self.assertIn("<PATH>",packaged_memory["title"]); self.assertIn("<PATH>",packaged_memory["body"])
    def test_boolean_fields_reject_string_coercion_in_imports_edits_and_filters(self):
        with self.assertRaisesRegex(RouteCraftLocalError,"active must be a boolean"):
            self.service.add_memory(self.project["id"],"note","invalid add","invalid add",active="false")
        item=self.service.add_memory(self.project["id"],"note","valid","valid")
        with self.assertRaisesRegex(RouteCraftLocalError,"verified must be a boolean"): self.service.update_memory(item["id"],verified="false")
        self.assertFalse(self.service.get_memory(item["id"])["verified"])
        with self.assertRaisesRegex(RouteCraftLocalError,"archived must be a boolean"): self.service.update_project(self.project["id"],archived="false")
        self.assertFalse(self.service.get_project(self.project["id"])["archived"])
        invalid=self.base/"string-boolean.jsonl"
        invalid.write_text(json.dumps({"id":"MEM-string-boolean","title":"invalid","body":"invalid","memory_type":"note","active":"false","verified":"false"})+"\n",encoding="utf-8")
        with self.assertRaisesRegex(RouteCraftLocalError,"line 1"): self.service.import_file(self.project["id"],invalid)
        self.assertIsNone(next((memory for memory in self.service.list_memories(self.project["id"],include_inactive=True) if memory["id"]=="MEM-string-boolean"),None))
        with self.assertRaisesRegex(RouteCraftLocalError,"verified must be a boolean"): self.service.list_memories(self.project["id"],verified="false")
        with self.assertRaisesRegex(RouteCraftLocalError,"active must be a boolean"): self.service.search_memories(self.project["id"],active="false")
        inactive=self.service.add_memory(self.project["id"],"note","inactive any search","inactive any search",active=False)
        self.assertEqual([],self.service.search_memories(self.project["id"],"inactive any search"))
        self.assertEqual([inactive["id"]],[item["id"] for item in self.service.search_memories(self.project["id"],"inactive any search",active=None)])
    def test_project_package_requires_its_own_schema_version_without_writes(self):
        baseline_projects=[project["id"] for project in self.service.list_projects(True)]
        baseline_memories=[memory["id"] for memory in self.service.list_memories(include_inactive=True,limit=1_000_000)]
        raw=self.base/"missing-payload-schema.json"
        raw.write_text(json.dumps({"project":{"id":"PRJ-no-schema","name":"no schema"},"memories":[]}),encoding="utf-8")
        with self.assertRaisesRegex(IntegrityError,"invalid project package"):
            self.service.import_project_package(raw)
        zipped=self.base/"future-payload-schema.zip"
        with zipfile.ZipFile(zipped,"w") as archive:
            archive.writestr("manifest.json",json.dumps({"schema_version":1,"kind":"routecraft-project-package","project_id":"PRJ-future-schema"}))
            archive.writestr("project.json",json.dumps({"schema_version":2,"project":{"id":"PRJ-future-schema","name":"future schema"},"memories":[]}))
        with self.assertRaisesRegex(IntegrityError,"invalid project package"):
            self.service.import_project_package(zipped)
        self.assertEqual(baseline_projects,[project["id"] for project in self.service.list_projects(True)])
        self.assertEqual(baseline_memories,[memory["id"] for memory in self.service.list_memories(include_inactive=True,limit=1_000_000)])
    def test_project_package_preserves_archive_state_and_chronology(self):
        memory=self.service.add_memory(self.project["id"],"decision","chronology","preserve timestamps",verified=True)
        project_created="2024-01-02T03:04:05Z"; project_updated="2024-02-03T04:05:06Z"
        memory_created="2024-03-04T05:06:07Z"; memory_updated="2024-04-05T06:07:08Z"
        with self.service.db.connect() as db:
            db.execute("UPDATE projects SET archived=1,created_at=?,updated_at=? WHERE id=?",(project_created,project_updated,self.project["id"]))
            db.execute("UPDATE memories SET active=0,verified=1,created_at=?,updated_at=? WHERE id=?",(memory_created,memory_updated,memory["id"]))
        package=self.service.export_project_package(self.project["id"],self.base/"chronology.zip")
        other=RouteCraftService(self.base/"chronology-import"); result=other.import_project_package(package["output"]); self.assertTrue(result["imported"])
        restored_project=other.get_project(self.project["id"]); restored_memory=other.get_memory(memory["id"])
        self.assertTrue(restored_project["archived"]); self.assertEqual((project_created,project_updated),(restored_project["created_at"],restored_project["updated_at"]))
        self.assertFalse(restored_memory["active"]); self.assertTrue(restored_memory["verified"]); self.assertEqual((memory_created,memory_updated),(restored_memory["created_at"],restored_memory["updated_at"]))
        invalid=self.base/"invalid-chronology.json"
        invalid.write_text(json.dumps({"schema_version":1,"project":{"id":"PRJ-invalid-chronology","name":"invalid","archived":"yes","created_at":"not-a-date"},"memories":[]}),encoding="utf-8")
        with self.assertRaisesRegex(IntegrityError,"invalid project package project fields"): other.import_project_package(invalid)
    def test_decision_store_ancestor_is_rejected_by_core_and_loop_config(self):
        store=self.base/"decision-store"; store.mkdir(); (store/".routecraft-store.json").write_text('{"schema_version":1}',encoding="utf-8")
        for target in (store,store/"nested"/"local-memory"):
            with self.assertRaisesRegex(IntegrityError,"must not reuse"):
                RouteCraftService(target)
            with self.assertRaisesRegex(RouteCraftLocalError,"must not reuse"):
                LOCAL_BRIDGE._validate_config({"schema_version":1,"enabled":True,"data_dir":str(target)})
    def test_file_encoding_exclusion_and_performance(self):
        source = self.base / "input.jsonl"; source.write_bytes(b'\xef\xbb\xbf'+json.dumps({"title":"CRLF", "body":"line1\r\nline2", "memory_type":"note"}, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(1, len(self.service.import_file(self.project["id"], source)["created"]))
        env = self.base / ".env"; env.write_text("TOKEN=unsafe", encoding="utf-8")
        with self.assertRaises(Exception): self.service.import_file(self.project["id"], env)
        start=time.monotonic()
        for n in range(1000): self.service.add_memory(self.project["id"], "note", f"記録 {n}", "検索可能な本文")
        self.assertLess(time.monotonic()-start, 15)
        start=time.monotonic(); self.assertTrue(self.service.search_memories(self.project["id"], "検索可能", limit=10)); self.assertLess(time.monotonic()-start, 3)
        start=time.monotonic(); self.assertEqual(1000, len(self.service.list_memories(self.project["id"], limit=1000))); self.assertLess(time.monotonic()-start, 3)
    def test_safe_jsonl_round_trip_is_idempotent_and_invalid_batch_rolls_back(self):
        item=self.service.add_memory(self.project["id"],"note","token "+"sk-"+"abcdefghijklmnopqrstuvwxyz","body",tags=("C:"+"/Users/test/path",),related_files=("/"+"home/test/file",),related_commits=("C:"+"/Users/test/commit",),source_ref="C:"+"/Users/test/ref")
        output=self.base/"safe.jsonl"; self.service.export_memories(self.project["id"],"jsonl",output,safe=True)
        raw=output.read_text(encoding="utf-8"); self.assertNotIn("C:"+"/Users",raw); self.assertNotIn("/"+"home/test",raw); self.assertNotIn("sk-"+"abcdefghijklmnopqrstuvwxyz",raw)
        other=RouteCraftService(self.base/"roundtrip"); target=other.add_project("target")
        first=other.import_file(target["id"],output); self.assertEqual([item["id"]],first["created"])
        second=other.import_file(target["id"],output); self.assertEqual([item["id"]],second["skipped"])
        bad=self.base/"partial.jsonl"; bad.write_text(json.dumps({"id":"MEM-valid","title":"ok","body":"ok","memory_type":"note"})+"\n"+json.dumps({"id":123,"title":"bad","body":"bad","memory_type":"note"}),encoding="utf-8")
        with self.assertRaises(RouteCraftLocalError): other.import_file(target["id"],bad)
        self.assertIsNone(next((x for x in other.list_memories(target["id"],include_inactive=True) if x["id"]=="MEM-valid"),None))
        late=self.base/"late-validation.jsonl"; late.write_text(json.dumps({"id":"MEM-first","title":"first","body":"first","memory_type":"note"})+"\n"+json.dumps({"id":"MEM-too-long","title":"x"*501,"body":"late","memory_type":"note"}),encoding="utf-8")
        with self.assertRaisesRegex(RouteCraftLocalError,"line 2"): other.import_file(target["id"],late)
        self.assertIsNone(next((x for x in other.list_memories(target["id"],include_inactive=True) if x["id"]=="MEM-first"),None))
        runtime=self.base/"runtime-failure.jsonl"; runtime.write_text(json.dumps({"id":"MEM-runtime-1","title":"one","body":"one","memory_type":"note"})+"\n"+json.dumps({"id":"MEM-runtime-2","title":"two","body":"two","memory_type":"note"}),encoding="utf-8")
        original=other._insert_memory
        def fail_second(db,item):
            if item["id"]=="MEM-runtime-2": raise RouteCraftLocalError("simulated late database failure")
            return original(db,item)
        with mock.patch.object(other,"_insert_memory",side_effect=fail_second):
            with self.assertRaisesRegex(RouteCraftLocalError,"simulated late database failure"): other.import_file(target["id"],runtime)
        ids={x["id"] for x in other.list_memories(target["id"],include_inactive=True)}
        self.assertNotIn("MEM-runtime-1",ids); self.assertNotIn("MEM-runtime-2",ids)
    def test_project_package_batch_is_atomic_for_new_and_existing_projects(self):
        def package(path, project, memories):
            path.write_text(json.dumps({"schema_version":1,"project":project,"memories":memories}),encoding="utf-8")
        incoming={"id":"PRJ-atomic-new","name":"atomic new"}
        first={"id":"MEM-atomic-first","memory_type":"note","importance":"medium","title":"first","body":"first"}
        second={"id":"MEM-atomic-second","memory_type":"note","importance":"medium","title":"second","body":"second"}
        new_package=self.base/"atomic-new.json"; package(new_package,incoming,[first,second])
        original=self.service._insert_memory
        def fail_second(db,item):
            if item["id"]=="MEM-atomic-second": raise RouteCraftLocalError("simulated package write failure")
            return original(db,item)
        with mock.patch.object(self.service,"_insert_memory",side_effect=fail_second):
            with self.assertRaisesRegex(RouteCraftLocalError,"simulated package write failure"):
                self.service.import_project_package(new_package)
        self.assertIsNone(next((p for p in self.service.list_projects(True) if p["id"]==incoming["id"]),None))
        self.assertEqual([],self.service.list_memories(include_inactive=True,limit=1_000_000))

        existing_package=self.base/"atomic-existing.json"
        package(existing_package,{"id":self.project["id"],"name":self.project["name"]},[first,second])
        with mock.patch.object(self.service,"_insert_memory",side_effect=fail_second):
            with self.assertRaisesRegex(RouteCraftLocalError,"simulated package write failure"):
                self.service.import_project_package(existing_package,conflict="skip")
        self.assertEqual([],self.service.list_memories(self.project["id"],include_inactive=True))
        with self.service.db.connect() as db: self.assertEqual(0,db.execute("SELECT COUNT(*) FROM import_conflicts").fetchone()[0])
        invalid_package=self.base/"atomic-invalid.json"
        package(invalid_package,{"id":"PRJ-invalid-package","name":"invalid package"},[first,{**second,"title":"x"*501}])
        with self.assertRaisesRegex(IntegrityError,"memory 2"):
            self.service.import_project_package(invalid_package)
        self.assertIsNone(next((p for p in self.service.list_projects(True) if p["id"]=="PRJ-invalid-package"),None))
    def test_project_package_conflict_rows_roll_back_with_late_failure(self):
        other=self.service.add_project("other package project")
        self.service.add_memory(other["id"],"note","durable","durable",memory_id="MEM-package-conflict")
        path=self.base/"atomic-conflict.json"
        path.write_text(json.dumps({
            "schema_version":1,
            "project":{"id":"PRJ-atomic-conflict","name":"atomic conflict"},
            "memories":[
                {"id":"MEM-package-created","memory_type":"note","importance":"medium","title":"created","body":"created"},
                {"id":"MEM-package-conflict","memory_type":"note","importance":"medium","title":"different","body":"different"},
                {"id":"MEM-package-fail","memory_type":"note","importance":"medium","title":"fail","body":"fail"},
            ],
        }),encoding="utf-8")
        original=self.service._insert_memory
        def fail_late(db,item):
            if item["id"]=="MEM-package-fail": raise RouteCraftLocalError("late package failure")
            return original(db,item)
        with mock.patch.object(self.service,"_insert_memory",side_effect=fail_late):
            with self.assertRaisesRegex(RouteCraftLocalError,"late package failure"):
                self.service.import_project_package(path)
        self.assertIsNone(next((p for p in self.service.list_projects(True) if p["id"]=="PRJ-atomic-conflict"),None))
        with self.service.db.connect() as db: self.assertEqual(0,db.execute("SELECT COUNT(*) FROM import_conflicts").fetchone()[0])
    def test_project_package_merge_uses_source_reference_identity(self):
        existing=self.service.add_memory(self.project["id"],"lesson","same source","same body",source_ref="package-source-ref")
        package=self.base/"source-ref-merge.json"
        package.write_text(json.dumps({"schema_version":1,"project":{"id":self.project["id"],"name":self.project["name"]},"memories":[
            {"id":"MEM-same-source-same-content","memory_type":"lesson","importance":"medium","title":"same source","body":"same body","source_ref":"package-source-ref"},
            {"id":"MEM-same-source-different-content","memory_type":"lesson","importance":"medium","title":"same source","body":"different body","source_ref":"package-source-ref"}
        ]}),encoding="utf-8")
        result=self.service.import_project_package(package,conflict="skip")
        self.assertEqual([],result["created"]); self.assertEqual([existing["id"]],result["skipped"]); self.assertEqual(["MEM-same-source-different-content"],result["conflicts"])
        ids={memory["id"] for memory in self.service.list_memories(self.project["id"],include_inactive=True)}
        self.assertNotIn("MEM-same-source-same-content",ids); self.assertNotIn("MEM-same-source-different-content",ids)
    def test_loop_session_summary_source_reference_is_serialized_and_idempotent(self):
        source_ref="routecraft-loop:shared-key"
        barrier=threading.Barrier(2); results=[]; errors=[]
        def add_summary():
            try:
                service=RouteCraftService(self.service.data_dir)
                barrier.wait(timeout=5)
                results.append(service.add_loop_session_summary(self.project["id"],"summary","changed files",source_ref=source_ref))
            except Exception as exc:
                errors.append(exc)
        first=threading.Thread(target=add_summary); second=threading.Thread(target=add_summary)
        first.start(); second.start(); first.join(10); second.join(10)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual([],errors)
        self.assertEqual(2,len(results)); self.assertEqual(results[0]["id"],results[1]["id"])
        replay=self.service.add_loop_session_summary(self.project["id"],"summary","changed files",source_ref=source_ref)
        self.assertEqual(results[0]["id"],replay["id"])
        summaries=[m for m in self.service.list_memories(self.project["id"],memory_type="session_summary") if m["source_ref"]==source_ref]
        self.assertEqual(1,len(summaries))
    def test_loop_session_summary_adopts_oldest_legacy_duplicate_without_mutation(self):
        source_ref="routecraft-loop:legacy-duplicate"
        oldest=self.service.add_memory(self.project["id"],"session_summary","old summary","old body",source="routecraft-loop",source_ref=source_ref)
        newer=self.service.add_memory(self.project["id"],"session_summary","new summary","new body",source="routecraft-loop",source_ref=source_ref)
        with self.service.db.connect() as db:
            db.execute("UPDATE memories SET created_at=?,updated_at=? WHERE id=?",("2020-01-01T00:00:00Z","2020-01-01T00:00:00Z",oldest["id"]))
            db.execute("UPDATE memories SET created_at=?,updated_at=? WHERE id=?",("2021-01-01T00:00:00Z","2021-01-01T00:00:00Z",newer["id"]))
        before=[(item["id"],item["title"],item["body"],item["created_at"],item["updated_at"]) for item in self.service.list_memories(self.project["id"],memory_type="session_summary")]
        adopted=self.service.add_loop_session_summary(self.project["id"],"ignored new summary","ignored new body",source_ref=source_ref)
        replay=self.service.add_loop_session_summary(self.project["id"],"ignored new summary","ignored new body",source_ref=source_ref)
        after=[(item["id"],item["title"],item["body"],item["created_at"],item["updated_at"]) for item in self.service.list_memories(self.project["id"],memory_type="session_summary")]
        self.assertEqual(oldest["id"],adopted["id"]); self.assertEqual(oldest["id"],replay["id"])
        self.assertEqual(before,after); self.assertEqual({oldest["id"],newer["id"]},{item[0] for item in after})
    def test_settings_and_no_overwrite_targets(self):
        self.assertFalse(self.service.get_settings()["telemetry_enabled"])
        with self.assertRaises(RouteCraftLocalError): self.service.update_settings({"telemetry_enabled":"false"})
        with self.assertRaises(RouteCraftLocalError): self.service.update_settings({"excluded_globs":"*.env"})
        changed=self.service.update_settings({"language":"ja","telemetry_enabled":False,"excluded_globs":["*.private"]}); self.assertFalse(changed["telemetry_enabled"])
        target=self.base/"exists.jsonl"; target.write_text("x",encoding="utf-8")
        with self.assertRaises(ConflictError): self.service.export_memories(self.project["id"],"jsonl",target)
        with self.assertRaises(RouteCraftLocalError): self.service.update_project(self.project["id"],name=" ")
        with self.assertRaises(RouteCraftLocalError): self.service.add_memory(self.project["id"],"note","long source","body",source="x"*501)
    def test_malformed_restore_fails(self):
        broken=self.base / "bad.zip"
        with zipfile.ZipFile(broken,"w") as z: z.writestr("../escape", "x")
        with self.assertRaises(IntegrityError): self.service.restore(broken,"RESTORE")
        item=self.service.add_memory(self.project["id"],"note","kept","kept")
        impostor_db=self.base/"impostor.sqlite3"
        db=sqlite3.connect(impostor_db)
        try: db.execute("PRAGMA user_version=1"); db.commit()
        finally: db.close()
        digest=__import__('hashlib').sha256(impostor_db.read_bytes()).hexdigest()
        impostor=self.base/"impostor.zip"
        manifest={"schema_version":1,"kind":"routecraft-local-backup","database":"routecraft-local.sqlite3","sha256":digest}
        with zipfile.ZipFile(impostor,"w") as z:
            z.writestr("manifest.json",json.dumps(manifest)); z.write(impostor_db,"routecraft-local.sqlite3")
        with self.assertRaises(IntegrityError): self.service.restore(impostor,"RESTORE")
        self.assertEqual(item["id"],self.service.get_memory(item["id"])["id"])
        self.assertEqual([],list(self.service.data_dir.glob("routecraft-backup-*.zip")))
        duplicate_service=RouteCraftService(self.base/"duplicate-restore-data"); duplicate=duplicate_service.add_project("duplicate active name")
        with duplicate_service.db.connect() as db:
            db.execute("DROP INDEX projects_active_name")
            db.execute("INSERT INTO projects SELECT ?,name,repo_path,git_remote_url,ai_agents,languages,tags,description,current_objective,archived,created_at,updated_at FROM projects WHERE id=?",("PRJ-duplicate-active-name",duplicate["id"]))
        duplicate_archive=self.base/"duplicate-active-name.zip"; duplicate_database=duplicate_service.db.path
        duplicate_digest=__import__('hashlib').sha256(duplicate_database.read_bytes()).hexdigest()
        duplicate_manifest={"schema_version":1,"kind":"routecraft-local-backup","created_at":"2026-08-24T00:00:00Z","database":"routecraft-local.sqlite3","sha256":duplicate_digest}
        with zipfile.ZipFile(duplicate_archive,"w",zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json",json.dumps(duplicate_manifest)); archive.write(duplicate_database,"routecraft-local.sqlite3")
        live_digest=__import__('hashlib').sha256(self.service.db.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(IntegrityError,"initialization preflight"): self.service.restore(duplicate_archive,"RESTORE")
        self.assertEqual(live_digest,__import__('hashlib').sha256(self.service.db.path.read_bytes()).hexdigest()); self.assertEqual(item["id"],self.service.get_memory(item["id"])["id"])
        malformed=self.base / "malformed.jsonl"; malformed.write_text('{"title":"safe"}\n{bad=secret}',encoding="utf-8")
        with self.assertRaisesRegex(RouteCraftLocalError, "line 2"): self.service.import_file(self.project["id"],malformed)
    def test_restore_failure_reports_retained_candidate_when_cleanup_fails(self):
        broken=self.base/"retained-candidate.zip"
        with zipfile.ZipFile(broken,"w") as archive: archive.writestr("unexpected.txt","invalid")
        real_unlink=Path.unlink
        def fail_candidate_cleanup(path,*args,**kwargs):
            if path.name.startswith(".routecraft-restore-"): raise OSError("injected candidate cleanup failure")
            return real_unlink(path,*args,**kwargs)
        with mock.patch.object(Path,"unlink",fail_candidate_cleanup):
            with self.assertRaisesRegex(IntegrityError,"temporary restore candidate retained at"): self.service.restore(broken,"RESTORE")
        self.assertTrue(list(self.service.data_dir.glob(".routecraft-restore-*.sqlite3")))
    def test_restore_serializes_writers_and_preserves_pre_restore_state(self):
        archive=self.service.backup(self.base/"restore-source.zip")
        protected=self.service.add_memory(self.project["id"],"note","pre-restore protected","must be in safety backup")
        real_replace=__import__('os').replace; writers=[]
        writer_code=(
            "import sys;"
            f"sys.path.insert(0,{str(ROOT/'plugins'/'codex-routecraft'/'scripts')!r});"
            "from routecraft_local.service import RouteCraftService;"
            f"item=RouteCraftService({str(self.service.data_dir)!r}).add_memory({self.project['id']!r},'note','concurrent writer','must run after restore');"
            "print(item['id'])"
        )
        def replace_with_waiting_writer(source,target):
            if writers:
                real_replace(source,target); return
            writer=subprocess.Popen([sys.executable,"-X","utf8","-c",writer_code],text=True,encoding="utf-8",stdout=subprocess.PIPE,stderr=subprocess.PIPE); writers.append(writer)
            time.sleep(0.2); self.assertIsNone(writer.poll())
            real_replace(source,target)
        with mock.patch("routecraft_local.service.os.replace",side_effect=replace_with_waiting_writer):
            restored=self.service.restore(archive["output"],"RESTORE")
        writer=writers.pop(); stdout,stderr=writer.communicate(timeout=5); self.assertEqual(0,writer.returncode,stderr); written_id=stdout.strip()
        if writer.stdout: writer.stdout.close()
        if writer.stderr: writer.stderr.close()
        del writer; __import__('gc').collect(); time.sleep(0.1)
        self.assertEqual(written_id,self.service.get_memory(written_id)["id"])
        with zipfile.ZipFile(restored["pre_restore_backup"]) as backup_zip:
            database=self.base/"pre-restore.sqlite3"; database.write_bytes(backup_zip.read("routecraft-local.sqlite3"))
        connection=sqlite3.connect(database)
        try: self.assertEqual(protected["id"],connection.execute("SELECT id FROM memories WHERE id=?",(protected["id"],)).fetchone()[0])
        finally: connection.close()
    def test_restore_rolls_back_if_post_replace_activation_fails(self):
        archive=self.service.backup(self.base/"activation-source.zip")
        protected=self.service.add_memory(self.project["id"],"note","activation rollback","must survive failed activation")
        real_initialize=self.service.initialize; injected={"done":False}
        def fail_once_after_replace():
            if not self.service._ready and not injected["done"]:
                injected["done"]=True; raise RuntimeError("injected activation failure")
            return real_initialize()
        with mock.patch.object(self.service,"initialize",side_effect=fail_once_after_replace):
            with self.assertRaisesRegex(IntegrityError,"previous database was retained"): self.service.restore(archive["output"],"RESTORE")
        self.assertTrue(injected["done"])
        self.assertEqual(protected["id"],self.service.get_memory(protected["id"])["id"])
    def test_restore_retains_recovery_artifacts_if_automatic_rollback_fails(self):
        archive=self.service.backup(self.base/"rollback-failure-source.zip")
        protected=self.service.add_memory(self.project["id"],"note","rollback recovery artifact","must remain recoverable")
        real_initialize=self.service.initialize; real_replace=__import__('os').replace; injected={"activation":False,"replaces":0}
        def fail_activation_once():
            if not self.service._ready and not injected["activation"]:
                injected["activation"]=True; raise RuntimeError("injected activation failure")
            return real_initialize()
        def fail_rollback_replace(source,target):
            injected["replaces"]+=1
            if injected["replaces"]==2: raise OSError("injected rollback replace failure")
            return real_replace(source,target)
        with mock.patch.object(self.service,"initialize",side_effect=fail_activation_once), mock.patch("routecraft_local.service.os.replace",side_effect=fail_rollback_replace):
            with self.assertRaisesRegex(IntegrityError,"recovery backup retained"): self.service.restore(archive["output"],"RESTORE")
        self.assertEqual(2,injected["replaces"]); backups=list(self.service.data_dir.glob("routecraft-backup-*.zip")); raw_rollbacks=list(self.service.data_dir.glob(".routecraft-rollback-*.sqlite3"))
        self.assertTrue(backups); self.assertTrue(raw_rollbacks)
        with zipfile.ZipFile(backups[-1]) as backup_zip:
            database=self.base/"rollback-recovery.sqlite3"; database.write_bytes(backup_zip.read("routecraft-local.sqlite3"))
        connection=sqlite3.connect(database)
        try: self.assertEqual(protected["id"],connection.execute("SELECT id FROM memories WHERE id=?",(protected["id"],)).fetchone()[0])
        finally: connection.close()
        connection=sqlite3.connect(raw_rollbacks[-1])
        try: self.assertEqual(protected["id"],connection.execute("SELECT id FROM memories WHERE id=?",(protected["id"],)).fetchone()[0])
        finally: connection.close()
    def test_restore_reports_success_if_rollback_cleanup_fails(self):
        archive=self.service.backup(self.base/"cleanup-failure-source.zip")
        later=self.service.add_memory(self.project["id"],"note","later than backup","must be replaced")
        real_unlink=Path.unlink
        def fail_rollback_cleanup(path,*args,**kwargs):
            if path.name.startswith(".routecraft-rollback-"): raise OSError("injected cleanup failure")
            return real_unlink(path,*args,**kwargs)
        with mock.patch.object(Path,"unlink",fail_rollback_cleanup): restored=self.service.restore(archive["output"],"RESTORE")
        self.assertIn("warnings",restored); self.assertTrue(Path(restored["retained_rollback"]).is_file())
        with self.assertRaises(NotFoundError): self.service.get_memory(later["id"])
    def test_existing_pre_v1_database_is_backed_up_before_additive_migration(self):
        older = self.base / "older"; older.mkdir(); database = older / "routecraft-local.sqlite3"
        db=sqlite3.connect(database)
        try:
            db.execute("CREATE TABLE old_data (value TEXT)"); db.execute("INSERT INTO old_data VALUES ('kept')"); db.commit()
        finally:
            db.close()
        upgraded=RouteCraftService(older); upgraded.initialize()
        self.assertTrue(list(older.glob("pre-migration-v0-*.sqlite3")))
        self.assertEqual(1, upgraded.doctor()["schema_version"])
    def test_empty_data_and_project_delete_safety_copy(self):
        self.assertTrue(self.service.doctor()["ok"]); self.assertEqual([], self.service.list_memories(self.project["id"]))
        tracked=self.service.add_memory(self.project["id"],"note","project delete","project delete")
        with self.assertRaises(ConfirmationRequiredError): self.service.delete_project(self.project["id"], "DELETE")
        real_export=self.service.export_project_package; writers=[]
        writer_code=(
            "import sys;"
            f"sys.path.insert(0,{str(ROOT/'plugins'/'codex-routecraft'/'scripts')!r});"
            "from routecraft_local.service import RouteCraftService;"
            f"RouteCraftService({str(self.service.data_dir)!r}).add_memory({self.project['id']!r},'note','delete race','must never commit then disappear')"
        )
        def export_then_start_writer(*args,**kwargs):
            result=real_export(*args,**kwargs)
            writer=subprocess.Popen([sys.executable,"-X","utf8","-c",writer_code],text=True,encoding="utf-8",stdout=subprocess.PIPE,stderr=subprocess.PIPE); writers.append(writer)
            time.sleep(0.2); self.assertIsNone(writer.poll())
            return result
        with mock.patch.object(self.service,"list_memories",side_effect=AssertionError("safety copy must bypass UI row caps")), mock.patch.object(self.service,"export_project_package",side_effect=export_then_start_writer):
            outcome=self.service.delete_project(self.project["id"], self.project["id"])
        _,writer_error=writers[0].communicate(timeout=5); self.assertNotEqual(0,writers[0].returncode); self.assertIn("project not found",writer_error)
        safety_copy=Path(outcome["safety_copy"]); self.assertTrue(safety_copy.is_file())
        self.assertEqual(self.service.data_dir.resolve(),safety_copy.resolve().parent); self.assertNotIn(self.project["id"],safety_copy.name)
        with zipfile.ZipFile(safety_copy) as archive:
            payload=json.loads(archive.read("project.json")); self.assertIn(tracked["id"],{item["id"] for item in payload["memories"]})
        if self.service.doctor()["fts5"]:
            with self.service.db.connect() as db: self.assertIsNone(db.execute("SELECT 1 FROM memories_fts WHERE memory_id=?",(tracked["id"],)).fetchone())

if __name__ == "__main__": unittest.main()
