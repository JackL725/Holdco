"""Phase 2: what the first real-model run (gpt-oss:20b, a sandbox Holdco with a fake Stripe) did that the scripted
model never does, each replayed through the real code:

- founding's plan stage sent create_task with its arguments wrapped in a second call object,
  {"name": "functions.create_task", "arguments": {...}}, then wrote the calls as text with the same prefix, and
  ended with no tasks;
- the CEO set budget_steps=3 and budget_tokens=200 on every task it created;
- it assigned work by name ("Echo") instead of role;
- it queued its own "Write daily brief" task next to the scheduled evening brief;
- the weekly review wrote its memo to a file, marked its own task done with update_task and finished without
  write_brief, so no weekly brief was stored.

Run with:  python -m unittest tests.test_phase2_realmodel -v
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
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-realmodel-"))

from holdco import db, llm, rhythms, roles, tools, workspace  # noqa: E402

db.init()

_created: list[int] = []


def make_company(name="PawPlan", members=("ceo", "engineering", "marketing")) -> dict:
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
            "priority": 2, "created_by": "routine", "created_at": db.now(), "updated_at": db.now(), **cols}
    tid = db.run(f"INSERT INTO tasks({', '.join(base)}) VALUES({','.join('?' * len(base))})", tuple(base.values()))
    return db.get_task(tid)


class Base(unittest.TestCase):
    def tearDown(self):
        while _created:   # archived, so other modules' scheduler checks never see these companies
            db.run("UPDATE companies SET status='archived' WHERE id=?", (_created.pop(),))


class TestWrappedToolCalls(Base):
    NAMES = {"create_task", "finish", "add_contact"}

    def test_wrapped_native_arguments_are_unwrapped(self):
        args = {"name": "functions.create_task",
                "arguments": {"title": "Build landing page with waitlist form", "assign_to": "Forge", "goal_id": 1}}
        self.assertEqual(llm.unwrap_call("create_task", args, self.NAMES),
                         ("create_task", {"title": "Build landing page with waitlist form", "assign_to": "Forge",
                                          "goal_id": 1}))
        # arguments given as a JSON string, and the namespace on the call's own name
        name, inner = llm.unwrap_call("functions.create_task",
                                      {"name": "create_task", "arguments": json.dumps({"title": "T"})}, self.NAMES)
        self.assertEqual((name, inner), ("create_task", {"title": "T"}))

    def test_real_arguments_called_name_are_kept(self):
        args = {"name": "Dana Smith", "email": "dana@example.com"}
        self.assertEqual(llm.unwrap_call("add_contact", args, self.NAMES), ("add_contact", args))
        # a wrapper naming another tool is not unwrapped into this one
        other = {"name": "functions.finish", "arguments": {"summary": "x"}}
        self.assertEqual(llm.unwrap_call("create_task", other, self.NAMES), ("create_task", other))

    def test_text_call_with_the_namespace_is_read(self):
        text = ('{\n  "name": "functions.create_task",\n  "arguments": {\n    "title": "Draft landing copy",\n'
                '    "assign_to": "Echo"\n  }\n}\n{\n  "name": "functions.create_task",\n  "arguments": {}\n}')
        tc = llm._text_tool_call(text, self.NAMES)
        self.assertEqual((tc.name, tc.arguments), ("create_task", {"title": "Draft landing copy", "assign_to": "Echo"}))


class TestCreateTaskFromTheRealRun(Base):
    def setUp(self):
        self.c = make_company()
        self.goal = db.run("INSERT INTO goals(company_id, title, metric, target, current, deadline, owner_role, "
                           "status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (self.c["id"], "20 waitlist signups", "waitlist_signups", 20, 0, "2026-12-15",
                            "marketing", "active", db.now()))
        self.ceo = ctx_for(self.c, "ceo", kind="cycle")

    def test_budgets_are_not_offered_to_agents(self):
        props = tools.REGISTRY["create_task"].parameters["properties"]
        self.assertNotIn("budget_steps", props)
        self.assertNotIn("budget_tokens", props)
        out = tools.execute(self.ceo, "create_task", {"title": "Interview 10 target users", "assign_to": "marketing",
                                                      "goal_id": self.goal, "budget_steps": 3, "budget_tokens": 200})
        self.assertTrue(out.startswith("Created task #"), out)
        t = db.get_task(int(out.split("#")[1].split(" ")[0]))
        self.assertEqual((t["budget_steps"] or 0, t["budget_tokens"] or 0), (0, 0))

    def test_assign_by_name(self):
        echo = agent_of(self.c, "marketing")["name"]
        out = tools.execute(self.ceo, "create_task", {"title": "Offer a manual plan to 3 owners",
                                                      "assign_to": echo, "goal_id": self.goal})
        self.assertIn("(marketing)", out)
        tid = int(out.split("#")[1].split(" ")[0])
        out = tools.execute(self.ceo, "update_task", {"task_id": tid, "assign_to": agent_of(self.c, "engineering")["name"]})
        self.assertEqual(out, f"Task #{tid} updated.")
        self.assertEqual(db.get_task(tid)["role"], "engineering")

    def test_rhythm_duplicates_are_refused(self):
        for title in ("Write daily brief", "Prepare the weekly review", "Monthly strategy check"):
            out = tools.execute(self.ceo, "create_task", {"title": title, "assign_to": "ceo", "goal_id": self.goal})
            self.assertTrue(out.startswith("ERROR: Holdco runs the morning plan"), out)
        out = tools.execute(self.ceo, "create_task", {"title": "Write a creative brief for the mascot",
                                                      "assign_to": "marketing", "goal_id": self.goal})
        self.assertTrue(out.startswith("Created task #"), out)


class TestWeeklyReviewMustStoreItsBrief(Base):
    def setUp(self):
        self.c = make_company()
        self.task = make_task(self.c, "ceo", "Weekly review", routine=rhythms.WEEKLY_KEY)
        self.ctx = ctx_for(self.c, "ceo", task=self.task)

    def test_own_task_is_not_closed_with_update_task(self):
        out = tools.execute(self.ctx, "update_task", {"task_id": self.task["id"], "status": "done"})
        self.assertTrue(out.startswith(f"ERROR: task #{self.task['id']} is the task you are working on now"), out)
        self.assertEqual(db.get_task(self.task["id"])["status"], "in_progress")

    def test_finish_without_the_brief_is_refused_once_then_partial(self):
        out = tools.execute(self.ctx, "finish", {"summary": "Reviewed the week", "status": "done",
                                                 "deliverable": "briefs/weekly-2026-10-16.md"})
        self.assertTrue(out.startswith("Not finished yet: this task's deliverable is the weekly brief"), out)
        self.assertIn('write_brief(kind="weekly"', out)
        self.assertFalse(self.ctx.finished)
        tools.execute(self.ctx, "finish", {"summary": "Reviewed the week", "status": "done"})
        self.assertTrue(self.ctx.finished)
        self.assertEqual(self.ctx.finish_status, "partial")
        self.assertIn("No weekly brief was stored", self.ctx.finish_summary)

    def test_finish_after_the_brief_is_done(self):
        self.ctx.brief_written = "weekly"
        tools.execute(self.ctx, "finish", {"summary": "Memo stored", "status": "done"})
        self.assertEqual(self.ctx.finish_status, "done")

    def test_morning_plan_needs_no_brief(self):
        self.assertEqual(rhythms.brief_kind({"routine": rhythms.MORNING_KEY}), "")
        self.assertEqual(rhythms.brief_kind({"routine": "ceo:Evening brief"}), "daily")
        self.assertEqual(rhythms.brief_kind(None), "")


if __name__ == "__main__":
    unittest.main()
