"""Phase 0.5 gate: Rooforyou's failure patterns replayed end to end (design section 9).

Every test drives the real agent loop (agents.run) with a scripted model standing in for llm.chat, against the
real truth, context and planning modules: none of them is patched. Only the outside world is faked: the model,
web pages and search results, the phone, and the scheduler's thread start (so a test can watch which runs it
would start instead of running them).

The gate this file proves: no fabricated record is accepted, there is at most one pending question per topic,
and no planning cycle starts while nothing changes.

Run with:  python -m unittest tests.test_phase05_replay -v
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-replay-"))

from holdco import (agents, context, db, doctor, llm, notify, planning, roles, routing, scheduler,  # noqa: E402
                    server, tools, truth, web, workspace)  # noqa: F401  (server registers the dashboard routes)
from tests.fake_ollama import CHARTER, CRITIQUE  # noqa: E402

db.init()

ADDRESS = "1 Main Street, Springfield, IL 62701"
SITE_URL = "https://rooforyou.netlify.app"

# Rooforyou's real charter from the owner's first run: no price, no competitors, no channels, no business model.
THIN_CHARTER = """# Rooforyou Charter

## Company Overview
- **One-liner**: A subscription service that delivers verified roofing leads directly to Phoenix roofers.

## Problem
Independent roofers in Phoenix struggle to find reliable, high-quality leads.

