"""Decisions queue (Phase 2, docs/phases/phase-2-organize.md section 5): "which of these should we do?" with
options, a recommendation and a deadline after which a reversible decision takes the recommended option.

Shared interface (used by brain.py for owner-routed proposals and goal changes, and by rhythms.py for the monthly
check): create() queues the decision; payload["origin"] says who asked, and on_decided() hands the answer back to
that module (brain.on_owner_decision for origin kinds "brain_change" and "goals"; the monthly check's "Kill" pauses
the company).
"""
from __future__ import annotations

import datetime as dt

from . import actions, db  # noqa: F401
from .tools import Ctx, ToolError, S, I, tool  # noqa: F401


def create(company: dict, agent: dict, task_id: int | None, question: str, options: list[str], recommended: str,
           deadline_hours: int = 48, reversible: bool = True, context: str = "", expected: str = "",
           origin: dict | None = None) -> str:
    """Queue a decision for the owner through actions.gate and return the gate's text. origin, when given, is
    {"kind": "brain_change", "change_id": N} or {"kind": "goals", ...} or {"kind": "monthly"}.
    Scaffold: no validation or deduplication beyond the gate's (the builder adds both)."""
    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=int(deadline_hours))).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload: dict = {"question": question, "options": list(options), "recommended": recommended,
                     "deadline_at": deadline, "reversible": bool(reversible)}
    if context:
        payload["context"] = context[:1500]
    if expected:
        payload["expected"] = expected
    if origin:
        payload["origin"] = origin
    return actions.gate(company, agent, task_id, "decision", payload)


def on_decided(row: dict, answer: str, decided_by: str) -> None:
    """actions.decide answered a decision: record it in the brain's decisions table and tell its origin."""
    return None


def resolve_due() -> None:
    """Scheduler: take the recommended option of reversible decisions past their deadline."""
    return None
