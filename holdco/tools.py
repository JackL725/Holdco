"""Tools the agents can call."""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Callable

from . import actions, bus, checks, db, presets, roles, web, workspace

MAX_RESULT = 7000
OPEN_STATUSES = ("todo", "in_progress", "blocked", "waiting")
TASK_STATUSES = ("todo", "in_progress", "done", "blocked", "waiting", "cancelled")
FINISH_STATUSES = ("done", "partial", "blocked", "waiting")
COMPANY_STAGES = ("idea", "building", "launched", "revenue")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


class ToolError(Exception):
    pass


@dataclass
class Ctx:
    company: dict
    agent: dict
    kind: str
    run_id: int
    task: dict | None = None
    settings: dict = field(default_factory=dict)
    finished: bool = False
    finish_status: str = "done"
    finish_summary: str = ""
    reply: str | None = None
    reply_sent: bool = False
    allowed: set = field(default_factory=set)   # tool names this agent may use
    finish_warned: bool = False
    deliverable: str = ""
    written: set = field(default_factory=set)   # files written in this session
    claims_warned: bool = False                  # Phase 0.5: finish() warned once about unsupported claims
    # Phase 0.5: the pending question or request this run waits on, when ask_owner refused a question as a
    # duplicate of it (truth.action_state and finish(status='waiting') read it).
    waiting_on: int | None = None
    waiting_warned: bool = False                 # finish() said once that nothing waits on the owner
    founding_stage: str = ""                     # research, charter, critic or plan in a founding run
    stage_warned: bool = False                   # finish() said once what the founding stage has not left
    refused_finish: dict | None = None           # the finish call that stage check refused (agents.run)
    # Phase 1: connector tools find_tool loaded into this session (specs the model sees from its next step on)
    loaded_tools: list = field(default_factory=list)
    loaded_by_name: dict = field(default_factory=dict)
    brief_written: str = ""   # Phase 2: the kind of brief write_brief stored in this run (a brief is a deliverable)
    brief_warned: bool = False   # Phase 2: write_brief refused once a brief with numbers its data does not hold
    # Phase 3: outside content this session read (search results, fetched pages, inbound email...): its sources,
    # and the verification gate's one push-back at finish
    tainted: bool = False
    taint_sources: list = field(default_factory=list)
    verify_warned: bool = False
    verify_waiting_on: int | None = None   # the run_code request the gate's last check queued or waits on
    verify_failed: list = field(default_factory=list)   # the gate's failed results from its last check


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable
    risk: str = "write"   # Phase 1: one of config.RISK_TAGS


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, props: dict, required: tuple = (), risk: str | None = None):
    def deco(fn):
        REGISTRY[name] = Tool(name, description, {"type": "object", "properties": props,
                                                  "required": list(required)}, fn,
                              risk or BUILTIN_RISK.get(name, "write"))
        return fn
    return deco


# Phase 1: the risk tag of every built-in tool. For built-ins the tag is descriptive (their own approval policies
# stay); for connector tools the tag decides the policy (connectors.policy_for). Anything not listed is "write".
BUILTIN_RISK = {
    # read: runs freely
    "web_search": "read", "fetch_url": "read", "read_file": "read", "list_files": "read", "recall": "read",
    "list_tasks": "read", "check_inbox": "read", "audit_page": "read", "check_contrast": "read",
    "review_text": "read", "browser_check": "read", "find_tool": "read", "search": "read", "grep": "read",
    "company_status": "read", "check_sources": "read",
    # contact, publish, spend
    "send_email": "contact", "social_post": "publish", "deploy_site": "publish", "http_request": "publish",
    "request_purchase": "spend",
}


def risk_of(name: str) -> str:
    t = REGISTRY.get(name)
    return t.risk if t else BUILTIN_RISK.get(name, "write")


def S(desc: str, enum: list | tuple | None = None, **extra) -> dict:
    """A string argument; enum lists the only values it accepts (clean_args checks them, Phase 3)."""
    return {"type": "string", "description": desc, **({"enum": list(enum)} if enum else {}), **extra}


def I(desc: str, lo: int | None = None, hi: int | None = None) -> dict:
    """An integer argument; lo and hi are the range clean_args checks (Phase 3)."""
    out: dict = {"type": "integer", "description": desc}
    if lo is not None:
        out["minimum"] = lo
    if hi is not None:
        out["maximum"] = hi
    return out


def specs(names: list[str]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "parameters": t.parameters}
            for n in names if (t := REGISTRY.get(n))]


# gpt-oss and other models sometimes call the tools they were trained with; map the obvious ones.
ALIASES = {"browser.search": "web_search", "search": "web_search", "browser.open": "fetch_url", "open": "fetch_url",
           "open_url": "fetch_url", "browse": "fetch_url", "read": "read_file", "write": "write_file",
           "python": "run_code", "container.exec": "run_code", "ls": "list_files",
           # Phase 0.5: names local models invent for finding and changing things
           "search_file": "search", "search_files": "search", "search_text": "search", "find_in_files": "search",
           "grep_files": "search", "find": "search", "cat": "read_file", "open_file": "read_file",
           "view_file": "read_file", "replace": "edit_file", "patch": "edit_file", "str_replace": "edit_file",
           "status": "company_status"}
ARG_RENAMES = {"fetch_url": {"id": "url", "link": "url"}, "web_search": {"q": "query", "topn": "max_results"},
               "run_code": {"script": "code", "cmd": "code"},
               "read_file": {"file": "path", "filename": "path", "file_path": "path"},
               "write_file": {"file": "path", "filename": "path", "file_path": "path", "text": "content", "body": "content"},
               "create_task": {"role": "assign_to", "assignee": "assign_to", "assigned_to": "assign_to",
                               "details": "description"},
               "finish": {"result": "summary", "message": "summary"}}


def resolve(ctx: Ctx, name: str, args: dict) -> tuple[str, dict]:
    if name not in REGISTRY and name in ALIASES:
        name = ALIASES[name]
    elif name in REGISTRY and ctx.allowed and name not in ctx.allowed and ALIASES.get(name) in ctx.allowed:
        # A real tool this agent does not have, whose older alias it does have. 'search' meant web_search before
        # it became Holdco's own search, and gpt-oss still calls its built-in browser search that way.
        name = ALIASES[name]
    renames = ARG_RENAMES.get(name, {})
    args = {renames.get(k, k): v for k, v in (args or {}).items()}
    if name == "run_code" and "code" in args and "language" not in args:
        args["language"] = "python"
    return name, args


