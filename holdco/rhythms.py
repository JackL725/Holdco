"""Rhythms (Phase 2, docs/phases/phase-2-organize.md section 4): data that code gathers for the morning plan, the
evening brief, the weekly review and the monthly check; the write_brief tool; the portfolio brief; the weekly
clean-up of tasks that serve no goal.

The model never invents a number. Code reads the database, the task prompt shows the result as JSON under
"## Data for this brief (the only numbers you may use)", and write_brief refuses, once, a brief that states a
number the data does not hold (the anti-fabrication rule of Phase 0.5's claims gate).

The data stored with a brief is the dict the prompt showed. task_data_section() keeps it per task in memory
(_shown) and write_brief() saves that same dict. Recomputing it at write time would not be the same thing: the
windows move with the clock, and task and approval states change during the run (the brief's own task is one of
them). A dict that is missing (a restart, which also rebuilds the prompt, or a brief written outside its routine
task) is recomputed.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import re
import threading
from collections import Counter

from . import brain, bus, contracts, db, llm, notify, roles
from .routing import HTTPError, company_or_404, route, to_int
from .tools import Ctx, ToolError, S, I, tool  # noqa: F401

MORNING_KEY = "ceo:Morning plan"
EVENING_KEYS = ("analyst:Evening brief", "ceo:Evening brief")
WEEKLY_KEY = "ceo:Weekly review"
MONTHLY_KEY = "ceo:Monthly strategy check"
RHYTHM_KEYS: set[str] = {MORNING_KEY, *EVENING_KEYS, WEEKLY_KEY, MONTHLY_KEY}
BRIEF_KINDS = ("daily", "weekly", "monthly")
KIND_OF = {MORNING_KEY: "", EVENING_KEYS[0]: "daily", EVENING_KEYS[1]: "daily", WEEKLY_KEY: "weekly",
           MONTHLY_KEY: "monthly"}
BRIEF_LABEL = {"daily": "daily brief", "weekly": "weekly memo", "monthly": "monthly memo"}


def brief_kind(task: dict | None) -> str:
    """The brief a rhythm task must store ("daily", "weekly", "monthly"), or "" for other tasks and the morning plan."""
    return KIND_OF.get((task or {}).get("routine") or "", "")


def stored_brief(company_id: int, task: dict | None) -> dict | None:
    """The newest brief of the task's kind stored since the task was created (in this run or an earlier attempt)."""
    kind = brief_kind(task)
    if not kind:
        return None
    return db.one("SELECT id, kind, created_at FROM briefs WHERE company_id=? AND kind=? AND created_at>=? "
                  "ORDER BY id DESC LIMIT 1", (company_id, kind, (task or {}).get("created_at") or ""))


# POST /api/companies/<id>/rhythms/<key>/run: the routine keys to try, in order (the evening brief is the analyst's
# when the company has one, otherwise the CEO's).
RUN_KEYS = {"morning": (MORNING_KEY,), "evening": EVENING_KEYS, "weekly": (WEEKLY_KEY,), "monthly": (MONTHLY_KEY,)}

DATA_CAP = 12000              # characters of JSON in the prompt
OPEN = ("todo", "in_progress", "blocked", "waiting")
METRIC_NAMES = 40             # newest metric names in a data block
CANCEL_NOTE = "[Weekly review] Cancelled: serves no goal"
EXEMPT_CREATORS = ("owner", "routine", "inbox")   # never cancelled for having no goal (inbox: a customer email)
PORTFOLIO_FRESH_HOURS = 20    # every active company has a daily brief this recent: the portfolio brief goes out
PORTFOLIO_WAIT_HOURS = 2      # or this long after the first company brief of the day, whoever is missing

_shown: dict[int, tuple[str, dict]] = {}   # task id -> (routine key, the data its prompt showed)
_shown_lock = threading.Lock()


# =============================================================== helpers

