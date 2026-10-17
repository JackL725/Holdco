"""Phase 2 integration: the seams between the brain (brain.py), task contracts (contracts.py), rhythms
(rhythms.py) and the decisions queue (decisions.py), and the Phase 0.5 and Phase 1 code they plug into.

Each test pins one place where two builders' work meets: decisions counted like questions by the ground-truth
checks and the waiting notice, the owner's brain edits as news for change-driven planning, the digest replacing the
old metrics block, routines created while every run slot is busy, leases freed when a run's wrap-up fails, and no
orphan proposal when the owner's decision is folded into one already waiting.

Run with:  python -m unittest tests.test_phase2_seams -v
"""
from __future__ import annotations

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
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-phase2-seams-"))

from holdco import (actions, agents, brain, db, decisions, notify, planning, roles, routing, scheduler,  # noqa: E402
                    server, tools, truth, workspace)  # noqa: F401  (server registers the routes)

db.init()

_created: list[int] = []


def make_company(name="SeamCo", members=("ceo", "engineering", "marketing")) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "Weekly meal plans for dogs", "active", "building",
                  json.dumps({"truth_scan_done": True}), "2020-01-01T00:00:00Z"))
    _created.append(cid)
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def fresh(company: dict) -> dict:
    return db.get_company(company["id"])


def agent_of(company: dict, role: str = "ceo") -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def add_task(company: dict, title: str, role: str = "ceo", status: str = "in_progress", created_by: str = "Atlas",
             goal_id=None) -> dict:
    a = agent_of(company, role)
    tid = db.run("INSERT INTO tasks(company_id, title, role, agent_id, status, priority, created_by, goal_id, "
                 "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (company["id"], title, role, a["id"], status, 2, created_by, goal_id, db.now(), db.now()))
    return db.get_task(tid)


def ctx_for(company: dict, role: str = "ceo", task: dict | None = None, kind: str = "task",
            run_id: int = 0) -> tools.Ctx:
    a = agent_of(company, role)
    c = tools.Ctx(company=fresh(company), agent=a, kind=kind, run_id=run_id, task=task, settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(a, kind)}
    return c


def call_route(method: str, path: str, body=None):
    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(path)):
            return fn(None, m, body)
    raise AssertionError(f"no route for {method} {path}")


def write_brain(company: dict) -> int:
    """A strategy and one goal: the company block becomes the digest."""
    db.run("INSERT INTO strategy(company_id, one_liner, icp, updated_at) VALUES(?,?,?,?)",
           (company["id"], "Weekly meal plans for dog owners", "Dog owners who cook", db.now()))
    return db.run("INSERT INTO goals(company_id, title, metric, target, deadline, owner_role, status, created_at) "
                  "VALUES(?,?,?,?,?,?,?,?)", (company["id"], "10 paying customers", "stripe.customers", 10,
                                              "2030-12-01", "ceo", "active", db.now()))


def tearDownModule():
    for cid in _created:
        db.update("companies", cid, status="archived")
    notify.flush()


