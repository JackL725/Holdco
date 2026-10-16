"""Maps agents to their preset and tool belt. The presets themselves live in presets.py."""
from __future__ import annotations

from . import presets

# Phase 0.5: founding runs in stages, each with a small belt of its own (design 4.1). Research cannot create tasks
# or write the charter, so it cannot eat the whole founding; the critic may only write docs/critique.md (enforced
# by planning.charter_write_hook). Research gets remember instead of the design's note_progress: founding runs
# have no task, and note_progress only writes a task's working notes.
FOUNDING_STAGE_TOOLS = {
    "research": ["web_search", "fetch_url", "search", "read_file", "write_file", "record_fact", "remember", "finish"],
    "charter": ["read_file", "search", "update_charter", "record_fact", "finish"],
    "critic": ["read_file", "search", "web_search", "fetch_url", "write_file", "finish"],
    "plan": ["read_file", "search", "create_task", "ask_owner", "report_to_owner", "finish"],
}
# Every founding tool, for callers that ask without a stage.
FOUNDING_TOOLS = list(dict.fromkeys(n for names in FOUNDING_STAGE_TOOLS.values() for n in names if n != "finish")) \
    + ["finish"]
CHAT_EXTRA = ["list_tasks", "create_task", "update_task", "report_to_owner"]
# Tools every role gets in task runs, on top of its preset's belt (Phase 0: working notes).
TASK_EXTRA = ["note_progress"]
# Phase 0.5: finding things and the real state of the company, for every role and run kind.
CONTEXT_EXTRA = ["search", "grep", "edit_file", "company_status"]
# Phase 0.5: verified facts and the source check, for roles that research (they have web_search).
RESEARCH_EXTRA = ["record_fact", "check_sources"]


def preset_key(agent: dict) -> str:
    return (agent.get("preset") or agent.get("role") or "").strip()


def role_info(agent_or_role) -> dict:
    """Preset for an agent (dict) or role key (str); a generic stand-in for custom agents."""
    key = agent_or_role if isinstance(agent_or_role, str) else preset_key(agent_or_role)
    p = presets.get(key)
    if p:
        return p
    return {"name": "", "title": "Specialist", "tools": presets.CUSTOM_TOOLS, "routines": [],
            "reasoning": "medium", "creativity": "balanced"}


def founding_tools(stage: str) -> list[str]:
    """The tool belt of one founding stage (research, charter, critic or plan)."""
    return list(FOUNDING_STAGE_TOOLS.get(stage) or FOUNDING_STAGE_TOOLS["research"])


def tools_for(agent: dict, kind: str) -> list[str]:
    if kind == "founding":
        return list(FOUNDING_TOOLS)
    names = list(role_info(agent)["tools"])
    if "finish" not in names:
        names.append("finish")
    if kind == "chat":
        names = [n for n in names if n != "finish"] + CHAT_EXTRA
    elif kind == "task":
        names = [n for n in names if n != "finish"] + TASK_EXTRA + ["finish"]
    research = RESEARCH_EXTRA if "web_search" in names else []
    names = [n for n in names if n != "finish"] + CONTEXT_EXTRA + research + (["finish"] if "finish" in names else [])
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


MAX_TEAM = 14


class TeamError(ValueError):
    pass


def add_agent(company_id: int, preset: str = "", role: str = "", name: str = "", title: str = "",
              focus: str = "", model: str = "") -> int:
    """Add an agent, from a preset when given. Returns the new agent id."""
    import re

    from . import db

    p = presets.get(preset) if preset else None
    if preset and not p:
        raise TeamError(f"Unknown preset '{preset}'.")
    if (db.scalar("SELECT COUNT(*) FROM agents WHERE company_id=?", (company_id,)) or 0) >= MAX_TEAM:
        raise TeamError(f"The team is at its maximum size ({MAX_TEAM}).")
    base = re.sub(r"[^a-z0-9_]+", "_", (role or preset or "").lower()).strip("_")[:24]
    if not base:
        raise TeamError("Give the agent a role key, like seo or design, or pick a preset.")
    key, n = base, 2
    while db.one("SELECT id FROM agents WHERE company_id=? AND role=?", (company_id, key)):
        if not p or role:  # explicit custom role keys must be unique
            raise TeamError(f"There's already a '{key}' agent.")
        key, n = f"{base}_{n}", n + 1
    return db.run(
        "INSERT INTO agents(company_id, role, preset, name, title, focus, model, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (company_id, key, preset or "", (name or (p["name"] if p else key.replace("_", " ").title()))[:30],
         (title or (p["title"] if p else "Specialist"))[:60], (focus or "")[:1500], model or "", db.now()))
