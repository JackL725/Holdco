"""Watchdog and working notes (Phase 0).

The watchdog spots stuck patterns and turns each into ONE question for the owner with options and a recommended
answer, instead of a silent "partial". An escalation is an approvals row with action 'question' created through
actions.gate (so the phone notification and the memory note on answering work exactly as for any question) with
payload {question, options, recommended, context, pattern, task_id, company_id}.

Patterns, their questions, options and recommended answers:

- task_failed_twice (raised by task_failed from agents._finalize): a task whose failed attempts reached the
  setting max_task_attempts (default 2). The task is set to blocked.
  "Task #12 'Build landing page' failed twice (last time: ran out of steps). What should we do?"
  Options: Skip this task, Retry once more with my note, Pause the company.
  Recommended: Skip this task, or Retry once more with my note when the task is priority 1.
- founding_partial (raised by founding_ended from agents._finalize): a founding run ended partial or out of
  steps. The company is already active at that point; the question only decides what happens next.
  "Founding PawPlan stopped before it finished (it ran out of steps). A charter exists. What should we do?"
  Options: Continue anyway, Retry founding, Pause the company.
  Recommended: Continue anyway when a charter was written, else Retry founding.
- repeated_error (tick): at least 3 error events with the same normalised message within 1 hour.
  "The same error happened 3 times in the last hour at PawPlan: <message>. What should we do?"
  Options: Pause the company, Keep going. Recommended: Pause the company.
- task_bouncing (tick): a task reassigned between roles 3 or more times (tasks.reassigned, bumped by
  tools.update_task when assign_to changes the role). The task is set to blocked.
  "Task #12 'Write FAQ' has been handed between agents 3 times without finishing. What should we do?"
  Options: Skip this task, Retry once more with my note, Pause the company.
  Recommended: Retry once more with my note.
- connector_down (tick): a Doctor row whose key starts with "connector:" (or "conn:") was bad on two consecutive
  Doctor runs; asked at most once per connector per day. The company comes from the key's second segment
  ("connector:<company id>:<name>") or the row's company_id.
  "Email (SMTP) for PawPlan has failed on two checks in a row (<detail>). What should we do?"
  Options: Keep going without it, Pause the company. Recommended: Keep going without it.
- stalled (tick): company active, autopilot on, open tasks, no run started in 2 hours, and either the Doctor
  says the model server is bad or the last 3 runs all ended in errors.
  "Agents at PawPlan are stuck: <reason>. What should we do?"
  Options: Pause the company, Keep going. Recommended: Pause the company.

Dedupe: while a pending question with the same pattern (and task, for task patterns) exists for the company,
nothing new is created; after one was answered the same pattern is not asked again for 6 hours.

Answers (resolve, called from actions.decide after its own memory note and task reopening):
  Skip this task -> task cancelled; Retry once more with my note -> task todo, attempts and reassigned reset,
  the owner's note appended to the working notes; Pause the company -> company paused; Retry founding ->
  agents.found; Continue anyway -> company active; Keep going / anything else -> nothing more.

Working notes (tasks.notes) record what each attempt tried, what failed and what is left, so a retry continues
instead of starting over. Entries look like "--- [author] 2026-10-14T10:22:05Z ---" followed by the text; the
newest NOTES_MAX_CHARS are kept. The note_progress tool lives here too.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
import threading
import traceback

from . import bus, db, doctor
from .tools import Ctx, S, tool  # noqa: F401  (tool registration)

PATTERNS = ("task_failed_twice", "repeated_error", "task_bouncing", "connector_down", "stalled", "founding_partial")

# Options are matched by exact text in resolve(), so keep them in one place.
TASK_OPTIONS = ["Skip this task", "Retry once more with my note", "Pause the company"]
PAUSE_OPTIONS = ["Pause the company", "Keep going"]
CONNECTOR_OPTIONS = ["Keep going without it", "Pause the company"]
FOUNDING_OPTIONS = ["Continue anyway", "Retry founding", "Pause the company"]

NOTES_MAX_CHARS = 4000          # newest working notes kept per task
CONTEXT_CHARS = 600             # evidence shown with a question
RESOLVED_QUIET_HOURS = 6        # an answered pattern is not asked again within this window
REPEATED_ERROR_MIN = 3          # same error this many times ...
REPEATED_ERROR_MINUTES = 60     # ... within this window
BOUNCE_LIMIT = 3                # reassignments before a task counts as bouncing
CONNECTOR_BAD_STREAK = 2        # consecutive bad Doctor runs before a connector is reported
STALL_MINUTES = 120             # no run started for this long
STALL_ERROR_RUNS = 3            # ... and the last N runs all errored
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

_escalate_lock = threading.Lock()
_bad_streak: dict[str, int] = {}             # connector row key -> consecutive Doctor runs seen bad
_last_connector_alert: dict[str, str] = {}   # connector row key -> date (YYYY-MM-DD) it was last reported
_last_doctor_seen: str | None = None         # checked_at of the Doctor run already counted


def _ago(minutes: float) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes)).strftime(TIME_FMT)


# ---------------------------------------------------------------- working notes

_ENTRY_RE = re.compile(r"^--- \[([^\]\n]*)\] (\S+) ---\n?", re.M)


def entries(notes: str | None) -> list[dict]:
    """Parse working notes into [{"author", "at", "text"}], oldest first."""
    notes = notes or ""
    found = list(_ENTRY_RE.finditer(notes))
    out = []
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(notes)
        out.append({"author": m.group(1), "at": m.group(2), "text": notes[m.end():end].strip()})
    if not found and notes.strip():
        out.append({"author": "", "at": "", "text": notes.strip()})
    return out


def _trim_notes(notes: str) -> str:
    """Keep the newest NOTES_MAX_CHARS, cutting at an entry boundary when one exists in the kept tail."""
    if len(notes) <= NOTES_MAX_CHARS:
        return notes
    tail = notes[-NOTES_MAX_CHARS:]
    m = _ENTRY_RE.search(tail)
    if m and m.start() > 0 and tail[m.start():].strip():
        tail = tail[m.start():]
    return tail.lstrip("\n")


def add_note(task_id: int, text: str, author: str = "") -> None:
    """Append a dated "[author]" entry to a task's working notes and keep the newest NOTES_MAX_CHARS."""
    text = (text or "").strip()
    t = db.get_task(int(task_id)) if task_id else None
    if not text or not t:
        return
    entry = f"--- [{(author or 'note').strip()[:40]}] {db.now()} ---\n{text}"
    notes = ((t.get("notes") or "").rstrip() + "\n\n" + entry).strip()
    db.update("tasks", t["id"], notes=_trim_notes(notes))
    bus.notify("tasks", company_id=t["company_id"])


