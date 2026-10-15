"""End-to-end test: the real Holdco server, driven over HTTP, with a fake model server.

Covers founding from a template, the 24/7 scheduler, approvals, chat, QA review with the deliverable check,
recurring duties, restart recovery, the prompt-based tool fallback, the OpenAI-compatible backend, pausing,
security checks, deletion, the shared ledger and the connectors (against a fake of every outside service).
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_apis import FakeAPIServer  # noqa: E402
from tests.fake_ollama import FakeModelServer  # noqa: E402
from tests.helpers import HoldcoProcess, wait_for  # noqa: E402


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = FakeModelServer(delay=0.05).start()
        cls.apis = FakeAPIServer().start()
        cls.app = HoldcoProcess(env=cls.apis.env()).start()
        cls.api = cls.app.client
        cls.api.put("/api/settings", {"llm_base_url": cls.model.url, "max_concurrent_runs": 2})

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.model.stop()
        cls.apis.stop()

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
        self.assertTrue({"founding", "run", "tool", "file", "task", "approval", "decision", "done", "ledger"} <= kinds, kinds)
        self.api.delete(f"/api/companies/{self.cid2}", {"confirm": "wrong"}, expect=400)
        self.api.delete(f"/api/companies/{self.cid2}", {"confirm": "BrewBuzz"})
        self.assertNotIn(self.cid2, [c["id"] for c in self.api.get("/api/state")["companies"]])
        self.assertTrue((Path(self.app.data_dir) / "trash").exists())

    # ---------------------------------------------------------------- the ledger
    def ledger(self, book, **qs):
        extra = "".join(f"&{k}={v}" for k, v in qs.items())
        return self.api.get(f"/api/companies/{self.cid}/ledger?book={book}{extra}")["entries"]

    def test_16_ledger_is_the_source_of_truth(self):
        cid = self.cid
        # agents wrote typed entries while working
        facts = {e["key"]: e["data"]["value"] for e in self.ledger("facts")}
        self.assertEqual(facts.get("pricing.monthly_usd"), "9")
        contacts = self.ledger("contacts")
        jo = next(e for e in contacts if e["key"] == "jo@happypaws.example")
        self.assertEqual((jo["data"]["name"], jo["data"]["company"], jo["data"]["stage"]), ("Jo", "Happy Paws", "contacted"))
        money = self.ledger("transactions")
        self.assertTrue(any(e["data"]["counterparty"] == "Namecheap" and e["data"]["amount"] == 12 for e in money), money)
        self.assertEqual(len([e for e in money if e["data"]["counterparty"] == "Namecheap"]), 1)   # recorded once despite retries
        wait_for(lambda: self.api.post(f"/api/companies/{cid}/cycle")["ok"], 30, what="CEO free to plan")
        wait_for(lambda: any(e["data"]["by"] == "Atlas" and "waitlist" in e["data"]["decision"] for e in self.ledger("decisions")),
                 40, what="the CEO's decision in the ledger")
        decisions = self.ledger("decisions")
        self.assertTrue(any(e["data"]["by"] == "owner" and e["data"]["decision"].startswith("Rejected") and
                            e["data"]["why"] == "Fix the form labels first" for e in decisions), decisions)
        handoffs = self.ledger("handoffs")
        built = next(e for e in handoffs if e["data"]["title"] == "Build landing page")
        self.assertEqual((built["data"]["status"], built["data"]["deliverable"]), ("done", "site/index.html"))
        self.assertTrue(all(e["agent_name"] for e in handoffs))
        # summary, search, history, owner edits, void, export, files
        summary = self.api.get(f"/api/companies/{cid}/ledger/summary")["summary"]
        self.assertGreaterEqual(summary["money"]["month_out"], 12)
        self.assertEqual(summary["contacts"].get("contacted"), 1)
        self.assertTrue(any("Namecheap" in e["summary"] for e in self.api.get(f"/api/companies/{cid}/ledger?q=namecheap")["entries"]))
        e = self.api.post(f"/api/companies/{cid}/ledger", {"book": "facts", "key": "pricing.monthly_usd", "value": "12", "source": "owner"})["entry"]
        self.assertEqual((e["action"], e["agent_name"]), ("updated", "You"))
        hist = self.ledger("facts", key="pricing.monthly_usd")
        self.assertEqual([h["data"]["value"] for h in hist], ["12", "9"])
        self.api.post(f"/api/companies/{cid}/ledger", {"book": "money", "date": "2026-13-45", "amount": 5, "category": "software"}, expect=400)
        note = self.api.post(f"/api/companies/{cid}/ledger", {"book": "notes", "content": "Never offer discounts", "pinned": True})["entry"]
        self.assertEqual(note["pinned"], 1)
        self.api.patch(f"/api/ledger/{note['id']}", {"pinned": False})
        self.api.delete(f"/api/ledger/{note['id']}")
        self.assertNotIn(note["id"], [x["id"] for x in self.ledger("notes")])
        self.assertIn(note["id"], [x["id"] for x in self.ledger("notes", history=1)])   # voided entries stay in the history
        status, body, headers = self.api.raw("GET", f"/api/companies/{cid}/ledger/export?book=contacts")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertIn(b"jo@happypaws.example,Jo,Happy Paws", body)
        files = [f["path"] for f in self.api.get(f"/api/companies/{cid}/files")["files"]]
        for path in ("ledger/README.md", "ledger/facts.md", "ledger/contacts.csv", "ledger/transactions.csv", "ledger/handoffs.md", "ledger/decisions.md"):
            self.assertIn(path, files)
        self.assertIn("pricing.monthly_usd", self.api.get(f"/api/companies/{cid}/file?path=ledger/facts.md")["content"])
        # the old memory API still works, backed by the ledger
        mem = self.api.get(f"/api/companies/{cid}/memory")["memory"]
        self.assertTrue(all("content" in m and "kind" in m for m in mem))
        metrics = self.api.get(f"/api/companies/{cid}")["metrics"]
        self.assertIn("waitlist_signups", [m["name"] for m in metrics])

    # ---------------------------------------------------------------- connectors
    def test_17_connectors(self):
        cid = self.cid
        cat = self.api.get("/api/connectors")
        self.assertTrue({"email", "netlify", "stripe", "plausible", "x", "bluesky", "calcom"} <= {c["key"] for c in cat["connectors"]})
        self.assertFalse(self.api.get(f"/api/companies/{cid}/connectors")["status"]["stripe"]["configured"])
        c = self.api.patch(f"/api/companies/{cid}", {"settings": {
            "stripe_secret_key": "rk_test_ok", "plausible_site_id": "pawplan.example", "plausible_api_key": "pl_ok",
            "netlify_token": "nf_ok", "netlify_site_id": "site_1", "x_api_key": "xk", "x_api_secret": "xs",
            "x_access_token": "xt", "x_access_secret": "xts"}})["company"]
        self.assertEqual(c["settings"]["stripe_secret_key"], "••••••••")                 # never shown back
        st = self.api.get(f"/api/companies/{cid}/connectors")
        self.assertTrue(st["status"]["stripe"]["configured"] and st["status"]["x"]["configured"])
        self.assertTrue({"payments", "analytics", "deploy", "forms", "social:x"} <= set(st["capabilities"]))
        r = self.api.post(f"/api/companies/{cid}/connectors/stripe/test")
        self.assertTrue(r["ok"], r)
        self.assertIn("Balance $123.45", r["message"])
        r = self.api.post(f"/api/companies/{cid}/connectors/netlify/test")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.api.get(f"/api/companies/{cid}")["company"]["settings"]["site_url"], "https://pawplan.netlify.app")
        self.api.patch(f"/api/companies/{cid}", {"settings": {"stripe_secret_key": "••••••••"}})   # masked value = unchanged
        self.assertTrue(self.api.post(f"/api/companies/{cid}/connectors/stripe/test")["ok"])
        bad = self.api.post(f"/api/companies/{cid}/connectors/calcom/test")
        self.assertFalse(bad["ok"])
        self.assertIn("Fill in", bad["message"])
        self.api.post(f"/api/companies/{cid}/connectors/nothing/test", expect=404)
        # the finance agent syncs Stripe into the ledger; a social post goes straight to X
        ops = self.agent(cid, "ops")
        t = self.api.post(f"/api/companies/{cid}/tasks", {"title": "Reconcile payments", "agent_id": ops["id"], "priority": 1})["task"]
        wait_for(lambda: next(x for x in self.tasks(cid) if x["id"] == t["id"])["status"] == "done", 60, what="payments task")
        stripe_rows = [e for e in self.ledger("transactions") if e["data"]["provider"] == "stripe"]
        self.assertEqual({e["data"]["external_id"] for e in stripe_rows}, {"txn_1", "txn_1:fee", "txn_2", "txn_2:fee", "txn_4"})
        self.assertIn("revenue_30d_usd", [m["name"] for m in self.api.get(f"/api/companies/{cid}")["metrics"]])
        echo = self.agent(cid, "marketing")
        t = self.api.post(f"/api/companies/{cid}/tasks", {"title": "Draft launch post v3", "agent_id": echo["id"], "priority": 1})["task"]
        wait_for(lambda: next(x for x in self.tasks(cid) if x["id"] == t["id"])["status"] == "done", 60, what="post task")
        post = next(a for a in self.pending(cid) if a["action"] == "social_post")
        r = self.api.post(f"/api/approvals/{post['id']}", {"decision": "approve"})["approval"]
        self.assertEqual(r["status"], "done")
        self.assertIn("Posted on X: https://x.com/i/web/status/1790", r["result"])
        tweet = next(c for c in self.apis.calls if c["path"] == "/x/2/tweets")
        self.assertIn("PawPlan", tweet["body"]["text"])
        # a deploy now really goes to (fake) Netlify
        forge = self.agent(cid, "engineering")
        t = self.api.post(f"/api/companies/{cid}/tasks", {"title": "Republish the site", "agent_id": forge["id"], "priority": 1})["task"]
        wait_for(lambda: next(x for x in self.tasks(cid) if x["id"] == t["id"])["status"] == "done", 60, what="deploy task")
        dep = next(a for a in self.pending(cid) if a["action"] == "deploy_site")
        r = self.api.post(f"/api/approvals/{dep['id']}", {"decision": "approve"})["approval"]
        self.assertEqual((r["status"], r["result"]), ("done", "Website deployed: https://pawplan.netlify.app"))
        # recurring duties that need a connector appear once it is connected
        wait_for(lambda: "ops:Payments sync" in {x["routine"] for x in self.tasks(cid) if x["routine"]}, 30, what="payments routine")

    def test_18_owner_notifications(self):
        self.api.put("/api/settings", {"notify_webhook_url": self.apis.url + "/hooks/slack", "telegram_bot_token": "123:abc",
                                       "telegram_chat_id": "42", "notify_events": ["approvals", "messages"]})
        r = self.api.post("/api/connectors/notifications/test")
        self.assertTrue(r["ok"], r)
        self.assertEqual({x["channel"] for x in r["results"]}, {"webhook", "telegram"})
        self.apis.calls.clear()
        hunter = self.agent(self.cid, "sales")
        t = self.api.post(f"/api/companies/{self.cid}/tasks", {"title": "Email one more lead", "agent_id": hunter["id"], "priority": 1})["task"]
        wait_for(lambda: next(x for x in self.tasks(self.cid) if x["id"] == t["id"])["status"] == "done", 60, what="sales task")
        wait_for(lambda: any(c["path"] == "/hooks/slack" and "needs your OK" in c["body"]["text"] for c in self.apis.calls), 10,
                 what="approval notification")
        self.assertTrue(any(c["path"].startswith("/telegram/bot123:abc/") for c in self.apis.calls))
        self.api.put("/api/settings", {"notify_webhook_url": "", "telegram_bot_token": "", "telegram_chat_id": ""})
        bad = self.api.post("/api/connectors/notifications/test")
        self.assertFalse(bad["ok"])


if __name__ == "__main__":
    unittest.main()
