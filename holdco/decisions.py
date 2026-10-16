"""Decisions queue (Phase 2, docs/phases/phase-2-organize.md section 5): "which of these should we do?" with
options, a recommendation and a deadline after which a reversible decision takes the recommended option.

Shared interface (used by brain.py for owner-routed proposals and goal changes, and by rhythms.py for the monthly
check): create() queues the decision; payload["origin"] says who asked, and on_decided() hands the answer back to
that module (brain.on_owner_decision for origin kinds "brain_change" and "goals"; the monthly check's "Kill" pauses
the company).

A decision is an approvals row of action 'decision'. It follows Phase 0.5's rules for owner questions: the owner
reads it on a phone (planning.question_problem), one open question per topic (planning.find_duplicate), and the
team's pending questions and decisions together are capped at planning.MAX_PENDING_QUESTIONS. Decisions that code
raises for a module (an origin is given) are worded by that code and skip those three rules; they keep the
validation and the gate's deduplication on the question text.
"""
from __future__ import annotations

import datetime as dt
import json

from . import actions, bus, db, notify, planning
from .tools import Ctx, ToolError, S, I, tool  # noqa: F401

MIN_OPTIONS, MAX_OPTIONS = 2, 5
MIN_HOURS, MAX_HOURS = 1, 336     # a deadline between one hour and two weeks
REVIEW_DAYS = 14                  # the weekly review brings a decision back for its outcome after this many days
MONTHLY_KEY = "ceo:Monthly strategy check"   # rhythms.MONTHLY_KEY: its keep, change or kill question


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(company: dict, agent: dict | None, task_id, question: str, topics: list, reason: str, approval_id,
           message: str) -> None:
    """Count a refused decision the way ask_owner counts a refused question (the Bench scores repeats)."""
    try:
        bus.emit(company["id"], "question_deduped", message, (agent or {}).get("id"),
                 {"reason": reason, "approval_id": approval_id, "topics": topics, "question": question[:300],
                  "task_id": task_id, "action": "decision"})
    except Exception as e:   # counting must never stop the agent
        print(f"decision event failed: {e}")


def _queue(company: dict, agent: dict, task_id: int | None, question: str, options, recommended: str,
           deadline_hours=48, reversible: bool = True, context: str = "", expected: str = "",
           origin: dict | None = None) -> tuple[str, int | None]:
    """create() with the id of the pending question or decision this one was folded into (None otherwise), so
    ask_decision can let the run finish 'waiting' on it."""
    question = (question or "").strip()
    if not question:
        return "ERROR: the question is empty. Write the decision the owner has to make.", None
    opts = planning.parse_options(options)   # one per line or a list; bullets, numbering and repeats removed
    if not MIN_OPTIONS <= len(opts) <= MAX_OPTIONS:
        given = f"only {len(opts)}" if len(opts) < MIN_OPTIONS else "more than that"
        return (f"ERROR: give {MIN_OPTIONS} to {MAX_OPTIONS} options, one per line ({given} given). Each option is "
                f"one thing the owner can choose; they become buttons on the owner's phone."), None
    rec = planning._clean_option(recommended)
    match = next((o for o in opts if o == rec), None) or next((o for o in opts if o.lower() == rec.lower()), None)
    if not match:
        return ("ERROR: recommended must be one of the options, copied exactly. Options: "
                + "; ".join(f"'{o}'" for o in opts) + "."), None
    try:
        hours = int(float(deadline_hours if deadline_hours not in (None, "") else 48))
    except (TypeError, ValueError):
        hours = 0
    if not MIN_HOURS <= hours <= MAX_HOURS:
        return f"ERROR: deadline_hours must be a whole number from {MIN_HOURS} to {MAX_HOURS}.", None
    cid = company["id"]
    topics = planning.detect_topics("\n".join([question] + opts))
    if origin is None:
        problem = planning.question_problem(question, opts)
        if problem:
            return problem, None
        dup = planning.find_duplicate(cid, question, topics)
        if dup:
            _event(company, agent, task_id, question, topics, dup["reason"], dup["id"],
                   f"decision not queued: already waiting as #{dup['id']} ({dup['label']})")
            return dup["text"], dup["id"]
        mine = planning.pending_agent_questions(cid)
        if len(mine) >= planning.MAX_PENDING_QUESTIONS:
            _event(company, agent, task_id, question, topics, "cap", None,
                   f"decision not queued: {len(mine)} questions are already waiting on the owner")
            return planning.cap_refusal(mine), None
    deadline = _iso(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours))
    payload: dict = {"question": question[:2000], "options": opts, "recommended": match, "deadline_at": deadline,
                     "reversible": bool(reversible)}
    if (context or "").strip():
        payload["context"] = context.strip()[:planning.CONTEXT_CAP]
    if (expected or "").strip():
        payload["expected"] = expected.strip()[:1000]
    if topics:
        payload["topics"] = topics
    if origin:
        payload["origin"] = origin
    return actions.gate(company, agent, task_id, "decision", payload), None


def create(company: dict, agent: dict, task_id: int | None, question: str, options: list[str], recommended: str,
           deadline_hours: int = 48, reversible: bool = True, context: str = "", expected: str = "",
           origin: dict | None = None) -> str:
    """Queue a decision for the owner through actions.gate and return the text for the agent: "Decision #N sent
    to the owner...", "Already waiting..." or an "ERROR: ..." it can act on. origin, when given, is
    {"kind": "brain_change", "change_id": N} or {"kind": "goals", ...} or {"kind": "monthly"}.

    Validation: 2 to 5 options (a list, or one per line), recommended equal to one of them, deadline_hours from 1
    to 336. Without an origin, Phase 0.5's rules for owner questions apply as well (see the module docstring)."""
    return _queue(company, agent, task_id, question, options, recommended, deadline_hours, reversible, context,
                  expected, origin)[0]


