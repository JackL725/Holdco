"""Keeps every active company working around the clock.

Every few seconds it: lets each CEO re-plan when a cycle is due, hands queued tasks to idle agents
(respecting the GPU concurrency limit), and checks company inboxes.

Phase 3 (docs/phases/phase-3-verify.md section 8): queued tasks go out by class, then priority and id across
companies: 1 follow-ups (the owner's answer reopened them within the hour), 2 due within a day, 3 priority 1,
4 normal, 5 routines, 6 low-value research (research or discovery, priority 3, no deadline), which runs only inside
the company's quiet_hours window (local time) unless nothing in classes 1 to 5 could start. Classes 1 and 2 go out
before the CEO cycles; the rest after them, as before.

Phase 4 (docs/phases/phase-4-scale.md section 2): inside classes 3 to 6 portfolio.order puts the company furthest
below its share of the GPU first, and a company with work and no run within its cycle_minutes before that.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import threading
import time
import traceback

from . import (agents, backup, bench, bus, config, contracts, db, decisions, doctor, emailer, notify, planning,
               portfolio, rhythms, roles, watchdog)

TICK_SECONDS = float(os.environ.get("HOLDCO_TICK_SECONDS", 8))
ROUTINE_CHECK_SECONDS = float(os.environ.get("HOLDCO_ROUTINE_CHECK_SECONDS", 60))
INBOX_MINUTES = 5
MODEL_RECHECK_MINUTES = 5      # while the model server is down, the Doctor is asked to look again this often
MODEL_WAIT_TEXT = ("Waiting for the model server: it is unreachable, so tasks and planning are on hold. The health "
                   "checks retry every few minutes.")
MODEL_BACK_TEXT = "The model server is back. Tasks and planning resume."
# Phase 3: dispatch classes
FOLLOWUP_SECONDS = 3600        # a task the owner's answer reopened is a follow-up (class 1) for this long
DUE_SOON_HOURS = 24            # a task due within this many hours (or overdue) is class 2
URGENT_CLASSES = (1, 2)        # dispatched before the CEO cycles
RESEARCH_CLASS = 6             # low-value research: inside the quiet window, or when nothing else can start
QUEUE_LIMIT = 200              # queued tasks looked at per tick
# The class of a todo task, in SQL so the tick's LIMIT keeps the most urgent ones; first match wins.
_CLASS_SQL = ("CASE WHEN COALESCE(t.followup_at, '')>=? THEN 1 "
              "WHEN COALESCE(t.due_at, '')!='' AND t.due_at<=? THEN 2 "
              "WHEN t.priority<=1 THEN 3 "
              "WHEN COALESCE(t.created_by, '')='routine' THEN 5 "
              "WHEN (t.role IN ('research', 'discovery') OR t.role GLOB 'research_[0-9]*' OR "
              "t.role GLOB 'discovery_[0-9]*') AND t.priority>=3 AND COALESCE(t.due_at, '')='' THEN 6 "
              "ELSE 4 END")


def _seconds_ago(n: float) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_now() -> _dt.datetime:
    """The PC's local time: routines with an at_hour follow the owner's day. Tests patch this function, and
    HOLDCO_TEST_LOCAL_NOW fixes it for end-to-end runs (a time of day such as 19:00, or an ISO date and time)."""
    fixed = os.environ.get("HOLDCO_TEST_LOCAL_NOW", "").strip()
    if fixed:
        try:
            return _dt.datetime.fromisoformat(fixed.replace("Z", "+00:00"))
        except ValueError:
            try:
                return _dt.datetime.combine(_dt.date.today(), _dt.time.fromisoformat(fixed))
            except ValueError:
                pass
    return _dt.datetime.now()


def _at_hour_due(c: dict, key: str, r: dict) -> bool:
    """Phase 2: a routine with an at_hour is due once local time has passed that hour today and no task for it
    was created since then. For a routine slower than daily (the weekly review) "since then" reaches back to that
    hour on the day the previous one was due, so it comes every_hours apart at the same hour.

    The local clock only says how long ago that hour was; task times are compared with the database clock, so a
    fixed test clock cannot make a routine come again on every check."""
    now = _local_now()
    at = now.replace(hour=int(r["at_hour"]) % 24, minute=0, second=0, microsecond=0)
    if now < at:
        return False
    window = (now - at) + _dt.timedelta(hours=max(0.0, float(r.get("every_hours") or 24) - 24))
    return not db.one("SELECT id FROM tasks WHERE company_id=? AND routine=? AND created_at>=? LIMIT 1",
                      (c["id"], key, _seconds_ago(window.total_seconds())))


def _window(text) -> tuple[int, int] | None:
    """'23:00-07:00' as (start, end) minutes after midnight, or None when it is not two times."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", str(text or ""))
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if h1 > 24 or h2 > 24 or m1 > 59 or m2 > 59:
        return None
    return (h1 * 60 + m1) % 1440, (h2 * 60 + m2) % 1440


