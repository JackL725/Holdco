"""Company brain (Phase 2, docs/phases/phase-2-organize.md section 1): strategy, goals, contacts, decisions,
experiments, lessons and proposals as structured records; a digest under 6,000 characters that every agent reads
first; the tools that read and change them; the API behind the Brain tab.

Who writes what: the CEO writes directly (set_strategy, set_goal, drop_goal, log_decision, the experiment tools and
review_brain_change); every other role proposes (propose_brain_change) and the CEO reviews the proposal in its
planning cycle. A proposal that changes the strategy's price, offer, positioning or ICP goes to the owner as a
decision instead. Lessons and, for the roles that message people, contacts are cheap and reversible, so those
write directly. The owner edits everything from the Brain tab.

The shared helpers (goal_line, active_goals, proposal_line, decision_line, experiment_line and the lists behind
them) are also used by contracts.py for the planning prompt and by rhythms.py for the brief data.
"""
from __future__ import annotations

import datetime as _dt
import json
import re

from . import bus, db, roles
from . import context as ctxsys
from .routing import HTTPError, company_or_404, route, to_int
from .tools import Ctx, S, I, tool

DIGEST_CAP = 6000
CHARTER_LINE = "Charter: docs/charter.md (read it with read_file when you need the long form)"
EMPTY_LINE = "Brain not written yet; the CEO fills it in the next planning cycle"

STRATEGY_FIELDS = ("one_liner", "icp", "problem", "offer", "price", "positioning", "voice")
STRATEGY_LABELS = {"one_liner": "One-liner", "icp": "Customer (ICP)", "problem": "Problem", "offer": "Offer",
                   "price": "Price", "positioning": "Positioning", "voice": "Voice"}
OWNER_FIELDS = ("price", "offer", "positioning", "icp")   # strategy changes the owner decides, never the CEO
CONTACT_STAGES = ("lead", "contacted", "replied", "customer", "lost")
GOAL_STATUSES = ("active", "done", "dropped")
VERDICTS = ("pending", "won", "lost", "inconclusive")
DECIDERS = ("ceo", "owner", "telegram", "deadline")
MAX_ACTIVE_GOALS = 3
LESSON_CAP = 300
LOOKUP_ROWS = 20
# Contacts' email addresses reach only the prompts of roles that message people (spec, Open questions 3).
EMAIL_ROLES = frozenset({"sales", "support", "success", "lifecycle", "partnerships", "ceo"})

# Sections an agent can look up or propose to, and the table behind each.
SECTIONS = ("strategy", "goals", "contacts", "decisions", "experiments", "lessons", "proposals")
TABLES = {"goals": "goals", "contacts": "contacts", "decisions": "decisions", "experiments": "experiments",
          "lessons": "lessons", "proposals": "brain_changes"}
# The columns each section's rows may set, with the kind of value each takes. Every write (owner routes,
# proposals, tools) goes through these lists, so a column name never comes from a request or a model.
COLUMNS = {
    "strategy": {f: "text" for f in STRATEGY_FIELDS},
    "goals": {"title": "text", "metric": "metric", "target": "number", "current": "number", "deadline": "date",
              "owner_role": "role", "status": "goal_status"},
    "contacts": {"name": "text", "email": "email", "company": "text", "role": "text", "stage": "stage",
                 "source": "text", "consent": "text", "unsubscribed": "flag", "last_touch": "date", "notes": "long"},
    "decisions": {"what": "text", "why": "long", "expected": "text", "review_at": "date", "outcome": "long",
                  "decided_by": "decider", "reversible": "flag"},
    "experiments": {"goal_id": "goal", "hypothesis": "text", "metric": "metric", "start": "date", "stop": "date",
                    "result": "long", "verdict": "verdict"},
    "lessons": {"text": "lesson"},
}
REQUIRED = {"goals": ("title",), "decisions": ("what",), "experiments": ("hypothesis",), "lessons": ("text",)}
# Text columns brain_lookup searches (email only for roles that may see it).
SEARCH_COLUMNS = {"goals": ("title", "metric", "owner_role", "status"),
                  "contacts": ("name", "email", "company", "role", "stage", "source", "notes"),
                  "decisions": ("what", "why", "expected", "outcome", "decided_by"),
                  "experiments": ("hypothesis", "metric", "result", "verdict"),
                  "lessons": ("text",), "proposals": ("section", "payload", "reason", "status")}
SECTION_ALIASES = {"brain_changes": "proposals", "changes": "proposals", "proposal": "proposals", "crm": "contacts",
                   "leads": "contacts", "kpis": "goals", "bets": "experiments"}

# Digest: each section is trimmed to its own cap (characters, and per line) before the whole is cut at DIGEST_CAP.
# The caps add up to just under the limit, so the final cut is only a safety net; strategy and goals come first
# and always fit whole.
SECTION_CAPS = {"strategy": 1260, "goals": 570, "metrics": 1100, "decisions": 830, "experiments": 770,
                "lessons": 700, "contacts": 160, "proposals": 425}
LINE_CAPS = {"strategy": 175, "goals": 180, "metrics": 100, "decisions": 160, "experiments": 150, "lessons": 110,
             "contacts": 150, "proposals": 125}
# In the digest the long text inside a line (a title, a hypothesis) is cut first, so the numbers, dates and names
# at the end of the line survive; brain_lookup shows the full text.
SHORT = {"title": 70, "what": 55, "expected": 35, "hypothesis": 65, "summary": 55, "reason": 30}
METRIC_NAMES = 20
DUE_DAYS = 3
SHOWN = {"decisions": 5, "experiments": 5, "lessons": 8, "proposals": 3}


# ---------------------------------------------------------------- small helpers

def _today() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def _stamp(days: int = 0) -> str:
    """db.now() format, days from now."""
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:max(0, n - 3)].rstrip() + "..."


def _parse_date(value) -> _dt.date | None:
    s = str(value or "").strip().lower()
    if s in ("today", "now"):
        return _today()
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _days_left(g: dict):
    d = _parse_date(g.get("deadline"))
    return (d - _today()).days if d else None


def latest_metric(company_id: int, name: str) -> dict | None:
    """The newest metrics row with this name (value, source, created_at), or None."""
    if not name:
        return None
    return db.one("SELECT value, source, created_at FROM metrics WHERE company_id=? AND name=? "
                  "ORDER BY created_at DESC, id DESC LIMIT 1", (company_id, name))


def active_goals(company_id: int) -> list[dict]:
    """Active goals, oldest first, with current refreshed from the latest metrics row named after the goal's metric
    (written back to goals.current) and days_left until the deadline (None without a valid date). Shared by the
    digest, contracts (planning prompt, create_task validation) and rhythms (brief data)."""
    out = []
    for g in db.q("SELECT * FROM goals WHERE company_id=? AND status='active' ORDER BY id", (company_id,)):
        m = latest_metric(company_id, g.get("metric") or "")
        if m is not None and m["value"] != g.get("current"):
            db.run("UPDATE goals SET current=? WHERE id=?", (m["value"], g["id"]))
            g["current"] = m["value"]
        g["current_source"] = (m or {}).get("source") or ""
        g["days_left"] = _days_left(g)
        out.append(g)
    return out


def _num(v) -> str:
    if v is None:
        return "?"
    return str(int(v)) if float(v).is_integer() else f"{float(v):g}"


def goal_line(g: dict) -> str:
    """'#1 10 paying customers by 2026-12-01: stripe.customers 2 of 10, 48 days left, owner sales'."""
    parts = [f"#{g['id']} {g.get('title') or ''}".rstrip()]
    if g.get("deadline"):
        parts[0] += f" by {g['deadline'][:10]}"
    detail = f"{g.get('metric') or 'no metric'} {_num(g.get('current'))} of {_num(g.get('target'))}"
    if g.get("days_left") is not None:
        d = g["days_left"]
        detail += f", {d} days left" if d >= 0 else f", {-d} days overdue"
    if g.get("owner_role"):
        detail += f", owner {g['owner_role']}"
    return f"{parts[0]}: {detail}"


def get_strategy(company_id: int) -> dict | None:
    return db.one("SELECT * FROM strategy WHERE company_id=?", (company_id,))


def has_strategy(company_id: int) -> bool:
    row = get_strategy(company_id)
    return bool(row and any((row.get(f) or "").strip() for f in STRATEGY_FIELDS))


def is_empty(company_id: int) -> bool:
    """No strategy and no goals: a company founded before Phase 2 whose CEO has not written the brain yet."""
    return not has_strategy(company_id) and not db.scalar("SELECT COUNT(*) FROM goals WHERE company_id=?",
                                                          (company_id,))