def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(t: _dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(**delta) -> str:
    return _iso(_now() - _dt.timedelta(**delta))


def _today() -> str:
    return _now().date().isoformat()


def _local_date(stamp: str | None = None) -> str:
    """The owner's local date, of now or of a UTC timestamp: the portfolio brief goes out once per local day."""
    t = db.parse_time(stamp) if stamp else _now()
    return (t or _now()).astimezone().date().isoformat()


def _num(v):
    """A number as the prompt should show it: 18 rather than 18.0, at most four decimals."""
    if v is None or isinstance(v, bool):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return int(f) if f.is_integer() else round(f, 4)


def _diff(a, b):
    return _num(float(a) - float(b)) if a is not None and b is not None else None


def _dump(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def _connector(sql_alias: str = "") -> str:
    """SQL: the row came from a connector. An empty source is manual, as everywhere else (the digest, the API)."""
    col = f"{sql_alias}source" if sql_alias else "source"
    return f"COALESCE(NULLIF({col}, ''), 'manual') != 'manual'"


def _latest(cid: int, name: str, before: str = "", connector_only: bool = False) -> dict | None:
    sql = "SELECT id, value, source, note, created_at FROM metrics WHERE company_id=? AND name=?"
    params: list = [cid, name]
    if before:
        sql += " AND created_at<=?"
        params.append(before)
    if connector_only:
        sql += " AND " + _connector()
    return db.one(sql + " ORDER BY created_at DESC, id DESC LIMIT 1", params)


def _metric_names(cid: int, since: str, where: str = "") -> list[str]:
    rows = db.q("SELECT name, MAX(created_at) AS at FROM metrics WHERE company_id=? AND created_at>=?"
                + (f" AND {where}" if where else "") + " GROUP BY name ORDER BY at DESC LIMIT ?",
                (cid, since, METRIC_NAMES))
    return [r["name"] for r in rows]


def _pending_owner(cid: int) -> tuple[list[dict], list[dict]]:
    """(escalations, decisions_needed): the watchdog's pending questions, and every pending decision and question
    with its recommendation and deadline. Newest first."""
    escalations, needed = [], []
    for a in db.q("SELECT id, action, payload FROM approvals WHERE company_id=? AND status='pending' AND "
                  "action IN ('decision','question') ORDER BY id DESC LIMIT 20", (cid,)):
        p = db.loads(a["payload"], {}) or {}
        question = str(p.get("question") or "")[:300]
        entry = {"id": a["id"], "kind": a["action"], "question": question,
                 "options": [str(o) for o in p.get("options") or []], "recommended": str(p.get("recommended") or "")}
        if a["action"] == "decision":
            entry["deadline_at"] = p.get("deadline_at") or ""
            entry["deadline"] = notify.deadline_text(p)
            entry["reversible"] = bool(p.get("reversible", True))
        needed.append(entry)
        if a["action"] == "question" and p.get("pattern"):
            escalations.append({"id": a["id"], "question": question, "recommended": entry["recommended"]})
    return escalations, needed


def _approvals_pending(cid: int) -> dict:
    return {r["action"]: r["n"] for r in db.q("SELECT action, COUNT(*) AS n FROM approvals WHERE company_id=? AND "
                                             "status='pending' GROUP BY action ORDER BY action", (cid,))}


def _connectors_down(company: dict) -> list[dict]:
    try:
        from . import connectors   # imported here: connectors imports tools, which imports this module
        return [{"service": c["label"], "detail": c["detail"]} for c in connectors.connected(company)
                if c.get("status") == "bad"]
    except Exception as e:   # a broken connector list costs this line, never the brief
        print(f"connector status for the brief failed: {e}")
        return []


def _stuck(company: dict, escalations: list[dict]) -> dict:
    tasks = db.q("SELECT id, title, role, status, goal_id FROM tasks WHERE company_id=? AND status IN "
                 "('blocked','waiting') ORDER BY updated_at DESC, id DESC LIMIT 20", (company["id"],))
    return {"tasks": tasks, "escalations": escalations, "connectors_down": _connectors_down(company)}


def _goal_rows(cid: int) -> list[dict]:
    return [{"id": g["id"], "title": g.get("title") or "", "metric": g.get("metric") or "",
             "current": _num(g.get("current")), "target": _num(g.get("target")), "days_left": g.get("days_left"),
             "source": g.get("current_source") or ""} for g in brain.active_goals(cid)]


# =============================================================== data gatherers

def daily_data(company: dict) -> dict:
    """The evening brief's data, from the database only (keys as in the spec's table)."""
    cid = company["id"]
    t24, t7 = _ago(hours=24), _ago(days=7)
    metrics, anomalies = [], []
    for name in _metric_names(cid, t24):
        last = _latest(cid, name)
        if not last:
            continue
        day, week = _latest(cid, name, before=t24), _latest(cid, name, before=t7)
        v24 = day["value"] if day else None
        v7 = week["value"] if week else None
        metrics.append({"name": name, "value": _num(last["value"]), "source": last["source"] or "manual",
                        "at": last["created_at"], "value_24h_ago": _num(v24), "delta_24h": _diff(last["value"], v24),
                        "value_7d_ago": _num(v7), "delta_7d": _diff(last["value"], v7)})
        # An anomaly needs a baseline: at least 3 earlier values in the last 7 days.
        rows = db.q("SELECT value FROM metrics WHERE company_id=? AND name=? AND created_at>=? AND id!=? AND "
                    "created_at<=? ORDER BY created_at, id", (cid, name, t7, last["id"], last["created_at"]))
        earlier = [float(r["value"]) for r in rows if r["value"] is not None]
        if len(earlier) >= 3 and last["value"] is not None:
            mean = sum(earlier) / len(earlier)
            change = (float(last["value"]) - mean) / abs(mean) if mean else None
            if (change is None and float(last["value"]) != 0) or (change is not None and abs(change) > 0.5):
                anomalies.append({"name": name, "value": _num(last["value"]), "mean_7d": _num(round(mean, 2)),
                                  "change_pct": None if change is None else int(round(change * 100)),
                                  "source": last["source"] or "manual"})
    shipped = db.q("SELECT id, title, role, deliverable, goal_id FROM tasks WHERE company_id=? AND "
                   "status='done' AND updated_at>=? ORDER BY updated_at DESC, id DESC LIMIT 30", (cid, t24))
    escalations, needed = _pending_owner(cid)
    return {"metrics": metrics, "anomalies": anomalies, "shipped": shipped, "stuck": _stuck(company, escalations),
            "decisions_needed": needed, "goals": _goal_rows(cid), "approvals_pending": _approvals_pending(cid)}


def morning_data(company: dict) -> dict:
    """The morning plan's data: goals with their gap, open tasks per goal, blocked tasks, pending escalations and
    decisions, yesterday's brief."""
    cid = company["id"]
    goals = []
    for g in _goal_rows(cid):
        g["gap"] = _diff(g["target"], g["current"])
        goals.append(g)
    titles = {g["id"]: g["title"] for g in goals}
    rhythm = ",".join("?" * len(RHYTHM_KEYS))
    rows = db.q(f"SELECT id, title, role, status, priority, goal_id FROM tasks WHERE company_id=? AND status IN "
                f"({','.join('?' * len(OPEN))}) AND COALESCE(routine, '') NOT IN ({rhythm}) "
                f"ORDER BY updated_at DESC, id DESC LIMIT 60", (cid, *OPEN, *sorted(RHYTHM_KEYS)))
    groups: dict = {}
    for t in rows:
        groups.setdefault(t["goal_id"] or None, []).append(t)
    order = [gid for gid in titles if gid in groups] + [gid for gid in groups if gid not in titles]
    by_goal = [{"goal_id": gid, "goal": titles.get(gid) or ("(no goal)" if gid is None else "(not an active goal)"),
                "tasks": groups[gid]} for gid in order]
    escalations, needed = _pending_owner(cid)
    y = db.one("SELECT id, body, created_at FROM briefs WHERE company_id=? AND kind='daily' ORDER BY id DESC LIMIT 1",
               (cid,))
    return {"goals": goals, "open_tasks_by_goal": by_goal,
            "blocked_tasks": [t for t in rows if t["status"] in ("blocked", "waiting")],
            "escalations": escalations, "decisions_needed": needed,
            "yesterday_brief": {"id": y["id"], "created_at": y["created_at"], "body": (y["body"] or "")[:2500]}
            if y else None}


def weekly_data(company: dict) -> dict:
    """The weekly review's data. Progress comes only from connector metrics; rows an agent typed in (source
    'manual') are listed under self_reported and not used for scoring."""
    cid = company["id"]
    week = _ago(days=7)
    goals = []
    for g in brain.active_goals(cid):
        now_m = _latest(cid, g.get("metric") or "", connector_only=True)
        then_m = _latest(cid, g.get("metric") or "", before=week, connector_only=True)
        cur = now_m["value"] if now_m else None
        prev = then_m["value"] if then_m else None
        goals.append({"id": g["id"], "title": g.get("title") or "", "metric": g.get("metric") or "",
                      "current": _num(cur), "last_week": _num(prev), "change": _diff(cur, prev),
                      "target": _num(g.get("target")), "days_left": g.get("days_left"),
                      "source": (now_m or {}).get("source") or ""})
    metrics = []
    for name in _metric_names(cid, week, _connector()):
        last = _latest(cid, name, connector_only=True)
        prev = _latest(cid, name, before=week, connector_only=True)
        pv = prev["value"] if prev else None
        metrics.append({"name": name, "value": _num(last["value"]), "source": last["source"], "at": last["created_at"],
                        "last_week": _num(pv), "change": _diff(last["value"], pv)})
    self_reported = []
    for name in _metric_names(cid, week, "NOT (" + _connector() + ")"):
        m = db.one("SELECT value, note, created_at FROM metrics WHERE company_id=? AND name=? AND NOT ("
                   + _connector() + ") ORDER BY created_at DESC, id DESC LIMIT 1", (cid, name))
        if m:
            self_reported.append({"name": name, "value": _num(m["value"]), "at": m["created_at"],
                                  "note": (m["note"] or "")[:160]})
    # Experiments past their stop date and decisions due for review today or earlier, read the way the digest and
    # the planning prompt read them (brain.py: date part only), newest first like every list here.
    today = _today()
    experiments = [{k: e.get(k) for k in ("id", "goal_id", "hypothesis", "metric", "stop")}
                   for e in brain.running_experiments(cid, limit=200) if brain.due_by(e.get("stop"), today)][::-1][:10]
    reviews = [{**{k: r.get(k) for k in ("id", "what", "expected", "review_at", "decided_by")},
                "reversible": bool(r.get("reversible", 1))}
               for r in brain.decisions_due(cid, days=0, limit=200)][::-1][:10]
    open_sql = (f"SELECT id, title, role, status, created_by FROM tasks WHERE company_id=? AND status IN "
                f"({','.join('?' * len(OPEN))}) AND (goal_id IS NULL OR goal_id=0)")
    goalless = db.q(open_sql + f" AND COALESCE(created_by, '') NOT IN ({','.join('?' * len(EXEMPT_CREATORS))})"
                    " AND COALESCE(routine, '')='' ORDER BY id DESC LIMIT 30", (cid, *OPEN, *EXEMPT_CREATORS))
    owner_goalless = db.q(open_sql + " AND created_by='owner' ORDER BY id DESC LIMIT 20", (cid, *OPEN))
    titles = {g["id"]: g["title"] for g in goals}
    done: dict = {}
    for t in db.q("SELECT id, title, deliverable, goal_id FROM tasks WHERE company_id=? AND status='done' AND "
                  "updated_at>=? ORDER BY updated_at DESC, id DESC LIMIT 60", (cid, week)):
        done.setdefault(t.pop("goal_id") or None, []).append(t)
    per_goal = [{"goal_id": gid, "goal": titles.get(gid) or ("(no goal)" if gid is None else "(not an active goal)"),
                 "done": len(ts), "tasks": ts} for gid, ts in done.items()]
    try:
        from . import conn_resend   # the email numbers come from the emails table, whatever sends them
        emails = {"sent": conn_resend.sent_7d(cid), "replies": conn_resend.replies_7d(cid)[0]}
    except Exception as e:
        print(f"email numbers for the brief failed: {e}")
        emails = {}
    tokens = db.scalar("SELECT COALESCE(SUM(tokens), 0) FROM runs WHERE company_id=? AND started_at>=?", (cid, week))
    escalations, needed = _pending_owner(cid)
    return {"goals": goals, "metrics": metrics, "self_reported": self_reported, "experiments_due": experiments,
            "decisions_due": reviews, "goalless_tasks": goalless, "owner_tasks_without_goal": owner_goalless,
            "done_per_goal": per_goal, "emails": emails,
            "usage": {"gpu_hours": round(contracts.gpu_hours(cid, week), 2), "tokens": int(tokens or 0)},
            "stuck": _stuck(company, escalations), "decisions_needed": needed,
            "approvals_pending": _approvals_pending(cid)}


def monthly_data(company: dict) -> dict:
    """The monthly strategy check's data: the last 4 weekly memos' numbers, the revenue trend, goals hit or
    missed, money spent and GPU hours in the last 30 days."""
    cid = company["id"]
    month = _ago(days=30)
    weekly = []
    for b in db.q("SELECT id, data, created_at FROM briefs WHERE company_id=? AND kind='weekly' ORDER BY id DESC "
                  "LIMIT 4", (cid,)):
        d = db.loads(b["data"], {}) or {}
        weekly.append({"id": b["id"], "created_at": b["created_at"],
                       "goals": [{k: g.get(k) for k in ("title", "current", "target")} for g in d.get("goals") or []
                                 if isinstance(g, dict)],
                       "metrics": [{k: m.get(k) for k in ("name", "value", "source")} for m in d.get("metrics") or []
                                   if isinstance(m, dict)]})
    revenue = []
    for name in _metric_names(cid, month, _connector() + " AND (lower(name) LIKE '%revenue%' OR lower(name) LIKE "
                                                          "'%mrr%')"):
        last = _latest(cid, name, connector_only=True)
        entry = {"name": name, "source": last["source"], "now": _num(last["value"])}
        for weeks in (1, 2, 3, 4):
            old = _latest(cid, name, before=_ago(days=7 * weeks), connector_only=True)
            entry[f"{weeks}_weeks_ago"] = _num(old["value"]) if old else None
        revenue.append(entry)
    brain.active_goals(cid)   # refreshes current from the metrics first
    goals = []
    for g in db.q("SELECT * FROM goals WHERE company_id=? AND (status='active' OR created_at>=? OR "
                  "COALESCE(deadline, '')>=?) ORDER BY id DESC LIMIT 12", (cid, month, month[:10])):
        cur, tgt = g.get("current"), g.get("target")
        hit = g["status"] == "done" or (cur is not None and tgt is not None and float(cur) >= float(tgt))
        late = bool(g.get("deadline")) and str(g["deadline"])[:10] < _today()
        result = "hit" if hit else "missed" if g["status"] == "dropped" or late else "open"
        goals.append({"id": g["id"], "title": g.get("title") or "", "status": g["status"], "current": _num(cur),
                      "target": _num(tgt), "deadline": g.get("deadline") or "", "result": result})
    spent = 0.0
    bought = db.q("SELECT payload FROM approvals WHERE company_id=? AND action='spend_money' AND status='done' AND "
                  "decided_at>=?", (cid, month))
    for a in bought:
        try:
            spent += float((db.loads(a["payload"], {}) or {}).get("amount_usd") or 0)
        except (TypeError, ValueError):
            pass
    tokens = db.scalar("SELECT COALESCE(SUM(tokens), 0) FROM runs WHERE company_id=? AND started_at>=?", (cid, month))
    return {"weekly_briefs": weekly, "revenue_trend": revenue, "goals": goals,
            "money_spent": {"approved_usd": _num(round(spent, 2)), "purchases": len(bought),
                            "budget_usd": _num(db.company_settings(company).get("money_usd") or 0)},
            "gpu_hours": round(contracts.gpu_hours(cid, month), 1), "tokens": int(tokens or 0)}


GATHERERS = {MORNING_KEY: morning_data, EVENING_KEYS[0]: daily_data, EVENING_KEYS[1]: daily_data,
             WEEKLY_KEY: weekly_data, MONTHLY_KEY: monthly_data}
KEY_FOR_KIND = {"daily": EVENING_KEYS[0], "weekly": WEEKLY_KEY, "monthly": MONTHLY_KEY}


def _lists_of_dicts(x, out: list) -> list:
    if isinstance(x, dict):
        for v in x.values():
            _lists_of_dicts(v, out)
    elif isinstance(x, list):
        if x and all(isinstance(i, dict) for i in x):
            out.append(x)
        for v in x:
            _lists_of_dicts(v, out)
    return out


def _shorten(x, n: int):
    if isinstance(x, dict):
        return {k: _shorten(v, n) for k, v in x.items()}
    if isinstance(x, list):
        return [_shorten(v, n) for v in x]
    return x[:n] + "..." if isinstance(x, str) and len(x) > n else x


def fit(data: dict, cap: int = DATA_CAP) -> dict:
    """A copy of data whose pretty JSON fits in cap characters. Every list is newest first, so the last entry of
    the longest list (its oldest) is dropped, again and again; long texts are shortened last."""
    data = copy.deepcopy(data)
    while len(_dump(data)) > cap:
        lists = [lst for lst in _lists_of_dicts(data, []) if lst]
        if not lists:
            break
        max(lists, key=len).pop()
    for n in (600, 200, 80):
        if len(_dump(data)) <= cap:
            break
        data = _shorten(data, n)
    return data


def gather(company: dict, key: str) -> dict:
    """The data block of a rhythm routine (by its routine key), capped for the prompt."""
    return fit(GATHERERS[key](company))


# =============================================================== the prompt

def _format(focus: str) -> str:
    return ("Write it in Markdown with these five headings, in this order: What moved (numbers with their source), "
            f"What shipped (with file paths), What is stuck, Decisions needed (each with the recommendation), "
            f"{focus}. Under 350 words. Every number must come from the data above; leave out anything the data "
            "does not show.")


def instruction(company: dict, key: str) -> str:
    """What the routine's agent does with its data block."""
    name = company.get("name") or "the company"
    if key == MORNING_KEY:
        return ("Plan today from the data above. For each goal, choose the top 3 priorities that close the gap "
                "between current and target. Add them at the top of docs/daily-plan.md under a heading with today's "
                "date (read the file first: the newest plan goes on top and the older ones stay below it). Queue the "
                "work that is not already open with create_task, each with goal_id and expected. Raise each blocker "
                "once: ask_decision when the owner must choose between options, ask_owner for a simple question. "
                "Use only numbers from the data above. Then finish with deliverable docs/daily-plan.md.")
    if key in EVENING_KEYS:
        return ("Write today's one-page brief from the data above (the last 24 hours, compared with 7 days ago) and "
                "store it with write_brief(kind=\"daily\", body=...). " + _format("Tomorrow's focus")
                + " The stored brief is your deliverable; then call finish.")
    if key == WEEKLY_KEY:
        return ("Run the weekly review from the data above (the last 7 days). First conclude every experiment in "
                "experiments_due (conclude_experiment) and log an outcome for every decision in decisions_due "
                "(log_decision). Give each task in goalless_tasks a goal with update_task(goal_id=...) or cancel "
                "it: the ones still without a goal are cancelled when this review finishes. Adjust the goals if the "
                "numbers say so. Score progress only from goals and metrics, which come from connected services; "
                "self_reported numbers were typed in by agents and do not count. Then write the board memo with "
                "write_brief(kind=\"weekly\", body=...). " + _format("Next week's focus")
                + " When owner_tasks_without_goal is not empty, list those tasks under a heading Owner tasks "
                "without a goal. Then call finish.")
    if key == MONTHLY_KEY:
        return (f"Run the monthly strategy check for {name} from the data above (the last 30 days and the 4 newest "
                f"weekly memos). Decide whether to keep the company as it is, change course or stop it, and ask the "
                f"owner once with ask_decision: question \"Keep, change or kill {name}?\", options \"Keep\", "
                f"\"Change: <the change in one line>\" and \"Kill (pause the company)\", one per line; recommended "
                f"is the option you recommend; reversible=false (it waits for the owner however long it takes; Kill "
                f"pauses the company and deletes nothing); context gives the numbers behind your recommendation. "
                f"Then write the monthly memo with write_brief(kind=\"monthly\", body=...). "
                + _format("Next month's focus") + " Then call finish.")
    return ""


def task_data_section(company: dict, task: dict) -> str:
    """'## Data for this brief (the only numbers you may use)' for a rhythm task: the data as pretty JSON (at most
    DATA_CAP characters, oldest entries dropped first) and the routine's instruction. Empty for other tasks."""
    key = (task or {}).get("routine") or ""
    if key not in RHYTHM_KEYS:
        return ""
    data = gather(company, key)
    with _shown_lock:
        _shown[task["id"]] = (key, data)
        while len(_shown) > 200:
            _shown.pop(next(iter(_shown)))
    return ("## Data for this brief (the only numbers you may use)\n" + _dump(data) + "\n\n### What to do\n"
            + instruction(company, key))


# =============================================================== the anti-fabrication check

_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.I)
# File paths and names: the part before the first slash has a letter (so "$9/month" keeps its number).
_PATH_RE = re.compile(r"(?<![\w$€£])[\w.-]*[A-Za-z_][\w.-]*/[\w./-]*|\b[\w-]+\.(?:md|markdown|html?|css|js|json|csv|"
                      r"txt|py|pdf|png|jpe?g|svg|xml|ya?ml)\b", re.I)
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_RES = [re.compile(p, re.I) for p in (
    r"\b\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?Z?)?",             # 2026-10-14, with a time
    r"\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b",                                       # 14/10/2026
    rf"\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b",             # Oct 14, 2026
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}(?:,?\s+\d{{4}})?",      # 14 Oct 2026
    r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]\.?m\.?)?",                           # 18:00, 6:30 pm
    r"\b\d{1,2}\s?[ap]\.?m\b\.?",                                               # 6 pm
    r"(?<![$€£])\b(?:19|20)\d{2}\b",                                            # a year ($2000 is money)
)]
_HEADING_MARK = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_LIST_MARK = re.compile(r"^\s*(?:[-*+•]\s+)?\(?\d{1,2}[.)]\s", re.M)    # "1. ", "2) ", "- 3. "
# A number may carry a size suffix ($1.5k, 10M, $2bn) or be a multiple (3x): those are claims too.
_NUM_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(k|K|m|M|bn|BN|x|X)?(?!\w|[.,]\d)")
_SCALE = {"k": 1e3, "m": 1e6, "bn": 1e9}
_NAME_RE = re.compile(r"[A-Za-z][\w]*(?:\.[\w]+)*")   # a metric or key name such as email.sent_7d