def execute(ctx: Ctx, name: str, args: dict) -> str:
    if name in ctx.loaded_by_name:   # Phase 1: a connector tool find_tool loaded into this session
        spec = ctx.loaded_by_name[name]
        try:
            with taint.current(ctx):
                out = str(connectors.call(ctx, name, clean_args(spec, args)))
            out = taint.after_tool(ctx, name, args, out)   # Phase 3: external reads taint the session
        except ArgError as e:   # Phase 3: the repair message is the result; the model fixes the call next step
            out = f"ERROR: bad arguments for {name}: {e}"
        except (ToolError, ValueError) as e:
            out = f"ERROR: {e}"
        except Exception as e:   # a connected service must never crash the run
            out = f"ERROR: {type(e).__name__}: {e}"
        return out[:MAX_RESULT] + (f"\n[truncated {len(out) - MAX_RESULT} characters]" if len(out) > MAX_RESULT else "")
    name, args = resolve(ctx, name, args)
    t = REGISTRY.get(name)
    if not t or (ctx.allowed and name not in ctx.allowed):
        close =difflib.get_close_matches(name, sorted(ctx.allowed), n=3, cutoff=0.5)
        hint = f" Did you mean {' or '.join(close)}?" if close else ""
        return (f"ERROR: there's no tool called '{name}' available to you.{hint} Your tools are: "
                f"{', '.join(sorted(ctx.allowed))}.")
    try:
        with taint.current(ctx):   # Phase 3: actions.gate sees what this session read
            out = t.fn(ctx, **_clean_args(t, args))
        out = taint.after_tool(ctx, name, args, out)
    except ArgError as e:
        out = f"ERROR: bad arguments for {name}: {e}"
    except TypeError as e:
        out = f"ERROR: bad arguments for {name}: {e}. Expected: {json.dumps(t.parameters['properties'])}"
    except (ToolError, workspace.PathError, web.WebError, ValueError) as e:
        out = f"ERROR: {e}"
    except Exception as e:  # never crash the run on a tool failure
        out = f"ERROR: {type(e).__name__}: {e}"
    out = str(out)
    if len(out) > MAX_RESULT:
        out = out[:MAX_RESULT] + f"\n[truncated {len(out) - MAX_RESULT} characters]"
    return out


def _clean_args(t: Tool, args: dict) -> dict:
    return clean_args({"parameters": t.parameters}, args)


class ArgError(ToolError):
    """Arguments that do not fit the tool's schema, one repair line per problem (Phase 3, design section 2).
    tools.execute returns them as "ERROR: bad arguments for <tool>: <lines>"."""


_NUMBER_RE = re.compile(r"[#$]?\s*([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[+-]?\.\d+)")
_TRUE, _FALSE = ("true", "yes", "y", "on", "1"), ("false", "no", "n", "off", "0", "none")