## North Star Metric
Number of qualified roofing leads delivered to roofers per week.
"""


# ---------------------------------------------------------------- helpers (copied locally, like the other modules)

def ts(minutes_ago: float = 0) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_company(name="Rooforyou", members=("ceo", "engineering", "marketing", "sales", "research"),
                 status="active", idea="Roofing leads for roofers in Phoenix", **settings) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    s = {"truth_scan_done": True, **settings}   # the one-time truth check has its own tests
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, idea, status, "building", json.dumps(s), db.now()))
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


def make_task(company, role, title, kind="", deliverable="", status="todo") -> dict:
    a = agent_of(company, role)
    tid = db.run("INSERT INTO tasks(company_id, title, role, agent_id, status, priority, deliverable, kind, created_by, "
                 "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (company["id"], title, role, a["id"], status, 2, deliverable, kind, "test", db.now(), db.now()))
    return db.get_task(tid)


def events(company, kind) -> list[dict]:
    rows = db.q("SELECT * FROM events WHERE company_id=? AND kind=? ORDER BY id", (company["id"], kind))
    for r in rows:
        r["data"] = db.loads(r["data"], {}) or {}
    return rows


def pending(company, action=None) -> list[dict]:
    rows = db.q("SELECT * FROM approvals WHERE company_id=? AND status='pending' ORDER BY id", (company["id"],))
    return [r for r in rows if action is None or r["action"] == action]


def call(method: str, path: str, body=None, query: dict | None = None):
    """Dispatch to a dashboard route the way server.Handler does, so the route patterns are exercised too."""
    class Handler:
        def __init__(self):
            self.query = {k: [v] for k, v in (query or {}).items()}

    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(path)):
            return fn(Handler(), m, body)
    raise AssertionError(f"no route for {method} {path}")


class Scripted:
    """Stands in for llm.chat: answers each call with the next (tool, args) and records what each call was given.
    A step may be a function of the call number, for a model that never stops."""

    def __init__(self, steps=()):
        self.steps = list(steps)
        self.calls: list[dict] = []

    def __call__(self, messages, tools_spec=None, model=None, settings=None, **kw):
        self.calls.append({"tools": [t["name"] for t in tools_spec or []], "system": messages[0]["content"],
                           "user": messages[1]["content"]})
        if not self.steps:
            return llm.Reply(content="")
        step = self.steps.pop(0)
        if callable(step):
            step = step(len(self.calls))
        name, args = step
        return llm.Reply(tool_calls=[llm.ToolCall(name=name, arguments=args)], tokens=100)


@contextlib.contextmanager
def captured_starts():
    """agents.start records instead of starting a thread: the test drives each run itself."""
    started: list = []
    with mock.patch.object(agents, "start", lambda aid, kind, **kw: started.append((aid, kind, kw)) or True):
        yield started


def quiet():
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
    return stack


class Replay(unittest.TestCase):
    """Process-wide state other modules may have left behind: Doctor rows, the kill switch, phone sends."""

    def setUp(self):
        self._saved = {k: db.get_settings().get(k) for k in ("kill_switch", "scheduler_enabled", "review_model",
                                                             "max_steps_per_run")}
        db.set_settings({"kill_switch": False, "scheduler_enabled": True, "review_model": "", "max_steps_per_run": 14})
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

    def run_agent(self, company, role, kind, script, task=None) -> tuple[list[tuple[str, str]], dict, Scripted]:
        """One session of `role` replaying `script`. Returns ([(tool, result)], the run row, the scripted model)."""
        s = script if isinstance(script, Scripted) else Scripted(script)
        a = agent_of(company, role)
        with mock.patch.object(agents.llm, "chat", s):
            agents.run(a["id"], kind, task_id=task["id"] if task else None)
        row = db.one("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC", (a["id"],))
        results = [(m["name"], m["content"]) for m in json.loads(row["transcript"] or "[]") if m["role"] == "tool"]
        return results, row, s

    def exists(self, company, path) -> bool:
        return workspace.safe_path(company, path).exists()


# =============================================================== the section 9 table, one test per row

class TestFabricatedRecords(Replay):
    def setUp(self):
        super().setUp()
        self.c = make_company("Rooforyou", postal_address=ADDRESS)
        self.deploy = db.run("INSERT INTO approvals(company_id, agent_id, action, summary, payload, status, created_at) "
                             "VALUES(?,?,?,?,?,?,?)", (self.c["id"], agent_of(self.c, "engineering")["id"], "deploy_site",
                                                       "Deploy the website from site/", json.dumps({"folder": "site"}),
                                                       "pending", ts(180)))

    def test_deployment_confirmation_is_refused_and_the_facts_say_not_deployed(self):
        task = make_task(self.c, "ceo", "Update the launch notes", kind="write", deliverable="docs/launch-notes.md")
        results, _run, s = self.run_agent(self.c, "ceo", "task", [
            ("write_file", {"path": "docs/deployment_confirmation.md",
                            "content": f"# Deployment confirmed\n\nThe site is live at {SITE_URL}\n"}),
            ("write_file", {"path": "docs/launch-notes.md",
                            "content": f"# Launch notes\n\nWe deployed the site. It is live at {SITE_URL}.\n"}),
            ("write_file", {"path": "docs/launch-notes.md",
                            "content": f"# Launch notes\n\nThe site is ready to deploy; request #{self.deploy} is "
                                       "waiting for the owner.\n"}),
            ("finish", {"summary": "Launch notes updated.", "deliverable": "docs/launch-notes.md"})], task)
        confirmation, claim, honest = results[0][1], results[1][1], results[2][1]
        self.assertTrue(confirmation.startswith(truth.LOG_REFUSAL), confirmation)
        self.assertIn("no deploy has run", confirmation)
        self.assertTrue(claim.startswith("ERROR: not saved: docs/launch-notes.md"), claim)   # the claims gate
        self.assertIn(f"request #{self.deploy}", claim)
        self.assertTrue(honest.startswith("Saved docs/launch-notes.md"), honest)
        self.assertFalse(self.exists(self.c, "docs/deployment_confirmation.md"))
        self.assertNotIn("live at", workspace.safe_path(self.c, "docs/launch-notes.md").read_text(encoding="utf-8"))
        # the facts say what really happened, in the prompt the agent got and on the owner's Overview card
        self.assertIn("Website: local preview only; not deployed.", s.calls[0]["system"])
        facts = call("GET", f"/api/companies/{self.c['id']}/facts")
        self.assertFalse(facts["website"]["deployed"])
        self.assertTrue(facts["website"]["line"].startswith("Website: local preview only; not deployed."))
        self.assertEqual([p["id"] for p in facts["website"]["pending"]], [self.deploy])
        rules = [e["data"]["rule"] for e in events(self.c, "claims")]
        self.assertEqual(rules[:2], ["log_name", "claims"])
        self.assertEqual(db.get_task(task["id"])["status"], "done")

    def test_email_log_is_refused_by_the_name_rule_and_the_claims_gate(self):
        task = make_task(self.c, "sales", "Prepare roofer outreach notes", kind="write", deliverable="sales/notes.md")
        results, _run, s = self.run_agent(self.c, "sales", "task", [
            ("write_file", {"path": "docs/email_log.txt",
                            "content": "Sent email to bob@bobsroofing.co\nSent email to ann@annroofs.co\n"}),
            ("write_file", {"path": "sales/notes.md",
                            "content": "# Outreach\n\nSent email to bob@bobsroofing.co about the pilot.\n"}),
            ("write_file", {"path": "sales/notes.md",
                            "content": "# Outreach plan\n\nEmail Bob at Bob's Roofing about the pilot once a mailbox "
                                       "is connected.\n"}),
            ("finish", {"summary": "Outreach notes saved.", "deliverable": "sales/notes.md"})], task)
        log, claim, plan = results[0][1], results[1][1], results[2][1]
        self.assertTrue(log.startswith(truth.LOG_REFUSAL), log)                        # the name rule ...
        self.assertIn("It also states things Holdco's records do not show", log)       # ... and the claims gate
        self.assertIn("no email has been sent", log)
        self.assertTrue(claim.startswith("ERROR: not saved: sales/notes.md"), claim)   # the claims gate alone
        self.assertIn("no email has been sent", claim)
        self.assertTrue(plan.startswith("Saved sales/notes.md"), plan)
        self.assertFalse(self.exists(self.c, "docs/email_log.txt"))
        refused = events(self.c, "claims")
        self.assertEqual([(e["data"]["rule"], e["data"]["path"]) for e in refused[:2]],
                         [("log_name", "docs/email_log.txt"), ("claims", "sales/notes.md")])
        self.assertIn("Email: not connected", s.calls[0]["system"])
        self.assertEqual(call("GET", f"/api/companies/{self.c['id']}/facts")["email"]["sent_total"], 0)


class TestOutreachTask(Replay):
    TITLE = "Send outreach emails to Phoenix roofers"

    def setUp(self):
        super().setUp()
        # A mailbox is configured (so the CEO may create the task) on a port nobody listens on: nothing is sent.
        self.c = make_company("RoofReach", smtp_host="127.0.0.1", smtp_port=9, smtp_user="hi@roofreach.co",
                              smtp_password="x", smtp_from="hi@roofreach.co", policies={"send_email": "approve"})
        self.addCleanup(lambda: db.update("companies", self.c["id"], status="archived"))   # keep the Doctor quiet

    def plan_step(self, n=1):
        return ("write_file", {"path": "sales/outreach-plan.md",
                               "content": f"# Outreach plan (version {n})\n\nEmail Phoenix roofers about the lead "
                                          "pilot, one short message each, starting with Bob at Bob's Roofing.\n"})

    def test_a_file_never_finishes_it_and_a_queued_email_ends_it_waiting(self):
        results, _run, _s = self.run_agent(self.c, "ceo", "cycle", [
            ("create_task", {"title": self.TITLE, "assign_to": "sales", "deliverable": "sales/outreach-plan.md"}),
            ("finish", {"summary": "Queued the outreach task."})])
        self.assertIn("Created task", results[0][1])
        task = db.one("SELECT * FROM tasks WHERE company_id=? AND title=?", (self.c["id"], self.TITLE))
        self.assertEqual(task["kind"], "outreach")

        # 1) A plan file is not outreach. No postal address yet, and a placeholder recipient: nothing is queued.
        results, run, _s = self.run_agent(self.c, "sales", "task", [
            self.plan_step(1),
            ("finish", {"summary": "Outreach done.", "deliverable": "sales/outreach-plan.md"}),
            ("send_email", {"to": "owner@example.com", "subject": "Leads for your roofing business",
                            "body": "Hi, we send homeowners to local roofers. Worth a look?"}),
            ("send_email", {"to": "bob@bobsroofing.co", "subject": "Leads for Bob's Roofing",
                            "body": "Hi Bob, we send homeowners to local roofers. Worth a look?"}),
            ("finish", {"summary": "Outreach done, see the plan.", "deliverable": "sales/outreach-plan.md"})], task)
        self.assertTrue(results[1][1].startswith("Not finished yet"), results[1][1])
        self.assertIn("A file such as sales/outreach-plan.md does not count", results[1][1])
        self.assertTrue(results[2][1].startswith("ERROR: email not queued:"), results[2][1])
        self.assertIn("example", results[2][1])
        self.assertTrue(results[3][1].startswith("ERROR: email not queued:"), results[3][1])
        self.assertIn("postal address", results[3][1])
        self.assertEqual(run["status"], "partial")
        self.assertNotEqual(db.get_task(task["id"])["status"], "done")
        self.assertEqual(pending(self.c, "send_email"), [])

        # 2) The owner adds the postal address; a real recipient is queued, so the task ends waiting, not done.
        set_settings(self.c, postal_address=ADDRESS)
        results, run, _s = self.run_agent(self.c, "sales", "task", [
            ("send_email", {"to": "bob@bobsroofing.co", "subject": "Leads for Bob's Roofing",
                            "body": "Hi Bob, we send homeowners to local roofers. Worth a look?"}),
            self.plan_step(2),
            ("finish", {"summary": "First email queued.", "deliverable": "sales/outreach-plan.md"}),
            ("finish", {"summary": "First email queued; plan saved.", "deliverable": "sales/outreach-plan.md"})],
            db.get_task(task["id"]))
        self.assertIn("Queued for the owner's approval", results[0][1])
        self.assertIn("is still waiting for the owner. Call finish again with status 'waiting'", results[2][1])
        self.assertEqual(run["status"], "waiting")
        self.assertEqual(db.get_task(task["id"])["status"], "waiting")
        email = pending(self.c, "send_email")
        self.assertEqual([(a["task_id"], json.loads(a["payload"])["to"]) for a in email], [(task["id"], "bob@bobsroofing.co")])
        self.assertIn(ADDRESS, json.loads(email[0]["payload"])["body"])   # the CAN-SPAM footer was added

        # 3) Only a real send makes it done (the owner's mailbox sending is simulated: an outgoing row exists).
        db.update("approvals", email[0]["id"], status="done", result="Email sent to bob@bobsroofing.co.", decided_at=ts())
        db.run("INSERT INTO emails(company_id, direction, from_addr, to_addr, subject, body, status, created_at) "
               "VALUES(?,?,?,?,?,?,?,?)", (self.c["id"], "out", "hi@roofreach.co", "bob@bobsroofing.co",
                                           "Leads for Bob's Roofing", "Hi Bob", "sent", db.now()))
        db.update("tasks", task["id"], status="todo")
        _results, run, _s = self.run_agent(self.c, "sales", "task", [
            ("finish", {"summary": "Sent the first email to bob@bobsroofing.co.", "deliverable": "email sent"})],
            db.get_task(task["id"]))
        self.assertEqual(run["status"], "done")
        self.assertEqual(db.get_task(task["id"])["status"], "done")


class TestOneQuestionPerTopic(Replay):
    QUESTION = "Can you deploy the site to Netlify and approve the roofer outreach emails?"

    def test_the_same_deploy_question_in_three_cycles_stays_one(self):
        c = make_company("AskCo", members=("ceo", "engineering"))
        outs = []
        for n in range(3):
            results, _run, _s = self.run_agent(c, "ceo", "cycle", [
                ("ask_owner", {"question": self.QUESTION if n != 1 else "Could you please deploy the website and "
                                                                       "approve the outreach emails to roofers?",
                               "options": "Deploy now\nWait", "recommended": "Deploy now"}),
                ("finish", {"summary": f"Planning cycle {n + 1}: asked the owner.", "status": "waiting"})])
            outs.append(results[0][1])
        questions = pending(c, "question")
        self.assertEqual(len(questions), 1)
        self.assertIn(f"#{questions[0]['id']}", outs[0])
        for out in outs[1:]:
            self.assertTrue(out.startswith(f"Already waiting on the owner as #{questions[0]['id']}"), out)
            self.assertIn("Do not ask again", out)
        deduped = events(c, "question_deduped")
        self.assertEqual(len(deduped), 2)
        self.assertTrue(all(e["data"]["approval_id"] == questions[0]["id"] for e in deduped))
        self.assertEqual(call("GET", f"/api/companies/{c['id']}/facts")["waiting"][0]["id"], questions[0]["id"])


class TestNoPlanningWhileNothingChanges(Replay):
    def setUp(self):
        super().setUp()
        self.c = make_company("WaitCo", members=("ceo", "engineering"))
        self.ceo = agent_of(self.c)
        make_task(self.c, "engineering", "Deploy the landing page", kind="deploy", status="waiting")
        db.run("INSERT INTO approvals(company_id, agent_id, action, summary, payload, status, created_at) "
               "VALUES(?,?,?,?,?,?,?)", (self.c["id"], agent_of(self.c, "engineering")["id"], "deploy_site",
                                         "Deploy the website from site/", json.dumps({"folder": "site"}), "pending",
                                         ts(30)))
        self.starts: list = []
        sched = scheduler.Scheduler()
        for name in ("_housekeeping", "_resume_founding", "_routines", "_check_inbox"):
            setattr(sched, name, lambda *a, **kw: None)
        sched._model_server_down = lambda: False
        self.sched = sched
        self._p = [mock.patch.object(agents, "start", lambda aid, kind, **kw: self.starts.append((aid, kind)) or True),
                   mock.patch.object(agents, "running_count", lambda *a, **kw: 0)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in reversed(self._p):
            p.stop()
        db.update("companies", self.c["id"], status="archived")
        super().tearDown()

    def cycles(self) -> int:
        return sum(1 for aid, kind in self.starts if aid == self.ceo["id"] and kind == "cycle")

    def tick(self, last_cycle_minutes_ago: float | None = None) -> None:
        if last_cycle_minutes_ago is not None:
            db.update("companies", self.c["id"], last_cycle_at=ts(last_cycle_minutes_ago))
        self.sched.tick()

    def test_no_cycles_until_something_changes_and_the_backoff_grows(self):
        self.tick()   # never planned: the first look plans once and records the baseline
        self.assertEqual(self.cycles(), 1)
        self.assertEqual(fresh(self.c)["cycle_backoff"], 0)
        for _ in range(3):
            self.tick()   # just planned, nothing changed
        self.assertEqual(self.cycles(), 1)
        self.tick(61)   # the due interval passed, but nothing changed and everything waits on the owner
        self.assertEqual((self.cycles(), fresh(self.c)["cycle_backoff"]), (1, 1))
        self.tick(61)
        self.tick(90)
        self.assertEqual((self.cycles(), fresh(self.c)["cycle_backoff"]), (1, 1), "the next look waits 120 minutes")
        self.tick(121)
        self.assertEqual((self.cycles(), fresh(self.c)["cycle_backoff"]), (1, 2))
        self.tick(241)
        self.assertEqual((self.cycles(), fresh(self.c)["cycle_backoff"]), (1, 3))
        status = call("GET", f"/api/companies/{self.c['id']}/planning")["planning"]
        self.assertFalse(status["changed"])
        self.assertEqual(status["cycle_backoff"], 3)
        self.assertEqual(status["waiting"]["requests"], 1)
        # one "still waiting on you" line for the day instead of a planning cycle every few minutes
        notices = [e for e in events(self.c, "info") if e["data"].get("waiting_notice")]
        self.assertEqual(len(notices), 1)
        self.assertEqual(sum(1 for text, _k in self.sent if "WaitCo" in text and "still waiting on you" in text), 1)

        # The owner writes in the chat (through the dashboard route): the next tick plans.
        call("POST", f"/api/companies/{self.c['id']}/messages", {"content": "I approved the deploy, carry on."})
        self.assertIn((self.ceo["id"], "chat"), self.starts)
        self.tick(241)
        self.assertEqual(self.cycles(), 2)
        self.assertEqual(fresh(self.c)["cycle_backoff"], 0)
        self.tick()
        self.tick()
        self.assertEqual(self.cycles(), 2)


class TestFoundingThatNeverStopsResearching(Replay):
    def test_research_ends_at_its_budget_and_the_charter_and_plan_still_run(self):
        c = make_company("StageCo", status="founding", founding_stage="research")
        db.update("companies", c["id"], stage="idea", charter="", mission="")
        ceo = agent_of(c)
        search = lambda q, n, s: [{"title": "Roofing leads", "url": "https://roofers.org/leads", "snippet": "leads"}]  # noqa: E731
        with captured_starts() as started, mock.patch.object(tools.web, "search", search):
            # research: the model searches forever
            _r, run, s = self.run_agent(c, "ceo", "founding",
                                        [lambda n: ("web_search", {"query": f"roofing leads price {n}"})] * 40)
            self.assertEqual(len(s.calls), planning.STAGE_STEPS["research"])
            self.assertEqual(run["status"], "out_of_steps")
            self.assertEqual(db.company_settings(fresh(c))["founding_stage"], "charter")
            # charter: validated and saved to the record
            _r, run, s = self.run_agent(c, "ceo", "founding", [
                ("update_charter", {"charter": CHARTER, "company_name": "StageCo",
                                    "one_liner": "Weekly meal plans for dog owners"}),
                ("finish", {"summary": "Charter saved."})])
            self.assertIn("Research ended at its step budget", s.calls[0]["user"])
            self.assertEqual(fresh(c)["charter"], CHARTER)
            self.assertEqual(db.company_settings(fresh(c))["founding_stage"], "critic")
            # critic: the skeptical investor writes the critique
            self.run_agent(c, "ceo", "founding", [
                ("write_file", {"path": "docs/critique.md", "content": CRITIQUE}),
                ("finish", {"summary": "Critique saved.", "deliverable": "docs/critique.md"})])
            self.assertTrue(self.exists(c, "docs/critique.md"))
            self.assertEqual(db.company_settings(fresh(c))["founding_stage"], "plan")
            # plan: the first tasks and the welcome brief; founding ends
            _r, run, s = self.run_agent(c, "ceo", "founding", [
                ("create_task", {"title": "Call 10 dog owners", "assign_to": "marketing",
                                 "deliverable": "marketing/owner-calls.md", "acceptance": "- 10 call notes"}),
                ("create_task", {"title": "Build the waitlist page", "assign_to": "engineering",
                                 "deliverable": "site/index.html", "acceptance": "- labelled form"}),
                ("report_to_owner", {"message": "Welcome! Week one: talk to dog owners and build the waitlist page."}),
                ("finish", {"summary": "Planned the first week."})])
            self.assertIn("docs/critique.md", s.calls[0]["user"])
        self.assertEqual(run["status"], "done")
        self.assertEqual(fresh(c)["status"], "active")
        self.assertEqual(db.company_settings(fresh(c))["founding_stage"], "done")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=?", (c["id"],)), 2)
        self.assertEqual([k for a, k, _kw in started if a == ceo["id"]], ["founding"] * 3)   # each stage started the next
        stages = [e["data"].get("stage") for e in events(c, "founding") if e["data"].get("stage")]
        self.assertEqual(stages, ["research", "charter", "critic", "plan"])
        self.assertEqual(call("GET", f"/api/companies/{c['id']}/planning")["planning"]["founding_stage"], "done")


class TestCharterThroughWriteFile(Replay):
    def test_a_charter_written_with_write_file_is_validated_and_saved_to_the_record(self):
        c = make_company("FileCharterCo", members=("ceo", "engineering"))
        db.update("companies", c["id"], stage="idea", mission="", charter="")
        results, _run, _s = self.run_agent(c, "ceo", "cycle", [
            ("write_file", {"path": "docs/charter.md", "content": THIN_CHARTER}),
            ("write_file", {"path": "docs/charter.md", "content": CHARTER}),
            ("finish", {"summary": "Charter written to docs/charter.md."})])
        thin, full = results[0][1], results[1][1]
        self.assertTrue(thin.startswith("ERROR: the charter is missing these sections"), thin)
        for missing in ("Offer and price", "Competitors", "Riskiest assumption", "30-day plan"):
            self.assertIn(missing, thin)
        self.assertTrue(full.startswith("Saved docs/charter.md"), full)
        c2 = fresh(c)
        self.assertEqual((c2["charter"], c2["mission"], c2["stage"]), (CHARTER, "Weekly meal plans for dogs.", "building"))
        self.assertTrue(events(c, "charter"))
        self.assertEqual(workspace.safe_path(c, "docs/charter.md").read_text(encoding="utf-8"), CHARTER)


class TestVerifiedFacts(Replay):
    PAGE = ("Roofr pricing. Roofr Pro costs $99 per month and includes unlimited roof measurement reports for "
            "contractors.")
    URL = "https://roofr.com/pricing"

    def test_record_fact_with_a_quote_not_on_the_fetched_page_is_refused(self):
        c = make_company("FactCo", members=("ceo", "research"))
        task = make_task(c, "research", "Analyse competitor pricing", kind="research",
                         deliverable="research/pricing.md")
        with mock.patch.object(web, "fetch_page", lambda url, include_links=False: self.PAGE):
            results, _run, _s = self.run_agent(c, "research", "task", [
                ("record_fact", {"claim": "Roofr costs $99 per month", "url": self.URL,
                                 "quote": "Roofr Pro costs $99 per month"}),
                ("fetch_url", {"url": self.URL}),
                ("record_fact", {"claim": "Roofr costs $49 per month", "url": self.URL,
                                 "quote": "Roofr Pro costs $49 per month for every contractor"}),
                ("record_fact", {"claim": "Roofr Pro costs $99 per month", "url": self.URL,
                                 "quote": "Roofr Pro costs $99 per month and includes unlimited roof measurement"}),
                ("finish", {"summary": "Recorded one verified fact.", "status": "partial"})], task)
        unfetched, wrong, right = results[0][1], results[2][1], results[3][1]
        self.assertTrue(unfetched.startswith("ERROR: fetch the page first"), unfetched)
        self.assertTrue(wrong.startswith("ERROR: that quote is not on the page you fetched"), wrong)
        self.assertTrue(right.startswith("Saved fact #"), right)
        facts = db.q("SELECT * FROM facts WHERE company_id=?", (c["id"],))
        self.assertEqual([f["claim"] for f in facts], ["Roofr Pro costs $99 per month"])


class TestSearchFindsOwnerContext(Replay):
    def setUp(self):
        super().setUp()
        self.c = make_company("SearchCo", members=("ceo", "engineering"))
        self.addCleanup(self._clear_global)

    @staticmethod
    def _clear_global():
        call("DELETE", "/api/context/file", {"path": "about-quokkaberry.md"})

    def test_search_finds_a_phrase_in_global_and_company_context_with_path_and_line(self):
        # The owner adds the files from the dashboard (the same routes the Context pages use).
        call("PUT", "/api/context/file", {"path": "about-quokkaberry.md", "content":
                                          "# About me\n\nI am a roofer.\nMy budget is $500 a month for quokkaberry ads.\n"})
        call("PUT", f"/api/companies/{self.c['id']}/context/file",
             {"path": "calls/interviews.md", "content": "# Interviews\n\n## Call 1\n\nSam wants quokkaberry shingles.\n"})
        task = make_task(self.c, "engineering", "Find what the owner said about quokkaberry", kind="research")
        results, _run, _s = self.run_agent(self.c, "engineering", "task", [
            ("search", {"query": "quokkaberry"}),
            ("read_file", {"path": "global:about-quokkaberry.md", "start_line": 4}),
            ("finish", {"summary": "Found the owner's notes.", "status": "partial"})], task)
        found, read = results[0][1], results[1][1]
        self.assertIn("global:about-quokkaberry.md:4", found)
        self.assertIn("context:calls/interviews.md:5", found)
        self.assertIn("Owner's context (read-only)", found)
        self.assertIn("4: My budget is $500 a month for quokkaberry ads.", read)
        # the owner's search box gets the same answer
        res = call("GET", f"/api/companies/{self.c['id']}/search", query={"q": "quokkaberry", "scope": "context"})
        got = {(r["path"], r["line"]) for r in res["results"]}
        self.assertEqual(got, {("global:about-quokkaberry.md", 4), ("context:calls/interviews.md", 5)})
        self.assertTrue(all("[quokkaberry]" in r["snippet"] for r in res["results"]))


class TestEditFileOneLine(Replay):
    PAGE = "\n".join(f"<p>line {i}</p>" for i in range(1, 400)) + "\n"

    def test_edit_file_changes_one_line_of_a_large_page_and_nothing_else(self):
        c = make_company("EditCo", members=("ceo", "engineering"))
        page = workspace.safe_path(c, "site/index.html")
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(self.PAGE, encoding="utf-8", newline="")
        task = make_task(c, "engineering", "Fix line 200 of the landing page", kind="build",
                         deliverable="site/index.html")
        results, run, _s = self.run_agent(c, "engineering", "task", [
            ("edit_file", {"path": "site/index.html", "find": "<p>line 200</p>", "replace": "<p>Line two hundred</p>"}),
            ("finish", {"summary": "Fixed line 200.", "deliverable": "site/index.html"})], task)
        self.assertTrue(results[0][1].startswith("Edited site/index.html: 1 change."), results[0][1])
        new = page.read_text(encoding="utf-8").split("\n")
        old = self.PAGE.split("\n")
        self.assertEqual(len(new), len(old))
        self.assertEqual([i for i, (a, b) in enumerate(zip(old, new)) if a != b], [199])
        self.assertEqual(new[199], "<p>Line two hundred</p>")
        self.assertEqual(run["status"], "done")
        # the index saw the change at once
        self.assertEqual(context.search_rows(c, "two hundred")[0]["path"], "site/index.html")


if __name__ == "__main__":
    unittest.main()
