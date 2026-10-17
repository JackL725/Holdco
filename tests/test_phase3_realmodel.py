"""Phase 3: what the first real Holdco Bench runs of gpt-oss:20b (17 of 33) did that the scripted model never does, each
replayed through the real code. Every case here was a product problem, not a bench or model one: the model did what a
careful operator would do with what Holdco told it, and Holdco misled it or lost the work.

- a founding stage, a research task and a price check ended with their finish arguments written as the reply's text,
  with no tool named, and ran out of steps instead of finishing;
- a planning cycle wrote its five create_task calls as text ({"create_task": {...}} one after another); the nudge
  "Use your tools to do the work" then read to it as "do the work yourself", and it wrote invented interviews;
- the plan stage asked the owner for a domain (as its prompt says), finished 'waiting' (as ask_owner's reply says),
  and founding counted as cut short, so the owner got a "founding stopped" question on top;
- with search down, the research prompt said both "do not call web_search more than once" and "3 to 6 searches";
- a marketer told that social posting is reached through find_tool looked for a posting service, found none and
  finished its post task blocked, with social_post on its own list;
- a designer whose file was leased read "work on something else or wait" and finished 'blocked', where a task stays;
  the 'waiting' refusal offered only ask_owner or 'blocked' too;
- a support agent dropped the page: prefix its task spelled out, read_file said the "file" does not exist, and it
  asked the owner for a policy the team had saved;
- write_file failed with "Try again", the model retried the identical call, and the loop guard called that a loop
  and then stopped the session;
- a research file was refused for a date ("September 30") and for "our $9 / month" price, though the refusal said
  the company's own prices need no source;
- the facts said "you cannot send email until the owner adds a mailbox", and a sales agent told to queue one intro
  email saved it as a file instead (tests.test_truth checks the new line);
- the plan stage's 12 calls could not hold its own instructions (two reads, up to 8 tasks, a brief, a question and
  finish);
- a planning cycle finished at its first step with a summary of work that never happened;
- find_tool("calculator") found nothing in a service whose tool "adds two numbers", and the agent asked the owner
  to connect a calculator (tests.test_phase1_connectors checks the "What they offer" line);
- a support agent searched its whole session for a policy file, because its role counted only files and the
  owner's answers as sources, not what its task said;
- planning cycles named a call only by its namespace, functions({"name": "start_experiment", ...}), or with a made-up
  one, server.create_task.

Run with:  python -m unittest tests.test_phase3_realmodel -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-realmodel3-"))

from holdco import agents, connectors, db, llm, planning, roles, tools, truth, workspace  # noqa: E402

db.init()

_created: list[int] = []


def make_company(name="PawPlan", members=("ceo", "engineering", "marketing", "research")) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "Meal plans for dogs", "active", "building", json.dumps({"truth_scan_done": True}),
                  db.now()))
    _created.append(cid)
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return db.get_company(cid)


def agent_of(company, role) -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def ctx_for(company, role="ceo", kind="task", task=None) -> tools.Ctx:
    a = agent_of(company, role)
    c = tools.Ctx(company=company, agent=a, kind=kind, run_id=0, task=task, settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(a, kind)}
    return c


def make_task(company, role, title, **cols) -> dict:
    a = agent_of(company, role)
    base = {"company_id": company["id"], "title": title, "role": role, "agent_id": a["id"], "status": "in_progress",
            "priority": 2, "created_by": "owner", "created_at": db.now(), "updated_at": db.now(), **cols}
    tid = db.run(f"INSERT INTO tasks({', '.join(base)}) VALUES({','.join('?' * len(base))})", tuple(base.values()))
    return db.get_task(tid)


class Base(unittest.TestCase):
    def tearDown(self):
        while _created:   # archived, so other modules' scheduler checks never see these companies
            db.run("UPDATE companies SET status='archived' WHERE id=?", (_created.pop(),))


class TestArgumentsWrittenAsText(Base):
    """The research stage's last reply: finish's arguments as the message text, no tool named."""
    TEXT = ('{"deliverable":"research/market.md",'
            '"evidence":"research/market.md exists with content and facts recorded",'
            '"next_step":"Gather missing competitor pricing.","open_issues":"Incomplete pricing data.",'
            '"status":"partial","summary":"1. Searched dog meal plan markets. 2. Saved partial findings."}')

    def belt(self, *names) -> list[dict]:
        return tools.specs(list(names))

    def test_bare_arguments_are_the_one_tool_they_fit(self):
        belt = self.belt("web_search", "fetch_url", "read_file", "write_file", "record_fact", "remember", "finish")
        tc = llm._text_tool_call(self.TEXT, {t["name"] for t in belt}, belt)
        self.assertEqual(tc.name, "finish")
        self.assertEqual(tc.arguments["status"], "partial")
        self.assertEqual(tc.arguments["deliverable"], "research/market.md")
        # a call of another tool written the same way
        tc = llm._text_tool_call('Sure:\n```json\n{"url": "https://www.pawchef.com/pricing"}\n```',
                                 {t["name"] for t in belt}, belt)
        self.assertEqual((tc.name, tc.arguments), ("fetch_url", {"url": "https://www.pawchef.com/pricing"}))

    def test_other_json_stays_text(self):
        belt = self.belt("web_search", "fetch_url", "read_file", "write_file", "finish")
        names = {t["name"] for t in belt}
        for text in ('{"summary": "x", "colour": "blue"}',          # a key no tool declares
                     '{"status": "done"}',                          # finish without its required summary
                     '["summary", "status"]', "Plain words, no JSON at all.", "{}"):
            self.assertIsNone(llm._text_tool_call(text, names, belt), text)
        # {"path": ...} fits read_file alone here; with list_files on the belt too it is ambiguous and stays text
        self.assertEqual(llm._text_tool_call('{"path": "notes.md"}', names, belt).name, "read_file")
        both = belt + self.belt("list_files")
        self.assertIsNone(llm._text_tool_call('{"path": "notes.md"}', names | {"list_files"}, both))
        self.assertIsNone(llm._text_tool_call(self.TEXT, names))   # no belt given: the old behaviour


class TestCallsWrittenAsText(Base):
    """The planning cycle's reply: its create_task calls as text, one JSON object per call."""

    def test_every_call_in_the_text_is_read(self):
        belt = tools.specs(["create_task", "start_experiment", "report_to_owner", "finish"])
        names = {t["name"] for t in belt}
        text = ('{\n  "create_task": {\n    "title": "Build landing page with waitlist form", "assign_to": "Forge",\n'
                '    "priority": 1, "deliverable": "site/index.html", "goal_id": 1\n  }\n}\n'
                '{\n  "create_task": {\n    "title": "Post landing page link in r/dogs", "assign_to": "Echo",\n'
                '    "goal_id": 1\n  }\n}\n')
        calls = llm.text_tool_calls(text, names, belt)
        self.assertEqual([(c.name, c.arguments["title"]) for c in calls],
                         [("create_task", "Build landing page with waitlist form"),
                          ("create_task", "Post landing page link in r/dogs")])
        # a call followed by an empty copy of itself (seen in Phase 2's real run): the empty one is left out
        two = ('{"name": "functions.create_task", "arguments": {"title": "Draft landing copy", "assign_to": "Echo"}}'
               '\n{"name": "functions.create_task", "arguments": {}}')
        self.assertEqual([c.arguments for c in llm.text_tool_calls(two, names, belt)],
                         [{"title": "Draft landing copy", "assign_to": "Echo"}])
        # alone, an empty call is still the call (its repair message tells the model what is missing)
        self.assertEqual([(c.name, c.arguments) for c in llm.text_tool_calls('{"tool": "create_task", "args": {}}',
                                                                               names, belt)], [("create_task", {})])
        self.assertEqual(len(llm.text_tool_calls("\n".join(['{"finish": {"summary": "x"}}'] * 6), names, belt)), 4)
        self.assertEqual(llm.text_tool_calls("I planned five tasks.", names, belt), [])

    def test_a_call_named_only_by_its_namespace_is_unwrapped(self):
        names = {"start_experiment", "create_task", "finish"}
        inner = {"goal_id": 1, "hypothesis": "A live page gets 10 signups in 14 days", "metric": "waitlist_signups",
                 "stop_days": 14}
        self.assertEqual(llm.unwrap_call("functions", {"name": "start_experiment", "arguments": inner}, names),
                         ("start_experiment", inner))
        # any name that is no tool here, around a call of one (gpt-oss's built-in container.exec in prompt-based mode)
        self.assertEqual(llm.unwrap_call("container.exec", {"tool": "list_tasks", "args": {}}, names | {"list_tasks"}),
                         ("list_tasks", {}))
        # no tool of this session inside: left for the unknown-tool error to explain
        self.assertEqual(llm.unwrap_call("functions", {"name": "rm_rf", "arguments": {"x": 1}}, names)[0], "functions")
        self.assertEqual(llm.unwrap_call("container.exec", {"cmd": ["bash", "-lc", "ls"]}, names)[0], "container.exec")
        # a made-up namespace on the name and the call wrapped once more (tool_call_repair, prompt-based mode)
        args = {"title": "Build the waitlist landing page", "assign_to": "engineering", "priority": 1}
        self.assertEqual(llm.unwrap_call("server.create_task", {"tool": "create_task", "args": args}, names),
                         ("create_task", args))
        # gpt-oss's built-in browser.search is not a namespace: tools.ALIASES maps it to web_search
        self.assertEqual(llm.unwrap_call("browser.search", {"query": "dog parks"}, names | {"search"})[0],
                         "browser.search")

    def test_the_nudge_says_the_text_did_nothing(self):
        self.assertIn("nothing happened", agents.NUDGE)
        self.assertIn("a call written out as text is not run", agents.NUDGE)
        self.assertNotIn("do the work", agents.NUDGE)