def _as_number(v) -> float | None:
    """v as a number when it plainly is one: 3, 2.5, "3", " 2.5", "#12", "$19", "1,200". None otherwise."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v and abs(v) != float("inf") else None
    m = _NUMBER_RE.fullmatch(v.strip()) if isinstance(v, str) else None
    return float(m.group(1).replace(",", "")) if m else None


def _shown(v) -> str:
    """A sent value as a repair message quotes it: strings in quotes, the rest as JSON, both kept short."""
    text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    text = text if len(text) <= 60 else text[:57] + "..."
    return f"'{text}'" if isinstance(v, str) else text


def _num_text(x) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _range_text(prop: dict) -> str:
    lo, hi = prop.get("minimum"), prop.get("maximum")
    if lo is not None and hi is not None:
        return f" {_num_text(lo)}-{_num_text(hi)}"
    if lo is not None:
        return f" of at least {_num_text(lo)}"
    return f" of at most {_num_text(hi)}" if hi is not None else ""


def _in_range(prop: dict, n: float) -> bool:
    lo, hi = prop.get("minimum"), prop.get("maximum")
    return (lo is None or n >= lo) and (hi is None or n <= hi)


def _enum_value(v, enum: list):
    """The allowed value v means, or None. Case, spaces and hyphens do not matter ("In progress" is in_progress),
    and one clear spelling variant is accepted ("canceled" is cancelled); anything else is refused."""
    if v in enum:
        return v
    if not isinstance(v, str) or not all(isinstance(e, str) for e in enum):
        return None
    key = re.sub(r"[\s-]+", "_", v.strip().lower())
    norm = {re.sub(r"[\s-]+", "_", e.strip().lower()): e for e in enum}
    if key in norm:
        return norm[key]
    close =difflib.get_close_matches(key, list(norm), n=2, cutoff=0.9)
    return norm[close[0]] if len(close) == 1 else None


def _coerce(k: str, prop: dict, v) -> tuple[bool, object, str]:
    """One argument against its property: (given, value, problem). A null (gpt-oss sends goal_id: null), and an
    empty value for anything but free text, means the argument was left out. Values that plainly mean the declared
    type are converted ("3" is 3, "true" is true); anything else is a problem, worded as the model reads it back
    (design section 2's table)."""
    typ = prop.get("type")
    types = typ if isinstance(typ, list) else [typ]
    if v is None:
        return ("null" in types), None, ""
    empty = isinstance(v, str) and not v.strip()
    if "integer" in types:
        if empty:
            return False, None, ""
        n = _as_number(v)
        if n == 0 and (prop.get("minimum") or 0) > 0:
            return False, None, ""   # gpt-oss fills optional numbers with 0 ("due_days": 0): it means "none"
        if n is None or not n.is_integer() or not _in_range(prop, n):
            return False, None, f"{k} must be an integer{_range_text(prop)}; you sent {_shown(v)}"
        v = int(n)
    elif "number" in types:
        if empty:
            return False, None, ""
        n = _as_number(v)
        if n is None or not _in_range(prop, n):
            return False, None, f"{k} must be a number{_range_text(prop)}; you sent {_shown(v)}"
        v = v if isinstance(v, (int, float)) and not isinstance(v, bool) else n
    elif "boolean" in types:
        word = v.strip().lower() if isinstance(v, str) else None
        if isinstance(v, bool):
            pass
        elif empty:
            return False, None, ""
        elif word in _TRUE or word in _FALSE or (isinstance(v, int) and v in (0, 1)):
            v = word in _TRUE if word is not None else bool(v)
        else:
            return False, None, f"{k} must be true or false; you sent {_shown(v)}"
    elif "string" in types and not isinstance(v, str):
        v = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
    enum = prop.get("enum")
    if enum:
        if isinstance(v, str) and not v.strip():
            return False, None, ""
        value = _enum_value(v, list(enum))
        if value is None:
            return False, None, f"{k} must be one of {', '.join(str(e) for e in enum)}; you sent {_shown(v)}"
        v = value
    return True, v, ""


def _near_miss(key: str, lacking: list[str]) -> str | None:
    """The declared argument an unknown key was meant to be ("titel", "Title" or "assign-to"), among those the call
    lacks; None for every other unknown key, which is dropped without a word (models add harmless extras)."""
    flat =re.sub(r"[\s_-]+", "", str(key).lower())
    for p in lacking:
        if re.sub(r"[\s_-]+", "", p.lower()) == flat:
            return p
    close = difflib.get_close_matches(str(key).lower(), lacking, n=1, cutoff=0.8)
    return close[0] if close else None


def clean_args(spec: dict, args: dict) -> dict:
    """Check and convert arguments against the schema a spec declares (a Tool or a connector spec). Values that
    plainly mean the declared type are converted; every value that does not fit raises ArgError with one repair
    line per problem (design section 2), so the model can fix the call on its next step. Unknown keys are dropped,
    unless one is a near miss of a declared argument the call lacks."""
    params = spec.get("parameters") or {}
    props = params.get("properties") or {}
    required = list(params.get("required") or [])
    args = args or {}
    clean, problems, bad = {}, [], set()
    for k, v in args.items():
        if k not in props:
            continue
        given, value, problem = _coerce(k, props[k] if isinstance(props[k], dict) else {}, v)
        if problem:
            problems.append(problem)
            bad.add(k)
        elif given:
            clean[k] = value
    lacking = [p for p in props if p not in args]
    near: dict[str, str] = {}
    for k in args:
        if k not in props:
            meant = _near_miss(k, [p for p in lacking if p not in near.values()])
            if meant:
                near[k] = meant
    if near:
        expected = ", ".join([r for r in required if r in props] + [p for p in props if p not in required])
        problems += [f"unknown argument '{k}'; expected {expected}" for k in near]
    missing = [r for r in required if r not in clean and r not in bad and r not in near.values()]
    if missing:
        problems.append(f"missing required argument(s): {', '.join(missing)}")
    if problems:
        raise ArgError("\n".join(problems))
    return clean


def _log(ctx: Ctx, kind: str, message: str, **data) -> None:
    bus.emit(ctx.company["id"], kind, message, ctx.agent["id"], data or None)


def _task_id(ctx: Ctx) -> int | None:
    return ctx.task["id"] if ctx.task else None


# ======================= research =======================

@tool("web_search", "Search the web. Returns titles, URLs and snippets.",
      {"query": S("What to search for"), "max_results": I("How many results, 1-10 (default 6)")}, ("query",))
def web_search(ctx: Ctx, query: str, max_results: int = 6) -> str:
    _log(ctx, "tool", f"searched the web for “{query}”")
    try:
        results = web.search(query, max_results, ctx.settings)
    except web.WebError as e:
        # Every provider failed. Tell the agent once and clearly (each retry would waste a step) and tell the
        # owner once per company per half hour instead of once per search.
        reasons = str(e).removeprefix("All search providers failed: ")
        if web.search_alert_due(ctx.company["id"]):
            _log(ctx, "error", f"Web search is unavailable: {reasons}")
        return (f"ERROR: web search is unavailable right now ({reasons}).\n"
                "Do not retry web_search this session. Continue with URLs you already know (fetch_url still works), "
                "note the gap with note_progress when you have that tool, and say in your finish summary that "
                "research was skipped.")
    except Exception as e:
        return f"ERROR: search failed ({type(e).__name__}: {e}). Try other words or fetch a known URL directly."
    if not results:
        return "No results. Try different words."
    return "\n\n".join(f"{i + 1}. {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(results))


@tool("fetch_url", "Read a public web page as text.",
      {"url": S("Full http(s) URL"), "include_links": {"type": "boolean", "description": "Also list links on the page"}},
      ("url",))
def fetch_url(ctx: Ctx, url: str, include_links: bool = False) -> str:
    _log(ctx, "tool", f"read {url[:120]}")
    text = web.fetch_page(url, include_links=include_links)
    truth.cache_page(ctx, url, text)   # record_fact and check_sources verify against what was really fetched
    return text


# ======================= files =======================

@tool("list_files", "List files in the company folder, each with a one-line summary.",
      {"path": S("Sub-folder (default: everything)")})
def list_files(ctx: Ctx, path: str = ".") -> str:
    return context.list_files(ctx, path)


@tool("read_file", "Read a text file: the company folder, the owner's context (context:<path>, global:<path>), or "
      "a web page your team already fetched (page:<url>). Reads by line range; outline=true lists the headings "
      "with line numbers.",
      {"path": S("Path relative to the company folder, e.g. research/market.md"),
       "start_line": I("First line to read (default 1)"), "end_line": I("Last line to read (default: 200 lines)"),
       "outline": {"type": "boolean", "description": "Only list the headings with their line numbers"},
       "offset": I("Old style: character offset to start from")}, ("path",))
def read_file(ctx: Ctx, path: str, start_line: int = 0, end_line: int = 0, outline: bool = False,
              offset: int = 0) -> str:
    return context.read_file(ctx, path, offset=offset, start_line=start_line, end_line=end_line, outline=outline)


@tool("write_file", "Create or overwrite a text file in the company folder (website, docs, code, drafts...).",
      {"path": S("Path relative to the company folder, e.g. site/index.html"),
       "content": S("The complete file content"),
       "append": {"type": "boolean", "description": "Append instead of overwriting"}}, ("path", "content"))
def write_file(ctx: Ctx, path: str, content: str, append: bool = False) -> str:
    p = workspace.safe_path(ctx.company, path)
    if p.is_dir():
        raise ToolError(f"{path} is a folder.")
    if len(content) > 500_000:
        raise ToolError("Content too large (max 500 KB per file).")
    r = workspace.rel(ctx.company, p)
    # Phase 0.5 guards: records/ and context/ protection, log-like names, future dates, unsupported claims,
    # and the one charter path. Each returns the full refusal text or None.
    full = (p.read_text(encoding="utf-8", errors="replace") + content) if append and p.exists() else content
    refusal = workspace.lease(ctx.company, r, ctx)   # Phase 2: another run is editing this file
    if refusal:
        return refusal
    for guard in (truth.write_guard, planning.charter_write_hook):
        refusal = guard(ctx, r, full)
        if refusal:
            return refusal
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    if append and existed:
        with p.open("a", encoding="utf-8", newline="") as f:
            f.write(content)
    else:
        p.write_text(content, encoding="utf-8", newline="")
    ctx.written.add(r)
    _log(ctx, "file", f"{'updated' if existed else 'created'} {r}", path=r, task_id=_task_id(ctx))
    context.after_write(ctx, r)
    return f"Saved {r} ({len(content)} characters)." + (
        " For big files, write a first section, then add the rest with append=true or change parts with edit_file."
        if len(content) > 8000 else "")


@tool("delete_file", "Move a file to the company's trash folder.", {"path": S("Path relative to the company folder")},
      ("path",))
def delete_file(ctx: Ctx, path: str) -> str:
    p = workspace.safe_path(ctx.company, path)
    if not p.exists() or p == workspace.root(ctx.company).resolve():
        raise ToolError(f"{path} doesn't exist.")
    # Phase 0.5: records/ is Holdco's account of what happened and context/ is the owner's library; agents may
    # not delete them (or their folders) any more than overwrite them. Same refusals as truth.write_guard, on the
    # resolved path, so "docs/../records" or "Records" cannot get round it.
    first = workspace.rel(ctx.company, p).lower().split("/", 1)[0].rstrip(". ")
    if first in ("records", "context"):
        msg = (getattr(truth, "RECORDS_REFUSAL", "") or "ERROR: records/ is written by Holdco from what really "
               "happened. Write plans and notes elsewhere.") if first == "records" else \
            (getattr(truth, "CONTEXT_REFUSAL", "") or "ERROR: context/ is the owner's. Save your notes elsewhere, "
             "for example docs/.")
        refused = getattr(truth, "_refused", None)
        if refused:
            try:
                refused(ctx, "delete_file", first, msg, path=workspace.rel(ctx.company, p))
            except Exception:
                pass
        return msg
    refusal = workspace.lease(ctx.company, workspace.rel(ctx.company, p), ctx)   # Phase 2: file leases
    if refusal:
        return refusal
    dest = workspace.trash(ctx.company, p)
    _log(ctx, "file", f"moved {path} to trash")
    return f"Moved to {dest}."


# ======================= tasks =======================

def _team_roles(ctx: Ctx) -> dict[str, dict]:
    return {a["role"]: a for a in db.q("SELECT * FROM agents WHERE company_id=? AND enabled=1", (ctx.company["id"],))}


def _role_named(team: dict[str, dict], assign_to: str) -> str:
    """The role key for assign_to, which models give as a role ("marketing") or a name ("Echo")."""
    wanted = (assign_to or "").strip().lower()
    if wanted in team:
        return wanted
    return next((r for r, a in team.items() if (a.get("name") or "").strip().lower() == wanted), wanted)


@tool("create_task", "Create a task for a teammate (or yourself). Keep it small enough for one work session.",
      {"title": S("Short imperative title"),
       "description": S("Objective, inputs (files to read), and what's out of scope"),
       "assign_to": S("Role or name of the teammate (see the Team list)"),
       "deliverable": S("Where the result will be: a file path like site/index.html, or an action like 'email queued'"),
       "acceptance": S("3-5 checkable acceptance criteria, one per line"),
       "priority": I("1 = urgent, 2 = normal, 3 = later", 1, 3),
       "kind": S("Optional: outreach, deploy, post, purchase, ask, research, build or write"),
       "goal_id": I("The active goal this task serves, e.g. 1"),
       "expected": S("What this task should move and by how much"),
       "inputs": S("Files and facts to read first"),
       "depends_on": S("Task ids that must be done first, e.g. 12, 14"),
       "due_days": I("Due in this many days (1-90)", 1, 90),
       "checks": S("Tool checks to run when it is done, one per line, e.g. browser_check site/index.html")},
      ("title", "assign_to"))
def create_task(ctx: Ctx, title: str, assign_to: str, description: str = "", deliverable: str = "",
                acceptance: str = "", priority: int = 2, kind: str = "", goal_id: int = 0, expected: str = "",
                inputs: str = "", depends_on: str = "", due_days: int = 0, budget_steps: int = 0,
                budget_tokens: int = 0, checks: str = "") -> str:
    kind = truth.classify_task(title, description, deliverable, kind)
    refusal = truth.task_guard(ctx, kind, title)
    if refusal:
        return refusal
    refusal = contracts.rhythm_task_problem(ctx, title)
    if refusal:
        return refusal
    team = _team_roles(ctx)
    role = _role_named(team, assign_to)
    if role not in team:
        match = next((r for r in team if r in role or role in r), None)
        if not match:
            by_title = next((r for r, a in team.items() if role in (a["title"] or "").lower()), None)
            if not by_title:
                raise ToolError(f"No teammate with role '{assign_to}'. Team roles: {', '.join(team)}")
            match = by_title
        role = match
    dup = db.one(f"SELECT id FROM tasks WHERE company_id=? AND lower(title)=lower(?) AND status IN "
                 f"({','.join('?' * len(OPEN_STATUSES))})", (ctx.company["id"], title.strip(), *OPEN_STATUSES))
    if dup:
        return f"A task with that title already exists (#{dup['id']}). Update it instead of duplicating it."
    if deliverable.strip():
        same = db.one(f"SELECT id, title FROM tasks WHERE company_id=? AND deliverable=? AND status IN "
                      f"({','.join('?' * len(OPEN_STATUSES))})", (ctx.company["id"], deliverable.strip(), *OPEN_STATUSES))
        if same:
            return (f"Task #{same['id']} ({same['title']}) already produces {deliverable}. Add to that task with "
                    f"update_task, or choose a different deliverable.")
    open_count = db.scalar(f"SELECT COUNT(*) FROM tasks WHERE company_id=? AND status IN "
                           f"({','.join('?' * len(OPEN_STATUSES))})", (ctx.company["id"], *OPEN_STATUSES))
    if open_count >= 40:
        raise ToolError("There are already 40 open tasks. Finish or cancel some first.")
    # Phase 2: the task contract (goal, expected effect, inputs, dependencies, deadline, budgets, checks).
    if not goal_id and ctx.task and ctx.task.get("goal_id") and db.one(
            "SELECT id FROM goals WHERE id=? AND company_id=? AND status='active'",
            (ctx.task["goal_id"], ctx.company["id"])):
        goal_id = ctx.task["goal_id"]   # a follow-up serves the goal of the task it came from
    contract = contracts.validate(ctx.company["id"], {
        "goal_id": goal_id, "expected": expected, "inputs": inputs, "depends_on": depends_on, "due_days": due_days,
        "budget_steps": budget_steps, "budget_tokens": budget_tokens, "checks": checks})
    ts = db.now()
    cols = {"company_id": ctx.company["id"], "title": title.strip()[:200], "description": description, "role": role,
            "agent_id": team[role]["id"], "status": "todo", "priority": int(priority or 2),   # 1-3: the schema
            "created_by": ctx.agent["name"], "deliverable": deliverable.strip()[:200],
            "acceptance": acceptance.strip()[:2000], "kind": kind, "created_at": ts, "updated_at": ts, **contract}
    tid = db.run(f"INSERT INTO tasks({', '.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values()))
    _log(ctx, "task", f"created task #{tid} for {team[role]['name']}: {title}", task_id=tid)
    bus.notify("tasks", company_id=ctx.company["id"])
    tips = []
    if not (deliverable.strip() and acceptance.strip()):
        tips.append("tasks with a deliverable and acceptance criteria get finished far more reliably.")
    if contract.get("goal_id") and not contract.get("expected"):
        tips.append("say what it should move with expected.")
    tip = (" Tip: " + " Also ".join(tips)) if tips else ""
    warning = contracts.no_goal_warning(ctx.company["id"], tid) \
        if ctx.agent.get("role") == "ceo" and not contract.get("goal_id") else None
    if warning:
        return warning + tip
    return f"Created task #{tid} for {team[role]['name']} ({role}).{tip}"


@tool("update_task", "Change a task's status or add a note (e.g. cancel a stale task, unblock one, record progress).",
      {"task_id": I("Task number"),
       "status": S("todo, in_progress, done, blocked, waiting or cancelled", enum=TASK_STATUSES),
       "note": S("A short note to add"), "assign_to": S("Reassign to another role"),
       "goal_id": I("Attach the task to this active goal"),
       "due_days": I("Due in this many days from now (1-90)", 1, 90)},
      ("task_id",))
def update_task(ctx: Ctx, task_id: int, status: str = "", note: str = "", assign_to: str = "", goal_id: int = 0,
                due_days: int = 0) -> str:
    t = db.get_task(task_id)
    if not t or t["company_id"] != ctx.company["id"]:
        raise ToolError(f"Task #{task_id} not found.")
    fields: dict = {"updated_at": db.now()}
    if status and ctx.task and ctx.task["id"] == t["id"]:
        # The real-model run marked its own weekly review done with update_task, skipping finish's checks.
        return (f"ERROR: task #{task_id} is the task you are working on now. End it with finish (status done, "
                f"partial, blocked or waiting), not update_task.")
    queued = ""
    if status:   # one of TASK_STATUSES: the schema's enum (clean_args)
        if status == "in_progress" and t["status"] != "in_progress":
            # Holdco marks a task in progress when an agent starts it; set by hand, no run would ever pick it up.
            status = "todo"
            queued = " It is queued as todo: Holdco marks a task in_progress when an agent starts it."
        fields["status"] = status
    if note:
        fields["result"] = ((t["result"] or "") + f"\n[{ctx.agent['name']}] {note}").strip()
    if assign_to:
        team = _team_roles(ctx)
        role = _role_named(team, assign_to)
        if role not in team:
            raise ToolError(f"No teammate with role '{assign_to}'. Team roles: {', '.join(team)}")
        fields.update(role=role, agent_id=team[role]["id"])
        if role != (t["role"] or ""):
            fields["reassigned"] = int(t.get("reassigned") or 0) + 1   # the watchdog spots tasks that keep bouncing
    change = {k: v for k, v in (("goal_id", goal_id), ("due_days", due_days)) if v}
    if change:
        fields.update(contracts.validate(ctx.company["id"], change, t))   # Phase 2: attach a goal, move the deadline
    db.update("tasks", task_id, **fields)
    _log(ctx, "task", f"updated task #{task_id}{' → ' + status if status else ''}", task_id=task_id)
    bus.notify("tasks", company_id=ctx.company["id"])
    return f"Task #{task_id} updated.{queued}"


@tool("list_tasks", "List the company's tasks.", {"status": S("open (default), done, or all")})
def list_tasks(ctx: Ctx, status: str = "open") -> str:
    if status == "all":
        rows = db.q("SELECT * FROM tasks WHERE company_id=? ORDER BY id DESC LIMIT 60", (ctx.company["id"],))
    elif status == "done":
        rows = db.q("SELECT * FROM tasks WHERE company_id=? AND status='done' ORDER BY updated_at DESC LIMIT 30",
                    (ctx.company["id"],))
    else:
        rows = db.q(f"SELECT * FROM tasks WHERE company_id=? AND status IN ({','.join('?' * len(OPEN_STATUSES))})"
                    f" ORDER BY priority, id", (ctx.company["id"], *OPEN_STATUSES))
    if not rows:
        return "No tasks."
    statuses = contracts.dependency_status(rows)   # Phase 2: goal, due date and what each task waits on
    return "\n".join(f"#{t['id']} [{t['status']}] ({t['role']}) p{t['priority']} {t['title']}"
                     + (f" -> {t['deliverable']}" if t.get("deliverable") else "")
                     + contracts.task_tags(t, statuses)
                     + (f" | {t['result'][-160:]}" if t["result"] else "") for t in rows)


# ======================= memory & metrics =======================

MEMORY_KINDS = ("fact", "decision", "lesson", "contact", "note")


@tool("remember", "Save a fact, decision or lesson to the company's shared memory.",
      {"content": S("What to remember (one clear statement)"),
       "kind": S("fact, decision, lesson, contact or note", enum=MEMORY_KINDS),
       "pin": {"type": "boolean", "description": "Pin it so every agent always sees it (use sparingly)"}},
      ("content",))
def remember(ctx: Ctx, content: str, kind: str = "note", pin: bool = False) -> str:
    # Phase 3: with an embedding model and a small model, a note that says what an existing one says is not saved
    # twice (the nearest note above 0.92 similarity, confirmed by the small model).
    same = embeddings.duplicate_memory(ctx.company, content, kind)
    if same:
        if pin:   # the agent wants every agent to see it: the note that is already there gets the pin
            db.run("UPDATE memory SET pinned=1 WHERE id=? AND company_id=?", (same, ctx.company["id"]))
            return f"Already in memory (#{same}); pinned it, so every agent sees it."
        return f"Already in memory (#{same})."
    mid = db.run("INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(?,?,?,?,?,?)",
                 (ctx.company["id"], ctx.agent["id"], (kind or "note")[:20], content[:2000], int(bool(pin)), db.now()))
    _log(ctx, "memory", f"noted: {content[:140]}")
    return f"Saved to memory (#{mid})."


@tool("recall", "Search the company's shared memory (notes, decisions, owner answers).",
      {"query": S("Words to look for")}, ("query",))
def recall(ctx: Ctx, query: str) -> str:
    # One search for everything since Phase 0.5; recall stays for agents and transcripts that know the old name.
    return context.search(ctx, query, scope="memory", limit=15)


@tool("record_metric", "Record a KPI value (e.g. visitors, signups, revenue_usd, emails_sent, leads).",
      {"name": S("Metric name, snake_case"), "value": {"type": "number", "description": "The value"},
       "note": S("Where the number came from")}, ("name", "value"))
def record_metric(ctx: Ctx, name: str, value: float, note: str = "") -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:40]
    db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, created_at) VALUES(?,?,?,?,?,?)",
           (ctx.company["id"], ctx.agent["id"], name, value, note[:300], db.now()))
    _log(ctx, "metric", f"recorded {name} = {value:g}")
    return f"Recorded {name} = {value:g}."