def _agent_name(agent_id) -> str:
    a = db.get_agent(agent_id) if agent_id else None
    return a["name"] if a else "the owner"


def _ceo_name(company_id: int) -> str:
    a = db.one("SELECT name FROM agents WHERE company_id=? AND role='ceo'", (company_id,))
    return a["name"] if a else "the CEO"


def _decider(company_id: int, decided_by: str) -> str:
    return {"ceo": _ceo_name(company_id), "owner": "the owner", "telegram": "the owner (phone)",
            "deadline": "the deadline"}.get(decided_by or "", decided_by or "someone")


def _change_payload(row: dict) -> dict:
    p = db.loads(row.get("payload"), {})
    return p if isinstance(p, dict) else {"text": str(p)}


def _change_text(section: str, change: dict) -> tuple[str, str]:
    """('strategy.icp', 'icp: "urban dog owners and walkers"') for a proposal's label and summary."""
    if set(change) <= {"text"}:
        return section, str(change.get("text") or "")
    keys = [k for k in change if k != "text"]
    label = f"{section}.{','.join(keys)}" if section == "strategy" else section
    return label, "; ".join(f'{k}: "{change[k]}"' if isinstance(change[k], str) else f"{k}: {change[k]}"
                            for k in keys)


def _cut(text, key: str, short: bool) -> str:
    text = (text or "").strip()
    return _clip(text, SHORT[key]) if short else text


def _end(text: str) -> str:
    """A sentence ending: text cut with "..." keeps its dots, anything else gets one full stop."""
    return text if text.endswith("...") else text.rstrip(".") + "."


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def proposal_line(row: dict, short: bool = False, emails: bool = True) -> str:
    """'#7 by Hunter (strategy.icp): icp: "...". Reason: ...'. short: the digest's version. emails=False hides
    email addresses (a contact proposal carries one), for prompts of roles that do not message people."""
    label, summary = _change_text(row.get("section") or "", _change_payload(row))
    line = f"#{row['id']} by {_agent_name(row.get('agent_id'))} ({label}): {_end(_cut(summary, 'summary', short))}"
    if (row.get("reason") or "").strip():
        line += f" Reason: {_end(_cut(row['reason'], 'reason', short))}"
    return line if emails else _EMAIL_RE.sub("(email hidden)", line)


def pending_proposals(company_id: int, limit: int = 50) -> list[dict]:
    return db.q("SELECT * FROM brain_changes WHERE company_id=? AND status='pending' ORDER BY id LIMIT ?",
                (company_id, limit))


def decision_line(row: dict, short: bool = False) -> str:
    """'#4 (review 2026-10-16) Price at $9/month. Expected: ... Decided by Atlas.'; irreversible ones say so."""
    when = (row.get("review_at") or "")[:10]
    head = f"#{row['id']} (review {when}" + (", IRREVERSIBLE" if not row.get("reversible", 1) else "") + ")"
    line = f"{head} {_end(_cut(row.get('what'), 'what', short))}"
    if (row.get("expected") or "").strip():
        line += f" Expected: {_end(_cut(row['expected'], 'expected', short))}"
    return line + f" Decided by {_decider(row.get('company_id'), row.get('decided_by'))}."


def due_by(value, day: str) -> bool:
    """Is a date or timestamp (decisions.review_at, experiments.stop) on or before day (YYYY-MM-DD)? Both forms are
    stored (the owner's form saves a date, tools save a timestamp), so only the date part is compared: a decision
    due at 10:00 today counts as due all day, the same as decisions_due."""
    v = str(value or "").strip()[:10]
    return bool(v) and v <= day


def decisions_due(company_id: int, days: int = DUE_DAYS, limit: int = 5) -> list[dict]:
    """Decisions with no outcome whose review date is within `days` days (or past), soonest first. Only the date
    part of review_at is compared (see due_by); days=0 means due today or earlier."""
    until = (_today() + _dt.timedelta(days=days)).isoformat()
    return db.q("SELECT * FROM decisions WHERE company_id=? AND COALESCE(outcome,'')='' AND COALESCE(review_at,'')!='' "
                "AND substr(review_at,1,10) <= ? ORDER BY review_at, id LIMIT ?", (company_id, until, limit))


def experiment_line(row: dict, short: bool = False) -> str:
    """'#2 (goal #2, stops 2026-10-21) "hypothesis". Metric forms.signup_rate: 3.4 so far.'"""
    goal = f"goal #{row['goal_id']}, " if row.get("goal_id") else ""
    hypothesis = _cut(row.get("hypothesis"), "hypothesis", short)
    line = f"#{row['id']} ({goal}stops {(row.get('stop') or '?')[:10]}) \"{hypothesis}\"."
    if row.get("metric"):
        m = latest_metric(row["company_id"], row["metric"])
        line += f" Metric {row['metric']}: " + (f"{_num(m['value'])} so far." if m else "no data yet.")
    return line


def running_experiments(company_id: int, limit: int = 5) -> list[dict]:
    return db.q("SELECT * FROM experiments WHERE company_id=? AND verdict='pending' ORDER BY stop, id LIMIT ?",
                (company_id, limit))


# ---------------------------------------------------------------- the digest

def _block(title: str, lines: list[str], key: str, empty: str = "(none)", more: str = "") -> str:
    """One digest section: the title and as many lines as fit its cap, each line cut to its own cap."""
    cap, line_cap = SECTION_CAPS[key], LINE_CAPS[key]
    out, used = [title], len(title)
    if not lines:
        return f"{title}\n{empty}"
    for i, ln in enumerate(lines):
        ln = _clip(ln, line_cap)
        after = len(lines) - i - 1   # keep room for the "N more" note while lines remain after this one
        reserve = len(more.format(n=after)) + 1 if more and after else 0
        if used + 1 + len(ln) + reserve > cap:
            if more:
                out.append(more.format(n=len(lines) - i))
            break
        out.append(ln)
        used += 1 + len(ln)
    return "\n".join(out)


def _header(company: dict) -> str:
    return f"# Company brain: {company.get('name') or 'the company'} (digest, {_today().isoformat()})"


def _strategy_block(cid: int) -> str:
    row = get_strategy(cid)
    if not row or not any((row.get(f) or "").strip() for f in STRATEGY_FIELDS):
        return "## Strategy\n(not set yet: the CEO sets it with set_strategy)"
    lines = [f"{STRATEGY_LABELS[f]}: {(row.get(f) or '').strip() or '(not set)'}" for f in STRATEGY_FIELDS]
    return _block("## Strategy", lines, "strategy")


def _metric_lines(cid: int) -> list[str]:
    rows = db.q("SELECT m.name, m.value, m.source, m.created_at, a.name AS agent_name FROM metrics m "
                "JOIN (SELECT name, MAX(id) AS mid FROM metrics WHERE company_id=? GROUP BY name) x ON m.id=x.mid "
                "LEFT JOIN agents a ON a.id=m.agent_id ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
                (cid, METRIC_NAMES))
    out = []
    for r in rows:
        src = (r.get("source") or "").strip() or "manual"   # rows from before Phase 1 have no source
        if src == "manual" and r.get("agent_name"):
            src += f", {r['agent_name']}"
        out.append(f"{r['name']} {_num(r['value'])} [{src}] {(r.get('created_at') or '')[:10]}")
    names = db.scalar("SELECT COUNT(DISTINCT name) FROM metrics WHERE company_id=?", (cid,)) or 0
    if names > len(out):
        out.append(f"({names - len(out)} older metric names not shown)")
    return out


def _contacts_line(cid: int) -> list[str]:
    counts = {r["stage"]: r["n"] for r in db.q("SELECT stage, COUNT(*) n FROM contacts WHERE company_id=? "
                                               "GROUP BY stage", (cid,))}
    if not counts:
        return []
    words = {"lead": ("lead", "leads"), "contacted": ("contacted", "contacted"), "replied": ("replied", "replied"),
             "customer": ("customer", "customers"), "lost": ("lost", "lost")}
    parts = [f"{counts[s]} {words[s][0] if counts[s] == 1 else words[s][1]}" for s in CONTACT_STAGES if counts.get(s)]
    parts += [f"{n} {s or 'no stage'}" for s, n in counts.items() if s not in CONTACT_STAGES]
    unsub = db.scalar("SELECT COUNT(*) FROM contacts WHERE company_id=? AND unsubscribed=1", (cid,)) or 0
    return [", ".join(parts) + "." + (f" {unsub} unsubscribed." if unsub else "")]


