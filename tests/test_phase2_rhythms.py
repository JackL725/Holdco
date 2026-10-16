"""Phase 2 unit tests for rhythms, briefs and the decisions queue (holdco/rhythms.py, holdco/decisions.py, the
decision paths of actions.py and notify.py, the routine rules of scheduler._routines).

Run with:  python -m unittest tests.test_phase2_rhythms -v
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOLDCO_DATA"] = tempfile.mkdtemp(prefix="holdco-phase2-rhythms-")

from holdco import (actions, brain, contracts, db, decisions, notify, planning, rhythms, roles,  # noqa: E402
                    routing, scheduler, tools, truth, workspace)
from tests.fake_telegram import FakeTelegram  # noqa: E402

db.init()

CREATED: list[int] = []   # every company these tests made; archived at the end so other modules never see them


def make_company(name="RhythmCo", members=("ceo", "engineering", "marketing")) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "An idea", "active", "building", "{}", "2020-01-01T00:00:00Z"))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    CREATED.append(cid)
    return company


def agent_of(company: dict, role: str) -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def ctx_for(company: dict, role: str, task: dict | None = None, kind: str = "task") -> tools.Ctx:
    agent = agent_of(company, role)
    c = tools.Ctx(company=company, agent=agent, kind=kind, run_id=0, task=task, settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(agent, kind)} | {"write_brief", "ask_decision"}
    return c


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**delta) -> str:
    return iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta))


def add_task(company: dict, title: str, role: str = "engineering", status: str = "todo", created_by: str = "Atlas",
             routine: str = "", goal_id=None, updated_at: str | None = None) -> dict:
    agent = agent_of(company, role)
    ts = updated_at or db.now()
    tid = db.run("INSERT INTO tasks(company_id, title, role, agent_id, status, priority, created_by, routine, goal_id,"
                 " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (company["id"], title, role, agent["id"] if agent else None, status, 2, created_by, routine, goal_id,
                  ts, ts))
    return db.get_task(tid)


def add_metric(company: dict, name: str, value: float, source: str, at: str, note: str = "") -> None:
    db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, created_at, source) VALUES(?,?,?,?,?,?,?)",
           (company["id"], None, name, value, note, at, source))


def add_goal(company: dict, title: str, metric: str, target: float, deadline: str = "2030-12-01") -> int:
    return db.run("INSERT INTO goals(company_id, title, metric, target, current, deadline, owner_role, status, "
                  "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (company["id"], title, metric, target, None, deadline, "sales", "active", db.now()))


def call_route(method: str, path: str, body=None):
    """Dispatch to a registered route the way server.Handler does (path with an optional query string)."""
    parsed = urllib.parse.urlparse(path)

    class H:
        query = urllib.parse.parse_qs(parsed.query)

    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(parsed.path)):
            return fn(H, m, body)
    raise AssertionError(f"no route for {method} {path}")


@contextlib.contextmanager
def sends():
    """Record every notify.send call (send_later goes through it on the sender thread)."""
    calls: list = []

    def fake(text, buttons=None, kind="info", *, dedupe=True):
        calls.append({"text": text, "buttons": buttons, "kind": kind})
        return True

    with mock.patch.object(notify, "send", fake):
        yield calls
        notify.flush()


def routine(title: str, every_hours: float = 24, at_hour=None, requires: str = "", first_after_hours: float = 0):
    r = {"title": title, "description": f"{title} (test)", "every_hours": every_hours,
         "first_after_hours": first_after_hours, "priority": 2, "deliverable": "", "requires": requires}
    if at_hour is not None:
        r["at_hour"] = at_hour
    return r


def only_for(preset: str, *routines):
    """A roles.role_info stand-in: the given routines for one preset, none for anyone else."""
    real = roles.role_info

    def info(agent):
        p = dict(real(agent))
        p["routines"] = list(routines) if roles.preset_key(agent) == preset else []
        return p
    return mock.patch.object(roles, "role_info", info)


def tearDownModule():
    for cid in CREATED:
        db.update("companies", cid, status="archived")
    notify.flush()
    notify._recent.clear()


class Base(unittest.TestCase):
    def setUp(self):
        notify.flush()
        notify._recent.clear()
        db.set_settings({"kill_switch": False})


# =============================================================== routines

class TestRoutines(Base):
    """The local clock is patched; task times stay on the database clock, so each step stamps the task with how
    long ago it was made as seen from that step's local time."""

    def at(self, day: int, hour: int, minute: int = 0) -> dt.datetime:
        return dt.datetime(2030, 1, day, hour, minute)

    def run_routines(self, company: dict, now: dt.datetime) -> list[dict]:
        with mock.patch.object(scheduler, "_local_now", lambda: now):
            scheduler.Scheduler()._routines(db.get_company(company["id"]))
        return db.q("SELECT * FROM tasks WHERE company_id=? AND routine!='' ORDER BY id", (company["id"],))

    def made_before(self, task: dict, made: dt.datetime, now: dt.datetime) -> None:
        """Mark a routine task done, created at local time `made` as seen from local time `now`."""
        stamp = ago(seconds=(now - made).total_seconds())
        db.update("tasks", task["id"], status="done", created_at=stamp, updated_at=stamp)

    def test_at_hour_waits_for_the_hour_and_runs_once_a_day(self):
        c = make_company("AtHourCo", ("ceo",))
        with only_for("ceo", routine("Evening brief", 24, at_hour=18)):
            self.assertEqual(self.run_routines(c, self.at(10, 17, 30)), [], "before 18:00 nothing is created")
            made = self.run_routines(c, self.at(10, 18, 5))
            self.assertEqual([t["routine"] for t in made], ["ceo:Evening brief"])
            self.assertEqual(made[0]["created_by"], "routine")
            first = made[0]
            self.made_before(first, self.at(10, 18, 5), self.at(10, 23, 0))
            self.assertEqual(len(self.run_routines(c, self.at(10, 23, 0))), 1, "one per day")
            self.made_before(first, self.at(10, 18, 5), self.at(11, 17, 59))
            self.assertEqual(len(self.run_routines(c, self.at(11, 17, 59))), 1, "the next day waits for 18:00")
            self.made_before(first, self.at(10, 18, 5), self.at(11, 18, 0))
            self.assertEqual(len(self.run_routines(c, self.at(11, 18, 0))), 2)
            # An open one is never duplicated.
            self.assertEqual(len(self.run_routines(c, self.at(12, 19, 0))), 2)

    def test_weekly_at_hour_comes_every_seven_days(self):
        c = make_company("WeeklyHourCo", ("ceo",))
        with only_for("ceo", routine("Weekly review", 168, at_hour=9)):
            made = self.run_routines(c, self.at(7, 9, 10))
            self.assertEqual(len(made), 1)
            for now, expected, why in ((self.at(13, 9, 30), 1, "six days later: not yet"),
                                       (self.at(14, 8, 59), 1, "a week later, before 9"),
                                       (self.at(14, 9, 0), 2, "a week later at 9")):
                self.made_before(made[0], self.at(7, 9, 10), now)
                self.assertEqual(len(self.run_routines(c, now)), expected, why)

    def test_a_fixed_test_clock_does_not_repeat_the_routine(self):
        c = make_company("FixedClockCo", ("ceo",))
        with only_for("ceo", routine("Evening brief", 24, at_hour=18)), \
                mock.patch.dict(os.environ, {"HOLDCO_TEST_LOCAL_NOW": "19:00"}):
            scheduler.Scheduler()._routines(db.get_company(c["id"]))
            t = db.one("SELECT * FROM tasks WHERE company_id=? AND routine='ceo:Evening brief'", (c["id"],))
            db.update("tasks", t["id"], status="done")
            scheduler.Scheduler()._routines(db.get_company(c["id"]))
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND routine!=''", (c["id"],)), 1)

    def test_no_analyst(self):
        c = make_company("NoAnalystCo", ("ceo",))
        r = routine("Evening brief", 24, requires="no_analyst")
        with only_for("ceo", r):
            made = self.run_routines(c, self.at(10, 12))
            self.assertEqual([t["routine"] for t in made], ["ceo:Evening brief"], "no analyst: the CEO writes it")
            self.made_before(made[0], self.at(9, 11), self.at(10, 12))
            aid = roles.add_agent(c["id"], preset="analyst")
            self.assertEqual(len(self.run_routines(c, self.at(10, 12))), 1, "an enabled analyst takes over")
            db.update("agents", aid, enabled=0)
            self.assertEqual(len(self.run_routines(c, self.at(10, 12))), 2, "a disabled analyst does not count")

    def test_first_after_hours_still_applies(self):
        c = make_company("YoungCo", ("ceo",))
        db.update("companies", c["id"], created_at=db.now())
        with only_for("ceo", routine("Evening brief", 24, at_hour=0, first_after_hours=20)):
            self.assertEqual(self.run_routines(c, self.at(10, 12)), [])