# ======================= owner =======================

@tool("report_to_owner", "Send the owner a message in the dashboard chat. Use for meaningful updates or when they asked you something.",
      {"message": S("The message (concise, Markdown allowed)")}, ("message",))
def report_to_owner(ctx: Ctx, message: str) -> str:
    refusal = truth.claims_guard(ctx, message, "report")
    if refusal:
        return refusal
    db.run("INSERT INTO messages(company_id, agent_id, sender, content, created_at) VALUES(?,?,?,?,?)",
           (ctx.company["id"], ctx.agent["id"], "agent", message, db.now()))
    _log(ctx, "message", f"messaged the owner: {message[:160]}")
    bus.notify("messages", company_id=ctx.company["id"])
    if ctx.kind == "chat":
        ctx.reply = message
        ctx.reply_sent = True
        ctx.finished = True
    return "Delivered to the owner."


@tool("ask_owner", "Ask the owner a question only they can answer (a decision, an account, a preference). Give "
      "options and your recommendation whenever there is a choice; they become buttons for the owner.",
      {"question": S("The question"), "options": S("The choices, one per line (optional)"),
       "recommended": S("The option you recommend, copied exactly (optional)"),
       "context": S("Why you are asking and what happens with each option (optional)")}, ("question",))
def ask_owner(ctx: Ctx, question: str, options: str = "", recommended: str = "", context: str = "") -> str:
    refusal = truth.claims_guard(ctx, "\n".join((question, context)), "ask")
    if refusal:
        return refusal
    return planning.ask_owner(ctx, question, options, recommended, context)