def digest(company: dict) -> str:
    """The brain digest, at most DIGEST_CAP characters: strategy, active goals, latest metrics with their source,
    decisions due for review, running experiments, lessons, contact counts and pending proposals, in that order.
    Every section header is always there (with "(none)"), so agents learn what the brain holds."""
    cid = company["id"]
    goals = active_goals(cid)
    lessons = db.q("SELECT text FROM lessons WHERE company_id=? ORDER BY id DESC LIMIT ?", (cid, SHOWN["lessons"]))
    pending = db.scalar("SELECT COUNT(*) FROM brain_changes WHERE company_id=? AND status='pending'", (cid,)) or 0
    parts = [
        _header(company),
        _strategy_block(cid),
        _block(f"## Goals ({len(goals)} of {MAX_ACTIVE_GOALS} active)",
               [goal_line(dict(g, title=_cut(g.get("title"), "title", True))) for g in goals], "goals",
               empty="(none yet: the CEO sets up to 3 with set_goal)"),
        _block("## Metrics (latest per name; source in brackets)", _metric_lines(cid), "metrics",
               more="({n} more not shown)"),
        _block("## Decisions due for review",
               [decision_line(r, short=True) for r in decisions_due(cid, limit=SHOWN["decisions"])], "decisions"),
        _block("## Running experiments",
               [experiment_line(r, short=True) for r in running_experiments(cid, SHOWN["experiments"])], "experiments"),
        _block("## Lessons (newest first)", [f"- {r['text']}" for r in lessons], "lessons",
               more="(older lessons: brain_lookup lessons)"),
        _block("## Contacts", _contacts_line(cid), "contacts"),
        # Every role reads the digest, so a contact proposal's email address stays out of it (spec, Open questions 3).
        _block(f"## Brain proposals waiting for CEO review: {pending}",
               [proposal_line(r, short=True, emails=False) for r in pending_proposals(cid, SHOWN["proposals"])],
               "proposals"),
    ]
    text = "\n".join(p for p in parts if p)
    if len(text) > DIGEST_CAP:   # safety net: cut from the bottom, at a line break
        tail = "\n[digest cut here; brain_lookup has the rest]"
        text = text[:text.rfind("\n", 0, DIGEST_CAP - len(tail))] + tail
    return text


def company_section(company: dict) -> str:
    """The system prompt's company block: the digest and where the charter is. A company whose brain is empty (no
    strategy and no goals) keeps Phase 0.5's charter block under the digest header; one with goals but no strategy
    yet gets the charter block after the digest, so the customer, offer and price are never lost."""
    core = "# The company\n" + ctxsys.company_core(company)
    if is_empty(company["id"]):
        return f"{_header(company)}\n{EMPTY_LINE}\n\n{core}"
    text = digest(company) + "\n" + CHARTER_LINE
    if not has_strategy(company["id"]):
        text += "\n\n" + core
    return text


# ---------------------------------------------------------------- validation shared by tools, proposals and routes

def _coerce(kind: str, value, company_id: int, label: str):
    """One column value checked and normalised; ValueError with a sentence the model or the owner can act on."""
    if kind in ("text", "long", "role"):
        text = str(value if value is not None else "").strip()
        if kind == "role":
            return text.lower()[:40]
        return text[:2000 if kind == "long" else 500]
    if kind == "lesson":
        text = " ".join(str(value or "").split())
        if not text:
            raise ValueError("the lesson is empty")
        if len(text) > LESSON_CAP:
            raise ValueError(f"a lesson is one short rule of at most {LESSON_CAP} characters (this one has "
                             f"{len(text)}); shorten it")
        return text
    if kind == "metric":
        name = re.sub(r"\s+", "_", str(value or "").strip().lower())
        name = re.sub(r"[^a-z0-9_.\-]", "", name)[:60]
        return name
    if kind == "number":
        if value is None or str(value).strip() == "":
            return None
        try:
            return float(str(value).replace(",", "").replace("$", "").strip())
        except ValueError:
            raise ValueError(f"{label} must be a number, not '{value}'") from None
    if kind == "date":
        if value is None or str(value).strip() == "":
            return ""
        d = _parse_date(value)
        if not d:
            raise ValueError(f"{label} must be a date like {_today().isoformat()}, not '{value}'")
        return d.isoformat()
    if kind == "flag":
        if isinstance(value, bool):
            return int(value)
        return 1 if str(value).strip().lower() in ("1", "true", "yes", "on") else 0
    if kind == "email":
        email = str(value or "").strip()
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError(f"'{email}' is not a valid email address")
        return email[:200]
    choices = {"goal_status": GOAL_STATUSES, "stage": CONTACT_STAGES, "verdict": VERDICTS, "decider": DECIDERS}
    if kind in choices:
        v = str(value or "").strip().lower()
        if v not in choices[kind]:
            raise ValueError(f"{label} must be one of {', '.join(choices[kind])}, not '{value}'")
        return v
    if kind == "goal":
        if value in (None, "", 0, "0"):
            return None
        gid = to_int(value, -1)
        if not db.one("SELECT id FROM goals WHERE id=? AND company_id=?", (gid, company_id)):
            raise ValueError(f"goal #{value} is not a goal of this company")
        return gid
    raise ValueError(f"unknown column kind {kind}")


def _clean_fields(section: str, data: dict, company_id: int) -> dict:
    """The whitelisted columns of `section` found in data, validated. Other keys are ignored."""
    cols = COLUMNS[section]
    return {col: _coerce(kind, data[col], company_id, col) for col, kind in cols.items() if col in (data or {})}


def _check_goal_room(company_id: int, exclude: int | None = None) -> None:
    active = db.q("SELECT * FROM goals WHERE company_id=? AND status='active' AND id!=? ORDER BY id",
                  (company_id, exclude or 0))
    if len(active) >= MAX_ACTIVE_GOALS:
        raise ValueError(f"{MAX_ACTIVE_GOALS} goals are active; drop or finish one first ("
                         + "; ".join(f"#{g['id']} {g['title']}" for g in active) + ")")


def _find_contact(company_id: int, email: str) -> dict | None:
    if not email:
        return None
    return db.one("SELECT * FROM contacts WHERE company_id=? AND lower(email)=lower(?) ORDER BY id LIMIT 1",
                  (company_id, email))


def _insert(section: str, company_id: int, fields: dict) -> int:
    """Insert a row (the strategy is upserted). Callers validated the fields with _clean_fields; this adds the
    defaults and the table rules: at most 3 active goals, one contact per email."""
    ts = db.now()
    if section == "strategy":
        _upsert_strategy(company_id, fields)
        return company_id
    missing = [c for c in REQUIRED.get(section, ()) if not fields.get(c)]
    if missing:
        raise ValueError(f"give {', '.join(missing)}")
    row = dict(fields)
    if section == "goals":
        row.setdefault("status", "active")
        if row["status"] == "active":
            _check_goal_room(company_id)
        if row.get("current") is None:
            m = latest_metric(company_id, row.get("metric") or "")
            row["current"] = m["value"] if m else None
    elif section == "contacts":
        if not (row.get("name") or row.get("email")):
            raise ValueError("give at least a name or an email")
        dup = _find_contact(company_id, row.get("email") or "")
        if dup:
            raise ValueError(f"a contact with the email {row['email']} already exists (#{dup['id']})")
        row.setdefault("stage", "lead")
    elif section == "decisions":
        row.setdefault("review_at", _stamp(14))
        row.setdefault("decided_by", "owner")
    elif section == "experiments":
        row.setdefault("start", _today().isoformat())
        row.setdefault("stop", (_today() + _dt.timedelta(days=14)).isoformat())
        row.setdefault("verdict", "pending")
    row.update(company_id=company_id, created_at=ts)
    cols = list(row)   # whitelisted names (COLUMNS) plus company_id and created_at
    return db.run(f"INSERT INTO {TABLES[section]}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                  [row[c] for c in cols])


def _update(section: str, row: dict, fields: dict) -> None:
    if not fields:
        raise ValueError("nothing to change: give one of " + ", ".join(COLUMNS[section]))
    if section == "strategy":
        _upsert_strategy(row["company_id"], fields)
        return
    if section == "goals" and fields.get("status") == "active" and row.get("status") != "active":
        _check_goal_room(row["company_id"], exclude=row["id"])
    if section == "contacts" and fields.get("email"):
        dup = _find_contact(row["company_id"], fields["email"])
        if dup and dup["id"] != row["id"]:
            raise ValueError(f"a contact with the email {fields['email']} already exists (#{dup['id']})")
    db.update(TABLES[section], row["id"], **fields)


def _upsert_strategy(company_id: int, fields: dict) -> None:
    fields = {k: v for k, v in fields.items() if k in STRATEGY_FIELDS}
    if not get_strategy(company_id):
        db.run("INSERT INTO strategy(company_id, updated_at) VALUES(?,?)", (company_id, db.now()))
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)   # names from STRATEGY_FIELDS only
        db.run(f"UPDATE strategy SET {cols}, updated_at=? WHERE company_id=?", [*fields.values(), db.now(), company_id])
    if fields.get("one_liner"):
        db.update("companies", company_id, mission=fields["one_liner"][:300])   # the one-liner is the mission


