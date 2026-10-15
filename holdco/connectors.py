"""Connector framework (Phase 1, docs/phases/phase-1-connect.md sections 4 to 6).

Definitions come from the conn_*.py modules (each exports DEFINITION, a dict or None). Instances live in the
connectors table: one per (key, company) for company-scoped connectors, one with company_id NULL for global ones.
Secret fields never touch the table: they go to the vault as connector.<instance id>.<field>.

This module owns:
- instance CRUD and the dashboard API (setup cards, save and test, remove, OAuth sign-in start);
- health checks (on save, and from the scheduler through check_if_stale in a background thread), the Doctor rows,
  the daily phone alert per failing instance, and the "down right now" lines of the agents' prompt;
- metric pipes (poll_if_due / poll_metrics) that write metrics rows with source = the connector key;
- find_tool, the catalog of tools the connected services offer, and call/execute, which route a loaded tool
  through the risk gate (read runs at once; anything else goes through actions.gate like other outbound actions).

A definition is a dict (see the design's section 5 table): label, category, kind (api | mcp-stdio | mcp-http |
builtin), scope (global | company), setup {where_to_get_key, permissions, fields, test_mode, docs_url, agents_can},
health(inst) -> (status, detail), optional metrics(inst, company) -> [(name, value, note)], tools(inst) for api
kinds, mcp {command, args, env | url, headers, classify, allow, blocked} for MCP kinds, optional test_mode(inst).
Optional extras this module also reads: health_timeout (seconds, default 15), oauth (bool, default true for
mcp-http), tag_for_call(tool, args) -> tag or None and refuse_call(tool, args) -> str or None (checked by call()).

Nothing here ever puts a secret in an event, a prompt, a tool result or a row: values come from the vault only
inside connector code, and every text that leaves this module passes vault.redact as a backstop.
"""
from __future__ import annotations

import fnmatch
import importlib
import json
import os
import re
import threading
import time

from . import actions, bus, config, db, roles, tools
from .routing import HTTPError, route, to_int

# The definition modules. A missing module, one that fails to import, or a None DEFINITION is skipped.
MODULES = ("conn_stripe", "conn_resend", "conn_playwright", "conn_buffer", "conn_brave")
KINDS = ("api", "mcp-stdio", "mcp-http", "builtin")
MCP_KINDS = ("mcp-stdio", "mcp-http")
SCOPES = ("global", "company")
STATUSES = ("ok", "warn", "bad")
MASK = "••••••••"                 # what the dashboard shows for a secret that is set; sending it back keeps it
UNCLASSIFIED = "unclassified"     # an MCP tool no classify rule matched: it asks like a publish
HEALTH_TIMEOUT = 15               # seconds a definition's health check may take before it counts as bad
METRICS_TIMEOUT = 60              # seconds one metrics() call may take
TOOL_CACHE_SECONDS = 600          # MCP tools/list results are kept this long per instance
FIND_SHOWN = 5                    # matches listed by find_tool
FIND_LOADED = 3                   # matches loaded into the session by find_tool
OFF_TEXT = "Turned off. Agents cannot use it."
GONE_TEXT = "This connector is not available in this version of Holdco."

DEFINITIONS: dict[str, dict] = {}

# A connector tool's risk tag decides which approval policy applies (read runs freely).
POLICY_OF_TAG = {"write": "connector_write", "publish": "connector_publish", "contact": "connector_contact",
                 "spend": "spend_money", "irreversible": "irreversible"}

_lock = threading.Lock()            # guards every cache and flag below
_clients: dict[int, object] = {}    # instance id -> open mcp.Client
_client_locks: dict[int, threading.Lock] = {}   # one creator per instance at a time
_tool_cache: dict[int, tuple[float, list]] = {}  # instance id -> (monotonic time, tools/list result)
_alerted: dict[int, str] = {}       # instance id -> date (YYYY-MM-DD) of its last phone alert
_checking = False                   # a background health-check pass is running
_polling = False                    # a background metrics poll is running
_last_poll: float | None = None     # monotonic time the last metrics poll started


def policy_for(tag: str) -> str | None:
    """The approval policy for a connector tool's tag. read needs none; an unknown tag asks like a publish."""
    return None if tag == "read" else POLICY_OF_TAG.get(tag, "connector_publish")


# ---------------------------------------------------------------- small helpers

def _env_seconds(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name) or default))
    except ValueError:
        return float(default)


def _redact(text: str) -> str:
    try:
        from . import vault   # imported here: vault imports db
        return vault.redact(text)
    except Exception:
        return text


def _one_line(text, n: int = 400) -> str:
    return " ".join(str(text or "").split())[:n]


def _clean(text, n: int = 400) -> str:
    """One line, capped, with any known secret scrubbed: for health details, notes and error texts."""
    return _one_line(_redact(str(text or "")), n)


def _error_text(e: BaseException) -> str:
    """A short, secret-free description of an exception for the owner or an agent."""
    try:
        from . import mcp
        if isinstance(e, mcp.AuthRequired):
            return "Sign in needed: press Sign in on this connector's card."
    except Exception:
        pass
    msg = str(e).strip()
    if not msg or isinstance(e, (KeyError, AttributeError, TypeError, IndexError, NotImplementedError)):
        msg = f"{type(e).__name__}: {msg}".rstrip(": ")
    return _clean(msg, 600)


def _timed(fn, timeout: float, *args):
    """Run fn(*args) in a daemon thread and give up after timeout seconds (TimeoutError). The thread is left to
    finish on its own: Python cannot stop it, and a stuck health check must not hold up the caller."""
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as e:   # handed to the caller below
            box["error"] = e

    t = threading.Thread(target=run, daemon=True, name="connector-call")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"took longer than {timeout:g} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _cid(company) -> int | None:
    if company is None:
        return None
    if isinstance(company, dict):
        return company.get("id")
    return to_int(company) or None


def _label(d: dict | None, key: str) -> str:
    return str((d or {}).get("label") or key)


def _jsonable(v):
    """A copy of a setup card without callables, safe to send to the dashboard."""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items() if not callable(x)}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v if not callable(x)]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


# ---------------------------------------------------------------- instances and secrets