class Base(unittest.TestCase):
    def setUp(self):
        db.set_settings({"kill_switch": False})
        self.sent = []
        patcher = mock.patch.object(notify, "send", lambda text, buttons=None, kind="info", **kw:
                                    self.sent.append(text) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(notify.flush)


class TestDecisionsAreAnswers(Base):
    def test_an_answered_decision_completes_an_ask_task(self):
        """truth._ask_state: an ask task is done once its decision is answered, the same as a question."""
        c = make_company("AskCo")
        task = add_task(c, "Ask the owner where to launch first")
        self.assertEqual(truth.classify_task(task["title"]), "ask")
        ctx = ctx_for(c, task=task)
        out = tools.execute(ctx, "ask_decision", {
            "question": "Where should we launch first: the r/dogs community or the PawTalk forum?",
            "options": "r/dogs\nPawTalk forum", "recommended": "r/dogs"})
        self.assertTrue(out.startswith("Decision #"), out)
        self.assertEqual(truth.action_state(ctx, "ask"), "waiting")
        self.assertEqual(tools.finish(ctx, "Asked the owner.", "waiting"), "Session ended.")
        row = db.one("SELECT * FROM approvals WHERE company_id=? AND action='decision'", (c["id"],))
        db.update("tasks", task["id"], status="waiting")
        actions.decide(row["id"], "approve", answer="PawTalk forum")
        task = db.get_task(task["id"])
        self.assertEqual(task["status"], "todo")
        again = ctx_for(c, task=task)
        self.assertEqual(truth.action_state(again, "ask"), "done")
        self.assertEqual(tools.finish(again, "The owner chose the forum.", "done"), "Session ended.")
        self.assertEqual(again.finish_status, "done")

    def test_waiting_summary_counts_decisions_apart(self):
        c = make_company("WaitCo")
        ceo = agent_of(c)
        add_task(c, "Waiting on the owner", status="waiting")
        for action, payload in (("question", {"question": "Should the newsletter go out on Mondays or Fridays?"}),
                                ("decision", {"question": "Keep the $9 price or raise it to $15?",
                                              "options": ["Keep", "Raise"], "recommended": "Keep"}),
                                ("send_email", {"to": "jo@example.com", "subject": "Hi", "body": "Hello"})):
            db.run("INSERT INTO approvals(company_id, agent_id, action, summary, payload, status, created_at) "
                   "VALUES(?,?,?,?,?,?,?)", (c["id"], ceo["id"], action, action, json.dumps(payload), "pending",
                                             db.now()))
        info = planning.waiting_summary(fresh(c))
        self.assertEqual((info["questions"], info["decisions"], info["requests"], info["tasks"]), (1, 1, 1, 1))
        self.assertEqual(info["what"], "1 question, 1 decision, 1 approval request, 1 task waiting or blocked")
        facts = truth.facts_section(fresh(c))
        self.assertIn("2 questions (", facts)          # a decision waits on an answer, like a question
        self.assertIn("1 approval (", facts)
        waiting = truth.waiting_section(fresh(c))
        self.assertIn("- Decision #", waiting)
        self.assertNotIn("Request (Decision)", waiting)

    def test_answered_decision_reads_as_a_decision_in_the_records(self):
        c = make_company("RecordCo")
        out = decisions.create(c, agent_of(c), None, "Should we price the starter plan at $9 or $15 a month?",
                               ["$9", "$15"], "$9", 48, True)
        self.assertTrue(out.startswith("Decision #"), out)
        row = db.one("SELECT * FROM approvals WHERE company_id=? AND action='decision'", (c["id"],))
        actions.decide(row["id"], "approve", answer="$15")
        engineer = agent_of(c, "engineering")
        db.run("INSERT INTO runs(company_id, agent_id, kind, status, started_at) VALUES(?,?,?,?,?)",
               (c["id"], engineer["id"], "task", "done", "2020-01-01T00:00:00Z"))
        changes = truth.changes_section(fresh(c), engineer, "task")
        self.assertIn(f"The owner decided #{row['id']} (", changes)
        self.assertNotIn("approved request", changes)
        state = truth._state(fresh(c))
        self.assertNotIn(f"#{row['id']} Decision", truth._outbound_text(state))
        owner = truth._owner_text(state)
        self.assertIn(f"- Decision #{row['id']} from", owner)
        self.assertIn("Answer (", owner)


class TestPlanningNews(Base):
    def test_owner_brain_edit_is_news_and_agents_brain_writes_are_not(self):
        c = make_company("NewsCo")
        write_brain(c)
        base = planning.change_stamp(fresh(c))
        # An agent's lesson and the CEO's review of a proposal carry the agent: not news.
        self.assertTrue(tools.execute(ctx_for(c, "marketing"), "add_lesson",
                                      {"text": "Short posts get more replies."}).startswith("Lesson #"))
        prop = tools.execute(ctx_for(c, "marketing"), "propose_brain_change",
                             {"section": "lessons", "change": "Reply within an hour.", "reason": "Seen twice"})
        cid_ = int(prop.split("#")[1].split(" ")[0])
        self.assertTrue(tools.execute(ctx_for(c), "review_brain_change", {"change_id": cid_, "decision": "accept"})
                        .startswith(f"Proposal #{cid_} accepted"))
        self.assertEqual(planning.change_stamp(fresh(c)), base)
        # The owner's edit on the Brain tab is.
        call_route("POST", f"/api/companies/{c['id']}/brain/lessons", {"text": "Never post on Sundays."})
        self.assertNotEqual(planning.change_stamp(fresh(c)), base)

    def test_metrics_block_only_while_the_brain_is_empty(self):
        c = make_company("MetricCo")
        db.run("INSERT INTO metrics(company_id, name, value, source, created_at) VALUES(?,?,?,?,?)",
               (c["id"], "stripe.customers", 2, "stripe", db.now()))
        empty = agents.system_prompt(fresh(c), agent_of(c), "task", 14)
        self.assertIn(brain.EMPTY_LINE, empty)
        self.assertIn("# Latest metrics\n- stripe.customers: 2", empty)
        write_brain(c)
        full = agents.system_prompt(fresh(c), agent_of(c), "task", 14)
        self.assertNotIn("# Latest metrics", full)
        self.assertIn("## Metrics (latest per name; source in brackets)\nstripe.customers 2 [stripe]", full)


class TestSchedulerAndRuns(Base):
    def test_routines_are_created_while_every_run_slot_is_busy(self):
        c = make_company("BusyCo")
        duty = {"title": "Busy duty", "description": "test", "every_hours": 24, "first_after_hours": 0,
                "priority": 2, "deliverable": "", "requires": ""}
        real = roles.role_info

        def info(a):
            p = dict(real(a))
            p["routines"] = [duty] if a["company_id"] == c["id"] and a["role"] == "ceo" else []
            return p
        s = scheduler.Scheduler()
        with mock.patch.object(roles, "role_info", info), \
                mock.patch.object(agents, "running_count", lambda background_only=True: 99), \
                mock.patch.object(scheduler.Scheduler, "_housekeeping", lambda self, s: None), \
                mock.patch.object(scheduler.Scheduler, "_model_server_down", lambda self: False), \
                mock.patch.object(scheduler.Scheduler, "_resume_founding", lambda self: None), \
                mock.patch.object(agents, "start", lambda *a, **k: self.fail("nothing may start at capacity")):
            s.tick()
        self.assertTrue(db.one("SELECT id FROM tasks WHERE company_id=? AND routine='ceo:Busy duty'", (c["id"],)))

    def test_leases_are_freed_when_the_wrap_up_fails(self):
        c = make_company("LeaseCo")
        forge = agent_of(c, "engineering")
        task = add_task(c, "Build the page", role="engineering", created_by="owner")
        run_id = db.run("INSERT INTO runs(company_id, agent_id, task_id, kind, status, started_at) "
                        "VALUES(?,?,?,?,?,?)", (c["id"], forge["id"], task["id"], "task", "running", db.now()))
        ctx = ctx_for(c, "engineering", task=task, run_id=run_id)
        self.assertTrue(tools.execute(ctx, "write_file", {"path": "site/index.html", "content": "<h1>Hi</h1>"})
                        .startswith("Saved"))
        self.assertEqual([x["path"] for x in workspace.leases(fresh(c))], ["site/index.html"])
        with agents._running_lock:
            agents._running[forge["id"]] = "task"
        self.addCleanup(db.update, "runs", run_id, status="error", finished_at=db.now())   # no 'running' run left
        with mock.patch.object(agents.watchdog, "add_note", side_effect=RuntimeError("disk full")), \
                self.assertRaises(RuntimeError):
            agents._finalize(fresh(c), forge, "task", run_id, task, ctx, [], "out_of_steps", "", 10, 3)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM leases WHERE run_id=?", (run_id,)), 0)
        self.assertFalse(agents.is_running(forge["id"]))
        self.assertEqual(db.get_task(task["id"])["tokens_used"], 10)


class TestProposalsAndDecisions(Base):
    def test_no_orphan_proposal_when_the_decision_is_folded(self):
        c = make_company("FoldCo")
        ctx = ctx_for(c, "marketing")
        folded = "Already waiting for the owner as decision #4242. Don't resubmit; continue with other work."
        with mock.patch.object(decisions, "create", lambda *a, **k: folded):
            out = tools.execute(ctx, "propose_brain_change", {"section": "strategy", "change": '{"price": "$15"}',
                                                              "reason": "Competitors charge $15"})
        self.assertEqual(out, folded)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM brain_changes WHERE company_id=?", (c["id"],)), 0)

    def test_gate_dedupe_gives_the_decision_to_wait_on(self):
        c = make_company("MonthCo")
        args = (c, agent_of(c), None, "Keep, change or kill MonthCo?", ["Keep", "Kill (pause the company)"], "Keep")
        first, _ = decisions._queue(*args, origin={"kind": "monthly"})
        self.assertTrue(first.startswith("Decision #"), first)
        row = db.one("SELECT id FROM approvals WHERE company_id=? AND action='decision'", (c["id"],))
        again, waiting_on = decisions._queue(*args, origin={"kind": "monthly"})
        self.assertTrue(again.startswith("Already waiting"), again)
        self.assertEqual(waiting_on, row["id"])


if __name__ == "__main__":
    unittest.main()
