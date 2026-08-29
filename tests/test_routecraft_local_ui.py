from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))
from routecraft_local.service import RouteCraftService
from routecraft_local.ui import create_server
from praxis_memory import PraxisMemory
from routecraft_protocols import new_event


class LocalUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.service = RouteCraftService(self.temp.name); self.service.initialize()
        self.server = create_server(self.service, port=0); self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
    def tearDown(self): self.server.shutdown(); self.thread.join(); self.server.server_close(); self.temp.cleanup()
    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1]); h = headers or {}
        if body is not None: h = {"Content-Type":"application/json", **h}; body=json.dumps(body, ensure_ascii=False).encode()
        c.request(method,path,body,h); r=c.getresponse(); data=r.read(); return r,json.loads(data.decode())
    def test_loopback_static_and_mutation_guards(self):
        with self.assertRaises(ValueError): create_server(self.service, host="0.0.0.0", port=0)
        response = http.client.HTTPConnection("127.0.0.1",self.server.server_address[1]); response.request("GET","/"); r=response.getresponse(); self.assertEqual(r.status,200); self.assertIn("charset=utf-8",r.getheader("Content-Type")); self.assertIn("connect-src 'self'",r.getheader("Content-Security-Policy")); self.assertIn("object-src 'none'",r.getheader("Content-Security-Policy")); r.read()
        responsive = http.client.HTTPConnection("127.0.0.1",self.server.server_address[1]); responsive.request("GET","/responsive.css"); r=responsive.getresponse(); self.assertEqual(r.status,200); self.assertIn("text/css",r.getheader("Content-Type")); self.assertIn(b"grid-template-columns",r.read())
        r,p=self.request("POST","/api/projects",{"name":"x"}); self.assertEqual(r.status,403); self.assertFalse(p["ok"])
        r,p=self.request("GET","/api/bootstrap"); token=p["data"]["csrf_token"]
        r,p=self.request("GET","/api/praxis/v1/snapshot"); self.assertEqual(r.status,200); self.assertFalse(p["data"]["available"])
        r,p=self.request("POST","/api/praxis/v1/snapshot",{}, {"X-RouteCraft-CSRF":token}); self.assertEqual(r.status,400); self.assertEqual(p["error"]["code"],"request")
        denied = http.client.HTTPConnection("127.0.0.1",self.server.server_address[1]); denied.request("GET","/",headers={"Host":"evil.example"}); r=denied.getresponse(); self.assertEqual(r.status,403); p=json.loads(r.read().decode()); self.assertEqual(p["error"]["code"],"host")
        r,p=self.request("POST","/api/projects",{"name":"x"},{"X-RouteCraft-CSRF":token,"Origin":"http://evil.example"}); self.assertEqual(r.status,403); self.assertEqual(p["error"]["code"],"origin")
        r,p=self.request("POST","/api/projects",{"name":"x"},{"X-RouteCraft-CSRF":token,"Content-Type":"text/plain"}); self.assertEqual(r.status,400); self.assertEqual(p["error"]["code"],"request")
        r,p=self.request("POST","/api/projects",{"name":"テスト"},{"X-RouteCraft-CSRF":token}); self.assertEqual(r.status,200); self.assertTrue(p["ok"]); self.assertIn("id",p["data"])
    def test_dashboard_memory_and_errors(self):
        r,p=self.request("GET","/api/bootstrap"); token=p["data"]["csrf_token"]
        _, project=self.request("POST","/api/projects",{"name":"テスト","git_remote_url":"https://example.invalid/repo.git","ai_agents":["Codex","Review"],"languages":["ja","en"],"tags":["ui","local"]},{"X-RouteCraft-CSRF":token}); ident=project["data"]["id"]
        self.assertEqual(project["data"]["ai_agents"],["Codex","Review"]); self.assertEqual(project["data"]["tags"],["ui","local"])
        r,p=self.request("GET","/api/projects"); self.assertTrue(p["ok"]); self.assertEqual(len(p["data"]),1)
        r,p=self.request("GET","/api/projects/"+ident); self.assertEqual(p["data"]["id"],ident)
        r,p=self.request("PATCH","/api/projects/"+ident,{"current_objective":"確認"},{"X-RouteCraft-CSRF":token}); self.assertEqual(p["data"]["current_objective"],"確認")
        r,p=self.request("POST","/api/memories",{"project_id":ident,"title":"判断","type":"decision","content":"根拠","tags":["ui","safe"],"related_files":["ui.py"],"related_commits":["abc123"],"source":"ui-test","verified":True},{"X-RouteCraft-CSRF":token}); self.assertTrue(p["ok"]); memory_id=p["data"]["id"]
        self.assertEqual(p["data"]["tags"],["ui","safe"]); self.assertEqual(p["data"]["related_files"],["ui.py"]); self.assertTrue(p["data"]["verified"])
        r,p=self.request("GET","/api/memories"); self.assertTrue(p["ok"]); self.assertEqual(len(p["data"]),1)
        r,p=self.request("POST","/api/memories/search",{"query":"判断"},{"X-RouteCraft-CSRF":token}); self.assertEqual(len(p["data"]),1)
        r,p=self.request("POST","/api/memories/search",{"project_id":ident,"types":["decision"],"tags":["ui"],"importance":["medium"],"active":True,"verified":True},{"X-RouteCraft-CSRF":token}); self.assertEqual([item["id"] for item in p["data"]],[memory_id])
        r,p=self.request("DELETE","/api/memories/"+memory_id,{}, {"X-RouteCraft-CSRF":token}); self.assertEqual(r.status,409); self.assertEqual(p["error"]["code"],"confirmation_required")
        r,p=self.request("GET","/api/memories/"+memory_id); self.assertTrue(p["ok"])
        r,p=self.request("DELETE","/api/memories/"+memory_id,{"confirm":memory_id}, {"X-RouteCraft-CSRF":token}); self.assertTrue(p["ok"])
        r,p=self.request("GET","/api/dashboard"); self.assertTrue(p["ok"]); self.assertEqual(p["data"]["projects"],1)
        r,p=self.request("POST","/api/context",{"project_id":ident},{"X-RouteCraft-CSRF":token}); self.assertTrue(p["ok"]); self.assertIn("Context Pack",p["data"]["content"])
        r,p=self.request("POST","/api/handoff",{"project_id":ident},{"X-RouteCraft-CSRF":token}); self.assertTrue(p["ok"]); self.assertTrue(Path(p["data"]["folder"]).is_dir())
        r,p=self.request("GET","/api/git?project_id="+ident); self.assertTrue(p["ok"]); self.assertIn("is_repository",p["data"])
        r,p=self.request("GET","/api/settings"); self.assertTrue(p["ok"]); self.assertEqual(p["data"]["language"],"ja")
        r,p=self.request("PATCH","/api/settings",{"language":"ja"},{"X-RouteCraft-CSRF":token}); self.assertTrue(p["ok"])
        r,p=self.request("POST","/api/backups",{},{"X-RouteCraft-CSRF":token}); self.assertTrue(p["ok"]); archive=p["data"]["output"]
        r,p=self.request("POST","/api/restore",{"backup_id":archive,"confirm":"RESTORE"},{"X-RouteCraft-CSRF":token}); self.assertTrue(p["ok"])
        r,p=self.request("GET","/api/doctor"); self.assertTrue(p["data"]["ok"])
    def test_ui_contains_all_memory_types(self):
        script=(ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_local" / "web" / "app.js").read_text(encoding="utf-8")
        for value in ("decision","failure","lesson","next_action","constraint","architecture","file_reference","dependency","deployment","security","note","session_summary"):
            self.assertIn(repr(value),script)
        for action in ("back","editMemory","deleteMemory","editProject","archiveProject","deleteProject","git","backup","doctor"):
            self.assertIn(f"'{action}' in b.dataset",script)
            self.assertNotIn(f"if(b.dataset.{action})",script)
        self.assertIn("d.retained_rollback",script); self.assertIn("warnings.join",script); self.assertIn("保持されたrollback",script)

    def test_integrated_praxis_get_reads_existing_sqlite_without_writes(self):
        memory = PraxisMemory(self.temp.name)
        memory.store_event(new_event("task.started", "ui-test", event_id="evt-ui-readonly", task_id="task-ui", status="running"))
        database = Path(self.temp.name) / "praxis-memory.sqlite3"
        before = database.read_bytes(); modified = database.stat().st_mtime_ns
        names = {item.name for item in Path(self.temp.name).iterdir()}
        server = create_server(self.service, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            connection.request("GET", "/api/praxis/v1/snapshot")
            response = connection.getresponse(); payload = json.loads(response.read().decode("utf-8")); connection.close()
            self.assertEqual(200, response.status); self.assertTrue(payload["data"]["available"])
            self.assertEqual(1, payload["data"]["data"]["events"]["total"])
        finally:
            server.shutdown(); thread.join(); server.server_close()
        self.assertEqual(before, database.read_bytes())
        self.assertEqual(modified, database.stat().st_mtime_ns)
        self.assertEqual(names, {item.name for item in Path(self.temp.name).iterdir()})

    def test_project_archive_is_post_only_and_protected(self):
        _, bootstrap = self.request("GET", "/api/bootstrap")
        token = bootstrap["data"]["csrf_token"]
        _, project = self.request("POST", "/api/projects", {"name": "archive guard"}, {"X-RouteCraft-CSRF": token})
        ident = project["data"]["id"]
        response, payload = self.request("GET", f"/api/projects/{ident}/archive")
        self.assertEqual(response.status, 400); self.assertFalse(payload["ok"])
        _, current = self.request("GET", f"/api/projects/{ident}"); self.assertFalse(current["data"]["archived"])
        response, payload = self.request("POST", f"/api/projects/{ident}/archive", {})
        self.assertEqual(response.status, 403); self.assertEqual(payload["error"]["code"], "csrf")
        response, payload = self.request("POST", f"/api/projects/{ident}/archive", {}, {"X-RouteCraft-CSRF": token, "Origin": "http://evil.example"})
        self.assertEqual(response.status, 403); self.assertEqual(payload["error"]["code"], "origin")
        _, current = self.request("GET", f"/api/projects/{ident}"); self.assertFalse(current["data"]["archived"])
        response, payload = self.request("POST", f"/api/projects/{ident}/archive", {}, {"X-RouteCraft-CSRF": token})
        self.assertEqual(response.status, 200); self.assertTrue(payload["data"]["archived"])

    def test_project_package_export_import_routes_use_explicit_paths(self):
        _, bootstrap = self.request("GET", "/api/bootstrap")
        token = bootstrap["data"]["csrf_token"]
        _, project = self.request("POST", "/api/projects", {"name": "portable"}, {"X-RouteCraft-CSRF": token})
        ident = project["data"]["id"]
        response, payload = self.request("POST", f"/api/projects/{ident}/package", {"action": "export"}, {"X-RouteCraft-CSRF": token})
        self.assertEqual(response.status, 400); self.assertEqual(payload["error"]["code"], "request")
        package = str(Path(self.temp.name) / "portable.zip")
        response, payload = self.request("POST", f"/api/projects/{ident}/package", {"action": "export", "output": package}, {"X-RouteCraft-CSRF": token})
        self.assertEqual(response.status, 200); self.assertEqual(payload["data"]["output"], str(Path(package).resolve())); self.assertTrue(Path(package).is_file())
        response, payload = self.request("POST", "/api/project-package/import", {"path": package, "conflict": "detect"}, {"X-RouteCraft-CSRF": token})
        self.assertEqual(response.status, 200); self.assertFalse(payload["data"]["imported"]); self.assertEqual(payload["data"]["conflict"], ident)
        script = (ROOT / "plugins" / "codex-routecraft" / "scripts" / "routecraft_local" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("data-package-export", script); self.assertIn("data-package-import", script)
        self.assertIn("/api/project-package/import", script); self.assertIn("/package", script)

if __name__ == "__main__": unittest.main()