def _inst(row: dict | None) -> dict | None:
    """A connectors row with its config parsed into a dict: the "inst" every definition function receives."""
    if not row:
        return None
    inst = dict(row)
    cfg = db.loads(row.get("config"), {})
    inst["config"] = cfg if isinstance(cfg, dict) else {}
    return inst


def get(inst_id) -> dict | None:
    """One instance by id, or None."""
    return _inst(db.one("SELECT * FROM connectors WHERE id=?", (to_int(inst_id),)))


def instance(key: str, company: dict | None) -> dict | None:
    """The company's instance of a connector when one exists, else the global one, else None."""
    cid = _cid(company)
    if cid:
        row = db.one("SELECT * FROM connectors WHERE key=? AND company_id=? ORDER BY id LIMIT 1", (key, cid))
        if row:
            return _inst(row)
    return _inst(db.one("SELECT * FROM connectors WHERE key=? AND company_id IS NULL ORDER BY id LIMIT 1", (key,)))


def visible(company) -> list[dict]:
    """Enabled instances a company can use: its own, plus global ones for keys it has no enabled instance of.
    Instances of a definition that no longer exists are left out."""
    cid = _cid(company) or -1
    rows = db.q("SELECT * FROM connectors WHERE enabled=1 AND (company_id IS NULL OR company_id=?) ORDER BY id",
                (cid,))
    own = {r["key"] for r in rows if r["company_id"] is not None}
    out, seen = [], set()
    for r in rows:
        if r["key"] not in DEFINITIONS or r["key"] in seen or (r["company_id"] is None and r["key"] in own):
            continue
        seen.add(r["key"])
        out.append(_inst(r))
    return out


def _active_rows() -> list[dict]:
    """Enabled instances, leaving out those of archived (or deleted) companies."""
    return db.q("SELECT c.* FROM connectors c LEFT JOIN companies co ON co.id=c.company_id WHERE c.enabled=1 AND "
                "(c.company_id IS NULL OR co.status!='archived') ORDER BY c.id")


def _secret_fields(d: dict | None) -> list[str]:
    return [str(f["key"]) for f in ((d or {}).get("setup") or {}).get("fields") or []
            if isinstance(f, dict) and f.get("secret") and f.get("key")]


def _vault_name(inst_id: int, field: str) -> str:
    return f"connector.{inst_id}.{field}"


def secrets(inst: dict) -> dict:
    """{field: value} for the definition's secret fields ("" when not set). Only connector code may see these."""
    from . import vault   # imported here: vault imports db
    out = {}
    for f in _secret_fields(DEFINITIONS.get(inst.get("key"))):
        try:
            out[f] = vault.get(_vault_name(inst["id"], f)) or ""
        except Exception as e:   # a secret stored on another machine reads as unset; the vault row says why
            print(f"Connector {inst.get('id')}: the secret {f} could not be read: {_error_text(e)}")
            out[f] = ""
    return out


def _mode(inst: dict) -> str | None:
    """"test" or "live" when the definition can tell, else None."""
    fn = (DEFINITIONS.get(inst.get("key")) or {}).get("test_mode")
    if not callable(fn):
        return None
    try:
        return "test" if fn(inst) else "live"
    except Exception:
        return None


def _signed_in(inst: dict) -> bool:
    try:
        from . import vault
        return bool(vault.get(f"oauth.{inst['id']}"))
    except Exception:
        return False


def connected(company) -> list[dict]:
    """What a company has connected, for the facts block: [{key, label, category, mode, status, detail, account}],
    where account reads like "Stripe (payments, test mode)"."""
    out = []
    for inst in visible(company):
        d = DEFINITIONS.get(inst["key"]) or {}
        mode = _mode(inst)
        bits = [b for b in (str(d.get("category") or "").lower(), f"{mode} mode" if mode else "",
                            "down" if inst.get("status") == "bad" else "") if b]
        label = _label(d, inst["key"])
        out.append({"key": inst["key"], "label": label, "category": d.get("category") or "", "mode": mode,
                    "status": inst.get("status") or "", "detail": _clean(inst.get("detail"), 160),
                    "account": label + (f" ({', '.join(bits)})" if bits else "")})
    return out


def _service_names(company) -> list[str]:
    """"stripe (test mode)", "playwright", ... for the prompt and find_tool."""
    out = []
    for inst in visible(company):
        mode = _mode(inst)
        out.append(inst["key"] + (f" ({mode} mode)" if mode else ""))
    return out


def mark_stale(inst_id: int) -> None:
    """Ask for a fresh health check on the next scheduler tick (for example after an OAuth sign-in) and forget
    any open client, so the next call connects with the new credentials."""
    _forget(to_int(inst_id))
    db.run("UPDATE connectors SET last_check=NULL WHERE id=?", (to_int(inst_id),))


# ---------------------------------------------------------------- MCP clients and tool lists

def _alive(c) -> bool:
    """Whether a cached client still works. Uses client.alive() / is_alive() when the client has one, else a
    closed flag, else the stdio process (re-created when the process has exited)."""
    for attr in ("alive", "is_alive"):
        f = getattr(c, attr, None)
        if callable(f):
            try:
                return bool(f())
            except Exception:
                return False
        if isinstance(f, bool):
            return f
    if getattr(c, "closed", False):
        return False
    proc = getattr(c, "proc", None) or getattr(getattr(c, "transport", None), "proc", None) \
        or getattr(getattr(c, "_t", None), "proc", None)
    if proc is not None and hasattr(proc, "poll"):
        try:
            return proc.poll() is None
        except Exception:
            return False
    return True


def _close_quietly(c) -> None:
    try:
        c.close()
    except Exception:
        pass


def _drop_client(inst_id: int) -> None:
    """Forget an instance's MCP client and close it in the background (a stdio close can take seconds)."""
    with _lock:
        c = _clients.pop(inst_id, None)
    if c is not None:
        threading.Thread(target=_close_quietly, args=(c,), daemon=True, name="mcp-close").start()


def _forget(inst_id: int) -> None:
    """Drop everything cached for an instance: its client and its tool list."""
    _drop_client(inst_id)
    with _lock:
        _tool_cache.pop(inst_id, None)