@tool("note_progress", "Save working notes: what you tried, what failed and what is left. They survive if this "
      "session ends early, and the next attempt starts from them.",
      {"notes": S("The notes (plain text, a few lines)")}, ("notes",))
def note_progress(ctx: Ctx, notes: str) -> str:
    if not ctx.task:
        return "ERROR: this session has no task, so there are no working notes to add to. Use remember instead."
    if not (notes or "").strip():
        return "ERROR: notes is empty."
    add_note(ctx.task["id"], notes, ctx.agent["name"])
    return "Saved."


# ---------------------------------------------------------------- retry guidance

# Failure kind -> (why it failed, in words; what to do differently). Chosen from the newest auto note entry.
GUIDANCE = {
    "repeated_action": ("it repeated the same action until the session was stopped",
                        "use a different tool or approach for that step; if it cannot be done, finish with status "
                        "partial and say why."),
    "search_failed": ("web search was unavailable",
                      "do not search. Fetch URLs you already know with fetch_url, or skip the research and say so in "
                      "the handoff."),
    "out_of_steps": ("it ran out of steps before finishing",
                     "scope down: do the smallest useful piece first, save it, call note_progress, then finish."),
    "model_error": ("the model call failed",
                    "keep tool arguments short: write files in sections with append=true, and keep each call under "
                    "about 2,000 characters."),
    "owner_answer": ("it could not finish without the owner, who has now answered",
                     "follow the owner's answer above exactly."),
    "partial": ("it finished only partially",
                "read the open issues in the handoff above and start from the last saved file."),
    "other": ("it did not finish",
              "keep the scope tight, save progress early with note_progress, and finish with status partial if you "
              "get stuck."),
}
_SEARCH_DOWN = ("web search is unavailable", "search failed", "search providers failed", "search is down")


