"""Ground truth (Phase 0.5): the facts block, the records folder, changes since an agent's last session, the
claims gate, action-task rules, write guards, outreach checks, the fetched-page cache and verified facts.

Design: docs/phases/phase-0.5-ground-truth-and-context.md, sections 2 and 5. Every function here is a seam that
tools.py, agents.py and actions.py already call. The scaffold returns "nothing to add, nothing wrong".
"""
from __future__ import annotations

from . import db  # noqa: F401
from .routing import route  # noqa: F401  (routes register here)
from .tools import Ctx, ToolError, S, I, tool  # noqa: F401  (tools register here)


# ---------------- prompt sections ----------------

def facts_section(company: dict) -> str:
    """'# Facts' block generated from the database. Scaffold: none."""
    return ""


def changes_section(company: dict, agent: dict, kind: str) -> str:
    """'# Since your last session' for this agent. Scaffold: none."""
    return ""


def waiting_section(company: dict) -> str:
    """'# Waiting on the owner': pending questions and approvals with ids and age. Scaffold: none."""
    return ""


# ---------------- records ----------------

def refresh_records(company: dict) -> None:
    """Rewrite records/ from the database (start of every run)."""
    return None


def records_changed(company: dict) -> None:
    """An outbound action or owner decision happened: records/ must be rewritten soon."""
    return None


# ---------------- guards (return an error message for the agent, or None) ----------------

def write_guard(ctx: Ctx, rel_path: str, content: str, is_edit: bool = False) -> str | None:
    """Before write_file/edit_file saves: records/ and context/ protection, log-like names, future dates,
    unsupported claims. Return the full 'ERROR: ...' text to refuse."""
    return None


def claims_guard(ctx: Ctx, text: str, where: str) -> str | None:
    """Before report_to_owner / ask_owner sends text to the owner (where = 'report' | 'ask')."""
    return None


def finish_guard(ctx: Ctx, status: str, deliverable: str, summary: str, evidence: str) -> str | None:
    """Before finish(status='done') is accepted: action-task rules, unsupported claims, source check."""
    return None


def outreach_guard(ctx: Ctx, to: str, subject: str, body: str) -> tuple[str | None, str]:
    """Before send_email queues: (error or None, the body to send, with the footer added when missing)."""
    return None, body


def deploy_guard(ctx: Ctx) -> str | None:
    """Before deploy_site queues: placeholders and other blockers in site/."""
    return None


def classify_task(title: str, description: str = "", deliverable: str = "", kind: str = "") -> str:
    """outreach | deploy | post | purchase | ask | research | build | write ('' when unknown)."""
    return (kind or "").strip().lower()


def task_guard(ctx: Ctx, kind: str, title: str) -> str | None:
    """Before create_task: refuse action tasks that cannot run today (for example email not connected)."""
    return None


def cache_page(ctx: Ctx, url: str, text: str) -> None:
    """fetch_url returned this text: keep it for record_fact and check_sources."""
    return None
