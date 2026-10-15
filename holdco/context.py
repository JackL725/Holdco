"""Context system (Phase 0.5): the index, unified search, line-based reading, small edits, the owner's context
library, and the prompt sections that give every agent the company's context without dumping it all.

Design: docs/phases/phase-0.5-ground-truth-and-context.md, section 1. The functions below are the seams other
modules call; this scaffold keeps today's behaviour so the suite stays green until the implementation lands.
"""
from __future__ import annotations

from . import db, workspace
from .routing import route  # noqa: F401  (routes register here)
from .tools import Ctx, ToolError, S, I, tool  # noqa: F401  (tools register here)

CHARTER_PROMPT_CAP = 3500   # today's cut; the implementation replaces the full charter with the core fields


def company_core(company: dict) -> str:
    """The '# The company' section body. Scaffold: today's text (name, one-liner, stage, idea, charter)."""
    charter = (company.get("charter") or "").strip()
    if len(charter) > CHARTER_PROMPT_CAP:
        charter = charter[:CHARTER_PROMPT_CAP] + "\n[charter truncated; full text in docs/charter.md]"
    return (f"Name: {company['name']}\nOne-liner: {company.get('mission') or '(not set yet)'}\n"
            f"Stage: {company.get('stage') or 'idea'}\nOriginal idea: {(company.get('idea') or '')[:600]}\n\n"
            f"Charter:\n{charter or '(not written yet)'}")


def memory_section(company: dict, agent: dict, task: dict | None) -> str:
    """Shared memory for the prompt. Scaffold: pinned notes plus the newest ones (today's behaviour)."""
    cid = company["id"]
    pinned = db.q("SELECT * FROM memory WHERE company_id=? AND pinned=1 ORDER BY id DESC LIMIT 15", (cid,))
    recent = db.q("SELECT * FROM memory WHERE company_id=? AND pinned=0 ORDER BY id DESC LIMIT 14", (cid,))
    lines = [f"- (pinned) {m['content'][:400]}" for m in pinned]
    lines += [f"- [{m['kind']}] {m['content'][:300]}" for m in reversed(recent)]
    return "\n".join(lines) or "(nothing yet)"


def pinned_section(company: dict) -> str:
    """Owner context files marked 'always include' (global and company). Scaffold: none."""
    return ""


def index_section(company: dict) -> str:
    """One line per context file and key document, with a summary. Scaffold: none."""
    return ""


def reindex(company: dict) -> None:
    """Bring the search index up to date for this company (incremental; called at the start of every run)."""
    return None


def after_write(ctx: Ctx, rel_path: str) -> None:
    """A file was written or edited by an agent: re-index it."""
    return None


def read_file(ctx: Ctx, path: str, offset: int = 0, start_line: int = 0, end_line: int = 0,
              outline: bool = False) -> str:
    """The read_file tool body. Scaffold: today's character-offset reader; lines and outline come later."""
    p = workspace.safe_path(ctx.company, path)
    if not p.exists() or not p.is_file():
        raise ToolError(f"{path} doesn't exist. Use list_files to see what's there.")
    if p.stat().st_size > 2_000_000:
        raise ToolError("File is too large to read.")
    raw = p.read_bytes()
    if b"\x00" in raw[:2000]:
        raise ToolError("That's a binary file.")
    text = raw.decode("utf-8", "replace")
    chunk = text[offset:offset + 12000]
    more = f"\n[{len(text) - offset - 12000} more characters: read again with offset={offset + 12000}]" \
        if len(text) > offset + 12000 else ""
    return chunk + more


def list_files(ctx: Ctx, path: str = ".") -> str:
    """The list_files tool body. Scaffold: today's listing; summaries from the index come later."""
    items = workspace.tree(ctx.company, path or ".", depth=4, limit=300)
    if not items:
        return "(empty)"
    return "\n".join(f"{i['path']}/" if i["dir"] else f"{i['path']} ({i['size']} bytes)" for i in items)