def _fill(v, values: dict):
    """Replace {field} placeholders in MCP settings with the instance's config and secret values."""
    if isinstance(v, str):
        return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), v)
    if isinstance(v, (list, tuple)):
        return [_fill(x, values) for x in v]
    if isinstance(v, dict):
        return {k: _fill(x, values) for k, x in v.items()}
    return v


def _mcp_settings(d: dict, inst: dict) -> dict:
    """Keyword arguments for mcp.Client from the definition's mcp dict. Each setting may be a value or a
    callable taking the instance; strings may carry {field} placeholders."""
    spec = d.get("mcp") or {}
    values = {k: v for k, v in inst["config"].items() if isinstance(v, (str, int, float))}
    values.update(secrets(inst))

    def res(name):
        v = spec.get(name)
        if callable(v):
            v = v(inst)
        return _fill(v, values)

    if d.get("kind") == "mcp-stdio":
        return {"transport": "stdio", "command": res("command"), "args": list(res("args") or []),
                "env": res("env") or None}
    auth = None
    if _signed_in(inst):
        from . import oauth   # imported here: oauth talks to the vault and the network
        auth = oauth.TokenSource(inst["id"])
    return {"transport": "http", "url": res("url"), "headers": res("headers") or None, "auth": auth}


def client(inst: dict):
    """An open (connected) mcp.Client for an MCP instance, kept in _clients and re-created when it has died."""
    d = DEFINITIONS.get(inst.get("key")) or {}
    if d.get("kind") not in MCP_KINDS:
        raise RuntimeError(f"{_label(d, inst.get('key', ''))} is not an MCP connector.")
    iid = inst["id"]
    with _lock:
        lock = _client_locks.setdefault(iid, threading.Lock())
    with lock:
        with _lock:
            c = _clients.get(iid)
        if c is not None and _alive(c):
            return c
        if c is not None:
            _drop_client(iid)
        from . import mcp   # imported here: only MCP connectors need it
        kw = _mcp_settings(d, inst)
        c = mcp.Client(inst["key"], kw.pop("transport"), **kw)
        c.connect()
        with _lock:
            _clients[iid] = c
        return c


def _mcp_tools(inst: dict, fresh: bool = False) -> list[dict]:
    """tools/list for an MCP instance, cached for TOOL_CACHE_SECONDS."""
    now = time.monotonic()
    with _lock:
        hit = _tool_cache.get(inst["id"])
    if hit and not fresh and now - hit[0] < TOOL_CACHE_SECONDS:
        return hit[1]
    try:
        listed = [t for t in (client(inst).list_tools() or []) if isinstance(t, dict) and t.get("name")]
    except Exception:
        with _lock:
            c = _clients.get(inst["id"])
        if c is not None and not _alive(c):
            _drop_client(inst["id"])
        raise
    with _lock:
        _tool_cache[inst["id"]] = (now, listed)
    return listed


def _mcp_health(inst: dict) -> tuple[str, str]:
    """Default health for an MCP definition without its own: the server starts and lists its tools."""
    n = len(_mcp_tools(inst, fresh=True))
    return "ok", f"{n} tool{'s' if n != 1 else ''} available."


def _classify(d: dict, tool: str) -> str:
    """The tag of an MCP tool from the definition's ordered classify rules (first match wins)."""
    rules = (d.get("mcp") or {}).get("classify") or []
    items = rules.items() if isinstance(rules, dict) else rules
    for item in items:
        try:
            glob, tag = item
        except (TypeError, ValueError):
            continue
        if fnmatch.fnmatchcase(tool, str(glob)):
            return tag if tag in config.RISK_TAGS else UNCLASSIFIED
    return UNCLASSIFIED


def _blocked(d: dict | None) -> set:
    d = d or {}
    return {str(x) for x in ((d.get("mcp") or {}).get("blocked") or []) + list(d.get("blocked") or [])}


def _role_keys(agent) -> set:
    if not agent:
        return set()
    if isinstance(agent, str):
        return {agent}
    return {k for k in (agent.get("role"), roles.preset_key(agent)) if k}


def _allowed(d: dict, tool: str, tag: str, role_keys: set) -> bool:
    """api and builtin connectors allow every role. MCP connectors allow read tools for every role, and the
    rest only when a glob in the allow map matches for the agent's role (or for "*")."""
    if d.get("kind") not in MCP_KINDS or tag == "read":
        return True
    allow = (d.get("mcp") or {}).get("allow") or {}
    globs = [g for r in (*role_keys, "*") for g in (allow.get(r) or [])]
    return any(fnmatch.fnmatchcase(tool, str(g)) for g in globs)


def _slim(schema) -> dict:
    """A tool's parameters with only type, properties, required and description (MCP schemas can be large).
    A definition may also give the properties dict alone."""
    if not isinstance(schema, dict):
        schema = {}
    if not isinstance(schema.get("type"), str) and "properties" not in schema:
        schema = {"type": "object", "properties": schema}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    out = {"type": "object", "properties": props,
           "required": [r for r in (schema.get("required") or []) if isinstance(r, str) and r in props]}
    if isinstance(schema.get("description"), str):
        out["description"] = schema["description"]
    return out


def _placeholder(key: str) -> dict:
    """An instance-shaped dict for listing a definition's tools before anything is connected."""
    return {"id": 0, "company_id": None, "key": key, "enabled": 1, "config": {}, "status": "", "detail": "",
            "last_check": None, "created_at": None}


def _api_tools(d: dict, inst: dict) -> list[dict]:
    """The definition's tools(inst) for api and builtin kinds, with the name unprefixed and the tag checked."""
    fn = d.get("tools")
    if d.get("kind") in MCP_KINDS or not callable(fn):
        return []
    prefix = f"{inst.get('key')}__"
    out = []
    for t in fn(inst) or []:
        if not isinstance(t, dict) or not t.get("name") or not callable(t.get("fn")):
            continue
        name = str(t["name"])
        name = name[len(prefix):] if name.startswith(prefix) else name
        tag = t.get("tag") if t.get("tag") in config.RISK_TAGS else UNCLASSIFIED
        out.append({**t, "name": name, "tag": tag})
    return out


