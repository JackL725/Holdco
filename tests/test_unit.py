"""Unit tests: checks, model-client parsing, presets, workspace safety, tools, approvals, the ledger and connectors.

Run all tests with:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOLDCO_DATA"] = tempfile.mkdtemp(prefix="holdco-unit-")

from holdco import actions, checks, config, connectors, db, ledger, llm, presets, roles, server, tools, web, workspace  # noqa: E402
from tests.fake_apis import TODAY, FakeAPIServer  # noqa: E402

db.init()
FAKE = FakeAPIServer().start()
os.environ.update(FAKE.env())


def set_company_settings(company: dict, **values) -> dict:
    cs = db.company_settings(company)
    cs.update(values)
    db.update("companies", company["id"], settings=json.dumps(cs))
    return db.get_company(company["id"])


def make_company(name="TestCo", members=("ceo", "engineering", "marketing", "sales", "qa")) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "An idea", "active", "building", "{}", db.now()))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def ctx_for(company, role, task=None, kind="task") -> tools.Ctx:
    agent = db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))
    c = tools.Ctx(company=company, agent=agent, kind=kind, run_id=0, task=task, settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(agent, kind)}
    return c


# Built at runtime so secret scanners don't mistake this test fixture for a real key.
FAKE_KEY = "sk" + "_live_" + "x" * 24


class TestChecks(unittest.TestCase):
    def test_contrast(self):
        self.assertAlmostEqual(checks.contrast("#000", "#fff"), 21.0, places=1)
        self.assertAlmostEqual(checks.contrast("white", "white"), 1.0, places=3)
        self.assertLess(checks.contrast("#777777", "#ffffff"), 4.5)        # classic just-fails grey
        self.assertGreaterEqual(checks.contrast("#767676", "#ffffff"), 4.5)  # classic just-passes grey
        self.assertAlmostEqual(checks.contrast("rgb(0,0,0)", "#ffffffcc"), 21.0, places=1)
        with self.assertRaises(ValueError):
            checks.parse_color("not-a-colour")
        self.assertIn("FAIL", checks.contrast_report("#888", "#fff"))

    def _audit(self, html: str, extra: dict | None = None) -> str:
        d = Path(tempfile.mkdtemp()) / "site"
        d.mkdir()
        (d / "index.html").write_text(html, encoding="utf-8")
        for name, content in (extra or {}).items():
            (d / name).write_text(content, encoding="utf-8")
        return checks.audit_page(d / "index.html", d)

    def test_audit_bad_page(self):
        out = self._audit('<html><head><title>Hi</title><style>a{outline:none}</style></head><body>'
                          '<h1>A</h1><h1>B</h1><img src="hero.png" loading="lazy">'
                          '<form><input placeholder="email"><button></button></form>'
                          '<a href="missing.html">More</a><p>Join 10,000 happy customers. Lorem ipsum.</p>'
                          '<script>const key = "' + FAKE_KEY + '";</script></body></html>')
        for needle in ["viewport", "lang attribute", "2 h1", "no alt", "lazy-loaded", "has no label",
                       "go nowhere", "button has no text", "Broken local link", "focus outlines",
                       "10,000 happy customers", "Placeholder text", "secret API key"]:
            self.assertIn(needle, out, needle)

    def test_audit_good_page(self):
        html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>PawPlan: weekly meal plans your dog will actually eat</title>
        <meta name="description" content="PawPlan builds a simple weekly meal plan for your dog from its age, weight and activity, so feeding is easy and consistent every day.">
        <link rel="canonical" href="https://pawplan.example/">
        <meta property="og:title" content="PawPlan"><meta property="og:description" content="Meal plans for dogs">
        <meta property="og:image" content="https://pawplan.example/og.png"><meta property="og:url" content="https://pawplan.example/">
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"PawPlan"}</script>
        </head><body><h1>Meal plans your dog will eat</h1><img src="hero.png" alt="A happy dog" width="800" height="400">
        <form name="waitlist" method="POST" data-netlify="true" netlify-honeypot="bot-field">
        <label for="email">Email</label><input id="email" name="email" type="email"><button>Join the waitlist</button></form>
        <a href="about.html">About PawPlan</a></body></html>"""
        out = self._audit(html, {"hero.png": "x", "about.html": "<p>about</p>", "robots.txt": "Sitemap: x",
                                 "sitemap.xml": "<urlset/>"})
        self.assertIn("0 blocker(s), 0 major, 0 minor", out, out)

    def test_review_text(self):
        long_email = "Hi Sam, " + "this is a revolutionary product that will skyrocket your sales " * 12
        out = checks.review_text(long_email, "cold_email")
        self.assertIn("under 80", out)
        self.assertIn("No opt-out line", out)
        self.assertIn("Hype words", out)
        good = ("Hi Sam, saw your bakery opened a second shop in Leeds. We help bakeries plan weekly specials in "
                "ten minutes. Worth a look? Not relevant? Reply 'no' and I won't follow up.")
        self.assertIn("No issues found", checks.review_text(good, "cold_email"))
        sup = checks.review_text("Sorry for any inconvenience, unfortunately we can give you a refund.", "support_reply")
        self.assertIn("Phrases to avoid", sup)
        self.assertIn("refund", sup)
        self.assertIn("X allows 280", checks.review_text("word " * 80, "social_x"))
        self.assertIn("Numbers found", checks.review_text("We have 5,000 users and grew 40% last month.", "landing"))