def _upsert_contact(company_id: int, fields: dict) -> tuple[int, bool]:
    """add_contact and accepted contact proposals: insert, or update the contact with the same email (case-blind).
    An agent can never clear unsubscribed. Returns (id, created)."""
    fields = dict(fields)
    dup = _find_contact(company_id, fields.get("email") or "")
    if dup:
        if dup.get("unsubscribed"):
            fields.pop("unsubscribed", None)
        changes = {k: v for k, v in fields.items() if v not in ("", None) and k != "email"}
        if changes:
            db.update("contacts", dup["id"], **changes)
        return dup["id"], False
    return _insert("contacts", company_id, fields), True


def _row(section: str, row_id: int) -> dict | None:
    return db.one(f"SELECT * FROM {TABLES[section]} WHERE id=?", (row_id,))


# ---------------------------------------------------------------- goals and owner decisions

def _active_ids(company_id: int) -> list[int]:
    return [r["id"] for r in db.q("SELECT id FROM goals WHERE company_id=? AND status='active' ORDER BY id",
                                  (company_id,))]


def _tell_owner_goals(company: dict, agent: dict, task_id, before: list[int], added: int) -> str:
    """The active goals changed outside founding: one decision tells the owner (Keep or Revert). A decision about
    the goals that is still pending is updated in place instead of adding another to the inbox, so a CEO that
    sets three goals in one cycle asks once (the Rooforyou run asked the same thing 38 times)."""
    from . import decisions   # imported here: decisions imports tools, which imports this module
    if not agent:
        return ""
    cid = company["id"]
    goals = active_goals(cid)
    after = [g["id"] for g in goals]
    question = (f"{agent.get('name') or 'The CEO'} changed the goals. Active goals now: "
                + ("; ".join(goal_line(g) for g in goals) or "none") + ". Keep them?")
    for row in db.q("SELECT * FROM approvals WHERE company_id=? AND action='decision' AND status='pending' "
                    "ORDER BY id DESC", (cid,)):
        p = db.loads(row["payload"], {}) or {}
        origin = p.get("origin") or {}
        if origin.get("kind") != "goals":
            continue
        # "before" stays the set from before the first change, so Revert still restores it.
        origin.update(after=after, added=sorted({to_int(i) for i in origin.get("added") or []} | {added}))
        p.update(origin=origin, question=question)
        db.update("approvals", row["id"], payload=json.dumps(p, sort_keys=True), summary=question[:140])
        bus.notify("approvals")
        return f"The owner's pending decision #{row['id']} about the goals now lists this one too."
    # origin: the active goals before and after the change and the goals added, all that Revert needs.
    return decisions.create(company, agent, task_id, question, ["Keep", "Revert"], "Keep", deadline_hours=48,
                            reversible=True, context="Revert restores the goals that were active before.",
                            origin={"kind": "goals", "before": before, "after": after, "added": [added]})


def on_owner_decision(company_id: int, origin: dict, answer: str) -> None:
    """decisions.on_decided calls this for origin kinds "brain_change" (answer "Accept the change" applies the
    proposal, "Reject it" rejects it) and "goals" ("Revert" restores the previous active goals; "Keep" changes
    nothing)."""
    origin = origin or {}
    answer = (answer or "").strip().lower()
    if origin.get("kind") == "brain_change":
        row = db.one("SELECT * FROM brain_changes WHERE id=? AND company_id=?", (to_int(origin.get("change_id")),
                                                                                  company_id))
        if not row or row["status"] != "owner":
            return   # decided already (from the Brain tab) or deleted
        accept = answer.startswith("accept")
        _decide_change(row, accept, by="owner")
        return
    if origin.get("kind") == "goals" and answer.startswith("revert"):
        before = {to_int(i) for i in origin.get("before") or []}
        after = {to_int(i) for i in origin.get("after") or []}
        for gid in sorted(after - before):
            db.run("UPDATE goals SET status='dropped' WHERE id=? AND company_id=? AND status='active'", (gid, company_id))
        for gid in sorted(before):
            if len(_active_ids(company_id)) >= MAX_ACTIVE_GOALS:
                break
            db.run("UPDATE goals SET status='active' WHERE id=? AND company_id=? AND status!='active'",
                   (gid, company_id))
        bus.emit(company_id, "brain", "the owner reverted the goals to " +
                 (", ".join(f"#{i}" for i in _active_ids(company_id)) or "none"))


# ---------------------------------------------------------------- applying proposals

def _apply_change(row: dict, by: str) -> str:
    """Write an accepted proposal into its table. Returns what was written; ValueError when it cannot be."""
    cid, section = row["company_id"], row["section"]
    change = _change_payload(row)
    if set(change) <= {"text"}:
        text = str(change.get("text") or "").strip()
        if section == "lessons" and text:
            return f"lesson #{_insert('lessons', cid, _clean_fields('lessons', {'text': text}, cid))} added"
        return "nothing written (the proposal is prose; make the change yourself with the right tool)"
    target = to_int(change.get("id"), 0)
    fields = _clean_fields(section, change, cid)
    if section == "strategy":
        _upsert_strategy(cid, fields)
        return "strategy updated (" + ", ".join(fields) + ")"
    if target:
        existing = _row(section, target)
        if not existing or existing["company_id"] != cid:
            raise ValueError(f"{section} #{target} does not exist in this company")
        if section == "contacts" and existing.get("unsubscribed"):
            fields.pop("unsubscribed", None)
        if section == "decisions":
            fields.pop("decided_by", None)   # who decided is history, not something a proposal changes
        _update(section, existing, fields)
        return f"{section} #{target} updated"
    if section == "contacts":
        contact_id, created = _upsert_contact(cid, fields)
        return f"contact #{contact_id} {'added' if created else 'updated'}"
    if section == "decisions":
        fields["decided_by"] = "owner" if by == "owner" else "ceo"   # whoever accepted it, never the proposer's say
    before = _active_ids(cid)
    new_id = _insert(section, cid, fields)
    told = ""
    if section == "goals" and by != "owner" and new_id in _active_ids(cid):
        company = db.get_company(cid)
        ceo = db.one("SELECT * FROM agents WHERE company_id=? AND role='ceo'", (cid,))
        told = _tell_owner_goals(company, ceo, None, before, new_id)   # the owner hears about a new active goal
    return f"{section[:-1]} #{new_id} added" + (f" ({told.rstrip('.')})" if told else "")


def _decide_change(row: dict, accept: bool, by: str, note: str = "", agent_id: int | None = None) -> str:
    """Accept (apply, then mark accepted) or reject a proposal and record it. by: ceo or owner; agent_id: the CEO
    that reviewed it. The event carries that id: a 'brain' event without an agent is the owner's edit, which
    planning treats as news (planning.REAL_CHANGE_SQL), and the CEO's own review must not wake it again."""
    cid = row["company_id"]
    what = _apply_change(row, by) if accept else ""
    status = "accepted" if accept else "rejected"
    db.update("brain_changes", row["id"], status=status, decided_at=db.now())
    who = "the owner" if by == "owner" else _ceo_name(cid)
    label = _change_text(row["section"], _change_payload(row))[0]
    msg = f"{who} {status} brain proposal #{row['id']} ({label})" + (f": {what}" if what else "")
    bus.emit(cid, "brain", msg, agent_id if by != "owner" else None, {"change_id": row["id"], "by": by})
    # The proposer reads its notes: an accepted or rejected proposal is not proposed again.
    db.run("INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(?,?,?,?,?,?)",
           (cid, row.get("agent_id"), "decision", _clip(msg[0].upper() + msg[1:] + (f". Note: {note}" if note else ""),
                                                        600), 0, db.now()))
    return what


# ---------------------------------------------------------------- tools: every role

def _see_emails(ctx: Ctx) -> bool:
    return roles.preset_key(ctx.agent) in EMAIL_ROLES or (ctx.agent or {}).get("role") == "ceo"


def _section_name(section: str) -> str:
    s = (section or "").strip().lower().replace(" ", "_")
    s = SECTION_ALIASES.get(s, s)
    return s if s in SECTIONS else (s + "s" if s + "s" in SECTIONS else s)


