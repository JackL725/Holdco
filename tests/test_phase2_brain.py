"""Phase 2, section 1: the company brain (docs/phases/phase-2-organize.md).

The digest and where it sits in the system prompt, the brain tools and who may call them, proposals that go to
the CEO or (price, offer, positioning, ICP) to the owner, the goal limit and the owner's Keep/Revert decision,
the Brain tab's routes, the belts and routines in presets.py, and founding writing the brain.

Only the outside world is faked: the model (a scripted llm.chat), the phone and the scheduler's thread start.

Run with:  python -m unittest tests.test_phase2_brain -v
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
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-brain-"))

from holdco import (actions, agents, brain, bus, db, llm, notify, planning, presets, roles, routing,  # noqa: E402
                    server, tools, workspace)  # noqa: F401  (server registers the routes)
from holdco.routing import HTTPError  # noqa: E402
from tests.fake_ollama import CHARTER, STRATEGY  # noqa: E402

db.init()

CEO_BELT = {"list_tasks", "create_task", "update_task", "read_file", "write_file", "web_search", "recall",
            "report_to_owner", "ask_owner", "update_charter", "hire_agent", "find_tool", "brain_lookup", "add_lesson",
            "set_strategy", "set_goal", "drop_goal", "log_decision", "start_experiment", "conclude_experiment",
            "review_brain_change", "ask_decision", "finish"}   # the spec's 23
PEOPLE = ("sales", "support", "success", "lifecycle", "partnerships")
HEADERS = ("# Company brain: ", "## Strategy", "## Goals (", "## Metrics (latest per name; source in brackets)",
           "## Decisions due for review", "## Running experiments", "## Lessons (newest first)", "## Contacts",
           "## Brain proposals waiting for CEO review: ")


# ---------------------------------------------------------------- helpers (local copies, like the other modules)

_created: list[int] = []   # archived after each test, so other modules that scan every company never see them


def make_company(name="BrainCo", members=("ceo", "engineering", "marketing", "sales", "research"), status="active",
                 charter="") -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, charter, settings, created_at) "
                 "VALUES(?,?,?,?,?,?,?,?)", (name, slug, "Meal plans for dogs", status, "building", charter,
                                             json.dumps({"truth_scan_done": True}), db.now()))
    _created.append(cid)
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def fresh(company) -> dict:
    return db.get_company(company["id"])


def agent_of(company, role="ceo") -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def ctx_for(company, role="ceo", kind="task", task=None) -> tools.Ctx:
    a = agent_of(company, role)
    c = tools.Ctx(company=fresh(company), agent=a, kind=kind, run_id=0, task=task, settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(a, kind)}
    return c


def founding_ctx(company, stage="charter") -> tools.Ctx:
    c = tools.Ctx(company=fresh(company), agent=agent_of(company), kind="founding", run_id=0,
                  settings=db.get_settings(), founding_stage=stage)
    c.allowed = set(roles.founding_tools(stage))
    return c


def run(ctx, tool_name, /, **args) -> str:
    return tools.execute(ctx, tool_name, args)


def in_days(n: int) -> str:
    return (_dt.date.today() + _dt.timedelta(days=n)).isoformat()


def call(method: str, path: str, body=None):
    """Dispatch to a dashboard route the way server.Handler does."""
    class Handler:
        query: dict = {}

    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(path)):
            return fn(Handler(), m, body)
    raise AssertionError(f"no route for {method} {path}")


def decisions_of(company) -> list[dict]:
    rows = db.q("SELECT * FROM approvals WHERE company_id=? AND action='decision' ORDER BY id", (company["id"],))
    for r in rows:
        r["p"] = db.loads(r["payload"], {})
    return rows


class Scripted:
    """Stands in for llm.chat: each call answers with the next (tool, args); nothing left means a text reply."""

    def __init__(self, steps=()):
        self.steps = list(steps)
        self.calls: list[dict] = []

    def __call__(self, messages, tools_spec=None, model=None, settings=None, **kw):
        self.calls.append({"tools": [t["name"] for t in tools_spec or []], "user": messages[1]["content"],
                           "system": messages[0]["content"]})
        if not self.steps:
            return llm.Reply(content="")
        name, args = self.steps.pop(0)
        return llm.Reply(tool_calls=[llm.ToolCall(name=name, arguments=args)], tokens=100)


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: db.get_settings().get(k) for k in ("kill_switch", "review_model", "max_steps_per_run")}
        db.set_settings({"kill_switch": False, "review_model": "", "max_steps_per_run": 14})
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
            db.update("companies", _created.pop(), status="archived")

    def seed_strategy(self, c, **fields):
        brain._upsert_strategy(c["id"], {**STRATEGY, **fields})


# ================================================================ the digest

class TestDigest(Base):
    def test_digest_under_limit(self):
        c = make_company("BigBrainCo")
        cid = c["id"]
        self.seed_strategy(c, positioning="The plan your vet would write. " * 20)   # far longer than its line
        for i, (metric, target) in enumerate((("stripe.customers", 10), ("forms.signups", 200), ("email.replies", 30))):
            brain._insert("goals", cid, {"title": f"Goal number {i} " + "with a long title " * 8, "metric": metric,
                                         "target": target, "deadline": in_days(30 + i), "owner_role": "sales"})
        ts = db.now()
        for i in range(40):
            db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, source, created_at) VALUES(?,?,?,?,?,?,?)",
                   (cid, None, f"stripe.metric_number_{i:02d}", i * 1.5, "", "stripe" if i % 2 else "", ts))
        for i in range(30):
            brain._insert("lessons", cid, {"text": f"Lesson {i}: " + "keep cold emails short and specific " * 7})
        stages = brain.CONTACT_STAGES
        for i in range(200):
            brain._insert("contacts", cid, {"name": f"Person {i}", "email": f"p{i}@example.com",
                                            "stage": stages[i % len(stages)], "unsubscribed": int(i % 50 == 0)})
        for i in range(7):
            db.run("INSERT INTO decisions(company_id, what, why, expected, review_at, outcome, decided_by, reversible, "
                   "created_at) VALUES(?,?,?,?,?,?,?,?,?)", (cid, f"Decision {i} " + "about pricing " * 10, "why",
                                                             "a 5% signup rate " * 5, in_days(i - 3), "", "ceo",
                                                             int(i != 0), ts))
        goal = db.scalar("SELECT MIN(id) FROM goals WHERE company_id=?", (cid,))
        for i in range(7):
            brain._insert("experiments", cid, {"goal_id": goal, "hypothesis": f"Bet {i} " + "lifts signups " * 12,
                                               "metric": "forms.signups"})
        for i in range(6):
            db.run("INSERT INTO brain_changes(company_id, agent_id, section, payload, reason, status, created_at) "
                   "VALUES(?,?,?,?,?,?,?)", (cid, agent_of(c, "sales")["id"], "lessons",
                                             json.dumps({"text": f"proposal {i} " * 30}), "because " * 30, "pending", ts))
        d = brain.digest(fresh(c))
        self.assertLessEqual(len(d), brain.DIGEST_CAP)
        for h in HEADERS:
            self.assertIn(h, d)
        self.assertNotIn("[digest cut here", d)   # the section caps alone keep it under the limit
        for label in brain.STRATEGY_LABELS.values():
            self.assertIn(f"\n{label}: ", d)
        self.assertIn("## Goals (3 of 3 active)", d)
        self.assertIn("[stripe]", d)
        self.assertIn("[manual]", d)   # an empty source (rows from before Phase 1) is manual
        self.assertIn("IRREVERSIBLE", d)
        self.assertIn("Decided by Atlas", d)
        self.assertIn("40 leads, 40 contacted, 40 replied, 40 customers, 40 lost. 4 unsubscribed.", d)
        self.assertIn("## Brain proposals waiting for CEO review: 6", d)
        self.assertEqual(d.count("\n#", d.index("## Brain proposals")), 3)   # the first 3
        metrics = d[d.index("## Metrics"):d.index("## Decisions")]
        self.assertLessEqual(metrics.count("stripe.metric_number_"), brain.METRIC_NAMES)
        self.assertIn("(20 older metric names not shown)", metrics)
        self.assertEqual(call("GET", f"/api/companies/{cid}/brain")["digest_chars"], len(d))

    def test_digest_example_shape(self):
        c = make_company("PawPlan")
        cid = c["id"]
        self.seed_strategy(c)
        gid = brain._insert("goals", cid, {"title": "10 paying customers", "metric": "stripe.customers", "target": 10,
                                           "deadline": in_days(48), "owner_role": "sales"})
        db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, source, created_at) VALUES(?,?,?,?,?,?,?)",
               (cid, None, "stripe.customers", 2, "", "stripe", db.now()))
        db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, source, created_at) VALUES(?,?,?,?,?,?,?)",
               (cid, agent_of(c, "marketing")["id"], "waitlist_signups", 58, "Tally", "manual", db.now()))
        brain._insert("lessons", cid, {"text": "r/dogs removes posts with links; post the text and reply with the link."})
        d = brain.digest(fresh(c))
        self.assertTrue(d.startswith(f"# Company brain: PawPlan (digest, {_dt.datetime.now(_dt.timezone.utc).date()})"))
        self.assertIn(f"#{gid} 10 paying customers by {in_days(48)}: stripe.customers 2 of 10, 48 days left, owner sales",
                      d)
        self.assertEqual(db.one("SELECT current FROM goals WHERE id=?", (gid,))["current"], 2)   # refreshed
        self.assertIn("waitlist_signups 58 [manual, Echo]", d)
        self.assertIn("- r/dogs removes posts with links", d)
        self.assertIn("## Decisions due for review\n(none)", d)
        self.assertIn("## Contacts\n(none)", d)

    def test_digest_replaces_charter(self):
        charter = CHARTER.replace("Dog owners who cook for their dogs", "Zebrafinch keepers who cook for their dogs")
        with_brain = make_company("BrainedCo", charter=charter)
        self.seed_strategy(with_brain)
        prompt = agents.system_prompt(fresh(with_brain), agent_of(with_brain), "task", 14)
        self.assertIn("# Company brain: BrainedCo", prompt)
        self.assertIn(brain.CHARTER_LINE, prompt)
        self.assertIn("Customer (ICP): Dog owners who cook", prompt)
        self.assertNotIn("Zebrafinch", prompt)
        self.assertNotIn("# The company\n", prompt)
        # a company founded before Phase 2 keeps its charter until the CEO writes the brain
        old = make_company("OldCo", charter=charter)
        prompt = agents.system_prompt(fresh(old), agent_of(old), "task", 14)
        self.assertIn("# Company brain: OldCo", prompt)
        self.assertIn(brain.EMPTY_LINE, prompt)
        self.assertIn("# The company\n", prompt)
        self.assertIn("Zebrafinch", prompt)
        # goals but no strategy yet: the digest, and the charter's key points are not lost
        brain._insert("goals", old["id"], {"title": "20 signups", "metric": "signups", "target": 20,
                                           "deadline": in_days(30)})
        section = brain.company_section(fresh(old))
        self.assertIn("## Goals (1 of 3 active)", section)
        self.assertIn("Zebrafinch", section)


# ================================================================ belts

class TestBelts(Base):
    def test_brain_tools_by_role(self):
        self.assertEqual(set(presets.PRESETS["ceo"]["tools"]), CEO_BELT | {"write_brief"})
        self.assertEqual(len(presets.PRESETS["ceo"]["tools"]), 24)
        c = make_company("BeltCo")
        sales, marketing, ceo = ctx_for(c, "sales"), ctx_for(c, "marketing"), ctx_for(c)
        self.assertTrue(run(sales, "add_contact", name="Jo", email="jo@happypaws.co").startswith("Contact #"))
        self.assertIn("no tool called 'add_contact'", run(marketing, "add_contact", name="Jo", email="jo@x.co"))
        self.assertIn("no tool called 'set_goal'", run(sales, "set_goal", title="x", metric="m", target=1,
                                                         deadline=in_days(9)))
        self.assertTrue(run(ceo, "set_goal", title="20 signups", metric="signups", target=20,
                            deadline=in_days(30)).startswith("Goal #"))
        for key, p in presets.PRESETS.items():
            names = set(p["tools"])
            self.assertNotIn("remember", names, key)
            if key == "ceo":
                continue
            self.assertLessEqual(len(p["tools"]), 18, key)
            self.assertTrue({"brain_lookup", "propose_brain_change", "add_lesson"} <= names, key)
            self.assertEqual({"add_contact", "update_contact"} <= names, key in PEOPLE, key)
            self.assertFalse(names & {"set_goal", "set_strategy", "review_brain_change", "ask_decision"}, key)
        self.assertIn("write_brief", presets.PRESETS["analyst"]["tools"])
        self.assertTrue({"brain_lookup", "propose_brain_change"} <= set(presets.CUSTOM_TOOLS))
        self.assertNotIn("remember", presets.CUSTOM_TOOLS)
        self.assertTrue({"set_strategy", "set_goal"} <= set(roles.founding_tools("charter")))
        self.assertTrue({"set_strategy", "set_goal"} <= set(roles.FOUNDING_TOOLS))
        self.assertEqual(tools.risk_of("brain_lookup"), "read")
        for name in ("propose_brain_change", "add_contact", "update_contact", "add_lesson", "set_strategy", "set_goal",
                     "drop_goal", "log_decision", "start_experiment", "conclude_experiment", "review_brain_change"):
            self.assertEqual(tools.risk_of(name), "write", name)

    def test_routines(self):
        ceo = {r["title"]: r for r in presets.PRESETS["ceo"]["routines"]}
        self.assertEqual(set(ceo), {"Morning plan", "Evening brief", "Weekly review", "Monthly strategy check"})
        self.assertEqual((ceo["Morning plan"]["every_hours"], ceo["Morning plan"]["at_hour"],
                          ceo["Morning plan"]["first_after_hours"]), (24, 7, 20))
        self.assertEqual((ceo["Evening brief"]["at_hour"], ceo["Evening brief"]["requires"],
                          ceo["Evening brief"]["deliverable"]), (18, "no_analyst", ""))
        self.assertEqual((ceo["Weekly review"]["every_hours"], ceo["Weekly review"]["at_hour"],
                          ceo["Weekly review"]["first_after_hours"]), (168, 9, 160))
        self.assertEqual((ceo["Monthly strategy check"]["every_hours"], ceo["Monthly strategy check"]["at_hour"],
                          ceo["Monthly strategy check"]["first_after_hours"]), (720, None, 700))
        analyst = {r["title"]: r for r in presets.PRESETS["analyst"]["routines"]}
        self.assertEqual(set(analyst), {"Evening brief", "Weekly KPI report"})
        self.assertEqual((analyst["Evening brief"]["at_hour"], analyst["Evening brief"]["requires"]), (18, ""))
        listed = next(p for p in presets.summary_list() if p["key"] == "ceo")["routines"]
        self.assertEqual({r["title"]: r["at_hour"] for r in listed}["Morning plan"], 7)
        self.assertIsNone(presets.R("x", "y", 24)["at_hour"])


# ================================================================ goals

class TestGoals(Base):
    def test_goal_limit_and_owner_decision(self):
        c = make_company("GoalCo")
        ceo = ctx_for(c)
        out = run(ceo, "set_goal", title="10 paying customers", metric="stripe.customers", target=10,
                  deadline=in_days(60), owner_role="sales")
        self.assertTrue(out.startswith("Goal #"), out)
        first = int(out.split("#")[1].split(" ")[0])
        rows = decisions_of(c)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["action"], row["status"]), ("decision", "pending"))
        p = row["p"]
        self.assertEqual((p["options"], p["recommended"], p["reversible"]), (["Keep", "Revert"], "Keep", True))
        self.assertTrue(p["question"].startswith("Atlas changed the goals. Active goals now: #"), p["question"])
        self.assertEqual(p["origin"], {"kind": "goals", "before": [], "after": [first], "added": [first]})
        hours = (db.parse_time(p["deadline_at"]) - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 3600
        self.assertAlmostEqual(hours, 48, delta=0.1)
        # two more goals in the same cycle update that decision instead of adding to the inbox
        for title in ("200 waitlist signups", "30 replies"):
            self.assertIn("now lists this one too", run(ceo, "set_goal", title=title, metric="signups", target=200,
                                                       deadline=in_days(30)))
        rows = decisions_of(c)
        self.assertEqual(len(rows), 1)
        active = brain._active_ids(c["id"])
        self.assertEqual(rows[0]["p"]["origin"]["after"], active)
        self.assertEqual(rows[0]["p"]["origin"]["added"], active)
        self.assertIn("30 replies", rows[0]["p"]["question"])
        # a fourth is refused with the spec's text
        out = run(ceo, "set_goal", title="Fourth", metric="x", target=1, deadline=in_days(10))
        self.assertTrue(out.startswith("ERROR: 3 goals are active; drop or finish one first"), out)
        self.assertEqual(len(brain._active_ids(c["id"])), 3)
        # Keep changes nothing; Revert restores the set from before the first change
        origin = rows[0]["p"]["origin"]
        brain.on_owner_decision(c["id"], origin, "Keep")
        self.assertEqual(brain._active_ids(c["id"]), active)
        brain.on_owner_decision(c["id"], origin, "Revert")
        self.assertEqual(brain._active_ids(c["id"]), [])
        self.assertEqual({g["status"] for g in db.q("SELECT status FROM goals WHERE company_id=?", (c["id"],))},
                         {"dropped"})

    def test_revert_brings_back_goals_that_were_active(self):
        c = make_company("RevertCo")
        keep = brain._insert("goals", c["id"], {"title": "Old goal", "metric": "m", "target": 5, "deadline": in_days(9)})
        origin = {"kind": "goals", "before": [keep], "after": [keep, 999]}
        new = brain._insert("goals", c["id"], {"title": "New goal", "metric": "m", "target": 6, "deadline": in_days(9)})
        db.update("goals", keep, status="dropped")
        origin["after"] = [new]
        brain.on_owner_decision(c["id"], origin, "Revert")
        self.assertEqual(brain._active_ids(c["id"]), [keep])

    def test_founding_goals_ask_nothing_and_validation(self):
        c = make_company("FoundGoalCo")
        f = founding_ctx(c)
        out = run(f, "set_goal", title="200 signups", metric="Waitlist Signups", target="200", deadline=in_days(45),
                  owner_role="astronaut")
        self.assertTrue(out.startswith("Goal #"), out)
        self.assertIn("No teammate has the role 'astronaut', so the CEO owns this goal", out)
        self.assertIn("No number is recorded as waitlist_signups yet", out)
        self.assertEqual(decisions_of(c), [])
        g = db.one("SELECT * FROM goals WHERE company_id=?", (c["id"],))
        self.assertEqual((g["metric"], g["target"], g["owner_role"], g["status"]), ("waitlist_signups", 200, "ceo",
                                                                                     "active"))
        self.assertIn("already says that", run(f, "set_goal", title="200 SIGNUPS", metric="m", target=1,
                                               deadline=in_days(9)))
        self.assertIn("deadline must be a future date", run(f, "set_goal", title="Late", metric="m", target=1,
                                                            deadline="2020-01-01"))
        self.assertIn("missing required argument(s): target", run(f, "set_goal", title="No number", metric="m",
                                                                  target="lots", deadline=in_days(9)))

    def test_drop_goal_and_experiments(self):
        c = make_company("BetCo")
        ceo = ctx_for(c)
        gid = brain._insert("goals", c["id"], {"title": "20 signups", "metric": "forms.signups", "target": 20,
                                               "deadline": in_days(30)})
        out = run(ceo, "start_experiment", goal_id=gid, hypothesis="A vet line lifts signups to 5%")
        self.assertIn(f"started for goal #{gid}; it stops on {in_days(14)}", out)
        eid = int(out.split("#")[1].split(" ")[0])
        e = db.one("SELECT * FROM experiments WHERE id=?", (eid,))
        self.assertEqual((e["metric"], e["verdict"], e["start"]), ("forms.signups", "pending", in_days(0)))
        self.assertIn("is not an active goal", run(ceo, "start_experiment", goal_id=9999, hypothesis="x"))
        self.assertIn("verdict must be won, lost or inconclusive",
                      run(ceo, "conclude_experiment", experiment_id=eid, result="3%", verdict="meh"))
        self.assertIn("concluded: lost", run(ceo, "conclude_experiment", experiment_id=eid, result="3.1% [forms]",
                                             verdict="LOST"))
        self.assertIn("already concluded as lost",
                      run(ceo, "conclude_experiment", experiment_id=eid, result="x", verdict="won"))
        out = run(ceo, "drop_goal", goal_id=gid, status="done", note="hit it")
        self.assertIn(f"Goal #{gid} (20 signups) closed as done: hit it", out)
        self.assertEqual(db.one("SELECT status FROM goals WHERE id=?", (gid,))["status"], "done")
        self.assertIn("already done", run(ceo, "drop_goal", goal_id=gid, status="dropped"))
        self.assertEqual(decisions_of(c), [])   # closing a goal asks the owner nothing

    def test_log_decision(self):
        c = make_company("LogCo")
        out = run(ctx_for(c), "log_decision", what="Price at $9/month", why="Competitors charge $9-15",
                  expected="5% signup rate", review_days=3, reversible=False)
        self.assertIn(f"comes back for review on {in_days(3)}", out)
        self.assertIn("Marked irreversible", out)
        d = db.one("SELECT * FROM decisions WHERE company_id=?", (c["id"],))
        self.assertEqual((d["decided_by"], d["reversible"], d["outcome"]), ("ceo", 0, ""))
        self.assertIn(f"(review {in_days(3)}, IRREVERSIBLE) Price at $9/month. Expected: 5% signup rate. Decided by "
                      "Atlas.", brain.digest(fresh(c)))
        self.assertIn("ERROR: say why", run(ctx_for(c), "log_decision", what="Something"))
        self.assertIn("ERROR: say what was decided", run(ctx_for(c), "log_decision", why="Because"))

    def test_log_decision_outcome(self):
        c = make_company("OutcomeCo")
        ceo = ctx_for(c)
        run(ceo, "log_decision", what="Price at $9/month", why="Cheaper than the rest", expected="5% signup rate",
            review_days=1)
        d = db.one("SELECT * FROM decisions WHERE company_id=?", (c["id"],))
        self.assertEqual([r["id"] for r in brain.decisions_due(c["id"])], [d["id"]])
        out = run(ceo, "log_decision", decision_id=d["id"], outcome="3.1% signup rate [forms], below the 5% expected")
        self.assertIn(f"Outcome of decision #{d['id']} saved; it is no longer due for review", out)
        after = db.one("SELECT * FROM decisions WHERE id=?", (d["id"],))
        self.assertEqual(after["outcome"], "3.1% signup rate [forms], below the 5% expected")
        self.assertEqual((after["review_at"], after["what"]), (d["review_at"], d["what"]))   # review_at is kept
        self.assertEqual(brain.decisions_due(c["id"]), [])
        self.assertIn("## Decisions due for review\n(none)", brain.digest(fresh(c)))
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM decisions WHERE company_id=?", (c["id"],)), 1)
        self.assertIn("give the outcome", run(ceo, "log_decision", decision_id=d["id"]))
        other = make_company("OtherCo")
        foreign = db.run("INSERT INTO decisions(company_id, what, review_at, created_at) VALUES(?,?,?,?)",
                         (other["id"], "Not yours", in_days(1), db.now()))
        self.assertIn(f"decision #{foreign} not found", run(ceo, "log_decision", decision_id=foreign, outcome="x"))
        self.assertEqual(db.one("SELECT outcome FROM decisions WHERE id=?", (foreign,))["outcome"], "")


# ================================================================ strategy, lessons, contacts

class TestStrategyLessonsContacts(Base):
    def test_set_strategy_needs_every_field_during_founding(self):
        c = make_company("StratCo")
        f = founding_ctx(c)
        out = run(f, "set_strategy", one_liner="Plans for dogs", price="$9/month")
        self.assertTrue(out.startswith("ERROR: during founding set_strategy needs all seven fields; missing: icp, "
                                       "problem, offer, positioning, voice."), out)
        self.assertIsNone(brain.get_strategy(c["id"]))
        self.assertTrue(run(f, "set_strategy", **STRATEGY).startswith("Strategy saved (7 fields)"))
        self.assertEqual(fresh(c)["mission"], STRATEGY["one_liner"])
        # outside founding a partial update keeps the rest
        self.assertTrue(run(ctx_for(c), "set_strategy", voice="Plain and kind.").startswith("Strategy saved (1 field)"))
        s = brain.get_strategy(c["id"])
        self.assertEqual((s["voice"], s["price"]), ("Plain and kind.", STRATEGY["price"]))
        self.assertIn("give at least one field", run(ctx_for(c), "set_strategy"))

    def test_lessons(self):
        c = make_company("LessonCo")
        eng = ctx_for(c, "engineering")
        self.assertIn("Lesson #", run(eng, "add_lesson", text="Netlify forms need the netlify attribute."))
        self.assertIn("Already saved as lesson #", run(eng, "add_lesson", text="netlify forms need the NETLIFY attribute."))
        out = run(eng, "add_lesson", text="x" * 301)
        self.assertTrue(out.startswith("ERROR: a lesson is one short rule of at most 300 characters (this one has 301)"))
        self.assertIn("ERROR: the lesson is empty", run(eng, "add_lesson", text="   "))

    def test_contacts_unique_and_unsubscribe_final(self):
        c = make_company("CrmCo")
        sales = ctx_for(c, "sales")
        out = run(sales, "add_contact", name="Jo Smith", email="Jo@HappyPaws.co", company="Happy Paws", source="research")
        self.assertTrue(out.startswith("Contact #"), out)
        jo = int(out.split("#")[1].split(" ")[0])
        out = run(sales, "add_contact", name="Joanna Smith", email="jo@happypaws.co", stage="replied")
        self.assertIn(f"Contact #{jo} already had that email; updated it", out)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM contacts WHERE company_id=?", (c["id"],)), 1)
        row = db.one("SELECT * FROM contacts WHERE id=?", (jo,))
        self.assertEqual((row["name"], row["stage"], row["email"]), ("Joanna Smith", "replied", "Jo@HappyPaws.co"))
        self.assertIn("not a valid email address", run(sales, "add_contact", name="X", email="nope"))
        self.assertIn("stage must be one of lead, contacted", run(sales, "add_contact", name="Y", stage="hot"))
        self.assertIn("give at least a name or an email", run(sales, "add_contact", company="Acme"))
        out = run(sales, "update_contact", contact_id=jo, stage="customer", last_touch="today", notes="Paid by card")
        self.assertIn(f"stage customer, last_touch {in_days(0)}", out)
        self.assertIn(f"[{in_days(0)}] Paid by card", db.one("SELECT notes FROM contacts WHERE id=?", (jo,))["notes"])
        self.assertIn("never contact them again", run(sales, "update_contact", contact_id=jo, unsubscribed=True))
        self.assertIn("That is final", run(sales, "update_contact", contact_id=jo, unsubscribed=False))
        run(sales, "add_contact", name="Jo again", email="JO@happypaws.co", consent="yes")   # cannot resubscribe
        self.assertEqual(db.one("SELECT unsubscribed FROM contacts WHERE id=?", (jo,))["unsubscribed"], 1)
        self.assertIn("contact #999999 not found", run(sales, "update_contact", contact_id=999999, stage="lost"))
        self.assertIn("1 customer. 1 unsubscribed.", brain.digest(fresh(c)))

    def test_lookup_hides_emails_from_roles_that_do_not_message_people(self):
        c = make_company("LookCo")
        brain._insert("contacts", c["id"], {"name": "Jo Smith", "email": "jo@happypaws.co", "company": "Happy Paws"})
        brain._insert("lessons", c["id"], {"text": "Pet shops reply on Tuesdays."})
        sales_view = run(ctx_for(c, "sales"), "brain_lookup", section="contacts", query="happy")
        self.assertIn("<jo@happypaws.co>", sales_view)
        eng_view = run(ctx_for(c, "engineering"), "brain_lookup", section="contacts", query="happy")
        self.assertIn("Jo Smith (email hidden for your role)", eng_view)
        self.assertNotIn("happypaws.co", eng_view.replace("Happy Paws", ""))
        self.assertIn("No contacts match 'happypaws.co'",
                      run(ctx_for(c, "engineering"), "brain_lookup", section="contacts", query="happypaws.co"))
        self.assertIn("<jo@happypaws.co>", run(ctx_for(c), "brain_lookup", section="contact"))   # the CEO sees it
        self.assertIn("Pet shops reply on Tuesdays", run(ctx_for(c, "engineering"), "brain_lookup", section="lessons",
                                                         query="tuesdays shops nonsense"))
        self.assertIn("The strategy is not set yet", run(ctx_for(c, "marketing"), "brain_lookup", section="strategy"))
        self.assertIn("ERROR: unknown section", run(ctx_for(c, "marketing"), "brain_lookup", section="secrets"))


# ================================================================ proposals

class TestProposals(Base):
    def test_proposal_routing(self):
        c = make_company("ProposeCo")
        self.seed_strategy(c)
        marketing, ceo = ctx_for(c, "marketing"), ctx_for(c)
        out = run(marketing, "propose_brain_change", section="strategy", change='{"price": "$15"}',
                  reason="Competitors charge $15")
        self.assertIn("so the owner decides it, not the CEO", out)
        price = db.one("SELECT * FROM brain_changes WHERE company_id=? ORDER BY id DESC", (c["id"],))
        self.assertEqual(price["status"], "owner")
        rows = decisions_of(c)
        self.assertEqual(len(rows), 1)
        p = rows[0]["p"]
        self.assertEqual((p["options"], p["recommended"], p["reversible"]),
                         (["Accept the change", "Reject it"], "Reject it", True))
        self.assertEqual(p["origin"], {"kind": "brain_change", "change_id": price["id"]})
        self.assertIn('price: "$15"', p["question"])
        self.assertIn('price now "$9 per month (to test)."', p["question"])
        hours = (db.parse_time(p["deadline_at"]) - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 3600
        self.assertAlmostEqual(hours, 72, delta=0.1)
        self.assertIn("waits for the owner's decision",
                      run(ceo, "review_brain_change", change_id=price["id"], decision="accept"))
        self.assertIn("Already proposed as #", run(marketing, "propose_brain_change", section="strategy",
                                                   change={"price": "$15"}))
        # a lesson proposal waits for the CEO and is applied when accepted
        out = run(marketing, "propose_brain_change", section="lessons", change="Reddit removes posts with links.")
        self.assertIn("saved for the CEO's review", out)
        lesson = db.one("SELECT * FROM brain_changes WHERE company_id=? ORDER BY id DESC", (c["id"],))
        self.assertEqual(lesson["status"], "pending")
        self.assertEqual(len(decisions_of(c)), 1)
        self.assertIn(f"#{lesson['id']} by Echo (lessons): Reddit removes posts with links.", brain.digest(fresh(c)))
        out = run(ceo, "review_brain_change", change_id=lesson["id"], decision="accept", note="Good catch")
        self.assertTrue(out.startswith(f"Proposal #{lesson['id']} accepted: lesson #"), out)
        lesson = db.one("SELECT * FROM brain_changes WHERE id=?", (lesson["id"],))
        self.assertEqual(lesson["status"], "accepted")
        self.assertTrue(lesson["decided_at"])
        self.assertTrue(db.one("SELECT id FROM lessons WHERE company_id=? AND text='Reddit removes posts with links.'",
                               (c["id"],)))
        self.assertIn("already accepted", run(ceo, "review_brain_change", change_id=lesson["id"], decision="reject"))
        # a strategy change outside price, offer, positioning and ICP goes to the CEO; prose is refused
        self.assertIn("names its fields as JSON",
                      run(marketing, "propose_brain_change", section="strategy", change="add dog walkers to the ICP"))
        out = run(marketing, "propose_brain_change", section="strategy", change='{"voice": "Short and warm."}')
        self.assertIn("saved for the CEO's review", out)
        voice = db.one("SELECT * FROM brain_changes WHERE company_id=? ORDER BY id DESC", (c["id"],))
        self.assertIn("strategy updated (voice)", run(ceo, "review_brain_change", change_id=voice["id"], decision="accept"))
        self.assertEqual(brain.get_strategy(c["id"])["voice"], "Short and warm.")
        # a contact proposal from a role without the contact tools; then a rejected one
        out = run(marketing, "propose_brain_change", section="contacts",
                  change='{"name": "Ann", "email": "ann@vet.example", "source": "a reply"}')
        contact = int(out.split("#")[1].split(" ")[0])
        self.assertIn("contact #", run(ceo, "review_brain_change", change_id=contact, decision="accept"))
        self.assertTrue(db.one("SELECT id FROM contacts WHERE company_id=? AND email='ann@vet.example'", (c["id"],)))
        out = run(marketing, "propose_brain_change", section="decisions",
                  change='{"what": "Post twice a week", "decided_by": "owner"}')
        self.assertIn("accepted: decision #", run(ceo, "review_brain_change",
                                                  change_id=int(out.split("#")[1].split(" ")[0]), decision="accept"))
        self.assertEqual(db.one("SELECT decided_by FROM decisions WHERE company_id=? AND what='Post twice a week'",
                                (c["id"],))["decided_by"], "ceo")   # a proposal cannot speak for the owner
        out = run(marketing, "propose_brain_change", section="decisions", change='{"what": "Stop Reddit posts"}')
        rejected = int(out.split("#")[1].split(" ")[0])
        self.assertEqual(run(ceo, "review_brain_change", change_id=rejected, decision="no"),
                         f"Proposal #{rejected} rejected.")
        self.assertEqual(db.one("SELECT status FROM brain_changes WHERE id=?", (rejected,))["status"], "rejected")
        self.assertTrue(db.one("SELECT id FROM memory WHERE company_id=? AND content LIKE ?",
                               (c["id"], f"%rejected brain proposal #{rejected}%")))
        # bad proposals are refused with what to fix
        self.assertIn("goals has no field budget", run(marketing, "propose_brain_change", section="goals",
                                                       change='{"budget": 5}'))
        self.assertIn("target must be a number", run(marketing, "propose_brain_change", section="goals",
                                                     change='{"title": "x", "target": "many"}'))
        self.assertIn("section must be one of", run(marketing, "propose_brain_change", section="proposals",
                                                    change="x"))

    def test_owner_decides_a_routed_proposal(self):
        c = make_company("OwnerCo")
        self.seed_strategy(c)
        sales = ctx_for(c, "sales")
        run(sales, "propose_brain_change", section="strategy", change='{"icp": "Dog walkers too", "voice": "Bold"}')
        accept = db.one("SELECT * FROM brain_changes WHERE company_id=? ORDER BY id DESC", (c["id"],))
        run(sales, "propose_brain_change", section="strategy", change='{"offer": "Plans and treats"}')
        reject = db.one("SELECT * FROM brain_changes WHERE company_id=? ORDER BY id DESC", (c["id"],))
        self.assertEqual((accept["status"], reject["status"]), ("owner", "owner"))
        brain.on_owner_decision(c["id"], {"kind": "brain_change", "change_id": accept["id"]}, "Accept the change")
        brain.on_owner_decision(c["id"], {"kind": "brain_change", "change_id": reject["id"]}, "Reject it")
        s = brain.get_strategy(c["id"])
        self.assertEqual((s["icp"], s["voice"], s["offer"]), ("Dog walkers too", "Bold", STRATEGY["offer"]))
        a, r = (db.one("SELECT * FROM brain_changes WHERE id=?", (x["id"],)) for x in (accept, reject))
        self.assertEqual((a["status"], r["status"]), ("accepted", "rejected"))
        self.assertTrue(a["decided_at"] and r["decided_at"])
        brain.on_owner_decision(c["id"], {"kind": "brain_change", "change_id": reject["id"]}, "Accept the change")
        self.assertEqual(brain.get_strategy(c["id"])["offer"], STRATEGY["offer"])   # decided already: no change


# ================================================================ the Brain tab's routes

class TestRoutes(Base):
    def test_routes(self):
        c = make_company("RouteCo")
        cid = c["id"]
        before = db.scalar("SELECT MAX(id) FROM events") or 0
        out = call("POST", f"/api/companies/{cid}/brain/strategy", {"price": "$12/month", "evil; DROP": "x"})
        self.assertEqual(out["row"]["price"], "$12/month")
        g = call("POST", f"/api/companies/{cid}/brain/goals", {"title": "20 signups", "metric": "Signups",
                                                               "target": "20", "deadline": in_days(20)})
        self.assertEqual((g["row"]["metric"], g["row"]["target"], g["row"]["status"]), ("signups", 20, "active"))
        for i in range(2):
            call("POST", f"/api/companies/{cid}/brain/goals", {"title": f"G{i}", "target": 1})
        with self.assertRaises(HTTPError) as e:
            call("POST", f"/api/companies/{cid}/brain/goals", {"title": "Fourth"})
        self.assertEqual(e.exception.status, 400)
        self.assertIn("3 goals are active; drop or finish one first", e.exception.message)
        with self.assertRaises(HTTPError) as e:
            call("POST", f"/api/companies/{cid}/brain/goals", {"title": "Bad", "target": "lots", "status": "done"})
        self.assertIn("Target must be a number", e.exception.message)
        contact = call("POST", f"/api/companies/{cid}/brain/contacts", {"name": "Jo", "email": "jo@x.co"})["id"]
        with self.assertRaises(HTTPError) as e:
            call("POST", f"/api/companies/{cid}/brain/contacts", {"name": "Jo 2", "email": "JO@x.co"})
        self.assertIn(f"already exists (#{contact})", e.exception.message)
        lesson = call("POST", f"/api/companies/{cid}/brain/lessons", {"text": "Short emails get replies."})["id"]
        call("POST", f"/api/companies/{cid}/brain/decisions", {"what": "Price at $12", "reversible": False})
        call("POST", f"/api/companies/{cid}/brain/experiments", {"hypothesis": "Vet line lifts signups",
                                                                 "goal_id": g["id"]})
        with self.assertRaises(HTTPError) as e:
            call("POST", f"/api/companies/{cid}/brain/experiments", {"hypothesis": "x", "goal_id": 999999})
        self.assertIn("not a goal of this company", e.exception.message)
        with self.assertRaises(HTTPError) as e:
            call("POST", f"/api/companies/{cid}/brain/secrets", {"x": 1})
        self.assertEqual(e.exception.status, 404)
        # inline edits: the owner may resubscribe; an active goal over the limit is refused
        self.assertEqual(call("PATCH", f"/api/brain/contacts/{contact}", {"stage": "customer", "unsubscribed": True,
                                                                          "id": 1})["row"]["stage"], "customer")
        self.assertEqual(call("PATCH", f"/api/brain/contacts/{contact}", {"unsubscribed": False})["row"]["unsubscribed"],
                         0)
        self.assertEqual(call("PATCH", f"/api/brain/strategy/{cid}", {"voice": "Calm"})["row"]["voice"], "Calm")
        call("PATCH", f"/api/brain/goals/{g['id']}", {"status": "done"})
        extra = call("POST", f"/api/companies/{cid}/brain/goals", {"title": "Replacement", "target": 2})["id"]
        with self.assertRaises(HTTPError):
            call("PATCH", f"/api/brain/goals/{g['id']}", {"status": "active"})
        with self.assertRaises(HTTPError) as e:
            call("PATCH", f"/api/brain/lessons/{lesson}", {"text": "x" * 400})
        self.assertIn("at most 300 characters", e.exception.message)
        with self.assertRaises(HTTPError) as e:
            call("PATCH", f"/api/brain/lessons/{lesson}", {"nothing": 1})
        self.assertIn("Nothing to change", e.exception.message)
        # deletes; the strategy is only edited
        self.assertEqual(call("DELETE", f"/api/brain/lessons/{lesson}"), {"ok": True})
        self.assertIsNone(db.one("SELECT id FROM lessons WHERE id=?", (lesson,)))
        with self.assertRaises(HTTPError) as e:
            call("DELETE", f"/api/brain/strategy/{cid}")
        self.assertIn("cannot be deleted", e.exception.message)
        call("DELETE", f"/api/brain/goals/{extra}")
        # the whole brain, with the digest
        b = call("GET", f"/api/companies/{cid}/brain")
        self.assertEqual(set(b) >= {"strategy", "goals", "contacts", "decisions", "experiments", "lessons",
                                    "proposals", "digest", "digest_chars"}, True)
        self.assertEqual(b["strategy"]["price"], "$12/month")
        self.assertEqual(b["digest_chars"], len(b["digest"]))
        self.assertIn("IRREVERSIBLE", b["decisions"] and brain.decision_line(b["decisions"][0]))
        self.assertEqual(len(b["goals"]), 3)
        events = db.q("SELECT message FROM events WHERE company_id=? AND kind='brain' AND id>?", (cid, before))
        self.assertIn("owner edited goals", {e["message"] for e in events})
        self.assertIn("owner edited strategy", {e["message"] for e in events})

    def test_owner_decides_proposals_from_the_brain_tab(self):
        c = make_company("TabCo")
        self.seed_strategy(c)
        echo = ctx_for(c, "marketing")
        run(echo, "propose_brain_change", section="lessons", change='{"text": "Post at 9am."}')
        pending = db.one("SELECT * FROM brain_changes WHERE company_id=? ORDER BY id DESC", (c["id"],))
        run(echo, "propose_brain_change", section="strategy", change='{"positioning": "Cheaper than a vet"}')
        routed = db.one("SELECT * FROM brain_changes WHERE company_id=? ORDER BY id DESC", (c["id"],))
        b = call("GET", f"/api/companies/{c['id']}/brain")
        listed = {p["id"]: p for p in b["proposals"]}
        self.assertEqual((listed[routed["id"]]["label"], listed[routed["id"]]["agent_name"]),
                         ("strategy.positioning", "Echo"))
        self.assertEqual(call("POST", f"/api/brain/changes/{pending['id']}", {"decision": "accept"})["status"],
                         "accepted")
        self.assertTrue(db.one("SELECT id FROM lessons WHERE company_id=? AND text='Post at 9am.'", (c["id"],)))
        out = call("POST", f"/api/brain/changes/{routed['id']}", {"decision": "accept"})
        self.assertEqual(out["applied"], "strategy updated (positioning)")
        self.assertEqual(brain.get_strategy(c["id"])["positioning"], "Cheaper than a vet")
        decision = decisions_of(c)[0]
        self.assertEqual((decision["status"], decision["result"], decision["decided_by"]),
                         ("answered", "Accept the change", "owner"))   # answered in the inbox too
        with self.assertRaises(HTTPError) as e:
            call("POST", f"/api/brain/changes/{routed['id']}", {"decision": "reject"})
        self.assertIn("already accepted", e.exception.message)
        with self.assertRaises(HTTPError):
            call("POST", f"/api/brain/changes/{pending['id']}", {"decision": "maybe"})


# ================================================================ founding writes the brain

class TestFounding(Base):
    def founding(self, stage) -> dict:
        c = make_company("FoundCo", status="founding")
        planning.update_company_settings(c["id"], founding_stage=stage)
        db.update("companies", c["id"], stage="idea", charter="" if stage == "charter" else CHARTER, mission="")
        return fresh(c)

    def run_stage(self, c, steps) -> tuple[list, dict, Scripted]:
        s = Scripted(steps)
        a = agent_of(c)
        with mock.patch.object(agents, "start", lambda aid, kind, **kw: True), mock.patch.object(agents.llm, "chat", s):
            agents.run(a["id"], "founding")
        row = db.one("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC", (a["id"],))
        results = [(m["name"], m["content"]) for m in json.loads(row["transcript"] or "[]") if m["role"] == "tool"]
        return results, row, s

    def test_charter_stage_writes_the_strategy_and_goals(self):
        c = self.founding("charter")
        results, run_row, s = self.run_stage(c, [
            ("update_charter", {"charter": CHARTER, "one_liner": "Plans for dogs", "stage": "building"}),
            ("set_strategy", STRATEGY),
            ("set_goal", {"title": "200 waitlist signups", "metric": "waitlist_signups", "target": 200,
                          "deadline": in_days(45), "owner_role": "marketing"}),
            ("set_goal", {"title": "10 paying customers", "metric": "stripe.customers", "target": 10,
                          "deadline": in_days(60), "owner_role": "sales"}),
            ("finish", {"summary": "Charter and brain saved."})])
        self.assertTrue({"set_strategy", "set_goal", "update_charter"} <= set(s.calls[0]["tools"]))
        user = s.calls[0]["user"]
        self.assertIn("Call set_strategy with all seven fields", user)
        self.assertIn("Call set_goal one to three times", user)
        self.assertIn(f"you have {planning.STAGE_STEPS['charter']} tool calls", s.calls[0]["system"].lower())
        self.assertNotIn("—", user)
        self.assertEqual(results[-1], ("finish", "Session ended."))
        self.assertEqual(run_row["status"], "done")
        self.assertTrue(brain.has_strategy(c["id"]))
        self.assertEqual(len(brain._active_ids(c["id"])), 2)
        self.assertEqual(decisions_of(c), [])   # founding goals are no change to tell the owner about
        self.assertEqual(db.company_settings(fresh(c))["founding_stage"], "critic")
        # the plan stage lists the goals and asks for goal_id and expected on every task
        planning.update_company_settings(c["id"], founding_stage="plan")
        prompt = agents.founding_prompt(fresh(c), "")
        self.assertIn("Goals (the numbers set_goal returned in the charter stage; use them as goal_id):", prompt)
        for g in brain.active_goals(c["id"]):
            self.assertIn(brain.goal_line(g), prompt)
        self.assertIn("goal_id (the goal it serves, from the list above) and expected", prompt)

    def test_charter_stage_without_the_brain_is_held_once(self):
        c = self.founding("charter")
        results, run_row, _s = self.run_stage(c, [
            ("update_charter", {"charter": CHARTER}),
            ("finish", {"summary": "Charter saved."}),
            ("finish", {"summary": "Charter saved."})])
        self.assertTrue(results[1][1].startswith("Not finished yet: the brain has no strategy yet (missing: "
                                                 "one_liner, icp"), results[1][1])
        self.assertIn("then set_goal one to three times", results[1][1])
        self.assertEqual((results[2][1], run_row["status"]), ("Session ended.", "done"))   # then it moves on
        self.assertIn("Stage check: the brain has no strategy yet", run_row["summary"])
        self.assertEqual(db.company_settings(fresh(c))["founding_stage"], "critic")
        brain._upsert_strategy(c["id"], STRATEGY)
        self.assertIn("the brain has no goals yet", planning.stage_problem(fresh(c), "charter"))
        brain._insert("goals", c["id"], {"title": "x", "metric": "m", "target": 1, "deadline": in_days(5)})
        self.assertIsNone(planning.stage_problem(fresh(c), "charter"))

    def test_the_fake_models_founding_script_writes_the_brain(self):
        """tests/fake_ollama.py's charter and plan stages, run against the real tools (the e2e founding uses them)."""
        from tests import fake_ollama

        def fake_chat(messages, tools_spec=None, model=None, settings=None, **kw):
            r = fake_ollama.script(messages, [{"function": {"name": t["name"]}} for t in tools_spec or []])
            if isinstance(r, str):
                return llm.Reply(content=r)
            return llm.Reply(tool_calls=[llm.ToolCall(name=r["function"]["name"],
                                                      arguments=r["function"]["arguments"])], tokens=100)

        c = make_company("PawPlan", members=presets.TEMPLATES["launch"]["members"], status="founding")
        planning.update_company_settings(c["id"], founding_stage="charter")
        a = agent_of(c)
        with mock.patch.object(agents, "start", lambda aid, kind, **kw: True), \
                mock.patch.object(agents.llm, "chat", fake_chat):
            agents.run(a["id"], "founding")
            self.assertEqual(db.company_settings(fresh(c))["founding_stage"], "critic")
            self.assertEqual({k: brain.get_strategy(c["id"])[k] for k in brain.STRATEGY_FIELDS}, STRATEGY)
            goals = brain._active_ids(c["id"])
            self.assertEqual(len(goals), 2)
            planning.update_company_settings(c["id"], founding_stage="plan")
            agents.run(a["id"], "founding")
        tasks = db.q("SELECT * FROM tasks WHERE company_id=?", (c["id"],))
        self.assertEqual(len(tasks), 5)
        self.assertEqual(fresh(c)["status"], "active")
        if "goal_id" in tools.REGISTRY["create_task"].parameters["properties"]:   # once create_task takes it
            self.assertEqual({t["goal_id"] for t in tasks}, set(goals))
            self.assertTrue(all(t["expected"] for t in tasks))

    def test_plan_stage_without_goals(self):
        c = self.founding("plan")
        prompt = agents.founding_prompt(fresh(c), "", "plan")
        self.assertIn("The company has no goals yet: leave goal_id out", prompt)


if __name__ == "__main__":
    unittest.main()
