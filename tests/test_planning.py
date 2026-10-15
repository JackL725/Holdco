"""Phase 0.5 planning tests: one open question per topic, change-driven planning cycles, the daily token budget,
charter validation and the one charter path, staged founding with the critic, and the business playbooks.

Run all tests with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import contextlib
import datetime as _dt
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
os.environ["HOLDCO_DATA"] = tempfile.mkdtemp(prefix="holdco-planning-")

from holdco import (actions, agents, backup, bus, db, doctor, llm, notify, planning, playbooks, roles,  # noqa: E402
                    scheduler, tools, watchdog, workspace)
from tests.fake_ollama import CHARTER as FAKE_CHARTER, FakeModelServer  # noqa: E402

db.init()

# Rooforyou's real charter from the owner's first run: no price, no competitors, no riskiest assumption, no
# channels, no business model and no 30-day plan. It must never pass again.
ROOFORYOU_CHARTER = """# Rooforyou Charter

## Company Overview
- **Name**: Rooforyou
- **One‑liner**: A subscription service that delivers verified roofing leads directly to Phoenix roofers, helping them grow faster with minimal effort.
- **Stage**: Idea

## Problem
Independent roofers in Phoenix, Arizona struggle to find reliable, high‑quality leads, spending significant time and money on costly marketing and cold outreach with low conversion rates.

## Solution
Rooforyou provides a subscription-based platform that sources and verifies roofing leads, delivering them directly to roofers' inboxes or dashboards. The service filters out low‑quality inquiries, ensuring roofers focus only on high‑intent prospects.

## Target Market
Independent roofing contractors, small roofing teams, and roofing service providers operating in the Phoenix, Arizona region who need a steady stream of local, intent‑based leads.

## Core Team
- **Atlas** – Chief Executive
- **Forge** – Web Engineer
- **Echo** – Growth Marketer

## Value Proposition
Rapidly scale roofing operations by receiving ready‑to‑convert leads, cutting acquisition costs, freeing time for installation work, and boosting revenue predictability.

## Legal Status
Limited Liability Company (LLC) – pending registration.

## North Star Metric
Number of qualified roofing leads delivered to roofers per week (≥10 per week).

## Objectives
- Achieve a consistent weekly lead delivery volume of at least 10 qualified leads per roofer.
- Improve lead qualification accuracy by 20% over the next quarter.
"""

# A good charter written the way local models write them: synonyms, ampersands, non-breaking hyphens.
SYNONYM_CHARTER = """# Roofy

## One‑liner
We help roofing contractors win more jobs by delivering exclusive homeowner leads.

## Who it's for
Small and mid-size roofing contractors with 2 to 15 crews in Phoenix.

## The problem
Shared leads from big directories are costly and convert badly; roofers chase the same homeowner.

## Offer & Pricing
Exclusive leads to test at $25 to $40 per lead, first 10 leads free as a pilot.

## Go‑to‑Market
Homeowner demand from local search ads and storm-damage pages; roofers reached by phone and email.

## Competitors & how we differ
- Angi: shared leads, roofers complain about price
- HomeAdvisor: shared leads, monthly fees
- Google Local Services Ads: pay per lead, roofers compete on reviews

## Revenue model
Roofers pay per accepted lead; estimate: lead cost $12, price $30, margin $18 per lead.

## Riskiest Assumption & Test
We can produce homeowner inquiries for under $15 each: pass if 20 inquiries cost under $300.

## North Star Metric
Leads accepted by paying roofers per week.