def _catalog(company, agent) -> tuple[list[dict], list[str]]:
    """(entries, problems) for every enabled connector visible to the company. Each entry:
    {name: "<key>__<tool>", description, parameters, tag, instance_id, allowed, connector, tool, label}.
    Blocked MCP tools are never listed; problems name connectors whose tools could not be listed."""
    role_keys = _role_keys(agent)
    entries: list[dict] = []
    problems: list[str] = []
    for inst in visible(company):
        key = inst["key"]
        d = DEFINITIONS.get(key) or {}
        label = _label(d, key)
        try:
            if d.get("kind") in MCP_KINDS:
                if inst.get("status") == "bad":
                    problems.append(f"{label} is down ({_clean(inst.get('detail'), 160) or 'the last check failed'})")
                    continue
                blocked = _blocked(d)
                raw = [(str(t["name"]), str(t.get("description") or ""), t.get("inputSchema") or {},
                        _classify(d, str(t["name"]))) for t in _mcp_tools(inst) if str(t["name"]) not in blocked]
            else:
                raw = [(t["name"], str(t.get("description") or ""), t.get("parameters") or {}, t["tag"])
                       for t in _api_tools(d, inst)]
        except Exception as e:
            problems.append(f"{label}: {_error_text(e)}")
            continue
        for tool, desc, params, tag in raw:
            entries.append({"name": f"{key}__{tool}", "description": desc, "parameters": _slim(params), "tag": tag,
                            "instance_id": inst["id"], "allowed": _allowed(d, tool, tag, role_keys),
                            "connector": key, "tool": tool, "label": label})
    return entries, problems


def catalog(company, agent=None) -> list[dict]:
    """Every tool the company's connected services offer, with "allowed" for the agent's role."""
    return _catalog(company, agent)[0]


# ---------------------------------------------------------------- find_tool

@tools.tool("find_tool", "Find a tool in the services the owner connected (payments, email, browser, social posting) "
            "and load it for this session. Describe the job in a few words; the best matches are loaded and you can "
            "call them on your next step.",
            {"query": tools.S("What you want to do, e.g. 'create a payment link' or 'open a web page'")},
            ("query",), risk="read")
def find_tool(ctx: tools.Ctx, query: str) -> str:
    query = _one_line(query, 200)
    tools._log(ctx, "tool", f"looked for a connected tool: {query}")
    entries, problems = _catalog(ctx.company, ctx.agent)
    words = list(dict.fromkeys(w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3))
    scored = []
    for e in entries:
        hay = f"{e['name']} {e['description']}".lower()
        score = sum(1 for w in words if w in hay)
        if score:
            scored.append((-score, e["name"], e))
    scored.sort(key=lambda x: (x[0], x[1]))
    matches = [e for _, _, e in scored]
    extra = [f"Could not list tools from {p}." for p in problems]
    if not matches:
        services = ", ".join(_service_names(ctx.company)) or "none"
        return "\n".join([f"No connected tool matches '{query}'. Connected services: {services}."] + extra)
    loaded = [e for e in matches if e["allowed"]][:FIND_LOADED]
    for e in loaded:
        if e["name"] in ctx.loaded_by_name:
            continue
        note = "" if e["tag"] == "read" else ", may wait for the owner's approval"
        spec = {"name": e["name"], "description": f"{e['description'][:900]} [{e['label']}: {e['tag']}{note}]".strip(),
                "parameters": e["parameters"], "tag": e["tag"], "instance_id": e["instance_id"], "allowed": True,
                "connector": e["connector"], "tool": e["tool"]}
        ctx.loaded_tools.append(spec)
        ctx.loaded_by_name[e["name"]] = spec
    lines = []
    for e in matches[:FIND_SHOWN]:
        line = f"{e['name']} [{e['tag']}]: {_one_line(e['description'], 120)}"
        lines.append(line + ("" if e["allowed"] else " (not allowed for your role)"))
    lines += extra
    if any(e["tag"] != "read" for e in loaded):
        lines.append("Tools not tagged read may wait for the owner's approval, like email.")
    if loaded:
        lines.append(f"Loaded: {', '.join(e['name'] for e in loaded)}. You can call them now.")
    else:
        lines.append("None of these is allowed for your role, so nothing was loaded. Create a task for a teammate "
                     "whose role can use it.")
    return "\n".join(lines)


# ---------------------------------------------------------------- calls

def _hook(d: dict, name: str, tool: str, args: dict):
    fn = d.get(name)
    return fn(tool, args) if callable(fn) else None


# How risky each tag is, so a definition's tag_for_call hook can raise the risk of one call but never lower it.
_RANK = {"read": 0, "write": 1, "publish": 2, "contact": 2, UNCLASSIFIED: 2, "spend": 3, "irreversible": 4}


def _raise_tag(tag: str, override) -> str:
    """The riskier of the catalog's tag and a hook's answer (None or an unknown tag keeps the catalog's)."""
    if override not in _RANK:
        return tag
    return override if _RANK[override] >= _RANK.get(tag, 2) else tag