# ======================= outbound =======================

@tool("send_email", "Send an email from the company mailbox (may need the owner's approval).",
      {"to": S("Recipient email address"), "subject": S("Subject line"), "body": S("Plain-text body, signed with the company name"),
       "reply_to_email_id": I("If replying to an inbound email, its number")}, ("to", "subject", "body"))
def send_email(ctx: Ctx, to: str, subject: str, body: str, reply_to_email_id: int = 0) -> str:
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to.strip()):
        raise ToolError("That doesn't look like a valid email address.")
    # Phase 3: the daily cap first (nothing else matters once it is reached), worded and counted exactly as
    # outreach_check words and counts it, so the agent reads one rule whichever check meets it.
    cap = verify.cap_problem(ctx.company)
    if cap:
        return "ERROR: email not sent: " + cap
    refusal, body = truth.outreach_guard(ctx, to.strip(), subject, body)
    if refusal:
        return refusal
    payload = {"to": to.strip(), "subject": subject, "body": body}
    if reply_to_email_id:
        payload["reply_to_email_id"] = int(reply_to_email_id)
    problems = verify.outreach_check(ctx, payload)   # Phase 3: no trust level or auto policy skips these rules
    if problems:
        return "ERROR: email not sent: " + "; ".join(problems)
    return actions.gate(ctx.company, ctx.agent, _task_id(ctx), "send_email", payload)


