"""Portfolio capital allocation (Phase 4, docs/phases/phase-4-scale.md section 2): the scorecard, the allocation
score and each company's share of the GPU, the monthly portfolio review, and GET /api/portfolio.

The scorecard's numbers come only from rows a machine wrote: connector metrics (a source other than manual), run
durations, purchases the owner approved and email headers. Nothing an agent typed moves a score.

Scheduler (order): Phase 3's class order stays. Inside classes 3 to 6 the next place goes to the company furthest
below its share of the runs started in the last hour (deficit), then by priority and id; a company with work and no
run started within its cycle_minutes goes first. One query per tick: the shares come from the scorecard, which is
recomputed at most every SHARE_SECONDS (and at once after a review answer or a new company).

Monthly review: built in code from the scorecard with a recommendation per company by rule (the small model only
adds an opening paragraph, in its own thread); one decision per active company with origin kind 'portfolio',
answered through apply_answer. Holdco never deletes a company: Shut down pauses it.

Only active and paused companies are scored, so Bench companies (status 'bench') never appear anywhere here.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import re
import threading
import time
import traceback

from . import bus, contracts, db, llm, notify
from .routing import route

WEIGHTS = {"progress": 0.4, "traction": 0.4, "freshness": 0.2}
SEED_DAYS = 14              # a company younger than this ...
SEED_SCORE = 0.5            # ... scores at least this, so it is not starved before it can have results
BOOST = 1.5                 # "Scale" in the monthly review multiplies the score by this ...
BOOST_DAYS = 30             # ... for this many days (company setting allocation_boost_until)
FRESH_DAYS = 14             # freshness falls from 1 to 0 over this many days since the last real-world result
DEFICIT_MINUTES = 60        # the shares are measured on the runs started in this window
SHARE_SECONDS = 300         # the shares are recomputed from the scorecard at most this often
WEIGHTED_CLASSES = (3, 4, 5, 6)   # Phase 3 classes ordered by deficit; 1 and 2 (follow-ups, due work) stay first
REVIEW_AGE_DAYS = 30        # the review is due once the oldest active company is this old ...
REVIEW_EVERY_DAYS = 28      # ... and no review was made in this many days
REVIEW_RETRY_SECONDS = 600  # a review that failed is tried again after this long, not on every tick
DECISION_HOURS = 7 * 24     # the review's decisions: a 7-day deadline
SCALE_SCORE, SCALE_DAYS = 0.6, 7                 # Scale: score at least this and a result within this many days
SHUT_AGE_DAYS, SHUT_RESULT_DAYS = 60, 45         # Shut down: older than this, no revenue, no result for this long
OPTIONS = {"scale": "Scale (more GPU share for 30 days)", "hold": "Hold", "shut_down": "Shut down (pause the company)"}
LABELS = {"scale": "Scale", "hold": "Hold", "shut_down": "Shut down"}
HOLDCO = {"id": None, "name": "Holdco"}   # who asks the review's decisions (no agent: the board, not a team)

# SQL: the row came from a connector. An empty source is manual, as everywhere else (rhythms, the digest, the API).
_CONNECTOR = "COALESCE(NULLIF(source, ''), 'manual') != 'manual'"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(t: _dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_since(stamp, now: _dt.datetime) -> float | None:
    t = db.parse_time(stamp)
    return max(0.0, (now - t).total_seconds() / 86400) if t else None


def _until(value) -> _dt.datetime | None:
    """allocation_boost_until as a time: a UTC stamp as the database writes it, or a date (to the end of that day)."""
    text = str(value or "").strip()
    if not text:
        return None
    t = db.parse_time(text)
    if t:
        return t
    try:
        day = _dt.date.fromisoformat(text[:10])
    except ValueError:
        return None
    return _dt.datetime.combine(day + _dt.timedelta(days=1), _dt.time(), _dt.timezone.utc)


def _minutes(value, default: float = 60.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


# =============================================================== the scorecard

def _latest(cid: int, name: str) -> float | None:
    """The newest value a connector wrote for this metric, or None when none did."""
    v = db.scalar(f"SELECT value FROM metrics WHERE company_id=? AND name=? AND {_CONNECTOR} "
                  "ORDER BY created_at DESC, id DESC LIMIT 1", (cid, name))
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _progress(current, target) -> float:
    """min(1, current / target) for one goal; 0 without a number yet."""
    try:
        cur, tgt = float(current), float(target)
    except (TypeError, ValueError):
        return 0.0
    if tgt <= 0:
        return 1.0 if cur >= tgt else 0.0
    return max(0.0, min(1.0, cur / tgt))


def _goal_progress(cid: int) -> tuple[float, int]:
    """The mean progress over the active goals (0 with none) and how many there are. brain.active_goals refreshes
    each goal's current from the latest metric named after it first."""
    from . import brain   # imported here: brain imports tools, which imports modules that import this one
    goals = brain.active_goals(cid)
    if not goals:
        return 0.0, 0
    return sum(_progress(g.get("current"), g.get("target")) for g in goals) / len(goals), len(goals)