class TestFoundingNeverWaits(Base):
    def setUp(self):
        self.c = make_company("FoundCo", members=("ceo", "engineering", "marketing", "sales"))
        for i, role in enumerate(("engineering", "marketing", "sales")):
            make_task(self.c, role, f"First week task {i}", status="todo")
        self.ctx = ctx_for(self.c, "ceo", kind="founding")
        self.ctx.founding_stage = "plan"

    def test_plan_stage_that_asked_the_owner_is_done(self):
        out = tools.execute(self.ctx, "ask_owner", {"question": "Which domain should the landing page use?",
                                                    "options": "benchpaws.com\nA free Netlify subdomain",
                                                    "recommended": "A free Netlify subdomain"})
        self.assertIn("Question #", out)
        self.assertEqual(tools.execute(self.ctx, "finish", {"summary": "Five tasks; asked for a domain.",
                                                            "status": "waiting"}), "Session ended.")
        self.assertEqual(self.ctx.finish_status, "done")
        self.assertIn("Waiting on the owner: 1 request(s) in the Inbox.", self.ctx.finish_summary)

    def test_waiting_with_nothing_asked_is_done_too(self):
        tools.execute(self.ctx, "finish", {"summary": "Planned the week.", "status": "waiting"})
        self.assertEqual(self.ctx.finish_status, "done")
        self.assertNotIn("Waiting on the owner", self.ctx.finish_summary)

    def test_a_task_still_waits(self):
        task = make_task(self.c, "marketing", "Pick the launch city")
        ctx = ctx_for(self.c, "marketing", task=task)
        tools.execute(ctx, "ask_owner", {"question": "Which city do we launch in first?",
                                         "options": "Portland\nAustin", "recommended": "Portland"})
        tools.execute(ctx, "finish", {"summary": "Asked.", "status": "waiting"})
        self.assertEqual(ctx.finish_status, "waiting")

    def test_the_prompts_agree(self):
        plan = planning.founding_prompt(self.c, "", "plan", "ceo, engineering", True, False)
        self.assertIn("Call finish with status 'done'", plan)
        # two reads, up to 8 tasks, the brief, one question and finish fit the plan stage's budget, with room to spare
        self.assertIn("5 to 8 first tasks", plan)
        self.assertGreaterEqual(planning.STAGE_STEPS["plan"], 2 + 8 + 1 + 1 + 1 + 2)
        self.assertIn(f"You have {planning.STAGE_STEPS['plan']} tool calls", plan)
        down = planning.founding_prompt(self.c, "", "research", "ceo", True, True)
        self.assertIn("Do not call web_search more than once", down)
        self.assertNotIn("3 to 6 searches", down)
        up = planning.founding_prompt(self.c, "", "research", "ceo", True, False)
        self.assertIn("3 to 6 searches", up)
        self.assertIn("stop searching and write down what you have", up)