@tool("check_inbox", "Read recent inbound emails that haven't been handled.",
      {"limit": I("How many (default 5)"), "include_handled": {"type": "boolean", "description": "Include handled ones"}})
def check_inbox(ctx: Ctx, limit: int = 5, include_handled: bool = False) -> str:
    cond = "" if include_handled else "AND status='new'"
    rows = db.q(f"SELECT * FROM emails WHERE company_id=? AND direction='in' {cond} ORDER BY id DESC LIMIT ?",
                (ctx.company["id"], max(1, min(int(limit or 5), 20))))
    if not rows:
        cs = db.company_settings(ctx.company)
        if not (cs.get("imap_host") and cs.get("imap_user")):
            return "The inbox isn't connected yet (the owner can add IMAP details in Settings)."
        return "No new emails."
    return "\n\n".join(f"Email #{e['id']} ({e['status']}) from {e['from_addr']} at {e['created_at']}\n"
                       f"Subject: {e['subject']}\n{(e['body'] or '')[:2500]}" for e in rows)


@tool("mark_email_handled", "Mark an inbound email as handled.",
      {"email_id": I("Email number"), "note": S("What was done")}, ("email_id",))
def mark_email_handled(ctx: Ctx, email_id: int, note: str = "") -> str:
    db.run("UPDATE emails SET status='handled' WHERE id=? AND company_id=?", (email_id, ctx.company["id"]))
    return f"Email #{email_id} marked handled."


@tool("social_post", "Publish a social media post (may need the owner's approval).",
      {"platform": S("x, linkedin, reddit, instagram, facebook, threads, bluesky..."),
       "content": S("The post text"), "link": S("Optional URL to include")}, ("platform", "content"))
def social_post(ctx: Ctx, platform: str, content: str, link: str = "") -> str:
    refusal = contracts.post_budget_problem(ctx)   # Phase 2: the weekly post budget
    if refusal:
        return refusal
    return actions.gate(ctx.company, ctx.agent, _task_id(ctx), "social_post",
                        {"platform": platform.lower(), "content": content, "link": link})


@tool("deploy_site", "Publish the website in site/ to the internet (may need the owner's approval).", {})
def deploy_site(ctx: Ctx) -> str:
    if not workspace.safe_path(ctx.company, "site/index.html").exists():
        raise ToolError("Create site/index.html first.")
    refusal = truth.deploy_guard(ctx)
    if refusal:
        return refusal
    return actions.gate(ctx.company, ctx.agent, _task_id(ctx), "deploy_site", {"folder": "site"})


@tool("http_request", "Call an outside web API. GET runs directly; POST/PUT/PATCH/DELETE may need approval.",
      {"url": S("Full URL"), "method": S("GET, POST, PUT, PATCH or DELETE", enum=HTTP_METHODS),
       "body": S("JSON body as a string"),
       "headers": S("JSON object of extra headers, as a string")}, ("url",))
def http_request(ctx: Ctx, url: str, method: str = "GET", body: str = "", headers: str = "") -> str:
    method = (method or "GET").upper()
    if method == "GET":
        _log(ctx, "tool", f"called {url[:120]}")
        return web.fetch_page(url, max_chars=8000)
    hdrs = {}
    if headers:
        try:
            hdrs = json.loads(headers)
        except ValueError:
            raise ToolError("headers must be a JSON object string") from None
    try:
        parsed_body = json.loads(body) if body else {}
    except ValueError:
        parsed_body = body
    web.check_public_url(url)
    return actions.gate(ctx.company, ctx.agent, _task_id(ctx), "http_post",
                        {"method": method, "url": url, "body": parsed_body, "headers": hdrs})


NETWORK_RE = re.compile(r"\bnetwork\s*=\s*true\b", re.I)   # Phase 3: a task that lets run_code use the network


@tool("run_code", "Run a Python script or shell command inside the company folder (may need approval).",
      {"language": S("python or shell"), "code": S("The code to run"), "timeout": I("Seconds, max 600 (default 60)")},
      ("code",))
def run_code(ctx: Ctx, code: str, language: str = "python", timeout: int = 60) -> str:
    lang = "python" if (language or "python").lower().startswith("py") else "shell"
    # Phase 3: the sandbox runs without a network unless the task says network=true (the owner can also tick
    # "Allow network" on the approval card).
    task = ctx.task or {}
    # A task the inbox made quotes the sender's subject, so an outside sender could write network=true there.
    network = task.get("created_by") != "inbox" and \
        bool(NETWORK_RE.search(f"{task.get('title') or ''}\n{task.get('description') or ''}"))
    return actions.gate(ctx.company, ctx.agent, _task_id(ctx), "run_code",
                        {"language": lang, "code": code, "timeout": int(timeout or 60), "network": network})


@tool("request_purchase", "Ask the owner to buy something (domain, tool, ads budget...). Money is never spent without them.",
      {"item": S("What to buy"), "amount_usd": {"type": "number", "description": "Cost in USD"},
       "vendor": S("Where to buy it"), "url": S("Link"), "reason": S("Why it's worth it now")},
      ("item", "amount_usd", "reason"))
def request_purchase(ctx: Ctx, item: str, amount_usd: float, reason: str, vendor: str = "", url: str = "") -> str:
    return actions.gate(ctx.company, ctx.agent, _task_id(ctx), "spend_money",
                        {"item": item, "amount_usd": amount_usd, "vendor": vendor, "url": url, "reason": reason})


# ======================= CEO =======================

@tool("update_charter", "Write or replace the company charter (Markdown). Optionally rename the company and set its one-liner and stage.",
      {"charter": S("The full charter in Markdown"), "company_name": S("New company name (optional)"),
       "one_liner": S("One sentence: what we do and for whom"),
       "stage": S("idea, building, launched or revenue", enum=COMPANY_STAGES)}, ("charter",))