def _numbers(text: str) -> list[tuple[str, float, float]]:
    """numbers_in with each number's rounding room: 0 for a number written out, half its last digit times the
    suffix for a scaled one ($1.5k is anything from 1,450 to 1,550)."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _URL_RE.sub(" ", t)
    t = _PATH_RE.sub(" ", t)
    for rx in _DATE_RES:
        t = rx.sub(" ", t)
    t = _HEADING_MARK.sub("", t)
    t = _LIST_MARK.sub(" ", t)
    out = []
    for m in _NUM_RE.finditer(t):
        raw = m.group(1) + (m.group(2) or "") + (m.group(3) or "")
        scale = _SCALE.get((m.group(3) or "").lower(), 1.0)
        try:
            value = float((m.group(1) + (m.group(2) or "")).replace(",", "")) * scale
        except ValueError:
            continue
        room = 0.5 * scale * 10 ** -len((m.group(2) or ".")[1:]) if scale > 1 else 0.0
        out.append((raw, value, room))
    return out


def numbers_in(text: str) -> list[tuple[str, float]]:
    """The numbers a text states, as (as written, value). Leaves out what is not a claim: dates and times, list
    numbering, file paths, URLs, and digits inside names (sent_7d, week3.md, 1st). $1.5k counts as 1500."""
    return [(raw, value) for raw, value, _room in _numbers(text)]


def _data_numbers(x, out: set) -> set:
    """Every number in a data block plus the ones a brief may fairly derive: list lengths, counts by status or
    role, sums, the percentage between a value and an earlier one, a goal's progress and gap."""
    if isinstance(x, bool) or x is None:
        return out
    if isinstance(x, (int, float)):
        out.add(abs(float(x)))
    elif isinstance(x, str):
        out.update(v for _raw, v in numbers_in(x))
        if _NAME_RE.fullmatch(x):   # a metric's window is part of its name: "30 days" for stripe.revenue_usd_30d
            out.update(float(d) for d in re.findall(r"\d+", x))
    elif isinstance(x, dict):
        nums = {k: float(v) for k, v in x.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if nums and len(nums) == len(x):   # a table of counts, like approvals_pending: its size and its total
            out.update((float(len(x)), abs(sum(nums.values()))))
        value = nums.get("value", nums.get("current", nums.get("now")))
        if value is not None:
            for k, prev in nums.items():
                if k.endswith("_ago") or k in ("last_week", "mean_7d"):
                    out.add(abs(value - prev))
                    if prev:
                        out.add(abs((value - prev) / prev * 100))
            target = nums.get("target")
            if target:
                out.add(abs(value / target * 100))
                out.add(abs(target - value))
        for v in x.values():
            _data_numbers(v, out)
    elif isinstance(x, list):
        out.add(float(len(x)))
        items = [i for i in x if isinstance(i, dict)]
        for field in ("status", "role", "kind", "action", "source", "goal_id", "result", "verdict"):
            out.update(float(n) for n in Counter(i.get(field) for i in items if field in i).values())
        for v in x:
            _data_numbers(v, out)
    return out


def _known(n: float, allowed: set) -> bool:
    for a in allowed:
        if abs(n - a) <= max(0.011, 0.005 * a) or round(a) == n or round(a, 1) == n or round(a, 2) == n:
            return True
    return False


def unsupported_numbers(body: str, data: dict, extra: str = "") -> list[str]:
    """Numbers the brief states that the data block does not hold (as written, first occurrence each). 0 and 1
    always pass; so do the numbers in the routine's own instruction (extra), such as its 350 words or 7 days."""
    allowed = _data_numbers(data, {0.0, 1.0})
    allowed.update(v for _raw, v in numbers_in(extra))
    out: list[str] = []
    for raw, value, room in _numbers(body):
        if raw in out or _known(abs(value), allowed) or (room and any(abs(abs(value) - a) <= room for a in allowed)):
            continue
        out.append(raw)
    return out


def _data_for(ctx: Ctx, kind: str) -> tuple[dict, str]:
    """The data block this run's prompt showed for a brief of this kind, or a fresh one (see the module
    docstring). Returns (data, routine key)."""
    task_id = (ctx.task or {}).get("id")
    with _shown_lock:
        entry = _shown.get(task_id) if task_id else None
    if entry and KIND_OF.get(entry[0]) == kind:
        return entry[1], entry[0]
    key = KEY_FOR_KIND[kind]
    return gather(ctx.company, key), key


# =============================================================== write_brief

@tool("write_brief", "Store this run's brief (daily, weekly or monthly). Use only numbers from the data block in your "
      "task; the brief is sent to the owner's phone and pinned on the dashboard.",
      {"kind": S("daily, weekly or monthly", enum=BRIEF_KINDS), "body": S("The brief in Markdown, under 350 words")},
      ("kind", "body"), risk="write")
def write_brief(ctx: Ctx, kind: str, body: str) -> str:
    kind = (kind or "").strip().lower()
    if kind not in BRIEF_KINDS:
        return "ERROR: kind must be daily, weekly or monthly."
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return "ERROR: the brief is empty. Write it in Markdown, under 350 words, then call write_brief again."
    company, cid = ctx.company, ctx.company["id"]
    data, key = _data_for(ctx, kind)
    if not ctx.brief_warned:
        unknown = unsupported_numbers(body, data, instruction(company, key))
        if unknown:
            # Once per run, like the claims gate: the second attempt is stored as written.
            ctx.brief_warned = True
            listed = ", ".join(unknown[:15]) + (f" and {len(unknown) - 15} more" if len(unknown) > 15 else "")
            bus.emit(cid, "claims", f"refused brief (numbers): {listed}"[:300], ctx.agent.get("id"),
                     {"where": "brief", "rule": "numbers", "problem": listed[:600], "count": len(unknown)})
            return (f"ERROR: not stored: the brief states numbers that are not in the data block: {listed}. Use only "
                    f"the numbers under '## Data for this brief' in your task (or leave those sentences out), then "
                    f"call write_brief again.")
    bid = db.run("INSERT INTO briefs(company_id, kind, body, data, created_at) VALUES(?,?,?,?,?)",
                 (cid, kind, body[:20000], json.dumps(data, ensure_ascii=False, default=str), db.now()))
    ctx.brief_written = kind
    bus.emit(cid, "brief", f"wrote the {BRIEF_LABEL[kind]}", ctx.agent.get("id"), {"brief_id": bid, "kind": kind})
    bus.notify("briefs", company_id=cid)
    notify.send_later(notify.brief_text(f"{company['name']}: {BRIEF_LABEL[kind]}", body,
                                        notify.dashboard_url(f"c/{cid}")), kind="brief")
    return f"Brief #{bid} stored ({kind}) and sent to the owner. It is this task's deliverable: call finish now."


# =============================================================== routine tasks, the weekly clean-up

def create_routine_task(company: dict, agent: dict, r: dict, key: str, created_by: str = "routine",
                        priority: int | None = None) -> int:
    """Insert a routine's task (the scheduler's recurring duties, and Run now from the Overview)."""
    ts = db.now()
    tid = db.run("INSERT INTO tasks(company_id, title, description, role, agent_id, status, priority, created_by, "
                 "deliverable, routine, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (company["id"], r["title"], r.get("description") or "", agent["role"], agent["id"], "todo",
                  priority or r.get("priority") or 2, created_by, r.get("deliverable") or "", key, ts, ts))
    what = "run now, asked by the owner" if created_by == "owner" else "recurring duty"
    bus.emit(company["id"], "task", f"{what} for {agent['name']}: {r['title']}", None, {"task_id": tid, "routine": key})
    bus.notify("tasks", company_id=company["id"])
    return tid


