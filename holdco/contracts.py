"""Task contracts, the dependency graph, file leases and budgets (Phase 2, docs/phases/phase-2-organize.md sections
2, 3 and 6). Scaffold: every seam answers "no contract, nothing blocks, no budget"."""
from __future__ import annotations

from . import db  # noqa: F401


def task_contract_section(task: dict) -> str:
    """The contract lines at the top of a task prompt (goal, expected, inputs, due, budget, checks)."""
    return ""


def cycle_sections(company: dict) -> str:
    """Goals, experiments, proposals and decisions due, placed before the open tasks in the planning prompt."""
    return ""


def dispatchable(task: dict) -> bool:
    """May the scheduler start this todo task now (every dependency done or cancelled)?"""
    return True


def max_steps_for(company: dict, task: dict, default: int) -> int:
    """Tool calls for this task attempt: min(company steps_per_task or default, task budget_steps)."""
    return default


def start_problem(task: dict) -> str | None:
    """A reason the task must not start (its token budget is used up), or None."""
    return None


def after_run(task: dict | None, tokens: int) -> None:
    """Add the run's tokens to the task's tokens_used."""
    return None


def over_gpu_budget(company: dict) -> bool:
    """Has the company used its GPU hours for today?"""
    return False


def post_budget_problem(ctx) -> str | None:
    """social_post: the weekly post budget is used up."""
    return None