class TestOwnToolsBeforeFindTool(Base):
    def test_find_tool_points_at_social_post(self):
        c = make_company("PostCo")
        ctx = ctx_for(c, "marketing")
        self.assertIn("social_post", ctx.allowed)
        out = tools.execute(ctx, "find_tool", {"query": "X social posting"})
        self.assertTrue(out.startswith("No connected tool matches 'X social posting'. Connected services: none."), out)
        self.assertIn("Your own tools already do this: social_post. Call it directly", out)
        # nothing to point at: the old answer, unchanged
        self.assertEqual(tools.execute(ctx, "find_tool", {"query": "zebra stripes"}),
                         "No connected tool matches 'zebra stripes'. Connected services: none.")
        # a tool the role does not have is never offered
        eng = ctx_for(c, "engineering")
        self.assertNotIn("social_post", eng.allowed)
        self.assertNotIn("Your own tools", connectors.find_tool(eng, "post on reddit"))

    def test_the_rule_names_the_built_in_tools(self):
        rules = agents._shared_rules("task", 14)
        self.assertIn("send_email and social_post included", rules)
        self.assertNotIn("(payments, email, a browser, social posting) are reached through find_tool", rules)


class TestRefusalsSayWhatToDo(Base):
    def test_lease_refusal_offers_a_draft_and_partial(self):
        c = make_company("LeaseCo", members=("ceo", "engineering", "marketing"))
        t1, t2 = make_task(c, "engineering", "Build the page"), make_task(c, "marketing", "Write the hero copy")
        r1 = db.run("INSERT INTO runs(company_id, agent_id, task_id, kind, status, started_at) VALUES(?,?,?,?,?,?)",
                    (c["id"], agent_of(c, "engineering")["id"], t1["id"], "task", "running", db.now()))
        r2 = db.run("INSERT INTO runs(company_id, agent_id, task_id, kind, status, started_at) VALUES(?,?,?,?,?,?)",
                    (c["id"], agent_of(c, "marketing")["id"], t2["id"], "task", "running", db.now()))
        eng, mkt = ctx_for(c, "engineering", task=t1), ctx_for(c, "marketing", task=t2)
        eng.run_id, mkt.run_id = r1, r2
        self.assertTrue(tools.execute(eng, "write_file", {"path": "site/index.html", "content": "<h1>x</h1>"})
                        .startswith("Saved"))
        out = tools.execute(mkt, "write_file", {"path": "site/index.html", "content": "<h1>y</h1>"})
        self.assertTrue(out.startswith("ERROR: site/index.html is being edited by"), out)
        self.assertIn("save your version in another file (for example drafts/site/index.html)", out)
        self.assertIn("finish with status 'partial'", out)
        self.assertNotIn("or wait", out)
        self.assertTrue(tools.execute(mkt, "write_file", {"path": "drafts/site/index.html", "content": "<h1>y</h1>"})
                        .startswith("Saved"))

    def test_waiting_refusal_names_partial(self):
        self.assertIn("finish with status 'partial' when another attempt can carry on", tools.WAITING_REFUSAL)
        self.assertIn("'blocked'", tools.WAITING_REFUSAL)