# =============================================================== data

class TestData(Base):
    def setUp(self):
        super().setUp()
        self.c = make_company("DataCo", ("ceo", "analyst", "engineering"))
        c = self.c
        for value, at in ((10, ago(days=8)), (11, ago(days=6)), (12, ago(days=4)), (12, ago(days=2)),
                          (13, ago(hours=30)), (30, ago(minutes=5))):
            add_metric(c, "stripe.revenue_usd_30d", value, "stripe", at)
        add_metric(c, "waitlist_signups", 50, "manual", ago(days=3), "typed in")
        add_metric(c, "waitlist_signups", 58, "manual", ago(hours=2), "from Tally")
        self.g_rev = add_goal(c, "30 dollars of revenue", "stripe.revenue_usd_30d", 100)
        self.g_wait = add_goal(c, "200 waitlist signups", "waitlist_signups", 200)

    def test_daily_data_from_connector_metrics(self):
        d = rhythms.daily_data(self.c)
        self.assertEqual(list(d), ["metrics", "anomalies", "shipped", "stuck", "decisions_needed", "goals",
                                   "approvals_pending"])
        rev = next(m for m in d["metrics"] if m["name"] == "stripe.revenue_usd_30d")
        self.assertEqual((rev["value"], rev["value_24h_ago"], rev["delta_24h"], rev["value_7d_ago"], rev["delta_7d"],
                          rev["source"]), (30, 13, 17, 10, 20, "stripe"))
        manual = next(m for m in d["metrics"] if m["name"] == "waitlist_signups")
        self.assertEqual(manual["source"], "manual")
        self.assertEqual([a["name"] for a in d["anomalies"]], ["stripe.revenue_usd_30d"])
        self.assertEqual(d["anomalies"][0]["change_pct"], 150)   # 30 against a mean of 12
        self.assertEqual({g["id"] for g in d["goals"]}, {self.g_rev, self.g_wait})

        w = rhythms.weekly_data(self.c)
        self.assertEqual([m["name"] for m in w["metrics"]], ["stripe.revenue_usd_30d"])
        self.assertEqual(w["metrics"][0]["last_week"], 10)
        self.assertEqual([(s["name"], s["value"]) for s in w["self_reported"]], [("waitlist_signups", 58)])
        goals = {g["id"]: g for g in w["goals"]}
        self.assertEqual((goals[self.g_rev]["current"], goals[self.g_rev]["source"]), (30, "stripe"))
        self.assertIsNone(goals[self.g_wait]["current"], "a goal fed only by manual rows has no score")
        # The gate: every number under goals[*].current and metrics comes from a connector row.
        connector = {r["value"] for r in db.q("SELECT value FROM metrics WHERE company_id=? AND source!='manual'",
                                              (self.c["id"],))}
        for n in [g["current"] for g in w["goals"] if g["current"] is not None] + [m["value"] for m in w["metrics"]]:
            self.assertIn(n, connector)

    def test_weekly_lists_goalless_and_counts(self):
        add_task(self.c, "Agent idea without a goal")
        add_task(self.c, "Owner wish", created_by="owner")
        add_task(self.c, "Shipped for revenue", status="done", goal_id=self.g_rev)
        w = rhythms.weekly_data(self.c)
        self.assertEqual([t["title"] for t in w["goalless_tasks"]], ["Agent idea without a goal"])
        self.assertEqual([t["title"] for t in w["owner_tasks_without_goal"]], ["Owner wish"])
        self.assertEqual([(g["goal_id"], g["done"]) for g in w["done_per_goal"]], [(self.g_rev, 1)])
        self.assertEqual(set(w["usage"]), {"gpu_hours", "tokens"})
        self.assertEqual(w["emails"], {"sent": 0, "replies": 0})

    def test_due_today_counts_as_due(self):
        """decisions.review_at holds a date (the owner's Brain form) or a timestamp (log_decision, an answered
        decision). One due later today is due now, and the weekly review, the planning prompt and the digest agree:
        each compares the date part only (brain.decisions_due, brain.due_by)."""
        c = self.c
        today = dt.datetime.now(dt.timezone.utc).date()
        tomorrow = today + dt.timedelta(days=1)

        def decision(what, review_at):
            return db.run("INSERT INTO decisions(company_id, what, review_at, decided_by, created_at) "
                          "VALUES(?,?,?,?,?)", (c["id"], what, review_at, "ceo", db.now()))

        def experiment(hypothesis, stop):
            return db.run("INSERT INTO experiments(company_id, goal_id, hypothesis, metric, start, stop, verdict, "
                          "created_at) VALUES(?,?,?,?,?,?,?,?)", (c["id"], self.g_rev, hypothesis, "", str(today),
                                                                  stop, "pending", db.now()))
        later_today = decision("Price at $9 a month", f"{today}T23:59:59Z")
        dated_today = decision("Launch in r/dogs first", today.isoformat())
        not_yet = decision("Email pet shops on Mondays", f"{tomorrow}T00:00:01Z")
        stops_today = experiment("A vet line lifts signups", today.isoformat())
        stops_later = experiment("Cold email works", f"{tomorrow}T09:00:00Z")

        w = rhythms.weekly_data(c)
        self.assertEqual({d["id"] for d in w["decisions_due"]}, {later_today, dated_today})
        self.assertEqual([e["id"] for e in w["experiments_due"]], [stops_today])
        self.assertEqual({d["id"] for d in brain.decisions_due(c["id"], days=0)}, {later_today, dated_today})
        prompt = contracts.cycle_sections(c)   # the planning prompt looks 3 days ahead
        for did in (later_today, dated_today, not_yet):
            self.assertIn(f"\n#{did} (review ", prompt)
        self.assertIn(f"Due to conclude: #{stops_today} ", prompt)
        self.assertIn(f"Running: #{stops_later} ", prompt)
        self.assertIn(f"#{later_today} (review {today}) Price at $9 a month.", brain.digest(c))

    def test_morning_and_monthly_data(self):
        add_task(self.c, "Blocked thing", status="blocked", goal_id=self.g_rev)
        m = rhythms.morning_data(self.c)
        self.assertEqual(next(g for g in m["goals"] if g["id"] == self.g_rev)["gap"], 70)
        self.assertEqual(m["open_tasks_by_goal"][0]["goal_id"], self.g_rev)
        self.assertEqual([t["title"] for t in m["blocked_tasks"]], ["Blocked thing"])
        mo = rhythms.monthly_data(self.c)
        self.assertEqual(set(mo), {"weekly_briefs", "revenue_trend", "goals", "money_spent", "gpu_hours", "tokens"})
        self.assertEqual(mo["revenue_trend"][0]["now"], 30)

    def test_task_data_section_caps_and_drops_the_oldest(self):
        for i in range(60):
            add_task(self.c, f"Shipped piece {i:02d} " + "x" * 220, status="done", updated_at=ago(minutes=200 - i))
        task = add_task(self.c, "Evening brief", role="analyst", created_by="routine", routine="analyst:Evening brief")
        text = rhythms.task_data_section(self.c, task)
        head, rest = text.split("\n", 1)
        self.assertEqual(head, "## Data for this brief (the only numbers you may use)")
        block, instruction = rest.split("\n\n### What to do\n", 1)
        self.assertLessEqual(len(block), rhythms.DATA_CAP)
        data = json.loads(block)
        titles = [t["title"][:16] for t in data["shipped"]]
        self.assertIn("Shipped piece 59", titles, "the newest stays")
        self.assertNotIn("Shipped piece 30", titles, "the oldest go first")
        self.assertIn("write_brief(kind=\"daily\"", instruction)
        self.assertIn("Tomorrow's focus", instruction)
        self.assertEqual(rhythms._shown[task["id"]], ("analyst:Evening brief", data))
        other = add_task(self.c, "Ordinary work")
        self.assertEqual(rhythms.task_data_section(self.c, other), "")
        monthly = add_task(self.c, "Monthly strategy check", role="ceo", created_by="routine",
                           routine=rhythms.MONTHLY_KEY)
        self.assertIn('"Kill (pause the company)"', rhythms.task_data_section(self.c, monthly))


