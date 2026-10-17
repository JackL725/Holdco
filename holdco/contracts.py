"""Task contracts, the dependency graph and budgets (Phase 2, docs/phases/phase-2-organize.md sections 2, 3 and 6).

A task is a contract: the goal it serves and what it should move (expected), what to read first (inputs), the
tasks that must be done first (depends_on), a deadline (due_at), a budget of tool calls and tokens, and the checks
Phase 3's gate will run. validate() is shared by create_task, update_task and the owner's task routes, so the
model and the dashboard get the same rules and the same texts. File leases live in workspace.py.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import traceback

from . import bus, db

CLOSED = ("done", "cancelled")                 # a dependency in one of these no longer holds anything up
OPEN = ("todo", "in_progress", "blocked", "waiting")
CHECK_TOOLS = ("browser_check", "audit_page", "review_text", "check_contrast", "fetch_url", "run_tests")   # Phase 3
DUE_DAYS = (1, 90)
BUDGET_STEPS = (3, 60)
PROPOSAL_LINE_CAP = 500   # characters of one brain proposal in the planning prompt (brain_lookup shows as much)


class ContractError(ValueError):
    """A contract field the model or the owner must fix. tools.execute shows it as 'ERROR: <text>'; the routes
    answer 400 with the text."""


def _utc(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# ---------------------------------------------------------------- the dependency graph

def parse_ids(value) -> list[int]:
    """Task ids from '12, 14', '#12 #14', a JSON list or a list of numbers; duplicates dropped, order kept."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        raw = " ".join(str(v) for v in value)
    else:
        raw = str(value)
    return list(dict.fromkeys(int(n) for n in re.findall(r"\d+", raw) if int(n) > 0))


def deps_of(task: dict | None) -> list[int]:
    """The ids a task waits on (tasks.depends_on is a JSON list; the API hands back a list)."""
    return parse_ids((task or {}).get("depends_on"))


def dependency_status(tasks: list[dict]) -> dict[int, str]:
    """{task id: status} for every id named in these tasks' depends_on, in one query. The scheduler calls it once
    per tick and passes the result to dispatchable(); the task list route uses it for blocked_by."""
    ids = sorted({d for t in tasks for d in deps_of(t)})
    if not ids:
        return {}
    rows = db.q(f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)
    return {r["id"]: r["status"] for r in rows}


def blocked_by(task: dict, statuses: dict[int, str] | None = None) -> list[int]:
    """The dependencies that are not done or cancelled yet. A dependency that no longer exists holds nothing up:
    waiting on it would wait forever."""
    deps = deps_of(task)
    if not deps:
        return []
    if statuses is None:
        statuses = dependency_status([task])
    return [d for d in deps if d in statuses and statuses[d] not in CLOSED]


def dispatchable(task: dict, statuses: dict[int, str] | None = None) -> bool:
    """May the scheduler start this todo task now (every dependency done or cancelled)? statuses: the tick's
    dependency_status(); without it the task's own dependencies are looked up."""
    return not blocked_by(task, statuses)


def _cycle_through(company_id: int, task_id: int | None, deps: list[int]) -> list[int] | None:
    """Walk depends_on from deps. Returns the path of a cycle the new edges would close (task_id -> ... ->
    task_id) or one that already exists among the tasks they lead to (a group that can never start), else None."""
    graph: dict[int, list[int]] = {}
    for r in db.q("SELECT id, depends_on FROM tasks WHERE company_id=?", (company_id,)):
        graph[r["id"]] = deps_of(r)
    if task_id is not None:
        graph[task_id] = list(deps)
        starts = [task_id]
    else:
        starts = list(deps)
    state: dict[int, int] = {}   # 1 = on the current path, 2 = finished
    path: list[int] = []

    def visit(n: int) -> list[int] | None:
        state[n] = 1
        path.append(n)
        for m in graph.get(n, []):
            if state.get(m) == 1:
                return path[path.index(m):] + [m]
            if m not in state:
                found = visit(m)
                if found:
                    return found
        path.pop()
        state[n] = 2
        return None

    for s in starts:
        if s not in state:
            found = visit(s)
            if found:
                return found
    return None


# ---------------------------------------------------------------- validation (tools and routes)