class TestAnInvitedRetryIsNoLoop(Base):
    """write_file failed with "Try again"; the model retried the identical call, the loop guard said "You already
    made this exact call", and the third try stopped the session for repeating itself."""

    def run_task(self, steps):
        c = make_company("RetryCo", members=("ceo", "marketing"))
        task = make_task(c, "marketing", "Write the launch notes", status="todo", deliverable="marketing/notes.md")
        replies = list(steps)

        def chat(messages, tools_spec=None, model=None, settings=None, **kw):
            name, args = replies.pop(0) if replies else ("finish", {"summary": "Done.", "status": "partial"})
            return llm.Reply(tool_calls=[llm.ToolCall(name=name, arguments=args)], tokens=10)

        failures = {"n": 0}
        real = tools.REGISTRY["write_file"].fn

        def flaky(ctx, *a, **kw):
            if failures["n"] < 2:
                failures["n"] += 1
                raise tools.ToolError("the disk was busy and the file was not saved. Try again.")
            return real(ctx, *a, **kw)

        saved = db.get_settings().get("kill_switch")
        db.set_settings({"kill_switch": False})
        try:
            with mock.patch.object(llm, "chat", chat), mock.patch.object(tools.REGISTRY["write_file"], "fn", flaky):
                agents.run(agent_of(c, "marketing")["id"], "task", task_id=task["id"])
        finally:
            db.set_settings({"kill_switch": saved})
        run = db.one("SELECT * FROM runs WHERE company_id=? ORDER BY id DESC LIMIT 1", (c["id"],))
        results = [m["content"] for m in json.loads(run["transcript"]) if m.get("role") == "tool"]
        return run, results, c

    def test_the_same_write_is_retried(self):
        notes = "# Launch notes\n\n- Dog parks: a flyer with the waitlist link.\n- r/dogs: one helpful post.\n"
        write = ("write_file", {"path": "marketing/notes.md", "content": notes})
        done = ("finish", {"summary": "Saved the notes.", "deliverable": "marketing/notes.md"})
        run, results, c = self.run_task([write, write, write, done])
        self.assertEqual([r.split(" ")[0] for r in results], ["ERROR:", "ERROR:", "Saved", "Session"], results)
        self.assertEqual(run["status"], "done")
        self.assertTrue(workspace.safe_path(c, "marketing/notes.md").is_file())

    def test_a_plain_repeat_is_still_a_loop(self):
        look = ("read_file", {"path": "marketing/none.md"})
        run, results, _c = self.run_task([look, look, look])
        self.assertTrue(results[0].startswith("ERROR: marketing/none.md doesn't exist"), results)
        self.assertTrue(results[1].startswith("You already made this exact call"), results)
        self.assertEqual(results[2], "Session stopped: repeated action.")
        self.assertEqual(run["status"], "partial")


