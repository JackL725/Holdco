"""The verification gate (Phase 3, docs/phases/phase-3-verify.md section 3): before a task may be marked done,
the work is checked by its kind (web page, code, payment link, outreach, research) and by the tool checks listed in
task.checks. Also the compliance rules every email must pass when it is queued, and the global do-not-email list.

Scaffold: nothing is checked yet; every seam answers "passes".
"""
from __future__ import annotations

from . import db  # noqa: F401


def finish_problem(ctx, target: str, summary: str, evidence: str) -> str | None:
    """tools.finish(status="done") on a task: run the gate, store the results in tasks.verification and return
    "<kind>: <detail>" for the first failure, or None when everything passes."""
    return None


def outreach_check(ctx, payload: dict) -> list[str]:
    """The compliance failures of one email payload (to, subject, body); an empty list means it may be queued."""
    return []


def suppress(email: str, reason: str = "", company_id: int | None = None) -> None:
    """Add an address to the global do-not-email list (unsubscribe, a stop reply, or the owner's button)."""
    return None


def is_suppressed(email: str) -> bool:
    return False


def summary_line(task: dict) -> str:
    """'[gate: web page PASS (browser); outreach FAIL: no opt-out line]' for the CEO's review, or ''."""
    return ""