def after_run(company: dict, task: dict | None, ctx, status: str) -> None:
    """After a run (agents._finalize): forget the data block it showed; when the CEO's weekly review finished,
    cancel the agent-created tasks that still serve no goal."""
    if not task:
        return
    with _shown_lock:
        _shown.pop(task.get("id"), None)
    if task.get("routine") != WEEKLY_KEY or status in ("error", "stopped"):
        return
    if getattr(ctx, "finished", False) or status == "done":
        after_weekly(company)


def after_weekly(company: dict) -> list[int]:
    """Cancel every open task that has no goal, was created by an agent and is not a routine. Tasks the owner
    created stay (the memo lists them), and so do replies to inbound email. Returns the cancelled ids."""
    cid = company["id"]
    rows = db.q(f"SELECT id, result FROM tasks WHERE company_id=? AND status IN ({','.join('?' * len(OPEN))}) AND "
                f"(goal_id IS NULL OR goal_id=0) AND COALESCE(created_by, '') NOT IN "
                f"({','.join('?' * len(EXEMPT_CREATORS))}) AND COALESCE(routine, '')='' ORDER BY id",
                (cid, *OPEN, *EXEMPT_CREATORS))
    ts = db.now()
    for t in rows:
        db.update("tasks", t["id"], status="cancelled", updated_at=ts,
                  result=((t["result"] or "") + "\n" + CANCEL_NOTE).strip()[-6000:])
    ids = [t["id"] for t in rows]
    if ids:
        bus.emit(cid, "task", f"weekly review cancelled {len(ids)} task{'s' if len(ids) != 1 else ''} that serve no "
                 f"goal: " + ", ".join(f"#{i}" for i in ids), None, {"task_ids": ids})
        bus.notify("tasks", company_id=cid)
    return ids