def call(ctx, name: str, args: dict) -> str:
    """A loaded connector tool was called: read runs now, anything else goes through actions.gate."""
    key, sep, tool = name.partition("__")
    d = DEFINITIONS.get(key)
    if not sep or not tool or not d:
        return f"ERROR: there's no connected tool called '{name}'. Use find_tool to load one."
    if tool in _blocked(d):
        return f"ERROR: {tool} is not available through Holdco"
    entry = ctx.loaded_by_name.get(name)
    if entry is None:
        entry = next((e for e in catalog(ctx.company, ctx.agent) if e["name"] == name), None)
    if entry is None:
        return (f"ERROR: {name} is not available to this company. Use find_tool to see what the connected services "
                "offer.")
    if not entry.get("allowed"):
        return f"ERROR: {name} is not allowed for your role."
    inst = get(entry.get("instance_id"))
    if not inst or not inst["enabled"] or inst["key"] != key or inst["company_id"] not in (None, ctx.company["id"]):
        return f"ERROR: {_label(d, key)} is no longer connected for this company."
    args = dict(args or {})
    try:
        refusal = _hook(d, "refuse_call", tool, args)
    except Exception as e:   # a broken rule refuses rather than lets the call through
        refusal = f"{tool} could not be checked ({_error_text(e)})"
    if refusal:
        refusal = str(refusal)
        return refusal if refusal.startswith("ERROR") else f"ERROR: {refusal}"
    tag = entry.get("tag") or UNCLASSIFIED
    try:   # a hook may raise the risk of one call (a refund through a write tool), never lower it
        tag = _raise_tag(tag, _hook(d, "tag_for_call", tool, args))
    except Exception:   # a broken rule asks the owner rather than lets the call run freely
        tag = _raise_tag(tag, UNCLASSIFIED)
    payload = {"connector_id": inst["id"], "connector": key, "tool": tool, "args": args, "tag": tag}
    if inst["company_id"] is None and d.get("kind") not in MCP_KINDS:
        payload["company_id"] = ctx.company["id"]   # a global api tool needs to know whose work this is
    if tag == "read":
        try:
            text = execute(payload, company=ctx.company)
        except RuntimeError as e:
            msg = str(e)
            text = msg if msg.startswith("ERROR") else f"ERROR: {msg}"
        bus.emit(ctx.company["id"], "tool", f"called {key} {tool}", ctx.agent["id"], {"connector": key, "tool": tool})
        return text
    task_id = ctx.task["id"] if ctx.task else None
    return actions.gate(ctx.company, ctx.agent, task_id, "connector_call", payload, policy_action=policy_for(tag))


def execute(payload: dict, company: dict | None = None) -> str:
    """actions.execute for action 'connector_call' (after approval or an auto policy), and read calls. Errors
    raise RuntimeError so gate reports FAILED and the approval row records the failure."""
    inst = get(payload.get("connector_id"))
    key, tool = str(payload.get("connector") or ""), str(payload.get("tool") or "")
    if not inst:
        raise RuntimeError(f"The {key} connector was removed.")
    d = DEFINITIONS.get(inst["key"])
    if not d:
        raise RuntimeError(GONE_TEXT)
    label = _label(d, inst["key"])
    if not inst["enabled"]:
        raise RuntimeError(f"{label} is turned off.")
    args = payload.get("args") or {}
    if not isinstance(args, dict):
        raise RuntimeError("The arguments must be an object.")
    if d.get("kind") in MCP_KINDS:
        if tool in _blocked(d):
            raise RuntimeError(f"{tool} is not available through Holdco")
        from . import mcp   # imported here: only MCP connectors need it
        try:
            result = client(inst).call_tool(tool, args)
        except Exception as e:
            with _lock:
                c = _clients.get(inst["id"])
            if c is not None and (not _alive(c) or isinstance(e, mcp.AuthRequired)):
                _drop_client(inst["id"])
            raise RuntimeError(f"{label} {tool} failed: {_error_text(e)}") from None
        text = _redact(mcp.text_of(result) if isinstance(result, dict) else str(result or ""))
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(text.removeprefix("ERROR: ").strip() or f"{label} {tool} reported an error.")
        return text
    spec = next((t for t in _api_tools(d, inst) if t["name"] == tool), None)
    if not spec:
        raise RuntimeError(f"{label} has no tool called {tool}.")
    if company is None:
        cid = inst["company_id"] if inst["company_id"] is not None else payload.get("company_id")
        company = db.get_company(to_int(cid)) if cid else None
    try:
        out = spec["fn"](inst, company, **args)
    except Exception as e:
        raise RuntimeError(_error_text(e)) from None
    return _redact(str(out if out is not None else ""))


# ---------------------------------------------------------------- health checks

def _health(d: dict, inst: dict) -> tuple[str, str]:
    """Run the definition's health check with its time limit; anything unexpected is bad, never an exception."""
    fn = d.get("health")
    if not callable(fn):
        if d.get("kind") not in MCP_KINDS:
            return "warn", "This connector has no health check."
        fn = _mcp_health
    try:
        timeout = float(d.get("health_timeout") or HEALTH_TIMEOUT)
    except (TypeError, ValueError):
        timeout = HEALTH_TIMEOUT
    try:
        res = _timed(fn, timeout, inst)
    except TimeoutError:
        return "bad", f"The health check took longer than {timeout:g} s."
    except Exception as e:
        return "bad", _error_text(e)
    try:
        status, detail = res
    except (TypeError, ValueError):
        return "bad", "The health check returned something Holdco cannot read."
    status = str(status or "").lower()
    if status not in STATUSES:
        return "bad", f"The health check returned an unknown status ({_clean(status, 40)})."
    return status, _clean(detail)


def _where(inst: dict) -> str:
    if inst.get("company_id") is None:
        return "global"
    c = db.get_company(inst["company_id"])
    return c["name"] if c else f"company {inst['company_id']}"


def _alert(inst: dict, d: dict | None, detail: str) -> None:
    """One phone message per failing instance per day."""
    today = db.now()[:10]
    with _lock:
        if _alerted.get(inst["id"]) == today:
            return
        _alerted[inst["id"]] = today
    where = _where(inst)
    place = "the Settings page" if inst.get("company_id") is None else f"{where}'s Settings tab"
    text = (f"Connector down: {_label(d, inst['key'])} ({'all companies' if where == 'global' else where}). "
            f"{(detail or 'The last check failed').rstrip('. ')}. Fix it in {place}.")
    try:
        from . import notify   # imported here: notify imports tools, which imports this module
        notify.send(text, kind="important")
    except Exception as e:   # the phone must never break a health check
        print(f"Connector alert failed: {_error_text(e)}")


def check(inst: dict, alert: bool = False) -> tuple[str, str]:
    """Health-check one instance and store the result. A bad result closes its MCP client; with alert=True (the
    scheduled checks) it also sends the daily phone alert."""
    d = DEFINITIONS.get(inst["key"])
    if not d:
        status, detail = "bad", GONE_TEXT
    elif not inst.get("enabled"):
        status, detail = "warn", OFF_TEXT
    else:
        status, detail = _health(d, inst)
    db.update("connectors", inst["id"], status=status, detail=detail, last_check=db.now())
    if status == "bad":
        _forget(inst["id"])
        if alert and d:
            _alert(inst, d, detail)
    return status, detail