# =============================================================== write_brief

class TestNumbers(unittest.TestCase):
    def test_extraction_skips_dates_markers_paths_and_names(self):
        text = ("## 1. What moved\n1. stripe.revenue_usd_30d rose to $1,240.50 on 2026-10-14 at 18:00 (Oct 14, 2026)\n"
                "2) see sales/outreach-week3.md and https://x.io/a/77\n- email.sent_7d: 34, up 12% from the 3rd\n"
                "- task #12 shipped")
        self.assertEqual([raw for raw, _v in rhythms.numbers_in(text)], ["1,240.50", "34", "12", "12"])

    def test_unsupported_numbers(self):
        data = {"metrics": [{"name": "m", "value": 30, "value_24h_ago": 24}], "shipped": [{"id": 7}, {"id": 8}]}
        self.assertEqual(rhythms.unsupported_numbers("m is 30, up 6 (25%) in a day. 2 tasks shipped: #7, #8.", data),
                         [])
        self.assertEqual(rhythms.unsupported_numbers("m is 30.0 and we have 12 customers paying $340.", data),
                         ["12", "340"])
        self.assertEqual(rhythms.unsupported_numbers("about 18 or 18.4", {"v": 18.42}), [], "rounding is fine")
        self.assertEqual(rhythms.unsupported_numbers("m is 30.4", data), ["30.4"])
        self.assertEqual(rhythms.unsupported_numbers("Under 350 words.", data, "Under 350 words"), [])


