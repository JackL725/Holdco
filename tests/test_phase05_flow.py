"""Phase 0.5 flow fixes from the review and the first real gpt-oss run: planning that never freezes an idle
company, tasks that wait on someone else's question, the delete guard, chat claims, settings as news, question
dedupe, pinned paragraphs, the smaller items, founding stage deliverables and fetched pages in search.

Each class turns one of the reviewer's reproduction scripts (stall, waiting, guards, dedupe, prompt, charter) or
one gap from the real run into a regression test. Only the outside world is faked: the model, web pages, the
phone and the scheduler's thread start.

Run with:  python -m unittest tests.test_phase05_flow -v
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-flow-"))

from holdco import (actions, agents, bus, context, db, doctor, llm, notify, planning, roles, routing,  # noqa: E402
                    scheduler, server, tools, truth, web, workspace)  # noqa: F401  (server registers routes)
from tests.fake_ollama import CHARTER, STRATEGY  # noqa: E402

db.init()

SITE_URL = "https://flowco.netlify.app"
PAGE_URL = "https://leadstar.com/pricing"
PAGE_TEXT = ("Title: LeadStar pricing for roofers\nURL: https://leadstar.com/pricing\n\n"
             "LeadStar sells roofing leads.\nLeadStar charges roofers $35 per exclusive lead in Phoenix.\n"
             "Shared leads cost $12 each.\n")


# ---------------------------------------------------------------- helpers (local copies, like the other modules)

def ago(minutes: float = 0) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


_created: list[int] = []   # archived after each test, so other modules' Doctor and scheduler checks never see them


def make_company(name="FlowCo", members=("ceo", "engineering", "marketing", "research"), status="active",
                 idea="Roofing leads for roofers in Phoenix", **settings) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    s = {"truth_scan_done": True, **settings}
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, idea, status, "building", json.dumps(s), db.now()))
    _created.append(cid)
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return db.get_company(cid)


def fresh(company) -> dict:
    return db.get_company(company["id"])


def agent_of(company, role="ceo") -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def set_settings(company, **kw) -> dict:
    s = db.company_settings(fresh(company))
    s.update(kw)
    db.update("companies", company["id"], settings=json.dumps(s))
    return fresh(company)


def make_task(company, role, title, status="todo", kind="", deliverable="") -> dict:
    a = agent_of(company, role)
    tid = db.run("INSERT INTO tasks(company_id, title, role, agent_id, status, priority, kind, deliverable, created_by, "
                 "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (company["id"], title, role, a["id"], status, 2, kind, deliverable, "test", db.now(), db.now()))
    return db.get_task(tid)


def ctx_for(company, role="ceo", kind="task", task=None) -> tools.Ctx:
    a = agent_of(company, role)
    c = tools.Ctx(company=fresh(company), agent=a, kind=kind, run_id=0, task=task, settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(a, kind)}
    return c


def add_run(company, kind="cycle", status="done", started=None, role="ceo", tokens=0) -> int:
    return db.run("INSERT INTO runs(company_id, agent_id, kind, status, tokens, started_at, finished_at) "
                  "VALUES(?,?,?,?,?,?,?)", (company["id"], agent_of(company, role)["id"], kind, status, tokens,
                                            started or ago(1), db.now()))


def question(company, text, task_id=None, role="ceo") -> int:
    out = actions.gate(company, agent_of(company, role), task_id, "question", {"question": text})
    return int(out.split("#")[1].split(" ")[0])


def pending(company, action=None) -> list[dict]:
    rows = db.q("SELECT * FROM approvals WHERE company_id=? AND status='pending' ORDER BY id", (company["id"],))
    return [r for r in rows if action is None or r["action"] == action]


def call(method: str, path: str, body=None, query: dict | None = None):
    """Dispatch to a dashboard route the way server.Handler does."""
    class Handler:
        def __init__(self):
            self.query = {k: [v] for k, v in (query or {}).items()}

    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(path)):
            return fn(Handler(), m, body)
    raise AssertionError(f"no route for {method} {path}")


class Scripted:
    """Stands in for llm.chat: each call answers with the next step. A step is (tool, args), a plain string (a
    text reply with no tool call) or a function of the call number. Records every call's messages."""

    def __init__(self, steps=()):
        self.steps = list(steps)
        self.calls: list[dict] = []

    def __call__(self, messages, tools_spec=None, model=None, settings=None, **kw):
        self.calls.append({"tools": [t["name"] for t in tools_spec or []], "system": messages[0]["content"],
                           "user": messages[1]["content"], "messages": [dict(m) for m in messages]})
        if not self.steps:
            return llm.Reply(content="")
        step = self.steps.pop(0)
        if callable(step):
            step = step(len(self.calls))
        if isinstance(step, str):
            return llm.Reply(content=step, tokens=50)
        name, args = step
        return llm.Reply(tool_calls=[llm.ToolCall(name=name, arguments=args)], tokens=100)


@contextlib.contextmanager
def captured_starts():
    started: list = []
    with mock.patch.object(agents, "start", lambda aid, kind, **kw: started.append((aid, kind, kw)) or True):
        yield started