def classify_failure(task: dict) -> tuple[str, str]:
    """(kind, detail) for the task's last failed attempt, read from its working notes, result and answered questions."""
    auto = [e for e in entries(task.get("notes")) if e["author"] == "auto"]
    last = auto[-1] if auto else None
    answered = None
    if task.get("id"):
        answered = db.scalar("SELECT MAX(decided_at) FROM approvals WHERE task_id=? AND action='question' AND "
                             "status='answered'", (task["id"],))
    if answered and (not last or answered >= last["at"]):
        return "owner_answer", ""
    text = (last["text"] if last else "") or ""
    low = text.lower()
    # The handoff of the latest attempt is the last "[agent] ..." block of tasks.result.
    blocks = re.split(r"\n(?=\[[^\]\n]+\] )", "\n" + (task.get("result") or ""))
    handoff = (blocks[-1] if blocks else "").lower()
    lines = {ln.split(":", 1)[0]: ln.split(":", 1)[1].strip() for ln in text.splitlines() if ":" in ln}
    err_line = lines.get("Last error", "")
    calls = [c.strip() for c in lines.get("Last tool calls", "").split("; ") if c.strip()]
    if "repeating the same action" in low or "repeating the same action" in handoff \
            or (len(calls) >= 3 and len(set(calls)) == 1):
        return "repeated_action", ""
    if any(k in err_line.lower() for k in _SEARCH_DOWN):
        return "search_failed", ""
    if "ended: out_of_steps" in low:
        return "out_of_steps", ""
    if "ended: error" in low:
        m = re.search(r"ended: error \((.*?)\)\s*$", text.splitlines()[0] if text else "")
        return "model_error", (m.group(1) if m else "")
    if "ended: partial" in low:
        return "partial", ""
    return "other", ""


def retry_guidance(task: dict) -> str:
    """Text for attempt >= 2: why the last attempt failed and what to do differently (under about 600 chars)."""
    kind, detail = classify_failure(task)
    why, guidance = GUIDANCE[kind]
    if detail:
        why += f" ({detail[:100]})"
    auto = [e for e in entries(task.get("notes")) if e["author"] == "auto"]
    m = re.match(r"Attempt (\d+) ended", auto[-1]["text"]) if auto else None
    n = int(m.group(1)) if m else max(1, int(task.get("attempts") or 2) - 1)
    return f"Attempt {n} failed because {why}. Do not repeat that approach: {guidance}"[:600]


# ---------------------------------------------------------------- escalation

def _latest_note(task: dict) -> str:
    ents = entries(task.get("notes"))
    return (ents[-1]["text"] if ents else "")[:CONTEXT_CHARS]


def _ceo(company: dict) -> dict:
    agent = (db.one("SELECT * FROM agents WHERE company_id=? AND role='ceo'", (company["id"],))
             or db.one("SELECT * FROM agents WHERE company_id=? ORDER BY id", (company["id"],)))
    return agent or {"id": None, "name": "watchdog"}


def escalate(company: dict, pattern: str, question: str, options: list[str], recommended: str,
             context: str = "", task_id: int | None = None) -> int | None:
    """Create one question for the owner (deduplicated by pattern). Returns the approval id or None.

    A pending question with the same pattern (and task) returns its id; one answered within
    RESOLVED_QUIET_HOURS returns None; otherwise the row goes through actions.gate so the notify hook,
    the approval event and the memory note on answering all apply.
    """
    from . import actions  # imported here: actions is loaded before tools finishes importing this module
    cid = company["id"]
    with _escalate_lock:
        quiet_since = _ago(RESOLVED_QUIET_HOURS * 60)
        rows = db.q("SELECT id, status, task_id, payload, decided_at FROM approvals WHERE company_id=? AND "
                    "action='question' AND payload LIKE ? ORDER BY id DESC LIMIT 50",
                    (cid, f'%"pattern": "{pattern}"%'))
        for r in rows:
            p = db.loads(r["payload"], {}) or {}
            if p.get("pattern") != pattern or (task_id is not None and r["task_id"] != task_id):
                continue
            if r["status"] == "pending":
                return r["id"]
            if (r["decided_at"] or "") >= quiet_since:
                return None
        payload = {"question": question, "options": list(options), "recommended": recommended,
                   "context": (context or "")[:2000], "pattern": pattern, "task_id": task_id, "company_id": cid}
        res = actions.gate(company, _ceo(company), task_id, "question", payload)
    m = re.search(r"#(\d+)", res or "")
    return int(m.group(1)) if m else None


