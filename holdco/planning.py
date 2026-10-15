"""Owner questions and planning effort (Phase 0.5): one open question per topic, structured questions, the change
stamp and backoff that stop idle re-planning, the daily token budget, charter validation, the planning header and
(owned by the implementer of this module) staged founding in agents.py.

Design: docs/phases/phase-0.5-ground-truth-and-context.md, sections 3 and 4.
"""
from __future__ import annotations

from . import actions, db  # noqa: F401
from .routing import route  # noqa: F401
from .tools import Ctx, ToolError, S, I, tool  # noqa: F401


def ask_owner(ctx: Ctx, question: str, options: str = "", recommended: str = "", context: str = "") -> str:
    """The ask_owner tool body. Scaffold: queue the question (with options when given), no dedupe yet."""
    payload: dict = {"question": question}
    opts = [o.strip(" -*\t") for o in (options or "").splitlines() if o.strip(" -*\t")]
    if opts:
        payload["options"] = opts[:6]
        if recommended.strip():
            payload["recommended"] = recommended.strip()
    if context.strip():
        payload["context"] = context.strip()[:1500]
    return actions.gate(ctx.company, ctx.agent, ctx.task["id"] if ctx.task else None, "question", payload)


def should_cycle(company: dict, due: bool, idle: bool, requested: bool) -> bool:
    """Scheduler: may the CEO plan now? Scaffold: today's rule."""
    return due or idle or requested


def cycle_started(company: dict) -> None:
    """Scheduler: a planning cycle just started (record the change stamp)."""
    return None


def over_budget(company: dict) -> bool:
    """Scheduler: has the company used its daily token budget? Scaffold: never."""
    return False


def validate_charter(text: str) -> list[str]:
    """Names of required charter sections that are missing or empty. Scaffold: none required."""
    return []


def charter_write_hook(ctx: Ctx, rel_path: str, content: str) -> str | None:
    """write_file/edit_file on docs/charter.md: validate and save to the company record. Return an error to
    refuse. Scaffold: nothing."""
    return None


def cycle_header(company: dict) -> str:
    """Top of the planning-cycle prompt: facts, changes, waiting items, playbook experiments. Scaffold: none."""
    return ""