class TestWriteBrief(Base):
    @classmethod
    def setUpClass(cls):
        notify.stop_poller()
        cls.tg = FakeTelegram().start()
        cls.settings = {"telegram_api_base": cls.tg.url, "telegram_bot_token": cls.tg.token,
                        "telegram_chat_id": str(cls.tg.chat_id), "ntfy_topic": "", "notify_level": "important",
                        "kill_switch": False}

    @classmethod
    def tearDownClass(cls):
        notify.flush()
        notify.stop_poller()
        cls.tg.stop()
        db.set_settings({"telegram_bot_token": "", "telegram_chat_id": "", "ntfy_topic": "",
                         "telegram_api_base": "https://api.telegram.org", "notify_level": "important"})
        notify._recent.clear()

    def setUp(self):
        super().setUp()
        db.set_settings(self.settings)
        self.tg.clear()
        self.c = make_company("BriefCo", ("ceo", "analyst", "engineering"))
        add_metric(self.c, "stripe.customers", 2, "stripe", ago(days=2))
        add_metric(self.c, "stripe.customers", 3, "stripe", ago(minutes=3))
        add_task(self.c, "Landing page", status="done")
        self.task = add_task(self.c, "Evening brief", role="analyst", status="in_progress", created_by="routine",
                             routine="analyst:Evening brief")
        self.shown = rhythms.task_data_section(self.c, self.task)
        self.ctx = ctx_for(self.c, "analyst", self.task)

    def tearDown(self):
        notify.flush()
        db.set_settings({"telegram_bot_token": "", "telegram_chat_id": ""})

    def test_write_brief_and_notify(self):
        body = ("## What moved\n- stripe.customers 3 (+1 in 24 h) [stripe]\n## What shipped\n- **Landing page**\n"
                "## What is stuck\nNothing.\n## Decisions needed\nNone.\n## Tomorrow's focus\n1. More customers")
        after = db.scalar("SELECT COALESCE(MAX(id), 0) FROM events") or 0
        with mock.patch.object(notify, "send", wraps=notify.send) as spy:
            out = tools.execute(self.ctx, "write_brief", {"kind": "daily", "body": body})
            notify.flush()
        self.assertTrue(out.startswith("Brief #"), out)
        row = db.one("SELECT * FROM briefs WHERE company_id=? ORDER BY id DESC LIMIT 1", (self.c["id"],))
        self.assertEqual((row["kind"], row["body"]), ("daily", body))
        self.assertEqual(json.loads(row["data"]), rhythms._shown[self.task["id"]][1], "the data the prompt showed")
        self.assertEqual(self.ctx.brief_written, "daily")
        ev = db.q("SELECT * FROM events WHERE company_id=? AND kind='brief' AND id>?", (self.c["id"], after))
        self.assertEqual(len(ev), 1)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(spy.call_args.args[2], "brief")
        self.assertEqual(len(self.tg.sent), 1)
        text = self.tg.sent[0]["text"]
        self.assertTrue(text.startswith("<b>BriefCo: daily brief</b>"), text)
        self.assertIn("<b>What moved</b>\n• stripe.customers 3 (+1 in 24 h) [stripe]", text)
        self.assertIn("• <b>Landing page</b>", text)
        self.assertIn(f"Dashboard: http://", text)
        self.assertIn(f"/#/c/{self.c['id']}", text)
        # The brief is the deliverable: finish passes without a file.
        self.assertEqual(tools.finish(self.ctx, "Wrote the brief", "done", "docs/nothing-here.md"), "Session ended.")
        self.assertEqual(self.ctx.finish_status, "done")
        # The run ended: the shown data is forgotten.
        rhythms.after_run(self.c, self.task, self.ctx, "done")
        self.assertNotIn(self.task["id"], rhythms._shown)

    def test_refuses_numbers_missing_from_the_data_once(self):
        body = "## What moved\nWe made $340 from 12 customers on 2026-10-14.\n1. Ship"
        first = rhythms.write_brief(self.ctx, "daily", body)
        self.assertTrue(first.startswith("ERROR: not stored"), first)
        self.assertIn("340, 12", first)
        self.assertNotIn("2026", first)
        self.assertIsNone(db.one("SELECT id FROM briefs WHERE company_id=?", (self.c["id"],)))
        self.assertEqual(self.ctx.brief_written, "")
        second = rhythms.write_brief(self.ctx, "daily", body)
        self.assertTrue(second.startswith("Brief #"), second)
        refused = db.q("SELECT * FROM events WHERE company_id=? AND kind='claims'", (self.c["id"],))
        self.assertEqual(json.loads(refused[0]["data"])["where"], "brief")

    def test_lenient_on_counts_dates_and_paths(self):
        body = ("## What moved\n1. stripe.customers: 3 [stripe], 2 a day earlier, on 2026-10-14 at 18:00.\n"
                "## What shipped\n1 task: site/index.html\n## Tomorrow's focus\n3 priorities, top 3 first")
        self.assertTrue(rhythms.write_brief(self.ctx, "daily", body).startswith("Brief #"))

    def test_kind_and_body_validation(self):
        self.assertTrue(rhythms.write_brief(self.ctx, "yearly", "x").startswith("ERROR: kind must be"))
        self.assertTrue(rhythms.write_brief(self.ctx, "daily", "  ").startswith("ERROR: the brief is empty"))

    def test_without_a_prompt_the_data_is_gathered_fresh(self):
        ctx = ctx_for(self.c, "ceo", None, kind="chat")
        out = rhythms.write_brief(ctx, "weekly", "## What moved\nstripe.customers 3 [stripe]")
        self.assertTrue(out.startswith("Brief #"), out)
        row = db.one("SELECT data FROM briefs WHERE company_id=? AND kind='weekly'", (self.c["id"],))
        self.assertIn("self_reported", json.loads(row["data"]))

    def test_long_brief_is_cut_for_the_phone(self):
        html = notify.brief_text("T", "\n".join(f"- line {i} " + "y" * 80 for i in range(200)), "http://x/#/")
        self.assertLess(len(html), notify.MAX_TEXT)
        self.assertIn("…", html)
        self.assertTrue(html.endswith("Dashboard: http://x/#/"))