def _failure_words(reason: str, run_status: str) -> str:
    words = {"out_of_steps": "ran out of steps", "error": "hit an error", "partial": "finished only partially",
             "stopped": "was stopped"}.get(run_status, run_status or "did not finish")
    detail = (reason or "").strip()
    if detail.startswith("Status:"):
        detail = next((ln[len("Summary:"):].strip() for ln in detail.splitlines() if ln.startswith("Summary:")), "")
    elif detail.startswith("Run ended:"):
        detail = detail[len("Run ended:"):].strip()
    detail = " ".join(detail.split())[:120]
    if not detail or detail.lower() == "ran out of steps before finishing.":
        return words
    return f"{words}: {detail}"


def task_failed(task: dict, reason: str, run_status: str) -> None:
    """Called by agents._finalize after a task attempt ends without a clean finish.

    A stopped run (pause or kill switch) is not the agent's failure: its attempt is given back. Otherwise, once
    the failed attempts reach max_task_attempts the task is blocked and the owner gets one question; below that
    the task goes back to todo so the next attempt runs with retry guidance.
    """
    t = db.get_task(task["id"]) if task else None
    if not t:
        return
    ts = db.now()
    if run_status == "stopped":
        db.update("tasks", t["id"], attempts=max(0, int(t["attempts"] or 0) - 1), updated_at=ts)
        return
    attempts = int(t["attempts"] or 0)
    limit = max(1, int(db.get_settings().get("max_task_attempts") or 2))
    if attempts < limit:
        if t["status"] != "todo":
            db.update("tasks", t["id"], status="todo", updated_at=ts)
            bus.notify("tasks", company_id=t["company_id"])
        return
    company = db.get_company(t["company_id"])
    if not company:
        return
    if t["status"] != "blocked":
        db.update("tasks", t["id"], status="blocked", updated_at=ts)
        bus.notify("tasks", company_id=t["company_id"])
    times = "twice" if attempts == 2 else f"{attempts} times"
    question = (f"Task #{t['id']} '{t['title']}' failed {times} (last time: {_failure_words(reason, run_status)}). "
                f"What should we do?")
    recommended = "Retry once more with my note" if int(t["priority"] or 2) == 1 else "Skip this task"
    escalate(company, "task_failed_twice", question, TASK_OPTIONS, recommended, _latest_note(t), t["id"])


def founding_ended(company: dict, run_status: str, summary: str) -> None:
    """Called by agents._finalize when a founding run ends partial / out of steps (the company is active by then)."""
    company = db.get_company(company["id"]) or company
    has_charter = bool((company.get("charter") or "").strip())
    how = {"partial": "it finished only partially", "out_of_steps": "it ran out of steps"}.get(run_status, run_status)
    question = (f"Founding {company['name']} stopped before it finished ({how}). "
                f"{'A charter exists.' if has_charter else 'No charter was written.'} What should we do?")
    escalate(company, "founding_partial", question, FOUNDING_OPTIONS,
             "Continue anyway" if has_charter else "Retry founding", (summary or "")[:CONTEXT_CHARS])


def match_option(answer: str, options: list[str]) -> str | None:
    """The option an answer means: exact text, else case-insensitive, else a case-insensitive prefix either way."""
    a = (answer or "").strip()
    if not a:
        return None
    for o in options:
        if a == o:
            return o
    low = a.lower()
    for o in options:
        if low == o.lower():
            return o
    for o in options:
        ol = o.lower()
        if low.startswith(ol) or (len(low) >= 3 and ol.startswith(low)):
            return o
    return None


def resolve(approval: dict, answer: str) -> None:
    """Called by actions.decide when a question carrying a pattern is answered: apply the chosen option."""
    try:
        _apply(approval, answer)
    except Exception:  # the owner's answer is already recorded; a failed follow-up must not undo that
        traceback.print_exc()