def in_quiet_hours(c: dict) -> bool:
    """Phase 3: is the company inside its quiet_hours window, by the PC's local time? A window may wrap midnight
    (23:00-07:00); equal ends mean all day; a blank setting means no quiet window, and one that cannot be read
    falls back to the default."""
    text = str(db.company_settings(c).get("quiet_hours") or "").strip()
    if not text:
        return False
    start, end = _window(text) or _window(config.DEFAULT_COMPANY_SETTINGS["quiet_hours"])
    now = _local_now()
    minute = now.hour * 60 + now.minute
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _queue() -> list[dict]:
    """Phase 3: todo tasks of active companies in dispatch order, each with its dispatch_class (1 to 6)."""
    due_by = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=DUE_SOON_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return db.q(f"SELECT t.*, {_CLASS_SQL} AS dispatch_class FROM tasks t JOIN companies c ON c.id=t.company_id "
                f"WHERE c.status='active' AND t.status='todo' ORDER BY dispatch_class, t.priority, t.id LIMIT ?",
                (_seconds_ago(FOLLOWUP_SECONDS), due_by, QUEUE_LIMIT))


class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="scheduler")
        # Not '_stop': Thread has an internal _stop() method on Python 3.12 and older.
        self._stop_event = threading.Event()
        self._last_inbox: dict[int, float] = {}
        self._inbox_polls: dict[int, threading.Thread] = {}   # the poll running for each company
        self._last_routines: dict[int, float] = {}
        self._model_waiting = False    # the outage notice was posted; the "back" notice is still owed

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        recover()
        # Build the search index for every company in the background, so the dashboard's search and the first
        # agent runs after an upgrade do not wait for it. Incremental after that, at the start of each run.
        from . import context
        threading.Thread(target=context.reindex_all, daemon=True, name="reindex-all").start()
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            self._stop_event.wait(TICK_SECONDS)

    def tick(self) -> None:
        s = db.get_settings()
        companies = db.q("SELECT * FROM companies WHERE status='active' ORDER BY id")
        for c in companies:
            self._check_inbox(c)
        self._housekeeping(s)
        if s.get("kill_switch"):
            return   # the kill switch stops every dispatch until the owner turns it off
        if self._model_server_down():
            return   # a run would only end in a connection error; housekeeping keeps the watchdog and poller going
        if bench.RUNNING:
            return   # Phase 3: a Holdco Bench run has the GPU; founding, dispatch and cycles wait (chat still works)
        self._resume_founding()
        if not s.get("scheduler_enabled", True):
            return
        capacity = max(1, int(s.get("max_concurrent_runs", 2)))
        over: dict[int, bool] = {}   # the daily budgets (tokens, GPU hours), looked up once per company per tick
        started: set[int] = set()    # tasks handed out this tick
        # 1) Phase 3: follow-ups on the owner's answers and work due within a day go out before planning.
        queue = _queue()
        deps = contracts.dependency_status(queue)   # Phase 2: one query for every dependency in the queue
        ready = self._dispatch([t for t in queue if t["dispatch_class"] in URGENT_CLASSES], capacity, deps, over,
                               started)
        # 2) CEO planning cycles: they create the work.
        for c in companies:
            if agents.running_count() >= capacity:
                break   # full: no cycle now, but the recurring duties below are still created
            self._maybe_cycle(c)
        # 3) Recurring duties from each agent's preset (morning report, follow-ups, weekly reviews...). A company
        #    over its token or GPU budget, or a scheduler at capacity, still gets them: they queue until a run
        #    slot is free and the budget resets (only creating a task here; dispatch below checks both).
        for c in companies:
            if time.time() - self._last_routines.get(c["id"], 0) >= ROUTINE_CHECK_SECONDS:
                self._last_routines[c["id"]] = time.time()
                self._routines(c)
        # 4) Hand out the rest by class, then priority, across all companies (the routines just created included).
        #    Low-value research waits for its company's quiet hours unless nothing else could start.
        known = {t["id"] for t in queue}
        # Phase 4: inside classes 3 to 6, each company's share of the GPU by its allocation score (one query; the
        # settings say whether allocation_mode is weighted or equal, which keeps this Phase 3 order).
        queue = portfolio.order(_queue(), s)
        new = [t for t in queue if t["id"] not in known]
        if new:   # tasks created since the first look (routines, a cycle's plan): their dependencies too
            deps.update(contracts.dependency_status(new))
        quiet: dict[int, bool] = {}   # company id -> inside its quiet hours now
        later = []                    # research outside the quiet window
        for t in queue:
            if agents.running_count() >= capacity:
                return
            if t["dispatch_class"] == RESEARCH_CLASS:
                if t["company_id"] not in quiet:
                    quiet[t["company_id"]] = in_quiet_hours(db.get_company(t["company_id"]) or {})
                if not quiet[t["company_id"]]:
                    later.append(t)
                    continue
            if self._dispatch([t], capacity, deps, over, started) and t["dispatch_class"] != RESEARCH_CLASS:
                ready = True
        if not ready:   # nothing in classes 1 to 5 could start: the GPU would idle, so research runs anyway
            self._dispatch(later, capacity, deps, over, started)

    def _dispatch(self, tasks: list[dict], capacity: int, deps: dict, over: dict, started: set) -> bool:
        """Start these tasks in order while a run slot is free. Phase 2's rules hold: every dependency done or
        cancelled, an enabled and idle agent of the role, and a company over its token or GPU budget waits (tasks
        the owner created excepted). Returns whether any of them could start."""
        ready = False
        for t in tasks:
            if agents.running_count() >= capacity:
                break
            if t["id"] in started:
                continue
            if not contracts.dispatchable(t, deps):
                continue   # Phase 2: a dependency is not done or cancelled yet
            agent = db.get_agent(t["agent_id"]) if t["agent_id"] else None
            if not agent or not agent["enabled"]:
                agent = db.one("SELECT * FROM agents WHERE company_id=? AND role=? AND enabled=1",
                               (t["company_id"], t["role"]))
                if not agent:
                    continue
                db.update("tasks", t["id"], agent_id=agent["id"])
            if agents.is_running(agent["id"]):
                continue
            if t["created_by"] not in config.OWNER_CREATORS:   # the owner's tasks still run over the budget
                if t["company_id"] not in over:
                    company = db.get_company(t["company_id"])
                    over[t["company_id"]] = bool(company and (planning.over_budget(company)
                                                              or contracts.over_gpu_budget(company)))
                if over[t["company_id"]]:
                    continue   # the company used its daily tokens or GPU hours; owner chat and owner tasks still work
            ready = True
            started.add(t["id"])
            agents.start(agent["id"], "task", task_id=t["id"])
        return ready

    def _housekeeping(self, s: dict) -> None:
        """Phase 0 hooks that must run even when autopilot is off: watchdog, health checks, backups, phone poller.
        Phase 2 adds decision deadlines (the kill switch holds them itself) and the daily portfolio brief, which
        Phase 3 builds in its own thread: its narrative is a small-model call, and nothing here waits on a model."""
        from . import connectors   # Phase 1: connector health checks and metric pipes (both cheap when not due)
        for step in (watchdog.tick, doctor.refresh_if_stale, backup.maybe_run, notify.ensure_poller,
                     connectors.check_if_stale, connectors.poll_if_due,
                     decisions.resolve_due, rhythms.portfolio_brief_in_background,   # Phase 2, 3
                     portfolio.monthly_review_if_due):   # Phase 4
            try:
                step()
            except NotImplementedError:
                pass
            except Exception:
                traceback.print_exc()

    def _model_server_down(self) -> bool:
        """True while the Doctor's last look found the model server unreachable.

        Dispatching then only burns runs on connection errors, so tasks and cycles wait. The Doctor is asked to
        look again every few minutes so a recovery is noticed in minutes, not at the next hourly run, and the
        owner gets one notice per outage plus one when it is back. When a run has just failed to reach the model
        while the Doctor still says ok, the Doctor is asked to look now.
        """
        down = doctor.row_status("model_server") == "bad"
        if down:
            doctor.refresh_if_stale(MODEL_RECHECK_MINUTES)
            if not self._model_waiting:
                self._model_waiting = True
                bus.emit(None, "info", MODEL_WAIT_TEXT)
            return True
        if self._model_waiting:
            self._model_waiting = False
            bus.emit(None, "info", MODEL_BACK_TEXT)
        if self._recent_connection_failure():
            doctor.refresh_if_stale(1)
        return False

    @staticmethod
    def _recent_connection_failure() -> bool:
        """Did the newest run end within the last minute because the model server could not be reached?"""
        row = db.one("SELECT summary FROM runs WHERE id=(SELECT MAX(id) FROM runs) AND status='error' AND "
                     "finished_at>=?", (_seconds_ago(60),))
        return bool(row and watchdog.is_model_connection_error(row["summary"]))

    def _resume_founding(self) -> None:
        """A founding run stopped by the kill switch (or by a model outage) leaves the company in "founding" with
        nothing running. recover() restarts those only at startup, so this does the same on every tick once the
        switch is off and the model server answers. A founding that ended in any other error waits for the owner
        to press Retry founding: restarting it every tick would help nobody.

        Founding runs in stages (planning.py): a stage that finished hands over to the next one itself. Should that
        hand-over not happen (the CEO was busy for a moment), the next stage starts here."""
        for c in db.q("SELECT * FROM companies WHERE status='founding' ORDER BY id"):
            ceo = db.one("SELECT id FROM agents WHERE company_id=? AND role='ceo'", (c["id"],))
            if not ceo or agents.is_running(ceo["id"]):
                continue
            last = db.one("SELECT status, summary FROM runs WHERE company_id=? AND kind='founding' "
                          "ORDER BY id DESC LIMIT 1", (c["id"],))
            if not last:
                continue   # the first run is about to start (create_company just returned)
            outage = last["status"] == "error" and watchdog.is_model_connection_error(last["summary"])
            if last["status"] in ("stopped", "interrupted") or outage:
                bus.emit(c["id"], "founding", "resuming the founding that was stopped before it finished")
                agents.found(c["id"])
            elif (last["status"] in ("done", "partial", "blocked", "waiting", "out_of_steps")
                  and planning.founding_stage(c) in planning.FOUNDING_STAGES):
                if agents.found(c["id"]):
                    bus.emit(c["id"], "founding", "starting the next founding stage")

    def _maybe_cycle(self, c: dict) -> None:
        ceo = db.one("SELECT * FROM agents WHERE company_id=? AND role='ceo'", (c["id"],))
        if not ceo or not ceo["enabled"] or agents.is_running(ceo["id"]):
            return
        cs = db.company_settings(c)
        since = db.minutes_since(c["last_cycle_at"])
        # A todo task waiting on a dependency (Phase 2) still counts as open work: a team whose queue all waits on
        # one task is busy, not idle, so it does not trigger a cycle (the due trigger and the watchdog still look).
        open_n = db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND status IN ('todo','in_progress')",
                           (c["id"],)) or 0
        due = since >= float(cs.get("cycle_minutes") or 60)
        idle = open_n == 0 and since >= 10        # team ran out of work
        owner_asked = bool(c["cycle_requested"])   # an answer, a settings change, Retry founding with a busy CEO
        requested = owner_asked and since >= 3
        # The daily token budget holds background planning only; a cycle the owner asked for still runs.
        over = planning.over_budget(c) or contracts.over_gpu_budget(c)   # tokens (Phase 0.5) or GPU hours (Phase 2)
        if planning.should_cycle(c, due, idle, requested) and (requested or not over):
            db.update("companies", c["id"], cycle_requested=0, last_cycle_at=db.now())
            planning.cycle_started(c, requested=owner_asked)
            agents.start(ceo["id"], "cycle")

    def _routines(self, c: dict) -> None:
        cs = db.company_settings(c)
        age_h = db.minutes_since(c["created_at"]) / 60
        analyst: bool | None = None   # Phase 2: is there an enabled analyst? Looked up once, when a routine asks
        for a in db.q("SELECT * FROM agents WHERE company_id=? AND enabled=1 AND routines_on=1", (c["id"],)):
            for r in roles.role_info(a).get("routines", []):
                requires = r.get("requires") or ""
                if requires == "imap" and not emailer.imap_configured(cs):
                    continue
                if requires == "smtp" and emailer.outbound(c, cs) is None:   # SMTP, or Resend (Phase 1)
                    continue
                if requires == "no_analyst":   # Phase 2: the CEO writes the evening brief only without an analyst
                    if analyst is None:
                        analyst = any(roles.preset_key(x) == "analyst" for x in
                                      db.q("SELECT * FROM agents WHERE company_id=? AND enabled=1", (c["id"],)))
                    if analyst:
                        continue
                if age_h < r.get("first_after_hours", r["every_hours"]):
                    continue
                key = f"{roles.preset_key(a)}:{r['title']}"
                if db.one("SELECT id FROM tasks WHERE company_id=? AND agent_id=? AND routine=? AND status IN "
                          "('todo','in_progress','blocked','waiting')", (c["id"], a["id"], key)):
                    continue
                if r.get("at_hour") is not None:
                    if not _at_hour_due(c, key, r):
                        continue
                else:
                    last = db.scalar("SELECT MAX(created_at) FROM tasks WHERE company_id=? AND agent_id=? AND "
                                     "routine=?", (c["id"], a["id"], key))
                    if last and db.minutes_since(last) / 60 < r["every_hours"]:
                        continue
                rhythms.create_routine_task(c, a, r, key)

    def _check_inbox(self, c: dict) -> None:
        cs = db.company_settings(c)
        if not emailer.imap_configured(cs):
            return
        last = self._last_inbox.get(c["id"], 0)
        if time.time() - last < INBOX_MINUTES * 60:
            return
        running = self._inbox_polls.get(c["id"])
        if running is not None and running.is_alive():
            # Phase 3: triage asks the small model about each new email, so a poll can outlast the interval; a
            # second one would read the same last uid and save the same emails (and reply tasks) twice.
            return
        self._last_inbox[c["id"]] = time.time()
        poll = threading.Thread(target=emailer.poll_inbox, args=(c,), daemon=True)
        self._inbox_polls[c["id"]] = poll
        poll.start()