def _dry_run(p: dict, result) -> bool:
    """Did an approved connector purchase buy nothing? The executor's own answer says so ("Dry run (nothing
    bought): ..."), since the card's dry run may have changed between the request and the approval; the request's
    dry_run flag decides only when there is no answer."""
    text = str(result or "").strip()
    return text.lower().startswith("dry run") if text else p.get("dry_run") is True


def _money_spent(cid: int, since: str) -> float:
    """Dollars of the purchases the owner approved since then: spend_money requests (the owner buys and records it)
    and connector calls that bought something themselves (Porkbun's register_domain carries amount_usd), unless
    they ran as a dry run."""
    total = 0.0
    for a in db.q("SELECT action, payload, result FROM approvals WHERE company_id=? AND status='done' AND action IN "
                  "('spend_money', 'connector_call') AND COALESCE(decided_at, created_at)>=?", (cid, since)):
        p = db.loads(a["payload"], {})
        p = p if isinstance(p, dict) else {}
        if a["action"] == "connector_call" and ("amount_usd" not in p or _dry_run(p, a["result"])):
            continue
        try:
            total += float(p.get("amount_usd") or 0)
        except (TypeError, ValueError):
            pass
    return total


def _last_results(ids: list[int]) -> dict[int, str]:
    """When each company last had a real-world result (a UTC stamp; absent when never):
    - a connector metric whose value differs from the previous row of the same name (the first row: from 0). Stripe's
      numbers count only as a payment: stripe.payments_30d going up (a 30-day window that drops is not a result);
    - an inbound email whose In-Reply-To names a Message-ID the company sent."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[int, str] = {}
    for r in db.q("SELECT company_id, MAX(created_at) AS at FROM (SELECT company_id, name, value, source, created_at, "
                  "LAG(value) OVER (PARTITION BY company_id, name ORDER BY created_at, id) AS prev FROM metrics WHERE "
                  f"company_id IN ({marks})) WHERE {_CONNECTOR} AND ((name NOT LIKE 'stripe.%' AND "
                  "value != COALESCE(prev, 0)) OR (name='stripe.payments_30d' AND value > COALESCE(prev, 0))) "
                  "GROUP BY company_id", ids):
        if r["at"]:
            out[r["company_id"]] = r["at"]
    for r in db.q("SELECT e.company_id, MAX(e.created_at) AS at FROM emails e WHERE e.direction='in' AND "
                  f"e.company_id IN ({marks}) AND COALESCE(e.in_reply_to, '')!='' AND EXISTS (SELECT 1 FROM emails o "
                  "WHERE o.company_id=e.company_id AND o.direction='out' AND COALESCE(o.message_id, '')!='' AND "
                  "instr(' ' || e.in_reply_to || ' ', ' ' || o.message_id || ' ')>0) GROUP BY e.company_id", ids):
        if r["at"] and r["at"] > out.get(r["company_id"], ""):
            out[r["company_id"]] = r["at"]
    return out


def _score(rows: list[dict]) -> None:
    """Add score and share to the scorecard rows (each signal scaled within the portfolio)."""
    active = [r for r in rows if r["status"] == "active"]
    top = max((math.log1p(r["revenue_30d"] or 0) for r in active), default=0.0)
    for r in rows:
        traction = min(1.0, math.log1p(r["revenue_30d"] or 0) / top) if top > 0 else 0.0
        days = r["days_since_result"]
        freshness = max(0.0, 1 - days / FRESH_DAYS) if days is not None else 0.0
        score = (WEIGHTS["progress"] * r["goal_progress"] + WEIGHTS["traction"] * traction
                 + WEIGHTS["freshness"] * freshness)
        if r["age_days"] < SEED_DAYS:
            score = max(score, SEED_SCORE)   # the seed period: not starved before it can have results
        if r["boost_until"]:
            score *= BOOST                   # the owner chose Scale in the last review
        r["score"] = score
    total = sum(r["score"] for r in active)
    for r in rows:
        if r["status"] != "active":
            r["share"] = 0.0
        else:   # nobody scores above 0 (no goals, revenue or results anywhere): an equal share each
            r["share"] = r["score"] / total if total > 0 else 1 / len(active)


def scorecard() -> list[dict]:
    """One row per active or paused company, highest score first: goal_progress, revenue_30d, mrr, gpu_hours_week,
    money_spent_30d, days_since_result, age_days, score, share (docs/phases/phase-4-scale.md section 2), with id,
    name, status, goals (active goals: 0 shows "no goals") and boost_until (the Scale boost while it lasts, else '').
    revenue_30d and mrr are None without a Stripe row, days_since_result None without any result yet."""
    companies = db.q("SELECT id, name, status, settings, created_at FROM companies WHERE status IN "
                     "('active', 'paused') ORDER BY id")
    if not companies:
        return []
    now = _now()
    week, month = _iso(now - _dt.timedelta(days=7)), _iso(now - _dt.timedelta(days=30))
    results = _last_results([c["id"] for c in companies])
    rows = []
    for c in companies:
        cid = c["id"]
        progress, goals = _goal_progress(cid)
        boost = _until(db.company_settings(c).get("allocation_boost_until"))
        revenue, mrr = _latest(cid, "stripe.revenue_usd_30d"), _latest(cid, "stripe.mrr_usd")
        rows.append({"id": cid, "name": c["name"], "status": c["status"], "goal_progress": progress, "goals": goals,
                     "revenue_30d": max(0.0, revenue) if revenue is not None else None,
                     "mrr": max(0.0, mrr) if mrr is not None else None,
                     "gpu_hours_week": contracts.gpu_hours(cid, week), "money_spent_30d": _money_spent(cid, month),
                     "days_since_result": _days_since(results.get(cid), now),
                     "age_days": _days_since(c["created_at"], now) or 0.0,
                     "boost_until": _iso(boost) if boost and boost > now else ""})
    _score(rows)
    for r in rows:
        for key, places in (("goal_progress", 3), ("revenue_30d", 2), ("mrr", 2), ("gpu_hours_week", 2),
                            ("money_spent_30d", 2), ("days_since_result", 1), ("age_days", 1), ("score", 3),
                            ("share", 6)):
            if r[key] is not None:
                r[key] = round(r[key], places)
    rows.sort(key=lambda r: (-r["score"], r["id"]))
    return rows


# =============================================================== the scheduler's share

_alloc_lock = threading.Lock()
_alloc: dict = {"at": None, "companies": {}}   # company id -> {"share", "cycle_minutes"}; "at": time.monotonic()


def invalidate() -> None:
    """Recompute the shares on the next tick (a review answer, a test)."""
    with _alloc_lock:
        _alloc["at"] = None


def allocation(need=()) -> dict[int, dict]:
    """Each active company's share and cycle_minutes, from the scorecard at most every SHARE_SECONDS (sooner when a
    company in need is not known yet)."""
    with _alloc_lock:
        at, companies = _alloc["at"], _alloc["companies"]
    if at is not None and time.monotonic() - at < SHARE_SECONDS and set(need) <= set(companies):
        return companies
    shares = {r["id"]: r["share"] for r in scorecard() if r["status"] == "active"}
    companies = {}
    for c in db.q("SELECT id, settings FROM companies WHERE status='active'"):
        if c["id"] in shares:
            companies[c["id"]] = {"share": shares[c["id"]],
                                  "cycle_minutes": _minutes(db.company_settings(c).get("cycle_minutes"))}
    with _alloc_lock:
        _alloc.update(at=time.monotonic(), companies=companies)
    return companies


def _task_key(t: dict) -> tuple:
    return (t["priority"] if t.get("priority") is not None else 2, t["id"])


def _weighted(rest: list[dict]) -> list[dict]:
    """Classes 3 to 6 (in Phase 3 order) reordered: starving companies first, then inside each class the company
    with the largest deficit, then priority and id. Each place handed out counts as a run started, so the next
    place goes to whoever is furthest behind then."""
    companies = allocation({t["company_id"] for t in rest})
    now = _now()
    longest = max([DEFICIT_MINUTES] + [companies.get(t["company_id"], {}).get("cycle_minutes", 60.0) for t in rest])
    # The one query per tick: runs started in the last hour and the newest start, per company.
    runs = {r["company_id"]: r for r in db.q(
        "SELECT company_id, SUM(CASE WHEN started_at>=? THEN 1 ELSE 0 END) AS recent, MAX(started_at) AS last "
        "FROM runs WHERE started_at>=? GROUP BY company_id",
        (_iso(now - _dt.timedelta(minutes=DEFICIT_MINUTES)), _iso(now - _dt.timedelta(minutes=longest))))}
    counts = {cid: int((runs.get(cid) or {}).get("recent") or 0) for cid in companies}
    total = sum(counts.values())
    first: dict[int, dict] = {}
    for t in rest:
        first.setdefault(t["company_id"], t)
    out, taken = [], set()
    # The starvation guard: work waiting and no run started within the company's cycle_minutes. Each such company's
    # first task goes ahead of the deficit order; among them, Phase 3 order (first holds the heads in queue order).
    for cid in first:
        minutes = companies.get(cid, {}).get("cycle_minutes", 60.0)
        last = (runs.get(cid) or {}).get("last") or ""
        if last and last >= _iso(now - _dt.timedelta(minutes=minutes)):
            continue
        out.append(first[cid])
        taken.add(first[cid]["id"])
        counts[cid] = counts.get(cid, 0) + 1
        total += 1

    def behind(cid: int) -> float:   # the deficit: share * runs started in the window - the company's own
        return round(companies.get(cid, {}).get("share", 0.0) * total - counts.get(cid, 0), 9)
    for cls in WEIGHTED_CLASSES:
        lanes: dict[int, list[dict]] = {}
        for t in rest:
            if t["dispatch_class"] == cls and t["id"] not in taken:
                lanes.setdefault(t["company_id"], []).append(t)
        while lanes:
            cid = min(lanes, key=lambda c: (-behind(c), _task_key(lanes[c][0])))
            out.append(lanes[cid].pop(0))
            if not lanes[cid]:
                del lanes[cid]
            counts[cid] = counts.get(cid, 0) + 1
            total += 1
    return out


def order(queue: list[dict], settings: dict | None = None) -> list[dict]:
    """Scheduler.tick: the dispatch queue (in Phase 3 class order) with classes 3 to 6 ordered by company deficit
    (share * runs started in the last hour - the company's own) and the starvation guard. Classes 1 and 2 keep
    their places. allocation_mode 'equal' returns the Phase 3 order unchanged; so does any failure here."""
    s = settings if settings is not None else db.get_settings()
    if str(s.get("allocation_mode") or "weighted").strip().lower() == "equal":
        return queue
    rest = [t for t in queue if t.get("dispatch_class") in WEIGHTED_CLASSES]
    if not rest:
        return queue
    try:
        return [t for t in queue if t.get("dispatch_class") not in WEIGHTED_CLASSES] + _weighted(rest)
    except Exception:   # the share is an optimisation: dispatch goes on in Phase 3 order
        traceback.print_exc()
        return queue


# =============================================================== the monthly review

REVIEW_SYSTEM = ("You write the opening paragraph of the owner's monthly portfolio review from the JSON you are "
                 "given: two or three plain sentences on which companies earn more GPU time, which to hold and which "
                 "to consider shutting down, and why. Use only facts and numbers that are in the JSON; never add a "
                 "number. No heading, no list.")


def _when(days) -> str:
    if days is None:
        return "never"
    n = int(days)
    return "today" if n < 1 else f"{n} day{'' if n == 1 else 's'} ago"


def _usd(v) -> str:
    if v is None:
        return "-"
    return f"${v:,.2f}".replace(".00", "")


def _pct(v) -> str:
    return f"{round((v or 0) * 100):d} %"


def recommendation(row: dict) -> tuple[str, str]:
    """The review's rule for one scorecard row: ('scale' | 'hold' | 'shut_down', why). The score it reads is the
    company's own, without a running Scale boost (a boost must not recommend itself)."""
    merit = row["score"] / BOOST if row.get("boost_until") else row["score"]
    days = row.get("days_since_result")
    if merit >= SCALE_SCORE and days is not None and days <= SCALE_DAYS:
        return "scale", f"score {merit:.2f} and a real-world result {_when(days)}"
    if row["age_days"] > SHUT_AGE_DAYS and not row.get("revenue_30d") and (days is None or days > SHUT_RESULT_DAYS):
        last = "no real-world result yet" if days is None else f"its last real-world result {_when(days)}"
        return "shut_down", f"{int(row['age_days'])} days old, no revenue in 30 days and {last}"
    return "hold", f"score {merit:.2f}, last real-world result {_when(days)}"


def _numbers(r: dict) -> str:
    """One company's scorecard as a sentence, for the phone and the decision's context."""
    goals = f"goal progress {_pct(r['goal_progress'])}" if r["goals"] else "no goals"
    return (f"Score {r['score']:.2f} (share {_pct(r['share'])}), {goals}, revenue 30 d {_usd(r['revenue_30d'])}, "
            f"MRR {_usd(r['mrr'])}, GPU {r['gpu_hours_week']:g} h this week, spent {_usd(r['money_spent_30d'])} "
            f"in 30 d, last result {_when(r['days_since_result'])}, {int(r['age_days'])} days old")


def _table(rows: list[dict], recs: dict) -> list[str]:
    out = ["| Company | Goal progress | Revenue 30 d | MRR | GPU h this week | Spent 30 d | Days since result | "
           "Score | Share | Recommendation |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        rec = LABELS[recs[r["id"]][0]] if r["id"] in recs else r["status"]
        days = "-" if r["days_since_result"] is None else f"{r['days_since_result']:g}"
        out.append(f"| {r['name']} | {_pct(r['goal_progress']) if r['goals'] else 'no goals'} | "
                   f"{_usd(r['revenue_30d'])} | {_usd(r['mrr'])} | {r['gpu_hours_week']:g} | "
                   f"{_usd(r['money_spent_30d'])} | {days} | {r['score']:.2f} | {_pct(r['share'])} | {rec} |")
    return out


def review_narrative(data: dict, settings: dict | None = None) -> str:
    """The review's opening paragraph ('summary' job on the small model). Raises LLMError when the model cannot be
    asked, and ValueError when the paragraph states a number the data does not hold (the review then goes without)."""
    from . import rhythms   # imported here: rhythms imports tools, which imports modules that import this one
    out = llm.strip_thinking(llm.run_job("summary", json.dumps(data, ensure_ascii=False, default=str)[:8000],
                                         REVIEW_SYSTEM, settings))
    text = " ".join(out.split())[:1200]
    if not text:
        raise llm.LLMError("the narrative came back empty")
    unknown = rhythms.unsupported_numbers(text, data)
    if unknown:
        raise ValueError(f"the narrative states numbers the data does not hold: {', '.join(unknown[:5])}")
    return text


def monthly_review(narrate: bool | None = None) -> int:
    """Build the monthly portfolio review in code: the scorecard table and each active company's recommendation by
    rule (Scale, Hold, Shut down). Stored as a briefs row with company_id NULL and kind 'portfolio', sent to the
    phone once, then one decision per active company (deadline 7 days; a Shut down recommendation is irreversible,
    so it waits for the owner). narrate (default: when a small model is set) adds the small model's opening
    paragraph, a model call: the scheduler builds a narrated review in its own thread. Returns the brief's id."""
    from . import decisions   # imported here: decisions imports tools, which imports modules that import this one
    rows = scorecard()
    asked = [r for r in rows if r["status"] == "active"]   # a paused company has nothing to scale or shut down
    recs = {r["id"]: recommendation(r) for r in asked}
    narrative = ""
    if llm.small_model() if narrate is None else narrate:
        facts = {"companies": [{**{k: r[k] for k in ("name", "status", "goal_progress", "goals", "revenue_30d", "mrr",
                                                      "gpu_hours_week", "money_spent_30d", "days_since_result",
                                                      "age_days", "score")},
                                "goal_progress_pct": round(r["goal_progress"] * 100),
                                "share_pct": round(r["share"] * 100),
                                "recommendation": LABELS[recs[r["id"]][0]] if r["id"] in recs else "",
                                "why": recs[r["id"]][1] if r["id"] in recs else ""} for r in rows],
                 "decisions_waiting": len(asked)}
        try:
            narrative = review_narrative(facts)
        except Exception as e:   # LLMError, a number the data does not hold, or anything else: no paragraph
            print(f"[portfolio] review narrative skipped: {e}")
    lines = [f"- **{r['name']}**: {LABELS[recs[r['id']][0]]}: {recs[r['id']][1]}." for r in asked]
    ask = (f"One decision per company is waiting for you ({len(asked)}): Scale gives it {BOOST:g} times its GPU share "
           f"for {BOOST_DAYS} days, Hold changes nothing, Shut down pauses it (nothing is deleted).")
    body = "\n".join(([narrative, ""] if narrative else []) + _table(rows, recs)
                     + ["", "## Recommendations"] + (lines or ["- No active company to review."]) + ["", ask])
    phone = "\n".join(([narrative, ""] if narrative else [])
                      + [f"- **{r['name']}**: {LABELS[recs[r['id']][0]]}. {_numbers(r)}."
                         for r in asked] + ["", ask])
    data = {"scorecard": rows, "weights": WEIGHTS, "narrative": bool(narrative),
            "recommendations": [{"company_id": r["id"], "name": r["name"], "recommendation": recs[r["id"]][0],
                                 "why": recs[r["id"]][1], "decision_id": None} for r in asked]}
    bid = db.run("INSERT INTO briefs(company_id, kind, body, data, created_at) VALUES(?,?,?,?,?)",
                 (None, "portfolio", body, json.dumps(data, ensure_ascii=False, default=str), db.now()))
    bus.emit(None, "brief", f"monthly portfolio review for {len(rows)} compan{'y' if len(rows) == 1 else 'ies'}",
             None, {"brief_id": bid, "kind": "portfolio"})
    bus.notify("briefs")
    day = _now().astimezone().date().isoformat()   # the owner's day, as the daily portfolio brief's title says it
    notify.send_later(notify.brief_text(f"Holdco monthly portfolio review, {day}", phone, notify.dashboard_url()),
                      kind="brief")
    for entry, r in zip(data["recommendations"], asked):
        company = db.get_company(r["id"])
        if not company:
            continue
        rec, why = recs[r["id"]]
        context = (f"{_numbers(r)}. Recommended {LABELS[rec]}: {why}. Scale gives it "
                   f"{BOOST:g} times its GPU share for {BOOST_DAYS} days; Shut down pauses the company and keeps "
                   f"everything (Holdco never deletes a company).")
        out = decisions.create(company, HOLDCO, None, f"{r['name']}: scale, hold or shut down?", list(OPTIONS.values()),
                               OPTIONS[rec], deadline_hours=DECISION_HOURS, reversible=rec != "shut_down",
                               context=context, origin={"kind": "portfolio", "brief_id": bid, "recommendation": rec})
        m = re.search(r"decision #(\d+)", out, re.I)
        if m and not out.startswith("ERROR"):
            entry["decision_id"] = int(m.group(1))
        else:
            print(f"[portfolio] no decision for {r['name']}: {out[:200]}")
    db.run("UPDATE briefs SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False, default=str), bid))
    return bid


_review_lock = threading.Lock()   # one review is built at a time
_review_failed = {"at": None}     # time.monotonic() of the last failure


def review_due() -> bool:
    """The oldest active company is at least REVIEW_AGE_DAYS old and no review was made in REVIEW_EVERY_DAYS. One
    query: the scheduler asks on every tick."""
    row = db.one("SELECT (SELECT MIN(created_at) FROM companies WHERE status='active') AS oldest, (SELECT "
                 "MAX(created_at) FROM briefs WHERE company_id IS NULL AND kind='portfolio') AS last") or {}
    now = _now()
    age = _days_since(row.get("oldest"), now)
    if age is None or age < REVIEW_AGE_DAYS:
        return False
    return not (row.get("last") and row["last"] >= _iso(now - _dt.timedelta(days=REVIEW_EVERY_DAYS)))


def monthly_review_if_due() -> bool:
    """Scheduler housekeeping (every tick, so one cheap query when not due): the monthly review when it is due.
    Without a small model it is built here (database work only); with one, in its own thread, because the
    narrative is a model call of up to a minute and the tick never waits on a model. Returns whether one started."""
    failed = _review_failed["at"]
    if failed is not None and time.monotonic() - failed < REVIEW_RETRY_SECONDS:
        return False
    if _review_lock.locked() or not review_due():
        return False
    if not _review_lock.acquire(blocking=False):
        return False

    def build(narrate: bool) -> None:
        try:
            if review_due():   # checked again under the lock: another build may have just made it
                monthly_review(narrate)
                _review_failed["at"] = None
        except Exception as e:   # tried again after REVIEW_RETRY_SECONDS; the scheduler goes on either way
            _review_failed["at"] = time.monotonic()
            print(f"[portfolio] monthly review failed: {type(e).__name__}: {e}")
        finally:
            _review_lock.release()
    if not llm.small_model():
        build(False)
        return True
    try:
        threading.Thread(target=build, args=(True,), daemon=True, name="holdco-portfolio-review").start()
    except Exception:
        _review_lock.release()
        raise
    return True


def apply_answer(company_id: int, origin: dict, answer: str) -> None:
    """decisions.on_decided for origin kind 'portfolio': Scale sets allocation_boost_until BOOST_DAYS ahead (a bigger
    GPU share); Hold changes nothing; Shut down pauses the company with a note in its feed (never deletes it)."""
    said = str(answer or "").strip().lower()
    company = db.get_company(company_id)
    if not company:
        return
    origin = origin if isinstance(origin, dict) else {}
    if said.startswith("scale"):
        until = _iso(_now() + _dt.timedelta(days=BOOST_DAYS))
        db.update_company_settings(company_id, allocation_boost_until=until)
        invalidate()
        bus.emit(company_id, "info", f"Portfolio review: Scale. The company gets {BOOST:g} times its share of the GPU "
                                     f"until {until[:10]}.", None,
                 {"by": "portfolio", "brief_id": origin.get("brief_id"), "allocation_boost_until": until})
        bus.notify("companies")
    elif said.startswith("shut") and company["status"] not in ("paused", "archived", "bench"):
        db.update("companies", company_id, status="paused")
        invalidate()
        bus.emit(company_id, "info", "paused the company on the owner's portfolio review (Shut down). Nothing was "
                                     "deleted: resume it from the company page whenever you like.", None,
                 {"by": "portfolio", "brief_id": origin.get("brief_id")})
        bus.notify("companies")


# =============================================================== API

def _brief(row: dict | None) -> dict | None:
    if row:
        row["data"] = db.loads(row.get("data"), {}) or {}
    return row


def _review(row: dict | None) -> dict | None:
    """The review brief with its decisions as they stand now (pending, answered or dismissed)."""
    row = _brief(row)
    if not row:
        return None
    ids = [e.get("decision_id") for e in row["data"].get("recommendations") or [] if isinstance(e, dict)]
    ids = [i for i in ids if isinstance(i, int)]
    found = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for a in db.q("SELECT a.id, a.company_id, a.status, a.result, a.decided_by, a.decided_at, a.payload, c.name "
                      f"FROM approvals a LEFT JOIN companies c ON c.id=a.company_id WHERE a.id IN ({marks})", ids):
            p = db.loads(a.pop("payload"), {}) or {}
            name = a.pop("name") or ""
            found[a["id"]] = {**a, "company": name, "question": p.get("question", ""),
                              "options": p.get("options", []), "recommended": p.get("recommended", ""),
                              "reversible": bool(p.get("reversible", True)), "deadline_at": p.get("deadline_at", ""),
                              "answer": a["result"] if a["status"] == "answered" else ""}
    row["decisions"] = [found[i] for i in ids if i in found]
    return row


@route("GET", "/api/portfolio")
def api_portfolio(h, m, body):
    """The Portfolio page: the scorecard (highest score first), the latest daily portfolio brief and the latest
    monthly review with its decisions; allocation_mode says whether the shares steer the scheduler."""
    s = db.get_settings()
    return {"scorecard": scorecard(),
            "latest_brief": _brief(db.one("SELECT * FROM briefs WHERE company_id IS NULL AND kind='daily' "
                                          "ORDER BY id DESC LIMIT 1")),
            "latest_review": _review(db.one("SELECT * FROM briefs WHERE company_id IS NULL AND kind='portfolio' "
                                            "ORDER BY id DESC LIMIT 1")),
            "allocation_mode": "equal" if str(s.get("allocation_mode") or "").strip().lower() == "equal"
            else "weighted",
            "weights": dict(WEIGHTS)}