def quiet():
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
    return stack


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: db.get_settings().get(k) for k in ("kill_switch", "review_model", "max_steps_per_run",
                                                             "pinned_context", "num_ctx")}
        db.set_settings({"kill_switch": False, "review_model": "", "max_steps_per_run": 14, "pinned_context": []})
        self.sent: list = []
        self._patches = [mock.patch.object(doctor, "LAST", []),
                         mock.patch.object(web, "SEARCH_FAIL_STREAK", 0),
                         mock.patch.object(notify, "send_later", lambda text, buttons=None, kind="info", **kw:
                                           self.sent.append((text, kind))),
                         mock.patch.object(notify, "send", lambda text, buttons=None, kind="info", **kw:
                                           self.sent.append((text, kind)) or True)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        db.set_settings({k: v for k, v in self._saved.items() if v is not None})
        while _created:
            db.update("companies", _created.pop(), status="archived")

    def run_agent(self, company, role, kind, script, task=None, **kw):
        """One session replaying `script`. Returns ([(tool, result)], the run row, the scripted model)."""
        s = script if isinstance(script, Scripted) else Scripted(script)
        a = agent_of(company, role)
        with mock.patch.object(agents.llm, "chat", s):
            agents.run(a["id"], kind, task_id=task["id"] if task else None, **kw)
        row = db.one("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC", (a["id"],))
        transcript = json.loads(row["transcript"] or "[]")
        return [(m["name"], m["content"]) for m in transcript if m["role"] == "tool"], row, s


# =============================================================== 1. a failed or empty cycle never freezes a company

class TestStalledCompany(Base):
    """review05/stall.py: one planning cycle that errored became the baseline, and an idle company with nothing
    open and nothing waiting never planned again."""

    def started(self, c) -> dict:
        planning.cycle_started(fresh(c))
        db.update("companies", c["id"], last_cycle_at=db.now())
        return fresh(c)

    def test_a_failed_cycle_is_no_baseline(self):
        for status in planning.FAILED_CYCLE:
            c = make_company(f"Stall {status}")
            self.assertTrue(planning.should_cycle(fresh(c), False, True, False))   # never planned
            c = self.started(c)
            add_run(c, status=status)
            db.update("companies", c["id"], last_cycle_at=ago(11))
            self.assertTrue(planning.should_cycle(fresh(c), False, True, False), status)
            self.assertEqual(planning.stamp_record(fresh(c))["s"], "", status)

    def test_a_cycle_that_finished_is_the_baseline(self):
        for status in ("done", "partial", "waiting"):
            c = make_company(f"Base {status}")
            c = self.started(c)
            make_task(c, "engineering", "Wait for the deploy answer", status="waiting")
            question(c, "May we deploy the site?")
            add_run(c, status=status)
            db.update("companies", c["id"], last_cycle_at=ago(11))
            self.assertFalse(planning.should_cycle(fresh(c), False, True, False), status)

    def test_the_safety_net_plans_an_idle_company_and_spaces_cycles_out(self):
        c = make_company("NetCo")
        c = self.started(c)
        add_run(c, status="done")   # a cycle that created nothing
        db.update("companies", c["id"], last_cycle_at=ago(30))
        self.assertFalse(planning.should_cycle(fresh(c), False, True, False), "the idle trigger needs a change")
        db.update("companies", c["id"], last_cycle_at=ago(61))
        self.assertTrue(planning.should_cycle(fresh(c), True, True, False))
        self.assertEqual(fresh(c)["cycle_backoff"], 1)
        # the scheduler starts it: the backoff it raised is kept, and the next cycle also creates nothing
        planning.cycle_started(fresh(c))
        self.assertEqual(fresh(c)["cycle_backoff"], 1)
        add_run(c, status="done", started=db.now())
        db.update("companies", c["id"], last_cycle_at=ago(61))
        self.assertFalse(planning.should_cycle(fresh(c), True, True, False), "the next one waits 120 minutes")
        self.assertAlmostEqual(planning.next_due_minutes(fresh(c)), 59, delta=1.5)
        db.update("companies", c["id"], last_cycle_at=ago(121))
        self.assertTrue(planning.should_cycle(fresh(c), True, True, False))
        self.assertEqual(fresh(c)["cycle_backoff"], 2)
        # real news resets the backoff
        db.run("INSERT INTO messages(company_id, agent_id, sender, content, created_at) VALUES(?,?,?,?,?)",
               (c["id"], None, "owner", "Any news?", db.now()))
        self.assertTrue(planning.should_cycle(fresh(c), False, True, False))
        self.assertEqual(fresh(c)["cycle_backoff"], 0)

    def test_no_safety_net_while_everything_waits_on_the_owner(self):
        c = make_company("StillWaitCo")
        make_task(c, "engineering", "Deploy", status="waiting")
        question(c, "May we deploy the site?")
        c = self.started(c)
        for minutes in (61, 121, 241, 481):
            db.update("companies", c["id"], last_cycle_at=ago(minutes))
            self.assertFalse(planning.should_cycle(fresh(c), True, True, False), minutes)
        self.assertEqual(fresh(c)["cycle_backoff"], 4)

    def test_open_work_is_not_replanned_by_the_safety_net(self):
        c = make_company("BusyCo")
        make_task(c, "engineering", "Build the page", status="todo")
        c = self.started(c)
        db.update("companies", c["id"], last_cycle_at=ago(61))
        self.assertFalse(planning.should_cycle(fresh(c), True, False, False))

    def test_the_scheduler_plans_again_after_an_error(self):
        c = make_company("StallSchedCo")
        starts: list = []
        sched = scheduler.Scheduler()
        with mock.patch.object(agents, "start", lambda aid, kind, **kw: starts.append(kind) or True):
            sched._maybe_cycle(fresh(c))
            self.assertEqual(starts, ["cycle"])
            add_run(c, status="error")
            bus.emit(c["id"], "error", "Atlas hit an error: error parsing tool call", agent_of(c)["id"])
            db.update("companies", c["id"], last_cycle_at=ago(11))
            sched._maybe_cycle(fresh(c))
            self.assertEqual(starts, ["cycle", "cycle"])


# =============================================================== 2. tasks waiting on someone else's question

class TestWaitingTasks(Base):
    """review05/waiting.py: a task whose question was refused as a duplicate finished 'waiting' and was never
    reopened when the owner answered."""

    def setUp(self):
        super().setUp()
        self.c = make_company("WaitFlowCo")

    def test_a_deduped_question_waits_and_the_answer_reopens_the_task(self):
        qid = question(self.c, "Can you add a Netlify token so we can deploy the site?")
        task = make_task(self.c, "marketing", "Announce the launch page")
        results, run, _s = self.run_agent(self.c, "marketing", "task", [
            ("ask_owner", {"question": "Do we have a Netlify token for deploying?"}),
            ("finish", {"summary": "The post is ready; publishing waits for the token.", "status": "waiting"})],
            task)
        self.assertTrue(results[0][1].startswith(f"Already waiting on the owner as #{qid}"), results[0][1])
        self.assertEqual(results[1][1], "Session ended.")
        t = db.get_task(task["id"])
        self.assertEqual((run["status"], t["status"]), ("waiting", "waiting"))
        self.assertIn(f"Waiting on the owner: #{qid}", t["result"])
        actions.decide(qid, "approve", answer="Token added")
        t = db.get_task(task["id"])
        self.assertEqual(t["status"], "todo")
        self.assertTrue(t["result"].endswith(f"[Holdco] The owner answered #{qid}; continue."), t["result"])

    def test_it_keeps_waiting_while_its_own_question_is_pending(self):
        q_deploy = question(self.c, "May we deploy the site to Netlify?")
        q_price = question(self.c, "What price should we charge per lead: $35 or $45?")
        task = make_task(self.c, "marketing", "Write the pricing section")
        ctx = ctx_for(self.c, "marketing", task=task)
        out = tools.execute(ctx, "ask_owner", {"question": "What price per lead should the page show?"})
        self.assertTrue(out.startswith(f"Already waiting on the owner as #{q_price}"), out)
        self.assertEqual(ctx.waiting_on, q_price)
        tools.execute(ctx, "finish", {"summary": "Waiting for the price.", "status": "waiting"})
        self.assertEqual(ctx.finish_status, "waiting")
        db.update("tasks", task["id"], status="waiting", result=ctx.finish_summary)   # what _finalize does
        actions.decide(q_deploy, "approve", answer="Yes")
        self.assertEqual(db.get_task(task["id"])["status"], "waiting", "an unrelated answer leaves it waiting")
        actions.decide(q_price, "approve", answer="$35")
        self.assertEqual(db.get_task(task["id"])["status"], "todo")

    def test_a_waiting_task_with_its_own_pending_question_is_left_alone(self):
        task = make_task(self.c, "marketing", "Pick the brand voice", status="waiting")
        question(self.c, "Playful or formal voice?", task_id=task["id"], role="marketing")
        other = question(self.c, "Which city first?")
        older = make_task(self.c, "engineering", "An older waiting task", status="waiting")
        actions.decide(other, "approve", answer="Phoenix")
        self.assertEqual(db.get_task(task["id"])["status"], "waiting")
        self.assertEqual(db.get_task(older["id"])["status"], "todo")   # nothing pending of its own

    def test_waiting_with_nothing_pending_is_refused_once_then_blocked(self):
        task = make_task(self.c, "marketing", "Write the FAQ")
        ctx = ctx_for(self.c, "marketing", task=task)
        out = tools.execute(ctx, "finish", {"summary": "Waiting.", "status": "waiting"})
        self.assertEqual(out, "Not finished yet: nothing is waiting on the owner for this task. Ask with ask_owner "
                              "(one question with options and your recommendation), or finish with status "
                              "'blocked' and say what blocks you.")
        self.assertFalse(ctx.finished)
        tools.execute(ctx, "finish", {"summary": "Waiting.", "status": "waiting"})
        self.assertEqual(ctx.finish_status, "blocked")
        self.assertIn("Nothing was waiting on the owner for this task.", ctx.finish_summary)

    def test_waiting_on_its_own_question_is_accepted_at_once(self):
        task = make_task(self.c, "marketing", "Choose the launch city")
        ctx = ctx_for(self.c, "marketing", task=task)
        self.assertIn("sent to the owner", tools.execute(ctx, "ask_owner", {"question": "Phoenix or Tucson first?"}))
        tools.execute(ctx, "finish", {"summary": "Asked the owner.", "status": "waiting"})
        self.assertEqual(ctx.finish_status, "waiting")

    def test_a_planning_cycle_may_still_finish_waiting(self):
        ctx = ctx_for(self.c, "ceo", kind="cycle")
        tools.execute(ctx, "finish", {"summary": "Nothing changed.", "status": "waiting"})
        self.assertEqual(ctx.finish_status, "waiting")

    def test_the_question_cap_points_to_blocked(self):
        for q in ("What should the brand voice be?", "Which city do we start in?", "Who is our first hire?"):
            question(self.c, q)
        out = planning.ask_owner(ctx_for(self.c, "ceo", kind="cycle"), "Should the logo be blue or green?")
        self.assertIn("finish with status 'blocked' and say what you need from the owner", out)


# =============================================================== 3. delete_file guard

class TestDeleteGuard(Base):
    """review05/guards.py: delete_file could move records/ and the owner's context library to the trash."""

    def test_records_and_context_cannot_be_deleted(self):
        c = make_company("GuardCo")
        root = workspace.root(c)
        for rel in ("records/STATUS.md", "context/brand.md", "docs/old.md"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# x\n", encoding="utf-8")
        ctx = ctx_for(c, "ceo")
        ctx.allowed = set()   # every tool, as in the reviewer's script (no preset has delete_file and records)
        for path in ("records/STATUS.md", "./records/STATUS.md", "docs/../records/STATUS.md", "records",
                     "/records/STATUS.md", "records\\STATUS.md"):
            self.assertEqual(tools.execute(ctx, "delete_file", {"path": path}), truth.RECORDS_REFUSAL, path)
        for path in ("context/brand.md", "context", "./context/", "docs/../context/brand.md", "context\\brand.md"):
            self.assertEqual(tools.execute(ctx, "delete_file", {"path": path}), truth.CONTEXT_REFUSAL, path)
        self.assertTrue((root / "records/STATUS.md").is_file())
        self.assertTrue((root / "context/brand.md").is_file())
        self.assertTrue(tools.execute(ctx, "delete_file", {"path": "docs/old.md"}).startswith("Moved to"))
        self.assertFalse((root / "docs/old.md").exists())


# =============================================================== 4. chat replies are checked for claims

class TestChatClaims(Base):
    CLAIM = f"Good news: we deployed the site. It is live at {SITE_URL}."
    HONEST = "The site is ready in site/ and the deploy request is waiting for your approval in the inbox."

    def setUp(self):
        super().setUp()
        self.c = make_company("ChatClaimCo")
        db.run("INSERT INTO approvals(company_id, agent_id, action, summary, payload, status, created_at) "
               "VALUES(?,?,?,?,?,?,?)", (self.c["id"], agent_of(self.c, "engineering")["id"], "deploy_site",
                                         "Deploy the website from site/", json.dumps({"folder": "site"}), "pending",
                                         ago(30)))

    def reply(self) -> str:
        return db.one("SELECT content FROM messages WHERE company_id=? AND sender='agent' ORDER BY id DESC LIMIT 1",
                      (self.c["id"],))["content"]

    def test_a_false_claim_gets_one_rewrite(self):
        _r, _run, s = self.run_agent(self.c, "ceo", "chat", [self.CLAIM, self.HONEST], text="Is the site live?")
        self.assertEqual(len(s.calls), 2)
        last = s.calls[1]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertTrue(last["content"].startswith("Your reply states things the records do not support:"), last["content"])
        self.assertEqual(self.reply(), self.HONEST)

    def test_a_second_false_claim_goes_out_with_the_records(self):
        _r, _run, s = self.run_agent(self.c, "ceo", "chat", [self.CLAIM, self.CLAIM], text="Is the site live?")
        self.assertEqual(len(s.calls), 2, "no loop: the second reply is sent")
        text = self.reply()
        self.assertTrue(text.startswith(self.CLAIM + "\n\nHoldco's records: "), text)
        self.assertNotIn("—", text)

    def test_an_honest_reply_is_sent_unchanged(self):
        _r, _run, s = self.run_agent(self.c, "ceo", "chat", [self.HONEST], text="Is the site live?")
        self.assertEqual((len(s.calls), self.reply()), (1, self.HONEST))


# =============================================================== 5. company settings count as a change

class TestSettingsAreNews(Base):
    def test_capability_settings_change_the_stamp(self):
        c = make_company("SettingsCo")
        stamp = planning.change_stamp(fresh(c))
        set_settings(c, last_waiting_notice="2026-01-01", budget_notice="2026-01-01", cycle_minutes=30)
        self.assertEqual(planning.change_stamp(fresh(c)), stamp, "Holdco's own notes are not news")
        for key, value in (("postal_address", "1 Main Street, Springfield"), ("netlify_token", "tok"),
                           ("site_url", SITE_URL), ("webhook_url", "https://hooks.example.org/x"),
                           ("directives", "Never email on Sundays."), ("archetype", "saas")):
            set_settings(c, **{key: value})
            new = planning.change_stamp(fresh(c))
            self.assertNotEqual(new, stamp, key)
            stamp = new
        set_settings(c, smtp_host="smtp.example.org", smtp_user="hi@flow.co")
        self.assertEqual(planning.change_stamp(fresh(c)), stamp, "a half-configured mailbox is not a mailbox")
        set_settings(c, smtp_password="secret")
        self.assertNotEqual(planning.change_stamp(fresh(c)), stamp)

    def test_the_settings_form_requests_a_cycle_only_for_what_matters(self):
        c = make_company("PatchCo")
        call("PATCH", f"/api/companies/{c['id']}", {"settings": {"cycle_minutes": 30, "daily_email_limit": 10}})
        self.assertEqual(fresh(c)["cycle_requested"], 0)
        call("PATCH", f"/api/companies/{c['id']}", {"settings": {"postal_address": "1 Main Street"}})
        self.assertEqual(fresh(c)["cycle_requested"], 1)
        db.update("companies", c["id"], cycle_requested=0)
        call("PATCH", f"/api/companies/{c['id']}", {"settings": {"netlify_token": "••••••••"}})   # unchanged secret
        self.assertEqual(fresh(c)["cycle_requested"], 0)
        call("PATCH", f"/api/companies/{c['id']}", {"charter": CHARTER})
        self.assertEqual(fresh(c)["cycle_requested"], 1)
        db.update("companies", c["id"], cycle_requested=0)
        call("PATCH", f"/api/companies/{c['id']}", {"charter": CHARTER})   # the same text again
        self.assertEqual(fresh(c)["cycle_requested"], 0)


# =============================================================== 6. question dedupe

class TestQuestionDedupe(Base):
    """review05/dedupe.py: generic topics (email, account) deduped unrelated questions, and deploy questions in
    other words were queued again."""

    def ask_pair(self, first, second):
        c = make_company("DedupeCo")
        ctx = ctx_for(c, "ceo", kind="cycle")
        self.assertIn("sent to the owner", planning.ask_owner(ctx, first))
        return planning.ask_owner(ctx, second), ctx, c

    def test_different_questions_on_a_generic_topic_are_queued(self):
        for first, second in [
                ("Can you connect a mailbox (SMTP) so we can send outreach emails to roofers?",
                 "Which email address should the landing page show as the contact address?"),
                ("Do you have a Netlify account we can deploy to?",
                 "Do you have a Google Analytics account, or should we skip analytics?"),
                ("What is our postal address for the email footer?", "Do you want weekly email updates or daily?"),
                ("Should we charge roofers $45 per lead or $60 per lead?",
                 "Can we spend $12 to buy the domain rooforyou.com?")]:
            out, ctx, _c = self.ask_pair(first, second)
            self.assertIn("sent to the owner", out, (first, second))
            self.assertIsNone(ctx.waiting_on)

    def test_the_same_deploy_question_in_other_words_is_deduped(self):
        for second in ("Can I put the website online now? It is ready.",
                       "Is it OK to make the landing page public on the internet?",
                       "Where should we host the site?"):
            out, ctx, c = self.ask_pair("Please approve deploying the site to Netlify.", second)
            qid = pending(c, "question")[0]["id"]
            self.assertTrue(out.startswith(f"Already waiting on the owner as #{qid} (deploy, asked"), out)
            self.assertEqual(ctx.waiting_on, qid)

    def test_a_pending_deploy_request_dedupes_them_too(self):
        c = make_company("DeployReqCo")
        workspace.safe_path(c, "site/index.html").write_text("<h1>Hi</h1>", encoding="utf-8")
        res = actions.gate(c, agent_of(c, "engineering"), None, "deploy_site", {"folder": "site"})
        rid = int(res.split("#")[1].split(".")[0])
        ctx = ctx_for(c, "ceo", kind="cycle")
        for q in ("Can I put the website online now?", "Is it OK to make the landing page public on the internet?"):
            out = planning.ask_owner(ctx, q)
            self.assertIn(f"your deploy request #{rid} is already in the owner's inbox", out)
        self.assertEqual(ctx.waiting_on, rid)

    def test_a_generic_topic_with_real_overlap_is_still_one_question(self):
        out, _ctx, _c = self.ask_pair("Can you connect a mailbox so we can send outreach emails?",
                                      "Can I send the outreach emails now?")
        self.assertTrue(out.startswith("Already waiting on the owner as #"), out)
        out, _ctx, _c = self.ask_pair("Which city should we target first: Austin or Dallas?",
                                      "Should we target Austin or Dallas first?")
        self.assertIn("(a similar question, asked", out)

    def test_deploy_words_need_the_site(self):
        self.assertIn("deploy", planning.detect_topics("Can I put the website online now?"))
        self.assertIn("deploy", planning.detect_topics("Make the landing page public?"))
        self.assertIn("deploy", planning.detect_topics("Which hosting should we use?"))
        self.assertNotIn("deploy", planning.detect_topics("Which online communities should we target?"))
        self.assertNotIn("deploy", planning.detect_topics("How do we get more online reviews?"))


# =============================================================== 7. pinned paragraphs and other prompt sizes

class TestPromptSizes(Base):
    """review05/prompt.py: a pinned owner file written as long paragraphs showed only its heading, the owner's
    directives had no cap, and the trim budget ignored the tool specs."""

    def tearDown(self):
        import shutil
        shutil.rmtree(context.global_dir(), ignore_errors=True)
        super().tearDown()

    def test_a_long_pinned_paragraph_is_shown_up_to_the_cap(self):
        c = make_company("PinCo")
        g = context.global_dir()
        g.mkdir(parents=True, exist_ok=True)
        para = "I am a solo founder with a $500 budget, 10 hours a week, and skills in marketing. " * 40
        (g / "owner.md").write_text("# About the owner\n" + para + "\n", encoding="utf-8")
        db.set_settings({"pinned_context": ["owner.md"]})
        sec = context.pinned_section(fresh(c))
        self.assertLessEqual(len(sec), context.PINNED_CAP)
        self.assertIn("## global:owner.md\n# About the owner\nI am a solo founder", sec)
        self.assertGreater(len(sec), 1500)
        self.assertIn(" ...\n[cut for room; read the rest with read_file global:owner.md start_line=2]", sec)
        body = sec.split("# About the owner\n", 1)[1].split(" ...\n", 1)[0]
        self.assertTrue(para.startswith(body), "cut at a word boundary")

    def test_directives_are_capped(self):
        c = make_company("DirCo", directives="Keep costs under $50 a month and never contact homeowners. " * 60)
        prompt = agents.system_prompt(fresh(c), agent_of(c), "task", 14)
        orders = prompt.split("# Owner's standing orders (always follow these)\n", 1)[1].split("\n\n", 1)[0]
        self.assertLessEqual(len(orders), agents.DIRECTIVES_CAP)
        self.assertTrue(orders.endswith("(cut; full text in company Settings)"), orders[-80:])
        short = make_company("ShortDirCo", directives="Be kind.")
        self.assertIn("(always follow these)\nBe kind.", agents.system_prompt(fresh(short), agent_of(short), "task", 14))

    def test_the_trim_budget_leaves_room_for_the_tool_specs(self):
        c = make_company("TrimCo")
        task = make_task(c, "marketing", "Write a note")
        budgets: list = []
        real = agents._trim
        with mock.patch.object(agents, "_trim", lambda msgs, budget: budgets.append(budget) or real(msgs, budget)):
            self.run_agent(c, "marketing", "task", [("finish", {"summary": "ok", "status": "partial"})], task)
        specs = tools.role_tools(agent_of(c, "marketing"), "task")
        want = int(int(db.get_settings().get("num_ctx", 32768)) * 2.6) - len(json.dumps(specs, ensure_ascii=False))
        self.assertEqual(budgets[0], want)


# =============================================================== 8. smaller items

class TestSmallerItems(Base):
    def test_retry_founding_on_a_company_from_before_phase_05(self):
        c = make_company("OldCo")
        cs = db.company_settings(c)
        cs.pop("founding_stage", None)
        db.update("companies", c["id"], settings=json.dumps(cs), charter=CHARTER)
        self.assertEqual(planning.founding_stage(fresh(c)), "done")
        workspace.safe_path(c, "research/market.md").write_text("# Market\n", encoding="utf-8")
        with captured_starts() as started:
            self.assertTrue(agents.found(c["id"]))
        self.assertEqual(db.company_settings(fresh(c))["founding_stage"], "plan")   # not research again
        self.assertEqual((fresh(c)["status"], len(started)), ("founding", 1))
        # a company still being founded without the setting starts at research, as before
        mid = make_company("MidCo", status="founding")
        cs = db.company_settings(mid)
        cs.pop("founding_stage", None)
        db.update("companies", mid["id"], settings=json.dumps(cs))
        self.assertEqual(planning.founding_stage(fresh(mid)), "research")

    def test_the_budget_never_blocks_a_cycle_the_owner_asked_for(self):
        c = make_company("BudgetAskCo", daily_token_budget=10)
        add_run(c, kind="task", tokens=500, started=db.now())
        db.update("companies", c["id"], last_cycle_at=ago(120))
        starts: list = []
        sched = scheduler.Scheduler()
        with mock.patch.object(agents, "start", lambda aid, kind, **kw: starts.append(kind) or True):
            sched._maybe_cycle(fresh(c))
            self.assertEqual(starts, [], "background planning waits for tomorrow")
            db.update("companies", c["id"], cycle_requested=1, last_cycle_at=ago(5))
            sched._maybe_cycle(fresh(c))
            self.assertEqual(starts, ["cycle"])

    def test_audit_refreshes_records_after_its_row_exists(self):
        c = make_company("AuditCo")
        seen: list = []
        with mock.patch.object(actions, "_records_changed", lambda cid: seen.append(
                db.scalar("SELECT COUNT(*) FROM audit WHERE company_id=?", (cid,)))):
            actions.audit(c["id"], "deploy_site", "Forge", "owner", {"folder": "site"}, "Website deployed: x")
        self.assertEqual(seen, [1])

    def test_finish_repeated_after_a_refusal_is_accepted_not_looped(self):
        c = make_company("LoopCo")
        task = make_task(c, "marketing", "Write the FAQ", deliverable="marketing/faq.md")
        same = ("finish", {"summary": "FAQ written.", "deliverable": "marketing/faq.md"})
        results, run, _s = self.run_agent(c, "marketing", "task", [same, same], task)
        self.assertTrue(results[0][1].startswith("Not finished yet"), results[0][1])
        self.assertEqual(results[1][1], "Session ended.")
        self.assertEqual(run["status"], "partial")
        self.assertNotIn("repeating the same action", run["summary"])


class TestCycleHeader(Base):
    def test_an_owner_requested_cycle_never_says_nothing_changed(self):
        c = make_company("HeaderAskCo")
        planning.cycle_started(fresh(c))
        planning.cycle_started(fresh(c))   # nothing changed since the last start
        self.assertNotIn("Nothing has changed", planning.cycle_header(fresh(c), requested=True))
        planning.cycle_started(fresh(c), requested=True)
        self.assertNotIn("Nothing has changed", planning.cycle_header(fresh(c)))

    def test_the_answer_is_used_once_and_computed_fresh(self):
        c = make_company("HeaderFreshCo")
        planning.cycle_started(fresh(c))   # first cycle: everything is new
        self.assertNotIn("Nothing has changed", planning.cycle_header(fresh(c)))
        # a later cycle that did not go through cycle_started compares the live stamp, not the old answer
        self.assertIn("Nothing has changed", planning.cycle_header(fresh(c)))

    def test_plan_now_and_owner_answers_reach_the_prompt_as_requested(self):
        c = make_company("PlanNowCo")
        planning.cycle_started(fresh(c))
        planning.cycle_header(fresh(c))
        with captured_starts() as started:
            out = call("POST", f"/api/companies/{c['id']}/cycle", {})
        self.assertTrue(out["ok"])
        self.assertEqual(started[0][1:], ("cycle", {"requested": True}))
        _r, _run, s = self.run_agent(c, "ceo", "cycle", [("finish", {"summary": "ok", "status": "waiting"})],
                                     requested=True)
        self.assertNotIn("Nothing has changed", s.calls[0]["user"])
        # a scheduler cycle with nothing new does say it
        planning.cycle_started(fresh(c))
        _r, _run, s = self.run_agent(c, "ceo", "cycle", [("finish", {"summary": "ok", "status": "waiting"})])
        self.assertIn("Nothing has changed", s.calls[0]["user"])
        # an owner's answer sets cycle_requested; the scheduler's cycle is then an owner-requested one
        qid = question(c, "Phoenix or Tucson?")
        actions.decide(qid, "approve", answer="Phoenix")
        with mock.patch.object(agents, "start", lambda aid, kind, **kw: True):
            db.update("companies", c["id"], last_cycle_at=ago(5))
            scheduler.Scheduler()._maybe_cycle(fresh(c))
        self.assertTrue(planning._cycle_flags[c["id"]][2])
        self.assertNotIn("Nothing has changed", planning.cycle_header(fresh(c)))


# =============================================================== 9. founding stage deliverables

class TestFoundingStageChecks(Base):
    MARKET = ("# Market\n\n## Customers\nIndependent roofers in Phoenix with 2 to 15 crews buy leads today.\n\n"
              "## Prices\n- LeadStar: $35 per exclusive lead (source: https://leadstar.com/pricing)\n"
              "- Shared leads: about $12 each (source: https://leadstar.com/pricing)\n\n## Gaps\n- Demand volume\n")

    def founding(self, stage, **extra) -> dict:
        c = make_company("StageFlowCo", status="founding", founding_stage=stage, **extra)
        db.update("companies", c["id"], stage="idea", charter="", mission="")
        return fresh(c)

    def run_stage(self, c, steps):
        with captured_starts(), mock.patch.object(web, "fetch_page", lambda url, include_links=False: PAGE_TEXT), \
                mock.patch.object(web, "search", lambda q, n, s: [{"title": "LeadStar", "url": PAGE_URL,
                                                                    "snippet": "roofing leads"}]):
            return self.run_agent(c, "ceo", "founding", steps)

    def test_research_must_save_market_md_and_hears_which_pages_it_fetched(self):
        c = self.founding("research")
        results, run, _s = self.run_stage(c, [
            ("fetch_url", {"url": PAGE_URL}),
            ("finish", {"summary": "Drafted research/market.md with competitor prices."}),
            ("write_file", {"path": "research/market.md", "content": self.MARKET}),
            ("finish", {"summary": "Saved research/market.md with competitor prices."})])
        refusal = results[1][1]
        self.assertTrue(refusal.startswith("Not finished yet: research/market.md does not exist."), refusal)
        self.assertIn(f"Pages this company fetched: {PAGE_URL}", refusal)
        self.assertEqual((run["status"], results[3][1]), ("done", "Session ended."))
        self.assertEqual(db.company_settings(fresh(c))["founding_stage"], "charter")
        self.assertEqual(db.company_settings(fresh(c)).get("founding_research_note"), "")

    def test_a_research_stage_that_still_saves_nothing_moves_on_with_a_note(self):
        c = self.founding("research")
        results, run, s = self.run_stage(c, [
            ("fetch_url", {"url": PAGE_URL}),
            ("finish", {"summary": "Drafted research/market.md."})])   # then silence
        self.assertTrue(results[1][1].startswith("Not finished yet"))
        self.assertEqual(len(s.calls), 3, "silence after the refusal ends the stage instead of burning steps")
        self.assertEqual(run["status"], "done")
        self.assertIn("Stage check: research/market.md does not exist.", run["summary"])
        self.assertEqual(db.company_settings(fresh(c))["founding_stage"], "charter")
        prompt = agents.founding_prompt(fresh(c), "")
        self.assertIn("Research ended without saving research/market.md", prompt)

    def test_research_checkpoint_halfway_when_nothing_is_saved(self):
        c = self.founding("research")
        _r, run, s = self.run_stage(c, [lambda n: ("web_search", {"query": f"roofing leads {n}"})] * 40)
        self.assertEqual(len(s.calls), planning.STAGE_STEPS["research"])
        transcript = json.loads(run["transcript"])
        idx = [i for i, m in enumerate(transcript) if m["role"] == "user" and m["content"] == planning.RESEARCH_CHECKPOINT]
        self.assertEqual(len(idx), 1)
        before = sum(1 for m in transcript[:idx[0]] if m["role"] == "assistant")
        self.assertEqual(before, planning.STAGE_STEPS["research"] // 2)
        self.assertIn("Save what you found so far to research/market.md now, each fact with the URL it came from, "
                      "then continue.", planning.RESEARCH_CHECKPOINT)
        # with the file saved early, no checkpoint
        c2 = self.founding("research")
        _r, run, _s = self.run_stage(c2, [("write_file", {"path": "research/market.md", "content": self.MARKET})]
                                     + [lambda n: ("web_search", {"query": f"roofing {n}"})] * 40)
        self.assertNotIn(planning.RESEARCH_CHECKPOINT, run["transcript"])

    def test_the_research_prompt_asks_to_save_early_and_record_facts(self):
        p = agents.founding_prompt(self.founding("research"), "")
        self.assertIn("Write research/market.md early", p)
        self.assertIn("append to it as you go", p)
        self.assertIn("Record prices and other key numbers with record_fact", p)
        self.assertNotIn("—", p)

    def test_critic_and_charter_stages(self):
        c = self.founding("critic")
        db.update("companies", c["id"], charter=CHARTER)
        results, run, _s = self.run_stage(fresh(c), [("finish", {"summary": "Critique done."}),
                                                     ("finish", {"summary": "Critique done."})])
        self.assertTrue(results[0][1].startswith("Not finished yet: docs/critique.md does not exist."))
        self.assertIn("Stage check: docs/critique.md does not exist.", run["summary"])
        c = self.founding("charter")
        results, _run, _s = self.run_stage(c, [("finish", {"summary": "Charter saved.", "status": "partial"})])
        self.assertTrue(results[0][1].startswith("Not finished yet: the company record has no complete charter "
                                                 "yet (missing or empty: One-liner"), results[0][1])
        c = self.founding("charter")
        # Phase 2: the charter stage also writes the brain (the strategy and a goal) before it finishes
        goal = {"title": "20 signups", "metric": "signups", "target": 20,
                "deadline": (_dt.date.today() + _dt.timedelta(days=30)).isoformat()}
        results, run, _s = self.run_stage(c, [("update_charter", {"charter": CHARTER}), ("set_strategy", STRATEGY),
                                              ("set_goal", goal), ("finish", {"summary": "Charter saved."})])
        self.assertEqual((results[3][1], run["status"]), ("Session ended.", "done"))

    def test_the_plan_needs_three_tasks(self):
        c = self.founding("plan")
        db.update("companies", c["id"], charter=CHARTER)
        task = lambda title, role: ("create_task", {"title": title, "assign_to": role,  # noqa: E731
                                                    "deliverable": f"{role}/{title[:8]}.md",
                                                    "acceptance": "- one\n- two\n- three"})
        results, run, _s = self.run_stage(fresh(c), [
            task("Call 10 roofers", "marketing"), task("Build the request page", "engineering"),
            ("finish", {"summary": "Planned the first week."}),
            task("Count Phoenix roofers", "research"),
            ("finish", {"summary": "Planned the first week."})])
        self.assertEqual(results[2][1], "Not finished yet: the first week has 2 tasks; it needs at least 3. Create "
                                        "the rest with create_task across the team, each with a deliverable and "
                                        "acceptance criteria, then call finish again.")
        self.assertEqual((results[4][1], run["status"]), ("Session ended.", "done"))
        self.assertEqual(fresh(c)["status"], "active")


# =============================================================== 10. fetched pages are searchable and readable

class TestFetchedPages(Base):
    """The real run: research fetched the LeadStar page, and the charter stage's search for "LeadStar" found
    nothing."""

    def setUp(self):
        super().setUp()
        self.c = make_company("PageCo")
        self.ctx = ctx_for(self.c, "research")
        with mock.patch.object(web, "fetch_page", lambda url, include_links=False: PAGE_TEXT):
            tools.execute(self.ctx, "fetch_url", {"url": PAGE_URL})

    def test_search_finds_a_fetched_page_and_read_file_opens_it(self):
        context.reindex(self.c)
        rows = context.search_rows(self.c, "LeadStar exclusive")
        self.assertEqual((rows[0]["source"], rows[0]["path"]), ("page", f"page:{PAGE_URL}"))
        self.assertEqual(rows[0]["line"], 5)
        self.assertIn("LeadStar pricing for roofers", rows[0]["heading"])
        self.assertEqual({r["source"] for r in context.search_rows(self.c, "LeadStar", "pages")}, {"page"})
        self.assertFalse([r for r in context.search_rows(self.c, "LeadStar", "files") if r["source"] == "page"])
        # the charter stage's own search tool
        founding = ctx_for(self.c, "ceo", kind="founding")
        founding.allowed = set(roles.founding_tools("charter"))
        out = tools.execute(founding, "search", {"query": "LeadStar"})
        self.assertIn("Web pages your team fetched: open one with read_file page:<url> start_line=<line>", out)
        self.assertIn(f"page:{PAGE_URL}:", out)
        read = tools.execute(founding, "read_file", {"path": f"page:{PAGE_URL}", "start_line": 5})
        self.assertIn("5: LeadStar charges roofers $35 per exclusive lead in Phoenix.", read)
        self.assertTrue(read.startswith(f"page:{PAGE_URL}: lines 5-"), read)
        # the same page spelled a little differently
        self.assertIn("LeadStar charges", tools.execute(founding, "read_file", {"path": "page:https://www.leadstar.com/pricing/"}))
        missing = tools.execute(founding, "read_file", {"path": "page:https://nowhere.example.org/x"})
        self.assertTrue(missing.startswith("ERROR: https://nowhere.example.org/x has not been fetched"), missing)
        self.assertTrue(tools.execute(self.ctx, "edit_file", {"path": f"page:{PAGE_URL}", "find": "a",
                                                              "replace": "b"}).startswith("ERROR: page:"))
        self.assertIn("web pages your team already fetched", tools.REGISTRY["search"].description)

    def test_error_pages_and_expired_pages_are_not_indexed(self):
        bad = "https://blocked.example.com/pricing"
        sha = "ab" * 32
        (context._pages_dir(self.c) / f"{sha}.txt").write_text("HTTP 403 fetching x\nForbidden zebrafence\n",
                                                               encoding="utf-8")
        db.run("INSERT INTO fetched(company_id, url, sha, fetched_at) VALUES(?,?,?,?)",
               (self.c["id"], bad, sha, db.now()))
        context.reindex(self.c)
        self.assertEqual(context.search_rows(self.c, "zebrafence"), [])
        self.assertTrue(context.search_rows(self.c, "LeadStar", "pages"))
        for f in context._pages_dir(self.c).glob("*.txt"):
            f.unlink()   # the 7-day prune
        context.reindex(self.c)
        self.assertEqual(context.search_rows(self.c, "LeadStar", "pages"), [])

    def test_no_change_reindex_skips_the_pages(self):
        context.reindex(self.c)
        self.assertEqual(context._sync_pages(self.c["id"], self.c), 0)
        t0 = time.perf_counter()
        for _ in range(3):
            context.reindex(self.c)
        self.assertLess((time.perf_counter() - t0) / 3, 0.5)
        with mock.patch.object(context, "_page_rows", side_effect=AssertionError("pages looked at again")):
            context.reindex(self.c)

    def test_search_without_fts_still_finds_pages(self):
        with mock.patch.object(context, "_fts_ok", lambda: False):
            rows = context.search_rows(self.c, "LeadStar", "pages")
        self.assertEqual(rows[0]["path"], f"page:{PAGE_URL}")


# =============================================================== charter variants (review05/charter.py)

class TestCharterVariants(unittest.TestCase):
    GPTOSS = """# Rooforyou - Company Charter

## 1. One‑Liner
Rooforyou connects Austin homeowners who need roof repairs with vetted local roofers, charging roofers per lead.

## 2. Problem
Homeowners struggle to find trustworthy roofers; roofers pay for shared, low-quality leads on Angi.

## 3. Target Customer
- **Primary (payer):** Independent roofing contractors in Austin with 2–20 crews.

## 4. Offer & Pricing
Exclusive, pre-qualified leads delivered by SMS. **Price:** $45 per lead (test range $35–$60).

## 5. Business Model
Roofers pay per accepted lead. Unit economics (estimate): $45 revenue minus $18 ad cost per lead.

## 6. Go‑to‑Market
Google Local Services ads, Nextdoor, roofing supply store flyers, cold calls to roofers.

## 7. Competitors & How We're Different
| Competitor | Price | Our edge |
|---|---|---|
| Angi | $15–$100 shared lead | Exclusive leads |
| HomeAdvisor | $15–$80 shared | Local vetting |
| Thumbtack | pay per contact | Pre-qualified |

## 8. Riskiest Assumption & Test
Roofers will pay for exclusive leads. **Pass:** at least 3 of 10 roofers agree to a paid pilot at $45/lead.

## 9. North‑Star Metric
Accepted leads per week.

## 10. 30‑Day Plan
- **Week 1:** Interview 10 roofers.
- **Week 2:** Launch landing page.
"""

    def test_what_local_models_write_passes(self):
        self.assertEqual(planning.validate_charter(self.GPTOSS), [])
        for heading in ("## 10. Thirty-Day Roadmap", "## 10. Launch Plan (Weeks 1–4)", "## 10. First four weeks"):
            self.assertEqual(planning.validate_charter(self.GPTOSS.replace("## 10. 30‑Day Plan", heading)), [],
                             heading)
        words = self.GPTOSS.replace("at least 3 of 10 roofers", "at least three out of ten roofers")
        self.assertEqual(planning.validate_charter(words), [])

    def test_what_is_really_missing_is_still_refused(self):
        inline = self.GPTOSS.split("| Competitor |")[0] + ("Angi, HomeAdvisor and Thumbtack all sell shared leads; "
                                                           "we sell exclusive ones.\n\n## 8." +
                                                           self.GPTOSS.split("## 8.", 1)[1])
        self.assertEqual(planning.missing_names(planning.validate_charter(inline)), ["Competitors"])
        tbd = self.GPTOSS.replace("**Price:** $45 per lead (test range $35–$60).", "Price to be decided later.")
        self.assertEqual(planning.validate_charter(tbd), ["Offer and price (needs a price, or a price range to test)"])
        vague = self.GPTOSS.replace("at least 3 of 10 roofers agree", "plenty of roofers agree").replace("$45/lead", "")
        self.assertEqual(planning.validate_charter(vague),
                         ["Riskiest assumption (needs a pass threshold: a number or percentage)"])


if __name__ == "__main__":
    unittest.main()