def _lookup_line(section: str, r: dict, emails: bool) -> str:
    if section == "goals":
        g = dict(r, days_left=_days_left(r) if r.get("status") == "active" else None)
        return goal_line(g) + f" [{r.get('status')}]"
    if section == "contacts":
        who = r.get("name") or "(no name)"
        if r.get("email"):
            who += f" <{r['email']}>" if emails else " (email hidden for your role)"
        extra = ", ".join(x for x in (r.get("company"), r.get("role")) if x)
        line = f"#{r['id']} {who}" + (f" ({extra})" if extra else "") + f": stage {r.get('stage') or 'lead'}"
        for label, key in (("source", "source"), ("consent", "consent"), ("last touch", "last_touch")):
            if r.get(key):
                line += f", {label} {r[key]}"
        if r.get("unsubscribed"):
            line += ". UNSUBSCRIBED: never contact"
        return line + (f". Notes: {r['notes']}" if r.get("notes") else "")
    if section == "decisions":
        line = decision_line(r)
        if r.get("why"):
            line += f" Why: {r['why']}"
        return line + (f" Outcome: {r['outcome']}" if r.get("outcome") else "")
    if section == "experiments":
        line = experiment_line(r) + f" Verdict: {r.get('verdict') or 'pending'}."
        return line + (f" Result: {r['result']}" if r.get("result") else "")
    if section == "lessons":
        return f"#{r['id']} ({(r.get('created_at') or '')[:10]}) {r.get('text') or ''}"
    return f"[{r.get('status')}] " + proposal_line(r, emails=emails)


@tool("brain_lookup", "Look up records in the company brain: strategy, goals, contacts, decisions, experiments, "
      "lessons or proposals. Returns up to 20 rows, newest first.",
      {"section": S("strategy, goals, contacts, decisions, experiments, lessons or proposals"),
       "query": S("Optional words to look for")}, ("section",), risk="read")
def brain_lookup(ctx: Ctx, section: str, query: str = "") -> str:
    cid = ctx.company["id"]
    section = _section_name(section)
    if section not in SECTIONS:
        return f"ERROR: unknown section '{section}'. Sections: {', '.join(SECTIONS)}."
    if section == "strategy":
        row = get_strategy(cid)
        if not row or not has_strategy(cid):
            return "The strategy is not set yet. The CEO sets it with set_strategy."
        return "\n".join([f"{STRATEGY_LABELS[f]}: {(row.get(f) or '').strip() or '(not set)'}" for f in STRATEGY_FIELDS]
                         + [f"Updated: {row.get('updated_at') or '?'}"])
    if section == "goals":
        active_goals(cid)   # refresh current from the metrics first
    emails = _see_emails(ctx)
    cols = [c for c in SEARCH_COLUMNS[section] if emails or c != "email"]
    words = [w for w in re.findall(r"[\w@.$%-]+", (query or "").lower()) if len(w) > 1][:6]
    table = TABLES[section]

    def find(joiner: str) -> list[dict]:
        where, params = "company_id=?", [cid]
        if words:
            # names in cols come from SEARCH_COLUMNS, never from the model
            per = ["(" + " OR ".join(f"lower(COALESCE({c},'')) LIKE ?" for c in cols) + ")" for _w in words]
            where += " AND (" + f" {joiner} ".join(per) + ")"
            for w in words:
                params += [f"%{w}%"] * len(cols)
        return db.q(f"SELECT * FROM {table} WHERE {where} ORDER BY id DESC LIMIT ?", params + [LOOKUP_ROWS])

    rows = find("AND")
    if not rows and len(words) > 1:
        rows = find("OR")   # a model's three-word query should still find the one row that has two of them
    if not rows:
        return f"No {section} match '{query}'." if words else f"No {section} yet."
    head = f"{section.capitalize()}" + (f" matching '{query}'" if words else "") + f" (newest first, {len(rows)}):"
    return head + "\n" + "\n".join(_clip(_lookup_line(section, r, emails), 500) for r in rows)


def _owner_proposal_problem(ctx: Ctx, data: dict, reason: str) -> str | None:
    """A strategy change for the owner reaches the owner like a question, so Phase 0.5's rules for owner questions
    hold: the claims gate on what the owner will read (the change and its reason), one decision per strategy field
    at a time, and the team's questions and decisions together at most planning.MAX_PENDING_QUESTIONS. Returns the
    refusal, or None."""
    from . import planning, truth   # imported here: both import tools, which imports this module
    cid = ctx.company["id"]
    refusal = truth.claims_guard(ctx, "\n".join([_change_text("strategy", data)[1], (reason or "").strip()]), "ask")
    if refusal:
        return refusal
    fields = {k for k in data if k in OWNER_FIELDS}
    for row in db.q("SELECT * FROM brain_changes WHERE company_id=? AND section='strategy' AND status='owner' "
                    "ORDER BY id", (cid,)):
        shared = sorted(fields & set(_change_payload(row)))
        if shared:
            return (f"Proposal #{row['id']} to change the strategy's {', '.join(shared)} already waits for the "
                    "owner's decision. Don't propose another change to it until the owner decides; continue with "
                    "your work.")
    waiting = planning.pending_agent_questions(cid)
    if len(waiting) >= planning.MAX_PENDING_QUESTIONS:
        return planning.cap_refusal(waiting)
    return None


