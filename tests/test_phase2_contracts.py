"""Phase 2 task contracts, the dependency graph, file leases and budgets (docs/phases/phase-2-organize.md sections
2, 3 and 6): create_task's contract fields and their texts, dispatch that waits on dependencies, leases that keep
two runs off one file, and the GPU, step, token and post budgets. Only the outside world is faked: the model, the
phone and the scheduler's thread start.

Run with:  python -m unittest tests.test_phase2_contracts -v
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
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-contracts-"))

from holdco import (agents, bus, context, contracts, db, llm, notify, roles, routing, scheduler,  # noqa: E402,F401
                    server, tools, workspace)  # noqa: F401  (server registers routes)

db.init()


# ---------------------------------------------------------------- helpers (local copies, like the other modules)

def ago(minutes: float = 0) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


_created: list[int] = []   # archived after each test, so other modules' scheduler checks never see them


def make_company(name="ContractCo", members=("ceo", "engineering", "marketing"), **settings) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    s = {"truth_scan_done": True, **settings}
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "Meal plans for dogs", "active", "building", json.dumps(s), db.now()))
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


def make_task(company, role, title, status="todo", created_by="test", **cols) -> dict:
    a = agent_of(company, role)
    base = {"company_id": company["id"], "title": title, "role": role, "agent_id": a["id"], "status": status,
            "priority": 2, "created_by": created_by, "created_at": db.now(), "updated_at": db.now(), **cols}
    tid = db.run(f"INSERT INTO tasks({', '.join(base)}) VALUES({','.join('?' * len(base))})", tuple(base.values()))
    return db.get_task(tid)


def add_goal(company, title, metric="", target=10, status="active") -> int:
    return db.run("INSERT INTO goals(company_id, title, metric, target, current, deadline, owner_role, status, "
                  "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (company["id"], title, metric, target, 0, "2026-12-01", "sales", status, db.now()))


def ctx_for(company, role="ceo", kind="task", task=None, run_id=0) -> tools.Ctx:
    a = agent_of(company, role)
    c = tools.Ctx(company=fresh(company), agent=a, kind=kind, run_id=run_id, task=task, settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(a, kind)}
    return c


def add_run(company, role="ceo", task=None, kind="task", status="running", started=None, finished=None) -> int:
    return db.run("INSERT INTO runs(company_id, agent_id, task_id, kind, status, started_at, finished_at) "
                  "VALUES(?,?,?,?,?,?,?)", (company["id"], agent_of(company, role)["id"], task["id"] if task else None,
                                            kind, status, started or db.now(), finished))


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
    """Stands in for llm.chat: each call answers with the next step, (tool, args) or a function of the call number.
    Records how often it was called."""

    def __init__(self, steps=()):
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, messages, tools_spec=None, model=None, settings=None, **kw):
        self.calls += 1
        step = self.steps.pop(0) if self.steps else ("list_tasks", {})
        if callable(step):
            step = step(self.calls)
        name, args = step
        return llm.Reply(tool_calls=[llm.ToolCall(name=name, arguments=args)], tokens=100)


def quiet():
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
    return stack


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: db.get_settings().get(k) for k in ("kill_switch", "max_steps_per_run", "scheduler_enabled",
                                                             "max_concurrent_runs")}
        db.set_settings({"kill_switch": False, "max_steps_per_run": 14, "scheduler_enabled": True,
                         "max_concurrent_runs": 2})
        self.sent: list = []
        self._patches = [mock.patch.object(notify, "send_later", lambda text, buttons=None, kind="info", **kw:
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
            cid = _created.pop()
            db.run("UPDATE runs SET status='done', finished_at=? WHERE company_id=? AND status='running'",
                   (db.now(), cid))   # run rows the lease tests opened by hand
            db.update("companies", cid, status="archived")

    def run_agent(self, company, role, task, script) -> Scripted:
        s = script if isinstance(script, Scripted) else Scripted(script)
        with mock.patch.object(agents.llm, "chat", s), quiet():
            agents.run(agent_of(company, role)["id"], "task", task_id=task["id"])
        return s


class SchedulerBase(Base):
    """A Scheduler whose tick only plans, creates routines and dispatches, with every start recorded. The tick looks
    at every active company in the shared test database, so the others are paused for the test."""

    def setUp(self):
        super().setUp()
        self.paused = [r["id"] for r in db.q("SELECT id FROM companies WHERE status='active'")]
        if self.paused:
            db.run(f"UPDATE companies SET status='paused' WHERE id IN ({','.join('?' * len(self.paused))})", self.paused)
        self.starts: list = []
        sched = scheduler.Scheduler()
        for name in ("_housekeeping", "_resume_founding", "_check_inbox"):
            setattr(sched, name, lambda *a, **kw: None)
        sched._model_server_down = lambda: False
        self.sched = sched
        self._sp = [mock.patch.object(agents, "start", lambda aid, kind, **kw: self.starts.append(
                        (aid, kind, kw.get("task_id"))) or True),
                    mock.patch.object(agents, "running_count", lambda *a, **kw: 0)]
        for p in self._sp:
            p.start()

    def tearDown(self):
        for p in reversed(self._sp):
            p.stop()
        super().tearDown()
        if self.paused:
            db.run(f"UPDATE companies SET status='active' WHERE id IN ({','.join('?' * len(self.paused))})", self.paused)

    def started_tasks(self) -> list:
        return [tid for _aid, kind, tid in self.starts if kind == "task"]

    def cycles(self, company) -> int:
        ceo = agent_of(company)
        return sum(1 for aid, kind, _t in self.starts if aid == ceo["id"] and kind == "cycle")


# =============================================================== 1. the task contract

class TestCreateTaskContract(Base):
    def setUp(self):
        super().setUp()
        self.c = make_company("PawPlan")
        self.g1 = add_goal(self.c, "10 paying customers", "stripe.customers")
        self.g2 = add_goal(self.c, "200 waitlist signups", "forms.signups", 200)
        self.dropped = add_goal(self.c, "Old goal", status="dropped")
        self.ceo = ctx_for(self.c, "ceo", kind="cycle")

    def create(self, ctx=None, **args) -> str:
        return tools.execute(ctx or self.ceo, "create_task", {"assign_to": "engineering", **args})

    def test_create_task_contract(self):
        out = self.create(title="Build the waitlist page", goal_id=self.g2, expected="signup rate from 3% to 5%",
                          inputs="research/market.md", due_days=5, budget_steps=12, budget_tokens=40000,
                          deliverable="site/index.html", acceptance="Form works",
                          checks="- browser_check site/index.html\r\n2. audit_page site/index.html\n")
        self.assertTrue(out.startswith("Created task #"), out)
        self.assertNotIn("serves no goal", out)
        t = db.get_task(int(out.split("#")[1].split(" ")[0]))
        # Budgets are the owner's: the tool no longer offers them (gpt-oss set 3 steps and 200 tokens on every task
        # in a real run), so the model's values are dropped and the company defaults apply.
        self.assertEqual((t["goal_id"], t["expected"], t["inputs"], t["budget_steps"] or 0, t["budget_tokens"] or 0),
                         (self.g2, "signup rate from 3% to 5%", "research/market.md", 0, 0))
        self.assertEqual(t["checks"], "browser_check site/index.html\naudit_page site/index.html")
        self.assertEqual(t["depends_on"], "[]")
        due = db.parse_time(t["due_at"])
        self.assertIsNotNone(due, t["due_at"])
        self.assertAlmostEqual((due - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 86400, 5, delta=0.01)

        # goal_id must be an active goal of this company
        other = make_company("OtherCo")
        foreign = add_goal(other, "Someone else's goal")
        for gid in (self.dropped, foreign, 999999):
            out = self.create(title=f"Bad goal {gid}", goal_id=gid)
            self.assertEqual(out, f"ERROR: goal #{gid} is not an active goal of this company. Active goals: "
                                  f"#{self.g1} 10 paying customers, #{self.g2} 200 waitlist signups.")
        self.assertIsNone(db.one("SELECT id FROM tasks WHERE title LIKE 'Bad goal%'"))

        # the CEO without a goal: created, with the warning listing the active goals
        out = self.create(title="Tidy the docs folder", deliverable="docs/index.md", acceptance="Lists every doc")
        tid = db.scalar("SELECT id FROM tasks WHERE company_id=? AND title='Tidy the docs folder'", (self.c["id"],))
        self.assertEqual(out, f"Created task #{tid}, but it serves no goal. Add goal_id (active goals: #{self.g1} 10 "
                              f"paying customers, #{self.g2} 200 waitlist signups) or it will be cancelled at the "
                              f"weekly review.")
        self.assertIsNone(db.get_task(tid)["goal_id"])
        # ... an executor gets no warning, and a follow-up inherits the goal of the task it came from
        parent = make_task(self.c, "marketing", "Launch post", goal_id=self.g1)
        out = self.create(ctx_for(self.c, "marketing", task=parent), title="Fix the hero copy")
        self.assertNotIn("serves no goal", out)
        self.assertEqual(db.scalar("SELECT goal_id FROM tasks WHERE company_id=? AND title='Fix the hero copy'",
                                   (self.c["id"],)), self.g1)
        # ... and a company with no active goals gets the plain text
        out = self.create(ctx_for(make_company("NoGoalCo"), "ceo", kind="cycle"), title="First task")
        self.assertTrue(out.startswith("Created task #") and "serves no goal" not in out, out)

        # depends_on: this company's open tasks only, and never a cycle
        a = make_task(self.c, "engineering", "Write the copy")
        done = make_task(self.c, "engineering", "Old work", status="done")
        theirs = make_task(other, "engineering", "Their task")
        out = self.create(title="Publish the page", goal_id=self.g2, depends_on=f"#{a['id']}, {a['id']}")
        self.assertTrue(out.startswith("Created task #"), out)
        self.assertEqual(json.loads(db.one("SELECT depends_on FROM tasks WHERE title='Publish the page' AND "
                                           "company_id=?", (self.c["id"],))["depends_on"]), [a["id"]])
        self.assertEqual(self.create(title="X1", goal_id=self.g1, depends_on=str(theirs["id"])),
                         f"ERROR: depends_on: there is no task #{theirs['id']} in this company.")
        self.assertEqual(self.create(title="X2", goal_id=self.g1, depends_on=str(done["id"])),
                         f"ERROR: depends_on: task #{done['id']} is already done, so there is nothing to wait for. "
                         f"Leave it out.")
        p = make_task(self.c, "engineering", "Loop A")
        q = make_task(self.c, "engineering", "Loop B", depends_on=json.dumps([p["id"]]))
        db.update("tasks", p["id"], depends_on=json.dumps([q["id"]]))   # a cycle made outside the tools
        out = self.create(title="X3", goal_id=self.g1, depends_on=str(q["id"]))
        self.assertTrue(out.startswith("ERROR: depends_on would make tasks wait on each other forever: #"), out)
        # ... and an edit that would close a cycle is refused with the path
        with self.assertRaises(contracts.ContractError) as e:
            contracts.validate(self.c["id"], {"depends_on": str(db.scalar(
                "SELECT id FROM tasks WHERE title='Publish the page' AND company_id=?", (self.c["id"],)))},
                db.get_task(a["id"]))
        self.assertIn(f"#{a['id']} -> #", str(e.exception))

        # checks, due_days, budget_steps
        out = self.create(title="X4", goal_id=self.g1, checks="browser_check site/index.html\nlook at it closely")
        # Phase 3 adds run_tests (the verification gate's code check)
        self.assertEqual(out, "ERROR: checks line 'look at it closely' must start with one of: browser_check, "
                              "audit_page, review_text, check_contrast, fetch_url, run_tests (one check per line, "
                              "for example 'browser_check site/index.html').")
        self.assertEqual(self.create(title="X5", goal_id=self.g1, due_days=91),   # Phase 3: the schema's range
                         "ERROR: bad arguments for create_task: due_days must be an integer 1-90; you sent 91")
        for steps in (2, 61):   # the owner's task form shares the validator
            with self.assertRaises(contracts.ContractError) as e:
                contracts.validate(self.c["id"], {"budget_steps": steps})
            self.assertEqual(str(e.exception), "budget_steps must be 3 to 60 (tool calls per attempt).")
        self.assertIsNone(db.one("SELECT id FROM tasks WHERE company_id=? AND title LIKE 'X%'", (self.c["id"],)))

    def test_update_task_and_list_tasks(self):
        dep = make_task(self.c, "engineering", "Write the copy")
        t = make_task(self.c, "marketing", "Announce the page", depends_on=json.dumps([dep["id"]]))
        out = tools.execute(self.ceo, "update_task", {"task_id": t["id"], "goal_id": self.g1, "due_days": 3})
        self.assertEqual(out, f"Task #{t['id']} updated.")
        row = db.get_task(t["id"])
        self.assertEqual(row["goal_id"], self.g1)
        due = row["due_at"][:10]
        self.assertEqual(due, (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=3)).strftime("%Y-%m-%d"))
        out = tools.execute(self.ceo, "update_task", {"task_id": t["id"], "goal_id": self.dropped})
        self.assertTrue(out.startswith(f"ERROR: goal #{self.dropped} is not an active goal"), out)
        self.assertEqual(db.get_task(t["id"])["goal_id"], self.g1)
        listing = tools.execute(self.ceo, "list_tasks", {})
        self.assertIn(f"#{t['id']} [todo] (marketing) p2 Announce the page (goal #{self.g1}, due {due}, waiting on "
                      f"#{dep['id']})", listing)
        self.assertIn(f"#{dep['id']} [todo] (engineering) p2 Write the copy", listing)
        db.update("tasks", dep["id"], status="done")
        self.assertNotIn("waiting on", tools.execute(self.ceo, "list_tasks", {}))

    def test_task_routes(self):
        dep = make_task(self.c, "engineering", "Write the copy")
        eng = agent_of(self.c, "engineering")
        out = call("POST", f"/api/companies/{self.c['id']}/tasks", {
            "title": "Owner's page review", "agent_id": eng["id"], "goal_id": self.g2, "expected": "fewer bounces",
            "depends_on": [dep["id"]], "due_at": "2026-12-01", "checks": "audit_page site/index.html",
            "budget_steps": 10})["task"]
        self.assertEqual((out["goal_id"], out["expected"], out["depends_on"], out["blocked_by"], out["due_at"],
                          out["checks"], out["budget_steps"], out["created_by"]),
                         (self.g2, "fewer bounces", [dep["id"]], [dep["id"]], "2026-12-01T23:59:59Z",
                          "audit_page site/index.html", 10, "owner"))
        for bad, text in (({"goal_id": self.dropped}, "is not an active goal"),
                          ({"due_days": 0.5}, "due_days must be 1 to 90"),
                          ({"checks": "make it pretty"}, "must start with one of"),
                          ({"depends_on": "999999"}, "there is no task #999999")):
            with self.assertRaises(routing.HTTPError) as e:
                call("POST", f"/api/companies/{self.c['id']}/tasks", {"title": "Bad", **bad})
            self.assertEqual(e.exception.status, 400)
            self.assertIn(text, e.exception.message)
        # PATCH: the same checks; a cycle is refused, null clears the goal, an unchanged closed dependency is kept
        with self.assertRaises(routing.HTTPError) as e:
            call("PATCH", f"/api/tasks/{dep['id']}", {"depends_on": str(out["id"])})
        self.assertIn("wait on each other forever", e.exception.message)
        db.update("tasks", dep["id"], status="done")
        patched = call("PATCH", f"/api/tasks/{out['id']}", {"title": "Owner's page review v2", "goal_id": None,
                                                             "depends_on": [dep["id"]], "due_at": "",
                                                             "budget_steps": 0})["task"]
        self.assertEqual((patched["goal_id"], patched["depends_on"], patched["blocked_by"], patched["due_at"],
                          patched["budget_steps"]), (None, [dep["id"]], [], "", 0))
        self.assertEqual(call("PATCH", f"/api/tasks/{out['id']}", {"goal_id": self.g1, "due_days": 7})["task"]
                         ["goal_id"], self.g1)
        # GET lists blocked_by, the goal's title and depends_on as a list
        waiting = make_task(self.c, "marketing", "Waits", depends_on=json.dumps([out["id"]]))
        rows = {t["id"]: t for t in call("GET", f"/api/companies/{self.c['id']}/tasks")["tasks"]}
        self.assertEqual(rows[waiting["id"]]["blocked_by"], [out["id"]])
        self.assertEqual(rows[out["id"]]["goal_title"], "10 paying customers")
        self.assertEqual(rows[dep["id"]]["depends_on"], [])


# =============================================================== 1b. the prompts

class TestContractPrompts(Base):
    def test_cycle_prompt_groups_by_goal(self):
        c = make_company("PawPlan")
        db.run("INSERT INTO strategy(company_id, one_liner, icp, updated_at) VALUES(?,?,?,?)",
               (c["id"], "Weekly meal plans for dog owners", "urban dog owners", db.now()))
        g1 = add_goal(c, "10 paying customers", "stripe.customers")
        g2 = add_goal(c, "200 waitlist signups", "forms.signups", 200)
        db.run("INSERT INTO metrics(company_id, name, value, source, created_at) VALUES(?,?,?,?,?)",
               (c["id"], "forms.signups", 61, "forms", db.now()))
        blocker = make_task(c, "engineering", "Build the waitlist page", goal_id=g2)
        t1 = make_task(c, "marketing", "Email 20 pet shops", goal_id=g1, due_at="2026-10-16T12:00:00Z",
                       deliverable="sales/outreach.md")
        t2 = make_task(c, "marketing", "Announce the waitlist", goal_id=g2, depends_on=json.dumps([blocker["id"]]))
        loose = make_task(c, "engineering", "Refactor the CSS")
        owner = make_task(c, "engineering", "Owner's idea", created_by="owner")
        routine = make_task(c, "ceo", "Morning report", created_by="routine", routine="ceo:Morning report")
        today = _dt.datetime.now(_dt.timezone.utc).date()   # UTC, like contracts' own dates
        db.run("INSERT INTO experiments(company_id, goal_id, hypothesis, metric, start, stop, verdict, created_at) "
               "VALUES(?,?,?,?,?,?,?,?)", (c["id"], g2, "A vet line lifts signups", "forms.signups", str(today),
                                            str(today + _dt.timedelta(days=7)), "pending", db.now()))
        due_exp = db.run("INSERT INTO experiments(company_id, goal_id, hypothesis, metric, start, stop, verdict, "
                         "created_at) VALUES(?,?,?,?,?,?,?,?)", (c["id"], g1, "Cold email works", "", "2026-01-01",
                                                                  str(today - _dt.timedelta(days=1)), "pending", db.now()))
        prop = db.run("INSERT INTO brain_changes(company_id, agent_id, section, payload, reason, status, created_at) "
                      "VALUES(?,?,?,?,?,?,?)", (c["id"], agent_of(c, "marketing")["id"], "strategy",
                                                json.dumps({"icp": "add dog walkers to the ICP"}),
                                                "3 of 3 replies came from walkers", "pending", db.now()))
        dec = db.run("INSERT INTO decisions(company_id, what, expected, review_at, created_at) VALUES(?,?,?,?,?)",
                     (c["id"], "Price at $9/month, not $15", "signup rate of 5% or more", ago(60), db.now()))
        prompt = agents.cycle_prompt(fresh(c))
        self.assertTrue(prompt.startswith(f"Run a planning cycle for PawPlan.\n\n{agents.planning.cycle_header(fresh(c))}"
                                          "\n\n## Goals (plan from these; at most 3 active)\n"), prompt[:400])
        self.assertIn(f"#{g1} 10 paying customers by 2026-12-01: stripe.customers 0 of 10, ", prompt)
        self.assertIn(f"#{g2} 200 waitlist signups by 2026-12-01: forms.signups 61 of 200, ", prompt)
        self.assertIn("owner sales. Open tasks: 1.\n", prompt)
        self.assertIn("owner sales. Open tasks: 2.\n", prompt)
        self.assertIn(f"(no goal) Open tasks: 1 (#{loose['id']}). Attach a goal with update_task(goal_id=...) or "
                      f"cancel them.", prompt)
        # The experiment, proposal and decision lines are brain.py's (the digest and brain_lookup read them alike).
        self.assertIn(f'Running: #{due_exp - 1} (goal #{g2}, stops {today + _dt.timedelta(days=7)}) "A vet line '
                      f'lifts signups". Metric forms.signups: 61 so far.', prompt)
        self.assertIn(f'Due to conclude: #{due_exp} (goal #{g1}, stops {today - _dt.timedelta(days=1)}) "Cold email '
                      f'works".\nCall conclude_experiment for each one that is due.', prompt)
        self.assertIn(f"## Brain proposals waiting for your review (review_brain_change)\n#{prop} by "
                      f"{agent_of(c, 'marketing')['name']} (strategy.icp): icp: \"add dog walkers to the ICP\". "
                      f"Reason: 3 of 3 replies came from walkers.\n", prompt)
        self.assertIn(f"## Decisions due for review (log the outcome with log_decision, or propose a change)\n#{dec} "
                      f"(review {ago(60)[:10]}) Price at $9/month, not $15. Expected: signup rate of 5% or more.",
                      prompt)
        tasks = prompt.split("## Open tasks (grouped by goal)\n", 1)[1].split("\n\n", 1)[0].splitlines()
        tasks = [ln for ln in tasks if not ln.startswith("    ")]
        self.assertEqual(tasks, [
            f"Goal #{g1}: #{t1['id']} [todo] (marketing) p2 Email 20 pet shops -> sales/outreach.md (due 2026-10-16)",
            f"Goal #{g2}: #{blocker['id']} [todo] (engineering) p2 Build the waitlist page",
            f"Goal #{g2}: #{t2['id']} [todo] (marketing) p2 Announce the waitlist (waiting on #{blocker['id']})",
            f"No goal: #{loose['id']} [todo] (engineering) p2 Refactor the CSS",
            f"No goal (owner's task): #{owner['id']} [todo] (engineering) p2 Owner's idea",
            f"Recurring duty: #{routine['id']} [todo] (ceo) p2 Morning report"])
        self.assertLess(prompt.index("## Goals"), prompt.index("## Open tasks"))
        self.assertLess(prompt.index("## Open tasks"), prompt.index("## Recently finished"))
        self.assertIn("Otherwise plan top down. For each goal: what is the biggest gap between current and target",
                      prompt)
        self.assertIn("Waiting on the owner sections of your instructions", prompt)   # Phase 0.5 stays
        self.assertNotIn("\u2014", prompt)

    def test_empty_brain_asks_for_strategy_and_goals(self):
        c = make_company("NewBrainCo")
        prompt = agents.cycle_prompt(fresh(c))
        self.assertIn("## Goals (plan from these; at most 3 active)\nThe company brain is empty: no strategy and no "
                      "goals yet. Before planning tasks, call set_strategy", prompt)
        self.assertIn("then set_goal one to three times", prompt)
        self.assertNotIn("## Experiments", prompt)
        db.run("INSERT INTO strategy(company_id, one_liner, updated_at) VALUES(?,?,?)", (c["id"], "Dog plans", db.now()))
        self.assertIn("No active goals. Call set_goal", agents.cycle_prompt(fresh(c)))

    def test_task_prompt_shows_the_contract(self):
        c = make_company("PromptCo", steps_per_task=20)
        g = add_goal(c, "10 paying customers", "stripe.customers")
        gone = make_task(c, "engineering", "Collect testimonials", status="cancelled")
        done = make_task(c, "engineering", "Write the copy", status="done", deliverable="docs/copy.md")
        t = make_task(c, "engineering", "Build the pricing page", goal_id=g, expected="checkout starts from 0 to 5",
                      inputs="docs/copy.md", due_at=ago(-60 * 24 * 2), budget_steps=12, budget_tokens=50000,
                      tokens_used=1200, checks="browser_check site/pricing.html\naudit_page site/pricing.html",
                      depends_on=json.dumps([gone["id"], done["id"]]), description="Make the page.")
        prompt = agents.task_prompt(fresh(c), db.get_task(t["id"]))
        self.assertIn(f"Contract:\nGoal: #{g} 10 paying customers by 2026-12-01: stripe.customers 0 of 10", prompt)
        self.assertIn("(this task should: checkout starts from 0 to 5)\n", prompt)
        self.assertIn("Inputs (read these first): docs/copy.md\n", prompt)
        self.assertIn(f"Note: task #{gone['id']} that this depended on was cancelled\n", prompt)
        self.assertIn(f"Builds on: task #{done['id']} Write the copy (its result: docs/copy.md)\n", prompt)
        self.assertRegex(prompt, r"Due: \d{4}-\d\d-\d\d \d\d:\d\d UTC \(in 2 days\)\n")
        self.assertIn("Budget: 12 tool calls; 50,000 tokens across attempts (1,200 used)\n", prompt)
        self.assertIn("Checks that will run when you finish: browser_check site/pricing.html; audit_page "
                      "site/pricing.html", prompt)
        self.assertLess(prompt.index("Contract:"), prompt.index("Details:\nMake the page."))
        plain = make_task(c, "engineering", "No contract")
        self.assertNotIn("Contract:", agents.task_prompt(fresh(c), plain))


# =============================================================== 2. dependencies in the scheduler

class TestDependencyDispatch(SchedulerBase):
    def test_dependency_dispatch(self):
        c = make_company("DepCo")
        db.update("companies", c["id"], last_cycle_at=db.now())   # no planning cycle in the way
        a = make_task(c, "engineering", "Build the page")
        b = make_task(c, "marketing", "Announce the page", depends_on=json.dumps([a["id"]]))
        calls: list = []
        real = contracts.dependency_status
        with mock.patch.object(contracts, "dependency_status", lambda tasks: calls.append(1) or real(tasks)):
            self.sched.tick()
        self.assertEqual(len(calls), 1, "one dependency lookup per tick")
        self.assertIn(a["id"], self.started_tasks())
        self.assertNotIn(b["id"], self.started_tasks(), "its dependency is still todo")
        rows = {t["id"]: t for t in call("GET", f"/api/companies/{c['id']}/tasks")["tasks"]}
        self.assertEqual(rows[b["id"]]["blocked_by"], [a["id"]])
        for status in ("in_progress", "blocked", "waiting"):
            db.update("tasks", a["id"], status=status)
            self.starts.clear()
            self.sched.tick()
            self.assertNotIn(b["id"], self.started_tasks(), status)
        db.update("tasks", a["id"], status="cancelled")
        self.starts.clear()
        self.sched.tick()
        self.assertIn(b["id"], self.started_tasks(), "a cancelled dependency no longer holds it up")
        self.assertIn(f"Note: task #{a['id']} that this depended on was cancelled",
                      agents.task_prompt(fresh(c), db.get_task(b["id"])))
        self.assertEqual(call("GET", f"/api/companies/{c['id']}/tasks")["tasks"][0]["blocked_by"], [])

    def test_waiting_tasks_are_not_idle_work(self):
        c = make_company("IdleCo")
        a = make_task(c, "engineering", "Fix the form", status="blocked")
        b = make_task(c, "marketing", "Announce the form", depends_on=json.dumps([a["id"]]))
        db.update("companies", c["id"], last_cycle_at=ago(20))   # past the idle trigger's 10 minutes, not yet due
        self.sched._maybe_cycle(fresh(c))
        self.assertEqual(self.cycles(c), 0, "everything waits on one task: the team is not idle")
        db.update("tasks", b["id"], status="cancelled")
        self.sched._maybe_cycle(fresh(c))
        self.assertEqual(self.cycles(c), 1, "nothing open: the idle trigger plans")


# =============================================================== 3. file leases

class TestFileLeases(Base):
    def setUp(self):
        super().setUp()
        self.c = make_company("LeaseCo")
        self.t1 = make_task(self.c, "engineering", "Build the page")
        self.t2 = make_task(self.c, "marketing", "Write the hero copy")
        self.r1 = add_run(self.c, "engineering", self.t1)
        self.r2 = add_run(self.c, "marketing", self.t2)
        self.eng = ctx_for(self.c, "engineering", task=self.t1, run_id=self.r1)
        self.mkt = ctx_for(self.c, "marketing", task=self.t2, run_id=self.r2)

    def lease_row(self, path="site/index.html") -> dict | None:
        return db.one("SELECT * FROM leases WHERE company_id=? AND path=?", (self.c["id"], path))

    def refusal(self, holder: str, task_id) -> str:
        until = db.parse_time(self.lease_row()["expires_at"]).strftime("%H:%M")
        what = f" (task #{task_id})" if task_id else ""
        return (f"ERROR: site/index.html is being edited by {holder}{what} until {until} UTC, so it can't be "
                f"changed now. Don't wait for it: save your version in another file (for example "
                f"drafts/site/index.html), say in your finish where it is so it can be applied once that edit is "
                f"done, and finish with status 'partial'.")

    def test_file_lease_conflict(self):
        html = "<html><body><h1>Dog meal plans</h1></body></html>"
        self.assertTrue(tools.execute(self.eng, "write_file", {"path": "site/index.html", "content": html})
                        .startswith("Saved site/index.html"))
        row = self.lease_row()
        self.assertEqual((row["run_id"], row["task_id"], row["agent_id"]), (self.r1, self.t1["id"], self.eng.agent["id"]))
        expires = db.parse_time(row["expires_at"])
        self.assertAlmostEqual((expires - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 60, 30, delta=1)
        expected = self.refusal(self.eng.agent["name"], self.t1["id"])
        self.assertEqual(tools.execute(self.mkt, "write_file", {"path": "site/index.html", "content": "mine"}), expected)
        self.assertEqual(tools.execute(self.mkt, "write_file", {"path": "./site//index.html", "content": "x"}), expected)
        self.assertEqual(tools.execute(self.mkt, "edit_file", {"path": "site/index.html", "find": "Dog",
                                                               "replace": "Cat"}), expected)
        self.assertEqual(tools.delete_file(self.mkt, "site/index.html"), expected)   # not in marketing's belt
        p = workspace.safe_path(self.c, "site/index.html")
        self.assertEqual(p.read_text(encoding="utf-8"), html, "the refused writes did not happen")
        # another file in the same folder is no conflict, and the holder renews its own lease
        self.assertTrue(tools.execute(self.mkt, "write_file", {"path": "site/about.html", "content": "<p>About</p>"})
                        .startswith("Saved"))
        self.assertTrue(tools.execute(self.eng, "edit_file", {"path": "site/index.html", "find": "Dog meal",
                                                              "replace": "Dog-friendly meal"}).startswith("Edited"))
        # the Files tab shows who is editing what; the owner's own edit ignores the lease
        leases = {x["path"]: x for x in call("GET", f"/api/companies/{self.c['id']}/files")["leases"]}
        self.assertEqual(leases["site/index.html"]["agent_name"], self.eng.agent["name"])
        self.assertEqual(leases["site/about.html"]["task_id"], self.t2["id"])
        self.assertEqual(call("PUT", f"/api/companies/{self.c['id']}/file", {"path": "site/index.html",
                                                                             "content": "<p>owner</p>"}), {"ok": True})
        # release frees the file for the other run
        workspace.release(self.r1)
        self.assertIsNone(self.lease_row())
        self.assertTrue(tools.execute(self.mkt, "write_file", {"path": "site/index.html", "content": "<p>m</p>"})
                        .startswith("Saved"))
        self.assertEqual(self.lease_row()["run_id"], self.r2)
        # an expired lease is taken over
        db.run("UPDATE leases SET expires_at=? WHERE run_id=?", (ago(1), self.r2))
        self.assertTrue(tools.execute(self.eng, "write_file", {"path": "site/index.html", "content": "<p>e</p>"})
                        .startswith("Saved"))
        self.assertEqual(self.lease_row()["run_id"], self.r1)
        # so is one whose run ended without releasing it
        db.update("runs", self.r1, status="done", finished_at=db.now())
        self.assertTrue(tools.execute(self.mkt, "write_file", {"path": "site/index.html", "content": "<p>m2</p>"})
                        .startswith("Saved"))
        self.assertEqual(self.lease_row()["run_id"], self.r2)

    def test_runs_without_a_task_and_contexts_without_a_run(self):
        r = add_run(self.c, "ceo", None, kind="chat")
        chat = ctx_for(self.c, "ceo", kind="chat", run_id=r)
        self.assertTrue(tools.execute(chat, "write_file", {"path": "site/index.html", "content": "<p>ceo</p>"})
                        .startswith("Saved"))
        self.assertIsNone(self.lease_row()["task_id"])
        self.assertEqual(tools.execute(self.eng, "write_file", {"path": "site/index.html", "content": "x"}),
                         self.refusal(chat.agent["name"], None))
        # a context outside any run (run_id 0) respects real leases but takes none of its own
        outside = ctx_for(self.c, "engineering")
        self.assertTrue(tools.execute(outside, "write_file", {"path": "docs/notes.md", "content": "notes"})
                        .startswith("Saved"))
        self.assertIsNone(self.lease_row("docs/notes.md"))
        self.assertTrue(tools.execute(outside, "write_file", {"path": "site/index.html", "content": "x"})
                        .startswith("ERROR: site/index.html is being edited by"))

    def test_a_finished_run_releases_its_leases(self):
        t = make_task(self.c, "engineering", "Write the FAQ", deliverable="docs/faq.md")
        self.run_agent(self.c, "engineering", t, [
            ("write_file", {"path": "docs/faq.md", "content": "# FAQ\n\nHow do plans work? Weekly, by email.\n"}),
            ("finish", {"summary": "FAQ written.", "deliverable": "docs/faq.md"})])
        run = db.one("SELECT * FROM runs WHERE task_id=? ORDER BY id DESC", (t["id"],))
        self.assertIn(run["status"], ("done", "partial"))
        self.assertTrue(workspace.safe_path(self.c, "docs/faq.md").is_file())
        self.assertIsNone(db.one("SELECT * FROM leases WHERE run_id=?", (run["id"],)), "_finalize released it")


# =============================================================== 4. budgets

class TestBudgets(SchedulerBase):
    def test_budgets(self):
        # social_post refuses at posts_per_week (done or pending in the last 7 days, and posts run by a policy)
        c = make_company("PostCo", members=("ceo", "marketing"), posts_per_week=3)
        mk = agent_of(c, "marketing")
        for status, created in (("done", ago(60)), ("pending", ago(30)), ("done", ago(60 * 24 * 8)),
                                ("rejected", ago(10))):
            db.run("INSERT INTO approvals(company_id, agent_id, action, summary, payload, status, created_at, "
                   "decided_at) VALUES(?,?,?,?,?,?,?,?)", (c["id"], mk["id"], "social_post", "post", "{}", status,
                                                           created, created))
        ctx = ctx_for(c, "marketing")
        self.assertEqual(contracts.posts_this_week(c["id"]), 2)
        out = tools.execute(ctx, "social_post", {"platform": "x", "content": "Plans for dogs, weekly."})
        self.assertNotIn("Weekly post budget", out)
        self.assertEqual(contracts.posts_this_week(c["id"]), 3)
        self.assertEqual(tools.execute(ctx, "social_post", {"platform": "x", "content": "Another one."}),
                         "Weekly post budget (3) reached. Keep the draft in a file and try again next week.")
        db.run("INSERT INTO audit(created_at, company_id, action, proposed_by, approved_by, payload, result) "
               "VALUES(?,?,?,?,?,?,?)", (db.now(), c["id"], "social_post", "Echo", "policy:auto", "{}", "Posted"))
        self.assertEqual(contracts.posts_this_week(c["id"]), 4)

        # agents.run gives a task min(steps_per_task, budget_steps) tool calls
        s = make_company("StepCo", members=("ceo", "engineering"), steps_per_task=6)
        t = make_task(s, "ceo", "Plan the week", budget_steps=4)
        self.assertEqual(self.run_agent(s, "ceo", t, []).calls, 4)
        t = make_task(s, "ceo", "Plan the month")
        self.assertEqual(self.run_agent(s, "ceo", t, []).calls, 6)
        self.assertEqual(db.get_task(t["id"])["tokens_used"], 600, "the run's tokens are added to the task")
        self.assertEqual(contracts.max_steps_for(fresh(s), {"budget_steps": 30}, 14), 6)
        self.assertEqual(contracts.max_steps_for(make_company("DefaultCo"), {"budget_steps": 0}, 14), 14)

        # a task whose tokens_used reached budget_tokens is blocked before it starts, and the owner is asked
        t = make_task(s, "ceo", "Rewrite the site", budget_tokens=1000, tokens_used=1500, attempts=1)
        self.assertEqual(self.run_agent(s, "ceo", t, []).calls, 0)
        row = db.get_task(t["id"])
        self.assertEqual(row["status"], "blocked")
        self.assertIn("[Holdco] Token budget exhausted (1,500 of 1,000 tokens used).", row["result"])
        q = db.one("SELECT * FROM approvals WHERE company_id=? AND action='question' AND task_id=?",
                   (s["id"], t["id"]))
        p = json.loads(q["payload"])
        self.assertEqual((p["pattern"], p["options"]), ("task_failed_twice", ["Skip this task",
                                                                              "Retry once more with my note",
                                                                              "Pause the company"]))
        # ... and the owner's "Retry once more" (attempts back to 0) gets its attempt
        db.update("tasks", t["id"], status="todo", attempts=0)
        self.assertEqual(self.run_agent(s, "ceo", t, []).calls, 6)

    def test_gpu_budget_stops_dispatch_and_cycles_but_not_routines(self):
        c = make_company("GpuCo", members=("ceo", "engineering", "qa"), gpu_hours_per_day=1)
        db.update("companies", c["id"], created_at=ago(60 * 24 * 30), last_cycle_at=ago(600))
        work = make_task(c, "engineering", "Build the page")
        mine = make_task(c, "engineering", "Owner's fix", created_by="owner")
        midnight = ago(180)
        add_run(c, "engineering", None, status="done", started=ago(170), finished=ago(20))   # 2.5 h today
        with mock.patch.object(contracts, "utc_midnight", lambda: midnight):
            self.assertAlmostEqual(contracts.gpu_hours(c["id"], midnight), 2.5, delta=0.01)
            self.assertTrue(contracts.over_gpu_budget(fresh(c)))
            self.sched.tick()
            self.assertNotIn(work["id"], self.started_tasks(), "no dispatch over the GPU budget")
            self.assertIn(mine["id"], self.started_tasks(), "tasks the owner created still run")
            self.assertEqual(self.cycles(c), 0, "no planning cycle")
            routines = db.q("SELECT * FROM tasks WHERE company_id=? AND created_by='routine'", (c["id"],))
            self.assertTrue(routines, "routine tasks are still created")
            self.assertFalse(set(r["id"] for r in routines) & set(self.started_tasks()), "... and queue")
            notices = db.q("SELECT * FROM events WHERE company_id=? AND kind='budget'", (c["id"],))
            self.assertEqual(len(notices), 1)
            self.assertIn("used its GPU budget for today (2.5 of 1 h)", notices[0]["message"])
            budgets = call("GET", f"/api/companies/{c['id']}")["budgets"]
            self.assertEqual((budgets["gpu_hours_today"], budgets["gpu_hours_per_day"], budgets["over_gpu_budget"]),
                             (2.5, 1.0, True))
            # a raised budget lets the work and the planning resume
            call("PATCH", f"/api/companies/{c['id']}", {"settings": {"gpu_hours_per_day": 6}})
            self.starts.clear()
            db.update("companies", c["id"], cycle_requested=0)
            self.sched.tick()
            self.assertIn(work["id"], self.started_tasks())
            self.assertEqual(self.cycles(c), 1)
            self.assertEqual(len(db.q("SELECT * FROM events WHERE company_id=? AND kind='budget'", (c["id"],))), 1)


if __name__ == "__main__":
    unittest.main()
