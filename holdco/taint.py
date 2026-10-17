"""Taint tracking (Phase 3, docs/phases/phase-3-verify.md section 5): a session that read outside content (search
results, fetched pages, inbound email, external browser pages, external connector reads) loses its automatic
permissions for outbound actions, and the content it read is fenced as information, not instructions.

tools.execute runs every tool inside `current(ctx)` and passes every result through `after_tool`; actions.gate asks
`current_sources()` when a caller does not pass the sources itself.

Scaffold: nothing taints yet.
"""
from __future__ import annotations

import contextlib
import contextvars

_current: contextvars.ContextVar = contextvars.ContextVar("holdco_tool_ctx", default=None)


@contextlib.contextmanager
def current(ctx):
    """The tool context of the tool running now, for actions.gate."""
    token = _current.set(ctx)
    try:
        yield
    finally:
        _current.reset(token)


def current_sources() -> list[str]:
    """Where the running session read outside content ([] outside a tool call or when it read none)."""
    ctx = _current.get()
    return list(getattr(ctx, "taint_sources", None) or []) if ctx is not None else []


def after_tool(ctx, name: str, args: dict, result: str) -> str:
    """Mark the session tainted after a reading tool and fence what it returned. Scaffold: unchanged."""
    return result