# =============================================================== the portfolio brief

def _active_companies() -> list[dict]:
    return db.q("SELECT * FROM companies WHERE status='active' ORDER BY id")


def portfolio_brief_if_due() -> int | None:
    """Once per local day, when every active company has a daily brief less than PORTFOLIO_FRESH_HOURS old, or
    PORTFOLIO_WAIT_HOURS after the first company brief of the day. Returns the new brief's id. The scheduler calls
    portfolio_brief_in_background instead: with a small model set, the brief waits for its narrative."""
    companies = _portfolio_due()
    return portfolio_brief(companies) if companies is not None else None


_portfolio_lock = threading.Lock()   # one portfolio brief is built at a time


def portfolio_brief_in_background() -> bool:
    """Scheduler (every tick, so cheap when not due): when the portfolio brief is due, build it in its own thread.
    Phase 3: its narrative is a small-model call of up to a minute, and the tick (watchdog, phone poller, dispatch)
    must never wait on a model. Returns whether a build started."""
    if _portfolio_lock.locked() or _portfolio_due() is None:
        return False
    if not _portfolio_lock.acquire(blocking=False):
        return False

    def build() -> None:
        try:
            portfolio_brief_if_due()   # due is checked again: another process may have sent it meanwhile
        except Exception as e:   # the next tick tries again; the scheduler keeps going either way
            print(f"[rhythms] portfolio brief failed: {type(e).__name__}: {e}")
        finally:
            _portfolio_lock.release()
    try:
        threading.Thread(target=build, daemon=True, name="holdco-portfolio-brief").start()
    except Exception:
        _portfolio_lock.release()
        raise
    return True