def update_charter(ctx: Ctx, charter: str, company_name: str = "", one_liner: str = "", stage: str = "") -> str:
    missing = planning.validate_charter(charter)
    if missing:
        return ("ERROR: the charter is missing these sections, or they are empty: " + "; ".join(missing)
                + ". Add them (an estimate labelled as an estimate is fine) and call update_charter again.")
    fields: dict = {"charter": charter}
    if company_name.strip() and not db.company_settings(ctx.company).get("name_locked"):
        fields["name"] = company_name.strip()[:80]
    if one_liner.strip():
        fields["mission"] = one_liner.strip()[:300]
    if stage.strip().lower() in ("idea", "building", "launched", "revenue"):
        fields["stage"] = stage.strip().lower()
    db.update("companies", ctx.company["id"], **fields)
    ctx.company.update(fields)
    p = workspace.safe_path(ctx.company, "docs/charter.md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(charter, encoding="utf-8")
    _log(ctx, "charter", "updated the company charter" + (f" and named the company {fields['name']}" if "name" in fields else ""))
    bus.notify("company", company_id=ctx.company["id"])
    return "Charter saved to docs/charter.md."


@tool("hire_agent", "Add a specialist to the team, ideally from a preset (a researched, ready-made role).",
      {"preset": S("Preset key: " + ", ".join(presets.PRESETS)),
       "name": S("A first name (optional)"),
       "focus": S("What exactly they should focus on at this company"),
       "role": S("Only for a custom agent with no preset: a short role key"),
       "title": S("Only for a custom agent: job title")}, ())
def hire_agent(ctx: Ctx, preset: str = "", name: str = "", focus: str = "", role: str = "", title: str = "") -> str:
    preset = preset.strip().lower()
    if preset and preset not in presets.PRESETS:
        raise ToolError(f"Unknown preset '{preset}'. Presets: {', '.join(presets.PRESETS)}")
    if not preset and not (role and title and focus):
        raise ToolError("Pick a preset, or give role, title and focus for a custom agent.")
    aid = roles.add_agent(ctx.company["id"], preset=preset, role=role, name=name, title=title, focus=focus)
    a = db.get_agent(aid)
    _log(ctx, "team", f"hired {a['name']}, {a['title']}", new_agent_id=aid)
    bus.notify("agents", company_id=ctx.company["id"])
    return f"Hired {a['name']} ({a['role']}). Assign them tasks with create_task(assign_to='{a['role']}')."


# ======================= checks =======================

@tool("audit_page", "Check an HTML page for accessibility, SEO, broken links, performance and honesty problems.",
      {"path": S("HTML file in the company folder, e.g. site/index.html"),
       "check_external_links": {"type": "boolean", "description": "Also fetch external links (slower)"}}, ("path",))
def audit_page(ctx: Ctx, path: str, check_external_links: bool = False) -> str:
    p = workspace.safe_path(ctx.company, path)
    if not p.is_file():
        raise ToolError(f"{path} doesn't exist.")
    site = workspace.safe_path(ctx.company, "site")
    in_site = site == p.parent or site in p.parents
    _log(ctx, "tool", f"audited {workspace.rel(ctx.company, p)}")
    return checks.audit_page(p, site if in_site else None, check_external_links)


@tool("check_contrast", "Measure WCAG colour contrast between a text colour and its background.",
      {"foreground": S("Text colour, e.g. #555555"), "background": S("Background colour, e.g. #ffffff"),
       "pairs": S("Optional: several pairs, one per line, as 'foreground background label'")}, ())
def check_contrast(ctx: Ctx, foreground: str = "", background: str = "", pairs: str = "") -> str:
    out = []
    if foreground and background:
        out.append(checks.contrast_report(foreground, background))
    for line in (pairs or "").splitlines():
        bits = line.replace(",", " ").split()
        if len(bits) >= 2:
            out.append(checks.contrast_report(bits[0], bits[1], " ".join(bits[2:])))
    if not out:
        raise ToolError("Give a foreground and background colour, or pairs.")
    return "\n".join(out)


@tool("review_text", "Check copy: reading grade, length, hype, unsourced numbers, and rules for the kind of text.",
      {"text": S("The text to check (or use path)"), "path": S("A file in the company folder to check instead"),
       "kind": S("landing, cold_email, email, support_reply, social, social_x, article or general",
                 enum=tuple(checks.GRADE_TARGET))}, ())
def review_text(ctx: Ctx, text: str = "", path: str = "", kind: str = "general") -> str:
    is_html = False
    if path and not text:
        p = workspace.safe_path(ctx.company, path)
        if not p.is_file():
            raise ToolError(f"{path} doesn't exist.")
        text = p.read_text(encoding="utf-8", errors="replace")
        is_html = p.suffix.lower() in (".html", ".htm")
    if not text.strip():
        raise ToolError("Give text or a path.")
    return checks.review_text(text, (kind or "general").lower(), is_html)


# ======================= control =======================

def _written_for_task(ctx: Ctx, rel_path: str, is_dir: bool) -> bool:
    """Was this file written while working on this task (this session or an earlier attempt)?"""
    if any(w == rel_path or (is_dir and w.startswith(rel_path.rstrip("/") + "/")) for w in ctx.written):
        return True
    if not ctx.task:
        return False
    for e in db.q("SELECT data FROM events WHERE company_id=? AND kind='file' AND agent_id IS NOT NULL "
                  "AND data LIKE ?", (ctx.company["id"], f'%"task_id": {ctx.task["id"]}}}')):
        path = (db.loads(e["data"], {}) or {}).get("path", "")
        if path == rel_path or (is_dir and path.startswith(rel_path.rstrip("/") + "/")):
            return True
    return False


def _deliverable_ok(ctx: Ctx, deliverable: str) -> str | None:
    """None if fine, otherwise what's wrong. Only checks things that look like file paths."""
    # Phase 2: the deliverable of a brief rhythm (evening brief, weekly review, monthly check) is the brief it
    # stores with write_brief, in this session or an earlier attempt at the same task. Without one the owner gets
    # no brief, so finishing as done is refused once like a missing file.
    task = ctx.task or {}
    kind = rhythms.brief_kind(task)
    if kind:
        if ctx.brief_written == kind or rhythms.stored_brief(ctx.company["id"], task):
            return None
        return (f"this task's deliverable is the {rhythms.BRIEF_LABEL[kind]}, and none is stored yet: write it with "
                f"write_brief(kind=\"{kind}\", body=...)")
    if ctx.brief_written:
        return None   # a brief stored outside its routine is a deliverable too
    d = (deliverable or "").strip().strip("`'\"")
    if not d or " " in d or not re.fullmatch(r"[\w./-]+", d) or not ("/" in d or "." in d):
        return None
    try:
        p = workspace.safe_path(ctx.company, d)
    except workspace.PathError:
        return None
    if not p.exists():
        return f"{d} doesn't exist"
    rel_path = workspace.rel(ctx.company, p)
    if not _written_for_task(ctx, rel_path, p.is_dir()):
        return f"{d} wasn't created or changed while working on this task"
    if p.is_file() and p.stat().st_size < 40:
        return f"{d} is nearly empty"
    return None


WAITING_REFUSAL = ("Not finished yet: nothing is waiting on the owner for this task. Ask with ask_owner (one question "
                   "with options and your recommendation), or finish with status 'blocked' and say what blocks you.")
WAITING_ON_LABEL = "Waiting on the owner: "


def _waiting_for(ctx: Ctx) -> int | None:
    """The pending approval this task waits on: its own question or request, else the pending one ask_owner
    folded its duplicate question into (ctx.waiting_on). None when nothing waits on the owner for it."""
    cid = ctx.company["id"]
    own = db.one("SELECT id FROM approvals WHERE company_id=? AND task_id=? AND status IN ('pending','running') "
                 "ORDER BY id DESC LIMIT 1", (cid, ctx.task["id"]))
    if own:
        return own["id"]
    if ctx.waiting_on and db.one("SELECT id FROM approvals WHERE id=? AND company_id=? AND status='pending'",
                                 (ctx.waiting_on, cid)):
        return int(ctx.waiting_on)
    return None


@tool("finish", "End your work session with a handoff for the team.",
      {"summary": S("What you did, in up to 5 short bullets"),
       "status": S("done, partial (some progress, more needed), blocked (can't proceed) or waiting (on the owner)",
                   enum=FINISH_STATUSES),
       "deliverable": S("Path of the main output"),
       "evidence": S("How you checked it: tool results, URLs"),
       "open_issues": S("Anything unfinished or uncertain"),
       "next_step": S("Suggested next task, if any")}, ("summary",))
def finish(ctx: Ctx, summary: str, status: str = "done", deliverable: str = "", evidence: str = "",
           open_issues: str = "", next_step: str = "") -> str:
    status = (status or "done").lower().strip()
    if status not in ("done", "partial", "blocked", "waiting"):
        status = "done"
    target = deliverable or (ctx.task or {}).get("deliverable", "")
    brief = rhythms.stored_brief(ctx.company["id"], ctx.task) if ctx.task else None
    if brief:
        # The brief is the deliverable, not the file path the model names for it (the real-model run named
        # briefs/daily-<date>.md, which never exists, and the next planning cycle flagged it MISSING).
        target = f"the {rhythms.BRIEF_LABEL[brief['kind']]} (brief #{brief['id']}, on the Overview)"
    if ctx.kind == "founding" and ctx.founding_stage:
        # Phase 0.5: founding runs have no task, so each stage's own deliverable is checked instead, whatever the
        # status. Once: the stage gets one more chance to save it, then its finish is accepted with a note.
        problem = planning.stage_problem(ctx.company, ctx.founding_stage)
        if problem and not ctx.stage_warned:
            ctx.stage_warned = True
            ctx.refused_finish = {"summary": summary, "status": status, "deliverable": deliverable,
                                  "evidence": evidence, "open_issues": open_issues, "next_step": next_step}
            return f"Not finished yet: {problem}"
        if problem:
            open_issues = (open_issues + f"\nStage check: {problem.split('. ', 1)[0]}.").strip()
    waiting_on = None
    if status == "waiting" and ctx.task:
        # Phase 0.5: 'waiting' means the owner has something to answer for this task: its own question or
        # request, or the pending one its duplicate question was folded into. Otherwise it would wait forever.
        waiting_on = _waiting_for(ctx)
        if waiting_on is None and not ctx.waiting_warned:
            ctx.waiting_warned = True
            return WAITING_REFUSAL
        if waiting_on is None:
            status = "blocked"
            open_issues = (open_issues + "\nNothing was waiting on the owner for this task.").strip()
    if status == "done" and ctx.task:
        problem = _deliverable_ok(ctx, target)
        if problem and not ctx.finish_warned:
            ctx.finish_warned = True
            return (f"Not finished yet: {problem}. Create or update it, then call finish again. If you can't, call "
                    f"finish with status 'partial' and explain what's missing.")
        if problem:
            status = "partial"
            open_issues = (open_issues + f"\nDeliverable check failed: {problem}.").strip()
    if status == "done" and ctx.task:
        # Phase 3: the verification gate, by kind of work and by the task's checks; once, like the deliverable check
        problem = verify.finish_problem(ctx, target, summary, evidence)
        waiting = verify.waiting_request(ctx) if problem else None
        if problem and waiting and verify.only_waiting(ctx):
            # The tests wait for the owner to approve running them (no sandbox): nothing to fix here, so the task
            # waits on that request, like Phase 0.5's waiting tasks, and reopens when the owner decides.
            status, waiting_on = "waiting", waiting
            open_issues = (open_issues + f"\nVerification waits on the owner: {problem}.").strip()
        elif problem and not ctx.verify_warned:
            ctx.verify_warned = True
            return (f"Not finished yet: {problem}. Fix it and call finish again, or finish with status 'partial' and "
                    f"explain.")
        elif problem:
            # Still failing after the push-back: partial, or waiting when the tests also wait for the owner (the
            # next attempt, after the owner's decision, sees what else fails).
            status, waiting_on = ("waiting", waiting) if waiting else ("partial", waiting_on)
            open_issues = (open_issues + f"\nVerification failed: {problem}.").strip()
    if status == "done":
        problem = truth.finish_guard(ctx, status, target, summary, evidence)
        if problem and not ctx.claims_warned:
            ctx.claims_warned = True
            return f"Not finished yet: {problem}"
        if problem:
            # The action this task exists for is queued for the owner: the task waits for the decision instead
            # of being retried; otherwise the check failed and the task is only partly done.
            kind = (ctx.task or {}).get("kind") or truth.classify_task((ctx.task or {}).get("title", ""))
            status = "waiting" if ctx.task and truth.action_state(ctx, kind) == "waiting" else "partial"
            open_issues = (open_issues + f"\nCheck failed: {problem}").strip()
    parts = [f"Status: {status}"]
    if target:
        parts.append(f"Deliverable: {target}")
    parts.append(f"Summary: {summary.strip()}")
    for label, val in (("Evidence", evidence), ("Open issues", open_issues), ("Suggested next", next_step)):
        if val and val.strip():
            parts.append(f"{label}: {val.strip()}")
    if status == "waiting" and waiting_on:
        # actions.decide reads this line: the task reopens once #N is decided, not on unrelated answers.
        parts.append(f"{WAITING_ON_LABEL}#{waiting_on}")
    ctx.finished = True
    ctx.finish_status = status
    ctx.finish_summary = "\n".join(parts)
    ctx.deliverable = target
    return "Session ended."


def role_tools(agent: dict, kind: str) -> list[dict]:
    return specs(roles.tools_for(agent, kind))


# Feature modules register extra tools with @tool when imported. Keep this at the very end so `tool`, `S`,
# `I`, `Ctx` and `ToolError` exist before they run.
from . import watchdog  # noqa: E402,F401  (note_progress lives with the working-notes logic)
from . import context, planning, truth  # noqa: E402,F401  (Phase 0.5: search, guards, owner questions)
from . import connectors, browser  # noqa: E402,F401  (Phase 1: find_tool, connector routing, browser_check)
from . import brain, contracts, decisions, rhythms  # noqa: E402,F401  (Phase 2: brain, contracts, rhythms, decisions)
from . import taint, verify  # noqa: E402,F401  (Phase 3: taint tracking, the verification gate)
from . import embeddings  # noqa: E402  (Phase 3: the duplicate check in remember)
for _name, _t in REGISTRY.items():   # tools registered before BUILTIN_RISK existed in a module keep their tag
    _t.risk = _t.risk if _t.risk != "write" else BUILTIN_RISK.get(_name, "write")