# =============================================================== the weekly clean-up

class TestWeekly(Base):
    def test_after_weekly_cancels_goalless(self):
        c = make_company("WeeklyCo", ("ceo", "engineering"))
        gid = add_goal(c, "10 customers", "stripe.customers", 10)
        doomed = add_task(c, "Agent idea", created_by="Atlas")
        blocked = add_task(c, "Another agent idea", status="blocked", created_by="Forge")
        kept = [add_task(c, "Serves a goal", goal_id=gid), add_task(c, "Owner wish", created_by="owner"),
                add_task(c, "Morning plan", role="ceo", created_by="routine", routine="ceo:Morning plan"),
                add_task(c, "Reply to email #3", created_by="inbox"),
                add_task(c, "Old idea", status="done", created_by="Atlas")]
        review = add_task(c, "Weekly review", role="ceo", status="in_progress", created_by="routine",
                          routine=rhythms.WEEKLY_KEY)
        ctx = ctx_for(c, "ceo", review)
        rhythms.after_run(c, review, ctx, "error")
        self.assertEqual(db.get_task(doomed["id"])["status"], "todo", "a failed review cancels nothing")
        ctx.finished = True
        rhythms.after_run(c, review, ctx, "done")
        for t in (doomed, blocked):
            row = db.get_task(t["id"])
            self.assertEqual(row["status"], "cancelled")
            self.assertTrue(row["result"].endswith("[Weekly review] Cancelled: serves no goal"))
        self.assertEqual([db.get_task(t["id"])["status"] for t in kept], ["todo", "todo", "todo", "todo", "done"])
        self.assertEqual(db.get_task(review["id"])["status"], "in_progress")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND status IN "
                                   "('todo','in_progress','blocked','waiting') AND goal_id IS NULL AND "
                                   "created_by NOT IN ('owner','inbox') AND routine=''", (c["id"],)), 0)

    def test_other_routines_do_not_clean_up(self):
        c = make_company("NotWeeklyCo", ("ceo", "engineering"))
        t = add_task(c, "Agent idea", created_by="Atlas")
        brief = add_task(c, "Evening brief", role="ceo", created_by="routine", routine="ceo:Evening brief")
        rhythms.after_run(c, brief, ctx_for(c, "ceo", brief), "done")
        self.assertEqual(db.get_task(t["id"])["status"], "todo")


# =============================================================== decisions