class TestModelClient(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(llm.extract_json('text ```json\n{"a": [1, {"b": "}"}]}\n``` more'), {"a": [1, {"b": "}"}]})
        self.assertEqual(llm.extract_json('prefix {"x": 1} suffix'), {"x": 1})
        self.assertIsNone(llm.extract_json("no json here"))

    def test_strip_thinking(self):
        self.assertEqual(llm.strip_thinking("<think>hmm</think>Answer"), "Answer")
        self.assertEqual(llm.strip_thinking("Answer<think>unterminated"), "Answer")

    def test_split_thinking(self):
        self.assertEqual(llm.split_thinking("<think>plan it</think>Answer"), ("Answer", "plan it"))
        self.assertEqual(llm.split_thinking("step one\n</think>Answer"), ("Answer", "step one"))   # template opened the tag
        self.assertEqual(llm.split_thinking("Answer<think>half a thought"), ("Answer", "half a thought"))
        self.assertEqual(llm.split_thinking("plain"), ("plain", ""))

    def test_text_tool_call(self):
        call = llm._text_tool_call('```json\n{"tool": "web_search", "args": {"query": "x"}}\n```', {"web_search"})
        self.assertEqual((call.name, call.arguments), ("web_search", {"query": "x"}))
        self.assertIsNone(llm._text_tool_call('{"tool": "rm_rf", "args": {}}', {"web_search"}))

    def test_sampling(self):
        s = db.get_settings()
        self.assertEqual(llm.sampling_for("gpt-oss:20b", "creative", s), {"temperature": 1.0, "top_p": 1.0})
        self.assertEqual(llm.sampling_for("qwen3:32b", "precise", s)["top_k"], 20)
        self.assertEqual(llm.sampling_for("qwen3:30b-a3b-instruct-2507", "precise", s)["temperature"], 0.7)
        self.assertEqual(llm.sampling_for("llama3.3", "precise", s), {"temperature": 0.3})
        self.assertEqual(llm.sampling_for("gpt-oss:20b", "creative", {**s, "sampling": "manual", "temperature": 0.2}),
                         {"temperature": 0.2})

    def test_message_formats(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
                {"role": "assistant", "content": "", "thinking": "t", "tool_calls": [
                    {"id": "c1", "name": "web_search", "arguments": {"query": "x"}}]},
                {"role": "tool", "name": "web_search", "tool_call_id": "c1", "content": "results"}]
        o = llm._to_openai(msgs)
        self.assertEqual(json.loads(o[2]["tool_calls"][0]["function"]["arguments"]), {"query": "x"})
        self.assertEqual(o[3]["tool_call_id"], "c1")
        self.assertEqual(llm._to_ollama(msgs, keep_thinking=True)[2]["thinking"], "t")
        self.assertNotIn("thinking", llm._to_ollama(msgs)[2])
        j = llm._to_json_mode(msgs, [{"name": "web_search", "description": "d",
                                      "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}])
        self.assertIn("web_search(query?: string)", j[0]["content"])
        self.assertEqual(j[3]["role"], "user")


class TestPresets(unittest.TestCase):
    def test_integrity(self):
        self.assertGreaterEqual(len(presets.PRESETS), 19)
        for key, p in presets.PRESETS.items():
            for field in ("name", "title", "category", "summary", "mission", "done", "workflow", "rules", "kpis",
                          "tools", "routines", "reasoning", "creativity", "sources"):
                self.assertIn(field, p, f"{key} missing {field}")
            self.assertIn(p["category"], presets.CATEGORIES)
            self.assertIn(p["reasoning"], ("low", "medium", "high"))
            self.assertIn(p["creativity"], llm.CREATIVITY_TEMPS)
            self.assertLessEqual(len(p["tools"]), 15, f"{key} has too many tools for a small model")
            unknown = [t for t in p["tools"] if t not in tools.REGISTRY]
            self.assertFalse(unknown, f"{key} uses unknown tools {unknown}")
            self.assertIn("finish", p["tools"])
            self.assertLessEqual(len(p["rules"]), 8, f"{key} has too many rules")
            self.assertLess(len(presets.render(key)), 4500, f"{key} role prompt is too long")
            caps = {cap for c in config.CONNECTORS.values() for cap in c["capabilities"]}
            for r in p["routines"]:
                self.assertGreater(r["every_hours"], 0)
                self.assertIn(r["requires"], caps | {""}, f"{key}: unknown routine requirement {r['requires']}")
            self.assertTrue(all(u.startswith("https://") for u in p["sources"]))

    def test_templates(self):
        self.assertIn(presets.DEFAULT_TEMPLATE, presets.TEMPLATES)
        for key, t in presets.TEMPLATES.items():
            self.assertEqual(t["members"][0], "ceo", key)
            self.assertTrue(all(m in presets.PRESETS for m in t["members"]), key)
            self.assertLessEqual(len(t["members"]), roles.MAX_TEAM)

    def test_old_agents_resolve_to_presets(self):
        for role in ("ceo", "research", "engineering", "marketing", "sales", "support", "ops"):
            self.assertEqual(roles.role_info({"role": role, "preset": ""})["title"], presets.PRESETS[role]["title"])
        self.assertEqual(roles.role_info({"role": "pricing", "preset": ""})["title"], "Specialist")


class TestWorkspaceAndWeb(unittest.TestCase):
    def test_safe_path(self):
        c = make_company("PathCo", ("ceo",))
        self.assertTrue(str(workspace.safe_path(c, "site/index.html")).endswith(os.path.join("site", "index.html")))
        for bad in ("../../etc/passwd", "site/../../x", "C:/Windows/win.ini"):
            with self.assertRaises(workspace.PathError, msg=bad):
                workspace.safe_path(c, bad)

    def test_private_urls_blocked(self):
        for url in ("http://127.0.0.1:8420/api/state", "http://localhost/", "http://192.168.1.1/", "file:///etc/passwd"):
            with self.assertRaises(web.WebError, msg=url):
                web.check_public_url(url)

    def test_html_to_text(self):
        title, text, links = web.html_to_text('<title>T</title><script>x()</script><h1>Big</h1><p>Hi <a href="/a">there</a></p>',
                                              "https://ex.com/")
        self.assertEqual(title, "T")
        self.assertNotIn("x()", text)
        self.assertIn(("there", "https://ex.com/a"), links)

    def test_ddg_parser(self):
        p = web._DDGParser()
        p.feed('<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F&rut=1">Example <b>Site</b></a>'
               '<a class="result__snippet">Snippet text</a>')
        self.assertEqual(p.results[0]["url"], "https://example.com/")
        self.assertEqual(p.results[0]["snippet"].strip(), "Snippet text")


class TestTools(unittest.TestCase):
    def setUp(self):
        self.c = make_company()

    def test_tool_belt_and_aliases(self):
        ctx = ctx_for(self.c, "marketing")
        self.assertIn("no tool called 'run_code'", tools.execute(ctx, "run_code", {"code": "print(1)"}))
        self.assertIn("no tool called", tools.execute(ctx, "apply_patch", {}))
        out = tools.execute(ctx, "write", {"file": "marketing/a.md", "text": "hello world"})  # aliases + arg renames
        self.assertIn("Saved marketing/a.md", out)
        self.assertIn("doesn't exist", tools.execute(ctx, "read_file", {"path": "nope.md"}))
        self.assertIn("outside the company folder", tools.execute(ctx, "read_file", {"path": "../../x"}))

    def test_create_task_dedup(self):
        ctx = ctx_for(self.c, "ceo")
        self.assertIn("Created task", tools.execute(ctx, "create_task", {
            "title": "Build page", "assign_to": "engineering", "deliverable": "site/index.html", "acceptance": "- ok"}))
        self.assertIn("already exists", tools.execute(ctx, "create_task", {"title": "build page", "assign_to": "engineering"}))
        self.assertIn("already produces", tools.execute(ctx, "create_task", {
            "title": "Another page", "assign_to": "engineering", "deliverable": "site/index.html"}))
        self.assertIn("Tip:", tools.execute(ctx, "create_task", {"title": "Vague", "assign_to": "Web Engineer"}))
        self.assertIn("No teammate", tools.execute(ctx, "create_task", {"title": "X", "assign_to": "astronaut"}))

    def test_finish_requires_deliverable(self):
        tid = db.run("INSERT INTO tasks(company_id, title, role, status, deliverable, created_by, created_at, updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (self.c["id"], "Page", "engineering", "in_progress", "site/page.html",
                                                 "test", db.now(), db.now()))
        ctx = ctx_for(self.c, "engineering", db.get_task(tid))
        self.assertIn("Not finished yet", tools.execute(ctx, "finish", {"summary": "done"}))
        self.assertFalse(ctx.finished)
        tools.execute(ctx, "write_file", {"path": "site/page.html", "content": "<html><body>" + "x" * 100 + "</body></html>"})
        tools.execute(ctx, "finish", {"summary": "built it", "evidence": "audit ok"})
        self.assertTrue(ctx.finished)
        self.assertEqual(ctx.finish_status, "done")
        self.assertIn("Evidence: audit ok", ctx.finish_summary)
        # a second lazy finish on another task is downgraded to partial
        tid2 = db.run("INSERT INTO tasks(company_id, title, role, status, deliverable, created_by, created_at, updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?)", (self.c["id"], "P2", "engineering", "in_progress", "site/p2.html",
                                                  "test", db.now(), db.now()))
        ctx2 = ctx_for(self.c, "engineering", db.get_task(tid2))
        tools.execute(ctx2, "finish", {"summary": "done"})
        tools.execute(ctx2, "finish", {"summary": "done"})
        self.assertEqual(ctx2.finish_status, "partial")

    def test_deliverable_written_by_another_task_does_not_count(self):
        mk = lambda title: db.run("INSERT INTO tasks(company_id, title, role, status, deliverable, created_by, created_at, "
                                  "updated_at) VALUES(?,?,?,?,?,?,?,?)", (self.c["id"], title, "engineering", "in_progress",
                                                                          "site/shared.html", "test", db.now(), db.now()))
        fix, other = mk("Fix it"), mk("Something else")
        other_ctx = ctx_for(self.c, "engineering", db.get_task(other))
        tools.execute(other_ctx, "write_file", {"path": "site/shared.html", "content": "<p>" + "y" * 100 + "</p>"})
        fix_ctx = ctx_for(self.c, "engineering", db.get_task(fix))
        self.assertIn("wasn't created or changed while working on this task",
                      tools.execute(fix_ctx, "finish", {"summary": "done"}))
        # an earlier attempt at the same task does count
        retry_ctx = ctx_for(self.c, "engineering", db.get_task(other))
        tools.execute(retry_ctx, "finish", {"summary": "done"})
        self.assertEqual(retry_ctx.finish_status, "done")

    def test_hire_agent(self):
        ctx = ctx_for(self.c, "ceo")
        self.assertIn("Hired Quill", tools.execute(ctx, "hire_agent", {"preset": "seo"}))
        self.assertIn("Hired", tools.execute(ctx, "hire_agent", {"preset": "seo"}))  # second copy gets seo_2
        self.assertTrue(db.one("SELECT id FROM agents WHERE company_id=? AND role='seo_2'", (self.c["id"],)))
        self.assertIn("Unknown preset", tools.execute(ctx, "hire_agent", {"preset": "wizard"}))

    def test_checks_as_tools(self):
        ctx = ctx_for(self.c, "qa")
        workspace.safe_path(self.c, "site/index.html").write_text("<html><body><h1>x</h1></body></html>", encoding="utf-8")
        self.assertIn("BLOCKER", tools.execute(ctx, "audit_page", {"path": "site/index.html"}))
        self.assertIn("21.00:1", tools.execute(ctx, "check_contrast", {"pairs": "#000000 #ffffff body text"}))
        self.assertIn("reading grade", tools.execute(ctx, "review_text", {"text": "Short and clear.", "kind": "landing"}))

    def test_approvals_flow(self):
        ctx = ctx_for(self.c, "marketing")
        out = tools.execute(ctx, "social_post", {"platform": "x", "content": "Hello world"})
        self.assertIn("Queued for the owner's approval", out)
        self.assertIn("Already waiting", tools.execute(ctx, "social_post", {"platform": "x", "content": "Hello world"}))
        aid = db.scalar("SELECT MAX(id) FROM approvals WHERE company_id=?", (self.c["id"],))
        row = actions.decide(aid, "approve", payload={"content": "Hello edited"})
        self.assertEqual(row["status"], "done")
        self.assertIn("twitter.com/intent/tweet", row["result"])
        self.assertIn("Hello edited", row["payload"])
        # rejection with feedback creates a revision task for the same agent
        tools.execute(ctx, "social_post", {"platform": "x", "content": "Buy now!!!"})
        aid = db.scalar("SELECT MAX(id) FROM approvals WHERE company_id=?", (self.c["id"],))
        actions.decide(aid, "reject", feedback="Too pushy")
        t = db.one("SELECT * FROM tasks WHERE company_id=? AND title LIKE 'Revise:%' ORDER BY id DESC", (self.c["id"],))
        self.assertIsNotNone(t)
        self.assertIn("Too pushy", t["description"])
        # an email with no SMTP set fails and can be retried
        sales = ctx_for(self.c, "sales")
        tools.execute(sales, "send_email", {"to": "a@b.co", "subject": "Hi", "body": "Hello"})
        aid = db.scalar("SELECT MAX(id) FROM approvals WHERE company_id=?", (self.c["id"],))
        self.assertEqual(actions.decide(aid, "approve")["status"], "failed")
        self.assertEqual(actions.retry(aid)["status"], "pending")
        self.assertIn("valid email", tools.execute(sales, "send_email", {"to": "nope", "subject": "x", "body": "y"}))

    def test_purchase_is_always_approval(self):
        s = db.company_settings(self.c)
        s["policies"] = {"spend_money": "auto"}
        db.update("companies", self.c["id"], settings=json.dumps(s))
        self.assertEqual(db.effective_policy(db.get_company(self.c["id"]), "spend_money"), "approve")


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.c = make_company("LedgerCo", ("ceo", "sales", "ops"))
        self.cid = self.c["id"]
        self.ceo = db.one("SELECT * FROM agents WHERE company_id=? AND role='ceo'", (self.cid,))

    def test_notes_and_search(self):
        a = ledger.note(self.cid, "Refund policy is 30 days", "fact", pinned=True, agent_id=self.ceo["id"])
        ledger.note(self.cid, "Cold emails over 80 words get fewer replies", "lesson", agent_id=self.ceo["id"])
        self.assertEqual(a["action"], "created")
        cur = ledger.current(self.cid, "notes")
        self.assertEqual([e["pinned"] for e in cur], [1, 0])               # pinned first
        hits = ledger.search(self.cid, "refund policy")
        self.assertEqual(hits[0]["id"], a["id"])
        self.assertIn("Refund policy", ledger.text(self.cid, query="refund"))
        ledger.void(a["id"])
        self.assertFalse(ledger.search(self.cid, "refund"))
        self.assertEqual(ledger.count(self.cid, "notes"), 1)

    def test_facts_supersede_and_keep_history(self):
        first = ledger.fact(self.cid, "Pricing.Pro USD", 29, "charter", self.ceo["id"])
        self.assertEqual(first["key"], "pricing.pro_usd")
        again = ledger.fact(self.cid, "pricing.pro_usd", 29, "charter", self.ceo["id"])
        self.assertEqual(again["action"], "unchanged")
        changed = ledger.fact(self.cid, "pricing.pro_usd", 39, "owner", self.ceo["id"])
        self.assertEqual((changed["action"], changed["supersedes"]), ("updated", first["id"]))
        cur = ledger.current(self.cid, "facts")
        self.assertEqual(len(cur), 1)
        self.assertEqual(cur[0]["data"]["value"], 39)
        hist = ledger.history(self.cid, "facts", "pricing.pro_usd")
        self.assertEqual([h["data"]["value"] for h in hist], [39, 29])
        self.assertIn("pricing.pro_usd = 39", ledger.text(self.cid, "facts"))
        self.assertIn("= 29", ledger.text(self.cid, "facts", key="pricing.pro_usd"))
        with self.assertRaises(ledger.LedgerError):
            ledger.fact(self.cid, "", "x")

    def test_decisions(self):
        d = ledger.decision(self.cid, "Pricing: one plan", "Charge $29/month", why="Simpler", options="Free tier (rejected)",
                            reversible=True, agent_id=self.ceo["id"])
        self.assertEqual(d["key"], "pricing-one-plan")
        self.assertEqual(d["data"]["by"], "Atlas")
        owner = ledger.decision(self.cid, "Send email: Hi Jo", "Approved", why="Looks good", by="owner", source="owner")
        self.assertIsNone(owner["agent_id"])
        txt = ledger.text(self.cid, "decisions")
        self.assertIn("Charge $29/month", txt)
        self.assertIn("by owner", txt)

    def test_contacts_merge_and_stages(self):
        a = ledger.contact(self.cid, "JO@HappyPaws.example", "Jo", "Happy Paws", stage="new", source_note="https://happypaws.example/team")
        self.assertEqual(a["key"], "jo@happypaws.example")
        b = ledger.contact(self.cid, "jo@happypaws.example", stage="contacted", notes="Sent intro email")
        self.assertEqual(b["action"], "updated")
        self.assertEqual(b["data"]["name"], "Jo")                          # merged, not lost
        self.assertEqual(b["data"]["touches"], 1)
        self.assertEqual(len(ledger.current(self.cid, "contacts")), 1)
        with self.assertRaises(ledger.LedgerError):
            ledger.contact(self.cid, "not-an-email", "X")
        with self.assertRaises(ledger.LedgerError):
            ledger.contact(self.cid, "a@b.co", stage="astronaut")
        dnc = ledger.contact(self.cid, "a@b.co", "Sam", stage="unsubscribed")      # aliases map onto the pipeline
        self.assertTrue(dnc["data"]["do_not_contact"])
        self.assertEqual(ledger.contact(self.cid, "w@b.co", "Win", stage="closed won")["data"]["stage"], "customer")
        ledger.void(ledger.current(self.cid, "contacts", "w@b.co")[0]["id"])
        self.assertEqual(ledger.contact_counts(self.cid), {"contacted": 1, "do_not_contact": 1})
        self.assertIn("Contacts by stage", ledger.text(self.cid, "contacts"))

    def test_transactions_money_and_dedupe(self):
        t = ledger.transaction(self.cid, TODAY, 12, "USD", "", "domain name", "Namecheap", "pawplan.co", agent_id=self.ceo["id"])
        self.assertEqual((t["data"]["category"], t["data"]["direction"]), ("software", "out"))
        self.assertEqual(ledger.transaction(self.cid, TODAY, 12, "USD", "", "domain name", "Namecheap", "pawplan.co")["action"], "unchanged")
        ledger.transaction(self.cid, TODAY, 29, "usd", "in", "Revenue", "", "First sale", "ch_1", "stripe")
        ledger.transaction(self.cid, TODAY, 29, "usd", "in", "Revenue", "", "First sale", "ch_1", "stripe")   # idempotent sync
        ledger.transaction(self.cid, "2020-01-05", 100, "USD", "in", "owner contribution", "Owner", "seed money")
        self.assertEqual(ledger.count(self.cid, "transactions"), 3)
        m = ledger.money_summary(self.cid)
        self.assertEqual((m["month_in"], m["month_out"], m["cash"]), (29.0, 12.0, 117.0))
        self.assertEqual(m["by_category"]["revenue"]["in"], 29.0)
        self.assertEqual(ledger.normalize_category("Stripe processing fee"), "fees")
        self.assertEqual(ledger.normalize_category("refund"), "refunds")
        self.assertEqual(ledger.normalize_category("???"), "other")
        with self.assertRaises(ledger.LedgerError):
            ledger.transaction(self.cid, "yesterday", 5, category="software")
        with self.assertRaises(ledger.LedgerError):
            ledger.transaction(self.cid, TODAY, 0, category="software")
        self.assertIn("cash $117.00", ledger.text(self.cid, "money"))

    def test_metrics_readings(self):
        ledger.metric(self.cid, "Visitors 7d", 100, "plausible", agent_id=self.ceo["id"])
        self.assertEqual(ledger.metric(self.cid, "visitors_7d", 100, "plausible")["action"], "unchanged")  # same reading, same day
        ledger.metric(self.cid, "visitors_7d", 140, "plausible")
        latest = ledger.latest_metrics(self.cid)
        self.assertEqual([(r["name"], r["value"]) for r in latest], [("visitors_7d", 140.0)])
        self.assertEqual(len(ledger.history(self.cid, "metrics", "visitors_7d")), 2)
        self.assertEqual(len(ledger.metric_rows(self.cid)), 2)

    def test_handoffs_and_overview(self):
        tid = db.run("INSERT INTO tasks(company_id, title, role, status, deliverable, created_by, created_at, updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (self.cid, "Build page", "ops", "done", "site/index.html", "t", db.now(), db.now()))
        agent = db.one("SELECT * FROM agents WHERE company_id=? AND role='ops'", (self.cid,))
        h = ledger.handoff(self.cid, db.get_task(tid), agent, "done", "Built it\nTwo lines", "site/index.html", "audit ok", "", "deploy")
        self.assertEqual(h["key"], f"task:{tid}")
        ledger.note(self.cid, "Always sign as the team", "owner", pinned=True)
        ledger.fact(self.cid, "site.url", "https://pawplan.example", "netlify")
        over = ledger.overview_text(self.cid)
        for needle in ("Pinned:", "Always sign", "Facts:", "site.url", "1 task handoffs"):
            self.assertIn(needle, over)
        self.assertIn("Task #", ledger.text(self.cid, "handoffs"))
        self.assertIn("Built it", ledger.text(self.cid, "handoffs", key=str(tid)))

    def test_export_and_files(self):
        ledger.contact(self.cid, "jo@happypaws.example", "Jo", "Happy Paws", stage="qualified")
        ledger.transaction(self.cid, TODAY, 5.5, "USD", "out", "software", "Vercel", "hosting")
        ledger.fact(self.cid, "policy.refund_days", 30, "owner")
        ledger.decision(self.cid, "Hosting", "Use Vercel", why="free tier")
        name, csv_text = ledger.export(self.cid, "contacts")
        self.assertEqual(name, "contacts.csv")
        self.assertIn("email,name,company", csv_text.splitlines()[0])
        self.assertIn("jo@happypaws.example,Jo,Happy Paws", csv_text)
        _, tx = ledger.export(self.cid, "money")
        self.assertIn(f"{TODAY},5.5,USD,out,software,Vercel,hosting", tx)
        _, facts = ledger.export(self.cid, "facts")
        self.assertIn("| policy.refund_days | 30 |", facts)
        _, dec = ledger.export(self.cid, "decisions")
        self.assertIn("**Decision:** Use Vercel", dec)
        for book in ledger.BOOKS:
            rel_path = ledger.render(self.c, book)
            self.assertTrue(workspace.safe_path(self.c, rel_path).is_file(), rel_path)
        self.assertTrue(workspace.safe_path(self.c, "ledger/README.md").is_file())
        with self.assertRaises(ledger.LedgerError):
            ledger.export(self.cid, "diary")

    def test_tools_write_to_the_ledger(self):
        ctx = ctx_for(self.c, "ceo")
        self.assertIn("decisions #", tools.execute(ctx, "remember", {"content": "Focus on cafés first. The rest can wait.", "kind": "decision"}))
        self.assertIn("Saved: pricing.pro_usd = 29", tools.execute(ctx, "record_fact", {"key": "pricing.pro_usd", "value": "29", "source": "charter"}))
        self.assertIn("Recorded decision", tools.execute(ctx, "record_decision", {"title": "Name", "decision": "PawPlan", "why": "short"}))
        self.assertIn("ledger/facts.md", ctx.written)
        sales = ctx_for(self.c, "sales")
        self.assertIn("Added: Jo", tools.execute(sales, "save_contact", {"email": "jo@happypaws.example", "name": "Jo", "stage": "contacted"}))
        self.assertIn("Updated", tools.execute(sales, "save_contact", {"email": "jo@happypaws.example", "stage": "replied", "notes": "Interested"}))
        self.assertIn("no tool called 'record_transaction'", tools.execute(sales, "record_transaction", {"date": TODAY, "amount": 1, "category": "software"}))
        ops = ctx_for(self.c, "ops")
        self.assertIn("Recorded in the money book", tools.execute(ops, "record_transaction", {"date": TODAY, "amount": 12, "category": "software", "counterparty": "Namecheap"}))
        self.assertIn("already in the ledger", tools.execute(ops, "record_transaction", {"date": TODAY, "amount": 12, "category": "software", "counterparty": "Namecheap"}))
        self.assertIn("ERROR: date must be", tools.execute(ops, "record_transaction", {"date": "soon", "amount": 12, "category": "software"}))
        out = tools.execute(ops, "recall", {"book": "money"})
        self.assertIn("out $12.00", out)
        self.assertIn("Namecheap", out)
        self.assertIn("Jo", tools.execute(ops, "recall", {"query": "Jo"}))
        self.assertIn("pricing.pro_usd", tools.execute(ops, "recall", {}))
        self.assertIn("Unknown book", tools.execute(ops, "recall", {"book": "diary"}))

    def test_owner_decisions_land_in_the_ledger(self):
        ctx = ctx_for(self.c, "sales")
        tools.execute(ctx, "send_email", {"to": "jo@happypaws.example", "subject": "Hi", "body": "Hello"})
        aid = db.scalar("SELECT MAX(id) FROM approvals WHERE company_id=?", (self.cid,))
        actions.decide(aid, "reject", feedback="Too long")
        dec = ledger.current(self.cid, "decisions")
        self.assertEqual(dec[0]["data"]["by"], "owner")
        self.assertIn("Rejected", dec[0]["data"]["decision"])
        self.assertEqual(dec[0]["data"]["why"], "Too long")
        tools.execute(ctx_for(self.c, "ceo"), "ask_owner", {"question": "Which city first?"})
        aid = db.scalar("SELECT MAX(id) FROM approvals WHERE company_id=?", (self.cid,))
        actions.decide(aid, "approve", answer="Leeds")
        self.assertIn("Leeds", ledger.current(self.cid, "decisions")[0]["summary"])

    def test_upgrade_migrates_old_memory_and_metrics(self):
        """A database from before the ledger existed is brought over on first start."""
        import sqlite3
        import subprocess
        data = Path(tempfile.mkdtemp(prefix="holdco-upgrade-"))
        con = sqlite3.connect(str(data / "holdco.db"))
        con.executescript(db.SCHEMA)
        con.execute("INSERT INTO companies(id, name, slug, idea, status, created_at) VALUES(1, 'Old', 'old', 'x', 'active', '2026-01-01T00:00:00Z')")
        con.execute("INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(1, NULL, 'owner', 'Never discount', 1, '2026-01-02T00:00:00Z')")
        con.execute("INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(1, NULL, 'lesson', 'Short emails win', 0, '2026-01-03T00:00:00Z')")
        con.execute("INSERT INTO metrics(company_id, agent_id, name, value, note, created_at) VALUES(1, NULL, 'signups', 3, '', '2026-01-02T00:00:00Z')")
        con.execute("INSERT INTO metrics(company_id, agent_id, name, value, note, created_at) VALUES(1, NULL, 'signups', 7, '', '2026-01-09T00:00:00Z')")
        con.commit()
        con.close()
        code = ("from holdco import db, ledger; db.init(); import json; "
                "print(json.dumps({'notes': [(e['data']['content'], e['pinned']) for e in ledger.current(1, 'notes')], "
                "'metrics': [(r['name'], r['value']) for r in ledger.latest_metrics(1)], "
                "'history': len(ledger.history(1, 'metrics', 'signups'))}))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env={**os.environ, "HOLDCO_DATA": str(data)},
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got, {"notes": [["Never discount", 1], ["Short emails win", 0]], "metrics": [["signups", 7.0]], "history": 2})


class TestConnectors(unittest.TestCase):
    def setUp(self):
        self.c = make_company("ConnCo", ("ceo", "ops", "marketing", "sales", "analyst"))
        self.cid = self.c["id"]
        FAKE.calls.clear()

    def test_catalogue_and_capabilities(self):
        cs = db.company_settings(self.c)
        self.assertEqual(connectors.capabilities(cs), set())
        self.assertTrue(connectors.has(cs, ""))
        self.assertFalse(connectors.has(cs, "payments"))
        cs.update(stripe_secret_key="rk_test_ok", netlify_token="nf_ok", smtp_host="h", smtp_user="u", smtp_password="p")
        caps = connectors.capabilities(cs)
        self.assertEqual(caps, {"payments", "deploy", "smtp"})      # forms needs a site id, imap needs imap fields
        self.assertEqual(connectors.provider_for(cs, "payments"), "stripe")
        cs["netlify_site_id"] = "site_1"
        self.assertIn("forms", connectors.capabilities(cs))
        self.assertTrue(connectors.status(cs)["stripe"]["configured"])
        cat = connectors.catalogue()
        self.assertIn("stripe", [c["key"] for c in cat["connectors"]])
        self.assertTrue(all(c["category"] in cat["categories"] for c in cat["connectors"]))
        for key in config.SECRET_COMPANY_KEYS:
            self.assertIn(key, config.DEFAULT_COMPANY_SETTINGS)
        self.assertIn("stripe_secret_key", config.SECRET_COMPANY_KEYS)
        self.assertIn("smtp_password", config.SECRET_COMPANY_KEYS)
        c = set_company_settings(self.c, stripe_secret_key="rk_test_ok")
        self.assertEqual(server._public_company(c)["settings"]["stripe_secret_key"], "••••••••")

    def test_stripe(self):
        cs = {"stripe_secret_key": "rk_test_ok"}
        s = connectors.stripe_summary(cs, 30)
        self.assertEqual((s["currency"], s["balance_available"], s["balance_pending"]), ("USD", 123.45, 5.0))
        self.assertEqual((s["sales_count"], s["gross"], s["refunds"]), (1, 29.0, 9.0))
        self.assertAlmostEqual(s["fees"], 1.14 - 0.56 + 2.0)
        self.assertEqual(s["subscriptions_active"], 2)
        self.assertAlmostEqual(s["mrr"], 29 + 2 * 240 / 12)
        rows = connectors.stripe_transactions(cs, 30)
        ids = [r["external_id"] for r in rows]
        self.assertEqual(ids, ["txn_1", "txn_1:fee", "txn_2", "txn_2:fee", "txn_4"])   # payouts are skipped
        self.assertEqual([r["direction"] for r in rows], ["in", "out", "out", "in", "out"])
        with self.assertRaises(connectors.ConnectorError) as cm:
            connectors.stripe_summary({"stripe_secret_key": "bad"}, 30)
        self.assertIn("HTTP 401", str(cm.exception))
        self.assertIn("Invalid API Key", str(cm.exception))
        self.assertIn("Balance", connectors.test("stripe", cs)[0])

    def test_lemonsqueezy_and_gumroad(self):
        cs = {"lemonsqueezy_api_key": "ls_ok", "lemonsqueezy_store_id": "9"}
        rows = connectors.lemonsqueezy_transactions(cs, 30)
        self.assertEqual([(r["external_id"], r["direction"]) for r in rows], [("101", "in"), ("102", "in"), ("102:refund", "out")])
        self.assertEqual(FAKE.calls[0]["query"]["filter[store_id]"], ["9"])
        self.assertEqual(FAKE.calls[0]["headers"]["accept"], "application/vnd.api+json")
        s = connectors.lemonsqueezy_summary(cs, 30)
        self.assertEqual((s["sales_count"], s["gross"], s["refunds"], s["subscriptions_active"]), (2, 38.0, 19.0, 1))
        self.assertAlmostEqual(s["mrr"], 19.0)
        self.assertIn("Jack", connectors.test("lemonsqueezy", cs)[0])
        g = connectors.gumroad_summary({"gumroad_access_token": "gr_ok"}, 30)
        self.assertEqual((g["gross"], g["fees"], g["subscriptions_active"], g["mrr"]), (15.0, 1.5, 1, 15.0))
        with self.assertRaises(connectors.ConnectorError):
            connectors.gumroad_summary({"gumroad_access_token": "nope"}, 30)
        text = connectors.payments_text(g)
        self.assertIn("MRR about $15.00", text)

    def test_analytics(self):
        cs = {"plausible_site_id": "pawplan.example", "plausible_api_key": "pl_ok", "plausible_url": ""}
        s = connectors.plausible_stats(cs, "7d")
        self.assertEqual((s["visitors"], s["visits"], s["pageviews"], s["bounce_rate"]), (340, 400, 900, 45.0))
        self.assertEqual(s["top_pages"][0], {"name": "/", "visitors": 120})
        self.assertEqual(FAKE.calls[0]["body"]["date_range"], "7d")
        self.assertIn("340 visitors", connectors.stats_text(s))
        self.assertIn("Top pages: / (120)", connectors.stats_text(s))
        g = connectors.goatcounter_stats({"goatcounter_url": FAKE.url + "/goatcounter", "goatcounter_api_key": "gc_ok"}, "30d")
        self.assertEqual((g["visitors"], g["pageviews"]), (210, 300))
        self.assertEqual(g["top_sources"][0]["name"], "news.ycombinator.com")
        self.assertEqual(connectors.site_stats(cs, "30d")["period"], "30d")
        with self.assertRaises(connectors.ConnectorError):
            connectors.site_stats({}, "7d")

    def test_netlify_forms(self):
        cs = {"netlify_token": "nf_ok", "netlify_site_id": "site_1"}
        subs = connectors.netlify_submissions(cs, 50)
        self.assertEqual([s["email"] for s in subs], ["jo@happypaws.example", "sam@example.com"])   # email found inside data too
        self.assertEqual(subs[0]["fields"], {"dog": "Rex"})
        self.assertEqual(FAKE.calls[0]["query"]["per_page"], ["50"])
        msg, updates = connectors.test("netlify", cs)
        self.assertIn("pawplan.netlify.app", msg)
        self.assertEqual(updates["site_url"], "https://pawplan.netlify.app")
        with self.assertRaises(connectors.ConnectorError):
            connectors.netlify_submissions({"netlify_token": "nf_ok"}, 10)

    def test_oauth1_signature_matches_the_reference_vector(self):
        # The worked example from the OAuth 1.0a / X developer documentation.
        header = connectors._oauth1_header(
            "POST", "https://api.twitter.com/1.1/statuses/update.json",
            {"status": "Hello Ladies + Gentlemen, a signed OAuth request!", "include_entities": "true"},
            "xvz1evFS4wEEPTGEFPHBog", "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
            "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
            nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", timestamp="1318622958")
        self.assertIn('oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"', header)

    def test_social_posting(self):
        x = {"x_api_key": "xk", "x_api_secret": "xs", "x_access_token": "xt", "x_access_secret": "xts"}
        self.assertEqual(connectors.x_post(x, "Hello", "https://pawplan.example"), "Posted on X: https://x.com/i/web/status/1790")
        self.assertEqual(FAKE.calls[-1]["body"], {"text": "Hello\nhttps://pawplan.example"})
        self.assertIn("@pawplan", connectors.test("x", x)[0])
        with self.assertRaises(connectors.ConnectorError):
            connectors.x_post(x, "x" * 281)
        bs = {"bluesky_handle": "@pawplan.bsky.social", "bluesky_app_password": "app-pass", "bluesky_url": ""}
        self.assertIn("bsky.app/profile/pawplan.bsky.social/post/3k2abc", connectors.bluesky_post(bs, "Meet PawPlan https://pawplan.example"))
        rec = FAKE.calls[-1]["body"]["record"]
        self.assertEqual(rec["facets"][0]["features"][0]["uri"], "https://pawplan.example")
        self.assertEqual(rec["facets"][0]["index"], {"byteStart": 13, "byteEnd": 36})
        self.assertEqual(connectors._bluesky_facets("café https://x.y")[0]["index"]["byteStart"], 6)   # byte offsets, not chars
        with self.assertRaises(connectors.ConnectorError):
            connectors.bluesky_post({**bs, "bluesky_app_password": "wrong"}, "hi")
        md = {"mastodon_url": FAKE.url + "/mastodon", "mastodon_access_token": "md_ok"}
        self.assertIn("mastodon.example/@pawplan/1", connectors.mastodon_post(md, "Hello fediverse"))
        self.assertEqual(FAKE.calls[-1]["body"], "status=Hello+fediverse")
        li = {"linkedin_access_token": "li_ok", "linkedin_author_urn": ""}
        self.assertIn("urn:li:share:999", connectors.linkedin_post(li, "Launching (today) #pawplan"))
        self.assertEqual(FAKE.calls[-1]["body"]["commentary"], "Launching \\(today\\) \\#pawplan")
        self.assertEqual(FAKE.calls[-1]["body"]["author"], "urn:li:person:abc123")
        self.assertEqual(FAKE.calls[-1]["headers"]["linkedin-version"], connectors.LINKEDIN_VERSION)
        self.assertEqual(connectors.test("linkedin", li)[1], {"linkedin_author_urn": "urn:li:person:abc123"})
        wh = {"webhook_url": FAKE.url + "/hooks/generic"}
        self.assertEqual(connectors.social_provider(wh, "instagram"), "webhook")
        self.assertEqual(connectors.social_provider({**wh, **x}, "twitter"), "x")
        self.assertIsNone(connectors.social_provider({}, "x"))
        self.assertIn("example.com/posted/1", connectors.social_post(wh, "PawPlan", "instagram", "Hi", ""))
        self.assertEqual(FAKE.calls[-1]["body"]["platform"], "instagram")

    def test_social_post_action_uses_the_connector(self):
        c = set_company_settings(self.c, bluesky_handle="pawplan.bsky.social", bluesky_app_password="app-pass")
        ctx = ctx_for(c, "marketing")
        tools.execute(ctx, "social_post", {"platform": "bluesky", "content": "Meet PawPlan"})
        aid = db.scalar("SELECT MAX(id) FROM approvals WHERE company_id=?", (c["id"],))
        row = actions.decide(aid, "approve")
        self.assertEqual(row["status"], "done")
        self.assertIn("Posted on Bluesky", row["result"])
        tools.execute(ctx, "social_post", {"platform": "x", "content": "No X connector here"})
        aid = db.scalar("SELECT MAX(id) FROM approvals WHERE company_id=?", (c["id"],))
        self.assertIn("One-click X link", actions.decide(aid, "approve")["result"])

    def test_calcom_and_domains(self):
        cs = {"calcom_api_key": "cal_ok", "calcom_url": ""}
        b = connectors.calcom_bookings(cs)
        self.assertEqual((b[0]["title"], b[0]["attendees"]), ("Intro call", ["Jo <jo@happypaws.example>"]))
        self.assertEqual(FAKE.calls[0]["headers"]["cal-api-version"], connectors.CALCOM_VERSION)
        self.assertIn("jack", connectors.test("calcom", cs)[0])
        r = connectors.domain_check("PawPlan.com")
        self.assertEqual((r["registered"], r["registrar"], r["expires"]), (True, "Namecheap", "2027-01-02"))
        self.assertFalse(connectors.domain_check("https://pawplan.xyz/")["registered"])
        self.assertIsNone(connectors.domain_check("pawplan.unknown")["registered"])
        with self.assertRaises(connectors.ConnectorError):
            connectors.domain_check("not a domain")

    def test_notifications(self):
        s = {"notify_webhook_url": FAKE.url + "/hooks/slack", "telegram_bot_token": "123:abc", "telegram_chat_id": "42",
             "ntfy_topic": "holdco-test", "notify_events": ["approvals"]}
        self.assertEqual(connectors.notify_targets(s), ["webhook", "telegram", "ntfy"])
        res = connectors.deliver(s, "PawPlan needs your OK", "Email to jo@happypaws.example: Hi")
        self.assertTrue(all(r["ok"] for r in res), res)
        slack = next(c for c in FAKE.calls if c["path"] == "/hooks/slack")
        self.assertIn("*PawPlan needs your OK*", slack["body"]["text"])
        tg = next(c for c in FAKE.calls if c["path"].startswith("/telegram/"))
        self.assertEqual(tg["body"]["chat_id"], "42")
        nt = next(c for c in FAKE.calls if c["path"] == "/ntfy/holdco-test")
        self.assertEqual(nt["headers"]["title"], "PawPlan needs your OK")
        res = connectors.deliver({"notify_webhook_url": FAKE.url + "/hooks/discord"}, "T", "x")
        self.assertTrue(res[0]["ok"])
        self.assertIn("content", next(c for c in FAKE.calls if c["path"] == "/hooks/discord")["body"])
        res = connectors.deliver({"notify_webhook_url": FAKE.url + "/hooks/fail"}, "T", "x")
        self.assertFalse(res[0]["ok"])
        self.assertIn("HTTP 500", res[0]["message"])
        # an approval request pings the owner (only for the events chosen)
        db.set_settings({"notify_webhook_url": FAKE.url + "/hooks/generic", "notify_events": ["approvals"]})
        try:
            FAKE.calls.clear()
            ctx = ctx_for(self.c, "marketing")
            tools.execute(ctx, "social_post", {"platform": "x", "content": "Ping test"})
            deadline = time.time() + 5
            while time.time() < deadline and not any(c["path"] == "/hooks/generic" for c in FAKE.calls):
                time.sleep(0.05)
            hook = next(c for c in FAKE.calls if c["path"] == "/hooks/generic")
            self.assertIn("needs your OK", hook["body"]["title"])
            FAKE.calls.clear()
            tools.execute(ctx, "report_to_owner", {"message": "Weekly numbers are in."})
            time.sleep(0.3)
            self.assertFalse(any(c["path"] == "/hooks/generic" for c in FAKE.calls))   # messages not selected
        finally:
            db.set_settings({"notify_webhook_url": "", "notify_events": ["approvals", "messages", "errors"]})

    def test_connector_tools(self):
        c = set_company_settings(self.c, stripe_secret_key="rk_test_ok", plausible_site_id="pawplan.example", plausible_api_key="pl_ok",
                                 netlify_token="nf_ok", netlify_site_id="site_1", calcom_api_key="cal_ok")
        ops = ctx_for(c, "ops")
        out = tools.execute(ops, "sync_payments", {"days": 30})
        self.assertIn("5 new", out)
        self.assertIn("revenue 29.00 USD", out)
        self.assertIn("0 new, 5 already", tools.execute(ops, "sync_payments", {"days": 30}))
        self.assertEqual(ledger.count(c["id"], "transactions"), 5)
        self.assertEqual(ledger.money_summary(c["id"])["by_category"]["fees"]["out"], 3.14)
        self.assertIn("ledger/transactions.csv", ops.written)
        self.assertIn("MRR about $69.00", tools.execute(ops, "payments_summary", {}))
        names = {r["name"]: r["value"] for r in ledger.latest_metrics(c["id"])}
        self.assertEqual((names["mrr_usd"], names["balance_usd"], names["revenue_30d_usd"]), (69.0, 123.45, 29.0))
        analyst = ctx_for(c, "analyst")
        self.assertIn("340 visitors", tools.execute(analyst, "site_stats", {"period": "7d"}))
        self.assertEqual({r["name"]: r["value"] for r in ledger.latest_metrics(c["id"])}["visitors_7d"], 340.0)
        out = tools.execute(analyst, "list_signups", {})
        self.assertIn("2 new contacts", out)
        self.assertIn("jo@happypaws.example", out)
        self.assertEqual(ledger.contact_counts(c["id"]), {"new": 2})
        self.assertIn("0 new contacts", tools.execute(analyst, "list_signups", {}))
        self.assertIn("registered via Namecheap", tools.execute(ctx_for(c, "ceo", kind="founding"), "check_domain", {"domain": "pawplan.com"}))
        self.assertIn("not registered", tools.execute(ctx_for(c, "ceo", kind="founding"), "check_domain", {"domain": "pawplan.xyz"}))
        self.assertIn("Intro call with Jo", tools.execute(ctx_for(c, "sales"), "list_bookings", {}))
        bare = ctx_for(make_company("BareCo", ("ceo", "ops")), "ops")
        self.assertIn("No payment provider is connected", tools.execute(bare, "sync_payments", {}))
        self.assertIn("no tool called 'sync_payments'", tools.execute(ctx_for(c, "marketing"), "sync_payments", {}))

    def test_routines_follow_connectors(self):
        info = presets.get("ops")
        sync = next(r for r in info["routines"] if r["title"] == "Payments sync")
        self.assertEqual(sync["requires"], "payments")
        self.assertFalse(connectors.has(db.company_settings(self.c), sync["requires"]))
        c = set_company_settings(self.c, lemonsqueezy_api_key="ls_ok")
        self.assertTrue(connectors.has(db.company_settings(c), "payments"))

    def test_connection_tests(self):
        cs = {**db.company_settings(self.c), "goatcounter_url": FAKE.url + "/goatcounter", "goatcounter_api_key": "gc_ok",
              "mastodon_url": FAKE.url + "/mastodon", "mastodon_access_token": "md_ok", "gumroad_access_token": "gr_ok",
              "webhook_url": FAKE.url + "/hooks/generic", "bluesky_handle": "pawplan.bsky.social", "bluesky_app_password": "app-pass"}
        for key, needle in (("goatcounter", "210 visitors"), ("mastodon", "@pawplan"), ("gumroad", "Jack"), ("webhook", "accepted"),
                            ("bluesky", "@pawplan.bsky.social")):
            self.assertIn(needle, connectors.test(key, cs)[0], key)
        with self.assertRaises(connectors.ConnectorError) as cm:
            connectors.test("stripe", {})
        self.assertIn("Fill in", str(cm.exception))
        with self.assertRaises(connectors.ConnectorError):
            connectors.test("mastodon", {**cs, "mastodon_access_token": "bad"})
        with self.assertRaises(connectors.ConnectorError):
            connectors.test("nothing", cs)


if __name__ == "__main__":
    unittest.main()
