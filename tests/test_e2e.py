"""End-to-end test: the real Holdco server, driven over HTTP, with a fake model server.

Covers founding from a template, the 24/7 scheduler, approvals, chat, QA review with the deliverable check,
recurring duties, restart recovery, the prompt-based tool fallback, the OpenAI-compatible backend, pausing,
security checks and deletion; Phase 1 adds connectors and the vault, the Stripe gate (a payment link created by an
agent, opened by browser_check, a test purchase polled in as revenue), Resend and Buffer behind approvals, and an
OAuth sign-in to an MCP server. The outside services are fakes on 127.0.0.1.
"""
from __future__ import annotations

import secrets
import sqlite3
import sys
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_buffer import FakeBuffer  # noqa: E402
from tests.fake_mcp_server import FakeMCPHttp  # noqa: E402
from tests.fake_oauth import FakeOAuth  # noqa: E402
from tests.fake_ollama import FakeModelServer  # noqa: E402
from tests.fake_resend import FakeResend  # noqa: E402
from tests.fake_stripe import FakeStripe  # noqa: E402
from tests.fake_telegram import FakeTelegram  # noqa: E402
from tests.helpers import HoldcoProcess, free_port, wait_for  # noqa: E402

MASK = "\u2022" * 8
STRIPE_KEY = "sk_test_e2e" + secrets.token_hex(10)
RESEND_KEY = "re_e2e" + secrets.token_hex(10)


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = FakeModelServer(delay=0.05).start()
        # Phase 1: the outside services the connectors talk to, all on 127.0.0.1.
        cls.stripe = FakeStripe().start()
        cls.resend = FakeResend(domains=("pawplan.example",)).start()
        cls.buffer = FakeBuffer().start()
        cls.oauth = FakeOAuth().start()
        cls.mcp = FakeMCPHttp(auth_base=cls.oauth.url).start()
        cls.app = HoldcoProcess(env={
            "HOLDCO_VAULT_BACKEND": "file",            # never touch the developer's keyring
            "HOLDCO_ALLOW_LOCAL": "127.0.0.1:*",       # the fakes count as public sites (test only)
            "HOLDCO_METRICS_POLL_SECONDS": "1", "HOLDCO_CONNECTOR_CHECK_SECONDS": "5",
            "HOLDCO_TEST_MCP": cls.mcp.mcp_url,        # adds the test-only "fakemcp" connector (test_29)
        }).start()
        cls.api = cls.app.client
        cls.api.put("/api/settings", {"llm_base_url": cls.model.url, "max_concurrent_runs": 2})

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        for fake in (cls.model, cls.stripe, cls.resend, cls.buffer, cls.mcp, cls.oauth):
            fake.stop()

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
        # Commercial email must carry a postal address (Phase 0.5 outreach checks), so the owner sets one.
        self.api.patch(f"/api/companies/{cid}", {"settings": {"postal_address": "1 Main Street, Springfield, IL 62701"}})
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
                 {"ceo:Morning plan", "qa:Daily review", "qa:Weekly site audit", "research:Competitor watch"},
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

    # ---------------------------------------------------------------- phase 0: stabilize
    def test_16_doctor(self):
        d = self.api.get("/api/doctor")
        rows = {r["key"]: r for r in d["checks"]}
        self.assertIn("model_server", rows)
        self.assertEqual(rows["model_server"]["status"], "ok", rows["model_server"])
        for key in ("key", "label", "status", "detail", "fix", "checked_at"):
            self.assertIn(key, rows["model_server"])
        self.assertTrue({"ok", "warn", "bad", "checked_at"} <= set(d["summary"]), d["summary"])
        self.assertIn("doctor", self.api.get("/api/state"))
        r = self.api.post("/api/doctor/run", {})
        self.assertTrue(r["checks"])
        self.assertEqual(next(x for x in r["checks"] if x["key"] == "model_server")["status"], "ok")
        self.assertTrue(all(x["status"] in ("ok", "warn", "bad") for x in r["checks"]))
        # a backup on demand shows up in the list and on disk
        b = self.api.post("/api/backup")
        self.assertTrue(b["name"].startswith("holdco-") and b["name"].endswith(".zip"), b)
        listed = self.api.get("/api/backups")
        self.assertIn(b["name"], [x["name"] for x in listed["backups"]])
        self.assertTrue((Path(self.app.data_dir) / "backups" / b["name"]).exists())
        self.assertEqual(listed["keep"], 14)
        self.assertEqual(next(x for x in self.api.get("/api/doctor")["checks"] if x["key"] == "backup")["status"], "ok")

    def test_17_preflight(self):
        body = {"idea": "A weekly newsletter for home bakers", "template": "minimal", "name": "Preflight Co"}
        self.api.put("/api/settings", {"llm_base_url": f"http://127.0.0.1:{free_port()}"})
        try:
            r = self.api.post("/api/companies", body, expect=409)
            self.assertIn("Fix this before founding", r["error"])
            checks = {c["key"]: c for c in r["checks"]}
            self.assertEqual(checks["model_server"]["status"], "bad", checks)
            self.assertEqual(checks["model_server"]["fix"]["kind"], "link")
        finally:
            self.api.put("/api/settings", {"llm_base_url": self.model.url})
        # the preflight merged its bad row into the cache; a fresh run clears it
        fresh = self.api.post("/api/doctor/run", {})["checks"]
        self.assertEqual(next(x for x in fresh if x["key"] == "model_server")["status"], "ok")
        # with the model back, founding works again without force (no company is founded on a dead model)
        c = self.api.post("/api/companies", body)["company"]
        wait_for(lambda: self.api.get(f"/api/companies/{c['id']}")["company"]["status"] == "active", 60,
                 what="Preflight Co founding")
        self.assertEqual(self.api.get(f"/api/companies/{c['id']}")["company"]["name"], "Preflight Co")
        self.api.delete(f"/api/companies/{c['id']}", {"confirm": "Preflight Co"})
        self.assertNotIn(c["id"], [x["id"] for x in self.api.get("/api/state")["companies"]])

    def test_18_kill_switch(self):
        self.assertTrue(self.api.post("/api/kill", {"on": True})["kill_switch"])
        self.assertTrue(self.api.get("/api/state")["kill_switch"])
        # sessions already running end at their next step; wait for that so the run count is stable
        wait_for(lambda: self.api.get("/api/state")["running"] == 0, 30, what="agents to stop")
        echo = self.agent(self.cid, "marketing")
        before = len(self.api.get(f"/api/companies/{self.cid}/runs")["runs"])
        t = self.api.post(f"/api/companies/{self.cid}/tasks", {"title": "Write launch post while stopped",
                                                              "agent_id": echo["id"], "priority": 1})["task"]
        time.sleep(4)
        self.assertEqual(len(self.api.get(f"/api/companies/{self.cid}/runs")["runs"]), before)
        self.assertEqual(next(x for x in self.tasks(self.cid) if x["id"] == t["id"])["status"], "todo")
        requests = [a for a in self.pending() if a["action"] != "question"]
        if requests:
            r = self.api.post(f"/api/approvals/{requests[0]['id']}", {"decision": "approve"}, expect=400)
            self.assertIn("kill switch", r["error"].lower())
        # (no pending request means every one was decided earlier; TestKillSwitch covers decide() directly)
        self.assertFalse(self.api.post("/api/kill", {"on": False})["kill_switch"])
        self.assertFalse(self.api.get("/api/state")["kill_switch"])
        wait_for(lambda: next(x for x in self.tasks(self.cid) if x["id"] == t["id"])["status"] != "todo", 60,
                 what="the task to run after resume")

    def test_19_phone(self):
        fake = FakeTelegram().start()
        try:
            self.api.put("/api/settings", {"telegram_bot_token": fake.token, "telegram_chat_id": str(fake.chat_id),
                                           "telegram_api_base": fake.url, "notify_level": "all"})
            r = self.api.post("/api/notify/test")
            self.assertTrue(r["ok"], r)
            tests = [m for m in fake.sent if "Holdco test" in m.get("text", "")]
            self.assertEqual(len(tests), 1, fake.sent)
            self.assertEqual(tests[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "noop")
            # a pending post with the same text would be deduplicated instead of announced: clear the way
            for a in self.pending(self.cid):
                if a["action"] == "social_post":
                    self.api.post(f"/api/approvals/{a['id']}", {"decision": "reject"})
            echo = self.agent(self.cid, "marketing")
            self.api.post(f"/api/companies/{self.cid}/tasks", {"title": "Write launch post for the phone",
                                                              "agent_id": echo["id"], "priority": 1})

            def approve_button():
                ids = {a["id"] for a in self.pending(self.cid) if a["action"] == "social_post"}
                for m in fake.sent:
                    for row in (m.get("reply_markup") or {}).get("inline_keyboard", []):
                        for b in row:
                            parts = str(b.get("callback_data", "")).split(":")
                            if len(parts) == 3 and parts[0] == "a" and parts[2] == "approve" and int(parts[1]) in ids:
                                return m, b["callback_data"]
                return None
            m, data = wait_for(approve_button, 60, what="the post request to reach the phone")
            aid = int(data.split(":")[1])
            self.assertIn("PawPlan", m["text"])
            fake.push_callback(data, message_id=m["message_id"])
            wait_for(lambda: any(a["id"] == aid and a["status"] == "done"
                                 for a in self.api.get("/api/approvals?status=decided")["approvals"]),
                     60, what="the phone button to approve the request")
            wait_for(lambda: fake.answered and any(e["method"] == "editMessageText" for e in fake.edits), 10,
                     what="callback answered and message edited")
            self.assertIn("You:", next(e for e in fake.edits if e["method"] == "editMessageText")["text"])
        finally:
            self.api.put("/api/settings", {"telegram_bot_token": "", "telegram_chat_id": "",
                                           "telegram_api_base": "https://api.telegram.org", "notify_level": "important"})
            time.sleep(2)   # the poller sees the change on its next loop and exits before the fake goes away
            fake.stop()

    def test_20_audit(self):
        rows = self.api.get("/api/audit")["audit"]
        self.assertTrue(rows)
        self.assertTrue(any(r["approved_by"] == "owner" for r in rows), [r["approved_by"] for r in rows])
        self.assertTrue(all(isinstance(r["payload"], dict) for r in rows))
        self.assertTrue(all(r["company_name"] for r in rows if r["company_id"]))
        self.assertEqual([r["id"] for r in rows], sorted((r["id"] for r in rows), reverse=True))
        mine = self.api.get(f"/api/audit?company_id={self.cid}&limit=5")["audit"]
        self.assertTrue(mine and len(mine) <= 5 and all(r["company_id"] == self.cid for r in mine))
        phone = next(r for r in rows if r["action"] == "social_post" and r["approved_by"] == "owner")
        self.assertIn("content", phone["payload"])

    # ---------------------------------------------------------------- phase 0.5: ground truth and context
    def test_21_context_library_round_trip(self):
        cid = self.cid
        # the owner's global library: every company sees it
        r = self.api.put("/api/context/file", {"path": "e2e/about-me.md",
                                               "content": "# About me\n\nI only sell to quetzalcoatl breeders.\n"})
        self.assertEqual(r["path"], "e2e/about-me.md")
        pin = self.api.put("/api/context/pinned", {"paths": ["e2e/about-me.md"]})
        self.assertEqual(pin["pinned"], ["e2e/about-me.md"])
        listing = self.api.get("/api/context")
        f = next(x for x in listing["files"] if x["path"] == "e2e/about-me.md")
        self.assertTrue(f["pinned"] and f["indexed"] and f["text"])
        self.assertEqual(listing["pinned_chars"], f["size"])
        self.assertEqual((listing["pinned_cap"], listing["max_bytes"]), (2500, 500000))
        self.assertIn(".md", listing["formats"])
        self.assertIn("quetzalcoatl", self.api.get("/api/context/file?path=e2e/about-me.md")["content"])
        # the company's own library
        self.api.put(f"/api/companies/{cid}/context/file", {"path": "calls/vet.md",
                                                            "content": "# Vet call\n\nThe vet likes quetzalcoatl kibble.\n"})
        self.api.put(f"/api/companies/{cid}/context/pinned", {"paths": ["calls/vet.md"]})
        mine = self.api.get(f"/api/companies/{cid}/context")
        self.assertEqual(mine["pinned"], ["calls/vet.md"])
        self.assertGreater(mine["pinned_chars"], 0)
        # the dashboard search finds both, with path and line, the matching word marked
        res = self.api.get(f"/api/companies/{cid}/search?q=quetzalcoatl&scope=context")
        self.assertEqual(res["scope"], "context")
        hits = {(h["path"], h["line"]) for h in res["results"]}
        self.assertEqual(hits, {("global:e2e/about-me.md", 3), ("context:calls/vet.md", 3)})
        self.assertTrue(all("[quetzalcoatl]" in h["snippet"] for h in res["results"]))
        everything = self.api.get(f"/api/companies/{cid}/search?q=quetzalcoatl")
        self.assertEqual({h["source"] for h in everything["results"]}, {"context"})
        self.api.get(f"/api/companies/{cid}/search?q=x&scope=planets", expect=400)
        # delete both: gone from the list, the pins and the search
        self.api.delete("/api/context/file", {"path": "e2e/about-me.md"})
        self.api.delete(f"/api/companies/{cid}/context/file", {"path": "calls/vet.md"})
        self.assertFalse(any(x["path"] == "e2e/about-me.md" for x in self.api.get("/api/context")["files"]))
        self.assertEqual(self.api.get("/api/context")["pinned"], [])
        self.assertEqual(self.api.get(f"/api/companies/{cid}/context")["pinned"], [])
        self.assertEqual(self.api.get(f"/api/companies/{cid}/search?q=quetzalcoatl")["results"], [])
        self.assertTrue(self.api.get("/api/index/status")["fts"])

    def test_22_facts_route(self):
        f = self.api.get(f"/api/companies/{self.cid}/facts")
        self.assertTrue(f["text"].startswith("# Facts (from Holdco's records; these override anything in files)"))
        self.assertFalse(f["website"]["deployed"])                       # no Netlify token: nothing went online
        self.assertTrue(f["website"]["line"].startswith("Website: local preview only; not deployed."), f["website"])
        self.assertIn(f["website"]["line"], f["text"])
        self.assertTrue(f["email"]["line"].startswith("Email: not connected"), f["email"])
        self.assertTrue(f["money"]["line"].startswith("Money: "))
        self.assertTrue(f["signals"]["line"])
        # agents keep working meanwhile, so compare two reads taken back to back until they agree
        wait_for(lambda: {w["id"] for w in self.api.get(f"/api/companies/{self.cid}/facts")["waiting"]}
                 == {a["id"] for a in self.pending(self.cid)}, 20, what="the waiting list to match the inbox")
        self.assertTrue(f["waiting"], "PawPlan has requests waiting for the owner")
        for w in f["waiting"]:
            self.assertIn(w["kind"], ("question", "approval"))
            self.assertTrue(w["label"] and w["age"] and w["created_at"])
        self.assertIsInstance(f["accounts"], list)
        self.assertIsInstance(f["down"], list)

    def test_23_planning_route(self):
        p = self.api.get(f"/api/companies/{self.cid}/planning")["planning"]
        self.assertEqual(p["company_id"], self.cid)
        self.assertEqual(p["founding_stage"], "done")
        self.assertIsInstance(p["cycle_backoff"], int)
        self.assertGreaterEqual(p["next_due_minutes"], 0)
        self.assertIsInstance(p["changed"], bool)
        self.assertEqual(p["budget"]["budget"], 3000000)
        self.assertGreater(p["budget"]["used"], 0)
        self.assertFalse(p["budget"]["over"])
        mine = next(b for b in self.api.get("/api/budget")["budget"] if b["company_id"] == self.cid)
        self.assertEqual((mine["name"], mine["budget"], mine["over"]), ("PawPlan", 3000000, False))
        # the owner's settings form: the three new fields round-trip, internal keys stay out of it
        c = self.api.patch(f"/api/companies/{self.cid}", {"settings": {"archetype": "saas", "daily_token_budget": "2500000",
                                                                        "postal_address": "1 Main Street, Springfield, IL 62701",
                                                                        "founding_retry": "charter"}})["company"]
        self.assertEqual((c["settings"]["archetype"], c["settings"]["daily_token_budget"]), ("saas", 2500000))
        self.assertNotEqual(c["settings"].get("founding_retry"), "charter")   # internal keys are not the form's
        self.assertEqual(self.api.get(f"/api/companies/{self.cid}/planning")["planning"]["archetype"], "saas")
        self.api.patch(f"/api/companies/{self.cid}", {"settings": {"archetype": "", "daily_token_budget": 3000000}})
        s = self.api.put("/api/settings", {"review_model": "gpt-oss:20b", "embedding_model": ""})["settings"]
        self.assertEqual(s["review_model"], "gpt-oss:20b")
        self.api.put("/api/settings", {"review_model": ""})

    def test_24_doctor_phase05_rows(self):
        rows = {r["key"]: r for r in self.api.post("/api/doctor/run", {})["checks"]}
        self.assertEqual(rows["search_index"]["status"], "ok", rows["search_index"])
        self.assertIn("Full-text index:", rows["search_index"]["detail"])
        self.assertEqual(rows["token_budgets"]["status"], "ok", rows["token_budgets"])
        self.assertIn("PawPlan", rows["token_budgets"]["detail"])
        r = self.api.post("/api/index/rebuild", {})
        self.assertTrue(r["ok"])
        wait_for(lambda: next(x for x in self.api.get("/api/doctor")["checks"] if x["key"] == "search_index")
                 ["status"] == "ok", 30, what="the search index row after a rebuild")

    def test_25_staged_founding(self):
        critique = self.api.get(f"/api/companies/{self.cid}/file?path=docs/critique.md")["content"]
        self.assertIn("Three strongest reasons this fails", critique)
        charter = self.api.get(f"/api/companies/{self.cid}")["company"]["charter"]
        for section in ("## One-liner", "## Competitors", "## Riskiest assumption", "## 30-day plan"):
            self.assertIn(section, charter)
        hits = self.api.get(f"/api/companies/{self.cid}/search?q=strongest%20reasons%20fails&scope=files")["results"]
        self.assertIn("docs/critique.md", [h["path"] for h in hits])
        stages = [e["data"].get("stage") for e in reversed(self.api.get(
            f"/api/events?company_id={self.cid}&kinds=founding&limit=300")["events"]) if e["data"].get("stage")]
        self.assertEqual(stages[:4], ["research", "charter", "critic", "plan"])


    # ---------------------------------------------------------------- phase 1: connect
    def connector(self, iid):
        rows = self.api.get(f"/api/connectors?company_id={self.cid}")["instances"]
        return next((i for i in rows if i["id"] == iid), None)

    def test_26_connectors_api_and_vault(self):
        out = self.api.post("/api/connectors", {"key": "stripe", "company_id": self.cid, "enabled": True,
                                                "config": {"api_key": STRIPE_KEY, "api_base": self.stripe.url}})
        self.assertEqual(out["health"], {"status": "ok", "detail": "test mode, balance available"})
        inst = out["instance"]
        type(self).stripe_id = inst["id"]
        self.assertEqual((inst["key"], inst["company_id"], inst["mode"], inst["enabled"]),
                         ("stripe", self.cid, "test", True))
        listing = self.api.get(f"/api/connectors?company_id={self.cid}")
        self.assertEqual(self.connector(inst["id"])["config"]["api_key"], MASK)
        self.assertEqual(listing["statuses"][str(inst["id"])]["status"], "ok")
        card = listing["definitions"]["stripe"]
        self.assertTrue(any(f["key"] == "api_base" and f["hidden"] for f in card["setup"]["fields"]))
        self.assertIn("stripe_mcp", listing["definitions"])
        self.assertTrue(listing["definitions"]["buffer"]["beta"])
        self.assertNotIn(STRIPE_KEY, str(listing))
        # the key is in the vault, and nowhere in the database
        data = Path(self.app.data_dir)
        self.assertTrue((data / "vault.json").exists())
        self.assertNotIn(STRIPE_KEY, (data / "vault.json").read_text(encoding="utf-8"))
        db = sqlite3.connect(str(data / "holdco.db"))
        try:
            for table in ("settings", "companies", "connectors"):
                for row in db.execute(f"SELECT * FROM {table}").fetchall():
                    self.assertNotIn(STRIPE_KEY, " ".join(str(x) for x in row), table)
        finally:
            db.close()
        for f in ("holdco.db", "holdco.db-wal"):
            if (data / f).exists():
                self.assertNotIn(STRIPE_KEY.encode(), (data / f).read_bytes(), f)
        rows = {r["key"]: r for r in self.api.get("/api/doctor")["checks"]}
        self.assertEqual(rows[f"connector:{inst['id']}"]["status"], "ok", rows.get(f"connector:{inst['id']}"))
        self.assertEqual(rows["vault"]["status"], "ok", rows["vault"])

    def test_27_stripe_gate(self):
        cid = self.cid
        self.api.patch(f"/api/companies/{cid}", {"settings": {"policies": {"connector_publish": "auto"}}})
        try:
            forge = self.agent(cid, "engineering")
            t = self.api.post(f"/api/companies/{cid}/tasks", {
                "title": "Create a payment link for the Starter plan", "agent_id": forge["id"], "priority": 1,
                "description": "Use the Stripe connector. $19 per month.",
                "deliverable": "Stripe payment link (URL in the handoff)",
                "acceptance": "- the link is live in Stripe test mode\n- browser_check on the link returns 200"})["task"]
            done = wait_for(lambda: next((x for x in self.tasks(cid) if x["id"] == t["id"] and x["status"] == "done"),
                                         None), 90, what="the payment link task")
        finally:
            self.api.patch(f"/api/companies/{cid}", {"settings": {"policies": {}}})
        link = f"{self.stripe.url}/pay/plink_1"
        self.assertIn(link, done["result"])
        # the transcript: find_tool, then the Stripe tool, then browser_check, in that order
        transcript = self.last_transcript(cid, forge["id"], t["id"])
        names = [m["name"] for m in transcript if m["role"] == "tool"]
        self.assertEqual([n for n in names if n in ("find_tool", "stripe__create_payment_link", "browser_check")],
                         ["find_tool", "stripe__create_payment_link", "browser_check"], names)
        results = {m["name"]: m["content"] for m in transcript if m["role"] == "tool"}
        self.assertTrue(results["find_tool"].splitlines()[0].startswith("stripe__create_payment_link [publish]"),
                        results["find_tool"])
        self.assertIn("Loaded: stripe__create_payment_link", results["find_tool"])
        self.assertIn(f"Created payment link {link}", results["stripe__create_payment_link"])
        check = results["browser_check"]
        self.assertTrue(check.startswith("Static check only: Playwright is not set up"), check)
        self.assertIn(f"GET {link} -> 200", check)
        self.assertIn("Title: Starter plan", check)
        self.assertIn("form found", check)
        # Stripe got a product, a monthly price and a link, once each; the link is live
        self.assertEqual([r["path"] for r in self.stripe.posts()], ["/v1/products", "/v1/prices", "/v1/payment_links"])
        self.assertEqual(self.stripe.posts("/v1/prices")[0]["body"]["recurring"], {"interval": "month"})
        self.assertEqual(self.stripe.posts("/v1/prices")[0]["body"]["unit_amount"], "1900")
        self.assertTrue(self.stripe.links["plink_1"]["active"])
        # audited like any outbound action, approved by the policy
        audit = [r for r in self.api.get(f"/api/audit?company_id={cid}")["audit"] if r["action"] == "connector_call"]
        self.assertTrue(audit, "no connector_call audit row")
        row = audit[0]
        self.assertEqual((row["approved_by"], row["payload"]["tag"], row["payload"]["tool"]),
                         ("policy:auto", "publish", "create_payment_link"))
        self.assertIn(link, row["result"])
        # a test purchase shows up as revenue with nobody typing it
        self.stripe.add_charge(amount=1900, currency="usd", paid=True)

        def revenue():
            rows = self.api.get(f"/api/companies/{cid}/metrics")["metrics"]
            hit = next((r for r in rows if r["name"] == "stripe.revenue_usd_30d"), None)
            return rows if hit and hit["value"] == 19.0 else None
        rows = wait_for(revenue, 30, what="the polled Stripe revenue")
        latest = {}
        for r in rows:   # newest first
            latest.setdefault(r["name"], r)
        rev = latest["stripe.revenue_usd_30d"]
        self.assertEqual((rev["value"], rev["source"], rev["agent_id"]), (19.0, "stripe", None))
        self.assertEqual(latest["stripe.payments_30d"]["value"], 1.0)
        self.assertIn("stripe.customers", latest)
        self.assertEqual(latest["stripe.mrr_usd"]["value"], 0.0)
        self.assertTrue(all(r["source"] == "stripe" for r in rows if r["name"].startswith("stripe.")))
        tile = next(m for m in self.api.get(f"/api/companies/{cid}")["metrics"] if m["name"] == "stripe.revenue_usd_30d")
        self.assertEqual(tile["source"], "stripe")
        # no agent typed a Stripe number in
        for run in self.api.get(f"/api/companies/{cid}/runs")["runs"]:
            for m in self.api.get(f"/api/runs/{run['id']}")["run"]["transcript"]:
                for c in m.get("tool_calls") or []:
                    if c.get("name") == "record_metric":
                        self.assertFalse(str((c.get("arguments") or {}).get("name", "")).startswith("stripe."), c)

    def test_28_resend_and_buffer(self):
        cid = self.cid
        out = self.api.post("/api/connectors", {"key": "resend", "company_id": cid, "enabled": True, "config": {
            "api_key": RESEND_KEY, "from_address": "hello@pawplan.example", "from_name": "PawPlan",
            "api_base": self.resend.url}})
        self.assertEqual(out["health"], {"status": "ok", "detail": "pawplan.example verified"})
        self.assertTrue(self.api.get(f"/api/companies/{cid}")["email_ready"])   # Resend counts without SMTP
        out = self.api.post("/api/connectors", {"key": "buffer", "company_id": cid, "enabled": True, "config": {
            "api_key": self.buffer.token, "api_base": self.buffer.url,
            "default_channels": "chan_x"}})   # approved posts go only to the default channels
        self.assertEqual(out["health"]["status"], "ok", out["health"])
        self.assertTrue(out["instance"]["enabled"])

        def pending(action):
            rows = [a for a in self.pending(cid) if a["action"] == action]
            return rows[0] if rows else None
        email = pending("send_email")
        if email is None:   # the earlier one was decided: Hunter queues another
            hunter = self.agent(cid, "sales")
            self.api.post(f"/api/companies/{cid}/tasks", {"title": "Find 20 more leads", "agent_id": hunter["id"],
                                                          "priority": 1})
            email = wait_for(lambda: pending("send_email"), 60, what="Hunter's email request")
        r = self.api.post(f"/api/approvals/{email['id']}", {"decision": "approve"})["approval"]
        self.assertEqual(r["status"], "done", r)
        self.assertIn("through Resend", r["result"])
        sent = [x for x in self.resend.sent if x["body"]["to"] == email["payload"]["to"]]
        self.assertEqual(len(sent), 1, self.resend.sent)
        self.assertEqual(sent[0]["body"]["subject"], email["payload"]["subject"])
        self.assertTrue(sent[0]["idempotency_key"])
        self.assertIn("Message-ID", sent[0]["body"]["headers"])
        rows = [e for e in self.api.get(f"/api/companies/{cid}/emails")["emails"]
                if e["direction"] == "out" and e["uid"] == sent[0]["id"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], sent[0]["body"]["headers"]["Message-ID"])

        post = pending("social_post")
        if post is None:
            echo = self.agent(cid, "marketing")
            self.api.post(f"/api/companies/{cid}/tasks", {"title": "Write launch post for Buffer",
                                                          "agent_id": echo["id"], "priority": 1})
            post = wait_for(lambda: pending("social_post"), 60, what="Echo's social post request")
        r = self.api.post(f"/api/approvals/{post['id']}", {"decision": "approve"})["approval"]
        self.assertEqual(r["status"], "done", r)
        self.assertIn("Buffer", r["result"])
        self.assertEqual(len(self.buffer.posts), 1, self.buffer.posts)
        self.assertEqual(self.buffer.posts[0]["input"]["channelId"], "chan_x")
        self.assertEqual(self.buffer.posts[0]["input"]["text"], post["payload"]["content"])
        audit: dict = {}
        for row in self.api.get(f"/api/audit?company_id={cid}&limit=100")["audit"]:   # newest first
            audit.setdefault(row["approval_id"], row)
        self.assertIn("through Resend", audit[email["id"]]["result"])
        self.assertEqual((audit[email["id"]]["action"], audit[email["id"]]["approved_by"]), ("send_email", "owner"))
        self.assertEqual((audit[post["id"]]["action"], audit[post["id"]]["approved_by"]), ("social_post", "owner"))
        self.assertNotIn(RESEND_KEY, str(audit))

    def test_29_mcp_oauth_round_trip(self):
        self.assertIn("fakemcp", self.api.get("/api/connectors")["definitions"])
        out = self.api.post("/api/connectors", {"key": "fakemcp", "company_id": None, "enabled": True, "config": {}})
        iid = out["instance"]["id"]
        try:
            self.assertEqual(out["health"]["status"], "bad")   # the server asks for a sign-in first
            self.assertIn("Sign in", out["health"]["detail"])
            url = self.api.post(f"/api/connectors/{iid}/oauth/start")["url"]
            self.assertTrue(url.startswith(self.oauth.url + "/authorize?"), url)
            # the browser's part: the authorization server redirects to the real dashboard's /oauth/callback
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=30) as r:
                final, page = r.geturl(), r.read().decode("utf-8")
            self.assertTrue(final.startswith(f"http://127.0.0.1:{self.app.port}/oauth/callback?"), final)
            self.assertIn("Connected Test MCP server", page)
            ready = wait_for(lambda: (lambda i: i if i and i["status"] == "ok" else None)(self.connector(iid)), 30,
                             what="the signed-in MCP connector to turn ok")
            self.assertTrue(ready["signed_in"])
            self.assertEqual(len(self.oauth.tokens), 1)
            # an agent loads the server's tool with find_tool and calls it with the bearer token
            scout = self.agent(self.cid, "research")
            t = self.api.post(f"/api/companies/{self.cid}/tasks", {"title": "Echo through the test MCP server",
                                                                  "agent_id": scout["id"], "priority": 1})["task"]
            wait_for(lambda: next((x for x in self.tasks(self.cid) if x["id"] == t["id"] and x["status"] == "done"),
                                  None), 60, what="the echo task")
            results = [m for m in self.last_transcript(self.cid, scout["id"], t["id"]) if m["role"] == "tool"]
            found = next(m["content"] for m in results if m["name"] == "find_tool")
            self.assertIn("Loaded: fakemcp__echo", found)
            self.assertEqual(next(m["content"] for m in results if m["name"] == "fakemcp__echo"), "hello from holdco")
            calls = [c for c in self.mcp.calls if c.method == "tools/call"]
            self.assertTrue(calls and calls[-1].params["name"] == "echo", calls)
            self.assertTrue(str(calls[-1].headers.get("authorization", "")).startswith("Bearer fake-at-"))
        finally:
            self.api.delete(f"/api/connectors/{iid}")
        self.assertIsNone(self.connector(iid))


if __name__ == "__main__":
    unittest.main()