class TestDecisions(Base):
    def setUp(self):
        super().setUp()
        self.c = make_company("DecideCo", ("ceo", "engineering"))
        self.ceo = agent_of(self.c, "ceo")

    def decision(self, question, options, rec, task_id=None, reversible=True, hours=1, **kw) -> dict:
        out = decisions.create(self.c, self.ceo, task_id, question, options, rec, hours, reversible, **kw)
        self.assertTrue(out.startswith("Decision #"), out)
        return db.one("SELECT * FROM approvals WHERE company_id=? AND action='decision' ORDER BY id DESC LIMIT 1",
                      (self.c["id"],))

    def overdue(self, row: dict) -> None:
        p = json.loads(row["payload"])
        p["deadline_at"] = ago(minutes=5)
        db.update("approvals", row["id"], payload=json.dumps(p))

    def test_decision_deadline(self):
        task = add_task(self.c, "Price the starter plan", role="ceo", status="waiting", created_by="Atlas")
        row = self.decision("Should we keep the starter plan at $9 a month or raise it to $15?",
                            ["Keep $9", "Raise to $15"], "Keep $9", task["id"], expected="signup rate of 5% or more")
        self.overdue(row)
        with sends() as sent:
            decisions.resolve_due()
        r = db.one("SELECT * FROM approvals WHERE id=?", (row["id"],))
        self.assertEqual((r["status"], r["result"], r["decided_by"]), ("answered", "Keep $9", "deadline"))
        t = db.get_task(task["id"])
        self.assertEqual(t["status"], "todo")
        self.assertIn(f"[Update] No answer by the deadline, so decision #{row['id']} took the recommended option: "
                      f"Keep $9", t["description"])
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["text"].startswith(f"Decision #{row['id']} was taken by the deadline: Keep $9"))
        self.assertEqual(db.get_company(self.c["id"])["cycle_requested"], 1)
        rec = db.one("SELECT * FROM decisions WHERE company_id=? ORDER BY id DESC LIMIT 1", (self.c["id"],))
        self.assertEqual((rec["what"], rec["decided_by"], rec["reversible"], rec["expected"]),
                         ("Keep $9", "deadline", 1, "signup rate of 5% or more"))
        self.assertIn("starter plan", rec["why"])
        self.assertGreater(db.parse_time(rec["review_at"]), dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=13))
        memory = db.one("SELECT content FROM memory WHERE company_id=? ORDER BY id DESC LIMIT 1", (self.c["id"],))
        self.assertIn("Keep $9", memory["content"])

        # An owner answer records the owner.
        task2 = add_task(self.c, "Pick a channel", role="ceo", status="waiting", created_by="Atlas")
        row2 = self.decision("Which community should we launch in first: r/dogs or the PawTalk forum?",
                             ["r/dogs", "PawTalk forum"], "r/dogs", task2["id"])
        actions.decide(row2["id"], "approve", answer="PawTalk forum")
        rec2 = db.one("SELECT * FROM decisions WHERE company_id=? ORDER BY id DESC LIMIT 1", (self.c["id"],))
        self.assertEqual((rec2["what"], rec2["decided_by"]), ("PawTalk forum", "owner"))
        self.assertIn(f"[Update] Owner decided #{row2['id']}: PawTalk forum", db.get_task(task2["id"])["description"])

        # An irreversible one stays pending and is reminded once a day.
        row3 = self.decision("Should we close the Etsy shop for good and delete its listings?",
                             ["Close it", "Keep it open"], "Keep it open", reversible=False)
        self.overdue(row3)
        with sends() as sent:
            decisions.resolve_due()
            decisions.resolve_due()
        self.assertEqual(db.one("SELECT status FROM approvals WHERE id=?", (row3["id"],))["status"], "pending")
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["text"].startswith("Reminder: "))
        self.assertIn("waits for you", sent[0]["text"])
        self.assertEqual(len(sent[0]["buttons"]), 2)

    def test_kill_switch_holds_the_deadline(self):
        row = self.decision("Should we post the launch thread on Tuesday or on Thursday?", ["Tuesday", "Thursday"],
                            "Tuesday")
        self.overdue(row)
        db.set_settings({"kill_switch": True})
        try:
            with sends():
                decisions.resolve_due()
            self.assertEqual(db.one("SELECT status FROM approvals WHERE id=?", (row["id"],))["status"], "pending")
        finally:
            db.set_settings({"kill_switch": False})
        with sends():
            decisions.resolve_due()
        self.assertEqual(db.one("SELECT status FROM approvals WHERE id=?", (row["id"],))["status"], "answered")

    def test_ask_decision_validation_and_dedupe(self):
        task = add_task(self.c, "Decide the price", role="ceo", status="in_progress", created_by="Atlas")
        ctx = ctx_for(self.c, "ceo", task)
        q = "Should we price the starter plan at $29 or $49 a month?"

        def ask(**kw):
            args = {"question": q, "options": "$29\n$49", "recommended": "$29", **kw}
            return tools.execute(ctx, "ask_decision", args)

        self.assertIn("give 2 to 5 options", ask(options="$29"))
        self.assertIn("give 2 to 5 options", ask(options="a\nb\nc\nd\ne\nf"))
        self.assertIn("recommended must be one of the options", ask(recommended="$39"))
        self.assertIn("deadline_hours must be", ask(deadline_hours=0))
        self.assertIn("deadline_hours must be", ask(deadline_hours=400))
        self.assertIn("the owner reads questions on a phone", ask(question="Proceed?"))
        out = ask(options="- $29\n- $49", recommended="$29", deadline_hours=24, context="Two competitors charge $39.")
        self.assertRegex(out, r"^Decision #\d+ sent to the owner\. If it is reversible and unanswered by "
                              r"\d{4}-\d\d-\d\d \d\d:\d\d UTC, '\$29' will be taken\. Continue with work that does not "
                              r"depend on it, or finish with status 'waiting'\.$")
        row = db.one("SELECT * FROM approvals WHERE company_id=? AND action='decision'", (self.c["id"],))
        p = json.loads(row["payload"])
        self.assertEqual((p["options"], p["recommended"], p["reversible"], p["topics"]),
                         (["$29", "$49"], "$29", True, ["pricing"]))
        self.assertEqual(row["task_id"], task["id"])
        self.assertEqual(row["decided_by"], "")
        # The same topic again is folded into the pending one; the run may then finish waiting on it.
        other = add_task(self.c, "Price check", role="ceo", status="in_progress", created_by="Atlas")
        ctx2 = ctx_for(self.c, "ceo", other)
        again = tools.execute(ctx2, "ask_decision", {"question": "What price should the starter plan have?",
                                                     "options": "$19\n$29", "recommended": "$19"})
        self.assertTrue(again.startswith(f"Already waiting on the owner as #{row['id']}"), again)
        self.assertEqual(ctx2.waiting_on, row["id"])
        self.assertEqual(tools.finish(ctx2, "Waiting on the price decision", "waiting"), "Session ended.")
        self.assertEqual(tools.finish(ctx, "Asked", "waiting"), "Session ended.")
        # ask_owner is folded into a pending decision too.
        ctx3 = ctx_for(self.c, "ceo", add_task(self.c, "Third", role="ceo", status="in_progress"))
        self.assertTrue(planning.ask_owner(ctx3, "Is $29 the right price for the starter plan?", "Yes\nNo",
                                           "Yes").startswith("Already waiting on the owner"))
        # The claims gate runs first.
        with mock.patch.object(truth, "claims_guard", lambda *a: "ERROR: not sent to the owner: test"):
            self.assertEqual(ask(question="Should we deploy the site to Netlify today?"),
                             "ERROR: not sent to the owner: test")

    def test_decisions_count_toward_the_question_cap(self):
        ctx = ctx_for(self.c, "ceo", add_task(self.c, "Ask things", role="ceo", status="in_progress"))
        self.decision("Should we write the blog in English or in Spanish first?", ["English", "Spanish"], "English")
        self.decision("Should the newsletter go out on Mondays or Fridays?", ["Mondays", "Fridays"], "Mondays")
        self.assertTrue(planning.ask_owner(ctx, "Should our mascot be a beagle or a corgi?", "Beagle\nCorgi",
                                           "Beagle").startswith("Question #"))
        capped = tools.execute(ctx, "ask_decision", {"question": "Should support answer within 4 or within 24 hours?",
                                                     "options": "4 hours\n24 hours", "recommended": "24 hours"})
        self.assertTrue(capped.startswith("ERROR: 3 questions from the team are already waiting"), capped)
        self.assertTrue(planning.ask_owner(ctx, "Should we offer a yearly plan or only monthly billing?",
                                           "Yearly too\nMonthly only", "Monthly only").startswith("ERROR: 3 questions"))
        # Decisions raised by code for a module (an origin) are not refused by the cap.
        out = decisions.create(self.c, self.ceo, None, "Atlas changed the goals. Keep them?", ["Keep", "Revert"],
                               "Keep", 48, True, origin={"kind": "goals"})
        self.assertTrue(out.startswith("Decision #"), out)

    def test_gate_dedupes_a_decision_on_its_question(self):
        args = (self.c, self.ceo, None, "Accept Hunter's change to the ICP: add dog walkers?",
                ["Accept the change", "Reject it"], "Reject it", 72, True)
        first = decisions.create(*args, origin={"kind": "brain_change", "change_id": 7})
        self.assertTrue(first.startswith("Decision #"), first)
        second = decisions.create(*args, origin={"kind": "brain_change", "change_id": 7})
        self.assertTrue(second.startswith("Already waiting for the owner as decision #"), second)

    def test_origin_gets_the_answer(self):
        seen = []
        row = self.decision("Accept Hunter's change to the price: $15 a month?", ["Accept the change", "Reject it"],
                            "Reject it", origin={"kind": "brain_change", "change_id": 9})
        with mock.patch.object(brain, "on_owner_decision", lambda cid, origin, answer: seen.append((cid, origin, answer))):
            actions.decide(row["id"], "approve", answer="Accept the change")
        self.assertEqual(seen, [(self.c["id"], {"kind": "brain_change", "change_id": 9}, "Accept the change")])

    def test_monthly_kill_pauses_the_company(self):
        task = add_task(self.c, "Monthly strategy check", role="ceo", status="in_progress", created_by="routine",
                        routine=rhythms.MONTHLY_KEY)
        ctx = ctx_for(self.c, "ceo", task)
        out = tools.execute(ctx, "ask_decision", {
            "question": "Keep, change or kill DecideCo?",
            "options": "Keep\nChange: sell to vets instead of owners\nKill (pause the company)",
            "recommended": "Keep", "reversible": True, "context": "Revenue is flat."})
        self.assertIn("cannot be undone", out)
        row = db.one("SELECT * FROM approvals WHERE company_id=? AND action='decision'", (self.c["id"],))
        p = json.loads(row["payload"])
        self.assertEqual((p["reversible"], p["origin"]), (False, {"kind": "monthly"}))
        self.overdue(row)
        with sends():
            decisions.resolve_due()
        self.assertEqual(db.one("SELECT status FROM approvals WHERE id=?", (row["id"],))["status"], "pending")
        actions.decide(row["id"], "approve", answer="Kill (pause the company)")
        self.assertEqual(db.get_company(self.c["id"])["status"], "paused")
        rec = db.one("SELECT * FROM decisions WHERE company_id=?", (self.c["id"],))
        self.assertEqual((rec["what"], rec["reversible"]), ("Kill (pause the company)", 0))

    def test_dismissed_decision_records_nothing(self):
        task = add_task(self.c, "Waiting on a choice", role="ceo", status="waiting")
        row = self.decision("Should we sponsor the local dog show this spring?", ["Sponsor it", "Skip it"],
                            "Skip it", task["id"])
        r = actions.decide(row["id"], "reject")
        self.assertEqual(r["status"], "rejected")
        self.assertIsNone(db.one("SELECT id FROM decisions WHERE company_id=?", (self.c["id"],)))
        self.assertIn("dismissed decision", db.get_task(task["id"])["description"])
        self.assertIsNone(db.one("SELECT id FROM tasks WHERE company_id=? AND title LIKE 'Revise:%'", (self.c["id"],)))

    def test_phone_rendering_and_buttons(self):
        row = self.decision("Should we launch on Product Hunt next week or wait a month?",
                            ["Next week", "Wait a month"], "Wait a month", hours=31,
                            context="The onboarding still has two known bugs.")
        text, buttons, kind = notify.build_approval(row)
        self.assertIn("asks you to decide (decision #", text)
        self.assertIn("Recommended: Wait a month", text)
        self.assertIn("Deadline: auto in 31 h", text)
        self.assertEqual(kind, "request")
        self.assertEqual(buttons, [[{"text": "Next week", "data": f"q:{row['id']}:0"}],
                                   [{"text": notify.CHECK + "Wait a month", "data": f"q:{row['id']}:1"}]])
        status = notify.format_status()
        self.assertIn("of them decision", status.splitlines()[1])
        self.assertEqual(notify.apply_callback(f"q:{row['id']}:0"), "Next week")
        r = db.one("SELECT * FROM approvals WHERE id=?", (row["id"],))
        self.assertEqual((r["status"], r["result"], r["decided_by"]), ("answered", "Next week", "telegram"))
        rec = db.one("SELECT decided_by FROM decisions WHERE company_id=?", (self.c["id"],))
        self.assertEqual(rec["decided_by"], "telegram")
        self.assertEqual(notify.deadline_text({"reversible": False, "deadline_at": ago(hours=-5)}), "waits for you")


