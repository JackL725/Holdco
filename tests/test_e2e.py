"""End-to-end test: the real Holdco server, driven over HTTP, with a fake model server.

Covers founding from a template, the 24/7 scheduler, approvals, chat, QA review with the deliverable check,
recurring duties, restart recovery, the prompt-based tool fallback, the OpenAI-compatible backend, pausing,
security checks and deletion.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_ollama import FakeModelServer  # noqa: E402
from tests.helpers import HoldcoProcess, wait_for  # noqa: E402


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = FakeModelServer(delay=0.05).start()
        cls.app = HoldcoProcess().start()
        cls.api = cls.app.client
        cls.api.put("/api/settings", {"llm_base_url": cls.model.url, "max_concurrent_runs": 2})

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.model.stop()

    def tearDown(self):
        problems = [line for line in self.app.log().splitlines() if "Traceback" in line]
        self.assertFalse(problems, "server logged errors:\n" + self.app.log()[-3000:])

    # ---------------------------------------------------------------- helpers
    def tasks(self, cid):
        return self.api.get(f"/api/companies/{cid}/tasks")["tasks"]

    def pending(self, cid=None):
        rows = self.api.get("/api/approvals")["approvals"]
        return [a for a in rows if cid is None or a["company_id"] == cid]

    def agent(self, cid, role):
        return next(a for a in self.api.get(f"/api/companies/{cid}")["agents"] if a["role"] == role)

    def last_transcript(self, cid, agent_id, task_id=None):
        runs = self.api.get(f"/api/companies/{cid}/runs?agent_id={agent_id}")["runs"]
        if task_id:
            runs = [r for r in runs if r["task_id"] == task_id]
        return self.api.get(f"/api/runs/{runs[0]['id']}")["run"]["transcript"]

    # ---------------------------------------------------------------- tests (run in order)
    def test_01_dashboard_and_model(self):
        status, body, _ = self.api.raw("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/static/app.js", body)
        for asset in ("/static/app.js", "/static/style.css"):
            self.assertEqual(self.api.raw("GET", asset)[0], 200)
        self.assertEqual(self.api.get("/api/llm/models")["models"], ["gpt-oss:20b", "notools:latest"])
        self.assertTrue(self.api.post("/api/llm/test")["ok"])
        r = self.api.post("/api/llm/test", {"model": "notools:latest"})
        self.assertIn("json mode", r["message"])
        presets = self.api.get("/api/presets")
        self.assertGreaterEqual(len(presets["presets"]), 19)
        self.assertIn("launch", presets["templates"])
        self.assertEqual(len(self.api.post("/api/ideas", {"interests": "dogs"})["ideas"]), 3)

    def test_02_security(self):
        status, _, _ = self.api.raw("POST", "/api/companies", {"idea": "x" * 20}, {})  # no X-Holdco header
        self.assertEqual(status, 403)
        status, _, _ = self.api.raw("POST", "/api/companies", None, {"X-Holdco": "1", "Content-Type": "text/plain"})
        self.assertIn(status, (400, 415))
        self.api.post("/api/companies", {"idea": "short"}, expect=400)

    def test_03_found_company_and_autopilot(self):
        c = self.api.post("/api/companies", {"idea": "Personalised weekly meal plans for dog owners",
                                             "template": "launch"})["company"]
        type(self).cid = cid = c["id"]
        team = {a["role"]: a["preset"] for a in self.api.get(f"/api/companies/{cid}")["agents"]}
        self.assertEqual(set(team), {"ceo", "research", "engineering", "qa", "marketing", "sales", "support", "ops"})
        wait_for(lambda: self.api.get(f"/api/companies/{cid}")["company"]["status"] == "active", 30, what="founding")
        info = self.api.get(f"/api/companies/{cid}")
        self.assertEqual(info["company"]["name"], "PawPlan")
        self.assertIn("One-liner", info["company"]["charter"])
        # the scheduler hands the founding tasks to the team
        wait_for(lambda: sum(t["status"] == "done" for t in self.tasks(cid)) >= 5, 60, what="first tasks done")
        wait_for(lambda: len(self.pending(cid)) >= 5, 30, what="approval requests")
        actions = {a["action"] for a in self.pending(cid)}
        self.assertTrue({"social_post", "deploy_site", "send_email", "spend_money", "run_code"} <= actions, actions)
        self.assertTrue(info["has_site"] or self.api.get(f"/api/companies/{cid}")["has_site"])
        # handoffs are structured
        built = next(t for t in self.tasks(cid) if t["title"] == "Build landing page")
        self.assertIn("Deliverable: site/index.html", built["result"])
        # per-role model settings were sent: gpt-oss reasoning effort + recommended sampling
        thinks = {c["think"] for c in self.model.calls if c["path"] == "/api/chat"}
        self.assertTrue({"high", "medium"} <= thinks, thinks)
        opts = next(c["options"] for c in self.model.calls if c["path"] == "/api/chat" and c["ntools"])
        self.assertEqual((opts["temperature"], opts["top_p"]), (1.0, 1.0))

    def test_04_files_and_preview(self):
        c = self.api.get(f"/api/companies/{self.cid}")["company"]
        status, body, headers = self.api.raw("GET", f"/preview/{c['slug']}/")
        self.assertEqual(status, 200)
        self.assertIn(b"Meal plans", body)
        self.assertIn("sandbox", headers.get("Content-Security-Policy", ""))
        self.assertIn(self.api.raw("GET", f"/preview/{c['slug']}/../../holdco.db")[0], (400, 404))
        self.api.get(f"/api/companies/{self.cid}/file?path=../../holdco.db", expect=400)
        files = [f["path"] for f in self.api.get(f"/api/companies/{self.cid}/files")["files"]]
        self.assertIn("research/market.md", files)
        self.api.put(f"/api/companies/{self.cid}/file", {"path": "docs/owner.md", "content": "hello"})
        self.assertEqual(self.api.get(f"/api/companies/{self.cid}/file?path=docs/owner.md")["content"], "hello")

    def test_05_approvals(self):
        by_action = {a["action"]: a for a in self.pending(self.cid)}
        r = self.api.post(f"/api/approvals/{by_action['social_post']['id']}",
                          {"decision": "approve", "payload": {"content": "Edited by owner"}})["approval"]
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["payload"]["content"], "Edited by owner")
        r = self.api.post(f"/api/approvals/{by_action['send_email']['id']}", {"decision": "approve"})["approval"]
        self.assertEqual(r["status"], "failed")  # no SMTP configured
        self.assertEqual(self.api.post(f"/api/approvals/{r['id']}/retry")["approval"]["status"], "pending")
        self.api.post(f"/api/approvals/{by_action['deploy_site']['id']}",
                      {"decision": "reject", "feedback": "Fix the form labels first"})
        wait_for(lambda: any(t["title"].startswith("Revise:") for t in self.tasks(self.cid)), 10, what="revise task")
        r = self.api.post(f"/api/approvals/{by_action['run_code']['id']}", {"decision": "approve"})["approval"]
        self.assertIn("4", r["result"])
        self.api.post(f"/api/approvals/{by_action['run_code']['id']}", {"decision": "approve"}, expect=400)

    def test_06_chat(self):
        r = self.api.post(f"/api/companies/{self.cid}/messages", {"content": "Draft a Reddit post please"})
        self.assertIn(r["agent"], ("Atlas",))
        wait_for(lambda: any(m["sender"] == "agent" and "queued that" in m["content"]
                             for m in self.api.get(f"/api/companies/{self.cid}/messages")["messages"]),
                 60, what="CEO chat reply")

    def test_07_qa_review_and_deliverable_check(self):
        qa = self.agent(self.cid, "qa")
        self.api.post(f"/api/companies/{self.cid}/tasks", {"title": "Review the landing page", "agent_id": qa["id"],
                                                          "deliverable": "docs/reviews.md", "acceptance": "- audit run"})
        wait_for(lambda: any(t["title"] == "Fix form labels on landing page" and t["status"] == "done"
                             for t in self.tasks(self.cid)), 60, what="QA fix task completed")
        audit = next(m["content"] for m in self.last_transcript(self.cid, qa["id"])
                     if m["role"] == "tool" and m["name"] == "audit_page")
        self.assertIn("viewport", audit)
        forge = self.agent(self.cid, "engineering")
        fix = next(t for t in self.tasks(self.cid) if t["title"] == "Fix form labels on landing page")
        tool_results = [m["content"] for m in self.last_transcript(self.cid, forge["id"], fix["id"]) if m["role"] == "tool"]
        self.assertTrue(any("Not finished yet" in r for r in tool_results), tool_results)

    def test_08_recurring_duties(self):
        db = sqlite3.connect(str(Path(self.app.data_dir) / "holdco.db"))
        db.execute("UPDATE companies SET created_at='2020-01-01T00:00:00Z' WHERE id=?", (self.cid,))
        db.commit()
        db.close()
        wait_for(lambda: {t["routine"] for t in self.tasks(self.cid) if t["routine"]} >=
                 {"ceo:Morning report", "qa:Daily review", "qa:Weekly site audit", "research:Competitor watch"},
                 30, what="recurring duties")
        routines = {t["routine"] for t in self.tasks(self.cid) if t["routine"]}
        self.assertNotIn("support:Inbox sweep", routines)          # needs email connected
        self.assertNotIn("sales:Follow-ups and replies", routines)

    def test_09_pause(self):
        self.api.patch(f"/api/companies/{self.cid}", {"status": "paused"})
        wait_for(lambda: self.api.get(f"/api/companies/{self.cid}")["stats"]["agents_working"] == 0, 30,
                 what="agents to stop")
        before = len(self.api.get(f"/api/companies/{self.cid}/runs")["runs"])
        self.api.post(f"/api/companies/{self.cid}/tasks", {"title": "Should wait while paused"})
        import time
        time.sleep(3)
        self.assertEqual(len(self.api.get(f"/api/companies/{self.cid}/runs")["runs"]), before)
        self.api.patch(f"/api/companies/{self.cid}", {"status": "active"})

    def test_10_hire_and_settings(self):
        a = self.api.post(f"/api/companies/{self.cid}/agents", {"preset": "seo"})["agent"]
        self.assertEqual((a["name"], a["role"], a["preset"]), ("Quill", "seo", "seo"))
        self.api.post(f"/api/companies/{self.cid}/agents", {"role": "seo", "title": "x"}, expect=400)
        self.api.patch(f"/api/agents/{a['id']}", {"reasoning": "low", "routines_on": False})
        a = self.agent(self.cid, "seo")
        self.assertEqual((a["reasoning"], a["routines_on"]), ("low", 0))
        c = self.api.patch(f"/api/companies/{self.cid}", {"settings": {"directives": "Never offer discounts",
                                                                       "smtp_password": "secret",
                                                                       "policies": {"spend_money": "auto"}}})["company"]
        self.assertEqual(c["settings"]["smtp_password"], "••••••••")   # secrets are masked
        self.assertNotIn("spend_money", c["settings"]["policies"])    # purchases can't be auto-approved

    def test_11_restart_recovery(self):
        self.model.delay = 0.4
        c = self.api.post("/api/companies", {"idea": "Weekly social media plans for small cafes",
                                             "name": "BrewBuzz", "template": "minimal"})["company"]
        wait_for(lambda: len(self.api.get(f"/api/companies/{c['id']}/runs")["runs"]) > 0, 10, what="founding start")
        self.app.stop()
        self.app.start()
        self.model.delay = 0.05
        wait_for(lambda: self.api.get(f"/api/companies/{c['id']}")["company"]["status"] == "active", 60,
                 what="founding to resume after restart")
        info = self.api.get(f"/api/companies/{c['id']}")
        self.assertEqual(info["company"]["name"], "BrewBuzz")   # owner-chosen name kept
        self.assertEqual(len(info["agents"]), 3)
        titles = [t["title"] for t in self.tasks(c["id"])]
        self.assertEqual(len(titles), len(set(titles)), "founding resumed without duplicating tasks")
        type(self).cid2 = c["id"]

    def test_12_prompt_based_tool_fallback(self):
        self.api.put("/api/settings", {"default_model": "notools:latest"})
        echo = self.agent(self.cid, "marketing")
        t = self.api.post(f"/api/companies/{self.cid}/tasks", {"title": "Draft launch post v2", "agent_id": echo["id"],
                                                              "priority": 1})["task"]
        wait_for(lambda: next(x for x in self.tasks(self.cid) if x["id"] == t["id"])["status"] == "done", 60,
                 what="task via prompt-based tool calls")
        self.api.put("/api/settings", {"default_model": "gpt-oss:20b"})

    def test_13_openai_compatible_backend(self):
        self.api.put("/api/settings", {"llm_backend": "openai", "default_model": "local-model"})
        self.assertTrue(self.api.post("/api/llm/test")["ok"])
        scout = self.agent(self.cid, "research")
        t = self.api.post(f"/api/companies/{self.cid}/tasks", {"title": "Scan pricing again", "agent_id": scout["id"],
                                                              "priority": 1})["task"]
        wait_for(lambda: next(x for x in self.tasks(self.cid) if x["id"] == t["id"])["status"] == "done", 60,
                 what="task via OpenAI-compatible API")
        self.assertTrue(any(c["path"] == "/v1/chat/completions" for c in self.model.calls))
        self.api.put("/api/settings", {"llm_backend": "ollama", "default_model": "gpt-oss:20b"})

    def test_14_thinking_view(self):
        think = self.api.get(f"/api/events?company_id={self.cid}&kinds=think&limit=300")["events"]
        self.assertTrue(think, "agents' reasoning should be recorded")
        self.assertTrue(any(e["data"].get("thinking") == "planning..." and e["data"].get("calls") for e in think))
        results = self.api.get(f"/api/events?company_id={self.cid}&kinds=result&limit=300")["events"]
        self.assertTrue(any(e["data"].get("name") == "web_search" for e in results))
        one = think[0]["agent_id"]
        only = self.api.get(f"/api/events?company_id={self.cid}&kinds=think&agent_id={one}")["events"]
        self.assertTrue(only and all(e["agent_id"] == one for e in only))
        # the main feed stays readable
        feed = self.api.get(f"/api/events?company_id={self.cid}&exclude=think,result&limit=300")["events"]
        self.assertFalse({e["kind"] for e in feed} & {"think", "result"})
        # junk filter values are ignored, not injected
        self.api.get(f"/api/events?company_id={self.cid}&kinds=think%27%3B--")

    def test_15_feed_and_delete(self):
        events = self.api.get(f"/api/events?company_id={self.cid}&limit=300")["events"]
        kinds = {e["kind"] for e in events}
        self.assertTrue({"founding", "run", "tool", "file", "task", "approval", "decision", "done"} <= kinds, kinds)
        self.api.delete(f"/api/companies/{self.cid2}", {"confirm": "wrong"}, expect=400)
        self.api.delete(f"/api/companies/{self.cid2}", {"confirm": "BrewBuzz"})
        self.assertNotIn(self.cid2, [c["id"] for c in self.api.get("/api/state")["companies"]])
        self.assertTrue((Path(self.app.data_dir) / "trash").exists())


if __name__ == "__main__":
    unittest.main()
