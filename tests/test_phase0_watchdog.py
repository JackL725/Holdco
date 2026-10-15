"""Phase 0 watchdog tests: working notes, retry guidance, escalations, the owner's answers and the periodic tick.

Run all tests with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOLDCO_DATA"] = tempfile.mkdtemp(prefix="holdco-watchdog-")

from holdco import (actions, agents, backup, bus, db, doctor, llm, notify, roles, scheduler, tools,  # noqa: E402
                    watchdog, web, workspace)
from tests.fake_ollama import FakeModelServer  # noqa: E402

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


@contextlib.contextmanager
def scheduler_sandbox(started: list | None = None, found: list | None = None, refreshed: list | None = None):
    """A Scheduler whose tick has every side effect caught: no run starts, no founding restarts and the Doctor
    never runs; the lists record what the tick asked for."""
    started = started if started is not None else []
    found = found if found is not None else []
    refreshed = refreshed if refreshed is not None else []
    def refresh(max_age_minutes=60):
        refreshed.append(max_age_minutes)

    with mock.patch.object(agents, "start", lambda aid, kind, **kw: started.append((aid, kind)) or True), \
            mock.patch.object(agents, "found", lambda cid: found.append(cid) or True), \
            mock.patch.object(doctor, "refresh_if_stale", refresh), \
            mock.patch.object(watchdog, "tick", lambda: None), \
            mock.patch.object(backup, "maybe_run", lambda: None), \
            mock.patch.object(notify, "ensure_poller", lambda: None):
        yield scheduler.Scheduler()


def global_events(message_prefix: str, after_id: int) -> list[dict]:
    return db.q("SELECT * FROM events WHERE company_id IS NULL AND message LIKE ? AND id>? ORDER BY id",
                (message_prefix + "%", after_id))


def last_event_id() -> int:
    return db.scalar("SELECT COALESCE(MAX(id), 0) FROM events") or 0


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

    def test_a_stopped_attempt_does_not_hide_the_real_failure(self):
        # The kill switch (or a pause, or a model outage) stopped attempt 2: that is not the agent's failure, so
        # the guidance still comes from attempt 1, which really failed.
        notes = (auto_entry("Attempt 1 ended: partial\nLast tool calls: web_search({\"query\": \"x\"})\n"
                            "Last error: ERROR: web search is unavailable right now (duckduckgo: blocked)", 30)
                 + "\n\n" + auto_entry("Attempt 2 ended: stopped (Kill switch: the owner stopped all agents.)", 5))
        text = self.guidance(notes)
        self.assertTrue(text.startswith("Attempt 1 failed because web search was unavailable"), text)
        self.assertIn("fetch_url", text)
        # Only stopped entries: nothing to learn from, so the generic guidance applies.
        text = self.guidance(auto_entry("Attempt 1 ended: stopped (Paused by the owner.)"), attempts=2)
        self.assertIn("it did not finish", text)


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
        # Phase 0 fixes: the payload gained "key", the dedupe key, next to the plain pattern name (the UI badge).
        self.assertEqual(set(q["p"]), {"question", "options", "recommended", "context", "pattern", "key", "task_id",
                                       "company_id"})
        self.assertEqual(q["p"]["pattern"], "task_failed_twice")
        self.assertEqual(q["p"]["key"], f"task_failed_twice:{t['id']}")
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

    def test_retry_once_more_then_a_second_failure_asks_again(self):
        t, aid = self.failed_task()
        actions.decide(aid, "approve", answer="Retry once more with my note")
        self.assertEqual(db.get_task(t["id"])["attempts"], 0)
        self.assertTrue(db.loads(db.one("SELECT payload FROM approvals WHERE id=?", (aid,))["payload"])["retried"])
        # The extra attempt fails twice inside the 6 h quiet window: the owner must still hear about it.
        db.update("tasks", t["id"], attempts=2)
        watchdog.task_failed(db.get_task(t["id"]), "Ran out of steps before finishing.", "out_of_steps")
        qs = questions(self.c["id"], "task_failed_twice")
        self.assertEqual(len(qs), 2)
        self.assertEqual(qs[-1]["status"], "pending")
        self.assertEqual(qs[-1]["p"]["key"], f"task_failed_twice:{t['id']}")
        self.assertEqual(db.get_task(t["id"])["status"], "blocked")
        # Answered any other way, the quiet window applies as before.
        actions.decide(qs[-1]["id"], "approve", answer="Skip this task")
        db.update("tasks", t["id"], status="todo", attempts=2)
        watchdog.task_failed(db.get_task(t["id"]), "Ran out of steps before finishing.", "out_of_steps")
        self.assertEqual(len(questions(self.c["id"], "task_failed_twice")), 2)

    def test_continue_anyway_writes_the_memory_note(self):
        db.update("companies", self.c["id"], charter="# Charter")
        watchdog.founding_ended(db.get_company(self.c["id"]), "partial", "")   # the company is already active
        aid = questions(self.c["id"], "founding_partial")[0]["id"]
        actions.decide(aid, "approve", answer="Continue anyway")
        self.assertEqual(db.get_company(self.c["id"])["status"], "active")
        note = db.one("SELECT * FROM memory WHERE company_id=? AND content='Founding accepted as is by the owner'",
                      (self.c["id"],))
        self.assertTrue(note)
        self.assertEqual(note["kind"], "decision")

    def test_retry_founding_with_a_busy_ceo_keeps_the_company_active(self):
        watchdog.founding_ended(db.get_company(self.c["id"]), "partial", "")
        aid = questions(self.c["id"], "founding_partial")[0]["id"]
        ceo = db.one("SELECT id FROM agents WHERE company_id=? AND role='ceo'", (self.c["id"],))
        with agents._running_lock:
            agents._running[ceo["id"]] = "cycle"   # the CEO is mid-run, so no founding can start now
        try:
            actions.decide(aid, "approve", answer="Retry founding")
        finally:
            with agents._running_lock:
                agents._running.pop(ceo["id"], None)
        c = db.get_company(self.c["id"])
        self.assertEqual(c["status"], "active")   # not stuck in "founding" with nothing running
        self.assertEqual(c["cycle_requested"], 1)
        self.assertTrue(db.one("SELECT id FROM events WHERE company_id=? AND message LIKE 'The CEO is busy%'",
                               (self.c["id"],)))
        self.assertEqual(db.q("SELECT id FROM runs WHERE company_id=? AND kind='founding'", (self.c["id"],)), [])

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
        web.SEARCH_FAIL_STREAK = 0

    def tearDown(self):
        doctor.LAST = self.saved_last
        watchdog._bad_streak.clear()
        watchdog._last_connector_alert.clear()
        watchdog._last_doctor_seen = None
        web.SEARCH_FAIL_STREAK = 0

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

    def test_search_outage_asks_each_searching_company_once_a_day(self):
        other = make_company("QuietCo")
        bus.emit(self.c["id"], "tool", "searched the web for “dog food prices”")
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "connector_down"), [])   # nothing has failed yet
        web.SEARCH_FAIL_STREAK = 1
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "connector_down"), [])   # one failed search may be a blip
        web.SEARCH_FAIL_STREAK = 2
        watchdog.tick()
        watchdog.tick()
        qs = questions(self.c["id"], "connector_down")
        self.assertEqual(len(qs), 1)
        p = qs[0]["p"]
        self.assertEqual(p["pattern"], "connector_down")
        self.assertEqual(p["key"], "connector_down:search")
        self.assertEqual(p["options"], ["Keep going without it", "Pause the company"])
        self.assertEqual(p["recommended"], "Keep going without it")
        self.assertRegex(p["question"],
                         r"^Web search for WatchCo has failed on every provider \(.+\)\. What should we do\?$")
        self.assertIn("ddg_html:", p["question"])   # the reasons come from web.PROVIDER_STATUS, provider by provider
        self.assertIn("Add a search key", p["context"])
        self.assertEqual(questions(other["id"], "connector_down"), [])   # QuietCo never searched
        # Answering does not bring it back the same day, even once the 6 h quiet window has passed.
        actions.decide(qs[0]["id"], "approve", answer="Keep going without it")
        db.update("approvals", qs[0]["id"], decided_at=ago(7 * 60))
        watchdog.tick()
        self.assertEqual(len(questions(self.c["id"], "connector_down")), 1)
        # A company that searched itself is asked too (QuietCo did, now).
        watchdog._last_connector_alert.clear()
        bus.emit(other["id"], "tool", "searched the web for “cafe pantry software”")
        watchdog.tick()
        self.assertEqual(len(questions(other["id"], "connector_down")), 1)
        # When no company searched in the last day, every active company is asked. (Other test modules leave
        # search events in the shared database, so every one of them is aged out, not only this company's.)
        third = make_company("NeverSearchedCo")
        watchdog._last_connector_alert.clear()
        db.run("UPDATE events SET created_at=? WHERE kind='tool' AND message LIKE 'searched the web%'",
               (ago(25 * 60),))
        watchdog.tick()
        self.assertEqual(len(questions(third["id"], "connector_down")), 1)
        # A working search clears the counter and the next day starts clean.
        web.SEARCH_FAIL_STREAK = 0
        watchdog._last_connector_alert.clear()
        watchdog.tick()
        self.assertEqual(len(questions(other["id"], "connector_down")), 1)

    def test_search_outage_from_two_bad_doctor_runs(self):
        def rows(status, checked):
            return [{"key": "model_server", "label": "Model server", "status": "ok", "detail": "", "fix": None,
                     "checked_at": checked},
                    {"key": "search", "label": "Web search", "status": status, "fix": None, "checked_at": checked,
                     "detail": "Every search provider failed its last try: ddg_html: blocked (HTTP 202 challenge)"}]
        bus.emit(self.c["id"], "tool", "searched the web for “x”")
        doctor.LAST = rows("bad", "2026-10-14T10:00:00Z")
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "connector_down"), [])
        doctor.LAST = rows("ok", "2026-10-14T11:00:00Z")
        watchdog.tick()
        doctor.LAST = rows("bad", "2026-10-14T12:00:00Z")
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "connector_down"), [])   # the ok run reset the streak
        doctor.LAST = rows("bad", "2026-10-14T13:00:00Z")
        watchdog.tick()
        watchdog.tick()
        qs = questions(self.c["id"], "connector_down")
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["p"]["key"], "connector_down:search")
        self.assertIn("Web search for WatchCo has failed on every provider", qs[0]["p"]["question"])

    def test_one_model_outage_is_one_question(self):
        add_task(self.c, "Anything")
        for _ in range(3):
            bus.emit(self.c["id"], "error", "Ava hit an error: Can't reach the model server at "
                     "http://127.0.0.1:11434/api/chat ([Errno 111] Connection refused). Is Ollama running?")
        bus.emit(self.c["id"], "error", "Ben hit an error: The model server dropped the connection "
                 "(RemoteDisconnected: Remote end closed connection without response).")
        bus.emit(self.c["id"], "error", "Ben hit an error: The model server took longer than 600.0s to reply.")
        bus.emit(self.c["id"], "error", "Ben hit an error: HTTP 503 from model server: overloaded")
        doctor.LAST = [{"key": "model_server", "label": "Model server", "status": "bad",
                        "detail": "connection refused on 127.0.0.1:11434", "fix": None, "checked_at": db.now()}]
        db.run("INSERT INTO runs(company_id, agent_id, kind, status, started_at) VALUES(?,?,?,?,?)",
               (self.c["id"], None, "task", "error", ago(3 * 60)))
        watchdog.tick()
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "repeated_error"), [])   # connection errors belong to "stalled"
        stalled = questions(self.c["id"], "stalled")
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0]["p"]["key"], f"stalled:{self.c['id']}")
        self.assertEqual(len(questions(self.c["id"])), 1)
        # Even with the model server reported fine, connection errors never count as a repeated error.
        doctor.LAST = []
        watchdog.tick()
        self.assertEqual(questions(self.c["id"], "repeated_error"), [])

    def test_a_second_distinct_repeating_error_gets_its_own_question(self):
        for n in range(3):
            bus.emit(self.c["id"], "error", f"Ava hit an error: Timed out after {600 + n}s on task #{n}")
        watchdog.tick()
        first = questions(self.c["id"], "repeated_error")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["p"]["pattern"], "repeated_error")
        self.assertEqual(first[0]["p"]["key"],
                         "repeated_error:" + watchdog.error_hash("Timed out after 600s on task #1"))
        actions.decide(first[0]["id"], "approve", answer="Keep going")
        for _ in range(3):
            bus.emit(self.c["id"], "error", "Ava hit an error: Webhook returned HTTP 500: boom")
        bus.emit(self.c["id"], "error", "Ava hit an error: Timed out after 603s on task #9")   # the newest error
        watchdog.tick()
        qs = questions(self.c["id"], "repeated_error")
        self.assertEqual(len(qs), 2)   # the answered timeout question does not hide the webhook problem
        self.assertNotEqual(qs[0]["p"]["key"], qs[1]["p"]["key"])
        self.assertIn("Webhook returned HTTP 500", qs[1]["p"]["question"])
        watchdog.tick()
        self.assertEqual(len(questions(self.c["id"], "repeated_error")), 2)   # neither is asked twice

    def test_task_bouncing_does_not_touch_a_task_in_progress(self):
        running = add_task(self.c, "Write FAQ", role="marketing", status="in_progress", reassigned=3)
        queued = add_task(self.c, "Write terms", role="marketing", status="todo", reassigned=3)
        watchdog.tick()
        # The running task keeps its status, so agents._finalize can still record how that run ended.
        self.assertEqual(db.get_task(running["id"])["status"], "in_progress")
        self.assertEqual(db.get_task(queued["id"])["status"], "blocked")
        qs = questions(self.c["id"], "task_bouncing")
        self.assertEqual({q["task_id"] for q in qs}, {running["id"], queued["id"]})
        self.assertEqual({q["p"]["key"] for q in qs},
                         {f"task_bouncing:{running['id']}", f"task_bouncing:{queued['id']}"})
        watchdog.tick()
        self.assertEqual(len(questions(self.c["id"], "task_bouncing")), 2)


class TestModelOutage(unittest.TestCase):
    """A model server that cannot be reached neither burns task attempts nor floods the owner: the attempt is
    given back and the scheduler waits for the Doctor to see the server again."""

    def setUp(self):
        self.c = make_company()
        db.set_settings({"max_task_attempts": 2, "scheduler_enabled": True, "kill_switch": False})
        self.agent = db.one("SELECT * FROM agents WHERE company_id=? AND role='marketing'", (self.c["id"],))
        self.task = add_task(self.c, "Draft positioning", role="marketing", attempts=0)
        self.saved_last = doctor.LAST
        doctor.LAST = []

    def tearDown(self):
        doctor.LAST = self.saved_last

    def run_failing(self, text: str) -> dict:
        def fake_chat(*a, **kw):
            raise llm.LLMError(text)

        with mock.patch.object(agents.llm, "chat", fake_chat):
            agents.run(self.agent["id"], "task", task_id=self.task["id"])
        return db.one("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC", (self.agent["id"],))

    def test_connection_errors_give_the_attempt_back(self):
        texts = ("Can't reach the model server at http://127.0.0.1:11434/api/chat ([Errno 111] Connection refused). "
                 "Is Ollama running?",
                 "The model server dropped the connection (RemoteDisconnected: Remote end closed connection).",
                 "The model server took longer than 600.0s to reply.",
                 "HTTP 502 from model server: bad gateway")
        for text in texts:
            run = self.run_failing(text)
            self.assertEqual(run["status"], "error")
            t = db.get_task(self.task["id"])
            self.assertEqual((t["status"], t["attempts"]), ("todo", 0), text)
        self.assertEqual(questions(self.c["id"]), [])   # no task_failed_twice: the watchdog was not told
        ents = watchdog.entries(db.get_task(self.task["id"])["notes"])
        self.assertEqual(len(ents), 4)
        for e in ents:
            self.assertTrue(e["text"].startswith("Attempt 1 ended: stopped (the model server could not be reached"),
                            e["text"])
            self.assertNotIn("\n", e["text"])   # one short line, nothing more
        self.assertFalse("Run ended:" in (db.get_task(self.task["id"])["result"] or ""))
        # The errors still reach the feed, where the stalled pattern and the Doctor count them.
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM events WHERE company_id=? AND kind='error' AND message "
                                   "LIKE '%hit an error: %'", (self.c["id"],)), 4)
        # A real model error still counts as a failed attempt, and the guidance skips the stopped entries.
        run = self.run_failing("HTTP 400 from model server: bad request")
        self.assertEqual(run["status"], "error")
        t = db.get_task(self.task["id"])
        self.assertEqual((t["status"], t["attempts"]), ("todo", 1))
        self.assertEqual(watchdog.entries(t["notes"])[-1]["text"],
                         "Attempt 1 ended: error (HTTP 400 from model server: bad request)")
        self.assertIn("Attempt 1 failed because the model call failed (HTTP 400", watchdog.retry_guidance(t))

    def test_is_model_connection_error(self):
        for text in ("Can't reach the model server at http://x (refused). Is Ollama running?",
                     "The model server dropped the connection (ConnectionResetError: reset).",
                     "The model server took longer than 600s to reply.", "HTTP 503 from model server: busy",
                     "Ava hit an error: HTTP 500 from model server: internal"):
            self.assertTrue(watchdog.is_model_connection_error(text), text)
        for text in ("HTTP 400 from model server: bad request", "HTTP 404 from model server: model not found",
                     "HTTP 500 from model server: error parsing tool call: invalid JSON",
                     "Bad response from model server: Expecting value", "", None):
            self.assertFalse(watchdog.is_model_connection_error(text), text)

    def test_scheduler_waits_for_the_model_server(self):
        # The newest run is a clean one, so nothing nudges the Doctor beyond the outage logic under test.
        db.run("INSERT INTO runs(company_id, agent_id, kind, status, started_at, finished_at) VALUES(?,?,?,?,?,?)",
               (self.c["id"], None, "task", "done", db.now(), db.now()))
        started, refreshed = [], []
        before = last_event_id()
        with scheduler_sandbox(started, refreshed=refreshed) as sched:
            doctor.LAST = [{"key": "model_server", "label": "Model server", "status": "bad",
                            "detail": "connection refused", "fix": None, "checked_at": db.now()}]
            sched.tick()
            sched.tick()
            self.assertEqual([s for s in started if s[0] == self.agent["id"]], [])
            self.assertEqual([m for m in refreshed if m != 60], [5, 5])   # the Doctor looks again every 5 min
            self.assertEqual(len(global_events("Waiting for the model server", before)), 1)   # one notice per outage
            self.assertEqual(global_events("The model server is back", before), [])
            doctor.LAST = [{"key": "model_server", "label": "Model server", "status": "ok",
                            "detail": "reachable", "fix": None, "checked_at": db.now()}]
            sched.tick()
            sched.tick()
            self.assertIn((self.agent["id"], "task"), started)
            self.assertEqual(len(global_events("The model server is back", before)), 1)
            self.assertEqual(len(global_events("Waiting for the model server", before)), 1)

    def test_a_fresh_connection_failure_asks_the_doctor_to_look_now(self):
        self.run_failing("Can't reach the model server at http://127.0.0.1:1 (refused). Is Ollama running?")
        refreshed = []
        with scheduler_sandbox(refreshed=refreshed) as sched:
            sched.tick()
        self.assertIn(1, refreshed)   # the hourly check would notice the outage far too late


class TestFoundingRuns(unittest.TestCase):
    """Founding outcomes through agents.run: the charter sync, the partial question, the phone hook and the kill
    switch.

    Phase 0.5 stages founding (research, charter, critic, plan; see tests/test_planning.py). The outcomes pinned
    here (company active, the founding_partial question, the phone notice) happen when the last stage ends, so
    each run here is the plan stage."""

    CHARTER = ("# FoundCo\n\n## One-liner\n**Weekly** meal plans for [dogs](https://example.com).\n\n"
               "## Problem\nOwners guess.\n")

    def setUp(self):
        self.c = make_company("FoundCo", members=("ceo", "engineering"))
        db.update("companies", self.c["id"], status="founding", stage="idea", charter="", mission="",
                  settings=json.dumps({"founding_stage": "plan"}))
        db.set_settings({"kill_switch": False, "scheduler_enabled": True})
        self.ceo = db.one("SELECT * FROM agents WHERE company_id=? AND role='ceo'", (self.c["id"],))
        self.sent: list = []
        self._notify = mock.patch.object(notify, "send", lambda text, buttons=None, kind="info", **kw:
                                         self.sent.append((text, kind)) or True)
        self._notify.start()
        # Phone hooks only queue messages when a phone is set up, and send them on notify's sender thread.
        self._configured = mock.patch.object(notify, "configured", lambda *a, **kw: True)
        self._configured.start()
        self.saved_last = doctor.LAST
        doctor.LAST = []

    def tearDown(self):
        notify.flush()
        self._configured.stop()
        self._notify.stop()
        db.set_settings({"kill_switch": False})
        doctor.LAST = self.saved_last

    def run_script(self, script) -> dict:
        steps = iter(script)

        def fake_chat(messages, tools_spec=None, *a, **kw):
            try:
                name, args = next(steps)
            except StopIteration:
                return llm.Reply(content="")
            return llm.Reply(tool_calls=[llm.ToolCall(name=name, arguments=args)])

        with mock.patch.object(agents.llm, "chat", fake_chat):
            agents.run(self.ceo["id"], "founding")
        notify.flush()   # phone messages are sent on notify's sender thread
        return db.one("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC", (self.ceo["id"],))

    def test_charter_file_becomes_the_charter_and_the_question_sees_it(self):
        # Phase 0.5: the file is already on disk (written outside update_charter, for example before the upgrade).
        # The plan stage has no write_file, and write_file now refuses a charter that misses required sections.
        workspace.safe_path(self.c, "docs/charter.md").write_text(self.CHARTER, encoding="utf-8")
        run = self.run_script([("finish", {"summary": "Charter drafted; no tasks yet.", "status": "partial"})])
        self.assertEqual(run["status"], "partial")
        c = db.get_company(self.c["id"])
        self.assertEqual(c["status"], "active")
        self.assertEqual(c["charter"], self.CHARTER.strip())
        self.assertEqual(c["mission"], "Weekly meal plans for dogs.")
        self.assertEqual(c["stage"], "building")
        self.assertTrue(db.one("SELECT id FROM events WHERE company_id=? AND kind='charter' AND message LIKE "
                               "'took the charter from docs/charter.md%'", (self.c["id"],)))
        qs = questions(self.c["id"], "founding_partial")
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["p"]["key"], f"founding_partial:{self.c['id']}")
        self.assertEqual(qs[0]["p"]["recommended"], "Continue anyway")
        self.assertIn("A charter exists", qs[0]["p"]["question"])
        self.assertIn("it finished only partially", qs[0]["p"]["question"])
        # The question itself went to the phone through gate (with buttons), then the founding notice.
        self.assertEqual([s[1] for s in self.sent], ["escalation", "important"])
        self.assertEqual(self.sent[-1], ("<b>FoundCo</b>: founding ended partial; see your inbox.", "important"))

    def test_update_charter_wins_and_a_clean_founding_reaches_the_phone(self):
        # Phase 0.5: update_charter belongs to the charter stage, which saved this charter to the record; an older
        # draft in the file must not replace it.
        db.update("companies", self.c["id"], charter="# Real charter", mission="Real one-liner", stage="building")
        workspace.safe_path(self.c, "docs/charter.md").write_text("# Older draft", encoding="utf-8")
        run = self.run_script([("create_task", {"title": "Build landing page", "assign_to": "engineering",
                                                "deliverable": "site/index.html"}),
                               ("finish", {"summary": "Founded."})])
        self.assertEqual(run["status"], "done")
        c = db.get_company(self.c["id"])
        self.assertEqual((c["status"], c["charter"], c["mission"]), ("active", "# Real charter", "Real one-liner"))
        self.assertEqual(questions(self.c["id"]), [])
        self.assertEqual(self.sent, [("<b>FoundCo</b> is up and running: 1 task queued.", "important")])

    def test_phone_problems_never_touch_the_run(self):
        def boom(*a, **kw):
            raise RuntimeError("phone exploded")

        with mock.patch.object(notify, "send", boom), contextlib.redirect_stdout(io.StringIO()):
            run = self.run_script([("finish", {"summary": "Founded.", "status": "partial"})])
        self.assertEqual(run["status"], "partial")
        self.assertEqual(db.get_company(self.c["id"])["status"], "active")
        self.assertEqual(len(questions(self.c["id"], "founding_partial")), 1)

    def test_founding_stopped_by_the_kill_switch_resumes_on_the_next_tick(self):
        db.set_settings({"kill_switch": True})
        run = self.run_script([("finish", {"summary": "never reached"})])
        self.assertEqual(run["status"], "stopped")
        self.assertEqual(db.get_company(self.c["id"])["status"], "founding")
        self.assertTrue(db.one("SELECT id FROM events WHERE company_id=? AND message LIKE "
                               "'Founding paused by the kill switch%'", (self.c["id"],)))
        self.assertEqual(self.sent, [])
        found: list = []
        with scheduler_sandbox(found=found) as sched:
            sched.tick()   # the switch is still on: nothing moves
            self.assertEqual([x for x in found if x == self.c["id"]], [])
            db.set_settings({"kill_switch": False})
            sched.tick()
            self.assertEqual([x for x in found if x == self.c["id"]], [self.c["id"]])
            # A founding run in flight, or one that ended in an ordinary error, is left alone.
            db.run("UPDATE runs SET status='running' WHERE id=?", (run["id"],))
            sched.tick()
            db.run("UPDATE runs SET status='error', summary='HTTP 400 from model server: bad request' WHERE id=?",
                   (run["id"],))
            sched.tick()
            self.assertEqual([x for x in found if x == self.c["id"]], [self.c["id"]])
            # One that could not reach the model is retried once the Doctor says the server answers.
            db.run("UPDATE runs SET summary=? WHERE id=?",
                   ("Can't reach the model server at http://127.0.0.1:1 (refused). Is Ollama running?", run["id"]))
            sched.tick()
            self.assertEqual([x for x in found if x == self.c["id"]], [self.c["id"]] * 2)
            # ... but not while the model server is down.
            doctor.LAST = [{"key": "model_server", "label": "Model server", "status": "bad", "detail": "refused",
                            "fix": None, "checked_at": db.now()}]
            sched.tick()
            self.assertEqual([x for x in found if x == self.c["id"]], [self.c["id"]] * 2)

    def test_found_leaves_the_status_alone_when_the_ceo_is_busy(self):
        db.update("companies", self.c["id"], status="active")
        with agents._running_lock:
            agents._running[self.ceo["id"]] = "cycle"
        try:
            self.assertFalse(agents.found(self.c["id"]))
        finally:
            with agents._running_lock:
                agents._running.pop(self.ceo["id"], None)
        self.assertEqual(db.get_company(self.c["id"])["status"], "active")


class TestFoundingWithTheFakeModel(unittest.TestCase):
    """The scripted "[partial]" founding in tests/fake_ollama.py drives agents.run end to end: the CEO writes
    docs/charter.md with write_file, never calls update_charter, creates no tasks and finishes partial."""

    @classmethod
    def setUpClass(cls):
        cls.model = FakeModelServer(delay=0).start()
        cls.saved = {k: db.get_settings().get(k) for k in ("llm_backend", "llm_base_url", "default_model")}
        db.set_settings({"llm_backend": "ollama", "llm_base_url": cls.model.url, "default_model": "gpt-oss:20b",
                         "kill_switch": False})

    @classmethod
    def tearDownClass(cls):
        db.set_settings(cls.saved)
        cls.model.stop()

    def test_partial_founding_reaches_founding_ended(self):
        sent: list = []
        with mock.patch.object(notify, "send", lambda text, buttons=None, kind="info", **kw:
                               sent.append((text, kind)) or True), \
                mock.patch.object(notify, "configured", lambda *a, **kw: True):
            c = agents.create_company("Weekly meal plans for dogs [partial]", name="PawPlan", template="minimal")
            ceo = db.one("SELECT id FROM agents WHERE company_id=? AND role='ceo'", (c["id"],))
            deadline = time.time() + 30
            while time.time() < deadline and (db.get_company(c["id"])["status"] == "founding"
                                               or agents.is_running(ceo["id"])):
                time.sleep(0.1)
            notify.flush()   # phone messages are sent on notify's sender thread
        c = db.get_company(c["id"])
        self.assertEqual(c["status"], "active")
        run = db.one("SELECT * FROM runs WHERE company_id=? AND kind='founding'", (c["id"],))
        self.assertEqual(run["status"], "partial")
        self.assertTrue(c["charter"].startswith("# PawPlan"), c["charter"])
        self.assertEqual(c["mission"], "Weekly meal plans for dogs.")
        self.assertEqual(c["stage"], "building")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=?", (c["id"],)), 0)
        self.assertFalse(db.one("SELECT id FROM events WHERE company_id=? AND kind='charter' AND message LIKE "
                                "'updated the company charter%'", (c["id"],)))   # update_charter was never called
        qs = questions(c["id"], "founding_partial")
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["status"], "pending")
        self.assertEqual(qs[0]["p"]["recommended"], "Continue anyway")
        self.assertIn("A charter exists", qs[0]["p"]["question"])
        self.assertEqual([s[1] for s in sent], ["escalation", "important"])   # one question, one founding notice
        self.assertIn("Founding PawPlan stopped before it finished", sent[0][0])
        self.assertEqual(sent[-1], ("<b>PawPlan</b>: founding ended partial; see your inbox.", "important"))


class TestCharterSync(unittest.TestCase):
    def test_mission_from_charter(self):
        self.assertEqual(agents.mission_from_charter("# X\n\n## One-liner\n\n*Weekly* meal plans for `dogs`.\n\n"
                                                     "## Problem"), "Weekly meal plans for dogs.")
        self.assertEqual(agents.mission_from_charter("**One-liner:** Meal plans for dogs"), "Meal plans for dogs")
        self.assertEqual(agents.mission_from_charter("One-liner: Meal [plans](http://x) for dogs"),
                         "Meal plans for dogs")
        self.assertEqual(agents.mission_from_charter("- One liner: Plans"), "Plans")
        self.assertEqual(agents.mission_from_charter("## One-liner\n\n## Problem\nnothing"), "")
        self.assertEqual(agents.mission_from_charter("no heading here"), "")
        self.assertEqual(agents.mission_from_charter(""), "")

    def test_sync_only_fills_empty_columns(self):
        c = make_company("SyncCo")
        db.update("companies", c["id"], charter="", mission="", stage="idea")
        self.assertFalse(agents._sync_charter(db.get_company(c["id"])))   # no file yet
        workspace.safe_path(c, "docs/charter.md").write_text("# SyncCo\n\n## One-liner\nPlans.\n", encoding="utf-8")
        self.assertTrue(agents._sync_charter(db.get_company(c["id"])))
        c2 = db.get_company(c["id"])
        self.assertEqual((c2["charter"], c2["mission"], c2["stage"]),
                         ("# SyncCo\n\n## One-liner\nPlans.", "Plans.", "building"))
        # A charter already in the column is never replaced by the file.
        workspace.safe_path(c, "docs/charter.md").write_text("# Changed", encoding="utf-8")
        self.assertFalse(agents._sync_charter(db.get_company(c["id"])))
        self.assertEqual(db.get_company(c["id"])["charter"], "# SyncCo\n\n## One-liner\nPlans.")
        # A one-liner the owner set and a later stage stay as they are.
        d = make_company("KeepCo")
        db.update("companies", d["id"], charter="", mission="Owner's words", stage="launched")
        workspace.safe_path(d, "docs/charter.md").write_text("## One-liner\nFile words", encoding="utf-8")
        self.assertTrue(agents._sync_charter(db.get_company(d["id"])))
        d2 = db.get_company(d["id"])
        self.assertEqual((d2["charter"], d2["mission"], d2["stage"]), ("## One-liner\nFile words", "Owner's words",
                                                                        "launched"))


class TestPromptRules(unittest.TestCase):
    def test_note_progress_rule_only_in_task_runs(self):
        c = make_company()
        agent = db.one("SELECT * FROM agents WHERE company_id=? AND role='engineering'", (c["id"],))
        line = ("If an approach fails or you are running low on steps, save what you learned with note_progress so "
                "the next attempt continues from it.")
        self.assertIn("- " + line, agents.system_prompt(c, agent, "task", 14))
        self.assertNotIn(line, agents.system_prompt(c, agent, "cycle", 18))
        self.assertNotIn(line, agents.system_prompt(c, agent, "chat", 8))


class TestKillSwitchGate(unittest.TestCase):
    def setUp(self):
        c = make_company()
        db.update("companies", c["id"], settings=json.dumps({"policies": {"social_post": "auto"}}))
        self.c = db.get_company(c["id"])
        db.set_settings({"kill_switch": True})

    def tearDown(self):
        db.set_settings({"kill_switch": False})

    def test_gate_queues_auto_actions_with_the_kill_switch_text(self):
        ctx = ctx_for(self.c, "marketing")
        out = tools.execute(ctx, "social_post", {"platform": "x", "content": "Hello while stopped"})
        m = re.match(r"^Kill switch is on: queued for the owner as request #(\d+)\. Don't resubmit it\. Continue "
                     r"with other work; the decision will appear in your notes\.$", out)
        self.assertTrue(m, out)
        aid = int(m.group(1))
        row = db.one("SELECT * FROM approvals WHERE id=?", (aid,))
        self.assertEqual((row["status"], row["action"]), ("pending", "social_post"))
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM audit WHERE company_id=?", (self.c["id"],)), 0)  # nothing ran
        with self.assertRaises(ValueError):
            actions.decide(aid, "approve")
        self.assertEqual(actions.decide(aid, "reject")["status"], "rejected")
        # Normal policy text is untouched: with the switch off an auto action runs at once.
        db.set_settings({"kill_switch": False})
        self.assertTrue(tools.execute(ctx, "social_post", {"platform": "x", "content": "Hello again"})
                        .startswith("Approved"))
        # ... and an approve-policy action keeps the usual wording even while the switch is on.
        db.set_settings({"kill_switch": True})
        out = actions.gate(self.c, ctx.agent, None, "send_email", {"to": "jo@example.com", "subject": "Hi",
                                                                   "body": "Hello"})
        self.assertIn("Queued for the owner's approval", out)
        self.assertNotIn("Kill switch", out)


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

    def test_out_of_steps_leaves_a_note_with_the_last_error(self):
        search = ("web_search", {"query": "roofers phoenix"})
        write = ("write_file", {"path": self.DOC, "content": "draft"})
        db.set_settings({"max_steps_per_run": 3})
        try:
            with mock.patch.object(web, "search", side_effect=web.WebError(
                    "All search providers failed: ddg_html: blocked (HTTP 202 challenge); brave: not set up")):
                run = self.run_script([search, write, ("read_file", {"path": self.DOC}), write])
        finally:
            db.set_settings({"max_steps_per_run": 14})
        self.assertEqual(run["status"], "out_of_steps")
        t = db.get_task(self.task_id)
        self.assertEqual((t["status"], t["attempts"]), ("todo", 1))
        ents = watchdog.entries(t["notes"])
        self.assertEqual([e["author"] for e in ents], ["auto"])
        lines = ents[0]["text"].splitlines()
        self.assertEqual(lines[0], "Attempt 1 ended: out_of_steps")
        self.assertIn("Files written: " + self.DOC, lines)
        self.assertTrue(any(ln.startswith("Last tool calls: web_search(") for ln in lines), lines)
        self.assertTrue(any(ln.startswith("Last error: ERROR: web search is unavailable right now (ddg_html: blocked")
                            for ln in lines), lines)
        self.assertEqual(watchdog.classify_failure(t)[0], "search_failed")
        self.assertIn("web search was unavailable", watchdog.retry_guidance(t))
        self.assertEqual(questions(self.c["id"]), [])

    def test_model_error_leaves_a_note_and_counts_the_attempt(self):
        def fake_chat(*a, **kw):
            raise llm.LLMError("HTTP 400 from model server: invalid request")

        with mock.patch.object(agents.llm, "chat", fake_chat):
            agents.run(self.agent["id"], "task", task_id=self.task_id)
        run = db.one("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC", (self.agent["id"],))
        self.assertEqual((run["status"], run["summary"]), ("error", "HTTP 400 from model server: invalid request"))
        t = db.get_task(self.task_id)
        self.assertEqual((t["status"], t["attempts"]), ("todo", 1))
        self.assertEqual(watchdog.entries(t["notes"])[-1]["text"],
                         "Attempt 1 ended: error (HTTP 400 from model server: invalid request)")
        self.assertEqual(watchdog.classify_failure(t), ("model_error", "HTTP 400 from model server: invalid request"))
        self.assertIn("Run ended: HTTP 400", t["result"])
        self.assertTrue(db.one("SELECT id FROM events WHERE company_id=? AND kind='error' AND message LIKE "
                               "'%hit an error: HTTP 400 from model server%'", (self.c["id"],)))

    def test_kill_switch_stops_the_run_and_gives_the_attempt_back(self):
        write = ("write_file", {"path": self.DOC, "content": "draft"})
        db.set_settings({"kill_switch": True})
        try:
            run = self.run_script([write, write])
        finally:
            db.set_settings({"kill_switch": False})
        self.assertEqual((run["status"], run["summary"]), ("stopped", "Kill switch: the owner stopped all agents."))
        self.assertEqual(self.prompts, [])   # stopped at the first step boundary, before any model call
        t = db.get_task(self.task_id)
        self.assertEqual((t["status"], t["attempts"]), ("todo", 0))
        self.assertEqual(watchdog.entries(t["notes"])[-1]["text"],
                         "Attempt 1 ended: stopped (Kill switch: the owner stopped all agents.)")
        self.assertEqual(questions(self.c["id"]), [])


if __name__ == "__main__":
    unittest.main()