def check_all(stale_only: bool = False, alert: bool = True) -> list[tuple[int, str, str]]:
    """Check every enabled instance (or only those older than HOLDCO_CONNECTOR_CHECK_SECONDS) right now.
    Returns [(instance id, status, detail)]. The scheduler runs this through check_if_stale."""
    interval = _env_seconds("HOLDCO_CONNECTOR_CHECK_SECONDS", 3600)
    out = []
    for r in _active_rows():
        if stale_only and db.minutes_since(r["last_check"]) * 60 < interval:
            continue
        inst = get(r["id"])   # fresh: an earlier check in this pass may take a while
        if not inst or not inst["enabled"]:
            continue
        try:
            out.append((inst["id"], *check(inst, alert=alert)))
        except Exception as e:   # one broken instance must not stop the others
            print(f"Connector {inst['id']} check failed: {_error_text(e)}")
    _sync_doctor()
    return out


def check_if_stale() -> None:
    """Called by the scheduler every tick: checks instances whose last check is older than
    HOLDCO_CONNECTOR_CHECK_SECONDS (default 3600) in a background thread. Never blocks the tick and never runs
    two passes at once."""
    global _checking
    with _lock:
        if _checking:
            return
    interval = _env_seconds("HOLDCO_CONNECTOR_CHECK_SECONDS", 3600)
    if not any(db.minutes_since(r["last_check"]) * 60 >= interval for r in _active_rows()):
        return
    with _lock:
        if _checking:
            return
        _checking = True

    def work() -> None:
        global _checking
        try:
            check_all(stale_only=True, alert=True)
        except Exception as e:
            print(f"Connector checks failed: {_error_text(e)}")
        finally:
            with _lock:
                _checking = False

    threading.Thread(target=work, daemon=True, name="connector-checks").start()


def down_for(company) -> list[str]:
    """One line per connector the company uses whose last check failed."""
    out = []
    for inst in visible(company):
        if inst.get("status") != "bad":
            continue
        detail = (_clean(inst.get("detail"), 200) or "the last check failed").rstrip(". ")
        out.append(f"{_label(DEFINITIONS.get(inst['key']), inst['key'])}: {detail}. "
                   "Do not plan work that depends on it.")
    return out


def prompt_section(company: dict) -> str:
    """'# Connected services' for the system prompt ("" when nothing is connected)."""
    names = _service_names(company)
    if not names:
        return ""
    lines = ["# Connected services", "- " + ", ".join(names), "Use find_tool to load a tool from them."]
    down = down_for(company)
    if down:
        lines.append("Connectors that are down right now:")
        lines += [f"- {x}" for x in down]
    return "\n".join(lines)


def doctor_rows() -> list[dict]:
    """Doctor rows connector:<id> from the last health checks (no network). Disabled instances and those of
    archived companies are left out."""
    from . import doctor   # imported here: doctor imports notify, which imports tools
    companies = {c["id"]: c for c in db.q("SELECT id, name, status FROM companies")}
    rows = []
    for r in db.q("SELECT * FROM connectors WHERE enabled=1 ORDER BY id"):
        cid = r["company_id"]
        if cid is not None and (cid not in companies or companies[cid]["status"] == "archived"):
            continue
        d = DEFINITIONS.get(r["key"])
        where = companies[cid]["name"] if cid is not None else "global"
        if not d:
            status, detail = "warn", GONE_TEXT
        elif r["status"] in STATUSES:
            status, detail = r["status"], r["detail"] or ""
        else:
            status, detail = "warn", "Not checked yet."
        fix = doctor._link("Connector settings", f"#/c/{cid}/settings" if cid is not None else "#/settings")
        row = doctor._row(f"connector:{r['id']}", f"{_label(d, r['key'])} ({where})", status, _clean(detail), fix)
        row.update(company_id=cid, instance_id=r["id"], last_check=r["last_check"])
        rows.append(row)
    return rows


def _sync_doctor(removed: int | None = None) -> None:
    """Refresh the Doctor's cached connector rows after a check, so the page and the sidebar dot agree with the
    card without waiting for the next hourly run."""
    try:
        from . import doctor
        doctor._merge(doctor_rows())
        if removed is not None:
            with doctor._lock:
                doctor.LAST[:] = [r for r in doctor.LAST if r.get("key") != f"connector:{removed}"]
    except Exception as e:
        print(f"Connector rows could not reach the Doctor: {_error_text(e)}")


# ---------------------------------------------------------------- metric pipes

def _metric_name(key: str, name) -> str:
    """Names come without the connector prefix and get "<key>."; a name that already has a namespace
    (resend's email.sent_7d) keeps it."""
    name = re.sub(r"[^A-Za-z0-9_.]+", "_", str(name or "").strip()).strip("_.")[:60]
    return name if "." in name else f"{key}.{name}"


def _companies_for(inst: dict) -> list[dict]:
    """The companies an instance serves: its own, or (global) every active company without its own instance."""
    if inst.get("company_id") is not None:
        c = db.get_company(inst["company_id"])
        return [c] if c and c["status"] != "archived" else []
    own = {r["company_id"] for r in db.q("SELECT company_id FROM connectors WHERE key=? AND enabled=1 AND "
                                         "company_id IS NOT NULL", (inst["key"],))}
    return [c for c in db.q("SELECT * FROM companies WHERE status!='archived' ORDER BY id") if c["id"] not in own]


