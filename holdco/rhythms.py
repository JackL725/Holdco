"""Rhythms (Phase 2, docs/phases/phase-2-organize.md section 4): data that code gathers for the morning plan, the
evening brief, the weekly review and the monthly check; the write_brief tool; the portfolio brief; the weekly
clean-up of tasks that serve no goal. Scaffold only."""
from __future__ import annotations

from . import db  # noqa: F401
from .routing import route  # noqa: F401
from .tools import Ctx, ToolError, S, I, tool  # noqa: F401

RHYTHM_KEYS: set[str] = set()


@tool("write_brief", "Store this run's brief (daily, weekly or monthly). Use only numbers from the data block in your "
      "task; the brief is sent to the owner's phone and pinned on the dashboard.",
      {"kind": S("daily, weekly or monthly"), "body": S("The brief in Markdown, under 350 words")},
      ("kind", "body"), risk="write")
def write_brief(ctx: Ctx, kind: str, body: str) -> str:
    return "ERROR: write_brief is not built yet."   # scaffold


def task_data_section(company: dict, task: dict) -> str:
    """'## Data for this brief (the only numbers you may use)' for a rhythm task."""
    return ""


def after_run(company: dict, task: dict | None, ctx, status: str) -> None:
    """After a run: the weekly review cancels agent-created tasks that serve no goal."""
    return None


def portfolio_brief_if_due() -> None:
    return None