def _portfolio_due() -> list[dict] | None:
    """The active companies, when the portfolio brief is due (see portfolio_brief_if_due); otherwise None."""
    last = db.one("SELECT created_at FROM briefs WHERE company_id IS NULL AND kind='daily' ORDER BY id DESC LIMIT 1")
    if last and _local_date(last["created_at"]) == _local_date():
        return None
    # Today's company briefs only (the owner's day): one that came in late last night is not a reason to send a
    # portfolio brief at 1 am.
    midnight = _dt.datetime.combine(_dt.date.fromisoformat(_local_date()), _dt.time()).astimezone()
    briefs = db.q("SELECT company_id, MIN(created_at) AS first, MAX(created_at) AS newest FROM briefs WHERE "
                  "company_id IS NOT NULL AND kind='daily' AND created_at>=? GROUP BY company_id",
                  (_iso(midnight.astimezone(_dt.timezone.utc)),))
    if not briefs:
        return None
    companies = _active_companies()
    active = {c["id"] for c in companies}
    by_company = {b["company_id"]: b for b in briefs if b["company_id"] in active}
    if not by_company:
        return None
    fresh = _ago(hours=PORTFOLIO_FRESH_HOURS)
    everyone = all(c["id"] in by_company and by_company[c["id"]]["newest"] >= fresh for c in companies)
    waited = min(b["first"] for b in by_company.values()) <= _ago(hours=PORTFOLIO_WAIT_HOURS)
    if not (everyone or waited):
        return None
    return companies