def poll_metrics(only: int | None = None) -> list[dict]:
    """Ask every enabled instance with a metrics function (or just instance `only`) for its numbers and write
    them as metrics rows (agent_id NULL, name "<key>.<metric>", source <key>). Instances whose last check was bad
    are skipped by the scheduled poll. Returns [{name, value, note, company_id}] written."""
    insts = [get(only)] if only is not None else [_inst(r) for r in _active_rows()]
    written = []
    for inst in insts:
        if not inst or not inst["enabled"]:
            continue
        d = DEFINITIONS.get(inst["key"]) or {}
        fn = d.get("metrics")
        if not callable(fn) or (only is None and inst.get("status") == "bad"):
            continue
        for company in _companies_for(inst):
            try:
                values = _timed(fn, METRICS_TIMEOUT, inst, company) or []
            except Exception as e:
                print(f"Connector {inst['key']} ({inst['id']}): metrics failed: {_error_text(e)}")
                continue
            ts = db.now()
            for item in values:
                try:
                    if isinstance(item, dict):
                        name, value, note = item.get("name"), item.get("value"), item.get("note", "")
                    else:
                        name, value, note = (list(item) + ["", "", ""])[:3]
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                name, note = _metric_name(inst["key"], name), _clean(note, 300)
                db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, source, created_at) "
                       "VALUES(?,?,?,?,?,?,?)", (company["id"], None, name, value, note, inst["key"], ts))
                written.append({"name": name, "value": value, "note": note, "company_id": company["id"]})
    return written


def poll_if_due() -> None:
    """Called by the scheduler every tick: polls metrics every HOLDCO_METRICS_POLL_SECONDS (default 3600) in a
    background thread, at most one poll at a time. Does nothing when no instance has metrics."""
    global _polling, _last_poll
    interval = _env_seconds("HOLDCO_METRICS_POLL_SECONDS", 3600)
    now = time.monotonic()
    with _lock:
        if _polling or (_last_poll is not None and now - _last_poll < interval):
            return
    if not any(callable((DEFINITIONS.get(r["key"]) or {}).get("metrics")) for r in _active_rows()):
        return
    with _lock:
        if _polling:
            return
        _polling, _last_poll = True, now

    def work() -> None:
        global _polling
        try:
            poll_metrics()
        except Exception as e:
            print(f"Connector metrics poll failed: {_error_text(e)}")
        finally:
            with _lock:
                _polling = False

    threading.Thread(target=work, daemon=True, name="connector-metrics").start()


# ---------------------------------------------------------------- saving and removing (the dashboard API)

def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def save(b: dict) -> tuple[dict, tuple[str, str]]:
    """Create or update an instance from the dashboard's body {"id"?, key, company_id, enabled, config} and run
    its health check. Secret fields go to the vault (the mask keeps a secret, an empty value deletes it); only
    the definition's other fields and api_base are kept in config. Raises HTTPError on bad input."""
    b = b if isinstance(b, dict) else {}
    key = str(b.get("key") or "").strip()
    iid = to_int(b.get("id"))
    existing = get(iid) if iid else None
    if iid and not existing:
        raise HTTPError(404, "Connector not found")
    if existing and key and key != existing["key"]:
        raise HTTPError(400, "A saved connector cannot change its kind; remove it and add the other one.")
    key = key or (existing or {}).get("key", "")
    d = DEFINITIONS.get(key)
    if not d:
        raise HTTPError(400, f"Unknown connector '{key}'.")
    label = _label(d, key)
    if existing is None:
        cid = to_int(b.get("company_id")) or None
        if d.get("scope", "company") == "company":
            if not cid:
                raise HTTPError(400, f"{label} is set up per company: use the company's Settings tab.")
            if not db.get_company(cid):
                raise HTTPError(404, "Company not found")
        else:
            cid = None
        existing = _inst(db.one("SELECT * FROM connectors WHERE key=? AND company_id IS ? ORDER BY id LIMIT 1",
                                (key, cid)))
    else:
        cid = existing["company_id"]
    cfg_in = b.get("config") or {}
    if not isinstance(cfg_in, dict):
        raise HTTPError(400, "config must be an object of field values.")
    fields = {str(f["key"]): f for f in (d.get("setup") or {}).get("fields") or [] if isinstance(f, dict)
              and f.get("key")}
    secret_keys = set(_secret_fields(d))
    cfg = dict(existing["config"]) if existing else {}
    secret_changes: dict[str, str] = {}
    for k, v in cfg_in.items():
        if k not in fields and k != "api_base":
            continue
        if v is not None and not isinstance(v, (str, bool, int, float)):
            raise HTTPError(400, f"{label}: {(fields.get(k) or {}).get('label') or k} must be a single value.")
        if k in secret_keys:
            if v is None or v == MASK:
                continue
            secret_changes[k] = str(v).strip()
            continue
        if isinstance(v, str):
            v = v.strip()[:2000]
        if k == "api_base" and v and not re.match(r"https?://", str(v)):
            raise HTTPError(400, "api_base must start with http:// or https://")
        if v is None or v == "":
            cfg.pop(k, None)
        else:
            cfg[k] = v
    enabled = _truthy(b["enabled"]) if "enabled" in b else bool(existing["enabled"]) if existing else True
    created = existing is None
    if created:
        iid = db.run("INSERT INTO connectors(company_id, key, enabled, config, status, detail, created_at) "
                     "VALUES(?,?,?,?,?,?,?)", (cid, key, int(enabled), json.dumps(cfg), "", "", db.now()))
    else:
        iid = existing["id"]
        db.update("connectors", iid, enabled=int(enabled), config=json.dumps(cfg))
    from . import vault   # imported here: vault imports db
    try:
        for k, v in secret_changes.items():
            if v:
                vault.put(_vault_name(iid, k), v)
            else:
                vault.delete(_vault_name(iid, k))
    except Exception as e:
        if created:
            db.run("DELETE FROM connectors WHERE id=?", (iid,))
        raise HTTPError(400, f"The vault could not store the secret: {_error_text(e)}") from None
    _forget(iid)   # new settings or a new key: the next call connects afresh (and a disabled one is closed)
    health = check(get(iid))
    _sync_doctor()
    return get(iid), health


def remove(inst_id: int) -> None:
    """Delete an instance, its vault secrets and OAuth tokens, and close its MCP client."""
    inst = get(inst_id)
    if not inst:
        return
    iid = inst["id"]
    _forget(iid)
    from . import vault
    names = {_vault_name(iid, f) for f in _secret_fields(DEFINITIONS.get(inst["key"]))} | {f"oauth.{iid}"}
    try:
        names |= {n for n in vault.names() if n.startswith(f"connector.{iid}.")}
    except Exception:
        pass
    for n in sorted(names):
        try:
            vault.delete(n)
        except Exception as e:
            print(f"Connector {iid}: could not delete {n} from the vault: {_error_text(e)}")
    db.run("DELETE FROM connectors WHERE id=?", (iid,))
    with _lock:
        _alerted.pop(iid, None)
        _client_locks.pop(iid, None)
    _sync_doctor(removed=iid)