def _parse_change(change: str) -> dict:
    text = (change or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    return {"text": text}


@tool("propose_brain_change", "Propose a change to the company brain; the CEO reviews it (changes to price, offer, "
      "positioning or customer go to the owner). For a lesson use add_lesson instead.",
      {"section": S("strategy, goals, contacts, decisions, experiments or lessons"),
       "change": S('A JSON object of fields and new values, e.g. {"icp": "dog walkers and owners"}, or plain text'),
       "reason": S("Why, with the evidence")}, ("section", "change"))
def propose_brain_change(ctx: Ctx, section: str, change: str, reason: str = "") -> str:
    cid = ctx.company["id"]
    section = _section_name(section)
    if section not in COLUMNS:
        return f"ERROR: section must be one of {', '.join(COLUMNS)}."
    data = _parse_change(change)
    if not any(str(v).strip() for v in data.values()):
        return "ERROR: the change is empty. Say what should change, as JSON fields or plain text."
    if section == "strategy":
        fields = [k for k in data if k != "text"]
        if not fields or any(k not in STRATEGY_FIELDS for k in fields):
            return ('ERROR: a strategy change names its fields as JSON, for example {"icp": "urban dog owners and '
                    'dog walkers"}. Fields: ' + ", ".join(STRATEGY_FIELDS) + ".")
    elif set(data) - {"text"}:
        unknown = [k for k in data if k not in COLUMNS[section] and k not in ("id", "text")]
        if unknown:
            return (f"ERROR: {section} has no field {', '.join(unknown)}. Fields: {', '.join(COLUMNS[section])}"
                    f" (add \"id\" to change an existing row), or send plain text.")
        try:
            _clean_fields(section, data, cid)
        except ValueError as e:
            return f"ERROR: {e}."
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
    dup = db.one("SELECT id, status FROM brain_changes WHERE company_id=? AND section=? AND payload=? AND status IN "
                 "('pending','owner')", (cid, section, payload))
    if dup:
        return f"Already proposed as #{dup['id']} ({dup['status']}). Don't resubmit; continue with your work."
    to_owner = section == "strategy" and any(k in OWNER_FIELDS for k in data)
    if to_owner:
        refusal = _owner_proposal_problem(ctx, data, reason)
        if refusal:
            return refusal
    ch_id = db.run("INSERT INTO brain_changes(company_id, agent_id, section, payload, reason, status, created_at) "
                   "VALUES(?,?,?,?,?,?,?)", (cid, ctx.agent["id"], section, payload, (reason or "").strip()[:1000],
                                             "owner" if to_owner else "pending", db.now()))
    label, summary = _change_text(section, data)
    if not to_owner:
        bus.emit(cid, "brain", f"proposed a brain change #{ch_id} ({label})", ctx.agent["id"], {"change_id": ch_id})
        return f"Proposal #{ch_id} saved for the CEO's review at the next planning cycle. Continue with your work."
    from . import decisions   # imported here: decisions imports tools, which imports this module
    current = get_strategy(cid) or {}
    now = "; ".join(f'{k} now "{(current.get(k) or "").strip() or "not set"}"' for k in data if k in STRATEGY_FIELDS)
    question = (f"{ctx.agent['name']} proposes changing the strategy: {summary} ({now}). Accept the change?")
    out = decisions.create(ctx.company, ctx.agent, ctx.task["id"] if ctx.task else None, question,
                           ["Accept the change", "Reject it"], "Reject it", deadline_hours=72, reversible=True,
                           context=(reason or "").strip(), origin={"kind": "brain_change", "change_id": ch_id})
    if not out.startswith("Decision #"):
        # Refused, or folded into a decision already waiting ("Already waiting ... as decision #N", which answers
        # a different proposal): no decision of its own, so no proposal either, or it would wait as 'owner' forever.
        db.run("DELETE FROM brain_changes WHERE id=?", (ch_id,))
        return out
    bus.emit(cid, "brain", f"proposed a strategy change #{ch_id} ({label}); the owner decides", ctx.agent["id"],
             {"change_id": ch_id})
    return (f"Proposal #{ch_id} changes the strategy's {', '.join(k for k in data if k in OWNER_FIELDS)}, so the "
            f"owner decides it, not the CEO. {out}")


@tool("add_lesson", "Save a lesson the team learned: one short rule (at most 300 characters).",
      {"text": S("The rule, e.g. 'r/dogs removes posts with links; post the text and reply with the link.'")},
      ("text",))
def add_lesson(ctx: Ctx, text: str) -> str:
    cid = ctx.company["id"]
    try:
        clean = _coerce("lesson", text, cid, "text")
    except ValueError as e:
        return f"ERROR: {e}, then call add_lesson again."
    dup = db.one("SELECT id FROM lessons WHERE company_id=? AND lower(text)=lower(?)", (cid, clean))
    if dup:
        return f"Already saved as lesson #{dup['id']}."
    lid = _insert("lessons", cid, {"text": clean})
    bus.emit(cid, "brain", f"added lesson #{lid}: {_clip(clean, 120)}", ctx.agent["id"])
    return f"Lesson #{lid} saved. Every agent sees the newest lessons in the brain digest."


# ---------------------------------------------------------------- tools: people-facing roles

@tool("add_contact", "Save a person to the company's contact list (CRM). If the email is already saved, that "
      "contact is updated instead.",
      {"name": S("Full name"), "email": S("Email address"), "company": S("Their company"),
       "role": S("Their job title"),
       "stage": S("lead, contacted, replied, customer or lost (default lead)", enum=CONTACT_STAGES),
       "source": S("Where they came from: a URL, a form, a reply"),
       "consent": S("yes if they agreed to be contacted, otherwise no"), "notes": S("Anything useful")}, ())
def add_contact(ctx: Ctx, name: str = "", email: str = "", company: str = "", role: str = "", stage: str = "",
                source: str = "", consent: str = "", notes: str = "") -> str:
    cid = ctx.company["id"]
    data = {k: v for k, v in (("name", name), ("email", email), ("company", company), ("role", role),
                              ("stage", stage), ("source", source), ("consent", consent), ("notes", notes))
            if str(v or "").strip()}
    if not (data.get("name") or data.get("email")):
        return "ERROR: give at least a name or an email."
    try:
        fields = _clean_fields("contacts", data, cid)
        contact_id, created = _upsert_contact(cid, fields)
    except ValueError as e:
        return f"ERROR: {e}."
    bus.emit(cid, "brain", f"{'added' if created else 'updated'} contact #{contact_id}", ctx.agent["id"])
    if created:
        return f"Contact #{contact_id} saved (stage {fields.get('stage') or 'lead'})."
    return f"Contact #{contact_id} already had that email; updated it. Change its stage with update_contact."


@tool("update_contact", "Update a saved contact: stage, last touch, consent, unsubscribed or notes.",
      {"contact_id": I("Contact number"),
       "stage": S("lead, contacted, replied, customer or lost", enum=CONTACT_STAGES),
       "last_touch": S("Date of the last contact, YYYY-MM-DD or 'today'"), "consent": S("yes or no"),
       "unsubscribed": {"type": "boolean", "description": "true when they opted out (final)"},
       "notes": S("A note to add")}, ("contact_id",))
def update_contact(ctx: Ctx, contact_id: int, stage: str = "", last_touch: str = "", consent: str = "",
                   unsubscribed: bool | None = None, notes: str = "") -> str:
    cid = ctx.company["id"]
    row = db.one("SELECT * FROM contacts WHERE id=? AND company_id=?", (contact_id, cid))
    if not row:
        return f"ERROR: contact #{contact_id} not found. Find it with brain_lookup(section='contacts', query=...)."
    data = {k: v for k, v in (("stage", stage), ("last_touch", last_touch), ("consent", consent)) if str(v or "").strip()}
    try:
        fields = _clean_fields("contacts", data, cid)
    except ValueError as e:
        return f"ERROR: {e}."
    if unsubscribed is not None:
        if row.get("unsubscribed") and not unsubscribed:
            return (f"ERROR: contact #{contact_id} unsubscribed. That is final: never contact them again, and only "
                    "the owner can change it.")
        if unsubscribed:
            fields["unsubscribed"] = 1
            if (row.get("email") or "").strip():
                from . import verify   # Phase 3: an unsubscribe stops every company, not only this one
                verify.suppress(row["email"], f"unsubscribed (contact #{contact_id})", cid)
    if notes.strip():
        fields["notes"] = ((row.get("notes") or "") + f"\n[{_today().isoformat()}] {notes.strip()}").strip()[-2000:]
    if not fields:
        return "ERROR: nothing to change. Give stage, last_touch, consent, unsubscribed or notes."
    db.update("contacts", contact_id, **fields)
    bus.emit(cid, "brain", f"updated contact #{contact_id}", ctx.agent["id"])
    shown = ", ".join(f"{k} {v}" for k, v in fields.items() if k != "notes") or "notes"
    return f"Contact #{contact_id} updated: {shown}." + (" They are unsubscribed: never contact them again."
                                                        if fields.get("unsubscribed") else "")


# ---------------------------------------------------------------- tools: the CEO

@tool("set_strategy", "Write the company strategy: one line each for the one-liner, customer (ICP), problem, offer, "
      "price, positioning and voice. Outside founding, fields you leave out keep their value.",
      {"one_liner": S("What we do and for whom, one sentence"), "icp": S("The customer who buys, one or two sentences"),
       "problem": S("The problem they pay to solve"), "offer": S("What we sell"), "price": S("The price, as text"),
       "positioning": S("Why us and not the alternatives"), "voice": S("How we sound when we write")}, ())
def set_strategy(ctx: Ctx, one_liner: str = "", icp: str = "", problem: str = "", offer: str = "", price: str = "",
                 positioning: str = "", voice: str = "") -> str:
    cid = ctx.company["id"]
    given = {"one_liner": one_liner, "icp": icp, "problem": problem, "offer": offer, "price": price,
             "positioning": positioning, "voice": voice}
    fields = {k: _coerce("text", v, cid, k) for k, v in given.items() if str(v or "").strip()}
    if ctx.kind == "founding":
        missing = [k for k in STRATEGY_FIELDS if k not in fields]
        if missing:
            return ("ERROR: during founding set_strategy needs all seven fields; missing: " + ", ".join(missing)
                    + ". Take them from the charter you just saved and call set_strategy again.")
    if not fields:
        return "ERROR: give at least one field: " + ", ".join(STRATEGY_FIELDS) + "."
    _upsert_strategy(cid, fields)
    if "one_liner" in fields:
        ctx.company["mission"] = fields["one_liner"][:300]
    bus.emit(cid, "brain", "set the strategy (" + ", ".join(fields) + ")", ctx.agent["id"])
    return (f"Strategy saved ({len(fields)} field{'s' if len(fields) != 1 else ''}). Every agent sees it in the brain "
            "digest from its next run.")


@tool("set_goal", "Add an active company goal: a metric, a target number and a deadline. At most 3 goals are "
      "active; tasks name the goal they serve with goal_id.",
      {"title": S("The goal in plain words, e.g. '10 paying customers' (the date goes in deadline)"),
       "metric": S("The metric that measures it, e.g. stripe.customers or waitlist_signups"),
       "target": {"type": "number", "description": "The number to reach"},
       "deadline": S("YYYY-MM-DD"), "owner_role": S("The role that owns the outcome (default ceo)")},
      ("title", "metric", "target", "deadline"))
def set_goal(ctx: Ctx, title: str, metric: str, target: float, deadline: str, owner_role: str = "ceo") -> str:
    cid = ctx.company["id"]
    title = " ".join((title or "").split())[:200]
    if not title:
        return "ERROR: give the goal a title, e.g. '10 paying customers'."
    metric = _coerce("metric", metric, cid, "metric")
    if not metric:
        return "ERROR: name the metric that measures this goal, e.g. stripe.customers or waitlist_signups."
    when = _parse_date(deadline)
    if not when or when <= _today():
        return f"ERROR: deadline must be a future date like {(_today() + _dt.timedelta(days=30)).isoformat()}."
    role = (owner_role or "ceo").strip().lower()
    team = [a["role"] for a in db.q("SELECT role FROM agents WHERE company_id=? AND enabled=1", (cid,))]
    note = ""
    if team and role not in team:   # a weak model's guess at a role should not cost it the goal
        note = f" No teammate has the role '{owner_role}', so the CEO owns this goal (roles: {', '.join(team)})."
        role = "ceo"
    dup = db.one("SELECT id FROM goals WHERE company_id=? AND status='active' AND lower(title)=lower(?)", (cid, title))
    if dup:
        return f"Goal #{dup['id']} already says that. Use goal_id={dup['id']}."
    before = _active_ids(cid)
    if len(before) >= MAX_ACTIVE_GOALS:
        listed = "; ".join(goal_line(g) for g in active_goals(cid))
        return (f"ERROR: {MAX_ACTIVE_GOALS} goals are active; drop or finish one first (drop_goal). Active: {listed}")
    gid = _insert("goals", cid, {"title": title, "metric": metric, "target": float(target),
                                  "deadline": when.isoformat(), "owner_role": role, "status": "active"})
    g = next((x for x in active_goals(cid) if x["id"] == gid), None) or db.one("SELECT * FROM goals WHERE id=?", (gid,))
    bus.emit(cid, "brain", f"set goal #{gid}: {title}", ctx.agent["id"], {"goal_id": gid})
    out = f"Goal #{gid} saved: {goal_line(g)}. Give the tasks that serve it goal_id={gid}.{note}"
    if not latest_metric(cid, metric):
        known = [r["name"] for r in db.q("SELECT DISTINCT name FROM metrics WHERE company_id=? ORDER BY name LIMIT 12",
                                         (cid,))]
        out += (f" No number is recorded as {metric} yet; it counts once a connector or the analyst records it"
                + (f" (known metrics: {', '.join(known)})." if known else "."))
    if ctx.kind != "founding":
        out += " " + _tell_owner_goals(ctx.company, ctx.agent, ctx.task["id"] if ctx.task else None, before, gid)
    return out


@tool("drop_goal", "Close an active goal as done or dropped. Its tasks keep their goal_id.",
      {"goal_id": I("Goal number"), "status": S("done or dropped", enum=("done", "dropped")),
       "note": S("Why, in one line")},
      ("goal_id", "status"))
def drop_goal(ctx: Ctx, goal_id: int, status: str, note: str = "") -> str:
    cid = ctx.company["id"]
    g = db.one("SELECT * FROM goals WHERE id=? AND company_id=?", (goal_id, cid))
    if not g:
        return f"ERROR: goal #{goal_id} not found. Active goals: " + (
            "; ".join(goal_line(x) for x in active_goals(cid)) or "none") + "."
    status = (status or "").strip().lower()
    if status not in ("done", "dropped"):
        return "ERROR: status must be done or dropped."
    if g["status"] != "active":
        return f"Goal #{goal_id} is already {g['status']}."
    db.update("goals", goal_id, status=status)
    msg = f"Goal #{goal_id} ({g['title']}) closed as {status}" + (f": {note.strip()}" if note.strip() else "")
    bus.emit(cid, "brain", msg, ctx.agent["id"], {"goal_id": goal_id})
    db.run("INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(?,?,?,?,?,?)",
           (cid, ctx.agent["id"], "decision", _clip(msg, 600), 0, db.now()))
    return msg + ". Its open tasks keep goal_id and are listed in the next weekly review."


@tool("log_decision", "Record a decision you made, why, and the result you expect; it comes back for review. To "
      "record how an earlier decision turned out, give decision_id and outcome instead.",
      {"what": S("The decision"), "why": S("The reasoning"), "expected": S("The measurable result you expect"),
       "review_days": I("Days until you check the result (default 14)"),
       "reversible": {"type": "boolean", "description": "false if it cannot be undone"},
       "decision_id": I("Only to record an outcome: the number of the earlier decision"),
       "outcome": S("Only with decision_id: what actually happened, with the numbers and their source")}, ())
def log_decision(ctx: Ctx, what: str = "", why: str = "", expected: str = "", review_days: int = 14,
                 reversible: bool = True, decision_id: int = 0, outcome: str = "") -> str:
    cid = ctx.company["id"]
    if decision_id:
        # The outcome of a decision under review. review_at stays as it was (the record of when it was due): a
        # decision with an outcome is no longer "due for review" in the digest or the planning prompt.
        row = db.one("SELECT * FROM decisions WHERE id=? AND company_id=?", (decision_id, cid))
        if not row:
            due = "; ".join(f"#{r['id']} {_clip(r['what'], 60)}" for r in decisions_due(cid)) or "none"
            return f"ERROR: decision #{decision_id} not found. Decisions due for review: {due}."
        if not (outcome or "").strip():
            return f"ERROR: give the outcome of decision #{decision_id}: what happened, with the numbers."
        db.update("decisions", decision_id, outcome=outcome.strip()[:2000])
        bus.emit(cid, "brain", f"logged the outcome of decision #{decision_id}", ctx.agent["id"])
        return (f"Outcome of decision #{decision_id} saved; it is no longer due for review. If it taught the team "
                "something, save it with add_lesson.")
    if not (what or "").strip():
        return "ERROR: say what was decided (what), and why. To record an outcome, give decision_id and outcome."
    if not (why or "").strip():
        return "ERROR: say why you decided it (why)."
    days = max(1, min(int(review_days or 14), 180))
    did = db.run("INSERT INTO decisions(company_id, what, why, expected, review_at, outcome, decided_by, reversible, "
                 "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 (cid, what.strip()[:500], (why or "").strip()[:2000], (expected or "").strip()[:500], _stamp(days),
                  "", "ceo", int(bool(reversible)), db.now()))
    bus.emit(cid, "brain", f"logged decision #{did}: {_clip(what, 120)}", ctx.agent["id"])
    out = f"Decision #{did} logged; it comes back for review on {_stamp(days)[:10]}."
    if not reversible:
        out += " Marked irreversible. If the owner should have a say, ask with ask_decision instead."
    return out


