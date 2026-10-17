"""The trust ladder (Phase 3, docs/phases/phase-3-verify.md section 6): per company and action, level 0 asks, level 1
runs on its own up to a daily cap, level 2 runs on its own. Clean approvals build a streak; at 15 the owner is asked
whether to promote; an edit resets the streak and a rejection demotes.

Scaffold: every action keeps today's policy.
"""
from __future__ import annotations

from . import db  # noqa: F401


def gate_policy(company: dict, action: str, payload: dict, policy: str, taint_sources: list[str]) -> tuple[str, str]:
    """actions.gate: the policy to apply ('approve', 'auto' or 'off') and the reason shown on the Inbox card when the
    ladder or the taint rule overrides the configured policy ('' otherwise). Runs after the locked actions."""
    return policy, ""


def after_decision(row: dict, decision: str, edited: bool) -> None:
    """actions.decide on a request: bump or reset the streak, demote on a rejection, suggest a promotion."""
    return None