class TestSourcesCheckKnowsOurNumbers(Base):
    """A research file was refused for "landing pages accessed on September 30" and "our $9 / month position", while
    the refusal itself said that prices the company set need no source; the run ran out of steps fixing them."""

    def test_dates_and_our_own_price_need_no_source(self):
        c = make_company("PriceCo")
        db.run("INSERT INTO strategy(company_id, price, offer, updated_at) VALUES(?,?,?,?)",
               (c["id"], "$9 per month (to test).", "A weekly meal plan.", db.now()))
        db.run("INSERT INTO goals(company_id, title, metric, target, current, deadline, owner_role, status, "
               "created_at) VALUES(?,?,?,?,?,?,?,?,?)", (c["id"], "200 waitlist signups", "waitlist_signups", 200,
                                                         40, "2026-12-01", "marketing", "active", db.now()))
        text = ("All prices were captured from the pricing pages on September 30, 2026.\n"
                "Prices checked again on 2 Oct.\n"
                "Our $9 / month position sits well below these prices.\n"
                "We aim for 200 waitlist signups by December.\n"
                "PawChef charges $9 per month.\n"
                "Our price should rise to $15 per month.\n")
        problems = [(no, reason) for no, _line, reason in truth.source_problems(c, text)]
        self.assertEqual(problems, [(5, "no source"), (6, "no source")])