@tool("ask_decision", "Ask the owner to choose between options, with your recommendation and a deadline after which a "
      "reversible decision takes the recommended option. For simple questions use ask_owner.",
      {"question": S("The decision, one paragraph"), "options": S("2 to 5 options, one per line"),
       "recommended": S("The option you recommend, exactly as written in options"),
       "deadline_hours": I("Hours before the recommended option is taken (1 to 336, default 48)"),
       "reversible": {"type": "boolean", "description": "False if it cannot be undone (then it waits for the owner)"},
       "context": S("Why now and what was tried (up to 1,500 characters)")},
      ("question", "options", "recommended"), risk="write")
def ask_decision(ctx: Ctx, question: str, options: str, recommended: str, deadline_hours: int = 48,
                 reversible: bool = True, context: str = "") -> str:
    from . import truth   # imported here: truth imports tools, which imports this module
    # Phase 0.5's claims gate on what the owner will read (the same check ask_owner runs).
    refusal = truth.claims_guard(ctx, "\n".join((question or "", context or "")), "ask")
    if refusal:
        return refusal
    origin = None
    task = ctx.task or {}
    if task.get("routine") == MONTHLY_KEY and any(o.lower().startswith("kill") for o in planning.parse_options(options)):
        # The monthly keep, change or kill question never resolves on its own: Kill pauses the company.
        origin, reversible = {"kind": "monthly"}, False
    text, waiting_on = _queue(ctx.company, ctx.agent, task.get("id"), question, options, recommended,
                              deadline_hours, reversible, context, origin=origin)
    if waiting_on:
        # Folded into a pending question or decision: finish(status='waiting') accepts that one, and its answer
        # reopens this task (actions.decide).
        ctx.waiting_on = waiting_on
    return text


def on_decided(row: dict, answer: str, decided_by: str) -> None:
    """actions.decide answered a decision: record it in the brain's decisions table and tell its origin."""
    p = db.loads(row.get("payload"), {}) or {}
    cid = row["company_id"]
    now = dt.datetime.now(dt.timezone.utc)
    why = str(p.get("question") or row.get("summary") or "")
    if p.get("context"):
        why += "\n\n" + str(p["context"])
    db.run("INSERT INTO decisions(company_id, what, why, expected, review_at, outcome, decided_by, reversible, "
           "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
           (cid, answer, why[:4000], str(p.get("expected") or ""), _iso(now + dt.timedelta(days=REVIEW_DAYS)), "",
            decided_by or "owner", 1 if p.get("reversible", True) else 0, _iso(now)))
    origin = p.get("origin") or {}
    kind = origin.get("kind") if isinstance(origin, dict) else ""
    if kind in ("brain_change", "goals"):
        from . import brain   # imported here: brain imports tools, which imports this module
        brain.on_owner_decision(cid, origin, answer)
    elif kind == "monthly" and str(answer or "").strip().lower().startswith("kill"):
        company = db.get_company(cid)
        if company and company["status"] not in ("paused", "archived"):
            # Holdco never deletes a company: Kill stops its agents and keeps everything for the owner.
            db.update("companies", cid, status="paused")
            bus.emit(cid, "info", "paused the company on the owner's monthly decision", None,
                     {"approval_id": row["id"], "by": decided_by})
            bus.notify("companies")


def resolve_due() -> None:
    """Scheduler (every tick): reversible decisions past their deadline take the recommended option; irreversible
    ones stay pending and the owner is reminded once a day. The kill switch holds auto-resolution."""
    rows = db.q("SELECT a.* FROM approvals a JOIN companies c ON c.id=a.company_id WHERE a.action='decision' AND "
                "a.status='pending' AND c.status='active' ORDER BY a.id")
    if not rows:
        return
    now =dt.datetime.now(dt.timezone.utc)
    killed = bool(db.get_settings().get("kill_switch"))
    for row in rows:
        p = db.loads(row["payload"], {}) or {}
        deadline = db.parse_time(p.get("deadline_at"))
        if not deadline or deadline > now:
            continue
        rec = str(p.get("recommended") or "")
        if p.get("reversible", True) and rec:
            if killed:
                continue
            try:
                actions.decide(row["id"], "approve", answer=rec, decided_by="deadline")
            except ValueError as e:   # decided by the owner a moment ago
                print(f"decision #{row['id']} not resolved: {e}")
                continue
            company = db.get_company(row["company_id"]) or {}
            notify.send_later(f"Decision #{row['id']} was taken by the deadline: {notify.escape(rec)} "
                              f"({notify.escape(company.get('name') or 'Holdco')})", kind="important")
            continue
        today = dt.date.today().isoformat()   # the owner's day
        if p.get("reminded_on") == today:
            continue
        p["reminded_on"] = today
        db.update("approvals", row["id"], payload=json.dumps(p, sort_keys=True))
        try:
            text, buttons, _kind = notify.build_approval({**row, "payload": json.dumps(p)})
            notify.send_later("Reminder: " + text, buttons, kind="request")
        except Exception as e:   # a phone that cannot be reached never blocks the scheduler
            print(f"decision reminder failed: {e}")
