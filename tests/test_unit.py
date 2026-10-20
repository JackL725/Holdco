"""Unit tests: checks, model-client parsing, presets, workspace safety, tools and approvals.

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

from holdco import actions, agents, checks, config, db, llm, presets, roles, tools, web, workspace  # noqa: E402

db.init()


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
        # the default model (the owner's Huihui Qwen3.8 27B) gets Qwen3's recommended thinking-mode sampling
        self.assertEqual(llm.sampling_for(config.DEFAULT_SETTINGS["default_model"], "precise", {"sampling": "auto"}),
                         {"temperature": 0.6, "top_p": 0.95, "top_k": 20})
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

    PARSE_ERR = ('HTTP 500 from model server: {"error":"error parsing tool call: raw=\'{\\"arguments\\":{\\"content\\":'
                 '\\"Budget $400 per month\\n')

    def _chat_with_scripted_call(self, outcomes):
        """Run llm.chat with _call replaced by a script of exceptions / Replies. Returns (result, calls made)."""
        calls = self.last_calls = []

        def fake_call(*args):
            calls.append(args)
            out = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(out, Exception):
                raise out
            return out

        original = llm._call
        llm._call = fake_call
        try:
            tools_spec = [{"name": "write_file", "description": "d", "parameters": {"type": "object", "properties": {}}}]
            return llm.chat([{"role": "user", "content": "go"}], tools_spec, settings={**db.get_settings(), "tool_mode": "auto"}), calls
        finally:
            llm._call = original

    def test_tool_parse_error_is_retried(self):
        self.assertTrue(llm.is_tool_parse_error(llm.LLMError(self.PARSE_ERR)))
        self.assertFalse(llm.is_tool_parse_error(llm.LLMError("HTTP 404 from model server: model not found")))
        ok = llm.Reply(content="fine")
        reply, calls = self._chat_with_scripted_call([llm.LLMError(self.PARSE_ERR), llm.LLMError(self.PARSE_ERR), ok])
        self.assertIs(reply, ok)
        self.assertEqual(len(calls), 3)

    def test_tool_parse_error_gives_up_and_keeps_native_tools(self):
        before = set(llm._json_mode_models)
        with self.assertRaises(llm.LLMError):
            # The error text contains "tool" and "400"; it must not flip the model to prompt-based tool calls.
            self._chat_with_scripted_call([llm.LLMError(self.PARSE_ERR)])
        self.assertEqual(llm._json_mode_models, before)
        self.assertEqual(len(self.last_calls), llm.PARSE_RETRIES + 1)

    def test_other_errors_are_not_retried(self):
        with self.assertRaises(llm.LLMError):
            self._chat_with_scripted_call([llm.LLMError("HTTP 503 from model server: busy")])
        self.assertEqual(len(self.last_calls), 1)


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
            # Phase 2: planning needs the brain tools (brain_lookup, propose_brain_change, add_lesson; the CEO's
            # set_strategy, set_goal and the rest), so the cap rose from 15 to 18 for executors and 24 for the CEO.
            cap = 24 if key == "ceo" else 18
            self.assertLessEqual(len(p["tools"]), cap, f"{key} has too many tools for a small model")
            unknown = [t for t in p["tools"] if t not in tools.REGISTRY]
            self.assertFalse(unknown, f"{key} uses unknown tools {unknown}")
            self.assertIn("finish", p["tools"])
            self.assertLessEqual(len(p["rules"]), 8, f"{key} has too many rules")
            self.assertLess(len(presets.render(key)), 4500, f"{key} role prompt is too long")
            for r in p["routines"]:
                self.assertGreater(r["every_hours"], 0)
                self.assertIn(r["requires"], ("", "imap", "smtp", "no_analyst"))
                self.assertIn(r["at_hour"], (None, *range(24)))
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
        # a page the verification gate's static check passes (Phase 3): viewport and title, no audit blocker
        tools.execute(ctx, "write_file", {"path": "site/page.html", "content": (
            '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width, initial-scale=1"><title>Page</title></head><body><h1>Page</h1><p>'
            + "x" * 100 + "</p></body></html>")})
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
        # an email with no SMTP set fails and can be retried (commercial email needs a postal address first)
        s = db.company_settings(self.c)
        s["postal_address"] = "1 Main Street, Springfield, IL 62701"
        db.update("companies", self.c["id"], settings=json.dumps(s))
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


class TestLoopGuard(unittest.TestCase):
    """The agent loop stops real repetition but lets an agent re-check a file it just changed."""

    DOC = "docs/marketing/positioning.md"

    def setUp(self):
        self.c = make_company()
        self.agent = db.one("SELECT * FROM agents WHERE company_id=? AND role='marketing'", (self.c["id"],))
        self.task_id = db.run("INSERT INTO tasks(company_id, title, role, status, deliverable, created_by, created_at, "
                              "updated_at) VALUES(?,?,?,?,?,?,?,?)",
                              (self.c["id"], "Draft positioning", "marketing", "in_progress", self.DOC, "test",
                               db.now(), db.now()))

    def run_script(self, script):
        """Run one task session where the model replays `script` (a list of (tool, args)). Returns [(tool, result)]."""
        steps = iter(script)

        def fake_chat(messages, tools_spec=None, *a, **kw):
            try:
                name, args = next(steps)
            except StopIteration:
                return llm.Reply(content="")
            return llm.Reply(tool_calls=[llm.ToolCall(name=name, arguments=args)])

        original = agents.llm.chat
        agents.llm.chat = fake_chat
        try:
            agents.run(self.agent["id"], "task", task_id=self.task_id)
        finally:
            agents.llm.chat = original
        row = db.one("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC", (self.agent["id"],))
        transcript = json.loads(row["transcript"])
        self.run_row = row
        return [(m["name"], m["content"]) for m in transcript if m["role"] == "tool"]

    def write(self, text):
        return ("write_file", {"path": self.DOC, "content": text, "append": False})

    REVIEW = ("review_text", {"path": DOC, "text": "", "kind": "article"})
    STOPPED = "Session stopped: repeated action."
    NUDGE = "You already made this exact call"

    def test_recheck_after_rewrite_is_allowed(self):
        # The exact sequence that stopped a real run: write, review, rewrite, review again with identical arguments.
        results = self.run_script([
            self.write("We help Phoenix homeowners find roofers. Every roofer is checked first."), self.REVIEW,
            self.write("We help Phoenix homeowners find a roofer. Each roofer is checked first."), self.REVIEW,
            ("finish", {"summary": "done", "deliverable": self.DOC})])
        second_review = [r for n, r in results if n == "review_text"][1]
        self.assertNotIn(self.NUDGE, second_review)
        self.assertIn("words", second_review)
        self.assertNotEqual(self.run_row["status"], "error")
        self.assertNotIn("repeating the same action", self.run_row["summary"])
        self.assertEqual(db.get_task(self.task_id)["status"], "done")

    def test_identical_repeat_without_changes_is_still_stopped(self):
        text = "We help Phoenix homeowners find roofers. Every roofer is checked first."
        results = self.run_script([self.write(text), self.REVIEW, self.REVIEW, self.REVIEW])
        review_results = [r for n, r in results if n == "review_text"]
        self.assertIn("words", review_results[0])
        self.assertIn(self.NUDGE, review_results[1])
        self.assertEqual(review_results[2], self.STOPPED)
        self.assertIn("repeating the same action", self.run_row["summary"])

    def test_identical_writes_are_still_stopped(self):
        same = self.write("We help Phoenix homeowners find roofers. Every roofer is checked first.")
        results = self.run_script([same, self.REVIEW, same, self.REVIEW, same, self.REVIEW])
        self.assertEqual([r for n, r in results if n == "write_file"][-1], self.STOPPED)

    def test_failed_write_does_not_reset_the_guard(self):
        bad = ("write_file", {"path": "../outside.md", "content": "x"})
        results = self.run_script([self.REVIEW, bad, self.REVIEW])
        self.assertIn(self.NUDGE, [r for n, r in results if n == "review_text"][1])



class TestTreeSurvivesVanishingFiles(unittest.TestCase):
    """Windows CI: a records/ temp file vanished between listing the folder and resolving its path, and the
    ValueError from relative_to crashed an agent run. A file that disappears mid-walk is skipped."""

    def test_vanishing_file_is_skipped(self):
        from unittest import mock
        c = make_company("VanishCo", members=("ceo",))
        root = workspace.root(c)
        (root / "docs" / "keep.md").write_text("x", encoding="utf-8")
        (root / "docs" / ".gone.tmp").write_text("x", encoding="utf-8")
        real_rel = workspace.rel

        def flaky_rel(company, p):
            if p.name == ".gone.tmp":
                raise ValueError("is not in the subpath")
            return real_rel(company, p)

        with mock.patch.object(workspace, "rel", flaky_rel):
            paths = [i["path"] for i in workspace.tree(c, ".", 3, 100)]
        self.assertIn("docs/keep.md", paths)
        self.assertNotIn("docs/.gone.tmp", paths)


if __name__ == "__main__":
    unittest.main()
