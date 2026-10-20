"""Phase 4 portfolio capital allocation (docs/phases/phase-4-scale.md section 2): the scorecard and where each column
comes from, the allocation score and share, the scheduler's share of the GPU with the starvation guard and equal
mode, and the monthly portfolio review with its decisions and answers. Only the outside world is faked: the phone,
the small model and the scheduler's thread start.

Run with:  python -m unittest tests.test_phase4_portfolio -v
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import json
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-phase4-portfolio-"))

from holdco import (actions, agents, db, llm, notify, portfolio, roles, routing, scheduler,  # noqa: E402
                    workspace)

db.init()

_created: list[int] = []   # archived after each test, so other modules' scheduler checks never see them
SAVED_SETTINGS = ("kill_switch", "scheduler_enabled", "max_concurrent_runs", "allocation_mode", "small_model")


def utc(minutes: float = 0) -> str:
    """A UTC time this many minutes from now (negative: in the past), as the database stores it."""
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_company(name: str, age_days: float = 0, status: str = "active", members=("ceo", "engineering"),
                 **settings) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    s = {"truth_scan_done": True, **settings}
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "Meal plans for dogs", status, "building", json.dumps(s), utc(-age_days * 1440)))
    _created.append(cid)
    workspace.create(db.get_company(cid))
    for m in members:
        roles.add_agent(cid, preset=m)
    return db.get_company(cid)


def metric(c: dict, name: str, value: float, source: str = "manual", minutes_ago: float = 0) -> None:
    db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, source, created_at) VALUES(?,?,?,?,?,?,?)",
           (c["id"], None, name, value, "", source, utc(-minutes_ago)))


def goal(c: dict, metric_name: str, target: float) -> None:
    db.run("INSERT INTO goals(company_id, title, metric, target, deadline, owner_role, status, created_at) "
           "VALUES(?,?,?,?,?,?,?,?)", (c["id"], f"{target:g} {metric_name}", metric_name, target, "", "ceo", "active",
                                       db.now()))


def run_row(c: dict, started_ago: float, finished_ago: float | None = None) -> None:
    """A run that started this many minutes ago and finished then (None: still running)."""
    db.run("INSERT INTO runs(company_id, agent_id, task_id, kind, status, started_at, finished_at) "
           "VALUES(?,?,?,?,?,?,?)", (c["id"], None, None, "task", "running" if finished_ago is None else "done",
                                     utc(-started_ago), None if finished_ago is None else utc(-finished_ago)))


def spend(c: dict, amount, status: str = "done", decided_ago: float = 60) -> None:
    db.run("INSERT INTO approvals(company_id, agent_id, task_id, action, summary, payload, status, created_at, "
           "decided_at) VALUES(?,?,?,?,?,?,?,?,?)",
           (c["id"], None, None, "spend_money", "Buy a domain", json.dumps({"item": "domain", "amount_usd": amount}),
            status, utc(-decided_ago - 5), utc(-decided_ago) if status != "pending" else None))


def email(c: dict, direction: str, message_id: str = "", in_reply_to: str = "", minutes_ago: float = 0) -> None:
    db.run("INSERT INTO emails(company_id, direction, uid, message_id, from_addr, to_addr, subject, body, status, "
           "created_at, in_reply_to) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
           (c["id"], direction, "", message_id, "a@example.com", "b@example.com", "Hello", "Hi", "new",
            utc(-minutes_ago), in_reply_to))


def row_of(c: dict) -> dict:
    rows = [r for r in portfolio.scorecard() if r["id"] == c["id"]]
    return rows[0] if rows else {}


def call(method: str, path: str, body=None):
    """Dispatch to a dashboard route the way server.Handler does."""
    class Handler:
        query: dict = {}

    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(path)):
            return fn(Handler(), m, body)
    raise AssertionError(f"no route for {method} {path}")


def quiet():
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
    return stack


class Base(unittest.TestCase):
    """Every other active company is paused for the test (the shares and the review are portfolio-wide) and the
    reviews other modules made are moved out of the way; all of it is put back afterwards."""

    def setUp(self):
        self._saved = {k: db.get_settings().get(k) for k in SAVED_SETTINGS}
        db.set_settings({"kill_switch": False, "allocation_mode": "weighted", "small_model": ""})
        self.others = [r["id"] for r in db.q("SELECT id FROM companies WHERE status='active'")]
        if self.others:
            db.run(f"UPDATE companies SET status='paused' WHERE id IN ({','.join('?' * len(self.others))})",
                   self.others)
        self._reviews = db.q("SELECT id, created_at FROM briefs WHERE company_id IS NULL AND kind='portfolio'")
        db.run("UPDATE briefs SET created_at='2000-01-01T00:00:00Z' WHERE company_id IS NULL AND kind='portfolio'")
        self._last_brief = db.scalar("SELECT COALESCE(MAX(id), 0) FROM briefs")
        self.phone: list = []      # (text, kind) sent with send_later
        self.announced: list = []  # approvals announced to the phone
        self._patches = [mock.patch.object(notify, "send_later", lambda text, buttons=None, kind="info", **kw:
                                           self.phone.append((text, kind))),
                         mock.patch.object(notify, "approval_requested", lambda row: self.announced.append(row)),
                         mock.patch.object(notify, "send", lambda *a, **kw: True)]
        for p in self._patches:
            p.start()
        portfolio.invalidate()
        portfolio._review_failed["at"] = None

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        db.set_settings({k: v for k, v in self._saved.items() if v is not None})
        while _created:
            db.update("companies", _created.pop(), status="archived")
        if self.others:
            db.run(f"UPDATE companies SET status='active' WHERE id IN ({','.join('?' * len(self.others))})",
                   self.others)
        db.run("DELETE FROM briefs WHERE company_id IS NULL AND kind='portfolio' AND id>?", (self._last_brief,))
        for b in self._reviews:
            db.run("UPDATE briefs SET created_at=? WHERE id=?", (b["created_at"], b["id"]))
        portfolio.invalidate()


class TestPortfolio(Base):
    COLUMNS = {"id", "name", "status", "goals", "boost_until", "goal_progress", "revenue_30d", "mrr", "gpu_hours_week",
               "money_spent_30d", "days_since_result", "age_days", "score", "share"}

    def test_scorecard_columns(self):
        c = make_company("ScoreCo", age_days=20)
        other = make_company("OtherCo", age_days=20)
        # goal_progress: the mean over active goals of min(1, current / target), current from the latest metric
        goal(c, "stripe.customers", 10)
        goal(c, "waitlist", 50)
        metric(c, "stripe.customers", 4, "stripe", minutes_ago=300)
        metric(c, "waitlist", 80, minutes_ago=300)                      # over target counts as 1
        # revenue_30d and mrr: the latest Stripe rows; a row an agent or the owner typed never counts
        metric(c, "stripe.revenue_usd_30d", 50, "stripe", minutes_ago=3 * 1440)
        metric(c, "stripe.revenue_usd_30d", 120, "stripe", minutes_ago=2 * 1440)
        metric(c, "stripe.revenue_usd_30d", 999, "manual", minutes_ago=60)
        metric(c, "stripe.mrr_usd", 30, "stripe", minutes_ago=2 * 1440)
        # gpu_hours_week: run durations started in the last 7 days, a running run up to now
        run_row(c, 180, 60)               # 2 h
        run_row(c, 30)                    # 0.5 h so far
        run_row(c, 8 * 1440, 8 * 1440 - 60)   # last week: not counted
        run_row(other, 120, 0)            # another company's
        # money_spent_30d: approved purchases (spend_money, done) decided in 30 days
        spend(c, 25, decided_ago=2 * 1440)
        spend(c, "15.5", decided_ago=10 * 1440)
        spend(c, 100, status="pending")
        spend(c, 70, status="failed")
        spend(c, 40, decided_ago=40 * 1440)
        # days_since_result: a connector metric that moved 5 days ago; the same value again is not a result ...
        metric(c, "analytics.visitors", 10, "plausible", minutes_ago=5 * 1440)
        metric(c, "analytics.visitors", 10, "plausible", minutes_ago=3 * 1440)
        metric(c, "signups", 3, minutes_ago=1440)                        # ... nor a manual metric that changed,
        metric(c, "signups", 9, minutes_ago=600)
        metric(c, "stripe.payments_30d", 4, "stripe", minutes_ago=6 * 1440)
        metric(c, "stripe.payments_30d", 2, "stripe", minutes_ago=2 * 1440)   # a payment leaving the 30-day window,
        email(c, "out", message_id="<a1@pawplan.co>", minutes_ago=3 * 1440)
        email(c, "in", in_reply_to="<zz@elsewhere.com>", minutes_ago=1440)   # a reply to someone else's message,
        email(other, "out", message_id="<b1@other.co>", minutes_ago=3 * 1440)
        email(c, "in", in_reply_to="<b1@other.co>", minutes_ago=1440)       # or to another company's message
        email(c, "in", minutes_ago=600)                                      # or an email that answers nothing
        r = row_of(c)
        self.assertEqual(set(r), self.COLUMNS)
        self.assertEqual((r["name"], r["status"], r["goals"], r["boost_until"]), ("ScoreCo", "active", 2, ""))
        self.assertAlmostEqual(r["goal_progress"], (0.4 + 1.0) / 2)
        self.assertEqual((r["revenue_30d"], r["mrr"]), (120.0, 30.0))
        self.assertAlmostEqual(r["gpu_hours_week"], 2.5, delta=0.02)
        self.assertEqual(r["money_spent_30d"], 40.5)
        self.assertAlmostEqual(r["days_since_result"], 5.0, delta=0.05)
        self.assertAlmostEqual(r["age_days"], 20.0, delta=0.05)
        # An inbound email that answers one the company sent is a result (In-Reply-To or the last References id) ...
        email(c, "in", in_reply_to="<x0@pawplan.co> <a1@pawplan.co>", minutes_ago=2 * 1440)
        self.assertAlmostEqual(row_of(c)["days_since_result"], 2.0, delta=0.05)
        # ... and so is a Stripe payment: stripe.payments_30d going up
        metric(c, "stripe.payments_30d", 3, "stripe", minutes_ago=6 * 60)
        self.assertAlmostEqual(row_of(c)["days_since_result"], 0.25, delta=0.06)
        # A company without Stripe, goals or results shows None where there is no number, 0 where there is
        blank = row_of(other)
        self.assertEqual((blank["goals"], blank["goal_progress"], blank["revenue_30d"], blank["mrr"],
                          blank["money_spent_30d"], blank["days_since_result"]), (0, 0.0, None, None, 0.0, None))
        self.assertAlmostEqual(blank["gpu_hours_week"], 2.0, delta=0.02)
        # Only active and paused companies: a Bench company and an archived one are never on the scorecard
        bench_co = make_company("BenchCo", age_days=90, status="bench")
        gone = make_company("GoneCo", age_days=90, status="archived")
        paused = make_company("NapCo", age_days=90, status="paused")
        ids = {r["id"] for r in portfolio.scorecard()}
        self.assertTrue({c["id"], other["id"], paused["id"]} <= ids)
        self.assertFalse({bench_co["id"], gone["id"]} & ids)
        self.assertEqual(row_of(paused)["share"], 0.0)

    def test_score_and_share(self):
        w = portfolio.WEIGHTS
        self.assertEqual(w, {"progress": 0.4, "traction": 0.4, "freshness": 0.2})
        a = make_company("Alpha", age_days=40)       # half its goal, the most revenue, a result 7 days ago
        goal(a, "waitlist", 10)
        metric(a, "waitlist", 5, minutes_ago=60)
        metric(a, "stripe.revenue_usd_30d", 100, "stripe", minutes_ago=60)
        metric(a, "analytics.visitors", 7, "plausible", minutes_ago=7 * 1440)
        b = make_company("Beta", age_days=40, allocation_boost_until=utc(-60))   # some revenue; its boost ended
        metric(b, "stripe.revenue_usd_30d", 10, "stripe", minutes_ago=60)
        young = make_company("Gamma", age_days=3)     # nothing yet, but in its seed period
        boosted = make_company("Delta", age_days=40, allocation_boost_until=utc(10 * 1440))   # goal met, Scale chosen
        goal(boosted, "waitlist", 10)
        metric(boosted, "waitlist", 12, minutes_ago=60)
        idle = make_company("Epsilon", age_days=40)   # nothing at all: 0
        paused = make_company("Zeta", age_days=40, status="paused")   # more revenue than anyone, but paused
        metric(paused, "stripe.revenue_usd_30d", 1000, "stripe", minutes_ago=60)
        rows = {r["id"]: r for r in portfolio.scorecard()}
        traction_b = math.log1p(10) / math.log1p(100)   # scaled by the top active company, not the paused one
        expected = {a["id"]: 0.4 * 0.5 + 0.4 * 1 + 0.2 * (1 - 7 / 14),
                    b["id"]: 0.4 * traction_b,
                    young["id"]: 0.5,                     # max(0, 0.5): the seed floor
                    boosted["id"]: (0.4 * 1) * 1.5,       # the boost: score * 1.5 while it lasts
                    idle["id"]: 0.0,
                    paused["id"]: 0.4 * 1}                # its traction is capped at 1
        for cid, score in expected.items():
            self.assertAlmostEqual(rows[cid]["score"], score, delta=0.002, msg=rows[cid]["name"])
        self.assertTrue(rows[boosted["id"]]["boost_until"])
        self.assertEqual(rows[b["id"]]["boost_until"], "")
        active = [a["id"], b["id"], young["id"], boosted["id"], idle["id"]]
        total = sum(expected[cid] for cid in active)
        for cid in active:
            self.assertAlmostEqual(rows[cid]["share"], expected[cid] / total, delta=0.002)
        self.assertAlmostEqual(sum(rows[cid]["share"] for cid in active), 1.0, places=4)
        self.assertEqual(rows[paused["id"]]["share"], 0.0)
        self.assertEqual(rows[idle["id"]]["share"], 0.0)
        # Highest score first
        listed = [r["id"] for r in portfolio.scorecard() if r["id"] in expected]
        self.assertEqual(listed, sorted(expected, key=lambda cid: (-rows[cid]["score"], cid)))
        # A company younger than 14 days keeps its own score when it is above the floor
        goal(young, "waitlist", 10)
        metric(young, "waitlist", 10, minutes_ago=1)
        metric(young, "stripe.revenue_usd_30d", 100, "stripe", minutes_ago=1)
        self.assertAlmostEqual(row_of(young)["score"], 0.8, delta=0.002)
        # Nobody with revenue: traction 0 everywhere; nobody above 0: an equal share each
        rows = [{"status": "active", "goal_progress": 0.0, "revenue_30d": None, "days_since_result": None,
                 "age_days": 30.0, "boost_until": ""} for _ in range(4)]
        portfolio._score(rows)
        self.assertEqual([(r["score"], r["share"]) for r in rows], [(0.0, 0.25)] * 4)

    # ------------------------------------------------------------ the scheduler

    def scheduler(self):
        """A scheduler whose runs start and finish at once: each start writes a runs row and frees the slot again
        before the next tick, so every tick hands out exactly one slot."""
        db.set_settings({"scheduler_enabled": True, "max_concurrent_runs": 1})
        sched = scheduler.Scheduler()
        for name in ("_housekeeping", "_resume_founding", "_check_inbox", "_routines", "_maybe_cycle"):
            setattr(sched, name, lambda *a, **kw: None)
        sched._model_server_down = lambda: False
        self.running = 0
        self.started: list[int] = []   # company id per start

        def start(aid, kind, task_id=None, **kw):
            agent = db.get_agent(aid)
            db.run("INSERT INTO runs(company_id, agent_id, task_id, kind, status, started_at, finished_at) "
                   "VALUES(?,?,?,?,?,?,?)", (agent["company_id"], aid, task_id, kind, "done", db.now(), db.now()))
            if task_id:
                db.update("tasks", task_id, status="done")
            self.started.append(agent["company_id"])
            self.running += 1
            return True
        for p in (mock.patch.object(agents, "start", start),
                  mock.patch.object(agents, "running_count", lambda *a, **kw: self.running)):
            p.start()
            self.addCleanup(p.stop)
        return sched

    def tick(self, sched, n: int = 1) -> list[int]:
        before = len(self.started)
        for _ in range(n):
            self.running = 0
            with quiet():
                sched.tick()
        return self.started[before:]

    def tasks(self, c: dict, n: int, priority: int = 2, **cols) -> list[int]:
        agent = db.one("SELECT id FROM agents WHERE company_id=? AND role='engineering'", (c["id"],))
        ids = []
        for i in range(n):
            base = {"company_id": c["id"], "title": f"Build part {i + 1}", "role": "engineering",
                    "agent_id": agent["id"], "status": "todo", "priority": priority, "created_by": "Atlas",
                    "created_at": db.now(), "updated_at": db.now(), **cols}
            ids.append(db.run(f"INSERT INTO tasks({', '.join(base)}) VALUES({','.join('?' * len(base))})",
                              tuple(base.values())))
        return ids

    def test_scheduler_share(self):
        sched = self.scheduler()
        # Share 0.75 and 0.25: Big has revenue and half its goal (0.6), Small half its goal (0.2).
        big = make_company("Big", age_days=40)
        small = make_company("Small", age_days=40)
        for c in (big, small):
            goal(c, "waitlist", 10)
            metric(c, "waitlist", 5, minutes_ago=60)
        metric(big, "stripe.revenue_usd_30d", 100, "stripe", minutes_ago=60)
        self.tasks(big, 12)     # Big's tasks come first in Phase 3 order (same class and priority, lower ids)
        self.tasks(small, 12)
        shares = {r["id"]: r["share"] for r in portfolio.scorecard()}
        self.assertEqual((shares[big["id"]], shares[small["id"]]), (0.75, 0.25))
        got = self.tick(sched, 10)
        # Neither had a run within its cycle_minutes, so each gets one first (the starvation guard); after that
        # the slot goes to whoever is furthest below its share of the runs started in the last hour.
        self.assertEqual(got[:2], [big["id"], small["id"]])
        self.assertEqual((got.count(big["id"]), got.count(small["id"])), (7, 3))
        for c in (big, small):
            db.update("companies", c["id"], status="archived")

        # A company whose score is 0 still gets one run per cycle_minutes.
        rich = make_company("Rich", age_days=40)
        goal(rich, "waitlist", 10)
        metric(rich, "waitlist", 10, minutes_ago=60)
        starved = make_company("Starved", age_days=40, cycle_minutes=30)
        self.tasks(rich, 20)
        self.tasks(starved, 6)
        portfolio.invalidate()
        shares = {r["id"]: r["share"] for r in portfolio.scorecard()}
        self.assertEqual((shares[rich["id"]], shares[starved["id"]]), (1.0, 0.0))
        got = self.tick(sched, 10)
        self.assertEqual(got.count(starved["id"]), 1, got)
        self.assertEqual(got[:2], [rich["id"], starved["id"]])
        # 31 minutes later (its cycle is 30) it is starving again and takes the next free slot, once.
        db.run("UPDATE runs SET started_at=?, finished_at=? WHERE company_id=?", (utc(-31), utc(-31), starved["id"]))
        self.assertEqual(self.tick(sched, 3), [starved["id"], rich["id"], rich["id"]])

        # Classes 1 and 2 keep their places in front: a task due within a day of the starved company stays first.
        due = self.tasks(starved, 1, due_at=utc(120))[0]
        queue = scheduler._queue()
        ordered = portfolio.order(queue, db.get_settings())
        self.assertEqual(sorted(t["id"] for t in ordered), sorted(t["id"] for t in queue))
        self.assertEqual(ordered[0]["id"], due)
        self.assertEqual(ordered[0]["dispatch_class"], 2)
        db.update("tasks", due, status="cancelled")

        # allocation_mode equal: the Phase 3 order, untouched (Rich's lower ids take every slot, no guard).
        db.set_settings({"allocation_mode": "equal"})
        queue = scheduler._queue()
        self.assertIs(portfolio.order(queue, db.get_settings()), queue)
        db.run("UPDATE runs SET started_at=?, finished_at=? WHERE company_id=?", (utc(-31), utc(-31), starved["id"]))
        self.assertEqual(self.tick(sched, 4), [rich["id"]] * 4)
        db.set_settings({"allocation_mode": "weighted"})
        db.run("UPDATE runs SET started_at=?, finished_at=? WHERE company_id=?", (utc(-31), utc(-31), starved["id"]))
        self.assertEqual(self.tick(sched, 1), [starved["id"]])
        # One query per tick: nothing but the runs are read once the shares are known.
        queue, settings = scheduler._queue(), db.get_settings()
        queries: list[str] = []
        real_q = db.q
        with mock.patch.object(db, "q", lambda sql, params=(): queries.append(sql) or real_q(sql, params)):
            portfolio.order(queue, settings)
        self.assertEqual(len(queries), 1, queries)
        self.assertIn("FROM runs", queries[0])

    # ------------------------------------------------------------ the monthly review

    def decisions_of(self, c: dict) -> list[dict]:
        rows = db.q("SELECT * FROM approvals WHERE company_id=? AND action='decision' ORDER BY id", (c["id"],))
        for r in rows:
            r["p"] = json.loads(r["payload"])
        return rows

    def test_monthly_review(self):
        young = make_company("Young", age_days=5)
        self.assertFalse(portfolio.review_due(), "the oldest company is younger than 30 days")
        self.assertFalse(portfolio.monthly_review_if_due())
        scaler = make_company("Scaler", age_days=40)       # goal met, the top revenue, a payment an hour ago
        goal(scaler, "waitlist", 10)
        metric(scaler, "waitlist", 10, minutes_ago=60)
        metric(scaler, "stripe.revenue_usd_30d", 100, "stripe", minutes_ago=60)
        metric(scaler, "stripe.payments_30d", 1, "stripe", minutes_ago=120)
        metric(scaler, "stripe.payments_30d", 2, "stripe", minutes_ago=60)
        holder = make_company("Holder", age_days=40)       # half its goal, no result: neither rule
        goal(holder, "waitlist", 10)
        metric(holder, "waitlist", 5, minutes_ago=60)
        dead = make_company("Dead", age_days=75)           # 75 days, no revenue, no result ever
        earner = make_company("Earner", age_days=90)       # old and quiet, but it has revenue: hold
        metric(earner, "stripe.revenue_usd_30d", 10, "stripe", minutes_ago=60)
        stale = make_company("Stale", age_days=90)         # its last result was 50 days ago: shut down
        metric(stale, "analytics.visitors", 3, "plausible", minutes_ago=50 * 1440)
        napping = make_company("Napping", age_days=90, status="paused")
        bench_co = make_company("BenchCo", age_days=200, status="bench")
        # The team already has three questions waiting: the review's decisions are not refused by that cap.
        for i in range(3):
            db.run("INSERT INTO approvals(company_id, agent_id, task_id, action, summary, payload, status, created_at)"
                   " VALUES(?,?,?,?,?,?,?,?)", (holder["id"], None, None, "question", f"Question {i}",
                                                json.dumps({"question": f"Which price {i}?"}), "pending", db.now()))
        self.assertTrue(portfolio.review_due())
        with mock.patch.object(llm, "run_job", side_effect=AssertionError("no small model is set")):
            self.assertTrue(portfolio.monthly_review_if_due())
            self.assertFalse(portfolio.monthly_review_if_due(), "once: a review exists from the last 28 days")
        reviews = db.q("SELECT * FROM briefs WHERE company_id IS NULL AND kind='portfolio' AND id>?",
                       (self._last_brief,))
        self.assertEqual(len(reviews), 1)
        review = reviews[0]
        data = json.loads(review["data"])
        self.assertIn("| Company | Goal progress | Revenue 30 d | MRR |", review["body"])
        self.assertIn("## Recommendations", review["body"])
        self.assertNotIn("BenchCo", review["body"])
        self.assertIn("| Napping |", review["body"])          # paused: in the table, but nothing to decide
        expected = {scaler["id"]: "scale", holder["id"]: "hold", dead["id"]: "shut_down", earner["id"]: "hold",
                    stale["id"]: "shut_down", young["id"]: "hold"}
        self.assertEqual({e["company_id"]: e["recommendation"] for e in data["recommendations"]}, expected)
        # The phone gets the review once (each decision is announced on its own, with its buttons)
        briefs = [t for t, kind in self.phone if kind == "brief"]
        self.assertEqual(len(briefs), 1)
        self.assertIn("Holdco monthly portfolio review", briefs[0])
        self.assertIn("Scaler", briefs[0])
        # One decision per active company, recommended by the rule; Shut down is irreversible; 7-day deadline
        options = ["Scale (more GPU share for 30 days)", "Hold", "Shut down (pause the company)"]
        label = {"scale": options[0], "hold": options[1], "shut_down": options[2]}
        for c in (scaler, holder, dead, earner, stale, young):
            rows = self.decisions_of(c)
            self.assertEqual(len(rows), 1, c["name"])
            p = rows[0]["p"]
            self.assertEqual(p["question"], f"{c['name']}: scale, hold or shut down?")
            self.assertEqual(p["options"], options)
            self.assertEqual(p["recommended"], label[expected[c["id"]]])
            self.assertEqual(p["reversible"], expected[c["id"]] != "shut_down")
            self.assertEqual(p["origin"], {"kind": "portfolio", "brief_id": review["id"],
                                           "recommendation": expected[c["id"]]})
            deadline = db.parse_time(p["deadline_at"]) - _dt.datetime.now(_dt.timezone.utc)
            self.assertAlmostEqual(deadline.total_seconds() / 86400, 7, delta=0.01)
            self.assertIn("Recommended", p["context"])
        self.assertEqual(self.decisions_of(napping), [])
        self.assertEqual(self.decisions_of(bench_co), [])
        self.assertEqual(len(self.announced), 6)
        self.assertEqual({e["decision_id"] for e in data["recommendations"]},
                         {self.decisions_of(c)[0]["id"] for c in (scaler, holder, dead, earner, stale, young)})
        # Answering Scale sets the boost for 30 days and the share follows it
        before = row_of(scaler)["score"]
        actions.decide(self.decisions_of(scaler)[0]["id"], "approve", answer=options[0])
        until = db.parse_time(db.company_settings(db.get_company(scaler["id"]))["allocation_boost_until"])
        self.assertAlmostEqual((until - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 86400, 30, delta=0.01)
        self.assertAlmostEqual(row_of(scaler)["score"], before * 1.5, delta=0.002)
        self.assertTrue(row_of(scaler)["boost_until"])
        # ... and the boost does not recommend itself next month
        self.assertEqual(portfolio.recommendation({**row_of(scaler), "days_since_result": 3})[0], "scale")
        self.assertEqual(portfolio.recommendation({**row_of(scaler), "score": 0.5 * 1.5, "days_since_result": 3})[0],
                         "hold")
        # Hold changes nothing; Shut down pauses the company with a note and keeps it
        actions.decide(self.decisions_of(holder)[0]["id"], "approve", answer="Hold")
        self.assertEqual(db.get_company(holder["id"])["status"], "active")
        self.assertEqual(db.company_settings(db.get_company(holder["id"]))["allocation_boost_until"], "")
        actions.decide(self.decisions_of(dead)[0]["id"], "approve", answer=options[2])
        self.assertEqual(db.get_company(dead["id"])["status"], "paused")
        note = db.one("SELECT message FROM events WHERE company_id=? AND kind='info' ORDER BY id DESC LIMIT 1",
                      (dead["id"],))
        self.assertIn("portfolio review (Shut down)", note["message"])
        self.assertIn("Nothing was deleted", note["message"])
        # GET /api/portfolio: the scorecard, the daily portfolio brief and the review with its decisions
        out = call("GET", "/api/portfolio")
        self.assertTrue({"scorecard", "latest_brief", "latest_review"} <= set(out))
        listed = {r["id"] for r in out["scorecard"]}
        self.assertTrue({scaler["id"], holder["id"], dead["id"], napping["id"]} <= listed)
        self.assertNotIn(bench_co["id"], listed)
        latest = out["latest_review"]
        self.assertEqual(latest["id"], review["id"])
        by_company = {d["company_id"]: d for d in latest["decisions"]}
        self.assertEqual((by_company[scaler["id"]]["status"], by_company[scaler["id"]]["answer"]),
                         ("answered", options[0]))
        self.assertEqual((by_company[stale["id"]]["status"], by_company[stale["id"]]["reversible"]), ("pending", False))
        self.assertEqual(by_company[young["id"]]["company"], "Young")
        # With a small model the review opens with its paragraph; a decision still waiting is not asked twice
        with mock.patch.object(llm, "run_job", lambda *a, **kw: "Scaler earns more GPU time; Stale and Dead do not."):
            second = portfolio.monthly_review(narrate=True)
        body = db.one("SELECT body FROM briefs WHERE id=?", (second,))["body"]
        self.assertTrue(body.startswith("Scaler earns more GPU time"), body[:200])
        self.assertEqual(len(self.decisions_of(stale)), 1)
        with mock.patch.object(llm, "run_job", lambda *a, **kw: "Scaler made 4,321 dollars."), quiet():
            third = portfolio.monthly_review(narrate=True)   # a number the data does not hold: no paragraph
        self.assertTrue(db.one("SELECT body FROM briefs WHERE id=?", (third,))["body"].startswith("| Company |"))


if __name__ == "__main__":
    unittest.main()