def clear_bench_leftovers() -> None:
    """Phase 3: a Holdco Bench cut short by a crash or a restart left its temporary companies (with their connectors,
    vault secrets and folders) and an unfinished bench_runs row; nothing else would ever remove them. At startup no
    bench is running, so every one of them is a leftover."""
    leftovers = db.q("SELECT * FROM companies WHERE status='bench'")
    unfinished = db.q("SELECT id FROM bench_runs WHERE COALESCE(finished_at, '')=''")
    if not leftovers and not unfinished:
        return
    try:
        from .bench import runner   # imported here: the runner loads every agent module
        for c in leftovers:
            runner._delete_company(c)
        runner._sweep_folders()
    except Exception:
        traceback.print_exc()
    db.run("UPDATE bench_runs SET finished_at=?, notes='Interrupted: Holdco stopped before this bench finished.' "
           "WHERE COALESCE(finished_at, '')=''", (db.now(),))


def recover() -> None:
    """After a restart, put interrupted work back in the queue."""
    ts = db.now()
    db.run("UPDATE tasks SET status='todo', updated_at=? WHERE status='in_progress'", (ts,))
    db.run("UPDATE agents SET status='idle', current_task_id=NULL, status_note='' WHERE status IN ('working','error')")
    db.run("UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'", (ts,))
    db.run("DELETE FROM leases")   # Phase 2: no run survives a restart, so neither do its file leases
    db.run("UPDATE approvals SET status='failed', result='Interrupted by a restart while running. Check whether it "
           "went through before asking the agent to retry.' WHERE status='running'")
    clear_bench_leftovers()
    for c in db.q("SELECT * FROM companies WHERE status='founding'"):
        bus.emit(c["id"], "info", "Holdco restarted while this company was being founded. Resuming founding.")
        agents.found(c["id"])
