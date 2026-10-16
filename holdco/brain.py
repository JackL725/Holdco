"""Company brain (Phase 2, docs/phases/phase-2-organize.md section 1): strategy, goals, contacts, decisions,
experiments, lessons and proposals as structured records; a digest under 6,000 characters that every agent reads
first; the tools that read and change them; the API behind the Brain tab.

Scaffold: company_section() keeps today's "# The company" block so nothing changes until the brain is built.
"""
from __future__ import annotations

import datetime as _dt

from . import db  # noqa: F401
from . import context as ctxsys
from .routing import route  # noqa: F401  (routes register here)
from .tools import Ctx, ToolError, S, I, tool  # noqa: F401  (tools register here)

DIGEST_CAP = 6000


def company_section(company: dict) -> str:
    """The system prompt's company block. Scaffold: the Phase 0.5 core (charter fields)."""
    return "# The company\n" + ctxsys.company_core(company)


def digest(company: dict) -> str:
    """The brain digest (Phase 2). Scaffold: empty."""
    return ""


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
    today = _dt.datetime.now(_dt.timezone.utc).date()
    for g in db.q("SELECT * FROM goals WHERE company_id=? AND status='active' ORDER BY id", (company_id,)):
        m = latest_metric(company_id, g.get("metric") or "")
        if m is not None and m["value"] != g.get("current"):
            db.run("UPDATE goals SET current=? WHERE id=?", (m["value"], g["id"]))
            g["current"] = m["value"]
        g["current_source"] = (m or {}).get("source") or ""
        try:
            g["days_left"] = (_dt.date.fromisoformat((g.get("deadline") or "")[:10]) - today).days
        except ValueError:
            g["days_left"] = None
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


def on_owner_decision(company_id: int, origin: dict, answer: str) -> None:
    """decisions.on_decided calls this for origin kinds "brain_change" (answer "Accept the change" applies the
    proposal, "Reject it" rejects it) and "goals" ("Revert" restores the previous active goals)."""
    return None