def _move(m: dict) -> float:
    """How much a metric moved in 24 hours, relative to where it was (a move from zero ranks first)."""
    before = m.get("value_24h_ago")
    return abs(m["delta_24h"]) / abs(before) if before else float("inf")


NARRATIVE_SYSTEM = ("You write the opening paragraph of the owner's daily portfolio brief from the JSON you are given: "
                    "two or three plain sentences on what moved, what shipped and what needs the owner. Use only facts "
                    "and numbers that are in the JSON; never add a number. No heading, no list.")


def portfolio_narrative(data: dict, settings: dict | None = None) -> str:
    """The portfolio brief's narrative paragraph ('summary' job, Phase 3). Raises LLMError when the model cannot be
    asked, and ValueError when the paragraph states a number the data does not hold (the brief is then sent
    without it)."""
    out = llm.strip_thinking(llm.run_job("summary", json.dumps(data, ensure_ascii=False, default=str)[:8000],
                                         NARRATIVE_SYSTEM, settings))
    text = " ".join(out.split())[:1200]
    if not text:
        raise llm.LLMError("the narrative came back empty")
    unknown = unsupported_numbers(text, data)
    if unknown:
        raise ValueError(f"the narrative states numbers the data does not hold: {', '.join(unknown[:5])}")
    return text