@tool("start_experiment", "Start a bet that could move a goal: a hypothesis, the metric that decides it and a stop "
      "date. Give its tasks the same goal_id.",
      {"goal_id": I("The goal it serves"), "hypothesis": S("'At least X% of Y will do Z within N days'"),
       "metric": S("The metric that decides it (default: the goal's metric)"),
       "stop_days": I("Days until it is judged (default 14)")}, ("goal_id", "hypothesis"))
def start_experiment(ctx: Ctx, goal_id: int, hypothesis: str, metric: str = "", stop_days: int = 14) -> str:
    cid = ctx.company["id"]
    g = db.one("SELECT * FROM goals WHERE id=? AND company_id=? AND status='active'", (goal_id, cid))
    if not g:
        return f"ERROR: goal #{goal_id} is not an active goal. Active goals: " + (
            "; ".join(goal_line(x) for x in active_goals(cid)) or "none (set one with set_goal)") + "."
    hypothesis = " ".join((hypothesis or "").split())[:500]
    if not hypothesis:
        return "ERROR: write the hypothesis, e.g. 'At least 5% of landing page visitors join the waitlist in 14 days'."
    dup = db.one("SELECT id FROM experiments WHERE company_id=? AND verdict='pending' AND lower(hypothesis)=lower(?)",
                 (cid, hypothesis))
    if dup:
        return f"Experiment #{dup['id']} already tests that."
    days = max(1, min(int(stop_days or 14), 90))
    stop = (_today() + _dt.timedelta(days=days)).isoformat()
    eid = _insert("experiments", cid, {"goal_id": goal_id, "hypothesis": hypothesis,
                                        "metric": _coerce("metric", metric, cid, "metric") or g.get("metric") or "",
                                        "start": _today().isoformat(), "stop": stop, "verdict": "pending"})
    bus.emit(cid, "brain", f"started experiment #{eid} for goal #{goal_id}", ctx.agent["id"])
    return f"Experiment #{eid} started for goal #{goal_id}; it stops on {stop}. Give its tasks goal_id={goal_id}."


@tool("conclude_experiment", "Judge an experiment: what happened, and the verdict won, lost or inconclusive.",
      {"experiment_id": I("Experiment number"), "result": S("What happened, with the numbers and their source"),
       "verdict": S("won, lost or inconclusive", enum=VERDICTS[1:])}, ("experiment_id", "result", "verdict"))