## First 30 days
- Week 1: call 10 roofers about what they pay per lead
- Week 2: homeowner request page for one service
- Week 3: small ad test
- Week 4: compare cost per inquiry with the threshold
"""


def make_company(name="PlanCo", members=("ceo", "engineering", "marketing"), status="active", idea="An idea",
                 **settings) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, idea, status, "building", json.dumps(settings), db.now()))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return db.get_company(cid)


def agent(company, role="ceo") -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def ctx_for(company, role="ceo", kind="task", task=None) -> tools.Ctx:
    a = agent(company, role)
    c = tools.Ctx(company=db.get_company(company["id"]), agent=a, kind=kind, run_id=0, task=task,
                  settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(a, kind)} | {"ask_owner", "write_file", "update_charter"}
    return c


def add_task(company, title="Build landing page", role="engineering", status="todo", created_by="Atlas") -> dict:
    a = agent(company, role)
    ts = db.now()
    tid = db.run("INSERT INTO tasks(company_id, title, role, agent_id, status, priority, created_by, created_at, "
                 "updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 (company["id"], title, role, a["id"] if a else None, status, 2, created_by, ts, ts))
    return db.get_task(tid)


def pending_questions(company) -> list[dict]:
    rows = db.q("SELECT * FROM approvals WHERE company_id=? AND action='question' AND status='pending' ORDER BY id",
                (company["id"],))
    for r in rows:
        r["p"] = db.loads(r["payload"], {}) or {}
    return rows


def ago(minutes: float) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fresh(company) -> dict:
    return db.get_company(company["id"])


class Scripted:
    """Stands in for llm.chat: answers each call with the next (tool, args), records what each call was given."""

    def __init__(self, steps=()):
        self.steps = list(steps)
        self.calls: list[dict] = []

    def __call__(self, messages, tools_spec=None, model=None, settings=None, **kw):
        self.calls.append({"model": model, "tools": [t["name"] for t in tools_spec or []],
                           "user": messages[1]["content"], "system": messages[0]["content"], "think": kw.get("think")})
        if not self.steps:
            return llm.Reply(content="")
        step = self.steps.pop(0)
        if callable(step):
            step = step(len(self.calls))
        name, args = step
        return llm.Reply(tool_calls=[llm.ToolCall(name=name, arguments=args)], tokens=100)


@contextlib.contextmanager
def captured_starts():
    """agents.start records instead of starting a thread, so a test drives each founding stage itself."""
    started: list = []
    with mock.patch.object(agents, "start", lambda aid, kind, **kw: started.append((aid, kind, kw)) or True):
        yield started


# =============================================================== owner questions

class TestQuestions(unittest.TestCase):
    def setUp(self):
        self.c = make_company("AskCo")
        self.ctx = ctx_for(self.c, "ceo", kind="cycle")

    def ask(self, question, **kw):
        return planning.ask_owner(self.ctx, question, **kw)

    def test_structured_options_reach_the_payload(self):
        out = self.ask("Which should we set up first?", options="- A company mailbox\n2. A domain\n* A small ad budget",
                       recommended="A company mailbox", context="x" * 5000)
        self.assertIn("sent to the owner", out)
        q = pending_questions(self.c)[0]
        self.assertEqual(q["p"]["options"], ["A company mailbox", "A domain", "A small ad budget"])
        self.assertEqual(q["p"]["recommended"], "A company mailbox")
        self.assertEqual(len(q["p"]["context"]), planning.CONTEXT_CAP)
        self.assertEqual(set(q["p"]["topics"]), {"email", "domain", "money"})
        # the phone message shows the options as buttons with the recommendation marked
        text, buttons, kind = notify.build_approval(q)
        self.assertEqual(len(buttons), 3)
        self.assertIn("Recommended: A company mailbox", text)

    def test_options_are_capped_and_a_stray_recommendation_is_dropped(self):
        self.ask("What should the brand voice be?", options="\n".join(f"Voice {i}" for i in range(1, 10)),
                 recommended="Something else")
        p = pending_questions(self.c)[0]["p"]
        self.assertEqual(p["options"], [f"Voice {i}" for i in range(1, 7)])
        self.assertNotIn("recommended", p)
        self.assertEqual(planning.parse_options('["Yes", "No", "Yes"]'), ["Yes", "No"])
        self.assertEqual(planning.parse_options("Yes | No | Later"), ["Yes", "No", "Later"])
        self.assertTrue(self.ask("  ").startswith("ERROR:"))

    def test_the_same_topic_is_refused_with_the_pending_number(self):
        first = self.ask("Approve deploying to Netlify and sending outreach emails?")
        qid = pending_questions(self.c)[0]["id"]
        self.assertIn(f"#{qid}", first)
        out = self.ask("Can I send the outreach emails now?")
        self.assertTrue(out.startswith(f"Already waiting on the owner as #{qid} (outreach, asked "), out)
        self.assertIn("ago). Do not ask again.", out)
        self.assertIn("finish with status 'waiting'", out)
        out = self.ask("Should we publish the website today?")
        self.assertTrue(out.startswith(f"Already waiting on the owner as #{qid} (deploy, asked "), out)
        self.assertEqual(len(pending_questions(self.c)), 1)
        events = db.q("SELECT * FROM events WHERE company_id=? AND kind='question_deduped' ORDER BY id",
                      (self.c["id"],))
        self.assertEqual(len(events), 2)
        data = db.loads(events[0]["data"], {})
        self.assertEqual((data["reason"], data["approval_id"]), ("topic", qid))

    def test_rooforyous_question_asked_again_and_again_stays_one(self):
        # The real run asked this 38 times, worded a little differently each cycle (identical payloads were
        # already caught by actions.gate).
        for i, q in enumerate(["Do you approve deploying the landing page to Netlify and sending the outreach emails?",
                               "Reminder: can we deploy the site and start outreach?",
                               "Please approve the Netlify deployment so we can go live.",
                               "May we send the first outreach emails to roofers?",
                               "Following up on deployment approval and email outreach."]):
            self.ask(q, context=f"cycle {i}")
        self.assertEqual(len(pending_questions(self.c)), 1)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM events WHERE company_id=? AND kind='question_deduped'",
                                   (self.c["id"],)), 4)

    def test_word_overlap_catches_a_rephrased_question(self):
        self.ask("Which dog breeds should the first meal plans focus on?")
        out = self.ask("Which dog breeds should our first meal plans focus on?")
        self.assertIn("(a similar question, asked", out)
        self.assertIn("sent to the owner", self.ask("Should the brand voice be playful or formal?"))
        self.assertEqual(len(pending_questions(self.c)), 2)
        self.assertGreaterEqual(planning.jaccard(planning.content_words("Which breeds should plans focus on"),
                                                 planning.content_words("Which breeds do the plans focus on")), 0.5)

    def test_a_pending_approval_blocks_a_duplicate_question(self):
        workspace.safe_path(self.c, "site/index.html").write_text("<h1>Hi</h1>", encoding="utf-8")
        res = actions.gate(self.c, agent(self.c, "engineering"), None, "deploy_site", {"folder": "site"})
        rid = int(res.split("#")[1].split(".")[0])
        out = self.ask("May I deploy the site to Netlify now?")
        self.assertTrue(out.startswith(f"Already waiting on the owner as #{rid} (deploy, asked"), out)
        self.assertIn(f"your deploy request #{rid} is already in the owner's inbox", out)
        self.assertEqual(pending_questions(self.c), [])
        ev = db.one("SELECT data FROM events WHERE company_id=? AND kind='question_deduped'", (self.c["id"],))
        self.assertEqual(db.loads(ev["data"], {})["reason"], "approval")

    def test_at_most_three_agent_questions_and_watchdog_questions_do_not_count(self):
        watchdog.escalate(self.c, "stalled", "Agents at AskCo are stuck. What should we do?", watchdog.PAUSE_OPTIONS,
                          "Pause the company", key=f"stalled:{self.c['id']}")
        for q in ("What should the brand voice be?", "Which city do we start in?", "Who is our first hire?"):
            self.assertIn("sent to the owner", self.ask(q))
        out = self.ask("Should the logo be blue or green?")
        self.assertTrue(out.startswith("ERROR: 3 questions from the team are already waiting"), out)
        self.assertIn("Which city do we start in?", out)
        self.assertEqual(len(pending_questions(self.c)), 4)   # three from agents plus the watchdog's
        ev = db.q("SELECT data FROM events WHERE company_id=? AND kind='question_deduped'", (self.c["id"],))
        self.assertEqual(db.loads(ev[-1]["data"], {})["reason"], "cap")

    def test_an_answer_frees_the_topic(self):
        self.ask("Approve deploying the site?")
        qid = pending_questions(self.c)[0]["id"]
        actions.decide(qid, "approve", answer="Yes, deploy it")
        self.assertIn("sent to the owner", self.ask("Approve deploying the new pricing page?"))

    def test_the_tool_goes_through_the_claims_guard_then_here(self):
        ctx = ctx_for(self.c, "ceo", kind="cycle")
        out = tools.execute(ctx, "ask_owner", {"question": "Approve deploying the site?",
                                               "options": "Deploy now\nWait", "recommended": "Deploy now"})
        self.assertIn("sent to the owner", out)
        again = tools.execute(ctx, "ask_owner", {"question": "Can we go live with the site?"})
        self.assertTrue(again.startswith("Already waiting on the owner as #"), again)

    def test_topics(self):
        self.assertEqual(planning.detect_topics("Add the GA4 measurement ID?"), ["analytics"])
        self.assertEqual(planning.detect_topics("Register the domain and set DNS?"), ["domain"])
        self.assertEqual(planning.detect_topics("Can I have the API key for the login?"), ["account"])
        self.assertEqual(planning.detect_topics("Should we form an LLC first?"), ["legal"])
        self.assertEqual(planning.detect_topics("What price should we charge?"), ["pricing"])
        self.assertEqual(planning.detect_topics("Which colour for the logo?"), [])
        self.assertEqual(planning.topic_label(["deploy", "email"]), "deploy and outreach")


# =============================================================== effort follows change

class TestChangeDrivenPlanning(unittest.TestCase):
    def setUp(self):
        self.c = make_company("CycleCo")
        self.sent: list = []
        self._p = mock.patch.object(notify, "send_later", lambda text, buttons=None, kind="info", **kw:
                                    self.sent.append((text, kind)))
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def started(self, company=None):
        company = fresh(company or self.c)
        planning.cycle_started(company)
        db.update("companies", company["id"], last_cycle_at=db.now())
        return fresh(company)

    def test_owner_request_always_cycles(self):
        c = self.started()
        self.assertTrue(planning.should_cycle(c, False, False, True))

    def test_idle_only_when_something_changed(self):
        c = self.started()
        self.assertFalse(planning.should_cycle(fresh(c), False, True, False))
        db.run("INSERT INTO messages(company_id, agent_id, sender, content, created_at) VALUES(?,?,?,?,?)",
               (c["id"], None, "owner", "Any news?", db.now()))
        self.assertTrue(planning.should_cycle(fresh(c), False, True, False))

    def test_what_counts_as_a_change(self):
        c = self.started()
        stamp = planning.change_stamp(c)
        bus.emit(c["id"], "done", "finished: planned", agent(c)["id"], {"run_id": 1, "task_id": None})
        bus.emit(c["id"], "file", "created docs/x.md", agent(c)["id"], {"path": "docs/x.md"})
        bus.emit(c["id"], "task", "created task #1", agent(c)["id"], {"task_id": 1})
        self.assertEqual(planning.change_stamp(c), stamp, "a cycle's own finish and agent file writes are not news")
        bus.emit(c["id"], "file", "edited docs/x.md", None, {"path": "docs/x.md", "by": "owner"})
        self.assertNotEqual(planning.change_stamp(c), stamp)
        stamp = planning.change_stamp(c)
        bus.emit(c["id"], "done", "finished: built", agent(c, "engineering")["id"], {"run_id": 2, "task_id": 7})
        self.assertNotEqual(planning.change_stamp(c), stamp)
        stamp = planning.change_stamp(c)
        bus.emit(c["id"], "email", "New email from a@b.co", None)
        self.assertNotEqual(planning.change_stamp(c), stamp)
        stamp = planning.change_stamp(c)
        add_task(c, status="blocked")
        self.assertNotEqual(planning.change_stamp(c), stamp)

    def test_due_skips_and_backs_off_while_nothing_changes(self):
        # Everything waits on the owner (a question and a task waiting for it): no cycle, only a longer wait.
        # (With nothing open and nothing waiting, the safety net plans instead; see tests/test_phase05_flow.py.)
        add_task(self.c, "Deploy the site", "engineering", status="waiting")
        actions.gate(self.c, agent(self.c), None, "question", {"question": "May we deploy the site?"})
        c = self.started()
        db.update("companies", c["id"], last_cycle_at=ago(61))
        self.assertFalse(planning.should_cycle(fresh(c), True, False, False))
        self.assertEqual(fresh(c)["cycle_backoff"], 1)
        self.assertFalse(planning.should_cycle(fresh(c), True, False, False))
        self.assertEqual(fresh(c)["cycle_backoff"], 1, "the next look waits for the doubled interval")
        self.assertAlmostEqual(planning.next_due_minutes(fresh(c)), 59, delta=1.5)
        db.update("companies", c["id"], last_cycle_at=ago(121))
        planning.should_cycle(fresh(c), True, False, False)
        self.assertEqual(fresh(c)["cycle_backoff"], 2)
        db.update("companies", c["id"], last_cycle_at=ago(10 ** 6), cycle_backoff=planning.MAX_BACKOFF)
        self.assertFalse(planning.should_cycle(fresh(c), True, False, False))
        self.assertEqual(fresh(c)["cycle_backoff"], planning.MAX_BACKOFF)
        # any change resets the backoff and lets the due cycle run
        actions.decide(int(actions.gate(c, agent(c), None, "question", {"question": "Pick a city?"})
                           .split("#")[1].split(" ")[0]), "approve", answer="Phoenix")
        self.assertTrue(planning.should_cycle(fresh(c), True, False, False))
        self.assertEqual(fresh(c)["cycle_backoff"], 0)

    def test_cycle_started_records_the_stamp_and_resets_the_backoff(self):
        db.update("companies", self.c["id"], cycle_backoff=5)
        c = self.started()
        self.assertEqual(planning.stamp_record(c)["s"], planning.change_stamp(c))
        self.assertEqual(c["cycle_backoff"], 0)

    def test_a_cycle_does_not_trigger_the_next_one_by_itself(self):
        c = self.started()
        ceo = agent(c)
        # the cycle runs: it creates a task, asks a question and cancels something
        db.run("INSERT INTO runs(company_id, agent_id, kind, status, started_at, finished_at) VALUES(?,?,?,?,?,?)",
               (c["id"], ceo["id"], "cycle", "running", db.now(), None))
        add_task(c, "Ask roofers what they pay", "marketing", status="waiting")
        actions.gate(c, ceo, None, "question", {"question": "Which city first?"})
        db.run("UPDATE runs SET status='waiting', finished_at=? WHERE company_id=? AND kind='cycle'",
               (db.now(), c["id"]))
        self.assertNotEqual(planning.change_stamp(c), planning.stamp_record(fresh(c))["s"])
        self.assertFalse(planning.should_cycle(fresh(c), False, True, False))
        self.assertFalse(planning.should_cycle(fresh(c), False, True, False))
        # real news afterwards still counts
        bus.emit(c["id"], "email", "New email from a roofer", None)
        self.assertTrue(planning.should_cycle(fresh(c), False, True, False))

    def test_news_during_a_cycle_is_not_absorbed(self):
        c = self.started()
        ceo = agent(c)
        db.run("INSERT INTO runs(company_id, agent_id, kind, status, started_at, finished_at) VALUES(?,?,?,?,?,?)",
               (c["id"], ceo["id"], "cycle", "running", db.now(), None))
        add_task(c, "Something new", "marketing", status="waiting")
        bus.emit(c["id"], "done", "finished: research", agent(c, "engineering")["id"], {"run_id": 9, "task_id": 3})
        db.run("UPDATE runs SET status='done', finished_at=? WHERE company_id=? AND kind='cycle'", (db.now(), c["id"]))
        self.assertTrue(planning.should_cycle(fresh(c), False, True, False))

    def test_still_waiting_notice_once_a_day(self):
        c = self.started()
        add_task(c, "Deploy the site", "engineering", status="waiting")
        actions.gate(c, agent(c), None, "question", {"question": "Approve the deploy?"})
        c = self.started()   # the baseline includes what waits
        for _ in range(3):
            self.assertFalse(planning.should_cycle(fresh(c), True, True, False))
        notices = [s for s in self.sent if "still waiting on you" in s[0]]
        self.assertEqual(len(notices), 1, self.sent)
        self.assertEqual(notices[0][1], "brief")
        self.assertIn("<b>CycleCo</b> is still waiting on you: 1 question, 1 task waiting or blocked", notices[0][0])
        self.assertIn("The oldest has waited", notices[0][0])
        self.assertEqual(db.company_settings(fresh(c))["last_waiting_notice"], _dt.date.today().isoformat())
        self.assertTrue(db.one("SELECT id FROM events WHERE company_id=? AND message LIKE '%still waiting on you%'",
                               (c["id"],)))

    def test_no_waiting_notice_while_work_can_move(self):
        c = self.started()
        add_task(c, "Write the FAQ", "marketing", status="todo")
        add_task(c, "Deploy", "engineering", status="waiting")
        c = self.started()
        planning.should_cycle(fresh(c), True, False, False)
        self.assertEqual([s for s in self.sent if "still waiting" in s[0]], [])
        self.assertIsNone(planning.waiting_summary(fresh(c)))

    def test_cycle_header(self):
        c = make_company("HeaderCo", idea="Roofing leads for roofers in Phoenix, sold per lead")
        planning.cycle_started(c)
        planning._cycle_flags.pop(c["id"], None)   # as after a restart: the header compares the live stamp
        h = planning.cycle_header(fresh(c))
        self.assertTrue(h.startswith("Nothing has changed since your last planning cycle."), h)
        self.assertIn("If nothing changed and the open work is waiting on the owner, finish at once with status "
                      "'waiting' and a one-line note. Plan only work that does not need the answer.", h)
        self.assertIn("Prefer work that touches real customers (conversations, signups, replies) over internal "
                      "documents. Never create a task whose only output is a status file.", h)
        self.assertIn("## Next experiments (lead generation or marketplace playbook)", h)
        self.assertIn("Ask 10 providers what they pay per lead", h)
        bus.emit(c["id"], "decision", "Owner answered", None)
        self.assertNotIn("Nothing has changed", planning.cycle_header(fresh(c)))
        # right after cycle_started the header uses its answer (the stamp was just stored)
        planning.cycle_started(fresh(c))
        self.assertNotIn("Nothing has changed", planning.cycle_header(fresh(c)))
        planning.cycle_started(fresh(c))
        self.assertIn("Nothing has changed", planning.cycle_header(fresh(c)))
        prompt = agents.cycle_prompt(fresh(c))
        self.assertTrue(prompt.startswith("Run a planning cycle for HeaderCo.\n\nNothing has changed"), prompt[:200])
        self.assertIn("Waiting on the owner sections of your instructions", prompt)
        self.assertNotIn("—", prompt)

    def test_scheduler_plans_only_on_change(self):
        c = self.started()
        add_task(c, "Deploy", "engineering", status="waiting")
        c = self.started()
        db.update("companies", c["id"], last_cycle_at=ago(30))
        starts: list = []
        sched = scheduler.Scheduler()
        with mock.patch.object(agents, "start", lambda aid, kind, **kw: starts.append(kind) or True):
            for _ in range(5):
                sched._maybe_cycle(fresh(c))
            self.assertEqual(starts, [], "everything waits on the owner and nothing changed")
            db.update("companies", c["id"], cycle_requested=1, last_cycle_at=ago(5))
            sched._maybe_cycle(fresh(c))
            self.assertEqual(starts, ["cycle"])
            self.assertEqual(fresh(c)["cycle_requested"], 0)


# =============================================================== daily token budget

class TestBudget(unittest.TestCase):
    def setUp(self):
        self.sent: list = []
        self._p = mock.patch.object(notify, "send_later", lambda text, buttons=None, kind="info", **kw:
                                    self.sent.append((text, kind)))
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def add_run(self, c, tokens, kind="task", started=None):
        db.run("INSERT INTO runs(company_id, agent_id, kind, status, tokens, started_at) VALUES(?,?,?,?,?,?)",
               (c["id"], agent(c)["id"], kind, "done", tokens, started or db.now()))

    def test_over_budget_and_the_one_time_notice(self):
        c = make_company("BudgetCo", daily_token_budget=1000)
        self.assertFalse(planning.over_budget(c))
        self.add_run(c, 600)
        self.add_run(c, 5000, kind="chat")                  # owner chat never counts
        self.add_run(c, 9000, started=ago(60 * 48))         # yesterday's work does not count today
        self.assertFalse(planning.over_budget(fresh(c)))
        self.assertEqual(planning.tokens_today(c["id"]), 600)
        self.add_run(c, 500, kind="cycle")
        self.assertTrue(planning.over_budget(fresh(c)))
        self.assertTrue(planning.over_budget(fresh(c)))
        events = db.q("SELECT * FROM events WHERE company_id=? AND kind='budget'", (c["id"],))
        self.assertEqual(len(events), 1)
        self.assertIn("used its daily token budget (1,100 of 1,000 tokens)", events[0]["message"])
        self.assertEqual([k for _t, k in self.sent], ["important"])
        status = {r["company_id"]: r for r in planning.budget_status()}[c["id"]]
        self.assertEqual(status, {"company_id": c["id"], "name": "BudgetCo", "used": 1100, "budget": 1000,
                                  "over": True})

    def test_zero_turns_it_off(self):
        c = make_company("NoLimitCo", daily_token_budget=0)
        self.add_run(c, 10 ** 9)
        self.assertFalse(planning.over_budget(c))
        self.assertFalse({r["company_id"]: r for r in planning.budget_status()}[c["id"]]["over"])

    def test_scheduler_holds_background_tasks_but_runs_owner_tasks(self):
        c = make_company("HoldCo", daily_token_budget=10)
        self.add_run(c, 100)
        bg = add_task(c, "Background task", "engineering", created_by="Atlas")
        mine = add_task(c, "Owner task", "marketing", created_by="owner")
        db.update("companies", c["id"], last_cycle_at=db.now())
        starts: list = []
        others = db.q("SELECT id FROM companies WHERE id!=? AND status='active'", (c["id"],))
        db.run(f"UPDATE companies SET status='paused' WHERE id IN ({','.join(str(o['id']) for o in others) or '0'})")
        try:
            with mock.patch.object(agents, "start", lambda aid, kind, **kw: starts.append(kw.get("task_id")) or True), \
                    mock.patch.object(doctor, "refresh_if_stale", lambda *a, **kw: None), \
                    mock.patch.object(doctor, "row_status", lambda key: "ok"), \
                    mock.patch.object(watchdog, "tick", lambda: None), \
                    mock.patch.object(backup, "maybe_run", lambda: None), \
                    mock.patch.object(notify, "ensure_poller", lambda: None):
                db.set_settings({"kill_switch": False, "scheduler_enabled": True, "max_concurrent_runs": 8})
                scheduler.Scheduler().tick()
        finally:
            db.run(f"UPDATE companies SET status='active' WHERE id IN "
                   f"({','.join(str(o['id']) for o in others) or '0'})")
            db.set_settings({"max_concurrent_runs": 2})
        self.assertIn(mine["id"], starts)
        self.assertNotIn(bg["id"], starts)


# =============================================================== charter

class TestCharter(unittest.TestCase):
    def test_a_complete_charter_passes(self):
        self.assertEqual(planning.validate_charter(FAKE_CHARTER), [])
        self.assertEqual(planning.validate_charter(SYNONYM_CHARTER), [])

    def test_rooforyous_charter_fails(self):
        missing = planning.missing_names(planning.validate_charter(ROOFORYOU_CHARTER))
        for name in ("Offer and price", "Competitors", "Riskiest assumption", "How we reach customers",
                     "Business model", "30-day plan"):
            self.assertIn(name, missing)
        for name in ("One-liner", "Problem", "Customer", "North Star metric"):
            self.assertNotIn(name, missing)   # a bold label and "Target Market" count

    def test_empty_and_thin_sections(self):
        thin = FAKE_CHARTER.replace("Meal plans that paying owners use each week.", "TBD")
        self.assertEqual(planning.validate_charter(thin), ["North Star metric"])
        self.assertEqual(len(planning.validate_charter("")), 10)

    def test_content_rules(self):
        no_price = FAKE_CHARTER.replace("Price to test: $9 per month.", "A price will be decided later on.")
        self.assertEqual(planning.validate_charter(no_price),
                         ["Offer and price (needs a price, or a price range to test)"])
        range_to_test = FAKE_CHARTER.replace("Price to test: $9 per month.", "A price range to test with owners.")
        self.assertEqual(planning.validate_charter(range_to_test), [])
        two = SYNONYM_CHARTER.replace("- Google Local Services Ads: pay per lead, roofers compete on reviews\n", "")
        self.assertEqual(planning.missing_names(planning.validate_charter(two)), ["Competitors"])
        self.assertIn("one per bullet or table row", planning.validate_charter(two)[0])
        no_threshold = FAKE_CHARTER.replace("At least 5% of landing page visitors join the waitlist",
                                            "Plenty of landing page visitors join the waitlist")
        self.assertEqual(planning.validate_charter(no_threshold),
                         ["Riskiest assumption (needs a pass threshold: a number or percentage)"])

    def test_update_charter_refuses_an_incomplete_charter(self):
        c = make_company("CharterCo")
        ctx = ctx_for(c, "ceo", kind="cycle")
        out = tools.execute(ctx, "update_charter", {"charter": ROOFORYOU_CHARTER})
        self.assertTrue(out.startswith("ERROR: the charter is missing these sections"), out)
        self.assertEqual(fresh(c)["charter"], "")
        self.assertEqual(tools.execute(ctx, "update_charter", {"charter": FAKE_CHARTER}),
                         "Charter saved to docs/charter.md.")
        self.assertEqual(fresh(c)["charter"], FAKE_CHARTER)

    def test_write_file_charter_is_validated_and_reaches_the_record(self):
        c = make_company("FileCharterCo")
        db.update("companies", c["id"], stage="idea", mission="", charter="")
        ctx = ctx_for(c, "ceo", kind="cycle")
        out = tools.execute(ctx, "write_file", {"path": "docs/charter.md", "content": ROOFORYOU_CHARTER})
        self.assertTrue(out.startswith("ERROR: the charter is missing these sections, or they are empty: "), out)
        self.assertIn("Add them and write it again.", out)
        self.assertFalse(workspace.safe_path(c, "docs/charter.md").exists())
        out = tools.execute(ctx, "write_file", {"path": "./docs/charter.md", "content": FAKE_CHARTER})
        self.assertTrue(out.startswith("Saved docs/charter.md"), out)
        c2 = fresh(c)
        self.assertEqual((c2["charter"], c2["mission"], c2["stage"]),
                         (FAKE_CHARTER, "Weekly meal plans for dogs.", "building"))
        self.assertTrue(db.one("SELECT id FROM events WHERE company_id=? AND kind='charter' AND message=?",
                               (c["id"], "saved docs/charter.md as the company charter")))
        # an owner's one-liner stays, except while founding writes the charter
        db.update("companies", c["id"], mission="Owner's words")
        tools.execute(ctx_for(c, "ceo", kind="cycle"), "write_file",
                      {"path": "docs/charter.md", "content": FAKE_CHARTER + "\n"})
        self.assertEqual(fresh(c)["mission"], "Owner's words")
        planning.update_company_settings(c["id"], founding_stage="charter")
        tools.execute(ctx_for(c, "ceo", kind="cycle"), "write_file",
                      {"path": "docs/charter.md", "content": FAKE_CHARTER + "\n\n"})
        self.assertEqual(fresh(c)["mission"], "Weekly meal plans for dogs.")
        # other files are not touched by the hook
        self.assertTrue(tools.execute(ctx, "write_file", {"path": "docs/notes.md", "content": "notes"})
                        .startswith("Saved"))

    def test_the_critic_only_writes_the_critique(self):
        c = make_company("CriticCo")
        ctx = ctx_for(c, "ceo", kind="founding")
        ctx.founding_stage = "critic"
        out = tools.execute(ctx, "write_file", {"path": "docs/charter.md", "content": FAKE_CHARTER})
        self.assertEqual(out, "ERROR: in this review you can only write docs/critique.md. Put your critique there.")
        self.assertTrue(tools.execute(ctx, "write_file", {"path": "docs/critique.md", "content": "# Critique\n"})
                        .startswith("Saved"))


# =============================================================== staged founding

class FoundingBase(unittest.TestCase):
    def setUp(self):
        self.sent: list = []
        self._patches = [mock.patch.object(notify, "send", lambda text, buttons=None, kind="info", **kw:
                                           self.sent.append((text, kind)) or True),
                         mock.patch.object(notify, "configured", lambda *a, **kw: True)]
        for p in self._patches:
            p.start()
        self.saved_last = doctor.LAST
        doctor.LAST = []
        db.set_settings({"kill_switch": False, "review_model": ""})

    def tearDown(self):
        notify.flush()
        for p in reversed(self._patches):
            p.stop()
        doctor.LAST = self.saved_last
        db.set_settings({"kill_switch": False, "review_model": ""})

    def founding(self, stage, **extra) -> dict:
        c = make_company("StageCo", status="founding", idea="Roofing leads for roofers in Phoenix",
                         founding_stage=stage, **extra)
        db.update("companies", c["id"], stage="idea", charter="", mission="")
        return fresh(c)

    def run_stage(self, c, scripted: Scripted) -> dict:
        with mock.patch.object(agents.llm, "chat", scripted):
            agents.run(agent(c)["id"], "founding")
        notify.flush()
        return db.one("SELECT * FROM runs WHERE company_id=? ORDER BY id DESC", (c["id"],))

    def stage(self, c) -> str:
        return db.company_settings(fresh(c)).get("founding_stage")


class TestStagedFoundingScripted(FoundingBase):
    def test_stage_tools_steps_and_prompts(self):
        c = self.founding("research")
        expect = {"research": ({"web_search", "fetch_url", "write_file", "remember", "finish"},
                               {"create_task", "update_charter", "ask_owner"}),
                  "charter": ({"update_charter", "read_file", "finish"}, {"write_file", "create_task", "web_search"}),
                  "critic": ({"read_file", "web_search", "fetch_url", "write_file", "finish"},
                             {"create_task", "update_charter", "report_to_owner"}),
                  "plan": ({"create_task", "ask_owner", "report_to_owner", "read_file", "finish"},
                           {"write_file", "web_search", "update_charter"})}
        for name, (have, lack) in expect.items():
            planning.update_company_settings(c["id"], founding_stage=name)
            if name == "plan":
                db.update("companies", c["id"], status="founding")
            s = Scripted([("finish", {"summary": "ok", "status": "partial"})])
            with captured_starts():
                self.run_stage(fresh(c), s)
            tools_seen = set(s.calls[0]["tools"])
            self.assertTrue(have <= tools_seen, (name, tools_seen))
            self.assertFalse(lack & tools_seen, (name, tools_seen))
            self.assertTrue(s.calls[0]["user"].startswith(f"You are founding a brand-new company. Stage: {name}"))
            self.assertIn(f"you have {planning.STAGE_STEPS[name]} tool calls", s.calls[0]["system"].lower())
            self.assertNotIn("—", s.calls[0]["user"])
        self.assertEqual(s.calls[0]["think"], "high")

    def test_research_that_never_stops_ends_at_its_budget_and_founding_moves_on(self):
        c = self.founding("research")
        s = Scripted([lambda n: ("web_search", {"query": f"roofing leads price {n}"})] * 40)
        with captured_starts() as started, mock.patch.object(tools.web, "search",
                                                             lambda q, n, s: [{"title": "t", "url": "https://x.org",
                                                                               "snippet": "s"}]):
            run = self.run_stage(c, s)
        self.assertEqual(len(s.calls), planning.STAGE_STEPS["research"])
        self.assertEqual(run["status"], "out_of_steps")
        self.assertEqual(self.stage(c), "charter")
        self.assertEqual([k for _a, k, _kw in started], ["founding"])   # the charter stage was started
        self.assertEqual(s.calls[0]["think"], "medium")
        # the charter stage hears that research stopped early, so it notes the gaps
        prompt = agents.founding_prompt(fresh(c), "")
        self.assertTrue(prompt.startswith("You are founding a brand-new company. Stage: charter"))
        self.assertIn("Research ended at its step budget", prompt)
        self.assertIn("never invent numbers", prompt)

    def test_charter_stage_retries_once_then_asks_and_carries_on(self):
        c = self.founding("charter")
        with captured_starts() as started:
            first = Scripted([("update_charter", {"charter": ROOFORYOU_CHARTER}),
                              ("finish", {"summary": "Tried.", "status": "partial"})])
            self.run_stage(c, first)
            self.assertEqual(self.stage(c), "charter")
            self.assertEqual(db.company_settings(fresh(c))["founding_retry"], "charter")
            self.assertEqual(len(started), 1)
            self.assertTrue(first.calls[1]["tools"])   # the refusal came back as a tool result
            second = Scripted([("finish", {"summary": "Still thin.", "status": "partial"})])
            self.run_stage(c, second)
        self.assertIn("Your last attempt did not save a complete charter. Missing or empty: One-liner",
                      second.calls[0]["user"])
        self.assertEqual(self.stage(c), "critic")
        self.assertEqual(len(started), 2)
        qs = pending_questions(c)
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["p"]["pattern"], "founding_partial")
        self.assertIn("the charter still lacks One-liner", qs[0]["p"]["question"])
        self.assertIn("Founding continues with the critique", qs[0]["p"]["context"])
        self.assertEqual(fresh(c)["status"], "founding")

    def test_a_valid_charter_moves_on_and_names_the_company(self):
        c = self.founding("charter")
        with captured_starts() as started:
            s = Scripted([("update_charter", {"charter": SYNONYM_CHARTER, "company_name": "Roofy",
                                              "one_liner": "Exclusive roofing leads", "stage": "building"}),
                          ("finish", {"summary": "Charter saved."})])
            self.run_stage(c, s)
        self.assertIn("# Playbook: lead generation or marketplace", s.calls[0]["user"])
        self.assertIn("Give the company a short, memorable name", s.calls[0]["user"])
        c2 = fresh(c)
        self.assertEqual((c2["name"], c2["mission"], c2["stage"]), ("Roofy", "Exclusive roofing leads", "building"))
        self.assertEqual(self.stage(c), "critic")
        self.assertEqual(len(started), 1)
        self.assertEqual(pending_questions(c), [])

    def test_the_critic_uses_the_review_model(self):
        db.set_settings({"review_model": "judge:latest"})
        c = self.founding("critic")
        db.update("companies", c["id"], charter=SYNONYM_CHARTER)
        with captured_starts():
            s = Scripted([("write_file", {"path": "docs/critique.md", "content": "# Critique\n\n1. Demand.\n"}),
                          ("write_file", {"path": "docs/other.md", "content": "x"}),
                          ("finish", {"summary": "Critique written."})])
            self.run_stage(c, s)
        self.assertEqual(s.calls[0]["model"], "judge:latest")
        self.assertIn("skeptical investor", s.calls[0]["user"])
        self.assertTrue(workspace.safe_path(c, "docs/critique.md").is_file())
        self.assertFalse(workspace.safe_path(c, "docs/other.md").exists())
        self.assertEqual(self.stage(c), "plan")
        # without review_model the critic uses the CEO's model
        db.set_settings({"review_model": "", "ceo_model": "boss:latest"})
        try:
            c = self.founding("critic")
            with captured_starts():
                s = Scripted([("finish", {"summary": "done"})])
                self.run_stage(c, s)
            self.assertEqual(s.calls[0]["model"], "boss:latest")
        finally:
            db.set_settings({"ceo_model": ""})

    def test_qa_agents_use_the_review_model_unless_the_owner_chose_one(self):
        db.set_settings({"review_model": "judge:latest"})
        c = make_company("QaCo", members=("ceo", "qa"))
        qa = agent(c, "qa")
        for model_set, expected in (("", "judge:latest"), ("own:1b", "own:1b")):
            db.update("agents", qa["id"], model=model_set)
            t = add_task(c, f"Review the page {model_set}", "qa")
            s = Scripted([("finish", {"summary": "ok", "status": "partial"})])
            with mock.patch.object(agents.llm, "chat", s):
                agents.run(qa["id"], "task", task_id=t["id"])
            self.assertEqual(s.calls[0]["model"], expected)
        eng = make_company("EngCo", members=("ceo", "engineering"))
        t = add_task(eng, "Build it", "engineering")
        s = Scripted([("finish", {"summary": "ok", "status": "partial"})])
        with mock.patch.object(agents.llm, "chat", s):
            agents.run(agent(eng, "engineering")["id"], "task", task_id=t["id"])
        self.assertEqual(s.calls[0]["model"], db.get_settings()["default_model"])

    def test_plan_stage_ends_founding(self):
        c = self.founding("plan")
        db.update("companies", c["id"], charter=SYNONYM_CHARTER, mission="Exclusive roofing leads")
        with captured_starts() as started:
            s = Scripted([("create_task", {"title": "Call 10 roofers", "assign_to": "marketing",
                                           "deliverable": "marketing/roofer-calls.md", "acceptance": "- 10 notes"}),
                          ("ask_owner", {"question": "What should the company get first?",
                                         "options": "A mailbox\nA postal address\nA domain",
                                         "recommended": "A mailbox"}),
                          ("report_to_owner", {"message": "Welcome! Week one: talk to roofers."}),
                          ("finish", {"summary": "Planned the first week."})])
            run = self.run_stage(c, s)
        self.assertEqual(started, [])
        self.assertEqual(run["status"], "done")
        c2 = fresh(c)
        self.assertEqual(c2["status"], "active")
        self.assertEqual(self.stage(c), "done")
        self.assertEqual(pending_questions(c)[0]["p"]["recommended"], "A mailbox")
        self.assertIn(("<b>StageCo</b> is up and running: 1 task queued.", "important"), self.sent)
        events = [e["message"] for e in db.q("SELECT message FROM events WHERE company_id=? AND kind='founding'",
                                             (c["id"],))]
        self.assertIn("Founding: planning the first week", events)

    def test_kill_switch_keeps_the_stage_and_found_resumes_it(self):
        c = self.founding("critic")
        db.set_settings({"kill_switch": True})
        with captured_starts() as started:
            run = self.run_stage(c, Scripted([("finish", {"summary": "never"})]))
        self.assertEqual(run["status"], "stopped")
        self.assertEqual(self.stage(c), "critic")
        self.assertEqual(started, [])
        db.set_settings({"kill_switch": False})
        with captured_starts() as started:
            self.assertTrue(agents.found(c["id"]))
        self.assertEqual(self.stage(c), "critic")
        self.assertEqual(len(started), 1)

    def test_the_scheduler_starts_a_stage_whose_hand_over_was_missed(self):
        c = self.founding("plan")
        db.run("INSERT INTO runs(company_id, agent_id, kind, status, started_at, finished_at) VALUES(?,?,?,?,?,?)",
               (c["id"], agent(c)["id"], "founding", "partial", db.now(), db.now()))
        found: list = []
        with mock.patch.object(agents, "found", lambda cid: found.append(cid) or True), \
                mock.patch.object(agents, "is_running", lambda aid: False):
            scheduler.Scheduler()._resume_founding()
        self.assertIn(c["id"], found)
        planning.update_company_settings(c["id"], founding_stage="done")
        found.clear()
        with mock.patch.object(agents, "found", lambda cid: found.append(cid) or True), \
                mock.patch.object(agents, "is_running", lambda aid: False):
            scheduler.Scheduler()._resume_founding()
        self.assertNotIn(c["id"], found)

    def test_retry_after_founding_restarts_where_it_was_weakest(self):
        c = self.founding("done")
        db.update("companies", c["id"], status="active")
        self.assertEqual(planning.retry_stage(fresh(c)), "research")
        workspace.safe_path(c, "research/market.md").write_text("# Market\n", encoding="utf-8")
        self.assertEqual(planning.retry_stage(fresh(c)), "charter")
        db.update("companies", c["id"], charter=SYNONYM_CHARTER)
        self.assertEqual(planning.retry_stage(fresh(c)), "plan")
        with captured_starts() as started:
            self.assertTrue(agents.found(c["id"]))
        self.assertEqual((self.stage(c), fresh(c)["status"], len(started)), ("plan", "founding", 1))

    def test_research_prompt_when_search_is_down(self):
        c = self.founding("research")
        doctor.LAST[:] = [doctor._row("search", "Web search", "bad", "Every search provider failed")]
        p = agents.founding_prompt(c, "Owner has 5 hours a week")
        self.assertTrue(p.startswith("You are founding a brand-new company. Stage: research (1 of 4"))
        self.assertIn("Web search is down right now", p)
        self.assertIn("About the owner and their constraints:\nOwner has 5 hours a week", p)
        self.assertIn("Do not write the charter or create tasks in this stage.", p)


class TestStagedFoundingWithTheFakeModel(FoundingBase):
    """The scripted founding in tests/fake_ollama.py goes through every stage over HTTP and agents.run."""

    @classmethod
    def setUpClass(cls):
        cls.model = FakeModelServer(delay=0).start()
        cls.saved = {k: db.get_settings().get(k) for k in ("llm_backend", "llm_base_url", "default_model")}
        db.set_settings({"llm_backend": "ollama", "llm_base_url": cls.model.url, "default_model": "gpt-oss:20b"})

    @classmethod
    def tearDownClass(cls):
        db.set_settings(cls.saved)
        cls.model.stop()

    def wait_founded(self, c, timeout=40):
        ceo = agent(c)
        deadline = time.time() + timeout
        while time.time() < deadline and (fresh(c)["status"] == "founding" or agents.is_running(ceo["id"])):
            time.sleep(0.05)
        notify.flush()
        return fresh(c)

    def test_founding_goes_through_every_stage(self):
        with mock.patch.object(tools.web, "search", lambda q, n, s: []):   # no network in tests
            c = agents.create_company("Personalised weekly meal plans for dog owners", template="launch")
            c = self.wait_founded(c)
        self.assertEqual(c["status"], "active")
        self.assertEqual(c["name"], "PawPlan")
        self.assertEqual(planning.validate_charter(c["charter"]), [])
        self.assertIn("One-liner", c["charter"])
        self.assertEqual(db.company_settings(c)["founding_stage"], "done")
        runs = db.q("SELECT * FROM runs WHERE company_id=? AND kind='founding' ORDER BY id", (c["id"],))
        stages = [json.loads(r["transcript"])[0]["content"].split("Stage: ")[1].split(" ")[0] for r in runs]
        self.assertEqual(stages, ["research", "charter", "critic", "plan"])
        self.assertTrue(workspace.safe_path(c, "research/market.md").is_file())
        self.assertIn("Three strongest reasons", workspace.safe_path(c, "docs/critique.md").read_text("utf-8"))
        titles = {t["title"] for t in db.q("SELECT title FROM tasks WHERE company_id=?", (c["id"],))}
        self.assertGreaterEqual(len(titles), 5)
        events = [e["message"] for e in db.q("SELECT message FROM events WHERE company_id=? AND kind='founding' "
                                             "ORDER BY id", (c["id"],))]
        for text in ("Founding: researching the market", "Founding: writing the charter",
                     "Founding: a skeptical investor is reviewing the charter", "Founding: planning the first week"):
            self.assertIn(text, events)
        self.assertIn(("<b>PawPlan</b> is up and running: 5 tasks queued.", "important"), self.sent)
        self.assertEqual(pending_questions(c), [])

    def test_an_interrupted_stage_resumes_where_it_stopped(self):
        c = make_company("ResumeCo", status="founding", idea="Weekly meal plans for dogs", founding_stage="critic",
                         name_locked=True)
        db.update("companies", c["id"], charter=FAKE_CHARTER, mission="Weekly meal plans for dogs.")
        db.run("INSERT INTO runs(company_id, agent_id, kind, status, started_at, finished_at) VALUES(?,?,?,?,?,?)",
               (c["id"], agent(c)["id"], "founding", "interrupted", db.now(), db.now()))
        self.assertTrue(agents.found(c["id"]))   # what recover() and the scheduler do for a stopped founding
        c = self.wait_founded(c)
        self.assertEqual(c["status"], "active")
        runs = db.q("SELECT transcript FROM runs WHERE company_id=? AND kind='founding' AND status!='interrupted' "
                    "ORDER BY id", (c["id"],))
        self.assertTrue(json.loads(runs[0]["transcript"])[0]["content"].startswith(
            "You are founding a brand-new company. Stage: critic"))
        self.assertEqual(len(runs), 2)   # critic, then plan; research and charter are not repeated
        self.assertTrue(workspace.safe_path(c, "docs/critique.md").is_file())
        self.assertEqual(c["name"], "ResumeCo")


# =============================================================== playbooks

class TestPlaybooks(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(playbooks.classify("Roofing leads subscription for roofers in Phoenix"), "lead_generation")
        self.assertEqual(playbooks.classify(ROOFORYOU_CHARTER), "lead_generation")
        self.assertEqual(playbooks.classify("A weekly newsletter for home bakers"), "newsletter")
        self.assertEqual(playbooks.classify("Notion templates for freelancers"), "digital_products")
        self.assertEqual(playbooks.classify("A Shopify store for handmade candles"), "ecommerce")
        self.assertEqual(playbooks.classify("Mobile dog grooming in my town"), "local_services")
        self.assertEqual(playbooks.classify("A Chrome extension that summarises PDFs"), "saas")
        self.assertEqual(playbooks.classify("Personalised weekly meal plans for dog owners"), "")

    def test_every_playbook_is_complete_and_short(self):
        self.assertEqual(set(playbooks.PLAYBOOKS), {"lead_generation", "newsletter", "saas", "services",
                                                    "digital_products", "ecommerce", "local_services"})
        for key, p in playbooks.PLAYBOOKS.items():
            for field in ("label", "money_flow", "riskiest", "experiments", "metrics", "traps", "compliance"):
                self.assertTrue(p[field], f"{key} lacks {field}")
            text = playbooks.render(key)
            self.assertLessEqual(len(text), playbooks.SECTION_CAP)
            self.assertIn("Compliance:", text)
            self.assertNotIn("—", text)

    def test_section_uses_the_owner_override(self):
        c = make_company("PlayCo", idea="Roofing leads for Phoenix roofers")
        self.assertIn("Two sides.", playbooks.section(c))
        planning.update_company_settings(c["id"], archetype="newsletter")
        self.assertTrue(playbooks.section(fresh(c)).startswith("# Playbook: newsletter or content"))
        d = make_company("PlainCo", idea="Personalised weekly meal plans for dog owners")
        self.assertEqual(playbooks.section(d), "")
        self.assertEqual(playbooks.experiments(d), [])


# =============================================================== dashboard routes

class TestRoutes(unittest.TestCase):
    def test_planning_and_budget_routes(self):
        from holdco.routing import ROUTES
        c = make_company("RouteCo", daily_token_budget=500)

        def call(method, path):
            for m, rx, fn in ROUTES:
                match = rx.match(path)
                if m == method and match:
                    return fn(None, match, None)
            raise AssertionError(f"no route {method} {path}")

        p = call("GET", f"/api/companies/{c['id']}/planning")["planning"]
        for key in ("changed", "cycle_backoff", "next_due_minutes", "waiting", "founding_stage", "archetype",
                    "budget"):
            self.assertIn(key, p)
        self.assertEqual(p["budget"], {"used": 0, "budget": 500, "over": False})
        rows = call("GET", "/api/budget")["budget"]
        self.assertTrue(any(r["company_id"] == c["id"] for r in rows))



class TestQuestionQuality(unittest.TestCase):
    """From the real gpt-oss run: 'Could you confirm which option we should choose?' reached the owner, and so did a
    request for the owner to provide a contact list and interview notes the team should gather itself."""

    def test_vague_questions_are_refused(self):
        for q in ("Could you confirm which option we should choose?", "Proceed?", "Is this ok?"):
            self.assertIn("Say the decision in the question itself", planning.question_problem(q, ["A", "B"]) or "", q)

    def test_team_work_is_refused(self):
        q = ("Could you please provide the roofer contact list file 'docs/roofers_contact_list.md' and interview notes "
             "on provider lead pricing, so we can calculate the cost per inquiry?")
        self.assertIn("your team can gather this", planning.question_problem(q, []) or "")

    def test_real_owner_questions_pass(self):
        for q in ("Should we price the starter plan at $29 or $49 a month?",
                  "Can you add a company mailbox (SMTP) so we can email the 10 roofers on our list?",
                  "Please provide the postal address we should put in the footer of outreach emails.",
                  "Do you approve deploying the landing page to Netlify so roofers can sign up?"):
            self.assertIsNone(planning.question_problem(q, []), q)


if __name__ == "__main__":
    unittest.main()
