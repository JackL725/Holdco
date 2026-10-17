"""Portfolio capital allocation (Phase 4, docs/phases/phase-4-scale.md section 2): the scorecard, the allocation
score and each company's share of the GPU, the monthly portfolio review, and GET /api/portfolio.

Scaffold: an empty scorecard, Phase 3's dispatch order, no review.
"""
from __future__ import annotations

from . import db  # noqa: F401

WEIGHTS = {"progress": 0.4, "traction": 0.4, "freshness": 0.2}


def scorecard() -> list[dict]:
    """One row per active or paused company: goal_progress, revenue_30d, mrr, gpu_hours_week, money_spent_30d,
    days_since_result, age_days, score, share."""
    return []


def order(queue: list[dict]) -> list[dict]:
    """Scheduler.tick: the dispatch queue (already in Phase 3 class order) with classes 3 to 6 ordered by company
    deficit, and the starvation guard. Scaffold: unchanged."""
    return queue


def monthly_review_if_due() -> None:
    """Scheduler housekeeping: the monthly portfolio review when it is due."""
    return None


def apply_answer(company_id: int, origin: dict, answer: str) -> None:
    """decisions.on_decided for origin kind 'portfolio': Scale sets the boost, Shut down pauses the company."""
    return None