def _apply(approval: dict, answer: str) -> None:
    p = db.loads(approval.get("payload"), {}) or {}
    option = match_option(answer, p.get("options") or [])
    if not option:
        return   # free text: actions.decide already saved it as a memory note and reopened the task
    cid = p.get("company_id") or approval.get("company_id")
    company = db.get_company(cid) if cid else None
    if not company:
        return
    task_id = p.get("task_id") or approval.get("task_id")
    task = db.get_task(task_id) if task_id else None
    ts = db.now()
    if option == "Skip this task" and task:
        db.update("tasks", task["id"], status="cancelled", updated_at=ts,
                  result=((task["result"] or "") + "\n[Owner] Skipped this task (watchdog question).").strip()[-6000:])
        bus.emit(cid, "task", f"skipped task #{task['id']} on the owner's answer", None, {"task_id": task["id"]})
        bus.notify("tasks", company_id=cid)
    elif option == "Retry once more with my note" and task:
        note = (approval.get("feedback") or "").strip()
        rest = (answer or "").strip()[len(option):].strip(" :,.-")
        note = note or rest or "Retry once more (the owner's decision)."
        db.update("tasks", task["id"], status="todo", attempts=0, reassigned=0, updated_at=ts)
        add_note(task["id"], note, "owner")
        bus.emit(cid, "task", f"task #{task['id']} gets one more attempt with the owner's note", None,
                 {"task_id": task["id"]})
        bus.notify("tasks", company_id=cid)
    elif option == "Pause the company":
        if company["status"] != "paused":
            db.update("companies", cid, status="paused")
            bus.emit(cid, "info", "paused the company on the owner's answer")
            bus.notify("companies")
    elif option == "Retry founding":
        from . import agents  # imported here: agents imports this module
        bus.emit(cid, "founding", "retrying the founding on the owner's answer")
        agents.found(cid)
    elif option == "Continue anyway":
        if company["status"] != "active":
            db.update("companies", cid, status="active", last_cycle_at=ts)
            bus.emit(cid, "founding", "founding accepted as is on the owner's answer")
            bus.notify("companies")
    # "Keep going" and "Keep going without it" need nothing beyond the memory note decide() already wrote.


# ---------------------------------------------------------------- the periodic pass

def normalise_error(message: str) -> str:
    """Fold one error message into a comparable key: lowercase, agent name dropped, digit runs and ids collapsed."""
    m = (message or "").lower()
    if "hit an error: " in m:
        m = m.split("hit an error: ", 1)[1]
    m = re.sub(r"#\d+", "#", m)
    m = re.sub(r"\d+", "#", m)
    return " ".join(m.split())[:120]


def error_hash(message: str) -> str:
    return hashlib.sha1(normalise_error(message).encode("utf-8")).hexdigest()[:8]


def tick() -> None:
    """Cheap SQL-only pass run by the scheduler every tick. Never raises."""
    try:
        companies = db.q("SELECT * FROM companies WHERE status='active' ORDER BY id")
        if not companies:
            return
        settings = db.get_settings()
        pending_tasks = {r["task_id"] for r in db.q("SELECT task_id FROM approvals WHERE action='question' AND "
                                                     "status='pending' AND task_id IS NOT NULL")}
        for c in companies:
            _check_repeated_errors(c)
            _check_bouncing(c, pending_tasks)
            _check_stalled(c, settings)
        _check_connectors({c["id"]: c for c in companies})
    except Exception:
        traceback.print_exc()


def _check_repeated_errors(c: dict) -> None:
    rows = db.q("SELECT message FROM events WHERE company_id=? AND kind='error' AND created_at>=? "
                "ORDER BY id DESC LIMIT 300", (c["id"], _ago(REPEATED_ERROR_MINUTES)))
    counts: dict[str, list] = {}
    for r in rows:
        counts.setdefault(normalise_error(r["message"]), []).append(r["message"])
    for key, msgs in counts.items():
        if key and len(msgs) >= REPEATED_ERROR_MIN:
            sample = " ".join((msgs[0] or "").split())
            question = (f"The same error happened {len(msgs)} times in the last hour at {c['name']}: "
                        f"{sample[:200]}. What should we do?")
            escalate(c, "repeated_error", question, PAUSE_OPTIONS, "Pause the company", sample[:CONTEXT_CHARS])
            return   # one question per company per tick is plenty


