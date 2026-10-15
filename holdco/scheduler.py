"""Keeps every active company working around the clock.

Every few seconds it: lets each CEO re-plan when a cycle is due, hands queued tasks to idle agents
(respecting the GPU concurrency limit), and checks company inboxes.
"""
from __future__ import annotations

import datetime as _dt
import os
import threading
import time
import traceback

from . import agents, backup, bus, db, doctor, emailer, notify, planning, roles, watchdog

TICK_SECONDS = float(os.environ.get("HOLDCO_TICK_SECONDS", 8))
ROUTINE_CHECK_SECONDS = float(os.environ.get("HOLDCO_ROUTINE_CHECK_SECONDS", 60))
INBOX_MINUTES = 5
MODEL_RECHECK_MINUTES = 5      # while the model server is down, the Doctor is asked to look again this often
MODEL_WAIT_TEXT = ("Waiting for the model server: it is unreachable, so tasks and planning are on hold. The health "
                   "checks retry every few minutes.")
MODEL_BACK_TEXT = "The model server is back. Tasks and planning resume."


def _seconds_ago(n: float) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="scheduler")
        self._stop = threading.Event()
        self._last_inbox: dict[int, float] = {}
        self._last_routines: dict[int, float] = {}
        self._model_waiting = False    # the outage notice was posted; the "back" notice is still owed

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        recover()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            self._stop.wait(TICK_SECONDS)

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
        self._resume_founding()
        if not s.get("scheduler_enabled", True):
            return
        capacity = max(1, int(s.get("max_concurrent_runs", 2)))
        # 1) CEO planning cycles first: they create the work.
        for c in companies:
            if agents.running_count() >= capacity:
                return
            self._maybe_cycle(c)
        # 2) Recurring duties from each agent's preset (morning report, follow-ups, weekly reviews...).
        for c in companies:
            if time.time() - self._last_routines.get(c["id"], 0) >= ROUTINE_CHECK_SECONDS:
                self._last_routines[c["id"]] = time.time()
                self._routines(c)
        # 3) Hand out tasks, highest priority first across all companies.
        open_tasks = db.q(
            "SELECT t.* FROM tasks t JOIN companies c ON c.id=t.company_id "
            "WHERE c.status='active' AND t.status='todo' ORDER BY t.priority, t.id LIMIT 200")
        for t in open_tasks:
            if agents.running_count() >= capacity:
                return
            agent = db.get_agent(t["agent_id"]) if t["agent_id"] else None
            if not agent or not agent["enabled"]:
                agent = db.one("SELECT * FROM agents WHERE company_id=? AND role=? AND enabled=1",
                               (t["company_id"], t["role"]))
                if not agent:
                    continue
                db.update("tasks", t["id"], agent_id=agent["id"])
            if agents.is_running(agent["id"]):
                continue
            company = db.get_company(t["company_id"])
            if company and planning.over_budget(company):
                continue   # the company used its daily token budget; owner chat and owner-started runs still work
            agents.start(agent["id"], "task", task_id=t["id"])

    def _housekeeping(self, s: dict) -> None:
        """Phase 0 hooks that must run even when autopilot is off: watchdog, health checks, backups, phone poller."""
        for step in (watchdog.tick, doctor.refresh_if_stale, backup.maybe_run, notify.ensure_poller):
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
        to press Retry founding: restarting it every tick would help nobody."""
        for c in db.q("SELECT * FROM companies WHERE status='founding' ORDER BY id"):
            ceo = db.one("SELECT id FROM agents WHERE company_id=? AND role='ceo'", (c["id"],))
            if not ceo or agents.is_running(ceo["id"]):
                continue
            last = db.one("SELECT status, summary FROM runs WHERE company_id=? AND kind='founding' "
                          "ORDER BY id DESC LIMIT 1", (c["id"],))
            if not last:
                continue   # the first run is about to start (create_company just returned)
            outage = last["status"] == "error" and watchdog.is_model_connection_error(last["summary"])
            if last["status"] not in ("stopped", "interrupted") and not outage:
                continue
            bus.emit(c["id"], "founding", "resuming the founding that was stopped before it finished")
            agents.found(c["id"])

    def _maybe_cycle(self, c: dict) -> None:
        ceo = db.one("SELECT * FROM agents WHERE company_id=? AND role='ceo'", (c["id"],))
        if not ceo or not ceo["enabled"] or agents.is_running(ceo["id"]):
            return
        cs = db.company_settings(c)
        since = db.minutes_since(c["last_cycle_at"])
        open_n = db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND status IN ('todo','in_progress')",
                           (c["id"],)) or 0
        due = since >= float(cs.get("cycle_minutes") or 60)
        idle = open_n == 0 and since >= 10        # team ran out of work
        requested = bool(c["cycle_requested"]) and since >= 3
        if planning.should_cycle(c, due, idle, requested) and not planning.over_budget(c):
            db.update("companies", c["id"], cycle_requested=0, last_cycle_at=db.now())
            planning.cycle_started(c)
            agents.start(ceo["id"], "cycle")

    def _routines(self, c: dict) -> None:
        cs = db.company_settings(c)
        age_h = db.minutes_since(c["created_at"]) / 60
        for a in db.q("SELECT * FROM agents WHERE company_id=? AND enabled=1 AND routines_on=1", (c["id"],)):
            for r in roles.role_info(a).get("routines", []):
                if r["requires"] == "imap" and not emailer.imap_configured(cs):
                    continue
                if r["requires"] == "smtp" and not emailer.smtp_configured(cs):
                    continue
                if age_h < r["first_after_hours"]:
                    continue
                key = f"{roles.preset_key(a)}:{r['title']}"
                if db.one("SELECT id FROM tasks WHERE company_id=? AND agent_id=? AND routine=? AND status IN "
                          "('todo','in_progress','blocked','waiting')", (c["id"], a["id"], key)):
                    continue
                last = db.scalar("SELECT MAX(created_at) FROM tasks WHERE company_id=? AND agent_id=? AND routine=?",
                                 (c["id"], a["id"], key))
                if last and db.minutes_since(last) / 60 < r["every_hours"]:
                    continue
                ts = db.now()
                tid = db.run("INSERT INTO tasks(company_id, title, description, role, agent_id, status, priority, "
                             "created_by, deliverable, routine, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (c["id"], r["title"], r["description"], a["role"], a["id"], "todo", r["priority"],
                              "routine", r["deliverable"], key, ts, ts))
                bus.emit(c["id"], "task", f"recurring duty for {a['name']}: {r['title']}", None,
                         {"task_id": tid, "routine": key})
                bus.notify("tasks", company_id=c["id"])

    def _check_inbox(self, c: dict) -> None:
        cs = db.company_settings(c)
        if not emailer.imap_configured(cs):
            return
        last = self._last_inbox.get(c["id"], 0)
        if time.time() - last < INBOX_MINUTES * 60:
            return
        self._last_inbox[c["id"]] = time.time()
        threading.Thread(target=emailer.poll_inbox, args=(c,), daemon=True).start()


def recover() -> None:
    """After a restart, put interrupted work back in the queue."""
    ts = db.now()
    db.run("UPDATE tasks SET status='todo', updated_at=? WHERE status='in_progress'", (ts,))
    db.run("UPDATE agents SET status='idle', current_task_id=NULL, status_note='' WHERE status IN ('working','error')")
    db.run("UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'", (ts,))
    db.run("UPDATE approvals SET status='failed', result='Interrupted by a restart while running. Check whether it "
           "went through before asking the agent to retry.' WHERE status='running'")
    for c in db.q("SELECT * FROM companies WHERE status='founding'"):
        bus.emit(c["id"], "info", "Holdco restarted while this company was being founded. Resuming founding.")
        agents.found(c["id"])
