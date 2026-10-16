"""Decisions queue (Phase 2, docs/phases/phase-2-organize.md section 5): "which of these should we do?" with
options, a recommendation and a deadline after which a reversible decision takes the recommended option.
Scaffold only."""
from __future__ import annotations

from . import db  # noqa: F401
from .tools import Ctx, ToolError, S, I, tool  # noqa: F401


def on_decided(row: dict, answer: str, decided_by: str) -> None:
    """actions.decide answered a decision: record it in the brain's decisions table."""
    return None


def resolve_due() -> None:
    """Scheduler: take the recommended option of reversible decisions past their deadline."""
    return None