def _check_bouncing(c: dict, pending_tasks: set) -> None:
    rows = db.q("SELECT * FROM tasks WHERE company_id=? AND reassigned>=? AND status IN "
                "('todo','in_progress','blocked','waiting') ORDER BY id", (c["id"], BOUNCE_LIMIT))
    for t in rows:
        if t["id"] in pending_tasks:
            continue
        question = (f"Task #{t['id']} '{t['title']}' has been handed between agents {t['reassigned']} times without "
                    f"finishing. What should we do?")
        context = _latest_note(t) or (t["result"] or "")[-CONTEXT_CHARS:]
        aid = escalate(c, "task_bouncing", question, TASK_OPTIONS, "Retry once more with my note", context, t["id"])
        if aid and t["status"] in ("todo", "in_progress"):
            db.update("tasks", t["id"], status="blocked", updated_at=db.now())
            bus.notify("tasks", company_id=c["id"])


def _doctor_rows() -> list[dict]:
    """The Doctor's cached rows, ignoring anything that is not a row (the cache belongs to another module)."""
    return [r for r in list(getattr(doctor, "LAST", None) or []) if isinstance(r, dict)]


def _model_server_bad() -> str | None:
    for r in _doctor_rows():
        if r.get("key") == "model_server" and r.get("status") == "bad":
            return r.get("detail") or "the model server is unreachable"
    return None


def _check_stalled(c: dict, settings: dict) -> None:
    if not settings.get("scheduler_enabled", True) or settings.get("kill_switch"):
        return   # nothing is meant to run, so nothing is stuck
    open_n = db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND status IN ('todo','in_progress')",
                       (c["id"],)) or 0
    if not open_n:
        return
    if db.scalar("SELECT COUNT(*) FROM runs WHERE company_id=? AND started_at>=?", (c["id"], _ago(STALL_MINUTES))):
        return
    reason = ""
    bad = _model_server_bad()
    if bad:
        reason = f"the model server is unreachable ({bad[:160]})"
    else:
        last = db.q("SELECT status FROM runs WHERE company_id=? ORDER BY id DESC LIMIT ?", (c["id"], STALL_ERROR_RUNS))
        if len(last) == STALL_ERROR_RUNS and all(r["status"] == "error" for r in last):
            reason = f"the last {STALL_ERROR_RUNS} runs all ended in errors and nothing has started for 2 hours"
    if reason:
        escalate(c, "stalled", f"Agents at {c['name']} are stuck: {reason}. What should we do?", PAUSE_OPTIONS,
                 "Pause the company", reason)


def _check_connectors(companies: dict[int, dict]) -> None:
    """Count consecutive Doctor runs in which each connector row is bad; ask once per connector per day."""
    global _last_doctor_seen
    rows = _doctor_rows()
    if not rows:
        return
    checked = rows[0].get("checked_at") or ""
    if checked == _last_doctor_seen:
        return   # the same Doctor run was counted already
    _last_doctor_seen = checked
    today = db.now()[:10]
    for r in rows:
        key = str(r.get("key") or "")
        if not key.startswith(("connector:", "conn:")):
            continue
        if r.get("status") != "bad":
            _bad_streak.pop(key, None)
            continue
        _bad_streak[key] = _bad_streak.get(key, 0) + 1
        if _bad_streak[key] < CONNECTOR_BAD_STREAK or _last_connector_alert.get(key) == today:
            continue
        # Doctor keys are "connector:<name>:<company id>" (global ones have no id); accept "conn:<id>:<name>" too.
        parts = key.split(":")
        digits = [x for x in parts[1:] if x.isdigit()]
        cid = int(digits[0]) if digits else r.get("company_id")
        c = companies.get(cid) if cid is not None else None
        if not c:
            continue
        label = r.get("label") or parts[-1]
        detail = " ".join(str(r.get("detail") or "").split())[:200]
        question = (f"{label} for {c['name']} has failed on two checks in a row"
                    f"{' (' + detail + ')' if detail else ''}. What should we do?")
        escalate(c, "connector_down", question, CONNECTOR_OPTIONS, "Keep going without it", detail)
        _last_connector_alert[key] = today