# =============================================================== portfolio brief and routes

class TestPortfolio(Base):
    def setUp(self):
        super().setUp()
        db.run("DELETE FROM briefs WHERE company_id IS NULL")
        self.a = make_company("PortA", ("ceo", "analyst"))
        self.b = make_company("PortB", ("ceo",))
        add_goal(self.a, "10 paying customers", "stripe.customers", 10)
        add_metric(self.a, "stripe.customers", 1, "stripe", ago(hours=30))
        add_metric(self.a, "stripe.customers", 2, "stripe", ago(minutes=2))
        add_task(self.a, "Shipped thing", status="done")
        decisions.create(self.a, agent_of(self.a, "ceo"), None, "Should we offer a free trial of 7 or 14 days?",
                         ["7 days", "14 days"], "14 days")
        self.active = mock.patch.object(rhythms, "_active_companies",
                                        lambda: [db.get_company(self.a["id"]), db.get_company(self.b["id"])])
        self.active.start()

    def tearDown(self):
        self.active.stop()
        for c in (self.a, self.b):
            db.update("companies", c["id"], status="archived")

    def brief(self, company: dict, at: str) -> int:
        return db.run("INSERT INTO briefs(company_id, kind, body, data, created_at) VALUES(?,?,?,?,?)",
                      (company["id"], "daily", "## What moved\nx", "{}", at))

    def test_portfolio_brief(self):
        self.brief(self.a, db.now())
        with sends() as sent:
            self.assertIsNone(rhythms.portfolio_brief_if_due(), "PortB has no brief yet and 2 h have not passed")
            with mock.patch.object(rhythms, "PORTFOLIO_WAIT_HOURS", 0):   # as if the first brief came 2 h ago
                bid = rhythms.portfolio_brief_if_due()
                self.assertIsNotNone(bid)
                self.assertIsNone(rhythms.portfolio_brief_if_due(), "once a day")
        row = db.one("SELECT * FROM briefs WHERE id=?", (bid,))
        self.assertIsNone(row["company_id"])
        self.assertEqual(row["kind"], "daily")
        self.assertIn("## PortA", row["body"])
        self.assertIn("- Goal #", row["body"])
        self.assertIn("stripe.customers 2 (+1 in 24 h) [stripe]", row["body"])
        self.assertIn("- Shipped 1, stuck 0, decisions needed 1", row["body"])
        self.assertTrue(row["body"].endswith("Decisions waiting for you: 1"))
        data = json.loads(row["data"])
        self.assertEqual([c["name"] for c in data["companies"]], ["PortA", "PortB"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["kind"], "brief")
        self.assertIn("<b>Holdco portfolio brief", sent[0]["text"])

    def test_due_at_once_when_every_company_has_a_brief(self):
        self.brief(self.a, db.now())
        self.brief(self.b, db.now())
        with sends() as sent:
            self.assertIsNotNone(rhythms.portfolio_brief_if_due())
        self.assertEqual(len(sent), 1)

    def test_a_brief_from_last_night_does_not_count(self):
        self.brief(self.a, ago(hours=30))
        with sends() as sent, mock.patch.object(rhythms, "PORTFOLIO_WAIT_HOURS", 0):
            self.assertIsNone(rhythms.portfolio_brief_if_due())
        self.assertEqual(sent, [])


class TestRoutes(Base):
    def test_run_now_and_brief_routes(self):
        c = make_company("RouteCo", ("ceo", "engineering"))
        r = call_route("POST", f"/api/companies/{c['id']}/rhythms/evening/run")
        self.assertTrue(r["created"])
        self.assertEqual((r["task"]["routine"], r["task"]["role"], r["task"]["created_by"]),
                         ("ceo:Evening brief", "ceo", "owner"))
        again = call_route("POST", f"/api/companies/{c['id']}/rhythms/evening/run")
        self.assertEqual((again["created"], again["task"]["id"]), (False, r["task"]["id"]))
        roles.add_agent(c["id"], preset="analyst")
        db.update("tasks", r["task"]["id"], status="done")
        by_analyst = call_route("POST", f"/api/companies/{c['id']}/rhythms/evening/run")
        self.assertEqual((by_analyst["task"]["routine"], by_analyst["task"]["role"]),
                         ("analyst:Evening brief", "analyst"))
        for key, routine_key in (("morning", rhythms.MORNING_KEY), ("weekly", rhythms.WEEKLY_KEY),
                                 ("monthly", rhythms.MONTHLY_KEY)):
            self.assertEqual(call_route("POST", f"/api/companies/{c['id']}/rhythms/{key}/run")["task"]["routine"],
                             routine_key)
        with self.assertRaises(routing.HTTPError):
            call_route("POST", f"/api/companies/{c['id']}/rhythms/hourly/run")
        lone = make_company("NoCeoCo", ("engineering",))
        with self.assertRaises(routing.HTTPError):
            call_route("POST", f"/api/companies/{lone['id']}/rhythms/weekly/run")

        b1 = db.run("INSERT INTO briefs(company_id, kind, body, data, created_at) VALUES(?,?,?,?,?)",
                    (c["id"], "daily", "Daily one", json.dumps({"metrics": []}), db.now()))
        b2 = db.run("INSERT INTO briefs(company_id, kind, body, data, created_at) VALUES(?,?,?,?,?)",
                    (c["id"], "weekly", "Weekly one", "{}", db.now()))
        listed = call_route("GET", f"/api/briefs?company_id={c['id']}")["briefs"]
        self.assertEqual([x["id"] for x in listed], [b2, b1])
        self.assertEqual(listed[1]["data"], {"metrics": []})
        self.assertEqual([x["id"] for x in call_route("GET", f"/api/briefs?company_id={c['id']}&kind=daily")["briefs"]],
                         [b1])
        self.assertEqual(len(call_route("GET", f"/api/briefs?company_id={c['id']}&limit=1")["briefs"]), 1)
        with self.assertRaises(routing.HTTPError):
            call_route("GET", "/api/briefs?kind=hourly")
        latest = call_route("GET", f"/api/briefs/latest?company_id={c['id']}")
        self.assertEqual((latest["daily"]["id"], latest["weekly"]["id"], latest["monthly"]), (b1, b2, None))
        portfolio = call_route("GET", "/api/briefs?company_id=0")["briefs"]
        self.assertTrue(all(x["company_id"] is None for x in portfolio))


if __name__ == "__main__":
    unittest.main()
