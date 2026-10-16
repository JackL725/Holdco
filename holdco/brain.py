"""Company brain (Phase 2, docs/phases/phase-2-organize.md section 1): strategy, goals, contacts, decisions,
experiments, lessons and proposals as structured records; a digest under 6,000 characters that every agent reads
first; the tools that read and change them; the API behind the Brain tab.

Scaffold: company_section() keeps today's "# The company" block so nothing changes until the brain is built.
"""
from __future__ import annotations

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


def active_goals(company_id: int) -> list[dict]:
    """Active goals with current refreshed from the metrics table (used by contracts and rhythms)."""
    return []