def conclude_experiment(ctx: Ctx, experiment_id: int, result: str, verdict: str) -> str:
    cid = ctx.company["id"]
    e = db.one("SELECT * FROM experiments WHERE id=? AND company_id=?", (experiment_id, cid))
    if not e:
        return f"ERROR: experiment #{experiment_id} not found. Running: " + (
            "; ".join(experiment_line(x) for x in running_experiments(cid)) or "none") + "."
    verdict = (verdict or "").strip().lower()
    if verdict not in ("won", "lost", "inconclusive"):
        return "ERROR: verdict must be won, lost or inconclusive."
    if e["verdict"] != "pending":
        return f"Experiment #{experiment_id} was already concluded as {e['verdict']}."
    db.update("experiments", experiment_id, result=(result or "").strip()[:2000], verdict=verdict)
    bus.emit(cid, "brain", f"concluded experiment #{experiment_id}: {verdict}", ctx.agent["id"])
    return (f"Experiment #{experiment_id} concluded: {verdict}. If it taught the team something, save it with "
            "add_lesson.")


@tool("review_brain_change", "Accept or reject a brain proposal from your team. Accepting writes it into the brain.",
      {"change_id": I("Proposal number"), "decision": S("accept or reject"), "note": S("Why, in one line")},
      ("change_id", "decision"))
def review_brain_change(ctx: Ctx, change_id: int, decision: str, note: str = "") -> str:
    cid = ctx.company["id"]
    row = db.one("SELECT * FROM brain_changes WHERE id=? AND company_id=?", (change_id, cid))
    if not row:
        waiting = "; ".join(f"#{r['id']}" for r in pending_proposals(cid, 10)) or "none"
        return f"ERROR: proposal #{change_id} not found. Waiting for your review: {waiting}."
    if row["status"] == "owner":
        return (f"ERROR: proposal #{change_id} changes the price, offer, positioning or customer, so it waits for "
                "the owner's decision. You cannot review it.")
    if row["status"] != "pending":
        return f"Proposal #{change_id} was already {row['status']}."
    d = (decision or "").strip().lower()
    if d.startswith(("acc", "app", "yes")):
        accept = True
    elif d.startswith(("rej", "dec", "no")):
        accept = False
    else:
        return "ERROR: decision must be accept or reject."
    try:
        what = _decide_change(row, accept, by="ceo", note=(note or "").strip(), agent_id=ctx.agent["id"])
    except ValueError as e:
        return f"ERROR: proposal #{change_id} cannot be applied: {e}. Reject it, or make the change yourself."
    if not accept:
        return f"Proposal #{change_id} rejected."
    return f"Proposal #{change_id} accepted: {what}."


# ---------------------------------------------------------------- API (the Brain tab)

def _proposal_json(r: dict) -> dict:
    label, summary = _change_text(r.get("section") or "", _change_payload(r))
    return {**r, "change": _change_payload(r), "label": label, "summary": summary,
            "agent_name": _agent_name(r.get("agent_id"))}


@route("GET", r"/api/companies/(\d+)/brain")
def api_brain(h, m, body):
    c = company_or_404(m.group(1))
    cid = c["id"]
    active_goals(cid)   # refresh current from the metrics
    goals = db.q("SELECT * FROM goals WHERE company_id=? ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id",
                 (cid,))
    for g in goals:
        g["days_left"] = _days_left(g)
    d = digest(c)
    strategy = get_strategy(cid) or {"company_id": cid, **{f: "" for f in STRATEGY_FIELDS}, "updated_at": None}
    return {"strategy": strategy, "goals": goals,
            "contacts": db.q("SELECT * FROM contacts WHERE company_id=? ORDER BY id DESC LIMIT 500", (cid,)),
            "decisions": db.q("SELECT * FROM decisions WHERE company_id=? ORDER BY id DESC LIMIT 200", (cid,)),
            "experiments": db.q("SELECT * FROM experiments WHERE company_id=? ORDER BY id DESC LIMIT 200", (cid,)),
            "lessons": db.q("SELECT * FROM lessons WHERE company_id=? ORDER BY id DESC LIMIT 200", (cid,)),
            "proposals": [_proposal_json(r) for r in db.q("SELECT * FROM brain_changes WHERE company_id=? "
                                                          "ORDER BY id DESC LIMIT 200", (cid,))],
            "digest": d, "digest_chars": len(d), "digest_cap": DIGEST_CAP, "empty": is_empty(cid)}


def _owner_section(section: str) -> str:
    if section not in COLUMNS:
        raise HTTPError(404, f"Unknown brain section '{section}'. Sections: {', '.join(COLUMNS)}.")
    return section


@route("POST", r"/api/companies/(\d+)/brain/(\w+)")
def api_brain_add(h, m, body):
    c = company_or_404(m.group(1))
    section = _owner_section(m.group(2))
    try:
        fields = _clean_fields(section, body or {}, c["id"])
        if not fields:
            raise ValueError("nothing to save: give one of " + ", ".join(COLUMNS[section]))
        row_id = _insert(section, c["id"], fields)
    except ValueError as e:
        raise HTTPError(400, str(e)[0].upper() + str(e)[1:] + ".") from None
    bus.emit(c["id"], "brain", f"owner edited {section}")
    row = get_strategy(c["id"]) if section == "strategy" else _row(section, row_id)
    return {"id": row_id, "row": row}


def _owner_row(section: str, row_id: str) -> dict:
    if section == "strategy":   # the strategy row's id is its company's id
        company_or_404(row_id)
        return get_strategy(to_int(row_id)) or {"company_id": to_int(row_id)}
    row = _row(section, to_int(row_id))
    if not row:
        raise HTTPError(404, f"{section} #{row_id} not found")
    return row


@route("PATCH", r"/api/brain/(\w+)/(\d+)")
def api_brain_patch(h, m, body):
    section = _owner_section(m.group(1))
    row = _owner_row(section, m.group(2))
    try:
        _update(section, row, _clean_fields(section, body or {}, row["company_id"]))
    except ValueError as e:
        raise HTTPError(400, str(e)[0].upper() + str(e)[1:] + ".") from None
    bus.emit(row["company_id"], "brain", f"owner edited {section}")
    return {"ok": True, "row": get_strategy(row["company_id"]) if section == "strategy" else _row(section, row["id"])}


@route("DELETE", r"/api/brain/(\w+)/(\d+)")
def api_brain_delete(h, m, body):
    section = m.group(1)
    if section == "strategy":
        raise HTTPError(400, "The strategy cannot be deleted, only edited.")
    if section not in TABLES:
        raise HTTPError(404, f"Unknown brain section '{section}'.")
    row = _row(section, to_int(m.group(2)))
    if not row:
        raise HTTPError(404, f"{section} #{m.group(2)} not found")
    db.run(f"DELETE FROM {TABLES[section]} WHERE id=?", (row["id"],))   # table name from TABLES only
    bus.emit(row["company_id"], "brain", f"owner edited {section}")
    return {"ok": True}


@route("POST", r"/api/brain/changes/(\d+)")
def api_brain_change_decide(h, m, body):
    row = db.one("SELECT * FROM brain_changes WHERE id=?", (to_int(m.group(1)),))
    if not row:
        raise HTTPError(404, "Proposal not found")
    d = str((body or {}).get("decision") or "").strip().lower()
    if d not in ("accept", "reject"):
        raise HTTPError(400, 'decision must be "accept" or "reject"')
    if row["status"] not in ("pending", "owner"):
        raise HTTPError(400, f"Proposal #{row['id']} is already {row['status']}.")
    was_owner = row["status"] == "owner"
    try:
        what = _decide_change(row, d == "accept", by="owner")
    except ValueError as e:
        raise HTTPError(400, f"Proposal #{row['id']} cannot be applied: {e}.") from None
    if was_owner:
        # The proposal also waits in the inbox as a decision: answer it there too, so it does not linger or
        # resolve itself at the deadline. on_owner_decision then finds the proposal decided and does nothing.
        from . import actions
        answer = "Accept the change" if d == "accept" else "Reject it"
        for a in db.q("SELECT id, payload FROM approvals WHERE company_id=? AND action='decision' AND "
                      "status='pending'", (row["company_id"],)):
            origin = (db.loads(a["payload"], {}) or {}).get("origin") or {}
            if origin.get("kind") == "brain_change" and to_int(origin.get("change_id")) == row["id"]:
                try:
                    actions.decide(a["id"], "approve", answer=answer, decided_by="owner")
                except ValueError:
                    pass
    bus.emit(row["company_id"], "brain", "owner edited proposals")
    return {"ok": True, "status": "accepted" if d == "accept" else "rejected", "applied": what}