def portfolio_brief(companies: list[dict] | None = None) -> int:
    """Build the portfolio brief in code: per company the goal lines, the top 3 metric moves, shipped, stuck and
    decisions needed; then the decisions waiting for the owner. Stored with company_id NULL and sent to the phone.
    Phase 3: with a small model set, its narrative paragraph (numbers from the data only) opens the brief."""
    companies = _active_companies() if companies is None else companies
    lines, entries, waiting = [], [], 0
    for c in companies:
        d = daily_data(c)
        goals = [brain.goal_line(g) for g in brain.active_goals(c["id"])]
        moves = sorted((m for m in d["metrics"] if m.get("delta_24h")), key=_move, reverse=True)[:3]
        stuck = sum(len(v) for v in d["stuck"].values())
        needed = db.scalar("SELECT COUNT(*) FROM approvals WHERE company_id=? AND status='pending' AND action IN "
                           "('decision','question')", (c["id"],)) or 0
        waiting += needed
        entries.append({"id": c["id"], "name": c["name"], "goals": goals,
                        "top_moves": [{k: m[k] for k in ("name", "value", "delta_24h", "source")} for m in moves],
                        "shipped": len(d["shipped"]), "stuck": stuck, "decisions_needed": needed})
        lines.append(f"## {c['name']}")
        lines += [f"- Goal {g}" for g in goals] or ["- No goals set yet"]
        lines += [f"- {m['name']} {m['value']} ({m['delta_24h']:+g} in 24 h) [{m['source']}]" for m in moves]
        lines.append(f"- Shipped {len(d['shipped'])}, stuck {stuck}, decisions needed {needed}")
        lines.append("")
    lines.append(f"Decisions waiting for you: {waiting}")
    data = {"companies": entries, "decisions_waiting": waiting}
    narrative = ""
    if llm.small_model():   # Phase 3: the small model writes the opening paragraph from the same data
        try:
            narrative = portfolio_narrative(data)
        except Exception as e:   # LLMError, a number the data does not hold, or anything else: no paragraph
            print(f"[rhythms] portfolio narrative skipped: {e}")
    body = "\n".join(([narrative, ""] if narrative else []) + lines)
    bid = db.run("INSERT INTO briefs(company_id, kind, body, data, created_at) VALUES(?,?,?,?,?)",
                 (None, "daily", body, json.dumps(data, ensure_ascii=False, default=str), db.now()))
    bus.emit(None, "brief", f"portfolio brief for {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}",
             None, {"brief_id": bid, "kind": "daily"})
    bus.notify("briefs")
    notify.send_later(notify.brief_text(f"Holdco portfolio brief, {_local_date()}", body, notify.dashboard_url()),
                      kind="brief")
    return bid


# =============================================================== API

def _brief_row(r: dict | None) -> dict | None:
    if r:
        r["data"] = db.loads(r.get("data"), {})
    return r


@route("GET", "/api/briefs")
def api_briefs(h, m, body):
    """Briefs, newest first. company_id 0 means the portfolio brief; no company_id means all."""
    raw = (h.query.get("company_id", [""])[0] or "").strip()
    sql, params = "SELECT * FROM briefs WHERE 1=1", []
    if raw:
        cid = to_int(raw)
        sql += " AND company_id=?" if cid else " AND company_id IS NULL"
        params += [cid] if cid else []
    kind = (h.query.get("kind", [""])[0] or "").strip()
    if kind:
        if kind not in BRIEF_KINDS:
            raise HTTPError(400, "kind must be daily, weekly or monthly")
        sql += " AND kind=?"
        params.append(kind)
    limit = max(1, min(to_int(h.query.get("limit", [20])[0], 20), 100))
    return {"briefs": [_brief_row(r) for r in db.q(sql + f" ORDER BY id DESC LIMIT {limit}", params)]}


@route("GET", "/api/briefs/latest")
def api_briefs_latest(h, m, body):
    """The newest brief of each kind for a company (company_id 0: the portfolio)."""
    cid = to_int(h.query.get("company_id", [0])[0])
    where = "company_id=?" if cid else "company_id IS NULL"
    out: dict = {"company_id": cid}
    for kind in BRIEF_KINDS:
        out[kind] = _brief_row(db.one(f"SELECT * FROM briefs WHERE {where} AND kind=? ORDER BY id DESC LIMIT 1",
                                      (cid, kind) if cid else (kind,)))
    return out


def routine_def(agent: dict, key: str) -> dict:
    """The routine a key names in the agent's preset; a plain stand-in when the preset has none (the data
    section carries the instructions either way)."""
    title = key.split(":", 1)[1]
    for r in roles.role_info(agent).get("routines", []):
        if r["title"] == title:
            return r
    return {"title": title, "description": f"{title}: follow the instructions under the data block below.",
            "priority": 2, "deliverable": "docs/daily-plan.md" if key == MORNING_KEY else "", "requires": ""}


@route("POST", r"/api/companies/(\d+)/rhythms/(\w+)/run")
def api_rhythm_run(h, m, body):
    """Run now: create the rhythm's task at once, for the analyst or the CEO. An open one is returned instead."""
    c = company_or_404(m.group(1))
    keys = RUN_KEYS.get(m.group(2))
    if not keys:
        raise HTTPError(400, "Rhythm must be morning, evening, weekly or monthly")
    team = db.q("SELECT * FROM agents WHERE company_id=? AND enabled=1 ORDER BY id", (c["id"],))
    agent, key = None, ""
    for key in keys:
        agent = next((a for a in team if roles.preset_key(a) == key.split(":", 1)[0]), None)
        if agent:
            break
    if not agent:
        raise HTTPError(400, "No enabled agent can run this: the company needs its CEO (or an analyst).")
    open_task = db.one(f"SELECT * FROM tasks WHERE company_id=? AND routine=? AND status IN "
                       f"({','.join('?' * len(OPEN))}) ORDER BY id DESC LIMIT 1", (c["id"], key, *OPEN))
    if open_task:
        return {"task": open_task, "created": False}
    tid = create_routine_task(c, agent, routine_def(agent, key), key, created_by="owner", priority=1)
    return {"task": db.get_task(tid), "created": True}