def _card_tools(key: str, d: dict) -> list[dict]:
    """The tools a card lists with their tags: an api definition's tools, or an MCP definition's classify rules."""
    try:
        if d.get("kind") in MCP_KINDS:
            rules = (d.get("mcp") or {}).get("classify") or []
            items = rules.items() if isinstance(rules, dict) else rules
            return [{"name": f"{key}__{g}", "tool": str(g), "description": "",
                     "tag": t if t in config.RISK_TAGS else UNCLASSIFIED} for g, t in items]
        return [{"name": f"{key}__{t['name']}", "tool": t["name"], "description": str(t.get("description") or ""),
                 "tag": t["tag"]} for t in _api_tools(d, _placeholder(key))]
    except Exception:
        return []


def _card(key: str, d: dict) -> dict:
    return {"key": key, "label": _label(d, key), "category": d.get("category") or "", "kind": d.get("kind") or "",
            "scope": d.get("scope") or "company", "setup": _jsonable(d.get("setup") or {}),
            "tools": _card_tools(key, d), "blocked": sorted(_blocked(d)),
            "oauth": bool(d.get("oauth", d.get("kind") == "mcp-http")), "has_metrics": callable(d.get("metrics")),
            "has_test_mode": callable(d.get("test_mode"))}


def _latest_metrics(inst: dict, company_id: int | None = None) -> list[dict]:
    cid = inst.get("company_id") or company_id
    if not cid:
        return []
    return db.q("SELECT name, value, note, created_at FROM metrics WHERE id IN (SELECT MAX(id) FROM metrics "
                "WHERE company_id=? AND source=? GROUP BY name) ORDER BY name", (cid, inst["key"]))


def public(inst: dict, company_id: int | None = None) -> dict:
    """An instance for the dashboard: config with secret fields shown as the mask (or "" when not set)."""
    d = DEFINITIONS.get(inst["key"]) or {}
    secret_keys = _secret_fields(d)
    cfg = {k: v for k, v in inst["config"].items() if k not in secret_keys}
    have = secrets(inst) if secret_keys else {}
    for k in secret_keys:
        cfg[k] = MASK if have.get(k) else ""
    company = db.get_company(inst["company_id"]) if inst.get("company_id") is not None else None
    oauth = bool(d.get("oauth", d.get("kind") == "mcp-http"))
    return {"id": inst["id"], "company_id": inst.get("company_id"),
            "company_name": company["name"] if company else None, "key": inst["key"], "label": _label(d, inst["key"]), "kind": d.get("kind") or "",
            "enabled": bool(inst.get("enabled")), "config": cfg, "status": inst.get("status") or "",
            "detail": inst.get("detail") or "", "last_check": inst.get("last_check"),
            "created_at": inst.get("created_at"), "mode": _mode(inst),
            "signed_in": _signed_in(inst) if oauth else False, "metrics": _latest_metrics(inst, company_id)}


def _inst_or_404(inst_id) -> dict:
    inst = get(inst_id)
    if not inst:
        raise HTTPError(404, "Connector not found")
    return inst


@route("GET", "/api/connectors")
def api_connectors(h, m, body):
    query = getattr(h, "query", None) or {}
    cid = to_int((query.get("company_id") or [""])[0])
    if cid:
        if not db.get_company(cid):
            raise HTTPError(404, "Company not found")
        rows = db.q("SELECT * FROM connectors WHERE company_id IS NULL OR company_id=? ORDER BY id", (cid,))
    else:
        rows = db.q("SELECT * FROM connectors ORDER BY id")
    instances = [public(_inst(r), cid or None) for r in rows]
    return {"definitions": {k: _card(k, d) for k, d in DEFINITIONS.items()}, "instances": instances,
            "statuses": {str(i["id"]): {"status": i["status"], "detail": i["detail"], "last_check": i["last_check"]}
                         for i in instances}}


@route("POST", "/api/connectors")
def api_connector_save(h, m, body):
    inst, (status, detail) = save(body or {})
    return {"instance": public(inst), "health": {"status": status, "detail": detail}}


@route("POST", r"/api/connectors/(\d+)/test")
def api_connector_test(h, m, body):
    inst = _inst_or_404(m.group(1))
    status, detail = check(inst)
    metrics = poll_metrics(only=inst["id"]) if status == "ok" else []
    _sync_doctor()
    return {"health": {"status": status, "detail": detail}, "metrics": metrics}


@route("DELETE", r"/api/connectors/(\d+)")
def api_connector_delete(h, m, body):
    remove(_inst_or_404(m.group(1))["id"])
    return {"ok": True}


@route("POST", r"/api/connectors/(\d+)/oauth/start")
def api_connector_oauth_start(h, m, body):
    inst = _inst_or_404(m.group(1))
    from . import oauth   # imported here: OAuth is only needed for hosted MCP servers
    try:
        url = oauth.start(inst["id"])
    except Exception as e:
        raise HTTPError(400, _error_text(e)) from None
    return {"url": url}


# ---------------------------------------------------------------- definitions

def _load_definitions() -> None:
    """Collect DEFINITION from each conn_*.py module. Runs last, so a definition module may import this one."""
    for name in MODULES:
        try:
            mod = importlib.import_module(f"{__package__}.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"{__package__}.{name}":
                print(f"Connector module {name} could not load: {e}")
            continue
        except Exception as e:   # a broken connector must not stop Holdco from starting
            print(f"Connector module {name} could not load: {type(e).__name__}: {e}")
            continue
        d = getattr(mod, "DEFINITION", None)
        if not isinstance(d, dict):
            continue
        key = str(d.get("key") or name.removeprefix("conn_"))
        if d.get("kind") not in KINDS or d.get("scope", "company") not in SCOPES:
            print(f"Connector {key}: kind or scope is not valid; skipped.")
            continue
        DEFINITIONS[key] = d


_load_definitions()
