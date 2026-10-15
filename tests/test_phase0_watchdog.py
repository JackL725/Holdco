"""Phase 0 watchdog tests: working notes, retry guidance, escalations, the owner's answers and the periodic tick.

Run all tests with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOLDCO_DATA"] = tempfile.mkdtemp(prefix="holdco-watchdog-")

from holdco import actions, agents, bus, db, doctor, llm, roles, tools, watchdog, workspace  # noqa: E402

db.init()


def make_company(name="WatchCo", members=("ceo", "engineering", "marketing")) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "An idea", "active", "building", "{}", db.now()))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def ctx_for(company, role, task=None, kind="task") -> tools.Ctx:
    agent = db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))
    c = tools.Ctx(company=company, agent=agent, kind=kind, run_id=0, task=task, settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(agent, kind)}
    return c


def add_task(company, title="Build landing page", role="engineering", status="todo", priority=2, **extra) -> dict:
    agent = db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))
    ts = db.now()
    cols = {"company_id": company["id"], "title": title, "role": role, "agent_id": agent["id"] if agent else None,
            "status": status, "priority": priority, "created_by": "test", "created_at": ts, "updated_at": ts, **extra}
    tid = db.run(f"INSERT INTO tasks({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", list(cols.values()))
    return db.get_task(tid)


def questions(company_id: int, pattern: str | None = None) -> list[dict]:
    out = []
    for r in db.q("SELECT * FROM approvals WHERE company_id=? AND action='question' ORDER BY id", (company_id,)):
        r["p"] = db.loads(r["payload"], {}) or {}
        if pattern is None or r["p"].get("pattern") == pattern:
            out.append(r)
    return out


def ago(minutes: float) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes)).strftime(watchdog.TIME_FMT)


def auto_entry(text: str, minutes_ago: float = 5) -> str:
    return f"--- [auto] {ago(minutes_ago)} ---\n{text}"


class TestNotes(unittest.TestCase):
    def setUp(self):
        self.c = make_company()
        self.task = add_task(self.c)

    def test_add_note_dates_entries_and_keeps_the_newest(self):
        watchdog.add_note(self.task["id"], "  tried the obvious thing  ", "Ava")
        notes = db.get_task(self.task["id"])["notes"]
        self.assertRegex(notes, r"^--- \[Ava\] \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ---\ntried the obvious thing$")
        watchdog.add_note(self.task["id"], "second", "")
        ents = watchdog.entries(db.get_task(self.task["id"])["notes"])
        self.assertEqual([e["author"] for e in ents], ["Ava", "note"])
        self.assertEqual(ents[-1]["text"], "second")
        # Big entries push the oldest out; the kept text starts at an entry boundary and ends with the newest text.
        for i in range(4):
            watchdog.add_note(self.task["id"], f"entry {i} " + ("x" * 1500), "auto")
        notes = db.get_task(self.task["id"])["notes"]
        self.assertLessEqual(len(notes), watchdog.NOTES_MAX_CHARS)
        self.assertTrue(notes.startswith("--- ["), notes[:40])
        self.assertNotIn("tried the obvious thing", notes)
        self.assertIn("entry 3", notes)
        self.assertEqual(watchdog.entries(notes)[-1]["text"], "entry 3 " + "x" * 1500)

    def test_add_note_ignores_blank_text_and_unknown_tasks(self):
        watchdog.add_note(self.task["id"], "   ", "Ava")
        self.assertEqual(db.get_task(self.task["id"])["notes"], "")
        watchdog.add_note(999999, "nothing to attach to", "Ava")   # must not raise

    def test_note_progress_tool_through_execute(self):
        self.assertIn("note_progress", roles.tools_for(ctx_for(self.c, "engineering").agent, "task"))
        self.assertNotIn("note_progress", roles.tools_for(ctx_for(self.c, "engineering").agent, "cycle"))
        ctx = ctx_for(self.c, "engineering", task=self.task)
        self.assertEqual(tools.execute(ctx, "note_progress", {"notes": "wrote the hero section; form is missing"}),
                         "Saved.")
        notes = db.get_task(self.task["id"])["notes"]
        self.assertIn(f"[{ctx.agent['name']}]", notes)
        self.assertIn("form is missing", notes)
        self.assertTrue(tools.execute(ctx, "note_progress", {"notes": " "}).startswith("ERROR:"))
        cycle = ctx_for(self.c, "ceo", kind="cycle")
        cycle.allowed.add("note_progress")
        self.assertTrue(tools.execute(cycle, "note_progress", {"notes": "x"}).startswith("ERROR:"))


class TestRetryGuidance(unittest.TestCase):
    def guidance(self, notes: str = "", result: str = "", attempts: int = 2, task: dict | None = None) -> str:
        t = dict(task or {})
        t.update(notes=notes, result=result, attempts=attempts)
        text = watchdog.retry_guidance(t)
        self.assertLess(len(text), 600)
        self.assertRegex(text, r"^Attempt \d+ failed because .+\. Do not repeat that approach: .+")
        return text

    def test_repeated_action(self):
        same = "; ".join(['review_text({"path": "docs/a.md"})'] * 3)
        text = self.guidance(auto_entry("Attempt 1 ended: partial\nLast tool calls: " + same))
        self.assertIn("repeated the same action", text)
        self.assertIn("different tool", text)
        text = self.guidance(auto_entry("Attempt 1 ended: partial\nLast tool calls: a({}); b({})"),
                             "[Ava] Status: partial\nSummary: stopped after repeating the same action (fetch_url)")
        self.assertIn("different tool", text)

    def test_search_unavailable(self):
        text = self.guidance(auto_entry("Attempt 1 ended: partial\nLast tool calls: web_search({\"query\": \"x\"})\n"
                                        "Last error: ERROR: web search is unavailable right now (duckduckgo: blocked)"))
        self.assertIn("web search was unavailable", text)
        self.assertIn("fetch_url", text)

    def test_out_of_steps(self):
        text = self.guidance(auto_entry("Attempt 1 ended: out_of_steps\nFiles written: site/index.html"))
        self.assertIn("ran out of steps", text)
        self.assertIn("Scope down".lower(), text.lower())

    def test_model_error(self):
        text = self.guidance(auto_entry("Attempt 1 ended: error (Timed out waiting for the model after 600s)"))
        self.assertIn("model call failed", text)
        self.assertIn("Timed out waiting", text)
        self.assertIn("append=true", text)

    def test_owner_answer_beats_the_failure_kind(self):
        c = make_company()
        t = add_task(c, status="todo", attempts=1)
        db.run("INSERT INTO approvals(company_id, agent_id, task_id, action, summary, payload, status, result, "
               "created_at, decided_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (c["id"], None, t["id"], "question", "Which plan?", '{"question": "Which plan?"}', "answered",
                "The cheap one", ago(30), db.now()))
        text = self.guidance(auto_entry("Attempt 1 ended: out_of_steps", minutes_ago=20), attempts=2, task=t)
        self.assertIn("owner's answer", text)
        # An answer older than the latest failed attempt is not the reason for the retry.
        db.run("UPDATE approvals SET decided_at=? WHERE task_id=?", (ago(60), t["id"]))
        self.assertIn("ran out of steps", self.guidance(auto_entry("Attempt 1 ended: out_of_steps", 20), task=t))

    def test_partial_and_unknown(self):
        self.assertIn("finished only partially", self.guidance(auto_entry("Attempt 1 ended: partial")))
        text = self.guidance("", "", attempts=3)
        self.assertTrue(text.startswith("Attempt 2 failed because it did not finish"), text)
        self.assertIn("note_progress", text)

    def test_attempt_number_comes_from_the_note(self):
        self.assertTrue(self.guidance(auto_entry("Attempt 4 ended: out_of_steps"), attempts=5).startswith("Attempt 4 "))


class TestTaskFailed(unittest.TestCase):
    def setUp(self):
        self.c = make_company()
        db.set_settings({"max_task_attempts": 2})

    def test_first_failure_leaves_todo_without_a_question(self):
        t = add_task(self.c, status="todo", attempts=1)
        watchdog.task_failed(t, "Ran out of steps before finishing.", "out_of_steps")
        self.assertEqual(db.get_task(t["id"])["status"], "todo")
        self.assertEqual(questions(self.c["id"]), [])

    def test_second_failure_blocks_and_asks_once(self):
        t = add_task(self.c, status="todo", attempts=2, notes=auto_entry("Attempt 2 ended: out_of_steps"))
        watchdog.task_failed(t, "Ran out of steps before finishing.", "out_of_steps")
        self.assertEqual(db.get_task(t["id"])["status"], "blocked")
        qs = questions(self.c["id"], "task_failed_twice")
        self.assertEqual(len(qs), 1)
        q = qs[0]
        self.assertEqual(q["status"], "pending")
        self.assertEqual(q["task_id"], t["id"])
        ceo = db.one("SELECT id FROM agents WHERE company_id=? AND role='ceo'", (self.c["id"],))
        self.assertEqual(q["agent_id"], ceo["id"])
        self.assertEqual(set(q["p"]), {"question", "options", "recommended", "context", "pattern", "task_id",
                                       "company_id"})
        self.assertEqual(q["p"]["options"], ["Skip this task", "Retry once more with my note", "Pause the company"])
        self.assertEqual(q["p"]["recommended"], "Skip this task")
        self.assertEqual(q["p"]["company_id"], self.c["id"])
        self.assertIn(f"Task #{t['id']} 'Build landing page' failed twice (last time: ran out of steps)",
                      q["p"]["question"])
        self.assertEqual(q["p"]["context"], "Attempt 2 ended: out_of_steps")
        self.assertEqual(q["summary"], q["p"]["question"][:140])
        # A second report for the same task does not create a second question.
        watchdog.task_failed(db.get_task(t["id"]), "Run ended: boom", "error")
        self.assertEqual(len(questions(self.c["id"], "task_failed_twice")), 1)
        self.assertEqual(db.get_task(t["id"])["status"], "blocked")

    def test_priority_one_recommends_a_retry_and_names_the_reason(self):
        t = add_task(self.c, status="todo", attempts=2, priority=1)
        watchdog.task_failed(t, "Status: partial\nSummary: stopped after repeating the same action (fetch_url) "
                             "three times.", "partial")
        q = questions(self.c["id"], "task_failed_twice")[0]
        self.assertEqual(q["p"]["recommended"], "Retry once more with my note")
        self.assertIn("finished only partially: stopped after repeating the same action", q["p"]["question"])

    def test_stopped_runs_give_the_attempt_back(self):
        t = add_task(self.c, status="todo", attempts=2)
        watchdog.task_failed(t, "Run ended: Kill switch", "stopped")
        self.assertEqual(db.get_task(t["id"])["attempts"], 1)
        self.assertEqual(db.get_task(t["id"])["status"], "todo")
        self.assertEqual(questions(self.c["id"]), [])

    def test_max_task_attempts_setting(self):
        db.set_settings({"max_task_attempts": 4})
        try:
            t = add_task(self.c, status="blocked", attempts=3)   # _finalize's old >= 3 rule blocked it
            watchdog.task_failed(t, "Run ended: boom", "error")
            self.assertEqual(db.get_task(t["id"])["status"], "todo")
            self.assertEqual(questions(self.c["id"]), [])
            db.update("tasks", t["id"], attempts=4)
            watchdog.task_failed(db.get_task(t["id"]), "Run ended: boom", "error")
            self.assertEqual(db.get_task(t["id"])["status"], "blocked")
            self.assertIn("failed 4 times (last time: hit an error: boom)",
                          questions(self.c["id"], "task_failed_twice")[0]["p"]["question"])
        finally:
            db.set_settings({"max_task_attempts": 2})


class TestEscalate(unittest.TestCase):
    def setUp(self):
        self.c = make_company()

    def test_dedupe_pending_then_quiet_for_six_hours(self):
        first = watchdog.escalate(self.c, "stalled", "Stuck?", watchdog.PAUSE_OPTIONS, "Pause the company", "why")
        self.assertIsInstance(first, int)
        self.assertEqual(watchdog.escalate(self.c, "stalled", "Stuck again?", watchdog.PAUSE_OPTIONS,
                                           "Pause the company"), first)
        self.assertEqual(len(questions(self.c["id"], "stalled")), 1)
        ev = db.one("SELECT * FROM events WHERE company_id=? AND kind='approval' ORDER BY id DESC", (self.c["id"],))
        self.assertIn("asked the owner: Stuck?", ev["message"])
        actions.decide(first, "approve", answer="Keep going")
        self.assertEqual(db.one("SELECT status FROM approvals WHERE id=?", (first,))["status"], "answered")
        self.assertIsNone(watchdog.escalate(self.c, "stalled", "Stuck?", watchdog.PAUSE_OPTIONS, "Pause the company"))
        self.assertEqual(len(questions(self.c["id"], "stalled")), 1)
        db.update("approvals", first, decided_at=ago(7 * 60))
        third = watchdog.escalate(self.c, "stalled", "Stuck?", watchdog.PAUSE_OPTIONS, "Pause the company")
        self.assertIsInstance(third, int)
        self.assertNotEqual(third, first)
        self.assertEqual(len(questions(self.c["id"], "stalled")), 2)

    def test_task_patterns_dedupe_per_task(self):
        a, b = add_task(self.c, "A"), add_task(self.c, "B")
        ia = watchdog.escalate(self.c, "task_failed_twice", "A?", watchdog.TASK_OPTIONS, "Skip this task", "", a["id"])
        ib = watchdog.escalate(self.c, "task_failed_twice", "B?", watchdog.TASK_OPTIONS, "Skip this task", "", b["id"])
        self.assertNotEqual(ia, ib)
        self.assertEqual(watchdog.escalate(self.c, "task_failed_twice", "A?", watchdog.TASK_OPTIONS, "Skip this task",
                                           "", a["id"]), ia)


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.c = make_company()
        db.set_settings({"max_task_attempts": 2})

    def failed_task(self, **extra) -> tuple[dict, int]:
        t = add_task(self.c, status="todo", attempts=2, **extra)
        watchdog.task_failed(t, "Ran out of steps before finishing.", "out_of_steps")
        return db.get_task(t["id"]), questions(self.c["id"], "task_failed_twice")[-1]["id"]

    def test_skip_this_task(self):
        t, aid = self.failed_task()
        row = actions.decide(aid, "approve", answer="Skip this task")
        self.assertEqual(row["status"], "answered")
        t = db.get_task(t["id"])
        self.assertEqual(t["status"], "cancelled")
        self.assertIn("[Owner] Skipped this task", t["result"])

    def test_retry_once_more_with_my_note(self):
        t, aid = self.failed_task()
        actions.decide(aid, "approve", feedback="Use the cached competitor list in research/",
                       answer="Retry once more with my note")
        t = db.get_task(t["id"])
        self.assertEqual((t["status"], t["attempts"], t["reassigned"]), ("todo", 0, 0))
        ents = watchdog.entries(t["notes"])
        self.assertEqual(ents[-1]["author"], "owner")
        self.assertEqual(ents[-1]["text"], "Use the cached competitor list in research/")

    def test_retry_takes_the_note_from_the_answer_text(self):
        t, aid = self.failed_task()
        actions.decide(aid, "approve", answer="retry once more with my note: skip the pricing table")
        t = db.get_task(t["id"])
        self.assertEqual(t["status"], "todo")
        self.assertEqual(watchdog.entries(t["notes"])[-1]["text"], "skip the pricing table")

    def test_pause_the_company(self):
        t, aid = self.failed_task()
        actions.decide(aid, "approve", answer="pause the company")
        self.assertEqual(db.get_company(self.c["id"])["status"], "paused")
        self.assertTrue(db.one("SELECT id FROM events WHERE company_id=? AND message LIKE 'paused the company%'",
                               (self.c["id"],)))

    def test_continue_anyway_and_retry_founding(self):
        db.update("companies", self.c["id"], status="founding", charter="# Charter")
        watchdog.founding_ended(db.get_company(self.c["id"]), "out_of_steps", "Status: partial")
        aid = questions(self.c["id"], "founding_partial")[0]["id"]
        actions.decide(aid, "approve", answer="Continue anyway")
        self.assertEqual(db.get_company(self.c["id"])["status"], "active")
        db.update("approvals", aid, decided_at=ago(7 * 60))
        watchdog.founding_ended(db.get_company(self.c["id"]), "partial", "")
        aid2 = questions(self.c["id"], "founding_partial")[-1]["id"]
        self.assertNotEqual(aid2, aid)
        calls = []
        original = agents.found
        agents.found = lambda cid: calls.append(cid) or True
        try:
            actions.decide(aid2, "approve", answer="Retry founding")
        finally:
            agents.found = original
        self.assertEqual(calls, [self.c["id"]])

    def test_free_text_only_keeps_todays_behaviour(self):
        t, aid = self.failed_task()
        actions.decide(aid, "approve", answer="hmm, let me think about it over the weekend")
        t = db.get_task(t["id"])
        self.assertEqual(t["status"], "todo")   # decide() reopened the blocked task, nothing was cancelled
        self.assertEqual(t["attempts"], 2)
        self.assertIn("Owner answered question", t["description"])

    def test_resolve_never_raises(self):
        watchdog.resolve({"payload": "not json", "company_id": None, "task_id": None}, "Skip this task")
        watchdog.resolve({"payload": '{"options": ["Skip this task"], "company_id": 999999}', "task_id": None},
                         "Skip this task")

    def test_match_option(self):
        self.assertEqual(watchdog.match_option("Skip this task", watchdog.TASK_OPTIONS), "Skip this task")
        self.assertEqual(watchdog.match_option("PAUSE THE COMPANY", watchdog.TASK_OPTIONS), "Pause the company")
        self.assertEqual(watchdog.match_option("skip", watchdog.TASK_OPTIONS), "Skip this task")
        self.assertIsNone(watchdog.match_option("do something else", watchdog.TASK_OPTIONS))
        self.assertIsNone(watchdog.match_option("", watchdog.TASK_OPTIONS))


class TestFoundingEnded(unittest.TestCase):
    def test_with_and_without_charter(self):
        c = make_company()
        db.update("companies", c["id"], charter="# PawPlan\nOne-liner: meal plans for dogs")
        watchdog.founding_ended(db.get_company(c["id"]), "out_of_steps", "Status: partial\nSummary: ran out")
        qs = questions(c["id"], "founding_partial")
        self.assertEqual(len(qs), 1)
        p = qs[0]["p"]
        self.assertEqual(p["options"], ["Continue anyway", "Retry founding", "Pause the company"])
        self.assertEqual(p["recommended"], "Continue anyway")
        self.assertIn("A charter exists", p["question"])
        self.assertIn("ran out of steps", p["question"])
        self.assertIsNone(p["task_id"])
        watchdog.founding_ended(db.get_company(c["id"]), "partial", "")
        self.assertEqual(len(questions(c["id"], "founding_partial")), 1)

        bare = make_company("BareCo")
        watchdog.founding_ended(bare, "partial", "")
        p = questions(bare["id"], "founding_partial")[0]["p"]
        self.assertEqual(p["recommended"], "Retry founding")
        self.assertIn("No charter was written", p["question"])


class TestTick(unittest.TestCase):
    def setUp(self):
        self.c = make_company()
        db.set_settings({"scheduler_enabled": True, "kill_switch": False})
        self.saved_last = doctor.LAST
        doctor.LAST = []
        watchdog._bad_streak.clear()
        watchdog._last_connector_alert.clear()
        watchdog._last_doctor_seen = None

    def tearDown(self):
        doctor.LAST = self.saved_last
        watchdog._bad_streak.clear()
        watchdog._last_connector_alert.clear()
        watchdog._last_doctor_seen = None

    def test_repeated_error(self):
        for n in (600, 601):
            bus.emit(self.c["id"], "error", f"Ava hit an error: Timed out after {n}s on task #{n}")
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "repeated_error"), [])
        bus.emit(self.c["id"], "error", "Ava hit an error: Timed out after 602s on task #7")
        watchdog.tick()
        watchdog.tick()
        qs = questions(self.c["id"], "repeated_error")
        self.assertEqual(len(qs), 1)
        p = qs[0]["p"]
        self.assertEqual(p["options"], ["Pause the company", "Keep going"])
        self.assertEqual(p["recommended"], "Pause the company")
        self.assertIn("The same error happened 3 times in the last hour at WatchCo", p["question"])
        self.assertIn("Timed out after", p["context"])

    def test_old_errors_do_not_count(self):
        for _ in range(3):
            db.run("INSERT INTO events(company_id, kind, message, data, created_at) VALUES(?,?,?,?,?)",
                   (self.c["id"], "error", "Ava hit an error: boom", "{}", ago(90)))
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "repeated_error"), [])

    def test_normalise_error(self):
        a = watchdog.normalise_error("Ava hit an error: Timed out after 600s on task #12")
        b = watchdog.normalise_error("Ben hit an error:  Timed  out after 7s on task #99")
        self.assertEqual(a, b)
        self.assertEqual(a, "timed out after #s on task #")
        self.assertNotEqual(a, watchdog.normalise_error("Ava hit an error: Disk full"))
        self.assertEqual(len(watchdog.error_hash("x")), 8)

    def test_task_bouncing(self):
        t = add_task(self.c, "Write FAQ", role="marketing")
        ctx = ctx_for(self.c, "ceo", kind="cycle")
        ctx.allowed.add("update_task")
        tools.execute(ctx, "update_task", {"task_id": t["id"], "assign_to": "marketing"})   # same role: no bounce
        self.assertEqual(db.get_task(t["id"])["reassigned"], 0)
        for role in ("engineering", "marketing", "engineering"):
            self.assertEqual(tools.execute(ctx, "update_task", {"task_id": t["id"], "assign_to": role}),
                             f"Task #{t['id']} updated.")
        self.assertEqual(db.get_task(t["id"])["reassigned"], 3)
        watchdog.tick()
        watchdog.tick()
        qs = questions(self.c["id"], "task_bouncing")
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["task_id"], t["id"])
        self.assertEqual(qs[0]["p"]["recommended"], "Retry once more with my note")
        self.assertEqual(qs[0]["p"]["options"], watchdog.TASK_OPTIONS)
        self.assertIn("handed between agents 3 times", qs[0]["p"]["question"])
        self.assertEqual(db.get_task(t["id"])["status"], "blocked")
        actions.decide(qs[0]["id"], "approve", answer="Retry once more with my note")
        t = db.get_task(t["id"])
        self.assertEqual((t["status"], t["reassigned"]), ("todo", 0))

    def test_stalled_by_the_model_server(self):
        add_task(self.c, "Anything")
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "stalled"), [])   # no evidence of a problem yet
        doctor.LAST = [{"key": "model_server", "label": "Model server", "status": "bad",
                        "detail": "connection refused on 127.0.0.1:11434", "fix": None, "checked_at": db.now()}]
        rid = db.run("INSERT INTO runs(company_id, agent_id, kind, status, started_at) VALUES(?,?,?,?,?)",
                     (self.c["id"], None, "task", "error", db.now()))
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "stalled"), [])   # a run started recently
        db.update("runs", rid, started_at=ago(3 * 60))
        watchdog.tick()
        watchdog.tick()
        qs = questions(self.c["id"], "stalled")
        self.assertEqual(len(qs), 1)
        self.assertIn("Agents at WatchCo are stuck: the model server is unreachable (connection refused",
                      qs[0]["p"]["question"])
        self.assertEqual(qs[0]["p"]["options"], ["Pause the company", "Keep going"])
        self.assertEqual(qs[0]["p"]["recommended"], "Pause the company")

    def test_stalled_by_errored_runs_needs_autopilot_and_open_work(self):
        for _ in range(3):
            db.run("INSERT INTO runs(company_id, agent_id, kind, status, started_at) VALUES(?,?,?,?,?)",
                   (self.c["id"], None, "task", "error", ago(3 * 60)))
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "stalled"), [])   # nothing open
        add_task(self.c, "Anything")
        db.set_settings({"kill_switch": True})
        try:
            watchdog.tick()
            self.assertEqual(questions(self.c["id"], "stalled"), [])   # stopped on purpose
        finally:
            db.set_settings({"kill_switch": False})
        watchdog.tick()
        qs = questions(self.c["id"], "stalled")
        self.assertEqual(len(qs), 1)
        self.assertIn("the last 3 runs all ended in errors", qs[0]["p"]["question"])

    def test_connector_down_after_two_consecutive_bad_checks(self):
        key = f"connector:{self.c['id']}:email_smtp"

        def rows(status, checked):
            return [{"key": "model_server", "label": "Model server", "status": "ok", "detail": "", "fix": None,
                     "checked_at": checked},
                    {"key": key, "label": "Email (SMTP)", "status": status, "detail": "login failed (535)",
                     "fix": None, "checked_at": checked}]
        doctor.LAST = rows("bad", "2026-10-14T10:00:00Z")
        watchdog.tick()
        watchdog.tick()   # the same Doctor run counts once
        self.assertEqual(questions(self.c["id"], "connector_down"), [])
        doctor.LAST = rows("ok", "2026-10-14T11:00:00Z")
        watchdog.tick()
        doctor.LAST = rows("bad", "2026-10-14T12:00:00Z")
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "connector_down"), [])   # the streak was reset by the ok run
        doctor.LAST = rows("bad", "2026-10-14T13:00:00Z")
        watchdog.tick()
        qs = questions(self.c["id"], "connector_down")
        self.assertEqual(len(qs), 1)
        p = qs[0]["p"]
        self.assertIn("Email (SMTP) for WatchCo has failed on two checks in a row (login failed (535))", p["question"])
        self.assertEqual(p["options"], ["Keep going without it", "Pause the company"])
        self.assertEqual(p["recommended"], "Keep going without it")
        actions.decide(qs[0]["id"], "approve", answer="Keep going without it")
        doctor.LAST = rows("bad", "2026-10-14T14:00:00Z")
        watchdog.tick()
        self.assertEqual(len(questions(self.c["id"], "connector_down")), 1)   # once per connector per day

    def test_tick_never_raises(self):
        def broken():
            raise RuntimeError("db gone")

        original = watchdog.db.get_settings
        watchdog.db.get_settings = broken
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:   # tick prints the problem instead of raising
                watchdog.tick()
        finally:
            watchdog.db.get_settings = original
        self.assertIn("db gone", err.getvalue())
        doctor.LAST = [{"key": None, "status": "bad"}, "garbage"]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            watchdog.tick()
        self.assertEqual(err.getvalue(), "")   # rows that are not rows are skipped quietly


class TestFailedTwiceThroughTheAgentLoop(unittest.TestCase):
    """A task that fails twice through agents.run leaves auto notes behind and ends as one question in the inbox."""

    DOC = "docs/marketing/positioning.md"

    def setUp(self):
        self.c = make_company()
        db.set_settings({"max_task_attempts": 2})
        self.agent = db.one("SELECT * FROM agents WHERE company_id=? AND role='marketing'", (self.c["id"],))
        self.task_id = add_task(self.c, "Draft positioning", role="marketing", deliverable=self.DOC)["id"]
        self.prompts: list[str] = []

    def run_script(self, script):
        steps = iter(script)

        def fake_chat(messages, tools_spec=None, *a, **kw):
            if len(self.prompts) == 0 or self.prompts[-1] is not messages[1]["content"]:
                self.prompts.append(messages[1]["content"])
            try:
                name, args = next(steps)
            except StopIteration:
                return llm.Reply(content="")
            return llm.Reply(tool_calls=[llm.ToolCall(name=name, arguments=args)])

        original = agents.llm.chat
        agents.llm.chat = fake_chat
        try:
            agents.run(self.agent["id"], "task", task_id=self.task_id)
        finally:
            agents.llm.chat = original
        return db.one("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC", (self.agent["id"],))

    def test_two_failed_attempts_reach_the_inbox(self):
        write = ("write_file", {"path": self.DOC, "content": "We help Phoenix homeowners find vetted roofers."})
        review = ("review_text", {"path": self.DOC, "text": "", "kind": "article"})
        run1 = self.run_script([write, review, review, review])
        self.assertEqual(run1["status"], "partial")
        t = db.get_task(self.task_id)
        self.assertEqual((t["status"], t["attempts"]), ("todo", 1))
        ents = watchdog.entries(t["notes"])
        self.assertEqual([e["author"] for e in ents], ["auto"])
        self.assertIn("Attempt 1 ended: partial", ents[0]["text"])
        self.assertIn("Files written: " + self.DOC, ents[0]["text"])
        self.assertIn("Last tool calls: review_text(", ents[0]["text"])
        self.assertEqual(questions(self.c["id"]), [])

        self.prompts.clear()
        run2 = self.run_script([write, review, review, review])
        self.assertEqual(run2["status"], "partial")
        prompt = self.prompts[0]
        self.assertIn("Working notes from earlier attempts", prompt)
        self.assertIn("Attempt 1 ended: partial", prompt)
        self.assertIn("Attempt 1 failed because it repeated the same action", prompt)
        self.assertIn("Do not repeat that approach: use a different tool", prompt)
        t = db.get_task(self.task_id)
        self.assertEqual((t["status"], t["attempts"]), ("blocked", 2))
        self.assertEqual([e["author"] for e in watchdog.entries(t["notes"])], ["auto", "auto"])
        qs = questions(self.c["id"], "task_failed_twice")
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["task_id"], self.task_id)
        self.assertIn("failed twice", qs[0]["p"]["question"])
        self.assertEqual(qs[0]["p"]["recommended"], "Skip this task")
        self.assertIn("Attempt 2 ended: partial", qs[0]["p"]["context"])
        # Pressing the recommended button from the inbox finishes the story.
        actions.decide(qs[0]["id"], "approve", answer="Skip this task")
        self.assertEqual(db.get_task(self.task_id)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