def active_goal_list(company_id: int) -> str:
    """'#1 10 paying customers, #2 200 waitlist signups' (the no-goal warning and goal errors)."""
    rows = db.q("SELECT id, title FROM goals WHERE company_id=? AND status='active' ORDER BY id", (company_id,))
    return ", ".join(f"#{g['id']} {g['title'] or ''}".rstrip() for g in rows)


def _int(value, name: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{name} must be a whole number.")
    try:
        return int(float(str(value).strip().lstrip("#")))
    except (TypeError, ValueError):
        raise ContractError(f"{name} must be a whole number.") from None


def _checks(text: str) -> str:
    lines = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()   # bullets and numbering a model adds
        if not line:
            continue
        if line.split()[0].rstrip(":") not in CHECK_TOOLS:
            raise ContractError(f"checks line '{line[:80]}' must start with one of: {', '.join(CHECK_TOOLS)} "
                                "(one check per line, for example 'browser_check site/index.html').")
        lines.append(line[:300])
    return "\n".join(lines[:10])


def _due_at(value) -> str:
    """An owner's deadline: YYYY-MM-DD (end of that day, UTC) or a full UTC timestamp; empty clears it."""
    s = str(value or "").strip()
    if not s:
        return ""
    if db.parse_time(s):
        return s
    try:
        return _dt.date.fromisoformat(s[:10]).isoformat() + "T23:59:59Z"
    except ValueError:
        raise ContractError("due_at must be a date like 2026-11-30.") from None


def validate(company_id: int, fields: dict, task: dict | None = None) -> dict:
    """Check the contract fields present in fields and return them as task columns (goal_id, expected, inputs,
    depends_on as a JSON list, due_at, budget_steps, budget_tokens, checks). task is the row being changed (None
    for a new task). Raises ContractError with text the model or the owner can act on. Only keys present are
    checked; goal_id 0, None or "" means no goal, and due_at (owners only) "" clears the deadline."""
    out: dict = {}
    if "goal_id" in fields:
        g = fields["goal_id"]
        if g in (None, "", 0, "0"):
            out["goal_id"] = None
        else:
            gid = _int(g, "goal_id")
            same = bool(task and task.get("goal_id") == gid)   # an edit may keep a goal that has closed since
            if not same and not db.one("SELECT id FROM goals WHERE id=? AND company_id=? AND status='active'",
                                       (gid, company_id)):
                goals = active_goal_list(company_id)
                raise ContractError(f"goal #{gid} is not an active goal of this company. "
                                    + (f"Active goals: {goals}." if goals else "There are no active goals; leave "
                                       "goal_id out."))
            out["goal_id"] = gid
    for key, cap in (("expected", 500), ("inputs", 1500)):
        if key in fields:
            out[key] = str(fields[key] or "").replace("\r\n", "\n").strip()[:cap]
    if "depends_on" in fields:
        deps = parse_ids(fields["depends_on"])
        kept = set(deps_of(task)) if task else set()   # an edit may keep a dependency that has finished since
        if task and task["id"] in deps:
            raise ContractError(f"task #{task['id']} cannot depend on itself.")
        for d in deps:
            row = db.one("SELECT id, company_id, status FROM tasks WHERE id=?", (d,))
            if not row or row["company_id"] != company_id:
                raise ContractError(f"depends_on: there is no task #{d} in this company.")
            if row["status"] in CLOSED and d not in kept:
                raise ContractError(f"depends_on: task #{d} is already {row['status']}, so there is nothing to wait "
                                    f"for. Leave it out.")
        cycle = _cycle_through(company_id, task["id"] if task else None, deps) if deps else None
        if cycle:
            raise ContractError("depends_on would make tasks wait on each other forever: "
                                + " -> ".join(f"#{n}" for n in cycle) + ".")
        out["depends_on"] = json.dumps(deps[:10])
    if fields.get("due_days") not in (None, "", 0, "0"):
        days = _int(fields["due_days"], "due_days")
        if not DUE_DAYS[0] <= days <= DUE_DAYS[1]:
            raise ContractError(f"due_days must be {DUE_DAYS[0]} to {DUE_DAYS[1]}.")
        out["due_at"] = _utc(_now() + _dt.timedelta(days=days))
    elif "due_at" in fields:
        out["due_at"] = _due_at(fields["due_at"])
    if fields.get("budget_steps") not in (None, "", 0, "0"):
        steps = _int(fields["budget_steps"], "budget_steps")
        if not BUDGET_STEPS[0] <= steps <= BUDGET_STEPS[1]:
            raise ContractError(f"budget_steps must be {BUDGET_STEPS[0]} to {BUDGET_STEPS[1]} (tool calls per "
                                "attempt).")
        out["budget_steps"] = steps
    elif "budget_steps" in fields and task is not None:
        out["budget_steps"] = 0   # an owner's edit cleared it
    if fields.get("budget_tokens") not in (None, "", 0, "0"):
        tokens = _int(fields["budget_tokens"], "budget_tokens")
        if tokens < 0:
            raise ContractError("budget_tokens must be 0 (no limit) or more.")
        out["budget_tokens"] = tokens
    elif "budget_tokens" in fields and task is not None:
        out["budget_tokens"] = 0
    if "checks" in fields:
        out["checks"] = _checks(fields["checks"])
    return out


def no_goal_warning(company_id: int, task_id: int) -> str | None:
    """create_task by the CEO with no goal while goals are active: the task exists, but the CEO hears about it."""
    goals = active_goal_list(company_id)
    if not goals:
        return None
    return (f"Created task #{task_id}, but it serves no goal. Add goal_id (active goals: {goals}) or it will be "
            f"cancelled at the weekly review.")


def task_tags(task: dict, statuses: dict[int, str] | None = None, goal: bool = True) -> str:
    """' (goal #1, due 2026-10-16, waiting on #12)' for a task line in list_tasks and the planning prompt; '' when
    the task has none of them. goal=False leaves the goal out (the line is already under its goal)."""
    tags = []
    if goal and task.get("goal_id"):
        tags.append(f"goal #{task['goal_id']}")
    if (task.get("due_at") or "").strip():
        tags.append(f"due {task['due_at'][:10]}")
    if task.get("status") not in CLOSED:
        waiting = blocked_by(task, statuses)
        if waiting:
            tags.append("waiting on " + ", ".join(f"#{d}" for d in waiting))
    return f" ({', '.join(tags)})" if tags else ""


def api_fields(task: dict, statuses: dict[int, str] | None = None) -> dict:
    """The contract as the dashboard reads it: depends_on as a list and blocked_by (ids not done or cancelled)."""
    return {"depends_on": deps_of(task), "blocked_by": blocked_by(task, statuses)}


# ---------------------------------------------------------------- prompts

def _goal_row(company_id: int, goal_id) -> dict | None:
    if not goal_id:
        return None
    from . import brain   # imported here: brain imports tools, which imports this module
    for g in brain.active_goals(company_id):
        if g["id"] == goal_id:
            return g
    return db.one("SELECT * FROM goals WHERE id=? AND company_id=?", (goal_id, company_id))


def task_contract_section(task: dict) -> str:
    """The contract lines at the top of a task prompt (goal, expected, inputs, due, budget, checks), plus a note for
    each dependency that was cancelled so the assignee can decide whether the task still makes sense."""
    from . import brain
    cid = task["company_id"]
    lines = []
    g = _goal_row(cid, task.get("goal_id"))
    expected = (task.get("expected") or "").strip()
    if g:
        if g.get("status", "active") == "active":
            goal = brain.goal_line(g)
        else:
            goal = f"#{g['id']} {g.get('title') or ''} (this goal is {g['status']})"
        lines.append(f"Goal: {goal}" + (f" (this task should: {expected})" if expected else ""))
    elif expected:
        lines.append(f"This task should: {expected}")
    if (task.get("inputs") or "").strip():
        lines.append(f"Inputs (read these first): {task['inputs'].strip()}")
    for d in deps_of(task):
        row = db.one("SELECT id, title, status, deliverable FROM tasks WHERE id=?", (d,))
        if not row:
            continue
        if row["status"] == "cancelled":
            lines.append(f"Note: task #{d} that this depended on was cancelled")
        elif row["status"] == "done":
            lines.append(f"Builds on: task #{d} {row['title']}"
                         + (f" (its result: {row['deliverable']})" if row.get("deliverable") else ""))
    due = db.parse_time(task.get("due_at"))
    if due:
        days = (due.date() - _now().date()).days
        when = "today" if days == 0 else (f"in {days} days" if days > 0 else f"{-days} days overdue")
        if days == 1:
            when = "tomorrow"
        lines.append(f"Due: {due.strftime('%Y-%m-%d %H:%M')} UTC ({when})")
    budget = []
    if int(task.get("budget_steps") or 0) > 0:
        company = db.get_company(cid) or {}
        steps = max_steps_for(company, task, int(db.get_settings().get("max_steps_per_run", 14))) if company \
            else int(task["budget_steps"])
        budget.append(f"{steps} tool calls")
    if int(task.get("budget_tokens") or 0) > 0:
        budget.append(f"{int(task['budget_tokens']):,} tokens across attempts ({int(task.get('tokens_used') or 0):,} "
                      "used)")
    if budget:
        lines.append("Budget: " + "; ".join(budget))
    checks = [c for c in (task.get("checks") or "").splitlines() if c.strip()]
    if checks:
        lines.append("Checks that will run when you finish: " + "; ".join(checks))
    if not lines:
        return ""
    return "Contract:\n" + "\n".join(lines)


def _num(v) -> str:
    if v is None:
        return "?"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def cycle_sections(company: dict) -> str:
    """Goals (with open tasks per goal and the no-goal line), experiments, brain proposals and decisions due, placed
    after Phase 0.5's header and before the open tasks in the planning prompt. The lists and their lines come from
    brain.py, so the planning prompt, the digest and brain_lookup read the brain the same way."""
    from . import brain, rhythms   # imported here: both import tools, which imports this module
    cid = company["id"]
    today = _now().date().isoformat()
    goals = brain.active_goals(cid)
    no_strategy = not brain.has_strategy(cid)
    counts = {r["goal_id"]: r["n"] for r in db.q(
        f"SELECT goal_id, COUNT(*) n FROM tasks WHERE company_id=? AND status IN ({','.join('?' * len(OPEN))}) "
        f"GROUP BY goal_id", (cid, *OPEN))}
    lines = []
    if goals:
        lines += [f"{brain.goal_line(g)}. Open tasks: {counts.get(g['id'], 0)}." for g in goals]
        # Routines, the owner's own tasks and replies to inbound email are exempt from the goal rule: the same
        # tasks the weekly review cancels (rhythms.after_weekly), so the CEO is never told to cancel a reply.
        exempt = rhythms.EXEMPT_CREATORS
        loose = [r["id"] for r in db.q(
            f"SELECT id FROM tasks WHERE company_id=? AND (goal_id IS NULL OR goal_id=0) AND COALESCE(routine, '')='' "
            f"AND COALESCE(created_by, '') NOT IN ({','.join('?' * len(exempt))}) AND status IN "
            f"({','.join('?' * len(OPEN))}) ORDER BY id", (cid, *exempt, *OPEN))]
        line = f"(no goal) Open tasks: {len(loose)}"
        if loose:
            ids = ", ".join(f"#{i}" for i in loose[:12]) + (", ..." if len(loose) > 12 else "")
            line += f" ({ids}). Attach a goal with update_task(goal_id=...) or cancel them."
        else:
            line += "."
        lines.append(line)
        if no_strategy:
            lines.append("The strategy is not written yet: call set_strategy with all seven fields from the charter.")
    elif no_strategy:
        lines.append("The company brain is empty: no strategy and no goals yet. Before planning tasks, call "
                     "set_strategy (one_liner, icp, problem, offer, price, positioning, voice) from the charter, "
                     "then set_goal one to three times, each a number with a date.")
    else:
        lines.append("No active goals. Call set_goal one to three times (a metric, a target number and a deadline) "
                     "before creating tasks.")
    parts = ["## Goals (plan from these; at most 3 active)\n" + "\n".join(lines)]

    exps = brain.running_experiments(cid, limit=10)   # verdict pending, soonest stop first
    due = [e for e in exps if brain.due_by(e.get("stop"), today)]
    running = [e for e in exps if e not in due]
    if exps or goals:
        ex = [f"Running: {brain.experiment_line(e)}" for e in running[:5]] or \
            ["Running: none. Write each bet as start_experiment(goal_id, hypothesis, metric, stop_days)."]
        if due:
            ex += [f"Due to conclude: {brain.experiment_line(e)}" for e in due[:5]]
            ex.append("Call conclude_experiment for each one that is due.")
        else:
            ex.append("Due to conclude: none.")
        parts.append("## Experiments\n" + "\n".join(ex))

    props = brain.pending_proposals(cid, 5)
    if props:
        # Each line capped: a proposal's text has no limit of its own, and five long ones would swamp the prompt.
        parts.append("## Brain proposals waiting for your review (review_brain_change)\n"
                     + "\n".join(brain._clip(brain.proposal_line(r), PROPOSAL_LINE_CAP) for r in props))

    decs = brain.decisions_due(cid)   # no outcome yet, review date today + 3 days or earlier
    if decs:
        parts.append("## Decisions due for review (log the outcome with log_decision, or propose a change)\n"
                     + "\n".join(brain.decision_line(d) for d in decs))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- budgets

def max_steps_for(company: dict, task: dict, default: int) -> int:
    """Tool calls for this task attempt: min(company steps_per_task or default, task budget_steps or 999)."""
    try:
        per_task = int(float(db.company_settings(company).get("steps_per_task") or 0))
    except (TypeError, ValueError):
        per_task = 0
    steps = per_task if per_task > 0 else int(default)
    own = int(task.get("budget_steps") or 0)
    if own > 0:
        steps = min(steps, own)
    return max(1, steps)


TOKEN_BUDGET_TEXT = "Token budget exhausted"


def start_problem(task: dict) -> str | None:
    """A reason the task must not start: its tokens_used has reached budget_tokens. The caller (agents.run) blocks
    the task with this text as its note; this asks the owner with the task_failed_twice options (skip, retry once
    more, pause). After "Retry once more with my note" the watchdog resets attempts to 0, and an attempt that
    starts from 0 always runs, so the owner's extra attempt is not refused straight away."""
    budget = int(task.get("budget_tokens") or 0)
    used = int(task.get("tokens_used") or 0)
    if budget <= 0 or used < budget or int(task.get("attempts") or 0) == 0:
        return None
    problem = f"{TOKEN_BUDGET_TEXT} ({used:,} of {budget:,} tokens used)."
    try:
        from . import watchdog   # imported here: watchdog imports tools, which imports this module
        company = db.get_company(task["company_id"])
        if company:
            recommended = "Retry once more with my note" if int(task.get("priority") or 2) == 1 else "Skip this task"
            watchdog.escalate(company, "task_failed_twice",
                              f"Task #{task['id']} '{task['title']}' used its token budget ({used:,} of {budget:,} "
                              f"tokens). What should we do?", watchdog.TASK_OPTIONS, recommended,
                              (task.get("notes") or "")[-1500:], task["id"], key=f"task_failed_twice:{task['id']}")
    except Exception:   # the question is a courtesy; the task is blocked either way
        traceback.print_exc()
    return problem


def after_run(task: dict | None, tokens: int) -> None:
    """Add the run's tokens to the task's tokens_used (agents._finalize)."""
    if task and tokens:
        db.run("UPDATE tasks SET tokens_used = COALESCE(tokens_used, 0) + ? WHERE id=?", (int(tokens), task["id"]))


def utc_midnight() -> str:
    """Today's start, UTC, in the db.now() format (the GPU budget resets at UTC midnight)."""
    return _now().strftime("%Y-%m-%dT00:00:00Z")


def gpu_hours(company_id: int, since: str) -> float:
    """Hours of model time used by runs that started at or after since (an ISO UTC string from db.now()); running
    runs count up to now. Shared by the GPU budget, the Overview and the weekly brief data."""
    total = 0.0
    for r in db.q("SELECT started_at, finished_at FROM runs WHERE company_id=? AND started_at >= ?", (company_id, since)):
        start = db.parse_time(r["started_at"])
        end = db.parse_time(r["finished_at"]) or _dt.datetime.now(_dt.timezone.utc)
        if start:
            total += max(0.0, (end - start).total_seconds()) / 3600
    return total


def gpu_budget(company: dict) -> float:
    """gpu_hours_per_day; 0 or less means no limit."""
    try:
        return float(db.company_settings(company).get("gpu_hours_per_day") or 0)
    except (TypeError, ValueError):
        return 0.0


def over_gpu_budget(company: dict) -> bool:
    """Has the company used its GPU hours for today (UTC)? The first time it crosses them in a day, the feed says
    so once."""
    budget = gpu_budget(company)
    if budget <= 0:
        return False
    used = gpu_hours(company["id"], utc_midnight())
    if used <= budget:
        return False
    day = utc_midnight()[:10]
    try:
        if db.company_settings(db.get_company(company["id"]) or company).get("gpu_notice") != day:
            db.update_company_settings(company["id"], gpu_notice=day)
            bus.emit(company["id"], "budget", f"{company['name']} used its GPU budget for today ({used:.1f} of "
                     f"{_num(budget)} h). Agents pick up tasks again after midnight UTC; chat and tasks you create "
                     "still run. Raise GPU hours per day in the company settings to continue today.", None,
                     {"gpu_hours": round(used, 2), "budget": budget})
    except Exception:
        traceback.print_exc()
    return True


def posts_this_week(company_id: int) -> int:
    """Social posts approved (done) or pending in the last 7 days, plus posts a policy of auto ran (audit rows
    without an approval): what the weekly post budget counts."""
    since = _utc(_now() - _dt.timedelta(days=7))
    queued = db.scalar("SELECT COUNT(*) FROM approvals WHERE company_id=? AND action='social_post' AND "
                       "(status='pending' OR (status='done' AND COALESCE(decided_at, created_at)>=?))",
                       (company_id, since)) or 0
    auto = db.scalar("SELECT COUNT(*) FROM audit WHERE company_id=? AND action='social_post' AND approval_id IS NULL "
                     "AND created_at>=? AND result NOT LIKE 'FAILED%'", (company_id, since)) or 0
    return int(queued) + int(auto)


def post_budget(company: dict) -> int:
    try:
        return max(0, int(float(db.company_settings(company).get("posts_per_week", 10))))
    except (TypeError, ValueError):
        return 10


def post_budget_problem(ctx) -> str | None:
    """social_post: the weekly post budget is used up."""
    limit = post_budget(ctx.company)
    if posts_this_week(ctx.company["id"]) >= limit:
        return f"Weekly post budget ({limit}) reached. Keep the draft in a file and try again next week."
    return None


def budget_status(company: dict) -> dict:
    """The Overview and the Budgets section: GPU hours used today against the budget, and the weekly posts."""
    from . import verify   # imported here: verify loads with tools, which imports this module at its end
    cs = db.company_settings(company)
    since_month = _now().strftime("%Y-%m-01T00:00:00Z")
    money = 0.0
    for r in db.q("SELECT payload FROM approvals WHERE company_id=? AND action='spend_money' AND status='done' AND "
                  "COALESCE(decided_at, created_at)>=?", (company["id"], since_month)):
        try:
            money += float((db.loads(r["payload"], {}) or {}).get("amount_usd") or 0)
        except (TypeError, ValueError):
            pass
    used = gpu_hours(company["id"], utc_midnight())
    budget = gpu_budget(company)
    return {"gpu_hours_today": round(used, 2), "gpu_hours_per_day": budget,
            "over_gpu_budget": bool(budget > 0 and used > budget),
            "posts_this_week": posts_this_week(company["id"]), "posts_per_week": post_budget(company),
            "emails_today": verify.emails_today(company["id"]),
            "daily_email_limit": verify.daily_email_limit(company),
            "steps_per_task": int(float(cs.get("steps_per_task") or 0)),
            "money_usd": float(cs.get("money_usd") or 0), "money_approved_month": round(money, 2)}


# Holdco creates the rhythms on schedule; a planning cycle that queues its own "daily brief" duplicates them.
_RHYTHM_TITLE = re.compile(r"\b(daily|evening|morning|weekly|monthly)\s+(brief|briefing|review|plan|memo|report|"
                           r"strategy check)\b", re.I)


def rhythm_task_problem(ctx, title: str) -> str | None:
    """create_task by an agent: a task that duplicates a rhythm is refused with what to do instead."""
    if ctx.kind == "founding" or not _RHYTHM_TITLE.search(title or ""):
        return None
    return ("ERROR: Holdco runs the morning plan, evening brief, weekly review and monthly check itself, on schedule "
            "(the owner can also start them from the Overview). Don't create tasks for them; plan the work that "
            "moves a goal.")