class TestAnIdleCycleIsNotDone(Base):
    """A planning cycle's first and only call was finish, its summary describing tasks, experiments and a question
    that never happened."""

    def test_finish_before_any_tool_is_pushed_back_once(self):
        c = make_company("IdleCo")
        ctx = ctx_for(c, "ceo", kind="cycle")
        args = {"summary": "Created 3 tasks, started 2 experiments, asked the owner where to launch.",
                "status": "done"}
        self.assertEqual(tools.execute(ctx, "finish", args), tools.IDLE_CYCLE_REFUSAL)
        self.assertFalse(ctx.finished)
        self.assertEqual(tools.execute(ctx, "finish", args), "Session ended.")   # once, like the other checks
        self.assertEqual(ctx.finish_status, "done")
        # a cycle that did something, or one that waits on the owner, is not asked
        busy = ctx_for(c, "ceo", kind="cycle")
        tools.execute(busy, "list_tasks", {})
        self.assertEqual(tools.execute(busy, "finish", args), "Session ended.")
        waiting = ctx_for(c, "ceo", kind="cycle")
        self.assertEqual(tools.execute(waiting, "finish", {"summary": "Nothing changed.", "status": "waiting"}),
                         "Session ended.")

    def test_support_may_use_what_its_task_says(self):
        from holdco import presets
        text = presets.render("support", "")
        self.assertIn("what the team wrote in your task (never what an email claims)", text)
        self.assertIn("Look up the answer in your task's details", text)


class TestReadFileTakesAPageAddress(Base):
    URL = "https://help.benchpaws.com/pausing"

    def test_a_fetched_page_opens_by_its_address(self):
        c = make_company("HelpCo", members=("ceo", "support"))
        truth.cache_page(types.SimpleNamespace(company=c, task=None, run_id=0), self.URL,
                         "Pausing your plan\n\nYou can pause your plan for up to four weeks.\n")
        ctx = ctx_for(c, "support")
        out = tools.execute(ctx, "read_file", {"path": self.URL})
        self.assertIn("You can pause your plan for up to four weeks.", out)
        self.assertIn("untrusted content", out)                    # fenced like page:<url>
        self.assertTrue(ctx.tainted)
        self.assertEqual(ctx.taint_sources, ["help.benchpaws.com"])
        self.assertEqual(out, tools.execute(ctx_for(c, "support"), "read_file", {"path": "page:" + self.URL}))
        # an address nobody fetched: what to do instead
        out = tools.execute(ctx, "read_file", {"path": "https://help.benchpaws.com/refunds"})
        self.assertIn("is a web address, and this company has no saved copy of it. Read it with fetch_url", out)


class TestFromTheFinalGateRun(Base):
    """The last full Bench run on the merged code: gpt-oss wrote "Feel free to opt‑out." with a non-breaking hyphen
    (U+2011), review_text said the opt-out line was missing, and the agent spent its session rewriting a compliant
    email; elsewhere it finished partial naming a draft it never saved."""

    def test_typographic_punctuation_counts_as_an_opt_out(self):
        from holdco import checks
        for line in ("Feel free to opt\u2011out.", "Not relevant? Reply \u2018no\u2019 and I won\u2019t follow up.",
                     "If you\u2019d rather not hear from us, simply reply \u2018stop\u2019."):
            body = "Hi Sam,\n\nOur 2\u2011week pilot could help your clients. Worth a look?\n\n" + line + "\n\nAva, PawPlan"
            self.assertNotIn("No opt-out line", checks.review_text(body, "cold_email"), line)
        self.assertTrue(truth.is_stop_request("Re: hi", "Please stop e\u2011mailing me."))

    def test_partial_finish_naming_an_unsaved_file_is_pushed_back_once(self):
        c = make_company("DraftCo")
        task = make_task(c, "engineering", "Update the hero copy", status="in_progress")
        ctx = ctx_for(c, "engineering", task=task)
        out = tools.execute(ctx, "finish", {"summary": "Draft saved", "status": "partial",
                                            "deliverable": "drafts/site/index.html"})
        self.assertTrue(out.startswith("Not finished yet: drafts/site/index.html doesn't exist."), out)
        self.assertFalse(ctx.finished)
        tools.execute(ctx, "finish", {"summary": "Draft saved", "status": "partial",
                                      "deliverable": "drafts/site/index.html"})
        self.assertTrue(ctx.finished)
        # a partial handoff without a file, or naming one that exists, is not refused
        ctx2 = ctx_for(c, "engineering", task=task)
        self.assertEqual(tools.execute(ctx2, "finish", {"summary": "Half done", "status": "partial"}), "Session ended.")


if __name__ == "__main__":
    unittest.main()
