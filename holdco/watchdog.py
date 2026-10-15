"""Watchdog and working notes (Phase 0).

The watchdog spots stuck patterns (a task failing twice, the same error repeating, a task bouncing between
agents, a connector down, a stalled company, a founding that ended partial) and turns each into ONE question
for the owner with options and a recommended answer, instead of a silent "partial".

Working notes (tasks.notes) record what each attempt tried, what failed and what is left, so a retry
continues instead of starting over. The note_progress tool lives here too.
"""
from __future__ import annotations

from . import db
from .tools import Ctx, S, tool  # noqa: F401  (tool registration)

PATTERNS = ("task_failed_twice", "repeated_error", "task_bouncing", "connector_down", "stalled", "founding_partial")


def tick() -> None:
    """Cheap SQL-only pass run by the scheduler every tick."""
    raise NotImplementedError("Phase 0: watchdog.tick")


def task_failed(task: dict, reason: str, run_status: str) -> None:
    """Called by agents._finalize after a task attempt ends without a clean finish."""
    raise NotImplementedError("Phase 0: watchdog.task_failed")


def founding_ended(company: dict, run_status: str, summary: str) -> None:
    """Called by agents._finalize when a founding run ends partial / out of steps."""
    raise NotImplementedError("Phase 0: watchdog.founding_ended")


def escalate(company: dict, pattern: str, question: str, options: list[str], recommended: str,
             context: str = "", task_id: int | None = None) -> int | None:
    """Create one question for the owner (deduplicated by pattern). Returns the approval id or None."""
    raise NotImplementedError("Phase 0: watchdog.escalate")


def resolve(approval: dict, answer: str) -> None:
    """Called by actions.decide when a question carrying a pattern is answered: apply the chosen option."""
    raise NotImplementedError("Phase 0: watchdog.resolve")


def add_note(task_id: int, text: str, author: str = "") -> None:
    """Append a dated entry to a task's working notes (keeps the newest ~4000 characters)."""
    raise NotImplementedError("Phase 0: watchdog.add_note")


def retry_guidance(task: dict) -> str:
    """Text for attempt >= 2: why the last attempt failed and what to do differently."""
    raise NotImplementedError("Phase 0: watchdog.retry_guidance")


@tool("note_progress", "Save working notes: what you tried, what failed and what is left. They survive if this "
      "session ends early, and the next attempt starts from them.",
      {"notes": S("The notes (plain text, a few lines)")}, ("notes",))
def note_progress(ctx: Ctx, notes: str) -> str:
    raise NotImplementedError("Phase 0: watchdog.note_progress")
